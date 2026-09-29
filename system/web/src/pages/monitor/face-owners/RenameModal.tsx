import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Pencil, X, Loader2 } from "lucide-react";

// Rename modal; fully controlled by the parent.
export function RenameModal({
  themeClass, renameValue, setRenameValue, renameError, setRenameError, renameSaving,
  onClose, onSubmit, inputStyle, fieldLabel, btnStyle,
}: {
  themeClass: string;
  renameValue: string; setRenameValue: (v: string) => void;
  renameError: string; setRenameError: (v: string) => void;
  renameSaving: boolean;
  onClose: () => void;
  onSubmit: () => void;
  inputStyle: CSSProperties;
  fieldLabel: CSSProperties;
  btnStyle: CSSProperties;
}) {
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
        aria-label="Rename user"
        className="lm-pop"
        style={{
          width: "min(380px, 100%)",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-amber) 4%, transparent), transparent 130px), var(--lm-surface)",
          border: "1px solid var(--lm-border-hi)",
          borderRadius: 14, boxShadow: "0 24px 64px -20px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.4)",
          overflow: "hidden",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 12, padding: "16px 18px", borderBottom: "1px solid var(--lm-border)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Pencil size={16} style={{ color: "var(--lm-amber)" }} />
            <span style={{ fontSize: 14.5, fontWeight: 600, color: "var(--lm-text)" }}>Rename User</span>
          </div>
          <button
            type="button" onClick={onClose} aria-label="Close"
            className="lm-u-btn"
            style={{
              width: 30, height: 30, borderRadius: 8, background: "var(--lm-bg)",
              border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <X size={15} />
          </button>
        </div>
        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <div>
            <label htmlFor="rename-input" style={fieldLabel}>New name</label>
            <input
              id="rename-input"
              type="text"
              autoFocus
              value={renameValue}
              onChange={(e) => { setRenameValue(e.target.value); if (renameError) setRenameError(""); }}
              onKeyDown={(e) => { if (e.key === "Enter" && !renameSaving) onSubmit(); }}
              className="lm-u-input"
              style={inputStyle}
            />
            <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 5 }}>
              Lowercase letters, digits, _ and - only.
            </div>
          </div>
          {renameError && (
            <div style={{
              fontSize: 11.5, color: "var(--lm-red)", padding: "7px 10px", borderRadius: 7,
              background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
            }}>{renameError}</div>
          )}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              type="button" onClick={onClose}
              className="lm-u-btn"
              style={{ ...btnStyle, padding: "8px 14px", fontSize: 12 }}
            >
              Cancel
            </button>
            <button
              type="button" onClick={onSubmit}
              disabled={renameSaving || !renameValue.trim()}
              className={"lm-u-btn" + (renameSaving || !renameValue.trim() ? "" : " lm-u-btn-primary")}
              style={{
                ...btnStyle, padding: "8px 14px", fontSize: 12,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              {renameSaving
                ? <><Loader2 size={13} className="lm-spin" /> Saving…</>
                : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
