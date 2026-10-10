# V1-main versus Rust V2 interaction comparison

Protocol frozen for preparation on 2026-10-10. This source audit performed no
SSH, playback, timed V1 runs, deployment or V1 edits. It establishes how to prove
an upgrade; it does not establish that V2 is already faster or ready.

**Primary evidence is live conversation on the physical Lamp:** cached speech
played through the iMac speaker → Lamp microphone and real input processing →
actual Gemini interaction → Lamp's physical speaker. Confirm which device emits
the stimulus and response. Record a continuous, independent room-audio track
covering the complete exchange. Measure final user speech sound → first
substantive audible answer word, addressed interruption → audible silence,
opening-word retention, and the subsequent answer. Preserve every failed,
rejected, late, interrupted and expected-silence attempt in the ledger.

DSP, IPC, controller and synthetic playback measurements diagnose components;
they are not the primary end-to-end result. Readiness requires the actual
conversational path, including its listening and restraint failures, to pass.

Historical paths below identify evidence in the old repository. They are not
lampOS build/runtime dependencies. Any common evaluator adopted for V2 must be
owned and versioned within lampOS; legacy Python tools remain optional, external
validation tools for the V1-main baseline. See [architecture](architecture.md).

## Arms and attribution

| Arm | Frozen identity | Purpose |
|---|---|---|
| A: V1-main + shared safety overrides | Main source at `d5efe9d7b73cc529b34cd4abe97624682a82ca94`, built into an isolated baseline bundle. Do not use a moving `main` reference. | Team source baseline, with explicitly disclosed safety changes. |
| B: Rust V2 | Source archive/content manifest, Cargo.lock, compiler/target/features/build profile, binary hashes and provisioned configuration; no legacy HAL or os-server running as a runtime dependency. | Complete replacement candidate. Component-only implementations cannot receive a product-level pass. |

Publish **A vs B** only. This is the total product change, not a measurement
of the Rust language alone. Freeze both configurations before holdout evaluation;
do not select the fastest trial after seeing the results. Modified V1 is excluded
from evaluation. Historical private deployment inventories remain rollback
evidence only; they do not identify the V1-main baseline.

Extract A from the pinned commit into an isolated baseline directory. Retain its
full source/content manifest and any separately identified safety or measurement
patch. Do not substitute the live installation or a dirty local worktree for
main. A source commit alone is not a complete deployment identity.

## Freeze the actual runtime without exposing credentials

For every arm retain a private, immutable run manifest containing:

- Base commit, separately disclosed safety/measurement patch IDs, explicit
  source inventory, per-file SHA-256 and aggregate source ID; source archive and
  built binary IDs. No experimental V1 fixes, user memory, caches, credentials or
  calibration copied from the existing installation into baseline source.
- Actual baseline HAL source/resources and native extension hashes for A;
  the running os-server executable hash, interpreter path/version and installed
  distribution names/versions. Hash model assets separately. A package lockfile
  alone does not establish installed wheel/native DSP versions.
- Effective service/process IDs and start times, boot ID, kernel, ALSA/driver
  versions, CPU governor/affinity, thermal/throttling state and enabled workers.
  Use PID + process start time to avoid confusing a reused PID with the old worker.
- Effective, typed, allowlisted configuration: input/output ALSA aliases and
  rates/periods/buffers, DSP/AEC/NS/delay/reference settings, endpoint/VAD mode and
  thresholds, Live/turn-based mode, addressing/wake/follow-up policy, session
  park/keepalive/resumption, model/voice/language, search/thinking, TTS speed,
  output gain and hardware/software mute, camera/privacy and motion policy.
- Sanitized provider route/model and an opaque credential-set label; prompt and
  synthetic conversation-context IDs/lengths. Keep raw personal context private.
  Record account/quota availability without recording its key/token.

Collect settings through a strict key/type allowlist, applied **on the device
before output**. Never dump whole `.env`, configuration JSON, process environment,
auth headers, command lines containing credentials or raw `pip freeze` URLs.
Never hash individual keys/tokens into a public report. Resolve precedence among
unit environment, `/opt/hal/.env`, persisted OS configuration and provider setup;
record configured and effective values separately. Reading HAL modules by import
may initialize devices, so inspection must not import the runtime blindly.

