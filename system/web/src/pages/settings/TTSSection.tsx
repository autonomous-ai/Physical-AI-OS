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
  // The choice the STORED key belongs to. A key is always saved alongside the
  // provider selected at the time, so the loaded (base_url, provider) pair
  // identifies its owner. Once the operator picks a different choice, that key
  // is a different vendor's and must not be advertised as configured.
  choice: ProviderChoice;
}

// Provider "choice" is a UI-only construct — it groups the two on-disk fields
// (`tts_provider` + `tts_base_url`) into one dropdown the operator picks first,
// so they can't accidentally paste a Deepgram STT URL under an ElevenLabs
// provider (a real bug reported: /listen is STT, TTS wants /speak, so
// Test Voice silently 404'd). Save-time the choice is decomposed back into
// the two on-wire fields — the backend contract does not change.
//
// Autonomous is special: it's a routing hub whose proxy path decides which
// vendor the request is billed to. So picking "Autonomous" also asks for a
// vendor (OpenAI, ElevenLabs or Gemini) — that vendor becomes `tts_provider` while

// Vendor covers only the choices that have a distinct audio backend on disk.
// Autonomous supports three vendors; every other choice has exactly one vendor
// (matching its provider name).
type Vendor = "openai" | "elevenlabs" | "gemini";

interface ChoiceMeta {
  label: string;
  baseUrl: string;   // pinned URL; blank for custom (user picks)
  vendor?: Vendor;   // omitted for autonomous (asks separately) and custom
  hint?: string;
}
const CHOICES: Record<ProviderChoice, ChoiceMeta> = {
  autonomous: {
    label: "Autonomous (proxy)",
    baseUrl: "https://campaign-api.autonomous.ai/api/v1/ai/v1",
    // Vendor deliberately UNSET — Autonomous routes to two backends and the
    // operator picks between them via the sub-picker below. The MQTT path
    // that web.autonomous.ai uses can drive OpenAI TTS through this proxy
    // successfully, so exposing OpenAI here is required even though a
    // direct probe of POST /audio/speech from the device sometimes returns
    // a chatcmpl-error body (key-tier issue; the endpoint is real).
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
    // No vendor sub-picker and no key: synthesis happens here, so there is
    // no account to authenticate and no shared quota to share. Voices are
    // the .onnx models installed on the device, listed by HAL.
    hint: "Runs on the robot — no API key, no quota, works offline. Lower quality than a hosted voice.",
  },
  custom: {
    label: "Custom (BYO URL)",
    baseUrl: "",
    hint: "Bring-your-own URL — for self-hosted proxies (vLLM, LiteLLM, …)",
  },
};


// Voices are provider-scoped: OpenAI's "alloy" doesn't exist on ElevenLabs.
// The full catalog comes from `ttsVoices` (server-derived), but that list is
// often for ALL providers mixed. When we know the vendor we prefer the
// hand-curated per-vendor list so the picker only shows names the vendor
// actually accepts — the server-side test would 404 otherwise.
// Supported voice languages. Mirrors hal/presets.py::SUPPORTED_LANGS and
// hal/drivers/voice/tts/elevenlabs.py's language bucket routing (zh-CN and
// zh-TW share the same voice pool). "" = auto (falls back to the current
// sttLanguage; no filtering).
type Lang = "" | "en" | "vi" | "zh-CN" | "zh-TW";
const LANG_LABEL: Record<Lang, string> = {
  "":      "Auto (follow robot language)",
  "en":    "English",
  "vi":    "Vietnamese",
  "zh-CN": "Chinese (Simplified)",
  "zh-TW": "Chinese (Traditional)",
};
const LANG_OPTIONS: Lang[] = ["", "en", "vi", "zh-CN", "zh-TW"];

