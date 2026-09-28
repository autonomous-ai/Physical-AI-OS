import { useEffect, useState } from "react";
import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { getRealtimeOptions } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";

// Realtime voice-agent (Gemini Live / OpenAI Realtime / GPT-Live / on-device
// Pipecat v1) config. Values map 1:1 to the config.json `realtime` block (HAL
// reads it; os-server restarts HAL on save). Voice + reasoning are
// provider-specific — keep these lists in sync with
// system/server/config/realtime.go (ValidateRealtimeKnobs) and the HAL enums.
const PROVIDERS = ["gemini", "openai", "gptlive", "pipecat_v1", "none"];

// Display labels for the Provider dropdown. Values on the wire stay lowercase
// (server-side switch keys off "gemini" / "openai" / …); only the human-facing
// string is title-cased. Unknown providers fall back to first-letter capitalise
// so the UI never shows a raw lowercase entry.
const PROVIDER_LABEL: Record<string, string> = {
  gemini: "Gemini",
  openai: "OpenAI",
  gptlive: "GPT-Live",
  pipecat_v1: "Pipecat v1 (on-device)",
  none: "None",
};
const displayProvider = (v: string): string =>
  PROVIDER_LABEL[v] ?? (v ? v[0].toUpperCase() + v.slice(1) : v);
const VOICES: Record<string, string[]> = {
  gemini: ["Puck", "Charon", "Kore", "Fenrir", "Aoede"],
  openai: ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"],
  // GPT-Live: the BuiltInVoice literal of openai SDK 3.14.1 (openai.types.live).
  // The voices gpt-live-1 accepts (BFF integration doc); Realtime-only names
  // such as alloy/ash are rejected at session.start and must not appear here.
  gptlive: [
    "marin", "quartz", "ripple", "vesper", "willow", "stone", "gleam",
    "meridian", "bossa", "tempo", "beacon", "delta", "cinder",
  ],
  // Pipecat v1 emits text; the device's own TTS voice speaks it → no voice.
  pipecat_v1: [],
};
// Reasoning depth = cost knob. First entry (cheapest) is the default.
// GPT-Live has no reasoning knob (the Live model exposes none) and neither
// does Pipecat v1 → empty list hides the selector.
const REASONING: Record<string, string[]> = {
  gemini: ["MINIMAL", "LOW", "MEDIUM", "HIGH"],
  openai: ["minimal", "low", "medium", "high", "xhigh"],
  gptlive: [],
  pipecat_v1: [],
};

export interface RealtimeLoadedState {
  apiKey: boolean;
}

const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};

const labelStyle = { display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 };

