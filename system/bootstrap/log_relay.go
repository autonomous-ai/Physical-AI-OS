package bootstrap

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/logger"
	"go.autonomous.ai/os/system/server/config"
)

// deviceConfigPath is os-server's config.json; bootstrap already reads it for
// device_type, agent_runtime and stt_language.
const deviceConfigPath = "/root/config/config.json"

// logRelayRefreshInterval is how often bootstrap re-reads the device key. It
// has no config-change signal of its own, and a first setup saves the key
// while bootstrap is already running.
const logRelayRefreshInterval = time.Minute

// logRelayTarget returns the device id and log-relay credentials from
// config.json, using os-server's own rule (config.GELFRelayCredentials): only
// the device's Autonomous credential, never an owner's own provider.
func logRelayTarget(path string) (deviceID, baseURL, apiKey string, err error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", "", "", fmt.Errorf("read device config: %w", err)
	}
	var cfg config.Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return "", "", "", fmt.Errorf("parse device config: %w", err)
	}
	baseURL, apiKey = cfg.GELFRelayCredentials()
	return strings.TrimSpace(cfg.DeviceID), baseURL, apiKey, nil
}

// RunLogRelay keeps bootstrap's log relay pointed at the device's current id
// and key until ctx ends. Before setup there is no key and records wait in the
// spool; OTA runs during and right after setup, so this is how an update that
// restarts os-server mid-setup becomes visible. logger.EnableGELFRelay is a
// no-op while the target is unchanged.
func RunLogRelay(ctx context.Context) {
	refresh := func() {
		deviceID, baseURL, apiKey, err := logRelayTarget(deviceConfigPath)
		if err != nil {
			return // no config yet (fresh device): keep spooling
		}
		if deviceID != "" {
			logger.SetGELFHost(deviceID)
		}
		logger.EnableGELFRelay(baseURL, apiKey)
	}
	refresh()
	ticker := time.NewTicker(logRelayRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			refresh()
		}
	}
}
