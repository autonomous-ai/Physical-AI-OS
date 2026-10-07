import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard, LABEL_STYLE } from "@/components/setup/shared";
import { hwUrl } from "@/lib/api";

type RGB = [number, number, number];
type Mode = "default" | "off" | "custom";

interface RestingLook { effect: string; color: RGB; speed?: number }
interface RestingState { mode: Mode; color: RGB | null; default: RestingLook; effective: RestingLook }

// Dim looks for a light that stays on all day; channels are raw 0-255 strip values.
const PRESETS: { label: string; color: RGB }[] = [
  { label: "Warm white", color: [5, 4, 3] },
  { label: "Candle", color: [8, 4, 1] },
  { label: "Clean white", color: [4, 4, 4] },
  { label: "Peach", color: [7, 4, 3] },
  { label: "Brighter warm", color: [12, 9, 6] },
];

// Brightest custom channel; higher reads as a lamp rather than a resting glow.
const MAX_LEVEL = 64;

// Saturated status cues a resting colour could be mistaken for. Mirrors the
// default STATUS_LED_PRESETS / mic-muted colours in hal/presets.py. Warm amber
// is left out on purpose: it is the device's ordinary mood colour.
const STATUS_HUES: { hue: number; label: string }[] = [
  { hue: 0, label: "mic muted / error (red)" },
  { hue: 60, label: "hardware warning (yellow)" },
  { hue: 125, label: "update / acknowledge (green)" },
  { hue: 180, label: "agent offline (cyan)" },
  { hue: 220, label: "listening / booting (blue)" },
  { hue: 285, label: "hardware service down (purple)" },
];
const WARN_MIN_SATURATION = 0.6;
const WARN_HUE_WINDOW = 20;

function toHsv([r, g, b]: RGB): { h: number; s: number; v: number } {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d > 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
  }
  return { h, s: max === 0 ? 0 : d / max, v: max };
}

function fromHsv(h: number, s: number, v: number): RGB {
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  const [r, g, b] =
    h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
      : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [Math.round(r + m), Math.round(g + m), Math.round(b + m)];
}

// Screen swatch: the strip values are far too dark to show as-is, so scale to full brightness.
function swatch(color: RGB): string {
  const max = Math.max(...color);
  if (max === 0) return "transparent";
  const k = 255 / max;
  return `rgb(${color.map((c) => Math.round(c * k)).join(",")})`;
}

function sameColor(a: RGB | null | undefined, b: RGB | null | undefined): boolean {
  return !!a && !!b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
}

// The status cue `color` could be confused with, or null.
function statusLookalike(color: RGB): string | null {
  const { h, s } = toHsv(color);
  if (s < WARN_MIN_SATURATION) return null;
  for (const cue of STATUS_HUES) {
    const dist = Math.min(Math.abs(h - cue.hue), 360 - Math.abs(h - cue.hue));
    if (dist <= WARN_HUE_WINDOW) return cue.label;
  }
  return null;
}

const SLIDER_STYLE: React.CSSProperties = { width: "100%", accentColor: "var(--lm-amber)" };

