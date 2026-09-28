import { useEffect, useRef } from "react";

// Polls `fetcher` on an interval with three properties the naive
// `useEffect(setInterval(fetch))` pattern lacked — and which caused Chrome
// to run out of HTTP/1.1 connection slots on the monitor page:
//
//   1. In-flight guard. If the previous fetch hasn't returned yet, the next
//      tick is skipped. Without this, slow network made concurrent fetches
//      pile up in Chrome's queue (6-per-origin cap) until every slot — and
//      the MJPEG/SSE streams on the same page — starved.
//   2. Hard timeout via AbortController. A stalled fetch aborts after
//      `timeoutMs` instead of hanging forever in "pending".
//   3. Visibility pause. When the tab is backgrounded the timer stops, so a
//      hidden tab doesn't keep hammering the Pi.
// An optional refreshKey restarts polling immediately when a section changes;
// cleanup aborts the previous request before the new section starts.
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
