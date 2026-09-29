package claudecode

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// A per-folder lock serializes turns, and a /proc check refuses to run while an interactive TUI
// still holds the folder (two writers would corrupt the transcript).

const (
	// codingSelFileDefault persists chat→session selections so a restart keeps
	// each chat in its session.
	codingSelFileDefault = "/root/.claudecode/telegram_coding.json"

	// codingTurnTimeout caps one remote-coding turn (tool use can be slow).
	codingTurnTimeout = 15 * time.Minute

	// telegramMessageLimit is Telegram's hard per-message character cap; longer
	// replies are chunked.
	telegramMessageLimit = 4000
)

const codingHelpText = "🤖 Coding over Telegram\n\n" +
	"/resume — list folders that have claude sessions\n" +
	"/resume <n> — pick a session by its number in the last list\n" +
	"/resume <folder> — pick the folder's newest session\n" +
	"/sessions <folder> — list every session in one folder\n" +
	"/new <folder> — start a new session in a folder\n" +
	"/here — show which session you're in\n" +
	"/device — return to the device assistant\n\n" +
	"Once a session is selected, a plain message runs claude in that folder and sends the result back here."

// codingTarget is a chat's selected coding session.
type codingTarget struct {
	Folder    string `json:"folder"`
	SessionID string `json:"session_id"`
}

// handleTelegramCoding intercepts coding commands and routes plain messages for
// a chat that has an active coding selection.
func (s *ClaudeCodeService) handleTelegramCoding(ctx context.Context, rawText, chatID string) bool {
	text := strings.TrimSpace(rawText)
	if strings.HasPrefix(text, "/") {
		return s.handleCodingCommand(ctx, text, chatID)
	}
	tgt, ok := s.getCodingTarget(chatID)
	if !ok {
		return false
	}
	go s.runTelegramCodingTurn(ctx, chatID, tgt, text)
	return true
}

// handleCodingCommand dispatches a /slash command.
func (s *ClaudeCodeService) handleCodingCommand(ctx context.Context, text, chatID string) bool {
	fields := strings.Fields(text)
	cmd := strings.ToLower(fields[0])
	arg := strings.TrimSpace(strings.TrimPrefix(text, fields[0]))
	switch cmd {
	case "/resume":
		if arg == "" {
			s.cmdListSessions(ctx, chatID, "")
		} else {
			s.cmdUseSession(ctx, chatID, arg)
		}
	case "/sessions", "/ls":
		s.cmdListSessions(ctx, chatID, arg)
	case "/use", "/cd":
		s.cmdUseSession(ctx, chatID, arg)
	case "/new":
		s.cmdNewSession(ctx, chatID, arg)
	case "/here", "/status", "/where":
		s.cmdWhere(ctx, chatID)
	case "/device", "/lamp", "/exit", "/quit":
		s.clearCodingTarget(chatID)
		s.dmCoding(ctx, chatID, "✅ Back to the device assistant. Plain messages now talk to the lamp.")
	case "/help", "/coding":
		s.dmCoding(ctx, chatID, codingHelpText)
	default:
		tgt, ok := s.getCodingTarget(chatID)
		if !ok {
			return false
		}
		go s.runTelegramCodingTurn(ctx, chatID, tgt, text)
	}
	return true
}

// cmdListSessions lists folders (no arg) or every session in one folder (arg),
// caches the listing for /use <n>, and DMs it.
func (s *ClaudeCodeService) cmdListSessions(ctx context.Context, chatID, arg string) {
	var (
		sessions []codingSession
		header   string
	)
	if strings.TrimSpace(arg) == "" {
		sessions = s.codingFolders()
		header = "📂 Coding sessions (most recent first):"
	} else {
		folder := normalizeFolder(arg)
		sessions = s.folderSessions(folder)
		header = "📂 Sessions in " + folder + ":"
	}
	s.setCodingList(chatID, sessions)
	if len(sessions) == 0 {
		s.dmCoding(ctx, chatID, "No coding sessions yet. Run `claude` in a folder from the terminal, or /new <folder> to start one.")
		return
	}
	var b strings.Builder
	b.WriteString(header)
	b.WriteString("\n\n")
	for i, cs := range sessions {
		fmt.Fprintf(&b, "%d.  📂 %s\n     🕐 %s\n", i+1, cs.Folder, humanizeAgo(cs.Modified))
		if len(cs.Recent) == 0 {
			b.WriteString("     📝 (no description)\n")
		}
		for j, p := range cs.Recent {
			marker := "📝"
			if j > 0 {
				marker = "  ↳"
			}
			fmt.Fprintf(&b, "     %s %s\n", marker, p)
		}
		b.WriteString("\n")
	}
	b.WriteString("👉 Reply /resume <number> (e.g. /resume 1) to pick one, /device for the assistant.")
	s.dmCoding(ctx, chatID, b.String())
}

