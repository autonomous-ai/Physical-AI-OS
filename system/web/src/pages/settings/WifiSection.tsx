import { useCallback, useEffect, useMemo, useState } from "react";
import { Wifi, Pencil, X, RefreshCw } from "lucide-react";
import { C, FIELD_GAP, LABEL_STYLE, INPUT_STYLE, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { getNetworks } from "@/lib/api";
import type { NetworkItem } from "@/types";

export interface WifiLoadedState {
  ssid: boolean;
  password: boolean;
}

// Settings-panel Wi-Fi tab. Mirrors the setup wizard's picker+refresh UX so
// operators changing networks from /setting#wifi get the same "scan → dropdown
// → refresh" flow as first-time setup — no more blind typing of the SSID.
// SSID is a locked field by default (device already has one); clicking the
// pencil unlocks it and swaps in the scan-backed dropdown.
export function WifiSection({
  active, wifiLoaded,
  ssid, setSsid, password, setPassword,
}: {
  active: boolean;
  wifiLoaded: WifiLoadedState;
  ssid: string; setSsid: (v: string) => void;
  password: string; setPassword: (v: string) => void;
}) {
  const [unlocked, setUnlocked] = useState(false);
  const readOnly = wifiLoaded.ssid && !unlocked;
  const [originalSsid, setOriginalSsid] = useState<string | null>(null);
  // Snapshot the current SSID the first time the field locks in so Cancel can
  // restore it if the operator abandoned an edit — same behavior as
  // LockedField.useLockToggle.
  useEffect(() => {
    if (wifiLoaded.ssid && originalSsid === null) {
      setOriginalSsid(ssid);
    }
  }, [wifiLoaded.ssid, ssid, originalSsid]);

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [scanErr, setScanErr] = useState<string | null>(null);

  const runScan = useCallback(async () => {
    setLoadingList(true);
    setScanErr(null);
    try {
      const nets = await getNetworks();
      setNetworks((nets ?? []).filter((n) => n.ssid !== ""));
    } catch {
      setNetworks([]);
      setScanErr("Wi-Fi scan failed. Try Refresh.");
    } finally {
      setLoadingList(false);
    }
  }, []);

  // First scan runs when the operator unlocks the SSID field — no point
  // hitting the device with `iw scan` while the read-only display is showing
  // the current network and no picker is visible.
  useEffect(() => {
    if (unlocked && networks.length === 0 && !loadingList) {
      runScan();
    }
  }, [unlocked, networks.length, loadingList, runScan]);

  const uniqueNetworks = useMemo(
    () => [...new Map(networks.map((n) => [n.ssid, n])).values()],
    [networks],
  );

  const handleCancel = () => {
    if (originalSsid !== null) setSsid(originalSsid);
    setUnlocked(false);
  };

  return (
    <SectionCard
      id="wifi"
      title="Wi-Fi"
      active={active}
      description="The network your robot connects to. Click the pencil to change it."
      icon={<Wifi size={17} />}
    >
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="ssid" style={LABEL_STYLE}>Wi-Fi network</label>
        {readOnly ? (
          <div style={{ position: "relative" }}>
            <input
              id="ssid" type="text" value={ssid} readOnly
              placeholder="Network name" autoComplete="off"
              style={{
                ...INPUT_STYLE,
                background: C.bg, color: C.textDim,
                paddingRight: 40, cursor: "default",
              }}
            />
            <button
              type="button"
              onClick={() => setUnlocked(true)}
              tabIndex={-1}
              aria-label="Edit" title="Edit"
              style={{
                position: "absolute", right: 0, top: 0, height: "100%",
                padding: "0 12px", background: "none", border: "none",
                color: C.amber, cursor: "pointer",
                display: "flex", alignItems: "center",
              }}
            >
              <Pencil size={14} />
            </button>
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                {uniqueNetworks.length > 0 ? (
                  <select
                    id="ssid" value={ssid}
                    onChange={(e) => setSsid(e.target.value)}
                    style={{ ...INPUT_STYLE, cursor: "pointer" }}
                  >
                    <option value="">Choose your Wi-Fi</option>
                    {uniqueNetworks.map((n) => (
                      <option key={n.bssid} value={n.ssid}>{n.ssid}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    id="ssid" type="text" value={ssid}
                    onChange={(e) => setSsid(e.target.value)}
                    placeholder={loadingList ? "Scanning…" : "Enter Wi-Fi name"}
                    autoComplete="off"
                    style={INPUT_STYLE}
                  />
                )}
              </div>
              <button
                type="button"
                onClick={runScan}
                disabled={loadingList}
                aria-label={loadingList ? "Scanning Wi-Fi networks" : "Refresh Wi-Fi networks"}
                style={{
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  background: "none", border: `1px solid ${C.border}`,
                  width: 40, height: 40, borderRadius: 10,
                  color: loadingList ? C.textMuted : C.amber,
                  cursor: loadingList ? "not-allowed" : "pointer",
                  flexShrink: 0,
                }}
                title={loadingList ? "Scanning…" : "Re-scan Wi-Fi networks"}
              >
                <RefreshCw size={14} className={loadingList ? "lm-spin-ico" : undefined} />
              </button>
              {wifiLoaded.ssid && (
                <button
                  type="button"
                  onClick={handleCancel}
                  aria-label="Cancel edit" title="Cancel edit"
                  style={{
                    display: "inline-flex", alignItems: "center",
                    background: "none", border: "none", padding: "0 4px",
                    color: C.textMuted, cursor: "pointer", flexShrink: 0,
                  }}
                >
                  <X size={15} />
                </button>
              )}
            </div>
            {scanErr && (
              <div style={{ fontSize: 12, color: C.red, marginTop: 6 }}>{scanErr}</div>
            )}
          </>
        )}
      </div>
      <LockedPasswordField lockedInitially={wifiLoaded.password} label="Password" id="password" value={password} onChange={setPassword} placeholder="Wi-Fi password" />
    </SectionCard>
  );
}
