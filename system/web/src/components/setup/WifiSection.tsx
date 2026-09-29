import { useState } from "react";
import { Wifi, Eye, EyeOff, Settings, Check, RefreshCw } from "lucide-react";
import { C, ConfiguredHint, PasswordField, SectionCard, SkeletonBlock, LABEL_STYLE, INPUT_STYLE, INPUT_PAD_ONE_ICON, FIELD_GAP, ADMIN_PASSWORD_MIN } from "./shared";
import type { NetworkItem } from "@/types";

// Small uppercase label separating field groups inside one card.
function GroupLabel({ children, first = false }: { children: React.ReactNode; first?: boolean }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 700, letterSpacing: "0.06em",
      textTransform: "uppercase", color: C.textMuted,
      marginTop: first ? 0 : 18, marginBottom: 10,
      paddingTop: first ? 0 : 16,
      borderTop: first ? "none" : `1px solid ${C.border}`,
    }}>
      {children}
    </div>
  );
}

// 802.11 caps SSID at 32 bytes (not chars); matches the backend check.
const SSID_MAX_BYTES = 32;
const ssidByteLength = (s: string) => new TextEncoder().encode(s).length;

const SKELETON_FIELD: React.CSSProperties = {
  width: "100%",
  height: 40,
  borderRadius: 10,
  background: C.surface,
  border: `1px solid ${C.border}`,
  boxSizing: "border-box",
};

