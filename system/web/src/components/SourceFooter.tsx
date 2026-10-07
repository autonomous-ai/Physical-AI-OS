// GPL v3 §6: link to the corresponding source on every page.
import { useLocation } from "react-router-dom";
import { C } from "@/components/setup/shared";

export function SourceFooter({ inline = false }: { inline?: boolean }) {
  const { pathname } = useLocation();
  if (!inline && ["/monitor", "/setting"].includes(pathname)) return null;
  return (
    <a
      href="https://github.com/autonomous-ai/autonomous-os"
      target="_blank"
      rel="noopener noreferrer"
      style={{
        position: inline ? "static" : "fixed", right: 8, bottom: 6,
        display: "block", flexShrink: 0, textAlign: "right",
        ...(inline ? { borderTop: `1px solid ${C.border}` } : {}),
        fontSize: inline ? 11 : 10, color: inline ? C.textDim : C.textMuted,
        textDecoration: "none", opacity: inline ? 1 : 0.7,
        padding: inline ? "5px 12px" : "2px 6px", borderRadius: inline ? 0 : 4,
        background: inline ? C.bg : "transparent",
        pointerEvents: "auto", zIndex: 1,
        fontFamily: "ui-monospace, monospace",
      }}
      title="Source code (GPL v3)"
    >
      {inline ? "Source code · GPL v3 ↗" : "⌥ github.com/autonomous-ai/autonomous-os"}
    </a>
  );
}
