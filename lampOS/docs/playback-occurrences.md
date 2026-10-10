# Multiple playback occurrences in one answer

A Gemini generation ending does not necessarily end the person's turn.
The Gemini session test
`audio_keeps_session_and_request_lineage_and_generation_is_not_retirement`
already covers another audio generation after `GenerationComplete` and a
non-idle `TurnComplete` for the same request. Preserve this transport behavior
and the existing `PlaybackToken` identity; do not invent a new turn or replay
the question to continue the answer.

## Reproduced failure

At baseline `4b8436d23099a2d86e90d933a83877985d21cf17`, later provider audio
cleared the coordinator's `final_chunk` while that previously accepted final
chunk could still be playing. Its valid speaker retirement then failed with
`speech retirement has no matching final chunk`, ending the directed session.
The coordinator also allowed more audio to reach a speaker which retains only
one pending final cursor. A newer final chunk could overwrite that cursor.

Two host regressions fail against the original production logic after a
behavior-preserving extraction into `Reply` methods. They reproduce loss of
the receipt identity and dispatch before the previous final cursor retires.
The baseline source, extracted source and failing test log are retained with
the checkpoint evidence. These are software interleavings, not recorded cloud
or acoustic failures.

## Runtime correction

New provider audio reopens generation and enters the existing bounded PCM
queue. It does not clear an accepted final chunk. The existing outstanding
speaker chunk prevents dispatch before acceptance; the retained final chunk
prevents dispatch after acceptance until its matching retirement receipt.
Retirement clears that marker and ends exactly its `PlaybackToken`. Queued
continuation audio can then begin a new playback occurrence for the same turn.
Cancellation still revokes the entire turn and all its queued audio and output
permits, including between occurrences.

If more audio arrives before the first final block has been dispatched, it
continues the queued generation normally. No output waits for the provider's
whole-turn idle marker to start speaking. Whole-turn completion still requires
provider idle, empty reply PCM, no pending speaker chunk and no active playback.

The change adds no PCM queue, worker, timer, request replay or speaker reset.
The existing 30-second backlog cap and two reserved provider packets remain.
Capture, privacy and cancellation run before speaker dispatch. The speaker
continues its existing physical clock and render-reference behavior while
waiting; host tests cannot establish its acoustic continuity.

The local service target remains one 10 ms playback block from eligibility to
handoff, with the coordinator's usual 2 ms service cadence. Serializing final
cursor retirement can add a boundary gap. Measure original retirement receipt
to next dispatch/write, physical playback continuity and microphone overlap on
Lamp before accepting it as responsive. Neither the 2 ms cadence nor passing
host tests is a hardware deadline or a measured voice speedup.

## Observation and evaluation contract

Each `speaker_first_write` and `speech_final_sample_retired` trace records the
actual positive `PlaybackToken::sequence()` as `playback_sequence`. A normal
`turn_finished` trace carries top-level `generation`, outcome and gap count;
it must not gain `owner`, which identifies the existing cancellation trace.
The corresponding [lifecycle cues](live-session-cues.md) remain observation
only, fixed at 512 bytes and a 100 ms original-event expiry. Their worst-case
sizes, including maximum numeric fields and datagram sequence, are 430 bytes
for first write, 425 for retirement and 475 for completion.

The runner must distinguish these lifetimes: a segment's retirement ends only
that playback interval; a `turn_completed` cue ends the answer. Ordinary
follow-ups wait for completion. Historical `SpeechRetired` plans preserve their
meaning and hashes. The evaluator must include every playback interval in
exposure, interruption and ring checks, and cannot call a cancelled later
generation complete because the first one retired. Missing identity or terminal
evidence must stay uncertain or fail qualification. First-word latency retains
the first playback onset; later starts do not replace it.

## Verification boundary

Focused producer checks pass 27 coordinator tests and 26 fixture/cue tests.
They cover continuation before final acceptance, before retirement and after
retirement; distinct playback tokens within one turn; exact PCM conservation;
stale first-occurrence retirement during the second; cancellation between or
during later occurrences; bounded cue sizes and truthful completion metadata.
The simulated speaker uses its production `SpeechPlayback` bookkeeping with
one epoch. It does not open ALSA, microphones, motors, camera or a cloud session.

The combined standalone source passed **753 host tests, zero failed and
zero ignored**, plus formatting, strict Clippy and both release builds. The
commands ran sequentially with sources unchanged throughout:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
```

Rust/Cargo/toolchain manifest: `6b0ea9e37fdc57cb34560080c01843f63b21aca9285237a3cffada62585fcd96` (165 files,
documentation excluded). Host: macOS x86-64; local Unix and loopback socket
tests ran outside the network sandbox. The integrated runner tests use the
actual CueSink and relay with scripted processes, including two segments,
interruption during segment two and missing/stale completion that withholds a
follow-up. No speaker or microphone is opened by those loopbacks.

The original evaluator incorrectly certified a cancelled second segment,
discarded later playback exposure and silently lost lifecycle records with
missing timestamps. Those failures, the runtime's two original red tests,
final focused receipts, immutable combined gate logs and exact source archive
are retained in `artifacts/playback-occurrences-20261011/` and a separate
durable backup. One agent development package run mixed a newly built deliberate
red-test executable into an already-running suite; its exit 101 is preserved
and is not counted as exact-source validation. The combined gate above replaces
that development run as the checkpoint's full-suite evidence.

ARM64/Linux, real Gemini response ordering, acoustic onset/end, boundary gaps
and direct-human interruption remain unqualified by this host-only change.
Playback-paced upstream delivery remains open. The separate
[ring trace-capacity correction](ring-choreography.md#renewal-evidence-qualification-2026-10-11)
is qualified in its own subsequent checkpoint. No speedup or release acceptance
follows from these software tests.
