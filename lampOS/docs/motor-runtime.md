# Motor protocol, calibration and read-only inspection

`lamp-motor` implements packet handling, calibration mathematics and a finite
Linux read-only inspection for Lamp's five Feetech STS3215 joints. There is no
register-write, torque, motion, service or shutdown actuator API. It does not
replace the movement runtime yet. No physical bus was opened for these batches;
no adapter open/close effects, position, clearance, load threshold or holding
behavior has been qualified on a Lamp.

Source: [motor crate](../crates/motor/src/lib.rs). The broader source audit is
[hardware contract](hardware-contract.md).

## Identities, packets, and bounded storage

| Bus ID | Typed joint |
|---|---|
| 1 | `BaseYaw` / `base_yaw` |
| 2 | `BasePitch` / `base_pitch` |
| 3 | `ElbowPitch` / `elbow_pitch` |
| 4 | `WristRoll` / `wrist_roll` |
| 5 | `WristPitch` / `wrist_pitch` |

`ReadOnlyRequest` exposes only `ping`, `read`, and `sync_read`. Sync reads accept
one to five distinct typed joints. A read window contains 1–244 bytes and must
not wrap the 8-bit register address space. Requests contain at most 13 bytes.
Known windows are model number (address 3, 2 bytes), position limits (9, 4),
homing offset plus operating mode (31, 3), and telemetry (56, 15).

```text
FF FF ID LENGTH INSTRUCTION PARAMETERS... CHECKSUM
LENGTH = parameter byte count + 2
CHECKSUM = !(wrapping sum of ID through final parameter)
```

The status error byte occupies the instruction byte's position. Words use
little-endian order. Ping is `01`; read is `02`; sync read is `82` with broadcast
ID `FE`, start address, byte count per joint, then joint IDs. A ping reply has no
model payload: request register 3 separately. The SDK table identifies STS3215
as model number 777; that number is source evidence, not this unit's readback.

`StatusParser` retains a fixed 250-byte array. Complete status frames are bounded
to 250 bytes, including the six framing bytes; the maximum payload is 244.
This intentionally corrects the legacy receive check, which compared its
250-byte maximum with the length field instead of the total frame length.
Partial chunks and multiple frames are handled without an internal output queue
or allocation. `feed` delivers every packet/error synchronously to a callback;
its report also counts discarded noise. The Linux inspection engine bounds input reads to 64 bytes and callbacks to
validated decode/report work. Other codec callers must bound their input and
callback work; this codec makes no wall-clock deadline claim.

Malformed lengths, broadcast/reserved status IDs, status bit 7, and checksum
failures are explicit errors. Resynchronization preserves possible following
frames. A corrupt but plausible long length can still retain later bytes as
payload until the caller's deadline: headers can legally appear inside payloads,
so the parser does not guess. `finish` reports truncation and clears retained
bytes. `reset` clears a parser explicitly and reports the discarded byte count.

The framing layer preserves other unicast IDs 0–253 for diagnostics, following
the audited SDK receive behavior. `ReplyTracker` accepts only requested Lamp
joints. It checks exact payload lengths, rejects duplicates, reports missing
joints at `finish`, and latches the first malformed/unexpected/device error.
Later packets cannot turn that failed transaction into success. All nonzero
servo error bits are faults, including unidentified low bits; multiple bits are
preserved. Known source bits are voltage `01`, angle `02`, overheat `04`,
electrical `08`, and overload `20`.

Pass parser events directly to `ReplyTracker::accept`. At an external stream end,
pass any `StatusParser::finish` error to the tracker before finishing the tracker.
The finite inspection engine instead aborts the entire pass on its first fault;
a deadline reports pending IDs and retained partial-byte count and the parser is
dropped. A successful earlier reply does not make a failed five-joint transaction
complete.

