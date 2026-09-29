import type { ReactNode } from "react";

// Centered icon + message for empty lists.
export function EmptyState({ icon, text }: { icon: ReactNode; text: string }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      gap: 8, padding: "28px 12px", textAlign: "center",
    }}>
      <span style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        width: 38, height: 38, borderRadius: "50%",
        background: "var(--lm-surface)",
        border: "1px solid var(--lm-border)",
        color: "var(--lm-text-muted)",
        opacity: 0.7,
      }} aria-hidden>{icon}</span>
      <span style={{ fontSize: 11.5, color: "var(--lm-text-muted)", maxWidth: 220, lineHeight: 1.5 }}>
        {text}
      </span>
    </div>
  );
}
