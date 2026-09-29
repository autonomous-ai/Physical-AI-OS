import { useEffect, useRef, useState, useCallback, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Paperclip, X, Copy, Check, RotateCcw, Download, ArrowDown, ArrowUp,
  Pin, ChevronRight, Sparkles, Plus, Trash2, History,
  Wrench, Lightbulb, Cog, Music, Palette, Search, Smile, ChevronDown, Square,
} from "lucide-react";
import { API } from "./types";
import { getDeviceConfig } from "@/lib/api";
import {
  putChatImages, getAllChatImages, deleteChatImages, pruneChatImages, clearChatImages,
} from "@/lib/chatImageStore";
import { useT, setLanguage } from "@/lib/i18n";
import type { DisplayEvent, MonitorEvent } from "./types";
import { PlusMenu, type SkillsAction } from "./chat/PlusMenu";
import { WriteSkillModal } from "./chat/WriteSkillModal";
import { UploadSkillModal } from "./chat/UploadSkillModal";
import { BrowseSkillsModal } from "./chat/BrowseSkillsModal";
import { ManageSkillsModal } from "./chat/ManageSkillsModal";
import { AgentFiles } from "./chat/AgentFiles";
import { pendingReplyKey, replayedReply } from "./chat/pendingReplies";

const CREATE_SKILL_WITH_AGENT_PROMPT =
  "Let's create a skill together using your skill-creator skill. First ask me what the skill should do.";

// Accepts http(s) URLs and repairs mangled schemes like "hthtps://"; null otherwise.
function normalizeHref(raw: string): string | null {
  const m = raw.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/(.*)$/);
  if (!m) return null;
  const scheme = m[1].toLowerCase();
  if (scheme === "http" || scheme === "https") return raw;
  if (/^[htps]{2,8}$/.test(scheme) && scheme.includes("tp")) {
    return (scheme.endsWith("s") ? "https://" : "http://") + m[2];
  }
  return null;
}

const linkStyle: React.CSSProperties = { color: "var(--lm-teal)", textDecoration: "underline" };

// linkifyPlain makes URLs clickable in user-authored bubbles WITHOUT any other markdown transformation
function linkifyPlain(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /[a-zA-Z]{2,10}:\/\/[^\s<>)"]+/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    const href = normalizeHref(match[0]);
    if (!href) continue;
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(
      <a key={`${keyPrefix}-${match.index}`} href={href} target="_blank" rel="noopener noreferrer" style={linkStyle}>
        {match[0].length > 50 ? match[0].slice(0, 50) + "…" : match[0]}
      </a>,
    );
    last = match.index + match[0].length;
  }
  if (parts.length === 0) return [text];
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

// Inline markdown: bold, italic, strikethrough, code, links and bare URLs.
function renderInline(line: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`|\[([^\]]+)\]\(([a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s)]+)\)|([a-zA-Z]{2,10}:\/\/[^\s<>)"]+))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(line)) !== null) {
    if (match.index > last) parts.push(line.slice(last, match.index));
    const k = `${keyPrefix}-${match.index}`;
    if (match[2]) parts.push(<strong key={k}>{match[2]}</strong>);
    else if (match[3]) parts.push(<em key={k}>{match[3]}</em>);
    else if (match[4]) parts.push(<del key={k} style={{ opacity: 0.6 }}>{match[4]}</del>);
    else if (match[5]) parts.push(<code key={k} style={{ background: "color-mix(in srgb, var(--lm-amber) 12%, var(--lm-surface))", color: "var(--lm-amber)", padding: "1.5px 5px", borderRadius: 4, fontSize: "0.86em", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", border: "1px solid color-mix(in srgb, var(--lm-amber) 18%, transparent)" }}>{match[5]}</code>);
    else if (match[6] && match[7]) {
      parts.push(<a key={k} href={normalizeHref(match[7]) ?? match[7]} target="_blank" rel="noopener noreferrer" style={linkStyle}>{match[6]}</a>);
    } else if (match[8]) {
      const href = normalizeHref(match[8]);
      if (href) parts.push(<a key={k} href={href} target="_blank" rel="noopener noreferrer" style={linkStyle}>{match[8].length > 50 ? match[8].slice(0, 50) + "…" : match[8]}</a>);
      else parts.push(match[8]);
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}

function renderMarkdown(text: string): ReactNode {
  const lines = text.split("\n");
  const result: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    if (lines[i].startsWith("```")) {
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      result.push(
        <pre key={`cb-${i}`} style={{
          background: "var(--lm-bg)", padding: "10px 13px", borderRadius: 8,
          fontSize: "0.82em", lineHeight: 1.5, overflowX: "auto", margin: "6px 0",
          border: "1px solid var(--lm-border)", whiteSpace: "pre-wrap", wordBreak: "break-word",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          color: "var(--lm-text-dim)",
        }}>
          <code style={{ fontFamily: "inherit" }}>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const headingMatch = lines[i].match(/^(#{1,6})\s+(.+)/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const sizes = [0, "1.3em", "1.15em", "1.05em", "1em", "0.95em", "0.9em"];
      result.push(
        <div key={`h-${i}`} style={{
          fontSize: sizes[level], fontWeight: 600,
          margin: "6px 0 2px", lineHeight: 1.3,
        }}>
          {renderInline(headingMatch[2], `h-${i}`)}
        </div>,
      );
      i++;
      continue;
    }

    if (/^([-*_])\1{2,}\s*$/.test(lines[i])) {
      result.push(<hr key={`hr-${i}`} style={{ border: "none", borderTop: "1px solid var(--lm-border)", margin: "8px 0" }} />);
      i++;
      continue;
    }

    if (lines[i].startsWith("> ")) {
      const quoteLines: ReactNode[] = [];
      while (i < lines.length && lines[i].startsWith("> ")) {
        quoteLines.push(
          <div key={`bq-${i}`}>{renderInline(lines[i].slice(2), `bq-${i}`)}</div>,
        );
        i++;
      }
      result.push(
        <div key={`blockquote-${i}`} style={{
          borderLeft: "3px solid rgba(245,158,11,0.4)", paddingLeft: 10,
          margin: "4px 0", color: "var(--lm-text-muted)", fontStyle: "italic",
        }}>
          {quoteLines}
        </div>,
      );
      continue;
    }

    if (lines[i].includes("|") && i + 1 < lines.length && /^\|?\s*[-:]+[-| :]*$/.test(lines[i + 1])) {
      const headerCells = lines[i].split("|").map((c) => c.trim()).filter(Boolean);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|")) {
        rows.push(lines[i].split("|").map((c) => c.trim()).filter(Boolean));
        i++;
      }
      result.push(
        <div key={`tw-${i}`} style={{
          overflowX: "auto", margin: "6px 0",
          border: "1px solid var(--lm-border)", borderRadius: 8,
        }}>
          <table style={{
            borderCollapse: "collapse", fontSize: "0.88em", width: "100%",
          }}>
            <thead>
              <tr>
                {headerCells.map((h, ci) => (
                  <th key={ci} style={{
                    padding: "6px 10px", borderBottom: "1px solid var(--lm-border-hi)",
                    background: "color-mix(in srgb, var(--lm-amber) 8%, var(--lm-surface))",
                    textAlign: "left", fontWeight: 700, fontSize: "0.92em",
                    color: "var(--lm-text)", whiteSpace: "nowrap",
                  }}>{renderInline(h, `th-${i}-${ci}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri} style={{
                  background: ri % 2 === 1 ? "color-mix(in srgb, var(--lm-text) 3%, transparent)" : "transparent",
                }}>
                  {row.map((cell, ci) => (
                    <td key={ci} style={{
                      padding: "5px 10px",
                      borderBottom: ri < rows.length - 1 ? "1px solid var(--lm-border)" : "none",
                    }}>{renderInline(cell, `td-${i}-${ri}-${ci}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^[-*]\s/.test(lines[i])) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i])) {
        items.push(<li key={`li-${i}`} style={{ margin: "2px 0", lineHeight: 1.55, paddingLeft: 2 }}>{renderInline(lines[i].replace(/^[-*]\s/, ""), `ul-${i}`)}</li>);
        i++;
      }
      result.push(<ul key={`ul-${i}`} style={{ margin: "5px 0", paddingLeft: 22 }}>{items}</ul>);
      continue;
    }

    if (/^\d+\.\s/.test(lines[i])) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(<li key={`oli-${i}`} style={{ margin: "2px 0", lineHeight: 1.55, paddingLeft: 2 }}>{renderInline(lines[i].replace(/^\d+\.\s/, ""), `ol-${i}`)}</li>);
        i++;
      }
      result.push(<ol key={`ol-${i}`} style={{ margin: "5px 0", paddingLeft: 22 }}>{items}</ol>);
      continue;
    }

    const inline = renderInline(lines[i], `l-${i}`);
    result.push(
      <span key={`s-${i}`}>
        {i > 0 && result.length > 0 && <br />}
        {inline.length > 0 ? inline : ""}
      </span>,
    );
    i++;
  }

  return result;
}

// Strip HW control markers; patterns mirror the Go executor grammar (handler_hw.go) exactly, never looser.
const HW_LINK_RE = /\[([^\]]*)\]\(\s*HW:\s*(?:\/[^(){:\s]+(?::[^(){:\s]+)*)(?::\{[^}]*\})?:?\s*\)/gi;
const HW_MARKER_RE = /\[HW:\/[^{\]]*(?:\{[^}]*\})?\]/g;
function stripHWMarkers(text: string): string {
  return text
    .replace(HW_LINK_RE, (_m, label: string) =>
      /^hw:/i.test(label) ? "" : label,
    )
    .replace(HW_MARKER_RE, "")
    .trim();
}

// Dynamic wire payload (SSE detail blobs, tool args), read defensively.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type JsonObject = Record<string, any>;

type IconKind = "tool" | "led" | "scene" | "led_off" | "music" | "servo" | "emotion" | "search";
interface ToolChip {
  id: string;
  iconKind: IconKind;
  label: string;
  detail?: string;
  args?: JsonObject;
  result?: string;
}

const TOOL_EVENT_TYPES = new Set(["tool_call", "hw_emotion", "hw_led", "hw_audio", "hw_servo", "led_set", "led_off"]);

// Tools already shown via hw_* events; skip their generic chip.
const HW_SHADOW_TOOLS = new Set(["set_emotion", "set_led", "play_music", "move_servo"]);

const CHAT_SUGGESTIONS: { icon: ReactNode; key: string }[] = [
  { icon: <Music size={14} />, key: "chat.suggest.music" },
  { icon: <Smile size={14} />, key: "chat.suggest.howAreYou" },
  { icon: <Lightbulb size={14} />, key: "chat.suggest.warmLight" },
  { icon: <Sparkles size={14} />, key: "chat.suggest.whatCanYouDo" },
];

// Map a tool name to which lucide icon best represents it.
function iconForTool(name: string): IconKind {
  const n = name.toLowerCase();
  if (n.includes("search") || n.includes("lookup") || n.includes("query")) return "search";
  return "tool";
}

// Render a tool chip's icon as a Lucide component at the given size.
function renderToolIcon(kind: IconKind, size = 12) {
  switch (kind) {
    case "led":     return <Lightbulb size={size} />;
    case "scene":   return <Palette size={size} />;
    case "led_off": return <Lightbulb size={size} />;
    case "music":   return <Music size={size} />;
    case "servo":   return <Cog size={size} />;
    case "emotion": return <Smile size={size} />;
    case "search":  return <Search size={size} />;
    case "tool":
    default:        return <Wrench size={size} />;
  }
}

