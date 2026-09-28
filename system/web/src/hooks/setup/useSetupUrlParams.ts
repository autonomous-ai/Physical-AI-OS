import { useMemo } from "react";

export interface SetupUrlParams {
  teleToken: string;
  teleUserId: string;
  slackBotToken: string;
  slackAppToken: string;
  slackUserId: string;
  discordBotToken: string;
  discordGuildId: string;
  discordUserId: string;
  llmApiKey: string;
  llmUrl: string;
  llmModel: string;
  deepgramApiKey: string;
  ttsApiKey: string;
  ttsBaseUrl: string;
  deviceId: string;
  mqttEndpoint: string;
  mqttPort: string;
  mqttUsername: string;
  mqttPassword: string;
  faChannel: string;
  fdChannel: string;
  sttLanguage: string;
  ttsProvider: string;
  ttsVoice: string;
}

// Snapshot the query at module load, before App's useScrubSecrets() strips secrets.
const SESSION_STORE_KEY = "autonomous.setup_url_search.v1";
const INITIAL_SEARCH: string = (() => {
  if (typeof window === "undefined") return "";
  const fromURL = window.location.search;
  if (fromURL) {
    try { sessionStorage.setItem(SESSION_STORE_KEY, fromURL); } catch {
      /* private-mode / disabled storage — fall back to URL-only behavior */
    }
    return fromURL;
  }
  try {
    return sessionStorage.getItem(SESSION_STORE_KEY) ?? "";
  } catch {
    return "";
  }
})();
const INITIAL_PARAMS: URLSearchParams = new URLSearchParams(INITIAL_SEARCH);

// Original (pre-scrub) query string, for cross-origin redirects.
export function getInitialSearch(): string {
  return INITIAL_SEARCH;
}

// Wipes the current Setup attempt and hard-reloads (module-level URL snapshots need a reload).
export function resetSetupSession(): void {
  if (typeof window === "undefined") return;
  clearStoredSetupParams();
  window.location.replace(`${window.location.pathname}`);
}

// Drops the persisted params snapshot WITHOUT reloading.
export function clearStoredSetupParams(): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.removeItem(SESSION_STORE_KEY);
  } catch {
    /* private-mode / disabled storage — nothing was persisted to begin with */
  }
}

// Params come from the module-load snapshot, not the (already scrubbed) router copy.
export function useSetupUrlParams(): SetupUrlParams {
  return useMemo(
    () => ({
      teleToken: INITIAL_PARAMS.get("tele_token") ?? "",
      teleUserId: INITIAL_PARAMS.get("tele_user_id") ?? "",
      slackBotToken: INITIAL_PARAMS.get("slack_bot_token") ?? "",
      slackAppToken: INITIAL_PARAMS.get("slack_app_token") ?? "",
      slackUserId: INITIAL_PARAMS.get("slack_user_id") ?? "",
      discordBotToken: INITIAL_PARAMS.get("discord_bot_token") ?? "",
      discordGuildId: INITIAL_PARAMS.get("discord_guild_id") ?? "",
      discordUserId: INITIAL_PARAMS.get("discord_user_id") ?? "",
      llmApiKey: INITIAL_PARAMS.get("llm_api_key") ?? "",
      llmUrl: INITIAL_PARAMS.get("llm_url") ?? "",
      llmModel: INITIAL_PARAMS.get("llm_model") ?? "",
      deepgramApiKey: INITIAL_PARAMS.get("deepgram_api_key") ?? "",
      ttsApiKey: INITIAL_PARAMS.get("tts_api_key") ?? "",
      ttsBaseUrl: INITIAL_PARAMS.get("tts_base_url") ?? "",
      deviceId: INITIAL_PARAMS.get("device_id") ?? "",
      mqttEndpoint: INITIAL_PARAMS.get("mqtt_endpoint") ?? "",
      mqttPort: INITIAL_PARAMS.get("mqtt_port") ?? "",
      mqttUsername: INITIAL_PARAMS.get("mqtt_username") ?? "",
      mqttPassword: INITIAL_PARAMS.get("mqtt_password") ?? "",
      faChannel: INITIAL_PARAMS.get("fa_channel") ?? "",
      fdChannel: INITIAL_PARAMS.get("fd_channel") ?? "",
      sttLanguage: INITIAL_PARAMS.get("stt_language") ?? "",
      ttsProvider: INITIAL_PARAMS.get("tts_provider") ?? "",
      ttsVoice: INITIAL_PARAMS.get("tts_voice") ?? "",
    }),
    [],
  );
}
