import { S } from "../styles";

// Status pill used in card headers.
export function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span style={{
      fontSize: 10, padding: "2px 7px", borderRadius: 4,
      background: `${color}26`,
      color,
      border: `1px solid ${color}55`,
      fontWeight: 700, letterSpacing: "0.05em",
      textTransform: "uppercase",
    }}>{text}</span>
  );
}

// CardHeader is the uppercase title + pill row shared by every Sensing card.
export function CardHeader({ label, pill }: { label: string; pill?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
      <div style={S.cardLabel}>{label}</div>
      {pill}
    </div>
  );
}
