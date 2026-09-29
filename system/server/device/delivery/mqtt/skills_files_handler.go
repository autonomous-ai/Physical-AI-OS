package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"unicode/utf8"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// mqttMaxFileTextBytes caps the inlined text of a single file on this uplink.
const mqttMaxFileTextBytes = 30 << 10

// handleSkillsFiles handles kind="skills.files" — the MQTT twin of GET
// /api/agent/skills/files.
func (h *DeviceMQTTHandler) handleSkillsFiles(env domain.MQTTDataCommand) error {
	var req domain.MQTTSkillsFilesData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid skills.files data: "+err.Error(), nil)
	}
	req.Name = strings.TrimSpace(req.Name)
	req.Path = strings.TrimSpace(req.Path)
	if req.Name == "" {
		return h.publishDataResult(env.Kind, "failure", "name is required", nil)
	}

	runtimeName := h.agentGateway.Name()

	if req.Path != "" {
		// Do not load the entire skill merely to return one requested
		// document.
		file, err := h.agentGateway.ReadSkillFile(req.Name, req.Path)
		if err != nil {
			if errors.Is(err, skills.ErrSkillFileNotFound) {
				slog.Warn("skills.files: path not in skill", "component", "mqtt",
					"skill", req.Name, "path", req.Path)
				return h.publishDataResult(env.Kind, "failure", "not_found: "+req.Path, map[string]interface{}{
					"name":        req.Name,
					"path":        req.Path,
					"runtime":     runtimeName,
					"failed_step": "not_found",
				})
			}
			step := "read"
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				step = "unsupported_runtime"
			} else if errors.Is(err, skills.ErrInvalidSkillName) {
				step = "validate_name"
			}
			slog.Error("skills.files: file read failed", "component", "mqtt",
				"skill", req.Name, "path", req.Path, "runtime", runtimeName, "step", step, "error", err)
			return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
				"name":        req.Name,
				"path":        req.Path,
				"runtime":     runtimeName,
				"failed_step": step,
			})
		}

		slog.Info("skills.files: success", "component", "mqtt",
			"skill", req.Name, "runtime", runtimeName, "path", file.Path, "bytes", len(file.Text))
		return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
			"name":    req.Name,
			"runtime": runtimeName,
			"file":    capSkillFileText(file),
		})
	}

	files, err := h.agentGateway.ReadSkillFiles(req.Name)
	if err != nil {
		step := "read"
		switch {
		case errors.Is(err, domain.ErrNotSupportedByRuntime):
			step = "unsupported_runtime"
		case errors.Is(err, skills.ErrInvalidSkillName):
			step = "validate_name"
		}
		slog.Error("skills.files: failed", "component", "mqtt",
			"skill", req.Name, "runtime", runtimeName, "step", step, "error", err)
		return h.publishDataResult(env.Kind, "failure", step+": "+err.Error(), map[string]interface{}{
			"name":        req.Name,
			"runtime":     runtimeName,
			"failed_step": step,
		})
	}

	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"name":    req.Name,
		"runtime": runtimeName,
		"files":   stripSkillFileText(files),
	})
}

// stripSkillFileText drops every inlined body, leaving path/size/binary
// metadata.
func stripSkillFileText(files []domain.SkillBundleFile) []domain.SkillBundleFile {
	out := make([]domain.SkillBundleFile, 0, len(files))
	for _, f := range files {
		f.Text = ""
		f.Truncated = false
		out = append(out, f)
	}
	return out
}

// capSkillFileText re-truncates a file's text to the MQTT budget. Size keeps
// reporting the file's REAL length. Do not split a UTF-8 rune at the byte cap.
func capSkillFileText(f domain.SkillBundleFile) domain.SkillBundleFile {
	if len(f.Text) <= mqttMaxFileTextBytes {
		return f
	}
	text := f.Text[:mqttMaxFileTextBytes]
	for len(text) > 0 && !utf8.ValidString(text) {
		text = text[:len(text)-1]
	}
	f.Text = text
	f.Truncated = true
	return f
}
