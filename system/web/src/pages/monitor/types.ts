export const API = "/api";
// HW points at the Go reverse proxy (/api/hardware/*) instead of nginx /hw/*.
// Web never touches /hw/* directly anymore — adminAuthMiddleware on the
// proxy gates the bearer, and Go calls the device on loopback. Bearer is
// attached automatically by the fetch interceptor in lib/api.ts (search for
// `__osFetchPatched`). For <img src> / <a href> / window.open use the
// `hwUrl()` helper which appends ?token= since those can't set headers.
export const HW  = "/api/hardware";
// Agent gateway base path. Runtime-agnostic: `/api/agent/*` proxies to the
// configured agent runtime (OpenClaw default; picoclaw / claudecode also
// supported via `config.AgentRuntime`). All callers must go through this
// constant so swapping providers stays a one-line change here.
export const AGENT_API = `${API}/agent`;
export const HISTORY_LEN = 60;
export const FLOW_EVENTS_MAX = 10000;

// ─── Types ──────────────────────────────────────────────────────────────────

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
  // Device's DECLARED capabilities (see Cap), from os-server's parse of
  // ROBOT.md. The web gates tabs/controls on these. Absent until /system/info
  // loads → treat as "all present" (fail-open).
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
  signal: number;      // dBm; 0 = unknown
  linkRate: number;    // current PHY link rate in Mbps; 0 = unknown
  internet: boolean;
  pingMs?: number;     // internet probe round-trip in ms; 0/absent = unmeasured
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
  uptime?: number; // seconds since OS server WS became ready; 0 when disconnected (debug only)
  agentUptime?: number; // OpenClaw gateway process uptime in seconds; survives OS server restarts
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
  // Hardware mic-mute slide switch (Intern v2 Pro PD1). null on devices
  // without the switch (Lamp) → UI hides the "HW-locked" hint. When true,
  // /voice/unmute returns 409 so the UI must disable the Unmute button.
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
  // SAFETY.md `audio.max_volume` ceiling (%), null/absent when the device
  // declares none. The slider bounds itself to this rather than letting the
  // operator drag past a value HAL would clamp back down.
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
// UI-augmented version with local seq id
export interface DisplayEvent extends MonitorEvent {
  _seq: number;
}

export type Section = "overview" | "system" | "flow" | "camera" | "servo" | "face-owners" | "analytics" | "logs" | "chat" | "pairing" | "cli" | "sensing" | "bluetooth" | "api-docs" | "agent-config" | "settings:device" | "settings:wifi" | "settings:llm" | "settings:runtime" | "settings:voice" | "settings:face" | "settings:tts" | "settings:realtime" | "settings:stt" | "settings:channel" | "settings:mqtt" | "settings:mcp" | "settings:plugins" | "settings:timezone" | "settings:scheduled" | "settings:facebook";

// ─── Area + URL serialization ────────────────────────────────────────────────
//
// The shell is mounted on two routes: /monitor and /setting. `Area` is derived
// from the current pathname. The in-memory section model keeps the internal
// `settings:*` ids untouched (so all rendering/cap/polling logic is unchanged),
// but the URL hash uses SHORT labels in the setting area. The only asymmetry —
// "General" (internal `settings:device`) serializes to the short hash `general`
// — lives ONLY in the two helpers below.

export type Area = "monitor" | "setting";

// Maps the URL path for an area. Used to build hrefs + navigate targets.
export function areaPath(area: Area): string {
  return area === "setting" ? "/setting" : "/monitor";
}

// The area a section belongs to: settings:* → "setting", everything else →
// "monitor".
export function sectionArea(section: Section): Area {
  return section.startsWith("settings:") ? "setting" : "monitor";
}

// short hash ↔ internal settings id. General↔device is the lone asymmetry.
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
// In the setting area, settings:* ids become their short label; monitor
// sections stay as their plain id.
export function sectionToHash(section: Section, area: Area): string {
  if (area === "setting") return SETTING_TO_SHORT[section] ?? "general";
  return section;
}

// Parse a URL hash (no leading "#") into a Section for the given area.
// Returns null when the hash is empty/unknown for that area, so callers can
// apply the area's default.
export function hashToSection(hash: string, area: Area): Section | null {
  const h = hash.replace(/^#/, "");
  if (area === "setting") return SHORT_TO_SETTING[h] ?? null;
  if (!h) return null;
  return h as Section;
}

// Capability names — the web mirror of Go's device.Cap* / robots/contract/
// capabilities.md. Single source for the capability strings the web references,
// so a tab's hardware requirement is a named constant, not a scattered literal.
// Keep in sync with the capability vocabulary (capabilities.v1).
export const Cap = {
  Audio: "audio",
  Vision: "vision",
  Motion: "motion",
  Sensing: "sensing",
  Environment: "environment",
  Connectivity: "connectivity",
  Expression: "expression",
} as const;

// A nav leaf may require one capability or any capability in an array; the nav hides it and the
// router redirects away when the device lacks that capability. Omit `cap` for
// sections with no hardware dependency (always shown).
export type NavLeaf = { id: Section; label: string; icon: string; cap?: string | readonly string[] };
export type NavLink = { href: string; label: string; icon: string; external?: boolean };
export type NavChild = NavLeaf | NavLink;
export type NavGroup = { group: string; label: string; icon: string; children: NavChild[] };
export type NavEntry = NavLeaf | NavGroup;

export function isNavGroup(e: NavEntry): e is NavGroup {
  return "group" in e;
}
export function isNavLink(c: NavChild): c is NavLink {
  return "href" in c;
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
      // Enrolling a face needs a camera to recognise it with. Without this the
      // leaf showed on every device, offering an upload that could never be
      // matched against anything.
      { id: "settings:face",     label: "Face",      icon: "☺", cap: Cap.Vision },
      { id: "settings:channel",  label: "Channels",  icon: "✉" },
      { id: "settings:facebook", label: "Facebook",  icon: "❦" },
      { id: "settings:mqtt",     label: "MQTT",      icon: "⇄" },
      { id: "settings:mcp",      label: "MCP Tools", icon: "⬡" },
      { id: "settings:plugins",  label: "Plugins",   icon: "⧉" },
      { id: "settings:timezone", label: "Timezone",  icon: "◷" },
      { id: "settings:scheduled", label: "Scheduled", icon: "⏰" },
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
      { id: "face-owners", label: "Users",     icon: "☺", cap: Cap.Vision }, // user roster needs the camera
      { id: "camera",      label: "Camera",    icon: "◎", cap: Cap.Vision },
      // Either input can populate Sensing; each card gates its own polling.
      { id: "sensing", label: "Sensing", icon: "◉" },
      // Analytics hidden from the menu for now (section code kept; re-enable
      // by uncommenting).
      // { id: "analytics",   label: "Analytics", icon: "⊟" },
      { id: "servo",       label: "Servo",     icon: "⎈", cap: Cap.Motion },
      { id: "pairing",     label: "Pairing",   icon: "⌘" },
      { id: "logs",        label: "Logs",      icon: "☰" },
      { id: "cli",         label: "CLI",       icon: "▸" },
      { id: "api-docs",    label: "API Docs",  icon: "⎗" },
    ],
  },
];
