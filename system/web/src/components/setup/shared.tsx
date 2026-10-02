import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { restoreAutonomousDefaults } from "@/lib/api";
import { Pencil, X, Eye, EyeOff, AlertTriangle } from "lucide-react";
import type { SectionId } from "@/hooks/setup/types";
import { C, FIELD_GAP, ADMIN_PASSWORD_MIN, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_FOCUS_SHADOW, INPUT_ERROR_SHADOW, INPUT_PAD_ONE_ICON, INPUT_PAD_TWO_ICONS } from "./styles";

export { C, FIELD_GAP, ADMIN_PASSWORD_MIN, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_FOCUS_SHADOW, INPUT_ERROR_SHADOW, INPUT_PAD_ONE_ICON, INPUT_PAD_TWO_ICONS };

// Red error line under an invalid input; renders null without an error.
export function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <div style={{
      fontSize: 12, color: C.red, marginTop: 6,
      display: "flex", alignItems: "center", gap: 5,
    }}>
      <span aria-hidden>⚠</span>{message}
    </div>
  );
}

export function Field({
  label, id, value, onChange, placeholder, type = "text", readOnly = false, required = false, error,
}: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; type?: string; readOnly?: boolean; required?: boolean;
  /** When set, the input shows the error (red border + glow) state and renders the message below. */
  error?: string;
}) {
  const [focused, setFocused] = useState(false);
  const hasError = !!error && !readOnly;
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>
        {label}
      </label>
      <input
        id={id} type={type} value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} autoComplete="off"
        readOnly={readOnly} required={required}
        aria-invalid={hasError || undefined}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          ...INPUT_STYLE,
          border: `1px solid ${borderColor(hasError, focused && !readOnly)}`,
          boxShadow: boxShadowFor(hasError, focused && !readOnly),
          ...(readOnly ? INPUT_READONLY_STYLE : null),
        }}
      />
      <FieldError message={hasError ? error : undefined} />
    </div>
  );
}

export function PasswordField({ label, id, value, onChange, placeholder, readOnly = false, error }: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; readOnly?: boolean;
  /** See Field.error — same red border/glow + message behaviour. */
  error?: string;
}) {
  const [show, setShow] = useState(false);
  const [focused, setFocused] = useState(false);
  const hasError = !!error && !readOnly;
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && (
        <label htmlFor={id} style={LABEL_STYLE}>
          {label}
        </label>
      )}
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly}
          aria-invalid={hasError || undefined}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            border: `1px solid ${borderColor(hasError, focused && !readOnly)}`,
            boxShadow: boxShadowFor(hasError, focused && !readOnly),
            padding: INPUT_PAD_ONE_ICON,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        <button type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide password" : "Show password"}
          style={{
            position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </div>
      <FieldError message={hasError ? error : undefined} />
    </div>
  );
}

// Border / glow resolution shared by the editable inputs.
function borderColor(hasError: boolean, focused: boolean): string {
  if (hasError) return C.red;
  if (focused) return C.amber;
  return C.border;
}
function boxShadowFor(hasError: boolean, focused: boolean): string {
  if (hasError) return INPUT_ERROR_SHADOW;
  if (focused) return INPUT_FOCUS_SHADOW;
  return "none";
}

// Shared lock/unlock + cancel-restore logic for LockedField and LockedPasswordField.
function useLockToggle(lockedInitially: boolean, value: string, onChange: (v: string) => void) {
  const [unlocked, setUnlocked] = useState(false);
  const originalRef = useRef<string | null>(null);
  useEffect(() => {
    if (lockedInitially && originalRef.current === null) {
      originalRef.current = value;
    }
  }, [lockedInitially, value]);
  const readOnly = lockedInitially && !unlocked;
  const handleCancel = () => {
    if (originalRef.current !== null) onChange(originalRef.current);
    setUnlocked(false);
  };
  return { readOnly, showToggle: lockedInitially, unlock: () => setUnlocked(true), handleCancel };
}

