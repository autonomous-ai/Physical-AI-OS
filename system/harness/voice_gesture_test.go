package harness

import (
	"context"
	"errors"
	"sync"
	"testing"
)

type gestureTransport struct {
	mu          sync.Mutex
	focus       Frame
	caps        []string
	unpaired    bool
	offline     bool
	ensureCalls int
	ensureHook  func()
	ensureError string
}

func (f *gestureTransport) Status() Status {
	f.mu.Lock()
	defer f.mu.Unlock()
	return Status{Paired: !f.unpaired, Connected: !f.unpaired && !f.offline, MachineID: "computer", Capabilities: f.caps}
}
func (f *gestureTransport) Request(_ context.Context, frame Frame) (Frame, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if frame["type"] == "focus.ensure" {
		f.ensureCalls++
		if f.ensureHook != nil {
			f.ensureHook()
		}
		if f.ensureError != "" {
			return Frame{"error": Frame{"code": f.ensureError}}, nil
		}
		f.focus = Frame{"machineId": "computer", "agentId": "first", "name": "First Agent"}
	}
	return Frame{"focus": f.focus, "focusRevision": "test:1"}, nil
}
func gestureTestController() (*VoiceController, *gestureTransport) {
	f := &gestureTransport{caps: []string{"focus.get", "focus.ensure"}}
	return NewVoiceController(f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }}), f
}
func requireGestureCode(t *testing.T, err error, code string) {
	t.Helper()
	var typed *VoiceGestureError
	if !errors.As(err, &typed) || typed.Code != code {
		t.Fatalf("want %s, got %v", code, err)
	}
}
func TestVoiceGestureSelectsFirstOnceAndDisablesOffline(t *testing.T) {
	v, f := gestureTestController()
	ctx := context.Background()
	on, err := v.ToggleGesture(ctx, "gesture-one")
	if err != nil || !on.Enabled || on.AgentID != "first" || on.AgentName != "First Agent" || f.ensureCalls != 1 {
		t.Fatalf("enable: %+v %v calls %d", on, err, f.ensureCalls)
	}
	duplicate, err := v.ToggleGesture(ctx, "gesture-one")
	if err != nil || !duplicate.Enabled || duplicate.Generation != on.Generation || f.ensureCalls != 1 {
		t.Fatal("duplicate gesture changed mode")
	}
	f.offline = true
	off, err := v.ToggleGesture(ctx, "gesture-two")
	if err != nil || off.Enabled {
		t.Fatalf("offline disable: %+v %v", off, err)
	}
	// A delayed retry of the first request must not re-enable the mode.
	_, _ = v.ToggleGesture(ctx, "gesture-one")
	if v.State().Enabled {
		t.Fatal("old gesture replayed")
	}
}
func TestVoiceGesturePreservesExistingFocus(t *testing.T) {
	v, f := gestureTestController()
	f.caps = []string{"focus.get"}
	f.focus = Frame{"machineId": "computer", "agentId": "chosen", "name": "Chosen"}
	state, err := v.ToggleGesture(context.Background(), "chosen")
	if err != nil || state.AgentID != "chosen" || !state.Enabled || f.ensureCalls != 0 {
		t.Fatalf("existing focus: %+v %v", state, err)
	}
}
func TestVoiceGestureFailuresDoNotEnableOrReplay(t *testing.T) {
	for _, tc := range []struct {
		name, code string
		setup      func(*gestureTransport)
	}{
		{"unpaired", "harness_unpaired", func(f *gestureTransport) { f.unpaired = true }},
		{"offline", "harness_offline", func(f *gestureTransport) { f.offline = true }},
		{"no agents", "no_agents", func(f *gestureTransport) { f.ensureError = "NO_AGENTS" }},
		{"no app", "focus_unavailable", func(f *gestureTransport) { f.ensureError = "FOCUS_UNAVAILABLE" }},
		{"old cli", "unsupported", func(f *gestureTransport) { f.caps = []string{"focus.get"} }},
		{"remote focus", "focus_unavailable", func(f *gestureTransport) { f.focus = Frame{"machineId": "other", "agentId": "remote"} }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v, f := gestureTestController()
			tc.setup(f)
			_, err := v.ToggleGesture(context.Background(), "failed")
			requireGestureCode(t, err, tc.code)
			if v.State().Enabled {
				t.Fatal("failed gesture enabled mode")
			}
			f.unpaired = false
			f.offline = false
			f.ensureError = ""
			f.caps = []string{"focus.get", "focus.ensure"}
			f.focus = nil
			_, err = v.ToggleGesture(context.Background(), "failed")
			requireGestureCode(t, err, tc.code)
			if v.State().Enabled {
				t.Fatal("failed gesture was replayed after recovery")
			}
		})
	}
}
func TestVoiceGestureExplicitOffCancelsInFlightEnable(t *testing.T) {
	v, f := gestureTestController()
	entered, release := make(chan struct{}), make(chan struct{})
	f.ensureHook = func() { close(entered); <-release }
	result := make(chan error, 1)
	go func() { _, err := v.ToggleGesture(context.Background(), "slow"); result <- err }()
	<-entered
	// Same-value off must cancel the pending gesture while keeping generation.
	before := v.State().Generation
	_, _ = v.SetMode(context.Background(), false)
	if v.State().Generation != before {
		t.Fatal("same-value set changed generation")
	}
	_, err := v.ToggleGesture(context.Background(), "busy")
	requireGestureCode(t, err, "busy")
	close(release)
	requireGestureCode(t, <-result, "mode_changed")
	if v.State().Enabled {
		t.Fatal("late app focus enabled mode after explicit off")
	}
}
