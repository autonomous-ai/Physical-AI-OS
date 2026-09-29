package buddy

import (
	"bytes"
	"encoding/json"
	"fmt"
	"time"
)

// Heartbeat is sent by Claude Desktop every 10s or on state change.
type Heartbeat struct {
	Total       int      `json:"total"`
	Running     int      `json:"running"`
	Waiting     int      `json:"waiting"`
	Msg         string   `json:"msg"`
	Entries     []string `json:"entries"`
	Tokens      int      `json:"tokens"`
	TokensToday int      `json:"tokens_today"`
	Prompt      *Prompt  `json:"prompt"`
}

// Prompt is present in Heartbeat only when permission is required.
type Prompt struct {
	ID   string `json:"id"`
	Tool string `json:"tool"`
	Hint string `json:"hint"`
}

// TimeSync is sent by Desktop on connect.
type TimeSync struct {
	Time [2]int64 `json:"time"`
}

// Event is a Claude Desktop event (e.g. evt="turn"); Content is decoded lazily.
type Event struct {
	Evt     string          `json:"evt"`
	Role    string          `json:"role,omitempty"`
	Content json.RawMessage `json:"content,omitempty"`
}

// ContentBlock is one parsed Anthropic content block; unused fields stay zero.
type ContentBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text,omitempty"`
	Thinking  string          `json:"thinking,omitempty"`
	Name      string          `json:"name,omitempty"`  // tool_use.name
	ID        string          `json:"id,omitempty"`    // tool_use.id
	Input     json.RawMessage `json:"input,omitempty"` // tool_use.input
	ToolUseID string          `json:"tool_use_id,omitempty"`
	Content   json.RawMessage `json:"content,omitempty"`   // tool_result.content
	ToolName  string          `json:"tool_name,omitempty"` // tool_reference.tool_name
}

// Blocks decodes content as typed blocks; returns nil for string-form (user) content.
func (e *Event) Blocks() []ContentBlock {
	if len(e.Content) == 0 {
		return nil
	}
	var blocks []ContentBlock
	if err := json.Unmarshal(e.Content, &blocks); err != nil {
		return nil
	}
	return blocks
}

