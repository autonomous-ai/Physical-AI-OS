package gatewayd

// The App Server protocol is JSON-RPC over one JSON object per stdio line.
// Keep this deliberately small: gatewayd exposes the legacy Codex exec event
// shape to the rest of OS, while this file owns only the protocol boundary.

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os/exec"
	"strings"
	"sync"
	"time"
)

type appServer struct {
	s         *Server
	cmd       *exec.Cmd
	in        io.WriteCloser
	mu        sync.Mutex
	nextID    int
	callbacks map[int]func(json.RawMessage, json.RawMessage)
}

func startAppServer(ctx context.Context, s *Server) (*appServer, error) {
	cmd := exec.CommandContext(ctx, s.cfg.CodexBin, "app-server", "--listen", "stdio://")
	cmd.Dir = s.cfg.Workspace
	cmd.Env = s.turnEnv()
	in, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("app server stdin: %w", err)
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("app server stdout: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("app server stderr: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start codex app-server: %w", err)
	}
	a := &appServer{s: s, cmd: cmd, in: in, callbacks: make(map[int]func(json.RawMessage, json.RawMessage))}
	go a.read(out)
	go appServerStderr(stderr)
	go func() {
		if err := cmd.Wait(); err != nil && ctx.Err() == nil {
			s.sendError("codex app-server exited: " + err.Error())
		}
	}()
	ready := make(chan error, 1)
	a.request("initialize", map[string]any{"clientInfo": map[string]string{"name": "autonomous-gatewayd", "version": "1"}, "capabilities": map[string]any{"experimentalApi": true}}, func(result, rpcErr json.RawMessage) {
		if len(rpcErr) > 0 {
			ready <- fmt.Errorf("initialize app-server: %s", rpcErr)
			return
		}
		// JSON-RPC requires initialized after the initialize response.
		_ = a.notify("initialized", map[string]any{})
		ready <- nil
	})
	select {
	case err := <-ready:
		if err != nil {
			a.close()
			return nil, err
		}
		return a, nil
	case <-ctx.Done():
		a.close()
		return nil, ctx.Err()
	}
}

func (a *appServer) close() {
	_ = a.in.Close()
	if a.cmd.Process != nil {
		_ = a.cmd.Process.Kill()
	}
}

func (a *appServer) notify(method string, params any) error {
	return a.write(map[string]any{"method": method, "params": params})
}

func (a *appServer) request(method string, params any, cb func(json.RawMessage, json.RawMessage)) {
	a.mu.Lock()
	a.nextID++
	id := a.nextID
	a.callbacks[id] = cb
	a.mu.Unlock()
	if err := a.write(map[string]any{"id": id, "method": method, "params": params}); err != nil {
		a.mu.Lock()
		delete(a.callbacks, id)
		a.mu.Unlock()
		cb(nil, json.RawMessage(fmt.Sprintf(`{"message":%q}`, err.Error())))
	}
}

func (a *appServer) write(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	_, err = a.in.Write(append(b, '\n'))
	return err
}

func (a *appServer) read(r io.Reader) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, scanBufSize), streamLimit)
	for sc.Scan() {
		a.handle([]byte(sc.Text()))
	}
	if err := sc.Err(); err != nil {
		log.Printf("%s app-server stdout: %v", logPrefix, err)
	}
}

func appServerStderr(r io.Reader) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, scanBufSize), streamLimit)
	for sc.Scan() {
		log.Printf("%s app-server: %s", logPrefix, sc.Text())
	}
}

