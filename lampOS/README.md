# lampOS

A standalone Rust runtime for natural conversation with a physical Lamp beside
one person at a desk.

**Status: V2 has completed finite real-Lamp voice pilots and a physical
cancellation-clock regression. False interruptions from playback remain
unresolved. The latest terminal shutdown fix passed 518 native ARM64 tests,
strict Clippy, formatting and release build; its physical rerun is pending.
There is no validated V1-main/V2 p50, p95 or speedup. The installed legacy
runtime remains the rollback; full choreography, sensors and social
interaction qualification remain.**

Read the [Claude Code handoff](HANDOFF.md) for the exact source, device,
evidence, reusable test assets and unfinished work. Codex is pausing at the
owner's request to conserve credits.

See [benchmark progress](docs/benchmark-progress.md) for the retained trials and current blockers.

Treat this directory as the repository root. It must build, test, install and
run after extraction, without source files or services from sibling projects.
All code comments, documentation and notes are English. Read the
[project rules](AGENTS.md), [release scope](docs/architecture.md), and
[hardware port contract](docs/hardware-contract.md), and
[V1-main versus V2 comparison protocol](docs/comparison-protocol.md).

Startup, configuration, supervision, hardware control and interaction logic
belong here in Rust. Keep dependency declarations, model asset management,
board setup, installation and any future UI/update components inside this
project. External Rust crates, Linux drivers and Gemini remain declared
infrastructure dependencies. Provision credentials and per-device calibration
explicitly; never embed secrets or silently depend on a legacy configuration.

The first release is live chat with coordinated voice, head, body and light,
using camera and sensor observations. Agentic tasks, new long-term memory,
legacy web UI and fleet management are outside that release scope. The existing
implementation remains available for validation rollback until V2 qualifies.

## Build and check

Use the pinned Rust toolchain in `rust-toolchain.toml`. Linux audio builds also
need ALSA development headers and pkg-config. macOS runs the portable core and
fixture tooling; Linux hardware modules are compiled and tested on Lamp.

```sh
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo fmt --all -- --check
```

## Current components

| Crate | Responsibility |
|---|---|
| `lamp-interaction` | Immutable turn/output ownership, cancellation, privacy lineage, readiness and observation freshness. |
| `lamp-ipc` | Private bounded Unix datagrams, shared monotonic timestamps and a separate-process control latency probe. |
| `lamp-audio` | Pure Rust Sonora AEC3/noise processing in 10 ms blocks; privacy-checked Linux ALSA capture and guarded speaker output. |
| `lamp-ring` | Fixed-size WS2812 encoding, bounded brightness and ownership-checked Linux SPI writes. |
| `lamp-camera` | Bounded frame lifecycle, privacy, latest-frame delivery and Linux V4L2 backend; native-qualified code, physical camera qualification pending. |
| `lamp-motor` | Read-only five-servo protocol, bounded status parsing and per-unit calibration math; no bus access or movement yet. |
| `lamp-acoustic` | Fixed desk scenarios, cached voices/noise mixes and asset integrity checks. |
| `lamp-gemini` | Bounded Gemini Live WebSocket transport, explicit activity, immutable response ownership and cancellation barriers. |
| `lamp-live` | Finite directed qualification with separate capture, speaker, physical privacy and provider processes. |
| `lamp-observer` | Mac room recording and native microphone permission preflight; explicit cached iMac-speaker playback with delivery/failure evidence. |

See the [directed runtime](docs/live-runtime.md),
[room observer](docs/room-observer.md), [motor contract](docs/motor-runtime.md), and [native component qualification](docs/component-qualification.md) for
frozen-source checks and measurements. Component tests do not establish
end-to-end conversation, acoustic latency,
addressee accuracy, physical privacy controls or motor safety.

## Reusable qualification tools

```sh
cargo run -p lamp-acoustic -- render .cache/acoustic .cache/render-first.json
cargo run -p lamp-acoustic -- verify .cache/acoustic
cargo run --release -p lamp-ipc --bin lamp-ipc-probe -- --samples 1000
cargo run --release -p lamp-audio -- bench-aec 6000
```

Keep `.cache/acoustic/` when cleaning build output. Rendering uses installed Mac
voices and reuses verified audio; it does not require cloud TTS or an LLM.
Use a new report path for every render. The IPC probe measures control roundtrip
between processes with a separate saturated bulk queue. The audio benchmark
measures synthetic DSP processing and signal integrity. Neither is an acoustic
speech-to-answer measurement or a completed live Lamp test.
