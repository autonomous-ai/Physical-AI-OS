# Conversation and ring ownership

The directed Rust runtime now has an optional, separately supervised ring
writer. The interaction Controller owns the conversation; a small local policy
derives light cues from that authority. This is the first connected expressive
output, not a finished character performance. Head/body motion, visual social
understanding and expressive variation remain implementation and physical
qualification work.

## What triggers a cue

| Observed state | Current prototype behavior |
|---|---|
| Startup, no admitted turn or completed turn | Black. Worker readiness alone does not imply listening readiness. |
| Fresh retained input admitted to the current turn | Static listening cue, RGB `(24, 32, 40)` before the configured channel ceiling. |
| Local input ended; the turn is unfinished without active playback | Static waiting cue, RGB `(32, 24, 8)`. No spoken waiting filler. |
| Matched speaker acceptance followed by `playback_started` | Static speaking cue, RGB `(32, 32, 24)`. Provider audio arrival alone does not start it. |
| Playback's actual final sample retired | End the speaking phase; completion clears the owner and cue. |
| New admitted question, privacy closure, expired authority or stop | Retire the old cue under its original identity. A successor can obtain its own cue. |

These colors are low-level prototype choices, not an accepted visual design or
measured brightness. The cue signals accepted conversation state, not proven
social understanding: the directed voice harness still has false interruptions
from playback and does not know whether ambient speech addresses Lamp.
ALSA acceptance/retirement are software/driver boundaries, not optical or
acoustic measurements. Stillness, silence and an unlit ring are valid behavior.

## Explicit qualification option

Append `--ring-channel-ceiling 0..120` to `directed` or `directed-fixture` to
include the ring worker. A value of zero still starts the guarded writer but
caps its output at black. Without the option no ring device is opened. Repeated,
missing, negative or out-of-range values fail argument validation. The option
is independent of audio diagnostics and software noise suppression.

```sh
target/release/lamp-live directed /absolute/private/provider.json 45 /new/private/run --diagnostics --ring-channel-ceiling 24
```

This flag isolates qualification experiments; light remains part of the full
release scope. The command requires exclusive hardware ownership and does not
stop legacy services itself. It opens no motor bus.

## Ordering and deadlines

The coordinator publishes input-end, matched playback-start/end and turn
completion immediately. New input, interruption and privacy already have an
authority path. A cue cannot authorize itself: the parent publishes its
snapshot on priority control before sending its frame on the separate data
socket. The worker rereads control after receiving data. All phase publication
also runs in an audio-only session; that mode avoids SPI but is not identical
to the earlier control-message timing.

The policy retains one pending immutable frame. It creates a new phase-bound
Light permit, normally renews the static cue every 20 ms, and waits at most
100 ms for its matching receipt. This is lease maintenance, not an animation.
The original permit is clipped by actual input/authority leases. A heartbeat
cannot extend an already displayed frame's permit. A phase change can replace
the last cue after the outstanding receipt retires; it never relabels old work.

The writer's cooperative tick is 2 ms. Each tick shares a budget of 16 priority
control packets, retains at most one data frame and uses its original request
time. Data expires after 100 ms. An older heartbeat cannot suppress a newer
privacy revocation. A newer expired authority packet fails closed; it is not
silently ignored in favor of older allowed state. Malformed/unknown control
fields, mismatched lineage and feedback backpressure are faults.

The software qualification target is at most 50 ms from policy request to
write completion, with no microphone/speaker work waiting for SPI. A slow
receipt fails the qualification run. This boundary excludes phase recognition
before the request and physical light onset afterward. The release's visible
transition target therefore still needs a real-device measurement.

The ring worker has a 630-second total lifetime cap, covering the maximum
600-second active directed run plus startup/shutdown reserve. Authority expiry
remains independent and much shorter: Controller snapshots are bounded to
250 ms, and active input leases are 100 ms. Audio diagnostics retain their
separate 600-second wall/sample cap; diagnostic runs must leave startup room.

## Failure and cancellation

Light receipts are evidence; they cannot advance the conversation or revive
an old owner. The policy checks request identity, write ordering and the
original permit. An expected supersession rejects old work without erasing a
new valid cue. A bare delayed `off()` callback is never queued for a turn.

Revocation and expiry reach `GuardedRing` even with no new frame. Stop is sent
to every worker before waiting for any one worker, within the existing shared
300 ms cooperative grace (750 ms with diagnostics). A clean ring shutdown
requires an explicit black-write receipt whose timestamps follow that stop
request, plus successful process exit. A zero exit alone is insufficient.

SPI I/O remains in the child. Linux cannot promise a userspace deadline for a
stalled kernel SPI write; the existing 10 ms write budget is checked on return.
The coordinator detects faults and reaps children, but a crashed/killed writer
cannot itself blank a latched physical ring. A verified exclusive replacement
writer after reaping is still required for robust crash recovery. Startup
errors before any admitted turn use existing kill/reap cleanup; they do not
provide an explicit optical-darkness receipt.

## Verification without Lamp

The policy tests exercise real Controller transitions, stale and duplicate
feedback, phase changes, privacy, original leases and time bounds. Worker tests
use local sockets and fake sinks to cover cross-channel ordering, delayed
authority, cancellation, expiry, malformed controls, bounded service and both
primary/cleanup failures. The process probe additionally starts a separate
child with a memory-only sink:

```sh
cargo run --locked -p lamp-live --example ring_process_probe -- --samples 120
```

It uses synthetic conversation transitions, including simulated accepted
playback. Its JSON retains ordered samples, first/warm timing, startup and
shutdown evidence. It opens no microphone, speaker, SPI, camera, motor or cloud
connection. Host test and timing results do not establish Linux/ARM64 behavior,
physical darkness, color quality, speech/light synchronization or an end-to-end
voice improvement. Keep those results separate in qualification reports.

See [reuse decisions](reuse-decisions.md) for the V1 behavior retained and
changed, and [the ring driver](../crates/ring/README.md) for protocol and safety
boundaries.

### Host qualification, 2026-10-10

The source-only copy outside the parent repository passed **535 workspace
tests**, with zero failures or ignored tests, strict workspace/all-target
Clippy, formatting and a host release build of `lamp-live`. The Rust source
manifest SHA-256 is
`50cb160bc188b910b35117617448337265b2ab104f85c133c2b2b91772c91d97`.
Exact commands, logs, ordered timing samples and file hashes are retained
privately in `artifacts/ring-live-20261010-50cb160b/`.

| macOS process probe, development profile | Result |
|---|---|
| Samples | 120, synthetic phases and memory-only sink |
| Worker startup | 7.049 ms |
| First request to write completion | 2.342 ms |
| All-sample p50 / p95 / maximum | 1.758 / 3.177 / 3.391 ms |
| Warm p50 / p95 / maximum, 119 samples | 1.758 / 3.189 / 3.391 ms |
| Shutdown | Matching post-stop black-write receipt and successful child exit |

The local Linux VM did not remain running, so this slice has **no new Linux or
ARM64 qualification**. It has not been deployed. The earlier 518-test native
audio checkpoint does not cover this source. Real SPI duration, physical light
onset/off, startup/crash recovery, brightness and concurrency with actual audio
remain unmeasured. No before/after voice speedup follows from this probe.
