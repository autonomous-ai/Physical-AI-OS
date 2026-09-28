package hermes

import (
	"bytes"
	"embed"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/goccy/go-yaml"
	"go.autonomous.ai/os/system/server/config"
)

const jevPluginName = "jev"

//go:embed plugins/jev/__init__.py plugins/jev/router.py plugins/jev/preload.py plugins/jev/plugin.yaml
var jevPluginFiles embed.FS

// JevSelectorAssets shares the bounded Python selector with native runtime hooks.
// Callers supply their own eligible catalog and safe skill loader.
func JevSelectorAssets() (map[string][]byte, error) {
	assets := make(map[string][]byte)
	for _, name := range []string{"router.py", "preload.py"} {
		data, err := jevPluginFiles.ReadFile("plugins/jev/" + name)
		if err != nil {
			return nil, fmt.Errorf("read Jev selector asset: %w", err)
		}
		assets[name] = data
	}
	return assets, nil
}

// ensureJevPlugin reconciles existing devices on OS startup, without invoking
// installers or adding a gateway restart reason. Remote Hermes is host-owned.
func (s *HermesService) ensureJevPlugin() error {
	if s.config != nil && s.config.AgentRuntime == "remote" {
		return nil
	}
	u, err := url.Parse(BaseURL)
	if err != nil {
		return fmt.Errorf("parse Hermes endpoint: %w", err)
	}
	ip := net.ParseIP(u.Hostname())
	if u.Hostname() != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return nil
	}
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()
	changed, err := syncJevPlugin(hermesHome, config.Path())
	if err != nil {
		return err
	}
	if changed {
		slog.Info("Hermes Jev plugin updated; gateway restart required to load updated code (not requested)", "component", "hermes", "plugin", jevPluginName)
	}
	return nil
}

// syncJevPlugin owns its assets, allow-list entry and unset hook spill default. A user
// deny-list takes precedence. The sidecar contains a path, never credentials.
func syncJevPlugin(home, configPath string) (bool, error) {
	yamlPath := filepath.Join(home, "config.yaml")
	if _, err := os.Stat(yamlPath); os.IsNotExist(err) {
		return false, nil // Hermes has not been installed on this host.
	} else if err != nil {
		return false, fmt.Errorf("stat Hermes config: %w", err)
	}
	cfg, err := readHermesConfig(yamlPath)
	if err != nil {
		return false, err
	}
	if cfg == nil {
		cfg = map[string]any{}
	}
	if !filepath.IsAbs(configPath) {
		return false, fmt.Errorf("Jev config path must be absolute")
	}
	dir := filepath.Join(home, "plugins", jevPluginName)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return false, fmt.Errorf("create Jev plugin directory: %w", err)
	}
	entries, err := jevPluginFiles.ReadDir("plugins/jev")
	if err != nil {
		return false, fmt.Errorf("read embedded Jev plugin: %w", err)
	}
	changed := false
	for _, entry := range entries {
		if entry.IsDir() || strings.HasPrefix(entry.Name(), "test_") {
			continue
		}
		data, err := jevPluginFiles.ReadFile("plugins/jev/" + entry.Name())
		if err != nil {
			return changed, fmt.Errorf("read Jev asset: %w", err)
		}
		updated, err := writeJevAsset(filepath.Join(dir, entry.Name()), data)
		changed = changed || updated
		if err != nil {
			return changed, err
		}
	}
	data, err := json.Marshal(map[string]string{"config_path": configPath})
	if err != nil {
		return changed, fmt.Errorf("encode Jev config path: %w", err)
	}
	updated, err := writeJevAsset(filepath.Join(dir, "os-config-path.json"), data)
	changed = changed || updated
	if err != nil {
		return changed, err
	}
	plugins, ok := cfg["plugins"].(map[string]any)
	if cfg["plugins"] != nil && !ok {
		return changed, fmt.Errorf("Hermes plugins config must be a mapping")
	}
	if plugins == nil {
		plugins = map[string]any{}
	}
	alreadyEnabled := false
	// Do not override an operator disabling the plugin in Hermes itself.
	for _, key := range []string{"disabled", "enabled"} {
		values, ok := plugins[key].([]any)
		if plugins[key] != nil && !ok {
			return changed, fmt.Errorf("Hermes plugins.%s must be a list", key)
		}
		for _, value := range values {
			if name, ok := value.(string); ok && name == jevPluginName {
				if key == "disabled" {
					return changed, nil
				}
				alreadyEnabled = true
			}
		}
	}
	configChanged, err := setJevHookSpillDefault(cfg)
	if err != nil {
		return changed, err
	}
	if !alreadyEnabled {
		enabled, _ := plugins["enabled"].([]any)
		plugins["enabled"] = append(enabled, jevPluginName)
		cfg["plugins"] = plugins
		configChanged = true
	}
	if !configChanged {
		return changed, nil
	}
	yamlData, err := yaml.Marshal(cfg)
	if err != nil {
		return changed, fmt.Errorf("encode Hermes plugin config: %w", err)
	}
	info, err := os.Stat(yamlPath)
	if err != nil {
		return changed, fmt.Errorf("stat Hermes plugin config: %w", err)
	}
	if err := atomicWriteFile(yamlPath, yamlData, info.Mode().Perm()); err != nil {
		return changed, fmt.Errorf("enable managed Jev plugin: %w", err)
	}
	return true, nil
}

func writeJevAsset(path string, data []byte) (bool, error) {
	current, err := os.ReadFile(path)
	if err == nil && bytes.Equal(current, data) {
		return false, nil
	}
	if err != nil && !os.IsNotExist(err) {
		return false, fmt.Errorf("read Jev asset: %w", err)
	}
	if err := atomicWriteFile(path, data, 0o600); err != nil {
		return false, fmt.Errorf("write Jev asset: %w", err)
	}
	return true, nil
}

// Hermes spills each hook result above 10,000 characters by default. Allow the
// bounded Jev preload inline; preserve every explicit operator spill setting.
func setJevHookSpillDefault(cfg map[string]any) (bool, error) {
	hooks, ok := cfg["hooks"].(map[string]any)
	if cfg["hooks"] != nil && !ok {
		return false, fmt.Errorf("Hermes hooks config must be a mapping")
	}
	if hooks == nil {
		hooks = map[string]any{}
	}
	spill, ok := hooks["output_spill"].(map[string]any)
	if hooks["output_spill"] != nil && !ok {
		return false, fmt.Errorf("Hermes hooks.output_spill must be a mapping")
	}
	if spill == nil {
		spill = map[string]any{}
	}
	if _, exists := spill["max_chars"]; exists {
		return false, nil
	}
	spill["max_chars"] = 128 * 1024
	hooks["output_spill"] = spill
	cfg["hooks"] = hooks
	return true, nil
}
