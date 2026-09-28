// One-way postMessage bridge from the device Setup page to the window that opened it.

export const BRIDGE_SOURCE = "autonomous-device-setup" as const;
export const BRIDGE_VERSION = 1 as const;

// Keep names stable: the parent switches on them.
export type SetupBridgeEvent =
  | "setup_opened"
  | "step_changed"
  | "wifi_selected"
  | "setup_submitted"
  | "setup_error"
  | "setup_connecting"
  | "setup_join_progress"
  | "setup_connected"
  | "setup_failed"
  | "retry_clicked"
  | "start_over_clicked"
  | "continue_clicked"
  | "monitor_clicked"
  | "setup_done";

// Resolves the parent origin once: ?parent_origin, then the referrer origin, then "*" (events carry no secrets).
function resolveParentOrigin(): string {
  if (typeof window === "undefined") return "*";
  try {
    const fromParam = new URLSearchParams(window.location.search).get("parent_origin");
    if (fromParam) return new URL(fromParam).origin;
  } catch {
    /* malformed parent_origin — fall through */
  }
  try {
    if (document.referrer) return new URL(document.referrer).origin;
  } catch {
    /* no/blocked referrer — fall through */
  }
  return "*";
}

const PARENT_ORIGIN = resolveParentOrigin();

// A popup posts to window.opener, an iframe to window.parent.
function targets(): Window[] {
  if (typeof window === "undefined") return [];
  const out: Window[] = [];
  if (window.opener && window.opener !== window) out.push(window.opener as Window);
  if (window.parent && window.parent !== window) out.push(window.parent);
  return out;
}

// Posts one event to the parent/opener; never throws.
export function emit(event: SetupBridgeEvent, data: Record<string, unknown> = {}): void {
  const payload = {
    source: BRIDGE_SOURCE,
    v: BRIDGE_VERSION,
    event,
    ts: Date.now(),
    ...data,
  };
  const dests = targets();
  console.log(`[setupBridge] ${event} → ${dests.length} target(s) @ ${PARENT_ORIGIN}`, payload);
  for (const target of dests) {
    try {
      target.postMessage(payload, PARENT_ORIGIN);
    } catch {
      /* cross-origin / closed window — ignore, this channel is best-effort */
    }
  }
}

export const setupBridge = {
  opened: (info: { mode: string; deviceId?: string; mac?: string }) =>
    emit("setup_opened", info),
  stepChanged: (step: string) => emit("step_changed", { step }),
  wifiSelected: (ssid: string) => emit("wifi_selected", { ssid }),
  submitted: (info: { ssid: string; channel: string }) =>
    emit("setup_submitted", info),
  error: (message: string) => emit("setup_error", { message }),
  connecting: () => emit("setup_connecting", {}),
  joinProgress: (elapsedSec: number) =>
    emit("setup_join_progress", { elapsed_sec: elapsedSec }),
  connected: (info: { mdns_host?: string; lan_ip?: string }) =>
    emit("setup_connected", info),
  failed: (message: string) => emit("setup_failed", { message }),
  retryClicked: () => emit("retry_clicked", {}),
  startOverClicked: () => emit("start_over_clicked", {}),
  continueClicked: (info: { mdns_host?: string }) =>
    emit("continue_clicked", info),
  monitorClicked: () => emit("monitor_clicked", {}),
  setupDone: () => emit("setup_done", {}),
};
