import { useEffect, useRef, useState, useCallback } from "react";
import { toast } from "sonner";
import { getDeviceConfig, updateDeviceConfig, getTTSVoices, getTTSProviders, hwUrl, restoreAutonomousDefaults } from "@/lib/api";
import type { DeviceConfig } from "@/lib/api";
import type { ChannelType } from "@/types";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";
import { C, ADMIN_PASSWORD_MIN } from "@/components/setup/shared";
import { DeviceSection } from "@/components/setup/DeviceSection";
import { LLMSection, type LlmMode } from "@/components/setup/LLMSection";
import { RestoreDefaultsButton } from "@/components/setup/shared";
import { WifiSection } from "@/pages/settings/WifiSection";
import { VoiceSection as EditVoiceSection } from "@/pages/settings/VoiceSection";
import { FaceSection as EditFaceSection } from "@/pages/settings/FaceSection";
import { TTSSection, type TtsLoadedState } from "@/pages/settings/TTSSection";
import { detectChoice } from "@/pages/settings/ttsProvider";
import { RealtimeSection } from "@/pages/settings/RealtimeSection";
import { AgentRuntimeSection } from "@/pages/settings/AgentRuntimeSection";
import { TimezoneSection } from "@/pages/settings/TimezoneSection";
import { STTSection, type SttProvider } from "@/pages/settings/STTSection";
import { ChannelSection } from "@/pages/settings/ChannelSection";
import { MqttSection } from "@/pages/settings/MqttSection";
import { MCPToolsSection } from "@/pages/settings/MCPToolsSection";
import { PluginsSection } from "@/pages/settings/PluginsSection";
import { ScheduledSection } from "@/pages/settings/ScheduledSection";
import { FacebookSection } from "@/pages/settings/FacebookSection";

// The set of sections this panel can render. Controlled by the parent now (the
// page shell owns the sidebar / active-section state). `stt` is the Language
// section (rendered under id="stt"), matching the legacy /edit layout. `runtime`
// is the agent-backend switch (its own Switch button, not part of Save).
// `scheduled` is read-only (see ScheduledSection's doc comment) — no Save flow.
export type SettingsSectionId = "device" | "wifi" | "llm" | "runtime" | "voice" | "face" | "tts" | "realtime" | "stt" | "channel" | "mqtt" | "mcp" | "plugins" | "timezone" | "scheduled" | "facebook";

// Header-row label lookup. Kept local so the panel can render the active-section
// title above the form without depending on the page's NAV_GROUPS config.
const SECTION_LABELS: Record<SettingsSectionId, string> = {
  device: "General",
  wifi: "Wi-Fi",
  llm: "AI Brain",
  runtime: "Runtime",
  voice: "My Voice",
  face: "Face",
  tts: "Voice",
  realtime: "Realtime",
  stt: "Language",
  channel: "Channels",
  mqtt: "MQTT",
  mcp: "MCP Tools",
  plugins: "Plugins",
  timezone: "Timezone",
  scheduled: "Scheduled",
  facebook: "Facebook",
};

// Field / LockedField / LockedPasswordField / SectionCard live in
// @/components/setup/shared. SkeletonBlock stays inline because this panel's
// version renders 4 stacked cards whereas Setup's renders just one.

function SkeletonBlock() {
  const bar = (w: string | number, h = 10) => (
    <div style={{ width: w, height: h, borderRadius: 6, background: C.surface, marginBottom: 10 }} />
  );
  return (
    <>
      {[1, 2, 3, 4].map((i) => (
        <div key={i} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: "18px 20px", marginBottom: 16 }}>
          {bar(80, 8)}
          <div style={{ marginTop: 14 }}>{bar("100%", 32)}{bar("100%", 32)}</div>
        </div>
      ))}
    </>
  );
}

