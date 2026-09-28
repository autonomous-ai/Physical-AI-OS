package config

import (
	"fmt"
	"strings"
)

// This file holds the realtime voice-agent (audio-native brain — Gemini Live /
// OpenAI Realtime / GPT-Live — or the on-device Pipecat pipeline) config types,
// defaults, and accessors. The Config.Realtime field that hangs these off the
// main config lives in config.go.

// RealtimeConfig groups the realtime voice-agent settings under the "realtime"
// key. Shared fields (enabled/provider/api_key/base_url) sit at the top; the
// per-provider knobs live in Gemini/OpenAI/GPTLive sub-objects, mirroring HAL's
// own GeminiConfig/OpenAIConfig/GPTLiveConfig dataclasses and the orchestrator's
// provider factory. `provider` selects which sub-object is active; all are kept
// so switching provider in the UI does not lose the others' tuned model/voice/
// reasoning.
//
// Defaults mirror HAL/Python (hal/config.py): unset → enabled + provider
// "gemini", so realtime runs out of the box. Only an explicit enabled:false or
// provider:"none" turns it off. Fields NOT modelled here (turn detection, session
// resumption, sample rate, memory/summarizer) stay governed by HAL's env/defaults
// — only the operator-facing knobs are lifted, exactly as the TTS config lifts
// provider/voice/instructions but not the VAD internals.
type RealtimeConfig struct {
	// Enabled toggles the realtime brain. Unset → true (mirrors HAL's
	// HAL_REALTIME_ENABLED default); set false to disable.
	Enabled   *bool              `json:"enabled,omitempty" yaml:"enabled"`
	Provider  string             `json:"provider,omitempty" yaml:"provider"` // none|gemini|openai|gptlive|pipecat_v1 ("" == none)
	APIKey    string             `json:"api_key,omitempty" yaml:"apiKey"`    // empty → falls back to LLMAPIKey
	BaseURL   string             `json:"base_url,omitempty" yaml:"baseURL"`  // empty → falls back to LLMBaseURL
	Gemini    *GeminiRealtime    `json:"gemini,omitempty" yaml:"gemini"`
	OpenAI    *OpenAIRealtime    `json:"openai,omitempty" yaml:"openai"`
	GPTLive   *GPTLiveRealtime   `json:"gptlive,omitempty" yaml:"gptlive"`
	PipecatV1 *PipecatV1Realtime `json:"pipecat_v1,omitempty" yaml:"pipecatV1"`
	// Pinned is set the first time an operator edits this block (web UI / MQTT
	// realtime.set). Until then the block is a fleet default: os-server rewrites
	// it from DefaultRealtimeConfig on every start, so changing the defaults in
	// code (e.g. gemini → openai) reaches every device that never chose. A
	// pinned block is never touched again. See ProvideConfig in config.go.
	Pinned bool `json:"pinned,omitempty" yaml:"pinned"`
}

// GeminiRealtime holds Gemini Live's provider-specific knobs. Empty fields → HAL
// applies its own default (e.g. model "gemini-…-live-preview", voice "Kore").
type GeminiRealtime struct {
	Model         string `json:"model,omitempty" yaml:"model"`
	Voice         string `json:"voice,omitempty" yaml:"voice"`                  // Gemini voice set (e.g. Kore)
	ThinkingLevel string `json:"thinking_level,omitempty" yaml:"thinkingLevel"` // Gemini-only reasoning knob (e.g. HIGH)
	// GoogleSearch toggles Google Search grounding (Gemini-only). nil → HAL default
	// (on). Kept here so an operator's explicit override survives config re-saves
	// instead of being silently dropped on the next marshal.
	GoogleSearch *bool `json:"google_search,omitempty" yaml:"googleSearch"`
	// Vision toggles the in-session `look` tool (Gemini-only): capture one camera
	// frame and answer visual questions in the realtime session instead of
	// delegating to main. nil → HAL default (on). Kept here so an operator's
	// explicit override survives config re-saves.
	Vision *bool `json:"vision,omitempty" yaml:"vision"`
}

