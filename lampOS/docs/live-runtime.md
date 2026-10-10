# Directed voice integration

This is the first integrated Rust audio slice, not the complete lampOS release.
It runs a finite, explicitly directed conversation after exclusive hardware
ownership is established. It is not an always-listening desk mode: speech
activity alone does not establish that someone addressed Lamp. Do not use its
results to claim multi-speaker restraint, acknowledgment handling, full
choreography, or an end-to-end speedup before the physical comparison runs.

The primary benchmark remains pinned V1 main against Rust V2 on the real Lamp,
with the same cached iMac speech and a continuous room recording separate from
the conversational capture. The current observer is on Lamp; it is not an
independent external recorder.
See the [comparison protocol](comparison-protocol.md). The modified V1 is only
an installed system to preserve for rollback, never a comparison arm.

## Runtime path and latency boundaries

```text
physical mute process ----> interaction owner
                                   |
ALSA capture process -> AEC/VAD -> retained speech prefix
                                   |
                          Gemini provider process
                                   |
                         immutable reply ownership
                                   |
                        guarded ALSA speaker process
                                   |
                 actual accepted PCM -> AEC reference
```

The four audio/privacy workers have separate bounded control and data sockets.
An explicit `--ring-channel-ceiling 0..120` adds a fifth, separately supervised
ring worker. Its local policy follows admitted input, input-end and matched
playback transitions. See [conversation/ring ownership](ring-choreography.md).
The parent
owns conversation and cancellation; cloud work cannot execute in an audio loop.
Workers target a 2 ms service tick, with 20 ms authority publication and 100 ms
input leases. The speaker and capture processes also have a direct, private pair
of bounded reference control/data sockets; accepted PCM no longer passes through
the coordinator. The supervisor supplies their expected immutable peer boots.
These are scheduling targets and checked freshness limits, not
hard real-time guarantees. Physical mute is sampled every 5 ms, closes promptly
on observed mute, and needs 60 ms stable unmute before input may open. Missing,
reordered or old observations close the gate. Input readiness is announced only
after provider setup, the reference clock start, and an actual current processed
microphone block are retained.

Capture supplies 160 samples at 16 kHz per 10 ms block. Sonora's Rust AEC3 and,
by default, noise suppression run locally; local speech probability drives the directed
turn detector. A 300 ms retained prefix protects opening words. The
[input admission boundary](input-admission.md) retains a candidate before any
turn replacement. The finite directed policy still accepts VAD-only onsets
immediately; it does not qualify echo or addressee recognition. Speech onset
requires six consecutive blocks at probability at least 0.80; endpointing waits
600 ms below 0.35. A missing audio block is a fault, not a fabricated endpoint.
An utterance is limited to 120 seconds. These initial thresholds need acoustic
tuning; they are not an addressee classifier or a completed interruption design.

Speaker blocks contain up to 240 samples at 24 kHz. The continuous writer targets
10–20 ms queued PCM; the shared silence/speech ledger and negotiated ALSA buffer
remain bounded to 960 frames (40 ms). During idle it writes actual zero PCM,
certified by a zero-only API accepting a frame count and no sample buffer. Its
opaque authority requires a fresh session and unchanged allowed microphone
privacy; it does not grant a speech permit or announce listening readiness.
Every speech sample still requires its immutable turn permit. While a provider
reply is unfinished but no speech block is ready, the speaker continues writing
actual zeros with the same zero-only authority and shared ledger. These are
tagged `speech_gap`, preserve every queued speech sample, and do not finish the
reply. This keeps capture and interruption alive through provider pacing gaps;
it does not synthesize a reference from an empty socket or relax a reference
fault. Gap start/resume/cancellation receipts retain the affected immutable
owner, accepted cursor range, zero-frame count and host timestamps. A reply that
encountered a gap ends as `audio_written_with_playback_gaps_unscored`, never the
ordinary written-audio outcome. A failed or cancelled reply keeps its failure or
cancellation outcome. Acoustic gaps and word retention still require the room
recording; accepted zero duration alone is not an audible-gap measurement.

Provider-audio enqueue records retain each bounded IPC part's source frame
count/offset, wrapper event-observation time, coordinator enqueue time, reply
queue size, final-block holdback and outstanding speaker sequence. The wrapper
timestamp is not a network-packet timestamp. The coordinator still holds up to
one 240-sample block until more PCM or generation completion identifies the
final block; this deliberate holdback is visible separately from provider
arrival delays. No speculative jitter buffer has been added. An empty provider
completion creates no synthetic speech block; zero-valued provider PCM remains
owned output whose substantive audibility is unscored.

Startup first opens/configures the microphone without starting or reading it.
The speaker disables automatic start, accepts exactly 480 zero frames while
prepared, and checks the reported prepared queue against that count. Capture
resets DSP, analyzes those two certified reference blocks, and acknowledges the
exact epoch/cursor. Only then does the speaker explicitly start, after another
privacy check; capture then starts its prepared microphone and retains sequence
1. The prime/ack barrier has a checked 100 ms bound and reference connection has
a 3 s startup bound. Neither is a guarantee that arbitrary driver calls finish
within that time. There is no blind warmup mute or discarded opening audio.

