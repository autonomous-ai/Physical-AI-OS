# Voice development coordination

The owner has prioritized complete voice interaction and requested two external
Claude Code sessions alongside Codex. This is an ownership plan, not proof of
the other sessions' implementation or test status. Use `lamp-v2-chat` as the
shared integration branch; identify the current checkpoint from its Git history.
Keep each workstream in a separate checkout and preserve local uncommitted work
when incorporating a newer shared checkpoint. Continue development on workstream
branches. Codex integrates reviewed commits into the shared branch; avoid
concurrent direct pushes that can change the source under another agent's gates.

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

## Next 24-hour assignments

Agent 1 continues provider reliability from pushed commit `fbf9ecc3c` on
`gemini-voice`: playback-paced 60–180-second answers, maximum-size bursts and
flow-control timeout behavior first; then pause/resume and repeated interruption,
explicit outage/recovery/turn-failure contracts, narrow service probes when
access permits, and Linux/ARM64 verification. Codex integrates coordinator
contract changes. No accepted audio may be silently lost or replayed twice.

Agent 2 delivered the Rust voice acceptance runner through `3eaa0365a`, with
42 scenarios, 21 evaluator canaries, fake-runtime fault injection, an attempt
ledger and per-attempt reports. Root integration preserves recording verification
and answer-quality accounting, and fixes prompted-human scoring and historical
plan compatibility. Continue hardening the relay and exercise real coordinator
events using the integrated [directed-mode cue interface](live-session-cues.md).
The interface is host-tested; physical qualification remains pending.
Cover quick/long replies, pauses, topic changes, repeated interruption, background
and multi-speaker speech, other-device speech, noise, failures/recovery and
ring/voice consistency. Rank reproducible bugs and retain failed and
expected-silence trials. Software timestamps are not acoustic measurements.
Physical direct-human and speaker-replay cohorts stay separate; device work
waits for restored access. See the runner's [integration proposals](voice-eval.md).

A separate runner-hardening item remains: the relay's `BufRead::lines()` reads
an entire line before `truncate(MAX_LINE_BYTES)`, and the Mac relay reader also
uses `lines()`. The stated 64 KiB line limit therefore does not bound allocation;
byte-index truncation can also land inside a UTF-8 character. Agent 2 should add
bounded incremental framing with explicit oversized/invalid-line failures and
regressions before calling the physical relay hardened. This does not affect the
provider-credit IPC contract and is not silently fixed in the reporting patch.

## Current contracts relevant to both sessions

- Agent 1 delivered Gemini reliability at `fbf9ecc3c`; see its
  [integration handoff](gemini-session-reliability.md). Its reported verification
  is macOS/scripted-service only, not cloud or device qualification.
- Agent 2 delivered ten commits from `186253939` through `3eaa0365a`. Preserve
  that history and its scenario/ledger format. The production provider-credit
  protocol does not change its CLI-level runner interface. Review fixes must
  keep invalid trials visible and require actual recording evidence before
  reporting acoustic results. Those two review defects are now fixed; see
  [answer accounting](voice-eval-answer-review.md) and
  [recording verification](voice-eval-recording-evidence.md). The earlier
  696-test host gate is documented in [provider flow control](provider-flow-control.md);
  the newer source is qualified separately in [integration evidence](voice-eval-integration-20261011.md).
- [Provider output credits](provider-flow-control.md) add the strict control
  command `ProviderOutputCapacity { through }`. Count every provider packet,
  preserve counters across startup/turns/recovery, and return credits according
  to actual coordinator capacity. Provider tests/fixtures must speak this
  protocol. Do not overwrite the sender or retired-output pruning while
  integrating new Gemini work. The upstream two-second delivery timeout needs
  a real playback-paced regression; fast burst draining is insufficient.

- A Gemini request can have [multiple playback occurrences](playback-occurrences.md).
  Do not equate generation completion or one segment's retirement with final
  turn completion. The coordinator serializes the speaker's final cursor and
  emits `playback_sequence` on both start/retirement observations. Normal
  `turn_finished` adds top-level `generation`; `owner` remains cancellation.
  Current runner follow-ups use the new `turn_completed` cue and retain exact
  outcome/gap metadata. Update producer and consumer together; preserve older
  plan hashes and original `SpeechRetired` semantics.

- [Ring renewal retention](ring-choreography.md#bounded-renewal-evidence) keeps
  exact transition/periodic heads and last-request samples plus summary counts.
  `ring_requested` with `trace_role: renewal_tail` samples an already-counted
  command. Count head requests plus summary `requested`, without adding tails.
  Fixed trace slots can update their original timestamps past interleaved
  records; use those timestamps, not array position. Actor commands and receipt
  validation remain unchanged; summary counts are not optical evidence.

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