// OpenAIRealtime holds OpenAI Realtime's provider-specific knobs. Empty fields →
// HAL applies its own default (e.g. model "gpt-realtime-…", voice "alloy").
type OpenAIRealtime struct {
	Model           string `json:"model,omitempty" yaml:"model"`
	Voice           string `json:"voice,omitempty" yaml:"voice"`                      // OpenAI voice set (e.g. alloy)
	ReasoningEffort string `json:"reasoning_effort,omitempty" yaml:"reasoningEffort"` // OpenAI-only reasoning knob (e.g. xhigh)
}

// GPTLiveRealtime holds GPT-Live's knobs — OpenAI `/v1/live/sessions`, the
// full-duplex Live API with client-side tool delegation (a different API from
// the Realtime API above). No reasoning knob: the Live model has none —
// reasoning lives in a Responses backend, which this integration does not use.
//
// Credentials reuse the shared realtime api_key/base_url like openai does. HAL
// resolves the key as OPENAI_API_KEY env > realtime.gptlive.api_key >
// realtime.api_key > llm_api_key, and the base URL as HAL_GPTLIVE_BASE_URL >
// realtime.gptlive.base_url > the OpenAI Realtime base URL (realtime.base_url >
// <llm_base_url>/ws/openai) — the SDK appends /live/sessions, so through the
// campaign-api proxy the session dials …/ws/openai/live/sessions next to the
// Realtime API's …/ws/openai/realtime. The optional api_key/base_url below only
// persist a per-provider override; realtime.set writes credentials to the
// shared fields.
type GPTLiveRealtime struct {
	APIKey  string `json:"api_key,omitempty" yaml:"apiKey"`
	BaseURL string `json:"base_url,omitempty" yaml:"baseURL"`
	Model   string `json:"model,omitempty" yaml:"model"`
	Voice   string `json:"voice,omitempty" yaml:"voice"` // GPT-Live voice set (openai.types.live BuiltInVoice, e.g. marin)
}

// PipecatV1Realtime holds the on-device Pipecat pipeline's knobs
// (hal/realtime/voice_agent/pipecat_v1.py): the device's own STT + an
// OpenAI-compatible chat LLM + the orchestrator's tools, audio in, TEXT out —
// HAL's TTS speaks the reply, so there is no voice and no reasoning knob.
//
// Credentials: HAL resolves the key as HAL_PIPECAT_API_KEY env >
// realtime.pipecat_v1.api_key > realtime.api_key > llm_api_key, and the chat
// base URL as HAL_PIPECAT_BASE_URL > realtime.pipecat_v1.base_url > its own
// default (the campaign-api Qwen relay). The shared realtime.base_url is
// deliberately NOT consulted: that field carries a WebSocket relay
// (…/ws/gemini) shape, not a chat-completions endpoint.
type PipecatV1Realtime struct {
	APIKey  string `json:"api_key,omitempty" yaml:"apiKey"`
	BaseURL string `json:"base_url,omitempty" yaml:"baseURL"`
	Model   string `json:"model,omitempty" yaml:"model"`
	// WebSearch toggles the client-side `web_search` tool (pipecat_v1-only): the
	// orchestrator answers public live-fact questions through the campaign-api
	// Google-Search relay in-session instead of delegating to main. nil → HAL
	// default (on). Kept here so an operator's explicit override survives config
	// re-saves instead of being silently dropped on the next marshal.
	WebSearch *bool `json:"web_search,omitempty" yaml:"webSearch"`
}

