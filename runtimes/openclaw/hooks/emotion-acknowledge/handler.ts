import http from "http";

const handler = async (event: any): Promise<void> => {
  if (event.type !== "message" || event.action !== "preprocessed") return;

  const ctx = event.context;
  const text: string = ctx?.bodyForAgent ?? ctx?.body ?? "";

  // Skip passive sensing: a NO_REPLY would leave the lamp stuck on "thinking".
  if (!text.trim()) return;
  if (
    text.startsWith("[sensing:") ||
    text.startsWith("[activity]") ||
    text.startsWith("[emotion]") ||
    text.startsWith("[speech_emotion]")
  ) {
    return;
  }

  // voice_agent_handled replays are silent; "thinking" would stick after the turn ended.
  if (text.includes("[HANDLED]")) return;

  const req = http.request({
    hostname: "127.0.0.1",
    port: 5001,
    path: "/emotion",
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  req.on("error", () => {});
  req.write(JSON.stringify({ emotion: "thinking", intensity: 0.7 }));
  req.end();
};

export default handler;
