import type { MonitorEvent } from "../types";

type PendingMessage = { role: string; pending?: boolean; runId?: string };

// Track persisted bubbles, not the currently selected conversation or a ref filled after an asynchronous POST.
export function pendingReplyKey(conversations: { messages: PendingMessage[] }[]): string {
  return JSON.stringify([...new Set(conversations.flatMap((conversation) =>
    conversation.messages.flatMap((message) =>
      message.role === "agent" && message.pending && message.runId ? [message.runId] : [],
    ),
  ))].sort());
}

// Only terminal reply nodes prove there is a complete answer.
export function replayedReply(events: MonitorEvent[], runId: string): string | undefined {
  for (const event of [...events].reverse()) {
    if (event.type !== "flow_event") continue;
    const detail = event.detail as { node?: string; run_id?: string; runId?: string; data?: Record<string, unknown> } | undefined;
    const data = detail?.data;
    if ((event.runId ?? detail?.run_id ?? detail?.runId ?? data?.run_id) !== runId) continue;
    if (detail?.node === "no_reply") return "…";
    if (!["tts_send", "tts_suppressed", "harness_response"].includes(detail?.node ?? "")) continue;
    const text = data?.full_text ?? data?.text;
    if (typeof text === "string" && text) return text;
  }
  return undefined;
}
