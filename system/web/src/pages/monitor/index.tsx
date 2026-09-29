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
  CalendarClock, Handshake, Facebook, Cable,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { S } from "./styles";
import { API, HW, HISTORY_LEN, FLOW_EVENTS_MAX, NAV, isNavGroup, isNavLink, isNavSubgroup, Cap, areaPath, sectionArea, sectionToHash, hashToSection } from "./types";
import type { Section, Area, SystemInfo, NetworkInfo, HWHealth, OCStatus, PresenceInfo, VoiceStatus, ServoState, DisplayState, AudioVolume, LEDColor, SceneInfo, MonitorEvent, DisplayEvent, NavEntry, NavChild } from "./types";
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

const EMBED_SECTIONS = new Set<Section>(["api-docs", "agent-config"]);

// Sections shown without ?debug=true.
const PUBLIC_SECTIONS = new Set<Section>(["sensing", "chat", "pairing", "overview", "system", "flow", "camera", "face-owners", "bluetooth", "logs", "cli", "settings:device", "settings:wifi", "settings:voice", "settings:face", "settings:mcp", "settings:plugins", "settings:timezone", "settings:scheduled", "settings:facebook"]);

// Prune a group's children by debug mode and capability; drops subgroups left empty.
function filterNavChildren(
  children: NavChild[],
  isDebug: boolean,
  sectionVisible: (id: Section) => boolean,
): NavChild[] {
  return children.reduce<NavChild[]>((acc, c) => {
    if (isNavLink(c)) {
      if (isDebug) acc.push(c);
      return acc;
    }
    if (isNavSubgroup(c)) {
      const kept = c.children.filter((leaf) => (isDebug || PUBLIC_SECTIONS.has(leaf.id)) && sectionVisible(leaf.id));
      if (kept.length > 0) acc.push({ ...c, children: kept });
      return acc;
    }
    if (!isDebug && !PUBLIC_SECTIONS.has(c.id)) return acc;
    if (!sectionVisible(c.id)) return acc;
    acc.push(c);
    return acc;
  }, []);
}

// The capability a section requires, from its NAV leaf; undefined = always shown.
function sectionCap(id: Section): string | readonly string[] | undefined {
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      for (const child of entry.children) {
        if (isNavLink(child)) continue;
        if (isNavSubgroup(child)) {
          const leaf = child.children.find((l) => l.id === id);
          if (leaf) return leaf.cap;
          continue;
        }
        if (child.id === id) return child.cap;
      }
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

const NAV_ICONS: Record<string, LucideIcon> = {
  chat: MessageCircle,
  pairing: Handshake,
  settings: Settings,
  device: MonitorSmartphone,
  connector: Cable,
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
  agent: Hexagon,
  "agent-gateway": ExternalLink,
  "agent-config": SlidersHorizontal,
};

// Renders the lucide icon for a given nav id (leaf Section, group name, or Agent pseudo id).
const NavIcon = ({ id, size = 16 }: { id: string; size?: number }) => {
  const I = NAV_ICONS[id];
  return I ? <I size={size} strokeWidth={1.9} /> : null;
};

function allNavLeaves(): { id: Section; label: string; icon: string }[] {
  const leaves: { id: Section; label: string; icon: string }[] = [];
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      entry.children.forEach((c) => {
        if (isNavLink(c)) return;
        if (isNavSubgroup(c)) c.children.forEach((l) => leaves.push(l));
        else leaves.push(c);
      });
    } else leaves.push(entry);
  }
  leaves.push({ id: "agent-config", label: "Agent Config", icon: "◈" });
  return leaves;
}

type SearchLeaf = { id: Section; label: string; group: string | null };
function searchableLeaves(): SearchLeaf[] {
  const out: SearchLeaf[] = [];
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      entry.children.forEach((c) => {
        if (isNavLink(c)) return;
        if (isNavSubgroup(c)) {
          // Subgroup leaves show as "Facebook · Settings › Connectors" so the
          // parent trail stays visible in a flat search list.
          c.children.forEach((leaf) => out.push({ id: leaf.id, label: leaf.label, group: `${entry.label} › ${c.label}` }));
        } else {
          out.push({ id: c.id, label: c.label, group: entry.label });
        }
      });
    } else {
      out.push({ id: entry.id, label: entry.label, group: null });
    }
  }
  return out;
}

