# PicoClaw agent backend

PicoClaw is one of the **swappable agentic backends** the os-server can run
behind its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. This doc. Code: `runtimes/picoclaw/`.

> Source of truth is the code. This documents `runtimes/picoclaw/` as
> implemented; keep it in sync on change (EN: this file, VI: `docs/vi/agentic/picoclaw_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) (Hermes) ·
> this file (PicoClaw).
>
> **Status: install parity; client-only gateway.** PicoClaw now ships a device-side
> installer + pre-start hook (`runtimes/picoclaw/install.sh` + `presync.sh`, embedded
> and registered via `install.go` → `runtimereg`), so a `picoclaw.setup` switch
> installs, provisions, and starts it like hermes (§1.1). Persona/memory migration is
> two-way through the Go reconciler — picoclaw has a `migrate_persona` adapter
> (`runtime_picoclaw.go`), so switching to/from it carries SOUL/IDENTITY/MEMORY/USER/
> KNOWLEDGE both directions; **skills** import on the way IN is done by `picoclaw
> migrate --workspace-only --force` in the presync hook (§1.1). The Go
> gateway itself stays **client-only**: most in-process lifecycle methods
> (`SetupAgent`, `RefreshModelsConfig` …) remain no-ops (§8) because provisioning
> happens out-of-process in install.sh/presync. The exceptions are `EnsureOnboarding`
> (`onboarding.go`, keeps the OS-managed blocks in SOUL/AGENTS/HEARTBEAT current),
> `StartSkillWatcher` (`skill_watcher.go`, CDN skill auto-update), identity
> (`identity.go`: `WatchIdentity`/`UpdateIdentityName` read/write `IDENTITY.md` like
> OpenClaw), `ResetAgent` (`reset.go`, factory-reset wipe of `/root/.picoclaw` +
> re-onboard), and emotion-acknowledge (`emotion_ack.go`: channel turns fire a
> "thinking" face on the observer hook's `agent:start`, capability-gated on
> `expression` like OpenClaw/Hermes) — all real (§1.1, §8).
> Remaining gaps (queue/steer pinning) are tracked
> against the
> [`adding-agent-runtime.md`](adding-agent-runtime.md) checklist — consult it before
> raising PicoClaw to full parity.

## 1. When and how it is selected

`agent_runtime` in `config.json` picks the backend; resolution lives in
`system/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| `"openclaw"` / unset | OpenClaw (default; or `gateway.default` from `ROBOT.md`) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) |
| anything else | OpenClaw (logged as `FALLBACK — unknown runtime=…`) |

On startup `ProvideGateway` prints an `AGENT BACKEND ACTIVE → PICOCLAW` banner
with `ws_url`, `conversation`, and `source`.

## 1.1 Install + provisioning (`install.sh` + `presync.sh`)

A `picoclaw.setup` switch runs the generic `system/device/switch_runtime.sh`,
which materializes PicoClaw's embedded scripts and drives them. The two scripts
live next to the backend and are embedded + registered in `install.go`:

| Script | On-disk path | Runs |
|---|---|---|
| `install.sh` | `/usr/local/lib/os-runtimes/picoclaw/install.sh` | first switch / failed `verify` |
| `presync.sh` | `/usr/local/bin/runtime-picoclaw-presync` | **before every** picoclaw start (and once at end of install) |

**`install.sh`** (one-time):
1. installs `jq` + `yq` + the pinned `picoclaw` binary (GitHub release,
   `picoclaw-linux-arm64`) into `/usr/local/bin`;

   > The pin is the image baseline only. Field updates go via
   > `make upload-picoclaw <release-tag>` + `make promote-picoclaw`, which the
   > bootstrap worker applies as `software-update picoclaw` on devices whose
   > `agent_runtime` is `picoclaw`. That command also stamps the installed tag to
   > `/usr/local/lib/os-runtimes/picoclaw/installed-version` — `picoclaw version`
   > prints a build description with no semver, so the stamp is the ONLY way the
   > worker can tell which release is installed. See `docs/bootstrap-ota.md` §5.
2. `picoclaw onboard` (only when `config.json` is absent) creates `/root/.picoclaw`
   — workspace + a baseline `config.json` and `.security.yml`;
