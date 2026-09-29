package device

import (
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/statusled"
)

// Setup phases exposed via /api/setup/status; they only move forward.
const (
	SetupPhaseIdle       = "idle"
	SetupPhaseConnecting = "connecting"
	SetupPhaseConnected  = "connected"
	SetupPhaseFailed     = "failed"
)

// apSetupIP is wlan0's static provisioning-AP address; never publish it as the LAN IP.
const apSetupIP = "192.168.100.1"

type setupState struct {
	mu    sync.RWMutex
	phase string
	lanIP string
	error string
	// run counts Setup() invocations so the client can identify its own run.
	run int
}

func (st *setupState) snapshot() (phase, ip, errMsg string, run int) {
	st.mu.RLock()
	defer st.mu.RUnlock()
	return st.phase, st.lanIP, st.error, st.run
}

// begin starts a new run at "connecting" with no IP or error.
func (st *setupState) begin() {
	st.mu.Lock()
	st.run++
	st.phase = SetupPhaseConnecting
	st.lanIP = ""
	st.error = ""
	st.mu.Unlock()
}

func (st *setupState) set(phase, ip, errMsg string) {
	st.mu.Lock()
	st.phase = phase
	st.lanIP = ip
	st.error = errMsg
	st.mu.Unlock()
}

// SetupStatus returns the Setup phase and LAN IP, falling back to the live
// default-route address when no run has published one.
func (s *Service) SetupStatus() (phase, lanIP, errMsg string, run int) {
	phase, lanIP, errMsg, run = s.setupState.snapshot()
	// Never report apSetupIP: it dies when the AP tears down.
	if lanIP == "" || lanIP == apSetupIP {
		if ip, err := s.networkService.GetCurrentIP(); err == nil && ip != apSetupIP {
			lanIP = ip
		} else {
			lanIP = ""
		}
	}
	return phase, lanIP, errMsg, run
}

// setupWired completes the network phase over an existing uplink (ethernet):
// verifies internet, then tears down the provisioning AP.
func (s *Service) setupWired() error {
	if _, err := s.networkService.CheckInternet(); err != nil {
		const msg = "no WiFi credentials given and the device has no working internet connection"
		s.setupState.set(SetupPhaseFailed, "", msg)
		return fmt.Errorf("%s: %w", msg, err)
	}

	// Publish the address before leaving AP mode, which can drop the client connection.
	ip, ipErr := s.networkService.GetCurrentIP()
	if ipErr != nil || ip == apSetupIP {
		slog.Warn("setup: wired path could not resolve a LAN IP", "component", "device", "ip", ip, "error", ipErr)
		ip = ""
	}
	s.setupState.set(SetupPhaseConnected, ip, "")
	slog.Info("setup: existing uplink verified, skipping WiFi join", "component", "device", "lan_ip", ip)

	if err := s.networkService.LeaveAPMode(); err != nil {
		// Non-fatal, but error level: a surviving AP is an open hotspot.
		slog.Error("setup: failed to tear down provisioning AP", "component", "device", "error", err)
	}
	return nil
}

// setupWiFi joins the given Wi-Fi and publishes the STA address before the AP disappears.
func (s *Service) setupWiFi(data domain.SetupRequest) error {
	s.statusLED.Set(statusled.StateWifiConnecting)

	// SetupNetwork blocks up to 60s but the AP dies ~2s after the switch, so
	// publish the STA IP as soon as it appears (phase stays "connecting").
	ipPollDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ipPollDone:
				return
			case <-ticker.C:
				ip, ipErr := s.networkService.GetCurrentIP()
				if ipErr == nil && ip != "" && ip != apSetupIP {
					if _, prevIP, _, _ := s.setupState.snapshot(); prevIP != ip {
						s.setupState.set(SetupPhaseConnecting, ip, "")
						slog.Info("setup: early LAN IP captured", "component", "device", "lan_ip", ip)
					}
				}
			}
		}
	}()

	result, err := s.networkService.SetupNetwork(data.SSID, data.Password)
	close(ipPollDone)
	if err != nil {
		s.setupState.set(SetupPhaseFailed, "", err.Error())
		return fmt.Errorf("setup network: %w", err)
	}
	if !result {
		s.setupState.set(SetupPhaseFailed, "", "network setup failed")
		return fmt.Errorf("network setup failed")
	}
	// Keep the early-captured IP if this read fails during AP teardown.
	ip, ipErr := s.networkService.GetCurrentIP()
	if ipErr != nil || ip == "" || ip == apSetupIP {
		_, prevIP, _, _ := s.setupState.snapshot()
		ip = prevIP
	}
	if ip != "" {
		s.setupState.set(SetupPhaseConnected, ip, "")
		slog.Info("setup: WiFi associated", "component", "device", "lan_ip", ip)
	} else {
		s.setupState.set(SetupPhaseConnected, "", "")
		slog.Warn("setup: WiFi associated but no IP detected", "component", "device", "error", ipErr)
	}
	return nil
}

