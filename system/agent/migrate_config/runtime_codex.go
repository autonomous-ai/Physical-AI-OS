package migrateconfig

import (
	"os"
	"path/filepath"

	"github.com/pelletier/go-toml/v2"
)

type codexAdapter struct{}

func (codexAdapter) runtime() Runtime { return RuntimeCodex }

// read takes the key from .env (OPENAI_API_KEY) and base_url from config.toml.
func (codexAdapter) read(opts Options) (LLMConfig, error) {
	return LLMConfig{
		APIKey:  readEnvVar(filepath.Join(opts.CodexHome, ".env"), "OPENAI_API_KEY"),
		BaseURL: readCodexBaseURL(filepath.Join(opts.CodexHome, "config.toml")),
	}, nil
}

func (codexAdapter) write(cfg LLMConfig, opts Options) error {
	if cfg.APIKey != "" {
		if err := writeEnvVar(filepath.Join(opts.CodexHome, ".env"), "OPENAI_API_KEY", cfg.APIKey); err != nil {
			return err
		}
	}
	if cfg.BaseURL != "" {
		if err := writeCodexBaseURL(filepath.Join(opts.CodexHome, "config.toml"), cfg.BaseURL); err != nil {
			return err
		}
	}
	return nil
}

// readCodexBaseURL returns model_providers.autonomous.base_url, or "" if missing.
func readCodexBaseURL(configTOML string) string {
	raw, err := os.ReadFile(configTOML)
	if err != nil {
		return ""
	}
	root := map[string]any{}
	if err := toml.Unmarshal(raw, &root); err != nil {
		return ""
	}
	providers, _ := root["model_providers"].(map[string]any)
	autonomous, _ := providers["autonomous"].(map[string]any)
	baseURL, _ := autonomous["base_url"].(string)
	return baseURL
}

// writeCodexBaseURL patches model_providers.autonomous.base_url; a missing config.toml is not
// an error (presync regenerates it).
func writeCodexBaseURL(configTOML, baseURL string) error {
	raw, err := os.ReadFile(configTOML)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	root := map[string]any{}
	if err := toml.Unmarshal(raw, &root); err != nil {
		return err
	}

	providers := ensureMap(root, "model_providers")
	autonomous := ensureMap(providers, "autonomous")
	if autonomous["base_url"] == baseURL {
		return nil
	}
	autonomous["base_url"] = baseURL

	out, err := toml.Marshal(root)
	if err != nil {
		return err
	}
	return atomicWrite(configTOML, out, 0o644)
}