3. writes **`picoclaw.service`** (`ExecStart=/usr/local/bin/picoclaw gateway`,
   `HOME=/root`, `Restart=always`) — `picoclaw gateway` only runs in the foreground,
   so unlike hermes (which ships `gateway install --system`) we wrap it ourselves.
   The unit name equals the runtime name, so **no** `os-runtimes/picoclaw/service`
   declaration file is needed (switch-runtime defaults to it);
4. runs the presync hook once, then drops a `verify` hook (`command -v picoclaw`) so
   switch-runtime can detect + self-heal an orphaned unit.

**`presync.sh`** (every switch — single owner of model + channel config, so it
self-heals after a factory reset, mirroring hermes' presync):
- **§0 migrate** — gated on a sentinel marker `~/.picoclaw/.openclaw-migrated`
  (**not** on `workspace/skills` emptiness — PicoClaw ships built-in skills so that
  dir is always non-empty). When the marker is absent and `/root/.openclaw` exists,
  stop openclaw and run `picoclaw migrate --workspace-only --force` to carry
  persona/memory/skills over from OpenClaw. **`--workspace-only`** means migrate does
  NOT touch `config.json` — converting `openclaw.json` into a picoclaw config produces
  a broken config, so `config.json` stays the valid onboard baseline and §1/§2 assert
  model/channel/gateway on top. It then does the file fixups migrate doesn't: copy
  `HEARTBEAT.md` + `KNOWLEDGE.md` from the
  openclaw workspace (KNOWLEDGE.md is openclaw's living learnings doc, seeded from an
  embedded template then appended daily — migrate skips it), delete `AGENT.md` (so
  PicoClaw runs the legacy `AGENTS.md` path — the only mode that reads `IDENTITY.md`),
  and copy openclaw's `IDENTITY.md` over (migrate skips it too). Finally it writes the
  marker. A factory reset wiping `/root/.picoclaw` clears
  the marker so migrate re-runs; a failed migrate leaves the marker unwritten and
  retries next switch.
- **§0.5 onboarding (`onboarding.go`)** — `EnsureOnboarding`, called on
  boot/config-change like openclaw/hermes, mirrors openclaw's reconcile (trimmed):
  - seeds `KNOWLEDGE.md` from an embedded template (`resources/KNOWLEDGE.md`) **only
    if absent** — covers the fresh picoclaw-only device where presync §0 had no
    openclaw copy; never overwrites;
  - injects the OS-managed `<!-- OS DO NOT REMOVE -->` blocks into `SOUL.md`
    (`ensureSoulMDBlock`, per-device-type soul from ROBOT.md `soul_ref`; owner
    content below `---` preserved), `AGENTS.md` (`ensureAgentsMDBlock`,
    skills/connectors/memory/priority rules — the **Connectors (MANDATORY)** block
    routes any Gmail/Calendar/Drive/… request through the `connectors` skill
    (credentials on disk at `/root/.openclaw/workspace/configs/<code>_access_tokens.json`)
    so the agent never installs its own mail client), and `HEARTBEAT.md` (`ensureHeartbeatMDBlock`,
    daily knowledge-synthesis) — mirroring openclaw but stripped of OpenClaw-only
    content, so the blocks stay current on a plain os-server OTA;
  - **capability-gates skills** (`pruneUnsupportedSkills`): removes skill dirs the
    device can't use — a skill survives if it is supported by `skills.Supported(caps)`
    (the same gate openclaw uses) **or** is a picoclaw built-in
    (`picoclawBuiltinSkills`: `agent-browser`, `github`, `hardware`, `skill-creator`,
    `summarize`, `tmux`, `weather`); everything else under `workspace/skills` is
    deleted. Fail-open when ROBOT.md declares no caps. No reload (skills read per-turn);
    then refreshes every supported skill from the CDN, so a local skill that was
    stale before the watcher started self-heals on boot/config reconciliation. If
    any skill content changed, it notifies the agent after a possible gateway restart
    to re-read the updated `SKILL.md` files;
  - when any block changed, **restarts the gateway** (`restartPicoclawGateway` →
    `systemctl restart picoclaw`) so it re-reads the workspace files (log+skip when
    systemctl is unavailable). Not the gateway `/reload` endpoint — it needs an admin
    auth we don't hold (the pico channel token is rejected) and isn't confirmed to
    re-read workspace markdown; a restart reliably does.
  - openclaw.json-specific steps (hooks/logging/controlUi registration) are N/A for
    picoclaw's `config.json`; queue/steer pinning is TODO.

A separate **skill watcher** (`skill_watcher.go`, started at boot like openclaw)
polls OTA metadata every 5 min and auto-updates `workspace/skills/<name>` from the
CDN when a supported skill's version bumps (capability-gated via
`skills.Supported`), then notifies the agent with `SendSystemChatMessage`. Each
successful poll logs `skill watcher: checked`; if a ZIP download or extraction fails,
the watcher leaves that skill's version pending and retries it on the next poll.
- **§1 structure** (`jq` on `config.json`) — `agents.defaults` (provider
  `anthropic-messages`, `model_name "autonomous"`, `image_model "autonomous_vision"`,
  `restrict_to_workspace:false`, `allow_read_outside_workspace:true`), the `autonomous`
  and `autonomous_vision` (`qwen/qwen3.6-plus`, same campaign-api endpoint) `model_list`
  entries, and the `channel_list` skeleton. `channel_list.pico` is always enabled.
  The `autonomous` entry's `model` is the `Auto-AI` alias, which only the
  campaign-api proxy resolves — §2 replaces it with the operator's `llm_model`
  whenever `llm_base_url` points somewhere else, or the host rejects every turn
  with an unknown-model error. The vision entry keeps its own model: that is a
  separate choice, not this one.
