package openclaw

import (
	"bytes"
	"embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/server/config"
)

// jevEnabled is the runtime build switch. Enable only after native validation.
const jevEnabled = false

const jevPluginID = "autonomous-jev"

//go:embed plugins/jev/index.mjs plugins/jev/native.mjs plugins/jev/openclaw.plugin.json plugins/jev/package.json
var jevPluginAssets embed.FS

// ensureJevPlugin installs and configures the native plugin using the build switch.
func (s *OpenclawService) ensureJevPlugin() (bool, error) {
	if s.config == nil || s.config.AgentRuntime == "remote" {
		return false, nil
	}
	return syncJevPlugin(s.config.OpenclawConfigDir, config.Path())
}

func syncJevPlugin(home, configPath string) (bool, error) {
	if !jevEnabled {
		return disableExistingJevPlugin(home)
	}
	if !filepath.IsAbs(configPath) {
		return false, fmt.Errorf("Jev OS config path must be absolute")
	}
	if _, err := os.Stat(filepath.Join(home, "openclaw.json")); os.IsNotExist(err) {
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("stat OpenClaw config: %w", err)
	}
	dir := filepath.Join(home, "extensions", jevPluginID)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return false, fmt.Errorf("create Jev plugin directory: %w", err)
	}
	changed := false
	write := func(name string, data []byte) error {
		file := filepath.Join(dir, name)
		if old, err := os.ReadFile(file); err == nil && bytes.Equal(old, data) {
			return nil
		}
		if err := atomicWriteFile(file, data, 0600); err != nil {
			return fmt.Errorf("write Jev plugin asset: %w", err)
		}
		changed = true
		return chownRuntimeUserIfRoot(file, openclawRuntimeUser)
	}
	for _, name := range []string{"index.mjs", "native.mjs", "openclaw.plugin.json", "package.json"} {
		data, err := jevPluginAssets.ReadFile("plugins/jev/" + name)
		if err != nil {
			return changed, fmt.Errorf("read Jev plugin asset: %w", err)
		}
		if err := write(name, data); err != nil {
			return changed, err
		}
	}
	data, err := json.Marshal(map[string]string{"config_path": configPath})
	if err != nil {
		return changed, fmt.Errorf("marshal Jev config pointer: %w", err)
	}
	if err := write("os-config-path.json", data); err != nil {
		return changed, err
	}
	file := filepath.Join(home, "openclaw.json")
	original, err := os.ReadFile(file)
	if err != nil {
		return changed, fmt.Errorf("read OpenClaw Jev config: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(original, &cfg); err != nil {
		return changed, fmt.Errorf("parse OpenClaw Jev config: %w", err)
	}
	if cfg == nil {
		return changed, fmt.Errorf("OpenClaw config must be an object")
	}
	ensureMap := func(parent map[string]any, key string) (map[string]any, error) {
		if value, exists := parent[key]; exists {
			if object, ok := value.(map[string]any); ok && object != nil {
				return object, nil
			}
			return nil, fmt.Errorf("OpenClaw %s must be an object", key)
		}
		object := map[string]any{}
		parent[key] = object
		return object, nil
	}
	plugins, err := ensureMap(cfg, "plugins")
	if err != nil {
		return changed, err
	}
	entries, err := ensureMap(plugins, "entries")
	if err != nil {
		return changed, err
	}
	entry, err := ensureMap(entries, jevPluginID)
	if err != nil {
		return changed, err
	}
	pluginConfig, err := ensureMap(entry, "config")
	if err != nil {
		return changed, err
	}
	if entry["enabled"] != jevEnabled || pluginConfig["enabled"] != jevEnabled {
		entry["enabled"] = jevEnabled
		pluginConfig["enabled"] = jevEnabled
		data, err := json.MarshalIndent(cfg, "", "  ")
		if err != nil {
			return changed, err
		}
		if err := atomicWriteFile(file, data, 0600); err != nil {
			return changed, fmt.Errorf("write OpenClaw Jev config: %w", err)
		}
		changed = true
		if err := chownRuntimeUserIfRoot(file, openclawRuntimeUser); err != nil {
			return changed, err
		}
	}
	return changed, nil
}

// stripJevPreload restores the original request for run correlation. The native
// hook prepends a single JSON envelope to the persisted user message. Malformed
// or oversized prefixes are left untouched; this function grants no authority.
func stripJevPreload(message string) string {
	const start = "[jev-skill-preload]\n"
	const end = "\n[/jev-skill-preload]\n\n"
	if !strings.HasPrefix(message, start) {
		return message
	}
	i := strings.Index(message[len(start):], end)
	if i < 0 || i > 65536 {
		return message
	}
	var payload struct {
		Version int    `json:"version"`
		Skill   string `json:"skill"`
		Path    string `json:"path"`
		Content string `json:"content"`
	}
	if json.Unmarshal([]byte(message[len(start):len(start)+i]), &payload) != nil || payload.Version != 1 || payload.Skill == "" || !filepath.IsAbs(payload.Path) || payload.Content == "" {
		return message
	}
	return message[len(start)+i+len(end):]
}

// Disabled builds install nothing. Only retire a registration from an older build.
func disableExistingJevPlugin(home string) (bool, error) {
	file := filepath.Join(home, "openclaw.json")
	raw, err := os.ReadFile(file)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("read existing Jev registration: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse existing Jev registration: %w", err)
	}
	plugins, _ := cfg["plugins"].(map[string]any)
	entries, _ := plugins["entries"].(map[string]any)
	entry, _ := entries[jevPluginID].(map[string]any)
	if entry == nil {
		return false, nil
	}
	pluginConfig, _ := entry["config"].(map[string]any)
	changed := entry["enabled"] != false
	entry["enabled"] = false
	if pluginConfig != nil && pluginConfig["enabled"] != false {
		pluginConfig["enabled"] = false
		changed = true
	}
	if !changed {
		return false, nil
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal disabled Jev registration: %w", err)
	}
	if err := atomicWriteFile(file, data, 0600); err != nil {
		return false, fmt.Errorf("disable existing Jev registration: %w", err)
	}
	return true, chownRuntimeUserIfRoot(file, openclawRuntimeUser)
}
