package gatewayd

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// turnResult mirrors bridge.py's per-run result dict.
type turnResult struct {
	rc             int
	threadStarted  bool
	turnEnded      bool // saw turn.completed or turn.failed
	timedOut       bool
	outputRejected bool
	spawnFailed    bool
	stderrTail     string   // bounded tail of stderr
	stdoutTail     string   // bounded tail of non-JSON stdout (resume heuristics)
	heldFrames     [][]byte // terminal failure frames held back during a resumed attempt
}

// turnWorker only starts a fresh App Server turn. Once started, later input is
// sent directly with turn/steer and does not wait for this worker.
func (s *Server) turnWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case o := <-s.ops:
			switch o.kind {
			case opTurn:
				if s.cfg.UseAppServer {
					s.runCorrelatedAppTurn(o.payload)
				} else {
					s.runCorrelatedTurn(ctx, o.payload)
				}
			case opSessionNew:
				s.mu.Lock()
				active := s.activeTurnID != "" || s.activeStarting
				if active {
					s.resetPending = true
				}
				s.mu.Unlock()
				if active {
					log.Printf("%s session.new waits for active App Server turn", logPrefix)
				} else {
					s.clearSession()
					s.sendStatus("session_cleared", "")
				}
			}
		}
	}
}

// runTurn executes one message.send: decode attachments, spawn codex exec
// (resuming the stored thread when present), retry once fresh if the resume
// target is gone, and surface terminal failures as bridge.error frames.
// During a resumed attempt, terminal failure frames are held back (see
// pumpStdout) so a fresh retry does not leave the client's turn already ended.
func (s *Server) runTurn(ctx context.Context, payload turnPayload) {
	payload = s.prepareSkill(ctx, payload)
	images := s.decodeAttachments(payload)
	s.pruneAttachments()

	s.mu.Lock()
	resumeID := s.threadID
	s.mu.Unlock()

	start := time.Now()
	log.Printf("%s turn start thread=%q resumed=%v images=%d",
		logPrefix, resumeID, resumeID != "", len(images))

	res := s.execTurn(ctx, payload.promptWithSkill(), images, resumeID)
	if resumeID != "" {
		if resumeFailed(res) {
			// Drop the held terminal failure frames: forwarding them would end the
			// device turn early and the fresh retry's events would re-open an
			// uncorrelated turn on the translator side.
			log.Printf("%s resume of thread %s failed (rc=%d) — retrying fresh (%d failure frames dropped)",
				logPrefix, resumeID, res.rc, len(res.heldFrames))
			s.clearSession()
			res = s.execTurn(ctx, payload.promptWithSkill(), images, "")
		} else if !res.outputRejected {
			// Resumed attempt is terminal (no fresh retry) — release what was held.
			for _, frame := range res.heldFrames {
				s.send(frame)
			}
		}
	}

	switch {
	case res.outputRejected:
		// Clear even a newly created thread. Its on-disk rollout contains the
		// rejected output; never resume it or replay the task's earlier mutations.
		s.clearSession()
		s.sendError(degenerateOutputError)
	case res.timedOut:
		// A resumed turn that ran out of time takes the thread down with it.
		// Rotation normally rides on a COMPLETED turn (the client's
		// ShouldRotateSession reads the token count off turn.completed), so a
		// thread whose every resume hangs can never be rotated — the next turn
		// resumes the same thread, hangs the same way, and the device stays
		// wedged across service restarts and reboots because the thread id is
		// on disk. Measured on lamp-0c89 2026-09-03: thread 01a06665 was
		// created 15:31 and every turn after 15:40 hung with no turn end, for
		// over an hour. Dropping the thread here is the only escape that does
		// not need a human: the next turn starts fresh, and continuity is
		// restored the same way any other rotation restores it.
		if resumeID != "" {
			log.Printf("%s resumed thread %s timed out after %s — dropping it so the next turn starts fresh",
				logPrefix, resumeID, s.cfg.TurnTimeout)
			s.clearSession()
		}
		s.sendError("timeout")
	case !res.turnEnded:
		errMsg := strings.TrimSpace(res.stderrTail)
		if errMsg == "" {
			errMsg = fmt.Sprintf("codex exited rc=%d without turn.completed", res.rc)
		}
		if len(errMsg) > stderrTailMax {
			errMsg = errMsg[len(errMsg)-stderrTailMax:]
		}
		s.sendError(errMsg)
	}

	s.mu.Lock()
	threadID := s.threadID
	s.mu.Unlock()
	log.Printf("%s turn end thread=%q resumed=%v duration=%s rc=%d",
		logPrefix, threadID, resumeID != "", time.Since(start).Round(time.Millisecond), res.rc)
}

