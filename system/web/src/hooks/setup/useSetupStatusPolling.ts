import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getSetupStatus } from "@/lib/api";
import { getInitialSearch } from "./useSetupUrlParams";

export type SetupPhase = "connecting" | "connected" | "failed";

// AP-mode static address; never redirect onto it.
const AP_SETUP_IP = "192.168.100.1";

// Phase-poll silence (while setupWorking) after which the AP is considered gone.
const AP_LOST_AFTER_MS = 5000;

// Client-side join failure timeout: the backend verdict is usually unreachable once the AP tears down.
const JOIN_TIMEOUT_SEC = 80;

// Pollers driving the post-submit "Setting up…" UI.
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
  setupPhase: SetupPhase;
  setupLanIP: string;
  elapsed: number;
  mdnsHost: string;
  setSetupPhase: Dispatch<SetStateAction<SetupPhase>>;
  setSetupLanIP: Dispatch<SetStateAction<string>>;
  setSetupErrorMsg: Dispatch<SetStateAction<string>>;
}) {
  const carrySearch = getInitialSearch();
  const [apLost, setApLost] = useState(false);
  const lastPollOkRef = useRef(0);
  useEffect(() => {
    if (!setupWorking) return;
    let cancelled = false;
    lastPollOkRef.current = performance.now();
    // Ignore the previous attempt's verdict until the run counter moves or phase is "connecting".
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
        if (!runStarted) return;
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
    // Poll fast: the AP only survives ~2s after the STA switch begins.
    tick();
    const id = setInterval(tick, 600);
    // Wall-clock watchdog: fetches to a vanished AP can hang for seconds.
    const watchdog = setInterval(() => {
      if (performance.now() - lastPollOkRef.current > AP_LOST_AFTER_MS) {
        setApLost(true);
      }
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
      clearInterval(watchdog);
      setApLost(false);
    };
  }, [setupWorking, setSetupPhase, setSetupLanIP, setSetupErrorMsg]);

  // Join timeout -> client-side "failed" verdict (see JOIN_TIMEOUT_SEC).
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

  // IP-first auto-redirect: probe the LAN IP and navigate once reachable.
  useEffect(() => {
    if (typeof window === "undefined" || !setupLanIP) return;
    if (window.location.hostname === setupLanIP) return;
    let cancelled = false;
    const base = `http://${setupLanIP}`;
    // Carry the hash so the deep-linked step survives the hop.
    const target = `${base}${window.location.pathname}${carrySearch}${window.location.hash}`;
    let attempt = 0;
    let timer: number | undefined;
    const probe = async () => {
      attempt += 1;
      try {
        // CSP connect-src must allow plain http:; no-cors does not bypass CSP.
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${setupLanIP} after ${attempt} probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* not reachable yet — user still on AP SSID, or device not up */
      }
      if (cancelled) return;
      const next = attempt < 4 ? 800 : 2000;
      timer = window.setTimeout(probe, next);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupLanIP, carrySearch]);

  // mDNS `.local` fallback when the AP died before lan_ip was read.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!setupWorking || !apLost || setupLanIP || !mdnsHost) return;
    const host = `${mdnsHost}.local`;
    if (window.location.hostname === host) return;
    let cancelled = false;
    const base = `http://${host}`;
    const target = `${base}${window.location.pathname}${carrySearch}${window.location.hash}`;
    let timer: number | undefined;
    let attempt = 0;
    const probe = async () => {
      attempt += 1;
      try {
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${host} after ${attempt} mDNS probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* mDNS blocked/unresolved, or operator not back on home Wi-Fi yet */
      }
      if (cancelled) return;
      timer = window.setTimeout(probe, 2000);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupWorking, apLost, setupLanIP, mdnsHost, carrySearch]);

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
