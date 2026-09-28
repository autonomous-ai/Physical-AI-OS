package http

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/serializers"
)

func (h *AgentHandler) Recent(c *gin.Context) {
	events := recentFlowFromJSONL(time.Now().Format("2006-01-02"), 500, h.agentGateway.GetConfiguredChannel())
	if events == nil {
		events = []domain.MonitorEvent{}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(events))
}

// readAllJSONLines reads every line from a flow_events_*.jsonl file (full day).
func readAllJSONLines(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var lines []string
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 2*1024*1024) // 2MB max line
	for scanner.Scan() {
		line := scanner.Text()
		if len(line) == 0 || line[0] != '{' {
			continue // skip corrupt/binary lines
		}
		lines = append(lines, line)
	}
	// Don't fail on scanner error — return what we have so far
	if err := scanner.Err(); err != nil {
		slog.Warn("readAllJSONLines: scanner error, returning partial results", "path", path, "lines_read", len(lines), "error", err)
	}
	return lines, nil
}

// recentFlowFromJSONL reads the last n lines from flow JSONL for a given date (YYYY-MM-DD)
// and converts them to MonitorEvents.
func recentFlowFromJSONL(day string, n int, channelName string) []domain.MonitorEvent {
	path := filepath.Join("local", fmt.Sprintf("flow_events_%s.jsonl", day))
	lines, err := readAllJSONLines(path)
	if err != nil {
		return nil
	}

	// Take last n lines
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}

	events := make([]domain.MonitorEvent, 0, len(lines))
	for _, line := range lines {
		var fe flow.Event
		if err := json.Unmarshal([]byte(line), &fe); err != nil {
			continue
		}
		ev := flowEventToMonitor(fe, channelName)
		events = append(events, ev)
	}
	return events
}

// FlowEvents returns flow events from JSONL file by date.
// Query params: date=YYYY-MM-DD (default today), last=<n> (default 500, max 2000).
func (h *AgentHandler) FlowEvents(c *gin.Context) {
	day := c.DefaultQuery("date", time.Now().Format("2006-01-02"))
	last := 500
	if s := c.Query("last"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n > 0 {
			last = n
		}
	}
	if last > 10000 {
		last = 10000
	}
	events := recentFlowFromJSONL(day, last, h.agentGateway.GetConfiguredChannel())
	if events == nil {
		events = []domain.MonitorEvent{}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"date":   day,
		"events": events,
	}))
}

// MoodHistory returns mood-relevant sensing events for music suggestion context.
// Query params:
//
//	user=<name>            (default: current user)
//	date=YYYY-MM-DD        (default today)
//	last=<n>               (default 100, max 500)
func (h *AgentHandler) FlowStream(c *gin.Context) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	lastMtime := int64(0)
	for {
		select {
		case <-c.Request.Context().Done():
			return
		default:
		}

		day := time.Now().Format("2006-01-02")
		path := filepath.Join("local", fmt.Sprintf("flow_events_%s.jsonl", day))
		if st, err := os.Stat(path); err == nil {
			mt := st.ModTime().UnixNano()
			if mt > lastMtime {
				lastMtime = mt
				events := recentFlowFromJSONL(day, 500, h.agentGateway.GetConfiguredChannel())
				if events == nil {
					events = []domain.MonitorEvent{}
				}
				payload := map[string]any{
					"date":   day,
					"events": events,
				}
				data, _ := json.Marshal(payload)
				c.SSEvent("message", string(data))
				c.Writer.Flush()
			}
		}
		time.Sleep(1000 * time.Millisecond)
	}
}

