# Physical benchmark progress

Checkpoint: 2026-10-10. **No accepted paired V1-main/V2 p50, p95, or speedup yet.**
The primary boundary is final iMac speech sound to Lamp's first substantive
answer sound in continuous room audio. Process exit zero is not an answer pass.
These pilots use loudspeaker replay from the iMac. A new team report of clearer
direct-human capture than speaker replay makes a separate human-speech cohort
necessary before judging the natural interaction experience.
The fixed V1 source is `d5efe9d7b73cc529b34cd4abe97624682a82ca94`; the modified
installed V1 is preserved only for rollback.

| Recorded pilot | Observation | Scoring status |
|---|---|---|
| V1-main `v1-main-pilot-e363pbra` | One complete reply; provisional 4.39–4.45 s acoustic gap. | n=1; not a percentile or paired aggregate. |
| V2 `directed-pilot-q0xpcx_j` | One complete recorded reply; provisional 1.945–2.015 s gap. | n=1; an unexplained same-turn DSP reset remains; not acceptance. |
| V2 `directed-pilot-_iiznjk4` | A second input was admitted about 519 ms into answering the only scheduled question; reply cut off and language changed. | Failed; no accepted answer latency. |
| V2 `directed-pilot-zejfwxq7` | One question led to four admitted inputs and three cancelled answers. Typed receipts proved stale-owner cancellation, not permit expiry. | Failed; no accepted answer latency. |
| V2 `directed-pilot-72kss2m8` | One admitted question; 90 ms of speech PCM accepted, then the continuous playback reference stopped. Fault 110.027 ms after first write. | Failed; room recorder captured 6.8403125 s until runtime exit. |
| V2 `directed-pilot-fvackatg` | Same question/settings with the gap correction; three inputs admitted, new inputs admitted 485.710/646.359 ms after the two first accepted reply writes. ReferenceLate during the second playback reset. | Failed; 10.063375 s observed room recording; no accepted answer latency. |
| V2 `directed-pilot-5koru8e0` | One input, one full recorded answer, no extra admission or worker failure. Three accepted-zero playback gaps: 90/10/10 ms. | Completed with certified diagnostics; acoustic latency and smoothness unscored. |
| V2 `directed-pilot-l3yc4zsy` (NS on) | One scheduled input, original reply retired, no playback gaps. | Certified integration recording; acoustic latency unscored. |
| V2 `directed-pilot-j5_xqe32` (NS off) | Three admissions from one scheduled question; first two replies cancelled; one 140 ms gap. | Conversation failed despite runtime exit zero and certified recordings. |
| V2 `directed-pilot-rubc3c4q` (NS on) | Five admissions from one scheduled question; first four replies cancelled; five gaps totaling 510 ms. | Conversation failed despite runtime exit zero and certified recordings. |
| V2 `directed-pilot-g2kp1mcp` (scheduling fix, NS on) | Two admissions from one question; original reply cancelled. One 130 ms provider supply/holdback gap; no observed backlog-ready gap. | Conversation failed; certified diagnostics and clean runtime exit. |

This table tracks the comparable greeting pilots, not every earlier component,
startup, speaker-only or partial integration attempt. Earlier failed attempts
remain documented in [live runtime](live-runtime.md) and their private evidence.
It is not a success-rate denominator. No failed attempt is discarded to make a
latency percentile look better.

## Resumed device discovery, 2026-10-10

The terminal-shutdown candidate still needs its first physical rerun. The old
`172.168.20.159` address timed out. At the owner's request, a bounded scan covered
`172.168.20.0/23` (510 usable addresses; TCP 22/80/5001/8080), checked reachable
SSH host keys against the saved 4ace key, read public setup identities and tried
local discovery. No matching 4ace identity was found. The other identified Lamps
were `lamp-52e6` at `.190` and `lamp-a0ae` at `.197`; neither was authenticated or
changed. This does not establish whether 4ace is powered off, disconnected or
otherwise unreachable. No new physical experiment or latency result followed.
See local `artifacts/network-rediscovery-20261010/result.json`.

At the owner's request, the existing iMac camera app captured a fresh
1920×1080 still at `2026-10-10T14:51:51Z`, after three seconds of exposure
settling. The room was too dark to reliably confirm Lamp's presence, power
or safe clearance. No movement was requested. The original photo and
capture receipt are private local evidence, not a usable placement check:
`artifacts/presence-photo-20261010/`. Launching the existing app via macOS
`open` worked with its established permission; direct execution returned
a camera-permission error. No privacy permissions were changed.