// Voice pools split by (vendor, language bucket). ElevenLabs voice NAMES
// are resolved to voice_ids server-side by HAL's mapping
// (hal/drivers/voice/tts/elevenlabs.py::VOICE_IDS_BY_LANG). Names not in
// that mapping get passed literally to the API and 404 with voice_not_found
// — keep this list a strict subset of HAL's mapping so every pick is
// guaranteed to resolve.
//
// OpenAI's TTS voices are language-agnostic (the model handles multilingual
// input with the same voice IDs), so the same list serves every language.
type LangBucket = "en" | "vi" | "zh";
function langBucket(lang: Lang): LangBucket {
  if (lang === "vi") return "vi";
  if (lang === "zh-CN" || lang === "zh-TW") return "zh";
  return "en";  // "" (auto) resolves via sttLanguage → this default is safe
}
const OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"];
// Gemini prebuilt voices are multilingual. Mirrors
// hal/drivers/voice/tts/gemini.py::GeminiTTSBackend.VOICES.
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
      // Female — top picks
      "Rachel", "Sarah", "Nicole", "Terra", "Maria", "Sophie",
      "Piper", "Mia", "Kimmy", "Brianna", "Ally", "Tori",
      // Male — top picks
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
  // "auto" (empty lang) falls back to the device's STT language so the TTS
  // voice list stays consistent with what the operator hears the device
  // recognise. Guards against a mis-typed sttLanguage by dropping through
  // to English.
  const effective = lang || (sttLang as Lang) || "en";
  return VOICES[vendor][langBucket(effective)];
}

// Display label for a voice name. Values on the wire stay whatever the vendor
// expects (OpenAI + Deepgram use lowercase IDs; ElevenLabs uses TitleCase),
// but the picker capitalises the first letter and cleans up hyphens so the
// UI reads consistently — no "alloy" beside "Rachel". Value posted to the
// server is unchanged; this only affects rendering.
function displayVoice(v: string): string {
  if (!v) return v;
  // For hyphen-separated IDs (Deepgram aura-asteria-en), keep the first token
  // capitalised and drop the trailing lang suffix so the picker is readable.
  const parts = v.split("-");
  if (parts.length > 1) {
    const named = parts.slice(0, 2).map((p, i) =>
      i === 0 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1)
    ).join(" ");
    return named;
  }
  return v[0].toUpperCase() + v.slice(1);
}

