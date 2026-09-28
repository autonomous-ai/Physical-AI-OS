declare const __WEB_VERSION__: string;
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { logout } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { usePolling } from "../../hooks/usePolling";
import { useEventSource } from "../../hooks/useEventSource";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";

import {
  MessageCircle, Settings, Cpu, Wifi, Brain, Globe, Volume2, MicVocal,
  UserCircle, MessageSquare, Link as LinkIcon, MonitorSmartphone, LayoutGrid,
  Workflow, Users, Camera, Radar, ChartColumn, Move3d, Bluetooth, ScrollText,
  Terminal, FileCode, Hexagon, ExternalLink, SlidersHorizontal, ChevronRight,
  Server, Zap, LogOut, Clock, Search, X, CornerDownLeft, Plug, Blocks,
  CalendarClock, Handshake, Facebook,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { S } from "./styles";
import { API, HW, HISTORY_LEN, FLOW_EVENTS_MAX, NAV, isNavGroup, isNavLink, Cap, areaPath, sectionArea, sectionToHash, hashToSection } from "./types";
import type { Section, Area, SystemInfo, NetworkInfo, HWHealth, OCStatus, PresenceInfo, VoiceStatus, ServoState, DisplayState, AudioVolume, LEDColor, SceneInfo, MonitorEvent, DisplayEvent, NavEntry } from "./types";
import { OverviewSection, type OverviewCache } from "./OverviewSection";
import { PairingSection } from "./PairingSection";
import { SystemSection } from "./SystemSection";
import { FlowSection } from "./FlowSection";
import { SensingSection } from "./SensingSection";
import { CameraSection } from "./CameraSection";
import { ServoSection } from "./ServoSection";
import { AnalyticsSection } from "./AnalyticsSection";
import { LogsSection } from "./LogsSection";
import { ChatSection } from "./ChatSection";
import { FaceOwnersSection } from "./FaceOwnersSection";
import { BluetoothSection } from "./BluetoothSection";
import { CliSection } from "./CliSection";
import { ConfirmDialog } from "./components";
import { SettingsPanel } from "@/pages/settings/SettingsPanel";
import type { SettingsSectionId } from "@/pages/settings/SettingsPanel";

ChartJS.register(CategoryScale, LinearScale, BarElement, PointElement, LineElement, Title, Tooltip, Legend, Filler);

// Sections rendered as full-bleed iframes — they need their own padding/overflow override.
const EMBED_SECTIONS = new Set<Section>(["api-docs", "agent-config"]);

// Sections shown to non-debug users. Append `?debug=true` to the URL to reveal
// the rest of the menu (Analytics, Servo, API Docs, Agent gateway).
const PUBLIC_SECTIONS = new Set<Section>(["sensing", "chat", "pairing", "overview", "system", "flow", "camera", "face-owners", "bluetooth", "logs", "cli", "settings:device", "settings:wifi", "settings:voice", "settings:face", "settings:mcp", "settings:plugins", "settings:timezone", "settings:scheduled", "settings:facebook"]);

// The capability a section requires, read from its NAV leaf (single source: the
// nav definition itself declares `cap`). undefined → no hardware dependency, the
// section is always shown.
function sectionCap(id: Section): string | readonly string[] | undefined {
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      const child = entry.children.find((c) => !isNavLink(c) && c.id === id);
      if (child && !isNavLink(child)) return child.cap;
    } else if (entry.id === id) return entry.cap;
  }
  return undefined;
}

const iframeStyle: React.CSSProperties = {
  width: "100%",
  height: "100%",
  border: "none",
  display: "block",
  background: "var(--lm-card)",
};

// Lucide icon map for the sidebar, keyed by leaf Section id, by group name, and
// by the Agent-menu pseudo ids. NAV still carries the legacy unicode `icon`
// string for backwards-compat; the sidebar/topbar render these lucide icons
// instead, falling back to nothing when a key is missing.
const NAV_ICONS: Record<string, LucideIcon> = {
  // top-level leaf
  chat: MessageCircle,
  pairing: Handshake,
  // group headers
  settings: Settings,
  device: MonitorSmartphone,
  // settings children
  "settings:device": Cpu,
  "settings:wifi": Wifi,
  "settings:llm": Brain,
  "settings:runtime": Server,
  "settings:stt": Globe,
  "settings:tts": Volume2,
  "settings:realtime": Zap,
  "settings:voice": MicVocal,
  "settings:face": UserCircle,
  "settings:channel": MessageSquare,
  "settings:facebook": Facebook,
  "settings:mqtt": LinkIcon,
  "settings:mcp": Plug,
  "settings:plugins": Blocks,
  "settings:timezone": Clock,
  "settings:scheduled": CalendarClock,
  // device children
  overview: LayoutGrid,
  system: Cpu,
  flow: Workflow,
  "face-owners": Users,
  camera: Camera,
  sensing: Radar,
  analytics: ChartColumn,
  servo: Move3d,
  bluetooth: Bluetooth,
  logs: ScrollText,
  cli: Terminal,
  "api-docs": FileCode,
  // Agent gateway menu
  agent: Hexagon,
  "agent-gateway": ExternalLink,
  "agent-config": SlidersHorizontal,
};

