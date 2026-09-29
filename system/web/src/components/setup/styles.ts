import type { CSSProperties } from "react";

export const C = {
  bg:        "var(--lm-bg)",
  sidebar:   "var(--lm-sidebar)",
  card:      "var(--lm-card)",
  surface:   "var(--lm-surface)",
  border:    "var(--lm-border)",
  amber:     "var(--lm-amber)",
  amberDim:  "var(--lm-amber-dim)",
  text:      "var(--lm-text)",
  textDim:   "var(--lm-text-dim)",
  textMuted: "var(--lm-text-muted)",
  red:       "var(--lm-red)",
  green:     "var(--lm-green)",
  yellow:    "var(--lm-yellow)",
};

export const FIELD_GAP = 14;

// Frontend-only policy; the backend accepts any non-empty value.
export const ADMIN_PASSWORD_MIN = 4;

export const LABEL_STYLE: CSSProperties = {
  display: "block",
  fontSize: 13,
  fontWeight: 500,
  letterSpacing: "0.01em",
  color: C.textDim,
  marginBottom: 6,
};

export const INPUT_STYLE: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: "10px 13px",
  fontSize: 14,
  color: C.text,
  caretColor: C.amber,
  outline: "none",
  transition: "border-color 0.15s, box-shadow 0.15s",
};

export const INPUT_READONLY_STYLE: CSSProperties = {
  background: C.bg,
  border: "1px solid transparent",
  color: C.textDim,
  caretColor: "transparent",
  cursor: "default",
};

export const INPUT_FOCUS_SHADOW = "0 0 0 3px var(--lm-amber-glow)";

export const INPUT_ERROR_SHADOW = "0 0 0 3px var(--lm-red-glow)";

export const INPUT_SUCCESS_SHADOW = "0 0 0 3px var(--lm-green-glow)";

export const INPUT_PAD_ONE_ICON = "10px 42px 10px 13px";
export const INPUT_PAD_TWO_ICONS = "10px 70px 10px 13px";
