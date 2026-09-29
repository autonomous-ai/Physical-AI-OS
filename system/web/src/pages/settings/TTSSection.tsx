import { useCallback, useEffect, useRef, useState } from "react";
import { getPiperStatus, installPiperEngine, installPiperVoice, removePiperVoice, type PiperJobStart, type PiperStatus } from "@/lib/api";
import { Loader2, Volume2, Check, AlertCircle } from "lucide-react";
import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { testTTSVoice } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";
import { detectChoice, type ProviderChoice } from "@/pages/settings/ttsProvider";

export interface TtsLoadedState {
  apiKey: boolean;
  baseUrl: boolean;
  // The choice the stored key belongs to.
  choice: ProviderChoice;
}

// Provider "choice" is UI-only; it maps back to tts_provider + tts_base_url on save.
type Vendor = "openai" | "elevenlabs" | "gemini";

interface ChoiceMeta {
  label: string;
  baseUrl: string;
  vendor?: Vendor;
  hint?: string;
}
const CHOICES: Record<ProviderChoice, ChoiceMeta> = {
  autonomous: {
    label: "Autonomous (proxy)",
    baseUrl: "https://campaign-api.autonomous.ai/api/v1/ai/v1",
    hint: "Routes through Autonomous — supports OpenAI, ElevenLabs + Gemini voices",
  },
  openai: {
    label: "OpenAI (direct)",
    baseUrl: "https://api.openai.com/v1",
    vendor: "openai",
  },
  elevenlabs: {
    label: "ElevenLabs (direct)",
    baseUrl: "https://api.elevenlabs.io/v1",
    vendor: "elevenlabs",
  },
  piper: {
    label: "Piper (Local — free)",
    baseUrl: "",
    hint: "Runs on the robot — no API key, no quota, works offline. Lower quality than a hosted voice.",
  },
  custom: {
    label: "Custom (BYO URL)",
    baseUrl: "",
    hint: "Bring-your-own URL — for self-hosted proxies (vLLM, LiteLLM, …)",
  },
};


// Mirrors hal/presets.py SUPPORTED_LANGS.
type Lang = "" | "en" | "vi" | "zh-CN" | "zh-TW";
const LANG_LABEL: Record<Lang, string> = {
  "":      "Auto (follow robot language)",
  "en":    "English",
  "vi":    "Vietnamese",
  "zh-CN": "Chinese (Simplified)",
  "zh-TW": "Chinese (Traditional)",
};
const LANG_OPTIONS: Lang[] = ["", "en", "vi", "zh-CN", "zh-TW"];

// Keep ElevenLabs names a strict subset of HAL's VOICE_IDS_BY_LANG, or they 404.
type LangBucket = "en" | "vi" | "zh";
function langBucket(lang: Lang): LangBucket {
  if (lang === "vi") return "vi";
  if (lang === "zh-CN" || lang === "zh-TW") return "zh";
  return "en";
}
const OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"];
// Mirrors hal/drivers/voice/tts/gemini.py GeminiTTSBackend.VOICES.
const GEMINI_VOICES = [
  "Kore", "Puck", "Zephyr", "Charon", "Fenrir", "Leda", "Orus", "Aoede",
  "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
  "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
  "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
  "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
];
const VOICES: Record<Vendor, Record<LangBucket, string[]>> = {
  elevenlabs: {
    en: [
      "Rachel", "Sarah", "Nicole", "Terra", "Maria", "Sophie",
      "Piper", "Mia", "Kimmy", "Brianna", "Ally", "Tori",
      "Brian", "Adam", "Daniel", "George", "James", "Liam",
      "Charlie", "Sam", "Sean", "Kael", "Brooks", "Erion",
    ],
    vi: ["Ngan", "Linh", "Huyen", "Freya", "Nathan"],
    zh: ["Amy", "Sage", "Xiaoxi", "Yun", "Evan Zhao"],
  },
  openai: { en: OPENAI_VOICES, vi: OPENAI_VOICES, zh: OPENAI_VOICES },
  gemini: { en: GEMINI_VOICES, vi: GEMINI_VOICES, zh: GEMINI_VOICES },
};

function voicesFor(vendor: Vendor, lang: Lang, sttLang: string): string[] {
  const effective = lang || (sttLang as Lang) || "en";
  return VOICES[vendor][langBucket(effective)];
}

