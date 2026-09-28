import { useEffect, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import { getAgentRuntime, setAgentRuntime } from "@/lib/api";

// Agent-runtime switch (openclaw / hermes / picoclaw / codex / claudecode / opencode / remote).
// Unlike the rest of EditConfig this is NOT part of the form's "Save Changes"
// flow: switching is a heavyweight action that toggles systemd units and
// restarts os-server, so it has its own Switch button hitting
// POST /api/device/agent-runtime directly. The POST only means "accepted" —
// after it, onSwitch polls GET /api/device/agent-runtime until the device
// reports the target runtime (real confirmation) or times out.
//
// "remote" is Hermes-over-LAN — the device reuses its Hermes runtime pointing
// at a Hermes server on another machine (usually the user's Mac). It goes
// through the same accepted → restart → poll flow as the other runtimes, plus
// two required fields (URL + optional token) captured before POST.
//
// Options come from the API (single source = domain.AgentRuntimes); the fallback
// list mirrors it only if the fetch fails.
const FALLBACK = ["openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode", "remote"];

const REMOTE = "remote";

const RUNTIME_BLURB: Record<string, string> = {
  openclaw: "OpenClaw — persistent WebSocket gateway (default).",
  hermes: "Hermes — local HTTP+SSE agent server (Nous Research).",
  picoclaw: "PicoClaw — lightweight Go agent gateway (WebSocket).",
  codex: "Codex — OpenAI Codex CLI behind the os-server bridge (WebSocket).",
  claudecode: "Claude Code — Anthropic CLI agent behind a local bridge.",
  opencode: "OpenCode — open-source coding agent behind the os-server bridge (WebSocket).",
  remote: "Remote (Hermes-over-LAN) — the robot reuses its Hermes runtime pointing at a Hermes server on another machine (typically your Mac). Requires the target Hermes to bind on 0.0.0.0, not just 127.0.0.1, so the robot can reach it.",
};

// Display labels for the runtime dropdown / status pill. Values on the wire
// stay lowercase (systemctl unit names / domain.AgentRuntime* constants); only
// the human-facing string is title-cased. Unknown runtimes (any future addition
// the API returns before this table is updated) fall back to capitalising the
// first letter so the UI never shows raw lowercase.
const RUNTIME_LABEL: Record<string, string> = {
  openclaw: "OpenClaw",
  hermes: "Hermes",
  picoclaw: "PicoClaw",
  codex: "Codex",
  claudecode: "Claude Code",
  opencode: "OpenCode",
  remote: "Remote (external)",
};
const displayRuntime = (v: string): string =>
  RUNTIME_LABEL[v] ?? (v ? v[0].toUpperCase() + v.slice(1) : v);

const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};
const labelStyle = { display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 };