**Freshness remains a transport responsibility.** The protocol carries neither
a transaction sequence nor the requested register address in a reply. A delayed
reply with the same ID and length can match a newer request. Parser reset does
not drain a serial device. The inspection transport rejects observed prior traffic, applies monotonic
deadlines, records host acquisition times, and never pipelines or retries a
failed request. These checks cannot identify an old same-length reply that
arrives after a new request. A matched reply is not proof of freshness or sensor
authenticity; the report explicitly keeps freshness unproven.

## Register values and calibration units

`MatchedReply` associates a checksum/error/ID/length-checked reply with its
request. Register decoders also require the exact expected read window: a
same-length read from another address cannot be decoded as model or telemetry.

| Type/value | Meaning and restrictions |
|---|---|
| `EncoderWord` | Unqualified 16-bit present-position register; retains values outside single-turn range for diagnosis. |
| `EncoderCounts` | Validated 0–4095 coordinate, after hardware homing adjustment. Numeric range alone does not prove operating mode. |
| `NormalizedPosition` | Finite, dimensionless −100 to +100 coordinate. It is not degrees, velocity, or a safe motion envelope. |
| `HomingOffset` | Signed magnitude at register 31: bit 11 is sign; magnitude ≤2047. Upper bits 12–15 are rejected. Negative zero decodes to zero. |
| `RawVelocity` | Register 58, signed magnitude with sign bit 15; magnitude ≤32767. Physical speed conversion is unqualified. |
| `RawLoad` | Register 60, magnitude masked by `03FF`, direction bit `0400`. Preserve uninterpreted upper bits; no force conversion. |
| Other telemetry | Voltage 62, temperature 63, status 65, moving 66, current 69–70 remain raw. Register 65 is distinct from the packet's error byte. |

`RawTelemetry` retains all 15 bytes from registers 56–70, including gaps 64,
67, and 68. Neither load nor current is presented as measured contact force.
There is no degrees/second, voltage, temperature, current, or physical joint-zero
conversion in this batch.

`UnitCalibration::new` requires an explicit bounded unit ID and exactly five
records keyed by typed joint. It rejects repeated joints, ID/key mismatch,
`drive_mode` outside 0/1, homing offsets outside −2047..2047, ranges outside
0..4095, and empty/reversed spans. Before accessing conversions, `bind` requires
the independently supplied expected unit ID to match. This checks caller-supplied
identity consistency; it does not authenticate a device. There is no default
calibration, repository file search, or fallback to another unit's values.
Provisioning/file parsing is outside this crate and must preserve the
validation boundary without narrowing or truncating untrusted numeric fields.

Normalization matches the pinned legacy `RANGE_M100_100` arithmetic for valid
inputs:

```text
n = ((present_count - range_min) / (range_max - range_min)) * 200 - 100
if drive_mode == 1: n = -n

if drive_mode == 1: requested_n = -requested_n
count = truncate(((requested_n + 100) / 200) * span + range_min)
```

`Present_Position = Actual_Position - Homing_Offset` is the audited SDK's
coordinate convention. Present-position data is already adjusted in hardware;
normalization never subtracts the stored homing offset again. `drive_mode` is a
software sign reversal, not an independently readable servo setting. Inverse
mapping preserves the SDK's truncation toward zero for positive counts, so a
floating-point round trip can lose one count. Inverse conversion creates no
motion packet.

There is one deliberate stricter behavior: out-of-span observations are rejected
instead of clipped to apparently valid endpoints. Invalid/nonfinite normalized
values are rejected rather than silently clamped. This preserves faults for the
future controller to handle.

`JointCalibration::compare_registers` compares supplied limit and homing/mode
readbacks with calibration, requiring the same joint and position mode 0. It
checks numeric consistency only. Model/firmware identity, readback freshness,
physical zero/sign, safe spans, velocity/acceleration scales, collision clearance,
and stability still need independent qualification. An inherited calibration
span must not be presented as a verified movement envelope.

## Finite Linux inspection

The public API is `linux::InspectionConfig::new(device, unit_id, lock_directory)`
followed by `linux::inspect_once(&config, nonblocking_cancellation_callback)`.
All three arguments are required. Paths must be absolute UTF-8, at most 4096
bytes and 64 components, with no parent traversal. The unit identifier is a
validated caller label; it does not authenticate the adapter or servos. The
public API exposes neither a descriptor nor an arbitrary instruction writer.
It owns the port for one finite pass and always attempts explicit cleanup.

