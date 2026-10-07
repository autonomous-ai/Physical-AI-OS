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
	// harnessReplies is keyed by the local device run ID, not agent ID: one
	// agent can serve several requests at once.
	harnessReplies       map[string]harnessReply
	harnessOverlapAgents map[string]bool
	harnessFollowup      atomic.Int64
	harnessResultMu      sync.RWMutex
	harnessResult        string
	harnessResultAt      time.Time
	// harnessPermissionNotified holds announced permission dialogs (machine/agent/question);
	// only the matching question.close clears one.
	harnessPermissionMu       sync.Mutex
	harnessPermissionNotified map[string]bool
	engine                    *gin.Engine
	config                    *config.Config

	environmentStartup *environment.StartupCoordinator

	// handlers
	healthHandler     _healthHttpDeliver.HealthHandler
	networkHandler    _networkHttpDeliver.NetworkHandler
	deviceHandler     _deviceHttpDeliver.DeviceHandler
	deviceMQTTHandler _deviceMQTTDeliver.DeviceMQTTHandler
	agentHandler      *_agentHttpDeliver.AgentHandler
	sensingHandler    *_sensingHttpDeliver.SensingHandler
	buddyHandler      *_buddyHttpDeliver.BuddyHandler
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
	// lastMQTTSig is the last MQTT-connection signature we acted on (endpoint
	// + port + username + password + fa_channel).
	lastMQTTSig *string
}

// Engine ...
func (s *Server) Engine() *gin.Engine {
	return s.engine
}

