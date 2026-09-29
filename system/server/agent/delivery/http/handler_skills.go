package http

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/skills"
)

// Device-side proxy for the Agent Skills catalog, so the web UI avoids CORS.

const (
	skillStoreTimeout    = 10 * time.Second
	skillDownloadTimeout = 30 * time.Second
)

// Caps that keep a hostile or broken archive from exhausting memory/disk.
const (
	maxBundleBytes = 16 << 20 // downloaded archive
	maxFileBytes   = 2 << 20  // one extracted file
	maxBundleFiles = 500
	// Bounds pagination against a malformed upstream `total`.
	storeMatchPageSize = 100
	maxStoreMatchPages = 50
)

// storeEnvelope is the catalog's JSON wrapper; failures return HTTP 200 with Status != 1.
type storeEnvelope struct {
	Status  int             `json:"status"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

// storeGet delegates to the shared catalog client in system/skills.
func storeGet(path string, query url.Values, timeout time.Duration, maxBytes int64) ([]byte, error) {
	return skills.StoreGet(path, query, timeout, maxBytes)
}

// ListSkills handles GET /api/agent/skills: the active runtime's installed skills (501 if unsupported).
func (h *AgentHandler) ListSkills(c *gin.Context) {
	list, err := h.agentGateway.ListSkills()
	if errors.Is(err, domain.ErrNotSupportedByRuntime) {
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot list skills yet"))
		return
	}
	if err != nil {
		slog.Error("[skills] list failed", "component", "agent-http", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	if list == nil {
		list = []domain.InstalledSkill{}
	}
	if len(list) > 0 {
		if err := h.annotateStoreAvailability(list); err != nil {
			// Never label device_only unless the whole catalog was read.
			slog.Warn("[skills] store availability unknown", "component", "agent-http", "error", err)
			for i := range list {
				list[i].StoreAvailability = "unknown"
			}
		}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(list))
}

// annotateStoreAvailability marks installed skills found in the catalog by
// normalized name; it reads the complete catalog or returns an error.
func (h *AgentHandler) annotateStoreAvailability(installed []domain.InstalledSkill) error {
	storeNames := make(map[string]struct{})
	for page := 1; page <= maxStoreMatchPages; page++ {
		q := url.Values{}
		q.Set("page", fmt.Sprint(page))
		q.Set("limit", fmt.Sprint(storeMatchPageSize))
		body, err := storeGet("/api/v1/agent-skills", q, skillStoreTimeout, maxBundleBytes)
		if err != nil {
			return fmt.Errorf("fetch catalog page %d: %w", page, err)
		}
		var env storeEnvelope
		if err := json.Unmarshal(body, &env); err != nil {
			return fmt.Errorf("decode catalog page %d envelope: %w", page, err)
		}
		if env.Status != 1 {
			return fmt.Errorf("catalog page %d returned status %d", page, env.Status)
		}
		var catalog domain.StoreSkillList
		if err := json.Unmarshal(env.Data, &catalog); err != nil {
			return fmt.Errorf("decode catalog page %d: %w", page, err)
		}
		for _, skill := range catalog.Data {
			storeNames[normalizeSkillName(skill.Slug)] = struct{}{}
			storeNames[normalizeSkillName(skill.Name)] = struct{}{}
		}
		if int64(page*storeMatchPageSize) >= catalog.Total {
			for i := range installed {
				if _, ok := storeNames[normalizeSkillName(installed[i].Name)]; ok {
					installed[i].StoreAvailability = "in_store"
				} else {
					installed[i].StoreAvailability = "device_only"
				}
			}
			return nil
		}
		if len(catalog.Data) == 0 {
			return fmt.Errorf("catalog page %d is empty before total %d", page, catalog.Total)
		}
	}
	return fmt.Errorf("catalog exceeds %d pages", maxStoreMatchPages)
}

// normalizeSkillName lowercases and keeps only [a-z0-9], e.g. "Computer Use" -> "computeruse".
func normalizeSkillName(value string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(strings.TrimSpace(value)) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// ReadSkillFiles handles GET /api/agent/skills/files?name=<skill>, returning a SkillBundle.
func (h *AgentHandler) ReadSkillFiles(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if name == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("name is required"))
		return
	}

	files, err := h.agentGateway.ReadSkillFiles(name)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot read skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		c.JSON(http.StatusNotFound, serializers.ResponseError(err.Error()))
		return
	}

	if files == nil {
		files = []domain.SkillBundleFile{}
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.SkillBundle{ID: name, Files: files}))
}

// PublishSkill packages a device-only skill and submits it to the campaign API.
// The browser never receives the device API key or accesses the skill directory.
func (h *AgentHandler) PublishSkill(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if err := skills.ValidateSkillName(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if h.config == nil || strings.TrimSpace(h.config.LLMBaseURL) == "" || strings.TrimSpace(h.config.LLMAPIKey) == "" {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("device LLM URL or API key is not configured"))
		return
	}
	base, err := url.Parse(h.config.LLMBaseURL)
	if err != nil || base.Scheme == "" || base.Host == "" {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("invalid device LLM URL"))
		return
	}
	tmp, err := os.MkdirTemp("", "skill-publish-*")
	if err != nil {
		c.JSON(500, serializers.ResponseError("cannot create temp dir"))
		return
	}
	defer os.RemoveAll(tmp)
	archive, err := h.agentGateway.ExportSkillArchive(name, tmp)
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	f, err := os.Open(archive)
	if err != nil {
		c.JSON(500, serializers.ResponseError("cannot read skill archive"))
		return
	}
	defer f.Close()
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	part, err := writer.CreateFormFile("file", filepath.Base(archive))
	if err == nil {
		_, err = io.Copy(part, f)
	}
	if err == nil {
		err = writer.Close()
	}
	if err != nil {
		c.JSON(500, serializers.ResponseError("cannot prepare skill publish"))
		return
	}
	base.Path = "/api/v1/skills/publish"
	base.RawQuery = ""
	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodPost, base.String(), &body)
	if err != nil {
		c.JSON(500, serializers.ResponseError("cannot create publish request"))
		return
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("x-api-key", h.config.LLMAPIKey)
	resp, err := (&http.Client{Timeout: skillDownloadTimeout}).Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to reach skill publishing service"))
		return
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, maxBundleBytes))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		c.JSON(resp.StatusCode, serializers.ResponseError(string(data)))
		return
	}
	c.Data(http.StatusOK, "application/json", data)
}

// UploadSkill handles POST /api/agent/skills/upload (multipart field `file`).
// Accepts .zip/.skill with SKILL.md at the root, or a bare SKILL.md with name + description front-matter.
func (h *AgentHandler) UploadSkill(c *gin.Context) {
	header, err := c.FormFile("file")
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("file is required (multipart field \"file\")"))
		return
	}
	if header.Size == 0 {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("the uploaded file is empty"))
		return
	}
	if header.Size > skills.StoreMaxBytes {
		c.JSON(http.StatusRequestEntityTooLarge, serializers.ResponseError(
			fmt.Sprintf("archive is %d bytes, max %d", header.Size, int64(skills.StoreMaxBytes))))
		return
	}

	base := filepath.Base(header.Filename)
	ext := strings.ToLower(filepath.Ext(base))

	var dir string
	switch ext {
	case ".md":
		dir, err = h.installUploadedMarkdown(header)
	case ".zip", ".skill":
		dir, err = h.installUploadedArchive(header, base)
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError(
			"unsupported file type "+ext+" — upload a .skill, .zip or .md"))
		return
	}

	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot install skills yet"))
		return
	case errors.Is(err, skills.ErrEmptyArchive),
		errors.Is(err, skills.ErrMissingSkillMD),
		errors.Is(err, skills.ErrInvalidFrontMatter),
		errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] upload install failed", "component", "agent-http",
			"file", header.Filename, "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	name := filepath.Base(dir)
	slog.Info("[skills] uploaded", "component", "agent-http",
		"file", header.Filename, "skill", name, "runtime", h.agentGateway.Name(), "path", dir)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"name": name, "path": dir}))
}

// installUploadedMarkdown installs a bare SKILL.md, named by its front-matter.
func (h *AgentHandler) installUploadedMarkdown(header *multipart.FileHeader) (string, error) {
	f, err := header.Open()
	if err != nil {
		return "", fmt.Errorf("open upload: %w", err)
	}
	defer f.Close()

	content, err := io.ReadAll(io.LimitReader(f, skills.StoreMaxBytes))
	if err != nil {
		return "", fmt.Errorf("read upload: %w", err)
	}
	return h.agentGateway.InstallSkillMarkdown(content)
}

// installUploadedArchive stages an archive upload and installs it; the filename
// is the fallback name for flat archives.
func (h *AgentHandler) installUploadedArchive(header *multipart.FileHeader, base string) (string, error) {
	tmpDir, err := os.MkdirTemp("", "skill-upload-*")
	if err != nil {
		return "", fmt.Errorf("create temp dir: %w", err)
	}
	defer os.RemoveAll(tmpDir)

	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := saveMultipartFile(header, zipPath); err != nil {
		return "", err
	}

	fallback := skills.SlugifySkillName(strings.TrimSuffix(base, filepath.Ext(base)))
	return h.agentGateway.InstallSkillArchive(zipPath, fallback)
}

// saveMultipartFile writes an uploaded part to dst, byte-capped.
func saveMultipartFile(header *multipart.FileHeader, dst string) error {
	src, err := header.Open()
	if err != nil {
		return fmt.Errorf("open upload: %w", err)
	}
	defer src.Close()

	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	if _, err := io.Copy(out, io.LimitReader(src, skills.StoreMaxBytes)); err != nil {
		out.Close()
		return fmt.Errorf("store upload: %w", err)
	}
	return out.Close()
}

// DeleteSkill handles DELETE /api/agent/skills?name=<skill>; 404 if not installed.
func (h *AgentHandler) DeleteSkill(c *gin.Context) {
	name := strings.TrimSpace(c.Query("name"))
	if name == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("name is required"))
		return
	}

	path, err := h.agentGateway.DeleteSkill(name)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot uninstall skills yet"))
		return
	case errors.Is(err, skills.ErrSkillNotFound):
		c.JSON(http.StatusNotFound, serializers.ResponseError(err.Error()))
		return
	case errors.Is(err, skills.ErrInvalidSkillName):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] uninstall failed", "component", "agent-http", "skill", name, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] uninstalled", "component", "agent-http",
		"skill", name, "runtime", h.agentGateway.Name(), "path", path)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"name": name, "path": path}))
}

// SaveSkill handles POST /api/agent/skills: writes a user-authored skill to the active runtime (501 if unsupported).
func (h *AgentHandler) SaveSkill(c *gin.Context) {
	var draft domain.SkillDraft
	if err := c.ShouldBindJSON(&draft); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	draft.Name = strings.TrimSpace(draft.Name)
	draft.Description = strings.TrimSpace(draft.Description)
	draft.Instructions = strings.TrimSpace(draft.Instructions)

	path, err := h.agentGateway.SaveSkill(draft)
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot store authored skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName), errors.Is(err, skills.ErrSkillExists):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] save failed", "component", "agent-http", "skill", draft.Name, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] saved", "component", "agent-http",
		"skill", draft.Name, "runtime", h.agentGateway.Name(), "path", path)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"name": draft.Name,
		"path": path,
	}))
}

// BrowseSkills handles GET /api/agent/skills/browse, proxying the catalog listing.
// Params: keyword, category_id, plan, page, limit (all optional).
func (h *AgentHandler) BrowseSkills(c *gin.Context) {
	q := url.Values{}
	// Never forward `status`: the catalog treats it as 0 and filters the listing.
	for _, k := range []string{"keyword", "category_id", "plan", "page", "limit"} {
		if v := strings.TrimSpace(c.Query(k)); v != "" {
			q.Set(k, v)
		}
	}

	body, err := storeGet("/api/v1/agent-skills", q, skillStoreTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] browse failed", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to reach the skill store"))
		return
	}

	var env storeEnvelope
	if err := json.Unmarshal(body, &env); err != nil {
		slog.Error("[skills] browse: invalid envelope", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("invalid JSON from the skill store"))
		return
	}
	if env.Status != 1 {
		msg := env.Message
		if msg == "" {
			msg = fmt.Sprintf("skill store returned status %d", env.Status)
		}
		c.JSON(http.StatusBadGateway, serializers.ResponseError(msg))
		return
	}

	var list domain.StoreSkillList
	if err := json.Unmarshal(env.Data, &list); err != nil {
		slog.Error("[skills] browse: invalid payload", "component", "agent-http", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("unexpected payload from the skill store"))
		return
	}
	if list.Data == nil {
		list.Data = []domain.StoreSkill{}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(list))
}

// SkillBundle handles GET /api/agent/skills/bundle?id=<skillID>: a preview of a
// catalog skill's files (not an install).
func (h *AgentHandler) SkillBundle(c *gin.Context) {
	id := strings.TrimSpace(c.Query("id"))
	if id == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("id is required"))
		return
	}
	// The id goes into the upstream path; reject anything that could escape it.
	if strings.ContainsAny(id, "/\\?#") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid skill id"))
		return
	}

	tmpDir, err := os.MkdirTemp("", "skill-bundle-*")
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot create temp dir"))
		return
	}
	defer os.RemoveAll(tmpDir)

	archive, err := storeGet("/api/v1/agent-skills/"+url.PathEscape(id)+"/download",
		nil, skillDownloadTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] bundle download failed", "component", "agent-http", "id", id, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to download the skill"))
		return
	}

	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot write temp file"))
		return
	}

	bundle, err := extractSkillBundle(zipPath, filepath.Join(tmpDir, "unpacked"))
	if err != nil {
		slog.Error("[skills] bundle extract failed", "component", "agent-http", "id", id, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	bundle.ID = id

	slog.Info("[skills] bundle ready", "component", "agent-http", "id", id, "files", len(bundle.Files))
	c.JSON(http.StatusOK, serializers.ResponseSuccess(bundle))
}

// InstallSkill handles POST /api/agent/skills/install {id, name}: installs a catalog skill into the active runtime.
func (h *AgentHandler) InstallSkill(c *gin.Context) {
	var req struct {
		ID   string `json:"id" binding:"required"`
		Name string `json:"name"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	req.ID = strings.TrimSpace(req.ID)
	if strings.ContainsAny(req.ID, "/\\?#") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid skill id"))
		return
	}

	tmpDir, err := os.MkdirTemp("", "skill-install-*")
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot create temp dir"))
		return
	}
	defer os.RemoveAll(tmpDir)

	archive, err := storeGet("/api/v1/agent-skills/"+url.PathEscape(req.ID)+"/download",
		nil, skillDownloadTimeout, maxBundleBytes)
	if err != nil {
		slog.Error("[skills] install download failed", "component", "agent-http", "id", req.ID, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to download the skill"))
		return
	}
	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot write temp file"))
		return
	}

	dir, err := h.agentGateway.InstallSkillArchive(zipPath, strings.TrimSpace(req.Name))
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		c.JSON(http.StatusNotImplemented, serializers.ResponseError(
			"the active agent runtime ("+h.agentGateway.Name()+") cannot install skills yet"))
		return
	case errors.Is(err, skills.ErrInvalidSkillName), errors.Is(err, skills.ErrEmptyArchive):
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	case err != nil:
		slog.Error("[skills] install failed", "component", "agent-http", "id", req.ID, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	slog.Info("[skills] installed", "component", "agent-http",
		"id", req.ID, "runtime", h.agentGateway.Name(), "dir", dir)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"name": filepath.Base(dir),
		"path": dir,
	}))
}

