# OpenCode agent backend

OpenCode is one of the **swappable agentic backends** the os-server can run behind
its agent gateway. The brain is pluggable (CLAUDE.md): os-server talks to
whatever backend `config.agent_runtime` selects through the single
`domain.AgentGateway` interface, so the rest of the pipeline (HAL TTS, `[HW:/…]`
hardware markers, Flow Monitor SSE, sensing drain, Telegram fan-out) never knows
which brain is active.

- **`openclaw`** (default): persistent WebSocket to the OpenClaw daemon. See `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: HTTP + SSE client against a local Hermes API server. See `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: persistent WebSocket client against a local PicoClaw runtime. See `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: the OpenAI Codex CLI behind a local WS bridge. See `docs/agentic/codex.md` + `runtimes/codex`.
- **`claudecode`**: the Claude Code CLI behind a local WS bridge. See `docs/agentic/claudecode.md` + `runtimes/claudecode`.
- **`opencode`**: the **[opencode](https://opencode.ai) CLI** (open-source AI coding agent) as the device brain, behind a local WS bridge. This doc. Code: `runtimes/opencode/`.

> Source of truth is the code. This documents `runtimes/opencode/` as implemented;
> keep it in sync on change (EN: this file, VI: `docs/vi/agentic/opencode_vi.md`).

> **Agentic-backend docs:** [`adding-agent-runtime.md`](adding-agent-runtime.md)
> (generic contract + how to add one) · [`hermes.md`](hermes.md) ·
> [`picoclaw.md`](picoclaw.md) · [`codex.md`](codex.md) ·
> [`claudecode.md`](claudecode.md) · this file (OpenCode).
>
> **Status: device-verified** (2026-07-23, `intern-v2` on opencode 1.18.4). The
> switch flow runs end-to-end — install → presync → gatewayd → per-turn
> `opencode run` → reply delivered. The on-device shakeout corrected four
> assumptions from the initial build (all now fixed in the code + this doc): the
> installer's dir override, the campaign-api wire (Responses API, not chat
> completions), the permission flag (`--auto`), and the terminal event
> (step_finish, no session.idle). See §10.

## 1. Overview & how it is selected

The opencode CLI is driven **per turn** (like codex), so the device runs a thin
local **WS bridge**: the `opencode.service` systemd unit runs **`os-server
opencode-gatewayd`** — the bridge is **compiled into the os-server binary**
(`runtimes/opencode/gatewayd`; no separate process to materialize, no Python).
The bridge exposes `ws://127.0.0.1:18793/opencode/ws/` (bearer token
`autonomous_opencode_token`) and spawns **one subprocess per turn**:

```
opencode run --format json --auto --dir /root/.opencode/workspace [--session <id>] [--file <img>…] <prompt>
```

resuming the session id persisted in `/root/.opencode/session.json` (`--session
<id>` — a plain flag, not a subcommand, so there is no codex-style flag-ordering
trap). Turns are strictly serialized (buffered queue + single worker). The
permissive flag is deliberate: an appliance running as root must never block on an
approval prompt — `--auto` auto-approves permissions not explicitly denied (the
shipped 1.18.4 flag; the dev-branch `--dangerously-skip-permissions` is not in
released builds). Model/provider come from `opencode.json` (presync-owned, §1.2)
— the bridge never passes `--model`.

`agent_runtime` in `config.json` picks the backend; resolution lives in
`system/agent/factory.go` `ProvideGateway()` — `"opencode"` →
`opencode.ProvideService`, anything unknown falls back to OpenClaw. On startup an
`AGENT BACKEND ACTIVE → OPENCODE` banner prints `ws_url` + `conversation`.

Wire constants (`runtimes/opencode/constants.go`, no per-unit config):

