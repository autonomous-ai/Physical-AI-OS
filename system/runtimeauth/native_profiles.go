package runtimeauth

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/goccy/go-yaml"
)

func nativeMap(parent map[string]any, key string) map[string]any {
	if value, ok := parent[key].(map[string]any); ok {
		return value
	}
	value := map[string]any{}
	parent[key] = value
	return value
}

func nativeString(value any) string { result, _ := value.(string); return strings.TrimSpace(result) }

func prepareHermes(root, provider string) (*Flow, error) {
	if root == "" {
		root = "/root"
	}
	nativeProvider, model, baseURL := "anthropic", "claude-sonnet-4-6", "https://api.anthropic.com"
	if provider == "openai" {
		nativeProvider, model, baseURL = "openai-codex", "gpt-5.6-sol", "https://chatgpt.com/backend-api/codex"
	} else if provider != "anthropic" {
		return nil, fmt.Errorf("unsupported Hermes account provider")
	}
	stage, err := newStage()
	if err != nil {
		return nil, err
	}
	home := filepath.Join(stage, ".hermes")
	cmd := exec.Command("hermes", "auth", "add", nativeProvider, "--type", "oauth", "--no-browser")
	cmd.Dir = stage
	cmd.Env = cleanEnv(stage, map[string]string{"HERMES_HOME": home, "PYTHONUNBUFFERED": "1"})
	return &Flow{Command: cmd, Hosts: []string{"claude.com", "claude.ai", "auth.openai.com"}, InputRequired: provider == "anthropic",
		Close: func() { _ = os.RemoveAll(stage) },
		Verify: func(ctx context.Context) error {
			_, err := readHermesOAuth(filepath.Join(home, "auth.json"), nativeProvider)
			return err
		},
		Install: func() (func() error, error) {
			pool, err := readHermesOAuth(filepath.Join(home, "auth.json"), nativeProvider)
			if err != nil {
				return nil, err
			}
			target := filepath.Join(root, ".hermes", "auth.json")
			auth, err := readJSON(target)
			if err != nil {
				return nil, err
			}
			if auth["version"] == nil {
				auth["version"] = 1
			}
			// Replace only this provider's pool: stale grants for the same account can
			// invalidate the fresh login when independently refreshed.
			nativeMap(auth, "credential_pool")[nativeProvider] = pool
			auth["active_provider"] = nativeProvider
			delete(nativeMap(auth, "suppressed_sources"), nativeProvider)
			authBytes, err := json.MarshalIndent(auth, "", "  ")
			if err != nil {
				return nil, err
			}
			configPath := filepath.Join(root, ".hermes", "config.yaml")
			configBytes, err := mergeHermesModel(configPath, nativeProvider, model, baseURL)
			if err != nil {
				return nil, err
			}
			return writeChanges(map[string][]byte{target: authBytes, configPath: configBytes})
		},
	}, nil
}

func readHermesOAuth(path, provider string) ([]any, error) {
	auth, err := readJSON(path)
	if err != nil {
		return nil, err
	}
	pool, _ := nativeMap(auth, "credential_pool")[provider].([]any)
	if len(pool) == 0 {
		return nil, fmt.Errorf("Hermes login did not save an account credential")
	}
	for _, raw := range pool {
		entry, ok := raw.(map[string]any)
		if !ok || entry["auth_type"] != "oauth" || nativeString(entry["access_token"]) == "" || nativeString(entry["refresh_token"]) == "" {
			return nil, fmt.Errorf("Hermes login saved an incomplete account credential")
		}
	}
	return pool, nil
}

func mergeHermesModel(path, provider, model, baseURL string) ([]byte, error) {
	config := map[string]any{}
	data, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	if len(data) > 0 {
		if err := yaml.Unmarshal(data, &config); err != nil {
			return nil, fmt.Errorf("read Hermes config: %w", err)
		}
	}
	selected := nativeMap(config, "model")
	selected["provider"], selected["default"], selected["base_url"] = provider, model, baseURL
	delete(selected, "api_key")
	return yaml.Marshal(config)
}

