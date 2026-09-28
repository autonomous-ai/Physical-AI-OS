package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// A lost backend terminal must not retain an interaction indefinitely. HAL
// independently bounds its lease, including when this process disappears.
const followupActivityTTL = 5 * time.Minute

type followupActivity struct {
	InteractionID string `json:"interaction_id"`
	RunID         string `json:"run_id"`
	Phase         string `json:"phase"`
}

type followupRun struct {
	interaction string
	started     time.Time
	pending     int
	ended       bool
	endSent     bool
	createdMS   int64
}

type followupTracker struct {
	mu                sync.Mutex
	runs              map[string]*followupRun
	send              func(followupActivity)
	cancelledBeforeMS int64
}

var voiceFollowup = followupTracker{runs: make(map[string]*followupRun), send: sendFollowupActivity}

// Activity must not delay the user turn behind HAL's normal five-second
// hardware timeout. A lost notification is bounded by HAL's local lease.
var followupHTTPClient = &http.Client{Timeout: 250 * time.Millisecond}

func sendFollowupActivity(activity followupActivity) {
	body, _ := json.Marshal(activity)
	req, err := newRequest(http.MethodPost, "/voice/followup/activity", bytes.NewReader(body))
	if err != nil {
		return
	}
	resp, err := followupHTTPClient.Do(req)
	if err == nil {
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			err = fmt.Errorf("HTTP %d", resp.StatusCode)
		}
	}
	if err != nil {
		slog.Warn("voice follow-up activity delivery failed", "phase", activity.Phase, "run_id", activity.RunID, "error", err)
	}
}

// StartVoiceFollowup binds an already-authorized HAL interaction to a main run.
// Call before dispatch, only for voice input; HAL ignores unknown interactions.
func StartVoiceFollowup(interactionID, runID string) { voiceFollowup.start(interactionID, runID) }

// EndVoiceFollowup releases processing after every accepted TTS submission has
// returned from HAL. Playback itself remains owned and tracked inside HAL.
func EndVoiceFollowup(runID string) { voiceFollowup.end(runID) }

// BeginVoiceFollowupSpeech reserves a submission before launching its worker.
// The returned function must run after admission, rejection, or transport error.
func BeginVoiceFollowupSpeech(runID string) func() { return voiceFollowup.speech(runID) }

// CancelVoiceFollowups mirrors the existing cancellation watermark: stale
// completions cannot recreate an interaction after its speech was cancelled.
func CancelVoiceFollowups(beforeMS int64) { voiceFollowup.cancel(beforeMS) }

func (t *followupTracker) pruneLocked() {
	for id, run := range t.runs {
		if time.Since(run.started) >= followupActivityTTL {
			delete(t.runs, id)
		}
	}
}

func (t *followupTracker) start(interaction, id string) {
	if interaction == "" || id == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked()
	if _, exists := t.runs[id]; exists {
		return
	}
	now := time.Now()
	createdMS := now.UnixMilli()
	// Device run IDs retain their allocation time even if preprocessing
	// overlaps a physical cancel before this dispatch registration.
	if i := strings.LastIndex(id, "-"); i >= 0 && len(id[i+1:]) == 13 {
		if stamp, err := strconv.ParseInt(id[i+1:], 10, 64); err == nil {
			createdMS = stamp
		}
	}
	if createdMS <= t.cancelledBeforeMS {
		return
	}
	t.runs[id] = &followupRun{interaction: interaction, started: now, createdMS: createdMS}
	// Serialize notifications with cancellation and terminal delivery. In
	// particular, an asynchronous start must never arrive after its end.
	t.send(followupActivity{interaction, id, "start"})
}

func (t *followupTracker) finishLocked(id string, run *followupRun) {
	if run.ended && run.pending == 0 && !run.endSent {
		// Admission completion is not physical playback completion. Retain
		// this bounded owner so a later speech cancel also reaches HAL while
		// its accepted audio is still draining.
		run.endSent = true
		t.send(followupActivity{run.interaction, id, "end"})
	}
}

func (t *followupTracker) end(id string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked()
	if run := t.runs[id]; run != nil {
		run.ended = true
		t.finishLocked(id, run)
	}
}

func (t *followupTracker) speech(id string) func() {
	t.mu.Lock()
	t.pruneLocked()
	run := t.runs[id]
	if run != nil {
		run.pending++
	}
	t.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			t.mu.Lock()
			defer t.mu.Unlock()
			t.pruneLocked()
			if run != nil && t.runs[id] == run {
				run.pending--
				t.finishLocked(id, run)
			}
		})
	}
}

func (t *followupTracker) cancel(beforeMS int64) {
	t.mu.Lock()
	t.pruneLocked()
	if beforeMS > t.cancelledBeforeMS {
		t.cancelledBeforeMS = beforeMS
	}
	var cancelled []followupActivity
	for id, run := range t.runs {
		if run.createdMS > beforeMS {
			continue
		}
		delete(t.runs, id)
		cancelled = append(cancelled, followupActivity{run.interaction, id, "cancel"})
	}
	t.mu.Unlock()
	// All preceding start/end requests finished under the lock, and deletion
	// plus the watermark prevents any later callback from reviving these runs.
	// Their cancellations are independent: send together so retained playback
	// owners cost one HTTP timeout, not N timeouts before the speaker is stopped.
	var pending sync.WaitGroup
	for _, activity := range cancelled {
		pending.Add(1)
		go func() {
			defer pending.Done()
			t.send(activity)
		}()
	}
	pending.Wait()
}
