import { useCallback, useEffect, useRef, useState } from "react";
import { HW } from "../types";
import type { FaceOwnersDetail } from "../types";
import { usePolling } from "../../../hooks/usePolling";
import type { CooldownState } from "./types";

// Enrolled-owners list + face detection state (cooldowns, current user), with background polling and a user-triggered refresh.
export function useFaceData() {
  const [data, setData] = useState<FaceOwnersDetail | null>(null);
  const [error, setError] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const [cooldowns, setCooldowns] = useState<CooldownState | null>(null);
  const [cdError, setCdError] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [manualRefreshing, setManualRefreshing] = useState(false);

  const [currentUser, setCurrentUser] = useState<string>("");

  const refresh = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const r = await fetch(`${HW}/face/owners`, { signal: ctrl.signal }).then((x) => x.json());
      if (ctrl.signal.aborted) return;
      setData({ enrolled_count: r.enrolled_count ?? 0, persons: r.persons ?? [] });
      setError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError(true);
    }
  }, []);

  const handleManualRefresh = useCallback(async () => {
    setManualRefreshing(true);
    const started = performance.now();
    try {
      await refresh();
    } finally {
      const elapsed = performance.now() - started;
      const wait = Math.max(0, 600 - elapsed);
      window.setTimeout(() => setManualRefreshing(false), wait);
    }
  }, [refresh]);

  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);

  usePolling(async (signal) => {
    // refresh() uses its own AbortController.
    void signal;
    await refresh();
  }, 10_000, { timeoutMs: 8000 });

  const refreshFaceState = useCallback(async (signal?: AbortSignal) => {
    const [cdRes, cuRes] = await Promise.allSettled([
      fetch(`${HW}/face/cooldowns`, { signal }),
      fetch(`${HW}/face/current-user`, { signal }),
    ]);
    if (cdRes.status === "fulfilled" && cdRes.value.ok) {
      setCooldowns(await cdRes.value.json());
      setCdError(false);
    } else {
      setCdError(true);
    }
    if (cuRes.status === "fulfilled" && cuRes.value.ok) {
      const j = await cuRes.value.json();
      setCurrentUser(typeof j?.current_user === "string" ? j.current_user : "");
    }
  }, []);

  usePolling(async (signal) => { await refreshFaceState(signal); }, 5000);

  const handleResetCooldowns = async () => {
    setResetting(true);
    try {
      await fetch(`${HW}/face/cooldowns/reset`, { method: "POST" });
      await refreshFaceState();
    } catch {
      // ignore
    } finally {
      setResetting(false);
    }
  };

  return {
    data, error, currentUser,
    cooldowns, cdError, resetting, manualRefreshing,
    refresh, handleManualRefresh, handleResetCooldowns,
  };
}
