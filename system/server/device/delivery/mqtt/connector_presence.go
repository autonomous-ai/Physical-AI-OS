package mqtthandler

import (
	"log/slog"
	"path/filepath"
)

// legacyConnectorsFile is the shared connectors map that firmware before the
// per-connector token files wrote "default"-routed connectors into. Nothing
// writes it any more (connector.set.<code> and connector.remove.<code> only
// touch <code>_access_tokens.json), but it was never migrated, and the
// connectors skill still reads it as "connected" — so a connector that lives
// only there is one the agent can actually use.
const legacyConnectorsFile = "connectors.json"

// connectorPresence is the one question connectorInstalled asks a connector
// writer: does your store hold an entry for this code? Both the generic
// connectorWriter and the special mcpConnectorWriter implement it lock-free
// (see connectorWriter.hasEntry for why that matters and why it is safe).
type connectorPresence interface {
	hasEntry(connector string) (bool, error)
}

// legacyConnectorsPath is <OpenclawConfigDir>/workspace/configs/connectors.json,
// the same configs dir every connector writer (and accessTokensPath) uses.
func (h *DeviceMQTTHandler) legacyConnectorsPath() string {
	return filepath.Join(h.config.OpenclawConfigDir, "workspace", "configs", legacyConnectorsFile)
}

// connectorInstalled is the production schedule.ConnectorChecker: it reports
// whether connector code currently has credentials on this device, which is
// what a template task's Schedule.Requires is checked against at fire time.
//
// "Installed" means an entry keyed by code exists in the store connector.set
// writes. That is resolved through connectorWriterFor — the SAME routing
// connector.set / connector.remove / the local PAT endpoint use — rather than
// by guessing a filename here, because the store differs per connector: the
// generic writer keeps <code>_access_tokens.json (and connector.remove leaves
// that file behind with the entry deleted), while a special writer such as
// figma-api's stdio MCP writer owns its own token file and deletes it outright.
// Each answers through connectorPresence.hasEntry, which reads WITHOUT the
// writer's mutex: connector.set holds that mutex across a gateway restart
// (30-60s), and this runs on the runner tick and the schedule.run MQTT
// handler, which must not stall behind it. The files are replaced atomically,
// so the read sees either the state before a connector.set or after it.
//
// Deliberately NOT consulted: the provider-keyed access_tokens.json (oauth.set).
// It is keyed by provider ("google"), not connector code, and a provider token
// says nothing about which services it covers — the connectors skill forbids
// reading "google" as "gmail is connected", and so does this.
//
// The answer is false ONLY on positive evidence of absence (see
// schedule.ConnectorChecker): every store was read cleanly and none has the
// entry. A store that cannot be read, or a writer that cannot be asked, fails
// OPEN — the task runs exactly as it did before the guard existed, instead of
// being skipped with a "missing connector" reason that would send the user to
// reconnect something that may be connected.
//
// Reads only h.config, h.connectorWriter and h.specialConnectorWriters: all
// set once in ProvideDeviceMQTTHandler, before the Runner that calls this is
// built, and never mutated afterwards (see the note there on binding to the
// local h).
func (h *DeviceMQTTHandler) connectorInstalled(code string) bool {
	// Outside the charset every writer enforces, no connector.set could ever
	// have installed this code — and it must never reach a filesystem path.
	// Logged at Warn because the code came from the backend's requires list:
	// the task will now be skipped on every occurrence, and its summary
	// ("missing connector: <code>") reads like an ordinary disconnected
	// service. Only this line shows the code itself is malformed.
	if !validConnectorCode.MatchString(code) {
		slog.Warn("schedule: required connector code is invalid, treating as not installed",
			"component", "mqtt", "connector", code)
		return false
	}

	if w := h.connectorWriterFor(code); w != nil {
		p, ok := w.(connectorPresence)
		if !ok {
			// A writer this check cannot ask. Unknown, so fail open.
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
