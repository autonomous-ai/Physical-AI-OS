import { useCallback, useEffect, useState } from "react";
import { Bluetooth, Loader2 } from "lucide-react";
import { HW } from "./types";
import { usePolling } from "../../hooks/usePolling";
import { S } from "./styles";
import { CardLabel } from "./components";

interface BTDevice {
  mac: string;
  name: string | null;
  paired: boolean;
  connected: boolean;
  trusted: boolean;
  active?: boolean;
}

interface BTStatus {
  available: boolean;
  active_mac: string | null;
  label: string;
  scanning: boolean;
  paired: BTDevice[];
}

interface DiscoveredDevice {
  mac: string;
  name: string;
}

function Pill({ text, tone }: { text: string; tone: "ok" | "warn" | "off" }) {
  const palette = {
    ok:   { fg: "var(--lm-green)", bg: "var(--lm-green-dim)", bd: "color-mix(in srgb, var(--lm-green) 33%, transparent)" },
    warn: { fg: "var(--lm-amber)", bg: "var(--lm-amber-dim)", bd: "color-mix(in srgb, var(--lm-amber) 33%, transparent)" },
    off:  { fg: "var(--lm-text-muted)", bg: "transparent", bd: "var(--lm-border)" },
  }[tone];
  return (
    <span style={{
      fontSize: 10, padding: "2px 7px", borderRadius: 4,
      background: palette.bg, color: palette.fg, border: `1px solid ${palette.bd}`,
      fontWeight: 700, letterSpacing: "0.05em", textTransform: "uppercase",
    }}>{text}</span>
  );
}

function deviceLabel(d: { mac: string; name: string | null }): string {
  return d.name && d.name.trim() ? d.name : d.mac;
}

type CaughtError = { name?: string; message?: string } | undefined;

function errMessage(e: unknown, fallback: string): string {
  return (e as CaughtError)?.message || fallback;
}

