# lampOS V2: live interaction release

Owner scope, 2026-10-10. **Rust ownership, IPC, audio, ring, Gemini transport and
fixture components are implemented. Early physical pilots give provisional reply
gaps of 4.4 seconds for pinned V1-main and 2.0 seconds for V2, each n=1 with
unmatched Gemini settings. Later V2 pilots exposed false interruption and
playback-reference starvation during speech gaps. There is no validated speedup,
p50/p95, or repeated-turn reliability. V2 has not replaced HAL. Full camera,
sensor, motor and social interaction integration remains.**

See [benchmark progress](benchmark-progress.md) for the retained trials and current blockers.
Source home: [top-level lampOS/](../README.md).

## Product boundary

**Chat with a physical robot.** Lamp sits in a fixed position on the left or
right of a desk, beside one person using a computer. Speech, listening, head,
body and ring form one coherent interaction. Stillness and silence are valid.
A colleague, a call and computer audio are part of this desk environment.

The first version includes:

| Experience | Required behavior |
|---|---|
| Conversation | Gemini Live handles short and extended chat, follow-ups, short answers to Lamp's questions, corrections, hesitant speech and topic changes. Keep the current conversation and explicit interaction settings available. |
| Interruption | The person can start a new addressed question during an answer. Retain the opening words, yield speech promptly and follow the new meaning. Measure and distinguish a listener's brief acknowledgment from an interruption. |
| Expression | One choreographer coordinates the voice, head, body and ring using actual input/playback events. Use small purposeful gestures with cancellation and repetition limits; no gesture is required on every turn. |
| Vision | Use fresh camera evidence for desk presence, permitted gaze adjustment and objects the person shows. Admit relevant frames to the live conversation. Report uncertainty and camera unavailability honestly. |
| Sensors and controls | Acquire the installed sensors efficiently, preserve per-field freshness and respect privacy/touch/button controls. Sensor data supports conversation and local behavior; it does not trigger repetitive unsolicited commentary. |
| Desk setup | Verify left/right placement, a safe motion envelope, useful sight, hearing and audible speech. A saved pose is not fresh placement or presence evidence. |
| Social restraint | Remain quiet while the person works, talks to someone else or joins a call. Face presence, recognized voice or a passing mention of Lamp is insufficient on its own. Looking at the monitor must not disqualify clearly addressed speech. |
| Failure | An admitted request gets a useful answer, relevant clarification, honest failure or recorded user interruption. Lost connections must not leave false listening/working cues or silently lose accepted requests. |

Agentic tasks, Harness/Codex sessions, browser/computer automation, background
research and new Hindsight or other long-term memory infrastructure are deferred.
Local camera/sensor reads and bounded light, volume or posture controls are part
of the interaction itself. The model must not claim to have run an excluded task.
No autonomous room search, stock spoken waiting fillers or random idle movements.

## Rust and reuse

All Lamp-owned runtime code lives in top-level `lampOS/` and will be Rust:
audio capture/playback coordination, provider transport, conversation control,
choreography, camera/sensor acquisition, motor/LED control and diagnostics.
The release has no Python runtime or Python HAL dependency. A temporary Python
bridge is not the migration plan for this release.

Port tested hardware protocols, calibration interpretation and safety behavior
as Rust implementations, using the existing code and device fixtures as
references. Existing constants and tests are evidence to check, not proof that a
port is correct; verify units, limits, timing and failure behavior on the device.
Retain useful model assets and established DSP/inference libraries through Rust
interfaces where appropriate. Review native dependencies, isolate unsafe/FFI,
and account for their memory and threading. An algorithm whose only available
integration requires Python must be ported, replaced or explicitly revisited
before release; do not silently add a Python sidecar.

Treat `lampOS/` as the repository root. Copying this directory into a new
repository must suffice to build, test and run V2 after its documented device
configuration and external dependencies are provided. Startup, configuration,
process supervision and all interaction duties required by V2 live here in Rust;
no sibling source, Python HAL service or Go os-server service may be required.
Credentials and per-device calibration are provisioned data, never committed
secrets or hidden path dependencies. Verify a source-only build outside the
parent repository.

Fleet management and the existing web UI are outside the first release scope.
Any setup, UI or update component needed by lampOS must be owned within this
repository boundary; it cannot become a sibling-service dependency. V2 must
install and operate while legacy HAL and os-server are stopped. A hidden legacy
fallback or Python/Go adapter is not an acceptable release. Retiring the old
implementation is conditional on complete device qualification.
Keep the existing runtime available for rollback, with exclusive device ownership
during each run. Rust provides useful ownership tools; bounded resources and
measured scheduling remain explicit design obligations.

## Runtime responsibilities

```text
Microphone / camera / sensors
          |
  fresh, bounded observations
          |
Rust interaction owner <----> Gemini Live
          |
  one local choreographer
          |
Voice output / head / body / ring
```

