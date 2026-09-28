import type { ScheduleRunStatus } from "@/lib/api";

// Dependency-free so tests/scheduleRunStatus.test.mjs can load it under node:test.

/** Prefix of a skipped run's summary; mirrors missingConnectorSummaryPrefix in system/schedule/runner.go (exact match). */
const MISSING_CONNECTOR_PREFIX = "missing connector: ";

/** Visual weight of a last-run label: success is green, failure red, and everything else (a skip, or a status this UI does not know) neutral amber. */
export type LastRunTone = "success" | "failure" | "neutral";

export interface LastRunView {
  label: string;
  tone: LastRunTone;
}

/** The connector codes a "missing connector: a, b" summary names, in order; [] when the summary is anything else. */
export function missingConnectorCodes(summary?: string | null): string[] {
  if (!summary || !summary.startsWith(MISSING_CONNECTOR_PREFIX)) return [];
  return summary
    .slice(MISSING_CONNECTOR_PREFIX.length)
    .split(",")
    .map((code) => code.trim())
    .filter(Boolean);
}

/** "gmail isn't connected" / "gmail, slack aren't connected", or null when the summary names no connector. */
export function skipReason(summary?: string | null): string | null {
  const codes = missingConnectorCodes(summary);
  if (codes.length === 0) return null;
  return `${codes.join(", ")} ${codes.length === 1 ? "isn't" : "aren't"} connected`;
}

/** The inline label shown next to "Last run", or null for a task that has never run. */
export function describeLastRun(
  status?: ScheduleRunStatus | string | null,
  summary?: string | null,
): LastRunView | null {
  if (!status) return null;
  switch (status) {
    case "success":
      return { label: "Succeeded", tone: "success" };
    case "failure":
      return { label: "Failed", tone: "failure" };
    case "skipped": {
      const reason = skipReason(summary);
      return { label: reason ? `Skipped · ${reason}` : "Skipped", tone: "neutral" };
    }
    default:
      return { label: status, tone: "neutral" };
  }
}

/** Toast copy after a local "Run now" that was skipped. */
export function skippedRunMessage(name: string, summary?: string | null): string {
  const reason = skipReason(summary);
  return reason ? `"${name}" skipped: ${reason}.` : `"${name}" skipped.`;
}