// resumeFailed reports whether a failed resumed run should be retried fresh:
// the run failed (nonzero exit or no terminal event — checked by the caller
// path below), was not a timeout/spawn failure, and either never started a
// thread or the output mentions the session/thread cannot be found.
func resumeFailed(res turnResult) bool {
	if res.timedOut || res.spawnFailed || res.outputRejected {
		return false
	}
	if res.rc == 0 && res.turnEnded {
		return false // run succeeded — nothing to retry
	}
	if !res.threadStarted {
		return true
	}
	low := strings.ToLower(res.stderrTail + "\n" + res.stdoutTail)
	for _, hint := range resumeErrHints {
		if strings.Contains(low, hint) {
			return true
		}
	}
	return false
}

// buildArgv builds the codex exec command line. The prompt is always the
// final positional argument; image paths ride repeated -i flags.
//
// Flag ordering is load-bearing at rust-v0.142.5: `--cd` (like other
// non-global exec flags) is NOT accepted after the `resume` subcommand —
// `codex exec resume <id> --cd …` dies with "unexpected argument" before the
// turn starts, which would make every resume silently fall back to a fresh
// thread. Shared flags therefore go BEFORE `resume`; only resume-safe pieces
// (-i is a resume-own flag) plus the id + prompt follow it.
func (s *Server) buildArgv(prompt string, images []string, resumeID string) []string {
	argv := []string{s.cfg.CodexBin, "exec",
		"--json", "--dangerously-bypass-approvals-and-sandbox",
		"--cd", s.cfg.Workspace}
	if resumeID != "" {
		argv = append(argv, "resume", resumeID)
	}
	for _, img := range images {
		argv = append(argv, "-i", img)
	}
	return append(argv, prompt)
}

// turnEnv is os.Environ() with HOME and CODEX_HOME asserted (deduped).
func (s *Server) turnEnv() []string {
	env := os.Environ()
	out := make([]string, 0, len(env)+2)
	for _, kv := range env {
		if strings.HasPrefix(kv, "HOME=") || strings.HasPrefix(kv, "CODEX_HOME=") {
			continue
		}
		out = append(out, kv)
	}
	return append(out, "HOME="+s.cfg.Home, "CODEX_HOME="+s.cfg.CodexHome)
}

// execTurn spawns one `codex exec` subprocess for the turn, forwards its
// JSONL stdout verbatim to the client and returns the bounded result.
func (s *Server) execTurn(ctx context.Context, prompt string, images []string, resumeID string) turnResult {
	res := turnResult{rc: -1}
	argv := s.buildArgv(prompt, images, resumeID)
	log.Printf("%s spawning: %s <prompt %d chars>",
		logPrefix, strings.Join(argv[:len(argv)-1], " "), len(prompt))

	tctx, cancel := context.WithTimeout(ctx, s.cfg.TurnTimeout)
	defer cancel()

	cmd := exec.CommandContext(tctx, argv[0], argv[1:]...)
	cmd.Dir = s.cfg.Workspace
	cmd.Env = s.turnEnv()
	// Own process group so a timeout kill reaps codex AND its children.
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

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		res.spawnFailed = true
		res.stderrTail = "stdout pipe: " + err.Error()
		return res
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		res.spawnFailed = true
		res.stderrTail = "stderr pipe: " + err.Error()
		return res
	}
	if err := cmd.Start(); err != nil {
		log.Printf("%s spawn codex failed: %v", logPrefix, err)
		res.spawnFailed = true
		res.stderrTail = "spawn codex failed: " + err.Error()
		return res
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); s.pumpStdout(stdout, &res, resumeID != "", cancel) }()
	go func() { defer wg.Done(); pumpStderr(stderr, &res) }()
	wg.Wait()
	err = cmd.Wait()

	res.rc = cmd.ProcessState.ExitCode()
	if err != nil && res.rc == 0 {
		res.rc = -1 // killed / wait error without a real exit code
	}
	if tctx.Err() == context.DeadlineExceeded {
		log.Printf("%s turn timed out after %s — killed process group",
			logPrefix, s.cfg.TurnTimeout)
		res.timedOut = true
	}
	log.Printf("%s codex exited rc=%d", logPrefix, res.rc)
	return res
}

