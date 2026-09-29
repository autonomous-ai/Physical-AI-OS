package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// Bring your own endpoint.

// autonomousHosts are the hosts that serve our own catalog.
var autonomousHosts = []string{
	"autonomous.ai",
	"autonomousdev.xyz",
}

// isAutonomousEndpoint reports whether baseURL points at our own gateway.
func isAutonomousEndpoint(baseURL string) bool {
	raw := strings.TrimSpace(baseURL)
	if raw == "" {
		return true
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return true
	}
	host := strings.ToLower(u.Hostname())
	for _, suffix := range autonomousHosts {
		if host == suffix || strings.HasSuffix(host, "."+suffix) {
			return true
		}
	}
	return false
}

// openAIModelsResponse is the OpenAI-compatible `GET /v1/models` shape.
type openAIModelsResponse struct {
	Data []struct {
		ID string `json:"id"`
	} `json:"data"`
}

// modelsEndpoint turns a provider base URL into its models URL: it appends /models, tolerating a base that already ends in /v1 or a trailing slash.
func modelsEndpoint(baseURL string) string {
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if strings.HasSuffix(base, "/models") {
		return base
	}
	return base + "/models"
}

// fetchOpenAIModels asks a BYO endpoint what it can serve.
func fetchOpenAIModels(ctx context.Context, baseURL, apiKey string) ([]domain.LLMModel, error) {
	endpoint := modelsEndpoint(baseURL)
	ctx, cancel := context.WithTimeout(ctx, modelsAPITimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("build models request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	if key := strings.TrimSpace(apiKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
		req.Header.Set("x-api-key", key)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", endpoint, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("fetch %s: status %d", endpoint, resp.StatusCode)
	}

	var raw json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, fmt.Errorf("decode models response: %w", err)
	}

	// Our own catalog shape first — a proxy may serve it verbatim.
	var ours domain.LLMModelsListResponse
	if err := json.Unmarshal(raw, &ours); err == nil && len(ours.Models) > 0 {
		return ours.Models, nil
	}

	var oai openAIModelsResponse
	if err := json.Unmarshal(raw, &oai); err != nil {
		return nil, fmt.Errorf("decode models response: %w", err)
	}
	out := make([]domain.LLMModel, 0, len(oai.Data))
	for _, m := range oai.Data {
		id := strings.TrimSpace(m.ID)
		if id == "" {
			continue
		}
		out = append(out, domain.LLMModel{
			Key:     id,
			Name:    id,
			Input:   []string{"text"},
			Privacy: "private",
		})
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("models response from %s is empty", endpoint)
	}
	return out, nil
}

// resolveModels returns the model catalog for a device, and whether it came from a bring-your-own endpoint.
func resolveModels(ctx context.Context, baseURL, apiKey string) (*domain.LLMModelsListResponse, bool, error) {
	if isAutonomousEndpoint(baseURL) {
		resp, err := FetchModelsFromAPI()
		return resp, false, err
	}

	slog.Info("byo endpoint: listing models from the configured base URL",
		"component", "openclaw", "endpoint", modelsEndpoint(baseURL))
	models, err := fetchOpenAIModels(ctx, baseURL, apiKey)
	if err != nil {
		return nil, true, fmt.Errorf("bring-your-own endpoint %s: %w", modelsEndpoint(baseURL), err)
	}
	return &domain.LLMModelsListResponse{
		Count:        len(models),
		DefaultModel: models[0].Key,
		API:          "openai-completions",
		Models:       models,
	}, true, nil
}
