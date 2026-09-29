import { useCallback, useEffect, useState } from "react";
import { S } from "./styles";
import { HW } from "./types";

type Source = "mood" | "wellbeing" | "music-suggestions" | "posture";

const POSTURE_ICONS: Record<string, { icon: string; title: string }> = {
  posture_alert:         { icon: "🪑",  title: "Posture alert" },
  nudge_posture:         { icon: "🔔",  title: "Nudge: posture" },
  praise_posture:        { icon: "👍",  title: "Praise: posture" },
  morning_recap_posture: { icon: "🌅",  title: "Morning recap (posture)" },
  evening_recap_posture: { icon: "🌙",  title: "Evening recap (posture)" },
  weekly_recap_posture:  { icon: "📊",  title: "Weekly recap (posture)" },
};

interface TimelineEntry {
  ts: number;
  source: Source;
  icon: string;
  color: string;
  title: string;
  detail: string;
}

interface Props {
  user: string;
  onClose: () => void;
}

const WELLBEING_ICONS: Record<string, { icon: string; title: string }> = {
  drink: { icon: "💧", title: "Drink" },
  break: { icon: "🧘", title: "Break" },
  celebrate: { icon: "🎉", title: "Celebrate" },
  sedentary: { icon: "💺", title: "Sedentary" },
  emotional: { icon: "✨", title: "Emotional" },
  enter: { icon: "👋", title: "Entered" },
  leave: { icon: "👋", title: "Left" },
  nudge_hydration: { icon: "🔔", title: "Nudged: drink" },
  nudge_break: { icon: "🔔", title: "Nudged: break" },
  morning_greeting: { icon: "🌅", title: "Morning greeting" },
  sleep_winddown: { icon: "🌙", title: "Sleep wind-down" },
  meal_reminder: { icon: "🍽", title: "Meal reminder" },
  noted_yawn: { icon: "🥱", title: "Noted: yawn" },
};

const RAW_LABEL_ICON: Record<string, string> = {
  "drinking": "💧",
  "drinking beer": "🍺",
  "drinking shots": "🥃",
  "tasting beer": "🍺",
  "opening bottle": "🍾",
  "making tea": "🍵",
  "tasting food": "🍴",
  "stretching arm": "💪",
  "stretching leg": "🦵",
  "dining": "🍽",
  "eating burger": "🍔",
  "eating cake": "🍰",
  "eating carrots": "🥕",
  "eating chips": "🍟",
  "eating doughnuts": "🍩",
  "eating hotdog": "🌭",
  "eating ice cream": "🍦",
  "eating spaghetti": "🍝",
  "eating watermelon": "🍉",
  "applauding": "👏",
  "clapping": "👏",
  "celebrating": "🎉",
  "sneezing": "🤧",
  "sniffing": "👃",
  "hugging": "🤗",
  "kissing": "😘",
  "headbanging": "🤘",
  "sticking tongue out": "😛",
  "using computer": "💻",
  "writing": "✍️",
  "texting": "📱",
  "reading": "📖",
  "reading book": "📖",
  "reading newspaper": "📰",
  "drawing": "🎨",
  "playing controller": "🎮",
  "yawning": "🥱",
};

function rawLabelIcon(notes: string, fallback: string): string {
  for (const part of notes.split(",")) {
    const key = part.trim().toLowerCase();
    if (RAW_LABEL_ICON[key]) return RAW_LABEL_ICON[key];
  }
  return fallback;
}

function todayLocal(): string {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function fmtTime(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  return d.toTimeString().slice(0, 8);
}

function parseJsonl(text: string): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      rows.push(JSON.parse(t));
    } catch {
      /* ignore malformed line */
    }
  }
  return rows;
}

