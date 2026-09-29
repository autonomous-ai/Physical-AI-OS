package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// chatSendTimeout bounds the loopback call.
const chatSendTimeout = 60 * time.Second

var chatSendClient = &http.Client{Timeout: chatSendTimeout}

// handleChatSend forwards the message and acks with the run id. Replies use
// kind chat.event, including synchronous local replies — see chat_stream.go.
func (h *DeviceMQTTHandler) handleChatSend(env domain.MQTTDataCommand) error {
	var data domain.MQTTChatSendData
	if err := json.Unmarshal(env.Data, &data); err != nil {
		slog.Error("chat.send: bad payload", "component", "mqtt-chat", "error", err)
		return h.publishDataResult(env.Kind, "failure", "invalid data: "+err.Error(), nil)
	}
	if strings.TrimSpace(data.Message) == "" {
		return h.publishDataResult(env.Kind, "failure", "message is required", nil)
	}

	// The agent can finish before the loopback POST returns its run ID. Capture
	// first, then release only events belonging to the acknowledged run.
	var finishCapture func(string, string)
	if h.chatStream != nil {
		finishCapture = h.chatStream.capture()
		defer finishCapture("", "")
	}

	runID, err := h.forwardChatToSensing(data)
	if err != nil {
		slog.Error("chat.send: forward failed", "component", "mqtt-chat", "error", err)
		return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
	}

	if finishCapture != nil {
		finishCapture(runID, data.SessionID)
	}

	slog.Info("chat.send accepted", "component", "mqtt-chat",
		"run_id", runID, "session_id", data.SessionID,
		"speak", data.Speak, "image_count", len(data.Images),
		"file_count", len(data.Files), "msg_len", len(data.Message))

	return h.publishDataResult(env.Kind, "success", "", domain.MQTTChatSendResult{
		RunID:     runID,
		SessionID: data.SessionID,
	})
}

// sensingRequest is the subset of the sensing endpoint's body this needs.
type sensingRequest struct {
	Type    string               `json:"type"`
	Message string               `json:"message"`
	Images  []string             `json:"images,omitempty"`
	Files   []domain.InboundFile `json:"files,omitempty"`
}

// sensingReply accepts both agent runs and synchronous local intent runs.
type sensingReply struct {
	Status  int    `json:"status"`
	Message string `json:"message"`
	Data    struct {
		RunID          string `json:"runId"`
		LocalRunID     string `json:"localRunId"`
		Handler        string `json:"handler"`
		HandledLocally string `json:"handledLocally"`
		Response       string `json:"response"`
	} `json:"data"`
}

func (h *DeviceMQTTHandler) forwardChatToSensing(data domain.MQTTChatSendData) (string, error) {
	evtType := "mqtt_chat"
	if data.Speak {
		evtType = "voice"
	}

	body, err := json.Marshal(sensingRequest{
		Type:    evtType,
		Message: data.Message,
		Images:  data.Images,
		Files:   data.Files,
	})
	if err != nil {
		return "", fmt.Errorf("marshal sensing event: %w", err)
	}

	url := fmt.Sprintf("http://127.0.0.1:%d/api/sensing/event", h.config.HttpPort)
	ctx, cancel := context.WithTimeout(context.Background(), chatSendTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build sensing request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := chatSendClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("post sensing event: %w", err)
	}
	defer resp.Body.Close()

	var reply sensingReply
	if err := json.NewDecoder(resp.Body).Decode(&reply); err != nil {
		return "", fmt.Errorf("decode sensing reply (http %d): %w", resp.StatusCode, err)
	}
	if resp.StatusCode >= 400 || reply.Status != 1 {
		if reply.Message != "" {
			return "", fmt.Errorf("sensing rejected the turn: %s", reply.Message)
		}
		return "", fmt.Errorf("sensing rejected the turn: http %d", resp.StatusCode)
	}
	if reply.Data.RunID == "" && reply.Data.Handler == "local" && reply.Data.HandledLocally == "true" {
		reply.Data.RunID = reply.Data.LocalRunID
		if reply.Data.RunID != "" && h.chatStream != nil {
			h.chatStream.handle(domain.MonitorEvent{
				ID: reply.Data.RunID + "-final", Time: time.Now().UTC().Format(time.RFC3339Nano),
				Type: "chat_response", RunID: reply.Data.RunID, State: "final",
				Summary: reply.Data.Response,
				Detail:  map[string]string{"role": "assistant", "message": reply.Data.Response},
			})
		}
	}
	if reply.Data.RunID == "" {
		return "", fmt.Errorf("sensing returned no run id")
	}
	return reply.Data.RunID, nil
}