// flowEventToMonitor converts a flow.Event (JSONL) to a domain.MonitorEvent (for UI).
func flowEventToMonitor(fe flow.Event, channelName string) domain.MonitorEvent {
	evType := "flow_" + string(fe.Kind)

	// Promote well-known nodes to their own event type for turn grouping
	switch fe.Node {
	case "sensing_input":
		if fe.Kind == "enter" {
			evType = "sensing_input"
		}
	case "chat_input":
		if fe.Kind == "enter" || fe.Kind == "event" {
			evType = "chat_input"
		}
	case "intent_match":
		if fe.Kind == "event" || fe.Kind == "exit" {
			evType = "intent_match"
		}
	}

	summary := fmt.Sprintf("[%s] %s", fe.Kind, fe.Node)
	if fe.DurationMs > 0 {
		summary += fmt.Sprintf(" (%dms)", fe.DurationMs)
	}

	// Build summary from data for well-known nodes
	if fe.Node == "sensing_input" && fe.Kind == "enter" && fe.Data != nil {
		if msg, ok := fe.Data["message"].(string); ok {
			typ, _ := fe.Data["type"].(string)
			summary = fmt.Sprintf("[%s] %s", typ, msg)
		}
	}
	if fe.Node == "chat_input" && fe.Data != nil {
		source, _ := fe.Data["source"].(string)
		msg, _ := fe.Data["message"].(string)
		if source == "channel" {
			// If the flow event names its channel explicitly, honor that —
			// GetConfiguredChannel is a single-string global fallback, so on a
			// device with multiple channels enabled (e.g. Telegram token still
			// present from an earlier setup + BlueBubbles now active) it can
			// mis-label a BlueBubbles turn as [telegram]. See intern-v2 20.159:
			// Telegram token remained after adding iMessage, so every iMessage
			// turn rendered as TELEGRAM in the Flow panel.
			if perEventChannel, _ := fe.Data["channel"].(string); perEventChannel != "" {
				channelName = perEventChannel
			}
			// Label routing mirrors handler_events.go goroutine:
			//  1. sender filled → "[telegram:Gray]" (real channel user)
			//  2. message is device-internal prefix → "[voice]" / "[emotion]"
			//     / ... (sensing or voice event the device posted via chat.send
			//     that OpenClaw merged into a UUID host turn via steer)
			//  3. otherwise fall back to channelName (or "[…]" when no msg
			//     yet — first emit before chat.history goroutine returns)
			sender, _ := fe.Data["sender"].(string)
			switch {
			case sender != "":
				label := channelName + ":" + sender
				if msg != "" {
					summary = fmt.Sprintf("[%s] %s", label, msg)
				} else {
					summary = "[" + label + "]"
				}
			case msg != "":
				if internal := labelForDeviceInternal(msg); internal != "" {
					summary = internal + " " + msg
				} else {
					summary = fmt.Sprintf("[%s] %s", channelName, msg)
				}
			default:
				summary = "[chat]"
			}
		} else {
			// system/user: caller already encodes its label inside message
			// (e.g. "[system] Bạn vừa thức dậy..."), so don't double-wrap.
			label := source
			if label == "" {
				label = channelName
			}
			switch {
			case msg == "":
				summary = "[" + label + "]"
			case strings.HasPrefix(msg, "["):
				summary = msg
			default:
				summary = fmt.Sprintf("[%s] %s", label, msg)
			}
		}
	}

	t := time.Unix(int64(fe.TS), int64((fe.TS-float64(int64(fe.TS)))*1e9))

	return domain.MonitorEvent{
		ID:      fmt.Sprintf("flow-%d", fe.Seq),
		Time:    t.Format(time.RFC3339Nano),
		Type:    evType,
		Summary: summary,
		RunID:   fe.TraceID,
		Phase:   string(fe.Kind),
		Detail:  map[string]any{"node": fe.Node, "dur_ms": fe.DurationMs, "data": fe.Data},
	}
}

// FlowLogs serves the daily flow JSONL log file for download.
// Query params: ?date=YYYY-MM-DD (default today); ?last=N (optional) — if set, only the last N lines
// are returned (same tail as GET /openclaw/flow-events?last=N). Omit ?last for the full day file.
func (h *AgentHandler) FlowLogs(c *gin.Context) {
	date := c.Query("date")
	if date == "" {
		date = time.Now().Format("2006-01-02")
	}
	path := filepath.Join("local", fmt.Sprintf("flow_events_%s.jsonl", date))

	last := 0
	if s := c.Query("last"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n > 0 {
			last = n
			if last > 2000 {
				last = 2000
			}
		}
	}

	filename := fmt.Sprintf("flow_%s.jsonl", date)
	var out []byte
	if last > 0 {
		lines, err := readAllJSONLines(path)
		if err != nil {
			c.JSON(http.StatusNotFound, serializers.ResponseError("no log for date: "+date))
			return
		}
		if len(lines) > last {
			lines = lines[len(lines)-last:]
		}
		filename = fmt.Sprintf("flow_%s_last%d.jsonl", date, last)
		out = []byte(strings.Join(lines, "\n"))
		if len(out) > 0 {
			out = append(out, '\n')
		}
	} else {
		var err error
		out, err = os.ReadFile(path)
		if err != nil {
			c.JSON(http.StatusNotFound, serializers.ResponseError("no log for date: "+date))
			return
		}
	}

	c.Header("Content-Disposition", "attachment; filename="+filename)
	c.Header("Content-Type", "application/x-ndjson")
	_, _ = c.Writer.Write(out)
}

// ClearFlowLogs truncates the daily flow JSONL log file.
// Query param ?date=YYYY-MM-DD selects a historical file; defaults to today.
func (h *AgentHandler) ClearFlowLogs(c *gin.Context) {
	date := c.Query("date")
	if date == "" {
		date = time.Now().Format("2006-01-02")
	}
	path := fmt.Sprintf("local/flow_events_%s.jsonl", date)
	if _, err := os.Stat(path); os.IsNotExist(err) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"cleared": false,
			"file":    path,
			"note":    "file not found",
		}))
		return
	}
	if err := os.Truncate(path, 0); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("clear flow log failed: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"cleared": true,
		"file":    path,
	}))
}

// Analytics returns aggregated per-day metrics from flow JSONL files.
