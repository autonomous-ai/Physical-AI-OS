package system

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadAgentEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	content := "# managed env\n" +
		"ANTHROPIC_BASE_URL=https://campaign-api.autonomous.ai/api/v1/ai\n" +
		"ANTHROPIC_API_KEY = \"sk-secret\"\n" +
		"junk line without equals\n" +
		"\n" +
		"ANTHROPIC_MODEL=Auto-AI\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write env: %v", err)
	}

	got := loadAgentEnv(path)
	want := map[string]string{
		"ANTHROPIC_BASE_URL": "https://campaign-api.autonomous.ai/api/v1/ai",
		"ANTHROPIC_API_KEY":  "sk-secret", // trimmed + quotes stripped
		"ANTHROPIC_MODEL":    "Auto-AI",
		"IS_SANDBOX":         "1", // appended when the file loads
	}
	if len(got) != len(want) {
		t.Fatalf("got %d entries %v, want %d", len(got), got, len(want))
	}
	for _, kv := range got {
		i := len(kv)
		for j := 0; j < len(kv); j++ {
			if kv[j] == '=' {
				i = j
				break
			}
		}
		k, v := kv[:i], kv[i+1:]
		if want[k] != v {
			t.Fatalf("entry %s=%s, want %q", k, v, want[k])
		}
	}
}

func TestLoadAgentEnvAbsentOrEmpty(t *testing.T) {
	if got := loadAgentEnv(""); got != nil {
		t.Fatalf("empty path should yield nil, got %v", got)
	}
	if got := loadAgentEnv(filepath.Join(t.TempDir(), "nope.env")); got != nil {
		t.Fatalf("missing file should yield nil, got %v", got)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "blank.env")
	if err := os.WriteFile(path, []byte("# only a comment\n\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if got := loadAgentEnv(path); got != nil {
		t.Fatalf("comment-only file should yield nil, got %v", got)
	}
}
