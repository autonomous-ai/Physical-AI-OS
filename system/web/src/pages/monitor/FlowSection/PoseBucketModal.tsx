import { useEffect, useState } from "react";
import { HW } from "../types";

type BucketSample = {
  ts: number;
  score: number;
  risk_level: number;
  filename: string;
  left?: Record<string, unknown>;
  right?: Record<string, unknown>;
};

type BucketSummary = {
  bad_ratio?: number;
  samples?: number;
  bad_samples?: number;
  window_min?: number;
  dominant_region?: string;
  dominant_count?: number;
  latest_score?: number;
  latest_risk_level?: number;
} & Record<string, unknown>;

type BucketPayload = {
  bucket_id?: string;
  window_start_ts?: number;
  window_end_ts?: number;
  kept?: boolean;
  summary?: BucketSummary;
  samples?: BucketSample[];
  worst_snapshots?: string[];
};

const RISK_COLOR: Record<number, string> = {
  4: "var(--lm-red)",
  3: "var(--lm-amber)",
  2: "var(--lm-teal)",
  1: "var(--lm-green)",
};
const RISK_LABEL: Record<number, string> = { 4: "high", 3: "med", 2: "low", 1: "neg" };

function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span
      style={{
        fontSize: 9,
        padding: "1px 6px",
        borderRadius: 3,
        background: `${color}22`,
        color,
        fontWeight: 700,
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function bodyScore(side: Record<string, unknown> | undefined, region: string): string {
  const bs = (side?.body_scores ?? {}) as Record<string, unknown>;
  const v = bs[region];
  return typeof v === "number" ? String(v) : "-";
}

function bodyAngle(side: Record<string, unknown> | undefined, key: string): string {
  const bs = (side?.body_scores ?? {}) as Record<string, unknown>;
  const v = bs[key];
  return typeof v === "number" ? `${v.toFixed(0)}°` : "";
}

export function PoseBucketModal({
  bucketId,
  onClose,
}: {
  bucketId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<BucketPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    (async () => {
      try {
        setLoading(true);
        setError(null);
        const r = await fetch(`${HW}/sensing/pose-bucket/${encodeURIComponent(bucketId)}`, {
          signal: ac.signal,
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as BucketPayload;
        setData(j);
      } catch (e) {
        if ((e as { name?: string })?.name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
    return () => ac.abort();
  }, [bucketId]);

  const samples = [...(data?.samples ?? [])].reverse();
  const worstSet = new Set(data?.worst_snapshots ?? []);
  const startLocal = data?.window_start_ts
    ? new Date(data.window_start_ts * 1000).toLocaleString()
    : "";
  const endLocal = data?.window_end_ts
    ? new Date(data.window_end_ts * 1000).toLocaleString()
    : "";
  const cols = "100px 90px 50px 60px 50px 50px 70px 70px 70px 70px 70px";

  return (
    <div
      id="FLOW_POSE_BUCKET_MODAL_OVERLAY" data-region="FLOW_POSE_BUCKET_MODAL_OVERLAY"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        background: "rgba(0,0,0,0.72)",
        backdropFilter: "blur(4px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        id="FLOW_POSE_BUCKET_MODAL_PANEL" data-region="FLOW_POSE_BUCKET_MODAL_PANEL"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--lm-card)",
          border: "1px solid var(--lm-border)",
          borderRadius: 16,
          padding: 20,
          width: "94vw",
          maxWidth: 1100,
          maxHeight: "88vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 12,
            flexShrink: 0,
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-text)" }}>
              🪑 Pose bucket · {bucketId}
            </span>
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontFamily: "monospace" }}>
              {startLocal} → {endLocal}
            </span>
          </div>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              color: "var(--lm-text-muted)",
              cursor: "pointer",
              fontSize: 16,
            }}
          >
            ✕
          </button>
        </div>

        {loading && (
          <div style={{ padding: 24, textAlign: "center", color: "var(--lm-text-muted)", fontSize: 12 }}>
            Loading bucket…
          </div>
        )}
        {error && (
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: "rgba(248,113,113,0.12)",
              color: "var(--lm-red)",
              border: "1px solid rgba(248,113,113,0.35)",
              fontSize: 12,
            }}
          >
            {error}
          </div>
        )}
        {!loading && !error && data && (
          <>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
              <Pill
                text={`Bad ${Math.round((data.summary?.bad_ratio ?? 0) * 100)}% (${data.summary?.bad_samples ?? 0}/${data.summary?.samples ?? 0})`}
                color={
                  (data.summary?.bad_ratio ?? 0) >= 0.6
                    ? "var(--lm-red)"
                    : (data.summary?.bad_ratio ?? 0) >= 0.3
                      ? "var(--lm-amber)"
                      : "var(--lm-green)"
                }
              />
              <Pill text={`Window ${data.summary?.window_min ?? "?"}m`} color="var(--lm-text-muted)" />
              {data.summary?.dominant_region && (
                <Pill
                  text={`Dominant ${data.summary.dominant_region} (${data.summary.dominant_count ?? 0})`}
                  color="var(--lm-purple)"
                />
              )}
              {typeof data.summary?.latest_score === "number" && (
                <Pill
                  text={`Last score ${data.summary.latest_score}`}
                  color={RISK_COLOR[data.summary.latest_risk_level ?? 0] ?? "var(--lm-text-muted)"}
                />
              )}
            </div>

            <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: cols,
                  gap: 6,
                  fontFamily: "monospace",
                  fontSize: 10,
                  color: "var(--lm-text-muted)",
                  fontWeight: 700,
                  paddingBottom: 4,
                  borderBottom: "1px solid var(--lm-border)",
                  marginBottom: 6,
                  whiteSpace: "nowrap",
                }}
              >
                <span>img</span>
                <span>time</span>
                <span>score</span>
                <span>risk</span>
                <span>neck</span>
                <span>trunk</span>
                <span>L u-arm</span>
                <span>R u-arm</span>
                <span>L l-arm</span>
                <span>R l-arm</span>
                <span>wrist L/R</span>
              </div>
              {samples.map((s, i) => {
                const d = new Date(s.ts * 1000);
                const hh = String(d.getHours()).padStart(2, "0");
                const mm = String(d.getMinutes()).padStart(2, "0");
                const ss = String(d.getSeconds()).padStart(2, "0");
                const url = `${HW}/sensing/pose-bucket/${encodeURIComponent(bucketId)}/img/${encodeURIComponent(s.filename)}`;
                const isWorst = worstSet.has(s.filename);
                const riskCol = RISK_COLOR[s.risk_level] ?? "var(--lm-text-muted)";
                return (
                  <div
                    key={`${s.ts}-${i}`}
                    style={{
                      display: "grid",
                      gridTemplateColumns: cols,
                      gap: 6,
                      alignItems: "center",
                      paddingTop: 2,
                      paddingBottom: 2,
                      whiteSpace: "nowrap",
                      fontFamily: "monospace",
                      fontSize: 10,
                      background: isWorst ? "rgba(248,113,113,0.10)" : "transparent",
                      borderRadius: 4,
                    }}
                  >
                    <img
                      src={url}
                      alt={`${hh}:${mm}:${ss}`}
                      loading="lazy"
                      onClick={() => setLightboxUrl(url)}
                      style={{
                        width: 100,
                        height: "auto",
                        borderRadius: 3,
                        border: isWorst
                          ? "2px solid var(--lm-red)"
                          : "1px solid var(--lm-text-muted)33",
                        cursor: "pointer",
                        display: "block",
                      }}
                    />
                    <span>{`${hh}:${mm}:${ss}`}{isWorst ? " ⭐" : ""}</span>
                    <span style={{ color: "var(--lm-text)" }}>{s.score}</span>
                    <span style={{ color: riskCol, fontWeight: 700 }}>
                      {RISK_LABEL[s.risk_level] ?? "?"}
                    </span>
                    <span>{bodyScore(s.left, "neck")} {bodyAngle(s.left, "neck_angle")}</span>
                    <span>{bodyScore(s.left, "trunk")} {bodyAngle(s.left, "trunk_angle")}</span>
                    <span>{bodyScore(s.left, "upper_arm")} {bodyAngle(s.left, "upper_arm_angle")}</span>
                    <span>{bodyScore(s.right, "upper_arm")} {bodyAngle(s.right, "upper_arm_angle")}</span>
                    <span>{bodyScore(s.left, "lower_arm")}</span>
                    <span>{bodyScore(s.right, "lower_arm")}</span>
                    <span>{bodyScore(s.left, "wrist")}/{bodyScore(s.right, "wrist")}</span>
                  </div>
                );
              })}
              {samples.length === 0 && (
                <div style={{ padding: 16, color: "var(--lm-text-muted)", fontSize: 12 }}>
                  No samples in this bucket.
                </div>
              )}
            </div>
          </>
        )}

        {lightboxUrl && (
          <div
            onClick={() => setLightboxUrl(null)}
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 200,
              background: "rgba(0,0,0,0.85)",
              backdropFilter: "blur(4px)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
            }}
          >
            <img
              src={lightboxUrl}
              onClick={(e) => e.stopPropagation()}
              style={{ width: "85vw", height: "85vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
