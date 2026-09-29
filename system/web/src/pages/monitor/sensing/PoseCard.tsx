import { useState } from "react";
import { S } from "../styles";
import { StatPill } from "../components";
import { poseSnapshotUrl } from "./visionApi";
import { CardHeader, Pill } from "./CardHeader";
import { fmtAgo, posePillStatus, poseDotColor, riskName } from "./format";
import type { Perception } from "./types";

export function PoseCard({ pose }: { pose: Perception }) {
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  return <>
      {pose ? (() => {
        const status = posePillStatus(pose);
        const samples = [...(pose.samples ?? [])].reverse();
        // Prefer running over summary: summary only exists for one tick at the window boundary.
        const live = pose.running ?? pose.summary;
        const threshold = pose.bad_ratio_threshold ?? 0.6;
        const minSamples = pose.window_min_samples ?? 3;
        const samplesNow = pose.samples_in_buffer ?? 0;
        const winDurMin = Math.round((pose.window_duration_s ?? 600) / 60);
        const winAgeMin = Math.round((pose.window_age_s ?? 0) / 60);
        const wouldFire = !!(live && samplesNow >= minSamples && live.bad_ratio >= threshold);
        return (
          <div style={S.card}>
            <CardHeader
              label="Pose / Posture"
              pill={<Pill text={status.text} color={status.color} />}
            />
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
              <StatPill label="Samples"     value={`${samplesNow} (min ${minSamples})`} color={samplesNow >= minSamples ? "var(--lm-green)" : undefined} />
              <StatPill label="Window"      value={`${winAgeMin}m / ${winDurMin}m`} color={pose.window_complete ? "var(--lm-green)" : undefined} />
              <StatPill label="Last"        value={fmtAgo(pose.seconds_since_sample)} />
              <StatPill label="Last score"  value={`${pose.ergo_score ?? "—"} (${riskName(pose.ergo_risk_level)})`} />
              {live ? (
                <>
                  <StatPill label="Bad" value={`${Math.round(live.bad_ratio * 100)}% (${live.bad_samples}/${live.samples})`} color={live.bad_ratio >= threshold ? "var(--lm-red)" : "var(--lm-green)"} />
                  <StatPill label="Dominant" value={live.dominant_region || "—"} />
                  <StatPill label="Will fire" value={wouldFire ? "YES — waiting window" : "no"} color={wouldFire ? "var(--lm-red)" : "var(--lm-text-muted)"} />
                </>
              ) : null}
            </div>
            <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 4, letterSpacing: "0.05em", textTransform: "uppercase" }}>
              Samples (newest first) — sub-score@angle° raw from perception-service; L = left, R = right
            </div>
            {samples.length === 0 ? (
              <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No samples yet</span>
            ) : (() => {
              const cols = "104px 92px 42px 56px 90px 90px 90px 90px 90px 90px 64px";
              const fmtCell = (sub: number | undefined, angle: number | undefined): string => {
                if (sub == null) return "-";
                if (angle == null) return `${sub}`;
                return `${sub}@${Math.round(angle)}°`;
              };
              return (
                <div style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11, lineHeight: 1.6, overflowX: "auto" }}>
                  <div style={{ display: "grid", gridTemplateColumns: cols, gap: 6, color: "var(--lm-text-muted)", paddingBottom: 4, borderBottom: "1px solid var(--lm-text-muted)33", whiteSpace: "nowrap" }}>
                    <div>img</div>
                    <div>time</div><div>score</div><div>risk</div>
                    <div>neck</div><div>trunk</div>
                    <div>L u-arm</div><div>R u-arm</div>
                    <div>L l-arm</div><div>R l-arm</div>
                    <div>wrist L/R</div>
                  </div>
                  {samples.map((s, idx) => {
                    const d = new Date(s.ts * 1000);
                    const hh = String(d.getHours()).padStart(2, "0");
                    const mm = String(d.getMinutes()).padStart(2, "0");
                    const ss = String(d.getSeconds()).padStart(2, "0");
                    const L = s.left?.body_scores ?? {};
                    const R = s.right?.body_scores ?? {};
                    const snapUrl = poseSnapshotUrl(s.ts);
                    return (
                      <div key={`${s.ts}-${idx}`} style={{ display: "grid", gridTemplateColumns: cols, gap: 6, whiteSpace: "nowrap", alignItems: "center", paddingTop: 2, paddingBottom: 2 }}>
                        <img
                          src={snapUrl}
                          alt={`pose ${hh}:${mm}:${ss}`}
                          loading="lazy"
                          onClick={() => setLightboxUrl(snapUrl)}
                          title="Click to enlarge"
                          style={{ width: 100, height: "auto", borderRadius: 3, border: "1px solid var(--lm-text-muted)33", background: "var(--lm-text-muted)15", display: "block", cursor: "pointer" }}
                          onError={(e) => { (e.currentTarget.style.visibility = "hidden"); }}
                        />
                        <div>{`${hh}:${mm}:${ss}`}</div>
                        <div>{s.score}</div>
                        <div style={{ color: poseDotColor(s) }}>{riskName(s.risk_level)}</div>
                        <div>{fmtCell(L.neck, L.neck_angle)}</div>
                        <div>{fmtCell(L.trunk, L.trunk_angle)}</div>
                        <div>{fmtCell(L.upper_arm, L.upper_arm_angle)}</div>
                        <div>{fmtCell(R.upper_arm, R.upper_arm_angle)}</div>
                        <div>{fmtCell(L.lower_arm, L.lower_arm_angle)}</div>
                        <div>{fmtCell(R.lower_arm, R.lower_arm_angle)}</div>
                        <div>{`${L.wrist ?? "-"}/${R.wrist ?? "-"}`}</div>
                      </div>
                    );
                  })}
                </div>
              );
            })()}
          </div>
        );
      })() : null}
      {lightboxUrl && (
        <div
          onClick={() => setLightboxUrl(null)}
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: "fixed", inset: 0, zIndex: 9999,
            background: "rgba(0,0,0,0.8)", backdropFilter: "blur(4px)",
            display: "flex", alignItems: "center", justifyContent: "center",
            cursor: "pointer",
          }}
        >
          <button
            onClick={() => setLightboxUrl(null)}
            style={{
              position: "absolute", top: 16, right: 16,
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", fontSize: 20, width: 36, height: 36,
              borderRadius: "50%", cursor: "pointer",
            }}
          >
            ✕
          </button>
          <img
            src={lightboxUrl}
            onClick={(e) => e.stopPropagation()}
            style={{ width: "85vw", height: "85vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
          />
        </div>
      )}
  </>;
}