// Sidebar search box that filters nav leaves by label/group.
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

// A nested collapsible that renders the leaves of a NavSubgroup under an
// indented header, matching the sub-item style of the parent group.
function NavSubgroupItem({ entry, section, setSection, closeSidebar, leafHref }: {
  entry: Extract<NavChild, { subgroup: string }>;
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
  leafHref: (id: Section) => string;
}) {
  const hasActiveChild = entry.children.some((leaf) => leaf.id === section);
  const [open, setOpen] = useState(hasActiveChild);
  useEffect(() => { setOpen(hasActiveChild); }, [section]); // eslint-disable-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className={"lm-snav-sub lm-snav-subgroup" + (hasActiveChild ? " lm-snav-sub--active" : "")}
        style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <NavIcon id={entry.subgroup} size={15} />
          {entry.label}
        </span>
        <ChevronRight
          size={12}
          strokeWidth={2}
          style={{ color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}
        />
      </button>
      {open && (
        <div className="lm-snav-children" style={{ paddingLeft: 12 }}>
          {entry.children.map((leaf) => (
            <a
              key={leaf.id}
              href={leafHref(leaf.id)}
              className={"lm-snav-sub" + (section === leaf.id ? " lm-snav-sub--active" : "")}
              onClick={(e) => { e.preventDefault(); setSection(leaf.id); closeSidebar(); }}
            >
              <NavIcon id={leaf.id} size={15} />
              {leaf.label}
            </a>
          ))}
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
  // A group is "active" when the current section is one of its direct leaves
  // OR a leaf nested one level deeper inside a subgroup.
  const hasActiveChild = entry.children.some((c) => {
    if (isNavLink(c)) return false;
    if (isNavSubgroup(c)) return c.children.some((leaf) => leaf.id === section);
    return c.id === section;
  });
  const [open, setOpen] = useState(hasActiveChild);
  // Sync expand state on navigation only; `open` must stay manually toggleable between navigations.
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
          {entry.children.map((child) => {
            if (isNavLink(child)) {
              return (
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
              );
            }
            if (isNavSubgroup(child)) {
              return (
                <NavSubgroupItem
                  key={child.subgroup}
                  entry={child}
                  section={section}
                  setSection={setSection}
                  closeSidebar={closeSidebar}
                  leafHref={leafHref}
                />
              );
            }
            return (
              <a
                key={child.id}
                href={leafHref(child.id)}
                className={"lm-snav-sub" + (section === child.id ? " lm-snav-sub--active" : "")}
                onClick={(e) => { e.preventDefault(); setSection(child.id); closeSidebar(); }}
              >
                <NavIcon id={child.id} size={15} />
                {child.label}
              </a>
            );
          })}
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
  // OpenClaw Control UI denies framing, so open it in a new tab.
  return (
    // Parked behind CSS (.lm-nav-agent in index.css).
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

// Resolve the initial / location-derived section for an area.
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

  const area: Area = location.pathname.startsWith("/setting") ? "setting" : "monitor";

  const [section, setSectionRaw] = useState<Section>(() =>
    resolveSection(area, window.location.hash, isDebug),
  );

  const setSection = useCallback((s: Section) => {
    const targetArea = sectionArea(s);
    const hash = sectionToHash(s, targetArea);
    const path = areaPath(targetArea);
    if (targetArea !== area) {
      // Preserve ?debug=true across the area switch.
      navigate(`${path}${location.search}#${hash}`);
    } else {
      window.location.hash = hash;
    }
    setSectionRaw(s);
  }, [area, navigate, location.search]);

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

  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } finally {
      navigate("/login" + location.search);
    }
  }, [navigate, location.search]);

  // Build the real href for a nav leaf (path + serialized hash) so middle-click / open-in-new-tab land on the correct URL.
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

  useEffect(() => {
    fetch(`${API}/system/info`).then((r) => r.json()).then((r) => {
      if (r.status === 1) setSys(r.data);
    }).catch(() => {});
  }, []);

  const caps = sys?.capabilities ? new Set(sys.capabilities) : null;
  const hasCap = (c: string): boolean => !caps || caps.has(c);
  const sectionVisible = (id: Section): boolean => {
    const cap = sectionCap(id);
    return !cap || (typeof cap === "string" ? hasCap(cap) : cap.some(hasCap));
  };

  useEffect(() => {
    if (caps && !sectionVisible(section)) setSection("overview");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sys?.capabilities, section]);

  const sectionRef = useRef(section);
  useEffect(() => { sectionRef.current = section; }, [section]);

  usePolling(async (signal) => {
    const ocR = await fetch(`${API}/agent/status`, { signal }).then((r) => r.json());
    if (ocR.status === 1) setOc(ocR.data);
    setLastUpdate(new Date().toLocaleTimeString());
  }, 10_000);

  // Section-specific polling; each card's fetch commits independently.
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
        // Peripheral panels are fetched only when health reports the hardware (absent routes 404).
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
          // A malformed SSE frame drops only that frame.
        }
      },
    },
  );

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const closeSidebar = () => setSidebarOpen(false);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);

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
      <div
        className={`lm-sidebar-overlay${sidebarOpen ? " lm-sidebar-overlay--open" : ""}`}
        onClick={closeSidebar}
      />

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
        <nav style={{ padding: "10px 0", flex: 1, display: navQuery.trim() ? "none" : undefined }}>
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
          {NAV
            .filter((e) => isNavGroup(e) && (e.group === "device" || e.group === "settings"))
            .sort((a, b) => {
              const rank = (e: NavEntry) => ((e as Extract<NavEntry, { group: string }>).group === "device" ? 0 : 1);
              return rank(a) - rank(b);
            })
            .map((entry) => {
              const group = entry as Extract<NavEntry, { group: string }>;
              const filtered = { ...group, children: filterNavChildren(group.children, isDebug, sectionVisible) };
              if (filtered.children.length === 0) return null;
              return <NavGroupItem key={group.group} entry={filtered} section={section} setSection={setSection} closeSidebar={closeSidebar} leafHref={leafHref} />;
            })}
          {isDebug && <AgentGWMenu section={section} setSection={setSection} closeSidebar={closeSidebar} />}
          {NAV
            .filter((e) => (isNavGroup(e) ? (e.group !== "settings" && e.group !== "device") : e.id !== "chat"))
            .map((entry) => {
              if (isNavGroup(entry)) {
                const filtered = { ...entry, children: filterNavChildren(entry.children, isDebug, sectionVisible) };
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

      <main style={S.main}>
        <div style={S.topbar}>
          <button
            className="lm-hamburger"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Menu"
          >☰</button>
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

        <div style={{
          ...S.content,
          ...(section === "chat" ? { padding: 0, overflow: "hidden" } : {}),
          ...(EMBED_SECTIONS.has(section) ? { padding: 0, overflow: "hidden" } : {}),
          // display:flex is load-bearing: SettingsPanel scrolls via flex:1/minHeight:0.
          ...(section.startsWith("settings:")
            ? { padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" as const }
            : {}),
        }} className="lm-content">
          {/* Chat stays outside this keyed wrapper so it is never remounted. */}
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
                // Commit on HAL's ack; a 409 (HW mic switch off) leaves state to the poll.
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
                setVoice((prev) => (prev && prev.tts_speaking !== tts ? { ...prev, tts_speaking: tts } : prev));
                setMusicPlaying((prev) => (prev === music ? prev : music));
              }}
              onEmotionPick={(e) => {
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
              // Via the admin-gated /api/hardware proxy; nginx /hw/ is loopback-only.
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
          {section.startsWith("settings:") && (
            <SettingsPanel activeSection={section.slice("settings:".length) as SettingsSectionId} />
          )}
          </div>
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
