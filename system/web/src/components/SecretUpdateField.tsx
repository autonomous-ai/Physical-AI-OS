import { useState } from "react";
import { Pencil, X, Eye, EyeOff } from "lucide-react";
import { C } from "@/components/setup/shared";

// Write-only credential input; an empty value means "no change" and must be omitted from the PUT payload.
export function SecretUpdateField({
  label, id, configured, value, onChange, placeholder,
}: {
  label: string;
  id: string;
  /** True when the backend has a value on file. */
  configured: boolean;
  /** Pending new value the operator typed. */
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [editing, setEditing] = useState(!configured);
  const [show, setShow] = useState(false);
  const [focused, setFocused] = useState(false);

  const inert = configured && !editing;

  return (
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{
        display: "flex", justifyContent: "space-between",
        fontSize: 11, color: C.textDim, marginBottom: 5,
      }}>
        <span>{label}</span>
        {configured && (
          <span style={{ color: C.green, fontSize: 10 }}>configured</span>
        )}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id}
          type={show ? "text" : "password"}
          value={inert ? "" : value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={inert ? "•••••••• (saved)" : (placeholder ?? "")}
          autoComplete="new-password"
          readOnly={inert}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: inert ? C.bg : C.surface,
            border: `1px solid ${focused && !inert ? C.amber : C.border}`,
            borderRadius: 7,
            padding: editing && !inert ? "8px 64px 8px 11px" : "8px 36px 8px 11px",
            fontSize: 12.5, color: inert ? C.textDim : C.text, outline: "none",
            cursor: inert ? "default" : "text",
            transition: "border-color 0.15s",
          }}
        />
        {!inert && (
          <button
            type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
            style={{
              position: "absolute",
              right: configured ? 28 : 0,
              top: 0, height: "100%",
              padding: "0 10px", background: "none", border: "none",
              color: C.textMuted, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {show ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        )}
        {configured && (
          <button
            type="button"
            onClick={() => {
              if (editing) {
                onChange("");
                setShow(false);
              }
              setEditing((v) => !v);
            }}
            tabIndex={-1}
            aria-label={editing ? "Cancel edit" : "Edit"}
            title={editing ? "Cancel edit" : "Edit"}
            style={{
              position: "absolute", right: 0, top: 0, height: "100%",
              padding: "0 10px", background: "none", border: "none",
              color: editing ? C.textMuted : C.amber, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {editing ? <X size={14} /> : <Pencil size={13} />}
          </button>
        )}
      </div>
    </div>
  );
}
