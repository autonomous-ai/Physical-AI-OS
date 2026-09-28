# Hermes agent backend

The Jev preload carries the categorized `lookup_name` and instructs reference reads to reuse it exactly (for example `openclaw-imports/computer-use`). A bundled skill with the same bare name can coexist; preload does not delete it or resolve ambiguous bare-name calls on the model’s behalf.

Hermes is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server (native Runs when supported; OpenAI *Responses API* fallback). This doc. Code: `runtimes/hermes/`.

> Source of truth is the code. This documents `runtimes/hermes/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/agentic/hermes_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · this file (Hermes) ·
> [`picoclaw.md`](picoclaw.md) (PicoClaw). Generic switch/install/migration
> mechanics live in the first; per-backend protocol lives in the others.

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`system/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| unset | falls back to `gateway.default` in `robots/<type>/ROBOT.md`, then OpenClaw if that is empty too |
| `"openclaw"` | OpenClaw (default) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) — persistent WebSocket client; assumes the PicoClaw service is already running. See `docs/agentic/picoclaw.md` + `runtimes/picoclaw`. |
| anything else | OpenClaw (logged as `FALLBACK — unknown runtime=…`) |

When `agent_runtime` is unset in `config.json`, the backend is taken from the
device's declared `gateway.default` (`robots/<type>/ROBOT.md`); OpenClaw is used
only if that is also empty. The banner logs `source` so you can tell which won.

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → HERMES` banner with
`base_url`, `conversation`, `model`, and `api_key_set`. There is **no per-unit
config** for these yet — they are compile-time constants in
`runtimes/hermes/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `BaseURL` | `http://127.0.0.1:8642` | Local Hermes API server |
| `APIKey` | `hermes-api-key` | Bearer for Hermes |
| `Conversation` | `device-main` | Named channel all turns flow into |
| `Model` | `hermes-agent` | Model id sent to Hermes |

Hermes itself is assumed to be already running on the device at `BaseURL` with
all skills provisioned; os-server is only a per-request client.

## 2. What changes vs OpenClaw — and what does not

| | OpenClaw | Hermes |
|---|---|---|
| Transport | one persistent WebSocket | stateless HTTP POST + SSE per turn |
| Connection state | socket up/down | `/health` poller goroutine (`health.go`) drives `ready`/`connectedAt` |
| Session | the socket | server-side UUID via `X-Hermes-Session-Id` header (§3) |
| Downstream pipeline | — | **identical** — Hermes translates SSE → the same `domain.WSEvent` frames |

Because Hermes emits the same `domain.WSEvent` shape that the OpenClaw handler
(`server/agent/delivery/http/handler_events.go`) already consumes, HAL TTS,
`[HW:/…]` marker routing, monitor SSE, the sensing drain, and Telegram fan-out
all stay untouched. `*hermes.Service` satisfies `domain.AgentGateway` in full
(`Name()`="Hermes", `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`/`SetBusy`,
`QueuePendingEvent`, `SendChat*`, `StartWS`, …).

### Cache usage in Flow Monitor

Hermes Responses `input_tokens` includes uncached input, cache reads and cache
writes. `translator.go` subtracts `input_tokens_details.cached_tokens` and
`input_tokens_details.cache_write_tokens` into separate domain buckets, so the
monitor shows `R`/`W` without counting cached input twice. Missing details retain
the legacy input total; impossible cache totals are ignored. For example, 14,097
input with 13,824 cache reads and 66 output becomes 273 uncached input, `R13.8k`,
and 66 output (14,163 total).

Some Hermes versions accumulate cache internally but drop it in
`_finish_turn_result` and `_responses_usage_payload` in
`gateway/platforms/api_server.py`. Local onboarding runs the embedded
`cache_usage_patch.py` with Python 3 to preserve `session_cache_read_tokens` and
`session_cache_write_tokens` in Responses usage. It checks known AST shapes,
compiles before an atomic write, and leaves already-patched files unchanged.
A changed patch joins the existing gateway restart decision. Unsupported source
produces a warning without a write; missing installations and remote endpoints
are skipped. Remote operators must apply the equivalent server fix themselves.
Existing recorded turns are not rewritten; inspect a new turn after restart.
No frontend change or cache-provider setting is required.

## 3. Session & conversation model

Hermes has no socket, so the "session" is server-side:

- Every response carries the `X-Hermes-Session-Id` response header — one UUID per
  conversation, stable across reconnects. `Service.sessionUUID` shadows it.
- `Conversation` (base name `device-main`) is the named channel every turn flows
  into; all chat/sensing/Telegram turns share it so the agent keeps one context.
- `Service.lastResponseID` caches the latest `response.id`, used to chain turns
  (Responses-API style continuation).

State is in-memory only (`sessionUUID`, `lastResponseID`, `reqCounter` + the
guard / broadcast / web_chat / pose-bucket run trackers); nothing persists across
an os-server restart.

### Conversation rotation (`rotation.go`)

The gateway chains the **entire** conversation history into one response blob per
turn, keyed on conversation name. A permanent name therefore accumulates an
unbounded chain (measured: a `device-main` blob of **28 MB / ~6M tokens**,
`response_store.db` at **1.9 GB**), and the gateway must reconstruct + recompress
it on every turn — turning a turn that should take ~7 s into **~50 s**. To bound
this, os-server rotates the conversation name:

- `conversationName()` is **boot-fresh**: seeded once per process as
  `device-main-<bootUnix>`, so an os-server restart never re-attaches to a
  previously bloated chain.
- `rotateConversation()` switches to `device-main-<bootUnix>-<seq>`, clears
  `lastResponseID`, and lets the gateway start a new (small) chain. Old chains are
  abandoned under their old name (a separate prune reclaims the disk).
- **Trigger:** the generic lifecycle handler calls `ShouldRotateSession(totalTokens,
  turnsSinceRotation)` once per turn (a `domain.AgentGateway` method). OpenClaw /
  PicoClaw rotate on a real-token threshold (150k); **Hermes rotates on turn count**
  (`rotateMaxTurns = 40`, or a `rotateTokenThreshold = 250_000` spike) because its
  reported tokens are post-compression (~20–60 k) and never reflect the real chain
  size — the token threshold is a safety net, not the primary gate. `NewSession()`
  performs the rotation.
- **The token net was 50_000 until 2026-09-09.** It sat inside the normal
  operating range: on lamp-a0ae a fresh conversation already reports ~12.3 k, and
  an ordinary turn that reads a `SKILL.md` and runs a tool adds ~25 k
  (12.3 k → 41.3 k → 64.5 k → 73.5 k), so the net fired every 2–3 turns. Because
  the wired path is `maybeAutoNewSession` (compact is disabled), each firing
  dropped the history with no summary and the device lost what it had just said.
  250 k holds ~10 turns at that rate — a net has to sit **above** where the
  gateway's own compression settles, not inside it (same value and reasoning as
  [`codex`](codex.md)).

## 4. Request protocol — native Runs and Responses fallback

On a server advertising `run_submission`, `run_events_sse`, `run_status`,
`run_stop`, and `run_steer` through `GET /v1/capabilities`, plus
`features.runs_idempotency.autonomous_run_events_v1=true`, eligible plain-text
turns use `POST /v1/runs`. The body carries `input`, `model`, optional
`instructions`, and the current `session_id`. Admission returns a server
`run_id`; events arrive through `GET /v1/runs/{run_id}/events` and status through
`GET /v1/runs/{run_id}`. The device run ID remains the trace/metric identifier.
Unlike Responses conversation chaining, Runs reloads persisted session history
by `session_id`; the server resolves compression continuations when opening the
next run. A native run ID is not a Responses `previous_response_id`.

Servers without these capabilities retain the Responses transport below.
Once native support is verified, the service keeps that transport for its
lifetime; a later capability probe failure must not resume stale Responses history.
On capable servers, images wait for their own native run on the same session;
the adapter converts Responses `input_text` / `input_image` parts to canonical
`text` / `image_url` content without dropping images. Image requests are never
injected through the text-only steer endpoint. Network failure after admission or steering is not permission to
resubmit the same request: the server may already be executing it.

### Responses fallback — `POST /v1/responses`

`client.go` POSTs a `streamRequest` with `stream: true` and reads an SSE stream:

```jsonc
{
  "model": "hermes-agent",
  "conversation": "device-main",
  "stream": true,
  "instructions": "…",        // optional system/role text
  "input": "<text>",           // plain turn …
  "title": "…"                 // optional
}
```

For **vision** turns `input` is a multi-part array instead of a string — Hermes
accepts both shapes:

```jsonc
"input": [{ "role": "user", "content": [
  { "type": "input_text",  "text": "…" },
  { "type": "input_image", "image_url": "data:…" }
]}]
```

## 5. SSE → `domain.WSEvent` translation

The SSE consumer (`client.go`) streams `response.*` events; `translator.go` maps
them into `domain.WSEvent` frames and dispatches them through the handler
registered by `StartWS` — the same path OpenClaw uses. The turn-lifecycle mirror
matches OpenClaw: `activeTurn` flips true on send and false on
`response.completed`; the completed result carries `response.id` (cached as
`lastResponseID`) and the full assistant text for send-and-wait callers.

