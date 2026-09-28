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
	// hermes_version / picoclaw_version / codex_version / claudecode_version
	// sit next to openclaw_version (all probed at startup); the active one is
	// named by agent_runtime.
	msg.HermesVersion = agenthttp.GetHermesVersion()
	msg.PicoclawVersion = agenthttp.GetPicoclawVersion()
	msg.CodexVersion = agenthttp.GetCodexVersion()
	msg.ClaudeCodeVersion = agenthttp.GetClaudeCodeVersion()
	msg.OpenCodeVersion = agenthttp.GetOpenCodeVersion()
	msg.AgentRuntime = device.CurrentAgentRuntimeFromConfig(h.config)
	// Channels configured here that the active runtime can't run (set by
	// ChannelReconcile after a runtime switch, e.g. slack after switching to picoclaw).
	msg.UnsupportedChannels = h.config.ChannelsUnsupported
	if ip, err := h.networkService.GetCurrentIP(); err == nil {
		msg.LocalIP = ip
	}
	// Override the config-seeded value with the live system zone (/etc/timezone),
	// so the uplink reflects an out-of-band `timedatectl` change too.
	msg.Timezone = h.deviceService.CurrentTimezone()
	// Skills installed on the ACTIVE runtime, same set the HTTP ping carries.
	// Best-effort: a runtime that can't list them (or an unreadable skills dir)
	// omits the field rather than failing the whole info uplink.
	if list, err := h.agentGateway.ListSkills(); err != nil {
		slog.Debug("info: skills list unavailable", "component", "mqtt", "error", err)
	} else {
		msg.Skills = domain.SummarizeSkills(list)
	}
	// Fingerprint of the scheduled tasks this device holds, for the backend's
	// drift check (see domain.MQTTInfoResponse.SchedulesDigest). Read from
	// the same schedules.json the runner fires from — not the pending-intent
	// queue, whose proposals the backend does not hold yet either. Best-effort
	// like skills above: a store that can't be read omits the field rather
	// than failing the uplink, and LoadChecked is used instead of Load so an
	// unreadable file is never reported as the empty-list digest (which would
	// make the backend re-send a sync this same store could not apply).
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

// publishInfoAfterSkillsMutation refreshes the server's cached skill inventory
// immediately after a successful MQTT skills write. The command response has
// already confirmed the mutation; an info uplink failure is therefore logged
// but must not turn that successful mutation into a failed command.
func (h *DeviceMQTTHandler) publishInfoAfterSkillsMutation() {
	if err := h.handleInfo(domain.MQTTMessage{Cmd: domain.CommandInfo}); err != nil {
		slog.Warn("skills: immediate info uplink failed", "component", "mqtt", "error", err)
	}
}
