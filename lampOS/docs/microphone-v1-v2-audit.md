# V1 microphone handling and V2 interruption diagnosis

This audit uses V1 main commit
`d5efe9d7b73cc529b34cd4abe97624682a82ca94`, not the modified installed V1.
The complete source review, original line references and 34-file SHA inventory
are in [the audit evidence](../artifacts/v1-main-microphone-audit-20261010/report.md).
These are source findings; saved device flags and input mode can change the
actual path.

## What V1 does

| Area | Pinned V1 main behavior | Implication for V2 |
|---|---|---|
| Microphone roles | Jieli mic2 for voice; C-Media mic1 for separate ambient RMS sampling. | Keep explicit roles. V1 does not fuse the two microphones for echo detection or direction. |
| Stock playback handling | `HAL_LIVE_MODE=false`; automatic voice capture discards mic frames while TTS speaks, or releases capture when warm-mic mode is off. | Stable replies in this mode do not prove effective full-duplex echo cancellation. Natural spoken interruption is suppressed too. |
| Software processing | AEC+NS near playback; both bypass after 2 s without a speaker reference write. AGC is disabled in the software processor. | V2's continuously active processing is different. Compare NS using identical waveforms and near-end speech preservation. |
| Optional hardware-AEC branch | Enabled only by `LIVE_MODE && !AEC_ENABLED`, with playback-relative level gating, warmup, pre-roll and temporary speaker ducking. Local activity alone does not cancel the reply. | The branch is a useful design reference, not evidence that Jieli's hardware cancellation is enabled. Its 3 s warmup and 240 ms activity requirement must not be copied without evaluating missed/slow interruptions. |
| Software-live confidence | `uncancelled=false` means processing had enough reference bytes. | Processing success is not proof that residual echo is absent or that speech came from the user. |
| Level normalization | Optional filtering and RMS normalization are part of speaker identification, not the STT/model uplink. | Adding that chain to V2 voice capture would be new behavior, not V1 parity. |

## Exact V2 trigger

The current finite directed test intentionally has no speaker/addressee inference.
`TurnDetector` starts an input after six consecutive 10 ms post-processing
blocks with speech probability at least 0.80. The coordinator then cancels an
unfinished reply and admits the new input. A speech probability is insufficient
to distinguish a person from Lamp's own speech.

In real pilot `g2kp1mcp`, first ALSA reply acceptance was 181601976891 µs;
the second input was admitted at 181602782864 µs, 805.973 ms later. No second
iMac utterance was scheduled. Captured blocks 773–778 supplied the six qualifying
scores. This establishes the local cancellation trigger; residual speaker echo
is the leading acoustic explanation, not a completed root-cause isolation of
the echo canceller.

The cached-reply pilot `7v7pvbyl` reproduces a second admission 835.170 ms after
first reply acceptance with no Gemini connection and no scheduled second prompt.
It then fails during cancellation: the last accepted render observation had
456 queued frames at 183637586619 µs. Capture reports nominal reference starvation
at 183637607118 µs, before speaker reset at 183637608132 µs and reprime. The two
failures are distinct: false admission first, reference/reset lifecycle failure
afterward. The run's diagnostics are an invalid fault prefix; it is not an
accepted echo-only trial, conversation pass, or acoustic latency measurement.
The normal stationary runtime was restored and verified.

The failed prefix supplies a narrower acoustic finding: the six triggering
post-AEC VAD scores were 0.940 / 0.970 / 0.967 / 0.967 / 0.954 / 0.864.
Post-AEC energy rose rapidly while the cached Lamp reply was playing. The room
recording contains the question followed by Lamp's reply; offline ASR of the
processed microphone contains only the question. Correlation and spectrum
checks are compatible with transformed speaker residue but do not establish
intelligible echoed words or separate leakage from a processing artifact. The
confirmed root cause of cancellation is the missing interruption-admission
evidence, not a proven specific hardware DSP defect.

## Changes to evaluate

- Separate activity detection from an accepted interruption. Preserve a short
  input prefix; use playback-aware echo evidence and measure false interruptions
  together with quiet speech and overlap. A reversible provisional response
  such as ducking must not erase the reply or become repetitive behavior.
- Keep ordinary turn cancellation from unnecessarily breaking the continuous
  audio/reference clock or rebuilding a learned echo path. Any bounded retirement
  of already accepted speech needs explicit old-owner cursors, a deadline, no
  new old-owner writes, and immediate flush on privacy/fault/authority loss.
  The qualified candidate now has one successful physical cancellation
  continuity regression; it does not yet reject the false input that triggers it.
- Validate processing and delay hypotheses on the target architecture. The
  unchanged x86_64 replay diverged later in the recording. The first native ARM64
  replay now reproduces all PCM samples, VAD scores and admission prefixes
  exactly. All five original runs subsequently passed their recorded-mode
  baselines before the predeclared delay offsets ran (results below). Exact replay
  is not an echo improvement. Evidence: `artifacts/native-echo-oracle-20261010/`.
- Use fixed reply recordings for echo experiments, then direct overlap and
  unrelated speech cases. Two synthetic voices from one iMac speaker do not
  establish behavior with two spatially separate people.

Failure receipts and traces are retained in
`artifacts/fixed-reply-pilots-20261010/`; raw room/microphone recordings remain
private. No successful V1-versus-V2 end-to-end p50/p95 or winning audio flag is
claimed by this audit.

