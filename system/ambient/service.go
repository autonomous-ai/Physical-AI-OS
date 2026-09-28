// Package ambient provides idle "living creature" behaviors: when no
// interaction is happening, it drives a breathing LED, servo micro-movements,
// and occasional self-talk via TTS. The whole suite is opt-in via the
// `lifelike` capability in ROBOT.md; each loop is additionally gated by the
// matching hardware capability. All hardware control goes through HAL HTTP
// API (port 5001).
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

// resumeDelay is how long after the last interaction before ambient resumes.
const resumeDelay = 60 * time.Second

// ambientRestingColor is what the breathing loop falls back to when HAL reports
// a dark strip (just booted, nothing has set a color, or the user turned the
// light off). MUST mirror the HAL-side AMBIENT_RESTING_LED (hal/presets.py) so
// idle and restore show the same look — flip the two together.
//
// BLACK means "resting state is dark": the loop skips the tick entirely instead
// of painting, so ambient never lights an unlit strip. Light becomes opt-in —
// it comes on for an action (emotion, status cue, explicit user/agent color)
// and goes back to black when that action releases the strip.
//
// Currently {0, 0, 0} — default off, per the 30/07/2026 product call: the lamp
// was lighting itself up unasked and shining into users' faces. Restore
// {255, 200, 140} (warm white ~2700K) to bring back the previous behavior,
// where a lamp at rest read as a cozy lamp turned on rather than a cold
// "device booting" blue, and the warm tone stayed clear of every status color
// (orange = no-internet, blue = booting, …).
var ambientRestingColor = [3]int{0, 0, 0}

// Service orchestrates ambient idle behaviors.
type Service struct {
	bus *monitor.Bus
	cfg *config.Config

	mu     sync.Mutex
	paused bool
	// lastInteraction tracks when the last real interaction happened.
	lastInteraction time.Time
	// ledLocked is true when a user or agent explicitly set an LED color/scene.
	// While locked, the breathing loop will not override the LED state.
	// Cleared when user explicitly turns off the LED.
	ledLocked bool
	// sleeping is true when sleepy emotion is active — suppresses all ambient
	// behaviors until a real interaction (chat, sensing, wake word) occurs.
	sleeping bool
}

// ProvideService constructs an AmbientLifeService.
func ProvideService(bus *monitor.Bus, cfg *config.Config) *Service {
	return &Service{
		bus:    bus,
		cfg:    cfg,
		paused: true, // start paused until explicitly started
	}
}

