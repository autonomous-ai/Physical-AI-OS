package http

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-playground/validator/v10"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
)

// DeviceHandler represents the HTTP handler for device
type DeviceHandler struct {
	service        *device.Service
	networkService *network.Service
	config         *config.Config
}

func ProvideDeviceHandler(ds *device.Service, ns *network.Service, cfg *config.Config) DeviceHandler {
	return DeviceHandler{
		service:        ds,
		networkService: ns,
		config:         cfg,
	}
}

// Setup godoc
//
//	@Summary	setup device
//	@Schemes
//	@Description	setup device
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.SetupRequest		true	"setup request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/setup [post]
func (h *DeviceHandler) Setup(c *gin.Context) {
	var req domain.SetupRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		slog.Warn("setup bind json failed", "component", "device", "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("setup request received", "component", "device",
		"ssid_len", len(req.SSID),
		"wifi_password_len", len(req.Password),
		"admin_password_len", len(req.AdminPassword),
		"llm_api_key_len", len(req.LLMAPIKey),
		"llm_base_url_len", len(req.LLMBaseURL),
		"device_id_len", len(req.DeviceID),
		"channel", req.Channel,
		"set_up_completed", h.config.SetUpCompleted,
		"admin_hash_on_file", h.config.AdminPasswordHash != "",
	)
	// First setup without a password: default to the hardware suffix (sticker /
	// AP SSID); fail 400 rather than fall back to a well-known password.
	if req.AdminPassword == "" && !h.config.SetUpCompleted && h.config.AdminPasswordHash == "" {
		mac := device.GetDeviceMac()
		dash := strings.LastIndex(mac, "-")
		slog.Info("admin_password default: input snapshot", "component", "device",
			"mac", mac,
			"mac_len", len(mac),
			"last_dash_idx", dash,
		)
		if mac == "" || dash < 0 || dash == len(mac)-1 {
			slog.Warn("admin_password default failed — device id unreadable", "component", "device",
				"mac", mac, "reason", "empty mac or malformed dash position")
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"device hardware ID unreadable — cannot default admin_password (set it manually)"))
			return
		}
		req.AdminPassword = mac[dash+1:]
		slog.Info("admin_password defaulted to device suffix", "component", "device",
			"suffix", req.AdminPassword, "suffix_len", len(req.AdminPassword))
	} else {
		slog.Info("admin_password default skipped", "component", "device",
			"has_admin_password_in_req", req.AdminPassword != "",
			"set_up_completed", h.config.SetUpCompleted,
			"admin_hash_on_file", h.config.AdminPasswordHash != "",
		)
	}
	// Fill omitted secrets from this device's own config (only empty slots).
	// Not gated on SetUpCompleted so a retry after a failed Wi-Fi step validates.
	mergeMissingFromConfig(&req, h.config)
	if err := validator.New().Struct(req); err != nil {
		slog.Warn("setup validator failed", "component", "device", "error", err.Error(),
			"ssid_set", req.SSID != "", "password_set", req.Password != "",
			"llm_api_key_set", req.LLMAPIKey != "", "llm_base_url_set", req.LLMBaseURL != "",
			"device_id_set", req.DeviceID != "")
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	// No race: the cookie validates against SessionSecret, not the password
	// hash that service.Setup persists asynchronously.
	if req.AdminPassword != "" {
		if err := session.Issue(c, h.config); err != nil {
			slog.Warn("setup: issue session failed", "component", "device", "error", err)
		}
	}

	go func() {
		time.Sleep(2 * time.Second)
		if err := h.service.Setup(req); err != nil {
			slog.Error("setup failed", "component", "device", "error", err,
				"setup_failure_reason", device.SetupFailureReason(err))
			if device.SetupFailureReason(err) != device.FailureSetupRuntime {
				h.networkService.SwitchToAPMode()
			}
			return
		}

		slog.Info("setup success", "component", "device")
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// WifiProvision godoc
//
//	@Summary	provision Wi-Fi only (AP-portal fast path)
//	@Description	Dedicated re-provisioning endpoint for a device that is already
//	@Description	fully configured but has been moved to a new Wi-Fi network.
//	@Description	Body is minimal ({ssid, password}); no LLM/channel/device_id
//	@Description	fields are touched. Runs the connect-wifi script + AP teardown.
//	@Description	Gated by apOnlyMiddleware (source IP must be in the AP subnet).
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.WifiProvisionRequest	true	"wifi credentials"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/wifi-provision [post]
func (h *DeviceHandler) WifiProvision(c *gin.Context) {
	var req domain.WifiProvisionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		slog.Warn("wifi-provision bind json failed", "component", "device", "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		slog.Warn("wifi-provision validator failed", "component", "device", "error", err.Error())
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("wifi-provision request", "component", "device",
		"ssid_len", len(req.SSID),
		"password_len", len(req.Password),
		"llm_api_key_supplied", req.LLMAPIKey != "",
		"llm_api_key_len", len(req.LLMAPIKey),
		"llm_base_url_supplied", req.LLMBaseURL != "",
		"llm_base_url", req.LLMBaseURL, // safe to log — not a secret
		"llm_model", req.LLMModel,
		"admin_password_supplied", req.AdminPassword != "",
		"set_up_completed", h.config.SetUpCompleted,
	)

	// A fresh device MUST supply an LLM triplet, or it joins Wi-Fi with no brain.
	if !h.config.SetUpCompleted {
		var missing []string
		if req.LLMAPIKey == "" {
			missing = append(missing, "llm_api_key")
		}
		if req.LLMBaseURL == "" {
			missing = append(missing, "llm_base_url")
		}
		if req.LLMModel == "" {
			missing = append(missing, "llm_model")
		}
		if len(missing) > 0 {
			slog.Warn("wifi-provision: fresh device missing required LLM fields",
				"component", "device", "missing", missing)
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"fresh device requires: "+strings.Join(missing, ", ")))
			return
		}
	}

	// First setup without a password: default to the hardware suffix (sticker /
	// AP SSID); fail 400 rather than fall back to a well-known password.
	if req.AdminPassword == "" && !h.config.SetUpCompleted && h.config.AdminPasswordHash == "" {
		mac := device.GetDeviceMac()
		dash := strings.LastIndex(mac, "-")
		if mac == "" || dash < 0 || dash == len(mac)-1 {
			slog.Warn("wifi-provision: admin_password default failed", "component", "device", "mac", mac)
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"device hardware ID unreadable — cannot default admin_password (set it manually)"))
			return
		}
		req.AdminPassword = mac[dash+1:]
	}
	if req.AdminPassword != "" {
		if err := session.Issue(c, h.config); err != nil {
			slog.Warn("wifi-provision: issue session failed", "component", "device", "error", err)
		}
	}

	go func() {
		time.Sleep(2 * time.Second)
		if err := h.service.ReprovisionWifi(req); err != nil {
			slog.Error("wifi-provision failed", "component", "device", "error", err)
			h.networkService.SwitchToAPMode()
			return
		}
		slog.Info("wifi-provision success", "component", "device")
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// GetConfig godoc
//
//	@Summary	get current device config (sanitized)
//	@Schemes
//	@Description	get current device config. Secrets (API keys, channel
//	@Description	tokens, passwords) are returned as Has* booleans only —
//	@Description	plaintext values never leave the device. Use PUT
//	@Description	/api/device/config to update individual secret fields.
//	@Tags			device
//	@Success		200	{object}	serializers.ResponseSuccess
//	@Router			/device/config [get]
func (h *DeviceHandler) GetConfig(c *gin.Context) {
	cfg := h.service.GetPublicConfig()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(cfg))
}