Accepted render has an epoch, contiguous sample cursor, sequence, acceptance
time, queue observation time, and privacy lineage. Speech additionally retains
its owner. Partial accepted writes are assembled exactly, without invented
padding. Capture services direct reference before processing each microphone
block and rechecks priority lifecycle after receiving data, so separate socket
arrival order cannot bypass a reset. Empty IPC is never treated as silence.
Final provider PCM may include actual zero padding to complete its final block;
completion uses the final speech sample cursor's normal ALSA-delay retirement.
Idle zeros may still be queued. Discarded cursors cannot complete an answer.

Cancellation physically drops the old queue before publishing its loss receipt.
A new epoch is re-primed with actual accepted zeros and the old outstanding range
is attached to the prime. Late old-epoch packets cannot enter the new DSP. The
microphone sequence and upstream interruption prefix continue; only DSP state
resets. Privacy closure stops instead of reopening. A write accepted just before
its post-write permit expires keeps its old cursor and discard receipt; the
worker cannot relabel that discarded PCM as new-epoch reference.

Capture continues during that restart. At most the two capture blocks allowed
by the certified prime may be processed before the clock-start notice arrives.
Their host read-completion timestamps are retained in a fixed two-entry buffer.
The speaker reports host timestamps immediately before its `start_clock` method
call and after the successful ALSA start call. These bound driver startup; they
are not an exact physical DAC onset. The notice establishes a one-time phase
origin: only reads strictly before the method-call request count as
`capture_blocks_before_clock_start`. A read during the ambiguous call interval
or after completion receives no exemption, even when the notice arrives late.
`capture_blocks_processed` still counts every DSP capture call in the epoch;
the running-clock balance subtracts the separately reported origin. No capture
sample, microphone sequence, or render call is inserted or removed. Future,
duplicate, stale, or wrong-epoch starts fail; the origin cannot move again until
an explicitly validated playback reset. Prime observation, request, completion,
and notice receipt must be ordered; both call bounds must be under 40 ms old.
A failed or expired start call publishes no successful start notice and cannot
announce readiness. This corrects restart phase accounting,
not echo admission or adaptation lost when the DSP resets.

Jieli capture and C-Media playback have separate USB clocks. Reference cursors
prove delivered sample continuity, not synchronized physical capture time. The
runtime explicitly bounds DSP call imbalance to eight render blocks, reference
freshness to 40 ms, and incomplete render assembly to 50 ms. Missing coverage,
backlog, wrong epoch/cursor, privacy mismatch, bad queue timing, or barrier loss
produces a typed reference fault, not invented coverage. Queue-derived AEC delay
is a nominal-rate estimate from the last queue observation and capture delay,
aging the prepared prime from the conservative start-call request bound.
An exhausted observed running queue still faults even when phase accounting has
remaining credit. The fixed origin does not compensate ongoing independent-clock
drift; drift and true acoustic delay remain unqualified. Host read completion
is not an ADC timestamp. Bounded once-per-second
reference timing records and exact fault cursors are retained in the event trace.
The finite component-test speaker API still supports a declared drain; the live
continuous clock never drains at each answer.

The release targets remain speech-end to first substantive audible word p50
at most 2 seconds and p95 at most 4 seconds, and admitted interruption to audible
silence p95 at most 200 ms. Provider-first-audio, host microphone reads and ALSA
acceptance are diagnostic boundaries. None is an acoustic answer timestamp.

## Ownership and failure behavior

Every reply keeps the turn and output plan that created it. New admission or
provider interruption revokes that output, discards queued reply PCM and
publishes cancellation to the speaker. A late callback cannot borrow the new
turn. Mic permission has its own monotonically increasing generation, so a
coalesced denied-to-allowed transition is visible without reopening capture on
every ordinary conversational turn.

Priority authority can overtake PCM on the other socket. The provider wrapper
permanently retires superseded request IDs, closes an actually submitted open
input once, and ignores subsequently delivered commands for that retired input.
Unknown/future requests still fail. The Gemini transport holds a bounded new
prefix while waiting for the old response's idle barrier, and fails explicitly
if that barrier does not arrive; it does not silently relabel old server audio.

Authority-before-Start send order does not impose receive order across the two
Unix sockets. The Gemini intake rereads priority control after receiving each
input command, before submitting it. One service tick shares a budget of at most
16 control packets and 8 input commands. Exhausting the control budget defers
both input submission and IPC output; already observed request retirement is
still applied locally. At most one input command is retained across ticks with
its original receipt time, request, privacy generation, sequence and capture
timestamp. It expires at 100 ms after that original receipt; repeated authority
does not renew it. Stop or a microphone privacy-lineage change discards the
retained command and shuts down the provider. Unknown/future owners after an
empty control drain remain immediate protocol errors; no grace period or new
capture/authority/heartbeat lease was added.

