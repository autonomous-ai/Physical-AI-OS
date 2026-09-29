# Simulator — the whole stack on a laptop

Run the **same binaries that ship to the board** on a developer machine: HAL with
virtual (or host) peripherals, os-server, the agent bridge, and the web UI.

There is no build tag and no second code path. Only the device-absolute paths
move, through one env var each (`system/lib/syspath` on the Go side, `HAL_*` on
the Python side). **Unset env = the board's behaviour, byte for byte** — which is
what makes the tested binary the shipped binary.

> **Source of truth:** this doc reflects the code. If they disagree, the code wins.

### What this is not

- **Not a physics simulator.** No mass, inertia, collision or torque. A pose that
  would jam on a real body succeeds here.
- **Not a claim that rendered geometry is correct.** The repo carries no
  calibrated joint hierarchy, pivots or CAD zero offsets.
- **Not `robots/sim`.** That is a separate minimal *body* (`motion` + `system`
  only) used to prove HAL mounts exactly what a `ROBOT.md` declares — see
  `robots/sim/ROBOT.md`. The laptop simulator boots the full **lamp** body.

---

# Setup

## Step 1 — Check prerequisites

| Need | Check with | If missing |
|---|---|---|
| `codex` CLI | `codex --version` | Install it yourself — nothing here installs it |
| codex auth — **either** mode | `ls ~/.codex/auth.json` | Optional. `auth.json` present = subscription mode (`codex login`), and codex uses its built-in provider; absent = api-key mode, and codex runs on `llm_api_key` + `llm_base_url` from config.json. `runtimes/codex/presync.sh` picks the mode on every boot, so deleting `auth.json` flips it |
| `ffmpeg` | `ffmpeg -version` | Needed for music playback |
| `uv` | `uv --version` | Builds HAL's `hal/.venv`. `make sim` creates it on first run and re-syncs it whenever `hal/uv.lock` or `hal/pyproject.toml` moves ahead of it |
| `node` + `npm` | `node --version` | Needed for `make web-dev` only |

**Only `codex` works off-device.** Other runtimes have no `*-dev` target.

## Step 2 — Create the config

```bash
make os-dev-config
```

It copies the template on a fresh machine, fills in everything a laptop run can
infer (`llm_base_url`, `llm_model`, `admin_password_hash`), leaves an existing
file's values alone, and prints the path to edit. `make os-dev` runs the same
seed step, so this is only for doing it up front.

## Step 3 — Fill in the config

```bash
nano ~/.autonomous-os/config/config.json   # only llm_api_key is left to you
```

### Required

| Key | Value | Missing → |
|---|---|---|
`os-dev-seed.sh` writes all three, so a fresh config already reads:

```json
"llm_api_key": "autonomous_api_key",
"llm_model": "Auto-AI",
"llm_base_url": "https://campaign-api.autonomous.ai/api/v1/ai/v1"
```

| Key | Value | Missing → |
|---|---|---|
| `llm_api_key` | **Ask the team for a real key** — `autonomous_api_key` is a placeholder that keeps the UI reachable, not a working credential | No TTS, no STT, no Gemini Live, no image description. Left *empty* it is worse: `adminAuthMiddleware` (`system/server/middleware.go`) falls back to this key as the bearer, answers 503 without it, and `AuthGate` reads 503 as "never set up" — the web UI bounces to `/setup`, a wizard that cannot finish off-device (no `mac`, no `iw`) |
| `llm_base_url` | Defaulted to the shared gateway. Set another OpenAI-compatible base to override | Nothing — it is defaulted |
| `llm_model` | Defaulted to `Auto-AI` | Nothing — it is defaulted |

### Required only for the web UI (`make web-dev`)

| Key | Value |
|---|---|
| `admin_password_hash` | Leave empty — `os-dev-seed.sh` fills in the bcrypt hash of `autonomous` when it is blank. Set your own hash to override (see *Setting your own password*) |
| `session_secret` | Leave empty — os-server writes a random one on first login (`system/server/session/session.go`) |

### Optional

