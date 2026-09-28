package server

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/runtimes/claudecode"
	"go.autonomous.ai/os/system/agent"
	"go.autonomous.ai/os/system/ambient"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/environment"
	"go.autonomous.ai/os/system/externalhistory"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/healthwatch"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/lib/logger"
	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/network"
	_agentHttpDeliver "go.autonomous.ai/os/system/server/agent/delivery/http"
	_buddyHttpDeliver "go.autonomous.ai/os/system/server/buddy/delivery/http"
	"go.autonomous.ai/os/system/server/config"
	_deviceHttpDeliver "go.autonomous.ai/os/system/server/device/delivery/http"
	_deviceMQTTDeliver "go.autonomous.ai/os/system/server/device/delivery/mqtt"
	_healthHttpDeliver "go.autonomous.ai/os/system/server/health/delivery/http"
	_networkHttpDeliver "go.autonomous.ai/os/system/server/network/delivery/http"
	_pluginHttpDeliver "go.autonomous.ai/os/system/server/plugin/delivery/http"
	_sensingHttpDeliver "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/server/serializers"
	systemshell "go.autonomous.ai/os/system/server/system"
	_telemetryHttpDeliver "go.autonomous.ai/os/system/server/telemetry/delivery/http"
	"go.autonomous.ai/os/system/statusled"
	"go.autonomous.ai/os/system/telemetry"
)

type Server struct {
	externalHistory *externalhistory.Store

	harnessPreparationMu    sync.Mutex
	harnessPreparationWaits map[string]*harnessPreparationWait
	harnessService          *harness.Service
	harnessResults          *harness.ResultStore
	harnessResultsMu        sync.Mutex
	harnessResultWake       chan struct{}
	harnessResultsPublished map[string]bool
	harnessSelector         *harnessSelector
	harnessVoice            *harness.VoiceController
	harnessVoiceCtx         context.Context
	harnessRepliesMu        sync.Mutex
	// harnessReplies is keyed by the local device run ID. A single Harness
	// agent can work on more than one user request at once, so it cannot be
	// keyed by agent ID.
	harnessReplies       map[string]harnessReply
	harnessOverlapAgents map[string]bool
	harnessFollowup      atomic.Int64
	harnessResultMu      sync.RWMutex
	harnessResult        string
	harnessResultAt      time.Time
	engine               *gin.Engine
	config               *config.Config

	environmentStartup *environment.StartupCoordinator

	// handlers
	healthHandler     _healthHttpDeliver.HealthHandler
	networkHandler    _networkHttpDeliver.NetworkHandler
	deviceHandler     _deviceHttpDeliver.DeviceHandler
	deviceMQTTHandler _deviceMQTTDeliver.DeviceMQTTHandler
	agentHandler      *_agentHttpDeliver.AgentHandler
	sensingHandler    *_sensingHttpDeliver.SensingHandler
	buddyHandler      _buddyHttpDeliver.BuddyHandler
	pluginHandler     _pluginHttpDeliver.PluginHandler

	agentGateway     domain.AgentGateway
	chatStream       *_deviceMQTTDeliver.ChatStream
	personaMigration *agent.PersonaMigration
	configMigration  *agent.ConfigMigration
	channelReconcile *agent.ChannelReconcile
	mcpReconcile     *agent.MCPReconcile
	userReconcile    *agent.UserProfileReconcile
	memoryGuard      *agent.MemoryGuard
	networkService   *network.Service
	deviceService    *device.Service
	ambientService   *ambient.Service
	healthWatch      *healthwatch.Service
	statusLED        *statusled.Service

	// mqttFactory is the optional MQTT factory (nil when broker not configured).
	mqttFactory *mqtt.Factory
	// mqttClient is the active MQTT client when setup is complete; guarded by mqttMu.
	mqttClient *mqtt.MQTT
	mqttCancel context.CancelFunc
	mqttMu     sync.Mutex

	// monitorCtx: context for network monitor + status reporter. Created when SetUpCompleted true, cancelled when false or on shutdown.
	monitorCtx context.Context
	// monitorCancel cancels monitorCtx.
	monitorCancel context.CancelFunc
	// monitorMu guards monitorCtx and monitorCancel.
	monitorMu sync.Mutex
	// lastSetupCompleted is the last SetUpCompleted value we acted on. Used to avoid redundant handleSetUpCompleteChanged when config notifies but value unchanged.
	lastSetupCompleted *bool
	// lastDeviceID is the last DeviceID value we acted on. When this changes (typically empty → assigned at first /device/setup), we restart claude-desktop-buddy so its BLE name picks up the new device_id.
	lastDeviceID *string
	// lastMQTTSig is the last MQTT-connection signature we acted on (endpoint +
	// port + username + password + fa_channel). When any of these change — via a
	// status-reporter ping response OR a PUT /api/device/config edit — we restart
	// the MQTT client so it reconnects/resubscribes with the new broker config,
	// without requiring a full device restart.
	lastMQTTSig *string
}

// Engine ...
func (s *Server) Engine() *gin.Engine {
	return s.engine
}

