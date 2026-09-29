# Flow Monitor — Event Pipeline (2026-05-08)

The OpenClaw section of the Flow Monitor previously had three separate
nodes for the "inside the agent" portion of a turn: `llm_first_token`
(LLM Start), `agent_thinking` (THINK), and `tool_exec` (TOOL). These were
replaced with a single **Event Pipeline** rect that lists OpenClaw stream
events in chronological order, with consecutive same-type deltas merged
into one summary row.

The custom `llm_first_token` flow event (Lamp-generated marker for the
first thinking/assistant delta) was also removed — the pipeline aggregator
and timing strip now observe the first stream delta directly from
`type === "thinking"` / `"assistant_delta"` SSE events, so the marker
became redundant.

## Why the change

OpenClaw emits 12 stream types under `event:"agent"` (`lifecycle`,
`tool`, `assistant`, `thinking`, plus 8 operational ones: `error`,
`item`, `plan`, `approval`, `command_output`, `patch`, `compaction`,
…). Mapping each to its own node would clutter the diagram for the
common case where only a handful fire. A single pipeline list:

- shows the actual sequence of events the way they happened
- naturally accommodates any of the 12 stream types — rare events
  (compaction, error) just appear as new rows, no extra layout work
- replaces the "thinking ↔ writing ↔ tool" loop diagram with concrete
  per-segment timing (thinking · 5.2s · 200 chunks · ~4k chars), which
  is what users actually want when debugging a slow turn

## Aggregation rules

`aggregateEvents()` in `helpers.ts` walks the in-memory event list
(received via the `/openclaw/flow-stream` SSE) and produces an array
of `PipelineRow`:

- Consecutive `thinking` deltas → one row (`kind="thinking"`).
- Consecutive `assistant_delta` events → one row (`kind="assistant"`).
- Each `tool_call` (start phase) → one row (`kind="tool"`); the result
  phase attaches its timestamp to the matching tool row's `endMs` so
  the row's duration covers the entire tool exec.
- `lifecycle_start` / `lifecycle_end` → one-shot rows.
- `compaction` / `error` operational streams → one row each, kind
  set so the UI can color them distinctly.
- Other flow events (`chat_send`, `hw_*`, `tts_send`, `token_usage`)
  are NOT shown in the pipeline — they belong to the surrounding
  flow nodes (Agent Call, Lamp Hook, etc.) and would clutter the list.

## JSONL footprint

The pipeline reads from in-memory events received over SSE — it does
NOT add any new events to `local/flow_events_*.jsonl`. Thinking and
assistant deltas use `monitorBus.Push` (SSE-only, no file write), so
they only exist while the UI is open.

Replaying an old turn from JSONL still works but only shows the
flow events that were written: `lifecycle_start`, `lifecycle_end`,
`tool_call`, etc. Per-delta chunk/char counts are unavailable for
replay. This is intentional — adding deltas to JSONL would 12× the
file size for marginal value.

## Reverting to the 5-core node design

The previous design (5 separate agent-core nodes with bidirectional
THINK ↔ WRITE ↔ TOOL loops + ×N count badges) was committed earlier
on this branch but discarded before merging. To restore it, look at
the git history right before the pipeline introduction commit and
revert only the FlowDiagram + helpers + types changes; the backend
`markFirstAssistant` helper is gone with that revert and would need
to be re-added.

## Files changed

- `system/web/src/pages/monitor/FlowSection/helpers.ts` — `aggregateEvents()`
  + `PipelineRow` type
- `system/web/src/pages/monitor/FlowSection/FlowDiagram.tsx` — new
  pipeline rect + foreignObject row list; `agent_thinking` and
  `tool_exec` node circles skipped (FlowStage entries kept for edge
  anchors and visited tracking; `llm_first_token` FlowStage was
  removed entirely).
- `docs/flow-monitor.md` + `docs/vi/flow-monitor_vi.md` — updated
  the "OpenClaw section" diagram description.