This reread adds only bounded nonblocking local work. The cooperative service
target remains 2 ms, without waiting for cloud I/O or lengthening the endpointer.
A deterministic test using real separate local sockets and a recording provider
sink reproduced the prior first-question failure, then passed with the reread.
Additional tests cover interruptions and old-data retirement, Stop/privacy,
coalesced privacy changes, control-budget deferral, original retention expiry,
and unchanged input/authority leases. These tests use no cloud or devices; their
success is not an acoustic latency or interruption measurement.

Startup continues servicing earlier workers while later children initialize.
The first microphone frame can trigger another control drain to consume its
already-sent open report; a valid cross-channel arrival race must not lose it.
An unfinished turn receives an explicit terminal outcome on user/provider
interruption, session deadline or runtime failure. Nonzero child exits and
forced shutdowns fail the run. Stop is sent to all workers before waiting for
any one child; remaining processes are reaped. A single overall cooperative
grace is 300 ms, or 750 ms when audio diagnostics are explicitly enabled. The
coordinator drains final control receipts while all children finish; the grace
is never multiplied by the worker count. This is not a hard guarantee about
arbitrary OS cleanup.

With the ring enabled, successful shutdown also requires its explicit black
write after the stop request. Missing, duplicate, future or pre-stop receipts
fail the run even if the child exits successfully. This confirms write return,
not optical darkness. A crashed writer still needs exclusive replacement and
physical recovery qualification.

After a successful local speaker stop and diagnostic reset, privacy closure and
explicit Stop may send one terminal reference notice over an already-connected
control socket. Capture shuts down independently and may have closed that socket
first. This final notice is optional: it never connects, retries, waits, or
substitutes for a worker/diagnostic acknowledgement. Its delivery failure cannot
change a clean local stop into an audio fault. Running reference/control/data
send failures, local stop failures, and diagnostic certification remain strict.
The target is zero added audio-stop delay: the optional send occurs only after
the existing local stop/reset. Actual privacy-stop timing still requires native
measurement; portable socket tests do not establish a kernel or acoustic deadline.

This slice records failures but does not yet guarantee an audible failure
message when the provider or speaker fails. That remains required release work.
There is no reconnect/retry that silently hides a failed trial.

A queued speaker-permission failure now retains one bounded discard receipt
with its immutable owner, typed reason and discarded frame count. New PCM
acceptance waits until the worker reports that receipt; the report precedes
final-speech retirement feedback and the next echo-reference epoch. This also
applies when installing a newer snapshot or checking a just-accepted write
causes the discard.
Only a recorded cancellation of the same older owner, with `stale_owner` and
no mixed ownership, counts as expected cancellation. Expired current speech,
unexplained owner loss and ambiguous mixed ownership fail the trial as
`playback_discarded`. They cannot produce `audio_written_unscored`. This patch
adds delivery correctness and diagnostic evidence; it does not widen permits,
change gains or qualify playback-time interruption admission.

## Private configuration and commands

Provision a mode-0600 JSON file and separate mode-0600 credential file owned by
the user running the process. The credential path must be absolute. Files are
bounded regular files; symlinks and group/world access are rejected. The runtime
reads no HAL or os-server configuration. An operator may migrate existing values
once into these independent files; credentials stay outside source and reports.

The JSON fields are `endpoint` (the complete secure WebSocket URL), `model`,
`voice`, `credential_file`, and optional `authentication` (`api_key`, `bearer`,
or `ephemeral`), `thinking_level`, and `language_code`. Thinking/language remain
omitted by default; explicit values are validated and sent without a silent
fallback. See [matched-model configuration](gemini-comparison-config.md). Use the actual provider endpoint; a proxy base URL alone is not
the complete WebSocket route. The provider-check command opens no microphone and
sends no speech. It establishes only that setup was accepted, not that a complete
answer or account quota will succeed.

```sh
cargo build --locked --release -p lamp-live
target/release/lamp-live provider-check /absolute/private/provider.json
target/release/lamp-live directed /absolute/private/provider.json 60 /new/private/run-directory
target/release/lamp-live directed /absolute/private/provider.json 45 /new/private/diagnostic-run --diagnostics
target/release/lamp-live directed /absolute/private/provider.json 45 /new/private/aec-only-run --diagnostics --noise-suppression off
target/release/lamp-live directed /absolute/private/provider.json 45 /new/private/ring-run --diagnostics --ring-channel-ceiling 24
```

`--noise-suppression on|off` selects a controlled software-processing experiment
before capture starts. The default is `on`, preserving `EchoProcessor::new(true)`;
`off` constructs `EchoProcessor::new(false)` with Sonora AEC3 still mandatory.
Both modes use the same reference clock, VAD thresholds, privacy and ownership
checks, gain settings and deadlines. The option does not describe or change any
Jieli onboard processing. It is independent of `--diagnostics`; either flag
order is accepted, while missing, repeated or unknown values fail startup.

