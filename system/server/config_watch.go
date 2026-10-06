package server

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/logger"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/server/config"
	_sensingHttpDeliver "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/statusled"
)

// runConfigChangeListener listens for config changes and calls handleSetUpCompleteChange only when SetUpCompleted changed.
func (s *Server) runConfigChangeListener(ctx context.Context) {
	ch := s.config.GetNotifyChannel()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ch:
			hal.SetAPIKey(s.config.LLMAPIKey)
			intent.SetChitchatEnabled(!s.config.RealtimeEnabled())
			s.handleSetUpCompleteChange(s.config.SetUpCompleted)
			s.handleDeviceIDChange(s.config.DeviceID)
			s.handleMQTTConfigChange()
			s.refreshLogRelay()
		}
	}
}

// refreshLogRelay points the log relay at the current device id and key, so a
// first setup ships logs without a restart. Same target is a no-op.
func (s *Server) refreshLogRelay() {
	if s.config.DeviceID != "" {
		logger.SetGELFHost(s.config.DeviceID)
	}
	logger.EnableGELFRelay(s.config.GELFRelayCredentials())
}

// handleDeviceIDChange restarts claude-desktop-buddy when device_id changes.
func (s *Server) handleDeviceIDChange(deviceID string) {
	if s.lastDeviceID == nil {
		s.lastDeviceID = &deviceID
		return
	}
	if *s.lastDeviceID == deviceID {
		return
	}
	prev := *s.lastDeviceID
	s.lastDeviceID = &deviceID

	slog.Info("device_id changed, restarting claude-desktop-buddy", "component", "config", "old", prev, "new", deviceID)
	safego.Go("claude-desktop-buddy-restart", func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()

		// `systemctl cat` exits non-zero when the unit doesn't exist; that's
		// expected on devices without the buddy plugin and we don't want to
		// spam logs there.
		if err := exec.CommandContext(ctx, "systemctl", "cat", "claude-desktop-buddy.service").Run(); err != nil {
			return
		}

		out, err := exec.CommandContext(ctx, "systemctl", "restart", "claude-desktop-buddy").CombinedOutput()
		if err != nil {
			slog.Warn("claude-desktop-buddy restart failed", "component", "config", "error", err, "output", strings.TrimSpace(string(out)))
			return
		}
		slog.Info("claude-desktop-buddy restarted", "component", "config")
	})
}

// handleMQTTConfigChange restarts the MQTT client when any broker-connection
// field (endpoint, port, username, password, fa_channel) changes.
func (s *Server) handleMQTTConfigChange() {
	sig := fmt.Sprintf("%s|%d|%s|%s|%s",
		s.config.MQTTEndpoint, s.config.MQTTPort, s.config.MQTTUsername,
		s.config.MQTTPassword, s.config.FAChannel)
	if s.lastMQTTSig == nil {
		s.lastMQTTSig = &sig
		return
	}
	if *s.lastMQTTSig == sig {
		return
	}
	s.lastMQTTSig = &sig

	slog.Info("mqtt config changed, restarting mqtt client", "component", "config", "endpoint", s.config.MQTTEndpoint)
	s.restartMQTT()
}

// waitAndPaintSetupReady survives slow HAL boots without blocking OS startup.
// Retry until HAL acknowledges the cue, setup completes, or Serve shuts down.
func (s *Server) waitAndPaintSetupReady(ctx context.Context) {
	if !device.Has(s.config.DeviceTypeOrDefault(), device.CapLight) {
		return
	}
	retrySetupLED(ctx, func() bool { return s.config.SetUpCompleted },
		func(ctx context.Context) error { return hal.SetStatusContext(ctx, "setup") }, waitSetupLED)
}

// Callbacks let tests advance a slow boot without wall-clock sleeps or hardware.
func retrySetupLED(ctx context.Context, completed func() bool, paint func(context.Context) error, wait func(context.Context, time.Duration) bool) {
	delay := time.Second
	for ctx.Err() == nil && !completed() {
		if err := paint(ctx); err == nil {
			slog.Info("setup-needed LED acknowledged by HAL", "component", "server")
			return
		} else {
			slog.Debug("setup-needed LED retry", "component", "server", "error", err)
		}
		if ctx.Err() != nil || completed() || !wait(ctx, delay) {
			return
		}
		delay = min(delay*2, 10*time.Second)
	}
}

