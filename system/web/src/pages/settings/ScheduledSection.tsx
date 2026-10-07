import { useCallback, useEffect, useState } from "react";
import { CalendarClock, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import {
  createSchedule, deleteSchedule, listSchedules, resolveCadenceTimes, resolveScheduleKind, runScheduleNow, updateSchedule,
} from "@/lib/api";
import type { ScheduleCadence, ScheduleItem } from "@/lib/api";
import { ScheduleEditor } from "./ScheduleEditor";
import { bodyFromDraft, draftFromSchedule } from "./scheduleDraft";
import type { ScheduleDraft } from "./scheduleDraft";
import { describeLastRun, skippedRunMessage } from "./scheduleRunStatus";
import type { LastRunTone } from "./scheduleRunStatus";

import "./settings-lists.css";

const LAST_RUN_TONE_COLOR: Record<LastRunTone, string> = {
  success: C.green,
  failure: C.red,
  neutral: C.amber,
};

// Edits here are proposals: the cloud stays authoritative and confirms via schedule.sync.
const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function weekdayLabel(d: number): string {
  const idx = d === 7 ? 0 : d;
  return WEEKDAY_LABELS[idx] ?? `day ${d}`;
}

function ordinal(n: number): string {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  switch (n % 10) {
    case 1: return `${n}st`;
    case 2: return `${n}nd`;
    case 3: return `${n}rd`;
    default: return `${n}th`;
  }
}

// Formats an interval in MILLISECONDS (every_ms) as a short phrase.
function formatMs(ms: number): string {
  const minute = 60_000, hour = 3_600_000, day = 86_400_000;
  if (ms >= day && ms % day === 0) {
    const n = ms / day;
    return `${n} day${n === 1 ? "" : "s"}`;
  }
  if (ms >= hour && ms % hour === 0) {
    const n = ms / hour;
    return `${n} hour${n === 1 ? "" : "s"}`;
  }
  if (ms >= minute) {
    const n = Math.round(ms / minute);
    return `${n} minute${n === 1 ? "" : "s"}`;
  }
  return `${Math.round(ms / 1000)} second${Math.round(ms / 1000) === 1 ? "" : "s"}`;
}

// Snaps a jittered next-run instant back onto the configured wall-clock time (display only; the device jitters by +/-5 min).
function snapToScheduledTime(
  iso: string | undefined,
  cadence: ScheduleCadence,
  tz: string,
): string | undefined {
  const times = resolveCadenceTimes(cadence);
  if (!iso || times.length === 0) return iso;

  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;

  let hh: number, mm: number;
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz || undefined, hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(d);
    hh = Number(parts.find((p) => p.type === "hour")?.value ?? NaN);
    mm = Number(parts.find((p) => p.type === "minute")?.value ?? NaN);
  } catch {
    return iso;
  }
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return iso;

  const actual = hh * 60 + mm;
  let best: string | undefined;
  let bestDelta = Infinity;
  for (const t of times) {
    const [th, tm] = t.split(":").map(Number);
    if (!Number.isFinite(th) || !Number.isFinite(tm)) continue;
    const target = th * 60 + tm;
    // Circular distance, so 23:58 vs 00:01 is 3 minutes.
    const raw = Math.abs(target - actual);
    const delta = Math.min(raw, 1440 - raw);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = t;
    }
  }
  // Only snap within the jitter band.
  if (best === undefined || bestDelta > 5) return iso;

  const [bh, bm] = best.split(":").map(Number);
  const snapped = new Date(d.getTime() + ((bh * 60 + bm) - actual) * 60_000);
  return snapped.toISOString();
}

function formatDeviceTime(iso: string | undefined, tz: string): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: tz || undefined, dateStyle: "medium", timeStyle: "short",
    }).format(d);
  } catch {
    return d.toLocaleString();
  }
}

/** "09:00" / "09:00, 13:00 and 17:00" — empty when the cadence has no time. */
function timesLabel(cadence: ScheduleCadence): string {
  const times = resolveCadenceTimes(cadence);
  if (times.length === 0) return "";
  if (times.length === 1) return times[0];
  return `${times.slice(0, -1).join(", ")} and ${times[times.length - 1]}`;
}