Continuous capture and local speaker cancellation have a dedicated execution
path. Listening and speaking can overlap; they must not be mutually exclusive
states that discard the start of an interruption. Heavy vision inference and
cloud I/O cannot run inside audio or motor timing loops. Run microphone capture, speaker playback, camera acquisition, vision inference,
ring output, physical controls and each enabled environmental sensor in their
own supervised processes, with explicit bounded communication. The five servos
share one motor process because they share a serialized bus. Keep one writer
for that bus and coordinate head/body motion there. The directed runtime now implements separate capture, speaker, physical privacy
and provider processes. Camera, sensor, ring and motor processes are not yet
integrated into that runtime. The earlier IPC measurement used separate test
processes; it is not a measurement of these hardware workers.

Code enforces privacy, output ownership, expiry, cancellation, readiness and
motion bounds. Gemini supplies meaning, language and optional expressive intent.
Whether someone is addressing Lamp remains a fallible perception decision:
evaluate false acceptance and false rejection, not just prompt compliance.
Each observation carries its age and uncertainty. Each output retains its turn
and playback ownership through the final write. A newer turn invalidates old
speech and conversational gestures without requiring a model-generated stop.

## Qualification

These are targets and planned tests, not measured V2 improvements. Compare only
pinned V1 main and Rust V2 using the [comparison protocol](comparison-protocol.md).
Live conversation on the physical Lamp is the primary evidence: cached speech
from the iMac speaker, Lamp's real microphones and Gemini, and the answer from
Lamp's speaker. A continuous independent room recording establishes the audible
boundaries. Modified V1 is excluded from the comparison. Component measurements
diagnose the result; they cannot establish a conversation speedup.

| Measurement boundary | Initial gate |
|---|---|
| User speech end to first substantive audible answer word | p50 ≤2 s; p95 ≤4 s. Waiting phrases and nonverbal cues do not count. |
| Admitted interruption to acoustic speaker silence | p95 ≤200 ms. Also report speech onset to admission, lost opening words and false interruptions. |
| Valid input/playback state transition to visible ring change | p95 ≤50 ms. Readiness requires actual retained input. |
| Local control event to controller acceptance under concurrent load | p99 ≤5 ms. Measure physical effects separately. |
| Motor, camera and sensors | Verify joint safety, control-cycle jitter/overruns, observation age, acquisition cadence and faults on the actual installed hardware. |

Use comparable before/after runs on the same device, voice, model settings and
fixtures. Start with at least 100 conversational turns and 50 addressed
interruptions, plus separate acknowledgment and false-interruption cases.
Report all failures and separate first use, warm turns and reconnection.
Include extended listening, partial thoughts, mid-sentence corrections,
camera-off/darkness/occlusion, an object shown at the desk, left/right placement,
privacy transitions, slow providers and component restart.

Run 4 hours of mixed background speech/calls/media with no unsolicited speech or
social gestures in that test, and an 8-hour concurrent audio/vision/sensor soak.
Synthetic voices support repeatable timing tests; real people are needed to
validate multiple speakers and comfortable body/head/light behavior. Zero
failures in these samples is not a claim of universal reliability.
The owner must find the combined experience natural in normal desk use.

Implement in complete, measured slices: first continuous audio with local
interruption and ring ownership; then safe head/body choreography and fresh
visual/sensor context; then integrated behavioral and sustained-load
qualification. An audio-only demo does not complete this release.

## Implemented test tooling

The `lamp-acoustic` crate contains a versioned catalog of 20 desk scenarios and
24 reusable utterances. Ownership, transport and audio processing now have Rust
implementations as described below. Gemini transport and a directed voice process
integration are described in [live runtime](live-runtime.md). This slice has no
addressee classifier, camera, motors or ring integration; it does not qualify the
complete desk experience. Native checks run in an isolated build directory on
Lamp; installed HAL services have not been replaced.

The catalog covers two colleagues, speech to another device, calls, media,
overlapping voices, synthetic ventilation/keyboard noise, hesitant speech,
short answers, topic-change interruptions, listener acknowledgments, visual
questions and fast follow-ups. Scenarios specify preconditions, expected
behavior and a start/speech-start/speech-end trigger. The current renderer
creates assets; a future runner must observe and execute those triggers.

From `lampOS/`:

```sh
cargo run -p lamp-acoustic -- list
cargo run -p lamp-acoustic -- render .cache/acoustic .cache/render-first.json
cargo run -p lamp-acoustic -- render .cache/acoustic .cache/render-repeat.json
cargo run -p lamp-acoustic -- verify .cache/acoustic
```

