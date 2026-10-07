import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Laptop } from "lucide-react";
import { harnessRequest } from "./harness-api";
import { HarnessVoiceMode } from "./HarnessVoiceMode";

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
    <div className="lm-mon-card lm-connection-card">
      <div className="lm-connection-header">
        <h2><span className="lm-mon-chip" aria-hidden><Laptop size={16} /></span>Harness</h2>
        <span role="status" className={`lm-connection-status ${connected ? "is-connected" : status?.paired ? "is-offline" : ""}`}>
          {stateLabel}
        </span>
      </div>
      <p className="lm-connection-description">Access agents running on your paired computer.</p>
      {status && hasTrust && !pairing && (
        <div className="lm-connection-stack">
          <strong className="lm-connection-machine">{status.machine_name || "Paired computer"}</strong>

          {status.paired && <span className="lm-connection-note">
            Pairing saved · {connected ? "Connected to this computer." : "Pairing does not mean the computer is online."}
          </span>}
          {!connected && <span className="lm-connection-note">
            {connectionError
              ? "Cannot check the connection right now. Refresh status to try again."
              : status.paired
              ? "Harness is offline. New requests cannot reach this computer until it reconnects. Keep Harness running on the same local network; you do not need to pair again just because it is offline."
              : "Pairing has not finished. Wait for the computer to reconnect, or unpair before trying again."}
          </span>}
          <button type="button" disabled={busy} onClick={() => { void handleUnpair(); }} className="lm-connection-button">
            {busy ? "Disconnecting…" : "Unpair computer"}
          </button>
        </div>
      )}
      {status?.paired && <HarnessVoiceMode key={status.machine_id} connected={connected} />}
      {pairing && <div className="lm-connection-stack">
        <span className="lm-connection-note">
          Open Harness Desktop → Settings → Devices on your computer.
          Select this Autonomous robot and enter the code below.
        </span>
        <strong aria-label="Pairing code" className="lm-connection-code">
          {remaining > 0 && pairInfo?.code ? pairInfo.code : "Expired"}
        </strong>
        <span className="lm-connection-note">
          {remaining > 0 ? `Expires in ${remaining} seconds` : "Cancel and generate a new code to try again."}
        </span>
        <button type="button" disabled={busy} onClick={() => { void handleCancel(); }} className="lm-connection-button">
          Cancel pairing
        </button>
      </div>}
      {status && !hasTrust && !pairing && (
        <form onSubmit={handlePair} className="lm-connection-stack">
          <p className="lm-connection-note">
            Generate a code here, then open Harness Desktop → Settings → Devices on your computer.
            Select this Autonomous robot and enter the code. Keep both on the same local network.
          </p>
          <button type="submit" disabled={busy}
            className="lm-connection-button is-primary">{busy ? "Preparing…" : "Generate pairing code"}</button>
          <span className="lm-connection-note">
            Codes expire after 60 seconds. Pairing gives access to this computer’s agents.
          </span>
        </form>
      )}
      {(error || connectionError || status?.error || pairInfo?.error) && <p role="alert" className="lm-connection-error">
        {error || connectionError || status?.error || pairInfo?.error}
      </p>}
      <button type="button" disabled={busy} onClick={() => { setError(null); setRefresh(value => value + 1); }}
        className="lm-connection-button lm-connection-refresh">Refresh status</button>
    </div>
  );
}
