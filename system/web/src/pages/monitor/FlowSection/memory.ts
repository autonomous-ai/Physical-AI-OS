import type { Turn } from "./types";

// Flow events arrive as dynamic JSON over SSE: the key set differs per event
// type and the device nests forwarded payloads under `data`
// (docs/flow-monitor.md). Every read below coerces with String/Number/Boolean
// or an Array.isArray guard, so `unknown` costs one cast and buys real safety.
// Keeping this module free of value imports is deliberate: it is what lets
// `node --test` load it directly (see system/web/tests/memory.test.mjs).
type FlowDetail = Record<string, unknown>;

export interface TurnMemoryInfo {
  // Fingerprint attached at lifecycle_start: file → {size, sha8}. Bytes.
  files: Record<string, { size: number; sha8: string }>;
  // memory_changed events that landed inside this turn (the agent wrote memory).
  // `execute` is the guard's mode: true = the quarantined blocks were really
  // removed; false = observe-only (`agent.memory_guard=false`), the file still
  // carries them and `quarantined` is what the guard WOULD have removed.
  // `runtime` is which runtime tree the write happened in — the guard sweeps
  // all six, but `files` above is the ACTIVE runtime's only (#463).
  changed: { file: string; runtime: string; quarantined: number; reasons: string[]; execute: boolean }[];
}

// Fixed display order for the memory fingerprint — the order the runtime loads
// them in, not Object.entries order, so a glance across turn cards compares
// like with like.
export const MEMORY_FILE_ORDER = ["USER.md", "MEMORY.md", "KNOWLEDGE.md"] as const;

export function orderedMemoryFiles(
  files: TurnMemoryInfo["files"],
): [string, { size: number; sha8: string }][] {
  const known = MEMORY_FILE_ORDER.filter((n) => n in files).map((n) => [n, files[n]] as [string, { size: number; sha8: string }]);
  const rest = Object.entries(files).filter(([n]) => !(MEMORY_FILE_ORDER as readonly string[]).includes(n));
  return [...known, ...rest];
}

// Memory the turn ran with, and whether it wrote any. Both come from flow
// events: `lifecycle_start.data.memory` (published by the OS memory guard) and
// `memory_changed` (emitted by its fsnotify watch ~2 s after the write, tagged
// with whatever trace is active then — or trace-less if the turn has already
// ended). Null when neither is present (older os-server, or no guard yet).
export function turnMemoryState(turn: Turn): TurnMemoryInfo | null {
  let files: TurnMemoryInfo["files"] | null = null;
  const changed: TurnMemoryInfo["changed"] = [];
  for (const ev of turn.events) {
    if (ev.type !== "flow_event") continue;
    const d = ev.detail as FlowDetail | undefined;
    const data = (d?.data ?? {}) as FlowDetail;
    if (d?.node === "lifecycle_start" && data.memory && typeof data.memory === "object") {
      files = data.memory as TurnMemoryInfo["files"];
    }
    if (d?.node === "memory_changed") {
      changed.push({
        file: String(data.file ?? ""),
        // "" when the guard could not name a runtime (a path outside the
        // adapter registry, migrate_persona/memory_guard_files.go:232).
        // Better silent than wrong — the label omits it entirely.
        runtime: String(data.runtime ?? ""),
        quarantined: Number(data.quarantined ?? 0),
        reasons: Array.isArray(data.reasons) ? data.reasons.map(String) : [],
        // Missing on an older os-server that always executed: default true.
        execute: data.execute === undefined ? true : Boolean(data.execute),
      });
    }
  }
  if (!files && changed.length === 0) return null;
  return { files: files ?? {}, changed };
}

// Bytes, with a unit, always. The memory sizes share a footer row with the LLM
// token counts, which use a bare 1000-based `k` — a number formatted the same
// way, a few pixels away, is read as a token count (#463). The unit is glued to
// the number (`854B`, not `854 B`) so the footer reads as two values, not four
// words. The exact byte count stays in the tooltip.
export function fmtBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}kB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

// "1 entry" / "2 entries". The badge speaks to the device owner, so the guard's
// own "quarantined" — which reads as a security incident — never reaches them.
function entries(n: number): string {
  return `${n} ${n === 1 ? "entry" : "entries"}`;
}