// SetupStatus godoc
//
//	@Summary	current setup phase + LAN IP
//	@Description	web polls this during the AP→STA transition to learn the
//	@Description	device's new LAN IP and redirect the user. Phase progresses
//	@Description	idle → connecting → connected (or failed).
//	@Tags			device
//	@Success		200	{object}	serializers.ResponseSuccess
//	@Router			/device/setup/status [get]
func (h *DeviceHandler) SetupStatus(c *gin.Context) {
	phase, lanIP, errMsg, run := h.service.SetupStatus()
	runtimePhase, runtimeError := h.service.SetupRuntimeStatus()
	if runtimeError != "" {
		errMsg = runtimeError
	}
	// The web client uses it to auto-redirect 192.168.100.1 →
	// <device_type>-xxxx.local even before the operator is authed, since
	// /api/device/config requires admin auth and fresh devices have none.
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"phase":  phase,
		"lan_ip": lanIP,
		"error":  errMsg,
		"mac":    device.GetDeviceMac(),
		// Setup runs since boot: lets the client tell its own verdict from a leftover.
		"run": run,
		// Not a secret; the endpoint stays open because an unset-up device has
		// no admin password.
		"set_up_completed": h.service.SetupCompleted(),
		"runtime_phase":    runtimePhase,
	}))
}

