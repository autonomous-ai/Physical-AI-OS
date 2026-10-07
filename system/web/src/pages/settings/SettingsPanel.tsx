import "./settings-polish.css";
import { useEffect, useRef, useState, useCallback } from "react";
import { toast } from "sonner";
import { getDeviceConfig, getCurrentNetwork, updateDeviceConfig, getTTSVoices, getTTSProviders, hwUrl, restoreAutonomousDefaults } from "@/lib/api";
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
import { LedSection } from "@/pages/settings/LedSection";
import { STTSection, type SttProvider } from "@/pages/settings/STTSection";
import { ChannelSection } from "@/pages/settings/ChannelSection";
import { MqttSection } from "@/pages/settings/MqttSection";
import { MCPToolsSection } from "@/pages/settings/MCPToolsSection";
import { PluginsSection } from "@/pages/settings/PluginsSection";
import { ScheduledSection } from "@/pages/settings/ScheduledSection";
import { confirmWifiConnection, isWifiHandoffError } from "@/pages/settings/wifiReconnect";
import { FacebookSection } from "@/pages/settings/FacebookSection";

export type SettingsSectionId = "device" | "wifi" | "llm" | "runtime" | "voice" | "face" | "tts" | "realtime" | "stt" | "channel" | "mqtt" | "mcp" | "plugins" | "timezone" | "led" | "scheduled" | "facebook";

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
  led: "Resting light",
  scheduled: "Scheduled",
  facebook: "Facebook",
};

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

