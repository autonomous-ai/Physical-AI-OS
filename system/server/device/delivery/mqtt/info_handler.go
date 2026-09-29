package mqtthandler

import (
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/schedule"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
)

func (h *DeviceMQTTHandler) handleInfo(_ domain.MQTTMessage) error {
	msg := domain.NewMQTTInfoResponse(h.config, "info", device.GetDeviceMac())
	if v, err := hal.GetVersion(); err == nil {
		msg.HalVersion = v
	}
	msg.OpenClawVersion = agenthttp.GetOpenClawVersion()
	msg.HermesVersion = agenthttp.GetHermesVersion()
	msg.PicoclawVersion = agenthttp.GetPicoclawVersion()
	msg.CodexVersion = agenthttp.GetCodexVersion()
	msg.ClaudeCodeVersion = agenthttp.GetClaudeCodeVersion()
	msg.OpenCodeVersion = agenthttp.GetOpenCodeVersion()
	msg.AgentRuntime = device.CurrentAgentRuntimeFromConfig(h.config)
	msg.UnsupportedChannels = h.config.ChannelsUnsupported
	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		msg.LocalIP = ip
	}
	msg.Timezone = h.deviceService.CurrentTimezone()
	if list, err := h.agentGateway.ListSkills(); err != nil {
		slog.Debug("info: skills list unavailable", "component", "mqtt", "error", err)
	} else {
		msg.Skills = domain.SummarizeSkills(list)
	}
	// LoadChecked, not Load: an unreadable store omits the digest instead of
	// reporting the empty-list one.
	if rows, err := h.scheduleStore.LoadChecked(); err != nil {
		slog.Warn("info: schedules store unreadable, omitting schedules_digest",
			"component", "mqtt", "error", err)
	} else {
		msg.SchedulesDigest = schedule.Digest(rows)
	}
	slog.Info("mqtt_handler_info",
		"id", msg.ID,
		"version", msg.Version,
		"hal_version", msg.HalVersion,
		"openclaw_version", msg.OpenClawVersion,
		"hermes_version", msg.HermesVersion,
		"picoclaw_version", msg.PicoclawVersion,
		"codex_version", msg.CodexVersion,
		"claudecode_version", msg.ClaudeCodeVersion,
		"opencode_version", msg.OpenCodeVersion,
		"agent_runtime", msg.AgentRuntime,
		"local_ip", msg.LocalIP,
		"tts_provider", msg.TTSProvider,
		"tts_voice", msg.TTSVoice,
		"stt_language", msg.STTLanguage,
		"timezone", msg.Timezone,
		"skills", len(msg.Skills),
		"schedules_digest", msg.SchedulesDigest,
	)
	return h.publish(msg)
}

// publishInfoAfterSkillsMutation refreshes the server's cached skill
// inventory immediately after a successful MQTT skills write.
func (h *DeviceMQTTHandler) publishInfoAfterSkillsMutation() {
	if err := h.handleInfo(domain.MQTTMessage{Cmd: domain.CommandInfo}); err != nil {
		slog.Warn("skills: immediate info uplink failed", "component", "mqtt", "error", err)
	}
}
