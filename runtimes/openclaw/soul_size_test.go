package openclaw

import (
	"os"
	"path/filepath"
	"testing"
	"unicode/utf8"
)

// soulOwnerHeadroom is left for the OS marker and the owner-editable
// "## Personal" section appended to the device soul on the device.
const soulOwnerHeadroom = 1000

// OpenClaw keeps only the first 75% and last 25% of a workspace file longer than
// bootstrapMaxChars, silently dropping the middle — on intern-v2 that removed
// the audio-tag palette, the chat no-tag rule and the reply-language rule.
func TestDeviceSoulsFitTheBootstrapCap(t *testing.T) {
	devices := repoDevicesDir(t)
	souls, err := filepath.Glob(filepath.Join(devices, "*", "SOUL.md"))
	if err != nil || len(souls) == 0 {
		t.Fatalf("no device souls under %s: %v", devices, err)
	}
	limit := bootstrapMaxChars - soulOwnerHeadroom
	for _, path := range souls {
		device := filepath.Base(filepath.Dir(path))
		b, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		n := utf8.RuneCount(b)
		if n > limit {
			t.Errorf("%s SOUL.md is %d chars; keep it under %d so OpenClaw does not cut its middle (cap %d)", device, n, limit, bootstrapMaxChars)
		}
	}
}
