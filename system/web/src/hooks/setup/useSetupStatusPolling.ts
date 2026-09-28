import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getSetupStatus } from "@/lib/api";
import { getInitialSearch } from "./useSetupUrlParams";

export type SetupPhase = "connecting" | "connected" | "failed";

// The AP-mode static address (backend system/device apSetupIP). Used to
// reject the AP's own IP when seeding lan_ip on a `.local` landing — we must
// never canonical-"upgrade" onto the soon-to-die AP address.
const AP_SETUP_IP = "192.168.100.1";

// How long the phase poll may go unanswered (while setupWorking) before we
// declare the AP dead. Normal cadence is 600ms, so 5s of silence means the
// AP tore down — not a slow response.
const AP_LOST_AFTER_MS = 5000;

// Seconds after submit before an unresolved join is declared failed client-side.
//
// Why this exists: the backend's own verdict is unreachable in the common
// wrong-password case. SetupNetwork polls for a full 60s before returning an
// error, but the AP tears down ~2s after submit — so from t=2s the phase poll
// can no longer reach 192.168.100.1, and the operator's machine has usually
// auto-rejoined its home Wi-Fi by then. The backend flips to phase="failed" at
// t≈62s with nobody listening, leaving the UI spinning on "joining Wi-Fi…"
// forever. This timeout is what turns that hang into a visible error.
//
// 80s sits deliberately past the backend's 60s network timeout plus the ~5-8s
// device-ap-mode needs to bring hostapd back up, so a device that IS about to
// come back has already done so — we never call failure on a join that was
// merely slow. Only applies when the AP has been unreachable (apLost) and no
// lan_ip was ever captured; a join with a known IP is handled by the LAN-IP
// probe, which succeeds the moment the operator rejoins their network.
const JOIN_TIMEOUT_SEC = 80;

