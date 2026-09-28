package harness

import (
	"context"
	"errors"
	"testing"
)

func TestVoiceModeSupportGate(t *testing.T) {
	for _, tc := range []struct {
		name  string
		check func(context.Context) (bool, error)
	}{
		{"missing", nil},
		{"disabled", func(context.Context) (bool, error) { return false, nil }},
		{"unavailable", func(context.Context) (bool, error) { return true, errors.New("HAL unavailable") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := NewVoiceController(nil, VoiceCallbacks{SupportsMode: tc.check})
			if s, err := v.SetMode(context.Background(), true); err == nil || s.Enabled || s.Supported {
				t.Fatalf("enabled without support: %+v %v", s, err)
			}
			if s, err := v.ToggleGesture(context.Background(), "gesture"); err == nil || s.Enabled {
				t.Fatalf("gesture bypassed gate: %+v %v", s, err)
			}
			if _, err := v.SetMode(context.Background(), false); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestVoiceModeSupportReadPreservesModeAndConcurrentDisable(t *testing.T) {
	supported := true
	v := NewVoiceController(nil, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return supported, nil }})
	if _, err := v.SetMode(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	supported = false
	if err := v.RefreshSupport(context.Background()); err == nil || !v.State().Enabled {
		t.Fatal("support read changed active mode")
	}
	v.callbacks.SupportsMode = func(context.Context) (bool, error) { return false, errors.New("HAL unavailable") }
	if err := v.RefreshSupport(context.Background()); err == nil || !v.State().Enabled {
		t.Fatal("HAL failure changed active mode")
	}
	entered, release := make(chan struct{}), make(chan struct{})
	v.callbacks.SupportsMode = func(context.Context) (bool, error) { close(entered); <-release; return true, nil }
	done := make(chan error, 1)
	go func() { _, err := v.SetMode(context.Background(), true); done <- err }()
	<-entered
	// State and disable must remain available while HAL is slow.
	_ = v.State()
	if _, err := v.SetMode(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err == nil || v.State().Enabled {
		t.Fatal("delayed support check overrode explicit disable")
	}
}

func TestVoiceModeSupportReadCachesDeclaration(t *testing.T) {
	calls := 0
	v := NewVoiceController(nil, VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) {
		calls++
		if calls == 1 {
			return false, errors.New("HAL starting")
		}
		return true, nil
	}})
	if v.SupportState(context.Background()).Supported {
		t.Fatal("unavailable HAL authorized mode")
	}
	if !v.SupportState(context.Background()).Supported {
		t.Fatal("read did not recover")
	}
	_ = v.SupportState(context.Background())
	if calls != 2 {
		t.Fatalf("cached read queried HAL: %d", calls)
	}
	if _, err := v.SetMode(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatal("enable did not check HAL again")
	}
}
