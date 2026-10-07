import { LoaderCircle } from "lucide-react";
import { downloadPercent, isTerminalProgress } from "./otaProgress";
import type { OtaProgress } from "./otaProgress";

const phaseLabels: Record<string, string> = {
  status_unavailable: "Status unavailable", preparing: "Preparing", downloading: "Downloading", verifying: "Verifying download",
  installing: "Installing", restarting: "Restarting", checking: "Checking health",
  completed: "Completed", failed: "Failed", rolling_back: "Rolling back", interrupted: "Interrupted",
};
const bytes = (value: number) => value < 1024 * 1024
  ? `${(value / 1024).toFixed(1)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB`;

export function UpdateProgress({ progress, updating, reconnecting }: {
  progress?: OtaProgress; updating: boolean; reconnecting: boolean;
}) {
  if (!progress && !updating) return null;
  const terminal = progress && isTerminalProgress(progress);
  const percent = progress ? downloadPercent(progress) : null;
  const downloaded = progress?.downloaded_bytes;
  const label = progress ? phaseLabels[progress.phase] ?? progress.phase : "Waiting for updater";
  return <div className="lm-ota-progress" data-phase={progress?.phase} role="status">
    <div className="lm-ota-progress-heading">
      {updating && <LoaderCircle size={14} className="lm-spin-ico" aria-hidden />}
      <strong>{terminal && progress.phase !== "status_unavailable" ? "Last update · " : ""}{label}{percent != null ? ` · ${percent}%` : ""}</strong>
      {reconnecting && <span>Reconnecting… Showing last known status.</span>}
    </div>
    {progress?.phase === "downloading" && downloaded != null && Number.isFinite(downloaded) && downloaded >= 0 && <span>
      {bytes(downloaded)}{percent != null && progress.total_bytes ? ` / ${bytes(progress.total_bytes)}` : " downloaded · total size unknown"}
    </span>}
    {percent != null && <progress aria-label="Download progress" value={percent} max={100} />}
    {progress?.message && <span>{progress.message}</span>}
  </div>;
}
