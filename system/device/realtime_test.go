package device

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// gptlive lands its model/voice in the gptlive sub-object (created on demand)
// while credentials stay on the shared realtime api_key/base_url — no
// provider-routed credentials, unlike the removed qwen path.
func TestApplyRealtimeSetGPTLive(t *testing.T) {
	c := baseConfig()
	c.Realtime = &config.RealtimeConfig{Provider: "gemini"} // no sub-objects at all

	applyRealtimeSet(c, domain.RealtimeSetData{
		Provider: " GPTLive ",
		Model:    "gpt-live-1",
		Voice:    "cedar",
		APIKey:   "rt-key",
		BaseURL:  "https://live.example",
	})

	rt := c.Realtime
	if rt.Provider != "gptlive" {
		t.Fatalf("provider = %q, want normalized gptlive", rt.Provider)
	}
	if rt.GPTLive == nil || rt.GPTLive.Model != "gpt-live-1" || rt.GPTLive.Voice != "cedar" {
		t.Fatalf("gptlive sub-object not written: %+v", rt.GPTLive)
	}
	if rt.APIKey != "rt-key" || rt.BaseURL != "https://live.example" {
		t.Fatalf("credentials must land on the shared fields: key=%q url=%q", rt.APIKey, rt.BaseURL)
	}
	if rt.GPTLive.APIKey != "" || rt.GPTLive.BaseURL != "" {
		t.Fatalf("realtime.set must not route credentials into the gptlive sub-object: %+v", rt.GPTLive)
	}
	if rt.Gemini != nil || rt.OpenAI != nil {
		t.Fatalf("other providers' sub-objects must stay untouched: gemini=%+v openai=%+v", rt.Gemini, rt.OpenAI)
	}
	if c.RealtimeModel() != "gpt-live-1" || c.RealtimeVoice() != "cedar" || c.RealtimeReasoning() != "" {
		t.Fatalf("resolution after apply: model=%q voice=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeVoice(), c.RealtimeReasoning())
	}

	// A knob-only follow-up keeps the provider and only touches the voice.
	applyRealtimeSet(c, domain.RealtimeSetData{Voice: "marin"})
	if rt.GPTLive.Model != "gpt-live-1" || rt.GPTLive.Voice != "marin" || rt.Provider != "gptlive" {
		t.Fatalf("voice-only apply drifted: %+v provider=%q", rt.GPTLive, rt.Provider)
	}
}

// validateRealtimeSet rejects a reasoning value for gptlive (no knob) but
// accepts a listed voice, both when the provider is sent and when it is the
// current one.
func TestValidateRealtimeSetGPTLive(t *testing.T) {
	s := &Service{config: baseConfig()}
	s.config.Realtime = &config.RealtimeConfig{Provider: "gptlive"}

	if err := s.validateRealtimeSet(domain.RealtimeSetData{Provider: "gptlive", Voice: "marin"}); err != nil {
		t.Fatalf("valid gptlive payload rejected: %v", err)
	}
	if err := s.validateRealtimeSet(domain.RealtimeSetData{Voice: "quartz"}); err != nil {
		t.Fatalf("voice-only payload against current gptlive provider rejected: %v", err)
	}
	if err := s.validateRealtimeSet(domain.RealtimeSetData{Provider: "gptlive", Reasoning: "minimal"}); err == nil {
		t.Fatal("reasoning on gptlive must be rejected")
	}
	if err := s.validateRealtimeSet(domain.RealtimeSetData{Reasoning: "high"}); err == nil {
		t.Fatal("reasoning against current gptlive provider must be rejected")
	}
	if err := s.validateRealtimeSet(domain.RealtimeSetData{Provider: "gptlive", Voice: "Kore"}); err == nil {
		t.Fatal("gemini voice on gptlive must be rejected")
	}
}

// pipecat_v1 lands its model in the pipecat_v1 sub-object (created on demand);
// credentials stay on the shared realtime fields; voice/reasoning are rejected
// up front, so the apply never sees them.
func TestApplyRealtimeSetPipecatV1(t *testing.T) {
	c := baseConfig()
	c.Realtime = &config.RealtimeConfig{Provider: "gemini"}

	applyRealtimeSet(c, domain.RealtimeSetData{
		Provider: " Pipecat_V1 ",
		Model:    "qwen/qwen3.6-35b-a3b",
		APIKey:   "rt-key",
	})
	rt := c.Realtime
	if rt.Provider != "pipecat_v1" {
		t.Fatalf("provider = %q, want normalized pipecat_v1", rt.Provider)
	}
	if rt.PipecatV1 == nil || rt.PipecatV1.Model != "qwen/qwen3.6-35b-a3b" {
		t.Fatalf("pipecat_v1 sub-object not written: %+v", rt.PipecatV1)
	}
	if rt.APIKey != "rt-key" || rt.PipecatV1.APIKey != "" {
		t.Fatalf("credentials must land on the shared fields only: shared=%q sub=%q", rt.APIKey, rt.PipecatV1.APIKey)
	}
	if c.RealtimeModel() != "qwen/qwen3.6-35b-a3b" || c.RealtimeVoice() != "" || c.RealtimeReasoning() != "" {
		t.Fatalf("resolution after apply: model=%q voice=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeVoice(), c.RealtimeReasoning())
	}

	s := &Service{config: c}
	if err := s.validateRealtimeSet(domain.RealtimeSetData{Provider: "pipecat_v1", Model: "qwen/x"}); err != nil {
		t.Fatalf("model-only set should validate: %v", err)
	}
	if s.validateRealtimeSet(domain.RealtimeSetData{Provider: "pipecat_v1", Voice: "marin"}) == nil {
		t.Fatal("a voice on pipecat_v1 must be rejected")
	}
	if s.validateRealtimeSet(domain.RealtimeSetData{Reasoning: "low"}) == nil {
		t.Fatal("reasoning on the current pipecat_v1 provider must be rejected")
	}
}

// web_search is a pipecat_v1-only knob: it lands in the sub-object as an
// explicit override (so a later re-save keeps it), resolves through
// RealtimeWebSearch with the HAL default (on) when unset, is left alone when
// omitted, and is rejected for any other provider — explicit or current.
func TestApplyRealtimeSetPipecatV1WebSearch(t *testing.T) {
	c := baseConfig()
	c.Realtime = &config.RealtimeConfig{Provider: "pipecat_v1"}
	if got := c.RealtimeWebSearch(); got == nil || !*got {
		t.Fatalf("unset web_search must resolve to the HAL default (on), got %v", got)
	}

	off := false
	applyRealtimeSet(c, domain.RealtimeSetData{Provider: "pipecat_v1", WebSearch: &off})
	if c.Realtime.PipecatV1 == nil || c.Realtime.PipecatV1.WebSearch == nil || *c.Realtime.PipecatV1.WebSearch {
		t.Fatalf("web_search=false not persisted: %+v", c.Realtime.PipecatV1)
	}
	if got := c.RealtimeWebSearch(); got == nil || *got {
		t.Fatalf("resolution after apply: %v, want false", got)
	}

	// A model-only follow-up must not clear the override.
	applyRealtimeSet(c, domain.RealtimeSetData{Provider: "pipecat_v1", Model: "qwen/x"})
	if c.Realtime.PipecatV1.WebSearch == nil || *c.Realtime.PipecatV1.WebSearch {
		t.Fatal("omitting web_search must leave the override unchanged")
	}

	s := &Service{config: c}
	on := true
	if err := s.validateRealtimeSet(domain.RealtimeSetData{WebSearch: &on}); err != nil {
		t.Fatalf("web_search on the current pipecat_v1 provider should validate: %v", err)
	}
	if s.validateRealtimeSet(domain.RealtimeSetData{Provider: "gemini", WebSearch: &on}) == nil {
		t.Fatal("web_search on gemini must be rejected")
	}
	c.Realtime.Provider = "gptlive"
	if s.validateRealtimeSet(domain.RealtimeSetData{WebSearch: &off}) == nil {
		t.Fatal("web_search on the current non-pipecat provider must be rejected")
	}
	if c.RealtimeWebSearch() != nil {
		t.Fatal("RealtimeWebSearch must be nil for a provider without the knob")
	}
}
