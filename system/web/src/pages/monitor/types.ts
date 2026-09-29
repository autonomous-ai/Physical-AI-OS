export const API = "/api";
// Go reverse proxy; use hwUrl() for <img>/<a>, which cannot set headers.
export const HW  = "/api/hardware";
export const AGENT_API = `${API}/agent`;
export const HISTORY_LEN = 60;
export const FLOW_EVENTS_MAX = 10000;

export interface SystemInfo {
  cpuLoad: number;
  cpuCount: number;
  cpuPerCore: number[];
  swapTotal: number;
  swapUsed: number;
  swapPercent: number;
  memTotal: number;
  memUsed: number;
  memPercent: number;
  cpuTemp: number;
  uptime: number;
  serviceUptime: number;
  halUptime: number;
  halVersion: string;
  goRoutines: number;
  version: string;
  deviceId: string;
  capabilities?: string[];
  diskTotal: number;
  diskUsed: number;
  diskPercent: number;
}
export interface NetworkInfo {
  ssid: string;
  ip: string;
  publicIp: string;
  tailscaleIp: string;
  signal: number;
  linkRate: number;
  internet: boolean;
  pingMs?: number;
  mac: string;
}
export interface HWHealth {
  status: string;
  servo: boolean;
  led: boolean;
  camera: boolean;
  audio: boolean;
  sensing: boolean;
  voice: boolean;
  tts: boolean;
  music: boolean;
  display: boolean;
}
export interface OCStatus {
  name: string;
  connected: boolean;
  sessionKey: boolean;
  emotion?: string;
  version?: string;
  uptime?: number;
  agentUptime?: number;
}
export interface PresenceInfo {
  state: string;
  enabled: boolean;
  seconds_since_motion: number;
}
export interface VoiceStatus {
  voice_available: boolean;
  voice_listening: boolean;
  tts_available: boolean;
  tts_speaking: boolean;
  mic_muted?: boolean;
  // null on devices without a HW mic switch; when true, /voice/unmute returns 409.
  hw_mic_switch_muted?: boolean | null;
}
export interface ServoState {
  available_recordings: string[];
  current: string | null;
  bus_connected?: boolean;
  robot_connected?: boolean;
}
export interface DisplayState {
  mode: string;
  hardware: boolean;
  available_expressions: string[];
}
export interface AudioVolume {
  control: string;
  volume: number;
  max_volume?: number | null;
}
export interface LEDColor {
  led_count: number;
  on: boolean;
  color: [number, number, number];
  hex: string;
  brightness: number;
  effect: string | null;
  scene: string | null;
}
export interface SceneInfo {
  scenes: string[];
  active?: string;
}
export interface FaceStatus {
  enrolled_count: number;
  enrolled_names: string[];
}
export interface FaceOwnerDetail {
  label: string;
  telegram_username?: string | null;
  telegram_id?: string | null;
  photo_count: number;
  photos: string[];
  mood_days?: string[];
  wellbeing_days?: string[];
  music_suggestion_days?: string[];
  posture_days?: string[];
  audio_history_days?: string[];
  voice_samples?: string[];
  habit_patterns?: boolean;
  files?: string[];
}
export interface FaceOwnersDetail {
  enrolled_count: number;
  persons: FaceOwnerDetail[];
}
export interface MonitorEvent {
  id: string;
  time: string;
  type: string;
  summary: string;
  detail?: Record<string, string> | null;
  runId?: string;
  phase?: string;
  state?: string;
  error?: string;
}
export interface DisplayEvent extends MonitorEvent {
  _seq: number;
}

export type Section = "overview" | "system" | "flow" | "camera" | "servo" | "face-owners" | "analytics" | "logs" | "chat" | "pairing" | "cli" | "sensing" | "bluetooth" | "api-docs" | "agent-config" | "settings:device" | "settings:wifi" | "settings:llm" | "settings:runtime" | "settings:voice" | "settings:face" | "settings:tts" | "settings:realtime" | "settings:stt" | "settings:channel" | "settings:mqtt" | "settings:mcp" | "settings:plugins" | "settings:timezone" | "settings:scheduled" | "settings:facebook";

export type Area = "monitor" | "setting";

// Maps the URL path for an area.
export function areaPath(area: Area): string {
  return area === "setting" ? "/setting" : "/monitor";
}

// settings:* -> "setting", everything else -> "monitor".
export function sectionArea(section: Section): Area {
  return section.startsWith("settings:") ? "setting" : "monitor";
}

// General <-> settings:device is the lone asymmetry.
const SHORT_TO_SETTING: Record<string, Section> = {
  general: "settings:device",
  wifi: "settings:wifi",
  voice: "settings:voice",
  face: "settings:face",
  llm: "settings:llm",
  runtime: "settings:runtime",
  stt: "settings:stt",
  tts: "settings:tts",
  realtime: "settings:realtime",
  channel: "settings:channel",
  mqtt: "settings:mqtt",
  mcp: "settings:mcp",
  plugins: "settings:plugins",
  timezone: "settings:timezone",
  scheduled: "settings:scheduled",
  facebook: "settings:facebook",
};
const SETTING_TO_SHORT: Record<string, string> = Object.fromEntries(
  Object.entries(SHORT_TO_SETTING).map(([short, id]) => [id, short]),
);

