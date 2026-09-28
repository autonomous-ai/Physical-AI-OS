package device

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/server/config"
)

// baseConfig returns a config prefilled the way a set-up device looks, so
// PATCH-semantics tests can assert that untouched fields survive.
func baseConfig() *config.Config {
	return &config.Config{
		LLMAPIKey:         "key-llm",
		LLMBaseURL:        "https://api.example.com",
		LLMModel:          "model-a",
		DeepgramAPIKey:    "key-dg",
		STTAPIKey:         "key-stt",
		TTSAPIKey:         "key-tts",
		STTBaseURL:        "https://stt.example.com",
		TTSBaseURL:        "https://tts.example.com",
		STTLanguage:       "en",
		STTModel:          "flux-general-en",
		TTSProvider:       "openai",
		TTSVoice:          "alloy",
		NetworkSSID:       "home-wifi",
		NetworkPassword:   "wifi-pass",
		Channel:           domain.ChannelTelegram,
		TelegramBotToken:  "tg-token",
		TelegramUserID:    "tg-user",
		MQTTEndpoint:      "mqtt.example.com",
		AdminPasswordHash: "old-hash",
	}
}

func TestApplyUpdateEmptyRequestChangesNothing(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{}, "")

	if ch.model || ch.thinking || ch.baseURL || ch.wifi || ch.lang || ch.halBoot || ch.realtime || ch.channel {
		t.Fatalf("empty request set flags: %+v", ch)
	}
	if c.LLMAPIKey != "key-llm" || c.TTSVoice != "alloy" || c.TelegramBotToken != "tg-token" ||
		c.NetworkSSID != "home-wifi" || c.AdminPasswordHash != "old-hash" {
		t.Fatalf("empty request mutated config: %+v", c)
	}
}

func TestApplyUpdateModelOnly(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMModel: "model-b"}, "")

	if !ch.model || ch.newModel != "model-b" {
		t.Fatalf("model change not flagged: %+v", ch)
	}
	// llm_model is not in hal's boot-read set — must not bounce TTS.
	if ch.halBoot || ch.thinking || ch.baseURL {
		t.Fatalf("model-only change set unrelated flags: %+v", ch)
	}

	// Re-sending the active model is a no-op.
	ch = applyUpdate(c, domain.UpdateConfigRequest{LLMModel: "model-b"}, "")
	if ch.model {
		t.Fatal("re-sending current model flagged as change")
	}
}

func TestApplyUpdateLLMKeyTriggersHALRestart(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMAPIKey: "key-new"}, "")
	if !ch.halBoot {
		t.Fatal("llm_api_key change must flag halBoot (hal reads it at boot)")
	}
	if ch.model || ch.baseURL {
		t.Fatalf("llm_api_key change set unrelated flags: %+v", ch)
	}
}

func TestApplyUpdateBaseURL(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMBaseURL: "https://api2.example.com"}, "")
	if !ch.baseURL || !ch.halBoot {
		t.Fatalf("base_url change not flagged: %+v", ch)
	}
	// Re-sending the same URL (post-normalization) is a no-op.
	ch = applyUpdate(c, domain.UpdateConfigRequest{LLMBaseURL: "https://api2.example.com"}, "")
	if ch.baseURL || ch.halBoot {
		t.Fatalf("identical base_url flagged as change: %+v", ch)
	}
}

func TestApplyUpdateThinkingFlagFollowsPresence(t *testing.T) {
	c := baseConfig()
	v := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{LLMDisableThinking: &v}, "")
	if !ch.thinking {
		t.Fatal("thinking pointer sent but not flagged")
	}
	if c.LLMDisableThinking == nil || !*c.LLMDisableThinking {
		t.Fatal("thinking value not persisted")
	}
}