// Realtime per-provider defaults — what os-server resolves (and pushes) when the
// operator hasn't overridden a knob. Model/voice match HAL's defaults
// (hal/config.py). The reasoning knobs sit at the cheapest tier each model
// accepts (Gemini extended-thinking LOW, OpenAI minimal) — os-server picks a
// cost-lean default; an operator who wants deeper reasoning sets it explicitly.
// Values must stay valid against HAL's enums (GeminiThinkingLevel / GeminiVoice /
// OpenAIReasoningEffort / OpenAI voices).
const (
	// 3.8 Live (GA 2026-09-15) replaces the legacy 3.1 preview at the same price.
	// The extended-thinking variant accepts LOW/MEDIUM/HIGH only (no MINIMAL);
	// plain gemini-3.8-live has no thinking knob at all — HAL omits it there.
	// 2.5 native-audio: ~33% cheaper text tokens but needs HAL's idle workarounds
	// and rejects speech_config.language_code. Override via realtime.gemini.model.
	//defaultRealtimeGeminiModel     = "gemini-2.5-flash-native-audio-preview-12-2025"
	// The extended-thinking variant, not plain gemini-3.8-live: it answers chit-chat
	// DIRECTLY via voice, whereas plain gemini-3.8-live delegates or stays silent
	// every turn so realtime never speaks (device-observed 2026-09-18). HAL declares
	// NON_BLOCKING tools for it (a BLOCKING one made it error mid-turn, device-observed
	// 2026-09-17). It accepts LOW/MEDIUM/HIGH thinking (no MINIMAL).
	defaultRealtimeGeminiModel     = "gemini-3.8-live-extended-thinking"
	defaultRealtimeGeminiVoice     = "Kore"
	defaultRealtimeGeminiThinking  = "LOW"
	defaultRealtimeOpenAIModel     = "gpt-realtime-2"
	defaultRealtimeOpenAIVoice     = "alloy"
	defaultRealtimeOpenAIReasoning = "minimal"
	// GPT-Live: the single Live model and HAL's default voice (hal/config.py).
	// No reasoning default — the Live model exposes no such knob.
	defaultRealtimeGPTLiveModel = "gpt-live-1"
	defaultRealtimeGPTLiveVoice = "marin"
	// Pipecat v1: the low-latency Qwen relay model (hal/config.py). No voice,
	// no reasoning — the pipeline emits text and HAL's TTS speaks it.
	defaultRealtimePipecatV1Model = "qwen/qwen3.6-35b-a3b"
	// Pipecat v1: the client-side `web_search` tool is on unless the operator
	// turns it off (HAL default, hal/config.py REALTIME_PIPECAT_WEB_SEARCH).
	defaultRealtimePipecatV1WebSearch = true
)

// DefaultRealtimeConfig returns the realtime block os-server seeds into
// config.json on first start (or after an upgrade) when none is present, so the
// file always carries an editable realtime config. Values come from the provider
// defaults above; HAL then reads them straight from config.json. api_key/base_url
// are intentionally left empty so they fall back to the LLM credentials.
func DefaultRealtimeConfig() *RealtimeConfig {
	enabled := true
	return &RealtimeConfig{
		Enabled:  &enabled,
		Provider: "gemini",
		Gemini: &GeminiRealtime{
			Model:         defaultRealtimeGeminiModel,
			Voice:         defaultRealtimeGeminiVoice,
			ThinkingLevel: defaultRealtimeGeminiThinking,
		},
		OpenAI: &OpenAIRealtime{
			Model:           defaultRealtimeOpenAIModel,
			Voice:           defaultRealtimeOpenAIVoice,
			ReasoningEffort: defaultRealtimeOpenAIReasoning,
		},
		GPTLive: &GPTLiveRealtime{
			Model: defaultRealtimeGPTLiveModel,
			Voice: defaultRealtimeGPTLiveVoice,
		},
		PipecatV1: &PipecatV1Realtime{
			Model: defaultRealtimePipecatV1Model,
		},
	}
}

// --- Realtime voice-agent accessors -----------------------------------------
// All are nil-safe so callers never touch the nested struct directly; keys/URLs
// fall back to the LLM credentials (same pattern as Get{TTS,STT}*), and the
// model/voice/reasoning getters resolve the ACTIVE provider's sub-object.

// RealtimeEnabled reports whether the realtime brain should run. Defaults to true
// (mirrors HAL's HAL_REALTIME_ENABLED default) — only an explicit enabled:false
// turns it off (so does provider:"none", via RealtimeProvider).
func (c *Config) RealtimeEnabled() bool {
	if c.Realtime != nil && c.Realtime.Enabled != nil {
		return *c.Realtime.Enabled
	}
	return true
}

