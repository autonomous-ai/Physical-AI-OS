package accountlogin

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/pelletier/go-toml/v2"
)

func fakeLoginCLI(t *testing.T, name, output string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\nprintf '%s\\n' '"+output+"'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func putLoginFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
}

func claudeLoginFixture(t *testing.T, stage string) {
	t.Helper()
	doc := map[string]any{"claudeAiOauth": map[string]any{
		"accessToken": "new-access", "refreshToken": "new-refresh", "expiresAt": time.Now().Add(time.Hour).UnixMilli(),
	}}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	putLoginFile(t, filepath.Join(stage, ".credentials.json"), string(b))
	putLoginFile(t, filepath.Join(stage, ".claude.json"), `{"oauthAccount":{"emailAddress":"new@example.test"},"unrelatedStagedSetting":true}`)
}

const codexLoginFixture = `{"auth_mode":"chatgpt","OPENAI_API_KEY":null,"tokens":{"access_token":"new-access","refresh_token":"new-refresh","id_token":"new-id"}}`

func TestClaudeLoginIsolatedInstallAndRollback(t *testing.T) {
	fakeLoginCLI(t, "claude", `{"loggedIn":true,"authMethod":"claude.ai"}`)
	root := t.TempDir()
	configPath := filepath.Join(root, ".claude.json")
	credentialsPath := filepath.Join(root, ".claude", ".credentials.json")
	oldConfig := `{"projects":{"work":{"mcpServers":{"test":{}}}},"oauthAccount":{"emailAddress":"old@example.test"}}`
	oldCredentials := `{"claudeAiOauth":{"accessToken":"old"},"otherProvider":{"secret":"preserve"}}`
	putLoginFile(t, configPath, oldConfig)
	putLoginFile(t, credentialsPath, oldCredentials)
	flow, err := prepareClaude(root)
	if err != nil {
		t.Fatal(err)
	}
	defer flow.Close()
	if flow.Command.Dir == root || !flow.InputRequired {
		t.Fatal("login must use staging and manual code input")
	}
	if err := flow.Verify(context.Background()); err == nil {
		t.Fatal("existing live credentials must not satisfy a new login")
	}
	claudeLoginFixture(t, flow.Command.Dir)
	if err := flow.Verify(context.Background()); err != nil {
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(configPath); string(raw) != oldConfig {
		t.Fatal("verification changed live configuration")
	}
	rollback, err := flow.Install()
	if err != nil {
		t.Fatal(err)
	}
	config, err := readJSON(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if config["projects"] == nil || config["unrelatedStagedSetting"] != nil || config["oauthAccount"].(map[string]any)["emailAddress"] != "new@example.test" {
		t.Fatalf("account import lost configuration or copied unrelated state: %v", config)
	}
	credentials, err := readJSON(credentialsPath)
	if err != nil || credentials["otherProvider"] == nil {
		t.Fatal("unrelated native credentials were not preserved")
	}
	if err := rollback(); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]string{configPath: oldConfig, credentialsPath: oldCredentials} {
		got, err := os.ReadFile(path)
		if err != nil || string(got) != want {
			t.Fatalf("rollback failed for %s: %v", path, err)
		}
	}
}

func TestClaudeLoginRejectsAPIKeyAndMalformedCredentials(t *testing.T) {
	fakeLoginCLI(t, "claude", `{"loggedIn":true,"authMethod":"api_key"}`)
	flow, err := prepareClaude(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer flow.Close()
	claudeLoginFixture(t, flow.Command.Dir)
	if err := flow.Verify(context.Background()); err == nil {
		t.Fatal("API key status is not a subscription login")
	}
	for _, body := range []string{`{`, `{}`, `{"claudeAiOauth":{"accessToken":"a","refreshToken":"b","expiresAt":1}}`} {
		putLoginFile(t, filepath.Join(flow.Command.Dir, ".credentials.json"), body)
		if _, err := flow.Install(); err == nil {
			t.Fatalf("installed malformed credentials: %s", body)
		}
	}
}

func TestCodexLoginSwitchesProviderPreservesToolsAndRollsBack(t *testing.T) {
	fakeLoginCLI(t, "codex", "Logged in using ChatGPT")
	root := t.TempDir()
	configPath := filepath.Join(root, ".codex", "config.toml")
	before := `model = "Auto-AI"
model_provider = "autonomous"
approval_policy = "never"
profile = "device"
[model_providers.autonomous]
base_url = "https://proxy.example/v1"
[mcp_servers.lamp]
url = "http://localhost:5000/mcp"
[profiles.device]
model_provider = "autonomous"
model = "Auto-AI"
sandbox_mode = "danger-full-access"
`
	putLoginFile(t, configPath, before)
	flow, err := prepareCodex(root)
	if err != nil {
		t.Fatal(err)
	}
	defer flow.Close()
	putLoginFile(t, filepath.Join(flow.Command.Dir, "auth.json"), codexLoginFixture)
	if err := flow.Verify(context.Background()); err != nil {
		t.Fatal(err)
	}
	rollback, err := flow.Install()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := toml.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	if config["model_provider"] != "openai" || config["model"] != nil || config["mcp_servers"] == nil || config["approval_policy"] != "never" || config["cli_auth_credentials_store"] != "file" {
		t.Fatalf("wrong native configuration: %v", config)
	}
	profile := config["profiles"].(map[string]any)["device"].(map[string]any)
	if profile["model_provider"] != "openai" || profile["model"] != nil || profile["sandbox_mode"] != "danger-full-access" {
		t.Fatalf("selected profile still overrides native auth: %v", profile)
	}
	if err := rollback(); err != nil {
		t.Fatal(err)
	}
	raw, _ = os.ReadFile(configPath)
	if string(raw) != before {
		t.Fatal("rollback did not restore exact original TOML")
	}
	if _, err := os.Stat(filepath.Join(root, ".codex", "auth.json")); !os.IsNotExist(err) {
		t.Fatal("rollback did not remove newly installed auth")
	}
}

func TestCodexRejectsInvalidAuthAndLeavesLiveConfigUntouched(t *testing.T) {
	fakeLoginCLI(t, "codex", "Logged in using ChatGPT")
	root := t.TempDir()
	flow, err := prepareCodex(root)
	if err != nil {
		t.Fatal(err)
	}
	defer flow.Close()
	for _, body := range []string{`{`, `{}`, `{"auth_mode":"apikey","OPENAI_API_KEY":"secret"}`, `{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}`} {
		putLoginFile(t, filepath.Join(flow.Command.Dir, "auth.json"), body)
		if err := flow.Verify(context.Background()); err == nil {
			t.Fatalf("accepted invalid auth: %s", body)
		}
		if _, err := flow.Install(); err == nil {
			t.Fatal("installed invalid auth")
		}
	}
	putLoginFile(t, filepath.Join(flow.Command.Dir, "auth.json"), codexLoginFixture)
	configPath := filepath.Join(root, ".codex", "config.toml")
	putLoginFile(t, configPath, "not valid = [")
	if _, err := flow.Install(); err == nil {
		t.Fatal("overwrote malformed native config")
	}
	if _, err := os.Stat(filepath.Join(root, ".codex", "auth.json")); !os.IsNotExist(err) {
		t.Fatal("auth was installed before config validation")
	}
}

func TestCodexNativeModelAndMCPArePreserved(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.toml")
	putLoginFile(t, path, "model_provider = \"openai\"\nmodel = \"gpt-native\"\n[mcp_servers.example]\ncommand = \"example\"\nargs = [\"--flag\"]\n")
	raw, err := codexAccountConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := toml.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got["model"] != "gpt-native" || !strings.Contains(string(raw), "--flag") {
		t.Fatal("native model or MCP arguments lost")
	}
	if got["model_provider"] != "openai" {
		t.Fatal("unexpected provider")
	}
}
