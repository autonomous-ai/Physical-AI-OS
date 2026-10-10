# Offline microphone processing experiments

The `lamp-audio replay-aec` command compares the existing Sonora AEC3 processor
with noise suppression disabled and enabled, using exactly the same recorded
microphone samples, accepted speaker reference, and recorded AEC delay. It opens
no audio devices and makes no network requests. It does not alter the live
runtime, microphone gain, AGC, playback level, or the DSP configuration used by
an archived recording.

The current physical mapping reported by the team is two Jieli side capsules
for voice mic2 and a center C-Media capsule for ambient mic1. Claims about the
Jieli's onboard processing have not been independently verified. In these
recordings `pre_aec` means ALSA-resampled, 16 kHz mono PCM **after hardware and
driver processing**; it is not raw per-capsule audio. Software cannot recover
speech already removed or distorted before this boundary.

## Invocation and evidence classes

Run from the standalone lampOS root, with an existing ignored `artifacts/` or
`.cache/` directory. `NEW_OUT` must be a fresh leaf beneath one of these roots.
Absolute input paths are supported. No build-machine or sibling repository
path is embedded in the utility.

```sh
cargo run -p lamp-audio --locked --offline -- replay-aec \
  /path/to/copied/runtime artifacts/aec-comparison-01
```

The input is a copied directed runtime directory containing `events.jsonl`,
`capture-audio/`, and `render-audio/`. Each audio leaf must contain its original
`manifest.pending.json`, `result.json`, `events.jsonl`, and three PCM files.
A certified input additionally needs both `complete.json` markers and the
matching successful, acknowledged completion receipts in runtime events.
Marker existence alone, a timed-out finish, missing receipt, fault end, hash
mismatch, or mismatched worker/session identity cannot certify a recording.
Certification means the diagnostic files were finalized successfully; it is
not conversational, microphone-quality, or acoustic-latency acceptance.

Hash-verified, intact fault-ended recordings can be explored explicitly:

```sh
cargo run -p lamp-audio --locked --offline -- replay-aec \
  /path/to/copied/failed-runtime artifacts/aec-fault-prefix-01 \
  --exploratory-prefix
```

These reports remain `exploratory_fault_prefix_not_benchmark_evidence` even
when the entire recorded prefix can be replayed. This switch does not repair
corrupt hashes, truncated JSONL, missing files, inconsistent byte offsets, or
invalid privacy boundaries. An arbitrary crash with no intact result manifest
is currently unsupported. No absent input or speaker reference is replaced
with generated silence.

New recordings may identify their actual processing as
`software_processing: {"aec":"sonora_aec3","noise_suppression":true|false}`
in both capture manifests. Missing or null metadata remains unspecified. For
an older recording whose processing was independently established from a
pinned source archive, an operator may attach an explicit historical claim:

```sh
cargo run -p lamp-audio --locked --offline -- replay-aec \
  /path/to/copied/runtime artifacts/aec-historical-01 \
  --exploratory-prefix \
  --historical-processing on artifacts/pinned-source/source-manifest.json
```

The evidence JSON must contain a SHA256 `source_id`. The report retains that
identifier and the evidence-file SHA256 and labels the mode as an operator
assertion, not a runtime mode receipt. The utility does not itself prove that
this source archive produced the recording. An assertion cannot override an
explicit recorded mode, and never changes source certification.

## Reconstruction and boundaries

Each pair uses `EchoProcessor::new(false)` and `EchoProcessor::new(true)`.
Other Sonora options remain unchanged, including the default-disabled optional
echo detector/statistics. There is no gain normalization or output boosting.
The utility does not estimate AEC delay from correlation or pick a delay that
produces a favorable result.