// SettingsPanel — the self-contained settings form body. Owns all form state,
// config load/save, and the section components. Renders WITHOUT a sidebar so it
// can be embedded both inside the /edit page shell and inside the Monitor
// dashboard. `activeSection` is controlled by the parent.
export function SettingsPanel({ activeSection }: { activeSection: SettingsSectionId }): React.JSX.Element {
  const [loadingCfg, setLoadingCfg] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // form state
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  // Rotate-admin-password field. Empty = no change; non-empty = bcrypt +
  // replace server-side. Existing session cookie keeps working since it's
  // signed by SessionSecret, not by the password hash.
  const [adminPassword, setAdminPassword] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [mac, setMac] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmUrl, setLlmUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmDisableThinking, setLlmDisableThinking] = useState(false);
  const [deepgramApiKey, setDeepgramApiKey] = useState("");
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
  // STT provider: derived from saved config (deepgram if key present, else autonomous).
  // Default for fresh devices is "autonomous" — uses LLM endpoint as fallback.
  const [sttProvider, setSttProvider] = useState<SttProvider>("autonomous");
  // STT language drives model selection on the server (operators don't pick
  // model directly). Defaults to "en" so a never-configured device lands on
  // English instead of "auto/unset" (which surfaces as a blank dropdown).
  const [sttLanguage, setSttLanguage] = useState("en");
  const [ttsApiKey, setTtsApiKey] = useState("");
  const [ttsBaseUrl, setTtsBaseUrl] = useState("");
  const [ttsProvider, setTtsProvider] = useState("elevenlabs");
  const [ttsProviders, setTtsProviders] = useState<string[]>([]);
  const [ttsVoice, setTtsVoice] = useState("Rachel");
  const [ttsSpeed, setTtsSpeed] = useState(1.2);
  const [ttsVoices, setTtsVoices] = useState<string[]>([]);
  const [realtimeEnabled, setRealtimeEnabled] = useState(true);
  const [wakeWord, setWakeWord] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [wakePhrases, setWakePhrases] = useState<string[]>([]);
  const [realtimeProvider, setRealtimeProvider] = useState("gemini");
  const [realtimeVoice, setRealtimeVoice] = useState("Kore");
  const [realtimeReasoning, setRealtimeReasoning] = useState("MINIMAL");
  const [realtimeApiKey, setRealtimeApiKey] = useState("");
  const [realtimeBaseUrl, setRealtimeBaseUrl] = useState("");
  // pipecat_v1 only: the in-session `web_search` tool. Default on (HAL's).
  const [realtimeWebSearch, setRealtimeWebSearch] = useState(true);
  const [channel, setChannel] = useState<ChannelType>("telegram");
  const [teleToken, setTeleToken] = useState("");
  const [teleUserId, setTeleUserId] = useState("");
  const [slackBotToken, setSlackBotToken] = useState("");
  const [slackAppToken, setSlackAppToken] = useState("");
  const [slackUserId, setSlackUserId] = useState("");
  const [discordBotToken, setDiscordBotToken] = useState("");
  const [discordGuildId, setDiscordGuildId] = useState("");
  const [discordUserId, setDiscordUserId] = useState("");
  // iMessage / BlueBubbles — see ChannelSection's guide for the operator flow
  // (Mac install → password → server URL → iMessage handle).
  const [bluebubblesServerUrl, setBluebubblesServerUrl] = useState("");
  const [bluebubblesPassword, setBluebubblesPassword] = useState("");
  const [bluebubblesUserAddress, setBluebubblesUserAddress] = useState("");
  // Optional plaintext caller-context prompt — see ChannelSection for the
  // operator-facing textarea. Non-secret, so we hydrate the raw string
  // from cfg.bluebubbles_caller_context and ship it verbatim on save.
  const [bluebubblesCallerContext, setBluebubblesCallerContext] = useState("");
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");
  // Snapshot of MQTT fields that were already populated when config loaded.
  // Locks those fields against edits; fields blank at load remain editable.
  const [mqttLoaded, setMqttLoaded] = useState({
    endpoint: false, port: false, username: false,
    password: false, faChannel: false, fdChannel: false,
  });
  // Same idea for messaging-channel credentials. Already-saved values render
  // read-only with an inline "Edit" button to opt-in to changing them.
  const [channelLoaded, setChannelLoaded] = useState({
    teleToken: false, teleUserId: false,
    slackBotToken: false, slackAppToken: false, slackUserId: false,
    discordBotToken: false, discordGuildId: false, discordUserId: false,
    bluebubblesServerUrl: false, bluebubblesPassword: false, bluebubblesUserAddress: false,
    bluebubblesCallerContext: false,
  });
  const [wifiLoaded, setWifiLoaded] = useState({ ssid: false, password: false });
  const [llmLoaded, setLlmLoaded] = useState({ apiKey: false, baseUrl: false, model: false });
  const [ttsLoaded, setTtsLoaded] = useState<TtsLoadedState>({ apiKey: false, baseUrl: false, choice: "autonomous" });
  // True once the device has preserved its shipped credentials — i.e. the
  // operator has replaced one at least once. Nothing to offer before that.
  const [hasDefaults, setHasDefaults] = useState(false);
  // Which brain the AI Brain section is showing. Derived from the config on
  // load — a device whose llm_base_url and llm_model still match the stored
  // Autonomous set is on it, and one that has never been edited is on it by
  // definition — then held locally so picking "Custom" unlocks the fields
  // before anything has been typed into them.
  const [llmMode, setLlmMode] = useState<LlmMode>("autonomous");
  const [realtimeLoaded, setRealtimeLoaded] = useState({ apiKey: false });
  const [sttLoaded, setSttLoaded] = useState({ deepgram: false, apiKey: false, baseUrl: false });

  // Baseline snapshot of non-secret fields captured after load (and after every
  // successful save). Used to gate Save button on dirty-only. Secrets are
  // handled separately: their input state is empty when nothing was typed, so
  // any non-empty secret state implies a pending change.
  type InitialSnapshot = {
    ssid: string; deviceId: string;
    llmUrl: string; llmModel: string; llmDisableThinking: boolean;
    sttBaseUrl: string; sttProvider: SttProvider; sttLanguage: string;
    ttsBaseUrl: string; ttsProvider: string; ttsVoice: string; ttsSpeed: number;
    wakeWord: boolean;
    channel: ChannelType;
    teleUserId: string; slackUserId: string;
    discordGuildId: string; discordUserId: string;
    bluebubblesServerUrl: string; bluebubblesUserAddress: string;
    bluebubblesCallerContext: string;
    mqttEndpoint: string; mqttPort: string; mqttUsername: string;
    faChannel: string; fdChannel: string;
    // Realtime block. Fields tracked here so /setting#realtime edits flip
    // the Save Changes button — earlier they were missing from the baseline
    // and the button stayed disabled no matter what the operator changed.
    realtimeEnabled: boolean;
    realtimeProvider: string;
    realtimeVoice: string;
    realtimeReasoning: string;
    realtimeBaseUrl: string;
    realtimeWebSearch: boolean;
  };
  // Held as state, not a ref: the Save button's disabled/enabled rendering is
  // derived from it, and React 19 requires render-relevant values to be state.
  // Written exactly twice (after config load, after a successful save) — both
  // outside render, so the render output is unchanged.
  const [baseline, setBaseline] = useState<InitialSnapshot | null>(null);

  // Face owners — top-level state because both Voice and Face sections read
  // it. Section-local state (faceName, voiceLabel, etc.) lives in the section
  // components themselves.
  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch(hwUrl("/face/owners")).then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch {
      // Face owners are optional decoration in the settings form — a device
      // without the face capability simply renders an empty list.
    }
  }, []);

  // Rule over-approximates here: loadFaceOwners is async and its only setState
  // runs after `await fetch(...)`, so nothing is set synchronously in the effect
  // body. Fetching the face-owner list on mount is exactly the "subscribe to an
  // external system" case the rule is meant to allow.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { loadFaceOwners(); }, [loadFaceOwners]);

  useEffect(() => {
    getDeviceConfig()
      .then((cfg: DeviceConfig) => {
        // ConfigPublicResponse — secrets are returned as has_* booleans only.
        // State for secret fields stays empty until the operator types a new
        // value in SecretUpdateField; submit then ships only the touched ones.
        setSsid(cfg.network_ssid ?? "");
        setDeviceId(cfg.device_id ?? "");
        setMac(cfg.mac ?? "");
        setLlmUrl(cfg.llm_base_url ?? "");
        setLlmModel(cfg.llm_model ?? "");
        setLlmDisableThinking(cfg.llm_disable_thinking ?? false);
        const llmUrlInit = cfg.llm_base_url ?? "";
        const sttProviderInit: SttProvider = cfg.has_deepgram_api_key ? "deepgram" : "autonomous";
        // Apply client-side fallback values with the async config response,
        // rather than scheduling a second render just to mirror form fields.
        setSttBaseUrl((cfg.stt_base_url ?? "") || (sttProviderInit === "autonomous" ? llmUrlInit : ""));
        setSttProvider(sttProviderInit);
        setSttLanguage(cfg.stt_language || "en");
        setTtsBaseUrl((cfg.tts_base_url ?? "") || llmUrlInit);
        setTtsProvider(cfg.tts_provider || "elevenlabs");
        setTtsVoice(cfg.tts_voice || "Rachel");
        setTtsSpeed(cfg.tts_speed ?? 1.2);
        setWakeWord(cfg.wakeword ?? false);
        setAgentName(cfg.agent_name ?? "");
        setWakePhrases(cfg.wake_phrases ?? []);
        if (cfg.realtime) {
          setRealtimeEnabled(cfg.realtime.enabled ?? true);
          setRealtimeProvider(cfg.realtime.provider || "gemini");
          if (cfg.realtime.voice) setRealtimeVoice(cfg.realtime.voice);
          if (cfg.realtime.reasoning) setRealtimeReasoning(cfg.realtime.reasoning);
          setRealtimeBaseUrl(cfg.realtime.base_url ?? "");
          setRealtimeWebSearch(cfg.realtime.web_search ?? true);
          setRealtimeLoaded({ apiKey: !!cfg.realtime.has_api_key });
        }
        setChannel((cfg.channel as ChannelType) || "telegram");
        setTeleUserId(cfg.telegram_user_id ?? "");
        setSlackUserId(cfg.slack_user_id ?? "");
        setDiscordGuildId(cfg.discord_guild_id ?? "");
        setDiscordUserId(cfg.discord_user_id ?? "");
        setMqttEndpoint(cfg.mqtt_endpoint ?? "");
        setMqttPort(cfg.mqtt_port ? String(cfg.mqtt_port) : "");
        setMqttUsername(cfg.mqtt_username ?? "");
        setFaChannel(cfg.fa_channel ?? "");
        setFdChannel(cfg.fd_channel ?? "");
        setMqttLoaded({
          endpoint: !!cfg.mqtt_endpoint,
          port: !!cfg.mqtt_port,
          username: !!cfg.mqtt_username,
          password: cfg.has_mqtt_password,
          faChannel: !!cfg.fa_channel,
          fdChannel: !!cfg.fd_channel,
        });
        setChannelLoaded({
          teleToken: cfg.has_telegram_bot_token,
          teleUserId: !!cfg.telegram_user_id,
          slackBotToken: cfg.has_slack_bot_token,
          slackAppToken: cfg.has_slack_app_token,
          slackUserId: !!cfg.slack_user_id,
          discordBotToken: cfg.has_discord_bot_token,
          discordGuildId: !!cfg.discord_guild_id,
          discordUserId: !!cfg.discord_user_id,
          bluebubblesServerUrl: !!cfg.bluebubbles_server_url,
          bluebubblesPassword: cfg.has_bluebubbles_password,
          bluebubblesUserAddress: !!cfg.bluebubbles_user_address,
          bluebubblesCallerContext: !!cfg.bluebubbles_caller_context,
        });
        setBluebubblesServerUrl(cfg.bluebubbles_server_url ?? "");
        setBluebubblesUserAddress(cfg.bluebubbles_user_address ?? "");
        setBluebubblesCallerContext(cfg.bluebubbles_caller_context ?? "");
        setWifiLoaded({
          ssid: !!cfg.network_ssid,
          password: cfg.has_network_password,
        });
        setLlmLoaded({
          apiKey: cfg.has_llm_api_key,
          baseUrl: !!cfg.llm_base_url,
          model: !!cfg.llm_model,
        });
        setHasDefaults(!!cfg.has_autonomous_defaults);
        setLlmMode(
          !cfg.has_autonomous_defaults ||
            ((cfg.llm_base_url ?? "") === (cfg.autonomous_default_base_url ?? "") &&
              (cfg.llm_model ?? "") === (cfg.autonomous_default_model ?? ""))
            ? "autonomous"
            : "custom",
        );
        setTtsLoaded({
          apiKey: cfg.has_tts_api_key,
          baseUrl: !!cfg.tts_base_url,
          choice: detectChoice(cfg.tts_base_url, cfg.tts_provider),
        });
        setSttLoaded({
          deepgram: cfg.has_deepgram_api_key,
          apiKey: cfg.has_stt_api_key,
          baseUrl: !!cfg.stt_base_url,
        });
        // Mirror the post-load behavior of the LLM→TTS/STT base-URL auto-fill
        // effects so the baseline matches the rendered state. Without this,
        // a config with llm_base_url but no tts/stt_base_url would show the
        // form as dirty immediately on load.
        setBaseline({
          ssid: cfg.network_ssid ?? "",
          deviceId: cfg.device_id ?? "",
          llmUrl: llmUrlInit,
          llmModel: cfg.llm_model ?? "",
          llmDisableThinking: cfg.llm_disable_thinking ?? false,
          sttBaseUrl: (cfg.stt_base_url ?? "") || (sttProviderInit === "autonomous" ? llmUrlInit : ""),
          sttProvider: sttProviderInit,
          sttLanguage: cfg.stt_language || "en",
          ttsBaseUrl: (cfg.tts_base_url ?? "") || llmUrlInit,
          ttsProvider: cfg.tts_provider || "elevenlabs",
          ttsVoice: cfg.tts_voice || "Rachel",
          ttsSpeed: cfg.tts_speed ?? 1.2,
          wakeWord: cfg.wakeword ?? false,
          channel: (cfg.channel as ChannelType) || "telegram",
          teleUserId: cfg.telegram_user_id ?? "",
          slackUserId: cfg.slack_user_id ?? "",
          discordGuildId: cfg.discord_guild_id ?? "",
          discordUserId: cfg.discord_user_id ?? "",
          bluebubblesServerUrl: cfg.bluebubbles_server_url ?? "",
          bluebubblesUserAddress: cfg.bluebubbles_user_address ?? "",
          bluebubblesCallerContext: cfg.bluebubbles_caller_context ?? "",
          mqttEndpoint: cfg.mqtt_endpoint ?? "",
          mqttPort: cfg.mqtt_port ? String(cfg.mqtt_port) : "",
          mqttUsername: cfg.mqtt_username ?? "",
          faChannel: cfg.fa_channel ?? "",
          fdChannel: cfg.fd_channel ?? "",
          // Mirror the defaults the individual useState calls use, so the
          // baseline reflects what actually rendered — not the server's raw
          // (possibly missing) values. Otherwise `dirty` would flip true on
          // page load for any field the server omitted.
          realtimeEnabled: cfg.realtime?.enabled ?? true,
          realtimeProvider: cfg.realtime?.provider || "gemini",
          realtimeVoice: cfg.realtime?.voice || "Kore",
          realtimeReasoning: cfg.realtime?.reasoning || "MINIMAL",
          realtimeBaseUrl: cfg.realtime?.base_url ?? "",
          realtimeWebSearch: cfg.realtime?.web_search ?? true,
        });
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoadingCfg(false));
    getTTSProviders().then(setTtsProviders).catch(() => {});
    getTTSVoices().then(setTtsVoices).catch(() => {});
  }, []);

  // Refetch voices when provider OR stt_language changes — only reset voice
  // if the currently-saved voice is not in the new (filtered) list.
  // Passing sttLanguage filters ElevenLabs voices to the active language's
  // bucket so VN/CN owners only see voices that sound natural for them.
  const providerChangedByUser = useRef(false);
  useEffect(() => {
    getTTSVoices(ttsProvider, sttLanguage).then((voices) => {
      setTtsVoices(voices);
      if (providerChangedByUser.current && voices.length > 0 && !voices.includes(ttsVoice)) {
        setTtsVoice(voices[0]);
      }
      providerChangedByUser.current = true;
    }).catch(() => {});
  }, [ttsProvider, sttLanguage, ttsVoice]);

  // Mirror AI Brain values in the event that changes a source or re-enables a
  // blank destination. The mirror remains sticky, without scheduling state
  // updates from an effect after React has already committed a render.
  // Only the inheriting choices may be auto-filled from the AI Brain key.
  // OpenAI/ElevenLabs direct reject that credential with a 401 that hal turns
  // into silence, so mirroring into one recreates issue #309 through the back
  // door — and a non-empty (but wrong) key would sail past the save guard.
  // Mirrors the `sttProvider === "autonomous"` guard the STT side already has.
  const ttsInheritsLlmKey = () => {
    const c = detectChoice(ttsBaseUrl, ttsProvider);
    return c === "autonomous" || c === "custom";
  };
  const setMirroredLlmApiKey = (value: string) => {
    setLlmApiKey(value);
    if (ttsInheritsLlmKey() && !ttsApiKey && value && !ttsLoaded.apiKey) setTtsApiKey(value);
    if (sttProvider === "autonomous" && !sttApiKey && value && !sttLoaded.apiKey) setSttApiKey(value);
  };
  const setMirroredLlmUrl = (value: string) => {
    setLlmUrl(value);
    if (!ttsBaseUrl && value) setTtsBaseUrl(value);
    if (sttProvider === "autonomous" && !sttBaseUrl && value) setSttBaseUrl(value);
  };
  const setMirroredTtsApiKey = (value: string) => {
    setTtsApiKey(!value && llmApiKey && !ttsLoaded.apiKey && ttsInheritsLlmKey() ? llmApiKey : value);
  };
  const setMirroredTtsBaseUrl = (value: string) => {
    setTtsBaseUrl(!value && llmUrl ? llmUrl : value);
  };
  const setMirroredSttApiKey = (value: string) => {
    setSttApiKey(!value && sttProvider === "autonomous" && llmApiKey && !sttLoaded.apiKey ? llmApiKey : value);
  };
  const setMirroredSttBaseUrl = (value: string) => {
    setSttBaseUrl(!value && sttProvider === "autonomous" && llmUrl ? llmUrl : value);
  };
  const setMirroredSttProvider = (value: SttProvider) => {
    setSttProvider(value);
    if (value !== "autonomous") return;
    if (!sttApiKey && llmApiKey && !sttLoaded.apiKey) setSttApiKey(llmApiKey);
    if (!sttBaseUrl && llmUrl) setSttBaseUrl(llmUrl);
  };

  // Dirty = any non-secret field diverges from the loaded/last-saved baseline,
  // OR any secret field has user-typed content. Save button uses this to stay
  // disabled until something actually changed.
  const dirty = !loadingCfg && baseline != null && (
    ssid !== baseline.ssid ||
    deviceId !== baseline.deviceId ||
    llmUrl !== baseline.llmUrl ||
    llmModel !== baseline.llmModel ||
    llmDisableThinking !== baseline.llmDisableThinking ||
    sttBaseUrl !== baseline.sttBaseUrl ||
    sttProvider !== baseline.sttProvider ||
    sttLanguage !== baseline.sttLanguage ||
    ttsBaseUrl !== baseline.ttsBaseUrl ||
    ttsProvider !== baseline.ttsProvider ||
    ttsVoice !== baseline.ttsVoice ||
    ttsSpeed !== baseline.ttsSpeed ||
    wakeWord !== baseline.wakeWord ||
    channel !== baseline.channel ||
    teleUserId !== baseline.teleUserId ||
    slackUserId !== baseline.slackUserId ||
    discordGuildId !== baseline.discordGuildId ||
    discordUserId !== baseline.discordUserId ||
    mqttEndpoint !== baseline.mqttEndpoint ||
    mqttPort !== baseline.mqttPort ||
    mqttUsername !== baseline.mqttUsername ||
    faChannel !== baseline.faChannel ||
    fdChannel !== baseline.fdChannel ||
    // Realtime block: without these, toggling Enabled / picking a provider /
    // pasting a Base URL on /setting#realtime silently produced NO change to
    // dirty and the Save button never enabled.
    realtimeEnabled !== baseline.realtimeEnabled ||
    realtimeProvider !== baseline.realtimeProvider ||
    realtimeVoice !== baseline.realtimeVoice ||
    realtimeReasoning !== baseline.realtimeReasoning ||
    realtimeBaseUrl !== baseline.realtimeBaseUrl ||
    realtimeWebSearch !== baseline.realtimeWebSearch ||
    !!password || !!adminPassword || !!llmApiKey || !!ttsApiKey ||
    !!sttApiKey || !!deepgramApiKey || !!mqttPassword ||
    !!teleToken || !!slackBotToken || !!slackAppToken || !!discordBotToken ||
    !!realtimeApiKey ||
    // iMessage: server URL + handle are plain fields (dirty when changed),
    // password is a secret (dirty when typed).
    bluebubblesServerUrl !== baseline.bluebubblesServerUrl ||
    bluebubblesUserAddress !== baseline.bluebubblesUserAddress ||
    bluebubblesCallerContext !== baseline.bluebubblesCallerContext ||
    !!bluebubblesPassword
  );

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Admin password rotation is optional here (empty = keep current). But when
    // the operator IS rotating, hold them to the same ADMIN_PASSWORD_MIN floor as
    // initial setup so /edit can't be used to weaken the admin login below the
    // policy the setup flow enforces. Backend has no min-length, so this is the gate.
    if (adminPassword && adminPassword.length < ADMIN_PASSWORD_MIN) {
      setError(`New admin password must be at least ${ADMIN_PASSWORD_MIN} characters.`);
      return;
    }
    // A direct vendor is authenticated with its own key. Blank means the
    // backend falls back to the AI-brain key — an Autonomous JWT — which the
    // vendor rejects with a 401 that hal retries, gives up on, and turns into
    // zero samples. The device goes mute with no error anywhere in this UI, so
    // refuse the save instead of shipping a silent device. Issue #309.
    const ttsChoiceToSave = detectChoice(ttsBaseUrl, ttsProvider);
    const ttsNeedsOwnKey = ttsChoiceToSave === "openai" || ttsChoiceToSave === "elevenlabs";
    if (ttsNeedsOwnKey && !ttsApiKey && !(ttsLoaded.apiKey && ttsLoaded.choice === ttsChoiceToSave)) {
      setError(`${ttsChoiceToSave === "openai" ? "OpenAI" : "ElevenLabs"} (direct) needs its own API key — the AI brain key will not work with it.`);
      return;
    }
    setSaving(true);
    try {
      // Build the payload from non-secret fields first, then layer on each
      // secret only when the operator typed something into its
      // SecretUpdateField. Empty secrets would otherwise clobber the saved
      // value on disk (PUT treats blanks as intentional clears for STT /
      // Deepgram). Channel id fields (telegram_user_id, slack_user_id,
      // discord_guild_id, discord_user_id) are non-secret and ship every time.
      const body: Record<string, unknown> = {
        ssid: ssid.trim(),
        channel,
        llm_base_url: llmUrl, llm_model: llmModel,
        llm_disable_thinking: llmDisableThinking,
        stt_base_url: sttBaseUrl, stt_language: sttLanguage,
        tts_base_url: ttsBaseUrl, tts_provider: ttsProvider, tts_voice: ttsVoice, tts_speed: ttsSpeed,
        device_id: deviceId,
        mqtt_endpoint: mqttEndpoint, mqtt_username: mqttUsername,
        mqtt_port: mqttPort ? parseInt(mqttPort, 10) : 0,
        fa_channel: faChannel, fd_channel: fdChannel,
      };
      if (password) body.password = password;
      if (adminPassword) body.admin_password = adminPassword;
      // Realtime block — server applies + restarts hal. api_key only when typed.
      const realtime: Record<string, unknown> = { enabled: realtimeEnabled, provider: realtimeProvider };
      if (realtimeProvider !== "none") { realtime.voice = realtimeVoice; realtime.reasoning = realtimeReasoning; }
      // The server rejects web_search for any other provider, so send it only
      // where the knob exists.
      if (realtimeProvider === "pipecat_v1") realtime.web_search = realtimeWebSearch;
      if (realtimeBaseUrl) realtime.base_url = realtimeBaseUrl;
      if (realtimeApiKey) realtime.api_key = realtimeApiKey;
      body.realtime = realtime;
      body.wakeword = wakeWord;
      if (llmApiKey) body.llm_api_key = llmApiKey;
      // The stored TTS key belongs to whichever provider was selected when it
      // was saved. Switching provider makes it a different vendor's credential,
      // and GetTTSAPIKey would hand it straight to the new one — an ElevenLabs
      // sk_... sent to the Autonomous proxy, or a proxy JWT sent to ElevenLabs.
      // Both 401, and hal turns a 401 into silence, so delete it explicitly.
      // See issue #309.
      if (ttsApiKey) {
        body.tts_api_key = ttsApiKey;
      } else if (ttsLoaded.apiKey && ttsLoaded.choice !== ttsChoiceToSave) {
        body.clear_tts_api_key = true;
      }
      if (mqttPassword) body.mqtt_password = mqttPassword;
      // STT provider switch: clear the opposing key explicitly so the
      // operator's mode toggle takes effect. When staying on the same provider
      // and not typing a new key, leave both fields untouched.
      if (sttProvider === "deepgram") {
        if (deepgramApiKey) body.deepgram_api_key = deepgramApiKey;
        if (sttLoaded.apiKey || sttApiKey) body.stt_api_key = "";
      } else {
        if (sttApiKey) body.stt_api_key = sttApiKey;
        if (sttLoaded.deepgram || deepgramApiKey) body.deepgram_api_key = "";
      }
      // Channel credentials: send IDs always, tokens only when typed.
      if (channel === "telegram") {
        body.telegram_user_id = teleUserId;
        if (teleToken) body.telegram_bot_token = teleToken;
      } else if (channel === "slack") {
        body.slack_user_id = slackUserId;
        if (slackBotToken) body.slack_bot_token = slackBotToken;
        if (slackAppToken) body.slack_app_token = slackAppToken;
      } else if (channel === "discord") {
        body.discord_guild_id = discordGuildId;
        body.discord_user_id = discordUserId;
        if (discordBotToken) body.discord_bot_token = discordBotToken;
      } else if (channel === "imessage") {
        // BlueBubbles: server URL + user address are plain (send always so
        // clearing them works too); password is a secret and only ships on
        // change so an empty save does not silently wipe the on-disk value.
        body.bluebubbles_server_url = bluebubblesServerUrl;
        body.bluebubbles_user_address = bluebubblesUserAddress;
        if (bluebubblesPassword) body.bluebubbles_password = bluebubblesPassword;
        // Caller-context is non-secret plaintext; ship every time so an
        // empty submit clears it (matches server URL / user address).
        body.bluebubbles_caller_context = bluebubblesCallerContext;
      }
      await updateDeviceConfig(body);
      // Re-baseline the key's presence and owner without a refetch, so the
      // "✓ configured" badge reflects what this save actually left on disk.
      setTtsLoaded({
        apiKey: body.clear_tts_api_key ? false : (ttsLoaded.apiKey || !!ttsApiKey),
        baseUrl: !!ttsBaseUrl,
        choice: ttsChoiceToSave,
      });
      toast.success("Config saved — restart your robot for changes to take effect.");
      // Reset baseline so Save button goes back to disabled until next edit.
      // Non-secret fields adopt their current values as the new baseline.
      setBaseline({
        ssid, deviceId,
        llmUrl, llmModel, llmDisableThinking,
        sttBaseUrl, sttProvider, sttLanguage,
        ttsBaseUrl, ttsProvider, ttsVoice, ttsSpeed,
        wakeWord,
        channel,
        teleUserId, slackUserId,
        discordGuildId, discordUserId,
        bluebubblesServerUrl, bluebubblesUserAddress,
        bluebubblesCallerContext,
        mqttEndpoint, mqttPort, mqttUsername,
        faChannel, fdChannel,
        realtimeEnabled, realtimeProvider, realtimeVoice,
        realtimeReasoning, realtimeBaseUrl, realtimeWebSearch,
      });
      // Clear typed secrets so their non-empty state no longer marks the form
      // dirty. Their persisted values live server-side; has_* flags surface
      // "configured" in the UI.
      setPassword(""); setAdminPassword("");
      setLlmApiKey(""); setTtsApiKey(""); setSttApiKey("");
      setDeepgramApiKey(""); setMqttPassword("");
      setTeleToken(""); setSlackBotToken(""); setSlackAppToken("");
      setDiscordBotToken("");
      setBluebubblesPassword("");
      setRealtimeApiKey("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    }
    setSaving(false);
  }, [
    channel, teleToken, teleUserId, slackBotToken, slackAppToken, slackUserId,
    discordBotToken, discordGuildId, discordUserId,
    bluebubblesServerUrl, bluebubblesPassword, bluebubblesUserAddress,
    bluebubblesCallerContext,
    ssid, password, adminPassword, llmUrl,
    llmApiKey, llmModel, llmDisableThinking, deepgramApiKey, sttApiKey, sttBaseUrl,
    sttProvider, sttLanguage, sttLoaded,
    ttsApiKey, ttsBaseUrl, ttsLoaded, ttsProvider, ttsVoice, ttsSpeed, deviceId,
    mqttEndpoint, mqttUsername, mqttPassword, mqttPort, faChannel, fdChannel,
    realtimeEnabled, wakeWord, realtimeProvider, realtimeVoice, realtimeReasoning, realtimeApiKey, realtimeBaseUrl,
    realtimeWebSearch,
  ]);

  // Save is hidden for sections that aren't part of the form's PUT flow: Face/My
  // Voice enroll via their own buttons, Runtime switches via its own action, and
  // Scheduled is read-only (its only action is per-row "Run now").
  const showSave = activeSection !== "face" && activeSection !== "voice" && activeSection !== "runtime" && activeSection !== "timezone" && activeSection !== "scheduled" && activeSection !== "facebook";

  return (
    <div className="lm-fade-in lm-settings-panel" style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
      <div style={{ maxWidth: 560, margin: "0 auto" }}>

        {/* Header row: active-section label on the left, Save button on the right.
            A hairline divider under the row separates the title from the body. */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          marginBottom: 18, paddingBottom: 14, borderBottom: `1px solid ${C.border}`,
        }}>
          <span className="lm-settings-title" style={{ fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
            {SECTION_LABELS[activeSection]}
          </span>
          {showSave && (
            <button
              form="edit-form"
              type="submit"
              disabled={saving || loadingCfg || !dirty}
              style={{
                padding: "6px 18px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                cursor: saving || loadingCfg || !dirty ? "not-allowed" : "pointer",
                border: "none",
                background: saving || loadingCfg || !dirty ? C.surface : C.amber,
                // Dark ink that reads on the amber fill in both themes; theme-
                // constant on purpose (see --lm-on-amber in index.css).
                color: saving || loadingCfg || !dirty ? C.textMuted : "var(--lm-on-amber)",
                transition: "all 0.15s",
                opacity: saving || loadingCfg || !dirty ? 0.6 : 1,
              }}
            >
              {saving ? "Saving…" : "Save Changes"}
            </button>
          )}
        </div>

        {error && (
          <div style={{
            background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
            borderRadius: 8, padding: "10px 14px", fontSize: 12, color: C.red, marginBottom: 16,
          }}>
            {error}
          </div>
        )}

        {loadingCfg ? <SkeletonBlock /> : (
          <form id="edit-form" onSubmit={handleSubmit}>

            <DeviceSection
              active={activeSection === "device"}
              deviceId={deviceId} setDeviceId={setDeviceId}
              mac={mac}
              rotateAdminPassword={adminPassword}
              setRotateAdminPassword={setAdminPassword}
              wakeWord={wakeWord}
              setWakeWord={setWakeWord}
              agentName={agentName}
              wakePhrases={wakePhrases}
            />

            <WifiSection
              active={activeSection === "wifi"}
              wifiLoaded={wifiLoaded}
              ssid={ssid} setSsid={setSsid}
              password={password} setPassword={setPassword}
            />

            <LLMSection
              active={activeSection === "llm"}
              llmLoaded={llmLoaded}
              llmApiKey={llmApiKey} setLlmApiKey={setMirroredLlmApiKey}
              llmUrl={llmUrl} setLlmUrl={setMirroredLlmUrl}
              llmModel={llmModel} setLlmModel={setLlmModel}
              mode={llmMode}
              onModeChange={(m) => {
                if (m === "custom") { setLlmMode("custom"); return; }
                // Back to Autonomous is a restore, not just an unlock: the
                // stored set has to be written back before the fields can
                // honestly claim to show it. Nothing to restore on a device
                // that was never edited — it is already there.
                if (!hasDefaults) { setLlmMode("autonomous"); return; }
                restoreAutonomousDefaults("llm")
                  .then(() => { toast.success("Back on the Autonomous brain"); window.location.reload(); })
                  .catch((e: Error) => toast.error(e.message || "Could not switch back"));
              }}
            />

            <AgentRuntimeSection active={activeSection === "runtime"} />

            <TimezoneSection active={activeSection === "timezone"} />

            <EditVoiceSection
              active={activeSection === "voice"}
              sttLanguage={sttLanguage}
              faceOwners={faceOwners}
              loadFaceOwners={loadFaceOwners}
            />

            <EditFaceSection
              active={activeSection === "face"}
              faceOwners={faceOwners}
              loadFaceOwners={loadFaceOwners}
            />

            <TTSSection
              active={activeSection === "tts"}
              ttsLoaded={ttsLoaded}
              llmLoaded={llmLoaded}
              ttsApiKey={ttsApiKey} setTtsApiKey={setMirroredTtsApiKey}
              setTtsApiKeyRaw={setTtsApiKey}
              ttsBaseUrl={ttsBaseUrl} setTtsBaseUrl={setMirroredTtsBaseUrl}
              ttsProvider={ttsProvider} setTtsProvider={setTtsProvider}
              ttsProviders={ttsProviders}
              ttsVoice={ttsVoice} setTtsVoice={setTtsVoice}
              ttsSpeed={ttsSpeed} setTtsSpeed={setTtsSpeed}
              ttsVoices={ttsVoices}
              sttLanguage={sttLanguage}
            />
            {activeSection === "tts" && hasDefaults && (
              <RestoreDefaultsButton section="voice" />
            )}

            <RealtimeSection
              active={activeSection === "realtime"}
              realtimeLoaded={realtimeLoaded}
              llmLoaded={llmLoaded}
              enabled={realtimeEnabled} setEnabled={setRealtimeEnabled}
              provider={realtimeProvider} setProvider={setRealtimeProvider}
              voice={realtimeVoice} setVoice={setRealtimeVoice}
              reasoning={realtimeReasoning} setReasoning={setRealtimeReasoning}
              apiKey={realtimeApiKey} setApiKey={setRealtimeApiKey}
              baseUrl={realtimeBaseUrl} setBaseUrl={setRealtimeBaseUrl}
              webSearch={realtimeWebSearch} setWebSearch={setRealtimeWebSearch}
            />
            {activeSection === "realtime" && hasDefaults && (
              <RestoreDefaultsButton section="realtime" />
            )}

            <STTSection
              active={activeSection === "stt"}
              sttLanguage={sttLanguage} setSttLanguage={setSttLanguage}
              sttProvider={sttProvider} setSttProvider={setMirroredSttProvider}
              sttLoaded={sttLoaded}
              llmLoaded={llmLoaded}
              deepgramApiKey={deepgramApiKey} setDeepgramApiKey={setDeepgramApiKey}
              sttApiKey={sttApiKey} setSttApiKey={setMirroredSttApiKey}
              sttBaseUrl={sttBaseUrl} setSttBaseUrl={setMirroredSttBaseUrl}
            />

            <ChannelSection
              active={activeSection === "channel"}
              channel={channel} setChannel={setChannel}
              channelLoaded={channelLoaded}
              teleToken={teleToken} setTeleToken={setTeleToken}
              teleUserId={teleUserId} setTeleUserId={setTeleUserId}
              slackBotToken={slackBotToken} setSlackBotToken={setSlackBotToken}
              slackAppToken={slackAppToken} setSlackAppToken={setSlackAppToken}
              slackUserId={slackUserId} setSlackUserId={setSlackUserId}
              discordBotToken={discordBotToken} setDiscordBotToken={setDiscordBotToken}
              discordGuildId={discordGuildId} setDiscordGuildId={setDiscordGuildId}
              discordUserId={discordUserId} setDiscordUserId={setDiscordUserId}
              bluebubblesServerUrl={bluebubblesServerUrl} setBluebubblesServerUrl={setBluebubblesServerUrl}
              bluebubblesPassword={bluebubblesPassword} setBluebubblesPassword={setBluebubblesPassword}
              bluebubblesUserAddress={bluebubblesUserAddress} setBluebubblesUserAddress={setBluebubblesUserAddress}
              bluebubblesCallerContext={bluebubblesCallerContext} setBluebubblesCallerContext={setBluebubblesCallerContext}
            />

            <FacebookSection active={activeSection === "facebook"} />

            <MCPToolsSection active={activeSection === "mcp"} />
            <PluginsSection active={activeSection === "plugins"} />
            <ScheduledSection active={activeSection === "scheduled"} />

            <MqttSection
              active={activeSection === "mqtt"}
              mqttLoaded={mqttLoaded}
              mqttEndpoint={mqttEndpoint} setMqttEndpoint={setMqttEndpoint}
              mqttPort={mqttPort} setMqttPort={setMqttPort}
              mqttUsername={mqttUsername} setMqttUsername={setMqttUsername}
              mqttPassword={mqttPassword} setMqttPassword={setMqttPassword}
              faChannel={faChannel} setFaChannel={setFaChannel}
              fdChannel={fdChannel} setFdChannel={setFdChannel}
            />

          </form>
        )}
      </div>
    </div>
  );
}
