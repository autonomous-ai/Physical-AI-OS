import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, Timer } from "lucide-react";
import type { DisplayEvent } from "../types";
import type { FlowStage, ActiveFlowStage } from "./types";
import { FLOW_NODES } from "./types";
import { useTheme } from "@/lib/useTheme";
import { cameraSnapshotURLs, extractNodeInfo, aggregateEvents } from "./helpers";

// Hidden-textarea clipboard fallback for non-secure origins (http://Pi.local).
function fallbackCopy(text: string): void {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* nothing to do */ }
  document.body.removeChild(ta);
}

function formatPipelineDuration(ms: number): string {
  return ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
    : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function pipelineRowColor(kind: string): string {
  if (kind === "thinking" || kind === "thinking_first_token") return "var(--lm-purple)";
  if (kind === "assistant" || kind === "agent_first_token") return "var(--lm-blue)";
  if (kind === "tool" || kind === "tool_result") return "var(--lm-amber)";
  if (kind === "lifecycle_start" || kind === "lifecycle_end") return "var(--lm-green)";
  if (kind === "error") return "var(--lm-red)";
  if (kind === "compaction") return "var(--lm-purple)";
  return "var(--lm-text-muted)";
}

export function FlowDiagram({
  activeStage,
  visitedStages,
  compact = false,
  turnEvents = [],
}: {
  activeStage: ActiveFlowStage;
  visitedStages: Set<FlowStage>;
  compact?: boolean;
  turnEvents?: DisplayEvent[];
}) {
  const VW = 1200;
  const VH = 1320;
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [isPhoneLayout, setIsPhoneLayout] = useState(false);
  const [mobileDetailsOpen, setMobileDetailsOpen] = useState(false);
  const [, , themeClass] = useTheme();

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [pipelineGuideOpen, setPipelineGuideOpen] = useState(false);
  const [pipelineCopied, setPipelineCopied] = useState(false);
  const dragStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const panRef = useRef(pan);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinchStart = useRef<{ distance: number; zoom: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => { panRef.current = pan; }, [pan]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 768px)");
    const update = () => setIsPhoneLayout(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  // Native wheel listener with { passive: false } so preventDefault works.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? -0.1 : 0.1;
      setZoom((z) => Math.min(4, Math.max(0.4, z + delta)));
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  const handlePointerDown = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    setDragging(true);
    if (pointers.current.size === 1) {
      dragStart.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
    } else if (pointers.current.size === 2) {
      const [first, second] = [...pointers.current.values()];
      pinchStart.current = {
        distance: Math.hypot(second.x - first.x, second.y - first.y),
        zoom,
      };
    }
  }, [zoom]);

  const handlePointerMove = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size >= 2 && pinchStart.current) {
      const [first, second] = [...pointers.current.values()];
      const distance = Math.hypot(second.x - first.x, second.y - first.y);
      if (pinchStart.current.distance > 0) {
        setZoom(Math.min(4, Math.max(0.4, pinchStart.current.zoom * distance / pinchStart.current.distance)));
      }
      return;
    }
    if (!dragging) return;
    const dx = e.clientX - dragStart.current.x;
    const dy = e.clientY - dragStart.current.y;
    const nextPan = { x: dragStart.current.panX + dx / zoom, y: dragStart.current.panY + dy / zoom };
    panRef.current = nextPan;
    setPan(nextPan);
  }, [dragging, zoom, setZoom, setPan]);

  const handlePointerEnd = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    pointers.current.delete(e.pointerId);
    pinchStart.current = null;
    if (pointers.current.size === 1) {
      const [remaining] = pointers.current.values();
      dragStart.current = { x: remaining.x, y: remaining.y, panX: panRef.current.x, panY: panRef.current.y };
    }
    setDragging(pointers.current.size > 0);
  }, []);

  const resetView = useCallback(() => {
    const initialPan = { x: 0, y: 0 };
    panRef.current = initialPan;
    setZoom(1);
    setPan(initialPan);
  }, [setZoom, setPan]);

  const vbW = VW / zoom;
  const vbH = VH / zoom;
  const vbX = (VW - vbW) / 2 - pan.x;
  const vbY = (VH - vbH) / 2 - pan.y;

  // Event Pipeline rect, shared by rendering, edge anchoring and the row list.
  const PIPE = { x: 798, y: 330, w: 304, h: 376 };
  // Returns the point on the pipeline rect boundary along the line from the rect center toward (extX, extY).
  const pipeAnchor = (extX: number, extY: number) => {
    const cx = PIPE.x + PIPE.w / 2;
    const cy = PIPE.y + PIPE.h / 2;
    const dx = extX - cx;
    const dy = extY - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const tx = dx === 0 ? Infinity : (PIPE.w / 2) / Math.abs(dx);
    const ty = dy === 0 ? Infinity : (PIPE.h / 2) / Math.abs(dy);
    const t = Math.min(tx, ty);
    return { x: cx + dx * t, y: cy + dy * t };
  };

  const positions: Record<FlowStage, { x: number; y: number }> = {
    intent_check:      { x: 80, y: 50 },
    local_match:       { x: 200, y: 50 },
    os_gate:         { x: 467, y: 795 },
    mic_input:         { x: -40, y: 240 },
    cam_input:         { x: 80, y: 240 },
    button_input:      { x: -40, y: 350 },
    hw_camera:         { x: 200, y: 345 },
    hw_emotion:        { x: 200, y: 480 },
    hw_led:            { x: 200, y: 615 },
    hw_servo:          { x: 200, y: 750 },
    hw_audio:          { x: 200, y: 885 },
    tts_speak:         { x: 200, y: 1020 },
    hw_mood:             { x: 467, y: 1065 },
    hw_wellbeing:        { x: 467, y: 1200 },
    hw_music_suggestion: { x: 467, y: 1335 },
    hw_posture:          { x: 467, y: 1470 },
    schedule_trigger:  { x: 750, y: 240 },
    agent_call:        { x: 950, y: 240 },
    agent_thinking:    { x: 1180, y: 480 },
    tool_exec:         { x: 820, y: 600 },
    agent_response:    { x: 950, y: 795 },
    channel_input:     { x: 1300, y: 240 },
    webchat_input:     { x: 1300, y: 440 },
    tg_out:            { x: 1300, y: 795 },
    tg_alert:          { x: 467, y: 930 },
  };

  const edges: [FlowStage, FlowStage][] = [
    ["mic_input",         "intent_check"],
    ["cam_input",         "intent_check"],
    ["button_input",      "intent_check"],
    ["intent_check",      "local_match"],
    ["local_match",       "hw_emotion"],
    ["local_match",       "hw_led"],
    ["local_match",       "hw_servo"],
    ["local_match",       "tts_speak"],
    ["intent_check",      "agent_call"],
    ["channel_input",     "agent_call"],
    ["webchat_input",     "agent_call"],
    ["schedule_trigger",  "agent_call"],
    ["agent_call",        "tool_exec"],
    ["tool_exec",         "agent_response"],
    ["tool_exec",         "hw_led"],
    ["tool_exec",         "hw_servo"],
    ["tool_exec",         "hw_emotion"],
    ["tool_exec",         "hw_audio"],
    ["tool_exec",         "hw_camera"],
    ["tool_exec",         "os_gate"],
    ["agent_response",    "os_gate"],
    ["os_gate",         "hw_emotion"],
    ["os_gate",         "hw_led"],
    ["os_gate",         "hw_servo"],
    ["os_gate",         "hw_audio"],
    ["os_gate",         "hw_wellbeing"],
    ["os_gate",         "hw_mood"],
    ["os_gate",         "hw_music_suggestion"],
    ["os_gate",         "hw_posture"],
    ["os_gate",         "tts_speak"],
    ["os_gate",         "tg_out"],
    ["os_gate",         "tg_alert"],
    ["tg_alert",          "tg_out"],
  ];

  const nodeR = compact ? 28 : 38;
  const gateR = compact ? 22 : 30;

  const ttsSuppressed = turnEvents.some((ev) =>
    ev.type === "flow_event" && ["tts_suppressed", "tts_muted", "tts_cancelled", "hw_cancelled"].includes(ev.detail?.node ?? "")
  );

  function nodeColor(id: FlowStage) {
    if (id === "tts_speak" && ttsSuppressed) return "var(--lm-red)";
    if (id === activeStage || visitedStages.has(id)) {
      return FLOW_NODES.find((n) => n.id === id)?.color ?? "var(--lm-text-muted)";
    }
    return "var(--lm-text-muted)";
  }
  function nodeOpacity(id: FlowStage) {
    if (id === activeStage) return 1;
    if (visitedStages.has(id)) return 1;
    return 1;
  }
  function edgeColor(from: FlowStage, to: FlowStage) {
    const fromVisited = visitedStages.has(from) || from === activeStage;
    const toVisited = visitedStages.has(to) || to === activeStage;
    if (fromVisited && toVisited) return nodeColor(to);
    if (fromVisited || toVisited) return "var(--lm-border-hi)";
    return "var(--lm-border)";
  }
  function edgeOpacity(from: FlowStage, to: FlowStage) {
    const fromVisited = visitedStages.has(from) || from === activeStage;
    const toVisited = visitedStages.has(to) || to === activeStage;
    if (fromVisited && toVisited) return 0.98;
    if (fromVisited || toVisited) return 0.8;
    return 0.45;
  }

  const glowId = compact ? "flow-glow-c" : "flow-glow";
  const pipelineClipId = compact ? "flow-pipeline-clip-c" : "flow-pipeline-clip";

  const nodeInfo = extractNodeInfo(turnEvents);
  const pipelineRows = aggregateEvents(turnEvents);
  const mobileNodeDetails = FLOW_NODES.flatMap((node) => {
    if (node.id === "agent_thinking" || node.id === "tool_exec") return [];
    const lines = (nodeInfo[node.id] ?? []).filter((line) => !line.startsWith("🖼"));
    return lines.length > 0 ? [{ label: node.label, lines }] : [];
  });

  const sensingSnapshotUrls: string[] = (nodeInfo.agent_call ?? [])
    .filter((l) => l.startsWith("🖼"))
    .map((l) => l.match(/snapshot:\s*\/tmp\/(?:lamp|hal)-(?:sensing|emotion|motion)-snapshots\/((?:sensing|emotion|motion)_[^\s]+\.jpg)/)?.[1])
    .filter((f): f is string => !!f)
    .map((f) => `/api/sensing/snapshot/${f}`);

  const imageSentToAgent: boolean = (nodeInfo.agent_call ?? []).some((l) => l.includes("📷 image attached"));
  const agentSnapshotUrls = cameraSnapshotURLs(turnEvents);

  return (
    <div id="FLOW_DIAGRAM" data-region="FLOW_DIAGRAM" style={{ position: "relative", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <svg
        ref={svgRef}
        id="FLOW_DIAGRAM_SVG" data-region="FLOW_DIAGRAM_SVG"
        viewBox={`${vbX} ${vbY} ${vbW} ${vbH}`}
        style={{
          display: "block", width: "100%", flex: 1, minHeight: 0,
          cursor: dragging ? "grabbing" : "grab", userSelect: "none",
          touchAction: "none",
        }}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerEnd}
        onPointerCancel={handlePointerEnd}
      >
        <defs>
          <clipPath id={pipelineClipId}>
            <rect x={PIPE.x + 6} y={PIPE.y + 18} width={PIPE.w - 12} height={PIPE.h - 24} />
          </clipPath>
          <filter id={glowId}>
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <marker id={`arrow-${compact ? "c" : "f"}`} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
            <path d="M0,0 L0,6 L6,3 z" fill="context-stroke" />
          </marker>
        </defs>

        <g>
          <rect x={-100} y={0} width={1500} height={110} rx={14}
            fill="var(--lm-teal)" fillOpacity={0.12} stroke="var(--lm-teal)" strokeWidth={2} opacity={0.6}
            strokeDasharray="4 4"
          />
          <rect x={417} y={100} width={110} height={1280} rx={10}
            fill="var(--lm-teal)" fillOpacity={0.12} stroke="var(--lm-teal)" strokeWidth={2} opacity={0.6}
            strokeDasharray="4 4"
          />
          <text x={467} y={-8} textAnchor="middle"
            fill="var(--lm-teal)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            OS Server
          </text>
        </g>
        <g>
          <rect x={-100} y={185} width={360} height={1050} rx={14}
            fill="var(--lm-amber)" fillOpacity={0.04} stroke="var(--lm-amber)" strokeWidth={1} opacity={0.3}
            strokeDasharray="4 4"
          />
          <text x={80} y={175} textAnchor="middle"
            fill="var(--lm-amber)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            Device
          </text>
        </g>
        <g>
          <rect x={695} y={185} width={520} height={665} rx={14}
            fill="var(--lm-blue)" fillOpacity={0.04} stroke="var(--lm-blue)" strokeWidth={1} opacity={0.3}
            strokeDasharray="4 4"
          />
          <text x={955} y={175} textAnchor="middle"
            fill="var(--lm-blue)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            Agent
          </text>
        </g>

        {edges.map(([from, to]) => {
          const f = positions[from];
          const t = positions[to];
          const color = edgeColor(from, to);
          const sw = edgeOpacity(from, to) > 0.5 ? 2 : 1.5;
          const op = edgeOpacity(from, to);
          const marker = `url(#arrow-${compact ? "c" : "f"})`;

          if (from === "local_match" && (to === "hw_led" || to === "hw_servo" || to === "tts_speak" || to === "hw_emotion" || to === "hw_audio")) {
            const elbowX = t.x - 80;
            const startY = f.y + nodeR;
            const endY = t.y;
            const endX = t.x - nodeR - 4;
            return (
              <path key={`${from}-${to}`}
                d={`M ${f.x - nodeR * 0.7} ${f.y + nodeR * 0.7} L ${elbowX} ${startY + 20} L ${elbowX} ${endY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
              />
            );
          }

          if (from === "os_gate" && (to === "hw_mood" || to === "hw_wellbeing" || to === "hw_music_suggestion" || to === "hw_posture")) {
            const elbowX = f.x + 90;
            const startX = f.x + nodeR + 4;
            const startY = f.y;
            const endX = t.x + nodeR + 4;
            const endY = t.y;
            return (
              <path key={`${from}-${to}`}
                d={`M ${startX} ${startY} L ${elbowX} ${startY} L ${elbowX} ${endY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
                strokeDasharray="6 4"
              />
            );
          }

          if (from === "tg_alert" && to === "tg_out") {
            const startX = f.x + nodeR + 4;
            const startY = f.y;
            const endX = t.x;
            const endY = t.y + nodeR + 4;
            const active = (visitedStages.has(from) || from === activeStage) && (visitedStages.has(to) || to === activeStage);
            return (
              <path key={`${from}-${to}`}
                d={`M ${startX} ${startY} L ${endX} ${startY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
                strokeDasharray={active ? undefined : "6 4"}
              />
            );
          }

          const isGateEdge = from === "os_gate" || to === "os_gate";
          const isHWMarkerEdge = from === "agent_response" && (to === "hw_emotion" || to === "hw_led" || to === "hw_servo" || to === "hw_audio" || to === "hw_wellbeing" || to === "hw_mood" || to === "hw_music_suggestion" || to === "hw_posture");

          let x1: number, y1: number, x2: number, y2: number;
          if (from === "tool_exec" && to !== "tool_exec") {
            const a = pipeAnchor(t.x, t.y);
            const dxA = t.x - a.x, dyA = t.y - a.y;
            const lenA = Math.sqrt(dxA * dxA + dyA * dyA) || 1;
            x1 = a.x;
            y1 = a.y;
            x2 = t.x - (dxA / lenA) * (nodeR + 4);
            y2 = t.y - (dyA / lenA) * (nodeR + 4);
          } else if (to === "tool_exec" && from !== "tool_exec") {
            const a = pipeAnchor(f.x, f.y);
            const dxA = a.x - f.x, dyA = a.y - f.y;
            const lenA = Math.sqrt(dxA * dxA + dyA * dyA) || 1;
            x1 = f.x + (dxA / lenA) * nodeR;
            y1 = f.y + (dyA / lenA) * nodeR;
            x2 = a.x;
            y2 = a.y;
          } else {
            const dx = t.x - f.x, dy = t.y - f.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            x1 = f.x + (dx / len) * nodeR;
            y1 = f.y + (dy / len) * nodeR;
            x2 = t.x - (dx / len) * (nodeR + 4);
            y2 = t.y - (dy / len) * (nodeR + 4);
          }
          const dashArray = isGateEdge || isHWMarkerEdge ? "6 4" : undefined;
          return (
            <line key={`${from}-${to}`} x1={x1} y1={y1} x2={x2} y2={y2}
              stroke={color} strokeWidth={sw}
              markerEnd={marker} opacity={op}
              {...(dashArray ? { strokeDasharray: dashArray } : {})}
            />
          );
        })}

        {(() => {
          const px = PIPE.x, py = PIPE.y, pw = PIPE.w, ph = PIPE.h;
          const pipelineColor = "var(--lm-blue)";
          const fmtDur = (ms: number) => ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
            : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
          const fmtChars = (n: number) => n >= 1000 ? `~${(n / 1000).toFixed(1)}k chars` : `${n} chars`;
          const fmtClockMs = (ms: number): string => {
            const d = new Date(ms);
            const pad = (n: number, w = 2) => String(n).padStart(w, "0");
            return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
          };
          let chatSendTs = 0, lcStartTs = 0, lcEndTs = 0;
          for (const ev of turnEvents) {
            const tt = new Date(ev.time).getTime();
            const fn = ev.detail?.node;
            if ((ev.type === "chat_send" || fn === "chat_send") && !chatSendTs) chatSendTs = tt;
            if (fn === "lifecycle_start" && !lcStartTs) lcStartTs = tt;
            if (fn === "lifecycle_end") lcEndTs = tt;
          }
          const toolTotalMs = pipelineRows
            .filter((r) => r.kind === "tool" && r.durationMs > 0)
            .reduce((acc, r) => acc + r.durationMs, 0);
          const initMs = (chatSendTs && lcStartTs && lcStartTs > chatSendTs) ? lcStartTs - chatSendTs : 0;
          const turnLlmMs = (lcStartTs && lcEndTs && lcEndTs > lcStartTs) ? (lcEndTs - lcStartTs) - toolTotalMs : 0;
          const totalMs = (chatSendTs && lcEndTs) ? lcEndTs - chatSendTs : 0;
          const sumParts: string[] = [];
          if (initMs > 0) sumParts.push(`init ${fmtDur(initMs)}`);
          if (turnLlmMs > 0) sumParts.push(`llm ${fmtDur(turnLlmMs)}`);
          if (toolTotalMs > 0) sumParts.push(`tool ${fmtDur(toolTotalMs)}`);
          const headerSummary = sumParts.length > 0 && totalMs > 0
            ? `${sumParts.join("  ·  ")}  =  total ${fmtDur(totalMs)}`
            : sumParts.length > 0
            ? sumParts.join("  ·  ")
            : (totalMs > 0 ? `total ${fmtDur(totalMs)}` : "");
          const rowColor = (kind: string) => {
            if (kind === "thinking" || kind === "thinking_first_token") return "var(--lm-purple)";
            if (kind === "assistant" || kind === "agent_first_token") return "var(--lm-blue)";
            if (kind === "tool" || kind === "tool_result") return "var(--lm-amber)";
            if (kind === "lifecycle_start" || kind === "lifecycle_end") return "var(--lm-green)";
            if (kind === "error") return "var(--lm-red)";
            if (kind === "compaction") return "var(--lm-purple)";
            return "var(--lm-text-muted)";
          };
          const guideBtnX = px + pw - 14;
          const guideBtnY = py + 10;
          const copyBtnX = px + pw - 28;
          const copyBtnY = py + 10;
          // Plain-text dump of the pipeline content for the clipboard.
          const buildPipelineText = (): string => {
            const lines: string[] = [];
            lines.push(`⟨agent event pipeline⟩`);
            if (headerSummary) lines.push(`⏱ ${headerSummary}`);
            for (let i = 0; i < pipelineRows.length; i++) {
              const r = pipelineRows[i];
              let line = `${fmtClockMs(r.startMs)}  ${r.label}`;
              if (r.kind === "thinking" || r.kind === "assistant") {
                line += `  ${fmtDur(r.durationMs)} · ${r.chunks} chunks · ${fmtChars(r.chars)}`;
              } else if (r.kind === "tool") {
                line += `  ${r.durationMs > 0 ? fmtDur(r.durationMs) : "…"}`;
                if (r.detail) line += `  ${r.detail}`;
              } else if (r.detail) {
                line += `  ${r.detail}`;
              }
              lines.push(line);
              const next = pipelineRows[i + 1];
              const gapMs = next ? next.startMs - r.endMs : 0;
              if (gapMs > 200) {
                lines.push(`    ⋯ + ${fmtDur(gapMs)} OS Server waiting next event`);
              }
            }
            return lines.join("\n");
          };
          const handleCopyPipeline = () => {
            const text = buildPipelineText();
            const done = () => {
              setPipelineCopied(true);
              window.setTimeout(() => setPipelineCopied(false), 1200);
            };
            if (navigator.clipboard && window.isSecureContext) {
              navigator.clipboard.writeText(text).then(done).catch(() => {
                fallbackCopy(text);
                done();
              });
              return;
            }
            fallbackCopy(text);
            done();
          };
          const guideEntries: { stream: string; desc: string; common: boolean }[] = [
            { stream: "lifecycle:start", desc: "Turn begins. Agent acked chat.send and is about to call the LLM.", common: true },
            { stream: "thinking",            desc: "LLM reasoning delta. Codex thinking=low / Claude extended thinking. Many per turn.", common: true },
            { stream: "thinking:first_token",desc: "Marker — first delta of the thinking stream. Persisted to JSONL so reloaded turns show when reasoning began. Only fires when extended thinking is enabled.", common: false },
            { stream: "assistant",           desc: "LLM reply text delta. The string that becomes the assistant message / TTS.", common: true },
            { stream: "agent:first_token",   desc: "Marker — first text delta of the assistant reply (not first tool call). Persisted to JSONL so reloaded turns show when text streaming began. Tool-only turns (NO_REPLY) won't fire this.", common: true },
            { stream: "tool · start",    desc: "Tool function call started. Carries the tool name + args.", common: true },
            { stream: "tool · result",   desc: "Tool returned. OS Server attaches duration to the tool row.", common: true },
            { stream: "lifecycle:end",   desc: "Turn complete. Includes optional usage tokens. OS Server flushes TTS here.", common: true },
            { stream: "error",           desc: "Turn errored mid-run (network, model, quota). Rare — investigate.", common: false },
            { stream: "compaction",      desc: "Auto-compact in progress. Session history is being summarized.", common: false },
            { stream: "item",            desc: "Codex CLI internal: each reasoning / tool / message wrapped as an item.", common: false },
            { stream: "plan",            desc: "Codex CLI planning step. Not used by OS Server.", common: false },
            { stream: "approval",        desc: "Codex CLI approval prompt. Not used by OS Server.", common: false },
            { stream: "command_output",  desc: "Bash tool stdout streaming. Fires for shell commands.", common: false },
            { stream: "patch",           desc: "Codex CLI file patch. Not used by OS Server.", common: false },
          ];
          return (
            <g>
              <rect x={px} y={py} width={pw} height={ph} rx={10}
                fill="var(--lm-card)" fillOpacity={0.55}
                stroke={pipelineColor} strokeOpacity={0.5} strokeWidth={1.5}
                strokeDasharray="3 2"
              />
              <text x={px + 8} y={py + 12}
                fill={pipelineColor} fontSize={7} fontWeight={700}
                fontFamily="monospace" opacity={0.85} style={{ letterSpacing: "0.06em" }}>
                ⟨agent event pipeline⟩
              </text>
              {!isPhoneLayout && <g
                onMouseDown={(e: React.MouseEvent) => { e.stopPropagation(); }}
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); handleCopyPipeline(); }}
                style={{ cursor: "pointer" }}
              >
                <circle cx={copyBtnX} cy={copyBtnY} r={5}
                  fill="var(--lm-card)" fillOpacity={0.7}
                  stroke={pipelineColor} strokeWidth={1} strokeOpacity={0.8}
                />
                <text x={copyBtnX} y={copyBtnY + 1.8} textAnchor="middle"
                  fontSize={5.5} fontWeight={700} fontFamily="monospace"
                  fill={pipelineColor}
                  style={{ pointerEvents: "none" }}>
                  {pipelineCopied ? "✓" : "⎘"}
                </text>
              </g>}
              {!isPhoneLayout && <g
                onMouseDown={(e: React.MouseEvent) => { e.stopPropagation(); }}
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); setPipelineGuideOpen(v => !v); }}
                style={{ cursor: "pointer" }}
              >
                <circle cx={guideBtnX} cy={guideBtnY} r={5}
                  fill={pipelineGuideOpen ? pipelineColor : "var(--lm-card)"}
                  fillOpacity={pipelineGuideOpen ? 0.9 : 0.7}
                  stroke={pipelineColor} strokeWidth={1} strokeOpacity={0.8}
                />
                <text x={guideBtnX} y={guideBtnY + 1.8} textAnchor="middle"
                  fontSize={6.5} fontWeight={700} fontFamily="monospace"
                  fill={pipelineGuideOpen ? "var(--lm-card)" : pipelineColor}
                  style={{ pointerEvents: "none" }}>
                  ?
                </text>
              </g>}
              {!isPhoneLayout && <foreignObject x={px + 6} y={py + 18} width={pw - 12} height={ph - 24} overflow="visible">
                <div
                  // @ts-expect-error xmlns required for foreignObject HTML
                  xmlns="http://www.w3.org/1999/xhtml"
                  onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                  style={{
                    fontFamily: "monospace",
                    fontSize: 6.5,
                    lineHeight: 1.55,
                    color: "var(--lm-text)",
                    overflow: "auto",
                    maxHeight: ph - 24,
                    userSelect: "text",
                    WebkitUserSelect: "text",
                  }}
                >
                  {headerSummary && (
                    <div style={{
                      display: "flex", gap: 8, marginBottom: 4, padding: "2px 4px",
                      fontSize: 6, opacity: 0.95,
                      color: "var(--lm-purple)",
                      borderBottom: "1px dashed color-mix(in srgb, var(--lm-blue) 40%, transparent)",
                      paddingBottom: 3,
                    }}>
                      <span style={{ color: "var(--lm-purple)", display: "inline-flex", alignItems: "center" }}>
                        <Timer size={8} strokeWidth={2.5} />
                      </span>
                      <span>{headerSummary}</span>
                    </div>
                  )}
                  {pipelineRows.length === 0 ? (
                    <div style={{ opacity: 0.6, padding: "6px 4px", color: "var(--lm-text)" }}>
                      (no agent stream events captured for this turn)
                    </div>
                  ) : pipelineRows.map((r, i) => {
                    const c = rowColor(r.kind);
                    const isStream = r.kind === "thinking" || r.kind === "assistant";
                    const isOneShot = r.kind === "lifecycle_start" || r.kind === "lifecycle_end"
                      || r.kind === "agent_first_token" || r.kind === "thinking_first_token"
                      || r.kind === "compaction" || r.kind === "error";
                    const next = pipelineRows[i + 1];
                    const gapMs = next ? next.startMs - r.endMs : 0;
                    return (
                      <div key={i}>
                        <div style={{ display: "flex", gap: 4, padding: "1px 2px", borderLeft: `2px solid ${c}`, paddingLeft: 4, marginBottom: 1 }}>
                          <span style={{ color: c, fontWeight: 700, minWidth: 56 }}>{r.label}</span>
                          {isStream && (
                            <span>
                              <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                                {fmtDur(r.durationMs)}
                              </span>
                              <span style={{ color: "var(--lm-text)", opacity: 0.85 }}>
                                {" "}· {r.chunks} chunks · {fmtChars(r.chars)}
                              </span>
                            </span>
                          )}
                          {r.kind === "tool" && (
                            <span style={{ minWidth: 0, flex: 1 }}>
                              <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                                {r.durationMs > 0 ? fmtDur(r.durationMs) : "…"}
                              </span>
                              {r.detail ? (
                                <span
                                  style={{
                                    color: "var(--lm-text)", opacity: 0.7, marginLeft: 6,
                                    wordBreak: "break-all" as const,
                                    whiteSpace: "pre-wrap" as const,
                                  }}
                                >
                                  {r.detail}
                                </span>
                              ) : null}
                            </span>
                          )}
                          {isOneShot && r.detail && (
                            <span style={{ color: "var(--lm-text)", opacity: 0.7 }}>{r.detail}</span>
                          )}
                        </div>
                        {gapMs > 200 && (
                          <div style={{
                            paddingLeft: 18, fontSize: 5.8, marginBottom: 1,
                            fontStyle: "italic",
                          }}>
                            <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                              ⋯ + {fmtDur(gapMs)}
                            </span>
                            <span style={{ color: "var(--lm-text)", opacity: 0.75 }}>
                              {" "}OS Server waiting next event
                            </span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </foreignObject>}
              {/* Mobile browsers can drop foreignObject HTML; keep a pure SVG text summary. */}
              {isPhoneLayout && (
                <g clipPath={`url(#${pipelineClipId})`} pointerEvents="none">
                  {pipelineRows.length === 0 ? (
                    <text x={px + 10} y={py + 34} fill="var(--lm-text-muted)" fontSize={7} fontFamily="monospace">
                      (no agent events)
                    </text>
                  ) : pipelineRows.slice(0, 11).map((row, index) => {
                    const detail = row.kind === "tool" && row.detail
                      ? ` · ${row.detail.replace(/\s+/g, " ").slice(0, 28)}`
                      : row.kind === "thinking" || row.kind === "assistant"
                        ? ` · ${formatPipelineDuration(row.durationMs)}`
                        : "";
                    return (
                      <text key={`${row.kind}-${index}`} x={px + 10} y={py + 32 + index * 14}
                        fill={pipelineRowColor(row.kind)} fontSize={7.5} fontWeight={700} fontFamily="monospace">
                        {`${row.label}${detail}`}
                      </text>
                    );
                  })}
                  <text x={px + 10} y={py + ph - 12} fill="var(--lm-text-muted)" fontSize={6.5} fontFamily="monospace">
                    View details for full payloads
                  </text>
                </g>
              )}
              {/* Rendered last so it paints above the row foreignObject. */}
              {!isPhoneLayout && pipelineGuideOpen && (
                <foreignObject
                  x={px + pw - 320} y={py + 22} width={320} height={ph - 30}
                  overflow="visible"
                  style={{ pointerEvents: "auto" }}
                >
                  <div
                    // @ts-expect-error xmlns required for foreignObject HTML
                    xmlns="http://www.w3.org/1999/xhtml"
                    onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                    onClick={(e: React.MouseEvent) => e.stopPropagation()}
                    style={{
                      background: "#34D399",
                      border: "1px solid #0F766E",
                      borderRadius: 6,
                      padding: 8,
                      fontFamily: "monospace",
                      fontSize: 7,
                      lineHeight: 1.5,
                      color: "#0A2A1F",
                      maxHeight: ph - 30,
                      overflow: "auto",
                      boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
                      pointerEvents: "auto",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6, alignItems: "center" }}>
                      <span style={{ fontWeight: 700, color: "#0A2A1F", fontSize: 7.5 }}>
                        Agent streams (event:"agent")
                      </span>
                      <button
                        type="button"
                        onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                        onClick={(e: React.MouseEvent) => { e.stopPropagation(); setPipelineGuideOpen(false); }}
                        style={{
                          cursor: "pointer", opacity: 0.85,
                          background: "transparent", border: "none", color: "#0A2A1F",
                          padding: "0 4px", lineHeight: 1,
                          display: "inline-flex", alignItems: "center",
                        }}
                      ><X size={11} strokeWidth={2.5} /></button>
                    </div>
                    <div style={{ opacity: 0.8, marginBottom: 5, fontSize: 6.5, color: "#0A2A1F" }}>
                      Common turn: lifecycle:start → thinking / assistant / tool — lifecycle:end. The other 7 fire only in special situations.
                    </div>
                    {guideEntries.map((e) => (
                      <div key={e.stream} style={{ marginBottom: 4, opacity: e.common ? 1 : 0.7 }}>
                        <span style={{ color: "#0A2A1F", fontWeight: 700 }}>{e.stream}</span>
                        <span style={{ marginLeft: 6, color: "#0A2A1F", opacity: e.common ? 0.9 : 0.75 }}>{e.desc}</span>
                      </div>
                    ))}
                  </div>
                </foreignObject>
              )}
            </g>
          );
        })()}

        {FLOW_NODES.map((node) => {
          // Absorbed into the Event Pipeline rect; still used for edges and visited tracking.
          if (node.id === "agent_thinking" || node.id === "tool_exec") {
            return null;
          }
          const pos = positions[node.id];
          const isActive = node.id === activeStage;
          const isVisited = visitedStages.has(node.id);
          const color = nodeColor(node.id);
          const opacity = nodeOpacity(node.id);
          const lines = nodeInfo[node.id] ?? [];
          const hasInfo = lines.length > 0 && (isActive || isVisited);
          const descLines = node.desc.split(" · ").length;
          const boxAbove = node.id === "agent_call";
          const boxY = boxAbove ? pos.y - nodeR - 4 : pos.y + nodeR + 14 + descLines * 10;
          return (
            <g key={node.id} opacity={opacity}>
              {(() => {
                const shape = node.shape ?? "circle";
                const r = shape === "square" ? gateR : nodeR;
                const fOpacity = isActive ? 0.25 : isVisited ? 0.18 : 0.12;
                const sOpacity = isActive ? 1 : isVisited ? 0.7 : 0.35;
                const sWidth = isActive ? 2.5 : 1.5;
                const glow = isActive ? { filter: `url(#${glowId})` } : undefined;
                const glowR = r + 6;
                const props = { fill: color, fillOpacity: fOpacity, stroke: color, strokeWidth: sWidth, strokeOpacity: sOpacity, style: glow };
                const glowProps = { fill: "none" as const, stroke: color, strokeWidth: 2, opacity: 0.35, style: { filter: `url(#${glowId})` } };

                const hexPoints = (cx: number, cy: number, rad: number) =>
                  Array.from({ length: 6 }, (_, i) => {
                    const angle = (Math.PI / 3) * i - Math.PI / 6;
                    return `${cx + rad * Math.cos(angle)},${cy + rad * Math.sin(angle)}`;
                  }).join(" ");

                const diamondPoints = (cx: number, cy: number, rad: number) =>
                  `${cx},${cy - rad} ${cx + rad},${cy} ${cx},${cy + rad} ${cx - rad},${cy}`;

                switch (shape) {
                  case "hexagon":
                    return (<>
                      {isActive && <polygon points={hexPoints(pos.x, pos.y, glowR)} {...glowProps} />}
                      <polygon points={hexPoints(pos.x, pos.y, r)} {...props} />
                    </>);
                  case "diamond":
                    return (<>
                      {isActive && <polygon points={diamondPoints(pos.x, pos.y, glowR)} {...glowProps} />}
                      <polygon points={diamondPoints(pos.x, pos.y, r)} {...props} />
                    </>);
                  case "square":
                    return (<>
                      {isActive && <rect x={pos.x - glowR} y={pos.y - glowR} width={glowR * 2} height={glowR * 2} rx={12} {...glowProps} />}
                      <rect x={pos.x - r} y={pos.y - r} width={r * 2} height={r * 2} rx={10} {...props} />
                    </>);
                  default:
                    return (<>
                      {isActive && <circle cx={pos.x} cy={pos.y} r={glowR} {...glowProps} />}
                      <circle cx={pos.x} cy={pos.y} r={r} {...props} />
                    </>);
                }
              })()}
              <text x={pos.x} y={pos.y - 6} textAnchor="middle"
                fill={color} fontSize={9} fontWeight={isActive ? 700 : 600}>
                {node.id === "agent_response" && lines.some((l) => l.includes("no reply")) ? "🚫"
                  : node.id === "agent_response" && lines.some((l) => l.includes("no output")) ? "💤"
                  : node.id === "agent_response" && lines.some((l) => l.startsWith('"')) ? "💬"
                  : node.icon} {node.short}
              </text>
              <text x={pos.x} y={pos.y + 6} textAnchor="middle"
                fill={color} fontSize={7} opacity={0.9}>
                {node.label}
              </text>
              {node.desc.split(" · ").map((part, i) => (
                <text key={`d${i}`} x={pos.x} y={pos.y + nodeR + 14 + i * 10} textAnchor="middle"
                  fill={color} fontSize={5.5} opacity={0.6}>
                  {part}
                </text>
              ))}

              {hasInfo && !isPhoneLayout && (() => {
                const textLines = lines.filter((l) => !l.startsWith("🖼"));
                const isWide = node.id === "agent_call";
                const boxW = isWide ? 480 : 190;
                const halfDefault = 95;
                const xCentered = isWide ? pos.x - halfDefault : pos.x - boxW / 2;
                const boxRight = node.id === "agent_call";
                const boxX = boxRight
                  ? pos.x + nodeR + 6
                  : (boxAbove ? pos.x + nodeR - boxW : xCentered);
                return (
                  <foreignObject
                    x={boxX} y={boxY - 2}
                    width={boxW} height={1}
                    overflow="visible"
                  >
                    <div
                      // @ts-expect-error xmlns required for foreignObject HTML
                      xmlns="http://www.w3.org/1999/xhtml"
                      onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                      style={{
                        background: "color-mix(in srgb, var(--lm-card) 70%, transparent)",
                        border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
                        borderRadius: 4,
                        padding: "4px 6px",
                        fontFamily: "monospace",
                        fontSize: 5.5,
                        lineHeight: 1.7,
                        color: color,
                        opacity: 0.95,
                        ...(boxAbove ? { transform: "translateY(-100%)" } : {}),
                        userSelect: "text",
                        WebkitUserSelect: "text",
                        cursor: "text",
                        wordBreak: "break-all" as const,
                        whiteSpace: "pre-wrap" as const,
                        maxWidth: boxW,
                      }}
                    >
                      {textLines.map((line, i) => (
                        <div key={i} style={{
                          color: line.startsWith("⏱") ? "var(--lm-amber)" : color,
                          fontWeight: line.startsWith("⏱") ? 700 : 400,
                        }}>
                          {line}
                        </div>
                      ))}
                    </div>
                  </foreignObject>
                );
              })()}
            </g>
          );
        })}

        {sensingSnapshotUrls.length > 0 && sensingSnapshotUrls.map((url, i) => {
          const imgW = 100;
          const imgH = 75;
          const snapX = 80 + i * (imgW + 10);
          const snapY = 340;
          return (
            <g key={i}>
              <rect
                x={snapX - imgW / 2} y={snapY - imgH / 2}
                width={imgW} height={imgH}
                rx={6} ry={6}
                fill="var(--lm-card)" stroke="var(--lm-amber)" strokeWidth={1}
                opacity={0.9}
              />
              <image
                href={url}
                x={snapX - imgW / 2 + 2} y={snapY - imgH / 2 + 2}
                width={imgW - 4} height={imgH - 4}
                preserveAspectRatio="xMidYMid meet"
                clipPath={`inset(0 round 4px)`}
                style={{ cursor: "pointer" }}
                onClick={(e) => { e.stopPropagation(); setLightboxUrl(url); }}
              />
              {i === 0 && (
                <text
                  x={snapX} y={snapY + imgH / 2 + 10}
                  textAnchor="middle"
                  fill="var(--lm-amber)" fontSize={6} fontWeight={600}
                >
                  📷 {sensingSnapshotUrls.length > 1 ? `${sensingSnapshotUrls.length} Snapshots` : "Snapshot"}
                </text>
              )}
            </g>
          );
        })}

        {imageSentToAgent && sensingSnapshotUrls.length > 0 && sensingSnapshotUrls.slice(0, 1).map((url, i) => {
          const imgW = 80;
          const imgH = 60;
          const snapX = 515;
          const snapY = 145;
          return (
            <g key={`agent-snap-${i}`}>
              <rect
                x={snapX - imgW / 2} y={snapY - imgH / 2}
                width={imgW} height={imgH}
                rx={6} ry={6}
                fill="var(--lm-card)" stroke="var(--lm-blue)" strokeWidth={1}
                opacity={0.9}
              />
              <image
                href={url}
                x={snapX - imgW / 2 + 2} y={snapY - imgH / 2 + 2}
                width={imgW - 4} height={imgH - 4}
                preserveAspectRatio="xMidYMid meet"
                clipPath={`inset(0 round 4px)`}
                style={{ cursor: "pointer" }}
                onClick={(e) => { e.stopPropagation(); setLightboxUrl(url); }}
              />
              <text
                x={snapX} y={snapY + imgH / 2 + 10}
                textAnchor="middle"
                fill="var(--lm-blue)" fontSize={6} fontWeight={600}
              >
                📷 → Agent
              </text>
            </g>
          );
        })}

        {agentSnapshotUrls.map((url, i) => {
          const imgW = 100;
          const imgH = 75;
          const snapX = 200 + i * (imgW + 10);
          const snapY = 1160;
          return (
            <g key={`agent-camera-${url}`}>
              <rect x={snapX - imgW / 2} y={snapY - imgH / 2} width={imgW} height={imgH}
                rx={6} ry={6} fill="var(--lm-card)" stroke="var(--lm-amber)" strokeWidth={1} opacity={0.9} />
              <image href={url} x={snapX - imgW / 2 + 2} y={snapY - imgH / 2 + 2}
                width={imgW - 4} height={imgH - 4} preserveAspectRatio="xMidYMid meet"
                clipPath={`inset(0 round 4px)`} style={{ cursor: "pointer" }}
                onClick={(e) => { e.stopPropagation(); setLightboxUrl(url); }} />
              {i === 0 && <text x={snapX} y={snapY + imgH / 2 + 10} textAnchor="middle"
                fill="var(--lm-amber)" fontSize={6} fontWeight={600}>📷 Tool snapshot</text>}
            </g>
          );
        })}
      </svg>

      {isPhoneLayout && (
        <button
          type="button"
          className="lm-u-btn"
          onClick={() => setMobileDetailsOpen(true)}
          style={{
            alignSelf: "center", marginTop: 4, padding: "7px 12px", borderRadius: 7,
            background: "var(--lm-blue-dim, rgba(96,165,250,0.14))",
            border: "1px solid var(--lm-blue)", color: "var(--lm-blue)",
            fontSize: 12, fontWeight: 700,
          }}
        >LLM / Tool / Curl details</button>
      )}

      {isPhoneLayout && mobileDetailsOpen && createPortal(
        <div
          className={`lm-root ${themeClass}`}
          onClick={() => setMobileDetailsOpen(false)}
          style={{
            position: "fixed", inset: 0, zIndex: 102,
            display: "flex", alignItems: "flex-end",
            background: "rgba(0,0,0,0.62)", backdropFilter: "blur(3px)",
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-label="Agent details"
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "100%", maxHeight: "78vh", overflowY: "auto",
              background: "var(--lm-card)", border: "1px solid var(--lm-border)",
              borderRadius: "16px 16px 0 0", padding: "16px 14px 24px",
              boxSizing: "border-box",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 14 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-text)" }}>Agent details</div>
                <div style={{ fontSize: 11, color: "var(--lm-text-muted)", marginTop: 2 }}>LLM, tools, curl arguments and node output</div>
              </div>
              <button
                type="button"
                className="lm-u-btn"
                onClick={() => setMobileDetailsOpen(false)}
                aria-label="Close agent details"
                style={{ width: 32, height: 32, padding: 0, borderRadius: 7, fontSize: 17, color: "var(--lm-text)" }}
              >×</button>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {pipelineRows.length === 0 ? (
                <div style={{ padding: 12, borderRadius: 8, background: "var(--lm-surface)", color: "var(--lm-text-muted)", fontSize: 12 }}>
                  No agent stream events were captured for this turn.
                </div>
              ) : pipelineRows.map((row, index) => {
                const color = pipelineRowColor(row.kind);
                const isStream = row.kind === "thinking" || row.kind === "assistant";
                return (
                  <div key={`${row.kind}-${index}`} style={{ borderLeft: `3px solid ${color}`, borderRadius: 7, padding: "8px 10px", background: "var(--lm-surface)" }}>
                    <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ color, fontSize: 12, fontWeight: 700 }}>{row.label}</span>
                      {isStream && <span style={{ color: "var(--lm-purple)", fontSize: 11, fontWeight: 600 }}>{formatPipelineDuration(row.durationMs)} · {row.chunks} chunks</span>}
                      {row.kind === "tool" && <span style={{ color: "var(--lm-purple)", fontSize: 11, fontWeight: 600 }}>{row.durationMs > 0 ? formatPipelineDuration(row.durationMs) : "…"}</span>}
                    </div>
                    {row.detail && (
                      <pre style={{ margin: "7px 0 0", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11, lineHeight: 1.45, color: "var(--lm-text-dim)" }}>{row.detail}</pre>
                    )}
                  </div>
                );
              })}

              {mobileNodeDetails.map((detail) => (
                <div key={detail.label} style={{ borderLeft: "3px solid var(--lm-teal)", borderRadius: 7, padding: "8px 10px", background: "var(--lm-surface)" }}>
                  <div style={{ color: "var(--lm-teal)", fontSize: 12, fontWeight: 700, marginBottom: 5 }}>{detail.label}</div>
                  {detail.lines.map((line, index) => (
                    <pre key={index} style={{ margin: index === 0 ? 0 : "6px 0 0", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11, lineHeight: 1.45, color: "var(--lm-text-dim)" }}>{line}</pre>
                  ))}
                </div>
              ))}
            </div>
          </section>
        </div>,
        document.body,
      )}

      {/* Portalled so position:fixed anchors to the viewport (ancestors are transformed). */}
      {lightboxUrl && createPortal(
        <div
          className={`lm-root ${themeClass}`}
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
            aria-label="Close"
            style={{
              position: "absolute", top: 16, right: 16,
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", width: 36, height: 36,
              borderRadius: "50%", cursor: "pointer",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <X size={20} strokeWidth={2.25} />
          </button>
          <img
            src={lightboxUrl}
            onClick={(e) => e.stopPropagation()}
            style={{ width: "85vw", height: "85vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
          />
        </div>,
        document.body,
      )}

      <div style={{
        display: "flex", gap: 16, justifyContent: "center", alignItems: "center",
        fontSize: 10, color: "var(--lm-text-muted)", padding: "8px 0 4px",
      }}>
        {([
          { label: "Input", color: "var(--lm-amber)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <polygon points={Array.from({ length: 6 }, (_, i) => {
                const a = (Math.PI / 3) * i - Math.PI / 6;
                return `${6 * Math.cos(a)},${6 * Math.sin(a)}`;
              }).join(" ")} fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Process", color: "var(--lm-blue)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <circle r="6" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Output", color: "var(--lm-purple)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <polygon points="0,-7 7,0 0,7 -7,0" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Gate", color: "var(--lm-teal)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <rect x="-5.5" y="-5.5" width="11" height="11" rx="2.5" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
        ] as const).map((item) => (
          <span key={item.label} style={{ display: "inline-flex", alignItems: "center", gap: 4, color: item.color }}>
            {item.shape(item.color)} {item.label}
          </span>
        ))}
      </div>

      <div style={{
        position: "absolute", bottom: 6, right: 6,
        display: "flex", gap: 4, alignItems: "center",
      }}>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)", marginRight: 4 }}>
          {Math.round(zoom * 100)}%
        </span>
        <button onClick={() => setZoom((z) => Math.max(0.4, z - 0.2))} className="lm-u-btn" style={{
          width: 22, height: 22, borderRadius: 5,
          color: "var(--lm-text-dim)", fontSize: 12, lineHeight: 1, padding: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>−</button>
        <button onClick={resetView} className="lm-u-btn" style={{
          width: 22, height: 22, borderRadius: 5,
          color: "var(--lm-text-dim)", fontSize: 12, lineHeight: 1, padding: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>⟳</button>
        <button onClick={() => setZoom((z) => Math.min(4, z + 0.2))} className="lm-u-btn" style={{
          width: 22, height: 22, borderRadius: 5,
          color: "var(--lm-text-dim)", fontSize: 12, lineHeight: 1, padding: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>+</button>
      </div>
    </div>
  );
}