// Start begins the ambient behavior loop. Blocks until ctx is cancelled.
func (s *Service) Start(ctx context.Context) {
	// Master switch: the `lifelike` capability declares that this body opts
	// into the idle living-creature suite at all. A device that declares
	// capabilities without `lifelike` (e.g. intern-v2) stays a quiet tool when
	// idle — no breathing, no micro-movements, no self-talk. Fail-open like
	// every capability gate: nil caps → full legacy Lamp behavior.
	devType := s.cfg.DeviceTypeOrDefault()
	if !device.Has(devType, device.CapLifelike) {
		slog.Info("ambient life disabled — device does not declare the lifelike capability",
			"component", "ambient", "device_type", devType)
		return
	}

	slog.Info("starting ambient life service", "component", "ambient")

	// Subscribe to monitor bus to detect real interactions
	eventCh, unsub := s.bus.Subscribe()
	defer unsub()

	// Watch for interactions in a separate goroutine
	go s.watchInteractions(ctx, eventCh)

	// Initial resume after startup delay
	time.Sleep(5 * time.Second)
	s.resume()

	// Run behavior loops concurrently — but only the ones this device's body
	// can actually perform. Each loop drives one peripheral, so gate it by the
	// matching capability: breathing→light, micro-movement→motion (servo),
	// mumble→audio. A device that lacks a peripheral must not run its loop,
	// else it spams HAL with calls it can't serve.
	// Fail-open (nil caps → all loops run, matching legacy Lamp behavior).
	var wg sync.WaitGroup
	start := func(loop func(context.Context)) {
		wg.Add(1)
		go func() { defer wg.Done(); loop(ctx) }()
	}
	if device.Has(devType, device.CapLight) {
		start(s.breathingLoop)
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

// Consult HAL before ambient output: os-server loses its event-derived sleep
// state on restart, while HAL retains it. An unavailable HAL is not permission
// to move or speak. Do not hold mu across the HTTP request.
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

// LockLED marks the LED as explicitly set by the user/agent so the ambient
// breathing loop won't override it. Same effect as the "led_set" monitor
// event — exposed for the /api/hardware proxy, where web-UI LED writes reach
// HAL directly and never pass through the intent/agent paths that emit the
// event (without this, ambient resumes ~60s after the last interaction and
// tramples a web-set color/effect).
func (s *Service) LockLED() {
	s.mu.Lock()
	s.ledLocked = true
	s.mu.Unlock()
	slog.Debug("LED locked by hardware proxy", "component", "ambient")
}

// UnlockLED clears the lock (web-UI /led/off or /scene/off) so breathing can
// resume on idle. Counterpart of the "led_off" monitor event.
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
			// Interaction types that should pause ambient and wake from sleep
			case "sensing_input", "chat_response", "intent_match", "tts", "chat_send":
				// Passive environmental observations do not prove user activity.
				detail, _ := evt.Detail.(map[string]any)
				if evt.Type == "sensing_input" && detail["type"] == "environment.update" {
					continue
				}
				s.mu.Lock()
				s.sleeping = false
				s.mu.Unlock()
				s.Pause()
			// Emotion fired — check if sleepy to suppress ambient
			case "hw_emotion":
				s.Pause()
				if strings.Contains(evt.Summary, `"sleepy"`) {
					s.mu.Lock()
					s.sleeping = true
					s.mu.Unlock()
					slog.Info("sleep mode activated — ambient suppressed", "component", "ambient")
				}
			// LED explicitly set by user/agent — don't override with breathing
			case "led_set":
				s.mu.Lock()
				s.ledLocked = true
				s.mu.Unlock()
				slog.Debug("LED locked by user/agent", "component", "ambient")
			// LED turned off — unlock so breathing can resume on idle
			case "led_off":
				s.mu.Lock()
				s.ledLocked = false
				s.mu.Unlock()
				slog.Debug("LED unlocked (off)", "component", "ambient")
			}
		case <-ticker.C:
			// Check if enough quiet time has passed to resume
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

// --- Behavior Loops ---

// breathingLoop delegates the breathing LED effect to HAL's built-in
// /led/effect endpoint instead of overriding /led/solid at 5 FPS.
// This way the agent's emotion/scene colors are never trampled by ambient.
func (s *Service) breathingLoop(ctx context.Context) {
	// Track whether we already started the HAL breathing effect
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
			// Respect user/agent LED: don't override with breathing
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
				// Read the current LED color from HAL and start breathing with
				// it; fall back to the resting look when HAL returns black
				// (just started, no color set, or the light is off).
				color := ambientRestingColor
				if c, err := hal.GetColor(); err == nil && (c[0]+c[1]+c[2]) > 0 {
					color = c
				}
				if (color[0] + color[1] + color[2]) == 0 {
					// Nothing lit and the resting look is dark — stay dark.
					continue
				}
				hal.SetEffect("breathing", color[0], color[1], color[2], 0.3)
				running = true
			}
		}
	}
}

// microMovementLoop plays safe, small servo recordings periodically.
// Only triggers servo — does NOT change LED color.
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

// mumbleLoop occasionally makes the device "talk to itself" via TTS.
// Phrase pool lives in lib/i18n (PhraseMumble) so all hardcoded TTS
// templates stay in one place.
func (s *Service) mumbleLoop(ctx context.Context) {
	for {
		delay := 5*60 + rand.Intn(10*60) // 5-15 minutes
		if !sleepCtx(ctx, time.Duration(delay)*time.Second) {
			return
		}
		if s.isPaused() {
			continue
		}

		// SpeakCached: fixed pool, self-caches into hal's WAV cache on first
		// render so replays skip the TTS provider.
		mumble := i18n.Pick(i18n.PhraseMumble)
		if err := hal.SpeakCached(mumble); err != nil {
			slog.Debug("mumble TTS failed", "component", "ambient", "error", err)
		}
		slog.Debug("mumble", "component", "ambient", "text", mumble)
	}
}

// --- Helpers ---

// sleepCtx sleeps for the given duration but returns early if ctx is cancelled.
// Returns false if ctx was cancelled.
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
