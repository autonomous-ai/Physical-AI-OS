package http

import (
	"log/slog"
	"math/rand"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/server/serializers"
)

// poolsForLang returns the (opening, continuation) pools for a BCP-47 STT
// language code.
func poolsForLang(lang string) (opening, continuation []string) {
	return i18n.FillerOpening(lang), i18n.FillerContinuation(lang)
}

// realtimePoolForLang is deliberately separate from the main-agent Opening
// pool: when the realtime model is thinking, the user has already finished
// speaking, so a quiet non-lexical thought is more natural than "got it".
func realtimePoolForLang(lang string) []string {
	return i18n.FillerRealtime(lang)
}

// toolPoolForLang returns the tool-specific filler pool for (lang, toolName).
func toolPoolForLang(lang, toolName string) []string {
	return i18n.FillerForTool(lang, toolName)
}

// Filler tuning. All durations are wall-clock.
const (
	// FillerDelay is how long to wait after the agent starts (or finishes a
	// non-reactive tool) before speaking a filler.
	FillerDelay = 3500 * time.Millisecond

	// FillerCooldown is the minimum gap between two filler reactions in the
	// same turn — covers both filler-spoken and hardware-reaction events.
	FillerCooldown = 2500 * time.Millisecond

	// MaxFillersPerTurn includes the reserved opening slot (also for delegates),
	// leaving at most one automatic continuation in each turn.
	MaxFillersPerTurn = 2
)

// fillerCancelToolMarkers are URL fragments for tool calls that themselves
// act as audible/visible reactions — when one fires, no filler is needed at
// that moment because the user already perceived the device reacting.
var fillerCancelToolMarkers = []string{"/emotion", "/audio/play", "/scene", "/servo"}

// isHWReactionTool reports whether toolArgs invokes a hardware reaction.
func isHWReactionTool(toolArgs string) bool {
	if toolArgs == "" {
		return false
	}
	for _, m := range fillerCancelToolMarkers {
		if strings.Contains(toolArgs, m) {
			return true
		}
	}
	return false
}

// fillerRun is the per-turn state tracked by FillerManager.
type fillerRun struct {
	timer          *time.Timer
	playing        bool
	fired          int       // count of fillers actually spoken this turn
	lastActivityAt time.Time // last time something audible/visible happened (filler or HW tool)
	ended          bool      // turn finalized or explicitly cancelled — no more arms
	suspended      bool      // assistant text is streaming; only a new tool.start may resume
	generation     uint64    // invalidates timer callbacks and speech completions after suspension
	lastSpoken     string    // text of the most recent filler — used to dedup back-to-back picks
	rearmPending   bool      // tool.end arrived while playing; fire() re-arms after speaking
	armOnTool      bool      // delegated realtime turn: first filler waits for the first tool.start
	lastToolName   string    // most recently started tool; selects the tool-aware filler pool
}

// fillersDisabled reports whether both English pools are empty — the kill
// switch.
func fillersDisabled() bool {
	return len(i18n.FillerOpening(i18n.LangEN)) == 0 && len(i18n.FillerContinuation(i18n.LangEN)) == 0
}

// pickFiller returns a phrase appropriate for the current turn position in
// the active language (read from i18n.Lang()), avoiding lastSpoken when an
// alternative exists.
func pickFiller(fired int, lastSpoken string) string {
	lang := i18n.Lang()
	// Automatic waiting feedback must not claim a tool-specific action.
	opening, continuation := poolsForLang(lang)
	primary, fallback := opening, continuation
	if fired > 0 {
		primary, fallback = continuation, opening
	}
	if pick := pickFrom(primary, lastSpoken); pick != "" {
		return pick
	}
	return pickFrom(fallback, lastSpoken)
}

// classifyFillerPool reports which pool the given filler text came from,
// purely for debug logging on the fire path.
func classifyFillerPool(filler, toolName string, fired int, lang string) string {
	if filler == "" {
		return "none"
	}
	opening, continuation := poolsForLang(lang)
	if poolContains(opening, filler) {
		return "opening"
	}
	if poolContains(continuation, filler) {
		return "continuation"
	}
	return "unknown"
}

// poolContains is a tiny linear-scan helper; pools are at most ~12 entries
// so a map lookup isn't worth the allocation churn.
func poolContains(pool []string, s string) bool {
	for _, p := range pool {
		if p == s {
			return true
		}
	}
	return false
}

