import { Volume2 } from "lucide-react";
import { C, SectionCard, LABEL_STYLE, INPUT_STYLE, FIELD_GAP } from "./shared";
import { testTTSVoice } from "@/lib/api";

export function TTSSection({
  active, isContinue,
  ttsProvider, setTtsProvider, ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  sttLanguage,
}: {
  active: boolean;
  isContinue: boolean;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  sttLanguage: string;
}) {
  return (
    <SectionCard id="tts" title="Voice" active={active} icon={<Volume2 size={17} />}
      description="Choose how your robot sounds when it speaks back to you.">
      {/* tts_api_key + tts_base_url are not exposed in Setup —
          they're auto-mirrored from AI Brain via useEffect and
          submitted silently. */}
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="tts_provider" style={LABEL_STYLE}>
          Provider
        </label>
        <select
          id="tts_provider"
          value={ttsProvider}
          onChange={(e) => setTtsProvider(e.target.value)}
          style={{ ...INPUT_STYLE, cursor: "pointer" }}
        >
          {(ttsProviders.length > 0 ? ttsProviders : ["elevenlabs"]).map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="tts_voice" style={LABEL_STYLE}>
          Voice
        </label>
        <select
          id="tts_voice"
          value={ttsVoice}
          onChange={(e) => setTtsVoice(e.target.value)}
          style={{ ...INPUT_STYLE, cursor: "pointer" }}
        >
          {(ttsVoices.length > 0 ? ttsVoices : ["Rachel"]).map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
        {isContinue ? (
          <button
            type="button"
            onClick={() => testTTSVoice(ttsVoice, {
              lang: sttLanguage,
              provider: ttsProvider,
            })}
            style={{
              marginTop: 10, width: "100%", padding: "10px 0",
              background: C.amber, color: "#0C0B09", border: "none",
              borderRadius: 8, fontSize: 13, cursor: "pointer", fontWeight: 600,
            }}
          >
            Test Voice
          </button>
        ) : (
          <div style={{ marginTop: 8, fontSize: 12.5, color: C.textDim }}>
            You can preview voices after your robot is online (next step).
          </div>
        )}
      </div>
    </SectionCard>
  );
}