func prepareOpenClaw(root, provider string) (*Flow, error) {
	if root == "" {
		root = "/root"
	}
	if provider != "anthropic" && provider != "openai" {
		return nil, fmt.Errorf("unsupported OpenClaw account provider")
	}
	stage, err := newStage()
	if err != nil {
		return nil, err
	}
	state := filepath.Join(stage, ".openclaw")
	configPath := filepath.Join(state, "openclaw.json")
	claudeHome := stage
	env := cleanEnv(stage, map[string]string{"OPENCLAW_STATE_DIR": state, "OPENCLAW_CONFIG_PATH": configPath, "CLAUDE_CONFIG_DIR": claudeHome})
	cmd := exec.Command("openclaw", "models", "auth", "login", "--provider", "openai", "--method", "device-code", "--set-default")
	if provider == "anthropic" {
		cmd = exec.Command("claude", "auth", "login", "--claudeai")
	}
	cmd.Dir, cmd.Env = stage, env
	return &Flow{Command: cmd, Hosts: []string{"claude.com", "claude.ai", "platform.claude.com", "console.anthropic.com", "auth.openai.com"}, InputRequired: provider == "anthropic",
		Close: func() { _ = os.RemoveAll(stage) },
		Verify: func(ctx context.Context) error {
			if provider == "anthropic" {
				if err := verifyNativeClaude(filepath.Join(claudeHome, ".credentials.json")); err != nil {
					return err
				}
				route := exec.CommandContext(ctx, "openclaw", "models", "auth", "login", "--provider", "anthropic", "--method", "cli", "--set-default")
				route.Dir, route.Env = stage, env
				if _, err := runPTY(ctx, route); err != nil {
					return fmt.Errorf("configure OpenClaw Claude account: %w", err)
				}
			}
			_, err := openClawChanges(stage, root, provider)
			return err
		},
		Install: func() (func() error, error) {
			changes, err := openClawChanges(stage, root, provider)
			if err != nil {
				return nil, err
			}
			return writeChanges(changes)
		},
	}, nil
}

func verifyNativeClaude(path string) error { return validateClaudeCredentials(path) }

func openClawChanges(stage, root, provider string) (map[string][]byte, error) {
	config, err := readJSON(filepath.Join(stage, ".openclaw", "openclaw.json"))
	if err != nil {
		return nil, err
	}
	defaults := nativeMap(nativeMap(config, "agents"), "defaults")
	primary := nativeString(nativeMap(defaults, "model")["primary"])
	if primary == "" || !strings.HasPrefix(primary, provider+"/") {
		return nil, fmt.Errorf("OpenClaw login did not select the account model")
	}
	livePath := filepath.Join(root, ".openclaw", "openclaw.json")
	live, err := readJSON(livePath)
	if err != nil {
		return nil, err
	}
	liveDefaults := nativeMap(nativeMap(live, "agents"), "defaults")
	liveDefaults["model"] = map[string]any{"primary": primary}
	for key, value := range nativeMap(defaults, "models") {
		if strings.HasPrefix(key, provider+"/") {
			nativeMap(liveDefaults, "models")[key] = value
		}
	}
	changes := map[string][]byte{}
	if provider == "anthropic" {
		staged := filepath.Join(stage, ".credentials.json")
		if err := verifyNativeClaude(staged); err != nil {
			return nil, err
		}
		credentials, err := mergeJSONKey(staged, filepath.Join(root, ".claude", ".credentials.json"), "claudeAiOauth")
		if err != nil {
			return nil, err
		}
		changes[filepath.Join(root, ".claude", ".credentials.json")] = credentials
		accountConfig, err := mergeJSONKey(filepath.Join(stage, ".claude.json"), filepath.Join(root, ".claude.json"), "oauthAccount")
		if err != nil {
			return nil, err
		}
		changes[filepath.Join(root, ".claude.json")] = accountConfig
		entry := nativeMap(defaults, "models")[primary]
		model, ok := entry.(map[string]any)
		if !ok || nativeString(nativeMap(model, "agentRuntime")["id"]) != "claude-cli" {
			return nil, fmt.Errorf("OpenClaw did not configure the Claude CLI account route")
		}
	} else {
		rel := filepath.Join(".openclaw", "agents", "main", "agent", "auth-profiles.json")
		staged, err := readJSON(filepath.Join(stage, rel))
		if err != nil {
			return nil, err
		}
		profiles := nativeMap(staged, "profiles")
		if len(profiles) == 0 {
			return nil, fmt.Errorf("OpenClaw login did not save an account profile")
		}
		liveAuth, err := readJSON(filepath.Join(root, rel))
		if err != nil {
			return nil, err
		}
		if liveAuth["version"] == nil {
			liveAuth["version"] = staged["version"]
		}
		var profileIDs []string
		for id, raw := range profiles {
			profile, ok := raw.(map[string]any)
			if !ok || profile["provider"] != provider || profile["type"] != "oauth" || nativeString(profile["access"]) == "" || nativeString(profile["refresh"]) == "" {
				return nil, fmt.Errorf("OpenClaw login saved an incomplete account profile")
			}
			nativeMap(liveAuth, "profiles")[id] = profile
			profileIDs = append(profileIDs, id)
		}
		sort.Strings(profileIDs)
		// An existing order override must not keep selecting the previous account.
		nativeMap(liveAuth, "order")[provider] = profileIDs
		nativeMap(nativeMap(live, "auth"), "order")[provider] = profileIDs
		for id, raw := range nativeMap(nativeMap(config, "auth"), "profiles") {
			nativeMap(nativeMap(live, "auth"), "profiles")[id] = raw
		}
		encoded, err := json.MarshalIndent(liveAuth, "", "  ")
		if err != nil {
			return nil, err
		}
		changes[filepath.Join(root, rel)] = encoded
	}
	encoded, err := json.MarshalIndent(live, "", "  ")
	if err != nil {
		return nil, err
	}
	changes[livePath] = encoded
	return changes, nil
}
