package gatewayd

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

// runResult carries per-run diagnostics out of the pump goroutines (written
// before wg.Wait(), read after — the WaitGroup orders the accesses).
type runResult struct {
	stderrTail string // bounded tail of stderr for crash logging
}

// childLoop is the respawn loop: spawn claude, enable resume for the next
// spawn, flush queued stdin lines, pump stdout/stderr until EOF, then report
// the exit (bridge.error when a turn was in flight) and respawn after the
// backoff. Mirrors bridge.py run_forever. On ctx cancellation the running
// child is killed via exec.CommandContext.
func (s *Server) childLoop(ctx context.Context) {
	for ctx.Err() == nil {
		env := s.loadChildEnv()
		argv := s.buildArgv(env)
		log.Printf("%s spawning: %s", logPrefix, strings.Join(argv, " "))

		cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
		cmd.Dir = s.cfg.Workspace
		cmd.Env = envSlice(env)
		// Own process group so a shutdown kill reaps claude AND its children.
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		cmd.Cancel = func() error {
			if p := cmd.Process; p != nil {
				if err := syscall.Kill(-p.Pid, syscall.SIGKILL); err == nil {
					return nil
				}
				return p.Kill()
			}
			return nil
		}

		stdin, err := cmd.StdinPipe()
		if err != nil {
			log.Printf("%s stdin pipe failed: %v", logPrefix, err)
			s.sleepBackoff(ctx)
			continue
		}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			log.Printf("%s stdout pipe failed: %v", logPrefix, err)
			s.sleepBackoff(ctx)
			continue
		}
		stderr, err := cmd.StderrPipe()
		if err != nil {
			log.Printf("%s stderr pipe failed: %v", logPrefix, err)
			s.sleepBackoff(ctx)
			continue
		}
		if err := cmd.Start(); err != nil {
			// Covers the claude-binary-not-found case: log, back off, retry.
			log.Printf("%s spawn claude failed: %v — retry in %s", logPrefix, err, s.cfg.RestartBackoff)
			s.sleepBackoff(ctx)
			continue
		}

		// Publish the child and flush the pending queue under stdinMu so no
		// message.send line can jump ahead of the queued ones.
		s.stdinMu.Lock()
		s.mu.Lock()
		s.child = cmd
		s.stdin = stdin
		// From now on respawns resume the persisted session (a session.new
		// before the NEXT spawn clears this again).
		s.resumeNext = true
		pending := s.pending
		s.pending = nil
		s.mu.Unlock()
		for _, line := range pending {
			s.writePipe(stdin, line)
		}
		s.stdinMu.Unlock()
		s.sendStatus(nil)

		res := &runResult{}
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); s.pumpStdout(stdout) }()
		go func() { defer wg.Done(); pumpStderr(stderr, res) }()
		wg.Wait()
		_ = cmd.Wait()
		rc := cmd.ProcessState.ExitCode()

		s.mu.Lock()
		s.child = nil
		s.stdin = nil
		hadInflight := s.inflight
		s.inflight = 0
		s.mu.Unlock()

		log.Printf("%s claude exited rc=%d", logPrefix, rc)
		if rc != 0 && strings.TrimSpace(res.stderrTail) != "" {
			log.Printf("%s stderr tail: %s", logPrefix, strings.TrimSpace(res.stderrTail))
		}
		// Self-heal a dead --resume: claude exits rc=1 with "No conversation
		// found with session ID" when the persisted session was dropped from
		// its store (storage wiped, workspace changed, session expired). Without
		// clearing it, every respawn re-resumes the same dead id and the runtime
		// bricks in a crash loop. Drop the stale session so the next spawn is
		// fresh (--resume gated off), exactly like a session.new.
		if rc != 0 && strings.Contains(res.stderrTail, staleSessionMarker) {
			log.Printf("%s stale session — dropping persisted session and respawning fresh", logPrefix)
			s.clearStaleSession()
		}
		if hadInflight > 0 {
			s.sendBridgeError("claude exited (rc=%d) mid-turn", rc)
		}
		s.sendStatus(map[string]any{"exit_code": rc})
		s.sleepBackoff(ctx)
	}
}

// sleepBackoff waits the restart backoff, returning early on ctx cancellation.
func (s *Server) sleepBackoff(ctx context.Context) {
	select {
	case <-ctx.Done():
	case <-time.After(s.cfg.RestartBackoff):
	}
}

