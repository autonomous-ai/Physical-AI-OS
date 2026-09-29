package migrateconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type opencodeAdapter struct{}

func (opencodeAdapter) runtime() Runtime { return RuntimeOpenCode }

// read takes the key from /root/.opencode/.env (LLM_API_KEY) and baseURL from opencode.json.
func (opencodeAdapter) read(opts Options) (LLMConfig, error) {
	return LLMConfig{
		APIKey:  readEnvVar(filepath.Join(opts.OpenCodeHome, ".env"), "LLM_API_KEY"),
		BaseURL: readOpenCodeBaseURL(opts.OpenCodeConfig),
	}, nil
}

func (opencodeAdapter) write(cfg LLMConfig, opts Options) error {
	if cfg.APIKey != "" {
		if err := writeEnvVar(filepath.Join(opts.OpenCodeHome, ".env"), "LLM_API_KEY", cfg.APIKey); err != nil {
			return err
		}
	}
	if cfg.BaseURL != "" {
		if err := writeOpenCodeBaseURL(opts.OpenCodeConfig, cfg.BaseURL); err != nil {
			return err
		}
	}
	return nil
}

// readOpenCodeBaseURL returns provider.campaign.options.baseURL, or "" if missing.
func readOpenCodeBaseURL(configJSON string) string {
	raw, err := os.ReadFile(configJSON)
	if err != nil {
		return ""
	}
	root := map[string]any{}
	if err := json.Unmarshal(raw, &root); err != nil {
		return ""
	}
	provider, _ := root["provider"].(map[string]any)
	campaign, _ := provider["campaign"].(map[string]any)
	options, _ := campaign["options"].(map[string]any)
	baseURL, _ := options["baseURL"].(string)
	return baseURL
}

// writeOpenCodeBaseURL patches provider.campaign.options.baseURL; a missing opencode.json is
// not an error (presync regenerates it).
func writeOpenCodeBaseURL(configJSON, baseURL string) error {
	raw, err := os.ReadFile(configJSON)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	root := map[string]any{}
	if err := json.Unmarshal(raw, &root); err != nil {
		return err
	}

	provider := ensureMap(root, "provider")
	campaign := ensureMap(provider, "campaign")
	options := ensureMap(campaign, "options")
	if options["baseURL"] == baseURL {
		return nil
	}
	options["baseURL"] = baseURL

	out, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return err
	}
	out = append(out, '\n')
	return atomicWrite(configJSON, out, 0o644)
}
