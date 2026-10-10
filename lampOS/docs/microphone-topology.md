# Microphone topology and processing qualification

Updated 2026-10-10 from the owner's relayed hardware-team layout, the pinned
parts list, manufacturer material, and a read-only check on lamp-4ace.

## Three physical capsules, two active capture devices

The supplied front-view diagram identifies one central capsule as `mic1` and
the two outer capsules as `mic2`. These are two input roles, not two physical
capsules. The team reports that the outer pair feeds a Jieli board with onboard
processing. Its mono USB stream does not mean that the board contains only one
microphone. Earlier channel-count reasoning must not be used to deny its
internal two-microphone arrangement.

| Role | Team-reported hardware and placement | Observed host interface |
|---|---|---|
| Environmental sound / mic1 | Center capsule through C-Media / Unitek Y-247A | `plug:device_micro1`, C-Media capture: mono PCM16 at 44.1/48 kHz; separate from its stereo playback endpoint. |
| Conversation / mic2 | Left and right capsules through a Jieli dual-microphone board | `plug:device_micro2`, Jieli capture: mono PCM16 at 48 kHz; one USB input endpoint, no playback endpoint in the observed ALSA stream descriptor. |

The manufacturer's [Y-247A page](https://www.unitek-products.com/products/usb-2-0-to-stereo-audio-converter)
describes a USB sound adapter with analog microphone input, supporting 16-bit
48 kHz conversion. This identifies the adapter, not the center capsule's
sensitivity, frequency response, directivity or self-noise.

The pinned parts list names the voice board `USB dual mic HK JZMIC v1.0`, although
its row calls it Mic 1; the wiring document and actual ALSA roles call voice
`device_micro2`. Use stable device roles rather than the inconsistent row label.
The [HK-JZMIC v1.0 manufacturer page](https://haokaimic.com/products/hk-jzmic-v1-0)
lists two analog microphones, voice enhancement, noise suppression, automatic
gain control, and 48 kHz/16-bit capture. The older manufacturer's
[product listing](https://www.haokaisz.com/en_US/proshow/1/HK-JZMIC-V1-1)
also identifies dual microphones and voice enhancement/noise reduction.
Those claims and the BOM do not identify the fitted board's firmware or verify
its active processing mode. Do not apply another board's advertised functions.

## Echo cancellation is still unqualified

The team reports echo cancellation and beamforming as well as environmental
noise cancellation. The inspected model-specific manufacturer pages do not
establish a usable acoustic echo-cancellation reference input, raw left/right
channels, direction-of-arrival output, or controls to select those functions.
Their absence from those pages does not prove the hardware lacks them.

Lamp's current software speaker route ends at the C-Media DAC and amplifier.
No ordinary USB playback endpoint was observed on the Jieli capture device.
Therefore a playback reference reaching Jieli through that USB audio route is
not established; an additional board-level connection or proprietary interface
would need separate evidence. Noise suppression/ENC must not be treated as proof
that Lamp's own speech will be removed during overlapping human speech.

The new software playback-clock defect remains a separate demonstrated bug:
a speech-chunk gap stopped the accepted PCM reference. Fixing that scheduling
bug neither proves nor disproves the hardware DSP's echo performance.

## Measurement names and intended use

`pre_aec.pcm16le` means ALSA-resampled input before lampOS's **software** Sonora
processing. On this voice input it can already include the board's processing;
it is not raw, independent microphone-capsule audio. `post_aec.pcm16le` is the
output after lampOS processing. Record mixer/AGC and any verified firmware modes
with both files. A clean-looking silent waveform can reflect suppression or a
noise gate, not a quiet room or successful speech preservation.

Keep the conversation input on Jieli for the current stationary pilots. Retain
mic1 for environmental sensing and the temporary continuous benchmark observer.
The observer shares Lamp's host and C-Media card with playback; it is not an
external microphone. Environmental levels remain uncalibrated digital levels.
Do not treat independent Jieli and C-Media USB clocks as a synchronized array.
The mono voice output does not expose separate left/right timing for a software
direction estimate. A vendor direction output or accessible synchronized raw
channels would require verification before use. Vision and fixed desk calibration
remain separate evidence for attention and orientation.

## Direct speech versus loudspeaker replay

The owner relayed an additional team listening observation: direct human speech
recorded through mic2 sounded clear, while speech played through a loudspeaker
and recorded again through mic2 sounded muffled. No paired WAVs, matching gain,
placement, loudspeaker response or DSP configuration accompanied that report.
It is a useful hypothesis and measurement warning, not a quantified comparison
or proof of a human-versus-recording detector.

Loudspeaker, room, microphone geometry, existing source processing, AGC and board
DSP may all contribute. Do not attribute the effect to ENC alone or call the
board a liveness detector. The current iMac-to-Lamp pilots remain physical
**loudspeaker-replay** tests. They exercise cloud interaction, actual audio I/O,
playback, cancellation and repeated regressions, but cannot alone establish
latency or comfort for direct human conversation. Keep replay and direct-human
results in separate cohorts; repeat the primary acoustic boundaries with a real
speaker before manual acceptance.

Compare the original direct-mic files with the replayed files if those source
recordings become available. Sending a direct-mic recording into an offline or
provider harness is useful for isolating software behavior, but bypasses live
acoustics and is not itself an end-to-end human trial. Existing speaker-only
recordings and pre/post-software-AEC diagnostics remain useful with their limits.

## Required controlled comparisons

1. Verify the exact fitted board/firmware, exposed DSP controls, playback-reference
   wiring and any direction/raw-channel interface without guessing vendor writes.
2. Use cached Lamp-only speech, human-only speech, genuine overlap, pauses,
   near/far speech and fan/keyboard/media noise at fixed gains and positions.
3. Compare board-processed USB input alone with additional software AEC and noise
   suppression as separately declared treatments. First inspect matched recorded
   signals; only promote a treatment after live full-duplex tests. Do not disable
   software AEC globally based on a feature label.
4. Measure preserved opening words, echo-only false admissions, overlap speech
   intelligibility, interruption-to-silence, final speech-to-answer, clipping and
   processing/queue delay. Quiet output alone is not a success criterion.
5. Keep the hardware treatment identical between V1-main and V2 for the primary
   runtime comparison; report microphone/DSP A/B experiments separately.

Multiple synthetic voices from one iMac loudspeaker do not qualify direction
finding or spatial separation. The team drawing supplies relative placement,
not measured capsule spacing or a qualified beam pattern. No enclosure was
opened or movement commanded. The later finite [microphone control experiments](microphone-control-experiments.md) changed only advertised Jieli gain/AGC and restored both; no new runtime setting was promoted.
Evidence: `artifacts/microphone-layout-20261010/`.