// shellAgentEnvFile resolves, per web-CLI connection, the launch env file to
// source into the PTY so an interactive `claude` reuses the campaign key. Only
// claudecode needs it (its .env is otherwise service-scoped); other runtimes
// return "" (no injection). Resolved lazily so a runtime switch is picked up
// without a restart.
func (s *Server) shellAgentEnvFile() string {
	if device.CurrentAgentRuntimeFromConfig(s.config) == domain.AgentRuntimeClaudeCode {
		return claudecode.EnvFile
	}
	return ""
}

// GetContext ...
func (s *Server) GetContext(c *gin.Context) context.Context {
	ctx := c.Request.Context()
	if ctx == nil {
		ctx = context.Background()
	}

	return ctx
}

func ProvideServer(
	cfg *config.Config,
	hh _healthHttpDeliver.HealthHandler,
	nh _networkHttpDeliver.NetworkHandler,
	dh _deviceHttpDeliver.DeviceHandler,
	dqth _deviceMQTTDeliver.DeviceMQTTHandler,
	agentH *_agentHttpDeliver.AgentHandler,
	sensingH *_sensingHttpDeliver.SensingHandler,
	buddyH _buddyHttpDeliver.BuddyHandler,
	pluginH _pluginHttpDeliver.PluginHandler,
	ds *device.Service,
	agentGW domain.AgentGateway,
	pm *agent.PersonaMigration,
	cm *agent.ConfigMigration,
	cr *agent.ChannelReconcile,
	mr *agent.MCPReconcile,
	upr *agent.UserProfileReconcile,
	mg *agent.MemoryGuard,
	ns *network.Service,
	mqttFactory *mqtt.Factory,
	ambientSvc *ambient.Service,
	hw *healthwatch.Service,
	sled *statusled.Service,
	chatStream *_deviceMQTTDeliver.ChatStream,
) *Server {
	// A realtime-handled turn means the user has already been answered out
	// loud about something newer than whatever the main agent is still working
	// on — that older turn keeps running but loses the speaker.
	sensingH.SetOnRealtimeHandled(agentH.CancelSpeechForNewerTurn)
	s := &Server{
		environmentStartup: environment.NewStartupCoordinator(),

		config:            cfg,
		healthHandler:     hh,
		networkHandler:    nh,
		deviceHandler:     dh,
		deviceMQTTHandler: dqth,
		agentHandler:      agentH,
		sensingHandler:    sensingH,
		buddyHandler:      buddyH,
		pluginHandler:     pluginH,
		agentGateway:      agentGW,
		personaMigration:  pm,
		configMigration:   cm,
		channelReconcile:  cr,
		mcpReconcile:      mr,
		userReconcile:     upr,
		memoryGuard:       mg,
		networkService:    ns,
		deviceService:     ds,
		mqttFactory:       mqttFactory,
		ambientService:    ambientSvc,
		healthWatch:       hw,
		statusLED:         sled,
		chatStream:        chatStream,
	}
	harnessConnected := func() bool {
		if s.harnessService == nil {
			return false
		}
		status := s.harnessService.Status()
		return status.Paired && status.Connected
	}
	sensingH.SetHarnessConnected(harnessConnected)
	sensingmsg.SetHarnessConnected(harnessConnected)
	sensingH.SetHarnessFollowup(s.HarnessVoiceFollowup)
	sensingH.SetHarnessTaskPending(s.HarnessTaskPending)
	sensingH.SetHarnessFollowupContext(s.HarnessFollowupContext)
	sensingH.SetHarnessVoice(s.handleHarnessVoice)
	return s
}

