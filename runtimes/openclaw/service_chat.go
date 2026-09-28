package openclaw

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// A failed WriteMessage has an uncertain outcome; only pre-write disconnects carry this sentinel and are eligible for automatic queue replay.
var errDisconnectedBeforeSend = errors.New("websocket disconnected before send")

// GetConfigJSON reads and returns the raw bytes of openclaw.json.
func (s *OpenclawService) GetConfigJSON() (json.RawMessage, error) {
	path := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read openclaw.json: %w", err)
	}
	return json.RawMessage(data), nil
}

// GetConfiguredChannel reads openclaw.json and returns the first enabled channel name.
func (s *OpenclawService) GetConfiguredChannel() string {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return "channel"
	}
	var cfg struct {
		Channels map[string]struct {
			Enabled *bool `json:"enabled"`
		} `json:"channels"`
	}
	if json.Unmarshal(data, &cfg) != nil {
		return "channel"
	}
	for _, name := range []string{"telegram", "discord", "slack"} {
		if ch, ok := cfg.Channels[name]; ok {
			if ch.Enabled == nil || *ch.Enabled {
				return name
			}
		}
	}
	for name, ch := range cfg.Channels {
		if ch.Enabled == nil || *ch.Enabled {
			return name
		}
	}
	return "channel"
}

// SendChatMessage sends a user message to the OpenClaw agent via WebSocket chat.send RPC.
func (s *OpenclawService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "user")
}

// SendSystemChatMessage sends a system-originated message, flagged as such for Flow Monitor.
func (s *OpenclawService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "system")
}

// SendChatMessageWithImages sends a message with base64 JPEG images to the OpenClaw agent.
func (s *OpenclawService) SendChatMessageWithImages(message string, imagesBase64 []string) (string, error) {
	return s.sendChat(message, imagesBase64, "", "", "user")
}

// NextChatRunID allocates ids for the next chat.send so callers can flow.SetTrace(runID) before flow.Start.
func (s *OpenclawService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

// SendChatMessageWithRun sends using ids from NextChatRunID (must match that pair).
func (s *OpenclawService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user")
}

// SendChatMessageWithImageAndRun sends with image using ids from NextChatRunID.
func (s *OpenclawService) SendChatMessageWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user")
}

// SendSlashCommandWithRun sends a slash command with deliver:false so the reply is not broadcast to bound channels.
func (s *OpenclawService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user", withDeliver(false))
}

// SendSlashCommandWithImageAndRun is SendSlashCommandWithRun with image attachment.
func (s *OpenclawService) SendSlashCommandWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user", withDeliver(false))
}

// sendChatOpt is a functional option that mutates the chat.send params map before the payload is marshaled.
type sendChatOpt func(map[string]interface{})

// withDeliver sets the chat.send `deliver` flag.
func withDeliver(v bool) sendChatOpt {
	return func(p map[string]interface{}) { p["deliver"] = v }
}