export function AgentRuntimeSection({ active }: { active: boolean }) {
  const [current, setCurrent] = useState<string>("");
  const [options, setOptions] = useState<string[]>(FALLBACK);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  // Selected is not the same as answering. config.agent_runtime flips the moment
  // the switch lands, but the gateway behind it is still booting — a backend
  // reported "Active" while it cannot take a turn sends the operator to the chat
  // to meet silence, and they read that as a broken device rather than a slow
  // start. So the label follows the gateway's own readiness probe.
  const [ready, setReady] = useState(true);
  // Remote-runtime config. Persisted server-side under
  // config.agent_remote_url/token; prefilled on load from the same GET so
  // re-opening the page shows what was last saved rather than empty fields.
  const [remoteURL, setRemoteURL] = useState<string>("");
  const [remoteToken, setRemoteToken] = useState<string>("");
  // In-page help modal so operators don't have to leave the settings page to
  // find the Mac-side setup instructions. Content is inlined below and mirrors
  // docs/agentic/remote-hermes.md — the full guide is one link away.
  const [showRemoteHelp, setShowRemoteHelp] = useState(false);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = () => {
      getAgentRuntime()
        .then((r) => {
          if (!alive) return;
          setCurrent(r.current);
          setSelected((sel) => sel || r.current);
          if (r.options?.length) setOptions(r.options);
          setReady(r.ready);
          // Only seed from the server when the user has not typed anything
          // yet — otherwise a poll during a keystroke would clobber the input.
          setRemoteURL((v) => v || r.remote_url || "");
          setRemoteToken((v) => v || r.remote_token || "");
          // Keep asking only while it is still coming up. A backend that is
          // answering does not go back to booting on its own, so there is
          // nothing to watch for after that.
          if (!r.ready) timer = setTimeout(poll, 3000);
        })
        .catch(() => { if (alive) timer = setTimeout(poll, 3000); })
        .finally(() => { if (alive) setLoading(false); });
    };
    poll();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, []);

  async function onSwitch() {
    if (switching) return;
    // "remote" carries two required fields (URL + optional token) — validate
    // them before the shared confirm/restart/poll flow. Blank URL is the most
    // common paste mistake; a wrong scheme means the backend would 400 anyway.
    let remoteOpts: { url: string; token: string } | undefined;
    if (selected === REMOTE) {
      const url = remoteURL.trim();
      if (!url) {
        toast.error("Gateway URL is required.");
        return;
      }
      if (!/^https?:\/\//i.test(url)) {
        toast.error("Gateway URL must start with http:// or https://.");
        return;
      }
      remoteOpts = { url, token: remoteToken.trim() };
    }
    if (selected === current && selected !== REMOTE) return;
    if (!window.confirm(
      selected === REMOTE
        ? `Switch to Remote Hermes at ${remoteOpts?.url}?\n\n` +
          `This restarts os-server — the robot will be briefly unavailable while it reconnects to the external Hermes.`
        : `Switch agentic backend to "${displayRuntime(selected)}"?\n\n` +
          `This stops the other backend and restarts os-server — the robot will ` +
          `be briefly unavailable while it reconnects.`,
    )) return;

    setSwitching(true);
    const target = selected;
    try {
      // POST returns 200 = "accepted" immediately; the switch itself runs in the
      // background (may run install.sh on a first switch — minutes, not seconds).
      await setAgentRuntime(target, remoteOpts);
    } catch (err) {
      // A different tab/client may already be switching. This is a definitive
      // rejection, unlike a dropped connection during the expected os-server restart.
      if ((err as Error & { status?: number }).status === 409) {
        toast.error("Another runtime switch is already in progress. Wait for it to finish before trying again.");
        setSwitching(false);
        return;
      }
      // os-server may restart before the response lands; a dropped connection
      // here usually means the switch WAS accepted — the poll below finds out.
    }
    toast.message(`Switching to ${displayRuntime(target)} — waiting for the robot to confirm…`);

    // config.agent_runtime is only persisted AFTER switch-runtime lands (a failed
    // switch rolls back and keeps the old value), so GET /device/agent-runtime is
    // the source of truth. Poll it until it reports the target: GET errors are the
    // os-server restart window, the old runtime means install still running (or
    // rolled back — indistinguishable until timeout). First-time installs download
    // from the CDN, hence the generous deadline.
    const deadline = Date.now() + 5 * 60_000;
    let landed = false;
    let lastSeen = "";
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const r = await getAgentRuntime();
        lastSeen = r.current;
        setReady(r.ready);
        // Both conditions: the name lands first, the gateway answers later.
        // Declaring victory on the name alone is what made "Active" a lie.
        if (r.current === target && r.ready) { landed = true; break; }
      } catch { /* os-server restarting — keep polling */ }
    }

    if (landed) {
      setCurrent(target);
      toast.success(`Switched to ${displayRuntime(target)} — backend is active.`);
    } else {
      // Timed out: reflect whatever the device actually reports instead of the
      // optimistic value, so a rollback is visible without a page reload.
      if (lastSeen) { setCurrent(lastSeen); setSelected(lastSeen); }
      toast.error(
        lastSeen && lastSeen !== target
          ? `Switch to ${displayRuntime(target)} not confirmed — robot still reports "${displayRuntime(lastSeen)}" (likely failed and rolled back; check journalctl -u os-runtime-switch).`
          : `Switch to ${displayRuntime(target)} not confirmed within 5 minutes — reload this page to re-check.`,
      );
    }
    setSwitching(false);
  }

  return (
    <SectionCard id="runtime" title="Agent Runtime" active={active}>
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          <div style={{ fontSize: 11.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
            The swappable agentic backend that runs the robot's brain. Switching
            stops the other backend and restarts os-server.
          </div>

          <div style={{ marginBottom: 6 }}>
            <label htmlFor="agent_runtime" style={labelStyle}>
              Backend (active: <span style={{ color: C.amber }}>{current ? displayRuntime(current) : "?"}</span>
              {current && !ready && <span style={{ color: C.textMuted }}> — starting…</span>})
            </label>
            <select
              id="agent_runtime"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={switching}
              style={selectStyle}
            >
              {options.map((o) => <option key={o} value={o}>{displayRuntime(o)}</option>)}
            </select>
          </div>

          {RUNTIME_BLURB[selected] && (
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 14 }}>
              {RUNTIME_BLURB[selected]}
            </div>
          )}

          {selected === REMOTE && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ marginBottom: 10 }}>
                <button
                  type="button"
                  onClick={() => setShowRemoteHelp(true)}
                  style={{
                    background: "none", border: "none", padding: 0, marginBottom: 10,
                    color: C.amber, fontSize: 11.5, cursor: "pointer",
                    textDecoration: "underline", textUnderlineOffset: 3,
                  }}
                >
                  ▸ How do I get the URL + API Key from my Mac?
                </button>
              </div>
              <div style={{ marginBottom: 10 }}>
                <label htmlFor="agent_remote_url" style={labelStyle}>
                  Hermes URL <span style={{ color: C.textMuted }}>(http:// or https://, on the Mac's LAN address, e.g. http://192.168.1.42:8642)</span>
                </label>
                <input
                  id="agent_remote_url"
                  type="text"
                  value={remoteURL}
                  onChange={(e) => setRemoteURL(e.target.value)}
                  placeholder="http://192.168.1.42:8642"
                  disabled={switching}
                  autoComplete="off"
                  spellCheck={false}
                  style={{ ...selectStyle, cursor: "text", fontFamily: "monospace" }}
                />
              </div>
              <div>
                <label htmlFor="agent_remote_token" style={labelStyle}>
                  API Key <span style={{ color: C.textMuted }}>(optional; sent as Bearer — leave blank if Hermes is open)</span>
                </label>
                <input
                  id="agent_remote_token"
                  type="password"
                  value={remoteToken}
                  onChange={(e) => setRemoteToken(e.target.value)}
                  placeholder="leave blank if not required"
                  disabled={switching}
                  autoComplete="off"
                  spellCheck={false}
                  style={{ ...selectStyle, cursor: "text", fontFamily: "monospace" }}
                />
              </div>
            </div>
          )}

          {(() => {
            // Every runtime including "remote" goes through the Switch/Active/Starting
            // shape. One exception: on "remote" the button stays enabled even
            // when active, so the operator can update the URL/token in place —
            // clicking it re-POSTs and restarts os-server exactly like a switch.
            const isRemote = selected === REMOTE;
            const disabled = switching || (selected === current && !isRemote);
            let label: string;
            if (switching) label = "Switching…";
            else if (isRemote && selected === current) label = "Update Remote Config";
            else if (selected === current) label = ready ? "Active" : "Starting…";
            else label = `Switch to ${displayRuntime(selected)}`;
            return (
              <button
                type="button"
                onClick={onSwitch}
                disabled={disabled}
                style={{
                  padding: "7px 18px", borderRadius: 7, fontSize: 12, fontWeight: 600,
                  border: "none",
                  cursor: disabled ? "not-allowed" : "pointer",
                  background: disabled ? C.surface : C.amber,
                  color: disabled ? C.textMuted : "#0C0B09",
                  opacity: disabled ? 0.6 : 1,
                  transition: "all 0.15s",
                }}
              >
                {label}
              </button>
            );
          })()}
        </>
      )}
      {showRemoteHelp && <RemoteHelpModal onClose={() => setShowRemoteHelp(false)} />}
    </SectionCard>
  );
}