func (s *Server) Serve(closeFn func()) error {
	// Device type is mandatory — refuse to boot rather than silently assume a
	// "lamp" (wrong soul/hardware/OTA). Mirrors the fail-loud provisioning layer.
	deviceType := s.config.DeviceTypeOrDefault()
	if deviceType == "" {
		log.Fatal("[config] device_type unresolved — set DEVICE_TYPE env (provisioning) or config.json device_type; refusing to assume 'lamp'")
	}
	// Persist the resolved class so config.json actually carries device_type, the
	// key HAL and software-update read. Provisioning only writes the DEVICE_TYPE
	// env, so without this seed the key never exists on a provisioned device and
	// every config.json reader silently falls back (HAL's wake words resolved to
	// "friend", making the device-type wake phrases the web UI advertises dead).
	// Idempotent — only the first start after upgrade writes.
	if s.config.DeviceType != deviceType {
		s.config.DeviceType = deviceType
		if err := s.config.Save(); err != nil {
			slog.Error("seed device_type failed", "component", "config", "error", err)
		}
	}

	// Set GELF host to device_id + stamp device class for centralized logging
	if s.config.DeviceID != "" {
		logger.SetGELFHost(s.config.DeviceID)
	}
	logger.SetGELFDeviceType(deviceType)
	// No GELF_URL on the device (the shipped case) → relay through the cloud API
	// with the device key; the collector credential stays server-side. No-op when
	// GELF_URL is set or the device has no Autonomous credential to relay with.
	logger.EnableGELFRelay(s.config.GELFRelayCredentials())

	// Common fields for every tracking event this device sends (see
	// system/telemetry). Set once here, where the resolved device class,
	// runtime and version all exist.
	telemetry.SetCommon(map[string]any{
		"os_version":                     config.OSVersion,
		"device_type":                    deviceType,
		"agent_runtime":                  string(device.CurrentAgentRuntimeFromConfig(s.config)),
		"realtime_supersedes_main_reply": _agentHttpDeliver.RealtimeSupersedesMainReply(),
	})
	// i18n device name (wake-words + {name}/{Name} in strings) — device_type as the
	// startup fallback; WatchIdentity overrides with the agent name once IDENTITY.md loads.
	i18n.SetDeviceName(deviceType)

	// Register the shared bearer token for outbound HAL HTTP calls.
	// HAL's local_only_middleware accepts Authorization: Bearer <llm_api_key>
	// as one of its allow paths; sending it lets calls succeed even if loopback
	// bypass is tightened later. Empty key drops the header (local LLM mode).
	hal.SetAPIKey(s.config.LLMAPIKey)

	// Signal booting state so the LED shows a slow blue pulse while initializing.
	s.statusLED.Set(statusled.StateBooting)

	// Wire i18n before any TTS-firing goroutine starts. Must precede StartWS
	// below — a WS reconnect that lands before i18n is wired falls back to
	// English even when STTLanguage is "vi"/"zh-*".
	i18n.SetConfig(s.config)

	// Seed the default TTS provider + voice from ROBOT.md (`voice:` block) when
	// the user hasn't chosen them yet. Persisting here means every downstream
	// consumer — HAL auto-start, StartHALVoice, and the Setup UI prefill — sees
	// the same device default; the user can still override in Setup/Settings
	// (their saved value is non-empty, so this never clobbers it). No declaration
	// → stays empty → HAL falls back to the legacy defaults (openai / "nova").
	// Only seeds on a first boot with empty fields; the WithLockSave notify then
	// costs at most one idempotent config reload, and never fires again.
	//
	// Voice must match the provider: an elevenlabs default with the openai voice
	// "nova" would 400 at ElevenLabs (unknown voice id). So when the seeded
	// provider is elevenlabs and no voice is set, pick a language-aware default
	// (Rachel/Ngan/Amy) unless ROBOT.md pins one via voice.tts_voice.
	seedProvider := ""
	if s.config.TTSProvider == "" {
		if p := device.TTSProvider(deviceType); domain.IsValidTTSProvider(p) {
			seedProvider = p
		}
	}
	effectiveProvider := s.config.TTSProvider
	if seedProvider != "" {
		effectiveProvider = seedProvider
	}
	seedVoice := ""
	if s.config.TTSVoice == "" {
		if v := device.TTSVoice(deviceType); v != "" {
			seedVoice = v
		} else if effectiveProvider == domain.TTSProviderElevenLabs {
			seedVoice = domain.DefaultElevenLabsVoiceForLang(s.config.STTLanguage)
		} else if effectiveProvider == domain.TTSProviderGemini {
			seedVoice = domain.DefaultGeminiVoice
		}
	}
	if seedProvider != "" || seedVoice != "" {
		if err := s.config.WithLockSave(func(c *config.Config) {
			if seedProvider != "" {
				c.TTSProvider = seedProvider
			}
			if seedVoice != "" {
				c.TTSVoice = seedVoice
			}
		}); err != nil {
			slog.Warn("seed tts defaults from ROBOT.md failed", "component", "server", "provider", seedProvider, "voice", seedVoice, "error", err)
		} else {
			slog.Info("seeded tts defaults from ROBOT.md", "component", "server", "provider", seedProvider, "voice", seedVoice)
		}
	}

	// Wake-word gate: adopt the body's declared default (ROBOT.md voice.wakeword)
	// while config.json still has no wakeword key. Only a config.json os-server
	// just created reaches here with nil — one loaded from disk without the key
	// is a device provisioned before the switch existed, and ProvideConfig has
	// already pinned it to false so an OTA cannot make a device in use stop
	// answering. Once written, Settings owns the value; this never runs again.
	if s.config.WakeWord == nil {
		if v, declared := device.WakeWordDefault(deviceType); declared {
			if err := s.config.WithLockSave(func(c *config.Config) {
				c.WakeWord = &v
			}); err != nil {
				slog.Warn("seed wakeword default from ROBOT.md failed", "component", "server", "wakeword", v, "error", err)
			} else {
				slog.Info("seeded wakeword default from ROBOT.md", "component", "server", "wakeword", v)
			}
		}
	}

	s.handleSetUpCompleteChange(s.config.SetUpCompleted)
	s.handleDeviceIDChange(s.config.DeviceID)
	s.handleMQTTConfigChange()

	configCtx, cancelConfig := context.WithCancel(context.Background())
	defer cancelConfig()
	go s.runConfigChangeListener(configCtx)

	eventCtx, cancelEvents := context.WithCancel(context.Background())
	defer cancelEvents()
	if err := s.initializeExternalHistory(eventCtx); err != nil {
		return err
	}
	harnessService, harnessErr := harness.NewService("config", harness.Callbacks{
		OnEvent:       s.forwardHarnessEvent,
		BeforeEvent:   s.captureHarnessResult,
		BeforeRequest: s.reserveHarnessResult,
		OnReceipt:     s.bindHarnessResultReceipt,
		OnRevoked: func() {
			if s.harnessVoice != nil {
				_, _ = s.harnessVoice.SetMode(eventCtx, false)
			}
		},
	})
	if harnessErr != nil {
		slog.Error("harness service initialization failed", "component", "harness", "error", harnessErr)
	} else {
		s.harnessService = harnessService
		if err := s.initializeHarnessResults("config/harness/results.json"); err != nil {
			slog.Error("Harness result storage unavailable", "error", err)
		}
		go s.watchHarnessResults(eventCtx)
		s.restoreHarnessHistoryReplies()
		harnessService.Start(eventCtx)
		s.deviceMQTTHandler.SetHarnessService(harnessService)
	}
	go s.agentGateway.StartWS(eventCtx, s.agentHandler.HandleEvent)
	go s.agentGateway.WatchIdentity(eventCtx)
	go s.agentGateway.StartSkillWatcher(eventCtx)
	// Mirrors chat.send runs back to the backend over fd_channel, so a phone app
	// sees the same turn the web monitor's SSE stream shows. Costs nothing until
	// a chat.send arrives — no run is tracked, so every bus event is dropped.
	s.chatStream.Start(eventCtx)
	go s.deviceMQTTHandler.StartBuddyStatusLoop(eventCtx)
	go s.deviceMQTTHandler.StartHarnessStatusLoop(eventCtx)
	// StartModelSync is launched from the startup-sequence goroutine AFTER
	// EnsureOnboarding completes, so the two writers to openclaw.json don't
	// race on first boot (sync's atomic write vs ensureAgentDefaults' plain
	// os.WriteFile would clobber each other).

	r := gin.Default()
	r.RedirectTrailingSlash = false // avoid 301 redirect loop on /network vs /network/
	r.Use(corsMiddleware())
	r.Use(gin.Recovery())

	api := r.Group("api")
	s.registerHarnessRoutes(api, eventCtx)

	health := api.Group("health")
	health.GET("/live", s.healthHandler.Live)
	health.GET("/readiness", s.healthHandler.Readiness)

	system := api.Group("system")
	system.GET("info", s.healthHandler.SystemInfo)
	system.GET("network", s.healthHandler.NetworkInfo)
	system.GET("dashboard", s.healthHandler.Dashboard)
	system.GET("ota-security", s.otaSecurity)
	system.GET("ota-versions", s.otaVersions)
	system.GET("ota-updating", s.otaUpdating)
	system.POST("software-update/:target", adminAuthMiddleware(s.config), s.softwareUpdate)
	system.POST("reboot", adminAuthMiddleware(s.config), systemshell.Reboot)
	system.POST("restart/:target", adminAuthMiddleware(s.config), serviceRestartHandler(restartCommand))
	system.POST("shutdown", adminAuthMiddleware(s.config), systemshell.Shutdown)
	system.POST("factory-reset", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		systemshell.FactoryReset(c, s.agentGateway)
	})
	system.POST("exec", localOnlyMiddleware(), s.execCommand)
	// xterm.js shell: admin-gated. WS upgrade doesn't carry the Bearer header
	// in browsers, so the cookie path inside adminAuthMiddleware is the live
	// auth on this route. Scripts may still ?token=<llm_api_key>=.
	system.GET("shell", adminAuthMiddleware(s.config), systemshell.ShellHandler(s.shellAgentEnvFile))

	// Login: POST {password} → bcrypt-verifies admin_password_hash, mints
	// signed session cookie. No auth required (this is how you get auth).
	api.POST("login", s.loginHandler)
	api.POST("logout", s.logoutHandler)
	// Exchange Bearer auth for a session cookie on the current origin.
	// Used by the AP→.local post-setup redirect: os_session is bound to
	// the AP origin and doesn't survive the host switch, so the web carries
	// the Bearer (llm_api_key) across via URL fragment and exchanges it for
	// a cookie here. adminAuthMiddleware already validates the Bearer (or an
	// existing cookie), so the handler just mints a fresh cookie. No new
	// capability vs. Bearer auth — both are root under the shared-secret
	// threat model — purely a UX helper that survives refresh / new tabs.
	api.POST("login/exchange", adminAuthMiddleware(s.config), s.loginExchangeHandler)

	device := api.Group("device")
	device.POST("setup", setupOrAdminMiddleware(s.config), s.deviceHandler.Setup)
	device.GET("setup/status", s.deviceHandler.SetupStatus)
	// AP-portal fast path: re-provision only the Wi-Fi association on an
	// already-configured device. Auth is physical presence on the hotspot
	// (client IP in the AP subnet); see middleware.apOnlyMiddleware.
	device.POST("wifi-provision", apOnlyMiddleware(), s.deviceHandler.WifiProvision)
	device.POST("channel", adminAuthMiddleware(s.config), s.deviceHandler.ChangeChannel)
	// GET config is admin-gated now. Pre-login web can no longer bootstrap
	// the bearer from here — browser must POST /api/login first (cookie),
	// scripts/curl must send Authorization: Bearer <llm_api_key>.
	device.GET("config", adminAuthMiddleware(s.config), s.deviceHandler.GetConfig)
	device.PUT("config", adminAuthMiddleware(s.config), s.deviceHandler.UpdateConfig)
	device.POST("restore-defaults", adminAuthMiddleware(s.config), s.deviceHandler.RestoreDefaults)
	device.GET("voices", s.deviceHandler.GetVoices)
	device.GET("tts-providers", s.deviceHandler.GetTTSProviders)
	device.GET("realtime-options", s.deviceHandler.GetRealtimeOptions)
	device.GET("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.GetAgentRuntime)
	device.POST("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.SetAgentRuntime)
	device.GET("timezone", adminAuthMiddleware(s.config), s.deviceHandler.GetTimezone)
	device.POST("timezone", adminAuthMiddleware(s.config), s.deviceHandler.SetTimezone)
	device.GET("mcp-tools", adminAuthMiddleware(s.config), s.deviceHandler.ListMCPTools)
	device.POST("mcp-tools", adminAuthMiddleware(s.config), s.deviceHandler.AddMCPTool)
	device.DELETE("mcp-tools/:name", adminAuthMiddleware(s.config), s.deviceHandler.RemoveMCPTool)

	pluginGroup := api.Group("plugin")
	// PARKED with the handler (#213): plugin discovery moves from Hugging Face
	// Spaces to our own catalog. Uncomment when the catalog has `plugins`.
	// pluginGroup.GET("browse", adminAuthMiddleware(s.config), s.pluginHandler.Browse)
	pluginGroup.POST("install", adminAuthMiddleware(s.config), s.pluginHandler.Install)
	pluginGroup.GET("", adminAuthMiddleware(s.config), s.pluginHandler.List)
	pluginGroup.POST(":name/start", adminAuthMiddleware(s.config), s.pluginHandler.Start)
	pluginGroup.POST(":name/stop", adminAuthMiddleware(s.config), s.pluginHandler.Stop)
	pluginGroup.DELETE(":name", adminAuthMiddleware(s.config), s.pluginHandler.Uninstall)

	network := api.Group("network")
	network.GET("", s.networkHandler.GetNetworks)
	network.GET("current", s.networkHandler.GetCurrentNetwork)
	network.GET("check-internet", s.networkHandler.CheckInternet)

	// Product analytics ingestion for on-device producers (HAL voice metrics
	// today). Loopback/LAN only, same gate as sensing: the poster is another
	// process on this device, never a browser session.
	telemetryGroup := api.Group("telemetry")
	telemetryGroup.POST("event", sameOriginOrLAN(), _telemetryHttpDeliver.ProvideTelemetryHandler().PostEvent)

	sensing := api.Group("sensing")
	sensing.POST("event", sameOriginOrLAN(), s.sensingHandler.PostEvent)
	sensing.GET("snapshot/:category/:name", s.sensingHandler.GetSnapshot)
	sensing.GET("agent-snapshot/:runtime/:source/:name", s.sensingHandler.GetAgentSnapshot)
	sensing.GET("audio/:name", s.sensingHandler.GetAudio)
	// HAL-driven dead-air filler for the realtime wait (see PlayFiller).
	sensing.POST("filler", s.sensingHandler.PlayFiller)

	// Voice file delete (filesystem orchestration on Pi). Voice enroll
	// itself lives on hal at /hw/speaker/record-enroll because hardware
	// capture is Python's domain.
	voice := api.Group("voice")
	voice.POST("file/remove", s.sensingHandler.RemoveVoiceFile)
	// TTS preview: web ships `{text, voice, provider}` only; server reads
	// the TTS API key + base URL from cfg and forwards to HAL. Replaces
	// the previous web-side `testTTSVoice` that POSTed tts_api_key through
	// the hardware proxy (audit web F13).
	voice.POST("preview", adminAuthMiddleware(s.config), s.voicePreview)
	// Piper (on-device TTS) install + voice download, proxied to HAL. Not part
	// of any OTA component, so the operator pulls it on demand from Settings →
	// Voice. Admin-gated: it installs software and writes ~63 MB per voice.
	voice.GET("piper/status", adminAuthMiddleware(s.config), s.piperStatus)
	voice.POST("piper/install", adminAuthMiddleware(s.config), s.piperInstall)
	voice.POST("piper/voice", adminAuthMiddleware(s.config), s.piperVoice)
	voice.POST("piper/voice/remove", adminAuthMiddleware(s.config), s.piperVoiceRemove)

	// Guard endpoints change persistent security state and can broadcast to every
	// chat session. Device-local HAL and agent-runtime callers are allowed; all
	// other callers must be authenticated as an administrator.
	guard := api.Group("guard", adminOrLoopbackAuth(s.config))
	guard.POST("enable", s.sensingHandler.EnableGuard)
	guard.POST("disable", s.sensingHandler.DisableGuard)
	guard.GET("", s.sensingHandler.GetGuardStatus)
	guard.POST("alert", s.sensingHandler.PostGuardAlert)

	moodGroup := api.Group("mood")
	moodGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostMoodLog)

	wellbeingGroup := api.Group("wellbeing")
	wellbeingGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostWellbeingLog)

	postureGroup := api.Group("posture")
	postureGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostPostureLog)

	musicSuggGroup := api.Group("music-suggestion")
	musicSuggGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostMusicSuggestionLog)
	musicSuggGroup.POST("status", sameOriginOrLAN(), s.sensingHandler.PostMusicSuggestionStatus)

	monitor := api.Group("monitor")
	monitor.POST("event", sameOriginOrLAN(), s.sensingHandler.PostMonitorEvent)

	// Autonomous Buddy (macOS companion app for remote computer use):
	//   - /pair/start, /status, /command, DELETE admin-gated
	//   - /pair/confirm anonymous (code-based)
	//   - /ws bearer-token gated (validated in handler against buddies.json)
	//   - /command localhost-only (OpenClaw skill is the caller)
	buddy := api.Group("buddy")
	buddy.POST("pair/start", adminAuthMiddleware(s.config), s.buddyHandler.PairStart)
	buddy.POST("pair/confirm", s.buddyHandler.PairConfirm)
	buddy.GET("status", adminAuthMiddleware(s.config), s.buddyHandler.Status)
	buddy.DELETE("", adminAuthMiddleware(s.config), s.buddyHandler.Revoke)
	// /self auth via Bearer token (the buddy app's own token), used when the
	// user unpairs from inside the buddy app — symmetric counterpart to the
	// admin DELETE above. Keeps device + buddy state in sync without manual web
	// UI clicks.
	buddy.DELETE("self", s.buddyHandler.RevokeSelf)
	buddy.GET("ws", s.buddyHandler.WS)
	buddy.POST("command", localOnlyMiddleware(), s.buddyHandler.Command)
	buddy.POST("observe", localOnlyMiddleware(), s.buddyHandler.Observe)
	buddy.POST("suggest", localOnlyMiddleware(), s.buddyHandler.Suggest)
	// /exec/:action is the marker-friendly variant used by OpenClaw skills via
	// [HW:/buddy/exec/<action>:{...}]. Localhost-only (loopback from agent handler's hwMarker dispatcher).
	buddy.POST("exec/:action", localOnlyMiddleware(), s.buddyHandler.Exec)

	agent := api.Group("agent")
	// Everything under /api/agent/ is admin-gated: status carries device
	// state, events / flow-stream / recent / flow-events / flow-logs /
	// analytics / compaction-latest contain conversation history + sensing
	// data, and mood/wellbeing/posture/music-suggestion histories are
	// per-user behavioural records. config-json keeps its stricter
	// `localOnlyMiddleware` (loopback callers only) — admin auth alone is
	// not enough since the raw openclaw.json holds gateway tokens.
	agent.POST("tts/stop", adminAuthMiddleware(s.config), s.agentHandler.StopTTS)
	agent.POST("busy", adminAuthMiddleware(s.config), s.agentHandler.SetBusy)
	// Physical cancel gesture — HAL calls this from the device itself, so it
	// authenticates by locality like the other HAL-initiated endpoints rather
	// than by admin token (the button must work before/without a login).
	agent.POST("speech/cancel", localOnlyMiddleware(), s.agentHandler.CancelSpeechHandler)
	// Restart the active runtime (openclaw/hermes/codex/opencode/claudecode/picoclaw).
	// Each runtime's RestartAgent() picks the actual command — see handler_api_monitor.go.
	agent.POST("restart", adminAuthMiddleware(s.config), s.agentHandler.Restart)
	agent.GET("status", adminAuthMiddleware(s.config), s.agentHandler.Status)
	agent.GET("events", adminAuthMiddleware(s.config), s.agentHandler.Events)
	agent.GET("recent", adminAuthMiddleware(s.config), s.agentHandler.Recent)
	agent.GET("flow-events", adminAuthMiddleware(s.config), s.agentHandler.FlowEvents)
	agent.GET("mood-history", adminAuthMiddleware(s.config), s.agentHandler.MoodHistory)
	agent.GET("wellbeing-history", adminAuthMiddleware(s.config), s.agentHandler.WellbeingHistory)
	agent.GET("posture-history", adminAuthMiddleware(s.config), s.agentHandler.PostureHistory)
	agent.GET("music-suggestion-history", adminAuthMiddleware(s.config), s.agentHandler.MusicSuggestionHistory)
	agent.GET("flow-stream", adminAuthMiddleware(s.config), s.agentHandler.FlowStream)
	agent.GET("flow-logs", adminAuthMiddleware(s.config), s.agentHandler.FlowLogs)
	agent.DELETE("flow-logs", adminAuthMiddleware(s.config), s.agentHandler.ClearFlowLogs)
	agent.GET("analytics", adminAuthMiddleware(s.config), s.agentHandler.Analytics)
	agent.GET("config-json", localOnlyMiddleware(), s.agentHandler.ConfigJSON)
	// user-reconcile: HAL calls this after an enrollment DIRECTORY disappears
	// (/face/remove, /face/reset, /users/rename) so a retired person's profile
	// leaves USER.md immediately instead of lingering until the next boot —
	// otherwise removing someone from the UI is a half-delete. Loopback-only,
	// like the other HAL-initiated endpoints: it must work before/without a
	// login. Deliberately NOT wired to /speaker/remove, which only drops the
	// voice/ subdir and leaves the person (and their face) enrolled.
	agent.POST("user-reconcile", localOnlyMiddleware(), func(c *gin.Context) {
		s.userReconcile.Reconcile()
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"reconciled": true}))
	})
	// memory/reset: no-SSH recovery for self-written memory that poisoned
	// routing (#421). Admin-only — it deletes learned memory (with a backup).
	agent.POST("memory/reset", adminAuthMiddleware(s.config), s.agentHandler.ResetMemory)
	// channel-turn: the Hermes gateway observer hook POSTs each turn here so
	// channel (Telegram/Slack/…) turns surface in Flow Monitor. Loopback-only.
	agent.POST("channel-turn", localOnlyMiddleware(), s.agentHandler.ChannelTurn)
	agent.GET("compaction-latest", adminAuthMiddleware(s.config), s.agentHandler.CompactionLatest)
	// Skill store discovery for the chat composer's "+" → Skills → Browse.
	// Proxied server-side (no CORS, store host stays off the browser). `bundle`
	// takes the skill id as a query param so it can't collide with `browse`.
	agent.GET("skills/browse", adminAuthMiddleware(s.config), s.agentHandler.BrowseSkills)
	agent.GET("skills/bundle", adminAuthMiddleware(s.config), s.agentHandler.SkillBundle)
	// Authoring: writes into the ACTIVE runtime's skills dir via the gateway;
	// backends that haven't implemented it answer 501 and store nothing.
	agent.GET("skills", adminAuthMiddleware(s.config), s.agentHandler.ListSkills)
	agent.GET("skills/files", adminAuthMiddleware(s.config), s.agentHandler.ReadSkillFiles)
	agent.POST("skills/publish", adminAuthMiddleware(s.config), s.agentHandler.PublishSkill)
	agent.POST("skills", adminAuthMiddleware(s.config), s.agentHandler.SaveSkill)
	agent.POST("skills/install", adminAuthMiddleware(s.config), s.agentHandler.InstallSkill)
	agent.POST("skills/upload", adminAuthMiddleware(s.config), s.agentHandler.UploadSkill)
	agent.DELETE("skills", adminAuthMiddleware(s.config), s.agentHandler.DeleteSkill)
	// Device-local file the agent produced, so a reply that names a path (a
	// camera snapshot, a generated report) can be SHOWN in the chat instead of
	// read as an unusable string. `path` is client-supplied and validated
	// against an allow-list of roots + served types — see handler_file.go.
	agent.GET("file", adminAuthMiddleware(s.config), s.agentHandler.ServeFile)

	// Scheduled tasks: read-only mirror of the backend's schedule.sync list
	// plus a local "Run now", surfaced by deviceMQTTHandler because that's
	// where the shared schedule.Store/schedule.Runner already live (see
	// ProvideDeviceMQTTHandler) — no second copy of the schedule data, and no
	// new wire provider needed. Admin-gated like the agent group above: both
	// endpoints expose schedule contents (instructions included) and the run
	// endpoint can trigger a real agent turn, so this is not local-only.
	scheduleGroup := api.Group("schedule")
	scheduleGroup.GET("list", adminAuthMiddleware(s.config), s.deviceMQTTHandler.ListSchedules)
	scheduleGroup.POST(":id/run", adminAuthMiddleware(s.config), s.deviceMQTTHandler.RunScheduleNow)
	// Device-side CRUD. These do NOT write schedules.json directly — each one
	// queues a proposal the backend must confirm (see schedule_crud_handler.go),
	// so the local UI can never arm a task the cloud does not know about.
	scheduleGroup.POST("", adminAuthMiddleware(s.config), s.deviceMQTTHandler.CreateSchedule)
	scheduleGroup.PATCH(":id", adminAuthMiddleware(s.config), s.deviceMQTTHandler.UpdateSchedule)
	scheduleGroup.DELETE(":id", adminAuthMiddleware(s.config), s.deviceMQTTHandler.DeleteSchedule)

	// Connectors: the local Settings UI's write path for a static-credential
	// (PAT) connector. Persists through the SAME connectorWriter the MQTT
	// connector.set.<code> dispatcher uses, so a token pasted on-device lands
	// in the same <code>_access_tokens.json file the skill layer already
	// reads — no separate storage, no drift. GET reports connected + the
	// non-secret identity fields (never the token); DELETE removes both the
	// on-disk entry and any mcp.servers.<code> side-effect.
	connectorGroup := api.Group("device/connectors")
	connectorGroup.POST("pat", adminAuthMiddleware(s.config), s.deviceMQTTHandler.SetConnectorPAT)
	connectorGroup.GET(":code", adminAuthMiddleware(s.config), s.deviceMQTTHandler.GetConnector)
	connectorGroup.DELETE(":code", adminAuthMiddleware(s.config), s.deviceMQTTHandler.RemoveConnector)

	// Look: snapshot + describe in one call, so the agent gets text it can read
	// instead of a file path it cannot. Loopback-only — the caller is the
	// agent's own shell tool, and it moves hardware and spends a vision-model
	// call. See lookAndDescribe in vision.go.
	api.POST("vision/look", localOnlyMiddleware(), s.lookAndDescribe)
	api.GET("environment/status", localOnlyMiddleware(), s.environmentStatus)

	logs := api.Group("logs")
	logs.GET("tail", adminAuthMiddleware(s.config), s.logTail)
	logs.GET("stream", adminAuthMiddleware(s.config), s.logStream)

	// Wildcard reverse proxy: web UI calls /api/hardware/<anything> with a
	// bearer token; Go gates the request then forwards to HAL on loopback.
	// Replaces direct browser /hw/* access (audit web F5) so nginx /hw/
	// allow 127.0.0.1; deny all; can stay locked down (audit local F2).
	api.Any("/hardware/*path", adminAuthMiddleware(s.config), s.ambientLEDGate(), gin.WrapH(hardwareProxy))

	// Top-level /openapi.json so the in-iframe HAL Swagger UI (loaded at
	// /api/hardware/docs) can fetch its spec — FastAPI hardcodes the spec
	// URL as the absolute path `/openapi.json` in the rendered HTML, so we
	// expose it at the root. Admin-auth gated; cookie auto-attaches in the
	// iframe context. Loopback-only on HAL side already enforced by the
	// proxy's same upstream as `/api/hardware/*`.
	r.GET("/openapi.json", adminAuthMiddleware(s.config), gin.WrapH(openapiProxy))

	slog.Info("server started", "component", "server")

	errChan := make(chan error)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGINT, syscall.SIGTERM)

	srv := &http.Server{
		Addr:    fmt.Sprintf("127.0.0.1:%d", s.config.HttpPort),
		Handler: r,
	}

	// HTTP server is about to listen — booting is done.
	s.statusLED.Clear(statusled.StateBooting)

	// When the device is still in AP/provisioning mode, paint the strip solid
	// white as a visual "ready for WiFi setup" signal. os-server typically reaches
	// this point before HAL's FastAPI is up on :5001 (Python boot is
	// slower — loads rpi_ws281x, SPI, audio, camera), so we poll /health in
	// the background and fire the setup status only once LED hardware reports ready.
	// Skipped post-setup — agent flash + ambient take over from here.
	if !s.config.SetUpCompleted {
		safego.Go("setup-needed-paint", s.waitAndPaintSetupReady)
	}

	// Warm the Go-owned spoken notices into hal's persistent WAV cache so
	// they still play when the TTS provider is rate-limited — the LLM-limit
	// notice fires exactly when TTS shares the exhausted quota, so it can't
	// be rendered on demand. Retries cover hal booting slower than os-server
	// and a quota-exhausted boot (next attempts after the provider recovers).
	safego.Go("notice-prerender", func() {
		phrase := i18n.One(i18n.PhraseLLMLimit)
		for attempt := 0; attempt < 5; attempt++ {
			time.Sleep(time.Duration(30+attempt*60) * time.Second)
			if err := hal.PrerenderCached(phrase); err == nil {
				slog.Info("notice prerender warmed", "component", "server", "text", phrase)
				return
			}
		}
		slog.Warn("notice prerender never succeeded — notice will self-warm on first successful fire", "component", "server")
	})

	go environment.Service{
		Startup:   s.environmentStartup,
		Settings:  s.config.EnvironmentSettings,
		Available: s.environmentAvailable,
		Read:      hal.GetEnvironmentStatusContext,
		Send:      environment.HTTPSender(fmt.Sprintf("http://127.0.0.1:%d/api/sensing/event", s.config.HttpPort)),
	}.Run(eventCtx)

	go func() {
		if err := srv.ListenAndServe(); err != nil {
			errChan <- err
		}
	}()

	for {
		select {
		case <-stop:
			// The context is used to inform the server it has 5 seconds to finish
			// the request it is currently handling
			cancelConfig()
			s.monitorMu.Lock()
			if s.monitorCancel != nil {
				s.monitorCancel()
			}
			s.monitorMu.Unlock()
			cancelEvents()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := srv.Shutdown(ctx); err != nil {
				log.Fatal("Server forced to shutdown: ", err)
			}
			closeFn()
			return nil
		case err := <-errChan:
			return err
		}
	}
}
