# Opt-in local audio diagnostics

`lamp_live::diagnostics` records bounded local evidence for investigating capture,
echo cancellation, VAD and self-interruptions. It is inactive unless its caller
explicitly constructs a `Recorder`. It opens no audio device, reads no microphone,
changes no authority or privacy setting, and sends no data to a provider. Turning
it on is not permission to capture or play: the existing audio guards remain
responsible for every sample. Record only during an explicitly authorized test.

Use one recorder and fresh private leaf per audio worker. Both workers use the
same controller/session `BootId` and host `CLOCK_MONOTONIC` microsecond domain.
`Config.boot` must match a speech owner's boot; a worker incarnation is not an
interchangeable owner boot. Join the two streams offline by boot, epochs, accepted
sample cursors and timestamps. These are host/driver observations, not acoustic
acquisition, DAC delivery or speech-end-to-first-word measurements.

## Lifecycle and callback interface

Construct `Config::new(directory, controller_boot, StreamKind::Capture | Render)`
and `Recorder::start(config)` before audio activation. The parent directory must
already exist, be owned by the process user and have no group/other permissions
(normally `0700`). The leaf must not exist. No recording buffer, writer thread or
PCM file is created outside this explicit lifecycle. The constructor creates and
opens private files before returning; it can fail without activating audio.

For capture, the directed worker sets `Config.software_processing` from the same
typed mode used to construct its processor. The pending manifest, final summary
and completion marker contain `{"aec":"sonora_aec3","noise_suppression":true}`
or the corresponding `false` value. AEC remains enabled in either case. The
supervisor separately checks the actual `CaptureStarted` mode against its request
before accepting input readiness. A generic recorder with no supplied processing
provenance emits `null`, as does render where capture processing is inapplicable;
assigning capture processing to a render recorder is rejected before file creation.
Historical missing or null metadata is unspecified. An external replay may attach
an explicit historical source assertion, but cannot silently infer the current
default or certify an old run's mode. This metadata concerns software processing
after the microphone's USB output, not any unverified onboard DSP features.

The single producer exposes fixed-size, nonblocking methods:

- `try_capture(CaptureMeta, &[i16; 160], &[i16; 160])` copies pre/post AEC samples.
- `try_render(RenderMeta, &[i16])` copies only the actual accepted 1–240 samples.
- `try_silence(RenderMeta, SilenceKind::Prime | Idle | SpeechGap, frames)` records a typed
  count of actual accepted zero frames, also 1–240. Prime writes must be captured
  individually; an intended 480-frame prime is not evidence of two accepted writes.
- `try_reset(ResetMeta)` records capture/DSP/playback transitions and discarded
  queued ranges. `try_privacy(PrivacyMeta)` records observed stream privacy.
- `faults()` exposes a latched diagnostic fault bitmask; `SubmitResult` distinguishes
  queued, disabled, malformed, overflow and duration-limited submissions.

A method performs one fixed-capacity SPSC queue push; it does not allocate, write a
file, hash, format JSON, log, lock a mutex, wait or retry. The queue has 64 records
and is pre-touched before activation. Sample arrays are bounded at 160 capture or
240 render samples; metadata contains fixed fields and typed enums. One writer
thread serializes metadata and writes PCM. Its idle poll is 1 ms and never wakes
through an audio-thread mutex. The design target for a normal producer call is
under 100 microseconds, well below the 10 ms capture block; that target is not yet
a measured hardware guarantee. Native load and callback timing must be qualified.

Overflow, bad records, writer failures and diagnostic deadlines disable further
valid evidence, not audio authority. Callers must not propagate a diagnostic hook
failure into the audio loop as permission to alter playback or microphone state.
The writer may drain already queued records; incomplete files remain useful for
debugging but are never silently repaired or scored as continuous evidence.

An actual speaker write can be accepted and then discarded before playback.
Record its old-epoch acceptance followed by the typed reset/discard range. Never
label accepted PCM as played or move old PCM into a new epoch. Source chunk sequence
and optional `TurnOwner` preserve lineage; they do not grant output authority.

