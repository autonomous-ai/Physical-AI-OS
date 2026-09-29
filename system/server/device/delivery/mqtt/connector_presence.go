package mqtthandler

import (
	"log/slog"
	"path/filepath"
)

// legacyConnectorsFile is the shared connectors map that firmware before the
// per-connector token files wrote "default"-routed connectors into.
const legacyConnectorsFile = "connectors.json"

// connectorPresence is the one question connectorInstalled asks a connector
// writer: does your store hold an entry for this code?
type connectorPresence interface {
	hasEntry(connector string) (bool, error)
}

// legacyConnectorsPath is <OpenclawConfigDir>/workspace/configs/connectors.json,
// the same configs dir every connector writer (and accessTokensPath) uses.
func (h *DeviceMQTTHandler) legacyConnectorsPath() string {
	return filepath.Join(h.config.OpenclawConfigDir, "workspace", "configs", legacyConnectorsFile)
}

// connectorInstalled is the production schedule.ConnectorChecker: it reports
// whether connector code currently has credentials on this device. Reads are
// lock-free, and an unreadable store fails OPEN (the task runs).
func (h *DeviceMQTTHandler) connectorInstalled(code string) bool {
	// Outside the charset every writer enforces, no connector.set could ever
	// have installed this code — and it must never reach a filesystem path.
	if !validConnectorCode.MatchString(code) {
		slog.Warn("schedule: required connector code is invalid, treating as not installed",
			"component", "mqtt", "connector", code)
		return false
	}

	if w := h.connectorWriterFor(code); w != nil {
		p, ok := w.(connectorPresence)
		if !ok {
			slog.Warn("schedule: connector writer cannot report presence, assuming installed",
				"component", "mqtt", "connector", code)
			return true
		}
		present, err := p.hasEntry(code)
		if err != nil {
			slog.Warn("schedule: connector token file unreadable, assuming installed",
				"component", "mqtt", "connector", code, "error", err)
			return true
		}
		if present {
			return true
		}
	}

	legacy, err := loadConnectorsFile(h.legacyConnectorsPath())
	if err != nil {
		slog.Warn("schedule: legacy connectors.json unreadable, assuming installed",
			"component", "mqtt", "connector", code, "error", err)
		return true
	}
	_, present := legacy.Connectors[code]
	return present
}
