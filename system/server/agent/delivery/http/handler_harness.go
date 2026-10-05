package http

import (
	"errors"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

type harnessReplyState struct {
	created     time.Time
	webChat     bool
	delegated   bool
	localOnly   bool
	delivered   bool
	restored    bool
	toolName    string
	toolArgs    string
	questionIDs map[string]bool
	lastLine    string
}

// pushHarnessLine shows one Harness line in chat as its own line. With dedupe, a status
// line equal to the run's previous one is dropped so repeated receipt updates don't pile up.
func (h *AgentHandler) pushHarnessLine(runID, text string, dedupe bool, detail map[string]string) {
	if h.monitorBus == nil {
		return
	}
	h.harnessRepliesMu.Lock()
	if state, ok := h.harnessReplies[runID]; ok {
		if dedupe && state.lastLine == text {
			h.harnessRepliesMu.Unlock()
			return
		}
		state.lastLine = text
		h.harnessReplies[runID] = state
	}
	h.harnessRepliesMu.Unlock()
	h.monitorBus.Push(domain.MonitorEvent{Type: "assistant_delta", Summary: text + "\n", RunID: runID, Detail: detail})
}

// MarkHarnessResponseRun holds a user turn open for the final recap from its
// paired Harness agent. webChat selects display-only delivery.
func (h *AgentHandler) MarkHarnessResponseRun(runID string, webChat, delegated bool) {
	h.markHarnessResponseRun(runID, webChat, delegated, false)
}

// MarkHarnessLocalResponseRun keeps device-generated errors distinct from remote results.
func (h *AgentHandler) MarkHarnessLocalResponseRun(runID string, webChat bool) {
	h.markHarnessResponseRun(runID, webChat, false, true)
}

func (h *AgentHandler) markHarnessResponseRun(runID string, webChat, delegated, localOnly bool) {
	if runID == "" {
		return
	}
	// Harness run ids carry no creation stamp; date the run from registration so the click
	// watermark isn't compared against its later first reply.
	h.runCreatedAtMs(runID)
	h.harnessRepliesMu.Lock()
	if h.harnessReplies == nil {
		h.harnessReplies = make(map[string]harnessReplyState)
	}
	for id, state := range h.harnessReplies {
		if time.Since(state.created) > 15*time.Minute {
			delete(h.harnessReplies, id)
		}
	}
	if _, exists := h.harnessReplies[runID]; !exists {
		h.harnessReplies[runID] = harnessReplyState{webChat: webChat, delegated: delegated, localOnly: localOnly, created: time.Now()}
	} else if localOnly {
		state := h.harnessReplies[runID]
		state.localOnly = true
		h.harnessReplies[runID] = state
	}
	h.harnessRepliesMu.Unlock()
}

// suppressHarnessAgentReply keeps a tombstone until expiry so a late OpenClaw generic final
// cannot overwrite the Harness response.
func (h *AgentHandler) suppressHarnessAgentReply(runID string) bool {
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	_, ok := h.harnessReplies[runID]
	return ok
}

func (h *AgentHandler) clearHarnessResponseRun(runID string) {
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	if state, ok := h.harnessReplies[runID]; ok && time.Since(state.created) > 15*time.Minute {
		delete(h.harnessReplies, runID)
	}
}

// DeliverHarnessProgress shows a Harness lifecycle update while the original turn is pending.
// It never speaks.
func (h *AgentHandler) DeliverHarnessProgress(runID, text string) bool {
	if runID == "" || text == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered {
		return false
	}
	h.pushHarnessLine(runID, text, true, map[string]string{"role": "assistant", "source": "harness"})
	return true
}

// DeliverHarnessTool retains a remote tool start for the voice filler; stored as well as forwarded
// because tool events can beat the device agent's NO_REPLY lifecycle.
func (h *AgentHandler) DeliverHarnessTool(runID, toolName, toolArgs string) bool {
	if runID == "" || toolName == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	if pending && !state.delivered {
		state.toolName = toolName
		state.toolArgs = toolArgs
		h.harnessReplies[runID] = state
	}
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered {
		return false
	}
	if !state.webChat {
		sensinghttp.DefaultFillerManager.OnToolStart(runID, toolArgs, toolName)
	}
	h.pushHarnessLine(runID, "Harness is "+toolName+".", true, map[string]string{"role": "assistant", "source": "harness"})
	return true
}

// ResumeHarnessVoiceFillers recreates a voice-only filler after the device agent returns NO_REPLY,
// so its lifecycle cancellation doesn't silence the delegated Harness task.
func (h *AgentHandler) ResumeHarnessVoiceFillers(runID string) {
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered || state.webChat {
		return
	}
	sensinghttp.DefaultFillerManager.MarkVoiceRun(runID, "")
	sensinghttp.DefaultFillerManager.OnTurnStart(runID)
	if state.toolName != "" {
		sensinghttp.DefaultFillerManager.OnToolStart(runID, state.toolArgs, state.toolName)
	}
}

// DeliverHarnessResponse emits the paired agent's final recap as the response
// to the original device turn. Web chat is display-only; voice uses normal TTS.
func (h *AgentHandler) DeliverHarnessResponse(runID, text string) bool {
	if runID == "" || text == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, ok := h.harnessReplies[runID]
	if !ok || state.delivered {
		h.harnessRepliesMu.Unlock()
		return false
	}
	state.delivered = true
	h.harnessReplies[runID] = state
	h.harnessRepliesMu.Unlock()
	flow.Log("harness_response", map[string]any{"run_id": runID, "text": text}, runID)
	// A final remote answer replaces any generic progress filler immediately.
	sensinghttp.DefaultFillerManager.Cancel(runID)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "chat_response", Summary: text, RunID: runID, State: "final",
			Detail: map[string]string{"role": "assistant", "message": text, "source": "harness"},
		})
	}
	if !state.webChat && !state.restored {
		// Same cancel gate as every reply: a click-cancelled run keeps history but loses the speaker.
		// Remote results go through HAL's announcer; device notices are already speech text.
		speak := func(text string) error {
			return hal.AnnounceHarnessUpdate(hal.HarnessUpdateResult, text, runID, "")
		}
		if state.localOnly {
			speak = hal.SpeakReply
		}
		cancelled := h.isHarnessSpeechCancelled
		if state.localOnly {
			cancelled = h.isSpeechCancelled
		}
		h.deliverTTSUnless(cancelled, speak, text, runID, "speak Harness result")
	}
	return true
}

