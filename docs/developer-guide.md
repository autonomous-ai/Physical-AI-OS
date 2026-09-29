# Intern Developer Guide

This guide walks a Developer Edition owner through the workflows they need on
day one: SSH into the device, ship code changes, watch logs, and hit the HAL
and OS Server APIs. Everything below assumes the Intern v2 Pro (OrangePi
sun60iw2) shipped with SSH open and Codex / Claude Code / OpenCode
pre-installed.

> **Developer Edition only.** You can tell the editions apart by the case
> colour of the unit you bought:
>
> - **Black case → Developer Edition.** SSH is open, the on-device
>   toolchain (Codex / Claude Code / OpenCode) is pre-installed, and every
>   workflow in this guide applies.
> - **Yellow case → OpenClaw edition.** Consumer image, SSH is closed, no
>   developer toolchain. This guide does not apply.
> - **Blue case → Hermes edition.** Consumer image, SSH is closed, no
>   developer toolchain. This guide does not apply.
>
> If your unit is yellow or blue and you need the developer workflows below,
> you'll need a Developer Edition (black) unit — the consumer images cannot
> be unlocked in the field.

The source of truth is the [`autonomous-os` repo](https://github.com/autonomous-ai/autonomous-os).
Every file path in this guide is relative to that repo unless noted otherwise.

---

## 1. Device access

### 1.1 SSH into the device

SSH is exposed **only** on the Developer Edition (black case) — the yellow
OpenClaw and blue Hermes consumer units keep `sshd` disabled and cannot be
reached this way. On the Developer Edition, the SD-card image creates a
single Linux user with sudo:

| | Value |
|---|---|
| SSH user | `orangepi` |
| SSH password | `orangepi` |
| Sudo password | `orangepi` (same as SSH) |

Your device IP is assigned by your router's DHCP. Find it in the router admin
page or on the web dashboard.

```bash
ssh orangepi@<device-ip>
# or
sshpass -p 'orangepi' ssh orangepi@<device-ip>
```

To become root:

```bash
sudo -i
# password: orangepi
```

### 1.2 On-device filesystem layout

| Path | What lives there |
|---|---|
| `/usr/local/bin/os-server` | Go binary — main HTTP API server (Gin, :5000) |
| `/usr/local/bin/bootstrap-server` | Go binary — OTA worker |
| `/opt/hal/` | Python HAL (FastAPI, :5001) — full source lives here |
| `/opt/hal/.env` | HAL environment variables (audio devices, VAD, model URLs, …) |
| `/root/config/config.json` | OS Server config — LLM base URL, API key, channel tokens, Wi-Fi, admin hash |
| `/root/config/bootstrap.json` | Bootstrap OTA metadata URL |
| `/root/.openclaw/` | OpenClaw runtime state — `openclaw.json`, skills, workspace |
| `/etc/systemd/system/os-server.service` | Service unit for os-server |
| `/etc/systemd/system/hal.service` | Service unit for HAL |
| `/etc/systemd/system/bootstrap.service` | Service unit for OTA worker |
| `/etc/nginx/conf.d/<DEVICE_TYPE>.conf` | Nginx (written by `setup.sh`, which also removes `sites-enabled/default`) — proxies `/api/*` to os-server, `/hw/*` to HAL (loopback callers only), serves setup Web UI |

Web (nginx) sits in front on port 80. The `/api/*` prefix is os-server;
os-server itself reverse-proxies `/api/hardware/*` to HAL (admin auth required).
nginx's `/hw/*` → HAL location allows only `127.0.0.1` / `::1`.

### 1.3 Web CLI — browser terminal from the Admin panel

If SSH is inconvenient (locked-down laptop, guest network, no `ssh` client),
the Admin Web UI ships an interactive terminal at:

```
http://<device-ip>/monitor#cli
```

**Admin password (how to sign in).** The Admin panel is gated by the device
admin password — different from the SSH `orangepi` password above. On a
factory-fresh unit the default is the **4-character suffix printed on the
sticker on the underside of the device**: the sticker reads
`Intern-XXXX`, and `XXXX` (case-sensitive) is your default admin password.
Enter it on the Admin login page at `http://<device-ip>/login`.

To rotate the password, sign in and go to
**Settings → General** (`http://<device-ip>/setting#general`); the new value
is written to `/root/config/config.json` and used from the next login. If
you forget the password later, either read it back from
`/root/config/config.json` over SSH or do a soft-reset from the Admin panel
to fall back to the `Intern-XXXX` default.

Once you're signed in, click **Device → CLI** in the sidebar. Each tab
opens a fresh interactive `bash -il` running as **root** on the device,
exactly like SSH — history, aliases, `$PATH`, colours, everything. Up to 6
tabs can be open at once and backgrounded tabs keep their shell alive, so a
build in tab 1 keeps running while you tail a log in tab 2.

- Transport: WebSocket at `/api/system/shell`, admin-auth gated.
- The Web CLI also pre-injects the active agent runtime's env file (e.g.
  `/root/.claudecode/.env` for the Claude Code runtime), so `claude`,
  `codex`, `opencode` etc. inherit the campaign API key and don't prompt
  for login the way a raw SSH shell would.
