package schedule

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestResolveKind checks everything but "speak" (including empty) resolves to agent.
func TestResolveKind(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"empty is agent (every pre-existing schedule)", "", KindAgent},
		{"explicit agent", "agent", KindAgent},
		{"explicit speak", "speak", KindSpeak},
		{"speak is case-insensitive", "Speak", KindSpeak},
		{"speak tolerates surrounding space", "  speak\n", KindSpeak},
		{"whitespace only is agent", "   ", KindAgent},
		{"unknown kind degrades to agent, never rejected", "sing", KindAgent},
		{"a future kind this firmware predates degrades to agent", "video", KindAgent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ResolveKind(tc.in); got != tc.want {
				t.Fatalf("ResolveKind(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// TestScheduleKindParsesFromSyncWire checks the wire "kind" key lands on Schedule.
func TestScheduleKindParsesFromSyncWire(t *testing.T) {
	const wire = `{
		"id": "s1",
		"name": "Morning nudge",
		"instructions": "Good morning. Stand up and stretch.",
		"enabled": true,
		"kind": "speak",
		"schedule": {"repeat": "daily", "time": "08:00"},
		"end_at": null,
		"rev": 3
	}`

	var got Schedule
	if err := json.Unmarshal([]byte(wire), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Kind != KindSpeak {
		t.Fatalf("Kind = %q, want %q", got.Kind, KindSpeak)
	}
	if ResolveKind(got.Kind) != KindSpeak {
		t.Fatalf("ResolveKind(%q) did not resolve to speak", got.Kind)
	}
}

// TestScheduleWithoutKindOnWireIsAgent checks a missing kind resolves to agent.
func TestScheduleWithoutKindOnWireIsAgent(t *testing.T) {
	const wire = `{
		"id": "s1",
		"name": "Daily briefing",
		"instructions": "Summarize my calendar and unread email.",
		"enabled": true,
		"schedule": {"repeat": "daily", "time": "08:00"},
		"end_at": null,
		"rev": 1
	}`

	var got Schedule
	if err := json.Unmarshal([]byte(wire), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Kind != "" {
		t.Fatalf("Kind = %q, want empty", got.Kind)
	}
	if ResolveKind(got.Kind) != KindAgent {
		t.Fatalf("a schedule with no kind must resolve to %q", KindAgent)
	}
}

// TestScheduleKindSurvivesStoreRoundTrip checks persistence and omitempty for agent rows.
func TestScheduleKindSurvivesStoreRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "schedules.json")
	store := NewStore(path)

	in := []Schedule{
		{ID: "speak-1", Name: "Nudge", Instructions: "Stretch.", Enabled: true, Kind: KindSpeak,
			Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}},
		{ID: "agent-1", Name: "Briefing", Instructions: "Summarize my day.", Enabled: true,
			Cadence: Spec{Repeat: RepeatDaily, Time: "09:00"}},
	}
	if err := store.Replace(in); err != nil {
		t.Fatalf("Replace: %v", err)
	}

	out, err := store.Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(out) != 2 {
		t.Fatalf("loaded %d schedules, want 2", len(out))
	}
	if out[0].Kind != KindSpeak {
		t.Fatalf("speak schedule reloaded with Kind = %q", out[0].Kind)
	}
	if out[1].Kind != "" {
		t.Fatalf("agent schedule reloaded with Kind = %q, want empty", out[1].Kind)
	}

	raw, err := readFile(path)
	if err != nil {
		t.Fatalf("read schedules.json: %v", err)
	}
	if strings.Count(raw, `"kind"`) != 1 {
		t.Fatalf("expected exactly one \"kind\" key on disk (the speak row), got:\n%s", raw)
	}
}

// TestRunnerFiresSpeakScheduleThroughSpeakNotTheAgent checks speak tasks use Speak only.
func TestRunnerFiresSpeakScheduleThroughSpeakNotTheAgent(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Morning nudge", Instructions: "Good morning. Time to stand up.",
		Enabled: true, Kind: KindSpeak,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.spoken) != 1 || gw.spoken[0] != sch.Instructions {
		t.Fatalf("spoken = %v, want exactly [%q]", gw.spoken, sch.Instructions)
	}
	if len(gw.sent) != 0 {
		t.Fatalf("a speak schedule must not run an agent turn, but sent = %v", gw.sent)
	}
	if len(reports) != 1 {
		t.Fatalf("got %d reports, want 1", len(reports))
	}
	if reports[0].Status != "success" {
		t.Fatalf("status = %q, want success", reports[0].Status)
	}
	if reports[0].RunID != "" {
		t.Fatalf("RunID = %q, want empty for a speak run", reports[0].RunID)
	}
	if reports[0].NextRunAt.IsZero() {
		t.Fatal("a successful speak run must still advance and report next_run_at")
	}
}

// TestRunnerFiresScheduleWithNoKindThroughTheAgent checks legacy rows (empty Kind) use the agent.
func TestRunnerFiresScheduleWithNoKindThroughTheAgent(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "Summarize my calendar.",
		Enabled: true, // Kind deliberately left unset
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 1 || gw.sent[0] != sch.Instructions {
		t.Fatalf("sent = %v, want exactly [%q]", gw.sent, sch.Instructions)
	}
	if len(gw.spoken) != 0 {
		t.Fatalf("a kind-less schedule must never reach TTS, but spoken = %v", gw.spoken)
	}
	if len(reports) != 1 || reports[0].RunID != "ok" {
		t.Fatalf("reports = %+v, want one report carrying the gateway's run id", reports)
	}
}

// TestRunnerFiresUnknownKindThroughTheAgent checks unknown kinds degrade to the agent.
func TestRunnerFiresUnknownKindThroughTheAgent(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Mystery", Instructions: "Do the thing.", Enabled: true,
		Kind:    "some-future-kind",
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want the agent path to have run", gw.sent)
	}
	if len(gw.spoken) != 0 {
		t.Fatalf("spoken = %v, want none", gw.spoken)
	}
}

