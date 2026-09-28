# AGENTS.md

This file provides guidance to Codex and other coding agents when working in
this repository. Treat `CLAUDE.md` as the upstream source of truth; this file is
the Codex-compatible mirror of those project rules.

## Multi-IDE Rules

This repo is developed across multiple AI-assisted environments. The following
rules apply to all code changes:

1. **Update docs on code change** - When changing behavior, architecture, or
   APIs, update both the English and Vietnamese docs. Keep numbers, flows,
   endpoints, and states accurate with the code. Platform docs are in `docs/`;
   lamp-specific docs are in `robots/lamp/docs/`.

   **Platform docs** (`docs/` + `docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | os-server, API, startup | `docs/os-server.md` | `docs/vi/os-server_vi.md` |
   | Setup flow, provisioning | `docs/setup-flow.md` | `docs/vi/setup-flow_vi.md` |
   | Web UI, configuration pages | `docs/web-ui.md` | `docs/vi/web-ui_vi.md` |
   | Flow Monitor (turn pipeline, JSONL, SSE) | `docs/flow-monitor.md` | `docs/vi/flow-monitor_vi.md` |
   | Overall structure | `docs/overview.md` | `docs/vi/overview_vi.md` |
   | MQTT, dispatch, publish | `docs/mqtt.md` | `docs/vi/mqtt_vi.md` |
   | OTA, bootstrap | `docs/bootstrap-ota.md` | `docs/vi/bootstrap-ota.md` |
   | Speech emotion recognition (SER) | `docs/speech-emotion.md` | `docs/vi/speech-emotion_vi.md` |
   | Realtime voice agent (HAL `realtime`, Gemini Live / OpenAI Realtime, delegate) | `docs/realtime-voice.md` | `docs/vi/realtime-voice_vi.md` |
   | Perception service (cloud DL inference), load balancer, encryption, models | `docs/perception-service.md` | `docs/vi/perception-service_vi.md` |
   | Hermes agent backend (`agent_runtime`, runtimes/hermes) | `docs/agentic/hermes.md` | `docs/vi/agentic/hermes_vi.md` |
   | PicoClaw agent backend (`agent_runtime`, runtimes/picoclaw, WebSocket) | `docs/agentic/picoclaw.md` | `docs/vi/agentic/picoclaw_vi.md` |
   | Adding/changing an agentic backend (AgentGateway contract, switch, install/presync, migration, skills, hooks, reset) | `docs/agentic/adding-agent-runtime.md` | `docs/vi/agentic/adding-agent-runtime_vi.md` |
   | Safety engine (SAFETY.md bounds, deterministic enforcement gate) | `docs/safety.md` | `docs/vi/safety_vi.md` |
   | Device telemetry (adding a tracker, `hal/telemetry`, `system/telemetry`, `/api/telemetry/event`) | `docs/telemetry.md` | `docs/vi/telemetry_vi.md` |
   | Voice response metrics (`hal/telemetry`, `system/telemetry`, `/api/telemetry/event`) | `docs/voice-metrics.md` | `docs/vi/voice-metrics_vi.md` |

   **Lamp-specific docs** (`robots/lamp/docs/` + `robots/lamp/docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | LED, effects, states, animations | `robots/lamp/docs/led-control.md` | `robots/lamp/docs/vi/led-control_vi.md` |
   | Sensing behavior, sound escalation, reactions | `robots/lamp/docs/sensing-behavior.md` | `robots/lamp/docs/vi/sensing-behavior_vi.md` |
   | Sensing threshold tuning | `robots/lamp/docs/sensing-tuning.md` | `robots/lamp/docs/vi/sensing-tuning_vi.md` |
   | Habit tracking, pattern building, habit-aware nudge phrasing | `robots/lamp/docs/habit-tracking.md` | `robots/lamp/docs/vi/habit-tracking_vi.md` |
   | Vision tracking, object follow, servo track | `robots/lamp/docs/vision-tracking.md` | `robots/lamp/docs/vi/vision-tracking_vi.md` |
   | Physical controls (GPIO button, TTP223 touchpad, gestures, pet response) | `robots/lamp/docs/physical-controls.md` | `robots/lamp/docs/vi/physical-controls_vi.md` |
   | Autonomous Buddy (Mac companion app) | `integrations/companions/autonomous-buddy/docs/autonomous-buddy.md`, `integrations/companions/autonomous-buddy/docs/autonomous-buddy-mvp.md`, `integrations/companions/autonomous-buddy/docs/release-signing.md` | `integrations/companions/autonomous-buddy/docs/vi/autonomous-buddy_vi.md`, `integrations/companions/autonomous-buddy/docs/vi/autonomous-buddy-mvp_vi.md`, `integrations/companions/autonomous-buddy/docs/vi/release-signing_vi.md` |
   | Security test checklist | `robots/lamp/docs/security-test.md` | _(no vi version)_ |

2. **Comments in English** - Project standard.
3. **Code is the single source of truth** - Docs reflect code, not the other
   way around.
4. **Do not commit binary artifacts** - Version is injected via ldflags at
   build time.

5. **Web/TSX lint is a required quality gate** - For every change under
   `system/web/`, work freely on the implementation but finish with both
   `npm run lint` and `npm run build` from `system/web/`. Do not hand off a
   change with ESLint warnings or errors, TypeScript errors, or a skipped
   validation command. Fix React Hook diagnostics by restructuring the code;
   do not silence them with `eslint-disable` unless the exception is narrowly
   scoped, documented in English with why it is safe, and has no practical
   compliant alternative. Report the exact commands run and any limitation.

See `docs/DEV-MULTI-IDE.md` for full conventions.

## Working Style

- The user reviews and commits by hand. Do not create commits unless explicitly
  asked. When asked: `git add` the exact files of that change (never `git add -A`),
  check `git status --short` for unrelated dirty or untracked files and leave them
  alone, then commit with a single-line message and **nothing else in it** — no
  `Co-Authored-By` trailer, no "Generated with" footer, no body. Commit only; do
  not push unless told to.
- Work in small, reviewable chunks. When a task spans multiple concerns, split
  it by concern and verify each batch before moving to the next.
- Stay in scope. Flag unrelated issues instead of fixing them opportunistically.
- Verify with concrete evidence such as focused tests, builds, greps, `bash -n`,
  or compile checks. Report what was and was not verified.
- Do not "clean up" inherited drift such as unrelated gofmt churn, duplicate
  dependency metadata, or upstream-preserved style unless it is required for the
  task.
- Respond to the user in Vietnamese unless they request otherwise.
- Do not auto-deploy to devices. Default to repo changes plus local verification;
  any on-device SSH/SCP/restart step is opt-in and must be confirmed first.

### Waiting for tools and background jobs

**Avoid model turns that only ask whether a job has finished.**

- Follow the active tool's completion contract. If it explicitly delivers the
  result automatically, do independent work or use its supported yield/wait
  mechanism. End a turn to await delivery only when the tool explicitly
  guarantees that it will resume the task; never assume a wakeup.
- Do not use shell `sleep` loops, repeated `ps` / `pgrep` / `top`, or repeated
  log/status reads to wait for a job that already has a completion mechanism.
  Targeted process/log inspection is still appropriate to diagnose a failure
  or a suspected stall.
- Retain the returned handle and use its matching continuation tool. In Codex,
  an `exec_command` session ID uses `write_stdin`; a yielded `functions.exec`
  cell ID uses `functions.wait`. For agents/tasks, use the available native
  wait tool and preserve any returned cursor. These handles are not
  interchangeable, and receiving one does not mean the job succeeded.
- For a long-running job, use a bounded wait suited to its expected duration
  (normally 30-60 seconds, within the active tool and communication limits).
  Avoid repeated zero/short-timeout checks; read only new output and report
  meaningful progress instead of narrating unchanged status.
- When an external job has no notification or native wait support, use a
  bounded watcher or read-only polling with backoff and an overall deadline.
  Do not rerun the original operation just to obtain its status.
- Inspect the final output and exit code or terminal status before claiming
  completion. A timeout, background handle, or quiet log is not success.

This governs coding-agent orchestration. It does not remove application/API
polling, test synchronization, or service-readiness checks required by their
own contracts.

## Parallel Work / Subagents

When work can be split across independent, file-scoped tasks, use available
parallelism instead of doing everything sequentially. In Codex, prefer
`multi_tool_use.parallel` for independent local reads/checks, and use subagents
only when the tool is available and the overhead is justified.

Common cases in this repo:

- Repetitive edits across many files, such as rebranding strings across EN + VI
  docs: split by file or language, with exact rules and a verification grep.
- Long-running builds or cross-compile checks, such as `swift build` or
  `GOOS=linux GOARCH=arm64 go build`: run in parallel/background when possible
  and continue with independent work.
- Repo-wide audits, such as stale paths after folder moves or broken cross-refs:
  use audit-only scope unless edits are explicitly part of the task.
- Independent English and Vietnamese doc updates after a code change: keep both
  sides consistent and verify matching numbers, endpoints, states, and flows.

Rules:

- Brief any delegated worker with goal, context, exact files/scope, verification
  step, and concise report format.
- Do not delegate when the overhead is larger than the work itself, especially
  for one or two quick edits in files already open.
- Trust but verify: spot-check actual diffs and run focused greps/tests before
  considering delegated work done.

## Device Access Rules

- Always ask the user before running any `sshpass` or `ssh` command to the Pi.
  Do not SSH automatically.
- Pi SSH: `ssh pi@<IP>` (credentials stored in the team password manager; IP
  varies per session).

## Harness integration context

Before changing `skills/harness-use`, `system/harness` or the OS Harness UI/API, read
[the integration context and ownership](docs/harness.md) and its
[Vietnamese counterpart](docs/vi/harness_vi.md). Harness app/device support is
maintained by a separate team in `autonomous-harness` and
`autonomous-harness-desktop`. This repo owns the OS client and skill only; keep
implementation edits here. Verify cross-repository contract changes against the
Harness team's current code and coordinate them rather than inventing another
pairing flow, transport or credential. Buddy stays independent.

## Project Overview

Autonomous is an open-source OS for physical AI agents. The Go backend
(`system`) provides device onboarding (WiFi, LLM provider, messaging
channel setup), OTA updates, and agent gateway integration. The brain is a
swappable agentic runtime (OpenClaw, Hermes, or any LLM + skills + memory).

**Go module:** `go.autonomous.ai/os` (rooted at repo root — covers `system/` and `runtimes/`) | **Go 1.24** | **Target:** Linux ARM64

## Build & Development Commands

All targets run from the repo root via the top-level `Makefile`.

```bash
# Build Go services (cross-compiles to linux/arm64)
make os-build                # Builds os-server binary
make os-build-bootstrap      # Builds bootstrap-server binary

# Code generation (Google Wire DI)
make os-generate             # Runs from repo root: GOFLAGS=-mod=mod go generate ./...

# Lint + tests (Go)
make os-lint                 # golangci-lint run (repo root, covers runtimes/)
make os-test                 # go test ./... (repo root, covers runtimes/)

# HAL (Python hardware runtime, hal)
make hal-dev                 # Install deps + run HAL locally
make hal-lint                # Catch broken local imports + undefined names
make hal-test                # Run HAL tests

# Web frontend (React/Vite/Tailwind in system/web)
make web-install             # npm install
make web-dev                 # Vite dev server
make web-build               # Production build to dist/
cd system/web && npm run lint  # Required after every web/TSX change
cd system/web && npm run build # Required type-check + production build
```

Go version is injected at build time via ldflags. HAL/web versions live in
`system/VERSION_OS_SERVER` and `hal/VERSION_HAL` and are auto-bumped by the
`make upload-*` release targets — do not hand-edit for releases.

## Architecture

### Two Executables

- `system/cmd/os-server/main.go` - Main HTTP API server (Gin). Handles device
  setup, network management, LED control, health checks, and agent gateway
  integration.
- `system/cmd/bootstrap/main.go` - OTA bootstrap worker. Periodically
  checks for and applies updates.

### Dependency Injection

Uses Google Wire for compile-time DI. After changing provider signatures, run
`make os-generate` to regenerate `wire_gen.go` files.

### Package Layout

**Agentic runtimes - `runtimes/` (repo root):** swappable backends,
one folder per brain: `runtimes/{openclaw,hermes,picoclaw,codex,claudecode}`.
Selected by `system/agent` (AgentGateway factory).

**Go backend - `system/` (single Go module rooted at the repo root):**

- `system/<domain>/` - System managers, one folder per diagram chip (ambient,
  beclient, buddy, device, healthwatch, intent, monitor, network, skills,
  statusled, vision) plus `system/agent/` (AgentGateway factory + migration).
- `system/server/` - HTTP layer: Gin router, handlers by domain
  (`delivery/http/handler.go` convention); `server/serializers/`,
  `server/config/`.
- `system/bootstrap/` - OTA worker: metadata fetching, update execution,
  state persistence.
- `system/domain/` - Shared data structures.
- `system/lib/` - Shared libraries (mqtt, core/system, i18n, logger, hal HAL
  client, safego, ...).
- `system/web/` - React 19 + TypeScript + Vite + Tailwind CSS 4 SPA.

**HAL - `hal/` (Python hardware runtime, FastAPI on :5001):**

- `drivers/` - Hardware drivers by subsystem (rgb, motors, voice, sensing,
  display, gpio_button, ...).
- `board/` - Per-board profiles (pin maps, debounce).
- `routes/` - FastAPI route modules (servo, led, camera, audio, emotion, ...).

**OS-level dirs (repo root):** `skills/` (agent skills), `robots/` (per-device
declarations + docs; `robots/contract/` device specs, `robots/contract/cts/`
compliance tests), `scripts/imager/` (OrangePi image build), `scripts/` (setup +
OTA upload), `integrations/perception-service/`, `integrations/companions/`.

### API Response Format

All HTTP endpoints return:

```json
{"status": 1, "data": {}, "message": null}
```

on success, and:

```json
{"status": 0, "data": null, "message": "error"}
```

on failure.

### Configuration

Config lives in `config/config.json` (path relative to the os-server working
dir) and is managed by `system/server/config/config.go`. It supports a
notification channel for config change propagation.

## Coding Standards

### Error Handling

```go
if err != nil {
    return fmt.Errorf("operation: %w", err)
}
```

Always wrap errors with useful context.

### Logging

```go
log.Println("[component] message")
log.Printf("[component] formatted %v", value)
```

### Goroutines

Always use `context.Context` for cancellation. Background goroutines must
respect `ctx.Done()`.

### Validation

Use `go-playground/validator` for struct validation. Validate at the HTTP
handler level before passing data to services.

### Naming (paths under `system/`)

- Handlers: `server/<domain>/delivery/http/handler.go`
- Services: `<domain>/service.go` (system managers live at `system/<domain>/`, e.g. `ambient/service.go`)
- Wire providers: `server/wire.go`, `bootstrap/wire.go`
- Domain types: `domain/<type>.go`