| Key | Default | Effect |
|---|---|---|
| `deepgram_api_key` | `""` | Set it to use Deepgram STT instead of `AutonomousSTT` |
| `realtime.enabled` | `true` | `false` → every turn goes to the main agent (slower, still works) |
| `wakeword` | `true` | `false` → always-listening, no wake phrase needed |
| `tts_voice` | `Rachel` | ElevenLabs voice |
| `stt_language` | `en` | STT language |
| `timezone` | `Asia/Ho_Chi_Minh` | IANA zone |

### Set automatically — do not edit

`device_type` · `agent_runtime` · `set_up_completed` — rewritten by
`os-dev-seed.sh` on every run.

### Leave empty

`device_id` · `mqtt_endpoint` · `mqtt_username` · `mqtt_password` ·
`fa_channel` · `fd_channel` — the backend uplink is off — see *The backend uplink is off*.

`telegram_bot_token` — if you copied a real device's config, **blank this**. One
bot token cannot have two pollers; the laptop would steal the device's messages.

## Step 4 — Run

Four terminals, in this order.

```bash
make sim SIM_MEDIA=host           # 1. HAL          :5001
make codex-dev CODEX_PORT=18892   # 2. agent bridge :18892
make os-dev    CODEX_PORT=18892   # 3. os-server    :5000
make web-dev                      # 4. web UI       :5173   (optional)
```

| # | Wait for this line before starting the next |
|---|---|
| 1 | `Simulation mode enabled for device 'lamp' (media=host)` |
| 2 | `[codex-gatewayd] listening on ws://127.0.0.1:18892/codex/ws/` |
| 3 | `Codex connected` |
| 4 | `VITE … ready` |

Rules:

- Start `make sim` first — os-server waits up to 120s on HAL's `/health`.
- `codex-dev` and `os-dev` must use the **same** `CODEX_PORT`. The default `18792`
  collides with an openclaw gateway if one is installed.
- macOS asks for **Microphone** and **Camera** on the first `SIM_MEDIA=host` run.
  Grant them, then re-run `make sim`.
- Drop `SIM_MEDIA=host` if you do not need voice — the stack still runs, silently.
- `make os-dev-all CODEX_PORT=18892` replaces terminals 2 and 3: it starts the
  bridge in the background and os-server in the foreground, and kills the bridge
  when os-server exits. The bridge's output then goes only to
  `$OS_STATE_DIR/codex-gatewayd.log` — use the two separate targets to watch it live.

## Step 5 — Verify

```bash
# 1. os-server is up
curl -s :5000/api/health/live

# 2. HAL is up and using host devices
curl -s :5001/simulator/state | jq   # expect media:"host", media_reasons:{}

# 3. The os-server → HAL path works (no LLM, ~50ms)
curl -s -X POST :5000/api/sensing/event -H 'Content-Type: application/json' \
  -d '{"type":"voice_command","message":"turn on the light"}'

# 4. The agent answers (~15s) — reply appears in terminal 3
curl -s -X POST :5000/api/sensing/event -H 'Content-Type: application/json' \
  -d '{"type":"voice_command","message":"introduce yourself"}'

# 5. Speak into the mic: say "hey lamp, what time is it"
grep '\[turn\] route=' ~/.autonomous-sim/log/server.log | tail
```

Open:

- `http://127.0.0.1:5001/simulator` — the 3D body
- `http://localhost:5173/monitor` — Flow Monitor (**`localhost`**, not `127.0.0.1`)

## Step 6 — Name your device (optional)

Wake words follow the agent name. Write:

```bash
echo '- **Name:** Lumi' > ~/.codex/workspace/IDENTITY.md
```

Picked up within 5 seconds, no restart. Now `hey lumi` works, alongside
`hey lamp` and `hey autonomous`.

---

# Reference

## What each `make` target does