// Self-contained settings form body (state, load/save, sections); `activeSection` is controlled by the parent.
export function SettingsPanel({ activeSection }: { activeSection: SettingsSectionId }): React.JSX.Element {
  const [loadingCfg, setLoadingCfg] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [wifiNotice, setWifiNotice] = useState<string | null>(null);
  const wifiCheck = useRef<AbortController | null>(null);
  useEffect(() => () => wifiCheck.current?.abort(), []);

  const checkWifi = useCallback(async (target: string, acknowledged: boolean, signal: AbortSignal) => {
    const saveStatus = acknowledged ? "Config saved." : "Connection interrupted; save result is unknown.";
    const reconnect = `Connect this phone or computer to “${target}” and reopen this device's address if needed.`;
    setWifiNotice(`${saveStatus} Waiting to confirm Wi-Fi. ${reconnect}`);
    const result = await confirmWifiConnection(target, getCurrentNetwork, signal);
    if (result === "cancelled") return;
    if (result === "connected") {
      setWifiNotice(acknowledged
        ? `Config saved. Device is connected to “${target}”.`
        : `Device is connected to “${target}”. The save response was lost; reload to confirm your settings before saving again.`);
    } else {
      setWifiNotice(`${saveStatus} Could not confirm the Wi-Fi connection. ${reconnect} Reload to check your settings before trying again.`);
    }
  }, []);

  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  // Empty = keep the current password.
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
  const [sttProvider, setSttProvider] = useState<SttProvider>("autonomous");
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
  const [bluebubblesServerUrl, setBluebubblesServerUrl] = useState("");
  const [bluebubblesPassword, setBluebubblesPassword] = useState("");
  const [bluebubblesUserAddress, setBluebubblesUserAddress] = useState("");
  const [bluebubblesCallerContext, setBluebubblesCallerContext] = useState("");
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");
  const [mqttLoaded, setMqttLoaded] = useState({
    endpoint: false, port: false, username: false,
    password: false, faChannel: false, fdChannel: false,
  });
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
  const [hasDefaults, setHasDefaults] = useState(false);
  const [llmMode, setLlmMode] = useState<LlmMode>("autonomous");
  const [realtimeLoaded, setRealtimeLoaded] = useState({ apiKey: false });
  const [sttLoaded, setSttLoaded] = useState({ deepgram: false, apiKey: false, baseUrl: false });

  // Baseline of non-secret fields, used to enable Save only when dirty.
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
    realtimeEnabled: boolean;
    realtimeProvider: string;
    realtimeVoice: string;
    realtimeReasoning: string;
    realtimeBaseUrl: string;
    realtimeWebSearch: boolean;
  };
  const [baseline, setBaseline] = useState<InitialSnapshot | null>(null);

  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch(hwUrl("/face/owners")).then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch {
      // Face owners are optional; a device without face capability shows an empty list.
    }
  }, []);

  // Only sets state after `await fetch`, i.e. subscribing to an external system.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { loadFaceOwners(); }, [loadFaceOwners]);

  useEffect(() => {
    getDeviceConfig()
      .then((cfg: DeviceConfig) => {
        // Secrets come back as has_* flags; their inputs stay empty until typed.
        setSsid(cfg.network_ssid ?? "");
        setDeviceId(cfg.device_id ?? "");
        setMac(cfg.mac ?? "");
        setLlmUrl(cfg.llm_base_url ?? "");
        setLlmModel(cfg.llm_model ?? "");
        setLlmDisableThinking(cfg.llm_disable_thinking ?? false);
        const llmUrlInit = cfg.llm_base_url ?? "";
        const sttProviderInit: SttProvider = cfg.has_deepgram_api_key ? "deepgram" : "autonomous";
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
        // Mirror post-load defaults so the form is not dirty on load.
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

  // TTS may inherit the AI Brain key only for inheriting choices; direct vendors would 401 into silence (#309).
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
    bluebubblesServerUrl !== baseline.bluebubblesServerUrl ||
    bluebubblesUserAddress !== baseline.bluebubblesUserAddress ||
    bluebubblesCallerContext !== baseline.bluebubblesCallerContext ||
    !!bluebubblesPassword
  );

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setWifiNotice(null);
    // Backend has no min length, so enforce ADMIN_PASSWORD_MIN when rotating.
    if (adminPassword && adminPassword.length < ADMIN_PASSWORD_MIN) {
      setError(`New admin password must be at least ${ADMIN_PASSWORD_MIN} characters.`);
      return;
    }
    // Direct vendors need their own key; a blank key falls back to the AI-brain JWT and 401s into silence (#309).
    const ttsChoiceToSave = detectChoice(ttsBaseUrl, ttsProvider);
    const ttsNeedsOwnKey = ttsChoiceToSave === "openai" || ttsChoiceToSave === "elevenlabs";
    if (ttsNeedsOwnKey && !ttsApiKey && !(ttsLoaded.apiKey && ttsLoaded.choice === ttsChoiceToSave)) {
      setError(`${ttsChoiceToSave === "openai" ? "OpenAI" : "ElevenLabs"} (direct) needs its own API key — the AI brain key will not work with it.`);
      return;
    }
    wifiCheck.current?.abort();
    const controller = new AbortController();
    wifiCheck.current = controller;
    const wifiMayReconnect = !!ssid.trim() && (activeSection === "wifi" || ssid !== baseline?.ssid || !!password);
    setSaving(true);
    try {
      // Secrets ship only when typed; blanks would clear the saved value.
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
      const realtime: Record<string, unknown> = { enabled: realtimeEnabled, provider: realtimeProvider };
      if (realtimeProvider !== "none") { realtime.voice = realtimeVoice; realtime.reasoning = realtimeReasoning; }
      // The server rejects web_search for other providers.
      if (realtimeProvider === "pipecat_v1") realtime.web_search = realtimeWebSearch;
      if (realtimeBaseUrl) realtime.base_url = realtimeBaseUrl;
      if (realtimeApiKey) realtime.api_key = realtimeApiKey;
      body.realtime = realtime;
      body.wakeword = wakeWord;
      if (llmApiKey) body.llm_api_key = llmApiKey;
      // Switching TTS provider invalidates the stored key, so delete it explicitly (#309).
      if (ttsApiKey) {
        body.tts_api_key = ttsApiKey;
      } else if (ttsLoaded.apiKey && ttsLoaded.choice !== ttsChoiceToSave) {
        body.clear_tts_api_key = true;
      }
      if (mqttPassword) body.mqtt_password = mqttPassword;
      if (sttProvider === "deepgram") {
        if (deepgramApiKey) body.deepgram_api_key = deepgramApiKey;
        if (sttLoaded.apiKey || sttApiKey) body.stt_api_key = "";
      } else {
        if (sttApiKey) body.stt_api_key = sttApiKey;
        if (sttLoaded.deepgram || deepgramApiKey) body.deepgram_api_key = "";
      }
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
        body.bluebubbles_server_url = bluebubblesServerUrl;
        body.bluebubbles_user_address = bluebubblesUserAddress;
        if (bluebubblesPassword) body.bluebubbles_password = bluebubblesPassword;
        body.bluebubbles_caller_context = bluebubblesCallerContext;
      }
      await updateDeviceConfig(body);
      setTtsLoaded({
        apiKey: body.clear_tts_api_key ? false : (ttsLoaded.apiKey || !!ttsApiKey),
        baseUrl: !!ttsBaseUrl,
        choice: ttsChoiceToSave,
      });
      if (!wifiMayReconnect) toast.success("Config saved — restart your robot for changes to take effect.");
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
      setPassword(""); setAdminPassword("");
      setLlmApiKey(""); setTtsApiKey(""); setSttApiKey("");
      setDeepgramApiKey(""); setMqttPassword("");
      setTeleToken(""); setSlackBotToken(""); setSlackAppToken("");
      setDiscordBotToken("");
      setBluebubblesPassword("");
      setRealtimeApiKey("");
      if (wifiMayReconnect) await checkWifi(ssid.trim(), true, controller.signal);
    } catch (err) {
      if (isWifiHandoffError(err, wifiMayReconnect)) {
        // No acknowledgement: retain every dirty field, including other settings.
        await checkWifi(ssid.trim(), false, controller.signal);
      } else {
        setError(err instanceof Error ? err.message : "Save failed.");
      }
    }
    setSaving(false);
  }, [
    activeSection, baseline, checkWifi,
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

  const showSave = activeSection !== "mcp" && activeSection !== "plugins" && activeSection !== "face" && activeSection !== "voice" && activeSection !== "runtime" && activeSection !== "timezone" && activeSection !== "led" && activeSection !== "scheduled" && activeSection !== "facebook";

  return (
    <div className={`lm-fade-in lm-settings-panel${activeSection !== "device" ? " lm-settings-polished" : ""}`} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
      <div style={{ maxWidth: 560, margin: "0 auto" }}>

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
                color: saving || loadingCfg || !dirty ? C.textMuted : "var(--lm-on-amber)",
                transition: "all 0.15s",
                opacity: saving || loadingCfg || !dirty ? 0.6 : 1,
              }}
            >
              {saving ? (wifiNotice ? "Checking Wi-Fi…" : "Saving…") : "Save Changes"}
            </button>
          )}
        </div>

        {wifiNotice && (
          <div role="status" style={{ border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 14px", fontSize: 12, marginBottom: 16 }}>
            {wifiNotice}
          </div>
        )}

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
                if (!hasDefaults) { setLlmMode("autonomous"); return; }
                restoreAutonomousDefaults("llm")
                  .then(() => { toast.success("Back on the Autonomous brain"); window.location.reload(); })
                  .catch((e: Error) => toast.error(e.message || "Could not switch back"));
              }}
            />

            <AgentRuntimeSection active={activeSection === "runtime"} />

            <TimezoneSection active={activeSection === "timezone"} />

            <LedSection active={activeSection === "led"} />

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
