package agent

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// writeDeviceMD writes a minimal ROBOT.md under a temp DEVICES_DIR.
func writeDeviceMD(t *testing.T, deviceType, body string) {
	t.Helper()
	root := t.TempDir()
	t.Setenv("DEVICES_DIR", root)
	dir := filepath.Join(root, deviceType)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "ROBOT.md"), []byte(body), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func TestResolveRuntime_PrefersConfigAgentRuntimeOverDeviceMD(t *testing.T) {
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: openclaw
---
`)
	cfg := &config.Config{AgentRuntime: "claudecode"}

	effective, raw, source := resolveRuntime(cfg)

	if effective != "claudecode" || raw != "claudecode" || source != "config.agent_runtime" {
		t.Fatalf("resolveRuntime = (%q, %q, %q), want (claudecode, claudecode, config.agent_runtime)", effective, raw, source)
	}
}

func TestResolveRuntime_FallsBackToDeviceMDWhenConfigEmpty(t *testing.T) {
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
gateway:
  default: hermes
---
`)
	cfg := &config.Config{}

	effective, raw, source := resolveRuntime(cfg)

	if effective != "hermes" || raw != "hermes" || source != "ROBOT.md gateway.default" {
		t.Fatalf("resolveRuntime = (%q, %q, %q), want (hermes, hermes, ROBOT.md gateway.default)", effective, raw, source)
	}
}

func TestResolveRuntime_DefaultsToOpenclawWhenNothingResolves(t *testing.T) {
	t.Setenv("DEVICE_TYPE", "intern-v2")
	writeDeviceMD(t, "intern-v2", `---
schema: autonomous.device.v1
---
`)
	cfg := &config.Config{}

	effective, _, _ := resolveRuntime(cfg)

	if effective != "openclaw" {
		t.Fatalf("resolveRuntime effective = %q, want openclaw", effective)
	}
}
