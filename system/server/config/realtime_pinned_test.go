package config

import (
	"os"
	"path/filepath"
	"testing"
)

// Un-pinned realtime blocks follow code defaults; pinned ones are left alone.
func TestProvideConfig_RealtimeReseedUnlessPinned(t *testing.T) {
	origPath := configPath
	configPath = filepath.Join(t.TempDir(), "config.json")
	defer func() { configPath = origPath }()

	for _, tc := range []struct {
		name string
		file string
		want string
	}{
		{"unpinned follows default", `{"realtime":{"provider":"openai"}}`, "gemini"},
		{"pinned kept", `{"realtime":{"provider":"openai","pinned":true}}`, "openai"},
	} {
		if err := os.WriteFile(configPath, []byte(tc.file), 0o600); err != nil {
			t.Fatal(err)
		}
		if got := ProvideConfig().RealtimeProvider(); got != tc.want {
			t.Errorf("%s: provider = %q, want %q", tc.name, got, tc.want)
		}
	}
}