// AnnounceHarnessProgress offers a progress update to HAL's announcer, which speaks only occasionally
// (HAL HARNESS_PROGRESS_SPEAK_P).
func (h *AgentHandler) AnnounceHarnessProgress(runID, text string) {
	if runID == "" || text == "" {
		return
	}
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered || state.webChat || state.restored || state.localOnly {
		return
	}
	// Progress never records a speech-cancel or feeds history; a cancelled run stays quiet.
	if h.isHarnessSpeechCancelled(runID) {
		return
	}
	go func() {
		if err := hal.AnnounceHarnessUpdate(hal.HarnessUpdateProgress, text, runID, ""); err != nil && !errors.Is(err, hal.ErrSpeakerMuted) {
			slog.Debug("Harness progress announcement not queued", "component", "agent", "run_id", runID, "error", err)
		}
	}()
}

// AnnounceHarnessNotice speaks a device-level Harness notice that belongs to no local run.
func (h *AgentHandler) AnnounceHarnessNotice(text string) {
	if text == "" {
		return
	}
	go func() {
		if err := hal.AnnounceHarnessUpdate(hal.HarnessUpdateResult, text, "", ""); err != nil && !errors.Is(err, hal.ErrSpeakerMuted) {
			slog.Warn("Harness notice announcement not queued", "component", "agent", "error", err)
		}
	}()
}

// MarkHarnessRestoredRun restores a display address without reviving its speaker.
func (h *AgentHandler) MarkHarnessRestoredRun(runID string) {
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	if state, ok := h.harnessReplies[runID]; ok {
		state.restored = true
		h.harnessReplies[runID] = state
	}
}
