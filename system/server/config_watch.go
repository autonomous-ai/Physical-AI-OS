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
			// Refresh the HAL bearer token whenever config changes — covers
			// llm_api_key rotation via PUT /api/device/config without restart.
			hal.SetAPIKey(s.config.LLMAPIKey)
			// Realtime can be toggled from Settings; chitchat follows it.
			intent.SetChitchatEnabled(!s.config.RealtimeEnabled())
			s.handleSetUpCompleteChange(s.config.SetUpCompleted)
			s.handleDeviceIDChange(s.config.DeviceID)
			s.handleMQTTConfigChange()
		}
	}
}

// handleDeviceIDChange restarts claude-desktop-buddy when device_id changes. Buddy's
// BLE name is now derived from the device id (Claude-{MAC}, e.g. Claude-lamp-a1b2) so the
// restart isn't needed for name resolution, but a device_id transition is
// still a useful signal that the device has been re-provisioned — restarting
// buddy clears any stale BLE pairing state from the previous identity.
//
// On the first call (startup bootstrap) we just record the current value
// without restarting — only later transitions trigger a restart.
//
// Best-effort: if claude-desktop-buddy isn't installed (systemctl returns non-zero) we
// log and move on.
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

		// Skip silently if claude-desktop-buddy isn't installed on this Pi. `systemctl cat`
		// exits non-zero when the unit doesn't exist; that's expected on devices
		// without the buddy plugin and we don't want to spam logs there.
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

// handleMQTTConfigChange restarts the MQTT client when ANY broker-connection
// field changes (endpoint, port, username, password, or the subscribed
// fa_channel) — whether pushed by the backend (status-reporter ping response) or
// edited via PUT /api/device/config — so the new config is picked up without a
// full device restart. restartMQTT reconnects and re-subscribes to fa_channel.
//
// On the first call (startup bootstrap) we just record the current signature
// without restarting — handleSetUpCompleteChange already brings MQTT up on the
// initial setup-completed flip, so we only need to act on later changes.
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

// waitAndPaintSetupReady polls HAL /health up to 30s; when LED hardware
// reports ready it paints the strip solid white as the "device awaiting WiFi
// setup" cue. Exits early if setup completes mid-wait so we don't repaint
// over the post-setup user/agent LED state. Best-effort — silent when HAL
// never reports LED ready within budget (logs a warning).
//
// Why this is a poll loop and not a single fire-and-forget call: os-server binds
// :5000 faster than HAL's FastAPI binds :5001 on cold boot, so a fire-
// and-forget paint at L<see Serve> would silently drop on connection refused
// and leave the strip dark — exactly when the user needs the "ready for AP"
// signal most.
func (s *Server) waitAndPaintSetupReady() {
	deadline := time.Now().Add(30 * time.Second)
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for time.Now().Before(deadline) {
		if s.config.SetUpCompleted {
			return
		}
		if h, err := hal.GetHealth(); err == nil && h.LED {
			hal.SetStatus("setup")
			slog.Info("setup-needed white painted", "component", "server")
			return
		}
		<-ticker.C
	}
	slog.Warn("setup-needed paint skipped: hal LED not ready within 30s", "component", "server")
}

// halStartupTimeout bounds waitHALReady. Generous because a first boot pays
// for building the Python venv and loading models before FastAPI binds :5001 —
// the case that matters most, since that is a new owner's very first impression.
const halStartupTimeout = 120 * time.Second

// waitHALReady polls HAL /health until it answers, and reports whether it did.
// os-server reaches its startup tasks long before HAL's FastAPI is listening
// (Python boot is slower), so a request fired straight at :5001 loses to a
// connection refused and — for one-shot calls like the volume init — is simply
// gone. Callers that need HAL up gate on this instead of each growing a retry
// loop of its own.
//
// Only reachability is checked, not per-subsystem readiness: the subsystem
// flags mean different things per device (a device with no LED never reports
// led:true), so gating on them would hang wherever the capability is absent.
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

