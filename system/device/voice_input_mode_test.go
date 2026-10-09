package device

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

func updateInputModeThrough(s *Service, entry string, mode string) error {
	if entry == "http" {
		return s.UpdateConfig(domain.UpdateConfigRequest{VoiceInputMode: &mode})
	}
	return s.UpdateVoiceInputMode(mode)
}

func assertSavedInputMode(t *testing.T, mode string) {
	t.Helper()
	data, err := os.ReadFile("config/config.json")
	if err != nil {
		t.Fatalf("read saved config: %v", err)
	}
	var saved struct {
		VoiceInputMode string `json:"voice_input_mode"`
	}
	if err := json.Unmarshal(data, &saved); err != nil {
		t.Fatalf("decode saved config: %v", err)
	}
	if saved.VoiceInputMode != mode {
		t.Fatalf("saved voice input mode = %v, want %v", saved.VoiceInputMode, mode)
	}
}

func TestInputModeApplyRetriesAcrossEntries(t *testing.T) {
	for _, first := range []string{"mqtt", "http"} {
		for _, retry := range []string{"mqtt", "http"} {
			for _, failure := range []string{"save", "apply", "deadline"} {
				t.Run(first+"_"+failure+"_retry_"+retry, func(t *testing.T) {
					t.Chdir(t.TempDir())
					cfg := baseConfig()
					disabled := false
					cfg.WakeWord = &disabled
					applyErr := errors.New("injected apply failure")
					if failure == "deadline" {
						applyErr = context.DeadlineExceeded
					}
					failApply := failure != "save"
					applys := 0
					s := &Service{config: cfg, halRestartCommand: func(context.Context) error { return errors.New("unexpected HAL apply for mode-only update") }, halInputModeApply: func(ctx context.Context, mode string, wake bool) error {
						applys++
						assertWakeDeadline(t, ctx)
						assertSavedInputMode(t, "tap_to_talk")
						if failApply {
							return applyErr
						}
						return nil
					}}
					if failure == "save" {
						if err := os.MkdirAll("config/config.json", 0755); err != nil {
							t.Fatal(err)
						}
					}
					err := updateInputModeThrough(s, first, "tap_to_talk")
					if err == nil {
						t.Fatal("failed save/apply reported success")
					}
					if failure != "save" && !errors.Is(err, applyErr) {
						t.Fatalf("apply error lost: %v", err)
					}
					if failure == "save" {
						if !strings.Contains(err.Error(), "save config") || applys != 0 {
							t.Fatalf("save failure = %v, apply calls = %d", err, applys)
						}
						if err := os.Remove("config/config.json"); err != nil {
							t.Fatal(err)
						}
					}
					failApply = false
					beforeRetry := applys
					if err := updateInputModeThrough(s, retry, "tap_to_talk"); err != nil {
						t.Fatalf("same-value retry: %v", err)
					}
					if applys != beforeRetry+1 {
						t.Fatalf("retry made %d apply calls, want 1", applys-beforeRetry)
					}
					assertSavedInputMode(t, "tap_to_talk")
					for _, duplicate := range []string{"mqtt", "http"} {
						if err := updateInputModeThrough(s, duplicate, "tap_to_talk"); err != nil {
							t.Fatalf("successful duplicate: %v", err)
						}
					}
					if applys != beforeRetry+1 {
						t.Fatalf("successful duplicates applyed HAL: %d calls", applys)
					}
				})
			}
		}
	}
}

func TestInputModeInvalidDoesNotMutate(t *testing.T) {
	for _, mode := range []string{"", "manual", "AUTOMATIC"} {
		cfg := baseConfig()
		s := &Service{config: cfg}
		if err := s.UpdateConfig(domain.UpdateConfigRequest{VoiceInputMode: &mode, TTSVoice: "must-not-save"}); err == nil {
			t.Fatal("invalid mode accepted")
		}
		if cfg.VoiceInputMode != "" || cfg.TTSVoice == "must-not-save" {
			t.Fatal("invalid request mutated config")
		}
	}
}

