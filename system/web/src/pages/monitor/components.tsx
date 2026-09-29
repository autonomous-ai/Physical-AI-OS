import { useState, useEffect } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { RotateCw } from "lucide-react";
import { getApiToken } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { API } from "./types";
import { S } from "./styles";

export function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: ok ? "var(--lm-green)" : "var(--lm-red)",
        boxShadow: ok ? "0 0 6px var(--lm-green)" : "none",
        flexShrink: 0,
      }}
    />
  );
}

// "agent" is a virtual target that os-server resolves to the active runtime's CLI.
export function SoftwareUpdateButton({ target, label, onTriggered }: {
  target: "os-server" | "bootstrap" | "web" | "hal" | "device" | "agent";
  label: string;
  onTriggered?: (target: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const trigger = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const token = getApiToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const r = await fetch(`${API}/system/software-update/${target}`, { method: "POST", headers });
      if (r.ok) {
        onTriggered?.(target);
      } else {
        let reason = "Failed";
        try {
          const j = await r.json();
          if (typeof j?.message === "string" && j.message) reason = j.message;
        } catch { /* keep the generic word */ }
        setMsg(reason.length > 42 ? `${reason.slice(0, 41)}…` : reason);
      }
    } catch {
      setMsg("Unreachable");
    } finally {
      setBusy(false);
      setTimeout(() => setMsg(null), 6000);
    }
  };
  return (
    <button
      onClick={trigger}
      disabled={busy}
      style={{
        padding: "3px 8px",
        fontSize: 9,
        fontWeight: 600,
        border: "1px solid var(--lm-border)",
        borderRadius: 4,
        background: "transparent",
        color: "var(--lm-amber)",
        cursor: busy ? "wait" : "pointer",
        opacity: busy ? 0.6 : 1,
      }}
    >
      {busy ? "…" : label}
      {msg && <span style={{ marginLeft: 4, color: msg === "OK" ? "var(--lm-green)" : "var(--lm-red)" }}>{msg}</span>}
    </button>
  );
}

// Icon-sized restart button for the Agent Gateway card.
export function RestartAgentButton({ agentName }: { agentName?: string }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const trigger = async () => {
    const label = agentName ? ` (${agentName})` : "";
    if (busy) return;
    if (!window.confirm(
      `Restart the agent gateway${label}?\n\n` +
      `This will re-enable auto-start on boot, then drop the current session and reconnect. ` +
      `Please wait a few seconds after clicking OK — do NOT click Restart again while it's spinning.`
    )) return;
    setBusy(true);
    setMsg(null);
    try {
      const token = getApiToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const r = await fetch(`${API}/agent/restart`, { method: "POST", headers });
      setMsg(r.ok ? "Restarted" : "Failed");
    } catch {
      setMsg("Unreachable");
    } finally {
      setBusy(false);
      setTimeout(() => setMsg(null), 4000);
    }
  };
  return (
    <div style={{
      position: "absolute", right: 8, bottom: 8,
      display: "flex", alignItems: "center", gap: 4,
    }}>
      {msg && (
        <span style={{ fontSize: 9.5, fontWeight: 600, color: msg === "OK" ? "var(--lm-green)" : "var(--lm-red)" }}>
          {msg}
        </span>
      )}
      <button
        onClick={trigger}
        disabled={busy}
        title={`Restart agent${agentName ? ` (${agentName})` : ""}`}
        aria-label="Restart agent"
        style={{
          display: "flex", alignItems: "center", justifyContent: "center",
          width: 24, height: 24, padding: 0, borderRadius: 6,
          background: "transparent",
          border: "1px solid var(--lm-border)",
          color: "var(--lm-text-muted)",
          cursor: busy ? "wait" : "pointer",
          opacity: busy ? 0.5 : 0.8,
          transition: "all 0.15s ease",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.color = "var(--lm-amber)";
          e.currentTarget.style.borderColor = "var(--lm-amber)";
          e.currentTarget.style.opacity = "1";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.color = "var(--lm-text-muted)";
          e.currentTarget.style.borderColor = "var(--lm-border)";
          e.currentTarget.style.opacity = "0.8";
        }}
      >
        <RotateCw size={12} className={busy ? "lm-spin-ico" : undefined} />
      </button>
    </div>
  );
}