// RealtimeProvider returns the normalized provider ("gemini"/"openai"/"gptlive"/"pipecat_v1"), defaulting
// to "gemini" (mirrors HAL's HAL_REALTIME_PROVIDER default). An explicit
// none/off/disabled returns "" — realtime off. Matches the HAL orchestrator's
// provider vocabulary so the value can be pushed through verbatim.
func (c *Config) RealtimeProvider() string {
	p := ""
	if c.Realtime != nil {
		p = strings.ToLower(strings.TrimSpace(c.Realtime.Provider))
	}
	switch p {
	case "none", "off", "disabled":
		return ""
	case "":
		return "gemini"
	default:
		return p
	}
}

// RealtimeAPIKey returns the realtime provider key, falling back to LLMAPIKey.
func (c *Config) RealtimeAPIKey() string {
	if c.Realtime != nil && c.Realtime.APIKey != "" {
		return c.Realtime.APIKey
	}
	return c.LLMAPIKey
}

// RealtimeBaseURL returns the realtime provider base URL, falling back to LLMBaseURL.
//
// NOTE: this is the RESOLVED endpoint and is intentionally NOT what the public
// config / web form shows. The bare LLMBaseURL fallback lacks the provider WS
// suffix (HAL appends "/ws/gemini" itself when the override is empty), so echoing
// it into the editable "Base URL (leave blank to derive)" field would make the
// web re-persist a bare URL on the next save — which HAL then hands to the genai
// SDK verbatim, producing a 404 at the Gemini Live handshake. Use
// RealtimeBaseURLOverride for display so the field stays blank when deriving.
func (c *Config) RealtimeBaseURL() string {
	if c.Realtime != nil && c.Realtime.BaseURL != "" {
		return c.Realtime.BaseURL
	}
	return c.LLMBaseURL
}

// RealtimeBaseURLOverride returns ONLY the operator's explicit base_url override
// (empty when unset), without the LLMBaseURL fallback. The public config exposes
// this so the web "Base URL (leave blank to derive)" field reflects whether an
// override is actually set — see RealtimeBaseURL for why the resolved value must
// not leak into the editable form.
func (c *Config) RealtimeBaseURLOverride() string {
	if c.Realtime == nil {
		return ""
	}
	return c.Realtime.BaseURL
}

// RealtimeHasAPIKey reports whether an explicit realtime API key is set (the
// shared realtime.api_key; empty means the LLM key is reused).
func (c *Config) RealtimeHasAPIKey() bool {
	if c.Realtime == nil {
		return false
	}
	return c.Realtime.APIKey != ""
}

// RealtimeModel returns the active provider's model — the operator override when
// set, else the provider default (mirrors HAL). "" only when realtime is off.
func (c *Config) RealtimeModel() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.Model != "" {
			return c.Realtime.Gemini.Model
		}
		return defaultRealtimeGeminiModel
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.Model != "" {
			return c.Realtime.OpenAI.Model
		}
		return defaultRealtimeOpenAIModel
	case "gptlive":
		if c.Realtime != nil && c.Realtime.GPTLive != nil && c.Realtime.GPTLive.Model != "" {
			return c.Realtime.GPTLive.Model
		}
		return defaultRealtimeGPTLiveModel
	case "pipecat_v1":
		if c.Realtime != nil && c.Realtime.PipecatV1 != nil && c.Realtime.PipecatV1.Model != "" {
			return c.Realtime.PipecatV1.Model
		}
		return defaultRealtimePipecatV1Model
	}
	return ""
}

// RealtimeVoice returns the active provider's voice — override or provider default.
// Empty for pipecat_v1, which has no voice of its own (HAL's TTS speaks).
func (c *Config) RealtimeVoice() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.Voice != "" {
			return c.Realtime.Gemini.Voice
		}
		return defaultRealtimeGeminiVoice
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.Voice != "" {
			return c.Realtime.OpenAI.Voice
		}
		return defaultRealtimeOpenAIVoice
	case "gptlive":
		if c.Realtime != nil && c.Realtime.GPTLive != nil && c.Realtime.GPTLive.Voice != "" {
			return c.Realtime.GPTLive.Voice
		}
		return defaultRealtimeGPTLiveVoice
	}
	return ""
}

