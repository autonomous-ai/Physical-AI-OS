import type { DisplayEvent } from "../types";

export type FlowStage =
  | "mic_input" | "cam_input" | "button_input" | "channel_input" | "webchat_input" | "intent_check" | "local_match"
  | "agent_call" | "agent_thinking" | "tool_exec" | "agent_response" | "tts_speak"
  | "schedule_trigger" | "os_gate" | "hw_camera" | "hw_led" | "hw_servo" | "hw_emotion" | "hw_audio" | "hw_wellbeing" | "hw_mood" | "hw_music_suggestion" | "hw_posture" | "tg_out" | "tg_alert";

/** No pipeline node highlighted — e.g. no matching triggers in recent events */
export type ActiveFlowStage = FlowStage | "idle";

export type NodeShape = "circle" | "hexagon" | "diamond" | "square";

export interface FlowNodeDef {
  id: FlowStage;
  label: string;
  short: string;
  icon: string;
  color: string;
  desc: string;
  triggers: string[];
  path: "main" | "fast" | "agent";
  shape?: NodeShape;
}

export interface Turn {
  id: string;
  runId?: string;
  boundary?: "mic" | "chat";
  boundaryInstanceSeq?: number;
  startTime: string;
  sessionBreak?: boolean;
  endTime?: string;
  type: string;
  voiceTurnType?: "voice" | "voice_command" | "voice_followup";
  path: "local" | "agent" | "harness" | "realtime" | "dropped" | "queued" | "unknown";
  status: "active" | "done" | "error";
  events: DisplayEvent[];
  queuedForMs?: number;
  mergedIntoRunId?: string;
}

export type NodeInfoMap = Record<FlowStage, string[]> & { ambient: string[] };

