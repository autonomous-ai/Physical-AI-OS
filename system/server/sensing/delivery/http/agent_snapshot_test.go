package http

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
)

// Driven through a real engine: CreateTestContext would always report 200.
func serveSnapshot(t *testing.T, runtime, source, name string) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/sensing/agent-snapshot/:runtime/:source/:name",
		(&SensingHandler{}).GetAgentSnapshot)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet,
		"/api/sensing/agent-snapshot/"+runtime+"/"+source+"/"+name, nil))
	return rec.Code
}

// The runtime allow-list matches camera_snapshot.go and hal/config.py.
func TestGetAgentSnapshotServesEveryRuntimeHALWritesTo(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)

	for _, runtime := range []string{
		"openclaw", "hermes", "picoclaw", "claudecode", "opencode",
	} {
		dir := filepath.Join(home, "."+runtime, "media", "hal-snapshots")
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "snap_1.jpg"), []byte("jpeg"), 0o644); err != nil {
			t.Fatal(err)
		}
		if code := serveSnapshot(t, runtime, "media-hal-snapshots", "snap_1.jpg"); code != http.StatusOK {
			t.Errorf("%s: got status %d, want 200 — this runtime is not served", runtime, code)
		}
	}
}

// An unknown runtime segment never reaches the filesystem.
func TestGetAgentSnapshotRejectsAnUnknownRuntime(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)
	dir := filepath.Join(home, ".evilruntime", "media", "hal-snapshots")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "snap_1.jpg"), []byte("jpeg"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := serveSnapshot(t, "evilruntime", "media-hal-snapshots", "snap_1.jpg"); code != http.StatusNotFound {
		t.Errorf("an unlisted runtime was served: status %d", code)
	}
}

// Traversal must not escape the snapshot directory.
func TestGetAgentSnapshotRejectsATraversingName(t *testing.T) {
	home := t.TempDir()
	t.Setenv("OS_AGENT_HOME", home)
	if err := os.WriteFile(filepath.Join(home, "secret.jpg"), []byte("jpeg"), 0o644); err != nil {
		t.Fatal(err)
	}
	if code := serveSnapshot(t, "codex", "media-hal-snapshots", "..%2f..%2fsecret.jpg"); code == http.StatusOK {
		t.Error("a traversing name escaped the snapshot directory")
	}
}