// cmdUseSession selects a session by index (into the last listing) or by folder
// path (its newest session).
func (s *ClaudeCodeService) cmdUseSession(ctx context.Context, chatID, arg string) {
	arg = strings.TrimSpace(arg)
	if arg == "" {
		s.dmCoding(ctx, chatID, "Usage: /resume <n>  or  /resume <folder>. /resume to list them.")
		return
	}
	if n, err := strconv.Atoi(arg); err == nil {
		list := s.getCodingList(chatID)
		if n < 1 || n > len(list) {
			s.dmCoding(ctx, chatID, "Invalid number. /resume to see the list again.")
			return
		}
		s.selectCoding(ctx, chatID, list[n-1])
		return
	}
	cs, ok := s.latestSessionForFolder(arg)
	if !ok {
		folder := normalizeFolder(arg)
		s.dmCoding(ctx, chatID, fmt.Sprintf("No session found in %s. Use /new %s to start one.", folder, folder))
		return
	}
	s.selectCoding(ctx, chatID, cs)
}

// selectCoding stores a resolved session selection and confirms it, including
// the terminal command that opens the same session interactively.
func (s *ClaudeCodeService) selectCoding(ctx context.Context, chatID string, cs codingSession) {
	s.setCodingTarget(chatID, codingTarget{Folder: cs.Folder, SessionID: cs.SessionID})
	s.dmCoding(ctx, chatID, fmt.Sprintf("✅ In session:\n📂 %s\n📝 %s\n💻 Terminal: claude --resume %s (in that folder), or just run claude-sessions\n\nSend a message to continue coding. /device to exit.", cs.Folder, cs.label(), cs.SessionID))
}

// cmdNewSession selects a folder for a brand-new session (no --resume).
func (s *ClaudeCodeService) cmdNewSession(ctx context.Context, chatID, arg string) {
	folder := normalizeFolder(arg)
	if folder == "" {
		s.dmCoding(ctx, chatID, "Usage: /new <folder>. Example: /new /root/myapp")
		return
	}
	// The device-main workspace is off-limits: the bridge permanently holds a live
	// headless claude there, so liveClaudeHolds() would refuse every turn and the
	// chat would be stuck on a session that can never run.
	if folder == deviceMainWorkspace {
		s.dmCoding(ctx, chatID, "❌ "+deviceMainWorkspace+" is the device assistant's own folder — it can't be used as a coding session.\nUse another folder (e.g. /new /root/myapp), or /device to talk to the assistant.")
		return
	}
	if err := os.MkdirAll(folder, 0o755); err != nil {
		s.dmCoding(ctx, chatID, "❌ Could not create folder "+folder+": "+err.Error())
		return
	}
	s.setCodingTarget(chatID, codingTarget{Folder: folder, SessionID: ""})
	s.dmCoding(ctx, chatID, "🆕 New session in "+folder+". Send your first request to begin.\n💻 To continue this session in the device terminal later: cd "+folder+" && claude-sessions, then pick it from the list.")
}