## Native delay experiment: no default change

The later [pinned-source alignment audit](aec-alignment.md) clarifies that the
setter seeds startup/reset alignment while the internal estimator controls
normal AEC3 alignment. This experiment varied hints, not fixed reference offsets.

The five unchanged recorded-mode replays reproduced every PCM sample, VAD score
and admission prefix exactly on ARM64. Only then did the fixed common-NS-on
matrix run. It added the same 0 / 80 / 100 / 120 ms to every recorded queue-delay
hint, with no reference rescheduling or reset changes. The previously NS-off
recording had a separate original-mode oracle; its NS-on comparison is a
counterfactual, not its original configuration.

| Added hint | Replayed input starts across five recordings | Reply blocks with VAD at least 0.80 |
|---|---:|---:|
| 0 ms | 12 | 119 |
| 80 ms | 11 | 123 |
| 100 ms | 13 | 164 |
| 120 ms | 12 | 132 |

No setting consistently improves the recordings. At +80 ms, one recording's
starts fall from five to two while another rises from one to two and a third
from two to three. These open-loop counts are not a closed-loop conversation
success rate: actual audio and resets remain those of the original recording,
even when a changed detection would have altered subsequent playback. Several
windows are censored or cross those original resets. All treatments preserve
the known greeting PCM; this says nothing about quiet direct speech or overlap.

Keep the existing delay setting. The subsequent fixed-reply fixture qualified
clock continuity through one cancellation; false interruption admission remains
unresolved. Evidence, exact commands and all per-record delay treatments are
in `artifacts/native-echo-delay-matrix-20261010/`. No delay flag was deployed
as a result of this experiment.

## Physical follow-up: separate trigger from cancellation failure

Fixed-reply run `h4jds57i` used the qualified native continuity fix and again
admitted a second input with no scheduled second prompt: 846.505 ms after first
ALSA reply acceptance. It retained 30 seconds of continuous capture/reference
with no cancellation fault or DSP reset. The 288-frame accepted tail retired
14.174 ms after logical cancellation; this is not an acoustic latency measure.

Offline ASR of the first 12 seconds again finds the greeting and the start of
Lamp's reply in the room/pre-software-AEC recordings, and only the greeting
after software processing. These are hypotheses, not proof that no other room
sound existed or that the echo canceller removed all speech-like residue. The
confirmed trigger remains six high-VAD blocks followed by unconditional local
interruption admission. See [continuous audio clock](continuous-audio-clock.md)
and `artifacts/fixed-reply-clock-continuity-20261010/` for the separate repair.

The two fixed-reply triggers are also waveform-related. Comparing the exact
60 ms trigger from the earlier trial against a fixed +/-60 ms search in the
new recording yields normalized correlations 0.998666 before software AEC and
0.808681 after AEC+NS, at the same alignment. Both primary inputs correlate
with cached-reply source segment 0.65-0.72 s. This strengthens the self-playback
residual explanation beyond similar timing; the coefficients are not
probabilities. It does not isolate hardware DSP, acoustics, AEC or NS, and
post-to-source matching does not justify an exact echoed-word claim. The
window, search range, spectra and source hashes are retained in the same
artifact's `trigger-report.md` and `trigger-report.json`.

## Existing echo metrics do not establish an interruption gate

A native ARM64 observation probe replayed five certified recordings in their
original modes, then enabled the optional residual-echo detector. All ten passes
matched the original PCM, VAD, admissions and reference/reset lineage exactly
(22,504 capture blocks per mode). The probe made no runtime changes.

The likelihood was useful in some extra admissions but remained zero for the
first five of six triggering blocks in one case, reaching only 0.000437 on the
admitting block. Another reached 0.018337. Two replies without extra admissions
still reached maxima 0.107349 and 0.306848. All five known greeting starts had
only silent render reference, so apparent greeting/echo separation is confounded
by playback absence. ERL/ERLE at every start were initialized/held values rather
than validated acoustic measurements. No scalar threshold, real-time overhead
claim, or treatment default follows. Evidence:
`artifacts/native-echo-telemetry-20261010/`.

## Fixed-reply NS off/on comparison

The next two physical runs used the same source, cached greeting/reply and
recorded mixer settings. The NS-off run certified both diagnostic streams,
accepted all 107,760 reply frames in 449 ordered chunks, retired the original
reply with zero gaps, and admitted no extra input. Its maximum post-playback VAD
was 0.752864.

The subsequent NS-on run also retained a full reply and no extra input in its
recorded prefix (maximum VAD 0.699609), but its render stream failed finalization.
It is an invalid trial, not a clean paired pass. The failure is a terminal peer
notification race: capture closed its direct reference socket on privacy denial
before speaker sent its final Stopped notice. The speaker had already stopped
PCM and recorded its privacy reset; the failed terminal send converted that
close into a fault. Diagnostics correctly refused to certify it.

Both primary inputs match the earlier falsely admitted reply segment closely,
while the processed residual differs. Neither reproduces the false admission,
and this pair does not select NS-off. Whole input recordings contain 36/30
clipped rail samples during early playback; the comparison windows and processed
outputs do not clip. Mixer settings were unchanged, including a later read after
the failed run. Cleanup/restoration succeeded, and motors stayed inhibited.
Evidence: `artifacts/fixed-reply-ns-pair-20261010/`. A prior read-only preflight
used the PCM alias as a hardware card ID and failed before any physical trial;
its receipt is retained separately.
