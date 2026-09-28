import assert from "node:assert/strict";
import test from "node:test";
import {
  describeLastRun, missingConnectorCodes, skipReason, skippedRunMessage,
} from "../src/pages/settings/scheduleRunStatus.ts";

// The device writes this exact summary for a skipped run
// (system/schedule/runner.go: missingConnectorSummaryPrefix + codes joined by ", ").

test("a skip names the one missing connector by its raw code", () => {
  assert.deepEqual(describeLastRun("skipped", "missing connector: gmail"),
    { label: "Skipped · gmail isn't connected", tone: "neutral" });
});

test("several missing connectors keep the device's order and read plural", () => {
  assert.deepEqual(describeLastRun("skipped", "missing connector: gmail, slack"),
    { label: "Skipped · gmail, slack aren't connected", tone: "neutral" });
});

test("a skip without a recognisable summary falls back to plain Skipped", () => {
  for (const summary of [undefined, "", "Daily briefing", "missing connector: ", "Missing connector: gmail"]) {
    assert.deepEqual(describeLastRun("skipped", summary), { label: "Skipped", tone: "neutral" }, String(summary));
  }
});

test("a skip is never rendered in the failure style", () => {
  assert.notEqual(describeLastRun("skipped", "missing connector: gmail").tone, "failure");
  assert.notEqual(describeLastRun("skipped").tone, "failure");
});

test("success and failure keep their existing labels and tones", () => {
  assert.deepEqual(describeLastRun("success", "Daily briefing"), { label: "Succeeded", tone: "success" });
  assert.deepEqual(describeLastRun("failure", "ws disconnected"), { label: "Failed", tone: "failure" });
});

test("a never-run row shows no status at all", () => {
  assert.equal(describeLastRun(undefined), null);
  assert.equal(describeLastRun(""), null);
});

test("an unrecognised status is shown as-is, not relabelled as a failure", () => {
  assert.deepEqual(describeLastRun("deferred"), { label: "deferred", tone: "neutral" });
});

test("missing connector codes are trimmed and blanks dropped", () => {
  assert.deepEqual(missingConnectorCodes("missing connector:  gmail ,, slack "), ["gmail", "slack"]);
  assert.deepEqual(missingConnectorCodes("ws disconnected"), []);
  assert.equal(skipReason("ws disconnected"), null);
});

test("the Run now toast carries the same reason", () => {
  assert.equal(skippedRunMessage("Inbox digest", "missing connector: gmail"),
    "\"Inbox digest\" skipped: gmail isn't connected.");
  assert.equal(skippedRunMessage("Inbox digest", "missing connector: gmail, slack"),
    "\"Inbox digest\" skipped: gmail, slack aren't connected.");
  assert.equal(skippedRunMessage("Inbox digest", undefined), "\"Inbox digest\" skipped.");
});
