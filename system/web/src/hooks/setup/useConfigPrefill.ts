import { useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getDeviceConfig, getSetupStatus } from "@/lib/api";
import type { SetupChannelType } from "@/types";
import type { SetupUrlParams } from "./useSetupUrlParams";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "./types";

// Hydrates Setup form state from /api/device/config so re-running setup
// preserves whatever the operator already configured. URL params and any
// user-typed value take precedence — we only fill empty state slots
// (`prev || cfg.x || ""`). Gated against URL for tts/lang fields since
// URL is the authoritative override channel.
// ConfigPublicResponse no longer carries plaintext secrets, so the prefill
// hook only takes setters for the non-secret fields it can still populate.
// Secret setters (setLlmApiKey, setPassword, setMqttPassword, channel
// tokens, etc.) used to mirror saved values into form state — they're now
// dropped from the signature because the source data is gone. The Setup
// page keeps owning that state for user input via SecretUpdateField /
// LockedPasswordField.
export function useConfigPrefill(args: {
  urlParams: SetupUrlParams;
  channelParam: string | null;
  setTtsProvider: Dispatch<SetStateAction<string>>;
  setTtsVoice: Dispatch<SetStateAction<string>>;
  setSsid: Dispatch<SetStateAction<string>>;
  setDeviceId: Dispatch<SetStateAction<string>>;
  setMac: Dispatch<SetStateAction<string>>;
  setActiveSection: Dispatch<SetStateAction<SectionId>>;
  setLlmUrl: Dispatch<SetStateAction<string>>;
  setLlmModel: Dispatch<SetStateAction<string>>;
  setLlmLoaded: Dispatch<SetStateAction<LlmLoadedState>>;
  setLlmDisableThinking: Dispatch<SetStateAction<boolean>>;
  setTtsBaseUrl: Dispatch<SetStateAction<string>>;
  setChannelLoaded: Dispatch<SetStateAction<ChannelLoadedState>>;
  setTeleUserId: Dispatch<SetStateAction<string>>;
  setSlackUserId: Dispatch<SetStateAction<string>>;
  setDiscordGuildId: Dispatch<SetStateAction<string>>;
  setDiscordUserId: Dispatch<SetStateAction<string>>;
  setChannel: Dispatch<SetStateAction<SetupChannelType>>;
  setMqttEndpoint: Dispatch<SetStateAction<string>>;
  setMqttPort: Dispatch<SetStateAction<string>>;
  setMqttUsername: Dispatch<SetStateAction<string>>;
  setFaChannel: Dispatch<SetStateAction<string>>;
  setFdChannel: Dispatch<SetStateAction<string>>;
  setSttLanguage: Dispatch<SetStateAction<string>>;
  // Non-secret iMessage caller-context prompt — prefill the textarea from
  // config on load so returning operators see what they typed last time.
  setBluebubblesCallerContext: Dispatch<SetStateAction<string>>;
  // Surfaces cfg.has_admin_password so the page can show the admin-password
  // fields only when the device hasn't been set up with one (fresh device or
  // existing device migrating from pre-Login-UI builds).
  setHasAdminPassword: Dispatch<SetStateAction<boolean>>;
  // Mirrors cfg.has_network_password so WifiSection can hide the password
  // input when a WiFi password is already on file (re-setup via `#force`).
  setHasNetworkPassword: Dispatch<SetStateAction<boolean>>;
}) {
  const {
    urlParams, channelParam,
    setTtsProvider, setTtsVoice, setSsid, setDeviceId, setMac, setActiveSection,
    setLlmUrl, setLlmModel, setLlmLoaded, setLlmDisableThinking,
    setTtsBaseUrl,
    setChannelLoaded,
    setTeleUserId,
    setSlackUserId,
    setDiscordGuildId, setDiscordUserId,
    setChannel,
    setMqttEndpoint, setMqttPort, setMqttUsername,
    setFaChannel, setFdChannel,
    setSttLanguage,
    setBluebubblesCallerContext,
    setHasAdminPassword,
    setHasNetworkPassword,
  } = args;

  useEffect(() => {
    getDeviceConfig().then((cfg) => {
      // ConfigPublicResponse only surfaces presence booleans for secrets, so
      // we can pre-fill non-secret fields (URLs, IDs, model name, language)
      // and only mark "loaded" for the secret slots — the operator retypes
      // any secret they want to change via the LockedField pencil.
      if (cfg.tts_provider && !urlParams.ttsProvider) setTtsProvider(cfg.tts_provider);
      if (cfg.tts_voice && !urlParams.ttsVoice) setTtsVoice(cfg.tts_voice);
      setSsid((prev) => prev || cfg.network_ssid || "");
      setDeviceId((prev) => prev || cfg.device_id || "");
      // Mac is hardware-derived and read-only; just mirror it from the backend.
      setMac(cfg.mac || "");
      // If Device ID is already provisioned (hardware-derived or saved), the
      // operator has nothing to fill there — jump straight to Wi-Fi. Don't
      // override an explicit user selection in progress.
      if (cfg.device_id) {
        setActiveSection((prev) => (prev === "device" ? "wifi" : prev));
      }
      setLlmUrl((prev) => prev || cfg.llm_base_url || "");
      setLlmModel((prev) => prev || cfg.llm_model || "");
      setLlmLoaded((prev) => ({
        apiKey: prev.apiKey || cfg.has_llm_api_key,
        baseUrl: prev.baseUrl || !!cfg.llm_base_url,
        model: prev.model || !!cfg.llm_model,
      }));
      if (cfg.llm_disable_thinking != null) setLlmDisableThinking((prev) => prev || cfg.llm_disable_thinking);
      setTtsBaseUrl((prev) => prev || cfg.tts_base_url || "");
      setChannelLoaded((prev) => ({
        teleToken: prev.teleToken || cfg.has_telegram_bot_token,
        teleUserId: prev.teleUserId || !!cfg.telegram_user_id,
        slackBotToken: prev.slackBotToken || cfg.has_slack_bot_token,
        slackAppToken: prev.slackAppToken || cfg.has_slack_app_token,
        slackUserId: prev.slackUserId || !!cfg.slack_user_id,
        discordBotToken: prev.discordBotToken || cfg.has_discord_bot_token,
        discordGuildId: prev.discordGuildId || !!cfg.discord_guild_id,
        discordUserId: prev.discordUserId || !!cfg.discord_user_id,
        // iMessage / BlueBubbles — same prev-wins pattern as the other
        // channels so an in-flight edit is not clobbered by a background
        // refetch (see the file-level comment for the merge semantics).
        bluebubblesServerUrl: prev.bluebubblesServerUrl || !!cfg.bluebubbles_server_url,
        bluebubblesPassword: prev.bluebubblesPassword || cfg.has_bluebubbles_password,
        bluebubblesUserAddress: prev.bluebubblesUserAddress || !!cfg.bluebubbles_user_address,
        bluebubblesCallerContext: prev.bluebubblesCallerContext || !!cfg.bluebubbles_caller_context,
      }));
      // Caller-context is non-secret plaintext, so we can hydrate the input
      // string directly (unlike the tokens that only round-trip has_* flags).
      setBluebubblesCallerContext((prev) => prev || cfg.bluebubbles_caller_context || "");
      setTeleUserId((prev) => prev || cfg.telegram_user_id || "");
      setSlackUserId((prev) => prev || cfg.slack_user_id || "");
      setDiscordGuildId((prev) => prev || cfg.discord_guild_id || "");
      setDiscordUserId((prev) => prev || cfg.discord_user_id || "");
      // Adopt saved channel only when the user hasn't already picked one via URL.
      if (!channelParam && (cfg.channel === "telegram" || cfg.channel === "slack" || cfg.channel === "discord")) {
        setChannel(cfg.channel as SetupChannelType);
      }
      setMqttEndpoint((prev) => prev || cfg.mqtt_endpoint || "");
      setMqttPort((prev) => prev || (cfg.mqtt_port ? String(cfg.mqtt_port) : ""));
      setMqttUsername((prev) => prev || cfg.mqtt_username || "");
      setFaChannel((prev) => prev || cfg.fa_channel || "");
      setFdChannel((prev) => prev || cfg.fd_channel || "");
      // Saved language wins over the browser-locale default — the browser
      // guess only matters for a never-configured device. URL param trumps both.
      if (cfg.stt_language && !urlParams.sttLanguage) setSttLanguage(cfg.stt_language);
      setHasAdminPassword(!!cfg.has_admin_password);
      setHasNetworkPassword(!!cfg.has_network_password);
    }).catch(() => {
      // 401 = ConfigPublicResponse gated and we don't have a session yet.
      // Treat that as "device may be missing admin password" so the field
      // shows up — operator can set it and the Setup submit issues the
      // cookie. Safer than hiding the field on a real migration target.
      setHasAdminPassword(false);
      setHasNetworkPassword(false);
      // Pull the hardware-derived MAC from the open setup-status endpoint
      // so the canonical-URL upgrade (192.168.100.1 → lamp-xxxx.local) and
      // the post-setup mDNS auto-redirect still work for fresh devices
      // that can't read /api/device/config without an admin session.
      getSetupStatus().then((s) => {
        if (s.mac) setMac(s.mac);
      }).catch(() => { /* status endpoint unreachable — manual flow still works via fallback link */ });
    });
    // Intentional empty deps — mount-only, like the original effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
