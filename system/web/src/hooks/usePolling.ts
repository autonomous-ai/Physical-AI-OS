import { useEffect, useRef } from "react";

// Polls `fetcher` with an in-flight guard, hard timeout and visibility pause.
export function usePolling(
  fetcher: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  opts: { timeoutMs?: number; enabled?: boolean; refreshKey?: string } = {},
) {
  const { timeoutMs = 4000, enabled = true, refreshKey } = opts;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return;

    let inFlight = false;
    let activeController: AbortController | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;

    const runOnce = async () => {
      if (inFlight) return;
      inFlight = true;
      const ac = new AbortController();
      activeController = ac;
      const t = setTimeout(() => ac.abort(), timeoutMs);
      try {
        await fetcherRef.current(ac.signal);
      } catch {
        // Callers handle their own errors; swallow abort + network here.
      } finally {
        clearTimeout(t);
        activeController = null;
        inFlight = false;
      }
    };

    const start = () => {
      if (timer !== null) return;
      runOnce();
      timer = setInterval(runOnce, intervalMs);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
      activeController?.abort();
    };
  }, [intervalMs, timeoutMs, enabled, refreshKey]);
}