For each capture block it preserves the exact 160 microphone samples, recorded
`aec_queue_delay_ms`, and `reference.analysed_through` cursor. Before processing
that block it feeds only the intervening 240-sample reference blocks assembled
from actual accepted writes. Partial speaker writes retain their order and
packet lineage. Initial and post-reset DSP states require an explicit recorded
Start/DSP-reset boundary and 480 actually accepted, typed prime-zero frames.
Accepted-but-discarded ranges are retained as reset evidence; accepted audio is
not proof that the speaker physically played it.

DSP resets create separate output segments and keep microphone frame sequence
continuity. Privacy closure ends the current segment. Unsupported restart,
missing prime, cursor gap, unrecorded reference, or ambiguous phase state stops
the replay at the first affected boundary and reports the preceding usable
prefix. It does not silently join epochs or extend a short recording. Input
clock/order contradictions are rejected. The optional
`capture_blocks_before_clock_start` count, optional `clock_start_requested_at_us`
lower bound, and nullable `clock_started_at_us` completion timestamp are retained
without equating microphone and speaker USB-clock phase. A historical missing
request timestamp remains absent; it is never inferred from completion.

`operations.jsonl` retains capture read/status/process clocks, DSP/playback
identities, source offsets, VAD values, complete reference metadata, reset and
privacy boundaries, and the accepted-render cursor/packet ranges used before
each capture. It uses the captured DSP call order, not a sort of unrelated
wall-clock events. Host timestamps and nominal device delay are diagnostic
evidence, not acoustic speech-end or first-word timestamps. A Mac replay may
differ numerically from target hardware even with identical sample ordering.

## Outputs and interpretation

Every nonempty DSP segment produces four mono 16 kHz PCM16 WAVs:

- `segment-NNN-pre_aec.wav`: original input at the software boundary.
- `segment-NNN-recorded_post_aec.wav`: original recorded processed output.
- `segment-NNN-aec_only.wav`: offline AEC with noise suppression disabled.
- `segment-NNN-aec_ns.wav`: offline AEC with noise suppression enabled.

`report.json` records source hashes and certification, processing provenance,
replayed versus available sample counts, per-segment identities, recorded
clock/delay ranges, PCM SHA256 hashes, RMS, peak level, saturated/near-rail
sample counts, and constant near-rail runs of at least three samples. The
`wav_pcm_sha256` values hash decoded little-endian PCM, not the WAV container.
Sample mismatch counts against recorded output help check reproducibility;
they do not measure intelligibility or indicate which mode is better. The
report contains no WER or acoustic-quality pass verdict.

A lower RMS may mean echo suppression, speech loss, or both. A louder AEC-only
output is not evidence that noise suppression should be disabled. Compare
known prompt openings, quiet words, and answer echo separately, using local
listening/transcription and waveform evidence when authorized. The utility
retains historical VAD scores but does not rerun VAD or predict interruption
behavior. It neither runs ASR nor exports audio. Speaker-replayed input cannot
qualify direct-human speech; a controlled human-input cohort is still needed.

Output directories are created exclusively with mode 0700; files use 0600 and
no-follow/exclusive creation. Inputs must be bounded regular non-symlink files.
Each input audio leaf is capped at 160 MiB and 600 nominal seconds; the runtime
trace is capped at 32 MiB, records at 120,000 per file, metadata lines at 4 KiB
(audio) or 64 KiB (runtime), and replay segments at 128. Files are loaded within
these bounds; this is an offline command, not an audio-callback implementation.
No output path is overwritten. It first writes `incomplete.json` and publishes
`report.json` last after WAV finalization and flush; the initial file remains
as the record of how work began. If output fails, partial artifacts remain
explicitly incomplete. A successful command means a report was produced:
always inspect `replay_complete_for_recorded_capture`, source certification,
status, and limitations before using the comparison.

## Local verification

```sh
cargo fmt --check -p lamp-audio
cargo clippy -p lamp-audio --all-targets --locked --offline -- -D warnings
cargo test -p lamp-audio --locked --offline
```

