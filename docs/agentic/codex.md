# Codex agent backend

Codex is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. See `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: the **OpenAI Codex CLI** as the device agent brain, behind a local WS bridge. This doc. Code: `runtimes/codex/`.

> Source of truth is the code. This documents `runtimes/codex/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/agentic/codex_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) ·
> [`picoclaw.md`](picoclaw.md) · this file (Codex).
>
> **Status: built, NOT device-verified yet.** The full stack (install, presync,
> gatewayd, translator, MCP, reset) compiles and is unit-tested, but no device
> has run the switch flow end-to-end. Two explicit on-device checks are flagged
> in the code: the gatewayd must listen on `127.0.0.1:18792` with the
> `constants.go` token, and ⚠️ campaign-api must serve `{base}/responses`
> (Codex speaks the Responses API only — see §1.2).

## 1. Overview & how it is selected

The device runs a thin local **WS bridge**: the `codex.service` systemd unit
runs **`os-server codex-gatewayd`** — the bridge is **compiled into the
os-server binary** (`runtimes/codex/gatewayd`; **no Python on the device**).
Gatewayd owns a persistent Codex App Server JSON-RPC process. It starts a turn
with `turn/start` and sends compatible additional input to an active turn with
`turn/steer`, rather than serializing direct user input through a per-turn
`codex exec` worker.

The bridge exposes `ws://127.0.0.1:18792/codex/ws/` (bearer token
`autonomous_codex_token`) to os-server. The permissive configuration is
deliberate: an appliance running as root must never block on an approval prompt
(`approval_policy = "never"` + `sandbox_mode = "danger-full-access"` in
config.toml, §1.2).

**Steering scope.** `turn/steer` is valid only for the currently active Codex
thread. Once that turn is terminal, the next input follows the normal start or
resume path. This removes runtime FIFO delay for an active conversation, but it
does not override OS safety policy: passive sensing, speaker/TTS safety gates,
offline delivery, and inputs that cannot safely join the active thread may
still be queued by os-server.

`agent_runtime` in `config.json` picks the backend; resolution lives in
`system/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, anything unknown falls back to OpenClaw. On startup a
`AGENT BACKEND ACTIVE → CODEX` banner prints `ws_url` + `conversation`.

Wire values (`runtimes/codex/constants.go`). All three device paths below are
resolved once at process start from the SAME env vars the gatewayd and
`presync.sh` read (`system/lib/syspath`); unset env gives the device defaults,
so the board is unaffected:

| Value | Default | Env | Meaning |
|---|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | `CODEX_PORT` | Local bridge WebSocket endpoint |
| `Token` | `autonomous_codex_token` | `CODEX_WS_TOKEN` | Bearer token on connect; the bridge reads the same value from `$CODEX_HOME/.env` (presync-owned) |
| `codexHome` | `/root/.codex` | `CODEX_HOME` | State dir every other codex path derives from — workspace, skills, sessions, config.toml, `.env`, the telegram state files |
| `Conversation` | `device-main` | — | Label only — Codex owns its thread ids (§3) |

Setting `CODEX_HOME` alone relocates the whole backend, on both the client and
`codex-gatewayd` (which anchors its per-file defaults on it). That is what
`make os-dev` / `make codex-dev` use to run the shipped binary off-device — see
[os-server.md § Off-device run](../os-server.md#off-device-run-laptop).

## 1.1 Install (`install.sh`)

A `codex.setup` switch runs the generic `system/device/switch_runtime.sh`,
which materializes Codex's embedded scripts. `install.sh` (one-time, self-
sufficient — a direct `bash install.sh` fully configures AND starts the
backend):

1. prerequisites `jq` + `curl`;
2. installs the Codex CLI from a **pinned GitHub release** (`rust-v0.142.5`,
   asset `codex-aarch64-unknown-linux-musl.tar.gz` — static musl, no runtime
   deps) to `/usr/local/bin/codex`; idempotent (skips when the pinned version
   is already installed);

   > **Updating a device already in the field** does NOT go through this pin:
   > publish with `make upload-codex <bare-semver>` + `make promote-codex`, and
   > the bootstrap worker runs `software-update codex` (binary swap + restart)
   > on every device whose `agent_runtime` is `codex`. The pin here is the
   > baseline a freshly flashed image starts from — keep it in step with
   > `scripts/imager/build-orangepi.sh`. See `docs/bootstrap-ota.md` §5.
3. runs the presync hook once (`/usr/local/bin/runtime-codex-presync`,
   materialized by os-server BEFORE the installer — §1.2);
4. writes + enables **`codex.service`** (`ExecStart=/usr/local/bin/os-server
   codex-gatewayd`, `EnvironmentFile=/root/.codex/.env`, `HOME=/root`,
   `Restart=always`) — nothing to materialize for the bridge, it ships inside
   os-server; then drops a cheap offline `verify` hook (`command -v codex` +
   os-server binary present) for switch-runtime self-heal.

Unit name == runtime name (`codex.service`), so no `os-runtimes/codex/service`
declaration file is needed. Install logs go to `/root/.codex/install.log`
(persistent rootfs — `/var/log` is volatile zram on these boards).

## 1.2 Presync (`presync.sh`) — embedded, runs on every switch + every boot

`presync.sh` is embedded in os-server and materialized to
`/usr/local/bin/runtime-codex-presync`. It runs before every codex start
(switch-runtime), once at the end of install, **and on every os-server boot /
config change via `EnsureOnboarding`** (hermes pattern): `EnsureOnboarding`
hashes the presync-owned files (`config.toml` + `.env`) around the run and
restarts the gateway only on a real change. It owns everything stateful:

- **§1 MIGRATE** — one-time persona/memory/skills copy from the openclaw
  workspace, gated on the marker `/root/.codex/.openclaw-migrated`. Stops
  openclaw first (3 retries, non-fatal), then copies `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **and `AGENTS.md`**
  verbatim (Codex reads `AGENTS.md` natively — zero-translation persona slot;
  the Go onboarding re-injects the OS block anyway), plus `memory/` + `skills/`
  only when absent. The marker is written only after a clean copy, so a failed
  migrate retries next run; a factory reset wiping `/root/.codex` clears it so
  migrate re-runs on the next switch.
