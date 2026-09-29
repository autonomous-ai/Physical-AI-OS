import type { Perception, PoseSample } from "./types";

export function fmtAgo(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

// Maps a presence state string to a color so users can scan at a glance.
export function presenceColor(state: string): string {
  switch (state) {
    case "active":   return "var(--lm-green)";
    case "idle":     return "var(--lm-amber)";
    case "away":     return "var(--lm-red)";
    default:         return "var(--lm-text-muted)";
  }
}

// Light tier from the raw sensor level (~0-1000).
export function lightTier(level: number): { label: string; color: string } {
  if (level < 30)  return { label: "Dark",   color: "var(--lm-text-muted)" };
  if (level < 200) return { label: "Dim",    color: "var(--lm-blue)" };
  if (level < 600) return { label: "Bright", color: "var(--lm-teal)" };
  return                  { label: "Sunlit", color: "var(--lm-green)" };
}

// Risk tier for pose samples.
export function poseDotColor(sample: PoseSample): string {
  switch (sample.risk_level) {
    case 4: return "var(--lm-red)";
    case 3: return "var(--lm-amber)";
    case 2: return "var(--lm-teal)";
    case 1: return "var(--lm-green)";
    default: return "var(--lm-text-muted)";
  }
}

export function riskName(level: number | null | undefined): string {
  switch (level) {
    case 4: return "high";
    case 3: return "medium";
    case 2: return "low";
    case 1: return "negligible";
    default: return "—";
  }
}

export function posePillStatus(pose: Perception): { text: string; color: string } {
  const ageS = pose.window_age_s ?? 0;
  const durS = pose.window_duration_s ?? 600;
  const threshold = pose.bad_ratio_threshold ?? 0.6;
  const minSamples = pose.window_min_samples ?? 3;
  const live = pose.running ?? pose.summary;
  const samples = pose.samples_in_buffer ?? 0;
  if (live && samples >= minSamples) {
    const pct = Math.round(live.bad_ratio * 100);
    if (live.bad_ratio >= threshold) {
      return { text: `Bad ${pct}% (${live.bad_samples}/${samples})`, color: "var(--lm-red)" };
    }
    return { text: `OK ${pct}% (${live.bad_samples}/${samples})`, color: "var(--lm-green)" };
  }
  if (ageS > 0) {
    const remainMin = Math.max(0, Math.ceil((durS - ageS) / 60));
    const samplesShort = Math.max(0, minSamples - samples);
    const reason = samplesShort > 0 ? `${samplesShort} sample${samplesShort === 1 ? "" : "s"} short` : `${remainMin}m left`;
    return { text: `Filling ${reason}`, color: "var(--lm-amber)" };
  }
  return { text: "Idle", color: "var(--lm-text-muted)" };
}