// TestRunnerSpeakFailureDoesNotBurnTheOccurrence checks a failed speak keeps NextRunAt (I5).
func TestRunnerSpeakFailureDoesNotBurnTheOccurrence(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Morning nudge", Instructions: "Stand up.", Enabled: true, Kind: KindSpeak,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{speakErr: errors.New("hal unreachable")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(scheduledAt.Add(time.Minute))

	if len(reports) != 1 || reports[0].Status != "failure" {
		t.Fatalf("reports = %+v, want a single failure report", reports)
	}
	if reports[0].Summary != "hal unreachable" {
		t.Fatalf("Summary = %q, want the error text", reports[0].Summary)
	}
	stored, ok := store.Get("s1")
	if !ok {
		t.Fatal("schedule vanished from the store")
	}
	if !stored.NextRunAt.Equal(scheduledAt) {
		t.Fatalf("NextRunAt moved to %v; a failed speak must not burn the occurrence (was %v)", stored.NextRunAt, scheduledAt)
	}
}

// TestRunnerDefersSpeakWhileAgentIsBusy checks speak honours single-flight (shared speaker).
func TestRunnerDefersSpeakWhileAgentIsBusy(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Morning nudge", Instructions: "Stand up.", Enabled: true, Kind: KindSpeak,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{busy: true}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.spoken) != 0 {
		t.Fatalf("spoke while the agent was mid-turn: %v", gw.spoken)
	}
	stored, _ := store.Get("s1")
	if !stored.NextRunAt.Equal(scheduledAt) {
		t.Fatal("a deferred speak must stay due, not advance")
	}
}

// TestRunNowRespectsSpeakKind checks RunNow routes speak tasks to Speak.
func TestRunNowRespectsSpeakKind(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Morning nudge", Instructions: "Stand up.", Enabled: true, Kind: KindSpeak,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	if err := store.Replace([]Schedule{sch}); err != nil {
		t.Fatalf("Replace: %v", err)
	}

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	rr, ok := r.RunNow(sch)
	if !ok {
		t.Fatal("RunNow was deferred, want it to run")
	}
	if len(gw.spoken) != 1 || len(gw.sent) != 0 {
		t.Fatalf("spoken = %v, sent = %v; want the speak path only", gw.spoken, gw.sent)
	}
	if rr.Status != "success" || rr.RunID != "" {
		t.Fatalf("report = %+v, want success with an empty run id", rr)
	}
}

// TestValidateIntentPayloadEnforcesSpeakCharacterCap checks the MaxSpeakChars rune cap.
func TestValidateIntentPayloadEnforcesSpeakCharacterCap(t *testing.T) {
	spec := Spec{Repeat: RepeatDaily, Time: "08:00"}

	atCap := &IntentPayload{
		Name: "Nudge", Instructions: strings.Repeat("a", MaxSpeakChars),
		Enabled: true, Kind: KindSpeak, Cadence: spec,
	}
	if err := ValidateIntentPayload(atCap); err != nil {
		t.Fatalf("exactly %d characters must be accepted, got: %v", MaxSpeakChars, err)
	}

	overCap := &IntentPayload{
		Name: "Nudge", Instructions: strings.Repeat("a", MaxSpeakChars+1),
		Enabled: true, Kind: KindSpeak, Cadence: spec,
	}
	if err := ValidateIntentPayload(overCap); err == nil {
		t.Fatalf("%d characters must be rejected for a speak task", MaxSpeakChars+1)
	}

	multiByte := &IntentPayload{
		Name: "Nudge", Instructions: strings.Repeat("é", MaxSpeakChars-1),
		Enabled: true, Kind: KindSpeak, Cadence: spec,
	}
	if err := ValidateIntentPayload(multiByte); err != nil {
		t.Fatalf("%d multi-byte runes are under the cap and must be accepted, got: %v", MaxSpeakChars-1, err)
	}

	agent := &IntentPayload{
		Name: "Briefing", Instructions: strings.Repeat("a", MaxSpeakChars+500),
		Enabled: true, Cadence: spec,
	}
	if err := ValidateIntentPayload(agent); err != nil {
		t.Fatalf("an agent task must not be length-capped, got: %v", err)
	}
}

// readFile returns the file contents as a string.
func readFile(path string) (string, error) {
	b, err := os.ReadFile(path)
	return string(b), err
}
