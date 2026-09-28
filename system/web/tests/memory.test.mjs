import assert from "node:assert/strict";
import test from "node:test";
import { turnMemoryState, fmtBytes, memoryBadge } from "../src/pages/monitor/FlowSection/memory.ts";

const turn = (...events) => ({ id: "t", startTime: "", type: "chat", path: "agent", status: "done", events });
const flow = (node, data = {}) => ({ type: "flow_event", detail: { node, data } });

test("a quarantine carries the runtime it happened in", () => {
  const state = turnMemoryState(turn(
    flow("lifecycle_start", { memory: { "USER.md": { size: 251, sha8: "a1b2c3d4" } } }),
    flow("memory_changed", { file: "USER.md", runtime: "hermes", quarantined: 1, reasons: ["free-prose"], execute: true }),
  ));
  assert.equal(state.changed[0].runtime, "hermes");
  assert.equal(state.changed[0].quarantined, 1);
  assert.equal(state.files["USER.md"].size, 251);
});

test("an os-server that sends no runtime yields an empty name, not undefined", () => {
  const state = turnMemoryState(turn(flow("memory_changed", { file: "MEMORY.md", quarantined: 0 })));
  assert.equal(state.changed[0].runtime, "");
  // Missing `execute` means an older os-server that always executed.
  assert.equal(state.changed[0].execute, true);
});

test("a turn with neither fingerprint nor change has no memory state", () => {
  assert.equal(turnMemoryState(turn(flow("lifecycle_start", {}))), null);
});

const state = (files, changed) => ({ files, changed });
const FP = { "USER.md": { size: 251, sha8: "a1b2c3d4" }, "MEMORY.md": { size: 1229, sha8: "e5f6a7b8" } };

test("bytes get a unit so they cannot be read as the token count beside them", () => {
  assert.equal(fmtBytes(251), "251B");
  assert.equal(fmtBytes(1023), "1023B");
  assert.equal(fmtBytes(1229), "1.2kB");
  assert.equal(fmtBytes(2 * 1024 * 1024), "2.0MB");
});

test("a read-only turn shows nothing in normal mode and sizes in debug", () => {
  assert.equal(memoryBadge(state(FP, []), false), null);
  const dbg = memoryBadge(state(FP, []), true);
  assert.equal(dbg.text, "USER.md 251B · MEMORY.md 1.2kB");
  assert.equal(dbg.color, "var(--lm-text-muted)");
  assert.match(dbg.title, /USER\.md 251 bytes · a1b2c3d4/);
});

test("an accepted write stays out of normal mode so a removal still lands", () => {
  const accepted = state(FP, [{ file: "USER.md", runtime: "hermes", quarantined: 0, reasons: [], execute: true }]);
  assert.equal(memoryBadge(accepted, false), null);
  const dbg = memoryBadge(accepted, true);
  assert.equal(dbg.color, "var(--lm-amber)");
  assert.equal(dbg.text, "USER.md 251B · MEMORY.md 1.2kB ✎ memory changed");
});

test("a removal is visible without debug, in the owner's words, and names the runtime", () => {
  const removed = state(FP, [{ file: "USER.md", runtime: "hermes", quarantined: 1, reasons: ["prescriptive"], execute: true }]);
  const badge = memoryBadge(removed, false);
  assert.equal(badge.color, "var(--lm-red)");
  assert.equal(badge.text, "✎ memory updated · 1 entry removed in hermes");
  // "quarantined" reads as a security incident; the active runtime's sizes
  // must not sit next to a removal that may have happened elsewhere.
  assert.doesNotMatch(badge.text, /quarantin/);
  assert.doesNotMatch(badge.text, /251/);
  assert.equal(badge.title, "USER.md (hermes) — 1 entry removed");
});

test("several removals read as entries and debug keeps the sizes and reasons", () => {
  const removed = state(FP, [{ file: "USER.md", runtime: "hermes", quarantined: 2, reasons: ["prescriptive", "free-prose"], execute: true }]);
  assert.equal(memoryBadge(removed, false).text, "✎ memory updated · 2 entries removed in hermes");
  const dbg = memoryBadge(removed, true);
  assert.equal(dbg.text, "USER.md 251B · MEMORY.md 1.2kB ✎ memory updated · 2 entries removed in hermes");
  assert.match(dbg.title, /prescriptive, free-prose/);
});

test("removals in two runtimes name both, once each", () => {
  const removed = state(FP, [
    { file: "USER.md", runtime: "hermes", quarantined: 1, reasons: [], execute: true },
    { file: "MEMORY.md", runtime: "hermes", quarantined: 1, reasons: [], execute: true },
    { file: "USER.md", runtime: "codex", quarantined: 1, reasons: [], execute: true },
  ]);
  assert.equal(memoryBadge(removed, false).text, "✎ memory updated · 3 entries removed in hermes, codex");
});

test("observe mode never claims a removal — the file still carries the block", () => {
  const observed = state(FP, [{ file: "USER.md", runtime: "codex", quarantined: 1, reasons: ["free-prose"], execute: false }]);
  assert.equal(memoryBadge(observed, false), null);
  const dbg = memoryBadge(observed, true);
  assert.equal(dbg.color, "var(--lm-amber)");
  assert.equal(dbg.text, "USER.md 251B · MEMORY.md 1.2kB ✎ memory changed · would remove 1 entry");
});

test("an unnamed runtime is omitted rather than guessed", () => {
  const removed = state({}, [{ file: "USER.md", runtime: "", quarantined: 1, reasons: [], execute: true }]);
  assert.equal(memoryBadge(removed, false).text, "✎ memory updated · 1 entry removed");
});
