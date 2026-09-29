package claudecode

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// SendChatMessage sends a user message to Claude Code.
func (s *ClaudeCodeService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "user")
}

// SendSystemChatMessage flags the flow event as system-originated (skill watcher,
// wake greeting, /compact).
func (s *ClaudeCodeService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "system")
}

func (s *ClaudeCodeService) SendChatMessageWithImages(message string, imagesBase64 []string) (string, error) {
	return s.sendChat(message, imagesBase64, "", "", "user")
}

// NextChatRunID allocates the run / req id pair.
func (s *ClaudeCodeService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *ClaudeCodeService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user")
}

func (s *ClaudeCodeService) SendChatMessageWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user")
}

// SendSlashCommandWithRun — Claude Code has no per-channel "deliver:false" flag, so
// slash commands look the same as any other user input on the wire.
func (s *ClaudeCodeService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user_slash")
}

func (s *ClaudeCodeService) SendSlashCommandWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user_slash")
}

// sendChat allocates ids, marks busy, records the pending trace + runID, emits
// chat_input / chat_send flow events for parity with openclaw, and writes the
// message.send frame to the persistent WebSocket.
func (s *ClaudeCodeService) sendChat(message string, imagesBase64 []string, fixedReqID, fixedRunID, sourceType string) (string, error) {
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

	payload := map[string]any{"content": wsMessage, "source": sourceType}
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
	// the in-flight turn.
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)
	s.addPendingRun(reqID, runID)

	s.fireAckEmotion(runID, message)

	s.SetPendingChatTrace(runID, message)

	slog.Info("claudecode >>> SEND user message", "component", "claudecode",
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
		s.removePendingRun(reqID)
		if s.getCurrentRunID() == "" && !s.hasPendingRuns() {
			s.activeTurn.Store(false)
		}
		slog.Error("claudecode send failed", "component", "claudecode", "runID", runID, "error", err)
		return "", fmt.Errorf("send message.send: %w", err)
	}

	s.markPendingRunSent(reqID)

	return runID, nil
}
