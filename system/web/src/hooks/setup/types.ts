export type SectionId =
  | "wifi" | "device" | "llm" | "language" | "stt" | "deepgram"
  | "tts" | "realtime" | "channel" | "mqtt" | "voice" | "face";

export interface LlmLoadedState {
  apiKey: boolean;
  baseUrl: boolean;
  model: boolean;
}

export interface ChannelLoadedState {
  teleToken: boolean;
  teleUserId: boolean;
  slackBotToken: boolean;
  slackAppToken: boolean;
  slackUserId: boolean;
  discordBotToken: boolean;
  discordGuildId: boolean;
  discordUserId: boolean;
  bluebubblesServerUrl: boolean;
  bluebubblesPassword: boolean;
  bluebubblesUserAddress: boolean;
  bluebubblesCallerContext: boolean;
}
