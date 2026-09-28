/** Fetch transport errors are ambiguous during a Wi-Fi handoff; server errors are not. */
export function isWifiHandoffError(error: unknown, wifiMayReconnect: boolean): boolean {
  return wifiMayReconnect && error instanceof TypeError && !("status" in error);
}

export type WifiConfirmation = "connected" | "unconfirmed" | "cancelled";

/** Only observe the live association. Never retry the configuration write. */
export async function confirmWifiConnection(
  ssid: string,
  readNetwork: (signal: AbortSignal) => Promise<{ ssid: string } | null>,
  signal: AbortSignal,
  timeoutMs = 120_000,
  intervalMs = 3_000,
): Promise<WifiConfirmation> {
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), timeoutMs);
  const bounded = AbortSignal.any([signal, deadline.signal]);
  const pause = () => new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(delay);
      bounded.removeEventListener("abort", finish);
      resolve();
    };
    const delay = setTimeout(finish, intervalMs);
    bounded.addEventListener("abort", finish, { once: true });
    if (bounded.aborted) finish();
  });
  try {
    // Allow the server's delayed handoff to begin before checking the old link.
    while (!bounded.aborted) {
      await pause();
      if (bounded.aborted) break;
      const request = new AbortController();
      const requestTimer = setTimeout(() => request.abort(), 4_000);
      try {
        const network = await readNetwork(AbortSignal.any([bounded, request.signal]));
        if (!bounded.aborted && network?.ssid === ssid) return "connected";
      } catch {
        // The device may disappear while switching AP/STA or changing address.
      } finally {
        clearTimeout(requestTimer);
      }
    }
    return signal.aborted ? "cancelled" : "unconfirmed";
  } finally {
    clearTimeout(timer);
  }
}
