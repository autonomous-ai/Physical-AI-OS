import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Facebook, Eye, EyeOff, Lock, ExternalLink, AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";
import { C } from "@/components/setup/shared";
import { getConnector, setConnectorPAT, removeConnector } from "@/lib/api";
import type { ConnectorInfo } from "@/lib/api";

// FacebookSection — the on-device settings surface for the Facebook Fan Page
// posting skill. Layout mirrors the ecm-website admin PAT connector modal
// (see connectorAuthRegistry's PatConnectModal): a compact "Connect Facebook
// Fan Page" card with the mandatory-prerequisites callout, a numbered
// how-to-get-the-token walkthrough, the two inputs (Page ID + Page Access
// Token), a bottom safety note, and a right-aligned Cancel/Connect pair.
//
// Storage: NOT config.json. This writes through the same connectorWriter the
// MQTT connector.set.<code> dispatcher uses, via POST
// /api/device/connectors/pat → <OpenclawConfigDir>/workspace/configs/
// facebook_access_tokens.json. That is the exact file the skill reads and
// the same file the ecm-website admin's connector flow would write when it
// ships — no drift between local paste and remote push.
//
// This section renders its own Cancel/Connect footer (SectionCard-scoped, not
// modal-shaped) so SettingsPanel excludes "facebook" from the shared Save
// button chrome. Cancel resets the local form; Connect submits.
const CONNECTOR_CODE = "facebook";

