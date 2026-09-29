package http

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// poseBucketRoot is where HAL writes pose buckets (hal/config.py SNAPSHOT_TMP_DIR + "/sensing_pose/buckets/").
const poseBucketRoot = "/tmp/hal-sensing-snapshots/sensing_pose/buckets"

// buildPoseBucketImagePaths returns absolute snapshot paths for a bucket; filenames that would
// escape the bucket dir are dropped.
func buildPoseBucketImagePaths(bucketID string, filenames []string) []string {
	if bucketID == "" || len(filenames) == 0 {
		return nil
	}
	if strings.ContainsAny(bucketID, "/\\") || strings.Contains(bucketID, "..") {
		return nil
	}
	paths := make([]string, 0, len(filenames))
	for _, f := range filenames {
		if strings.ContainsAny(f, "/\\") || strings.Contains(f, "..") {
			continue
		}
		paths = append(paths, filepath.Join(poseBucketRoot, bucketID, f))
	}
	return paths
}

// HandleEvent processes incoming WebSocket events from the OpenClaw gateway.
func (h *AgentHandler) HandleEvent(ctx context.Context, evt domain.WSEvent) error {
	defer h.observeExternalHistory(evt)
	slog.Debug("event received", "component", "agent", "event", evt.Event)

	// Cron "started" precedes the turn's lifecycle_start: cache sessionKey so that run is marked
	// a cron fire and its TTS reaches the device speaker.
	if evt.Event == "cron" {
		// Diagnostic: keep until cron correlation is proven across all sessionTarget variants.
		slog.Info("cron event raw payload", "component", "agent", "payload", string(evt.Payload))
		var cronEvt struct {
			Action  string `json:"action"`
			JobID   string `json:"jobId"`
			RunAtMs int64  `json:"runAtMs"`
		}
		if err := json.Unmarshal(evt.Payload, &cronEvt); err == nil && cronEvt.Action == "started" {
			now := time.Now().UnixMilli()
			h.cronFireExpectedMu.Lock()
			cutoff := now - cronFireWindowMs
			pruned := h.cronFireExpected[:0]
			for _, ts := range h.cronFireExpected {
				if ts >= cutoff {
					pruned = append(pruned, ts)
				}
			}
			h.cronFireExpected = append(pruned, now)
			h.cronFireExpectedMu.Unlock()
			slog.Info("cron started — expecting lifecycle_start", "component", "agent", "job_id", cronEvt.JobID, "run_at_ms", cronEvt.RunAtMs)
		}
	}

	switch evt.Event {
	case "agent":
		return h.handleAgentStreamEvent(evt)
	case "session.tool":
		return h.handleSessionToolEvent(evt)
	case "chat":
		return h.handleChatEvent(evt)
	case "session.message":
		return h.handleSessionMessageEvent(evt)
	default:
	}

	return nil
}

// parseHistoryTimestamp parses RFC3339 strings or unix milliseconds; returns zero time when
// absent or unparseable (callers treat zero as fresh).
func parseHistoryTimestamp(raw json.RawMessage) time.Time {
	if len(raw) == 0 {
		return time.Time{}
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if ts, err := time.Parse(time.RFC3339Nano, s); err == nil {
			return ts
		}
		return time.Time{}
	}
	var ms int64
	if json.Unmarshal(raw, &ms) == nil && ms > 0 {
		return time.UnixMilli(ms)
	}
	return time.Time{}
}

// extractLastUserMessageFromHistory returns the latest user message text, senderLabel and timestamp
// from a chat.history payload, or ("","",zero). Callers use the timestamp to reject stale messages
// (a fetch at lifecycle_start can race persistence of the new message).
func extractLastUserMessageFromHistory(payload json.RawMessage) (text string, senderLabel string, msgTime time.Time) {
	var hist struct {
		Messages []struct {
			Role        string          `json:"role"`
			Timestamp   json.RawMessage `json:"timestamp"`
			Content     json.RawMessage `json:"content"`
			SenderLabel string          `json:"senderLabel"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return "", "", time.Time{}
	}
	for i := len(hist.Messages) - 1; i >= 0; i-- {
		if hist.Messages[i].Role != "user" {
			continue
		}
		senderLabel = hist.Messages[i].SenderLabel
		msgTime = parseHistoryTimestamp(hist.Messages[i].Timestamp)
		var s string
		if json.Unmarshal(hist.Messages[i].Content, &s) == nil {
			return s, senderLabel, msgTime
		}
		var blocks []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if json.Unmarshal(hist.Messages[i].Content, &blocks) == nil {
			var parts []string
			for _, b := range blocks {
				if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
					parts = append(parts, b.Text)
				}
			}
			return strings.Join(parts, " "), senderLabel, msgTime
		}
		return "", senderLabel, msgTime
	}
	return "", "", time.Time{}
}
