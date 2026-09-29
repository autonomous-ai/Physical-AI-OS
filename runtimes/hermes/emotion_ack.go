package hermes

import (
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/skills"
)

// Compile-time check: *HermesService fires the channel-turn "thinking" ack.
var _ domain.ChannelStartEmotioner = (*HermesService)(nil)

// emotion-acknowledge parity for Hermes.
const (
	ackEmotionName      = "thinking"
	ackEmotionIntensity = 0.7
)

// ackSkipPrefixes mirror runtimes/openclaw/hooks/emotion-acknowledge/handler.ts.
var ackSkipPrefixes = []string{
	"[sensing:",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
}

// ackEmotionEnabled reports whether this device installs the emotion-acknowledge hook (capability-gated identically to OpenClaw onboarding).
func ackEmotionEnabled(deviceType string) bool {
	for _, h := range skills.SupportedHooks(device.Capabilities(deviceType)) {
		if h == "emotion-acknowledge" {
			return true
		}
	}
	return false
}

// fireAckEmotion drives the "thinking" face for a turn that will produce a visible reply.
func (s *HermesService) fireAckEmotion(runID, message string) {
	if !s.ackHookEnabled {
		return
	}
	if strings.TrimSpace(message) == "" {
		return
	}
	for _, p := range ackSkipPrefixes {
		if strings.HasPrefix(message, p) {
			return
		}
	}
	if runID != "" && s.IsSilentRun(runID) {
		return
	}
	safego.Go("hermes-ack-emotion", func() {
		if err := hal.SetEmotion(ackEmotionName, ackEmotionIntensity); err != nil {
			slog.Debug("ack emotion post failed", "component", "hermes", "error", err)
		}
	})
}

// FireChannelStartEmotion gives the "thinking" ack to gateway-owned channel turns (native Telegram/Discord) that never reach sendChat.
func (s *HermesService) FireChannelStartEmotion(message, runID string) {
	s.fireAckEmotion(runID, message)
}
