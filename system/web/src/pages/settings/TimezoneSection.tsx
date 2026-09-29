import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import { C, SectionCard, LABEL_STYLE, INPUT_STYLE } from "@/components/setup/shared";
import { getTimezone, setTimezone } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";

// Current wall-clock time in `zone` as a preview ("" when invalid).
function formatZoneTime(zone: string): string {
  if (!zone) return "";
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit",
      hour12: false,
    }).format(new Date());
  } catch {
    return "";
  }
}

// regionOf is the optgroup bucket for a zone
function regionOf(zone: string): string {
  const i = zone.indexOf("/");
  return i === -1 ? "Other" : zone.slice(0, i);
}

// Friendly location part, e.g. "Asia/Ho_Chi_Minh" -> "Ho Chi Minh".
function cityOf(zone: string): string {
  const i = zone.indexOf("/");
  const tail = i === -1 ? zone : zone.slice(i + 1);
  return tail.replace(/_/g, " ");
}

// gmtOffset returns the zone's current UTC offset as a short string like "GMT+7" / "GMT+5:30" / "GMT-8" (DST-aware), or "" when it can't resolve.
function gmtOffset(zone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, timeZoneName: "shortOffset",
    }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  } catch {
    return "";
  }
}

// offsetMinutes is the signed UTC offset in minutes, used to sort zones within a region by offset (the order most web pickers use).
function offsetMinutes(zone: string): number {
  const o = gmtOffset(zone).replace("GMT", "").trim();
  if (!o) return 9999;
  const m = /^([+-])(\d{1,2})(?::(\d{2}))?$/.exec(o);
  if (!m) return 0;
  const sign = m[1] === "-" ? -1 : 1;
  return sign * (parseInt(m[2], 10) * 60 + (m[3] ? parseInt(m[3], 10) : 0));
}

// Full option text, e.g. "(GMT+7) Ho Chi Minh".
function labelOf(zone: string): string {
  const off = gmtOffset(zone);
  const city = cityOf(zone);
  return off ? `(${off}) ${city}` : city;
}

type ZoneOpt = { value: string; label: string; off: number; region: string };

