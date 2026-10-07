export interface OtaProgress {
  target: string;
  run_id: string;
  phase: string;
  downloaded_bytes?: number;
  total_bytes?: number;
  updated_at: number;
  message?: string;
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
