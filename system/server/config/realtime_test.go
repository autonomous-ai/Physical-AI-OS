package config

import (
	"encoding/json"
	"strings"
	"testing"
)

func boolPtr(b bool) *bool { return &b }

// With no realtime block the accessors return HAL/Python-mirroring defaults —
// enabled + provider gemini, flash model, Kore voice — and the reasoning knob
// defaults to the cost-lean MINIMAL (deliberately below HAL's HIGH). Keys/URLs
// fall back to the LLM credentials.
func TestRealtime_DefaultsWhenUnset(t *testing.T) {
	c := &Config{LLMAPIKey: "llm-key", LLMBaseURL: "https://llm.example"}
	if !c.RealtimeEnabled() {
		t.Error("RealtimeEnabled() = false, want true by default")
	}
	if got := c.RealtimeProvider(); got != "gemini" {
		t.Errorf("RealtimeProvider() = %q, want gemini default", got)
	}
	if got := c.RealtimeModel(); got != defaultRealtimeGeminiModel {
		t.Errorf("RealtimeModel() = %q, want %q", got, defaultRealtimeGeminiModel)
	}
	if got := c.RealtimeVoice(); got != defaultRealtimeGeminiVoice {
		t.Errorf("RealtimeVoice() = %q, want %q", got, defaultRealtimeGeminiVoice)
	}
	if got := c.RealtimeReasoning(); got != defaultRealtimeGeminiThinking {
		t.Errorf("RealtimeReasoning() = %q, want %q (cost-lean default)", got, defaultRealtimeGeminiThinking)
	}
	if got := c.RealtimeAPIKey(); got != "llm-key" {
		t.Errorf("RealtimeAPIKey() = %q, want LLM fallback", got)
	}
	if got := c.RealtimeBaseURL(); got != "https://llm.example" {
		t.Errorf("RealtimeBaseURL() = %q, want LLM fallback", got)
	}
}

// RealtimeBaseURLOverride returns ONLY the explicit override (no LLM fallback) so
// the public config / web form stays blank when deriving. Echoing the resolved
// bare LLMBaseURL into the editable field would let the web re-persist a URL
// missing the "/ws/gemini" suffix, breaking HAL's Gemini Live handshake (404).
func TestRealtime_BaseURLOverride(t *testing.T) {
	// No block, and an empty block: override is blank even though the resolver
	// would fall back to LLMBaseURL.
	for _, c := range []*Config{
		{LLMBaseURL: "https://llm.example"},
		{LLMBaseURL: "https://llm.example", Realtime: &RealtimeConfig{}},
	} {
		if got := c.RealtimeBaseURLOverride(); got != "" {
			t.Errorf("RealtimeBaseURLOverride() = %q, want \"\" (no override)", got)
		}
		if got := c.RealtimeBaseURL(); got != "https://llm.example" {
			t.Errorf("RealtimeBaseURL() = %q, want LLM fallback (resolver unchanged)", got)
		}
	}
	// An explicit override is returned verbatim.
	set := &Config{LLMBaseURL: "https://llm.example", Realtime: &RealtimeConfig{BaseURL: "https://rt.example/ws/gemini"}}
	if got := set.RealtimeBaseURLOverride(); got != "https://rt.example/ws/gemini" {
		t.Errorf("RealtimeBaseURLOverride() = %q, want explicit override", got)
	}
}

// Enabled defaults true; only an explicit false (or provider none) turns it off.
func TestRealtime_EnabledAndOff(t *testing.T) {
	if (&Config{Realtime: &RealtimeConfig{}}).RealtimeEnabled() != true {
		t.Error("empty block → want enabled true")
	}
	off := &Config{Realtime: &RealtimeConfig{Enabled: boolPtr(false)}}
	if off.RealtimeEnabled() {
		t.Error("Enabled:false → want false")
	}
	// provider none disables via the provider path; model/voice go empty (off).
	none := &Config{Realtime: &RealtimeConfig{Provider: "none"}}
	if none.RealtimeProvider() != "" {
		t.Errorf("provider none → want \"\", got %q", none.RealtimeProvider())
	}
	if none.RealtimeModel() != "" || none.RealtimeReasoning() != "" {
		t.Error("provider none → model/reasoning should be empty (off)")
	}
}