The owner then directed continued work without the physical device until their
next office check. The optional ring choreography passed 535 host workspace
tests and a finite separate-process memory-sink probe; see
[its qualification boundaries](ring-choreography.md#host-qualification-2026-10-10).
It has no new Linux/ARM64 or physical result and does not change the audio
benchmark table above. No additional discovery, SSH or device test was made.

## Owner-requested connectivity recheck, 2026-10-10 15:43 UTC

A fresh read-only check still found no open port at `172.168.20.159`. The
bounded scan covered 509 other usable addresses on `172.168.20.0/23`
(excluding the iMac) on TCP 22/80/5001/8080, and compared reachable SSH keys
against the saved 4ace identity. No key matched. Public setup identities still
showed `lamp-52e6` at `.190` and `lamp-a0ae` at `.197`, plus a non-Lamp device;
none identified 4ace. The scan took 20.43 seconds. This does not establish
power state or explain the loss of connectivity. No SSH login or device change
was performed. This check was specifically requested after the owner deferred
physical work; local implementation/testing continues. Evidence:
`artifacts/connectivity-recheck-20261010-154339/`.

## Latest defect and evidence

The earlier speech-gap trial used source
`545e9b9b5d23d1f755bd2ab64748e5db00c8c7ec79f3b118fa1598d76080eed3`,
which passed 183 native ARM64 tests, strict workspace clippy, formatting and
release build. A previous source passed a 12-second real idle-clock smoke run.
The conversation then revealed behavior absent from the idle test: the speaker
worker prohibited accepted zero PCM while an unfinished reply awaited another
speech chunk. Nine accepted 240-frame speech blocks were followed by no accepted
PCM; capture continued until its reference lead was exhausted. The coordinator
also held back one full 240-frame block until more PCM or generation completion.

The trace records one input and low post-software-AEC VAD at failure; this trial
did not reproduce an extra admitted interruption before it ended. The speaker's
subsequent connection-refused error is not evidence of a cloud quota failure.
Fault-ended diagnostic prefixes are retained for diagnosis, with invalid final
status; they are not silently upgraded to complete audio evidence. No substantive
word boundary has been accepted for the 90 ms speech prefix.

The correction keeps the physical accepted-zero reference running during speech
supply gaps, retains owed speech and original ownership, and records gaps without
pretending delivery was continuous or complete. Source `59dc99df08a9d2eb69f81f577bb750c374a269afbfb01509ada36e0ac97d28d5` passed 379 native ARM64 workspace tests, strict clippy, formatting and release build. The subsequent real greeting failed for extra turn admissions and a reference-clock reset fault. It reached two speech bursts, but no speech supply gap occurred before the failure, so this run does not acoustically qualify the new gap path. No capture mute, VAD threshold relaxation, gain change or permit extension is justified by these results.

In the new trace, a microphone block was processed against playback epoch 2 before the reported speaker-start call completion. That completion is not the physical DAC start; the old trace lacks a pre-call request timestamp, so the exact pre-start phase is unproven. The running-clock phase accounting and the subsequent reset failure are being investigated separately from echo admission. Offline English ASR finds Lamp reply fragments in the pre-software-AEC and room recordings, while the post-AEC transcript contains only the question. This supports investigating residual echo but does not establish intelligibility or rule out other room sound. The detector still reached 0.997 during the first reply. Do not treat a large RMS reduction as successful interruption behavior.

New evidence: `artifacts/native-live-gap-59dc99df/` and `artifacts/directed-pilot-fvackatg/`. The installed runtime was restored and its health/motor inhibit verified after the failure.

Evidence is retained in `artifacts/directed-pilot-72kss2m8/`. Installed HAL,
os-server and Hermes were restored, health checked and the motor inhibit retained.
The C-Media room observer shares Lamp's host/card with playback; it is not an
external recorder or a qualified iMac-microphone setup.

## Matching the next cohort

The V1 pilot resolved extended-thinking Gemini, LOW and Kore; the early V2 pilots
used plain Gemini and Kore without a thinking field. A read-only persisted-config
check confirms the same credential value is used locally (no value or credential
hash recorded). V1's speech language is `en`. New optional V2 thinking/language
fields allow an explicit matching setup; their unit tests do not prove provider
acceptance. See [comparison configuration](gemini-comparison-config.md).

Keep the first speech-gap regression on its prior model configuration to isolate
the code correction. Then qualify the matching setup and run balanced repeated
V1-main/V2 conversation blocks. Prompt/tool load, context, local endpointing,
DSP and process ownership remain declared whole-system differences. Even a
matched-model improvement does not isolate the programming language's effect.

## Resource measurements

The old sampler in `72kss2m8` retained only coordinator counters because this kernel does not expose `/proc/PID/task/PID/children`. Reject those rows as aggregate runtime CPU/RSS/PSS evidence. The replacement bounded `/proc` parent/start-time scan passed five fixture checks and a native temporary parent/child probe. In `fvackatg`, all 13 discovery rows completed without errors and the running phase included the coordinator plus four worker processes. Startup/shutdown rows have fewer processes. This failed, short interaction is not a comparative resource benchmark. Collection is non-atomic, PSS is sampled less frequently, and the external observer/supervisor overhead must be applied consistently to the matched V1 workload.

## Hardware processing context

The owner relayed a three-capsule layout: a center environmental microphone and
a left/right Jieli voice pair with onboard processing. The host exposes two
mono capture devices. `pre_aec` is before lampOS software AEC, potentially after
board DSP; it is not raw separate capsule audio. Hardware echo cancellation and
its playback-reference wiring remain unverified. See
[microphone topology](microphone-topology.md). Keep any microphone/DSP treatment
constant across the primary runtime benchmark and report DSP A/B tests separately.

The isolated phase-accounting correction (source `b65b2c46d3e80b7272c4958abe4172c01fb487176dc6b73f2ddd7defb4424d92`) passed 397 native ARM64 tests, strict clippy, formatting and release build. It adds start-call request/completion bounds and counts only reads strictly before the request as the initial phase offset. It changes neither gain, VAD thresholds nor software noise suppression. The host serial live suite passed 91 tests; a default-parallel run had two diagnostic-writer finish timeouts under host load, which remain recorded. See [microphone control experiments](microphone-control-experiments.md) for the separate 11 physical capture trials and [offline processing](microphone-experiments.md) for the AEC/NS replay tool.

## Latest completed greeting

`directed-pilot-5koru8e0` ran 45 seconds and restored the installed runtime with
motors inhibited. The full reply in the room ASR hypothesis agrees with the
provider transcript: "I'm doing well, thank you. I'm ready to chat or help with
anything you need today." The run had one input, one reply and no extra admission.
Both diagnostic streams certified complete. There was no cancellation/reset, so
this run does not exercise the newly added restart-phase correction. It does
exercise the accepted-zero supply-gap path without the earlier worker failure.

The final local endpoint event to first ALSA-accepted reply PCM was 1,325.036 ms;
provider-first-audio event to that write was 6.487 ms. These software boundaries
exclude endpoint waiting, physical output delay and substantive-word annotation.
They are not the primary acoustic metric, and no p50/p95 or speedup is inferred.
The room recording is 45.030125 seconds. Its full-window ASR finds Lamp's reply but
misses the much quieter iMac question, so the transcript alone cannot supply the
user speech-end boundary.

There were 3 playback gaps: 2,160/240/240 actually accepted zero frames at 24 kHz
(90/10/10 ms). Before each later gap, the coordinator already had speech queued
but was processing a burst of provider messages before its next speaker dispatch.
That local scheduling defect is being corrected separately from the first
provider supply gap. Nothing was discarded or hidden to produce a passing score.
Evidence: `artifacts/native-live-phase-b65b2c46/` and
`artifacts/directed-pilot-5koru8e0/`.


## Explicit software-noise-suppression integration runs

Source `238ff60da22d0a8994b28103315d3727f8882747db790f848138051b81154b44`
passed 402 native ARM64 workspace tests, strict workspace Clippy, formatting and
release build. The on/off experiment option leaves AEC enabled and defaults to
noise suppression on. Requested mode, capture-worker mode and retained processing
metadata agreed in all three subsequent 45-second physical greeting runs.
Evidence: `artifacts/native-live-ns-238ff60d/` and
`artifacts/ns-integration-sanity-20261010/`.

The on/off/on runs admitted 1/3/5 inputs for one scheduled question each. Only the
first completed the original reply without interruption. All six capture/render
diagnostic streams certified valid. Six observed cancellation resets retained
continuous microphone sequences without ReferenceLate or worker failure; this
exercises the corrected lifecycle but does not establish successful echo rejection.
Extra admissions occurred during Lamp speech. The schedule suggests false
activations, but room sound has not been independently annotated to establish
that every event was echo rather than another speaker.

Scheduled-turn endpoint to first ALSA-accepted reply PCM was
1,204.696 / 1,125.151 / 1,495.123 ms. These are software boundaries; the configured
600 ms endpoint silence wait precedes that interval. They do not establish
speech-end to first audible word or a useful latency percentile. Two first-audio
provider events contained just one sample; the ensuing 196.110 / 175.072 ms until
first write mostly waited for substantive PCM. Treating those whole intervals
as local speaker latency would be wrong.

No noise-suppression winner is selected: the generated replies differ and both
modes have unresolved unwanted admissions. The separate offline English ASR
review is retained as hypotheses, including unreliable output on the third run's
Spanish reply and quiet intervals. It is not an acoustic boundary or a human
intelligibility score. The next causal comparison uses one preloaded cached reply
with source identity and identical gain/placement, then separately varies actual
near-end overlap. Installed services were restored after all three runs with
motor inhibition preserved.


## Playback scheduling and supply follow-up

The isolated scheduling source `1d115001f3a0c4aff9d52a865db494a92584787c04087309328ce80ae662a3ae`
passed 405 native ARM64 workspace tests, strict workspace Clippy, formatting and
release build. The new physical greeting `directed-pilot-g2kp1mcp` had one genuine
130 ms supply/holdback gap and no observed recurrence of a gap despite already
buffered speech. It still admitted an extra input 806 ms into the original reply,
cancelled that reply, and delivered a second answer. This is a conversation
failure; scheduling evidence does not qualify echo rejection. The board was
below 70 degrees C at pre-test readiness. Installed services were restored and
motor inhibition retained.

The first question's local endpoint to accepted reply PCM was 1,324.559 ms,
excluding the preceding endpoint silence wait and acoustic output delay. The
first source packet contained 960 samples. Playback consumed three 240-frame
blocks and held its final block; the next provider event arrived about 170 ms
later. Speech resumed 2.040 ms after the next eligible coordinator enqueue.
No old queue backlog was available to remove this supply gap. The gap's two
start/resume observations describe one 3,120-zero-frame interval, not two gaps.

A five-run, twelve-turn trace audit found seven supply/holdback gaps and three
10 ms local scheduling gaps. Counterfactual startup buffering is not a selected
fix: requiring 40 ms playable supply would add 158.898 ms before this new reply;
an 80 ms wait cap would still leave a simulated 50 ms gap. Another recorded
reply waits 371.566 ms to reach an 80 ms supply threshold. These simulations
preserve observed cancellation horizons and flag censored output; they are not
acoustic tests or a reason to hide failed turns. Provider event observation and
coordinator enqueue are distinct from actual network/model arrival.

Evidence: `artifacts/native-live-scheduling-1d115001/`,
`artifacts/directed-pilot-g2kp1mcp/`, and
`artifacts/provider-supply-review-20261010/`. The bounded scheduling code is
explained in [playback scheduling](playback-scheduling.md).

## Fixed reply: cancellation continuity repaired, false admission remains

The offline-provider fixture `h4jds57i` played the cached iMac greeting and one
cached Lamp reply through real hardware. Gemini was disconnected and no second
stimulus was scheduled. V2 still admitted a second input 846.505 ms after first
reply acceptance, so the answer was cancelled after 20,400 of 107,760 frames.
This remains a conversation failure, not a successful latency observation.

Unlike the prior fixed-reply fault prefix `7v7pvbyl`, the qualified continuity
fix kept the runtime alive for all 30 seconds. All 3,001 capture and 3,003 render
blocks were contiguous and hash-verified, with no cancellation reset or
reference fault. Old-owner writes stopped, actual zeros continued, and the
288-frame tail retired 14.174 ms after the cancellation trace. That is a
software/ALSA boundary, not audible stop latency. The next task is rejecting
false admissions while preserving quiet speech and genuine overlap.

Evidence: `artifacts/native-live-tail-camera-e4ff39d9/` (514 native tests, strict
Clippy, formatting, release build) and
`artifacts/fixed-reply-clock-continuity-20261010/` (physical diagnostics and
independent review). Installed services were restored; motors stayed inhibited.

## Later fixed-reply comparison

Two additional fixed-reply cases used NS off then on with fresh mixer readings.
The off case completed a certified full reply with no extra admission or gap.
The on case also retained a full reply without an extra admission, but failed
render diagnostic finalization after a terminal reference-peer socket race.
It remains invalid. This comparison does not select a noise-suppression winner,
provide an acoustic latency percentile, or qualify genuine human interruption.
No settings were selected for release. Installed services were restored after
both cases, and unchanged mixer state was verified again after the failure.
See `artifacts/fixed-reply-ns-pair-20261010/` and
[microphone audit](microphone-v1-v2-audit.md) for the separate physical and
observation-only replay findings.


## Credit-conserving handoff checkpoint

The terminal reference-close fix is promoted in source `36566770ec2a8531a4a3ea85373ea76237994dd8a62a826ab8b8d474767998e4`.
All 518 native ARM64 workspace tests passed, followed by strict all-target
Clippy, formatting and release build. This validates compilation and regression
checks; no physical rerun of this fix was performed. The prior NS-on recording
remains invalid. Echo admission and genuine human overlap remain unqualified;
there is still no accepted paired latency percentile or speedup.

The owner requested no new experiments and an English handoff for Claude Code.
Installed services, motor inhibition and absence of a remaining V2 runtime were
verified read-only. See [the handoff](../HANDOFF.md),
`artifacts/native-live-terminal-36566770/`, and
`artifacts/handoff-20261010/device-check.json`. Reusable recordings and helpers
were copied to private durable local storage. The owner subsequently authorized
committing and pushing the source/docs checkpoint on `lamp-v2-chat`; this does
not include a fleet release or a claim of product acceptance.