// pumpStdout forwards each JSON stdout line verbatim to the client while
// watching for thread.started (persist session) and terminal turn events.
//
// When holdFailures is set (resumed attempt), terminal failure frames
// (turn.failed and the top-level error event) are stashed in res.heldFrames
// instead of forwarded: the caller drops them when it retries fresh, or
// forwards them when the resumed attempt is terminal. thread.started/item.*
// frames still flow normally.
func (s *Server) pumpStdout(r io.Reader, res *turnResult, holdFailures bool, cancel context.CancelFunc) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if !json.Valid([]byte(line)) {
			log.Printf("%s debug: non-JSON stdout (not forwarded): %.200s", logPrefix, line)
			res.stdoutTail = tail(res.stdoutTail+line+"\n", stderrTailMax)
			continue
		}
		var evt struct {
			Type     string `json:"type"`
			ThreadID string `json:"thread_id"`
			Item     struct {
				Type     string `json:"type"`
				ItemType string `json:"item_type"`
				Text     string `json:"text"`
			} `json:"item"`
		}
		hold := false
		if json.Unmarshal([]byte(line), &evt) == nil {
			kind := evt.Item.ItemType
			if kind == "" {
				kind = evt.Item.Type
			}
			if kind == "agent_message" && degenerateAssistantOutput(evt.Item.Text) {
				res.outputRejected = true
				log.Printf("%s rejected degenerate assistant output (bytes=%d); killing process group", logPrefix, len(evt.Item.Text))
				cancel()
				return // Do not forward this item, terminal success, or later output.
			}
			switch evt.Type {
			case "thread.started":
				res.threadStarted = true
				if evt.ThreadID != "" {
					s.storeThreadID(evt.ThreadID)
				}
			case "turn.completed":
				res.turnEnded = true
			case "turn.failed":
				res.turnEnded = true
				hold = holdFailures
			case "error":
				hold = holdFailures
			}
		}
		if hold {
			res.heldFrames = append(res.heldFrames, []byte(line))
			continue
		}
		s.send([]byte(line))
	}
	if err := scanner.Err(); err != nil {
		log.Printf("%s stdout scan error: %v", logPrefix, err)
	}
}

// pumpStderr logs stderr lines and keeps a bounded tail for diagnostics.
func pumpStderr(r io.Reader, res *turnResult) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scanBufSize), streamLimit)
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), " \t\r")
		log.Printf("%s codex: %s", logPrefix, line)
		res.stderrTail = tail(res.stderrTail+line+"\n", stderrTailMax)
	}
}

func tail(s string, max int) string {
	if len(s) > max {
		return s[len(s)-max:]
	}
	return s
}

// -- image attachments -------------------------------------------------------

// decodeAttachments writes data-URL images to files and returns their paths;
// codex exec has no stdin image channel, so paths ride repeated -i flags and
// never enter the prompt.
func (s *Server) decodeAttachments(payload turnPayload) []string {
	var paths []string
	nowMS := time.Now().UnixMilli()
	for i, att := range payload.Attachments {
		if !strings.HasPrefix(att.URL, "data:") {
			continue
		}
		comma := strings.Index(att.URL, ",")
		if comma < 0 {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		data, err := base64.StdEncoding.DecodeString(att.URL[comma+1:])
		if err != nil {
			log.Printf("%s bad image attachment, skipped", logPrefix)
			continue
		}
		if err := os.MkdirAll(s.cfg.AttachDir, 0o755); err != nil {
			log.Printf("%s write attachment failed: %v", logPrefix, err)
			continue
		}
		path := filepath.Join(s.cfg.AttachDir, fmt.Sprintf("attach-%d-%d.jpg", nowMS, i))
		if err := os.WriteFile(path, data, 0o600); err != nil {
			log.Printf("%s write attachment failed: %v", logPrefix, err)
			continue
		}
		paths = append(paths, path)
	}
	return paths
}

// pruneAttachments drops attachments older than attachMaxAge (best-effort).
func (s *Server) pruneAttachments() {
	cutoff := time.Now().Add(-attachMaxAge)
	entries, err := os.ReadDir(s.cfg.AttachDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().Before(cutoff) {
			_ = os.Remove(filepath.Join(s.cfg.AttachDir, e.Name()))
		}
	}
}

// -- session persistence -----------------------------------------------------

// loadSession reads the persisted thread id (absent/corrupt file -> "").
func (s *Server) loadSession() string {
	data, err := os.ReadFile(s.cfg.SessionFile)
	if err != nil {
		return ""
	}
	var sess struct {
		ThreadID string `json:"thread_id"`
	}
	if err := json.Unmarshal(data, &sess); err != nil {
		return ""
	}
	return sess.ThreadID
}

// storeThreadID persists a changed thread id atomically (temp + rename).
func (s *Server) storeThreadID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == s.threadID {
		return
	}
	s.threadID = id
	data, _ := json.Marshal(map[string]string{"thread_id": id})
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

// clearSession forgets the thread id and deletes the session file.
func (s *Server) clearSession() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.threadID = ""
	if err := os.Remove(s.cfg.SessionFile); err != nil && !os.IsNotExist(err) {
		log.Printf("%s remove session file failed: %v", logPrefix, err)
	}
}
