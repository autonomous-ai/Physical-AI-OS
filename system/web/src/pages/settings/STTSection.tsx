import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import type { LlmLoadedState } from "@/hooks/setup/types";

export interface SttLoadedState {
  deepgram: boolean;
  apiKey: boolean;
  baseUrl: boolean;
}

export type SttProvider = "autonomous" | "deepgram";

export function STTSection({
  active,
  sttLanguage, setSttLanguage,
  sttProvider, setSttProvider,
  sttLoaded, llmLoaded,
  deepgramApiKey, setDeepgramApiKey,
  sttApiKey, setSttApiKey,
  sttBaseUrl, setSttBaseUrl,
}: {
  active: boolean;
  sttLanguage: string; setSttLanguage: (v: string) => void;
  sttProvider: SttProvider; setSttProvider: (v: SttProvider) => void;
  sttLoaded: SttLoadedState;
  llmLoaded: LlmLoadedState;
  deepgramApiKey: string; setDeepgramApiKey: (v: string) => void;
  sttApiKey: string; setSttApiKey: (v: string) => void;
  sttBaseUrl: string; setSttBaseUrl: (v: string) => void;
}) {
  return (
    <SectionCard id="stt" title="Language" active={active}
      description="The language your robot listens in — and the one it speaks its own notices in, like “Be right back”. It does not change the language the agent replies in; that follows whoever is talking to it.">
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="stt_language" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
          Language
        </label>
        <select
          id="stt_language"
          value={sttLanguage}
          onChange={(e) => setSttLanguage(e.target.value)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          <option value="">Auto (default)</option>
          <option value="en">English</option>
          <option value="vi">Vietnamese</option>
          <option value="zh-CN">Chinese (Simplified)</option>
          <option value="zh-TW">Chinese (Traditional)</option>
        </select>
      </div>

      <div style={{ marginTop: 18, paddingTop: 12, borderTop: `1px solid ${C.border}` }}>
        <div style={{ fontSize: 11, color: C.textMuted, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
          Speech recognition (STT)
        </div>
        <div style={{ fontSize: 11, color: C.textDim, marginBottom: 10, lineHeight: 1.55 }}>
          Which service turns speech into text. The default reuses your AI Brain
          credentials, so there is nothing to fill in unless you want a different one.
        </div>
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="stt_provider" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
            Provider
          </label>
          <select
            id="stt_provider"
            value={sttProvider}
            onChange={(e) => setSttProvider(e.target.value as SttProvider)}
            style={{
              width: "100%", boxSizing: "border-box",
              background: C.surface, border: `1px solid ${C.border}`,
              borderRadius: 7, padding: "8px 11px",
              fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
            }}
          >
            <option value="autonomous">Autonomous (reuse AI brain)</option>
            <option value="deepgram">Deepgram</option>
          </select>
        </div>
        {sttProvider === "deepgram" ? (
          <LockedPasswordField lockedInitially={sttLoaded.deepgram} label="Deepgram API Key" id="deepgram_api_key" value={deepgramApiKey} onChange={setDeepgramApiKey} placeholder="Deepgram key" />
        ) : (
          <>
            <LockedPasswordField lockedInitially={sttLoaded.apiKey || llmLoaded.apiKey} label="API Key (optional — leave blank to reuse AI brain key)" id="stt_api_key" value={sttApiKey} onChange={setSttApiKey} placeholder="sk-..." />
            <LockedField lockedInitially={sttLoaded.baseUrl || llmLoaded.baseUrl} label="Base URL (optional — leave blank to reuse AI brain base URL)" id="stt_base_url" value={sttBaseUrl} onChange={setSttBaseUrl} placeholder="https://api.openai.com/v1" />
          </>
        )}
      </div>
    </SectionCard>
  );
}
