# Directed conversation cue integration — 2026-10-11

This checkpoint extends `ae08b273e` with the same optional lifecycle cue channel
for Gemini and the cached-reply provider. The test runner can schedule a
follow-up after observed playback retirement or a topic change during observed
playback. It no longer withholds these Gemini scenarios solely for proposal P1.
See the [producer contract](live-session-cues.md) and
[runner behavior](voice-eval-directed-cues.md).

This changes observation and test scheduling, not the production microphone,
VAD, addressee policy, prompt, voice model or motor authority. Metadata cues do
not opt into PCM recording. Software playback retirement is not an acoustic
measurement, and admitting VAD activity does not establish a human addressee.

## Verification

A standalone source-only copy passed **724 host tests, zero failed and
zero ignored**, with these gates run sequentially:

```sh
cargo fmt --all -- --check
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
cargo test --locked --offline --workspace --no-fail-fast -- --test-threads=1
cargo build --locked --offline --release -p lamp-live -p lamp-voice-eval
```

Host: macOS x86-64. Rust/Cargo/toolchain manifest: `5a827d27d4e2d98580011c7382260224cfc6c75f4b549c850d7d487d9e9184c1`
(163 files, documentation excluded). Tests requiring local Unix and
loopback sockets ran outside the filesystem/network sandbox; no hardware or
cloud was used. Separate-process loopbacks exercise the actual local relay and
a fake Lamp runtime/player, not the Gemini service or room acoustics.

The review added regressions for cancellation after complete playback, and
child observations arriving with older original timestamps. Retirement does
not erase the original cancellation, and emission order does not rewrite
observation times. Missing, stale, duplicated or relabelled cues cannot trigger
a guessed-delay follow-up. Runner and runtime must be updated together.

The producer's separate 200-event same-process socket measurement was first
31 µs, p50 16 µs, p95 18 µs and max 31 µs. It measures event creation to local
peer receipt only, excluding coordinator scheduling, relay/network, ARM and
acoustics. It is observation overhead, not a voice speedup.

Exact commands, logs, original failures, source/patch identities and the
source-only archive are retained privately in
`artifacts/directed-cues-20261011/` and a separate durable handoff backup.
Earlier 712-test receipts remain attached to their own source.

## Remaining qualification

No Linux/ARM64 build, cloud call, real Lamp trial or acoustic result is added.
Playback-paced Gemini delivery, relay framing, robust echo/addressee admission
and physical duplex/choreography still need qualification. Follow
[workstream ownership](development-workstreams.md). Physical work remains
deferred until the owner checks the device; this is not release acceptance.