The replay tests use synthetic offline fixtures to verify exact DSP call
ordering, partial writes, reset/sequence behavior, actual prime requirements,
privacy, source hashes, certification receipts, bounds, and exclusive output.
These tests do not validate microphone quality, self-interruption rate, target
CPU behavior, or end-to-end speech latency.

## Finite cached-reply interruption fixture

`lamp-live directed-fixture` replaces only the cloud provider with one preloaded
local reply. It still uses the directed coordinator, physical privacy worker,
real admitted microphone input, immutable turn ownership, ordinary speaker
permits, actual accepted render reference, and normal local cancellation. It
opens no provider connection and reads no credentials. It starts no camera,
ring, or motor worker. External actuator owners must remain inhibited; the
existing exclusive-owner check still refuses active legacy services.

On Linux, after explicit device-test authorization:

```sh
lamp-live directed-fixture /absolute/cache/reply.wav WAV_SHA256 25 NEW_OUTPUT \
  --diagnostics --noise-suppression on --cue-socket /private/session/cues.sock
```

`--diagnostics` is required explicitly because this mode retains microphone and
speaker PCM. The run duration is bounded to 1–600 seconds after actual input
readiness, with the existing finite startup and shutdown bounds. The source is
opened without following its final symlink, checked as a regular file no larger
than 2 MiB, verified against the required lowercase SHA256, and decoded before
workers activate. It must contain 10 ms–30 seconds of mono 24 kHz PCM16. The
provider worker independently reopens, verifies, and owns its complete copy
before signaling readiness. Neither the source nor the microphone recording is
resampled, amplified, normalized, regenerated, or uploaded by this mode. The
source WAV/PCM hashes, frame count, format, and rail-sample count are recorded
under `run_start.fixture`.

The first controller-admitted owner reserves the reply. A matching Start,
contiguous fresh captured PCM, and End are required before any reply PCM is
emitted. The worker discards input PCM after checking its protocol continuity.
Cancellation that overtakes queued input or output retires the original reply;
no successor borrows its PCM and no automatic retry/replay occurs. Subsequent
admitted inputs can finish with no fixture audio, explicitly outside answer
correctness scoring. This provider has no ASR or addressee classifier: an
erroneous VAD admission can reserve/cancel the fixture and is experiment evidence,
not a reason to fabricate an input or replay until success.

A fixture packet contains at most 960 samples, with at most one input and one
output step per tick. Priority control is drained before and after input receipt
because authority and data use separate sockets. Either full 16-message control
slice defers input submission and output; at most one input is retained for less
than 100 ms from its original receipt, without changing its owner or privacy
generation. An empty second drain still requires matching admitted authority;
missing authority is not retried. The control loop has a 2 ms cooperative
wait and targets servicing revocation within one 10 ms audio block; this is a
design target, not a measured OS or hardware deadline. All source I/O occurs
before readiness. A blocked data channel retains only its bounded pending packet; fresh privacy, owner and input leases are checked before
each attempted emission. An expired lease, coalesced close/reopen, Stop, or
control failure cannot revive buffered PCM. The coordinator still issues each
normal 240-sample speaker chunk and the speaker still validates its permit at
the final write. A final short chunk may have the existing explicitly recorded
zero padding. A fixture's final accepted sample/retirement is not an acoustic
end timestamp.

### Optional scheduling cues

Before activation, the optional cue endpoint must already be an absolute,
same-user Unix datagram socket in a same-user private directory. The connected
socket is nonblocking and pins that endpoint. The runtime sends at most 512
bytes per cue using a fixed stack serialization buffer; it does no filesystem,
stdout, callback, or waiting work for cue delivery. No cue grants authority.
The external finite test harness owns the listener and its cleanup.

Cue kinds are `listening_ready`, `speaker_first_write`, `cancelled`,
`speech_retired`, and `run_end`. Each carries a sequence, controller boot,
original turn/generation when applicable, capture epoch, event/send timestamps,
and an event-based 100 ms expiry in Lamp's local monotonic domain.
`reference_epoch_context` is the latest observed reference-clock context;
`Playback` receipts do not carry their actual render cursor, so this field is
not an exact write-to-render-epoch binding. Use accepted render diagnostics for
that association. `speaker_first_write` means the normal speaker accepted PCM,
not that its first word was already audible.