// provider normalizes: empty/unset → gemini default; none/off/disabled → "".
func TestRealtime_ProviderNormalize(t *testing.T) {
	cases := map[string]string{
		"  Gemini ": "gemini", "OPENAI": "openai", "GPTLive": "gptlive",
		"": "gemini", "none": "", "off": "", "disabled": "",
	}
	for in, want := range cases {
		c := &Config{Realtime: &RealtimeConfig{Provider: in}}
		if got := c.RealtimeProvider(); got != want {
			t.Errorf("RealtimeProvider(%q) = %q, want %q", in, got, want)
		}
	}
}

// The active provider selects which sub-object the knobs read; explicit overrides
// beat the defaults, and the inactive provider's block is ignored.
func TestRealtime_ProviderAwareOverrides(t *testing.T) {
	c := &Config{Realtime: &RealtimeConfig{
		Provider: "gemini",
		Gemini:   &GeminiRealtime{Model: "gem-live", Voice: "Charon", ThinkingLevel: "HIGH"},
		OpenAI:   &OpenAIRealtime{Model: "gpt-rt", Voice: "echo", ReasoningEffort: "high"},
	}}
	if c.RealtimeModel() != "gem-live" || c.RealtimeVoice() != "Charon" || c.RealtimeReasoning() != "HIGH" {
		t.Errorf("gemini overrides not applied: model=%q voice=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeVoice(), c.RealtimeReasoning())
	}
	c.Realtime.Provider = "openai"
	if c.RealtimeModel() != "gpt-rt" || c.RealtimeReasoning() != "high" {
		t.Errorf("openai overrides not applied after switch: model=%q reasoning=%q",
			c.RealtimeModel(), c.RealtimeReasoning())
	}
}

// Active provider with no sub-object → provider defaults (not empty); per-field
// key/baseURL override beats the LLM fallback.
func TestRealtime_MissingSubAndKeyOverride(t *testing.T) {
	c := &Config{
		LLMAPIKey:  "llm-key",
		LLMBaseURL: "https://llm.example",
		Realtime:   &RealtimeConfig{Provider: "openai"}, // no OpenAI sub
	}
	if got := c.RealtimeModel(); got != defaultRealtimeOpenAIModel {
		t.Errorf("RealtimeModel() = %q, want openai default %q", got, defaultRealtimeOpenAIModel)
	}
	if got := c.RealtimeReasoning(); got != "minimal" {
		t.Errorf("RealtimeReasoning() = %q, want openai cost-lean default minimal", got)
	}
	if got := c.RealtimeAPIKey(); got != "llm-key" {
		t.Errorf("RealtimeAPIKey() = %q, want LLM fallback", got)
	}
	c.Realtime.APIKey = "rt-key"
	c.Realtime.BaseURL = "https://rt.example"
	if c.RealtimeAPIKey() != "rt-key" || c.RealtimeBaseURL() != "https://rt.example" {
		t.Errorf("override not applied: key=%q url=%q", c.RealtimeAPIKey(), c.RealtimeBaseURL())
	}
}

// Validation accepts good provider/voice/reasoning and rejects bad ones.
func TestRealtime_Validate(t *testing.T) {
	for _, ok := range []string{"gemini", "openai", "gptlive", "pipecat_v1", "none", "off", "", "Gemini", " GPTLive ", " Pipecat_V1 "} {
		if err := ValidateRealtimeProvider(ok); err != nil {
			t.Errorf("provider %q should be valid: %v", ok, err)
		}
	}
	for _, bad := range []string{"grok", "qwen", "gpt-live", "openai-live", "pipecat", "pipecat-v1", "pipecat_v2"} {
		if ValidateRealtimeProvider(bad) == nil {
			t.Errorf("provider %q should be rejected", bad)
		}
	}

	// gemini knobs
	if err := ValidateRealtimeKnobs("gemini", "Kore", "MINIMAL"); err != nil {
		t.Errorf("valid gemini knobs rejected: %v", err)
	}
	if ValidateRealtimeKnobs("gemini", "alloy", "") == nil {
		t.Error("openai voice on gemini should be rejected")
	}
	if ValidateRealtimeKnobs("gemini", "", "xhigh") == nil {
		t.Error("openai reasoning on gemini should be rejected")
	}
	// openai knobs
	if err := ValidateRealtimeKnobs("openai", "alloy", "minimal"); err != nil {
		t.Errorf("valid openai knobs rejected: %v", err)
	}
	if ValidateRealtimeKnobs("openai", "Kore", "") == nil {
		t.Error("gemini voice on openai should be rejected")
	}
	// gptlive knobs: any listed voice is fine, reasoning is always rejected
	// (the Live model has no such knob), foreign voices are rejected.
	for _, v := range RealtimeGPTLiveVoiceList {
		if err := ValidateRealtimeKnobs("gptlive", v, ""); err != nil {
			t.Errorf("valid gptlive voice %q rejected: %v", v, err)
		}
	}
	if err := ValidateRealtimeKnobs(" GPTLive ", "marin", ""); err != nil {
		t.Errorf("gptlive provider should normalize case/space: %v", err)
	}
	if ValidateRealtimeKnobs("gptlive", "Kore", "") == nil {
		t.Error("gemini voice on gptlive should be rejected")
	}
	if ValidateRealtimeKnobs("gptlive", "fable", "") == nil {
		t.Error("openai-only voice fable on gptlive should be rejected")
	}
	for _, r := range []string{"minimal", "low", "high", "xhigh", "MINIMAL"} {
		err := ValidateRealtimeKnobs("gptlive", "", r)
		if err == nil {
			t.Errorf("reasoning %q on gptlive should be rejected (no reasoning knob)", r)
		} else if !strings.Contains(err.Error(), "no reasoning knob") {
			t.Errorf("gptlive reasoning error should say why, got: %v", err)
		}
	}
	if err := ValidateRealtimeKnobs("gptlive", "", ""); err != nil {
		t.Errorf("empty gptlive knobs should be allowed: %v", err)
	}
	// gptlive-only voices must not leak into the openai realtime set.
	if ValidateRealtimeKnobs("openai", "marin", "") == nil {
		t.Error("gptlive-only voice marin on openai should be rejected")
	}
	// empty voice/reasoning allowed (keep current)
	if err := ValidateRealtimeKnobs("gemini", "", ""); err != nil {
		t.Errorf("empty knobs should be allowed: %v", err)
	}
	// knobs require a concrete provider
	if ValidateRealtimeKnobs("none", "Kore", "") == nil {
		t.Error("knobs with provider none should be rejected")
	}
}

// DefaultRealtimeConfig seeds enabled + gemini with the cost-lean defaults and
// both provider sub-objects (so switching provider keeps tuned values). api_key /
// base_url stay empty → LLM fallback.
func TestRealtime_DefaultSeed(t *testing.T) {
	rt := DefaultRealtimeConfig()
	if rt.Enabled == nil || !*rt.Enabled {
		t.Error("seed: want enabled true")
	}
	if rt.Provider != "gemini" {
		t.Errorf("seed provider = %q, want gemini", rt.Provider)
	}
	if rt.APIKey != "" || rt.BaseURL != "" {
		t.Error("seed: api_key/base_url should be empty (LLM fallback)")
	}
	if rt.Gemini == nil || rt.Gemini.ThinkingLevel != defaultRealtimeGeminiThinking || rt.Gemini.Model != defaultRealtimeGeminiModel {
		t.Errorf("seed gemini wrong: %+v", rt.Gemini)
	}
	if rt.OpenAI == nil || rt.OpenAI.ReasoningEffort != "minimal" {
		t.Errorf("seed openai wrong: %+v", rt.OpenAI)
	}
	if rt.GPTLive == nil || rt.GPTLive.Model != "gpt-live-1" || rt.GPTLive.Voice != "marin" {
		t.Errorf("seed gptlive wrong: %+v", rt.GPTLive)
	}
	if rt.GPTLive != nil && (rt.GPTLive.APIKey != "" || rt.GPTLive.BaseURL != "") {
		t.Error("seed gptlive: api_key/base_url should be empty (shared realtime credentials)")
	}

	// Default() now carries the seeded block, so a fresh config.json includes it.
	if Default().Realtime == nil {
		t.Error("Default() should seed Realtime")
	}
	data, _ := json.Marshal(Default())
	if !strings.Contains(string(data), `"realtime"`) {
		t.Errorf("Default() marshal should include realtime block: %s", data)
	}
}

// The pointer field must omit cleanly: a nil Realtime emits no "realtime" key,
// while a present block round-trips. (Guards the omitempty-on-struct gotcha — a
// value field would always marshal "realtime":{}.)
func TestRealtime_JSONOmitAndRoundTrip(t *testing.T) {
	noBlock, err := json.Marshal(&Config{LLMAPIKey: "k"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(noBlock), "realtime") {
		t.Errorf("nil Realtime should omit the key, got: %s", noBlock)
	}

	in := &Config{Realtime: &RealtimeConfig{
		Provider: "openai",
		Enabled:  boolPtr(true),
		OpenAI:   &OpenAIRealtime{Model: "gpt-rt", Voice: "alloy", ReasoningEffort: "xhigh"},
	}}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out Config
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.RealtimeProvider() != "openai" || out.RealtimeModel() != "gpt-rt" || out.RealtimeReasoning() != "xhigh" {
		t.Errorf("round-trip lost data: provider=%q model=%q reasoning=%q",
			out.RealtimeProvider(), out.RealtimeModel(), out.RealtimeReasoning())
	}
}

// gptlive resolves like the other providers: defaults when the sub-object is
// missing, overrides when set, and NO reasoning (the Live model has no knob).
// Credentials stay on the shared realtime fields (LLM fallback), not the
// sub-object — HAL reads the shared key; the base URL is never derived from
// llm_base_url, so the override stays blank unless the operator sets one.
func TestRealtime_GPTLiveResolution(t *testing.T) {
	c := &Config{
		LLMAPIKey:  "llm-key",
		LLMBaseURL: "https://llm.example",
		Realtime:   &RealtimeConfig{Provider: "gptlive"}, // no GPTLive sub
	}
	if got := c.RealtimeProvider(); got != "gptlive" {
		t.Fatalf("RealtimeProvider() = %q, want gptlive", got)
	}
	if got := c.RealtimeModel(); got != "gpt-live-1" {
		t.Errorf("RealtimeModel() = %q, want gpt-live-1 default", got)
	}
	if got := c.RealtimeVoice(); got != "marin" {
		t.Errorf("RealtimeVoice() = %q, want marin default", got)
	}
	if got := c.RealtimeReasoning(); got != "" {
		t.Errorf("RealtimeReasoning() = %q, want \"\" (gptlive has no reasoning knob)", got)
	}
	if c.RealtimeAPIKey() != "llm-key" || c.RealtimeHasAPIKey() {
		t.Errorf("gptlive credentials should fall back to the shared/LLM key: key=%q has=%v",
			c.RealtimeAPIKey(), c.RealtimeHasAPIKey())
	}
	if got := c.RealtimeBaseURLOverride(); got != "" {
		t.Errorf("RealtimeBaseURLOverride() = %q, want \"\" (no override)", got)
	}

	c.Realtime.GPTLive = &GPTLiveRealtime{Model: "gpt-live-next", Voice: "cedar"}
	c.Realtime.APIKey = "rt-key"
	if c.RealtimeModel() != "gpt-live-next" || c.RealtimeVoice() != "cedar" {
		t.Errorf("gptlive overrides not applied: model=%q voice=%q", c.RealtimeModel(), c.RealtimeVoice())
	}
	if c.RealtimeAPIKey() != "rt-key" || !c.RealtimeHasAPIKey() {
		t.Errorf("shared realtime key not used for gptlive: key=%q", c.RealtimeAPIKey())
	}
	// The inactive providers' blocks do not bleed into gptlive resolution.
	c.Realtime.OpenAI = &OpenAIRealtime{Model: "gpt-rt", Voice: "alloy", ReasoningEffort: "xhigh"}
	if c.RealtimeModel() != "gpt-live-next" || c.RealtimeReasoning() != "" {
		t.Errorf("openai block leaked into gptlive: model=%q reasoning=%q", c.RealtimeModel(), c.RealtimeReasoning())
	}
}

// The options payload the web renders carries gptlive: listed as a provider
// (before none), its 22-voice list, and an EMPTY (not absent, not null)
// reasoning list so the selector is hidden client-side.
func TestRealtime_OptionsIncludeGPTLive(t *testing.T) {
	opts := GetRealtimeOptions()
	want := []string{"gemini", "openai", "gptlive", "pipecat_v1", "none"}
	if strings.Join(opts.Providers, ",") != strings.Join(want, ",") {
		t.Errorf("Providers = %v, want %v", opts.Providers, want)
	}
	if got := opts.Voices["gptlive"]; len(got) != 13 || got[0] != "marin" || got[len(got)-1] != "cinder" {
		t.Errorf("Voices[gptlive] = %v, want the 22-entry BuiltInVoice list", got)
	}
	for _, v := range opts.Voices["gptlive"] {
		if err := ValidateRealtimeKnobs("gptlive", v, ""); err != nil {
			t.Errorf("options voice %q not accepted by validation: %v", v, err)
		}
	}
	reasoning, ok := opts.Reasoning["gptlive"]
	if !ok || reasoning == nil || len(reasoning) != 0 {
		t.Errorf("Reasoning[gptlive] = %v (present=%v), want an empty non-nil list", reasoning, ok)
	}
	data, err := json.Marshal(opts)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(data), `"gptlive":[]`) {
		t.Errorf("options JSON should carry \"gptlive\":[] for reasoning, got: %s", data)
	}
}

// The gptlive sub-object round-trips through JSON (including its optional
// per-provider credential overrides) and a nil sub-object omits the key.
func TestRealtime_GPTLiveJSONRoundTrip(t *testing.T) {
	noSub, err := json.Marshal(&Config{Realtime: &RealtimeConfig{Provider: "gptlive"}})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(noSub), `"gptlive":{`) {
		t.Errorf("nil GPTLive should omit the sub-object, got: %s", noSub)
	}
	in := &Config{Realtime: &RealtimeConfig{
		Provider: "gptlive",
		GPTLive:  &GPTLiveRealtime{Model: "gpt-live-1", Voice: "marin", APIKey: "sub-key", BaseURL: "https://live.example"},
	}}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out Config
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.RealtimeProvider() != "gptlive" || out.RealtimeModel() != "gpt-live-1" || out.RealtimeVoice() != "marin" {
		t.Errorf("round-trip lost data: provider=%q model=%q voice=%q",
			out.RealtimeProvider(), out.RealtimeModel(), out.RealtimeVoice())
	}
	if out.Realtime.GPTLive == nil || out.Realtime.GPTLive.APIKey != "sub-key" || out.Realtime.GPTLive.BaseURL != "https://live.example" {
		t.Errorf("gptlive credential overrides lost: %+v", out.Realtime.GPTLive)
	}
}

