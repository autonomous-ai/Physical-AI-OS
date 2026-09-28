package claudecode

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// Claude Code's IDENTITY.md uses the same `- **Name:** <value>` card format as
// OpenClaw's (the Go persona migration carries it 1-for-1 on a switch, and the
// CLAUDE.md OS block @imports it into Claude's context), so the identity handling

// identityPath returns the workspace IDENTITY.md location.
func identityPath() string {
	return filepath.Join(claudecodeWorkspaceDir, "IDENTITY.md")
}

// WatchIdentity polls IDENTITY.md in the Claude Code workspace and pushes updated wake
// words to HAL whenever the agent's name changes (e.g. the user says "call yourself
// Noah").
func (s *ClaudeCodeService) WatchIdentity(ctx context.Context) {
	path := identityPath()
	var lastName string
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		name := parseIdentityName(string(data))
		if name == "" || name == lastName {
			continue
		}
		lastName = name
		words := i18n.BuildVoiceWakeWords(name)
		slog.Info("agent renamed, updating wake words", "component", "claudecode", "name", name, "words", words)
		hal.SetVoiceConfig(words)
		i18n.SetDeviceName(name)
	}
}

// UpdateIdentityName rewrites the `**Name:**` line in workspace/IDENTITY.md so
// downstream consumers (parseIdentityName / WatchIdentity → wake words) see the new
// agent name.
// Atomic tmp+rename so a mid-write crash cannot truncate the file.
func (s *ClaudeCodeService) UpdateIdentityName(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("identity name is required")
	}

	path := identityPath()
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}

	existing, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", path, err)
	}

	updated := rewriteIdentityName(string(existing), name)

	tmp, err := os.CreateTemp(dir, ".IDENTITY.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(updated); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	slog.Info("identity name updated", "component", "claudecode", "name", name, "path", path)
	return nil
}

// rewriteIdentityName returns content with the first `**name:**` line's value
// replaced by name.
func rewriteIdentityName(content, name string) string {
	lines := strings.Split(content, "\n")
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), "**name:**")
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**Name:** " + name
		if i+1 < len(lines) && isItalicPlaceholder(lines[i+1]) {
			lines = append(lines[:i+1], lines[i+2:]...)
		}
		return strings.Join(lines, "\n")
	}
	prefix := content
	if prefix != "" && !strings.HasSuffix(prefix, "\n") {
		prefix += "\n"
	}
	return prefix + "- **Name:** " + name + "\n"
}

// isItalicPlaceholder returns true when line is a markdown italic note wrapped in
// `_(...)_` or `*(...)*` (with optional leading whitespace).
func isItalicPlaceholder(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// parseIdentityName extracts the agent name from the `- **Name:** <value>` line in
// IDENTITY.md.
func parseIdentityName(content string) string {
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		lower := strings.ToLower(line)
		idx := strings.Index(lower, "**name:**")
		if idx < 0 {
			continue
		}
		name := strings.TrimSpace(line[idx+len("**name:**"):])
		if i := strings.IndexAny(name, "—-|"); i > 0 {
			name = strings.TrimSpace(name[:i])
		}
		if name != "" {
			return name
		}
	}
	return ""
}
