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

// kind:"chat.send" — start an agent turn from the backend, so a phone app can
// hold the same conversation the web monitor's chat holds.
//
// Deliberately re-enters the device's OWN sensing endpoint over loopback rather
// than calling the AgentGateway directly. That endpoint is where the describe-
// first vision gate, the agent-busy queue fork, the web-chat run marking and the
// flow logging all live (see system/server/sensing/delivery/http/handler.go);
// reaching past it would mean a second copy of that logic, drifting from the
// web chat this is supposed to mirror. Same reason the Hermes gateway hook POSTs
// to /api/agent/channel-turn instead of reaching in.

// chatSendTimeout bounds the loopback call. The endpoint returns as soon as the
// agent turn is forwarded, or a local intent has completed. This covers the
// describe-first gate on an attached image and bounded intent classification.
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

	// Replay and track before acking; the deferred call discards failed sends.
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

// sensingRequest is the subset of the sensing endpoint's body this needs. Not
// the endpoint's own struct: that lives in a package which would import back
// into this one.
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
	// "mqtt_chat" is a typed-chat type (sensingmsg.IsChat) — same behaviour as
	// the monitor composer's "web_chat": TTS suppressed, no wake greeting /
	// opening filler. It is a SEPARATE type only so the Monitor's turn flow can
	// show which chat the turn came from; every gate treats the two alike.
	// `speak` opts back into a spoken reply by sending the turn down the voice
	// path instead — the same distinction the one-way chat hook bridge
	// hardcodes to "voice".
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
		// Local intents finish before this POST returns, without an agent event.
		reply.Data.RunID = reply.Data.LocalRunID
		if reply.Data.RunID != "" && h.chatStream != nil {
			// Capture and replay the final reply with the acknowledged session,
			// including speak=true requests that entered through the voice path.
			h.chatStream.handle(domain.MonitorEvent{
				ID: reply.Data.RunID + "-final", Time: time.Now().UTC().Format(time.RFC3339Nano),
				Type: "chat_response", RunID: reply.Data.RunID, State: "final",
				Summary: reply.Data.Response,
				Detail:  map[string]string{"role": "assistant", "message": reply.Data.Response},
			})
		}
	}
	if reply.Data.RunID == "" {
		// Without a run id nothing can be correlated, so this is a failure even
		// though the turn may well be running.
		return "", fmt.Errorf("sensing returned no run id")
	}
	return reply.Data.RunID, nil
}
