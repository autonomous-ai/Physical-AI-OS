package codex

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

// A per-folder lock serializes turns, and a /proc check refuses to run while an interactive codex
// TUI still holds the folder.

// codingSelFileDefault persists chat→thread selections so a restart keeps each
// chat in its thread.
var codingSelFileDefault = codexHome + "/telegram_coding.json"

const (
	// codingTurnTimeout caps one remote-coding turn (tool use can be slow).
	codingTurnTimeout = 15 * time.Minute

	// telegramMessageLimit is Telegram's hard per-message character cap; longer
	// replies are chunked.
	telegramMessageLimit = 4000
)

const codingHelpText = "🤖 Coding over Telegram\n\n" +
	"/resume — list folders that have codex threads\n" +
	"/resume <n> — pick a thread by its number in the last list\n" +
	"/resume <folder> — pick the folder's newest thread\n" +
	"/sessions <folder> — list every thread in one folder\n" +
	"/new <folder> — start a new thread in a folder\n" +
	"/here — show which thread you're in\n" +
	"/device — return to the device assistant\n\n" +
	"Once a thread is selected, a plain message runs codex in that folder and sends the result back here."

// codingTarget is a chat's selected coding thread.
type codingTarget struct {
	Folder    string `json:"folder"`
	SessionID string `json:"session_id"`
}