// Display label for a voice name.
function displayVoice(v: string): string {
  if (!v) return v;
  const parts = v.split("-");
  if (parts.length > 1) {
    const named = parts.slice(0, 2).map((p, i) =>
      i === 0 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1)
    ).join(" ");
    return named;
  }
  return v[0].toUpperCase() + v.slice(1);
}

// Edit-mode TTS exposes the api key + base URL fields so operators can override them per-section.
export function TTSSection({
  active,
  ttsLoaded, llmLoaded,
  ttsApiKey, setTtsApiKey, setTtsApiKeyRaw,
  ttsBaseUrl, setTtsBaseUrl,
  ttsProvider, setTtsProvider, ttsProviders: _ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  ttsSpeed, setTtsSpeed,
  sttLanguage,
}: {
  active: boolean;
  ttsLoaded: TtsLoadedState;
  llmLoaded: LlmLoadedState;
  ttsApiKey: string; setTtsApiKey: (v: string) => void;
  // Unmirrored setter: onChoice must bypass the AI-Brain mirror.
  setTtsApiKeyRaw: (v: string) => void;
  ttsBaseUrl: string; setTtsBaseUrl: (v: string) => void;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  ttsSpeed: number; setTtsSpeed: (v: number) => void;
  sttLanguage: string;
}) {
  // Stored as state so "Custom" can be picked while the URL still matches a preset.
  const [choice, setChoice] = useState<ProviderChoice>(() => detectChoice(ttsBaseUrl, ttsProvider));
  // Session-only cache of typed-but-unsaved keys per choice; never persisted.
  const keyDrafts = useRef<Partial<Record<ProviderChoice, string>>>({});
  const [syncedUrl, setSyncedUrl] = useState(ttsBaseUrl);
  if (syncedUrl !== ttsBaseUrl) {
    setSyncedUrl(ttsBaseUrl);
    if (choice !== "custom") setChoice(detectChoice(ttsBaseUrl, ttsProvider));
  }
  const meta = CHOICES[choice];

  const vendor: Vendor = meta.vendor
    ?? (ttsProvider === "openai" || ttsProvider === "elevenlabs" || ttsProvider === "gemini"
      ? (ttsProvider as Vendor)
      : "elevenlabs");

  const speedMin = 0.5;
  const speedMax = 2.0;
  const effectiveSpeed = Math.max(speedMin, Math.min(speedMax, ttsSpeed));

  const [lang, setLang] = useState<Lang>("");
  const [piperInstalled, setPiperInstalled] = useState<string[]>([]);
  const piperLang: string = choice === "piper" ? (ttsVoice.split("_")[0] || "") : "";
  const voices = choice === "piper"
    ? piperInstalled
    : voicesFor(vendor, lang, sttLanguage);

  const keyRequired = choice === "openai" || choice === "elevenlabs";
  // A stored key belongs to the choice it was saved under (#309).
  const storedKeyIsForThisChoice = ttsLoaded.apiKey && ttsLoaded.choice === choice;

  const onChoice = (next: ProviderChoice) => {
    if (next === choice) return;
    keyDrafts.current[choice] = ttsApiKey;
    setTtsApiKeyRaw(next === "autonomous" || next === "piper" ? "" : (keyDrafts.current[next] ?? ""));
    setChoice(next);
    const nextMeta = CHOICES[next];
    if (next === "custom") {
      return;
    }
    if (next === "piper") {
      setTtsBaseUrl("");
      setTtsProvider("piper");
      // Only keep a voice the device actually has (ttsVoices may still hold the previous provider's list).
      if (!piperInstalled.includes(ttsVoice)) setTtsVoice("");
      return;
    }
    setTtsBaseUrl(nextMeta.baseUrl);
    if (next === "autonomous") {
      if (ttsProvider !== "openai" && ttsProvider !== "elevenlabs" && ttsProvider !== "gemini") {
        setTtsProvider("elevenlabs");
        setTtsVoice(voicesFor("elevenlabs", lang, sttLanguage)[0]);
      }
      return;
    }
    if (nextMeta.vendor && nextMeta.vendor !== ttsProvider) {
      setTtsProvider(nextMeta.vendor);
      setTtsVoice(voicesFor(nextMeta.vendor, lang, sttLanguage)[0]);
    }
  };

  const onVendor = (v: Vendor) => {
    setTtsProvider(v);
    setTtsVoice(voicesFor(v, lang, sttLanguage)[0]);
  };

  const onLang = (next: Lang) => {
    setLang(next);
    const pool = voicesFor(vendor, next, sttLanguage);
    if (!pool.includes(ttsVoice) && pool.length > 0) {
      setTtsVoice(pool[0]);
    }
  };

  return (
    <SectionCard id="tts" title="Voice" active={active}>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_provider_choice" style={labelStyle}>Provider</label>
        <select
          id="tts_provider_choice"
          value={choice}
          onChange={(e) => onChoice(e.target.value as ProviderChoice)}
          style={selectStyle}
        >
          {(Object.keys(CHOICES) as ProviderChoice[]).map((k) => (
            <option key={k} value={k}>{CHOICES[k].label}</option>
          ))}
        </select>
        {meta.hint && (
          <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 4 }}>{meta.hint}</div>
        )}
      </div>

      {choice === "piper" && (
        <PiperPanel
          voice={ttsVoice}
          onPickVoice={setTtsVoice}
          onInstalledChange={setPiperInstalled}
        />
      )}

      {choice === "autonomous" && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="tts_vendor" style={labelStyle}>Vendor (voices come from here)</label>
          <select
            id="tts_vendor"
            value={ttsProvider === "openai" || ttsProvider === "gemini" ? ttsProvider : "elevenlabs"}
            onChange={(e) => onVendor(e.target.value as Vendor)}
            style={selectStyle}
          >
            <option value="openai">OpenAI</option>
            <option value="elevenlabs">ElevenLabs</option>
            <option value="gemini">Gemini</option>
          </select>
        </div>
      )}

      {choice === "piper" ? null : choice === "custom" ? (
        <LockedField
          lockedInitially={ttsLoaded.baseUrl || llmLoaded.baseUrl}
          label="Base URL"
          id="tts_base_url"
          value={ttsBaseUrl}
          onChange={setTtsBaseUrl}
          placeholder="https://your-proxy.example.com/v1"
        />
      ) : (
        <div style={{ marginBottom: 12 }}>
          <label style={labelStyle}>Base URL (locked — set by provider)</label>
          <div style={{
            ...selectStyle,
            cursor: "default", fontFamily: "ui-monospace, monospace",
            color: C.textDim,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{meta.baseUrl}</div>
        </div>
      )}

      {choice === "custom" && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="tts_custom_vendor" style={labelStyle}>Vendor protocol (which API your URL speaks)</label>
          <select
            id="tts_custom_vendor"
            value={vendor}
            onChange={(e) => onVendor(e.target.value as Vendor)}
            style={selectStyle}
          >
            <option value="openai">OpenAI-compatible</option>
            <option value="elevenlabs">ElevenLabs-compatible</option>
          </select>
        </div>
      )}

      {choice !== "piper" && (<>
      <div style={{ marginBottom: 5, display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <label htmlFor="tts_api_key" style={labelStyle}>
          {keyRequired
            ? "API Key (required — this provider does not accept the AI brain key)"
            : "API Key (optional — leave blank to reuse AI brain key)"}
        </label>
        {storedKeyIsForThisChoice && (
          <span style={{ fontSize: 10, color: "var(--lm-green, #34d399)", fontWeight: 600 }}>
            ✓ configured
          </span>
        )}
        {keyRequired && !storedKeyIsForThisChoice && !ttsApiKey && (
          <span style={{ fontSize: 10, color: "var(--lm-amber, #fbbf24)", fontWeight: 600 }}>
            key required
          </span>
        )}
      </div>
      <LockedPasswordField
        lockedInitially={storedKeyIsForThisChoice || (!keyRequired && llmLoaded.apiKey)}
        label=""
        id="tts_api_key"
        value={ttsApiKey}
        onChange={setTtsApiKey}
        placeholder={storedKeyIsForThisChoice ? "•••••••• saved (click ✎ to rotate)" : "sk-..."}
      />
      </>)}

      {choice !== "piper" && (
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_lang" style={labelStyle}>Language (voice list filter)</label>
        <select
          id="tts_lang"
          value={lang}
          onChange={(e) => onLang(e.target.value as Lang)}
          style={selectStyle}
        >
          {LANG_OPTIONS.map((l) => (
            <option key={l || "auto"} value={l}>{LANG_LABEL[l]}</option>
          ))}
        </select>
        {(vendor === "openai" || vendor === "gemini") && (
          <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 4 }}>
            {vendor === "openai" ? "OpenAI" : "Gemini"} voices are multilingual — the same voice handles any
            language. Filter is a no-op here.
          </div>
        )}
      </div>
      )}

      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_voice" style={labelStyle}>Voice</label>
        <select
          id="tts_voice"
          value={voices.includes(ttsVoice) ? ttsVoice : (voices[0] ?? "")}
          onChange={(e) => setTtsVoice(e.target.value)}
          style={selectStyle}
        >
          {(voices.length > 0 ? voices : ttsVoices).map((v) => (
            <option key={v} value={v}>{displayVoice(v)}</option>
          ))}
        </select>
      </div>

      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_speed" style={labelStyle}>Speech speed: {effectiveSpeed.toFixed(2)}×</label>
        <input
          id="tts_speed"
          type="range"
          min={speedMin}
          max={speedMax}
          step={0.05}
          value={effectiveSpeed}
          onChange={(e) => setTtsSpeed(Number(e.target.value))}
          aria-valuetext={`${effectiveSpeed.toFixed(2)} times normal speed`}
          style={{ width: "100%", accentColor: C.green }}
        />
        <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 4 }}>
          {speedMin}×–{speedMax}× · 1.0× normal. Test Voice uses this speed immediately.
        </div>
        <TestVoiceButton
          voice={ttsVoice}
          lang={piperLang || lang || sttLanguage}
          provider={ttsProvider}
          baseUrl={ttsBaseUrl}
          apiKey={ttsApiKey}
          speed={effectiveSpeed}
          blockedReason={
            choice !== "piper" ? ""
              : piperInstalled.length === 0 ? "Download a voice first"
              : !piperInstalled.includes(ttsVoice) ? "Select a downloaded voice"
              : ""
          }
        />
      </div>

    </SectionCard>
  );
}

