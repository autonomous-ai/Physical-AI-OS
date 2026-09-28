# Flow Monitor

External history runs with a valid `device-chat-context-` ID and attributed `[external-context]` / `[HANDLED]` / `[REPLY]` envelope display as **History sync**, with **Harness → Main** or **Realtime → Main** (or another external source). Cards show the original question and reported answer as **Context**, not a new TTS response. Agent name is available on the route tooltip; raw metadata remains in event details. Parsing uses complete chat-send data when chat-input previews are truncated, and does not change lifecycle status.

Harness-only voice cards use `sensing_input.data.route: "harness_only"` to display **Harness** instead of **Agent**. After merging events by run ID, a nonempty `harness_response` closes that same turn and supplies its output and Response pipeline details, including history loaded from JSONL. A successful input dispatch alone stays active. Harness replies are not stitched onto nearby inputs with different run IDs; existing error states remain errors. DONE means the final response arrived, not that audio playback or the requested real-world action was verified.

Regression checks: `node --test system/web/tests/flow-harness.test.cjs` from the repository root (after installing web dependencies).

Codex steering records `turn_merged` under the follow-up run ID with
`data.parent_run_id` pointing to the active host. The follow-up keeps its own
input card and displays the shared host pipeline when selected. Its status
follows the host until its own terminal event arrives; an unavailable parent is
identified as outside loaded history. Voice/web requests wait for actual
completion, while silent realtime history sync may finish at acknowledgement.
The link is available for new recorded events; older traces without the marker
are not retroactively inferred.
The shared view also includes the selected follow-up's Harness result, TTS and hardware delivery events, without duplicating its input or lifecycle.

The Flow Monitor is an observability layer for tracking agent turns end-to-end. It records events to daily JSONL files (`local/flow_events_YYYY-MM-DD.jsonl`) and streams them to the web UI via SSE.

**Important**: The flow monitor is purely observational. It does NOT affect device behavior, agent communication, TTS, LED, or any business logic.

## Architecture

```
HAL (Python)                       OS Server (Go)                     Web UI (React)
  sensing event ──POST──→ SensingHandler ──flow.Start/End──→ JSONL file
                            │                                    ↓
                            └─ agentGateway.SendChat ──→ Agentic Runtime (WS)
                                                           │
                          SSE Handler ←── WS events ───────┘
                            │
                            ├─ flow.Log("lifecycle_*") ──→ JSONL file ──→ /flow-stream (SSE)
                            ├─ flow.Log("tool_call")                         ↓
                            ├─ flow.Log("tts_send")                    system/web/.../Monitor.tsx
                            └─ monitorBus.Push() ──→ /openclaw/events (SSE)  └─ groupIntoTurns()
```

## Per-Event Run ID (refactored from global trace)

### Before (global trace)
```go
flow.SetTrace(runID)           // set global, affects ALL subsequent events
flow.Log("lifecycle_end", data) // picks up global trace
flow.ClearTrace()              // clear global
```

Problems:
- Concurrent turns override each other's trace
- Server restart loses in-memory trace
- `ClearTrace()` in goroutine races with next `SetTrace()`

### After (per-event run ID)
```go
flow.Log("lifecycle_end", data, payload.RunID)  // explicit per-event
flow.Log("tool_call", data, payload.RunID)      // each event carries its own ID
```

- `Start()`, `End()`, `Log()` accept optional variadic `runID` parameter
- If provided, overrides the global trace for that event only
- Global `SetTrace`/`GetTrace` retained for the Telegram-detection heuristic
- `ClearTrace()` decrements active trace (ref-counted), called after runtime `lifecycle_end`

### Telegram Detection Heuristic

When `lifecycle_start` arrives without an active device trace (`flow.GetTrace() == ""`), the handler checks if it's a channel-initiated turn (Telegram/Slack). Device-originated `chat.send` turns are excluded via `lamp-chat-*` (and legacy `lamp-sensing-*`) so they are not mis-labeled as Telegram when the trace was lost.

#### Fetching user message content via `chat.history` RPC

The runtime's chat stream **never broadcasts `role:"user"` events** — it only emits `role:"assistant"` (delta/final/error). To get the user message text and sender name, the OS server calls the `chat.history` WebSocket RPC on the same WS connection used for events:

```
→  {"type":"req","id":"history-1","method":"chat.history",
    "params":{"sessionKey":"agent:main:telegram:group:-5139766247","limit":20}}

←  {"type":"res","id":"history-1","ok":true,
    "payload":{"sessionKey":"...","sessionId":"...","messages":[
      {"role":"user","content":[{"type":"text","text":"dừng phát nhạc đi"}],
       "senderLabel":"Leo (158406741)"},
      {"role":"assistant","content":[...]},
      ...
    ],"thinkingLevel":"low"}}
```

Implementation details:

- **Async goroutine**: The fetch runs in a separate goroutine because calling it synchronously inside the WS read loop handler would deadlock (the read loop blocks waiting for the handler to return, but the RPC response can only arrive after the handler returns).
- **Pending RPC tracking**: `pendingRPC map[string]chan json.RawMessage` in `runtimes/openclaw/service.go` matches `type:"res"` frames to waiting callers by request ID. `dispatchRPCResponse()` hooks into the read loop before event handling.
- **Two-phase emit**: First `chat_input` fires immediately with a neutral `[chat]` placeholder (no message yet). After the goroutine gets the history, a second `chat_input` fires with the actual message text and a label chosen by `senderLabel` / message-prefix inspection — the UI picks up the one with content.
- **Label routing (second emit)**: (1) `senderLabel` non-empty → `[telegram:Gray]` (real channel user). (2) `senderLabel` empty + message matches a device-internal prefix → `[voice]` / `[emotion]` / `[activity]` / `[wellbeing]` / `[music]` / `[sensing]` / `[system]` (sensing or voice event the device posted via chat.send that OpenClaw merged into this UUID host turn via steer mode). (3) Otherwise → generic `[chat]`. Previously every UUID channel-turn was unconditionally labelled with the configured channel (`[telegram]`), mis-attributing steer-merged self-fire turns to Telegram.
- **Best-effort**: 3-second timeout. If the fetch fails, the turn stays at the generic `[chat]` placeholder — better than mis-attributing to a specific channel.
- **Heartbeat noise**: OpenClaw heartbeat cron (every 30m) also triggers `lifecycle_start`. The last `role:"user"` message in those turns will be the heartbeat system prompt (starts with `"System:"`), not a real user message.
- **Token usage**: `chat.history` is also called on `lifecycle_end` to fetch token usage. OpenClaw `lifecycle_end` events do not include `usage` data. The last `role:"assistant"` message in the history response contains `usage: {input, output, totalTokens, cacheRead, cacheWrite}` for the completed turn. This is emitted as a `token_usage` flow event with `source: "chat_history"`. Only the NEWEST assistant message is considered, and only when its `timestamp` is fresh (≤30s old; one 2s retry) — the fetch races OpenClaw persisting the reply, and walking back to any older assistant message used to attribute the PREVIOUS turn's usage (and thinking) to self-fired runs like heartbeats. The same staleness gate (120s) applies to the lifecycle_start `chat.history` fetch that labels channel turns, so a heartbeat no longer clones the previous turn's input text. Heartbeat runs (reply `HEARTBEAT_OK`) additionally emit a `heartbeat_run` flow event; the web Flow section classifies those turns as `heartbeat`. The turn card's token footer shows `↓in ↑out R<cacheRead> Σtotal` — cache read is the bulk of the per-turn context, and the Autonomous backend bills cached reads at FULL price, so Σtotal (in + out + cache) is the billed count (no 0.1× discount anywhere in the monitor).