// handleTelegramCoding intercepts coding commands and routes plain messages for
// a chat that has an active coding selection.
func (s *CodexService) handleTelegramCoding(ctx context.Context, rawText, chatID string) bool {
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
func (s *CodexService) handleCodingCommand(ctx context.Context, text, chatID string) bool {
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

// cmdListSessions lists folders (no arg) or every thread in one folder (arg),
// caches the listing for /use <n>, and DMs it.
func (s *CodexService) cmdListSessions(ctx context.Context, chatID, arg string) {
	var (
		sessions []codingSession
		header   string
	)
	if strings.TrimSpace(arg) == "" {
		sessions = s.codingFolders()
		header = "📂 Coding threads (most recent first):"
	} else {
		folder := normalizeFolder(arg)
		sessions = s.folderSessions(folder)
		header = "📂 Threads in " + folder + ":"
	}
	s.setCodingList(chatID, sessions)
	if len(sessions) == 0 {
		s.dmCoding(ctx, chatID, "No coding threads yet. Run `codex` in a folder from the terminal, or /new <folder> to start one.")
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

// cmdUseSession selects a thread by index (into the last listing) or by folder
// path (its newest thread).
func (s *CodexService) cmdUseSession(ctx context.Context, chatID, arg string) {
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
		s.dmCoding(ctx, chatID, fmt.Sprintf("No thread found in %s. Use /new %s to start one.", folder, folder))
		return
	}
	s.selectCoding(ctx, chatID, cs)
}

// selectCoding stores a resolved thread selection and confirms it.
func (s *CodexService) selectCoding(ctx context.Context, chatID string, cs codingSession) {
	s.setCodingTarget(chatID, codingTarget{Folder: cs.Folder, SessionID: cs.SessionID})
	s.dmCoding(ctx, chatID, fmt.Sprintf("✅ In thread:\n📂 %s\n📝 %s\n\nSend a message to continue coding. /device to exit.", cs.Folder, cs.label()))
}

// cmdNewSession selects a folder for a brand-new thread (no resume).
func (s *CodexService) cmdNewSession(ctx context.Context, chatID, arg string) {
	folder := normalizeFolder(arg)
	if folder == "" {
		s.dmCoding(ctx, chatID, "Usage: /new <folder>. Example: /new /root/myapp")
		return
	}
	if err := os.MkdirAll(folder, 0o755); err != nil {
		s.dmCoding(ctx, chatID, "❌ Could not create folder "+folder+": "+err.Error())
		return
	}
	s.setCodingTarget(chatID, codingTarget{Folder: folder, SessionID: ""})
	s.dmCoding(ctx, chatID, "🆕 New thread in "+folder+". Send your first request to begin.")
}

// cmdWhere reports the chat's current selection.
func (s *CodexService) cmdWhere(ctx context.Context, chatID string) {
	tgt, ok := s.getCodingTarget(chatID)
	if !ok {
		s.dmCoding(ctx, chatID, "On the device assistant. /sessions to pick a coding thread.")
		return
	}
	sid := tgt.SessionID
	if sid == "" {
		sid = "(new thread, no turn run yet)"
	}
	s.dmCoding(ctx, chatID, fmt.Sprintf("📂 %s\n🔑 %s", tgt.Folder, sid))
}

// runTelegramCodingTurn executes one hand-off turn: serialize on the folder,
// refuse if an interactive TUI holds it, run codex, persist any new thread id,
// and DM the reply.
// Runs in its own goroutine (called with `go`).
func (s *CodexService) runTelegramCodingTurn(ctx context.Context, chatID string, tgt codingTarget, prompt string) {
	unlock := s.lockCodingFolder(tgt.Folder)
	defer unlock()

	if s.liveCodexHolds(tgt.Folder) {
		s.dmCoding(ctx, chatID, "⚠️ An interactive codex session is open in the terminal at "+tgt.Folder+".\nClose it before continuing over Telegram (two writers would corrupt the rollout).")
		return
	}

	stopTyping := s.startCodingTyping(ctx, chatID)
	run := s.codingRunner
	if run == nil {
		run = s.runCodingCodex
	}
	reply, newID, err := run(ctx, tgt.Folder, tgt.SessionID, prompt)
	stopTyping()

	if err != nil {
		slog.Warn("telegram coding turn failed", "component", "codex", "folder", tgt.Folder, "error", err)
		s.dmCoding(ctx, chatID, "❌ Turn failed:\n"+err.Error())
		return
	}
	if newID != "" && newID != tgt.SessionID {
		s.setCodingTarget(chatID, codingTarget{Folder: tgt.Folder, SessionID: newID})
	}
	if strings.TrimSpace(reply) == "" {
		reply = "(turn finished with no reply text)"
	}
	s.dmCoding(ctx, chatID, reply)
}

// runCodingCodex is the production runner: `codex exec --json
// --dangerously-bypass-approvals-and-sandbox --cd <folder> [resume <thread>]
// <prompt>` (flag order matters: --cd is rejected after `resume`, so it rides
// before).
func (s *CodexService) runCodingCodex(ctx context.Context, folder, threadID, prompt string) (string, string, error) {
	cctx, cancel := context.WithTimeout(ctx, codingTurnTimeout)
	defer cancel()

	argv := []string{"exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--cd", folder}
	if threadID != "" {
		argv = append(argv, "resume", threadID)
	}
	argv = append(argv, prompt)

	cmd := exec.CommandContext(cctx, "codex", argv...)
	cmd.Dir = folder
	cmd.Env = s.codingChildEnv()
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb
	runErr := cmd.Run()

	reply, newID, turnErr := parseCodexResult(out.Bytes())
	if turnErr != "" && strings.TrimSpace(reply) == "" {
		return "", newID, fmt.Errorf("%s", truncRunes(turnErr, 400))
	}
	if runErr != nil && strings.TrimSpace(reply) == "" {
		if tail := strings.TrimSpace(errb.String()); tail != "" {
			return "", newID, fmt.Errorf("%s", truncRunes(tail, 400))
		}
		return "", newID, runErr
	}
	return reply, newID, nil
}

// parseCodexResult scans `codex exec --json` JSONL: thread.started → thread id,
// item.completed/agent_message → accumulated reply text, turn.completed → done,
// turn.failed/error → error message.
func parseCodexResult(b []byte) (reply, threadID, turnErr string) {
	sc := bufio.NewScanner(bytes.NewReader(b))
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	var parts []string
	var lastErr string
	completed := false
	for sc.Scan() {
		var f struct {
			Type     string `json:"type"`
			ThreadID string `json:"thread_id"`
			Message  string `json:"message"`
			Item     struct {
				ItemType string `json:"item_type"`
				Type     string `json:"type"`
				Text     string `json:"text"`
			} `json:"item"`
		}
		if json.Unmarshal(sc.Bytes(), &f) != nil {
			continue
		}
		switch f.Type {
		case "thread.started":
			if f.ThreadID != "" {
				threadID = f.ThreadID
			}
		case "item.completed":
			kind := f.Item.ItemType
			if kind == "" {
				kind = f.Item.Type
			}
			if kind == "agent_message" && strings.TrimSpace(f.Item.Text) != "" {
				parts = append(parts, f.Item.Text)
			}
		case "turn.completed":
			completed = true
		case "turn.failed", "error":
			if f.Message != "" {
				lastErr = f.Message
			}
		}
	}
	reply = strings.Join(parts, "\n\n")
	if !completed && strings.TrimSpace(reply) == "" {
		if lastErr == "" {
			lastErr = "codex turn did not complete"
		}
		turnErr = lastErr
	}
	return reply, threadID, turnErr
}

// codingChildEnv mirrors the gatewayd's turnEnv: the process env with HOME and
// CODEX_HOME asserted (deduped), plus the presync .env pairs (OPENAI_API_KEY in
// API mode; omitted in subscription mode, where codex reads auth.json from
// CODEX_HOME).
func (s *CodexService) codingChildEnv() []string {
	base := os.Environ()
	out := make([]string, 0, len(base)+8)
	for _, kv := range base {
		if strings.HasPrefix(kv, "HOME=") || strings.HasPrefix(kv, "CODEX_HOME=") {
			continue
		}
		out = append(out, kv)
	}
	out = append(out, loadEnvFilePairs(s.codingEnvFile())...)
	out = append(out, "HOME="+filepath.Dir(codexHome), "CODEX_HOME="+codexHome)
	return out
}

func (s *CodexService) codingEnvFile() string {
	if s.codingEnvFilePath != "" {
		return s.codingEnvFilePath
	}
	return codexHome + "/.env"
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

// liveCodexHolds reports whether an interactive codex process currently has
// folder as its cwd — the hand-off guard against concurrent rollout writers.
func (s *CodexService) liveCodexHolds(folder string) bool {
	if s.folderHasLiveCodex != nil {
		return s.folderHasLiveCodex(folder)
	}
	return procHoldsFolder(folder)
}

// procHoldsFolder scans /proc for a `codex` process whose cwd == folder (Linux;
// the device is Linux).
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
		if !procIsCodex(pid) {
			continue
		}
		cwd, err := os.Readlink(filepath.Join("/proc", pid, "cwd"))
		if err == nil && cwd == folder {
			return true
		}
	}
	return false
}

// procIsCodex checks whether /proc/<pid> is a codex CLI process.
func procIsCodex(pid string) bool {
	comm, err := os.ReadFile(filepath.Join("/proc", pid, "comm"))
	if err == nil && strings.TrimSpace(string(comm)) == "codex" {
		return true
	}
	cmdline, err := os.ReadFile(filepath.Join("/proc", pid, "cmdline"))
	if err != nil {
		return false
	}
	first := string(bytes.SplitN(cmdline, []byte{0}, 2)[0])
	return strings.Contains(filepath.Base(first), "codex")
}

func (s *CodexService) codingSelFile() string {
	if s.codingSelPath != "" {
		return s.codingSelPath
	}
	return codingSelFileDefault
}

func (s *CodexService) getCodingTarget(chatID string) (codingTarget, bool) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	tgt, ok := s.codingSel[chatID]
	return tgt, ok
}

func (s *CodexService) setCodingTarget(chatID string, tgt codingTarget) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	s.codingSel[chatID] = tgt
	s.saveCodingSelLocked()
}