| Const | Default | Meaning |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18793/opencode/ws/` | Local bridge WebSocket endpoint |
| `Token` | `autonomous_opencode_token` | Bearer token on connect; the bridge reads the same value from `/root/.opencode/.env` (`OPENCODE_WS_TOKEN`, presync-owned) |
| `Conversation` | `device-main` | Label only — opencode owns its session ids (§4) |

State layout: the bridge's device-local state lives under `/root/.opencode/`
(`.env`, `session.json`, `workspace/`, `attachments/`, `install.log`); the
opencode CLI's own config/data live under **XDG** — `~/.config/opencode/`
(`opencode.json`, `AGENTS.md`, `skills/`) and `~/.local/share/opencode/`
(`auth.json`, sessions), with `HOME=/root`.

## 1.1 Install (`install.sh`)

An `opencode.setup` switch runs the generic `system/device/switch_runtime.sh`,
which materializes OpenCode's embedded scripts. `install.sh` (one-time, self-
sufficient — a direct `bash install.sh` fully configures AND starts the backend):

1. prerequisites `jq` + `curl` + `tar`;
2. installs the opencode CLI via the **official pinned installer**
   (`curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin
   bash -s -- --version <OPENCODE_VERSION>`) — it handles arch detection (linux
   arm64/x64), the `.tar.gz` asset + extraction, and is idempotent. Two device-
   learned gotchas are handled: the env var **must prefix `bash`** (in `VAR=x curl
   | bash` it binds to curl, not the piped bash), and the installer **still** put
   the binary in its default `~/.opencode/bin` on the test device — so a
   belt-and-suspenders step copies whatever the installer produced into
   `/usr/local/bin/opencode` (the path the unit + `verify` hook use).
   `OPENCODE_VERSION` is pinned (currently `1.18.4`) — the baseline for a
   freshly flashed image only: devices in the field update via
   `make upload-opencode <bare-semver>` + `make promote-opencode`, which the
   bootstrap worker applies as `software-update opencode`
   (`docs/bootstrap-ota.md` §5);
3. runs the presync hook once (`/usr/local/bin/runtime-opencode-presync`,
   materialized by os-server BEFORE the installer — §1.2);
4. writes + enables **`opencode.service`** (`ExecStart=/usr/local/bin/os-server
   opencode-gatewayd`, `EnvironmentFile=/root/.opencode/.env`, `HOME=/root`,
   `Restart=always`) — nothing to materialize for the bridge, it ships inside
   os-server; then drops a cheap offline `verify` hook (`command -v opencode` +
   os-server binary present) for switch-runtime self-heal.

Unit name == runtime name (`opencode.service`), so no `os-runtimes/opencode/service`
declaration file is needed. Install logs go to `/root/.opencode/install.log`
(persistent rootfs — `/var/log` is volatile zram on these boards).

## 1.2 Presync (`presync.sh`) — embedded, runs on every switch + every boot

`presync.sh` is embedded in os-server and materialized to
`/usr/local/bin/runtime-opencode-presync`. It runs before every opencode start
(switch-runtime), once at the end of install, **and on every os-server boot /
config change via `EnsureOnboarding`** (hermes pattern): `EnsureOnboarding` hashes
the presync-owned files (`opencode.json` + `.env`) around the run and restarts the
gateway only on a real change. It owns everything stateful:

- **§1 MIGRATE** — one-time persona/memory/skills copy from the openclaw
  workspace, gated on the marker `/root/.opencode/.openclaw-migrated`. Stops
  openclaw first (3 retries, non-fatal), then copies `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **and `AGENTS.md`**
  verbatim (opencode reads `AGENTS.md` natively — zero-translation persona slot;
  the Go onboarding re-injects the OS block anyway), plus `memory/` into the
  workspace and `skills/` into **`~/.config/opencode/skills`** (opencode's global
  discovery root) only when absent. The marker is written only after a clean copy,
  so a failed migrate retries next run; a factory reset wiping `/root/.opencode`
  clears it so migrate re-runs on the next switch.
