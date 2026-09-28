import { MAX_SPEAK_CHARS, MAX_TIMES_PER_SCHEDULE, resolveCadenceTimes, resolveScheduleKind } from "@/lib/api";
import type { ScheduleCadence, ScheduleItem, ScheduleKind, ScheduleWriteBody } from "@/lib/api";

// Draft state + wire mapping for one scheduled task. Kept out of
// ScheduleEditor.tsx so that file exports components only
// (react-refresh/only-export-components).
//
// It mirrors the cadence rules the DEVICE enforces (system/schedule/intent.go's
// ValidateSpec) rather than inventing its own, so a form that submits is one
// the device will accept — the alternative is a round trip that fails with a
// message the user cannot act on.

// Matches minInterval in system/schedule/spec.go. The device floors anything
// shorter, so offering a smaller option than this in the editor would silently
// not do what it says.
const MIN_INTERVAL_MINUTES = 5;

/** The cadence kinds the device accepts. Kept as a union (not string) so a
 *  typo in the form cannot produce a repeat the runner will refuse. */
export type ScheduleRepeat = ScheduleCadence["repeat"];

export type ScheduleDraft = {
  name: string;
  instructions: string;
  kind: ScheduleKind;
  enabled: boolean;
  repeat: ScheduleRepeat;
  /** Every fire time for daily/weekly/monthly. Never empty — a cadence with no time cannot fire. */
  times: string[];
  days: number[];
  dayOfMonth: number;
  everyMs: number;
};

export function draftFromSchedule(sch?: ScheduleItem): ScheduleDraft {
  const cadence = sch?.schedule;
  return {
    name: sch?.name ?? "",
    instructions: sch?.instructions ?? "",
    // Absent resolves to agent, so a task authored before this field opens on
    // the behaviour it actually has rather than on a blank third state.
    kind: resolveScheduleKind(sch?.kind),
    enabled: sch?.enabled ?? true,
    repeat: cadence?.repeat ?? "daily",
    // Falls back to the single `time` for a schedule authored before the list.
    times: (() => {
      const resolved = resolveCadenceTimes(cadence);
      return resolved.length > 0 ? resolved : ["08:00"];
    })(),
    // 7 is an accepted alias for Sunday on the wire; normalise so the chip
    // toggles compare equal.
    days: (cadence?.days ?? [1, 2, 3, 4, 5]).map((d) => (d === 7 ? 0 : d)),
    dayOfMonth: cadence?.day_of_month ?? 1,
    everyMs: cadence?.every_ms ?? 60 * 60_000,
  };
}

/** Builds the wire cadence, emitting ONLY the fields the chosen repeat uses —
 *  the same "repeat selects which fields matter" rule the device applies. */
/** Sorted + deduped, so `time` is deterministic and matches what the backend
 *  will normalise this to — the form submits the value it will read back. */
function times(d: ScheduleDraft): string[] {
  return [...new Set(d.times)].sort();
}

function cadenceFromDraft(d: ScheduleDraft): ScheduleCadence {
  switch (d.repeat) {
    case "daily":
      return { repeat: "daily", time: times(d)[0], times: times(d) };
    case "weekly":
      return { repeat: "weekly", time: times(d)[0], times: times(d), days: d.days };
    case "monthly":
      return { repeat: "monthly", time: times(d)[0], times: times(d), day_of_month: d.dayOfMonth };
    case "interval":
      return { repeat: "interval", every_ms: d.everyMs };
    default:
      return { repeat: d.repeat };
  }
}

export function bodyFromDraft(d: ScheduleDraft): ScheduleWriteBody {
  return {
    name: d.name.trim(),
    instructions: d.instructions.trim(),
    kind: d.kind,
    enabled: d.enabled,
    schedule: cadenceFromDraft(d),
  };
}

/** Returns a user-facing reason the draft cannot be saved, or null. */
export function validateDraft(d: ScheduleDraft): string | null {
  if (!d.name.trim()) return "Give the task a name.";
  if (!d.instructions.trim()) {
    return d.kind === "speak" ? "Type what the robot should say." : "Tell the robot what to do.";
  }
  // Mirrors ValidateIntentPayload in system/schedule/intent.go, so the form
  // refuses what the device would refuse instead of queueing a doomed intent.
  if (d.kind === "speak") {
    const n = [...d.instructions.trim()].length;
    if (n > MAX_SPEAK_CHARS) {
      return `Spoken text must be at most ${MAX_SPEAK_CHARS} characters, got ${n}.`;
    }
  }
  if (["daily", "weekly", "monthly"].includes(d.repeat)) {
    if (d.times.length === 0) return "Add at least one time.";
    if (d.times.length > MAX_TIMES_PER_SCHEDULE) {
      return `A task can have at most ${MAX_TIMES_PER_SCHEDULE} times.`;
    }
    if (new Set(d.times).size !== d.times.length) return "Each time can only be listed once.";
    if (d.times.some((t) => !/^\d{2}:\d{2}$/.test(t))) return "Pick a valid time.";
  }
  if (d.repeat === "weekly" && d.days.length === 0) return "Pick at least one day.";
  if (d.repeat === "monthly" && (d.dayOfMonth < 1 || d.dayOfMonth > 31)) {
    return "Day of month must be between 1 and 31.";
  }
  if (d.repeat === "interval" && d.everyMs < MIN_INTERVAL_MINUTES * 60_000) {
    return `Interval must be at least ${MIN_INTERVAL_MINUTES} minutes.`;
  }
  return null;
}