Native Runs names events inside each JSON `data` envelope (`event`), rather
than an SSE `event:` line. The adapter maps `message.delta` and actual terminal
`run.completed` / `run.failed` / `run.cancelled` / `run.interrupted` to the
existing event pipeline. A `run.steered` acknowledgment is not task completion.
If the stream is lost, a confirmed terminal status can recover the outcome.
Otherwise the adapter requests stop and polls until execution settles, keeping
remote ownership while status is unresolved. EOF or a still-running status
cannot be reported as success.

An unresolved run marks its conversation *uncertain*: any request already queued
on it is refused ("unknown acceptance ... start a new session") because the lost
run may still be acting on that prompt. The adapter then **rotates the
conversation itself** so the next request goes out on a fresh one — nothing else
ever rotated it, and lamp-0c4e (2026-09-16) failed every turn for 6+ minutes
after a presync-triggered gateway restart until os-server was restarted. A
**dial failure** on `POST /v1/runs` (connection refused while the gateway
restarts) is *not* uncertain: the prompt never left the device, so the request
fails but the conversation stays usable.

Native mode also requires the OS compatibility marker above: the unpatched
Runs API omits tool-call IDs/results and cache details needed by the existing
handler. The compatibility patch adds `tool.call.started` / `tool.call.completed`
with real call IDs, arguments and results, plus `cache_read_tokens` and
`cache_write_tokens` in usage. The adapter reuses the Responses translator and
maps cache counters into `input_tokens_details`; legacy `tool.started` /
`tool.completed` progress is ignored to avoid duplicate callbacks. If the patch
is absent or refuses an unknown source shape, keep Responses until a verified
patch is installed and the gateway restarts.

Sensing/pose markers are stripped before send using the same regexes as OpenClaw
(`[snapshot: …]`, `[pose_bucket: …]`, `[pose_worst: …]`) so the agent never sees
internal hardware markers.

## 6. Connection state & health

No socket means liveness is polled. `health.go` runs a `/health` poller that
flips `ready` / `connectedAt`, derives `agentStartedAt` from
`/health/detailed.uptime_s` when available, and uses `hasConnected` to skip the
"reconnected" TTS chime on the first successful poll. `AgentUptime()` reports the
Hermes process uptime, independent of os-server.

## 7. Busy state & pending sensing events

Identical contract to OpenClaw: while a turn is active (`IsBusy`), passive sensing
events are dropped or buffered (`QueuePendingEvent`, last-write-wins per type) and
replayed when idle, so ambient signals never interrupt an in-flight command.

For a managed native run, new eligible user text can be submitted to
`POST /v1/runs/{run_id}/steer` instead of starting another parallel agent.
Hermes appends this guidance at an iteration/tool boundary; an accepted request
does not imply immediate interruption of the current model call or tool.
`POST /v1/runs/{run_id}/stop` is a distinct cancellation operation, and its
`stopping` acknowledgment is not a terminal result. Requests that cannot steer, including images, wait for their own native turn
rather than being dropped or moved to a disconnected Responses conversation.

An accepted steer that arrives too late may return in terminal `pending_steer`.
It must be retained for a subsequent turn, not counted as work completed by the
finished run. Multiple pending steer texts are concatenated with newlines by
Hermes. Device request ownership and execution outcomes remain distinct from
these transport acknowledgments.

Ordinary runs keep progressive speech. After accepting a steer, the adapter
holds subsequent text until the terminal result identifies the consumed inputs;
the newest audible request receives that text through the existing assistant
buffer before lifecycle end flushes TTS. Earlier voice requests stay silent.
The shared execution's hardware markers and token usage are emitted once.

After a confirmed explicit stop, the next user request in that conversation
carries a factual cancellation notice in its input. Hermes can merge consecutive
user history rows after an interrupted model call; the notice records that the
unfinished request was cancelled and should not be resumed. Passive requests do not
consume this notice, and a new conversation does not inherit it.

**Merge-drain (Hermes-only).** OpenClaw's backend has a steer mode
(`messages.queue.mode=steer`) that merges concurrent messages into the in-flight
turn at the next model boundary. Hermes now uses native steering for eligible
user text when the installed server supports it; passive sensing still uses
client-side batching: when `drainPendingEvents` runs, surviving events are
partitioned by `standaloneDrain`. Standalone events keep their own turn — real
voice commands (`voice` / `voice_command`, answered directly), web chat (`web_chat`), `voice_agent_handled`
(silent reply), and image-bearing events. The remaining pure-ambient sensing
(presence / motion / emotion / speech_emotion) is collapsed into a **single turn**
via `sendMergedPending` (one `runID`, lines joined under `mergedSensingHeader`),
so the per-turn prompt floor is paid once instead of once per event. This recovers
most of steer's cost saving but not its immediacy: the batch fires only after the
current turn ends, never mid-turn. Gated by the `mergeDrainEnabled` const
(set `false` to fall back to one-turn-per-event replay). See
`runtimes/hermes/events.go`.

Unsent events stay queued while Hermes is unreachable. A successful health
transition resumes draining through the same speaker gate; each standalone
request finishes before the next queued request starts. Only a synchronous
not-ready rejection is retained. An attempted HTTP POST is never replayed,
because its desktop actions may already have executed.

Each live HTTP/SSE stream keeps the adapter busy independently. One stream ending
or failing cannot clear another stream's busy state, including after the legacy
busy TTL. SSE consumption stops at `response.completed` or `response.failed`;
EOF before either terminal event emits a lifecycle error instead of success.
Per-request device run IDs already isolate SSE replies, so no Codex WebSocket
request-ID protocol or CLI-specific repetitive-output heuristic is copied here.
These guarantees are covered by local transport tests; they do not establish
live Hermes desktop or voice success.

## 8. Channels (Telegram/Slack/Discord) — inbound visibility + fan-out

The Hermes gateway **owns Telegram/Discord/WhatsApp I/O**: it polls those platforms
itself with the tokens `presync` syncs into `~/.hermes/.env`, runs the turn, and
replies to the chat directly. (**Slack is the exception** on this fleet — the app
runs in HTTP/Events mode, not Socket Mode, so os-server bridges it; see the Slack
subsection below.) os-server is not on the gateway's channel path, so — unlike
OpenClaw, which pushes `session.message` WS events — a gateway-handled channel turn
would never show up in Flow Monitor. The gateway has no cross-platform turn
broadcast to subscribe to either; the only seam is its **hook** system.

So os-server installs a gateway hook, `os-server-observer`
(`runtimes/hermes/hooks/os-server-observer/{HOOK.yaml,handler.py}`, materialized
to `~/.hermes/hooks/` by `ensureObserverHook` on every boot — see §10). It fires
on `agent:start` / `agent:end` for **every** platform and POSTs the turn to the
loopback endpoint `POST /api/agent/channel-turn` (`handler_channel_turn.go`),
which emits the same flow events a normal turn does:

- `agent:start` → `chat_input` (source `channel`, with `sender` + `channel`) plus
  `lifecycle_start`. It also fires the **emotion-acknowledge** "thinking" face
  (`FireChannelStartEmotion` → `fireAckEmotion`) for these gateway-owned
  Telegram/Discord turns — the same ack `sendChat` gives os-server-mediated turns,
  which native channel turns never reach. Slack/web (`api_server`) are skipped here
  (see below), so they get the ack once, from `sendChat`, with no double-fire.
- `agent:end` → `lifecycle_end` plus `tts_suppressed` carrying the **marker-stripped**
  reply text (the reply went to the channel, not the device speaker — the same node
  the OpenClaw channel path uses, so the web turn renders it), or `no_reply` for an
  empty / `NO_REPLY` turn, or `hw_only_reply` when the turn was markers with no
  spoken text. On this event the handler also runs `extractHWCalls` over the reply
  and fires any `[HW:/…]` markers on the **local device** (LED/emotion/servo/audio)
  via `fireHWCalls`, so a gateway-owned channel turn (Telegram/Discord) can drive the
  hardware — matching the OpenClaw `session.message` path and the device
  `/v1/responses` path. (Slack is unaffected: its turns run through `/v1/responses`
  and are skipped here as `api_server`, so `handler_event_agent` already fired their
  markers.) **Caveat:** the gateway may truncate `response` (~500 chars), so a marker
  near the end of a long reply can be clipped and dropped — raise the gateway-side
  truncation if end-of-reply markers go missing in practice.

Both events share one `run_id`, correlated by `session_id`. The handler is
channel-agnostic (keyed on the `platform` field) and **skips** `api_server` / `cli`
turns — those are os-server's own `/v1/responses` calls, already logged by
`sendChat`; emitting them again would double the device-originated turns. (Slack
bridge turns below are driven through `/v1/responses`, so they are logged by
`sendChat` and likewise skipped by the hook — no double-counting.)

Outbound (proactive) sends — `Broadcast` / `SendToUser` in `telegram.go` /
`telegram_sender.go` — go straight to the Telegram Bot API for device-initiated
alerts, using the bot token and the `telegramTargetsFile` chat list.

### Slack — HTTP-mode bridge (for Socket-Mode-only runtimes)

