package harness

import (
	"context"
	"errors"
	"regexp"
	"sync"
	"testing"
)

type voiceFake struct {
	mu               sync.Mutex
	status           Status
	frames           []Frame
	question         any
	mutationError    error
	receiptState     string
	beforeMutation   func()
	beforeStatus     func()
	focus            any
	focusRevision    string
	mutationResponse Frame
}

func (f *voiceFake) Status() Status { f.mu.Lock(); defer f.mu.Unlock(); return f.status }
func (f *voiceFake) Request(_ context.Context, r Frame) (Frame, error) {
	f.mu.Lock()
	f.frames = append(f.frames, r)
	f.mu.Unlock()
	switch r["type"] {
	case "focus.get":
		return Frame{"focus": f.focus, "focusRevision": f.focusRevision}, nil
	case "status":
		if f.beforeStatus != nil {
			f.beforeStatus()
		}
		return Frame{"openQuestion": f.question}, nil
	case "receipt.get":
		return Frame{"receipt": map[string]any{"state": f.receiptState}}, nil
	case "turn.send", "question.answer":
		// Match the owner CLI contract, not just the local Go transport.
		if key, _ := r["idempotencyKey"].(string); !regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`).MatchString(key) {
			return Frame{"error": map[string]any{"code": "INVALID_REQUEST", "message": "Invalid idempotencyKey"}}, nil
		}
		if f.beforeMutation != nil {
			f.beforeMutation()
		}
		if f.mutationResponse != nil {
			return f.mutationResponse, nil
		}
		if f.mutationError != nil {
			return nil, f.mutationError
		}
		return Frame{"receipt": map[string]any{"state": f.receiptState}}, nil
	}
	return Frame{}, nil
}
func newVoiceFake() *voiceFake {
	return &voiceFake{status: Status{Paired: true, Connected: true, MachineID: "computer", Capabilities: []string{"focus.get"}}, receiptState: "queued", focusRevision: "rev-1", focus: Frame{"machineId": "computer", "agentId": "agent", "name": "Agent"}}
}
func enableVoice(t *testing.T, f *voiceFake, cb VoiceCallbacks) *VoiceController {
	t.Helper()
	cb.SupportsMode = func(context.Context) (bool, error) { return true, nil }
	v := NewVoiceController(f, cb)
	if e := v.RefreshFocus(context.Background()); e != nil {
		t.Fatal(e)
	}
	if _, e := v.SetMode(context.Background(), true); e != nil {
		t.Fatal(e)
	}
	f.frames = nil
	return v
}
func mutationCount(f *voiceFake) int {
	n := 0
	for _, r := range f.frames {
		if mutation(stringField(r, "type")) {
			n++
		}
	}
	return n
}
func questionSet(id string, keys ...string) any {
	rows := []VoiceQuestion{}
	for _, key := range keys {
		rows = append(rows, VoiceQuestion{Key: key, Q: key, Options: []string{"A", "B"}})
	}
	return voiceQuestionSet{RequestID: id, Questions: rows}
}
func TestVoiceDefaultRestartAndStale(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := NewVoiceController(f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	old := v.State()
	if old.Enabled {
		t.Fatal("enabled by default")
	}
	if e := v.Submit(ctx, "hello", "off", old.Generation); e == nil {
		t.Fatal("off accepted")
	}
	if len(f.frames) != 0 {
		t.Fatal("off sent")
	}
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	s, e := v.SetMode(ctx, true)
	if e != nil {
		t.Fatal(e)
	}
	if e = v.Submit(ctx, "hello", "stale", old.Generation); e == nil {
		t.Fatal("stale accepted")
	}
	if mutationCount(f) != 0 {
		t.Fatal("stale mutation")
	}
	restart := NewVoiceController(f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }}).State()
	if restart.Enabled || restart.Generation == s.Generation {
		t.Fatalf("bad restart: %+v", restart)
	}
}
func TestVoiceCallbackBeforeMutationAndDuplicate(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	registered := false
	f.beforeMutation = func() {
		if !registered {
			t.Error("mutation before callback")
		}
	}
	v := enableVoice(t, f, VoiceCallbacks{OnDispatch: func(a, r string) { registered = a == "agent" && r == "run" }})
	for i := 0; i < 2; i++ {
		if e := v.Submit(ctx, "hello", "run", v.State().Generation); e != nil {
			t.Fatal(e)
		}
	}
	if mutationCount(f) != 1 {
		t.Fatal("duplicate mutation")
	}
}
func TestVoiceUnknownReceiptAllowsNewInputWithoutRetry(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.mutationError = &DeliveryUnknownError{Cause: errors.New("timeout")}
	if err := v.Submit(ctx, "first", "first", v.State().Generation); err == nil {
		t.Fatal("unknown accepted")
	}
	original := v.State().Pending
	if original == nil {
		t.Fatal("missing pending")
	}
	f.mutationError = nil
	f.receiptState = "queued"
	if err := v.Submit(ctx, "second", "second", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	if err := v.Submit(ctx, "first", "first", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	if mutationCount(f) != 2 {
		t.Fatal("old turn retried or new turn lost")
	}
	for _, frame := range f.frames {
		if frame["type"] == "receipt.get" {
			t.Fatal("new input waited for old receipt")
		}
	}
	last := f.frames[len(f.frames)-1]
	if last["text"] != "second" || last["idempotencyKey"] == original.IdempotencyKey {
		t.Fatal("new input reused old mutation")
	}
}

func TestVoiceStartedTurnAllowsFollowupWithDistinctDeliveryCorrelation(t *testing.T) {
	f := newVoiceFake()
	f.receiptState = "started"
	keys := map[string]string{}
	registered := 0
	f.beforeMutation = func() {
		if registered != mutationCount(f) {
			t.Error("input reached transport before its correlation was registered")
		}
	}
	v := enableVoice(t, f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil },
		OnDispatch: func(_, _ string) { t.Error("legacy callback also invoked") },
		OnDispatchRequest: func(agentID, runID string, frame Frame) {
			registered++
			key, _ := frame["idempotencyKey"].(string)
			if key == "" || agentID != "agent" || frame["agentId"] != agentID || frame["machineId"] != "computer" {
				t.Fatalf("incomplete dispatch correlation: agent=%s run=%s frame=%v", agentID, runID, frame)
			}
			keys[runID] = key
		},
	})
	// No terminal event occurs between these inputs: the first task is running.
	for _, runID := range []string{"first", "second"} {
		if err := v.Submit(context.Background(), runID, runID, v.State().Generation); err != nil {
			t.Fatal(err)
		}
	}
	if mutationCount(f) != 2 || len(keys) != 2 || keys["first"] == keys["second"] {
		t.Fatalf("followup lost or delivery key reused: mutations=%d keys=%v", mutationCount(f), keys)
	}
	if err := v.Submit(context.Background(), "second", "second", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	if mutationCount(f) != 2 || registered != 2 {
		t.Fatal("duplicate input re-registered or resent")
	}
}

func TestVoiceResolveDoesNotRetryAndOffPreservesPending(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.receiptState = "unknown"
	_ = v.Submit(ctx, "first", "first", v.State().Generation)
	p := v.State().Pending
	if p == nil {
		t.Fatal("missing pending")
	}
	_, _ = v.SetMode(ctx, false)
	if v.State().Pending == nil {
		t.Fatal("off discarded pending")
	}
	if e := v.Resolve("do_not_retry", "wrong"); e == nil {
		t.Fatal("wrong key accepted")
	}
	if e := v.Resolve("do_not_retry", p.IdempotencyKey); e != nil {
		t.Fatal(e)
	}
	if v.State().Pending != nil || mutationCount(f) != 1 {
		t.Fatal("resolve retried")
	}
}
func TestVoiceCollectQuestionsAndExactKeys(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	var prompt string
	v := enableVoice(t, f, VoiceCallbacks{OnResponse: func(_, _, s string) { prompt = s }})
	f.question = questionSet("q1", "first key", "second key")
	if e := v.Submit(ctx, "A", "one", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	if prompt == "" || mutationCount(f) != 0 {
		t.Fatal("did not collect")
	}
	if e := v.Submit(ctx, "free text", "two", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	last := f.frames[len(f.frames)-1]
	answers := last["answers"].(map[string]string)
	if last["type"] != "question.answer" || last["questionRequestId"] != "q1" || answers["first key"] != "A" || answers["second key"] != "free text" {
		t.Fatalf("bad answers: %#v", last)
	}
}
func TestVoiceFocusChangeDuringQuestionLookupRejectsPartialAnswer(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	responses := 0
	v := enableVoice(t, f, VoiceCallbacks{OnResponse: func(_, _, _ string) { responses++ }})
	f.question = questionSet("old-question", "first", "second")
	f.beforeStatus = func() {
		f.focus = Frame{"machineId": "computer", "agentId": "next"}
		f.focusRevision = "rev-2"
	}
	if err := v.Submit(ctx, "A", "stale-partial", v.State().Generation); err == nil {
		t.Fatal("focus changed during status but partial answer accepted")
	}
	if responses != 0 || mutationCount(f) != 0 {
		t.Fatalf("stale question produced response or mutation: responses=%d mutations=%d", responses, mutationCount(f))
	}
	if v.question != nil || len(v.answers) != 0 {
		t.Fatalf("stale partial answer retained: question=%+v answers=%+v", v.question, v.answers)
	}
	// A fresh capture starts collecting the new agent's question from its first key.
	f.beforeStatus = nil
	f.question = questionSet("new-question", "first", "second")
	if err := v.Submit(ctx, "B", "fresh-partial", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	if responses != 1 || mutationCount(f) != 0 || v.answers["first"] != "B" || len(v.answers) != 1 {
		t.Fatalf("new question did not restart collection: responses=%d answers=%+v", responses, v.answers)
	}
}

func TestVoiceQuestionReplacementDoesNotConsumeAnswer(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.question = questionSet("q1", "first", "second")
	if e := v.Submit(ctx, "A", "one", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	f.question = questionSet("q2", "replacement")
	if e := v.Submit(ctx, "B", "two", v.State().Generation); e == nil {
		t.Fatal("replaced question accepted")
	}
	if mutationCount(f) != 0 {
		t.Fatal("replacement answered")
	}
	if e := v.Answer(ctx, "q1", map[string]string{"first": "A"}, "ui-stale", v.State().FocusRevision); e == nil {
		t.Fatal("stale UI answer")
	}
	if e := v.Answer(ctx, "q2", map[string]string{"wrong": "A"}, "ui-keys", v.State().FocusRevision); e == nil {
		t.Fatal("wrong keys accepted")
	}
}
func TestVoiceOfflineAndMachineChange(t *testing.T) {
	for _, offline := range []bool{true, false} {
		f := newVoiceFake()
		v := enableVoice(t, f, VoiceCallbacks{})
		if offline {
			f.status.Connected = false
		} else {
			f.status.MachineID = "other"
		}
		if e := v.Submit(context.Background(), "hello", "run", v.State().Generation); e == nil {
			t.Fatal("unavailable accepted")
		}
		if mutationCount(f) != 0 {
			t.Fatal("unavailable sent")
		}
	}
}

func TestVoiceFocusSyncWhileOffKeepsCaptureGeneration(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := NewVoiceController(f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	generation := v.State().Generation
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	state := v.State()
	if state.Enabled || state.AgentID != "agent" || state.AgentName != "Agent" || !state.FocusAvailable || state.Generation != generation {
		t.Fatalf("off mirror: %+v", state)
	}
	f.focus = Frame{"machineId": "computer", "agentId": "next"}
	f.focusRevision = "rev-2"
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	if state = v.State(); state.AgentID != "next" || state.Generation != generation {
		t.Fatalf("off focus invalidated normal capture: %+v", state)
	}
	f.frames = nil
	if _, err := v.SetMode(ctx, true); err != nil {
		t.Fatal(err)
	}
	if len(f.frames) != 0 {
		t.Fatal("toggle made a network request")
	}
	f.receiptState = "unknown"
	err := v.Submit(ctx, "hello", "run", v.State().Generation)
	var unknown *DeliveryUnknownError
	if !errors.As(err, &unknown) {
		t.Fatalf("unknown result type: %v", err)
	}
}

func TestVoiceEnabledFocusChangeInvalidatesCapture(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	original := v.State()
	f.focus = Frame{"machineId": "computer", "agentId": "next"}
	f.focusRevision = "rev-2"
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	if v.State().Generation == original.Generation {
		t.Fatal("focus change retained generation")
	}
	if err := v.Submit(ctx, "hello", "stale", original.Generation); err == nil {
		t.Fatal("stale capture accepted")
	}
	if mutationCount(f) != 0 {
		t.Fatal("stale capture sent")
	}
	if err := v.Submit(ctx, "repeat", "fresh", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	last := f.frames[len(f.frames)-1]
	if last["agentId"] != "next" || last["focusRevision"] != "rev-2" {
		t.Fatalf("wrong target: %#v", last)
	}
}

func TestVoiceUnavailableFocusNeverMutates(t *testing.T) {
	for _, variant := range []string{"none", "remote", "old-cli", "offline", "missing-revision"} {
		t.Run(variant, func(t *testing.T) {
			f := newVoiceFake()
			v := enableVoice(t, f, VoiceCallbacks{})
			switch variant {
			case "none":
				f.focus = nil
			case "remote":
				f.focus = Frame{"machineId": "remote", "agentId": "agent"}
			case "old-cli":
				f.status.Capabilities = nil
			case "offline":
				f.status.Connected = false
			case "missing-revision":
				f.focusRevision = ""
			}
			if err := v.Submit(context.Background(), "hello", "run", v.State().Generation); err == nil {
				t.Fatal("unavailable focus accepted")
			}
			if mutationCount(f) != 0 || v.State().FocusAvailable {
				t.Fatalf("unavailable focus sent: %+v", v.State())
			}
		})
	}
}

func TestVoiceAnswerRejectsStaleFocusRevision(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.question = questionSet("same-question", "key")
	old := v.State().FocusRevision
	f.focusRevision = "rev-2"
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	for _, revision := range []string{"", old} {
		if err := v.Answer(ctx, "same-question", map[string]string{"key": "A"}, "stale-"+revision, revision); err == nil {
			t.Fatal("stale revision accepted")
		}
	}
	if mutationCount(f) != 0 {
		t.Fatal("stale answer sent")
	}
	if err := v.Answer(ctx, "same-question", map[string]string{"key": "A"}, "fresh", v.State().FocusRevision); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceFocusChangedResponseIsKnownRejection(t *testing.T) {
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.mutationResponse = Frame{"error": Frame{"code": "FOCUS_CHANGED", "message": "Focus changed"}}
	err := v.Submit(context.Background(), "hello", "run", v.State().Generation)
	var unknown *DeliveryUnknownError
	if err == nil || errors.As(err, &unknown) || v.State().Pending != nil {
		t.Fatalf("focus rejection unresolved: %v %+v", err, v.State())
	}
}

func TestVoiceNewInputAfterFocusChangeDoesNotResendPending(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.receiptState = "unknown"
	if err := v.Submit(ctx, "hello", "original", v.State().Generation); err == nil {
		t.Fatal("unknown accepted")
	}
	original := *v.State().Pending
	f.focus = Frame{"machineId": "computer", "agentId": "next"}
	f.focusRevision = "rev-2"
	if err := v.RefreshFocus(ctx); err != nil {
		t.Fatal(err)
	}
	if state := v.State(); state.AgentID != "next" || state.Pending == nil || *state.Pending != original {
		t.Fatalf("pending retargeted: %+v", state)
	}
	f.receiptState = "queued"
	if err := v.Submit(ctx, "another", "new-turn", v.State().Generation); err != nil {
		t.Fatal(err)
	}
	if mutationCount(f) != 2 {
		t.Fatal("new input not sent")
	}
	last := f.frames[len(f.frames)-1]
	if last["type"] != "turn.send" || last["agentId"] != "next" || last["idempotencyKey"] == original.IdempotencyKey {
		t.Fatalf("old delivery resent or wrong target: %#v", last)
	}

}

func TestVoiceConcurrentDuplicateAndDisable(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	entered := make(chan struct{})
	release := make(chan struct{})
	f.beforeMutation = func() { close(entered); <-release }
	v := enableVoice(t, f, VoiceCallbacks{})
	s := v.State()
	done := make(chan error, 1)
	go func() { done <- v.Submit(ctx, "hello", "run", s.Generation) }()
	<-entered
	duplicate := make(chan error, 1)
	go func() { duplicate <- v.Submit(ctx, "hello", "run", s.Generation) }()
	off, e := v.SetMode(ctx, false)
	if e != nil || off.Enabled {
		t.Fatal("disable blocked")
	}
	close(release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	if err := <-duplicate; err != nil {
		t.Fatal(err)
	}
	if mutationCount(f) != 1 {
		t.Fatal("duplicate sent")
	}
}

func TestVoiceRejectedReceiptReturnsKnownError(t *testing.T) {
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.receiptState = "rejected"
	e := v.Submit(context.Background(), "hello", "run", v.State().Generation)
	var unknown *DeliveryUnknownError
	if e == nil || errors.As(e, &unknown) || v.State().Pending != nil {
		t.Fatalf("rejection not resolved: %v %+v", e, v.State())
	}
}

func TestVoiceCanceledAdmissionCanRetryAndSecondInputDoesNotWaitForCompletion(t *testing.T) {
	f := newVoiceFake()
	f.receiptState = "started"
	entered, release := make(chan struct{}), make(chan struct{})
	first := true
	f.beforeMutation = func() {
		if first {
			first = false
			close(entered)
			<-release
		}
	}
	v := enableVoice(t, f, VoiceCallbacks{})
	generation := v.State().Generation
	done := make(chan error, 1)
	go func() { done <- v.Submit(context.Background(), "A", "A", generation) }()
	<-entered
	ctx, cancel := context.WithCancel(context.Background())
	canceled := make(chan error, 1)
	go func() { canceled <- v.Submit(ctx, "B", "B", generation) }()
	cancel()
	if err := <-canceled; !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation: %v", err)
	}
	second := make(chan error, 1)
	go func() { second <- v.Submit(context.Background(), "B", "B", generation) }()
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := <-second; err != nil {
		t.Fatal(err)
	}
	if mutationCount(f) != 2 {
		t.Fatal("second input not sent")
	}
}

func TestVoiceUnknownDeliveriesRemainIndividuallyReconcilable(t *testing.T) {
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	ctx := context.Background()
	generation := v.State().Generation
	f.receiptState = "unknown"
	if err := v.Submit(ctx, "A", "A", generation); err == nil {
		t.Fatal("expected uncertain A")
	}
	a := *v.State().Pending
	f.receiptState = "started"
	if err := v.Submit(ctx, "B", "B", generation); err != nil {
		t.Fatal(err)
	}
	if p := v.State().Pending; p == nil || *p != a {
		t.Fatal("known B erased A")
	}
	f.receiptState = "unknown"
	if err := v.Submit(ctx, "C", "C", generation); err == nil {
		t.Fatal("expected uncertain C")
	}
	if p := v.State().Pending; p == nil || *p != a {
		t.Fatal("unknown C replaced A")
	}
	f.receiptState = "completed"
	if _, err := v.Receipt(ctx); err != nil {
		t.Fatal(err)
	}
	c := v.State().Pending
	if c == nil || c.RunID != "C" {
		t.Fatalf("C not retained: %+v", c)
	}
	if err := v.Resolve("do_not_retry", a.IdempotencyKey); err == nil {
		t.Fatal("stale resolution accepted")
	}
	if err := v.Resolve("do_not_retry", c.IdempotencyKey); err != nil {
		t.Fatal(err)
	}
	if v.State().Pending != nil || mutationCount(f) != 3 {
		t.Fatal("reconciliation resent or retained input")
	}
}
func TestVoicePermissionDialogIsNeverAnswered(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.question = map[string]any{"requestId": "q-perm", "questions": []any{map[string]any{"key": "Run printf hi?", "q": "Run printf hi?", "options": []any{"Yes", "No"}, "multi": false}},
		"permission": map[string]any{"dialog": "Run printf hi?", "resolution": "desktop"}}
	if e := v.Submit(ctx, "yes", "perm-run", v.State().Generation); !errors.Is(e, ErrPermissionInDesktop) {
		t.Fatalf("submit error = %v", e)
	}
	if e := v.Answer(ctx, "q-perm", map[string]string{"Run printf hi?": "Yes"}, "perm-answer", v.State().FocusRevision); !errors.Is(e, ErrPermissionInDesktop) {
		t.Fatalf("answer error = %v", e)
	}
	if mutationCount(f) != 0 {
		t.Fatal("permission dialog answered")
	}
}
