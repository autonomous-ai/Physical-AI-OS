import { useCallback, useMemo, useState } from "react";
import {
  Search, SlidersHorizontal, X, Hexagon, ClipboardList, LayoutDashboard,
  PackageOpen, CalendarDays, Trash2, Inbox,
} from "lucide-react";
import { S } from "../styles";
import { API, FLOW_EVENTS_MAX, HW } from "../types";
import type { DisplayEvent, FaceOwnersDetail } from "../types";
import type { FlowStage } from "./types";
import { usePolling } from "../../../hooks/usePolling";
import { FLOW_NODES } from "./types";
import { turnDisplayType, turnMatchesSearch, migrateTurnTypeFilters, deriveActiveStage, groupIntoTurns, sharedTurnEvents, turnIO, turnBilledTokens, turnDurationMs, extractSensingType, hasSensingPrefix, isCameraAPICommand } from "./helpers";
import { FlowDiagram } from "./FlowDiagram";
import { TurnBadge } from "./TurnBadge";
import { CanvasModal } from "./CanvasModal";
import { CompactionModal } from "./CompactionModal";
import { PipelineModal } from "./PipelineModal";
import { FiltersModal } from "./FiltersModal";
import { UserAvatar } from "./UserAvatar";

// Typed `boolean` (not literal `false`) so the JSX guard is not a constant expression.
const SHOW_SIMULATE_CARD: boolean = false;

// Free-form flow/SSE JSON from the device, read defensively.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type FlowEventDetail = Record<string, any>;

const CAT_TYPES: Record<string, string[]> = {
  mic: ["voice", "voice_command", "voice_followup", "voice_agent_handled", "voice_command_handled", "voice_followup_handled", "sound", "speech_emotion", "speech_emotion.detected"],
  cam: ["motion", "motion.activity", "emotion.detected", "pose.ergo_risk", "presence.enter", "presence.leave", "presence.away", "light.level", "environment"],
  channel: ["telegram", "discord", "slack", "wechat", "channel"],
  web: ["web_chat", "mqtt_chat"],
  cron: ["cron", "cron:music"],
  system: ["system", "schedule", "music.mood", "heartbeat"],
  button: ["touch.head_pat"],
};

const FAKE_EVENTS: { label: string; type: string; message: string; color: string; tag: string }[] = [
  { label: "bật đèn",          type: "voice",       message: "bật đèn",                            color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "tắt đèn",          type: "voice",       message: "tắt đèn",                            color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "reading mode",     type: "voice",       message: "reading mode",                       color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "thời tiết?",       type: "voice",       message: "hôm nay thời tiết thế nào?",         color: "var(--lm-blue)",   tag: "AGENT"  },
  { label: "kể chuyện",        type: "voice",       message: "kể cho tôi nghe một câu chuyện",     color: "var(--lm-blue)",   tag: "AGENT"  },
  { label: "motion",           type: "motion",      message: "motion detected in living room",     color: "var(--lm-amber)",  tag: "SENSE"  },
  { label: "environment",      type: "environment", message: "temperature 28C humidity 65%",       color: "var(--lm-teal)",   tag: "ENV"    },
];

