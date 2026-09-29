package codex

import (
	"os"
	"strings"
	"testing"
)

// The device (OrangePi) contract: with no env set, every path-derived value
// must equal the literal it replaced.
func TestDeviceDefaultsUnchanged(t *testing.T) {
	for _, k := range []string{"CODEX_HOME", "CODEX_PORT", "CODEX_WS_TOKEN", "OS_AGENT_HOME"} {
		if v := os.Getenv(k); v != "" {
			t.Skipf("%s=%s set in the environment — defaults not observable", k, v)
		}
	}

	cases := []struct{ name, got, want string }{
		{"codexHome", codexHome, "/root/.codex"},
		{"codexWorkspaceDir", codexWorkspaceDir, "/root/.codex/workspace"},
		{"codexSkillsDir", codexSkillsDir, "/root/.codex/skills"},
		{"codexUserAgentsMD", codexUserAgentsMD, "/root/.codex/AGENTS.md"},
		{"codexConfigTOML", codexConfigTOML, "/root/.codex/config.toml"},
		{"codexEnvFile", codexEnvFile, "/root/.codex/.env"},
		{"codexDataDir", codexDataDir, "/root/.codex"},
		{"codexSessionsDirDefault", codexSessionsDirDefault, "/root/.codex/sessions"},
		{"telegramOffsetFile", telegramOffsetFile, "/root/.codex/telegram_offset.json"},
		{"telegramTargetsFile", telegramTargetsFile, "/root/.codex/telegram_targets.json"},
		{"codingSelFileDefault", codingSelFileDefault, "/root/.codex/telegram_coding.json"},
		{"WSURL", WSURL, "ws://127.0.0.1:18792/codex/ws/"},
		{"Token", Token, "autonomous_codex_token"},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("%s = %q, want %q", c.name, c.got, c.want)
		}
	}

	// The prompt blocks name the skills dir by absolute path — the text and the
	// dir must not be able to drift apart.
	for _, want := range []string{
		"`/root/.codex/skills/<name>/SKILL.md`",
		"`/root/.codex/skills/connectors/SKILL.md`",
	} {
		if !strings.Contains(agentsMDBlock, want) {
			t.Errorf("agentsMDBlock missing %s", want)
		}
	}
	if !strings.Contains(userAgentsMDBlock, "`/root/.codex/skills/connectors/SKILL.md`") {
		t.Error("userAgentsMDBlock missing the absolute connectors skill path")
	}

	if got := normalizeFolder("~/myapp"); got != "/root/myapp" {
		t.Errorf("normalizeFolder(~/myapp) = %q, want /root/myapp", got)
	}
	if got := normalizeFolder("myapp"); got != "/root/myapp" {
		t.Errorf("normalizeFolder(myapp) = %q, want /root/myapp", got)
	}
}
