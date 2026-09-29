package gatewayd

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"path/filepath"

	jev "go.autonomous.ai/os/system/lib/jevskills"
)

// jevEnabled is the runtime build switch. Enable only after native validation.
const jevEnabled = false

func newPreloader(cfg Config) func(context.Context, string) string {
	if !cfg.JevEnabled {
		return nil
	}
	router := jev.New(jev.Options{Runtime: "opencode", ConfigPath: cfg.JevConfigPath,
		SkillsDir: filepath.Join(cfg.Home, ".config", "opencode", "skills"), Disabled: !cfg.JevEnabled, AdditionalRoots: func() []string { return nativeSkillDirs(cfg) },
		Allowed: func() bool { return nativePreloadAllowed(cfg) }})
	return router.Context
}

// prepareSkill makes a turn-local copy.
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
	// Linux limits each argv string to 128 KiB.
	if len(p.preload)+2+len(p.Content) >= 128<<10 {
		log.Printf("[opencode-jev] outcome=skipped reason=argument_budget")
		return p.Content
	}
	return p.preload + "\n\n" + p.Content
}

// Native policy is authoritative.
func nativePreloadAllowed(cfg Config) bool {
	for key, expected := range map[string]string{"OPENCODE_CONFIG": filepath.Join(cfg.Home, ".config", "opencode", "opencode.json"), "OPENCODE_CONFIG_DIR": filepath.Join(cfg.Home, ".config", "opencode"), "XDG_CONFIG_HOME": filepath.Join(cfg.Home, ".config")} {
		if value := os.Getenv(key); value != "" {
			actual, err := filepath.Abs(value)
			want, wantErr := filepath.Abs(expected)
			if err != nil || wantErr != nil || actual != want {
				return false
			}
		}
	}
	if os.Getenv("OPENCODE_CONFIG_CONTENT") != "" {
		return false
	}

	paths := []string{filepath.Join(cfg.Home, ".config", "opencode", "opencode.json"), filepath.Join(cfg.Home, ".config", "opencode", "opencode.jsonc")}
	dir, err := filepath.Abs(cfg.Workspace)
	if err != nil {
		return false
	}
	for {
		// Covered static roots are scanned together; unknown roots stay native-owned.
		for _, catalog := range []string{filepath.Join(dir, ".opencode", "skills"), filepath.Join(dir, ".claude", "skills"), filepath.Join(dir, ".agents", "skills")} {
			covered := false
			for _, root := range nativeSkillDirs(cfg) {
				if filepath.Clean(catalog) == filepath.Clean(root) {
					covered = true
					break
				}
			}
			if covered {
				continue
			}
			if _, err := os.Lstat(catalog); err == nil || !os.IsNotExist(err) {
				return false
			}
		}
		paths = append(paths, filepath.Join(dir, "opencode.json"), filepath.Join(dir, "opencode.jsonc"), filepath.Join(dir, ".opencode", "opencode.json"), filepath.Join(dir, ".opencode", "opencode.jsonc"))
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
		for _, key := range []string{"permission", "skills", "agent", "tools"} {
			if _, exists := policy[key]; exists {
				return false
			}
		}
	}
	return true
}

// nativeSkillDirs lists only documented static discovery roots. Plugin registries
// and dynamic invocation remain owned by the runtime.
func nativeSkillDirs(cfg Config) []string {
	roots := []string{filepath.Join(cfg.Home, ".config", "opencode", "skills"), filepath.Join(cfg.Home, ".claude", "skills"), filepath.Join(cfg.Home, ".agents", "skills")}
	return append(roots, jev.ProjectSkillDirs(cfg.Workspace, ".opencode", ".claude", ".agents")...)
}
