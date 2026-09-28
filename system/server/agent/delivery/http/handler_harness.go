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
	// Harness run ids carry no creation stamp, so the click watermark would
	// otherwise date this run from its first spoken reply -- which lands
	// after the click it should have respected. Date it from registration.
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

// suppressHarnessAgentReply retains ownership after either lifecycle ordering.
// OpenClaw may emit a generic final after lifecycle:end; keep the tombstone
// until expiry so that final cannot overwrite the Harness response.
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

// DeliverHarnessProgress shows a lifecycle update emitted by Harness while the
// original user turn remains pending. It deliberately never speaks: voice
// users hear the terminal result, while Web Chat replaces this pending text
// with that exact result when turn.summary arrives.
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
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "assistant_delta", Summary: text, RunID: runID,
			Detail: map[string]string{"role": "assistant", "source": "harness"},
		})
	}
	return true
}

// DeliverHarnessTool retains a remote tool start for the voice filler that
// stays alive after the device agent has handed work to Harness. Tool events
// can beat the device agent's NO_REPLY lifecycle by a few milliseconds, so
// the tool is stored as well as forwarded to an already-active filler.
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
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "assistant_delta", Summary: "Harness is " + toolName + ".", RunID: runID,
			Detail: map[string]string{"role": "assistant", "source": "harness"},
		})
	}
	return true
}

// ResumeHarnessVoiceFillers recreates a voice-only filler after the device
// agent returns NO_REPLY. Its normal lifecycle cancellation must not silence a
// long-running Harness task that it has just delegated.
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
	// Persist the same final text for web recovery when its live SSE is closed.
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
		// Same gate as every other reply: a run the user cancelled by click
		// keeps its answer in history but loses the speaker. Harness results
		// land tens of seconds later, exactly when a bypass is audible.
		// Remote results are written for a screen, so HAL's announcer renders
		// them for speech; device-generated notices are already spoken text.
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

// AnnounceHarnessProgress offers a Harness lifecycle or tool update to HAL's
// announcer, which speaks only an occasional one (see HAL
// HARNESS_PROGRESS_SPEAK_P). DeliverHarnessProgress stays display-only.
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
	// A progress line is never worth a speech-cancel record or a history feed:
	// a cancelled run simply stays quiet.
	if h.isHarnessSpeechCancelled(runID) {
		return
	}
	go func() {
		if err := hal.AnnounceHarnessUpdate(hal.HarnessUpdateProgress, text, runID, ""); err != nil && !errors.Is(err, hal.ErrSpeakerMuted) {
			slog.Debug("Harness progress announcement not queued", "component", "agent", "run_id", runID, "error", err)
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
