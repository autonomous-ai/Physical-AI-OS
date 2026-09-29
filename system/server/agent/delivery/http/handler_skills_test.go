package http

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"errors"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// writeZip builds a zip at path from name→content pairs.
func writeZip(t *testing.T, path string, entries map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create zip: %v", err)
	}
	defer f.Close()

	w := zip.NewWriter(f)
	for name, content := range entries {
		e, err := w.Create(name)
		if err != nil {
			t.Fatalf("create entry %s: %v", name, err)
		}
		if _, err := e.Write([]byte(content)); err != nil {
			t.Fatalf("write entry %s: %v", name, err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close zip: %v", err)
	}
}

func TestExtractSkillBundle(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skill.zip")
	writeZip(t, zipPath, map[string]string{
		"my-skill/SKILL.md":            "# My Skill\n",
		"my-skill/reference/notes.md":  "notes\n",
		"my-skill/assets/icon.png":     "\x89PNG\x00\x01\x02binary",
		"my-skill/.DS_Store":           "junk",
		"__MACOSX/my-skill/._SKILL.md": "junk",
	})

	bundle, err := extractSkillBundle(zipPath, filepath.Join(dir, "unpacked"))
	if err != nil {
		t.Fatalf("extract: %v", err)
	}

	byPath := map[string]bool{}
	for _, f := range bundle.Files {
		byPath[f.Path] = true
	}
	if len(bundle.Files) != 3 {
		t.Fatalf("want 3 files (cruft filtered), got %d: %v", len(bundle.Files), byPath)
	}
	if byPath[".DS_Store"] || byPath["my-skill/.DS_Store"] {
		t.Error(".DS_Store must be filtered out")
	}

	for _, f := range bundle.Files {
		switch f.Path {
		case "my-skill/SKILL.md":
			if f.Text != "# My Skill\n" || f.Binary {
				t.Errorf("SKILL.md: text=%q binary=%v", f.Text, f.Binary)
			}
		case "my-skill/assets/icon.png":
			if !f.Binary || f.Text != "" {
				t.Errorf("icon.png must be reported as binary with no text, got binary=%v text=%q", f.Binary, f.Text)
			}
		}
	}

	// The entries must actually land on disk.
	if _, err := os.Stat(filepath.Join(dir, "unpacked", "my-skill", "SKILL.md")); err != nil {
		t.Errorf("expected SKILL.md unpacked on disk: %v", err)
	}
}

func TestExtractSkillBundleRejectsTraversal(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "evil.zip")
	writeZip(t, zipPath, map[string]string{"../escaped.md": "pwned"})

	if _, err := extractSkillBundle(zipPath, filepath.Join(dir, "unpacked")); err == nil {
		t.Fatal("expected traversal entry to be rejected")
	}
	if _, err := os.Stat(filepath.Join(dir, "escaped.md")); !os.IsNotExist(err) {
		t.Error("traversal entry escaped the unpack dir")
	}
}

func TestExtractSkillBundleEmptyArchive(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "empty.zip")
	writeZip(t, zipPath, map[string]string{})

	if _, err := extractSkillBundle(zipPath, filepath.Join(dir, "unpacked")); err == nil {
		t.Fatal("expected an empty archive to be an error")
	}
}

