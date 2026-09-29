// Package healthwatch monitors HAL health and restarts the voice pipeline on ALSA mic
// failures before they escalate into a HAL SIGABRT.
package healthwatch

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

const (
	pollInterval    = 5 * time.Second
	failThreshold   = 2 // consecutive sensing failures before acting
	restartCooldown = 30 * time.Second
)

// Service polls HAL /health and auto-restarts the voice pipeline on sensing failures.
type Service struct {
	bus       *monitor.Bus
	cfg       *config.Config
	statusLED *statusled.Service
}

// ProvideService constructs the health watch Service.
func ProvideService(bus *monitor.Bus, cfg *config.Config, sled *statusled.Service) *Service {
	return &Service{
		bus:       bus,
		cfg:       cfg,
		statusLED: sled,
	}
}

// Start begins the health polling loop. Blocks until ctx is cancelled.
func (s *Service) Start(ctx context.Context) {
	slog.Info("starting health watcher", "component", "healthwatch")

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()

	consecutiveFails := 0
	var lastRestart time.Time
	// Recover only after voice was confirmed running, to avoid false positives at HAL startup.
	voiceWasRunning := false
	// wasUnreachable tracks HAL downtime to announce recovery.
	wasUnreachable := false

	// Only require hardware the device declares (fail-open on nil caps).
	devType := s.cfg.DeviceTypeOrDefault()
	hasMotion := device.Has(devType, device.CapMotion)
	hasLight := device.Has(devType, device.CapLight)
	hasAudio := device.Has(devType, device.CapAudio)

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h, err := hal.GetHealth()
			if err != nil {
				// HAL down is not an ALSA error; leave consecutiveFails alone.
				slog.Debug("HAL unreachable", "component", "healthwatch", "error", err)
				s.statusLED.Set(statusled.StateHALDown)
				wasUnreachable = true
				continue
			}

			if h.Voice && !voiceWasRunning {
				slog.Info("voice pipeline confirmed running", "component", "healthwatch")
				voiceWasRunning = true
			}

			if wasUnreachable {
				s.statusLED.Set(statusled.StateHALDown) // now HAL is up, purple actually shows
				go func() {
					time.Sleep(3 * time.Second)
					s.statusLED.Clear(statusled.StateHALDown)
				}()
			} else {
				s.statusLED.Clear(statusled.StateHALDown)
			}

			// Camera and sensing are excluded (a scene preset may turn them off).
			servoOK := !hasMotion || h.Servo
			if hasMotion {
				if ss, err := hal.GetServoStatus(); err == nil {
					for name, info := range ss.Servos {
						if !info.Online {
							servoOK = false
							slog.Warn("servo offline", "component", "healthwatch", "servo", name, "id", info.ID)
						}
					}
				}
			}
			ledOK := !hasLight || h.LED
			audioOK := !hasAudio || h.Audio
			voiceOK := !hasAudio || h.Voice
			if servoOK && ledOK && audioOK && voiceOK {
				s.statusLED.Clear(statusled.StateHardware)
			} else {
				s.statusLED.Set(statusled.StateHardware)
				slog.Warn("hardware component failure", "component", "healthwatch",
					"servo", servoOK, "led", ledOK, "audio", audioOK, "voice", voiceOK)
			}

			if wasUnreachable && h.Voice && h.TTS {
				wasUnreachable = false
				slog.Info("HAL recovered from downtime, announcing via TTS", "component", "healthwatch")
				go s.speakRecovery()
			} else if wasUnreachable && (h.Voice || h.TTS) {
			} else {
				wasUnreachable = false
			}

			if h.Sensing {
				if consecutiveFails >= failThreshold {
					slog.Info("ALSA/sensing recovered", "component", "healthwatch")
					s.bus.Push(domain.MonitorEvent{
						Type:    "hw_alsa_recover",
						Summary: "ALSA mic stream recovered",
					})
				}
				consecutiveFails = 0
				continue
			}

			// sensing=false before voice ever started is expected; don't interfere.
			if !voiceWasRunning {
				slog.Debug("sensing false but voice never ran — skipping (HAL startup?)", "component", "healthwatch")
				continue
			}

			consecutiveFails++
			slog.Warn("ALSA/sensing degraded",
				"component", "healthwatch",
				"consecutiveFails", consecutiveFails,
				"sensing", h.Sensing,
				"audio", h.Audio,
				"voice", h.Voice,
			)

			if consecutiveFails < failThreshold {
				continue
			}

			s.bus.Push(domain.MonitorEvent{
				Type:    "hw_alsa_error",
				Summary: fmt.Sprintf("ALSA mic stream failing (%d consecutive)", consecutiveFails),
				Detail: map[string]any{
					"sensing": h.Sensing,
					"audio":   h.Audio,
					"voice":   h.Voice,
				},
			})

			// Cooldown to avoid restart storms.
			if time.Since(lastRestart) < restartCooldown {
				slog.Debug("skipping voice restart — within cooldown", "component", "healthwatch")
				continue
			}

			s.restartVoice()
			lastRestart = time.Now()
			voiceWasRunning = false
		}
	}
}

// speakRecovery announces over TTS that the device is back after HAL downtime.
func (s *Service) speakRecovery() {
	phrase := i18n.Pick(i18n.PhraseRecovery)
	if err := hal.SpeakCached(phrase); err != nil {
		slog.Warn("recovery TTS failed", "component", "healthwatch", "error", err)
		return
	}
	slog.Info("recovery TTS sent", "component", "healthwatch")
}

// restartVoice stops and restarts the HAL voice pipeline to clear stuck ALSA state.
// All STT keys are sent so HAL can choose its provider.
func (s *Service) restartVoice() {
	slog.Info("restarting HAL voice pipeline to recover ALSA", "component", "healthwatch")

	_ = hal.StopVoicePipeline()

	time.Sleep(2 * time.Second)

	if err := hal.StartVoice(hal.VoiceStartConfig{
		DeepgramKey: s.cfg.DeepgramAPIKey,
		LLMKey:      s.cfg.LLMAPIKey,
		LLMBaseURL:  s.cfg.LLMBaseURL,
		TTSProvider: s.cfg.TTSProvider,
	}); err != nil {
		slog.Error("voice restart failed", "component", "healthwatch", "error", err)
		s.bus.Push(domain.MonitorEvent{
			Type:    "hw_alsa_restart_failed",
			Summary: "voice pipeline restart failed: " + err.Error(),
		})
		return
	}

	slog.Info("HAL voice pipeline restarted", "component", "healthwatch")
	s.bus.Push(domain.MonitorEvent{
		Type:    "hw_alsa_restarted",
		Summary: "voice pipeline restarted to clear ALSA failure",
	})
}