- Source: `system/server/system/shell.go` (backend PTY + WebSocket) and
  `system/web/src/pages/monitor/CliSection.tsx` (xterm.js frontend).

Use SSH for scripted / long-running work (rsync, tmux sessions that must
survive a browser close). Use the Web CLI for quick "poke at the device"
sessions or when you're on a machine without a proper terminal.

---

## 2. Logs

Every long-running component is a systemd unit — use `journalctl -u <name>`.

```bash
# Live tail — most common
sudo journalctl -u os-server -f
sudo journalctl -u hal -f
sudo journalctl -u openclaw -f
sudo journalctl -u bootstrap -f

# Last 100 lines
sudo journalctl -u os-server --no-pager -n 100

# Errors only
sudo journalctl -u hal -p warning --no-pager -n 100

# By time window
sudo journalctl -u os-server --since "10 minutes ago"
sudo journalctl -u hal --since "2026-07-08 10:00" --until "2026-07-08 11:00"
```

Grep tip: the log lines carry a `component=<name>` tag. Filter by that when
tailing os-server:

```bash
sudo journalctl -u os-server -f | grep component=mqtt
```

---

## 3. Configuration

### 3.1 LLM / STT / TTS (OS Server — `/root/config/config.json`)

The main config for the LLM brain plus messaging channels lives here. Edit
directly, then restart os-server for the change to apply.

Key fields:

```jsonc
{
  "llm_base_url": "https://api.openai.com/v1",
  "llm_api_key": "sk-…",
  "llm_model": "gpt-4o-mini",

  "stt_base_url": "https://api.openai.com/v1",
  "stt_api_key": "sk-…",
  "stt_language": "en",

  "tts_base_url": "https://api.openai.com/v1",
  "tts_api_key": "sk-…",
  "tts_provider": "openai",
  "tts_voice": "alloy",

  "channel": "telegram",
  "telegram_bot_token": "…",
  "telegram_user_id": "…"
}
```

To use a self-hosted OpenAI-compatible endpoint (Ollama / vLLM / LM Studio), set
`llm_base_url` to `http://<host>:<port>/v1`. Any API key placeholder works if
the endpoint doesn't check auth.

```bash
sudo nano /root/config/config.json
sudo systemctl restart os-server
```

### 3.2 HAL runtime env (`/opt/hal/.env`)

HAL reads its runtime tuning (audio devices, VAD thresholds, realtime voice
config, camera settings) from `/opt/hal/.env`. A curated set of presets lives
in the repo under `hal/env-presets/`.

```bash
sudo nano /opt/hal/.env
sudo systemctl restart hal
```

Common knobs:

| Var | What it does |
|---|---|
| `HAL_AUDIO_INPUT_ALSA` | Mic ALSA device (`plug:device_micro2` on Intern v2 Pro) |
| `HAL_AUDIO_OUTPUT_ALSA` | Speaker ALSA device |
| `HAL_VAD_THRESHOLD` | Voice-activity RMS floor (lower = more sensitive) |
| `HAL_SILENCE_TIMEOUT` | Seconds of silence before committing a turn (fallback clock, no STT final) |
| `HAL_ENDPOINT_SILENCE_S` | Seconds of silence before committing a turn once STT emitted a final |
| `HAL_REALTIME_TURN_DETECTION` | `on` = server-side VAD (Gemini/OpenAI), `off` = client VAD; ignored by GPT-Live (no VAD on the Live API) |
| `HAL_GEMINI_LIVE_MODEL` | Gemini realtime model id |
| `HAL_OPENAI_REALTIME_MODEL` | OpenAI realtime model id |
| `HAL_GPTLIVE_MODEL` | GPT-Live model id (`gpt-live-1`, OpenAI `/v1/live/sessions`) |
| `DEVICE_TYPE` | `intern-v2` — hardware kit gate for drivers like the mic mute switch |

