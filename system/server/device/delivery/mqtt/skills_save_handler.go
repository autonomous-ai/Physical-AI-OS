package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// handleSkillsSave handles kind="skills.save" — the MQTT twin of POST
// /api/agent/skills.
func (h *DeviceMQTTHandler) handleSkillsSave(env domain.MQTTDataCommand) error {
	draft, errMsg := parseSkillsSaveData(env.Data)
	if errMsg != "" {
		return h.publishDataResult(env.Kind, "failure", errMsg, nil)
	}

	// Shares skillsInstallMu with skills.install (same skills dir). TryLock:
	// the MQTT dispatch path must not stall behind a long install.
	if !skillsInstallMu.TryLock() {
		return h.publishDataResult(env.Kind, "failure",
			"a skills install is in progress; try again later", nil)
	}
	defer skillsInstallMu.Unlock()

	runtimeName := h.agentGateway.Name()

	path, err := h.agentGateway.SaveSkill(draft)
	if err != nil {
		step := classifySkillsSaveError(err)
		slog.Error("skills.save: failed", "component", "mqtt",
			"skill", draft.Name, "runtime", runtimeName, "step", step, "error", err)
		return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
			"name":        draft.Name,
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	slog.Info("skills.save: success", "component", "mqtt",
		"skill", draft.Name, "runtime", runtimeName, "path", path)
	if err := h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"name":    draft.Name,
		"runtime": runtimeName,
		"path":    path,
	}); err != nil {
		return err
	}
	h.publishInfoAfterSkillsMutation()
	return nil
}

// parseSkillsSaveData decodes + normalises a skills.save payload into the
// draft the gateway takes.
func parseSkillsSaveData(raw json.RawMessage) (domain.SkillDraft, string) {
	var req domain.MQTTSkillsSaveData
	if err := json.Unmarshal(raw, &req); err != nil {
		return domain.SkillDraft{}, "invalid skills.save data: " + err.Error()
	}

	draft := domain.SkillDraft{
		Name:         strings.TrimSpace(req.Name),
		Description:  strings.TrimSpace(req.Description),
		Instructions: strings.TrimSpace(req.Instructions),
	}
	if draft.Name == "" || draft.Description == "" || draft.Instructions == "" {
		return domain.SkillDraft{}, "name, description and instructions are required"
	}
	return draft, ""
}

// classifySkillsSaveError maps a SaveSkill failure to the `failed_step` label
// the backend reads off the result, mirroring how skills.install reports its
// step.
func classifySkillsSaveError(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotSupportedByRuntime):
		return "unsupported_runtime"
	case errors.Is(err, skills.ErrInvalidSkillName):
		return "validate_name"
	case errors.Is(err, skills.ErrSkillExists):
		return "already_exists"
	default:
		return "write"
	}
}