## Continuity and privacy boundaries

An explicit open privacy record with a nonzero generation must precede PCM. A
closed privacy generation cannot be overridden by a PCM record. A changed privacy
state requires an advancing generation. Privacy records do not erase sample or
source-sequence history. They are observations, not instructions to reopen input.

Capture begins at source frame sequence 1. Render begins at sample cursor 0.
Within an epoch, capture source sequences and accepted render cursors must be
contiguous. The independent record queue sequence must also be contiguous. A
missing opening frame, dropped record, cursor gap, backward event clock, or
unannounced epoch/privacy change prevents validity.

`Start` anchors the initial epoch. `DspReset` advances DSP identity while preserving
the microphone epoch, privacy generation and expected next source sequence. It
cannot conceal a missing microphone block. `CaptureRestart` requires a newer
capture epoch and restarts its source sequence at 1. `PlaybackReset` requires a
newer playback epoch and validates the old discarded range against the recorded
accepted cursor before restarting at 0. `PrivacyChange` itself cannot reset either
cursor. `Stop` and `Fault` can retain observed discarded ranges. Every boundary has
explicit byte offsets in the metadata stream; concatenated PCM must never be
interpreted as one uninterrupted acoustic timeline across those boundaries.

`CaptureMeta` includes microphone/DSP/privacy identity, source frame sequence,
read start/completion, optional ALSA status time, status observation time, available
and delayed driver frames, processing completion, VAD probability and AEC queue
delay. Its `ReferenceMeta` holds playback epoch, accepted/analysed cursors and their
host observations, queued frames, capture-block accounting and clock start.
`capture_blocks_processed` retains all DSP calls within the current reference
epoch. `capture_blocks_before_clock_start` is the fixed count of at most two
retained host reads strictly before `clock_start_requested_at_us`; it is zero
until that start notice is validated. The request is a host lower bound taken
before the speaker's start method call; `clock_started_at_us` retains the
post-ALSA-call completion timestamp. Neither is exact physical DAC onset. A late
notice cannot exempt a read during that ambiguous call interval or afterward.
This separate origin describes restart accounting, not synchronized USB clocks
or ADC acquisition time. Historical recordings lacking these fields retain
their original interpretation; do not silently infer an unrecorded request bound
from their completion timestamp or assume they used this correction.
`RenderMeta` carries playback/privacy identity, first/end accepted sample cursors,
acceptance/queue observation times, queue depth and optional owner/chunk identity.
Negative driver queue values remain observations; diagnostics do not correct them.

## Bounds and artifacts

`Config` permits 1–600 seconds and 16 KiB–160 MiB. Limits cover PCM and metadata,
including a reserved final report budget. Frame totals are separately bounded at
16,000 capture or 24,000 render samples per requested second, so repeated or frozen
timestamps cannot evade the duration cap. Filesystem block allocation and directory
entries are outside the logical byte budget; the completion marker is a hard link
and does not duplicate its report payload. A cap reached during recording is an
invalid diagnostic run, not an automatically shortened successful one.

The directory is pinned by an open descriptor. Leaf/file creation uses exclusive,
no-follow descriptor-relative operations; prior paths are never overwritten.
Files are `0600`, the leaf is `0700`, and ownership, privacy permissions and inode
identity are rechecked before certification. Replacing a pathname does not redirect
an already open PCM descriptor; it invalidates the result. This is not a security
boundary against arbitrary code already running as the same user. Native filesystem
calls can still block, which is handled by incomplete shutdown rather than an
unbounded join on an audio or privacy path.

Artifacts are local private test data:

- `manifest.pending.json`: immutable initial request, boot, scope, labels and bounds;
  explicitly incomplete from creation, retained even after a completed run.
- `pre_aec.pcm16le`: **ALSA-resampled 16 kHz mono PCM16 little-endian** before the
  capture processing call; not raw native-rate microphone audio. Jieli output may
  already contain onboard processing; this is not raw separate capsule audio.
  See [microphone topology](microphone-topology.md).
