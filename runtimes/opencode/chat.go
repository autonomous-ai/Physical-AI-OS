package opencode

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// SendChatMessage sends a user message to OpenCode. Returns the run ID the
// caller uses to correlate flow/monitor events with the resulting turn.
func (s *OpenCodeService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "user")
}

// SendSystemChatMessage flags the flow event as system-originated (skill watcher,
// wake greeting, /compact). Wire payload is identical to SendChatMessage.
func (s *OpenCodeService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "system")
}

func (s *OpenCodeService) SendChatMessageWithImages(message string, imagesBase64 []string) (string, error) {
	return s.sendChat(message, imagesBase64, "", "", "user")
}

// NextChatRunID allocates the run / req id pair. Same shape as openclaw/hermes so
// logs / monitor stay identical across backends.
func (s *OpenCodeService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *OpenCodeService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user")
}

func (s *OpenCodeService) SendChatMessageWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user")
}

// SendSlashCommandWithRun — OpenCode has no per-channel "deliver:false" flag, so
// slash commands look the same as any other user input on the wire. We still tag
// the flow source so logs distinguish web-monitor input from voice.
func (s *OpenCodeService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user_slash")
}

func (s *OpenCodeService) SendSlashCommandWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user_slash")
}

// sendChat allocates ids, marks busy, records the pending trace + runID, emits
// chat_input / chat_send flow events for parity with openclaw, and writes the
// message.send frame to the persistent WebSocket. The reply arrives on the read
// loop and is translated there — this returns as soon as the frame is sent.
func (s *OpenCodeService) sendChat(message string, imagesBase64 []string, fixedReqID, fixedRunID, sourceType string) (string, error) {
	s.sendChatMu.Lock()
	defer s.sendChatMu.Unlock()
	if !s.wsConnected.Load() {
		return "", errDisconnectedBeforeSend
	}

	var reqID, runID string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		runID = fixedRunID
	} else {
		reqID, runID = s.NextChatRunID()
	}

	// Strip [snapshot: ...] paths from presence events so the agent doesn't waste
	// tokens on file paths it has no tools to access (matches openclaw/hermes).
	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
	s.markOutboundChat(wsMessage)

	previewMsg := truncRunes(message, 500)
	flow.Log("chat_input", map[string]any{
		"run_id":  runID,
		"source":  sourceType,
		"message": previewMsg,
	}, runID)

	// Build the outbound frame. Image attachments are best-effort: the text
	// content is always sent so the turn proceeds even if OpenCode ignores the
	// attachment shape.
	payload := map[string]any{"content": wsMessage, "source": sourceType}
	// One attachment entry per image: the frame already carries a LIST, so a
	// chat client that attached several photos sends them in a single turn.
	hasImage := len(imagesBase64) > 0
	if hasImage {
		attachments := make([]map[string]any, 0, len(imagesBase64))
		for _, img := range imagesBase64 {
			if img == "" {
				continue
			}
			attachments = append(attachments, map[string]any{
				"type": "image",
				"url":  "data:image/jpeg;base64," + img,
			})
		}
		if len(attachments) > 0 {
			payload["attachments"] = attachments
		}
	}
	frame := map[string]any{
		"type":    "message.send",
		"id":      reqID,
		"run_id":  runID,
		"payload": payload,
	}
	if sk := s.GetSessionKey(); sk != "" {
		frame["session_id"] = sk
	}

	// Mark busy + stash the runID BEFORE the write so the first inbound frame of
	// this turn adopts it (ensureTurnStarted) and sensing-while-busy gates catch
	// the in-flight turn. Cleared by emitFinal/handleError (or busyTTL).
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)
	s.addPendingRun(reqID, runID)

	// Flash the "thinking" face for visible turns (OpenClaw emotion-acknowledge
	// hook parity). Skips passive sensing + realtime-handled turns. See emotion_ack.go.
	s.fireAckEmotion(runID, message)

	s.SetPendingChatTrace(runID, message)

	slog.Info("opencode >>> SEND user message", "component", "opencode",
		"reqId", reqID, "runId", runID, "sessionKey", s.GetSessionKey(),
		"source", sourceType, "hasImage", hasImage, "msgLen", len(message),
		"message", truncRunes(message, 500))

	flow.Log("chat_send", map[string]any{
		"run_id":      runID,
		"type":        sourceType,
		"has_session": s.GetSessionKey() != "",
		"has_image":   hasImage,
		"message":     message,
	}, runID)

	s.monitorBus.Push(domain.MonitorEvent{Type: "chat_send", Summary: message, RunID: runID})

	if err := s.sendFrame(frame); err != nil {
		// Roll back busy so the next sensing/voice round can proceed.
		s.removePendingRun(reqID)
		if s.getCurrentRunID() == "" && !s.hasPendingRuns() {
			s.activeTurn.Store(false)
		}
		slog.Error("opencode send failed", "component", "opencode", "runID", runID, "error", err)
		return "", fmt.Errorf("send message.send: %w", err)
	}

	s.markPendingRunSent(reqID)

	return runID, nil
}