func TestApplyUpdateLanguageDerivesModel(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{STTLanguage: "vi"}, "")
	if !ch.lang || ch.prevLang != "en" || ch.newLang != "vi" {
		t.Fatalf("language change not flagged: %+v", ch)
	}
	if c.STTModel != "nova-3-general" {
		t.Fatalf("stt model not derived: %s", c.STTModel)
	}
	// Same language again → no session reset, no hal restart.
	ch = applyUpdate(c, domain.UpdateConfigRequest{STTLanguage: "vi"}, "")
	if ch.lang {
		t.Fatal("re-sending current language flagged as change")
	}
}

func TestApplyUpdateWifi(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{SSID: "new-wifi", Password: "new-pass"}, "")
	if !ch.wifi || ch.newSSID != "new-wifi" || ch.newPassword != "new-pass" {
		t.Fatalf("wifi change not captured: %+v", ch)
	}
	// Same SSID → no reconnect, even with a password present.
	ch = applyUpdate(c, domain.UpdateConfigRequest{SSID: "new-wifi", Password: "other-pass"}, "")
	if ch.wifi {
		t.Fatal("same-ssid save flagged wifi reconnect")
	}
	if c.NetworkPassword != "other-pass" {
		t.Fatal("password not persisted on same-ssid save")
	}
}

func TestApplyUpdateChannelTokens(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{TelegramBotToken: "tg-token-2"}, "")
	if !ch.channel {
		t.Fatal("token change not flagged for gateway re-push")
	}
	// chanReq must carry the full post-save credential set, not just the delta.
	if ch.chanReq.Channel != domain.ChannelTelegram || ch.chanReq.TelegramBotToken != "tg-token-2" || ch.chanReq.TelegramUserID != "tg-user" {
		t.Fatalf("chanReq incomplete: %+v", ch.chanReq)
	}

	// Switching channel entirely also re-pushes; untouched telegram creds survive.
	ch = applyUpdate(c, domain.UpdateConfigRequest{Channel: domain.ChannelSlack, SlackBotToken: "sl-bot", SlackUserID: "sl-user"}, "")
	if !ch.channel || ch.chanReq.Channel != domain.ChannelSlack || ch.chanReq.SlackBotToken != "sl-bot" {
		t.Fatalf("channel switch not captured: %+v", ch.chanReq)
	}
	if c.TelegramBotToken != "tg-token-2" {
		t.Fatal("channel switch wiped telegram creds")
	}
}

func TestApplyUpdateMQTTOnlySkipsRestarts(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{MQTTEndpoint: "mqtt2.example.com", MQTTPort: 8883}, "")
	if ch.halBoot || ch.lang || ch.realtime || ch.channel || ch.wifi || ch.model {
		t.Fatalf("mqtt-only save set restart flags: %+v", ch)
	}
	if c.MQTTEndpoint != "mqtt2.example.com" || c.MQTTPort != 8883 {
		t.Fatalf("mqtt fields not persisted: %+v", c)
	}
}

func TestApplyUpdateAdminHash(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{}, "new-hash")
	if c.AdminPasswordHash != "new-hash" {
		t.Fatal("admin hash not replaced")
	}
	if ch.halBoot || ch.channel {
		t.Fatalf("admin-only save set restart flags: %+v", ch)
	}
}

func TestApplyUpdateRealtime(t *testing.T) {
	c := baseConfig()
	enabled := true
	wakeWord := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{Realtime: &domain.RealtimeSetData{Enabled: &enabled}, WakeWord: &wakeWord}, "")
	if !ch.realtime {
		t.Fatal("realtime payload not flagged for hal restart")
	}
	if c.Realtime == nil || c.Realtime.Enabled == nil || !*c.Realtime.Enabled {
		t.Fatal("realtime enabled not persisted")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword not persisted")
	}
}

func TestApplyUpdateWakeWordRestartsHAL(t *testing.T) {
	c := baseConfig()
	wakeWord := true
	ch := applyUpdate(c, domain.UpdateConfigRequest{WakeWord: &wakeWord}, "")
	if !ch.halBoot {
		t.Fatal("wakeword-only save must restart HAL")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword not persisted")
	}
}

