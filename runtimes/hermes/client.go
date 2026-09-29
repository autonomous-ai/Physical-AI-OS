package hermes

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// hermesSessionHeader is the response header Hermes uses to publish the server-side session UUID (see docs/agentic/hermes.md §3).
const hermesSessionHeader = "X-Hermes-Session-Id"

// streamRequest is the on-wire POST body to /v1/responses.
type streamRequest struct {
	Model        string `json:"model"`
	Conversation string `json:"conversation,omitempty"`
	Stream       bool   `json:"stream"`
	Instructions string `json:"instructions,omitempty"`
	Input        any    `json:"input"`
	Title        string `json:"title,omitempty"`
}

// inputContent represents one element of the multi-part input array used for vision turns.
type inputContent struct {
	Type     string `json:"type"`                // "input_text" | "input_image"
	Text     string `json:"text,omitempty"`      // when Type == "input_text"
	ImageURL string `json:"image_url,omitempty"` // when Type == "input_image"; data: URL or remote URL
}

type inputMessage struct {
	Role    string         `json:"role"`
	Content []inputContent `json:"content"`
}

// streamResult carries the response.id, full assistant text and session UUID from response.completed.
type streamResult struct {
	// DeviceRunID is the device-side idempotency key (device-chat-N-…) the turn was started with.
	DeviceRunID string
	ResponseID  string
	SessionID   string
	FinalText   string
	Terminal    bool
	Errored     bool
	ErrorText   string
}

// postStream issues POST /v1/responses with stream:true and reads the SSE stream until response.completed | response.failed | context cancel | EOF.
func (s *HermesService) postStream(ctx context.Context, deviceRunID string, body streamRequest, dispatch func(domain.WSEvent)) (streamResult, error) {
	bodyBytes, err := json.Marshal(body)
	if err != nil {
		return streamResult{}, fmt.Errorf("marshal request: %w", err)
	}

	url := strings.TrimRight(BaseURL, "/") + "/v1/responses"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(bodyBytes))
	if err != nil {
		return streamResult{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+APIKey)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return streamResult{}, fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()

	if sid := resp.Header.Get(hermesSessionHeader); sid != "" {
		s.sessionUUID.Store(sid)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(resp.Body)
		return streamResult{}, fmt.Errorf("hermes /v1/responses status %d: %s", resp.StatusCode, truncRunes(string(raw), 400))
	}

	return s.readSSE(ctx, deviceRunID, resp.Body, dispatch)
}

// readSSE consumes the SSE byte stream line-by-line into (event, data) pairs and forwards each to translateAndDispatch.
func (s *HermesService) readSSE(ctx context.Context, deviceRunID string, body io.Reader, dispatch func(domain.WSEvent)) (streamResult, error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 1<<20), 8<<20)

	var (
		currentEvent string
		dataBuf      strings.Builder
		result       = streamResult{DeviceRunID: deviceRunID}
	)

	flush := func() {
		defer func() {
			currentEvent = ""
			dataBuf.Reset()
		}()
		data := dataBuf.String()
		if data == "" {
			return
		}
		if data == "[DONE]" {
			return
		}
		s.translateSSE(currentEvent, data, dispatch, &result)
	}

	for scanner.Scan() {
		select {
		case <-ctx.Done():
			return result, ctx.Err()
		default:
		}
		line := scanner.Text()
		if line == "" {
			flush()
			if result.Terminal {
				return result, nil
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		switch {
		case strings.HasPrefix(line, "event:"):
			currentEvent = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			val := strings.TrimPrefix(line, "data:")
			val = strings.TrimPrefix(val, " ")
			if dataBuf.Len() > 0 {
				dataBuf.WriteByte('\n')
			}
			dataBuf.WriteString(val)
		}
		if result.Errored {
			break
		}
	}
	flush()

	if err := scanner.Err(); err != nil {
		slog.Warn("SSE read error mid-stream", "component", "hermes", "error", err)
		return result, fmt.Errorf("sse read: %w", err)
	}
	if !result.Terminal {
		return result, fmt.Errorf("sse stream ended before response.completed or response.failed")
	}
	return result, nil
}
