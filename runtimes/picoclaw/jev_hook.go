package picoclaw

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/runtimes/hermes"
	"go.autonomous.ai/os/system/server/config"
)

// jevEnabled is the runtime build switch. Enable only after native validation.
const jevEnabled = false

//go:embed resources/hooks/jev/hook.py
var jevHookScript []byte

// ensureJevHook reconciles assets and registration using the runtime build switch.
func (s *PicoclawService) ensureJevHook() (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()
	return syncJevHook(picoclawConfigPath(), config.Path(), filepath.Join(filepath.Dir(picoclawConfigPath()), "hooks", "jev"))
}

func syncJevHook(cfgPath, osConfigPath, dir string) (bool, error) {
	if !jevEnabled {
		return disableExistingJevHook(cfgPath)
	}
	cfg, err := readPicoclawConfig(cfgPath)
	if err != nil {
		return false, err
	}
	if !filepath.IsAbs(osConfigPath) {
		return false, fmt.Errorf("Jev config path must be absolute")
	}
	before, _ := json.Marshal(cfg)
	assets, err := hermes.JevSelectorAssets()
	if err != nil {
		return false, err
	}
	assets["router.py"] = bytes.ReplaceAll(assets["router.py"], []byte("[hermes-jev]"), []byte("[picoclaw-jev]"))
	assets["hook.py"] = jevHookScript
	assets["__init__.py"] = []byte{}
	assets["os-config-path.json"], _ = json.Marshal(map[string]string{"config_path": osConfigPath})
	if err := os.MkdirAll(dir, 0700); err != nil {
		return false, err
	}
	changed := false
	for name, data := range assets {
		updated, err := writePicoFileIfChanged(filepath.Join(dir, name), data, 0600)
		if err != nil {
			return changed, err
		}
		changed = changed || updated
	}
	applyJevHook(cfg, filepath.Join(dir, "hook.py"))
	after, _ := json.Marshal(cfg)
	if !bytes.Equal(before, after) {
		if err := writePicoclawConfig(cfgPath, cfg); err != nil {
			return changed, err
		}
		changed = true
	}
	return changed, nil
}

// The runtime build switch owns the Jev registration.
// The observer owns the global hook gate; Jev must never enable it itself.
func applyJevHook(cfg map[string]any, path string) {
	hooks := ensurePicoMap(cfg, "hooks")
	processes := ensurePicoMap(hooks, "processes")
	processes["jev"] = map[string]any{"enabled": jevEnabled, "transport": "stdio", "command": []any{"python3", path}, "intercept": []any{"before_llm"}}
}

// Disabled builds install nothing. Only retire a registration from an older build.
func disableExistingJevHook(cfgPath string) (bool, error) {
	if _, err := os.Stat(cfgPath); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("stat existing Jev hook config: %w", err)
	}
	cfg, err := readPicoclawConfig(cfgPath)
	if err != nil {
		return false, err
	}
	hooks, _ := cfg["hooks"].(map[string]any)
	processes, _ := hooks["processes"].(map[string]any)
	entry, _ := processes["jev"].(map[string]any)
	if entry == nil || entry["enabled"] == false {
		return false, nil
	}
	entry["enabled"] = false
	if err := writePicoclawConfig(cfgPath, cfg); err != nil {
		return false, fmt.Errorf("disable existing Jev hook: %w", err)
	}
	return true, nil
}
