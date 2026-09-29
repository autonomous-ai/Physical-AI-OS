# Device telemetry

How this device reports numbers about itself. Read this before adding a new
tracker; `docs/voice-metrics.md` is one tracker built on top of it, not the
contract.

## What this is for

A tracker answers a question about the device that cannot be answered from a
laptop: how long the lamp took to respond, whether an update applied, how long
a servo move actually ran. It writes every observation to the device's own log
and, when configured, forwards the same rows to Autonomous Analytics (AA).

It is **not** an event bus, not a debug logger, and not a place to stream
per-frame data. One row per thing that happened.

## The pieces

| Layer | Path | Yours to touch? |
|-------|------|-----------------|
| Tracker (measures one thing) | `hal/telemetry/<name>.py` | **Yes — write one per question** |
| HAL pipe | `hal/telemetry/client.py` | No |
| Playback hooks shared by trackers | `hal/telemetry/tts_hooks.py` | Only if you add a hook |
| OS ingestion | `POST /api/telemetry/event` (`system/server/telemetry/…`) | No |
| OS pipe | `system/telemetry/telemetry.go` | No |
| AA transport | `system/lib/analytics` | No |

## Adding a tracker

```python
# hal/telemetry/ota_metrics.py
from hal.telemetry import client

client.report("ota_update", {
    "from_version": "1.2.3",
    "to_version": "1.2.4",
    "duration_ms": 48210,
    "outcome": "applied",          # applied | rolled_back | failed
})
```

That is the whole integration. From os-server the equivalent is
`telemetry.Report(telemetry.Event{Name: ..., ID: ..., Params: ...})`.

What the pipe already does, so no tracker reimplements it:

- writes the row to the local log **before** sending
- bounded queue, background sender — `report()` never blocks or raises
- de-duplication by `event_id`
- counts what it dropped or failed to deliver, and rides those counters along
- adds common fields on the os-server side: `os_version`, `device_type`,
  `agent_runtime`, policy flags
- stays off entirely when no endpoint is configured

## Configuration

Both keys live in the body's `.env` (`robots/<device>/rootfs/opt/hal/.env`,
deployed to `/opt/hal/.env`):

| Key | Meaning |
|-----|---------|
| `AUTONOMOUS_ANALYTICS_URL` | Where rows are posted — **and the on/off switch**. Empty (the default) = nothing leaves the device. No built-in fallback endpoint. |
| `AUTONOMOUS_ANALYTICS_ID` | AA authorization key. |

Off does not mean blind: everything is still in the log.

HAL's local JSON includes the `event_id` from the outgoing envelope alongside
the parameters, so original events and amendments can be joined offline.
This adds no transcript or other voice content to telemetry.

```bash
journalctl -u hal -f | grep '\[telemetry\]'        # HAL: every row + POST failures
journalctl -u os-server -f | grep '\[telemetry\]'  # os-server: forwarded / dropped / failed
```

## Rules that cost us bugs to learn

Each of these came from a real defect in the first tracker. They are cheap to
follow and expensive to rediscover.

**1. Never infer a fact from state sampled later.**
Ask "was this true when it happened", not "is this true now". Two bugs came
from breaking it: audio was attributed to whichever turn was open at the time
(not the one that actually owned it), and a muted speaker was detected by
reading the mute flag ten seconds after the fact — by then someone had
unmuted, so a muted turn was recorded as an unanswered one. Both fixes were
the same shape: record the event where it occurs, carry an explicit owner.

**2. Unknown must stay unknown.**
When ownership or a value cannot be established, record `unknown` / `null` and
count it. Do not guess, and do not drop the row. A metric that quietly
discards what it cannot explain reports a better number than reality.

**3. Measure the effect, not the request.**
An HTTP 200 is not proof of an outcome. The voice tracker measures the first
audio frame written to the stream, because `speak_queue()` deliberately
returns success for speech it then drops. Whatever you measure, find the point
where the thing actually happened.

**4. Define the denominator before writing code.**
Which rows count, which are excluded, and why. Every exclusion is reported
with a reason — never silently dropped — so a rate can always be audited.
Failures stay in the denominator; only "this was never a valid sample" is an
exclusion.

**5. A verdict written too early must be correctable.**
Trackers that report on a timer can be wrong when late information arrives.
Emit an amendment row carrying `amends_event_id`, and say in the tracker's doc
that queries must prefer the amendment. Do not leave a known-wrong row
standing.

**6. Incomplete observation is not success.**
If a watch window closes while the thing being watched can still happen, say
so in the row (`observation_complete: false`) and exclude it from the rate as
coverage loss.

**7. Telemetry never degrades the feature.**
Hooks swallow their own exceptions, reporting stays off locks held by
real-time paths, and no product code branches on tracker data. If the tracker
breaks, the device still works — you lose numbers, not behaviour.

## Naming

- Event: `<domain>_<thing>` — `voice_metrics_interaction`, `ota_update`.
- Durations end in `_ms`; the analytics client's second-resolution
  `event_timestamp` is too coarse for latency.
- Enumerated outcomes (`applied` / `failed`), not free text.

## Privacy

Never send transcripts, reply text, raw audio, file contents, or credentials.
Ids, enumerated outcomes and durations only. `user_pseudo_id` is the device
hostname; `platform` is `device`.

## Existing trackers

| Tracker | Question it answers | Doc |
|---------|--------------------|-----|
| `hal/telemetry/voice_metrics.py` | How fast the device acknowledges speech, and whether it ever plays a reply the user moved past | [`docs/voice-metrics.md`](voice-metrics.md) |
| `hal/telemetry/live_voice.py` | Same voice KPIs for realtime (live) sessions: maps each provider turn to one HAL interaction and feeds `voice_metrics` without guessing audio ownership | [`docs/voice-metrics.md`](voice-metrics.md) |
| `system/telemetry/voice_task.go` | Whether an accepted voice/chat task actually executed (start, run binding, lifecycle end or local-intent return) | [`docs/voice-metrics.md`](voice-metrics.md) |

## Validation

```bash
go build ./...
go test ./system/telemetry/ ./system/server/telemetry/... ./system/lib/analytics/
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_telemetry_flag.py -q
```

Voice task tracking emits `voice_metrics_task_started` at OS receipt and run
binding for `voice`, `voice_command`, and `voice_followup`, plus
`voice_metrics_task_execution` at OS lifecycle/local-intent/dispatch-failure
boundaries and HAL realtime return. Merge OS starts and versioned HAL task
snapshots by device + interaction/run before scoring, so OS-only voice paths
count once even without HAL instrumentation. Arbitrary backend runs and
`voice_agent_handled` memory sync do not create tasks. The default reporting
horizon is 0 seconds: unfinished eligible tasks stay in the denominator. “Completed” means execution finished, not semantic correctness
or finished audio playback. See the [KPI-3 agent query/report runbook](voice-metrics.md#agent-runbook-query-and-report-kpi-3).

Chat emits `chat_metrics_task_started` at receipt (including queued requests); sensing emits `sensing_metrics_task_started` only for selected task dispatch, after suppression/queue selection. Both reuse `voice_metrics_task_execution` for terminal evidence, joined within the chosen cohort. MQTT `speak: true` remains voice. See the [chat/sensing AA query and report runbook](task-metrics.md) for eligibility, shared evidence, and `--group chat|sensing` commands.

For `voice_metrics_*`, `chat_metrics_*`, and `sensing_metrics_*`, INFO `[telemetry] delivered` confirms successful AA HTTP
delivery, not warehouse query visibility. Use a warehouse read query/export to
verify the latter; the ingestion key alone does not grant read access.
