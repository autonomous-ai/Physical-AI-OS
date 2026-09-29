import { useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getDeviceConfig, getSetupStatus } from "@/lib/api";
import type { SetupChannelType } from "@/types";
import type { SetupUrlParams } from "./useSetupUrlParams";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "./types";

// Hydrates Setup form state from /api/device/config so re-running setup preserves whatever the operator already configured.
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
  setBluebubblesCallerContext: Dispatch<SetStateAction<string>>;
  setHasAdminPassword: Dispatch<SetStateAction<boolean>>;
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
      if (cfg.tts_provider && !urlParams.ttsProvider) setTtsProvider(cfg.tts_provider);
      if (cfg.tts_voice && !urlParams.ttsVoice) setTtsVoice(cfg.tts_voice);
      setSsid((prev) => prev || cfg.network_ssid || "");
      setDeviceId((prev) => prev || cfg.device_id || "");
      setMac(cfg.mac || "");
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
        bluebubblesServerUrl: prev.bluebubblesServerUrl || !!cfg.bluebubbles_server_url,
        bluebubblesPassword: prev.bluebubblesPassword || cfg.has_bluebubbles_password,
        bluebubblesUserAddress: prev.bluebubblesUserAddress || !!cfg.bluebubbles_user_address,
        bluebubblesCallerContext: prev.bluebubblesCallerContext || !!cfg.bluebubbles_caller_context,
      }));
      setBluebubblesCallerContext((prev) => prev || cfg.bluebubbles_caller_context || "");
      setTeleUserId((prev) => prev || cfg.telegram_user_id || "");
      setSlackUserId((prev) => prev || cfg.slack_user_id || "");
      setDiscordGuildId((prev) => prev || cfg.discord_guild_id || "");
      setDiscordUserId((prev) => prev || cfg.discord_user_id || "");
      if (!channelParam && (cfg.channel === "telegram" || cfg.channel === "slack" || cfg.channel === "discord")) {
        setChannel(cfg.channel as SetupChannelType);
      }
      setMqttEndpoint((prev) => prev || cfg.mqtt_endpoint || "");
      setMqttPort((prev) => prev || (cfg.mqtt_port ? String(cfg.mqtt_port) : ""));
      setMqttUsername((prev) => prev || cfg.mqtt_username || "");
      setFaChannel((prev) => prev || cfg.fa_channel || "");
      setFdChannel((prev) => prev || cfg.fd_channel || "");
      if (cfg.stt_language && !urlParams.sttLanguage) setSttLanguage(cfg.stt_language);
      setHasAdminPassword(!!cfg.has_admin_password);
      setHasNetworkPassword(!!cfg.has_network_password);
    }).catch(() => {
      // 401: no session yet; assume no admin password so the field shows.
      setHasAdminPassword(false);
      setHasNetworkPassword(false);
      getSetupStatus().then((s) => {
        if (s.mac) setMac(s.mac);
      }).catch(() => { /* status endpoint unreachable — manual flow still works via fallback link */ });
    });
    // Intentional empty deps — mount-only, like the original effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
