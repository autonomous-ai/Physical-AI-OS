package http

import (
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// speakVoiceFailure is hal.SpeakCached; a var so tests can capture it.
var speakVoiceFailure = hal.SpeakCached

// voiceFailureDebounce keeps one failing brain from narrating every retry.
const voiceFailureDebounce = 20 * time.Second

// speakVoiceTurnFailure tells the user a spoken request failed instead of
// leaving them waiting in silence. Only runs HAL marked as spoken voice turns
// qualify; chat, cron and passive sensing runs stay quiet. Reports whether the
// notice was sent.
func (h *AgentHandler) speakVoiceTurnFailure(runID string) bool {
	resolved := h.resolveRunID(runID)
	if !sensinghttp.DefaultFillerManager.IsVoiceRun(resolved) || h.isSpeechCancelled(resolved) {
		return false
	}
	now := time.Now().UnixMilli()
	last := h.lastVoiceFailureTTS.Load()
	if now-last < voiceFailureDebounce.Milliseconds() || !h.lastVoiceFailureTTS.CompareAndSwap(last, now) {
		return false
	}
	phrase := i18n.One(i18n.PhraseVoiceTurnFailed)
	if phrase == "" {
		return false
	}
	slog.Info("voice turn failed — telling the user", "component", "agent", "run_id", runID)
	go func() {
		if err := speakVoiceFailure(phrase); err != nil {
			slog.Warn("voice failure notice failed", "component", "agent", "run_id", runID, "error", err)
		}
	}()
	return true
}