// sendChat is the internal implementation for sending chat messages, optionally with an image.
func (s *OpenclawService) sendChat(message string, imagesBase64 []string, fixedReqID string, fixedRunID string, sourceType string, opts ...sendChatOpt) (string, error) {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return "", errDisconnectedBeforeSend
	}

	// reqID labels outbound chat.send from the os server (sensing POST, wake greeting, etc.) — not "audio only".
	var reqID string
	var idempotencyKey string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		idempotencyKey = fixedRunID
	} else {
		reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
		idempotencyKey = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	}

	params := map[string]interface{}{
		"idempotencyKey": idempotencyKey,
	}
	sessionKey := s.GetSessionKey()
	if sessionKey != "" {
		params["sessionKey"] = sessionKey
	}
	for _, opt := range opts {
		opt(params)
	}

	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
	params["message"] = wsMessage
	s.markOutboundChat(wsMessage)
	previewMsg := message
	if len(previewMsg) > 500 {
		previewMsg = previewMsg[:500] + "…"
	}
	flow.Log("chat_input", map[string]any{
		"run_id":  idempotencyKey,
		"source":  sourceType,
		"message": previewMsg,
	}, idempotencyKey)
	attachments := make([]map[string]interface{}, 0, len(imagesBase64))
	imgLen := 0
	for _, img := range imagesBase64 {
		if img == "" {
			continue
		}
		imgLen += len(img)
		attachments = append(attachments, map[string]interface{}{
			"type":     "image",
			"mimeType": "image/jpeg",
			"content":  img,
		})
	}
	hasImage := len(attachments) > 0
	if hasImage {
		params["attachments"] = attachments
		slog.Info("[chat.send] attaching images", "component", "openclaw",
			"reqId", reqID, "runId", idempotencyKey, "count", len(attachments),
			"base64Len", imgLen, "approxKB", imgLen*3/4/1024)
	}

	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "chat.send",
		"params": params,
	}
	body, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal chat.send: %w", err)
	}

	slog.Info("[chat.send] full payload", "component", "openclaw", "reqId", reqID, "payload", string(body))
	slog.Info("[chat.send] >>> sending to OpenClaw", "component", "openclaw",
		"reqId", reqID, "runId", idempotencyKey,
		"sessionKey", sessionKey,
		"message", message,
		"hasImage", hasImage,
		"attachments", func() string {
			if !hasImage {
				return "none"
			}
			return fmt.Sprintf("%dx image/jpeg ~%dKB", len(attachments), imgLen*3/4/1024)
		}(),
		"payloadBytes", len(body))

	s.wsMu.Lock()
	conn = s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		return "", errDisconnectedBeforeSend
	}
	// Set busy before write so IsBusy() covers the gap until lifecycle_start.
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)
	// Register before the write: a fast lifecycle/final may arrive as soon as the peer reads the frame.
	s.SetPendingChatTrace(idempotencyKey, wsMessage)
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		s.RemovePendingChatTraceByRunID(idempotencyKey)
		s.activeTurn.Store(false)
		slog.Error("[chat.send] write failed", "component", "openclaw",
			"reqId", reqID, "runId", idempotencyKey, "error", err)
		return "", fmt.Errorf("write chat.send: %w", err)
	}

	slog.Info("[chat.send] <<< sent OK", "component", "openclaw",
		"reqId", reqID, "runId", idempotencyKey, "hasImage", hasImage)
	flow.Log("chat_send", map[string]any{
		"run_id":      idempotencyKey,
		"type":        sourceType,
		"has_session": sessionKey != "",
		"has_image":   hasImage,
		"image_count": len(attachments),
		"image_bytes": imgLen,
		"message":     message,
	}, idempotencyKey)
	slog.Info("flow correlation", "op", "ws_chat_send", "section", "os_to_openclaw_ws",
		"device_run_id", idempotencyKey, "req_id", reqID, "has_image", hasImage)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_send",
		Summary: message,
		RunID:   idempotencyKey,
	})

	return idempotencyKey, nil
}

// CompactSession sends a sessions.compact RPC to reduce conversation history.
func (s *OpenclawService) CompactSession(sessionKey string) error {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return fmt.Errorf("ws not connected")
	}

	reqID := fmt.Sprintf("compact-%d", s.reqCounter.Add(1))
	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "sessions.compact",
		"params": map[string]interface{}{
			"key": sessionKey,
		},
	}
	body, err := json.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal compact request: %w", err)
	}

	s.wsMu.Lock()
	conn = s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return fmt.Errorf("ws not connected")
	}

	if err := conn.WriteMessage(websocket.TextMessage, body); err != nil {
		return fmt.Errorf("write compact request: %w", err)
	}

	slog.Info("sessions.compact sent", "component", "openclaw", "sessionKey", sessionKey)
	return nil
}

// NewSession resets the agent's in-session conversation history by sending the OpenClaw `/new` text command (alias of `/reset`) via the normal chat.send path.
func (s *OpenclawService) NewSession(sessionKey string) error {
	if _, err := s.sendChat("/new", nil, "", "", "system"); err != nil {
		return fmt.Errorf("send /new: %w", err)
	}
	slog.Info("/new sent", "component", "openclaw", "sessionKey", sessionKey)
	return nil
}

// sessionRotateTokenThreshold is the conversation token count above which a turn triggers an auto-new-session.
const sessionRotateTokenThreshold = 150_000

// ShouldRotateSession rotates on real session token count (see domain.AgentGateway).
func (s *OpenclawService) ShouldRotateSession(totalTokens, _ int) bool {
	return totalTokens > sessionRotateTokenThreshold
}