## Run ID Format & Mapping

```
sendChat() generates:
  reqID           = "chat-1"                       (WS message ID, local counter)
  idempotencyKey  = "lamp-chat-1-1774841927380"    (sent to OpenClaw, globally unique; not "sensing-only" — any outbound chat from the device uses this prefix)

sendChat returns idempotencyKey → used as trace_id in flow events
```

**OpenClaw run_id behavior depends on version:**
- **5.2** (and rare paths in 5.4): assigns its own UUID (e.g., `a8a51f3c-b44f-434b-a4c9-cd1a2a1e3c30`) — the OS server must map UUID → idempotencyKey.
- **5.4** (majority): echoes the idempotencyKey directly as the runId (verified in `src/gateway/server-methods/chat.ts:2002`, `clientRunId = p.idempotencyKey`). The lifecycle runId IS already the device trace; no map is needed.
- **Mixed within one session**: a single chat.send can produce up to two lifecycle phases — Phase 1 with the echoed idempotencyKey, Phase 2 with a fresh UUID for the actual embedded run (drain/burst pattern). Both must be handled.

**Solution: pending traces matched by message** in the SSE handler:
1. Sensing handler allocates `NextChatRunID()`, then calls `flow.SetTrace(idempotencyKey)` **before** `flow.Start("sensing_input", ...)` so the JSONL `enter` line uses the same `trace_id` as `chat_send` for that POST. (Calling `SetTrace` only after `SendChatMessage` used to leave `enter` tagged with the **previous** turn's id — ghost turns and mismatched Pair exports.)
2. Before writing `chat.send`, the OS server records the idempotencyKey and transmitted message in `pendingChatBuf`; write failure removes the trace. Routing trace retention remains 2 minutes and the pending-send busy window remains 30 seconds. A separate telemetry buffer retains at most 1024 traces for 24 hours.
3. On `lifecycle_start` the handler branches on `payload.RunID` format:
   - **Device-format** (`device-chat-*`, including legacy `lamp-chat-*`): remove the matching pending trace by run ID; no mapping is needed.
   - **UUID**: fetch `chat.history` for the device session. Runtime routing retains its existing trimmed exact/prefix matching and oldest-match behavior. Separately, telemetry accepts only a unique exact trimmed match and stores its alias outside the routing map; missing/ambiguous evidence does not inherit a routing guess. History fetch failure still leaves correlation unresolved.
4. All subsequent **agent-stream** events (`lifecycle`, `tool`, `thinking`, `assistant` deltas, `tts_send`) use `resolveRunID(payload.RunID)` so `trace_id` matches the device key.
5. **Chat stream** events (`case "chat"`: user/assistant text from the parallel chat feed) also call `resolveRunID` for `flow.Log` and monitor `RunID`. Without this, OpenClaw could emit the **UUID** in chat payloads while JSONL from step 4 used the **device id** — the Monitor would split one turn into two IDs.

### Correlation logs (grep: `flow correlation`)

Structured `slog.Info` lines for end-to-end ID alignment (device idempotency key = `lamp-chat-*`):

| `op` | `section` (when set) | When |
|------|------------------------|------|
| `ws_chat_send` | `lamp_to_openclaw_ws` | Every `chat.send` from the device (`device_run_id` = idempotency key). |
| `hal_agent_out` | `hal_to_openclaw` | Sensing handler after `SetTrace` + `agent_call` (same `device_run_id`). |
| `openclaw_uuid_map` | `openclaw` | `lifecycle_start`: OpenClaw UUID stored → device id. |
| `chat_run_resolve` | `openclaw_chat` | Chat stream event where `resolveRunID` changed the id (UUID → device). |

## Turn Grouping (Frontend)

`groupIntoTurns()` in `Monitor.tsx` groups events into turns:

1. **Turn start detection**: `sensing_input`, `chat_input`, `ambient_action`, `schedule_trigger`
2. **Run ID grouping**: events with same `runId` stay in same turn
3. **Fragment merging**: turns sharing same `runId` get merged (handles split events)
4. **Placeholder type upgrade**: channel `chat_input` fires twice (see "Two-phase emit" above). The first emit pins `turn.type = "chat"` from the `[chat]` placeholder. When the second emit lands with the real message, `isTurnStart` re-derives a specific type (`emotion.detected`, `speech_emotion.detected`, `voice`, `telegram`, …) from the message prefix; `groupIntoTurns` upgrades `turn.type` only if it was still the `"chat"` (or `"unknown"`) placeholder — preserving any already-specific classification. The `[speech_emotion]` prefix maps to `speech_emotion.detected` and is categorized under the `mic` source (voice-driven), not `cam`, even though the label contains "emotion". `voice_agent_handled` (realtime-handled voice turn replayed to the main agent) is also in the `mic` source group — previously it belonged to no group, so the Mic/Cam source toggles never hid it and it appeared to "belong" to whatever category was left enabled.
5. **Stitching**: orphaned output-only turns merge with nearby input-only turns (handles server restart splits)
6. **Session breaks**: >60s gap between turns marks a session boundary

### Stitching Rules

| Previous Turn | Current Turn | Condition | Action |
|---|---|---|---|
| Telegram fallback (no message) | Agent output | <30s gap | Merge |
| Sensing input (no output) | Orphan output (no input) | <30s gap | Merge |

## Turn Pipeline (SVG `FlowDiagram`)

Rendered by `FlowDiagram` in `system/web/src/pages/Monitor.tsx`. The diagram is **observational only** (zoom/pan, node highlights from recent events). Drag with a mouse or one finger to pan; pinch with two fingers (or use the in-canvas minus/plus controls) to zoom, and use reset to restore the default view. On phones, the LLM/tool stream is rendered as pure SVG text for reliable display; **LLM / Tool / Curl details** opens a responsive native sheet for long payloads and node output. Three **tinted cluster** regions group nodes:

| Region | Color (theme) | Stages |
|--------|----------------|--------|
| **OS Server** | Teal (`--lm-teal`) | `intent_check`, `local_match`, `schedule_trigger`, `lamp_gate` |
| **HAL** | Amber (`--lm-amber`) | `mic_input`, `cam_input`, `hw_camera`, `hw_emotion`, `hw_led`, `hw_servo`, `tts_speak` |
| **OpenClaw** | Blue (`--lm-blue`) | `agent_call`, `telegram_input`, `tool_exec`, `agent_thinking`, `agent_response`, `tg_out` |

### OS Server (top band)

- **Intent** and **Local** sit on the **same top row** (left to right).
- **Cron** (`schedule_trigger`) is an **OS server** stage (timer owned by the OS server, not OpenClaw). It shares the **same top `y`** as Intent / Local but uses **`x` aligned with `agent_call`** so Cron → Agent reads as a **vertical column** in the SVG.
- Cron is **not** inside the OpenClaw cluster; only the shared `x` is for layout.

### HAL (left column)

- **MIC** and **CAM** are input nodes (top of HAL section). The lower **CAM**
  diamond is separate and represents an agent tool calling
  `GET /camera/snapshot`.
- Output nodes are stacked vertically in a single column:
  - **EMO** (`hw_emotion`) — `/emotion` calls (coordinated LED + servo + display eyes)
  - **LED** (`hw_led`) — `/led/solid`, `/led/effect`, `/scene`, `/led/off`
  - **SERVO** (`hw_servo`) — move or animate servos: `/servo/aim`,
    `/servo/play`, `/servo/nudge`, `/servo/search`, `/servo/demo`. The node
    detail shows the actual agent command/API call for the selected turn.
    Reads (`/servo/position`, `/servo/status`, `/servo/bearing`) do not count —
    a `hw_servo` event means the lamp did something a person could see.

  **Hardware events are matched on the resolved endpoint, not on raw shell
  text.** A tool call's arguments are scanned for `127.0.0.1:500[01]/<path>`
  (`hwPathFromToolArgs` in `handler_event_agent.go`) and the `/emotion` and
  `/servo/*` branches compare against that path. The previous substring test
  turned `cat …/skills/emotion/SKILL.md` into a `hw_emotion` + `led_set` pair
  for a turn in which the lamp did nothing. The `/led/*` and `/audio/play`
  branches still use the substring form — same weakness, no phantom observed
  there yet.

  **`hw_failed`** — a `[HW:...]` marker the OS tried to fire whose POST failed
  at the transport: the 5 s client timeout, a refused connection. Carries
  `path`, `args`, `run_id` and `error`. Before this event existed that branch
  returned before any `flow.Log`, so a 40 s body movement left nothing in the
  monitor at all (`device-chat-44`: a search marker fired, HAL swept the room,
  the timeline showed an idle lamp). Deliberately **not** a cancellation: it
  lights the OS-gate node and adds a `⚠ → HW call failed` line to its detail,
  but it does not set the turn's cancelled badge or turn the TTS node red —
  those mean the *user* silenced the turn, and a timeout must never read as
  the user's doing.
  - **CAM** (`hw_camera`) — `GET /camera/snapshot`; its saved result is
    rendered as a clickable thumbnail so operators can debug the exact frame
    returned to the agent (including an agent workspace image such as
    `cam_face3.jpg`, not a newly captured preview). The thumbnail needs the
    tool's arguments AND its result, which arrive on different events on the CLI
    runtimes (codex and friends send `arguments` only on `start`, `result` only
    on `end`), so the args are carried across by `toolCallId` — see
    `rememberToolArgs` in `system/server/agent/delivery/http/camera_snapshot.go`.
  - **TTS** (`tts_speak`) — `/voice/speak`, text-to-speech output
- These represent direct hardware calls from OpenClaw tools that bypass the OS server.

### OpenClaw layout rules (column + row)

These are the **stable rules** for nodes inside the OpenClaw rectangle; `positions` in `Monitor.tsx` follow this grid.

**Columns (left → right)**

| Col | Stages |
|-----|--------|
| **1** | Tool Exec, Response (stacked — Response under Tool) |
| **2** | Agent Call (top) → Event Pipeline rect (middle) → Response (bottom). The pipeline contains rows for thinking / assistant / tool / lifecycle / compaction / error events in order; see `docs/debug/flow-monitor-pipeline.md` for aggregation rules and the rationale for collapsing the previous 3-node `LLM Start / Thinking / Tool Exec` chain into one rect. |
| **3** | Telegram In (`TG IN`) |

**Rows (top → bottom)**

| Row | Rule |
|-----|------|
| **1** | **Agent** and **TG In** share one horizontal row (TG → Agent). |
| **2** | **Thinking** and **Tool** share one horizontal row (flow Think → Tool, left to right). |
| **3** | **Response** under column 1 (below Tool). |

**ASCII grid (OpenClaw only)**

```
              Col1        Col2        Col3
         ┌──────────┬──────────┬──────────┐
    Row1 │          │  Agent   │  TG In   │
         ├──────────┼──────────┼──────────┤
    Row2 │   Tool   │ Thinking │          │
         ├──────────┴──────────┴──────────┤
    Row3 │   Response (under Tool)        │
         └────────────────────────────────┘
```

### Approximate coordinates (for layout maintenance)

Values are the **node center** `(x, y)` in the SVG view box (see `positions` in `Monitor.tsx`). Adjust clusters if you move nodes.

| Stage | `(x, y)` | Note |
|-------|----------|------|
| `intent_check` | `(80, 50)` | OS server top |
| `local_match` | `(200, 50)` | OS server top |
| `schedule_trigger` | `(800, 50)` | OS server top; `x` = Agent column |
| `lamp_gate` | `(400, 570)` | OS server; between HAL and OpenClaw |
| `mic_input` | `(-40, 240)` | HAL input |
| `cam_input` | `(80, 240)` | HAL input |
| `hw_camera` | `(200, 345)` | HAL output; agent camera API call |
| `hw_emotion` | `(200, 480)` | HAL output; emotion calls |
| `hw_led` | `(200, 615)` | HAL output; LED control |
| `hw_servo` | `(200, 750)` | HAL output; servo motor |
| `hw_audio` | `(200, 885)` | HAL output; audio playback |
| `tts_speak` | `(200, 1020)` | HAL output; TTS |
| `agent_call` | `(800, 240)` | OpenClaw row 1 |
| `telegram_input` | `(1000, 240)` | OpenClaw row 1 |
| `tool_exec` | `(600, 390)` | OpenClaw row 2, col 1 |
| `agent_thinking` | `(800, 390)` | OpenClaw row 2, col 2 |
| `agent_response` | `(600, 570)` | OpenClaw row 3, col 1 |
| `tg_out` | `(1000, 570)` | OpenClaw row 3; Telegram output |

### Edges

```
mic_input → intent_check → local_match → hw_emotion / hw_led / hw_servo / tts_speak
cam_input → intent_check → agent_call
schedule_trigger → agent_call
telegram_input → agent_call
agent_call → [Event Pipeline rect — thinking/assistant/tool rows] → agent_response
tool_exec → hw_emotion         (OpenClaw /emotion call → HAL)
tool_exec → hw_led             (OpenClaw /led/* or /scene call → HAL)
tool_exec → hw_servo           (OpenClaw /servo/* call → HAL)
tool_exec → hw_camera          (agent GET /camera/snapshot → HAL; saved frame shown below node)
tool_exec → lamp_gate          (OS server listens: pause ambient if LED; TTS-vs-music ordering is handled in HAL, not suppressed here)
agent_response → lamp_gate     (OS server accumulates assistant text for TTS)
agent_response → tts_speak     (Direct TTS from response to HAL)
agent_response → tg_out        (Telegram/Slack output)
lamp_gate → tts_speak          (Gate passes if not suppressed → HAL TTS)
```

If the device speaker is muted, HAL answers the TTS call with HTTP 200 `{"status":"suppressed"}` and plays nothing — surfaced as the `tts_muted` flow event (see below).

**Elbow routing**: Edges from `local_match` to output nodes (hw_emotion, hw_led, hw_servo, tts_speak) use elbow paths routed to the **left** of the output column to avoid crossing intermediate nodes.

### Event → node labels (runtime detail boxes)

Node info extracted from turn events:
- `sensing_input` → Sensing node (type + message). Detail: `{ type }`.
- **The `look` frame rides the voice turn.** When the realtime `look` tool captures, HAL copies the frame to `/var/lib/hal/snapshots/sensing_look/` (`hal/realtime/look_monitor.py`, newest 20 kept) and appends a `[snapshot: ...]` marker to the message that turn already sends — so the picture appears **inside the turn that asked for it**, next to the transcript and the reply, instead of as a separate event with no question attached. os-server strips the marker before the text reaches the model but keeps it in the flow JSONL, exactly as `motion.activity` does. Unlike a `motion.activity` snapshot, **this frame IS what the model saw**, making it the artefact to compare against the model's answer. (A `look.capture` monitor-only branch still exists in `handler.go` from the earlier design; HAL no longer sends it.)
- `chat_send` → outbound `chat.send` from the device. Detail: `{ type, run_id, has_session, has_image, image_bytes, message }`. `type` is `"user"` for real user / sensing-driven input, or `"system"` for internal notifications (skill watcher, wake greeting). The WS RPC payload sent to OpenClaw is identical in both cases — `type` only labels the flow event so the UI can distinguish them. Auto-compact does **not** emit a `chat_send`; it calls the `sessions.compact` RPC directly via `CompactSession`.
- `sound_tracker` → pushed by HAL Python directly via `POST /api/monitor/event`. Appears alongside `sensing_input` turns to show escalation state:
  - `{ action: "silent", occurrence: 1 }` — forwarded, agent stays silent
  - `{ action: "persistent", occurrence: 3 }` — forwarded, agent will speak
  - `{ action: "drop" }` — dropped by dedup or suppression window
- `chat_input` → Telegram In node
- `intent_match` → Local Match node
- `lifecycle_start` → Agent Call node + first row in the Event Pipeline.
- `tool_call` → one Event Pipeline row per tool invocation, kind=`tool`,
  label=`tool · <name>`, with `start`/`result` phases collapsed into the
  row's duration. Outgoing HW edges (LED / servo / emotion / audio /
  lamp_gate) anchor at the pipeline's right edge.
  - **Echoed-marker rescue (2026-07-23):** an `[HW:...]` marker only reaches
    HAL when the agent emits it as **reply text** (the Go interceptor runs
    `extractHWCalls` on the assistant message). Occasionally the agent instead
    wraps it in a shell tool call — e.g. `echo '[HW:/audio/play:{...}]'` — which
    only prints to the sandbox stdout and never fires, yet the tool args contain
    the marker string so the HW node (e.g. `hw_audio`) still lights up: the node
    looks active but nothing played. `fireEchoedHWMarkers`
    (`handler_hw.go`, called from both the device-chat `tool` path in
    `handler_event_agent.go` and the `session.tool` path) now detects any
    `[HW:...]` markers in the tool args and fires them for real, logging a WARN.
    It matches only the `[HW:/path:{json}]` grammar, so a legitimate
    `curl .../audio/play` (no marker) is untouched and still plays via its own
    request. When the rescue fires, the cosmetic args-only HW node emit is
    skipped so the node isn't duplicated.