func TestInputModeMixedUpdatePreservesWakeAndRestartsOnce(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	enabled := true
	cfg.WakeWord = &enabled
	restarts := 0
	applies := 0
	s := &Service{config: cfg, halRestartCommand: func(ctx context.Context) error { restarts++; return nil }, halInputModeApply: func(ctx context.Context, mode string, wake bool) error { applies++; return nil }}
	mode := "tap_to_talk"
	if err := s.UpdateConfig(domain.UpdateConfigRequest{VoiceInputMode: &mode, WakeWord: &enabled, STTLanguage: "vi", TTSVoice: "nova"}); err != nil {
		t.Fatal(err)
	}
	if restarts != 1 || !cfg.WakeWordEnabled() {
		t.Fatalf("restarts=%d wake=%v", restarts, cfg.WakeWordEnabled())
	}
	if err := s.UpdateVoiceInputMode("automatic"); err != nil {
		t.Fatal(err)
	}
	if restarts != 1 || applies != 1 || !cfg.WakeWordEnabled() {
		t.Fatal("switching back lost saved wake setting")
	}
}

func TestInputModeApplySerializesOpposingEntries(t *testing.T) {
	for _, first := range []string{"mqtt", "http"} {
		t.Run(first+"_first", func(t *testing.T) {
			t.Chdir(t.TempDir())
			cfg := baseConfig()
			disabled := false
			cfg.WakeWord = &disabled
			started := make(chan string, 2)
			release := make(chan struct{}, 1)
			var workers sync.WaitGroup
			defer func() {
				close(release)
				done := make(chan struct{})
				go func() { workers.Wait(); close(done) }()
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Error("input mode update workers did not stop")
				}
			}()
			s := &Service{config: cfg, halRestartCommand: func(context.Context) error { return errors.New("unexpected HAL restart for mode-only update") }, halInputModeApply: func(ctx context.Context, mode string, wake bool) error {
				assertWakeDeadline(t, ctx)
				// Inspect the persisted snapshot that this restart will consume.
				data, err := os.ReadFile("config/config.json")
				if err != nil {
					return err
				}
				var saved struct {
					VoiceInputMode string `json:"voice_input_mode"`
				}
				if err := json.Unmarshal(data, &saved); err != nil {
					return err
				}
				started <- saved.VoiceInputMode
				select {
				case <-release:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			}}
			firstDone := make(chan error, 1)
			secondDone := make(chan error, 1)
			workers.Add(1)
			go func() { defer workers.Done(); firstDone <- updateInputModeThrough(s, first, "tap_to_talk") }()
			select {
			case enabled := <-started:
				if enabled != "tap_to_talk" {
					t.Fatal("first restart did not see tap-to-talk mode")
				}
			case <-time.After(5 * time.Second):
				t.Fatal("first restart did not start")
			}
			second := "http"
			if first == "http" {
				second = "mqtt"
			}
			workers.Add(1)
			go func() { defer workers.Done(); secondDone <- updateInputModeThrough(s, second, "automatic") }()
			select {
			case <-started:
				t.Fatal("second restart overlapped the first")
			case err := <-secondDone:
				t.Fatalf("opposing request finished before first apply: %v", err)
			case <-time.After(50 * time.Millisecond):
			}
			assertSavedInputMode(t, "tap_to_talk")
			release <- struct{}{}
			select {
			case err := <-firstDone:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("first update did not complete")
			}
			select {
			case enabled := <-started:
				if enabled != "automatic" {
					t.Fatal("second restart did not see automatic mode")
				}
			case <-time.After(5 * time.Second):
				t.Fatal("second restart did not start")
			}
			release <- struct{}{}
			select {
			case err := <-secondDone:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("second update did not complete")
			}
			assertSavedInputMode(t, "automatic")
		})
	}
}

func TestInputModeToggleSerializesWithExplicitUpdate(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	started := make(chan string, 2)
	release := make(chan struct{})
	s := &Service{config: cfg, halRestartCommand: func(context.Context) error { return errors.New("unexpected HAL restart for mode-only update") }, halInputModeApply: func(ctx context.Context, mode string, wake bool) error {
		started <- mode
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}
	explicit := make(chan error, 1)
	go func() { explicit <- s.UpdateVoiceInputMode("tap_to_talk") }()
	if mode := <-started; mode != "tap_to_talk" {
		t.Fatal(mode)
	}
	toggled := make(chan error, 1)
	go func() {
		mode, err := s.ToggleVoiceInputMode()
		if err == nil && mode != "automatic" {
			err = errors.New("toggle used stale mode")
		}
		toggled <- err
	}()
	select {
	case mode := <-started:
		t.Fatalf("overlapping apply: %s", mode)
	case <-time.After(50 * time.Millisecond):
	}
	close(release)
	if err := <-explicit; err != nil {
		t.Fatal(err)
	}
	if err := <-toggled; err != nil {
		t.Fatal(err)
	}
	if mode := <-started; mode != "automatic" {
		t.Fatal(mode)
	}
	assertSavedInputMode(t, "automatic")
}