// handleSetUpCompleteChange starts or stops the network monitor and status reporter based on SetUpCompleted.
// When true: cancels any previous monitor context, creates a new one, starts monitor and reporter, and runs OpenClaw ready check.
// When false: cancels monitor/reporter (they exit on ctx.Done()) and switches to AP mode.
func (s *Server) handleSetUpCompleteChange(setupCompleted bool) {
	if s.lastSetupCompleted != nil && *s.lastSetupCompleted == setupCompleted {
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

		// Keep Google (Workspace) access tokens fresh: they expire after 1 hour
		// and the device holds only the refresh_token, so the actual exchange
		// runs on the backend. Loop refreshes them before they lapse.
		safego.Go("oauth-refresh", func() { s.deviceMQTTHandler.StartOAuthRefreshLoop(s.monitorCtx) })

		// Keep MCP connector tokens (Notion/Figma/Asana/Linear/GitHub) fresh.
		// Same model as oauth-refresh: only entries the backend flagged
		// refresh:true (with a refresh_token) are rotated before they lapse.
		safego.Go("connector-refresh", func() { s.deviceMQTTHandler.StartConnectorRefreshLoop(s.monitorCtx) })

		// Run due "Scheduled" tasks (schedule.sync's list) once a minute.
		// Independent of which agentic runtime is active — it only ever calls
		// AgentGateway.SendSystemChatMessage, so switching runtimes never
		// strands a schedule.
		safego.Go("schedule-runner", func() { s.deviceMQTTHandler.StartScheduleRunnerLoop(s.monitorCtx) })

		s.restartMQTT()

		safego.Go("startup-sequence", func() {
			// Migrate persona/memory if agent runtime switched; non-blocking.
			s.personaMigration.Reconcile()

			// Carry LLM config from the previous runtime's native files to the
			// current one. Runs before EnsureOnboarding so ensureProviderConfig
			// (the fallback) sees the already-migrated values and is a true no-op
			// on a clean switch.
			s.configMigration.Reconcile()

			// Re-apply configured messaging channels to the (possibly new) runtime
			// and record any the runtime can't run. No-op when the runtime is
			// unchanged; gated by config.ChannelsAppliedRuntime. Non-blocking.
			s.channelReconcile.Reconcile()

			// Clone the previous runtime's MCP connectors into the (possibly new)
			// runtime so wired connectors survive a switch. No-op when the runtime is
			// unchanged; gated by config.MCPAppliedRuntime. Non-blocking.
			s.mcpReconcile.Reconcile()

			// Sync user-configured MCP tools (HF Spaces, public endpoints) from
			// config.json into openclaw.json so they survive gateway restarts.
			s.deviceService.SyncMCPTools()

			// Retire people from every runtime's USER.md once their face/voice
			// enrollment is gone. Runs AFTER personaMigration so a freshly
			// migrated profile is reconciled in the same boot rather than a
			// stale name surviving until the next one. Writes only when
			// something is actually stale — USER.md is in the cached prompt
			// prefix, so an unconditional rewrite would cost a cache miss.
			s.userReconcile.Reconcile()

			// Quarantine self-written memory that could steer routing (#421),
			// then keep watching every runtime's USER.md / MEMORY.md for the
			// life of this monitor context. Runs AFTER the retire pass so a
			// file it rewrote is swept in the same boot.
			s.memoryGuard.Run("startup")
			safego.Go("memory-guard-watch", func() { s.memoryGuard.Watch(s.monitorCtx) })

			// Seed SOUL.md + IDENTITY.md into workspace (factory defaults, once only)
			if err := s.agentGateway.EnsureOnboarding(); err != nil {
				slog.Error("onboarding seed failed", "component", "server", "error", err)
			}

			// Start the periodic model sync only AFTER onboarding finishes —
			// both touch openclaw.json (ensureAgentDefaults via os.WriteFile,
			// sync via atomic tmp+rename); running them concurrently would
			// race and could clobber sync's writes.
			safego.Go("model-sync", func() { s.agentGateway.StartModelSync(s.monitorCtx) })
			safego.Go("primary-model-watch", func() { s.agentGateway.StartPrimaryModelWatch(s.monitorCtx) })

			// Reconcile/EnsureOnboarding may restart the gateway after a previous
			// health probe set IsReady=true. Require one full health-poll window of
			// uninterrupted readiness so the wake greeting cannot race that restart.
			const startupAgentReadyStability = 15 * time.Second
			gatewayStable := s.deviceService.WaitForAgentReadyStable(120*time.Second, startupAgentReadyStability)
			if gatewayStable {
				slog.Info("agent gateway ready and stable", "component", "server", "stable_for", startupAgentReadyStability)
				s.statusLED.FlashReady()
			} else {
				slog.Warn("agent gateway stable readiness timeout", "component", "server", "stable_for", startupAgentReadyStability)
			}
			// Restart hal only when the config it reads changed since HAL last
			// started — e.g. fresh setup, an OTA config swap, or an edit while
			// os-server was down. A plain os-server restart with unchanged config
			// leaves the already-running HAL untouched, so we don't needlessly drop
			// the voice pipeline. If HAL is actually down, hal.service Restart=always
			// brings it back independently.
			if config.HALConfigChanged() {
				slog.Info("config changed since HAL last started, restarting hal", "component", "server")
				if out, err := exec.Command("systemctl", "restart", "hal").CombinedOutput(); err != nil {
					slog.Warn("hal restart failed", "component", "server", "error", err, "output", string(out))
				} else if err := config.SnapshotHALConfig(); err != nil {
					slog.Warn("hal config snapshot failed", "component", "server", "error", err)
				}
			} else {
				slog.Info("config unchanged since HAL last started, skipping hal restart", "component", "server")
			}

			// Everything below talks to HAL, so wait for its HTTP port once here
			// rather than making each call site carry its own retry. The restart
			// above is the obvious reason HAL may be down, but it is not the only
			// one (hal.service Restart=always, a slow first boot building the venv
			// and loading models), so we poll even when no restart happened —
			// GetHealth returns immediately against a live HAL.
			waitHALReady(halStartupTimeout)

			// Start voice pipeline on HAL (if Deepgram key configured).
			// Retries cover a rejected key or a HAL that came up unhealthy, not
			// HAL being absent — waitHALReady above handles that.
			if s.config.DeepgramAPIKey != "" {
				for attempt := 1; attempt <= 10; attempt++ {
					err := s.agentGateway.StartHALVoice(s.config.DeepgramAPIKey, s.config.LLMAPIKey, s.config.GetSTTAPIKey(), s.config.GetTTSAPIKey(), s.config.LLMBaseURL, s.config.GetSTTBaseURL(), s.config.GetTTSBaseURL(), s.config.TTSVoice, s.config.TTSInstructions, s.config.TTSProvider)
					if err == nil {
						break
					}
					slog.Warn("start HAL voice failed", "component", "server", "attempt", attempt, "maxAttempts", 10, "error", err)
					time.Sleep(5 * time.Second)
				}
			}

			// Init speaker volume — only for devices that declare the `audio`
			// capability; a device with no speaker has no volume to set. A volume the
			// user last set (persisted by HAL on every /audio/volume change) wins, so
			// the boot level follows their last choice instead of resetting every
			// reboot. Falls back to the device profile (ROBOT.md `startup_volume`,
			// default 100) on first boot — 100 keeps the legacy behavior (software at
			// max, hardware/alsactl is the effective control) while a loud-speaker
			// device can declare a quieter boot level instead of hardcoding it.
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

			// Greet user now that agent + voice pipeline are ready.
			// Prompt is localized by STTLanguage so the very first turn
			// lands in the owner's language without relying on the agent
			// to translate the priming message.
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
					// Greeting invites a reply; open the wake follow-up window so
					// the user need not repeat the wake phrase. HAL no-ops when
					// wake word is off.
					slog.Warn("boot greeting wake focus failed", "component", "server", "error", err)
				}
			} else {
				if s.environmentStartup != nil {
					s.environmentStartup.FinishGreeting(false)
				}
				slog.Warn("startup greeting skipped: gateway not ready, device sleeping, or sleep state unavailable", "component", "server", "backend", s.agentGateway.Name())
			}

			// Prewarm dead-air filler WAV cache so the first filler fire is
			// a cache hit (~50ms) instead of a 1.5s ElevenLabs roundtrip.
			// Runs in a goroutine because rendering ~17 phrases serially can
			// take 30-60s and must not block the boot greeting.
			safego.Go("prewarm-fillers", func() { _sensingHttpDeliver.PrewarmFillers() })
			// Start ambient life behaviors (breathing LED, micro-movements, mumbles)
			safego.Go("ambient", func() { s.ambientService.Start(s.monitorCtx) })
			// Watch HAL component health; auto-restart voice on ALSA failure
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
