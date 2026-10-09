package runtimeauth

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/pelletier/go-toml/v2"
)

func prepareClaude(root string) (*Flow, error) {
	if root == "" {
		root = "/root"
	}
	binary, err := exec.LookPath("claude")
	if err != nil {
		return nil, fmt.Errorf("Claude Code CLI is not installed: %w", err)
	}
	stage, err := newStage()
	if err != nil {
		return nil, err
	}
	env := cleanEnv(stage, map[string]string{"CLAUDE_CONFIG_DIR": stage})
	cmd := exec.Command(binary, "auth", "login", "--claudeai")
	cmd.Dir, cmd.Env = stage, env
	flow := &Flow{
		Command: cmd, Hosts: []string{"claude.com", "claude.ai", "console.anthropic.com", "platform.claude.com"},
		InputRequired: true, Close: func() { _ = os.RemoveAll(stage) },
	}
	flow.Verify = func(ctx context.Context) error {
		if err := validateClaudeCredentials(filepath.Join(stage, ".credentials.json")); err != nil {
			return err
		}
		status := exec.CommandContext(ctx, binary, "auth", "status", "--json")
		status.Dir, status.Env = stage, env
		output, err := status.Output()
		if err != nil {
			return fmt.Errorf("Claude Code could not verify the new sign-in")
		}
		var result struct {
			LoggedIn bool   `json:"loggedIn"`
			Method   string `json:"authMethod"`
		}
		if json.Unmarshal(output, &result) != nil || !result.LoggedIn || result.Method != "claude.ai" {
			return fmt.Errorf("Claude Code did not confirm a Claude account sign-in")
		}
		return nil
	}
	flow.Install = func() (func() error, error) {
		if err := validateClaudeCredentials(filepath.Join(stage, ".credentials.json")); err != nil {
			return nil, err
		}
		credentialsPath := filepath.Join(root, ".claude", ".credentials.json")
		credentials, err := mergeJSONKey(filepath.Join(stage, ".credentials.json"), credentialsPath, "claudeAiOauth")
		if err != nil {
			return nil, err
		}
		configPath := filepath.Join(root, ".claude.json")
		config, err := mergeJSONKey(filepath.Join(stage, ".claude.json"), configPath, "oauthAccount")
		if err != nil {
			return nil, err
		}
		return writeChanges(map[string][]byte{credentialsPath: credentials, configPath: config})
	}
	return flow, nil
}

func validateClaudeCredentials(path string) error {
	doc, err := readJSON(path)
	if err != nil {
		return fmt.Errorf("read new Claude Code credentials: %w", err)
	}
	oauth, ok := doc["claudeAiOauth"].(map[string]any)
	if !ok || !nonemptyString(oauth["accessToken"]) || !nonemptyString(oauth["refreshToken"]) {
		return fmt.Errorf("Claude Code did not save complete account credentials")
	}
	expires, ok := oauth["expiresAt"].(float64)
	if !ok || expires <= float64(time.Now().UnixMilli()) {
		return fmt.Errorf("Claude Code saved expired or invalid account credentials")
	}
	return nil
}

func prepareCodex(root string) (*Flow, error) {
	if root == "" {
		root = "/root"
	}
	binary, err := exec.LookPath("codex")
	if err != nil {
		return nil, fmt.Errorf("Codex CLI is not installed: %w", err)
	}
	stage, err := newStage()
	if err != nil {
		return nil, err
	}
	env := cleanEnv(stage, map[string]string{"CODEX_HOME": stage})
	args := []string{"-c", `cli_auth_credentials_store="file"`, "login", "--device-auth"}
	cmd := exec.Command(binary, args...)
	cmd.Dir, cmd.Env = stage, env
	flow := &Flow{
		Command: cmd, Hosts: []string{"auth.openai.com"},
		Close: func() { _ = os.RemoveAll(stage) },
	}
	flow.Verify = func(ctx context.Context) error {
		if _, err := codexCredentials(filepath.Join(stage, "auth.json")); err != nil {
			return err
		}
		status := exec.CommandContext(ctx, binary, "-c", `cli_auth_credentials_store="file"`, "login", "status")
		status.Dir, status.Env = stage, env
		output, err := status.CombinedOutput()
		if err != nil || !strings.Contains(string(output), "Logged in using ChatGPT") {
			return fmt.Errorf("Codex did not confirm a ChatGPT account sign-in")
		}
		return nil
	}
	flow.Install = func() (func() error, error) {
		auth, err := codexCredentials(filepath.Join(stage, "auth.json"))
		if err != nil {
			return nil, err
		}
		configPath := filepath.Join(root, ".codex", "config.toml")
		config, err := codexAccountConfig(configPath)
		if err != nil {
			return nil, err
		}
		return writeChanges(map[string][]byte{filepath.Join(root, ".codex", "auth.json"): auth, configPath: config})
	}
	return flow, nil
}

func codexCredentials(path string) ([]byte, error) {
	doc, err := readJSON(path)
	if err != nil {
		return nil, fmt.Errorf("read new Codex credentials: %w", err)
	}
	tokens, ok := doc["tokens"].(map[string]any)
	if doc["auth_mode"] != "chatgpt" || nonemptyString(doc["OPENAI_API_KEY"]) || !ok ||
		!nonemptyString(tokens["access_token"]) || !nonemptyString(tokens["refresh_token"]) || !nonemptyString(tokens["id_token"]) {
		return nil, fmt.Errorf("Codex did not save complete ChatGPT credentials")
	}
	return json.Marshal(doc)
}

func nonemptyString(value any) bool {
	s, ok := value.(string)
	return ok && strings.TrimSpace(s) != ""
}

// Keep tools, permissions and custom providers; select the native account provider.
func codexAccountConfig(path string) ([]byte, error) {
	doc := map[string]any{}
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("read Codex configuration: %w", err)
	}
	if len(raw) > 0 {
		if err := toml.Unmarshal(raw, &doc); err != nil {
			return nil, fmt.Errorf("parse Codex configuration: %w", err)
		}
	}
	selectCodexAccount(doc)
	doc["cli_auth_credentials_store"] = "file"
	if profile, ok := doc["profile"].(string); ok && profile != "" {
		if profiles, ok := doc["profiles"].(map[string]any); ok {
			if active, ok := profiles[profile].(map[string]any); ok {
				selectCodexAccount(active)
			}
		}
	}
	return toml.Marshal(doc)
}

func selectCodexAccount(doc map[string]any) {
	provider, _ := doc["model_provider"].(string)
	if provider != "" && provider != "openai" {
		delete(doc, "model")
		delete(doc, "review_model")
	} else if doc["model"] == "Auto-AI" {
		delete(doc, "model")
	}
	doc["model_provider"] = "openai"
}
