# Ordinary interruption and the continuous audio clock

The continuous Linux speaker keeps its PCM clock and echo-canceller reference
running when a **new admitted input** supersedes a reply. It cancels the old
reply immediately in logical ownership, rejects further old-owner writes, and
allows only the immutable PCM already accepted by ALSA to retire. This contract
does not decide whether the input was a person, echo, or background speech.

`Speaker::open` retains strict cancellation and discard behavior.
`Speaker::open_clock` opts into bounded accepted-tail retirement after its clock
has started. No reference thresholds, wire format, synthesized silence, or
capture grace period changes are involved.

## Fixed tail and authority

Each clock-mode speech acceptance retains its original `OutputPermit` and the
microphone privacy token checked at that write. `BoundaryGuard::check` still
authorizes every new speech write, including a partial chunk's next suffix.
The separate `check_accepted_speech_retirement` check grants no write authority.
It checks permit shape, boot, kind, issue time, revision, expiry, fresh state,
current input leases, accepted microphone generation, and the original camera
dependency before considering a newer owner. A `StaleOwner` result from normal
write validation is insufficient: it can otherwise hide camera or input loss.

Retirement begins only while the installed snapshot contains a newer active
admitted owner on the same boot and microphone privacy generation. The queue
must contain only one old speech owner; mixed-owner or untracked-privacy entries
keep the strict discard path. OwnerNone, an already-ended successor at initial
recognition, unknown/closed privacy, coalesced close→reopen, missing/expired
authority, input loss, camera revocation, malformed control, clock regression,
hardware errors, and explicit Stop still stop/flush. Successor input ending
after a tail was latched does not by itself invalidate that tail; all hard
fences continue to apply.

The tail records its original owner, first superseding owner, playback epoch,
start/end cursors, queued-frame count, speech-frame count, and start time. Its
range includes any already-accepted zeros. The entire queue remains bounded to
960 mono frames at 24 kHz: **40 ms nominal PCM**, not a measured acoustic stop
time. It has a nonrenewable 100 ms wall watchdog (40 ms queue plus 60 ms scheduling
margin) and remains bounded by the earliest original permit expiry. Every
remaining permit is rechecked. Fresh authority may maintain its normal lease;
heartbeats, additional admitted turns, and appended zeros cannot enlarge the
tail or extend its watchdog. An expired tail is discarded, never certified as
retired. A returning filesystem/kernel/device operation is not preempted by
these local timestamp checks.

Fresh nonnegative ALSA delay retires accepted frames in FIFO order. A larger
delay proves nothing; writable buffer space is not retirement evidence. New
speech waits until the fixed old tail retires and its receipt is collected.
Fresh, separately authorized zero-only writes can maintain the same PCM epoch
and render sequence meanwhile. The original samples and reference always keep
their old owner. There is no drop/prepare/reprime, AEC reset, or VAD reset for
ordinary supersession. A hard flush still resets the existing lifecycle.

The speaker clears both unfinished reply debt and an unretired final speech
cursor on logical supersession. It never turns a cancelled prefix into a
`SpeechRetired` answer completion. A stale independently queued data packet is
rejected without stopping newer output; its rejection does not consume the
tick's opportunity to write authorized zeros. At most one pending speech chunk
is retained. These checks take place after the writer observes authority; they
do not claim atomicity between the controller's decision and an already-running
bounded ALSA write.

## Receipts and outcome

`WorkerEvent::CancelledTailRetired` records `owner`, `superseded_by`,
`first_sample`, `end_sample`, `queued_frames`, `speech_frames`, `started_at_us`,
and `observed_at_us`. It is emitted once only after checked delay-based
retirement. It is neither a new accepted write nor a physical discard nor a
completed reply. Maintenance reports a later hard abort through the existing
`PlaybackDiscarded` evidence and failure path. Explicit Stop/privacy retain their
existing reset/stop diagnostics; neither emits a tail-retired receipt.