func TestBrowseSkills(t *testing.T) {
	gin.SetMode(gin.TestMode)

	var gotPath, gotQuery, gotLocation string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotQuery, gotLocation = r.URL.Path, r.URL.RawQuery, r.Header.Get("location")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":1,"data":{"data":[{"id":"abc","name":"design-critique","version":"1.0.43"}],"total":1}}`))
	}))
	defer srv.Close()
	t.Setenv("SKILL_STORE_BASE_URL", srv.URL)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills/browse?keyword=design&status=7", nil)

	(&AgentHandler{}).BrowseSkills(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if gotPath != "/api/v1/agent-skills" {
		t.Errorf("upstream path = %q", gotPath)
	}
	if gotLocation != "en-US" {
		t.Errorf("location header = %q, want en-US", gotLocation)
	}
	if !strings.Contains(gotQuery, "keyword=design") {
		t.Errorf("keyword not forwarded: %q", gotQuery)
	}
	// `status` can't distinguish unset from 0 upstream, so it is never forwarded.
	if strings.Contains(gotQuery, "status=") {
		t.Errorf("status must not be forwarded: %q", gotQuery)
	}

	var resp struct {
		Status int `json:"status"`
		Data   struct {
			Data []struct {
				ID   string `json:"id"`
				Name string `json:"name"`
			} `json:"data"`
			Total int64 `json:"total"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Status != 1 || len(resp.Data.Data) != 1 || resp.Data.Data[0].Name != "design-critique" {
		t.Fatalf("unexpected body: %s", rec.Body.String())
	}
}

// A catalog HTTP 200 with a non-1 status must surface as an error.
func TestBrowseSkillsUpstreamBusinessError(t *testing.T) {
	gin.SetMode(gin.TestMode)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":4001,"message":"catalog unavailable"}`))
	}))
	defer srv.Close()
	t.Setenv("SKILL_STORE_BASE_URL", srv.URL)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills/browse", nil)

	(&AgentHandler{}).BrowseSkills(c)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "catalog unavailable") {
		t.Errorf("upstream message not surfaced: %s", rec.Body.String())
	}
}