export const FLOW_NODES: FlowNodeDef[] = [
  { id: "mic_input",
    label: "Mic Input", short: "MIC", icon: "🎤", color: "var(--lm-amber)", path: "main",
    shape: "hexagon",
    desc: "Voice / sound from microphone",
    triggers: [
      "flow_enter:voice_pipeline_start", "flow_event:voice_pipeline_start",
    ] },

  { id: "cam_input",
    label: "Cam Input", short: "CAM", icon: "👁", color: "var(--lm-amber)", path: "main",
    shape: "hexagon",
    desc: "Motion / presence / light from camera",
    triggers: [] },

  { id: "button_input",
    label: "Button / Touch", short: "BTN", icon: "✋", color: "var(--lm-amber)", path: "main",
    shape: "hexagon",
    desc: "Physical input from GPIO button / TTP223 touchpad — head pat, single click, etc.",
    triggers: [] },

  { id: "channel_input",
    label: "Channel In", short: "CH IN", icon: "💬", color: "#229ed9", path: "main",
    shape: "hexagon",
    desc: "Inbound message via messaging channel (Telegram, Discord, Slack, etc.)",
    triggers: [
      "chat_input",
      "flow_event:chat_input",
    ] },

  { id: "webchat_input",
    label: "Web Chat", short: "WEB", icon: "🖥", color: "#7c4dff", path: "main",
    shape: "hexagon",
    desc: "Message sent from web monitor chat UI",
    triggers: [] },

  { id: "intent_check",
    label: "Intent Check", short: "INTENT", icon: "🔀", color: "var(--lm-teal)", path: "main",
    desc: "Route to local match or agent call",
    triggers: [
      "chat_send",
      "flow_event:chat_send", "flow_enter:chat_send", "flow_exit:chat_send",
      "flow_event:agent_call",
      "intent_match", "flow_event:intent_match",
    ] },

  { id: "local_match",
    label: "Local Intent", short: "LOCAL", icon: "⚡", color: "var(--lm-green)", path: "fast",
    desc: "Fast path ~50ms · regex match → instant TTS · bypasses agent",
    triggers: [
      "intent_match",
      "flow_event:intent_match", "flow_enter:intent_match", "flow_exit:intent_match",
    ] },

  { id: "agent_call",
    label: "Agent Call", short: "AGENT", icon: "🤖", color: "var(--lm-blue)", path: "agent",
    desc: "WebSocket chat.send RPC to Agent",
    triggers: [
      "flow_event:agent_call", "flow_enter:agent_call", "flow_exit:agent_call",
      "flow_event:lifecycle_start",
    ] },

  { id: "agent_thinking",
    label: "Thinking", short: "THINK", icon: "🧠", color: "var(--lm-purple)", path: "agent",
    desc: "LLM reasoning · streaming thinking tokens",
    triggers: [
      "thinking",
      "flow_event:agent_thinking",
    ] },

  { id: "tool_exec",
    label: "Tool Exec", short: "TOOL", icon: "🔧", color: "#f59e0b", path: "agent",
    desc: "Agent invoked a tool · function call",
    triggers: [
      "tool_call",
      "flow_event:tool_call", "flow_enter:tool_call", "flow_exit:tool_call",
      "hw_call", "flow_event:hw_call",
    ] },

  { id: "agent_response",
    label: "Response", short: "RESP", icon: "💡", color: "var(--lm-green)", path: "agent",
    desc: "Agent turn ended · may respond or [no reply] (do nothing)",
    triggers: [
      "chat_response",
      "flow_event:lifecycle_end",
      "flow_event:harness_response",
      "flow_event:realtime_response",
      "flow_event:no_reply",
      "hw_only_reply", "flow_event:hw_only_reply",
    ] },

  { id: "tts_speak",
    label: "TTS Speak", short: "TTS", icon: "🔊", color: "var(--lm-purple)", path: "agent",
    shape: "diamond",
    desc: "POST /voice/speak · text-to-speech output",
    triggers: [
      "tts",
      "flow_event:tts_send", "flow_enter:tts_send", "flow_exit:tts_send",
      "flow_event:tts_muted",
      "flow_event:tts_cancelled",
      "intent_match", "flow_event:intent_match",
      "flow_event:voice_pipeline_start",
    ] },

  { id: "schedule_trigger",
    label: "Schedule", short: "CRON", icon: "⏰", color: "#f97316", path: "agent",
    desc: "Cron/timer fired by Agent · agent turn triggered by schedule",
    triggers: [
      "schedule_trigger", "flow_event:schedule_trigger",
      "flow_enter:schedule_trigger", "flow_exit:schedule_trigger",
      "flow_event:cron_fire", "cron_fire",
    ] },

  { id: "os_gate",
    label: "OS Hook", short: "HOOK", icon: "🚦", color: "var(--lm-teal)", path: "agent",
    shape: "square",
    desc: "OS Server middleware · parse [HW:] markers · dispatch HW calls\n→ emotion / LED / servo / audio\n→ TTS (suppress if music)\n→ Telegram broadcast\n→ pause ambient if LED changed",
    triggers: [
      "led_set", "led_off", "ambient_pause", "ambient_resume",
      "flow_event:led_set", "flow_event:led_off",
      "flow_event:tts_suppressed",
      "flow_event:tts_muted",
      "flow_event:tts_cancelled",
      "flow_event:hw_cancelled",
      "flow_event:hw_failed",
      "hw_emotion", "flow_event:hw_emotion",
      "hw_led", "flow_event:hw_led",
      "hw_servo", "flow_event:hw_servo",
      "hw_audio", "flow_event:hw_audio",
      "hw_posture", "flow_event:hw_posture",
      "flow_event:tts_send",
      "flow_event:no_reply",
      "flow_event:hw_only_reply",
      "flow_event:telegram_alert_broadcast",
    ] },

  { id: "tg_out",
    label: "Channel Out", short: "CH OUT", icon: "💬", color: "#229ed9", path: "agent",
    shape: "diamond",
    desc: "Agent delivers response to messaging channel (Telegram, Discord, Slack, etc.)",
    triggers: [
      "flow_event:telegram_alert_broadcast",
    ] },

  { id: "tg_alert",
    label: "Broadcast", short: "BCAST", icon: "📢", color: "#e53935", path: "agent",
    shape: "diamond",
    desc: "Broadcast to all messaging channels (guard alerts, wellbeing reminders, music suggestions)",
    triggers: [
      "flow_event:telegram_alert_broadcast",
    ] },

  { id: "hw_led",
    label: "LED", short: "LED", icon: "🔆", color: "var(--lm-amber)", path: "agent",
    shape: "diamond",
    desc: "LED control · solid color / effect / scene / off",
    triggers: [
      "hw_led", "led_set", "led_off",
      "flow_event:hw_led", "flow_event:led_set", "flow_event:led_off",
    ] },

  { id: "hw_servo",
    label: "Servo Action", short: "SERVO", icon: "🤖", color: "#8b5cf6", path: "agent",
    shape: "diamond",
    desc: "Move / animate servos · /servo/aim · /servo/play · /servo/track",
    triggers: [
      "hw_servo",
      "flow_event:hw_servo",
    ] },

  { id: "hw_camera",
    label: "Camera", short: "CAM", icon: "📷", color: "var(--lm-amber)", path: "agent",
    shape: "diamond",
    desc: "Take a saved frame · GET /camera/snapshot",
    triggers: [] },

  { id: "hw_emotion",
    label: "Emotion", short: "EMO", icon: "😀", color: "#ec4899", path: "agent",
    shape: "diamond",
    desc: "Emotion expression · coordinated LED + servo + display eyes",
    triggers: [
      "hw_emotion", "emotion",
      "flow_event:hw_emotion", "flow_event:emotion",
    ] },

  { id: "hw_audio",
    label: "Audio", short: "AUDIO", icon: "🎵", color: "#a855f7", path: "agent",
    shape: "diamond",
    desc: "Music / audio playback · speaker output",
    triggers: [
      "hw_audio",
      "flow_event:hw_audio",
    ] },

  { id: "hw_wellbeing",
    label: "Wellbeing log", short: "WELL", icon: "💧", color: "#06b6d4", path: "agent",
    shape: "diamond",
    desc: "Wellbeing nudge log · async POST via [HW:/wellbeing/log:{...}]",
    triggers: [
      "hw_wellbeing",
      "flow_event:hw_wellbeing",
    ] },

  { id: "hw_mood",
    label: "Mood log", short: "MOOD", icon: "🧠", color: "#06b6d4", path: "agent",
    shape: "diamond",
    desc: "Mood signal/decision log · async POST via [HW:/mood/log:{...}]",
    triggers: [
      "hw_mood",
      "flow_event:hw_mood",
    ] },

  { id: "hw_music_suggestion",
    label: "Music suggest log", short: "MSUG", icon: "🎼", color: "#06b6d4", path: "agent",
    shape: "diamond",
    desc: "Music suggestion log · async POST via [HW:/music-suggestion/log:{...}]",
    triggers: [
      "hw_music_suggestion",
      "flow_event:hw_music_suggestion",
    ] },

  { id: "hw_posture",
    label: "Posture log", short: "POS", icon: "🪑", color: "#06b6d4", path: "agent",
    shape: "diamond",
    desc: "Posture coach log · async POST via [HW:/posture/log:{...}] — alert / nudge / praise / ritual recap rows",
    triggers: [
      "hw_posture",
      "flow_event:hw_posture",
    ] },
];

