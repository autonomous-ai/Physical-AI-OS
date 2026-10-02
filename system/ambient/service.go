// Package ambient drives idle "living creature" behaviors (resting LED, servo
// micro-movements, self-talk), opt-in via the `lifelike` capability.
package ambient

import (
	"context"
	"log/slog"
	"math/rand"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// resumeDelay is the quiet time before ambient resumes.
const resumeDelay = 60 * time.Second

// Retained for reference; resting colors now come from device presets in HAL.
/*
// ambientRestingColor is the breathing fallback for a dark strip; must mirror HAL's
// AMBIENT_RESTING_LED. Black means ambient never lights an unlit strip.
var ambientRestingColor = [3]int{0, 0, 0}
*/

// Service orchestrates ambient idle behaviors.
type Service struct {
	bus *monitor.Bus
	cfg *config.Config

	mu     sync.Mutex
	paused bool
	// lastInteraction is the time of the last real interaction.
	lastInteraction time.Time
	// ledLocked is true while a user/agent-set LED must not be overridden.
	ledLocked bool
	// sleeping suppresses ambient behaviors until a real interaction.
	sleeping bool
}

// ProvideService constructs the ambient Service.
func ProvideService(bus *monitor.Bus, cfg *config.Config) *Service {
	return &Service{
		bus:    bus,
		cfg:    cfg,
		paused: true, // start paused until explicitly started
	}
}

// Start begins the ambient behavior loop. Blocks until ctx is cancelled.
func (s *Service) Start(ctx context.Context) {
	// `lifelike` is the master switch; fail-open when caps are nil.
	devType := s.cfg.DeviceTypeOrDefault()
	if !device.Has(devType, device.CapLifelike) {
		slog.Info("ambient life disabled — device does not declare the lifelike capability",
			"component", "ambient", "device_type", devType)
		return
	}

	slog.Info("starting ambient life service", "component", "ambient")

	eventCh, unsub := s.bus.Subscribe()
	defer unsub()

	go s.watchInteractions(ctx, eventCh)

	time.Sleep(5 * time.Second)
	s.resume()

	// Each loop runs only if the body has its peripheral (light, motion, audio).
	var wg sync.WaitGroup
	start := func(loop func(context.Context)) {
		wg.Add(1)
		go func() { defer wg.Done(); loop(ctx) }()
	}
	if device.Has(devType, device.CapLight) {
		// start(s.breathingLoop) // Disabled: HAL owns the device resting preset.
		start(s.restingLEDLoop)
	}
	if device.Has(devType, device.CapMotion) {
		start(s.microMovementLoop)
	}
	if device.Has(devType, device.CapAudio) {
		start(s.mumbleLoop)
	}

	<-ctx.Done()
	wg.Wait()
	slog.Info("stopped", "component", "ambient")
}

// Pause stops ambient behaviors (called when real interaction begins).
func (s *Service) Pause() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.paused {
		s.paused = true
		flow.Log("ambient_pause", nil)
	}
	s.lastInteraction = time.Now()
}

func (s *Service) resume() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.paused {
		s.paused = false
		flow.Log("ambient_resume", nil)
	}
}

func (s *Service) isPaused() bool {
	return s.isPausedWithSleep(hal.GetSleeping)
}

// isPausedWithSleep asks HAL for sleep state (it survives os-server restarts);
// an unavailable HAL counts as paused. Do not hold mu across the request.
func (s *Service) isPausedWithSleep(getSleeping func() (bool, error)) bool {
	s.mu.Lock()
	paused := s.paused || s.sleeping
	s.mu.Unlock()
	if paused {
		return true
	}
	if !device.Has(s.cfg.DeviceTypeOrDefault(), device.CapExpression) {
		return false
	}
	sleeping, err := getSleeping()
	return err != nil || sleeping
}

// LockLED stops ambient restore from overriding a web-UI LED write (like "led_set").
func (s *Service) LockLED() {
	s.mu.Lock()
	s.ledLocked = true
	s.mu.Unlock()
	slog.Debug("LED locked by hardware proxy", "component", "ambient")
}

// UnlockLED clears the lock so ambient restore can resume (like "led_off").
func (s *Service) UnlockLED() {
	s.mu.Lock()
	s.ledLocked = false
	s.mu.Unlock()
	slog.Debug("LED unlocked by hardware proxy", "component", "ambient")
}