// shellAgentEnvFile resolves, per web-CLI connection, the launch env file to
// source into the PTY so an interactive `claude` reuses the campaign key.
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
	buddyH *_buddyHttpDeliver.BuddyHandler,
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
	deviceType := s.config.DeviceTypeOrDefault()
	if deviceType == "" {
		log.Fatal("[config] device_type unresolved — set DEVICE_TYPE env (provisioning) or config.json device_type; refusing to assume 'lamp'")
	}
	// Persist device_type: provisioning only sets the env, and config.json
	// readers (HAL wake words, software-update) need the key.
	if s.config.DeviceType != deviceType {
		s.config.DeviceType = deviceType
		if err := s.config.Save(); err != nil {
			slog.Error("seed device_type failed", "component", "config", "error", err)
		}
	}

	if s.config.DeviceID != "" {
		logger.SetGELFHost(s.config.DeviceID)
	}
	logger.SetGELFDeviceType(deviceType)
	logger.EnableGELFRelay(s.config.GELFRelayCredentials())

	telemetry.SetCommon(map[string]any{
		"os_version":                     config.OSVersion,
		"device_type":                    deviceType,
		"agent_runtime":                  string(device.CurrentAgentRuntimeFromConfig(s.config)),
		"realtime_supersedes_main_reply": _agentHttpDeliver.RealtimeSupersedesMainReply(),
	})
	i18n.SetDeviceName(deviceType)

	// HAL's local_only_middleware accepts Authorization: Bearer <llm_api_key>
	// as one of its allow paths; sending it lets calls succeed even if
	// loopback bypass is tightened later.
	hal.SetAPIKey(s.config.LLMAPIKey)

	s.statusLED.Set(statusled.StateBooting)

	// Must precede StartWS below — a WS reconnect that lands before i18n is
	// wired falls back to English even when STTLanguage is "vi"/"zh-*".
	i18n.SetConfig(s.config)

	// Seed TTS provider/voice from ROBOT.md only while unset; never clobbers
	// a user choice. The voice must match the provider (elevenlabs rejects "nova").
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

	// Adopt ROBOT.md's wakeword default only for a freshly created config;
	// ProvideConfig pins pre-existing configs to false.
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
	s.chatStream.Start(eventCtx)
	go s.deviceMQTTHandler.StartBuddyStatusLoop(eventCtx)
	go s.deviceMQTTHandler.StartHarnessStatusLoop(eventCtx)
	// StartModelSync runs AFTER EnsureOnboarding (see config_watch.go): both
	// write openclaw.json.

	r := gin.New()
	r.Use(credentialSafeLogger(gin.DefaultWriter))
	r.RedirectTrailingSlash = false // avoid 301 redirect loop on /network vs /network/
	r.Use(corsMiddleware())
	r.Use(credentialSafeRecovery(gin.DefaultErrorWriter))

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
	// WS upgrade doesn't carry the Bearer header in browsers, so the cookie
	// path inside adminAuthMiddleware is the live auth on this route.
	system.GET("shell", adminAuthMiddleware(s.config), systemshell.ShellHandler(s.shellAgentEnvFile))

	// Login: POST {password} → bcrypt-verifies admin_password_hash, mints
	// signed session cookie. No auth required (this is how you get auth).
	api.POST("login", s.loginHandler)
	api.POST("logout", s.logoutHandler)
	// Exchange Bearer auth for a session cookie on the current origin.
	api.POST("login/exchange", adminAuthMiddleware(s.config), s.loginExchangeHandler)

	device := api.Group("device")
	device.POST("setup", setupOrAdminMiddleware(s.config), s.deviceHandler.Setup)
	device.GET("setup/status", s.deviceHandler.SetupStatus)
	// Auth is physical presence on the hotspot (client IP in the AP subnet);
	// see middleware.apOnlyMiddleware.
	device.POST("wifi-provision", apOnlyMiddleware(), s.deviceHandler.WifiProvision)
	device.POST("channel", adminAuthMiddleware(s.config), s.deviceHandler.ChangeChannel)
	// Pre-login web can no longer bootstrap the bearer from here — browser
	// must POST /api/login first (cookie), scripts/curl must send
	// Authorization: Bearer <llm_api_key>.
	device.GET("config", adminAuthMiddleware(s.config), s.deviceHandler.GetConfig)
	device.PUT("config", adminAuthMiddleware(s.config), s.deviceHandler.UpdateConfig)
	device.POST("restore-defaults", adminAuthMiddleware(s.config), s.deviceHandler.RestoreDefaults)
	device.GET("voices", s.deviceHandler.GetVoices)
	device.GET("tts-providers", s.deviceHandler.GetTTSProviders)
	device.GET("realtime-options", s.deviceHandler.GetRealtimeOptions)
	device.GET("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.GetAgentRuntime)
	device.POST("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.SetAgentRuntime)
	device.GET("runtime-login", adminAuthMiddleware(s.config), s.deviceHandler.GetRuntimeLogin)
	device.POST("runtime-login", adminAuthMiddleware(s.config), s.deviceHandler.StartRuntimeLogin)
	device.POST("runtime-login/code", adminAuthMiddleware(s.config), s.deviceHandler.SubmitRuntimeLoginCode)
	device.DELETE("runtime-login/:id", adminAuthMiddleware(s.config), s.deviceHandler.CancelRuntimeLogin)
	device.GET("timezone", adminAuthMiddleware(s.config), s.deviceHandler.GetTimezone)
	device.POST("timezone", adminAuthMiddleware(s.config), s.deviceHandler.SetTimezone)
	device.GET("mcp-tools", adminAuthMiddleware(s.config), s.deviceHandler.ListMCPTools)
	device.POST("mcp-tools", adminAuthMiddleware(s.config), s.deviceHandler.AddMCPTool)
	device.DELETE("mcp-tools/:name", adminAuthMiddleware(s.config), s.deviceHandler.RemoveMCPTool)

	pluginGroup := api.Group("plugin")
	// PARKED (#213) until our own catalog serves plugins.
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

	// Admin or direct loopback only, same gate as sensing: the poster is an on-device process.
	telemetryGroup := api.Group("telemetry")
	telemetryGroup.POST("event", adminOrLoopbackAuth(s.config), _telemetryHttpDeliver.ProvideTelemetryHandler().PostEvent)

	sensing := api.Group("sensing")
	// Every event can become an agent turn; LAN membership and Origin are not credentials.
	sensing.POST("event", adminOrLoopbackAuth(s.config), s.sensingHandler.PostEvent)
	sensing.GET("snapshot/:category/:name", s.sensingHandler.GetSnapshot)
	sensing.GET("agent-snapshot/:runtime/:source/:name", s.sensingHandler.GetAgentSnapshot)
	sensing.GET("audio/:name", s.sensingHandler.GetAudio)
	s.registerVoiceMutationRoutes(api)

	voice := api.Group("voice")
	// TTS preview reads the API key from cfg server-side so the web never sends it (audit web F13).
	voice.POST("preview", adminAuthMiddleware(s.config), s.voicePreview)
	voice.GET("piper/status", adminAuthMiddleware(s.config), s.piperStatus)
	voice.POST("piper/install", adminAuthMiddleware(s.config), s.piperInstall)
	voice.POST("piper/voice", adminAuthMiddleware(s.config), s.piperVoice)
	voice.POST("piper/voice/remove", adminAuthMiddleware(s.config), s.piperVoiceRemove)

	// Guard endpoints change persistent security state and can broadcast to
	// every chat session.
	guard := api.Group("guard", adminOrLoopbackAuth(s.config))
	guard.POST("enable", s.sensingHandler.EnableGuard)
	guard.POST("disable", s.sensingHandler.DisableGuard)
	guard.GET("", s.sensingHandler.GetGuardStatus)
	guard.POST("alert", s.sensingHandler.PostGuardAlert)

	moodGroup := api.Group("mood")
	moodGroup.POST("log", adminOrLoopbackAuth(s.config), s.sensingHandler.PostMoodLog)

	wellbeingGroup := api.Group("wellbeing")
	wellbeingGroup.POST("log", adminOrLoopbackAuth(s.config), s.sensingHandler.PostWellbeingLog)

	postureGroup := api.Group("posture")
	postureGroup.POST("log", adminOrLoopbackAuth(s.config), s.sensingHandler.PostPostureLog)

	musicSuggGroup := api.Group("music-suggestion")
	musicSuggGroup.POST("log", adminOrLoopbackAuth(s.config), s.sensingHandler.PostMusicSuggestionLog)
	musicSuggGroup.POST("status", adminOrLoopbackAuth(s.config), s.sensingHandler.PostMusicSuggestionStatus)

	monitor := api.Group("monitor")
	monitor.POST("event", adminOrLoopbackAuth(s.config), s.sensingHandler.PostMonitorEvent)

	buddy := api.Group("buddy")
	buddy.POST("pair/start", adminAuthMiddleware(s.config), s.buddyHandler.PairStart)
	buddy.POST("pair/confirm", s.buddyHandler.PairConfirm)
	buddy.GET("status", adminAuthMiddleware(s.config), s.buddyHandler.Status)
	buddy.DELETE("", adminAuthMiddleware(s.config), s.buddyHandler.Revoke)
	// /self auth via Bearer token (the buddy app's own token), used when the
	// user unpairs from inside the buddy app — symmetric counterpart to the
	// admin DELETE above.
	buddy.DELETE("self", s.buddyHandler.RevokeSelf)
	buddy.GET("ws", s.buddyHandler.WS)
	buddy.POST("command", localOnlyMiddleware(), s.buddyHandler.Command)
	buddy.POST("observe", localOnlyMiddleware(), s.buddyHandler.Observe)
	buddy.POST("suggest", localOnlyMiddleware(), s.buddyHandler.Suggest)
	buddy.POST("exec/:action", localOnlyMiddleware(), s.buddyHandler.Exec)

	agent := api.Group("agent")
	// config-json keeps its stricter `localOnlyMiddleware` (loopback callers
	// only) — admin auth alone is not enough since the raw openclaw.json
	// holds gateway tokens.
	agent.POST("tts/stop", adminAuthMiddleware(s.config), s.agentHandler.StopTTS)
	agent.POST("busy", adminAuthMiddleware(s.config), s.agentHandler.SetBusy)
	// Physical cancel gesture from HAL: loopback-gated, must work without a login.
	agent.POST("speech/cancel", localOnlyMiddleware(), s.agentHandler.CancelSpeechHandler)
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
	// Loopback-only, like the other HAL-initiated endpoints: it must work
	// before/without a login.
	agent.POST("user-reconcile", localOnlyMiddleware(), func(c *gin.Context) {
		s.userReconcile.Reconcile()
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"reconciled": true}))
	})
	// memory/reset: no-SSH recovery for self-written memory that poisoned
	// routing (#421). Admin-only — it deletes learned memory (with a backup).
	agent.POST("memory/reset", adminAuthMiddleware(s.config), s.agentHandler.ResetMemory)
	agent.POST("channel-turn", localOnlyMiddleware(), s.agentHandler.ChannelTurn)
	agent.GET("compaction-latest", adminAuthMiddleware(s.config), s.agentHandler.CompactionLatest)
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
	agent.GET("file", adminAuthMiddleware(s.config), s.agentHandler.ServeFile)

	scheduleGroup := api.Group("schedule")
	scheduleGroup.GET("list", adminAuthMiddleware(s.config), s.deviceMQTTHandler.ListSchedules)
	scheduleGroup.POST(":id/run", adminAuthMiddleware(s.config), s.deviceMQTTHandler.RunScheduleNow)
	// These do NOT write schedules.json directly — each one queues a
	// proposal the backend must confirm (see schedule_crud_handler.go), so
	// the local UI can never arm a task the cloud does not know about.
	scheduleGroup.POST("", adminAuthMiddleware(s.config), s.deviceMQTTHandler.CreateSchedule)
	scheduleGroup.PATCH(":id", adminAuthMiddleware(s.config), s.deviceMQTTHandler.UpdateSchedule)
	scheduleGroup.DELETE(":id", adminAuthMiddleware(s.config), s.deviceMQTTHandler.DeleteSchedule)

	// GET reports connected + the non-secret identity fields (never the
	// token); DELETE removes both the on-disk entry and any
	// mcp.servers.<code> side-effect.
	connectorGroup := api.Group("device/connectors")
	connectorGroup.POST("pat", adminAuthMiddleware(s.config), s.deviceMQTTHandler.SetConnectorPAT)
	connectorGroup.GET(":code", adminAuthMiddleware(s.config), s.deviceMQTTHandler.GetConnector)
	connectorGroup.DELETE(":code", adminAuthMiddleware(s.config), s.deviceMQTTHandler.RemoveConnector)

	api.POST("vision/look", localOnlyMiddleware(), s.lookAndDescribe)
	api.GET("environment/status", localOnlyMiddleware(), s.environmentStatus)

	logs := api.Group("logs")
	logs.GET("tail", adminAuthMiddleware(s.config), s.logTail)
	logs.GET("stream", adminAuthMiddleware(s.config), s.logStream)

	// Replaces direct browser /hw/* access (audit web F5) so nginx /hw/ allow
	// 127.0.0.1; deny all; can stay locked down (audit local F2).
	api.Any("/hardware/*path", adminAuthMiddleware(s.config), s.ambientLEDGate(), gin.WrapH(hardwareProxy))

	// Admin-auth gated; cookie auto-attaches in the iframe context.
	r.GET("/openapi.json", adminAuthMiddleware(s.config), gin.WrapH(openapiProxy))

	slog.Info("server started", "component", "server")

	errChan := make(chan error)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGINT, syscall.SIGTERM)

	srv := &http.Server{
		Addr:    fmt.Sprintf("127.0.0.1:%d", s.config.HttpPort),
		Handler: r,
	}

	s.statusLED.Clear(statusled.StateBooting)

	if !s.config.SetUpCompleted {
		safego.Go("setup-needed-paint", func() { s.waitAndPaintSetupReady(eventCtx) })
	}

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

// registerVoiceMutationRoutes keeps HAL filler access local or authenticated,
// while voice enrollment file deletion always requires administrator access.
func (s *Server) registerVoiceMutationRoutes(api *gin.RouterGroup) {
	api.POST("sensing/filler", adminOrLoopbackAuth(s.config), s.sensingHandler.PlayFiller)
	api.POST("voice/file/remove", adminAuthMiddleware(s.config), s.sensingHandler.RemoveVoiceFile)
}
