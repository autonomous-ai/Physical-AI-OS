import type { CSSProperties } from "react";
import { UserCheck, UserX } from "lucide-react";
import { EmptyState } from "./EmptyState";
import { fmtCountdown } from "./format";
import type { CooldownEntry } from "./types";

// Face Recognition Cooldowns.
export function CooldownsCard({
  allCooldownEntries, cdError, hasActiveCooldowns, resetting, onReset, monCard, cardHeader,
}: {
  allCooldownEntries: CooldownEntry[];
  cdError: boolean;
  hasActiveCooldowns: boolean;
  resetting: boolean;
  onReset: () => void;
  monCard: CSSProperties;
  cardHeader: CSSProperties;
}) {
  return (
    <details className="lm-mon-card lm-users-cooldowns" style={monCard}>
      <summary>
        <span>Face recognition cooldowns</span>
        <span style={{ color: cdError ? "var(--lm-red)" : "var(--lm-text-muted)", fontSize: 13, fontWeight: 400 }}>
          {cdError ? "Status unavailable" : `${allCooldownEntries.filter((entry) => entry.cooldown_remaining > 0).length} active`}
        </span>
      </summary>
      <div style={cardHeader}>
        <h2 className="lm-users-card-title"><UserCheck size={17} aria-hidden />Event timing</h2>
        <button
          onClick={onReset}
          disabled={resetting || !hasActiveCooldowns}
          className="lm-u-btn"
          style={{
            fontSize: 12,
            padding: "7px 12px",
            borderRadius: 6,
            fontWeight: 600,
            ...(hasActiveCooldowns
              ? { background: "var(--lm-amber-dim)", color: "var(--lm-amber)", borderColor: "var(--lm-amber)" }
              : { color: "var(--lm-text-muted)" }),
            opacity: resetting ? 0.5 : 1,
          }}
        >
          {resetting ? "Resetting..." : "Reset Cooldowns"}
        </button>
      </div>

      {cdError && (
        <EmptyState icon={<UserX size={18} />} text="Cooldown info unavailable" />
      )}

      {!cdError && allCooldownEntries.length === 0 && (
        <EmptyState icon={<UserCheck size={18} />} text="No faces currently tracked" />
      )}

      {!cdError && allCooldownEntries.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }} className="lm-users-list">
          {allCooldownEntries.map((entry) => {
            const pct = entry.cooldown_total > 0
              ? (entry.cooldown_remaining / entry.cooldown_total) * 100
              : 0;
            const kindColor =
              entry.kind === "stranger" ? "var(--lm-red)"
              : "var(--lm-blue)";
            return (
              <div key={`${entry.kind}-${entry.person_id}`} className="lm-u-interactive" style={{
                padding: "8px 12px",
                borderRadius: 8,
                background: "var(--lm-surface)",
                cursor: "default",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 6 }}>
                  <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
                    <span style={{
                      fontSize: 13, overflowWrap: "anywhere",
                      fontWeight: 600,
                      color: kindColor,
                      textTransform: "capitalize",
                    }}>
                      {entry.person_id}
                    </span>
                    <span style={{
                      fontSize: 12,
                      padding: "1px 6px",
                      borderRadius: 4,
                      background: `color-mix(in srgb, ${kindColor} 14%, transparent)`,
                      color: kindColor,
                      fontWeight: 600,
                    }}>
                      {entry.kind}
                    </span>
                  </div>
                  <span style={{
                    fontSize: 12,
                    fontWeight: 600,
                    fontFamily: "monospace",
                    color: entry.cooldown_remaining > 0 ? "var(--lm-text)" : "var(--lm-green)",
                  }}>
                    {fmtCountdown(entry.cooldown_remaining)}
                  </span>
                </div>
                <div style={{
                  height: 4,
                  borderRadius: 2,
                  background: "var(--lm-border)",
                  overflow: "hidden",
                }}>
                  <div
                    className={entry.cooldown_remaining > 0 ? "lm-cooldown-fill" : undefined}
                    style={{
                      height: "100%",
                      width: `${pct}%`,
                      borderRadius: 2,
                      background: kindColor,
                      transition: "width 1.5s linear",
                    }}
                  />
                </div>
                <div style={{ fontSize: 12, color: "var(--lm-text-muted)", marginTop: 4 }}>
                  seen {Math.round(entry.last_seen_ago)}s ago · next event in {fmtCountdown(entry.cooldown_remaining)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </details>
  );
}
