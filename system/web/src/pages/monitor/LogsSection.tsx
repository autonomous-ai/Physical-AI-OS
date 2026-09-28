import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getApiToken, withApiToken } from "@/lib/api";
import { S } from "./styles";
import { API } from "./types";
import { StatusBadge } from "./components";

type LogSource = "hal" | "os-server" | "bootstrap" | "openclaw" | "openclaw-service" | "buddy";
const LOG_SOURCES: { id: LogSource; label: string; color: string }[] = [
  { id: "hal",              label: "HAL",        color: "var(--lm-green)" },
  { id: "os-server",        label: "OS",         color: "var(--lm-amber)" },
  { id: "bootstrap",        label: "Bootstrap",  color: "var(--lm-purple)" },
  { id: "openclaw",         label: "Agent",      color: "var(--lm-blue)" },
  { id: "openclaw-service", label: "Agent Service", color: "var(--lm-purple)" },
  { id: "buddy",            label: "Claude Desktop Buddy", color: "var(--lm-cyan)" },
];

const LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARN", "ERROR"] as const;
type LogLevel = (typeof LOG_LEVELS)[number];

// Word-boundary level detection avoids false positives like `error_count=0`.
const LEVEL_RE = {
  ERROR: /\b(ERROR|ERR)\b/i,
  WARN:  /\b(WARN(?:ING)?)\b/i,
  DEBUG: /\b(DEBUG|DBG)\b/i,
  INFO:  /\b(INFO|INF)\b/i,
};
function detectLevel(line: string): LogLevel {
  if (LEVEL_RE.ERROR.test(line)) return "ERROR";
  if (LEVEL_RE.WARN.test(line))  return "WARN";
  if (LEVEL_RE.DEBUG.test(line)) return "DEBUG";
  if (LEVEL_RE.INFO.test(line))  return "INFO";
  return "ALL";
}

const levelColor: Record<LogLevel, string> = {
  ALL: "var(--lm-text-dim)",
  DEBUG: "var(--lm-purple)",
  INFO: "var(--lm-text-dim)",
  WARN: "var(--lm-amber)",
  ERROR: "var(--lm-red)",
};

const levelFilterTone: Record<Exclude<LogLevel, "ALL">, { fg: string; bg: string }> = {
  DEBUG: { fg: "var(--lm-purple)", bg: "rgba(167,139,250,0.14)" },
  INFO:  { fg: "var(--lm-blue)",   bg: "rgba(96,165,250,0.14)" },
  WARN:  { fg: "var(--lm-amber)",  bg: "var(--lm-amber-dim)" },
  ERROR: { fg: "var(--lm-red)",    bg: "var(--lm-red-dim)" },
};

// Maps a raw level token ("DEBUG", "INF", "[abcDEBUG]") to a chip color.
function chipTone(token: string): { fg: string; bg: string } {
  const u = token.toUpperCase();
  if (/ERR/.test(u))   return levelFilterTone.ERROR;
  if (/WARN/.test(u))  return levelFilterTone.WARN;
  if (/DBG|DEBUG/.test(u)) return levelFilterTone.DEBUG;
  return levelFilterTone.INFO;
}

// Strip ANSI escape codes only when prefixed by ESC. The earlier fallback that
// matched any `[…m` ate parts of content like `[200ms]` from app logs.
// eslint-disable-next-line no-control-regex -- matching the ESC control char is the point: that anchor is what tells a real SGR sequence apart from log text like "[200ms]".
const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");

