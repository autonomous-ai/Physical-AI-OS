# Directed conversation scheduling cues

`lamp-session` creates a fresh private work directory and binds its same-user
Unix datagram socket before starting either `lamp-live directed` or
`lamp-live directed-fixture`. Both commands receive `--cue-socket ABS_PATH`.
Directed cues do not enable PCM diagnostics; that remains a separate explicit
option. A runtime without this CLI capability fails instead of silently using
fixed delays. Provider-kind, fault-injection, verified cached-source and
reviewed-room-evidence restrictions remain in force.

The schema-1 cue channel is scheduling evidence only. Each datagram is at most
512 bytes and contains its session boot, monotonic sequence, event and send
timestamps, an expiry exactly 100 ms after the event, optional capture/reference
epochs, and an immutable turn/generation for owned events. Supported kinds are
`listening_ready`, `input_admitted`, `local_endpoint`, `speaker_first_write`,
`speech_retired`, `cancelled`, and `run_end`. A cancellation preserves the
optional original `turn_finished.outcome` in `reason`; an omitted reason stays
unknown. The cue does not supply acoustic input boundaries, input transcript,
or proof of a complete or correct audible answer.

Compatibility is envelope-level, not whole-stream compatibility: an older
schema-1 cancellation without `reason` remains parseable. The new ordering
checks require `input_admitted` and `local_endpoint` from the updated producer,
so deploy this runner and runtime together. An older stream missing those
owner/phase cues is withheld or invalidated, never silently treated as a
supported directed conversation.

The runner validates the envelope, consecutive sequence and nondecreasing send
times, stable boot and per-turn generation, admission before endpoint, endpoint
before playback, and playback before retirement. Child-observation event times
may arrive after newer coordinator timestamps; they are preserved, not required
to be globally sorted. Cancellation may precede playback, or follow complete
audio retirement while its provider turn remains active. It rejects a revived
cancelled owner, unknown owner, duplicate phase or changed lineage. Retirement
cannot authorize duplicate retirement or revived playback. The
Lamp relay receipt must be at or after send and strictly before expiry. For
owned cues, a ping/pong clock mapping is also required: the mapped upper bound
of the runner receipt, including clock uncertainty, must precede expiry.
Readiness can additionally arrive on stdout after the relay has acknowledged
its requested cue capability. That readiness line never substitutes for a
missing retirement or playback-start cue.

The raw accepted or rejected cue envelopes, Lamp relay receipt times and runner
receipt times are retained in `evidence.cue_receipts`; `session_start` records
mode and actual runtime arguments. Sequence/identity/freshness or malformed
transport failures invalidate the attempt. A cue failure immediately ends
stimulus scheduling with an explicitly synthetic `cue_protocol_invalid`
run-end event in the runner clock domain, while cleanup still collects the
actual runtime trace. All attempted or withheld steps remain in the ledger.
The final trace can explain a missed trigger but cannot retroactively authorize
a stimulus.

The scenario's delay is measured from its matching software cue. For example,
`rapid-follow-up` targets 150 ms after the original turn's speech retirement;
`topic-change` targets 1,200 ms after its first accepted speaker write and still
requires that reply to be playing. These are software scheduling offsets,
not measured silence gaps or acoustic interruption latency. Missing or stale
cues withhold the follow-up; there is no fixed-delay fallback. Existing runner
lateness and reply-terminal checks remain active. A correct relay/fake test
cannot qualify microphone pickup, speech quality, source identity, semantic
correctness, audibility, or end-to-end latency. Those still require valid room
recordings and independent review.

## Offline regression path

`tests/physical_loopback.rs` starts the real `lamp-session` subprocess and a
`fake-lamp-live` subprocess. The fake supports both runtime command forms,
uses the actual live `CueSink`, and receives declared stimulus intervals through
a private fake-room socket. Cached synthetic tones validate the cache and
orchestration paths only: no CPAL/ALSA device, provider connection, SSH command,
TTS, microphone or speaker is opened by these tests.

Two fake-only fault options interpose one bounded local datagram shim:
`--fake-cue-fault drop-retirement` omits the first retirement cue, while
`stale-retirement` forwards that original datagram 150 ms late without changing
its timestamps, sequence or 100 ms expiry. The shim retains at most one delayed
512-byte datagram and drains at most 64 per tick. Nominal tests do not use it.
Both fault cases retain the final trace retirement while proving that a second
stimulus was never started. Additional pure tests reject schema aliasing,
missing or zero identities, changed generations, duplicates, missing sequence
numbers, invalid phase order, and receipt at the exact expiry boundary.

Run the finite process/socket tests serially to avoid unnecessary host
scheduling contention:

```sh
cargo test --locked --offline -p lamp-voice-eval --lib
cargo test --locked --offline -p lamp-voice-eval --test physical_loopback -- --test-threads=1
```

Local scheduling assertions are test sanity bounds, not hardware latency gates.
No Rust runtime behavior or threshold is changed by this runner implementation.