func TestApplyWakeWord(t *testing.T) {
	c := baseConfig()
	if !applyWakeWord(c, true) {
		t.Fatal("false to true must report a change")
	}
	if c.WakeWord == nil || !*c.WakeWord {
		t.Fatal("wakeword true not persisted")
	}
	if applyWakeWord(c, true) {
		t.Fatal("re-applying true must not report a change")
	}
	if !applyWakeWord(c, false) {
		t.Fatal("true to false must report a change")
	}
}

func TestGetPublicConfigReturnsEffectiveWakePhrases(t *testing.T) {
	i18n.SetDeviceName("Luna")
	t.Cleanup(func() { i18n.SetDeviceName("autonomous") })

	s := &Service{config: &config.Config{DeviceType: "lamp"}}
	cfg := s.GetPublicConfig()
	if cfg.AgentName != "luna" {
		t.Fatalf("AgentName = %q, want luna", cfg.AgentName)
	}
	if len(cfg.WakePhrases) != 21 {
		t.Fatalf("WakePhrases = %v, want 21 phrases", cfg.WakePhrases)
	}
	for _, phrase := range []string{"hey autonomous", "hey lamp", "hey luna"} {
		found := false
		for _, got := range cfg.WakePhrases {
			if got == phrase {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("WakePhrases = %v, missing %q", cfg.WakePhrases, phrase)
		}
	}
}

// The most common save an operator makes. hal takes provider and voice per
// utterance, so this must be pushed into the running process — restarting for
// it left the device deaf and mute for ten to fifteen seconds, and any admin
// click landing in that window was lost.
func TestApplyUpdateVoiceOnlyDoesNotRestartHAL(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{TTSVoice: "vi_VN-vais1000-medium"}, "")

	if !ch.tts {
		t.Fatalf("voice change not flagged for a live push: %+v", ch)
	}
	if ch.halBoot || ch.lang || ch.realtime {
		t.Fatalf("voice-only change must not restart hal: %+v", ch)
	}
	if c.TTSVoice != "vi_VN-vais1000-medium" {
		t.Fatalf("voice not persisted: %q", c.TTSVoice)
	}

	// Re-saving the same voice changes nothing at all.
	ch = applyUpdate(c, domain.UpdateConfigRequest{TTSVoice: "vi_VN-vais1000-medium"}, "")
	if ch.tts || ch.halBoot {
		t.Fatalf("identical voice flagged as a change: %+v", ch)
	}
}

// An STT key sits in hal's boot-read set, so it still has to bounce the process
// even though it arrives through the same voice settings page.
func TestApplyUpdateSTTKeyStillRestartsHAL(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{STTAPIKey: "stt-new"}, "")
	if !ch.halBoot {
		t.Fatalf("stt_api_key must restart hal: %+v", ch)
	}
}

// The settings page sends the realtime block on every save, so presence must
// not read as change: it restarted hal for a voice-only save and undid the
// whole point of pushing TTS config live.
func TestApplyUpdateUnchangedRealtimeDoesNotRestartHAL(t *testing.T) {
	c := baseConfig()
	enabled := true
	block := domain.RealtimeSetData{Enabled: &enabled, Provider: "gemini"}

	ch := applyUpdate(c, domain.UpdateConfigRequest{Realtime: &block}, "")
	if !ch.realtime {
		t.Fatalf("first realtime apply must count as a change: %+v", ch)
	}

	// The same block again — what every subsequent save sends.
	ch = applyUpdate(c, domain.UpdateConfigRequest{Realtime: &block}, "")
	if ch.realtime {
		t.Fatalf("re-sending the current realtime block flagged as a change: %+v", ch)
	}

	// A genuine switch still restarts.
	other := domain.RealtimeSetData{Enabled: &enabled, Provider: "openai"}
	ch = applyUpdate(c, domain.UpdateConfigRequest{Realtime: &other}, "")
	if !ch.realtime {
		t.Fatalf("a realtime provider switch must restart hal: %+v", ch)
	}
}

