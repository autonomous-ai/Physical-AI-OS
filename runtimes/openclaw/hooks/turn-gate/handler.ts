import http from "http";

const handler = async (event: any): Promise<void> => {
  if (event.type !== "message" || event.action !== "preprocessed") return;

  const ctx = event.context;
  const text: string = ctx?.bodyForAgent ?? ctx?.body ?? "";

  // Skip sensing events — Lamp sets busy proactively in sendChat for those
  if (text.startsWith("[sensing:") || !text.trim()) return;

  // Skip heartbeat turns (body ends with HEARTBEAT_OK): they emit no lifecycle.end, so busy would wedge for
  // the 5-min busyTTL. The hook context exposes no heartbeat field, hence the string match.
  if (text.includes("HEARTBEAT_OK")) return;

  const req = http.request({
    hostname: "127.0.0.1",
    port: 5000,
    path: "/api/openclaw/busy",
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  req.on("error", () => {});
  req.end();
};

export default handler;