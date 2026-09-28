package hermes

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// hermesHome is the Hermes data dir (matches runtimes/hermes/install.sh HERMES_DIR and the persona-migration target).
const hermesHome = "/root/.hermes"

// identitySoulHeading marks the identity block the openclaw→hermes migration inlines into SOUL.md (see system/agent/migrate_persona).
const identitySoulHeading = "## Your identity card"

// Match an actual name field, never inline examples in SOUL instructions.
var soulNameLine = regexp.MustCompile(`(?i)^(\s*(?:[-*]\s+)?)\*\*name:\*\*\s*(.*)$`)

// UpdateIdentityName rewrites the agent's name under Hermes by editing the `**Name:**` line in <hermes>/SOUL.md — the file Hermes loads as its identity.
func (s *HermesService) UpdateIdentityName(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("identity name is required")
	}
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	existing, err := os.ReadFile(soulPath)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read %s: %w", soulPath, err)
	}

	updated := rewriteSoulName(string(existing), name)

	dir := filepath.Dir(soulPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, ".SOUL.*.tmp")
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
	if err := os.Rename(tmpPath, soulPath); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	slog.Info("identity name updated", "component", "hermes", "name", name, "path", soulPath)
	return nil
}

// rewriteSoulName returns content with the first `**name:**` line's value replaced by name (preserving the bullet prefix, dropping any trailing description).
func rewriteSoulName(content, name string) string {
	lines := strings.Split(content, "\n")
	for i, line := range lines {
		field := soulNameLine.FindStringSubmatch(line)
		if field == nil {
			continue
		}
		lines[i] = field[1] + "**Name:** " + name
		return strings.Join(lines, "\n")
	}
	prefix := strings.TrimRight(content, "\n")
	if prefix != "" {
		prefix += "\n"
	}
	return prefix + "\n" + identitySoulHeading + "\n\n- **Name:** " + name + "\n"
}

// WatchIdentity polls SOUL.md and pushes updated wake words to HAL + the i18n device name whenever the agent's name changes (e.g. the user says "call yourself Noah").
func (s *HermesService) WatchIdentity(ctx context.Context) {
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	var lastName string
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
		data, err := os.ReadFile(soulPath)
		if err != nil {
			continue
		}
		name := parseSoulName(string(data))
		if name == "" || name == lastName {
			continue
		}
		lastName = name
		words := i18n.BuildVoiceWakeWords(name)
		slog.Info("agent renamed, updating wake words", "component", "hermes", "name", name, "words", words)
		hal.SetVoiceConfig(words)
		i18n.SetDeviceName(name)
	}
}

// parseSoulName extracts the agent name from the `- **Name:** <value>` card line in SOUL.md.
func parseSoulName(content string) string {
	for _, line := range strings.Split(content, "\n") {
		field := soulNameLine.FindStringSubmatch(line)
		if field == nil {
			continue
		}
		name := strings.TrimSpace(field[2])
		if i := strings.IndexAny(name, "—-|"); i > 0 {
			name = strings.TrimSpace(name[:i])
		}
		if name != "" {
			return name
		}
	}
	return ""
}