// Compact one-line preview of tool args.
function summarizeArgs(args: JsonObject | undefined): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  for (const key of ["query", "q", "url", "command", "text", "name", "recording"]) {
    if (typeof args[key] === "string" && args[key]) {
      const v = args[key];
      return v.length > 80 ? v.slice(0, 80) + "…" : v;
    }
  }
  try {
    const j = JSON.stringify(args);
    return j.length > 80 ? j.slice(0, 80) + "…" : j;
  } catch {
    return undefined;
  }
}

interface ToolEventInput {
  type: string;
  summary: string;
  id: string;
  detail?: JsonObject | null;
  phase?: string;
}

// Turns a tool_call flow event into a chip.
function parseToolChip(ev: ToolEventInput): ToolChip | null {
  const s = ev.summary;
  switch (ev.type) {
    case "hw_emotion": {
      const m = s.match(/"emotion"\s*:\s*"([^"]+)"/);
      return { id: ev.id, iconKind: "emotion", label: m ? m[1] : "emotion" };
    }
    case "hw_led": {
      if (s.includes("/scene/")) {
        const m = s.match(/\/scene\/(\w+)/);
        return { id: ev.id, iconKind: "scene", label: m ? `scene: ${m[1]}` : "LED scene" };
      }
      if (s.includes("/led/off")) return { id: ev.id, iconKind: "led_off", label: "LED off" };
      const m = s.match(/"hex"\s*:\s*"([^"]+)"/);
      return { id: ev.id, iconKind: "led", label: m ? `LED ${m[1]}` : "LED" };
    }
    case "led_off": return { id: ev.id, iconKind: "led_off", label: "LED off" };
    case "led_set": return null;
    case "hw_audio": return { id: ev.id, iconKind: "music", label: "music" };
    case "hw_servo": {
      if (s.includes("/aim")) return { id: ev.id, iconKind: "servo", label: "servo aim" };
      if (s.includes("/play")) {
        const m = s.match(/\/play\/(\w+)/);
        return { id: ev.id, iconKind: "servo", label: m ? `servo: ${m[1]}` : "servo play" };
      }
      return { id: ev.id, iconKind: "servo", label: "servo" };
    }
    case "tool_call": {
      const d = ev.detail as JsonObject | undefined;
      // phase is top-level on newer servers, nested under detail on older ones.
      const phase: string = ev.phase ?? d?.data?.phase ?? d?.phase ?? "";
      const name: string =
        d?.tool ?? d?.data?.tool ?? d?.data?.name ?? d?.name
        ?? (s.match(/^(\w+)/)?.[1] ?? "tool");
      if (HW_SHADOW_TOOLS.has(name)) return null;
      let argsObj: JsonObject | undefined;
      const rawArgs = d?.args ?? d?.data?.args;
      if (rawArgs) {
        try {
          argsObj = typeof rawArgs === "string" ? JSON.parse(rawArgs) : rawArgs;
        } catch { /* keep undefined */ }
      }
      const isResult = phase === "result" || phase === "end";
      let resultText: string | undefined;
      if (isResult) {
        const m = s.match(/done:\s*(.+)$/);
        resultText = m ? m[1] : "completed";
      }
      return {
        id: ev.id,
        iconKind: iconForTool(name),
        label: name,
        detail: summarizeArgs(argsObj),
        args: argsObj,
        result: resultText,
      };
    }
    default: return null;
  }
}

const CONVOS_KEY = "os_chat_convos";
const ACTIVE_KEY = "os_chat_active";
const MAX_MESSAGES = 200;
const MAX_CONVOS = 50;

// Chat history TTL: stored content can be personal, so it is not kept indefinitely.
const HISTORY_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// Idle (not absolute) give-up window: every run event refreshes it.
const REPLY_IDLE_TIMEOUT_MS = 12 * 60 * 1000;
const RECOVERY_IDLE_TIMEOUT_MS = REPLY_IDLE_TIMEOUT_MS;
// Replay poll while a reply is pending, in case the live SSE drops.
const PENDING_FLOW_REPLAY_MS = 3_000;

interface ConvosEnvelope {
  savedAt: number;
  convos: Conversation[];
}

const clockTime = () =>
  new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

interface ChatMessage {
  id: string;
  role: "user" | "agent";
  text: string;
  time: string;
  ts?: number;
  date?: string;
  imageUrls?: string[];
  fileName?: string;
  fileSize?: number;
  attachmentCount?: number;
  runId?: string;
  pending?: boolean;
  error?: boolean;
  tools?: ToolChip[];
  tokenUsage?: { input: number; output: number; cacheRead?: number; cacheWrite?: number; total: number };
}

interface Conversation {
  id: string;
  title: string;
  createdAt: number;
  messages: ChatMessage[];
  manualTitle?: boolean;
  pinned?: boolean;
}

const EMPTY_MESSAGES: ChatMessage[] = [];

function loadConvos(): Conversation[] {
  try {
    const raw = localStorage.getItem(CONVOS_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw) as Conversation[] | ConvosEnvelope;

    if (Array.isArray(parsed)) {
      return parsed.map((c) => ({ ...c, messages: cleanPending(c.messages) }));
    }

    if (parsed && typeof parsed.savedAt === "number" && Array.isArray(parsed.convos)) {
      if (Date.now() - parsed.savedAt > HISTORY_TTL_MS) {
        localStorage.removeItem(CONVOS_KEY);
        localStorage.removeItem(ACTIVE_KEY);
        return [];
      }
      return parsed.convos.map((c) => ({ ...c, messages: cleanPending(c.messages) }));
    }

    return [];
  } catch {
    return [];
  }
}

// Pending turns younger than this are recovered across a reload.
const PENDING_RECOVERY_WINDOW_MS = 10 * 60 * 1000;

function cleanPending(msgs: ChatMessage[]): ChatMessage[] {
  return msgs.map((m) => {
    if (!m.pending) return m;
    if (m.runId && m.ts && Date.now() - m.ts < PENDING_RECOVERY_WINDOW_MS) return m;
    return { ...m, pending: false, text: m.text || "…", error: !m.text };
  });
}

function titleFromMessages(msgs: ChatMessage[]): string {
  const userMsg = msgs.find((m) => m.role === "user");
  if (!userMsg) return "New chat";
  const agentMsg = msgs.find((m) => m.role === "agent" && !m.pending && !m.error && m.text && m.text !== "…");
  if (agentMsg) {
    const q = userMsg.text.length > 20 ? userMsg.text.slice(0, 20) + "…" : userMsg.text;
    const a = agentMsg.text.replace(/\n/g, " ");
    const aShort = a.length > 20 ? a.slice(0, 20) + "…" : a;
    return `${q} → ${aShort}`;
  }
  return userMsg.text.length > 36 ? userMsg.text.slice(0, 36) + "…" : userMsg.text;
}

// A conversation should follow the last message, not the moment it was first created.
function conversationActivityAt(convo: Conversation): number {
  const lastMessage = convo.messages[convo.messages.length - 1];
  return lastMessage?.ts ?? convo.createdAt;
}

function newestFirst(convos: Conversation[]): Conversation[] {
  return [...convos].sort((a, b) => conversationActivityAt(b) - conversationActivityAt(a));
}

function saveConvos(convos: Conversation[]) {
  try {
    const trimmed = newestFirst(convos).slice(0, MAX_CONVOS).map((c) => ({
      ...c,
      // imageUrls are too large for localStorage; they live in IndexedDB (chatImageStore).
      messages: c.messages.slice(-MAX_MESSAGES).map(({ imageUrls: _, ...m }) => m),
    }));
    const envelope: ConvosEnvelope = { savedAt: Date.now(), convos: trimmed };
    localStorage.setItem(CONVOS_KEY, JSON.stringify(envelope));
  } catch {
    // Storage full or blocked; the cache is optional.
  }
}

// clearLocalChatHistory wipes the conversation cache from localStorage
function clearLocalChatHistory() {
  try {
    localStorage.removeItem(CONVOS_KEY);
    localStorage.removeItem(ACTIVE_KEY);
  } catch {
    // No localStorage: nothing to clear.
  }
  void clearChatImages();
}

function loadActiveId(): string | null {
  try { return localStorage.getItem(ACTIVE_KEY); } catch { return null; }
}

function saveActiveId(id: string | null) {
  try {
    if (id) localStorage.setItem(ACTIVE_KEY, id);
    else localStorage.removeItem(ACTIVE_KEY);
  } catch {
    // Best-effort, like saveConvos.
  }
}

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  return new Promise((resolve) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    resolve();
  });
}

const MAX_FILE_SIZE = 10 * 1024 * 1024;
const MAX_ATTACHMENTS = 8;

type Attachment = {
  id: string;
  name: string;
  mime: string;
  size: number;
  isImage: boolean;
  base64: string;
  previewUrl: string | null;
};

interface Props {
  events: DisplayEvent[];
  // False when the tab is hidden; the live SSE only opens while active.
  isActive: boolean;
}

