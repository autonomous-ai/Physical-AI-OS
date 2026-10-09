package http

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// llmLimitPatterns fingerprint (lower-case) chunks of the LLM plan-usage-limit banner.
var llmLimitPatterns = []string{
	"reached the usage limit",
	"upgrade for higher limits",
	"your access will reset",
	"upgrade your plan",
}

func isLLMLimitText(text string) bool {
	t := strings.ToLower(text)
	for _, p := range llmLimitPatterns {
		if strings.Contains(t, p) {
			return true
		}
	}
	return false
}

// runFirstSeenTTL bounds how long an unparseable runID stays in the first-seen registry.
const runFirstSeenTTL = 30 * time.Minute

// ttsTurnSequence returns the monotonic sequence for runID, assigning one on first use.
// Sequences persist for the server lifetime so a late POST from an old turn keeps
// its old number and is rejected by HAL.
func (h *AgentHandler) ttsTurnSequence(runID string) uint64 {
	if runID == "" {
		return 0
	}
	h.ttsTurnMu.Lock()
	defer h.ttsTurnMu.Unlock()
	if h.ttsTurnOrder == nil {
		h.ttsTurnOrder = make(map[string]uint64)
	}
	if seq, ok := h.ttsTurnOrder[runID]; ok {
		return seq
	}
	h.ttsTurnNextSeq++
	seq := h.ttsTurnNextSeq
	h.ttsTurnOrder[runID] = seq
	return seq
}

// runCreatedAtMs returns the unix-ms creation time of runID: parsed from a trailing
// 13-digit stamp ("device-chat-7-1755600000000"), else the first time it was seen.
func (h *AgentHandler) runCreatedAtMs(runID string) int64 {
	if i := strings.LastIndex(runID, "-"); i >= 0 && i < len(runID)-1 {
		// Require 13 digits so "tg-<session>-42" is not read as 1970 (would mute forever).
		if suffix := runID[i+1:]; len(suffix) == 13 {
			if ms, err := strconv.ParseInt(suffix, 10, 64); err == nil {
				return ms
			}
		}
	}

	now := time.Now().UnixMilli()
	h.runFirstSeenMu.Lock()
	defer h.runFirstSeenMu.Unlock()
	if h.runFirstSeenMs == nil {
		h.runFirstSeenMs = make(map[string]int64)
	}
	if seen, ok := h.runFirstSeenMs[runID]; ok {
		return seen
	}
	cutoff := now - runFirstSeenTTL.Milliseconds()
	for id, ts := range h.runFirstSeenMs {
		if ts < cutoff {
			delete(h.runFirstSeenMs, id)
		}
	}
	h.runFirstSeenMs[runID] = now
	return now
}

// olderThanWatermark reports whether runID's turn was created at or before mark.
// Resolves backend UUIDs first so speech and HW dispatch get the same verdict per turn.
func (h *AgentHandler) olderThanWatermark(runID string, mark int64) bool {
	if mark == 0 || runID == "" {
		return false
	}
	return h.runCreatedAtMs(h.resolveRunID(runID)) <= mark
}

// isSpeechCancelled reports whether runID predates the click or realtime-supersede watermark.
func (h *AgentHandler) isSpeechCancelled(runID string) bool {
	return h.olderThanWatermark(runID, h.speechWatermarkMs.Load()) ||
		h.olderThanWatermark(runID, h.autoSpeechWatermarkMs.Load())
}

// isHWCancelled reports whether runID's HW markers are dropped. Only the user click
// counts; the system-stamped mark must never drop hardware the user asked for.
func (h *AgentHandler) isHWCancelled(runID string) bool {
	return h.olderThanWatermark(runID, h.speechWatermarkMs.Load())
}

// Cancel sources reported on the tts_cancelled flow event.
const (
	cancelSourceClick    = "click"
	cancelSourceRealtime = "realtime_handled"
)

// speechCancelSource names which mark silenced runID; the click wins when both apply.
func (h *AgentHandler) speechCancelSource(runID string) string {
	if h.olderThanWatermark(runID, h.speechWatermarkMs.Load()) {
		return cancelSourceClick
	}
	return cancelSourceRealtime
}

// CancelSpeech mutes (does not abort) every in-flight turn, including its HW markers
// and fillers. Used by the physical cancel gesture.
func (h *AgentHandler) CancelSpeech() {
	now := time.Now().UnixMilli()
	h.speechWatermarkMs.Store(now)
	// Fillers bypass deliverTTS, so the watermark alone would not stop them.
	cancelledFillers := sensinghttp.DefaultFillerManager.CancelAllActive()
	hal.CancelVoiceFollowups(now)
	slog.Info("speech cancelled -- in-flight turns muted",
		"component", "agent", "watermark_ms", now, "fillers_cancelled", cancelledFillers)
	// Monitor bus, not flow.Log: an empty runID would inherit an unrelated active trace.
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "speech_cancel",
			Summary: "✋ speech cancelled by click — in-flight turns muted",
			Detail:  map[string]any{"watermark_ms": now},
		})
	}
}

// cancelSpeechBefore suppresses older voice replies without aborting agent work.
// The cutoff comes from HAL on the same device, before admitting the new capture.
func (h *AgentHandler) cancelSpeechBefore(beforeMS int64) {
	for {
		old := h.autoSpeechWatermarkMs.Load()
		if old >= beforeMS || h.autoSpeechWatermarkMs.CompareAndSwap(old, beforeMS) {
			break
		}
	}
	sensinghttp.DefaultFillerManager.CancelBefore(beforeMS)
	hal.CancelVoiceFollowups(beforeMS)
}

