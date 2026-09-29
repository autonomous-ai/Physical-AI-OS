import type { Turn } from "./types";

// No value imports in this module: tests load it directly with `node --test`.
type FlowDetail = Record<string, unknown>;

export interface TurnMemoryInfo {
  files: Record<string, { size: number; sha8: string }>;
  // `execute` false = observe-only: `quarantined` is what the guard would have removed.
  changed: { file: string; runtime: string; quarantined: number; reasons: string[]; execute: boolean }[];
}

// Runtime load order, so cards compare like with like.
export const MEMORY_FILE_ORDER = ["USER.md", "MEMORY.md", "KNOWLEDGE.md"] as const;

export function orderedMemoryFiles(
  files: TurnMemoryInfo["files"],
): [string, { size: number; sha8: string }][] {
  const known = MEMORY_FILE_ORDER.filter((n) => n in files).map((n) => [n, files[n]] as [string, { size: number; sha8: string }]);
  const rest = Object.entries(files).filter(([n]) => !(MEMORY_FILE_ORDER as readonly string[]).includes(n));
  return [...known, ...rest];
}

// Memory the turn ran with, and whether it wrote any.
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

// Bytes, with a unit, always.
export function fmtBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}kB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

// "1 entry" / "2 entries".
function entries(n: number): string {
  return `${n} ${n === 1 ? "entry" : "entries"}`;
}

// Runtimes a removal actually happened in, deduped, in event order.
function removalRuntimes(removals: TurnMemoryInfo["changed"]): string {
  const names = [...new Set(removals.map((c) => c.runtime).filter(Boolean))];
  return names.length > 0 ? ` in ${names.join(", ")}` : "";
}

// Debug tooltip: the full fingerprint plus every change.
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

// What the turn-card memory badge shows, if anything (#463).
export function memoryBadge(memory: TurnMemoryInfo, isDebug: boolean): MemoryBadge | null {
  // Red only when blocks were really removed (not observe mode).
  const removals = memory.changed.filter((c) => c.execute && c.quarantined > 0);
  const removed = removals.reduce((n, c) => n + c.quarantined, 0);
  const wouldRemove = memory.changed.reduce((n, c) => n + (c.execute ? 0 : c.quarantined), 0);
  if (removed === 0 && !isDebug) return null;

  const files = orderedMemoryFiles(memory.files);
  const sizes = files.map(([name, f]) => `${name} ${fmtBytes(f.size)}`).join(" · ");
  const changedLabel = removed > 0
    ? `✎ memory updated · ${entries(removed)} removed${removalRuntimes(removals)}`
    : wouldRemove > 0
      ? `✎ memory changed · would remove ${entries(wouldRemove)}`
      : memory.changed.length > 0 ? "✎ memory changed" : "";
  const color = removed > 0 ? "var(--lm-red)"
    : memory.changed.length > 0 ? "var(--lm-amber)"
    : "var(--lm-text-muted)";
  const text = isDebug ? [sizes, changedLabel].filter(Boolean).join(" ") : changedLabel;
  if (!text) return null;
  const title = isDebug
    ? debugTitle(files, memory.changed)
    : removals.map((c) => `${c.file}${c.runtime ? ` (${c.runtime})` : ""} — ${entries(c.quarantined)} removed`).join("\n");
  return { color, text, title };
}
