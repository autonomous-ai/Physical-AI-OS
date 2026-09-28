package agentfile

import (
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// seedFile writes a file under dir and returns its path.
func seedFile(t *testing.T, dir, name string, size int) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, make([]byte, size), 0644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestResolveServesAllowedFile(t *testing.T) {
	root := t.TempDir()
	img := seedFile(t, root, "media/snap.jpg", 8)

	got, ct, err := Resolve(img, []string{filepath.Join(root, "media")})
	if err != nil {
		t.Fatalf("want served, got %v", err)
	}
	// t.TempDir() may sit under a symlink (/var -> /private/var on macOS).
	resolved, _ := filepath.EvalSymlinks(img)
	if got != resolved {
		t.Errorf("path = %q, want %q", got, resolved)
	}
	if ct != "image/jpeg" {
		t.Errorf("content type = %q", ct)
	}
}

// The extension whitelist is checked before the filesystem.
func TestResolveRejectsUnservedTypes(t *testing.T) {
	root := t.TempDir()
	roots := []string{root}

	for _, name := range []string{"openclaw.json", "agent.log", "id_rsa", "run.sh", "archive.zip"} {
		p := seedFile(t, root, name, 4)
		if _, _, err := Resolve(p, roots); !errors.Is(err, ErrType) {
			t.Errorf("%s: err = %v, want ErrType", name, err)
		}
	}
	// Absent files get the same error, so presence cannot be probed.
	if _, _, err := Resolve(filepath.Join(root, "absent.json"), roots); !errors.Is(err, ErrType) {
		t.Errorf("absent .json: err = %v, want ErrType", err)
	}
}

// `..` must not walk out of an allow-listed root.
func TestResolveRejectsTraversal(t *testing.T) {
	base := t.TempDir()
	allowed := filepath.Join(base, "media")
	secret := seedFile(t, base, "secret.txt", 4)
	seedFile(t, allowed, "keep.txt", 4)

	escape := filepath.Join(allowed, "..", "secret.txt")
	if _, _, err := Resolve(escape, []string{allowed}); !errors.Is(err, ErrOutsideRoots) {
		t.Fatalf("traversal err = %v, want ErrOutsideRoots", err)
	}
	if _, _, err := Resolve(secret, []string{base}); err != nil {
		t.Fatalf("control case failed: %v", err)
	}
}

// A symlink INSIDE a root pointing out of it is the classic /tmp attack.
func TestResolveRejectsSymlinkEscape(t *testing.T) {
	base := t.TempDir()
	allowed := filepath.Join(base, "media")
	outside := seedFile(t, base, "outside.txt", 4)
	if err := os.MkdirAll(allowed, 0755); err != nil {
		t.Fatal(err)
	}

	link := filepath.Join(allowed, "innocent.txt")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, _, err := Resolve(link, []string{allowed}); !errors.Is(err, ErrOutsideRoots) {
		t.Errorf("symlink escape err = %v, want ErrOutsideRoots", err)
	}
}

// A sibling whose name merely starts with the root's must not pass.
func TestResolveRejectsRootPrefixLookalike(t *testing.T) {
	base := t.TempDir()
	allowed := filepath.Join(base, "media")
	if err := os.MkdirAll(allowed, 0755); err != nil {
		t.Fatal(err)
	}
	evil := seedFile(t, base, "media-evil/snap.jpg", 4)

	if _, _, err := Resolve(evil, []string{allowed}); !errors.Is(err, ErrOutsideRoots) {
		t.Errorf("lookalike root err = %v, want ErrOutsideRoots", err)
	}
}

func TestResolveRejectsDirsRelativeAndOversized(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "shots.jpg"), 0755); err != nil {
		t.Fatal(err)
	}
	roots := []string{root}

	// A directory that happens to carry a served extension is not a file.
	if _, _, err := Resolve(filepath.Join(root, "shots.jpg"), roots); !errors.Is(err, ErrNotFound) {
		t.Errorf("directory err = %v, want ErrNotFound", err)
	}
	// Relative paths never reach the filesystem.
	if _, _, err := Resolve("media/snap.jpg", roots); !errors.Is(err, ErrNotFound) {
		t.Errorf("relative err = %v, want ErrNotFound", err)
	}
	if _, _, err := Resolve("", roots); !errors.Is(err, ErrNotFound) {
		t.Errorf("empty err = %v, want ErrNotFound", err)
	}
	// Over the size cap is refused rather than streamed.
	big := seedFile(t, root, "big.jpg", MaxBytes+1)
	if _, _, err := Resolve(big, roots); !errors.Is(err, ErrNotFound) {
		t.Errorf("oversized err = %v, want ErrNotFound", err)
	}
}

// A root that doesn't exist on this device must not make everything pass.
func TestResolveMissingRootAllowsNothing(t *testing.T) {
	root := t.TempDir()
	img := seedFile(t, root, "snap.jpg", 4)

	if _, _, err := Resolve(img, []string{filepath.Join(root, "nope")}); !errors.Is(err, ErrOutsideRoots) {
		t.Errorf("err = %v, want ErrOutsideRoots", err)
	}
}

// Roots cover HAL snapshots and /tmp but never a runtime's config dir.
func TestRootsScope(t *testing.T) {
	roots := Roots()
	has := func(want string) bool {
		for _, r := range roots {
			if r == want {
				return true
			}
		}
		return false
	}

	for _, want := range []string{"/root/.openclaw/media", "/root/.openclaw/workspace", "/tmp"} {
		if !has(want) {
			t.Errorf("missing root %q", want)
		}
	}
	for _, unwanted := range []string{"/root/.openclaw", "/root", "/"} {
		if has(unwanted) {
			t.Errorf("root %q must not be served", unwanted)
		}
	}
}

