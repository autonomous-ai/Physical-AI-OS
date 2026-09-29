package migrateconfig

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"

	"github.com/goccy/go-yaml"
)

type hermesAdapter struct{}

func (hermesAdapter) runtime() Runtime { return RuntimeHermes }

// read extracts LLMConfig from the presync-owned config.yaml (base_url) and .env (AUTONOMOUS_API_KEY).
func (hermesAdapter) read(opts Options) (LLMConfig, error) {
	baseURL := readHermesBaseURL(filepath.Join(opts.HermesRoot, "config.yaml"))
	apiKey := readEnvVar(filepath.Join(opts.HermesRoot, ".env"), "AUTONOMOUS_API_KEY")
	return LLMConfig{APIKey: apiKey, BaseURL: baseURL}, nil
}

// write updates config.yaml and .env with the same fields presync.sh owns.
func (hermesAdapter) write(cfg LLMConfig, opts Options) error {
	if cfg.BaseURL != "" {
		if err := writeHermesBaseURL(filepath.Join(opts.HermesRoot, "config.yaml"), cfg.BaseURL); err != nil {
			return err
		}
	}
	if cfg.APIKey != "" {
		if err := writeEnvVar(filepath.Join(opts.HermesRoot, ".env"), "AUTONOMOUS_API_KEY", cfg.APIKey); err != nil {
			return err
		}
	}
	return nil
}

// hermesConfigYAML is the minimal shape of ~/.hermes/config.yaml we need to parse.
type hermesConfigYAML struct {
	CustomProviders []struct {
		Name    string `yaml:"name"`
		BaseURL string `yaml:"base_url"`
	} `yaml:"custom_providers"`
}

func readHermesBaseURL(configYAML string) string {
	data, err := os.ReadFile(configYAML)
	if err != nil {
		return ""
	}
	var h hermesConfigYAML
	if err := yaml.Unmarshal(data, &h); err != nil {
		return ""
	}
	for _, p := range h.CustomProviders {
		if p.Name == "autonomous" {
			return p.BaseURL
		}
	}
	return ""
}

func writeHermesBaseURL(configYAML, baseURL string) error {
	data, err := os.ReadFile(configYAML)
	if err != nil {
		return err
	}
	var raw map[string]interface{}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return err
	}
	providers, _ := raw["custom_providers"].([]interface{})
	for _, p := range providers {
		pm, ok := p.(map[string]interface{})
		if !ok {
			continue
		}
		if name, _ := pm["name"].(string); name == "autonomous" {
			pm["base_url"] = baseURL
		}
	}
	out, err := yaml.Marshal(raw)
	if err != nil {
		return err
	}
	return atomicWrite(configYAML, out, 0o644)
}

// readEnvVar reads a KEY=VALUE line from a shell env file.
func readEnvVar(envFile, key string) string {
	f, err := os.Open(envFile)
	if err != nil {
		return ""
	}
	defer f.Close()
	prefix := key + "="
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if strings.HasPrefix(line, prefix) {
			return strings.TrimPrefix(line, prefix)
		}
	}
	return ""
}

// writeEnvVar upserts KEY=VALUE in a shell env file, preserving all other lines.
func writeEnvVar(envFile, key, value string) error {
	data, err := os.ReadFile(envFile)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	prefix := key + "="
	newLine := prefix + value
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	found := false
	for i, line := range lines {
		if strings.HasPrefix(strings.TrimSpace(line), prefix) {
			lines[i] = newLine
			found = true
		}
	}
	if !found {
		lines = append(lines, newLine)
	}
	return atomicWrite(envFile, []byte(strings.Join(lines, "\n")+"\n"), 0o600)
}