- **§2 dynamic** (secrets from the **project** `/root/config/config.json`, which
  wins) — `model_list[autonomous,autonomous_vision].api_base` from `llm_base_url`
  (PicoClaw needs a trailing `/v1`, unlike hermes), `.security.yml`
  `model_list."autonomous:0".api_keys` + `"autonomous_vision:0".api_keys` from
  `llm_api_key`, the `pico` bearer token (must equal `constants.go` `Token`),
  and each non-pico channel **enabled only when its credentials exist**: telegram
  (`telegram_bot_token` + `telegram_user_id`), discord (`discord_bot_token` +
  `discord_user_id`), slack (`slack_bot_token` + `slack_app_token` + `slack_user_id`),
  whatsapp native (`whatsapp_user_id` → `allow_from`, no token, QR pairing on first
  run). Secrets land in `.security.yml` under `channel_list.<ch>.settings`; structure
  stays in `config.json`.

The running gateway logs confirm the wiring on boot (`Gateway started on
127.0.0.1:18790`, health at `/health` `/ready` `/reload`, `Channels enabled:
[pico]`). A `SECURITY: Channel allows EVERYONE (allow_from is empty) channel=pico`
warning is expected: `pico` is the device-local native gateway and intentionally
has no `allow_from`.

## 2. Wire constants

There is **no per-unit config**; the endpoint is a compile-time constant in
`runtimes/picoclaw/constants.go`:

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18790/pico/ws/` | Local PicoClaw WebSocket endpoint |
| `Token` | `darren_pico_token` | Bearer token sent in the `Authorization` header on connect |
| `Conversation` | `device-main` | Default session label until the server assigns a `session_id` |

## 3. Transport

`client.go` holds **one persistent WebSocket** (gorilla/websocket), mirroring the
openclaw reconnect loop but simplified — PicoClaw has **no challenge / pairing
handshake**, just a bearer token:

1. `StartWS` dials `WSURL` with `Authorization: Bearer <Token>`.
2. On connect, readiness flips true (`IsReady`/`ConnectedAt`), the `StateAgentDown`
   LED clears, and a reconnect (not first-connect) plays the i18n reconnect TTS.
3. A keepalive goroutine sends `{"type":"ping","id":…}` every 25s; PicoClaw replies
   `pong` (ignored) which refreshes the 90s read deadline.
4. The read loop translates each inbound frame and dispatches into the registered
   `domain.AgentEventHandler` (synchronously — safe because `FetchChatHistory` is a
   no-op here, so the handler never blocks on a WS RPC).
5. On drop: clear busy + in-flight turn ids, paint `StateAgentDown`, stop servo
   tracking (motion devices only), back off 5s, reconnect.

## 4. Sending a turn

`chat.go` `sendChat` admits one turn at a time. It writes immediately when idle,
or retains the request locally while busy and returns its run ID. Replies arrive
on the read loop:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>" }, "session_id": "<if known>" }
```

