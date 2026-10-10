# Physical microphone control experiments

Checkpoint: 2026-10-10, lamp-4ace. No new microphone setting is selected.

## What ran

Eleven finite paired captures recorded both Jieli conversation input and C-Media
ambient input for 26 seconds each: 286 seconds per microphone. Every trial used
the same cached iMac greeting and output route/volume. All 11 player receipts and
22 capture streams completed; all Jieli gain/AGC controls and the installed
stationary runtime were restored after each batch. No Lamp speaker, cloud
connection, software AEC/NS or motor was used during these capture experiments.
The fixture is loudspeaker replay, not a direct-human recording.

The first five-trial A/B/A/C/A batch compared gain 147 / AGC on, gain 115 / AGC on and
gain 147 / AGC off. Matched Jieli speech RMS stayed between -17.36 and -17.11 dBFS.
The wider six-trial probe then checked minimum gain while advertised AGC was off:

| Trial in order | Advertised gain | Advertised AGC | Matched voice RMS dBFS | Independent ASR hypothesis for opening word |
|---|---:|---|---:|---|
| Baseline 1 |147|on|-17.107|Len|
| AGC off |147|off|-16.947|Len|
| Lower gain |115|off|-17.243|Left|
| Minimum gain |0|off|-17.245|Lab|
| Return to maximum |147|off|-16.971|Well|
| Baseline 2 |147|on|-17.159|Lab|

The remainder of each ASR hypothesis was "how are you doing today?". This is
one independent offline recognizer, not Gemini, a listening panel, or a WER
cohort. No treatment has demonstrated improved recognition of "Lamp".

Gain0 versus the bracketing gain 147 trials with AGC **off in all three** changed
matched RMS by only -0.299 and -0.274 dB. The advertised range spans 27.43 dB.
Controls read back as requested both before and after capture, including gain 0.
Therefore successful USB control readback is not enough to establish useful
control of the captured speech level. These recordings cannot distinguish a
no-op firmware control, another adaptive processing stage, or other board
behavior. No firmware bypass or vendor-specific mode has been established.

No Jieli sample reached a digital rail in these 11 recordings. This does not
exclude upstream analog clipping in other scenes. C-Media remained at gain 4;
its matched greeting was much quieter (around -60 dBFS). This is an unqualified
observer/sensing level, not evidence that the center microphone cannot capture
speech or a reason to replace Jieli without testing.

## Interface findings

Read-only ALSA/USB inspection exposes Jieli mute, capture volume 0..147 and
advertised automatic gain control. The audio endpoint is 48 kHz mono capture.
The 51-byte HID descriptor is a consumer-control input report with media and
volume usages; no output/feature report or vendor page was identified. Neither
this descriptor nor the audio stream establishes a DSP bypass, two raw capsule
channels, a direction estimate, or a playback-reference input. No speculative
HID/vendor writes or mass-storage operations were performed.

## Measurement limits and evidence

RMS windows are aligned by normalized waveform correlation against the cached
fixture; they are not manually reviewed speech-onset or speech-end boundaries.
The fixed two-second settling interval does not prove that hidden DSP adaptation
was fully settled. Trials are sequential with baseline returns, not randomized
population evidence. Room conditions and direct-human speech need separate
qualification. The two USB inputs have independent clocks and cannot be treated
as an aligned microphone array.

The external experiment monitor held the physical privacy GPIO and checked it
during recording. Its maximum reported check interval includes uncaptured setup
and file/control work; this is not a runtime privacy-latency certification.

`artifacts/microphone-controls-20261010/` retains control descriptors, requested
and observed values, player/capture receipts, hashes, analysis and restoration
records. Private source WAV directories are listed in its summary; no binary
recording is added to source control.

## Separate software-processing result

On the fault-ended `directed-pilot-fvackatg` recording, replaying the pinned
AEC+NS chain reproduced all three admission prefixes and both endpoints.
AEC-only left the first unscheduled admission unchanged. Its later rising VAD
probability was cut off by the runtime failure, so absence of a third admission
before that cutoff is not a fix. Removing NS is not recommended on this evidence.
See `artifacts/offline-vad-fvackatg-59dc-final/review.md`.

Next qualification separates echo-only playback, real overlap and unrelated
speech. It must preserve opening words and natural interruption while avoiding
self-interruption; a lower residual RMS by itself does not satisfy that goal.
