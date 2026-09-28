import { useCallback, useEffect, useRef, useState } from "react";
import { usePolling } from "../../hooks/usePolling";
import { S } from "./styles";
import { hwUrl } from "@/lib/api";
import { HW } from "./types";

interface TrackStatus {
  tracking: boolean;
  target: string | null;
  bbox: number[] | null;
  confidence: number | null;
}

export function CameraSection({
  displayTs: _displayTs,
}: {
  displayTs: number;
}) {
  const [snapTs, setSnapTs] = useState(() => Date.now());
  const [snapError, setSnapError] = useState(false);
  const [streamError, setStreamError] = useState(false);
  // Bumped to remount the MJPEG <img> with a fresh connection.
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [cameraDisabled, setCameraDisabled] = useState(false);
  const [manualOverride, setManualOverride] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [zoom, setZoom] = useState(1.0);
  const [mode, setMode] = useState<{ w: number | null; h: number | null; fps: number | null }>({
    w: null, h: null, fps: null,
  });
  const zoomTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [track, setTrack] = useState<TrackStatus>({ tracking: false, target: null, bbox: null, confidence: null });
  const [trackTarget, setTrackTarget] = useState("object");
  const [trackBbox, setTrackBbox] = useState("");

  const [streamActive, setStreamActive] = useState(!document.hidden);
  useEffect(() => {
    const onVis = () => setStreamActive(!document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  useEffect(() => {
    if (!cameraDisabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- this reacts to a DEVICE state transition (the poll seeing the camera come back), not to a prop that could be derived during render. Bumping streamEpoch is what forces the MJPEG <img> to remount with a fresh connection; there is no render-time equivalent.
      setStreamError(false);
      setSnapError(false);
      setStreamEpoch((e) => e + 1);
      setSnapTs(Date.now());
    }
  }, [cameraDisabled]);

  // Auto-retry a failed stream while enabled: HAL may not have delivered the first frame yet.
  useEffect(() => {
    if (cameraDisabled || !streamError || !streamActive) return;
    const t = setTimeout(() => {
      setStreamError(false);
      setStreamEpoch((e) => e + 1);
    }, 1200);
    return () => clearTimeout(t);
  }, [cameraDisabled, streamError, streamActive]);

  const fetchTrackStatus = useCallback(async () => {
    try {
      const r = await fetch(`${HW}/servo/track`).then((x) => x.json());
      setTrack({ tracking: !!r.tracking, target: r.target, bbox: r.bbox, confidence: r.confidence ?? null });
    } catch {
      // Keep the last tracking state on a transient failure.
    }
  }, []);

  usePolling(async (signal) => {
    const r = await fetch(`${HW}/camera`, { signal }).then((x) => x.json());
    setCameraDisabled(!!r.disabled);
    setManualOverride(!!r.manual_override);
    setMode({
      w: typeof r.width === "number" ? r.width : null,
      h: typeof r.height === "number" ? r.height : null,
      fps: typeof r.fps === "number" ? r.fps : null,
    });
    // Skip server zoom while user is sliding (timer pending) to avoid jitter.
    if (!zoomTimer.current && typeof r.zoom === "number") setZoom(r.zoom);
  }, 3000);

  usePolling(async (signal) => {
    const r = await fetch(`${HW}/servo/track`, { signal }).then((x) => x.json());
    setTrack({ tracking: !!r.tracking, target: r.target, bbox: r.bbox, confidence: r.confidence ?? null });
  }, 3000);

  const applyZoom = (z: number) => {
    setZoom(z);
    if (zoomTimer.current) clearTimeout(zoomTimer.current);
    zoomTimer.current = setTimeout(() => {
      zoomTimer.current = null;
      fetch(`${HW}/camera/zoom`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ zoom: z }),
      }).catch(() => {});
    }, 200);
  };

  const toggleCamera = async () => {
    setToggling(true);
    try {
      await fetch(`${HW}/camera/${cameraDisabled ? "enable" : "disable"}`, { method: "POST" });
      setCameraDisabled(!cameraDisabled);
    } catch {
      // The next poll is the source of truth for the camera state.
    }
    setToggling(false);
  };

  const startTracking = async () => {
    const labels = trackTarget.split(",").map((s) => s.trim()).filter(Boolean);
    const body: Record<string, unknown> = {};
    if (labels.length === 1) body.target = labels[0];
    else if (labels.length > 1) body.target = labels;
    if (trackBbox.trim()) {
      const parts = trackBbox.split(",").map((s) => parseInt(s.trim(), 10));
      if (parts.length === 4 && !parts.some(isNaN)) {
        body.bbox = parts;
      }
    }
    if (!body.target && !body.bbox) return;
    try {
      const r = await fetch(`${HW}/servo/track`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((x) => x.json());
      setTrack({ tracking: !!r.tracking, target: r.target, bbox: r.bbox, confidence: r.confidence ?? null });
    } catch {
      // Start failed: the status poll reports the real state.
    }
  };

  const stopTracking = async () => {
    try {
      await fetch(`${HW}/servo/track/stop`, { method: "POST" });
      setTrack({ tracking: false, target: null, bbox: null, confidence: null });
    } catch {
      // Stop failed: keep the tracking state as-is.
    }
  };

  const refreshSnapshot = () => {
    setSnapError(false);
    setSnapTs(Date.now());
  };

  const statusText = cameraDisabled
    ? (manualOverride ? "Disabled by you" : "Auto-disabled (scene/emotion)")
    : "Streaming";
  const statusColor = cameraDisabled ? "var(--lm-red)" : "var(--lm-green)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="lm-grid-2">

        <div style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, gap: 10, flexWrap: "wrap" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={S.cardLabel}>Live Stream</div>
              <span style={{
                fontSize: 10, padding: "2px 7px", borderRadius: 4,
                background: track.tracking ? "rgba(52,211,153,0.15)" : (cameraDisabled ? "rgba(248,113,113,0.15)" : "rgba(245,158,11,0.15)"),
                color: track.tracking ? "var(--lm-green)" : (cameraDisabled ? "var(--lm-red)" : "var(--lm-amber)"),
                fontWeight: 700, letterSpacing: "0.05em",
              }}>
                {cameraDisabled ? "OFF" : track.tracking ? "TRACK" : "LIVE"}
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {mode.w && mode.h && (
                <span
                  title="Actual capture mode negotiated with the camera"
                  style={{
                    fontSize: 10, padding: "2px 7px", borderRadius: 4,
                    background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                    color: "var(--lm-text-dim)", fontFamily: "monospace", fontWeight: 600,
                  }}
                >
                  {mode.w}×{mode.h}{mode.fps ? ` @ ${mode.fps.toFixed(0)}fps` : ""}
                </span>
              )}
              <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>{statusText}</span>
              <button
                onClick={toggleCamera}
                disabled={toggling}
                style={{
                  padding: "4px 12px", borderRadius: 6, fontSize: 11, fontWeight: 600,
                  cursor: toggling ? "wait" : "pointer",
                  background: cameraDisabled ? "rgba(52,211,153,0.1)" : "rgba(248,113,113,0.1)",
                  border: `1px solid ${cameraDisabled ? "rgba(52,211,153,0.3)" : "rgba(248,113,113,0.3)"}`,
                  color: cameraDisabled ? "var(--lm-green)" : "var(--lm-red)",
                }}
              >
                {toggling ? "…" : cameraDisabled ? "Enable" : "Disable"}
              </button>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span style={{ fontSize: 11, color: "var(--lm-text-dim)", minWidth: 36 }}>Zoom</span>
            <input
              type="range"
              min={1}
              max={5}
              step={0.1}
              value={zoom}
              onChange={(e) => applyZoom(Number(e.target.value))}
              disabled={cameraDisabled}
              style={{ flex: 1, accentColor: "var(--lm-amber)" }}
              title={zoom > 1 ? "Zoom narrows FOV — sensing will only see center" : ""}
            />
            <span style={{
              fontSize: 11, color: zoom > 1 ? "var(--lm-amber)" : "var(--lm-text-muted)",
              minWidth: 32, fontFamily: "monospace", fontWeight: 600,
            }}>
              {zoom.toFixed(1)}×
            </span>
            <button
              onClick={() => applyZoom(1.0)}
              disabled={cameraDisabled || zoom === 1.0}
              style={{
                fontSize: 10, padding: "2px 8px", borderRadius: 4,
                background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                color: "var(--lm-text-dim)",
                cursor: (cameraDisabled || zoom === 1.0) ? "not-allowed" : "pointer",
                opacity: (cameraDisabled || zoom === 1.0) ? 0.4 : 1,
              }}
              title="Reset to 1.0x (full FOV)"
            >Reset</button>
          </div>

          <div style={{ position: "relative" }}>
            <MediaFrame
              disabled={cameraDisabled}
              paused={!streamActive}
              error={streamError}
              disabledText="Camera disabled"
              pausedText="Stream paused (tab hidden)"
              errorText="Stream unavailable"
              highlight={track.tracking}
            >
              <img
                key={streamEpoch}
                src={hwUrl(`/camera/stream?e=${streamEpoch}`)}
                alt="camera"
                style={mediaImgStyle(track.tracking)}
                onError={() => setStreamError(true)}
                onLoad={() => setStreamError(false)}
              />
            </MediaFrame>

            <div style={{
              position: "absolute",
              bottom: 8,
              right: 8,
              width: 130,
              borderRadius: 6,
              border: `1px solid ${statusColor === "var(--lm-green)" ? "rgba(52,211,153,0.4)" : "var(--lm-border)"}`,
              background: "var(--lm-card)",
              boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
              padding: 6,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 4 }}>
                <span style={{
                  fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
                  color: "var(--lm-text-dim)", textTransform: "uppercase",
                }}>Snapshot</span>
                <div style={{ display: "flex", gap: 3 }}>
                  <a
                    href={cameraDisabled ? undefined : hwUrl(`/camera/snapshot?t=${snapTs}`)}
                    download={`snapshot-${new Date(snapTs).toISOString().replace(/[:.]/g, "-")}.jpg`}
                    title="Download snapshot"
                    aria-disabled={cameraDisabled}
                    onClick={(e) => { if (cameraDisabled) e.preventDefault(); }}
                    style={{
                      fontSize: 9, padding: "2px 6px", borderRadius: 4,
                      background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                      color: "var(--lm-text-dim)", cursor: cameraDisabled ? "not-allowed" : "pointer",
                      opacity: cameraDisabled ? 0.5 : 1, textDecoration: "none",
                      display: "inline-flex", alignItems: "center", justifyContent: "center",
                      minWidth: 16, lineHeight: 1,
                    }}
                  >↓</a>
                  <button
                    onClick={refreshSnapshot}
                    disabled={cameraDisabled}
                    title="Capture fresh snapshot"
                    style={{
                      fontSize: 9, padding: "2px 6px", borderRadius: 4,
                      background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                      color: "var(--lm-text-dim)", cursor: cameraDisabled ? "not-allowed" : "pointer",
                      opacity: cameraDisabled ? 0.5 : 1,
                    }}
                  >↻</button>
                </div>
              </div>
              <div style={{
                width: "100%",
                aspectRatio: "4 / 3",
                borderRadius: 4,
                background: "var(--lm-surface)",
                border: "1px solid var(--lm-border)",
                overflow: "hidden",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}>
                {cameraDisabled || snapError ? (
                  <span style={{ fontSize: 9, color: "var(--lm-text-muted)" }}>
                    {cameraDisabled ? "off" : "—"}
                  </span>
                ) : (
                  <img
                    src={hwUrl(`/camera/snapshot?t=${snapTs}`)}
                    alt="snapshot"
                    style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                    onError={() => setSnapError(true)}
                    onLoad={() => setSnapError(false)}
                  />
                )}
              </div>
            </div>
          </div>
        </div>

        <div style={{ ...S.card, alignSelf: "start" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div style={S.cardLabel}>Vision Tracking</div>
            <span style={{
              fontSize: 10, padding: "2px 7px", borderRadius: 4,
              background: track.tracking ? "rgba(52,211,153,0.15)" : "var(--lm-surface)",
              color: track.tracking ? "var(--lm-green)" : "var(--lm-text-muted)",
              border: `1px solid ${track.tracking ? "rgba(52,211,153,0.3)" : "var(--lm-border)"}`,
              fontWeight: 700, letterSpacing: "0.05em",
            }}>
              {track.tracking ? "ACTIVE" : "IDLE"}
            </span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <input
              value={trackTarget}
              onChange={(e) => setTrackTarget(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !track.tracking) startTracking(); }}
              placeholder="cup, mug, coffee cup"
              style={inputStyle}
            />
            <input
              value={trackBbox}
              onChange={(e) => setTrackBbox(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !track.tracking) startTracking(); }}
              placeholder="x, y, w, h (optional bbox)"
              style={{ ...inputStyle, fontFamily: "monospace" }}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <button
                onClick={startTracking}
                disabled={track.tracking}
                style={{
                  flex: 1,
                  padding: "6px 10px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                  cursor: track.tracking ? "not-allowed" : "pointer",
                  background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.3)",
                  color: "var(--lm-green)", opacity: track.tracking ? 0.5 : 1,
                }}
              >Start</button>
              <button
                onClick={stopTracking}
                disabled={!track.tracking}
                style={{
                  flex: 1,
                  padding: "6px 10px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                  cursor: !track.tracking ? "not-allowed" : "pointer",
                  background: "rgba(248,113,113,0.1)", border: "1px solid rgba(248,113,113,0.3)",
                  color: "var(--lm-red)", opacity: !track.tracking ? 0.5 : 1,
                }}
              >Stop</button>
            </div>

            <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", lineHeight: 1.5 }}>
              One label or comma-separated synonyms. Bbox optional — skips YOLO detection.
            </div>

            {track.tracking && (
              <div style={{
                marginTop: 4, padding: "8px 12px", borderRadius: 6, fontSize: 11,
                background: "rgba(52,211,153,0.08)", border: "1px solid rgba(52,211,153,0.2)",
                color: "var(--lm-green)", fontFamily: "monospace",
                display: "flex", flexDirection: "column", gap: 4,
              }}>
                <span>target: <strong>{track.target}</strong></span>
                <span>conf: {track.confidence?.toFixed(3) ?? "?"}</span>
                {track.bbox && <span style={{ fontSize: 10 }}>bbox: [{track.bbox.join(", ")}]</span>}
                <button
                  onClick={fetchTrackStatus}
                  style={{
                    marginTop: 4, padding: "2px 8px", borderRadius: 4, fontSize: 10,
                    background: "transparent", border: "1px solid rgba(52,211,153,0.3)",
                    color: "var(--lm-green)", cursor: "pointer", alignSelf: "flex-start",
                  }}
                >Refresh status</button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// MediaFrame keeps stream at a stable aspect ratio and shows a friendly fallback when off/paused/errored
function MediaFrame({
  disabled,
  paused,
  error,
  disabledText,
  pausedText,
  errorText,
  highlight,
  children,
}: {
  disabled?: boolean;
  paused?: boolean;
  error?: boolean;
  disabledText: string;
  pausedText?: string;
  errorText: string;
  highlight?: boolean;
  children: React.ReactNode;
}) {
  const showFallback = disabled || paused || error;
  const fallbackText = disabled ? disabledText : paused ? (pausedText ?? "Paused") : errorText;
  return (
    <div style={{
      width: "100%",
      aspectRatio: "4 / 3",
      borderRadius: 8,
      border: `1px solid ${highlight ? "var(--lm-green)" : "var(--lm-border)"}`,
      background: "var(--lm-surface)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      overflow: "hidden",
      position: "relative",
    }}>
      {showFallback ? (
        <div style={{ fontSize: 11, color: "var(--lm-text-muted)", textAlign: "center", padding: 12 }}>
          {fallbackText}
        </div>
      ) : children}
    </div>
  );
}

const mediaImgStyle = (highlight: boolean): React.CSSProperties => ({
  width: "100%",
  height: "100%",
  objectFit: "contain",
  display: "block",
  background: "var(--lm-surface)",
  borderRadius: 7,
  outline: highlight ? "1px solid var(--lm-green)" : "none",
});

const inputStyle: React.CSSProperties = {
  padding: "6px 10px",
  borderRadius: 6,
  fontSize: 12,
  background: "var(--lm-surface)",
  border: "1px solid var(--lm-border)",
  color: "var(--lm-text)",
};