The ignored `.cache/acoustic/` directory keeps rendered PCM and asset manifests
on this worktree's disk for reuse. Keep it when cleaning build output; it is not
under `target/`. Binary audio is not committed. Speech is keyed by text, voice,
rate, explicit voice revision, macOS build, speech-engine executable hash and
render version. Mixes are keyed by their audio inputs and composition. Changing
an expected answer or an event trigger does not regenerate identical audio.
The renderer uses the installed Mac voices; rendering calls no language model
or cloud TTS service.

Each cache read verifies the WAV hash, format, frame count and signal metadata.
A timed interprocess lock prevents duplicate synthesis of the same asset.
Complete objects publish atomically; failures do not publish partial objects.
Decoded speech is bounded to 300 seconds per catalog; each scene is at most
60 seconds. Overlapping sounds are attenuated together to avoid clipping,
and the attenuation is recorded. Noise has deterministic seeds and a digital
dBFS level; neither dBA in the room nor acoustic SNR is implied.

Reports preserve their existing contents, record reuse/generation counts and
explicitly say `rendered_not_run_or_scored`. Assets alone are not evidence that
Lamp passed a scenario. Acoustic recordings, synchronized observation and
scoring will be separate run artifacts.


## Implemented runtime components

`lamp-interaction` is a fixed-size, I/O-free controller with immutable turn and
output-plan tokens. An asynchronous callback retains its original plan; it cannot
renew a cancelled turn or an obsolete listening/speaking cue. The final writer
checks a short output permit against fresh controller authority immediately
before each bounded write. Capture retention and input admission have independent
leases, so playback does not turn off hearing. Input leases have a one-second
ceiling; output and controller authority have 250 ms ceilings. Those are failure
bounds, not measured interruption latency. Camera and microphone privacy have separate lineages. Capture uses its own
privacy generation so a coalesced mute/reopen discards retained audio without
reopening the microphone on every conversation turn.
Observations distinguish acquisition time, source incarnation, uncertainty and
freshness. This controller does not infer who someone addresses.

`lamp-ipc` provides connected private Unix datagrams, an 8,192-byte cap, truncation
rejection, finite kernel queues and nonblocking backpressure. Control and bulk
PCM use separate endpoints. Timestamps use the same host monotonic clock across
processes, with independent boot IDs to exclude data from an earlier process.
The `lamp-ipc-probe` executable measures real parent/child control roundtrips
while the bulk endpoint stays full; it has finite deadlines and child cleanup.
It does not measure speech recognition, model time, audible output or visual cues.

`lamp-audio` wraps Sonora 0.2.0, a pure Rust WebRTC audio-processing port, with
16 kHz capture and 24 kHz render reference in 10 ms blocks. AEC3 and moderate
noise suppression are enabled; automatic gain changes are disabled. Reference
must come from audio actually accepted by the speaker, never generated but
unplayed provider audio. Delay values outside 0–500 ms are rejected. Lost frames,
privacy transitions and device faults require explicit discontinuity handling.
The synthetic benchmark measures cold initialization, first block, warm
processing percentiles, missed 10 ms budgets and an echo fixture; live room
alignment, clock drift, double-talk quality and audibility remain unqualified.

The Linux speaker boundary uses safe ALSA bindings and the board's configured
speaker alias, preserving its mixer path. It requests mono 24 kHz PCM, at most a
10 ms period and 40 ms buffer, and rejects a negotiated format outside those
bounds. Each nonblocking write is at most 240 samples and checks ownership at the
write boundary. Cancellation drops queued audio rather than draining old speech.
Transport failure latches authority closed. Native tests use ALSA's `null` plugin
and produce no sound; the physical speaker and its acoustic stop latency still
require measurement. The complete runtime must enforce one speaker owner and
service urgent control and expiry independently of new audio arrival.

Queued speaker audio retains the permit for every accepted chunk, including its
camera lineage and deadline. A newer chunk cannot hide a revoked or expired
older chunk still in the device buffer. ALSA queue observations retire metadata
only for frames reported consumed; this is not an acoustic silence measurement.
A rejected old control or PCM packet cannot stop a newer valid reply. A failed
hardware discard preserves pending metadata, closes authority and returns an
error instead of reporting that playback stopped.

`lamp-ring` encodes the installed 32-pixel GRB ring into one 1,028-byte SPI frame.
It validates the configured channel ceiling (maximum 120), checks fresh output
ownership at the final write and preserves newer valid cues when stale packets
arrive. Revocation, expiry and transport loss blank the ring; write failures
latch it closed. The Linux transport checks SPI mode, bit order and 6.4 MHz
settings and rejects short writes. Its 10 ms write budget is measured when the
syscall returns, not a hard timeout. A supervised worker must service control
and call its expiry tick at least every 10 ms. Cooperative file locking does
not exclude legacy HAL or boot/shutdown services; cutover must ensure one writer.
Tests cover codec, cancellation and fault paths. Physical light timing and
brightness remain unmeasured.
