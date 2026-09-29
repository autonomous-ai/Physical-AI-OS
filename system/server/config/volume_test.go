package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPersistedVolumeHardwareProfileIsolation(t *testing.T) {
	for _, tc := range []struct {
		name    string
		profile string
		missing bool
		want    int
		ok      bool
	}{
		{"missing", "", true, 30, true},
		{"empty", "", false, 30, true},
		{"standard", "standard\n", false, 30, true},
		{"pro", "pro\n", false, 77, true},
		{"other hardware", "audio-v2_test", false, 65, true},
		{"new hardware ignores legacy", "new", false, 0, false},
		{"path traversal", "../pro", false, 0, false},
		{"embedded newline", "pro\nstandard", false, 0, false},
		{"uppercase", "Pro", false, 0, false},
		{"too long", strings.Repeat("a", 65), false, 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			profilePath := filepath.Join(dir, "hardware-profile")
			statePath := filepath.Join(dir, ".volume")
			files := map[string]string{
				statePath:                    "30",
				statePath + "-pro":           "77",
				statePath + "-audio-v2_test": "65",
			}
			if !tc.missing {
				files[profilePath] = tc.profile
			}
			for path, value := range files {
				if err := os.WriteFile(path, []byte(value), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if value, ok := persistedVolume(profilePath, statePath); value != tc.want || ok != tc.ok {
				t.Fatalf("got (%d, %v), want (%d, %v)", value, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestPersistedVolumeUnreadableProfileDoesNotUseLegacy(t *testing.T) {
	dir := t.TempDir()
	statePath := filepath.Join(dir, ".volume")
	if err := os.WriteFile(statePath, []byte("30"), 0600); err != nil {
		t.Fatal(err)
	}
	if value, ok := persistedVolume(dir, statePath); ok {
		t.Fatalf("unreadable profile restored legacy volume: (%d, %v)", value, ok)
	}
}