export const SOURCE_ICON: Record<string, string> = {
  voice: "🎤", voice_command: "🎙", sound: "🔊",
  motion: "👁", "motion.activity": "🏃", "presence.enter": "🙂", "presence.leave": "👋", "presence.away": "😴", "light.level": "🌡", "emotion.detected": "😊", "speech_emotion": "🗣", "speech_emotion.detected": "🗣", "pose.ergo_risk": "🪑", "touch.head_pat": "✋",
  "wellbeing.music": "🎵",
  environment: "🌡", system: "⚙", unknown: "❓",
  web_chat: "🖥", mqtt_chat: "📱", telegram: "💬", discord: "💬", slack: "💬", wechat: "💬", channel: "💬", chat: "💬", schedule: "⏰",
  emotion: "😊", activity: "🏃", wellbeing: "💧", music: "🎵", sensing: "📡", posture: "🪑",
  cron: "⏰", "cron:music": "🎵",
  "ambient:breathing": "💨", "ambient:movement": "🤖", "ambient:mumble": "💭",
  "ambient:idle": "😴",
  "music.mood": "🎵",
};

import type { LucideIcon } from "lucide-react";
import {
  Mic, Mic2, Volume2, Eye, Activity, Smile, Hand, Armchair, Speech,
  Thermometer, Settings, Music, Monitor, Clock, CircleHelp,
  Wind, Bot, MessagesSquare, Moon, Droplet, Radio, Sun, UserPlus, UserMinus,
  HeartPulse, Smartphone,
} from "lucide-react";

