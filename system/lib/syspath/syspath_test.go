package syspath

import "testing"

// Device contract: every accessor returns its on-device default when its env var is unset ("").
func TestDeviceDefaults(t *testing.T) {
	cases := []struct {
		env  string
		got  func() string
		want string
	}{
		{"CODEX_HOME", CodexHome, "/root/.codex"},
		{"CODEX_PORT", CodexPort, "18792"},
		{"CODEX_WS_TOKEN", CodexWSToken, "autonomous_codex_token"},
		{"OS_AGENT_HOME", AgentHome, "/root"},
		{"OS_AGENT_STATE_PATH", AgentStatePath, "/root/config/agent_state.json"},
		{"OS_BOOTSTRAP_CONFIG", BootstrapConfig, "/root/config/bootstrap.json"},
		{"OS_LOG_FILE", LogFile, "/var/log/os-server.log"},
		{"OS_GELF_SPOOL_DIR", GELFSpoolDir, "/var/lib/autonomous/gelf-spool"},
	}
	for _, c := range cases {
		t.Setenv(c.env, "")
		if got := c.got(); got != c.want {
			t.Errorf("%s unset: got %q, want %q", c.env, got, c.want)
		}
		t.Setenv(c.env, "/tmp/override")
		if got := c.got(); got != "/tmp/override" {
			t.Errorf("%s set: got %q, want the override", c.env, got)
		}
	}
}

// AgentRuntimeHome resolves to /root/.<runtime> on device; codex tracks CODEX_HOME, not OS_AGENT_HOME.
func TestAgentRuntimeHome(t *testing.T) {
	t.Setenv("OS_AGENT_HOME", "")
	t.Setenv("CODEX_HOME", "")
	for _, rt := range []string{"codex", "openclaw", "hermes", "picoclaw", "claudecode", "opencode"} {
		if got, want := AgentRuntimeHome(rt), "/root/."+rt; got != want {
			t.Errorf("%s unset env: got %q, want %q", rt, got, want)
		}
	}

	t.Setenv("CODEX_HOME", "/Users/dev/.codex")
	t.Setenv("OS_AGENT_HOME", "/tmp/state")
	if got, want := AgentRuntimeHome("codex"), "/Users/dev/.codex"; got != want {
		t.Errorf("codex: got %q, want %q — must follow CODEX_HOME, not OS_AGENT_HOME", got, want)
	}
	if got, want := AgentRuntimeHome("openclaw"), "/tmp/state/.openclaw"; got != want {
		t.Errorf("openclaw: got %q, want %q", got, want)
	}
}

// Backend reporting stays on unless explicitly "off" (make os-dev); typos fail safe to on.
func TestBackendUplink(t *testing.T) {
	for _, c := range []struct {
		env  string
		want bool
	}{
		{"", true},
		{"on", true},
		{"off", false},
		{"anything", true},
	} {
		t.Setenv("OS_BACKEND_UPLINK", c.env)
		if got := BackendUplink(); got != c.want {
			t.Errorf("OS_BACKEND_UPLINK=%q: got %v, want %v", c.env, got, c.want)
		}
	}
}