export function TimezoneSection({ active }: { active: boolean }) {
  // Portaled outside .lm-root, so it needs its own theme class.
  const [, , themeClass] = useTheme();
  const [current, setCurrent] = useState<string>("");
  const [zones, setZones] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [, setTick] = useState(0);

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getTimezone()
      .then((r) => {
        setCurrent(r.current);
        setSelected(r.current);
        if (r.zones?.length) setZones(r.zones);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  // Built once per zone list: the Intl calls are the expensive part.
  const options = useMemo<ZoneOpt[]>(
    () =>
      zones.map((z) => ({
        value: z, label: labelOf(z), off: offsetMinutes(z), region: regionOf(z),
      })),
    [zones],
  );

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = q
      ? options.filter(
          (o) =>
            o.value.toLowerCase().includes(q) ||
            o.label.toLowerCase().includes(q),
        )
      : options;
    const byRegion = new Map<string, ZoneOpt[]>();
    for (const o of matched) {
      (byRegion.get(o.region) ?? byRegion.set(o.region, []).get(o.region)!).push(o);
    }
    for (const list of byRegion.values()) {
      list.sort((a, b) => a.off - b.off || a.label.localeCompare(b.label));
    }
    return [...byRegion.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [options, query]);

  const flat = useMemo(() => groups.flatMap(([, list]) => list), [groups]);

  const preview = useMemo(() => formatZoneTime(selected), [selected]);
  const selectedLabel = selected ? labelOf(selected) : "";

  useEffect(() => {
    if (!open) return;
    setQuery("");
    const i = flat.findIndex((o) => o.value === selected);
    setHighlight(i >= 0 ? i : 0);
    const t = setTimeout(() => searchRef.current?.focus(), 0);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => {
      clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-idx="${highlight}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [highlight, open]);

  // Picking a row selects it, closes the modal, AND applies immediately
  function pick(value: string) {
    setSelected(value);
    setOpen(false);
    applyZone(value);
  }

  function onSearchKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const o = flat[highlight];
      if (o) pick(o.value);
    }
  }

  // applyZone POSTs the given IANA zone.
  async function applyZone(zone: string) {
    if (!zone || zone === current || applying || !zones.includes(zone)) return;
    setApplying(true);
    try {
      await setTimezone(zone);
      setCurrent(zone);
      toast.success(`Timezone set to ${zone}.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to set timezone.");
    } finally {
      setApplying(false);
    }
  }

  let flatIdx = 0;

  const modal =
    open &&
    createPortal(
      <div
        className={`lm-root ${themeClass}`}
        onClick={() => setOpen(false)}
        style={{
          position: "fixed", inset: 0, zIndex: 1000,
          background: "rgba(0,0,0,0.66)", backdropFilter: "blur(3px)",
          display: "flex", alignItems: "center", justifyContent: "center",
          padding: 16,
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Select timezone"
          onClick={(e) => e.stopPropagation()}
          style={{
            width: "100%", maxWidth: 520, height: "min(560px, 86vh)",
            background: C.card, border: `1px solid ${C.border}`,
            borderRadius: 12, boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
            display: "flex", flexDirection: "column", overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "13px 15px", borderBottom: `1px solid ${C.border}`, flexShrink: 0,
            }}
          >
            <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>
              Select timezone
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close"
              style={{
                width: 32, height: 32, borderRadius: 8, padding: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: C.surface, border: `1px solid ${C.border}`,
                color: C.textDim, cursor: "pointer", fontSize: 15,
              }}
            >
              ✕
            </button>
          </div>

          <div style={{ padding: 12, borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHighlight(0);
              }}
              onKeyDown={onSearchKeyDown}
              placeholder="Search city, region or GMT offset…"
              style={INPUT_STYLE}
            />
          </div>

          <div ref={listRef} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "0 6px 6px" }}>
            {flat.length === 0 ? (
              <div
                style={{
                  height: "100%", display: "flex",
                  alignItems: "center", justifyContent: "center",
                  padding: "16px 12px", fontSize: 13, color: C.textMuted,
                  textAlign: "center",
                }}
              >
                No timezones match “{query}”.
              </div>
            ) : (
              groups.map(([region, list]) => (
                <div key={region}>
                  <div
                    style={{
                      position: "sticky", top: 0, zIndex: 1,
                      margin: "0 -6px", padding: "9px 17px 7px",
                      fontSize: 11, fontWeight: 700,
                      letterSpacing: "0.09em", textTransform: "uppercase", color: C.textDim,
                      background: C.surface, borderBottom: `1px solid ${C.border}`,
                    }}
                  >
                    {region}
                  </div>
                  {list.map((o) => {
                    const idx = flatIdx++;
                    const isSel = o.value === selected;
                    const isHi = idx === highlight;
                    return (
                      <div
                        key={o.value}
                        data-idx={idx}
                        role="option"
                        aria-selected={isSel}
                        onMouseEnter={() => setHighlight(idx)}
                        onClick={() => pick(o.value)}
                        style={{
                          display: "flex", alignItems: "center", justifyContent: "space-between",
                          gap: 8, padding: "10px 12px", borderRadius: 8, cursor: "pointer",
                          fontSize: 13,
                          background: isHi ? C.surface : "transparent",
                          color: isSel ? C.amber : C.text,
                          fontWeight: isSel ? 600 : 400,
                        }}
                      >
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {o.label}
                        </span>
                        {isSel && <span style={{ flexShrink: 0, fontSize: 12 }}>✓</span>}
                      </div>
                    );
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      </div>,
      document.body,
    );

  return (
    <SectionCard id="timezone" title="Timezone" active={active}>
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
            The device's local time zone. Used for quiet hours, daily history
            buckets, and the assistant's sense of time. Applies immediately — no
            restart needed.
          </div>

          <div style={{ marginBottom: 8 }}>
            <label htmlFor="timezone-button" style={LABEL_STYLE}>
              Zone (current: <span style={{ color: C.amber }}>{current || "?"}</span>)
            </label>

            <button
              id="timezone-button"
              type="button"
              onClick={() => !applying && setOpen(true)}
              disabled={applying}
              aria-haspopup="dialog"
              aria-expanded={open}
              style={{
                ...INPUT_STYLE,
                display: "flex", alignItems: "center", justifyContent: "space-between",
                gap: 8, textAlign: "left",
                opacity: applying ? 0.6 : 1,
                cursor: applying ? "not-allowed" : "pointer",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {selectedLabel || "Select a timezone…"}
              </span>
              <span style={{ color: C.textMuted, fontSize: 11, flexShrink: 0 }}>▼</span>
            </button>
          </div>

          {preview && (
            <div style={{ fontSize: 12, color: C.textMuted }}>
              Local time there now: <span style={{ color: C.text }}>{preview}</span>
            </div>
          )}

          {applying && (
            <div style={{ marginTop: 8, fontSize: 12, color: C.amber }}>Applying…</div>
          )}

          {modal}
        </>
      )}
    </SectionCard>
  );
}
