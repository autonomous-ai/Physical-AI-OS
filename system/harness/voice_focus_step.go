package harness

import (
	"context"
	"errors"
	"time"
)

// StepFocus requests an app-owned adjacent selection exactly once. The CLI must
// negotiate focus.step and enforce the revision and idempotency key itself.
func (v *VoiceController) StepFocus(ctx context.Context, id, direction string, generation uint64) (VoiceModeState, error) {
	if id == "" || len(id) > 64 || (direction != "next" && direction != "previous") {
		return v.State(), gestureError("invalid_request", "Invalid focus gesture")
	}
	if !v.op.TryLock() {
		return v.State(), gestureError("busy", "A voice operation is already being handled")
	}
	defer v.op.Unlock()
	v.mu.Lock()
	snapshot, intent := v.state, v.modeIntent
	v.mu.Unlock()
	if !snapshot.Enabled || snapshot.Generation != generation {
		return v.State(), gestureError("mode_changed", "Voice mode or app focus changed")
	}
	status := v.transport.Status()
	if !status.Paired {
		return v.State(), gestureError("harness_unpaired", "Device is not paired with Harness")
	}
	if !status.Connected {
		return v.State(), gestureError("harness_offline", "Harness is offline")
	}
	supported := false
	for _, capability := range status.Capabilities {
		if capability == "focus.step" {
			supported = true
		}
	}
	if !supported {
		return v.State(), gestureError("unsupported", "Harness does not support switching app focus")
	}
	if snapshot.FocusRevision == "" {
		return v.State(), gestureError("focus_unavailable", "App focus revision is unavailable")
	}
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	reply, err := v.request(ctx, Frame{
		"type": "focus.step", "idempotencyKey": id,
		"direction": direction, "focusRevision": snapshot.FocusRevision,
	})
	if err != nil {
		code := "focus_unavailable"
		var unknown *DeliveryUnknownError
		if errors.As(err, &unknown) {
			code = "delivery_unknown"
		}
		return v.State(), &VoiceGestureError{Code: code, Cause: err}
	}
	// Once the app has switched, never report the gesture as failed (HAL would announce a
	// failure for a switch that happened); a reply without usable focus refreshes via polling.
	if !v.applyFocusReply(reply, status.MachineID) {
		_ = v.RefreshFocus(ctx)
	}
	v.mu.Lock()
	changed := v.modeIntent != intent || !v.state.Enabled
	v.mu.Unlock()
	if changed {
		return v.State(), gestureError("mode_changed", "Voice mode changed while switching app focus")
	}
	return v.State(), nil
}

// applyFocusReply stores the focus a focus.step reply carries, when it names an
// agent on the paired computer. Returns false when the reply has no usable focus.
func (v *VoiceController) applyFocusReply(reply Frame, machineID string) bool {
	focus := payloadOf(Frame{"payload": reply["focus"]})
	revision := stringField(reply, "focusRevision")
	machine, agent, name := stringField(focus, "machineId"), stringField(focus, "agentId"), stringField(focus, "name")
	if revision == "" || agent == "" || machine != machineID {
		return false
	}
	v.setFocus(machine, agent, name, revision, true, nil)
	return true
}