export function UserTimelineModal({ user, onClose }: Props) {
  const [date, setDate] = useState(todayLocal());
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchFile = useCallback(async (sub: string, filename: string): Promise<string | null> => {
    try {
      const res = await fetch(`${HW}/face/file/${user}/${sub}/${filename}`);
      if (!res.ok) return null;
      return await res.text();
    } catch {
      return null;
    }
  }, [user]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const file = `${date}.jsonl`;
    const merged: TimelineEntry[] = [];

    const [moodText, wellbeingText, musicText, postureText] = await Promise.all([
      fetchFile("mood", file),
      fetchFile("wellbeing", file),
      fetchFile("music-suggestions", file),
      fetchFile("posture", file),
    ]);

    if (moodText) {
      for (const r of parseJsonl(moodText)) {
        const ts = Number(r.ts) || 0;
        const kind = String(r.kind || "");
        const mood = String(r.mood || "");
        const source = String(r.source || "");
        const trigger = String(r.trigger || "");
        if (kind === "signal") {
          merged.push({
            ts,
            source: "mood",
            icon: "🫧",
            color: "rgb(74,222,128)",
            title: `Signal: ${mood}`,
            detail: `${source}${trigger ? ` · ${trigger}` : ""}`,
          });
        } else if (kind === "decision") {
          const reasoning = String(r.reasoning || "");
          merged.push({
            ts,
            source: "mood",
            icon: "🎯",
            color: "rgb(34,197,94)",
            title: `Decision: ${mood}`,
            detail: reasoning || String(r.based_on || ""),
          });
        }
      }
    }

    if (wellbeingText) {
      const BUCKET_WITH_RAW = new Set(["sedentary", "drink", "break"]);
      for (const r of parseJsonl(wellbeingText)) {
        const ts = Number(r.ts) || 0;
        const action = String(r.action || "");
        const notes = String(r.notes || "");
        const bucketMeta = WELLBEING_ICONS[action];
        const rawIcon = RAW_LABEL_ICON[action.toLowerCase()];
        const AGENT_WRITTEN_NUDGES = new Set(["morning_greeting", "sleep_winddown", "meal_reminder"]);
        const isAgentNudge = action.startsWith("nudge_") || AGENT_WRITTEN_NUDGES.has(action);
        const color = isAgentNudge ? "rgb(251,146,60)" : "rgb(96,165,250)";

        if (!bucketMeta && rawIcon) {
          merged.push({
            ts,
            source: "wellbeing",
            icon: rawIcon,
            color,
            title: action,
            detail: notes,
          });
        } else if (bucketMeta) {
          const legacyRawAsTitle = BUCKET_WITH_RAW.has(action) && notes !== "";
          merged.push({
            ts,
            source: "wellbeing",
            icon: legacyRawAsTitle ? rawLabelIcon(notes, bucketMeta.icon) : bucketMeta.icon,
            color,
            title: legacyRawAsTitle ? notes : bucketMeta.title,
            detail: legacyRawAsTitle ? "" : notes,
          });
        } else {
          merged.push({
            ts,
            source: "wellbeing",
            icon: "•",
            color,
            title: action,
            detail: notes,
          });
        }
      }
    }

    if (musicText) {
      for (const r of parseJsonl(musicText)) {
        const ts = Number(r.ts) || 0;
        const trigger = String(r.trigger || "");
        const message = String(r.message || "");
        const status = String(r.status || "");
        merged.push({
          ts,
          source: "music-suggestions",
          icon: "🎵",
          color: "rgb(168,85,247)",
          title: `Music suggested${status ? ` (${status})` : ""}`,
          detail: `${trigger ? `[${trigger}] ` : ""}${message}`,
        });
      }
    }

    if (postureText) {
      for (const r of parseJsonl(postureText)) {
        const ts = Number(r.ts) || 0;
        const action = String(r.action || "");
        const notes = String(r.notes || "");
        const score = Number(r.score) || 0;
        const risk = String(r.risk || "");
        const level = Number(r.nudge_level) || 0;
        const meta = POSTURE_ICONS[action];
        const isAgentNudge = action === "nudge_posture" || action === "praise_posture" ||
          action === "morning_recap_posture" || action === "evening_recap_posture" ||
          action === "weekly_recap_posture";
        const color = isAgentNudge ? "rgb(251,146,60)" : "rgb(6,182,212)";

        const title = meta
          ? action === "posture_alert" && risk
            ? `${meta.title} · ${risk}${score ? ` (${score})` : ""}`
            : action === "nudge_posture" && level
              ? `${meta.title} · L${level}`
              : meta.title
          : action;
        merged.push({
          ts,
          source: "posture",
          icon: meta?.icon ?? "🪑",
          color,
          title,
          detail: notes,
        });
      }
    }

    merged.sort((a, b) => a.ts - b.ts);
    setEntries(merged);
    setLoading(false);
    if (merged.length === 0) {
      setError("No entries for this date.");
    }
  }, [fetchFile, date]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-open: `load` flips the loading flag before awaiting the timeline files. That is an effect by definition (network I/O on mount and on date change), not derivable state.
    load();
  }, [load]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        zIndex: 1000,
        padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          ...S.card,
          width: "min(800px, 100%)",
          maxHeight: "90vh",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--lm-amber)", textTransform: "capitalize" }}>
              {user}'s timeline
            </div>
            <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginTop: 2 }}>
              mood + wellbeing + music-suggestions + posture, merged chronologically
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              style={{
                fontSize: 12,
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid var(--lm-border)",
                background: "var(--lm-surface)",
                color: "var(--lm-text)",
              }}
            />
            <button
              onClick={onClose}
              style={{
                fontSize: 11,
                padding: "4px 10px",
                borderRadius: 6,
                border: "1px solid var(--lm-border)",
                background: "var(--lm-surface)",
                color: "var(--lm-text-dim)",
                cursor: "pointer",
              }}
            >
              Close
            </button>
          </div>
        </div>

        <div
          style={{
            flex: 1,
            overflowY: "auto",
            borderTop: "1px solid var(--lm-border)",
            paddingTop: 8,
          }}
        >
          {loading && (
            <div style={{ fontSize: 12, color: "var(--lm-text-muted)", textAlign: "center", padding: 20 }}>
              Loading…
            </div>
          )}
          {!loading && error && (
            <div style={{ fontSize: 12, color: "var(--lm-text-muted)", textAlign: "center", padding: 20, fontStyle: "italic" }}>
              {error}
            </div>
          )}
          {!loading && !error && entries.map((e, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                gap: 10,
                padding: "6px 4px",
                borderBottom: "1px solid var(--lm-border)",
                alignItems: "flex-start",
              }}
            >
              <div style={{ fontSize: 16, width: 24, textAlign: "center" }}>{e.icon}</div>
              <div style={{
                fontFamily: "monospace",
                fontSize: 11,
                color: "var(--lm-text-muted)",
                width: 64,
                flexShrink: 0,
                paddingTop: 1,
              }}>
                {fmtTime(e.ts)}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: e.color }}>
                  {e.title}
                </div>
                {e.detail && (
                  <div style={{ fontSize: 11, color: "var(--lm-text-muted)", marginTop: 2, wordBreak: "break-word" }}>
                    {e.detail}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        <div style={{ fontSize: 10, color: "var(--lm-text-muted)", textAlign: "right" }}>
          {entries.length} entr{entries.length === 1 ? "y" : "ies"}
        </div>
      </div>
    </div>
  );
}