export function BluetoothSection() {
  const [status, setStatus] = useState<BTStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  const [pairOpen, setPairOpen] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredDevice[]>([]);
  const [scanning, setScanning] = useState(false);
  const [pairingMac, setPairingMac] = useState<string | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const [busyMac, setBusyMac] = useState<string | null>(null);
  const [forgetConfirm, setForgetConfirm] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const r = await fetch(`${HW}/bluetooth/status`, { signal });
      const j = (await r.json()) as BTStatus;
      setStatus(j);
      setStatusError(null);
    } catch (e) {
      if ((e as CaughtError)?.name !== "AbortError") {
        setStatusError("Failed to fetch Bluetooth status");
      }
    }
  }, []);

  usePolling(refresh, 5_000);

  useEffect(() => {
    if (!pairOpen) return;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      try {
        const r = await fetch(`${HW}/bluetooth/scan/results`);
        const j = await r.json();
        setDiscovered(j.devices || []);
        setScanning(!!j.scanning);
      } catch {
        // Best-effort: keep the last scan results and retry next tick.
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { stop = true; clearInterval(id); };
  }, [pairOpen]);

  const startScan = async () => {
    setPairOpen(true);
    setPairError(null);
    setDiscovered([]);
    setSearchQuery("");
    try {
      const r = await fetch(`${HW}/bluetooth/scan/start`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      setScanning(true);
    } catch (e) {
      setPairError(errMessage(e, "Failed to start scan"));
    }
  };

  const pairDevice = async (mac: string) => {
    setPairingMac(mac);
    setPairError(null);
    try {
      const r = await fetch(`${HW}/bluetooth/pair`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mac }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Pairing failed");
      }
      setPairOpen(false);
      await refresh();
    } catch (e) {
      setPairError(errMessage(e, "Pairing failed"));
    } finally {
      setPairingMac(null);
    }
  };

  const setActive = async (mac: string | null) => {
    setBusyMac(mac || "__device__");
    setActionError(null);
    try {
      const r = await fetch(`${HW}/bluetooth/active`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mac: mac || "" }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to switch audio route");
      }
      await refresh();
    } catch (e) {
      setActionError(errMessage(e, "Failed to switch audio route"));
    } finally {
      setBusyMac(null);
    }
  };

  const forgetDevice = async (mac: string) => {
    setBusyMac(mac);
    setActionError(null);
    try {
      const r = await fetch(`${HW}/bluetooth/forget`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mac }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to forget device");
      }
      await refresh();
    } catch (e) {
      setActionError(errMessage(e, "Failed to forget device"));
    } finally {
      setBusyMac(null);
      setForgetConfirm(null);
    }
  };

  const monCard = { ...S.card, boxShadow: undefined };

  if (status && !status.available) {
    return (
      <div className="lm-mon-card" style={monCard}>
        <div style={{ marginBottom: 12 }}><CardLabel icon={<Bluetooth size={13} />} text="Bluetooth Headset" /></div>
        <p style={{ fontSize: 13, color: "var(--lm-text-muted)" }}>
          Bluetooth is not available on this host (bluetoothctl missing).
          Install BlueZ + PipeWire/PulseAudio on the Pi to use a BT headset.
        </p>
      </div>
    );
  }

  const active = status?.active_mac || null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="lm-mon-card" style={monCard}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <CardLabel icon={<Bluetooth size={13} />} text="Bluetooth Headset" />
          <div style={{ display: "flex", gap: 6 }}>
            {active
              ? <Pill text="Private mode" tone="ok" />
              : <Pill text="Device" tone="off" />}
          </div>
        </div>

        <p style={{ fontSize: 12, color: "var(--lm-text-muted)", marginTop: 0, marginBottom: 12 }}>
          When on, TTS and STT route through the BT headset instead of the device speaker/mic.
          Background sensing mic stays on the device so it keeps listening to the room.
        </p>

        {statusError && (
          <div style={errBox}>{statusError}</div>
        )}
        {actionError && (
          <div style={errBox}>{actionError}</div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {(status?.paired || []).length === 0 && (
            <div style={{ fontSize: 13, color: "var(--lm-text-muted)" }}>
              No headset paired yet.
            </div>
          )}
          {(status?.paired || []).map((d) => {
            const isActive = active === d.mac;
            const rowBusy = busyMac === d.mac || (busyMac === "__device__" && isActive);
            return (
              <div key={d.mac} className="lm-bt-row" style={deviceRow}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {deviceLabel(d)}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--lm-text-muted)", marginTop: 2 }}>
                    {d.mac} · {rowBusy
                      ? (isActive ? "Switching back to device..." : "Routing audio...")
                      : isActive
                        ? <span style={{ color: "var(--lm-green)", fontWeight: 600 }}>Connected</span>
                        : (d.connected ? "Ready" : "Offline")}
                  </div>
                </div>
                <button
                  onClick={() => setActive(isActive ? null : d.mac)}
                  disabled={rowBusy || busyMac === "__device__"}
                  className={`lm-u-btn${!isActive && !rowBusy ? " lm-bt-hover-reveal" : ""}`}
                  style={toggleBtn}
                  title={isActive
                    ? "Disconnect — route audio back to the device speaker/mic"
                    : "Connect — route audio through this headset"}
                >
                  {rowBusy
                    ? <><Loader2 size={13} className="lm-spin-ico" />{isActive ? "Disconnecting..." : "Connecting..."}</>
                    : (isActive ? "Disconnect" : "Connect")}
                </button>
                <button
                  onClick={() => setForgetConfirm(d.mac)}
                  disabled={rowBusy}
                  className="lm-u-btn"
                  style={ghostBtn}
                  title="Forget device"
                >
                  ✕
                </button>
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: 14, display: "flex", justifyContent: "flex-end" }}>
          <button onClick={startScan} className="lm-u-btn lm-u-btn-primary" style={primaryBtn}>
            + Connect headset
          </button>
        </div>
      </div>

      {pairOpen && (
        <Modal onClose={() => setPairOpen(false)} title="Connect Bluetooth headset">
          <p style={{ fontSize: 13, color: "var(--lm-text-muted)", marginTop: 0 }}>
            Put the headset in pairing mode (hold the power button 3-5s until the LED blinks,
            or open the AirPods case and hold the rear button).
          </p>
          <div style={{ fontSize: 11, color: "var(--lm-text-muted)", marginBottom: 8 }}>
            {scanning ? "Scanning..." : "Scan stopped — press Rescan if your device isn't listed"}
          </div>
          {pairError && <div style={errBox}>{pairError}</div>}
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Filter by name or MAC..."
            className="lm-u-input"
            style={searchInput}
          />
          {(() => {
            const q = searchQuery.trim().toLowerCase();
            const filtered = q
              ? discovered.filter((d) =>
                  (d.name || "").toLowerCase().includes(q) ||
                  d.mac.toLowerCase().includes(q),
                )
              : discovered;
            return (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 320, overflowY: "auto" }}>
            {discovered.length === 0 && (
              <div style={{ fontSize: 12, color: "var(--lm-text-muted)", padding: "8px 0" }}>
                No devices found yet...
              </div>
            )}
            {discovered.length > 0 && filtered.length === 0 && (
              <div style={{ fontSize: 12, color: "var(--lm-text-muted)", padding: "8px 0" }}>
                No devices match "{searchQuery}"
              </div>
            )}
            {filtered.map((d) => (
              <button
                key={d.mac}
                onClick={() => pairDevice(d.mac)}
                disabled={!!pairingMac}
                className="lm-u-interactive"
                style={listRowBtn}
              >
                <div style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{deviceLabel(d)}</div>
                  <div style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>{d.mac}</div>
                </div>
                <span style={{ fontSize: 12, color: "var(--lm-text-muted)" }}>
                  {pairingMac === d.mac ? "Pairing..." : "Pair"}
                </span>
              </button>
            ))}
          </div>
            );
          })()}
          <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
            <button onClick={startScan} className="lm-u-btn" style={ghostBtn}>Rescan</button>
            <button onClick={() => setPairOpen(false)} className="lm-u-btn lm-u-btn-primary" style={primaryBtn}>Close</button>
          </div>
        </Modal>
      )}

      {forgetConfirm && (
        <Modal onClose={() => setForgetConfirm(null)} title="Forget device?">
          <p style={{ fontSize: 13, color: "var(--lm-text-muted)" }}>
            After forgetting, you'll have to pair the headset again (30-60s) next time you want to use it.
          </p>
          <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
            <button onClick={() => setForgetConfirm(null)} className="lm-u-btn" style={ghostBtn}>Cancel</button>
            <button
              onClick={() => forgetDevice(forgetConfirm)}
              className="lm-u-btn"
              style={{ ...primaryBtn, background: "var(--lm-red)", color: "#fff", border: "1px solid var(--lm-red)" }}
            >
              Forget
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

const errBox: React.CSSProperties = {
  fontSize: 12, padding: "6px 10px", marginBottom: 10,
  background: "var(--lm-red-dim)", color: "var(--lm-red)", borderRadius: 6,
  border: "1px solid color-mix(in srgb, var(--lm-red) 33%, transparent)",
};

const deviceRow: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 8,
  padding: "10px 12px",
  background: "var(--lm-bg)",
  border: "1px solid var(--lm-border)",
  borderRadius: 8,
};

const baseBtn: React.CSSProperties = {
  padding: "6px 12px", fontSize: 12, fontWeight: 600,
  borderRadius: 6, cursor: "pointer",
  transition: "all 0.15s",
};

const toggleBtn: React.CSSProperties = {
  ...baseBtn,
  display: "inline-flex", alignItems: "center", gap: 6,
  background: "transparent", color: "var(--lm-text)",
  border: "1px solid var(--lm-border)",
};

const ghostBtn: React.CSSProperties = {
  ...baseBtn,
  background: "transparent", color: "var(--lm-text-muted)",
  border: "1px solid var(--lm-border)",
};

const primaryBtn: React.CSSProperties = {
  ...baseBtn,
  background: "var(--lm-amber)", color: "var(--lm-bg)",
  border: "1px solid var(--lm-amber)",
};

const searchInput: React.CSSProperties = {
  width: "100%",
  padding: "8px 12px",
  fontSize: 13,
  borderRadius: 6,
  marginBottom: 10,
  outline: "none",
  boxSizing: "border-box",
};

const listRowBtn: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 8,
  padding: "10px 12px", borderRadius: 8,
  background: "var(--lm-bg)",
  border: "1px solid var(--lm-border)",
  cursor: "pointer", color: "inherit",
};

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 1000, padding: 16,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--lm-card)", border: "1px solid var(--lm-border)",
          borderRadius: 12, padding: 20, maxWidth: 480, width: "100%",
          maxHeight: "calc(100vh - 64px)", overflow: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>{title}</h3>
          <button onClick={onClose} style={{ ...ghostBtn, padding: "4px 8px" }}>✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}
