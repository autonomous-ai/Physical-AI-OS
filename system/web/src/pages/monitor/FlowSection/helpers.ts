import type { DisplayEvent } from "../types";
import type { ActiveFlowStage, Turn, NodeInfoMap } from "./types";
import { FLOW_NODES, CHANNEL_FALLBACK_MESSAGE, isChatType } from "./types";

// Dynamic per-event JSON over SSE; every read is an optional chain with a fallback.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type FlowEventDetail = Record<string, any>;

const CHANNEL_TYPES = new Set(["telegram", "discord", "slack", "wechat", "channel"]);
function isChannelType(type: string): boolean {
  return CHANNEL_TYPES.has(type);
}

const SENSING_PREFIX_RE = /^\s*\[(?:sensing:([^\]]+)|(activity|emotion|speech_emotion|posture))\]/i;

// Returns the internal sensing type ("motion.activity", "emotion.detected", "speech_emotion.detected", "presence.enter", …) from a message prefix, or null if the message doesn't start with one.
export function extractSensingType(msg: string): string | null {
  const m = msg.match(SENSING_PREFIX_RE);
  if (!m) return null;
  if (m[1]) return m[1];
  if (m[2] === "activity") return "motion.activity";
  if (m[2] === "emotion") return "emotion.detected";
  if (m[2] === "speech_emotion") return "speech_emotion.detected";
  if (m[2] === "posture") return "pose.ergo_risk";
  return null;
}

// True if the message starts with a sensing prefix (any taxonomy, anchored).
export function hasSensingPrefix(msg: string): boolean {
  return SENSING_PREFIX_RE.test(msg);
}

const SENSING_PREFIX_ANYWHERE_RE = /\[(?:sensing:[^\]]+|activity|emotion|speech_emotion|posture)\]/i;
export function containsSensingPrefix(msg: string): boolean {
  return SENSING_PREFIX_ANYWHERE_RE.test(msg);
}

