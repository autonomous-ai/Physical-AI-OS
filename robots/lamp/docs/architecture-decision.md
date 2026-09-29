# Architecture Decision: AI Lamp — Hybrid Hardware Control

## Date: 2026-03-24

## 1. Context & Decision Journey

This project controls an AI-powered desk lamp built on a Raspberry Pi 4 with articulated servos, RGB LEDs, camera, microphone, and speaker.

The architecture went through several pivots before reaching the final design:

1. **Standalone Go + MCP** — Initially planned as a new Go project using MCP protocol for hardware control. Abandoned when we discovered OpenClaw uses its own native skill system (SKILL.md), not MCP.
2. **HAL runtime already exists** — Discovered a Python runtime is ALREADY running on the Pi4 with working hardware drivers for servos (MotorsService), LEDs (RGBService), and audio (amixer). It was previously controlled via LiveKit @function_tool decorators.
3. **Final decision** — Hybrid architecture. OpenClaw replaces LiveKit + OpenAI entirely. OpenClaw skills call the Lamp HTTP API, which bridges to the existing HAL Python services for hardware access.

## 2. Final Architecture Decision

**Hybrid two-layer architecture + HAL Python bridge + Hardware Plugin system.**

- **Layer 1 (System)**: Lamp Server handles system-critical functions that work without OpenClaw.
- **Layer 2 (Skills)**: OpenClaw's LLM reads SKILL.md files and calls Lamp HTTP endpoints, which bridge to HAL's Python hardware drivers.

The HAL Python runtime is kept as the hardware driver layer. We do NOT rewrite drivers in Go — we bridge to them.

### Hardware as Plugins (Plug & Play)

Every hardware component is a **plugin** — if it's plugged in, its driver loads and its skill becomes available. If not, the system works fine without it.

On startup, the Lamp server auto-detects connected hardware and:
1. Loads only the drivers for detected hardware
2. Enables only the corresponding HTTP API endpoints
3. Deploys only the relevant SKILL.md files to OpenClaw

| Plugin | Detection Method | If Missing |
|---|---|---|
| Servo Motors | USB serial port scan (Feetech) | No body language, lamp is static — still works as smart light |
| LED (WS2812) | SPI device check (`/dev/spidev0.0`) | No light control — system LED only |
| Camera | V4L2 device check (`/dev/video0`) | No gesture, presence, tracking — voice-only control |
| Microphone | ALSA device enumeration | No voice input — app/text control only |
| Speaker | ALSA device enumeration | No voice output — silent mode, LED-only feedback |
| Display (Eyes) | I2C/SPI device scan (GC9A01/SSD1306) | No eyes — LED-only emotion feedback |

This means the same codebase supports different product configurations:
- **Full lamp**: All plugins → complete AI companion
- **Simple lamp**: LED + Mic + Speaker only → smart light with voice
- **Dev/test**: No hardware → stub drivers, API still works

## 3. Software Stack

### OpenClaw — AI Brain

Replaces HAL's previous LiveKit + OpenAI stack completely. Provides:

- Personality and conversation management
- LLM inference (multi-provider: Anthropic, OpenAI, local)
- Native skill system (SKILL.md auto-discovery)
- Channels (voice, text, app)
- Memory and context

### HAL Runtime — Hardware Drivers (Python)

Already running on the Pi4. Provides event-driven services with priority dispatch via ServiceBase:

