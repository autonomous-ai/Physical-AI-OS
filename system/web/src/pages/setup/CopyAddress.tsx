import { useState } from "react";
import { C } from "@/components/setup/shared";

// Device URL with a one-tap Copy button, shown before the operator switches networks.
export function CopyAddress({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  const text = url;
  const flashCopied = () => {
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  const legacyCopy = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      ta.setAttribute("readonly", "");
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) flashCopied();
    } catch {
      /* copy unsupported — user can still select the text manually */
    }
  };
  const copy = () => {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(flashCopied, legacyCopy);
    } else {
      legacyCopy();
    }
  };
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 8, padding: "8px 10px",
    }}>
      <span style={{
        flex: 1, textAlign: "left", fontSize: 13.5, color: C.text,
        fontFamily: "ui-monospace, monospace", overflow: "hidden", textOverflow: "ellipsis",
      }}>
        {text}
      </span>
      <button
        type="button"
        onClick={copy}
        className={`lm-btn lm-btn-ghost${copied ? " lm-copied" : ""}`}
        style={{ padding: "5px 10px", fontSize: 11, flexShrink: 0, transition: "background 0.15s, color 0.15s, border-color 0.15s", minWidth: 64 }}
      >
        {copied ? "Copied ✓" : "Copy"}
      </button>
    </div>
  );
}
