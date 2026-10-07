import { useState } from "react";
import { toast } from "sonner";
import { Eye, EyeOff, Copy, Check, Cpu, Fingerprint, Network } from "lucide-react";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import { copyText } from "@/lib/clipboard";
import { C, Field, PasswordField, SectionCard, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_PAD_ONE_ICON, FIELD_GAP, ADMIN_PASSWORD_MIN } from "./shared";

// Read-only MAC field masked behind ••••, with an eye toggle to reveal.
function MaskedReadField({ label, id, value }: {
  label: string; id: string; value: string;
}) {
  const [show, setShow] = useState(false);
  const displayed = show ? value : "•".repeat(Math.min(12, value.length || 8));
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>{label}</label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type="text" value={displayed} readOnly
          style={{
            ...INPUT_STYLE,
            ...INPUT_READONLY_STYLE,
            padding: INPUT_PAD_ONE_ICON,
            fontFamily: "ui-monospace, monospace",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide MAC" : "Show MAC"}
          style={{
            position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

// Advisory password strength meter under the admin password input.
function PasswordStrength({ value }: { value: string }) {
  if (!value) return null;
  const tooShort = value.length < ADMIN_PASSWORD_MIN;
  if (tooShort) {
    return (
      <StrengthRow level={-1} color={C.red}
        message={`At least ${ADMIN_PASSWORD_MIN} characters needed (${value.length}/${ADMIN_PASSWORD_MIN}).`} />
    );
  }
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^a-zA-Z0-9]/].filter((re) => re.test(value)).length;
  let score = 0;
  if (value.length >= 12) score += 1;
  if (classes >= 2) score += 1;
  if (classes >= 3) score += 1;
  const level = score === 0 ? 0 : score <= 2 ? 1 : 2;
  const messages = [
    "A bit simple — add a capital letter, number, or symbol to make it stronger.",
    "Good — add a symbol or make it longer for a stronger password.",
    "Strong password.",
  ];
  const colors = [C.yellow, C.yellow, C.green];
  return <StrengthRow level={level} color={colors[level]} message={messages[level]} />;
}

// 3-segment strength bar + hint; level -1 = invalid.
function StrengthRow({ level, color, message }: { level: number; color: string; message: string }) {
  const filled = level + 1;
  return (
    <div style={{ marginTop: -4, marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 4 }}>
        {[0, 1, 2].map((i) => (
          <div key={i} style={{
            flex: 1, height: 3, borderRadius: 2,
            background: i < filled ? color : C.border,
            transition: "background 0.2s",
          }} />
        ))}
      </div>
      <div style={{ fontSize: 12, color, marginTop: 5 }}>
        {message}
      </div>
    </div>
  );
}

function MetaRow({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 14px", minHeight: 44 }}>
      <span style={{ flexShrink: 0, color: C.textMuted, display: "flex" }}>{icon}</span>
      <span style={{ flexShrink: 0, fontSize: 12.5, color: C.textDim, width: 84 }}>{label}</span>
      <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
        {children}
      </div>
    </div>
  );
}

function DeviceMetaCard({ deviceId, mac }: { deviceId: string; mac?: string }) {
  const [copied, setCopied] = useState(false);
  const [showMac, setShowMac] = useState(false);
  const copyId = async () => {
    const ok = await copyText(deviceId);
    if (ok) {
      setCopied(true);
      toast.success("Device ID copied");
      setTimeout(() => setCopied(false), 1400);
    } else {
      toast.error("Copy failed — please copy the Device ID manually");
    }
  };
  const maskedMac = showMac ? mac : "•".repeat(Math.min(14, (mac?.length ?? 0) || 8));
  const valueText: React.CSSProperties = {
    fontFamily: "ui-monospace, monospace", fontSize: 13, color: C.text,
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  };
  const iconBtn: React.CSSProperties = {
    flexShrink: 0, height: 28, width: 28, padding: 0, borderRadius: 7,
    background: "transparent", border: "none", cursor: "pointer",
    display: "flex", alignItems: "center", justifyContent: "center",
    color: C.textMuted, transition: "background 0.15s, color 0.15s",
  };
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ ...LABEL_STYLE, marginBottom: 8 }}>Device info</div>
      <div style={{
        border: `1px solid ${C.border}`, borderRadius: 12,
        background: C.surface, overflow: "hidden",
      }}>
        <MetaRow icon={<Fingerprint size={15} />} label="Device ID">
          <span style={valueText} title={deviceId}>{deviceId || "—"}</span>
          {deviceId && (
            <button
              type="button" onClick={copyId} tabIndex={-1}
              className="lm-eye-btn" style={iconBtn}
              aria-label="Copy Device ID" title={copied ? "Copied!" : "Copy"}
            >
              {copied ? <Check size={14} style={{ color: C.green }} /> : <Copy size={14} />}
            </button>
          )}
        </MetaRow>
        {mac && (
          <>
            <div style={{ height: 1, background: C.border }} />
            <MetaRow icon={<Network size={15} />} label="MAC">
              <span style={valueText} title={showMac ? mac : undefined}>{maskedMac}</span>
              <button
                type="button" onClick={() => setShowMac((v) => !v)} tabIndex={-1}
                className="lm-eye-btn" style={iconBtn}
                aria-label={showMac ? "Hide MAC" : "Show MAC"} title={showMac ? "Hide" : "Reveal"}
              >
                {showMac ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </MetaRow>
          </>
        )}
      </div>
    </div>
  );
}

export function DeviceSection({
  active, deviceId, setDeviceId, mac,
  adminPassword, setAdminPassword,
  adminPasswordConfirm, setAdminPasswordConfirm,
  rotateAdminPassword, setRotateAdminPassword,
  wakeWord, setWakeWord,
  agentName, wakePhrases,
}: {
  active: boolean;
  deviceId: string;
  setDeviceId: (v: string) => void;
  mac?: string;
  adminPassword?: string;
  setAdminPassword?: (v: string) => void;
  adminPasswordConfirm?: string;
  setAdminPasswordConfirm?: (v: string) => void;
  // Empty means keep the existing password.
  rotateAdminPassword?: string;
  setRotateAdminPassword?: (v: string) => void;
  wakeWord?: boolean;
  setWakeWord?: (v: boolean) => void;
  agentName?: string;
  wakePhrases?: string[];
}) {
  const showAdminPasswordFields = setAdminPassword !== undefined;
  const showRotateField = setRotateAdminPassword !== undefined;
  const mismatch =
    showAdminPasswordFields &&
    !!adminPasswordConfirm &&
    !!adminPassword &&
    adminPassword !== adminPasswordConfirm;
  const description = showAdminPasswordFields
    ? "Set an admin password — you'll use it to sign in from any browser after setup."
    : "Your device's identity and admin login.";
  return (
    <SectionCard id="device" title="Device" active={active} description={description} icon={<Cpu size={17} />}>
      {showAdminPasswordFields && (
        <>
          <PasswordField
            label="Admin Password"
            id="admin_password"
            value={adminPassword ?? ""}
            onChange={setAdminPassword!}
            placeholder={`At least ${ADMIN_PASSWORD_MIN} characters`}
          />
          <PasswordStrength value={adminPassword ?? ""} />
          <PasswordField
            label="Confirm Password"
            id="admin_password_confirm"
            value={adminPasswordConfirm ?? ""}
            onChange={setAdminPasswordConfirm!}
            placeholder="Re-enter password"
            error={mismatch ? "Passwords don't match." : undefined}
          />
        </>
      )}
      {showRotateField && (
        <>
          <SecretUpdateField
            label="Admin Password"
            id="admin_password"
            configured={true}
            value={rotateAdminPassword ?? ""}
            onChange={setRotateAdminPassword!}
            placeholder={`New password (min ${ADMIN_PASSWORD_MIN} chars)`}
          />
          <PasswordStrength value={rotateAdminPassword ?? ""} />
        </>
      )}

      {setWakeWord && (
        <div style={{ marginTop: 18, paddingTop: 14, borderTop: `1px solid ${C.border}` }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: C.text }}>
            <input type="checkbox" checked={wakeWord ?? false} onChange={(e) => setWakeWord(e.target.checked)} style={{ flexShrink: 0 }} />
            <span style={{ whiteSpace: "nowrap" }}>Require attention trigger</span>
          </label>
          <div style={{ marginTop: 5, marginLeft: 23, fontSize: 11.5, lineHeight: 1.45, color: C.textMuted }}>
            Require an attention trigger before handling speech. When off, your device keeps its existing always-listening behavior.
          </div>
          {!!wakePhrases?.length && (
            <div style={{ marginTop: 10, marginLeft: 23, fontSize: 11.5, lineHeight: 1.45, color: C.textMuted }}>
              <div style={{ marginBottom: 6 }}>
                Spoken wake phrases (other triggers: tap, gaze, or a recognized person){agentName ? ` — current agent: ${agentName}` : ""}:
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {wakePhrases.map((phrase) => (
                  <code key={phrase} style={{ padding: "3px 6px", borderRadius: 5, background: C.surface, border: `1px solid ${C.border}`, color: C.text }}>
                    {phrase}
                  </code>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {showRotateField ? (
        <DeviceMetaCard deviceId={deviceId} mac={mac} />
      ) : (
        <>
          <Field label="Device ID" id="device_id" value={deviceId} onChange={setDeviceId} placeholder="device-001" readOnly />
          {mac && <MaskedReadField label="MAC" id="mac" value={mac} />}
        </>
      )}
    </SectionCard>
  );
}
