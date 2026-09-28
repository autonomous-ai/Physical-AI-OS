import { useEffect, useRef, useState } from "react";
import { Wifi, RefreshCw, Eye, EyeOff, CheckCircle2, AlertCircle, ChevronDown, ChevronRight, Copy, Check, History } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { C } from "@/components/setup/shared";
import { getNetworks, wifiProvision, getSetupStatus } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import type { NetworkItem } from "@/types";

// The provisioning AP's own static IP. Backend's /setup/status fallback used
// to leak this as `lan_ip` while wlan0 was still in AP mode; even with the
// server-side filter in place, keep this defensive check so a stale response
// or a pre-fix device can't strand the operator with a link that's dead the
// moment the AP tears down.
//
// Also used as the "am I actually on the hotspot?" test — /wifi is ONLY
// meaningful when the browser reached the device via its AP static, which is
// the hotspot mode where first-time provisioning happens. When the operator
// hits /wifi via the LAN IP, they already have a working device; the setup
// wizard is out of scope and we bounce them to /monitor.
const AP_STATIC_IP = "192.168.100.1";
function isRealLanIp(ip: string | undefined): ip is string {
  return !!ip && ip !== AP_STATIC_IP;
}

// Key for persisting the last captured device address across browser sessions,
// so if the operator closes the /wifi tab (or the browser stalls in the
// AP-teardown window and they refresh), reopening /wifi surfaces the IP
// again instead of stranding them at "AP is gone, now what?".
const LAST_DEVICE_STORAGE_KEY = "autonomous.wifi.lastDevice";
interface LastDevice {
  lanIp: string;
  mac: string;
  savedAt: number;
}
function readLastDevice(): LastDevice | null {
  try {
    const raw = localStorage.getItem(LAST_DEVICE_STORAGE_KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as LastDevice;
    // Stale entries (7+ days old) are worse than nothing — the device's DHCP
    // lease has almost certainly rolled over. Drop them silently.
    if (Date.now() - d.savedAt > 7 * 24 * 60 * 60 * 1000) return null;
    return d;
  } catch { return null; }
}
function writeLastDevice(d: { lanIp: string; mac: string }) {
  try {
    localStorage.setItem(LAST_DEVICE_STORAGE_KEY,
      JSON.stringify({ ...d, savedAt: Date.now() }));
  } catch { /* quota / private mode — best-effort */ }
}

// WifiProvision — standalone AP-portal page served at /wifi.
//
// Purpose: let an operator who is physically on the device's hotspot bring
// the device online end-to-end WITHOUT depending on autonomous.ai pushing a
// URL. Wi-Fi is mandatory; LLM + STT + TTS + admin password are optional
// (empty = keep whatever is on disk).
//
// Deliberately separate from /setup — inherits none of the wizard's
// validation, section-gating, or URL-push state. Body posted to the equally
// standalone POST /api/device/wifi-provision (gated by apOnlyMiddleware).
//
// Post-submit redirect: the AP tears down ~2–5s into the join, so /setup/status
// is only reachable during that brief window. We poll aggressively and grab
// `lan_ip` the moment the device publishes it, then show the operator BOTH
// the captured IP AND the mDNS name (<mac>.local) as reconnect targets — the
// mDNS fallback covers routers/networks that don't leak the DHCP lease back
// to the LAN address the poller saw.

type Phase = "idle" | "connecting" | "connected" | "failed";

export default function WifiProvision() {
  // /wifi is a first-time setup screen served from the provisioning AP
  // (192.168.100.1). Once the operator is on the LAN, they should be on
  // /monitor — landing on /wifi is either a stale bookmark or a manual URL
  // edit, so we bounce them straight to the admin app. window.location.replace
  // (not react-router navigate) so nothing about /wifi ends up in the tab's
  // history — Back should go where they came from, not back to a redirect.
  //
  // wrongOrigin is computed once at first render and used both to short-
  // circuit the effect below (fires the redirect) and to skip the form paint
  // (returns null). `.local` mDNS variants are allowed to see the same
  // fallback screen — some setups reach the device that way during the
  // AP-teardown window, and the setup wizard is still the right place then.
  const wrongOrigin = typeof window !== "undefined"
    && window.location.hostname !== AP_STATIC_IP
    && !window.location.hostname.endsWith(".local");
  useEffect(() => {
    if (wrongOrigin) window.location.replace("/monitor");
  }, [wrongOrigin]);

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [lanIp, setLanIp] = useState("");
  const [mac, setMac] = useState("");   // e.g. "intern-v2-893f" — mDNS host prefix
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  // set_up_completed from /setup/status. When false (fresh device) the LLM
  // triplet is promoted out of Advanced and made required — a device with no
  // brain that joins Wi-Fi just to sit at the admin page saying "chat
  // unavailable" is worse UX than making the operator paste their key here.
  const [provisioned, setProvisioned] = useState<boolean | null>(null);

  // Optional advanced config fields. All empty = "keep on-disk value".
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
  const [sttLanguage, setSttLanguage] = useState("");
  const [ttsApiKey, setTtsApiKey] = useState("");
  const [ttsBaseUrl, setTtsBaseUrl] = useState("");
  const [ttsProvider, setTtsProvider] = useState("");
  const [ttsVoice, setTtsVoice] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  // Messaging channel. Empty = keep on-disk value. Backend only writes the
  // sub-tokens (telegram_*, slack_*, discord_*) that match the picked channel.
  const [channel, setChannel] = useState<"" | "telegram" | "slack" | "discord">("");
  const [teleBotToken, setTeleBotToken] = useState("");
  const [teleUserId, setTeleUserId] = useState("");
  const [slackBotToken, setSlackBotToken] = useState("");
  const [slackAppToken, setSlackAppToken] = useState("");
  const [slackUserId, setSlackUserId] = useState("");
  const [discordBotToken, setDiscordBotToken] = useState("");
  const [discordUserId, setDiscordUserId] = useState("");

  // Initial fetch: get mac (for mDNS fallback) + any already-published lan_ip.
  useEffect(() => {
    (async () => {
      try {
        const s = await getSetupStatus();
        if (s.mac) setMac(s.mac);
        if (isRealLanIp(s.lan_ip)) setLanIp(s.lan_ip);
        setProvisioned(!!s.set_up_completed);
      } catch { /* status open — ignore transient fetch errors */ }
    })();
  }, []);

  // Persist the captured IP the instant we see it: if the operator closes
  // the tab or the AP-teardown window kills the page mid-flight, they can
  // still find the device by reopening /wifi later (either from a bookmark
  // or by rejoining the hotspot) — see the LastKnownBanner below.
  useEffect(() => {
    if (isRealLanIp(lanIp) && mac) writeLastDevice({ lanIp, mac });
  }, [lanIp, mac]);

  const refreshNetworks = async () => {
    setScanning(true);
    try {
      const list = await getNetworks();
      const uniq = new Map<string, NetworkItem>();
      for (const n of list) {
        if (!n.ssid) continue;
        const prev = uniq.get(n.ssid);
        if (!prev || (n.signal ?? -100) > (prev.signal ?? -100)) {
          uniq.set(n.ssid, n);
        }
      }
      setNetworks([...uniq.values()].sort((a, b) => (b.signal ?? -100) - (a.signal ?? -100)));
    } catch { /* scan errors are OK — user can type SSID by hand */ }
    finally { setScanning(false); }
  };

  useEffect(() => { refreshNetworks(); }, []);

  // Aggressive poller during the connecting phase. Runs while the AP is
  // still up and grabs the new lan_ip the instant the device publishes it —
  // that value is our best chance at telling the operator the exact IP to
  // reconnect to, because once the AP tears down this URL is dead.
  //
  // lanIp / mac are tracked via refs (not the state closures) because the
  // poll's catch block fires MUCH later than the effect setup: by the time
  // the AP teardown drops the request, the closure's snapshot is often
  // still "" while state has captured real values one tick earlier. Using
  // state as an effect dep would restart the poller every tick and race
  // itself — refs are the correct read-only latest-value handle.
  const lanIpRef = useRef("");
  const macRef = useRef("");
  useEffect(() => { lanIpRef.current = lanIp; }, [lanIp]);
  useEffect(() => { macRef.current = mac; }, [mac]);

  useEffect(() => {
    if (phase !== "connecting") return;
    let stopped = false;
    const tick = async () => {
      try {
        const s = await getSetupStatus();
        if (stopped) return;
        if (isRealLanIp(s.lan_ip)) setLanIp(s.lan_ip);
        if (s.mac) setMac(s.mac);
        if (s.phase === "connected") { setPhase("connected"); return; }
        if (s.phase === "failed") {
          setPhase("failed");
          setError(s.error || "The robot could not join that Wi-Fi network.");
          return;
        }
      } catch {
        // AP tore down mid-poll — expected. Bail out and let the operator
        // reconnect to their home Wi-Fi and use the captured lan_ip / mDNS.
        if (stopped) return;
        // AP is gone — this URL is dead. Flip to the success screen with
        // whatever we managed to capture. It renders three tiers of options
        // (real IP, mDNS via mac, router-admin instructions) and gracefully
        // degrades to just the last one when nothing was captured, so even
        // an empty-refs case is more useful than an eternal "connecting…".
        // Only stay on the connecting screen if we have LITERALLY nothing —
        // that means the run probably never made it out the door.
        const gotIp = isRealLanIp(lanIpRef.current);
        const gotMac = !!macRef.current;
        if (gotIp || gotMac) setPhase("connected");
      }
    };
    const iv = setInterval(tick, 800);   // faster than /setup (grace window is tiny)
    tick();
    return () => { stopped = true; clearInterval(iv); };
  }, [phase]);

  // Fresh device (never fully set up) MUST have an LLM triplet — the backend
  // also validates, but front-loading it here catches the mistake before a
  // pointless network POST + AP teardown cycle.
  const llmRequired = provisioned === false;
  const missingLlm = llmRequired && (!llmApiKey.trim() || !llmBaseUrl.trim() || !llmModel.trim());

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!ssid.trim()) {
      setError("Pick a Wi-Fi network first.");
      return;
    }
    if (missingLlm) {
      setError("This robot isn't set up yet — enter the LLM API key, base URL, and model so the assistant can chat after connecting.");
      // Force the fields into view even if the operator collapsed them.
      setShowAdvanced(true);
      return;
    }
    setSubmitting(true);
    setPhase("connecting");
    try {
      await wifiProvision({
        ssid: ssid.trim(),
        password,
        llm_api_key: llmApiKey || undefined,
        llm_base_url: llmBaseUrl || undefined,
        llm_model: llmModel || undefined,
        stt_api_key: sttApiKey || undefined,
        stt_base_url: sttBaseUrl || undefined,
        stt_language: sttLanguage || undefined,
        tts_api_key: ttsApiKey || undefined,
        tts_base_url: ttsBaseUrl || undefined,
        tts_provider: ttsProvider || undefined,
        tts_voice: ttsVoice || undefined,
        admin_password: adminPassword || undefined,
        // Channel: send the identity only when the operator picked one; the
        // per-channel sub-tokens ride along and the backend switches on
        // `channel` before applying them.
        channel: channel || undefined,
        telegram_bot_token: teleBotToken || undefined,
        telegram_user_id: teleUserId || undefined,
        slack_bot_token: slackBotToken || undefined,
        slack_app_token: slackAppToken || undefined,
        slack_user_id: slackUserId || undefined,
        discord_bot_token: discordBotToken || undefined,
        discord_user_id: discordUserId || undefined,
      });
    } catch {
      // AP teardown kills the in-flight request on success; the poller
      // decides the outcome. A truly bad payload would 400 before teardown —
      // that's rare here (only ssid+password required) and still shows via
      // the poller.
    }
    setSubmitting(false);
  };

  // Wrong-origin visits: skip the form entirely so the redirect (fired in
  // the useEffect above) happens against a blank tree — no flash of setup UI
  // for an already-provisioned device the operator reached over LAN.
  if (wrongOrigin) return null;

  if (phase === "connected") {
    return <SuccessPanel lanIp={lanIp} mac={mac} />;
  }

  return (
    <div style={{
      minHeight: "100vh", background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif",
      display: "flex", justifyContent: "center",
      padding: "24px 16px",
    }}>
      <div style={{ width: "100%", maxWidth: 520 }}>

        <LastKnownBanner />

        <Header />

        <form onSubmit={onSubmit} noValidate
          // Suppress Google Password Manager / 1Password / LastPass on the whole
          // form: Wi-Fi + LLM/STT/TTS keys aren't user-account credentials, so
          // saving them into a password vault under this origin (192.168.100.1)
          // is worse than useless — it clutters the vault with entries that
          // never belong to a real account and pops up the "we saved a strong
          // password!" dialog every submit.
          autoComplete="off"
          data-form-type="other"
          style={{
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 12, padding: "20px 22px",
          }}>

          {/* ── Wi-Fi (required) ── */}
          <SubsectionCard title="Wi-Fi" hint="Required" accent="required">
            <Field label="Network">
              <div style={{ display: "flex", gap: 8 }}>
                <select
                  value={ssid}
                  onChange={(e) => setSsid(e.target.value)}
                  disabled={submitting || phase === "connecting"}
                  style={{ ...selectStyle, flex: 1 }}
                >
                  <option value="">Choose your Wi-Fi</option>
                  {networks.map((n) => (
                    <option key={n.ssid} value={n.ssid}>
                      {n.ssid}{n.signal ? ` — ${n.signal} dBm` : ""}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={refreshNetworks}
                  disabled={scanning || submitting}
                  aria-label="Rescan networks" title="Rescan networks"
                  style={iconBtn}
                >
                  <RefreshCw size={14} className={scanning ? "lm-spin-ico" : undefined} />
                </button>
              </div>
            </Field>

            <Field label="Password">
              <PasswordInput value={password} onChange={setPassword} show={showPw} onToggle={setShowPw}
                placeholder="Wi-Fi password (leave blank for open network)"
                disabled={submitting || phase === "connecting"} />
            </Field>
          </SubsectionCard>

          {/* ── AI Brain (required for fresh device, optional otherwise) ── */}
          {/* Hoisted out of Advanced when fresh so the operator can't miss
              the fields that decide whether chat actually works. On a
              provisioned device the same block still shows — but placeholder
              copy makes "leave blank" explicit. */}
          <SubsectionCard title="AI Brain (LLM)"
            accent={llmRequired ? "required" : "default"}
            hint={llmRequired ? "Required — chat needs this" : "Blank = keep current"}>
            <Field label={llmRequired ? "API Key *" : "API Key"}>
              <PasswordInput value={llmApiKey} onChange={setLlmApiKey}
                placeholder={llmRequired ? "sk-..." : "sk-... (blank = keep current)"}
                disabled={submitting || phase === "connecting"} />
            </Field>
            <Field label={llmRequired ? "Base URL *" : "Base URL"}>
              <TextInput value={llmBaseUrl} onChange={setLlmBaseUrl}
                placeholder="https://api.openai.com/v1"
                disabled={submitting || phase === "connecting"} />
            </Field>
            <Field label={llmRequired ? "Model *" : "Model"}>
              <TextInput value={llmModel} onChange={setLlmModel}
                placeholder="gpt-4o-mini"
                disabled={submitting || phase === "connecting"} />
            </Field>
          </SubsectionCard>

          {/* ── Messaging Channel (optional, always visible) ── */}
          {/* Sits between LLM and Advanced because a lot of operators want to
              set the channel here without expanding the STT/TTS/admin block. */}
          <SubsectionCard title="Messaging Channel"
            hint="Where the agent talks — optional, you can configure this later on the admin page after setup">
            <Field label="Channel">
              <select
                value={channel}
                onChange={(e) => setChannel(e.target.value as typeof channel)}
                disabled={submitting || phase === "connecting"}
                style={{ ...selectStyle, width: "100%" }}
              >
                <option value="">— skip, configure later on admin —</option>
                <option value="telegram">Telegram</option>
                <option value="slack">Slack</option>
                <option value="discord">Discord</option>
              </select>
            </Field>
            {channel === "telegram" && (
              <>
                <Field label="Bot Token">
                  <PasswordInput value={teleBotToken} onChange={setTeleBotToken}
                    placeholder="123456:ABC-DEF..."
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="User ID">
                  <TextInput value={teleUserId} onChange={setTeleUserId}
                    placeholder="123456789"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </>
            )}
            {channel === "slack" && (
              <>
                <Field label="Bot Token (xoxb-…)">
                  <PasswordInput value={slackBotToken} onChange={setSlackBotToken}
                    placeholder="xoxb-..."
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="App Token (xapp-…)">
                  <PasswordInput value={slackAppToken} onChange={setSlackAppToken}
                    placeholder="xapp-..."
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="User ID">
                  <TextInput value={slackUserId} onChange={setSlackUserId}
                    placeholder="U0123456789"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </>
            )}
            {channel === "discord" && (
              <>
                <Field label="Bot Token">
                  <PasswordInput value={discordBotToken} onChange={setDiscordBotToken}
                    placeholder="MTIzNDU2..."
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="User ID">
                  <TextInput value={discordUserId} onChange={setDiscordUserId}
                    placeholder="123456789012345678"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </>
            )}
          </SubsectionCard>

          {/* ── Advanced (optional) — collapsed by default ── */}
          {/* Hint copy is context-aware. This page's whole reason to exist is
              the "backend / autonomous.ai unavailable" fallback path, so on a
              FRESH device we tell the operator plainly they can skip these
              now and finish them from the admin page later — "keep existing"
              would be nonsense (nothing is on disk yet). On a re-provision
              (already-set-up device just changing Wi-Fi) the on-disk values
              genuinely stay, so we keep the original phrasing there. */}
          <button type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            style={{
              display: "flex", alignItems: "center", gap: 6,
              background: "transparent", border: "none", padding: "12px 0 8px",
              color: C.textDim, fontSize: 12, cursor: "pointer",
            }}>
            {showAdvanced ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <span style={{ fontWeight: 600 }}>Advanced — voice / admin</span>
            <span style={{ color: C.textMuted, fontWeight: 400 }}>
              &nbsp;(optional — configure later on the admin page after setup)
            </span>
          </button>

          {showAdvanced && (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <SubsectionCard title="Speech-to-text (STT)"
                hint="Optional — configure later on the admin page after setup">
                <Field label="API Key">
                  <PasswordInput value={sttApiKey} onChange={setSttApiKey}
                    placeholder="leave blank — set later on admin"
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="Base URL">
                  <TextInput value={sttBaseUrl} onChange={setSttBaseUrl}
                    placeholder="leave blank — set later on admin"
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="Language (ISO-639-1)">
                  <TextInput value={sttLanguage} onChange={setSttLanguage}
                    placeholder="en, vi, fr, …"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </SubsectionCard>

              <SubsectionCard title="Text-to-speech (TTS)"
                hint="Optional — configure later on the admin page after setup">
                <Field label="API Key">
                  <PasswordInput value={ttsApiKey} onChange={setTtsApiKey}
                    placeholder="leave blank — set later on admin"
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="Base URL">
                  <TextInput value={ttsBaseUrl} onChange={setTtsBaseUrl}
                    placeholder="leave blank — set later on admin"
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="Provider">
                  <TextInput value={ttsProvider} onChange={setTtsProvider}
                    placeholder="elevenlabs, openai, …"
                    disabled={submitting || phase === "connecting"} />
                </Field>
                <Field label="Voice">
                  <TextInput value={ttsVoice} onChange={setTtsVoice}
                    placeholder="voice id / name"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </SubsectionCard>

              <SubsectionCard title="Admin"
                hint="Optional — the robot's hardware suffix on the sticker is the default password">
                <Field label="Admin password">
                  <PasswordInput value={adminPassword} onChange={setAdminPassword}
                    placeholder="leave blank — hardware-suffix default"
                    disabled={submitting || phase === "connecting"} />
                </Field>
              </SubsectionCard>
            </div>
          )}

          {error && (
            <div style={errorBox}>
              <AlertCircle size={14} /> {error}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
            <button type="submit"
              disabled={submitting || phase === "connecting" || !ssid.trim() || missingLlm}
              style={{
                padding: "9px 22px", borderRadius: 8, border: "none",
                fontSize: 13, fontWeight: 600,
                background: "var(--lm-amber, #f5c25a)", color: "#0C0B09",
                cursor: submitting || phase === "connecting" ? "wait" : "pointer",
                opacity: submitting || phase === "connecting" || !ssid.trim() || missingLlm ? 0.6 : 1,
              }}>
              {phase === "connecting" ? "Connecting…" : submitting ? "Submitting…" : "Setup"}
            </button>
          </div>

          {phase === "connecting" && (
            <div style={{
              marginTop: 14, padding: "10px 14px",
              background: C.bg, border: `1px solid ${C.border}`, borderRadius: 8,
              fontSize: 12, color: C.textMuted,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <RefreshCw size={12} className="lm-spin-ico" />
                <b style={{ color: C.text }}>Joining {ssid}…</b>
              </div>
              <div style={{ marginBottom: 6, lineHeight: 1.5 }}>
                We know it worked the moment the robot publishes its new home-network IP.
                Then the hotspot tears down and this tab loses connection.
              </div>
              {isRealLanIp(lanIp) ? (
                <div style={{ color: "var(--lm-green, #34d399)" }}>
                  ✓ Got IP: <code>{lanIp}</code>&nbsp; — showing you the reconnect options shortly.
                </div>
              ) : (
                <div>Waiting for the robot to grab an IP from your router…</div>
              )}
            </div>
          )}
        </form>

        <div style={{ marginTop: 14, textAlign: "center", fontSize: 11, color: C.textMuted }}>
          On the AP hotspot — Advanced fields override on-disk config only when non-blank.
        </div>
      </div>
    </div>
  );
}

// Shows a one-tap link back to whatever device this browser last reached via
// the /wifi flow. Points at localStorage — survives tab close and browser
// restart for up to 7 days, so the operator who accidentally lost the success
// screen mid-teardown can still find their device without hunting the router
// admin panel.
function LastKnownBanner() {
  // Age is computed in the effect, not during render: Date.now() is impure and
  // would make this component re-render to a different value on every pass.
  const [last] = useState<(LastDevice & { ageMin: number }) | null>(() => {
    const d = readLastDevice();
    // Suppress the banner when the stored IP is the AP static — an older
    // build wrote it there before the leak was closed, and showing it as
    // "your device was last at 192.168.100.1" is misleading (that address
    // only ever means "AP mode is up right now").
    if (!d || !isRealLanIp(d.lanIp)) return null;
    return { ...d, ageMin: Math.round((Date.now() - d.savedAt) / 60000) };
  });
  if (!last) return null;
  const mdns = last.mac ? `${last.mac}.local` : "";
  const { ageMin } = last;
  const ageLabel = ageMin < 60 ? `${ageMin}m ago`
    : ageMin < 60 * 24 ? `${Math.round(ageMin / 60)}h ago`
    : `${Math.round(ageMin / (60 * 24))}d ago`;
  return (
    <div style={{
      marginBottom: 16, padding: "10px 14px",
      background: "rgba(52,211,153,0.08)", border: "1px solid rgba(52,211,153,0.25)",
      borderRadius: 10, display: "flex", alignItems: "center", gap: 10,
    }}>
      <History size={16} style={{ color: "var(--lm-green, #34d399)", flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, color: C.textDim }}>Last reached this robot {ageLabel} at</div>
        <div style={{ fontSize: 13, fontFamily: "ui-monospace, monospace", color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {last.lanIp}{mdns ? ` · ${mdns}` : ""}
        </div>
      </div>
      <a href={`http://${last.lanIp}/monitor`} style={{
        padding: "6px 12px", borderRadius: 6, textDecoration: "none",
        background: "var(--lm-green, #34d399)", color: "#0C0B09",
        fontSize: 11.5, fontWeight: 600, flexShrink: 0,
      }}>Open</a>
    </div>
  );
}

function Header() {
  return (
    <div style={{ marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
      <div style={{
        width: 42, height: 42, borderRadius: 10,
        background: "var(--lm-amber-dim, rgba(255,190,80,0.15))",
        color: "var(--lm-amber, #f5c25a)",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <Wifi size={20} />
      </div>
      <div>
        <div style={{ fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
          Set up robot
        </div>
        <div style={{ fontSize: 12.5, color: C.textDim, marginTop: 2 }}>
          Join your Wi-Fi. Optionally override LLM / voice / admin config.
        </div>
      </div>
    </div>
  );
}

function SuccessPanel({ lanIp, mac }: { lanIp: string; mac: string }) {
  // mDNS host prefix comes back as "<device-type>-<4 hex>" from
  // /api/device/setup/status.mac (see handler.SetupStatus). Append `.local`
  // to get the address Bonjour/Avahi advertises on the LAN.
  const mdns = mac ? `${mac}.local` : "";
  const mdnsHref = mdns ? `http://${mdns}/monitor` : "";
  const ipHref = isRealLanIp(lanIp) ? `http://${lanIp}/monitor` : "";
  // QR points at the IP first — it works even when the router filters mDNS
  // multicast (which is common on cheap consumer routers with AP isolation
  // enabled by default). Falls back to mDNS only when we somehow have a mac
  // but no IP, since a phone scanning a broken QR has no recourse.
  const qrHref = ipHref || mdnsHref;

  return (
    <div style={{
      minHeight: "100vh", background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif",
      display: "flex", alignItems: "center", justifyContent: "center", padding: 24,
    }}>
      <div style={{ width: "100%", maxWidth: 560 }}>
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 12, padding: "26px 22px",
        }}>
          <div style={{ textAlign: "center" }}>
            <div style={{
              width: 52, height: 52, borderRadius: "50%",
              background: "rgba(52,211,153,0.14)", color: "var(--lm-green, #34d399)",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              marginBottom: 14,
            }}>
              <CheckCircle2 size={28} />
            </div>
            <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>
              Robot is on your Wi-Fi
            </div>
            <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 18, lineHeight: 1.55 }}>
              Reconnect your computer to your home network, then use one of the
              addresses below. Or scan the QR with a phone that's already on
              home Wi-Fi.
            </div>
          </div>

          {/* Primary: raw LAN IP. Always works when both machines are on the
              same LAN — the only failure mode is the router handing out a
              different lease later, and this address is saved to localStorage
              so a stale copy can be spotted on the LastKnownBanner. */}
          {isRealLanIp(lanIp) && (
            <div style={{
              padding: "14px 16px", marginBottom: 12,
              background: "rgba(52,211,153,0.06)",
              border: "1px solid rgba(52,211,153,0.22)", borderRadius: 10,
            }}>
              <div style={{ fontSize: 10, color: "var(--lm-green, #34d399)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6, fontWeight: 700 }}>
                Recommended · direct IP
              </div>
              <LinkCard label="" href={ipHref} text={lanIp} accent="var(--lm-green, #34d399)" />
              <div style={{ fontSize: 11, color: C.textDim, marginTop: 8, lineHeight: 1.5 }}>
                Works as long as both this browser and the robot are on the
                same LAN. The IP was saved locally — <code>/wifi</code>
                remembers it for 7 days.
              </div>
            </div>
          )}

          {/* QR: scans with phone that's on the same LAN. Uses the IP (not
              mDNS) so it works regardless of the router's multicast policy. */}
          {qrHref && (
            <div style={{
              padding: "14px", marginBottom: 12,
              background: C.bg, border: `1px solid ${C.border}`,
              borderRadius: 10, textAlign: "center",
            }}>
              <div style={{ fontSize: 10, color: C.textMuted, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
                Scan with a phone on home Wi-Fi
              </div>
              <div style={{ display: "inline-block", padding: 10, background: "#fff", borderRadius: 8 }}>
                <QRCodeSVG value={qrHref} size={160} level="M" />
              </div>
              <div style={{ fontSize: 11, color: C.textDim, marginTop: 8, fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>
                {qrHref}
              </div>
            </div>
          )}

          {/* mDNS: nice when it works, but many home routers filter multicast
              between clients (AP isolation / IGMP snooping) — silently, with
              no way to tell from the UI, so `.local` links look broken. We
              show it as an OPTION with a plain-language caveat instead of
              promoting it as the primary. */}
          {mdns && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 10, color: C.textMuted, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                Domain address · works if your router forwards mDNS
              </div>
              <LinkCard label="" href={mdnsHref} text={mdns} />
              <div style={{ fontSize: 11, color: C.textDim, marginTop: 6, lineHeight: 1.5 }}>
                Some routers block multicast between clients (look for
                "AP&nbsp;isolation" / "Client&nbsp;isolation" / "multicast
                filtering" in router admin). If <code>.local</code> doesn't
                open, use the IP above.
              </div>
            </div>
          )}

          {/* Last resort: router admin page + expected device name. */}
          <div style={{ fontSize: 10, color: C.textMuted, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
            Last resort · router admin page
          </div>
          <div style={{ fontSize: 12, color: C.textDim, lineHeight: 1.55, marginBottom: 4 }}>
            Open your router's admin (usually{" "}
            <a href="http://192.168.1.1" style={{ color: "var(--lm-amber, #f5c25a)" }}>192.168.1.1</a>)
            and look for a robot named{" "}
            <code style={{ background: C.bg, padding: "1px 5px", borderRadius: 3 }}>
              {mac || "<device-type>-XXXX"}
            </code>{" "}
            in the DHCP lease list.
          </div>

          {!isRealLanIp(lanIp) && !mdns && (
            <div style={{
              ...errorBox, marginTop: 16, background: "rgba(255,190,80,0.08)",
              borderColor: "rgba(255,190,80,0.28)", color: "var(--lm-amber, #f5c25a)",
            }}>
              <AlertCircle size={14} /> Could not capture the robot's new address in
              time — only the router-admin option will work this run.
            </div>
          )}

          <div style={{ marginTop: 18, padding: "10px 12px", background: C.bg, border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 11, color: C.textMuted, lineHeight: 1.55 }}>
            <b>Heads up:</b> the session cookie was set on the hotspot origin —
            when you land on the new address you'll be asked for the admin
            password once. This address is also saved locally for 7 days —
            reopening <code>/wifi</code> shows it again.
          </div>
        </div>
      </div>
    </div>
  );
}

function LinkCard({ label, href, text, accent }: {
  label: string;
  href: string;
  text: string;
  accent?: string;
}) {
  const [copied, setCopied] = useState(false);
  const color = accent ?? "var(--lm-amber, #f5c25a)";
  const doCopy = async (e: React.MouseEvent) => {
    e.preventDefault(); e.stopPropagation();
    if (await copyText(href)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }
  };
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      background: C.bg, border: `1px solid ${C.border}`,
      borderRadius: 8, padding: "10px 14px",
      transition: "border-color 0.15s",
    }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = color)}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = C.border)}
    >
      <a href={href} style={{ flex: 1, minWidth: 0, textDecoration: "none" }}>
        {label && (
          <div style={{ fontSize: 10, color: C.textMuted, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
            {label}
          </div>
        )}
        <div style={{ fontSize: 14, fontFamily: "ui-monospace, monospace", color, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {text} →
        </div>
      </a>
      <button type="button" onClick={doCopy}
        title={copied ? "Copied" : "Copy full URL"}
        aria-label="Copy full URL"
        style={{
          display: "flex", alignItems: "center", justifyContent: "center",
          width: 30, height: 30, padding: 0, borderRadius: 6,
          background: "transparent", border: `1px solid ${C.border}`,
          color: copied ? "var(--lm-green, #34d399)" : C.textMuted,
          cursor: "pointer", flexShrink: 0,
        }}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}

// ─── shared bits ────────────────────────────────────────────────────────────
const selectStyle: React.CSSProperties = {
  boxSizing: "border-box",
  background: C.bg, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 13, color: C.text, outline: "none",
};
const inputStyle: React.CSSProperties = {
  width: "100%", boxSizing: "border-box",
  background: C.bg, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 13, color: C.text, outline: "none",
};
const iconBtn: React.CSSProperties = {
  background: "transparent", border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "0 10px",
  color: C.textMuted, cursor: "pointer",
  display: "flex", alignItems: "center",
};
const errorBox: React.CSSProperties = {
  background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
  borderRadius: 8, padding: "8px 12px", fontSize: 12, color: C.red,
  display: "flex", alignItems: "center", gap: 8, marginTop: 12,
};

// SubsectionCard wraps a related group of inputs in a bordered card so the
// operator can visually distinguish "this is my TTS block" from "this is my
// Channel block" — instead of one long stack of labels where the ownership
// of each field blurs.
//
// Visual design:
//   - Elevated look via a slightly-brighter background + stronger 1.5px
//     border than the page surface, so cards read as distinct chips against
//     the outer form.
//   - Subtle amber-tinted header divider under the title so the eye lands on
//     the section name first.
//   - `accent="required"` swaps the border/background/title to amber so the
//     LLM section (or any required block) reads as the visual anchor of the
//     form — the operator can't miss what they have to fill in.
function SubsectionCard({ title, hint, accent = "default", children }: {
  title: string;
  hint?: string;
  accent?: "default" | "required";
  children: React.ReactNode;
}) {
  const isRequired = accent === "required";
  const borderColor = isRequired
    ? "var(--lm-amber, #f5c25a)"
    : "color-mix(in srgb, var(--lm-border, #2a2622) 100%, transparent)";
  const bg = isRequired
    ? "color-mix(in srgb, var(--lm-amber, #f5c25a) 6%, var(--lm-bg, #14110d))"
    : "color-mix(in srgb, var(--lm-bg, #14110d) 92%, var(--lm-amber, #f5c25a) 4%)";
  const titleColor = isRequired ? "var(--lm-amber, #f5c25a)" : "var(--lm-text, #e8e2d5)";
  const shadow = isRequired
    ? "0 0 0 3px color-mix(in srgb, var(--lm-amber, #f5c25a) 12%, transparent), 0 2px 8px rgba(0,0,0,0.25)"
    : "0 1px 3px rgba(0,0,0,0.35), inset 0 1px 0 color-mix(in srgb, #fff 3%, transparent)";
  return (
    <div style={{
      border: `1.5px solid ${borderColor}`,
      borderRadius: 12,
      background: bg,
      padding: "14px 16px",
      marginTop: 12,
      boxShadow: shadow,
      transition: "border-color 0.15s, box-shadow 0.15s",
    }}>
      <div style={{
        display: "flex", alignItems: "baseline", justifyContent: "space-between",
        gap: 10, marginBottom: 10, paddingBottom: 10,
        borderBottom: `1px solid ${isRequired
          ? "color-mix(in srgb, var(--lm-amber, #f5c25a) 30%, transparent)"
          : "color-mix(in srgb, var(--lm-border, #2a2622) 60%, transparent)"}`,
      }}>
        <div style={{
          fontSize: 12.5, fontWeight: 700, color: titleColor,
          letterSpacing: "0.01em",
        }}>{title}</div>
        {hint && (
          <div style={{
            fontSize: 10.5, color: C.textMuted, textAlign: "right", maxWidth: "60%",
          }}>{hint}</div>
        )}
      </div>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <label style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>{label}</label>
      {children}
    </div>
  );
}

function TextInput({ value, onChange, placeholder, disabled }: {
  value: string; onChange: (v: string) => void; placeholder?: string; disabled?: boolean;
}) {
  return (
    <input type="text" value={value} onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder} disabled={disabled} style={inputStyle} />
  );
}

function PasswordInput({ value, onChange, show, onToggle, placeholder, disabled }: {
  value: string; onChange: (v: string) => void;
  show?: boolean; onToggle?: (v: boolean) => void;
  placeholder?: string; disabled?: boolean;
}) {
  // Local show/hide when caller doesn't manage it (used for all the advanced
  // fields — they each have their own eye without lifting state up).
  const [localShow, setLocalShow] = useState(false);
  const visible = show ?? localShow;
  const toggle = onToggle ?? setLocalShow;
  return (
    <div style={{ position: "relative" }}>
      <input type={visible ? "text" : "password"} value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} disabled={disabled}
        // Suppress Chrome's "strong password" popup + 1Password / LastPass
        // vault prompts. These fields are Wi-Fi / API-key inputs — not login
        // credentials — so saving them under this origin's password vault
        // just clutters the operator's stored passwords. `autoComplete="off"`
        // alone isn't enough (Chrome ignores it on type=password since ~2015)
        // — the data-* hints + a non-standard `name` are what actually stop
        // the suggestion popup.
        autoComplete="off"
        name="field-a"
        data-form-type="other"
        data-lpignore="true"
        data-1p-ignore="true"
        style={{ ...inputStyle, paddingRight: 36 }} />
      <button type="button" tabIndex={-1} onClick={() => toggle(!visible)}
        style={{
          position: "absolute", right: 0, top: 0, height: "100%",
          padding: "0 10px", background: "none", border: "none",
          color: C.textMuted, cursor: "pointer",
          display: "flex", alignItems: "center",
        }}>
        {visible ? <EyeOff size={14} /> : <Eye size={14} />}
      </button>
    </div>
  );
}