The coordinator checks bounded dimensions, cursor/owner lineage, the retirement
deadline, matching prior `user_interrupted` cancellation and subsequent admitted
owner, and duplicate receipts. It logs `cancelled_tail_retired` without mutating
either the cancelled outcome or the successor reply. Input-admission traces
carry full owner identity and the exact published authority issue time. The
coordinator requires cancellation time <= authority issue time <= tail start;
missing, early or future admission authority cannot justify a retirement.
Original-owner render diagnostics remain unchanged;
there is no new reference-control or PCM diagnostic schema.

## Regression and qualification boundary

Pilot `7v7pvbyl` admitted a new input 835.170 ms after its first reply write.
Its last accepted write was at monotonic 183637586619 µs; queue observation
183637586628 µs retained 456 frames. Capture failed at 183637607118 µs:
20.490 ms nominal consumption is 491 frames, exhausting that last observed
queue. The later flush/new-prime arrived after the failure. The trace does not
measure time spent inside each ALSA syscall and does not establish why the new
input was falsely admitted.

Offline tests preserve those queue/timing values, demonstrate that the unchanged
reference check fails without new accepted PCM, and keep it running using actual
modeled zero acceptances in the same epoch. Tests cover the 960-frame bound,
partial retirement, repeated admissions, fixed deadlines, camera dependencies
in middle entries, original expiry, privacy close/reopen, input/state loss,
faults, strict callers, final-cursor cancellation, and stale packets. Linux
software-null tests exercise the PCM boundary without opening physical output;
they must run on a Linux target because host macOS gates exclude that module.

These gates do not prove real audio continuity, echo rejection, double-talk
quality, or acoustic interruption latency. A later authorized physical fixture
run must show no clock/DSP reset on ordinary supersession, continuous actual
reference, truthful cancelled-tail receipts, and independently measured audible
stop latency (target p95 ≤200 ms). Privacy and explicit Stop require separate
hard-stop verification. Preserving AEC adaptation is not a fix or acceptance
claim for the initial false admission.

## Native qualification

Frozen source `e4ff39d998caa1eb6c503e408f91b7184ea10c3c7b92e06b861d82ea08657071`
passed 514 native ARM64 workspace tests (zero failures/ignored), strict all-target
Clippy, formatting and release build. Both new Linux null-sink cancellation and
hard-stop tests passed. Exact commands, logs, hashes and the retained host
parallel-test limitation are in `artifacts/native-live-tail-camera-e4ff39d9/`.
The fixture test on the real speaker/microphones is recorded separately; these
build gates do not establish acoustic continuity or interruption latency.

## First physical cancellation regression

The 30-second fixed-reply run `h4jds57i` used that qualified native binary, the
same cached iMac greeting and cached Lamp reply, and NS on. No second stimulus
was scheduled; Gemini was disconnected. False admission still occurred
846.505 ms after the first ALSA reply write. The test deliberately supplied only
one reply, so the successor's `no_audio_answer` is the fixture policy.

After cancellation, all 3,001 capture blocks and 3,003 render blocks remained
contiguous in playback/microphone/DSP epoch 1. No old-owner speech was accepted
after cancellation; chunk 86 was rejected. Actual accepted zeros started at the
old speech end cursor 4.007 ms after the cancellation trace. The immutable
288-frame tail retired 14.174 ms after cancellation (12.336 ms after latch).
These are ALSA/software boundaries, not a measured acoustic stop time. Both
diagnostic streams certified complete, with no cancellation reset or reference
fault. The only later render reset was the planned final privacy close.

This qualifies one execution of the continuity repair. It does not qualify
false-interruption rejection, intentional human interruption, double-talk, or
acoustic p95. Fresh mixer state was not read during this trial; the harness
requested no gain change. Installed services were restored and motor inhibition
retained. Evidence and independent hash/cursor review are in
`artifacts/fixed-reply-clock-continuity-20261010/`; raw recordings remain private.
