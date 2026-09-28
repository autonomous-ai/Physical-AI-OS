## Autonomous OS: The "Android" for Robots

Autonomous OS is an open-source operating system for physical AI agents. Give your robot a voice, vision, memory, and skills — then connect it to agents and apps on your computer.

**Talk to your robot. Control its hardware. Delegate work to your computer.**

https://github.com/user-attachments/assets/c80f1255-4355-4f59-9114-6d3b8d4007a2

[Set up a robot](#quick-start) · [Bring your own robot](docs/bring-your-own-robot.md) · [Build a skill](#contribute) · [Architecture](#platform-architecture)

- **Understand and act.** Control lights, volume, and tracking through voice or chat. Local rules handle familiar commands; Jev can recognize natural phrasing before the request reaches the main agent.
- **Work beyond the robot.** [Harness](skills/harness-use/) delegates digital tasks to agents on your computer. [Autonomous Buddy](skills/computer-use/) lets the device agent operate Mac apps. [Connectors](skills/connectors/) give it access to linked services.
- **Make it yours.** Swap the [agent runtime](runtimes/), [model](docs/hosted.md), [voice](hal/drivers/voice/), [skills](skills/), or [board](hal/board/boards.json). Define its personality in `SOUL.md` and its hardware in `ROBOT.md`.

The OS runs on the robot and coordinates hardware and agent tasks. Model inference may use remote services, depending on your configuration.

## Try saying

On a configured Lamp, try these in normal voice mode or text-only Web/MQTT chat. These are supported intent examples; selection depends on confidence and available device capabilities. Uncertain requests go to the main agent.

| Say or type | Expected behavior |
|---|---|
| “Turn off the lights.” | Turn off Lamp's light through a local command. |
| “This lamp is too bright.” | Reduce the current light brightness by half. |
| “You're speaking too loudly.” | Reduce the current speaker volume by half. |
| “Make this lamp violet.” | Set a solid purple light. |
| “I need light to read a book.” | Activate the reading lighting scene. |

Context matters too: while working on an image or render, “Make it brighter” goes to the main agent to interpret the task context. It does not automatically brighten the Lamp. Explicit hardware requests such as “Turn off the lights” remain eligible for intent handling while a Harness task is pending.

[Intent configuration and limits](docs/os-server.md#jev-intent-fallback) · [Harness context routing](docs/harness.md#local-intent-versus-digital-task-context)

## Work across your robot and computer

- **Delegate to Harness agents.** [`harness-use`](skills/harness-use/) sends coding and research tasks to agents already running in [Harness](https://github.com/autonomous-ai/openharness) on your paired computer. Ask a named agent to work, answer its follow-up questions, and receive its result through voice or chat. Pair from OS Monitor and Harness Desktop on the same LAN. **Harness-only voice** sends manual tap-to-record turns to the agent focused in Harness. [Get Harness](https://github.com/autonomous-ai/openharness) · [Integration and setup](docs/harness.md).
- **Use Mac apps through Buddy.** [`computer-use`](skills/computer-use/) lets the device agent inspect an app, click or type, and verify the resulting UI through a paired Autonomous Buddy. Buddy bundles Cua Driver for Accessibility observations and actions, with screenshot support for visual tasks. The device agent owns the task; Buddy executes on the Mac. Harness and Buddy have separate connections and pairing. [Computer-use guide](integrations/companions/autonomous-buddy/docs/computer-use.md).
- **Route requests with Jev.** For normal voice requests received by os-server and text-only Web/MQTT chat, local rules run first, then the Jev intent fallback (enabled by default). The OS validates the selected command, parameters, confidence, and device capabilities before execution; uncertain requests continue to the main agent. Context-dependent follow-ups bypass intent handling when they need the main agent. Attachments and Harness-only voice retain their separate routes. Separate [skill-preloading integrations](docs/agentic/adding-agent-runtime.md#jev-skill-preloading-across-runtimes) prepare instructions before the main model call through runtime hooks or managed bridges, with native discovery as fallback. New integrations outside Hermes are disabled by default pending native validation, controlled by a Go build switch in each runtime. Jev also supports experimental [Buddy UI-action suggestions](docs/os-server.md#buddy-computer-use-feedback).

## Quick start

The simplest way in is a robot we have already tested it on. What each of them can do: [robot comparison](docs/robot-comparison.md).

### Autonomous Lamp

[Lamp](https://www.autonomous.ai/lamp) is the robot that shows the whole OS — it sees, hears, speaks, moves, and ships with Autonomous OS on it.

1. **Add it.** In the Autonomous app ([iOS](https://apps.apple.com/app/id6744885683) | [Android](https://play.google.com/store/apps/details?id=ai.autonomous.connect.wifi)), tap **Add robot → Lamp**.
2. **Set up Wi-Fi.** Pick your network in the app; it joins the robot's hotspot and hands over the keys and pairing.
3. **Interact with Lamp.** Say something, it turns to look at you, the ring lights up, and it answers.
4. **Install a skill** from the Skill Store — one tap, live on the next conversation.
5. **Build your own skill.** Type what you want it to do in the app and it writes the skill.
6. **Give it a character.** Edit [`SOUL.md`](robots/lamp/SOUL.md) and it is someone else on the next turn.

### Reachy Mini

[Reachy Mini](https://huggingface.co/docs/reachy_mini) is Hugging Face's desk robot, running our OS beside its own stack.

https://github.com/user-attachments/assets/2f0aaafb-287c-488e-a3b1-a82f0ad9e776

1. **SSH in** — `ssh pollen@reachy-mini.local`.
2. **Run one command.** Nothing is flashed; the Reachy daemon keeps the motors.
   ```bash
   curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/robots/reachy-mini/install.sh | sudo bash
   ```
3. **Add it.** In the app, tap **Add robot → Reachy Mini** and give it `reachy-mini.local`.
4. **Interact with it.** Say something — the head tilts, the antennas lift, and it answers.
5. **Install a skill** from the Skill Store, or type what you want it to do and it writes one.
6. **Give it a character.** Edit `/opt/devices/reachy-mini/SOUL.md`. Everything else, including how to undo the install: [`devices/reachy-mini/README.md`](robots/reachy-mini/README.md).
7. **Put it next to a Lamp.** Each one hears the other's answer as its next input, so the two of them will hold a conversation until you stop them.

### Autonomous Intern

[Intern](https://www.autonomous.ai/intern) is the always-on desk agent: mic, speaker, LED ring.

<img src="robots/intern-v2/images/intern-hero.webp" alt="Autonomous Intern on a desk beside a laptop, tip glowing blue">

1. **Add it.** In the app, tap **Add robot → Intern**.
2. **Set up Wi-Fi.** Same flow as Lamp: pick your network and it handles the keys and pairing.
3. **Interact with it.** Say something and it answers; the ring shows what it is doing.
4. **Install a skill** from the Skill Store.
5. **Build your own skill.** Type what you want in the app; it is live on the next conversation.
6. **Give it a character.** Edit `/opt/devices/intern-v2/SOUL.md`.

## Bring your own robot

Autonomous OS runs on any robot you can describe in four markdown files.

- **`ROBOT.md`** — the body: the board and the hardware it has.
- **`SOUL.md`** — the self: who it is and how it talks.
- **`SAFETY.md`** — the bounds: how fast, how bright, how late.
- **`SKILL.md`** — the hands: one thing it can do.

Follow **[the full guide](docs/bring-your-own-robot.md)**.

## Platform architecture

Autonomous OS is a software stack. Each layer uses only the layer below it, so any layer can be replaced without touching the others. Every layer is a folder in this repo.

![Autonomous OS stack, top down: apps, skills, the agentic runtime, the Go system services, the realtime voice agent, the capabilities a robot declares, the safety gate, drivers, boards, the vendor Linux kernel, and the bodies — one colour per layer, and the rows you can extend yourself drawn dashed](docs/architecture/autonomous-stack.png)

### [Apps](system/web/)

What a person touches. The Autonomous app adds a robot, sets up Wi-Fi, installs skills from the Skill Store and switches brains; the robot also serves its own setup and monitor UI from `system/web/`. Both talk to os-server on :5000.

### [Skills](skills/)

One folder per behavior, one `SKILL.md` inside: markdown the agent reads. Hardware skills emit `[HW:/path:{json}]` markers for OS dispatch instead of touching a servo bus or GPIO pin. Computer and agent skills use helpers that call OS APIs and return observations or task results. Skills declare required capabilities so they install on compatible robots.

### [Agentic runtime](runtimes/)

The engine that thinks. Six of them — Hermes, OpenClaw, PicoClaw, Codex, Claude Code, OpenCode — behind one 76-method `AgentGateway`. It reads the robot's `SOUL.md` and its installed skills. Switch live from the web UI; persona, memory and connectors move with it.

### [System services](system/)

The Go daemon `os-server` on :5000, one package per box in the figure. `intent` handles eligible voice and text-only Web/MQTT commands through local rules, then a validated Jev fallback (on by default), while deferring contextual follow-ups to the main agent; `harness` delegates work to paired computer agents; `buddy` carries Mac observations and actions; `server` strips `[HW:…]` markers out of a reply and POSTs them to HAL before the words are spoken; `agent` switches engines; `bootstrap` is OTA, its own binary.

### [Realtime voice](hal/realtime/)

HAL supports Gemini Live (default model: `gemini-3.8-live`), OpenAI Realtime, GPT-Live (`gpt-live-1`), and **Pipecat v1** (`pipecat_v1`). In normal voice mode, the realtime agent answers directly or delegates tasks to the main runtime; Harness-only voice routes manual captures directly to Harness.

Pipecat runs the pipeline inside HAL: speech recognition → an OpenAI-compatible LLM (default: `qwen/qwen3.6-35b-a3b`) → text spoken by HAL's TTS. Pipeline orchestration runs on the robot; model calls still use remote services. It supports both committed turns and continuous Live input.

**Smart Turn** runs a local ONNX model to help decide when a person has finished speaking. It supplements silence detection in the shared non-Live capture path and works with Silero VAD in Pipecat Live. Bounded silence fallbacks handle unavailable inference; manual Harness recording still ends on the user's tap. Pipecat and Smart Turn require HAL's optional `pipecat` extra, included in Lamp/Pi/OrangePi setup but excluded from Reachy because of conflicting ONNX dependencies. [Voice architecture, configuration and limits](docs/realtime-voice.md).

### [Capabilities](robots/contract/capabilities.md)

The 13 names a robot may declare — audio, vision, sensing, presence, motion, light, display, expression, lifelike, media, connectivity, companion, system. Ten mount [HTTP routes](hal/routes/) on :5001 (111 endpoints, live Swagger at `/api/hardware/docs`); `presence` and `lifelike` are loops with no route, `companion` lives in os-server. HAL mounts only what `ROBOT.md` declares and fails loud on a missing required driver.

### [Safety gate](hal/safety/)

A pure function of `SAFETY.md`, below the engine and in every request path: brightness, quiet hours, explicit-move speed. No model in the loop — the same clamp whoever asked. What it does not cover yet: [`docs/safety.md`](docs/safety.md).

### [Drivers](hal/drivers/)

One folder per subsystem: motors, rgb, camera, voice, display, sensing, tracking, and the media handover a third-party daemon needs. New hardware is one class and one factory line.

### [Boards](hal/board/boards.json)

One JSON entry per board, matched against `/proc/device-tree/model`. Raspberry Pi 4, Pi 5, CM4 and OrangePi 4 Pro today. A new board is an entry, not a code change.

### Linux

The vendor kernel — Raspberry Pi OS, OrangePi Debian, or the robot's own image. We do not ship one, and nothing above the drivers has a real-time deadline: position control closes in the servo firmware, or in the robot's own daemon.

### [Bodies](robots/)

Four markdown files and a driver per robot. Declarations, not forks — a body is a PR.

Long form: [architecture](docs/architecture/overview.md) · [HAL](docs/architecture/hal.md) · [device spec](robots/contract/ROBOT-SPEC.md) · [capabilities](robots/contract/capabilities.md) · [safety](docs/safety.md) · [developer guide](docs/developer-guide.md).

## Contribute

The easiest way in is a skill: one markdown file, no Go, no hardware, and it lands on every robot that has the parts. PRs welcome, vibe-coded ones included. Questions, half-built ports and show-and-tell go in [Discussions](https://github.com/autonomous-ai/autonomous-os/discussions); gaps we would love help with are labelled [`claim-me`](https://github.com/autonomous-ai/autonomous-os/issues?q=is%3Aissue+is%3Aopen+label%3Aclaim-me) — comment to take one.

| You want to… | You write… | Start from |
|---|---|---|
| Teach every robot something new | `skills/<name>/SKILL.md` (+ `skill.json` if it needs hardware) | [`skills/guard/`](skills/guard/) · [`skill-creator`](skills/skill-creator/) |
| Run Autonomous on your robot | `robots/<id>/ROBOT.md` + `SAFETY.md` + `SOUL.md` | [`robots/reachy-mini/`](robots/reachy-mini/) — a third-party port, end to end |
| Support new hardware | a class in `hal/drivers/<subsystem>/` + one factory line | [`reachy_service.py`](hal/drivers/motors/reachy_service.py) |
| Support a new board | one entry in `hal/board/boards.json` | [`boards.json`](hal/board/boards.json) |
| Add a brain | an `AgentGateway` implementation in `runtimes/<name>/` | [`adding-agent-runtime.md`](docs/agentic/adding-agent-runtime.md) |

Seven more paths — apps, chat bridges, perception models, voices, safety bounds, CTS probes — and the norms: [`CONTRIBUTING.md`](CONTRIBUTING.md). One rule worth knowing up front: [`robots/contract/`](robots/contract/) is the interface everyone builds on, so open an issue before you change it.

Build locally:

```bash
make os-build && make os-test          # Go daemon, cross-compiled to linux/arm64
(cd hal && uv sync) && make hal-dev    # HAL on :5001 with reload
make web-install && make web-dev       # setup + monitor UI
make cts                               # is this a valid Autonomous device?
```

## License

Everything outside `hal/` is Apache-2.0. `hal/` is GPL-3.0, kept that way by choice so the tree has one license per top-level folder; a driver you commit there is GPL, so a closed vendor SDK wraps out of process.

A robot running this carries other people's work: Pollen's [`reachy_mini`](https://github.com/pollen-robotics/reachy_mini) SDK, [YOLOv8](https://github.com/ultralytics/ultralytics) for tracking (AGPL-3.0 — read it before you ship), [TEN-VAD](https://github.com/TEN-framework/ten-vad) and [Silero](https://github.com/snakers4/silero-vad) for hearing, [LeRobot](https://github.com/huggingface/lerobot) and the [LeLamp Runtime](https://github.com/humancomputerlab/lelamp_runtime) under the motion code, and the brains we install but do not ship. All of it, including what we copied verbatim: [`CREDITS.md`](CREDITS.md). Security issues: [`SECURITY.md`](SECURITY.md).
