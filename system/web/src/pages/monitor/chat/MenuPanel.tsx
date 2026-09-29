import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

// Hairline between two groups of MenuItems.
export function MenuDivider() {
  return (
    <div
      role="separator"
      style={{ height: 1, margin: "5px 4px", background: "var(--lm-border)" }}
    />
  );
}

export function MenuItem({
  icon: Icon, label, hint, trailing, active, onClick,
}: {
  icon: LucideIcon;
  label: string;
  hint?: string;
  trailing?: ReactNode;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      role="menuitem"
      onClick={onClick}
      style={{
        display: "flex", alignItems: "center", gap: 9, width: "100%",
        padding: "7px 9px", borderRadius: 8,
        background: active ? "color-mix(in srgb, var(--lm-text) 7%, transparent)" : "transparent",
        border: "none", cursor: "pointer", textAlign: "left",
        color: "var(--lm-text)", transition: "background 0.12s",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 9%, transparent)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = active ? "color-mix(in srgb, var(--lm-text) 7%, transparent)" : "transparent"; }}
    >
      <Icon size={15} style={{ color: "var(--lm-amber)", flexShrink: 0, alignSelf: "flex-start", marginTop: 1 }} />
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 12.5, lineHeight: 1.3 }}>{label}</span>
        {hint && <span style={{ display: "block", fontSize: 10, color: "var(--lm-text-muted)", marginTop: 1 }}>{hint}</span>}
      </span>
      {trailing}
    </button>
  );
}
