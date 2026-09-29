import { useEffect } from "react";
import {
  Mic, Eye, Hand, MessageSquare, Monitor, Clock, Settings,
  PauseCircle, Search, SlidersHorizontal, X, RotateCcw, Circle, ArrowRight,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { TYPE_LUCIDE, TYPE_LABEL } from "./types";

export type SortKey =
  | "newest" | "oldest" | "time_desc" | "time_asc" | "tokens_desc" | "tokens_asc";

// "HH:MM" for `d` shifted back `minsAgo` minutes, in local time (the turn timestamps the range filters against are local HH:MM, so presets must be too).
function hhmmAgo(minsAgo: number): string {
  const d = new Date(Date.now() - minsAgo * 60_000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Preset matching tolerates the minute having ticked since selection.
const within = (a: string, b: string) => {
  if (!a || !b) return false;
  const toMin = (s: string) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
  return Math.abs(toMin(a) - toMin(b)) <= 1;
};
const TIME_PRESETS: {
  label: string;
  range: () => [string, string];
  test: (from: string, to: string) => boolean;
}[] = [
  { label: "Last 15m", range: () => [hhmmAgo(15), hhmmAgo(0)], test: (f, t) => within(f, hhmmAgo(15)) && within(t, hhmmAgo(0)) },
  { label: "Last 1h",  range: () => [hhmmAgo(60), hhmmAgo(0)], test: (f, t) => within(f, hhmmAgo(60)) && within(t, hhmmAgo(0)) },
  { label: "Last 6h",  range: () => [hhmmAgo(360), hhmmAgo(0)], test: (f, t) => within(f, hhmmAgo(360)) && within(t, hhmmAgo(0)) },
  { label: "Today",    range: () => ["00:00", hhmmAgo(0)], test: (f, t) => f === "00:00" && within(t, hhmmAgo(0)) },
];

// A single labeled, clock-prefixed time field.
function TimeField({ label, value, onChange }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const set = Boolean(value);
  return (
    <label
      className="lm-time-field"
      style={{
        flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 7,
        padding: "6px 10px", borderRadius: 9, cursor: "text",
        background: set ? "var(--lm-amber-dim)" : "var(--lm-surface)",
        border: `1px solid ${set ? "color-mix(in srgb, var(--lm-amber) 55%, transparent)" : "var(--lm-border)"}`,
        transition: "border-color 0.15s, background 0.15s",
      }}
    >
      <Clock size={14} strokeWidth={2} style={{ flexShrink: 0, color: set ? "var(--lm-amber)" : "var(--lm-text-muted)" }} />
      <span style={{
        fontSize: 9, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase",
        color: "var(--lm-text-muted)", flexShrink: 0,
      }}>{label}</span>
      <input
        type="time"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="lm-time-input"
        style={{
          flex: 1, minWidth: 0, border: "none", background: "transparent", outline: "none",
          fontSize: 12, fontFamily: "inherit",
          color: set ? "var(--lm-text)" : "var(--lm-text-muted)",
        }}
      />
    </label>
  );
}

// Advanced Flow turn-list filters in a modal; all filter state is owned by the parent.
export function FiltersModal({
  onClose,
  searchText, setSearchText,
  excludedTypes, toggleType, toggleCategory,
  availableTypes, setExcludedTypes, saveExcluded,
  hasDropped,
  sortBy, setSortBy,
  fromTime, setFromTime, toTime, setToTime,
  onResetAll,
  activeFilters,
  catAvailability,
}: {
  onClose: () => void;
  searchText: string;
  setSearchText: (v: string) => void;
  excludedTypes: Set<string>;
  toggleType: (type: string) => void;
  toggleCategory: (cat: string) => void;
  availableTypes: string[];
  setExcludedTypes: React.Dispatch<React.SetStateAction<Set<string>>>;
  saveExcluded: (next: Set<string>) => void;
  hasDropped: boolean;
  sortBy: SortKey;
  setSortBy: (s: SortKey) => void;
  fromTime: string;
  setFromTime: (v: string) => void;
  toTime: string;
  setToTime: (v: string) => void;
  onResetAll: () => void;
  activeFilters: number;
  catAvailability: (cat: string) => { active: boolean; partial: boolean };
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sectionLabel: React.CSSProperties = {
    fontSize: 9, fontWeight: 700, color: "var(--lm-text-muted)",
    marginBottom: 7, textTransform: "uppercase", letterSpacing: "0.08em",
  };

  const CATS: { key: string; Icon: LucideIcon; label: string }[] = [
    { key: "mic", Icon: Mic, label: "Mic" },
    { key: "cam", Icon: Eye, label: "Cam" },
    { key: "button", Icon: Hand, label: "Btn" },
    { key: "channel", Icon: MessageSquare, label: "CH" },
    { key: "web", Icon: Monitor, label: "Web" },
    { key: "cron", Icon: Clock, label: "Cron" },
    { key: "system", Icon: Settings, label: "Sys" },
  ];

  const allOn = availableTypes.length > 0 && availableTypes.every((t) => !excludedTypes.has(t));
  const hasRange = Boolean(fromTime || toTime);

  return (
    <div
      id="FLOW_FILTERS_MODAL_OVERLAY" data-region="FLOW_FILTERS_MODAL_OVERLAY"
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 100,
        background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 16,
      }}
    >
      <div
        id="FLOW_FILTERS_MODAL_PANEL" data-region="FLOW_FILTERS_MODAL_PANEL"
        onClick={(e) => e.stopPropagation()}
        className="lm-filters-modal"
        style={{
          background: "var(--lm-card)", border: "1px solid var(--lm-border)",
          borderRadius: 16, width: "min(560px, 94vw)", maxHeight: "88vh",
          display: "flex", flexDirection: "column",
          boxShadow: "0 24px 64px -24px rgba(0,0,0,0.7)",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 10, padding: "16px 18px", borderBottom: "1px solid var(--lm-border)",
          flexShrink: 0,
        }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
            <SlidersHorizontal size={16} strokeWidth={2} style={{ color: "var(--lm-amber)" }} />
            <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-text)" }}>Filters</span>
            {activeFilters > 0 && (
              <span style={{
                fontSize: 10, fontWeight: 700, padding: "1px 7px", borderRadius: 999,
                background: "var(--lm-amber-dim)", color: "var(--lm-amber)",
                border: "1px solid color-mix(in srgb, var(--lm-amber) 45%, transparent)",
              }}>{activeFilters} active</span>
            )}
          </span>
          <button
            onClick={onClose}
            aria-label="Close"
            className="lm-u-btn"
            style={{
              width: 30, height: 30, borderRadius: 8, padding: 0,
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              color: "var(--lm-text-dim)",
            }}
          ><X size={16} strokeWidth={2} /></button>
        </div>

        <div style={{
          flex: 1, minHeight: 0, overflowY: "auto", padding: 18,
          display: "flex", flexDirection: "column", gap: 16,
        }}>
          <div>
            <div style={sectionLabel}>Search</div>
            <div style={{ position: "relative" }}>
              <Search
                size={14}
                strokeWidth={2}
                style={{
                  position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
                  color: "var(--lm-text-muted)", pointerEvents: "none",
                }}
              />
              <input
                type="text"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Search input / output…"
                className="lm-u-input"
                autoFocus
                style={{
                  width: "100%", boxSizing: "border-box",
                  padding: "8px 12px 8px 32px", borderRadius: 8, fontSize: 12,
                  outline: "none",
                }}
              />
            </div>
          </div>

          <div>
            <div style={sectionLabel}>Sources</div>
            <div style={{ display: "flex", gap: 6, rowGap: 6, flexWrap: "wrap" }}>
              {CATS.map((f) => {
                const { active, partial } = catAvailability(f.key);
                const border = active ? "var(--lm-amber)" : partial ? "var(--lm-teal)" : "var(--lm-border)";
                const color = active ? "var(--lm-amber)" : partial ? "var(--lm-teal)" : "var(--lm-text-muted)";
                const lit = active || partial;
                return (
                  <button key={f.key} onClick={() => toggleCategory(f.key)} className="lm-u-btn" style={{
                    padding: "5px 11px", borderRadius: 8, fontSize: 12, minHeight: 28,
                    border: `1px solid ${border}`,
                    background: active ? "var(--lm-amber-dim)" : partial ? "var(--lm-teal-dim)" : "transparent",
                    color, fontWeight: lit ? 600 : 500,
                    display: "inline-flex", alignItems: "center", gap: 5,
                  }}>
                    <f.Icon size={14} strokeWidth={2} style={{ opacity: lit ? 1 : 0.7 }} /> {f.label}
                  </button>
                );
              })}
              {hasDropped && (() => {
                const on = !excludedTypes.has("__dropped");
                return (
                  <button onClick={() => toggleType("__dropped")} className="lm-u-btn" style={{
                    padding: "5px 11px", borderRadius: 8, fontSize: 12, minHeight: 28,
                    border: `1px solid ${on ? "var(--lm-red)" : "var(--lm-border)"}`,
                    background: on ? "var(--lm-red-dim)" : "transparent",
                    color: on ? "var(--lm-red)" : "var(--lm-text-dim)",
                    fontWeight: on ? 600 : 500,
                    display: "inline-flex", alignItems: "center", gap: 5,
                  }}>
                    <PauseCircle size={14} strokeWidth={2} style={{ opacity: on ? 1 : 0.7 }} /> Dropped
                  </button>
                );
              })()}
            </div>
          </div>

          <div>
            <div style={sectionLabel}>Sort</div>
            <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
              {([
                { key: "newest", label: "Newest" },
                { key: "oldest", label: "Oldest" },
                { key: "time_desc", label: "Slowest" },
                { key: "time_asc", label: "Fastest" },
                { key: "tokens_desc", label: "↑ Tokens" },
                { key: "tokens_asc", label: "↓ Tokens" },
              ] as const).map((s) => (
                <button
                  key={s.key}
                  onClick={() => setSortBy(s.key)}
                  className="lm-u-btn"
                  style={{
                    padding: "5px 11px", borderRadius: 8, fontSize: 12,
                    border: `1px solid ${sortBy === s.key ? "var(--lm-amber)" : "var(--lm-border)"}`,
                    background: sortBy === s.key ? "var(--lm-amber-dim)" : "transparent",
                    color: sortBy === s.key ? "var(--lm-amber)" : "var(--lm-text-dim)",
                    fontWeight: sortBy === s.key ? 600 : 500,
                  }}
                >{s.label}</button>
              ))}
            </div>
          </div>

          {availableTypes.length > 0 && (
            <div>
              <div style={{ ...sectionLabel, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span>Sub-types</span>
                <button
                  onClick={() => {
                    setExcludedTypes((prev) => {
                      const next = new Set(prev);
                      if (allOn) { availableTypes.forEach((t) => next.add(t)); }
                      else { availableTypes.forEach((t) => next.delete(t)); }
                      saveExcluded(next);
                      return next;
                    });
                  }}
                  className="lm-u-btn"
                  style={{
                    padding: "2px 9px", borderRadius: 6, fontSize: 10, fontWeight: 600,
                    border: `1px solid ${allOn ? "var(--lm-amber)" : "var(--lm-border)"}`,
                    background: allOn ? "var(--lm-amber-dim)" : "transparent",
                    color: allOn ? "var(--lm-amber)" : "var(--lm-text-dim)",
                    textTransform: "none", letterSpacing: 0,
                  }}
                >{allOn ? "All on" : "Enable all"}</button>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                {availableTypes.map((type) => {
                  const on = !excludedTypes.has(type);
                  const Icon = TYPE_LUCIDE[type] ?? Circle;
                  const label = TYPE_LABEL[type] ?? type.replace("ambient:", "~");
                  return (
                    <button key={type} onClick={() => toggleType(type)} title={type} className="lm-u-btn" style={{
                      padding: "5px 11px", borderRadius: 8, fontSize: 12, minHeight: 28,
                      border: `1px solid ${on ? "var(--lm-teal)" : "var(--lm-border)"}`,
                      background: on ? "var(--lm-teal-dim)" : "transparent",
                      color: on ? "var(--lm-teal)" : "var(--lm-text-dim)",
                      fontWeight: on ? 600 : 500,
                      display: "inline-flex", alignItems: "center", gap: 5,
                    }}>
                      <Icon size={14} strokeWidth={2} style={{ opacity: on ? 1 : 0.55 }} /> {label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <div>
            <div style={{ ...sectionLabel, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span>Time range</span>
              {hasRange && (
                <button
                  onClick={() => { setFromTime(""); setToTime(""); }}
                  className="lm-u-btn"
                  style={{
                    padding: "2px 8px", borderRadius: 6, fontSize: 10, fontWeight: 600,
                    border: "1px solid var(--lm-border)", background: "transparent",
                    color: "var(--lm-red)", textTransform: "none", letterSpacing: 0,
                    display: "inline-flex", alignItems: "center", gap: 4,
                  }}
                ><X size={11} strokeWidth={2.5} /> Clear</button>
              )}
            </div>

            <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 8 }}>
              {TIME_PRESETS.map((p) => {
                const sel = hasRange && p.test(fromTime, toTime);
                return (
                  <button
                    key={p.label}
                    onClick={() => { const [f, t] = p.range(); setFromTime(f); setToTime(t); }}
                    className="lm-u-btn"
                    style={{
                      padding: "3px 10px", borderRadius: 999, fontSize: 11,
                      border: `1px solid ${sel ? "var(--lm-amber)" : "var(--lm-border)"}`,
                      background: sel ? "var(--lm-amber-dim)" : "transparent",
                      color: sel ? "var(--lm-amber)" : "var(--lm-text-dim)",
                      fontWeight: sel ? 700 : 500,
                    }}
                  >{p.label}</button>
                );
              })}
            </div>

            <div style={{ display: "flex", alignItems: "stretch", gap: 8 }}>
              <TimeField label="From" value={fromTime} onChange={setFromTime} />
              <span style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                flexShrink: 0, color: hasRange ? "var(--lm-amber)" : "var(--lm-text-muted)",
                transition: "color 0.15s",
              }}>
                <ArrowRight size={15} strokeWidth={2.25} />
              </span>
              <TimeField label="To" value={toTime} onChange={setToTime} />
            </div>
          </div>
        </div>

        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 10, padding: "12px 18px", borderTop: "1px solid var(--lm-border)",
          flexShrink: 0,
        }}>
          <button
            onClick={onResetAll}
            className="lm-u-btn"
            style={{
              padding: "6px 12px", borderRadius: 8, fontSize: 12,
              border: "1px solid var(--lm-border)", background: "transparent",
              color: "var(--lm-text-dim)", fontWeight: 600,
              display: "inline-flex", alignItems: "center", gap: 6,
            }}
          ><RotateCcw size={13} strokeWidth={2} /> Reset all</button>
          <button
            onClick={onClose}
            className="lm-u-btn lm-u-btn-primary"
            style={{ padding: "6px 16px", borderRadius: 8, fontSize: 12, fontWeight: 700 }}
          >Done</button>
        </div>
      </div>
    </div>
  );
}
