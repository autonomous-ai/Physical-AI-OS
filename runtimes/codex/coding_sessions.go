package codex

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/syspath"
)

// Unlike claude, codex resumes by thread id GLOBALLY — `codex exec --cd <dir> resume <id>` sets
// the cwd independently and does NOT require it to match the original (device-verified: resuming an
// old thread echoes its id back).

// codexSessionsDirDefault is the on-device rollout store.
var codexSessionsDirDefault = codexHome + "/sessions"

const (
	// rolloutMetaScanLimit bounds how many bytes of a rollout are read while
	// recovering its thread id / cwd / recent prompts.
	rolloutMetaScanLimit = 4 * 1024 * 1024

	// recentPromptsMax is how many recent user prompts a listing shows per
	// session (most-recent first).
	recentPromptsMax = 3
)

// codingSession is one resumable codex thread discovered on disk.
type codingSession struct {
	Folder    string    // cwd recorded in the rollout — passed to `codex exec --cd`
	SessionID string    // codex thread id (`codex exec resume <id>`)
	Modified  time.Time // rollout mtime — recency for listing / newest-per-folder
	Recent    []string  // up to recentPromptsMax real user prompts, most-recent first
}

// label is the single-line description for a session (its most recent prompt).
func (c codingSession) label() string {
	if len(c.Recent) > 0 {
		return c.Recent[0]
	}
	return "(no description)"
}

// codexSessionsDir returns the rollout store path (test seam or default).
func (s *CodexService) codexSessionsDir() string {
	if s.codexSessionsDirPath != "" {
		return s.codexSessionsDirPath
	}
	return codexSessionsDirDefault
}

// allCodingSessions returns every discovered thread, most-recently-modified
// first, deduped by thread id (codex may write more than one rollout file for a
// resumed thread — keep the newest).
func (s *CodexService) allCodingSessions() []codingSession {
	root := s.codexSessionsDir()
	byThread := map[string]codingSession{}
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() || !strings.HasPrefix(d.Name(), "rollout-") || !strings.HasSuffix(d.Name(), ".jsonl") {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		threadID, folder, recent := readRolloutMeta(path)
		if threadID == "" || folder == "" {
			return nil
		}
		cur, ok := byThread[threadID]
		if !ok || info.ModTime().After(cur.Modified) {
			byThread[threadID] = codingSession{
				Folder:    folder,
				SessionID: threadID,
				Modified:  info.ModTime(),
				Recent:    recent,
			}
		}
		return nil
	})
	out := make([]codingSession, 0, len(byThread))
	for _, cs := range byThread {
		out = append(out, cs)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Modified.After(out[j].Modified) })
	return out
}

// codingFolders returns the NEWEST thread per folder, most-recent folder first
// — the default /sessions view (one line per project).
func (s *CodexService) codingFolders() []codingSession {
	all := s.allCodingSessions()
	seen := map[string]bool{}
	var out []codingSession
	for _, cs := range all {
		if seen[cs.Folder] {
			continue
		}
		seen[cs.Folder] = true
		out = append(out, cs)
	}
	return out
}

// folderSessions returns all threads in one folder, newest first.
func (s *CodexService) folderSessions(folder string) []codingSession {
	folder = normalizeFolder(folder)
	var out []codingSession
	for _, cs := range s.allCodingSessions() {
		if cs.Folder == folder {
			out = append(out, cs)
		}
	}
	return out
}

// latestSessionForFolder returns the newest thread in folder (ok=false if none).
func (s *CodexService) latestSessionForFolder(folder string) (codingSession, bool) {
	sessions := s.folderSessions(folder)
	if len(sessions) == 0 {
		return codingSession{}, false
	}
	return sessions[0], true
}

// readRolloutMeta recovers a rollout's thread id, cwd and the recent user
// prompts.
func readRolloutMeta(path string) (threadID, folder string, recent []string) {
	f, err := os.Open(path)
	if err != nil {
		return "", "", nil
	}
	defer f.Close()

	sc := bufio.NewScanner(io.LimitReader(f, rolloutMetaScanLimit))
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		var rec struct {
			Type    string          `json:"type"`
			Payload json.RawMessage `json:"payload"`
		}
		if json.Unmarshal(sc.Bytes(), &rec) != nil {
			continue
		}
		switch rec.Type {
		case "session_meta":
			var m struct {
				ID  string `json:"id"`
				Cwd string `json:"cwd"`
			}
			if json.Unmarshal(rec.Payload, &m) == nil {
				threadID, folder = m.ID, m.Cwd
			}
		case "response_item":
			if txt := rolloutUserText(rec.Payload); txt != "" && !isInjectedContext(txt) {
				recent = appendRecent(recent, txt)
			}
		}
	}
	return threadID, folder, mostRecentFirst(recent)
}

// rolloutUserText returns the text of a response_item ONLY when it is a user
// message (role == "user"), whose content is an array of {type:input_text,text}
// blocks.
func rolloutUserText(raw json.RawMessage) string {
	var p struct {
		Type    string `json:"type"`
		Role    string `json:"role"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Type != "message" || p.Role != "user" {
		return ""
	}
	parts := make([]string, 0, len(p.Content))
	for _, c := range p.Content {
		if c.Text != "" {
			parts = append(parts, c.Text)
		}
	}
	return strings.Join(parts, " ")
}

// appendRecent adds one compact prompt to the rolling window, keeping only the
// last recentPromptsMax (chronological order).
func appendRecent(recent []string, s string) []string {
	recent = append(recent, truncRunes(oneLine(s), 90))
	if len(recent) > recentPromptsMax {
		recent = recent[len(recent)-recentPromptsMax:]
	}
	return recent
}

// mostRecentFirst reverses a chronological prompt window (returns a fresh slice).
func mostRecentFirst(recent []string) []string {
	if len(recent) == 0 {
		return nil
	}
	out := make([]string, len(recent))
	for i, s := range recent {
		out[len(recent)-1-i] = s
	}
	return out
}

// normalizeFolder cleans a user-supplied path: trims quotes/space, expands a
// leading ~ to the agent home (/root on device, OS_AGENT_HOME off-device),
// makes it absolute (relative paths resolve under that home) and drops any trailing slash.
func normalizeFolder(p string) string {
	p = strings.TrimSpace(p)
	p = strings.Trim(p, `"'`)
	if p == "" {
		return ""
	}
	home := syspath.AgentHome()
	switch {
	case p == "~":
		p = home
	case strings.HasPrefix(p, "~/"):
		p = filepath.Join(home, p[2:])
	case !filepath.IsAbs(p):
		p = filepath.Join(home, p)
	}
	return filepath.Clean(p)
}

// oneLine collapses whitespace/newlines into single spaces for a compact label.
func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// isInjectedContext reports whether a "user" message is actually a synthetic
// context block the CLI prepends before the real prompt (environment info,
// system reminders, IDE/command wrappers) — not something the human typed.
func isInjectedContext(s string) bool {
	t := strings.TrimSpace(s)
	if t == "" {
		return true
	}
	if !strings.HasPrefix(t, "<") {
		return false
	}
	for _, tag := range []string{
		"<environment_context", "<system-reminder", "<user_instructions",
		"<command-name", "<local-command", "<ide_", "<system>",
	} {
		if strings.HasPrefix(t, tag) {
			return true
		}
	}
	return false
}

// humanizeAgo renders how long ago t was, for session listings.
func humanizeAgo(t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	}
}
