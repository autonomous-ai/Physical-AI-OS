# DEV — RunId Mis-attribution: Lamp `pendingChatTrace` Single-Slot Overwrite

Playbook for the bug where Lamp's `chat.send` idempotency key (e.g. `lamp-chat-201-1776763410829`) ends up wrapping an agent turn that actually processed a *different* input — a message Lamp sent earlier (`chat-200`, `chat-198`, …) or a Telegram inbound that slipped in on the same session lane.

First detected: 2026-04-21 on OpenClaw gateway `2026.5.7`, session `agent:main:main`.

Fixed in Lamp: see §5. Keep this doc as reference when new `runId` drift shows up.

---

## 1. Symptom

Flow monitor / TTS pipeline attributes a turn's output to the wrong runId. The `tts_send` / `agent_thinking` events under a `trace_id` are semantically disconnected from the `chat_send.message` that shares the same `trace_id`.

Evidence cases on 2026-04-21 (session `agent:main:main`):

| Lamp `chat.send` (intended) | Content actually delivered under that runId | Shift |
|---|---|---|
| `lamp-chat-26-1776759969005` · sensing `presence.enter stranger_75` | ~400-token answer to Leo's Telegram question *"why did it add node-host?"* | +1 (Telegram inbound interleaved) |
| `lamp-chat-201-1776763410829` · sensing `emotion.detected Angry` | ambient-speech reply *"Back on that — yeah. [listening]"* (belongs to `chat-200`) | +1 (pure Lamp burst) |
| `lamp-chat-203-1776763417607` · ambient `lily: right` | mood decision CoT about the Angry event (belongs to `chat-201`) | +2 |
| `lamp-chat-339-1776764806327` · sensing `sound occurrence 1` | *"58k/200k (29%) — 7 compactions."* (answer to Telegram question *"size context đang bao nhiêu"* on `chat-337`) | +2 |

Downstream effects:

- Wrong text spoken on the lamp speaker (in channel-origin turns TTS is suppressed; other paths do reach the speaker).
- Double-reaction: the sensing event eventually gets processed in a later turn, re-triggering skills that were already partially executed.
- Monitor UI groups thinking + response under a runId whose `chat_send` shows an unrelated message — misleading during incident triage.

---

## 2. Root cause — Lamp side, not OpenClaw

Lamp's SSE handler maps an OpenClaw lifecycle UUID back to its device runId by consuming a "pending chat trace" that was stashed at `chat.send` time. Before the fix, the stash was a **single slot**:

`runtimes/openclaw/service.go` (pre-fix):

```go
pendingChatMu      sync.Mutex
pendingChatTrace   string       // ← one slot, overwritten each chat.send
pendingChatTraceAt time.Time

func (s *Service) SetPendingChatTrace(runID string) {
    s.pendingChatMu.Lock()
    s.pendingChatTrace = runID    // ← blindly replaces previous entry
    s.pendingChatTraceAt = time.Now()
    s.pendingChatMu.Unlock()
}
```

Paired with `system/server/agent/delivery/http/handler.go:404-411`:

```go
if payload.Stream == "lifecycle" && payload.Data.Phase == "start" && isLampSession {
    if deviceTrace := h.agentGateway.ConsumePendingChatTrace(); deviceTrace != "" && deviceTrace != payload.RunID {
        h.mapRunID(payload.RunID, deviceTrace)
    }
}
```

When sensing or channel traffic bursts (`chat.send` arriving faster than the agent turn loop processes them), every new send **overwrites** the prior idempotencyKey. By the time `lifecycle_start` fires for the earlier turn, the slot already holds the newer key — Lamp maps OpenClaw's UUID to the wrong device runId and every subsequent turn is shifted.

Replay of `lamp-chat-201` swap:

```
T         Lamp gửi chat-200 "back on that"  → pendingChatTrace = "chat-200"
T + 8 s   Lamp gửi chat-201 "Angry"         → pendingChatTrace = "chat-201"   ← overwrite
T + 10 s  OpenClaw lifecycle_start (turn processing chat-200)
           → ConsumePendingChatTrace() returns "chat-201"
           → map UUID → lamp-chat-201
           → tts_send "Back on that — yeah" attributed to chat-201
```

OpenClaw processes the session lane FIFO correctly; Lamp's correlation is what misaligns.

Introduced by commit `64571f7b` (2026-04-14, *"Fix race condition in OpenClaw UUID → device trace mapping"*) which replaced the previous `flow.GetTrace()` global with this single-slot variable — closing one race while opening a new one for burst sends.

### Why the original commit made sense at the time

Before `64571f7b`, the handler used a **global** `flow.GetTrace()` to carry the device runId across `lifecycle_start`. That global was cleared by `flow.ClearTrace()` inside channel-turn handling, so a concurrent Telegram reply could wipe the sensing trace mid-flight and leave `lifecycle_start` with nothing to map. The old code had explicit comments about keeping `flow.GetTrace()` "active for the duration of the device turn so the Telegram heuristic can work correctly" — the author was already fighting that race.