- Image turns add `payload.attachments: [{ "type": "image", "url": "data:image/jpeg;base64,…" }]` (best-effort; the text content is always sent so the turn proceeds even if the attachment shape is ignored).
- Before the write: mark busy, stash the `runID` as the **pending run id**, record a pending chat trace, and emit `chat_input` / `chat_send` flow events (parity with openclaw).

PicoClaw processes **one turn at a time** and does not stream tokens, so turns
are correlated by a single in-flight `runID` rather than a per-frame id: the
pending run id is adopted by the first inbound frame of the turn.

The adapter serializes both direct chat and buffered events locally; a second
request cannot overwrite the first request's pending run ID. Queued direct chat
preserves its text, images, request ID, run ID, and source. `chat.final` and
`lifecycle.end` can both request idle, but admission stays reserved until the
complete terminal sequence has been dispatched, then exactly one next request
is sent. Errors release the same reservation after dispatch.

Unsent queue entries survive offline idle/speaker callbacks and drain as soon as
a connection is ready, still honoring speaker gating, voice priority, and passive
event expiry/coalescing. If the socket disappears before any write, the request
stays queued. A write failure closes the socket and is treated as uncertain:
that request is never automatically replayed; only its untouched queue tail
survives reconnect. The 45-minute busy expiry also retires the socket before any
next request can start, so a late response cannot be attached to that request.

This protocol has no response request IDs or replay acknowledgements. Local
serialization cannot disambiguate unsolicited/interleaved external turns or a
server that replays old responses on a new connection. Codex CLI repetition
filtering and request-ID matching are not copied into this adapter. These paths
are covered by local WebSocket tests; live PicoClaw/device behavior still needs
runtime validation.


## 5. Inbound protocol → `domain.WSEvent` mapping

This is the critical part for correct Flow Monitor / web-chat rendering. The
frame `type` alone is **not** enough — `message.create` / `message.update` must
be classified by their payload (`placeholder` / `kind` / `tool_calls` / `content`),
in this priority order (`translator.go` `categorize`):

| Inbound frame | Classified as | Emitted `domain.WSEvent` |
|---|---|---|
| `typing.start` | turn start | `agent` lifecycle `phase:start` (once per turn) |
| `message.create/update`, `placeholder:true` | thinking | *(none — status, not content)* |
| `message.create/update`, `kind:"thought"` / `thought:true` | reasoning | *(none — rendered as status only)* |
| `message.create`, `kind:"tool_calls"` / has `tool_calls` | tool call | `agent` tool `phase:start` + `phase:end` per call |
| `message.create/update`, non-empty `content` (none of the above) | **final answer** | `agent` `stream:assistant` (whole reply as one delta) **+** `chat` `state:final role:assistant` **+** `agent` lifecycle `phase:end` (with usage) — **ends the turn** |
| `error` | error | `agent` lifecycle `phase:error` — ends the turn |
| `typing.stop` / `message.delete` / `pong` | — | *(ignored)* |

### Turn lifecycle gotchas

- **`typing.stop` is NOT the end of the turn.** It arrives early, right after the
  thinking phase. The turn ends only on the first **final** frame (or `error`).
- **No-tool turn:** `typing.start → placeholder → typing.stop → message.update (final)`.
  The final is a `message.update` that reuses the placeholder's `message_id`.
- **Tool turn:** `placeholder → typing.stop → message.delete (placeholder removed)
  → message.create kind:"tool_calls" (×N) → message.create (clean, final)`.
- PicoClaw does not emit a separate tool-result frame, so each tool call emits a
  `tool` `phase:start` immediately followed by a `phase:end` with an empty result,
  purely to close the trace.
- **Non-streaming → one assistant delta.** PicoClaw delivers the whole reply in a
  single final frame, but the shared consumer derives TTS + `[HW:/…]` hardware
  markers (and the Flow Monitor `tts_speak` / `hw_*` nodes) from the **assistant-delta
  stream**, flushed at `lifecycle.end`. So the final answer is surfaced as `agent`
  `stream:assistant` — the whole reply (markers intact) as **one** delta — **before**
  `chat.final` / `lifecycle.end`, the N=1 case of the openclaw/hermes streaming
  contract. Without it the reply renders in web chat but never reaches the speaker or
  hardware. `translator.go` `emitFinal`.