- **§2 CONFIG** — regenerates `~/.config/opencode/opencode.json` from config.json
  via `jq`. It writes a top-level `model` = `campaign/<llm_model>` (fallback
  `Auto-AI`) and a **custom provider** `provider.campaign` using the
  **`@ai-sdk/openai`** npm adapter with `options.baseURL` from `llm_base_url`
  (fallback `https://campaign-api.autonomous.ai/api/v1/ai/v1`) and `options.apiKey`
  = the **reference** `"{env:LLM_API_KEY}"` (resolved from `.env` at launch — the
  real key never enters the JSON). **`@ai-sdk/openai` (not `@ai-sdk/openai-compatible`)
  because campaign-api speaks the OpenAI Responses API, not chat completions** —
  device-verified: `{base}/chat/completions` 404s, `{base}/responses` works, and
  opencode routes to the Responses API via `@ai-sdk/openai` (per opencode's
  provider docs). The existing `"mcp"` object is **preserved verbatim** — os-server's
  `mcp.go` owns those entries (§7), so the two owners never collide.
- **§3 ENV** — writes `/root/.opencode/.env` (systemd EnvironmentFile, mode 0600):
  `OPENCODE_WS_TOKEN` (must equal `constants.go` `Token`), `OPENCODE_PORT=18793`,
  `OPENCODE_WORKSPACE=/root/.opencode/workspace`, and `LLM_API_KEY` from
  `llm_api_key`.

Presync also writes `/etc/profile.d/agent-cli-env.sh` (interactive login shells
source the active runtime's `.env`, so a bare `opencode` in an SSH/web-CLI shell
reuses the campaign key — resolved live from `config.json` so it stays correct
across switches).

On top of the presync run, `EnsureOnboarding` (`onboarding.go`) does the same
workspace reconcile the other backends get: seeds `KNOWLEDGE.md` from the embedded
template only if absent, injects the OS-managed `<!-- OS DO NOT REMOVE -->` blocks
into `SOUL.md` / `AGENTS.md` / `HEARTBEAT.md`, refreshes the **global** user
AGENTS.md block (`~/.config/opencode/AGENTS.md`), and capability-gates skills.
Markdown-only changes never restart the gateway — each `opencode run` re-reads the
workspace; only a presync config change or a unit self-heal restarts it.

**Persona inline block (AGENTS.md).** opencode auto-loads `AGENTS.md` into context
(project `AGENTS.md` in the `--dir` workspace + the global
`~/.config/opencode/AGENTS.md`). Like codex, the persona is inlined INTO the
workspace `AGENTS.md` via an idempotent OS block (generated from `SOUL.md` +
`IDENTITY.md`), rebuilt on every `EnsureOnboarding` and right after a rename
(`UpdateIdentityName`) so the very next turn sees the new name.

### Skills — global `~/.config/opencode/skills` discovery

Device skills live in **`~/.config/opencode/skills/<name>/SKILL.md`** —
opencode's global discovery root (it also honors `.opencode/skills/` in the
project dir and `~/.claude/skills/` for Claude compatibility). All producers
target that XDG path: `presync.sh` §1 (openclaw migration), `skill_watcher.go`
(CDN download + the skill-change notify), and `pruneUnsupportedSkills`
(capability gate). Factory reset wipes `~/.config/opencode`, so the set is
re-migrated from openclaw on the next `EnsureOnboarding`.
`EnsureOnboarding` also refreshes every supported skill from the CDN on boot or
config reconciliation, self-healing a local skill that was stale before the watcher
started. It sends the skill-change notification after a possible gateway restart.
The watcher logs each successful metadata poll as `skill watcher: checked`; a ZIP
download or extraction failure leaves that skill's version pending for retry on the
next poll.

## 2. Transport & sending a turn

`client.go` holds one persistent WebSocket to the bridge (picoclaw-shaped: bearer
token, no pairing handshake, 25s ping keepalive → `pong`, reconnect with backoff,
`StateAgentDown` LED on drop). `chat.go` `sendChat` writes one frame and returns;
the reply arrives on the read loop:

