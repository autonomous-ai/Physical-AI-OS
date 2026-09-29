# OpenClaw self-replay bug — symptom, cause, workarounds

Playbook for the "same sensing event gets processed twice" phenomenon observed on Pi.

## Symptom

One `motion.activity` or `presence.enter` event from Lamp turns into **two agent lifecycles**:

1. First turn with Lamp-assigned runId (`lamp-chat-NN-<ts>`) — ends with `no_reply`
2. Second turn with a fresh OpenClaw UUID (`657eb6ee-...`), ~1 s later, identical input message

Example observed 2026-04-21:

| Run | When | Input | Outcome |
|---|---|---|---|
| `lamp-chat-105-1776757912265` | 14:51:52 | `[sensing:presence.enter] ... friend (chloe)` | `no_reply`, 3 spurious tool calls (mood log) |
| `657eb6ee-af20-45e5-817c-e14872d32966` | 14:52:03 | Same message (snapshot suffix stripped) | `no_reply`, `tts_suppressed` |

Flow UI shows them as two separate turns with different runIds but visually the same payload.

## Root cause — upstream

Tracked at [openclaw/openclaw#50956](https://github.com/openclaw/openclaw/issues/50956): *"Agent loop does not terminate after final response when Queued messages exist in context — causes full task replay."*

Key quote from the issue:

> After the assistant produces a text-only final response, the loop should stop. Instead it appears to scan the full session chain for any unanswered user message — finds the Queued message — and invokes the model again.

Concretely for our pipeline:

1. Lamp sends `[sensing:presence.enter] ...` as `chat-105`.
2. Agent follows SKILL, emits `[HW:/emotion:...]` markers + `NO_REPLY`.
3. OpenClaw strips the silent token, leaving **no assistant text**.
4. Termination logic mistakes this for "final response missing" → scans the session chain → finds the same user message still "unanswered" → re-invokes the model.
5. The re-invocation gets a fresh UUID run (not `chat-105`) because it's treated as a new turn, not a retry of the previous run.

Replay-length scales with context size — longer sessions produce longer replay chains.

## Why it matters

- **Doubles sensing token cost** — each passive event charges twice for prompt + output.
- **Triggers hallucinated side-effects** on the replay turn. In the 2026-04-21 case the replay ran `POST /api/mood/log {kind:"signal",mood:"sad",user:"unknown"}` even though:
  - the event was a `presence.enter`, which `skills/sensing/SKILL.md` explicitly forbids from calling tools, and
  - the mood "sad" was fabricated with no `emotion.detected` trigger in that turn.
- **Confuses flow correlation** — monitor UI shows two turns for one real event; runId tracking races between Lamp idempotency key and OpenClaw UUID.

## How to spot it in logs

Fastest signal — grep `/root/local/flow_events_YYYY-MM-DD.jsonl`:

```bash
# Find UUID runs whose input is byte-identical to a preceding lamp-chat-* run
jq -c 'select(.node=="chat_input") | {ts, trace_id, msg:.data.message}' \
  /root/local/flow_events_$(date +%F).jsonl \
  | awk '
    /lamp-chat-/ { last_msg=$0; next }
    /trace_id":"[0-9a-f-]{36}"/ && index($0, substr(last_msg, index(last_msg,"msg"))) { print }
  '
```

Or simpler: any run whose `trace_id` is a bare UUID (36 chars, 4 hyphens) with no `lamp-chat-` prefix is a candidate replay. Correlate by timestamp — if it fires <5 s after a `lamp-chat-*` lifecycle_end, it's almost certainly a self-replay.

## Workarounds

### Short-term (no code changes)

- **Accept the double cost** for passive sensing. Not ideal but functional — mood/wellbeing side effects are idempotent at the data layer (wellbeing JSONL appends, mood appends). The worst case is extra spoken nudges, which we have rarely seen.
- **Delete session JSONL + restart** when the session context gets poisoned and replay chains grow (≥3 replays per event). Upstream confirmed: restart alone is not enough — the file must go.

### Medium-term (Lamp-side filter)

Detect the UUID replay and drop it before it reaches the agent runtime. Implemented in the Lamp SSE handler:

1. Track `lastLifecycleEnd` per session — `{runId, endedAt, inputHash}`.
2. On incoming `lifecycle_start` with a new UUID runId, compute input hash.
3. If the hash matches `lastLifecycleEnd.inputHash` **and** the gap is under e.g. 5 s, mark this run as a replay and suppress hardware/tool side effects (but still log to flow for visibility).

Trade-off: miss legitimate rapid duplicate events from the agent side. Acceptable because OpenClaw genuinely doesn't need to re-invoke on the same input.

### Long-term

Upstream fix on #50956. Bump the OpenClaw pin once the fix lands, then remove the Lamp-side filter.

## Related issues

- [#48814](https://github.com/openclaw/openclaw/issues/48814) — pre-send queue check (suppress stale replies when newer messages pending)
- [#42112](https://github.com/openclaw/openclaw/issues/42112) — orphaned toolCall poisons session replay
- [#8785](https://github.com/openclaw/openclaw/issues/8785) — stop-typing signal on NO_REPLY end
- [#67065](https://github.com/openclaw/openclaw/issues/67065) — session-scoped next-turn suppression for managed media
