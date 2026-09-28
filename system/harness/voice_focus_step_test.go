package harness

import (
	"context"
	"errors"
	"testing"
)

type focusStepTransport struct {
	*voiceFake
	step  func(Frame) (Frame, error)
	steps []Frame
}

func (f *focusStepTransport) Request(ctx context.Context, frame Frame) (Frame, error) {
	if frame["type"] == "focus.step" {
		f.steps = append(f.steps, frame)
		return f.step(frame)
	}
	return f.voiceFake.Request(ctx, frame)
}
func newFocusStepController(t *testing.T) (*VoiceController, *focusStepTransport) {
	t.Helper()
	f := &focusStepTransport{voiceFake: newVoiceFake()}
	f.status.Capabilities = append(f.status.Capabilities, "focus.step")
	f.step = func(Frame) (Frame, error) {
		f.focus = Frame{"machineId": "computer", "agentId": "next-agent", "name": "Next Agent"}
		f.focusRevision = "rev-2"
		return Frame{"focus": f.focus, "focusRevision": f.focusRevision}, nil
	}
	v := NewVoiceController(f, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	if err := v.RefreshFocus(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, _ = v.SetMode(context.Background(), true)
	return v, f
}
func TestFocusStepDirectionsAndStaleGeneration(t *testing.T) {
	for _, direction := range []string{"next", "previous"} {
		t.Run(direction, func(t *testing.T) {
			v, f := newFocusStepController(t)
			generation := v.State().Generation
			state, err := v.StepFocus(context.Background(), "gesture-id", direction, generation)
			if err != nil || state.AgentID != "next-agent" || state.AgentName != "Next Agent" || state.Generation == generation {
				t.Fatalf("state=%+v err=%v", state, err)
			}
			if len(f.steps) != 1 || f.steps[0]["direction"] != direction || f.steps[0]["focusRevision"] != "rev-1" || f.steps[0]["idempotencyKey"] != "gesture-id" {
				t.Fatalf("requests=%v", f.steps)
			}
			_, err = v.StepFocus(context.Background(), "gesture-id", direction, generation)
			if err == nil || len(f.steps) != 1 {
				t.Fatal("stale gesture sent again")
			}
		})
	}
}
func TestFocusStepRejectsBeforeDispatch(t *testing.T) {
	for _, kind := range []string{"unsupported", "disabled", "stale", "offline"} {
		t.Run(kind, func(t *testing.T) {
			v, f := newFocusStepController(t)
			generation := v.State().Generation
			switch kind {
			case "unsupported":
				f.status.Capabilities = []string{"focus.get"}
			case "disabled":
				_, _ = v.SetMode(context.Background(), false)
			case "stale":
				generation--
			case "offline":
				f.status.Connected = false
			}
			_, err := v.StepFocus(context.Background(), "gesture-id", "next", generation)
			if err == nil || len(f.steps) != 0 {
				t.Fatalf("err=%v requests=%v", err, f.steps)
			}
		})
	}
}
func TestFocusStepModeOffDuringRPC(t *testing.T) {
	v, f := newFocusStepController(t)
	f.step = func(Frame) (Frame, error) {
		_, _ = v.SetMode(context.Background(), false)
		f.focusRevision = "rev-2"
		return Frame{"focus": f.focus, "focusRevision": f.focusRevision}, nil
	}
	state, err := v.StepFocus(context.Background(), "gesture-id", "next", v.State().Generation)
	var gesture *VoiceGestureError
	if !errors.As(err, &gesture) || gesture.Code != "mode_changed" || state.Enabled {
		t.Fatalf("state=%+v err=%v", state, err)
	}
}
func TestFocusStepUnknownDeliveryDoesNotRetry(t *testing.T) {
	v, f := newFocusStepController(t)
	f.step = func(Frame) (Frame, error) { return nil, &DeliveryUnknownError{Cause: errors.New("timeout")} }
	_, err := v.StepFocus(context.Background(), "gesture-id", "next", v.State().Generation)
	var gesture *VoiceGestureError
	if !errors.As(err, &gesture) || gesture.Code != "delivery_unknown" || len(f.steps) != 1 {
		t.Fatalf("requests=%v err=%v", f.steps, err)
	}
}
func TestFocusStepSucceedsWhenRefreshLagsBehindCommittedSwitch(t *testing.T) {
	v, f := newFocusStepController(t)
	// The app switched and said so in the reply, but focus.get is still stale
	// (or failing) right after the step. The gesture must not report failure.
	f.step = func(Frame) (Frame, error) {
		f.focusRevision = ""
		return Frame{"focus": Frame{"machineId": "computer", "agentId": "lagging-agent", "name": "Lagging"}, "focusRevision": "rev-9"}, nil
	}
	state, err := v.StepFocus(context.Background(), "gesture-id", "next", v.State().Generation)
	if err != nil || !state.FocusAvailable || state.AgentID != "lagging-agent" || state.FocusRevision != "rev-9" {
		t.Fatalf("state=%+v err=%v", state, err)
	}
	// A reply without focus still never fails once the step was accepted.
	v, f = newFocusStepController(t)
	f.step = func(Frame) (Frame, error) { f.focusRevision = ""; return Frame{}, nil }
	state, err = v.StepFocus(context.Background(), "gesture-id", "next", v.State().Generation)
	if err != nil || state.FocusAvailable {
		t.Fatalf("state=%+v err=%v", state, err)
	}
}