func (a *appServer) handle(raw []byte) {
	var msg struct {
		ID     int             `json:"id"`
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	if json.Unmarshal(raw, &msg) != nil {
		return
	}
	if msg.ID != 0 && msg.Method == "" {
		a.mu.Lock()
		cb := a.callbacks[msg.ID]
		delete(a.callbacks, msg.ID)
		a.mu.Unlock()
		if cb != nil {
			cb(msg.Result, msg.Error)
		}
		return
	}
	a.s.handleAppNotification(msg.Method, msg.Params)
}

func (s *Server) startAppTurn(payload turnPayload) {
	s.mu.Lock()
	app, threadID := s.app, s.threadID
	s.mu.Unlock()
	if app == nil {
		s.sendError("codex app-server unavailable")
		return
	}
	payload = s.prepareSkill(s.lifetimeContext, payload)
	input := appInput(payload)
	var startFresh func()
	start := func(thread string, retryFreshOnMissingThread bool) {
		app.request("turn/start", map[string]any{"threadId": thread, "input": input, "cwd": s.cfg.Workspace}, func(result, rpcErr json.RawMessage) {
			if len(rpcErr) > 0 {
				if retryFreshOnMissingThread && missingAppThread(rpcErr) {
					// App Server owns threads in memory. A gateway restart can
					// therefore reload a persisted thread id that the new child no
					// longer knows. Drop only that invalid session and retry this
					// same device turn once on a fresh thread.
					log.Printf("%s persisted App Server thread is gone — retrying fresh", logPrefix)
					s.clearSession()
					startFresh()
					return
				}
				s.endAppTurnError(rpcErr)
				return
			}
			var r struct {
				Turn struct {
					ID string `json:"id"`
				} `json:"turn"`
			}
			_ = json.Unmarshal(result, &r)
			s.mu.Lock()
			s.activeTurnID, s.activeStarting = r.Turn.ID, false
			s.mu.Unlock()
			s.armAppTurnTimeout(r.Turn.ID)
		})
	}
	startFresh = func() {
		app.request("thread/start", map[string]any{"cwd": s.cfg.Workspace, "approvalPolicy": "never", "sandbox": "danger-full-access"}, func(result, rpcErr json.RawMessage) {
			if len(rpcErr) > 0 {
				s.endAppTurnError(rpcErr)
				return
			}
			var r struct {
				Thread struct {
					ID string `json:"id"`
				} `json:"thread"`
			}
			_ = json.Unmarshal(result, &r)
			if r.Thread.ID == "" {
				s.endAppTurnError(json.RawMessage(`{"message":"app-server returned no thread id"}`))
				return
			}
			s.storeThreadID(r.Thread.ID)
			s.sendJSON(map[string]any{"type": "thread.started", "thread_id": r.Thread.ID})
			start(r.Thread.ID, false)
		})
	}
	if threadID != "" {
		start(threadID, true)
		return
	}
	startFresh()
}

func missingAppThread(raw json.RawMessage) bool {
	var e struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(raw, &e)
	message := strings.ToLower(e.Message + " " + string(raw))
	return strings.Contains(message, "thread not found") ||
		strings.Contains(message, "no rollout found") ||
		strings.Contains(message, "conversation not found")
}

func appInput(p turnPayload) []map[string]any {
	in := []map[string]any{}
	if p.preload != "" {
		in = append(in, map[string]any{"type": "text", "text": p.preload})
	}
	in = append(in, map[string]any{"type": "text", "text": p.Content})
	for _, a := range p.Attachments {
		if strings.HasPrefix(a.URL, "data:image/") {
			in = append(in, map[string]any{"type": "image", "url": a.URL})
		}
	}
	return in
}

func (s *Server) steerAppTurn(p turnPayload) {
	s.mu.Lock()
	app, thread, turn := s.app, s.threadID, s.activeTurnID
	s.mu.Unlock()
	if app == nil || thread == "" || turn == "" {
		s.enqueue(op{kind: opTurn, payload: p})
		return
	}
	p = s.prepareSkill(s.lifetimeContext, p)
	// Selection can take up to three seconds; the previous turn may finish
	// meanwhile. Recheck ownership before steering so a new request is not lost.
	s.mu.Lock()
	app, thread, turn = s.app, s.threadID, s.activeTurnID
	s.mu.Unlock()
	if app == nil || thread == "" || turn == "" {
		s.enqueue(op{kind: opTurn, payload: p})
		return
	}
	app.request("turn/steer", map[string]any{"threadId": thread, "expectedTurnId": turn, "input": appSteerInput(p)}, func(_ json.RawMessage, rpcErr json.RawMessage) {
		if len(rpcErr) > 0 {
			log.Printf("%s steer rejected: %s", logPrefix, rpcErr)
			// A steered input has no independent terminal turn event. Tell the
			// client that this specific request was rejected so it can retire its
			// pending correlation without ending the currently active turn.
			s.sendJSON(map[string]any{"type": "bridge.rejected", "error": string(rpcErr), "request_id": p.RequestID, "run_id": p.RunID})
			return
		}
		// The active App Server turn retains the first request/run correlation.
		// Acknowledge this follow-up explicitly: otherwise the OS client keeps a
		// second pending run forever, because it will never receive its own
		// turn.completed frame.
		s.sendJSON(map[string]any{"type": "bridge.steered", "request_id": p.RequestID, "run_id": p.RunID})
	})
}

// User requests can arrive while the model is handling a passive sensor event.
// Keep their response address intact and explicitly distinguish them from the
// silent realtime history synchronization that also uses turn/steer.
func appSteerInput(p turnPayload) []map[string]any {
	if strings.Contains(p.Content, "[harness-reply run_id=") {
		p.Content = "[system-routing: New direct user request during the active turn. Prioritize this request over unfinished passive sensing or wellbeing work. Use this request's harness-reply address if delegating. When the user requests work from a computer agent, use harness-use; do not search the device filesystem for that computer's project. A steer acknowledgement is not task completion: handle the request before ending the turn.]\n" + p.Content
	}
	return appInput(p)
}

func (s *Server) endAppTurnError(raw json.RawMessage) {
	var e struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(raw, &e)
	if e.Message == "" {
		e.Message = string(raw)
	}
	s.mu.Lock()
	if s.appTurnTimer != nil {
		s.appTurnTimer.Stop()
		s.appTurnTimer = nil
	}
	s.activeTurnID, s.activeStarting = "", false
	s.appTurnTimedOut = false
	s.mu.Unlock()
	s.sendError(e.Message)
	s.mu.Lock()
	s.activeRequestID, s.activeRunID = "", ""
	s.mu.Unlock()
}

func (s *Server) handleAppNotification(method string, params json.RawMessage) {
	var p struct {
		ThreadID string `json:"threadId"`
		TurnID   string `json:"turnId"`
		Turn     struct {
			ID     string          `json:"id"`
			Status string          `json:"status"`
			Error  any             `json:"error"`
			Usage  json.RawMessage `json:"usage"`
		} `json:"turn"`
		// Some codex builds put usage on the turn-completed notification (top
		// level or under `turn`); 0.150.1 sends it on thread/tokenUsage/updated
		// instead. Accept every shape — usageOf picks the first populated one —
		// so the device turn card keeps showing tokens across codex upgrades.
		Usage      json.RawMessage `json:"usage"`
		TokenUsage struct {
			Last  json.RawMessage `json:"last"`
			Total json.RawMessage `json:"total"`
		} `json:"tokenUsage"`
		Item json.RawMessage `json:"item"`
	}
	_ = json.Unmarshal(params, &p)
	switch method {
	case "thread/tokenUsage/updated":
		// Token usage does NOT ride turn/completed — codex-rs 0.150.1 pushes it
		// on its own notification, in camelCase, ahead of the turn's terminal
		// event. `last` is this turn; `total` is the whole thread. Stash the
		// per-turn block and attach it to turn.completed below, which is the
		// frame the translator reads usage from.
		s.storeAppUsage(p.TokenUsage.Last, p.TokenUsage.Total)
	case "turn/started":
		s.mu.Lock()
		if p.Turn.ID != "" {
			s.activeTurnID = p.Turn.ID
		}
		s.activeStarting = false
		s.mu.Unlock()
		s.armAppTurnTimeout(p.Turn.ID)
		s.sendJSON(map[string]any{"type": "turn.started"})
	case "item/started", "item/updated", "item/completed":
		item := legacyItem(p.Item)
		if method == "item/completed" && item["item_type"] == "agent_message" {
			if message, _ := item["text"].(string); degenerateAssistantOutput(message) {
				s.rejectAppOutput(p.ThreadID, p.TurnID)
				return
			}
		}
		s.mu.Lock()
		rejected := s.appOutputRejected
		s.mu.Unlock()
		if rejected {
			return
		}
		s.sendJSON(map[string]any{"type": strings.Replace(method, "/", ".", 1), "item": item})
	case "turn/completed":
		s.mu.Lock()
		if s.appTurnTimer != nil {
			s.appTurnTimer.Stop()
			s.appTurnTimer = nil
		}
		timedOut := s.appTurnTimedOut
		s.appTurnTimedOut = false
		rejected := s.appOutputRejected
		s.appOutputRejected = false
		s.activeTurnID, s.activeStarting = "", false
		reset := s.resetPending
		s.resetPending = false
		s.mu.Unlock()
		if rejected {
			// rejectAppOutput already emitted the correlated terminal error.
		} else if timedOut {
			s.sendError("timeout")
		} else if p.Turn.Status != "completed" {
			s.sendJSON(map[string]any{"type": "turn.failed", "error": p.Turn.Error})
		} else {
			// Forward the usage block verbatim: without it the translator's
			// lifecycle.end carries no usage and the Flow Monitor turn card
			// shows no in/out/cache tokens at all (the `codex exec` JSONL path
			// never lost it — it forwards stdout untouched).
			frame := map[string]any{"type": "turn.completed"}
			if u := usageOf(p.Usage, p.Turn.Usage, s.takeAppUsage()); u != nil {
				frame["usage"] = u
			}
			s.sendJSON(frame)
		}
		s.mu.Lock()
		s.activeRequestID, s.activeRunID = "", ""
		s.mu.Unlock()
		if reset {
			s.clearSession()
			s.sendStatus("session_cleared", "")
		}
	}
}

func (s *Server) rejectAppOutput(threadID, turnID string) {
	s.mu.Lock()
	if s.appOutputRejected || s.activeTurnID == "" || (turnID != "" && s.activeTurnID != turnID) {
		s.mu.Unlock()
		return
	}
	app := s.app
	if threadID == "" {
		threadID = s.threadID
	}
	if turnID == "" {
		turnID = s.activeTurnID
	}
	s.appOutputRejected = true
	s.mu.Unlock()
	s.clearSession()
	s.sendError(degenerateOutputError)
	if app != nil {
		app.request("turn/interrupt", map[string]any{"threadId": threadID, "turnId": turnID}, func(_ json.RawMessage, rpcErr json.RawMessage) {
			if len(rpcErr) > 0 {
				log.Printf("%s reject output interrupt: %s", logPrefix, rpcErr)
			}
		})
	}
}

func (s *Server) armAppTurnTimeout(turnID string) {
	if turnID == "" || s.cfg.TurnTimeout <= 0 {
		return
	}
	s.mu.Lock()
	if s.activeTurnID != turnID {
		s.mu.Unlock()
		return
	}
	if s.appTurnTimer != nil {
		s.appTurnTimer.Stop()
	}
	s.appTurnTimer = time.AfterFunc(s.cfg.TurnTimeout, func() {
		s.interruptTimedOutAppTurn(turnID)
	})
	s.mu.Unlock()
}

func (s *Server) interruptTimedOutAppTurn(turnID string) {
	s.mu.Lock()
	if s.activeTurnID != turnID || s.app == nil {
		s.mu.Unlock()
		return
	}
	app, threadID := s.app, s.threadID
	s.appTurnTimedOut = true
	s.mu.Unlock()
	app.request("turn/interrupt", map[string]any{"threadId": threadID, "turnId": turnID}, func(_ json.RawMessage, rpcErr json.RawMessage) {
		if len(rpcErr) > 0 {
			s.endAppTurnError(rpcErr)
		}
	})
}

func legacyItem(raw json.RawMessage) map[string]any {
	var item map[string]any
	_ = json.Unmarshal(raw, &item)
	if typ, _ := item["type"].(string); typ != "" {
		item["item_type"] = snakeItemType(typ)
	}
	// The App Server uses camelCase whereas the existing OS translator was
	// built against codex exec's snake_case JSONL. Normalize only fields it
	// consumes; leave unknown item data intact for forward compatibility.
	for appKey, execKey := range map[string]string{
		"aggregatedOutput": "aggregated_output",
		"exitCode":         "exit_code",
	} {
		if v, ok := item[appKey]; ok {
			item[execKey] = v
		}
	}
	return item
}

func snakeItemType(v string) string {
	var b strings.Builder
	for i, r := range v {
		if i > 0 && r >= 'A' && r <= 'Z' {
			b.WriteByte('_')
		}
		b.WriteRune(r)
	}
	return strings.ToLower(b.String())
}

// usageOf returns the first non-empty usage block, normalized to the snake_case
// field names the exec JSONL path uses (the App Server speaks camelCase), so
// the translator has one shape to decode. Empty/`null` candidates are skipped;
// nil means no usage was reported.
func usageOf(candidates ...json.RawMessage) map[string]any {
	for _, raw := range candidates {
		if len(raw) == 0 {
			continue
		}
		var m map[string]any
		if json.Unmarshal(raw, &m) != nil || len(m) == 0 {
			continue
		}
		for camel, snake := range map[string]string{
			"inputTokens":           "input_tokens",
			"cachedInputTokens":     "cached_input_tokens",
			"cacheWriteInputTokens": "cache_write_input_tokens",
			"outputTokens":          "output_tokens",
			"totalTokens":           "total_tokens",
		} {
			if v, ok := m[camel]; ok {
				if _, taken := m[snake]; !taken {
					m[snake] = v
				}
				delete(m, camel)
			}
		}
		return m
	}
	return nil
}

// storeAppUsage keeps the newest per-turn usage block (falling back to the
// thread total when a build omits `last`) until the turn's terminal event
// collects it.
func (s *Server) storeAppUsage(last, total json.RawMessage) {
	raw := last
	if len(raw) == 0 {
		raw = total
	}
	if len(raw) == 0 {
		return
	}
	s.mu.Lock()
	s.appUsage = append(raw[:0:0], raw...)
	s.mu.Unlock()
}

// takeAppUsage returns and clears the stashed usage, so a turn that reported
// none never inherits the previous turn's numbers.
func (s *Server) takeAppUsage() json.RawMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	raw := s.appUsage
	s.appUsage = nil
	return raw
}