// pickFrom returns a random entry from pool.
func pickFrom(pool []string, lastSpoken string) string {
	switch len(pool) {
	case 0:
		return ""
	case 1:
		return pool[0]
	}
	pick := pool[rand.Intn(len(pool))]
	if pick == lastSpoken {
		pick = pool[rand.Intn(len(pool))]
		if pick == lastSpoken {
			for i, p := range pool {
				if p == lastSpoken {
					pick = pool[(i+1)%len(pool)]
					break
				}
			}
		}
	}
	return pick
}

// PrewarmFillers asks hal to render+save WAV for every filler phrase in the
// active STT language (read from i18n.Lang()) so the first runtime fire is a
// cache hit (no ElevenLabs roundtrip).
func PrewarmFillers() {
	lang := i18n.Lang()
	const (
		readyMaxWait   = 120 * time.Second
		readyInterval  = 2 * time.Second
		perPhraseRetry = 3
	)
	deadline := time.Now().Add(readyMaxWait)
	ready := false
	for time.Now().Before(deadline) {
		if _, err := hal.GetHealth(); err == nil {
			ready = true
			break
		}
		time.Sleep(readyInterval)
	}
	if !ready {
		slog.Warn("filler prewarm aborted: hal /health not reachable", "component", "sensing")
		return
	}

	opening, continuation := poolsForLang(lang)
	all := append([]string{}, opening...)
	all = append(all, continuation...)
	all = append(all, realtimePoolForLang(lang)...)
	for _, pool := range i18n.AllPoolKeys() {
		all = append(all, i18n.FillerForTool(lang, pool)...)
	}
	all = append(all, intent.CacheableReplies...)
	seen := make(map[string]struct{}, len(all))
	unique := make([]string, 0, len(all))
	for _, p := range all {
		if _, ok := seen[p]; ok {
			continue
		}
		seen[p] = struct{}{}
		unique = append(unique, p)
	}
	all = unique
	rendered := 0
	for _, phrase := range all {
		var lastErr error
		for attempt := 1; attempt <= perPhraseRetry; attempt++ {
			if err := hal.PrerenderCached(phrase); err != nil {
				lastErr = err
				time.Sleep(time.Duration(attempt) * time.Second)
				continue
			}
			lastErr = nil
			break
		}
		if lastErr != nil {
			slog.Warn("filler prerender failed", "component", "sensing", "phrase", phrase, "error", lastErr)
			continue
		}
		rendered++
		slog.Debug("filler prerendered", "component", "sensing", "phrase", phrase)
	}
	slog.Info("filler cache prewarm complete", "component", "sensing", "lang", lang, "rendered", rendered, "total", len(all))
}

// PlayOpeningFillerNow fires a single Opening-pool filler immediately,
// fire-and-forget, without going through FillerManager.
func PlayOpeningFillerNow(owner string) {
	// Temporarily pause opening acknowledgments; retain playback for re-enabling.
	const openingFillerPaused = true
	if openingFillerPaused {
		return
	}
	lang := i18n.Lang()
	opening, _ := poolsForLang(lang)
	if len(opening) == 0 {
		return
	}
	filler := pickFrom(opening, "")
	if filler == "" {
		return
	}
	slog.Info("opening filler firing (immediate, cached)", "component", "sensing", "lang", lang, "filler", filler, "owner", owner)
	if err := hal.SpeakCachedInterruptibleForTurn(filler, owner); err != nil {
		slog.Warn("opening filler failed", "component", "sensing", "error", err)
	}
}

