package mqtthandler

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

// seedServedFile writes a file under the allow-listed /tmp root.
func seedServedFile(t *testing.T, name string, size int) string {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "chatfile-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, make([]byte, size), 0644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestBuildChatFileServesAllowedFile(t *testing.T) {
	img := seedServedFile(t, "snap.jpg", 64)

	got, err := buildChatFile(domain.MQTTChatFileGetData{
		Path: img, SessionID: "sess1", RunID: "run1",
	})
	if err != nil {
		t.Fatalf("want served, got %v", err)
	}
	if got.Name != "snap.jpg" || got.MIME != "image/jpeg" || got.Size != 64 {
		t.Errorf("metadata = %+v", got)
	}
	if got.TooLarge {
		t.Error("64 bytes must not be flagged too large")
	}
	raw, derr := base64.StdEncoding.DecodeString(got.Content)
	if derr != nil || len(raw) != 64 {
		t.Errorf("content decode = %d bytes, err %v", len(raw), derr)
	}
	if got.RunID != "run1" || got.SessionID != "sess1" || got.Path != img {
		t.Errorf("correlation = %+v", got)
	}
}

// Past the inline budget the metadata still comes back.
func TestBuildChatFileMarksOversized(t *testing.T) {
	big := seedServedFile(t, "clip.mp4", chatFileMaxInlineBytes+1)

	got, err := buildChatFile(domain.MQTTChatFileGetData{Path: big})
	if err != nil {
		t.Fatalf("oversized file must still answer with metadata: %v", err)
	}
	if !got.TooLarge {
		t.Error("want too_large set")
	}
	if got.Content != "" {
		t.Error("oversized file must not carry bytes")
	}
	if got.Size <= chatFileMaxInlineBytes {
		t.Errorf("size = %d, want the real size reported anyway", got.Size)
	}
}

// `path` is client-supplied.
func TestBuildChatFileRefusals(t *testing.T) {
	unserved := seedServedFile(t, "creds.json", 16) // served root, unserved type
	dirWithExt := filepath.Join(t.TempDir(), "shots.jpg")
	if err := os.MkdirAll(dirWithExt, 0755); err != nil {
		t.Fatal(err)
	}

	cases := []struct{ name, path string }{
		{"unserved extension in a served root", unserved},
		{"runtime config json", "/root/.openclaw/openclaw.json"},
		{"outside every root", "/etc/shadow"},
		{"traversal out of a root", "/root/.openclaw/media/../openclaw.json"},
		{"absent file", "/tmp/definitely-not-here-9f3a.png"},
		{"relative path", "media/snap.jpg"},
		{"empty", ""},
		{"directory carrying a served extension", dirWithExt},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := buildChatFile(domain.MQTTChatFileGetData{Path: tc.path})
			if err == nil {
				t.Fatalf("want refusal, got %+v", got)
			}
			if got.Content != "" {
				t.Error("a refusal must never carry bytes")
			}
			if err.Error() != "file not available" {
				t.Errorf("error = %q, want the uniform refusal", err)
			}
		})
	}
}