// Renders the lucide icon for a given nav id (leaf Section, group name, or Agent
// pseudo id). Returns null when no icon is mapped.
const NavIcon = ({ id, size = 16 }: { id: string; size?: number }) => {
  const I = NAV_ICONS[id];
  return I ? <I size={size} strokeWidth={1.9} /> : null;
};

function allNavLeaves(): { id: Section; label: string; icon: string }[] {
  const leaves: { id: Section; label: string; icon: string }[] = [];
  for (const entry of NAV) {
    if (isNavGroup(entry)) entry.children.forEach((c) => { if (!isNavLink(c)) leaves.push(c); });
    else leaves.push(entry);
  }
  // Agent config isn't in NAV (rendered by AgentGWMenu) — register it here
  // so hash routing + topbar title work for the embedded view.
  leaves.push({ id: "agent-config", label: "Agent Config", icon: "◈" });
  return leaves;
}

// Flat, searchable list of nav leaves carrying their parent-group label (so a
// result can show "Voice · Settings" for context). Top-level leaves (e.g. Chat)
// carry no group. Order follows NAV; the sidebar search filters this list.
type SearchLeaf = { id: Section; label: string; group: string | null };
function searchableLeaves(): SearchLeaf[] {
  const out: SearchLeaf[] = [];
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      entry.children.forEach((c) => { if (!isNavLink(c)) out.push({ id: c.id, label: c.label, group: entry.label }); });
    } else {
      out.push({ id: entry.id, label: entry.label, group: null });
    }
  }
  return out;
}

