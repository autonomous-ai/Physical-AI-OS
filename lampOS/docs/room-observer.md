# Independent room-audio observer

`lamp-observer` is a macOS test tool for collecting continuous room audio from an
explicitly selected microphone. The iMac stimulus and Lamp's physical reply can
then be annotated in one WAV timebase. It is recording infrastructure, not a
voice benchmark result: no live device qualification is implied by its unit tests.
The [comparison protocol](comparison-protocol.md) defines the V1-main versus V2
experiment and the acoustic measurements.

## Build and commands

From the `lampOS` repository root, compile and query permission without opening
any microphone or displaying a prompt:

```sh
cargo build --locked -p lamp-observer
target/debug/lamp-observer authorization --out /private/tmp/lamp-permission-check-001
target/debug/lamp-observer devices
```

A standalone CLI launch can inherit privacy attribution from its launching
application. Its bundle may also lack a microphone purpose string. For reproducible
local testing, package the same Rust executable as a named macOS application:

```sh
tools/observer-bundle/build.sh /private/tmp/LampRoomObserver.app debug
open -n -W -a /private/tmp/LampRoomObserver.app --args authorize --out /private/tmp/lamp-permission-request-001
open -n -W -a /private/tmp/LampRoomObserver.app --args record --input 'iMac Microphone' --seconds 120 --out /private/tmp/lamp-room-attempt-001
```

These are commands for an authorized live operator; building the app does not
launch it, prompt or record. Replace the input name with the exact inventory name.
Use fresh app and output paths. Omit `debug` for an optimized release build. Set
`CARGO` to the Cargo executable path if it is not in `PATH`. The packager builds
from this repository with the lockfile into an isolated temporary target directory,
then removes that temporary build directory. It does not depend on V1 files.

The bundle includes a stable identifier (`ai.autonomous.lamp.room-observer`),
`NSMicrophoneUsageDescription`, and the hardened-runtime audio-input entitlement.
It is signed ad hoc for local development, verified with `codesign`, and accompanied
by a hash of the signed executable. An existing signing identity may be specified
with `LAMP_OBSERVER_SIGN_IDENTITY`; the packager does not create credentials.
Ad hoc signing does not guarantee that TCC remembers authorization across rebuilt
executables. Keep the same qualified bundle for a comparison run and log its hash.

`open -W` waits for app exit but does not make the app's stdout or exit status a
reliable experiment result. Read the output directory's final `metadata.json`.
`authorize` records `authorized`, `capture_allowed`, before/after status, whether
a native request was sent, callback result and timeout. Its `valid` remains false
because permission alone is never a valid acoustic recording. A missing final
report is incomplete. `authorization` can also write a fresh diagnostic directory
with `--out`; without it, the status report goes to stdout.

## macOS microphone permission

Before any input stream is created, `record` reads
`AVCaptureDevice.authorizationStatusForMediaType(AVMediaTypeAudio)`. It blocks with
an explicit report unless authorization is `authorized` and the main bundle has
a nonempty microphone purpose string. The final report repeats the check after
capture; a known loss of authorization prevents validity. Record never requests
permission implicitly.