- `lifecycle_end` → Response node + final row in the Event Pipeline.
- `tts_send` → TTS Speak + Output nodes. Output text is read from `detail.data.full_text` (the complete reply) with fallback to `detail.data.text`. When the agent's first sentence is streamed to TTS mid-turn (`tts_stream_send`, sent early for lower latency), `data.text` holds only the **remainder** (sentence 1 sliced off so it isn't spoken twice); `data.full_text` carries sentence 1 + remainder so the web chat and flow Output show the full reply. `data.streamed_len` is the byte offset where the remainder begins.
- `tts_suppressed` → 🔇 marker in the gate column. `data.reason` discriminates: `channel_run` (real Telegram user turn — detected by `tg-` runID prefix synthesised in the `session.message` handler, or `channelRuns` map mark from chat.history fallback; reply fans out via OpenClaw session instead of the device speaker), `already_spoken` (built-in tts tool already routed), `voice_agent_handled` (realtime voice agent already spoke this turn), `web_chat` (typed chat — reply shown in the chat UI only; the reason string stays `web_chat` for MQTT `mqtt_chat` turns too, since both mark the run through `MarkWebChatRun`). Emitted *instead of* `tts_send` when the actual `SendToHalTTS` call is skipped — prevents the UI from misleadingly claiming TTS happened. Note: there is no `music_playing` reason — playing music no longer suppresses the spoken reply; the OS server always sends the reply TTS and HAL serializes it before music (the reply speaks first, then music plays). Classifier uses positive evidence only: UUID runs from OpenClaw steer-mode self-fire, cron fires, and heartbeats are NOT `channel_run` and DO speak on the device.
- `tts_muted` → TTS node tinted red (same as suppressed); detail panel shows "🔇 speaker muted — reply not spoken", gate line "🔇 → TTS muted (speaker)". Emitted right *after* `tts_send` on the same run: the reply was sent to HAL, but the device speaker is muted, so HAL answers HTTP 200 `{"status":"suppressed"}` and does NOT synthesize or play anything (no TTS API call spent). The Go HAL client (`lib/hal` `SpeakReply`/`SpeakQueueReply`) decodes that body into the sentinel error `hal.ErrSpeakerMuted`, and the handler logs `tts_muted` `{run_id, text}`. Unlike `tts_suppressed` (a Go-side decision made *before* sending, with no accompanying `tts_send` — the chat text comes from the suppress event itself), the reply text still reaches the web chat via `tts_send`; only the audio stays silent.
- `token_usage` → Response node (token counts).
- `cot_leak_filtered` → emitted at lifecycle:end when the CoT-leak filter dropped sentences from the reply. `data.dropped` is the sentence count, `data.preview` a bounded preview of what was removed. See "CoT-leak filter" below.

