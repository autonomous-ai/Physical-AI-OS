package http

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/runtimes/claudecode"
	"go.autonomous.ai/os/runtimes/codex"
	"go.autonomous.ai/os/runtimes/hermes"
	"go.autonomous.ai/os/runtimes/openclaw"
	"go.autonomous.ai/os/runtimes/opencode"
	"go.autonomous.ai/os/runtimes/picoclaw"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// agentUnitByBackend maps runtime Name() to its systemd unit; keep in sync with each runtime's gateway unit.
var agentUnitByBackend = map[string]string{
	domain.AgentRuntimeOpenClaw:   "openclaw",
	domain.AgentRuntimeHermes:     "hermes-gateway",
	domain.AgentRuntimePicoclaw:   "picoclaw",
	domain.AgentRuntimeCodex:      "codex",
	domain.AgentRuntimeClaudeCode: "claudecode",
	domain.AgentRuntimeOpenCode:   "opencode",
}

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.5.27").
func GetOpenClawVersion() string {
	return openclaw.GetOpenClawVersion()
}

// populateOpenClawVersion populates the shared openclaw version cache at startup.
func populateOpenClawVersion() {
	openclaw.PopulateOpenClawVersion()
}

// GetHermesVersion returns the cached Hermes CLI version (e.g. "0.17.0").
func GetHermesVersion() string {
	return hermes.GetHermesVersion()
}

// populateHermesVersion populates the shared hermes version cache at startup.
func populateHermesVersion() {
	hermes.PopulateHermesVersion()
}

func GetPicoclawVersion() string {
	return picoclaw.GetPicoclawVersion()
}

// populatePicoclawVersion populates the shared picoclaw version cache at startup.
func populatePicoclawVersion() {
	picoclaw.PopulatePicoclawVersion()
}

func GetCodexVersion() string {
	return codex.GetCodexVersion()
}

// populateCodexVersion populates the shared codex version cache at startup.
func populateCodexVersion() {
	codex.PopulateCodexVersion()
}

func GetClaudeCodeVersion() string {
	return claudecode.GetClaudeCodeVersion()
}

// populateClaudeCodeVersion populates the shared claudecode version cache at startup.
func populateClaudeCodeVersion() {
	claudecode.PopulateClaudeCodeVersion()
}

func GetOpenCodeVersion() string {
	return opencode.GetOpenCodeVersion()
}

// populateOpenCodeVersion populates the shared opencode version cache at startup.
func populateOpenCodeVersion() {
	opencode.PopulateOpenCodeVersion()
}

// StopTTS interrupts active TTS playback on HAL.
func (h *AgentHandler) StopTTS(c *gin.Context) {
	if err := h.agentGateway.StopTTS(); err != nil {
		slog.Warn("StopTTS failed", "component", "agent", "backend", h.agentGateway.Name(), "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// CancelSpeechHandler silences in-flight turns and cuts current playback (physical cancel gesture).
// Both halves are needed: StopTTS clears HAL's playing+queued audio; the watermark mutes not-yet-generated sentences.
func (h *AgentHandler) CancelSpeechHandler(c *gin.Context) {
	h.CancelSpeech()
	if err := h.agentGateway.StopTTS(); err != nil {
		// Watermark already applied; report the HAL failure without failing the request.
		slog.Warn("StopTTS during speech cancel failed", "component", "agent", "error", err)
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// SetBusy marks the agent busy from an external signal (e.g. turn-gate hook), covering
// channel-initiated turns that bypass the OS server.
func (h *AgentHandler) SetBusy(c *gin.Context) {
	h.agentGateway.SetBusy(true)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// Restart enables the unit (best-effort) then restarts the agent gateway, so a stopped or
// disabled gateway can be recovered from the UI and survives reboot.
func (h *AgentHandler) Restart(c *gin.Context) {
	name := h.agentGateway.Name()
	slog.Info("agent restart requested", "component", "agent", "backend", name)

	enabled := false
	if unit, ok := agentUnitByBackend[name]; ok && unit != "" && os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			if out, err := exec.Command("systemctl", "enable", unit).CombinedOutput(); err != nil {
				slog.Warn("systemctl enable failed (best-effort, continuing to restart)",
					"component", "agent", "backend", name, "unit", unit,
					"error", err, "output", strings.TrimSpace(string(out)))
			} else {
				enabled = true
				slog.Info("systemctl enabled for auto-start on boot",
					"component", "agent", "backend", name, "unit", unit)
			}
		}
	}

	if err := h.agentGateway.RestartAgent(); err != nil {
		slog.Warn("RestartAgent failed", "component", "agent", "backend", name, "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"backend": name,
		"enabled": enabled,
	}))
}

// Status returns the current agent connection status.
func (h *AgentHandler) Status(c *gin.Context) {
	emotion := h.fetchHALEmotion()

	version := h.agentGateway.Version()

	// uptime: since the WS last became ready; agentUptime: runtime process uptime from hello-ok.
	var uptime int64
	if connectedAt := h.agentGateway.ConnectedAt(); connectedAt > 0 {
		uptime = time.Now().Unix() - connectedAt
		if uptime < 0 {
			uptime = 0
		}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"name":        h.agentGateway.Name(),
		"connected":   h.agentGateway.IsReady(),
		"sessionKey":  h.agentGateway.GetSessionKey() != "",
		"emotion":     emotion,
		"version":     version,
		"uptime":      uptime,
		"agentUptime": h.agentGateway.AgentUptime(),
	}))
}

// fetchHALEmotion calls HAL /emotion/status to get the current emotion.
// Falls back to lastEmotion if HAL is unreachable.
func (h *AgentHandler) fetchHALEmotion() string {
	// Only devices with the `expression` capability mount HAL /emotion; avoid 404 polling.
	if !device.Has(h.config.DeviceTypeOrDefault(), device.CapExpression) {
		return ""
	}
	emotion, err := hal.GetEmotion()
	if err != nil {
		h.lastEmotionMu.Lock()
		defer h.lastEmotionMu.Unlock()
		return h.lastEmotion
	}
	return emotion
}

// Events streams monitor bus events over SSE to connected web UI clients.
func (h *AgentHandler) Events(c *gin.Context) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no") // disable nginx buffering

	sub, unsub := h.monitorBus.Subscribe()
	defer unsub()

	c.Stream(func(w io.Writer) bool {
		select {
		case evt := <-sub:
			data, _ := json.Marshal(evt)
			c.SSEvent("message", string(data))
			return true
		case <-c.Request.Context().Done():
			return false
		}
	})
}

// ConfigJSON returns the active runtime's raw config contents for the gw-config UI.
func (h *AgentHandler) ConfigJSON(c *gin.Context) {
	data, err := h.agentGateway.GetConfigJSON()
	if err != nil {
		if errors.Is(err, domain.ErrNotSupportedByRuntime) {
			c.JSON(http.StatusOK, serializers.ResponseError(
				h.agentGateway.Name()+" has no device-side config file"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(data))
}
