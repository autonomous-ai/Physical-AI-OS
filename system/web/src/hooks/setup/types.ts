// Shared types between Setup.tsx and its custom hooks.

// `language` is Setup's id for the STT-language section; EditConfig uses `stt`
// for the same thing. Both kept here so SectionCard accepts either page's ids.
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
  // iMessage via BlueBubbles. See ChannelSection.tsx for the operator-facing
  // guide and runtimes/hermes/presync.sh for the env-var mapping.
  bluebubblesServerUrl: boolean;
  bluebubblesPassword: boolean;
  bluebubblesUserAddress: boolean;
  // Optional caller-context prompt (plaintext). Presence flag so the setup
  // page can render the textarea in its "already configured" state, matching
  // the other channel fields.
  bluebubblesCallerContext: boolean;
}
