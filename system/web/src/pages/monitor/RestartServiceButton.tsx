import { useEffect, useState } from "react";
import { getApiToken } from "@/lib/api";
import { API } from "./types";

export function RestartServiceButton({ target, disabled }: {
  target: "os-server" | "hal";
  disabled: boolean;
}) {
  const [state, setState] = useState<"idle" | "sending" | "queued">("idle");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (state !== "queued") return;
    // Click cooldown, not a health confirmation.
    const timer = setTimeout(() => setState("idle"), 15000);
    return () => clearTimeout(timer);
  }, [state]);

  const restart = async () => {
    if (disabled || state !== "idle") return;
    setState("sending");
    setError(null);
    try {
      const token = getApiToken();
      const response = await fetch(`${API}/system/restart/${target}`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: AbortSignal.timeout(15000),
      });
      const body = await response.json();
      if (!response.ok || body.status !== 1) {
        throw new Error(typeof body.message === "string" ? body.message : "Restart request failed");
      }
      setState("queued");
    } catch (err) {
      setError(err instanceof TypeError || (err instanceof DOMException && err.name === "TimeoutError")
        ? "Connection lost. Check service status before retrying."
        : err instanceof Error ? err.message : "Restart request failed");
      setState("idle");
    }
  };

  return (
    <span style={{ minWidth: 0, textAlign: "right" }}>
      <button
        className="lm-u-btn"
        onClick={() => void restart()}
        disabled={disabled || state !== "idle"}
        aria-label={`Restart ${target === "hal" ? "HAL" : "OS Server"} service`}
        title={state === "queued" ? "Restart scheduled; monitor status will refresh automatically" : `Restart ${target} service`}
        style={{
          padding: "3px 8px", fontSize: 9, fontWeight: 600,
          border: "1px solid var(--lm-border)", borderRadius: 4,
          background: "transparent", color: "var(--lm-amber)",
          cursor: disabled || state !== "idle" ? "wait" : "pointer",
          opacity: disabled || state !== "idle" ? 0.6 : 1,
        }}
      >
        {state === "sending" ? "sending…" : state === "queued" ? "queued" : "restart"}
      </button>
      {error && <span role="alert" style={{ display: "block", fontSize: 9, color: "var(--lm-red)", overflowWrap: "anywhere" }}>{error}</span>}
    </span>
  );
}
