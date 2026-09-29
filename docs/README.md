# Autonomous OS documentation

Index of the platform docs in `docs/`. Each entry links the English doc and, when
one exists, its Vietnamese counterpart (`· VI`). Vietnamese index:
[`vi/README_vi.md`](vi/README_vi.md).

Robot-specific docs, integrations and companion apps have their own indexes — see
[Robots, integrations and companions](#robots-integrations-and-companions) below.

## Getting started

- [Architecture overview](overview.md) · [VI](vi/overview_vi.md) — the 3-layer stack (agentic runtime → OS server → HAL → hardware) and how the pieces talk.
- [Developer guide](developer-guide.md) — day-one workflows for a Developer Edition owner: SSH, shipping changes, logs, HAL and OS Server APIs.
- [Simulator](simulator.md) · [VI](vi/simulator_vi.md) — run the whole stack (HAL, os-server, agent bridge, web UI) on a laptop.
- [Bring your own robot](bring-your-own-robot.md) — the short path: three markdown files and one driver, seven steps.
- [Porting a robot](porting-a-robot.md) — the long form of porting, with file paths, sizes and caveats.
- [What each robot can do](robot-comparison.md) — capability-based skill grid across Lamp, Intern, Reachy Mini and others.
- [Hosted vs. local](hosted.md) — what the OS phones home to, what stays on your network, and what works offline.

## Architecture & OS server

- [Designing Autonomous](architecture/DESIGN.md) — architecture rules that let many devices share one OS without forking.
- [Layered architecture](architecture/overview.md) — the layer stack and its interfaces (with the stack figure).
- [Hardware Abstraction Layer](architecture/hal.md) — the frozen capability interface between Autonomous and hardware.
- [Kernel](architecture/kernel.md) — why the kernel is the vendor Linux kernel, not the agent runtime.
- Figure scripts: [`build_figures.py`](architecture/build_figures.py) draws the stack figure ([SVG](architecture/autonomous-stack.svg) · [PNG](architecture/autonomous-stack.png)) using the shared tokens in [`figs.py`](architecture/figs.py).
- [OS Server API](os-server.md) · [VI](vi/os-server_vi.md) — os-server (Go/Gin, :5000) endpoints and startup.
- [MQTT](mqtt.md) · [VI](vi/mqtt_vi.md) — backend MQTT: status reporting, OTA commands, channel management, dispatch.
- [Web UI — Monitor dashboard](web-ui.md) · [VI](vi/web-ui_vi.md) — the configuration and monitor SPA.
- [Flow Monitor](flow-monitor.md) · [VI](vi/flow-monitor_vi.md) — turn pipeline, JSONL events and SSE stream.
- [Agent session compaction](agent-compaction.md) · [VI](vi/agent-compaction_vi.md) — how auto-compaction summaries are injected and why they can override SKILL.md.
- [Plugin system](plugin-system.md) · [VI](vi/plugin-system_vi.md) — standalone Python apps that extend the device via HAL's HTTP API.
- [On-device model weights](hal-models.md) · [VI](vi/hal-models_vi.md) — where HAL's ONNX weights live and how they are fetched.
- [Device telemetry](telemetry.md) · [VI](vi/telemetry_vi.md) — the telemetry pipe and rules for adding a tracker.
- [Voice response metrics](voice-metrics.md) · [VI](vi/voice-metrics_vi.md) — voice KPI tracker built on the telemetry pipe.
- [Task completion metrics](task-metrics.md) · [VI](vi/task-metrics_vi.md) — chat and sensing completion cohorts.
- [What a turn costs](benchmarks.md) — benchmark method and command (numbers not yet measured).

## Setup, OTA & provisioning

- [Setup flow](setup-flow.md) · [VI](vi/setup-flow_vi.md) — AP-mode onboarding: WiFi, LLM provider, messaging channel.
- [Bootstrap & OTA](bootstrap-ota.md) · [VI](vi/bootstrap-ota.md) — installed components and the background OTA worker.

## Voice & realtime

- [Realtime voice agent](realtime-voice.md) · [VI](vi/realtime-voice_vi.md) — speech-to-speech layer (Gemini Live / OpenAI Realtime) and delegation to the main agent.
- [Speech emotion recognition](speech-emotion.md) · [VI](vi/speech-emotion_vi.md) — recognizing the user's emotion from voice.
- [Test procedure — Gemini idle-session recycle](testing-gemini-idle-recycle.md) — manual test for the post-silence closed-session fix.

## Agent runtimes

- [Adding an agentic runtime](agentic/adding-agent-runtime.md) · [VI](vi/agentic/adding-agent-runtime_vi.md) — AgentGateway contract and everything a new backend must wire up.
- [OpenClaw](agentic/openclaw.md) · [VI](vi/agentic/openclaw_vi.md) — Jev skill preloading plugin for OpenClaw.
- [Hermes](agentic/hermes.md) · [VI](vi/agentic/hermes_vi.md) — Hermes backend.
- [Remote Hermes](agentic/remote-hermes.md) · [VI](vi/agentic/remote-hermes_vi.md) — use a device as voice frontend for Hermes on your Mac.
- [PicoClaw](agentic/picoclaw.md) · [VI](vi/agentic/picoclaw_vi.md) — PicoClaw backend over WebSocket.
- [Codex](agentic/codex.md) · [VI](vi/agentic/codex_vi.md) — Codex backend via WS bridge.
- [Claude Code](agentic/claudecode.md) · [VI](vi/agentic/claudecode_vi.md) — Claude Code backend, bridge WebSocket, Telegram channel plugin.
- [OpenCode](agentic/opencode.md) · [VI](vi/agentic/opencode_vi.md) — OpenCode backend (`opencode run --format json` per turn).
- [Harness integration](harness.md) · [VI](vi/harness_vi.md) — pairing a device with a Harness computer; OS client ownership.
- [Harness Store](harness-store.md) · [VI](vi/harness-store_vi.md) — Store v1 contract implementation; pinned schemas in [`contracts/autonomous-device-store-v1`](contracts/autonomous-device-store-v1/README.md).
- [Skills review — 2026-09-10](skills-review.md) · [VI](vi/skills-review_vi.md) — source review of all `skills/*/SKILL.md` entrypoints.

## Perception & sensing

- [Perception service (DL backend)](perception-service.md) · [VI](vi/perception-service_vi.md) — cloud GPU inference, load balancer, encryption, models. Service docs: [`integrations/perception-service/docs`](../integrations/perception-service/docs/README.md).

## Safety & security

- [Safety engine](safety.md) · [VI](vi/safety_vi.md) — deterministic enforcement of `SAFETY.md` bounds below the agent.
- [Security audit checklist](security/CHECKLIST.md) — consolidated status of the three audits below.
- [Go server audit](security/go-server-audit.md) — findings for the Go server.
- [Local-only API boundary](security/local-only-boundary.md) — local-only boundary for server / HAL / agent runtime.
- [Web frontend audit](security/web-frontend-audit.md) — findings for the web frontend.

## Development & CI

- [Multi-IDE development](DEV-MULTI-IDE.md) — Cursor + Claude Code conventions, including doc-update rules.
- [Continuous integration](ci.md) · [VI](vi/ci_vi.md) — what CI runs and how to run the gates locally.

## Plans & roadmap

- [Developer platform roadmap](dev-platform-roadmap.md) · [VI](vi/dev-platform-roadmap_vi.md) — SDK, MCP server, CLI and skill marketplace plan.
- [Not built yet — claim one](not-built-yet.md) — open `claim-me` work items for contributors.
- [Buddy MQTT mobile handoff prompt](vi/buddy-mobile-handoff_vi.md) — VI only; implementation prompt for the mobile app side of Buddy MQTT.

## Reference & marketing

- [Product page](autonomous-os-product-page.html) — standalone HTML product page "AI, out in the world" (hero animation: [`media/hero.gif`](media/hero.gif)).

## Robots, integrations and companions

- [Robot contract](../robots/contract/ROBOT-SPEC.md) — `ROBOT.md` spec, plus [COMPATIBILITY](../robots/contract/COMPATIBILITY.md), [SAFETY-SPEC](../robots/contract/SAFETY-SPEC.md), [capabilities](../robots/contract/capabilities.md), [WIFI-BODY](../robots/contract/WIFI-BODY.md) · [VI](../robots/contract/vi/WIFI-BODY_vi.md), and the [compliance tests](../robots/contract/cts/README.md).
- [Lamp docs](../robots/lamp/README.md#docs) — LED, sensing, habits, motion, vision, physical controls, debug playbooks.
- [Reachy Mini docs](../robots/reachy-mini/docs/) — [runtime](../robots/reachy-mini/docs/runtime.md), [recovery](../robots/reachy-mini/docs/recovery.md), [first-boot plan](../robots/reachy-mini/docs/first-boot-plan.md), [Pollen ecosystem](../robots/reachy-mini/docs/pollen-ecosystem-analysis.md) (VI in [`docs/vi/`](../robots/reachy-mini/docs/vi/)).
- [Perception service docs](../integrations/perception-service/docs/README.md) — API, architecture, deployment, troubleshooting.
- [Autonomous Buddy docs](../integrations/companions/autonomous-buddy/README.md#docs) — Mac companion app design, workspace, signing.