### CoT-leak filter (agent path)

Some models behind openclaw/hermes (notably DeepSeek) emit their English planning monologue as plain assistant text ahead of the real reply ("The `[emotion_context]` shows … Route = **music**. I need to log the signal … [nhẹ nhàng] Có vẻ hơi trầm …"). `server/agent/delivery/http/cot_leak_filter.go` — a Go port of HAL's `drivers/voice/_internal/cot_leak_filter.py` (which only guards the Gemini Live transcript path) — drops those sentences before the text reaches TTS, the web chat (`full_text`), and channel fan-out (Telegram DM/broadcast, Slack final reply). Same three tiers as the Python filter (TRIGGER markers → CoT mode on + drop; SECONDARY markers drop only in CoT mode; CoT-mode continuation drops English-looking sentences on non-English devices, quoted drafts, bare plan runts, fuzzy near-duplicates), plus a Go-side TRIGGER addition: snake_case identifiers (`emotion_context`, `telegram_id`, …), which the DeepSeek corpus opens with. Reply language comes from `config.json` `stt_language`; unset → English mode (marker tiers only). Applied at three points:

- **First-sentence stream** (`tryFirstSentenceFlush`): the flush candidate is filtered with fresh state each attempt; if everything so far is CoT the flush defers (streamed offset NOT marked) so a later clean sentence still streams early.
- **lifecycle:end**: the full text is filtered fresh (feeds `full_text`, DM/broadcast, Slack reply); the TTS remainder is filtered by a second instance seeded with the already-streamed prefix so CoT mode and the dedup memory carry across the boundary. `text` is only replaced when something was dropped, so clean turns keep their original whitespace.
- **Channel-turn finalize** (`session.message` path): channel turns never TTS, but their text reaches the web chat / Flow Monitor via `chat_response` and `tts_suppressed` — filtered there too.

