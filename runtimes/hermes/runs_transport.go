package hermes

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// managedRunHTTPError retains a definitive control rejection separately from an ambiguous network failure: a lost steer acknowledgement must not be retried.
type managedRunHTTPError struct {
	StatusCode int
	Body       string
}

func (e *managedRunHTTPError) Error() string {
	return fmt.Sprintf("hermes runs status %d: %s", e.StatusCode, e.Body)
}

func (s *HermesService) managedRunRequest(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var input io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		input = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(BaseURL, "/")+path, input)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+APIKey)
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		defer resp.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, &managedRunHTTPError{resp.StatusCode, string(raw)}
	}
	return resp, nil
}

func (s *HermesService) discoverRunSteering(ctx context.Context) bool {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	resp, err := s.managedRunRequest(ctx, http.MethodGet, "/v1/capabilities", nil)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	var caps struct {
		Features map[string]json.RawMessage `json:"features"`
	}
	if json.NewDecoder(resp.Body).Decode(&caps) != nil {
		return false
	}
	for _, key := range []string{"run_submission", "run_events_sse", "run_status", "run_stop", "run_steer"} {
		var enabled bool
		if json.Unmarshal(caps.Features[key], &enabled) != nil || !enabled {
			return false
		}
	}
	// Native progress must retain the same tool results and cache evidence as Responses before changing transport on a production device.
	var runs struct {
		Enriched bool `json:"autonomous_run_events_v1"`
	}
	return json.Unmarshal(caps.Features["runs_idempotency"], &runs) == nil && runs.Enriched
}

// managedRunInput emits the canonical content shape consumed directly by Hermes run_conversation.
func managedRunInput(input any) (any, error) {
	if text, ok := input.(string); ok {
		if strings.TrimSpace(text) == "" {
			return nil, fmt.Errorf("empty managed run input")
		}
		return text, nil
	}
	messages, ok := input.([]inputMessage)
	if !ok || len(messages) != 1 || messages[0].Role != "user" {
		return nil, fmt.Errorf("unsupported managed run input")
	}
	parts := make([]map[string]any, 0, len(messages[0].Content))
	for _, part := range messages[0].Content {
		switch part.Type {
		case "input_text", "text":
			if part.Text != "" {
				parts = append(parts, map[string]any{"type": "text", "text": part.Text})
			}
		case "input_image", "image_url":
			imageURL := strings.TrimSpace(part.ImageURL)
			lower := strings.ToLower(imageURL)
			if !(strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://") || (strings.HasPrefix(lower, "data:image/") && strings.Contains(imageURL, ","))) {
				return nil, fmt.Errorf("invalid managed run image URL")
			}
			parts = append(parts, map[string]any{"type": "image_url", "image_url": map[string]string{"url": imageURL}})
		default:
			return nil, fmt.Errorf("unsupported managed run content type %q", part.Type)
		}
	}
	if len(parts) == 0 {
		return nil, fmt.Errorf("empty managed run input")
	}
	return []map[string]any{{"role": "user", "content": parts}}, nil
}