| Target | Does | Does **not** |
|---|---|---|
| `make sim` | Boots HAL on the lamp body with virtual (or host) peripherals | — |
| `make hal-install` | `uv sync` — an **exact** reconcile of `hal/.venv`, dropping anything absent from `uv.lock`. `make sim` syncs by itself with `--inexact`, which keeps a hand-installed pytest | Does not install the `dev` extra (pyflakes); add `--extra dev` by hand |
| `make codex-dev` | **Only** runs `os-server codex-gatewayd`: a loopback WebSocket listener that spawns one `codex exec` per turn | No onboarding, no presync, **no skill sync** |
| `make os-dev` | Three things in sequence: `os-dev-build` (compile), `os-dev-seed` (prepare the state dir), then run the API — which also does all agent provisioning: `presync.sh`, seeding `AGENTS.md`/`SOUL.md`/`KNOWLEDGE.md`/`HEARTBEAT.md`, `downloadSkills()`, the skill watcher | — |
| `make web-dev` | Runs Vite in nginx's place (os-server serves no HTML) | — |

If the workspace is empty or the agent has no persona, look at **`os-dev`**, not
`codex-dev`.

### `os-dev-seed` vs `os-dev`

`os-dev-seed` is a prerequisite target of `os-dev`, not a separate step you
normally run. It touches only the state dir, never starts a process:

| It does | It does not |
|---|---|
| Creates `config.json` from `config.example.json` when it is missing | Overwrite an existing `config.json` — the values in it are yours |
| Rewrites `device_type`, `agent_runtime`, `set_up_completed` in it | Touch any other key |
| Fills a blank `admin_password_hash` with the hash of `autonomous` | Overwrite a hash that is already set |
| Fills a blank `llm_base_url` with `https://campaign-api.autonomous.ai/api/v1/ai/v1` and a blank `llm_model` with `Auto-AI` | Overwrite a value that is already set |
| Warns about an empty `llm_api_key` | — |
| Seeds `config/bootstrap.json` (once) so skills can download | — |
| Backs up an existing `config.toml` to `config.toml.pre-os-dev` (once) | — |

`make os-dev-seed` runs it alone — useful to re-check the state dir without
booting the server.

## Config file locations

| | Path |
|---|---|
| Template (in the repo) | `scripts/dev/config.example.json` |
| Live config | `$OS_STATE_DIR/config/config.json` — default `~/.autonomous-os/config/config.json` |
| OTA metadata (auto-seeded) | `$OS_STATE_DIR/config/bootstrap.json` |
| Agent workspace | `$CODEX_HOME/workspace/` |

One config file serves **both** HAL and os-server, the same role
`/root/config/config.json` plays on a board: os-server resolves
`config/config.json` relative to its cwd (`make os-dev` cds into the state dir),
HAL reads `OS_CONFIG_PATH`.

The seed copies the template **once**. Later runs only rewrite `device_type`,
`agent_runtime` and `set_up_completed`; your edits survive.

## Which credential buys what

| You want | You need |
|---|---|
| The agent answers text | nothing — codex uses its own login |
| Skills install automatically | nothing — the CDN objects are public |
| The device speaks (TTS) | `llm_api_key` + `llm_base_url` |
| Voice input (STT) | `llm_api_key`, or `deepgram_api_key` |
| Sub-second voice (Gemini Live) | `llm_api_key` + `realtime.enabled` |
| The agent sees images | `llm_api_key` |

One key covers all of it: the realtime key falls back to `llm_api_key` and its
endpoint to `llm_base_url` + `/ws/gemini` (`hal/config.py`), so no separate Google
credential is involved.

## Media modes

`SIM_MEDIA` decides whether HAL opens the developer machine's peripherals.

| | `virtual` (default) | `host` |
|---|---|---|
| Camera | synthetic calibration scene, deterministic | the Mac's webcam (AVFoundation) |
| Mic / speaker | virtual device ids, never passed to `sounddevice` | real devices via PortAudio |
| **Voice pipeline** | **inert stub** | **the real one** — VAD, Silero, STT, realtime agent, wake word, `[turn] route=…` dispatch |
| Music | full pipeline into a null sink | audible |
| Permissions | none needed | macOS asks for Microphone + Camera |
| Suitable for | tests, CI, offline work | manual end-to-end checks |

The gate is `state.simulation_audio` (`hal/server.py`), not the raw media string.
That matters: `_sim_audio_probe()` runs first and flips the flag **back to
virtual** when a device is missing, busy or permission-denied, so a refused
microphone lands on the stub with a logged `[sim-media]` reason instead of a real
pipeline reading a dead device. `routes/voice.py` keys off the same flag, so the
boot path and `POST /voice/start` can never disagree.