func TestSkillBundle(t *testing.T) {
	gin.SetMode(gin.TestMode)

	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skill.zip")
	writeZip(t, zipPath, map[string]string{"design-critique/SKILL.md": "# Design Critique\n"})
	archive, err := os.ReadFile(zipPath)
	if err != nil {
		t.Fatalf("read zip: %v", err)
	}

	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/zip")
		_, _ = w.Write(archive)
	}))
	defer srv.Close()
	t.Setenv("SKILL_STORE_BASE_URL", srv.URL)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills/bundle?id=abc123", nil)

	(&AgentHandler{}).SkillBundle(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if gotPath != "/api/v1/agent-skills/abc123/download" {
		t.Errorf("upstream path = %q", gotPath)
	}

	var resp struct {
		Data struct {
			ID    string `json:"id"`
			Files []struct {
				Path string `json:"path"`
				Text string `json:"text"`
			} `json:"files"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Data.ID != "abc123" {
		t.Errorf("id = %q", resp.Data.ID)
	}
	if len(resp.Data.Files) != 1 || resp.Data.Files[0].Path != "design-critique/SKILL.md" {
		t.Fatalf("unexpected files: %s", rec.Body.String())
	}
	if resp.Data.Files[0].Text != "# Design Critique\n" {
		t.Errorf("text = %q", resp.Data.Files[0].Text)
	}
}

func TestSkillBundleRejectsBadID(t *testing.T) {
	gin.SetMode(gin.TestMode)

	for _, id := range []string{"", "../secret", "a/b"} {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills/bundle?id="+id, nil)

		(&AgentHandler{}).SkillBundle(c)

		if rec.Code != http.StatusBadRequest {
			t.Errorf("id %q: want 400, got %d", id, rec.Code)
		}
	}
}

// fakeGateway implements only the skill methods the handlers touch.
type fakeGateway struct {
	domain.AgentGateway
	name        string
	saveErr     error
	savePath    string
	gotDraft    domain.SkillDraft
	installErr  error
	installDir  string
	gotArchive  string
	gotFallback string
	list        []domain.InstalledSkill
	listErr     error
	readFiles   []domain.SkillBundleFile
	readErr     error
	gotReadName string
	deleteErr   error
	deletePath  string
	gotDelName  string
	mdDir       string
	mdErr       error
	gotMD       []byte
}

func (f *fakeGateway) Name() string { return f.name }

func (f *fakeGateway) SaveSkill(d domain.SkillDraft) (string, error) {
	f.gotDraft = d
	return f.savePath, f.saveErr
}

func (f *fakeGateway) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	f.gotArchive, f.gotFallback = archivePath, fallbackName
	return f.installDir, f.installErr
}

func postJSON(t *testing.T, path, body string) (*httptest.ResponseRecorder, *gin.Context) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	return rec, c
}

func TestSaveSkill(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", savePath: "/root/.openclaw/workspace/skills/x/SKILL.md"}
	rec, c := postJSON(t, "/api/agent/skills",
		`{"name":"  x  ","description":" d ","instructions":" i "}`)

	(&AgentHandler{agentGateway: gw}).SaveSkill(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	// Fields must reach the runtime trimmed.
	if gw.gotDraft != (domain.SkillDraft{Name: "x", Description: "d", Instructions: "i"}) {
		t.Errorf("draft not trimmed: %+v", gw.gotDraft)
	}
	if !strings.Contains(rec.Body.String(), gw.savePath) {
		t.Errorf("path not echoed: %s", rec.Body.String())
	}
}

// A runtime with no skills dir must fail with 501, not look like a successful save.
func TestSaveSkillNotSupported(t *testing.T) {
	gw := &fakeGateway{name: "Hermes", saveErr: domain.ErrNotSupportedByRuntime}
	rec, c := postJSON(t, "/api/agent/skills", `{"name":"x","description":"d","instructions":"i"}`)

	(&AgentHandler{agentGateway: gw}).SaveSkill(c)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "Hermes") {
		t.Errorf("runtime name not surfaced: %s", rec.Body.String())
	}
}

func TestSaveSkillValidationErrorsAre400(t *testing.T) {
	for _, err := range []error{skills.ErrInvalidSkillName, skills.ErrSkillExists} {
		gw := &fakeGateway{name: "OpenClaw", saveErr: err}
		rec, c := postJSON(t, "/api/agent/skills", `{"name":"x","description":"d","instructions":"i"}`)

		(&AgentHandler{agentGateway: gw}).SaveSkill(c)

		if rec.Code != http.StatusBadRequest {
			t.Errorf("%v: want 400, got %d", err, rec.Code)
		}
	}
}

func TestSaveSkillRequiresAllFields(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw"}
	rec, c := postJSON(t, "/api/agent/skills", `{"name":"x"}`)

	(&AgentHandler{agentGateway: gw}).SaveSkill(c)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.gotDraft.Name != "" {
		t.Error("gateway must not be called on a malformed body")
	}
}

func TestInstallSkill(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skill.zip")
	writeZip(t, zipPath, map[string]string{"design-critique/SKILL.md": "body"})
	archive, err := os.ReadFile(zipPath)
	if err != nil {
		t.Fatalf("read zip: %v", err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer srv.Close()
	t.Setenv("SKILL_STORE_BASE_URL", srv.URL)

	gw := &fakeGateway{name: "OpenClaw", installDir: "/root/.openclaw/workspace/skills/design-critique"}
	rec, c := postJSON(t, "/api/agent/skills/install", `{"id":"abc123","name":"design-critique"}`)

	(&AgentHandler{agentGateway: gw}).InstallSkill(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	// The runtime receives a real on-disk archive, not the raw bytes.
	if gw.gotArchive == "" {
		t.Fatal("gateway did not receive an archive path")
	}
	if gw.gotFallback != "design-critique" {
		t.Errorf("fallback name = %q", gw.gotFallback)
	}
	if !strings.Contains(rec.Body.String(), "design-critique") {
		t.Errorf("installed name not echoed: %s", rec.Body.String())
	}
	// Temp dir is cleaned up once the response is built.
	if _, err := os.Stat(gw.gotArchive); !os.IsNotExist(err) {
		t.Error("temp archive was not removed after the response")
	}
}

func TestInstallSkillNotSupported(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skill.zip")
	writeZip(t, zipPath, map[string]string{"x/SKILL.md": "body"})
	archive, _ := os.ReadFile(zipPath)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer srv.Close()
	t.Setenv("SKILL_STORE_BASE_URL", srv.URL)

	gw := &fakeGateway{name: "Codex", installErr: domain.ErrNotSupportedByRuntime}
	rec, c := postJSON(t, "/api/agent/skills/install", `{"id":"abc"}`)

	(&AgentHandler{agentGateway: gw}).InstallSkill(c)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "Codex") {
		t.Errorf("runtime name not surfaced: %s", rec.Body.String())
	}
}

func (f *fakeGateway) ListSkills() ([]domain.InstalledSkill, error) {
	return f.list, f.listErr
}

func TestListSkills(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("limit"); got != "100" {
			t.Errorf("limit = %q, want 100", got)
		}
		switch r.URL.Query().Get("page") {
		case "1":
			_, _ = w.Write([]byte(`{"status":1,"data":{"data":[{"name":"Music","slug":"music"}],"total":101}}`))
		case "2":
			_, _ = w.Write([]byte(`{"status":1,"data":{"data":[{"name":"Local Tool","slug":"local-tool"}],"total":101}}`))
		default:
			t.Errorf("unexpected catalog page %q", r.URL.Query().Get("page"))
		}
	}))
	defer store.Close()
	t.Setenv("SKILL_STORE_BASE_URL", store.URL)
	gw := &fakeGateway{name: "OpenClaw", list: []domain.InstalledSkill{
		{Name: "music", Description: "Play music.", Files: []domain.SkillNode{{Name: "SKILL.md", Path: "music/SKILL.md"}}},
		{Name: "local-tool", Files: []domain.SkillNode{{Name: "SKILL.md", Path: "local-tool/SKILL.md"}}},
		{Name: "device-only", Files: []domain.SkillNode{{Name: "SKILL.md", Path: "device-only/SKILL.md"}}},
	}}
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills", nil)

	(&AgentHandler{agentGateway: gw}).ListSkills(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"music/SKILL.md"`) {
		t.Errorf("tree not serialized: %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"store_availability":"in_store"`) ||
		!strings.Contains(rec.Body.String(), `"store_availability":"device_only"`) {
		t.Errorf("store availability not serialized: %s", rec.Body.String())
	}
}

func TestListSkillsStoreUnavailableIsUnknown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer store.Close()
	t.Setenv("SKILL_STORE_BASE_URL", store.URL)
	gw := &fakeGateway{name: "OpenClaw", list: []domain.InstalledSkill{{Name: "music"}}}
	rec, c := getReq(t, "/api/agent/skills")

	(&AgentHandler{agentGateway: gw}).ListSkills(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"store_availability":"unknown"`) {
		t.Errorf("unavailable store must be unknown: %s", rec.Body.String())
	}
}

// A runtime with no skills dir must be distinguishable from an empty one.
func TestListSkillsNotSupported(t *testing.T) {
	gin.SetMode(gin.TestMode)
	gw := &fakeGateway{name: "PicoClaw", listErr: domain.ErrNotSupportedByRuntime}
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills", nil)

	(&AgentHandler{agentGateway: gw}).ListSkills(c)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "PicoClaw") {
		t.Errorf("runtime name not surfaced: %s", rec.Body.String())
	}
}

// A nil slice must serialize as [] so the UI doesn't have to handle null.
func TestListSkillsEmptyIsArray(t *testing.T) {
	gin.SetMode(gin.TestMode)
	gw := &fakeGateway{name: "OpenClaw"}
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/agent/skills", nil)

	(&AgentHandler{agentGateway: gw}).ListSkills(c)

	if !strings.Contains(rec.Body.String(), `"data":[]`) {
		t.Errorf("empty list must serialize as []: %s", rec.Body.String())
	}
}

func (f *fakeGateway) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	f.gotReadName = name
	return f.readFiles, f.readErr
}
func (f *fakeGateway) ExportSkillArchive(name, destDir string) (string, error) {
	return "", domain.ErrNotSupportedByRuntime
}

func getReq(t *testing.T, path string) (*httptest.ResponseRecorder, *gin.Context) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, path, nil)
	return rec, c
}

func TestReadSkillFilesHandler(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", readFiles: []domain.SkillBundleFile{
		{Path: "music/SKILL.md", Size: 12, Text: "# Music"},
	}}
	rec, c := getReq(t, "/api/agent/skills/files?name=music")

	(&AgentHandler{agentGateway: gw}).ReadSkillFiles(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.gotReadName != "music" {
		t.Errorf("name = %q", gw.gotReadName)
	}
	// Same SkillBundle envelope as the store preview.
	if !strings.Contains(rec.Body.String(), `"id":"music"`) ||
		!strings.Contains(rec.Body.String(), `"music/SKILL.md"`) {
		t.Errorf("unexpected body: %s", rec.Body.String())
	}
}

func TestReadSkillFilesHandlerRequiresName(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw"}
	rec, c := getReq(t, "/api/agent/skills/files")

	(&AgentHandler{agentGateway: gw}).ReadSkillFiles(c)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
	if gw.gotReadName != "" {
		t.Error("gateway must not be called without a name")
	}
}

// A stale listing pointing at a deleted skill is a 404, not a 500.
func TestReadSkillFilesHandlerMissingIs404(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", readErr: errors.New("skill \"gone\" not found")}
	rec, c := getReq(t, "/api/agent/skills/files?name=gone")

	(&AgentHandler{agentGateway: gw}).ReadSkillFiles(c)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestReadSkillFilesHandlerInvalidNameIs400(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", readErr: skills.ErrInvalidSkillName}
	rec, c := getReq(t, "/api/agent/skills/files?name=../etc")

	(&AgentHandler{agentGateway: gw}).ReadSkillFiles(c)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
}

func (f *fakeGateway) DeleteSkill(name string) (string, error) {
	f.gotDelName = name
	return f.deletePath, f.deleteErr
}

func TestDeleteSkillHandler(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", deletePath: "/root/.openclaw/workspace/skills/music"}
	rec, c := getReq(t, "/api/agent/skills?name=music")

	(&AgentHandler{agentGateway: gw}).DeleteSkill(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.gotDelName != "music" {
		t.Errorf("name = %q", gw.gotDelName)
	}
	if !strings.Contains(rec.Body.String(), gw.deletePath) {
		t.Errorf("deleted path not echoed: %s", rec.Body.String())
	}
}

// Uninstalling an already-removed skill is a 404, not a silent 200.
func TestDeleteSkillHandlerMissingIs404(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", deleteErr: skills.ErrSkillNotFound}
	rec, c := getReq(t, "/api/agent/skills?name=gone")

	(&AgentHandler{agentGateway: gw}).DeleteSkill(c)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestDeleteSkillHandlerGuards(t *testing.T) {
	// No name at all → 400, and the gateway is never called.
	gw := &fakeGateway{name: "OpenClaw"}
	rec, c := getReq(t, "/api/agent/skills")
	(&AgentHandler{agentGateway: gw}).DeleteSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("missing name: want 400, got %d", rec.Code)
	}
	if gw.gotDelName != "" {
		t.Error("gateway must not be called without a name")
	}

	// Bad shape → 400.
	gw = &fakeGateway{name: "OpenClaw", deleteErr: skills.ErrInvalidSkillName}
	rec, c = getReq(t, "/api/agent/skills?name=..%2Fetc")
	(&AgentHandler{agentGateway: gw}).DeleteSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("bad name: want 400, got %d", rec.Code)
	}

	// Runtime can't uninstall → 501 naming it.
	gw = &fakeGateway{name: "PicoClaw", deleteErr: domain.ErrNotSupportedByRuntime}
	rec, c = getReq(t, "/api/agent/skills?name=music")
	(&AgentHandler{agentGateway: gw}).DeleteSkill(c)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("unsupported: want 501, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "PicoClaw") {
		t.Errorf("runtime name not surfaced: %s", rec.Body.String())
	}
}

// postMultipart builds a multipart request with one `file` part.
func postMultipart(t *testing.T, path, filename string, content []byte) (*httptest.ResponseRecorder, *gin.Context) {
	t.Helper()
	gin.SetMode(gin.TestMode)

	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	part, err := w.CreateFormFile("file", filename)
	if err != nil {
		t.Fatalf("create part: %v", err)
	}
	if _, err := part.Write(content); err != nil {
		t.Fatalf("write part: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close writer: %v", err)
	}

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, path, &body)
	c.Request.Header.Set("Content-Type", w.FormDataContentType())
	return rec, c
}

func TestUploadSkill(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skill.zip")
	writeZip(t, zipPath, map[string]string{"design-critique/SKILL.md": "body"})
	archive, err := os.ReadFile(zipPath)
	if err != nil {
		t.Fatalf("read zip: %v", err)
	}

	gw := &fakeGateway{name: "OpenClaw", installDir: "/root/.openclaw/workspace/skills/design-critique"}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "My Skill.zip", archive)

	(&AgentHandler{agentGateway: gw}).UploadSkill(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	// The runtime gets a real on-disk archive, not the raw bytes.
	if gw.gotArchive == "" {
		t.Fatal("gateway did not receive an archive path")
	}
	// Filename stem is slugified for the fallback name.
	if gw.gotFallback != "my-skill" {
		t.Errorf("fallback = %q, want my-skill", gw.gotFallback)
	}
	// Installed name is read back from the dir, not the upload's filename.
	if !strings.Contains(rec.Body.String(), "design-critique") {
		t.Errorf("installed name not echoed: %s", rec.Body.String())
	}
	// Temp dir is cleaned up once the response is built.
	if _, err := os.Stat(gw.gotArchive); !os.IsNotExist(err) {
		t.Error("temp archive was not removed after the response")
	}
}

func TestUploadSkillGuards(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", installDir: "/skills/x"}

	// No file part at all.
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/agent/skills/upload", nil)
	c.Request.Header.Set("Content-Type", "multipart/form-data; boundary=nope")
	(&AgentHandler{agentGateway: gw}).UploadSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("missing file: want 400, got %d", rec.Code)
	}

	// Empty file.
	rec, c = postMultipart(t, "/api/agent/skills/upload", "empty.zip", nil)
	(&AgentHandler{agentGateway: gw}).UploadSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("empty file: want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.gotArchive != "" {
		t.Error("gateway must not be called for an empty upload")
	}
}

// A non-zip upload is a 400, not a 500.
func TestUploadSkillRejectsNonArchive(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw", installErr: skills.ErrEmptyArchive}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "notes.txt", []byte("just text"))

	(&AgentHandler{agentGateway: gw}).UploadSkill(c)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestUploadSkillNotSupported(t *testing.T) {
	gw := &fakeGateway{name: "Hermes", installErr: domain.ErrNotSupportedByRuntime}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "x.zip", []byte("PK\x03\x04"))

	(&AgentHandler{agentGateway: gw}).UploadSkill(c)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "Hermes") {
		t.Errorf("runtime name not surfaced: %s", rec.Body.String())
	}
}

func (f *fakeGateway) InstallSkillMarkdown(content []byte) (string, error) {
	f.gotMD = content
	return f.mdDir, f.mdErr
}

// A bare .md upload takes the markdown path; its front-matter names the skill.
func TestUploadSkillMarkdown(t *testing.T) {
	md := []byte("---\nname: weekly-report\ndescription: Sums up the week.\n---\nbody")
	gw := &fakeGateway{name: "OpenClaw", mdDir: "/skills/weekly-report"}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "anything.md", md)

	(&AgentHandler{agentGateway: gw}).UploadSkill(c)

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if string(gw.gotMD) != string(md) {
		t.Errorf("content not passed verbatim: %q", gw.gotMD)
	}
	// The archive path must NOT have been used for a .md.
	if gw.gotArchive != "" {
		t.Error("a .md upload must not go through InstallSkillArchive")
	}
	// Name comes from the installed dir, not the uploaded filename.
	if !strings.Contains(rec.Body.String(), "weekly-report") {
		t.Errorf("installed name not echoed: %s", rec.Body.String())
	}
}

// The two documented file requirements both surface as 400, not 500.
func TestUploadSkillRequirementFailuresAre400(t *testing.T) {
	// .md without valid front-matter.
	gw := &fakeGateway{name: "OpenClaw", mdErr: skills.ErrInvalidFrontMatter}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "x.md", []byte("# no front-matter"))
	(&AgentHandler{agentGateway: gw}).UploadSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("bad front-matter: want 400, got %d: %s", rec.Code, rec.Body.String())
	}

	// Archive with no SKILL.md.
	gw = &fakeGateway{name: "OpenClaw", installErr: skills.ErrMissingSkillMD}
	rec, c = postMultipart(t, "/api/agent/skills/upload", "x.zip", []byte("PK"))
	(&AgentHandler{agentGateway: gw}).UploadSkill(c)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("missing SKILL.md: want 400, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestUploadSkillRejectsUnsupportedExtension(t *testing.T) {
	gw := &fakeGateway{name: "OpenClaw"}
	rec, c := postMultipart(t, "/api/agent/skills/upload", "notes.txt", []byte("text"))

	(&AgentHandler{agentGateway: gw}).UploadSkill(c)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.gotArchive != "" || gw.gotMD != nil {
		t.Error("gateway must not be called for an unsupported extension")
	}
}
