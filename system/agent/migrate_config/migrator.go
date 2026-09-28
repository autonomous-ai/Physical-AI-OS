// Package migrateconfig carries LLM provider config (API key + base URL) between agent runtimes
// on a switch via one read and one write adapter per runtime (hub-and-spoke).
package migrateconfig

import (
	"fmt"

	"go.autonomous.ai/os/system/lib/syspath"
)

// LLMConfig is the runtime-neutral LLM provider config (model selection is runtime-specific, excluded).
type LLMConfig struct {
	APIKey  string
	BaseURL string
}

// Empty reports whether the config carries no useful data.
func (c LLMConfig) Empty() bool {
	return c.APIKey == "" && c.BaseURL == ""
}

// Runtime identifies an agent backend whose LLM config lives on-device.
type Runtime string

const (
	RuntimeOpenclaw   Runtime = "openclaw"
	RuntimeHermes     Runtime = "hermes"
	RuntimePicoclaw   Runtime = "picoclaw"
	RuntimeCodex      Runtime = "codex"
	RuntimeClaudeCode Runtime = "claudecode"
	RuntimeOpenCode   Runtime = "opencode"
)

// runtimeAdapter is the read/write surface every migratable runtime implements.
type runtimeAdapter interface {
	runtime() Runtime
	read(opts Options) (LLMConfig, error)
	write(cfg LLMConfig, opts Options) error
}

var adapters = map[Runtime]runtimeAdapter{
	RuntimeOpenclaw:   openclawAdapter{},
	RuntimeHermes:     hermesAdapter{},
	RuntimePicoclaw:   picoclawAdapter{},
	RuntimeCodex:      codexAdapter{},
	RuntimeClaudeCode: claudecodeAdapter{},
	RuntimeOpenCode:   opencodeAdapter{},
}

// CanMigrate reports whether a runtime has a registered config adapter.
func CanMigrate(r Runtime) bool {
	_, ok := adapters[r]
	return ok
}

// Options holds the on-device paths for each runtime's config files.
type Options struct {
	OpenclawConfigDir string // e.g. /root/.openclaw
	HermesRoot        string // e.g. /root/.hermes
	PicoclawConfigDir string // e.g. /root/.picoclaw
	CodexHome         string // e.g. /root/.codex (config.toml + .env)
	ClaudecodeDir     string // e.g. /root/.claudecode
	OpenCodeHome      string // e.g. /root/.opencode (.env)
	OpenCodeConfig    string // e.g. /root/.config/opencode/opencode.json
}

func DefaultOptions(openclawConfigDir, hermesRoot string) Options {
	if openclawConfigDir == "" {
		openclawConfigDir = "/root/.openclaw"
	}
	if hermesRoot == "" {
		hermesRoot = "/root/.hermes"
	}
	return Options{
		OpenclawConfigDir: openclawConfigDir,
		HermesRoot:        hermesRoot,
		PicoclawConfigDir: "/root/.picoclaw",
		CodexHome:         syspath.CodexHome(),
		ClaudecodeDir:     "/root/.claudecode",
		OpenCodeHome:      "/root/.opencode",
		OpenCodeConfig:    "/root/.config/opencode/opencode.json",
	}
}

// ReadConfig reads the LLM config from the source runtime's native on-disk files.
// Returns an empty LLMConfig (and no error) when the source has no config to carry.
func ReadConfig(from Runtime, opts Options) (LLMConfig, error) {
	src, ok := adapters[from]
	if !ok {
		return LLMConfig{}, fmt.Errorf("migrateconfig: no adapter for source runtime %q", from)
	}
	cfg, err := src.read(opts)
	if err != nil {
		return LLMConfig{}, fmt.Errorf("migrateconfig: read %s: %w", from, err)
	}
	return cfg, nil
}

// WriteConfig writes cfg to the destination runtime's native files. Sync config.json first so
// ensureProviderConfig falls back to fresh values if this fails.
func WriteConfig(to Runtime, cfg LLMConfig, opts Options) error {
	dst, ok := adapters[to]
	if !ok {
		return fmt.Errorf("migrateconfig: no adapter for destination runtime %q", to)
	}
	if err := dst.write(cfg, opts); err != nil {
		return fmt.Errorf("migrateconfig: write %s: %w", to, err)
	}
	return nil
}