// Pollers driving the post-submit "Setting up…" UI. Redirect is IP-FIRST —
// the raw LAN IP resolves on every network, so it's the primary target:
//   (1) phase poll — runs while setupWorking, hits the AP IP for phase/lan_ip
//       (the backend captures the STA IP early so the FE can read it during the
//       brief window the AP is still alive — see system/device/service.go).
//   (2) LAN-IP probe — once lan_ip is known, probe http://<lan_ip>/api/health;
//       when it succeeds (operator rejoined home Wi-Fi, device is up) redirect
//       to http://<lan_ip>/setup. Also runs as the pre-submit canonical-URL
//       upgrade so the URL bar moves off the soon-to-die AP IP onto the IP that
//       survives the AP→STA switch. Requires the device CSP to allow plain
//       `http:` in connect-src (the IP is cross-origin from the AP page).
//   (3) mDNS `.local` fallback — DISCOVERY ONLY, fires only when the IP
//       channel is dead: on a first-time setup the AP often tears down before
//       wlan0 even has a DHCP lease, so the phase poll never reads lan_ip and
//       (2) has no target. Once the poll has been silent >5s (AP gone) we
//       probe http://<mdnsHost>.local/api/health and redirect there. `.local`
//       is NOT the durable home — many routers block mDNS multicast (and
//       Android Chrome has no native mDNS) — it's a best-effort bootstrap;
//       (4) immediately moves the page back onto the raw IP.
//   (4) `.local` landing seed — when the page is served from a `.local` host,
//       fetch lan_ip once from the open setup-status endpoint so probe (2)
//       canonical-upgrades the URL to the raw IP.
export function useSetupStatusPolling({
  setupWorking,
  setupPhase,
  setupLanIP,
  elapsed,
  mdnsHost,
  setSetupPhase,
  setSetupLanIP,
  setSetupErrorMsg,
}: {
  setupWorking: boolean;
  // Current phase, so the join-timeout below can stand down once the join has
  // already resolved either way.
  setupPhase: SetupPhase;
  setupLanIP: string;
  // Seconds since the join started (ticked by the controller). Drives the
  // JOIN_TIMEOUT_SEC failure verdict.
  elapsed: number;
  // Device hostname without the ".local" suffix (e.g. "lamp-a1b2"), derived
  // from the hardware MAC by Setup.tsx. Empty when the config hasn't loaded —
  // the mDNS fallback simply stays off in that case.
  mdnsHost: string;
  setSetupPhase: Dispatch<SetStateAction<SetupPhase>>;
  setSetupLanIP: Dispatch<SetStateAction<string>>;
  setSetupErrorMsg: Dispatch<SetStateAction<string>>;
}) {
  // Cross-origin redirect URL must carry every original param (incl.
  // llm_api_key) so the new host can rehydrate state + re-auth. Read via
  // the module-load snapshot — window.location.search at redirect time has
  // already been scrubbed by App.useScrubSecrets().
  const carrySearch = getInitialSearch();
  // Flips true when the phase poll has gone unanswered >5s while
  // setupWorking — the AP died before lan_ip could be read, so the IP-only
  // redirect can never fire and the mDNS fallback below takes over.
  const [apLost, setApLost] = useState(false);
  const lastPollOkRef = useRef(0);
  // Phase poll: runs against the AP IP, so it works while the user is still
  // on the AP SSID. Once the AP shuts down the polls will fail and we keep
  // the last value.
  useEffect(() => {
    if (!setupWorking) return;
    let cancelled = false;
    lastPollOkRef.current = performance.now();
    // Guards against reading the PREVIOUS attempt's verdict as this one's.
    // handler.Setup answers 200 immediately but defers device.Setup by 2s, and
    // nothing resets setupState to idle in between (see system/device/setup.go)
    // — so for ~2s after a resubmit the backend still reports the old
    // phase="failed". Without this, the very first poll would bounce a retrying
    // operator straight back to the failure screen.
    //
    // Two signals promote the backend's state to "this is our run":
    //   - the run counter moved past what we saw before the run began. This is
    //     the reliable one. `baselineRun` is read on the first tick, which
    //     happens ~2s before device.Setup bumps it, so any higher value is ours.
    //   - phase === "connecting", kept as the fallback for a device still on an
    //     os-server build that doesn't publish `run`.
    // The counter matters most on the wired path: its network step is a single
    // ping, so "connecting" can begin and end between two 600ms polls. Latching
    // only on that phase meant the wired "connected" verdict was thrown away as
    // stale, the parent window never got setup_connected, and the join sat on
    // the progress screen until the 80s timeout called it failed.
    let runStarted = false;
    let baselineRun: number | null = null;
    const tick = async () => {
      try {
        const s = await getSetupStatus();
        if (cancelled) return;
        lastPollOkRef.current = performance.now();
        setApLost(false);
        if (typeof s.run === "number") {
          if (baselineRun === null) baselineRun = s.run;
          else if (s.run > baselineRun) runStarted = true;
        }
        if (s.phase === "connecting") {
          runStarted = true;
          if (s.lan_ip) setSetupLanIP(s.lan_ip);
          return;
        }
        if (!runStarted) return; // stale verdict from a prior attempt
        if (s.phase === "connected") {
          setSetupPhase("connected");
          if (s.lan_ip) setSetupLanIP(s.lan_ip);
        } else if (s.phase === "failed") {
          setSetupPhase("failed");
          setSetupErrorMsg(s.error || "Wi-Fi setup failed.");
        }
      } catch {
        /* AP likely shutting down — keep last known phase */
      }
    };
    // Poll fast (600ms). The AP at 192.168.100.1 only survives ~2s after the
    // AP→STA switch begins, and the backend publishes the captured lan_ip during
    // that window — so a slow 2s cadence can miss the one-or-two polls that land
    // while the AP is still answering, which is exactly what stranded the user.
    // Fast polling maximizes the chance we read lan_ip before the AP dies; once
    // it's dead the catch above just keeps the last phase and the LAN-IP probe
    // below takes over.
    tick();
    const id = setInterval(tick, 600);
    // Staleness watchdog on wall-clock, NOT on consecutive fetch failures —
    // fetches to a vanished AP can hang for many seconds in the browser's TCP
    // retry, which would delay AP-loss detection far past the real teardown.
    const watchdog = setInterval(() => {
      if (performance.now() - lastPollOkRef.current > AP_LOST_AFTER_MS) {
        setApLost(true);
      }
    }, 1000);
    // Clear the AP-loss verdict when the session ends (retry, or an aborted
    // submit) rather than when the next one starts: `apLost` is only ever read
    // under a `setupWorking` gate, so resetting on teardown leaves every
    // reader with exactly the same value it saw before, while keeping the
    // reset out of the effect body (no synchronous setState → no cascading
    // render). A fresh mount already starts at false.
    return () => {
      cancelled = true;
      clearInterval(id);
      clearInterval(watchdog);
      setApLost(false);
    };
  }, [setupWorking, setSetupPhase, setSetupLanIP, setSetupErrorMsg]);

  // Join timeout → client-side "failed" verdict. See JOIN_TIMEOUT_SEC above for
  // why the backend's own verdict usually never arrives.
  //
  // Gates, all required:
  //   - setupWorking:        only judge an in-flight join, never the idle form.
  //   - phase === connecting: the poll may still have delivered a real verdict
  //                           during the AP-alive window; that one wins.
  //   - apLost:              the AP is confirmed unreachable. While it still
  //                          answers, the backend remains authoritative and a
  //                          slow-but-succeeding join must not be called dead.
  //   - !setupLanIP:         a captured IP means the join reached DHCP; the
  //                          LAN-IP probe redirects as soon as the operator is
  //                          back on their network, so there's nothing to fail.
  //
  // The message is deliberately hedged ("couldn't be confirmed" + likely
  // causes) rather than asserting a wrong password: we never received the
  // backend's reason, so stating one as fact would be guessing at the
  // operator's expense. The wording still leads with the most common cause.
  useEffect(() => {
    if (!setupWorking || setupPhase !== "connecting") return;
    if (!apLost || setupLanIP) return;
    if (elapsed < JOIN_TIMEOUT_SEC) return;
    console.warn(`[setup] no verdict after ${elapsed}s with AP unreachable — declaring join failed`);
    setSetupErrorMsg(
      "The robot couldn't be reached after joining Wi-Fi. This usually means " +
      "the Wi-Fi password was wrong, or the network is 5GHz-only.",
    );
    setSetupPhase("failed");
  }, [setupWorking, setupPhase, apLost, setupLanIP, elapsed, setSetupPhase, setSetupErrorMsg]);

  // IP-first auto-redirect. Once we know the device's LAN IP, probe it from the
  // browser; when the probe succeeds the user is back on home Wi-Fi and the
  // device is reachable, so navigate to http://<lan_ip>/setup?<params>.
  //
  // This is the PRIMARY redirect channel. A raw IP resolves on every LAN,
  // including the mDNS-blocked networks where the `.local` name silently
  // fails, so the IP is the preferred single target; `.local` (below) is only
  // a last-resort discovery path for when this channel never learned the IP.
  //
  // It also serves as the pre-submit canonical-URL upgrade: while the page is
  // on the AP IP (192.168.100.1) and a lan_ip is already known (e.g. re-setup
  // from a device that's still on home Wi-Fi, or after the early-capture poll
  // lands), it bounces the URL bar off the soon-to-die AP IP onto the IP that
  // survives the AP→STA switch. Before submit, with wlan0 still serving the AP
  // and no STA IP yet, lan_ip is empty and this effect simply does nothing —
  // the page stays on 192.168.100.1, exactly as intended.
  //
  // Not gated on setupWorking: it must also run pre-submit for the canonical
  // upgrade. It's safe — it can only fire once setupLanIP is non-empty, and
  // the probe only succeeds once the device is actually reachable at that IP.
  useEffect(() => {
    if (typeof window === "undefined" || !setupLanIP) return;
    // Already on the target IP — nothing to redirect to. (Avoids a same-URL
    // navigation no-op loop once we've landed.)
    if (window.location.hostname === setupLanIP) return;
    let cancelled = false;
    const base = `http://${setupLanIP}`;
    // Carry pathname + original search so the IP host lands back on /setup with
    // the OS-server-pushed params (llm_api_key, device_id, …) intact. The hash
    // rides along too — it names the active/deep-linked step (#voice / #face),
    // and dropping it lands the reload on Wi-Fi with no record of the target.
    // Read live (not snapshotted like carrySearch) so it reflects the step the
    // operator is on at redirect time.
    const target = `${base}${window.location.pathname}${carrySearch}${window.location.hash}`;
    let attempt = 0;
    let timer: number | undefined;
    const probe = async () => {
      attempt += 1;
      try {
        // The device CSP must allow plain `http:` in connect-src for this
        // cross-origin fetch to leave the browser. `mode: "no-cors"` does not
        // bypass CSP — it only suppresses the opaque-response read.
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${setupLanIP} after ${attempt} probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* not reachable yet — user still on AP SSID, or device not up */
      }
      if (cancelled) return;
      // Back-off: 800ms × 4 then 2s × ∞ — fast initial retries so the redirect
      // lands sub-second when reachable, then slow polls so we don't hammer the
      // network while the user is still on the AP.
      const next = attempt < 4 ? 800 : 2000;
      timer = window.setTimeout(probe, next);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupLanIP, carrySearch]);

  // mDNS `.local` fallback probe — rescues the first-time-setup race where the
  // AP tears down before wlan0 has a DHCP lease: the phase poll never reads
  // lan_ip, the operator's machine auto-rejoins home/office Wi-Fi, and the IP
  // probe above has no target — the page would be stranded on the dead AP IP
  // forever. `.local` is deliberately NOT a primary channel (routers that
  // block mDNS, Android Chrome), but when the IP channel is provably dead a
  // best-effort mDNS probe strictly improves the odds. Gates:
  //   - setupWorking: only post-submit; never yanks the pre-submit form away.
  //   - apLost:       only after the AP has been silent >5s. While the AP is
  //                   alive, mDNS may resolve OVER THE AP LINK (avahi answers
  //                   on all interfaces) and a premature redirect would reload
  //                   the page mid-join, losing the "Setting up…" state.
  //   - !setupLanIP:  the IP channel stays authoritative when it has a target.
  // On success we redirect to http://<host>.local/setup?<params>; the landing
  // seed below then bounces the page onto the raw IP.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!setupWorking || !apLost || setupLanIP || !mdnsHost) return;
    const host = `${mdnsHost}.local`;
    // Already on the .local host — same-origin polling works; nothing to do.
    if (window.location.hostname === host) return;
    let cancelled = false;
    const base = `http://${host}`;
    // Same as the IP probe above: carry the hash so the step survives the hop.
    const target = `${base}${window.location.pathname}${carrySearch}${window.location.hash}`;
    let timer: number | undefined;
    let attempt = 0;
    const probe = async () => {
      attempt += 1;
      try {
        // Same CSP note as the IP probe: connect-src must allow plain `http:`.
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${host} after ${attempt} mDNS probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* mDNS blocked/unresolved, or operator not back on home Wi-Fi yet */
      }
      if (cancelled) return;
      // Flat 2s cadence — this only starts after the 5s AP-loss window, so
      // there's no sub-second race to win, and mDNS lookups are not cheap.
      timer = window.setTimeout(probe, 2000);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupWorking, apLost, setupLanIP, mdnsHost, carrySearch]);

  // `.local` landing seed: after the mDNS fallback redirect the page mounts
  // fresh on http://<host>.local with empty state — nothing would populate
  // lan_ip pre-submit, so the canonical-URL upgrade above could never move the
  // page onto the raw IP. Fetch it once from the open setup-status endpoint
  // (which falls back to the live wlan0 address when no setup run is in
  // flight — see system/device/service.go SetupStatus). `.local` is only the
  // discovery bootstrap; the raw IP is the durable home, since mDNS can stop
  // resolving at any time on flaky networks. The AP_SETUP_IP guard prevents a
  // perverse "upgrade" onto the AP address if this ever runs in AP mode.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!window.location.hostname.endsWith(".local")) return;
    let cancelled = false;
    getSetupStatus().then((s) => {
      if (cancelled) return;
      if (s.lan_ip && s.lan_ip !== AP_SETUP_IP) {
        setSetupLanIP((prev) => prev || s.lan_ip);
      }
    }).catch(() => { /* status unreachable — stay on .local, page still works */ });
    return () => { cancelled = true; };
  }, [setSetupLanIP]);
}
