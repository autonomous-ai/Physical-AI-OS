package http

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func removeSampleRequest(root, name, file string) *httptest.ResponseRecorder {
	h := &SensingHandler{}
	router := gin.New()
	router.POST("/remove", func(c *gin.Context) { h.removeVoiceFile(c, root) })
	body, _ := json.Marshal(VoiceFileRemoveRequest{Name: name, File: file})
	req := httptest.NewRequest(http.MethodPost, "/remove", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	out := httptest.NewRecorder()
	router.ServeHTTP(out, req)
	return out
}

func makeVoiceFixture(t *testing.T, root, name string) string {
	t.Helper()
	dir := filepath.Join(root, name, "voice")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for _, file := range []string{"sample.wav", "sample.npy", "keep.wav"} {
		if err := os.WriteFile(filepath.Join(dir, file), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestVoiceRemovalRejectsProfileTraversal(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "users")
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	outside := makeVoiceFixture(t, base, "outside")
	for _, name := range []string{"../outside", "..", ".", "/tmp/outside", `..\outside`, "bad\x00name"} {
		out := removeSampleRequest(root, name, "sample.wav")
		if out.Code != 400 {
			t.Fatalf("name %q: %d %s", name, out.Code, out.Body.String())
		}
	}
	if _, err := os.Stat(filepath.Join(outside, "sample.wav")); err != nil {
		t.Fatal("outside sample changed", err)
	}
}

func TestVoiceRemovalRejectsSymlinkEscapes(t *testing.T) {
	for _, kind := range []string{"profile", "voice", "sample"} {
		t.Run(kind, func(t *testing.T) {
			base := t.TempDir()
			root := filepath.Join(base, "users")
			outside := makeVoiceFixture(t, base, "outside")
			if err := os.Mkdir(root, 0700); err != nil {
				t.Fatal(err)
			}
			var link, target string
			switch kind {
			case "profile":
				link, target = filepath.Join(root, "alice"), filepath.Dir(outside)
			case "voice":
				if err := os.Mkdir(filepath.Join(root, "alice"), 0700); err != nil {
					t.Fatal(err)
				}
				link, target = filepath.Join(root, "alice", "voice"), outside
			case "sample":
				dir := makeVoiceFixture(t, root, "alice")
				link, target = filepath.Join(dir, "escape.wav"), filepath.Join(outside, "sample.wav")
			}
			if err := os.Symlink(target, link); err != nil {
				t.Fatal(err)
			}
			file := "sample.wav"
			if kind == "sample" {
				file = "escape.wav"
			}
			out := removeSampleRequest(root, "alice", file)
			if out.Code != 400 {
				t.Fatalf("got %d %s", out.Code, out.Body.String())
			}
			if _, err := os.Stat(filepath.Join(outside, "sample.wav")); err != nil {
				t.Fatal("outside sample changed", err)
			}
		})
	}
}

func TestVoiceRemovalDeletesOnlySelectedSampleAndEmbedding(t *testing.T) {
	root := t.TempDir()
	dir := makeVoiceFixture(t, root, "alice smith")
	out := removeSampleRequest(root, " Alice Smith ", "sample.wav")
	if out.Code != 200 {
		t.Fatalf("got %d %s", out.Code, out.Body.String())
	}
	for _, file := range []string{"sample.wav", "sample.npy"} {
		if _, err := os.Stat(filepath.Join(dir, file)); !os.IsNotExist(err) {
			t.Fatalf("%s not deleted", file)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "keep.wav")); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceRemovalPreservesNonAudioAndMissingSamples(t *testing.T) {
	root := t.TempDir()
	dir := makeVoiceFixture(t, root, "alice")
	if err := os.Mkdir(filepath.Join(dir, "directory.wav"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		file   string
		status int
	}{{"sample.npy", 400}, {"../sample.wav", 400}, {"directory.wav", 400}, {"missing.wav", 404}} {
		out := removeSampleRequest(root, "alice", tc.file)
		if out.Code != tc.status {
			t.Fatalf("%s: %d", tc.file, out.Code)
		}
	}
}

func TestVoiceDirectoryHandleSurvivesPathReplacement(t *testing.T) {
	root := t.TempDir()
	dir := makeVoiceFixture(t, root, "alice")
	outside := makeVoiceFixture(t, t.TempDir(), "outside")
	handle, err := openVoiceDirectory(root, "alice")
	if err != nil {
		t.Fatal(err)
	}
	defer handle.Close()
	moved := dir + "-moved"
	if err = os.Rename(dir, moved); err != nil {
		t.Fatal(err)
	}
	if err = os.Symlink(outside, dir); err != nil {
		t.Fatal(err)
	}
	if err = handle.Remove("sample.wav"); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(outside, "sample.wav")); err != nil {
		t.Fatal("replacement target changed", err)
	}
	if _, err = os.Stat(filepath.Join(moved, "sample.wav")); !os.IsNotExist(err) {
		t.Fatal("original sample not removed")
	}
}

type voiceRemovalTransport func(*http.Request) (*http.Response, error)

func (f voiceRemovalTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestLastVoiceSampleStillRemovesProfile(t *testing.T) {
	root := t.TempDir()
	dir := makeVoiceFixture(t, root, "alice")
	if err := os.Remove(filepath.Join(dir, "keep.wav")); err != nil {
		t.Fatal(err)
	}
	old := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = old })
	calls := 0
	http.DefaultTransport = voiceRemovalTransport(func(req *http.Request) (*http.Response, error) {
		calls++
		if req.URL.String() != "http://127.0.0.1:5001/speaker/remove" {
			t.Fatalf("unexpected URL %s", req.URL)
		}
		var body map[string]string
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["name"] != "alice" {
			t.Fatal(body)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	})
	out := removeSampleRequest(root, "alice", "sample.wav")
	if out.Code != 200 || calls != 1 {
		t.Fatalf("status=%d calls=%d", out.Code, calls)
	}
}
