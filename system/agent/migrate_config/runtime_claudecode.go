package migrateconfig

import (
	"path/filepath"
)

type claudecodeAdapter struct{}

func (claudecodeAdapter) runtime() Runtime { return RuntimeClaudeCode }

// read extracts LLMConfig from the presync-owned /root/.claudecode/.env.
func (claudecodeAdapter) read(opts Options) (LLMConfig, error) {
	env := filepath.Join(opts.ClaudecodeDir, ".env")
	return LLMConfig{
		APIKey:  readEnvVar(env, "ANTHROPIC_API_KEY"),
		BaseURL: readEnvVar(env, "ANTHROPIC_BASE_URL"),
	}, nil
}

// write updates the presync-owned .env; ANTHROPIC_AUTH_TOKEN mirrors ANTHROPIC_API_KEY.
func (claudecodeAdapter) write(cfg LLMConfig, opts Options) error {
	env := filepath.Join(opts.ClaudecodeDir, ".env")
	if cfg.BaseURL != "" {
		if err := writeEnvVar(env, "ANTHROPIC_BASE_URL", cfg.BaseURL); err != nil {
			return err
		}
	}
	if cfg.APIKey != "" {
		if err := writeEnvVar(env, "ANTHROPIC_API_KEY", cfg.APIKey); err != nil {
			return err
		}
		if err := writeEnvVar(env, "ANTHROPIC_AUTH_TOKEN", cfg.APIKey); err != nil {
			return err
		}
	}
	return nil
}