export function RealtimeSection({
  active,
  realtimeLoaded, llmLoaded,
  enabled, setEnabled,
  provider, setProvider,
  voice, setVoice,
  reasoning, setReasoning,
  apiKey, setApiKey,
  baseUrl, setBaseUrl,
  webSearch, setWebSearch,
}: {
  active: boolean;
  realtimeLoaded: RealtimeLoadedState;
  llmLoaded: LlmLoadedState;
  enabled: boolean; setEnabled: (v: boolean) => void;
  provider: string; setProvider: (v: string) => void;
  voice: string; setVoice: (v: string) => void;
  reasoning: string; setReasoning: (v: string) => void;
  apiKey: string; setApiKey: (v: string) => void;
  baseUrl: string; setBaseUrl: (v: string) => void;
  // pipecat_v1 only: the in-session `web_search` tool (realtime.pipecat_v1.web_search).
  webSearch: boolean; setWebSearch: (v: boolean) => void;
}) {
  // Options come from the API (single source = server config); the const lists
  // above are only a fallback if the fetch fails.
  const [opts, setOpts] = useState<{ providers: string[]; voices: Record<string, string[]>; reasoning: Record<string, string[]> } | null>(null);
  useEffect(() => { getRealtimeOptions().then(setOpts).catch(() => {}); }, []);
  const providers = opts?.providers ?? PROVIDERS;
  const voices = (opts?.voices ?? VOICES)[provider] ?? [];
  const reasonings = (opts?.reasoning ?? REASONING)[provider] ?? [];

  // Switching provider resets voice/reasoning to that provider's defaults so we
  // never submit, e.g., an OpenAI voice while provider=gemini (server rejects it).
  function onProviderChange(p: string) {
    setProvider(p);
    if (p === "none") return;
    if (!(VOICES[p] ?? []).includes(voice)) setVoice((VOICES[p] ?? [])[0] ?? "");
    if (!(REASONING[p] ?? []).includes(reasoning)) setReasoning((REASONING[p] ?? [])[0] ?? "");
  }

  return (
    <SectionCard id="realtime" title="Realtime" active={active}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, cursor: "pointer", fontSize: 12.5, color: C.text }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled (realtime brain — Gemini Live / OpenAI Realtime / GPT-Live / Pipecat v1 on-device)
      </label>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="realtime_provider" style={labelStyle}>Provider</label>
        <select id="realtime_provider" value={provider} onChange={(e) => onProviderChange(e.target.value)} style={selectStyle}>
          {providers.map((p) => <option key={p} value={p}>{displayProvider(p)}</option>)}
        </select>
      </div>

      {provider !== "none" && (
        <>
          {/* Realtime voice output is NOT used (device speaks via TTS), so the
              voice selector is hidden via display:none — code kept for re-enable. */}
          <div style={{ marginBottom: 12, display: "none" }}>
            <label htmlFor="realtime_voice" style={labelStyle}>Voice</label>
            <select id="realtime_voice" value={voice} onChange={(e) => setVoice(e.target.value)} style={selectStyle}>
              {voices.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </div>

          {reasonings.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <label htmlFor="realtime_reasoning" style={labelStyle}>Reasoning (cost — cheapest first)</label>
              <select id="realtime_reasoning" value={reasoning} onChange={(e) => setReasoning(e.target.value)} style={selectStyle}>
                {reasonings.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          )}

          <LockedPasswordField lockedInitially={realtimeLoaded.apiKey || llmLoaded.apiKey} label="API Key (optional — leave blank to reuse AI brain key)" id="realtime_api_key" value={apiKey} onChange={setApiKey} placeholder="sk-... / AIza..." />
          {provider === "pipecat_v1" ? (
            // The shared Base URL carries a WebSocket relay shape (…/ws/gemini) that
            // HAL never uses for this provider; its chat endpoint is
            // realtime.pipecat_v1.base_url in config.json (default: the Qwen relay).
            <>
              <div style={{ fontSize: 11, color: C.textDim, marginBottom: 12 }}>
                Runs the voice pipeline on the robot (its own STT + a text LLM, spoken by the TTS voice above).
                LLM endpoint: <code>realtime.pipecat_v1.base_url</code> in config.json — default is the low-latency Qwen relay.
              </div>
              {/* The relay LLM has no hosted search; this is the client-side
                  web_search tool (HAL_PIPECAT_WEB_SEARCH). Off → public live
                  facts (weather, news, scores) delegate to the main agent. */}
              <label style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12.5, color: C.text, cursor: "pointer", marginBottom: 12 }}>
                <input type="checkbox" checked={webSearch} onChange={(e) => setWebSearch(e.target.checked)} style={{ marginTop: 3 }} />
                <span>
                  Web search — answer public live facts (weather, news, scores, prices) in-session through the Google-Search relay.
                  <span style={{ display: "block", fontSize: 11, color: C.textDim, marginTop: 3 }}>
                    Off: those questions are delegated to the main agent instead. One relay call (~4 s) per lookup.
                  </span>
                </span>
              </label>
            </>
          ) : (
            <LockedField lockedInitially={llmLoaded.baseUrl} label="Base URL (optional — leave blank to derive from AI brain base URL)" id="realtime_base_url" value={baseUrl} onChange={setBaseUrl} placeholder="wss://… /ws/gemini" />
          )}
        </>
      )}
    </SectionCard>
  );
}
