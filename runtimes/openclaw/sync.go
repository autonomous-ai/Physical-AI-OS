package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/alert"
	"go.autonomous.ai/os/system/server/config"
)

// SyncModelsFromAPI fetches the live model list from ModelsAPIURL and reconciles it into openclaw.json under s.config.OpenclawConfigDir.
func (s *OpenclawService) SyncModelsFromAPI() (bool, error) {
	if s.config.LLMRuntimeManaged() {
		return false, nil
	}
	// Same source-of-catalog decision as SetupAgent / ensureProviderConfig / ensureAgentDefaults (byo_models.go).
	resp, byo, err := resolveModels(context.Background(), s.config.LLMBaseURL, s.config.LLMAPIKey)
	if err != nil {
		return false, fmt.Errorf("fetch models (byo=%v): %w", byo, err)
	}

	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()
	if s.config.LLMRuntimeManaged() {
		return false, nil
	}

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(raw, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw config: %w", err)
	}

	autonomousMap, ok := autonomousProviderMap(configData)
	if !ok {
		return false, nil
	}

	return s.applyModelsToConfig(configPath, configData, autonomousMap, resp)
}

// StartModelSync runs the periodic model sync loop until ctx is cancelled.
func (s *OpenclawService) StartModelSync(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("[modelsync] PANIC recovered, sync loop stopped", "panic", r)
		}
	}()

	tick := func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("[modelsync] tick PANIC recovered", "panic", r)
			}
		}()
		if _, err := s.SyncModelsFromAPI(); err != nil {
			slog.Warn("[modelsync] tick failed", "err", err)
		}
	}

	tick()
	ticker := time.NewTicker(ModelSyncInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			tick()
		}
	}
}

// FetchModelsFromAPI does the actual HTTP GET against ModelsAPIURL (tunables.go) and returns the upstream model list.
func FetchModelsFromAPI() (*domain.LLMModelsListResponse, error) {
	url := strings.TrimSpace(ModelsAPIURL)
	if url == "" {
		return nil, fmt.Errorf("empty models api url (check tunables.go)")
	}

	ctx, cancel := context.WithTimeout(context.Background(), modelsAPITimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build models request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("fetch %s: status %d", url, resp.StatusCode)
	}

	var out domain.LLMModelsListResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode models response: %w", err)
	}
	if len(out.Models) == 0 {
		return nil, fmt.Errorf("models response is empty")
	}
	return &out, nil
}

// autonomousProviderMap drills into models.providers.autonomous, returning the inner map and ok=true only when every level exists.
func autonomousProviderMap(configData map[string]any) (map[string]any, bool) {
	modelsMap, _ := configData["models"].(map[string]any)
	if modelsMap == nil {
		return nil, false
	}
	providersMap, _ := modelsMap["providers"].(map[string]any)
	if providersMap == nil {
		return nil, false
	}
	autonomousMap, _ := providersMap[customProviderName].(map[string]any)
	if autonomousMap == nil {
		return nil, false
	}
	return autonomousMap, true
}

// applyModelsToConfig writes the autonomous model catalog and version-gated defaults, restarts the gateway on change, and persists the catalog version.
func (s *OpenclawService) applyModelsToConfig(configPath string, configData map[string]any, autonomousMap map[string]any, resp *domain.LLMModelsListResponse) (bool, error) {
	fetched := resp.Models

	existingProvider, _ := autonomousMap["models"].([]any)
	newProvider, providerChanged := overwriteProviderModels(existingProvider, fetched)

	// Overwrite the provider wire protocol from upstream (fallback to the built-in default when omitted).
	apiType := resolveAutonomousAPI(resp.API)
	var apiChanged bool
	if cur, _ := autonomousMap["api"].(string); cur != apiType {
		apiChanged = true
	}

	var agentChanged bool
	if agentsMap, ok := configData["agents"].(map[string]any); ok {
		if defaultsMap, ok := agentsMap["defaults"].(map[string]any); ok {
			existingAgent, _ := defaultsMap["models"].(map[string]any)
			merged, changed := overwriteAgentAutonomousModels(existingAgent, fetched)
			if changed {
				defaultsMap["models"] = merged
				agentChanged = true
			}
		}
	}

	// Version-gated default model / image model.
	applyDefaults := resp.Version > 0 && resp.Version > s.config.DefaultModelVersion
	var primaryChanged, imageChanged bool
	if applyDefaults {
		if isDefaultsOnAutonomous(configData) {
			primaryChanged = applyDefaultPrimaryModel(configData, strings.TrimSpace(resp.DefaultModel))
		}
		if isImageModelOnAutonomous(configData) {
			imageChanged = applyDefaultImageModel(configData, strings.TrimSpace(resp.DefaultImageModel))
		}
	}

	if providerChanged {
		autonomousMap["models"] = newProvider
	}
	if apiChanged {
		autonomousMap["api"] = apiType
	}

	if !providerChanged && !apiChanged && !agentChanged && !primaryChanged && !imageChanged {
		if applyDefaults {
			s.persistModelState(resp.Version, "")
		}
		return false, nil
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw config: %w", err)
	}
	setOSWriteFlag(filepath.Dir(configPath), extractPrimaryModel(configData))
	if err := atomicWriteFile(configPath, written, 0600); err != nil {
		return false, fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return false, fmt.Errorf("set openclaw config ownership: %w", err)
	}

	if applyDefaults {
		newModel := ""
		if primaryChanged {
			_, newModel, _ = splitProviderModel(extractPrimaryModel(configData))
		}
		s.persistModelState(resp.Version, newModel)
	}

	slog.Info("[modelsync] reconciled openclaw config",
		"path", configPath,
		"provider_changed", providerChanged,
		"api_changed", apiChanged,
		"agent_changed", agentChanged,
		"primary_changed", primaryChanged,
		"image_changed", imageChanged,
		"version", resp.Version,
		"fetched", len(fetched),
	)

	if primaryChanged || imageChanged {
		var changed []string
		if primaryChanged {
			changed = append(changed, "primary="+extractPrimaryModel(configData))
		}
		if imageChanged {
			changed = append(changed, "image_model updated")
		}
		alert.Notifyf(context.Background(), s.config,
			fmt.Sprintf("🔄 Default model updated (catalog v%d)", resp.Version),
			strings.Join(changed, " · "))
	}

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[modelsync] restart openclaw gateway", "err", err)
	}
	return true, nil
}