For example, A's inspected `HAL_STT_KEEPALIVE` parser only accepts the literal
`true`; setting it to `presence` does not enable that policy on main. Match
behavior and record effective values rather than assuming equal setting names
or strings establish equivalent behavior.

A disk manifest alone cannot prove Python code already imported by a running
process. Freeze the bundle, start the corresponding arm, record start/ready
receipts, and verify files remain unchanged before and after its block. Preserve
installed-only files: the old `scripts/deploy-device.sh` uses rsync without
`--delete` and retains `.env`, `.venv` and calibration. Three source hashes or a
HAL version string are insufficient; a mixed installation must be labeled mixed.
Build A in an isolated bundle and retain the existing deployment as a private
rollback copy. Neither its current processes nor its file inventory prove that
main has been deployed for evaluation.

## Shared safety profile

Neither fairness nor a baseline requires unsafe/spooky behavior. Retain a known
stable posture, torque/hold and fresh placement/clearance evidence; no automatic
room scans, repetitive waiting speech or unsolicited motion. The following
configuration controls exist in the pinned V1-main source:

```text
HAL_BACKCHANNEL_FILLERS=
HAL_REALTIME_FILLER_DELAY_S=0
HAL_GAZE_SWEEP=false
HAL_GAZE_PITCH=false
HAL_GAZE_YAW=false
HAL_GAZE_WAKE=false
```

These are a starting overlay, **not proof that every motor/filler path is
inhibited**. Verify startup, emotion, scene, tool and error paths. Do not use
`HAL_MOTION_ENABLED` as a motor interlock: that name also refers to perception.
For stationary acoustic/component blocks, use an independently verified final
motion inhibit that preserves safe holding torque. If A needs a minimal safety
patch, hash/disclose it and call the arm “V1-main + safety patch.” Do not run an
unsafe default merely to recreate a failure. Do not lift these overrides during
background tests. Safe choreographed-motion trials form a separate, supervised
block with the same permitted envelope, pose, load and cancellation checks.

Disabling an unwanted gesture is a declared policy change, not a measured speedup
from Rust. Do not count safety-profile suppression as evidence that an arm has
learned social restraint. Audio-only stationary trials do not qualify body UX.

## Matched experiment

Use the same Lamp board, power supply, firmware, microphones, speaker, camera,
ALSA routing, output device and observer for all arms. Record placement/photo,
desk side, distances/angles, room light, ambient level, cable position, iMac
volume and actual Lamp mixer gain/mute. Do not compare gain percentages from
different gain paths as though they were equal loudness. Keep audibility and
clipping comparable using the same measurement method and fixed geometry.

Reuse the exact cached WAVs and mixing gains; never regenerate between arms.
At audit time, `fixtures/desk-v1.json` contains 24 utterances and 20 scenes, SHA-256
`636a5199cb3da36f0f91576bab8aabaf5af5e97c584ae7b82145f7a1463a675f`.
Record catalog, source-WAV, mix-WAV and evaluator IDs per run. Digital dBFS is not
room dBA or acoustic SNR. Multiple synthetic voices from one iMac position do
not validate sound direction or multiple real people.

Hold Google route/account tier, resolved model, voice, language, speaking style,
search setting and output speed constant where supported. Freeze equal synthetic
user facts, conversation prefix and memory-reset rules; record unavoidable
prompt/architecture differences. Never compare a warmed cloud session against a
cold/reconnected one without labeling the stratum. A different model, DSP
algorithm, context size, endpoint policy or answer length can improve the
product, but confounds a claim about language/runtime efficiency.

Pre-register scenario IDs, expected response/silence, trigger conditions,
timeouts, scoring and a random seed. Use blocked paired crossover on the same
physical setup: each scenario block is run on both arms, with randomized order
balanced between **AB** and **BA**. Keep paired blocks close in wall time to
reduce cloud/load drift; preserve randomized order rather than rerunning a slow
arm until it wins. Reset conversation consistently between independent scenarios;
retain prescribed context within follow-up/yes-no/multi-turn scenarios.

