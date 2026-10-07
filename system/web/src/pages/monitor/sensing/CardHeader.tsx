import { S } from "../styles";

// Status pill used in card headers.
export function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span style={{
      fontSize: 10, padding: "2px 7px", borderRadius: 4,
      background: `color-mix(in srgb, ${color} 15%, transparent)`,
      color,
      border: `1px solid color-mix(in srgb, ${color} 33%, transparent)`,
      flexShrink: 0,
      fontWeight: 700, letterSpacing: "0.05em",
      textTransform: "uppercase",
    }}>{text}</span>
  );
}

// CardHeader is the uppercase title + pill row shared by every Sensing card.
export function CardHeader({ label, pill }: { label: string; pill?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
      <div style={{ ...S.cardLabel, marginBottom: 0 }}>{label}</div>
      {pill}
    </div>
  );
}
