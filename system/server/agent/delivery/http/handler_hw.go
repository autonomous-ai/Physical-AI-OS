package http

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// trackFailMessage returns the apology spoken when /servo/track fails.
func trackFailMessage(target string) string {
	return fmt.Sprintf(i18n.One(i18n.PhraseTrackFailFmt), target)
}

// parseTrackTarget returns the first target label (string or []string) of a
// /servo/track body, or "it" when parsing fails.
func parseTrackTarget(body string) string {
	var req struct {
		Target any `json:"target"`
	}
	if err := json.Unmarshal([]byte(body), &req); err != nil {
		return "it"
	}
	switch v := req.Target.(type) {
	case string:
		if v != "" {
			return v
		}
	case []any:
		for _, t := range v {
			if s, ok := t.(string); ok && s != "" {
				return s
			}
		}
	}
	return "it"
}

// prunedImageMarkerRe matches LLM-echoed markers like "[image description removed]".
var prunedImageMarkerRe = regexp.MustCompile(`\[image[^\]]*removed[^\]]*\]`)

// hwMarkerRe matches inline markers like [HW:/emotion:{"emotion":"happy"}].
// Body is optional ([HW:/led/off]) and must not nest objects; the path may use
// colon separators (/audio:play), normalized to slashes by extractHWCalls.
var hwMarkerRe = regexp.MustCompile(`\[HW:((?:/[^{:\]]+(?::[^{:\]]+)*))(?::(\{[^}]*\}))?\]`)

// hwLinkRe matches markdown-link-form markers, e.g. [Lights off!](HW:/led/off:{}).
// The strip-only mirrors (claudecode/codex stripForChannel, web stripHWMarkers,
// HAL strip_rt_markers) MUST stay exactly as loose as this regex.
var hwLinkRe = regexp.MustCompile(`(?i)\[([^\]]*)\]\(\s*HW:\s*((?:/[^(){:\s]+(?::[^(){:\s]+)*))(?::(\{[^}]*\}))?:?\s*\)`)

// normalizeHWMarkers rewrites link-form markers to canonical form, placing the
// marker before the label so it still counts as a leading marker.
func normalizeHWMarkers(text string) string {
	return hwLinkRe.ReplaceAllStringFunc(text, func(m string) string {
		sub := hwLinkRe.FindStringSubmatch(m)
		label, path, body := sub[1], sub[2], sub[3]
		if body == "" {
			body = "{}"
		}
		marker := "[HW:" + path + ":" + body + "]"
		// Label may itself be a canonical marker ([HW:/a:{}](HW:/b:{})); keep both in order.
		if len(label) >= 3 && strings.EqualFold(label[:3], "HW:") {
			return "[HW:" + label[3:] + "]" + marker
		}
		if strings.TrimSpace(label) == "" {
			return marker
		}
		return marker + " " + label
	})
}

type hwCall struct {
	path string
	body string
}

// extractHWCalls returns all HW markers in text and the text with them stripped.
func extractHWCalls(text string) ([]hwCall, string) {
	text = normalizeHWMarkers(text)
	matches := hwMarkerRe.FindAllStringSubmatch(text, -1)
	calls := make([]hwCall, 0, len(matches))
	for _, m := range matches {
		body := m[2]
		if body == "" {
			body = "{}"
		}
		path := "/" + strings.ReplaceAll(strings.TrimPrefix(m[1], "/"), ":", "/")
		calls = append(calls, hwCall{path: path, body: body})
	}
	hasBuddy := false
	paths := make([]string, 0, len(calls))
	for _, c := range calls {
		paths = append(paths, c.path)
		if strings.HasPrefix(c.path, "/buddy/") {
			hasBuddy = true
		}
	}
	if hasBuddy {
		slog.Info("HW markers extracted", "component", "agent-hw", "count", len(calls), "paths", paths)
	}
	return calls, strings.TrimSpace(hwMarkerRe.ReplaceAllString(text, ""))
}

