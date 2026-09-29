package device

import (
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// validateRealtimeSet checks the provider and its knobs (against the payload's
// provider, else the current one) without writing anything.
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

// applyRealtimeSet writes non-empty payload fields into c.Realtime. Run
// validateRealtimeSet first; must run inside WithLockSave.
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
	// Credentials are shared across providers; empty means HAL falls back to the
	// LLM credentials (gptlive: empty base_url means api.openai.com).
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

// UpdateRealtimeConfig persists a realtime payload and restarts HAL, which reads
// config.json at import.
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