// cmdWhere reports the chat's current selection.
func (s *ClaudeCodeService) cmdWhere(ctx context.Context, chatID string) {
	tgt, ok := s.getCodingTarget(chatID)
	if !ok {
		s.dmCoding(ctx, chatID, "On the device assistant. /sessions to pick a coding session.")
		return
	}
	if tgt.SessionID == "" {
		s.dmCoding(ctx, chatID, fmt.Sprintf("📂 %s\n🔑 (new session, no turn run yet)", tgt.Folder))
		return
	}
	s.dmCoding(ctx, chatID, fmt.Sprintf("📂 %s\n🔑 %s\n💻 Terminal: claude --resume %s (in that folder), or just run claude-sessions", tgt.Folder, tgt.SessionID, tgt.SessionID))
}

// runTelegramCodingTurn executes one hand-off turn: serialize on the folder,
// refuse if an interactive TUI holds it, run claude, persist any new session id,
// and DM the reply.
// Runs in its own goroutine (called with `go`).
func (s *ClaudeCodeService) runTelegramCodingTurn(ctx context.Context, chatID string, tgt codingTarget, prompt string) {
	unlock := s.lockCodingFolder(tgt.Folder)
	defer unlock()

	if s.liveClaudeHolds(tgt.Folder) {
		s.dmCoding(ctx, chatID, "⚠️ An interactive claude session is open in the terminal at "+tgt.Folder+".\nClose it before continuing over Telegram (two writers would corrupt the transcript).")
		return
	}

	stopTyping := s.startCodingTyping(ctx, chatID)
	run := s.codingRunner
	if run == nil {
		run = s.runCodingClaude
	}
	reply, newSID, err := run(ctx, tgt.Folder, tgt.SessionID, prompt)
	stopTyping()

	if err != nil {
		slog.Warn("telegram coding turn failed", "component", "claudecode", "folder", tgt.Folder, "error", err)
		s.dmCoding(ctx, chatID, "❌ Turn failed:\n"+err.Error())
		return
	}
	if newSID != "" && newSID != tgt.SessionID {
		s.setCodingTarget(chatID, codingTarget{Folder: tgt.Folder, SessionID: newSID})
	}
	if strings.TrimSpace(reply) == "" {
		reply = "(turn finished with no reply text)"
	}
	s.dmCoding(ctx, chatID, reply)
}

// runCodingClaude is the production runner: `claude --print --output-format json
// [--resume <uuid>] --dangerously-skip-permissions` in the folder's cwd, prompt
// on stdin.
func (s *ClaudeCodeService) runCodingClaude(ctx context.Context, folder, sessionID, prompt string) (string, string, error) {
	cctx, cancel := context.WithTimeout(ctx, codingTurnTimeout)
	defer cancel()

	args := []string{"--print", "--output-format", "json", "--dangerously-skip-permissions"}
	if sessionID != "" {
		args = append(args, "--resume", sessionID)
	} else {
		args = append(args, "--name", codingSessionName(prompt))
	}
	cmd := exec.CommandContext(cctx, "claude", args...)
	cmd.Dir = folder
	cmd.Env = s.codingChildEnv()
	cmd.Stdin = strings.NewReader(prompt)
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		if tail := strings.TrimSpace(errb.String()); tail != "" {
			return "", "", fmt.Errorf("%s", truncRunes(tail, 400))
		}
		return "", "", err
	}
	result, sid, isErr := parseClaudeJSONResult(out.Bytes())
	if isErr && strings.TrimSpace(result) == "" {
		return "", "", fmt.Errorf("claude turn errored")
	}
	return result, sid, nil
}

// codingSessionName derives a display name for a new session from its first
// prompt, tagged so its Telegram origin is obvious when the session is
// reopened in the terminal (`cc` / `claude --resume <id>`).
func codingSessionName(prompt string) string {
	n := truncRunes(oneLine(prompt), 40)
	if n == "" {
		n = "session"
	}
	return "tg: " + n
}