The supervisor sends an explicit named mode to the capture child and records
`software_processing_requested` in `run_start`. The worker reports its actual
typed `noise_suppression` in `CaptureStarted`; a mismatch fails before its epoch
or first microphone frame can establish readiness. The capture diagnostic pending
manifest and final result retain the same actual software mode. The private
child CLI also requires named diagnostic paths, so an old positional path cannot
be mistaken for a mode. This option enables matched experiments; it does not
establish that either setting improves live echo rejection or conversation.

`--diagnostics` opts into bounded local pre/post-AEC capture and actual accepted
render PCM, including prime and idle zeros. It is disabled by default. The
capture and render workers create separate fresh private leaves under the new
run directory before activation. Their recorders use the common controller boot;
worker transport boots remain separate identities. Audio loops only submit to a
fixed nonblocking queue. Overflow, missing opening frames, cursor gaps, invalid
privacy/reset lineage, limits, or runtime faults invalidate the recording.
No PCM is sent to a diagnostic cloud service.

Hardware authority is invalidated and microphone/speaker handles are closed
before each recorder waits up to 500 ms for its writer. The parent accepts a
recording only with its matching startup identity, an acknowledged valid finish
receipt, and the exact SHA-256 of its bounded regular `complete.json` file.
The common session boot, stream and start time must agree. A late completion
marker cannot rescue a timeout or missing receipt. Requested diagnostics that
fail certification fail the qualification run, without delaying privacy closure.
The recorder's 600-second wall/sample cap includes worker startup; choose a
shorter directed duration to leave setup and shutdown headroom. These files are
software/driver diagnostics; they cannot replace independent room audio for
first audible word or interruption silence. See [audio diagnostics](audio-diagnostics.md).

`directed` is Linux-only and refuses active HAL, os-server, boot-ring and
shutdown-ring services. It uses the observed board aliases
`plug:device_micro2` and `plug:device_speaker`, and the existing active-low mute
input at GPIO chip 1 line 9. It does not switch mixers or silently choose another
device. A future installer must provision and validate these board settings.
Stopping legacy services, checking placement, and preparing a safe rollback are
operator deployment duties; the command does not stop services itself.

The runtime writes a fresh private `events.jsonl` after a finite run, including
failed trials. Trace storage is bounded at 20,000 events. Runtime output audio
is bounded at 30 seconds of 24 kHz PCM, input handoff at 64 commands, and provider
transport/IPC queues have separate limits. A trace failure cannot become a valid
benchmark. A complete report with `completed_unscored` still needs a valid room
recording, acoustic annotation and correctness review.

The Gemini session is voice-chat-only: native audio, manual activity boundaries,
no delegated tasks or search tools. The configured instruction cannot override
local privacy/output checks. Dependency debug/trace logging is disabled at
compile time to avoid raw WebSocket handshake logging exposing authentication.

## What remains before release

The directed slice still needs real acoustic route verification and paired
V1-main/V2 conversations. The [Mac room observer](room-observer.md) needs native
microphone consent. For current routing preflights, the previously working test
setup uses explicit iMac-speaker playback and a separate capture channel on Lamp
(`plug:device_micro1`, C-Media) while the conversation uses `plug:device_micro2`
(Jieli USB). The former records the complete room exchange without endpointing or
AEC; it shares Lamp's host and C-Media card with playback, so it is not an external
recorder. Preserve capture errors, full frame counts and the ALSA configuration
for both arms. A successful routing preflight is not an answer-latency result.
The external `arecord` and prior playback helper are temporary qualification
tools; neither is a lampOS runtime dependency or proof of a standalone final
evaluator. An external room recorder remains the stronger independent check.

On the tested lamp-4ace, the two active input roles are the Jieli conversation
microphone and the C-Media ambient microphone. The latter is an existing sensing
input temporarily reused as the benchmark recorder, not an added microphone.
The camera also enumerates a USB recording endpoint, but the hardware notes
report near-silent capture; enumeration alone does not establish a working mic
or the number of physical microphone capsules. Do not call the ambient input a
spare mic or present it as an independent external recorder.

