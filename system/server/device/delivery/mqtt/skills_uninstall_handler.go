package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// handleSkillsUninstall handles kind="skills.uninstall" — the MQTT twin of
// DELETE /api/agent/skills.
func (h *DeviceMQTTHandler) handleSkillsUninstall(env domain.MQTTDataCommand) error {
	var req domain.MQTTSkillsUninstallData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid skills.uninstall data: "+err.Error(), nil)
	}
	req.Name = strings.TrimSpace(req.Name)
	if req.Name == "" {
		return h.publishDataResult(env.Kind, "failure", "name is required", nil)
	}

	// Shared with every other skills-dir writer: an uninstall must not interleave
	// with an install extracting into the same tree.
	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"a skills install is in progress; try again later", nil)
	}
	defer skillsInstallMu.Unlock()

	runtimeName := h.agentGateway.Name()

	path, err := h.agentGateway.DeleteSkill(req.Name)
	if err != nil {
		step := classifySkillsUninstallError(err)
		slog.Error("skills.uninstall: failed", "component", "mqtt",
			"skill", req.Name, "runtime", runtimeName, "step", step, "error", err)
		return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
			"name":        req.Name,
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	slog.Info("skills.uninstall: success", "component", "mqtt",
		"skill", req.Name, "runtime", runtimeName, "path", path)
	h.alertOps("🗑 skills.uninstall "+req.Name+" — OK", "runtime="+runtimeName)
	if err := h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"name":    req.Name,
		"runtime": runtimeName,
		"path":    path,
	}); err != nil {
		return err
	}
	h.publishInfoAfterSkillsMutation()
	return nil
}

// classifySkillsUninstallError maps a DeleteSkill failure to the `failed_step`
// label the backend reads off the result.
func classifySkillsUninstallError(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		return "unsupported_runtime"
	case errors.Is(err, skills.ErrSkillNotFound):
		return "not_found"
	case errors.Is(err, skills.ErrInvalidSkillName):
		return "validate_name"
	default:
		return "remove"
	}
}