- `media.create` is defined in the protocol but the server never emits it — media
  rides inside `message.create` as `attachments`.

### Tool call shape

Each entry in `tool_calls` is OpenAI-style: name + params live in
`function.name` and `function.arguments` (a **JSON string**, not an object). The
agent's human-readable lead-in is in `extra_content.tool_feedback_explanation`
(may contain stray ANSI control chars from terminal input). The current
translator forwards `name` + `arguments`; the explanation is logged but not
surfaced (the device `AgentPayload` has no slot for it).

### Token usage

`context_usage` (only on the final frame) is cumulative context size, not
per-turn input/output. It maps to `TokenUsage{ InputTokens: history_tokens,
TotalTokens: used_tokens }`.

## 6. Session

PicoClaw owns the session: the server-assigned `session_id` is captured from any
inbound frame and stored (`SetSessionKey`) so the next `message.send` echoes it.
`NewSession` just clears the local id so the next turn starts a fresh server
session. There is no compact RPC, so `CompactSession` returns
`domain.ErrNotSupportedByRuntime` (the caller logs and rotates via `NewSession`
instead).

## 7. Channel capability

PicoClaw runs **telegram only**. The Telegram receive loop is **device-owned**
(driven by `config.TelegramBotToken`), and PicoClaw has no slack/discord delivery
of its own. The three channel methods in `runtimes/picoclaw/channels.go` encode
this honestly:

| Method | telegram | slack / discord / whatsapp |
|---|---|---|
| `SupportedChannels()` | returns `[telegram]` (the only entry) | — |
| `AddChannel(…)` | honest **no-op success** — telegram is device-owned, so there is nothing to write into the runtime | returns `domain.ErrChannelNotSupported` |
| `RefreshChannelConfig(…)` | `("", nil)` — success no-op (no runtime re-apply needed) | returns `domain.ErrChannelNotSupported` |

This is part of a **repo-wide generic capability model**: every runtime declares
`SupportedChannels()` and returns `domain.ErrChannelNotSupported` (the string
`"channel_not_supported"`) for channels it cannot run, instead of the old silent
no-op. The shared not-supported behavior and the post-switch `ChannelReconcile`
are documented in [`adding-agent-runtime.md`](adding-agent-runtime.md) — see there
rather than duplicating here.

**Switching FROM openclaw → picoclaw:** if openclaw had slack/discord configured,
those channels become unsupported under PicoClaw. After the switch
`ChannelReconcile` reports them in the MQTT info uplink's `unsupported_channels`
field (`domain.MQTTInfoResponse`), and their creds **stay in `config.json`** —
switching back to openclaw restores them.

## 7.1 Channel turns in Flow Monitor (observer hook)

A Telegram turn is handled entirely inside the PicoClaw gateway (it owns the
channel I/O) and never crosses the WebSocket, so — exactly like Hermes — os-server
would be blind to it. Parity is restored with an **observer hook**, the direct
analogue of the Hermes `os-server-observer` hook: os-server ships a small
subprocess that the gateway runs on its shared agent pipeline, and that subprocess
forwards each turn to the loopback `POST /api/agent/channel-turn` endpoint the
shared `ChannelTurn` handler already serves (`handler_channel_turn.go`). No
consumer/UI change — the inbound user text lights up the IN node, and any
`[HW:/…]` markers in the reply drive the local hardware, same as Hermes.

Two things differ from Hermes and are owned by `runtimes/picoclaw/hooks.go`:

1. **Transport.** PicoClaw process hooks are a **subprocess speaking NDJSON
   JSON-RPC over stdio** (`resources/hooks/os-server-observer/observer.py`), not an
   in-process Python `handle()` discovered by directory scan. PicoClaw sends the
   subprocess (verified on-device, picoclaw 0.2.9):
   - `hook.hello` — a REQUEST (has JSON-RPC `id`); the script replies
     `{"action":"continue"}`. Any request (has `id`) is answered immediately so the
     turn is never blocked.
   - `hook.runtime_event` — a NOTIFICATION whose `params` is the event envelope
     `{kind, scope{channel,chat_id,sender_id,session_key,turn_id}, payload}`. The
     script acts on two kinds and ignores the rest (`agent.tool.*`, `agent.llm.*`):
     - `agent.turn.start` → `agent:start`, message = **`payload.UserMessage`**.
     - `agent.turn.end`   → `agent:end`, response = **`payload.FinalContent`** (the
       reply, WITH any `[HW:/…]` markers — `agent.turn.end` carries both the user
       message and the final reply, so **observe alone suffices; no intercept**).
   - forwards **every** channel by default (channel-agnostic, like the Hermes hook —
     `OBSERVER_CHANNELS` is an optional allow-list, empty = all) and drops internal
     senders (`sender_id == heartbeat`). The device-local `pico` turns os-server
     already logs via `sendChat`/`session.message` are excluded downstream by
     `skipPlatform` (`channelHookSkipPlatforms` now includes `pico`, alongside
     `api_server`/`cli`) — so forwarding-all cannot double-count or double-fire the
     ack. This is the exact analogue of how Hermes excludes its own `api_server` turns.
   - maps `scope` → the `ChannelTurn` payload (`platform=channel`, `chat_id`,
     `sender_id`→`user_id`, `session_key`→`session_id`). PicoClaw pairs the two
     forwards into one Flow turn by `session_key`.
   - the POST fires on a daemon thread (audit log stays synchronous) so a slow
     os-server never stalls the next event (`observer_timeout_ms` is 500ms).
   - `OBSERVER_DEBUG=1` dumps every raw stdin line to stderr (surfaced in the
     gateway log as `Process hook stderr hook=os-server-observer`) — used to verify
     the field names above.

2. **Registration.** PicoClaw does **not** scan a hooks dir; a hook is registered
   by a `config.json` entry under `hooks.processes.<name>`, gated by BOTH the global
   `hooks.enabled` AND the **per-process `enabled`** (both default false — same shape
   as the `tools.mcp` global + per-server gate; without the per-process `enabled`
   PicoClaw silently never spawns the subprocess). So `ensureObserverHook()` (called
   from `EnsureOnboarding`) does two writes: materializes `observer.py` to
   `/root/.picoclaw/hooks/os-server-observer/` with the `__OS_SERVER_TURN_URL__`
   placeholder substituted, and upserts:

   ```json
   "hooks": { "enabled": true, "processes": { "os-server-observer": {
     "enabled": true,
     "transport": "stdio",
     "command": ["python3", "/root/.picoclaw/hooks/os-server-observer/observer.py"],
     "env": { "OS_SERVER_TURN_URL": "http://127.0.0.1:<HttpPort>/api/agent/channel-turn", "OBSERVER_DEBUG": "1" },
     "observe": ["turn_start", "turn_end"]
   } } }
   ```

   It is idempotent (script diff + config-marshal diff) and returns `changed`;
   `EnsureOnboarding` restarts the gateway only when something changed, since the
   gateway loads hooks only at start. The config write is serialized under the same
   `mcpMu` that guards `config.json` (see `mcp.go`).

## 8. What is stubbed

Everything not on the PicoClaw hot path is a no-op so the single
`domain.AgentGateway` interface is satisfied without inventing features the
backend does not have: `SetupAgent`, WhatsApp pairing,
`RefreshModelsConfig`, `FetchChatHistory`, `CompactSession`,
the model watchers (`StartModelSync`/`StartPrimaryModelWatch`), `UpdatePrimaryModel`.
The error-returning stubs (`RefreshModelsConfig`, `UpdatePrimaryModel`,
`CompactSession`) return `domain.ErrNotSupportedByRuntime` — never `nil` — so
callers can tell "nothing to apply" from "applied" (see
[`adding-agent-runtime.md`](adding-agent-runtime.md) §4 "No fake success").
(`AddChannel` / `RefreshChannelConfig` are NOT stubs — they return
`domain.ErrChannelNotSupported` for unsupported channels, see §7; `EnsureOnboarding`
(§1.1) and `StartSkillWatcher` (skill auto-update, §1.1) are real.) These are also
real, not stubs: `RestartAgent` (restarts the `picoclaw` systemd unit via
`restartPicoclawGateway`), `ResetAgent` (factory-reset wipe — see §8.2),
`GetConfigJSON` (returns `/root/.picoclaw/config.json` —
the structure file; secrets in `.security.yml` are never exposed),
`WriteMCPEntry` / `RemoveMCPEntry` (MCP connectors — see §8.1), and
`WatchIdentity` / `UpdateIdentityName` (`identity.go`) — PicoClaw's `IDENTITY.md` is
a 1-for-1 copy of OpenClaw's, so the `**Name:**` card line is watched (→ wake words)
and rewritten exactly as on OpenClaw.
HAL TTS/voice, Telegram fan-out, sensing-event queue/drain, and the run-marker
helpers (guard / broadcast / web-chat / silent / pose-bucket) are backend-agnostic
and behave exactly like the Hermes backend.