Live USB descriptors on 2026-10-10 report mono signed 16-bit capture at 48 kHz
for Jieli and 44.1/48 kHz for C-Media. ALSA converts both test captures to
16 kHz. Jieli reports capture 147/147 with automatic gain control on; C-Media
reports capture 4/35 with automatic gain control off. These are different mixer
scales and gain conditions, not comparable microphone sensitivity measurements.
The USB chips and adapter label do not establish the microphone capsule model,
frequency response, self-noise or directionality. Compare identical cached
speech/noise with recorded gain settings before choosing an input. Independent
USB inputs are not a qualified synchronized direction-finding array. The team
subsequently identified the Jieli input as an internally processed dual-capsule
board: one USB channel does not imply one physical microphone. See the
[microphone topology](microphone-topology.md) and the separate direct-human versus
loudspeaker-replay qualification requirement. Ambient
RMS monitoring can share a suitable speech capture source; a second input must
justify itself through measured benefit. Echo reference comes from PCM accepted
by the speaker, not from a second room microphone.
Optional ring phase cues now share the directed conversation owner; physical
light synchronization and visual quality remain unqualified. Camera,
environmental sensors, motor choreography, desk calibration,
addressee decisions, natural acknowledgment versus interruption handling,
recovery, spoken failure paths, and the specified stress/soak tests remain.
Component and null-device tests do not substitute for those measurements.


## First physical integration attempt: 2026-10-10

Source `528164cb94df737c33504624b2a02a293ad7c784820f3a09e9cbd2f320749e02`
passed 195 native checks (including one doctest), strict clippy and formatting;
the source-only Mac copy passed 170 checks. Its first real-device directed pilot
failed before `listening_ready`. The provider child reported a closed event
stream and the parent subsequently received IPC connection refusal. No iMac
question was played and no answer latency was measured. Provider setup acceptance
alone did not establish a usable voice session. Preserve the failure as attempt
`directed-pilot-oovtns0e`; the backend reason was not yet surfaced by that build.

The ambient recorder was stopped early on the failure. Its WAV header declared
70 seconds but only 2.1100625 seconds of PCM were readable. The recording is
incomplete and rejected, rather than accepted from its header duration. The
runtime workers were reaped and no ALSA or physical-privacy device owner remained.

Legacy services were stopped gracefully for exclusive access. HAL's shutdown
log confirmed held-joint pinning and serial disconnect without releasing held
torque; camera images before/after showed the same folded stationary pose.
A temporary systemd runtime drop-in, `hal.service.d/90-lampOS-stationary.conf`,
sets `InaccessiblePaths=/dev/ttyACM0` on legacy restoration. It prevents a restored
legacy process from commanding the servo bus; it is not a lampOS motor driver
or a physical movement qualification. Keep its presence in the run manifest and
verify restoration separately. The shutdown LED oneshot was stopped after HAL
(the inspected hook sends a black ring frame only), so it cannot contend during
the trial. This is an integration record, not the pinned V1-main comparison.

The next diagnostic revision preserves the Gemini terminal error category and
numeric close code even when the event channel closes before the polling tick.
It publishes terminal state before observable channel EOF. This changes
diagnostics/lifecycle ordering, not provider setup, speech policy or timeouts;
the initial pilot alone did not establish a transport defect.

The second physical attempt, `directed-pilot-u6kxdx0y`, used source
`6fb100f72bff218440a5be341a7c396099c9a7096e2e73d48ee71a29e6f25c4e`.
It also stopped before readiness and played no iMac question. Improved diagnostics
identified `UnsupportedMessage`. A separate bounded no-audio probe using the
same setup fields confirmed `setupComplete`, followed by
`sessionResumptionUpdate` with a string handle and boolean `resumable`, then
six seconds of idle connection. Only field names/types were recorded; no handle,
credential, raw message or room audio was sent or retained by that probe.
This establishes a compatibility defect in our handling of optional metadata,
not a quota rejection. Session setup acceptance by itself remains insufficient
for a conversation pass.