// The shipped credentials have to be preserved before the first edit lands on
// them, because that edit is what destroys them. Devices reached the field with
// the team's key overwritten and no way back.
func TestFirstCredentialChangeCapturesShippedDefaults(t *testing.T) {
	c := baseConfig()
	c.LLMAPIKey, c.LLMBaseURL, c.LLMModel = "team-key", "https://campaign-api.example.com", "team-model"

	applyUpdate(c, domain.UpdateConfigRequest{LLMAPIKey: "my-own-key"}, "")

	d := c.AutonomousDefaults
	if d == nil {
		t.Fatal("shipped credentials were not captured")
	}
	// The values as they were, not as the save left them.
	if d.APIKey != "team-key" || d.BaseURL != "https://campaign-api.example.com" || d.Model != "team-model" {
		t.Fatalf("captured the wrong values: %+v", d)
	}
	if c.LLMAPIKey != "my-own-key" {
		t.Fatalf("the edit itself did not apply: %q", c.LLMAPIKey)
	}
}

// Capturing twice would store the operator's own key under the Autonomous name
// and lose the real one for good — the exact failure this exists to prevent.
func TestLaterCredentialChangesDoNotOverwriteTheCapture(t *testing.T) {
	c := baseConfig()
	c.LLMAPIKey, c.LLMBaseURL, c.LLMModel = "team-key", "https://campaign-api.example.com", "team-model"

	applyUpdate(c, domain.UpdateConfigRequest{LLMAPIKey: "first-key"}, "")
	applyUpdate(c, domain.UpdateConfigRequest{TTSAPIKey: "second-key"}, "")
	applyUpdate(c, domain.UpdateConfigRequest{LLMBaseURL: "https://elsewhere.example.com"}, "")

	if got := c.AutonomousDefaults.APIKey; got != "team-key" {
		t.Fatalf("capture was overwritten: %q", got)
	}
}

// A wifi or rename save must not trip the capture — not because storing it
// early is harmful, but because "captured" is what the restore affordance keys
// off, and offering to restore on a device nobody has edited is noise.
func TestNonCredentialSaveDoesNotCapture(t *testing.T) {
	c := baseConfig()
	c.LLMAPIKey, c.LLMBaseURL = "team-key", "https://campaign-api.example.com"

	applyUpdate(c, domain.UpdateConfigRequest{DeviceID: "renamed"}, "")

	if c.AutonomousDefaults != nil {
		t.Fatalf("captured on a save that touched no credential: %+v", c.AutonomousDefaults)
	}
}

// An empty set is not a default worth restoring to.
func TestCaptureSkipsADeviceWithNoCredentials(t *testing.T) {
	c := baseConfig()
	c.LLMAPIKey, c.LLMBaseURL = "", ""

	applyUpdate(c, domain.UpdateConfigRequest{LLMAPIKey: "first-ever-key"}, "")

	if c.AutonomousDefaults != nil {
		t.Fatalf("captured an empty set: %+v", c.AutonomousDefaults)
	}
}

func TestApplyUpdateTTSSpeed(t *testing.T) {
	t.Setenv("HAL_TTS_SPEED", "")
	c := baseConfig()
	speed := 1.5
	ch := applyUpdate(c, domain.UpdateConfigRequest{TTSSpeed: &speed}, "")
	if c.GetTTSSpeed() != 1.5 || !ch.tts || ch.halBoot || ch.lang {
		t.Fatalf("speed update: %v %+v", c.GetTTSSpeed(), ch)
	}
	ch = applyUpdate(c, domain.UpdateConfigRequest{}, "")
	if c.GetTTSSpeed() != 1.5 || ch.tts {
		t.Fatal("omitted speed changed state")
	}
	ch = applyUpdate(c, domain.UpdateConfigRequest{TTSSpeed: &speed}, "")
	if ch.tts {
		t.Fatal("same speed triggered live apply")
	}
	speed = 1
	ch = applyUpdate(c, domain.UpdateConfigRequest{TTSSpeed: &speed}, "")
	if c.GetTTSSpeed() != 1 || !ch.tts {
		t.Fatal("normal speed not restored")
	}
}