// ReprovisionWifi joins Wi-Fi (SSID required) and applies only the non-empty
// config fields; agent setup runs only when an LLM key is available.
func (s *Service) ReprovisionWifi(data domain.WifiProvisionRequest) error {
	slog.Info("starting wifi reprovision", "component", "device", "ssid", data.SSID)
	s.setupState.begin()
	defer s.statusLED.Clear(statusled.StateWifiConnecting)

	if strings.TrimSpace(data.SSID) == "" {
		err := fmt.Errorf("ssid is required")
		s.setupState.set(SetupPhaseFailed, "", err.Error())
		return err
	}

	s.statusLED.Set(statusled.StateWifiConnecting)

	// Early LAN-IP poller, as in setupWiFi.
	ipPollDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ipPollDone:
				return
			case <-ticker.C:
				ip, ipErr := s.networkService.GetCurrentIP()
				if ipErr == nil && ip != "" && ip != apSetupIP {
					if _, prevIP, _, _ := s.setupState.snapshot(); prevIP != ip {
						s.setupState.set(SetupPhaseConnecting, ip, "")
						slog.Info("wifi reprovision: early LAN IP captured",
							"component", "device", "lan_ip", ip)
					}
				}
			}
		}
	}()

	ok, err := s.networkService.SetupNetwork(data.SSID, data.Password)
	close(ipPollDone)
	if err != nil {
		s.setupState.set(SetupPhaseFailed, "", err.Error())
		return fmt.Errorf("reprovision wifi: %w", err)
	}
	if !ok {
		s.setupState.set(SetupPhaseFailed, "", "network setup failed")
		return fmt.Errorf("reprovision wifi: network setup failed")
	}

	ip, ipErr := s.networkService.GetCurrentIP()
	if ipErr != nil || ip == "" || ip == apSetupIP {
		_, prevIP, _, _ := s.setupState.snapshot()
		ip = prevIP
	}
	s.setupState.set(SetupPhaseConnected, ip, "")
	slog.Info("wifi reprovision: joined home wifi", "component", "device", "lan_ip", ip)

	// Optional config edits: empty keeps the on-disk value.
	if v := strings.TrimSpace(data.LLMAPIKey); v != "" {
		s.config.LLMAPIKey = v
	}
	if v := strings.TrimSpace(data.LLMBaseURL); v != "" {
		s.config.LLMBaseURL = urlnorm.NormalizeBaseURL(v)
	}
	if v := strings.TrimSpace(data.LLMModel); v != "" {
		s.config.LLMModel = v
	}
	if v := strings.TrimSpace(data.DeepgramAPIKey); v != "" {
		s.config.DeepgramAPIKey = v
	}
	if v := strings.TrimSpace(data.STTAPIKey); v != "" {
		s.config.STTAPIKey = v
	}
	if v := strings.TrimSpace(data.STTBaseURL); v != "" {
		s.config.STTBaseURL = urlnorm.NormalizeBaseURL(v)
	}
	if v := strings.TrimSpace(data.STTLanguage); v != "" {
		s.config.STTLanguage = v
		s.config.STTModel = sttModelForLanguage(v)
	}
	if v := strings.TrimSpace(data.TTSAPIKey); v != "" {
		s.config.TTSAPIKey = v
	}
	if v := strings.TrimSpace(data.TTSBaseURL); v != "" {
		s.config.TTSBaseURL = urlnorm.NormalizeBaseURL(v)
	}
	if v := strings.TrimSpace(data.TTSProvider); v != "" {
		s.config.TTSProvider = v
	}
	if v := strings.TrimSpace(data.TTSVoice); v != "" {
		s.config.TTSVoice = v
	}
	if data.AdminPassword != "" {
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(data.AdminPassword), bcrypt.DefaultCost)
		if hashErr != nil {
			return fmt.Errorf("hash admin password: %w", hashErr)
		}
		s.config.AdminPasswordHash = string(hash)
	}

	s.applyWifiProvisionChannel(data)

	if err := s.config.Save(); err != nil {
		slog.Error("wifi reprovision: save config failed", "component", "device", "error", err)
	}

	if s.config.LLMAPIKey != "" {
		// Snapshot before SetupAgent, which may overwrite LLMModel with the upstream default.
		operatorModel := strings.TrimSpace(data.LLMModel)

		setupData := s.wifiProvisionSetupRequest()
		if err := s.agentGateway.SetupAgent(setupData); err != nil {
			slog.Warn("wifi reprovision: agent setup failed", "component", "device", "error", err)
			return nil // Wi-Fi is up; surface the agent failure via /monitor
		}

		if operatorModel != "" && s.config.LLMModel != operatorModel {
			slog.Info("wifi reprovision: restoring operator's model over upstream default",
				"component", "device",
				"operator_model", operatorModel,
				"upstream_default", s.config.LLMModel)
			s.config.LLMModel = operatorModel
			if err := s.config.Save(); err != nil {
				slog.Error("wifi reprovision: save operator model failed", "component", "device", "error", err)
			}
			if err := s.agentGateway.UpdatePrimaryModel(operatorModel); err != nil {
				if !errors.Is(err, domain.ErrNotSupportedByRuntime) {
					slog.Warn("wifi reprovision: push operator model to gateway failed",
						"component", "device", "error", err)
				}
			}
		}

		if ok := s.WaitForAgentReady(120 * time.Second); !ok {
			slog.Warn("wifi reprovision: agent ready timeout", "component", "device")
			return nil
		}
		s.config.SetUpCompleted = true
		if err := s.config.Save(); err != nil {
			slog.Error("wifi reprovision: save SetUpCompleted failed", "component", "device", "error", err)
		}
		slog.Info("wifi reprovision: agent ready + SetUpCompleted", "component", "device")
	}

	return nil
}

