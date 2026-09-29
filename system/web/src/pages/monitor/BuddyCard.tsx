import { useCallback, useEffect, useRef, useState } from "react";
import { Laptop } from "lucide-react";
import { S } from "./styles";
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
    <div className="lm-mon-card" style={{ ...S.card, boxShadow: undefined }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ ...S.cardLabel, display: "flex", alignItems: "center", gap: 8, marginBottom: 0 }}>
          <span className="lm-mon-chip" aria-hidden><Laptop size={13} /></span>
          <span>Autonomous Buddy (Mac)</span>
        </div>
        <span
          style={{
            fontSize: 10,
            padding: "3px 9px",
            borderRadius: 4,
            fontWeight: 700,
            background: status?.connected
              ? "rgba(52,211,153,0.1)"
              : status?.paired
                ? "rgba(245,158,11,0.1)"
                : "rgba(80,74,60,0.4)",
            color: status?.connected
              ? "var(--lm-green)"
              : status?.paired
                ? "var(--lm-amber)"
                : "var(--lm-text-muted)",
            border: `1px solid ${
              status?.connected
                ? "rgba(52,211,153,0.3)"
                : status?.paired
                  ? "rgba(245,158,11,0.3)"
                  : "rgba(80,74,60,0.4)"
            }`,
          }}
        >
          {status?.connected ? "CONNECTED" : status?.paired ? "OFFLINE" : "NOT PAIRED"}
        </span>
      </div>

      {!status && !error && (
        <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Loading…</span>
      )}

      {status?.paired && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {status.name && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Name</span>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>{status.name}</span>
            </div>
          )}
          {status.osVersion && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>macOS</span>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)", fontFamily: "monospace" }}>{status.osVersion}</span>
            </div>
          )}
          {status.buddyId && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Buddy ID</span>
              <span style={{ fontSize: 11, fontWeight: 500, color: "var(--lm-text-dim)", fontFamily: "monospace" }}>{status.buddyId}</span>
            </div>
          )}
          <button
            type="button"
            onClick={handleRevoke}
            disabled={busy}
            style={{
              marginTop: 8,
              padding: "6px 10px",
              fontSize: 12,
              border: "1px solid rgba(239,68,68,0.3)",
              background: "rgba(239,68,68,0.08)",
              color: "var(--lm-red)",
              borderRadius: 4,
              cursor: busy ? "not-allowed" : "pointer",
            }}
          >
            Revoke pairing
          </button>
        </div>
      )}

      {status && !status.paired && !code && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>
            No Mac paired. Install Autonomous Buddy on your Mac then click below to start pairing.
          </span>
          <button
            type="button"
            onClick={handlePair}
            disabled={busy}
            style={{
              padding: "8px 12px",
              fontSize: 13,
              fontWeight: 600,
              border: "1px solid rgba(52,211,153,0.4)",
              background: "rgba(52,211,153,0.08)",
              color: "var(--lm-green)",
              borderRadius: 4,
              cursor: busy ? "not-allowed" : "pointer",
            }}
          >
            {busy ? "Generating…" : "Pair new Mac"}
          </button>
        </div>
      )}

      {code && (
        <div ref={codeBox} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span style={{ fontSize: 11, color: "var(--lm-text-dim)" }}>
            Enter this code in Autonomous Buddy → <em>Pair with device…</em>
          </span>
          <button
            type="button"
            onClick={handleCopyCode}
            title="Click to copy"
            style={{
              fontFamily: "monospace",
              fontSize: 28,
              fontWeight: 700,
              letterSpacing: "0.25em",
              textAlign: "center",
              padding: "10px 0",
              background: "rgba(255,255,255,0.02)",
              border: "1px dashed rgba(255,255,255,0.15)",
              color: "var(--lm-text)",
              borderRadius: 4,
              cursor: "pointer",
            }}
          >
            {code}
          </button>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>
              Expires in {codeTtl}s
            </span>
            <button
              type="button"
              onClick={handlePair}
              disabled={busy}
              style={{
                padding: "3px 8px",
                fontSize: 11,
                border: "1px solid rgba(255,255,255,0.1)",
                background: "transparent",
                color: "var(--lm-text-dim)",
                borderRadius: 4,
                cursor: busy ? "not-allowed" : "pointer",
              }}
            >
              New code
            </button>
          </div>
        </div>
      )}

      {error && (
        <div style={{ marginTop: 8, fontSize: 11, color: "var(--lm-red)" }}>{error}</div>
      )}
    </div>
  );
}
