import { SettingsSelect } from "@/components/SettingsSelect";
import { useEffect, useState } from "react";
import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { getRealtimeOptions } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";

// Keep these lists in sync with system/server/config/realtime.go (ValidateRealtimeKnobs) and the HAL enums.
const PROVIDERS = ["gemini", "openai", "gptlive", "pipecat_v1", "none"];

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
  // gpt-live-1 voices only; Realtime-only names (alloy/ash) are rejected at session.start.
  gptlive: [
    "marin", "quartz", "ripple", "vesper", "willow", "stone", "gleam",
    "meridian", "bossa", "tempo", "beacon", "delta", "cinder",
  ],
  pipecat_v1: [],
};
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

const labelStyle = { display: "block", fontSize: 12, color: C.textDim, marginBottom: 5 };

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
  webSearch: boolean; setWebSearch: (v: boolean) => void;
}) {
  // Options come from the API; the const lists above are only a fallback.
  const [opts, setOpts] = useState<{ providers: string[]; voices: Record<string, string[]>; reasoning: Record<string, string[]> } | null>(null);
  useEffect(() => { getRealtimeOptions().then(setOpts).catch(() => {}); }, []);
  const providers = opts?.providers ?? PROVIDERS;
  const voices = (opts?.voices ?? VOICES)[provider] ?? [];
  const reasonings = (opts?.reasoning ?? REASONING)[provider] ?? [];

  // Switching provider resets voice/reasoning to that provider's defaults so we never submit, e.g., an OpenAI voice while provider=gemini (server rejects it).
  function onProviderChange(p: string) {
    setProvider(p);
    if (p === "none") return;
    if (!(VOICES[p] ?? []).includes(voice)) setVoice((VOICES[p] ?? [])[0] ?? "");
    if (!(REASONING[p] ?? []).includes(reasoning)) setReasoning((REASONING[p] ?? [])[0] ?? "");
  }

  return (
    <SectionCard id="realtime" title="Realtime" active={active}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, cursor: "pointer", fontSize: 12.5, color: C.text }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} style={{ flexShrink: 0 }} />
        <span style={{ whiteSpace: "nowrap" }}>Enable realtime voice</span>
      </label>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="realtime_provider" style={labelStyle}>Provider</label>
        <SettingsSelect id="realtime_provider" value={provider} onValueChange={(value) => onProviderChange(value)} style={selectStyle}>
          {providers.map((p) => <option key={p} value={p}>{displayProvider(p)}</option>)}
        </SettingsSelect>
      </div>

      {provider !== "none" && (
        <>
          <div style={{ marginBottom: 12, display: "none" }}>
            <label htmlFor="realtime_voice" style={labelStyle}>Voice</label>
            <SettingsSelect id="realtime_voice" value={voice} onValueChange={(value) => setVoice(value)} style={selectStyle}>
              {voices.map((v) => <option key={v} value={v}>{v}</option>)}
            </SettingsSelect>
          </div>

          {reasonings.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <label htmlFor="realtime_reasoning" style={labelStyle}>Reasoning (cost — cheapest first)</label>
              <SettingsSelect id="realtime_reasoning" value={reasoning} onValueChange={(value) => setReasoning(value)} style={selectStyle}>
                {reasonings.map((r) => <option key={r} value={r}>{r}</option>)}
              </SettingsSelect>
            </div>
          )}

          <LockedPasswordField lockedInitially={realtimeLoaded.apiKey || llmLoaded.apiKey} label="API Key (optional)" id="realtime_api_key" value={apiKey} onChange={setApiKey} placeholder="sk-... / AIza..." />
          <p style={{ fontSize: 12, color: C.textDim, marginTop: -8, marginBottom: 12 }}>
            Leave blank to reuse the AI brain key.
          </p>
          {provider === "pipecat_v1" ? (
            <>
              <div style={{ fontSize: 12, color: C.textDim, marginBottom: 12 }}>
                Runs the voice pipeline on the robot (its own STT + a text LLM, spoken by the TTS voice above).
                LLM endpoint: <code>realtime.pipecat_v1.base_url</code> in config.json — default is the low-latency Qwen relay.
              </div>
              <label style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12.5, color: C.text, cursor: "pointer", marginBottom: 12 }}>
                <input type="checkbox" checked={webSearch} onChange={(e) => setWebSearch(e.target.checked)} style={{ marginTop: 3, flexShrink: 0 }} />
                <span style={{ minWidth: 0 }}>
                  <span style={{ whiteSpace: "nowrap" }}>Web search</span>
                  <span style={{ display: "block", fontSize: 12, color: C.textDim, marginTop: 3 }}>
                    Answer public live facts (weather, news, scores, prices) in-session through the Google-Search relay.
                  </span>
                  <span style={{ display: "block", fontSize: 12, color: C.textDim, marginTop: 3 }}>
                    Off: those questions are delegated to the main agent instead. One relay call (~4 s) per lookup.
                  </span>
                </span>
              </label>
            </>
          ) : (
            <>
              <LockedField lockedInitially={llmLoaded.baseUrl} label="Base URL (optional)" id="realtime_base_url" value={baseUrl} onChange={setBaseUrl} placeholder="wss://… /ws/gemini" />
              <p style={{ fontSize: 12, color: C.textDim, marginTop: -8, marginBottom: 12 }}>
                Leave blank to derive from the AI brain base URL.
              </p>
            </>
          )}
        </>
      )}
    </SectionCard>
  );
}
