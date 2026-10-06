# Contributing

Autonomous is an open-source OS for physical AI agents — and we'd love your help. PRs welcome,
vibe-coded ones too. 🤖

## What you can build

| You want to… | You write… | Start from |
|---|---|---|
| Teach every robot something new | `skills/<name>/SKILL.md` | [`skills/guard/`](skills/guard/) · [`skill-creator`](skills/skill-creator/) |
| Run the OS on your robot | `robots/<id>/ROBOT.md` + `SAFETY.md` + `SOUL.md` | [`robots/reachy-mini/`](robots/reachy-mini/) — a third-party port, end to end |
| Support new hardware (open SDK) | a class in `hal/drivers/<subsystem>/` + one factory line | [`motors/reachy_service.py`](hal/drivers/motors/reachy_service.py) · [`camera/rpicam_capture_device.py`](hal/drivers/camera/rpicam_capture_device.py) |
| Support new hardware (closed SDK) | a small HTTP service speaking `MotionService` — [#204](https://github.com/autonomous-ai/Physical-AI-Operating-System/issues/204), not in-tree yet | [`base.py`](hal/drivers/motors/base.py) |
| Support a new board | one entry in `hal/board/boards.json` | [`boards.json`](hal/board/boards.json) |
| Add a brain | an `AgentGateway` implementation (76 methods, Go) in `runtimes/<name>/` + one factory case — the heaviest path | [`docs/agentic/adding-agent-runtime.md`](docs/agentic/adding-agent-runtime.md) · [`runtimes/opencode/`](runtimes/opencode/) |
| Ship an app people install with one click | a Python plugin against the plugin API | [`integrations/community-apps/plugin-template/`](integrations/community-apps/plugin-template/) · [plugin system](docs/plugin-system.md) |
| Add a voice — STT, TTS, or a realtime provider | a subclass in `hal/drivers/voice/` or `hal/realtime/voice_agent/` | [`voice_agent/openai_realtime.py`](hal/realtime/voice_agent/openai_realtime.py) |
| Turn a chat platform into a robot sense | a small Go program posting to `/api/sensing/event` (the web-chat bridge is ~230 lines) | [`integrations/chat-bridges/`](integrations/chat-bridges/) |
| Give the robot new eyes (a perception model) | a predictor in `integrations/perception-service/` or `hal/drivers/sensing/perceptions/` | [`perception-service/`](integrations/perception-service/) |
| Add a safety bound | a field + pure gate in `hal/safety/policy.py`, documented in [`SAFETY-SPEC.md`](robots/contract/SAFETY-SPEC.md) | [`policy.py`](hal/safety/policy.py) |
| Make the CTS stricter | a probe in `robots/contract/cts/` | [`test_runtime.py`](robots/contract/cts/test_runtime.py) |

You never fork the OS to add a device. If you're forking, the contract is missing something —
open an issue and let's fix it.

## A few norms (not rules)

- Keep PRs focused; green CI helps us merge faster.
- `robots/contract/` is the stable interface everyone builds on — open an issue before changing it.
- Two licenses: everything outside `hal/` is Apache-2.0; `hal/` is GPL-3.0 (see the [License](README.md#license) section). A driver under `hal/` ships GPL-3.0.
- Be kind.

Questions, half-built ports and show-and-tell go in [Discussions](https://github.com/autonomous-ai/Physical-AI-Operating-System/discussions); bugs and claims in [Issues](https://github.com/autonomous-ai/Physical-AI-Operating-System/issues). Open gaps are labelled [`claim-me`](https://github.com/autonomous-ai/Physical-AI-Operating-System/issues?q=is%3Aissue+is%3Aopen+label%3Aclaim-me) — comment to take one.
