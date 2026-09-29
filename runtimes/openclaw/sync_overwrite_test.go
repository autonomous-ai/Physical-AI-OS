package openclaw

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func intPtr(v int) *int { return &v }

func model(key string, ctx, max int) domain.LLMModel {
	return domain.LLMModel{Key: key, Name: key, ContextWindow: intPtr(ctx), MaxTokens: intPtr(max)}
}

func TestOverwriteProviderModels_ReplacesAndDropsStale(t *testing.T) {
	existing := []any{
		map[string]any{"id": "old-stale", "contextWindow": 100, "maxTokens": 10},
		map[string]any{"id": "claude-opus-4-6", "contextWindow": 200, "maxTokens": 20},
	}
	fetched := []domain.LLMModel{model("claude-opus-4-6", 200, 20)}

	out, changed := overwriteProviderModels(existing, fetched)
	if !changed {
		t.Fatal("expected changed=true when a stale entry is dropped")
	}
	if len(out) != 1 {
		t.Fatalf("expected 1 model after overwrite, got %d", len(out))
	}
	if id := out[0].(map[string]any)["id"]; id != "claude-opus-4-6" {
		t.Errorf("expected only fetched model to remain, got id=%v", id)
	}
}

func TestOverwriteProviderModels_NoChangeWhenEquivalent(t *testing.T) {
	fetched := []domain.LLMModel{model("claude-opus-4-6", 200000, 8192)}
	existing := []any{openclawModelToProviderEntry(fetched[0])}

	_, changed := overwriteProviderModels(existing, fetched)
	if changed {
		t.Error("expected changed=false when existing already equals fetched")
	}
}

func TestOverwriteProviderModels_OrderFollowsFetched(t *testing.T) {
	existing := []any{
		map[string]any{"id": "b", "contextWindow": 1, "maxTokens": 1},
		map[string]any{"id": "a", "contextWindow": 1, "maxTokens": 1},
	}
	fetched := []domain.LLMModel{model("a", 1, 1), model("b", 1, 1)}

	out, changed := overwriteProviderModels(existing, fetched)
	if !changed {
		t.Fatal("expected changed=true when order differs")
	}
	if out[0].(map[string]any)["id"] != "a" || out[1].(map[string]any)["id"] != "b" {
		t.Error("expected result order to follow fetched order")
	}
}

func TestOverwriteAgentAutonomousModels_PurgesStaleAndLegacy(t *testing.T) {
	existing := map[string]any{
		"autonomous/old-stale":       map[string]any{},
		"claude-haiku-4-5":           map[string]any{},
		"venice/some-model":          map[string]any{},
		"autonomous/claude-opus-4-6": map[string]any{"params": "keep"},
	}
	fetched := []domain.LLMModel{
		{Key: "claude-opus-4-6"},
		{Key: "claude-haiku-4-5"},
	}

	out, changed := overwriteAgentAutonomousModels(existing, fetched)
	if !changed {
		t.Fatal("expected changed=true")
	}
	if _, ok := out["autonomous/old-stale"]; ok {
		t.Error("stale autonomous/* key should be removed")
	}
	if _, ok := out["claude-haiku-4-5"]; ok {
		t.Error("legacy unprefixed key should be removed")
	}
	if _, ok := out["venice/some-model"]; !ok {
		t.Error("other-provider key must be preserved")
	}
	if _, ok := out["autonomous/claude-opus-4-6"]; !ok {
		t.Error("wanted autonomous/* key must be kept")
	}
	if _, ok := out["autonomous/claude-haiku-4-5"]; !ok {
		t.Error("missing wanted autonomous/* key must be added")
	}
}

func TestApplyDefaultPrimaryModel(t *testing.T) {
	cfg := map[string]any{}
	if !applyDefaultPrimaryModel(cfg, "claude-opus-4-6") {
		t.Fatal("expected change on first set")
	}
	if got := extractPrimaryModel(cfg); got != "autonomous/claude-opus-4-6" {
		t.Errorf("primary = %q; want autonomous/claude-opus-4-6", got)
	}
	if applyDefaultPrimaryModel(cfg, "claude-opus-4-6") {
		t.Error("expected no change when already set")
	}
	if applyDefaultPrimaryModel(cfg, "") {
		t.Error("expected no change for empty model")
	}
}

func TestApplyDefaultImageModel(t *testing.T) {
	cfg := map[string]any{}
	if !applyDefaultImageModel(cfg, "claude-opus-4-6") {
		t.Fatal("expected change on first set")
	}
	if got := extractImageModel(cfg); got != "autonomous/claude-opus-4-6" {
		t.Errorf("imageModel = %q; want autonomous/claude-opus-4-6", got)
	}
	if applyDefaultImageModel(cfg, "") {
		t.Error("expected no change for empty image model")
	}
}

func TestIsDefaultsOnAutonomous(t *testing.T) {
	if !isDefaultsOnAutonomous(map[string]any{}) {
		t.Error("missing primary should be treated as on-autonomous (seedable)")
	}
	onVenice := map[string]any{}
	applyDefaultPrimaryModel(onVenice, "x")
	onVenice["agents"].(map[string]any)["defaults"].(map[string]any)["model"].(map[string]any)["primary"] = "venice/foo"
	if isDefaultsOnAutonomous(onVenice) {
		t.Error("venice/* primary must NOT be treated as on-autonomous")
	}
}