func (s *Service) Setup(data domain.SetupRequest) error {
	slog.Info("starting setup", "component", "device")
	data.LLMBaseURL = urlnorm.NormalizeBaseURL(data.LLMBaseURL)
	data.STTBaseURL = urlnorm.NormalizeBaseURL(data.STTBaseURL)
	data.TTSBaseURL = urlnorm.NormalizeBaseURL(data.TTSBaseURL)
	s.setupState.begin()

	defer s.statusLED.Clear(statusled.StateWifiConnecting)

	// An empty SSID means an existing (wired) uplink.
	if strings.TrimSpace(data.SSID) == "" {
		if err := s.setupWired(); err != nil {
			return err
		}
	} else if err := s.setupWiFi(data); err != nil {
		return err
	}

	// SetupAgent falls back to this model when the model API is unreachable.
	s.config.LLMModel = data.LLMModel

	llmAPIKey := data.LLMAPIKey
	llmBaseURL := data.LLMBaseURL

	s.config.LLMAPIKey = llmAPIKey
	s.config.LLMBaseURL = llmBaseURL
	s.applySetupChannel(data)
	s.config.DeviceID = data.DeviceID
	s.config.DeepgramAPIKey = data.DeepgramAPIKey
	s.config.STTAPIKey = data.STTAPIKey
	s.config.TTSAPIKey = data.TTSAPIKey
	s.config.STTBaseURL = data.STTBaseURL
	s.config.TTSBaseURL = data.TTSBaseURL
	s.config.STTLanguage = data.STTLanguage
	s.config.STTModel = sttModelForLanguage(data.STTLanguage)
	if data.TTSProvider != "" {
		s.config.TTSProvider = data.TTSProvider
	}
	if data.TTSVoice != "" {
		s.config.TTSVoice = data.TTSVoice
	}
	s.config.MQTTEndpoint = data.MQTTEndpoint
	s.config.MQTTUsername = data.MQTTUsername
	s.config.MQTTPassword = data.MQTTPassword
	s.config.MQTTPort = data.MQTTPort
	s.config.FAChannel = data.FAChannel
	s.config.FDChannel = data.FDChannel
	if data.LLMDisableThinking != nil {
		s.config.LLMDisableThinking = data.LLMDisableThinking
	}
	// Never persisted in plaintext; empty is allowed for older clients.
	if data.AdminPassword != "" {
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(data.AdminPassword), bcrypt.DefaultCost)
		if hashErr != nil {
			return fmt.Errorf("hash admin password: %w", hashErr)
		}
		s.config.AdminPasswordHash = string(hash)
	}
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}
	slog.Info("config saved", "component", "device")

	// Early ping publishes the STA IP for the setup redirect rescue; must run
	// after config.Save, which sets the backend URL.
	if key := s.config.BackendKey(); s.beClient != nil && key != "" {
		go func() { s.beClient.PingSafe(key, s.buildPingPayload("setting_up")) }()
	}

	// Run after config.json is saved: Hermes presync reads it.
	if err := s.agentGateway.SetupAgent(data); err != nil {
		return err
	}

	if ok := s.WaitForAgentReady(120 * time.Second); !ok {
		return fmt.Errorf("agent gateway ready timeout, something went wrong")
	}

	s.config.SetUpCompleted = true
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}
	// Discard the white AP cue before the deferred clear calls /led/restore.
	hal.ResetLEDToResting()

	slog.Info("agent gateway is ready", "component", "device")
	if key := s.config.BackendKey(); s.beClient != nil && key != "" {
		s.beClient.PingSafe(key, s.buildPingPayload("working"))
	}
	return nil
}

