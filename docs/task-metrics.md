# Chat and sensing task completion metrics

These metrics use the same technical completion definition as [voice KPI-3](voice-metrics.md#kpi-3-execution-completion-not-correctness), with separate chat and sensing cohorts. **Completed means execution finished without observed terminal errors.** It does not assess answer correctness, user satisfaction, physical effects, or audio playback.

## Groups and eligibility

| Group | Cohort event | Eligible task |
|-------|--------------|---------------|
| Voice | `voice_metrics_task_started` (plus HAL task snapshots) | `voice`, `voice_command`, `voice_followup`; unchanged voice scoring |
| Chat | `chat_metrics_task_started` | `web_chat`, `mqtt_chat` accepted by OS, including queued requests |
| Sensing | `sensing_metrics_task_started` | Other noninternal sensing inputs selected for actual task dispatch |

MQTT with `speak: true` becomes voice and belongs only to the voice group. Internal `voice_agent_handled`, `voice_listening`, `voice_listening_end`, and `look.capture` notifications create no cohort entry.

Chat enters the denominator on receipt, before execution, and receives a fixed run binding when queued. Queue acceptance is not completion. Sensing enters only after dispatch selection: inputs dropped by sleep, cooldown, coalescing, expiry, or another suppression decision are not tasks. Queued sensing enters when replay selects it for dispatch. A merged Hermes ambient batch is one dispatched task, not one task per raw sensor input. For queued sensing in Hermes, emit the start only after `SendChat` is accepted or fails without retry; a known-unsent retry may regroup the batch and does not emit a start. This applies to both single and merged queued inputs. Limitation: these Hermes tasks have not entered the denominator while that send call is pending, or if the process crashes before its outcome. The other five runtimes emit queued sensing starts before dispatch and preserve the fixed run identity across known-unsent retries. A known-unsent retry does not create a terminal failure or a second task.

Each start event carries `schema_version=1`, `interaction_id`, `run_id`, `event_type`, and `task_started_at_ms` (Unix milliseconds). Receipt and subsequent run binding are amendments, not separate turns. Merge identities using device + interaction id or nonempty device + run id; retain the earliest start.

All three groups reuse **`voice_metrics_task_execution`** as their shared terminal evidence event. Its historical name does not restrict it to voice. Do not expect `chat_metrics_task_execution` or `sensing_metrics_task_execution`, and never count standalone execution rows as tasks. Join evidence only to the selected group's starts, on device + run id or device + interaction id.

Evidence has `schema_version=1`, `interaction_id`, `run_id`, `outcome` (`completed`, `failed`, `unknown`, `cancelled`), `evidence`, `execution_at_ms`, and boolean `error`. An explicit runtime end without abort/error is completed; terminal lifecycle/chat/dispatch errors are failed. Recovery alone is unknown. Any joined terminal failure through the report cutoff is sticky: later cleanup cannot turn it into completion. Harness also recognizes `harness_correlated_summary` cancellation as terminal, counted separately in `cancelled_turns`; later cleanup cannot make it successful. Failure takes precedence over cancellation. Otherwise select the latest execution timestamp (completed wins an equal-timestamp tie with unknown). Voice-specific HAL/realtime rules remain in the voice runbook.

## AA-only query and report

An agent needs AA event tracking read/export access and the repository reporter; device access is unnecessary. The query below assumes a BigQuery-style `event_tracking` table containing `event_name`, Unix-second `event_timestamp`, and `data` with `user_pseudo_id` and an `event_params` key/value array. Adapt table/accessors to your warehouse; this is an example schema, not a verified warehouse contract.

```sql
SELECT event_name, event_timestamp, data
FROM event_tracking
WHERE event_name IN (
  'chat_metrics_task_started',
  'sensing_metrics_task_started',
  'voice_metrics_task_execution'
)
  AND event_timestamp >= UNIX_SECONDS(TIMESTAMP('2026-09-11 00:00:00+07'))
  AND event_timestamp <= UNIX_SECONDS(TIMESTAMP('2026-09-11 12:00:00+07'))
-- AND data.user_pseudo_id = '<device identity stored in AA>'
ORDER BY event_timestamp;
```

Export one full row per JSONL line, preserving device identity, observation timestamps, and all params. Include **all starts and binding amendments**, not just completed turns, and execution evidence through the desired as-of time. For a single group the query may keep only that group's start name plus the shared execution name. Do not filter shared execution rows by event name prefix or by `event_type` (execution events need not carry that field). To report voice from the same export, also include `voice_metrics_task_started` and `voice_metrics_interaction`.

```bash
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --group chat --settle-seconds 0 > chat-task-report.json
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --group sensing --settle-seconds 0 > sensing-task-report.json
```

For historical snapshots add `--now-ms <cutoff in Unix milliseconds>`. The default group is `voice`; specify chat/sensing explicitly. The reporter also accepts journal JSONL (`journalctl -u hal -u os-server -o json --no-pager`) from an authorized device session. `--device` supplies a missing-identity fallback, not a filter. Filter devices in the export.

Input coverage defines the start interval; there is no start-window CLI. Preserve the full start/binding history for the selected cohort and its execution tail through as-of. The reporter filters observation timestamps before selecting evidence. Untimestamped exports cannot establish an accurate historical snapshot; missing timestamps and identity must be reported as coverage gaps. Historical tasks without start events cannot be reconstructed from terminal events alone.

## Counts and interpretation

For each group independently:

```text
completion_pct = 100 * completed_turns / eligible_mature_turns
```

The default settling horizon is **0 seconds**: all eligible starts through as-of enter `eligible_mature_turns`, including failed, unknown, and unfinished tasks. For example, **85 completed out of 100 eligible is 85%**. The default target is **≥85%**, evaluated before rounding. Zero eligible tasks means N/A (`completion_pct` and `meets_target` are null), not 0% or 100%. `completion_pct` aliases the legacy `kpi3_pct` field; `group` identifies the cohort. An optional positive `--settle-seconds` excludes newer starts into `fresh_pending_turns`; it is not an execution timeout.

Report group, interval, as-of, source, horizon, completed/eligible counts, percentage, target result, failed/cancelled/unknown/incomplete counts, and coverage. Across devices divide summed completed counts by summed eligible counts; do not average percentages. Keep chat, sensing, and voice results separate. An unmatched execution may belong to another group; it is not proof of a dropped task in the selected group. Telemetry loss counters are device-wide cumulative counters, not losses attributable to this group; examine maxima and restart resets rather than summing event values.

`vi-smoke-`, `chat-smoke-`, and `sensing-smoke-` interactions are excluded by default. Diagnostic callers must use a smoke prefix; unmarked requests cannot be distinguished from real tasks. Queued sensing replay generates its own interaction ID from the run ID, so an ingress smoke ID is not retained: for queue diagnostics, exclude the known test run IDs in the export before scoring. `--include-synthetic` is for instrumentation checks only. A local `[telemetry] event` proves recording; `[telemetry] delivered` proves successful AA HTTP ingestion, not warehouse visibility. Confirm warehouse visibility using exported event IDs and interaction/run identities. These docs describe the implementation, not proof that any particular device has deployed it.