export function ChatSection({ events, isActive }: Props) {
  const navigate = useNavigate();
  const t = useT();
  const [convos, setConvos] = useState<Conversation[]>(loadConvos);
  const [activeId, setActiveId] = useState<string | null>(() => {
    const saved = loadActiveId();
    return saved && loadConvos().some((c) => c.id === saved) ? saved : null;
  });
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  // Rehydrate images from IndexedDB and prune orphans (fresh entries are age-guarded).
  useEffect(() => {
    let cancelled = false;
    getAllChatImages().then((stored) => {
      if (cancelled) return;
      const keepIds = new Set(loadConvos().flatMap((c) => c.messages.map((m) => m.id)));
      void pruneChatImages(keepIds);
      if (stored.size === 0) return;
      setConvos((prev) => prev.map((c) => ({
        ...c,
        messages: c.messages.map((m) =>
          !m.imageUrls?.length && stored.has(m.id) ? { ...m, imageUrls: stored.get(m.id) } : m,
        ),
      })));
    });
    return () => { cancelled = true; };
  }, []);
  const [editTitle, setEditTitle] = useState("");
  const [search, setSearch] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [hoveredConvoId, setHoveredConvoId] = useState<string | null>(null);

  const [modelLabel, setModelLabel] = useState<string>("");
  useEffect(() => {
    getDeviceConfig()
      .then((cfg) => {
        setLanguage(cfg.stt_language);
        const primary = cfg.llm_model;
        if (!primary) return;
        const raw = primary.includes("/") ? primary.split("/").pop() ?? primary : primary;
        const compact = raw
          .replace(/^claude-/i, "")
          .replace(/-\d{8}$/, "");
        setModelLabel(compact);
      })
      .catch(() => {});
  }, []);

  // Shared compact icon-button style for the per-row pin/delete actions.
  const hoverIconBtnStyle = (color: string): React.CSSProperties => ({
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    width: 22, height: 22, padding: 0, borderRadius: 4,
    background: "transparent", border: "none", cursor: "pointer",
    color,
  });

  const headerPillBtnStyle: React.CSSProperties = {
    background: "var(--lm-surface)",
    border: "1px solid var(--lm-border)",
    borderRadius: 6,
    cursor: "pointer",
    color: "var(--lm-text-dim)",
    padding: "4px 10px",
    display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: 11, fontWeight: 600,
  };
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(
    () => typeof window !== "undefined" && window.innerWidth >= 768,
  );
  const [dragging, setDragging] = useState(false);
  const [skillsView, setSkillsView] = useState<SkillsAction | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingRunIdRef = useRef<string | null>(null);
  // Lets Stop win over a late HTTP response that would revive the reply.
  const pendingLocalReplyIdRef = useRef<string | null>(null);
  const sendSequenceRef = useRef(0);
  const sendAbortRef = useRef<AbortController | null>(null);
  // Pairs a steered turn that OpenClaw re-fires under a new run id.
  const pendingUserTextRef = useRef<string | null>(null);
  const resolvedIds = useRef<Set<string>>(new Set());
  const deltaBufRef = useRef<Map<string, string>>(new Map());
  // Bubble time = when the reply's first content arrived.
  const firstReplyTime = (runId: string, m: ChatMessage) =>
    deltaBufRef.current.has(runId) ? m.time : clockTime();
  const thinkingBufRef = useRef<Map<string, string>>(new Map());
  const rafRef = useRef<number | null>(null);
  const dirtyRef = useRef(false);
  const [thinkingText, setThinkingText] = useState<string | null>(null);
  const [toolChips, setToolChips] = useState<ToolChip[]>([]);
  const [replayedFlowEvents, setReplayedFlowEvents] = useState<MonitorEvent[]>([]);

  const active = convos.find((c) => c.id === activeId) ?? null;
  const messages = active?.messages ?? EMPTY_MESSAGES;

  useEffect(() => { saveConvos(convos); }, [convos]);
  useEffect(() => { saveActiveId(activeId); }, [activeId]);

  // Window-level: nothing inside the lightbox holds focus.
  useEffect(() => {
    if (!lightboxUrl) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setLightboxUrl(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightboxUrl]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "n") {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });

  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  }, [input]);

  const onScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setShowScrollBtn(!nearBottom);
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const updateMessages = useCallback((fn: (prev: ChatMessage[]) => ChatMessage[]) => {
    setConvos((prev) =>
      prev.map((c) => {
        if (c.id !== activeId) return c;
        const updated = fn(c.messages);
        const autoTitle = !c.manualTitle ? titleFromMessages(updated) : c.title;
        return { ...c, messages: updated, title: autoTitle };
      }),
    );
  }, [activeId]);

  // One shared idle watchdog, refreshed by SSE, send and reload recovery.
  const replyWatchdogRef = useRef<{ runId: string; convoId: string; timer: number } | null>(null);

  const clearReplyWatchdog = useCallback(() => {
    const w = replyWatchdogRef.current;
    if (w) window.clearTimeout(w.timer);
    replyWatchdogRef.current = null;
  }, []);

  const pendingRepliesKey = pendingReplyKey(convos);
  useEffect(() => {
    if (pendingRepliesKey === "[]") return;
    let cancelled = false;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const replay = async () => {
      try {
        const response = await fetch(`${API}/agent/flow-events?last=500`, { signal: abort.signal });
        const payload = await response.json();
        const next = payload?.data?.events;
        if (!cancelled && Array.isArray(next)) {
          const flowEvents = next as MonitorEvent[];
          setReplayedFlowEvents(flowEvents);
          const replies = new Map<string, string>();
          for (const runId of JSON.parse(pendingRepliesKey) as string[]) {
            if (runId === pendingRunIdRef.current) continue;
            const text = replayedReply(flowEvents, runId);
            if (text !== undefined) replies.set(runId, stripHWMarkers(text));
          }
          if (replies.size > 0) {
            setConvos((prev) => prev.map((conversation) => ({
              ...conversation,
              messages: conversation.messages.map((message) =>
                message.role === "agent" && message.pending && message.runId && replies.has(message.runId)
                  ? { ...message, text: replies.get(message.runId)!, pending: false }
                  : message,
              ),
            })));
          }
        }
      } catch {
        // Live SSE and the next poll remain available; keep the pending turn.
      } finally {
        // Do not overlap requests on a slow device.
        if (!cancelled) timer = setTimeout(() => void replay(), PENDING_FLOW_REPLAY_MS);
      }
    };
    void replay();
    return () => {
      cancelled = true;
      abort.abort();
      clearTimeout(timer);
    };
  }, [pendingRepliesKey]);

  const armReplyWatchdog = useCallback(
    (runId: string, convoId: string, ms: number) => {
      clearReplyWatchdog();
      const timer = window.setTimeout(() => {
        replyWatchdogRef.current = null;
        if (pendingRunIdRef.current !== runId) return;
        pendingRunIdRef.current = null;
        pendingUserTextRef.current = null;
        setSending(false);
        setThinkingText(null);
        setToolChips([]);
        const streamed = deltaBufRef.current.get(runId);
        deltaBufRef.current.delete(runId);
        thinkingBufRef.current.delete(runId);
        toolChipsRef.current.clear();
        setConvos((prev) =>
          prev.map((c) =>
            c.id === convoId
              ? {
                  ...c,
                  messages: c.messages.map((m) =>
                    m.runId === runId && m.pending
                      ? { ...m, text: streamed || "⏱ no response", pending: false, error: !streamed }
                      : m,
                  ),
                }
              : c,
          ),
        );
      }, ms);
      replyWatchdogRef.current = { runId, convoId, timer };
    },
    [clearReplyWatchdog],
  );

  const toolChipsRef = useRef<Map<string, ToolChip>>(new Map());
  const tokenUsageRef = useRef<ChatMessage["tokenUsage"]>(undefined);

  useEffect(() => {
    // Close the EventSource while the tab is hidden (connection-slot limit).
    let es: EventSource | null = null;

    // Batch delta/thinking updates into a single render per animation frame
    const scheduleFlush = () => {
      if (rafRef.current != null) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        if (!dirtyRef.current) return;
        dirtyRef.current = false;
        const p = pendingRunIdRef.current;
        if (!p) return;
        setThinkingText(thinkingBufRef.current.get(p) ?? null);
        const buf = deltaBufRef.current.get(p);
        if (buf) {
          const cleaned = stripHWMarkers(buf);
          updateMessages((prev) =>
            prev.map((m) =>
              m.runId === p && m.role === "agent" && m.pending
                ? { ...m, text: cleaned }
                : m,
            ),
          );
        }
      });
    };

    const resolveRun = (runId: string) => {
      clearReplyWatchdog();
      const buf = deltaBufRef.current.get(runId) ?? "";
      const text = stripHWMarkers(buf || "…");
      deltaBufRef.current.delete(runId);
      thinkingBufRef.current.delete(runId);
      resolvedIds.current.add(runId);
      pendingRunIdRef.current = null;
      pendingUserTextRef.current = null;
      setSending(false);
      setThinkingText(null);
      const chips = Array.from(toolChipsRef.current.values());
      const savedChips = chips.length > 0 ? chips : undefined;
      toolChipsRef.current.clear();
      setToolChips([]);
      const usage = tokenUsageRef.current;
      tokenUsageRef.current = undefined;
      return { text, savedChips, usage };
    };

    const onMessage = (msg: MessageEvent) => {
      try {
        const ev = JSON.parse(msg.data) as MonitorEvent;
        if (!ev.type) return;
        const pending = pendingRunIdRef.current;
        if (!pending || resolvedIds.current.has(pending)) return;

        const evRunId = ev.runId ?? (ev.detail as JsonObject | undefined)?.run_id ?? (ev.detail as JsonObject | undefined)?.runId;
        if (!evRunId || evRunId !== pending) return;
        // The run is alive: push the idle deadline back.
        const watchdog = replyWatchdogRef.current;
        if (watchdog && watchdog.runId === pending) {
          armReplyWatchdog(pending, watchdog.convoId, REPLY_IDLE_TIMEOUT_MS);
        }

        // Tool call chips: the start and result phases of one call merge into a single chip.
        const detailNode = (ev.detail as JsonObject | undefined)?.node;
        const isToolCall =
          TOOL_EVENT_TYPES.has(ev.type) ||
          (ev.type === "flow_event" && detailNode && (
            detailNode === "tool_call" ||
            detailNode === "hw_emotion" ||
            detailNode === "hw_led" ||
            detailNode === "hw_audio" ||
            detailNode === "hw_servo" ||
            detailNode === "led_set" ||
            detailNode === "led_off"
          ));
        if (isToolCall) {
          const normalizedType = TOOL_EVENT_TYPES.has(ev.type) ? ev.type : detailNode;
          const chip = parseToolChip({
            type: normalizedType,
            summary: ev.summary,
            id: ev.id,
            detail: ev.detail,
            phase: ev.phase ?? (ev.detail as JsonObject | undefined)?.data?.phase,
          });
          if (chip) {
            const argsKey = chip.detail ?? (chip.args ? JSON.stringify(chip.args) : "");
            const key = chip.iconKind + ":" + chip.label + ":" + argsKey;
            const existing = toolChipsRef.current.get(key);
            const merged: ToolChip = existing ? {
              ...existing,
              ...chip,
              args: chip.args ?? existing.args,
              detail: chip.detail ?? existing.detail,
              result: chip.result ?? existing.result,
            } : chip;
            toolChipsRef.current.set(key, merged);
            setToolChips(Array.from(toolChipsRef.current.values()));
          }
        }

        const isTokenUsage =
          ev.type === "token_usage" ||
          (ev.type === "flow_event" && (ev.detail as JsonObject | undefined)?.node === "token_usage");
        if (isTokenUsage) {
          const d = ev.detail as JsonObject | undefined;
          const src = (d?.data && typeof d.data === "object") ? d.data : d;
          if (src) {
            const num = (v: unknown) => typeof v === "number" ? v : parseInt(String(v ?? "0"), 10);
            tokenUsageRef.current = {
              input: num(src.input_tokens ?? src.input),
              output: num(src.output_tokens ?? src.output),
              cacheRead: num(src.cache_read_tokens ?? src.cache_read) || undefined,
              cacheWrite: num(src.cache_write_tokens ?? src.cache_write) || undefined,
              total: num(src.total_tokens ?? src.total),
            };
          }
          return;
        }

        if (ev.type === "thinking") {
          const delta = ev.summary ?? "";
          if (delta) {
            const buf = thinkingBufRef.current.get(pending) ?? "";
            thinkingBufRef.current.set(pending, buf + delta);
            dirtyRef.current = true;
            scheduleFlush();
          }
          return;
        }

        if (ev.type === "assistant_delta") {
          const delta = ev.summary ?? "";
          if (delta) {
            const buf = deltaBufRef.current.get(pending) ?? "";
            if (!buf) {
              const firstTokenTime = clockTime();
              updateMessages((prev) =>
                prev.map((m) => m.runId === pending && m.pending ? { ...m, time: firstTokenTime } : m),
              );
            }
            deltaBufRef.current.set(pending, buf + delta);
            dirtyRef.current = true;
            scheduleFlush();
          }
          return;
        }

        if (ev.type === "chat_response") {
          const d = ev.detail as JsonObject | undefined;
          const chatMsg = d?.message ?? ev.summary ?? "";

          if (chatMsg === "[no reply]") {
            const { text, savedChips, usage } = resolveRun(pending);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: text === "…" ? "…" : text, pending: false, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          if (ev.state === "complete" || ev.state === "final") {
            const finalText = chatMsg || deltaBufRef.current.get(pending) || "";
            // Skip empty finals: OpenClaw sends acknowledgment-only finals.
            if (!finalText) return;
            const { savedChips, usage } = resolveRun(pending);
            const cleaned = stripHWMarkers(finalText);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: cleaned, time: firstReplyTime(pending, m), pending: false, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          if (ev.state === "error") {
            const errMsg = (ev.detail as Record<string, string>)?.error ?? ev.summary ?? "error";
            const { savedChips, usage } = resolveRun(pending);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: errMsg, pending: false, error: true, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          if (chatMsg && !deltaBufRef.current.has(pending)) {
            const cleaned = stripHWMarkers(chatMsg);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: cleaned, time: firstReplyTime(pending, m) }
                  : m,
              ),
            );
          }
        }
      } catch {
        // ignore malformed SSE data
      }
    };

    const open = () => {
      if (es !== null) return;
      es = new EventSource(`${API}/agent/events`, { withCredentials: true });
      es.onmessage = onMessage;
    };
    const close = () => {
      if (es !== null) { es.close(); es = null; }
    };
    const shouldBeOpen = () => isActive && !document.hidden;
    const onVisibility = () => {
      if (shouldBeOpen()) open(); else close();
    };

    if (shouldBeOpen()) open();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close();
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
  }, [updateMessages, isActive, armReplyWatchdog, clearReplyWatchdog]);

  useEffect(() => {
    const pending = pendingRunIdRef.current;
    if (!pending || resolvedIds.current.has(pending)) return;

    // Steered/merged turn: OpenClaw re-fires the input under a new run id; pair it by the user's text.
    const observedEvents = Array.from(
      new Map([...replayedFlowEvents, ...events].map((event) => [event.id, event])).values(),
    );
    const acceptedRunIds = new Set<string>([pending]);
    const userText = pendingUserTextRef.current;
    if (userText) {
      let emptyIdx = -1;
      for (let i = 0; i < observedEvents.length; i++) {
        const ev = observedEvents[i];
        if (ev.type !== "flow_event") continue;
        const d = ev.detail as JsonObject | undefined;
        if (d?.node !== "chat_final_empty") continue;
        const r = ev.runId ?? d?.run_id ?? d?.data?.run_id;
        if (r === pending) { emptyIdx = i; break; }
      }
      if (emptyIdx >= 0) {
        const expected = userText.trim().toLowerCase();
        for (let i = emptyIdx + 1; i < observedEvents.length; i++) {
          const ev = observedEvents[i];
          if (ev.type !== "flow_event") continue;
          const d = ev.detail as JsonObject | undefined;
          if (d?.node !== "chat_input") continue;
          if (d?.data?.source !== "channel") continue;
          const msg = String(d?.data?.message ?? "");
          if (!msg) continue;
          const norm = msg.replace(/^\[[^\]]+\]\s*/, "").trim().toLowerCase();
          if (norm !== expected) continue;
          const uuidId = d?.data?.run_id ?? ev.runId;
          if (uuidId && uuidId !== pending) {
            acceptedRunIds.add(uuidId);
            break;
          }
        }
      }
    }

    for (const ev of [...observedEvents].reverse()) {
      const evRunId: string | undefined =
        ev.runId ??
        (ev.detail as JsonObject | undefined)?.run_id ??
        (ev.detail as JsonObject | undefined)?.runId ??
        (ev.detail as JsonObject | undefined)?.data?.run_id;
      if (!evRunId || !acceptedRunIds.has(evRunId)) continue;

      const d = ev.detail as JsonObject | undefined;
      if (ev.type === "flow_event" && (d?.node === "tts_send" || d?.node === "tts_suppressed" || d?.node === "harness_response")) {
        // Prefer full_text: tts_send.text is only the remainder after a streamed first sentence.
        const text: string = d?.data?.full_text ?? d?.full_text ?? d?.data?.text ?? d?.text ?? "";
        if (text) {
          resolvedIds.current.add(pending);
          pendingRunIdRef.current = null;
          setSending(false);
          setThinkingText(null);
          const chips = Array.from(toolChipsRef.current.values());
          const savedChips = chips.length > 0 ? chips : undefined;
          toolChipsRef.current.clear();
          setToolChips([]);
          const usage = tokenUsageRef.current;
          tokenUsageRef.current = undefined;
          const cleaned = stripHWMarkers(text);
          updateMessages((prev) =>
            prev.map((m) =>
              m.runId === pending && m.role === "agent" && m.pending
                ? { ...m, text: cleaned, time: firstReplyTime(pending, m), pending: false, tools: savedChips, tokenUsage: usage }
                : m,
            ),
          );
          return;
        }
      }
      if (ev.type === "flow_event" && d?.node === "no_reply") {
        resolvedIds.current.add(pending);
        pendingRunIdRef.current = null;
        setSending(false);
        setThinkingText(null);
        toolChipsRef.current.clear();
        setToolChips([]);
        tokenUsageRef.current = undefined;
        updateMessages((prev) =>
          prev.map((m) =>
            m.runId === pending && m.role === "agent" && m.pending
              ? { ...m, text: "…", pending: false }
              : m,
          ),
        );
        return;
      }
    }
    // `sending` forces a re-scan once reload recovery attaches the run.
  }, [events, replayedFlowEvents, updateMessages, sending]);

  // Recover an in-flight turn after reload by re-attaching the run refs.
  const recoveryDoneRef = useRef(false);
  useEffect(() => {
    if (!isActive || recoveryDoneRef.current) return;
    recoveryDoneRef.current = true;
    if (pendingRunIdRef.current) return;
    const convo = convos.find((c) => c.id === activeId);
    if (!convo) return;
    // A pending bubble without a run id died with the page; finalize it.
    const strandedIds: string[] = [];
    for (const m of convo.messages) {
      if (m.pending && !m.runId && m.role === "agent") strandedIds.push(m.id);
    }
    if (strandedIds.length > 0) {
      const stranded = new Set(strandedIds);
      setConvos((prev) =>
        prev.map((c) =>
          c.id === convo.id
            ? { ...c, messages: c.messages.map((m) => stranded.has(m.id)
                ? { ...m, text: "⏹ interrupted — the page reloaded before the robot answered", pending: false, error: true }
                : m) }
            : c,
        ),
      );
    }
    let idx = -1;
    for (let i = convo.messages.length - 1; i >= 0; i--) {
      const m = convo.messages[i];
      if (m.pending && m.runId && m.role === "agent") { idx = i; break; }
    }
    if (idx < 0) return;
    const runId = convo.messages[idx].runId!;
    const prevUser = convo.messages.slice(0, idx).reverse().find((m) => m.role === "user");
    pendingRunIdRef.current = runId;
    pendingUserTextRef.current = prevUser?.text ?? null;
    setSending(true);
    armReplyWatchdog(runId, convo.id, RECOVERY_IDLE_TIMEOUT_MS);
    return () => clearReplyWatchdog();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive]);

  useEffect(() => {
    setTimeout(() => {
      const el = scrollContainerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, 50);
  }, [activeId]);

  // Chat stays mounted while hidden, so scroll once it becomes active.
  useEffect(() => {
    if (!isActive) return;
    setTimeout(() => {
      const el = scrollContainerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, 50);
  }, [isActive]);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const lastMsg = messages[messages.length - 1];
    const hasPending = lastMsg?.pending;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
    if (nearBottom || hasPending) scrollToBottom();
  }, [messages, scrollToBottom]);

  const newChat = useCallback(() => {
    if (active && active.messages.length === 0) return;
    const id = `c-${Date.now()}`;
    const convo: Conversation = { id, title: "New chat", createdAt: Date.now(), messages: [] };
    setConvos((prev) => [convo, ...prev]);
    setActiveId(id);
    setSending(false);
    pendingRunIdRef.current = null;
    setTimeout(() => textareaRef.current?.focus(), 50);
  }, [active]);

  const switchTo = (id: string) => {
    if (id === activeId) return;
    setActiveId(id);
    setSending(false);
    pendingRunIdRef.current = null;
  };

  const deleteConvo = (id: string) => {
    if (confirmDeleteId !== id) {
      setConfirmDeleteId(id);
      setTimeout(() => setConfirmDeleteId((prev) => prev === id ? null : prev), 3000);
      return;
    }
    setConvos((prev) => {
      const gone = prev.find((c) => c.id === id);
      if (gone) void deleteChatImages(gone.messages.map((m) => m.id));
      return prev.filter((c) => c.id !== id);
    });
    if (activeId === id) setActiveId(null);
    setConfirmDeleteId(null);
  };

  const togglePin = (id: string) => {
    setConvos((prev) => prev.map((c) => c.id === id ? { ...c, pinned: !c.pinned } : c));
  };

  const startRename = (c: Conversation) => {
    setEditingId(c.id);
    setEditTitle(c.title);
  };

  const commitRename = () => {
    if (!editingId) return;
    const trimmed = editTitle.trim();
    if (trimmed) {
      setConvos((prev) =>
        prev.map((c) => c.id === editingId ? { ...c, title: trimmed, manualTitle: true } : c),
      );
    }
    setEditingId(null);
  };

  const attachFile = useCallback((file: File) => {
    if (file.size > MAX_FILE_SIZE) {
      alert(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max 10 MB.`);
      return;
    }
    const isImage = file.type.startsWith("image/");
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      const base64 = dataUrl.split(",")[1] ?? "";
      if (!base64) return;
      // Cap checked here: files arrive one FileReader at a time.
      setAttachments((prev) => prev.length >= MAX_ATTACHMENTS ? prev : [...prev, {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        name: file.name,
        mime: file.type,
        size: file.size,
        isImage,
        base64,
        previewUrl: isImage ? dataUrl : null,
      }]);
    };
    reader.readAsDataURL(file);
  }, []);

  const attachFiles = useCallback((files: FileList | File[]) => {
    for (const file of Array.from(files)) attachFile(file);
  }, [attachFile]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) attachFiles(e.target.files);
    e.target.value = "";
  };

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const clearFile = () => setAttachments([]);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(true);
  }, []);
  const onDragLeave = useCallback((e: React.DragEvent) => {
    // Only leave when exiting the container (not children)
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragging(false);
  }, []);
  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files?.length) attachFiles(e.dataTransfer.files);
  }, [attachFiles]);

  const onPaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    const images: File[] = [];
    for (let i = 0; i < items.length; i++) {
      if (!items[i].type.startsWith("image/")) continue;
      const file = items[i].getAsFile();
      if (file) images.push(file);
    }
    if (images.length === 0) return;
    e.preventDefault();
    attachFiles(images);
  }, [attachFiles]);

  const exportConversation = () => {
    if (!active || active.messages.length === 0) return;
    const lines = active.messages.map((m) => {
      const role = m.role === "user" ? "You" : "Assistant";
      return `[${m.time}] ${role}: ${m.text}`;
    });
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `chat-${active.id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const copyMessage = (msg: ChatMessage) => {
    copyToClipboard(msg.text).then(() => {
      setCopiedId(msg.id);
      setTimeout(() => setCopiedId((prev) => prev === msg.id ? null : prev), 1500);
    });
  };

  const retryMessage = (errorMsg: ChatMessage) => {
    const idx = messages.findIndex((m) => m.id === errorMsg.id);
    if (idx < 1) return;
    const userMsg = messages[idx - 1];
    if (userMsg.role !== "user") return;

    updateMessages((prev) => prev.filter((m) => m.id !== errorMsg.id));
    setInput(userMsg.text);
    setTimeout(() => {
      updateMessages((prev) => prev.filter((m) => m.id !== userMsg.id));
      sendText(userMsg.text);
    }, 50);
  };

  const sendText = useCallback(async (text: string, attachedImage?: string | null) => {
    if (!text || sending) return;

    let targetId = activeId;
    if (!targetId) {
      const id = `c-${Date.now()}`;
      const convo: Conversation = { id, title: "New chat", createdAt: Date.now(), messages: [] };
      setConvos((prev) => [convo, ...prev]);
      setActiveId(id);
      targetId = id;
    }

    const nowDate = new Date();
    const now = clockTime();
    const dateStr = nowDate.toISOString().slice(0, 10);
    const userMsg: ChatMessage = {
      id: `u-${Date.now()}`, role: "user", text, time: now, ts: nowDate.getTime(), date: dateStr,
      imageUrls: attachments.filter((a) => a.isImage).map((a) => a.previewUrl!).filter(Boolean),
      fileName: attachments.find((a) => !a.isImage)?.name,
      fileSize: attachments.find((a) => !a.isImage)?.size,
      attachmentCount: attachments.filter((a) => !a.isImage).length > 1
        ? attachments.filter((a) => !a.isImage).length
        : undefined,
    };
    void putChatImages(userMsg.id, attachments.filter((a) => a.isImage).map((a) => a.previewUrl!).filter(Boolean));

    setConvos((prev) =>
      prev.map((c) => {
        if (c.id !== targetId) return c;
        const msgs = [...c.messages, userMsg];
        const title = !c.manualTitle ? titleFromMessages(msgs) : c.title;
        return { ...c, messages: msgs, title };
      }),
    );
    setInput("");
    clearFile();
    setSending(true);
    // Show the waiting bubble before the POST, which can take a minute.
    const localReplyId = `l-pending-${Date.now()}`;
    const sendSequence = ++sendSequenceRef.current;
    pendingLocalReplyIdRef.current = localReplyId;
    setConvos((prev) =>
      prev.map((c) =>
        c.id === targetId
          ? { ...c, messages: [...c.messages, { id: localReplyId, role: "agent", text: "", time: now, ts: Date.now(), pending: true }] }
          : c,
      ),
    );
    setTimeout(scrollToBottom, 50);

    // Images ride `image` (vision gate); other files ride `file`.
    const staged = attachments;
    const sendImages = [
      ...(attachedImage ? [attachedImage] : []),
      ...staged.filter((a) => a.isImage).map((a) => a.base64),
    ];
    const sendFiles = staged.filter((a) => !a.isImage).map((a) => ({
      name: a.name || "attachment", mime: a.mime, content: a.base64,
    }));

    try {
      const body: Record<string, unknown> = { type: "web_chat", message: text };
      if (sendImages.length > 0) body.images = sendImages;
      if (sendFiles.length > 0) body.files = sendFiles;
      const abort = new AbortController();
      sendAbortRef.current = abort;
      const res = await fetch(`${API}/sensing/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: abort.signal,
      });
      const json = await res.json();

      // Stop was pressed while this request was being accepted; drop the late reply.
      if (sendSequenceRef.current !== sendSequence) {
        if (json.status === 1 && json.data?.runId) resolvedIds.current.add(json.data.runId);
        return;
      }
      sendAbortRef.current = null;

      if (json.status === 1 && json.data?.runId) {
        const runId: string = json.data.runId;
        pendingRunIdRef.current = runId;
        pendingLocalReplyIdRef.current = null;
        pendingUserTextRef.current = text;
        const replyTime = clockTime();
        // Adopt the spinning bubble; `l-<runId>` is what reload recovery looks for.
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: c.messages.map((m) => m.id === localReplyId
                  ? { ...m, id: `l-${runId}`, runId, time: replyTime, ts: Date.now() }
                  : m) }
              : c,
          ),
        );
        armReplyWatchdog(runId, targetId, REPLY_IDLE_TIMEOUT_MS);
      } else if (json.data?.handler === "local") {
        setSending(false);
        const localText = json.data?.response || "✓ handled locally";
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: c.messages.map((m) => m.id === localReplyId
                  ? { ...m, text: localText, pending: false }
                  : m) }
              : c,
          ),
        );
      } else if (json.data?.handler === "dropped" || json.data?.handler === "queued") {
        setSending(false);
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: c.messages.map((m) => m.id === localReplyId
                  ? { ...m, text: "⏸ busy — try again", pending: false, error: true }
                  : m) }
              : c,
          ),
        );
      } else {
        setSending(false);
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: c.messages.map((m) => m.id === localReplyId
                  ? { ...m, text: json.message ?? "error", pending: false, error: true }
                  : m) }
              : c,
          ),
        );
      }
    } catch {
      if (sendSequenceRef.current !== sendSequence) return;
      sendAbortRef.current = null;
      setSending(false);
      setConvos((prev) =>
        prev.map((c) =>
          c.id === targetId
            ? { ...c, messages: c.messages.map((m) => m.id === localReplyId
                ? { ...m, text: "connection error", pending: false, error: true }
                : m) }
            : c,
        ),
      );
    }
  }, [activeId, sending, attachments, scrollToBottom, armReplyWatchdog]);

  // sendText splits the staged attachments by kind itself.
  const send = () => { sendText(input.trim()); };

  // Stop is intentionally scoped to this browser's pending reply.
  const stopPendingReply = () => {
    const runId = pendingRunIdRef.current;
    const localReplyId = pendingLocalReplyIdRef.current;
    ++sendSequenceRef.current;
    sendAbortRef.current?.abort();
    sendAbortRef.current = null;
    clearReplyWatchdog();
    if (runId) {
      resolvedIds.current.add(runId);
      deltaBufRef.current.delete(runId);
      thinkingBufRef.current.delete(runId);
    }
    pendingRunIdRef.current = null;
    pendingUserTextRef.current = null;
    pendingLocalReplyIdRef.current = null;
    toolChipsRef.current.clear();
    setToolChips([]);
    setThinkingText(null);
    setSending(false);
    setConvos((prev) => prev.map((c) => ({
      ...c,
      messages: c.messages.map((m) =>
        m.pending && (m.runId === runId || m.id === localReplyId)
          ? { ...m, text: "Stopped", pending: false }
          : m,
      ),
    })));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const filtered = search.trim()
    ? convos.filter((c) => {
        const q = search.toLowerCase();
        if (c.title.toLowerCase().includes(q)) return true;
        return c.messages.some((m) => m.text.toLowerCase().includes(q));
      })
    : convos;
  const grouped = groupConvosByDate(filtered);
  const nowTs = Date.now();

  return (
    <div style={{ display: "flex", height: "100%", gap: 0, position: "relative" }}>
      {sidebarOpen && (
      <div style={{
        width: 280, flexShrink: 0, order: 2,
        borderLeft: "1px solid var(--lm-border)",
        display: "flex", flexDirection: "column",
        background: "var(--lm-sidebar)",
      }}>
        <div style={{
          padding: "14px 14px 10px",
          borderBottom: "1px solid var(--lm-border)",
          display: "flex", flexDirection: "column", gap: 10,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <History size={14} style={{ color: "var(--lm-text-muted)" }} />
            <span style={{
              fontSize: 10, fontWeight: 700, color: "var(--lm-text-muted)",
              textTransform: "uppercase", letterSpacing: "0.08em",
            }}>History</span>
            <span style={{ flex: 1 }} />
            <button
              onClick={() => setSidebarOpen(false)}
              style={{
                width: 24, height: 24, padding: 0, borderRadius: 5,
                background: "transparent", border: "none",
                color: "var(--lm-text-muted)",
                cursor: "pointer", flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
              title="Hide history"
              aria-label="Hide history"
            ><ChevronRight size={14} /></button>
          </div>
          <button
            onClick={newChat}
            title="New chat (Ctrl+N)"
            style={{
              width: "100%", padding: "9px 12px", borderRadius: 8,
              background: "color-mix(in srgb, var(--lm-amber) 14%, transparent)",
              border: "1px solid color-mix(in srgb, var(--lm-amber) 30%, transparent)",
              color: "var(--lm-amber)", fontSize: 12, fontWeight: 600,
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
            }}
          >
            <Plus size={14} /> New chat
          </button>
          <div style={{ position: "relative" }}>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Escape") setSearch(""); }}
              placeholder="Search chats…"
              aria-label="Search chat history"
              style={{
                width: "100%", padding: search ? "7px 30px 7px 10px" : "7px 10px", borderRadius: 6,
                background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                color: "var(--lm-text)", fontSize: 11.5, outline: "none",
                boxSizing: "border-box",
              }}
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch("")}
                title="Clear search"
                aria-label="Clear chat history search"
                style={{
                  position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
                  width: 21, height: 21, padding: 0, border: "none", borderRadius: 4,
                  background: "transparent", color: "var(--lm-text-muted)", cursor: "pointer",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}
              ><X size={12} /></button>
            )}
          </div>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "6px 10px 12px" }}>
          {filtered.length === 0 && (
            <div style={{ padding: 16, textAlign: "center", color: "var(--lm-text-muted)", fontSize: 11 }}>
              {search ? "No matches" : "No conversations yet"}
            </div>
          )}
          {search && filtered.length > 0 && (
            <div style={{ padding: "7px 6px 3px", color: "var(--lm-text-muted)", fontSize: 10.5 }}>
              {filtered.length} {filtered.length === 1 ? "conversation" : "conversations"}
            </div>
          )}
          {grouped.map(({ label, items }) => (
            <div key={label} style={{ marginBottom: 8 }}>
              <div style={{
                display: "flex", alignItems: "center", gap: 7,
                padding: "10px 6px 6px",
              }}>
                <span style={{
                  fontSize: 9.5, fontWeight: 700, color: "var(--lm-text-muted)",
                  textTransform: "uppercase", letterSpacing: "0.06em",
                }}>
                  {label === "Pinned" ? (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <Pin size={9} fill="currentColor" /> {label}
                    </span>
                  ) : label}
                </span>
                <span style={{ flex: 1, height: 1, background: "var(--lm-border)", opacity: 0.6 }} />
                <span style={{
                  fontSize: 9, fontWeight: 600, color: "var(--lm-text-muted)",
                  opacity: 0.7,
                }}>{items.length}</span>
              </div>
              {items.map((c) => {
                const isActive = c.id === activeId;
                const isHovered = hoveredConvoId === c.id;
                return (
                <div
                  key={c.id}
                  onClick={() => switchTo(c.id)}
                  onMouseEnter={() => setHoveredConvoId(c.id)}
                  onMouseLeave={() => setHoveredConvoId((cur) => (cur === c.id ? null : cur))}
                  className="lm-convo-row"
                  style={{
                    position: "relative",
                    padding: "8px 10px 8px 12px", borderRadius: 8, cursor: "pointer",
                    background: isActive
                      ? "color-mix(in srgb, var(--lm-amber) 14%, transparent)"
                      : isHovered ? "color-mix(in srgb, var(--lm-text) 4%, transparent)" : "transparent",
                    marginBottom: 3,
                    transition: "background 0.15s",
                  }}
                >
                  {isActive && (
                    <span style={{
                      position: "absolute", left: 0, top: 7, bottom: 7, width: 3,
                      borderRadius: 999, background: "var(--lm-amber)",
                      boxShadow: "0 0 8px -1px var(--lm-amber-glow)",
                    }} />
                  )}
                  {c.pinned && !isHovered && (
                    <span style={{
                      position: "absolute", top: 6, right: 8,
                      color: "var(--lm-amber)", display: "flex", alignItems: "center",
                      pointerEvents: "none",
                    }}><Pin size={10} fill="currentColor" /></span>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      flexShrink: 0, width: 7, height: 7, borderRadius: "50%",
                      background: convoColor(c.id),
                      boxShadow: isActive ? `0 0 6px -1px ${convoColor(c.id)}` : "none",
                      opacity: isActive || isHovered ? 1 : 0.65,
                      transition: "opacity 0.15s",
                    }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {editingId === c.id ? (
                        <input
                          autoFocus
                          value={editTitle}
                          onChange={(e) => setEditTitle(e.target.value)}
                          onBlur={commitRename}
                          onKeyDown={(e) => { if (e.key === "Enter") commitRename(); if (e.key === "Escape") setEditingId(null); }}
                          onClick={(e) => e.stopPropagation()}
                          style={{
                            fontSize: 12, width: "100%", background: "var(--lm-surface)",
                            border: "1px solid var(--lm-amber)", borderRadius: 4,
                            color: "var(--lm-text)", padding: "1px 4px", outline: "none",
                          }}
                        />
                      ) : (
                        <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
                          <div
                            onDoubleClick={(e) => { e.stopPropagation(); startRename(c); }}
                            title="Double-click to rename"
                            style={{
                              flex: 1, minWidth: 0,
                              fontSize: 12.5,
                              color: isActive ? "var(--lm-amber)" : "var(--lm-text)",
                              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                              fontWeight: isActive ? 600 : 500,
                              paddingRight: c.pinned ? 14 : 0,
                            }}
                          >
                            {c.title}
                          </div>
                          {!isHovered && (
                            <span style={{
                              flexShrink: 0, fontSize: 9.5, fontWeight: 500,
                              color: "var(--lm-text-muted)",
                              paddingRight: c.pinned ? 12 : 0,
                            }}>{relativeTime(conversationActivityAt(c), nowTs, t)}</span>
                          )}
                        </div>
                      )}
                      {c.messages.length > 0 && (
                        <div style={{
                          fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 3,
                          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                        }}>
                          {(() => {
                            const last = c.messages[c.messages.length - 1];
                            const txt = last.text || "…";
                            return (last.role === "agent" ? "↪ " : "") + (txt.length > 40 ? txt.slice(0, 40) + "…" : txt);
                          })()}
                        </div>
                      )}
                    </div>
                    <div style={{
                      display: "flex", gap: 2, flexShrink: 0,
                      opacity: isHovered ? 1 : 0,
                      pointerEvents: isHovered ? "auto" : "none",
                      transition: "opacity 0.15s",
                    }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); togglePin(c.id); }}
                        style={hoverIconBtnStyle(c.pinned ? "var(--lm-amber)" : "var(--lm-text-muted)")}
                        title={c.pinned ? "Unpin" : "Pin to top"}
                        aria-label={c.pinned ? "Unpin" : "Pin"}
                      >{c.pinned ? <Pin size={12} fill="currentColor" /> : <Pin size={12} />}</button>
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteConvo(c.id); }}
                        style={{
                          ...hoverIconBtnStyle(confirmDeleteId === c.id ? "var(--lm-red)" : "var(--lm-text-muted)"),
                          background: confirmDeleteId === c.id ? "color-mix(in srgb, var(--lm-red) 20%, transparent)" : "transparent",
                        }}
                        title={confirmDeleteId === c.id ? "Click again to confirm" : "Delete"}
                        aria-label="Delete conversation"
                      >{confirmDeleteId === c.id ? <Check size={12} /> : <Trash2 size={12} />}</button>
                    </div>
                  </div>
                </div>
                );
              })}
            </div>
          ))}
        </div>
        {convos.length > 1 && (
          <div style={{ padding: "8px 12px 12px", borderTop: "1px solid var(--lm-border)" }}>
            <button
              onClick={() => {
                if (confirm(`Delete all ${convos.filter((c) => !c.pinned).length} unpinned conversations?`)) {
                  setConvos((prev) => {
                    const goneIds = prev
                      .filter((c) => !c.pinned)
                      .flatMap((c) => c.messages.map((m) => m.id));
                    void deleteChatImages(goneIds);
                    return prev.filter((c) => c.pinned);
                  });
                  setActiveId(null);
                }
              }}
              style={{
                width: "100%", padding: "7px 0", borderRadius: 7,
                background: "transparent", border: "1px solid transparent",
                color: "var(--lm-text-muted)", fontSize: 10.5, fontWeight: 500, cursor: "pointer",
                display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 5,
                transition: "color 0.15s, background 0.15s, border-color 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "var(--lm-red)"; e.currentTarget.style.background = "color-mix(in srgb, var(--lm-red) 10%, transparent)"; e.currentTarget.style.borderColor = "color-mix(in srgb, var(--lm-red) 25%, transparent)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "var(--lm-text-muted)"; e.currentTarget.style.background = "transparent"; e.currentTarget.style.borderColor = "transparent"; }}
            ><Trash2 size={11} /> Clear all unpinned</button>
          </div>
        )}
      </div>
      )}

      <div
        className="lm-chat-panel"
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, position: "relative" }}
      >
        {dragging && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 10,
            background: "rgba(245,158,11,0.08)",
            border: "2px dashed var(--lm-amber)",
            borderRadius: 8,
            display: "flex", alignItems: "center", justifyContent: "center",
            pointerEvents: "none",
          }}>
            <span style={{ fontSize: 14, color: "var(--lm-amber)", fontWeight: 600 }}>
              Drop file here
            </span>
          </div>
        )}
        <div style={{
          padding: "10px 16px", borderBottom: "1px solid var(--lm-border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
          background: "var(--lm-sidebar)", minHeight: 44, gap: 12,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, flex: 1 }}>
            <span className={sending ? "lm-orb-ring" : undefined} style={{ position: "relative", flexShrink: 0, display: "inline-flex" }}>
              <span
                className={`lm-assistant-orb${sending ? " lm-orb-live" : ""}`}
                style={{ width: 26, height: 26 }}
              >
                <Sparkles size={13} />
              </span>
              <span
                className={`lm-status-dot${sending ? " lm-thinking" : ""}`}
                style={{ position: "absolute", right: -1, bottom: -1 }}
              />
            </span>
            <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
              <span style={{
                fontSize: 14,
                color: active ? "var(--lm-text)" : "var(--lm-text-muted)",
                fontWeight: 700,
                letterSpacing: "-0.01em",
                lineHeight: 1.2,
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {active ? active.title : "Select or start a chat"}
              </span>
              <span style={{
                fontSize: 10, fontWeight: 600, lineHeight: 1.2,
                color: sending ? "var(--lm-amber)" : "var(--lm-text-dim)",
                whiteSpace: "nowrap",
              }}>
                {sending ? t("chat.status.thinking") : t("chat.status.online")}
              </span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            {active && active.messages.length > 0 && (
              <button
                onClick={exportConversation}
                style={headerPillBtnStyle}
                title="Export as text"
                aria-label="Export conversation"
              ><Download size={12} /> Export</button>
            )}
            {convos.length > 0 && (
              <button
                onClick={() => {
                  if (!window.confirm("Clear all local chat history? This wipes the browser cache only — server-side flow logs are untouched.")) return;
                  clearLocalChatHistory();
                  setConvos([]);
                  setActiveId(null);
                }}
                style={headerPillBtnStyle}
                title={`Clear local chat history (auto-purges after ${Math.round(HISTORY_TTL_MS / (24 * 60 * 60 * 1000))}d)`}
                aria-label="Clear local chat history"
              ><Trash2 size={12} /> Clear</button>
            )}
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                style={headerPillBtnStyle}
                title="Show history"
                aria-label="Show history"
              ><History size={13} /> History</button>
            )}
          </div>
        </div>
        <div
          ref={scrollContainerRef}
          onScroll={onScroll}
          style={{
            flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 16px 8px",
            display: "flex", flexDirection: "column",
          }}
        >
          {/* marginTop:auto anchors a short thread to the bottom. */}
          <div style={{ maxWidth: 760, margin: messages.length === 0 ? "auto" : "auto auto 0", width: "100%", display: "flex", flexDirection: "column", gap: 12 }}>
          {messages.length === 0 && (
            <div style={{ margin: "auto", textAlign: "center", color: "var(--lm-text-dim)", maxWidth: 460 }}>
              <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}>
                <span className="lm-assistant-orb lm-orb-live" style={{ width: 56, height: 56 }}>
                  <Sparkles size={26} />
                </span>
              </div>
              <div style={{ fontSize: 16, fontWeight: 700, color: "var(--lm-text)", letterSpacing: "-0.01em" }}>
                {t("chat.empty.title")}
              </div>
              <div style={{ fontSize: 12.5, marginTop: 5, lineHeight: 1.6 }}>
                {t("chat.empty.subtitle")}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center", marginTop: 16 }}>
                {CHAT_SUGGESTIONS.map((s) => {
                  const label = t(s.key);
                  return (
                  <button
                    key={s.key}
                    onClick={() => { setInput(label); setTimeout(() => textareaRef.current?.focus(), 0); }}
                    className="lm-suggestion-chip"
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 7,
                      padding: "8px 13px", borderRadius: 999,
                      background: "var(--lm-card)", border: "1px solid var(--lm-border)",
                      color: "var(--lm-text)", fontSize: 12.5, cursor: "pointer",
                      fontFamily: "inherit",
                    }}
                  >
                    <span style={{ color: "var(--lm-amber)", display: "inline-flex" }}>{s.icon}</span>
                    {label}
                  </button>
                  );
                })}
              </div>
            </div>
          )}
          {messages.map((msg, i) => {
            const prevDate = i > 0 ? messages[i - 1].date : null;
            const showDate = msg.date && msg.date !== prevDate;
            return (
            <div key={msg.id}>
              {showDate && (
                <div style={{
                  textAlign: "center", fontSize: 10, color: "var(--lm-text-muted)",
                  padding: "8px 0 4px", fontWeight: 500,
                }}>
                  {formatDateLabel(msg.date!)}
                </div>
              )}
              <div
                className="lm-chat-msg"
                data-role={msg.role}
                style={{ display: "flex", flexDirection: msg.role === "user" ? "row-reverse" : "row", alignItems: "flex-end" }}
              >
              {msg.role === "agent" && (() => {
                const isFirstOfTurn = i === 0 || messages[i - 1]?.role === "user";
                return (
                  <div style={{ width: 26, flexShrink: 0, marginRight: 8, display: "flex", justifyContent: "center" }}>
                    {isFirstOfTurn && (
                      <span
                        className={`lm-assistant-orb${msg.pending ? " lm-orb-live" : ""}`}
                        style={{ width: 26, height: 26 }}
                      ><Sparkles size={13} /></span>
                    )}
                  </div>
                );
              })()}
              <div style={{ maxWidth: msg.role === "user" ? "72%" : "85%", display: "flex", flexDirection: "column", alignItems: msg.role === "user" ? "flex-end" : "flex-start", gap: 3 }}>
                {msg.role === "agent" && (i === 0 || messages[i - 1]?.role === "user") && (
                  <span style={{ fontSize: 11, color: "var(--lm-amber)", fontWeight: 600, letterSpacing: "0.01em", paddingLeft: 4 }}>Assistant</span>
                )}
                {msg.pending && msg.role === "agent" && msg.runId === pendingRunIdRef.current && thinkingText && (
                  <ThinkingBlock text={thinkingText} />
                )}
                {msg.role === "agent" && (() => {
                  const isActivePending = !!msg.pending && msg.runId === pendingRunIdRef.current;
                  const chips = isActivePending ? toolChips : msg.tools;
                  if (!chips || chips.length === 0) return null;
                  return <ToolChipGroup chips={chips} live={isActivePending} />;
                })()}
                <div data-bubble style={{
                  padding: "9px 13px",
                  borderRadius: msg.role === "user" ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
                  background: msg.role === "user"
                    ? "linear-gradient(135deg, rgba(245,158,11,0.20), rgba(245,158,11,0.12))"
                    : "var(--lm-surface)",
                  border: `1px solid ${msg.role === "user" ? "rgba(245,158,11,0.28)" : "var(--lm-border)"}`,
                  color: msg.error ? "var(--lm-red)" : "var(--lm-text)",
                  fontSize: 13.5, lineHeight: 1.55, wordBreak: "break-word",
                  minWidth: 40, minHeight: 36, position: "relative",
                }}>
                  {msg.imageUrls && msg.imageUrls.length > 0 && (
                    <div style={{
                      display: "flex", flexWrap: "wrap", gap: 4,
                      marginBottom: msg.text ? 6 : 0,
                    }}>
                      {msg.imageUrls.map((url, i) => (
                        <img
                          key={i}
                          src={url}
                          alt={`attached ${i + 1} of ${msg.imageUrls!.length}`}
                          onClick={() => setLightboxUrl(url)}
                          title="Click to view full size"
                          style={{
                            maxWidth: msg.imageUrls!.length > 1 ? 120 : 200,
                            maxHeight: msg.imageUrls!.length > 1 ? 120 : 150,
                            borderRadius: 6,
                            cursor: "zoom-in",
                          }}
                        />
                      ))}
                    </div>
                  )}
                  {msg.fileName && (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 6,
                      padding: "4px 8px", borderRadius: 6,
                      background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                      marginBottom: msg.text ? 6 : 0, fontSize: 11.5,
                    }}>
                      <span>📎</span>
                      <span style={{ color: "var(--lm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 160 }}>
                        {msg.fileName}
                      </span>
                      {msg.fileSize != null && (
                        <span style={{ color: "var(--lm-text-muted)", fontSize: 10, flexShrink: 0 }}>
                          {msg.fileSize < 1024 ? `${msg.fileSize} B`
                            : msg.fileSize < 1024 * 1024 ? `${(msg.fileSize / 1024).toFixed(0)} KB`
                            : `${(msg.fileSize / 1024 / 1024).toFixed(1)} MB`}
                        </span>
                      )}
                      {msg.attachmentCount != null && msg.attachmentCount > 1 && (
                        <span style={{ color: "var(--lm-text-muted)", fontSize: 10, flexShrink: 0 }}>
                          +{msg.attachmentCount - 1} more
                        </span>
                      )}
                    </div>
                  )}
                  {msg.pending && !msg.text ? (
                    <span style={{ color: "var(--lm-text-muted)" }}>
                      <span className="lm-blink">●</span>
                      <span style={{ marginLeft: 4 }}>●</span>
                      <span style={{ marginLeft: 4 }}>●</span>
                    </span>
                  ) : msg.pending && msg.text ? (
                    <>
                      {msg.role === "agent" ? renderMarkdown(msg.text) : linkifyPlain(msg.text, msg.id)}
                      <span className="lm-cursor" style={{
                        display: "inline-block", width: 2, height: "1em",
                        background: "var(--lm-amber)", marginLeft: 2,
                        verticalAlign: "text-bottom", borderRadius: 1,
                      }} />
                    </>
                  ) : msg.role === "agent" ? renderMarkdown(msg.text) : linkifyPlain(msg.text, msg.id)}
                  {msg.role === "agent" && !msg.pending && <AgentFiles text={msg.text} tools={msg.tools} />}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6, paddingInline: 4 }}>
                  <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>{msg.time}</span>
                  {!msg.pending && msg.text && msg.text !== "…" && (
                    <button
                      onClick={() => copyMessage(msg)}
                      className={copiedId === msg.id ? undefined : "lm-msg-action"}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        color: copiedId === msg.id ? "var(--lm-green)" : "var(--lm-text-muted)",
                        padding: 0, transition: "opacity 0.15s, color 0.15s",
                        display: "inline-flex", alignItems: "center",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.color = "var(--lm-text)"; }}
                      onMouseLeave={(e) => { if (copiedId !== msg.id) e.currentTarget.style.color = "var(--lm-text-muted)"; }}
                      title="Copy"
                      aria-label="Copy message"
                    >{copiedId === msg.id ? <Check size={12} /> : <Copy size={12} />}</button>
                  )}
                  {msg.error && msg.role === "agent" && (
                    <button
                      onClick={() => retryMessage(msg)}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        fontSize: 10, color: "var(--lm-amber)", padding: 0,
                        opacity: 0.7, transition: "opacity 0.15s",
                        display: "inline-flex", alignItems: "center", gap: 3,
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.7"; }}
                      title="Retry"
                    ><RotateCcw size={11} /> retry</button>
                  )}
                  {msg.tokenUsage && msg.role === "agent" && <UsageBadge usage={msg.tokenUsage} model={modelLabel} />}
                </div>
              </div>
            </div>
            </div>
            );
          })}
          <div ref={bottomRef} />
          </div>
        </div>

        {showScrollBtn && (
          <button
            onClick={scrollToBottom}
            style={{
              position: "absolute", bottom: 80, right: 20,
              width: 32, height: 32, borderRadius: "50%",
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: "var(--lm-text-muted)",
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              boxShadow: "0 2px 8px rgba(0,0,0,0.3)", transition: "opacity 0.2s",
            }}
            title="Scroll to bottom"
            aria-label="Scroll to bottom"
          ><ArrowDown size={16} /></button>
        )}

        <div  id="CHAT_BOX" style={{
          flexShrink: 0,
          padding: "10px 16px 12px",
          borderTop: "1px solid var(--lm-border)",
          background: "var(--lm-sidebar)",
        }}>
          <div style={{ maxWidth: 760, margin: "0 auto", width: "100%" }}>
            <input ref={fileInputRef} type="file" multiple style={{ display: "none" }} onChange={handleFileSelect} />
            <div
              className="lm-chat-composer"
              style={{
                display: "flex", flexDirection: "column", gap: 6,
                background: "var(--lm-card)",
                border: "1px solid var(--lm-border)",
                borderRadius: 16,
                padding: 6,
                boxShadow: "0 1px 2px rgba(0,0,0,0.18), 0 8px 24px -16px rgba(0,0,0,0.5)",
                transition: "border-color 0.15s, box-shadow 0.15s",
              }}>
              {attachments.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, margin: "0 2px 4px" }}>
                  {attachments.map((att) => (
                    <div key={att.id} style={{
                      display: "flex", alignItems: "center", gap: 8,
                      padding: "6px 8px",
                      borderRadius: 12,
                      maxWidth: 220,
                      background: "color-mix(in srgb, var(--lm-amber) 8%, transparent)",
                      border: "1px solid color-mix(in srgb, var(--lm-amber) 25%, transparent)",
                    }}>
                      {att.previewUrl ? (
                        <img
                          src={att.previewUrl}
                          alt={att.name}
                          onClick={() => setLightboxUrl(att.previewUrl)}
                          title="Click to view full size"
                          style={{ height: 36, width: 36, objectFit: "cover", borderRadius: 6, flexShrink: 0, cursor: "zoom-in" }}
                        />
                      ) : (
                        <div style={{
                          width: 36, height: 36, borderRadius: 6,
                          background: "color-mix(in srgb, var(--lm-amber) 15%, transparent)",
                          display: "flex", alignItems: "center", justifyContent: "center",
                          color: "var(--lm-amber)", flexShrink: 0,
                        }}><Paperclip size={16} /></div>
                      )}
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 11.5, color: "var(--lm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{att.name}</div>
                        <div style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                          {att.size < 1024 ? `${att.size} B` : att.size < 1024 * 1024 ? `${(att.size / 1024).toFixed(0)} KB` : `${(att.size / 1024 / 1024).toFixed(1)} MB`}
                        </div>
                      </div>
                      <button
                        onClick={() => removeAttachment(att.id)}
                        style={{
                          background: "transparent", border: "none", cursor: "pointer",
                          color: "var(--lm-text-muted)", padding: 4, borderRadius: 4,
                          display: "flex", alignItems: "center",
                        }}
                        title={`Remove ${att.name}`}
                        aria-label={`Remove ${att.name}`}
                      ><X size={14} /></button>
                    </div>
                  ))}
                </div>
              )}

              <div style={{
                display: "flex", alignItems: "flex-end", gap: 6,
              }}>
              <PlusMenu
                disabled={sending}
                onAttachFile={() => fileInputRef.current?.click()}
                onSkillsAction={setSkillsView}
                onCreateWithAgent={() => {
                  setInput(CREATE_SKILL_WITH_AGENT_PROMPT);
                  requestAnimationFrame(() => textareaRef.current?.focus());
                }}
                onScheduledAction={(action) => {
                  navigate(action === "new" ? "/setting?new=1#scheduled" : "/setting#scheduled");
                }}
              />
              <textarea
                id="CHAT_TEXTAREA"
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                onPaste={onPaste}
                disabled={sending}
                placeholder="Message Assistant…"
                rows={1}
                style={{
                  flex: 1, minWidth: 0,
                  background: "transparent", border: "none",
                  padding: "8px 4px",
                  color: "var(--lm-text)", fontSize: 14,
                  outline: "none", opacity: sending ? 0.6 : 1,
                  resize: "none", lineHeight: 1.5, fontFamily: "inherit",
                  minHeight: 35, maxHeight: 200, overflow: "auto",
                  boxSizing: "border-box",
                }}
              />
              <button
                onClick={sending ? stopPendingReply : send}
                disabled={!sending && !input.trim()}
                className={input.trim() && !sending ? "lm-send-ready" : undefined}
                style={{
                  width: 34, height: 34, borderRadius: "50%", flexShrink: 0,
                  background: sending || input.trim()
                    ? "var(--lm-amber)"
                    : "color-mix(in srgb, var(--lm-text) 15%, transparent)",
                  border: "none",
                  color: sending || input.trim() ? "var(--lm-on-amber)" : "var(--lm-text-muted)",
                  cursor: sending || input.trim() ? "pointer" : "default",
                  boxShadow: sending || input.trim() ? "0 2px 10px -2px var(--lm-amber-glow)" : "none",
                  transition: "background 0.15s, color 0.15s, box-shadow 0.15s, transform 0.12s",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}
                title={sending ? "Stop receiving this reply" : "Send (Enter)"}
                aria-label={sending ? "Stop receiving reply" : "Send message"}
              >{sending ? <Square size={14} fill="currentColor" /> : <ArrowUp size={17} strokeWidth={2.6} />}</button>
              </div>
            </div>
            <div style={{
              fontSize: 10, color: "var(--lm-text-muted)",
              textAlign: "center", marginTop: 6, opacity: 0.7,
            }}>
              Press Enter to send · Shift+Enter for new line
            </div>
          </div>
        </div>
      </div>

      {lightboxUrl && (
        <div
          onClick={() => setLightboxUrl(null)}
          style={{
            position: "fixed", inset: 0, zIndex: 200,
            background: "rgba(0,0,0,0.85)", backdropFilter: "blur(4px)",
            display: "flex", alignItems: "center", justifyContent: "center",
            cursor: "zoom-out",
          }}
        >
          <img
            src={lightboxUrl}
            alt="attachment full size"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: "92vw", maxHeight: "92vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
          />
        </div>
      )}
      {skillsView === "write" && <WriteSkillModal onClose={() => setSkillsView(null)} />}
      {skillsView === "upload" && <UploadSkillModal onClose={() => setSkillsView(null)} />}
      {skillsView === "browse" && <BrowseSkillsModal onClose={() => setSkillsView(null)} />}
      {skillsView === "manage" && <ManageSkillsModal
        onClose={() => setSkillsView(null)}
        onCreateWithAgent={() => {
          setSkillsView(null);
          setInput(CREATE_SKILL_WITH_AGENT_PROMPT);
          requestAnimationFrame(() => textareaRef.current?.focus());
        }}
      />}
    </div>
  );
}

// Assumed context window for the "% ctx" indicator.
const CONTEXT_WINDOW = 200_000;

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  const k = n / 1000;
  return k >= 100 ? `${k.toFixed(0)}k` : `${k.toFixed(1)}k`;
}

// Compact one-line usage strip under each device message
function UsageBadge({ usage, model }: { usage: NonNullable<ChatMessage["tokenUsage"]>; model?: string }) {
  const ctxPct = usage.total > 0 ? Math.min(100, (usage.total / CONTEXT_WINDOW) * 100) : 0;
  return (
    <span
      style={{
        fontSize: 9.5, color: "var(--lm-text-muted)",
        fontFamily: "monospace", opacity: 0.75,
        display: "inline-flex", gap: 10, alignItems: "center",
        whiteSpace: "nowrap",
      }}
      title={
        `Input: ${usage.input.toLocaleString()}\n` +
        `Output: ${usage.output.toLocaleString()}\n` +
        (usage.cacheRead ? `Cache read: ${usage.cacheRead.toLocaleString()}\n` : "") +
        (usage.cacheWrite ? `Cache write: ${usage.cacheWrite.toLocaleString()}\n` : "") +
        `Total: ${usage.total.toLocaleString()}\n` +
        `Context: ${ctxPct.toFixed(1)}% of ${CONTEXT_WINDOW.toLocaleString()}`
      }
    >
      <span>↑{formatTokens(usage.input)}</span>
      <span>↓{formatTokens(usage.output)}</span>
      {usage.cacheRead != null && usage.cacheRead > 0 && (
        <span>R{formatTokens(usage.cacheRead)}</span>
      )}
      <span style={{ color: ctxPct > 80 ? "var(--lm-red)" : ctxPct > 60 ? "var(--lm-amber)" : "var(--lm-text-muted)" }}>
        {ctxPct.toFixed(0)}% ctx
      </span>
      {model && (
        <span style={{ color: "var(--lm-text-dim)" }}>{model}</span>
      )}
    </span>
  );
}

// Collapses a run's tool calls into one summary row; a single tool renders as its own chip.
function ToolChipGroup({ chips, live }: { chips: ToolChip[]; live: boolean }) {
  const [open, setOpen] = useState(false);

  if (chips.length === 1) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
        <ToolChipView chip={chips[0]} />
      </div>
    );
  }

  const accent = "var(--lm-teal)";
  const previewIcons: ToolChip[] = [];
  const seen = new Set<string>();
  for (const c of chips) {
    if (!seen.has(c.iconKind)) { seen.add(c.iconKind); previewIcons.push(c); }
    if (previewIcons.length >= 4) break;
  }
  const allDone = chips.every((c) => c.result);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 4, alignItems: "flex-start", maxWidth: "100%" }}>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          padding: "4px 10px", borderRadius: 999,
          background: `color-mix(in srgb, ${accent} 10%, transparent)`,
          border: `1px solid color-mix(in srgb, ${accent} 22%, transparent)`,
          color: accent, cursor: "pointer", fontSize: 11.5,
        }}
        title={open ? "Hide steps" : "Show steps"}
      >
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          {previewIcons.map((c, idx) => (
            <span key={c.id} style={{
              display: "inline-flex", marginLeft: idx === 0 ? 0 : -5,
              width: 17, height: 17, borderRadius: "50%",
              alignItems: "center", justifyContent: "center",
              background: "var(--lm-card)",
              border: `1px solid color-mix(in srgb, ${accent} 30%, transparent)`,
              zIndex: previewIcons.length - idx,
            }}>{renderToolIcon(c.iconKind, 10)}</span>
          ))}
        </span>
        <strong style={{ fontWeight: 600 }}>
          {chips.length} {chips.length === 1 ? "step" : "steps"}
        </strong>
        {live && !allDone ? (
          <span style={{ fontSize: 10, opacity: 0.85 }} className="lm-blink">●</span>
        ) : allDone ? (
          <span style={{
            fontSize: 9, padding: "0 5px", borderRadius: 3,
            background: "color-mix(in srgb, var(--lm-green) 18%, transparent)",
            color: "var(--lm-green)", fontWeight: 700, letterSpacing: "0.04em",
          }}>DONE</span>
        ) : null}
        <span style={{ marginLeft: 1, opacity: 0.7, display: "inline-flex" }}>
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </span>
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start", paddingLeft: 6 }}>
          {chips.map((c) => <ToolChipView key={c.id} chip={c} />)}
        </div>
      )}
    </div>
  );
}

// Tool chip: collapsed headline, expandable to the full args.
function ToolChipView({ chip }: { chip: ToolChip }) {
  const [open, setOpen] = useState(false);
  const hasDetail = chip.args || chip.detail || chip.result;
  const accent = "var(--lm-teal)";
  return (
    <div style={{
      display: "inline-flex", flexDirection: "column",
      maxWidth: "100%", minWidth: 0,
      borderRadius: 10,
      background: `color-mix(in srgb, ${accent} 10%, transparent)`,
      border: `1px solid color-mix(in srgb, ${accent} 22%, transparent)`,
      color: accent,
    }}>
      <button
        onClick={() => hasDetail && setOpen((v) => !v)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "3px 9px",
          background: "transparent", border: "none",
          color: "inherit",
          cursor: hasDetail ? "pointer" : "default",
          fontSize: 10.5,
          textAlign: "left",
          minWidth: 0,
        }}
        title={hasDetail ? (open ? "Hide details" : "Show details") : undefined}
      >
        {renderToolIcon(chip.iconKind, 12)}
        <strong style={{ fontWeight: 700 }}>{chip.label}</strong>
        {chip.detail && (
          <span style={{
            opacity: 0.85, fontFamily: "monospace", fontSize: 10,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            maxWidth: 320,
          }}>{chip.detail}</span>
        )}
        {chip.result && (
          <span style={{
            fontSize: 9, padding: "0 5px", borderRadius: 3,
            background: "color-mix(in srgb, var(--lm-green) 18%, transparent)",
            color: "var(--lm-green)", fontWeight: 700, letterSpacing: "0.04em",
          }}>OK</span>
        )}
        {hasDetail && (
          <span style={{ marginLeft: 2, opacity: 0.7, display: "inline-flex" }}>
            {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </span>
        )}
      </button>
      {open && (
        <div style={{
          padding: "6px 10px 8px",
          borderTop: `1px solid color-mix(in srgb, ${accent} 18%, transparent)`,
          background: "color-mix(in srgb, var(--lm-text) 4%, transparent)",
          display: "flex", flexDirection: "column", gap: 6,
          maxWidth: 560,
        }}>
          {chip.args && (
            <div>
              <div style={{
                fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
                color: "var(--lm-text-muted)", marginBottom: 3,
              }}>ARGS</div>
              <pre style={{
                margin: 0, padding: 0,
                fontSize: 10, lineHeight: 1.45, fontFamily: "monospace",
                color: "var(--lm-text-dim)",
                whiteSpace: "pre-wrap", overflowWrap: "anywhere",
                maxHeight: 200, overflowY: "auto",
              }}>{JSON.stringify(chip.args, null, 2)}</pre>
            </div>
          )}
          {chip.result && chip.result !== "completed" && (
            <div>
              <div style={{
                fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
                color: "var(--lm-text-muted)", marginBottom: 3,
              }}>RESULT</div>
              <pre style={{
                margin: 0, padding: 0,
                fontSize: 10, lineHeight: 1.45, fontFamily: "monospace",
                color: "var(--lm-text)",
                whiteSpace: "pre-wrap", overflowWrap: "anywhere",
                maxHeight: 200, overflowY: "auto",
              }}>{chip.result}</pre>
              <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginTop: 3, fontStyle: "italic" }}>
                (truncated to 100 chars by server)
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const preview = text.length > 80 ? text.slice(0, 80) + "…" : text;

  return (
    <div style={{
      fontSize: 11, lineHeight: 1.5, borderRadius: 8,
      border: "1px solid rgba(168,85,247,0.2)",
      background: "rgba(168,85,247,0.06)",
      overflow: "hidden",
    }}>
      <button
        onClick={() => setExpanded((p) => !p)}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          width: "100%", padding: "6px 10px",
          background: "none", border: "none", cursor: "pointer",
          color: "rgba(168,85,247,0.8)", fontSize: 11, fontWeight: 600,
          textAlign: "left",
        }}
      >
        <span className="lm-blink" style={{ fontSize: 8 }}>●</span>
        <span>Thinking</span>
        <span style={{ fontSize: 9, opacity: 0.6 }}>{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded ? (
        <div style={{
          padding: "0 10px 8px", color: "var(--lm-text-muted)",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: 200, overflowY: "auto",
        }}>
          {text}
        </div>
      ) : (
        <div style={{
          padding: "0 10px 6px", color: "var(--lm-text-muted)",
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {preview}
        </div>
      )}
    </div>
  );
}

function formatDateLabel(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = today.getTime() - d.getTime();
  if (diff <= 0) return "Today";
  if (diff <= 86400_000) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// Compact relative timestamp ("now", "5m", "2h", "yesterday", "3d"); `now` keeps it stable within a render.
function relativeTime(ts: number, now: number, t: (k: string, p?: Record<string, string | number>) => string): string {
  const sec = Math.max(0, Math.floor((now - ts) / 1000));
  if (sec < 60) return t("chat.time.now");
  const min = Math.floor(sec / 60);
  if (min < 60) return t("chat.time.minutes", { n: min });
  const hr = Math.floor(min / 60);
  if (hr < 24) return t("chat.time.hours", { n: hr });
  const day = Math.floor(hr / 24);
  if (day === 1) return t("chat.time.yesterday");
  if (day < 7) return t("chat.time.days", { n: day });
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const CONVO_DOT_COLORS = [
  "var(--lm-amber)", "var(--lm-teal)", "var(--lm-green)",
  "var(--lm-blue)", "var(--lm-purple)", "var(--lm-orange)",
];
function convoColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return CONVO_DOT_COLORS[h % CONVO_DOT_COLORS.length];
}

function groupConvosByDate(convos: Conversation[]): { label: string; items: Conversation[] }[] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = today - 86400_000;
  const weekAgo = today - 7 * 86400_000;

  const pinned = newestFirst(convos.filter((c) => c.pinned));
  const unpinned = newestFirst(convos.filter((c) => !c.pinned));

  const groups: Record<string, Conversation[]> = {};
  const order: string[] = [];

  if (pinned.length > 0) {
    groups["Pinned"] = pinned;
    order.push("Pinned");
  }

  for (const c of unpinned) {
    let label: string;
    const activityAt = conversationActivityAt(c);
    if (activityAt >= today) label = "Today";
    else if (activityAt >= yesterday) label = "Yesterday";
    else if (activityAt >= weekAgo) label = "This week";
    else label = "Older";

    if (!groups[label]) { groups[label] = []; order.push(label); }
    groups[label].push(c);
  }

  return order.map((label) => ({ label, items: groups[label] }));
}