```json
{ "type": "message.send", "id": "<reqID>", "run_id": "<runID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

The bridge saves attachments to `/root/.opencode/attachments` and passes them via
`opencode run --file <path>`. A `{"type":"session.new"}` frame makes the bridge
drop the persisted session id (§4). The bridge serializes turns and adds
`request_id`/`run_id` to turn frames, including resume retries and errors. The
adapter retains a FIFO of pending pairs and prefers explicit tags; older untagged
bridges use FIFO. Completed or interleaved frames are ignored before session updates.

A full queue returns `bridge.rejected` for that request/run only, preserving the
current turn and busy state. Terminal callbacks do not idle while transmitted
turns remain. Reconnect drains only **unsent** local events, preserving run IDs
and speaker gating. Offline callbacks retain the queue; disappearance before
any write restores its unsent tail. A failed attempted write has uncertain
delivery and is never replayed. Disconnect clears transmitted correlation,
which is never used as a replay source.


## 3. Event translation (`translator.go`)

The bridge forwards the `opencode run --format json` JSONL event content, adding correlation fields to turn frames
(plus its own `bridge.status` / `bridge.error` / `pong` frames); every opencode
line carries a `sessionID`. The Go translator maps them onto the same
`domain.WSEvent` shape the OpenClaw handler consumes:

| Inbound event | Emitted `domain.WSEvent` |
|---|---|
| first line carrying `sessionID` | capture session key |
| `step_start` | `agent` lifecycle `phase:start` (once per turn) |
| `text` | **buffered as the reply** (device shape: `part.text`; flat `text` accepted as fallback); no token delta stream. A newer part demotes the previous to `stream:thinking` (see *Preambles* below) |
| `reasoning` | *(ignored — thinking, not content)* |
| `tool_use` | `agent` tool `phase:start` + `phase:end` pair |
| `step_finish` / `message.updated` | capture per-turn token usage (`part.tokens` / `info.tokens`) |
| `session.idle` (synthesized by the gatewayd on clean exit) | `agent` `stream:assistant` (whole reply as **one** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` with usage — ends the turn |
| `session.error` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — ends the turn |
| `bridge.rejected` | `lifecycle.error` for the rejected request/run only; current turn preserved |
| `bridge.status` / `pong` | *(logged / ignored)* |

**Terminal event (device-verified 1.18.4).** `opencode run --format json` does
**not** emit a `session.idle`/`turn.completed` — a turn ends with a `step_finish`
whose `part.reason == "stop"`, then the process exits. Since `opencode run` is a
per-turn subprocess, a **clean exit (rc=0) is the turn boundary**: the gatewayd
(`turn.go`) marks the turn ended and **synthesizes a `{"type":"session.idle"}`
frame** so the translator finalizes exactly once. The buffered `text` is
surfaced there as a single assistant delta **before** `chat.final` / `lifecycle.end`
— the N=1 case of the streaming contract, which lets the shared consumer flush TTS
+ `[HW:/…]` hardware markers at `lifecycle.end`.

**Preambles.** opencode narrates before it calls a tool, as its own `text` part
("Using the sensing skill for this presence event."). Joining every part would
speak that whole trail — the same leak fixed in codex (see
[codex.md](codex.md)). So only the **last** `text` part of a turn is the reply:
each earlier one is demoted to `stream:thinking` (Flow Monitor only, never TTS
or a channel reply) as soon as a newer part proves it was not the reply.
Exception: a non-final part carrying a `[HW:/…]` marker is a real hardware
action and stays in the reply. Prompt wording cannot suppress preambles
reliably — this is the enforcement point.

**Usage:** token counts ride `step_finish` under `part.tokens.{input,output,cache.read}`
(also read from `message.updated` `info.tokens` when present). The translator
stashes the latest (`captureUsage` → `lastUsage`) and reads it at the synthesized
`session.idle`. opencode reports Anthropic-style — `input` EXCLUDES the cached
prefix — so the fields stay separate: `input → InputTokens`, `cache.read →
CacheReadTokens`, `cache.write → CacheWriteTokens`, `output → OutputTokens`,
`TotalTokens = input + cache.read + output`. Folding cache read into
`InputTokens` (the previous mapping) made a cache-hit turn read as if it had
re-sent the whole context and hid the `R` figure on the turn card.

