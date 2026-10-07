import { useCallback, useEffect, useRef, useState } from "react";
import { S } from "./styles";
import { HW } from "./types";
import type { ServoState } from "./types";
import { StatusDot } from "./components";
import { usePolling } from "../../hooks/usePolling";
import "./servo.css";

const cardTitle = { ...S.cardLabel, fontSize: 15, letterSpacing: "normal", textTransform: "none" as const, marginBottom: 10 };

// Minimum gap between live-drag writes (shared 6.4 Mbit servo bus).
const LIVE_THROTTLE_MS = 80;

interface ServoDetail {
  id: number;
  angle: number | null;
  online: boolean;
  error?: string | null;
}

export function ServoSection() {
  const [servo, setServo] = useState<ServoState | null>(null);
  const [servos, setServos] = useState<Record<string, ServoDetail> | null>(null);
  const [aims, setAims] = useState<string[]>([]);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [moveTargets, setMoveTargets] = useState<Record<string, number>>({});
  const [moveDuration, setMoveDuration] = useState<number>(2.0);
  const [moving, setMoving] = useState(false);
  const [liveDrag, setLiveDrag] = useState(false);
  // Throttle live drag; a trailing send guarantees the final value lands.
  const liveLastSent = useRef(0);
  const liveTrailing = useRef<ReturnType<typeof setTimeout> | null>(null);
  const liveInFlight = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [sr, st] = await Promise.all([
        fetch(`${HW}/servo`).then((r) => r.json()).catch(() => null),
        fetch(`${HW}/servo/status`).then((r) => r.json()).catch(() => null),
      ]);
      if (sr) setServo(sr);
      if (st?.servos) setServos(st.servos);
    } catch {
      // Keep the last readout while HAL restarts.
    }
  }, []);

  useEffect(() => {
    fetch(`${HW}/servo/aim`).then((r) => r.json()).then((r) => {
      if (r?.directions) setAims(r.directions);
    }).catch(() => {});
  }, []);

  usePolling(async (signal) => {
    const [sr, st] = await Promise.all([
      fetch(`${HW}/servo`, { signal }).then((r) => r.json()).catch(() => null),
      fetch(`${HW}/servo/status`, { signal }).then((r) => r.json()).catch(() => null),
    ]);
    if (sr) setServo(sr);
    if (st?.servos) setServos(st.servos);
  }, 3000);

  const flash = (msg: string) => {
    setActionMsg(msg);
    setTimeout(() => setActionMsg(null), 2000);
  };

  const playAnim = async (recording: string) => {
    flash(`Playing ${recording}…`);
    await fetch(`${HW}/servo/play`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording }),
    }).catch(() => {});
    setTimeout(refresh, 500);
  };

  const aimTo = async (direction: string) => {
    flash(`Aiming ${direction}…`);
    await fetch(`${HW}/servo/aim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction, duration: 2.0 }),
    }).catch(() => {});
    setTimeout(refresh, 2500);
  };

  const release = async () => {
    flash("Releasing…");
    await fetch(`${HW}/servo/release`, {
      method: "POST",
      headers: { accept: "application/json" },
    }).catch(() => {});
    setTimeout(refresh, 500);
  };

  const uploadCsv = async (file: File | null) => {
    if (!file || uploading) return;
    const rawName = file.name || "recording";
    const recordingName = rawName.replace(/\.csv$/i, "").trim();
    if (!recordingName) {
      flash("Upload failed: missing recording name");
      return;
    }
    try {
      setUploading(true);
      flash(`Uploading ${recordingName}…`);
      const form = new FormData();
      form.append("file", file);
      form.append("recording_name", recordingName);
      const resp = await fetch(`${HW}/servo/upload`, { method: "POST", body: form });
      if (!resp.ok) {
        const msg = await resp.text().catch(() => "");
        throw new Error(msg || `HTTP ${resp.status}`);
      }
      flash(`Uploaded ${recordingName}`);
      setTimeout(refresh, 1000);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      flash(`Upload failed: ${msg.slice(0, 120)}`);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const zeroServos = async () => {
    flash("Moving to 0° (hold mode)…");
    await fetch(`${HW}/servo/zero`, { method: "POST", headers: { accept: "application/json" } }).catch(() => {});
    setTimeout(refresh, 2500);
  };
  const holdServos = async () => {
    flash("Hold — freezing current pose");
    await fetch(`${HW}/servo/hold`, { method: "POST", headers: { accept: "application/json" } }).catch(() => {});
    setTimeout(refresh, 500);
  };
  const resumeServos = async () => {
    flash("Resuming animation");
    await fetch(`${HW}/servo/resume`, { method: "POST", headers: { accept: "application/json" } }).catch(() => {});
    setTimeout(refresh, 500);
  };

  useEffect(() => {
    if (!servos) return;
    setMoveTargets((prev) => {
      if (Object.keys(prev).length > 0) return prev;
      const seed: Record<string, number> = {};
      Object.keys(servos).forEach((j) => { seed[j] = 0; });
      return seed;
    });
  }, [servos]);

  const syncMoveFromCurrent = () => {
    if (!servos) return;
    const next: Record<string, number> = {};
    Object.entries(servos).forEach(([j, info]) => {
      next[j] = info.angle != null ? Math.round(info.angle * 10) / 10 : 0;
    });
    setMoveTargets(next);
    flash("Synced sliders to current pose");
  };

  // Duration 0 takes the send_action path: one bus write, no queued tween.
  const sendLive = useCallback((joint: string, value: number) => {
    const fire = async () => {
      if (liveInFlight.current) return;
      liveInFlight.current = true;
      liveLastSent.current = Date.now();
      try {
        await fetch(`${HW}/servo/move`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ positions: { [joint]: value }, duration: 0 }),
        });
      } catch {
        // A dropped frame is corrected by the next one and the trailing send.
      } finally {
        liveInFlight.current = false;
      }
    };
    if (liveTrailing.current) clearTimeout(liveTrailing.current);
    const since = Date.now() - liveLastSent.current;
    if (since >= LIVE_THROTTLE_MS && !liveInFlight.current) {
      void fire();
    } else {
      liveTrailing.current = setTimeout(() => { void fire(); }, LIVE_THROTTLE_MS - since);
    }
  }, []);

  const onJointChange = useCallback((joint: string, v: number) => {
    setMoveTargets((m) => ({ ...m, [joint]: v }));
    if (liveDrag) sendLive(joint, v);
  }, [liveDrag, sendLive]);

  // Drop a pending trailing send if the section unmounts mid-drag.
  useEffect(() => () => {
    if (liveTrailing.current) clearTimeout(liveTrailing.current);
  }, []);

  const moveServo = async () => {
    if (moving) return;
    const positions = Object.fromEntries(Object.entries(moveTargets).map(([j, v]) => [j, Number(v)]));
    if (Object.keys(positions).length === 0) {
      flash("No joints to move");
      return;
    }
    setMoving(true);
    flash(`Moving (duration ${moveDuration}s)…`);
    try {
      const resp = await fetch(`${HW}/servo/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ positions, duration: moveDuration }),
      });
      const json = await resp.json().catch(() => null);
      if (!resp.ok) {
        flash(`Move failed: HTTP ${resp.status}`);
      } else if (json?.errors) {
        const keys = Object.keys(json.errors);
        flash(`Move warnings: ${keys.join(", ")}`);
      } else {
        flash("Move complete");
      }
    } catch (e) {
      flash(`Move failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setMoving(false);
      setTimeout(refresh, Math.max(500, moveDuration * 1000 + 200));
    }
  };

  const onlineCount = servos ? Object.values(servos).filter((s) => s.online).length : 0;
  const totalCount = servos ? Object.keys(servos).length : 0;
  const allOnline = totalCount > 0 && onlineCount === totalCount;
  const headerColor = totalCount === 0 ? "var(--lm-text-muted)"
    : allOnline ? "var(--lm-green)"
    : onlineCount === 0 ? "var(--lm-red)"
    : "var(--lm-amber)";

  return (
    <div className="servo-section">

      <div className="servo-overview">

        <div style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, gap: 6 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
              <div style={cardTitle}>Servos</div>
              <span style={{
                fontSize: 13, padding: "2px 7px", borderRadius: 4,
                background: `color-mix(in srgb, ${headerColor} 12%, transparent)`, color: headerColor,
                border: `1px solid color-mix(in srgb, ${headerColor} 35%, transparent)`,
                fontWeight: 700, letterSpacing: "0.05em",
                flexShrink: 0,
              }}>
                {onlineCount}/{totalCount} online
              </span>
            </div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-amber)", textAlign: "right", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {servo?.current || "idle"}
            </div>
          </div>
          {servos ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {Object.entries(servos).sort(([,a], [,b]) => a.id - b.id).map(([joint, info]) => (
                <ServoCard key={joint} joint={joint} info={info} />
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>Loading…</div>
          )}
        </div>

        <div style={{ ...S.card, alignSelf: "start" }}>
          <div style={cardTitle}>Aim Direction</div>
          <div style={{ fontSize: 13, color: "var(--lm-text-dim)", marginBottom: 10 }}>
            Choose a direction. Movement takes 2 seconds.
          </div>
          {aims.length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {aims.map((dir) => (
                <ChipButton key={dir} onClick={() => aimTo(dir)}>{dir}</ChipButton>
              ))}
            </div>
          ) : <span style={{ fontSize: 13, color: "var(--lm-text-dim)" }}>No directions configured</span>}
        </div>

        <div style={{ ...S.card, alignSelf: "start" }}>
          <div style={cardTitle}>Motor Control</div>
          <div style={{ fontSize: 13, color: "var(--lm-text-dim)", marginBottom: 10 }}>
            Control motor position and torque.
          </div>
          <div style={{
            display: "grid",
            gridTemplateColumns: "minmax(96px, max-content) 1fr",
            rowGap: 8, columnGap: 10,
            alignItems: "center",
          }}>
            <ControlButton onClick={zeroServos}   color="var(--lm-teal)"             title="Zero (0°)" hint="Hold at 0°; blocks play calls" />
            <ControlButton onClick={holdServos}   color="var(--lm-amber)"            title="Hold"      hint="Freeze pose; emotions still play" />
            <ControlButton onClick={resumeServos} color="var(--lm-indigo, #6366f1)"  title="Resume"    hint="Exit hold, restart idle" />
            <ControlButton onClick={release}      color="var(--lm-red)"              title="Release"   hint="Disable torque; move by hand" />
          </div>
        </div>

        <div style={{ ...S.card, alignSelf: "start" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, gap: 6 }}>
            <div style={cardTitle}>Animations</div>
            <input
              type="file"
              accept=".csv,text/csv"
              style={{ display: "none" }}
              ref={fileInputRef}
              onChange={(e) => uploadCsv(e.target.files?.[0] ?? null)}
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              title="Upload CSV — file name becomes recording name"
              style={{
                fontSize: 13, minHeight: 36, padding: "6px 10px", borderRadius: 5, fontWeight: 600,
                background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                color: uploading ? "var(--lm-text-muted)" : "var(--lm-amber)",
                cursor: uploading ? "not-allowed" : "pointer",
                flexShrink: 0,
              }}
            >
              {uploading ? "…" : "Upload CSV"}
            </button>
          </div>
          {(servo?.available_recordings ?? []).length > 0 ? (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {(servo?.available_recordings ?? []).map((anim) => (
                <ChipButton key={anim} active={anim === servo?.current} onClick={() => playAnim(anim)}>
                  {anim}
                </ChipButton>
              ))}
            </div>
          ) : <span style={{ fontSize: 13, color: "var(--lm-text-dim)" }}>No recordings available</span>}
        </div>
      </div>

      <div style={S.card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10, gap: 10, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div>
              <div style={cardTitle}>Manual Move</div>
              <div style={{ fontSize: 13, color: "var(--lm-text-dim)" }}>
                Set each joint target between −90° and +90°.
              </div>
            </div>
            <label
              title="Send each slider change straight to the servo (duration 0), instead of waiting for the Move button."
              style={{
                fontSize: 13, display: "flex", alignItems: "center", gap: 5, cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0,
                color: liveDrag ? "var(--lm-green)" : "var(--lm-text-dim)", fontWeight: 600,
              }}
            >
              <input
                type="checkbox"
                checked={liveDrag}
                onChange={(e) => setLiveDrag(e.target.checked)}
                style={{ accentColor: "var(--lm-green)", cursor: "pointer", flexShrink: 0 }}
              />
              Live drag
            </label>
          </div>
          <div className="servo-move-actions">
            <button
              onClick={syncMoveFromCurrent}
              disabled={!servos}
              style={{
                fontSize: 13, padding: "10px 14px", borderRadius: 8, minHeight: 40, maxWidth: "100%", overflowWrap: "anywhere", fontWeight: 600,
                background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                color: "var(--lm-text-dim)", cursor: servos ? "pointer" : "not-allowed",
              }}
            >Sync from current</button>
            <label style={{ fontSize: 13, color: "var(--lm-text-dim)", display: "flex", alignItems: "center", gap: 4 }}>
              Duration
              <input
                type="number"
                min={0}
                max={10}
                step={0.1}
                value={moveDuration}
                onChange={(e) => setMoveDuration(Math.max(0, Math.min(10, Number(e.target.value))))}
                style={{
                  width: 60, padding: "4px 6px", borderRadius: 4, fontSize: 13,
                  background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                  color: "var(--lm-text)", fontFamily: "monospace",
                }}
              />
              s
            </label>
            <button
              onClick={moveServo}
              disabled={moving || !servos}
              style={{
                fontSize: 13, minHeight: 40, padding: "8px 16px", borderRadius: 6, fontWeight: 600,
                background: "rgba(52,211,153,0.1)", border: "1px solid rgba(52,211,153,0.3)",
                color: "var(--lm-green)",
                cursor: moving ? "wait" : (servos ? "pointer" : "not-allowed"),
                opacity: servos && !moving ? 1 : 0.5,
              }}
            >{moving ? "Moving…" : "Move"}</button>
          </div>
        </div>
        {servos && Object.keys(moveTargets).length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {Object.entries(servos).sort(([,a], [,b]) => a.id - b.id).map(([joint]) => (
              <JointSlider
                key={joint}
                joint={joint}
                value={moveTargets[joint] ?? 0}
                actual={servos[joint]?.angle ?? null}
                onChange={(v) => onJointChange(joint, v)}
              />
            ))}
          </div>
        ) : <span style={{ fontSize: 13, color: "var(--lm-text-dim)" }}>Loading joints…</span>}
      </div>

      {actionMsg && (
        <div style={{
          position: "fixed",
          bottom: 20,
          right: 20,
          zIndex: 50,
          padding: "10px 16px",
          borderRadius: 8,
          background: "var(--lm-card)",
          border: "1px solid var(--lm-amber)",
          color: "var(--lm-amber)",
          fontSize: 12,
          fontWeight: 600,
          boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
          maxWidth: "min(360px, calc(100vw - 40px))",
        }}>{actionMsg}</div>
      )}
    </div>
  );
}

// Per-servo row on a flat grid so columns align across servos.
function ServoCard({ joint, info }: { joint: string; info: ServoDetail }) {
  return (
    <div className="servo-readout" style={{
      display: "grid",
      alignItems: "center",
      gap: 8,
      padding: "10px 12px",
      borderRadius: 6,
      background: "var(--lm-surface)",
      border: `1px solid ${info.online ? "var(--lm-border)" : "rgba(239,68,68,0.4)"}`,
    }}>
      <StatusDot ok={info.online} />
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-text)", fontFamily: "monospace", overflowWrap: "anywhere" }}>
        {joint.replace(".pos", "")}
      </span>
      <span style={{ fontSize: 13, color: "var(--lm-text-dim)", textAlign: "right" }}>
        #{info.id}
      </span>
      {info.online && info.angle != null ? (
        <>
          <div className="servo-angle-bar" aria-hidden="true" style={{ height: 6, borderRadius: 3, background: "var(--lm-border)", overflow: "hidden" }}>
            <div style={{
              width: `${Math.min(100, Math.max(0, ((info.angle + 180) / 360) * 100))}%`,
              height: "100%", borderRadius: 3,
              background: "var(--lm-teal)", transition: "width 0.3s ease",
            }} />
          </div>
          <span style={{
            fontSize: 13, fontWeight: 600, color: "var(--lm-teal)",
            textAlign: "right", fontFamily: "monospace",
          }}>
            {info.angle.toFixed(1)}°
          </span>
        </>
      ) : (
        <span className="servo-readout-error" style={{
          gridColumn: "4 / -1",
          fontSize: 13, fontWeight: 500,
          color: "var(--lm-red)",
          fontFamily: "monospace",
          lineHeight: 1.35,
          overflowWrap: "anywhere" as const,
          whiteSpace: "normal" as const,
        }} title={info.error || "offline"}>
          {info.error || "offline"}
        </span>
      )}
    </div>
  );
}

// One /servo/move target row: slider, numeric input and delta vs actual.
function JointSlider({ joint, value, actual, onChange }: {
  joint: string;
  value: number;
  actual: number | null;
  onChange: (v: number) => void;
}) {
  const delta = actual != null ? value - actual : null;
  return (
    <div className="servo-joint">
      <span className="servo-joint-name" style={{ fontSize: 13, color: "var(--lm-text)", fontWeight: 600, fontFamily: "monospace" }}>
        {joint.replace(".pos", "")}
      </span>
      <input
        className="servo-joint-range"
        aria-label={`${joint.replace(".pos", "")} target angle`}
        type="range"
        min={-90}
        max={90}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: "var(--lm-teal)" }}
      />
      <input
        className="servo-joint-input"
        aria-label={`${joint.replace(".pos", "")} target angle in degrees`}
        type="number"
        min={-90}
        max={90}
        step={0.5}
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (!isNaN(n)) onChange(Math.max(-90, Math.min(90, n)));
        }}
        style={{
          width: "100%", padding: "3px 6px", borderRadius: 4, fontSize: 13,
          background: "var(--lm-bg)", border: "1px solid var(--lm-border)",
          color: "var(--lm-text)", fontFamily: "monospace", textAlign: "right",
        }}
      />
      <span className="servo-joint-current" style={{
        fontSize: 13,
        color: delta == null ? "var(--lm-text-dim)" : Math.abs(delta) < 1 ? "var(--lm-green)" : "var(--lm-amber)",
        fontFamily: "monospace",
      }}>
        {actual != null ? `Current ${actual.toFixed(0)}°` : "—"}
      </span>
    </div>
  );
}

// ChipButton is the consistent style shared by Aim presets and Animation presets.
function ChipButton({ children, onClick, active }: {
  children: React.ReactNode;
  onClick: () => void;
  active?: boolean;
}) {
  return (
    <button onClick={onClick} style={{
      fontSize: 13, padding: "10px 14px", borderRadius: 8, minHeight: 40, maxWidth: "100%", overflowWrap: "anywhere",
      background: active ? "rgba(245,158,11,0.12)" : "var(--lm-surface)",
      border: `1px solid ${active ? "var(--lm-amber)" : "var(--lm-border)"}`,
      color: active ? "var(--lm-amber)" : "var(--lm-text)",
      cursor: "pointer",
      fontWeight: active ? 600 : 500,
      transition: "all 0.15s",
    }}>{children}</button>
  );
}

// Renders two grid cells (button + hint) so buttons align in a 2-column grid.
function ControlButton({ onClick, color, title, hint }: {
  onClick: () => void;
  color: string;
  title: string;
  hint: string;
}) {
  return (
    <>
      <button
        onClick={onClick}
        style={{
          fontSize: 13, minHeight: 40, padding: "8px 14px", borderRadius: 6, width: "100%",
          background: `color-mix(in srgb, ${color} 18%, transparent)`,
          border: `1.5px solid color-mix(in srgb, ${color} 70%, transparent)`,
          color,
          cursor: "pointer", fontWeight: 700,
          whiteSpace: "nowrap",
          transition: "background 0.15s, transform 0.05s",
          boxShadow: `0 1px 0 color-mix(in srgb, ${color} 30%, transparent) inset`,
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = `color-mix(in srgb, ${color} 30%, transparent)`;
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = `color-mix(in srgb, ${color} 18%, transparent)`;
        }}
        onMouseDown={(e) => { e.currentTarget.style.transform = "translateY(1px)"; }}
        onMouseUp={(e) => { e.currentTarget.style.transform = "translateY(0)"; }}
      >{title}</button>
      <span style={{ fontSize: 13, color: "var(--lm-text-dim)", lineHeight: 1.35 }}>{hint}</span>
    </>
  );
}