// DevicePowerButtons uses os-server rather than HAL directly.
export function DevicePowerButtons() {
  const [busy, setBusy] = useState<"reboot" | "shutdown" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<"reboot" | "shutdown" | null>(null);

  const trigger = async (action: "reboot" | "shutdown") => {
    if (busy) return;
    const isShutdown = action === "shutdown";
    setBusy(action);
    setMessage(null);
    let accepted = false;
    try {
      const token = getApiToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const response = await fetch(`${API}/system/${action}`, { method: "POST", headers });
      if (response.ok) {
        accepted = true;
        setMessage(isShutdown ? "Shutting down…" : "Restarting…");
        return;
      }
      let reason = "Failed";
      try {
        const body = await response.json();
        if (typeof body?.message === "string" && body.message) reason = body.message;
      } catch { /* Keep the generic error. */ }
      setMessage(reason);
    } catch {
      setMessage("Unreachable");
    } finally {
      if (!accepted) {
        setBusy(null);
        setTimeout(() => setMessage(null), 5000);
      }
    }
  };

  const buttonStyle = (action: "reboot" | "shutdown"): React.CSSProperties => {
    const isShutdown = action === "shutdown";
    const active = busy === action;
    return {
      flex: 1,
      padding: "6px 9px",
      borderRadius: 6,
      border: `1px solid ${isShutdown ? "rgba(248,113,113,0.55)" : "rgba(245,158,11,0.55)"}`,
      background: isShutdown ? "rgba(248,113,113,0.10)" : "var(--lm-amber-dim)",
      color: isShutdown ? "var(--lm-red)" : "var(--lm-amber)",
      fontSize: 11,
      fontWeight: 650,
      cursor: busy ? "wait" : "pointer",
      opacity: busy && !active ? 0.45 : 1,
    };
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={() => setConfirmAction("reboot")} disabled={!!busy} style={buttonStyle("reboot")}>Reboot</button>
        <button onClick={() => setConfirmAction("shutdown")} disabled={!!busy} style={buttonStyle("shutdown")}>Shut down</button>
      </div>
      {message && <div style={{ marginTop: 7, fontSize: 10.5, color: busy ? "var(--lm-text-dim)" : "var(--lm-red)" }}>{message}</div>}
      {confirmAction && (
        <ConfirmDialog
          title={confirmAction === "shutdown" ? "Shut down robot?" : "Restart robot?"}
          message={confirmAction === "shutdown"
            ? "The robot will announce the shutdown, release its servos, and turn off. You must restore power before it can come back online."
            : "The robot will announce the reboot and be unavailable for about 30 seconds."}
          confirmLabel={confirmAction === "shutdown" ? "Shut down" : "Restart"}
          destructive={confirmAction === "shutdown"}
          onCancel={() => setConfirmAction(null)}
          onConfirm={() => {
            const action = confirmAction;
            setConfirmAction(null);
            void trigger(action);
          }}
        />
      )}
    </div>
  );
}

export function SoftwareUpdateButtons() {
  return (
    <div style={{ marginTop: 4, display: "flex", flexDirection: "column", gap: 2 }}>
      <SoftwareUpdateButton target="web" label="software-update web" />
      <SoftwareUpdateButton target="os-server" label="software-update os-server" />
      <SoftwareUpdateButton target="hal" label="software-update hal" />
    </div>
  );
}

export function HWBadge({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div
      className={ok ? undefined : "lm-hw-down"}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "5px 10px",
        borderRadius: 8,
        background: ok ? "var(--lm-green-dim)" : "var(--lm-red-dim)",
        border: `1px solid ${ok ? "color-mix(in srgb, var(--lm-green) 30%, transparent)" : "color-mix(in srgb, var(--lm-red) 25%, transparent)"}`,
        fontSize: 11.5,
        fontWeight: 500,
        color: ok ? "var(--lm-green)" : "var(--lm-red)",
      }}
    >
      <StatusDot ok={ok} />
      {label}
    </div>
  );
}

// Shimmering placeholder bar that holds a card's height while loading.
export function Skeleton({ width = "100%", height = 12, style }: {
  width?: number | string;
  height?: number;
  style?: React.CSSProperties;
}) {
  return <div className="lm-skel" style={{ width, height, ...style }} />;
}

export function SkeletonRows({ lines = 4 }: { lines?: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 2 }}>
      {Array.from({ length: lines }).map((_, i) => (
        <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <Skeleton width={`${38 + ((i * 13) % 22)}%`} height={11} />
          <Skeleton width={`${28 + ((i * 7) % 18)}%`} height={11} />
        </div>
      ))}
    </div>
  );
}

