export interface OtaProgress {
  target: string;
  run_id: string;
  phase: string;
  downloaded_bytes?: number;
  total_bytes?: number;
  updated_at: number;
  message?: string;
  activity_at?: number;
}

export function installationActivityAge(progress: OtaProgress, nowSeconds: number): string | null {
  const activityAt = progress.activity_at;
  if (progress.phase !== "installing" || activityAt == null || !Number.isFinite(activityAt)
    || activityAt <= 0 || !Number.isFinite(nowSeconds)) return null;
  // A device clock ahead of the browser must never produce a negative duration.
  const seconds = Math.max(0, Math.floor(nowSeconds - activityAt));
  if (seconds < 5) return "Last activity just now";
  if (seconds < 60) return `Last activity ${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `Last activity ${minutes}m ${seconds % 60}s ago`;
  return `Last activity ${Math.floor(minutes / 60)}h ${minutes % 60}m ago`;
}

export const isTerminalProgress = (progress: OtaProgress) =>
  ["completed", "failed", "interrupted", "status_unavailable"].includes(progress.phase);

export function downloadPercent(progress: OtaProgress): number | null {
  const downloaded = progress.downloaded_bytes;
  const total = progress.total_bytes;
  if (progress.phase !== "downloading" || !Number.isFinite(total) || !Number.isFinite(downloaded)
    || total == null || total <= 0 || downloaded == null || downloaded < 0 || downloaded > total) return null;
  return Math.floor(downloaded / total * 100);
}

export function isNewUpdate(progress: OtaProgress, previousRun: string | undefined): boolean {
  // Device and browser clocks may differ; the updater run identity is authoritative.
  return !!progress.run_id && progress.run_id !== previousRun;
}