export function FlowSection({
  events,
  onClearEvents,
  isDebug,
}: {
  events: DisplayEvent[];
  onClearEvents: () => void;
  // Passed down because window.location is not reactive (#463).
  isDebug: boolean;
}) {
  const [showCanvas, setShowCanvas] = useState(false);
  const [showCompaction, setShowCompaction] = useState(false);
  const [compactionAt, setCompactionAt] = useState<{ at: string; label: string } | null>(null);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [mobilePipelineOpen, setMobilePipelineOpen] = useState(false);
  // Opt-out model: store excluded types; empty = show all.
  const [excludedTypes, setExcludedTypes] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem("os-excluded-types-v2");
      if (saved) return new Set<string>(JSON.parse(saved));
      const legacy = localStorage.getItem("os-excluded-types-v1");
      if (legacy) return migrateTurnTypeFilters(JSON.parse(legacy));
    } catch {
      // Corrupt saved filter: start with nothing excluded.
    }
    return new Set();
  });
  const [searchText, setSearchText] = useState("");
  const [fromTime, setFromTime] = useState("");
  const [toTime, setToTime] = useState("");
  const [sortBy, setSortBy] = useState<"newest" | "oldest" | "time_desc" | "time_asc" | "tokens_desc" | "tokens_asc">("newest");
  const [filtersOpen, setFiltersOpen] = useState(false);

  const flowGhostBtn = {
    fontSize: 11, padding: "4px 10px", borderRadius: 6,
    background: "transparent", border: "1px solid var(--lm-border)",
    color: "var(--lm-text-dim)", fontWeight: 600,
    whiteSpace: "nowrap" as const,
  };
  const flowPrimaryBtn = {
    ...flowGhostBtn,
    background: "var(--lm-amber-dim)", border: "1px solid var(--lm-amber)",
    color: "var(--lm-amber)", fontWeight: 700,
  };
  const flowDangerBtn = {
    ...flowGhostBtn,
    border: "1px solid var(--lm-red)", color: "var(--lm-red)", fontWeight: 700,
  };
  const flowGroup = {
    display: "inline-flex", alignItems: "center", gap: 4,
    padding: 3, borderRadius: 9,
    background: "color-mix(in srgb, var(--lm-text) 4%, transparent)",
    border: "1px solid var(--lm-border)",
  };
  const [firing, setFiring] = useState<string | null>(null);

  async function fireEvent(ev: typeof FAKE_EVENTS[0]) {
    setFiring(ev.label);
    try {
      await fetch(`${API}/sensing/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: ev.type, message: ev.message }),
      });
    } finally {
      setTimeout(() => setFiring(null), 800);
    }
  }

  const clearServerFlowLog = useCallback(async () => {
    const ok = window.confirm("Clear flow log file on server (today)? This cannot be undone.");
    if (!ok) return;
    try {
      const r = await fetch(`${API}/agent/flow-logs`, { method: "DELETE" });
      const j = await r.json();
      if (!r.ok || j?.status !== 1) throw new Error(j?.message || "request failed");

      setSelectedTurnId(null);
      onClearEvents();
      window.alert("Server flow log cleared.");
    } catch (e) {
      window.alert(`Failed to clear server flow log: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [onClearEvents]);

  const downloadUISnapshot = useCallback(() => {
    const turnsSnapshot = groupIntoTurns(events);
    const payload = {
      exportedAt: new Date().toISOString(),
      format: "os-monitor-ui-snapshot-v1",
      flowEventsWindow: FLOW_EVENTS_MAX,
      eventCount: events.length,
      turnCount: turnsSnapshot.length,
      events,
      turns: turnsSnapshot.map((t) => ({
        id: t.id,
        runId: t.runId,
        startTime: t.startTime,
        endTime: t.endTime,
        type: t.type,
        path: t.path,
        status: t.status,
        sessionBreak: t.sessionBreak,
        events: t.events,
      })),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `flow_ui_snapshot_${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [events]);

  const downloadServerJsonlTail = useCallback(async (): Promise<boolean> => {
    try {
      const r = await fetch(`${API}/agent/flow-logs?last=${FLOW_EVENTS_MAX}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const day = new Date().toISOString().slice(0, 10);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `flow_${day}_last${FLOW_EVENTS_MAX}.jsonl`;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      return true;
    } catch (e) {
      console.error(e);
      window.alert(`JSONL download failed: ${e instanceof Error ? e.message : String(e)}`);
      return false;
    }
  }, []);


  const downloadFlowBundle = useCallback(async () => {
    const jsonlOk = await downloadServerJsonlTail();
    if (jsonlOk) await new Promise((resolve) => setTimeout(resolve, 500));
    downloadUISnapshot();
  }, [downloadServerJsonlTail, downloadUISnapshot]);

  const saveExcluded = (next: Set<string>) => {
    try { localStorage.setItem("os-excluded-types-v2", JSON.stringify([...next])); } catch { /* Filter persistence only: the in-memory Set below still drives this session's view. */ }
  };

  const toggleType = (type: string) => {
    setExcludedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type); else next.add(type);
      saveExcluded(next);
      return next;
    });
  };

  const toggleCategory = (cat: string) => {
    const catTypes = CAT_TYPES[cat] ?? [];
    setExcludedTypes((prev) => {
      const allExcluded = catTypes.every((t) => prev.has(t));
      const next = new Set(prev);
      if (allExcluded) { catTypes.forEach((t) => next.delete(t)); }
      else { catTypes.forEach((t) => next.add(t)); }
      saveExcluded(next);
      return next;
    });
  };

  const resetAll = useCallback(() => {
    setSearchText(""); setFromTime(""); setToTime(""); setSortBy("newest");
    setExcludedTypes(() => { saveExcluded(new Set()); return new Set(); });
  }, []);

  const turns = useMemo(() => groupIntoTurns(events), [events]);

  // Polled live: turn events can be minutes stale.
  const [currentUser, setCurrentUser] = useState<string>("");
  const [currentUserSource, setCurrentUserSource] = useState<string>("");
  usePolling(async (signal) => {
    const r = await fetch(`${HW}/identity/current-user`, { signal });
    if (!r.ok) return;
    const j = await r.json();
    setCurrentUser(typeof j?.user === "string" ? j.user : "");
    setCurrentUserSource(typeof j?.source === "string" ? j.source : "");
  }, 5000);

  const [userPhotos, setUserPhotos] = useState<Record<string, string>>({});
  usePolling(async (signal) => {
    const r = await fetch(`${HW}/face/owners`, { signal });
    if (!r.ok) return;
    const j: FaceOwnersDetail = await r.json();
    const map: Record<string, string> = {};
    for (const p of j.persons ?? []) {
      if (p.label && p.photos?.[0]) map[p.label] = p.photos[0];
    }
    setUserPhotos(map);
  }, 30_000, { timeoutMs: 8000 });

  const availableTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const t of turns) seen.add(turnDisplayType(t));
    return [...seen];
  }, [turns]);

  const catAvailability = useCallback((cat: string) => {
    const catTypes = CAT_TYPES[cat] ?? [];
    const available = catTypes.filter((t) => availableTypes.includes(t));
    const active = available.length > 0 && available.every((t) => !excludedTypes.has(t));
    const partial = !active && available.some((t) => !excludedTypes.has(t));
    return { active, partial };
  }, [availableTypes, excludedTypes]);

  const activeFilters = useMemo(() =>
    (searchText.trim() ? 1 : 0) +
    (fromTime || toTime ? 1 : 0) +
    (sortBy !== "newest" ? 1 : 0) +
    (availableTypes.filter((t) => excludedTypes.has(t)).length > 0 ? 1 : 0),
    [searchText, fromTime, toTime, sortBy, availableTypes, excludedTypes],
  );

  const filteredTurns = useMemo(() => {
    const filtered = turns.filter((t) => {
      if (t.path === "dropped" && excludedTypes.has("__dropped")) return false;
      if (t.path !== "dropped" && excludedTypes.has(turnDisplayType(t))) return false;
      if (fromTime || toTime) {
        const m = t.startTime.match(/T(\d{2}:\d{2})/);
        const tt = m?.[1] ?? "";
        if (fromTime && tt < fromTime) return false;
        if (toTime && tt > toTime) return false;
      }
      if (searchText.trim()) {
        if (!turnMatchesSearch(t, searchText)) return false;
      }
      return true;
    });
    if (sortBy === "oldest") {
      filtered.reverse();
    } else if (sortBy === "time_desc") {
      filtered.sort((a, b) => turnDurationMs(b) - turnDurationMs(a));
    } else if (sortBy === "time_asc") {
      filtered.sort((a, b) => turnDurationMs(a) - turnDurationMs(b));
    } else if (sortBy === "tokens_desc") {
      filtered.sort((a, b) => turnBilledTokens(b) - turnBilledTokens(a));
    } else if (sortBy === "tokens_asc") {
      filtered.sort((a, b) => turnBilledTokens(a) - turnBilledTokens(b));
    }
    return filtered;
  }, [turns, excludedTypes, fromTime, toTime, searchText, sortBy]);
  const pairTintMap = useMemo(() => {
    const map = new Map<string, string>();
    const PAIR_BGS = [
      "rgba(167, 139, 250, 0.14)",
      "rgba(34, 211, 238, 0.14)",
      "rgba(244, 114, 182, 0.14)",
      "rgba(45, 212, 191, 0.14)",
      "rgba(129, 140, 248, 0.14)",
      "rgba(248, 113, 113, 0.12)",
      "rgba(132, 204, 22, 0.14)",
      "rgba(236, 72, 153, 0.12)",
    ];
    const hashColor = (key: string) => {
      let h = 0;
      for (let i = 0; i < key.length; i++) h = ((h << 5) - h + key.charCodeAt(i)) | 0;
      return PAIR_BGS[Math.abs(h) % PAIR_BGS.length];
    };
    // OS-server and OpenClaw copies of one message can differ (truncation, stripped snapshots), so match loosely.
    const normalizeForMatch = (s: string) =>
      s.replace(/^\[[^\]]+\]\s*/, "").replace(/…\s*$/, "").trim();
    const isDeviceRun = (id: string) => id.startsWith("device-");
    for (let i = 0; i < filteredTurns.length - 1; i++) {
      const a = filteredTurns[i];
      const b = filteredTurns[i + 1];
      const tryPair = (deviceTurn: typeof a, uuidTurn: typeof b) => {
        if (!isDeviceRun(deviceTurn.id) || isDeviceRun(uuidTurn.id)) return false;
        const closedEmpty = deviceTurn.events.some((ev) =>
          ev.type === "flow_event" && (
            (ev.detail as FlowEventDetail)?.node === "chat_final_empty" ||
            (ev.detail as FlowEventDetail)?.node === "turn_steered"
          )
        );
        if (!closedEmpty) return false;
        const deviceIn = normalizeForMatch(turnIO(deviceTurn).input);
        const uuidIn = normalizeForMatch(turnIO(uuidTurn).input);
        if (!deviceIn || !uuidIn) return false;
        if (Math.min(deviceIn.length, uuidIn.length) < 32) return false;
        if (!deviceIn.includes(uuidIn) && !uuidIn.includes(deviceIn)) return false;
        const color = hashColor(deviceTurn.id);
        map.set(a.id, color);
        map.set(b.id, color);
        return true;
      };
      if (!tryPair(a, b)) tryPair(b, a);
    }
    return map;
  }, [filteredTurns]);
  const selectedTurn = selectedTurnId
    ? (turns.find((t) => t.id === selectedTurnId) ?? turns.find((t) => t.runId === selectedTurnId))
    : filteredTurns[0];

  let pipelineTurn = selectedTurn;
  const pipelineRunIds = new Set<string>();
  while (pipelineTurn?.mergedIntoRunId && !pipelineRunIds.has(pipelineTurn.mergedIntoRunId)) {
    pipelineRunIds.add(pipelineTurn.mergedIntoRunId);
    const parent = turns.find((turn) => turn.runId === pipelineTurn?.mergedIntoRunId);
    if (!parent) break;
    pipelineTurn = parent;
  }
  const turnEvents = pipelineTurn && selectedTurn
    ? sharedTurnEvents(pipelineTurn, selectedTurn)
    : events.slice(-30);
  const activeStage = deriveActiveStage(turnEvents);

  const visitedStages = new Set<FlowStage>();
  for (const ev of turnEvents) {
    const node = ev.detail?.node as string | undefined;
    const key = (ev.type === "flow_event" || ev.type === "flow_enter" || ev.type === "flow_exit") && node
      ? `${ev.type}:${node}`
      : ev.type;
    for (const flowNode of FLOW_NODES) {
      if (flowNode.triggers.includes(key)) visitedStages.add(flowNode.id);
    }
    // tool_exec anchors the Event Pipeline; mark it visited on any agent stream event.
    if (ev.type === "thinking" || ev.type === "assistant_delta") {
      visitedStages.add("tool_exec");
    }
    if (ev.type === "flow_event" && (node === "lifecycle_start" || node === "lifecycle_end")) {
      visitedStages.add("tool_exec");
    }
    const isToolCall = ev.type === "tool_call" || (ev.type === "flow_event" && node === "tool_call");
    if (isToolCall) {
      const d = ev.detail as FlowEventDetail | undefined;
      const args = d?.args ?? d?.data?.args ?? "";
      if (isCameraAPICommand(String(args))) visitedStages.add("hw_camera");
    }
  }
  for (const ev of turnEvents) {
    const isSensingInput = ev.type === "sensing_input" ||
      (ev.type === "flow_enter" && ev.detail?.node === "sensing_input") ||
      (ev.type === "flow_event" && ev.detail?.node === "sensing_input");
    const fromSensingChatSend = (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) &&
      hasSensingPrefix(ev.summary ?? "");
    const d = ev.detail as FlowEventDetail | undefined;
    const sensingType = d?.data?.type ?? d?.type;
    const fromSensingAgentCall = (ev.type === "flow_event" && ev.detail?.node === "agent_call") &&
      (sensingType === "voice" || sensingType === "voice_command" || sensingType === "motion" || sensingType === "motion.activity" || sensingType === "emotion.detected" || sensingType === "speech_emotion.detected" || sensingType === "pose.ergo_risk" || sensingType === "sound");
    if (isSensingInput || fromSensingChatSend || fromSensingAgentCall) {
      let detectedType = sensingType;
      if (!detectedType && ev.summary) {
        detectedType = extractSensingType(ev.summary) ?? "";
      }
      const isMicEmotion = /speech_emotion/i.test(detectedType ?? "");
      const isButton = /^touch\./i.test(detectedType ?? "");
      const isCam = !isMicEmotion && !isButton && /motion|presence|light|emotion/i.test(detectedType ?? "");
      visitedStages.add(isButton ? "button_input" : isCam ? "cam_input" : "mic_input");
      break;
    }
  }

  if (visitedStages.has("local_match")) {
    const hasActions = turnEvents.some((ev) => {
      if (ev.type !== "intent_match" && !(ev.type === "flow_event" && ev.detail?.node === "intent_match")) return false;
      const d = ev.detail as FlowEventDetail | undefined;
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      return actions.length > 0;
    });
    if (hasActions) visitedStages.add("hw_led");
  }

  const hasTtsSuppressed = turnEvents.some((ev) =>
    ev.type === "flow_event" && ["tts_suppressed", "tts_muted", "tts_cancelled", "hw_cancelled"].includes((ev.detail as FlowEventDetail)?.node)
  );
  if (hasTtsSuppressed) visitedStages.add("tts_speak");

  const CHANNEL_TYPES = new Set(["telegram", "discord", "slack", "wechat", "channel"]);
  if (selectedTurn && CHANNEL_TYPES.has(selectedTurn.type) && visitedStages.has("agent_response")) {
    const hasNoReply = turnEvents.some((ev) =>
      (ev.type === "flow_event" && ev.detail?.node === "no_reply") ||
      (ev.type === "chat_response" && ev.summary === "[no reply]")
    );
    if (!hasNoReply) {
      visitedStages.add("tg_out");
    }
  }

  // Pipeline body, shared by the desktop inline card and the mobile PipelineModal.
  const pipelineBody = (
    <>
      <div style={{ marginBottom: 10, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" as const }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" as const }}>
          <span style={S.cardLabel}>Turn Pipeline</span>
          {selectedTurn && (
            <button
              onClick={() => setCompactionAt({
                at: selectedTurn.startTime,
                label: `${selectedTurn.type} @ ${new Date(selectedTurn.startTime).toLocaleTimeString()}`,
              })}
              title="Show the Agent compaction summary that was active at the moment this turn fired — the text injected at the top of this turn's prompt."
              style={{
                fontSize: 10, padding: "2px 8px", borderRadius: 4,
                background: "var(--lm-purple)", border: "1px solid var(--lm-purple)",
                color: "#fff", cursor: "pointer", fontWeight: 700,
                display: "inline-flex", alignItems: "center", gap: 4,
              }}
            >
              <ClipboardList size={11} strokeWidth={2.25} /> summary prompt of this turn
            </button>
          )}
        </div>
        {selectedTurn && (
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
            {selectedTurn.type} · {selectedTurn.events.length} events
            {selectedTurn.endTime ? ` · done` : ` · active`}
          </span>
        )}
      </div>
      {selectedTurn?.mergedIntoRunId && (
        <div style={{ padding: "6px 10px", fontSize: 11, color: "var(--lm-purple)", overflowWrap: "anywhere" }}>
          Merged into {selectedTurn.mergedIntoRunId} · {pipelineTurn !== selectedTurn
            ? "showing shared pipeline"
            : "shared pipeline is outside the loaded history"}
        </div>
      )}
      <FlowDiagram activeStage={activeStage} visitedStages={visitedStages} turnEvents={turnEvents} compact />
    </>
  );

  return (
    <div id="FLOW_SECTION" data-region="FLOW_SECTION" style={{ display: "flex", flexDirection: "column", gap: 14, height: "100%", overflow: "hidden" }}>
      {showCanvas && (
        <CanvasModal
          activeStage={activeStage}
          visitedStages={visitedStages}
          turnEvents={turnEvents}
          onClose={() => setShowCanvas(false)}
        />
      )}

      {mobilePipelineOpen && selectedTurn && (
        <PipelineModal onClose={() => setMobilePipelineOpen(false)}>
          {pipelineBody}
        </PipelineModal>
      )}

      {filtersOpen && (
        <FiltersModal
          onClose={() => setFiltersOpen(false)}
          searchText={searchText}
          setSearchText={setSearchText}
          excludedTypes={excludedTypes}
          toggleType={toggleType}
          toggleCategory={toggleCategory}
          availableTypes={availableTypes}
          setExcludedTypes={setExcludedTypes}
          saveExcluded={saveExcluded}
          hasDropped={turns.some((t) => t.path === "dropped")}
          sortBy={sortBy}
          setSortBy={setSortBy}
          fromTime={fromTime}
          setFromTime={setFromTime}
          toTime={toTime}
          setToTime={setToTime}
          onResetAll={resetAll}
          activeFilters={activeFilters}
          catAvailability={catAvailability}
        />
      )}

      {showCompaction && <CompactionModal onClose={() => setShowCompaction(false)} />}
      {compactionAt && (
        <CompactionModal
          at={compactionAt.at}
          turnLabel={compactionAt.label}
          onClose={() => setCompactionAt(null)}
        />
      )}

      <div
        id="FLOW_TOPBAR" data-region="FLOW_TOPBAR"
        style={{
          ...S.card, padding: "9px 14px",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-text) 3%, var(--lm-card)) 0%, var(--lm-card) 100%)",
          boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--lm-text) 8%, transparent)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" as const, gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" as const }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <span
                aria-hidden
                style={{
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  width: 20, height: 20, borderRadius: 6,
                  color: "var(--lm-amber)",
                  background: "var(--lm-amber-dim)",
                  border: "1px solid color-mix(in srgb, var(--lm-amber) 45%, transparent)",
                }}
              ><Hexagon size={12} strokeWidth={2.25} /></span>
              <span style={{ ...S.cardLabel, marginBottom: 0 }}>Flow Panel</span>
              <span
                title={`${turns.length} turns captured`}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  fontSize: 9, fontWeight: 700, letterSpacing: "0.08em",
                  textTransform: "uppercase" as const, color: "var(--lm-teal)",
                }}
              >
                <span
                  className="lm-live-dot"
                  style={{
                    width: 6, height: 6, borderRadius: "50%",
                    background: "var(--lm-teal)",
                    boxShadow: "0 0 6px var(--lm-teal)",
                  }}
                />
                live
              </span>
            </span>
            {currentUser && (() => {
              const isUnknown = currentUser === "unknown";
              const color = isUnknown ? "var(--lm-text-muted)" : "var(--lm-teal)";
              const photo = !isUnknown ? userPhotos[currentUser] : undefined;
              return (
                <span
                  title={
                    isUnknown
                      ? "Robot knows someone is here but not who (stranger / unrecognized voice)"
                      : `Robot's current user: ${currentUser}` +
                        (currentUserSource === "voice"
                          ? " (heard — recognized by voice)"
                          : currentUserSource === "face"
                            ? " (seen — recognized by camera)"
                            : "")
                  }
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 6,
                    fontSize: 11, padding: "2px 9px 2px 3px", borderRadius: 999,
                    background: `${color}18`, color,
                    fontWeight: 700, textTransform: "capitalize",
                    border: `1px solid ${color}55`,
                  }}
                >
                  <UserAvatar user={currentUser} photo={photo} size={18} color={color} />
                  {currentUser}
                </span>
              );
            })()}
          </div>

          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 8, alignItems: "center" }}>
            <div style={flowGroup}>
              <button
                onClick={() => setShowCompaction(true)}
                title={
                  "Xem 'bộ nhớ tóm tắt' mà Agent tự sinh và chèn vào đầu prompt của MỖI turn agent.\n\n" +
                  "• Vì sao cần: khi context vượt ~80k tokens, Agent auto-compact — gộp history cũ thành 1 đoạn summary, rồi dùng summary này thay cho history đến lần compact tiếp theo.\n" +
                  "• Rủi ro: nếu summary vô tình copy/méo rule từ SKILL.md, KNOWLEDGE.md, SOUL.md → agent sẽ theo summary (đứng đầu prompt) thay vì SKILL.md → trợ lý trả lời sai lý do không giải thích nổi.\n\n" +
                  "Click để xem: timestamp, summary chars, session file, và TOÀN VĂN summary đang điều khiển trợ lý."
                }
                className="lm-u-btn"
                style={{ ...flowGhostBtn, display: "inline-flex", alignItems: "center", gap: 5 }}
              ><ClipboardList size={13} strokeWidth={2} /> Summary</button>
              <button
                onClick={() => setShowCanvas(true)}
                title="Open the flow canvas — a stacked timeline of all turns."
                className="lm-u-btn"
                style={{ ...flowPrimaryBtn, display: "inline-flex", alignItems: "center", gap: 5 }}
              ><LayoutDashboard size={13} strokeWidth={2} /> Canvas</button>
            </div>

            <div style={flowGroup}>
              <button
                type="button"
                onClick={() => void downloadFlowBundle()}
                title={`Downloads 3 files: (1) server JSONL last ${FLOW_EVENTS_MAX} lines — same tail as this panel; (2) UI snapshot JSON (events + turns); (3) Agent debug payload JSONL.`}
                className="lm-u-btn"
                style={{ ...flowGhostBtn, display: "inline-flex", alignItems: "center", gap: 5 }}
              ><PackageOpen size={13} strokeWidth={2} /> Bundle</button>
              <a
                href={`${API}/agent/flow-logs`}
                download
                title="Full day JSONL on server (all lines today — wider than the panel window)"
                className="lm-u-btn"
                style={{ ...flowGhostBtn, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 5 }}
              ><CalendarDays size={13} strokeWidth={2} /> Full day</a>
            </div>

            <button
              onClick={clearServerFlowLog}
              title="Clear server flow log + Agent debug logs"
              className="lm-u-btn"
              style={{ ...flowDangerBtn, display: "inline-flex", alignItems: "center", gap: 5 }}
            ><Trash2 size={13} strokeWidth={2} /> Clear</button>
          </div>
        </div>
      </div>

      {SHOW_SIMULATE_CARD && window.location.hostname === "localhost" && (
        <div style={{ ...S.card, padding: "10px 14px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
            <span style={S.cardLabel}>Simulate Event</span>
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>dev only · fires POST /sensing/event on robot</span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 6 }}>
            {FAKE_EVENTS.map((ev) => (
              <button
                key={ev.label}
                onClick={() => fireEvent(ev)}
                disabled={firing !== null}
                style={{
                  fontSize: 11, padding: "4px 11px", borderRadius: 6, cursor: "pointer",
                  background: firing === ev.label ? `${ev.color}25` : "var(--lm-surface)",
                  border: `1px solid ${firing === ev.label ? ev.color : "var(--lm-border)"}`,
                  color: firing === ev.label ? ev.color : "var(--lm-text-dim)",
                  fontWeight: 600, transition: "all 0.15s",
                  display: "flex", alignItems: "center", gap: 5,
                }}
              >
                <span style={{
                  fontSize: 9, padding: "1px 4px", borderRadius: 3,
                  background: `${ev.color}20`, color: ev.color, fontWeight: 700,
                }}>{ev.tag}</span>
                {firing === ev.label ? "…" : ev.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div id="FLOW_BODY" data-region="FLOW_BODY" className="lm-flow-layout" style={{ display: "flex", gap: 14, flex: 1, minHeight: 0 }}>

        <div id="FLOW_LEFTBAR" data-region="FLOW_LEFTBAR" className="lm-flow-turns" style={{
          ...S.card,
          flexShrink: 0,
          display: "flex",
          flexDirection: "column" as const,
          minHeight: 0,
          padding: 0,
          overflow: "hidden",
        }}>
          <div id="FLOW_LEFTBAR_HEADER" data-region="FLOW_LEFTBAR_HEADER" className="lm-flow-turns-header" style={{ padding: "10px 12px 8px", borderBottom: "1px solid var(--lm-border)" }}>
            <div style={{ display: "flex", alignItems: "center", marginBottom: 6, gap: 6 }}>
              <span style={{ ...S.cardLabel, marginBottom: 0 }}>Turns</span>
              {(() => {
                const filtered = filteredTurns.length !== turns.length;
                return (
                  <span
                    className="lm-flow-count"
                    title={filtered ? `${filteredTurns.length} shown · ${turns.length} total (filtered)` : `${turns.length} turns`}
                    style={filtered ? {
                      color: "var(--lm-amber)",
                      background: "var(--lm-amber-dim)",
                      borderColor: "color-mix(in srgb, var(--lm-amber) 45%, transparent)",
                    } : undefined}
                  >
                    {filtered
                      ? <>{filteredTurns.length}<span style={{ opacity: 0.6 }}> / {turns.length}</span></>
                      : turns.length}
                  </span>
                );
              })()}
              <button
                onClick={() => setFiltersOpen(true)}
                className="lm-u-btn"
                style={{
                  marginLeft: "auto", padding: "3px 9px", borderRadius: 6, fontSize: 11,
                  fontWeight: 600,
                  border: `1px solid ${activeFilters > 0 ? "var(--lm-amber)" : "var(--lm-border)"}`,
                  background: activeFilters > 0 ? "var(--lm-amber-dim)" : "transparent",
                  color: activeFilters > 0 ? "var(--lm-amber)" : "var(--lm-text-dim)",
                  display: "inline-flex", alignItems: "center", gap: 5,
                }}
                title="Open filters"
              >
                <SlidersHorizontal size={12} strokeWidth={2} />
                Filters{activeFilters > 0 ? ` · ${activeFilters}` : ""}
              </button>
            </div>

            <div style={{ position: "relative" }}>
              <Search
                size={13}
                strokeWidth={2}
                style={{
                  position: "absolute", left: 9, top: "50%", transform: "translateY(-50%)",
                  color: "var(--lm-text-muted)", pointerEvents: "none",
                }}
              />
              <input
                type="text"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Search input / output…"
                className="lm-u-input"
                style={{
                  width: "100%", boxSizing: "border-box" as const,
                  padding: "6px 28px 6px 28px", borderRadius: 6, fontSize: 11,
                  outline: "none",
                }}
              />
              {searchText && (
                <button
                  onClick={() => setSearchText("")}
                  aria-label="Clear search"
                  className="lm-u-btn"
                  style={{
                    position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
                    width: 20, height: 20, padding: 0, borderRadius: 5, border: "none",
                    background: "transparent", color: "var(--lm-text-muted)",
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                  }}
                ><X size={13} strokeWidth={2} /></button>
              )}
            </div>
          </div>
          <div id="FLOW_LEFTBAR_LIST" data-region="FLOW_LEFTBAR_LIST" style={{ flex: 1, overflowY: "auto", padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5 }} className="lm-flow-scroll">
            {filteredTurns.length === 0 ? (
              <div style={{
                flex: 1, display: "flex", flexDirection: "column",
                alignItems: "center", justifyContent: "center", gap: 6,
                padding: "32px 16px", color: "var(--lm-text-muted)", textAlign: "center" as const,
              }}>
                <Inbox size={26} strokeWidth={1.75} style={{ opacity: 0.5 }} />
                <span style={{ fontSize: 11, fontWeight: 600 }}>
                  {turns.length === 0 ? "No turns captured yet" : "No turns match filter"}
                </span>
                {turns.length > 0 && (
                  <span style={{ fontSize: 9, opacity: 0.7 }}>Try widening the filters above</span>
                )}
              </div>
            ) : (
              filteredTurns.map((turn, i) => (
                <div key={turn.id}>
                  {i > 0 && filteredTurns[i - 1].sessionBreak && (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 8, padding: "6px 4px", margin: "2px 0",
                    }}>
                      <div className="lm-flow-session-rule" />
                      <span style={{
                        fontSize: 8, fontWeight: 700, letterSpacing: "0.1em",
                        textTransform: "uppercase" as const,
                        color: "var(--lm-text-muted)", whiteSpace: "nowrap",
                        padding: "1px 7px", borderRadius: 999,
                        border: "1px solid var(--lm-border)",
                        background: "color-mix(in srgb, var(--lm-text) 4%, transparent)",
                      }}>{(() => {
                        const prev = filteredTurns[i - 1];
                        const gapMs = new Date(prev.startTime).getTime()
                          - new Date(turn.endTime || turn.startTime).getTime();
                        if (!Number.isFinite(gapMs) || gapMs <= 0) return "idle gap";
                        const min = Math.round(gapMs / 60_000);
                        const label = min < 60
                          ? `${Math.max(min, 1)}m`
                          : `${Math.floor(min / 60)}h ${min % 60}m`;
                        return `idle ${label}`;
                      })()}</span>
                      <div className="lm-flow-session-rule is-right" />
                    </div>
                  )}
                  <div
                    className="lm-turn-card"
                    data-expanded={turn.id === selectedTurn?.id ? "true" : "false"}
                    onClick={() => setSelectedTurnId(turn.id === selectedTurn?.id ? null : turn.id)}
                    style={{
                      borderRadius: 8,
                      cursor: "pointer",
                    }}
                  >
                    <TurnBadge
                      turn={turn}
                      pairTint={pairTintMap.get(turn.id)}
                      userPhotos={userPhotos}
                      isDebug={isDebug}
                      onViewPipeline={() => {
                        setSelectedTurnId(turn.id);
                        setMobilePipelineOpen(true);
                      }}
                    />
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div id="FLOW_CANVAS" data-region="FLOW_CANVAS" className="lm-flow-pipeline" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 12, minHeight: 0 }}>
          <div id="FLOW_PIPELINE" data-region="FLOW_PIPELINE" style={{ ...S.card, flex: 1, minHeight: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
            {pipelineBody}
          </div>
        </div>

      </div>
    </div>
  );
}