// watchInteractions monitors the event bus and pauses/resumes accordingly.
func (s *Service) watchInteractions(ctx context.Context, eventCh <-chan domain.MonitorEvent) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case evt := <-eventCh:
			switch evt.Type {
			case "sensing_input", "chat_response", "intent_match", "tts", "chat_send":
				detail, _ := evt.Detail.(map[string]any)
				if evt.Type == "sensing_input" && detail["type"] == "environment.update" {
					continue
				}
				s.mu.Lock()
				s.sleeping = false
				s.mu.Unlock()
				s.Pause()
			case "hw_emotion":
				s.Pause()
				if strings.Contains(evt.Summary, `"sleepy"`) {
					s.mu.Lock()
					s.sleeping = true
					s.mu.Unlock()
					slog.Info("sleep mode activated — ambient suppressed", "component", "ambient")
				}
			case "led_set":
				s.mu.Lock()
				s.ledLocked = true
				s.mu.Unlock()
				slog.Debug("LED locked by user/agent", "component", "ambient")
			case "led_off":
				s.mu.Lock()
				s.ledLocked = false
				s.mu.Unlock()
				slog.Debug("LED unlocked (off)", "component", "ambient")
			}
		case <-ticker.C:
			s.mu.Lock()
			shouldResume := s.paused && !s.sleeping && !s.lastInteraction.IsZero() &&
				time.Since(s.lastInteraction) > resumeDelay
			s.mu.Unlock()
			if shouldResume {
				s.resume()
			}
		}
	}
}

// Disabled: the old breathing loop could override the device resting look.
// Keep it here for reference; Start now runs restingLEDLoop instead.
/*
// breathingLoop uses HAL's /led/effect so agent colors are never trampled.
func (s *Service) breathingLoop(ctx context.Context) {
	running := false

	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			if running {
				hal.StopEffect()
			}
			return
		case <-ticker.C:
			if s.isPaused() {
				if running {
					hal.StopEffect()
					running = false
				}
				continue
			}
			s.mu.Lock()
			locked := s.ledLocked
			s.mu.Unlock()
			if locked {
				if running {
					hal.StopEffect()
					running = false
				}
				continue
			}
			if !running {
				// Breathe the current HAL color, else the resting look.
				color := ambientRestingColor
				if c, err := hal.GetColor(); err == nil && (c[0]+c[1]+c[2]) > 0 {
					color = c
				}
				if (color[0] + color[1] + color[2]) == 0 {
					continue
				}
				hal.SetEffect("breathing", color[0], color[1], color[2], 0.3)
				running = true
			}
		}
	}
}
*/

// restingLEDLoop asks HAL to restore its resting look or saved user preference.
// Never paint a fallback here: HAL owns explicit off, overlays and the default.
func (s *Service) restingLEDLoop(ctx context.Context) {
	applied := false
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if s.isPaused() {
				applied = false
				continue
			}
			s.mu.Lock()
			locked := s.ledLocked
			s.mu.Unlock()
			if locked {
				applied = false
				continue
			}
			if !applied {
				hal.RestoreLED()
				applied = true
			}
		}
	}
}

// microMovementLoop periodically plays small servo recordings (servo only).
func (s *Service) microMovementLoop(ctx context.Context) {
	safeRecordings := []string{"idle", "curious", "nod"}

	for {
		delay := 45 + rand.Intn(75) // 45-120 seconds
		if !sleepCtx(ctx, time.Duration(delay)*time.Second) {
			return
		}
		if s.isPaused() {
			continue
		}

		recording := safeRecordings[rand.Intn(len(safeRecordings))]
		if err := hal.PlayServo(recording); err != nil {
			slog.Debug("micro-movement servo failed", "component", "ambient", "error", err)
		}
		slog.Debug("micro-movement", "component", "ambient", "recording", recording)
	}
}

// mumbleLoop occasionally speaks a phrase from i18n.PhraseMumble.
func (s *Service) mumbleLoop(ctx context.Context) {
	for {
		delay := 5*60 + rand.Intn(10*60) // 5-15 minutes
		if !sleepCtx(ctx, time.Duration(delay)*time.Second) {
			return
		}
		if s.isPaused() {
			continue
		}

		mumble := i18n.Pick(i18n.PhraseMumble)
		if err := hal.SpeakCached(mumble); err != nil {
			slog.Debug("mumble TTS failed", "component", "ambient", "error", err)
		}
		slog.Debug("mumble", "component", "ambient", "text", mumble)
	}
}

// sleepCtx sleeps for d; returns false if ctx was cancelled.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
