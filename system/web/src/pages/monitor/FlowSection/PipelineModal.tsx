import type React from "react";

// Full-screen modal that hosts the Turn Pipeline content on mobile.
export function PipelineModal({
  onClose,
  title,
  children,
}: {
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      id="FLOW_PIPELINE_MODAL_OVERLAY" data-region="FLOW_PIPELINE_MODAL_OVERLAY"
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 100,
        background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", flexDirection: "column",
      }}
    >
      <div
        id="FLOW_PIPELINE_MODAL_PANEL" data-region="FLOW_PIPELINE_MODAL_PANEL"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--lm-card)",
          borderTop: "1px solid var(--lm-border)",
          padding: 10,
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minHeight: 0,
          overflow: "hidden",
        }}
      >
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          marginBottom: 8, flexShrink: 0,
        }}>
          <span style={{
            fontSize: 11, fontWeight: 700, color: "var(--lm-text)",
            textTransform: "uppercase", letterSpacing: "0.08em",
          }}>{title ?? "Turn Pipeline"}</span>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: "var(--lm-text)", cursor: "pointer", fontSize: 16,
              width: 32, height: 32, borderRadius: 6, padding: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >✕</button>
        </div>
        <div style={{
          flex: 1, minHeight: 0, display: "flex", flexDirection: "column",
          overflow: "hidden",
        }}>
          {children}
        </div>
      </div>
    </div>
  );
}