// True when a tool command addresses one of HAL's camera endpoints.
export function isCameraAPICommand(value: string): boolean {
  return /\/camera(?:[/?\s"']|$)/.test(value);
}

// Keep the runtime list in step with camera_snapshot.go, the sensing handler and hal/config.py _AGENT_CONFIG_DIRS.
const AGENT_SNAPSHOT_PATH_RE = /\/root\/\.(openclaw|hermes|picoclaw|codex|claudecode|opencode)\/(workspace|media\/hal-snapshots)\/([A-Za-z0-9][A-Za-z0-9._-]*\.(?:jpg|jpeg))\b/g;

function agentSnapshotURL(path: string): string | null {
  const match = [...path.matchAll(AGENT_SNAPSHOT_PATH_RE)][0];
  if (!match) return null;
  const source = match[2] === "workspace" ? "workspace" : "media-hal-snapshots";
  return `/api/sensing/agent-snapshot/${match[1]}/${source}/${match[3]}`;
}

// Extract UI-safe, server-served snapshot URLs from the camera tool result.
export function cameraSnapshotURLs(events: DisplayEvent[]): string[] {
  const urls = new Set<string>();
  for (const ev of events) {
    const d = ev.detail as FlowEventDetail | undefined;
    const url = d?.snapshot_url ?? d?.data?.snapshot_url;
    if (typeof url === "string" && url.startsWith("/api/sensing/agent-snapshot/")) {
      urls.add(url);
      continue;
    }
    const legacyURL = agentSnapshotURL(`${ev.summary ?? ""}\n${JSON.stringify(d ?? {})}`);
    if (legacyURL) urls.add(legacyURL);
  }
  return [...urls];
}

export interface PipelineRow {
  /** Underlying OpenClaw stream type — drives row color/label. */
  kind: "thinking" | "assistant" | "tool" | "tool_result"
    | "lifecycle_start" | "lifecycle_end"
    | "agent_first_token" | "thinking_first_token"
    | "compaction" | "error" | "other";
  label: string;
  detail?: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  chunks: number;
  chars: number;
}

// Aggregate the raw turn events into a sequential list of pipeline rows.
export function aggregateEvents(events: DisplayEvent[]): PipelineRow[] {
  const rows: PipelineRow[] = [];

  const flowEventNode = (ev: DisplayEvent): string | undefined => {
    if (ev.type !== "flow_event") return undefined;
    const d = ev.detail as FlowEventDetail | undefined;
    return d?.node;
  };

  const ts = (ev: DisplayEvent) => new Date(ev.time).getTime();
  const deltaText = (ev: DisplayEvent): string => {
    const d = ev.detail as FlowEventDetail | undefined;
    return (d?.delta ?? d?.text ?? d?.data?.delta ?? d?.data?.text ?? ev.summary ?? "");
  };

  for (const ev of events) {
    const fnode = flowEventNode(ev);

    let kind: PipelineRow["kind"] | null = null;
    if (ev.type === "thinking") kind = "thinking";
    else if (ev.type === "assistant_delta") kind = "assistant";

    if (kind) {
      const t = ts(ev);
      const text = deltaText(ev);
      const last = rows[rows.length - 1];
      if (last && last.kind === kind) {
        last.endMs = t;
        last.durationMs = last.endMs - last.startMs;
        last.chunks += 1;
        last.chars += text.length;
      } else {
        rows.push({
          kind,
          label: kind,
          startMs: t,
          endMs: t,
          durationMs: 0,
          chunks: 1,
          chars: text.length,
        });
      }
      continue;
    }

    // tool_call is logged twice per phase (agent stream + session.tool); merge the duplicates.
    const isTool = ev.type === "tool_call" || fnode === "tool_call";
    if (isTool) {
      const d = ev.detail as FlowEventDetail | undefined;
      const phase = d?.data?.phase ?? d?.phase ?? "";
      const toolName =
        d?.data?.name ?? d?.data?.tool
        ?? d?.name ?? d?.tool
        ?? "tool";
      const t = ts(ev);
      if (phase === "start" || phase === "") {
        const argsObj = d?.data?.args ?? d?.args;
        let argsSummary = "";
        if (argsObj) {
          try {
            const parsed = typeof argsObj === "string" ? JSON.parse(argsObj) : argsObj;
            argsSummary = parsed?.command ?? JSON.stringify(parsed);
          } catch { argsSummary = String(argsObj); }
        }
        const last = rows[rows.length - 1];
        if (last && last.kind === "tool" && last.label === `tool · ${toolName}` && (t - last.startMs) < 1000 && last.durationMs === 0) {
          if (argsSummary && !last.detail) last.detail = argsSummary;
          if (t > last.endMs) last.endMs = t;
          continue;
        }
        rows.push({
          kind: "tool",
          label: `tool · ${toolName}`,
          detail: argsSummary || undefined,
          startMs: t, endMs: t, durationMs: 0, chunks: 1, chars: 0,
        });
      } else if (phase === "result" || phase === "end") {
        for (let i = rows.length - 1; i >= 0; i--) {
          const r = rows[i];
          if (r.kind === "tool" && r.label === `tool · ${toolName}`) {
            r.endMs = t;
            r.durationMs = r.endMs - r.startMs;
            break;
          }
        }
      }
      continue;
    }

    if (fnode === "lifecycle_start" || fnode === "lifecycle_end") {
      const t = ts(ev);
      rows.push({
        kind: fnode === "lifecycle_start" ? "lifecycle_start" : "lifecycle_end",
        label: fnode.replace("_", ":"),
        startMs: t, endMs: t, durationMs: 0, chunks: 1, chars: 0,
      });
      continue;
    }

    if (fnode === "agent_first_token" || fnode === "thinking_first_token") {
      const t = ts(ev);
      const label = fnode.startsWith("thinking_")
        ? "thinking:" + fnode.slice("thinking_".length)
        : "agent:" + fnode.slice("agent_".length);
      rows.push({
        kind: fnode as PipelineRow["kind"],
        label,
        startMs: t, endMs: t, durationMs: 0, chunks: 1, chars: 0,
      });
      continue;
    }

    if (fnode === "compaction" || ev.type === "compaction") {
      rows.push({ kind: "compaction", label: "compaction", startMs: ts(ev), endMs: ts(ev), durationMs: 0, chunks: 1, chars: 0 });
      continue;
    }
    if (fnode === "error" || fnode === "agent_error") {
      const errMsg = ((ev.detail as FlowEventDetail | undefined)?.error
        ?? (ev.detail as FlowEventDetail | undefined)?.data?.error ?? "") as string;
      rows.push({ kind: "error", label: "error", detail: errMsg ? String(errMsg).slice(0, 120) : undefined, startMs: ts(ev), endMs: ts(ev), durationMs: 0, chunks: 1, chars: 0 });
      continue;
    }
  }
  return rows;
}

// Derive active stage from most recent relevant events
export function deriveActiveStage(events: DisplayEvent[]): ActiveFlowStage {
  const recent = events.slice(-30);
  for (let i = recent.length - 1; i >= 0; i--) {
    const ev = recent[i];
    const key = ev.type === "flow_event" && ev.detail?.node
      ? `flow_event:${ev.detail.node}`
      : ev.type === "flow_enter" && ev.detail?.node
      ? `flow_enter:${ev.detail.node}`
      : ev.type === "flow_exit" && ev.detail?.node
      ? `flow_exit:${ev.detail.node}`
      : ev.type;
    for (const node of [...FLOW_NODES].reverse()) {
      if (node.triggers.includes(key)) return node.id;
    }
  }
  return "idle";
}

export function extractEventRunId(ev: DisplayEvent): string | undefined {
  if (ev.runId) return ev.runId;
  const detail = ev.detail as FlowEventDetail | undefined;
  return detail?.run_id ?? detail?.runId ?? detail?.data?.run_id ?? detail?.data?.runId;
}

export function parseChannelSummary(summary: string): string {
  const m = summary.match(/^\[[^\]]+\]\s*(.*)/);
  if (!m) return summary.trim();
  return (m[1] ?? "").trim();
}

// Only our dedicated history runs carry this envelope.
export function externalHistory(turn: Turn): { source: string; agentName: string; input: string; output: string } | null {
  if (!turn.runId?.startsWith("device-chat-context-")) return null;
  for (const event of turn.events) {
    if (extractEventRunId(event) !== turn.runId) continue;
    const detail = event.detail as FlowEventDetail | undefined;
    if (event.type !== "chat_send" && event.type !== "chat_input" &&
        !(["flow_event", "flow_enter"].includes(event.type) &&
          ["chat_send", "chat_input"].includes(detail?.node ?? ""))) continue;
    const message = detail?.data?.message ?? detail?.message;
    if (typeof message !== "string") continue;
    const metadata = message.match(/^\[external-context\] (.+)$/m);
    const handled = message.match(/^\[HANDLED\] (.+)$/m);
    const reply = message.match(/^\[REPLY\] (.+)$/m);
    if (!metadata || !handled || !reply) continue;
    try {
      const meta = JSON.parse(metadata[1]);
      const input = JSON.parse(handled[1]);
      const output = JSON.parse(reply[1]);
      if (!meta || typeof meta.source !== "string" || !meta.source ||
          typeof input !== "string" || typeof output !== "string") continue;
      return { source: meta.source, agentName: typeof meta.agent_name === "string" ? meta.agent_name : "", input, output };
    } catch { /* A truncated chat_input preview may precede the complete chat_send. */ }
  }
  return null;
}

function externalResponseText(ev: DisplayEvent): string {
  if (ev.type !== "flow_event" || (ev.detail?.node !== "harness_response" && ev.detail?.node !== "realtime_response")) return "";
  const detail = ev.detail as FlowEventDetail;
  const text = detail.data?.text ?? detail.text;
  return typeof text === "string" ? text.trim() : "";
}

// A persisted Harness reply proves output, not speaker playback.
export function harnessOutputPresentation(turn: Turn, output: string): {
  label: "Harness" | "Shared result";
  resultRunId?: string;
} | null {
  if (!turn.runId || !output) return null;
  for (const event of [...turn.events].reverse()) {
    if (extractEventRunId(event) !== turn.runId) continue;
    const detail = event.detail as FlowEventDetail | undefined;
    const data = detail?.data ?? detail;
    const isFlowReply = event.type === "flow_event" && detail?.node === "harness_response";
    const isChatReply = event.type === "chat_response" && event.state === "final" && data?.source === "harness";
    if (!isFlowReply && !isChatReply) continue;
    const text = isFlowReply ? externalResponseText(event) : data?.message ?? event.summary;
    if (typeof text !== "string" || text.trim() !== output.trim()) continue;
    const reference = data?.result_reference === true || data?.result_reference === "true";
    return {
      label: reference ? "Shared result" : "Harness",
      resultRunId: reference && typeof data?.result_run_id === "string" ? data.result_run_id : undefined,
    };
  }
  return null;
}

export function turnHasOutput(turn: Turn): boolean {
  return turn.events.some((ev) =>
    Boolean(externalResponseText(ev)) ||
    ev.type === "tts" ||
    ev.type === "intent_match" ||
    (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed" || ev.detail?.node === "intent_match")),
  );
}

export function turnHasRealChannelInput(turn: Turn): boolean {
  return turn.events.some((ev) => {
    if (ev.type !== "chat_input") return false;
    const msg = parseChannelSummary(ev.summary);
    return msg.length > 0;
  });
}

export function turnHasChatInputEvent(turn: Turn): boolean {
  return turn.events.some((ev) =>
    ev.type === "chat_input" ||
    (ev.type === "flow_event" && ev.detail?.node === "chat_input") ||
    (ev.type === "flow_enter" && ev.detail?.node === "chat_input") ||
    (ev.type === "flow_exit" && ev.detail?.node === "chat_input"),
  );
}

export function turnHasSensingInput(turn: Turn): boolean {
  return turn.events.some((ev) =>
    ev.type === "sensing_input" ||
    (ev.type === "flow_enter" && ev.detail?.node === "sensing_input"),
  );
}

export function turnHasVoicePipeline(turn: Turn): boolean {
  return turn.events.some((ev) =>
    (ev.type === "flow_event" || ev.type === "flow_enter") && ev.detail?.node === "voice_pipeline_start",
  );
}

/** Bracket label from "[voice] hello" / "[motion] ..." on sensing_input / flow_enter sensing_input. */
export function sensingInputBracketType(ev: DisplayEvent): string | null {
  if (ev.type !== "sensing_input" && !(ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
    return null;
  }
  const sensingType = extractSensingType(ev.summary);
  if (sensingType) return sensingType;
  const m = ev.summary.match(/^\[([^\]]+)\]/);
  return m ? m[1] : null;
}

/** Same run_id can include motion (camera) then voice in one session; merge keeps the first segment's type (often "motion"). */
export function refineTurnTypeFromSensingInputs(turn: Turn): void {
  if (turn.type.startsWith("ambient:") || turn.type === "schedule") {
    return;
  }

  // Classify heartbeat runs early: their chat_input can carry a stale copy of the previous message.
  const hasDeviceInput = turn.events.some((ev) => {
    const isSensingInput = ev.type === "sensing_input"
      || ((ev.type === "flow_enter" || ev.type === "flow_event")
        && ev.detail?.node === "sensing_input");
    if (!isSensingInput) return false;
    const detail = ev.detail as FlowEventDetail | undefined;
    const message = detail?.data?.message ?? detail?.message;
    return typeof message === "string" && message.trim() !== "";
  });

  if (!hasDeviceInput) {
    for (const ev of turn.events) {
      if (ev.type === "heartbeat_run" || (ev.type === "flow_event" && ev.detail?.node === "heartbeat_run")) {
        turn.type = "heartbeat";
        return;
      }
    }
  }

  if (isChannelType(turn.type)) {
    let hasRealUser = false;
    let sensingType: string | null = null;
    let hasSystemMsg = false;
    for (const ev of turn.events) {
      if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
        const d = ev.detail as FlowEventDetail | undefined;
        const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
        const sender = d?.sender ?? d?.data?.sender ?? "";
        if (sender && sender !== "node-host") hasRealUser = true;
        const sensType = extractSensingType(msg);
        if (sensType && !sensingType) sensingType = sensType;
        if (sensType || /you just woke up/i.test(msg)) hasSystemMsg = true;
      }
    }
    if (hasRealUser) return;
    if (sensingType) { turn.type = sensingType; return; }
    let isCron = false;
    let cronLabel = "cron";
    for (const ev of turn.events) {
      if (ev.type === "cron_fire" || (ev.type === "flow_event" && ev.detail?.node === "cron_fire")) {
        isCron = true;
        break;
      }
    }
    for (const ev of turn.events) {
      if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
        const d = ev.detail as FlowEventDetail | undefined;
        const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
        const sender = d?.sender ?? d?.data?.sender ?? "";
        if (!isCron && (!sender || sender === "") && /scheduled reminder/i.test(msg)) {
          isCron = true;
        }
        if (isCron) {
          if (/music/i.test(msg)) cronLabel = "cron:music";
          break;
        }
      }
    }
    if (isCron) { turn.type = cronLabel; return; }
    if (hasSystemMsg) { turn.type = "system"; return; }
    return;
  }

  let sawChat = "";
  let sawVoice = false;
  let sawVoiceCommand = false;
  const noteChat = (t: string) => { if (isChatType(t) && !sawChat) sawChat = t; };
  for (const ev of turn.events) {
    const t = sensingInputBracketType(ev);
    if (t && isChatType(t)) noteChat(t);
    else if (t === "voice_command") sawVoiceCommand = true;
    else if (t === "voice") sawVoice = true;
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const dtype = d?.data?.type ?? d?.type ?? "";
      if (isChatType(dtype)) noteChat(dtype);
      else if (dtype === "voice_command") sawVoiceCommand = true;
      else if (dtype === "voice") sawVoice = true;
    }
  }
  if (sawChat) turn.type = sawChat;
  else if (sawVoiceCommand) turn.type = "voice_command";
  else if (sawVoice) turn.type = "voice";
}