The external harness must match session and original turn, enforce ordered and
fresh cues, and withhold/cancel a secondary stimulus on cancellation, missing
cue, expiry, sequence ambiguity, or cue invalidation. Cross-host clocks require
an explicit correlation and transport-age bound; copying Lamp's monotonic number
into a Mac schedule does not establish freshness or an acoustic offset. The
runtime's expiry check protects event-to-send age, not an arbitrarily delayed
external relay. It does not implement the external iMac trigger consumer.

Backpressure, an unavailable listener, stale events, or serialization failure
latch cue-invalid, disable subsequent cues, and add `fixture_cue_invalid` to the
bounded trace. Voice/physical privacy continue normally. `run_end.cue_valid`
reports the final scheduling-channel state (null when omitted); a zero exit
status or complete PCM does not override a false value. The full trace is still
written after the run. Cues cannot substitute for the mandatory diagnostic
completion acknowledgement plus durable marker or independent room recording.

### Controlled echo-only and double-talk use

Use the same cached initial question to trigger the fixture, then vary only the
secondary iMac input: none, a directed interruption, or unrelated dialogue.
`lamp-observer play --output 'iMac Speakers' --wav CACHED.wav --out NEW_DIR`
already supplies an explicit output route and bounded cached playback. Its
stream-open/preparation delay is not an exact scheduling guarantee; retain every
attempt and use room waveforms to verify actual overlap and boundaries. A missed
early target or an early cancellation is a censored observation, not permission
to filter or automatically replace the attempt. Current directed admission does
not identify an addressee, so unrelated near-end speech is a separate policy
negative case even when echo cancellation works.

Freeze source, reply/stimulus hashes, NS mode, mic/AGC configuration, playback
levels, placement, and observer route. Start with near-end-only controls and
no-secondary-input repeats, then early/later directed and unrelated overlap.
Keep a held-out far-end voice/text and a later direct-human cohort. Confirm that
no unplanned room speaker was active before labeling a trial echo-only.
Measure false admission/cancellation and actual far-end exposure, preserved
opening/quiet words, acoustic interruption onset to local admission and last
old audible reply word, clipping, reference continuity, and capture faults.
RMS attenuation alone is not intelligibility or successful interruption.

A reply extracted from certified accepted speech is a new cached fixture: if
recorded injected-zero gaps were removed, its manifest must explicitly retain
that fact and the source diagnostic identity. It does not reproduce the original
playback timing. This command verifies the selected WAV hash; it does not itself
certify the upstream extraction manifest or synthesize missing speech. Cached
synthetic speech can be used as a separately labeled holdout without generating
new TTS during a run.


## Cached-reply native qualification, 2026-10-10

The isolated source `9bdbcfaeddf9b0aed8b8a81fab135f358a47eefa6d55401a3980acacb63037b1`
passed 437 native ARM64 workspace tests (none failed or ignored), strict workspace
Clippy with all targets, workspace formatting, and the release `lamp-live` build.
The resulting binary SHA256 is
`683df87861fa6df45c1c4741d17b4dabdd12be3f589e3b8803abfdaf7158e129`.
Source identity, exact commands, logs, and the earlier failed source-overlay
attempt are retained in `artifacts/native-live-fixture-9bdbcfae/`.

The preceding overlay (`733e9b20…`) omitted the offline replay module export and
CLI; its test compilation failed before tests or audio execution. The corrected
overlay adds those files. This qualification includes the reviewed two-socket
authority/intake fix in the cached-reply provider. The separate ordinary Gemini
provider repair and newer camera backend are not part of this source snapshot.
Native test completion is not acoustic, interruption, or voice-quality acceptance.
