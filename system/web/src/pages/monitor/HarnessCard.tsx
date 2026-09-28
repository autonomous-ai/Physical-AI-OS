import { useEffect, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import { Laptop } from "lucide-react";
import { harnessRequest } from "./harness-api";
import { HarnessVoiceMode } from "./HarnessVoiceMode";
import { S } from "./styles";

interface HarnessStatus {
  paired: boolean;
  connected: boolean;
  machine_id?: string;
  machine_name?: string;
  error?: string;
  pairing?: boolean;
}

interface HarnessPairInfo {
  code?: string;
  expires_at?: number;
  pairing: boolean;
  state: string;
  machine_id?: string;
  error?: string;
}


const buttonStyle: CSSProperties = {
  padding: "8px 12px", borderRadius: 4, border: "1px solid var(--lm-border)",
  background: "var(--lm-surface)", color: "var(--lm-text)", fontSize: 12,
};

export function HarnessCard() {
  const [status, setStatus] = useState<HarnessStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [pairInfo, setPairInfo] = useState<HarnessPairInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const poll = async () => {
      try {
        const [result, pending] = await Promise.all([
          harnessRequest("/status", { signal: controller.signal }),
          harnessRequest("/pair/status", { signal: controller.signal, cache: "no-store" }),
        ]);
        if (disposed) return;
        if (!result || typeof result.paired !== "boolean" || typeof result.connected !== "boolean") {
          throw new Error("Harness returned an invalid connection status.");
        }
        setStatus(result as HarnessStatus);
        setPairInfo(pending as HarnessPairInfo);
        setConnectionError(null);
      } catch (cause) {
        if (!disposed) setConnectionError(cause instanceof Error ? cause.message : "Could not read Harness status.");
      } finally {
        if (!disposed) timer = setTimeout(() => { void poll(); }, 2000);
      }
    };
    void poll();
    return () => {
      disposed = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [busy, refresh]);

  const handlePair = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || status?.pairing) return;
    setBusy(true);
    setError(null);
    try {
      const pending = await harnessRequest("/pair", {
        method: "POST",
      });
      setPairInfo(pending as HarnessPairInfo);
      setStatus(previous => ({ ...previous, paired: false, connected: false, pairing: true }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start pairing.");
    } finally {
      // Read status after an ambiguous failure; never replay a pairing POST.
      setBusy(false);
    }
  };

  const handleUnpair = async () => {
    if (busy || !window.confirm("Disconnect Harness and remove this computer’s pairing from the robot?")) return;
    setBusy(true);
    setError(null);
    try {
      await harnessRequest("", { method: "DELETE" });
      setStatus({ paired: false, connected: false });
      setPairInfo(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not remove pairing.");
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await harnessRequest("/pair/cancel", { method: "POST" });
      setPairInfo(null);
      setStatus(previous => previous ? { ...previous, pairing: false } : previous);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not cancel pairing.");
    } finally {
      setBusy(false);
    }
  };

  const pairing = status?.pairing === true || pairInfo?.pairing === true;
  const hasTrust = status?.paired === true || Boolean(status?.machine_id);
  const remaining = Math.max(0, Math.ceil(((pairInfo?.expires_at ?? 0) - Date.now()) / 1000));
  const connected = status?.connected === true && !connectionError;
  const stateLabel = connectionError ? "STATUS UNAVAILABLE" : pairing ? "PAIRING" : connected ? "CONNECTED"
    : status?.paired ? "OFFLINE" : hasTrust ? "PAIRING INCOMPLETE"
      : status ? "NOT PAIRED" : "LOADING";

  return (
    <div className="lm-mon-card" style={{ ...S.card, boxShadow: undefined }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 12 }}>
        <div style={{ ...S.cardLabel, display: "flex", alignItems: "center", gap: 8, marginBottom: 0 }}>
          <span className="lm-mon-chip" aria-hidden><Laptop size={13} /></span>
          <span>Harness</span>
        </div>
        <span role="status" style={{ fontSize: 10, color: connected ? "var(--lm-green)" : "var(--lm-text-muted)" }}>
          {stateLabel}
        </span>
      </div>
      {status && hasTrust && !pairing && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <strong style={{ fontSize: 13 }}>{status.machine_name || "Paired computer"}</strong>

          {status.paired && <span style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>
            Pairing saved · {connected ? "Connected to this computer." : "Pairing does not mean the computer is online."}
          </span>}
          {!connected && <span style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>
            {connectionError
              ? "Cannot check the connection right now. Refresh status to try again."
              : status.paired
              ? "Harness is offline. New requests cannot reach this computer until it reconnects. Keep Harness running on the same local network; you do not need to pair again just because it is offline."
              : "Pairing has not finished. Wait for the computer to reconnect, or unpair before trying again."}
          </span>}
          <button type="button" disabled={busy} onClick={() => { void handleUnpair(); }} style={buttonStyle}>
            {busy ? "Disconnecting…" : "Unpair computer"}
          </button>
        </div>
      )}
      {status?.paired && <HarnessVoiceMode key={status.machine_id} connected={connected} />}
      {pairing && <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>
          Open Harness Desktop → Settings → Devices on your computer.
          Select this Autonomous robot and enter the code below.
        </span>
        <strong aria-label="Pairing code" style={{ fontSize: 24, letterSpacing: "0.18em" }}>
          {remaining > 0 && pairInfo?.code ? pairInfo.code : "Expired"}
        </strong>
        <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>
          {remaining > 0 ? `Expires in ${remaining} seconds` : "Cancel and generate a new code to try again."}
        </span>
        <button type="button" disabled={busy} onClick={() => { void handleCancel(); }} style={buttonStyle}>
          Cancel pairing
        </button>
      </div>}
      {status && !hasTrust && !pairing && (
        <form onSubmit={handlePair} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <p style={{ margin: 0, fontSize: 12, lineHeight: 1.5, color: "var(--lm-text-dim)" }}>
            Generate a code here, then open Harness Desktop → Settings → Devices on your computer.
            Select this Autonomous robot and enter the code. Keep both on the same local network.
          </p>
          <button type="submit" disabled={busy}
            style={{ ...buttonStyle, color: "var(--lm-green)" }}>{busy ? "Preparing…" : "Generate pairing code"}</button>
          <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>
            Codes expire after 60 seconds. Pairing gives access to this computer’s agents.
          </span>
        </form>
      )}
      {(error || connectionError || status?.error || pairInfo?.error) && <p role="alert" style={{ fontSize: 12, color: "var(--lm-red)" }}>
        {error || connectionError || status?.error || pairInfo?.error}
      </p>}
      <button type="button" disabled={busy} onClick={() => { setError(null); setRefresh(value => value + 1); }}
        style={{ ...buttonStyle, marginTop: 10 }}>Refresh status</button>
    </div>
  );
}