// Scan finds paths in replies, tool args and tool results, only under served roots.
func TestScan(t *testing.T) {
	cases := []struct {
		name string
		text string
		want []string
	}{
		{
			"typed into the reply",
			"Đây nè cậu: /root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg",
			[]string{"/root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg"},
		},
		{
			"inside tool args json",
			`{"action":"send","message":"ảnh đây","media":"/root/.openclaw/media/hal-snapshots/snap_1.jpg"}`,
			[]string{"/root/.openclaw/media/hal-snapshots/snap_1.jpg"},
		},
		{
			"inside a tool result",
			`Tool Bash done: {"path": "/root/.hermes/workspace/report.pdf"}`,
			[]string{"/root/.hermes/workspace/report.pdf"},
		},
		{
			"tmp scratch output",
			"wrote /tmp/chart.png",
			[]string{"/tmp/chart.png"},
		},
		{
			"trailing sentence punctuation is not part of the name",
			"saved at /tmp/a.png, then /tmp/b.png.",
			[]string{"/tmp/a.png", "/tmp/b.png"},
		},
		{
			"same path twice yields one",
			"/tmp/a.png and again /tmp/a.png",
			[]string{"/tmp/a.png"},
		},
		{
			"outside the roots is not a candidate",
			"see /etc/hosts.txt and /usr/share/doc/readme.md and /root/.openclaw/openclaw.json",
			nil,
		},
		{
			"served roots but unserved extension",
			"/tmp/creds.json and /root/.openclaw/workspace/agent.log",
			nil,
		},
		{"empty", "", nil},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Scan(tc.text)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("[%d] got %q, want %q", i, got[i], tc.want[i])
				}
			}
		})
	}
}

// Scan only proposes candidates; Resolve still decides.
func TestScanIsCandidatesOnly(t *testing.T) {
	got := Scan("/tmp/definitely-not-here-9f3a.png")
	if len(got) != 1 {
		t.Fatalf("want the candidate, got %v", got)
	}
	if _, _, err := Resolve(got[0], Roots()); err == nil {
		t.Error("a scanned path that doesn't exist must still fail Resolve")
	}
}

// Only a plain short client extension may be used.
func TestSafeExt(t *testing.T) {
	cases := []struct{ name, want string }{
		{"report.pdf", ".pdf"},
		{"photo.JPG", ".jpg"},
		{"notes.md", ".md"},
		{"archive.tar.gz", ".gz"}, // last suffix only
		{"noext", ".bin"},
		{"", ".bin"},
		{"trailing.", ".bin"},
		{"../../etc/passwd", ".bin"}, // no extension survives Base()
		{"evil.jpg/../../../etc/shadow", ".bin"},
		{"weird.a b", ".bin"},        // space is not an extension char
		{"long.abcdefghijk", ".bin"}, // over 8 chars
		{"dotfile.p", ".p"},
	}
	for _, tc := range cases {
		if got := SafeExt(tc.name); got != tc.want {
			t.Errorf("SafeExt(%q) = %q, want %q", tc.name, got, tc.want)
		}
	}
}

// Attachments keep their real extension.
func TestSaveInboundKeepsRealExtension(t *testing.T) {
	dir := t.TempDir()
	content := base64.StdEncoding.EncodeToString([]byte("%PDF-1.4 hello"))

	path, err := SaveInbound(dir, "quarterly report.pdf", content, 1785393455291)
	if err != nil {
		t.Fatalf("save: %v", err)
	}
	if filepath.Ext(path) != ".pdf" {
		t.Errorf("path = %q, want a .pdf suffix", path)
	}
	if strings.Contains(filepath.Base(path), "quarterly") {
		t.Errorf("client filename leaked into %q", path)
	}
	if filepath.Dir(path) != dir {
		t.Errorf("wrote outside dir: %q", path)
	}
	body, err := os.ReadFile(path)
	if err != nil || string(body) != "%PDF-1.4 hello" {
		t.Errorf("content = %q, err %v", body, err)
	}
}

// A hostile name must not steer the write out of dir.
func TestSaveInboundIgnoresHostileNames(t *testing.T) {
	dir := t.TempDir()
	content := base64.StdEncoding.EncodeToString([]byte("x"))

	for _, name := range []string{"../../etc/passwd", "/etc/shadow", "a/../../b.jpg", ""} {
		path, err := SaveInbound(dir, name, content, 1)
		if err != nil {
			t.Fatalf("%q: %v", name, err)
		}
		if filepath.Dir(path) != dir {
			t.Errorf("%q escaped to %q", name, path)
		}
	}
}

func TestSaveInboundRejectsBadInput(t *testing.T) {
	dir := t.TempDir()

	if _, err := SaveInbound(dir, "a.jpg", "", 1); err == nil {
		t.Error("empty content must fail")
	}
	if _, err := SaveInbound(dir, "a.jpg", "not!base64!", 1); err == nil {
		t.Error("undecodable content must fail")
	}
	big := base64.StdEncoding.EncodeToString(make([]byte, InboundMaxBytes+1))
	if _, err := SaveInbound(dir, "a.jpg", big, 1); err == nil {
		t.Error("oversized attachment must fail")
	}
}