Host mode never hard-fails. `GET /simulator/state` reports the outcome per
subsystem:

```json
{"media":"host","media_camera":"host","media_audio":"host","media_reasons":{}}
```

`media` is `"host"` only when both are; `media_reasons` carries the actionable
why for each downgrade. On macOS the permissions live under
System Settings → Privacy & Security → Camera / Microphone and must be granted to
the terminal app running HAL. Enumeration is not permission — the device list is
populated either way and only the first real read fails, which is why HAL probes
at boot rather than failing a turn later.

---

## Wake word and naming the agent

With `"wakeword": true` you must address the device. Prefixes are
`hello` `hey` `hi` `alo` `okay` `ok` `wake up`, combined with:

- the **agent name** from `$CODEX_HOME/workspace/IDENTITY.md`
- the **device type** (`lamp`)
- the permanent alias **`autonomous`**

To name the agent, write:

```markdown
- **Name:** Lumi
```

into `$CODEX_HOME/workspace/IDENTITY.md`. `WatchIdentity` polls that file every 5
seconds and pushes fresh wake words to HAL — **no restart needed**. HAL merges
them with the permanent set, so `hey lumi`, `hey lamp` and `hey autonomous` all
work. With no `IDENTITY.md`, the name falls back to `device_type`.

A wake phrase is accepted at the **start or end of any sentence** in the turn.
Mid-sentence is rejected — a device name inside a sentence is people talking
*about* the device.

