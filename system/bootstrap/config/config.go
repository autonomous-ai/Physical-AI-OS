package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// configPath is the bootstrap worker's own config file.
const configPath = "/root/config/bootstrap.json"

// BootstrapVersion is injected at build time via ldflags.
var BootstrapVersion = "dev"

// Config holds bootstrap OTA worker configuration (stored in bootstrap.json).
type Config struct {
	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	MetadataURL string `json:"metadata_url" yaml:"metadataURL"`
	// SigningPublicKey is the base64 Ed25519 key authorizing OTA metadata; provisioned
	// locally, never accepted from the feed.
	SigningPublicKey string `json:"signing_public_key" yaml:"signingPublicKey"`
	// RollbackVersions maps component -> rolled-back version; only that exact target is skipped.
	RollbackVersions map[string]string `json:"rollback_versions,omitempty" yaml:"rollbackVersions"`
	PollInterval     string            `json:"poll_interval" yaml:"pollInterval"` // e.g. "1h", "10m"
	StateFile        string            `json:"state_file" yaml:"stateFile"`
}

// Default returns operational defaults; MetadataURL is empty until provisioning.
func Default() Config {
	return Config{
		HttpPort:     8080,
		MetadataURL:  "",
		PollInterval: "5m",
		StateFile:    "/root/bootstrap/state.json",
	}
}

// LoadOrDefault overlays bootstrap.json onto Default(); missing/corrupt file yields defaults.
func LoadOrDefault() *Config {
	cfg := Default()
	data, err := os.ReadFile(configPath)
	if err != nil {
		return &cfg
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		d := Default()
		return &d
	}
	return &cfg
}

// Save writes the config to /root/config/bootstrap.json.
func (c *Config) Save() error {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}
	dir := filepath.Dir(configPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	if err := os.WriteFile(configPath, data, 0600); err != nil {
		return fmt.Errorf("write config %s: %w", configPath, err)
	}
	return nil
}