- `post_aec.pcm16le`: caller-supplied 16 kHz mono PCM16 little-endian after AEC.
- `render_accepted.pcm16le`: only actual accepted 24 kHz mono PCM16 little-endian
  samples, including explicitly typed accepted zeros.
- `events.jsonl`: typed capture/render/reset/privacy/end records, local sequence,
  exact frame counts and byte offsets in all three PCM files.
- `result.json`: final summary, file hashes, counters and errors when finalization
  can finish. A missing, empty or partial result is incomplete.
- `complete.json`: exclusive hard link to the fully flushed successful report,
  published only after PCM/metadata flush and sync checks. Its presence alone is
  insufficient for acceptance.

No file is uploaded or committed implicitly. Raw PCM is intentionally not wrapped
in a WAV header: use the recorded rate/channel/format labels and per-record offsets,
not a guessed format or a reconstructed gap-free timeline. PCM clipping is retained
faithfully for diagnosis; this module does not normalize, suppress or score it.

## Finishing without delaying audio or privacy shutdown

Stop/revoke audio and close the hardware first. On the control exit path call
`finish(self, EndMeta, timeout)`. The default wait is 500 ms, with a hard caller
ceiling of 1 second; zero or larger waits are invalid. Never call it in an audio
callback or wait for it before privacy shutdown. `EndReason::Fault` always makes
the evidence invalid, including faults before a complete accepted-write receipt.
`Stopped` or `PrivacyClosed` may finish a valid bounded diagnostic session, but do
not mean an intended conversation or audio response completed.

Finishing closes the producer and waits only for a bounded writer acknowledgement.
It never joins a potentially filesystem-stalled thread. Drop without an explicit
end, a missing end record, queue loss, timeout or writer disconnection is invalid.
A timed-out writer is detached; it remains bounded to its already queued data and
is reclaimed when the worker process exits. The module cannot preempt a native
write, sync, close or link operation. A timeout can race a completion-marker
publication already inside the filesystem, so a marker may appear later.

The supervisor must require **all** of these:

1. A timely `FinishReport` with `valid: true`, `outcome: complete` and
   `completion_marker_published: true`.
2. The durable `complete.json` bytes matching `completion_sha256` from that receipt.
3. Matching session boot, stream identity and start time, plus the successful
   counters/hashes in its summary.

A timeout/error receipt invalidates later files regardless of marker appearance.
The report is diagnostic integrity only; it does not certify echo cancellation,
absence of self-triggering, physical audibility or acoustic latency. A physical
run must correlate these artifacts with independent room audio and the live trace.

Fault bits: queue overflow `1`, writer error `2`, record/capture sequence gap `4`,
render cursor gap `8`, epoch/privacy boundary `16`, byte limit `32`, duration/frame
limit `64`, invalid record `128`, abandoned/incomplete stream `256`, finish timeout
or invalid wait `512`, path identity/privacy failure `1024`, audio/runtime fault
end `2048`. A failed sync may also invalidate final filesystem certification.

## Offline validation

Tests use synthetic samples and private temporary files only. They cover exact PCM
and offset preservation, hashes and completion receipt, partial render writes and
priming zeros, missing opening frames, DSP reset continuity, playback discard/new
epoch checks, closed privacy, sequence/cursor gaps, no overwrite, symlink replacement,
byte/frame/time caps, full queue, stalled/failing writer and incomplete finalization.
The writer stall/fault hooks exist only in test builds.

```sh
cargo test -p lamp-live --test diagnostics --locked --offline
cargo test -p lamp-live --lib diagnostics::tests --locked --offline
cargo clippy -p lamp-live --all-targets --locked --offline -- -D warnings
```

These tests perform no microphone, speaker, camera, device, SSH or cloud operation.
They do not establish callback performance on the Lamp or reproduce a physical
self-interruption. Hardware qualification remains a separate authorized run.