// extractSkillBundle unzips zipPath into destDir with path-traversal guards and size caps.
func extractSkillBundle(zipPath, destDir string) (domain.SkillBundle, error) {
	var bundle domain.SkillBundle

	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return bundle, fmt.Errorf("the downloaded skill is not a valid archive")
	}
	defer r.Close()

	if err := os.MkdirAll(destDir, 0700); err != nil {
		return bundle, fmt.Errorf("cannot create unpack dir")
	}
	cleanDest, err := filepath.Abs(destDir)
	if err != nil {
		return bundle, fmt.Errorf("cannot resolve unpack dir")
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := filepath.ToSlash(f.Name)
		if strings.HasPrefix(name, "/") || strings.Contains(name, "..") {
			return bundle, fmt.Errorf("archive contains an unsafe path")
		}
		if base := filepath.Base(name); base == ".DS_Store" || strings.HasPrefix(name, "__MACOSX/") {
			continue
		}
		if len(bundle.Files) >= maxBundleFiles {
			bundle.Skipped++
			continue
		}

		target := filepath.Join(destDir, filepath.FromSlash(name))
		absTarget, err := filepath.Abs(target)
		if err != nil || !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) {
			return bundle, fmt.Errorf("archive contains an unsafe path")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return bundle, fmt.Errorf("cannot unpack the skill")
		}

		content, err := readZipEntry(f, target)
		if err != nil {
			return bundle, err
		}
		bundle.Files = append(bundle.Files, skills.BuildFilePreview(name, content, int64(f.UncompressedSize64)))
	}

	if len(bundle.Files) == 0 {
		return bundle, fmt.Errorf("the skill archive is empty")
	}
	return bundle, nil
}

// readZipEntry writes one entry to target and returns its bytes, capped at maxFileBytes.
func readZipEntry(f *zip.File, target string) ([]byte, error) {
	rc, err := f.Open()
	if err != nil {
		return nil, fmt.Errorf("cannot read %s from the archive", f.Name)
	}
	defer rc.Close()

	content, err := io.ReadAll(io.LimitReader(rc, maxFileBytes))
	if err != nil {
		return nil, fmt.Errorf("cannot read %s from the archive", f.Name)
	}
	if err := os.WriteFile(target, content, 0600); err != nil {
		return nil, fmt.Errorf("cannot unpack the skill")
	}
	return content, nil
}
