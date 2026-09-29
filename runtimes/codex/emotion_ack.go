package codex

import (
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/skills"
)

// emotion-acknowledge parity for Codex.
const (
	ackEmotionName      = "thinking"
	ackEmotionIntensity = 0.7
)

// ackSkipPrefixes mirror runtimes/openclaw/hooks/emotion-acknowledge/handler.ts exactly: passive
// sensing turns frequently resolve to NO_REPLY, which would leave the face stuck
// on "thinking" with nothing to overwrite it.
var ackSkipPrefixes = []string{
	"[sensing:",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
}

// ackEmotionEnabled reports whether this device installs the emotion-acknowledge
// hook (capability-gated identically to OpenClaw onboarding).
func ackEmotionEnabled(deviceType string) bool {
	for _, h := range skills.SupportedHooks(device.Capabilities(deviceType)) {
		if h == "emotion-acknowledge" {
			return true
		}
	}
	return false
}

// fireAckEmotion drives the "thinking" face for a turn that will produce a
// visible reply.
// Fire-and-forget (the TS handler ignores POST errors too) and off the caller's goroutine so
// sendChat never blocks on the HAL round-trip.
func (s *CodexService) fireAckEmotion(runID, message string) {
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
	safego.Go("codex-ack-emotion", func() {
		if err := hal.SetEmotion(ackEmotionName, ackEmotionIntensity); err != nil {
			slog.Debug("ack emotion post failed", "component", "codex", "error", err)
		}
	})
}