func waitSetupLED(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// halStartupTimeout bounds waitHALReady.
const halStartupTimeout = 120 * time.Second

// waitHALReady polls HAL /health until it answers, and reports whether it
// did. Only reachability is checked; capability flags vary per device.
func waitHALReady(timeout time.Duration) bool {
	start := time.Now()
	deadline := start.Add(timeout)
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for {
		if _, err := hal.GetHealth(); err == nil {
			slog.Info("hal ready", "component", "server", "waited", time.Since(start).Round(time.Second))
			return true
		}
		if !time.Now().Before(deadline) {
			slog.Warn("hal not ready within timeout, continuing anyway",
				"component", "server", "timeout", timeout)
			return false
		}
		<-ticker.C
	}
}

// handleSetUpCompleteChange starts or stops the network monitor and status
// reporter based on SetUpCompleted.
func (s *Server) handleSetUpCompleteChange(setupCompleted bool) {
	var finishSetup func(error) bool
	var setupCtx context.Context
	if setupCompleted {
		finishSetup, setupCtx = s.deviceService.ClaimSetupRuntime()
	}
	if s.lastSetupCompleted != nil && *s.lastSetupCompleted == setupCompleted && finishSetup == nil {
		return
	}
	if setupCompleted {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
		}
		s.monitorCtx, s.monitorCancel = context.WithCancel(context.Background())
		s.monitorMu.Unlock()

		slog.Info("setup completed, starting internet monitor", "component", "config")
		s.networkService.StartNetworkMonitor(s.monitorCtx,
			func() { s.statusLED.Set(statusled.StateConnectivity) },
			func() { s.statusLED.Clear(statusled.StateConnectivity) },
		)
		slog.Info("setup completed, starting status reporter", "component", "config")
		safego.Go("status-reporter", func() { s.deviceService.StartStatusReporter(s.monitorCtx) })

		safego.Go("oauth-refresh", func() { s.deviceMQTTHandler.StartOAuthRefreshLoop(s.monitorCtx) })

		safego.Go("connector-refresh", func() { s.deviceMQTTHandler.StartConnectorRefreshLoop(s.monitorCtx) })

		// Independent of which agentic runtime is active — it only ever
		// calls AgentGateway.SendSystemChatMessage, so switching runtimes
		// never strands a schedule.
		safego.Go("schedule-runner", func() { s.deviceMQTTHandler.StartScheduleRunnerLoop(s.monitorCtx) })

		s.restartMQTT()

		safego.Go("startup-sequence", func() {
			// Strict readiness applies only to an explicit setup run. Ordinary
			// boots retain their best-effort startup behavior.
			var setupErr error
			if finishSetup != nil {
				defer func() {
					if setupErr == nil {
						setupErr = fmt.Errorf("device preparation did not complete")
					}
					finishSetup(setupErr)
				}()
			}
			s.personaMigration.Reconcile()

			s.configMigration.Reconcile()

			s.channelReconcile.Reconcile()

			s.mcpReconcile.Reconcile()

			s.deviceService.SyncMCPTools()

			s.userReconcile.Reconcile()

			// Quarantine self-written memory that could steer routing (#421),
			// then keep watching every runtime's USER.md / MEMORY.md for the
			// life of this monitor context.
			s.memoryGuard.Run("startup")
			safego.Go("memory-guard-watch", func() { s.memoryGuard.Watch(s.monitorCtx) })

			if err := s.agentGateway.EnsureOnboarding(); err != nil {
				slog.Error("onboarding seed failed", "component", "server", "error", err)
			}

			// Model sync only AFTER onboarding: both write openclaw.json and
			// would clobber each other.
			safego.Go("model-sync", func() { s.agentGateway.StartModelSync(s.monitorCtx) })
			safego.Go("primary-model-watch", func() { s.agentGateway.StartPrimaryModelWatch(s.monitorCtx) })

			// Require a stable ready window so the wake greeting cannot race a
			// Reconcile/EnsureOnboarding gateway restart.
			const startupAgentReadyStability = 15 * time.Second
			gatewayStable := s.deviceService.WaitForAgentReadyStable(120*time.Second, startupAgentReadyStability)
			if gatewayStable {
				slog.Info("agent gateway ready and stable", "component", "server", "stable_for", startupAgentReadyStability)
				if finishSetup == nil {
					s.statusLED.FlashReady()
				}
			} else {
				slog.Warn("agent gateway stable readiness timeout", "component", "server", "stable_for", startupAgentReadyStability)
			}
			if finishSetup != nil && !gatewayStable {
				setupErr = fmt.Errorf("agent did not become ready during setup")
				return
			}
			if setupCtx != nil && setupCtx.Err() != nil {
				return
			}
			// A plain os-server restart with unchanged config leaves the
			// already-running HAL untouched, so we don't needlessly drop the
			// voice pipeline.
			if config.HALConfigChanged() {
				slog.Info("config changed since HAL last started, restarting hal", "component", "server")
				restartCtx := context.Background()
				if setupCtx != nil {
					restartCtx = setupCtx
				}
				if out, err := exec.CommandContext(restartCtx, "systemctl", "restart", "hal").CombinedOutput(); err != nil {
					slog.Warn("hal restart failed", "component", "server", "error", err, "output", string(out))
					if finishSetup != nil {
						setupErr = fmt.Errorf("restart HAL: %w", err)
						return
					}
				} else if err := config.SnapshotHALConfig(); err != nil {
					slog.Warn("hal config snapshot failed", "component", "server", "error", err)
				}
			} else {
				slog.Info("config unchanged since HAL last started, skipping hal restart", "component", "server")
			}

			if ready := waitHALReady(halStartupTimeout); !ready && finishSetup != nil {
				setupErr = fmt.Errorf("HAL did not become ready during setup")
				return
			}

			if s.config.DeepgramAPIKey != "" {
				var voiceErr error
				for attempt := 1; attempt <= 10; attempt++ {
					if setupCtx != nil && setupCtx.Err() != nil {
						return
					}
					err := s.agentGateway.StartHALVoice(s.config.DeepgramAPIKey, s.config.LLMAPIKey, s.config.GetSTTAPIKey(), s.config.GetTTSAPIKey(), s.config.LLMBaseURL, s.config.GetSTTBaseURL(), s.config.GetTTSBaseURL(), s.config.TTSVoice, s.config.TTSInstructions, s.config.TTSProvider)
					voiceErr = err
					if err == nil {
						break
					}
					slog.Warn("start HAL voice failed", "component", "server", "attempt", attempt, "maxAttempts", 10, "error", err)
					time.Sleep(5 * time.Second)
				}
				if finishSetup != nil && voiceErr != nil {
					setupErr = fmt.Errorf("start voice: %w", voiceErr)
					return
				}
			}

			if setupCtx != nil && setupCtx.Err() != nil {
				return
			}
			if device.Has(s.config.DeviceTypeOrDefault(), device.CapAudio) {
				startupVol := device.StartupVolume(s.config.DeviceTypeOrDefault())
				volSrc := "device profile"
				if v, ok := config.PersistedVolume(); ok {
					startupVol, volSrc = v, "persisted"
				}
				if err := s.agentGateway.SetVolume(startupVol); err != nil {
					slog.Warn("init volume failed", "component", "server", "error", err, "volume", startupVol)
				} else {
					slog.Info("init volume", "component", "server", "volume", startupVol, "source", volSrc)
				}
			}

			if finishSetup != nil && !finishSetup(nil) {
				// A timed-out or superseded setup must not greet as successful.
				return
			}

			if startupGreetingAllowed(gatewayStable, device.Has(s.config.DeviceTypeOrDefault(), device.CapExpression), hal.GetSleeping) {
				deviceType := s.config.DeviceTypeOrDefault()
				slog.Info("INBOUND from system → agent (startup greeting)",
					"component", "server", "backend", s.agentGateway.Name(),
					"source", "wake_greeting")
				settings := s.config.EnvironmentSettings()
				if err := sendWakeGreetingWithEnvironment(
					wakeGreetingPrompt(s.agentGateway.Name(), deviceType, device.Capabilities(deviceType)),
					s.environmentStartup, settings.Enabled && settings.InitialReport && s.environmentAvailable(),
					s.agentGateway.SendSystemChatMessage,
				); err != nil {
					slog.Warn("startup greeting failed", "component", "server", "backend", s.agentGateway.Name(), "error", err)
				} else if err := hal.GrantWakeFocus("boot_greeting"); err != nil {
					slog.Warn("boot greeting wake focus failed", "component", "server", "error", err)
				}
			} else {
				if s.environmentStartup != nil {
					s.environmentStartup.FinishGreeting(false)
				}
				slog.Warn("startup greeting skipped: gateway not ready, device sleeping, or sleep state unavailable", "component", "server", "backend", s.agentGateway.Name())
			}

			// Runs in a goroutine because rendering ~17 phrases serially can
			// take 30-60s and must not block the boot greeting.
			safego.Go("prewarm-fillers", func() { _sensingHttpDeliver.PrewarmFillers() })
			safego.Go("ambient", func() { s.ambientService.Start(s.monitorCtx) })
			safego.Go("healthwatch", func() { s.healthWatch.Start(s.monitorCtx) })
		})
	} else {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
			s.monitorCancel = nil
		}
		s.monitorMu.Unlock()
		s.stopMQTT()
		s.networkService.SwitchToAPMode()
	}
	s.lastSetupCompleted = &setupCompleted
}
