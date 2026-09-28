import { useState } from "react";
import { Wifi, Eye, EyeOff, Settings, Check, RefreshCw } from "lucide-react";
import { C, ConfiguredHint, PasswordField, SectionCard, SkeletonBlock, LABEL_STYLE, INPUT_STYLE, INPUT_PAD_ONE_ICON, FIELD_GAP, ADMIN_PASSWORD_MIN } from "./shared";
import type { NetworkItem } from "@/types";

// Small uppercase group label that separates the Device and Wi-Fi field groups
// inside the merged (V2) card, so the single card still reads as two distinct
// sections without needing two separate cards.
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

// 802.11 caps SSID at 32 bytes (not 32 chars). Each Chinese UTF-8 char is
// 3 bytes, so a short-looking SSID can still overflow. Counting bytes here
// matches the backend's len([]byte(ssid)) check in network.SetupNetwork.
const SSID_MAX_BYTES = 32;
const ssidByteLength = (s: string) => new TextEncoder().encode(s).length;

// Placeholder bar shown while useWifiConnected's first probe decides whether the
// device is already on Wi-Fi. Mirrors INPUT_STYLE's box (height ≈ 40px from
// 14px text + 10px×2 padding + border, radius 10) so the real network/password
// fields drop in without a layout jump. Theme-aware via C.surface / C.border.
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
  /** Re-run `iw scan` on the device. Wired to the "Refresh list" button next
   *  to the picker: right after a soft reset, wlan0 comes up in AP mode with
   *  no cached scan results, so the FIRST scan often returns empty. Letting
   *  the operator retry manually is simpler than backend warmup + timing
   *  guesses. Also useful for a weak-signal SSID (e.g. "Glinks 1") that a
   *  single scan window missed — a second scan often catches it. */
  refreshNetworks?: () => void;
  /** True while useWifiConnected's first probe is still deciding whether the
   *  device is already on home Wi-Fi. We show a skeleton for the whole Wi-Fi
   *  group during this window so the step doesn't flash the empty "Choose your
   *  Wi-Fi" picker for a beat before collapsing into the connected state. */
  checkingConnection?: boolean;
  /** True when the device has internet while joined to no SSID — in practice an
   *  ethernet cable, though a USB modem or tether looks the same and behaves the
   *  same for our purposes. Only ever true on devices that actually have such a
   *  connection right now, so a Wi-Fi-only device never sees this. The picker
   *  stays available (the operator may still want Wi-Fi as well), but leaving it
   *  empty is now a valid choice, so we say so instead of letting the step look
   *  unfinished. */
  wiredUplink?: boolean;
  /** True when ConfigPublicResponse.has_network_password=true: hide the
   *  password input + show "configured" indicator. Operator can rotate via
   *  /edit or by clicking "update" → toggles back into the input. */
  passwordConfigured?: boolean;
  /** The SSID the device is CURRENTLY joined to (from useWifiConnected /
   *  GET /api/network/current). When set, the device already left the setup AP
   *  and is on home Wi-Fi — this is the state after the AP→STA join reloads the
   *  page — so we show a read-only "Connected to <ssid>" row instead of
   *  re-prompting the operator to pick a network + type a password they already
   *  entered. Switching networks is a /setting#wifi concern, not a setup one. */
  connectedSsid?: string;
  /** Device admin password. Only passed (and only rendered) when the device
   *  has no admin password on file yet — i.e. first-time setup. Shown in clear
   *  text on purpose (no hide toggle, no confirm): it's set once here and the
   *  operator needs to see what they're typing since they'll sign in with it.
   *  Caller gates this on `!hasAdminPassword`. */
  adminPassword?: string;
  setAdminPassword?: (v: string) => void;
}) {
  const showAdminPassword = setAdminPassword !== undefined;
  // When the device reports it's already on home Wi-Fi, collapse the picker
  // into a read-only "connected" confirmation. Setup has no way back to the
  // picker from here by design — switching networks mid-setup isn't a flow this
  // wizard supports, and the operator can change it later from /setting#wifi
  // (pages/settings/WifiSection.tsx, which always renders the full picker).
  const showConnected = !!connectedSsid;
  // Device password is revealed by default (it's set once and the operator
  // needs to read it back), with an eye toggle to hide if someone's watching.
  const [adminVisible, setAdminVisible] = useState(true);
  const bytes = ssidByteLength(ssid);
  const overLimit = bytes > SSID_MAX_BYTES;
  // "bytes" is jargon to a normal user, so only surface the counter once the
  // SSID actually exceeds the 802.11 limit — at that point the number is
  // actionable ("trim it down"). Below the limit we stay silent.
  const showCounter = overLimit;
  return (
    // When the admin password is folded in (V2), the card covers two groups, so
    // it gets a neutral title/icon + a description that mentions both. When it's
    // just Wi-Fi (V1), it keeps the Wi-Fi title/icon and Wi-Fi-only copy.
    <SectionCard
      id="wifi"
      active={active}
      title={showAdminPassword ? "Setting up" : "Wi-Fi"}
      icon={showAdminPassword ? <Settings size={17} /> : <Wifi size={17} />}
      description={showConnected
        ? "Your robot is connected to Wi-Fi."
        : "Choose your Wi-Fi and enter its password."}
    >
      {/* Device admin password — kept mounted but hidden so its state (empty)
          still submits. Operators no longer see or type this: the backend
          defaults an empty AdminPassword to the 4 characters after the dash in
          the device's hardware ID (see handler.Setup). The Login page tells the
          operator where to read that suffix (sticker on the bottom of device).
          V1 (isV1) still shows the password in its dedicated Device step; only
          the V2 merged flow hides it. */}
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
        // First useWifiConnected probe still deciding: skeleton the whole Wi-Fi
        // group (network + password) so we never flash the empty picker before
        // resolving into either the connected state or the real picker. Bars
        // mirror the real input box (height/radius/border from INPUT_STYLE) so
        // nothing jumps when they resolve; C.surface + C.border keep them
        // theme-aware (light + dark), matching SkeletonBlock's static style.
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
        // Device is already on home Wi-Fi (post-join page reload). Show the
        // live network as a done state instead of an empty picker + password
        // the operator would otherwise be forced to re-enter. Read-only —
        // network switching lives in /setting#wifi, not in the setup wizard.
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
            // Online without any SSID = wired. Say so, otherwise the empty
            // picker reads as an unfinished step and the operator hunts for a
            // network the device does not need.
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
            {/* Flex row: input/select fills, Refresh button sits inline on the
                right. Kept as sibling of the input (not overlaid absolute like
                the password eye) so it's discoverable in both branches — the
                dropdown case AND the empty-list "Enter Wi-Fi name" fallback,
                where scan usually just needs one more shot to catch the
                target SSID. Rendered regardless of showAdminPassword so the
                first-time-setup form still has it. */}
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