// Test Voice button with idle/loading/played/failed feedback.
function TestVoiceButton({ voice, lang, provider, baseUrl, apiKey, speed, blockedReason = "" }: {
  voice: string;
  lang: string;
  provider: string;
  blockedReason?: string;
  baseUrl: string;
  apiKey: string;
  speed: number;
}) {
  type Phase = "idle" | "loading" | "ok" | "error";
  const [phase, setPhase] = useState<Phase>("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const busy = phase === "loading";

  const onClick = async () => {
    if (busy) return;
    setPhase("loading");
    setErrorMsg("");
    try {
      await testTTSVoice(voice, { lang, provider, baseUrl, apiKey, speed });
      setPhase("ok");
      window.setTimeout(() => setPhase("idle"), 2500);
    } catch (err) {
      setPhase("error");
      setErrorMsg(err instanceof Error ? err.message : "Test failed");
      window.setTimeout(() => setPhase("idle"), 3500);
    }
  };

  // Loading disables clicks so slow proxies cannot stack synths.
  const blocked = !!blockedReason;
  const bg =
    blocked ? "var(--lm-border, #3a3a3a)" :
    phase === "ok" ? "var(--lm-green, #34d399)" :
    phase === "error" ? "var(--lm-red, #ef4444)" :
    "var(--lm-amber, #f5c25a)";
  const icon =
    phase === "loading" ? <Loader2 size={14} className="lm-spin-ico" /> :
    phase === "ok" ? <Check size={14} /> :
    phase === "error" ? <AlertCircle size={14} /> :
    <Volume2 size={14} />;
  const label =
    blocked ? blockedReason :
    phase === "loading" ? "Sending to robot…" :
    phase === "ok" ? "Playing on robot" :
    phase === "error" ? "Failed" :
    "Test Voice";

  return (
    <>
      <button
        type="button"
        onClick={onClick}
        disabled={busy || blocked}
        aria-live="polite"
        style={{
          marginTop: 8, width: "100%", padding: "10px 0",
          background: bg, color: "#fff", border: "none",
          borderRadius: 7, fontSize: 12, fontWeight: 700,
          cursor: blocked ? "not-allowed" : busy ? "wait" : "pointer",
          opacity: blocked ? 0.6 : busy ? 0.85 : 1,
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          transition: "background 0.2s ease, opacity 0.15s",
        }}>
        {icon}
        <span>{label}</span>
      </button>
      {phase === "error" && errorMsg && (
        <div style={{
          marginTop: 6, fontSize: 11, color: "var(--lm-red, #ef4444)",
          textAlign: "center",
        }}>{errorMsg}</div>
      )}
    </>
  );
}

const labelStyle = {
  display: "block" as const,
  fontSize: 11, color: C.textDim, marginBottom: 5,
};
const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};