export function LockedField({
  lockedInitially, label, id, value, onChange, placeholder, type = "text", required = false,
}: {
  lockedInitially: boolean; label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; type?: string; required?: boolean;
}) {
  const { readOnly, showToggle, unlock, handleCancel } = useLockToggle(lockedInitially, value, onChange);
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>
        {label}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type={type} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            padding: showToggle ? INPUT_PAD_ONE_ICON : INPUT_STYLE.padding,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        {showToggle && (
          <button
            type="button"
            onClick={readOnly ? unlock : handleCancel}
            tabIndex={-1}
            aria-label={readOnly ? "Edit" : "Cancel edit"}
            title={readOnly ? "Edit" : "Cancel edit"}
            style={{
              position: "absolute", right: 0, top: 0, height: "100%",
              padding: "0 12px", background: "none", border: "none",
              color: readOnly ? C.amber : C.textMuted, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {readOnly ? <Pencil size={14} /> : <X size={15} />}
          </button>
        )}
      </div>
    </div>
  );
}

export function LockedPasswordField({
  lockedInitially, label, id, value, onChange, placeholder, required = false,
}: {
  lockedInitially: boolean; label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; required?: boolean;
}) {
  const [show, setShow] = useState(false);
  const { readOnly, showToggle, unlock, handleCancel } = useLockToggle(lockedInitially, value, onChange);
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && (
        <label htmlFor={id} style={LABEL_STYLE}>
          {label}
        </label>
      )}
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            padding: showToggle ? INPUT_PAD_TWO_ICONS : INPUT_PAD_ONE_ICON,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide" : "Show"}
          style={{
            position: "absolute", right: showToggle ? 37 : 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
        {showToggle && (
          <button
            type="button"
            onClick={readOnly ? unlock : handleCancel}
            tabIndex={-1}
            className="lm-eye-btn"
            aria-label={readOnly ? "Edit" : "Cancel edit"}
            title={readOnly ? "Edit" : "Cancel edit"}
            style={{
              position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
              height: 32, width: 32, padding: 0, background: "none", border: "none",
              color: readOnly ? C.amber : undefined, cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            {readOnly ? <Pencil size={14} /> : <X size={15} />}
          </button>
        )}
      </div>
    </div>
  );
}

// ConfiguredHint renders a "✓ configured" row for a secret field that the server already has on file.
export function ConfiguredHint({ label, editPath = "/setting" }: { label: string; editPath?: string }) {
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && <label style={LABEL_STYLE}>{label}</label>}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 10, padding: "10px 13px",
        background: C.bg, border: `1px solid ${C.border}`,
        borderRadius: 10, fontSize: 14, color: C.textDim,
      }}>
        <span><span style={{ color: C.green }}>✓</span>&nbsp;configured</span>
        <a href={editPath} style={{ color: C.amber, textDecoration: "none", fontSize: 13 }}>
          update →
        </a>
      </div>
    </div>
  );
}

export function SectionCard({ id, title, description, icon, active, children }: {
  id: SectionId | "runtime" | "timezone" | "led" | "mcp" | "plugins" | "scheduled"; title: string; description?: string; icon?: React.ReactNode; active: boolean; children: React.ReactNode;
}) {
  // Stay mounted when inactive (display:none) so inputs keep refs and state.
  return (
    <div
      id={`section-${id}`}
      className="lm-card lm-fade-in"
      style={{
        display: active ? "block" : "none",
        padding: "22px 24px", marginBottom: 16,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        {icon && (
          <div style={{
            flexShrink: 0,
            width: 36, height: 36, borderRadius: 10,
            background: C.amberDim, color: C.amber,
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: "inset 0 0 0 1px var(--lm-amber-glow)",
          }}>
            {icon}
          </div>
        )}
        <div style={{ minWidth: 0, paddingTop: icon ? 1 : 0 }}>
          <div style={{
            fontSize: 14.5, fontWeight: 600, color: C.text, lineHeight: 1.3,
          }}>
            {title}
          </div>
          {description && (
            <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.5, marginTop: 4 }}>
              {description}
            </div>
          )}
        </div>
      </div>
      {children}
    </div>
  );
}

