import { useState } from "react";
import { C } from "@/components/setup/shared";
import { validateDraft } from "./scheduleDraft";
import type { ScheduleDraft, ScheduleRepeat } from "./scheduleDraft";
import { MAX_SPEAK_CHARS, MAX_TIMES_PER_SCHEDULE } from "@/lib/api";
import type { ScheduleKind } from "@/lib/api";

// The create/edit form for a scheduled task on the device itself.
//
// Split out of ScheduledSection so the list stays readable: the list renders
// rows and owns fetching, this owns one task's draft state and validation.
//
// It mirrors the cadence rules the DEVICE enforces (system/schedule/intent.go's
// ValidateSpec) rather than inventing its own, so a form that submits is one
// the device will accept — the alternative is a round trip that fails with a
// message the user cannot act on.

const WEEKDAYS = [
  { value: 1, label: "Mon" }, { value: 2, label: "Tue" }, { value: 3, label: "Wed" },
  { value: 4, label: "Thu" }, { value: 5, label: "Fri" }, { value: 6, label: "Sat" },
  { value: 0, label: "Sun" },
];

const INTERVAL_CHOICES = [
  { ms: 15 * 60_000, label: "15 minutes" },
  { ms: 30 * 60_000, label: "30 minutes" },
  { ms: 60 * 60_000, label: "1 hour" },
  { ms: 4 * 60 * 60_000, label: "4 hours" },
  { ms: 12 * 60 * 60_000, label: "12 hours" },
];

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "7px 10px", borderRadius: 7, fontSize: 13,
  background: C.bg, border: `1px solid ${C.border}`, color: C.text,
  outline: "none", boxSizing: "border-box",
};

const smallBtnStyle: React.CSSProperties = {
  padding: "6px 10px", borderRadius: 7, fontSize: 12,
  background: "transparent", border: `1px solid ${C.border}`, color: C.textDim,
  cursor: "pointer",
};

const labelStyle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, color: C.textDim, marginBottom: 4, display: "block",
};

