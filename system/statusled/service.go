// Package statusled shows prioritized device status states on the LED as transient effects,
// restoring the user's LED state when they clear.
package statusled

import (
	"log/slog"
	"sync"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
)

// State represents a named LED status.
type State string

const (
	StateOTA            State = "ota"             // Firmware updating
	StateError          State = "error"           // System error
	StateBooting        State = "booting"         // Starting up
	StateConnectivity   State = "connectivity"    // No internet connection
	StateWifiConnecting State = "wifi_connecting" // Associating with Wi-Fi during POST /api/device/setup
	StateHALDown        State = "hal_down"        // HAL hardware server unreachable
	StateAgentDown      State = "agent_down"      // OpenClaw agent disconnected
	StateHardware       State = "hardware"        // Hardware component failure (servo/led/audio/voice)
)

// HAL owns each state's look (STATUS_LED_PRESETS); State values match its preset keys.

// priority decides which active state wins (connectivity > error > ota > ... > hardware).
var priority = map[State]int{
	StateHardware:       1,
	StateAgentDown:      2,
	StateHALDown:        3,
	StateBooting:        4,
	StateWifiConnecting: 5,
	StateOTA:            6,
	StateError:          7,
	StateConnectivity:   8,
}

// Service manages status LED states.
type Service struct {
	mu       sync.Mutex
	active   map[State]bool
	hasLight bool // device declares the light capability — else status LED is a no-op
}

// ProvideService creates the service; without an LED it is a no-op. hasLight is resolved
// by the caller to avoid a device <-> statusled import cycle.
func ProvideService(hasLight bool) *Service {
	return &Service{
		active:   make(map[State]bool),
		hasLight: hasLight,
	}
}

// Set activates a status LED state.
func (s *Service) Set(state State) {
	if !s.hasLight {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	s.active[state] = true
	s.applyHighest()
	slog.Info("status LED set", "component", "statusled", "state", state)
}

// Clear deactivates a status LED state. No-op if state wasn't active.
func (s *Service) Clear(state State) {
	if !s.hasLight {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, was := s.active[state]; !was {
		// Already inactive: skip RestoreLED (callers Clear on every tick).
		return
	}
	delete(s.active, state)

	if len(s.active) == 0 {
		hal.RestoreLED()
		slog.Info("status LED cleared", "component", "statusled", "state", state)
		return
	}
	s.applyHighest()
	slog.Info("status LED cleared, showing next", "component", "statusled", "cleared", state)
}

// applyHighest applies the highest-priority active state. Caller must hold s.mu.
func (s *Service) applyHighest() {
	var best State
	bestPri := 0
	for st := range s.active {
		if p := priority[st]; p > bestPri {
			bestPri = p
			best = st
		}
	}
	if best != "" {
		hal.SetStatus(string(best))
	}
}

// FlashReady briefly flashes white; no-op while a status state is active.
func (s *Service) FlashReady() {
	if !s.hasLight {
		return
	}
	s.mu.Lock()
	if len(s.active) > 0 {
		s.mu.Unlock()
		return
	}
	hal.SetStatus("ready_flash")
	s.mu.Unlock()
	slog.Info("status LED ready flash", "component", "statusled")
	go func() {
		time.Sleep(time.Second)
		s.mu.Lock()
		defer s.mu.Unlock()
		if len(s.active) == 0 {
			hal.RestoreLED()
		}
	}()
}
