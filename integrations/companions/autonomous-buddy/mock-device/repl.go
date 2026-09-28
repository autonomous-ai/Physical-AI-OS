package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

const helpText = `
mock-device REPL — type a command to send it to the paired buddy.

Intent-based (no vision needed):
  ping                              sanity check
  notification <title>              e.g. notification meeting in 5
  open_url <url>                    e.g. open_url https://news.ycombinator.com
  open_app <name>                   e.g. open_app Calculator
  close_app <name>                  e.g. close_app Calculator
  type_text <text>                  needs Accessibility
  key_combo <keys>                  e.g. key_combo cmd space (Accessibility)

Vision / click / mouse:
  screenshot [scale]                e.g. "screenshot 0.5" — saves to ~/Library/Application Support/AutonomousBuddy/screenshots/latest.png
  click_at <x> <y> [button] [n]     e.g. "click_at 540 320" or "click_at 540 320 right"
  click <x> <y>                     alias for click_at left
  double_click <x> <y>              two clicks at coord
  right_click <x> <y>               right-click at coord
  scroll <dy> [dx]                  e.g. "scroll -300" (down)
  mouse_move <x> <y> [smooth]       e.g. "mouse_move 100 100 smooth"
  drag <x1> <y1> <x2> <y2> [ms]     e.g. "drag 100 200 400 200 500"

Clipboard:
  read_clipboard                    returns current clipboard text
  write_clipboard <text>            e.g. write_clipboard hello

Accessibility (find element by label, click it — best for native apps):
  click_button <label> [app=X]      e.g. click_button Submit
                                    e.g. click_button Admit app="Google Chrome"

Coord helpers (no need to eyeball pixel coords):
  where                             one-shot — print current cursor (x,y)
  pick [seconds]                    countdown N seconds then capture cursor pos
  pick_click [seconds]              countdown then click at captured pos
                                    (alias: click_here)

Session:
  code                              re-issue a new pairing code
  status                            show pairing / connection state
  help | ?                          this list
  quit                              exit
`

func RunREPL(ctx context.Context, state *State) {
	fmt.Print(helpText)
	scanner := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("> ")
		if !scanner.Scan() {
			return
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		switch line {
		case "help", "?":
			fmt.Print(helpText)
			continue
		case "quit", "exit":
			os.Exit(0)
		case "code":
			state.IssueCode()
			continue
		case "status":
			printStatus(state)
			continue
		case "where":
			handleWhere(ctx, state)
			continue
		}
		if strings.HasPrefix(line, "pick_click") || strings.HasPrefix(line, "click_here") {
			rest := strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(line, "click_here"), "pick_click"))
			handlePick(ctx, state, rest, true)
			continue
		}
		if strings.HasPrefix(line, "pick") {
			rest := strings.TrimSpace(strings.TrimPrefix(line, "pick"))
			handlePick(ctx, state, rest, false)
			continue
		}

		cmd, ok := parseREPL(line)
		if !ok {
			fmt.Printf("  unknown command — type 'help'\n\n")
			continue
		}
		resp, err := state.Dispatch(ctx, cmd)
		if err != nil {
			fmt.Printf("  ✗ %v\n\n", err)
			continue
		}
		printResponse(resp)
	}
}

func handleWhere(ctx context.Context, state *State) {
	cmd := newCommand("cursor_pos", map[string]any{})
	resp, err := state.Dispatch(ctx, cmd)
	if err != nil {
		fmt.Printf("  ✗ %v\n\n", err)
		return
	}
	x, y, ok := extractXY(resp)
	if !ok {
		printResponse(resp)
		return
	}
	fmt.Printf("  cursor at: (%d, %d)\n  → click_at %d %d\n\n", x, y, x, y)
}

func handlePick(ctx context.Context, state *State, rest string, alsoClick bool) {
	seconds := 3
	if rest != "" {
		if n, err := strconv.Atoi(rest); err == nil && n > 0 && n <= 30 {
			seconds = n
		}
	}
	fmt.Printf("  Move cursor to target. Capturing in %ds...\n", seconds)
	for i := seconds; i > 0; i-- {
		fmt.Printf("  %d...\n", i)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}

	resp, err := state.Dispatch(ctx, newCommand("cursor_pos", map[string]any{}))
	if err != nil {
		fmt.Printf("  ✗ %v\n\n", err)
		return
	}
	x, y, ok := extractXY(resp)
	if !ok {
		printResponse(resp)
		return
	}
	fmt.Printf("  cursor at: (%d, %d)\n", x, y)

	if !alsoClick {
		fmt.Println()
		return
	}
	clickResp, err := state.Dispatch(ctx, newCommand("click_at", map[string]any{
		"x": x, "y": y,
	}))
	if err != nil {
		fmt.Printf("  ✗ click: %v\n\n", err)
		return
	}
	printResponse(clickResp)
}

func extractXY(raw json.RawMessage) (int, int, bool) {
	var parsed struct {
		OK     bool `json:"ok"`
		Result struct {
			X int `json:"x"`
			Y int `json:"y"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil || !parsed.OK {
		return 0, 0, false
	}
	return parsed.Result.X, parsed.Result.Y, true
}

func printStatus(state *State) {
	paired := state.pairedSnapshot()
	ws := state.currentWS()
	if paired == nil {
		fmt.Println("  pairing: none")
	} else {
		fmt.Printf("  pairing: id=%s name=%q os=%q\n", paired.BuddyID, paired.Name, paired.OSVersion)
	}
	if ws == nil {
		fmt.Println("  ws: not connected")
	} else {
		fmt.Println("  ws: connected")
	}
	fmt.Println()
}

func printResponse(raw json.RawMessage) {
	var pretty map[string]any
	if err := json.Unmarshal(raw, &pretty); err != nil {
		fmt.Printf("  raw: %s\n\n", raw)
		return
	}
	okVal, _ := pretty["ok"].(bool)
	duration := pretty["duration_ms"]
	if okVal {
		result := truncateLarge(pretty["result"])
		fmt.Printf("  ✓ %v  (%vms)\n\n", result, duration)
	} else {
		fmt.Printf("  ✗ %v  (%vms)\n\n", pretty["error"], duration)
	}
}

// truncateLarge replaces string values >200 chars (e.g. image_b64) with "<N chars>".
func truncateLarge(v any) any {
	m, ok := v.(map[string]any)
	if !ok {
		return v
	}
	out := make(map[string]any, len(m))
	for k, val := range m {
		if s, ok := val.(string); ok && len(s) > 200 {
			out[k] = fmt.Sprintf("<%d chars>", len(s))
		} else {
			out[k] = val
		}
	}
	return out
}