export function WifiSection({
  active, ssid, setSsid, password, setPassword, loadingList, uniqueNetworks,
  refreshNetworks,
  passwordConfigured = false,
  connectedSsid = "",
  checkingConnection = false,
  wiredUplink = false,
  adminPassword, setAdminPassword,
}: {
  active: boolean;
  ssid: string;
  setSsid: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  loadingList: boolean;
  uniqueNetworks: NetworkItem[];
  /** Re-run `iw scan` on the device. */
  refreshNetworks?: () => void;
  /** True while useWifiConnected's first probe is still deciding whether the device is already on home Wi-Fi. */
  checkingConnection?: boolean;
  /** True when online without any SSID (wired uplink). */
  wiredUplink?: boolean;
  /** Hides the password input and shows a "configured" indicator. */
  passwordConfigured?: boolean;
  /** SSID the device is currently joined to; shows a read-only "Connected" row. */
  connectedSsid?: string;
  /** Device admin password; only rendered on first-time setup. */
  adminPassword?: string;
  setAdminPassword?: (v: string) => void;
}) {
  const showAdminPassword = setAdminPassword !== undefined;
  const showConnected = !!connectedSsid;
  const [adminVisible, setAdminVisible] = useState(true);
  const bytes = ssidByteLength(ssid);
  const overLimit = bytes > SSID_MAX_BYTES;
  const showCounter = overLimit;
  return (
    <SectionCard
      id="wifi"
      active={active}
      title={showAdminPassword ? "Setting up" : "Wi-Fi"}
      icon={showAdminPassword ? <Settings size={17} /> : <Wifi size={17} />}
      description={showConnected
        ? "Your robot is connected to Wi-Fi."
        : "Choose your Wi-Fi and enter its password."}
    >
      {showAdminPassword && (
        <>
          <div style={{ display: "none" }}>
            <GroupLabel first>Robot password</GroupLabel>
            <div style={{ marginBottom: FIELD_GAP }}>
              <div style={{ position: "relative" }}>
              <input
                id="admin_password" type={adminVisible ? "text" : "password"} value={adminPassword ?? ""}
                onChange={(e) => setAdminPassword!(e.target.value)}
                placeholder={`At least ${ADMIN_PASSWORD_MIN} characters`} autoComplete="off"
                style={{ ...INPUT_STYLE, padding: INPUT_PAD_ONE_ICON }}
              />
              <button
                type="button" onClick={() => setAdminVisible((v) => !v)} tabIndex={-1}
                className="lm-eye-btn"
                aria-label={adminVisible ? "Hide password" : "Show password"}
                style={{
                  position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
                  height: 32, width: 32, padding: 0, background: "none", border: "none",
                  cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
                }}
              >
                {adminVisible ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
              <div style={{ marginTop: 6, fontSize: 12, color: C.textDim, lineHeight: 1.5 }}>
                Keeps your robot private and lets you sign in later. Don't lose it.
              </div>
            </div>
          </div>
        </>
      )}
      {checkingConnection ? (
        <>
          <div style={{ marginBottom: FIELD_GAP }}>
            {!showAdminPassword && <label style={LABEL_STYLE}>Wi-Fi network</label>}
            <div style={SKELETON_FIELD} />
          </div>
          <div style={{ marginBottom: FIELD_GAP }}>
            {!showAdminPassword && <label style={LABEL_STYLE}>Wi-Fi password</label>}
            <div style={SKELETON_FIELD} />
          </div>
        </>
      ) : showConnected ? (
        <div style={{ marginBottom: FIELD_GAP }}>
          {!showAdminPassword && (
            <label style={LABEL_STYLE}>Wi-Fi network</label>
          )}
          <div style={{
            display: "flex", alignItems: "center",
            gap: 10, padding: "10px 13px",
            background: C.bg, border: `1px solid ${C.border}`,
            borderRadius: 10, fontSize: 14, color: C.textDim,
          }}>
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Check size={15} style={{ color: C.green }} />
              <span>Connected to <span style={{ color: C.text }}>{connectedSsid}</span></span>
            </span>
          </div>
        </div>
      ) : (
        <>
          {wiredUplink && !ssid && (
            <div style={{
              display: "flex", alignItems: "center",
              gap: 8, marginBottom: FIELD_GAP, padding: "10px 13px",
              background: C.bg, border: `1px solid ${C.border}`,
              borderRadius: 10, fontSize: 13, color: C.textDim,
            }}>
              <Check size={15} style={{ color: C.green, flexShrink: 0 }} />
              <span>
                This robot is already online without Wi-Fi — usually an ethernet
                cable. Leave this empty to keep that connection, or pick a network
                to add Wi-Fi.
              </span>
            </div>
          )}
          <div style={{ marginBottom: FIELD_GAP }}>
            {!showAdminPassword && (
              <label htmlFor="ssid" style={LABEL_STYLE}>
                Wi-Fi network
              </label>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                {loadingList ? (
                  <SkeletonBlock />
                ) : uniqueNetworks.length > 0 ? (
                  <select
                    id="ssid"
                    value={ssid}
                    onChange={(e) => setSsid(e.target.value)}
                    style={{
                      ...INPUT_STYLE,
                      border: `1px solid ${overLimit ? C.red : C.border}`,
                      cursor: "pointer",
                    }}
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
                    placeholder="Enter Wi-Fi name" autoComplete="off"
                    style={{
                      ...INPUT_STYLE,
                      border: `1px solid ${overLimit ? C.red : C.border}`,
                    }}
                  />
                )}
              </div>
              {refreshNetworks && (
                <button
                  type="button"
                  onClick={refreshNetworks}
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
                  title={loadingList ? "Scanning…" : "Re-scan Wi-Fi networks (useful right after a soft reset when the first scan came up empty)"}
                >
                  <RefreshCw size={14} className={loadingList ? "lm-spin-ico" : undefined} />
                </button>
              )}
            </div>
            {showCounter && (
              <div style={{
                marginTop: 6, fontSize: 12,
                color: overLimit ? C.red : C.textDim,
              }}>
                {overLimit
                  ? `SSID too long: ${bytes}/${SSID_MAX_BYTES} bytes (802.11 limit)`
                  : `${bytes}/${SSID_MAX_BYTES} bytes`}
              </div>
            )}
          </div>
          {passwordConfigured ? (
            <ConfiguredHint label={showAdminPassword ? "" : "Wi-Fi password"} />
          ) : (
            <PasswordField label={showAdminPassword ? "" : "Wi-Fi password"} id="password" value={password} onChange={setPassword} placeholder="Wi-Fi password" />
          )}
        </>
      )}
    </SectionCard>
  );
}
