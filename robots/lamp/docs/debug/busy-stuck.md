# DEV — Lamp Busy-Flag Wedge (Sensing Pipeline Stuck)

Lamp's sensing pipeline goes silent for several minutes: no `[chat.send] >>>`, no new `lifecycle event`, just a stream of `sensing event queued — agent busy ... runId=` (empty runId). OpenClaw side is idle (`active=0 queued=0`). Self-heals after ~5 min via `busyTTL`.

This is **not** a sensing-layer bug — sensing keeps producing events. Lamp is gating them because its `activeTurn` / `IsBusy()` thinks an agent run is still in flight.

## Self-heal signature

```
WARN  busy flag expired — auto-clearing (lifecycle.end likely missed) component=openclaw stuck_for_s=348
INFO  draining pending sensing events component=sensing count=N
```

`busyTTL` is `5 * time.Minute` in `runtimes/openclaw/service_events.go:29`. After that the flag forcibly clears and queued sensing events drain (with high-frequency events coalesced + 60s expiry per `drainPendingEvents`).

## Root cause

OpenClaw heartbeat / memory-flush turns run with **`target=none`** and never emit `lifecycle.end` SSE to Lamp. But the `before_agent_reply` turn-gate hook still fires unconditionally and POSTs `/api/agent/busy` → `SetBusy(true)`. With no SSE clear and no early hook signal, Lamp waits the full TTL.

```
hook (turn-gate) ─POST /busy─▶ Lamp.SetBusy(true)            ┐
OpenClaw heartbeat turn (target=none) ─────────────────────▶ │ no lifecycle.end SSE
                                                             ▼
                              Lamp busy=true for up to 5 min, drops every sensing chat.send
```

Files:
- Hook source: `runtimes/openclaw/hooks/turn-gate/handler.ts`
- Lamp handler: `system/server/agent/delivery/http/handler_api_monitor.go` (`SetBusy`)
- Auto-clear: `runtimes/openclaw/service_events.go` (`busyTTL`, `IsBusy`, `drainPendingEvents`)

## Confirm (3 commands)

```bash
PI=orangepi@<IP>; PASS=<pass>
SSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no $PI"

# 1. Last lifecycle event Lamp saw (should be old):
$SSH "sudo journalctl -u os-server.service --since '20 min ago' --no-pager | grep 'lifecycle event' | tail -3"

# 2. Last /api/agent/busy POST (should be after #1):
$SSH "sudo journalctl -u os-server.service --since '20 min ago' --no-pager | grep '/api/agent/busy' | tail -3"

# 3. OpenClaw heartbeat / memoryFlush near #2's timestamp:
$SSH "sudo grep -E 'isHeartbeat=true|before_agent_reply' /var/log/openclaw/lamp.log | tail -10"
```

Wedged when:
- (2) more recent than (1)
- (3) shows a heartbeat fire close to (2)
- Lamp journal has many `sensing event queued — agent busy` lines after (2) with no chat.send

## Workarounds

- **Wait ≤5 min** — auto-clear fires (`stuck_for_s` in the WARN line tells you how long).
- **Restart Lamp** if urgent: `sudo systemctl restart os-server`. There is no idle endpoint — POSTing `/api/agent/busy` only sets `busy=true` again.

## Real fix paths

1. **Hook side (preferred)** — turn-gate skips `/api/agent/busy` when OpenClaw turn metadata says `target=none` or `isHeartbeat=true`. Edit `runtimes/openclaw/hooks/turn-gate/handler.ts`. This is cheapest and removes the trigger entirely.
2. **Lamp side** — propagate heartbeat marker into `lifecycle.start` payload and have the SSE handler skip `SetBusy(true)` for those. Or shorten `busyTTL` to 60-90s (heartbeat turns finish in ~20s, no point waiting 5 min).

## Risk profile

Frequency increases as session token count climbs toward the memoryFlush threshold. Observed wedge:

| Pi | Date/time | `stuck_for_s` | OpenClaw `tokenCount` | Threshold |
|---|---|---|---|---|
| .38 (orangepi4pro) | 2026-05-06 15:34→15:40 | 348 | 74,731 | 116,000 |

As `tokenCount` approaches threshold, `memoryFlush check` fires `isHeartbeat=true` more often → more wedges. After compaction, threshold resets but a fresh compaction summary risks distorting SKILL rules (see `project_openclaw_compaction_summary_risk` memory).

## Related

- `robots/lamp/docs/debug/sensing-pipeline.md` §8 — short pointer.
- `robots/lamp/docs/debug/openclaw-selfreplay.md` — different stuck pattern (NO_REPLY → UUID self-fire).
- `robots/lamp/docs/debug/sleep-stuck.md` — sleep gate wedge (different layer).