// TurnText renders an Event's content as a single log-friendly string.
func (e *Event) TurnText() string {
	if len(e.Content) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(e.Content, &s); err == nil {
		return s
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(e.Content, &blocks); err != nil {
		return ""
	}
	var out []string
	for _, raw := range blocks {
		if s := formatContentBlock(raw); s != "" {
			out = append(out, s)
		}
	}
	return joinNonEmpty(out, "\n")
}

// formatContentBlock renders one content block as a single log line.
func formatContentBlock(raw json.RawMessage) string {
	var meta struct {
		Type      string          `json:"type"`
		Text      string          `json:"text"`
		Thinking  string          `json:"thinking"`
		Name      string          `json:"name"`
		ID        string          `json:"id"`
		Input     json.RawMessage `json:"input"`
		ToolUseID string          `json:"tool_use_id"`
		Content   json.RawMessage `json:"content"`
		ToolName  string          `json:"tool_name"`
	}
	if err := json.Unmarshal(raw, &meta); err != nil {
		return ""
	}
	switch meta.Type {
	case "text":
		return meta.Text
	case "thinking":
		if meta.Thinking == "" {
			return "[thinking]"
		}
		return fmt.Sprintf("[thinking: %s]", meta.Thinking)
	case "tool_use":
		return fmt.Sprintf("[tool_use %s(%s)]", meta.Name, string(meta.Input))
	case "tool_result":
		return fmt.Sprintf("[tool_result %s: %s]", meta.ToolUseID, summarizeContent(meta.Content))
	case "tool_reference":
		return fmt.Sprintf("[tool_ref: %s]", meta.ToolName)
	case "":
		// Untyped block with nested content: return raw JSON rather than drop it.
		return string(raw)
	default:
		return fmt.Sprintf("[%s]", meta.Type)
	}
}

// summarizeContent flattens tool_result content (string or blocks) into one line.
func summarizeContent(c json.RawMessage) string {
	if len(c) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(c, &s); err == nil {
		return s
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(c, &blocks); err == nil {
		var out []string
		for _, raw := range blocks {
			if s := formatContentBlock(raw); s != "" {
				out = append(out, s)
			}
		}
		return joinNonEmpty(out, " | ")
	}
	return string(c)
}

func joinNonEmpty(parts []string, sep string) string {
	var b bytes.Buffer
	for i, p := range parts {
		if i > 0 {
			b.WriteString(sep)
		}
		b.WriteString(p)
	}
	return b.String()
}

// Command is sent by Desktop for status/name/owner/unpair and folder-push
// (char_begin, file, chunk, file_end, char_end).
type Command struct {
	Cmd  string `json:"cmd"`
	Name string `json:"name,omitempty"`

	// Folder-push fields (Claude Desktop streams a data folder to the device)
	Total int    `json:"total,omitempty"` // char_begin: total bytes across all files
	Path  string `json:"path,omitempty"`  // file: relative file path
	Size  int    `json:"size,omitempty"`  // file: file size in bytes
	D     string `json:"d,omitempty"`     // chunk: base64-encoded data
}

// PermissionDecision is sent from device to Desktop.
type PermissionDecision struct {
	Cmd      string `json:"cmd"`
	ID       string `json:"id"`
	Decision string `json:"decision"`
}

// Ack is sent from device to Desktop for any received command.
type Ack struct {
	AckCmd string      `json:"ack"`
	OK     bool        `json:"ok"`
	N      int         `json:"n"`
	Data   interface{} `json:"data,omitempty"`
	Error  string      `json:"error,omitempty"`
}

// StatusData is the payload for status ack.
type StatusData struct {
	Name  string    `json:"name"`
	Sec   bool      `json:"sec"`
	Bat   BatInfo   `json:"bat"`
	Sys   SysInfo   `json:"sys"`
	Stats StatsInfo `json:"stats"`
}

type BatInfo struct {
	Pct int  `json:"pct"`
	MV  int  `json:"mV"`
	MA  int  `json:"mA"`
	USB bool `json:"usb"`
}

type SysInfo struct {
	Up   int `json:"up"`
	Heap int `json:"heap"`
}

type StatsInfo struct {
	Appr int     `json:"appr"`
	Deny int     `json:"deny"`
	Vel  float64 `json:"vel"`
	Nap  int     `json:"nap"`
	Lvl  int     `json:"lvl"`
}

// ParseOrSalvage parses data, retrying from the last known JSON opener when a dropped packet left a garbage prefix.
// Returns the message, the number of discarded bytes, and any terminal error.
func ParseOrSalvage(data []byte) (interface{}, int, error) {
	msg, err := ParseMessage(data)
	if err == nil {
		return msg, 0, nil
	}
	openers := [][]byte{[]byte(`{"cmd":"`), []byte(`{"time":`), []byte(`{"total":`), []byte(`{"evt":"`)}
	best := -1
	var bestMsg interface{}
	for _, opener := range openers {
		idx := bytes.LastIndex(data, opener)
		if idx <= 0 || idx <= best {
			continue
		}
		salvage, salErr := ParseMessage(data[idx:])
		if salErr != nil {
			continue
		}
		best = idx
		bestMsg = salvage
	}
	if best < 0 {
		return nil, 0, err
	}
	return bestMsg, best, nil
}

// ParseMessage parses a JSON line from Desktop into *Heartbeat, *TimeSync, *Command or *Event.
func ParseMessage(data []byte) (interface{}, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse json: %w", err)
	}

	if _, ok := raw["cmd"]; ok {
		var cmd Command
		if err := json.Unmarshal(data, &cmd); err != nil {
			return nil, fmt.Errorf("parse command: %w", err)
		}
		return &cmd, nil
	}

	if _, ok := raw["time"]; ok {
		var ts TimeSync
		if err := json.Unmarshal(data, &ts); err != nil {
			return nil, fmt.Errorf("parse timesync: %w", err)
		}
		return &ts, nil
	}

	if _, ok := raw["evt"]; ok {
		var e Event
		if err := json.Unmarshal(data, &e); err != nil {
			return nil, fmt.Errorf("parse event: %w", err)
		}
		return &e, nil
	}

	if _, ok := raw["total"]; ok {
		var hb Heartbeat
		if err := json.Unmarshal(data, &hb); err != nil {
			return nil, fmt.Errorf("parse heartbeat: %w", err)
		}
		return &hb, nil
	}

	return nil, fmt.Errorf("unknown message type")
}

// MakePermission creates a permission decision JSON line.
func MakePermission(id, decision string) []byte {
	msg := PermissionDecision{Cmd: "permission", ID: id, Decision: decision}
	data, _ := json.Marshal(msg)
	return append(data, '\n')
}

// MakeAck creates an ack JSON line.
func MakeAck(cmd string, ok bool) []byte {
	ack := Ack{AckCmd: cmd, OK: ok, N: 0}
	data, _ := json.Marshal(ack)
	return append(data, '\n')
}

// MakeAckN creates an ack JSON line with a byte count (for chunk/file_end).
func MakeAckN(cmd string, ok bool, n int) []byte {
	ack := Ack{AckCmd: cmd, OK: ok, N: n}
	data, _ := json.Marshal(ack)
	return append(data, '\n')
}

// MakeStatusAck creates a status ack with device info.
func MakeStatusAck(name string, uptime time.Duration, approvedCount, deniedCount int) []byte {
	ack := Ack{
		AckCmd: "status",
		OK:     true,
		Data: StatusData{
			Name: name,
			Sec:  false,                                         // TODO: set true when BLE link is encrypted
			Bat:  BatInfo{Pct: 100, MV: 5000, MA: 0, USB: true}, // Pi is always on USB
			Sys:  SysInfo{Up: int(uptime.Seconds()), Heap: 0},
			Stats: StatsInfo{
				Appr: approvedCount,
				Deny: deniedCount,
			},
		},
	}
	data, _ := json.Marshal(ack)
	return append(data, '\n')
}