// UpdateConfig godoc
//
//	@Summary	update device config
//	@Schemes
//	@Description	update device config fields (all optional; saves to disk, restart os-server for full effect)
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.UpdateConfigRequest	true	"update config request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/config [put]
func (h *DeviceHandler) UpdateConfig(c *gin.Context) {
	var req domain.UpdateConfigRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.UpdateConfig(req); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// GetVoices returns the list of available TTS voices for the requested
// provider.
func (h *DeviceHandler) GetVoices(c *gin.Context) {
	provider := c.DefaultQuery("provider", domain.TTSProviderOpenAI)
	lang := c.Query("lang")

	voices, err := hal.ListVoices(provider, lang)
	if err == nil && len(voices) > 0 {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
		return
	}
	// Error, not an empty list: the web would treat empty as authoritative and
	// never refetch.
	if provider == domain.TTSProviderPiper {
		if err != nil {
			c.JSON(http.StatusServiceUnavailable,
				serializers.ResponseError("hal unreachable: "+err.Error()))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
		return
	}
	staticVoices, ok := domain.TTSVoicesByProvider[provider]
	if !ok {
		staticVoices = domain.TTSVoices
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(staticVoices))
}

// GetTTSProviders returns the list of supported TTS providers.
func (h *DeviceHandler) GetTTSProviders(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.TTSProviders))
}

// GetRealtimeOptions returns the valid realtime providers + per-provider voice /
// reasoning lists, so the web never hardcodes them (single source = config).
func (h *DeviceHandler) GetRealtimeOptions(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(config.GetRealtimeOptions()))
}

// GetAgentRuntime returns the active agentic backend + selectable options for
// the web settings dropdown.
//
//	@Router	/device/agent-runtime [get]
func (h *DeviceHandler) GetAgentRuntime(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.AgentRuntimeStatus{
		Current:     h.service.CurrentAgentRuntime(),
		Options:     domain.AgentRuntimes,
		Ready:       h.service.AgentReady(),
		RemoteURL:   h.config.AgentRemoteURL,
		RemoteToken: h.config.AgentRemoteToken,
	}))
}

// SetAgentRuntime validates synchronously (400), then switches the agentic
// backend in the background and returns 200 "accepted"; os-server restarts
// on success, so the client should re-poll GetAgentRuntime.
//
//	@Router	/device/agent-runtime [post]
func (h *DeviceHandler) SetAgentRuntime(c *gin.Context) {
	var req domain.AgentRuntimeSetData
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if !domain.IsValidAgentRuntime(req.Runtime) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(
			fmt.Sprintf("invalid runtime %q (want %s)", req.Runtime, strings.Join(domain.AgentRuntimes, "|"))))
		return
	}
	if strings.ToLower(strings.TrimSpace(req.Runtime)) == domain.AgentRuntimeRemote {
		url := strings.TrimSpace(req.URL)
		if url == "" {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("remote gateway URL is required"))
			return
		}
		if !strings.HasPrefix(url, "http://") && !strings.HasPrefix(url, "https://") {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("remote gateway URL must start with http:// or https://"))
			return
		}
		token := strings.TrimSpace(req.Token)
		if err := h.config.WithLockSave(func(cfg *config.Config) {
			cfg.AgentRemoteURL = url
			cfg.AgentRemoteToken = token
			cfg.AgentRuntime = domain.AgentRuntimeRemote
		}); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
			return
		}
		go func() {
			if rerr := h.service.RestartForAgentRuntime(); rerr != nil {
				slog.Error("agent-runtime os-server restart failed (remote)", "component", "device-http", "error", rerr)
			}
		}()
		c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
		return
	}
	run, err := h.service.ReserveAgentRuntimeSwitchReady(req)
	if err != nil {
		if errors.Is(err, device.ErrAgentRuntimeSwitchInProgress) {
			c.JSON(http.StatusConflict, serializers.ResponseError("agent runtime switch already in progress"))
			return
		}
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	go func() {
		switched, err := run()
		if err != nil {
			slog.Error("agent-runtime switch failed", "component", "device-http", "error", err)
			return
		}
		if switched {
			if rerr := h.service.RestartForAgentRuntime(); rerr != nil {
				slog.Error("agent-runtime os-server restart failed", "component", "device-http", "error", rerr)
			}
		}
	}()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// GetTimezone returns the device's current IANA timezone plus the selectable
// zone list (from the system tzdata) for the web Settings picker.
//
//	@Router	/device/timezone [get]
func (h *DeviceHandler) GetTimezone(c *gin.Context) {
	current, zones := h.service.GetTimezone()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.TimezoneStatus{
		Current: current,
		Zones:   zones,
	}))
}

