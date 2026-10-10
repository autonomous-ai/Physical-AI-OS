# Lamp ring driver

`lamp-ring` is a Rust library for the fixed 32-pixel WS2812 ring, not a complete
runtime or a running ring process. It adds no animation, filler, idle behavior,
privacy color or listening cue. Its caller must choose purposeful pixels and
hold an actual `lamp-interaction` light permit.

`lamp-live` now supplies an optional supervised writer and a phase policy for
directed qualification. See [conversation/ring ownership](../../docs/ring-choreography.md)
for its option, bounded scheduling, cancellation and remaining physical checks.

The codec emits exactly 1,028 bytes: 10 low primer bytes, 32 GRB pixels with
MSB-first bits encoded as `0xC0`/`0xFC`, and 250 low reset bytes. The policy passes
a validated channel ceiling of 0–120 (for example 40 at night). All channels
scale together, with nearest-integer quantization, to preserve hue.

The Linux transport opens only `/dev/spidev3.0`, obtains an advisory exclusive
lock, sets mode 0, 8 bits, MSB first and 6.4 MHz, and rejects differing
configuration readback. Returned settings describe the driver's accepted
configuration; they do not measure actual clock rate or optical output.
Legacy boot/shutdown LED services do not honor this lock and must be stopped or
excluded by deployment. Do not run competing ring writers.

`GuardedRing::show` encodes before checking `BoundaryGuard` with a fresh shared
`lamp-ipc` monotonic timestamp immediately before a single frame write. It
rechecks authority on return. `install_authority` applies revocations without
waiting for another paint; `tick` expires the displayed cue when no new work
arrives. Drain priority control messages before output and call `tick` at least
every 10 ms. The owner must refresh authority and policy; the driver never
manufactures readiness or permits from its last displayed color.

Rejected old permits or stale/duplicate snapshots preserve a newer valid cue.
The displayed cue's own expiry/revocation causes one black frame. Malformed
trusted control, transport loss or an I/O fault latches the writer fault and
blanks. Report both the original write error and any failed cleanup. There are
no automatic retries: one primary frame and at most one cleanup frame per fault.
`off` explicitly sends black; it is not a privacy/readiness status and does not
change controller permissions. `shutdown` is explicit and idempotent. Drop does
no I/O; after a crash the supervisor must arrange an exclusive replacement
writer before it can blank the hardware.

A frame's nominal wire duration is 1.285 ms at 6.4 MHz. The 10 ms write budget
is checked **after a write returns**. Linux spidev has no per-write userspace
timeout, so a stalled kernel call is not hard-bounded by this crate. Run the
writer in its own supervised process and enforce process-health deadlines
without blocking microphone capture or local speaker interruption. Bytes and
cleanup attempts are bounded; hard timing and optical response are unproven.

Dependencies are confined to lampOS path crates `lamp-interaction` and
`lamp-ipc`, plus Linux-only external Rust binding `spidev = 0.7.1`. The kernel
SPI driver, device node and permissions must be provisioned by lampOS. No HAL,
os-server, Python source, or sibling-repository configuration is required.

Validation commands after workspace inclusion:

```sh
cargo fmt --all -- --check
cargo test -p lamp-ring
cargo clippy -p lamp-ring --all-targets -- -D warnings
```

Tests cover all 256 channel encodings in all RGB positions, every ceiling
outside the permitted range, hue quantization, guarded cancellation/privacy,
idle expiry, stale-message preservation, short writes, fault cleanup and
shutdown. Linux-only tests also reject incorrect configuration readback. Unit
tests are not physical LED tests; device timing, visual brightness, ownership
handover and failure recovery still need qualification.