// pipecat_v1: an on-device text-out pipeline — listed as a provider before
// none, with EMPTY (present, non-nil) voice and reasoning lists so the web
// hides both selectors; any voice or reasoning value is rejected for it, the
// model resolves to the Qwen relay default and is overridable.
func TestRealtime_PipecatV1(t *testing.T) {
	opts := GetRealtimeOptions()
	for _, m := range []map[string][]string{opts.Voices, opts.Reasoning} {
		got, ok := m["pipecat_v1"]
		if !ok || got == nil || len(got) != 0 {
			t.Errorf("pipecat_v1 option list = %v (present=%v), want an empty non-nil list", got, ok)
		}
	}
	if err := ValidateRealtimeKnobs("pipecat_v1", "", ""); err != nil {
		t.Errorf("empty knobs on pipecat_v1 rejected: %v", err)
	}
	if ValidateRealtimeKnobs("pipecat_v1", "alloy", "") == nil {
		t.Error("a voice on pipecat_v1 should be rejected (HAL's TTS speaks)")
	}
	if ValidateRealtimeKnobs("pipecat_v1", "", "low") == nil {
		t.Error("reasoning on pipecat_v1 should be rejected (no knob)")
	}

	c := &Config{LLMAPIKey: "llm-key", LLMBaseURL: "https://llm.example", Realtime: &RealtimeConfig{Provider: "pipecat_v1"}}
	if c.RealtimeProvider() != "pipecat_v1" || c.RealtimeModel() != defaultRealtimePipecatV1Model {
		t.Errorf("resolution: provider=%q model=%q", c.RealtimeProvider(), c.RealtimeModel())
	}
	if c.RealtimeVoice() != "" || c.RealtimeReasoning() != "" {
		t.Errorf("pipecat_v1 must resolve no voice/reasoning: voice=%q reasoning=%q", c.RealtimeVoice(), c.RealtimeReasoning())
	}
	c.Realtime.PipecatV1 = &PipecatV1Realtime{Model: "qwen/other"}
	if c.RealtimeModel() != "qwen/other" {
		t.Errorf("model override not applied: %q", c.RealtimeModel())
	}
	seed := DefaultRealtimeConfig()
	if seed.PipecatV1 == nil || seed.PipecatV1.Model != defaultRealtimePipecatV1Model || seed.PipecatV1.APIKey != "" || seed.PipecatV1.BaseURL != "" {
		t.Errorf("seed pipecat_v1 wrong: %+v", seed.PipecatV1)
	}
}
