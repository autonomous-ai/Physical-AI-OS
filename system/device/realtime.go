package device

import (
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// validateRealtimeSet checks a realtime payload before any write: the provider
// selector, and (when per-provider knobs are present) the target provider's
// voice/reasoning. The target is the provider being set, or the current one when
// `provider` is omitted. Returns a descriptive error; nothing is written.
func (s *Service) validateRealtimeSet(d domain.RealtimeSetData) error {
	if err := config.ValidateRealtimeProvider(d.Provider); err != nil {
		return err
	}
	if d.Model != "" || d.Voice != "" || d.Reasoning != "" {
		target := strings.TrimSpace(d.Provider)
		if target == "" {
			target = s.config.RealtimeProvider() // current resolved provider
		}
		if err := config.ValidateRealtimeKnobs(target, d.Voice, d.Reasoning); err != nil {
			return err
		}
	}
	if d.WebSearch != nil {
		target := strings.ToLower(strings.TrimSpace(d.Provider))
		if target == "" {
			target = s.config.RealtimeProvider()
		}
		if target != "pipecat_v1" {
			return fmt.Errorf("web_search is a pipecat_v1 knob, got provider %q", target)
		}
	}
	return nil
}

// applyRealtimeSet mutates the `realtime` block in c per the payload. Caller must
// have run validateRealtimeSet first; this only writes. Empty/omitted fields leave
// the current value unchanged; per-provider knobs land in the active provider's
// sub-object. Must run inside WithLockSave.
func applyRealtimeSet(c *config.Config, d domain.RealtimeSetData) {
	if c.Realtime == nil {
		c.Realtime = config.DefaultRealtimeConfig()
	}
	rt := c.Realtime
	// Any operator edit pins the block: os-server stops re-seeding defaults.
	rt.Pinned = true
	if d.Enabled != nil {
		rt.Enabled = d.Enabled
	}
	if d.Provider != "" {
		rt.Provider = strings.ToLower(strings.TrimSpace(d.Provider))
	}
	// Credentials live in the shared api_key/base_url fields (empty → HAL falls
	// back to the LLM credentials), regardless of which provider is active —
	// gptlive included (HAL reads the shared key; its base_url is never derived
	// from llm_base_url, so an empty override means api.openai.com).
	if d.APIKey != "" {
		rt.APIKey = d.APIKey
	}
	if d.BaseURL != "" {
		rt.BaseURL = d.BaseURL
	}
	if d.Model == "" && d.Voice == "" && d.Reasoning == "" && d.WebSearch == nil {
		return
	}
	switch strings.ToLower(strings.TrimSpace(rt.Provider)) {
	case "gemini":
		if rt.Gemini == nil {
			rt.Gemini = &config.GeminiRealtime{}
		}
		if d.Model != "" {
			rt.Gemini.Model = d.Model
		}
		if d.Voice != "" {
			rt.Gemini.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.Gemini.ThinkingLevel = d.Reasoning
		}
	case "openai":
		if rt.OpenAI == nil {
			rt.OpenAI = &config.OpenAIRealtime{}
		}
		if d.Model != "" {
			rt.OpenAI.Model = d.Model
		}
		if d.Voice != "" {
			rt.OpenAI.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.OpenAI.ReasoningEffort = d.Reasoning
		}
	case "gptlive":
		if rt.GPTLive == nil {
			rt.GPTLive = &config.GPTLiveRealtime{}
		}
		if d.Model != "" {
			rt.GPTLive.Model = d.Model
		}
		if d.Voice != "" {
			rt.GPTLive.Voice = d.Voice
		}
		// no reasoning knob — validateRealtimeSet already rejected it
	case "pipecat_v1":
		if rt.PipecatV1 == nil {
			rt.PipecatV1 = &config.PipecatV1Realtime{}
		}
		if d.Model != "" {
			rt.PipecatV1.Model = d.Model
		}
		if d.WebSearch != nil {
			v := *d.WebSearch
			rt.PipecatV1.WebSearch = &v
		}
		// no voice, no reasoning — validateRealtimeSet already rejected them
	}
}

// UpdateRealtimeConfig applies a realtime payload (MQTT realtime.set or the HTTP
// `realtime` field) to config.json under the config lock, then restarts hal so it
// reads the new block (HAL reads config.json at import).
func (s *Service) UpdateRealtimeConfig(d domain.RealtimeSetData) error {
	if err := s.validateRealtimeSet(d); err != nil {
		return err
	}
	if err := s.config.WithLockSave(func(c *config.Config) {
		applyRealtimeSet(c, d)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("realtime config updated", "component", "device",
		"provider", s.config.RealtimeProvider(), "enabled", s.config.RealtimeEnabled())
	s.restartHAL("realtime config change")
	return nil
}