// Runtimes a removal actually happened in, deduped, in event order. Empty when
// the guard could not name one, in which case the label says nothing about
// where rather than implying the active runtime.
function removalRuntimes(removals: TurnMemoryInfo["changed"]): string {
  const names = [...new Set(removals.map((c) => c.runtime).filter(Boolean))];
  return names.length > 0 ? ` in ${names.join(", ")}` : "";
}

// Debug tooltip: the whole fingerprint plus every change, including the guard's
// internal reason codes. Named runtimes are shown per change because the sizes
// above them are the active runtime's.
function debugTitle(
  files: [string, { size: number; sha8: string }][],
  changed: TurnMemoryInfo["changed"],
): string {
  return [
    ...files.map(([name, f]) => `${name} ${f.size} bytes · ${f.sha8}`),
    ...changed.map((c) => {
      const where = c.runtime ? ` (${c.runtime})` : "";
      if (!c.quarantined) return `${c.file}${where} changed`;
      const verb = c.execute ? "removed" : "would remove (observe mode)";
      return `${c.file}${where} changed — ${verb} ${entries(c.quarantined)}: ${c.reasons.join(", ")}`;
    }),
  ].join("\n") || "memory";
}

export interface MemoryBadge {
  color: string;
  text: string;
  title: string;
}

// What the turn-card memory badge shows, if anything (#463). Null = render
// nothing.
//
// Gated by state rather than shown on every turn. `flow` is in PUBLIC_SECTIONS
// and debug is a header toggle, not a hidden URL param, so "debug-only" here
// means one click away, not gone:
//
//   gray  (the turn only read memory)       debug only — it reports the size of
//                                           a file the owner cannot open, on
//                                           every turn. A debugging aid by
//                                           definition.
//   amber (agent wrote, guard accepted)     debug only — lighting a badge on
//                                           every successful write trains
//                                           people to ignore the row, and then
//                                           red does not land either. Observe
//                                           mode belongs here too: nothing was
//                                           removed, the file is untouched.
//   red   (the guard removed blocks)        always — the only place in the
//                                           product where the owner can see
//                                           that the OS deleted something the
//                                           agent wrote. The rest of the
//                                           removal is an atomic rename, a
//                                           .quarantine.txt sidecar and a
//                                           .bak-<nano>, all SSH-only, and the
//                                           #421 reset endpoint ships with no
//                                           UI. Hiding it behind a toggle
//                                           recreates the invisibility #421
//                                           was filed about.
export function memoryBadge(memory: TurnMemoryInfo, isDebug: boolean): MemoryBadge | null {
  // Red only when blocks were really removed. In observe mode the guard reports
  // what it WOULD remove but the file is untouched, so that count is a warning.
  const removals = memory.changed.filter((c) => c.execute && c.quarantined > 0);
  const removed = removals.reduce((n, c) => n + c.quarantined, 0);
  const wouldRemove = memory.changed.reduce((n, c) => n + (c.execute ? 0 : c.quarantined), 0);
  if (removed === 0 && !isDebug) return null;

  const files = orderedMemoryFiles(memory.files);
  // Full filenames with the `.md`, separated by a middot: these are files on
  // disk, and "USER 854B MEMORY 1.9kB" ran together as one label instead of
  // reading as two sizes.
  const sizes = files.map(([name, f]) => `${name} ${fmtBytes(f.size)}`).join(" · ");
  const changedLabel = removed > 0
    ? `✎ memory updated · ${entries(removed)} removed${removalRuntimes(removals)}`
    : wouldRemove > 0
      ? `✎ memory changed · would remove ${entries(wouldRemove)}`
      : memory.changed.length > 0 ? "✎ memory changed" : "";
  const color = removed > 0 ? "var(--lm-red)"
    : memory.changed.length > 0 ? "var(--lm-amber)"
    : "var(--lm-text-muted)";
  // Normal mode carries the removal alone: the sizes belong to the active
  // runtime and the removal may not.
  const text = isDebug ? [sizes, changedLabel].filter(Boolean).join(" ") : changedLabel;
  if (!text) return null;
  const title = isDebug
    ? debugTitle(files, memory.changed)
    : removals.map((c) => `${c.file}${c.runtime ? ` (${c.runtime})` : ""} — ${entries(c.quarantined)} removed`).join("\n");
  return { color, text, title };
}
