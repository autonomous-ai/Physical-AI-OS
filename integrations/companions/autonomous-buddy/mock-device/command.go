package main

import (
	"crypto/rand"
	"encoding/hex"
	"strconv"
	"strings"
	"time"
)

// Command matches the JSON shape the buddy expects on its WebSocket.
type Command struct {
	ID        string         `json:"id"`
	Action    string         `json:"action"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms,omitempty"`
	IssuedAt  string         `json:"issued_at,omitempty"`
	IssuedBy  string         `json:"issued_by,omitempty"`
}

func newCommand(action string, params map[string]any) Command {
	idBytes := make([]byte, 8)
	_, _ = rand.Read(idBytes)
	return Command{
		ID:        hex.EncodeToString(idBytes),
		Action:    action,
		Params:    params,
		TimeoutMs: 5000,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  "mock-device:repl",
	}
}

// parseREPL turns a one-line REPL input into a Command, or (zero, false) if unrecognized.
func parseREPL(line string) (Command, bool) {
	line = strings.TrimSpace(line)
	if line == "" {
		return Command{}, false
	}
	parts := strings.SplitN(line, " ", 2)
	action := parts[0]
	rest := ""
	if len(parts) == 2 {
		rest = strings.TrimSpace(parts[1])
	}

	switch action {
	case "ping":
		return newCommand("ping", map[string]any{}), true
	case "open_app":
		return newCommand("open_app", map[string]any{"app": fallback(rest, "Calculator")}), true
	case "close_app":
		return newCommand("close_app", map[string]any{"app": fallback(rest, "Calculator")}), true
	case "open_url":
		return newCommand("open_url", map[string]any{"url": fallback(rest, "https://example.com")}), true
	case "type_text":
		return newCommand("type_text", map[string]any{"text": fallback(rest, "hello from device")}), true
	case "key_combo":
		if rest == "" {
			return Command{}, false
		}
		return newCommand("key_combo", map[string]any{"keys": strings.Fields(rest)}), true
	case "notification":
		return newCommand("notification", map[string]any{
			"title": fallback(rest, "Device"),
			"body":  "Test from mock-device",
		}), true

	case "screenshot":
		// usage: screenshot [scale]   e.g. "screenshot 0.5"
		params := map[string]any{}
		if rest != "" {
			if s, err := strconv.ParseFloat(rest, 64); err == nil {
				params["scale"] = s
			}
		}
		return newCommand("screenshot", params), true

	case "click_at", "click":
		// usage: click_at <x> <y> [button] [clicks]
		toks := strings.Fields(rest)
		if len(toks) < 2 {
			return Command{}, false
		}
		x, errX := strconv.Atoi(toks[0])
		y, errY := strconv.Atoi(toks[1])
		if errX != nil || errY != nil {
			return Command{}, false
		}
		params := map[string]any{"x": x, "y": y}
		if len(toks) >= 3 {
			params["button"] = toks[2]
		}
		if len(toks) >= 4 {
			if c, err := strconv.Atoi(toks[3]); err == nil {
				params["clicks"] = c
			}
		}
		return newCommand("click_at", params), true

	case "double_click":
		toks := strings.Fields(rest)
		if len(toks) < 2 {
			return Command{}, false
		}
		x, errX := strconv.Atoi(toks[0])
		y, errY := strconv.Atoi(toks[1])
		if errX != nil || errY != nil {
			return Command{}, false
		}
		return newCommand("click_at", map[string]any{"x": x, "y": y, "clicks": 2}), true

	case "right_click":
		toks := strings.Fields(rest)
		if len(toks) < 2 {
			return Command{}, false
		}
		x, errX := strconv.Atoi(toks[0])
		y, errY := strconv.Atoi(toks[1])
		if errX != nil || errY != nil {
			return Command{}, false
		}
		return newCommand("click_at", map[string]any{"x": x, "y": y, "button": "right"}), true

	case "scroll":
		// usage: scroll <delta_y> [delta_x]    e.g. "scroll -300" (down 300px)
		toks := strings.Fields(rest)
		if len(toks) == 0 {
			return Command{}, false
		}
		dy, err := strconv.Atoi(toks[0])
		if err != nil {
			return Command{}, false
		}
		params := map[string]any{"delta_y": dy}
		if len(toks) >= 2 {
			if dx, err := strconv.Atoi(toks[1]); err == nil {
				params["delta_x"] = dx
			}
		}
		return newCommand("scroll", params), true

	case "mouse_move", "move":
		// usage: mouse_move <x> <y> [smooth]
		toks := strings.Fields(rest)
		if len(toks) < 2 {
			return Command{}, false
		}
		x, errX := strconv.Atoi(toks[0])
		y, errY := strconv.Atoi(toks[1])
		if errX != nil || errY != nil {
			return Command{}, false
		}
		params := map[string]any{"x": x, "y": y}
		if len(toks) >= 3 && (toks[2] == "smooth" || toks[2] == "true") {
			params["smooth"] = true
		}
		return newCommand("mouse_move", params), true

	case "drag":
		// usage: drag <x1> <y1> <x2> <y2> [duration_ms]
		toks := strings.Fields(rest)
		if len(toks) < 4 {
			return Command{}, false
		}
		x1, _ := strconv.Atoi(toks[0])
		y1, _ := strconv.Atoi(toks[1])
		x2, _ := strconv.Atoi(toks[2])
		y2, _ := strconv.Atoi(toks[3])
		params := map[string]any{
			"from": map[string]any{"x": x1, "y": y1},
			"to":   map[string]any{"x": x2, "y": y2},
		}
		if len(toks) >= 5 {
			if d, err := strconv.Atoi(toks[4]); err == nil {
				params["duration_ms"] = d
			}
		}
		return newCommand("drag", params), true

	case "read_clipboard", "paste_read":
		return newCommand("read_clipboard", map[string]any{}), true

	case "write_clipboard", "copy_write":
		return newCommand("write_clipboard", map[string]any{"text": fallback(rest, "hello clipboard")}), true

	case "click_button":
		// usage: click_button <label> [app=X]
		if rest == "" {
			return Command{}, false
		}
		var label, appName string
		if idx := strings.Index(rest, " app="); idx >= 0 {
			label = strings.TrimSpace(rest[:idx])
			appName = strings.TrimSpace(rest[idx+5:])
		} else {
			label = rest
		}
		params := map[string]any{"label": label}
		if appName != "" {
			params["app"] = appName
		}
		return newCommand("click_button", params), true
	}
	return Command{}, false
}

func fallback(s, def string) string {
	if s == "" {
		return def
	}
	return s
}