function LogPanel({ source, label, color, initialFilter, initialLevel, onFilterChange }: {
  source: LogSource; label: string; color: string;
  initialFilter: string; initialLevel: LogLevel;
  onFilterChange: (source: LogSource, filter: string, level: LogLevel) => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [lastN, setLastN] = useState(200);
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState(initialFilter);
  const [level, setLevel] = useState<LogLevel>(initialLevel);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sseRef = useRef<EventSource | null>(null);


  const fetchLines = useCallback(async () => {
    setLoading(true);
    try {
      const token = getApiToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const resp = await fetch(`${API}/logs/tail?source=${source}&lines=${lastN}`, { headers });
      if (!resp.ok) {
        setError(`HTTP ${resp.status} ${resp.statusText}`);
        setLines([]);
        return;
      }
      const r = await resp.json();
      const data = r?.data;
      if (data?.error) setError(data.error);
      else setError(null);
      setLines(Array.isArray(data?.lines) ? data.lines.map(stripAnsi) : []);
    } catch (e) {
      setError(`Fetch error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, [source, lastN]);

  useEffect(() => { fetchLines(); }, [fetchLines]);

  useEffect(() => {
    if (paused) return;

    let es: EventSource | null = null;
    const onLog = (e: Event) => {
      const line = stripAnsi((e as MessageEvent).data);
      if (line) setLines((prev) => [...prev.slice(-4999), line]);
    };
    const open = () => {
      if (es !== null) return;
      // EventSource cannot set headers; cookies attach and a legacy ?token rides along.
      es = new EventSource(withApiToken(`${API}/logs/stream?source=${source}`), { withCredentials: true });
      sseRef.current = es;
      es.addEventListener("log", onLog);
      es.addEventListener("error", () => { /* EventSource auto-reconnects */ });
    };
    const close = () => {
      if (es !== null) { es.close(); es = null; sseRef.current = null; }
    };
    const onVisibility = () => {
      if (document.hidden) close(); else open();
    };
    if (!document.hidden) open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close();
    };
  }, [source, paused, fetchLines]);

  const filtered = useMemo(() => {
    let result = lines;
    if (level !== "ALL") {
      const levelIdx = LOG_LEVELS.indexOf(level);
      result = result.filter((l) => {
        const ll = detectLevel(l);
        return ll === "ALL" || LOG_LEVELS.indexOf(ll) >= levelIdx;
      });
    }
    if (filter.trim()) {
      try {
        const re = new RegExp(filter, "i");
        result = result.filter((l) => re.test(l));
      } catch {
        const lower = filter.toLowerCase();
        result = result.filter((l) => l.toLowerCase().includes(lower));
      }
    }
    return result;
  }, [lines, level, filter]);

  const counts = useMemo(() => {
    const c = { ERROR: 0, WARN: 0 };
    for (const l of lines) {
      const ll = detectLevel(l);
      if (ll === "ERROR") c.ERROR++;
      else if (ll === "WARN") c.WARN++;
    }
    return c;
  }, [lines]);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filtered, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
  };

  const jumpToBottom = () => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    setAutoScroll(true);
  };

  const highlightLine = (line: string) => {
    if (!filter.trim()) return formatLine(line);
    try {
      // Split with a capture group instead of re.test() with /g (lastIndex mutation).
      const re = new RegExp(`(${filter})`, "gi");
      const parts = line.split(re);
      if (parts.length <= 1) return formatLine(line);
      return parts.map((p, i) =>
        i % 2 === 1
          ? <mark key={i} style={{ background: "rgba(245,158,11,0.4)", color: "inherit", borderRadius: 2, padding: "0 1px" }}>{p}</mark>
          : p,
      );
    } catch {
      return formatLine(line);
    }
  };

  // Formats a log line: dim timestamp, bold level, dim %key=value metadata.
  const formatLine = (line: string) => {
    // HAL: 2026-04-13 17:47:52,944 INFO hal.voice: message
    const pyMatch = line.match(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.\d]*)\s+(DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|DBG|INF)\s+([\s\S]*)$/i);
    // OS server: [0be]2026-04-13 17:55:13 [0beDEBUG] message %key=value
    const goMatch = line.match(/^(\[\w+\]\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(\[\w+(?:DEBUG|INFO|WARN|ERROR)\])\s+([\s\S]*)$/i);

    if (!pyMatch && !goMatch) return line;

    const ts = pyMatch ? pyMatch[1] : goMatch![1];
    const lvl = pyMatch ? pyMatch[2] : goMatch![2];
    const rest = pyMatch ? pyMatch[3] : goMatch![3];

    const metaIdx = rest.search(/\s%\w+=/);
    let msg = rest;
    let meta = "";
    if (metaIdx >= 0) {
      msg = rest.slice(0, metaIdx);
      meta = rest.slice(metaIdx);
    }

    const tone = chipTone(lvl);
    return (
      <>
        <span style={{ opacity: 0.35 }}>{ts}</span>
        {" "}
        <span
          style={{
            display: "inline-block",
            background: tone.bg,
            color: tone.fg,
            fontWeight: 700,
            fontSize: "0.92em",
            letterSpacing: "0.03em",
            padding: "1px 6px",
            borderRadius: 4,
            lineHeight: 1.4,
            verticalAlign: "baseline",
          }}
        >{lvl}</span>
        {" "}
        {msg}
        {meta && <span style={{ opacity: 0.3 }}>{meta}</span>}
      </>
    );
  };

  const btnStyle: React.CSSProperties = {
    fontSize: 12, padding: "5px 10px", borderRadius: 6,
    background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
    color: "var(--lm-text-dim)", cursor: "pointer", fontWeight: 600, lineHeight: 1,
  };
  const selectStyle: React.CSSProperties = {
    fontSize: 12, padding: "5px 8px", borderRadius: 6,
    background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
    color: "var(--lm-text)", cursor: "pointer",
  };

  const lvlTone = level !== "ALL" ? levelFilterTone[level] : null;
  const isEmpty = filtered.length === 0;

  return (
    <div style={{ ...S.card, flex: 1, minHeight: 0, padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--lm-border)", display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0, boxShadow: `0 0 6px ${color}` }} />
        <span style={{ ...S.cardLabel, marginBottom: 0, fontSize: 13 }}>{label}</span>

        <button onClick={fetchLines} style={btnStyle} title="Refresh">↻</button>
        <button
          onClick={() => setPaused((p) => !p)}
          title={paused ? "Resume live stream" : "Pause live stream"}
          style={{
            ...btnStyle,
            background: paused ? "var(--lm-amber-dim)" : "var(--lm-surface)",
            color: paused ? "var(--lm-amber)" : "var(--lm-text-dim)",
          }}
        >
          {paused ? "▶" : "⏸"}
        </button>
        <select
          value={lastN}
          onChange={(e) => setLastN(Number(e.target.value))}
          title="Tail size"
          style={selectStyle}
        >
          {[100, 200, 500, 1000].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>

        <span className="lm-log-sep" />

        <select
          value={level}
          onChange={(e) => { const v = e.target.value as LogLevel; setLevel(v); onFilterChange(source, filter, v); }}
          style={{
            ...selectStyle,
            background: lvlTone ? lvlTone.bg : "var(--lm-surface)",
            border: `1px solid ${lvlTone ? lvlTone.fg : "var(--lm-border)"}`,
            color: lvlTone ? lvlTone.fg : "var(--lm-text)",
            fontWeight: lvlTone ? 700 : 400,
          }}
        >
          {LOG_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <input
          type="text"
          value={filter}
          onChange={(e) => { setFilter(e.target.value); onFilterChange(source, e.target.value, level); }}
          placeholder="grep…"
          style={{
            fontSize: 12, padding: "5px 10px", borderRadius: 6, width: 150,
            background: filter ? "var(--lm-amber-dim)" : "var(--lm-surface)",
            border: `1px solid ${filter ? "var(--lm-amber)" : "var(--lm-border)"}`,
            color: "var(--lm-text)", fontFamily: "monospace",
            outline: "none",
          }}
        />
        {filter && (
          <button onClick={() => { setFilter(""); onFilterChange(source, "", level); }} style={{ ...btnStyle, padding: "5px 9px" }} title="Clear filter">✕</button>
        )}

        <span className="lm-log-sep" />

        <button
          onClick={() => {
            const text = (filtered.length ? filtered : lines).join("\n");
            const blob = new Blob([text], { type: "text/plain" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${source}-${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
            a.click();
            URL.revokeObjectURL(url);
          }}
          title="Download visible lines as .log"
          style={btnStyle}
        >↓</button>
        <button onClick={() => setLines([])} style={btnStyle} title="Clear view">Clear</button>

        {counts.ERROR > 0 && <StatusBadge text={`${counts.ERROR} ERR`} tone="error" />}
        {counts.WARN > 0 && <StatusBadge text={`${counts.WARN} WARN`} tone="active" />}

        <label style={{ marginLeft: "auto", fontSize: 11, color: "var(--lm-text-muted)", display: "flex", alignItems: "center", gap: 5, cursor: "pointer", userSelect: "none" }}>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
            style={{ width: 13, height: 13, accentColor: "var(--lm-amber)", cursor: "pointer" }}
          />
          Auto-scroll
        </label>
        <span style={{ fontSize: 11, color: "var(--lm-text-muted)", fontVariantNumeric: "tabular-nums" }}>
          {loading ? "Loading…" : error ? error : filtered.length !== lines.length ? `${filtered.length}/${lines.length}` : `${lines.length} lines`}
        </span>
      </div>

      <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          style={{
            height: "100%", overflowY: "auto", padding: "6px 0",
            fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
            fontSize: 11, lineHeight: 1.55,
            whiteSpace: "pre-wrap" as const,
            overflowWrap: "anywhere" as const,
          }}
          className="lm-hide-scroll"
        >
          {isEmpty ? (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 8, color: "var(--lm-text-muted)", textAlign: "center", padding: 24 }}>
              {loading ? (
                <>
                  <span className="lm-spin-ico" style={{ display: "inline-block", width: 18, height: 18, border: "2px solid var(--lm-border-hi)", borderTopColor: "var(--lm-amber)", borderRadius: "50%" }} />
                  <span style={{ fontSize: 11 }}>Loading {label} logs…</span>
                </>
              ) : (
                <>
                  <span style={{ fontSize: 22, opacity: 0.5 }}>{error ? "⚠" : "≋"}</span>
                  <span style={{ fontSize: 11 }}>
                    {error ? error : filter || level !== "ALL" ? "No matching lines." : `No log lines from ${label} yet.`}
                  </span>
                </>
              )}
            </div>
          ) : (
            filtered.map((line, i) => {
              const ll = detectLevel(line);
              return (
                <div
                  key={i}
                  className="lm-log-line"
                  style={{
                    padding: "3px 14px",
                    color: levelColor[ll],
                    borderLeft: `2px solid ${ll === "ERROR" ? "var(--lm-red)" : ll === "WARN" ? "var(--lm-amber)" : "transparent"}`,
                    background: ll === "ERROR" ? "var(--lm-red-dim)" : i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.018)",
                  }}
                >
                  {highlightLine(line)}
                </div>
              );
            })
          )}
        </div>

        {!autoScroll && !isEmpty && (
          <button className="lm-log-jump" onClick={jumpToBottom}>
            ↓ Jump to latest
          </button>
        )}
      </div>
    </div>
  );
}

const STORAGE_KEY = "lm-logs-state";

function loadLogState(): { active: LogSource; filters: Record<string, { filter: string; level: LogLevel }> } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        active: LOG_SOURCES.some((s) => s.id === parsed.active) ? parsed.active : "openclaw",
        filters: parsed.filters ?? {},
      };
    }
  } catch {
    // Corrupt saved state: fall through to the defaults.
  }
  return { active: "openclaw", filters: {} };
}

let _saveTimer: number | null = null;
function saveLogState(active: LogSource, filters: Record<string, { filter: string; level: LogLevel }>) {
  if (_saveTimer != null) clearTimeout(_saveTimer);
  _saveTimer = window.setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ active, filters }));
    } catch {
      // Best-effort persistence.
    }
  }, 250);
}

export function LogsSection() {
  const [saved] = useState(loadLogState);
  const [active, setActive] = useState<LogSource>(saved.active);
  const [filters, setFilters] = useState<Record<string, { filter: string; level: LogLevel }>>(saved.filters);

  const src = LOG_SOURCES.find((s) => s.id === active)!;

  const handleTabChange = (id: LogSource) => {
    setActive(id);
    saveLogState(id, filters);
  };

  const handleFilterChange = (source: LogSource, filter: string, level: LogLevel) => {
    setFilters((prev) => {
      const next = { ...prev, [source]: { filter, level } };
      saveLogState(active, next);
      return next;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0, height: "100%" }}>
      <div style={{ display: "flex", gap: 4, padding: "0 0 8px 0", flexShrink: 0 }}>
        {LOG_SOURCES.map((s) => (
          <button
            key={s.id}
            onClick={() => handleTabChange(s.id)}
            style={{
              fontSize: 11, padding: "5px 14px", borderRadius: 6, cursor: "pointer",
              border: active === s.id ? `1px solid ${s.color}` : "1px solid var(--lm-border)",
              background: active === s.id ? `${s.color}22` : "var(--lm-surface)",
              color: active === s.id ? s.color : "var(--lm-text-dim)",
              fontWeight: active === s.id ? 700 : 500,
              transition: "all 0.15s",
            }}
          >
            <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: s.color, marginRight: 5, verticalAlign: "middle" }} />
            {s.label}
          </button>
        ))}
      </div>
      <LogPanel
        key={active}
        source={src.id}
        label={src.label}
        color={src.color}
        initialFilter={filters[src.id]?.filter ?? ""}
        initialLevel={filters[src.id]?.level ?? "ALL"}
        onFilterChange={handleFilterChange}
      />
    </div>
  );
}
