import { useEffect } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { Trash2 } from "lucide-react";
import { useTheme } from "@/lib/useTheme";

// Themed replacement for window.confirm(), portalled to <body>.
export function ConfirmDialog({
  title, message, confirmLabel, confirmIcon, danger = false, onConfirm, onCancel,
}: {
  title: string; message?: ReactNode; confirmLabel: string; confirmIcon?: ReactNode;
  danger?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  const [, , themeClass] = useTheme();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const accent = danger ? "var(--lm-red)" : "var(--lm-amber)";
  const accentDim = danger ? "var(--lm-red-dim)" : "var(--lm-amber-dim)";
  const accentGlow = danger ? "var(--lm-red-glow)" : "var(--lm-amber-glow)";
  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onCancel}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        className="lm-pop"
        style={{
          width: "min(380px, 100%)", padding: "20px 22px",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-amber) 4%, transparent), transparent 130px), var(--lm-surface)",
          border: "1px solid var(--lm-border-hi)",
          borderRadius: 14, boxShadow: "0 24px 64px -20px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.4)",
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: message ? 8 : 16 }}>
          <div style={{
            flexShrink: 0, width: 34, height: 34, borderRadius: 9,
            background: accentDim, color: accent,
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: `inset 0 0 0 1px ${accentGlow}`,
          }}>
            {confirmIcon ?? <Trash2 size={16} />}
          </div>
          <div style={{ minWidth: 0, paddingTop: 1 }}>
            <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--lm-text)", lineHeight: 1.3 }}>
              {title}
            </div>
            {message && (
              <div style={{ fontSize: 13, color: "var(--lm-text-dim)", lineHeight: 1.5, marginTop: 4, wordBreak: "break-word" }}>
                {message}
              </div>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
          <button
            type="button" onClick={onCancel}
            style={{
              padding: "8px 14px", borderRadius: 7, fontSize: 12, fontWeight: 600, cursor: "pointer",
              background: "var(--lm-bg)", border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)",
            }}
          >
            Cancel
          </button>
          <button
            type="button" onClick={onConfirm} autoFocus
            style={{
              padding: "8px 14px", borderRadius: 7, fontSize: 12, fontWeight: 600, cursor: "pointer",
              background: accentDim, border: `1px solid ${accentGlow}`, color: accent,
              display: "inline-flex", alignItems: "center", gap: 6,
            }}
          >
            {confirmIcon ?? <Trash2 size={13} />} {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