These stay no-ops **on purpose**: PicoClaw is provisioned out-of-process by
`install.sh` + `presync.sh` (§1.1), not by in-process gateway calls. Install,
model/channel config, and persona migration all happen in those scripts during the
`switch-runtime` flow. The one exception is **`EnsureOnboarding`**
(`onboarding.go`), which is real: it injects the OS-managed block into
`workspace/AGENTS.md` on boot/config-change (§1.1), the same contract openclaw has.

### 8.1 MCP connectors (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` wire remote-MCP connectors (the `connector.set`
MQTT flow — Notion, Asana, Linear, GitHub, Ahrefs, Figma) into PicoClaw's
`config.json`. They are the same shared callers OpenClaw/Hermes use; only the on-disk
shape differs, and PicoClaw's differs in two ways that `mcp.go` handles:

1. **Nesting.** PicoClaw servers live under **`tools.mcp.servers.<name>`**, not a
   top-level `mcp.servers` (OpenClaw) or `mcp_servers` (Hermes) map. `applyMCPServerWrite`
   creates the `tools` → `mcp` → `servers` chain if absent.
2. **Global gate.** `tools.mcp.enabled` defaults to **`false`** — a server written
   under a disabled block is silently ignored. `WriteMCPEntry` asserts it `true`.

The incoming OpenClaw-shaped entry (`{type:"http", url, headers}` for hosted MCP,
`{command, args, env}` for stdio) is passed through with **`enabled: true`** asserted
(PicoClaw's per-server flag). The `type` key is **kept verbatim** — PicoClaw's
transport values (`stdio` / `sse` / `http`) already match, and an explicit
`type:"http"` avoids PicoClaw's empty-type→`sse` inference. `RemoveMCPEntry` deletes
the named server (idempotent — `removed=false`, no restart, when absent) and leaves
`tools.mcp.enabled` on so other wired servers keep loading. Both paths write
`config.json` atomically (temp + rename, no chown — PicoClaw runs as root) under
`mcpMu`, then `restartPicoclawGateway`. Secrets in `.security.yml` are never touched.

### 8.2 Factory reset (`reset.go`)

`ResetAgent` is the PicoClaw factory-reset wipe, called by
`server/system/factoryreset.go` on the active gateway. Unlike OpenClaw (which keeps
`identity/` + `device-key.json` and lets `SetupAgent` recreate `openclaw.json`) and
Hermes (which resets `config.yaml`/`.env` in place and preserves `SOUL.md`),
**PicoClaw preserves nothing**: its `config.json` + `.security.yml` are regenerated
from the project `/root/config/config.json` by `presync.sh` on the next switch, so
the reset wipes `/root/.picoclaw` **wholesale** and re-onboards a clean baseline.

`wipePicoclawState()` runs four steps:

1. **`systemctl stop picoclaw` + verify** — the gateway's systemd unit (written by
   `install.sh`) sets `WorkingDirectory=/root/.picoclaw` and `Restart=always`, so it
   holds the data dir open and would recreate files under it. An explicit stop
   overrides `Restart=always` (it is not a crash), so the gateway stays down while we
   wipe. `waitForPicoclawStop` polls `is-active` for up to 5s.
2. **`systemctl disable picoclaw`** — a factory reset also wipes `/root/config/config.json`
   and reboots into the **default (openclaw)** runtime, so PicoClaw must NOT auto-start.
   `switch-runtime` re-enables it only when the user switches back.
