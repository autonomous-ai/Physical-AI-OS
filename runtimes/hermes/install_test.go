package hermes

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/runtimereg"
)

// The presync hook must be registered so os-server materializes it on switch.
func TestPresyncRegistered(t *testing.T) {
	script, ok := runtimereg.GetPresync(domain.AgentRuntimeHermes)
	if !ok || len(script) == 0 {
		t.Fatal("hermes presync not registered with runtimereg")
	}
}

// presync owns config.yaml model wiring: static provider structure plus dynamic per-device values.
func TestPresyncOwnsConfigStructure(t *testing.T) {
	s := string(PresyncScript)
	for _, want := range []string{
		`.model.provider = "custom:autonomous"`,
		`.custom_providers[0].api_mode = "anthropic_messages"`,
		`.custom_providers[0].name     = "autonomous"`,
		`.model.default = "Auto-AI"`,
		`yq -i '.model = {}'`,
		"AUTONOMOUS_API_KEY",
		`.custom_providers[0].models["Auto-AI"].prompt_caching = true`,
		`.prompt_caching.cache_ttl = "1h"`,
	} {
		if !strings.Contains(s, want) {
			t.Errorf("presync.sh missing %q — config structure/sync incomplete", want)
		}
	}
}

// presync must restore OpenClaw-imported skills (claw migrate) when missing after a factory reset.
func TestPresyncRestoresSkills(t *testing.T) {
	s := string(PresyncScript)
	if !strings.Contains(s, "claw migrate") {
		t.Error("presync.sh must run `claw migrate` to restore openclaw-imported skills")
	}
	if !strings.Contains(s, "openclaw-imports") {
		t.Error("presync.sh must guard the skill restore on the openclaw-imports dir")
	}
	// install.sh must NOT also INVOKE claw migrate (single owner = presync; otherwise a reset fix in presync would drift from a stale copy in install.sh).
	if strings.Contains(string(InstallScript), "claw migrate --preset") {
		t.Error("install.sh still invokes claw migrate — must delegate skill import to presync.sh")
	}
}

// install.sh must NOT carry its own config.yaml patch or a presync heredoc anymore — both are owned by presync.sh / os-server materialization.
func TestInstallDelegatesConfigToPresync(t *testing.T) {
	s := string(InstallScript)
	if strings.Contains(s, ".custom_providers = [") {
		t.Error("install.sh still patches .custom_providers — must delegate to presync.sh")
	}
	if !strings.Contains(s, "runtime-hermes-presync") {
		t.Error("install.sh must invoke the materialized presync hook")
	}
}
