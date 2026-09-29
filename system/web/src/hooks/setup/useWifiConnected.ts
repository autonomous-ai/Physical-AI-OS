import { useEffect, useRef, useState } from "react";
import { checkInternet, getCurrentNetwork } from "@/lib/api";

// Whether the device is on home Wi-Fi (or wired), from live device state.
export function useWifiConnected(): { wifiConnected: boolean; wiredUplink: boolean; currentSsid: string; checking: boolean } {
  const [wifiConnected, setWifiConnected] = useState(false);
  const [wiredUplink, setWiredUplink] = useState(false);
  const [currentSsid, setCurrentSsid] = useState("");
  const [checking, setChecking] = useState(true);
  const doneRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let attempt = 0;
    let timer: number | undefined;

    const check = async () => {
      attempt += 1;
      try {
        // A null SSID means wired only when the probe succeeded; keep failures distinct.
        const [online, current] = await Promise.all([
          checkInternet().catch(() => false),
          getCurrentNetwork()
            .then((n) => ({ ok: true, ssid: n?.ssid ?? "" }))
            .catch(() => ({ ok: false, ssid: "" })),
        ]);
        if (cancelled) return;
        if (online && current.ok && current.ssid) {
          doneRef.current = true;
          setCurrentSsid(current.ssid);
          setWifiConnected(true);
          setChecking(false);
          return;
        }
        if (online && current.ok) {
          doneRef.current = true;
          setWiredUplink(true);
          setChecking(false);
          return;
        }
      } catch {
        /* transient — retry below */
      }
      if (cancelled || doneRef.current) return;
      setChecking(false);
      if (attempt < 5) timer = window.setTimeout(check, 1500);
    };

    check();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, []);

  return { wifiConnected, wiredUplink, currentSsid, checking };
}