// buildArgv builds the persistent claude command line — EXACTLY the bridge.py
// argv: --print --verbose, stream-json on both ends, and
// --dangerously-skip-permissions (headless device, no TTY to approve tools).
// --resume rides only when a session id is persisted and resume is enabled;
// --channels tokens come from CLAUDECODE_CHANNELS in the merged child env
// (whitespace-split, presync-owned).
func (s *Server) buildArgv(env map[string]string) []string {
	argv := []string{s.cfg.ClaudeBin, "--print", "--verbose",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
		"--dangerously-skip-permissions"}
	s.mu.Lock()
	if s.resumeNext && s.sessionID != "" {
		argv = append(argv, "--resume", s.sessionID)
	}
	s.mu.Unlock()
	if channels := strings.Fields(env["CLAUDECODE_CHANNELS"]); len(channels) > 0 {
		argv = append(argv, "--channels")
		argv = append(argv, channels...)
	}
	return argv
}

// loadChildEnv is the process env with HOME + IS_SANDBOX asserted and the
// KEY=VALUE pairs from the env file merged on top (blank/#/no-"=" lines
// skipped, keys/values space-trimmed, surrounding double quotes stripped from
// values).
func (s *Server) loadChildEnv() map[string]string {
	env := map[string]string{}
	for _, kv := range os.Environ() {
		if i := strings.Index(kv, "="); i >= 0 {
			env[kv[:i]] = kv[i+1:]
		}
	}
	env["HOME"] = s.cfg.Home
	// The device runs the bridge as root; claude refuses
	// --dangerously-skip-permissions under uid 0 unless IS_SANDBOX=1 (the
	// containerized-root escape hatch). The device is a dedicated appliance,
	// which is exactly that case.
	env["IS_SANDBOX"] = "1"
	data, err := os.ReadFile(s.cfg.EnvFile)
	if err != nil {
		return env // missing env file is fine (bridge.py behavior)
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		k, v, _ := strings.Cut(line, "=")
		env[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"`)
	}
	return env
}

// envSlice flattens an env map into the sorted KEY=VALUE form exec.Cmd wants.
func envSlice(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k, v := range m {
		out = append(out, k+"="+v)
	}
	sort.Strings(out)
	return out
}

// sendUserMessage converts a message.send payload into one stream-json `user`
// stdin line, counts it in flight, and writes it (or queues it while the
// child is down — flushed on the next spawn).
func (s *Server) sendUserMessage(payload turnPayload) {
	payload = s.prepareSkill(s.lifetimeContext, payload)
	blocks := []map[string]any{}
	if payload.preload != "" {
		blocks = append(blocks, map[string]any{"type": "text", "text": payload.preload})
	}
	blocks = append(blocks, map[string]any{"type": "text", "text": payload.Content})
	for _, att := range payload.Attachments {
		if !strings.HasPrefix(att.URL, "data:") {
			continue
		}
		comma := strings.Index(att.URL, ",")
		if comma < 0 {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		media := att.URL[5:comma]
		if i := strings.Index(media, ";"); i >= 0 {
			media = media[:i]
		}
		if media == "" {
			media = "image/jpeg"
		}
		b64 := att.URL[comma+1:]
		if _, err := base64.StdEncoding.DecodeString(b64); err != nil {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		blocks = append(blocks, map[string]any{
			"type": "image",
			"source": map[string]any{
				"type":       "base64",
				"media_type": media,
				"data":       b64,
			},
		})
	}
	line, err := json.Marshal(map[string]any{
		"type":    "user",
		"message": map[string]any{"role": "user", "content": blocks},
	})
	if err != nil {
		log.Printf("%s marshal user message failed: %v", logPrefix, err)
		return
	}
	s.mu.Lock()
	s.inflight++
	s.mu.Unlock()
	s.writeStdin(line)
}

// writeStdin writes one JSONL line to the child stdin; while the child is
// down the line goes to the pending queue instead,
// flushed on the next spawn. The blocking pipe write happens under stdinMu
// only — never under mu (see the lock-order note on Server).
func (s *Server) writeStdin(line []byte) {
	s.stdinMu.Lock()
	defer s.stdinMu.Unlock()
	s.mu.Lock()
	w := s.stdin
	s.mu.Unlock()
	if w == nil {
		s.queuePending(line)
		return
	}
	s.writePipe(w, line)
}

// writePipe performs the actual pipe write (caller holds stdinMu). Once a write
// is attempted, delivery is uncertain on error: never replay a desktop action.
func (s *Server) writePipe(w io.Writer, line []byte) {
	buf := make([]byte, 0, len(line)+1)
	buf = append(append(buf, line...), '\n')
	if n, err := w.Write(buf); err != nil || n != len(buf) {
		log.Printf("%s stdin write failed (delivery uncertain, not replayed): wrote=%d/%d error=%v", logPrefix, n, len(buf), err)
	}
}

func (s *Server) queuePending(line []byte) {
	s.mu.Lock()
	s.pending = append(s.pending, line)
	s.mu.Unlock()
}

// staleSessionMarker is the claude CLI stderr substring emitted when --resume
// targets a session id it can no longer find in its store.
const staleSessionMarker = "No conversation found with session ID"

// clearStaleSession drops the persisted session after a failed --resume so the
// next respawn starts fresh. Unlike newSession it does not signal the child
// (already exited) and leaves resumeNext untouched — buildArgv's `sessionID !=
// ""` gate alone suppresses --resume until a fresh session id is captured.
func (s *Server) clearStaleSession() {
	s.mu.Lock()
	s.sessionID = ""
	s.mu.Unlock()
	if err := os.Remove(s.cfg.SessionFile); err != nil && !os.IsNotExist(err) {
		log.Printf("%s remove stale session file failed: %v", logPrefix, err)
	}
}

// newSession clears the session id, disables resume for the NEXT spawn only,
// removes the session file and terminates the running child — the respawn
// loop restarts claude fresh (its bridge.status is the only ack, matching
// bridge.py).
func (s *Server) newSession() {
	log.Printf("%s session.new — restarting claude without resume", logPrefix)
	s.mu.Lock()
	s.sessionID = ""
	s.resumeNext = false
	child := s.child
	s.mu.Unlock()
	if err := os.Remove(s.cfg.SessionFile); err != nil && !os.IsNotExist(err) {
		log.Printf("%s remove session file failed: %v", logPrefix, err)
	}
	if child != nil && child.Process != nil {
		_ = child.Process.Signal(syscall.SIGTERM)
	}
}

// pumpStdout reads the child's JSONL stdout: session_id changes are persisted,
// result events settle an in-flight turn, and every JSON line is forwarded
// VERBATIM to the client (non-JSON lines are logged, not forwarded).
func (s *Server) pumpStdout(r io.Reader) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if !json.Valid([]byte(line)) {
			log.Printf("%s non-JSON stdout: %.200s", logPrefix, line)
			continue
		}
		var evt struct {
			Type      string `json:"type"`
			SessionID string `json:"session_id"`
		}
		if json.Unmarshal([]byte(line), &evt) == nil {
			if evt.SessionID != "" {
				s.storeSessionID(evt.SessionID)
			}
			if evt.Type == "result" {
				s.mu.Lock()
				if s.inflight > 0 {
					s.inflight--
				}
				s.mu.Unlock()
			}
		}
		s.send([]byte(line))
	}
	if err := scanner.Err(); err != nil {
		log.Printf("%s stdout scan error: %v", logPrefix, err)
	}
}

// pumpStderr logs stderr lines and keeps a bounded tail for crash logging.
func pumpStderr(r io.Reader, res *runResult) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), " \t\r")
		log.Printf("%s claude: %s", logPrefix, line)
		res.stderrTail = tail(res.stderrTail+line+"\n", stderrTailMax)
	}
}

func tail(s string, max int) string {
	if len(s) > max {
		return s[len(s)-max:]
	}
	return s
}

// -- session persistence -----------------------------------------------------

// loadSession reads the persisted session id (absent/corrupt file -> "").
func (s *Server) loadSession() string {
	data, err := os.ReadFile(s.cfg.SessionFile)
	if err != nil {
		return ""
	}
	var sess struct {
		SessionID string `json:"session_id"`
	}
	if err := json.Unmarshal(data, &sess); err != nil {
		return ""
	}
	return sess.SessionID
}

// storeSessionID persists a changed session id atomically (temp + rename).
func (s *Server) storeSessionID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == s.sessionID {
		return
	}
	s.sessionID = id
	data, _ := json.Marshal(map[string]string{"session_id": id})
	dir := filepath.Dir(s.cfg.SessionFile)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	tmp, err := os.CreateTemp(dir, ".session-*.json")
	if err != nil {
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
		return
	}
	if err := os.Rename(tmpName, s.cfg.SessionFile); err != nil {
		_ = os.Remove(tmpName)
		log.Printf("%s save session failed: %v", logPrefix, err)
	}
}