`64571f7b` moved the state onto `Service` so channel code could not clear it. That fix was correct for its stated goal. The **single-slot** choice was reasonable given the sensing traffic at the time: roughly one event every tens of seconds, agent turns finishing before the next chat.send landed. One pending trace was never overlapped by a second.

### Why the precondition broke

Between 2026-04-14 and 2026-04-21, several sensing changes landed that turned a calm stream into a burst:

- `b207efc` — bypass the 60 s cooldown on `motion.activity` + `emotion.detected`, so these fire every time the upstream filter passes.
- `f76eb72` — allow `motion.activity` + `emotion.detected` to fire tool calls, stretching each turn to 5–10 s.
- New/reactivated event types: ambient voice, `sensing:sound`, richer `presence.enter`, queued replays on busy.

Net effect: at peak, 4–6 `chat.send` can be in flight within a single turn's duration. Each one overwrote the previous `pendingChatTrace`. The race observed on 2026-04-21 is a direct consequence of this shift — the author of `64571f7b` did not model a burst regime.

---

## 3. Evidence sources on the Pi

| Source | What it proves |
|---|---|
| `journalctl -u os-server` filtered by runId | Lamp's `chat.send` payload (intended input) + the UUID→device runId mapping line (`mapped OpenClaw runId to device trace`) |
| `/root/local/flow_events_YYYY-MM-DD.jsonl` | What actually fired under that runId: `tts_send`, `agent_thinking`, `tool_call`, `token_usage`. Content often does not match the `chat_send.message` on the same `trace_id` |
| **`/root/.openclaw/agents/main/sessions/<sessionId>.jsonl`** | Ground truth: every user + assistant entry with UTC timestamps and `thinking` blocks + signatures. Use this to confirm which message the turn actually processed |
| `journalctl -u openclaw` | Session lane diagnostics (e.g. `lane wait exceeded waitedMs=…`), chat.send / chat.history ACKs, connId |

The session JSONL is the decisive file — align its timestamps with Lamp's `chat.send` timestamps to see which message was actually prompt-fed into each agent turn.

---

## 4. Detection queries

Assuming Pi SSH is authorized (per `CLAUDE.md`, always ask first):

```bash
PI=pi@<IP>
PASS=12345
SSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no $PI"
RUN=lamp-chat-<N>-<ms>
```

### 4.1 Confirm the mis-attribution

```bash
# What Lamp intended to send under this runId
$SSH "sudo journalctl -u os-server --no-pager | grep '\[chat.send\] full payload' | grep '$RUN'"

# What actually came back under this runId
$SSH "sudo grep '$RUN' /root/local/flow_events_$(date +%F).jsonl \
      | jq -c 'select(.node==\"tts_send\" or .node==\"agent_thinking\") | {node, text:(.data.text|.[:250])}'"
```

If the `tts_send.text` / `agent_thinking.text` is semantically unrelated to the `chat.send.message`, this bug (or its Telegram-interleave variant) is in play.

### 4.2 Inspect the burst window

```bash
# All Lamp chat.sends in a window — run id + first 100 chars of message
$SSH "sudo journalctl -u os-server --since '<START>' --until '<END>' --no-pager \
      | grep '\[chat.send\] full payload' \
      | sed -E 's/.*idempotencyKey\":\"([^\"]+)\",\"message\":\"([^\"]{0,100}).*/\\1  →  \\2/'"
```

### 4.3 Pull the session ground truth

```bash
SESSION=$($SSH "sudo ls -t /root/.openclaw/agents/main/sessions/*.jsonl" \
          | grep -v checkpoint | head -1)

# Replace TS_LOW / TS_HIGH with the UTC window around the lifecycle
$SSH "sudo jq -c 'select(.type==\"message\" and .message.role==\"user\" \
                  and .timestamp >= \"<TS_LOW>\" and .timestamp <= \"<TS_HIGH>\") \
                  | {ts:.timestamp, preview:(.message.content | tostring | .[:250])}' \
      $SESSION"
```

A user entry whose content matches the `tts_send.text` answer (but whose timestamp pairs with the *previous* chat.send) confirms the shift.

### 4.4 Session lane contention

```bash
$SSH "sudo journalctl -u openclaw --no-pager | grep 'lane wait exceeded'"
```

Recurring `lane=session:agent:main:main waitedMs=…` lines indicate the queue is piling up and the race window is frequent.

---

## 5. Fix — landed

`runtimes/openclaw/service.go`: replace the single-slot `pendingChatTrace` with a FIFO `pendingChatQueue []pendingTrace`. `SetPendingChatTrace` appends, `ConsumePendingChatTrace` pops the head, stale entries (older than `pendingChatTTL = 2 * time.Minute`) are dropped from the head before popping. Public API unchanged so the SSE handler needs no edit.

### Flow after the fix (end-to-end)

```
Lamp.sendChat(msg)
  └─ WS write OK
  └─ SetPendingChatTrace(K_n)                      queue: [... , K_n]
                                                  ▲ tail

OpenClaw gateway
  └─ session:agent:main:main lane enqueue (FIFO)
  └─ agent turn loop pulls head → lifecycle_start(UUID_n)
                                                     │
Lamp.SSE handler                                     ▼
  └─ isLampSession=true, phase=start
  └─ ConsumePendingChatTrace() → K_n                queue: [...]
  └─ mapRunID(UUID_n, K_n)
  └─ runIDMap[UUID_n] = K_n

subsequent events for this turn
  └─ resolveRunID(UUID_n) → K_n                    (flow / JSONL / monitor key-stable)

lifecycle_end
  └─ flush assistant text → TTS attributed to K_n ✓
```

