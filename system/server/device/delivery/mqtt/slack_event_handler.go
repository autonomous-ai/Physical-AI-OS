package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

const (
	// slackEventForwardTimeout caps the localhost POST so a slow/wedged
	// gateway doesn't keep the MQTT handler goroutine alive forever.
	slackEventForwardTimeout = 10 * time.Second

	// slackEventDedupTTL bounds the in-memory event_id LRU. Slack retries
	// identical events up to ~5 min on non-2xx; this matches.
	slackEventDedupTTL = 5 * time.Minute

	// slackEventForwardURL is the OpenClaw gateway HTTP-mode webhook the
	// slack HTTP-mode config writes (webhookPath=/slack/events, gateway port
	// defaults to 18789).
	slackEventForwardURL = "http://127.0.0.1:18789/slack/events"

	// maxSlackErrorLength truncates the gateway error body before it rides back
	// over MQTT so a verbose 4xx can't bloat the fd_channel payload.
	maxSlackErrorLength = 500
)

// slackEventPayload is the verbatim forward shape published by the
// bff-campaign-service proxy.
type slackEventPayload struct {
	Cmd     string            `json:"cmd"`
	EventID string            `json:"event_id"`
	Body    string            `json:"body"`
	Headers map[string]string `json:"headers"`
}

// slackEventDedup is a tiny LRU keyed on Slack's event_id. We don't need a
// full LRU library: events are short-lived and the working set is small.
var slackEventDedup = struct {
	sync.Mutex
	seen map[string]time.Time
}{seen: make(map[string]time.Time)}

// rememberOrSkip returns true if the event was already seen within the TTL
// window (caller should skip), false on first sight (caller should process).
func rememberOrSkip(eventID string) bool {
	if eventID == "" {
		return false // can't dedup without an ID; forward and let OpenClaw decide
	}
	slackEventDedup.Lock()
	defer slackEventDedup.Unlock()
	now := time.Now()
	if ts, ok := slackEventDedup.seen[eventID]; ok && now.Sub(ts) < slackEventDedupTTL {
		return true
	}
	slackEventDedup.seen[eventID] = now
	if len(slackEventDedup.seen) > 1024 {
		for k, v := range slackEventDedup.seen {
			if now.Sub(v) > slackEventDedupTTL {
				delete(slackEventDedup.seen, k)
			}
		}
	}
	return false
}

// publishSlackResult mirrors publishAddChannelResult's shape so the
// fd_channel response vocabulary stays consistent.
func (h *DeviceMQTTHandler) publishSlackResult(cmdType, eventID, status, errMsg string, httpStatus int) error {
	resp := map[string]any{
		"channel":     domain.ChannelSlack,
		"type":        cmdType,
		"event_id":    eventID,
		"status":      status,
		"error":       errMsg,
		"http_status": httpStatus,
		// Same device/version metadata as every fd_channel ack.
		"info": domain.NewMQTTInfoResponse(h.config, cmdType, device.GetDeviceMac()),
	}
	return h.publish(resp)
}

// handleSlackEvent forwards a proxy-relayed Slack Events API delivery to the
// local OpenClaw gateway.
func (h *DeviceMQTTHandler) handleSlackEvent(cmd domain.MQTTMessage) error {
	return h.forwardSlackHTTP(cmd, domain.CommandSlackEvent)
}

// handleSlackCommand forwards a proxy-relayed Slack slash command to the
// local OpenClaw gateway.
func (h *DeviceMQTTHandler) handleSlackCommand(cmd domain.MQTTMessage) error {
	return h.forwardSlackHTTP(cmd, domain.CommandSlackCommand)
}

// forwardSlackHTTP is the shared Slack forwarder behind handleSlackEvent and
// handleSlackCommand.
func (h *DeviceMQTTHandler) forwardSlackHTTP(cmd domain.MQTTMessage, cmdType string) error {
	var p slackEventPayload
	if err := json.Unmarshal(cmd.Raw(), &p); err != nil {
		slog.Error("slack forward: invalid payload", "component", "mqtt", "cmd", cmdType, "error", err)
		return h.publishSlackResult(cmdType, "", "failure", "invalid JSON payload", 0)
	}
	if p.Body == "" {
		slog.Error("slack forward: empty body", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID)
		return h.publishSlackResult(cmdType, p.EventID, "failure", "empty body", 0)
	}
	if rememberOrSkip(p.EventID) {
		slog.Debug("slack forward: dedup skip", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID)
		return h.publishSlackResult(cmdType, p.EventID, "skipped_duplicate", "", 0)
	}

	if sb, ok := h.agentGateway.(domain.SlackBridge); ok {
		challenge, handled, err := sb.HandleInboundSlack(domain.SlackInbound{Body: p.Body})
		if err != nil {
			slog.Error("slack bridge: handle failed", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "error", err)
			return h.publishSlackResult(cmdType, p.EventID, "failure", err.Error(), 0)
		}
		if challenge != "" {
			slog.Info("slack bridge: url_verification handled", "component", "mqtt", "event_id", p.EventID)
		}
		slog.Debug("slack bridge: handled", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "started_turn", handled)
		return h.publishSlackResult(cmdType, p.EventID, "success", "", 200)
	}

	ctx, cancel := context.WithTimeout(context.Background(), slackEventForwardTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, slackEventForwardURL, bytes.NewBufferString(p.Body))
	if err != nil {
		return h.publishSlackResult(cmdType, p.EventID, "failure", fmt.Sprintf("build request: %v", err), 0)
	}
	// Forward verbatim: OpenClaw re-verifies X-Slack-Signature/-Timestamp.
	for k, v := range p.Headers {
		req.Header.Set(k, v)
	}
	if req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		slog.Error("slack forward: forward failed", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "error", err)
		return h.publishSlackResult(cmdType, p.EventID, "failure", fmt.Sprintf("forward: %v", err), 0)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		slog.Debug("slack forward: forwarded ok", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "status", resp.StatusCode)
		return h.publishSlackResult(cmdType, p.EventID, "success", "", resp.StatusCode)
	}

	errMsg := string(respBody)
	if len(errMsg) > maxSlackErrorLength {
		errMsg = errMsg[:maxSlackErrorLength]
	}
	slog.Warn("slack forward: gateway rejected", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "status", resp.StatusCode, "body", errMsg)
	return h.publishSlackResult(cmdType, p.EventID, "failure", errMsg, resp.StatusCode)
}
