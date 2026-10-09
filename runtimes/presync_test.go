package runtimes_test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// Execute the shipped scripts against isolated runtime homes. Only output paths
// are redirected; jq/yq and all configuration mutations are the real tools.
func TestPresyncLLMOwnership(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq required to exercise device presync")
	}
	hostOS := runtime.GOOS
	for _, runtime := range []string{"codex", "claudecode", "opencode", "hermes", "picoclaw"} {
		t.Run(runtime, func(t *testing.T) {
			if runtime == "hermes" || runtime == "picoclaw" {
				if _, err := exec.LookPath("yq"); err != nil {
					t.Skip("Mike Farah yq required to exercise YAML presync")
				}
			}
			dir := t.TempDir()
			home := filepath.Join(dir, "runtime")
			if err := os.MkdirAll(home, 0700); err != nil {
				t.Fatal(err)
			}
			write := func(path, value string) {
				t.Helper()
				if err := os.WriteFile(path, []byte(value), 0600); err != nil {
					t.Fatal(err)
				}
			}
			read := func(path string) string {
				t.Helper()
				b, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				return string(b)
			}
			write(filepath.Join(home, ".openclaw-migrated"), "")
			write(filepath.Join(home, ".env"), "OPENAI_API_KEY=old-proxy\nANTHROPIC_API_KEY=old-proxy\nLLM_API_KEY=old-proxy\n")
			configPath := filepath.Join(dir, "device.json")
			config := map[string]any{"llm_config_mode": "runtime", "llm_model": "os-model", "llm_api_key": "os-key", "llm_base_url": "https://example.test/v1", "telegram_bot_token": "channel-token"}
			save := func() {
				t.Helper()
				b, err := json.Marshal(config)
				if err != nil {
					t.Fatal(err)
				}
				write(configPath, string(b))
			}
			save()
			testPath := os.Getenv("PATH")
			if hostOS == "darwin" {
				// Device scripts use GNU sed's -i; BSD sed needs an empty backup suffix.
				bin := filepath.Join(dir, "bin")
				if err := os.Mkdir(bin, 0700); err != nil {
					t.Fatal(err)
				}
				sed := filepath.Join(bin, "sed")
				write(sed, "#!/bin/sh\nif [ \"$1\" = -i ]; then shift; exec /usr/bin/sed -i '' \"$@\"; fi\nexec /usr/bin/sed \"$@\"\n")
				if err := os.Chmod(sed, 0700); err != nil {
					t.Fatal(err)
				}
				testPath = bin + string(os.PathListSeparator) + testPath
			}
			env := append(os.Environ(), "PATH="+testPath, "CONFIG_JSON="+configPath, "CODEX_DIR="+home, "CC_DIR="+home, "CLAUDE_HOME="+home, "CLAUDE_JSON="+filepath.Join(dir, "claude.json"), "OPENCODE_DIR="+home, "OPENCODE_XDG_DIR="+home, "HERMES_DIR="+home, "HERMES_BIN="+filepath.Join(dir, "missing-hermes"), "PICO_DIR="+home, "CLI_PROFILE_PATH="+filepath.Join(dir, "profile.sh"), "SESSION_PICKER_PATH="+filepath.Join(dir, "claude-sessions"))
			run := func() {
				t.Helper()
				cmd := exec.Command("bash", filepath.Join(runtime, "presync.sh"))
				cmd.Env = env
				if out, err := cmd.CombinedOutput(); err != nil {
					t.Fatalf("presync: %v\n%s", err, out)
				}
			}
			var nativePath, native string
			switch runtime {
			case "codex":
				nativePath = filepath.Join(home, "config.toml")
				native = "model = \"native-model\"\nmodel_reasoning_effort = \"high\"\n[mcp_servers.example]\ncommand = \"example\"\n"
				write(filepath.Join(home, "auth.json"), `{"auth_mode":"chatgpt","tokens":{"access_token":"native-token"}}`)
			case "opencode":
				nativePath = filepath.Join(home, "opencode.json")
				native = `{"model":"native/model","provider":{"native":{"options":{"custom":true}}},"mcp":{"example":{"type":"local","command":["example"]}}}`
			case "claudecode":
				nativePath = filepath.Join(home, ".credentials.json")
				native = `{"claudeAiOauth":{"accessToken":"native-token"}}`
				config["claude_code_oauth_token"] = "stale-os-token"
				save()
			case "hermes":
				nativePath = filepath.Join(home, "config.yaml")
				native = "model:\n  provider: native\n  default: native-model\ncustom_providers: []\nauxiliary:\n  vision:\n    provider: native\n    model: native-vision\n"
			case "picoclaw":
				nativePath = filepath.Join(home, "config.json")
				native = `{"agents":{"defaults":{"provider":"native","model_name":"native-model","image_model":"native-vision"}},"model_list":[{"model_name":"native-model","model":"native/model"}]}`
				write(filepath.Join(home, ".security.yml"), "model_list:\n  native-model:0:\n    api_keys: [native-key]\n")
			}
			write(nativePath, native)
			for i := 0; i < 2; i++ {
				run()
			}
			got := read(nativePath)
			switch runtime {
			case "codex", "opencode", "claudecode":
				if got != native {
					t.Fatalf("native config/auth changed: %s", got)
				}
				e := read(filepath.Join(home, ".env"))
				for _, key := range []string{"OPENAI_API_KEY=", "ANTHROPIC_API_KEY=", "ANTHROPIC_BASE_URL=", "ANTHROPIC_MODEL=", "LLM_API_KEY=", "stale-os-token"} {
					if strings.Contains(e, key) {
						t.Fatalf("native env retains %s", key)
					}
				}
			case "hermes":
				if !strings.Contains(got, "provider: native") || !strings.Contains(got, "default: native-model") || !strings.Contains(got, "model: native-vision") {
					t.Fatalf("native LLM overwritten: %s", got)
				}
				if !strings.Contains(read(filepath.Join(home, ".env")), "TELEGRAM_BOT_TOKEN=channel-token") {
					t.Fatal("channel sync did not run")
				}
			case "picoclaw":
				var cfg struct {
					Agents struct {
						Defaults struct {
							Provider string `json:"provider"`
							Model    string `json:"model_name"`
						} `json:"defaults"`
					} `json:"agents"`
				}
				if err := json.Unmarshal([]byte(got), &cfg); err != nil {
					t.Fatal(err)
				}
				if cfg.Agents.Defaults.Provider != "native" || cfg.Agents.Defaults.Model != "native-model" {
					t.Fatalf("native LLM overwritten: %s", got)
				}
				if !strings.Contains(read(filepath.Join(home, ".security.yml")), "native-key") {
					t.Fatal("native secret removed")
				}
			}
			config["llm_config_mode"] = "os"
			save()
			run()
			switch runtime {
			case "codex":
				got = read(nativePath)
				if !strings.Contains(got, `model_provider = "autonomous"`) || !strings.Contains(got, `model = "os-model"`) || !strings.Contains(got, "mcp_servers.example") {
					t.Fatalf("OS config not restored: %s", got)
				}
				if !strings.Contains(read(filepath.Join(home, ".env")), "OPENAI_API_KEY=os-key") {
					t.Fatal("OS key not restored despite native auth")
				}
			case "claudecode":
				e := read(filepath.Join(home, ".env"))
				if !strings.Contains(e, "ANTHROPIC_API_KEY=os-key") || strings.Contains(e, "CLAUDE_CODE_OAUTH_TOKEN=") {
					t.Fatalf("OS env not restored: %s", e)
				}
				if read(nativePath) != native {
					t.Fatal("subscription credentials deleted")
				}
			case "opencode":
				got = read(nativePath)
				if !strings.Contains(got, "campaign/os-model") || !strings.Contains(got, "example") {
					t.Fatalf("OS config or MCP not restored: %s", got)
				}
			case "hermes":
				got = read(nativePath)
				if !strings.Contains(got, "custom:autonomous") || !strings.Contains(got, "default: os-model") {
					t.Fatalf("OS config not restored: %s", got)
				}
			case "picoclaw":
				got = read(nativePath)
				if !strings.Contains(got, `"model_name": "autonomous"`) || !strings.Contains(got, `"model": "os-model"`) {
					t.Fatalf("OS config not restored: %s", got)
				}
			}
			// Existing devices without the new setting keep subscription auto-detection.
			if runtime == "codex" || runtime == "claudecode" {
				delete(config, "llm_config_mode")
				save()
				run()
				e := read(filepath.Join(home, ".env"))
				if strings.Contains(e, "OPENAI_API_KEY=") || strings.Contains(e, "ANTHROPIC_API_KEY=") {
					t.Fatalf("legacy subscription detection changed: %s", e)
				}
			}

		})
	}
}
