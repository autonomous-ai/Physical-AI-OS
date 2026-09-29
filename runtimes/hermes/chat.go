package hermes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

var errHermesNotReady = errors.New("hermes not ready")

// SendChatMessage sends a user message to Hermes via POST /v1/responses.
func (s *HermesService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "user", nil)
}

// SendSystemChatMessage flags the flow event as system-originated for Flow Monitor.
func (s *HermesService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "system", nil)
}

func (s *HermesService) SendChatMessageWithImages(message string, imagesBase64 []string) (string, error) {
	return s.sendChat(message, imagesBase64, "", "", "user", nil)
}

// NextChatRunID allocates the run / req id pair.
func (s *HermesService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *HermesService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user", nil)
}

func (s *HermesService) SendChatMessageWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user", nil)
}

// SendSlashCommandWithRun — Hermes has no per-channel "deliver:false" flag, so slash commands look the same as any other user input on the wire.
func (s *HermesService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user_slash", nil)
}

func (s *HermesService) SendSlashCommandWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user_slash", nil)
}

// sendChat is the internal entry.
func (s *HermesService) sendChat(message string, imagesBase64 []string, fixedReqID string, fixedRunID string, sourceType string, _ any) (string, error) {
	if !s.ready.Load() {
		return "", errHermesNotReady
	}

	var reqID, idempotencyKey string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		idempotencyKey = fixedRunID
	} else {
		reqID, idempotencyKey = s.NextChatRunID()
	}

	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
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

	body := streamRequest{
		Model:        Model,
		Conversation: s.conversationName(),
		Stream:       true,
	}
	content := []inputContent{{Type: "input_text", Text: wsMessage}}
	imgLen := 0
	for _, img := range imagesBase64 {
		if img == "" {
			continue
		}
		imgLen += len(img)
		content = append(content, inputContent{Type: "input_image", ImageURL: "data:image/jpeg;base64," + img})
	}
	hasImage := len(content) > 1
	if hasImage {
		body.Input = []inputMessage{{Role: "user", Content: content}}
		slog.Info("[hermes /v1/responses] attaching images", "component", "hermes",
			"reqId", reqID, "runId", idempotencyKey, "count", len(content)-1,
			"base64Len", imgLen, "approxKB", imgLen*3/4/1024)
	} else {
		body.Input = wsMessage
	}

	s.busySince.Store(time.Now().UnixMilli())
	s.inFlightStreams.Add(1)
	s.activeTurn.Store(true)

	s.fireAckEmotion(idempotencyKey, message)

	s.SetPendingChatTrace(idempotencyKey, message)

	slog.Info("hermes >>> SEND  user message", "component", "hermes",
		"reqId", reqID,
		"runId", idempotencyKey,
		"sessionKey", s.GetSessionKey(),
		"conversation", body.Conversation,
		"model", body.Model,
		"source", sourceType,
		"hasImage", hasImage,
		"imageCount", len(content)-1,
		"imageBytes", imgLen,
		"msgLen", len(message),
		"message", truncRunes(message, 500))

	flow.Log("chat_send", map[string]any{
		"run_id":      idempotencyKey,
		"type":        sourceType,
		"has_session": s.GetSessionKey() != "",
		"has_image":   hasImage,
		"image_count": len(content) - 1,
		"image_bytes": imgLen,
		"message":     message,
	}, idempotencyKey)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_send",
		Summary: message,
		RunID:   idempotencyKey,
	})

	// Run the SSE stream in a background goroutine: Device callers (sensing handler, voice loop) shouldn't block for the full turn duration.
	s.steeringMu.Lock()
	native := s.SupportsNativeSteering()
	s.steeringMu.Unlock()
	if native {
		s.enqueueManagedRun(idempotencyKey, body, sourceType)
	} else {
		go s.runStream(idempotencyKey, body)
	}

	return idempotencyKey, nil
}

// runStream issues the POST and pumps translated events into the registered handler.
func (s *HermesService) runStream(runID string, body streamRequest) {
	defer func() {
		s.RemovePendingChatTraceByRunID(runID)
		if s.inFlightStreams.Add(-1) == 0 {
			s.SetBusy(false)
		}
	}()
	handler := s.currentHandler()
	dispatch := func(evt domain.WSEvent) {
		if handler == nil {
			return
		}
		if err := handler(context.Background(), evt); err != nil {
			slog.Error("hermes dispatch handler error", "component", "hermes",
				"event", evt.Event, "runID", runID, "error", err)
		}
	}

	ctx, cancel := context.WithCancel(s.runContext())
	defer cancel()

	res, err := s.postStream(ctx, runID, body, dispatch)
	if err != nil {
		slog.Error("hermes stream error", "component", "hermes", "runID", runID, "error", err)
		payload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "lifecycle",
			"data": map[string]any{
				"phase":   "error",
				"error":   err.Error(),
				"endedAt": nowUnixMs(),
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
		return
	}

	if res.Errored {
		slog.Warn("hermes <<< turn FAILED", "component", "hermes",
			"runID", runID, "responseID", res.ResponseID, "error", res.ErrorText)
	} else {
		slog.Info("hermes <<< turn COMPLETE", "component", "hermes",
			"runID", runID,
			"responseID", res.ResponseID,
			"sessionID", s.GetSessionKey(),
			"finalLen", len(res.FinalText),
			"finalPreview", truncRunes(res.FinalText, 300))
	}
}

func (s *HermesService) currentHandler() domain.AgentEventHandler {
	s.handlerMu.Lock()
	h := s.handler
	s.handlerMu.Unlock()
	return h
}
