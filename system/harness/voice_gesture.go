package harness

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"
)

// VoiceGestureError is a stable code for HAL-localized physical-action feedback.
// Transport diagnostics stay in logs; spoken phrases belong to HAL's i18n table.
type VoiceGestureError struct {
	Code  string
	Cause error
}

func (e *VoiceGestureError) Error() string { return e.Code + ": " + e.Cause.Error() }
func (e *VoiceGestureError) Unwrap() error { return e.Cause }
func gestureError(code, message string) error {
	return &VoiceGestureError{Code: code, Cause: errors.New(message)}
}

type voiceGestureResult struct {
	state VoiceModeState
	err   error
}
type voiceGestures struct {
	mu      sync.Mutex
	results map[string]voiceGestureResult
	order   []string
}

// ToggleGesture handles one physical action, not an utterance. A bounded RAM
// cache prevents an HTTP retry from toggling twice. Failed attempts are cached
// too: recovering a connection must never replay an old physical gesture.
func (v *VoiceController) ToggleGesture(ctx context.Context, id string) (VoiceModeState, error) {
	if id == "" || len(id) > 64 {
		return v.State(), gestureError("invalid_request", "Invalid gesture ID")
	}
	if !v.gestures.mu.TryLock() {
		return v.State(), gestureError("busy", "A voice gesture is already being handled")
	}
	defer v.gestures.mu.Unlock()
	if previous, ok := v.gestures.results[id]; ok {
		return previous.state, previous.err
	}
	state, err := v.toggleGesture(ctx)
	if v.gestures.results == nil {
		v.gestures.results = make(map[string]voiceGestureResult)
	}
	v.gestures.results[id] = voiceGestureResult{state: state, err: err}
	v.gestures.order = append(v.gestures.order, id)
	if len(v.gestures.order) > 128 {
		delete(v.gestures.results, v.gestures.order[0])
		v.gestures.order = v.gestures.order[1:]
	}
	return state, err
}

func (v *VoiceController) toggleGesture(ctx context.Context) (VoiceModeState, error) {
	v.mu.Lock()
	intent := v.modeIntent
	if v.state.Enabled {
		v.setModeLocked(false)
		v.mu.Unlock()
		v.NotifyFocusChanged()
		return v.State(), nil
	}
	v.mu.Unlock()
	if err := v.RefreshSupport(ctx); err != nil {
		return v.State(), &VoiceGestureError{Code: "unsupported", Cause: err}
	}
	if err := v.prepareGestureFocus(ctx); err != nil {
		return v.State(), err
	}
	v.mu.Lock()
	// Even an explicit off while already off cancels a delayed enable, without
	// changing the generation or breaking idempotent MQTT set semantics.
	if v.modeIntent != intent || ctx.Err() != nil {
		v.mu.Unlock()
		return v.State(), gestureError("mode_changed", "Voice mode changed while preparing app focus")
	}
	if !v.state.Supported || !v.state.FocusAvailable {
		v.mu.Unlock()
		return v.State(), gestureError("focus_unavailable", "App focus is no longer available")
	}
	v.setModeLocked(true)
	v.mu.Unlock()
	v.NotifyFocusChanged()
	return v.State(), nil
}

func (v *VoiceController) prepareGestureFocus(parent context.Context) error {
	ctx, cancel := context.WithTimeout(parent, 4*time.Second)
	defer cancel()
	status := v.transport.Status()
	if !status.Paired {
		return gestureError("harness_unpaired", "Device is not paired with Harness")
	}
	if !status.Connected {
		return gestureError("harness_offline", "Harness is offline")
	}
	// Refresh first so older CLIs can still activate an existing valid focus.
	_ = v.RefreshFocus(ctx)
	current := v.State()
	if current.FocusAvailable {
		return nil
	}
	if current.AgentID != "" {
		return gestureError("focus_unavailable", "Focus an agent on the paired computer")
	}
	supported := false
	for _, cap := range status.Capabilities {
		if cap == "focus.ensure" {
			supported = true
		}
	}
	if !supported {
		return gestureError("unsupported", "Harness does not support selecting initial app focus")
	}
	frame, err := v.transport.Request(ctx, Frame{"type": "focus.ensure"})
	if err != nil {
		return &VoiceGestureError{Code: "focus_unavailable", Cause: err}
	}
	if err = voiceError(frame); err != nil {
		raw, _ := json.Marshal(frame["error"])
		var remote struct {
			Code string `json:"code"`
		}
		_ = json.Unmarshal(raw, &remote)
		code := "focus_unavailable"
		if remote.Code == "NO_AGENTS" {
			code = "no_agents"
		}
		if remote.Code == "UNSUPPORTED_CAPABILITY" {
			code = "unsupported"
		}
		return &VoiceGestureError{Code: code, Cause: err}
	}
	// Read the app-confirmed authority again, never adopt a locally guessed ID.
	if err = v.RefreshFocus(ctx); err != nil {
		return &VoiceGestureError{Code: "focus_unavailable", Cause: err}
	}
	return nil
}
