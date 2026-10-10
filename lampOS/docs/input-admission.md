# Input candidates before interruption

`lamp-live::admission` separates detected speech from permission to replace a
turn. The directed coordinator now passes each `TurnDetector` onset through
this boundary before cancelling a reply, clearing pending output, admitting a
new owner or sending Gemini `activityStart`. This is a retention and ordering
change, **not a qualified echo, speaker or addressee classifier**. Directed runs
still immediately accept with `directed_session_vad_only`, without an added
confirmation wait. Do not interpret this change as a false-interruption fix.

The immediate development priority is reliable voice conversation, including
natural interruptions, complete answers and follow-ups. Sensor acquisition is
deferred behind that priority, while remaining part of the complete release.

## Why this boundary exists

Pinned V1-main `d5efe9d7b73cc529b34cd4abe97624682a82ca94` distinguishes local
speech suspicion from destructive cancellation. In its optional hardware-AEC
path, local activity ducks playback; provider interruption or an addressed
transcript can cancel. Ducking restores volume, not words already played quietly.
With wakeword off, V1's addressed-text check accepts every transcript. Neither
the local energy test nor a transcript proves that the desk owner addressed Lamp.

Rust previously cancelled immediately after six 10 ms VAD blocks at or above
0.80. The certified h4jds57i recording has a second admission 846.505 ms after
the first accepted reply write, with no second question scheduled. Original
trigger blocks 666–671 have scores .906020, .926991, .952209, .942355, .845727,
.865209. Speech-likeness alone cannot distinguish Lamp's playback from a person.
The recorded waveform and hardware/DSP chain still need causal qualification.

Do not copy V1's provider confirmation literally: this Rust provider disables
automatic activity detection and configures `START_OF_ACTIVITY_INTERRUPTS`.
Sending `activityStart` itself requests interruption. Its response is not
independent confirmation. Input transcripts remain session-scoped, uncorrelated
observations and cannot be attached to whichever candidate is newest.

## Retention and authority

The gate owns one candidate at a time. It retains the original prefix (at most
30 blocks / 300 nominal ms) and at most 20 additional blocks / 200 nominal ms.
Storage is preallocated for 50 blocks; no worker, network call or lock is added.
The decision deadline is fixed when the candidate begins: the earlier of
trigger read completion + 200 ms or first retained read completion + 600 ms.
Wall-clock and block-count bounds are independent. New frames, decisions or
fresh authority snapshots cannot extend it. The provider's original one-second
input-age limit remains in force after acceptance; the extra headroom is not a
delivery guarantee.

The immutable candidate ID has the controller boot and a non-reused local
serial. Candidate metadata also retains capture-worker boot, capture/DSP epochs,
microphone privacy generation, original read times and displaced owner. Evidence
names that ID and an actual retained sequence at or after the trigger. A future,
missing or at-least-100-ms-old supporting capture cannot authorize acceptance;
receipt time never makes it fresh. Repeated/late decisions cannot commit twice.

While pending, the old answer and its output authority remain intact. The gate
does not send microphone audio into that answer, create a new listening cue,
mute capture, attenuate the speaker or call the provider. Rejection discards the
candidate audio and drains that detected burst through its `Activity::End`.
One sustained rejected burst therefore cannot repeatedly interrupt/rearm.
This policy can also miss real speech starting inside a rejected burst: measure
that tradeoff before choosing a production rejection classifier.

Acceptance returns all retained blocks once, with unchanged samples, sequences
and timestamps. The coordinator then transfers ownership and sends the retained
prefix before subsequent live input. Pending expiry, input loss, capture/DSP
incarnation changes and owner replacement reject the candidate. Invalid capture
continuity clears it, so an earlier valid decision cannot resurrect pre-gap audio.
Accepted streaming separately checks capture/privacy identity and continuity;
a planned DSP-only epoch advance preserves already accepted speech.

An old answer can finish naturally during a pending candidate. The coordinator
must explicitly report that exact completion before checking the new snapshot.
Only the matching next generation with no successor owner can preserve the
candidate, allowing its acceptance as a new turn. An absent owner alone is not
evidence of normal completion; cancellation or a newer turn invalidates it.

Inactive VAD prefixes are discarded on capture/DSP lineage changes without
resetting sequence checks or an already active utterance. This avoids assigning
a new DSP identity to an older retained prefix.

## Timing, traces and remaining qualification

The processing target for candidate creation plus an immediate local decision
is host p99 <=100 microseconds; this excludes VAD computation, capture cadence,
IPC, cloud processing and audible output. A future delayed classifier has a
200 ms retention budget, not an entitlement to consume that entire delay.
Physical onset-to-yield timing must still be measured from room audio.

`input_candidate` records the immutable candidate and its deadline.
`input_candidate_rejected` records its rejection reason. `input_admitted` retains
the candidate ID, admission basis and actual post-admission publication time.
It is not the physical speech-onset timestamp. Ordinary input/output lifecycle
events and ownership tokens retain their existing meaning.

Host tests cover pending/rejected ownership, exact retained audio, evidence
correlation and expiry, privacy/source changes, independent resource bounds,
natural completion, capture gaps and repeated decisions. These are mechanism
tests. No local classifier is connected to `classifier_decision`, no ducking is
implemented in this slice, and no physical speech quality is established.

The saved corpus includes unrequested playback-triggered admissions and clean
ordinary greetings. No clean physical V2 overlapping-user or direct-human
positive cohort has been located. Rejecting all playback-time speech could pass
those negatives while breaking interruption. Before changing the directed
policy, test quiet and loud true overlap, topic changes, brief acknowledgments,
colleague/TV speech, first-word retention, false interruption and answer recovery.

## Host verification, 2026-10-10

The source-only candidate passed formatting, strict workspace/all-target Clippy,
557 workspace tests (zero failed/ignored), and the macOS release build. Of these,
21 focused admission tests and one detector-boundary test are new. The Rust/
Cargo/toolchain manifest identity is
`8176c4752fe60756ca2d1a669d3c586e7137d87c331aa4f2ccedee4ec827e538`.

`cargo run --release --locked --offline -p lamp-live --example admission_latency`
measured 4,096 synthetic immediate decisions: first/max 8.523 microseconds,
p50 0.346, p95 0.356, and p99 0.364 microseconds. This includes candidate
creation and immediate decision, excluding VAD, prefix preparation, result
buffer destruction, IPC, provider and physical output. It meets the component
host target; it is not a conversational speedup or acoustic interruption result.
The raw result, logs, source manifest and source reviews are retained locally in
`artifacts/input-admission-20261010-8176c475/`.

No Linux/ARM64 or physical run qualifies this new source. The directed policy
still admits the same ordinary VAD onsets, including potential playback residue.