- **MotorsService** — 5x Feetech servo control (pan, tilt, 5-axis articulation)
- **RGBService** — 64x WS2812 LED control (8x5 grid, per-pixel color via rpi_ws281x)
- **Camera** — OpenCV capture, JPEG snapshot, MJPEG stream
- **Audio** — Seeed mic/speaker, amixer volume, record WAV, play tone
- **DisplayService** — small round display (GC9A01 1.28" or similar), dual-mode: eyes emotion (default) + info display (time, weather, timer, notifications)

All hardware exposed via FastAPI on `127.0.0.1:5001` (systemd service: `hal.service`). Nginx proxies `/hw/*` for same-machine callers only — external clients receive 403. Swagger UI at `/hw/docs` is not accessible from LAN.

### Lamp Server — System Layer + HTTP API Bridge (Go)

Provides:

- All system-critical services (boot, network, OTA, reset, MQTT)
- HTTP API on port 5000 that bridges requests to HAL Python services

## 4. Layer 1: System (Lamp Server, Always Running)

Works **without OpenClaw**. If the AI is down, the device still boots, shows status via LED, and can be re-provisioned.

| Function | Description |
|---|---|
| Reset button | GPIO 26 long-press detection for power off / factory reset |
| Network management | AP mode for provisioning, STA mode for operation, WiFi scanning |
| OTA updates | Version check, download, install via bootstrap |
| MQTT communication | Auto-reconnect, message dispatch to backend |
| Internet monitoring | Connectivity check, auto-recovery |
| **Autonomous sensing** | Lightweight sensing loop: camera (presence, light level), mic (sound level, silence, voice tone), time (schedules), plug-in sensors. Emits events to OpenClaw when significant changes detected. |
| **Ambient life** | Idle behaviors that make Lamp feel alive: breathing LED (sine-wave brightness), color drift (warm palette rotation), micro-movements (safe servo recordings), TTS self-talk. Auto-pauses on interaction, resumes after 60s quiet. |

### Autonomous Sensing Loop (Layer 1.5)

Lamp runs a continuous, low-cost sensing loop that does **edge detection** on-device. When a significant event is detected, Lamp pushes context to OpenClaw for AI decision-making. This enables proactive behavior without burning LLM tokens continuously.

```
Sensing Loop (Lamp Server, always running):
  Camera → presence.enter / presence.leave / light.level
  Mic    → sound.level / sound.silence / sound.voice_tone
  Time   → time.schedule (cron-like)
  Sensor → sensor.* (plug-in: temp, humidity, etc.)
       │
       │ event + context (only on significant change)
       ▼
  OpenClaw (AI Brain) → decides action → calls Lamp HTTP API → hardware
```

**Rule-based actions** (no AI needed): auto-dim on leave, brightness adjust on darkness, idle animations.
**AI-driven actions** (OpenClaw decides): greetings, mood response, empathetic reactions, schedule-aware suggestions.

Lamp Server modules (under `system/`; `runtimes/` is at the repo root):

- `server/server.go` — Gin HTTP server on port 5000
- `server/config/` — JSON config with reload
- ~~`internal/resetbutton/`~~ — removed; GPIO button click/hold gestures are now handled by HAL (`hal/drivers/gpio_button.py`, `hal/drivers/button_actions.py`)
- `system/network/` — WiFi AP/STA management
- `runtimes/openclaw/` — OpenClaw config generation and WebSocket
- `system/beclient/` — Backend status reporter
- `system/device/` — Setup, MQTT command handling, status reporting
- `system/ambient/` — Idle "living creature" behaviors (breathing LED, servo micro-movements, TTS mumbles). Opt-in per device via the `lifelike` capability. Runs when no interaction is happening; auto-pauses on real input, resumes after 60s of quiet. Calls HAL HTTP API. See `sensing-behavior.md` → "Ambient Idle Behaviors".
- `lib/mqtt/` — MQTT client with auto-reconnect
- `bootstrap/` — OTA version check and install
- `domain/` — Shared structs (device, network, OTA, OpenClaw)

**MQTT commands** (received via fa_channel): `info`, `add_channel`, `ota`

## 5. Layer 2: OpenClaw Skills (SKILL.md + HTTP API)

All user-facing hardware control uses OpenClaw's native skill system. This is **NOT MCP**.

How it works:

1. SKILL.md files are placed in `workspace/skills/`
2. OpenClaw auto-discovers them (`skills.load.watch: true`)
3. The LLM reads the SKILL.md description and understands available APIs
4. The LLM calls the HAL HTTP API via `curl` at `127.0.0.1:5001` for hardware (the Lamp server at `127.0.0.1:5000` serves system/agent APIs)
5. The HAL Python service drives the hardware

### Skills

| Skill | SKILL.md Location | Description |
|---|---|---|
| `led-control` | `workspace/skills/led-control/SKILL.md` | Color, brightness, effects for 64-LED grid |
| `servo-control` | `workspace/skills/servo-control/SKILL.md` | Aim, animations, positions for 5 servo axes |
| `camera` | `workspace/skills/camera/SKILL.md` | Snapshot, MJPEG stream |
| `audio` | `workspace/skills/audio/SKILL.md` | Volume, play-tone, record WAV |
| `voice` | `workspace/skills/voice/SKILL.md` | TTS speak, voice status |
| `display` | `workspace/skills/display/SKILL.md` | Eyes expressions + info text on round LCD |
| `emotion` | `workspace/skills/emotion/SKILL.md` | Combined expression (servo + LED + display) |
| `scene` | `workspace/skills/scene/SKILL.md` | 6 lighting presets |
| `sensing` | `workspace/skills/sensing/SKILL.md` | Motion/sound events, presence |
| `scheduling` | `workspace/skills/scheduling/SKILL.md` | Cron scheduler (removed — no longer in `skills/`) |

Source of truth for skills is the repo `skills/<name>/SKILL.md`; they are installed into the runtime workspace (`workspace/skills/`).

### HTTP API Endpoints (HAL FastAPI, :5001)

All hardware endpoints run on HAL. OpenClaw skills call `127.0.0.1:5001` directly.

| Endpoint | Method | Description |
|---|---|---|
| `/led` | GET | LED strip info |
| `/led/color` | GET | Current LED color |
| `/led/solid` | POST | Fill all LEDs with single RGB color |
| `/led/paint` | POST | Set individual pixel colors (up to 64) |
| `/led/off` | POST | Turn off all LEDs |
| `/led/effect` | POST | Start effect (breathing, candle, rainbow, notification_flash, pulse) |
| `/led/effect/stop` | POST | Stop current effect |
| `/led/status` | POST | Status-LED effect |
| `/led/restore` | POST | Restore the previous user LED state |
| `/servo/play` | POST | Play a recording from `hal/recordings/` (list via `GET /servo`), e.g. curious, nod, happy_wiggle, idle, sad, excited, shy, shock, headshake, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching |
| `/servo/move` | POST | Send joint positions with smooth interpolation |
| `/servo/aim` | POST | Aim lamp head (center, desk, wall, left, right, up, down, user) |
| `/servo/track` | POST/GET (+ `POST /servo/track/update`, `POST /servo/track/stop`) | Vision-guided object tracking. See [vision-tracking.md](vision-tracking.md) |
| `/camera/snapshot` | GET | Capture single JPEG frame. `?save=true` saves to timestamped file, returns JSON path |
| `/camera/stream` | GET | MJPEG live stream |
| `/audio/volume` | GET/POST | Get/set speaker volume (0-100%) |
| `/audio/play-tone` | POST | Play test tone |
| `/audio/record` | POST | Record WAV from microphone |
| `/voice/speak` | POST | Text-to-speech output |
| `/voice/status` | GET | Voice pipeline status |
| `/display/eyes` | POST | Set eye expression + pupil position |
| `/display/info` | POST | Show info text (time, weather, etc.) |
| `/emotion` | POST | Combined expression (servo + LED + display); presets in `hal/presets.py` `EMOTION_PRESETS` (list via `GET /emotion/presets`) |
| `/scene` | POST | Activate lighting scene (reading, focus, relax, movie, night, energize) |
| `/presence` | GET | Presence state (present/idle/away) |

### Example

User says: *"Point the light at my desk, focus mode"*

OpenClaw LLM reads `servo-control/SKILL.md` and `scene/SKILL.md`, then executes:

```bash
curl -s -X POST http://127.0.0.1:5001/servo/aim \
  -H "Content-Type: application/json" \
  -d '{"direction": "desk"}'

curl -s -X POST http://127.0.0.1:5001/scene \
  -H "Content-Type: application/json" \
  -d '{"scene": "focus"}'
```

No command parsing logic needed — the LLM figures it out from the SKILL.md description.

## 5b. Monitor Dashboard & System API

The web UI at `/monitor` provides real-time observability into the lamp's operation. The Go server exposes system/monitor endpoints alongside the skill-facing API.

### Monitor API Endpoints (Lamp Server, port 5000)

| Endpoint | Method | Description | Data Source |
|---|---|---|---|
| `/api/system/info` | GET | CPU load, RAM usage, temp, uptime, version | `/proc/loadavg`, `/proc/meminfo`, `/sys/class/thermal/`, `/proc/uptime` |
| `/api/system/network` | GET | WiFi SSID, IP, signal, internet status | `iwgetid`, `ip addr`, `ping 8.8.8.8` |
| `/api/system/dashboard` | GET | Combined status snapshot | OpenClaw + config |
| `/api/agent/status` | GET | OpenClaw WS connection + session state | `openclaw.Service.IsReady()` |
| `/api/agent/events` | GET | SSE stream of real-time workflow events | Event bus (ring buffer + SSE broadcast) |
| `/api/agent/recent` | GET | Last 100 monitor events | Event bus ring buffer |

### OpenClaw Event Bus

The Go server maintains a **ring buffer (200 events)** in `openclaw.Service` that captures all observable events in the OpenClaw workflow. Events are broadcast to browser clients via Server-Sent Events (SSE).

**Event types captured:**

| Type | Source | Description |
|---|---|---|
| `sensing_input` | `POST /api/sensing/event` | HAL detected motion/sound |
| `chat_send` | `SendChatMessage()` | Message forwarded to OpenClaw |
| `lifecycle` | OpenClaw WS `agent.lifecycle` | Agent start/end/error |
| `thinking` | OpenClaw WS `agent.thinking` | Chain-of-thought reasoning (requires `caps: ["thinking-events"]`) |
| `tool_call` | OpenClaw WS `agent.tool` | Tool invocation start/end with name + args |
| `assistant_delta` | OpenClaw WS `agent.assistant` | Streamed text generation |
| `chat_response` | OpenClaw WS `chat` | Partial/final assistant response |
| `tts` | `SendToLeLampTTS()` | Response sent to speaker |

**Observable flow:**
```
👁 Sensing → ➜ Send → ⚙ Agent start → 🧠 Thinking → 🔧 Tool call → ✏ Writing → 💬 Response → 🔊 TTS
```

### Web Monitor Page (`/monitor`)

Dashboard layout with 4 sections:
1. **Status grid** (4 cards): OpenClaw connection, System info (CPU/RAM/temp/uptime), Network (SSID/IP/signal/internet), Hardware badges (servo/LED/cam/audio/sensing)
2. **Presence bar**: Present/idle/away state from HAL `/hw/presence`
3. **Workflow timeline**: Real-time SSE event stream with color-coded event types
4. **Camera**: Collapsible MJPEG stream + snapshot from HAL `/hw/camera/stream`

### HAL Status Endpoints (proxied via nginx `/hw/*`, local-only)

| Endpoint | Method | Description |
|---|---|---|
| `/hw/health` | GET | Hardware status: servo, LED, camera, audio, sensing (boolean each) |
| `/hw/presence` | GET | Presence state: present/idle/away, seconds_since_motion, auto/manual |
| `/hw/led` | GET | LED strip info (led_count) |
| `/hw/camera/stream` | GET | MJPEG live stream |
| `/hw/camera/snapshot` | GET | Single JPEG frame |

## 6. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User (Voice / Gesture / App)                │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        OpenClaw (AI / LLM)                          │
│                                                                     │
│  • Personality, conversation, memory                                │
│  • Multi-provider LLM (Anthropic, OpenAI, local)                    │
│  • Channels (voice, text, app)                                      │
│                                                                     │
│  workspace/skills/                                                  │
│  ├── led-control/SKILL.md                                           │
│  ├── servo-control/SKILL.md                                         │
│  ├── camera/SKILL.md                                                │
│  ├── audio/SKILL.md                                                 │
│  └── emotion/SKILL.md       ← key: combined emotional expression   │
│                                                                     │
│  LLM reads SKILL.md → calls curl → Lamp HTTP API                  │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ HTTP (127.0.0.1:5000)
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Lamp Server (Go)                                │
│                                                                     │
│  ┌───────────────────────────┐  ┌─────────────────────────────────┐ │
│  │  Layer 1: System          │  │  Layer 2: HTTP API Bridge       │ │
│  │  (always running)         │  │  (port 5000)                    │ │
│  │                           │  │                                 │ │
│  │  • Reset button (GPIO 26) │  │  /api/led     → HAL RGB     │ │
│  │  • Network mgmt (AP/STA)  │  │  /api/servo   → HAL Motors  │ │
│  │  • OTA updates            │  │  /api/camera  → Camera module   │ │
│  │  • MQTT backend           │  │  /api/audio   → Audio / amixer  │ │
│  │  • Internet monitor       │  │  /api/emotion → Motors+RGB+Audio│ │
│  │                           │  │  Bridges HTTP requests to       │ │
│  │  Works WITHOUT OpenClaw   │  │  HAL Python services         │ │
│  └───────────────────────────┘  └────────────────┬────────────────┘ │
│                                                   │                  │
└───────────────────────────────────────────────────┼──────────────────┘
                                                    │ Bridge (HTTP / gRPC / subprocess)
                                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   HAL Runtime (Python, on Pi4)                    │
│                                                                     │
│  • MotorsService  — 5x Feetech servos (5-axis articulation)        │
│  • RGBService     — 64x WS2812 LEDs (8x5 grid, rpi_ws281x)        │
│  • Camera         — OpenCV capture, snapshot, MJPEG stream          │
│  • Audio          — Seeed mic/speaker, amixer volume, record WAV    │
│  • ServiceBase    — event-driven with priority dispatch             │
│                                                                     │
│  FastAPI on :5001 | systemd: hal.service                    │
│  nginx: /hw/* → 127.0.0.1:5001 (Swagger at /hw/docs)              │
│                                                                     │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Hardware (Raspberry Pi 4)                    │
│                                                                     │
│  • 5x Feetech servo motors (5-axis articulated movement)            │
│  • 64x WS2812 RGB LEDs (8x5 grid, full color, per-pixel)           │
│  • Camera (inside lamp core, vision)                                │
│  • Microphone (voice input)                                         │
│  • Speaker (voice output)                                           │
│  • Reset button (GPIO 26)                                           │
└─────────────────────────────────────────────────────────────────────┘
```

> **Current implementation:** the Lamp-server bridge (`/api/led`, `/api/servo`, `/api/emotion`, …) shown above was the original design. Today OpenClaw skills call HAL directly at `127.0.0.1:5001` (e.g. `POST /led/solid`, `POST /servo/aim`, `POST /emotion`); the Lamp server (`:5000`) serves system/agent APIs only.

## 7. Emotion Skill — Key Differentiator

The emotion skill is the most important new skill. It combines all hardware subsystems to create generative body language — making the lamp feel alive.

Instead of calling servo, LED, and audio separately, the LLM calls a single endpoint:

```
POST /api/emotion
{"emotion": "curious", "intensity": 0.8}
```

(Implemented as HAL `POST http://127.0.0.1:5001/emotion` with the same body.)

The Lamp server translates this into coordinated hardware actions:

- **Servo**: Tilt head forward and slightly to the side (curious posture)
- **LED**: Shift to warm yellow-white, gentle pulse
- **Audio**: Optional soft chime or intake sound

Each call produces a unique expression — predefined emotion presets with randomized parameters (slight variations in tilt angle, LED hue, timing) so the lamp never repeats the exact same gesture.

The LLM calls this whenever it wants to express emotion during conversation, making interactions feel natural and embodied rather than purely verbal.

## 8. Communication Flow

```
User speaks
  → Microphone captures audio
    → OpenClaw processes voice input
      → LLM generates response + decides on actions
        → LLM reads relevant SKILL.md files
          → LLM calls curl to HAL HTTP API (127.0.0.1:5001)
            → HAL Python service drives hardware
                  → Servos move / LEDs change / Speaker outputs audio
```

## 9. New to Build

| Component | Path | Description |
|---|---|---|
| Servo HTTP handlers | `server/servo/delivery/` | Gin routes for `/api/servo`, bridges to HAL MotorsService |
| Camera HTTP handlers | `server/camera/delivery/` | Gin routes for `/api/camera/*`, bridges to camera module |
| Audio HTTP handlers | `server/audio/delivery/` | Gin routes for `/api/audio/*`, bridges to audio / amixer |
| Emotion HTTP handler | `server/emotion/delivery/` | Gin route for `/api/emotion`, coordinates servo + LED + audio |
| OpenClaw skills | `skills/` (repo root) | SKILL.md files for servo-control, camera, audio, emotion |
| Python bridge layer | Resolved: none — skills call HAL FastAPI (`:5001`) directly | Communication layer between Go Lamp server and HAL Python services (HTTP, gRPC, or subprocess) |

## 10. Open Questions

- [x] **Go-to-Python bridge**: HTTP proxy. HAL runs FastAPI on `127.0.0.1:5001`, Lamp Server proxies requests from port 5000. Simple, debuggable, no tight coupling.
- [ ] **Camera processing**: Run vision on-device with OpenCV, or offload to OpenClaw's vision capabilities?
- [ ] **Audio input**: Does OpenClaw handle the microphone directly, or does the Lamp server capture audio and forward it?
- [x] **LED driver**: HAL Python rpi_ws281x driver owns all LED control. Go SPI driver removed from Lamp — this lamp's hardware uses HAL's LED driver exclusively.
- [ ] **Generative body language**: How does the LLM generate servo positions for emotions? Predefined emotion presets with randomized parameters, or fully generative coordinates from the LLM?