func (s *HermesService) createManagedRun(ctx context.Context, body streamRequest, sessionID string) (string, error) {
	input, err := managedRunInput(body.Input)
	if err != nil {
		return "", err
	}
	payload := map[string]any{"input": input, "model": body.Model}
	if sessionID != "" {
		payload["session_id"] = sessionID
	}
	if body.Instructions != "" {
		payload["instructions"] = body.Instructions
	}
	resp, err := s.managedRunRequest(ctx, http.MethodPost, "/v1/runs", payload)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var accepted struct {
		RunID string `json:"run_id"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&accepted); err != nil {
		return "", fmt.Errorf("decode run admission: %w", err)
	}
	if accepted.RunID == "" {
		return "", fmt.Errorf("run admission omitted run_id")
	}
	return accepted.RunID, nil
}

func (s *HermesService) controlManagedRun(ctx context.Context, runID, action, text string) error {
	if action != "steer" && action != "stop" {
		return fmt.Errorf("unsupported managed run action %q", action)
	}
	var body any = map[string]any{}
	if action == "steer" {
		body = map[string]string{"input": text}
	}
	resp, err := s.managedRunRequest(ctx, http.MethodPost, "/v1/runs/"+url.PathEscape(runID)+"/"+action, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if action == "steer" {
		var ack struct {
			Accepted bool `json:"accepted"`
		}
		if err = json.NewDecoder(resp.Body).Decode(&ack); err != nil {
			return fmt.Errorf("decode steer acknowledgement: %w", err)
		}
		if !ack.Accepted {
			return fmt.Errorf("steer response did not acknowledge acceptance")
		}
	}
	return nil
}

type managedRunEvent struct {
	Event        string          `json:"event"`
	RunID        string          `json:"run_id"`
	Status       string          `json:"status"`
	SessionID    string          `json:"session_id"`
	Delta        string          `json:"delta"`
	Text         string          `json:"text"`
	Tool         string          `json:"tool"`
	Preview      string          `json:"preview"`
	Output       json.RawMessage `json:"output"`
	Error        json.RawMessage `json:"error"`
	Usage        json.RawMessage `json:"usage"`
	PendingSteer string          `json:"pending_steer"`
	Duration     json.RawMessage `json:"duration"`
	ToolCallID   string          `json:"tool_call_id"`
	Arguments    json.RawMessage `json:"arguments"`
	Result       json.RawMessage `json:"result"`
}

func (s *HermesService) getManagedRunStatus(ctx context.Context, runID string) (managedRunEvent, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	resp, err := s.managedRunRequest(ctx, http.MethodGet, "/v1/runs/"+url.PathEscape(runID), nil)
	if err != nil {
		return managedRunEvent{}, err
	}
	defer resp.Body.Close()
	var state managedRunEvent
	err = json.NewDecoder(resp.Body).Decode(&state)
	return state, err
}

// managedRunUsage bridges the native agent counters to Responses' canonical breakdown; retain preexisting details and do not infer missing cache usage.
func managedRunUsage(raw json.RawMessage) json.RawMessage {
	var usage map[string]json.RawMessage
	if json.Unmarshal(raw, &usage) != nil {
		return raw
	}
	details := map[string]json.RawMessage{}
	if existing := usage["input_tokens_details"]; len(existing) > 0 {
		_ = json.Unmarshal(existing, &details)
	}
	if details == nil {
		details = map[string]json.RawMessage{}
	}
	if value, ok := usage["cache_read_tokens"]; ok {
		details["cached_tokens"] = value
	}
	if value, ok := usage["cache_write_tokens"]; ok {
		details["cache_write_tokens"] = value
	}
	if len(details) > 0 {
		usage["input_tokens_details"], _ = json.Marshal(details)
	}
	normalized, err := json.Marshal(usage)
	if err != nil {
		return raw
	}
	return normalized
}

// readManagedRun consumes the native Runs stream.
func (s *HermesService) readManagedRun(ctx context.Context, runID, deviceRunID string, dispatch func(domain.WSEvent)) (streamResult, string, error) {
	result := streamResult{DeviceRunID: deviceRunID, ResponseID: runID}
	pending := ""
	emit := func(kind string, payload any) {
		raw, _ := json.Marshal(payload)
		s.translateSSE(kind, string(raw), dispatch, &result)
	}
	apply := func(event managedRunEvent) {
		if event.RunID != "" && event.RunID != runID {
			return
		}
		if event.SessionID != "" {
			result.SessionID = event.SessionID
			s.sessionUUID.Store(event.SessionID)
		}
		kind := event.Event
		if kind == "" && event.Status != "" {
			kind = "run." + event.Status
		}
		switch kind {
		case "message.delta":
			emit("response.output_text.delta", map[string]any{"delta": event.Delta})
		case "tool.call.started":
			arguments := "{}"
			if len(event.Arguments) > 0 {
				if json.Unmarshal(event.Arguments, &arguments) != nil {
					arguments = string(event.Arguments)
				}
			}
			emit("response.output_item.added", map[string]any{"item": map[string]any{"type": "function_call", "call_id": event.ToolCallID, "name": event.Tool, "arguments": arguments}})
		case "tool.call.completed":
			output := event.Result
			if len(output) == 0 {
				output = json.RawMessage(`""`)
			}
			emit("response.output_item.added", map[string]any{"item": map[string]any{"type": "function_call_output", "call_id": event.ToolCallID, "output": output}})
		case "tool.started", "tool.completed":
		case "reasoning.available":
			raw, _ := json.Marshal(map[string]any{"runId": deviceRunID, "sessionKey": s.GetSessionKey(), "stream": "tool", "data": map[string]any{"phase": "update", "name": "_thinking", "text": event.Text}})
			dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: raw})
		case "run.completed":
			pending = event.PendingSteer
			var final string
			_ = json.Unmarshal(event.Output, &final)
			response := map[string]any{"id": runID, "output": []any{map[string]any{"type": "message", "role": "assistant", "content": []any{map[string]any{"type": "output_text", "text": final}}}}}
			if len(event.Usage) > 0 {
				response["usage"] = managedRunUsage(event.Usage)
			}
			emit("response.completed", map[string]any{"response": response})
			result.Terminal = true
		case "run.failed", "run.cancelled", "run.interrupted":
			message := "Hermes " + strings.TrimPrefix(kind, "run.")
			var detail string
			if json.Unmarshal(event.Error, &detail) == nil && detail != "" {
				message = detail
			}
			emit("response.failed", map[string]any{"response": map[string]any{"id": runID, "error": map[string]any{"message": message}}})
			result.Terminal = true
		}
	}
	recoverOwner := func() (streamResult, string, error) {
		// Recover the remote owner before releasing local busy state; a broken observer does not cancel the run.
		status, recoveryErr := s.stopAndSettleManagedRun(ctx, runID)
		for recoveryErr != nil && ctx.Err() == nil {
			select {
			case <-ctx.Done():
			case <-time.After(time.Second):
			}
			if ctx.Err() != nil {
				break
			}
			status, recoveryErr = s.stopAndSettleManagedRun(ctx, runID)
		}
		if recoveryErr == nil {
			apply(status)
			if result.Terminal {
				return result, pending, nil
			}
		}
		if recoveryErr != nil {
			return result, pending, recoveryErr
		}
		return result, pending, fmt.Errorf("managed run stream ended before terminal (status %s)", status.Status)
	}
	status, err := s.getManagedRunStatus(ctx, runID)
	if err != nil {
		return recoverOwner()
	}
	emit("response.created", map[string]any{"response": map[string]any{"id": runID, "session_id": status.SessionID}})
	result.SessionID = status.SessionID
	resp, err := s.managedRunRequest(ctx, http.MethodGet, "/v1/runs/"+url.PathEscape(runID)+"/events", nil)
	if err == nil {
		defer resp.Body.Close()
		scan := bufio.NewScanner(resp.Body)
		scan.Buffer(make([]byte, 4096), 8*1024*1024)
		var data strings.Builder
		consume := func() {
			if data.Len() == 0 {
				return
			}
			var e managedRunEvent
			if json.Unmarshal([]byte(data.String()), &e) == nil {
				apply(e)
			}
			data.Reset()
		}
		for scan.Scan() {
			line := scan.Text()
			if line == "" {
				consume()
				if result.Terminal {
					return result, pending, nil
				}
				continue
			}
			if strings.HasPrefix(line, "data:") {
				if data.Len() > 0 {
					data.WriteByte('\n')
				}
				data.WriteString(strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			}
		}
		consume()
		if result.Terminal {
			return result, pending, nil
		}
		err = scan.Err()
	}
	return recoverOwner()
}

// stopAndSettleManagedRun bounds cleanup even after the caller cancels.
// stopAndSettleManagedRun bounds cleanup even after cancel; a nonterminal error means ownership is unresolved (keep busy).
func (s *HermesService) stopAndSettleManagedRun(ctx context.Context, runID string) (managedRunEvent, error) {
	cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	terminal := func(status managedRunEvent) bool {
		switch status.Status {
		case "completed", "failed", "cancelled", "interrupted":
			return true
		}
		return false
	}
	poll := func() (managedRunEvent, error) {
		probe, done := context.WithTimeout(cleanup, 3*time.Second)
		defer done()
		return s.getManagedRunStatus(probe, runID)
	}
	status, err := poll()
	if err == nil && terminal(status) {
		return status, nil
	}
	stopCtx, stopCancel := context.WithTimeout(cleanup, 3*time.Second)
	stopErr := s.controlManagedRun(stopCtx, runID, "stop", "")
	stopCancel()
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-cleanup.Done():
			return status, fmt.Errorf("managed run %s ownership unresolved after stream loss (stop: %v, status: %v): %w", runID, stopErr, err, cleanup.Err())
		case <-ticker.C:
			status, err = poll()
			if err == nil && terminal(status) {
				return status, nil
			}
		}
	}
}