// Themed replacement for window.confirm(); closes on overlay click and Escape, confirms on Enter.
export function ConfirmDialog({
  title, message, confirmLabel = "Confirm", cancelLabel = "Cancel",
  danger = false, onConfirm, onCancel,
}: {
  title: string; message?: string; confirmLabel?: string; cancelLabel?: string;
  danger?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
      else if (e.key === "Enter") onConfirm();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onConfirm, onCancel]);

  const accent = danger ? C.red : C.amber;
  const accentDim = danger ? "var(--lm-red-dim)" : C.amberDim;
  const accentGlow = danger ? "var(--lm-red-glow)" : "var(--lm-amber-glow)";
  return (
    <div
      onClick={onCancel}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        className="lm-card lm-pop"
        style={{ width: "min(380px, 100%)", padding: "20px 22px" }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: message ? 8 : 16 }}>
          <div style={{
            flexShrink: 0, width: 34, height: 34, borderRadius: 9,
            background: accentDim, color: accent,
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: `inset 0 0 0 1px ${accentGlow}`,
          }}>
            <AlertTriangle size={16} />
          </div>
          <div style={{ minWidth: 0, paddingTop: 1 }}>
            <div style={{ fontSize: 14.5, fontWeight: 600, color: C.text, lineHeight: 1.3 }}>
              {title}
            </div>
            {message && (
              <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.5, marginTop: 4, wordBreak: "break-word" }}>
                {message}
              </div>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
          <button
            type="button" onClick={onCancel}
            style={{
              padding: "8px 14px", borderRadius: 9, fontSize: 12.5, fontWeight: 500,
              cursor: "pointer", background: C.surface,
              border: `1px solid ${C.border}`, color: C.textDim,
            }}
          >
            {cancelLabel}
          </button>
          <button
            type="button" onClick={onConfirm} autoFocus
            style={{
              padding: "8px 14px", borderRadius: 9, fontSize: 12.5, fontWeight: 600,
              cursor: "pointer", background: accentDim,
              border: `1px solid ${accentGlow}`, color: accent,
            }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SkeletonBlock() {
  return (
    <div className="lm-card" style={{ padding: "20px 22px", marginBottom: 16 }}>
      <div style={{ width: 80, height: 8, borderRadius: 6, background: C.surface, marginBottom: 14 }} />
      <div style={{ width: "100%", height: 32, borderRadius: 6, background: C.surface, marginBottom: 10 }} />
    </div>
  );
}

/** Puts one settings section back on the credentials the device shipped with. */
export function RestoreDefaultsButton({ section, label }: {
  section: "llm" | "voice" | "realtime";
  label?: string;
}): React.JSX.Element {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const fire = () => {
    if (!armed) { setArmed(true); return; }
    setBusy(true);
    restoreAutonomousDefaults(section)
      .then(() => {
        toast.success("Restored the Autonomous defaults");
        window.location.reload();
      })
      .catch((e: Error) => {
        toast.error(e.message || "Could not restore");
        setBusy(false);
        setArmed(false);
      });
  };

  return (
    <button
      type="button"
      onClick={fire}
      onBlur={() => setArmed(false)}
      disabled={busy}
      style={{
        marginTop: 6, padding: "5px 10px", borderRadius: 6,
        fontSize: 11, fontWeight: 600,
        background: "transparent",
        color: armed ? "var(--lm-amber, #f5c25a)" : C.textMuted,
        border: `1px solid ${armed ? "var(--lm-amber, #f5c25a)" : C.border}`,
        cursor: busy ? "wait" : "pointer",
        opacity: busy ? 0.6 : 1,
        transition: "color 0.15s, border-color 0.15s",
      }}>
      {busy ? "Restoring…" : armed ? "Confirm — overwrite with Autonomous default"
        : (label ?? "Restore Autonomous default")}
    </button>
  );
}
