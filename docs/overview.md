# Architecture Overview — Autonomous

## 3-Layer Architecture

```
Agentic Runtime (AI/LLM) → OS Server (Go, :5000) → HAL (Python, :5001) → Hardware
```

| Layer | Language | Port | Role |
|-------|----------|------|------|
| Agentic Runtime | Go | WS | AI brain, LLM, SKILL.md, memory, channels |
| OS Server | Go | 5000 | System (network, OTA, MQTT, reset), sensing event routing, local intent |
| HAL | Python | 5001 | Hardware drivers (servo, LED, camera, audio, display), FastAPI |

## Computer integration and voice

- **[Harness](https://github.com/autonomous-ai/openharness):** `harness-use` delegates coding and research to agents on a paired computer over a direct authenticated LAN connection. Questions and results return through voice or chat. Harness-only voice routes manual tap-to-record captures to the agent focused in Harness. See [Harness](harness.md).
- **Mac computer use:** `computer-use` lets the device agent observe, act and verify through Autonomous Buddy, using the bundled Cua Driver and screenshot support. Buddy executes desktop actions; the device agent owns reasoning and completion. Harness and Buddy keep separate pairing and connections. See [Computer use](../integrations/companions/autonomous-buddy/docs/computer-use.md).
- **Jev:** the OS-managed Hermes plugin selects and preloads an installed skill before the first model call, with normal skill discovery as fallback. This plugin is enabled in the current build; the separate voice-intent fallback is on by default, while Buddy's action-suggestion endpoint is enabled but experimental. Selection does not execute actions or grant permission. See [Hermes preloading](agentic/hermes.md#13-optional-jev-skill-preloading), [intent fallback](os-server.md#jev-intent-fallback), and [Buddy suggestions](os-server.md#buddy-computer-use-feedback).
- **Realtime voice:** HAL supports Gemini Live (default model `gemini-3.8-live`), OpenAI Realtime, GPT-Live and Pipecat v1. Pipecat orchestrates STT → an OpenAI-compatible LLM (default `qwen/qwen3.6-35b-a3b`) → HAL TTS on the device; model calls still use remote services. Normal voice can answer directly or delegate to the main runtime.
- **Smart Turn:** local ONNX inference supplements silence detection in shared non-Live hands-free capture; Pipecat Live uses its own Silero VAD + Smart Turn pipeline. Silence fallbacks bound the wait when inference is unavailable. Manual Harness capture ends on the user's tap. The optional `pipecat` extra is included in Lamp/Pi/OrangePi setup and excluded from Reachy because of ONNX dependency conflicts. See [Realtime voice](realtime-voice.md).

## Project Directory

```
system/
├── cmd/os-server/main.go              — OS Server entry point
├── cmd/bootstrap/main.go         — OTA bootstrap worker
├── server/
│   ├── server.go                 — Gin HTTP server, route setup
│   ├── config/                   — JSON config management
│   ├── health/delivery/http/     — Health, system info, dashboard
│   ├── network/delivery/http/    — WiFi scan, connect
│   ├── device/delivery/          — Setup (HTTP + MQTT handlers)
│   ├── sensing/delivery/http/    — Sensing event → intent match / agent gateway
│   └── openclaw/delivery/sse/    — Agent gateway status, SSE events
├── agent/  ambient/  beclient/  buddy/  device/  healthwatch/
├── intent/  monitor/  network/  skills/  statusled/  vision/
│                                 — System managers, one folder per diagram chip
├── lib/mqtt/                     — MQTT client (Eclipse Paho autopaho)
├── domain/                       — Shared structs
├── bootstrap/                    — OTA worker
└── web/                          — React 19 + Vite + Tailwind CSS 4 SPA

runtimes/                   — Swappable brains: openclaw/ hermes/ picoclaw/ codex/ claudecode/ opencode/

hal/
├── server.py                     — FastAPI server
├── config.py                     — Runtime constants (sensing thresholds, timeouts, URLs)
├── board/                        — Device profiles, board pin maps, and overlays
├── drivers/                      — Hardware services (camera, motors, RGB, sensing, voice, display)
├── routes/                       — FastAPI capability route modules
├── safety/                       — Parsed safety policy and deterministic gates
├── realtime/                     — Realtime voice agent and context managers
├── server_support/               — Shared HTTP/security support
└── pyproject.toml                — Python dependencies (opencv-python, onnxruntime)

robots/                          — Per-device configs and overlays
  contract/                       — Shared API contracts (+ cts/ compliance suite)
skills/                           — Built-in SKILL.md files for agent runtime, including
                                    skill-creator for owner-authored skills
integrations/                     — Off-device: companions/, chat-bridges/, perception-service/
```

Mechanical GPIO button wiring is owned by each device in
`robots/lamp/gpio_button.json` and `robots/intern-v2/gpio_button.json`.
HAL resolves the device directory through `DEVICES_DIR` and `DEVICE_TYPE`.
Each detected-board entry accepts the original flat `chip`, `line`,
`debounce_ns` configuration or a `buttons` list with named inputs.
`load_button_configs` supplies one shared `hal/drivers/gpio_button.py` instance
per input; HAL stops every instance on cleanup. `load_button_config` remains
available for callers that need only the first input (the primary button in Lamp). Lamp on OrangePi uses
`primary` at pin 37 / PD4 / gpiochip0 line 100 (`behavior: "standard"`) and
`factory_reset` at pin 35 / PD3 / gpiochip0 line 99
(`behavior: "factory_reset"`, `hold_s: 5`). The reset input ignores taps and
holds below 5 s; holding at least 5 s arms the shared solid-red LED feedback,
then releasing calls the shared factory-reset action. It does not reset while
held or invoke sleep, shutdown, or single/triple-click actions.
Device configuration takes priority; a missing file or board entry falls back
to exactly one existing `button` default in `hal/board/boards.json`.
Intern v2's JSON remains unchanged. Changing wiring requires updating the
selected device's JSON and restarting HAL; moving physical wires is not
detected automatically. Malformed configuration, duplicate input names, and
duplicate chip/line pairs are rejected before GPIO is claimed. Simulation
skips hardware buttons.

The privacy switch always controls microphone mute. Optional boolean fields
`disable_camera_on_mute` and `mute_speaker_on_mute` also lock camera capture
and speaker output; both default to `false`. Lamp enables both in its JSON,
while Intern keeps its microphone-only fallback and ships no privacy JSON.
HAL keeps opted-in peripherals closed until the first GPIO read; read/claim
failure leaves them locked. The camera capture wrapper, camera routes,
auto-wake helpers and audio output gates share `hal/privacy.py`. Unlock restores
the prior camera/speaker preferences; temporary locks do not overwrite their
boot-scoped persisted preferences.
The loader accepts legacy `mic_button.json` only when `privacy_button.json` is absent,
so HAL can be updated before the device JSON is renamed.
The microphone slide switch uses device-owned `privacy_button.json`, initially
shipped at `robots/lamp/privacy_button.json` for physical pin 11 / PL9 / gpiochip1 line 9. The detected board's entry
under `boards` supplies `chip`, `line`, `settle_s`, `muted_level`, and
`watchdog_s` to `hal/board/privacy_button.py`, then the shared
`hal/drivers/privacy_button.py` driver. Intern v2 ships no mic JSON and continues
using the code fallback: gpiochip0 line 97 (PD1), a 0.06 s settling delay,
LOW (`0`) for muted, and a 30 s watchdog. Lamp uses the same timing/polarity. Pull-up remains enabled; `muted_level` accepts `0` or `1`.
A missing file or board entry preserves the old defaults for `intern-v2`
only, including its existing device-type gate regardless of board ID. Other
devices without configuration skip the switch; `enabled: false` explicitly
disables it. Malformed configuration is rejected before GPIO is claimed.
HAL synchronizes mute to the switch position at startup and after settled
edges. The watchdog reconciles only when the pin changed, preserving software
mute changes while the switch stays still. Restart HAL to apply JSON changes;
simulation skips this hardware input. Keep Lamp's GPIO button JSON installed:
its legacy primary-button fallback also uses pin 11, while the device JSON
moves the primary button to pin 37. Lamp startup with this mic JSON was verified on 2026-09-11: chip1/line9
was ready and the initial LOW position applied mute. Live toggle testing is pending.

TTP223 touch wiring is device-owned in `robots/lamp/ttp223.json`. Intern v2
has no TTP223 hardware and does not ship this file. `hal/board/ttp223.py` resolves the detected
board's `chip`, `lines` and optional `axis` from `boards` and passes a
`TouchConfig` to the shared driver. Missing file/board falls back to legacy
`touch` in `hal/board/boards.json`; `enabled: false` disables TTP223 explicitly.
Malformed configuration is rejected before GPIO is claimed. Restart HAL after
editing the selected device's configuration; simulation skips hardware input.
Lamp uses two TTP223 pads: S1 on physical pin 29 / PD0 / gpiochip0 line 96
and S3 on pin 33 / PD2 / line 98. Its button uses pin 37 / PD4 / line 100.
Keep the Lamp JSON installed: the legacy touch fallback still overlaps the button.
Intern v2 retains its existing button wiring. The legacy board fallback remains
unchanged; absence of a JSON file alone does not disable the driver.

Optional MPR121 touch wiring currently belongs to Lamp only, in
`robots/lamp/mpr121.json`; Intern v2 has no MPR121 declaration. It follows the
same device-directory selection, validated by `hal/board/mpr121.py`. Lamp
ships an enabled `orangepi_sun60` entry using bus 0 and address 0x5A, verified
for initialization and polling on Lamp `lamp-0c4e`. The hardware script uses
pins 3/5 (TWI0); verify the actual wiring and enable the
correct I²C controller externally. HAL does not change boot overlays. Missing
`/dev/i2c-0` logs an initialization failure and leaves existing inputs running.
Its `boards` entry requires an explicit I²C `bus`; there is no scan or guessed MPR121 fallback. Missing
file/board or `enabled: false` skips MPR121 and preserves existing GPIO/TTP223
inputs; simulation also skips it. Malformed enabled configuration rejects
startup. Restart HAL after changing wiring configuration. The shared
`hal/drivers/mpr121.py` driver groups selected electrodes into one debounced
contact. It shares GPIO gesture thresholds in `hal/drivers/button_gestures.py`:
the first resolved short release calls single-click with `announce=False`;
a 0.4 s quiet window produces the listening cue for 1/2/4+ clicks. Exactly
3 clicks produce no additional action or cue: reboot is disabled in the MPR121 wrapper. Holds commit only on release: 2–<5 s sleepy, 5–<10 s shutdown,
≥10 s factory reset. While held, debounced hold-tier events use the same
`HoldLEDFeedback` in `hal/drivers/button_actions.py` and `BUTTON_LED_PRESETS`
as GPIO: purple blinking at 2 Hz for 2–<5 s, red blinking
at 2 Hz for 5–<10 s, and solid red from 10 s. Release stops blinking; accepted
shutdown/reset actions reaffirm solid red before execution. Boot-held contacts
show no feedback; stop or hardware failure cancels it. LED feedback is verified
with mocked local tests; it has not been checked on the live device. Defaults are address
`0x5A`, electrodes 0–11, touch/release thresholds 2/1, autoconfiguration enabled,
10 ms polling and 30 ms debounce, with 100 ms startup settling. An I²C failure
or overcurrent fault stops only this driver. Logger `hal.drivers.mpr121` records
configuration, electrode changes, debounced taps, action dispatch and lifecycle
in the normal HAL log/journal; unchanged polls do not emit INFO logs.
Lamp enables swipe detection with `swipe_axis: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]`.
This ordered electrode axis follows observed travel in device logs; either
orientation works because both directions call shared `swipe_action` to sleep.
Partial traversals are supported. Stationary contacts keep click/hold actions;
a moving contact suppresses those actions so dragging cannot become reset.
Swipe classification adds up to 120 ms of release grace for electrode handoff.
Missing/null `swipe_axis` preserves the original click/hold-only behavior.
Install the updated HAL before the new device JSON: older strict loaders reject
unknown configuration fields. Swipe tests replay recorded electrode transitions;
the new runtime and swipe configuration were deployed to Lamp `lamp-0c4e` on
2026-09-11 and startup was verified. Live gesture testing is pending.
See [physical controls](../robots/lamp/docs/physical-controls.md) for configuration
and gesture details.

## Principles

- **Hardware is a plugin** — plug in and it works, unplug and it's skipped
- **System layer runs WITHOUT the runtime** — device always responds
- **Code is the source of truth** — docs reflect code
- **HAL is the hardware driver** — no AI logic
- **SKILL.md native** — no MCP, LLM reads skills and calls curl directly
- **Owners can create skills** — the built-in `skill-creator` guides an owner
  through drafting, testing, and packaging a skill for the Autonomous Skill Store.

## Lamp Simulator on a Laptop

> **Setup and run instructions live in [simulator.md](simulator.md)** — prerequisites,
> the config file and its example, the four `make` targets, and troubleshooting.
> This section covers what the simulator *is*.

`make sim` boots the production `lamp` declaration on a laptop. It keeps the
normal HAL routes and safety gates, but substitutes virtual motion, LED,
camera, microphone, speaker, voice and sensing services; it never opens a
servo bus, macOS camera/mic, GPIO, or sends GELF logs. Startup prints clickable
local links for the HAL docs and, for the default Lamp body,
`http://127.0.0.1:5001/simulator`. The latter has an orbitable view of the
checked-in Lamp CAD assembly plus live five-joint values, recording playback,
and LED-effect controls using the same `/servo/*` and `/led/*` endpoints a
skill uses. Drag to orbit, scroll to zoom, and double-click to reset the
camera. The state panel names the posture mode holding the body (`zero`,
`hold`) next to the live joints, and the Motor control button that owns it is lit: an
animation pressed while the motor is held is reported as ignored with that
reason, not as a tick, because `/servo/play` answers `"ignored"` rather than
`"ok"` for a play it dropped. Its CAD motion preview responds to the live joint
values, including
recording playback; a control switches to the untouched static assembly for
comparison. The repository does not include the mechanical joint hierarchy,
pivots, axes, or calibrated CAD zero offsets, so this visual response is not a
claim that a rendered `down` or `right` pose is physically correct.

This is an interface simulator, not a physics model: it has no mass or
collision model, while shipped recording CSVs replay their timing in memory.
Virtual camera/audio content is deterministic. To boot a minimal contract test
body instead, use `make sim DEVICE_TYPE=sim`. That body declares `motion` and
`system` and nothing else, which is how we prove HAL mounts exactly what a
`ROBOT.md` declares and no more — lamp cannot show that, because lamp declares
everything. See `robots/sim/ROBOT.md`.

For a manual Mac media check, use `make sim SIM_MEDIA=host`. This explicitly
opens the host camera, microphone and speaker: the simulator page shows its
camera stream, **Play test tone** uses the speaker, and **Record 3 seconds**
captures a WAV for playback. The default `SIM_MEDIA=virtual` remains
permission-free and deterministic for tests.

Host mode never hard-fails. Each subsystem is probed at boot — the webcam is
opened and read once, the microphone records a few milliseconds — and whichever
one is missing, busy or permission-denied falls back to its virtual device with
a logged `[sim-media]` line. `GET /simulator/state` reports the outcome per
subsystem (`media_camera`, `media_audio`, `media_reasons`, with `media` being
"host" only when both are), and the simulator page prints the same reason and
disables the tone/record buttons whenever audio is virtual. On macOS the two
permissions live under System Settings > Privacy & Security > Camera and
Microphone, and must be granted to the terminal app running HAL.

The camera in host mode uses a simulation-only driver
(`hal/drivers/camera/host_capture_device.py`, registered as `host`) that opens
the webcam through the platform's native OpenCV backend — AVFoundation on
macOS, where the production V4L2 path and its USB power-cycle healing do not
exist. Production bodies still resolve their `driver:` from ROBOT.md.

Host mode also runs the **real voice pipeline**: entry VAD, Silero, STT, the
realtime agent (Gemini Live), wake word, and the `[turn] route=…` dispatch that
forwards to os-server — the same code a board runs, not a stub. The gate is
`state.simulation_audio`, so the two decisions can never drift apart: a laptop
using its own microphone gets the pipeline that microphone exists for, and a
macOS permission denial flips the flag back and lands on the inert
`VirtualVoiceService` with a logged reason rather than a real pipeline reading a
dead device. `SIM_MEDIA=virtual` keeps the stub, so tests stay silent and offline.

Credentials come from the config.json HAL shares with os-server, exactly as on a
board — point `OS_CONFIG_PATH` at the `make os-dev` state dir and one file feeds
both processes. `llm_api_key` alone covers LLM, `AutonomousSTT`, ElevenLabs TTS,
image description, **and** Gemini Live (whose key falls back to it and whose
endpoint is `llm_base_url` + `/ws/gemini`); `deepgram_api_key` is optional and
only swaps the STT provider.

Music plays too. `MusicService` streams yt-dlp → ffmpeg → **aplay** (ALSA), or
**paplay** when a Bluetooth sink is active — neither exists on macOS, so both
routes failed at `Popen` and the device apologised out loud with "Sorry, I can't
play that right now". macOS gets a third route: ffmpeg's own AudioToolbox output
device, chosen over `ffplay` because ffmpeg is already a hard dependency here and
`ffplay` is not in every build. `SIM_MEDIA=virtual` keeps the same pipeline into
a null sink — the search and decode still run, the laptop just stays quiet.

Two HAL paths are device-absolute and must move for a laptop, both already
env-driven and set by the `sim` target: `HAL_SNAPSHOT_DIR` (where
`GET /camera/snapshot?save=true` writes — it has to sit under the agent
runtime's own home, `/root/.codex/media/hal-snapshots` on a board, or the agent
cannot read the frame back and os-server cannot serve its thumbnail) and
`HAL_SNAPSHOT_PERSIST_DIR` (`/var/lib/hal/snapshots` is root-only).

## Voice Pipeline

```
Mic (always on) → Local VAD (RMS energy, free)
    → Speech detected → Connect Deepgram STT
        → "hey lamp, turn off light" → voice_command → local intent → execute
        → "hey wanna grab lunch?" → voice (ambient) → OpenClaw
    → Silence 3s → Disconnect Deepgram
```

## Sensing Flow

SEN55 environmental acquisition is a separate optional HAL capability. See
[Environmental sensing](../robots/lamp/docs/environment-sensing.md) for wiring, configuration,
and snapshot APIs; it does not yet emit OS/agent sensing events.

```
HAL sensing loop (every 2s) → Read 1 camera frame, run all detectors:
    ├─ Motion detection (frame diff) → event if >8% pixels changed
    ├─ Face recognition (InsightFace buffalo_sc) → friend/stranger classification
    │     → presence.enter (annotated JPEG with colored bboxes: green=friend, red=stranger)
    │     → presence.leave (3 consecutive ticks without face)
    ├─ Light level (mean brightness, every 30s) → event if change >30/255
    └─ Sound detection (mic RMS) → event if > threshold

Event has image? (large motion, face enter) → encode frame full-resolution JPEG q85
Face enter image: original frame annotated with bounding boxes + labels

POST /api/sensing/event {type, message, image?}
    → OS server (Go):
        1. Voice event + local intent match? → execute directly (~50ms)
        2. No match → forward to OpenClaw:
           - Has image → SendChatMessageWithImage (text + vision content block)
           - No image → SendChatMessage (text only)
        3. OpenClaw AI sees image + reads context → decides action → calls SKILL API
```

Cooldowns to protect LLM costs: motion/sound 60s, presence 10s, light.level 30s.