// PlayFiller speaks one realtime filler on demand.
func (h *SensingHandler) PlayFiller(c *gin.Context) {
	// Optional body selects a specific pool. Bodyless calls use the dedicated
	// realtime-wait pool, so the main-agent opening pool remains unchanged.
	var req struct {
		Pool string `json:"pool"`
		// Owner is an opaque tag HAL sends back to itself so a played filler
		// can be attributed to the utterance it was armed for (voice
		// metrics).
		Owner string `json:"owner"`
	}
	_ = c.ShouldBindJSON(&req)
	if req.Pool != "" {
		go PlayPoolFillerNow(req.Pool, req.Owner)
	} else {
		go PlayRealtimeFillerNow(req.Owner)
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// PlayRealtimeFillerNow speaks one quiet, non-lexical cue while the realtime
// model has not produced its first audio frame.
func PlayRealtimeFillerNow(owner string) {
	lang := i18n.Lang()
	filler := pickFrom(realtimePoolForLang(lang), "")
	if filler == "" {
		return
	}
	slog.Info("realtime filler firing (cached)", "component", "sensing", "lang", lang, "filler", filler, "owner", owner)
	if err := hal.SpeakCachedInterruptibleForTurn(filler, owner); err != nil {
		slog.Warn("realtime filler failed", "component", "sensing", "error", err)
	}
}

// PlayPoolFillerNow speaks one phrase from a named tool pool.
func PlayPoolFillerNow(pool, owner string) {
	lang := i18n.Lang()
	phrases := toolPoolForLang(lang, pool)
	if len(phrases) == 0 {
		return
	}
	filler := pickFrom(phrases, "")
	if filler == "" {
		return
	}
	slog.Info("pool filler firing", "component", "sensing", "lang", lang, "pool", pool, "filler", filler, "owner", owner)
	if err := hal.SpeakCachedInterruptibleForTurn(filler, owner); err != nil {
		slog.Warn("pool filler failed", "component", "sensing", "pool", pool, "error", err)
	}
}

// speakCue plays a SayInVoiceRun cue; a var so tests can capture it.
var speakCue = hal.SpeakCachedInterruptibleForTurn

// FillerManager schedules and cancels dead-air fillers driven by OpenClaw
// agent events. Safe for concurrent use; all exported methods are idempotent.
type FillerManager struct {
	supersededBefore atomic.Int64
	mu               sync.Mutex
	runs             map[string]*fillerRun
	voiceRuns        map[string]bool
	// delegated marks voice runs the realtime model handed off after speaking
	// its own filler — see fillerRun.armOnTool.
	delegated map[string]bool
	// interactions maps a run to HAL's voice-metrics interaction id, so a filler
	// fired later in the turn is attributed the same way the opening one is.
	interactions map[string]string
	// Suppression survives Cancel because a delegated task can resume fillers
	// after the device agent finishes. Keep only a bounded set of recent runs.
	suppressed      map[string]bool
	suppressedOrder []string
	// cueRuns are suppressed voice turns that started: no automatic filler, but
	// SayInVoiceRun may still announce an action the agent chose. The value is
	// true while the reply streams.
	cueRuns map[string]bool
}

// NewFillerManager constructs an empty FillerManager. Language is read at
// fire time from lib/i18n, so no config wiring is needed here.
func NewFillerManager() *FillerManager {
	return &FillerManager{
		runs:         make(map[string]*fillerRun),
		voiceRuns:    make(map[string]bool),
		delegated:    make(map[string]bool),
		interactions: make(map[string]string),
		suppressed:   make(map[string]bool),
		cueRuns:      make(map[string]bool),
	}
}

// DefaultFillerManager is the process-wide singleton shared by the sensing
// HTTP handler (MarkVoiceRun) and the OpenClaw SSE event handler
// (OnTurnStart/OnToolStart/OnToolEnd/Cancel).
var DefaultFillerManager = NewFillerManager()

const maxSuppressedFillerRuns = 4096

// SuppressRun disables automatic fillers for a turn, including re-registration
// after queued replay or a delegated task resumes. Call before dispatch.
func (fm *FillerManager) SuppressRun(runID string) {
	if runID == "" || fm.Superseded(runID) {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if fm.suppressed[runID] {
		return
	}
	if len(fm.suppressedOrder) >= maxSuppressedFillerRuns {
		delete(fm.suppressed, fm.suppressedOrder[0])
		fm.suppressedOrder = fm.suppressedOrder[1:]
	}
	fm.suppressed[runID] = true
	fm.suppressedOrder = append(fm.suppressedOrder, runID)
}

// MarkVoiceRun marks runID as eligible for fillers. Other turn types
// (Telegram, web chat, passive sensing, cron, guard) must NOT be marked.
func (fm *FillerManager) MarkVoiceRun(runID, interactionID string) {
	if runID == "" || fm.Superseded(runID) {
		return
	}
	fm.mu.Lock()
	if fm.suppressed[runID] {
		fm.mu.Unlock()
		return
	}
	fm.voiceRuns[runID] = true
	if interactionID != "" {
		fm.interactions[runID] = interactionID
	}
	fm.mu.Unlock()
}

// MarkDelegatedVoiceRun is MarkVoiceRun for a turn the realtime model handed
// off to the main agent (`[voice-instruction]`).
func (fm *FillerManager) MarkDelegatedVoiceRun(runID, interactionID string) {
	fm.MarkVoiceRun(runID, interactionID)
	if runID == "" || fm.Superseded(runID) {
		return
	}
	fm.mu.Lock()
	if !fm.suppressed[runID] {
		fm.delegated[runID] = true
	}
	fm.mu.Unlock()
}

// fillerOwner is the tag HAL attributes played filler audio to: the
// voice-metrics interaction when HAL sent one, else the run id.
func fillerOwner(interactionID, runID string) string {
	if interactionID != "" {
		return interactionID
	}
	return runID
}

// OnTurnStart records the run as active and arms a Continuation timer so dead
// air gets filled even when the agent thinks without invoking any tool (no
// tool.end -> no OnToolEnd re-arm without this).
func (fm *FillerManager) OnTurnStart(runID string) {
	if runID == "" {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if fm.Superseded(runID) {
		return
	}
	if fm.suppressed[runID] {
		if _, ok := fm.cueRuns[runID]; !ok {
			fm.cueRuns[runID] = false
		}
		return
	}
	if fillersDisabled() || !fm.voiceRuns[runID] {
		return
	}
	delete(fm.voiceRuns, runID)
	delete(fm.interactions, runID)
	delegated := fm.delegated[runID]
	delete(fm.delegated, runID)
	if _, exists := fm.runs[runID]; exists {
		return
	}
	run := &fillerRun{fired: 1, lastActivityAt: time.Now(), armOnTool: delegated}
	fm.runs[runID] = run
	if delegated {
		slog.Info("filler OnTurnStart deferred to first tool (delegated realtime turn)", "component", "sensing", "run_id", runID)
		return
	}
	fm.armLocked(runID, run, FillerDelay)
}

// OnToolStart records the tool name for diagnostics and soft-cancels
// the pending filler when the tool is itself a hardware reaction.
func (fm *FillerManager) OnToolStart(runID, toolArgs, toolName string) {
	if runID == "" || fm.Superseded(runID) {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if _, ok := fm.cueRuns[runID]; ok {
		fm.cueRuns[runID] = false
		return
	}
	run, ok := fm.runs[runID]
	if !ok || run.ended {
		slog.Debug("filler OnToolStart skipped — no active run", "component", "sensing", "run_id", runID, "tool", toolName)
		return
	}
	if toolName != "" {
		run.lastToolName = toolName
	}
	wasSuspended := run.suspended
	run.suspended = false
	hw := isHWReactionTool(toolArgs)
	slog.Info("filler OnToolStart", "component", "sensing", "run_id", runID, "tool", toolName, "hw", hw, "fired", run.fired, "playing", run.playing, "timer_armed", run.timer != nil)
	if !hw {
		if wasSuspended {
			fm.armLocked(runID, run, fillerRearmDelay(run))
		} else if run.armOnTool {
			fm.armLocked(runID, run, FillerDelay)
		}
		run.armOnTool = false
		return
	}
	run.armOnTool = false
	fm.softCancelLocked(run)
}

// OnToolEnd attempts to re-arm a filler timer after a tool finishes — the
// turn may still have minutes of thinking ahead.
func (fm *FillerManager) OnToolEnd(runID string) {
	if runID == "" || fillersDisabled() {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	run, ok := fm.runs[runID]
	if !ok || run.ended || run.suspended {
		slog.Debug("filler OnToolEnd skipped — no active run", "component", "sensing", "run_id", runID)
		return
	}
	if run.playing {
		run.rearmPending = true
		slog.Info("filler OnToolEnd deferred (playing) — rearm after speak", "component", "sensing", "run_id", runID, "tool", run.lastToolName, "fired", run.fired)
		return
	}
	delay := fillerRearmDelay(run)
	slog.Info("filler OnToolEnd arming", "component", "sensing", "run_id", runID, "tool", run.lastToolName, "fired", run.fired, "delay_ms", delay.Milliseconds())
	fm.armLocked(runID, run, delay)
}

// fillerRearmDelay preserves the cooldown when a tool resumes work.
func fillerRearmDelay(run *fillerRun) time.Duration {
	delay := FillerDelay
	if !run.lastActivityAt.IsZero() {
		// Add the regular delay on top so we don't immediately re-fire the
		// moment cooldown elapses — give the next thought a chance.
		if elapsed := time.Since(run.lastActivityAt); elapsed < FillerCooldown {
			delay = (FillerCooldown - elapsed) + FillerDelay
		}
	}
	return delay
}

// OnAssistantText interrupts fillers without ending a voice turn.
func (fm *FillerManager) OnAssistantText(runID string) {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if _, ok := fm.cueRuns[runID]; ok {
		fm.cueRuns[runID] = true
		return
	}
	run, ok := fm.runs[runID]
	if !ok || run.ended || run.suspended {
		return
	}
	run.suspended = true
	fm.softCancelLocked(run)
}

// Cancel hard-cancels the run: stop pending timer, interrupt any filler
// mid-speech, mark the run ended so future tool events are no-ops, and drop
// the entry from the runs map.
func (fm *FillerManager) Cancel(runID string) {
	fm.cancel(runID, true)
}

func (fm *FillerManager) cancel(runID string, stopPlayback bool) {
	if runID == "" {
		return
	}
	fm.mu.Lock()
	delete(fm.voiceRuns, runID)
	delete(fm.delegated, runID)
	delete(fm.interactions, runID)
	delete(fm.cueRuns, runID)
	run, ok := fm.runs[runID]
	if !ok {
		fm.mu.Unlock()
		return
	}
	run.ended = true
	if run.timer != nil {
		run.timer.Stop()
		run.timer = nil
	}
	wasPlaying := run.playing
	run.playing = false
	delete(fm.runs, runID)
	fm.mu.Unlock()

	if wasPlaying && stopPlayback {
		go func() {
			if err := hal.StopTTS(); err != nil {
				slog.Warn("filler stop TTS failed", "component", "sensing", "run_id", runID, "error", err)
			}
		}()
	}
}

// CancelAllActive hard-cancels every run currently holding filler state, and
// reports how many filler runs there were. It also drops the cue-only suppressed
// turns, which hold no filler and so are not counted. Called by the physical
// cancel gesture.
func (fm *FillerManager) CancelAllActive() int {
	return fm.cancelMatching(func(string) bool { return true }, true)
}

// cancelMatching invokes the predicate outside fm.mu.
func (fm *FillerManager) cancelMatching(matches func(string) bool, stopPlayback bool) int {
	fm.mu.Lock()
	runIDs := make([]string, 0, len(fm.runs))
	for runID := range fm.runs {
		runIDs = append(runIDs, runID)
	}
	cueIDs := make([]string, 0, len(fm.cueRuns))
	for runID := range fm.cueRuns {
		cueIDs = append(cueIDs, runID)
	}
	fm.mu.Unlock()
	count := 0
	for _, runID := range runIDs {
		if matches(runID) {
			fm.cancel(runID, stopPlayback)
			count++
		}
	}
	// Cue-only suppressed turns hold no filler: dropped, but not counted.
	for _, runID := range cueIDs {
		if matches(runID) {
			fm.cancel(runID, stopPlayback)
		}
	}
	return count
}

// cueTargetLocked picks the voice turn a cue speaks on: a filler run first, else
// a suppressed (cue-only) turn, whose run is nil. Caller holds fm.mu.
func (fm *FillerManager) cueTargetLocked() (string, *fillerRun) {
	for id, r := range fm.runs {
		if !r.ended && !r.suspended {
			return id, r
		}
	}
	for id, streaming := range fm.cueRuns {
		if !streaming {
			return id, nil
		}
	}
	return "", nil
}

// SayInVoiceRun speaks one phrase from pool for the voice turn in progress and
// pushes that turn's dead-air filler back by its cooldown, so a generic "Hmm..."
// does not land on top of the cue. It is silent and returns false when no voice
// turn is running (Telegram, web chat, cron) or the reply is already streaming.
// The cue does not count toward MaxFillersPerTurn. A suppressed voice follow-up
// (SuppressRun) still gets cues, which announce an action, never automatic fillers.
func (fm *FillerManager) SayInVoiceRun(pool string) bool {
	phrases := toolPoolForLang(i18n.Lang(), pool)
	if len(phrases) == 0 {
		return false
	}
	fm.mu.Lock()
	runID, run := fm.cueTargetLocked()
	if runID == "" {
		fm.mu.Unlock()
		return false
	}
	// A cue-only turn has no dead-air filler to push back.
	if run != nil {
		if run.timer != nil {
			run.timer.Stop()
			run.timer = nil
			run.generation++
		}
		run.lastActivityAt = time.Now()
		fm.armLocked(runID, run, fillerRearmDelay(run))
	}
	owner := fillerOwner(fm.interactions[runID], runID)
	fm.mu.Unlock()

	filler := pickFrom(phrases, "")
	slog.Info("voice cue firing", "component", "sensing", "run_id", runID, "pool", pool, "filler", filler, "owner", owner)
	if err := speakCue(filler, owner); err != nil {
		slog.Warn("voice cue failed", "component", "sensing", "pool", pool, "error", err)
	}
	return true
}

// HasActiveRun reports whether runID still holds filler state. Exported for
// the agent handler's tests, which assert that a muted turn stops re-arming.
func (fm *FillerManager) HasActiveRun(runID string) bool {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	run, ok := fm.runs[runID]
	return ok && !run.ended
}

// armLocked schedules a filler timer for run after delay. Caller holds fm.mu.
// No-op when the run has ended, the cap is reached, or a timer/filler is already active.
func (fm *FillerManager) armLocked(runID string, run *fillerRun, delay time.Duration) {
	if run.ended || run.suspended || fm.Superseded(runID) {
		slog.Debug("filler arm blocked — ended", "component", "sensing", "run_id", runID)
		return
	}
	if run.fired >= MaxFillersPerTurn {
		slog.Info("filler arm blocked — cap reached", "component", "sensing", "run_id", runID, "fired", run.fired, "cap", MaxFillersPerTurn)
		return
	}
	if run.timer != nil {
		slog.Debug("filler arm blocked — timer already pending", "component", "sensing", "run_id", runID)
		return
	}
	if run.playing {
		slog.Debug("filler arm blocked — currently playing", "component", "sensing", "run_id", runID)
		return
	}
	generation := run.generation
	run.timer = time.AfterFunc(delay, func() { fm.fire(runID, run, generation) })
}

// softCancelLocked clears a pending timer and interrupts in-flight TTS, but
// keeps the run alive so OnToolEnd can re-arm later.
func (fm *FillerManager) softCancelLocked(run *fillerRun) {
	run.generation++
	run.rearmPending = false
	if run.timer != nil {
		run.timer.Stop()
		run.timer = nil
	}
	wasPlaying := run.playing
	run.playing = false
	run.lastActivityAt = time.Now()
	if wasPlaying {
		go func() {
			if err := hal.StopTTS(); err != nil {
				slog.Warn("filler stop TTS failed (soft cancel)", "component", "sensing", "error", err)
			}
		}()
	}
}

// fire is the timer callback. It re-checks state under the lock and speaks
// outside it.
func (fm *FillerManager) fire(runID string, expectedRun *fillerRun, generation uint64) {
	fm.mu.Lock()
	run, ok := fm.runs[runID]
	if fm.Superseded(runID) || !ok || run != expectedRun || run.generation != generation || run.ended || run.suspended || run.timer == nil {
		fm.mu.Unlock()
		return
	}
	// Temporarily pause continuation audio; retain the timer lifecycle and playback code.
	const continuationFillerPaused = true
	if continuationFillerPaused {
		run.timer = nil
		fm.mu.Unlock()
		return
	}
	filler := pickFiller(run.fired, run.lastSpoken)
	if filler == "" {
		run.timer = nil
		fm.mu.Unlock()
		slog.Warn("filler fire bail — both pools empty", "component", "sensing", "run_id", runID, "tool", run.lastToolName)
		return
	}
	run.timer = nil
	run.playing = true
	toolName := run.lastToolName
	fired := run.fired
	run.fired++
	fm.mu.Unlock()

	fm.mu.Lock()
	owner := fillerOwner(fm.interactions[runID], runID)
	fm.mu.Unlock()
	pool := classifyFillerPool(filler, toolName, fired, i18n.Lang())
	slog.Info("dead air filler firing", "component", "sensing", "run_id", runID, "filler", filler, "tool", toolName, "fired", fired, "pool", pool)
	if err := hal.SpeakCachedInterruptibleForTurn(filler, owner); err != nil {
		slog.Warn("dead air filler failed", "component", "sensing", "run_id", runID, "error", err)
	}

	fm.finishFiller(runID, expectedRun, generation, filler)
}

// finishFiller ignores completions from speech interrupted by assistant text
// or hardware reactions, including after the run has already resumed.
func (fm *FillerManager) finishFiller(runID string, expectedRun *fillerRun, generation uint64, filler string) {
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if run, ok := fm.runs[runID]; ok && run == expectedRun && run.generation == generation && !run.ended && !run.suspended {
		run.playing = false
		run.lastActivityAt = time.Now()
		run.lastSpoken = filler
		if run.rearmPending {
			run.rearmPending = false
			fm.armLocked(runID, run, FillerDelay)
		}
	}
}
