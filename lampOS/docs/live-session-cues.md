# Conversation lifecycle cues

Both `lamp-live directed` (Gemini) and `directed-fixture` (one cached reply)
can emit the same optional local scheduling cues. This closes proposal P1:
the voice runner can trigger a follow-up after observed whole-turn completion or
a topic change during observed playback, rather than guessing the model delay.
It does not change admission, VAD, echo processing, voice ownership or content.

```sh
lamp-live directed /absolute/private/provider.json 60 /new/private/run \
  --cue-socket /absolute/private/session/cue.sock
```

The consumer must already have bound a same-user Unix datagram socket inside a
private, non-symlink directory. The producer connects before starting workers.
The flag is optional and accepts an absolute path; missing, repeated, relative
or flag-shaped values fail parsing. `SessionOptions` combines the observation
path with the existing copyable `DirectedOptions`. Existing library entry points
without cues keep their behavior. Fixture runs still require `--diagnostics`;
Gemini cues do **not** turn on microphone PCM recording.

## Contract

Schema 1 retains a maximum datagram size of 512 bytes and a 100 ms expiry from
the original event timestamp. Fields include session boot, sequence, turn,
ownership generation, capture/reference epoch context and event/send/expiry
times. Playback cues also carry the existing controller's `playback_sequence`;
completion carries the exact outcome and playback-gap count. A cue carries no
PCM, transcript, credential, permission or control.

| Kind | Original observation |
|---|---|
| `listening_ready` | Input retention and provider readiness permit the directed session. |
| `input_admitted` | A specific candidate was accepted as a turn, after local admission. |
| `local_endpoint` | Captured input ended under that turn's ownership. This is not acoustic speech end. |
| `speaker_first_write` | First accepted speaker write for one playback occurrence, not first audible word. |
| `speech_retired` | That occurrence's matching final sample retirement report; the same turn may speak again. |
| `turn_completed` | Provider idle, no queued reply PCM, no pending speaker chunk and no active playback. Includes original generation, outcome and gap count. |
| `cancelled` | The original turn's cancellation; optional `reason` retains its `turn_finished.outcome`. |
| `run_end` | The finite conversation ended; the final trace supplies its complete outcome. |

Normal `turn_finished` records produce completion cues, never cancellation cues.
They retain a top-level `generation`; cancellation retains its original `owner`.
New input does not
lend an old event the successor's generation. The endpoint trace now includes
its actual owner so its cue has the same provenance as admission and playback.
The schema number remains 1 with additive fields. Current scheduling producers
and consumers must be updated together; old traces cannot supply missing
occurrence evidence merely by being readable.

Playback retirement, turn completion and cancellation are distinct. One Gemini
request can generate another audio segment after its previous segment retires.
Consumers must match each start/retirement by `playback_sequence`, keep the turn
open until completion or cancellation, and preserve cancellation between or
during later segments. Retirement of the first segment cannot certify the
whole answer or trigger an ordinary post-answer follow-up. The existing
`SpeechRetired` trigger retains its segment meaning for historical plans;
current follow-up plans use `TurnCompleted`. See
[playback occurrences](playback-occurrences.md).
Sequence and send times follow emission order; original event
times need not be globally sorted because child-worker observations can arrive
later. Keep those timestamps unchanged and validate each event's freshness.

The existing `CueSink` serializes into a fixed stack buffer, sends nonblocking,
does not retry, and scans each retained trace event once. Peer loss, a full
socket or a stale/invalid event latches one scheduling fault; it cannot cancel
speech or alter privacy authority. The final trace marks `cue_valid: false`
and retains the historical `fixture_cue_invalid` event name for either provider.
Scheduling evidence is then unusable; voice handling continues independently.

## Responsiveness and verification

The design target for event creation through local peer receipt is p95 under
2 ms on the host; the unchanged conversation loop normally services cues on
its 2 ms cadence. The 100 ms expiry is a rejection boundary, not a latency
target. Setup filesystem checks occur before workers start, not per event.

At the preceding directed-cue checkpoint, a 200-event same-process Unix-socket
test measured first event 31 µs, p50 16 µs, p95 18 µs and max 31 µs on the x86-64
Mac. The boundary starts before event JSON creation and ends after receiving
its datagram. It excludes coordinator
scheduling, relay/network time, ARM execution, microphone and acoustic latency.
This is observation overhead, not a before/after voice speedup.

Focused host checks passed 26 fixture/producer tests and 27 coordinator tests. They
cover ownership, cancellation reason, duplicate/missing CLI options, maximum
encoded fields, stale events, private endpoints, peer loss and full queues.
The [playback occurrence qualification](playback-occurrences.md) records the
current source identity and complete gates. The earlier
[directed-cue checkpoint](directed-cues-integration-20261011.md) retains its own receipts.
The physical runner's real-relay/fake-Lamp tests are described in
[directed cue evaluation](voice-eval-directed-cues.md); they produce no audio.
Linux/ARM64 and live Lamp qualification remain required before a physical claim.