// RealtimeSupersedesMainReply reports whether OS_REALTIME_SUPERSEDES_MAIN_REPLY is set
// ("1"/"true"). Default off; gates CancelSpeechForNewerTurn.
func RealtimeSupersedesMainReply() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY")))
	return v == "1" || v == "true"
}

// CancelSpeechForNewerTurn mutes in-flight turns (speech and fillers only, never HW)
// after realtime answered a newer utterance. Returns whether the mark was stamped.
func (h *AgentHandler) CancelSpeechForNewerTurn() bool {
	if !RealtimeSupersedesMainReply() {
		return false
	}
	now := time.Now().UnixMilli()
	h.autoSpeechWatermarkMs.Store(now)
	cancelledFillers := sensinghttp.DefaultFillerManager.CancelAllActive()
	hal.CancelVoiceFollowups(now)
	slog.Info("speech auto-cancelled -- realtime answered a newer turn",
		"component", "agent", "watermark_ms", now, "fillers_cancelled", cancelledFillers)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "speech_cancel",
			Summary: "🗣 realtime answered a newer turn — older turn muted",
			Detail:  map[string]any{"watermark_ms": now, "source": "realtime_handled"},
		})
	}
	return true
}

// deliverTTS sends text to HAL asynchronously unless the turn lost the speaker.
// LLM-limit banners are replaced by a debounced localized notice.
func (h *AgentHandler) deliverTTS(send func(string) error, text, flowRunID, errCtx string) {
	h.deliverTTSUnless(h.isSpeechCancelled, send, text, flowRunID, errCtx)
}

// isHarnessSpeechCancelled: only the user click cancels Harness updates; the realtime
// supersede mark would drop long-running Harness results.
func (h *AgentHandler) isHarnessSpeechCancelled(runID string) bool {
	return h.olderThanWatermark(runID, h.speechWatermarkMs.Load())
}

// deliverTTSUnless is deliverTTS with the caller's cancellation rule.
func (h *AgentHandler) deliverTTSUnless(cancelled func(string) bool, send func(string) error, text, flowRunID, errCtx string) {
	if cancelled(flowRunID) {
		source := h.speechCancelSource(flowRunID)
		slog.Info("TTS dropped -- turn lost the speaker",
			"component", "agent", "run_id", flowRunID, "source", source,
			"text", text[:min(len(text), 80)])
		flow.Log("tts_cancelled", map[string]any{"run_id": flowRunID, "text": text, "source": source}, flowRunID)
		// Still feed realtime history: HAL already saved the question awaiting this reply.
		go func() {
			if err := hal.FeedRealtimeHistory(text); err != nil {
				slog.Warn("realtime history feed failed for cancelled turn",
					"component", "agent", "run_id", flowRunID, "error", err)
			}
		}()
		return
	}
	if isLLMLimitText(text) {
		now := time.Now().UnixMilli()
		last := h.lastLLMLimitTTS.Load()
		if now-last < 30_000 || !h.lastLLMLimitTTS.CompareAndSwap(last, now) {
			slog.Info("LLM limit banner chunk suppressed (already announced)",
				"component", "agent", "run_id", flowRunID)
			return
		}
		slog.Warn("LLM usage-limit reply detected — speaking short notice instead",
			"component", "agent", "run_id", flowRunID, "banner", text[:min(len(text), 120)])
		flow.Log("tts_llm_limit", map[string]any{"run_id": flowRunID, "banner": text}, flowRunID)
		// SpeakCached: not fed into realtime history, and replays from WAV cache
		// when the TTS provider shares the exhausted quota.
		text = i18n.One(i18n.PhraseLLMLimit)
		send = hal.SpeakCached
	}
	finishAdmission := hal.BeginVoiceFollowupSpeech(flowRunID)
	dispatchAt := time.Now()
	textKey := ttsTextKey(text)
	go func() {
		defer finishAdmission()
		sendAt := time.Now()
		slog.Info("[tts-timing] delivery_start", "run_id", flowRunID,
			"text_key", textKey, "dispatch_to_send_ms", sendAt.Sub(dispatchAt).Milliseconds())
		err := send(text)
		slog.Info("[tts-timing] delivery_complete", "run_id", flowRunID,
			"text_key", textKey, "send_ms", time.Since(sendAt).Milliseconds(),
			"dispatch_to_complete_ms", time.Since(dispatchAt).Milliseconds(), "success", err == nil)
		if err == nil {
			return
		}
		if errors.Is(err, hal.ErrSpeakerMuted) {
			slog.Info("TTS muted -- speaker muted on device", "component", "agent", "run_id", flowRunID, "text", text[:min(len(text), 80)])
			flow.Log("tts_muted", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
			return
		}
		slog.Error(errCtx, "component", "agent", "error", err)
	}()
}

// deliverTTSQueue sends a reply via the turn-aware TTS queue, falling back to the plain queue.
func (h *AgentHandler) deliverTTSQueue(text, flowRunID, errCtx string) {
	if queue, ok := h.agentGateway.(domain.TurnAwareTTSQueue); ok {
		turnSeq := h.ttsTurnSequence(flowRunID)
		h.deliverTTS(func(text string) error {
			return queue.SendToHALTTSQueueForTurn(text, flowRunID, turnSeq)
		}, text, flowRunID, errCtx)
		return
	}
	h.deliverTTS(h.agentGateway.SendToHALTTSQueue, text, flowRunID, errCtx)
}

// ttsTextKey correlates text at this boundary without logging its contents.
func ttsTextKey(text string) string {
	digest := sha256.Sum256([]byte(text))
	return hex.EncodeToString(digest[:6])
}
