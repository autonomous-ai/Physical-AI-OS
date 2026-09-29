import type { CSSProperties } from "react";
import { ScanFace, UserX } from "lucide-react";
import { CardLabel } from "../components";
import { EmptyState } from "./EmptyState";
import { fmtIsoAgo } from "./format";
import { FAMILIAR_VISIT_THRESHOLD } from "./types";
import type { FaceStrangerStat } from "./types";

// Unknown Faces: visit stats per stranger_id.
export function UnknownFacesCard({
  faceStrangers, faceStrangersError, monCard, cardHeader,
}: {
  faceStrangers: FaceStrangerStat[] | null;
  faceStrangersError: boolean;
  monCard: CSSProperties;
  cardHeader: CSSProperties;
}) {
  return (
    <div className="lm-mon-card" style={monCard}>
      <div style={cardHeader}>
        <CardLabel icon={<ScanFace size={13} />} text="Unknown Faces" />
        <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
          {faceStrangers ? `${faceStrangers.length} stranger${faceStrangers.length !== 1 ? "s" : ""}` : ""}
        </span>
      </div>

      {faceStrangersError && (
        <EmptyState icon={<UserX size={18} />} text="Face stranger stats unavailable (sensing not started?)" />
      )}

      {!faceStrangersError && faceStrangers && faceStrangers.length === 0 && (
        <EmptyState icon={<ScanFace size={18} />} text="No unknown faces tracked yet." />
      )}

      {!faceStrangersError && faceStrangers && faceStrangers.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }} className="lm-hide-scroll lm-scroll-fade">
          {faceStrangers.map((s) => {
            const familiar = s.count >= FAMILIAR_VISIT_THRESHOLD;
            const accent = familiar ? "var(--lm-amber)" : "var(--lm-red)";
            const accentBg = familiar ? "var(--lm-amber-dim)" : "var(--lm-red-dim)";
            return (
              <div key={s.stranger_id} className="lm-u-interactive" style={{
                padding: "8px 12px",
                borderRadius: 8,
                background: "var(--lm-surface)",
                cursor: "default",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4, gap: 8, flexWrap: "wrap" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span style={{
                      fontSize: 12,
                      fontWeight: 700,
                      color: accent,
                      fontFamily: "monospace",
                    }}>
                      {s.stranger_id}
                    </span>
                    <span style={{
                      fontSize: 10,
                      padding: "1px 6px",
                      borderRadius: 4,
                      background: accentBg,
                      color: accent,
                      fontWeight: 600,
                    }}>
                      {s.count} visit{s.count !== 1 ? "s" : ""}
                    </span>
                    {familiar && (
                      <span
                        className="lm-pulse"
                        title={`Visit count ≥ ${FAMILIAR_VISIT_THRESHOLD} (familiar threshold). The device fires the enroll prompt only on the 1→${FAMILIAR_VISIT_THRESHOLD} transition — strangers whose count was already past the threshold before the trigger code was deployed will NOT have been prompted.`}
                        style={{
                          fontSize: 10,
                          padding: "1px 6px",
                          borderRadius: 4,
                          background: "var(--lm-amber-dim)",
                          color: "var(--lm-amber)",
                          fontWeight: 700,
                          letterSpacing: 0.3,
                        }}
                      >
                        ● FAMILIAR
                      </span>
                    )}
                  </div>
                  <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                    last {s.last_seen ? fmtIsoAgo(s.last_seen) : "?"}
                  </span>
                </div>
                <div style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                  first seen {s.first_seen ? fmtIsoAgo(s.first_seen) : "?"}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
