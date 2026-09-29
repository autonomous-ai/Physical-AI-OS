package opencode

import (
	"testing"
	"time"
)

// Discovery is intentionally degraded: opencode stores sessions under
// ~/.local/share/opencode in an internal format with no on-disk cwd we can
// parse, so allCodingSessions (and everything built on it) is empty until wired
// up on-device.
func TestAllCodingSessionsDegraded(t *testing.T) {
	s := &OpenCodeService{}
	if got := s.allCodingSessions(); got != nil {
		t.Fatalf("allCodingSessions() = %v, want nil", got)
	}
	if got := s.codingFolders(); len(got) != 0 {
		t.Fatalf("codingFolders() = %v, want empty", got)
	}
	if got := s.folderSessions("/root/app"); len(got) != 0 {
		t.Fatalf("folderSessions(/root/app) = %v, want empty", got)
	}
	if _, ok := s.latestSessionForFolder("/root/app"); ok {
		t.Fatal("latestSessionForFolder should be absent while discovery is degraded")
	}
}

func TestCodingSessionLabel(t *testing.T) {
	if got := (codingSession{}).label(); got != "(no description)" {
		t.Errorf("empty label = %q, want '(no description)'", got)
	}
	cs := codingSession{Recent: []string{"newest", "older"}}
	if got := cs.label(); got != "newest" {
		t.Errorf("label = %q, want 'newest'", got)
	}
}

func TestNormalizeFolder(t *testing.T) {
	cases := map[string]string{
		"/root/test":     "/root/test",
		"/root/test/":    "/root/test",
		"test":           "/root/test",
		"~/proj":         "/root/proj",
		"~":              "/root",
		`"/root/a b"`:    "/root/a b",
		"/root/./x/../y": "/root/y",
	}
	for in, want := range cases {
		if got := normalizeFolder(in); got != want {
			t.Errorf("normalizeFolder(%q) = %q, want %q", in, got, want)
		}
	}
	if got := normalizeFolder("   "); got != "" {
		t.Errorf("normalizeFolder(blank) = %q, want empty", got)
	}
}

func TestOneLine(t *testing.T) {
	cases := map[string]string{
		"a  b\n c":             "a b c",
		"  hello  ":            "hello",
		"":                     "",
		"one":                  "one",
		"line1\nline2\t line3": "line1 line2 line3",
	}
	for in, want := range cases {
		if got := oneLine(in); got != want {
			t.Errorf("oneLine(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestHumanizeAgo(t *testing.T) {
	now := time.Now()
	cases := []struct {
		t    time.Time
		want string
	}{
		{now.Add(-30 * time.Second), "just now"},
		{now.Add(-5 * time.Minute), "5m ago"},
		{now.Add(-3 * time.Hour), "3h ago"},
		{now.Add(-2 * 24 * time.Hour), "2d ago"},
	}
	for _, c := range cases {
		if got := humanizeAgo(c.t); got != c.want {
			t.Errorf("humanizeAgo(%v) = %q, want %q", c.t, got, c.want)
		}
	}
}