// extractLeadingHWCalls returns markers that precede the first non-marker text.
// Fired at stream time; lifecycle:end skips them via recordFiredHWCount.
func extractLeadingHWCalls(text string) []hwCall {
	text = normalizeHWMarkers(text)
	matches := hwMarkerRe.FindAllStringSubmatchIndex(text, -1)
	var calls []hwCall
	expectedPos := 0
	for _, m := range matches {
		start, end := m[0], m[1]
		for expectedPos < start {
			c := text[expectedPos]
			if c != ' ' && c != '\n' && c != '\t' && c != '\r' {
				return calls
			}
			expectedPos++
		}
		// Body group index is -1 for bodyless markers; text[-1:] would panic.
		body := "{}"
		if m[4] >= 0 {
			body = text[m[4]:m[5]]
		}
		rawPath := text[m[2]:m[3]]
		path := "/" + strings.ReplaceAll(strings.TrimPrefix(rawPath, "/"), ":", "/")
		calls = append(calls, hwCall{path: path, body: body})
		expectedPos = end
	}
	return calls
}

// fireHWCall POSTs one marker and emits its flow/monitor events.
// Returns true on HTTP success (status < 400).
func (h *AgentHandler) fireHWCall(c hwCall, flowRunID string, client *http.Client) bool {
	// Internal control markers, not HAL endpoints.
	if c.path == "/broadcast" || c.path == "/speak" || c.path == "/dm" {
		return true
	}
	// A cancelled turn drops hardware only; must stay AFTER the /dm and
	// /broadcast check so remote delivery survives the cancel gesture.
	if h.isHWCancelled(flowRunID) {
		slog.Info("HW marker dropped -- turn cancelled by physical gesture",
			"component", "agent", "run_id", flowRunID, "path", c.path)
		flow.Log("hw_cancelled", map[string]any{"run_id": flowRunID, "path": c.path, "source": cancelSourceClick}, flowRunID)
		return true
	}
	// Deterministic gate: never drive hardware this device doesn't declare.
	if cap := device.RouteCapability(c.path); cap != "" && !device.Has(h.config.DeviceTypeOrDefault(), cap) {
		slog.Debug("HW marker dropped — device lacks capability",
			"component", "openclaw", "path", c.path, "capability", cap)
		return true
	}
	// These prefixes are served by os-server (:5000/api), not HAL.
	postURL := hal.BaseURL + c.path
	if strings.HasPrefix(c.path, "/wellbeing/") ||
		strings.HasPrefix(c.path, "/mood/") ||
		strings.HasPrefix(c.path, "/music-suggestion/") ||
		strings.HasPrefix(c.path, "/posture/") ||
		strings.HasPrefix(c.path, "/buddy/") {
		postURL = "http://127.0.0.1:5000/api" + c.path
	}
	isBuddy := strings.HasPrefix(c.path, "/buddy/")
	if isBuddy {
		slog.Info("HW marker → buddy POST", "component", "openclaw", "url", postURL, "body", c.body)
	}
	resp, err := client.Post(postURL, "application/json", strings.NewReader(c.body))
	if err != nil {
		slog.Warn("HW marker call failed", "component", "openclaw", "path", c.path, "error", err)
		flow.Log("hw_failed", map[string]any{
			"path": c.path, "args": c.body, "run_id": flowRunID, "error": err.Error(),
		}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_failed", Summary: c.path + " " + err.Error(), RunID: flowRunID})
		return false
	}
	hwOK := resp.StatusCode < 400
	if !hwOK {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		resp.Body.Close()
		slog.Warn("HW marker error response", "component", "openclaw", "path", c.path, "status", resp.StatusCode, "body", string(errBody))

		// Optimistic TTS already played by the time track start fails; correct it.
		if c.path == "/servo/track" {
			target := parseTrackTarget(c.body)
			if err := hal.Speak(trackFailMessage(target)); err != nil {
				slog.Warn("track fallback TTS failed", "component", "openclaw", "error", err)
			}
		}
	} else {
		if isBuddy {
			okBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
			resp.Body.Close()
			slog.Info("HW marker → buddy OK", "component", "openclaw", "path", c.path, "response", string(okBody))
		} else {
			resp.Body.Close()
			slog.Info("HW marker fired", "component", "openclaw", "path", c.path, "run_id", flowRunID)
		}
	}
	switch {
	case strings.Contains(c.path, "/emotion"):
		flow.Log("hw_emotion", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		if hwOK {
			if e := parseEmotion(c.body); e != "" {
				h.lastEmotionMu.Lock()
				h.lastEmotion = e
				h.lastEmotionMu.Unlock()
			}
		}
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_emotion", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.Contains(c.path, "/scene"), strings.Contains(c.path, "/led"):
		flow.Log("hw_led", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: c.path + " " + c.body, RunID: flowRunID})
		// Lock/unlock ambient breathing so it doesn't trample the agent-set LED.
		switch {
		case strings.Contains(c.path, "/led/off"), strings.Contains(c.path, "/scene/off"):
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "agent hw: " + c.path})
		case strings.Contains(c.path, "/led/effect/stop"), strings.Contains(c.path, "/led/restore"):
			// Release only: neither lock nor unlock.
		default:
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent hw: " + c.path})
		}
	case strings.Contains(c.path, "/servo"):
		flow.Log("hw_servo", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.Contains(c.path, "/audio"):
		flow.Log("hw_audio", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/wellbeing/"):
		flow.Log("hw_wellbeing", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_wellbeing", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/mood/"):
		flow.Log("hw_mood", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_mood", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/music-suggestion/"):
		flow.Log("hw_music_suggestion", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_music_suggestion", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/posture/"):
		flow.Log("hw_posture", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_posture", Summary: c.path + " " + c.body, RunID: flowRunID})
	case strings.HasPrefix(c.path, "/buddy/"):
		flow.Log("hw_buddy", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{Type: "hw_buddy", Summary: c.path + " " + c.body, RunID: flowRunID})
	default:
		flow.Log("hw_call", map[string]any{"path": c.path, "args": c.body, "run_id": flowRunID}, flowRunID)
	}
	return hwOK
}

// fireEchoedHWMarkers fires HW markers the agent wrapped in a tool call
// (e.g. `echo '[HW:...]'`) instead of reply text. Returns true when it fired.
func (h *AgentHandler) fireEchoedHWMarkers(toolName, toolArgs, flowRunID string) bool {
	calls, _ := extractHWCalls(toolArgs)
	if len(calls) == 0 {
		return false
	}
	paths := make([]string, 0, len(calls))
	for _, c := range calls {
		paths = append(paths, c.path)
	}
	slog.Warn("agent echoed HW marker(s) inside a tool call instead of emitting them as reply text — firing to HAL so the action is not silently dropped",
		"component", "agent-hw", "tool", toolName, "paths", paths, "run_id", flowRunID)
	h.fireHWCalls(calls, flowRunID)
	return true
}

// fireHWCalls fires calls sequentially in a background goroutine; order matters.
func (h *AgentHandler) fireHWCalls(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	go func() {
		client := &http.Client{Timeout: 5 * time.Second}
		for _, c := range calls {
			h.fireHWCall(c, flowRunID, client)
		}
	}()
}

// fireHWCallsSync fires calls in order in one goroutine and waits at most 100ms.
// Invariants: markers must never overtake each other, and slow calls must never
// be aborted and retried (relative commands like /servo/nudge would double).
func (h *AgentHandler) fireHWCallsSync(calls []hwCall, flowRunID string) {
	if len(calls) == 0 {
		return
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		client := &http.Client{Timeout: 5 * time.Second}
		for _, c := range calls {
			h.fireHWCall(c, flowRunID, client)
		}
	}()
	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		// Budget spent; the goroutine finishes the tail in order.
	}
}