func (s *CodexService) clearCodingTarget(chatID string) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingSel == nil {
		s.loadCodingSelLocked()
	}
	delete(s.codingSel, chatID)
	s.saveCodingSelLocked()
}

func (s *CodexService) setCodingList(chatID string, list []codingSession) {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	if s.codingList == nil {
		s.codingList = map[string][]codingSession{}
	}
	s.codingList[chatID] = list
}

func (s *CodexService) getCodingList(chatID string) []codingSession {
	s.codingMu.Lock()
	defer s.codingMu.Unlock()
	return s.codingList[chatID]
}

// loadCodingSelLocked reads persisted selections (called under codingMu with a
// nil map).
func (s *CodexService) loadCodingSelLocked() {
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
func (s *CodexService) saveCodingSelLocked() {
	data, err := json.Marshal(s.codingSel)
	if err != nil {
		return
	}
	path := s.codingSelFile()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		slog.Warn("telegram coding: save selection failed", "component", "codex", "error", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		slog.Warn("telegram coding: rename selection failed", "component", "codex", "error", err)
	}
}

// lockCodingFolder returns an unlock func after acquiring the per-folder mutex,
// so two turns for the same folder never run concurrently.
func (s *CodexService) lockCodingFolder(folder string) func() {
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
func (s *CodexService) dmCoding(ctx context.Context, chatID, text string) {
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
func (s *CodexService) postTelegramText(ctx context.Context, base, token, chatID, text string) {
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
		slog.Warn("telegram coding sendMessage failed", "component", "codex", "chatID", chatID, "error", err)
		return
	}
	_ = resp.Body.Close()
}

// startCodingTyping keeps the chat's "typing…" indicator alive until the
// returned stop func is called (indicator expires after ~5s).
func (s *CodexService) startCodingTyping(ctx context.Context, chatID string) func() {
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