export function groupIntoTurns(events: DisplayEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | null = null;

  function isTurnStart(ev: DisplayEvent): { type: string; path: Turn["path"]; forceNewTurn?: boolean; boundary?: Turn["boundary"] } | null {
    if (ev.type === "sensing_drop") {
      const m = ev.summary.match(/^\[([^\]]+)\]/);
      const t = m ? m[1] : "unknown";
      return { type: t, path: "dropped", forceNewTurn: true };
    }
    if (ev.type === "sensing_queued") {
      const m = ev.summary.match(/^\[([^\]]+)\]/);
      const t = m ? m[1] : "unknown";
      return { type: t, path: "queued", forceNewTurn: true };
    }
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const t = sensingInputBracketType(ev) ?? "unknown";
      return {
        type: t,
        path: "unknown",
        forceNewTurn: t === "voice" || t === "voice_command" || isChatType(t),
        boundary: t === "voice" || t === "voice_command" ? "mic" : undefined,
      };
    }
    if ((ev.type === "flow_event" || ev.type === "flow_enter") && ev.detail?.node === "voice_pipeline_start") {
      return { type: "voice", path: "unknown", forceNewTurn: true, boundary: "mic" };
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      if (/you just woke up/i.test(msg)) {
        return { type: "system", path: "agent", boundary: "chat" as const };
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      const sender = d?.sender ?? d?.data?.sender ?? "";
      if (sender === "node-host" && (containsSensingPrefix(msg) || /\[MANDATORY:/.test(msg) || /\[Follow /.test(msg) || /\[REPLY RULE:/.test(msg) || /\[context: current_user=/.test(msg))) {
        return null;
      }
      const sensType = extractSensingType(msg);
      if (sensType) {
        return { type: sensType, path: "agent", boundary: "chat" as const };
      }
      const chMatch = ev.summary.match(/^\[([^\]:]+)/);
      return { type: chMatch ? chMatch[1] : "channel", path: "agent", boundary: "chat" };
    }
    const ambientNode = ev.detail?.node ?? "";
    const isAmbientTurn = ev.type === "ambient_action" ||
      ((ev.type === "flow_event" || ev.type === "flow_enter") &&
       ambientNode.startsWith("ambient_") &&
       ambientNode !== "ambient_pause" && ambientNode !== "ambient_resume");
    if (isAmbientTurn) {
      const sub = ambientNode.replace("ambient_", "") || "idle";
      return { type: `ambient:${sub}`, path: "local" };
    }
    if (ev.type === "schedule_trigger" || ev.type === "cron_fire" ||
        (ev.type === "flow_event" && (ev.detail?.node === "schedule_trigger" || ev.detail?.node === "cron_fire"))) {
      return { type: "schedule", path: "agent" };
    }
    return null;
  }

  for (const ev of events) {
    const evRunId = extractEventRunId(ev);
    const start = isTurnStart(ev);
    if (start) {
      const shouldForceSplit = Boolean(start.forceNewTurn);
      const currentDone = current?.status === "done" || current?.status === "error";
      if (!shouldForceSplit && !currentDone && current && current.runId && evRunId && current.runId === evRunId) {
        current.events.push(ev);
        // Channel chat_input fires twice (placeholder, then real); upgrade the placeholder's type.
        if (current.type === "chat" || current.type === "unknown") {
          current.type = start.type;
        }
        continue;
      }
      if (current) turns.push(current);
      // Suffix duplicate run ids with seq so turn ids stay unique.
      let turnId = evRunId || `turn-${ev._seq}`;
      if (evRunId && turns.some((t) => t.id === evRunId)) {
        turnId = `${evRunId}:${ev._seq}`;
      }
      const isTerminalQueued = start.path === "queued";
      const isTerminalDropped = start.path === "dropped";
      const queuedForMs = (() => {
        const d = ev.detail as FlowEventDetail | undefined;
        const v = d?.data?.queued_for_ms ?? d?.queued_for_ms;
        return typeof v === "number" ? v : undefined;
      })();
      current = {
        id: turnId,
        runId: evRunId,
        startTime: ev.time,
        endTime: (isTerminalDropped || isTerminalQueued) ? ev.time : undefined,
        type: start.type,
        path: start.path,
        boundary: start.boundary,
        boundaryInstanceSeq: start.boundary ? ev._seq : undefined,
        status: (isTerminalDropped || isTerminalQueued) ? "done" : "active",
        events: [ev],
        queuedForMs,
      };
      continue;
    }

    if (current && current.runId && evRunId && current.runId !== evRunId) {
      const inferredType: Turn["type"] = current.type !== "unknown" ? current.type : "agent";
      const inferredPath: Turn["path"] = current.path !== "unknown" ? current.path : "agent";
      turns.push(current);
      current = {
        id: evRunId,
        runId: evRunId,
        startTime: ev.time,
        type: inferredType,
        path: inferredPath,
        status: "active",
        events: [ev],
      };
      continue;
    }

    if (!current) {
      continue;
    }

    // Split turn when a new lifecycle_start arrives after the turn already saw a lifecycle_end.
    const isLifecycleStart = (ev.type === "lifecycle" && ev.phase === "start") ||
      (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start");
    const hasLifecycleEnd = current.events.some((e) =>
      (e.type === "lifecycle" && e.phase === "end") ||
      (e.type === "flow_event" && e.detail?.node === "lifecycle_end"));
    if (isLifecycleStart && hasLifecycleEnd) {
      turns.push(current);
      current = {
        id: evRunId || `turn-${ev._seq}`,
        runId: evRunId || current.runId,
        startTime: ev.time,
        type: "unknown",
        path: "agent",
        status: "active",
        events: [ev],
      };
      continue;
    }

    current.events.push(ev);
    if (current.queuedForMs === undefined &&
        (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input"))) {
      const d = ev.detail as FlowEventDetail | undefined;
      const v = d?.data?.queued_for_ms ?? d?.queued_for_ms;
      if (typeof v === "number") current.queuedForMs = v;
    }
    if (current.type === "unknown" && (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input"))) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      const sensType = extractSensingType(msg);
      if (sensType) {
        current.type = sensType;
      } else {
        const chM = ev.summary.match(/^\[([^\]:]+)/);
        current.type = chM ? chM[1] : "channel";
      }
    }
    if (!current.runId && evRunId) {
      current.runId = evRunId;
      current.id = evRunId;
    }
    refineTurnTypeFromSensingInputs(current);

    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      current.path = "local";
    } else if (current.path !== "local") {
      const belongsToTurn = !current.runId || !evRunId || evRunId === current.runId;
      if (belongsToTurn && (evRunId || ev.type === "lifecycle" || ev.type === "thinking")) {
        current.path = "agent";
      }
    }

    if ((ev.type === "lifecycle" && (ev.phase === "end" || ev.phase === "error")) ||
        (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end")) {
      current.status = (ev.phase === "error" || ev.error) ? "error" : "done";
      current.endTime = ev.time;
    }
    if (ev.type === "intent_match") {
      current.status = "done";
      current.endTime = ev.time;
    }
    if (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed" || ev.detail?.node === "no_reply")) {
      current.status = "done";
      current.endTime = ev.time;
    }
    // chat_final_empty / chat_final_ok close a run that never opened a lifecycle (turn_steered is legacy).
    if (ev.type === "flow_event" && (ev.detail?.node === "chat_final_empty" || ev.detail?.node === "chat_final_ok" || ev.detail?.node === "turn_steered")) {
      current.status = "done";
      current.endTime = ev.time;
    }
    if (current.type.startsWith("ambient:") && ev.type === "flow_exit" && ev.detail?.node?.startsWith("ambient_")) {
      current.status = "done";
      current.endTime = ev.time;
    }
  }
  if (current) turns.push(current);

  const merged: Turn[] = [];
  const runIndex = new Map<string, number>();
  for (const turn of turns) {
    if (!turn.runId) {
      merged.push(turn);
      continue;
    }
    const idx = runIndex.get(turn.runId);
    if (idx === undefined) {
      runIndex.set(turn.runId, merged.length);
      merged.push(turn);
      continue;
    }
    if (turn.boundaryInstanceSeq !== undefined) {
      merged.push(turn);
      runIndex.set(turn.runId, merged.length - 1);
      continue;
    }

    const base = merged[idx];
    base.events.push(...turn.events);
    if (base.status !== "error" && turn.status === "error") base.status = "error";
    else if (base.status === "active" && turn.status === "done") base.status = "done";
    else if (base.status === "active" && turn.events.some(
      (e) => e.type === "flow_event" && (e.detail?.node === "chat_final_empty" || e.detail?.node === "turn_steered"))) {
      base.status = "done";
      if (!base.endTime) base.endTime = turn.endTime || turn.startTime;
    }
    if (!base.endTime && turn.endTime) base.endTime = turn.endTime;
    else if (base.endTime && turn.endTime && turn.endTime > base.endTime) base.endTime = turn.endTime;
    if (base.path !== "agent" && turn.path === "agent") base.path = "agent";
    if (base.type === "unknown" && turn.type !== "unknown") base.type = turn.type;
  }
  for (const turn of merged) {
    turn.events.sort((a, b) => a._seq - b._seq);
    const ownEvents = turn.runId
      ? turn.events.filter((event) => extractEventRunId(event) === turn.runId)
      : [];
    for (const event of ownEvents) {
      const detail = event.detail as FlowEventDetail | undefined;
      if (detail?.node !== "sensing_input" && detail?.node !== "realtime_response") continue;
      const voiceTurnType: unknown = detail.data?.voice_turn_type;
      if (voiceTurnType === "voice" || voiceTurnType === "voice_command" || voiceTurnType === "voice_followup") {
        turn.voiceTurnType = voiceTurnType;
        break;
      }
    }
    if (ownEvents.some((event) => {
      const detail = event.detail as FlowEventDetail | undefined;
      return detail?.node === "sensing_input" &&
        (detail.data?.route ?? detail.route) === "harness_only";
    })) turn.path = "harness";
    if (ownEvents.some((event) => event.detail?.node === "realtime_response")) {
      turn.path = "realtime";
      turn.type = "voice_agent_handled";
    }
    const response = [...ownEvents].reverse().find((event) => externalResponseText(event));
    if (response) {
      if (turn.status !== "error") turn.status = "done";
      turn.endTime = response.time;
    }
    const mergeEvent = turn.events.find((event) => event.type === "flow_event" && event.detail?.node === "turn_merged");
    const mergeDetail = mergeEvent?.detail as FlowEventDetail | undefined;
    const parentRunId = mergeDetail?.data?.parent_run_id ?? mergeDetail?.parent_run_id;
    if (typeof parentRunId === "string" && parentRunId && parentRunId !== turn.runId) {
      turn.mergedIntoRunId = parentRunId;
    }
  }

  const stitched: Turn[] = [];
  for (const turn of merged) {
    const prev = stitched[stitched.length - 1];
    if (!prev) {
      stitched.push(turn);
      continue;
    }
    if (turn.mergedIntoRunId || prev.mergedIntoRunId ||
        turn.path === "harness" || prev.path === "harness" ||
        turn.path === "realtime" || prev.path === "realtime" ||
        turn.events.some((event) => externalResponseText(event))) {
      stitched.push(turn);
      continue;
    }
    const prevHasNoOutput = !turnHasOutput(prev);
    const currLooksAgentReply = turn.path === "agent" && turnHasOutput(turn);
    const prevTs = new Date(prev.endTime || prev.startTime).getTime();
    const currTs = new Date(turn.startTime).getTime();
    const closeInTime = Number.isFinite(prevTs) && Number.isFinite(currTs) && (currTs - prevTs) <= 30_000;

    const prevIsChannelFallback = isChannelType(prev.type) && !turnHasRealChannelInput(prev);
    if (prevIsChannelFallback && prevHasNoOutput && currLooksAgentReply && closeInTime) {
      if (turn.runId && /^device-(chat|sensing)-/i.test(turn.runId)) {
        stitched.push(turn);
        continue;
      }
      prev.events.push(...turn.events);
      prev.events.sort((a, b) => a._seq - b._seq);
      prev.status = turn.status === "error" ? "error" : turn.status;
      prev.endTime = turn.endTime || prev.endTime;
      prev.path = "agent";
      continue;
    }

    const prevIsSensingNoOutput = turnHasSensingInput(prev) && prevHasNoOutput;
    const currIsOrphanOutput = !turnHasSensingInput(turn) && !turnHasRealChannelInput(turn) && turnHasOutput(turn);
    if (prevIsSensingNoOutput && currIsOrphanOutput && closeInTime) {
      prev.events.push(...turn.events);
      prev.events.sort((a, b) => a._seq - b._seq);
      prev.status = turn.status === "error" ? "error" : turn.status;
      prev.endTime = turn.endTime || prev.endTime;
      prev.path = "agent";
      continue;
    }

    stitched.push(turn);
  }

  for (const turn of stitched) {
    refineTurnTypeFromSensingInputs(turn);
    if (isChannelType(turn.type) && (!turnHasChatInputEvent(turn))) {
      turn.type = "unknown";
    }
    if (turn.status === "done" && !turnHasSensingInput(turn) && !turnHasRealChannelInput(turn) && !turnHasVoicePipeline(turn)) {
      if (isChannelType(turn.type) || turn.type === "unknown") {
        turn.type = "unknown";
      }
    }
  }

  const turnsByRunId = new Map(stitched.filter((turn) => turn.runId).map((turn) => [turn.runId, turn]));
  for (const turn of stitched) {
    if (!turn.mergedIntoRunId) continue;
    const ownTerminal = [...turn.events].reverse().find((event) =>
      (event.type === "lifecycle" && (event.phase === "end" || event.phase === "error")) ||
      (event.type === "flow_event" && (event.detail?.node === "lifecycle_end" || event.detail?.node === "lifecycle_error")));
    if (ownTerminal) {
      turn.status = ownTerminal.phase === "error" || ownTerminal.error || ownTerminal.detail?.node === "lifecycle_error" ? "error" : "done";
      turn.endTime = ownTerminal.time;
      continue;
    }
    if (turn.status !== "active") continue;
    const seen = new Set([turn.runId]);
    let parent = turnsByRunId.get(turn.mergedIntoRunId);
    while (parent && !seen.has(parent.runId)) {
      seen.add(parent.runId);
      if (parent.status !== "active") {
        turn.status = parent.status;
        turn.endTime = parent.endTime;
        break;
      }
      parent = parent.mergedIntoRunId ? turnsByRunId.get(parent.mergedIntoRunId) : undefined;
    }
  }

  for (const turn of stitched) {
    if (externalHistory(turn)) turn.type = "history_sync";
  }

  for (let i = 1; i < stitched.length; i++) {
    const prev = stitched[i - 1];
    const curr = stitched[i];
    const prevEnd = new Date(prev.endTime || prev.startTime).getTime();
    const currStart = new Date(curr.startTime).getTime();
    if (currStart - prevEnd > 60_000) {
      curr.sessionBreak = true;
    }
  }

  return stitched.reverse();
}

// Child runs own delivery after steering.
export function sharedTurnEvents(parent: Turn, child: Turn): DisplayEvent[] {
  if (parent === child) return child.events;
  const downstream = child.events.filter((event) => {
    const node = event.detail?.node ?? event.type;
    return node.startsWith("harness_") || node.startsWith("tts") || node.startsWith("hw_") ||
      ["assistant", "assistant_delta", "agent_response", "agent_first_token", "chat_response", "telegram_alert_broadcast", "error", "lifecycle_error"].includes(node);
  });
  // Identical assistant chunks already present on the host represent the same shared final.
  const responseKey = (event: DisplayEvent) => JSON.stringify([
    event.detail?.node ?? event.type, event.summary,
    (event.detail as FlowEventDetail | undefined)?.data,
    (event.detail as FlowEventDetail | undefined)?.text,
    (event.detail as FlowEventDetail | undefined)?.delta,
  ]);
  const parentResponses = new Set(parent.events.map(responseKey));
  const uniqueDownstream = downstream.filter((event) =>
    !["assistant", "assistant_delta", "agent_response", "agent_first_token"].includes(event.detail?.node ?? event.type) ||
    !parentResponses.has(responseKey(event)));
  return [...parent.events, ...uniqueDownstream].sort((a, b) => a._seq - b._seq);
}

// Extract runtime info for each node from turn events
export function extractNodeInfo(events: DisplayEvent[]): NodeInfoMap {
  const info: NodeInfoMap = {
    mic_input: [], cam_input: [], button_input: [], channel_input: [], webchat_input: [], intent_check: [], local_match: [],
    agent_call: [], agent_thinking: [], tool_exec: [],
    agent_response: [], tts_speak: [], schedule_trigger: [],
    os_gate: [], hw_camera: [], hw_led: [], hw_servo: [], hw_emotion: [], hw_audio: [], hw_wellbeing: [], hw_mood: [], hw_music_suggestion: [], hw_posture: [], tg_out: [], tg_alert: [],
    ambient: [],
  };
  const fmtToken = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  const pushUnique = (arr: string[], line: string) => {
    if (!line) return;
    if (!arr.includes(line)) arr.push(line);
  };
  const pushAgentResponse = (line: string) => pushUnique(info.agent_response, line);
  const pushLLMTokens = (line: string) => {
    pushUnique(info.agent_call, line);
    pushUnique(info.agent_thinking, line);
    pushUnique(info.agent_response, line);
  };

  let sensingEnterTs = 0;
  let lifecycleStartTs = 0;
  for (const ev of events) {
    const ts = new Date(ev.time).getTime();
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      if (!sensingEnterTs) sensingEnterTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start") {
      if (!lifecycleStartTs) lifecycleStartTs = ts;
    }
  }
  for (const ev of events) {
    if (ev.type === "sensing_input") {
      const m = ev.summary.match(/^\[([^\]]+)\]\s*(.*)/);
      const sType = m?.[1] ?? "";
      const d = ev.detail as FlowEventDetail | undefined;
      const dtype = d?.type ?? d?.data?.type ?? "";
      const isWeb = isChatType(sType) || isChatType(dtype);
      if (isWeb) {
        info.webchat_input.push(`"${m?.[2] ?? ev.summary}"`);
      } else {
        const isButton = /^touch\./i.test(sType);
        const isCam = !isButton && /motion|presence|light/i.test(sType);
        const target = isButton ? info.button_input : isCam ? info.cam_input : info.mic_input;
        if (m) {
          target.push(`type: ${m[1]}`, `"${m[2]}"`);
        } else {
          target.push(ev.summary);
        }
      }
    }
    {
      const aNode = ev.detail?.node ?? "";
      const isAmbientInfo = ev.type === "ambient_action" ||
        ((ev.type === "flow_event" || ev.type === "flow_enter" || ev.type === "flow_exit") &&
         aNode.startsWith("ambient_") && aNode !== "ambient_pause" && aNode !== "ambient_resume");
      if (isAmbientInfo) {
        const sub = aNode.replace("ambient_", "") || ev.summary || "";
        if (info.ambient.length < 3) info.ambient.push(`${sub}: ${ev.summary || "active"}`);
      }
    }
    if (ev.type === "schedule_trigger" || ev.type === "cron_fire" ||
        (ev.type === "flow_event" && (ev.detail?.node === "schedule_trigger" || ev.detail?.node === "cron_fire"))) {
      const d = ev.detail as Record<string, string> | undefined;
      info.schedule_trigger.push(d?.name ?? ev.summary ?? "cron fired");
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const msg = parseChannelSummary(ev.summary);
      info.channel_input.push(`"${msg || CHANNEL_FALLBACK_MESSAGE}"`);
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.data?.message ?? d?.message ?? "";
      const tts = d?.data?.tts ?? d?.tts ?? "";
      const rule = d?.data?.rule ?? d?.rule ?? "";
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      info.intent_check.push("⚡ local match");
      const parts = [`"${msg}" → ${tts}`];
      if (rule) parts.push(`rule: ${rule}`);
      for (const a of actions) {
        const m = a.match(/^(POST|GET|PUT|DELETE)\s+(\/\S+)\s*(.*)?$/);
        if (m) {
          const [, method, path, body] = m;
          let curl = `curl -s -X ${method} http://127.0.0.1:5001${path}`;
          if (body) curl += ` -H "Content-Type: application/json" -d '${body}'`;
          parts.push(`🔧 ${curl}`);
        } else {
          parts.push(`🔧 ${a}`);
        }
      }
      info.local_match.push(msg ? parts.join("\n") : ev.summary);
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      info.intent_check.push("→ agent route");
      const d = ev.detail as FlowEventDetail | undefined;
      const hasImage = d?.data?.has_image || d?.has_image;
      const imgBytes = Number(d?.data?.image_bytes ?? d?.image_bytes ?? 0);
      const chatMsg = d?.data?.message ?? d?.message ?? "";
      if (hasImage) info.agent_call.push(`📷 image attached (~${Math.round(imgBytes * 3 / 4 / 1024)}KB)`);
      if (chatMsg) {
        const snapAllRe = /\[snapshot:\s*([^\]]+)\]/g;
        let snapM;
        while ((snapM = snapAllRe.exec(chatMsg)) !== null) {
          info.agent_call.push(`🖼 snapshot: ${snapM[1].trim()}`);
        }
        const idx = info.agent_call.findIndex((l) => l.startsWith("📩"));
        if (idx >= 0) info.agent_call[idx] = `📩 ${chatMsg}`;
        else info.agent_call.push(`📩 ${chatMsg}`);
      }
    }
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.data?.message ?? d?.message ?? ev.summary ?? "";
      if (msg) {
        const snapAllRe = /\[snapshot:\s*([^\]]+)\]/g;
        let snapM;
        while ((snapM = snapAllRe.exec(msg)) !== null) {
          const line = `🖼 snapshot: ${snapM[1].trim()}`;
          if (!info.agent_call.includes(line)) info.agent_call.push(line);
        }
        if (!info.agent_call.some((l) => l.startsWith("📩"))) {
          info.agent_call.push(`📩 ${msg}`);
        }
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const msg = d?.message ?? d?.data?.message ?? "";
      const sender = d?.sender ?? d?.data?.sender ?? "";
      if (msg && !info.agent_call.some((l) => l.startsWith("📩"))) {
        info.agent_call.push(`📩 ${sender ? `[${sender}] ` : ""}${msg}`);
      }
    }
    if (ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const phase = d?.phase ?? d?.data?.phase ?? "";
      if (phase !== "start" && phase !== "") continue;
      const rawArgs = d?.args ?? d?.data?.args ?? "";
      let argsSummary = "";
      if (rawArgs) {
        try {
          const parsed = typeof rawArgs === "string" ? JSON.parse(rawArgs) : rawArgs;
          if (parsed?.command) {
            argsSummary = (parsed.command as string);
          } else {
            argsSummary = JSON.stringify(parsed);
          }
        } catch { argsSummary = String(rawArgs); }
      }
      if (argsSummary) {
        const entry = `🔧 ${argsSummary}`;
        if (!info.tool_exec.includes(entry)) info.tool_exec.push(entry);
        if (/\/emotion/.test(argsSummary)) pushUnique(info.hw_emotion, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/led|\/scene/.test(argsSummary)) pushUnique(info.hw_led, `🤖 LLM tool → ${argsSummary}`);
        else if (isCameraAPICommand(argsSummary)) pushUnique(info.hw_camera, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/servo/.test(argsSummary)) pushUnique(info.hw_servo, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/audio/.test(argsSummary)) pushUnique(info.hw_audio, `🤖 LLM tool → ${argsSummary}`);
      }
    }
    if (ev.type === "thinking" || (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start")) {
      if (ev.type === "thinking" && ev.summary) {
        info.agent_thinking.push(`"${ev.summary}…"`);
      }
      if (ev.type === "flow_event" && info.agent_thinking.length === 0) {
        info.agent_thinking.push("reasoning…");
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "agent_thinking") {
      const d = ev.detail as FlowEventDetail | undefined;
      const text = d?.data?.text ?? d?.text ?? "";
      if (text && !info.agent_thinking.some((l) => l.startsWith("🧠"))) {
        info.agent_thinking.push(`🧠 ${text}`);
      }
    }
    if (externalResponseText(ev)) {
      pushAgentResponse(`"${externalResponseText(ev)}"`);
    }
    if (ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      pushAgentResponse("🚫 [no reply] — agent decided to do nothing");
    }
    if (ev.type === "chat_response" || (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end")) {
      const d = ev.detail as FlowEventDetail | undefined;
      if (d?.message && !info.agent_response.some((l) => l.startsWith('"'))) {
        info.agent_response.push(`"${d.message}"`);
      }
      const dataErr = d?.data?.error;
      if (dataErr && info.agent_response.length < 2) {
        info.agent_response.push(`❌ ${dataErr}`);
      }
    }
    if (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed"))) {
      const d = ev.detail as FlowEventDetail | undefined;
      const text = d?.data?.text ?? d?.text ?? "";
      const isSuppressed = ev.type === "flow_event" && d?.node === "tts_suppressed";
      const label = isSuppressed ? "💬" : "🔊";
      if (text && info.tts_speak.length < 2) {
        info.tts_speak.push(`${label} "${text}"`);
      }
      if (text && !info.agent_response.some((l) => l.startsWith('"'))) {
        info.agent_response.push(`"${text}"`);
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_muted") {
      if (info.tts_speak.length < 3) info.tts_speak.push("🔇 speaker muted — reply not spoken");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_cancelled") {
      if (info.tts_speak.length < 3) info.tts_speak.push("✋ cancelled by click — reply not spoken");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "telegram_alert_broadcast") {
      const d = ev.detail as FlowEventDetail | undefined;
      const sessions = Number(d?.data?.sessions ?? 0);
      const msg = d?.data?.message ?? "";
      if (sessions) info.tg_alert.push(`📢 broadcast → ${sessions} session${sessions > 1 ? "s" : ""}`);
      if (msg) {
        const short = msg.length > 80 ? msg.slice(0, 80) + "…" : msg;
        info.tg_alert.push(`💬 ${short}`);
      }
    }
    if (ev.type === "lifecycle") {
      if (ev.phase === "start") info.agent_call.push(`run: ${ev.runId ?? "?"}`);
      if (ev.phase === "error") {
        pushAgentResponse(`❌ ${ev.error ?? ev.summary ?? "error"}`);
      }
      if (ev.phase === "end") {
        pushAgentResponse(ev.error ? `❌ ${ev.error}` : "✓ done");
        const d = ev.detail as Record<string, string> | undefined;
        if (d?.inputTokens) {
          const inp = parseInt(d.inputTokens, 10);
          const out = parseInt(d.outputTokens ?? "0", 10);
          pushLLMTokens(`tokens: ${fmtToken(inp)} in / ${fmtToken(out)} out`);
        }
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const d = ev.detail as FlowEventDetail | undefined;
      const u = d?.data;
      const inTok = Number(u?.input_tokens ?? 0);
      const outTok = Number(u?.output_tokens ?? 0);
      const cacheRead = Number(u?.cache_read_tokens ?? 0);
      const cacheWrite = Number(u?.cache_write_tokens ?? 0);
      const total = Number(u?.total_tokens ?? 0);
      if (inTok || outTok) pushLLMTokens(`tokens: ${fmtToken(inTok)} in / ${fmtToken(outTok)} out`);
      if (cacheRead || cacheWrite) pushLLMTokens(`cache: ${fmtToken(cacheRead)} read / ${fmtToken(cacheWrite)} write`);
      if (total) pushLLMTokens(`total: ${fmtToken(total)}`);
      const billed = inTok + cacheWrite + cacheRead + outTok;
      if (billed) pushLLMTokens(`billed: ${fmtToken(billed)}`);
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end") {
      const d = ev.detail as FlowEventDetail | undefined;
      const err = d?.data?.error;
      if (err) info.agent_response.push(`❌ ${err}`);
    }
    if (ev.type === "hw_call" || (ev.type === "flow_event" && ev.detail?.node === "hw_call")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const path = d?.data?.path ?? d?.path ?? "";
      const args = d?.data?.args ?? d?.args ?? "";
      if (path) pushUnique(info.tool_exec, `⚙ HW ${path} ${args}`);
    }
    if (ev.type === "hw_only_reply" || (ev.type === "flow_event" && ev.detail?.node === "hw_only_reply")) {
      pushAgentResponse("⚙ HW-only reply (no spoken text)");
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const tts = d?.data?.tts ?? d?.tts ?? "";
      if (tts && info.tts_speak.length < 3) info.tts_speak.push(`💡 ${tts}`);
    }
    // HW marker events: path + body from flow_event detail.data or a direct event summary.
    const parseHWEvent = (ev: DisplayEvent, fallbackPath: string): { path: string; body: string } => {
      const d = ev.detail as FlowEventDetail | undefined;
      if (d?.data?.path && d?.data?.args) return { path: d.data.path, body: d.data.args };
      const s = ev.summary ?? "";
      const i = s.indexOf(" ");
      return i > 0 ? { path: s.slice(0, i), body: s.slice(i + 1) } : { path: fallbackPath, body: s };
    };
    if (ev.type === "hw_emotion" || (ev.type === "flow_event" && ev.detail?.node === "hw_emotion")) {
      const { path, body } = parseHWEvent(ev, "/emotion");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_emotion, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -H "Content-Type: application/json" -d '${body}'`);
        const m = body.match(/"emotion"\s*:\s*"([^"]+)"/);
        pushUnique(info.os_gate, `🎭 → ${m ? m[1] : "emotion"}`);
      }
    }
    if (ev.type === "hw_led" || (ev.type === "flow_event" && ev.detail?.node === "hw_led")) {
      const { path, body } = parseHWEvent(ev, "/led/solid");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_led, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -H "Content-Type: application/json" -d '${body}'`);
        pushUnique(info.os_gate, `💡 → LED ${path}`);
      }
    }
    if (ev.type === "hw_servo" || (ev.type === "flow_event" && ev.detail?.node === "hw_servo")) {
      const { path, body } = parseHWEvent(ev, "/servo/play");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_servo, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -H "Content-Type: application/json" -d '${body}'`);
        pushUnique(info.os_gate, `🤖 → servo ${path}`);
      }
    }
    if (ev.type === "hw_audio" || (ev.type === "flow_event" && ev.detail?.node === "hw_audio")) {
      const { path, body } = parseHWEvent(ev, "/audio/play");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_audio, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -H "Content-Type: application/json" -d '${body}'`);
        pushUnique(info.os_gate, `🎵 → audio ${path}`);
      }
    }
    if (ev.type === "hw_wellbeing" || (ev.type === "flow_event" && ev.detail?.node === "hw_wellbeing")) {
      const { path, body } = parseHWEvent(ev, "/wellbeing/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_wellbeing, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -H "Content-Type: application/json" -d '${body}'`);
        const m = body.match(/"action"\s*:\s*"([^"]+)"/);
        pushUnique(info.os_gate, `💧 → wellbeing ${m ? m[1] : path}`);
      }
    }
    if (ev.type === "hw_mood" || (ev.type === "flow_event" && ev.detail?.node === "hw_mood")) {
      const { path, body } = parseHWEvent(ev, "/mood/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_mood, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -H "Content-Type: application/json" -d '${body}'`);
        const kindMatch = body.match(/"kind"\s*:\s*"([^"]+)"/);
        const moodMatch = body.match(/"mood"\s*:\s*"([^"]+)"/);
        const kind = kindMatch ? kindMatch[1] : "log";
        const mood = moodMatch ? moodMatch[1] : "?";
        pushUnique(info.os_gate, `🧠 → mood ${kind}=${mood}`);
      }
    }
    if (ev.type === "hw_music_suggestion" || (ev.type === "flow_event" && ev.detail?.node === "hw_music_suggestion")) {
      const { path, body } = parseHWEvent(ev, "/music-suggestion/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_music_suggestion, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -H "Content-Type: application/json" -d '${body}'`);
        const triggerMatch = body.match(/"trigger"\s*:\s*"([^"]+)"/);
        pushUnique(info.os_gate, `🎼 → music-suggest ${triggerMatch ? triggerMatch[1] : path}`);
      }
    }
    if (ev.type === "hw_posture" || (ev.type === "flow_event" && ev.detail?.node === "hw_posture")) {
      const { path, body } = parseHWEvent(ev, "/posture/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_posture, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -H "Content-Type: application/json" -d '${body}'`);
        const kindMatch = body.match(/"kind"\s*:\s*"([^"]+)"/);
        pushUnique(info.os_gate, `🪑 → posture ${kindMatch ? kindMatch[1] : path}`);
      }
    }
    if (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed")) {
      pushUnique(info.os_gate, "🔊 → TTS");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_suppressed") {
      const d = ev.detail as FlowEventDetail | undefined;
      const reason = d?.data?.reason ?? "suppressed";
      pushUnique(info.os_gate, `🔇 → TTS suppressed (${reason})`);
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_muted") {
      pushUnique(info.os_gate, "🔇 → TTS muted (speaker)");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_cancelled") {
      pushUnique(info.os_gate, "✋ → TTS cancelled (click)");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "hw_cancelled") {
      pushUnique(info.os_gate, "✋ → HW marker cancelled (click)");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "hw_failed") {
      const d = ev.detail as FlowEventDetail | undefined;
      const path = typeof d?.data?.path === "string" ? d.data.path : "";
      const err = typeof d?.data?.error === "string" ? d.data.error : "";
      pushUnique(info.os_gate, `⚠ → HW call failed ${path}${err ? ` (${err})` : ""}`.trim());
    }
    if (ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      pushUnique(info.os_gate, "🚫 → no reply");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "hw_only_reply") {
      pushUnique(info.os_gate, "⚙ → HW only (no speech)");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "telegram_alert_broadcast") {
      pushUnique(info.os_gate, "📢 → broadcast");
    }
  }
  const hasLifecycleEnd = events.some((e) =>
    (e.type === "lifecycle" && e.phase === "end") ||
    (e.type === "flow_event" && e.detail?.node === "lifecycle_end"));
  const hasNoReply = events.some((e) => e.type === "flow_event" && e.detail?.node === "no_reply");
  if (hasLifecycleEnd && info.agent_response.length === 0 && !hasNoReply) {
    info.agent_response.push("💤 no output — processed silently");
  }

  const fmtDur = (ms: number) => ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
    : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;

  let nSensingTs = 0, nChatSendTs = 0, nChatInputTs = 0;
  let nLifecycleStartTs = 0, nLifecycleEndTs = 0, nTtsTs = 0;
  let nLlmFirstTokenTs = 0;
  let nFirstToolTs = 0, nLastToolResultTs = 0;
  let nToolTotalMs = 0, nToolStartTs = 0;
  let nIntentMatchTs = 0;
  let nSensingExitDur = 0;
  let nLastBatchResultTs = 0, nInterToolMs = 0;

  for (const ev of events) {
    const ts = new Date(ev.time).getTime();
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      if (!nSensingTs) nSensingTs = ts;
    }
    if (ev.type === "flow_exit" && ev.detail?.node === "sensing_input") {
      const dataObj = typeof ev.detail?.data === "string" ? (() => { try { return JSON.parse(ev.detail!.data); } catch { return null; } })() : null;
      const dur = Number(ev.detail?.dur_ms ?? dataObj?.dur_ms ?? 0);
      if (dur > 0) nSensingExitDur = dur;
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      if (!nIntentMatchTs) nIntentMatchTs = ts;
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      if (!nChatInputTs) nChatInputTs = ts;
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      if (!nChatSendTs) nChatSendTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start") {
      if (!nLifecycleStartTs) nLifecycleStartTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end") {
      nLifecycleEndTs = ts;
    }
    {
      const isLiveDelta = ev.type === "thinking" || ev.type === "assistant_delta";
      const isMarker = ev.type === "flow_event"
        && (ev.detail?.node === "agent_first_token" || ev.detail?.node === "thinking_first_token");
      if ((isLiveDelta || isMarker) && !nLlmFirstTokenTs) {
        nLlmFirstTokenTs = ts;
      }
    }
    if (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed"))) {
      if (!nTtsTs) nTtsTs = ts;
    }
    const isToolCall = ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call");
    if (isToolCall) {
      const d = ev.detail as FlowEventDetail | undefined;
      const phase = d?.data?.phase ?? d?.phase ?? "";
      if (phase === "start") {
        if (!nFirstToolTs) nFirstToolTs = ts;
        if (nLastBatchResultTs && ts > nLastBatchResultTs) {
          nInterToolMs += ts - nLastBatchResultTs;
          nLastBatchResultTs = 0;
        }
        nToolStartTs = ts;
      }
      if (phase === "result") {
        nLastToolResultTs = ts;
        nLastBatchResultTs = ts;
        if (nToolStartTs) { nToolTotalMs += ts - nToolStartTs; nToolStartTs = 0; }
      }
    }
  }

  if (nSensingExitDur > 0) {
    const dur = fmtDur(nSensingExitDur);
    if (info.mic_input.length > 0) info.mic_input.unshift(`⏱ ${dur}`);
    else if (info.cam_input.length > 0) info.cam_input.unshift(`⏱ ${dur}`);
  }

  if (nSensingTs || nChatInputTs) {
    const from = nSensingTs || nChatInputTs;
    const to = nIntentMatchTs || nChatSendTs;
    if (to && to > from) {
      const ms = to - from;
      info.intent_check.unshift(`⏱ ${fmtDur(ms)}`);
    }
  }

  const thinkStartTs = nLlmFirstTokenTs || nLifecycleStartTs;
  if (thinkStartTs) {
    const to = nFirstToolTs || nLifecycleEndTs;
    if (to && to > thinkStartTs) {
      const streaming = to - thinkStartTs;
      const totalThinking = streaming + nInterToolMs;
      if (nInterToolMs > 0) {
        info.agent_thinking.unshift(`⏱ ${fmtDur(totalThinking)} (first ${fmtDur(streaming)} + between tools ${fmtDur(nInterToolMs)})`);
      } else {
        info.agent_thinking.unshift(`⏱ ${fmtDur(streaming)}`);
      }
    }
  }

  if (nToolTotalMs > 0) {
    info.tool_exec.unshift(`⏱ ${fmtDur(nToolTotalMs)}`);
  }

  if (nLastToolResultTs && nLifecycleEndTs && nLifecycleEndTs > nLastToolResultTs) {
    const ms = nLifecycleEndTs - nLastToolResultTs;
    if (ms > 0) info.agent_response.unshift(`⏱ ${fmtDur(ms)}`);
  }

  if (nLifecycleEndTs && nTtsTs) {
    const ms = nTtsTs - nLifecycleEndTs;
    if (ms > 0 && ms < 30_000) info.tts_speak.unshift(`⏱ ${fmtDur(ms)}`);
  } else if (nIntentMatchTs && nTtsTs) {
    const ms = nTtsTs - nIntentMatchTs;
    if (ms > 0 && ms < 30_000) info.tts_speak.unshift(`⏱ ${fmtDur(ms)}`);
  }

  return info;
}