// RealtimeReasoning returns the active provider's reasoning knob — Gemini's
// thinking_level or OpenAI's reasoning_effort — override or the (cost-lean)
// provider default. Empty when realtime is off or the provider has no reasoning
// knob (gptlive, pipecat_v1).
func (c *Config) RealtimeReasoning() string {
	switch c.RealtimeProvider() {
	case "gemini":
		if c.Realtime != nil && c.Realtime.Gemini != nil && c.Realtime.Gemini.ThinkingLevel != "" {
			return c.Realtime.Gemini.ThinkingLevel
		}
		return defaultRealtimeGeminiThinking
	case "openai":
		if c.Realtime != nil && c.Realtime.OpenAI != nil && c.Realtime.OpenAI.ReasoningEffort != "" {
			return c.Realtime.OpenAI.ReasoningEffort
		}
		return defaultRealtimeOpenAIReasoning
	}
	// gptlive / pipecat_v1: no reasoning knob.
	return ""
}

// RealtimeWebSearch returns the resolved in-session web-search toggle for the
// active provider: the operator override when set, else the HAL default (on).
// nil for every provider but pipecat_v1 — Gemini grounds and GPT-Live's backend
// searches on their own side, so the knob does not exist there.
func (c *Config) RealtimeWebSearch() *bool {
	if c.RealtimeProvider() != "pipecat_v1" {
		return nil
	}
	if c.Realtime != nil && c.Realtime.PipecatV1 != nil && c.Realtime.PipecatV1.WebSearch != nil {
		v := *c.Realtime.PipecatV1.WebSearch
		return &v
	}
	v := defaultRealtimePipecatV1WebSearch
	return &v
}

// --- Validation (for realtime.set MQTT downlinks) ---------------------------
// Valid knob values per provider — KEEP IN SYNC with hal/realtime/
// enums (GeminiVoice / GeminiThinkingLevel / OpenAIReasoningEffort) and the
// OpenAI voice list in hal/routes/voice.py. Case-sensitive to match the HAL
// StrEnums (Gemini voices are Capitalized, OpenAI/GPT-Live voices and efforts
// are lowercase).
var (
	realtimeGeminiVoices    = map[string]bool{"Puck": true, "Charon": true, "Kore": true, "Fenrir": true, "Aoede": true}
	realtimeOpenAIVoices    = map[string]bool{"alloy": true, "ash": true, "coral": true, "echo": true, "fable": true, "onyx": true, "nova": true, "sage": true, "shimmer": true}
	realtimeGPTLiveVoices   = stringSet(RealtimeGPTLiveVoiceList)
	realtimeGeminiThinking  = map[string]bool{"MINIMAL": true, "LOW": true, "MEDIUM": true, "HIGH": true}
	realtimeOpenAIReasoning = map[string]bool{"minimal": true, "low": true, "medium": true, "high": true, "xhigh": true}
)

// stringSet builds a membership map from an ordered option list, so the
// validation set cannot drift from the list the web renders.
func stringSet(list []string) map[string]bool {
	m := make(map[string]bool, len(list))
	for _, v := range list {
		m[v] = true
	}
	return m
}

// Ordered option lists — the SINGLE SOURCE the web reads via GET realtime options
// (so the FE never hardcodes/drifts). Order matters: first reasoning entry is the
// cheapest (the default). Voices match the maps below; KEEP IN SYNC with the HAL
// enums (hal/realtime/enums).
var (
	RealtimeProviders       = []string{"gemini", "openai", "gptlive", "pipecat_v1", "none"}
	RealtimeGeminiVoiceList = []string{"Puck", "Charon", "Kore", "Fenrir", "Aoede"}
	RealtimeOpenAIVoiceList = []string{"alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"}
	// GPT-Live voices: the set gpt-live-1 accepts at session.start (BFF GPT-Live
	// integration doc, verified on the real model 2026-09-17). The SDK's wider
	// BuiltInVoice literal also carries Realtime-only names (alloy, ash, …) that
	// a Live session rejects — never list those here. Mirrors HAL's GPTLiveVoice.
	RealtimeGPTLiveVoiceList = []string{
		"marin", "quartz", "ripple", "vesper", "willow", "stone", "gleam",
		"meridian", "bossa", "tempo", "beacon", "delta", "cinder",
	}
	RealtimeGeminiThinkingList  = []string{"MINIMAL", "LOW", "MEDIUM", "HIGH"}
	RealtimeOpenAIReasoningList = []string{"minimal", "low", "medium", "high", "xhigh"}
)