`domain.SlackBridge` (`system/domain/slack_bridge.go`) is a **generic
mechanism**, not hermes-specific: it is the interface for **any** runtime whose
native Slack support is **Socket Mode only** (today: hermes is the one example)
and which therefore has **no local HTTP Slack webhook** to receive events. For
such a runtime, os-server itself becomes the **HTTP-mode Slack frontend** — it
parses the event, drives a turn, and posts the reply via the Bot API. OpenClaw and
picoclaw serve the Slack HTTP webhook themselves (OpenClaw's local
`127.0.0.1:18789/slack/events`), so they do **not** implement `SlackBridge` and
keep the existing local-webhook POST path untouched.

**Slack app requirements.** The Slack app must have **"Agents & AI Apps"
enabled** plus the scopes **`assistant:write`** (assistant typing status),
**`chat:write`** (streaming + posting), and **`im:history`** (read DMs). Without
`assistant:write` the typing status is silently skipped (best-effort) but text
still streams via `chat:write`.

**Inbound** — Slack → bff-campaign-service proxy → MQTT `slack_event` → device.
`server/device/delivery/mqtt/slack_event_handler.go` (`forwardSlackHTTP`)
type-asserts the active gateway to `domain.SlackBridge`. When it matches (hermes)
it calls `HandleInboundSlack`; when it does not (openclaw, picoclaw) it keeps the
existing local-webhook POST path.

`runtimes/hermes/slack.go` `HandleInboundSlack` / `parseSlackInbound` decode the
Slack Events JSON (`url_verification` challenge — defensive, the public proxy
normally owns Slack's Request URL check; `event_callback` with `event.type`
`message`/`app_mention`). It skips bot messages (`bot_id`), `subtype` events
(edits/joins), and empty-`user` events (loop guard); enforces an allowed-user gate
via `config.SlackUserID` (empty = open); strips a leading `<@Uxxx>` mention; and
captures the `channel`, `thread_ts`, the user message `ts`, and `team_id`. For a
real user message it then:

1. records the Slack origin (channel + `thread_ts` + the user message `ts`) in the
   `slackRunOrigin` map;
2. sets the assistant status to **"...is typing"** via
   **`assistant.threads.setStatus`** (`setSlackAssistantStatus`, best-effort,
   async — needs `assistant:write`);
3. registers a **lazy** stream session (`startSlackStreamSession`) — no Slack call
   yet, just a per-run goroutine ready to stream;
4. sends the turn via `SendChatMessageWithRun`;
5. adds an 👀 (`eyes`) reaction to the user's message (`setSlackReaction`, constant
   `slackAckReaction = "eyes"`, async).

