package opencode

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// opencode stores its sessions under ~/.local/share/opencode in an internal format — NOT the
// parseable per-folder rollout transcripts codex used — so there is no on-disk cwd we can recover
// to build a cross-folder "resumable sessions" list.

// codingSession is one resumable opencode session.
type codingSession struct {
	Folder    string    // working dir — passed to `opencode run --dir`
	SessionID string    // opencode session id — passed to `opencode run --session <id>`
	Modified  time.Time // recency for listing / newest-per-folder
	Recent    []string  // short recent-prompt lines, most-recent first (unused while discovery is degraded)
}

// label is the single-line description for a session (its most recent prompt).
func (c codingSession) label() string {
	if len(c.Recent) > 0 {
		return c.Recent[0]
	}
	return "(no description)"
}

// codingSessionListTimeout bounds the `opencode session list` call.
const codingSessionListTimeout = 5 * time.Second

// codingSessionExcludeDirs lists working-directory prefixes that DO NOT
// represent a user-visible coding thread.
var codingSessionExcludeDirs = []string{
	"/root/.opencode/workspace",
}

// allCodingSessions returns every resumable user coding session, most-recent
// first.
// On any failure (CLI missing, timeout, JSON shape change) returns nil so the list is
// empty-but-consistent rather than a stale/partial view.
func (s *OpenCodeService) allCodingSessions() []codingSession {
	ctx, cancel := context.WithTimeout(context.Background(), codingSessionListTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "opencode", "session", "list", "--format", "json")
	cmd.Env = s.codingChildEnv()
	out, err := cmd.Output()
	if err != nil {
		slog.Warn("opencode session list failed", "component", "opencode-coding", "error", err)
		return nil
	}

	var raw []struct {
		ID        string `json:"id"`
		Title     string `json:"title"`
		Updated   int64  `json:"updated"` // unix ms
		Directory string `json:"directory"`
	}
	if err := json.Unmarshal(out, &raw); err != nil {
		slog.Warn("opencode session list decode failed", "component", "opencode-coding", "error", err)
		return nil
	}

	result := make([]codingSession, 0, len(raw))
	for _, r := range raw {
		if r.Directory == "" || r.ID == "" {
			continue
		}
		if isExcludedCodingDir(r.Directory) {
			continue
		}
		title := strings.TrimSpace(r.Title)
		recent := []string(nil)
		if title != "" {
			recent = []string{oneLine(title)}
		}
		result = append(result, codingSession{
			Folder:    r.Directory,
			SessionID: r.ID,
			Modified:  time.UnixMilli(r.Updated),
			Recent:    recent,
		})
	}
	sort.SliceStable(result, func(i, j int) bool {
		return result[i].Modified.After(result[j].Modified)
	})
	return result
}

// isExcludedCodingDir reports whether a session's working directory belongs to
// the device-chat / gatewayd territory and should be hidden from the Telegram
// coding list.
func isExcludedCodingDir(dir string) bool {
	clean := filepath.Clean(dir)
	for _, prefix := range codingSessionExcludeDirs {
		if clean == prefix || strings.HasPrefix(clean, prefix+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// codingFolders returns the NEWEST session per folder, most-recent folder first
// — the default /sessions view (one line per project).
func (s *OpenCodeService) codingFolders() []codingSession {
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

// folderSessions returns all sessions in one folder, newest first.
func (s *OpenCodeService) folderSessions(folder string) []codingSession {
	folder = normalizeFolder(folder)
	var out []codingSession
	for _, cs := range s.allCodingSessions() {
		if cs.Folder == folder {
			out = append(out, cs)
		}
	}
	return out
}

// latestSessionForFolder returns the newest session in folder (ok=false if
// none).
func (s *OpenCodeService) latestSessionForFolder(folder string) (codingSession, bool) {
	sessions := s.folderSessions(folder)
	if len(sessions) == 0 {
		return codingSession{}, false
	}
	return sessions[0], true
}

// normalizeFolder cleans a user-supplied path: trims quotes/space, expands a
// leading ~ to /root, makes it absolute (relative paths resolve under /root)
// and drops any trailing slash.
func normalizeFolder(p string) string {
	p = strings.TrimSpace(p)
	p = strings.Trim(p, `"'`)
	if p == "" {
		return ""
	}
	switch {
	case p == "~":
		p = "/root"
	case strings.HasPrefix(p, "~/"):
		p = filepath.Join("/root", p[2:])
	case !filepath.IsAbs(p):
		p = filepath.Join("/root", p)
	}
	return filepath.Clean(p)
}

// oneLine collapses whitespace/newlines into single spaces for a compact label.
func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
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