---

## 4. Building + shipping code

Do the build on your Mac / dev machine — the device only runs the artifacts.
The Go binary cross-compiles to `linux/arm64`; the HAL is plain Python, no
build step needed.

> **Disable the OTA bootstrap first.** The `bootstrap` service polls the
> Intern team's OTA channel and, if it finds a newer official build, will
> reinstall os-server / HAL / web on top of whatever you just pushed —
> silently wiping your custom binary at the next tick. Before you push a
> single custom build to a device, turn bootstrap off so your changes
> stick:
>
> ```bash
> sudo systemctl disable --now bootstrap
> ```
>
> See [§5](#5-controlling-the-ota-bootstrap) for the full enable / disable /
> re-enable flow. Only turn bootstrap back on when you want to go back to
> tracking the official fleet builds — at that point the next tick will
> overwrite your custom changes with the current OTA version.

### 4.1 Go — os-server + bootstrap

```bash
# From the repo root
make os-build            # → system/os-server
make os-build-bootstrap  # → system/bootstrap-server
```

Push the binary and restart:

```bash
sshpass -p 'orangepi' scp system/os-server orangepi@<ip>:/tmp/os-server-new
sshpass -p 'orangepi' ssh orangepi@<ip> "echo orangepi | sudo -S bash -c '
  cp /usr/local/bin/os-server /usr/local/bin/os-server.bak.\$(date +%s)
  mv /tmp/os-server-new /usr/local/bin/os-server
  chmod 755 /usr/local/bin/os-server
  systemctl restart os-server
'"
```

### 4.2 Python — HAL

Sync individual files, or the whole directory:

```bash
# One file
sshpass -p 'orangepi' scp hal/drivers/privacy_button.py orangepi@<ip>:/tmp/
sshpass -p 'orangepi' ssh orangepi@<ip> "echo orangepi | sudo -S bash -c '
  mv /tmp/privacy_button.py /opt/hal/drivers/privacy_button.py
  chown root:root /opt/hal/drivers/privacy_button.py
  systemctl restart hal
'"

# Whole HAL tree (destructive — replaces /opt/hal contents)
sshpass -p 'orangepi' rsync -avz --delete hal/ orangepi@<ip>:/tmp/hal/
sshpass -p 'orangepi' ssh orangepi@<ip> "echo orangepi | sudo -S bash -c '
  rsync -avz --delete /tmp/hal/ /opt/hal/
  systemctl restart hal
'"
```

### 4.3 Web (Setup + Admin UI)

The React SPA lives in `system/web/`. Nginx serves the built assets from
`/usr/share/nginx/html/setup/`.

```bash
# Build
cd system/web && npm install && npm run build   # → dist/

# Push (from repo root)
cd system/web/dist && zip -qr /tmp/setup-web.zip .
sshpass -p 'orangepi' scp /tmp/setup-web.zip orangepi@<ip>:/tmp/
sshpass -p 'orangepi' ssh orangepi@<ip> "echo orangepi | sudo -S bash -c '
  find /usr/share/nginx/html/setup -mindepth 1 -not -name VERSION -exec rm -rf {} + 2>/dev/null || true
  unzip -o -q /tmp/setup-web.zip -d /usr/share/nginx/html/setup
  chown -R root:root /usr/share/nginx/html/setup
'"
```

Nginx serves the SPA — no restart needed after replacing files.

---

## 5. Controlling the OTA bootstrap

The `bootstrap` service polls `https://cdn.autonomous.ai/os/ota/metadata.json`
and applies any component (os-server, HAL, web, bootstrap itself, Buddy, the
device profile, and the active agent runtime's CLI) whose
official version is newer than what's on disk. Left running, it **will**
overwrite any custom os-server / HAL / web build you pushed with `scp`,
usually within a minute — the Intern team ships OTAs on their own cadence
and there is no way to opt-out per-file. If you're doing custom development,
disable bootstrap first (§4 callout) and only re-enable it when you want
your device to snap back to the official fleet build.

```bash
# Stop for one session (survives until next boot)
sudo systemctl stop bootstrap

# Persist across reboots — nothing OTA will hit this device
sudo systemctl disable --now bootstrap

# Re-enable when you're done developing
sudo systemctl enable --now bootstrap
```

To confirm nothing else is overwriting your binary, tail the bootstrap log
before and after your push:

```bash
sudo journalctl -u bootstrap -f
```

---

## 6. HAL API — TTS / STT / Mic / Speaker / LED

Full HAL routes live in `hal/routes/`. Reach them from the device with
`curl http://127.0.0.1:5001/…`, or from your laptop / web dashboard via
os-server's admin-authenticated proxy at `http://<device-ip>/api/hardware/…`
(nginx `/api/` → os-server → HAL).

Every endpoint returns `{"status": 1, "data": <payload>, "message": null}` on
success, `{"status": 0, "data": null, "message": "…"}` on failure.

Two ways to call anything below:

- **Over HTTP** (from anywhere — a Python script, a curl, another service).
  Every subsection ships a `curl` you can paste.
- **In-process from HAL Python code** — import the route function
  directly and call it with the pydantic request. Useful when you're
  writing a new HAL driver or route.

Line numbers below are anchored to the current `main` — if they drift, grep
for the decorator (e.g. `@router.post("/voice/speak"`) in the same file.

### 6.1 TTS — make the speaker talk

| What | Where |
|---|---|
| Route module | `hal/routes/voice.py` |
| `POST /voice/speak` — one-shot say (interrupts current speech) | `voice.py:310` — `def speak_text(req: SpeakRequest)` |
| `POST /voice/speak-queue` — say after current speech finishes | `voice.py:463` — `def speak_queue_text(req: SpeakRequest)` |
| `POST /tts/stop` — cut off the currently-playing sentence | `voice.py:515` — `def stop_tts()` |
| `SpeakRequest` schema (text, voice, provider, interruptible, cached…) | `hal/models.py:327` |

```bash
# Fire-and-forget say
curl -X POST http://127.0.0.1:5001/voice/speak \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello from the device"}'

# Queue behind any in-flight speech (e.g. multi-turn narration)
curl -X POST http://127.0.0.1:5001/voice/speak-queue \
  -H 'Content-Type: application/json' \
  -d '{"text": "…and that concludes the update."}'

# Interrupt whatever is playing right now
curl -X POST http://127.0.0.1:5001/tts/stop
```

Call it from inside HAL Python:

```python
from hal.models import SpeakRequest
from hal.routes.voice import speak_text

speak_text(SpeakRequest(text="Hello", voice="Rachel", interruptible=False))
```

### 6.2 STT — read what the mic heard

STT runs continuously as part of the voice pipeline — you generally don't
call it to transcribe an ad-hoc WAV. Instead:

- Live turn stream (VAD → STT → LLM → TTS): subscribe to the OS Server
  flow monitor SSE endpoint — see `docs/flow-monitor.md`.
- Current voice-pipeline state (STT provider, language, VAD, is speaking): 
  `GET /voice/status` — `hal/routes/voice.py:682` (`def voice_status()`).
- Live mic RMS level (SSE, useful for a "am I hearing you?" indicator):
  `GET /voice/mic-level` — `hal/routes/voice.py:605`
  (`async def mic_level_stream(...)`).

```bash
curl http://127.0.0.1:5001/voice/status
curl -N http://127.0.0.1:5001/voice/mic-level   # SSE stream
```

The STT providers (`deepgram.py`, `autonomous.py`, behind the abstract
`provider.py` interface) live under `hal/drivers/voice/stt/` if you need to
plug a new one in.

### 6.3 Mic — mute / unmute mid-conversation

| What | Where |
|---|---|
| Route module | `hal/routes/voice.py` |
| `POST /voice/mute` — stops feeding audio into VAD/STT, ignores mic | `voice.py:551` — `def mute_mic()` |
| `POST /voice/unmute` — re-arms the mic | `voice.py:570` — `def unmute_mic()` |
| Slide-switch driver (physical mic mute on PD1, Intern v2 Pro) | `hal/drivers/privacy_button.py` — calls `mute_mic()` / `unmute_mic()` on GPIO edge |

```bash
curl -X POST http://127.0.0.1:5001/voice/mute
curl -X POST http://127.0.0.1:5001/voice/unmute
```

The physical slide switch on the top of the Intern v2 Pro just calls those
two endpoints internally — that's the whole pattern for any hardware
button you want to bolt on.

### 6.4 Speaker — mute / play WAV / stop

| What | Where |
|---|---|
| Route module | `hal/routes/music.py` |
| `POST /audio/play` — play a music track or WAV path | `music.py:218` — `def audio_play(req: MusicPlayRequest)` |
| `POST /audio/stop` — stop current playback | `music.py:278` — `def audio_stop()` |
| `POST /speaker/mute` — mute the speaker at the ALSA level (TTS still runs but silent) | `music.py:288` — `def mute_speaker()` |
| `POST /speaker/unmute` — restore | `music.py:312` — `def unmute_speaker()` |
| `GET /audio/status` — what's playing right now | `music.py:327` — `def audio_status()` |

```bash
# Play a local WAV (playing music via the LLM's music skill goes here too)
curl -X POST http://127.0.0.1:5001/audio/play \
  -H 'Content-Type: application/json' \
  -d '{"path": "/opt/hal/assets/ok.wav"}'

# Stop
curl -X POST http://127.0.0.1:5001/audio/stop

# Speaker mute / unmute — TTS keeps running, output is silenced
curl -X POST http://127.0.0.1:5001/speaker/mute
curl -X POST http://127.0.0.1:5001/speaker/unmute
```

`/speaker/mute` is the right knob if you want the device to still "think
out loud" internally but not disturb the room — TTS advances, LEDs still
pulse, only the audio out is muted. Use `/tts/stop` if you want it to
actually shut up.

### 6.5 LED

| What | Where |
|---|---|
| Route module | `hal/routes/led.py` |
| `POST /led/solid` — solid RGB colour | `led.py:127` — `def set_led_solid(req: LEDSolidRequest)` |
| `POST /led/paint` — per-pixel colours | `led.py:176` — `def set_led_paint(req: LEDPaintRequest)` |
| `POST /led/effect` — named animation (breathing, blink, pulse, rainbow…) | `led.py:246` — `def start_led_effect(req: LEDEffectRequest)` |
| `POST /led/status` — status-cue overlay (booting, listening, error) | `led.py:340` — `def set_led_status(req: LEDStatusRequest)` |
| `POST /led/off` | `led.py:212` — `def turn_off_leds(...)` |
| `POST /led/effect/stop` — stop the running animation, keep last frame | `led.py:411` — `def stop_led_effect()` |
| `POST /led/restore` — hand strip back to user's saved state (after a transient overlay) | `led.py:363` — `def restore_led()` |
| `GET /led` — current state | `led.py:54` — `def get_led_state()` |
| `GET /led/color` — ring state: brightest pixel + `uniform` flag, read across every pixel | `led.py` — `def get_led_color()` |
| Named status → colour/effect table (STATUS_LED_PRESETS) | `hal/presets.py:350` |

Concrete calls, all copy-pasteable:

```bash
# 1. Solid colour — cold blue
curl -X POST http://127.0.0.1:5001/led/solid \
  -H 'Content-Type: application/json' \
  -d '{"color": [0, 135, 255]}'

# 2. Per-pixel paint — colour each of the N LEDs individually
#    (useful for progress bars, level meters, tests)
curl -X POST http://127.0.0.1:5001/led/paint \
  -H 'Content-Type: application/json' \
  -d '{"colors": [[255,0,0],[0,255,0],[0,0,255],[255,255,0]]}'

# 3. Named animation with tint + speed
curl -X POST http://127.0.0.1:5001/led/effect \
  -H 'Content-Type: application/json' \
  -d '{"effect": "breathing", "color": [0, 255, 0], "speed": 1.0}'

# Other effects: blink, pulse, rainbow, wave, chase, sparkle, …
curl -X POST http://127.0.0.1:5001/led/effect \
  -H 'Content-Type: application/json' \
  -d '{"effect": "rainbow", "speed": 0.5}'

# 4. Status overlay — HAL picks the colour/effect from STATUS_LED_PRESETS
#    ("booting", "listening", "wifi_connecting", "hardware", "agent_down", …)
curl -X POST http://127.0.0.1:5001/led/status \
  -H 'Content-Type: application/json' \
  -d '{"state": "listening"}'

# 5. Stop the animation but keep the last frame lit
curl -X POST http://127.0.0.1:5001/led/effect/stop

# 6. Restore the user's saved LED after a transient overlay
curl -X POST http://127.0.0.1:5001/led/restore

# 7. Off
curl -X POST http://127.0.0.1:5001/led/off

# 8. Read current state
curl http://127.0.0.1:5001/led
curl http://127.0.0.1:5001/led/color
```

**Where the LED is actually driven from in this repo** (good places to
copy patterns from):

| Caller | File | What it does |
|---|---|---|
| Status-cue overlay service | `system/statusled/service.go:71` — `func (s *Service) Set(state State)` | Priority-stacked overlay: booting / ota / error / connectivity / hal_down / agent_down / hardware / wifi_connecting. Highest priority wins; on `Clear` the strip is restored. |
| State constants (booting, wifi_connecting, agent_down, …) | `system/statusled/service.go:19-26` | The exact set of states you can pass to `/led/status`. |
| Wi-Fi connecting blue-blink | `system/device/setup.go:145` — `s.statusLED.Set(statusled.StateWifiConnecting)` | Fires the blue blink while the STA join is happening during setup. |
| HAL health watchdog | `system/healthwatch/service.go:94,107,136` | Sets `StateHALDown` / `StateHardware` when HAL or a driver stops responding. |
| Agent-runtime health | `runtimes/openclaw/service_ws.go:43`, `runtimes/claudecode/client.go:60`, `runtimes/codex/client.go:65`, `runtimes/opencode/client.go:63`, `runtimes/picoclaw/client.go:54`, `runtimes/hermes/health.go:141` | Every agent runtime sets `StateAgentDown` when its socket drops so the user sees the LED go red without having to check logs. |
| Ambient "breathing" idle behaviour | `system/ambient/service.go` | Drives soft colour drift + breathing while there's no interaction. Uses `/led/effect` with `transient=true` so it doesn't clobber the user's saved state. |
| LLM skill / tool-call bridge (`[HW:/led/…:{...}]`) | `system/server/agent/delivery/http/handler_hw.go` | When an OpenClaw / Hermes / Claude Code skill emits e.g. `[HW:/led/solid:{"color":[255,0,0]}]`, this handler forwards it to HAL. This is how any skill turns the LED red without wiring plumbing itself. |
| Go HAL client (helpers you'd call from an os-server service) | `system/lib/hal/client.go:138-200` — `SetEffect`, `StopEffect`, `SetStatus`, `RestoreLED`, `GetColor` | Fire-and-forget wrappers around the endpoints above. New Go services should use these instead of hand-rolling HTTP. |

Full LED preset catalogue and rendering rules (colours, priorities, per-preset
behaviour): `robots/lamp/docs/led-control.md`.

### 6.6 Other useful HAL surfaces

| Domain | Source file | Highlights |
|---|---|---|
| Servo (Lamp only) | `hal/routes/servo.py` | `/servo/aim`, `/servo/track`, `/servo/play` |
| Emotion | `hal/routes/emotion.py` | `POST /emotion` — coordinated servo + LED; `/emotion/presets`, `/emotion/status` |
| Scene | `hal/routes/scene.py` | `GET /scene` (list), `POST /scene` (activate by name), `POST /scene/off` |
| Camera | `hal/routes/camera.py` | `/camera/snapshot`, `/camera/stream`, `/camera/enable`, `/camera/disable` |
| Display (Lamp) | `hal/routes/display.py` | `/display/eyes`, `/display/info`, `/display/eyes-mode`, `/display/snapshot` |
| Face recognition | `hal/routes/sensing.py` | `/face/enroll`, `/face/owners`, `/face/remove`, `/face/reset` |

---

## 7. MQTT — add a new event kind

The device speaks MQTT for admin commands (fired from the web dashboard) and
status reports. Every command is an envelope shaped like
`{"cmd": "data", "kind": "<kind>", "data": {…}}`.

### 7.1 Sending an MQTT command from the backend

The web admin fires commands via the BFF endpoint
`POST /device/{id}/send-message?listen=true`, which relays over MQTT. Web-side
client wrapper (in the autonomous.ai frontend repo) is `StandToEarnApi.sendDeviceMessage`.

Example payload:

```json
{ "cmd": "data", "kind": "device.soft_reset", "data": {} }
```

### 7.2 Receiving on the device — add a new handler

Adding a new MQTT command is three edits inside `system/`:

1. **Declare the kind constant** in `system/domain/device.go`:

   ```go
   const (
       KindDeviceRename    = "device.rename"
       KindDeviceSoftReset = "device.soft_reset"   // ← your new one
       // …
   )
   ```

2. **Write the handler** — one file under
   `system/server/device/delivery/mqtt/`. See
   `device_soft_reset_handler.go` and `device_rename_handler.go` for reference
   patterns (envelope unmarshalling, ack via `publishDataResult`,
   goroutine-off-callback for long work).

3. **Wire it into the dispatcher** — one case in the switch in
   `system/server/device/delivery/mqtt/handler.go` `dispatchData()`:

   ```go
   case domain.KindDeviceSoftReset:
       return h.handleDeviceSoftReset(env)
   ```

Rebuild os-server (§4.1) and the new command is live.

The full MQTT protocol reference (topics, envelope shape, privacy vs inline
delivery, ack model) lives in `docs/mqtt.md`.

---

## 8. Web UI — Setup and Admin

The React SPA in `system/web/` covers both flows:

| Route | What it is | Key files |
|---|---|---|
| `/setup` | First-boot provisioning — Wi-Fi + admin password + LLM/channel prefill | `src/pages/setup/Setup.tsx`, `src/components/setup/*` |
| `/login` | Admin login (bcrypt-checked against `config.admin_password_hash`) | `src/pages/Login.tsx` |
| `/monitor` | Post-login dashboard — flow monitor, live device state | `src/pages/monitor/*` |
| `/setting` | Admin settings — rotate password, edit config, channel management | `src/pages/settings/*` |

Setup form deep-links: the companion app pushes `?llm_api_key=…&tele_token=…`
into the Setup page so operators don't retype secrets. Params are captured at
module load and stripped from the URL for privacy (see `hooks/setup/useSetupUrlParams.ts`).

---

## 9. Repo map — what lives where

| Path | Owner |
|---|---|
| `system/cmd/os-server/main.go` | OS Server entry point |
| `system/cmd/bootstrap/main.go` | OTA worker entry point |
| `system/server/` | HTTP handlers (Gin), organised by domain |
| `system/` | Business services — agent, device, network, openclaw, hermes, mqtt, statusled, healthwatch, … |
| `system/domain/` | Shared Go types |
| `hal/` | Python HAL — drivers, routes, board profiles |
| `hal/drivers/` | Hardware drivers (rgb, motors, voice, sensing, gpio_button, privacy_button, …) |
| `hal/routes/` | FastAPI routes (voice, led, camera, emotion, scene, music, servo, …) |
| `robots/contract/` | Frozen HAL capability ABI (`capabilities.md`, `ROBOT-SPEC.md`) |
| `skills/` | 25 built-in skills — agents auto-discover these, including `skill-creator` for authoring and evaluating new skills |
| `robots/` | Per-device declarations (`intern-v2/`, `lamp/`, `unitree-go2w/`) with `ROBOT.md` + `SOUL.md` + `SAFETY.md` |
| `scripts/provision/` | Image build + on-device setup scripts |
| `scripts/release/` | OTA upload scripts (`upload-os-server.sh`, `upload-hal.sh`, `upload-web.sh`) |
| `docs/` | Docs (this file lives here) |

---

## 10. Further reading

- [`docs/overview.md`](overview.md) — the layered stack, top to bottom
- [`docs/os-server.md`](os-server.md) — routes, wire graph, run flags
- [`docs/setup-flow.md`](setup-flow.md) — provisioning, AP→STA handoff, admin password default
- [`docs/mqtt.md`](mqtt.md) — full MQTT command reference
- [`docs/realtime-voice.md`](realtime-voice.md) — Gemini Live / OpenAI Realtime / GPT-Live pipelines
- [`docs/bootstrap-ota.md`](bootstrap-ota.md) — OTA metadata format + promotion model
- [`docs/agentic/hermes.md`](agentic/hermes.md), [`docs/agentic/picoclaw.md`](agentic/picoclaw.md), [`docs/agentic/codex.md`](agentic/codex.md), [`docs/agentic/opencode.md`](agentic/opencode.md) — per-runtime protocol

---

## 11. Getting help & buying a Developer Edition device

**Where to buy.** The Developer Edition (black case, SSH open, on-device
toolchain pre-installed — the unit this whole guide targets) is sold on
the official store:

- https://www.autonomous.ai/intern?product_url_sequence_code=7496_8

Make sure you pick the **Developer Edition** SKU at checkout — the yellow
OpenClaw and blue Hermes editions on the same product page ship with SSH
closed and cannot be turned into a developer unit later.

**Support:**

- **GitHub**: [`autonomous-ai/autonomous-os`](https://github.com/autonomous-ai/autonomous-os) — issues + discussions