// RealtimeOptions is the payload for the realtime-options endpoint: valid
// providers, and the per-provider voice/reasoning lists the web renders.
type RealtimeOptions struct {
	Providers []string            `json:"providers"`
	Voices    map[string][]string `json:"voices"`
	Reasoning map[string][]string `json:"reasoning"`
}

// GetRealtimeOptions returns the valid realtime option lists.
func GetRealtimeOptions() RealtimeOptions {
	return RealtimeOptions{
		Providers: RealtimeProviders,
		// pipecat_v1 → empty voice list: the pipeline emits text, HAL's TTS speaks.
		Voices: map[string][]string{"gemini": RealtimeGeminiVoiceList, "openai": RealtimeOpenAIVoiceList, "gptlive": RealtimeGPTLiveVoiceList, "pipecat_v1": {}},
		// gptlive / pipecat_v1 → empty list: no reasoning knob, so the web hides the selector.
		Reasoning: map[string][]string{"gemini": RealtimeGeminiThinkingList, "openai": RealtimeOpenAIReasoningList, "gptlive": {}, "pipecat_v1": {}},
	}
}

// ValidateRealtimeProvider accepts the provider selector (gemini|openai|gptlive|
// pipecat_v1|none and the off-synonyms / empty). Anything else is rejected.
func ValidateRealtimeProvider(provider string) error {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "gemini", "openai", "gptlive", "pipecat_v1", "none", "off", "disabled", "":
		return nil
	default:
		return fmt.Errorf("invalid realtime provider %q (want gemini|openai|gptlive|pipecat_v1|none)", provider)
	}
}

// ValidateRealtimeKnobs checks voice/reasoning against a CONCRETE provider
// (gemini|openai|gptlive|pipecat_v1). Empty voice/reasoning are allowed (means
// "keep current"). The per-provider knobs (model/voice/reasoning) only make
// sense for a concrete provider, so anything else is an error. gptlive has no
// reasoning knob, pipecat_v1 has neither voice nor reasoning, so those values
// are rejected for them.
func ValidateRealtimeKnobs(provider, voice, reasoning string) error {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "gemini":
		if voice != "" && !realtimeGeminiVoices[voice] {
			return fmt.Errorf("invalid gemini voice %q (Puck|Charon|Kore|Fenrir|Aoede)", voice)
		}
		if reasoning != "" && !realtimeGeminiThinking[reasoning] {
			return fmt.Errorf("invalid gemini thinking_level %q (MINIMAL|LOW|MEDIUM|HIGH)", reasoning)
		}
	case "openai":
		if voice != "" && !realtimeOpenAIVoices[voice] {
			return fmt.Errorf("invalid openai voice %q (alloy|ash|coral|echo|fable|onyx|nova|sage|shimmer)", voice)
		}
		if reasoning != "" && !realtimeOpenAIReasoning[reasoning] {
			return fmt.Errorf("invalid openai reasoning_effort %q (minimal|low|medium|high|xhigh)", reasoning)
		}
	case "gptlive":
		if voice != "" && !realtimeGPTLiveVoices[voice] {
			return fmt.Errorf("invalid gptlive voice %q (%s)", voice, strings.Join(RealtimeGPTLiveVoiceList, "|"))
		}
		if reasoning != "" {
			return fmt.Errorf("gptlive realtime has no reasoning knob, got %q", reasoning)
		}
	case "pipecat_v1":
		if voice != "" {
			return fmt.Errorf("pipecat_v1 realtime has no voice (HAL's TTS speaks), got %q", voice)
		}
		if reasoning != "" {
			return fmt.Errorf("pipecat_v1 realtime has no reasoning knob, got %q", reasoning)
		}
	default:
		return fmt.Errorf("realtime model/voice/reasoning require a concrete provider (gemini|openai|gptlive|pipecat_v1), got %q", provider)
	}
	return nil
}