// RemoteHelpModal — one-page walkthrough for getting a Hermes URL + API key
// off the operator's Mac. Content is a tightened subset of
// docs/agentic/remote-hermes.md so an operator who just wants to switch a
// device to a Mac they already have running Hermes doesn't have to leave the
// settings page for the setup. The full doc is linked at the bottom for the
// long tail (firewall, troubleshoot, install layout overrides).
function RemoteHelpModal({ onClose }: { onClose: () => void }) {
  const setupCmd =
    'curl -fsSL https://cdn.autonomous.ai/os/tools/setup-remote-hermes.sh | bash';
  const [copied, setCopied] = useState(false);
  // navigator.clipboard requires a secure context (https or localhost); the
  // device serves this page over plain http on the LAN, so the modern API
  // is blocked. Fall back to a hidden textarea + document.execCommand("copy"),
  // which works in that context — and flag `copied` so the label confirms.
  const copyCmd = () => {
    let ok = false;
    try {
      const ta = document.createElement("textarea");
      ta.value = setupCmd;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch {
      ok = false;
    }
    if (!ok) {
      // Modern API as a last resort — it will succeed if the browser exposes
      // it over http, which some Firefox / Safari builds actually do.
      navigator.clipboard?.writeText(setupCmd).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }).catch(() => undefined);
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10,
          maxWidth: 640, width: "100%", maxHeight: "85vh", overflow: "auto",
          padding: "22px 26px", color: C.text, fontSize: 13, lineHeight: 1.6,
          position: "relative",
        }}
      >
        <button
          onClick={onClose}
          aria-label="Close"
          style={{
            position: "absolute", top: 10, right: 12,
            background: "none", border: "none", color: C.textMuted,
            fontSize: 22, lineHeight: 1, cursor: "pointer", padding: 4,
          }}
        >
          ×
        </button>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6, color: C.amber }}>
          Get the Hermes URL + API Key
        </div>
        <div style={{ fontSize: 11.5, color: C.textMuted, marginBottom: 16 }}>
          Runs on your Mac. Enables Hermes's built-in API server on the LAN so the robot can reach it, and prints the two fields to paste back here.
        </div>

        <div style={{ marginBottom: 6, fontSize: 11.5, color: C.textDim }}>
          1. Open <b>Terminal</b> on your Mac (same Wi-Fi as this robot).
        </div>
        <div style={{ marginBottom: 6, fontSize: 11.5, color: C.textDim }}>
          2. Paste this command and press Enter:
        </div>
        <pre
          style={{
            background: "#0C0B09", border: `1px solid ${C.border}`, borderRadius: 6,
            padding: "10px 12px", fontSize: 11, fontFamily: "monospace",
            whiteSpace: "pre-wrap", wordBreak: "break-all", margin: "0 0 10px",
            color: C.text,
          }}
        >
          {setupCmd}
        </pre>
        <button
          onClick={copyCmd}
          style={{
            background: copied ? C.amber : C.surface,
            border: `1px solid ${copied ? C.amber : C.border}`,
            borderRadius: 6,
            padding: "4px 10px", fontSize: 11,
            color: copied ? "#0C0B09" : C.textDim,
            cursor: "pointer", marginBottom: 16,
            transition: "all 0.15s",
          }}
        >
          {copied ? "✓ Copied" : "Copy command"}
        </button>

        <div style={{ marginBottom: 6, fontSize: 11.5, color: C.textDim }}>
          3. The script installs a small dependency, starts the Hermes API server on <code style={{ fontSize: 10.5 }}>0.0.0.0:8642</code>, and prints something like:
        </div>
        <pre
          style={{
            background: "#0C0B09", border: `1px solid ${C.border}`, borderRadius: 6,
            padding: "10px 12px", fontSize: 10.5, fontFamily: "monospace",
            whiteSpace: "pre-wrap", margin: "0 0 10px", color: C.textDim,
          }}
        >
{`Hermes URL : http://192.168.1.42:8642
API Key    : intern2-hermes-a1b2c3d4e5f6a7b8`}
        </pre>
        <div style={{ marginBottom: 16, fontSize: 11.5, color: C.textDim }}>
          4. Copy those two values into the <b>Hermes URL</b> + <b>API Key</b> fields on this page, then click <b>Switch to Remote (external)</b>.
        </div>

        <div style={{ fontSize: 11, color: C.textMuted, borderTop: `1px solid ${C.border}`, paddingTop: 12, marginTop: 8 }}>
          Auto-detect fails? Firewall blocks the port? Chat works then stops on sleep? The full guide covers all of it — including how to override the Hermes install path if the script can't find it.
          <br />
          <span style={{ color: C.textDim }}>
            <a
              href="https://github.com/anthropics/autonomous/blob/main/docs/agentic/remote-hermes.md"
              target="_blank"
              rel="noreferrer"
              style={{ color: C.amber }}
            >
              docs/agentic/remote-hermes.md
            </a>
          </span>
        </div>
      </div>
    </div>
  );
}