// parseClaudeJSONResult extracts result text, session id and error flag from
// `--output-format json` output (a single result object; a stray leading line
// is tolerated by scanning for the last JSON object).
func parseClaudeJSONResult(b []byte) (result, sessionID string, isErr bool) {
	type res struct {
		Result    string `json:"result"`
		SessionID string `json:"session_id"`
		IsError   bool   `json:"is_error"`
		Subtype   string `json:"subtype"`
	}
	parse := func(data []byte) (res, bool) {
		var r res
		if json.Unmarshal(bytes.TrimSpace(data), &r) == nil && (r.Result != "" || r.SessionID != "" || r.Subtype != "") {
			return r, true
		}
		return res{}, false
	}
	if r, ok := parse(b); ok {
		return r.Result, r.SessionID, r.IsError
	}
	sc := bufio.NewScanner(bytes.NewReader(b))
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	var last res
	found := false
	for sc.Scan() {
		if r, ok := parse(sc.Bytes()); ok {
			last, found = r, true
		}
	}
	if found {
		return last.Result, last.SessionID, last.IsError
	}
	return strings.TrimSpace(string(b)), "", false
}

// codingChildEnv builds the exec env: the process env, the presync .env pairs
// (ANTHROPIC_* creds), then IS_SANDBOX=1 + HOME=/root (root needs IS_SANDBOX for
// --dangerously-skip-permissions; same as the gatewayd child).
func (s *ClaudeCodeService) codingChildEnv() []string {
	env := os.Environ()
	env = append(env, loadEnvFilePairs(s.codingEnvFile())...)
	env = append(env, "IS_SANDBOX=1", "HOME=/root")
	return env
}

func (s *ClaudeCodeService) codingEnvFile() string {
	if s.codingEnvFilePath != "" {
		return s.codingEnvFilePath
	}
	return EnvFile
}

// loadEnvFilePairs parses a KEY=VALUE launch env file into "KEY=VALUE" entries
// (blank/#/no-"=" lines skipped, trimmed, surrounding double quotes stripped —
// same rules as the gatewayd child loader).
func loadEnvFilePairs(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		i := strings.Index(line, "=")
		if i < 0 {
			continue
		}
		key := strings.TrimSpace(line[:i])
		if key == "" {
			continue
		}
		val := strings.Trim(strings.TrimSpace(line[i+1:]), `"`)
		out = append(out, key+"="+val)
	}
	return out
}

// liveClaudeHolds reports whether an interactive claude process currently has
// folder as its cwd — the hand-off guard against concurrent transcript writers.
func (s *ClaudeCodeService) liveClaudeHolds(folder string) bool {
	if s.folderHasLiveClaude != nil {
		return s.folderHasLiveClaude(folder)
	}
	return procHoldsFolder(folder)
}

// procHoldsFolder scans /proc for a `claude` process whose cwd == folder.
func procHoldsFolder(folder string) bool {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return false
	}
	self := strconv.Itoa(os.Getpid())
	for _, e := range entries {
		pid := e.Name()
		if !e.IsDir() || pid == self || pid[0] < '0' || pid[0] > '9' {
			continue
		}
		if !procIsClaude(pid) {
			continue
		}
		cwd, err := os.Readlink(filepath.Join("/proc", pid, "cwd"))
		if err == nil && cwd == folder {
			return true
		}
	}
	return false
}

// procIsClaude checks whether /proc/<pid> is a claude CLI process (not this
// gatewayd's headless child, which the caller can't distinguish — but the
// headless child runs in the workspace, never a user coding folder, so a cwd
// match already implies an interactive session).
func procIsClaude(pid string) bool {
	comm, err := os.ReadFile(filepath.Join("/proc", pid, "comm"))
	if err == nil && strings.TrimSpace(string(comm)) == "claude" {
		return true
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", pid, "cmdline"))
	if err != nil {
		return false
	}
	first := string(bytes.SplitN(cmdline, []byte{0}, 2)[0])
	return strings.Contains(filepath.Base(first), "claude")
}

func (s *ClaudeCodeService) codingSelFile() string {
	if s.codingSelPath != "" {
		return s.codingSelPath
	}
	return codingSelFileDefault
}

func (s *ClaudeCodeService) getCodingTarget(chatID string) (codingTarget, bool) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	tgt, ok := s.codingSel[chatID]
	return tgt, ok
}

func (s *ClaudeCodeService) setCodingTarget(chatID string, tgt codingTarget) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	s.codingSel[chatID] = tgt
	s.saveCodingSelLocked()
}

