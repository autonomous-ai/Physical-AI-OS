# Reuse decisions: V1, ROS 2 and existing Rust systems

Review date: 2026-10-10. This is a source-based decision record, not a performance
comparison. V1 means main commit `d5efe9d7b73cc529b34cd4abe97624682a82ca94`.
The current Rust checkpoint is `64529dee4d1228dd9373298a807d506f94421dca`.
External links below were inspected on the review date; rolling documentation
can change. Implementation status is deliberately separate from the design.

The rule is to preserve useful behavior and proven interfaces, reuse maintained
implementations where they fit, and write Rust for the remaining Lamp-owned
responsibilities. A new language alone does not fix timing, echo, ownership or
interaction quality. Do not label an entire system obsolete before examining
its current mechanisms and measuring the relevant alternative.

## What V1 already gets right

Source links in this section are pinned to the baseline commit, so this document
remains useful if lampOS becomes a separate repository. Detailed protocol and
unit evidence lives in the local [hardware contract](hardware-contract.md).

| Preserve | V1 evidence | Rust decision and present status |
|---|---|---|
| Per-field sensor freshness and partial readiness | Environment [service](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/environment/service.py#L109) and [aggregation](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/environment/group.py#L58) exclude stale/faulted samples and retain individual metric timestamps. | Preserve original acquisition times, missing fields and explicit sources. `ObservationTracker` already supplies incarnation, ordering and age checks. The new [environmental snapshot contract](environment-snapshots.md) implements typed values, per-field freshness and fault/restart behavior without I/O. Sensor acquisition and device qualification remain missing. A fresh temperature must not refresh CO2. |
| Privacy closure before slow teardown | [Privacy gates](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/privacy.py#L24) close before service shutdown; camera checks privacy around acquisition. | Keep local authoritative denial and recheck at delivery. Rust capture/camera authority and final output guards implement this principle. Privacy does not wait for a cloud response. |
| Latest camera observation instead of a growing backlog | [Capture](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/camera/video_capture_device.py#L166) replaces the latest response; perception shares frames. | Rust camera has bounded scratch/pending/in-flight buffers and retains the original capture grant. Real camera and conversational vision qualification are still pending. |
| Fast touch acquisition independent of slow actions | [MPR121](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/mpr121.py#L642) separates polling/action execution, uses a capacity-two queue, invalidation generations and event age checks. | Port the recognition and cancellation cases. Route qualified events to the interaction owner; avoid copying direct voice/agent side effects. Ordinary touch/buttons are not yet implemented in Rust. |
| One serialized motor bus, with an atomic overload lockout | [Motor service](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/motors/animation_service.py#L1153) blocks new goals before disabling torque on overload. | Preserve one bus writer and distinguish cancellation from torque release. Rust has protocol/calibration inspection only; motion protection and choreography are not implemented. Limits and units need physical qualification. |

These are meaningful examples of good V1 engineering. V1 runs many hardware
owners as threads inside one HAL process; it is not correct to say that it has
no separation. The change is independent failure boundaries, explicit output
authority and bounded communication, while preserving useful subsystem logic.

## What must change

For the first connected ring policy, retain V1's
[replaceable pending event](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/base.py#L37)
and [stale-restore regression](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/test/test_led_restore_generation.py#L38)
semantics through one pending frame and existing turn/presentation generations.
V1's [speaking wave](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/rgb/effects.py#L284)
is a random simulated equalizer, not actual playback evidence. Its timed effect
thread stop also cannot prove the old writer terminated. The Rust
[ring integration](ring-choreography.md) instead uses fixed prototype phase cues,
matched speaker receipts, one supervised writer and explicit shutdown evidence.
It adds no animation thread, delayed bare `off()` callback, privacy color or
motion. Visual quality and crash recovery still require physical qualification.

| Observed problem | Replacement rule | Verification required |
|---|---|---|
| V1 motor events append to an unbounded list with no expiry or interaction owner ([source](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/motors/animation_service.py#L364)). | Bounded admission, explicit replacement semantics and owner/expiry checks before each physical write. | Flood the queue, cancel/restart owners and verify obsolete gestures cannot execute. |
| V1 camera shutdown clears its thread handle after a timed join without confirming exit ([source](https://github.com/autonomous-ai/Physical-AI-Operating-System/blob/d5efe9d7b73cc529b34cd4abe97624682a82ca94/hal/drivers/camera/video_capture_device.py#L692)). | A stop request is not completed shutdown. Retain ownership until actual exit/reaping; report failure. Rust process shutdown already checks exit status. | Stuck worker, late completion and restart tests; no two device owners. |
| Legacy startup can configure torque and queue motion; normalized position values are not degrees. | Read-only inspection first; explicit per-device units, calibrated bounds and current placement evidence before motion. | Fresh whole-device view, limits, current/load protection and stop behavior on Lamp. A successful serial write is insufficient. |
| V1's ordinary TTS capture suppression avoids some self-interruption but discards user overlap. | Preserve continuous capture and a stable render reference; qualify interruption admission separately. | Matched reply-only and quiet/normal overlapping-speech trials, with lost opening words and acoustic stop measured. |
| Current Rust treats six high-VAD blocks as permission to cancel the old reply. Playback residue has triggered that path. | Separate candidate detection from qualified admission; retain the beginning of speech while deciding. | A gate must reject echo without rejecting genuine quiet overlap. No threshold or classifier is selected merely because it passes one recording. |

The last two rows are documented in the [microphone audit](microphone-v1-v2-audit.md).
They show why the new runtime also needs criticism: process separation and Rust
ownership do not themselves solve conversational perception.

The [pinned AEC source audit](aec-alignment.md) found no sample-rate or float
normalization mismatch. Keep Sonora's existing processing while gathering its
internal alignment separately from the supplied queue hint. V1's delay default,
idle bypass and adaptation-preserving buffer reset are different mechanisms;
copying one constant does not reproduce them or establish better duplex audio.

## Borrow from current ROS 2

**Lifecycle:** ROS 2 distinguishes configuration, inactivity, activation and
terminal states. Adopt that separation in worker supervision: opening a device
does not prove useful input or output readiness. A worker restart must establish
new identity and fresh evidence. The current finite Rust runtime fails visibly
and reaps workers; a general automatic restart supervisor is not implemented.
[Managed nodes](https://design.ros2.org/articles/node_lifecycle.html)

**Actions:** ROS 2 separates an accepted goal, execution, cancellation in progress
and terminal outcomes. Use those distinctions for speech and motion. A request
to stop, device acceptance and observed physical completion are different
events. Keep a bounded result with the original action identity; a delayed
callback cannot complete a newer action. This is a contract to reuse, without
requiring the entire ROS action transport in the first release.
[Actions](https://design.ros2.org/articles/actions.html)

**Communication policy:** ROS 2 makes history depth, reliability, durability,
deadline, lifespan and liveliness explicit. Use per-channel policies and expose
incompatibility or loss. Current Rust Unix IPC already bounds messages and
separates control from audio. Every new channel needs its own overflow and
freshness rules; one generic queue setting is insufficient.
[QoS policies](https://github.com/ros2/ros2_documentation/blob/rolling/source/ROS-Framework/interfaces/topics/About-Quality-of-Service-Settings.rst)

**Scheduling:** ROS 2 supports callback groups and assigning them to different
executors. Its current event executor reduces polling work, but the documented
event queue can grow without bound under overload. Borrow explicit scheduling
and ready-event handling, while keeping bounded queues and independent audio
workers. Do not assume that using an executor, async Rust or more threads proves
a deadline. Measure scheduling delays under concurrent load.
[Executors](https://github.com/ros2/ros2_documentation/blob/rolling/source/ROS-Framework/client-libraries/About-Executors/About-Executors.rst)

**Real-time discipline:** avoid unbounded blocking and cold allocation on timing
paths, report overruns and distinguish mean throughput from worst-tail timing.
The OS, drivers and device buffers remain part of the path. Rust's memory
safety is useful but does not supply a real-time scheduling guarantee.
[Real-time programming](https://github.com/ros2/ros2_documentation/blob/rolling/source/Capabilities/Motion-planning/Real-Time-Programming.rst)

**Deployment:** a ROS node is a logical component, not necessarily an OS process.
Lamp's separate physical workers are a deliberate isolation choice. The five
servos still need one owner because they share a bus. Do not create competing
drivers simply to give each joint a process.
[Launch design](https://design.ros2.org/articles/roslaunch.html)

### Channel contracts for Lamp

These are design requirements. Existing audio/control implementations are
described in [live runtime](live-runtime.md); the remaining channels must be
implemented and qualified before claiming the whole graph runs.

| Channel | Required policy |
|---|---|
| Microphone and accepted render PCM | Ordered, bounded, timestamped blocks; sequence gaps and reference loss are explicit faults. Do not arbitrarily drop old blocks as if PCM were independent camera frames. |
| Camera | Latest useful frame, bounded in-flight ownership, acquisition age and original privacy grant. Drop/replace obsolete pending frames deliberately. |
| Environmental values | Latest per field, explicit units/source, unavailable and fault states, per-field age. A new value for one field does not refresh another. |
| Touch/button events | Bounded ordered recognized events, acquisition times, no invented multi-taps after overflow, generation/expiry checks before an action. |
| Speech/ring/motion commands | Original turn/action identity through the final writer; reject expired or superseded output. Never replay old movement commands after restart. |
| Privacy/revocation | Independent of bulk-data congestion; denied by default after lost authority. A middleware delivery receipt is not proof of physical silence. |
| Diagnostics | Bounded and nonblocking; distinguish dropped diagnostics from a clean measurement. Preserve source/configuration/fixture identities for replay. |

## Reuse existing Rust implementations before inventing infrastructure

Keep the already selected Rust libraries and reviewed DSP interfaces where they
fit. Do not rewrite working resampling, cryptography, audio backends or model
inference just to call the project a rewrite. Lamp owns the runtime policy,
hardware contracts and end-to-end experience; dependencies remain explicit.

| Candidate | Useful existing work | Decision now |
|---|---|---|
| [iceoryx2](https://github.com/eclipse-iceoryx/iceoryx2) | Rust shared-memory IPC, bounded-resource communication mechanisms and large-payload transfer without serialization copies. | Evaluate before implementing a custom cross-process camera frame pool. No replacement of working control IPC is selected. ARM64 support and crash recovery need qualification; upstream desktop results are not Lamp results. |
| [dora](https://github.com/dora-rs/dora) | Rust dataflow runtime, process lifecycle, declared graphs, record/replay and observability. | Evaluate reuse for launch/supervision/tooling before building a general robot framework. Verify cancellation, bounded overload, device exclusivity and our local timing targets. No runtime dependency has been added. |
| [Zenoh / ROS 2 integration](https://github.com/ros2/rmw_zenoh/blob/rolling/docs/design.md) | Current ROS 2 can use Zenoh; its design separates discovery from direct same-host peer communication. | Consider when remote components or ROS interoperation are actually required. Network discovery does not belong in the immediate local interruption path. No network middleware is required for the desk-chat slice. |

These are inspected candidates, not benchmark winners. Any dependency decision
must include license, maintenance, platform, failure behavior and memory review.
A replacement must pass the same workload and failure cases before it displaces
current code. Retain failed trials as well as successful ones.

## Next implementation and proof

1. Finish physical validation of the already-built terminal audio shutdown fix.
   Then use paired cached-reply tests to distinguish false interruption from
   real overlap. Preserve the quiet-speaker case; do not solve echo by deafening
   Lamp during its own answer.
2. The small environmental snapshot contract is now implemented and host-tested
   with explicit units, missing values, per-field freshness and restart/fault
   regressions. Keep it I/O-free until configured sensor workers are
   implemented; qualify the installed hardware before integrating it.
   Do not auto-probe the bus.
3. Reuse V1 touch recognition and safety regression cases as those workers are
   added. Require real device exit before restart and preserve acquisition time.
4. The optional ring policy now shares actual playback events and interaction
   ownership. Qualify its physical timing/visual quality and complete crash
   recovery, then add safe motion and fresh vision. Stillness is valid. No
   automatic startup motion.
5. Compare reusable infrastructure candidates only on the workload they would
   replace: control latency under camera/audio load, bounded memory, dropped
   observations, cancellation, worker death and restart. Keep cloud response
   time separate from local runtime costs.

The general-purpose opportunity is reusable ownership, lifecycle, device
contracts and diagnostics, with robot-specific profiles and drivers. Lamp is
the first qualification target. It does not yet establish support for arbitrary
robots or a tenfold speedup. There is still no accepted matched V1-main/V2
end-to-end p50/p95 comparison.

## Candidate admission before interruption

The [input admission contract](input-admission.md) borrows V1's distinction
between local speech suspicion and destructive cancellation. It does not copy
V1's unqualified energy thresholds, three-second hardware warmup assumption,
or transcript/provider confirmation. In the current explicit-activity Gemini
configuration, Start itself requests interruption; its response is circular
evidence. The bounded local gate preserves original audio and candidate identity
while leaving pending/rejected output ownership intact. Directed qualification
still chooses VAD-only acceptance and has no new classifier accuracy result.