// Piper install state: engine first, then the voice catalogue with per-voice downloads.
function PiperPanel({ voice, onPickVoice, onInstalledChange }: {
  voice: string;
  onPickVoice: (v: string) => void;
  onInstalledChange: (installed: string[]) => void;
}) {
  const [st, setSt] = useState<PiperStatus | null>(null);
  // Not an error: saving a voice restarts HAL, so status is briefly unreachable.
  const [unreachable, setUnreachable] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState("");
  // HAL answers rejections with 200 + {status:"error"}.
  const [notice, setNotice] = useState("");

  const load = useCallback(() => {
    return getPiperStatus()
      .then((next) => {
        setSt(next);
        setUnreachable(false);
      })
      .catch(() => setUnreachable(true));
  }, []);

  useEffect(() => { load(); }, [load]);

  // Adopt the job from the reply; polling only runs while a job is active.
  const postAndRefresh = useCallback((run: () => Promise<PiperJobStart>) => {
    run()
      .then((res) => {
        setNotice(res.status === "error" ? res.message || "Request refused" : "");
        const started = res.job;
        if (started) setSt((prev) => (prev ? { ...prev, job: started } : prev));
        load();
      })
      .catch(() => {
        setUnreachable(true);
        setNotice("Robot was restarting — nothing changed. Try again in a moment.");
      });
  }, [load]);

  // Mask in-flight removals; the poll still reports them installed until done.
  const [removing, setRemoving] = useState<string[]>([]);

  const removeVoice = useCallback((name: string) => {
    setNotice("");
    setConfirmRemove("");
    setRemoving((cur) => [...cur, name]);
    removePiperVoice(name)
      .then((res) => {
        if (res.status === "error") setNotice(res.message || "Request refused");
        return load();
      })
      .catch(() => {
        setUnreachable(true);
        setNotice("Robot was restarting — nothing changed. Try again in a moment.");
      })
      .finally(() => setRemoving((cur) => cur.filter((n) => n !== name)));
  }, [load]);

  useEffect(() => {
    const busy = !!st?.job?.active;
    if (!busy && !unreachable && st) return;
    const t = setInterval(load, busy ? 2000 : 3000);
    return () => clearInterval(t);
  }, [st, unreachable, load]);

  useEffect(() => {
    if (st) onInstalledChange(st.voices_installed.filter((n) => !removing.includes(n)));
  }, [st, removing, onInstalledChange]);

  if (!st) {
    return (
      <div style={{ fontSize: 12, color: C.textMuted, marginBottom: 12 }}>
        {unreachable ? "Robot is restarting — reconnecting…" : "Checking robot…"}
      </div>
    );
  }

  const job = st.job;
  const busy = job.active;
  const catalog = st.catalog.map((c) =>
    removing.includes(c.name) ? { ...c, installed: false } : c);
  // HAL refuses to delete the last model.
  const onlyOneLeft = catalog.filter((c) => c.installed).length <= 1;

  return (
    <div style={{ marginBottom: 14, padding: "12px 14px", background: "var(--lm-surface-2, #1a1a1a)", borderRadius: 8 }}>
      {!st.engine_installed && (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: C.textDim }}>Engine not installed (~26 MB)</span>
          <button
            type="button"
            onClick={() => postAndRefresh(installPiperEngine)}
            disabled={busy}
            style={{ ...smallBtn, opacity: busy ? 0.5 : 1 }}
          >
            {busy && job.kind === "engine" ? `Installing ${job.percent}%` : "Install engine"}
          </button>
        </div>
      )}

      {busy && (
        <div style={{ marginBottom: 12 }}>
          <div style={{
            display: "flex", justifyContent: "space-between",
            alignItems: "baseline", gap: 8, marginBottom: 5,
          }}>
            <span style={{ fontSize: 11.5, color: C.text }}>
              {job.kind === "engine"
                ? "Installing engine…"
                : `Downloading ${voiceLabel(st, job.target)}…`}
            </span>
            <span style={{
              fontSize: 11, color: C.textMuted,
              fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap",
            }}>
              {job.bytes_total > 0 && `${mb(job.bytes_done)} / ${mb(job.bytes_total)} MB · `}
              {job.percent}%
            </span>
          </div>
          <div style={{
            height: 4, borderRadius: 2, overflow: "hidden",
            background: "var(--lm-border, #2a2a2a)",
          }}>
            <div style={{
              height: "100%", width: `${Math.max(2, job.percent)}%`,
              background: C.green, transition: "width 0.4s ease",
            }} />
          </div>
          <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 5 }}>
            Running on the robot — you can leave this page or reload, it keeps going.
          </div>
        </div>
      )}

      {st.engine_installed && (
        <>
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 8 }}>
            Voices are downloaded to the robot. Each is 63–79 MB and stays offline once installed.
          </div>
          {catalog.map((v) => {
            const downloading = busy && job.kind === "voice" && job.target === v.name;
            return (
              <div key={v.name} style={{
                display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                borderTop: "1px solid var(--lm-border, #2a2a2a)",
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12.5, color: C.text }}>{v.language}</div>
                  <div style={{ fontSize: 10.5, color: C.textMuted }}>
                    {v.name} · {v.license}
                  </div>
                </div>
                {v.installed && voice === v.name ? (
                  <button type="button" style={{ ...smallBtn, opacity: 0.5 }} disabled>In use</button>
                ) : v.installed ? (
                  <>
                    <button type="button" onClick={() => onPickVoice(v.name)} style={smallBtn}>Use</button>
                    {!onlyOneLeft && <button
                      type="button"
                      onClick={() => {
                        if (confirmRemove !== v.name) { setConfirmRemove(v.name); return; }
                        removeVoice(v.name);
                      }}
                      onBlur={() => setConfirmRemove((cur) => (cur === v.name ? "" : cur))}
                      disabled={busy || unreachable}
                      style={{
                        ...smallBtn,
                        color: confirmRemove === v.name ? C.red : C.textMuted,
                        opacity: busy ? 0.5 : 1,
                      }}
                    >{confirmRemove === v.name ? "Confirm" : "Remove"}</button>}
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => postAndRefresh(() => installPiperVoice(v.name))}
                    disabled={busy || unreachable}
                    style={{ ...smallBtn, opacity: busy ? 0.5 : 1 }}
                  >{downloading ? `${job.percent}%` : `Download ${v.size_mb} MB`}</button>
                )}
              </div>
            );
          })}
        </>
      )}

      {unreachable && (
        <div style={{ fontSize: 11.5, color: C.textMuted, marginTop: 8 }}>
          Robot is restarting — reconnecting…
        </div>
      )}
      {notice && (
        <div style={{ fontSize: 11.5, color: C.red, marginTop: 8 }}>{notice}</div>
      )}
      {job.error && (
        <div style={{ fontSize: 11.5, color: C.red, marginTop: 8 }}>Last job failed: {job.error}</div>
      )}
    </div>
  );
}

/** Bytes as decimal MB, matching the unit the catalogue quotes on the button. */
function mb(bytes: number): string {
  return (bytes / 1e6).toFixed(1);
}

/** Human name for a voice being downloaded, falling back to its model id. */
function voiceLabel(st: PiperStatus, name: string): string {
  return st.catalog.find((c) => c.name === name)?.language || name;
}

const smallBtn: React.CSSProperties = {
  fontSize: 11, padding: "4px 9px", borderRadius: 5, cursor: "pointer",
  background: "var(--lm-surface, #222)", color: "var(--lm-text, #eee)",
  border: "1px solid var(--lm-border, #333)", whiteSpace: "nowrap",
};