## 4. Session

opencode owns the session: the `sessionID` is present on every JSONL line,
captured by the bridge and persisted in `/root/.opencode/session.json`, then
replayed via `opencode run --session <id>` (history lives under
`~/.local/share/opencode/` — process exit ≠ session loss). A resumed run whose
session no longer exists is retried fresh (the bridge's `resumeErrHints` catch the
missing-session error). `NewSession` sends a `session.new` frame → the bridge
drops the session id → the next turn is fresh.

`ShouldRotateSession` is a 150k-token safety net for runaway sessions. Per
[`adding-agent-runtime.md`](adding-agent-runtime.md) §4 "No fake success",
`CompactSession`, `UpdatePrimaryModel`, and `RefreshModelsConfig` return
`domain.ErrNotSupportedByRuntime` — never `nil` (an llm change still applies live:
the caller falls back to `EnsureOnboarding`, whose presync re-reads `llm_*` and the
hash gate restarts the gateway). Unlike codex, **`GetConfigJSON` does real work**:
opencode's config IS JSON, so it returns `~/.config/opencode/opencode.json`
verbatim (safe — the provider apiKey is a `{env:LLM_API_KEY}` reference; the real
secret lives only in `.env`).

## 5. Channels

Telegram, Slack and Discord are **device-owned** under OpenCode — identical to
codex (`SupportedChannels()` → `["telegram", "slack", "discord"]`). os-server runs
the receive loops itself, driven by `config.json` tokens read fresh on each use, so
there is nothing runtime-side to write and no restart needed. The full behavior
(receive loop, sender-metadata prefix, silent-run tracking, `stripForChannel`
cleanup, typing keepers, reply fan-out at `session.idle`) mirrors
[`codex.md` §5](codex.md) 1:1 — see `runtimes/opencode/{telegram_poll,slack,discord}.go`.
`AddChannel` / `RefreshChannelConfig` are honest no-op successes for the supported
channels and return `domain.ErrChannelNotSupported` for anything else (whatsapp).

### Telegram remote coding-sessions (`telegram_coding.go`, `coding_sessions.go`)

A Telegram chat can start a folder-scoped `opencode` coding turn and continue it
from the phone, separate from the device-main persona turn. Each accepted turn
spawns a fresh `opencode run --format json --auto --dir
<folder> [--session <id>] <prompt>` in os-server directly (independent of the
persistent gatewayd child); the reply is parsed from the opencode JSONL
(`parseOpenCodeResult`: `sessionID` → id, `text` → reply, `session.idle` → done)
and DMed chunked at Telegram's 4000-char limit. The exec env asserts `HOME=/root`
+ the presync `.env` pairs (there is **no** `OPENCODE_HOME` — opencode uses XDG
under HOME), so the coding child resolves the same `opencode.json` + auth the
gatewayd uses. A per-folder mutex serializes turns.

> ⚠️ **Cross-folder session discovery is intentionally degraded** in this pass.
> codex enumerated resumable threads by parsing its on-disk "rollout" JSONL store;
> opencode stores sessions internally under `~/.local/share/opencode/` and
> `opencode session list` is not confirmed to expose the working directory needed
> to resume in-folder across all projects. So `allCodingSessions()` returns empty
> (a `TODO(opencode-coding-sessions)` in `coding_sessions.go`): `/new <folder>` and
> per-turn `--session` resume work, but the `/resume` / `/sessions` **list** shows
> nothing until this is wired to a verified `opencode session list --json` (or a
> direct read of the session store) on-device (§10). Only the allowlisted
> `telegram_user_id` reaches any of this; the run is unsandboxed, so the allowlist
> is the security boundary.

## 6. Hooks

