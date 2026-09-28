// GPL v3 §6: link to the corresponding source on every page.
import { C } from "@/components/setup/shared";

export function SourceFooter() {
  return (
    <a
      href="https://github.com/autonomous-ai/autonomous-os"
      target="_blank"
      rel="noopener noreferrer"
      style={{
        position: "fixed", right: 8, bottom: 6,
        fontSize: 10, color: C.textMuted,
        textDecoration: "none", opacity: 0.7,
        padding: "2px 6px", borderRadius: 4,
        background: "transparent",
        pointerEvents: "auto", zIndex: 1,
        fontFamily: "ui-monospace, monospace",
      }}
      title="Source code (GPL v3)"
    >
      ⌥ github.com/autonomous-ai/autonomous-os
    </a>
  );
}