The CLI accepts only these three options, or `--help` alone:

```text
lamp-motor-inspect --device /dev/EXPLICIT_SERVO_ALIAS --unit-id PROVISIONED_UNIT --lock-directory /home/WORKER/private-motor-locks
```

This example is for a reviewed future hardware run, not an instruction to open
the current cabinet Lamp. `--help`, argument parsing and all current tests open
no serial device. The command has no default alias, calibration search, movement
mode or daemon mode. It outputs one bounded JSON report after closing the tty;
any preparation, transaction or cleanup fault produces a nonzero exit. SIGINT
and SIGTERM are consumed by a nonblocking signalfd in the single-threaded CLI;
cancellation is checked between operations. The library does not install signal
handlers or spawn processes.

The fixed plan is five individual pings (IDs 1 through 5), then four all-five
sync reads: model, position limits, homing/mode and telemetry. That is nine
requests and 25 status replies on success. Every reported model must be 777
before STS3215-specific decoding continues. Readback contains raw register units;
there is no new physical-unit claim or automatic calibration update.

| Boundary | Implemented bound / evidence |
|---|---|
| Preparation | Observed 200 ms fault deadline, with checks between operations. |
| Initial input | Observe 20 ms without readable bytes; maximum 200 ms, 4096 discarded bytes, 512 I/O operations. Continuous traffic aborts. |
| Transaction | 200 ms fault deadline and 512 I/O operations, at most 13 written bytes and 64 bytes per read. Partial I/O retains bounded state. |
| Poll / cancellation | Each requested poll wait is at most 2 ms; check cancellation and time before/after I/O. The callback must itself be nonblocking. |
| Telemetry target | All-five telemetry transaction at most 25 ms from transaction entry through final checked reply. This is an unverified target, not measured performance. |
| Retry | None. First fault terminates the complete inspection; no reuse of a timed-out parser, pending reply or partial request. |
| Storage | At most nine transaction reports and five replies each; bounded fixed parser and I/O arrays; error detail capped at 192 characters. |

Before every partial request write, already readable bytes abort the transaction
as prior traffic. Checksum/status/length/ID faults, duplicates, noise, unexpected
models, disconnects, missing IDs, partial replies at deadline and cancellation
are explicit failures. Bytes returned after the deadline are counted but never
decoded into an accepted reply. The initial quiet interval is only observed
silence; neither it nor parser reset proves absence of delayed traffic. An
aborted or partial transaction requires separate bus recovery review before
another run; there is no automatic recovery sequence.

Times are host monotonic microseconds relative to entry into `inspect_once`.
The report records tty open start/end, preparation completion, transaction
entry, first write attempt, final request-byte acceptance, first read completion,
each decoded reply's read completion, transaction end and cleanup end. Driver
write acceptance is not wire delivery, read completion is not a device sample
timestamp, and the five replies are not a simultaneous sample. Partial readbacks
from a failed transaction remain diagnostic observations. `completed` requires
all nine transactions and cleanup to succeed; freshness, authenticated unit
identity and hardware qualification remain explicitly false/unproven.

`O_NONBLOCK` and small buffers bound the application work; they cannot preempt
an open, ioctl, close or scheduler stall. A separately supervised process is
still required. No kernel hard deadline, 2 ms maximum real cancellation latency,
or 25 ms physical telemetry result is claimed.

## Exclusive access and tty lifecycle

Provision the lock directory before a reviewed run. Every ancestor must be owned
by root or the effective worker UID and must not be group/world writable. The
final directory must be mode 0700. Walking each directory descriptor uses
`O_NOFOLLOW`; symlink ancestors, shared `/tmp` paths and insecure permissions are
rejected. A private worker-owned path below its home, or a root-provisioned
private directory under `/run`, fits this policy. The library never chmods or
creates directory ancestors.