export function LedSection({ active }: { active: boolean }) {
  const [state, setState] = useState<RestingState | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [saving, setSaving] = useState(false);
  // Slider positions; kept separately so dragging is smooth between saves.
  const [hue, setHue] = useState(30);
  const [sat, setSat] = useState(0.4);
  const [level, setLevel] = useState(5);
  // Last colour the owner chose, so switching the light back on returns to it.
  const lastCustom = useRef<RGB | null>(null);
  const timer = useRef<number | null>(null);

  const syncSliders = useCallback((color: RGB) => {
    const { h, s, v } = toHsv(color);
    setHue(Math.round(h));
    setSat(Math.round(s * 100) / 100);
    setLevel(Math.max(1, Math.min(MAX_LEVEL, v)));
  }, []);

  useEffect(() => {
    if (!active || state) return;
    let cancelled = false;
    fetch(hwUrl("/led/resting"))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((s: RestingState) => {
        if (cancelled) return;
        setState(s);
        const shown = s.mode === "custom" && s.color ? s.color : s.default.color;
        if (s.mode === "custom" && s.color) lastCustom.current = s.color;
        if (Math.max(...shown) > 0) syncSliders(shown);
      })
      .catch(() => { if (!cancelled) setUnavailable(true); });
    return () => { cancelled = true; };
  }, [active, state, syncSliders]);

  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);

  const save = useCallback(async (mode: Mode, color?: RGB) => {
    setSaving(true);
    try {
      const r = await fetch(hwUrl("/led/resting"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(color ? { mode, color } : { mode }),
      });
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      setState(await r.json());
    } catch (e) {
      toast.error(`Could not update the resting light: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, []);

  // Sliders save once dragging pauses, so the device follows along without a request per pixel.
  const saveSoon = useCallback((color: RGB) => {
    lastCustom.current = color;
    setState((s) => (s ? { ...s, mode: "custom", color } : s));
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { void save("custom", color); }, 250);
  }, [save]);

  const pickColor = (color: RGB) => {
    lastCustom.current = color;
    syncSliders(color);
    void save("custom", color);
  };

  const onSlider = (h: number, s: number, l: number) => {
    setHue(h); setSat(s); setLevel(l);
    const color = fromHsv(h, s, l);
    // Very low levels can round every channel to zero; keep at least a glimmer.
    saveSoon(Math.max(...color) > 0 ? color : [1, 1, 1]);
  };

  if (!state) {
    return (
      <SectionCard id="led" title="Resting light" active={active}>
        <div style={{ fontSize: 12, color: C.textMuted }}>
          {unavailable ? "This device has no LED light, or the hardware service is not running." : "Loading…"}
        </div>
      </SectionCard>
    );
  }

  const isOn = state.mode !== "off";
  const current: RGB = state.mode === "custom" && state.color ? state.color : state.default.color;
  const defaultDark = Math.max(...state.default.color) === 0;
  const lookalike = isOn ? statusLookalike(current) : null;

  const toggle = (on: boolean) => {
    if (!on) { void save("off"); return; }
    if (lastCustom.current) { void save("custom", lastCustom.current); return; }
    // A dark device default would leave "on" looking off, so start from the first preset.
    if (defaultDark) { pickColor(PRESETS[0].color); return; }
    void save("default");
  };

  const chip = (key: string, label: string, color: RGB, selected: boolean, onClick: () => void) => (
    <button
      key={key}
      type="button"
      onClick={onClick}
      disabled={saving && selected}
      aria-pressed={selected}
      style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "7px 12px", borderRadius: 999, cursor: "pointer",
        background: selected ? C.amberDim : C.surface,
        border: `1px solid ${selected ? C.amber : C.border}`,
        color: selected ? C.amber : C.text, fontSize: 12.5,
      }}
    >
      <span style={{
        width: 14, height: 14, borderRadius: "50%", flexShrink: 0,
        background: swatch(color), border: `1px solid ${C.border}`,
      }} />
      {label}
    </button>
  );

  return (
    <SectionCard id="led" title="Resting light" active={active}>
      <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 14, lineHeight: 1.6 }}>
        The glow the light settles on when nothing else is happening. Moods,
        speaking and status alerts still light up as usual. Changes show on the
        device right away and are kept after a restart.
      </div>

      <label htmlFor="led-resting-on" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, cursor: "pointer", fontSize: 13.5, color: C.text }}>
        <input
          id="led-resting-on"
          type="checkbox"
          style={{ flexShrink: 0 }}
          checked={isOn}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span style={{ whiteSpace: "nowrap" }}>Enable resting light</span>
      </label>

      {isOn && (
        <>
          <div style={LABEL_STYLE}>Colour</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 18 }}>
            {!defaultDark && chip("default", "Device default", state.default.color, state.mode === "default", () => { void save("default"); })}
            {PRESETS.map((p) => chip(p.label, p.label, p.color, state.mode === "custom" && sameColor(state.color, p.color), () => pickColor(p.color)))}
          </div>

          <div style={LABEL_STYLE}>Custom colour</div>
          <div style={{ display: "grid", gap: 12, marginBottom: 12 }}>
            <div>
              <label htmlFor="led-hue" style={{ fontSize: 12, color: C.textMuted }}>Hue</label>
              <input
                id="led-hue" type="range" min={0} max={359} value={hue}
                onChange={(e) => onSlider(Number(e.target.value), sat, level)}
                style={SLIDER_STYLE}
              />
              <div aria-hidden style={{
                height: 6, borderRadius: 3, marginTop: 2,
                background: "linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)",
              }} />
            </div>
            <div>
              <label htmlFor="led-sat" style={{ fontSize: 12, color: C.textMuted }}>White ↔ colour</label>
              <input
                id="led-sat" type="range" min={0} max={100} value={Math.round(sat * 100)}
                onChange={(e) => onSlider(hue, Number(e.target.value) / 100, level)}
                style={SLIDER_STYLE}
              />
            </div>
            <div>
              <label htmlFor="led-level" style={{ fontSize: 12, color: C.textMuted }}>
                Brightness ({Math.round((level / MAX_LEVEL) * 100)}%)
              </label>
              <input
                id="led-level" type="range" min={1} max={MAX_LEVEL} value={level}
                onChange={(e) => onSlider(hue, sat, Number(e.target.value))}
                style={SLIDER_STYLE}
              />
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12, color: C.textMuted }}>
            <span style={{
              width: 18, height: 18, borderRadius: "50%",
              background: swatch(current), border: `1px solid ${C.border}`,
            }} />
            <span style={{ fontFamily: "monospace" }}>RGB({current.join(", ")})</span>
            {saving && <span style={{ color: C.amber }}>Saving…</span>}
          </div>

          {lookalike && (
            <div role="status" style={{
              marginTop: 12, padding: "9px 12px", borderRadius: 8, fontSize: 12.5, lineHeight: 1.5,
              background: "var(--lm-yellow-dim)", border: `1px solid ${C.yellow}`, color: C.text,
            }}>
              This colour is close to the <b>{lookalike}</b> status light, so people may read it
              as that alert. A warmer or whiter colour avoids the mix-up.
            </div>
          )}
        </>
      )}
    </SectionCard>
  );
}
