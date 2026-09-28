/** Network item (from GET /api/network) */
export interface NetworkItem {
  bssid: string;
  ssid: string;
  mode: string;
  channel: number;
  rate: string;
  signal: number;
  security: string;
}

export type ChannelType = "telegram" | "slack" | "discord" | "imessage";

/** Initial setup may defer messaging-channel configuration entirely. */
export type SetupChannelType = ChannelType | "none";

/** Request body for POST /api/device/setup */
export interface SetupRequest {
  ssid: string;
  password: string;
  channel?: ChannelType;
  telegram_bot_token?: string;
  telegram_user_id?: string;
  slack_bot_token?: string;
  slack_app_token?: string;
  slack_user_id?: string;
  discord_bot_token?: string;
  discord_user_id?: string;
  llm_base_url: string;
  llm_api_key: string;
  llm_model: string;
  deepgram_api_key?: string;
  stt_api_key?: string;
  stt_language?: string;
  tts_api_key?: string;
  stt_base_url?: string;
  tts_base_url?: string;
  tts_provider?: string;
  tts_voice?: string;
  device_id?: string;
  /** Hardware-derived ID (Lamp-XXXX from Pi serial / eth MAC). Read-only — set by the device at runtime. */
  mac?: string;
  /** MQTT (optional): empty endpoint means MQTT disabled, auto-fetched via ping */
  mqtt_endpoint?: string;
  mqtt_port?: number;
  mqtt_username?: string;
  mqtt_password?: string;
  fa_channel?: string;
  fd_channel?: string;
  /** Disable extended thinking/reasoning for all LLM models (default false). */
  llm_disable_thinking?: boolean;
  /** Admin password the operator picks at setup. Server bcrypts it into
   *  config.admin_password_hash and uses it to gate post-setup browser
   *  access via /api/login. Empty allowed (legacy clients). */
  admin_password?: string;
}