// Extract total duration (ms) from a turn's start/end times.
export function turnDurationMs(turn: Turn): number {
  if (!turn.startTime || !turn.endTime) return 0;
  const ms = new Date(turn.endTime).getTime() - new Date(turn.startTime).getTime();
  return ms > 0 ? ms : 0;
}

// Billed tokens = input + cache_write + cache_read + output (cached reads are billed at full price).
export function turnBilledTokens(turn: Turn): number {
  for (const ev of turn.events) {
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const u = (ev.detail as FlowEventDetail)?.data;
      const inTok = Number(u?.input_tokens ?? 0);
      const outTok = Number(u?.output_tokens ?? 0);
      const cacheRead = Number(u?.cache_read_tokens ?? 0);
      const cacheWrite = Number(u?.cache_write_tokens ?? 0);
      return inTok + cacheWrite + cacheRead + outTok;
    }
  }
  return 0;
}

// Extract input/output summary from a turn
export function turnIO(turn: Turn): {
  input: string;
  output: string;
  hwOutput: string;
  snapshotUrls: string[];
  audioUrls: string[];
  poseBucket?: { id: string; files: string[] } | null;
} {
  let input = "";
  let output = "";
  let outputFromIntent = false;
  let hwOutput = "";
  const snapshotUrls: string[] = [];
  const audioUrls: string[] = [];
  let poseBucket: { id: string; files: string[] } | null = null;
  const turnRunId = turn.runId;
  for (const ev of turn.events) {
    const evRunId = extractEventRunId(ev);
    const sameRun = !turnRunId || !evRunId || evRunId === turnRunId;
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const dataMsg = d?.data?.message ?? d?.message;
      if (!input) {
        const m = ev.summary.match(/^\[([^\]]+)\]\s*(.*)/);
        input = dataMsg || (m ? m[2] : "") || ev.summary;
      }
      const audioUrl = d?.data?.audio ?? d?.audio;
      if (typeof audioUrl === "string" && audioUrl && !audioUrls.includes(audioUrl)) {
        audioUrls.push(audioUrl);
      }
      if (typeof dataMsg === "string") {
        const snapRe = /\[snapshot:\s*(?:\/tmp\/(?:lamp|hal)-(?:sensing|emotion|motion)-snapshots|\/var\/lib\/hal\/snapshots)\/((?:sensing|emotion|motion)_[^\]]+\.jpg)\]/g;
        let snapMatch;
        while ((snapMatch = snapRe.exec(dataMsg)) !== null) {
          const url = `/api/sensing/snapshot/${snapMatch[1]}`;
          if (!snapshotUrls.includes(url)) snapshotUrls.push(url);
        }
        // Pattern aligned with Go rePoseBucketMarker.
        const bm = dataMsg.match(/\[pose_bucket:\s*([^\]]+)\]/);
        if (bm) {
          const wm = dataMsg.match(/\[pose_worst:\s*([^\]]+)\]/);
          const files = wm
            ? wm[1].split(",").map((f) => f.trim()).filter(Boolean)
            : [];
          poseBucket = { id: bm[1].trim(), files };
        }
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const fullMsg = d?.message ?? d?.data?.message;
      const sender = d?.sender ?? d?.data?.sender;
      const msg = fullMsg || parseChannelSummary(ev.summary);
      if (msg) {
        input = sender ? `[${sender}] ${msg}` : msg;
      } else if (!input) {
        input = CHANNEL_FALLBACK_MESSAGE;
      }
    }
    if (!input && turn.type.startsWith("ambient:")) {
      input = turn.type.replace("ambient:", "") + " behavior";
    }
    if (!input && (ev.type === "schedule_trigger" || ev.type === "cron_fire")) {
      const d = ev.detail as FlowEventDetail | undefined;
      input = d?.name ?? d?.data?.name ?? ev.summary ?? "scheduled task";
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const raw = (d?.data?.message ?? d?.message ?? ev.summary ?? "").trim();
      const snapRe = /\[snapshot:\s*(?:\/tmp\/(?:lamp|hal)-(?:sensing|emotion|motion)-snapshots|\/var\/lib\/hal\/snapshots)\/((?:sensing|emotion|motion)_[^\]]+\.jpg)\]/g;
      let snapMatch;
      while ((snapMatch = snapRe.exec(raw)) !== null) {
        const url = `/api/sensing/snapshot/${snapMatch[1]}`;
        if (!snapshotUrls.includes(url)) snapshotUrls.push(url);
      }
      if (!input) {
        const m = raw.match(/^\s*\[(?:sensing:[^\]]+|activity|emotion|speech_emotion)\]\s*(.*)$/is);
        const extracted = (m?.[1] ?? "").replace(/\n?\[snapshot:[^\]]+\]/g, "").trim();
        if (extracted) input = extracted;
      }
    }
    if (sameRun && (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match"))) {
      const d = ev.detail as FlowEventDetail | undefined;
      output = d?.data?.tts ?? d?.tts ?? ev.summary ?? output;
      outputFromIntent = true;
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      for (const a of actions) {
        const m = a.match(/^(?:POST|GET|PUT|DELETE)\s+(\/\S+)/);
        if (m && !hwOutput.includes(m[1])) {
          hwOutput += (hwOutput ? ", " : "") + m[1];
        }
      }
    }
    if (!outputFromIntent && sameRun && (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed")))) {
      const d = ev.detail as FlowEventDetail | undefined;
      output = d?.data?.full_text ?? d?.full_text ?? d?.data?.text ?? d?.text ?? ev.summary ?? output;
    }
    if (turnRunId && evRunId === turnRunId && externalResponseText(ev)) {
      output = externalResponseText(ev);
      if (ev.detail?.node === "realtime_response") {
        const detail = ev.detail as FlowEventDetail;
        const question = detail.data?.input ?? detail.input;
        if (typeof question === "string") input = question;
      }
    }
    if (!output && sameRun && ev.type === "chat_response" && ev.state === "final") {
      const d = ev.detail as FlowEventDetail | undefined;
      output = d?.message ?? ev.summary ?? "";
    }
    if (!output && sameRun && ev.type === "flow_event" && ev.detail?.node === "chat_final_ok") {
      const d = ev.detail as FlowEventDetail | undefined;
      output = d?.data?.message ?? d?.message ?? "";
    }
    if (!output && sameRun && ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      output = "[no reply]";
    }
    if (turn.type.startsWith("ambient:") && ev.type === "flow_exit" && ev.detail?.node?.startsWith("ambient_")) {
      output = ev.summary || "done";
    }
    if (ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call")) {
      const d = ev.detail as FlowEventDetail | undefined;
      const args = d?.args ?? d?.data?.args ?? "";
      if (args) {
        const argsStr = typeof args === "string" ? args : JSON.stringify(args);
        const m = argsStr.match(/(?:POST|GET|PUT|DELETE)\s+(http\S+)/i);
        if (m) {
          const endpoint = m[1].replace(/^https?:\/\/127\.0\.0\.1:\d+/, "");
          if (endpoint && !hwOutput.includes(endpoint)) {
            hwOutput += (hwOutput ? ", " : "") + endpoint;
          }
        }
      }
    }
  }
  for (const url of cameraSnapshotURLs(turn.events)) {
    if (!snapshotUrls.includes(url)) snapshotUrls.push(url);
  }
  const history = externalHistory(turn);
  if (history) {
    input = history.input;
    output = history.output;
  }
  return { input, output, hwOutput, snapshotUrls, audioUrls, poseBucket };
}

