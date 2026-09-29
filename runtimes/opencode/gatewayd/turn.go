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

// turnResult is the bounded per-run result of one `opencode run` subprocess.
type turnResult struct {
	rc            int
	threadStarted bool
	turnEnded     bool // saw session.idle (terminal success)
	timedOut      bool
	spawnFailed   bool
	stderrTail    string   // bounded tail of stderr
	stdoutTail    string   // bounded tail of non-JSON stdout (resume heuristics)
	errText       string   // error text pulled from session.error/error JSON frames
	heldFrames    [][]byte // terminal failure frames held back during a resumed attempt
	// sessionID is the opencode session id seen in the streamed JSON frames of
	// this run.
	sessionID string
}

// turnWorker drains the op queue one entry at a time (strict serialization).
// session.new rides the same queue so it executes AFTER earlier turns.
func (s *Server) turnWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case o := <-s.ops:
			switch o.kind {
			case opTurn:
				s.runCorrelatedTurn(ctx, o.payload)
			case opSessionNew:
				log.Printf("%s session.new — clearing thread id, next turn starts fresh", logPrefix)
				s.clearSession()
				s.sendStatus("session_cleared", "")
			}
		}
	}
}

// runTurn executes one message.send: decode attachments, spawn opencode run
// (resuming the stored session when present), retry once fresh if the resume
// target is gone, and surface terminal failures as bridge.error frames.
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
			log.Printf("%s resume of thread %s failed (rc=%d) — retrying fresh (%d failure frames dropped)",
				logPrefix, resumeID, res.rc, len(res.heldFrames))
			s.clearSession()
			res = s.execTurn(ctx, payload.promptWithSkill(), images, "")
		} else {
			for _, frame := range res.heldFrames {
				s.send(frame)
			}
		}
	}

	switch {
	case res.timedOut:
		// Same escape as the codex gatewayd: rotation rides on a COMPLETED
		// turn, so a thread whose every resume hangs can never be rotated and
		// the device stays wedged across restarts (the thread id is on disk).
		if resumeID != "" {
			log.Printf("%s resumed thread %s timed out after %s — dropping it so the next turn starts fresh",
				logPrefix, resumeID, s.cfg.TurnTimeout)
			s.clearSession()
		}
		s.sendError("timeout")
	case !res.turnEnded:
		errMsg := strings.TrimSpace(res.errText)
		if errMsg == "" {
			errMsg = strings.TrimSpace(res.stderrTail)
		}
		if errMsg == "" {
			errMsg = fmt.Sprintf("opencode run exited rc=%d without producing a reply", res.rc)
		}
		if len(errMsg) > stderrTailMax {
			errMsg = errMsg[len(errMsg)-stderrTailMax:]
		}
		s.sendError(errMsg)
	default:
		// Persist the session id only after success so a failed turn's session never lands on
		// disk and strands every later resume.
		if res.sessionID != "" {
			s.storeThreadID(res.sessionID)
		}
		// opencode run emits no terminal event, so synthesize the session.idle the translator finalizes on.
		s.sendJSON(map[string]any{"type": "session.idle", "sessionID": res.sessionID})
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
	if res.timedOut || res.spawnFailed {
		return false
	}
	if res.rc == 0 && res.turnEnded {
		return false
	}
	if !res.threadStarted {
		return true
	}
	low := strings.ToLower(res.stderrTail + "\n" + res.stdoutTail + "\n" + res.errText)
	for _, hint := range resumeErrHints {
		if strings.Contains(low, hint) {
			return true
		}
	}
	return false
}

// buildArgv builds the `opencode run` command line.
// Model/provider come from opencode.json (presync-owned) — never --model here.
func (s *Server) buildArgv(prompt string, images []string, resumeID string) []string {
	argv := []string{s.cfg.Bin, "run",
		"--format", "json",
		"--auto", // headless permission bypass
		"--dir", s.cfg.Workspace}
	if resumeID != "" {
		argv = append(argv, "--session", resumeID)
	}
	for _, img := range images {
		argv = append(argv, "--file", img)
	}
	// "--" keeps a user prompt starting with "-" from being parsed as an opencode flag.
	return append(argv, "--", prompt)
}

// turnEnv is os.Environ() with HOME asserted (deduped).
func (s *Server) turnEnv() []string {
	env := os.Environ()
	out := make([]string, 0, len(env)+1)
	for _, kv := range env {
		if strings.HasPrefix(kv, "HOME=") {
			continue
		}
		out = append(out, kv)
	}
	return append(out, "HOME="+s.cfg.Home)
}

// execTurn spawns one `opencode run` subprocess for the turn, forwards its
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
	// Own process group so a timeout kill reaps opencode AND its children.
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
		log.Printf("%s spawn opencode failed: %v", logPrefix, err)
		res.spawnFailed = true
		res.stderrTail = "spawn opencode failed: " + err.Error()
		return res
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); s.pumpStdout(stdout, &res, resumeID != "") }()
	go func() { defer wg.Done(); pumpStderr(stderr, &res) }()
	wg.Wait()
	err = cmd.Wait()

	res.rc = cmd.ProcessState.ExitCode()
	if err != nil && res.rc == 0 {
		res.rc = -1
	}
	if tctx.Err() == context.DeadlineExceeded {
		log.Printf("%s turn timed out after %s — killed process group",
			logPrefix, s.cfg.TurnTimeout)
		res.timedOut = true
	}
	if res.rc == 0 && !res.timedOut {
		res.turnEnded = true
	}
	log.Printf("%s opencode exited rc=%d", logPrefix, res.rc)
	return res
}

// pumpStdout forwards each JSON stdout line verbatim to the client while
// watching for the opencode sessionID (persist session) and terminal turn
// events (session.idle = success, session.error/error = failure).
func (s *Server) pumpStdout(r io.Reader, res *turnResult, holdFailures bool) {
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
			Type      string          `json:"type"`
			SessionID string          `json:"sessionID"`
			Status    string          `json:"status"`
			Error     json.RawMessage `json:"error"`
			Message   string          `json:"message"`
		}
		hold := false
		if json.Unmarshal([]byte(line), &evt) == nil {
			if evt.SessionID != "" {
				res.threadStarted = true
				res.sessionID = evt.SessionID
			}
			switch {
			case evt.Type == "session.idle",
				evt.Type == "session.status" && evt.Status == "idle":
				res.turnEnded = true
			case evt.Type == "session.error", evt.Type == "error":
				res.errText = tail(res.errText+string(evt.Error)+" "+evt.Message+"\n", stderrTailMax)
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
		log.Printf("%s opencode: %s", logPrefix, line)
		res.stderrTail = tail(res.stderrTail+line+"\n", stderrTailMax)
	}
}

func tail(s string, max int) string {
	if len(s) > max {
		return s[len(s)-max:]
	}
	return s
}

// decodeAttachments writes data-URL images to files and returns their paths;
// opencode run takes image/file attachments via repeated --file flags, so paths
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

// loadSession reads the persisted opencode session id (absent/corrupt file -> "").
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

// storeThreadID persists a changed opencode session id atomically (temp + rename).
func (s *Server) storeThreadID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == s.threadID {
		return
	}
	s.threadID = id
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

// clearSession forgets the thread id and deletes the session file.
func (s *Server) clearSession() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.threadID = ""
	if err := os.Remove(s.cfg.SessionFile); err != nil && !os.IsNotExist(err) {
		log.Printf("%s remove session file failed: %v", logPrefix, err)
	}
}
