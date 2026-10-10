# Playback service during provider bursts

The directed runtime must service already available speech without waiting for
an entire burst of newly arrived cloud audio to be decoded. The service target
is to hand off the next eligible speaker block within one 10 ms playback block,
while processing retained microphone input and cancellation first. This is a
target to measure on Lamp, not a hard real-time guarantee from host tests.

## Observed failure

The 45-second physical run `directed-pilot-5koru8e0` completed with one admitted
question and one reply, valid capture/render diagnostic recordings, and three
reported playback gaps. Exit zero did not make its speech uninterrupted. Its
phase-corrected candidate had passed native tests, Clippy, formatting and build.
This particular run did not exercise a cancellation/reset.

The first gap accepted 2,160 zero frames (90 ms at 24 kHz) while the provider
had not supplied more than the deliberately held final 240-frame block. It
remains a real supply/holdback gap. The other two gaps had a different cause:

| Evidence boundary, host monotonic microseconds | Gap 2 | Gap 3 |
| --- | ---: | ---: |
| Previous speech chunk accepted by ALSA | 180007622605 (sequence 25) | 180008042311 (sequence 66) |
| First provider packet enqueue in the blocking burst | 180007624683 | 180008044938 |
| Reply PCM already queued before that new 960-frame packet | 3,840 frames | 43,920 frames |
| Last provider packet enqueue in the burst | 180007633659 | 180008057654 |
| Provider packets handled before the next dispatch | 13 | 18 |
| Actual zero PCM accepted at the missing speech position | 180007632885 | 180008052591 |
| Following speech chunk accepted by ALSA | 180007643175 (sequence 26) | 180008062875 (sequence 67) |
| Zero frames inserted | 240 (10 ms) | 240 (10 ms) |

Before each burst, the coordinator had consumed the previous speaker acceptance:
the provider enqueue records show no outstanding speaker sequence. It then spent
8.976 ms and 12.716 ms between the first and last enqueue records, respectively,
before reaching speaker dispatch. Decoding the first packet and subsequent
dispatch work are outside those intervals. The old loop allowed up to 32 provider
packets before servicing capture and the next speaker handoff.

This establishes local scheduling starvation despite already available PCM. The
speaker correctly kept the physical reference clock running with actual zeros
when no authorized speech chunk had arrived. No missing reference or AEC failure
is needed to explain these two gaps. The later gap observation's queue size alone
would not establish earlier availability; the preceding enqueue records do.

These timestamps describe host receipt and ALSA acceptance. They do not locate
the first audible word or prove the perceptual effect of a 10 ms inserted zero
block. Independent room audio remains the acoustic evidence.

## Bounded correction

The coordinator now handles at most two provider packets per service slice and
checks a 2 ms elapsed budget before decoding another packet. It then runs the
existing retained-input, cancellation and speaker-dispatch path. The next tick
begins with privacy and worker control. Remaining provider packets stay in FIFO
order on the existing bounded transport; they are not dropped or coalesced.

The elapsed limit is cooperative: a single bounded datagram decode, OS scheduling
delay or other operation can exceed the target. The two-packet ceiling also
applies when handling is fast. Two full 960-sample packets contain 80 ms of audio,
so this service slice does not require waiting for a playback buffer to fill.

The correction does not increase the outstanding speaker chunk count, the
240-frame final-block holdback, the 960-frame hardware/ownership bound, IPC
freshness limits, or authority leases. Actual zero writes and explicit
`speech_gap` receipts remain necessary when speech is unavailable. No speech
chunk can bypass input cancellation or final speaker authority checks.

## Verification and remaining measurement

Host regressions use connected private Unix channels to feed the observed
13- and 18-packet burst sizes, retaining sender data on backpressure. They verify
that the production slice yields before draining the burst, the next speaker
chunk is handed off, an unacknowledged chunk prevents another handoff, and all PCM
is retained in order. A deterministic elapsed-budget case leaves the next packet
unconsumed. A cancellation between provider service and dispatch revokes the old
permit and prevents its queued PCM reaching the speaker channel.

These tests model packet-processing cost without sleeping or imposing an audio
latency assertion on CI. Native qualification and a repeated physical conversation
must still measure whether local gaps disappear, whether the genuine initial
provider gap remains, and whether interruption and opening-word retention hold.
The existing physical run is before-change evidence, not a claim that the fix
already passed on Lamp.


## Native and first physical follow-up

Source `1d115001f3a0c4aff9d52a865db494a92584787c04087309328ce80ae662a3ae`
passed 405 native ARM64 workspace tests, strict Clippy, formatting and release
build. The next physical greeting (`directed-pilot-g2kp1mcp`) had no observed
buffered-speech scheduling gap. Its only gap contained 3,120 real zero frames
(130 ms) while the provider had supplied only the held final block. After the
next eligible PCM enqueue, speech resumed in 2.040 ms. This single run is useful
integration evidence, not a reliability rate or an acoustic-latency comparison.
An unscheduled admission still cancelled the original reply; that failure and
all diagnostic evidence remain in the [benchmark progress](benchmark-progress.md).