The compatibility fix accepts the documented optional
[`sessionResumptionUpdate`](https://ai.google.dev/api/live#SessionResumptionUpdate)
object, validates its known field types and discards the handle during parsing.
It does not request or perform session resumption, emit a readiness event, change
turn ownership, or accept tool calls. The existing message-size bound and
conflicting-message rejection remain. Tests cover setup, unsolicited metadata,
first input and owned reply. The next physical attempt below reached readiness
and recognized the question, but did not complete an answer.


The third physical attempt, `directed-pilot-dvqdikyb`, used source
`8877494284a6cac948412289cf85df1ccca5c23253feead425e2d651fe7ad8ed`.
It reached `listening_ready`, then the explicit iMac speaker played the cached
question. Gemini returned the partial input transcript “How are you doing
today?”; this does not verify preservation of the opening name. The provider
then failed with `MalformedMessage` before any reply PCM. The incomplete room
recording is rejected and there is no answer-latency result.

A bounded diagnostic subsequently sent only the cached synthetic question
directly to the same provider. It observed valid empty top-level JSON objects
between transcript/audio messages and completed a generated answer. That probe
opened neither microphone nor speaker and is not a physical conversation pass.
The Rust decoder now accepts exactly valid empty objects as no-ops, preserving
readiness, ownership and sequence barriers. Unknown-only messages, malformed
JSON, conflicting message kinds and tools remain rejected. Regressions cover
empty messages before setup and interleaved with an owned response. Native
validation and the next physical pilot must qualify the correction.


The fourth physical attempt, `directed-pilot-hahmsqek`, used source
`1686b4233eda555e65a94c88ec978633f7ab78bd4adfd71c9895968dd8d18a60`,
after 58 native tests plus one doctest, strict workspace clippy, formatting and
a release build passed. The provider produced reply PCM and an output transcript
starting “I'm doing well, thank”. The speaker accepted its first frames, then
faulted with `queued playback ownership exceeds the 960-frame bound`. This is
a failed, partial playback attempt, not a conversation pass. The input transcript
again omitted the opening name. The next correction must bound writes before
ALSA acceptance without increasing the 40 ms ownership ledger or relaxing
revocation. No accepted acoustic latency was measured.


## Two-input recording sanity check: 2026-10-10

Two 30-second trials recorded the same cached iMac question simultaneously on
both existing inputs. Both returned all 480,000 mono PCM16 frames at 16 kHz,
with no recorder errors, no PCM rail samples and no iMac output underflows.
These trials include no Gemini response and do not measure conversational
latency. The fixture SHA-256 is
`29b5d2a18fe92163b0fbb4cbddd7fff105fc329ebfcbdb165b33c7841a9985f3`.

| Input/setting | First trial speech-window RMS | Second trial speech-window RMS |
|---|---:|---:|
| Jieli, capture 147/147, AGC on (unchanged) | -17.06 dBFS | -16.77 dBFS |
| C-Media, capture 4/35 then 35/35, AGC off | -60.54 dBFS | -29.33 dBFS |

The ambient input gain increase raised both speech and background by about
31 dB. Its speech-window versus quiet-window RMS difference remained about
10 dB (9.74 then 9.94 dB); gain alone did not improve that separation. Its
maximum recorded peak at the higher gain was -14.74 dBFS. The original 4/35
gain was restored and verified after the second trial. Keep the Jieli input
for the present conversational tests; neither this small sample nor different
gain/DSP conditions rank the capsules' inherent quality. Do not interpret
Jieli's much quieter recorded idle level as a calibrated room-noise reading.

Alignment uses correlation with the known source; speech-window RMS includes
background sound. These are digital recording levels, not acoustic SPL/SNR.
The separate input streams' WAV offsets do not establish device latency.
Local offline ASR recovered the main question from both first-trial inputs, but
misrecognized the opening name in both. This is an ASR hypothesis, not evidence
that the microphone discarded the name or a human intelligibility score.
Evidence: `dual-mic-2u5j6a17` and `dual-mic-fni_n6yr`; raw WAVs remain private
outside the source bundle. More speech levels, noise, overlap and gain/AGC
conditions remain necessary before selecting permanent settings.


The speaker correction now observes ALSA availability and delay together, then
caps each offered write by requested samples, available frames, and remaining
ownership credit before calling ALSA. A full ledger applies backpressure while
priority control continues. It does not enlarge the 960-frame limit or retire
permissions merely because the device has writable space. Actual driver timing
behind the failed pilot was not captured; the code previously enforced the
ownership bound only after a write had already been accepted. New regressions
cover consumption between observation/write, delay beyond writable-ring occupancy,
and repeated bounded playback under irregular consumption. Impossible accounting reports
only frame counts, never audio payload. Native and physical checks follow.


The fifth physical attempt, `directed-pilot-q0xpcx_j`, used source
`2f4d597a0f3105c8dd9a5bc2bda701c4716d5e30569b8d96db4c31408967a5fa`.
Native audio/Gemini/live checks passed 110 tests including the doctest, followed
by strict workspace clippy, formatting and release build. Both the runtime and
the 70-second continuous recorder exited successfully; all 1,120,000 room PCM
frames were readable, and the explicit iMac playback reported no underflow.

The provider completed its turn and the speaker completed its queued audio.
Local offline ASR recovered the complete answer from the room recording:
“I'm doing well. Thank you. Ready to help with whatever you need. How about
you?” Waveform/spectrogram review puts the question tail at approximately
5.51–5.56 seconds and first answer sound at 7.505–7.525 seconds in that same
WAV: a **provisional single-trial estimate of about 2.0 seconds**. This has no
human listening signoff or p50/p95. The later V1-main pilot below is not yet a
matched aggregate comparison. The observer is
still on Lamp, and the quiet input's tail boundary has limited precision.

The legacy scorer's whole 20-second source correlation was ambiguous because
the synthetic source contains a long silent tail and Lamp's reply is much
louder at this observer. Reject that alignment. A separate active-speech-only
normalized correlation found the question at correlation 0.3563, matching its
waveform and local transcript. Keep both outputs and the manual interval note;
do not silently turn an ambiguous automatic result into an accepted aggregate.
Future standalone scoring must account for silence-padded fixtures. This pilot
does not establish natural interruption, background-speech restraint, body
choreography, repeated-turn reliability or a Rust speedup.


## Pinned-main baseline preparation, 2026-10-10

The isolated V1-main bundle uses commit
`d5efe9d7b73cc529b34cd4abe97624682a82ca94`. Its locked CPython 3.12.14
Linux ARM64 environment installed successfully; synthetic Silero, TEN, WebRTC,
AEC3 and bundled Smart Turn 3.2 initialization passed. A real systemd namespace
probe verified all 695 pinned HAL source entries, private state/control paths,
read-only source mounts, and the inaccessible servo device. Only
`CAP_DAC_READ_SEARCH` is retained for traversal of the owner's private staging
parents; host home permissions are unchanged. These checks do not establish
conversation latency.

The separate baseline OS and HAL started successfully. HAL reports audio, voice
and TTS available; the servo remains disconnected under the stationary inhibit.
The installed modified HAL/OS remain stopped and are not comparison arms. The
existing Hermes gateway was also stopped so this chat-only baseline cannot reach
installed agent sessions. Its stop command returned zero, but the gateway itself
exited with status 1; that cleanup result is retained for restoration.

The generated benchmark overlay also applies the shared no-filler/no-gaze flags
from `comparison-protocol.md`. These explicit safety overrides differ from the
stock profile; the frozen application source is unchanged. The private saved
volume is set to 75 to match the current test setting; the installed saved value
was 57 and is left untouched. Existing software sleep was cleared through the
normal greeting API after verifying the physical microphone switch permitted
input. The motor namespace remains in force during that API call and shutdown.

Main selects `gemini-3.8-live-extended-thinking`, Kore and LOW thinking. The first
Rust pilot uses `gemini-3.8-live`, Kore. Keep that model/configuration difference
visible; an initial comparison is not a Rust-language-only speedup. The first acoustic baseline pilot has now completed; its single provisional
measurement is recorded below. No paired aggregate statistics exist.

The standalone Rust Mac player also completed a physical route qualification
(`dual-mic-zu739ooi`): the same cached 20-second stimulus on explicit iMac Speakers,
with complete 30-second recordings on both existing inputs. Player delivery
checks passed, no PCM rails were found, and active-speech source matching was
confirmed. This qualifies that fixture/route under the recorded conditions, not
an answer or interruption. Raw recordings remain private. The external recorder
is still temporary benchmark tooling, not a lampOS runtime dependency.


### First completed V1-main physical pilot

`v1-main-pilot-e363pbra` captured the complete 45-second exchange with no recorder
or Rust test-player delivery failure. The same cached question elicited:
“I’m feeling great and ready for a wonderful day.” Provider text and offline
room ASR agree. Active-speech correlation plus waveform/spectral review place
question end at approximately 5.16–5.21 s and first substantive answer sound at
9.60–9.61 s in the continuous WAV. A preceding brief click is not scored as a
word. This is a **provisional 4.39–4.45 s gap, rounded to 4.4 s, n=1**.

Main logged `answer_latency_ms=1765` from its internal endpoint boundary. That
value omits earlier endpoint/session waiting and must not substitute for the
approximately 4.4 s acoustic gap. The older V2 pilot was approximately 2.0 s,
also n=1; model and player differences, first-use/session state and lack of
repeated paired trials prevent a validated speedup or p50/p95 claim. The low
level of the iMac tail and lack of human listening signoff remain limitations.
The full metadata and private recording reference are in
`artifacts/v1-main-pilot-e363pbra/`.


### Failed V2 repeat with the standalone Rust player

`directed-pilot-_iiznjk4` reused the same V2 binary and provider configuration as
the approximately 2.0-second pilot. It used the exact cached question and Rust
iMac player used for the V1-main trial. The 70-second recording is complete,
player delivery checks passed and the runtime exited zero. The interaction
**failed**: it admitted a second turn while answering the only scheduled
question, cancelled the first answer and generated a Spanish response. There
was no scheduled second utterance or interruption. The provider's unexpected
input transcript was “¿Qué?” and its second answer was “Perdón, ¿en qué te puedo
ayudar?” The English-only offline ASR is not suitable to verify that Spanish
wording; preserve the provider text as such.

Second-turn admission occurred about 519 ms after the first speaker write.
Speaker echo is a hypothesis, not a proven cause without raw/reference/clean
signal evidence. The current directed detector admits from post-AEC speech
probability alone; it does not yet qualify double talk during playback. The
retained prefix timestamp is not a measured acoustic speech onset.

The audit also found a delivery-accounting gap: a failed queued-output guard
can flush playback without surfacing its reason and affected owner to the
coordinator. Same-turn echo resets were observed in this repeat and the earlier
pilot; the current trace cannot establish whether lease expiry caused them.
The correction must retain immediate revocation and expose lost delivery, not
widen leases or count a clean exit as a correct answer. This repeat stays in
the failure ledger; no successful reply latency is assigned. Evidence is in
`artifacts/directed-pilot-_iiznjk4/`.


A subsequent 30-second raw echo diagnostic, `echo-only-PL7oUGD6`, played the
cached sentence deliberately from Lamp's speaker with Gemini stopped. Both
inputs recorded all 480,000 frames; mixer settings were unchanged. Jieli peaked
at -0.26 dBFS with speech-window RMS -15.46 dBFS and zero PCM rail samples.
C-Media peaked at -19.55 dBFS. This shows little peak recording headroom for
that test signal, but does not prove analog/digital clipping, an AEC defect or
the source of the unexpected second turn. It used a cached test voice and
`aplay`, not the actual Gemini waveform or Rust speaker scheduling. No permanent
gain or AGC decision follows from this one diagnostic.


### Native discard correction and repeated admission failure

Source `beb6d6585fa0b819cf5f89a3077d3c4612f3540e1d8d5ef0aa07c1f4b62a21f4`
passed 115 native tests including the doctest, strict workspace clippy, formatting
and release build. Pilot `directed-pilot-zejfwxq7` then played the same single
iMac question. It admitted four turns in total, cutting off three answers.
The new receipts identified three `stale_owner` discards following those
admissions (888, 912 and 912 queued frames), all expected cancellation of the
old owner. No permit-expiry discard occurred. This validates useful diagnostic
evidence, not successful conversation: the final provider output was “Hmm?”,
another unwanted behavior in the failed exchange.

Provider input fragments “Great.” and “I'm” match portions of Lamp's replies,
consistent with residual speaker echo. Raw/post-AEC conversation audio and exact
render-call evidence are still absent, so the cause is not declared proven.
Source inspection found that reference delivery currently advances only for
accepted speech blocks while capture continues; it has no explicit continuous
known-silent render timeline. Each cancellation also cold-rebuilds AEC and VAD.
The next experiment must measure reference/capture timing and convergence, and
represent authoritative silence explicitly. An empty IPC queue is not proof of
silence, and disabling input while speaking would defeat natural interruption.

The temporary observer in this trial started after runtime readiness and stopped
at runtime exit instead of recording an unmonitored tail. Its raw PCM was
wrapped in a WAV using the actual 717,885 complete samples (44.8678125 seconds).
The harness initially rejected `arecord`'s expected post-SIGINT EINTR diagnostic;
that original failure report is preserved. Future handling recognizes only that
exact diagnostic after an intentional stop, not other recorder errors. Observer
lifecycle coupling is not a qualified physical-mute latency measurement. No
accepted answer latency or successful interaction is assigned to this trial.


After these finite trials, the preserved installed Hermes gateway, os-server
and HAL were restored at 08:15 UTC. HAL health reports voice/audio/TTS available.
The running HAL mount namespace still maps the servo device to the inaccessible
0:0 node, and the real serial device has no owner. Speaker soft volume remains
75. This is a stationary rollback to the installed runtime, not V2 acceptance
or a new acoustic voice pass. The gateway start initially exceeded the client's
30-second wait because its existing pre-start step waits for HAL readiness;
that same job later completed, and was not restarted just to obtain status.
Both the timeout and completed restoration are retained in
`artifacts/installed-runtime-restoration-20261010/`.

The existing synthetic DSP quality benchmark skips its first 500 ten-millisecond
blocks (five seconds). That steady-state score cannot qualify echo rejection
during the first word of an answer. Cold first output, output after silence and
output after cancellation need separate measured cases alongside genuine
overlapping speech.

## Continuous-clock native and hardware checkpoint (2026-10-10)

Source `dbfe036eb37df93b9cdd93afe73c582e53e22c1effee915b4e979666678fd62d`
passed 160 native ARM64 tests (0 failed, 0 ignored), workspace strict clippy,
formatting, and the release build. The snapshot used a fresh private Cargo target
directory. It contains continuous accepted idle zeros and the direct private
speaker-to-capture reference link; the later opt-in PCM diagnostics integration
is not part of this identity. The source-only manifest, exact commands and logs
are retained in `artifacts/continuous-clock-dbfe036e/`.

A 12-second finite directed session then ran on lamp-4ace's real Jieli microphone
and C-Media speaker path, with no stimulus played. It returned zero, admitted no
turns, generated no replies, and produced 12 reference-clock receipts with no
fault or reset. At those receipts the analyzed render count stayed two 10 ms
blocks ahead of processed capture; accepted-to-analyzed time was 145–1,627 µs,
and the latest observed speaker queue contained 456–480 frames (19–20 ms at
24 kHz). These are sparse software/driver observations, not physical acquisition
or audible-output timestamps. No room recording was made in this smoke test.

This verifies a short idle startup/continuity/shutdown path only. It does not
establish echo suppression during speech, natural interruptions, independent USB
clock stability over long sessions, or a V1-main versus V2 latency improvement.
The installed runtime was restored afterward; HAL's servo-device mask and the
unowned physical servo port were verified. Movement remained inhibited.