Before opening the tty, the owner acquires a nonblocking exclusive flock on a
0600 regular, singly linked lock file keyed by character-device major/minor in
that directory. The explicit device alias is resolved and its filesystem
identity recorded. The tty is opened with `O_NOCTTY | O_CLOEXEC | O_NONBLOCK`;
`fstat` must match the pre-open device/inode/rdev before any request. A second
nonblocking tty flock and `TIOCEXCL` protect cooperating owners and later ordinary
opens. The worker refuses effective `CAP_SYS_ADMIN`, which bypasses tty
exclusivity in Linux. These mechanisms cannot evict an already open foreign
handle; stopping and verifying all other owners remains an external prerequisite.
The report never claims it independently excluded preexisting foreign handles.
See [Linux tty open/exclusive handling](https://raw.githubusercontent.com/torvalds/linux/v6.12/drivers/tty/tty_io.c).

Serial settings are explicitly negotiated and read back: 1,000,000 baud, raw
8N1, no software/hardware flow control, `CREAD | CLOCAL`, `VMIN=1`, `VTIME=0`,
`HUPCL` clear. The inherited settings must have nonzero output baud, the normal
N_TTY line discipline and hardware flow control already disabled; otherwise the
inspection aborts without deliberately changing those modes. Complete prior and
negotiated settings are reported. The known control-character order is VINTR,
VQUIT, VERASE, VKILL, VEOF, VTIME, VMIN, VSWTC, VSTART, VSTOP, VSUSP, VEOL,
VREPRINT, VDISCARD, VWERASE, VLNEXT, VEOL2; unnamed ABI padding is not validated.

Normal cleanup restores the prior termios object and checks the visible fields,
**except it deliberately leaves HUPCL clear**. This intentional difference is
reported. Failed inspections make one `TCOFLUSH` attempt to discard unsent host
output; it is not a guarantee that USB/wire/servo queues are empty. Cleanup then
releases tty exclusivity, attempts tty close and releases the private lock. A
cleanup failure is reported and does not skip later cleanup steps. There is no
blocking `tcdrain`, serial retry, break, USB reset, DTR/RTS ioctl, torque release,
startup pose or goal write. Drop offers best-effort cleanup on unwinding;
SIGKILL, power loss or a stuck kernel call cannot guarantee restoration.

**An initial open/close is not electrically inert.** Kernel driver activation
can raise control lines before userspace reads termios. CDC ACM code also changes
DTR on transitions to/from baud B0 and sends USB line-coding updates. Keeping
HUPCL clear avoids requesting the usual hang-up-on-last-close behavior but cannot
qualify an unknown adapter, driver shutdown behavior or an early open failure
before valid settings are available. These are source observations from
[Linux CDC ACM](https://raw.githubusercontent.com/torvalds/linux/v6.12/drivers/usb/class/cdc-acm.c)
and [tty port lifecycle](https://github.com/torvalds/linux/blob/v6.12/drivers/tty/tty_port.c),
not measurements of the installed Lamp adapter. Read-only bus bytes alone do
not prove stable holding torque or safe placement.

## Movement boundary still to implement

The legacy shared five-servo bus ordinarily uses `/dev/ttyACM0` or the
`/dev/device-servo` role alias. These are audit references, not runtime defaults.
Hardware qualification must establish the actual adapter/open-close effects,
IDs, firmware, model, mode, calibration readbacks, telemetry timings and units.
The current cabinet safety overlay is unchanged; this batch grants no movement
or device-opening authorization.

A later movement runtime needs a single supervised bus owner, calibrated
placement/clearance, local range and speed/acceleration checks, measured physical
units, load fault handling, output ownership/cancellation and an explicit
validated holding/stop/shutdown policy. Legacy startup poses, release-on-cleanup
and goal writes that re-enable torque are not ported here.

## Dependencies

`serde` and `serde_json` use the standalone workspace's locked versions. Linux
uses `rustix = 1.1.5` with `fs`, `termios`, `event`, `process`, and `nix = 0.29.0`
with `signal` and default features disabled. These provide reviewed safe Rust
bindings; this crate contains no unsafe blocks or added serial dependency. No
legacy source, configuration, Python process or installed SDK is required at
build/runtime. Linux procfs is required for the bounded effective-capability
check. The private lock directory and device access must be provisioned for the
worker separately.

## Verification and source provenance

Host validation uses Rust 1.96.0 on macOS. Commands from the standalone `lampOS`
root:

```text
cargo +stable fmt -p lamp-motor -- --check
cargo +stable test -p lamp-motor --locked
cargo +stable clippy -p lamp-motor --all-targets --locked -- -D warnings
```

The host suite currently has 54 tests. The original 35 cover exhaustive register-window bounds,
all fragment splits of a golden reply, all chunk sizes of a maximum frame,
concatenated/out-of-order multi-servo replies, corrupt lengths/checksums/status,
truncation/reset, duplicate/unexpected/missing replies, all nonzero servo-error
combinations, long noise streams, all representable homing offsets and reserved
homing words, per-unit identity validation, sign reversal, no double homing
adjustment, strict observation spans, monotonic conversions, quantization bounds,
and raw register decoding. Seventeen transport tests additionally cover all
partial-write/read sizes, bounded continuous traffic, late and truncated replies,
clock regression, cancellation-source failure, cancellation immediately after
an accepted write, duplicates, wrong model, corruption/noise, missing servo,
zero/busy writes and the distinct telemetry target. Two CLI tests cover mandatory
arguments, forbidden controls and help. Three Linux-only pure policy tests are
present but not yet run on this macOS host.

Host tests, clippy and formatting do not compile the Linux-only backend. Only the
macOS standard-library target is installed here. Native Linux ARM64 compile,
clippy and pure tests are still required before a separate reviewed hardware run.
No test opens a serial device, changes torque, moves Lamp or establishes hardware
safety/latency. No physical use of this transport has occurred.

Observed sources, not runtime/build dependencies:

- Legacy autonomous-os commit `d5efe9d7b73cc529b34cd4abe97624682a82ca94`:
  `hal/follower/hal_follower.py:53–64` (joint identity and normalization mode),
  `hal/follower/config_hal_follower.py:26–94` (per-unit data and unsafe shared
  fallback), `hal/drivers/motors/overload.py:6–12` (load mask).
- Locally installed `feetech-servo-sdk` 1.0.0 source:
  `scservo_sdk/protocol_packet_handler.py:69–155,241–299,431–445` (framing,
  read/sync-read), `:17–22` (error bits), `scservo_def.py` and
  `packet_handler.py` (word order).
- Pinned LeRobot source commit `55198de096f46a8e0447a8795129dd9ee84c088c`:
  `src/lerobot/motors/feetech/tables.py:41–91` (registers),
  `src/lerobot/motors/feetech/feetech.py:283–294` (homing convention),
  `src/lerobot/motors/motors_bus.py:776–831` (normalization),
  `src/lerobot/utils/encoding_utils.py:16–36` (sign-magnitude).

These are source-level protocol/calibration observations. They do not establish
installed servo firmware, per-unit calibration correctness, or current cabinet
placement safety.

## Native compile checkpoint (2026-10-10)

The read-only transport source set
`ffa861b412380145c72ae0d54636c4130b90f29d5c01c31c2ba972fce52db93b`
was built in isolated source snapshot
`aec49f01943060d7c8f7a98b46282b86ab8e172f6a58a4dff08203f7b42723af`
on lamp-4ace's ARM64 Linux board. `cargo test --locked -p lamp-motor` passed
57 tests (0 failed, 0 ignored); package formatting, strict clippy with all targets,
and the release `lamp-motor-inspect` build also passed. This includes the three
Linux-only policy tests that were absent from the 54-test host run.

Exact commands, logs and identities are in `artifacts/motor-inspection-aec49f01/`.
The inspector was not invoked, no serial device was opened, and no physical
readback, latency, adapter lifecycle or movement result is established by these
checks. The current Lamp remains under its stationary motor inhibit.