export function GaugeRing({
  value,
  label,
  detail,
  color = "var(--lm-amber)",
  size = 110,
}: {
  value: number;
  label: string;
  detail?: string;
  color?: string;
  size?: number;
}) {
  const r = (size - 18) / 2;
  const circ = 2 * Math.PI * r;
  const filled = (Math.min(100, Math.max(0, value)) / 100) * circ;
  const glowId = `glow-${label.replace(/\s/g, "")}`;

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
      <svg width={size} height={size} style={{ overflow: "visible" }}>
        <defs>
          <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        <circle
          cx={size / 2} cy={size / 2} r={r}
          fill="none"
          stroke="var(--lm-border)"
          strokeWidth={8}
        />
        <circle
          cx={size / 2} cy={size / 2} r={r}
          fill="none"
          stroke={color}
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${circ}`}
          strokeDashoffset={0}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ filter: `url(#${glowId})`, transition: "stroke-dasharray 0.7s ease" }}
        />
        <text
          x={size / 2} y={size / 2 - 4}
          textAnchor="middle"
          dominantBaseline="middle"
          fill={color}
          fontSize={size * 0.18}
          fontWeight={700}
        >
          {Math.round(value)}%
        </text>
        {detail && (
          <text
            x={size / 2} y={size / 2 + size * 0.15}
            textAnchor="middle"
            dominantBaseline="middle"
            fill="var(--lm-text-muted)"
            fontSize={size * 0.1}
          >
            {detail}
          </text>
        )}
      </svg>
      <span style={{ fontSize: 11, color: "var(--lm-text-dim)", fontWeight: 500 }}>{label}</span>
    </div>
  );
}