After an authorized turn a follow-up window opens
(`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, code default **20s**; the lamp image's `robots/lamp/rootfs/opt/hal/.env` also sets 20)
and **resets after every authorized turn**, so a continuous conversation never
needs the name again. Set `"wakeword": false` for always-listening.

---

## The two web UIs

They are different things and easy to confuse.

### `http://127.0.0.1:5001/simulator` — the body

Served by HAL. Only when `HAL_SIMULATE=1` **and** the body is `lamp`; otherwise
404. An orbitable view of the lamp's rigged GLB — five joint nodes named exactly
as HAL names them (`base_yaw`, `base_pitch`, `elbow_pitch`, `wrist_pitch`,
`wrist_roll`) — driven by live joint values, plus recording playback and LED
controls that call the same `/servo/*` and `/led/*` endpoints a skill calls.

Drag to orbit, scroll to zoom, double-click to reset. The camera opens on the lamp's face,
three quarters round from dead-on, aims at the centre of whatever geometry
loaded and backs off far enough to frame it, so the head does
not run off the canvas on a wide window. Zoom spans 0.3x-4x of that distance
through a 38 deg lens, and elevation runs from level with the floor to just
short of straight overhead. The body stands on a
1.6 m ground grid (50 mm cells, brighter every 250 mm, fading to a circular
edge) so pan and lean are readable against something; the canvas renders at the
display's pixel ratio, capped at 2x. The LED ring reads
`/simulator/pixels` (the strip buffer itself), not `/led/color`, because the
latter reports an effect's static base colour and would render breathing, candle
and rainbow as one dead colour.

The rendered pose is a visual response to live joint values, **not** a claim that
it is physically correct.

### `http://localhost:5173/monitor` — the product UI

Flow Monitor, Settings, Logs — the same SPA that runs on a board.

os-server serves **no HTML**: on a device nginx serves `web/dist` and proxies
`/api` to `:5000` (and `/hw/` to HAL on `127.0.0.1:5001`). `make web-dev` puts Vite in nginx's place, with
`LAMP_PROXY` naming the device the SPA talks to. A `.env` in `system/web/` still
wins (vite.config reads it before `process.env`), so pointing at a real Pi is
unchanged.

> **Vite binds `[::1]` only** — `127.0.0.1:5173` is refused and looks like the
> server never started. Use `localhost`.

Log in with the password whose bcrypt hash is in `admin_password_hash` — off
device that is `autonomous` unless you set your own, since `os-dev-seed.sh`
fills a blank hash in.
Alternatively append `?llm_api_key=<the key in config.json>` — but note this
misses on the *first* load of a fresh tab (`api.ts` initialises its token from
`sessionStorage` at module load, and `AuthGate`'s effect runs before `App`'s
`useBearerFromQuery`), so navigate to `/monitor` a second time in the same tab.

There is no third way: an empty `admin_password_hash` is not an open door — it
is why `os-dev-seed.sh` defaults one rather than leaving it blank.
`VerifyAdminPassword` (`system/device/config_update.go`) refuses outright when
no hash is set, and it refuses off-device exactly as it does on a board — the
simulator runs the shipped binary, so it has no auth bypass to enable.

#### Setting your own password

A config copied from a device carries that device's hash. To log in with a
password of your own instead, generate a cost-10 hash and put it in
`admin_password_hash`:

```bash
htpasswd -bnBC 10 "" 'your-password' | tr -d ':\n'
```

`htpasswd` ships with macOS. It emits a `$2y$` prefix where Go's `bcrypt` writes
`$2a$`; both verify against the same algorithm and `CompareHashAndPassword`
accepts either, so paste the output as-is. The leading `""` is the username
field, which os-server does not use — `tr` strips it along with the newline.

---

## What is simulated

Boot on the lamp body mounts 12 routes and skips one:

```
mounted=['audio','bluetooth','camera','emotion','led','music','scene',
         'sensing','servo','speaker','system','voice']  skipped=['buddy']  failed_required=[]
```

### Runs exactly as on a board

- the whole route surface — still decided by `ROBOT.md`, not by the simulator
- safety gates — `motion.max_speed`, the whole-body stability gate
  (`motion.max_cog_offset_mm`), the LED brightness ceiling. `/servo/move` rejects
  unknown joint names, but no bound clamps a joint angle to a range.
- the declaration-driven mount plan
- emotion presets, scene, music, bluetooth, system routes
- `TrackerService`, volume, user/stranger stores

### Substituted

| Subsystem | Board | Simulator |
|---|---|---|
| Servo | `feetech` over a serial bus | `MockMotionService` — joints are floats in a dict; moves interpolate over their commanded `duration` and block until arrival; `aim`/`nudge` obey `SAFETY.md`'s `max_speed`; CSV recordings replay through the same 30 Hz stretch-and-resample timing, so an animation takes the same wall-clock time — and through the same `resample_recording`, so the stability gate refuses in `make sim` exactly the clip a physical body refuses |
| LED | WS2812 over SPI | `_MemoryStrip` — a real pixel buffer in RAM, effects run for real, still funnelled through the same brightness clamp |
| Camera | `opencv`/V4L2 | `virtual` (synthetic scene) or `host` (the Mac's webcam) |
| Sensing | `SensingService` + face recognition | `VirtualSensingService` — keeps presence state and the route contract; no perception-service calls, no face identity |
| Board profile | reads the device tree | the inert `sim` profile |
| Music output | `aplay` (ALSA) or `paplay` (PulseAudio) | ffmpeg's AudioToolbox output device on macOS |
| Voice enroll capture | `arecord` over the ALSA alias | PortAudio (`sounddevice`) on the same input device the voice pipeline records with; the WAV carries its own rate and the recognizer resamples |
| GELF logging | ships to the log server | off |
| GPIO button / touchpad | real | skipped (`_board_id != "sim"` gate) |

`HAL_BOARD=sim` is refused without `HAL_SIMULATE=1` — HAL will not boot physical
drivers on a virtual board.

---

## Environment reference

### Go side — `system/lib/syspath`

Every accessor keeps its production default and is overridden by exactly one env
var. `make os-dev` and `make codex-dev` share one set so the bridge and its
client can never disagree.

| Env | Board default | Purpose |
|---|---|---|
| `CODEX_HOME` | `/root/.codex` | Root of every codex path, in the client *and* the gatewayd |
| `CODEX_PORT` | `18792` | Bridge listener + `WSURL` |
| `CODEX_WS_TOKEN` | `autonomous_codex_token` | Bearer token between os-server and the bridge |
| `OS_AGENT_HOME` | `/root` | Root a Telegram coding session resolves `~` against |
| `OS_AGENT_STATE_PATH` | `/root/config/agent_state.json` | Runtime-switch history |
| `OS_BOOTSTRAP_CONFIG` | `/root/config/bootstrap.json` | Source of `metadata_url` — the skill-zip base |
| `OS_LOG_FILE` | `/var/log/os-server.log` | os-server's rotating log |
| `OS_BACKEND_UPLINK` | `on` | Backend ping + MQTT. `make os-dev` sets `off` |
| `OS_HAL_LOG_FILE` | `/var/log/hal/server.log` | Where the web UI reads HAL's log from |
| `OS_AGENT_BRIDGE_LOG` | `""` (use the journal) | File to read the bridge from when there is no systemd |
| `DEVICES_DIR` | `/opt/devices` | Root of `robots/<type>/` |

Only `"off"` disables `OS_BACKEND_UPLINK` — any other value, including a typo,
keeps it on, so a fleet upgrade can never silently take devices off their uplink.

### HAL side

HAL already read all of these; the `sim` target is what points them somewhere a
laptop can write. Their failures surface far from their cause — the TTS cache one
appeared as `POST /voice/speak 409` with the real `PermissionError` buried in a
background thread's traceback — which is why they are set as a block.

| Env | Board default | `make sim` |
|---|---|---|
| `OS_CONFIG_PATH` | `/root/config/config.json` | `$OS_STATE_DIR/config/config.json` — the file shared with os-server |
| `HAL_SNAPSHOT_DIR` | `/root/.<runtime>/media/hal-snapshots` | `$CODEX_HOME/media/hal-snapshots` — must sit under the agent's own home, or it cannot read the frame back and os-server cannot serve its thumbnail |
| `HAL_CODEX_WORKSPACE_DIR` | `/root/.codex/workspace` | `$CODEX_HOME/workspace` — the realtime agent's `memory.jsonl` derives from it |
| `HAL_LOG_DIR` | `/var/log/hal` | `$SIM_STATE_DIR/log` |
| `HAL_SNAPSHOT_PERSIST_DIR` | `/var/lib/hal/snapshots` | `$SIM_STATE_DIR/snapshots` |
| `HAL_TTS_CACHE_DIR` | `/var/lib/hal/tts_cache` | `$SIM_STATE_DIR/tts_cache` |
| `HAL_CALIBRATION_DIR` | `/var/lib/hal/calibration/…` | `$SIM_STATE_DIR/calibration/…` |
| `HAL_USER_BEARING_PATH` | `/var/lib/hal/user_bearing.json` | `$SIM_STATE_DIR/user_bearing.json` |
| `HAL_FACE_HEIGHT_PATH` | `/var/lib/hal/face_height.json` | `$SIM_STATE_DIR/face_height.json` |
| `HAL_USERS_DIR` / `HAL_STRANGERS_DIR` / `HAL_VOICE_STRANGERS_DIR` | `/root/local/…` | `$SIM_STATE_DIR/…` |
| `HAL_BT_STATE_DIR` / `HAL_VOLUME_STATE_PATH` | `/var/lib/hal`, `…/.volume` | `$SIM_STATE_DIR/…` |
| `HAL_DL_STALL_LOG` | `/root/local/dl_ws_stall.log` | `$SIM_STATE_DIR/dl_ws_stall.log` |
| `HAL_SIMULATE` / `HAL_BOARD` / `HAL_SIM_MEDIA` | unset | `1` / `sim` / `$(SIM_MEDIA)` |

Two device defaults are deliberately left alone: `/root/local/models` and
`/opt/piper` are read-only model paths. Absent on a laptop, the feature that
needs them simply stays off.

### Makefile knobs

| Knob | Default |
|---|---|
| `DEVICE_TYPE` | `lamp` |
| `SIM_MEDIA` | `virtual` |
| `SIM_STATE_DIR` | `~/.autonomous-sim` |
| `OS_STATE_DIR` | `~/.autonomous-os` |
| `OS_AGENT_RUNTIME` | `codex` |
| `CODEX_HOME` | `$HOME/.codex` |
| `CODEX_PORT` | `18792` |
| `CODEX_BIN` | first `codex` on `PATH` |
| `OS_BACKEND_UPLINK` | `off` |
| `HAL_PORT` | `5001` |
| `LAMP_PROXY` | `http://127.0.0.1:5000` |

Every path in the environment tables above is a knob too — each is its own `?=`
variable, so one can move without touching the rest:

```bash
make sim HAL_TTS_CACHE_DIR=/Volumes/sd/tts     # one path
make sim SIM_STATE_DIR=~/work/sim-a            # all of them
```

An exported shell variable wins over the default for the same reason. Two pairs
stay coupled through a variable rather than a repeated literal, so overriding one
half moves the other: `OS_HAL_LOG_FILE` derives from `HAL_LOG_DIR` (HAL writes,
os-server reads it for the web UI's HAL tab), and `codex-dev`'s `tee` target is
`OS_AGENT_BRIDGE_LOG` itself (bridge writes, os-server reads it for the Agent tab).

---

## The backend uplink is off

`make os-dev` sets `OS_BACKEND_UPLINK=off`, which stops two things: the 15s
status ping (`system/device/status_reporter.go`) and the MQTT command channel
(`system/server/mqtt.go`).

This is not a simulator preference — it is a safety interlock. **The backend
identifies a device by its `llm_api_key`, not by `device_id`**, so a laptop
holding a copy of a device's config is indistinguishable from that device.
Measured with both running:

- the ping overwrote the real device's `local_ip`, `mac`, `version` and `skills`
  every 15 seconds
- the MQTT client ids — derived from the `device_id` the backend hands back —
  collided, and the two clients evicted each other from the broker about once a
  second, indefinitely

Config edits cannot avoid this: blanking `device_id` gets the same id back, and
blanking `mqtt_endpoint` is undone by the ping response, which writes the broker
config to disk and triggers `restartMQTT`. Blocking the subscriber is enough for
the whole chain — both publisher clients connect lazily, and the chat stream only
publishes for runs a backend `chat.send` created.

Nothing a developer needs goes through the uplink: the web UI, Flow Monitor,
voice pipeline, agent, skills and Telegram (polled directly by the device) are
all local. What is lost is remote control: phone-app chat, remote OTA, remote
skill installs, and the Slack webhook proxy.

`make os-dev OS_BACKEND_UPLINK=on` re-enables it. Only do that when the laptop is
not carrying a live device's credentials.

---

## Logs

| Web UI tab | Off-device |
|---|---|
| HAL | ✅ `$SIM_STATE_DIR/log/server.log` |
| OS | ✅ `$OS_STATE_DIR/os-server.log` |
| Agent / Agent Service | ✅ `$OS_STATE_DIR/codex-gatewayd.log` — `make codex-dev` tees the bridge to a file (`2>&1`, because Go's `slog` writes to stderr) since a laptop has no journal |
| Bootstrap | ❌ the OTA worker has no off-device target |
| Claude Desktop Buddy | ❌ a separate Mac app that writes no log here |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `codex CLI not found on PATH` | The agent is not installed — see *Step 1* |
| `curl :5000` returns a binary plist or 403 | os-server is not running — macOS AirPlay Receiver answers on `*:5000`. Turn it off (System Settings → General → AirDrop & Handoff) or change `httpPort` |
| `listen failed: address already in use` | `CODEX_PORT` left at `18792`, which an openclaw gateway holds |
| `bad handshake (status 404)` | `codex-dev` and `os-dev` disagree on `CODEX_PORT` |
| `dial 127.0.0.1:5001: connection refused` | HAL is not up yet |
| `127.0.0.1:5173` refuses the connection | Vite binds `[::1]` — use `localhost:5173` |
| Speaking does nothing | Missing `SIM_MEDIA=host`, or macOS denied the microphone. Check `media_reasons` in `/simulator/state`. The variable is `SIM_MEDIA` — `make sim MEDIA=host` sets a variable nothing reads and make says nothing, so confirm the boot line says `media=host` |
| Voice enroll returns 503 `needs a real microphone` | `SIM_MEDIA=virtual` — enroll refuses to open the host mic in a mode that promises not to |
| Voice enroll returns 400 `vad_removed_all` | The clip held no speech. Read the phrases aloud, closer to the mic, for the full countdown |
| STT hears the wrong name | `flux-general-en` mis-hears proper nouns; "hi lamp" has come back as "hi lance", and a miss drops the whole turn silently. Wake terms are sent as STT boost terms, but say the name clearly |
| `POST /voice/speak 409` + `PermissionError: /var/lib/hal` | `HAL_TTS_CACHE_DIR` not set — an old `sim` target |
| "Sorry, I can't play that right now" | Music: macOS has no `aplay`/`paplay`. Needs `ffmpeg` on `PATH` for the AudioToolbox route |
| `POST /audio/volume` returns 503 | Expected — macOS has no ALSA mixer |
| Agent calls itself "Codex", no persona | `$CODEX_HOME/workspace` must hold `AGENTS.md`, `SOUL.md`, `KNOWLEDGE.md`, `HEARTBEAT.md`. These come from `os-dev`, not `codex-dev` |
| Empty workspace, no `seeded file` log | `set_up_completed` is not true, so the startup sequence never ran |
| `skill download skipped: no ota_metadata_url` | `config/bootstrap.json` missing |


## What does not work off-device

| | Why |
|---|---|
| Backend uplink | Off by design — see *The backend uplink is off* |
| Bootstrap / OTA | `bootstrap-server` has no off-device target |
| Claude Desktop Buddy logs | Separate Mac app |
| Any runtime other than codex | No `*-dev` target; never exercised on a laptop |
| Real `SensingService` (face, motion perception) | Its module imports the Feetech driver, which pulls `lerobot`. `VirtualSensingService` stands in |
| Face recognition, speech emotion, speaker ID | Need the perception service / embedding endpoint |
| GPIO button, touchpad, mic button | No hardware; skipped at boot |
| SoC temperature | `temp_c: null`, so the thermal-throttle path is untestable |

---

## Why the board is unaffected

Every off-device behaviour sits behind an env var whose unset value is the
board's, or behind a platform check a board never satisfies:

| Change | Board |
|---|---|
| Voice-pipeline gate | `state.simulation_audio` is False when `HAL_SIMULATE` is unset — identical to the `_simulation` check it replaced |
| macOS music route | Guarded by `sys.platform == "darwin"` |
| `record-enroll` capture backend | `shutil.which("arecord")` finds it on the board, so the PortAudio fallback never runs |
| `BackendUplink()` | Defaults on; the var appears in no unit file, rootfs or image script |
| Every `syspath` accessor | Unset env returns the literal it replaced |
| Makefile, docs | Not shipped to the device |

Guarded by tests that assert the *board* contract, not the laptop one:
`system/lib/syspath/syspath_test.go` (`TestDeviceDefaults`, `TestAgentRuntimeHome`,
`TestBackendUplink`), `system/server/logs_source_test.go`
(`TestResolveLogSourceBoardDefaults`) and `runtimes/codex/paths_default_test.go`.

---

## Code reference

- `Makefile` — `── Off-device run (laptop) ──`, `SIM_HAL_ENV`, `sim`, `web-dev`
- `scripts/dev/os-dev-seed.sh` — config.json + bootstrap.json seeding
- `system/lib/syspath/syspath.go` — every Go-side env override
- `system/server/logs.go` — `resolveLogSource`
- `runtimes/codex/gatewayd/gatewayd.go` — the bridge `codex-dev` runs
- `runtimes/codex/onboarding.go` — what `os-dev` seeds
- `hal/server.py` — simulation gates, mount plan, `_sim_audio_probe`
- `hal/drivers/motors/mock_service.py` — the mock body
- `hal/drivers/camera/host_capture_device.py` — the host webcam backend
- `hal/static/lamp-simulator.html` — the 3D page
- `robots/sim/ROBOT.md` — the minimal contract-test body

Related: [overview.md](overview.md) · [os-server.md](os-server.md) ·
[agentic/codex.md](agentic/codex.md) · [realtime-voice.md](realtime-voice.md)