Alignment invariant: if OpenClaw pulls the session lane FIFO and Lamp appends FIFO in `chat.send` order, then N-th `lifecycle_start` pairs with N-th `chat.send`. Both halves verified — OpenClaw FIFO confirmed from session JSONL insertion order (`lamp-chat-200` inserted at `09:23:32.260Z`, `lamp-chat-201` at `09:23:39.273Z`, matching send order).

### Design confidence — same pattern already in the handler

The existing `h.cronFireExpected []int64` queue at `handler.go:419-444` uses **the exact same head-drop-stale → pop pattern** to correlate cron "started" events with their lifecycle UUID:

```go
// Drop stale entries from the head.
idx := 0
for idx < len(h.cronFireExpected) && h.cronFireExpected[idx] < cutoff {
    idx++
}
h.cronFireExpected = h.cronFireExpected[idx:]
if len(h.cronFireExpected) > 0 {
    startedAt := h.cronFireExpected[0]
    h.cronFireExpected = h.cronFireExpected[1:]
    // ...
}
```

The fix mirrors this style for consistency with prior engineering choices in the same file.

### Edge cases verified

- **Burst sends (A, B, C within one turn)**: queue = [A,B,C] → pops align to lifecycle_start order → ✓ fixed.
- **Slow turn (send A, wait 30 s, send B)**: queue = [A] → pop A → queue = [B] → pop B → ✓.
- **Stale entry (send A, OpenClaw drops it silently, 2+ min later send B)**: on B's lifecycle_start, head (A) is stale → drop → pop B → ✓.
- **Stale head + fresh tail** (send A, then 2+ min later send B, then lifecycle for B): drop-while-head-stale loop drops A, pops B → ✓.
- **Concurrent Set + Consume**: guarded by `pendingChatMu`.
- **Memory growth** on slice tail reslicing: bounded — TTL drops stale entries from head, so queue length tracks in-flight send count (~max 10 at peak observed).

### Residual edge case: Telegram interleave

If a Telegram user sends a message into the same session between Lamp's `chat.send` and the matching `lifecycle_start`, OpenClaw runs the Telegram turn first; Lamp's FIFO still pops the head (a stale Lamp key) and mis-attributes the Telegram response to Lamp's pending sensing. Observable as a +1 shift with Telegram content landing under a `lamp-chat-*` runId (case `lamp-chat-26` above).

Two ways to close this:

1. **OpenClaw-side**: have `chat.send` response echo the UUID OpenClaw assigned, so Lamp maps `idempotencyKey ↔ UUID` directly without guessing on `lifecycle_start`. Requires upstream change — consider filing against openclaw.
2. **Lamp-side guard**: enrich `lifecycle_start` mapping with a secondary check (e.g. first `chat_input` event payload), only popping the queue once we've confirmed the turn is Lamp-originated. More complex; defer until Telegram-interleave shifts are seen in practice after this fix ships.

---

## 6. Related memory / context

- `project_runid_uuid_vs_lamp_chat.md` — documents the invariant "sensing always uses `lamp-chat-*`; UUIDs are Telegram/cron/OpenClaw-initiated". This bug violated the invariant silently: a `lamp-chat-*` runId could wrap any other input that happened to run ahead in the queue.
- `project_guard_broadcast_evolution.md` — prior "Haiku ignores SKILL" instability. Some of the observed misbehaviour likely included runId drift on top of real skill compliance issues; now separable.
- Thinking leak (`[Latest decision was sad…]` showing up in `tts_send`) is a separate class of bug — the model producing CoT as `text` instead of native thinking content. Orthogonal to this runId fix.

---

## 7. Status

- **Detected**: 2026-04-21 — `lamp-chat-26`, `lamp-chat-201`, `lamp-chat-203`, `lamp-chat-337/339` (session `agent:main:main`).
- **Root cause**: Lamp `pendingChatTrace` single-slot overwrite, introduced by commit `64571f7b` on 2026-04-14. Precondition (low sensing burst rate) broke between 14/4 and 21/4 as sensing pipeline gained ambient voice, sound events, tool-calling emotion/motion skills.
- **Fix landed**: FIFO queue + TTL head-drop in `runtimes/openclaw/service.go` + interface doc refresh in `system/domain/agent.go`. Public API unchanged. `GOOS=linux GOARCH=arm64 go build ./...` clean.
- **Deploy**: not yet. `make os-build` → scp `os-server` to Pi → `sudo systemctl restart os-server`.
- **Post-deploy verification**: re-run §4.1 / §4.2 on a fresh burst window; expect `tts_send.text` to align with `chat_send.message` under the same `trace_id`.
- **Follow-up**: Telegram-interleave variant still open — see §5. Consider upstream openclaw change to echo UUID in `chat.send` response.