// Sidebar search box — filters nav by label/group, with a clear (×) button that
// appears once there's a query. Renders a flat result list under .lm-root so the
// theme + amber treatment match the rest of the rail.
function SidebarSearch({ query, setQuery, results, section, setSection, closeSidebar, leafHref, onEnter }: {
  query: string;
  setQuery: (q: string) => void;
  results: SearchLeaf[];
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
  leafHref: (id: Section) => string;
  onEnter: () => void;
}) {
  const go = (id: Section) => { setSection(id); setQuery(""); closeSidebar(); };
  return (
    <div className={"lm-snav-search" + (query ? " lm-snav-search--active" : "")}>
      <div className="lm-snav-search-box">
        <Search size={15} strokeWidth={1.9} className="lm-snav-search-icon" />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); onEnter(); }
            else if (e.key === "Escape") { e.preventDefault(); setQuery(""); }
          }}
          placeholder="Search features…"
          className="lm-snav-search-input"
          aria-label="Search features"
          autoComplete="off"
          spellCheck={false}
        />
        {query && (
          <button
            type="button"
            className="lm-snav-search-clear"
            onClick={() => setQuery("")}
            aria-label="Clear search"
            title="Clear"
          >
            <X size={14} strokeWidth={2.2} />
          </button>
        )}
      </div>
      {query && (
        <div className="lm-snav-search-results">
          {results.length === 0 ? (
            <div className="lm-snav-search-empty">No matches for “{query}”</div>
          ) : (
            results.map((r, i) => (
              <a
                key={r.id}
                href={leafHref(r.id)}
                className={"lm-snav-item lm-snav-result" + (section === r.id ? " lm-snav-item--active" : "")}
                onClick={(e) => { e.preventDefault(); go(r.id); }}
              >
                <NavIcon id={r.id} size={16} />
                <span className="lm-snav-result-label">{r.label}</span>
                {r.group && <span className="lm-snav-result-group">{r.group}</span>}
                {i === 0 && <CornerDownLeft size={13} strokeWidth={2} className="lm-snav-result-enter" />}
              </a>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function NavGroupItem({ entry, section, setSection, closeSidebar, leafHref }: {
  entry: Extract<NavEntry, { group: string }>;
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
  leafHref: (id: Section) => string;
}) {
  const hasActiveChild = entry.children.some((c) => !isNavLink(c) && c.id === section);
  const [open, setOpen] = useState(hasActiveChild);
  // Sync expand state to the active section whenever it changes: a group
  // auto-opens when navigation lands on one of its children and auto-collapses
  // when it lands elsewhere (e.g. picking "My Voice" from search closes a Device
  // group that was left open). Keyed on `section` only, so manual header toggles
  // — which don't change the section — are preserved.
  // set-state-in-effect is disabled here for the same reason the dep list is
  // pinned to `section`: `open` must stay MANUALLY toggleable between
  // navigations, so it cannot be derived from `hasActiveChild` during render.
  useEffect(() => { setOpen(hasActiveChild); }, [section]); // eslint-disable-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className={"lm-snav-group" + (hasActiveChild ? " lm-snav-group--active" : "")}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <NavIcon id={entry.group} size={16} />
          {entry.label}
        </span>
        <ChevronRight
          size={14}
          strokeWidth={2}
          style={{ color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}
        />
      </button>
      {open && (
        <div className="lm-snav-children">
          {entry.children.map((child) =>
            isNavLink(child) ? (
              <a
                key={child.href}
                href={child.href}
                className="lm-snav-sub"
                target={child.external ? "_blank" : undefined}
                rel={child.external ? "noreferrer" : undefined}
                onClick={closeSidebar}
              >
                <NavIcon id={child.label} size={15} />
                {child.label}
              </a>
            ) : (
              <a
                key={child.id}
                href={leafHref(child.id)}
                className={"lm-snav-sub" + (section === child.id ? " lm-snav-sub--active" : "")}
                onClick={(e) => { e.preventDefault(); setSection(child.id); closeSidebar(); }}
              >
                <NavIcon id={child.id} size={15} />
                {child.label}
              </a>
            )
          )}
        </div>
      )}
    </div>
  );
}

function AgentGWMenu({ section, setSection, closeSidebar }: {
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
}) {
  const hasActive = section === "agent-config";
  const [open, setOpen] = useState(hasActive);
  // OpenClaw Control UI 5.2 sets X-Frame-Options: DENY so we open in a new
  // tab. The gateway auth token used to ride along as a `#token=…` fragment
  // fetched from /api/agent/config-json — that endpoint is now
  // loopback-only (audit local F5c), so we drop the fragment entirely and
  // let the on-device OpenClaw control UI handle its own auth. Off-device
  // browsers reaching the link will be blocked by nginx /gw/ deny-LAN
  // anyway (audit local F6).
  return (
    // ponytail: parked behind CSS, not deleted — see .lm-nav-agent in index.css.
    <div className="lm-nav-agent">
      <button
        onClick={() => setOpen((v) => !v)}
        className={"lm-snav-group" + (hasActive ? " lm-snav-group--active" : "")}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <NavIcon id="agent" size={16} />
          Agent
        </span>
        <ChevronRight
          size={14}
          strokeWidth={2}
          style={{ color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}
        />
      </button>
      {open && (
        <div className="lm-snav-children">
          <a
            href="/gw/chat?session=agent:main:main"
            target="_blank"
            rel="noopener noreferrer"
            className="lm-snav-sub"
            onClick={closeSidebar}
            title="Opens in a new tab — Agent blocks iframe embedding"
          >
            <NavIcon id="agent-gateway" size={15} />
            Gateway
          </a>
          <a
            href="#agent-config"
            className={"lm-snav-sub" + (section === "agent-config" ? " lm-snav-sub--active" : "")}
            onClick={(e) => { e.preventDefault(); setSection("agent-config"); closeSidebar(); }}
          >
            <NavIcon id="agent-config" size={15} />
            Config
          </a>
        </div>
      )}
    </div>
  );
}

// Resolve the initial / location-derived section for an area. Falls back to the
// area default ("overview" for monitor, "settings:device" for setting) when the
// hash is empty or unknown, and to "overview" when a non-debug user deep-links a
// private section.
function resolveSection(area: Area, hash: string, isDebug: boolean): Section {
  const parsed = hashToSection(hash, area);
  if (parsed === null) return area === "setting" ? "settings:device" : "overview";
  const known = allNavLeaves().some((n) => n.id === parsed);
  if (!known) return area === "setting" ? "settings:device" : "overview";
  if (!isDebug && !PUBLIC_SECTIONS.has(parsed)) return area === "setting" ? "settings:device" : "overview";
  return parsed;
}

export default function Monitor() {
  const [theme, toggleTheme, themeClass] = useTheme();
  const location = useLocation();
  const navigate = useNavigate();
  const isDebug = new URLSearchParams(location.search).get("debug") === "true";

  // Keep every existing query parameter and the current section hash while
  // enabling or disabling the debug-only Monitor and Settings sections.
  const toggleDebug = useCallback(() => {
    const params = new URLSearchParams(location.search);
    if (isDebug) {
      params.delete("debug");
    } else {
      params.set("debug", "true");
    }
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ""}${location.hash}`);
  }, [isDebug, location.hash, location.pathname, location.search, navigate]);

  // Area is derived from the route path: /setting → "setting", else "monitor".
  const area: Area = location.pathname.startsWith("/setting") ? "setting" : "monitor";

  const [section, setSectionRaw] = useState<Section>(() =>
    resolveSection(area, window.location.hash, isDebug),
  );

  // setSection switches BOTH the in-memory section and the URL. When the target
  // section's area differs from the current path, navigate to the other route
  // (no remount — see App.tsx layout route); the hash is always the area's
  // serialized form (short label in the setting area, e.g. /setting#general).
  const setSection = useCallback((s: Section) => {
    const targetArea = sectionArea(s);
    const hash = sectionToHash(s, targetArea);
    const path = areaPath(targetArea);
    if (targetArea !== area) {
      // Preserve the query string (?debug=true, etc.) across the area switch —
      // dropping it would, e.g., hide every debug-only Settings leaf the moment
      // you cross from /monitor into /setting.
      navigate(`${path}${location.search}#${hash}`);
    } else {
      window.location.hash = hash;
    }
    setSectionRaw(s);
  }, [area, navigate, location.search]);

  // React to location changes (back/forward, deep-links, path switches): keep
  // the in-memory section in sync with pathname + hash. Also normalize an empty
  // setting hash to /setting#general so the URL is always explicit.
  const search = location.search;
  useEffect(() => {
    if (area === "setting" && !location.hash) {
      navigate("/setting#general" + search, { replace: true });
      setSectionRaw("settings:device");
      return;
    }
    setSectionRaw(resolveSection(area, location.hash, isDebug));
  }, [location.pathname, location.hash, search, area, isDebug, navigate]);

  const sectionLeaf = allNavLeaves().find((n) => n.id === section);
  const sectionLabel = sectionLeaf?.label ?? "Monitor";
  useDocumentTitle(area === "setting" ? ["Settings", sectionLabel] : sectionLabel);

  // Clear the session (token + os_session cookie via POST /api/logout), then
  // send the user to /login. We navigate even if the network call fails — the
  // local token is already cleared, so the session is effectively gone client-side.
  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } finally {
      navigate("/login" + location.search);
    }
  }, [navigate, location.search]);

  // Build the real href for a nav leaf (path + serialized hash) so middle-click
  // / open-in-new-tab land on the correct URL. Carry the current query string so
  // ?debug=true (and friends) survive an open-in-new-tab across areas.
  const leafHref = (id: Section): string => {
    const a = sectionArea(id);
    return `${areaPath(a)}${location.search}#${sectionToHash(id, a)}`;
  };

  const [overviewCache] = useState<OverviewCache>(() => ({}));
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [net, setNet] = useState<NetworkInfo | null>(null);
  const [hw, setHw] = useState<HWHealth | null>(null);
  const [oc, setOc] = useState<OCStatus | null>(null);
  const [presence, setPresence] = useState<PresenceInfo | null>(null);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [servo, setServo] = useState<ServoState | null>(null);
  const [displayState, setDisplayState] = useState<DisplayState | null>(null);
  const [audio, setAudio] = useState<AudioVolume | null>(null);
  const [musicPlaying, setMusicPlaying] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [ledColor, setLedColor] = useState<LEDColor | null>(null);
  const [sceneInfo, setSceneInfo] = useState<SceneInfo | null>(null);
  const [events, setEvents] = useState<DisplayEvent[]>([]);
  const [displayTs, setDisplayTs] = useState(0);

  const [cpuHistory, setCpuHistory] = useState<number[]>([]);
  const [ramHistory, setRamHistory] = useState<number[]>([]);
  const [lastUpdate, setLastUpdate] = useState<string>("");


  const evtIdRef = useRef(0);
  const clearFlowEvents = useCallback(() => {
    setEvents([]);
  }, []);

  // HAL version comes from /api/system/info (sys.halVersion), populated
  // by the OS server via a cached loopback call to :5001/version. Avoids a direct
  // browser fetch to /hw/version which nginx gates to loopback only.

  // One-shot fetch for system info on mount — populates sidebar version /
  // uptime labels without needing a recurring poll on every section.
  useEffect(() => {
    fetch(`${API}/system/info`).then((r) => r.json()).then((r) => {
      if (r.status === 1) setSys(r.data);
    }).catch(() => {});
  }, []);

  // Device's DECLARED capabilities, served by os-server on /api/system/info
  // (sys.capabilities) — Go owns the contract and parses ROBOT.md, so the web
  // asks the OS rather than reaching through to the HAL runtime. Used to gate
  // tabs + controls. null (not yet loaded) → show everything (fail-open).
  const caps = sys?.capabilities ? new Set(sys.capabilities) : null;
  const hasCap = (c: string): boolean => !caps || caps.has(c);
  const sectionVisible = (id: Section): boolean => {
    const cap = sectionCap(id);
    return !cap || (typeof cap === "string" ? hasCap(cap) : cap.some(hasCap));
  };

  // If the active section is for hardware this device lacks, fall back to overview.
  useEffect(() => {
    if (caps && !sectionVisible(section)) setSection("overview");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sys?.capabilities, section]);

  // Section ref so polling callback always sees current section without re-mounting
  const sectionRef = useRef(section);
  useEffect(() => { sectionRef.current = section; }, [section]);

  // Sidebar polling: openclaw status only (needed for all tabs).
  // Runs at 10s via the shared usePolling hook, which adds a 4s hard
  // timeout, skips ticks that overlap a previous in-flight call, and
  // pauses entirely while the tab is hidden — that combination is what
  // keeps the monitor page from saturating Chrome's 6-per-origin HTTP/1.1
  // connection pool and freezing.
  usePolling(async (signal) => {
    const ocR = await fetch(`${API}/agent/status`, { signal }).then((r) => r.json());
    if (ocR.status === 1) setOc(ocR.data);
    setLastUpdate(new Date().toLocaleTimeString());
  }, 10_000);

  // Section-specific polling at 5s. The fetcher branches on the active
  // section so hidden sections don't pull data they won't show.
  //
  // Every card's fetch is fired CONCURRENTLY and commits its own state the
  // moment it resolves — no card waits on a slower sibling. Previously this was
  // three sequential `await Promise.all` waves (system → health → peripherals),
  // so the Audio panel (last wave) only appeared after system/info + network +
  // health had all returned, stacking the latency of the `/api/hardware/*`
  // proxy hops to HAL. The only real dependency is the peripheral panels on
  // `/health` (it reports which capability routes are mounted), so those alone
  // chain off it; system/info, network, presence and scene run in parallel.
  usePolling(async (signal) => {
    const s = sectionRef.current;
    const json = async (r: Response) => {
      const data = await r.json();
      signal.throwIfAborted();
      return data;
    };
    const tasks: Promise<unknown>[] = [];

    if (s === "overview" || s === "system") {
      tasks.push(
        fetch(`${API}/system/info`, { signal }).then(json).then((sysR) => {
          if (sysR.status === 1) {
            const d = sysR.data;
            setSys(d);
            setCpuHistory((h) => [...h.slice(-(HISTORY_LEN - 1)), d.cpuLoad]);
            setRamHistory((h) => [...h.slice(-(HISTORY_LEN - 1)), d.memPercent]);
          }
        }).catch(() => {}),
        fetch(`${API}/system/network`, { signal }).then(json).then((netR) => {
          if (netR.status === 1) setNet(netR.data);
        }).catch(() => {}),
      );
    }

    if (s === "overview") {
      tasks.push(
        fetch(`${HW}/presence`, { signal }).then(json).then(setPresence).catch(() => {}),
        fetch(`${HW}/scene`, { signal }).then(json).then((sceneR) => {
          if (sceneR.scenes) setSceneInfo(sceneR);
        }).catch(() => {}),
        // Each peripheral panel is fetched ONLY when health reports that hardware
        // present — its HAL route is mounted by the device's declared capability,
        // so an absent peripheral means the route 404s. Gating here keeps a device
        // that lacks a peripheral (e.g. intern-v2 has no servo/display, and no
        // `media` → no music; a device with no speaker has audio:false) from
        // hitting 404 endpoints every 5s poll. Panels render null-safe when their
        // state stays at the initial value.
        fetch(`${HW}/health`, { signal }).then(json).then((hwR) => {
          setHw(hwR);
          const peripherals: Promise<unknown>[] = [];
          if (hwR.voice) peripherals.push(fetch(`${HW}/voice/status`, { signal }).then(json).then((r) => { if (r) setVoice(r); }).catch(() => {}));
          if (hwR.audio) peripherals.push(fetch(`${HW}/audio/volume`, { signal }).then(json).then((r) => { if (r) setAudio(r); }).catch(() => {}));
          if (hwR.music) peripherals.push(fetch(`${HW}/audio/status`, { signal }).then(json).then((r) => {
            if (r?.playing !== undefined) setMusicPlaying(r.playing);
            if (r?.speaker_muted !== undefined) setSpeakerMuted(r.speaker_muted);
          }).catch(() => {}));
          if (hwR.led) peripherals.push(fetch(`${HW}/led/color`, { signal }).then(json).then((r) => { if (r?.hex) setLedColor(r); }).catch(() => {}));
          if (hwR.servo) peripherals.push(fetch(`${HW}/servo`, { signal }).then(json).then((r) => { if (r) setServo(r); }).catch(() => {}));
          if (hwR.display) peripherals.push(fetch(`${HW}/display`, { signal }).then(json).then((r) => { if (r) setDisplayState(r); }).catch(() => {}));
          return Promise.all(peripherals).then(() => setDisplayTs(Date.now()));
        }).catch(() => {}),
      );
    }

    await Promise.all(tasks);
  }, 5_000, { timeoutMs: 8000, refreshKey: section });

  // Flow SSE: only open when flow or chat section is active. useEventSource
  // auto-closes the stream on tab-hidden / unmount, freeing its connection
  // slot (one per stream against Chrome's 6-per-origin cap).
  const needsFlow = section === "flow" || section === "chat";
  useEventSource(
    needsFlow ? `${API}/agent/flow-stream` : null,
    {
      onMessage: (msg) => {
        try {
          const payload = JSON.parse(msg.data) as { events?: MonitorEvent[] };
          if (!Array.isArray(payload.events)) return;
          const next = payload.events
            .slice(-FLOW_EVENTS_MAX)
            .map((ev, i) => ({ ...ev, _seq: i }));
          setEvents(next);
          evtIdRef.current = next.length;
        } catch {
          // A malformed SSE frame drops that frame only: the stream stays open
          // and the next full snapshot replaces the event list anyway.
        }
      },
    },
  );

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const closeSidebar = () => setSidebarOpen(false);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);

  // Sidebar feature search. Filters nav leaves by label/group (case-insensitive,
  // substring), honouring the same debug + hardware visibility gates as the
  // rendered nav so search never surfaces a tab the user can't open. Enter jumps
  // to the first result.
  const [navQuery, setNavQuery] = useState("");
  const q = navQuery.trim().toLowerCase();
  const searchResults = q
    ? searchableLeaves().filter((leaf) => {
        if (!isDebug && !PUBLIC_SECTIONS.has(leaf.id)) return false;
        if (!sectionVisible(leaf.id)) return false;
        return leaf.label.toLowerCase().includes(q) || (leaf.group?.toLowerCase().includes(q) ?? false);
      })
    : [];
  const gotoFirstResult = () => { if (searchResults.length > 0) { setSection(searchResults[0].id); setNavQuery(""); closeSidebar(); } };

  return (
    <div className={`lm-root ${themeClass}`} style={S.root}>
      {/* Mobile overlay */}
      <div
        className={`lm-sidebar-overlay${sidebarOpen ? " lm-sidebar-overlay--open" : ""}`}
        onClick={closeSidebar}
      />

      {/* Sidebar */}
      <aside style={S.sidebar} className={`lm-sidebar${sidebarOpen ? " lm-sidebar--open" : ""}`}>
        <SidebarSearch
          query={navQuery}
          setQuery={setNavQuery}
          results={searchResults}
          section={section}
          setSection={setSection}
          closeSidebar={closeSidebar}
          leafHref={leafHref}
          onEnter={gotoFirstResult}
        />
        {/* When a search query is active the grouped nav is replaced by the flat
            result list rendered inside SidebarSearch, so skip the normal tree. */}
        <nav style={{ padding: "10px 0", flex: 1, display: navQuery.trim() ? "none" : undefined }}>
          {/* Chat is the only top-level leaf; Pairing is grouped under Device. */}
          {NAV.filter((e) => !isNavGroup(e) && e.id === "chat").map((entry) => {
            const leaf = entry as Extract<NavEntry, { id: Section }>;
            return (
              <a
                key={leaf.id}
                href={leafHref(leaf.id)}
                className={"lm-snav-item" + (section === leaf.id ? " lm-snav-item--active" : "")}
                onClick={(e) => { e.preventDefault(); setSection(leaf.id); closeSidebar(); }}
              >
                <NavIcon id={leaf.id} size={16} />
                {leaf.label}
              </a>
            );
          })}
          {/* Device and Settings rendered explicitly here (Device above
              Settings, both before Agent) so the visible order stays
              Chat → Device → Settings → Agent → (other groups). Their children
              come from NAV; the generic groups loop below excludes both to
              avoid a duplicate render. */}
          {NAV
            .filter((e) => isNavGroup(e) && (e.group === "device" || e.group === "settings"))
            // Force Device before Settings regardless of NAV declaration order.
            .sort((a, b) => {
              const rank = (e: NavEntry) => ((e as Extract<NavEntry, { group: string }>).group === "device" ? 0 : 1);
              return rank(a) - rank(b);
            })
            .map((entry) => {
              const group = entry as Extract<NavEntry, { group: string }>;
              const filtered = {
                ...group,
                children: group.children.filter((c) => {
                  if (isNavLink(c)) return isDebug; // external links: debug only
                  if (!isDebug && !PUBLIC_SECTIONS.has(c.id)) return false;
                  return sectionVisible(c.id); // hide tabs for absent hardware; settings leaves have no cap
                }),
              };
              if (filtered.children.length === 0) return null;
              return <NavGroupItem key={group.group} entry={filtered} section={section} setSection={setSection} closeSidebar={closeSidebar} leafHref={leafHref} />;
            })}
          {isDebug && <AgentGWMenu section={section} setSection={setSection} closeSidebar={closeSidebar} />}
          {NAV
            .filter((e) => (isNavGroup(e) ? (e.group !== "settings" && e.group !== "device") : e.id !== "chat"))
            .map((entry) => {
              if (isNavGroup(entry)) {
                const filtered = {
                  ...entry,
                  children: entry.children.filter((c) => {
                    if (isNavLink(c)) return isDebug; // external links: debug only
                    if (!isDebug && !PUBLIC_SECTIONS.has(c.id)) return false;
                    return sectionVisible(c.id); // hide tabs for absent hardware
                  }),
                };
                if (filtered.children.length === 0) return null;
                return <NavGroupItem key={entry.group} entry={filtered} section={section} setSection={setSection} closeSidebar={closeSidebar} leafHref={leafHref} />;
              }
              if (!isDebug && !PUBLIC_SECTIONS.has(entry.id)) return null;
              return (
                <a
                  key={entry.id}
                  href={leafHref(entry.id)}
                  className={"lm-snav-item" + (section === entry.id ? " lm-snav-item--active" : "")}
                  onClick={(e) => { e.preventDefault(); setSection(entry.id); closeSidebar(); }}
                >
                  <NavIcon id={entry.id} size={16} />
                  {entry.label}
                </a>
              );
            })}
        </nav>
        <div style={{
          padding: "12px 16px",
          borderTop: "1px solid var(--lm-border)",
          fontSize: 10,
          color: "var(--lm-text-muted)",
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}>
          <button
            onClick={() => setShowLogoutConfirm(true)}
            className="lm-logout-btn"
            title="Log out of this robot"
          >
            <LogOut size={15} strokeWidth={1.9} />
            Logout
          </button>
          {lastUpdate && <div>Updated {lastUpdate}</div>}
        </div>
      </aside>

      {/* Main */}
      <main style={S.main}>
        {/* Topbar: hamburger (mobile-only, left) + display/debug controls (right). */}
        <div style={S.topbar}>
          <button
            className="lm-hamburger"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Menu"
          >☰</button>
          {/* Current section label — gives the user a visual anchor for where they are. */}
          <span style={{
            display: "flex", alignItems: "center", gap: 8,
            fontSize: 13, fontWeight: 600, color: "var(--lm-text)",
          }}>
            <span style={{ display: "flex", color: "var(--lm-amber)" }}><NavIcon id={section} size={16} /></span>
            <span>{sectionLabel}</span>
          </span>
          <span style={{ flex: 1 }} />
          <button onClick={toggleDebug} style={{
            display: "flex", alignItems: "center", gap: 6, background: "none",
            border: "1px solid var(--lm-border)", borderRadius: 6, cursor: "pointer", fontSize: 12,
            color: isDebug ? "var(--lm-amber)" : "var(--lm-text-muted)", padding: "4px 10px", marginRight: 8,
          }} title={isDebug ? "Disable debug mode" : "Enable debug mode"} role="switch" aria-checked={isDebug}>
            <span>Debug</span>
            <span style={{
              width: 30, height: 16, padding: 2, borderRadius: 999,
              display: "flex", alignItems: "center",
              justifyContent: isDebug ? "flex-end" : "flex-start",
              background: isDebug ? "var(--lm-amber)" : "var(--lm-border)",
              transition: "background 150ms ease, justify-content 150ms ease",
            }}>
              <span style={{
                width: 12, height: 12, borderRadius: "50%", background: "var(--lm-card)",
                boxShadow: "0 1px 2px rgba(0,0,0,0.25)", transition: "transform 150ms ease",
              }} />
            </span>
          </button>
          <button onClick={toggleTheme} style={{
            background: "none", border: "1px solid var(--lm-border)", cursor: "pointer",
            fontSize: 12, color: "var(--lm-text-muted)", padding: "4px 10px",
            borderRadius: 6,
          }} title={`Theme: ${theme}`}>
            {theme === "dark" ? "◑ Dark" : "◐ Light"}
          </button>
        </div>

        {/* Content */}
        <div style={{
          ...S.content,
          ...(section === "chat" ? { padding: 0, overflow: "hidden" } : {}),
          ...(EMBED_SECTIONS.has(section) ? { padding: 0, overflow: "hidden" } : {}),
          // display:flex + column is load-bearing, not cosmetic: SettingsPanel
          // scrolls itself via flex:1/minHeight:0/overflowY:auto, and those do
          // nothing unless this wrapper is a flex container. Without it the
          // panel's height stays `auto`, grows past this overflow:hidden box,
          // and a long list (many schedules) is simply clipped with no
          // scrollbar anywhere to reach it.
          ...(section.startsWith("settings:")
            ? { padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" as const }
            : {}),
        }} className="lm-content">
          {/* Non-chat sections share a keyed wrapper so switching between them
              re-triggers the fade-in. Chat stays OUTSIDE this wrapper (always
              mounted) so its history survives tab switches — keying it would
              remount and wipe it. */}
          <div key={section === "chat" ? "_keep" : section} className={section === "chat" ? undefined : "lm-fade-in"} style={{ display: "contents" }}>
          {section === "overview" && (
            <OverviewSection
              cache={overviewCache}
              sys={sys}
              net={net}
              hw={hw}
              oc={oc}
              presence={presence}
              voice={voice}
              servo={servo}
              displayState={displayState}
              audio={audio}
              musicPlaying={musicPlaying}
              speakerMuted={speakerMuted}
              ledColor={ledColor}
              sceneInfo={sceneInfo}
              hasEmotion={hasCap(Cap.Expression)}
              hasMotion={hasCap(Cap.Motion)}
              webVersion={__WEB_VERSION__}
              halVersion={sys?.halVersion ?? null}
              onSceneActivate={(scene) => {
                const url = scene === "off" ? `${HW}/scene/off` : `${HW}/scene`;
                const opts: RequestInit = { method: "POST", headers: { "Content-Type": "application/json" } };
                if (scene !== "off") opts.body = JSON.stringify({ scene });
                fetch(url, opts).then((r) => r.json()).then((res) => {
                  if (res.status === "ok") setSceneInfo((prev) => prev ? { ...prev, active: scene === "off" ? undefined : scene } : prev);
                }).catch(() => {});
              }}
              onMicMutedChange={(muted) => {
                // Commit on HAL's ack (ms) so the toggle flips immediately instead
                // of waiting out the 5s poll. A 409 (hardware mic switch off) is
                // !r.ok → state untouched; the poll stays the reconciler of truth.
                fetch(`${HW}/voice/${muted ? "mute" : "unmute"}`, { method: "POST" }).then((r) => {
                  if (r.ok) setVoice((prev) => (prev ? { ...prev, mic_muted: muted } : prev));
                }).catch(() => {});
              }}
              onSpeakerMutedChange={(muted) => {
                fetch(`${HW}/speaker/${muted ? "mute" : "unmute"}`, { method: "POST" }).then((r) => {
                  if (r.ok) setSpeakerMuted(muted);
                }).catch(() => {});
              }}
              onTTSStop={() => {
                fetch(`${API}/agent/tts/stop`, { method: "POST" }).then((r) => {
                  if (r.ok) {
                    setVoice((prev) => (prev ? { ...prev, tts_speaking: false } : prev));
                    setMusicPlaying(false);
                  }
                }).catch(() => {});
              }}
              onPlaybackLive={(tts, music) => {
                // Fired by the mic-level SSE stream only on CHANGE — commits
                // playback state the moment HAL flips it, so "Speaking…" clears
                // in ~100ms instead of waiting out the 5s poll (which stays on
                // as the reconciler when the stream is closed/hidden).
                setVoice((prev) => (prev && prev.tts_speaking !== tts ? { ...prev, tts_speaking: tts } : prev));
                setMusicPlaying((prev) => (prev === music ? prev : music));
              }}
              onEmotionPick={(e) => {
                // Optimistic pill highlight: oc.emotion refreshes on the 10s
                // sidebar poll, which reconciles if the agent moves on.
                fetch(`${HW}/emotion`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ emotion: e, intensity: 1.0 }),
                }).then((r) => {
                  if (r.ok) setOc((prev) => (prev ? { ...prev, emotion: e } : prev));
                }).catch(() => {});
              }}
              onServoPlay={(p) => {
                fetch(`${HW}/servo/play`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ recording: p }),
                }).then((r) => {
                  if (r.ok) setServo((prev) => (prev ? { ...prev, current: p } : prev));
                }).catch(() => {});
              }}
              onServoRelease={() => {
                fetch(`${HW}/servo/release`, { method: "POST", headers: { accept: "application/json" } }).then((r) => {
                  if (r.ok) setServo((prev) => (prev ? { ...prev, current: null } : prev));
                }).catch(() => {});
              }}
            />
          )}
          {section === "pairing" && <PairingSection />}
          {section === "system" && (
            <SystemSection
              sys={sys}
              net={net}
              cpuHistory={cpuHistory}
              ramHistory={ramHistory}
            />
          )}
          {section === "flow"      && <FlowSection events={events} onClearEvents={clearFlowEvents} isDebug={isDebug} />}
          {section === "camera"    && <CameraSection displayTs={displayTs} />}
          {section === "sensing"   && <SensingSection hasVision={caps?.has(Cap.Vision) ?? false} hasEnvironment={caps?.has(Cap.Environment) ?? false} />}
          {section === "servo"     && <ServoSection />}
          {section === "bluetooth" && <BluetoothSection />}
          {section === "face-owners" && <FaceOwnersSection />}
          {section === "analytics" && <AnalyticsSection />}
          {section === "logs"      && <LogsSection />}
          {section === "cli" && <CliSection />}
          {section === "api-docs" && (
            <iframe
              title="API Docs"
              // Routed through `/api/hardware/*` (admin-auth gated reverse
              // proxy to the device) instead of `/hw/docs` directly: nginx /hw/
              // is `allow 127.0.0.1; deny all;` per audit local F2, so the
              // direct path is broken from any remote browser. The proxy
              // accepts the session cookie via fetch credentials.
              src="/api/hardware/docs"
              style={iframeStyle}
            />
          )}
          {section === "agent-config" && (
            <iframe
              title="Agent Config"
              src="/gw-config"
              style={iframeStyle}
            />
          )}
          {/* Settings leaves render the shared SettingsPanel, which owns its
              own scroll container + padding (content wrapper is padding:0 above
              for these sections). The "settings:" prefix is stripped to the
              SettingsSectionId the panel expects. */}
          {section.startsWith("settings:") && (
            <SettingsPanel activeSection={section.slice("settings:".length) as SettingsSectionId} />
          )}
          </div>
          {/* Chat lives OUTSIDE the keyed fade wrapper so it stays mounted and
              keeps its history across tab switches (keying it would remount). */}
          <div style={{ display: section === "chat" ? "contents" : "none" }}>
            <ChatSection events={events} isActive={section === "chat"} />
          </div>
        </div>
      </main>

      {showLogoutConfirm && (
        <ConfirmDialog
          title="Log out?"
          message="You'll need to sign in again with the admin password to access this robot."
          confirmLabel="Logout"
          destructive
          onConfirm={() => { setShowLogoutConfirm(false); handleLogout(); }}
          onCancel={() => setShowLogoutConfirm(false)}
        />
      )}
    </div>
  );
}
