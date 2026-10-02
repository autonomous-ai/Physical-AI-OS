# MQTT — Documentation

## Overview

The OS server uses MQTT to communicate with the backend server (status reporting, OTA commands, channel management).

- Client: Eclipse Paho autopaho (Go)
- Auto-reconnect on connection loss
- Client ID format: `os-server-{DeviceID}`

## Configuration

```json
// config/config.json
{
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/{device_id}",
  "fd_channel": "fd/{device_id}"
}
```

## Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `fa_channel` | Server → Device | Commands from backend (from-agent) |
| `fd_channel` | Device → Server | Responses from device (for-device) |

## Commands

### Envelope Format

```json
{
  "cmd": "info|add_channel|slack_event|slack_command|whatsapp_pair|claudecode_login|claudecode_login_code|ota|data",
  ...payload fields
}
```

`ota` is a defined constant but has no router case; see [`ota` — not handled](#ota--not-handled).

### `info` — Report device information

**Receive:** `{"cmd": "info"}`

**Response (publish fd_channel):**
```json
{
  "device": "lamp",
  "type": "info",
  "version": "0.0.35",
  "id": "{DeviceID}",
  "mac": "{MAC address}",
  "time": "2026-03-26T17:00:00Z",
  "wakeword_enabled": false,
  "agent_runtime": "openclaw"
}
```

`agent_runtime` is the **effective** agentic backend currently running
(`openclaw` | `hermes` | `picoclaw` | `codex` | `claudecode` | `opencode`) — resolved as
`config.agent_runtime`, else the device's `ROBOT.md` `gateway.default`, else
`openclaw`. The response also carries these optional fields when known:
`hal_version`, `openclaw_version`, `hermes_version`, `picoclaw_version`,
`codex_version`, `claudecode_version`, `opencode_version`, `local_ip`, `tts_provider`, `tts_voice`,
`stt_language`, `timezone`, `unsupported_channels`, `skills`. `wakeword_enabled` is always
present and reports the effective top-level wake-word gate from config
(`true` or `false`; a missing legacy config value reports `false`). `timezone` is the device's
**live** IANA zone (e.g. `Asia/Ho_Chi_Minh`), read fresh from `/etc/timezone`
(falling back to config), not just the config record. The six per-runtime
versions are all probed at startup (each from its own `--version`) and
reported side by side; `agent_runtime` names the active one. Each cache
(`system/lib/versioncache`) also re-probes when the CLI binary's size/mtime
changes, so a runtime updated under a running os-server (OTA
`software-update <runtime>`, or a manual install) is reported on the next
`info` message instead of waiting for an os-server restart.

`unsupported_channels` (omitted when empty) lists the channels configured on the
device that the **active** runtime cannot run. It is populated by `ChannelReconcile`
after a runtime switch — e.g. switching `openclaw` → `picoclaw` (telegram-only) leaves
any configured `slack`/`discord` as unsupported. The list is sourced from
`config.channels_unsupported`, which `ChannelReconcile` rewrites on each switch.

`skills` (omitted when empty) is what the **active** runtime currently has
installed — the same set the web UI's Manage-skills panel shows
(`AgentGateway.ListSkills`). Shape is
`[{"name":"music","description":"Play music."}]`: name + description only, never
the per-skill file trees that `GET /api/agent/skills` also returns. The HTTP ping
carries the identical array (same `domain.SkillSummary` type, so the two uplinks
cannot drift). Best-effort — a runtime that can't list skills or an unreadable
skills dir omits the field instead of failing the uplink. The field lives on
`MQTTInfoResponse`, which the `data` replies embed, but only `handleInfo`
populates it, so `data` results never carry it.

**HTTP backend ping mirrors these fields.** The device-initiated ping
(`POST {llm_base}/ping`, built by `system/device.buildPingPayload`, sent via
`system/beclient`) carries the same device-state fields as this `info` uplink —
`local_ip`, `device`, `device_id`, `timezone`, `tts_provider`, `tts_voice`,
`stt_language`, `wakeword_enabled`, `hal_version`, `unsupported_channels` — plus `agent_runtime` and
`agent_runtime_version`. Unlike `info` (which reports every installed backend's
version side by side), the ping sends **only the active runtime's version**. It
fires (1) right after WiFi join during setup (status `setting_up`,
fire-and-forget — publishes `local_ip` before the up-to-2-min agent setup, so
the Setup-popup rescue described in `docs/setup-flow.md` can work), (2) once
when setup completes (status `working`), and (3) periodically from the status
reporter. Fields the backend doesn't consume are simply ignored.

The ping also carries **`skills`** — what the active runtime currently has
installed, the same set the web UI's Manage-skills panel shows
(`AgentGateway.ListSkills`, also served by `GET /api/agent/skills`). Sent on
every ping so the backend's per-device skill index self-heals, the same rationale
as `slack_team_id`. Shape is `[{"name":"music","description":"Play music."}]` —
**name + description only**, deliberately not the per-skill file trees that
endpoint also returns: the ping fires every 15s, so shipping full trees that
often would be pure waste (the web detail pane fetches them on demand from
`GET /api/agent/skills/files` instead). Best-effort — a runtime that cannot list
skills, an unreadable skills dir, or a device with none simply omits the field
rather than failing a ping that also carries the setup-critical `local_ip`.

**The ping survives a LAN address change.** A device's address is not stable —
moving the ethernet cable to another network, or a DHCP re-lease, changes it
while os-server keeps running. `beclient` holds keep-alive connections bound to
the *old* source address, and those don't fail fast: the old path is simply gone,
so there is no RST to observe and the next ping writes into a blackhole until the
15s client timeout, logging `ping failed` once per stale connection. The status
reporter therefore compares `local_ip` against the previous tick's and calls
`beclient.CloseIdleConnections()` when it moved, so the next ping dials fresh.
The client also owns its transport (cloned, `IdleConnTimeout` 30s) rather than
using `http.DefaultTransport`, so dropping the pool can't disturb other HTTP
users in the process.

### `add_channel` — Add messaging channel

**Receive:**
```json
{
  "cmd": "add_channel",
  "channel": "telegram|slack|discord|whatsapp",
  "config": {
    // telegram: bot_token + chat_id
    // slack:    bot_token + app_token + channel_id        (socket mode, default)
    // slack:    bot_token + mode:"http" + signing_secret  (+ optional webhook_path, default /slack/events)
    // discord:  bot_token + guild_id  + user_id
    // whatsapp: user_id (E.164 phone — only field; the bot logs in via Baileys)
  }
}
```

**Slack transport modes.** `mode` selects how OpenClaw receives Slack events:

- **`socket`** (default when `mode` is omitted) — OpenClaw opens an outbound WebSocket to Slack; requires `app_token`. Existing installs are unaffected.
- **`http`** — OpenClaw listens for Slack Events API POSTs at `webhook_path` (default `/slack/events`) and re-verifies the Slack signature with `signing_secret`; `app_token` is not used. A public proxy (bff-campaign-service) receives Slack's HTTP events and fans them out to the owning device over MQTT as `slack_event` (below). HTTP mode is the message-loss-tolerant path because Slack retries failed deliveries ~3× over 5 min.

**Response (single — telegram/slack/discord):**
```json
{
  "device": "lamp",
  "type": "add_channel",
  "channel": "telegram",
  "status": "success|failure",
  "error": "..."
}
```

**Capability gate.** `add_channel` is capability-aware: when the **active** agent
runtime cannot run the requested channel, the device replies `status:"failure"` with
the stable error code `error:"channel_not_supported"` (mapped from
`domain.ErrChannelNotSupported` via `errors.Is`, mirroring how `channel.refresh_config`
maps its sentinels). Previously every runtime silently accepted any channel. Each
runtime declares its own `SupportedChannels` — e.g. `picoclaw` runs telegram only, so
`slack`/`discord`/`whatsapp` return `channel_not_supported`.

**Response (streamed — whatsapp):** the device publishes one fd_channel message
per pairing event:

1. `{"status":"pairing_starting"}` — CLI subprocess launched.
2. `{"status":"pairing_qr","pairing_qr_text":"<unicode-block grid>","pairing_qr_format":"unicode_blocks_2x1","pairing_qr_seq":1,"pairing_expires_at":"<RFC3339>"}` — repeated up to 5 times as Baileys rotates the QR (~20s each).
3. One terminal event:
   - `{"status":"success"}` — link confirmed; emitted after a 5-minute post-pair sync wait so Baileys' history/pre-keys finish loading before the operator is told the channel is ready.
   - `{"status":"timeout","error":"..."}` — operator did not scan within the QR window.
   - `{"status":"failure","error":"..."}` — CLI exited unexpectedly or another pairing was already in progress.

If a Baileys session already exists on disk (`<openclaw_config_dir>/credentials/whatsapp/default/creds.json`), the device skips QR rendering and publishes just `{"status":"success"}`.

### `whatsapp_pair` — Re-run WhatsApp pairing

Re-runs the QR-scan flow without re-bootstrapping the channel config. Used when the Baileys session was lost and needs re-linking.

**Receive:** `{"cmd": "whatsapp_pair"}`

**Response (streamed):** same shape as the whatsapp `add_channel` stream above, but `type:"whatsapp_pair"`. Timeout 120 s (vs. 10 min for `add_channel`) — no plugin install or restart on this path.

### `claudecode_login` / `claudecode_login_code` — claude.ai OAuth login (claudecode runtime)

Runs the claude.ai subscription login (`claude setup-token`) on a device whose active
runtime is claudecode, so the brain authenticates with the user's Claude account
instead of `llm_api_key`. Only the claudecode runtime supports it — other runtimes
answer a one-shot `{"status":"failure","error":"claude login not supported on … backend"}`.
See `docs/agentic/claudecode.md` §"Auth".

**Receive:** `{"cmd": "claudecode_login"}`

**Response (streamed, `type:"claudecode_login"`):**

1. `{"status":"pairing_starting"}`
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/authorize?..."}` — the
   user opens this URL in a browser, authorizes, and copies the code it shows.
3. terminal: `{"status":"success"}` (token persisted to config.json
   `claude_code_oauth_token`; presync flips the runtime to subscription auth and the
   bridge restarts) · `{"status":"timeout","error":"no login within 10m0s"}` ·
   `{"status":"failure","error":"..."}`.

**Receive (second leg):** `{"cmd": "claudecode_login_code", "code": "<pasted code>"}` —
feeds the browser code back into the waiting flow. Acked with
`{"status":"code_accepted"}` (or `{"status":"failure","error":"no claude login in progress"}`);
the flow's own terminal status still arrives on the `claudecode_login` stream.

Unlike `whatsapp_pair`, the login handler does not block MQTT dispatch while the flow
runs — the code arrives as a second MQTT command, which could never be dispatched if
the first handler held the loop.

### `slack_event` — Forward a Slack Events API delivery (HTTP mode)

Sent by the public Slack-events proxy (bff-campaign-service) when Slack delivers an
Events API POST for a workspace this device owns. The payload (a verbatim forward of
Slack's HTTP request body + signature headers) and the MQTT wire shape are unchanged —
but **how the device handles it now branches on the active runtime** (the handler
type-asserts the agent gateway to `domain.SlackBridge`):

- **Runtimes that serve the Slack webhook themselves** (not a `SlackBridge` — today:
  OpenClaw) — unchanged behavior: the device POSTs the verbatim body + signature headers to
  the local gateway's `webhook_path` (default `http://127.0.0.1:18789/slack/events`), which
  re-verifies the Slack signature against the shared `signing_secret`. The fd_channel ack
  carries the gateway's HTTP status. Only relevant when the device's slack channel is
  configured with `mode:"http"` (see `add_channel`).
- **Runtimes whose native Slack support is Socket Mode only** (implement
  `domain.SlackBridge`) — this branch is **generic to any such runtime** (hermes is the
  current example, not a special case): with only Socket Mode it has **no local HTTP Slack
  webhook**, so os-server **is** the HTTP-mode Slack frontend for it. It parses the event
  itself and drives a turn (`HandleInboundSlack`). The reply is rendered **straight to
  Slack via the Bot API**, not relayed back over MQTT, using Slack's **native streaming
  API**: `chat.startStream` (opens the streaming message) → `chat.appendStream`
  (progressive `markdown_text`) → `chat.stopStream` (finalize), plus
  `assistant.threads.setStatus` for the native "…is typing" indicator. The fd_channel ack
  still becomes `status:"success"` (`http_status` 200) once the inbound turn is dispatched.
  A `url_verification` challenge
  normally terminates at the public proxy (which owns the Slack Request URL), so it is
  handled defensively here and still acked `success`.

**Receive:**
```json
{
  "cmd": "slack_event",
  "event_id": "Ev123",
  "body": "<raw Slack JSON body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/json"
  }
}
```

The device dedups on `event_id` with a 5-minute in-memory LRU (matches Slack's retry
window) and forwards headers verbatim so OpenClaw's signature check validates.

**Response (publish fd_channel):**
```json
{
  "channel": "slack",
  "type": "slack_event",
  "event_id": "Ev123",
  "status": "success|failure|skipped_duplicate",
  "error": "...",
  "http_status": 200,
  "info": { /* same device/version metadata as other acks */ }
}
```

For the proxy to route inbound events back to the right device, each `/ping` includes
`slack_team_id` — the workspace ID the device resolves on-device via Slack `auth.test`
against its stored `botToken` (cached, sent once resolved).

### `slack_command` — Forward a Slack slash command (HTTP mode)

Sent by the same Slack proxy (bff-campaign-service) when Slack delivers a slash-command
invocation (`/openclaw`, `/new`, ...) for a workspace this device owns. Forwarded and
verified exactly like `slack_event`: the device POSTs the verbatim body + signature
headers to the **same** OpenClaw gateway `webhook_path` (default
`http://127.0.0.1:18789/slack/events`) — OpenClaw's single HTTP endpoint routes events
vs. commands by body shape (urlencoded `command=` vs. JSON `type`) and replies to the
user via the command's `response_url`. Only relevant when the device's slack channel is
configured with `mode:"http"` (see `add_channel`).

**Receive:**
```json
{
  "cmd": "slack_command",
  "event_id": "<trigger_id>",
  "body": "<raw urlencoded form body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/x-www-form-urlencoded"
  }
}
```

Differences from `slack_event`: the body is the urlencoded slash-command form (it carries
`command`, `text`, `response_url`, `trigger_id`, ...), the `Content-Type` is
`application/x-www-form-urlencoded`, and the `event_id` slot carries Slack's `trigger_id`
(slash commands have no `event_id`) — reused as the dedup key.

**Runtime support:** slash commands remain **OpenClaw-only**. The hermes `SlackBridge`
defers slash commands for now (v1) — only `slack_event` is runtime-aware — so on a hermes
device `slack_command` still follows the OpenClaw local-webhook path described above.

**Response (publish fd_channel):** same shape as `slack_event` but `type:"slack_command"`.

### `data` — Generic data envelope

A generic envelope whose `kind` selects a sub-handler. The optional `data` object
carries kind-specific fields. Every kind replies on fd_channel with the same shape:
the standard device/version metadata plus `kind`, `status` (`success|failure`),
optional `error`, and an optional `data` payload.

`tts.set` accepts optional `speed` in `0.25–4.0`, for example,
`{"cmd":"data","kind":"tts.set","data":{"speed":1.2}}`. Omitting it
preserves saved speed; without one, `HAL_TTS_SPEED` applies (default `1.2`).
The command acknowledges `starting` then `success` or `failure`, saving and
reapplying HAL settings even when unchanged. The `info` uplink includes effective
`tts_speed`. ElevenLabs HTTP v3 applies speed locally with provider speed `1.0`;
other ElevenLabs models clamp the outgoing speed to `0.7–1.2`.

`tts.preview` accepts the same optional `speed` range (`0.25–4.0`) for the
preview utterance only. For example:
`{"cmd":"data","kind":"tts.preview","data":{"text":"Hello","voice":"Rachel","speed":1.5}}`.
It does not save config or change the speed of subsequent normal speech.
Omitted/null speed uses the runtime setting. Invalid speed returns `failure`
before synthesis; valid requests acknowledge `starting`, then `success` or
`failure`. Success means HAL accepted playback, not that playback finished.

**Receive:** `{"cmd": "data", "kind": "<kind>", "data": { ... }}`

| Kind | Purpose | `data` fields |
|------|---------|---------------|
| `buddy.pair.start` | Issue a single-use 6-digit Buddy pairing code, valid 60s | _(none; optional `data` ignored)_ |
| `buddy.pair.revoke` | Revoke the current Buddy pairing and disconnect its WebSocket | _(none; optional `data` ignored)_ |
| `buddy.status` | Query the current Buddy pairing and connection snapshot; also emitted on changes | _(none; optional `data` ignored)_ |
| `tts.set` | Persist TTS voice/provider/language/speed config | `provider`, `voice`, `language`, optional `speed` |
| `tts.preview` | One-shot TTS preview (no config write) | `text` (required), optional `provider`/`voice`/`language`/`speed` |
| `wakeword.gate` | Set the top-level wake-word gate (async; acks `starting`) | `enabled` (required boolean) |
| `timezone.set` | Apply the device's IANA timezone (async; acks `starting`) | `timezone` (required, e.g. `Asia/Ho_Chi_Minh`) |
| `oauth.set` | Store/replace an OAuth token for a provider | `provider`, `access_token`, optional `refresh_token`/`token_type`/`expires_at`/`scopes`/`user_email`/`client_id` |
| `oauth.remove` | Delete the stored OAuth token for a provider | `provider` |
| `connector.set.<code>` | Store/replace credentials for a connector (async; acks `starting`) | `connector`, `auth_type`, optional `access_token`/`refresh_token`/`api_key`/`expires_in`/`expires_at`/`scopes`/`credentials`/`refresh` |
| `connector.remove.<code>` | Delete a connector's credentials (async; acks `starting`) | `connector` |
| `channel.refresh_config` | Re-apply a channel's canonical config block (async; acks `configuring`) | `channel` |
| `skills.install_store` | Install ONE catalog skill on the active runtime (async; acks `starting`) | `id`, optional `name` |
| `skills.files` | Read one installed skill's files — list, or one file's contents (synchronous) | `name`, optional `path` |
| `skills.uninstall` | Remove one installed skill from the active runtime (synchronous) | `name` |
| `skills.save` | Write one authored skill into the active runtime's skills dir (synchronous) | `name`, `description`, `instructions` |
| `skills.upload` | Install one `.md`, `.zip`, or `.skill` file on the active runtime (synchronous) | `filename`, `content_base64` |
| `face.enroll` | Enroll one face photo via HAL `POST /face/enroll` (async; acks `starting`) | `image_base64`, `label`, optional `telegram_username`/`telegram_id` |
| `face.owners` | List enrolled people via HAL `GET /face/owners` (synchronous) | _(none)_ |
| `face.remove` | Delete one person's whole `users/<label>/` folder (face, voice, Telegram, history) via HAL `POST /face/remove` (async; acks `starting`) | `label` |
| `voice.enroll` | Record 15s on the lamp's own mic and enroll the voice via HAL `POST /speaker/record-enroll` (async; acks `starting` with the duration) | `label` |
| `voice.owners` | List everyone's voice sample files from `users/<label>/voice/` (synchronous) | _(none)_ |
| `voice.remove` | Delete one person's whole voice profile via HAL `POST /speaker/remove`; face data stays (async; acks `starting`) | `label` |
| `voice.file.get` | Return one voice sample file as base64 for playback (synchronous) | `label`, `file` |
| `voice.file.remove` | Delete one voice sample and its `.npy`, like `POST /api/voice/file/remove` (synchronous) | `label`, `file` |
| `led.resting.get` | Read the owner's resting light choice, device default and look in effect via HAL `GET /led/resting` (synchronous) | _(none)_ |
| `led.resting.set` | Save the resting light (`default`, `off` or `custom` colour) via HAL `PUT /led/resting`; survives reboots (synchronous) | `mode`, `color` for `custom` |
| `led.resting.preview` | Paint a candidate colour without saving via HAL `POST /led/resting/preview`; reverts 10 s after the last preview; acks failures only | `color` |
| `chat.file.get` | Fetch one device-local file a turn named (synchronous) | `path` (required), optional `session_id`/`run_id` |
| `chat.send` | Start an agent turn from the backend and stream it back (acks a run id, then emits `chat.event`) | `message` (required), optional `images[]`/`files[]`/`session_id`/`speak` |
| `environment.status` | Read the HAL environment snapshot by capability, independent of sensor model | _(none)_ |
| `system.info` | Aggregate snapshot: versions + network + host | _(none)_ |
| `system.version` | Component versions only (cheaper than `system.info`) | _(none)_ |
| `system.network` | network facts of the default-route interface only | _(none)_ |
| `system.reboot` | Queue HAL's cue-aware OS reboot | _(none)_ |
| `system.shutdown` | Queue HAL's cue- and servo-aware OS shutdown | _(none)_ |
| `system.ota_versions` | Per-component OTA versions + what bootstrap is installing now (the web Versions card's data) | _(none)_ |
| `system.software_update` | Force-install the published version of one component (the Versions card's `update` button); replies `success`/`started`, then an unsolicited completion report | `target` (required) |
| `device.rename` | Rewrite the agent name in `IDENTITY.md` (new wake words follow via WatchIdentity); then tells the agent its new name | `name` (required) |
| `device.soft_reset` | Delete `config.json` and restart os-server into AP setup mode (no reboot, firmware kept). Fire-and-forget: **no ack** is published | _(none)_ |
| `openclaw.setup` / `hermes.setup` / `picoclaw.setup` / `claudecode.setup` / `codex.setup` / `opencode.setup` | Switch the agentic backend to the runtime the kind names (acks `starting`, then `success` after the readiness probe passes, or `failure` after rollback; each ack echoes the kind). os-server restarts after `success` | _(none)_ |
| `schedule.sync` | Replace the whole scheduled-task list (backend is authoritative); acks `success` with each schedule's `next_run_at` | `timezone`, `schedules[]` |
| `schedule.run` | Run one stored schedule now (cadence and `next_run_at` unchanged); `failure` when the agent is busy | `id` (required) |
| `schedule.mutate.ack` | Backend verdict on one device proposal; the device drops the queued intent on any terminal verdict | `intent_id`, `applied`, `conflict`, optional `reason` |
| `harness.pair.start` | Start Harness pairing; replies with the pairing info (live code) | _(none)_ |
| `harness.status` | Harness pairing/connection status | _(none)_ |
| `harness.pair.cancel` | Cancel a pending Harness pairing; replies `{cancelled:true}` | _(none)_ |
| `harness.pair.revoke` | Remove the Harness pairing and turn Harness-only voice off; replies `{unpaired:true}` | _(none)_ |

`schedule.mutate` is **outbound** only (published on `fd_channel`, never received): when the user creates, edits or deletes a task from the device's own UI (`POST`/`PATCH`/`DELETE /api/schedule…`), the device publishes a proposal `{intent_id, op, schedule_id?, base_rev?, schedule?}` and retries it until `schedule.mutate.ack` arrives; the next `schedule.sync` is what the device treats as truth.

`system.reboot` and `system.shutdown` publish `status:"starting"` before the
device schedules the action, so the backend receives an acknowledgement before
the device drops off the network. They share the OS-server single-flight guard:
when another power action is already pending, a terminal `status:"failure"`
reply follows with the reason. The commands invoke HAL's full actions, not raw
OS commands: reboot plays its cue; shutdown plays its cue and releases servos.

This request/reply does not generate agent events. The separate OS worker handles
sustained changes; see [Lamp environment sensing](../robots/lamp/docs/environment-sensing.md#os-change-policy-and-agent-access).

The shared snapshot supports SEN55 + SCD41 or SEN63C without a new MQTT kind.
Per-component JSON `enabled` flags control acquisition, not a selection list.
`sample` always contains nine nullable metric keys and a nullable timestamp.
Missing, disabled or unavailable readings are null; SEN63C has no VOC/NOx.
`components` holds independent
status/error/timing/sample diagnostics, `sources` maps metrics to components,
and `metric_timestamps` carries their observation times. Group `ready` means
at least one fresh metric; `partial` indicates an enabled component is
unavailable. Check individual sources rather than treating the group's newest
timestamp as the age of every metric. A failed sensor does not discard healthy
readings. Missing CO₂ never produces an inferred value.

**`environment.status`:** send on `fa_channel`:

```json
{"cmd":"data","kind":"environment.status","data":{}}
```

The OS checks the device's declared `environment` capability, not SEN55 or any
hardware model. It reads local HAL `GET /environment/status` with a 5-second
timeout and returns the unchanged JSON snapshot in `data` on `fd_channel`.
Replies use the standard `MQTTDataResponse`; this example omits the normal
device/version/id/mac/time metadata:

```json
{"type":"data","kind":"environment.status","status":"success","data":{"enabled":false,"state":"disabled","last_error":null,"sample":{"pm1_0_ug_m3":null,"pm2_5_ug_m3":null,"pm4_0_ug_m3":null,"pm10_ug_m3":null,"temperature_c":null,"humidity_pct":null,"voc_index":null,"nox_index":null,"co2_ppm":null,"timestamp":null},"age_s":null,"stale":true,"sources":{},"metric_timestamps":{}}}
```

The snapshot may also contain `timing` and other fields supplied by HAL.
`success` means a valid snapshot was read, including when the sensor is disabled,
in error, or stale. Clients must inspect `data.state`, `data.stale`, `data.sample`,
and `data.last_error` before displaying measurements. A missing capability returns:

```json
{"type":"data","kind":"environment.status","status":"failure","error":"environment capability not declared"}
```

HAL transport errors, non-200 HTTP responses, and invalid status JSON also return
`failure` with `error`. This is request/reply only: no subscription, continuous
stream, events, or agent invocation. Replies retain the request's `kind`; the
existing protocol adds no request ID. Mobile clients use the device's existing
broker credentials and topic ACLs.

**`system.info` response:** synchronous (no `starting` intermediate); each probe
falls back to its zero value on failure.
```json
{
  "device": "lamp",
  "type": "data",
  "kind": "system.info",
  "status": "success",
  "data": {
    "versions": {
      "os-server": "0.0.35",
      "bootstrap": "0.0.10",
      "hal": "1.2.3",
      "openclaw": "2026.6.10",
      "openclaw_detected": true
    },
    "network": {
      "private_ip": "192.168.1.42",
      "interface": "wlan0",
      "mac": "aa:bb:cc:dd:ee:ff",
      "ssid": "MyWiFi",
      "gateway": "192.168.1.1"
    },
    "host": {
      "hostname": "lamp-7f72",
      "device_id": "{DeviceID}",
      "device_name": "lamp-7f72",
      "uptime_seconds": 86400,
      "timezone": "Asia/Ho_Chi_Minh"
    }
  }
}
```

The `host.timezone` field is the device's **live** IANA zone, read fresh from the
system (`/etc/timezone`, falling back to config); omitted when it can't be resolved.

The `network` block describes **the interface holding the default route**, not `wlan0`
specifically — `network.PrimaryInterface()` reads it from `ip route show default`, so a
device wired over ethernet reports `"interface": "end0"` with that link's `private_ip`
and `mac`, and an empty `ssid` (it is not associated to any WiFi). When there is no
default route at all — AP/provisioning mode — it falls back to `wlan0`, which then holds
the AP's own `192.168.100.1`. Previously the interface was hardcoded, so an
ethernet-only device reported a blank IP and MAC despite being perfectly reachable.

`system.version` returns just the `versions` block as `data`; `system.network`
returns just the `network` block. Version probes: `os-server` from the ldflags build
var, `bootstrap` via `bootstrap-server --version`, `hal` over HTTP from the
local HAL `/version` endpoint, `openclaw` from the agent monitor's cached probe
(`openclaw_detected` distinguishes "not installed" from "installed but unparseable").

#### `system.ota_versions` / `system.software_update`

The cloud twin of the web Versions card. Both kinds and the HTTP endpoints
(`GET /api/system/ota-versions`, `GET /api/system/ota-updating`,
`POST /api/system/software-update/:target`) call the same package,
`system/ota`, so they share the target allowlist, the `agent` alias and **one
per-target 30 s rate limiter** — a web click and a cloud command for the same
target within 30 s are rate-limited against each other. No admin auth on MQTT
(same trust as `system.reboot`).

`system.ota_versions` (synchronous) returns bootstrap's `/versions` report —
including the `agent` alias of the configured runtime's CLI — and `/updating`:

```json
{"cmd":"data","kind":"system.ota_versions","data":{}}
{"kind":"system.ota_versions","status":"success","data":{
  "versions":{
    "hal":{"current":"1.4.2","target":"1.4.3","min_version":"1.4.2","update_available":true,"held_by_floor":true},
    "hermes":{"current":"0.21.1","target":"0.21.1","min_version":"0.21.1","update_available":false,"held_by_floor":false},
    "agent":{"current":"0.21.1","target":"0.21.1","min_version":"0.21.1","update_available":false,"held_by_floor":false}},
  "updating":["hal"]}}
```

Bootstrap unreachable (or any other read error) → `status:"failure"`,
`error:"bootstrap unreachable: …"`, no `data`.

`system.software_update` — `target`: `os-server` | `bootstrap` | `web` | `hal` |
`device` | `codex` | `claudecode` | `opencode` | `picoclaw` | `hermes` | `agent`
(virtual: resolved to the configured runtime's CLI). The immediate reply is the
**terminal** `success` with `state:"started"` (an install runs for minutes, so
the backend must not wait on an intermediate ack):

```json
{"cmd":"data","kind":"system.software_update","data":{"target":"agent"}}
{"kind":"system.software_update","status":"success","data":{"target":"agent","resolved_target":"hermes","state":"started"}}
```

Rejections are `status:"failure"` with the same messages as the HTTP API:
`unknown target: <t>`, `software-update <t> rate-limited, retry in <n>s` (plus
`data.retry_after_seconds`), `bootstrap unreachable: …`,
`bootstrap refused <t>: <status> <body>`:

```json
{"kind":"system.software_update","status":"failure","error":"software-update hal rate-limited, retry in 27s","data":{"target":"hal","retry_after_seconds":27}}
```

After a `started` reply the device polls bootstrap `/updating` every 3 s (up to
40 min; a target not seen there within 15 s counts as finished), re-reads the
versions and publishes an **unsolicited** report with the same kind:

```json
{"kind":"system.software_update","status":"success","data":{"target":"hal","resolved_target":"hal","state":"completed","current":"1.4.3","target_version":"1.4.3","update_available":false}}
{"kind":"system.software_update","status":"failure","error":"update finished but hal is still at 1.4.2 (published 1.4.3)","data":{"target":"hal","resolved_target":"hal","state":"failed","current":"1.4.2","target_version":"1.4.3","update_available":true}}
```

The completion report is best-effort: installing `os-server`, `device` or
`hermes` restarts os-server, which kills the watcher, so no report arrives for
those — poll `system.ota_versions` for the final state.

An unrecognized `kind` replies with `status:"failure"` and `error:"unknown kind: <kind>"`.

#### `wakeword.gate`

Turns the top-level `wakeword` flag on or off. It uses the same asynchronous
acknowledgement pattern as `realtime.set`: the device acknowledges receipt,
persists the flag to `config.json`, restarts HAL when the value changes, then
publishes the outcome.

**Receive:** `{"cmd":"data","kind":"wakeword.gate","data":{"enabled":true}}`

The terminal success acknowledgement echoes `{"enabled":true}`. Omitting
`enabled` or supplying invalid JSON returns `status:"failure"`. `success`
means the flag was saved and HAL is restarting; it does not wait for HAL to be
ready.

#### `timezone.set`

Sets the device's IANA timezone. Same async shape as `realtime.set` / `tts.set`:
the device acks immediately, applies the change in the background, then acks the
outcome.

**Receive:** `{"cmd": "data", "kind": "timezone.set", "data": {"timezone": "Asia/Ho_Chi_Minh"}}`

**Ack flow** (each on fd_channel, carrying the standard device/version metadata plus
`kind:"timezone.set"`):

1. `{"status":"starting"}` — command received, before applying.
2. One terminal ack:
   - `{"status":"success","data":{"timezone":"Asia/Ho_Chi_Minh"}}` — applied (the
     requested zone is echoed back in `data`).
   - `{"status":"failure","error":"..."}` — rejected (e.g. unknown zone, or invalid
     JSON payload).

**Apply:** the zone is validated against `/usr/share/zoneinfo` (an unknown zone →
`failure`), then the device rewrites the `/etc/localtime` symlink, writes
`/etc/timezone`, runs `timedatectl set-timezone` best-effort, and persists `timezone`
to `config.json`. The change takes effect **without a HAL restart** — HAL's clock
helpers read `/etc/timezone` fresh on each call.

#### Connectors

`connector.set.<code>` / `connector.remove.<code>` route by prefix (the connector
code is the suffix). A single **data-driven** writer (`connectorWriter`) handles
every connector; a small map of **special writers** claims the handful of codes that
can't be expressed as a plain HTTP MCP entry (today only `figma-api`, a local stdio
MCP server). The generic writer decides per-message — from the payload — whether the
connector is an MCP server and how to authenticate; there is **no per-connector
registry to update for a new connector**.

**Storage:** every connector persists to its own `<code>_access_tokens.json` under
`workspace/configs/` (atomic tmp+rename, mode 0600). The connector code is validated
against `^[a-z0-9_-]{1,64}$` before it is used as a filename or `mcp.servers.<code>`
key, so an untrusted code cannot escape the configs dir via path traversal.

**Routing (per `connector.set` payload):** the backend sets routing keys in the
payload's `credentials` map:

| `credentials` key | Effect |
|-------------------|--------|
| `mcp_url` | Present → MCP connector: writes `mcp.servers.<code>` (`{type:"http", url, headers.Authorization}`) into `openclaw.json` and restarts the gateway. Absent → credential-only connector (e.g. `gmail`/`google_*`): token stored, **no** `openclaw.json` entry. |
| `mcp_auth_header` | `bearer_access_token` (default) → `Authorization: Bearer <access_token>`; `bearer_api_key` → `Bearer <api_key>` (static-key connectors, e.g. `ahrefs`); `header:<Name>` → raw header `<Name>: <token>` with no Bearer prefix (token prefers `api_key`, falls back to `access_token`) for non-Bearer providers, e.g. a Figma PAT via `header:X-Figma-Token`. A PAT connector relays `auth_type:"pat"` with the token in `api_key`. |

**Fallback table:** for connectors that shipped before the wire carried these keys
(`notion`, `asana`, `linear`, `github`, `ahrefs`), a compiled-in table
supplies the `mcp_url` + header style from the openclaw catalog
(`runtimes/openclaw/mcp.go`). The payload **always wins** — `mcp_url` in the payload
overrides the fallback — so the table is only a migration safety net until the
backend pushes the routing keys.

**Special writers:** `figma-api` uses the hosted Figma MCP allowlist workaround — a
local stdio MCP server (`{command:"node", args:[wrapper], env:{FIGMA_ACCESS_TOKEN}}`)
whose Node wrapper is dropped on disk before the entry is written. Special-writer
codes are excluded (`reserved`) from the generic writer's refresh scan so it never
re-writes them in the wrong (HTTP) shape.

**Expiry:** `expires_in` (seconds from now) is persisted as an absolute
`expires_at`. A credential that never expires — an app password or static API key
(`auth_type:"pat"`, or a payload carrying `api_key` and no `access_token`) — is
stored with `expires_at: 0` when the backend sends no expiry info, instead of the
1-hour OAuth default. Nothing rotates such a credential (the refresh loop skips
`expires_at == 0`), so a synthetic expiry would leave it reading as permanently
expired an hour after it was stored.

**Refresh:** the refresh loop scans the generic writer (globbing
`*_access_tokens.json`) plus each special writer, and proactively rotates any entry
carrying BOTH a `refresh_token` AND `refresh:true` (the backend owns refresh
eligibility via the `refresh` flag) once it is within 10 minutes of expiry, via the
backend `/connector/refresh-token` endpoint.

#### `channel.refresh_config`

Re-applies a channel's canonical config block on an already-onboarded device — for
older customers whose runtime config predates schema additions (e.g. the Slack
`socketMode` block, object-form streaming, `dmPolicy`). Config-only: **no** plugin
install, CLI bootstrap, or pairing. Credentials are read from `config.json` on the
device — they are **NOT** carried in the payload; the device builds the per-channel
`RefreshChannelRequest` from config.json.

**Generic.** Refresh now works for `telegram`, `slack`, and `discord` — previously only
`slack` was wired, and other channels returned `channel_not_supported`. The capability
gate still applies: a channel the **active** runtime can't run returns
`channel_not_supported`.

**Receive:** `{"cmd": "data", "kind": "channel.refresh_config", "data": {"channel": "slack"}}`

**Async flow** — the device acks `configuring` (not `starting`, because the channel was
already set up; this is a re-apply), then runs the write + gateway restart in the
background and publishes a terminal status:

```json
{
  "device": "lamp",
  "type": "data",
  "kind": "channel.refresh_config",
  "status": "configuring | success | failure",
  "error": "<code>",
  "data": { "channel": "slack", "runtime": "2026.6.10" }
}
```

`data.runtime` carries the detected runtime version string (empty when probing failed)
so the backend can correlate refresh outcomes with runtime upgrades. Error codes (in
`error` when `status:"failure"`, mapped from sentinels via `errors.Is`):

| Code | Meaning |
|------|---------|
| `slack_credentials_missing` | config.json has no credentials for the channel being refreshed (kept for wire back-compat; applies to any channel, not just slack) |
| `channel_not_supported` | the active runtime can't run this channel |

#### `skills.install_store`

The MQTT twin of `POST /api/agent/skills/install` (the web UI's Install button).
The device downloads the catalog's `.skill` archive and the **active** runtime
extracts it into its own skills dir via `AgentGateway.InstallSkillArchive`, so
this works on every backend, not just openclaw.

> The `_store` suffix is only because the bare `skills.install` kind is already
> taken by the older, different feature: a whole ROLE bundle written straight
> into `OpenclawConfigDir`.

**Receive:** `{"cmd": "data", "kind": "skills.install_store", "data": {"id": "6a195e59e438b1a9f06299d0"}}`

`id` is the catalog skill id. Optional `name` is a fallback used **only** when the
archive has no single wrapping directory to take the skill name from (catalog
`.skill` bundles normally do, shaped `<name>/SKILL.md`).

**Async** — the download crosses the network, so the device acks `starting` and
publishes a terminal status when done.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.install_store",
  "status": "starting | success | failure",
  "error": "<step>: <message>",
  "data": { "id": "6a19\u2026", "name": "design-critique", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/design-critique" }
}
```

`data.name` is read back from the directory that was actually created, so the
device never has to be told the name. `data.runtime` + `data.path` say which
runtime stored it and where — both differ per backend.

| `failed_step` | Meaning |
|---------------|---------|
| `validate_id` | id empty or contains a path separator (`/ \\ ? #`) |
| `temp_dir` | could not create the staging dir |
| `download` | catalog unreachable, non-200, or the id doesn't exist |
| `archive` | the downloaded file isn't a usable zip / is empty |
| `validate_name` | the skill name derived from the archive isn't a legal slug |
| `unsupported_runtime` | the active runtime has no device-writable skills dir; **nothing was installed** |
| `install` | extract/swap failure |

Installing **replaces** an existing skill of the same name — unlike
`skills.save`, which refuses to overwrite: installing is an explicit instruction,
authoring is not. The extract is staged in `<skill>.new` and swapped in only on
full success (previous version kept at `<skill>.old` until then), so a corrupt
download can never leave a half-installed skill or destroy a working one.

Concurrency: shares one mutex with `skills.install`, `skills.save`,
`skills.upload`, and `skills.uninstall` — all five mutate the same skills tree.
A second one arriving mid-flight fails fast with
`"another skills install is already in progress; try again later"`.

No gateway restart: every backend with a skills dir picks new files up per
session.

#### `skills.files`

The MQTT twin of `GET /api/agent/skills/files`. That endpoint is LAN-only and
admin-gated, so the backend — and through it a mobile app — has no way to inspect
a skill the `skills` uplink advertised. This is that way in.

**Two modes**, because MQTT is not a bulk transport and a whole skill can be
megabytes:

| Receive | Returns |
|---------|---------|
| `{"name":"music"}` | the file **list** — `path` / `size` / `binary` per entry, **no contents** |
| `{"name":"music","path":"music/SKILL.md"}` | that **one file**, contents inlined |

`path` must be the entry path exactly as the list reported it (relative to the
skills root, so it includes the skill dir). A basename or a `..` attempt does not
resolve — lookup is an exact match against the listing, never a filesystem join.
When `path` is supplied, the device reads only that file; a reference-heavy skill
does not delay the reply by loading all of its other files first.

**Synchronous** — reading a skill dir is local disk, so there is no `starting` ack.

List mode:
```json
{
  "device": "lamp", "type": "data", "kind": "skills.files",
  "status": "success",
  "data": { "name": "music", "runtime": "OpenClaw", "files": [
    {"path": "music/SKILL.md", "size": 1204},
    {"path": "music/reference/tempo.md", "size": 380},
    {"path": "music/assets/icon.png", "size": 9001, "binary": true}
  ]}
}
```

Single-file mode returns `data.file` instead: the same entry plus `text`, and
`truncated: true` when the body was cut.

**Size budget.** This uplink returns up to the first **5 KiB** of a requested
text file and flags `truncated: true` when it cut content. The cut never splits a
multi-byte rune, so text stays valid UTF-8. Binary entries carry metadata only,
never bytes.

| `failed_step` | Meaning |
|---------------|---------|
| `validate_name` | skill name isn't a legal slug |
| `not_found` | the requested `path` isn't in that skill |
| `unsupported_runtime` | the active runtime has no device-readable skills dir |
| `read` | skill missing (stale listing) or unreadable |

#### `skills.uninstall`

The MQTT twin of `DELETE /api/agent/skills`. Removes the skill from whichever
skills dir the **active** runtime owns, via `AgentGateway.DeleteSkill`.

**Receive:** `{"cmd": "data", "kind": "skills.uninstall", "data": {"name": "music"}}`

**Synchronous** — removing a directory is local disk, so there is no `starting` ack.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.uninstall",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "music", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/music" }
}
```

**Not idempotent on purpose:** a skill that isn't installed comes back
`failed_step: "not_found"`, not success — so a stale backend view or a
double-send is visible instead of being reported as a deletion that never
happened.

| `failed_step` | Meaning |
|---------------|---------|
| `validate_name` | name isn't a legal slug — a `..` or `/` can never reach outside the skills dir |
| `not_found` | no such skill (or the path isn't a skill directory) |
| `unsupported_runtime` | the active runtime has no device-writable skills dir |
| `remove` | filesystem failure |

Concurrency: shares one mutex with the install/save/upload kinds, so an uninstall can't
interleave with an extract into the same tree.

On Hermes the roots are tried device-owned first, matching the listing's
precedence — so an uninstall removes the skill the `skills` uplink actually
advertised.

After its `success` response, the device immediately publishes its normal MQTT
`info` uplink with the refreshed `skills` inventory. A failure to publish that
best-effort follow-up is logged, but does not change the successful uninstall result.

#### `skills.save`

Writes ONE authored skill into whichever skills dir the **active** agentic runtime
owns. The MQTT twin of `POST /api/agent/skills`: both call
`AgentGateway.SaveSkill`, so a backend-pushed skill lands in exactly the same
place as one written in the web UI's "Write skill" form and honours the same
no-overwrite rule.

**Receive:**
```json
{"cmd": "data", "kind": "skills.save", "data": {
  "name": "weekly-status-report",
  "description": "Summarise the week's activity into a short status report.",
  "instructions": "When the user asks for a weekly status report:\n1. …"
}}
```

`name` + `description` become the SKILL.md YAML front-matter, `instructions` the
markdown body (`skills.RenderSkillMarkdown`). All three are required; each is
trimmed first, so a padded value is accepted rather than rejected.

**Synchronous** — unlike `skills.install` there is no `starting` ack: writing one
file takes milliseconds, so the device publishes a single terminal status.

```json
{
  "device": "lamp",
  "type": "data",
  "kind": "skills.save",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "weekly-status-report", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/weekly-status-report/SKILL.md" }
}
```

`data.runtime` names the runtime that stored it and `data.path` is where it
landed — both differ per backend, so the backend can tell which tree received the
skill. On failure `data.failed_step` carries the same label as the `error` prefix:

| `failed_step` | Meaning |
|---------------|---------|
| `validate_name` | name isn't a `^[a-z0-9_-]+$` slug, or is over 64 chars |
| `already_exists` | a skill of that name is already installed — authoring never overwrites |
| `unsupported_runtime` | the active runtime has no device-writable skills dir; **nothing was stored** |
| `write` | filesystem failure |

The name shape is validated inside `SaveSkill` (via `skills.ValidateSkillName`),
not at the MQTT layer, so this path and the HTTP one can never disagree on what a
legal skill name is.

Concurrency: `skills.save` shares its mutex with `skills.install`,
`skills.upload`, `skills.install_store`, and `skills.uninstall`. A save arriving
mid-install fails fast with
`"a skills install is in progress; try again later"` rather than stalling the MQTT
dispatch loop for the length of a CDN download.

After its `success` response, the device immediately publishes its normal MQTT
`info` uplink with the refreshed `skills` inventory. A failure to publish that
best-effort follow-up is logged, but does not change the successful save result.

No gateway restart: every backend with a skills dir picks new files up per
session.

#### `skills.upload`

Installs a `.md`, `.zip`, or `.skill` file supplied directly in the MQTT
command. This is the MQTT counterpart of `POST /api/agent/skills/upload`: it
uses `AgentGateway.InstallSkillMarkdown` for `.md` and
`AgentGateway.InstallSkillArchive` for `.zip`/`.skill`.

**Receive:**
```json
{"cmd": "data", "kind": "skills.upload", "data": {
  "filename": "daily-note.md",
  "content_base64": "LS0tCm5hbWU6IGRhaWx5LW5vdGUKZGVzY3JpcHRpb246IENhcHR1cmUgYSBkYWlseSBub3RlLgotLS0KCiMgRGFpbHkgbm90ZQ=="
}}
```

`filename` is required and its extension must be `.md`, `.zip`, or `.skill`.
`content_base64` carries the exact file bytes; the decoded file must be at most
16 MiB (so a broker must allow the approximately 21.4 MiB JSON/base64 payload
at the limit). A `.md` must be a valid bare `SKILL.md` with `name` and
`description` in YAML front-matter. Archives follow the same validation and
atomic replace semantics as the HTTP upload flow.

**Synchronous** — decoding and installation run in the MQTT dispatch path, so
there is no `starting` ack.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.upload",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "daily-note", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/daily-note" }
}
```

Installing replaces an existing skill of the same name atomically. On failure
`data.failed_step` is one of:

| `failed_step` | Meaning |
|---------------|---------|
| `validate_front_matter` | content has no usable SKILL.md YAML front-matter |
| `validate_name` | front-matter name is not a legal slug |
| `archive` | archive is empty or lacks a root SKILL.md |
| `unsupported_runtime` | the active runtime has no device-writable skills dir |
| `install` | filesystem failure |

Concurrency: shares the skills mutex with `skills.install`, `skills.install_store`,
`skills.save`, and `skills.uninstall`; a concurrent command fails fast rather
than interleaving writes into the runtime's skills tree.

After its `success` response, the device immediately publishes its normal MQTT
`info` uplink with the refreshed `skills` inventory. A failure to publish that
best-effort follow-up is logged, but does not change the successful upload result.

#### `face.enroll`

Enrolls one face photo for a person. This is the MQTT counterpart of HAL
`POST /face/enroll` (the call the web Face settings make): os-server validates
the payload and forwards it to HAL, which saves the photo under
`users/{label}/`, trains its embeddings, and records the Telegram fields in
`metadata.json`.

**Receive:**
```json
{"cmd": "data", "kind": "face.enroll", "data": {
  "image_base64": "/9j/4AAQSkZJRgABAQ...",
  "label": "alice",
  "telegram_username": "alice_tg",
  "telegram_id": "123456789"
}}
```

| Field | Required | Notes |
|-------|----------|-------|
| `image_base64` | yes | JPEG or PNG bytes, standard base64. A `data:image/...;base64,` prefix is stripped. Decoded size at most 10 MiB |
| `label` | yes | Person name, 1–64 characters after trimming; HAL normalizes it (the folder name is the `label` in the reply) |
| `telegram_username` | no | Stored in the person's metadata |
| `telegram_id` | no | Telegram user ID for DM targeting |

Send one command per photo; to enroll several photos of one person, repeat
the command with the same `label`. Large photos can use the `privacy`
envelope, which fetches `data` over TLS instead of carrying it on the broker.

**Async**: invalid payloads fail at once, without calling HAL. Valid ones ack
`starting`, then `success` or `failure` once HAL finishes. Enrollments run
one at a time on the device because HAL rewrites the person's metadata file
without a lock (`face.remove` shares that lock). A `failure` carries HAL's reason in `error`, for example
`POST /face/enroll returned 400: <detail>` for a photo HAL rejects, or a
`503` when sensing is not running. The image is never logged.

```json
{
  "device": "lamp", "type": "data", "kind": "face.enroll",
  "status": "starting | success | failure",
  "error": "<message>",
  "data": { "status": "ok", "label": "alice", "telegram_username": "alice_tg",
            "telegram_id": "123456789", "photo_path": "/root/.../users/alice/<file>.jpg",
            "enrolled_count": 2 }
}
```

`data` is present only on `success`. `enrolled_count` is the number of people
face recognition knows after this photo (not photos, and not voice-only
people). It is not the same count as `face.owners`' `enrolled_count`.

#### `face.owners` / `face.remove`

`face.owners` lists enrolled people via HAL `GET /face/owners`. It runs
synchronously (a directory scan), so there is no `starting` ack. HAL's shared
`unknown` bucket for unidentified people is dropped from the list, and only
identity fields are returned; the per-day logs HAL also reports are not.

The list holds everyone with a face photo, a voice sample or a
`metadata.json`, so a voice-only person appears with `photo_count: 0`.
`enrolled_count` counts those same people, so it can be higher than the
face-only count that `face.enroll` and `face.remove` return. `photos` are
filenames only; there is no MQTT kind to fetch the image bytes.

```json
{"cmd": "data", "kind": "face.owners"}
```
```json
{
  "kind": "face.owners", "status": "success | failure", "error": "<message>",
  "data": { "enrolled_count": 1, "persons": [
    { "label": "alice", "telegram_username": "alice_tg", "telegram_id": "123456789",
      "photo_count": 2, "photos": ["1711929600000.jpg", "1711929700000.jpg"] } ] }
}
```

`face.remove` deletes the person, not only their face: HAL `POST /face/remove`
removes the whole `users/<label>/` folder, taking the face photos, enrolled
voice samples, `metadata.json` (Telegram) and the mood, wellbeing, posture
and habit history with it. It cannot be undone, so clients should label the
action "remove this person" and confirm first. HAL then retrains every
remaining person from disk, so the time grows with people and photos. It acks `starting`, then `success`
or `failure`, and shares the `face.enroll` lock so a remove never overlaps an
enrollment. `label` is required (1–64 characters); `unknown` is rejected
without calling HAL. An unknown label fails with `POST /face/remove returned
404: person not found`.

```json
{"cmd": "data", "kind": "face.remove", "data": {"label": "alice"}}
```
```json
{
  "kind": "face.remove", "status": "starting | success | failure", "error": "<message>",
  "data": { "status": "ok", "label": "alice", "enrolled_count": 0 }
}
```

#### `voice.*` — Voice settings over MQTT

The `voice.*` kinds give the phone exactly what the web Voice settings
(`VoiceSection.tsx`) do over HTTP:

| Web action | Web HTTP call | MQTT kind |
|------------|---------------|-----------|
| Start Recording (15s on robot) | HAL `POST /speaker/record-enroll` | `voice.enroll` |
| Voice Files list | `voice_samples` of HAL `GET /face/owners` | `voice.owners` |
| Play a sample | HAL `GET /face/file/<label>/voice/<file>` | `voice.file.get` |
| × on a sample | os-server `POST /api/voice/file/remove` | `voice.file.remove` |
| Remove all (face data is preserved) | HAL `POST /speaker/remove` | `voice.remove` |

`label` is trimmed and lowercased, as the web does (1–64 characters).
Enroll and both removals run one at a time on the device.

**`voice.enroll`** records from the **lamp's own microphone** — never phone
audio, because speaker recognition listens through that mic. Recording is
15 seconds, the web's length; the person should stand near the lamp and read
aloud for the whole time (the web shows phrases from
`components/setup/voice-phrases.ts`; any 3-second "get ready" countdown is
the app's own). Invalid payloads fail at once, without calling HAL. A valid
one acks `starting` when recording begins — `data` carries `label` and
`duration_sec` for the countdown — then `success` with the voice profile, or
`failure` with HAL's reason, a few seconds after the recording ends. If
another voice change is running, `starting` waits for it. Repeating the
command for the same `label` adds another sample. Samples carry HAL's
default origin, `web`.

```json
{"cmd": "data", "kind": "voice.enroll", "data": {"label": "alice"}}
```
```json
{
  "kind": "voice.enroll", "status": "starting | success | failure", "error": "<message>",
  "data": { "label": "alice", "duration_sec": 15 }
}
```

On `success`, `data` is the profile:

```json
{ "name": "alice", "display_name": "alice", "enrollment_sources": ["web"],
  "num_samples": 2, "num_extended": 0,
  "enrolled_at": "2026-10-01T07:00:00Z", "updated_at": "2026-10-01T07:05:00Z" }
```

Failure examples: `POST /speaker/record-enroll returned 409: Privacy switch
is on -- microphone recording is blocked`, `503: voice enroll needs a real
microphone` in the simulator, `503: embedding service unavailable — please
try again`.

**`voice.owners`** lists everyone with at least one file in
`users/<label>/voice/`, with the sorted file names — the same `voice_samples`
the web shows. The list includes `.npy` embeddings and other files next to
the WAVs, as the web does. os-server reads the folder directly; HAL is not
called. HAL's `unknown` bucket and hidden folders are skipped.

```json
{"cmd": "data", "kind": "voice.owners"}
```
```json
{
  "kind": "voice.owners", "status": "success | failure", "error": "<message>",
  "data": { "persons": [
    { "label": "alice", "voice_samples": ["sample_web_1711929600_ab12.npy", "sample_web_1711929600_ab12.wav"] } ] }
}
```

**`voice.file.get`** returns one file from `users/<label>/voice/` inline as
base64 for playback. `file` must be one name from `voice_samples`; paths,
symlinks out of the folder and files over 2 MiB fail. A 15-second sample is
about 480 KB.

```json
{"cmd": "data", "kind": "voice.file.get", "data": {"label": "alice", "file": "sample_web_1711929600_ab12.wav"}}
```
```json
{
  "kind": "voice.file.get", "status": "success | failure", "error": "<message>",
  "data": { "label": "alice", "file": "sample_web_1711929600_ab12.wav",
            "content_type": "audio/wav", "size": 480044, "content_base64": "UklGR..." }
}
```

**`voice.file.remove`** deletes one audio sample (`.wav`, `.ogg`, `.mp3`,
`.webm`, `.m4a`) and its `.npy` embedding. It runs the same code as
`POST /api/voice/file/remove` (`system/lib/voicefile`): other files are
refused with `only audio samples can be deleted`, and a missing sample fails
with `file not found`. When the last WAV is gone, HAL `POST /speaker/remove`
drops the whole voice profile and `profile_removed` is `true`. It fails with
`voice enrollment in progress — try again` while a voice change is running.

```json
{"cmd": "data", "kind": "voice.file.remove", "data": {"label": "alice", "file": "sample_web_1711929600_ab12.wav"}}
```
```json
{
  "kind": "voice.file.remove", "status": "success | failure", "error": "<message>",
  "data": { "deleted": "sample_web_1711929600_ab12.wav", "remaining": 1, "profile_removed": false }
}
```

**`voice.remove`** deletes the person's whole voice profile via HAL
`POST /speaker/remove`; face photos, `metadata.json` and history stay. It
acks `starting`, then `success` or `failure`. A person with no voice profile
fails with `POST /speaker/remove returned 404: voice profile not found: <label>`.

```json
{"cmd": "data", "kind": "voice.remove", "data": {"label": "alice"}}
```
```json
{
  "kind": "voice.remove", "status": "starting | success | failure", "error": "<message>",
  "data": { "status": "ok", "name": "alice", "removed": true }
}
```

#### `led.resting.*` — Resting light over MQTT

The `led.resting.*` kinds give the phone the web **Resting light** settings
(`LedSection.tsx`). HAL owns the look and saves the choice in
`/var/lib/hal/resting_led.json`; os-server only validates and relays.

| App action | HAL call | MQTT kind |
|------------|----------|-----------|
| Open the screen | `GET /led/resting` | `led.resting.get` |
| Drag a colour (live on the device) | `POST /led/resting/preview` | `led.resting.preview` |
| Save / toggle off / back to default | `PUT /led/resting` | `led.resting.set` |

```json
{"cmd": "data", "kind": "led.resting.set", "data": {"mode": "custom", "color": [8, 4, 1]}}
{"cmd": "data", "kind": "led.resting.preview", "data": {"color": [20, 0, 10]}}
```

`mode` is `default` (device preset), `off`, or `custom`; `custom` needs `color`
`[r, g, b]` with channels 0-255, and black is stored as `off`. `color` is
ignored for the other modes. `get` and `set` reply `success` with HAL's snapshot:

```json
{"mode": "custom", "color": [8, 4, 1],
 "default": {"effect": "solid", "color": [5, 4, 3]},
 "effective": {"effect": "solid", "color": [8, 4, 1]}}
```

**Live preview.** While the owner drags, send `led.resting.preview`, throttled
to about 5 per second (one every ~200 ms) plus the final position. Each preview
paints immediately without touching the saved choice or the SD card, and does
not reply on success, so a drag does not flood fd_channel. HAL skips the paint
while the device sleeps, speaks, plays music or shows the mic-muted light. If no `set` follows, the strip
returns to the saved look 10 s after the last preview, so an abandoned screen
never leaves a colour behind. Send `led.resting.set` when the owner taps Save;
it cancels any pending preview, stores the choice, clears an earlier explicit
off/colour and repaints. Expect the round trip through the broker to add a few
hundred milliseconds of lag.

Devices without the `light` capability reply `failure` with
`this device has no light`. Invalid input fails before HAL is called.

#### `chat.send` + `chat.event`

Internal `NO_REPLY` handoff sentinels are suppressed from `chat.event`; clients receive Harness progress and the final Harness response instead.

Lets the backend (and through it a phone app) hold the **same conversation the
web monitor's chat holds**. The web chat is two halves — `POST
/api/sensing/event` with `type:"web_chat"` to start a turn (this path forwards
the same way as `type:"mqtt_chat"`), and the SSE stream
`GET /api/agent/events` to render it — and both are device-local behind admin
auth, so a phone on mobile data can reach neither. fa/fd is already a
per-device path that survives NAT, so the pair rides here.

```
mobile ──HTTP──▶ backend ──fa: chat.send──▶ device
mobile ◀── SSE ── backend ◀── fd: chat.event × N ── device
```

**Receive:**
```json
{"cmd": "data", "kind": "chat.send", "data": {
  "message": "what do you see?",
  "images": ["<base64 jpeg>", "…"],   // optional
  "file": {"name": "report.pdf", "mime": "application/pdf", "content": "<base64>"},
  "session_id": "abc123",
  "speak": false
}}
```

`message` is required. `images` is the same base64 the web chat puts in the
sensing event, so a phone attaches a photo the same way.

`file` is for anything that is NOT a photo — a PDF, a CSV. Deliberately its own
field rather than more traffic through `images`: an image goes through the
device's describe-first vision gate, and a document must not (it would fail
there). It lands in `/tmp` with its **real** extension and the turn carries a
`[file: <path> (<name>)]` tag so the agent can open it; `name` is used only for
that extension and the display label, never as the path, so a hostile filename
cannot steer the write. Capped at 10 MB decoded (`agentfile.InboundMaxBytes`),
matching the web composer's own check. `mime` is advisory — the device decides
nothing from it. Both fields may be sent together. `session_id` is opaque
to the device — echoed on the ack and on every event so the backend can fan the
stream to the right client; the device does **not** partition conversation state
by it, since there is one agent and one history, exactly as if two people stood
next to the box. `speak` (default false) makes the device say the reply out loud
too; off by default because a user chatting from another room does not expect it
to start talking — the same reason the web chat suppresses TTS.

**Ack** (immediate, carries only the correlation id — not the reply):
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.send",
  "status": "success | failure",
  "data": { "run_id": "run-…", "session_id": "abc123" }
}
```

**Then a stream** of `chat.event`, device-initiated, one per monitor event of
that run:
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.event", "status": "success",
  "data": { "run_id": "run-…", "session_id": "abc123",
            "event": { "id": "evt-42", "time": "…", "type": "assistant_delta",
                       "summary": "Taking a photo", "runId": "run-…" } }
}
```

`event` is `domain.MonitorEvent` **verbatim** — the same struct the web SSE
stream delivers (`assistant_delta`, `thinking`, `tool_call`, `hw_*`,
`token_usage`, `chat_response`). That is deliberate: a client reuses the web
chat's reducer instead of a second vocabulary that would drift the first time an
event type is added.

Implementation notes that matter to a backend author:

- **Only backend-started runs are mirrored.** The bus carries every turn on the
  device, including spoken ones; a run is tracked when its `chat.send` is
  accepted and untracked at its terminal event, with a 10-minute TTL for turns
  that die without one. Before forwarding the sensing POST, the device temporarily
  captures untracked events so a fast reply cannot be lost before the run ID
  returns. It replays only the exact returned run, in order before live events;
  failed requests discard their capture. This does not mirror unrelated turns.
- **A turn emits MANY `chat_response` events, and only the last one ends it.**
  The runtime pushes `chat_response` repeatedly as a reply streams in — the
  earlier ones carry `state` `"delta"`/`"partial"`, each with a longer prefix of
  the same reply. The run ends only at `state` `"complete"`, `"final"` or
  `"error"` (or at a `no_reply` event, which fires once by nature). A client that
  treats the first `chat_response` as the end shows every reply truncated to its
  first chunk — the exact bug this shipped with. The web monitor's reducer
  applies the same rule (`ChatSection.tsx`), which is the point of sharing one
  event vocabulary.
- **`assistant_delta` is coalesced** into ~250 ms batches. The bus emits one
  delta per model chunk and every fd publish is QoS 1 (a round-trip), so
  forwarding them 1:1 would cost more than generating them. A coalesced event
  carries the whole accumulated run of text, `detail.coalesced: true`, and an
  **empty `id`** so it can't be mistaken for a replay of the chunk it was built
  from. Pending text is always flushed *before* any other event, so a tool chip
  never overtakes the sentence it followed.
- **The turn re-enters the device's own sensing endpoint over loopback** rather
  than calling the AgentGateway directly, so the describe-first vision gate, the
  agent-busy queue fork, the web-chat run marking and the flow logging are the
  same code the web chat exercises. Same reasoning as the Hermes gateway hook
  POSTing to `/api/agent/channel-turn`.
- **It forwards as sensing type `mqtt_chat`, not `web_chat`.** Every gate treats
  the two identically (`sensingmsg.IsChat`: TTS suppressed, no physical wake, no
  opening filler, queued the same way when the agent is busy, same `[user] `
  prefix to the model). The separate type exists so the Flow Monitor's turn
  badge shows **where the message was typed** — 📱 `mqtt_chat` for a phone app
  vs 🖥 `web_chat` for the monitor composer — and so the message ends with
  `[via:mobile]` instead of `[via:web]` (see os-server.md, "Source marker"). With `speak: true` the turn forwards
  as `voice` instead and shows as a voice turn.
- **A dedicated broker client** (`device-<id>-chat`) is held open for the stream.
  The shared `publish` helper opens and closes a connection per message, which is
  right for a one-shot command result and ruinous for dozens of events a turn.
  A distinct client id matters: two connections sharing one id make the broker
  evict the first.
**Files the turn produced — `chat.file.get`**

A turn can only NAME a file it made: "take a photo" ends with an absolute path
like `/root/.openclaw/media/hal-snapshots/snap_*.jpg`. The client spots that path
in the message it is rendering and asks for the file — the MQTT twin of the web
chat fetching `GET /api/agent/file`.

**Client-driven pull, not a device push.** Same shape as the web deliberately:
it works on messages the client ALREADY has (a conversation scrolled back weeks
still resolves its images, which a push covering only the live turn never
would), files nobody opens cost nothing on the device's uplink, and a phone
reuses the web client's path regex and its "on failure just leave the path as
text" behaviour instead of a second implementation.

**Receive:**
```json
{"cmd": "data", "kind": "chat.file.get", "data": {
  "path": "/root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg",
  "session_id": "abc123",
  "run_id": "run-…"
}}
```

`path` is required. `session_id` / `run_id` are opaque to the device and echoed
back untouched so the backend can route the reply to the client that asked; both
are optional, since a file can be requested long after its run ended.

**Reply:**
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.file.get",
  "status": "success",
  "data": {
    "run_id": "run-…", "session_id": "abc123",
    "name": "snap_1785393455291.jpg",
    "path": "/root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg",
    "mime": "image/jpeg", "size": 43165,
    "content": "<base64>"
  }
}
```

- **`path` is client-supplied, so it is hostile input.** What may leave the
  device is decided by `system/agentfile` — the same allow-list
  `GET /api/agent/file` enforces, deliberately one implementation: an allow-list
  with two copies is two chances to widen one by accident. Roots are `media/` +
  `workspace/` per runtime plus `/tmp`; `.json` and `.log` are not served (a
  runtime's config JSON holds gateway tokens); the path must resolve
  (`EvalSymlinks`) inside a root, so `..` and symlink escapes both fail.
- **Every refusal is the same message**, `"file not available"`. Which of wrong
  type / outside the roots / absent it was would tell a prober about the
  device's filesystem; the real reason is logged on the device.
- **`content` is base64**, mirroring how a `chat.send` carries an inbound image,
  so the backend handles one encoding in both directions.
- **Over 2 MB the bytes are dropped, the record is not**: `too_large: true` with
  `content` empty and the real `size`, so a client can say "a 12 MB video"
  instead of showing nothing. That cap is the MQTT inline budget, much tighter
  than the 32 MB `agentfile.MaxBytes` governing a same-network HTTP fetch — this
  is a device uplink shared with every other command, and base64 adds a third on
  top.
- **Cache the bytes backend-side.** Every request re-reads and re-encodes on the
  device, and `/tmp` files do not survive a reboot.

Superseded: `integrations/chat-bridges/autonomous-chat-hook/` forwards backend
chat one-way as `type:"voice"`, so the device speaks the reply and nothing comes
back. It cannot back a chat UI; this pair replaces it for that purpose.

### `harness.voice-mode.get` / `harness.voice-mode.set` — Harness-only voice

Enabling requires HAL to confirm MPR121 configured for the active board. Missing/false support metadata or a failed HAL lookup returns `status:"failure"` and leaves the mode off. Disabling remains allowed. The snapshot includes `supported`; see [Harness integration](harness.md#harness-only-voice-mode).


**Receive on `fa_channel`:**
```json
{"cmd":"data","kind":"harness.voice-mode.get"}
{"cmd":"data","kind":"harness.voice-mode.set","data":{"enabled":true}}
{"cmd":"data","kind":"harness.voice-mode.set","data":{"enabled":false}}
```

`get` needs no `data` and reads the cached shared voice state without querying
Harness. `set` requires an object containing only `enabled`, a JSON boolean;
missing/null/string values and extra fields such as `agentId` are rejected.
This sets an explicit value, never inverts it: repeating the same value does
not change the routing generation or interrupt another capture.

Both commands reply on `fd_channel` with the standard `MQTTDataResponse`
metadata, `type:"data"`, the request's `kind`, and the same snapshot returned
by `GET /api/harness/voice-mode` (device metadata omitted here):
```json
{"type":"data","kind":"harness.voice-mode.set","status":"success","data":{"enabled":true,"supported":true,"generation":1789350000000000,"machineId":"computer-id","agentId":"agent-id","agentName":"Mike","focusRevision":"instance:3","focusAvailable":true}}
```

The snapshot is `{enabled,supported,generation,machineId,agentId,agentName?,focusRevision,focusAvailable,pending?,error?}`.
An unavailable focus can appear as `focusAvailable:false` with a snapshot
`error` even when the command succeeds. Invalid input or an unavailable
controller uses `status:"failure"` with the envelope's `error` field.
There are no unsolicited voice-state MQTT pushes; clients should query `get`
after subscribing, reconnecting, or when refreshing the current state.

MQTT uses the same RAM controller as HTTP, Monitor and HAL. The flag defaults
to off after service restart. Setting it is allowed offline or without focus;
voice delivery still requires the agent selected in the Harness app. These
commands cannot select an agent and do not change typed MQTT/Web chat or skill
routing. Turning the mode off leaves already dispatched work and its output
route intact. `harness.pair.revoke` also disables the mode, matching HTTP
unpair. See [Harness voice mode](harness.md#harness-only-voice-mode).

Authorization uses the existing broker credentials and device command-topic
ACLs; the publisher must be authorized for that device. No new MQTT topic,
credential or Harness transport is introduced.

### `buddy.pair.start` — Issue a Buddy pairing code

**Receive on `fa_channel`:**
```json
{"cmd":"data","kind":"buddy.pair.start","data":{}}
```

`data` is optional and ignored. The synchronous response on `fd_channel` uses
`MQTTDataResponse`, with the standard device/version/id/mac/time metadata plus:
```json
{"type":"data","kind":"buddy.pair.start","status":"success","data":{"code":"123456","expires_in":60}}
```

This calls the same Buddy service as admin-authenticated
`POST /api/buddy/pair/start`. The code is a six-digit string,
valid for 60 seconds and usable once. Issuing a new code through either
HTTP or MQTT replaces any pending code from either transport. Failures use the
standard `status:"failure"` and `error` fields.

MQTT authorization relies on the existing broker credentials and topic ACLs;
the backend must authorize the device owner before publishing the request.
Confirmation still uses `POST /api/buddy/pair/confirm` over LAN; MQTT does not
confirm pairing. Query and change notifications use `buddy.status` below.

### `buddy.pair.revoke` — Revoke Buddy pairing

**Receive on `fa_channel`:**
```json
{"cmd":"data","kind":"buddy.pair.revoke","data":{}}
```

`data` is optional and ignored. The synchronous response on `fd_channel` uses
`MQTTDataResponse`, with the standard device/version/id/mac/time metadata plus:
```json
{"type":"data","kind":"buddy.pair.revoke","status":"success","data":{"revoked":true}}
```

This calls `buddy.Service.Unpair`, the same operation as `DELETE /api/buddy`:
it closes the active WebSocket, clears the current pairing and persisted store,
and invalidates the paired token. Repeating the command when already unpaired
also succeeds. A pending pairing code is not cancelled.

Failures, including an unavailable Buddy service or failure to persist the
removal, use the standard `status:"failure"` and `error` fields.
MQTT authorization relies on the existing broker credentials and topic ACLs;
the backend must authorize the device owner before publishing the request.

### `buddy.status` — Query and observe Buddy state

**Receive on `fa_channel`:**
```json
{"cmd":"data","kind":"buddy.status","data":{}}
```

`data` is optional and ignored. The response and unsolicited state notifications
share the standard `MQTTDataResponse` envelope on `fd_channel` (normal device
metadata omitted here):
```json
{"type":"data","kind":"buddy.status","status":"success","data":{"paired":true,"connected":false,"instance_id":"service-instance-id","revision":1,"buddy_id":"buddy-id","name":"Leo’s Mac","os_version":"15.0","paired_at":"2026-09-08T10:00:00Z"}}
```

An unpaired snapshot contains only `paired:false`, `connected:false`,
`instance_id`, and `revision`. `buddy_id`, `name`, `os_version`, and RFC3339
`paired_at` describe the paired Mac and are omitted when unpaired (empty optional
strings are omitted). No token, pairing code, or fingerprint is included.
`paired` means a saved pairing exists; `connected` means its WebSocket is active.
A disconnected or paused Mac remains paired.

`instance_id` changes on each Buddy service restart. `revision` is an unsigned
64-bit integer, starts at 0, and increases after successful pairing, revocation,
WebSocket connection, or disconnection of the current connection. Compare
revisions only within the same instance: ignore duplicates and lower revisions;
accept a new instance as a fresh state sequence. Queries do not increment it.

The service emits a startup snapshot and asynchronous change snapshots, including
HTTP pair confirmation, HTTP/MQTT revocation, and Buddy self-revocation. The bounded
queue coalesces pending changes to the latest state; this is state synchronization,
not a guaranteed history of every transition. FD publishes use QoS 1 without
retain. Publish failures are logged and dropped without undoing pairing; clients
must query after subscribing, reconnecting, or resuming. Status can arrive before
the pair/revoke command response. An unavailable service returns
`status:"failure"` with `error` for a query.

**Direct mobile MQTT:** use the device's existing broker settings and topic ACLs,
with an app-specific unique client ID (never the device's client ID). Subscribe to
that device's `fd_channel`, wait for SUBACK, then publish `buddy.status` or the
existing `buddy.pair.start` / `buddy.pair.revoke` commands on `fa_channel`. No BFF
code change is required. ACL permissions and broker reachability from mobile
networks must be verified against the real deployment. This supports a connected
foreground app; it does not deliver mobile push notifications when the app is
closed. See the [mobile handoff prompt](vi/buddy-mobile-handoff_vi.md).

### `ota` — not handled

There is no `ota` case in the os-server MQTT router: the message is logged as `unknown command` and ignored. OTA runs on the bootstrap worker's own schedule; to install one component now over MQTT use `data` kind `system.software_update`.

## Code

| File | Role |
|------|------|
| `system/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `system/lib/mqtt/config.go` | Config struct |
| `system/lib/mqtt/options.go` | Connection options |
| `system/lib/mqtt/factory.go` | Factory to create client with unique ID |
| `system/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `system/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `system/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (streams pairing events for WhatsApp) |
| `system/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` / `slack_command` (runtime-aware: forwards Slack HTTP-mode events/slash commands to the local OpenClaw gateway, or drives a hermes turn when the runtime is a `SlackBridge`) |
| `system/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `system/server/device/delivery/mqtt/skills_install_store_handler.go` | Handle `skills.install_store` (async catalog download → `AgentGateway.InstallSkillArchive`) |
| `system/server/device/delivery/mqtt/skills_upload_handler.go` | Handle `skills.upload` (inline SKILL.md → `AgentGateway.InstallSkillMarkdown`) |
| `system/server/device/delivery/mqtt/face_enroll_handler.go` | Handle `face.enroll` (validate, then async `hal.FaceEnroll` → HAL `POST /face/enroll`) |
| `system/server/device/delivery/mqtt/face_owners_handler.go` | Handle `face.owners` (→ HAL `GET /face/owners`, `unknown` bucket dropped) and `face.remove` (async → HAL `POST /face/remove`) |
| `system/server/device/delivery/mqtt/led_resting_handler.go` | Handle `led.resting.get`, `led.resting.set` and `led.resting.preview` (HAL `/led/resting`, capability `light`) |
| `system/server/device/delivery/mqtt/voice_enroll_handler.go` | Handle `voice.enroll` (async → HAL `POST /speaker/record-enroll`, lamp mic, 15s), `voice.owners`, `voice.file.get`, `voice.file.remove` (via `system/lib/voicefile`) and `voice.remove` (async → HAL `POST /speaker/remove`) |
| `system/lib/voicefile/voicefile.go` | List, read and delete voice samples under `users/<label>/voice/`; shared by `POST /api/voice/file/remove` and the `voice.*` kinds |
| `system/server/device/delivery/mqtt/skills_files_handler.go` | Handle `skills.files` (read one installed skill's files: list, or one file's contents) |
| `system/server/device/delivery/mqtt/skills_uninstall_handler.go` | Handle `skills.uninstall` |
| `system/server/device/delivery/mqtt/chat_send_handler.go` | Handle `chat.send` — forwards the turn over loopback to the sensing endpoint |
| `system/server/device/delivery/mqtt/chat_stream.go` | Mirror a chat run's monitor events back as `chat.event` |
| `system/server/device/delivery/mqtt/chat_file_handler.go` | Handle `chat.file.get` — validate a requested path and return the file |
| `system/agentfile/agentfile.go` | Package deciding which device files an agent turn may hand out, plus the path scanner clients use to find them (shared by `chat.file.get` and `GET /api/agent/file`) |
| `system/server/device/delivery/mqtt/skills_save_handler.go` | Handle `skills.save` (synchronous authored-skill write via `AgentGateway.SaveSkill`) |
| `system/server/device/delivery/mqtt/connector_handler.go` | Handle `connector.set.<code>`/`connector.remove.<code>` (async, writer dispatch via `connectorWriterFor`) |
| `system/server/device/delivery/mqtt/connector_writer.go` | `ConnectorWriter` interface + shared `<code>_access_tokens.json` file helpers |
| `system/server/device/delivery/mqtt/connector_writer_generic.go` | Data-driven `connectorWriter`: payload-driven MCP routing, fallback table, path-traversal guard, per-connector token files |
| `system/server/device/delivery/mqtt/mcp_connector_writer.go` | Special stdio MCP writer (`figma-api`): token file + local-wrapper `openclaw.json` MCP entry |
| `system/server/device/delivery/mqtt/connector_refresh.go` | Connector token refresh loop (`/connector/refresh-token`) |
| `system/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `system/server/device/delivery/mqtt/system_ota_handler.go` | Handle `data` kinds `system.ota_versions`/`system.software_update` (+ completion watcher) |
| `system/ota/ota.go` | Shared bootstrap OTA client (versions, updating, force-update, per-target rate limit) behind both the MQTT kinds and `/api/system/ota-*` + `software-update` |
| `system/server/device/delivery/mqtt/channel_refresh_handler.go` | Handle `data` kind `channel.refresh_config` (async re-apply of a channel's config block) |
| `system/server/device/delivery/mqtt/timezone_set_handler.go` | Handle `data` kind `timezone.set` (async apply of the device IANA timezone) |
| `system/device/timezone.go` | `SetTimezone`/`CurrentTimezone`: validate zone, rewrite `/etc/localtime` + `/etc/timezone`, best-effort `timedatectl`, persist config |
| `system/device/channels.go` | `RefreshChannelConfig` (generic per-channel request build + capability gate) |
| `system/agent/channel_reconcile.go` | `ChannelReconcile`: re-applies channels after a runtime switch, records `channels_unsupported` |
| `system/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `system/server/device/delivery/mqtt/claudecode_login_handler.go` | Handle `claudecode_login` / `claudecode_login_code` (claude.ai OAuth login) |
| `runtimes/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `system/domain/device.go` | MQTTMessage, command constants |
| `system/domain/pairing.go` | PairingEvent + status enum |

A silent terminal `NO_REPLY` without a pending Harness handoff is sent as a `chat_response` with `state: final`, empty summary and empty assistant message. The mobile client can terminate its pending indicator without displaying the internal sentinel.

MQTT treats every final Harness response as terminal regardless of wording; handoff suppression is handled by the shared agent handler using the exact run ID.