- **§2 CONFIG** — regenerates the head of `/root/.codex/config.toml` from
  config.json. **Auth gate:** when `/root/.codex/auth.json` exists
  (ChatGPT-subscription login, §9) the head is written WITHOUT `model` /
  `model_provider` / `[model_providers.autonomous]` — codex uses its built-in
  default provider + model (keeping only `approval_policy` + `sandbox_mode`,
  and still preserving the `[mcp_servers` tail). Otherwise (api-key mode):
  `model` from `llm_model` (fallback `Auto-AI`),
  `model_provider = "autonomous"` → `[model_providers.autonomous]` with
  `base_url` from `llm_base_url` normalized to end in `/v1` (Codex appends
  `/responses` itself), `env_key = "OPENAI_API_KEY"`, and
  **`wire_api = "responses"` ONLY** — the chat-completions wire was removed
  upstream ~2/2026 (⚠️ VERIFY ON DEVICE: campaign-api must serve
  `{base}/responses`). Also `approval_policy = "never"` +
  `sandbox_mode = "danger-full-access"`. The **tail from the first
  `[mcp_servers` line onward is preserved verbatim** — os-server's `mcp.go`
  owns those entries (§7), so the two owners never collide.
- **§3 ENV** — writes `/root/.codex/.env` (systemd EnvironmentFile, mode 0600):
  `CODEX_WS_TOKEN` (must equal `constants.go` `Token`), `CODEX_PORT=18792`,
  `CODEX_HOME=/root/.codex`, `CODEX_WORKSPACE=/root/.codex/workspace`, and
  `OPENAI_API_KEY` from `llm_api_key` — **omitted in subscription mode** (an
  API key outranks/conflicts with ChatGPT auth).

On top of the presync run, `EnsureOnboarding` (`onboarding.go`) does the same
workspace reconcile the other backends get: seeds `KNOWLEDGE.md` **and
`AGENTS.md`** from their embedded templates only if absent, injects the OS-managed
`<!-- OS DO NOT REMOVE -->` blocks into `SOUL.md` / `AGENTS.md` /
`HEARTBEAT.md` (OpenClaw-derived, stripped of OpenClaw-only bits), refreshes
the **global** user AGENTS.md block (`ensureUserAgentsMDBlock`, see below), and
capability-gates skills. Markdown-only changes never restart the gateway; only
a presync config change or a unit self-heal restarts it.

**Why `AGENTS.md` is seeded.** Codex has no `setup` command to regenerate a base
`AGENTS.md` the way openclaw does, and a **codex-only device** — one that never
ran openclaw, leaving presync §1 nothing to migrate — therefore had none at all.
Since `AGENTS.md` is the only file codex auto-loads, no file meant no OS block
**and no persona**: the agent introduced itself as "Codex". `EnsureOnboarding`
now seeds `runtimes/codex/resources/AGENTS.md` (a short workspace base carrying
the `Your Workspace` heading the block injector anchors on) through
`seedFileIfAbsent`, which **never overwrites** — a device migrated from openclaw
keeps its own file untouched.