function cadenceSummary(cadence: ScheduleCadence, tz: string): string {
  const at = timesLabel(cadence);
  switch (cadence.repeat) {
    case "daily":
      return at ? `Daily at ${at}` : "Daily";
    case "weekly": {
      const days = (cadence.days ?? []).map(weekdayLabel).join(", ");
      const suffix = at ? ` at ${at}` : "";
      return days ? `Weekly on ${days}${suffix}` : `Weekly${suffix}`;
    }
    case "monthly": {
      return cadence.day_of_month
        ? `Monthly on the ${ordinal(cadence.day_of_month)}${at ? ` at ${at}` : ""}`
        : `Monthly${at ? ` at ${at}` : ""}`;
    }
    case "interval":
      return cadence.every_ms ? `Every ${formatMs(cadence.every_ms)}` : "Interval";
    case "once": {
      const at = formatDeviceTime(cadence.at, tz);
      return at ? `Once, at ${at}` : "Once";
    }
    case "manual":
      return "Manual only — no automatic schedule";
    default:
      return cadence.repeat;
  }
}

export function ScheduledSection({ active }: { active: boolean }) {
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [timezone, setTimezone] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const r = await listSchedules();
      setSchedules(r.schedules);
      setTimezone(r.timezone);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load schedules.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // ?new=1 opens the create form once; the URL is rewritten so a reload does not reopen it.
  useEffect(() => {
    if (!active) return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("new") !== "1") return;
    setEditing("new");
    params.delete("new");
    const qs = params.toString();
    window.history.replaceState(
      null, "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
  }, [active]);

  // Poll only while a proposal is pending.
  const hasPending = schedules.some((s) => s.pending);
  useEffect(() => {
    if (!hasPending) return;
    const t = setInterval(() => { void refresh(); }, 3000);
    return () => clearInterval(t);
  }, [hasPending, refresh]);

  async function handleSave(draft: ScheduleDraft) {
    setSaving(true);
    try {
      if (editing === "new") {
        await createSchedule(bodyFromDraft(draft));
        toast.success("Task queued — it will run once the app confirms it.");
      } else if (editing) {
        await updateSchedule(editing, bodyFromDraft(draft));
        toast.success("Change queued.");
      }
      setEditing(null);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(sch: ScheduleItem) {
    if (!window.confirm(`Delete "${sch.name}"? It keeps running until the app confirms the removal.`)) {
      return;
    }
    try {
      await deleteSchedule(sch.id);
      toast.success("Removal queued.");
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete.");
    }
  }

  // Pause/resume is an ordinary update of `enabled`
  async function handleToggleEnabled(sch: ScheduleItem) {
    try {
      await updateSchedule(sch.id, { enabled: !sch.enabled });
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update.");
    }
  }

  async function handleRunNow(sch: ScheduleItem) {
    setRunning(sch.id);
    try {
      const result = await runScheduleNow(sch.id);
      setSchedules((prev) => prev.map((s) => s.id === sch.id
        ? { ...s, last_run_at: result.started_at, last_run_status: result.status, last_run_summary: result.summary }
        : s));
      if (result.status === "success") {
        toast.success(`Ran "${sch.name}".`);
      } else if (result.status === "skipped") {
        toast.warning(skippedRunMessage(sch.name, result.summary));
      } else {
        toast.error(`"${sch.name}" failed: ${result.summary}`);
      }
    } catch (err) {
      const status = (err as { status?: number } | undefined)?.status;
      const msg = status === 409
        ? "Agent is busy right now — try again shortly."
        : err instanceof Error ? err.message : "Failed to run.";
      toast.error(msg);
    } finally {
      setRunning(null);
    }
  }

  const BTN: React.CSSProperties = {
    padding: "6px 14px", borderRadius: 8, fontSize: 13, fontWeight: 500,
    cursor: "pointer", border: `1px solid ${C.border}`, background: C.surface,
    color: C.text,
  };

  return (
    <SectionCard id="scheduled" title="Scheduled" icon={<CalendarClock size={17} />} active={active}>
      <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
        Tasks this device runs on a schedule. Changes made here are sent to the
        Autonomous app for confirmation, so a new task starts running once it
        syncs. "Run now" fires a task immediately without changing its schedule.
      </div>

      {editing !== "new" && (
        <button
          type="button"
          onClick={() => setEditing("new")}
          style={{
            ...BTN, display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 12,
          }}
        >
          <Plus size={14} /> New task
        </button>
      )}

      {editing === "new" && (
        <ScheduleEditor
          initial={draftFromSchedule()}
          saving={saving}
          onSave={handleSave}
          onCancel={() => setEditing(null)}
        />
      )}

      {loading ? (
        <div style={{ fontSize: 12, color: C.textDim }}>Loading…</div>
      ) : error ? (
        <div style={{ fontSize: 12, color: C.red }}>{error}</div>
      ) : schedules.length === 0 ? (
        editing !== "new" && (
          <div style={{ fontSize: 12, color: C.textDim }}>
            No scheduled tasks yet. Create one here, or in the Autonomous app.
          </div>
        )
      ) : (
        schedules.map((sch) => {
          if (editing === sch.id) {
            return (
              <ScheduleEditor
                key={sch.id}
                initial={draftFromSchedule(sch)}
                saving={saving}
                onSave={handleSave}
                onCancel={() => setEditing(null)}
              />
            );
          }
          const lastRun = formatDeviceTime(sch.last_run_at, timezone) ?? "Never";
          const lastRunView = describeLastRun(sch.last_run_status, sch.last_run_summary);
          // A pending create is not armed yet, so it has no next run.
          const nextRun = sch.pending === "create"
            ? "Waiting to sync"
            : !sch.enabled
              ? "Paused"
              : formatDeviceTime(snapToScheduledTime(sch.next_run_at, sch.schedule, timezone), timezone) ??
                "Not scheduled";
          const isRunning = running === sch.id;
          const isPending = Boolean(sch.pending);
          return (
            <div
              key={sch.id}
              style={{
                padding: "12px 14px", marginBottom: 8,
                background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8,
              }}
            >
              <div className="lm-settings-list-row" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: C.text, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                    <span className="lm-settings-list-name">{sch.name}</span>
                    <span style={{
                      fontSize: 12, fontWeight: 600, padding: "1px 7px", borderRadius: 999,
                      border: `1px solid ${C.border}`,
                      color: sch.enabled ? C.green : C.textMuted,
                      background: sch.enabled ? "var(--lm-green-dim)" : "transparent",
                    }}>
                      {sch.enabled ? "Enabled" : "Paused"}
                    </span>
                    {resolveScheduleKind(sch.kind) === "speak" && (
                      <span
                        title="Spoken out loud, word for word — no agent turn"
                        style={{
                          fontSize: 12, fontWeight: 600, padding: "1px 7px", borderRadius: 999,
                          border: `1px solid ${C.border}`, color: C.amber,
                        }}
                      >
                        Speaks
                      </span>
                    )}
                    {sch.pending && (
                      <span style={{
                        fontSize: 12, fontWeight: 600, padding: "1px 7px", borderRadius: 999,
                        border: `1px solid ${C.border}`, color: C.textDim,
                      }}>
                        {sch.pending === "delete" ? "Removing…" : "Syncing…"}
                      </span>
                    )}
                  </div>
                  {sch.instructions && (
                    <div style={{
                      fontSize: 12, color: C.textDim, marginTop: 2,
                      overflowWrap: "anywhere", display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 3, overflow: "hidden",
                    }}>
                      {sch.instructions}
                    </div>
                  )}
                </div>
                <div className="lm-settings-list-actions">
                  <button
                    type="button"
                    aria-label={`Run ${sch.name} now`}
                    onClick={() => handleRunNow(sch)}
                    disabled={isRunning || sch.pending === "create"}
                    style={{
                      ...BTN,
                      opacity: isRunning || sch.pending === "create" ? 0.4 : 1,
                      cursor: isRunning || sch.pending === "create" ? "not-allowed" : "pointer",
                    }}
                  >
                    {isRunning ? "Running…" : "Run now"}
                  </button>
                  <button
                    type="button"
                    title={sch.enabled ? "Pause" : "Resume"}
                    onClick={() => handleToggleEnabled(sch)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 10px", opacity: isPending ? 0.4 : 1 }}
                  >
                    {sch.enabled ? "Pause" : "Resume"}
                  </button>
                  <button
                    type="button"
                    title="Edit"
                    aria-label={`Edit ${sch.name}`}
                    onClick={() => setEditing(sch.id)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 9px", opacity: isPending ? 0.4 : 1 }}
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    type="button"
                    title="Delete"
                    aria-label={`Delete ${sch.name}`}
                    onClick={() => handleDelete(sch)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 9px", color: C.red, opacity: isPending ? 0.4 : 1 }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>

              <div style={{ fontSize: 12, color: C.textDim, marginTop: 8 }}>
                {cadenceSummary(sch.schedule, timezone)}
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: 14, fontSize: 12, color: C.textDim, marginTop: 6 }}>
                <span>Next run: <span style={{ color: C.text }}>{nextRun}</span></span>
                <span>
                  Last run: <span style={{ color: C.text }}>{lastRun}</span>
                  {lastRunView && (
                    <span style={{ color: LAST_RUN_TONE_COLOR[lastRunView.tone], marginLeft: 6 }}>
                      {lastRunView.label}
                    </span>
                  )}
                </span>
              </div>
            </div>
          );
        })
      )}
    </SectionCard>
  );
}
