# Voice development coordination

The owner has prioritized complete voice interaction and requested two external
Claude Code sessions alongside Codex. This is an ownership plan, not proof of
the other sessions' implementation or test status. Use `lamp-v2-chat` as the
shared integration branch; identify the current checkpoint from its Git history.
Keep each workstream in a separate checkout and preserve local uncommitted work
when incorporating a newer shared checkpoint.

| Workstream | Primary ownership | Expected deliverable |
|---|---|---|
| Codex integration | Microphone/AEC, input admission, `crates/live/src/coordinator.rs`, interaction ownership, choreography and final integration | Reliable local listening/cancellation, synchronized output, reviewed integration and physical qualification |
| Claude Gemini reliability | `crates/gemini/**`, `crates/live/src/provider_worker.rs`, focused tests/docs | Complete answers, follow-ups, stale-event isolation and bounded failure/recovery behavior |
| Claude voice evaluation | New `crates/voice-eval/**`, associated docs and necessary scenario additions | Executable Rust conversation runner, event-driven overlap/follow-up tests, complete attempt ledger and honest measurements |

Propose shared Cargo/API changes separately for integration. Do not overwrite
another workstream's files or assume its uncommitted changes are in HEAD. Reuse
`crates/acoustic`, `crates/observer`, `fixtures/desk-v1.json` and existing cached
audio. Production/tooling code remains Rust and standalone inside lampOS.
Environmental acquisition is deferred behind voice completion; Harness, agentic
tasks and new long-term memory are outside this first release.

## Current contracts relevant to both sessions

- Opt-in audio diagnostics add optional `aec_internal_alignment_ms`; replay
  preserves it and emits separate `replayed_internal_alignment_ms` values.
  This is cached AEC buffer state, not confidence or an admission signal. The
  microphone/coordinator protocol and provider behavior are unchanged. See
  [AEC alignment evidence](aec-alignment.md).

- [Input admission](input-admission.md) separates candidates from destructive
  turn replacement. Directed mode still immediately accepts VAD-only activity.
  It has no qualified speaker/echo/addressee classifier and cannot establish
  multi-speaker restraint. `input_candidate` and `input_candidate_rejected` are
  new trace events; `input_admitted` adds candidate ID and admission basis.
- Gemini's automatic activity detection is disabled. Explicit `activityStart`
  requests interruption. Its response is not independent confirmation that a
  human addressed Lamp. Input transcripts remain session-scoped/unreliable for
  per-turn attribution. A late transcript must not target the newest candidate.
- Preserve original capture times, privacy generations and ownership through
  asynchronous work. Old callbacks must not borrow a newer turn's authority.
- Accepted PCM and software cancellation receipts are not acoustic onset/mute
  measurements. Follow [the comparison protocol](comparison-protocol.md),
  retain failed and expected-silence attempts, and leave missing evidence unscored.
- Cached playback tests and synthetic overlaps cannot replace a direct-human
  cohort. No clean physical V2 positive-overlap cohort has been located yet.
- The latest owner-directed scan did not find lamp-4ace. Physical work is
  deferred until the owner's office check; do not treat a local test as a new
  device result. No movement is allowed without fresh placement/clearance proof.

Before integration, provide exact changed files, formatting/strict Clippy/test
commands and results, measured boundaries, known failures and required physical
checks. Distinguish host verification from Linux/ARM64 compilation and device
qualification. The owner has authorized Codex to push the verified shared
checkpoint; do not infer permission for deployment or a fleet release.
