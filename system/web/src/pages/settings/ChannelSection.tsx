import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, MessageSquare, AlertTriangle } from "lucide-react";
import { C, LockedField, SectionCard } from "@/components/setup/shared";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import { getAgentRuntime } from "@/lib/api";
import type { ChannelType } from "@/types";
import type { ChannelLoadedState } from "@/hooks/setup/types";

// Bot tokens now go through SecretUpdateField (write-only) — the server only
// returns has_* booleans so the previous "show saved token" affordance is
// gone. Channel IDs stay plain LockedField since they're not secrets.
export function ChannelSection({
  active, channel, setChannel, channelLoaded,
  teleToken, setTeleToken, teleUserId, setTeleUserId,
  slackBotToken, setSlackBotToken, slackAppToken, setSlackAppToken, slackUserId, setSlackUserId,
  discordBotToken, setDiscordBotToken, discordGuildId, setDiscordGuildId, discordUserId, setDiscordUserId,
  bluebubblesServerUrl, setBluebubblesServerUrl,
  bluebubblesPassword, setBluebubblesPassword,
  bluebubblesUserAddress, setBluebubblesUserAddress,
  bluebubblesCallerContext, setBluebubblesCallerContext,
}: {
  active: boolean;
  channel: ChannelType;
  setChannel: (v: ChannelType) => void;
  channelLoaded: ChannelLoadedState;
  teleToken: string; setTeleToken: (v: string) => void;
  teleUserId: string; setTeleUserId: (v: string) => void;
  slackBotToken: string; setSlackBotToken: (v: string) => void;
  slackAppToken: string; setSlackAppToken: (v: string) => void;
  slackUserId: string; setSlackUserId: (v: string) => void;
  discordBotToken: string; setDiscordBotToken: (v: string) => void;
  discordGuildId: string; setDiscordGuildId: (v: string) => void;
  discordUserId: string; setDiscordUserId: (v: string) => void;
  bluebubblesServerUrl: string; setBluebubblesServerUrl: (v: string) => void;
  bluebubblesPassword: string; setBluebubblesPassword: (v: string) => void;
  bluebubblesUserAddress: string; setBluebubblesUserAddress: (v: string) => void;
  bluebubblesCallerContext: string; setBluebubblesCallerContext: (v: string) => void;
}) {
  // iMessage guide starts collapsed — an operator who already has the three
  // values in hand should not scroll past a wall of Mac install steps every
  // time. Persist per-browser so a returning user does not have to re-open.
  const [imessageGuideOpen, setImessageGuideOpen] = useState<boolean>(() => {
    try { return localStorage.getItem("imessage-guide-open") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("imessage-guide-open", imessageGuideOpen ? "1" : "0"); } catch { /* private mode: silently skip */ }
  }, [imessageGuideOpen]);

  // Active agent runtime — used only by the iMessage panel to warn when the
  // current backend can't drive BlueBubbles. Kept as null until the fetch
  // returns so we render nothing (no false-negative "not Hermes" flash) on
  // slow devices; the field also stays null if the request errors, which is
  // the safe fallback since the InfoCard at the bottom already documents the
  // Hermes-only requirement in prose. Only fetches once the channel section
  // is actually visible — no point paying the round trip on the LLM tab.
  const [runtimeCurrent, setRuntimeCurrent] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    getAgentRuntime()
      .then((r) => { if (!cancelled) setRuntimeCurrent(r.current || ""); })
      .catch(() => { /* leave null — bottom InfoCard still explains the rule */ });
    return () => { cancelled = true; };
  }, [active]);
  const runtimeMismatch =
    channel === "imessage" && runtimeCurrent !== null && runtimeCurrent !== "hermes";

  return (
    <SectionCard id="channel" title="Messaging Channels" active={active}>
      <div style={{ marginBottom: 12 }}>
        <label style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>Channel</label>
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value as ChannelType)}
          style={{
            width: "100%", boxSizing: "border-box" as const,
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          <option value="telegram">Telegram</option>
          <option value="slack">Slack</option>
          <option value="discord">Discord</option>
          <option value="imessage">iMessage (BlueBubbles)</option>
        </select>
      </div>
      {channel === "telegram" && (
        <>
          <SecretUpdateField configured={channelLoaded.teleToken} label="Bot Token" id="tele_token" value={teleToken} onChange={setTeleToken} placeholder="123456:ABC-DEF..." />
          <LockedField lockedInitially={channelLoaded.teleUserId} label="User ID" id="tele_user_id" value={teleUserId} onChange={setTeleUserId} placeholder="123456789" />
        </>
      )}
      {channel === "slack" && (
        <>
          <SecretUpdateField configured={channelLoaded.slackBotToken} label="Bot Token" id="slack_bot_token" value={slackBotToken} onChange={setSlackBotToken} placeholder="xoxb-..." />
          <SecretUpdateField configured={channelLoaded.slackAppToken} label="App Token" id="slack_app_token" value={slackAppToken} onChange={setSlackAppToken} placeholder="xapp-..." />
          <LockedField lockedInitially={channelLoaded.slackUserId} label="User ID" id="slack_user_id" value={slackUserId} onChange={setSlackUserId} placeholder="U0123456789" />
        </>
      )}
      {channel === "discord" && (
        <>
          <SecretUpdateField configured={channelLoaded.discordBotToken} label="Bot Token" id="discord_bot_token" value={discordBotToken} onChange={setDiscordBotToken} placeholder="Bot token" />
          <LockedField lockedInitially={channelLoaded.discordGuildId} label="Guild ID" id="discord_guild_id" value={discordGuildId} onChange={setDiscordGuildId} placeholder="123456789" />
          <LockedField lockedInitially={channelLoaded.discordUserId} label="User ID" id="discord_user_id" value={discordUserId} onChange={setDiscordUserId} placeholder="123456789" />
        </>
      )}
      {channel === "imessage" && (
        <>
          {/* Runtime mismatch banner — iMessage/BlueBubbles is Hermes-only
              today. Runs BEFORE the form so an operator on the wrong runtime
              is stopped from filling in three fields that will never activate
              a working plugin. Rendered only after the runtime probe has
              returned a non-Hermes value; a null/loading state stays quiet so
              a slow API call cannot flash the warning at a Hermes device. */}
          {runtimeMismatch && (
            <div style={{
              background: "rgba(220, 38, 38, 0.10)",
              border: "1px solid rgba(220, 38, 38, 0.45)",
              borderRadius: 10, padding: "12px 14px", marginBottom: 16,
              display: "flex", alignItems: "flex-start", gap: 10,
            }}>
              <AlertTriangle size={18} color="#dc2626" strokeWidth={2.2} style={{ flexShrink: 0, marginTop: 1 }} />
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: 12.5, fontWeight: 700, color: "#dc2626", marginBottom: 4 }}>
                  Wrong runtime for the BlueBubbles bridge
                </div>
                <div style={{ fontSize: 11.5, color: C.text, lineHeight: 1.55, marginBottom: 8 }}>
                  This device is running{" "}
                  <b style={{ textTransform: "capitalize" }}>{runtimeCurrent || "unknown"}</b>.
                  The BlueBubbles path below only works with the <b>Hermes</b>{" "}
                  runtime — Hermes ships the plugin that listens on the webhook.
                  {runtimeCurrent === "openclaw" && (
                    <>
                      {" "}
                      OpenClaw does have iMessage support, but via a different
                      bridge (<code>@openclaw/imessage</code> + the{" "}
                      <code>imsg</code> CLI over SSH) — that path is not wired
                      into this UI yet.
                    </>
                  )}
                  {" "}Save on the wrong runtime and the credentials stay dormant.
                </div>
                <button
                  type="button"
                  onClick={() => { window.location.hash = "runtime"; }}
                  style={{
                    background: "#dc2626", border: "none", color: "#fff",
                    padding: "6px 12px", borderRadius: 6,
                    fontSize: 11.5, fontWeight: 600, cursor: "pointer",
                  }}
                >
                  Switch to Hermes runtime →
                </button>
              </div>
            </div>
          )}

          {/* Header row with amber Apple/message logo tile — visually anchors
              the iMessage form so an operator who scrolled past a wall of
              other channels knows they landed on the right one. */}
          <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 14 }}>
            <div style={{
              flexShrink: 0,
              width: 40, height: 40, borderRadius: 10,
              background: "#0A84FF",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <MessageSquare size={22} color="#fff" strokeWidth={2.2} fill="#fff" />
            </div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 3 }}>
                Connect iMessage (via BlueBubbles)
              </div>
              <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.5 }}>
                Apple has no official third-party iMessage API. You bring your own
                bridge: a Mac running BlueBubbles server, and this device talks to it.
              </div>
            </div>
          </div>

          {/* WHO IS THIS FOR — most-common misconception this channel hits.
              Operators buy the device, set up iMessage, then chat their own
              Mac's phone from their own iPhone expecting the bot to reply.
              iMessage tags SAME-Apple-ID traffic as isFromMe:true, and the
              plugin drops those as self-echoes to avoid reply loops. So the
              operator sees silence and thinks the setup is broken. Same
              pattern every other messaging bot has (a Telegram bot doesn't
              reply to itself either) — but iMessage's same-Apple-ID sync
              across iPhone + Mac makes it easier to trip into by accident.
              Set that expectation before the operator spends 20 minutes on
              setup + BlueBubbles install; the amber card below the identity
              field only covers the narrow testing workaround. */}
          <div style={{
            background: "rgba(10, 132, 255, 0.08)",
            border: "1px solid rgba(10, 132, 255, 0.35)",
            borderRadius: 10, padding: "12px 14px", marginBottom: 16,
            fontSize: 11.5, color: C.text, lineHeight: 1.6,
          }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, color: "#0A84FF", marginBottom: 6 }}>
              Who is this channel for?
            </div>
            <div style={{ marginBottom: 6 }}>
              This channel is for <b>other people to message your Mac&apos;s
              iMessage handle</b> — a customer, a client, a friend on their
              own iPhone / iPad / Mac with a <b>different Apple ID</b>. When
              they text your Mac&apos;s phone or email, the bot answers on
              your behalf. Think WhatsApp Business bot or a Telegram bot,
              routed through iMessage.
            </div>
            <div style={{ color: C.textDim }}>
              <b>Not designed for chatting with yourself.</b> If your own
              iPhone shares the same Apple ID as the Mac, iMessage flags every
              message you send as <code>isFromMe:true</code> and the plugin
              drops it to avoid a reply loop (bot answering its own reply).
              Use a friend&apos;s iPhone to try the bot, or borrow any iPhone
              signed into a different Apple ID.
            </div>
          </div>

          <InfoCard tone="blue" title="BEFORE YOU START">
            You need <b>a Mac that stays powered on</b> (any Mac — laptop, Mac
            mini, iMac) with <b>Messages.app signed into your iMessage account</b>,
            <b> &ldquo;Enable Messages in iCloud&rdquo; ticked</b>{" "}
            (Messages.app → Settings → iMessage), and this device on the{" "}
            <b>same network as the Mac</b> (or reachable via ngrok / Cloudflare
            tunnel). Without iCloud sync, older threads and history don&apos;t
            reach the Mac and the bot may look silent even when it is not.
          </InfoCard>

          <FieldLabel htmlFor="bb_server_url">BlueBubbles Server URL</FieldLabel>
          <input
            id="bb_server_url"
            type="text"
            value={bluebubblesServerUrl}
            onChange={(e) => setBluebubblesServerUrl(e.target.value)}
            placeholder="http://192.168.1.10:1234 or https://<your>.ngrok.app"
            autoComplete="off"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 6, fontFamily: "monospace" }}
          />
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
            LAN URL for same-Wi-Fi setup, or a public tunnel URL if the Mac is elsewhere.{" "}
            <button
              type="button"
              onClick={() => {
                setImessageGuideOpen(true);
                document
                  .getElementById("imessage-guide-anchor")
                  ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
              }}
              style={{
                background: "none", border: "none", padding: 0,
                color: C.amber, cursor: "pointer",
                font: "inherit", fontWeight: 500, textDecoration: "underline",
                textUnderlineOffset: 2,
              }}
            >
              See how to install BlueBubbles
            </button>.
          </div>

          <FieldLabel htmlFor="bb_password">Server Password</FieldLabel>
          <SecretUpdateField
            configured={channelLoaded.bluebubblesPassword}
            label=""
            id="bb_password"
            value={bluebubblesPassword}
            onChange={setBluebubblesPassword}
            placeholder="The password you set in BlueBubbles"
          />

          <FieldLabel htmlFor="bb_user_address">Your iMessage Handle</FieldLabel>
          <LockedField
            lockedInitially={channelLoaded.bluebubblesUserAddress}
            label=""
            id="bb_user_address"
            value={bluebubblesUserAddress}
            onChange={setBluebubblesUserAddress}
            placeholder="+84901234567 or you@example.com"
          />
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 12, lineHeight: 1.5 }}>
            Your phone number or email for the operator's home conversation.
            Customer messages from other addresses are accepted by default.
          </div>
          {/* The amber "Testing from your OWN iPhone?" workaround card used to
              live here — removed. In practice the two-settings-must-disagree
              workaround is unreliable (iMessage tags isFromMe inconsistently
              even after the swap, so tests came back hit-or-miss and the
              amber block only added noise for shop/service operators reading
              the form). The blue "Who is this channel for?" callout at the
              top already tells the operator to borrow a friend's iPhone with
              a different Apple ID, which is the actually-reliable path. */}

          {/* Optional caller-context prompt — non-secret plaintext prepended
              to every incoming iMessage as a system-context block so the LLM
              treats callers correctly (shops / customer support / services
              set-ups). Uses a plain <textarea> rather than LockedField since
              it is multi-line and not a secret. Empty preserves default
              behaviour where the bot treats the caller as the owner. */}
          <FieldLabel htmlFor="bb_caller_context">Customer instructions (optional)</FieldLabel>
          <textarea
            id="bb_caller_context"
            rows={4}
            value={bluebubblesCallerContext}
            onChange={(e) => setBluebubblesCallerContext(e.target.value)}
            placeholder="e.g. The sender is a customer of a clothing shop. Introduce yourself as 'the assistant for Anna's Boutique'. Suggest products, share prices. Do not reveal the owner's personal information. If a customer wants to order, take their phone number and pass it back to the owner."
            spellCheck={false}
            style={{
              width: "100%",
              boxSizing: "border-box",
              background: C.surface,
              border: `1px solid ${C.border}`,
              borderRadius: 7,
              padding: "8px 11px",
              fontSize: 12.5,
              color: C.text,
              outline: "none",
              fontFamily: "monospace",
              minHeight: 90,
              resize: "vertical",
              marginBottom: 6,
            }}
          />
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 16, lineHeight: 1.5 }}>
            Optional. Prepended to every incoming message as a system-context
            block so the LLM treats customers correctly. Great for shops /
            services / customer support setups. Leave empty for default
            behaviour (bot treats caller as owner).
          </div>

          {/* Collapsible walkthrough — 5 steps. Header always visible so a
              first-time operator sees where to click; body starts collapsed
              so a returning user with credentials in hand does not scroll
              past it. The `id` on the outer div is a scroll target for the
              Server URL hint link above. */}
          <div id="imessage-guide-anchor" style={{
            background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10,
            padding: "10px 14px", marginBottom: 12,
          }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <button
                type="button"
                onClick={() => setImessageGuideOpen((v) => !v)}
                style={{
                  background: "none", border: "none", padding: 0, cursor: "pointer",
                  display: "inline-flex", alignItems: "center", gap: 6,
                  color: C.textDim, fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em",
                }}
                aria-expanded={imessageGuideOpen}
              >
                {imessageGuideOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                HOW TO INSTALL BLUEBUBBLES ON YOUR MAC
              </button>
              <a
                href="https://bluebubbles.app/install"
                target="_blank"
                rel="noreferrer"
                style={{ color: C.amber, fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 4, textDecoration: "none" }}
              >
                Open BlueBubbles <ExternalLink size={11} />
              </a>
            </div>
            {imessageGuideOpen && (
              <div style={{ marginTop: 12, fontSize: 11.5, color: C.textDim, lineHeight: 1.6 }}>
                <Step n={1}>
                  On the Mac you want to use as the bridge, open Messages.app
                  and confirm you are <b>signed in to your Apple ID</b> and can
                  send / receive iMessages normally. If not, sign in first —
                  BlueBubbles does not create an iMessage account, it only
                  bridges the one Messages.app is already using.
                </Step>
                <Step n={2}>
                  Download the <b>BlueBubbles Server</b> from{" "}
                  <a
                    href="https://bluebubbles.app/install"
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: C.amber, display: "inline-flex", alignItems: "center", gap: 3 }}
                  >
                    bluebubbles.app/install <ExternalLink size={10} />
                  </a>
                  {" "}(pick the macOS build). Open the downloaded <code
                    style={{
                      background: C.surface, border: `1px solid ${C.border}`, borderRadius: 4,
                      padding: "1px 5px", fontFamily: "monospace", fontSize: 10.5,
                    }}
                  >.dmg</code>, drag <b>BlueBubbles</b> into <b>Applications</b>, then launch it.
                </Step>
                <Step n={3}>
                  <b style={{ color: C.amber }}>Grant the four macOS permissions</b>{" "}
                  it asks for the first time — without these it cannot read or
                  send messages:
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18, listStyle: "disc" }}>
                    <li><b>Full Disk Access</b> (to read Messages database)</li>
                    <li><b>Accessibility</b> (to send messages via Messages.app)</li>
                    <li><b>Automation</b> → allow BlueBubbles to control Messages</li>
                    <li><b>Contacts</b> (optional, resolves names on incoming messages)</li>
                  </ul>
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    If you miss a prompt, open <b>System Settings → Privacy &amp; Security</b>{" "}
                    and toggle BlueBubbles on under each section listed above.
                  </div>
                </Step>
                <Step n={4}>
                  In the BlueBubbles Server window, open <b>Settings → Password</b>{" "}
                  and set a password. Paste the same password into the{" "}
                  <b>Server Password</b> field above.
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    Any string works — make it long / random. The device is the
                    only client that will use it.
                  </div>
                </Step>
                <Step n={5}>
                  Pick how the device reaches your Mac and paste the URL into{" "}
                  <b>BlueBubbles Server URL</b> above:
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18, listStyle: "disc" }}>
                    <li>
                      <b>Same Wi-Fi</b> (easiest): in the BlueBubbles Server
                      window look for the LAN URL (e.g.{" "}
                      <code style={{
                        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 4,
                        padding: "1px 5px", fontFamily: "monospace", fontSize: 10.5,
                      }}>http://192.168.1.10:1234</code>) — copy it.
                    </li>
                    <li>
                      <b>Different network</b>: BlueBubbles has built-in{" "}
                      <b>ngrok</b> and <b>Cloudflare Tunnel</b> options under{" "}
                      <b>Settings → Server Connection</b>. Enable one, wait
                      for it to print a public URL, copy that. Same-machine{" "}
                      <b>Tailscale</b> also works (paste your Tailnet
                      hostname / IP + port).
                    </li>
                  </ul>
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    Whichever URL you paste,{" "}
                    <b>keep the Mac powered on and BlueBubbles running</b> —
                    if it quits or the Mac sleeps, iMessage on this device
                    goes offline.
                  </div>
                </Step>
                <Step n={6} last>
                  Paste your <b>iMessage handle</b> (the phone number or Apple
                  ID email you use to send / receive iMessage — the same one
                  showing under Messages.app → Settings → iMessage) into{" "}
                  <b>Your iMessage Handle</b> above, then click <b>Save Changes</b>.
                </Step>
              </div>
            )}
          </div>

          <InfoCard tone="warning" title={null} icon={<AlertTriangle size={14} color={C.amber} />}>
            The BlueBubbles bridge above is a <b>Hermes-only</b> path — it uses
            the plugin baked into the Hermes gateway that listens on the
            device's webhook. OpenClaw ships its own iMessage plugin{" "}
            (<code>@openclaw/imessage</code>) that talks to the Mac through the{" "}
            <code>imsg</code> CLI (SSH stdio, no webhook, no port) instead;
            that route isn't wired into this UI yet. If you're on Codex,
            OpenCode, or Claude Code and want iMessage, switch to Hermes under{" "}
            <b>Settings → Runtime</b>.
          </InfoCard>
        </>
      )}
    </SectionCard>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function InfoCard({
  tone,
  title,
  titleRight,
  icon,
  children,
}: {
  tone: "blue" | "warning" | "plain";
  title: string | null;
  titleRight?: React.ReactNode;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const bg = tone === "warning" ? "var(--lm-amber-dim)" : tone === "blue" ? "var(--lm-info-bg, rgba(30,64,175,0.10))" : C.bg;
  const border = tone === "warning" ? "var(--lm-amber-glow)" : tone === "blue" ? "var(--lm-info-border, rgba(59,130,246,0.35))" : C.border;
  const titleColor = tone === "warning" ? C.amber : tone === "blue" ? C.amber : C.textDim;
  return (
    <div style={{
      background: bg, border: `1px solid ${border}`, borderRadius: 10,
      padding: "12px 14px", marginBottom: 14, fontSize: 11.5, color: C.textDim, lineHeight: 1.6,
    }}>
      {(title || titleRight || icon) && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: title ? 8 : 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {icon}
            {title && <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em", color: titleColor }}>{title}</div>}
          </div>
          {titleRight}
        </div>
      )}
      <div>{children}</div>
    </div>
  );
}

function Step({ n, last, children }: { n: number; last?: boolean; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, marginBottom: last ? 0 : 10 }}>
      <div style={{
        flexShrink: 0,
        width: 18, height: 18, borderRadius: 9,
        background: C.amberDim, color: C.amber,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 10.5, fontWeight: 700,
      }}>{n}</div>
      <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.6, minWidth: 0 }}>{children}</div>
    </div>
  );
}

function FieldLabel({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return (
    <label htmlFor={htmlFor} style={{ display: "block", fontSize: 11.5, fontWeight: 600, color: C.text, marginBottom: 5 }}>
      {children}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  padding: "9px 12px",
  fontSize: 12.5,
  color: C.text,
  outline: "none",
};
