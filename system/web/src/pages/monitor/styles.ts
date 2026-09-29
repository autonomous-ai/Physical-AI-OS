import type React from "react";

export const S = {
  root: {
    display: "flex",
    height: "100vh",
    background: "var(--lm-bg)",
    color: "var(--lm-text)",
    fontFamily: "'Inter', 'Segoe UI', sans-serif",
    fontSize: 14,
  } as React.CSSProperties,
  sidebar: {
    width: 250,
    flexShrink: 0,
    background: "var(--lm-sidebar)",
    borderRight: "1px solid var(--lm-border)",
    display: "flex",
    flexDirection: "column" as const,
  },
  sidebarLogo: {
    padding: "18px 16px 14px",
    borderBottom: "1px solid var(--lm-border)",
  },
  sidebarLogoName: {
    fontSize: 15,
    fontWeight: 700,
    color: "var(--lm-amber)",
    letterSpacing: "-0.3px",
  },
  sidebarLogoSub: {
    fontSize: 10,
    color: "var(--lm-text-muted)",
    marginTop: 2,
  },
  main: {
    flex: 1,
    minWidth: 0,
    display: "flex",
    flexDirection: "column" as const,
    overflow: "hidden",
  },
  topbar: {
    padding: "10px 20px",
    borderBottom: "1px solid var(--lm-border)",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    flexShrink: 0,
  },
  content: {
    flex: 1,
    minHeight: 0,
    overflowY: "auto" as const,
    padding: "20px",
  },
  grid2: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 14,
  },
  grid3: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr 1fr",
    gap: 14,
  },
  card: {
    background: "var(--lm-card)",
    border: "1px solid var(--lm-border)",
    borderRadius: 14,
    padding: 16,
    boxShadow: "0 1px 2px rgba(0,0,0,0.18), 0 8px 24px -16px rgba(0,0,0,0.5)",
  },
  cardLabel: {
    fontSize: 10,
    fontWeight: 600,
    color: "var(--lm-text)",
    textTransform: "uppercase" as const,
    letterSpacing: "0.08em",
    marginBottom: 12,
  },
};