Maintain separate strata for first process start/first accepted turn, first cloud
session, warm conversation, idle resume and forced reconnect. A “cold” test must
state which caches/connections were cold. Do not clear shared OS caches or erase
user memory for benchmarking. Match concurrent camera/sensor workloads, and record
CPU temperature/frequency, network changes and provider/quota failures per block.
If an arm cannot support the same feature, report unsupported; do not silently
route it through another engine. Agentic tasks are outside this release.

## Measurement boundaries and reusable V1 entry points

| Quantity | Matched entry points/evidence | Interpretation |
|---|---|---|
| AEC computation | A: `hal/drivers/voice/aec.py`, `EchoReference.write_samples`, `EchoCanceller.process` (native `aec_audio_processing.AudioProcessor`), 10 ms internal frames. B: `crates/audio/src/lib.rs`, `EchoProcessor.render` + `capture`. | Replay the same immutable mic/reference pairs, delay and NS policy. Report constructor/reset, first blocks and steady-state wall/CPU time, quality, reference underruns and discontinuities. V1 resamples its reference to capture rate; V2 currently takes 24 kHz render and 16 kHz capture. Include equivalent required resampling in the matched pipeline result, and list isolated-DSP timings separately. Different AEC3 revisions/defaults are not a pure language comparison. |
| Input availability/retention | A: `ArecordStream.read` in `_internal/audio_recorder.py`, `AecStream.read` in `aec.py`, `DeviceInputLease`/`DeviceTapInput` in `_internal/device_input.py`. | Sample boundary → retained PCM → consumer delivery; count xruns, stale/backlogged frames and lost interruption prefixes. Blocking read duration includes waiting for samples and is not DSP CPU cost. Mark unavailable stamps/counters explicitly; additional common measurement instrumentation needs its own disclosed patch ID. |
| Endpoint decision | `voice_service.py` `_stream_session_impl`, `_internal/vad_filters.py:turn_should_close`, `_internal/turn_endpoint.py:TurnEndpoint.should_close`; `LiveVoiceMetrics.speech` in `hal/telemetry/live_voice.py`. | Actual final user speech sample → decision/commit. A stores `endpoint_ts = time.monotonic()` when the turn closes, so subsequent log latency can omit endpoint waiting. `server_vad_receive` is a network arrival, not acoustic speech end. Use the continuous room WAV for the common boundary. |
| Playback | A: `TTSService.native_play_begin`, `native_play_frame`, `_WatchedStream.write`, `_note_audio_written`; `hal/telemetry/tts_hooks.py` and `voice_metrics.playback_audio`. | Provider PCM arrival → first accepted device write and → actual audible first substantive word, separately. The hook follows the first successful blocking write; it is not acoustic onset. Inspected V1 uses 40 ms write slices and requests at least 120 ms output latency; read actual device buffering rather than treating those constants as measured delay. |
| Local cancellation/discard | A: `POST /tts/stop` on HAL port 5001, `VoiceService.cancel_automatic_reply`, `TTSService.stop`/`stop_realtime_reply`, `_WatchedStream.write` stop checks. B: `crates/audio/src/pcm.rs:stop`, which calls ALSA drop then prepare and invalidates output on failure. | Play the same cached PCM through the real arm's speaker path; measure accepted cancel → last old write, confirmed device discard, and room silence separately. V1 setting `_stop_event`, returning HTTP 200, or stopping future writes does not prove queued hardware audio was discarded. If no discard event exists, mark it unavailable. Do not invent an equivalent acknowledgment. |
| Natural interruption | New addressed speech in the room → local/provider admission → cancellation → actual silence/new answer. Inspect `gemini_live.py` server `interrupted` handling and `InterruptedOutput`, plus the active V1 consumer. | Server interruption receipt is not local speech onset. An HTTP stop or synthetic event exercises cancellation mechanics only; it does not prove natural topic changes, prefix retention or correct addressee decisions. |
| Resource use | Actual service cgroups and PID/start-time inventories; `/proc/PID/stat`, `status`, `smaps_rollup`, cgroup `memory.current`/`memory.peak` when supported. | Include every required process/native child: HAL, os-server, capture/playback helpers and local vision workers versus the complete V2 worker set. Report aggregate PSS where available, per-process/aggregate RSS with shared-memory double counting disclosed, cgroup memory, CPU-time deltas, threads, restarts and peak/steady values. One core = 100% CPU. |

