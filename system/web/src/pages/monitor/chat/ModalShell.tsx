import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { X, ArrowLeft } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useTheme } from "@/lib/useTheme";

// Mount order of on-screen shells, innermost last (portalled siblings share no context).
const shellStack: symbol[] = [];

export function ModalShell({
  icon: Icon, title, subtitle, width = 480, onClose, onBack, children, footer, bodyPadding = 18,
  headerActions,
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  /** Max width in px — the shell always shrinks to fit narrow viewports. */
  width?: number;
  onClose: () => void;
  /** When set, a back arrow appears left of the icon and Escape goes back instead of closing — so a drill-down doesn't dump the whole modal. */
  onBack?: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** 0 lets the body own its own padding (e.g. a full-bleed split pane). */
  bodyPadding?: number;
  /** Rendered left of the close button; menus here must open downward (the dialog clips). */
  headerActions?: ReactNode;
}) {
  const [, , themeClass] = useTheme();

  // Register once for the mounted lifetime; re-running would reorder the stack.
  const idRef = useRef<symbol | null>(null);
  if (idRef.current === null) idRef.current = Symbol("modal-shell");
  useEffect(() => {
    const id = idRef.current as symbol;
    shellStack.push(id);
    return () => {
      const i = shellStack.indexOf(id);
      if (i >= 0) shellStack.splice(i, 1);
    };
  }, []);

  // Escape is handled only by the top-most shell.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (shellStack[shellStack.length - 1] !== idRef.current) return;
      (onBack ?? onClose)();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, onBack]);

  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="lm-pop"
        style={{
          width: `min(${width}px, 100%)`,
          maxHeight: "min(680px, 90vh)",
          display: "flex", flexDirection: "column",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-amber) 4%, transparent), transparent 130px), var(--lm-surface)",
          border: "1px solid var(--lm-border-hi)",
          borderRadius: 14,
          boxShadow: "0 24px 64px -20px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.4)",
          overflow: "hidden",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 12, padding: "16px 18px", borderBottom: "1px solid var(--lm-border)",
          flexShrink: 0,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0 }}>
            {onBack && (
              <button
                type="button" onClick={onBack} aria-label="Back"
                className="lm-u-btn"
                style={{
                  width: 28, height: 28, borderRadius: 8, background: "var(--lm-bg)",
                  border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
                  display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                }}
              ><ArrowLeft size={14} /></button>
            )}
            <Icon size={16} style={{ color: "var(--lm-amber)", flexShrink: 0, alignSelf: "flex-start", marginTop: 3 }} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--lm-text)" }}>{title}</div>
              {subtitle && (
                <div style={{ fontSize: 11, color: "var(--lm-text-dim)", marginTop: 1 }}>{subtitle}</div>
              )}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            {headerActions}
            <button
              type="button" onClick={onClose} aria-label="Close"
              className="lm-u-btn"
              style={{
                width: 30, height: 30, borderRadius: 8, background: "var(--lm-bg)",
                border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
                display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
              }}
            >
              <X size={15} />
            </button>
          </div>
        </div>

        <div style={{
          flex: 1, minHeight: 0,
          overflowY: bodyPadding === 0 ? "hidden" : "auto",
          display: bodyPadding === 0 ? "flex" : undefined,
          padding: bodyPadding,
        }}>{children}</div>

        {footer && (
          <div style={{
            display: "flex", justifyContent: "flex-end", gap: 8,
            padding: "12px 18px", borderTop: "1px solid var(--lm-border)",
            background: "var(--lm-bg)", flexShrink: 0,
          }}>{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}
