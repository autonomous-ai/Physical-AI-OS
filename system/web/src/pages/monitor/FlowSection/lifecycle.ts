import type { DisplayEvent } from "../types";

// Live monitor events and persisted JSONL events encode the same terminal differently.
export function lifecycleTerminal(event: DisplayEvent): { status: "done" | "error"; error?: string } | undefined {
  const node = event.detail?.node;
  const live = event.type === "lifecycle";
  if (!(live && (event.phase === "end" || event.phase === "error")) &&
      !(event.type === "flow_event" && (node === "lifecycle_end" || node === "lifecycle_error"))) return;
  const data = event.detail?.data as unknown as { error?: unknown } | undefined;
  const error = event.error || (typeof data?.error === "string" ? data.error : undefined);
  return {
    status: (live ? event.phase === "error" : node === "lifecycle_error") || error ? "error" : "done",
    error,
  };
}