Pinned main's `hal/drivers/voice/aec.py` SHA-256 is
`771e366737c8d69754aa9d39d34a01d055cc84175488c20d8212143b2de62a4c`.
For the component profiler, copy that file **from the pinned main commit** into a
separate baseline directory and pass its explicit path. Do not load the live
`/opt/hal` source as the baseline. The existing Python environment's installed
`aec-audio-processing==1.0.1`, `numpy==2.2.6` and `scipy==1.17.1` are declared
component-test dependencies, with their actual content hashes; using them does
not establish that the full main runtime is installed or running.

The shared AEC fixture test feeds controlled 10 ms blocks into both APIs,
including required PCM conversion and resampling. Production V1 speaker writes
can be 40 ms and capture batching/cadence must be recorded separately. This is a
component API comparison, not production pipeline timing. Different native DSP
versions, defaults, delay/reference and bypass settings can affect results.

`AecStream.read`
bypasses processing after an idle tail; a bypassed frame is not a fast active-AEC
sample. `HAL_AEC_DUMP_DIR` writes `aec_mic.wav`, `aec_ref.wav`, `aec_out.wav` only
through active processing, so these are not continuous room recordings. Use a
fresh directory per diagnostic run and the same dump policy across arms. Disk
writes from diagnostics must not quietly change the primary latency cohort.

A cancellation benchmark using fake streams proves policy/ordering, not speaker
discard or silence. An ALSA synthetic-PCM test proves the physical playback path
under its stated conditions, not conversational interruption. A full-duplex live
recording is required for the latter. Likewise, V2 IPC-probe microseconds cannot
be compared with V1 conversational seconds as an end-to-end speedup.

Sample resource counters at a fixed, declared cadence (for example 1 Hz CPU/RSS,
5 s PSS), and include observer overhead consistently. Record cold startup peaks,
warm idle, identical active conversations and the concurrent vision/sensor soak
separately. Sampling may miss transient memory peaks; use supported peak counters
as additional evidence. No complete matched RSS baseline was established by this
audit.

## Existing evaluators: reuse with explicit limits

- Historical `scripts/bench/voice_acoustic.py` is external observer tooling,
  not part of pinned A. SHA-256 `48294d89a4c144df95e6c1643ec6dc96798ce79ce8e52241769ef27e07586663`.
  `--wav` reuses an asset; `--output-device` explicitly selects CoreAudio speakers;
  `--capture-device` defaults to `plug:device_micro1`, an independent observer
  rather than HAL's active input. It captures continuous mono 16 kHz PCM and
  bounded HAL log windows with rotation checks. Status is `captured_not_scored`;
  scheduling timestamps never prove word latency. Its manifest hashes only
  VERSION_HAL, voice_service.py and realtime_turn.py: extend provenance externally
  before treating a run as an identified baseline. Its HAL readiness checks may
  not apply to V2 unchanged. Never have two recorders contend for the active mic.
- Historical `scripts/bench/voice_turns.py` parses `[voice-metrics]`, `[turn-timing]`,
  routes and admission logs. It summarizes only available integer fields and can
  omit unanswered turns from a percentile. A does not emit the historical
  parser's `[turn-timing]` fields. Keep a fixture-attempt ledger outside this parser;
  missing measurements are not zero latency or successful answers.
- Historical `scripts/bench/voice_replay.py` replays 64 ms RMS/WebRTC/Silero gates
  and a simplified silence clock. It does not run STT, cloud generation, hardware
  playback or the full current semantic endpoint. Its “earliest” result assumes
  a final transcript already existed: counterfactual diagnostic, not live proof.
- `scripts/bench/latency.py --file ...` is present in pinned A. It reports flow stage
  enter/exit durations and first-to-last trace span, not acoustic first-word
  latency. Use offline files; do not put credentials in its `--password` or
  `--token` command-line options.
- Private historical pilots under `/private/tmp/lamp-4ace-voice-validation/`
  include `voice_multiturn.py`, `voice_acoustic_followup.py`, and
  `natural-interruption-probe-v2/voice_natural_interruption.py`. The last pins its
  dependencies and labels attempts unscored; it has old pose/readiness assumptions.
  They are provenance/examples, not portable or approved-for-blind-reuse runners.
  Their acoustic activity gates do not identify speakers or phonetic boundaries.