export const TYPE_LUCIDE: Record<string, LucideIcon> = {
  voice: Mic, voice_command: Mic2, voice_followup: Mic, voice_agent_handled: Mic, voice_command_handled: Mic2, voice_followup_handled: Mic, sound: Volume2,
  motion: Eye, "motion.activity": Activity,
  "presence.enter": Smile, "presence.leave": Hand, "presence.away": Moon,
  "light.level": Sun, "emotion.detected": Smile,
  "speech_emotion": Speech, "speech_emotion.detected": Speech,
  "pose.ergo_risk": Armchair, "touch.head_pat": Hand,
  "wellbeing.music": Music,
  environment: Thermometer, system: Settings, unknown: CircleHelp,
  web_chat: Monitor,
  mqtt_chat: Smartphone,
  telegram: MessagesSquare, discord: MessagesSquare, slack: MessagesSquare,
  wechat: MessagesSquare, channel: MessagesSquare, chat: MessagesSquare,
  schedule: Clock,
  emotion: Smile, activity: Activity, wellbeing: Droplet, music: Music,
  sensing: Radio, posture: Armchair,
  cron: Clock, "cron:music": Music,
  heartbeat: HeartPulse,
  "ambient:breathing": Wind, "ambient:movement": Bot, "ambient:mumble": Speech,
  "ambient:idle": Moon,
  "music.mood": Music,
  enter: UserPlus, leave: UserMinus,
};

export const TYPE_LABEL: Record<string, string> = {
  voice: "voice", voice_command: "cmd", voice_followup: "followup", voice_agent_handled: "handled", voice_command_handled: "voice_command_handled", voice_followup_handled: "voice_followup_handled", sound: "sound",
  motion: "motion", "motion.activity": "activity", "emotion.detected": "emotion",
  "speech_emotion": "voice_emo", "speech_emotion.detected": "voice_emo",
  "pose.ergo_risk": "posture", "presence.enter": "enter", "presence.leave": "leave",
  "presence.away": "away", "touch.head_pat": "head pat",
  "light.level": "light", environment: "env", system: "sys",
  "music.mood": "mood", web_chat: "web", mqtt_chat: "mqtt", telegram: "channel", discord: "channel",
  slack: "channel", wechat: "channel", channel: "channel", schedule: "sched",
  cron: "cron", "cron:music": "music", heartbeat: "heartbeat",
};

export const CHAT_TYPES = ["web_chat", "mqtt_chat"] as const;
export function isChatType(type: string): boolean {
  return (CHAT_TYPES as readonly string[]).includes(type);
}

export const CHANNEL_FALLBACK_MESSAGE = "Message from channel";
export const TURN_INPUT_FALLBACK = "Input not captured";