export function ScheduleEditor({
  initial, saving, onSave, onCancel,
}: {
  initial: ScheduleDraft;
  saving: boolean;
  onSave: (draft: ScheduleDraft) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<ScheduleDraft>(initial);
  const [touched, setTouched] = useState(false);
  const problem = validateDraft(draft);

  const speaking = draft.kind === "speak";
  // Code points, not UTF-16 units, so an emoji counts as the single character
  // the device's utf8.RuneCountInString will also count.
  const spokenLength = speaking ? [...draft.instructions].length : 0;
  const overSpeakLimit = spokenLength > MAX_SPEAK_CHARS;

  const set = <K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const setTimeAt = (i: number, value: string) =>
    setDraft((d) => ({ ...d, times: d.times.map((t, idx) => (idx === i ? value : t)) }));

  const removeTimeAt = (i: number) =>
    setDraft((d) => ({ ...d, times: d.times.filter((_, idx) => idx !== i) }));

  // Seeds an hour after the last row, so adding several does not pile up
  // duplicates the user then has to correct one by one.
  const addTime = () =>
    setDraft((d) => {
      const last = d.times[d.times.length - 1] ?? "08:00";
      const [h, m] = last.split(":").map(Number);
      const next = `${String((Number.isFinite(h) ? h + 1 : 9) % 24).padStart(2, "0")}:${String(
        Number.isFinite(m) ? m : 0,
      ).padStart(2, "0")}`;
      if (d.times.includes(next)) return d;
      return { ...d, times: [...d.times, next] };
    });

  const toggleDay = (day: number) =>
    setDraft((d) => ({
      ...d,
      days: d.days.includes(day) ? d.days.filter((x) => x !== day) : [...d.days, day].sort(),
    }));

  const submit = () => {
    setTouched(true);
    if (problem) return;
    onSave(draft);
  };

  return (
    <div style={{
      padding: 14, marginBottom: 8, borderRadius: 8,
      background: C.surface, border: `1px solid ${C.border}`,
    }}>
      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Name</label>
        <input
          style={inputStyle}
          value={draft.name}
          placeholder="Daily briefing"
          onChange={(e) => set("name", e.target.value)}
        />
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>When it runs, the robot will</label>
        <select
          style={inputStyle}
          value={draft.kind}
          onChange={(e) => set("kind", e.target.value as ScheduleKind)}
        >
          <option value="agent">Ask the agent</option>
          <option value="speak">Speak this text (less cost)</option>
        </select>
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>{speaking ? "What to say" : "Instructions"}</label>
        <textarea
          style={{ ...inputStyle, minHeight: 64, resize: "vertical", fontFamily: "inherit" }}
          value={draft.instructions}
          placeholder={speaking
            ? "Time to drink some water."
            : "Summarize my calendar, unread email, and messages for today."}
          onChange={(e) => set("instructions", e.target.value)}
        />
        <div style={{ fontSize: 11, color: overSpeakLimit ? C.red : C.textDim, marginTop: 4 }}>
          {speaking
            ? <>Spoken out loud word for word. No agent turn, so it costs less than an agent task. <span style={{ fontVariantNumeric: "tabular-nums" }}>{spokenLength}/{MAX_SPEAK_CHARS}</span></>
            : "The agent reads this as a prompt and decides what to say or do."}
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <div style={{ flex: "1 1 150px" }}>
          <label style={labelStyle}>Frequency</label>
          <select
            style={inputStyle}
            value={draft.repeat}
            onChange={(e) => set("repeat", e.target.value as ScheduleRepeat)}
          >
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="interval">Every…</option>
            <option value="manual">Manual only</option>
          </select>
        </div>

        {["daily", "weekly", "monthly"].includes(draft.repeat) && (
          <div style={{ flex: "1 1 200px" }}>
            <label style={labelStyle}>{draft.times.length === 1 ? "Time" : "Times"}</label>
            {draft.times.map((t, i) => (
              <div key={i} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                <input
                  type="time"
                  style={{ ...inputStyle, flex: "0 0 120px" }}
                  value={t}
                  onChange={(e) => setTimeAt(i, e.target.value)}
                />
                {/* No remove on the last row: a cadence with no time cannot fire. */}
                {draft.times.length > 1 && (
                  <button type="button" style={smallBtnStyle} onClick={() => removeTimeAt(i)}>
                    Remove
                  </button>
                )}
              </div>
            ))}
            {draft.times.length < MAX_TIMES_PER_SCHEDULE && (
              <button type="button" style={smallBtnStyle} onClick={addTime}>
                + Add time
              </button>
            )}
          </div>
        )}

        {draft.repeat === "monthly" && (
          <div style={{ flex: "0 0 110px" }}>
            <label style={labelStyle}>Day of month</label>
            <input
              type="number" min={1} max={31}
              style={inputStyle}
              value={draft.dayOfMonth}
              onChange={(e) => set("dayOfMonth", Number(e.target.value))}
            />
          </div>
        )}

        {draft.repeat === "interval" && (
          <div style={{ flex: "1 1 150px" }}>
            <label style={labelStyle}>Every</label>
            <select
              style={inputStyle}
              value={draft.everyMs}
              onChange={(e) => set("everyMs", Number(e.target.value))}
            >
              {INTERVAL_CHOICES.map((c) => (
                <option key={c.ms} value={c.ms}>{c.label}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {draft.repeat === "weekly" && (
        <div style={{ marginBottom: 10 }}>
          <label style={labelStyle}>Days</label>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {WEEKDAYS.map((d) => {
              const on = draft.days.includes(d.value);
              return (
                <button
                  key={d.value}
                  type="button"
                  onClick={() => toggleDay(d.value)}
                  style={{
                    padding: "5px 11px", borderRadius: 999, fontSize: 12, cursor: "pointer",
                    border: `1px solid ${on ? C.green : C.border}`,
                    background: on ? "var(--lm-green-dim)" : "transparent",
                    color: on ? C.green : C.textDim,
                  }}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: C.textDim, cursor: "pointer", marginBottom: 12 }}>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => set("enabled", e.target.checked)}
        />
        Active
      </label>

      {touched && problem && (
        <div style={{ fontSize: 12, color: C.red, marginBottom: 10 }}>{problem}</div>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          onClick={submit}
          disabled={saving}
          style={{
            padding: "7px 16px", borderRadius: 8, fontSize: 13, fontWeight: 600,
            border: "none", background: C.green, color: "#04120a",
            cursor: saving ? "not-allowed" : "pointer", opacity: saving ? 0.6 : 1,
          }}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          style={{
            padding: "7px 16px", borderRadius: 8, fontSize: 13,
            border: `1px solid ${C.border}`, background: "transparent", color: C.textDim,
            cursor: "pointer",
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
