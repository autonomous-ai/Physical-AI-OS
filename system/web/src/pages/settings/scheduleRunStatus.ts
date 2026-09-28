import type { ScheduleRunStatus } from "@/lib/api";

// How the device's own Settings page words a scheduled task's last run.
//
// Kept in a plain, dependency-free module (type-only imports, which erase) so
// the wording is testable with node:test alone — see
// tests/scheduleRunStatus.test.mjs — without a browser or a test-runner
// dependency, the same way chat/pendingReplies.ts is tested.
//
// The case this exists for is "skipped": the device deliberately did not run
// a template task because a connector it needs is not installed
// (Schedule.Requires, system/schedule/runner.go). That is NOT a failure —
// nothing broke, nothing will be retried, the user just has not connected a
// service — so it must never borrow the red failure style, and it should say
// what to fix rather than only that something happened.

/** Prefix of a skipped run's summary. Mirrors missingConnectorSummaryPrefix in
 *  system/schedule/runner.go, which is followed by the missing connector codes
 *  in the task's `requires` order, joined by ", ". Exact match, case included:
 *  the device writes it verbatim, so anything else is not a skip reason. */
const MISSING_CONNECTOR_PREFIX = "missing connector: ";

/** Visual weight of a last-run label: success is green, failure red, and
 *  everything else (a skip, or a status this UI does not know) neutral amber. */
export type LastRunTone = "success" | "failure" | "neutral";

export interface LastRunView {
  label: string;
  tone: LastRunTone;
}

/** The connector codes a "missing connector: a, b" summary names, in order;
 *  [] when the summary is anything else. Codes stay RAW (e.g. "gmail"): the
 *  device has no connector catalog to map them to display labels — unlike
 *  the Autonomous web app, which does. */
export function missingConnectorCodes(summary?: string | null): string[] {
  if (!summary || !summary.startsWith(MISSING_CONNECTOR_PREFIX)) return [];
  return summary
    .slice(MISSING_CONNECTOR_PREFIX.length)
    .split(",")
    .map((code) => code.trim())
    .filter(Boolean);
}

/** "gmail isn't connected" / "gmail, slack aren't connected", or null when
 *  the summary names no connector. */
export function skipReason(summary?: string | null): string | null {
  const codes = missingConnectorCodes(summary);
  if (codes.length === 0) return null;
  return `${codes.join(", ")} ${codes.length === 1 ? "isn't" : "aren't"} connected`;
}

/** The inline label shown next to "Last run", or null for a task that has
 *  never run. A skip whose summary can't be parsed still reads "Skipped" —
 *  never "Failed". An unrecognised status is shown as the device sent it,
 *  neutrally, rather than guessed into a failure (the old behaviour was to
 *  call anything but "success" a failure). */
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
