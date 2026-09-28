package gatewayd

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"

	jev "go.autonomous.ai/os/system/lib/jevskills"
)

// newPreloader belongs to the runtime bridge, so all OS-managed chat channels
// share the same bounded selector without an additional OS dispatch decision.
// jevEnabled is the runtime build switch. Enable only after native validation.
const jevEnabled = false

func newPreloader(cfg Config) func(context.Context, string) string {
	if !cfg.JevEnabled {
		return nil
	}
	router := jev.New(jev.Options{Runtime: "claudecode", ConfigPath: cfg.JevConfigPath,
		SkillsDir: filepath.Join(cfg.Home, ".claude", "skills"), Disabled: !cfg.JevEnabled,
		Eligible: func(_ string, _ map[string]any) bool { return nativePreloadAllowed(cfg) }})
	return router.Context
}

// prepareSkill makes a turn-local copy. Retries reuse the prepared payload;
// neither the original request nor a persistent system prompt is rewritten.
func (s *Server) prepareSkill(ctx context.Context, p turnPayload) turnPayload {
	if s.preloadContext == nil || p.preloadChecked {
		return p
	}
	p.preloadChecked = true
	if s.preloadContext == nil || (p.Source != "" && p.Source != "user") || len(p.Attachments) > 0 {
		return p
	}
	if ctx == nil {
		ctx = context.Background()
	}
	p.preload = s.preloadContext(ctx, p.Content)
	return p
}

func (p turnPayload) promptWithSkill() string {
	if p.preload == "" {
		return p.Content
	}
	return p.preload + "\n\n" + p.Content
}

// Native policy is authoritative. A custom skill/permission policy is left to
// the native loader instead of approximating its inheritance or wildcard rules.
// Unreadable settings also abstain. Files are rechecked for each decision.
func nativePreloadAllowed(cfg Config) bool {
	// Default-path overrides name the same policy files checked below. Other
	// roots are left to native discovery instead of selecting from a stale catalog.
	for key, expected := range map[string]string{"CLAUDE_CONFIG_DIR": filepath.Join(cfg.Home, ".claude")} {
		if value := os.Getenv(key); value != "" {
			actual, err := filepath.Abs(value)
			want, wantErr := filepath.Abs(expected)
			if err != nil || wantErr != nil || actual != want {
				return false
			}
		}
	}

	paths := []string{filepath.Join(cfg.Home, ".claude", "settings.json"), filepath.Join(cfg.Home, ".claude", "settings.local.json"), "/etc/claude-code/managed-settings.json"}
	dir, err := filepath.Abs(cfg.Workspace)
	if err != nil {
		return false
	}
	for {
		// A project catalog may override the installed skill of the same name.
		// Leave its discovery and precedence to the runtime's native loader.
		for _, catalog := range []string{filepath.Join(dir, ".claude", "skills")} {
			if filepath.Clean(catalog) == filepath.Clean(filepath.Join(cfg.Home, ".claude", "skills")) {
				continue
			}
			if _, err := os.Lstat(catalog); err == nil || !os.IsNotExist(err) {
				return false
			}
		}
		paths = append(paths, filepath.Join(dir, ".claude", "settings.json"), filepath.Join(dir, ".claude", "settings.local.json"))
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	for _, path := range paths {
		data, err := jev.ReadPolicyFile(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return false
		}
		policy := map[string]any{}
		if err := json.Unmarshal(data, &policy); err != nil || policy == nil {
			return false
		}
		for _, key := range []string{"permissions", "hooks"} {
			if _, exists := policy[key]; exists {
				return false
			}
		}
	}
	return true
}
