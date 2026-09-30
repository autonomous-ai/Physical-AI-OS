// Package syspath resolves os-server's device paths; each has a device default overridable by one env var.
package syspath

import "os"

// envOr returns the env value for key, or def when unset/empty.
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// CodexHome is Codex's state dir (CODEX_HOME, shared with the gatewayd).
func CodexHome() string { return envOr("CODEX_HOME", "/root/.codex") }

// CodexPort is the loopback port codex-gatewayd listens on.
func CodexPort() string { return envOr("CODEX_PORT", "18792") }

// CodexWSToken is the bearer token os-server sends to the codex bridge.
func CodexWSToken() string { return envOr("CODEX_WS_TOKEN", "autonomous_codex_token") }

// AgentHome is the agent user's home dir.
func AgentHome() string { return envOr("OS_AGENT_HOME", "/root") }

// AgentRuntimeHome is one runtime's state dir (/root/.<runtime> on device).
// Codex must resolve via CodexHome, which gatewayd, presync.sh and HAL all read.
func AgentRuntimeHome(runtime string) string {
	if runtime == "codex" {
		return CodexHome()
	}
	return AgentHome() + "/." + runtime
}

// AgentStatePath records the agent-runtime switch history (persona migration).
func AgentStatePath() string {
	return envOr("OS_AGENT_STATE_PATH", "/root/config/agent_state.json")
}

// BackendUplink reports whether the status ping and MQTT channel may run; only "off" disables it.
// Off-device copies of a device config would otherwise impersonate the real device.
func BackendUplink() bool {
	return envOr("OS_BACKEND_UPLINK", "on") != "off"
}

// LogFile is os-server's rotating log file.
func LogFile() string { return envOr("OS_LOG_FILE", "/var/log/os-server.log") }

// GELFSpoolDir holds log records not shipped yet (first setup has no key or
// internet). Under /var/lib because /var/log is RAM (zram) on devices.
func GELFSpoolDir() string { return envOr("OS_GELF_SPOOL_DIR", "/var/lib/autonomous/gelf-spool") }

// HALLogFile is HAL's rotating log file, read for the web UI's HAL log tab.
func HALLogFile() string { return envOr("OS_HAL_LOG_FILE", "/var/log/hal/server.log") }

// AgentBridgeLog is a file to read agent bridge output from instead of the journal; "" keeps the journal.
func AgentBridgeLog() string { return envOr("OS_AGENT_BRIDGE_LOG", "") }

// BootstrapConfig is the OTA worker's config file (os-server reads only metadata_url).
func BootstrapConfig() string {
	return envOr("OS_BOOTSTRAP_CONFIG", "/root/config/bootstrap.json")
}