Raw deltas in `agent_last_token` stay unfiltered for debugging. Slack mid-turn streaming (`chat.appendStream`) is not filtered (append-only diffing can't retract text); the final Slack reply is.

For delegated music requests, `skills/music/SKILL.md` requires leading HW markers followed by one short confirmation; song selection, noisy-transcript interpretation, and unknown-speaker attribution stay internal. `skills/input-branching/SKILL.md` also keeps routing decisions out of spoken replies. Both Go and HAL filters recognize sentence-initial `They named a/the song:` planning labels and `Speaker [identity] is unknown` bookkeeping, including in English mode. Regression coverage uses the reported “Eternal Flame” output and checks the first-sentence stream as well as the complete reply. These are targeted safeguards; identifying whether a model or proxy put reasoning into assistant text still requires the raw runtime/provider response.

### NO_REPLY suppression

The agent may respond with `NO_REPLY` (or truncated forms `NO`, `NO_RE`, `NO_...`) when it decides not to respond — typically for passive sensing events like sound/motion. These are suppressed by `isAgentNoReply()` in `handler.go`: no TTS playback, no output display. Matches: exact `"NO"`, or any string starting with `"NO_"` or `"NO_RE"` (case-insensitive after trim). Source: `lifecycle_end` payload if available, otherwise fetched from `chat.history` RPC on `lifecycle_end` (async goroutine, best-effort). OpenClaw `lifecycle_end` currently does not include usage data, so `chat.history` is the primary source.

**Silence-narration gate.** Models sometimes describe their decision to stay quiet instead of emitting the sentinel (e.g. `Sound event, no user message. Nothing to say`). That prose passes `isAgentNoReply()`, so `isMetaNonReply()` in `handler_text.go` suppresses it too: text ≤ 100 bytes, no `?`, matching one of the meta phrases (`nothing to say/add/report`, `no (user) message/reply/response/comment (needed)`, `no need to reply/respond/speak/say`, `staying|remaining silent`, `no action needed`). Applied at end-of-turn (`handler_event_agent.go`) and in `tryFirstSentenceFlush()` (`handler_state.go`), where it defers WITHOUT marking the run streamed so a later real sentence still gets the first-audio latency win. Both hits log a `WARN` and the turn is reported as `no_reply`.

## Stream summary events (`agent_*_token` / `thinking_*_token`)

Raw `assistant_delta` and `thinking` deltas are pushed to monitorBus (RAM) but **never written to JSONL** — the persist layer would otherwise grow by ~50–500 lines per turn. Since the Flow Monitor reads JSONL on reload, the pipeline rect for past turns would show no streaming rows.

To bridge that gap, the OpenClaw stream handler emits four lightweight summary flow events per run:

| Node | When | Payload (`data.*`) | Purpose |
|---|---|---|---|
| `agent_first_token` | First non-empty `assistant` delta in the run | `{run_id}` | TTFT marker — `ts` field = perceived reply latency moment |
| `agent_last_token` | `lifecycle.end` (drain accumulator) | `{run_id, text, chunks, chars}` | Closes the assistant streaming row in the pipeline rect |
| `thinking_first_token` | First non-empty `thinking` delta (extended-thinking only) | `{run_id}` | Same as above, for the thinking stream |
| `thinking_last_token` | `lifecycle.end` | `{run_id, text, chunks, chars}` | Same as above, for the thinking stream |
| `narration_demoted` | A `tool` start arrives while `assistant` text is buffered | `{run_id, tool, text}` | Text streamed BEFORE a tool call is the model narrating its plan ("Let me take a look."), not the reply. The handler drops it from the reply buffer (so it never reaches web chat / TTS at `lifecycle.end`) and shows it in the thinking row instead. Runtime-agnostic; skipped when the first sentence already streamed to TTS or the text carries an `[HW:...]` marker |

Maximum 4 extra JSONL lines per turn (often 0–2). Stream from OpenClaw is still called `"assistant"` in code (`handler_events.go: case "assistant"`); only the JSONL node names use the `agent_` prefix for consistency with existing `agent_thinking` / `agent_call` / `agent_response` nodes.

State lives in `OpenClawHandler.streamStats` (per-run counters + accumulated text), independent of `assistantBuf` (which serves TTS flush). Drained on `lifecycle.end`. See `recordAssistantDelta` / `recordThinkingDelta` / `drainStreamStats` in `handler_state.go`.

Frontend (`aggregateEvents` in `helpers.ts`) builds pipeline rows from `*_first_token` (opens a row) + `*_last_token` (closes it with `chunks`/`chars`). `extractTurnTiming` and `turnFirstTokenMs` both fall back to these markers when live deltas aren't in `turn.events`.

The legacy `llm_first_token` flow event that was previously removed for being "redundant with the pipeline aggregator" is effectively re-introduced here — split into the two streams (`agent_*` and `thinking_*`), because the aggregator can't observe streaming moments when raw deltas never reach JSONL.

## Turn Item Display

```
[icon] TYPE  PATH  STATUS  👤 user  ⏱ total  ⚡ ttft
id: run-id
IN   <input text>
[snapshot strip — up to 3 thumbnails]
[🪑 LOAD MORE · pose bucket <id>]   ← only when motion.activity carried a posture nudge
OUT  🔊 <output text>
N events
```

- **IN**: extracted from `sensing_input` summary or `chat_input` detail.message
- **OUT**: from `intent_match` (local) or `tts_send` (agent). Intent match is authoritative and won't be overwritten by stale tts_send from different runs.
- **Path badge**: LOCAL (green) / AGENT (blue) — only set from events belonging to the same run
- **⏱ total**: `turn.startTime → turn.endTime` (full server-observed window: input event → lifecycle_end / tts_send / chat_final). Green ≤5s, amber ≤15s, red >15s.
- **⚡ TTFT** (time-to-first-token): `turn.startTime → first thinking/assistant_delta`. Matches the chat page agent-bubble stamp — the moment the user *sees* a reply begin (first delta for streaming runtimes; the final `chat_response`/`tts_send` for non-streaming ones such as codex). Gap between ⚡ and ⏱ = tail streaming + lifecycle close. Green ≤3s, amber ≤8s, red >8s. Hidden when no LLM stream (e.g., local intent match).
- **Snapshot strip**: extracted from `[snapshot:]` markers in `sensing_input`. For `motion.activity` with a pose bucket, the strip is capped at 3 tiles (the activity snapshot + two worst pose snapshots). Clicking a tile opens the inline lightbox.
- **Pose bucket popup**: when `[pose_bucket:]` is present, a `LOAD MORE` button surfaces `PoseBucketModal`, which fetches `/api/hardware/sensing/pose-bucket/<id>` (proxied to lelamp) and renders the full per-sample table — same monospace grid + click-thumbnail-to-lightbox as the live Sensing tab. Rows whose filename is in `worst_snapshots` are highlighted (red border, ⭐).
- **Debug audio clip** (`speech_emotion.detected`): a click-to-play `<audio controls>` player labeled `🎙 debug` is rendered for each audio URL, so you can listen to the exact clip that produced the detected emotion. The clip's on-Pi path arrives as the optional `audio` field in the `POST /api/sensing/event` body. `system/server/sensing/delivery/http/handler.go` converts the path's basename into a servable URL (`audioURLForPath` → `/api/sensing/audio/<file>.wav`) and stores it in the monitor event `Detail` under key `audio` — **only the basename URL, never the raw path**. The frontend `turnIO()` (`helpers.ts`) pulls these into `audioUrls` from the `sensing_input` event's `detail.audio`; `TurnBadge.tsx` renders the players. **This is a DEBUG-ONLY affordance — the audio is NEVER sent to the LLM.** The path lives in a separate JSON field, never in the chat message text, so it is naturally excluded from what the agent sees — mirroring how `motion.activity` snapshots are surfaced in the UI but stripped before the LLM.
  - **Route**: `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`) serves the `.wav` by basename from `/var/lib/hal/speech-emotion` or `/tmp/hal-speech-emotion`, with strict basename validation — the name must end in `.wav` and contain no `/`, `\`, or `..` (otherwise `404`).

The two badges are meant to be read together: ⚡ is *perceived* latency (what the user feels), ⏱ is *server* latency (what ops sees). Big gap = lots of tail streaming; small gap = short reply or fast lifecycle close.

## Memory state per turn (`lifecycle_start.memory`, `memory_changed`)

Issue #421: one self-written `USER.md` line broke skill routing for most of a
day because nothing showed which memory a turn ran with. Two additions:

- `lifecycle_start` flow data carries `memory`: `{"USER.md": {"size", "sha8"},
  "MEMORY.md": …, "KNOWLEDGE.md": …}` for the **active** runtime — the
  fingerprint published by the OS memory guard (`docs/os-server.md`, "Memory
  guard"). Sizes and 8-hex sha256 prefixes only; never content.
- `memory_changed` (kind `event`) is emitted by the guard's fsnotify watch on
  every write to any runtime's `USER.md` / `MEMORY.md`. It fires ~2 s after
  the write (debounce) and is tagged with whatever trace is active at that
  moment — usually the turn that wrote, but a later turn if one has started,
  or trace-less if the turn has already ended. Data: `file`, `runtime`,
  `path`, `size`, `sha8`, `quarantined` (blocks removed — or, when `execute`
  is false, blocks the guard would have removed), `reasons`
  (`free-prose` | `unknown-label` | `prescriptive`), `execute` (false in
  observe mode, `agent.memory_guard=false`), `trigger`
  (`startup` | `watch` | `rescan`).

The turn card footer gates the badge by state (#463) — `flow` is a public
section and debug is a one-click header toggle, not a hidden URL param:

| State | Meaning | Normal mode | Debug mode |
|---|---|---|---|
| gray | the turn only read memory | hidden | `USER.md 251B · MEMORY.md 1.2kB` |
| amber | the agent wrote, the guard accepted | hidden | `… ✎ memory changed` |
| red | the guard removed blocks | `✎ memory updated · 1 entry removed in hermes` | same, prefixed with the sizes |

Gray reports the size of a file the owner cannot open, on every turn; amber on
every successful write trains people to ignore the row, and then red does not
land either — so both are debugging aids. Red is permanent: it is the only
place in the product where the owner can see that the OS deleted something the
agent wrote (the rest of the removal is an atomic rename, a `.quarantine.txt`
sidecar and a `.bak-<nano>`, all SSH-only, and the #421 reset endpoint ships
without UI). It reads "entry removed", not "quarantined", which sounds like a
security incident, and it names the `runtime` from `memory_changed`: the guard
sweeps all six runtime trees while the sizes beside it are the **active**
runtime's, so without the name a removal in an inactive runtime would turn the
card red next to unrelated file sizes.

Sizes are bytes with the unit glued to the number (`251B`, `1.2kB`) — never the
1000-based `k` the LLM token counts on the same row use. Filenames keep their
`.md` and are separated by a middot, so the row reads as two files with two
sizes rather than one run-on label. In observe mode
(`agent.memory_guard=false`) the badge stays amber and reads
`· would remove 1 entry`; nothing was removed and the file still carries the
block. Hover for exact bytes, `sha8`, the runtime and the reasons. Comparing
`sha8` across two turns tells you whether the memory changed between them.

Badge logic lives in `system/web/src/pages/monitor/FlowSection/memory.ts` and is
unit-tested: `cd system/web && node --test tests/memory.test.mjs`.

## Known Edge Cases

### 1. OpenClaw assigns different run_id
OpenClaw 5.2 (and rare 5.4 paths) generate a UUID for the embedded run; 5.4 mostly echoes the `idempotencyKey`. Mapping logic must handle both.
- **Fix**: SSE handler picks path by `payload.RunID` format — device-format → search-and-remove queue entry; UUID → existing message matcher + map. Telemetry uses a separate unique exact match. See section above.
- **Edge case**: If server restarts between `sendChat` and `lifecycle_start`, the global trace is lost and no mapping is created. Frontend stitching handles this as a fallback.
- **Status**: Fast replies register their trace before the write. Long queued turns retain telemetry evidence within the separate 24-hour/1024-entry bound without extending the routing TTL. Restart, history fetch failure, and missing or ambiguous matches can still leave KPI execution unmatched; UI stitching is not execution evidence.

### 1a. Pending-trace orphan misattribution (regression in 0.0.465, fixed in 0.0.468)
A previous attempt (commit `1897dfee`) skipped the FIFO pop entirely when the runId was device-format, but left the entry in the queue. The next UUID lifecycle then popped that orphan and was misattributed to it — observed pattern: Phase 1 reply + Phase 2 reply both rendered under the Phase 1 chat-N, with Phase 2's content actually belonging to the next chat in the drain.
- **Trigger**: drain/burst flushes multiple chat.sends; first one returns Phase 1 with device-format runId; the next chat's UUID lifecycle (Phase 2) then pulls the orphan.
- **Symptom**: assistant turn shows two unrelated replies under one runId; off-by-one cascade for ~2 min until orphan TTL expires.
- **Fix**: replace skip with `RemovePendingChatTraceByRunID(payload.RunID)` so the matching entry is cleared instead of left as orphan.

### 2. sensing_input enter has no run_id
`flow.Start("sensing_input")` fires before `sendChat()` returns the run ID. The first event of a turn has no trace_id.
- **Mitigation**: Frontend assigns turn's runId from subsequent events (`sensing_input` exit has the ID).
- **Status**: Working. `isTurnStart` detects the event, `extractEventRunId` from later events fills in the ID.

### 3. Concurrent sensing events
Two sensing events arriving close together: turn B's `SetTrace` overwrites turn A's global trace. Turn A's lifecycle events may land with turn B's trace.
- **Mitigation**: Per-event runID means each `flow.Log` carries its own ID regardless of global state. The global trace is only used for the Telegram heuristic.
- **Status**: Mostly fixed. The `sensing_input enter` still has no per-event ID (pre-sendChat).

### 4. Double TTS
Both agent stream (`lifecycle_end` flush) and chat stream (`chat final assistant`) can send TTS for the same response.
- **Status**: Known bug, documented as TODO in handler.go. Fix: deduplicate with per-runID guard.

### 5. Server restarts every ~20s
WebSocket reconnects cause process-level restarts (seq counter resets). This is likely a separate stability issue, not a monitor bug.
- **Impact**: Trace lost mid-turn, events split across restarts.
- **Mitigation**: Per-event runID + frontend stitching handles most cases.

### 6. OpenClaw built-in `tts` tool bypasses HAL speaker (FIXED)
Agent called OpenClaw's built-in `tts` tool instead of responding with assistant text. OpenClaw generated audio server-side (`"Generated audio reply."`) but never routed it to the physical speaker (`/voice/speak` on HAL). Agent then returned `NO_REPLY`, so the OS server had no assistant text to flush → silent.
- **Root cause**: OpenClaw provides a built-in `tts` tool when `tools.profile = "full"`. The sensing SKILL.md instructed the agent to call `/voice/speak`, which the agent mapped to the built-in `tts` tool instead of using `curl` to HAL.
- **Fix**: (1) Deny OpenClaw built-in `tts` tool via `tools.deny: ["tts"]` in config (`service.go`). `tools.disabled` is NOT a valid OpenClaw key — use `tools.deny` (deny wins over `tools.profile`). (2) Intercept fallback in handler.go: if agent still calls `tts` tool, extract text and route to `SendToHalTTS()`. (3) Updated sensing SKILL.md and SOUL.md to instruct the agent to respond with plain text — the OS server's assistant-delta accumulation pipeline routes it to HAL TTS automatically.
- **Status**: Fixed in v0.0.138.

### 7. OpenClaw tool-call visibility gap (action without `tool_call`)
Observed on multiple Telegram turns: user asks for a device action (e.g. LED color change) and the device state/output confirms the action, but flow/debug logs contain only lifecycle + assistant/tts and no `tool_call` event.

- **Impact**: `TOOL` node can stay off even when an action appears to be executed.
- **Current status**: OpenClaw raw payload logging is enabled (`source: "openclaw_raw"`), but some runs still show no `stream:"tool"` payload.
- **Open question**: OpenClaw may be executing an internal path that does not emit tool stream, or action may be inferred from assistant text without explicit tool invocation.

## Compaction summary inspector

The agent session auto-compacts when context tokens cross ~80k. Every compaction writes a `type:"compaction"` record into `/root/.openclaw/agents/main/sessions/<sessionId>.jsonl` containing a `summary` string that is then prepended to every subsequent turn's prompt until the next compaction. Rules accidentally copied or generalized into that summary can override SKILL.md (the summary sits earlier in the prompt and is framed as "established context").

**UI:** Flow Monitor header exposes a `📋 Summary` button. Click → fetch + render modal showing the latest compaction record: timestamp, `tokensBefore`, `summaryChars`, `compactionCount`, `readFiles` fed into the compaction prompt, and the full `summary` text.

**Endpoint:** `GET /api/agent/compaction-latest?session=<key>` (default session key `agent:main:main`). Returns:

```json
{
  "status": 1,
  "data": {
    "found": true,
    "sessionKey": "agent:main:main",
    "sessionFile": "/root/.openclaw/agents/main/sessions/<id>.jsonl",
    "compactionCount": 18,
    "id": 17170331,
    "timestamp": "2026-04-24T03:21:30.305Z",
    "tokensBefore": 80458,
    "summaryChars": 14263,
    "summary": "...",
    "details": { "readFiles": ["..."], "modifiedFiles": ["..."] },
    "fromHook": true,
    "firstKeptEntryId": 17170331
  }
}
```

Use when the agent cites rules that cannot be found in any `skills/**/SKILL.md` — the source is almost always the compaction summary, not the loaded skill. Handler: `system/server/openclaw/delivery/sse/handler_api_compaction.go`.

## Turns list vs downloaded log

| Source | Scope |
|--------|--------|
| **Turns list** (Monitor) | Built from the **last 10 000** `flow_events_*.jsonl` lines (`GET /openclaw/flow-events?last=10000`), then `groupIntoTurns` returns **all** turns (no cap). |
| **↓ Bundle** button | One click downloads **two**: (1) `GET /openclaw/flow-logs?last=10000` via `fetch` + blob save (`lamp_flow_YYYY-MM-DD_last10000.jsonl`) — **same tail** as the UI feed; (2) client JSON of `events[]` + grouped `turns[]` (`lamp_flow_ui_snapshot_*.json`). |
| **full day** link | `GET /openclaw/flow-logs` — entire day file; can be **longer** than the UI window, so Turns are **not** a reconstruction of the full file. |

Turns now show every turn derivable from the fetched events. Comparing server to UI should use **↓ Bundle** (or the same two artifacts manually: `flow-logs?last=10000` + UI snapshot JSON).

## Files

| File | Role |
|---|---|
| `system/lib/flow/flow.go` | Flow event emission, JSONL persistence, per-event runID API |
| `system/server/sensing/delivery/http/handler.go` | Sensing input → flow.Start/End with runID |
| `system/server/openclaw/delivery/sse/handler.go` | Agent events → flow.Log with payload.RunID, turn detection |
| `runtimes/openclaw/service.go` | sendChat returns idempotencyKey as runID |
| `system/web/src/pages/Monitor.tsx` | `groupIntoTurns`, `turnIO`, `extractNodeInfo`, `FlowDiagram` |

Vietnamese summary: `docs/vi/flow-monitor_vi.md`.

Harness final delivery records `harness_response` in flow JSONL with the original device run ID and complete `text`. Web Chat uses this event to recover pending results after SSE disconnects or page reloads. Live delivery still emits `chat_response` with state `final`.

Realtime handled voice and main-agent history sync use separate IDs: `device-realtime-…` for the original exchange, `device-chat-context-…` for synchronization. The persisted `realtime_response` closes the original card and supplies its question/answer; the History sync card follows its own lifecycle. `history_run_id` links the records without merging them. Previously stored shared-ID events retain their existing combined display.

### Voice command and follow-up labels

`sensing_input` and `realtime_response` can carry `data.voice_turn_type` (`voice`, `voice_command`, or `voice_followup`). Flow Monitor combines this field with the handled event type: realtime wake commands display `VOICE_COMMAND_HANDLED`, follow-ups display `VOICE_FOLLOWUP_HANDLED`, and ordinary or legacy handled turns retain `VOICE_AGENT_HANDLED`. Badges, subtype filters and search share the same display name; search also accepts the original event type. Saved broad exclusions expand to the new subtypes. This is display-only; event type, run grouping, realtime/main path, queueing and cancellation remain unchanged. A wake phrase uses the same HAL classifier in LIVE ON/OFF. An authorized LIVE follow-up keeps its wake-focus classification even if focus expires before the provider replies. Realtime-handled replies retain `voice_agent_handled` for silent history synchronization. Legacy rows without metadata keep their original label; history sync does not inherit a neighboring voice turn's classification.

## Harness Store progress

Observed `agent.prepare` / `operation.get` snapshots record `harness_store_progress` with the local run ID, operation ID, state, phase and bounded display text. While the main-agent run is active, the existing `assistant_delta` stream displays these observations; preparation neither suppresses the main reply nor starts TTS. Once task delivery owns the response, late preparation progress cannot replace it. This is polled evidence, not a new Harness protocol event. `ready` means agent preparation only and does not complete the user's task. Action-needed/error guidance remains available to main. No new frontend card or automatic background polling is introduced; see [Harness Store](harness-store.md).

### Harness preparation wait expiry

`harness_preparation_wait_expired` records a local response deadline, with `task_dispatched:false`; it is not a remote operation failure or task result. Native Hermes ends the scoped owner through the normal `lifecycle_error` path after bounded cancellation. Errors prefixed `OS_RUN_EXPIRED:` bypass partial-answer recovery so a timed-out wait cannot be displayed as a recovered success. Preparation progress suppresses repeated identical snapshots per active run/operation and separates changed messages with paragraph breaks.

### Main-agent TTS timing diagnostics

OS-server service logs include `[tts-timing]` markers for both progressive replies and final TTS delivery. These are diagnostic logs, not new Flow Monitor events or acknowledgement KPI endpoints:

- `sentence_ready`: `first_delta_to_ready_ms` measures the first non-empty assistant delta received by OS-server to a speakable first sentence. This includes sentence buffering and existing safety gates, not model time before the first delta.
- `sentence_dispatch`: `ready_to_dispatch_ms` includes leading hardware calls and filler cancellation before TTS dispatch.
- `delivery_start` / `delivery_complete`: `dispatch_to_send_ms`, `send_ms`, and `dispatch_to_complete_ms` separate background scheduling from the runtime delivery call.
- `hal_post_start` / `hal_post_complete`: `http_ms` measures the actual HAL HTTP request through response parsing. `success` means the call returned without an error (including the existing muted-response check), not that audio was heard.

Markers contain `run_id` and a 12-hex-character SHA-256 `text_key`, without repeating reply text or credentials. The handler hash identifies text before runtime sanitization; the HAL POST hash identifies the final payload and can be correlated with HAL `queue_requested` for unchanged text. If a runtime strips markdown or tags, use the final POST hash for that correlation. Legacy unowned speech may have an empty run ID. First-sentence timing is absent when streaming is ineligible or no complete safe sentence appears before the final flush. Runtime ownership/silence gates and TTS buffering are unchanged.

`assistant_end` and the existing `agent_last_token` event also carry `first_delta_to_end_ms` when a first-delta timestamp is known. This covers final-only replies and measures the entire assistant output interval even when sentence 1 streamed early. `final_dispatch` records `end_to_buffer_ready_ms` and `buffer_ready_to_dispatch_ms`, with `streamed_len` indicating whether this is the remainder. “Buffer ready” means the raw final assistant buffer was extracted; the following interval includes sanitization, hardware calls and routing checks. The dispatch marker is emitted only on the actual TTS delivery path, never for suppressed or empty replies. Unknown first-delta timing is omitted, not replaced with zero.


### Harness shared-result labels

Flow cards label Harness result output as `Harness` and sibling references as
`Shared result`, with the owning run ID available in the reference tooltip.
A `harness_response` is result delivery, not evidence of TTS playback. Reference
text still completes its exact pending turn and survives JSONL/SSE recovery;
only the newest grouped input displays the full answer. The shared result is
submitted for speech once under that newest input, subject to voice eligibility
and cancellation. These display labels do not claim that audio was heard.