func (s *ClaudeCodeService) clearCodingTarget(chatID string) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	delete(s.codingSel, chatID)
	s.saveCodingSelLocked()
}

func (s *ClaudeCodeService) setCodingList(chatID string, list []codingSession) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingList == nil {
		s.codingList = map[string][]codingSession{}
	}
	s.codingList[chatID] = list
}

func (s *ClaudeCodeService) getCodingList(chatID string) []codingSession {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	return s.codingList[chatID]
}

// loadCodingSelLocked reads persisted selections (called under codingMu with a
// nil map).
func (s *ClaudeCodeService) loadCodingSelLocked() {
	s.codingSel = map[string]codingTarget{}
	data, err := os.ReadFile(s.codingSelFile())
	if err != nil {
		return
	}
	var m map[string]codingTarget
	if json.Unmarshal(data, &m) == nil && m != nil {
		s.codingSel = m
	}
}

// saveCodingSelLocked persists selections atomically (called under codingMu).
func (s *ClaudeCodeService) saveCodingSelLocked() {
	data, err := json.Marshal(s.codingSel)
	if err != nil {
		return
	}
	path := s.codingSelFile()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		slog.Warn("telegram coding: save selection failed", "component", "claudecode", "error", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		slog.Warn("telegram coding: rename selection failed", "component", "claudecode", "error", err)
	}
}

// lockCodingFolder returns an unlock func after acquiring the per-folder mutex,
// so two turns for the same folder never run concurrently.
func (s *ClaudeCodeService) lockCodingFolder(folder string) func() {
	s.codingMu.Lock()
	if s.codingFolder == nil {
		s.codingFolder = map[string]*sync.Mutex{}
	}
	mu := s.codingFolder[folder]
	if mu == nil {
		mu = &sync.Mutex{}
		s.codingFolder[folder] = mu
	}
	s.codingMu.Unlock()
	mu.Lock()
	return mu.Unlock
}

// dmCoding sends text to chatID, chunked to Telegram's per-message limit.
func (s *ClaudeCodeService) dmCoding(ctx context.Context, chatID, text string) {
	token := s.config.TelegramBotToken
	if token == "" {
		return
	}
	base := s.telegramAPIBase
	if base == "" {
		base = telegramAPIBaseDefault
	}
	for _, chunk := range chunkString(text, telegramMessageLimit) {
		s.postTelegramText(ctx, base, token, chatID, chunk)
	}
}

// postTelegramText POSTs one sendMessage (respects telegramAPIBase for tests).
func (s *ClaudeCodeService) postTelegramText(ctx context.Context, base, token, chatID, text string) {
	url := fmt.Sprintf("%s/bot%s/sendMessage", base, token)
	payload, _ := json.Marshal(map[string]string{"chat_id": chatID, "text": text})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		slog.Warn("telegram coding sendMessage failed", "component", "claudecode", "chatID", chatID, "error", err)
		return
	}
	_ = resp.Body.Close()
}

// startCodingTyping keeps the chat's "typing…" indicator alive until the
// returned stop func is called (indicator expires after ~5s).
func (s *ClaudeCodeService) startCodingTyping(ctx context.Context, chatID string) func() {
	done := make(chan struct{})
	go func() {
		s.sendTelegramTyping(ctx, chatID)
		t := time.NewTicker(4 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				return
			case <-t.C:
				s.sendTelegramTyping(ctx, chatID)
			}
		}
	}()
	var once sync.Once
	return func() { once.Do(func() { close(done) }) }
}

// chunkString splits s into pieces of at most limit runes, preferring to break
// on a newline near the boundary so replies stay readable.
func chunkString(s string, limit int) []string {
	if limit <= 0 || len([]rune(s)) <= limit {
		return []string{s}
	}
	var out []string
	runes := []rune(s)
	for len(runes) > 0 {
		if len(runes) <= limit {
			out = append(out, string(runes))
			break
		}
		cut := limit
		for i := limit - 1; i > limit*3/4; i-- {
			if runes[i] == '\n' {
				cut = i + 1
				break
			}
		}
		out = append(out, string(runes[:cut]))
		runes = runes[cut:]
	}
	return out
}
