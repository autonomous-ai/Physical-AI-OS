import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import "./technical-panels.css";

interface SessionMeta {
  id: string;
}
type Status = "connecting" | "open" | "closed";

const MAX_TABS = 6;

// Random id used only as a React key.
function newId() { return Math.random().toString(36).slice(2, 9); }

export function CliSection() {
  const [sessions, setSessions] = useState<SessionMeta[]>(() => [{ id: newId() }]);
  const [active, setActive] = useState<string>(sessions[0].id);

  const addTab = () => {
    if (sessions.length >= MAX_TABS) return;
    const id = newId();
    setSessions((prev) => [...prev, { id }]);
    setActive(id);
  };

  const closeTab = (id: string) => {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      if (next.length === 0) {
        const fresh = { id: newId() };
        setActive(fresh.id);
        return [fresh];
      }
      if (active === id) setActive(next[next.length - 1].id);
      return next;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 8 }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 4, flexShrink: 0,
        flexWrap: "wrap",
      }}>
        {sessions.map((s, i) => (
          <TabPill
            key={s.id}
            name={`shell ${i + 1}`}
            active={s.id === active}
            onSelect={() => setActive(s.id)}
            onClose={() => closeTab(s.id)}
          />
        ))}
        <button
          onClick={addTab}
          aria-label="New shell"
          disabled={sessions.length >= MAX_TABS}
          title={sessions.length >= MAX_TABS ? `Max ${MAX_TABS} sessions` : "New shell"}
          style={{
            fontSize: 16, minWidth: 40, minHeight: 40, padding: "8px 12px", borderRadius: 5,
            background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
            color: sessions.length >= MAX_TABS ? "var(--lm-text-muted)" : "var(--lm-amber)",
            cursor: sessions.length >= MAX_TABS ? "not-allowed" : "pointer",
            fontWeight: 700, lineHeight: 1,
          }}
        >+</button>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12, fontFamily: "monospace", color: "var(--lm-text-dim)" }}>
          Ctrl+C/Z · arrows · tab-complete
        </span>
      </div>

      <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
        {sessions.map((s) => (
          <TerminalSession
            key={s.id}
            visible={s.id === active}
          />
        ))}
      </div>
    </div>
  );
}

function TabPill({ name, active, onSelect, onClose }: {
  name: string;
  active: boolean;
  onSelect: () => void;
  onClose: () => void;
}) {
  return (
    <div className="lm-cli-tab" data-active={active}>
      <button type="button" onClick={onSelect} aria-pressed={active}>{name}</button>
      <button type="button" onClick={onClose} aria-label={`Close ${name}`} title="Close session">×</button>
    </div>
  );
}

// TerminalSession owns one xterm + WS lifecycle.
function TerminalSession({ visible }: { visible: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<Status>("connecting");
  const [connectionAttempt, setConnectionAttempt] = useState(0);

  const refit = useCallback(() => {
    const f = fitRef.current;
    const t = termRef.current;
    const ws = wsRef.current;
    if (!f || !t) return;
    try { f.fit(); } catch { /* fit() throws while the host element is detached or zero-sized; the next resize re-fits. */ }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", rows: t.rows, cols: t.cols }));
    }
  }, []);

  useEffect(() => {
    if (!hostRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      fontSize: 12.5,
      lineHeight: 1.2,
      convertEol: true,
      scrollback: 5000,
      theme: {
        background: "#0c0b09",
        foreground: "#dad6cd",
        cursor: "#f59e0b",
        cursorAccent: "#0c0b09",
        selectionBackground: "rgba(245,158,11,0.35)",
        black: "#1f1b16", red: "#ef4444", green: "#34d399", yellow: "#f59e0b",
        blue: "#60a5fa", magenta: "#c084fc", cyan: "#2dd4bf", white: "#dad6cd",
        brightBlack: "#504a3c", brightRed: "#fca5a5", brightGreen: "#6ee7b7",
        brightYellow: "#fcd34d", brightBlue: "#93c5fd", brightMagenta: "#d8b4fe",
        brightCyan: "#5eead4", brightWhite: "#f5f5f5",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current);
    try { fit.fit(); } catch { /* First fit right after open(): the host may not be laid out yet, the ResizeObserver below fits again. */ }
    termRef.current = term;
    fitRef.current = fit;

    const ro = new ResizeObserver(() => refit());
    ro.observe(hostRef.current);

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/system/shell`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("open");
      refit();
      term.focus();
    };
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
      else if (typeof e.data === "string") term.write(e.data);
    };
    ws.onclose = () => {
      setStatus("closed");
      term.write("\r\n\x1b[90m[shell closed]\x1b[0m\r\n");
    };
    ws.onerror = () => {
      term.write("\r\n\x1b[31m[shell connection error]\x1b[0m\r\n");
    };

    const dataDisposable = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });

    const onWinResize = () => refit();
    window.addEventListener("resize", onWinResize);

    return () => {
      window.removeEventListener("resize", onWinResize);
      ro.disconnect();
      dataDisposable.dispose();
      // A retired socket must not change the status of a replacement session.
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try { ws.close(); } catch { /* Unmount teardown: close() throws on an already-closing socket, which is the state we want anyway. */ }
      term.dispose();
      termRef.current = null;
      wsRef.current = null;
      fitRef.current = null;
    };
  }, [refit, connectionAttempt]);

  // xterm mis-measures inside display:none, so re-fit when visible.
  useEffect(() => {
    if (!visible) return;
    const t = setTimeout(() => {
      refit();
      termRef.current?.focus();
    }, 30);
    return () => clearTimeout(t);
  }, [visible, refit]);

  return (
    <div style={{
      position: "absolute", inset: 0,
      display: visible ? "flex" : "none",
      flexDirection: "column", gap: 6,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8, flexShrink: 0,
        fontSize: 11, color: "var(--lm-text-muted)",
      }}>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 5,
          padding: "2px 7px", borderRadius: 4,
          background:
            status === "open"     ? "rgba(52,211,153,0.15)" :
            status === "closed"   ? "rgba(248,113,113,0.15)" :
                                    "rgba(245,158,11,0.15)",
          color:
            status === "open"     ? "var(--lm-green)" :
            status === "closed"   ? "var(--lm-red)" :
                                    "var(--lm-amber)",
          fontWeight: 700, fontSize: 9.5, letterSpacing: "0.05em",
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: "currentColor",
            boxShadow: "0 0 4px currentColor",
          }} />
          {status.toUpperCase()}
        </span>
        {status === "closed" && (
          <button type="button" className="lm-cli-key" onClick={() => {
            setStatus("connecting");
            setConnectionAttempt((attempt) => attempt + 1);
          }}>Reconnect</button>
        )}
      </div>
      <div className="lm-cli-mobile-keys" aria-label="Terminal keys">
        {[
          ["Ctrl+C", "\x03"], ["Tab", "\t"], ["Esc", "\x1b"],
          ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"],
        ].map(([label, sequence]) => (
          <button key={label} type="button" className="lm-cli-key" disabled={status !== "open"}
            aria-label={label === "↑" ? "Arrow up" : label === "↓" ? "Arrow down" : label === "←" ? "Arrow left" : label === "→" ? "Arrow right" : label}
            onClick={() => {
              if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(sequence);
              termRef.current?.focus();
            }}>{label}</button>
        ))}
      </div>
      <div
        ref={hostRef}
        style={{
          flex: 1, minHeight: 0, width: "100%",
          background: "#0c0b09",
          border: "1px solid var(--lm-border)",
          borderRadius: 10,
          padding: "8px 10px",
          overflow: "hidden",
        }}
      />
    </div>
  );
}