// Edit-mode TTS exposes the api key + base URL fields so operators can override
// them per-section. Setup hides those because they auto-mirror from AI Brain.
export function TTSSection({
  active,
  ttsLoaded, llmLoaded,
  ttsApiKey, setTtsApiKey, setTtsApiKeyRaw,
  ttsBaseUrl, setTtsBaseUrl,
  // ttsProviders is kept in the props signature so a future switch back to a
  // server-driven provider list is a drop-in swap.
  ttsProvider, setTtsProvider, ttsProviders: _ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  ttsSpeed, setTtsSpeed,
  sttLanguage,
}: {
  active: boolean;
  ttsLoaded: TtsLoadedState;
  llmLoaded: LlmLoadedState;
  ttsApiKey: string; setTtsApiKey: (v: string) => void;
  // Unmirrored setter. onChoice must bypass the AI-Brain mirror: it runs before
  // this switch's setTtsBaseUrl has landed, so the mirror would evaluate the
  // OUTGOING provider and could refill a direct vendor's box with the AI brain
  // key. The visible input keeps the mirrored setter.
  setTtsApiKeyRaw: (v: string) => void;
  ttsBaseUrl: string; setTtsBaseUrl: (v: string) => void;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  ttsSpeed: number; setTtsSpeed: (v: number) => void;
  sttLanguage: string;
}) {
  // Choice is stored as state (not derived) so the operator can pick
  // "Custom (BYO URL)" while the on-disk URL still matches a preset host —
  // a pure derivation would snap the dropdown back to that preset the
  // moment we let the click land, because the operator hasn't typed a new
  // URL yet. Initialised from the URL on first mount; re-synced when the
  // URL changes externally (config save, prop reload) but NOT when the
  // current choice is "custom" (they own the URL then, don't yank them).
  // Re-sync happens during render (not in an effect) by comparing the URL we
  // last synced against: setting state in an effect would cascade an extra
  // render pass on every URL change.
  const [choice, setChoice] = useState<ProviderChoice>(() => detectChoice(ttsBaseUrl, ttsProvider));
  // Session-only cache of keys the operator TYPED but has not saved. Never
  // persisted: it dies on reload and on Save, and the device stores exactly one
  // key — the one belonging to the selected provider. Its only job is that
  // flipping ElevenLabs → Autonomous → ElevenLabs before saving doesn't force a
  // retype. Keyed by choice, not vendor: "autonomous" and "elevenlabs" can
  // share a vendor while needing entirely different credentials.
  const keyDrafts = useRef<Partial<Record<ProviderChoice, string>>>({});
  const [syncedUrl, setSyncedUrl] = useState(ttsBaseUrl);
  if (syncedUrl !== ttsBaseUrl) {
    setSyncedUrl(ttsBaseUrl);
    if (choice !== "custom") setChoice(detectChoice(ttsBaseUrl, ttsProvider));
  }
  const meta = CHOICES[choice];

  // Vendor for the current choice. Autonomous defers to the on-disk
  // tts_provider (openai / elevenlabs); every other preset pins vendor via
  // meta.vendor; custom asks the operator to choose.
  const vendor: Vendor = meta.vendor
    ?? (ttsProvider === "openai" || ttsProvider === "elevenlabs" || ttsProvider === "gemini"
      ? (ttsProvider as Vendor)
      : "elevenlabs");

  // One range for every provider: outside 0.5–2.0 speech stops sounding
  // natural, and HAL applies tempo locally where the provider has no speed knob.
  const speedMin = 0.5;
  const speedMax = 2.0;
  // Show the selectable rate without changing a saved legacy value
  // when the user edits another setting. Only a slider action changes it.
  const effectiveSpeed = Math.max(speedMin, Math.min(speedMax, ttsSpeed));

  // Language picker — local state (not persisted server-side). Voice list
  // filters by this; empty means "follow the device's STT language".
  // ElevenLabs voice pools differ per language (Rachel is English, Ngan is
  // Vietnamese, Amy is Chinese) — surfacing the language selector lets the
  // operator pick a Vietnamese voice without the picker being cluttered by
  // English names, and vice-versa. OpenAI's voices are language-agnostic,
  // so the language dropdown is a no-op there (voice list stays the same).
  const [lang, setLang] = useState<Lang>("");
  // Voices present on the device, reported by the Piper panel. Used to keep
  // Test Voice from firing at a model that is still downloading — HAL answers
  // that with a bare 503 that reads like the API died.
  const [piperInstalled, setPiperInstalled] = useState<string[]>([]);
  // Language of the Piper voice itself, parsed from its name. Used for the
  // preview phrase so Test Voice speaks the language the model was trained on.
  const piperLang: string = choice === "piper" ? (ttsVoice.split("_")[0] || "") : "";
  // Piper's catalogue is whatever .onnx files exist on the device, which only
  // the server knows; the curated pools above describe hosted vendors.
  // For Piper the panel below is the better source: it polls HAL directly, so
  // the list follows a download or a removal the moment it finishes, and a
  // transient failure leaves the last good answer in place instead of blanking
  // the picker. The page-level `ttsVoices` fetch is one snapshot taken when the
  // provider changed, and nothing refetches it when a model appears on disk.
  const voices = choice === "piper"
    ? piperInstalled
    : voicesFor(vendor, lang, sttLanguage);

  // Direct vendors authenticate with their own credential. Autonomous and
  // Custom inherit the AI-brain key through the backend's GetTTSAPIKey
  // fallback; Piper needs none at all.
  const keyRequired = choice === "openai" || choice === "elevenlabs";
  // A stored key belongs to the choice it was saved under. Once the operator
  // picks a different one it is a different vendor's key, and advertising it as
  // "configured" is what made issue #309 impossible to diagnose from the UI.
  const storedKeyIsForThisChoice = ttsLoaded.apiKey && ttsLoaded.choice === choice;

  const onChoice = (next: ProviderChoice) => {
    // Re-picking the current choice must not disturb the draft cache: it would
    // stash the live value over itself and then restore it, which is a no-op
    // today but becomes a footgun the moment a branch below stops being pure.
    if (next === choice) return;
    keyDrafts.current[choice] = ttsApiKey;
    // Restore whatever was last typed for the incoming choice. Autonomous and
    // Piper never carry their own key: autonomous inherits the AI-brain key via
    // the backend's GetTTSAPIKey fallback, and piper synthesises on-device with
    // no account to authenticate to. Direct vendors and Custom get their draft
    // back, or a blank box if they have none.
    setTtsApiKeyRaw(next === "autonomous" || next === "piper" ? "" : (keyDrafts.current[next] ?? ""));
    setChoice(next);   // always commit the pick — even Custom, so the picker doesn't snap back
    const nextMeta = CHOICES[next];
    // Custom: leave URL as-is if there was one — clearing would wipe a
    // useful value; the URL input is editable so the operator picks up
    // where they were. Vendor: keep whatever was on disk; the Custom vendor
    // sub-picker below lets them flip protocol.
    if (next === "custom") {
      return;
    }
    if (next === "piper") {
      // No URL and no key to set. Clearing the URL matters: detectChoice reads
      // it on reload, and a stale hosted URL would drag the picker back.
      setTtsBaseUrl("");
      setTtsProvider("piper");
      // Only ever keep a voice the device actually has. `ttsVoices` cannot
      // answer that here: it is fetched per provider and still holds the
      // *previous* provider's list at this instant, so a hosted name like
      // "Rachel" passes its includes() check and survives the switch — which
      // then saves the device as provider=piper, voice=Rachel, a model it can
      // never load. The panel's own listing is the only authority, and empty
      // is the honest answer until it has one.
      if (!piperInstalled.includes(ttsVoice)) setTtsVoice("");
      return;
    }
    setTtsBaseUrl(nextMeta.baseUrl);
    if (next === "autonomous") {
      // Autonomous supports 3 vendors — preserve the current vendor if it's
      // already OpenAI/ElevenLabs/Gemini; else default to ElevenLabs (the
      // historical default that the proxy has always accepted).
      if (ttsProvider !== "openai" && ttsProvider !== "elevenlabs" && ttsProvider !== "gemini") {
        setTtsProvider("elevenlabs");
        setTtsVoice(voicesFor("elevenlabs", lang, sttLanguage)[0]);
      }
      // The key is cleared at the top of onChoice, together with every other
      // choice's key handling — a stale vendor key left here would be sent to
      // the proxy and 401 into silence.
      return;
    }
    // Direct presets pin a vendor via nextMeta.vendor. Sync provider + reset
    // voice when the vendor changes so the voice picker doesn't offer names
    // that don't exist on the new vendor.
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
    // Snap voice to a valid choice for the new language: if the current
    // voice isn't in the target pool, jump to the first entry. Otherwise
    // keep the operator's pick (they may want to hop between languages
    // without losing "Rachel").
    const pool = voicesFor(vendor, next, sttLanguage);
    if (!pool.includes(ttsVoice) && pool.length > 0) {
      setTtsVoice(pool[0]);
    }
  };

  return (
    <SectionCard id="tts" title="Voice" active={active}>
      {/* 1. Provider FIRST — decides URL + vendor. Read-only URL below
          removes the "I pasted a wrong URL" foot-gun. */}
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

      {/* 2a. Piper install panel — engine first, then voices. Only Piper needs
          this: every other provider is a URL that already exists. */}
      {choice === "piper" && (
        <PiperPanel
          voice={ttsVoice}
          onPickVoice={setTtsVoice}
          onInstalledChange={setPiperInstalled}
        />
      )}

      {/* 2. Vendor sub-picker — only when Autonomous. Other presets pin
          vendor via their meta.vendor. */}
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

      {/* 3. Base URL — read-only for presets, editable ONLY for Custom.
          Preset URLs come from the CHOICES table, not from the operator's
          keyboard — a wrong URL was the reported bug. */}
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

      {/* 4. Vendor picker for Custom — operator picks the vendor protocol
          their custom proxy speaks (OpenAI-compat / ElevenLabs-compat / …). */}
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

      {/* 5. API Key — always editable. Blank inherits AI brain key. Hidden for
          Piper: there is no service to authenticate to. */}
      {choice !== "piper" && (<>
      {/* Label shows a "configured" badge when tts_api_key is on file so
          the operator has visual confirmation the save landed — the actual
          value never leaves the device (server returns has_tts_api_key
          only), and the input starts empty on reload so we can't render
          the real chars. Placeholder shows "•••••••• saved" for the same
          reason: an empty box after a save reads as "nothing was saved". */}
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

      {/* 6a. Language — filters the Voice list. Session-local (not saved to
          config). Autonomous(ElevenLabs) has distinct voice pools per
          language (English "Rachel" vs Vietnamese "Ngan" vs Chinese "Amy"),
          so surfacing this picker lets the operator narrow the Voice
          picker without hunting through a mixed list. */}
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

      {/* 6b. Voice — options filter to the vendor AND language so the picker
          doesn't offer names that don't exist on the target combination
          (server would 404). */}
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
            // Read from what the device has, not from what is selected. The
            // selection can still hold the previous provider's voice ("Rachel"),
            // and inferring "still downloading" from a name that is simply not a
            // Piper voice announced a download that was never started.
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

// Local button with a 4-state loading feedback loop so a click stops looking
// like the UI is frozen: idle → loading (spinner + "Sending…") → played
// ("Playing on device") for ~2.5s → back to idle. Errors flip to a red
// "Failed" state for the same window. Prior version fired-and-forgot with no
// visual change — the operator saw nothing happen and clicked again.
function TestVoiceButton({ voice, lang, provider, baseUrl, apiKey, speed, blockedReason = "" }: {
  voice: string;
  lang: string;
  provider: string;
  // Non-empty when the device cannot possibly speak yet — a Piper voice whose
  // .onnx is still downloading. Pressing through would reach a backend that
  // reports itself unavailable, and HAL answers that with a bare 503 that
  // reads like the whole API fell over. Say what is actually happening.
  blockedReason?: string;
  // Pending URL / key from the parent's state — sent alongside the test so
  // the operator's un-saved edits are validated (not the on-disk config).
  // Empty strings fall back to saved config server-side.
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

  // Colour + label track the phase. Loading disables clicks so a slow proxy
  // (5s+ TTFB observed on OpenAI TTS via the campaign-api proxy) can't be
  // re-fired mid-request and stack up multiple synths on the same speaker.
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

// PiperPanel — install state for the on-device engine, and the voice
// catalogue with a download button per entry.
//
// Two steps in order, because that is the real dependency: the engine has to
// exist before a voice can be loaded. The panel refuses to let the operator
// skip ahead rather than letting them download 63 MB that cannot be used yet.
//
// Licence is shown per voice on purpose. Every entry here is safe to ship,
// but "CC BY 4.0" means someone owes an attribution line, and that obligation
// is invisible unless the person choosing the voice can see it.
// Every button here carries type="button". The panel renders inside the
// settings <form>, where a button defaults to type="submit" — so Download, Use
// and Remove were each submitting the whole form, saving the config and
// restarting HAL underneath the very request they had just fired. That is what
// killed downloads mid-transfer, lost Remove clicks to a 502, and made the
// device announce "Be right back" on a click that was supposed to touch
// nothing but /opt/piper.
function PiperPanel({ voice, onPickVoice, onInstalledChange }: {
  voice: string;
  onPickVoice: (v: string) => void;
  onInstalledChange: (installed: string[]) => void;
}) {
  const [st, setSt] = useState<PiperStatus | null>(null);
  // Not an error state. Saving a voice change restarts HAL, so the status call
  // is refused for a normal 10-15s window every time the operator hits Save.
  // Treating that as a failure left a red Go error on screen that only a page
  // reload could clear — the panel now just keeps asking until HAL answers.
  const [unreachable, setUnreachable] = useState(false);
  // Voice whose Remove has been pressed once. Deleting a 63 MB model that
  // takes minutes to fetch again deserves a second press, and an inline
  // confirm keeps that in the row instead of behind a browser dialog.
  const [confirmRemove, setConfirmRemove] = useState("");
  // What HAL refused, when it refuses. These endpoints answer a rejection with
  // 200 and {status:"error"}, so without this the button would appear to do
  // nothing at all.
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

  // HAL claims the job before it replies, so the reply already describes the
  // running download. Adopting it here is what makes the panel react to the
  // click itself: polling only runs while a job is active, so a panel that
  // waited for the next poll to discover the job could miss it starting and
  // then never look again.
  const postAndRefresh = useCallback((run: () => Promise<PiperJobStart>) => {
    run()
      .then((res) => {
        setNotice(res.status === "error" ? res.message || "Request refused" : "");
        const started = res.job;
        if (started) setSt((prev) => (prev ? { ...prev, job: started } : prev));
        load();
      })
      .catch(() => {
        // Almost always a HAL restart: saving any voice setting triggers one,
        // and a click landing in that window is simply lost. Saying only
        // "reconnecting" would let the operator believe the voice was removed.
        setUnreachable(true);
        setNotice("Robot was restarting — nothing changed. Try again in a moment.");
      });
  }, [load]);

  // Voices whose removal is in flight. The row has to update on the confirm
  // press, but the removal itself can take ten seconds when HAL is restarting,
  // and the status poll keeps running throughout — each poll reports the voice
  // as still installed, which put the Remove button straight back and made the
  // confirm look ignored. Masking the polled truth is what holds the row down;
  // mutating it would just be overwritten again by the next poll.
  const [removing, setRemoving] = useState<string[]>([]);

  const removeVoice = useCallback((name: string) => {
    setNotice("");
    setConfirmRemove("");
    setRemoving((cur) => [...cur, name]);
    removePiperVoice(name)
      .then((res) => {
        if (res.status === "error") setNotice(res.message || "Request refused");
        // Lift the mask only once real status is in, or the row would flash
        // back for the moment between the two.
        return load();
      })
      .catch(() => {
        setUnreachable(true);
        setNotice("Robot was restarting — nothing changed. Try again in a moment.");
      })
      .finally(() => setRemoving((cur) => cur.filter((n) => n !== name)));
  }, [load]);

  // Poll while a download runs (the one time this page changes on its own),
  // and while HAL is unreachable so the panel recovers by itself.
  useEffect(() => {
    const busy = !!st?.job?.active;
    if (!busy && !unreachable && st) return;
    const t = setInterval(load, busy ? 2000 : 3000);
    return () => clearInterval(t);
  }, [st, unreachable, load]);

  useEffect(() => {
    if (st) onInstalledChange(st.voices_installed.filter((n) => !removing.includes(n)));
  }, [st, removing, onInstalledChange]);

  // No status yet and HAL is not answering: almost always a restart in flight.
  if (!st) {
    return (
      <div style={{ fontSize: 12, color: C.textMuted, marginBottom: 12 }}>
        {unreachable ? "Robot is restarting — reconnecting…" : "Checking robot…"}
      </div>
    );
  }

  const job = st.job;
  const busy = job.active;
  // What the panel renders: polled truth with in-flight removals taken out.
  const catalog = st.catalog.map((c) =>
    removing.includes(c.name) ? { ...c, installed: false } : c);
  // HAL refuses to delete the last model — the device would have nothing to
  // speak with. Hiding the button is better than offering one that answers
  // with a refusal ten seconds later.
  const onlyOneLeft = catalog.filter((c) => c.installed).length <= 1;

  return (
    <div style={{ marginBottom: 14, padding: "12px 14px", background: "var(--lm-surface-2, #1a1a1a)", borderRadius: 8 }}>
      {/* Step 1 — the engine. Shown only while it is missing: once installed
          the line carries no information the voice list below does not already
          imply, and a permanent green tick on a finished setup step is just
          something to read past every time. */}
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

      {/* The download in flight. Given its own row rather than a number on the
          button it was started from: on a domestic connection 63 MB takes
          minutes, and for all of them this is the only thing on the page that
          is happening. */}
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

      {/* Step 2 — voices. Hidden until the engine exists: downloading a model
          the device cannot load yet is 63 MB of wasted bandwidth. */}
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
                    {/* Licence shown, obligation not: attribution is owed by
                        whoever distributes the voice, not by the person
                        switching it on here. CREDITS.md is what discharges it. */}
                    {v.name} · {v.license}
                  </div>
                </div>
                {v.installed && voice === v.name ? (
                  // No Remove here: deleting the voice being spoken with would
                  // drop the device to a fallback mid-sentence, or to silence
                  // if it were the only one. Switch first, then remove.
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

/** Bytes as decimal MB, matching the unit the catalogue quotes on the button.
 *  Using MiB here instead would show 60.6 under a button that promised 64. */
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