export function Sparkline({
  data,
  color = "var(--lm-amber)",
  height = 44,
  max,
  grid = false,
}: {
  data: number[];
  color?: string;
  height?: number;
  // Locks the Y scale max; otherwise auto-scales.
  max?: number;
  grid?: boolean;
}) {
  if (data.length < 2) return <div style={{ height }} />;
  const w = 280;
  const h = height;
  const yMax = max ?? Math.max(...data, 1);
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - (Math.min(v, yMax) / yMax) * (h - 4) - 2;
    return `${x},${y}`;
  });
  const areaPath =
    `M 0,${h} ` +
    pts.join(" L ") +
    ` L ${w},${h} Z`;

  const gridLevels = grid ? [0, 0.25, 0.5, 0.75, 1] : [];

  const svg = (
    // Pin pixel height: width:100% + preserveAspectRatio="none" would scale it.
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: "block", height: h }}>
      <defs>
        <linearGradient id={`sg-${color.replace(/[^a-z]/gi, "")}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.25} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {gridLevels.map((g) => {
        const y = h - g * (h - 4) - 2;
        return (
          <line
            key={g}
            x1={0}
            x2={w}
            y1={y}
            y2={y}
            stroke="var(--lm-border)"
            strokeWidth={0.6}
            strokeDasharray="3 4"
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
      <path d={areaPath} fill={`url(#sg-${color.replace(/[^a-z]/gi, "")})`} />
      <polyline
        points={pts.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );

  if (!grid) return svg;

  // HTML labels keep a fixed pixel size despite the SVG's non-uniform stretch.
  return (
    <div style={{ position: "relative", paddingRight: 28 }}>
      {svg}
      <div style={{ position: "absolute", top: 0, right: 0, bottom: 0, width: 26 }}>
        {gridLevels.map((g) => {
          const yPct = (1 - g) * 100;
          return (
            <span key={g} style={{
              position: "absolute",
              right: 0,
              top: `${yPct}%`,
              transform: g === 1 ? "translateY(0)" : g === 0 ? "translateY(-100%)" : "translateY(-50%)",
              fontSize: 9,
              color: "var(--lm-text-muted)",
              fontFamily: "monospace",
              lineHeight: 1,
              padding: "0 2px",
            }}>
              {Math.round(yMax * g)}
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function SignalBars({ value }: { value: number }) {
  const bars = 4;
  const active = value >= -50 ? 4 : value >= -65 ? 3 : value >= -75 ? 2 : value >= -85 ? 1 : 0;
  const tierColor =
    active >= 3 ? "var(--lm-green)" :
    active === 2 ? "var(--lm-amber)" :
    "var(--lm-red)";
  return (
    <div style={{ display: "flex", gap: 2, alignItems: "flex-end" }}>
      {Array.from({ length: bars }).map((_, i) => (
        <div
          key={i}
          style={{
            width: 4,
            height: 6 + i * 3,
            borderRadius: 1,
            background: i < active ? tierColor : "var(--lm-border-hi)",
          }}
        />
      ))}
    </div>
  );
}

export function StatPill({ label, value, color, bullet }: {
  label: string;
  value: string | number;
  color?: string;
  bullet?: string;
}) {
  return (
    <div style={{
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      padding: "6px 12px",
      background: "var(--lm-surface)",
      borderRadius: 8,
      border: "1px solid var(--lm-border)",
      borderLeft: bullet ? `3px solid ${bullet}` : "1px solid var(--lm-border)",
    }}>
      <span style={{ fontSize: 11.5, color: "var(--lm-text-dim)", display: "flex", alignItems: "center", gap: 7 }}>
        {bullet && (
          <span style={{
            display: "inline-block",
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: bullet,
            boxShadow: `0 0 5px ${bullet}80`,
          }} />
        )}
        {label}
      </span>
      <span style={{ fontSize: 12, fontWeight: 600, color: color || "var(--lm-text)" }}>{value}</span>
    </div>
  );
}

// StatRow renders the recurring "label left / value right" row used across the Overview status cards (Agent Gateway, Network, Presence, Versions…).
export function StatRow({ label, value, color, mono }: {
  label: string;
  value: React.ReactNode;
  color?: string;
  mono?: boolean;
}) {
  const isPrimitive = typeof value === "string" || typeof value === "number";
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>{label}</span>
      {isPrimitive ? (
        <span style={{
          fontSize: mono ? 11 : 12.5,
          fontWeight: 600,
          color: color ?? "var(--lm-text)",
          fontFamily: mono ? "monospace" : undefined,
        }}>{value}</span>
      ) : value}
    </div>
  );
}

export const STATUS_TONE = {
  ok:     { color: "var(--lm-green)",      bg: "rgba(52,211,153,0.1)",  border: "rgba(52,211,153,0.3)" },
  error:  { color: "var(--lm-red)",        bg: "rgba(239,68,68,0.1)",   border: "rgba(239,68,68,0.3)" },
  active: { color: "var(--lm-amber)",      bg: "rgba(245,158,11,0.1)",  border: "rgba(245,158,11,0.3)" },
  idle:   { color: "var(--lm-text-muted)", bg: "rgba(80,74,60,0.4)",    border: "var(--lm-border)" },
} as const;

export type StatusTone = keyof typeof STATUS_TONE;

// StatusBadge is the uppercase pill in card headers (ONLINE/OFFLINE, ACTIVE, session Active/Pending).
export function StatusBadge({ text, tone, ok, pulse }: {
  text: string;
  tone?: StatusTone;
  ok?: boolean;
  pulse?: boolean;
}) {
  const t = STATUS_TONE[tone ?? (ok ? "ok" : "error")];
  return (
    <span className={pulse ? "lm-pulse" : undefined} style={{
      fontSize: 10, padding: "3px 9px", borderRadius: 4, fontWeight: 700,
      background: t.bg, color: t.color, border: `1px solid ${t.border}`,
    }}>
      {text}
    </span>
  );
}

// CardLabel renders a card's uppercase heading with a small amber icon chip in front
export function CardLabel({ icon, text }: { icon: ReactNode; text: string }) {
  return (
    <div style={{ ...S.cardLabel, display: "flex", alignItems: "center", gap: 8, marginBottom: 0 }}>
      <span className="lm-mon-chip" aria-hidden>{icon}</span>
      <span>{text}</span>
    </div>
  );
}

// Confirmation modal; Esc or backdrop cancels, `destructive` reads red.
export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [, , themeClass] = useTheme();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Portal to <body>: card overflow/transform would clip a fixed dialog.
  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 2000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ ...S.card, width: "min(380px, 100%)", display: "flex", flexDirection: "column", gap: 14 }}
      >
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--lm-text)" }}>{title}</div>
        <div style={{ fontSize: 13, lineHeight: 1.55, color: "var(--lm-text-dim)" }}>{message}</div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4 }}>
          <button onClick={onCancel} className="lm-confirm-btn lm-confirm-btn--cancel">
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            autoFocus
            className={"lm-confirm-btn " + (destructive ? "lm-confirm-btn--danger" : "lm-confirm-btn--primary")}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