// Returns X from the backend-injected `[context: current_user=X]` tag, or null.
export function turnCurrentUser(turn: Turn): string | null {
  const re = /\[context:\s*current_user=([^\]]+)\]/i;
  for (const ev of turn.events) {
    const d = ev.detail as FlowEventDetail | undefined;
    const candidates: unknown[] = [
      d?.message,
      d?.data?.message,
      d?.text,
      d?.data?.text,
      ev.summary,
    ];
    for (const c of candidates) {
      if (typeof c !== "string") continue;
      const m = c.match(re);
      if (m) return m[1].trim();
    }
  }
  return null;
}

export function turnTokenStats(turn: Turn): { inTok: number; outTok: number; cacheRead: number; cacheWrite: number; total: number } | null {
  let inTok = 0;
  let outTok = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  let total = 0;

  for (const ev of turn.events) {
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const d = ev.detail as FlowEventDetail | undefined;
      const u = d?.data ?? {};
      inTok = Math.max(inTok, Number(u.input_tokens ?? 0));
      outTok = Math.max(outTok, Number(u.output_tokens ?? 0));
      cacheRead = Math.max(cacheRead, Number(u.cache_read_tokens ?? 0));
      cacheWrite = Math.max(cacheWrite, Number(u.cache_write_tokens ?? 0));
      total = Math.max(total, Number(u.total_tokens ?? 0));
      continue;
    }

    if (ev.type === "lifecycle" && ev.phase === "end" && ev.detail) {
      const d = ev.detail as FlowEventDetail;
      inTok = Math.max(inTok, Number(d.inputTokens ?? 0));
      outTok = Math.max(outTok, Number(d.outputTokens ?? 0));
      cacheRead = Math.max(cacheRead, Number(d.cacheRead ?? 0));
      cacheWrite = Math.max(cacheWrite, Number(d.cacheWrite ?? 0));
      total = Math.max(total, Number(d.totalTokens ?? 0));
    }
  }

  if (!inTok && !outTok && !cacheRead && !cacheWrite && !total) return null;
  if (!total && (inTok || outTok || cacheRead || cacheWrite)) {
    total = inTok + outTok + cacheRead + cacheWrite;
  }
  return { inTok, outTok, cacheRead, cacheWrite, total };
}

// Display/filter name; never changes the event's routing type.
export function turnDisplayType(turn: Turn): string {
  if (externalHistory(turn)) return "history_sync";
  if (turn.type === "voice_agent_handled") {
    return turn.voiceTurnType === "voice_command" || turn.voiceTurnType === "voice_followup"
      ? `${turn.voiceTurnType}_handled` : turn.type;
  }
  return turn.voiceTurnType ?? turn.type;
}

export function turnMatchesSearch(turn: Turn, query: string): boolean {
  const { input, output } = turnIO(turn);
  return `${input} ${output} ${turnDisplayType(turn)} ${turn.type} ${turn.runId ?? ""} ${turn.id}`
    .toLowerCase().includes(query.toLowerCase().trim());
}

export function migrateTurnTypeFilters(types: string[]): Set<string> {
  const migrated = new Set(types);
  if (migrated.has("voice_agent_handled")) {
    migrated.add("voice_command_handled");
    migrated.add("voice_followup_handled");
  }
  if (migrated.has("voice")) {
    migrated.add("voice_command");
    migrated.add("voice_followup");
  }
  return migrated;
}
