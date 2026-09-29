package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// handleSkillsInstallStore handles kind="skills.install_store" — the MQTT
// twin of POST /api/agent/skills/install (the web UI's Install button).
func (h *DeviceMQTTHandler) handleSkillsInstallStore(env domain.MQTTDataCommand) error {
	var req domain.MQTTSkillsInstallStoreData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid skills.install_store data: "+err.Error(), nil)
	}
	req.ID = strings.TrimSpace(req.ID)
	req.Name = strings.TrimSpace(req.Name)

	// The id lands in an upstream URL path, so validate before anything else.
	if err := skills.ValidateStoreSkillID(req.ID); err != nil {
		return h.publishDataResult(env.Kind, "failure", "validate_id: "+err.Error(), nil)
	}

	// Shared with skills.install/save (same dir); TryLock keeps dispatch free.
	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"another skills install is already in progress; try again later", nil)
	}

	if err := h.publishDataResult(env.Kind, "starting", "", nil); err != nil {
		slog.Error("skills.install_store: ack publish failed", "component", "mqtt", "error", err)
	}

	go func() {
		defer skillsInstallMu.Unlock()
		h.runSkillsInstallStore(env.Kind, req.ID, req.Name)
	}()
	return nil
}

// runSkillsInstallStore downloads the catalog archive into a temp dir, hands
// it to the active runtime, and publishes the terminal status.
func (h *DeviceMQTTHandler) runSkillsInstallStore(kind, id, fallbackName string) {
	runtimeName := h.agentGateway.Name()
	slog.Info("skills.install_store: start", "component", "mqtt", "id", id, "runtime", runtimeName)

	fail := func(step, msg string) {
		slog.Error("skills.install_store: failed", "component", "mqtt",
			"id", id, "runtime", runtimeName, "step", step, "error", msg)
		h.alertOps("❌ skills.install_store "+id+" — FAILED", step+": "+msg)
		_ = h.publishDataResult(kind, "failure", step+": "+msg, map[string]interface{}{
			"id":          id,
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	tmpDir, err := os.MkdirTemp("", "skill-install-*")
	if err != nil {
		fail("temp_dir", err.Error())
		return
	}
	defer os.RemoveAll(tmpDir)

	zipPath, err := skills.DownloadStoreArchive(id, tmpDir)
	if err != nil {
		fail("download", err.Error())
		return
	}

	dir, err := h.agentGateway.InstallSkillArchive(zipPath, fallbackName)
	if err != nil {
		fail(classifySkillsInstallStoreError(err), err.Error())
		return
	}

	name := filepath.Base(dir)
	slog.Info("skills.install_store: success", "component", "mqtt",
		"id", id, "runtime", runtimeName, "skill", name, "dir", dir)
	h.alertOps("✅ skills.install_store "+name+" — OK", "runtime="+runtimeName)
	_ = h.publishDataResult(kind, "success", "", map[string]interface{}{
		"id":      id,
		"name":    name,
		"runtime": runtimeName,
		"path":    dir,
	})
}

// classifySkillsInstallStoreError maps an InstallSkillArchive failure to the
// `failed_step` label the backend reads off the result.
func classifySkillsInstallStoreError(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		return "unsupported_runtime"
	case errors.Is(err, skills.ErrEmptyArchive):
		return "archive"
	case errors.Is(err, skills.ErrInvalidSkillName):
		return "validate_name"
	default:
		return "install"
	}
}