## Score every attempt and qualify the whole experience

Create an append-only attempt row **before** each stimulus: arm/content ID,
fixture/seed, block/order/stratum, run/interaction ID, preconditions, expected
behavior, timestamps with clock domain, recorder continuity and outcome. Keep
no-answer, wrong answer, provider/quota error, false rejection, muted playback,
truncated speech, stale replay, echo loop, false interruption, restart and timeout
rows. A failed precondition is a withheld attempt with a reason, not a success.
Negative scenarios expecting silence have their own success denominator.

Pre-register a 15-second conversational answer deadline and preserve observed
late responses; no response by that boundary is right-censored/failed, not a
15-second successful answer. Publish attempts, admitted inputs, correct answers,
correct answers within 2/4 seconds, all error counts, and conditional latency
p50/p95/p99/max with its exact denominator. Include wrong-answer onset timing in
an all-audible-response diagnostic, never as a correct-answer win. Report any
unserved fraction beside quantiles; do not quietly drop unmatched failures from
paired improvements. Paired effect/uncertainty estimates use block-aware analysis;
complete-case speedup ratios are secondary and explicitly labeled. Standardize
one percentile method across all arms rather than mixing the old scripts' methods.

For the primary metric, review the same continuous room recording: final user
speech sound → first substantive audible answer word. Mark annotation uncertainty
and overlap; fillers, acknowledgments, ring cues and model-audio arrival do not
count. Match accepted answer quality/verbosity rather than rewarding a fast but
unhelpful one-word response. Verify held-out failures and a sample of ordinary
turns by listening, not transcript strings alone.

Initial acceptance, per arm and scenario stratum:

- At least 100 conversational attempts and 50 addressed interruptions, with
  separate short acknowledgment, hesitation, multi-speaker/call/other-device,
  noise, camera/sensor, privacy, follow-up and reconnect cohorts. Report repeated
  fixtures as repetitions, not unique independent scenarios.
- V2 conversational acoustic p50 ≤2 s and p95 ≤4 s; admitted interruption →
  acoustic silence p95 ≤200 ms; also report onset → admission, lost prefixes,
  false interruption and the subsequent answer. No dropped or muted admitted
  answers, stale output, or silently unreported accepted-request failures.
- Ring transition p95 ≤50 ms and local controller acceptance p99 ≤5 ms under
  matched load; physical motion timing/safety and fresh visual context remain
  separate gates. Preserve privacy and honest readiness throughout.
- At least 4 hours **per arm** of background conversations/calls/media with
  expected silence. Report false spoken activations/hour and unwanted social
  motions/hour, exposure by scenario and all incidents. Zero in four hours is
  not zero risk: under a Poisson assumption its one-sided 95% upper rate is
  `-ln(0.05)/4 ≈ 0.75/hour`; correlated/repetitive playback weakens that model.
  Brief five-minute TV tests cannot establish a <1/hour rate. Combine extended
  exposure with targeted hard negatives and real people.
- An 8-hour concurrent audio/vision/sensor soak for the complete V2 candidate,
  with matched V1 resource/robustness exposure for upgrade claims, no progressive
  resource growth or stranded output, and all recovery events reported.
- The owner validates comfortable human desk conversation, interruptions and
  synchronized light/head/body behavior. Synthetic iMac tests do not replace it.

Require B to improve the pinned A baseline without hiding degraded accuracy,
restraint, audio quality, resource use or expressiveness. State which dimensions
improved and by how much. Claim “10×” only for the specific matched measured
quantity that supports it, not for the whole robot.

## Before the first paired run

Root owns live runs. Retain the current deployment's private rollback bundle,
then freeze the isolated A and B source/build/configuration bundles and shared
safety overlays. Verify exclusive hardware ownership and the continuous observer;
freeze randomization/evaluator IDs; run a small dry readiness/quality block before
the registered cohorts. Only the pinned main-derived bundle supplies baseline A.

Unresolved here: the built-and-started A runtime's exact bytes, dependencies and
effective settings, effective Google model/voice/quota, physical audibility and
placement, a verified complete motion inhibit for A, cross-arm compatible
runner/annotations, active capture/playback buffering and a complete V2 runtime.
None should be filled in from the current live installation, branch names, old
settings files or earlier mixed-cohort latency tables.
