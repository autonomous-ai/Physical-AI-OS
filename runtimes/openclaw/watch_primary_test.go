package openclaw

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSplitProviderModel(t *testing.T) {
	tests := []struct {
		input    string
		provider string
		key      string
		ok       bool
	}{
		{"autonomous/claude-opus-4-6", "autonomous", "claude-opus-4-6", true},
		{"openai-codex/gpt-5.5", "openai-codex", "gpt-5.5", true},
		{"autonomous/meta/llama-3", "autonomous", "meta/llama-3", true},
		{"claude-opus-4-6", "", "claude-opus-4-6", false},
		{"", "", "", false},
	}
	for _, tc := range tests {
		prov, key, ok := splitProviderModel(tc.input)
		if ok != tc.ok || prov != tc.provider || key != tc.key {
			t.Errorf("splitProviderModel(%q) = (%q, %q, %v); want (%q, %q, %v)",
				tc.input, prov, key, ok, tc.provider, tc.key, tc.ok)
		}
	}
}

func TestExtractPrimaryModel(t *testing.T) {
	cfg := map[string]any{
		"agents": map[string]any{
			"defaults": map[string]any{
				"model": map[string]any{
					"primary": "autonomous/claude-opus-4-6",
				},
			},
		},
	}
	got := extractPrimaryModel(cfg)
	if got != "autonomous/claude-opus-4-6" {
		t.Errorf("extractPrimaryModel = %q; want %q", got, "autonomous/claude-opus-4-6")
	}
}

func TestExtractPrimaryModel_Missing(t *testing.T) {
	cases := []map[string]any{
		{},
		{"agents": map[string]any{}},
		{"agents": map[string]any{"defaults": map[string]any{}}},
		{"agents": map[string]any{"defaults": map[string]any{"model": map[string]any{}}}},
	}
	for _, cfg := range cases {
		if got := extractPrimaryModel(cfg); got != "" {
			t.Errorf("expected empty string for %v, got %q", cfg, got)
		}
	}
}

// TestLampWriteFlag_ContentMatch: flag must match the primary written
func TestLampWriteFlag_ContentMatch(t *testing.T) {
	dir := t.TempDir()

	if isOSWrite(dir, "autonomous/claude-opus-4-6") {
		t.Fatal("expected no match before setOSWriteFlag")
	}

	setOSWriteFlag(dir, "autonomous/claude-opus-4-6")

	if !isOSWrite(dir, "autonomous/claude-opus-4-6") {
		t.Fatal("expected match after setOSWriteFlag with same primary")
	}

	// Different primary within 3 s window → NOT a Lamp write (external race).
	if isOSWrite(dir, "autonomous/claude-haiku-4-5") {
		t.Fatal("expected mismatch: flag carries opus but file now has haiku")
	}

	clearOSWriteFlag(dir)
	if isOSWrite(dir, "autonomous/claude-opus-4-6") {
		t.Fatal("expected no match after clearOSWriteFlag")
	}
}

// TestLampWriteFlag_Expired: expired flag is never a match regardless of content.
func TestLampWriteFlag_Expired(t *testing.T) {
	dir := t.TempDir()
	flagPath := filepath.Join(dir, osWriteFlagName)

	setOSWriteFlag(dir, "autonomous/claude-opus-4-6")

	past := time.Now().Add(-(osWriteFlagWindow + time.Second))
	if err := os.Chtimes(flagPath, past, past); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	if isOSWrite(dir, "autonomous/claude-opus-4-6") {
		t.Fatal("expected flag to be expired after back-dating mtime")
	}
}