opencode ships no hooks loader, so OpenClaw's `emotion-acknowledge` hook is
reproduced **natively in Go** (`runtimes/opencode/emotion_ack.go`, mirroring
codex/hermes): on each user-visible turn, `sendChat` fires `{emotion:"thinking"}`
to HAL — same skip prefixes, same intensity, same capability gate
(`skills.SupportedHooks`) as the TS handler. The companion `turn-gate` hook is
intentionally not mirrored (`sendChat` already marks the turn busy). ⚠️ Keep it in
lockstep with `runtimes/openclaw/hooks/emotion-acknowledge/handler.ts` and the
sibling `emotion_ack.go` in hermes/picoclaw/codex/claudecode.

## 7. MCP connectors (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (the `connector.set` MQTT flow) edit the
top-level `"mcp"` object of `~/.config/opencode/opencode.json` via
`encoding/json`, atomically (temp + rename) under `mcpMu`, then restart the gateway
so the next `opencode run` picks the server up. Shape translation from the
canonical OpenClaw-shaped entry: an **http** entry → `{type:"remote", url,
headers, enabled:true}`; a **stdio** entry → `{type:"local", command:[cmd,
args…], environment:env, enabled:true}` (opencode wants a single merged `command`
array and names the env map `environment`). `RemoveMCPEntry` is idempotent
(`removed=false`, no restart, when absent). presync regenerates only the
provider/model head and preserves the `"mcp"` object (§1.2), so entries survive
every sync. A switch **into** opencode also clones the previous runtime's MCP
servers via `MCPReconcile` (the write path).

## 8. Factory reset (`reset.go`)

`ResetAgent` (called by `server/system/factoryreset.go` on the active gateway)
preserves nothing — opencode.json/.env are regenerated by presync on the next
switch: **stop** `opencode.service` (+ verify inactive, 5s poll), **disable** it
(reboot defaults to openclaw), **wipe** the bridge state dir `/root/.opencode`
**and** opencode's XDG dirs `~/.config/opencode` (opencode.json, AGENTS.md,
skills/) + `~/.local/share/opencode` (auth.json, sessions) and the
`.openclaw-migrated` marker (so presync §1 re-migrates on the next switch) — then
recreate the baseline `workspace/` + `attachments/` dirs (the CLI recreates its own
XDG state on first run). `/root/config/agent_state.json` is wiped in lockstep with
`config.json` by the platform reset (per `adding-agent-runtime.md` §7).

## 9. Migration & platform wiring

- **Persona/memory** (`system/agent/migrate_persona/runtime_opencode.go`): one
  read + one write adapter over the opencode workspace, layout-identical to
  OpenClaw's (presync seeds it as a verbatim copy). Registered in the `adapters`
  map, so opencode migrates both ways with every other runtime. SOUL → SOUL.md,
  identity → its own IDENTITY.md, MEMORY + daily + KNOWLEDGE + USER to their native
  slots; `Overwrite=true` for SOUL. `rebrandToOpenCode` maps other runtimes' brand
  names onto OpenCode, and `reOpenCode` is consumed by the openclaw/hermes/picoclaw
  rebrand functions for the reverse.
- **LLM config** (`system/agent/migrate_config/runtime_opencode.go`): reads/writes
  `provider.campaign.options.baseURL` in opencode.json + `LLM_API_KEY` in `.env`,
  mirroring the codex adapter.
- **Version uplink**: `opencode --version` is probed at startup
  (`runtime.go` → `GetOpenCodeVersion`) and reported as `opencode_version` on the
  MQTT `info` message (`domain.MQTTInfoResponse`). The cache is a
  `versioncache.Cache`: `GetOpenCodeVersion` re-probes whenever the binary's
  size/mtime changes, so an update applied under a running os-server shows up
  without restarting os-server.
- **Switch triggers**: MQTT `opencode.setup` (`KindOpenCodeSetup`), HTTP
  `POST /api/device/agent-runtime`, and the web Settings **Runtime** dropdown
  (`AgentRuntimeSection.tsx`). `domain.AgentRuntimes` includes `opencode`, so the
  generic switch/validate paths accept it with no per-runtime code.