**Streaming.** The reply renders with Slack's native streaming API — the real
"…is typing" indicator plus text that streams in progressively. During the turn,
the agent SSE handler (`server/agent/delivery/http/handler_event_agent.go`) feeds
the **cleaned cumulative** reply text (`cleanedSlackStreamText` in
`handler_state.go`, which strips HW markers and defers on a partial `[HW:` marker /
any `<say>` wrapper / `NO_REPLY` / `HEARTBEAT_OK`) to `StreamSlackDelta` on every
delta. A dedicated per-run goroutine (`slack_stream.go`) opens the stream
**lazily** on the first content via **`chat.startStream`** — seeded with that first
text as a `markdown_text` chunk, so the bubble is **never empty** — then appends
the new tail via **`chat.appendStream`** (`markdown_text` chunks), throttled to
~650 ms (the first flush is immediate via a kick). It appends only the new
(un-appended) tail, in order, so the SSE delta loop never blocks on a Slack HTTP
call. `chat.startStream` takes `channel`, `thread_ts` (required — reply in the
existing thread, else thread under the user's message), and `recipient_team_id`
(required for channels, taken from the event's `team_id`).

**Reply finalize** — `handler_event_agent.go` calls `DeliverSlackReply(runID,
text)` (`runtimes/hermes/slack.go`) for the completed runID. It consumes the
origin, **clears the assistant status** (`setSlackAssistantStatus` with `""`),
removes the 👀 reaction, then `finishSlackStream` does a **final flush +
`chat.stopStream`** (which also clears the typing indicator and marks the message
complete). When the stream never opened (no content reached it, or `startStream`
kept failing), it falls back to a single `chat.postMessage` (`PostSlackReply`). The
Web API calls all go through the generic `slackAPI` helper in
`runtimes/hermes/slack_sender.go`.

**TTS suppression (both halves of the turn).** A Slack-origin run never reaches the
device speaker; suppression is enforced at **two** points via the **non-consuming**
`IsSlackOriginRun(runID)` peek (so both fire before `DeliverSlackReply` consumes
the origin at reply time): the mid-turn first-sentence stream
(`canStreamSentenceTTS` in `server/agent/delivery/http/handler_text.go`) **and**
the final remainder (`isChannelRun`, set from `isSlackRun`, in
`handler_event_agent.go`). The reply goes to Slack, not the speaker.

**Bot API methods used.** `chat.startStream` / `chat.appendStream` /
`chat.stopStream` (streaming reply), `assistant.threads.setStatus` (typing
status), `reactions.add` / `reactions.remove` (👀 ack), `chat.postMessage`
(fallback + proactive).

**Outbound / proactive** — a `SlackSender` (`domain.ChannelSender`, in
`slack_sender.go`) posts sensing/broadcast messages to `config.SlackUserID` via
`chat.postMessage`; it is wired into the hermes `channels` list in
`runtimes/hermes/service.go` alongside `TelegramSender`.

**`.env`** — `SLACK_BOT_TOKEN` (synced from `config.json` by the presync hook) is
what the bridge uses for every Bot API call. `SLACK_APP_TOKEN` is irrelevant to the
HTTP bridge (it drives native Socket Mode) but harmless.

**v1 scope limits.** The bridge skips Slack request-signature re-verification (the
MQTT broker path is device-authenticated and the proxy already verified the
signature) and defers slash commands (`slack_command`). The reply itself is
text-only and image attachments are dropped on the proactive path.

## 9. Voice

`hal.go` wires Hermes turns into the HAL voice path (TTS on speak-end, the same
`lib/hal` entry points OpenClaw uses), so spoken interaction works the same
regardless of backend.

### Dead-air fillers during tool work

`system/lib/i18n/fillers.go` maps Hermes tool names to short, activity-specific
voice phrases. Coverage follows the [official tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference)
(reviewed 2026-09-11); `system/lib/i18n/fillers_test.go` keeps a registry snapshot
and checks coverage in English, Vietnamese, Simplified Chinese and Traditional
Chinese. The mappings cover:

- Core terminal/process, file/skill reading and editing, web search/extraction,
  memory/session lookup, delegation, planning/cron and media tools.
  `terminal` uses execution phrases, `search_files` uses neutral lookup phrases,
  and `skill_view` uses reading phrases. Memory writes and speech generation
  have separate `memory_store` and `audio_generate` pools.
- Optional browser/CDP, desktop/preview, project/kanban, Home Assistant,
  Feishu, Discord, Spotify and Yuanbao tools, plus Honcho compatibility names.
  Mixed-action tools use neutral checking phrases because the tool name alone
  does not establish the action or its success.

Known names also resolve inside `mcp__<server>__<tool>` wrappers. Unknown tools
retain the generic continuation fallback; this mapping does not install or
enable any optional tool. HAL prewarms the new lookup, memory-write and
speech-generation pools alongside the existing pools.

Only runs marked as voice turns are eligible. The scheduler uses a **1.5-second**
delay and a **2.5-second** cooldown when rearming at tool boundaries, with at most
**6 fillers per turn including the opening acknowledgment** (at most 5 scheduled
fillers after it). Assistant text suspends pending fillers; a subsequent
`tool.start` resumes scheduling without resetting the count or cooldown.
End/error/cancellation or user interruption stops the run's fillers permanently.
Dispatching the first spoken answer sentence or intercepting a TTS tool also
stops them permanently to avoid overlapping the answer audio.
Hardware reactions also suppress nearby fillers. Scheduling follows lifecycle
and tool events; it does not guarantee periodic speech throughout a long wait.
Flow Monitor labels such as `agent:first_token` and “OS Server waiting next
event” are pipeline events/elapsed gaps, not tool names that need their own pool.

## 10. Operating it

Hermes is installed by `runtimes/hermes/install.sh` (co-located with
its implementation). The script is **embedded in os-server** (`go:embed`,
registered via `lib/runtimereg`), so it ships + OTA-updates with the binary;
os-server materializes it to `/usr/local/lib/os-runtimes/hermes/install.sh` and
switch-runtime runs that local copy — fully offline, no CDN round-trip. (The CDN
path `${RUNTIMES_BASE_URL}/hermes/install.sh` remains a fallback for backends not
compiled into the binary.) The installer pulls the Hermes CLI to
`/usr/local/bin/hermes` **stage by stage** (see below), stops `openclaw` (so the
skill import doesn't race its running state), seeds the `API_SERVER_*` keys in
`~/.hermes/.env`, then **delegates all `config.yaml` + skill setup to the presync
hook**, which it invokes inline, and finally installs + starts the gateway as a
**system service** via `hermes gateway install --system --run-as-user root` +
`hermes gateway start --system` (unit: **`hermes-gateway.service`**). Because the
presync hook does the config + skills (see below) and the installer runs it
inline, a direct `bash install.sh` is fully configured and running.

> **Staged CLI install (skips `node-deps`).** Rather than the monolithic
> `curl | bash --skip-setup`, the installer downloads the upstream installer
> (`https://hermes-agent.nousresearch.com/install.sh`) to a temp file and drives
> only these stages, in order:
> `prerequisites repository venv python-deps path config`
> (each via `bash <installer> --stage <name> --non-interactive`). It deliberately
> **skips the `node-deps` stage**: that stage runs an `npm install` of
> browser-tool native modules (node-gyp) that hangs indefinitely on the ARM board,
> and a voice device never uses browser tools — the gateway is Python-only and
> doesn't need them. After the loop it stamps
> `echo git > /usr/local/lib/hermes-agent/.install_method` so a later
> `hermes update` recognizes this as a git install.

> **Hermes is OTA-updated like the other CLIs, pinned by commit.** `hermes update`
> takes no target version (it moves to upstream HEAD), so the metadata entry
> carries the exact upstream commit: `make upload-hermes 0.21.1 v2026.9.7` resolves
> the date tag to its commit, `make promote-hermes` raises the floor, and the
> bootstrap worker runs `software-update hermes`, which checks that commit out
> through the upstream installer (`--commit --force-commit`, as the imager does).
> The web Versions card shows the button once bootstrap reports the entry — i.e.
> the entry has a `commit` and the on-device updater is the pinning one. An
> unpinned entry stays SSH-only (`hermes update` to HEAD, warning on mismatch).
> See `docs/bootstrap-ota.md` §5.

> **Install log lives off zram.** The installer tees all stdout+stderr to
> `$HERMES_LOG`, default **`/root/.hermes/install.log`** (persistent rootfs) —
> **not** under `/var/log`, which on these boards is a volatile zram mount
> (log2ram) wiped on reboot, exactly losing the install log when you need it.
> Follow it live with `tail -f /root/.hermes/install.log`; override the path with
> `HERMES_LOG=…` in the environment before invoking.

> Unit name: the gateway runs as `hermes-gateway.service`. The installer declares
> this in `/usr/local/lib/os-runtimes/hermes/service` so `switch-runtime` enables
> the right unit (§11); `reset_hermes.go` targets the same unit.

### The gateway unit is self-healed (image pre-bake + runtime backstop)

`IsReady()` and the device-setup gate (`WaitForAgentReady`, `system/device/service.go`)
both wait on the gateway's HTTP `/health` (`127.0.0.1:8642`). That requires the
**`hermes-gateway.service` unit to exist** — having the `hermes` binary on `PATH`
(`hermes --version` works) is **not** sufficient. The unit is normally created by
`install.sh` on the first switch to hermes, but a device can reach hermes *without*
that path — e.g. an operator hand-edits `config.json`'s `agent_runtime` to `hermes`
after a factory reset. With no unit, the gateway never starts, `WaitForAgentReady`
times out, `SetUpCompleted` stays `false`, the device falls back to AP mode, and the
symptom reads as "**WiFi won't connect**" even though the WiFi association itself
succeeded. Two layers close this gap:

- **A — image pre-bake** (`scripts/imager/build-orangepi.sh`, `scripts/imager/build.sh`): right after pre-baking the
  Hermes CLI binary, the image runs `hermes gateway install --system` to write the
  unit file, then `systemctl disable hermes-gateway` so it does **not** auto-start at
  boot (OpenClaw is the default active runtime; enabling both would run two agents).
  Best-effort — the build chroot has no running systemd, so if the CLI cannot create
  the unit there, layer B installs it at runtime.
- **B — runtime backstop** (`ensureGatewayUnit`, `runtimes/hermes/gateway.go`, called
  from `EnsureOnboarding`): when the unit is absent (`systemctl cat hermes-gateway`
  fails), it runs `hermes gateway install --system` on demand and re-declares the
  switch-runtime `service`/`verify` files. This is fast — the binary + venv are
  already pre-baked, so it only writes the unit (no git clone / `uv sync`).
  `EnsureOnboarding` then **`systemctl enable`s** the unit (factory reset disables it
  — `reset_hermes.go` step 4, "SetupAgent re-enables" — and a freshly installed one
  is not enabled for boot) and (re)starts it whenever config changed, the unit was
  just installed, **or** the unit exists but is not active (crashed / disabled).

### The presync hook owns `config.yaml` + skills

The Hermes model config in `config.yaml` and the OpenClaw-imported skills are owned
by the **presync hook** (`runtimes/hermes/presync.sh`), **not** by `install.sh`.
**os-server materializes the hook to `/usr/local/bin/runtime-hermes-presync` on
every switch** (`materializePresync`, registered via `runtimereg.RegisterPresync`),
so a plain os-server OTA refreshes it on disk — unlike a copy written once by
`install.sh`, which `switch-runtime` skips on a later switch (the *activation gap*;
see `docs/agentic/adding-agent-runtime.md` §3).

**The hook also runs on every os-server boot AND at initial setup**, not only on a
switch — both via `EnsureOnboarding` (`runtimes/hermes/onboarding.go`), which
executes the embedded `PresyncScript` and restarts `hermes-gateway` only when the
config actually changed (content-hash guarded — no restart loop). The
change-detection hash covers **both** `config.yaml` **and** `.env` (`hermesEnvFile`
= `/root/.hermes/.env`), so a channel-token-only change (which touches only `.env`,
e.g. adding Slack live) also restarts the gateway — letting the Hermes server pick
the new channel up:

- **Boot:** the startup sequence calls `EnsureOnboarding`. Closes the gap where a
  device that **boots straight into Hermes** (`ROBOT.md gateway.default: hermes`,
  or imaged with it) without ever switching from OpenClaw, or whose `llm_*` changed
  while Hermes was already active, would keep a stale `config.yaml` that never
  picked up `config.json`'s real `llm_api_key`/`base_url`.
- **Setup:** `SetupAgent` (also in `onboarding.go`) just calls `EnsureOnboarding`.
  This works because **Hermes provisions from `config.json`, not from the
  `SetupRequest`** (unlike OpenClaw, whose `SetupAgent` writes `openclaw.json`
  straight from the request — hence OpenClaw needs *two* distinct functions, Hermes
  *one*). The device setup flow saves `config.json` **before** calling `SetupAgent`
  (`system/device/setup.go` — the call was deliberately ordered after
  `config.Save()`), so presync materializes `config.yaml`/`.env` from the
  freshly-entered keys immediately instead of waiting for the next boot.

This gives Hermes the same config self-heal OpenClaw has (`ensureAgentDefaults` +
`StartModelSync`), reusing the one presync script instead of duplicating the sync
in Go. (A live `llm_*` rotation via `PUT /api/device/config` without a reboot still
waits for the next boot — a config-change trigger is a possible follow-up.)

The hook runs right before the gateway starts (on switch and boot, and inline
during install) and does three things, in order:

1. **Restores skills** — when `~/.hermes/skills/openclaw-imports` is empty (first
   install OR after a factory reset wiped it), runs `hermes claw migrate` (it
   **copies** OpenClaw skills, no transform). Guarded on the dir being empty so a
   normal switch is a no-op (no re-import churn). `claw migrate` also touches
   SOUL/MEMORY, but harmlessly: the Go persona migration (§12) runs afterwards and
   rewrites those cleanly, so only the skills persist.

   A migrate that runs while canonical copies are already on disk (install-time
   race with the skill watcher, manual runs) uses `--skill-conflict rename` and
   leaves `<name>-imported` **duplicates** — two candidates for one name make
   Hermes' `skill_view` refuse to load the skill at all ("Ambiguous skill name"),
   so the agent improvises without it. `EnsureOnboarding` therefore prunes them
   every boot (`pruneImportedSkillDuplicates`): the `-imported` copy is dropped
   when `<name>` exists (the CDN copy is canonical), or renamed to `<name>` when
   it is the only copy — and the gateway is restarted when anything changed (the
   session skill index is built at gateway start).
2. **Ensures the `config.yaml` model structure** (idempotent — self-heals after a
   factory reset's `hermes setup --reset` blanks it). It coerces a reset-left
   `model: ''` back to a map, then asserts:
   - `.model.provider = custom:autonomous`
   - `.model.default = "Auto-AI"` — the campaign-api model alias, which that proxy
     resolves to whatever model it picks. Left as-is **while the device is on that
     proxy**; `llm_model` may hold an OpenClaw primary model, which means nothing
     here.

     On any other `llm_base_url` the alias is an unknown model id, and DYNAMIC
     below replaces it with the operator's `llm_model`. Measured on
     intern-v2-d16f pointed at openrouter: every turn returned
     `400 Auto-AI is not a valid model ID`, and because presync self-heals each
     boot, editing `config.yaml` by hand did not survive a restart.
   - `.custom_providers[0]` → `name: autonomous`, `key_env: AUTONOMOUS_API_KEY`,
     `api_mode: anthropic_messages`, `base_url` (default campaign-api, overridden below).
   - `.custom_providers[0].models.Auto-AI.prompt_caching = true` — **only while the
     device is on the campaign-api proxy** (same `llm_base_url` check as the alias
     above). Hermes emits Anthropic `cache_control` breakpoints for a custom
     provider only when the model declares this capability; without it the whole
     ~18k-token floor (system prompt + 25 tool schemas) is re-sent uncached every
     turn. Measured on lamp-0c4e (2026-09-16), same "hello" in one session: no
     markers 18.3s → 12.6s with `cache_read` 0; markers 13.8s → 9.2s steady with
     `cache_read` ~85%. A BYO brain keeps Hermes' own per-provider caching policy.
   - `.prompt_caching.cache_ttl = "1h"` — same proxy-only scope. A lamp user
     typically speaks once and goes quiet for 10-20 min, which outlives the `5m`
     default and re-bills the full prefill on the next turn (measured 2026-09-16:
     3.6s with a warm cache vs 11.8s after an 8 min gap). Hermes accepts only
     `5m` | `1h` and sends `ttl: "1h"` on every cache marker; if the gateway
     ignores the field the cache silently stays at 5m, so the setting is harmless
     where unsupported. The 1h tier costs 2x input on the cache write (vs 1.25x
     for 5m); reads are 0.1x either way.
   - `.auxiliary.vision` (the whole node is **overwritten**) → `provider: custom:autonomous`,
     `model: qwen/qwen3.6-plus`, `timeout: 120`, `download_timeout: 30`, `extra_body: {}`
     — the image-understanding model, routed through the same autonomous provider.
   - `.agent.image_input_mode = "auto"` — lets the agent decide when to attach images.
   - `.terminal.cwd = /root/.hermes` (always overwritten) — what makes the OS
     `AGENTS.md` block reachable: Hermes discovers project-context files from the
     **configured** cwd, and the stock relative `.` leaves that lookup empty. See
     *AGENTS.md rule block* below.
   - `.approvals.mode = "off"` — disables Hermes' command-approval prompts (tirith /
     dangerous-command cards) entirely. The device runs unattended on voice + chat
     channels, where an approval card is a dead end that stalls the turn (product
     decision). Hermes' hardline blocklist still applies. Written force-quoted
     (`style="double"`): yq v4 (YAML 1.2) emits a bare `off`, but Hermes parses
     config.yaml with PyYAML (YAML 1.1) where bare `off` is boolean `False` — the
     mode never matches and prompts silently stay on.
   - Only `.auxiliary.vision`, `.agent.image_input_mode`, `.approvals.mode` and
     `.terminal.cwd` are written; **other keys under
     `.auxiliary`/`.agent`/`.approvals`/`.terminal` are preserved**
     (`.auxiliary`/`.agent`/`.approvals` are each coerced from a reset-left scalar
     to a map first, same as `.model`).
3. **Syncs per-device values** from `config.json` (only non-empty fields, so
   unconfigured channels are untouched):

| `config.json` | → | Hermes |
|---|---|---|
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |
| `telegram_bot_token` | → | `.env` `TELEGRAM_BOT_TOKEN` |
| `telegram_user_id` | → | `.env` `TELEGRAM_ALLOWED_USERS` |
| `slack_bot_token` / `slack_app_token` / `slack_user_id` | → | `.env` `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_ALLOWED_USERS` |
| `discord_bot_token` / `discord_guild_id` / `discord_user_id` | → | `.env` `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_ALLOWED_USERS` |
| `whatsapp_user_id` | → | `.env` `WHATSAPP_ALLOWED_USERS` |

`.env` `API_SERVER_KEY` must equal `constants.go` `APIKey` (`hermes-api-key`) or
every turn 401s. Hermes must listen on `127.0.0.1:8642` to match `BaseURL`.

To target a different Hermes endpoint / key / model today, edit
`runtimes/hermes/constants.go` and rebuild (making these per-unit configurable is
future work).

### Device persona block in SOUL.md (`soul_ref`, injected every boot)

Hermes shipped without persona injection. Every other runtime writes the device's
character into its prompt file on each boot; Hermes only ever received one through
the openclaw→hermes persona migration (§12), which copies from a *previous*
runtime — so a device that booted straight into Hermes (`gateway.default: hermes`
in `robots/<type>/ROBOT.md`, which is the lamp default) ran with **no persona at
all**, and the routing rules that turn a `[sensing:*]` event into a skill call were
simply absent.

- `EnsureOnboarding` calls three writers in order (`runtimes/hermes/onboarding.go`):
  `ensureSoulMDBlock()` (the persona, into SOUL.md), `ensureAgentsMDBlock()` (the OS
  rule set, into AGENTS.md — next section), then `pruneSoulOSRuleBlock()` (drops the
  rule block from SOUL.md on a device that carries it from an older os-server). So
  **SOUL.md now holds the persona only**. All three run after presync (whose §0
  `claw migrate` can rewrite the soul), all three are best-effort (`slog.Warn` on
  failure), and all three sit **outside** the gateway-restart decision — both files
  are read per session, not at gateway start.
- The text comes from `device.ResolveSoul(deviceType)` (`system/device/soul.go`),
  the runtime-agnostic resolver for the `soul_ref` declared in
  `robots/<type>/ROBOT.md`: **no ref → no-op and no error** (a soulless body keeps
  whatever default soul Hermes ships); an `http(s)://` ref is downloaded with a 30 s
  timeout; any other value is read as a path under `DevicesDir()/<type>/`; a
  declared-but-unresolvable ref is an error (surfaced, boot continues). The Hermes
  factory reset uses the same resolver via `resolveSoulContent`
  (`runtimes/hermes/reset.go`), falling back to `hermesSoulFallback` on error so a
  wiped device still comes back with a parseable soul — onboarding re-injects the
  real one on the next boot.
- `upsertSoulPersonaBlock` leaves exactly one `<!-- OS DO NOT REMOVE -->`…`---`
  block at the **top** of `~/.hermes/SOUL.md`, dropping the previous one wherever it
  sat — so an OTA with new soul wording refreshes in place instead of stacking a
  second copy. The block is byte-identical in shape to the one openclaw/picoclaw
  write (`osMandatoryMarker`), so a runtime switch in either direction carries the
  persona across unchanged.
- Owner content below the block is preserved, with one exception shared with the
  other runtimes: a **managed default soul** left there is discarded rather than
  kept as fake owner edits, or the file grows a second, competing persona.
  `isManagedDefaultSoul` (counterpart of openclaw/picoclaw's
  `isDefaultSoulHeading`) matches the prefixes in `managedDefaultSoulPrefixes`.
  Owner edits under `## Personal` survive the discard.
- **The Hermes gateway re-seeds its own persona whenever SOUL.md is missing**, and
  presync runs before `ensureSoulMDBlock` — so on a freshly flashed device that
  seed is exactly what the persona upsert finds below the block it just wrote. It
  is the one managed default that opens with **prose, not a heading**
  (`You are Hermes Agent, built by Nous Research…`), which is why
  `managedDefaultSoulPrefixes` is a prefix list rather than the heading-only check
  the other runtimes use. Left in place it contradicts the device persona outright
  — one file telling the agent it is Lamp and, a few lines down, that it is Hermes
  Agent. The remaining entries are `hermesSoulFallback` (`# Hermes Agent Persona`,
  written by a factory reset on a device with no `soul_ref`) and the `# Soul` /
  `# SOUL.md` shapes that reach Hermes through migration.
- A first install is seeded with the same owner-editable `## Personal` section
  openclaw/picoclaw/codex/opencode write, word for word, so the owner has a place
  to write that an OTA will not overwrite.

### AGENTS.md rule block (device skills beat Hermes bundled skills)

For a live connector request, `skills/connectors/SKILL.md` requires Discover
in one terminal call immediately after loading the skill, before auxiliary
skill reads such as `input-branching` for ordinary voice input. A successful
check with no matching connector ends that service task with a short reply;
unreadable or invalid config is a verification failure, not proof of absence.
Explicit history-only routing still takes precedence. This is skill guidance,
not a runtime latency guarantee.

Hermes ships its own bundled skill catalog, and left alone it weighs those as
equals of the device's platform skills — so any request both catalogs can serve
may get routed to a bundled skill instead of the device one. Example: asked to
"send an email", it can pick a bundled email skill and start installing CLI
tools (himalaya) while the `connectors` skill already has the device's Gmail
credentials on disk. The rules that prevent this ride in an OS-managed
**`~/.hermes/AGENTS.md`** block — the same slot openclaw/picoclaw/codex/opencode
use (claudecode uses `CLAUDE.md`). Hermes reads that file now; why it did not
before is the `terminal.cwd` bullet below.

- `EnsureOnboarding` calls `ensureAgentsMDBlock()`
  (`runtimes/hermes/onboarding.go`) right **after** presync and after the persona
  block above. `upsertAgentsMDBlock` strips any previous
  `<!-- OS DO NOT REMOVE -->`…`---` block (`soulOSMarker`, the shared one) and
  writes the embedded `agentsMDBlock` at the **top** of `~/.hermes/AGENTS.md` — so
  an os-server OTA refreshes the wording, and a factory reset that drops the file
  self-heals on the next boot. Owner content below the block's closing `---` is
  preserved.
- **Why an `AGENTS.md` works now, and did not before.** Hermes discovers
  project-context files (`AGENTS.md`, `.cursorrules`) by walking up from the
  **configured** cwd: `resolve_context_cwd()` (Hermes' `agent/runtime_cwd.py`, used
  by `agent/system_prompt.py`) returns `None` rather than falling back to the launch
  directory — a deliberate guard so an agent self-spawned inside the Hermes source
  tree cannot swallow that repo's own `AGENTS.md`. Hermes ships `terminal.cwd: .`,
  which is relative and never bridged to `TERMINAL_CWD`, so that lookup was empty
  and an `AGENTS.md` written there was a file nothing read. `presync.sh` now pins
  `terminal.cwd` to the Hermes home with `yq` (§1b2 *TERMINAL CWD*, always
  overwritten). The gateway process already runs there
  (`WorkingDirectory=/root/.hermes` in the unit Hermes installs), so this only
  states an existing fact — the agent's shell cwd does not move. Verified on
  lamp-0c89: with `terminal.cwd: .` a codeword planted in `/root/.hermes/AGENTS.md`
  was absent from the prompt (the agent answered it had no such codeword); with the
  absolute path the agent returned the codeword. With the real block shipped, the
  agent quoted all three version-check commands verbatim — a rule that appears only
  in AGENTS.md (0 occurrences in SOUL.md).
- **The old copy is pruned out of SOUL.md.** A device updating from an os-server
  that predates the move still carries the rule block inside SOUL.md; two copies in
  two prompt files are wasted tokens on every turn and a future contradiction when
  only one gets updated. `pruneSoulOSRuleBlock()` removes it every boot, in **either**
  shape it shipped in — under `soulSkillPriorityMarker`
  (`<!-- OS HERMES SKILL PRIORITY -->`), or under the shared `soulOSMarker` matched
  by `soulSkillPrioritySentinel`, the block's `**Skill priority (MANDATORY):**`
  first line, so a *persona* block wearing that marker is never touched.
  `stripSoulOSRuleBlock()` is the pure helper; `stripSoulMarkedBlock(text, marker,
  match)` takes the marker plus an optional body predicate and removes only the
  blocks that predicate accepts. `isSkillPriorityBody` is that predicate, and its
  inverse `isPersonaBody` guards the persona upsert — the same mechanism that kept a
  migrated device's persona from being deleted when both blocks shared one marker
  in one file (issue #403).
- The block carries **the OS rule set every runtime gets** — eight rules: skill
  priority (`skills/openclaw-imports/` beat any overlapping Hermes bundled skill;
  third-party services go through `connectors`; never install an alternative
  client/CLI for a service a connector covers), `memories/USER.md` discipline,
  skill scope before acting — including the four-branch `SKILL.md` selection
  protocol (a `[skills: a, b, c]` tag is an authoritative whitelist; with no tag
  pick the single most specific skill; several matches take the most specific; no
  match reads none), `Skills > memory > history` ordering, writing memory in the
  same turn it happens (kept distilled — `MEMORY.md` is in every prompt and nothing
  rotates it, unlike openclaw's daily `memory/*.md`), `[user]` messages answered
  before `[activity]`/`[emotion]`/`[speech_emotion]`/`[ambient]`/`[sensing:*]`
  context, the version-check commands, and `NO_REPLY` as the literal silence token.
- **One rule is deliberately left out**: openclaw's `hooks/` rule, which describes
  `handler.ts` triggers firing on `message:preprocessed`. Hermes never loads those
  — its thinking-emotion ack is an os-server-side hook
  (`runtimes/hermes/emotion_ack.go`), and `~/.hermes/hooks/` holds only
  `os-server-observer`. Shipping the rule would describe machinery that does not
  run. Paths are adapted too: Hermes has no `KNOWLEDGE.md` and no daily
  `memory/*.md`, so both fold into `memories/MEMORY.md`.
- Both managed files are written atomically (tmp + rename) through the shared
  `writeManagedFile` — renamed from `writeSoulFile` now that it writes AGENTS.md as
  well as SOUL.md, same helper `UpdateIdentityName` relies on. Owner content and
  the inlined identity card in SOUL.md are untouched, as is the persona block, and
  **no gateway restart is needed**: Hermes re-reads both files at the next session.

### Channel capability & live add/refresh

Hermes is a **first-class channel runtime** in the generic capability flow
(`runtimes/hermes/channels.go`). Hermes Agent delivers **telegram / slack /
discord** natively inside its own server — a channel is enabled by the presence of
its tokens in `~/.hermes/.env` (Slack uses Socket Mode → `SLACK_APP_TOKEN`), which
the §10 `.env` mapping table above already populates from `config.json`. os-server
runs **no channel receive loop** of its own; its only job is to land creds in `.env`
and bounce the gateway.

- **`SupportedChannels()`** returns `[telegram, slack, discord]`. **WhatsApp is NOT
  supported on Hermes** (Baileys pairing is OpenClaw-only) → `AddChannel` /
  `RefreshChannelConfig` for `whatsapp` return `domain.ErrChannelNotSupported`
  (capability gated via `domain.ChannelSupported`).
- **`AddChannel` and `RefreshChannelConfig` are no longer no-ops.** They used to be
  silent `return nil` stubs; they now re-sync `~/.hermes/.env` from `config.json` by
  reusing the presync primitive and restart `hermes-gateway` **only when config
  changed**. Mechanism: `syncChannelsEnv()` → `EnsureOnboarding()` → `runPresync()`
  (upserts the `.env` channel vars) → the `config.yaml`+`.env` hash-diff →
  `restartHermesGateway()`. Both reduce to "re-sync `.env` + restart-if-changed", so
  they share one code path.
- **Persist-then-apply.** The device layer (`system/device/channels.go`
  `AddChannel`) capability-gates first, then persists the channel creds to
  `config.json` **before** calling the gateway's `AddChannel`, so the presync run
  re-reads `config.json` and sees the new tokens. A transient apply failure leaves
  creds persisted (the recoverable direction — boot presync / `ChannelReconcile`
  re-applies them).
- **Runtime switch vs live add.** On a **switch into Hermes**, the presync hook
  already runs before the gateway starts, so slack/discord carry over
  automatically; the new code closes the **live** add/refresh gap (adding a channel
  while already running on Hermes). The startup `ChannelReconcile`
  (`system/agent/channel_reconcile.go`) also re-applies channels after a switch,
  but for Hermes it is effectively a **no-op** — presync already synced `.env`, so
  the hash-diff finds no change and skips the restart. It also records WhatsApp as
  unsupported (`ChannelsUnsupported`) for the info uplink, leaving its creds for a
  switch back to OpenClaw.

### MCP connectors (`mcp_servers` in `config.yaml`)

Remote-MCP connectors (Notion, Linear, Asana, GitHub, Ahrefs, …) wired by the
backend's `connector.set` MQTT flow are first-class on Hermes:
`WriteMCPEntry`/`RemoveMCPEntry` (`runtimes/hermes/mcp.go`) upsert/delete
`mcp_servers.<name>` in `~/.hermes/config.yaml` and restart `hermes-gateway`,
mirroring `runtimes/openclaw/mcp.go` (which edits `openclaw.json` `mcp.servers`).

The connector writer hands the gateway a canonical, OpenClaw-shaped entry —
`{type:"http", url, headers}` for hosted MCP, or `{command, args, env}` for stdio.
`toHermesMCPEntry` translates it to the Hermes `mcp_servers` schema: Hermes infers
the transport from the presence of `url` vs `command`, so the OpenClaw-only `type`
discriminator is dropped and `enabled: true` is asserted. The presync hook edits
only `.model`/`.custom_providers`/`.env` (via `yq`) and leaves `mcp_servers`
untouched, so the two `config.yaml` owners do not collide; the read-modify-write is
serialized under `HermesService.mcpMu`. As with OpenClaw, `config.yaml` must already
exist (connectors are configured post-onboarding) — a `hermes setup --reset` wipes
`mcp_servers` along with the rest, and the next `connector.set` re-pushes.

**Cloned on a runtime switch.** `MCPReconcile` (`system/agent/mcp_reconcile.go`)
mirrors `ChannelReconcile`: gated by `config.MCPAppliedRuntime`, it fires once in the
startup sequence on an observed switch, reads the **previous** runtime's MCP entries
straight from its on-disk config (`openclaw.json` `mcp.servers` ↔ `config.yaml`
`mcp_servers`, normalizing each to the canonical shape), and re-pushes them through
the now-active gateway's `WriteMCPEntry`. Each entry is self-contained (the auth
header carries the token inline), so the clone is a pure config→config copy — no
token-file/refresh machinery. A clone error leaves the marker un-advanced so the next
boot retries (neither switch direction wipes the other runtime's config).

## 11. Switching backends at runtime

The switch mechanism is **generic** (backend-agnostic) and fully documented in
[`adding-agent-runtime.md`](adding-agent-runtime.md) §2–§3: three triggers (MQTT
`hermes.setup`, HTTP `POST /api/device/agent-runtime {"runtime":"hermes"}`, web
Settings → *Runtime*) funnel into `device.Service.UpdateAgentRuntime`, which runs
`switch-runtime <new> <old>` under `systemd-run --wait` and persists
`config.agent_runtime` only after a clean exit (so a mid-switch crash resolves the
still-installed old backend). Hermes-specific facts the generic switcher relies on:

- **Unit name** `hermes-gateway.service` (not `hermes.service`) — declared in
  `/usr/local/lib/os-runtimes/hermes/service` so `switch-runtime` enables the right
  unit; `reset_hermes.go` targets the same unit.
- **Verify hook** `/usr/local/lib/os-runtimes/hermes/verify` runs `command -v
  hermes` (cheap CLI-presence check). It is deliberately **not** a config-structure
  check — config self-heals via presync (§10), so a verify failure would force an
  unnecessary full reinstall.
- **Presync** `runtime-hermes-presync` runs before the gateway starts (§10).
- The MQTT `hermes.setup` ack reflects the **real** outcome (success only after the
  switch lands; failure with the rollback reason otherwise), since
  `UpdateAgentRuntime` blocks on the switcher's exit code.

Confirm the swap from the `AGENT BACKEND ACTIVE → HERMES` banner + a healthy
`/health` poll.

## 12. Persona, memory & skills carried across a switch

Switching openclaw→hermes runs a Go persona migration
(`system/agent/migrate_persona/openclaw_to_hermes.go`) at os-server boot —
**separate from `claw migrate`**. It carries, into `~/.hermes/`:

- **SOUL.md** (rebranded) — and, because Hermes has no separate IDENTITY.md slot,
  inlines the owner's filled IDENTITY fields as a `## Your identity card` block so
  the custom name (e.g. "Ngân") survives. `UpdateIdentityName` (device rename) edits
  that block; `WatchIdentity` (`runtimes/hermes/identity.go`) polls SOUL.md and, on
  a name change, pushes the new wake words to HAL + `i18n.SetDeviceName` — mirroring
  OpenClaw's `WatchIdentity`, just watching SOUL.md instead of IDENTITY.md. The
  migration overwrites SOUL.md, but `EnsureOnboarding` (which runs after the
  migration in the startup sequence) re-asserts the device persona block at the top
  of it via `ensureSoulMDBlock`, and `pruneSoulOSRuleBlock` strips any OS rule block
  the migrated file dragged along. The rule set itself lives in `AGENTS.md`, which
  the migration never touches (see *Device persona block in SOUL.md* and *AGENTS.md
  rule block* above). A device that never migrates gets its persona from
  `ensureSoulMDBlock` alone.
- **MEMORY.md + daily `memory/*.md` + KNOWLEDGE.md** → merged into
  `memories/MEMORY.md`. Hermes loads only `MEMORY.md` + `USER.md` **by name** (no
  `memories/*.md` glob), so KNOWLEDGE is folded in rather than kept as a separate,
  ignored file.
- **USER.md** → `memories/USER.md`.

Identity parsing and renaming accept only a standalone `**Name:**` field line,
optionally prefixed by a Markdown bullet (`-` or `*`). Inline mentions such as
“Do NOT fill `**Name:**` or the other single-value fields” in managed SOUL
instructions are ignored, so they cannot become wake words or be overwritten
when the device is renamed.

The soul copy uses `Overwrite=true` (a switch adopts the source runtime's persona;
backed up first). The reverse hermes→openclaw **strips the identity card from the
SOUL and restores its fields back into OpenClaw's `IDENTITY.md`** (`restoreIdentityCard`,
the inverse of the inline) — so the name set under Hermes survives the trip back,
not just the trip out. **Skills** stay fresh under Hermes via two complementary
paths: every `EnsureOnboarding` capability-gates and reconciles the full supported
catalog from the CDN into `skills/openclaw-imports` (repairing stale local files
even when OTA was published before the watcher started), while `skill_watcher.go`
polls OTA metadata every five minutes for later publishes. Both use the shared
`system/skills/skillzip.go` engine; a real content change restarts the gateway and
then tells the agent to re-read the changed skills. A failed ZIP download or extract
does not advance its version, so the next five-minute poll retries it.

**Two skill roots.** `~/.hermes/skills/` is namespaced, and os-server writes to a
second root of its own (`runtimes/hermes/save_skill.go`):

| Root | Owner | Written by |
|------|-------|------------|
| `skills/openclaw-imports/` | `hermes claw migrate` + the skill watcher | presync §0, CDN updates |
| `skills/authored/` | the device | `AgentGateway.SaveSkill` / `InstallSkillArchive` (web UI "Write skill" / "Install") |

The split is load-bearing, not cosmetic: presync §0 restores the imported
platform skills **only when `openclaw-imports` is empty**, so an authored skill
written in there would keep that guard permanently satisfied and a factory reset
would silently never restore the real imports. `ListSkills` merges both roots
(`skills.ListInstalledFrom`, device root first on a name clash). Hermes
discovers skills anywhere under `~/.hermes/skills`, so no config change is
needed, and no gateway restart either — skills are re-read per session.

Note that `wipeHermesState` (reset.go) clears `skills/openclaw-imports` but
**not** `skills/authored`, so user-authored skills survive a factory reset.

**MCP connectors are carried across too** — the configured remote-MCP servers are
cloned config→config by `MCPReconcile` on the same switch boot (see §10, *MCP
connectors*), so a device that had Notion/Linear wired under OpenClaw keeps them
under Hermes (and vice versa).

### Round-trip is content-lossless but structurally one-way (Hermes-specific)

Persona, name, user profile, and memory **content** survive openclaw→hermes→openclaw
without loss. The one **structural** asymmetry is a consequence of Hermes loading
only `MEMORY.md` + `USER.md` by name (no `KNOWLEDGE.md`, no daily-memory slot):

- The forward step **folds** OpenClaw's `KNOWLEDGE.md` and daily `memory/*.md` **into**
  the single Hermes `MEMORY.md`. On the way back those entries are already merged, so
  they all land in OpenClaw's `MEMORY.md` — **never split back out** into a
  `KNOWLEDGE.md` or per-day files. No data is lost; the structure is flattened.

This is specific to Hermes's memory model — a backend that *does* have those slots
would map them 1:1 and round-trip cleanly. (See the fold-vs-move rule in
[`adding-agent-runtime.md`](adding-agent-runtime.md) §4.)

> **Adding another backend** is a generic recipe — see
> [`adding-agent-runtime.md`](adding-agent-runtime.md) for the `AgentGateway`
> contract, the install/presync pattern, migration, skills, hooks, reset, and the
> full checklist.

## 13. Optional Jev skill preloading

The OS-managed `jev` plugin selects one installed skill before a Hermes user
turn's first model call through the `pre_llm_call` hook. On an accepted decision,
it revalidates the skill against the live eligible catalog and loads its content
through native `tools.skills_tool.skill_view(name, task_id, preprocess=False)`.
The skill is injected into ephemeral context for the current turn, so Hermes
receives its instructions without first having to choose and call `skill_view`.
This changes the OS-managed plugin, configuration and instructions, not Hermes
core. The managed `AGENTS.md` instructions count a complete native Jev preload
for **this turn** as satisfying the mandatory skill read; Hermes should not
reread that same `SKILL.md`. A partial preview or a user claim that a skill was
read does not satisfy this rule. Loading instructions does not
execute the skill's actions, authorize tools, or bypass mandatory
connector/platform rules and permission checks.

Preloading fails open to normal Hermes discovery if the selected skill is
missing or disabled, the native API is unsupported, reading fails, or the native
JSON result exceeds 128 KiB. The final context must also fit within 131,072
characters and, when native hook output spilling is enabled, its configured
`max_chars` threshold (whichever is smaller). Otherwise it fails open rather
than logging `preloaded` while Hermes replaces the content with a file pointer.
Older Hermes without the spill API uses the local character cap. Shell
preprocessing is disabled; skills containing
dynamic shell snippets (an exclamation mark followed by a backtick-delimited
command) also fail open. Timeout results cannot be attached to a later turn.
System notices prefixed with `[system]` bypass routing and preloading.

### Installation on existing devices

`runtimes/hermes/jev_plugin.go` embeds the Python plugin under
`runtimes/hermes/plugins/jev/` into os-server. `EnsureOnboarding` reconciles it on
OS startup and setup for a local Hermes installation with an existing
`/root/.hermes/config.yaml`. Remote Hermes is skipped. Thus an existing device
receives the plugin with its OS update; rerunning provisioning or installing a
Python package separately is unnecessary.

Go writes changed assets atomically to `/root/.hermes/plugins/jev/`
and adds `jev` to `plugins.enabled` in Hermes's `config.yaml`, preserving
other plugin settings and an explicit `plugins.disabled` entry. The generated
`os-config-path.json` contains only the absolute path to the OS config, never an
API key. Unchanged assets are not rewritten.

When Jev is not explicitly disabled, sync also sets
`hooks.output_spill.max_chars: 131072` **only if unset**. Explicit thresholds and
the `plugins.disabled` setting are preserved. This raises Hermes's global
per-hook output threshold from its 10,000-character default so a complete skill
can stay inline; it does not disable output spilling. The loader respects an
explicit smaller threshold and falls back if the full context will not fit.

Plugin installation or updates do **not** add a gateway restart reason. os-server
logs that loading changed plugin code needs the next gateway restart; existing
restart reasons elsewhere in onboarding remain unchanged. This build enables Jev
in the embedded plugin. Existing devices need the updated OS to sync the plugin
and a gateway restart to load its changed code; this update does not add an
automatic restart. To disable Jev, set `ENABLED = False`, rebuild os-server, sync
the plugin, and restart Hermes to load it.

### Configuration and proxy contract

The plugin uses hardcoded defaults in `runtimes/hermes/plugins/jev/router.py`:

```python
ENABLED = True
TIMEOUT_SECONDS = 3.0
```

There is no separate Hermes Jev config block or Go config type. These defaults
are independent of `local_intent` and `jev_intent`. OFF bypasses even config
reads, catalog lookup, and network requests.
When enabled, the plugin reuses `llm_base_url` and `llm_api_key` for
`POST {llm_base_url}/jev/decisions` with bearer authentication. There is no
separate Jev key in `.env` and no direct-provider fallback. Requests identify the client
with `User-Agent: AutonomousOS-Jev/0.1`; the proxy edge rejects Python urllib's
default signature with HTTP 403. HTTP failures log only the status code, never
credentials or response bodies. HTTPS is required,
except HTTP on loopback for local tests. BFF must support the compatible
[Decisions contract](../os-server.md#jev-bff-contract); the plugin sends model
`typesafe/jev-1.13`, a `skill` choice including `none`, and one `fit_<id>` noul
question per candidate. The raw response must contain `answers`. Routing
instructions prioritize the explicitly requested action over small talk,
question/context modifiers, and proactive or supporting skills. Missing action
parameters do not prevent routing: the selected skill can resolve them later.

Only the current user message and OS platform skill names/descriptions are
sent. The roster scans `skills/openclaw-imports` under the active Hermes home,
using Hermes's native `agent.skill_utils` helpers: `iter_skill_index_files`,
`parse_frontmatter`, `get_disabled_skill_names`, `skill_matches_platform`, and
`skill_matches_environment`. This avoids `skills_list()` deduplication by first matching
name hiding an OS skill behind a bundled skill with the same name. Other bundled,
authored, and plugin skill categories are excluded. Metadata reads are bounded;
conversation history and skill bodies are not sent. Each candidate description
is capped at 500 characters. Native `skill_view` lookups use the qualified
`openclaw-imports/<relative directory>` path to avoid name collisions.

If there are more than 32 eligible candidates, the router skips Jev entirely
rather than truncating the catalog. Empty messages, messages over 8,000 UTF-8
bytes, slash commands, `[system]` notices, and explicit `[skills:...]` selections also bypass the
router. The catalog worker inherits the current Hermes context so
session/platform skill filters still apply. This preloading experiment covers
OS platform skills; normal Hermes discovery continues to handle other skills.

A selection requires choice probability at least 0.70, a margin of at least
0.20 over the runner-up, and fit at least 0.60. These are provisional thresholds
for skill preloading, not action authorization; skill and platform
permission checks still apply. Buddy and OS intent thresholds remain unchanged
at choice 0.90, margin 0.40, and fit 0.95. Uncertain or invalid decisions leave
normal Hermes behavior unchanged. The decision wait is temporarily hardcoded to 3 seconds for proxy validation before latency tuning; there are no retries or redirects. An error or timeout starts
a 30-second cooldown. One worker per router is allowed; a busy router bypasses
immediately. A timed-out worker may finish its request in the background, but
its late result cannot inject skill content. HTTP 429 follows the same fallback
and 30-second cooldown; no provider error body or `Retry-After` diagnostics are
added by preloading.

Structured logs distinguish accepted selections from valid abstentions and
failures instead of grouping them as `deferred`:

| Outcome | Meaning / reason |
|---|---|
| `preloaded` | A valid decision passed all acceptance thresholds and native skill content was loaded for this turn |
| `abstained` | A valid decision selected `none` or failed `low_choice`, `low_margin`, or `low_fit` |
| `error` | `http_error`, `network_error`, `invalid_json`, `response_too_large`, `provider_error`, `invalid_schema`, `catalog_error`, `thread_error`, `config_error`, `skill_unavailable`, `skill_load_failed`, or `preload_timeout` |
| `skipped` | `disabled`, `invalid_message`, `explicit_selection`, `system_message`, `unconfigured`, `cooldown`, `busy`, or `no_candidates` |
| `timeout` | The decision wait budget expired |

Logs include validated `session_id`, `turn_id` and `task_id` when supplied by
Hermes, so a slow turn can be correlated without logging its prompt. Accepted
preloads include `context_chars`. Native preload failures distinguish
`skill_response_size`, `skill_rejected`, `skill_empty`, `skill_dynamic`, and
`skill_inline_budget`; other native failures remain `skill_load_failed`.
Logs include total `decision_ms`, and `catalog_ms`, `request_ms`, and native
`load_ms` when available;
`request_ms` measures the full proxy round trip, not model inference alone.
Valid decisions include numeric `choice_probability`, `margin`, and `fit` where
applicable. `candidate` records the qualified selected name even when a valid
decision is rejected, or `none` when no skill was chosen; accepted selections
also include `skill`. HTTP errors include `http_status`. Logs exclude prompts, credentials,
response bodies, and exception text.

Local validation uses mocked proxy responses and temporary Hermes homes. Neither
these tests nor individual synthetic on-device probes establish live routing
accuracy, coverage, or latency improvements. Compare OFF/ON on representative
requests using the installed roster and a compatible BFF endpoint; do not treat
synthetic probes as a benchmark. No device deployment is required by the local
checks.

Focused local checks (Python plugin tests also run in CI):

```bash
go test -race -timeout 90s ./runtimes/hermes ./system/server/config ./system/intent/...
python3 -B -m unittest discover -s runtimes/hermes/plugins/jev -p 'test_*.py' -v
```

### Reference implementation review (2026-09-21)

Reviewed actual code from the community
[`typesafe-skill-router`](https://github.com/DECRUX9812/typesafe-skill-router/tree/e6cdac26f9ed588b4a94b8a2f7f9f026e1b9faf3)
at the Hermes catalog pin `e6cdac26f9ed588b4a94b8a2f7f9f026e1b9faf3`, and upstream
`f150284d3da33c85b8adc97e2d326987573b30d3`. This is a community plugin listed by
Hermes, not Hermes core or a TypeSafe-maintained plugin. Its routing recipe is
based on the [TypeSafe skill-suggestion cookbook](https://docs.typesafe.ai/cookbooks/skill_suggestion).

| Area | Reference plugin | OS plugin |
|---|---|---|
| Hook | `pre_llm_call`, returns optional user-message context | Same hook; accepted skill content is loaded natively into ephemeral current-turn context; no system-prompt modification |
| Skill data | Filesystem roster; shortlist receives full descriptions and up to 700 body characters | Bounded metadata from `openclaw-imports` files with native Hermes eligibility filters, descriptions capped at 500 characters, no body text; qualified skill lookup avoids name collisions |
| Decision | Stage 1 ranks and gates skill need; stage 2 reranks a shortlist of 3 (per chunk) | One request with choice, `none`, and per-candidate fit |
| Large catalogs | Splits into chunks of 240 choices | Skips when more than 32 eligible skills |
| Acceptance | Catalog pin: gate 0.30 and winner fit 0.40; newer upstream also arbitrates disagreeing choice/fit signals | Choice 0.70, margin 0.20, fit 0.60; provisional preloading thresholds, uncalibrated for this workload |
| Latency | Default hook budget 10 seconds, answer cache and retry-capable client | Temporary 3-second diagnostic budget, no retries/cache, busy bypass and cooldown |
| Credentials | TypeSafe API with separate key | Shared OS proxy credentials |

The OS implementation is a narrower experiment, not an equivalent implementation
of the two-stage recipe. Its provisional thresholds and shorter deadline may suppress
useful selections; mock tests and limited synthetic probes cannot establish
calibrated routing accuracy or coverage.
Do not transfer the reference's benchmark results to this plugin. To evaluate
the enabled experiment, compare both policies on the same representative requests and installed roster,
including unrelated chat, ambiguous skills, explicit commands, and Vietnamese.

Review fixes preserve Hermes session context in the worker (needed for
platform-specific disabled-skill filtering) and skip slash commands, as the
reference hook does. Those review fixes did not change thresholds, the default
OFF setting at review time, or device state. The current build enables the
plugin separately as described above.

### OS-owned preparation deadlines

Hermes implements the optional `domain.RunExpirer` interface for native managed
runs. `ExpireRun(ctx, runID, reason)` accepts cancellation only when `runID` is
both the active reply owner and the latest admitted request. An old deadline
cannot stop a newer steered request, including a web request sharing the same
native run. Unsupported/non-native runs return an error.

The expiry cancels only that run's stream context; the existing reader sends a
remote stop and checks terminal status, bounded by its 30-second cleanup budget.
The normal lifecycle reports an error with the OS deadline reason, never a
successful task completion. A remote `pending_steer` suffix is never replayed
after expiry. Acceptance is not proof that remote execution has
stopped. If cleanup cannot confirm a terminal state, existing unknown-ownership
handling isolates the conversation instead of replaying work. Unrelated queued
requests retain their existing admission and ownership checks. This is not a
user `/stop`, does not create a model turn, and does not erase the Harness intent
journal or dispatch a Harness task.

### BlueBubbles runtime patch validation

BlueBubbles source patches compile their generated Python before replacing the installed module. The webhook subscription patch keeps its marker on a standalone line so inline registration dictionaries retain their closing delimiters. CI fixture suites cover application, compilation, repeated application and rejected source drift for every embedded patch; these checks do not require a device or BlueBubbles server.
