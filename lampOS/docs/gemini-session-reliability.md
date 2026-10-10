# Gemini session reliability

Scope: `crates/gemini` and `crates/live/src/provider_worker.rs`. Everything here
is verified on the macOS host against a scripted in-memory service. **No cloud
request, Linux/ARM64 build, physical device or room audio is part of this
evidence.** Passing these tests does not establish natural voice interaction;
it establishes that the listed transport failures no longer occur under the
listed service behavior. Service behavior that could not be verified offline
is called out in [Unverified service behavior](#unverified-service-behavior).

## Failures reproduced on the original transport

Each row was reproduced against the unmodified code at commit `64529dee4` with
a scripted service, then fixed. The transport is identical at shared checkpoint
`23bde4873`. "Ends the session" meant the provider worker exited and the whole
run failed.

| Scenario | Original behavior | Behavior now |
|---|---|---|
| Delayed `interrupted` arrives while the person speaks the next question | The new question was cancelled (`Interrupted` for the new request), then the session ended with `BarrierTimeout` | Dropped as `LateInterruption`; the question is unaffected |
| Duplicate completion arrives while the next question is being spoken | Session ended with `UnexpectedResponse` | Dropped as `LateTerminal` |
| Stray `interrupted` + completion arrive after the next question ended | The new question was completed with no answer | Both dropped; the real answer is delivered |
| Output transcript trails its answer's completion | Session ended with `UnexpectedResponse` | Attributed to the request that spoke it, never to the next one |
| 400 chunks arrive at once after a network stall | Session ended with `Backpressure` after 128 messages; 0 audio events delivered | All 400 delivered in order |
| Interruption whose confirmation takes longer than 0.8 s | Session ended after 803 ms and 70 blocks | Audio is not held; only the barrier bound applies |
| Answer still arriving after the response deadline | Cut with `ResponseTimeout` after 2 of 9 chunks | Deadline applies to the first output only |
| Service closes right after a complete answer | The answer's audio was dropped; only its lifecycle events were delivered | Audio, generation and completion are delivered, then the end |
| Second interruption before the first is confirmed | Session ended with `OverlappingInput` | The waiting request is replaced |
| `goAway` during an answer | Session ended at once | The answer completes, then the connection is replaced |
| Any connection loss, including the idle close | Worker exited; run failed | Recovered when nothing accepted can be lost or repeated |

V1 measured the service closing a session nobody talks to after 86-198 s with
WebSocket code 1008. That alone ended a V2 run after any long pause.

## Request ownership

Provider activity detection is disabled, so only this client's explicit
`activityStart` can interrupt a response, and the service cannot answer an
activity before its `activityEnd`. The transport uses exactly those two facts.

- **Interruption is requested, never observed.** `interrupted` is honored only
  for a request this client has already retired. For any other request it is
  the delayed result of an earlier `activityStart`. It is dropped and reported
  as `Discard::LateInterruption`. It cannot cancel a request and it is not
  evidence that anyone spoke.
- **Nothing is owed before `activityEnd`.** Audio, text, generation or turn
  completion that arrives while the current request is still input belongs to
  the past and is dropped (`LateOutput`, `LateTerminal`).
- **No owner, no output.** Output with no request in progress is dropped
  (`UnownedOutput`) instead of ending the session.
- **A stray pair stays a pair.** After a dropped `interrupted`, a completion
  that follows within the barrier bound and before any output of the current
  request is dropped as its companion.
- **Output transcripts trail.** The service does not order them against audio
  or completion. One that arrives before the current request has produced
  output is attributed to the previous responder; the receiver then drops it if
  that request is retired. Input transcripts remain session-scoped and are
  never attached to a request.
- **Idle barrier.** A new request admitted while an earlier response is
  unfinished sends `activityStart` and its audio immediately. Only its
  `activityEnd` waits for the earlier response's idle completion, so any output
  before that point is provably old. Nothing is buffered, so there is no
  held-audio size or age limit to exceed.
- **Cancelled input is not committed.** A request retired before its end is
  dropped without `activityEnd`. The activity stays open and the next admitted
  request continues it; its audio is not sent again and no response is
  requested for it.

One ambiguity remains: a lone stale completion that arrives after the newer
request ended and before its first output cannot be told apart from an empty
answer. It completes that request with no audio, which the coordinator records
as `no_audio_answer`. The service sends no correlation identifier that would
resolve this.

## Bounds

| Bound | Value | On expiry |
|---|---|---|
| Event queue to the worker | 16 events | Socket reads stop; service is held by transport flow control |
| Decoded events awaiting the queue | one message, at most 32 events | `Backpressure` |
| Consumer not taking events (`deliver`) | 2 s | `Backpressure` |
| Worker IPC backlog high-water | 384 packets (hard bound 512) | Worker stops taking provider events |
| Interruption to idle barrier (`barrier`) | 2 s (was 0.8 s) | `BarrierTimeout` |
| Ended input to first output (`response`) | 30 s (was 90 s to completion) | `ResponseTimeout` |
| Silence between outputs before generation completes (`stall`) | 10 s | `StalledResponse` |
| Idle completion after generation (`completion`) | received audio duration + 15 s | `CompletionTimeout` |
| Ended input to idle completion (`turn`) | 300 s | `ResponseTimeout` |

The service withholds the idle completion while it assumes generated audio is
still playing, so `completion` scales with the audio received. The old single
90 s bound from ended input to completion would have ended any answer whose
playback ran past it.

While the worker is behind, the transport stops reading the socket rather than
dropping or failing. Commands, local retirement and shutdown stay live; only
cloud output waits. One server message is handled per scheduling turn, so a
burst already in the socket buffer cannot starve the worker's 2 ms control
tick. Local retirement remains synchronous and never waits for the network.

## Connection loss

`lamp_gemini::Supervisor` owns one connection at a time and reports what the
accepted request had reached when it ended.

| Request state at loss | Result |
|---|---|
| None in flight | Reconnect |
| Cancelled locally | Reconnect |
| Generation complete, all output delivered | `Settled`: the closed connection is the idle barrier; the worker emits `TurnComplete { idle: true }`; reconnect |
| Input still open | `TurnLost { InputOpen }` |
| Input ended, no output | `TurnLost { AwaitingResponse }` |
| Output began, generation incomplete | `TurnLost { Responding }` |

**Nothing is replayed.** Input is never re-sent and no answer is requested
twice. A lost request's identifier is retired across the reconnect. Output that
arrived before the failure is still delivered, in order, before the loss is
reported; a local shutdown or privacy stop still drops queued output.

The worker treats `TurnLost` as fatal and exits naming the request and stage,
because the coordinator contract has no per-request failure report yet (see
[Proposed coordinator contract](#proposed-coordinator-contract)). Input admitted
while no connection exists also ends the worker explicitly; it is not held.

Recovery is bounded: 4 attempts per outage; 0.5 s, 1 s, 2 s between them; 15 s
from loss to completed setup; at most 3 recoveries per 60 s; the first
connection is never retried. Quota, rate-limit and usage-limit refusals
(`QuotaExceeded`, `RateLimited`, relay close codes 4001/4002/4029) and
authentication, configuration and protocol faults do not reconnect. Server text
is never retained: a close reason or error body only selects a fixed category.

### Conversation context

Context survives a reconnect only through the service's session resumption
handle. The worker uses `Resumption::Retain`: the initial setup is unchanged,
any handle the service volunteers is kept in this process's memory, and only a
reconnect presents it. The handle is never logged, serialized or persisted and
has no value in `Debug` output. `Resumption::Off` (the library default) still
discards handles while parsing.

`RecoveryPolicy::require_context` is on. With no handle, the worker does not
continue with a session that has forgotten the conversation; it ends with
`NoResumableContext`. A handle issued before the latest exchange is used and
reported as `ResumedBeforeLatest`, because the service marks points during
generation as not resumable.

`goAway` is advance notice. An idle connection ends at once and is replaced; an
unfinished answer completes first.

### What the coordinator sees today

- A repeated `provider_ready`. Its `setup_us` spans connection loss to completed
  setup, not setup alone.
- One standard-error line per recovery, settled answer, announced close and
  first dropped event of each kind. Fixed categories only. A clean run with no
  such event writes nothing.
- A recovery during startup, before `listening_ready`, sends a second `Ready`
  that the startup poll rejects. That window still fails as before.

## Unverified service behavior

These need a bounded probe against the real endpoint. No microphone, speaker or
device is required, only credentials.

1. **Interruption before any output.** If `activityStart` arrives after
   `activityEnd` but before the first output, does the service send
   `interrupted` and/or a completion for the cancelled request? This is the
   pause-and-resume pattern of hesitant speech. If it sends nothing, the barrier
   expires after 2 s and the request is lost. `UnansweredInterruption::
   AssumeCancelled` then lets the successor proceed, as V1 does after its 10 s
   commit wait. It is off by default because it would misattribute the old
   answer if the service finished the old response instead of cancelling it.
2. **Handles without a request.** One relay session volunteered
   `sessionResumptionUpdate`. Whether every endpoint does, and whether
   `Resumption::Request` is accepted, is unknown. Without handles every loss
   ends with `NoResumableContext`, which is the previous behavior.
3. **Resumed setup.** Whether a volunteered handle is accepted on reconnect, how
   long it stays valid, and what context it restores.
4. **Long-open empty activity.** An interrupted response leaves an activity
   open until the next request. Service behavior for a long empty activity is
   unknown.
5. **Quota signals.** The close codes and reason wording used for quota and
   idle closes are taken from V1 notes and Google's error model.

## Proposed coordinator contract

Not implemented. `ProviderOutput` is matched exhaustively in `coordinator.rs`,
so each addition needs a coordinator change by its owner.

- `TurnFailed { request, stage, reason }` for `TurnLost`. The coordinator can
  then fail one turn with an honest outcome and keep listening, instead of
  losing the runtime. This is the prerequisite for a spoken failure.
- `Recovered { outage_us, context, attempts, reason }` instead of a repeated
  `Ready`, so the trace records why and whether the conversation was kept. It
  also allows `require_context` to be relaxed deliberately.
- `Unavailable` / `Ready` gating of admission, so input is not admitted while
  no connection exists. Today `provider_ready` never returns to false.
- A `ProviderConfig` option for `Resumption::Request` and the barrier policy.
- `MAX_REPLY_SAMPLES` is a 30 s backlog of undelivered PCM. The worker now
  delivers a fast burst intact (80 s of audio in about 1.5 s on the host), so a
  long answer generated more than 30 s ahead of playback would reach that
  bound. It needs coordinator-side flow control or a deliberate larger bound.
- `provider_interrupted` can no longer arrive for the current reply; that
  cancel path only ever fired on late events.

## Verification

From `lampOS/`, with a fresh target directory:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace
cargo build --locked --offline --release -p lamp-live
```

Focused suites:

```sh
cargo test --locked --offline -p lamp-gemini
cargo test --locked --offline -p lamp-live --lib provider_worker
```

`lamp_gemini::testing` is the scripted service used by both. It is in-memory:
no TLS, socket, credential or cloud request, and no runtime code uses it.

Results from the night of 2026-10-10, macOS x86_64 host, Rust 1.96.0, tests run with
`--no-fail-fast -- --test-threads=1`:

| Source, built as a source-only export outside the repository | Formatting | Strict Clippy | Tests | Release build |
|---|---|---|---|---|
| This change on shared checkpoint `23bde4873` (what the branch contains) | pass | pass | 605 passed, 0 failed | pass |
| This change on the earlier checkpoint `64529dee4` | pass | pass | 525 passed, 0 failed | pass |

Of these, `lamp-gemini` contributes 74 tests (56 unit, 6 setup, 12 codec) and
the provider worker 27. One earlier parallel (not serial) workspace run had a
single failure in `crates/live/tests/diagnostics.rs`
(`FinishOutcome::TimedOut`). That is the load-sensitive diagnostic finish
timeout already recorded in `HANDOFF.md`; the suite passed 3 of 3 in isolation
and in every serial run, and it exercises no provider code.

The ten original failures in the first table were reproduced by adding
scripted-service tests to an untouched export of `64529dee4`.

Measured on the macOS host only, inside the test process:

| Measurement | Boundary | Result |
|---|---|---|
| Stop during a hung reconnect | Control datagram sent to worker task ended | 2.4-24.2 ms over 7 runs, asserted under 100 ms |
| 80 s of audio offered at once (2,000 IPC packets) | Scripted service write to local IPC reader | 1.5-1.6 s, complete and in order |
| Heartbeat during a hung reconnect | 400 ms with no provider progress | Worker alive; 250 ms heartbeat bound never tripped |

These are host scheduling observations. They are not Linux/ARM64 timings,
network latency, answer latency or audible behavior.

Linux/ARM64 was not built or type-checked: the pinned toolchain on this host
has no `aarch64-unknown-linux-gnu` standard library and none was installed.
The provider worker has no Linux-only code path, but that is an inference from
the source, not a result.

## Integration notes

Branch `gemini-voice`, one commit on top of shared checkpoint `23bde4873`. It
contains only:

- `crates/gemini/src/{lib,config,wire,session}.rs` and `session/tests.rs`
- `crates/gemini/src/recovery.rs`, `recovery/tests.rs`, `testing.rs` (new)
- `crates/gemini/tests/wire.rs`
- `crates/live/src/provider_worker.rs`
- `docs/gemini-session-reliability.md` (new), three sentences in
  `docs/live-runtime.md`, three lines in `docs/gemini-comparison-config.md`

No audio driver, `coordinator.rs`, admission, interaction ownership or
choreography file is touched, and `ProviderInput` / `ProviderOutput` are
unchanged on the wire. In `docs/live-runtime.md` only the three sentences about
the barrier, reconnects and resumption change.

What an integrator will notice:

- `lamp_gemini::Event` gained `GoAway` and `Discarded`. Only the provider worker
  matches it.
- `wire::ServerFrame::go_away` is now `Option<GoAway>` and the frame has a
  `resumption` field. `wire::decode_server` still never returns a handle.
- `Timeouts` gained `stall`, `completion`, `turn` and `deliver`; `barrier` and
  `response` changed default and meaning as listed under [Bounds](#bounds).
- Provider failure text changed. It still begins with `provider connection
  ended: Gemini Live <Category>` and now ends with the recovery outcome or the
  lost request and stage.
- A recovery writes to the worker's inherited standard error. A harness that
  treats any standard-error output as failure will flag a recovered run, which
  is the intended visibility.
- Do not build two checkouts into one cargo target directory. Workspace crates
  share build identity by relative path, and a stale test binary from the other
  checkout can run without any warning.

## Required before relying on this

- Native Linux/ARM64 build, strict Clippy and tests of this source.
- The five service probes above.
- A physical conversation cohort that includes: an idle pause longer than
  200 s followed by a follow-up that depends on earlier context; a
  pause-and-resume question; an interruption of a long answer; a topic change;
  a deliberately dropped network link while idle and mid-answer. Record every
  `provider_ready`, the worker's standard error and the room audio.
- Echo-driven false admissions are outside this work and unchanged by it. Each
  one still cancels the answer in progress; this transport only guarantees that
  the service's late reaction to it cannot cancel anything further.