// applySetupChannel stores credentials for the channel selected during full setup.
func (s *Service) applySetupChannel(data domain.SetupRequest) {
	s.config.Channel = data.EffectiveChannel()
	switch s.config.Channel {
	case "slack":
		s.config.SlackBotToken = data.SlackBotToken
		s.config.SlackAppToken = data.SlackAppToken
		s.config.SlackUserID = data.SlackUserID
	case "discord":
		s.config.DiscordBotToken = data.DiscordBotToken
		s.config.DiscordUserID = data.DiscordUserID
	case domain.ChannelIMessage:
		s.config.BluebubblesServerURL = data.BluebubblesServerURL
		s.config.BluebubblesPassword = data.BluebubblesPassword
		s.config.BluebubblesUserAddress = data.BluebubblesUserAddress
	default:
		s.config.TelegramBotToken = data.TelegramBotToken
		s.config.TelegramUserID = data.TelegramUserID
	}
}

// applyWifiProvisionChannel preserves omitted credentials during Wi-Fi reprovisioning.
func (s *Service) applyWifiProvisionChannel(data domain.WifiProvisionRequest) {
	// Apply channel first; tokens are written only for the new channel.
	if v := strings.TrimSpace(data.Channel); v != "" {
		s.config.Channel = v
	}
	switch s.config.Channel {
	case domain.ChannelTelegram, "":
		if v := strings.TrimSpace(data.TelegramBotToken); v != "" {
			s.config.TelegramBotToken = v
		}
		if v := strings.TrimSpace(data.TelegramUserID); v != "" {
			s.config.TelegramUserID = v
		}
	case domain.ChannelSlack:
		if v := strings.TrimSpace(data.SlackBotToken); v != "" {
			s.config.SlackBotToken = v
		}
		if v := strings.TrimSpace(data.SlackAppToken); v != "" {
			s.config.SlackAppToken = v
		}
		if v := strings.TrimSpace(data.SlackUserID); v != "" {
			s.config.SlackUserID = v
		}
	case domain.ChannelDiscord:
		if v := strings.TrimSpace(data.DiscordBotToken); v != "" {
			s.config.DiscordBotToken = v
		}
		if v := strings.TrimSpace(data.DiscordUserID); v != "" {
			s.config.DiscordUserID = v
		}
	case domain.ChannelIMessage:
		if v := strings.TrimSpace(data.BluebubblesServerURL); v != "" {
			s.config.BluebubblesServerURL = v
		}
		if v := strings.TrimSpace(data.BluebubblesPassword); v != "" {
			s.config.BluebubblesPassword = v
		}
		if v := strings.TrimSpace(data.BluebubblesUserAddress); v != "" {
			s.config.BluebubblesUserAddress = v
		}
	}
}

// wifiProvisionSetupRequest forwards persisted settings to the agent runtime.
func (s *Service) wifiProvisionSetupRequest() domain.SetupRequest {
	return domain.SetupRequest{
		LLMAPIKey:  s.config.LLMAPIKey,
		LLMBaseURL: s.config.LLMBaseURL,
		LLMModel:   s.config.LLMModel,
		DeviceID:   s.config.DeviceID,
		// Channel identity + tokens for the first SetupAgent after a channel change.
		Channel:                s.config.Channel,
		TelegramBotToken:       s.config.TelegramBotToken,
		TelegramUserID:         s.config.TelegramUserID,
		SlackBotToken:          s.config.SlackBotToken,
		SlackAppToken:          s.config.SlackAppToken,
		SlackUserID:            s.config.SlackUserID,
		DiscordBotToken:        s.config.DiscordBotToken,
		DiscordUserID:          s.config.DiscordUserID,
		BluebubblesServerURL:   s.config.BluebubblesServerURL,
		BluebubblesPassword:    s.config.BluebubblesPassword,
		BluebubblesUserAddress: s.config.BluebubblesUserAddress,
	}
}