3. **`rm -rf /root/.picoclaw`** — config, `.security.yml`, workspace (persona/memory/
   skills), sessions, and the **`.openclaw-migrated` marker** (so `presync.sh` §0
   re-migrates persona/memory from OpenClaw on the next switch).
4. **`picoclaw onboard`** (`HOME=/root`) — recreate a valid baseline (workspace +
   baseline `config.json`/`.security.yml`). Non-fatal: the config is not correct until
   `presync.sh` re-asserts the real model/channel wiring on the next switch, and
   `install.sh` also onboards when `config.json` is absent, so a failure self-heals.

The gateway is left **stopped + disabled** — the post-reboot setup wizard runs the
default runtime's `SetupAgent`, mirroring how OpenClaw/Hermes disable their own unit
in `ResetAgent`.

### Jev skill preloading

When enabled, OS onboarding installs a native `hooks.processes.jev` stdio hook, with
`intercept: ["before_llm"]`. Asset or registration changes participate in the
existing onboarding gateway restart; an unchanged reconcile is a no-op.
**Jev is disabled by default** by `const jevEnabled = false` in `runtimes/picoclaw/jev_hook.go`. When disabled, bridges construct no selector; native onboarding installs no assets and creates no Jev registration. If an older Jev registration exists, Go only disables that registration and preserves unrelated settings. Disabled integrations do not read skills, call the provider, or add Jev content to requests. Enabling after validation requires changing the Go switch, rebuilding and restarting through runtime management. The observer still manages the global `hooks.enabled` gate; Jev leaves that gate unchanged.
The sidecar stores only the absolute OS config path; credentials are read at use.

The hook shares Hermes's selector implementation: at most 32 candidates, a
3-second total selection/load budget, choice >= 0.70, margin >= 0.20 and
independent fit >= 0.60. Errors, busy workers, timeout, abstention, slash commands
and explicit `[skills: ...]` instructions preserve ordinary runtime discovery.
Only current user text and candidate descriptions reach Jev, never chat history.
Logs use `[picoclaw-jev]` and omit prompts, credentials and skill contents.

The candidate roster comes from PicoClaw's native system `<skills>` summary,
restricted to existing files inside `/root/.picoclaw/workspace/skills`. No extra
filesystem catalog is invented. Missing/unsupported roster shapes fail open.
The selected complete SKILL.md, absolute path and reference directory are loaded
as transient current-user-message context, preserving system messages and tools.
Files larger than 128 KiB, paths escaping that skill root, ambiguous names and
inline shell templates are rejected; nothing in a skill is executed by preloading.
A bounded 256-turn in-memory cache remembers only the selected skill and original
prompt. Every model iteration rechecks the current native roster and safely
reloads the complete file; removal or load failure invalidates that selection.
Attachment/content-part, system, sensing and already-handled turns bypass Jev.
Descriptor-relative no-follow opens reject symlinks and non-regular files; the
128 KiB limit also applies to the composed context. No persona or conversation-history file is written.
Unknown turn IDs/iterations and child turns abstain.

This native hook covers the `pico` WebSocket channel (OS voice/Web/MQTT) and
PicoClaw's own channels such as Telegram; it is not an OS dispatcher interceptor.
Contract evidence: the Autonomous PicoClaw checkout at
`3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488`, specifically
[`hooks.go`](https://github.com/autonomous-ai/picoclaw/blob/3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488/pkg/agent/hooks.go),
[`hook_process.go`](https://github.com/autonomous-ai/picoclaw/blob/3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488/pkg/agent/hook_process.go),
`pipeline_llm.go` and `pkg/skills/loader.go`: before-LLM hooks may modify non-system
messages, while system-message and tool-definition changes are rejected; the
pipeline uses hook messages for the provider call, separate from stored history.
Local mocked hook/config tests cover channel independence, turn isolation,
path/content bounds and opt-outs. No live Jev, native binary, paid task or robot
integration was run; the installed release still needs a native smoke test.

Bare/context-dependent follow-ups such as “brighter”, “Make it brighter”, “continue”
and “do it” bypass preloading so the main model can resolve the ongoing task;
explicit hardware targets and clear new digital requests keep normal selection.

Only the unique anchored `[voice-instruction]` is sent to the selector; a conflicting transcript is excluded. Malformed or empty authoritative envelopes bypass preloading. The original runtime message remains intact.