func TestTTSSpeedRejectsBeforeMutation(t *testing.T) {
	t.Setenv("HAL_TTS_SPEED", "")
	c := baseConfig()
	s := &Service{config: c}
	speed := 0.0
	if err := s.UpdateConfig(domain.UpdateConfigRequest{TTSSpeed: &speed, TTSVoice: "changed"}); err == nil {
		t.Fatal("HTTP service accepted zero")
	}
	if err := s.UpdateVoiceConfig("elevenlabs", "changed", "vi", &speed); err == nil {
		t.Fatal("MQTT service accepted zero")
	}
	if c.TTSVoice != "alloy" || c.TTSProvider != "openai" || c.STTLanguage != "en" || c.TTSSpeed != nil {
		t.Fatal("invalid speed partially mutated config")
	}
	if got := s.GetPublicConfig().TTSSpeed; got != 1.2 {
		t.Fatalf("public default %v", got)
	}
}

// A provider switch must be able to DELETE the stored TTS key. Every field in
// UpdateConfigRequest is PATCH-style, where "" means "not sent", so an empty
// TTSAPIKey cannot express a clear — hence the explicit flag. Without this the
// previous vendor's key survives and GetTTSAPIKey hands it to the new provider.
func TestApplyUpdateClearTTSAPIKey(t *testing.T) {
	c := baseConfig()
	ch := applyUpdate(c, domain.UpdateConfigRequest{ClearTTSAPIKey: true}, "")

	if c.TTSAPIKey != "" {
		t.Fatalf("clear flag did not empty TTSAPIKey: %q", c.TTSAPIKey)
	}
	// Once cleared, resolution falls back to the AI-brain key — the credential
	// the Autonomous proxy actually authenticates against.
	if got := c.GetTTSAPIKey(); got != "key-llm" {
		t.Fatalf("GetTTSAPIKey after clear = %q, want the LLM key", got)
	}
	// The key is part of ttsSnapshot, so clearing it must flag a TTS change and
	// get pushed live to hal. Nothing hal reads at boot changed, so no restart.
	if !ch.tts {
		t.Fatalf("clearing the TTS key did not flag a tts change: %+v", ch)
	}
	if ch.halBoot {
		t.Fatalf("clearing the TTS key must not force a hal restart: %+v", ch)
	}
}

// Defensive ordering check. The UI never sends both, but if it ever did, the
// operator's newly typed key is the intent that should win.
func TestApplyUpdateClearTTSAPIKeyWithNewKeyPrefersNewKey(t *testing.T) {
	c := baseConfig()
	applyUpdate(c, domain.UpdateConfigRequest{
		ClearTTSAPIKey: true,
		TTSAPIKey:      "key-new",
	}, "")

	if c.TTSAPIKey != "key-new" {
		t.Fatalf("TTSAPIKey = %q, want the newly sent key", c.TTSAPIKey)
	}
}

// Regression guard for PATCH semantics: without the flag, a save that carries
// no TTS key must leave the stored one alone. Voice-only saves rely on this.
func TestApplyUpdateWithoutClearKeepsTTSAPIKey(t *testing.T) {
	c := baseConfig()
	applyUpdate(c, domain.UpdateConfigRequest{TTSVoice: "nova"}, "")

	if c.TTSAPIKey != "key-tts" {
		t.Fatalf("TTSAPIKey = %q, want it untouched", c.TTSAPIKey)
	}
}
