import { X } from "lucide-react";
import type { DisplayEvent } from "../types";
import type { FlowStage, ActiveFlowStage } from "./types";
import { FLOW_NODES } from "./types";
import { FlowDiagram } from "./FlowDiagram";

export function CanvasModal({
  activeStage,
  visitedStages,
  turnEvents,
  onClose,
}: {
  activeStage: ActiveFlowStage;
  visitedStages: Set<FlowStage>;
  turnEvents: DisplayEvent[];
  onClose: () => void;
}) {
  return (
    <div
      id="FLOW_CANVAS_MODAL_OVERLAY" data-region="FLOW_CANVAS_MODAL_OVERLAY"
      style={{
        position: "fixed", inset: 0, zIndex: 100,
        background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
      onClick={onClose}
    >
      <div
        id="FLOW_CANVAS_MODAL_PANEL" data-region="FLOW_CANVAS_MODAL_PANEL"
        style={{
          background: "var(--lm-card)", border: "1px solid var(--lm-border)",
          borderRadius: 16, padding: 32, maxWidth: 820, width: "90vw",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-text)" }}>Turn Workflow</span>
          <button onClick={onClose} aria-label="Close" style={{
            background: "none", border: "none", color: "var(--lm-text-muted)",
            cursor: "pointer", lineHeight: 1,
            display: "inline-flex", alignItems: "center",
          }}><X size={16} strokeWidth={2.25} /></button>
        </div>

        <FlowDiagram activeStage={activeStage} visitedStages={visitedStages} turnEvents={turnEvents} />
        <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginTop: 8, textAlign: "center" as const }}>
          Scroll to zoom · Drag to pan · Click ⟳ to reset · Zoom in to see tool/func details
        </div>

        <div style={{ marginTop: 20, display: "flex", flexWrap: "wrap" as const, gap: 8 }}>
          {FLOW_NODES.map((n) => (
            <div key={n.id} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10.5, color: "var(--lm-text-dim)" }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: n.color, display: "inline-block", flexShrink: 0 }} />
              {n.label}
            </div>
          ))}
        </div>

        <div style={{ marginTop: 16, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, fontSize: 10.5, color: "var(--lm-text-dim)" }}>
          <div style={{ padding: "8px 12px", borderRadius: 8, background: "var(--lm-surface)", border: "1px solid var(--lm-border)" }}>
            <span style={{ color: "var(--lm-green)", fontWeight: 600 }}>Fast path (~50ms)</span><br />
            Sensing → Intent Check → Local Match → TTS Speak → Idle
          </div>
          <div style={{ padding: "8px 12px", borderRadius: 8, background: "var(--lm-surface)", border: "1px solid var(--lm-border)" }}>
            <span style={{ color: "var(--lm-blue)", fontWeight: 600 }}>Agent path (~2–5s)</span><br />
            Sensing → Intent Check → Agent Call → Thinking → [Tools] → Response → TTS → Idle
          </div>
        </div>
      </div>
    </div>
  );
}