export function FacebookSection({ active }: { active: boolean }) {
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [connectedPageId, setConnectedPageId] = useState<string>("");
  const [connectedAt, setConnectedAt] = useState<number>(0);
  const [pageId, setPageId] = useState("");
  const [pageAccessToken, setPageAccessToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  // Guide starts collapsed to keep the form short. Persist the choice per-
  // browser so an operator who wants it open doesn't have to re-expand on
  // every visit; fall back to false on any storage error (private windows).
  const [guideOpen, setGuideOpen] = useState<boolean>(() => {
    try { return localStorage.getItem("fb-guide-open") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("fb-guide-open", guideOpen ? "1" : "0"); } catch { /* private mode: silently skip */ }
  }, [guideOpen]);
  const [submitting, setSubmitting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    getConnector(CONNECTOR_CODE)
      .then((r: ConnectorInfo) => {
        setConnected(!!r.connected);
        setConnectedPageId(r.credentials?.page_id ?? "");
        setConnectedAt(r.obtained_at ?? 0);
      })
      .catch(() => {
        // A device that has never had this connector set answers with
        // connected:false; a hard error only fires on 500. Silent on 500 is
        // fine — the UI shows the empty form and the operator can try again.
      })
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const reset = () => {
    setPageId("");
    setPageAccessToken("");
    setError(null);
    setShowToken(false);
  };

  // Explicit click handler, not a form submit. FacebookSection is rendered
  // inside SettingsPanel's shared <form id="edit-form"> and HTML forbids
  // nested forms — a browser flattens them, so an inner form's submit event
  // never fires and our POST never leaves the page. Using onClick sidesteps
  // that entirely; the Enter-key affordance is handled by onKeyDown below.
  const onSubmit = async (e: React.MouseEvent | React.KeyboardEvent) => {
    e.preventDefault();
    setError(null);
    const id = pageId.trim();
    const token = pageAccessToken.trim();
    if (!id) { setError("Page ID is required."); return; }
    if (!/^\d{6,32}$/.test(id)) {
      setError("Page ID must be numeric (typically ~15 digits).");
      return;
    }
    if (!token) { setError("Page Access Token is required."); return; }
    if (token.length < 40) {
      setError("Page Access Token looks too short — copy the whole string from Graph API Explorer.");
      return;
    }
    setSubmitting(true);
    try {
      await setConnectorPAT({
        connector: CONNECTOR_CODE,
        api_key: token,
        credentials: { page_id: id },
      });
      toast.success("Facebook Fan Page connected.");
      reset();
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const onDisconnect = async () => {
    if (!connected) return;
    if (!window.confirm("Disconnect this Facebook Fan Page? The robot will keep the Page ID until you connect again.")) return;
    setDisconnecting(true);
    try {
      await removeConnector(CONNECTOR_CODE);
      toast.success("Facebook Fan Page disconnected.");
      setConnected(false);
      setConnectedPageId("");
      setConnectedAt(0);
      reset();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Disconnect failed.");
    } finally {
      setDisconnecting(false);
    }
  };

  // Bypass SectionCard: this section renders a modal-style card whose own
  // header replaces SectionCard's title chip, so we mount/unmount inline and
  // keep the display:none idiom the other sections use.
  return (
    <div
      id="section-facebook"
      className="lm-card lm-fade-in"
      style={{
        display: active ? "block" : "none",
        padding: "22px 24px",
        marginBottom: 16,
      }}
    >
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <div
          onKeyDown={(e) => {
            // Preserve the Enter-to-submit affordance without a real <form>.
            // Only fires when the token/page-id inputs are focused; ignores
            // Enter inside anything else (e.g. no textarea here today, but
            // keeps behaviour narrow if one is added later).
            const target = e.target as HTMLElement;
            if (e.key === "Enter" && target.tagName === "INPUT") {
              if (!submitting && pageId && pageAccessToken) onSubmit(e);
            }
          }}
        >
          <ModalHeader connected={connected} connectedAt={connectedAt} connectedPageId={connectedPageId} />

          <InfoCard tone="blue" title="BEFORE YOU START">
            You must be an <b>admin</b> of the Facebook Page you want to post
            to. Personal profiles are not supported — Meta's Graph API can
            only publish to a Page.
          </InfoCard>

          <FieldLabel htmlFor="fb_page_id">Facebook Page ID</FieldLabel>
          <input
            id="fb_page_id"
            type="text"
            value={pageId}
            onChange={(e) => setPageId(e.target.value)}
            placeholder="e.g. 123456789012345"
            autoComplete="off"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 6, fontFamily: "monospace" }}
          />
          {/* One-line hint under the Page ID input — a link that expands the
              walkthrough below so the how-to lives in exactly ONE place. The
              hint used to inline the full "Page Transparency" instructions
              which duplicated step 6 of the guide and made the form scroll.
              openGuide → expand + scroll into view so people who click the
              link actually see the section they were pointed at. */}
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
            Don't know your Page ID?{" "}
            <button
              type="button"
              onClick={() => {
                setGuideOpen(true);
                document
                  .getElementById("fb-guide-anchor")
                  ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
              }}
              style={{
                background: "none", border: "none", padding: 0,
                color: C.amber, cursor: "pointer",
                font: "inherit", fontWeight: 500, textDecoration: "underline",
                textUnderlineOffset: 2,
              }}
            >
              See how to find it
            </button>
            .
          </div>

          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 5 }}>
            <FieldLabel htmlFor="fb_page_token">Page Access Token</FieldLabel>
            <div style={{ fontSize: 11, color: C.textMuted, display: "flex", alignItems: "center", gap: 8 }}>
              <TokenCounter length={pageAccessToken.length} />
              <button
                type="button"
                onClick={() => setShowToken((v) => !v)}
                aria-label={showToken ? "Hide" : "Show"}
                style={{ background: "none", border: "none", padding: 0, color: C.textMuted, cursor: "pointer", display: "flex", alignItems: "center" }}
              >
                {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </div>
          <input
            id="fb_page_token"
            type={showToken ? "text" : "password"}
            value={pageAccessToken}
            onChange={(e) => setPageAccessToken(e.target.value)}
            placeholder="paste your Page Access Token"
            autoComplete="off"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 12, fontFamily: "monospace" }}
          />

          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            fontSize: 11, color: C.amber, marginBottom: 16,
          }}>
            <Lock size={11} /> Stored on your robot only. Never uploaded to our servers.
          </div>

          {/* Collapsible walkthrough — placed BELOW the form fields (after the
              Privacy line) so an operator who already has the token in hand
              sees the form first without scrolling past a wall of steps. Header
              row is always visible so people who need the walkthrough know
              where to click. Expanded state persisted in localStorage — see
              guideOpen. The `id` is a scroll target for the Page-ID hint link
              above so a click on "See how to find it" jumps here. */}
          <div id="fb-guide-anchor" style={{
            background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10,
            padding: "10px 14px", marginBottom: 12,
          }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <button
                type="button"
                onClick={() => setGuideOpen((v) => !v)}
                style={{
                  background: "none", border: "none", padding: 0, cursor: "pointer",
                  display: "inline-flex", alignItems: "center", gap: 6,
                  color: C.textDim, fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em",
                }}
                aria-expanded={guideOpen}
              >
                {guideOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                HOW TO GET YOUR PAGE ACCESS TOKEN
              </button>
              <a
                href="https://developers.facebook.com/tools/explorer/"
                target="_blank"
                rel="noreferrer"
                style={{ color: C.amber, fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 4, textDecoration: "none" }}
              >
                Open Graph Explorer <ExternalLink size={11} />
              </a>
            </div>
            {guideOpen && (
              <div style={{ marginTop: 12, fontSize: 11.5, color: C.textDim, lineHeight: 1.6 }}>
                <Step n={1}>
                  Open the <b>Graph API Explorer</b> and sign in with the Facebook
                  account that manages the Page.
                </Step>
                <Step n={2}>
                  Pick a Meta app in the <b>Meta App</b> dropdown (any app you own
                  works for testing).
                </Step>
                <Step n={3}>
                  Click <b>User Token</b> → tick the 6 permissions below, then{" "}
                  <b>Generate Access Token</b>:
                  <div style={{
                    background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6,
                    padding: "6px 8px", marginTop: 5, fontFamily: "monospace", fontSize: 10.5,
                    color: C.text, lineHeight: 1.55, wordBreak: "break-word",
                  }}>
                    pages_show_list, pages_manage_posts, pages_read_engagement,<br />
                    pages_read_user_content, pages_manage_engagement, read_insights
                  </div>
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    First 3 = post to Page. Last 3 = read + reply to comments +
                    analytics. Fewer is fine if the skill's auto-comment / insights
                    features are disabled.
                  </div>
                </Step>
                <Step n={4}>
                  <b style={{ color: C.amber }}>Extend the token to ~60 days</b>{" "}
                  <span style={{ color: C.textMuted }}>(strongly recommended for
                  schedules / cron — the raw token from step 3 expires in ~1 hour)</span>.
                  Open the{" "}
                  <a
                    href="https://developers.facebook.com/tools/debug/accesstoken/"
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: C.amber, display: "inline-flex", alignItems: "center", gap: 3 }}
                  >
                    Access Token Debugger <ExternalLink size={10} />
                  </a>{" "}
                  → paste the token from step 3 → click <b>Debug</b> → scroll down
                  and click <b>Extend Access Token</b> (Facebook reauth prompt may
                  appear). Copy the <b>new</b> token that shows below "Your new
                  extended access token".
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    Paste this extended token <b>back</b> into the "Access Token"
                    box in Graph API Explorer, replacing the short-lived one, before
                    continuing to step 5. The Page Access Token derived from this
                    long-lived User Token{" "}
                    <b style={{ color: C.amber }}>never expires</b> — scheduled
                    tasks keep running until you revoke the token or Facebook
                    terminates the session.
                  </div>
                </Step>
                <Step n={5}>
                  <b style={{ color: C.amber }}>This is the step operators get
                  wrong most often.</b> In the <b>User or Page</b> dropdown
                  (right panel, under Meta App — labelled "Người dùng hoặc Trang"
                  in Vietnamese), change <b>User Token</b> → pick your Page's
                  name (e.g. "Elvis"). The <b>Access Token</b> box on top{" "}
                  <b style={{ color: C.amber }}>auto-switches</b> to the Page
                  Access Token — copy <b>that</b> one. The token shown while the
                  dropdown still says "User Token" is a <b>User Access Token</b>
                  {" "}and Meta refuses it on Page posts (error #200) — same
                  scopes, wrong owner.
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    <b style={{ color: C.text }}>Verify before pasting:</b>{" "}
                    drop the token into the{" "}
                    <a
                      href="https://developers.facebook.com/tools/debug/accesstoken/"
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: C.amber }}
                    >
                      Access Token Debugger
                    </a>
                    {" "}and click <b>Debug</b>. The row <b>Type</b> must read{" "}
                    <b style={{ color: C.amber }}>PAGE</b> (not USER). If it says
                    USER, redo this step.
                  </div>
                  <div style={{ marginTop: 4, color: C.textMuted, fontSize: 10.5 }}>
                    Only Pages you've authorized this app for appear in the
                    dropdown. Missing a Page? Go to{" "}
                    <a
                      href="https://www.facebook.com/settings?tab=business_tools"
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: C.amber }}
                    >
                      facebook.com Business Integrations
                    </a>
                    → open the app → tick the extra Pages → regenerate the token.
                  </div>
                </Step>
                <Step n={6} last>
                  Paste the Page Access Token above along with your Page ID, then
                  click <b>Connect</b>.
                </Step>
              </div>
            )}
          </div>

          <InfoCard tone="warning" title={null} icon={<AlertTriangle size={14} color={C.amber} />}>
            Short-lived tokens expire in about an hour. For long-term use,
            exchange for a{" "}
            <a
              href="https://developers.facebook.com/docs/pages/access-tokens/#long-lived-page-access-tokens"
              target="_blank"
              rel="noreferrer"
              style={{ color: C.amber }}
            >
              long-lived Page Access Token
            </a>{" "}
            first — those effectively never expire.
          </InfoCard>

          {error && (
            <div style={{
              background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
              borderRadius: 8, padding: "8px 12px", fontSize: 11.5, color: C.red, marginBottom: 14,
            }}>
              {error}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
            {connected ? (
              <button
                type="button"
                onClick={onDisconnect}
                disabled={disconnecting}
                style={{
                  padding: "8px 14px", borderRadius: 8, fontSize: 12, fontWeight: 500,
                  background: "none",
                  border: `1px solid ${C.border}`,
                  color: C.red,
                  cursor: disconnecting ? "not-allowed" : "pointer",
                  opacity: disconnecting ? 0.5 : 1,
                }}
              >
                {disconnecting ? "Disconnecting…" : "Disconnect"}
              </button>
            ) : <span />}

            <div style={{ display: "flex", gap: 10 }}>
              <button
                type="button"
                onClick={reset}
                disabled={submitting || (!pageId && !pageAccessToken)}
                style={{
                  padding: "8px 18px", borderRadius: 8, fontSize: 12, fontWeight: 500,
                  background: "none",
                  border: `1px solid ${C.border}`,
                  color: C.text,
                  cursor: submitting || (!pageId && !pageAccessToken) ? "not-allowed" : "pointer",
                  opacity: submitting || (!pageId && !pageAccessToken) ? 0.5 : 1,
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onSubmit}
                disabled={submitting || !pageId || !pageAccessToken}
                style={{
                  padding: "8px 22px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                  background: submitting || !pageId || !pageAccessToken ? C.surface : C.amber,
                  color: submitting || !pageId || !pageAccessToken ? C.textMuted : "var(--lm-on-amber)",
                  border: "none",
                  cursor: submitting || !pageId || !pageAccessToken ? "not-allowed" : "pointer",
                  transition: "all 0.15s",
                }}
              >
                {submitting ? "Connecting…" : connected ? "Reconnect" : "Connect"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

// ModalHeader — logo + title + description row. Named "Modal" because it
// visually anchors the card like the modal header of the ecm-website's PAT
// connector, even though this renders in-page (not a real dialog).
function ModalHeader({
  connected,
  connectedAt,
  connectedPageId,
}: {
  connected: boolean;
  connectedAt: number;
  connectedPageId: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 14, marginBottom: 18 }}>
      <div style={{
        flexShrink: 0,
        width: 44, height: 44, borderRadius: 12,
        background: "#1877F2",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <Facebook size={26} color="#fff" strokeWidth={2.2} fill="#fff" />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: C.text, marginBottom: 3 }}>
          Connect Facebook Fan Page
        </div>
        <div style={{ fontSize: 12, color: C.textDim, lineHeight: 1.5 }}>
          Add a Page Access Token so the robot can post to your Facebook Page.
        </div>
        {connected && (
          <div style={{ marginTop: 8, fontSize: 11, color: C.green, display: "inline-flex", alignItems: "center", gap: 6 }}>
            ✓ connected
            {connectedPageId && <span style={{ color: C.textMuted }}>· Page {connectedPageId}</span>}
            {connectedAt > 0 && (
              <span style={{ color: C.textMuted }}>
                · {new Date(connectedAt * 1000).toLocaleDateString()}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

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
    <div style={{ display: "flex", gap: 10, marginBottom: last ? 0 : 8 }}>
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

// TokenCounter reproduces the "✓ 16/16" affordance from the Gmail modal —
// a green check when the token looks long enough to be a real Page Access
// Token, plus the character count. Threshold is intentionally loose
// (>=40 chars) because Meta's tokens are variable-length.
function TokenCounter({ length }: { length: number }) {
  if (length === 0) return null;
  const ok = length >= 40;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, color: ok ? C.green : C.textMuted }}>
      {ok && <span>✓</span>}
      <span>{length}</span>
    </span>
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


