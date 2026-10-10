# lampOS development rules

Treat this directory as the root of a standalone repository. Follow the
[V2 live interaction scope](docs/architecture.md).

- First release: chat with a physical robot beside one person at a desk.
  Lamp is fixed on the left or right; calls and visiting colleagues are negative
  and mixed-attention test cases.
- Implement Lamp-owned runtime code in Rust. Do not introduce Python services,
  scripts or adapters into lampOS, or require Python HAL for the release.
  Native DSP/inference dependencies require explicit interfaces and review.
- Reuse tested protocol semantics, calibration data, safety rules and regression
  fixtures. Do not copy the old callback ownership or shared execution model.
  Confirm units and behavior on actual hardware before accepting a port.
- Before designing a new subsystem, inspect the corresponding V1 behavior and
  current maintained alternatives. Record keep/change/reuse decisions and their
  evidence in [reuse decisions](docs/reuse-decisions.md). Reuse suitable Rust
  libraries; rewriting runtime policy does not require reimplementing every
  dependency. Compare failure behavior and measured workloads before replacement.
- Keep capture and interruption independent of cloud, vision inference and
  presentation. Bound queues, buffers, worker counts and retry work.
- One interaction owner coordinates voice, head, body and ring. Validate
  ownership, freshness, privacy and cancellation at the final output boundary.
  Keep exactly one writer for each actuator bus.
- Enforce motor range, velocity and cancellation locally. Gemini may propose
  intent; it cannot bypass safety or directly set unchecked joint targets.
  A frame showing the actual placement and clearance is required before motion.
- Stillness and silence are valid. No automatic room scans, stock spoken
  fillers, mandatory gestures, fabricated visual facts or forced eye contact.
- Camera and microphone privacy controls remain authoritative. Never show
  listening readiness unless input can actually be retained.
- Keep Harness, agent sessions, browsing and new long-term memory infrastructure
  out of this release.
- Owner instruction: all code comments, documentation, and notes are English
  only. Keep documentation in this directory, normally `docs/`. This explicit
  instruction overrides any inherited translation requirements.
- Keep the build, tests, configuration, startup, deployment tooling and runtime
  self-contained. No required source files, path dependencies, configuration
  files or running services from sibling directories. Ordinary external Rust
  crates, Linux drivers, model assets and cloud APIs remain explicit dependencies.
- This is a full runtime replacement. Do not ship a bridge, hidden fallback, or
  permanent dependency on the legacy implementation. A clean installation must
  run while legacy HAL and os-server are stopped. Keep the old system only as
  a temporary validation/rollback option; do not delete it before V2 qualifies.
- Own all required setup, dependency declarations, model asset management,
  configuration, driver setup and future UI/update components inside lampOS.
  Build and deployment must work with this directory as the repository root.
- Implement every responsibility required by the V2 runtime in Rust, including
  functionality currently split across Python HAL and Go coordination. A
  dependency on the legacy HAL or os-server is not a standalone implementation.
- Prove portability by building and testing a source-only copy outside the
  parent repository. Keep credentials and per-device calibration separate from
  source; document provisioning without embedding secrets.
- With Rust implementation changes, run formatting, clippy and applicable tests.
  Replays and unit tests do not establish acoustic latency or motor safety.
  Report real-device measurements separately, with exact boundaries.
- Do not commit, push or deploy without the authorization required by the
  repository and current conversation.