// SetTimezone applies an IANA timezone (e.g. "Asia/Ho_Chi_Minh"); an unknown
// zone returns 400.
//
//	@Router	/device/timezone [post]
//
// RestoreDefaults puts one settings section ("llm" | "voice" | "realtime")
// back on the shipped credentials.
func (h *DeviceHandler) RestoreDefaults(c *gin.Context) {
	var req struct {
		Section string `json:"section" validate:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.RestoreAutonomousDefaults(req.Section); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

func (h *DeviceHandler) SetTimezone(c *gin.Context) {
	var req domain.TimezoneSetData
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.SetTimezone(req.Timezone); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// ChangeChannel godoc
//
//	@Summary	change messaging channel
//	@Schemes
//	@Description	change messaging channel (telegram/slack/discord) without full device re-setup
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.ChangeChannelRequest	true	"change channel request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/channel [post]
func (h *DeviceHandler) ChangeChannel(c *gin.Context) {
	var req domain.AddChannelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := req.ValidateChannel(); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if req.EffectiveChannel() == domain.ChannelWhatsapp {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("whatsapp pairing not supported via HTTP; use MQTT add_channel"))
		return
	}
	if !h.service.SupportsChannel(req.EffectiveChannel()) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(req.EffectiveChannel()+" not supported on the active runtime"))
		return
	}

	go func() {
		if _, err := h.service.AddChannel(context.Background(), req); err != nil {
			slog.Error("add channel failed", "component", "device", "error", err)
			return
		}
		slog.Info("add channel success", "component", "device")
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// mergeMissingFromConfig fills empty SetupRequest fields with the values
// already saved in config.json.
func mergeMissingFromConfig(req *domain.SetupRequest, cfg *config.Config) {
	if req.SSID == "" {
		req.SSID = cfg.NetworkSSID
	}
	if req.Password == "" {
		req.Password = cfg.NetworkPassword
	}
	if req.LLMAPIKey == "" {
		req.LLMAPIKey = cfg.LLMAPIKey
	}
	if req.LLMBaseURL == "" {
		req.LLMBaseURL = cfg.LLMBaseURL
	}
	if req.LLMModel == "" {
		req.LLMModel = cfg.LLMModel
	}
	if req.DeviceID == "" {
		req.DeviceID = cfg.DeviceID
	}
	if req.Channel == "" {
		req.Channel = cfg.Channel
	}
	if req.TelegramBotToken == "" {
		req.TelegramBotToken = cfg.TelegramBotToken
	}
	if req.TelegramUserID == "" {
		req.TelegramUserID = cfg.TelegramUserID
	}
	if req.SlackBotToken == "" {
		req.SlackBotToken = cfg.SlackBotToken
	}
	if req.SlackAppToken == "" {
		req.SlackAppToken = cfg.SlackAppToken
	}
	if req.SlackUserID == "" {
		req.SlackUserID = cfg.SlackUserID
	}
	if req.DiscordBotToken == "" {
		req.DiscordBotToken = cfg.DiscordBotToken
	}
	if req.DiscordGuildID == "" {
		req.DiscordGuildID = cfg.DiscordGuildID
	}
	if req.DiscordUserID == "" {
		req.DiscordUserID = cfg.DiscordUserID
	}
	if req.DeepgramAPIKey == "" {
		req.DeepgramAPIKey = cfg.DeepgramAPIKey
	}
	if req.STTAPIKey == "" {
		req.STTAPIKey = cfg.STTAPIKey
	}
	if req.TTSAPIKey == "" {
		req.TTSAPIKey = cfg.TTSAPIKey
	}
	if req.STTBaseURL == "" {
		req.STTBaseURL = cfg.STTBaseURL
	}
	if req.TTSBaseURL == "" {
		req.TTSBaseURL = cfg.TTSBaseURL
	}
	if req.STTLanguage == "" {
		req.STTLanguage = cfg.STTLanguage
	}
	if req.TTSProvider == "" {
		req.TTSProvider = cfg.TTSProvider
	}
	if req.TTSVoice == "" {
		req.TTSVoice = cfg.TTSVoice
	}
	if req.MQTTEndpoint == "" {
		req.MQTTEndpoint = cfg.MQTTEndpoint
	}
	if req.MQTTUsername == "" {
		req.MQTTUsername = cfg.MQTTUsername
	}
	if req.MQTTPassword == "" {
		req.MQTTPassword = cfg.MQTTPassword
	}
	if req.MQTTPort == 0 {
		req.MQTTPort = cfg.MQTTPort
	}
	if req.FAChannel == "" {
		req.FAChannel = cfg.FAChannel
	}
	if req.FDChannel == "" {
		req.FDChannel = cfg.FDChannel
	}
}

// ListMCPTools returns the configured remote MCP tools.
func (h *DeviceHandler) ListMCPTools(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(h.service.ListMCPTools()))
}

// AddMCPTool adds a remote MCP tool endpoint.
func (h *DeviceHandler) AddMCPTool(c *gin.Context) {
	var req config.MCPTool
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.AddMCPTool(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// RemoveMCPTool removes a remote MCP tool by name.
func (h *DeviceHandler) RemoveMCPTool(c *gin.Context) {
	name := c.Param("name")
	if err := h.service.RemoveMCPTool(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}