// persistModelState records the catalog version (only advances) and, when newModel != "", the primary model.
func (s *OpenclawService) persistModelState(version int, newModel string) {
	if err := s.config.WithLockSave(func(c *config.Config) {
		if version > c.DefaultModelVersion {
			c.DefaultModelVersion = version
		}
		if newModel != "" {
			c.LLMModel = newModel
		}
	}); err != nil {
		slog.Warn("[modelsync] persist model state", "err", err)
	}
}

// resolveAutonomousAPI returns the upstream wire protocol, or autonomousProviderAPI when absent.
func resolveAutonomousAPI(api string) string {
	if v := strings.TrimSpace(api); v != "" {
		return v
	}
	return autonomousProviderAPI
}

// overwriteProviderModels REPLACES the providers.autonomous.models[] slice with the fetched list.
func overwriteProviderModels(existing []any, fetched []domain.LLMModel) ([]any, bool) {
	out := make([]any, 0, len(fetched))
	for _, m := range fetched {
		out = append(out, openclawModelToProviderEntry(m))
	}
	if len(existing) != len(out) {
		return out, true
	}
	for i, freshAny := range out {
		fresh := freshAny.(map[string]any)
		oldEntry, ok := existing[i].(map[string]any)
		if !ok {
			return out, true
		}
		if oldID, _ := oldEntry["id"].(string); oldID != fresh["id"].(string) {
			return out, true
		}
		if !numbersEqual(oldEntry["contextWindow"], fresh["contextWindow"]) {
			return out, true
		}
		if !numbersEqual(oldEntry["maxTokens"], fresh["maxTokens"]) {
			return out, true
		}
	}
	return out, false
}

// numbersEqual compares JSON-decoded numbers that may be int, int64 or float64.
func numbersEqual(a, b any) bool {
	av, aOk := toFloat(a)
	bv, bOk := toFloat(b)
	if !aOk || !bOk {
		return false
	}
	return av == bv
}

func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case int:
		return float64(x), true
	case int32:
		return float64(x), true
	case int64:
		return float64(x), true
	case float32:
		return float64(x), true
	case float64:
		return x, true
	}
	return 0, false
}

// overwriteAgentAutonomousModels reconciles agents.defaults.models so the set of "autonomous-owned" keys exactly matches the fetched list.
func overwriteAgentAutonomousModels(existing map[string]any, fetched []domain.LLMModel) (map[string]any, bool) {
	out := make(map[string]any, len(existing)+len(fetched))
	for k, v := range existing {
		out[k] = v
	}
	wanted := make(map[string]struct{}, len(fetched))
	for _, m := range fetched {
		if m.Key == "" {
			continue
		}
		wanted[agentModelKey(m)] = struct{}{}
	}
	changed := false
	prefix := customProviderName + "/"
	for k := range existing {
		switch {
		case strings.HasPrefix(k, prefix):
			if _, keep := wanted[k]; !keep {
				delete(out, k)
				changed = true
			}
		case !strings.Contains(k, "/"):
			delete(out, k)
			changed = true
		}
	}
	for want := range wanted {
		if _, ok := out[want]; !ok {
			out[want] = map[string]any{}
			changed = true
		}
	}
	return out, changed
}

// agentModelKey returns the key used under agents.defaults.models for a given provider model.
func agentModelKey(m domain.LLMModel) string {
	return customProviderName + "/" + m.Key
}

// atomicWriteFile writes via temp file + rename so readers and power loss never see a half-written file.
func atomicWriteFile(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".openclaw-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpPath := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpPath) }

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("fsync temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Chmod(tmpPath, perm); err != nil {
		cleanup()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}
