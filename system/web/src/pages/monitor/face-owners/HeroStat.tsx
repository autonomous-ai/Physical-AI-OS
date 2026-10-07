import type { ReactNode } from "react";

// HeroStat is a compact tile in the page hero showing one headline number (Enrolled / Here now / Unknown voices / Unknown faces).
export function HeroStat({ icon, label, value, tone = "neutral", pulse }: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  tone?: "amber" | "teal" | "purple" | "red" | "neutral";
  pulse?: boolean;
}) {
  const color =
    tone === "amber" ? "var(--lm-amber)" :
    tone === "teal" ? "var(--lm-teal)" :
    tone === "purple" ? "var(--lm-purple)" :
    tone === "red" ? "var(--lm-red)" :
    "var(--lm-text)";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12, minWidth: 0,
      padding: "16px", borderRadius: 10,
      background: "color-mix(in srgb, var(--lm-card) 70%, transparent)",
      border: "1px solid var(--lm-border)",
      backdropFilter: "blur(4px)",
    }}>
      <span className={pulse ? "lm-pulse" : undefined} style={{
        flexShrink: 0, width: 30, height: 30, borderRadius: 9,
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        background: `color-mix(in srgb, ${color} 14%, transparent)`,
        color,
        boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 30%, transparent)`,
        overflow: "hidden",
      }} aria-hidden>{icon}</span>
      <div style={{ minWidth: 0 }}>
        <div style={{
          fontSize: 24, fontWeight: 800, color, lineHeight: 1.3, letterSpacing: "-0.4px",
          overflowWrap: "anywhere",
        }}>
          {value}
        </div>
        <div style={{ fontSize: 12, color: "var(--lm-text-muted)", marginTop: 4 }}>
          {label}
        </div>
      </div>
    </div>
  );
}