// Serialize a section to the URL hash (no leading "#") for the given area.
export function sectionToHash(section: Section, area: Area): string {
  if (area === "setting") return SETTING_TO_SHORT[section] ?? "general";
  return section;
}

// Parse a URL hash (no leading "#") into a Section for the given area.
export function hashToSection(hash: string, area: Area): Section | null {
  const h = hash.replace(/^#/, "");
  if (area === "setting") return SHORT_TO_SETTING[h] ?? null;
  if (!h) return null;
  return h as Section;
}

// Web mirror of Go's device.Cap* (capabilities.v1).
export const Cap = {
  Audio: "audio",
  Vision: "vision",
  Motion: "motion",
  Sensing: "sensing",
  Environment: "environment",
  Connectivity: "connectivity",
  Expression: "expression",
} as const;

export type NavLeaf = { id: Section; label: string; icon: string; cap?: string | readonly string[] };
export type NavLink = { href: string; label: string; icon: string; external?: boolean };
// A subgroup nests one level of leaves inside a top-level group — used to gather
// integration-style items (e.g. Connectors: Facebook, Discord, Slack) under a
// single collapsible header inside the parent group. Subgroups do not nest
// further; the sidebar renderer walks children one level deep only.
export type NavSubgroup = { subgroup: string; label: string; icon: string; children: NavLeaf[] };
export type NavChild = NavLeaf | NavLink | NavSubgroup;
export type NavGroup = { group: string; label: string; icon: string; children: NavChild[] };
export type NavEntry = NavLeaf | NavGroup;

export function isNavGroup(e: NavEntry): e is NavGroup {
  return "group" in e;
}
export function isNavLink(c: NavChild): c is NavLink {
  return "href" in c;
}
export function isNavSubgroup(c: NavChild): c is NavSubgroup {
  return "subgroup" in c;
}

export const NAV: NavEntry[] = [
  { id: "chat",     label: "Chat",     icon: "▤" },
  {
    group: "settings",
    label: "Settings",
    icon: "⚙",
    children: [
      { id: "settings:device",   label: "General",   icon: "⚙" },
      { id: "settings:wifi",     label: "Wi-Fi",     icon: "⌁" },
      { id: "settings:llm",      label: "AI Brain",  icon: "✦" },
      { id: "settings:runtime",  label: "Runtime",   icon: "▦" },
      { id: "settings:stt",      label: "Language",  icon: "⌘" },
      { id: "settings:tts",      label: "Voice",     icon: "♫" },
      { id: "settings:realtime", label: "Realtime",  icon: "⚡" },
      { id: "settings:voice",    label: "My Voice",  icon: "◉" },
      { id: "settings:face",     label: "Face",      icon: "☺", cap: Cap.Vision },
      { id: "settings:channel",  label: "Channels",  icon: "✉" },
      { id: "settings:mqtt",     label: "MQTT",      icon: "⇄" },
      { id: "settings:mcp",      label: "MCP Tools", icon: "⬡" },
      { id: "settings:plugins",  label: "Plugins",   icon: "⧉" },
      { id: "settings:timezone", label: "Timezone",  icon: "◷" },
      { id: "settings:scheduled", label: "Scheduled", icon: "⏰" },
      // Third-party integrations grouped under a collapsible header inside
      // Settings, mirroring the Connectors menu on autonomous.ai admin. Leaf
      // ids keep the `settings:` prefix so existing hash routes still resolve.
      {
        subgroup: "connector",
        label: "Connectors",
        icon: "⚯",
        children: [
          { id: "settings:facebook", label: "Facebook", icon: "❦" },
        ],
      },
    ],
  },
  {
    group: "device",
    label: "Robot",
    icon: "⎚",
    children: [
      { id: "overview",    label: "Overview",  icon: "⊞" },
      { id: "system",      label: "System",    icon: "⚙" },
      { id: "flow",        label: "Flow",      icon: "⇄" },
      { id: "face-owners", label: "Users",     icon: "☺", cap: Cap.Vision },
      { id: "camera",      label: "Camera",    icon: "◎", cap: Cap.Vision },
      { id: "sensing", label: "Sensing", icon: "◉" },
      { id: "servo",       label: "Servo",     icon: "⎈", cap: Cap.Motion },
      { id: "pairing",     label: "Pairing",   icon: "⌘" },
      { id: "logs",        label: "Logs",      icon: "☰" },
      { id: "cli",         label: "CLI",       icon: "▸" },
      { id: "api-docs",    label: "API Docs",  icon: "⎗" },
    ],
  },
];
