import { useCallback, useEffect, useRef, useState } from "react";
import { Laptop } from "lucide-react";
import { API } from "./types";

interface BuddyStatus {
  paired: boolean;
  connected?: boolean;
  buddyId?: string;
  name?: string;
  osVersion?: string;
  pairedAt?: string;
}

interface PairStartResponse {
  code: string;
  expiresIn: number;
}

// Camelify the keys the server sends (snake_case) for the small set we care about.
function normalizeStatus(d: Record<string, unknown> | null): BuddyStatus {
  if (!d) return { paired: false };
  return {
    paired: Boolean(d.paired),
    connected: Boolean(d.connected),
    buddyId: typeof d.buddy_id === "string" ? d.buddy_id : undefined,
    name: typeof d.name === "string" ? d.name : undefined,
    osVersion: typeof d.os_version === "string" ? d.os_version : undefined,
    pairedAt: typeof d.paired_at === "string" ? d.paired_at : undefined,
  };
}

export function BuddyCard() {
  const [status, setStatus] = useState<BuddyStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [codeExpiresAt, setCodeExpiresAt] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const codeBox = useRef<HTMLDivElement | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/buddy/status`);
      const j = await r.json();
      if (j.status === 1) {
        setStatus(normalizeStatus(j.data));
        setError(null);
      } else {
        setError(j.message ?? "status error");
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 5000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  useEffect(() => {
    if (!codeExpiresAt) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [codeExpiresAt]);

  useEffect(() => {
    if (codeExpiresAt && now >= codeExpiresAt) {
      setCode(null);
      setCodeExpiresAt(null);
    }
  }, [now, codeExpiresAt]);

  useEffect(() => {
    if (status?.paired) {
      setCode(null);
      setCodeExpiresAt(null);
    }
  }, [status?.paired]);

  const handlePair = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${API}/buddy/pair/start`, { method: "POST" });
      const j = await r.json();
      if (j.status !== 1) {
        setError(j.message ?? "pair start failed");
        return;
      }
      const d = j.data as PairStartResponse;
      setCode(d.code);
      setCodeExpiresAt(Date.now() + d.expiresIn * 1000);
      setNow(Date.now());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async () => {
    if (!confirm("Revoke this Mac's pairing? The buddy app will lose access.")) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${API}/buddy`, { method: "DELETE" });
      const j = await r.json();
      if (j.status !== 1) setError(j.message ?? "revoke failed");
      else await fetchStatus();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleCopyCode = () => {
    if (!code) return;
    void navigator.clipboard?.writeText(code).catch(() => {});
    if (codeBox.current) {
      codeBox.current.animate(
        [{ background: "rgba(52,211,153,0.3)" }, { background: "rgba(255,255,255,0.02)" }],
        { duration: 600 },
      );
    }
  };

  const codeTtl = codeExpiresAt ? Math.max(0, Math.ceil((codeExpiresAt - now) / 1000)) : 0;

  return (
    <div className="lm-mon-card lm-connection-card">
      <div className="lm-connection-header">
        <h2><span className="lm-mon-chip" aria-hidden><Laptop size={16} /></span>Autonomous Buddy <small>Mac</small></h2>
        <span className={`lm-connection-status ${status?.connected ? "is-connected" : status?.paired ? "is-offline" : ""}`}>
          {status?.connected ? "Connected" : status?.paired ? "Offline" : "Not paired"}
        </span>
      </div>
      <p className="lm-connection-description">Connect your Mac companion to this robot.</p>

      {!status && !error && <p className="lm-connection-note">Loading…</p>}

      {status?.paired && (
        <div className="lm-connection-stack">
          <dl className="lm-connection-facts">
            {status.name && <div><dt>Name</dt><dd>{status.name}</dd></div>}
            {status.osVersion && <div><dt>macOS</dt><dd>{status.osVersion}</dd></div>}
          </dl>
          {status.buddyId && <details className="lm-connection-details">
            <summary>Connection details</summary>
            <dl className="lm-connection-facts"><div><dt>Buddy ID</dt><dd><code>{status.buddyId}</code></dd></div></dl>
          </details>}
          <button type="button" onClick={handleRevoke} disabled={busy} className="lm-connection-button is-danger">
            Revoke pairing
          </button>
        </div>
      )}

      {status && !status.paired && !code && (
        <div className="lm-connection-stack">
          <p className="lm-connection-note">No Mac paired. Install Autonomous Buddy on your Mac then click below to start pairing.</p>
          <button type="button" onClick={handlePair} disabled={busy} className="lm-connection-button is-primary">
            {busy ? "Generating…" : "Pair new Mac"}
          </button>
        </div>
      )}

      {code && (
        <div ref={codeBox} className="lm-connection-stack">
          <p className="lm-connection-note">Enter this code in Autonomous Buddy → <em>Pair with device…</em></p>
          <button type="button" onClick={handleCopyCode} title="Click to copy" className="lm-connection-code">
            {code}
          </button>
          <div className="lm-connection-actions">
            <span className="lm-connection-note">Expires in {codeTtl}s</span>
            <button type="button" onClick={handlePair} disabled={busy} className="lm-connection-button">New code</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="lm-connection-error">{error}</p>}
    </div>
  );
}