- **Logs**: the Flow Monitor "openclaw"/"openclaw-service" log tabs resolve to
  `journal:opencode.service` while opencode is active (`server/logs.go`).

## 10. On-device shakeout (2026-07-23, `intern-v2`, opencode 1.18.4)

Verified working end-to-end; four fixes came out of it (all landed):

1. ✅ **Installer dir** — the official installer ignored `OPENCODE_INSTALL_DIR`
   (dropped the binary in `~/.opencode/bin`); the env var must prefix `bash` and a
   fallback copies the binary into `/usr/local/bin/opencode` (§1.1).
2. ✅ **Permission flag** — the shipped `opencode run` flag is `--auto`, not the
   dev-branch `--dangerously-skip-permissions`.
3. ✅ **Provider wire** — campaign-api serves the **Responses API** (`/responses`),
   not chat completions (`/chat/completions` 404s) → provider is `@ai-sdk/openai`
   (§1.2).
4. ✅ **Terminal event** — `opencode run` emits `text` (`part.text`) then
   `step_finish` (`part.reason:"stop"`, `part.tokens`) and exits; no `session.idle`
   from the CLI → the gatewayd synthesizes one on clean exit so the translator
   finalizes the reply (§3).

Still open:
- **`OPENCODE_VERSION`** is pinned to `1.18.4` — bump as `anomalyco/opencode`
  releases move.
- **Coding-session discovery** is degraded (§5) — wire it to a verified
  `opencode session list --json` (or a direct session-store read) that exposes the
  working directory, then drop the `TODO(opencode-coding-sessions)`.
- **Deploy note:** the gatewayd runs as a **separate** process
  (`os-server opencode-gatewayd` under `opencode.service`); a binary-only update
  needs `systemctl restart opencode.service` too — restarting `os-server.service`
  alone leaves the old gatewayd running (an OTA that also bumps presync config
  triggers the hash-gated gateway restart automatically).


## Jev skill preloading in the managed bridge

The OpenCode runtime bridge selects at most one eligible skill with Jev before forwarding a text-only user request. It reads the OS-installed native skill root, `$HOME/.config/opencode/skills`, and adds the complete selected skill and its directory to that request. This is a runtime adapter, not the hardware intent router and not a plugin installed into a standalone upstream CLI. OS-managed voice, Web/MQTT chat and ordinary messaging-channel conversations share this bridge. Standalone CLI sessions and Telegram coding-session mode run outside it and keep their normal native skill discovery.

Selection uses the shared `system/lib/jevskills` implementation with a three-second budget and fails open: abstention, missing credentials, invalid responses and load errors preserve the original request. System messages, slash commands and attachments skip preloading. For CLI execution, if the complete preload plus request would reach Linux’s 128 KiB single-argument limit, the bridge drops the preload, logs `reason=argument_budget`, and sends the original request; it never truncates a skill. Each prepared request reuses its context on a missing-session retry; no context is written to AGENTS.md or a global system prompt. The upstream runtime may retain the supplied turn in normal conversation history; the preload explicitly applies to the current request only.

**Jev is disabled by default** for this runtime pending native validation. The build switch is `const jevEnabled = false` in `runtimes/opencode/gatewayd/jev.go`. Enabling it after validation requires changing the Go switch, rebuilding and restarting through runtime management; environment variables cannot enable Jev. `JEV_CONFIG_PATH` defaults to `/root/config/config.json`; credentials are read from that file, never embedded in the preload. Skills with unsupported execution metadata are excluded. Malformed native settings, unsupported JSONC syntax, alternate config-root environment overrides and project skill catalogs also abstain. Ancestor project settings are checked to preserve inherited policies and skill precedence. If native settings contain custom skill or permission controls, the adapter conservatively leaves selection to the native loader rather than bypassing its policy. It does not grant permission to execute a selected skill or change existing tool approvals.

Local verification covers request isolation, non-user and attachment bypass, native-policy abstention and rendered child input. Mock selectors and fake runtime processes are used; live Jev, native model behavior and on-device delivery require separate integration testing.
