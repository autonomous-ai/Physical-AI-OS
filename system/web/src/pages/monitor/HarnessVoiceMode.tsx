import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { HarnessQuestion } from "./HarnessQuestion";
import { harnessRequest } from "./harness-api";

interface VoiceMode {
  supported: boolean;
  enabled: boolean;
  generation: number;
  machineId: string;
  agentId: string;
  agentName?: string;
  focusRevision?: string;
  focusAvailable: boolean;
  pending?: { idempotencyKey: string; agentId: string; runId: string };
  error?: string;
}
const control: CSSProperties = {
  padding: "8px 12px", borderRadius: 4, border: "1px solid var(--lm-border)",
  background: "var(--lm-surface)", color: "var(--lm-text)", fontSize: 12,
};

export function HarnessVoiceMode({ connected }: { connected: boolean }) {
  const [mode, setMode] = useState<VoiceMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  useEffect(() => {
    if (busy) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const result = await harnessRequest("/voice-mode", { signal: controller.signal, cache: "no-store" });
        if (disposed) return;
        setMode(result as VoiceMode);
        setPollError(null);
      } catch (cause) {
        if (!disposed) setPollError(cause instanceof Error ? cause.message : "Could not read voice mode.");
      } finally {
        if (!disposed) timer = setTimeout(() => { void poll(); }, 2000);
      }
    };
    void poll();
    return () => { disposed = true; controller.abort(); if (timer) clearTimeout(timer); };
  }, [busy, refresh]);

  const mutate = async (path: string, body: object, method = "POST") => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await harnessRequest(path, { method, body: JSON.stringify(body) });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update Harness voice mode.");
    } finally {
      // Refresh actual state after every result, including ambiguous network failures.
      setRefresh(value => value + 1);
      setBusy(false);
    }
  };

  const unavailable = busy || !mode || Boolean(pollError);
  return <section aria-label="Harness-only voice" style={{ borderTop: "1px solid var(--lm-border)", marginTop: 14, paddingTop: 14, display: "flex", flexDirection: "column", gap: 10, fontSize: 12 }}>
    {mode?.supported === true && <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <input type="checkbox" role="switch" checked={mode?.enabled ?? false}
        disabled={unavailable}
        onChange={event => { void mutate("/voice-mode", { enabled: event.target.checked }, "PUT"); }} />
      <strong>Harness-only voice</strong>
    </label>}
    {mode?.supported === true && <p style={{ margin: 0, lineHeight: 1.5, color: "var(--lm-text-dim)" }}>
      Send your spoken requests directly to the agent focused in the Harness app. Replies play on the robot as usual.
      This mode turns off when the robot service restarts. Text chat keeps its normal behavior.
    </p>}
    <div role="status" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <strong>Focused Harness agent</strong>
      {pollError ? <span>Focus status is unavailable. Retrying…</span> : !connected ? <span>
        Harness is offline. Reconnect the computer to sync focus and deliver voice requests.
      </span> : mode?.focusAvailable && mode.agentId ? <>
        <span>{mode.agentName || mode.agentId}{mode.agentName ? ` · ${mode.agentId}` : ""}</span>
        <span style={{ color: "var(--lm-text-dim)" }}>Synced from the Harness app, including while voice mode is off.</span>
      </> : <span>{mode ? "Waiting for focus from Harness. Open an agent pane in the Harness app. A CLI that supports focus sync is required." : "Loading Harness focus…"}</span>}
      {mode?.enabled && (!connected || !mode.focusAvailable) && <span>
        Voice requests cannot be delivered until a focused agent is available. You can turn this mode off to use the robot agent.
      </span>}
    </div>
    {mode?.pending && <div role="status" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <strong>Delivery is not confirmed</strong>
      <span>The last request may already be running. You can send a new request; the previous request will not be retried.</span>
      <button type="button" style={control} disabled={busy || !connected} onClick={() => { void mutate("/voice-mode/receipt", {}); }}>Check delivery</button>
      <button type="button" style={control} disabled={busy} onClick={() => {
        if (window.confirm("Dismiss this delivery warning without retrying the request? It may still run on Harness.")) {
          void mutate("/voice-mode/resolve", { resolution: "do_not_retry", idempotencyKey: mode.pending?.idempotencyKey });
        }
      }}>Dismiss without retrying</button>
    </div>}
    {connected && !pollError && mode?.focusAvailable && mode.agentId && <HarnessQuestion key={`${mode.machineId}:${mode.agentId}:${mode.focusRevision}`} connected={connected} disabled={unavailable} refresh={refresh} onAnswer={answers => mutate("/voice-mode/answer", answers)} />}
    {(error || pollError || mode?.error) && <p role="alert" style={{ margin: 0, color: "var(--lm-red)" }}>{error || pollError || mode?.error}</p>}
  </section>;
}
