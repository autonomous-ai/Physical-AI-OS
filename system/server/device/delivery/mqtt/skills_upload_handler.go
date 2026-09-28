package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// handleSkillsUpload handles kind="skills.upload" — the MQTT counterpart of
// POST /api/agent/skills/upload.
func (h *DeviceMQTTHandler) handleSkillsUpload(env domain.MQTTDataCommand) error {
	filename, content, errMsg := parseSkillsUploadData(env.Data)
	if errMsg != "" {
		return h.publishDataResult(env.Kind, "failure", errMsg, nil)
	}

	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"a skills install is in progress; try again later", nil)
	}
	defer skillsInstallMu.Unlock()

	runtimeName := h.agentGateway.Name()
	base := filepath.Base(filename)
	ext := strings.ToLower(filepath.Ext(base))
	var dir string
	var err error
	switch ext {
	case ".md":
		dir, err = h.agentGateway.InstallSkillMarkdown(content)
	case ".zip", ".skill":
		dir, err = h.installUploadedSkillArchive(content, base)
	default:
		return h.publishDataResult(env.Kind, "failure",
			"unsupported file type "+ext+" — upload a .skill, .zip or .md", nil)
	}
	if err != nil {
		step := classifySkillsUploadError(err)
		slog.Error("skills.upload: failed", "component", "mqtt",
			"runtime", runtimeName, "step", step, "error", err)
		return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	name := filepath.Base(dir)
	slog.Info("skills.upload: success", "component", "mqtt",
		"skill", name, "runtime", runtimeName, "path", dir)
	if err := h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"name":    name,
		"runtime": runtimeName,
		"path":    dir,
	}); err != nil {
		return err
	}
	h.publishInfoAfterSkillsMutation()
	return nil
}

func (h *DeviceMQTTHandler) installUploadedSkillArchive(content []byte, base string) (string, error) {
	tmpDir, err := os.MkdirTemp("", "skill-upload-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmpDir)

	zipPath := filepath.Join(tmpDir, "skill.zip")
	if err := os.WriteFile(zipPath, content, 0600); err != nil {
		return "", err
	}
	fallback := skills.SlugifySkillName(strings.TrimSuffix(base, filepath.Ext(base)))
	return h.agentGateway.InstallSkillArchive(zipPath, fallback)
}

func parseSkillsUploadData(raw json.RawMessage) (string, []byte, string) {
	var req domain.MQTTSkillsUploadData
	if err := json.Unmarshal(raw, &req); err != nil {
		return "", nil, "invalid skills.upload data: " + err.Error()
	}
	filename := filepath.Base(strings.TrimSpace(req.Filename))
	if filename == "" || filename == "." {
		return "", nil, "filename is required"
	}
	encoded := strings.TrimSpace(req.ContentBase64)
	if encoded == "" {
		return "", nil, "content_base64 is required"
	}
	if len(encoded) > base64.StdEncoding.EncodedLen(int(skills.StoreMaxBytes)) {
		return "", nil, "content exceeds maximum skill size"
	}
	content, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", nil, "content_base64 must be valid base64"
	}
	if int64(len(content)) > skills.StoreMaxBytes {
		return "", nil, "content exceeds maximum skill size"
	}
	return filename, content, ""
}

func classifySkillsUploadError(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		return "unsupported_runtime"
	case errors.Is(err, skills.ErrInvalidFrontMatter):
		return "validate_front_matter"
	case errors.Is(err, skills.ErrEmptyArchive), errors.Is(err, skills.ErrMissingSkillMD):
		return "archive"
	case errors.Is(err, skills.ErrInvalidSkillName):
		return "validate_name"
	default:
		return "install"
	}
}
