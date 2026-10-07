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

func updateWakeThrough(s *Service, entry string, enabled bool) error {
	if entry == "http" {
		return s.UpdateConfig(domain.UpdateConfigRequest{WakeWord: &enabled})
	}
	return s.UpdateWakeWord(enabled)
}

func assertSavedWake(t *testing.T, enabled bool) {
	t.Helper()
	data, err := os.ReadFile("config/config.json")
	if err != nil {
		t.Fatalf("read saved config: %v", err)
	}
	var saved struct {
		WakeWord *bool `json:"wakeword"`
	}
	if err := json.Unmarshal(data, &saved); err != nil {
		t.Fatalf("decode saved config: %v", err)
	}
	if saved.WakeWord == nil || *saved.WakeWord != enabled {
		t.Fatalf("saved wakeword = %v, want %v", saved.WakeWord, enabled)
	}
}

func assertWakeDeadline(t *testing.T, ctx context.Context) {
	t.Helper()
	deadline, ok := ctx.Deadline()
	if !ok || time.Until(deadline) <= 0 || time.Until(deadline) > 30*time.Second {
		t.Errorf("wake restart must have a positive deadline within 30 seconds: %v, %v", deadline, ok)
	}
}

func TestWakeApplyRetriesAcrossEntries(t *testing.T) {
	for _, first := range []string{"mqtt", "http"} {
		for _, retry := range []string{"mqtt", "http"} {
			for _, failure := range []string{"save", "restart", "deadline"} {
				t.Run(first+"_"+failure+"_retry_"+retry, func(t *testing.T) {
					t.Chdir(t.TempDir())
					cfg := baseConfig()
					disabled := false
					cfg.WakeWord = &disabled
					restartErr := errors.New("injected restart failure")
					if failure == "deadline" {
						restartErr = context.DeadlineExceeded
					}
					failRestart := failure != "save"
					restarts := 0
					s := &Service{config: cfg, halRestartCommand: func(ctx context.Context) error {
						restarts++
						assertWakeDeadline(t, ctx)
						assertSavedWake(t, true)
						if failRestart {
							return restartErr
						}
						return nil
					}}
					if failure == "save" {
						if err := os.MkdirAll("config/config.json", 0755); err != nil {
							t.Fatal(err)
						}
					}
					err := updateWakeThrough(s, first, true)
					if err == nil {
						t.Fatal("failed save/restart reported success")
					}
					if failure != "save" && !errors.Is(err, restartErr) {
						t.Fatalf("restart error lost: %v", err)
					}
					if failure == "save" {
						if !strings.Contains(err.Error(), "save config") || restarts != 0 {
							t.Fatalf("save failure = %v, restart calls = %d", err, restarts)
						}
						if err := os.Remove("config/config.json"); err != nil {
							t.Fatal(err)
						}
					}
					failRestart = false
					beforeRetry := restarts
					if err := updateWakeThrough(s, retry, true); err != nil {
						t.Fatalf("same-value retry: %v", err)
					}
					if restarts != beforeRetry+1 {
						t.Fatalf("retry made %d restart calls, want 1", restarts-beforeRetry)
					}
					assertSavedWake(t, true)
					for _, duplicate := range []string{"mqtt", "http"} {
						if err := updateWakeThrough(s, duplicate, true); err != nil {
							t.Fatalf("successful duplicate: %v", err)
						}
					}
					if restarts != beforeRetry+1 {
						t.Fatalf("successful duplicates restarted HAL: %d calls", restarts)
					}
				})
			}
		}
	}
}

func TestWakeApplySerializesOpposingEntries(t *testing.T) {
	for _, first := range []string{"mqtt", "http"} {
		t.Run(first+"_first", func(t *testing.T) {
			t.Chdir(t.TempDir())
			cfg := baseConfig()
			disabled := false
			cfg.WakeWord = &disabled
			started := make(chan bool, 2)
			release := make(chan struct{}, 1)
			var workers sync.WaitGroup
			defer func() {
				close(release)
				done := make(chan struct{})
				go func() { workers.Wait(); close(done) }()
				select {
				case <-done:
				case <-time.After(5 * time.Second):
					t.Error("wake update workers did not stop")
				}
			}()
			s := &Service{config: cfg, halRestartCommand: func(ctx context.Context) error {
				assertWakeDeadline(t, ctx)
				// Inspect the persisted snapshot that this restart will consume.
				data, err := os.ReadFile("config/config.json")
				if err != nil {
					return err
				}
				var saved struct {
					WakeWord bool `json:"wakeword"`
				}
				if err := json.Unmarshal(data, &saved); err != nil {
					return err
				}
				started <- saved.WakeWord
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
			go func() { defer workers.Done(); firstDone <- updateWakeThrough(s, first, true) }()
			select {
			case enabled := <-started:
				if !enabled {
					t.Fatal("first restart did not see enabled wake")
				}
			case <-time.After(5 * time.Second):
				t.Fatal("first restart did not start")
			}
			second := "http"
			if first == "http" {
				second = "mqtt"
			}
			workers.Add(1)
			go func() { defer workers.Done(); secondDone <- updateWakeThrough(s, second, false) }()
			select {
			case <-started:
				t.Fatal("second restart overlapped the first")
			case err := <-secondDone:
				t.Fatalf("opposing request finished before first apply: %v", err)
			case <-time.After(50 * time.Millisecond):
			}
			assertSavedWake(t, true)
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
				if enabled {
					t.Fatal("second restart did not see disabled wake")
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
			assertSavedWake(t, false)
		})
	}
}

func TestWakeApplyMixedHTTPVoiceChangesRestartOnce(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	disabled, enabled := false, true
	cfg.WakeWord = &disabled
	restarts := make(chan struct{}, 4)
	s := &Service{config: cfg, halRestartCommand: func(context.Context) error {
		restarts <- struct{}{}
		return nil
	}}
	if err := s.UpdateConfig(domain.UpdateConfigRequest{
		WakeWord:    &enabled,
		STTLanguage: "vi",
		TTSVoice:    "nova",
		Realtime:    &domain.RealtimeSetData{Enabled: &disabled},
	}); err != nil {
		t.Fatalf("mixed settings update: %v", err)
	}
	select {
	case <-restarts:
	default:
		t.Fatal("mixed settings update did not restart HAL")
	}
	select {
	case <-restarts:
		t.Fatal("mixed wake/voice settings scheduled a duplicate HAL restart")
	case <-time.After(50 * time.Millisecond):
	}
	assertSavedWake(t, true)
	if cfg.STTLanguage != "vi" || cfg.TTSVoice != "nova" {
		t.Fatal("mixed voice fields were not saved")
	}
}