**Persona inline block (AGENTS.md).** Codex auto-loads ONLY `AGENTS.md` into
context; the "Session Startup" instruction to read `SOUL.md`/`IDENTITY.md` is
voluntary, and on short turns the model skips it (device-verified: "bạn tên
gì" → "Tôi là Codex"). OpenClaw/Hermes inject the soul into the system prompt
at the runtime layer; codex has no such layer, so `ensurePersonaInlineBlock`
inlines the persona INTO `AGENTS.md`: a
`<!-- OS PERSONA INLINE — DO NOT EDIT (generated from SOUL.md + IDENTITY.md) -->`
… `<!-- /OS PERSONA INLINE -->` block upserted idempotently at the very top
(above the OS mandatory block) containing a mandatory "Who you are" preamble,
the agent name parsed from `IDENTITY.md` (`- **Name:** …`), and the
freshly-reconciled `SOUL.md` verbatim (capped at 20 000 bytes with a
truncation note so `AGENTS.md` stays under codex's 32 KiB project-doc cap).
The block is rebuilt after `ensureSoulMDBlock` on every `EnsureOnboarding`
and also right after a rename (`UpdateIdentityName`), so the very next turn
sees the new name; a missing `SOUL.md` removes the block. Written atomically
(tmp+rename), and only when the bytes actually differ.

### Skills — native `$CODEX_HOME/skills` discovery (`codexSkillsDir`)

Device skills live in **`/root/.codex/skills/<name>/SKILL.md`** — codex-cli's
**native** discovery root (`$CODEX_HOME/skills` on 0.142.x). Codex auto-discovers
every `<name>/SKILL.md` with valid YAML frontmatter here, in **every** session
regardless of cwd, and lists them in the interactive `@` skill picker. This
mirrors the claudecode fix (skills moved to Claude Code's native
`~/.claude/skills`). They are **not** placed in `workspace/skills`, which codex
never scans — a device with skills there gets an empty `@` picker and no native
skill loading. All producers target `codexSkillsDir`: `presync.sh` §1 (openclaw
migration → `$CODEX_DIR/skills`), `skill_watcher.go` (CDN download + the
`notifySkillChanges` message), and `pruneUnsupportedSkills` (capability gate).
`EnsureOnboarding` also refreshes every supported skill from the CDN on boot or
configuration reconciliation. This repairs a stale local skill if OS Server was
restarted after a CDN publish, before the five-minute version watcher observed
the change; unchanged content is not re-notified. The watcher logs every poll and
only records an OTA version after its archive is downloaded and extracted, so a
transient download failure is retried on the next five-minute poll.
After an onboarding-triggered sync that also restarts the Codex bridge, the
re-read notification waits up to one minute for the bridge to reconnect rather
than being dropped while it is unavailable.
`migrateSkillsToCodexHome` lifts any legacy `workspace/skills` left by an older
os-server into the native root and drops the workspace copy (idempotent);
factory reset wipes all of `/root/.codex`, so the set is re-migrated from
openclaw on the next `EnsureOnboarding`.

**Global user AGENTS.md — device-wide rules for coding sessions.** The workspace
`AGENTS.md` only reaches the **device-chat** session: the gatewayd runs
`codex exec --cd /root/.codex/workspace` (`gatewayd/turn.go`), so codex's
repo-root→cwd AGENTS.md walk finds it. A **Telegram coding session**
(`telegram_coding.go`, §"Telegram remote coding-sessions") runs
`codex exec --cd <folder>` in an arbitrary directory (`/root`, `/root/myapp`, …),
whose walk never reaches the workspace file. Codex additionally loads a **global**
user-instructions file, `$CODEX_HOME/AGENTS.md` = `/root/.codex/AGENTS.md`, in
**every** session regardless of cwd (codex-rs `CodexHomeUserInstructionsProvider`,
merged before the project walk). `ensureUserAgentsMDBlock` (`codexUserAgentsMD`)
injects an OS block there — same marker discipline as the workspace file — carrying
the **Connectors (MANDATORY)** rules and the note that device skills live at the
**absolute** path `/root/.codex/skills/<name>/SKILL.md`. Without it, a coding
session has no idea the device's connectors exist and tells the owner
Gmail/Calendar is "not connected". As a read-by-path fallback (independent of
native discovery), the workspace `AGENTS.md` block and `notifySkillChanges` both
cite skills by that **absolute** path, never relative `skills/…` — which would
resolve under the coding session's `<folder>`. No gateway restart is needed
(per-turn re-read); factory reset wipes all of `/root/.codex`, so the global file
is rebuilt on the next `EnsureOnboarding`.

## 2. Transport & sending a turn

`client.go` holds one persistent WebSocket to the bridge (picoclaw-shaped:
bearer token, no pairing handshake, 25s ping keepalive → `pong`, reconnect
with backoff, `StateAgentDown` LED on drop). `chat.go` `sendChat` writes one
frame and returns; the reply arrives on the read loop:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

The bridge saves attachments to `/root/.codex/attachments` and includes them in
the App Server turn input. A `{"type":"session.new"}` frame clears the current
thread (§4). A compatible new message for an active thread uses `turn/steer`;
otherwise gatewayd starts a normal turn. Events remain correlated to the
initiating device `runID`; steering never silently reassigns a turn.

## 2.1 Turn duration — timeout and the busy TTL

A chat turn is expected to be slow: a user opens chat precisely for work that
takes a while, and the same prompt answered over Telegram/OpenClaw runs 35
minutes to completion. Two numbers bound it, and they must stay ordered:

| Knob | Where | Default | Meaning |
|---|---|---|---|
| `CODEX_TURN_TIMEOUT_S` | `gatewayd/gatewayd.go` | `600` (10 min) | Ends the active App Server turn and sends `bridge.error: timeout`. Gatewayd ALWAYS ends a turn — completed, failed, or this timeout. |
| `busyTTL()` | `events.go` | that timeout **+ 5 min** | Unwedges the sensing pipeline when a turn's terminal frame was DROPPED. Derived from the same env var so raising the timeout cannot leave it behind. |

**An expired TTL ends the turn properly — both halves.** It drops the run id AND
dispatches a lifecycle error carrying that id (`failStuckTurn`, wired through
`wsDispatch` because the busy check runs on the sensing path, outside the WS read
loop). Doing only one half is a bug either way: leaving the id set orphans the
NEXT turn (`ensureTurnStarted` returns early while `currentRunID` is non-empty, so
the new turn's frames are attributed to the dead run and the new chat hangs);
clearing it silently leaves the browser on a pending bubble until its own
deadline, which is the "no response" this path exists to prevent.

**The TTL must outlast the timeout.** It was a fixed 5 minutes against a 10-minute
timeout, so every turn slower than 5 minutes tripped the "frame was dropped" path
— and that path also called `clearTurn()`, wiping the id of the turn still
running. The browser's pending run was then orphaned: later lifecycle/error
frames found no current run, allocated a fresh id, and attached to an unrelated
queued turn, so the chat sat on a pending bubble and reported "no response" while
the backend was working normally.

Measured on lamp-0c89, 2026-09-03: run `device-chat-139` started 15:40:41, lost
its id at 15:45:41, and the 15:50:41 `timeout` was reported against
`device-chat-168`. `openclaw` and `hermes` never wiped the id on TTL expiry,
which is why long Telegram turns always worked there; the codex path now matches.

**A timed-out resumed turn DROPS the thread.** Rotation (`ShouldRotateSession`)
reads the context size off `turn.completed`, so it can only ever fire after a turn
SUCCEEDS — a thread whose every resume hangs is therefore never rotated, and the
thread id lives in the session file, so restarting the service and rebooting the
device both resume the same dead thread. Measured on lamp-0c89 2026-09-03: thread
`01a06665` was created 15:31 and every turn after 15:40 hung with no `turn end`
for over an hour, through two service restarts and a reboot; direct `curl` to the
same endpoint answered the same 75k-token payload in 57 s, and a FRESH
`codex exec` answered in seconds, so the endpoint was never the problem. The
gatewayd now calls `clearSession()` on a resume timeout, which is the only escape
that does not need a human.

The web chat's own give-up window (`REPLY_IDLE_TIMEOUT_MS`) is sized to outlast
the turn cap. App Server events can report live progress, but a steer remains
part of the same active turn rather than an independent completion promise.

### Reconnect drains locally buffered requests

Once a WebSocket connection and its event dispatch callback are ready, the Codex
client drains `pendingEvents` without waiting for another turn to finish. This
prevents an unsent user message from remaining queued after a gateway restart
until an unrelated sensing turn ends. Existing speaker deferral, event expiry,
coalescing and priority rules still apply.

Drain callbacks are serialized across reconnect, idle and speaker notifications;
offline drains retain the queue. If no socket write was attempted, the current
event and remaining batch stay queued. A failed socket write has an uncertain
outcome and is **not** automatically replayed. Already transmitted `pendingRuns`
are correlation records only and are never used as a replay source. If a gateway
turn survived the connection drop, its FIFO places newly submitted unsent events
after that turn. The queue remains in memory; this does not persist unsent events
across an os-server process restart.

### Degenerate assistant output guard

The gateway rejects the observed runaway syllable pattern before forwarding an
assistant item to the translator, response logs, or TTS. The conservative check
requires at least **64 KiB** of assistant text and **4096 whitespace-delimited
words**: at least **95%** must be covered by at most **eight alphabetic tokens**
of at most **eight Unicode characters**, each occurring at least **128 times**.
Candidate vocabulary is bounded at **1024** tokens; diverse text is accepted.
This is a repetition heuristic, not a maximum answer length: long code, numeric
fixtures, and ordinary explanations remain eligible. Tool output is not classified.

On rejection the gateway ends the active App Server turn, suppresses the
rejected item and later success frames, clears the persisted thread before the
next queued turn, and sends `bridge.error` with the `degenerate_output:` prefix.
It does this for fresh and resumed threads and **does not replay the task**.
Earlier tool actions may already have happened; inspect the actual task state
before continuing. Quarantine forgets the resume pointer; it does not delete the
CLI rollout or undo side effects.

The pinned CLI emits assistant text as completed items, so this guard cannot
stop repetitive generation before such an item arrives. The existing turn
timeout remains unchanged. It also does not add voice interruption, request-ID
queue correlation, or provider-side output-token limits. Tests cover the observed
accented syllable variants, long valid output, child termination, fresh/resumed
quarantine, and a queued next turn starting fresh without replay.

## 3. Event translation (`translator.go`)

The bridge maps persistent Codex App Server JSON-RPC notifications (plus its
own `bridge.status` / `bridge.error` / `pong` frames) onto the same
`domain.WSEvent` shape the OpenClaw handler consumes:

| Inbound event | Emitted `domain.WSEvent` |
|---|---|
| `thread.started` | capture thread id as session key + `agent` lifecycle `phase:start` (once per turn) |
| `turn.started` | `agent` lifecycle `phase:start` (once per turn) |
| `item.started` `command_execution` / `mcp_tool_call` | `agent` tool `phase:start` (`shell` / `server.tool`) |
| `item.completed` `command_execution` / `mcp_tool_call` | tool `phase:end` (start emitted first when unseen) |
| `item.completed` `web_search` / `file_change` | tool `phase:start` + `phase:end` pair |
| `item.completed` `agent_message` | buffered as the reply; a newer one demotes the previous to `stream:thinking` (see *Preambles* below) |
| `item.*` `reasoning` / `todo_list` | *(ignored — status, not content)* |
| `turn.completed` | `agent` `stream:assistant` (whole reply as **one** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` with usage — ends the turn |
| `turn.failed` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — ends the turn |
| `bridge.status` / `pong` | *(logged / ignored)* |

Like PicoClaw, the accumulated `agent_message` text is surfaced at
`turn.completed` as a single assistant delta **before** `chat.final` /
`lifecycle.end` — the N=1 case of the streaming contract, which is what lets
the shared consumer flush TTS + `[HW:/…]` hardware markers at `lifecycle.end`.

**Preambles.** Codex can narrate before it calls a tool, as its own
`agent_message` item ("Using the sensing skill for this presence event.",
"Posture summary is present, so this is the posture-nudge route."). Joining
every `agent_message` would speak that whole trail — the leak seen on
`presence.enter` and `motion.activity` nudges. So only the **last**
`agent_message` of a turn is the reply: each earlier one is demoted to
`stream:thinking` (Flow Monitor only, never TTS or a channel reply) as soon as a
newer one proves it was not the reply. Exception: a non-final message carrying a
`[HW:/…]` marker is a real hardware action and stays in the reply. Prompt
wording cannot suppress preambles reliably — this is the enforcement point.

**Usage:** the App Server does NOT put usage on `turn/completed`. codex-rs
0.150.1 pushes it on its own notification, in camelCase, before the turn's
terminal event:

```
thread/tokenUsage/updated  {"threadId":…,"turnId":…,"tokenUsage":{
   "total":{…thread cumulative…},
   "last":{"totalTokens":…,"inputTokens":…,"cachedInputTokens":…,
           "cacheWriteInputTokens":…,"outputTokens":…,"reasoningOutputTokens":…}}}
```

gatewayd stashes the `last` block (`storeAppUsage`), normalizes camelCase to
the snake_case names the exec JSONL path uses, and attaches it to the
`turn.completed` frame the translator reads (`usageOf` also still accepts a
`usage` block on the notification itself or under `turn`, for other builds).
Until this landed the frame was bare and the Flow Monitor turn card showed **no
tokens at all**. `takeAppUsage` clears the stash, so a turn that reports nothing
never inherits the previous turn's numbers.

Codex speaks the OpenAI Responses API, whose `input_tokens` **already includes**
`cached_input_tokens`. The translator therefore SUBTRACTS — `input - cached →
InputTokens`, `cached → CacheReadTokens`, `cache_write → CacheWriteTokens`,
`output → OutputTokens`, `TotalTokens = fresh + cached + out` — the same
conversion `runtimes/hermes/translator.go` does for the chat/completions shape,
so the monitor renders `↓fresh R<cache> Σtotal`. Rotation keys on the RAW
`input_tokens` (`lastContextTokens`), which is the whole prompt and hence the
live context size; adding cached on top would halve the effective threshold.

> The three wires differ: `/responses` (codex) and `chat/completions` (hermes)
> fold the cached prefix INTO `input_tokens`; `/messages` (claudecode,
> openclaw) and opencode report it separately. `domain.TokenUsage` follows the
> Anthropic split, so only the first two subtract.

## 4. Session

Codex owns the session: the bridge captures the App Server thread id as its
session key. `NewSession` sends a `session.new` frame → the bridge clears the
current thread → the next `turn/start` is fresh. `turn/steer` appends only to
that thread's live turn, never to a completed or different thread.

Codex **auto-compacts its own context** (`model_auto_compact_token_limit`), but
the device has a **120k-token safety net**: at 134k a per-turn `codex exec`
already took 100 seconds, and later resumed turns grew to 376k and 473k.
It keys on the live **context** size — `input_tokens +
cached_input_tokens` from the last `turn.completed`, stashed by the translator
into `lastContextTokens` — and not on the `totalTokens` the shared handler
passes, which folds in this turn's output (turn volume, not context). Reading
its own usage frame keeps this codex-local: the other backends are untouched.

The cap intentionally leaves a small margin above the largest healthy sensing
turn observed (116k), while preventing the measured latency cliff. Per [`adding-agent-runtime.md`](adding-agent-runtime.md) §4
"No fake success", `CompactSession`, `GetConfigJSON` (Codex config is TOML +
`.env` secrets — no JSON file to expose), `UpdatePrimaryModel`, and
`RefreshModelsConfig` all return `domain.ErrNotSupportedByRuntime` — never
`nil`. This is not a dead end: `RefreshModelsConfig`'s caller falls back to
`EnsureOnboarding`, whose presync re-reads `llm_*` from config.json and the
hash gate restarts the gateway — so an llm change **is applied live**, just not
by that method.

## 5. Channels

### Telegram (device-owned receive loop)

Telegram is **device-owned** under Codex. The Codex CLI has no channel layer
of its own (unlike PicoClaw, whose runtime binary polls the Telegram Bot API
itself — its presync enables `channel_list.telegram` in PicoClaw's own
config), so os-server runs the inbound receive loop:
`runtimes/codex/telegram_poll.go`, one goroutine started from `StartWS`
(outside its reconnect loop, so it survives WS drops). Because it lives inside
the codex service's lifecycle it runs **only while codex is the active
runtime** — it can never compete with the openclaw/hermes gateway pollers for
`getUpdates` (Telegram 409s concurrent pollers).

The loop long-polls `getUpdates` (50 s window, client timeout 70 s) and reads
`config.TelegramBotToken` / `TelegramUserID` **fresh on every iteration** — no
restart is needed after saving or rotating creds; while the token is empty it
rechecks every 30 s, and HTTP/network errors back off 5 s. A message is
accepted only if it is a non-empty **text** message, in a **private** chat,
and `from.id` equals `TelegramUserID` (string compare after `strconv`);
everything else is skipped at debug level while the offset still advances (no
re-delivery). The next-update offset is persisted atomically
(temp + rename) to `/root/.codex/telegram_offset.json`, and each accepted chat
id is upserted into `/root/.codex/telegram_targets.json` so outbound
`Broadcast` (proactive sensing/guard alerts) reaches the chat.

Each accepted message waits for the agent to go idle (`IsBusy`, 500 ms poll,
ctx-aware), then is injected via `sendChat` with flow source `telegram`, so
`chat_input` / `chat_send` fire as usual and Flow Monitor shows the origin.
The injected turn text is prefixed with sender metadata — exact format
`[telegram] Message from <FirstName LastName> (@username) [id:<numeric>]:\n<text>`,
built by `tgUser.label()` (the `(@username)` part is omitted when absent, the
name falls back to `unknown`) — so the agent knows who is talking and on which
channel, mirroring openclaw's telegram plugin behavior.
The run is marked **silent** (the reply must not hit TTS) and tracked in
`telegramRuns`; at `turn.completed`, `emitFinal` consumes the tracker and DMs
the final text back to the originating chat with `[HW:/...]` hardware markers
and TTS audio tags (`[laugh]`, `[sigh]`, …) stripped (`stripForChannel` in
`hal.go`, mirroring the downstream `hwMarkerRe` and HAL's audio-tag
whitelist). On `turn.failed` the tracker is consumed without a DM so the map
cannot leak.

While the turn runs, a `telegramTypingKeeper` goroutine keeps Telegram's
"typing…" indicator alive: after the turn is injected it fires Bot API
`sendChatAction(typing)` immediately and then every 4 s (the indicator expires
after ~5 s) until the run is consumed — reply DMed by `emitFinal` or the turn
errored via `handleError` — capped at `telegramTypingLifetime` = 10 minutes so
a wedged turn cannot leave the chat "typing…" forever. Sends are best-effort
(errors logged at debug and ignored).

### Slack (HTTP-mode proxy path)

Slack is also **device-owned**, via the HTTP-mode proxy path (modeled on the
hermes bridge, `runtimes/hermes/slack.go`): the public bff-campaign-service
proxy receives Slack Events API deliveries and fans them out over MQTT to the
device's `slack_event` handler
(`server/device/delivery/mqtt/slack_event_handler.go`), which dedups by
`event_id` (in-memory LRU, 5 min TTL) and type-asserts the active gateway to
`domain.SlackBridge`. `CodexService` implements that bridge
(`runtimes/codex/slack.go`), so events route here **only while codex is the
active runtime** — no server-side dispatch code changes, same wiring as
hermes. Socket Mode is not involved; the device never opens a Slack WebSocket.

Event handling mirrors hermes: `url_verification` echoes the challenge; only
`message` / `app_mention` events from a real user pass (bot messages, subtyped
events and empty-user events are ignored — loop guard); a leading bot mention
is stripped; and when `config.SlackUserID` is set, only that user may drive
turns (empty = open, the workspace/app already scopes access). The reply goes
to the existing thread when the message was threaded, else it threads under
the user's message (`thread_ts` fallback = message `ts`).

An accepted message is injected asynchronously (goroutine): it waits for the
agent to go idle (`IsBusy`, 500 ms poll, capped at 2 min — past the cap the
message is dropped, since the MQTT handler already acked and Slack will not
retry), then goes through `sendChat` with flow source `slack` and the
sender-metadata prefix
`[slack] Message from <@U…> [channel:C…]:\n<text>`. The run is marked
**silent** (no TTS) and tracked in `slackRuns` (runID → channel/thread_ts/
message ts); receipt is acknowledged with an **eyes reaction** on the user's
message (best-effort). At `turn.completed`, `emitFinal` consumes the tracker
and posts the reply — `stripForChannel`-cleaned, like telegram — back to the
channel/thread via `chat.postMessage` (`config.SlackBotToken`), clearing the
eyes reaction; on `turn.failed` the tracker is consumed and only the reaction
is cleared (no reply). Unlike hermes there is **no progressive streaming**
(`chat.startStream`/`appendStream`) and no assistant "…is typing" status —
codex exec emits the reply whole, so `StreamSlackDelta` is a no-op and the
final text is posted once; `DeliverSlackReply` (called by the shared agent
handler) is a consume-if-present safety net that is normally a no-op because
`emitFinal` consumes the tracker synchronously before dispatching lifecycle
events.

Requirements (config.json): `slack_bot_token` (`chat.postMessage` +
reactions) and optionally `slack_user_id` (allowlist + proactive `Broadcast`
target for the `SlackSender` channel sender). The HTTP-mode
`slack_signing_secret` is consumed by the public proxy, not on the device —
codex, like the hermes bridge, trusts the authenticated MQTT path.

### Discord (device-owned gateway bot session)

Discord is also **device-owned**: it requires a Gateway WebSocket bot session
(there is no long-poll receive API), so os-server runs one via
[discordgo](https://github.com/bwmarrin/discordgo)
(`runtimes/codex/discord.go`). Like the telegram loop, the session is started
from `StartWS` (`go s.startDiscordBot(ctx)`) and lives inside the codex
service's lifecycle — it runs **only while codex is the active runtime**, so
it can never fight another runtime's bot session for the same token. The loop
reads `config.DiscordBotToken` **fresh on every connect attempt** (empty →
recheck every 30 s; failed open → back off 15 s); once open, discordgo
handles gateway reconnects itself and the session is closed when the gateway
ctx ends. Intents: direct messages + guild messages + message content.

A message is accepted only if it is not from a bot (loop guard, covers the
bot's own messages), the sender id equals `config.DiscordUserID` (**the
allowlist is required** — empty rejects everyone, since anyone can share a
guild with the bot), and it is either a **DM** or a message in
`config.DiscordGuildID` that **@mentions the bot** (mirrors common openclaw
plugin behavior: guild requires @mention, DM does not — the mention itself is
stripped from the turn text). Everything else is skipped at debug level.

Each accepted message waits for the agent to go idle (`IsBusy`, 500 ms poll,
ctx-aware), then is injected via `sendChat` with flow source `discord` and
the sender-metadata prefix
`[discord] Message from <Username> [id:<id>]:\n<text>`. The run is marked
**silent** (no TTS) and tracked in `discordRuns` (runID → channel id); while
the turn runs, a `discordTypingKeeper` goroutine keeps Discord's **native
typing indicator** alive (`ChannelTyping` immediately and every 8 s — the
indicator lasts ~10 s — capped at 10 minutes). At `turn.completed`,
`emitFinal` consumes the tracker and posts the reply —
`stripForChannel`-cleaned, like telegram — back to the channel via
`ChannelMessageSend`, **chunked at Discord's hard 2000-character message
limit** (splitting on newline boundaries when possible); on `turn.failed` the
tracker is consumed without a reply so the map cannot leak. The reply sender
uses a mutex-guarded session handle on the service (nil session → log +
drop).

Requirements (config.json): `discord_bot_token`, `discord_user_id`
(allowlist), and `discord_guild_id` when guild mentions should work (DM-only
setups can leave it empty).

### Channel API

`SupportedChannels()` returns `["telegram", "slack", "discord"]`.
`AddChannel` / `RefreshChannelConfig` for all three are honest no-op
successes — every consumer reads the creds fresh from config.json on each use
(the telegram loop per iteration, the Slack bridge per event / Web API call,
the discord bot per connect attempt / message), so there is nothing
agent-side to write and no restart needed. `AddChannel(discord)` additionally
validates that `discord_bot_token` and `discord_user_id` are present (the
receive path cannot work without them — accepting would be fake success).
Whatsapp has no receive path and returns `domain.ErrChannelNotSupported`. See
also [`adding-agent-runtime.md`](adding-agent-runtime.md).

### Telegram remote coding-sessions (`telegram_coding.go`, `coding_sessions.go`)

Mirrors `runtimes/claudecode/telegram_coding.go` 1:1 — a Telegram chat can
**attach to a folder's interactive `codex` thread and continue coding it from
the phone**, across multiple folders each with its own thread. Separate from the
device-main persona turn.

- **Discovery** (`coding_sessions.go`): codex stores each thread as a "rollout"
  JSONL under `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
  (`/root/.codex/sessions`). `allCodingSessions` walks that tree; each rollout's
  **thread id (`payload.id`) and cwd (`payload.cwd`) come from its first
  `session_meta` record**; the listing shows the **3 most recent user prompts**
  (most-recent first, synthetic `<environment_context>` blocks skipped). Entries
  are deduped by thread id (newest rollout wins). Unlike claude, codex resume is
  **thread-id-global**: `codex exec --cd <dir> resume <id>` sets the cwd
  independently and does not require it to match the original (device-verified —
  resuming an old thread echoes its id back; a 404 in that test was the separate
  campaign-api `/responses` endpoint gap, not the resume mechanism).
- **Commands** (intercepted in `handleTelegramUpdate` before the device-main
  injection): `/resume` (mirrors the codex CLI — no arg lists folders, `/resume
  <n>` picks by number, `/resume <folder>` picks the newest) · aliases
  `/sessions` (list) + `/use <n|folder>` (pick) · `/sessions <folder>` (all
  threads in one folder) · `/new <folder>` · `/here` · `/device` · `/help`. A
  chat with no selection and no command falls through to device-main unchanged.
- **Hand-off model.** Each accepted turn spawns a fresh `codex exec --json
  --dangerously-bypass-approvals-and-sandbox --cd <folder> [resume <thread>]
  <prompt>` (flag order matters — `--cd` is rejected after `resume`, so it rides
  before). The reply is the accumulated `agent_message` items parsed from the
  JSONL (`parseCodexResult`: `thread.started` → id, `item.completed`/
  `agent_message` → text, `turn.completed` → done), DMed chunked at Telegram's
  4000-char limit. The exec env **mirrors the gatewayd's `turnEnv`** (process env
  + presync `.env` pairs + `HOME=/root` + `CODEX_HOME=/root/.codex`), so codex
  resolves the same `config.toml` + auth — working in **both** auth modes
  (`OPENAI_API_KEY` via `/responses`, or the ChatGPT-subscription `auth.json`)
  with no runner change.
- **Guards.** A per-folder mutex serializes turns; a `/proc` scan
  (`procHoldsFolder`) refuses a turn while an interactive `codex` TUI still holds
  the folder (two writers would corrupt the rollout). Only the allowlisted
  `telegram_user_id` reaches any of this; remote coding runs
  `--dangerously-bypass-approvals-and-sandbox`, so the allowlist is the security
  boundary.
- **State.** Per-chat selection persists to `/root/.codex/telegram_coding.json`
  (survives restart). Runs in os-server directly (its own `codex exec` per turn),
  independent of the persistent gatewayd child.

## 6. Hooks

Codex ships no hooks loader, so OpenClaw's `emotion-acknowledge` hook is
reproduced **natively in Go** (`runtimes/codex/emotion_ack.go`, mirroring
hermes' `emotion_ack.go`): on each user-visible turn, sendChat fires
`{emotion:"thinking"}` to HAL — same skip prefixes, same intensity, same
capability gate (`skills.SupportedHooks`) as the TS handler. The companion
`turn-gate` hook is intentionally not mirrored (sendChat already marks the turn
busy). ⚠️ Keep it in lockstep with `runtimes/openclaw/hooks/emotion-acknowledge/handler.ts`.

## 7. MCP connectors (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (the `connector.set` MQTT flow) edit
`/root/.codex/config.toml` `[mcp_servers.<name>]` via **go-toml/v2**, atomically
(temp + rename) under `mcpMu`, then restart the gateway so the next `codex
exec` picks the server up. Shape translation from the canonical
OpenClaw-shaped entry: **http entries map `headers` → `http_headers` and the
`type` key is dropped** (Codex infers the transport from `url` vs `command`);
stdio entries pass through. `RemoveMCPEntry` is idempotent (`removed=false`, no
restart, when absent). presync only regenerates the config **head** and
preserves the `[mcp_servers` tail (§1.2), so the entries survive every sync.

## 8. Factory reset (`reset.go`)

`ResetAgent` (called by `server/system/factoryreset.go` on the active gateway)
preserves nothing — config.toml/.env are regenerated by presync on the next
switch: **stop** `codex.service` (+ verify inactive, 5s poll), **disable** it
(reboot defaults to openclaw), **wipe `/root/.codex` wholesale** — config, CLI
auth, threads (`sessions/`), workspace, and the `.openclaw-migrated` marker (so
presync §1 re-migrates on the next switch) — then recreate the baseline
`workspace/` + `attachments/` dirs (Codex has no onboard subcommand; the CLI
recreates its own state under `CODEX_HOME` on first run).

## 9. Auth — phase 2 note

Phase 1 (current) authenticates with an **API key via campaign-api**:
`OPENAI_API_KEY` = config.json `llm_api_key`, provider `base_url` =
`llm_base_url` (§1.2). ChatGPT-subscription auth (`codex login --device-auth`)
is **deferred to phase 2** — it will share the login-pairing plumbing with the
claudecode branch's `ClaudeLoginPairer` once that branch merges.

### Subscription auth (manual)

Available today without the phase-2 pairing flow: run
`codex login --device-auth` on the device, or copy an existing
`~/.codex/auth.json` from another machine to `/root/.codex/auth.json`
(`chmod 600`). Presync auto-detects `auth.json` on every run (so on every
boot): it omits the custom provider block from config.toml and drops
`OPENAI_API_KEY` from `.env`, so codex talks to OpenAI directly with its
built-in default provider + model — this **bypasses the campaign-api
`/responses` 404 blocker** entirely. Delete `auth.json` to fall back to
api-key mode; the flip is automatic on the next presync run.

### Active-turn correlation and steering

`message.send` carries the request `id` and originating device `run_id`.
Gatewayd preserves those IDs on turn events. A compatible message targeting the
active thread uses `turn/steer` instead of waiting behind it. Its independent
device trace receives a `bridge.steered` acknowledgement and a `turn_merged`
flow event containing `parent_run_id`. Merged voice and web-chat requests stay
active until the shared final reply or error, including disconnect and timeout.
The latest audible follow-up owns speech under its original run ID and Harness
response route; the host and earlier voice inputs suppress duplicate TTS.
Hardware markers execute once on the host and are stripped from child replies.
Merged lifecycle events do not replace or clear the host session tracking. This
prevents a merged follow-up from retaining a phantom pending run and wedging
busy state. Control frames (`pong`, `bridge.status`) remain independent.
The realtime `voice_agent_handled` history sync also steers when Codex is
active: the voice layer already spoke, so its silent trace closes on the
acknowledgement instead of queueing behind the active task.
Steered direct user requests retain their complete input and response address,
with routing context prioritizing them over passive sensing work and directing
computer-agent tasks through `harness-use`. This context is not added to silent
history synchronization; it is guidance, not proof of a Harness delivery.
If a gateway restart leaves a persisted thread ID that the new App Server no
longer has, gatewayd clears that stale session and retries the same turn once on
a fresh thread.

The adapter retains request/run correlation for terminal events and rejects a
steer that cannot be applied to the active thread rather than silently attaching
it elsewhere. A completed turn cannot be steered: its next message follows the
normal start/resume path. OS-level queues remain for safety and delivery
reasons, including passive sensing and speaker-busy handling, so steering is
not a general cancellation or interruption mechanism.

If a live bridge connection drops while a turn is active, the client emits a
correlated lifecycle error (`codex gateway restarted; turn cancelled`) before
clearing its local correlation. This prevents the monitor and web chat from
showing a permanently active turn; it deliberately does not replay the task,
because it may already have side effects.


## Jev skill preloading in the managed bridge

The Codex runtime bridge selects at most one eligible skill with Jev before forwarding a text-only user request. It reads the OS-installed native skill root, `$CODEX_HOME/skills`, and adds the complete selected skill and its directory to that request. This is a runtime adapter, not the hardware intent router and not a plugin installed into a standalone upstream CLI. OS-managed voice, Web/MQTT chat and ordinary messaging-channel conversations share this bridge. Standalone CLI sessions and Telegram coding-session mode run outside it and keep their normal native skill discovery.

Selection uses the shared `system/lib/jevskills` implementation with a three-second budget and fails open: abstention, missing credentials, invalid responses and load errors preserve the original request. System messages, slash commands and attachments skip preloading. For CLI execution, if the complete preload plus request would reach Linux’s 128 KiB single-argument limit, the bridge drops the preload, logs `reason=argument_budget`, and sends the original request; it never truncates a skill. Each prepared request reuses its context on a missing-session retry; no context is written to AGENTS.md or a global system prompt. The upstream runtime may retain the supplied turn in normal conversation history; the preload explicitly applies to the current request only.

**Jev is disabled by default** for this runtime pending native validation. The build switch is `const jevEnabled = false` in `runtimes/codex/gatewayd/jev.go`. Enabling it after validation requires changing the Go switch, rebuilding and restarting through runtime management; environment variables cannot enable Jev. `JEV_CONFIG_PATH` defaults to `/root/config/config.json`; credentials are read from that file, never embedded in the preload. Skills with unsupported execution metadata are excluded. Malformed native settings, unsupported JSONC syntax, alternate config-root environment overrides and project skill catalogs also abstain. Ancestor project settings are checked to preserve inherited policies and skill precedence. If native settings contain custom skill or permission controls, the adapter conservatively leaves selection to the native loader rather than bypassing its policy. It does not grant permission to execute a selected skill or change existing tool approvals.

Local verification covers request isolation, non-user and attachment bypass, native-policy abstention and rendered child input. Mock selectors and fake runtime processes are used; live Jev, native model behavior and on-device delivery require separate integration testing.