`authorize` requests consent only from `not_determined`, using Apple's native
permission API. A denied state directs the operator to System Settings > Privacy
& Security > Microphone; a restricted state requires resolving that macOS policy.
A missing bundle purpose string prevents the request rather than risking macOS
terminating the process. It waits at most 60 seconds for the asynchronous result;
a timeout is not consent. No command reads or edits the TCC database, resets
permissions, or attempts to bypass the operating system's decision. These rules
follow [Apple's media capture authorization guidance](https://developer.apple.com/documentation/bundleresources/requesting-authorization-for-media-capture-on-macos).

An earlier live preflight received 240,128 exact-zero samples over 5.0027 seconds,
with no queue drops, timestamp discontinuities or backend errors. That evidence
established a silent input, not its cause. Apple documents that denied or
unanswered authorization can produce silent audio;
[the authorization API](https://developer.apple.com/documentation/avfoundation/avcapturedevice/authorizationstatus(for:))
provides the direct check. Even after permission passes, a muted input, input gain,
wrong routing or launcher attribution can still yield zero samples. Such a run
fails with `all_zero_audio_after_authorized_preflight` and remains unscored.
The initial preflight is not a successful acoustic qualification.

The small Objective-C boundary uses `objc2`, `objc2-av-foundation`,
`objc2-foundation` and `block2`. Three narrowly scoped functions permit unsafe
calls to the documented immutable media-type constant and authorization methods.
The permission callback owns a thread-safe bounded sender and no borrowed data;
AVFoundation retains its escaping block. Nothing from that path runs in the audio
callback. Pure tests cover permission-state decisions without invoking those APIs.

`devices` reports the default input's identity, input names and IDs, and their
default configurations. Listing does not establish microphone permission or
physical routing. `record` accepts an exact, unique input name; missing or
ambiguous names fail instead of choosing another input. It does not automatically
switch to a new default input. Linux builds expose the pure support code but the
recording and inventory commands return an explicit unsupported-platform error.

Use an output directory that does not yet exist. The tool creates it with mode
`0700`, creates artifacts with mode `0600`, and never overwrites a prior attempt.
The parent directory must already exist. These files contain private room audio;
they are local test artifacts and must not be committed or uploaded implicitly.

The command writes `ready.json` only after nonzero audio has passed through the
capture queue and writer without a known fault. Its status is
`capturing_not_yet_validated`. The experiment controller may start cached iMac
speech after this receipt, leaving enough recording time for pre-roll, the whole
reply and post-roll. A receipt is not proof that the intended microphone or
speaker is audible, nor is it final capture validity. The `record` command does not play
stimulus audio, use Gemini, or control Lamp. The separate `play` command below
provides explicit iMac output and cached fixture identity; the experiment controller
still owns scheduling and room annotation.

The requested duration is an integer from 1 through 600 seconds of WAV frames.
Rates are limited to 8,000–192,000 Hz, one through eight channels, and at most
1 GiB of float audio data. Unsupported default sample formats fail. There is no
resampling, downmixing or Lamp-owned DSP in this observer. It records the default
configuration actually delivered by the selected device; it does not claim that
CoreAudio or the physical input supplies unprocessed microphone audio.

## Capture and bounded handoff

The input callback copies into fixed-size packets and makes one nonblocking
single-producer/single-consumer queue push. The queue is allocated and touched
before capture: 32 packets, each holding at most 8,192 interleaved float samples
(about 1 MiB of sample storage). A too-large callback is rejected. A full queue
rejects the new packet and counts the lost frames; it never overwrites queued
audio, silently inserts silence, or grows. An independent worker writes the WAV
and per-callback JSONL ledger. Lamp-owned callback code does not allocate, log,
wait for the writer or acquire a mutex. That statement does not certify CPAL or
CoreAudio's internal implementation as lock-free.

Capture is bounded by the requested frame budget, at most 250,000 retained-window
callbacks, a five-second readiness deadline, a two-second callback-stall deadline,
and a wall deadline of requested seconds plus eight. A worker deadline is
requested seconds plus twenty. These are checked deadlines, not a guarantee
that arbitrary native initialization, a TCC prompt, stream teardown, or blocked
filesystem I/O can be interrupted by Rust. An external process deadline remains
necessary for unattended runs. Killing or crashing the process leaves the initial
attempt pending; a missing final metadata file is never success.

The final audio callback is trimmed to the exact requested frame count. Two later
callbacks are checked for timestamps and backend xrun reports without writing
extra audio; CoreAudio may report an xrun on a following cycle. The physical input
therefore remains active briefly beyond the recorded duration. Their counts and
timestamps are reported separately. A stall or missing tail check invalidates the
capture. The stream is stopped and dropped before the writer drains and closes.

## Artifacts and validity

- `attempt.json`: initial request, attempt ID and wall/monotonic creation times;
  initially `valid: false` and `pending`.
- `ledger.jsonl`: initial attempt, accepted audio callbacks with source and WAV
  frame offsets, and final report. Callback indices expose rejected or missing
  queued packets; final counters also retain failures absent from the audio.
- `room.wav`: interleaved IEEE float32 audio at the delivered rate and channel count.
- `ready.json`: a provisional capture receipt, if readiness was reached.
- `metadata.json`: final report, WAV SHA-256, format and input identities,
  default-input identity at start and end, callback/frame counters, timestamp
  checks, clipping, queue drops, backend xruns, stream error categories, writer
  status and start/stop timestamps.

For recording, exit status is `0` only for a valid capture, `2` for a completed
invalid/failed attempt, and `1` for invalid invocation or an artifact error
preventing completion. The permission commands return `0` only when their reported
`capture_allowed` is true, and `2` when blocked; they do not record audio.
Both the exit status and final metadata must be checked. Include invalid and
incomplete attempts in the experiment's failure denominator.

`valid: true` requires the complete frame budget to be received, retained and
written, readiness, successful shutdown and writing, nonzero input, two trailing
callback checks, and no observed fault or source sequence gap. Any known queue
loss, oversize or malformed callback, timestamp discontinuity, backend xrun,
stream error, callback limit, clipping, or nonfinite sample prevents validity.
An all-zero recording is rejected conservatively because mute/permission failures
can otherwise resemble silence. This does not detect every wrong physical route.

The digital clipping check flags absolute normalized sample values at or above
`1 - 1/32768`; it is intentionally conservative and may reject a signal touching
that threshold without an audible distortion. Nonfinite samples are written as
zero, explicitly counted, and always invalidate the attempt. Audio is not patched
to conceal dropped frames. A partial or invalid WAV remains useful for debugging
but must not be scored as continuous acoustic evidence.

Capture integrity does not establish successful conversation, absence of acoustic
clipping, speaker separation, audibility, or the correctness of Lamp's answer.
Driver faults that neither affect the reported timestamps nor generate a backend
fault are outside the observer's visibility.

## Timebases and acoustic annotation

Each accepted callback records three timestamps and actual input/retained frame
counts. `host_monotonic_ns` is Lamp IPC's OS `CLOCK_MONOTONIC` clock, usable for
same-host event correlation. CPAL callback and capture timestamps use CoreAudio's
mach absolute clock, converted to nanoseconds. Treat that as a separate clock
domain; no cross-domain offset is assumed. Host arrival jitter is reported
separately and is not automatically treated as missing audio.

The capture-clock increment is compared with the preceding callback's input
frame count divided by the delivered rate, with two sample periods of tolerance
for rounding and timing variation. Backward clocks, larger discontinuities and
backend-reported xruns invalidate the capture. This is a conservative continuity
check, not a measurement of oscillator accuracy. A backend latency-estimate change
can also cause a rejection.

In the pinned CPAL CoreAudio backend, the capture timestamp is the CoreAudio host
timestamp minus an estimated device latency. It is **not** independent evidence
of an ADC sampling instant, end of human speech, or first audible Lamp word.
Acoustic boundaries must be annotated from the valid continuous WAV:

1. Mark the end of the iMac question and the first substantive audible Lamp word.
2. For interruptions, mark the new utterance, Lamp's audible stop and retention of
   the new opening words; include talk-over and missed responses.
3. Record unsolicited replies and the complete duration of negative/background
   scenarios, including other-device speech and conversations between people.

WAV frame index divided by the recorded sample rate is the shared acoustic
measurement timebase. Provider logs and callback timestamps help diagnose a turn;
they do not replace its acoustic boundaries or prove speech-end-to-word latency.

## Finite cached-WAV playback

The separate output commands neither enumerate microphones nor query or request
microphone authorization. They do not use a model, generate speech, access the
network, change volume, control Lamp, or orchestrate a conversation:

```sh
target/debug/lamp-observer inspect-output --output 'iMac Speakers'
target/debug/lamp-observer play --output 'iMac Speakers' --wav /private/tmp/cached-question.wav --out /private/tmp/lamp-play-001 --cancel-file /private/tmp/lamp-play-001.cancel
```

`inspect-output` reports the exact selected output identity and its current
default configuration without opening a stream. The literal name `iMac Speakers`
is required and must match exactly one enumerated output. There is no default
output handle, alternate-device fallback or automatic reroute. Enumeration is
limited to 256 devices. An identity or configuration change detected after playback
invalidates delivery; software identity alone does not prove physical localization.

`play` uses that selected device's current default rate and channel count, with
its default buffer setting. This first implementation requires float32 output,
one or two channels, and 8,000–192,000 Hz. Unsupported configurations fail before
opening the stream. The pinned CPAL backend may configure the selected device's
physical stream format/rate; it does not select another output to make a fixture
work. Qualify the named route with an isolated room recording before a conversation
cohort. A system-default route is never sufficient evidence.

The source must be an existing regular, non-symlink RIFF/WAVE file, 44 bytes through
32 MiB, containing nonempty PCM16 mono or stereo at 8,000–192,000 Hz and no more
than 120 seconds. The parser rejects inconsistent lengths/alignment, duplicate
format/data chunks, float/compressed formats, exact-zero fixtures and PCM-rail
samples. Bounded metadata chunks are permitted. The descriptor is opened without
following the final symlink and in nonblocking mode, then checked as a regular
file; a FIFO cannot stall fixture loading. The loaded bytes' SHA-256 identifies
exactly the source used. No fixture is overwritten or regenerated.

Before a stream opens, the full output is rendered into at most 64 MiB of float32
samples. Equal rates preserve PCM sample values. Different rates use a documented
32-tap, phase-normalized Hann-windowed sinc filter with 0.94 cutoff, at most 4,096
phases, and zero padding at file boundaries. Mono can be duplicated into stereo
without changing gain; stereo is never downmixed. Output frame count is the
ceiling of source duration times output rate. Nonfinite or clipping conversion
fails instead of applying hidden gain. The report includes the conversion method,
source/prepared formats, durations, peaks, and a SHA-256 of the interleaved prepared
float32 little-endian samples. This is a reproducible benchmark conversion, not a
claim of transparent perceptual quality; qualify the 16 kHz mono to 48 kHz stereo
route physically. Source decoding and preparation together use bounded memory,
up to roughly 129 MiB of sample/filter storage while preparing the largest accepted
combination. No producer queue can starve after playback starts.

The output callback only copies prepared samples, zero-fills the remainder, and
updates atomic counters. Lamp-owned callback code performs no file I/O, allocation,
logging, conversion, or mutex acquisition. It checks at most 8,192 frames per
callback and 250,000 callbacks; malformed/oversized callbacks and known backend
faults invalidate delivery. This does not certify CPAL/CoreAudio internals as
lock-free. Host callback jitter is diagnostic. Output-clock gaps beyond two sample
periods, backward clocks and backend-reported xruns are failures; a changed latency
estimate may also fail conservatively. CPAL does not expose every possible driver
or hardware underrun, so zero reported xruns never proves their absence.

After the source has been submitted, the stream supplies intentional zeros until
at least two later callbacks have passed the same checks and a callback clock has
passed CPAL's estimated source-end output time. The controller then pauses and
drops the stream and captures final counters/errors. These are software delivery
and estimated-drain checks, **not** DAC measurements, audible onset/end, or acoustic
latency. A complete-looking source counter without the tail/drain checks fails.

An independent controller checks the optional cancellation path about every 2 ms;
creating any filesystem entry at that path requests cancellation. A preexisting
entry prevents playback. Cancellation remains observable if callbacks stop; the
controller requests pause without waiting for a new callback. It also enforces a
five-second first-callback timeout, a two-second callback-stall timeout and a wall
deadline of prepared duration plus eight seconds. Preparation checks cancellation
at bounded frame intervals. Native initialization/teardown and blocked filesystem
calls are not preemptible guarantees: unattended controllers still need an external
process deadline. Killing the process leaves pending/incomplete artifacts, never
an inferred success. There are no automatic playback retries.

The fresh private output directory contains `attempt.json`, `ledger.jsonl` and
`metadata.json`; playback creates no `room.wav` or capture-ready receipt. The
shared initial attempt warning refers to capture integrity, while final playback
metadata explicitly narrows `scope` to output delivery only. Final metadata records
selected route/configuration before and after, source/prepared hashes, stream
lifecycle timestamps, source/intentional-zero/rejected frame counts, callback and
error counts, and the stop reason. Fault bits are: backend xrun `1`, callback
shape/limit `2`, timestamp gap `4`, controller failure/timeout `8`, stream error `16`.
Stream-error category bits are: unavailable `1`, permission `2`, invalidated `4`,
changed `8`, other `16`. A missing final metadata file is incomplete. `valid: true`
and exit `0` mean delivery checks passed only; cancellation or failed delivery
returns `2`, while invalid invocation/artifact-finalization errors return `1`.

Callback/estimated output timestamps use the CPAL stream clock. Lifecycle and
callback arrival timestamps use the separate host `CLOCK_MONOTONIC` clock. Never
subtract across these clock domains or score response latency from scheduling.
Use the independently recorded continuous room WAV to confirm the actual stimulus,
first audible answer, quality and complete reply. Keep all incomplete/failed
playback attempts visible in the experiment ledger. Offline tests do not establish
route audibility, realtime callback cadence or acoustic acceptance.

## Dependency provenance and validation

This Mac test tool uses CPAL's pre-release `0.19.0` at exact upstream commit
[`79275c2313da30e9f8b1ad8168385d091a2c9ada`](https://github.com/RustAudio/cpal/tree/79275c2313da30e9f8b1ad8168385d091a2c9ada),
plus `rtrb 0.4.0` for bounded callback handoff and `hound 3.5.1` for WAV writing.
Playback uses the already locked `rustix 1.1` filesystem API for checked fixture
opening, `sha2` for source/prepared identity, and a bounded PCM RIFF parser.
The exact dependency graph is locked in `Cargo.lock`. CPAL `0.18.2` could not
coexist in this workspace with the tested runtime's `alsa 0.12.1`: its Linux
native-link dependency differed even when this observer used CPAL only on macOS.
The pinned upstream revision aligns that dependency without downgrading the
runtime's ALSA crate. Its declared Rust minimum is 1.85, below lampOS's 1.91
minimum. The timestamp/xrun semantics are audited against the
[exact CoreAudio backend source](https://github.com/RustAudio/cpal/blob/79275c2313da30e9f8b1ad8168385d091a2c9ada/src/host/coreaudio/macos/device.rs).

Pure tests exercise format/duration limits, ambiguous input names, queue overflow,
oversized/malformed callbacks, lost timestamp intervals, host scheduling jitter,
clipping/nonfinite input, final-frame trimming, delayed xruns, fresh artifact
creation and synthetic WAV/ledger preservation. No test opens or enumerates a
physical audio device, queries live authorization, or requests a privacy prompt. macOS TCC permission, actual microphone routing, CPU/disk
load tolerance, actual callback cadence and acoustic quality still require a
supervised real capture. Permission to run a test does not itself grant macOS
microphone access; the application or terminal launching the binary must also
have that permission.

```sh
cargo fmt -p lamp-observer --check
cargo clippy -p lamp-observer --all-targets --locked -- -D warnings
cargo test -p lamp-observer --locked
```
