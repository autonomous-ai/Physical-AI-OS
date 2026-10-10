# Rust camera capture contract

`lamp-camera` owns privacy-controlled capture lifetime and bounded latest-frame
retention behind a provider-neutral `PortIo`. Its opt-in Linux V4L2 backend
implements explicit USB identity checks, MJPG negotiation and a bounded MMAP
queue. It has no process launcher, image decoder, vision inference, cloud image
input, light cue or motor operation. Synthetic tests do not qualify hardware,
optical freshness, privacy response time or real-time performance. The Linux
backend has not opened a physical camera.

## Local ownership and privacy

Construct `Capture::new(port, clock, controller_boot, worker_boot, config)` with
an explicitly provisioned source label. Construction allocates its buffers but
calls no port operation. A trusted fresh `Snapshot` must permit the camera
before an explicit `start`. The camera permission is independent of microphone
readiness and ordinary conversational turns.

The narrow interaction addition is
`BoundaryGuard::current_camera_grant(now)`. It checks the installed authority,
monotonic clock, lease and camera permission. It does not claim a current frame,
known person, gaze, addressability, or listening readiness.

The lifecycle is:

1. **Closed:** no port call until permitted explicit start.
2. **Acquiring:** the port reports compatible negotiation, but no current frame
   has been retained. Start alone never makes the camera ready.
3. **Ready:** at least one current pending or in-flight frame is retained under
   fresh authority. Ageing out or completing its delivery removes readiness
   until another current frame exists.
4. **Faulted:** authority loss, invalid trusted state, clock failure, capture
   fault, incompatible data or an observed operation overrun closes capture.
   Recovery requires a newly supervised worker incarnation; no retry or reset
   method revives this one.

Camera denial, unknown permission, or changed camera lineage clears all local
slots/tokens **before** calling stop. This also catches a close/reopen interval
coalesced into a single allowed snapshot. Privacy reopening and ordinary stop
require explicit start and a new capture epoch. A stale, duplicate, wrong-boot
or future snapshot is rejected without replacing newer valid authority. A fresh
heartbeat arriving after an active authority lease has already expired cannot
hide that control gap and revive capture.

Each port read is preceded by a current grant check and followed by another
check. The returned observation keeps the **original** grant. Bytes read during
an expired lease are discarded; a later allowed grant cannot relabel them.
`FrameObservation` retains the grant, worker boot, capture epoch, frame sequence,
source, negotiated mode and timestamps through the delivery boundary. A delayed
old delivery completion cannot release a newer in-flight frame.

The `lamp-live` camera worker drains priority control before polling, again after a
port call returns, and before metadata handoff. These synchronous methods do
not process concurrently arriving control messages themselves. Finite
`maintain` calls remain necessary when no consumer wants images. Explicit
`control_lost` invalidates all data and latches a fault. Final consumers must
recheck the original grant and frame identity; camera-derived actuator output
must keep that grant through the interaction owner's final output boundary.
Already exported bytes or cloud requests cannot be retracted by this library.

## Bounded storage and nonblocking delivery

There are exactly three construction-time storage slots: one read scratch
buffer, one newest pending frame, and one in-flight handoff. Each configured
capacity is 4 bytes through 2 MiB, so application frame storage is at most 6 MiB.
Poll and handoff swap these buffers without frame-sized allocation or cloning.
The slice passed to the port is capped by the validated negotiated `size_image`.
This limit excludes driver/kernel buffers. The Linux backend validates all
reported buffer counts and lengths before mapping them.

`poll` performs at most one nonblocking port read. A new frame replaces only the
pending slot, counting the replacement. A slow consumer cannot create a queue
or block acquisition. `begin_delivery` returns an opaque non-cloneable token for
one in-flight frame. `delivery_view(&token)` provides a borrowed byte slice plus
its original observation; `finish_delivery(token)` releases only that token's
slot. A second handoff reports busy. Completion means the bounded handoff
finished, not that network delivery or inference finished.

Do not wait, infer, or perform network I/O while borrowing frame bytes: that
borrow excludes maintenance. Any future image consumer must copy to an independently
bounded nonblocking transport, release the borrow, and continue priority control.
The inspection worker publishes metadata only and never copies image bytes to IPC.
The library cannot prevent a consumer deliberately copying bytes outside its
bounds; that receiver needs its own size, queue, age and privacy enforcement.
Raw JPEG frames do not fit the existing small control datagram contract and are
not added to it by this slice.

Pending and in-flight frames age out after 250 ms of **host dequeue age**. Slots
are cleared on replacement, completion, ageing, stop and faults. Clearing is
local retention hygiene, not a claim of cryptographic erasure. Counters record
received frames, pending replacement, ageing, invalidation, completed handoffs
and driver sequence gaps. Diagnostic counters saturate rather than wrapping;
frame/capture identity exhaustion faults.

## Explicit negotiation and timestamp meaning

The first codec contract is MJPG only. Configuration requires a source label,
exact dimensions (up to 1280x720), an optional rational frame interval, a frame
byte ceiling, and 2–4 requested buffers. Actual negotiation must report the same
source and dimensions, MJPG, nonzero bounded `size_image`, and 2 through the
requested number of buffers. If an interval was explicitly requested, an actual
equivalent rational interval must be reported. If no interval was requested,
unknown cadence remains unknown. No FPS is invented from a preview setting.
The source is a checked label, **not authenticated hardware identity**.

Every port result must carry the capture ID assigned at start. The port must
clear earlier capture queues instead of relabeling their bytes. Wrong source,
old capture, impossible byte length, non-JPEG SOI/EOI envelope, repeated/backward
driver sequence, or incompatible metadata faults. SOI/EOI validation is only an
envelope check; there is no JPEG decoding or claim that the scene is visible.
Driver sequence wrap uses forward serial-number order; observed gaps are counted.

`FrameObservation.dequeue` records host times immediately around the port call.
`PollReport.completed_at` records the final host check after retention work.
Neither is an exposure timestamp. Optional driver timestamps preserve:

- clock domain: shared host monotonic, realtime, or unknown;
- timestamp point: start of exposure, end of frame, or unknown;
- original microsecond value.

Only an explicitly declared shared monotonic driver clock is compared with host
time. Future, pre-start, regressing and (for a known timestamp point) excessively
old values fail closed. Realtime jumps and unknown timestamp values are recorded
without interpreting them as host age. Missing/unknown timestamp meaning stays
unknown; a current host dequeue cannot prove a current scene. The Linux backend
preserves kernel timestamp flags, but actual device behavior
still needs qualification before making optical freshness claims.

## Timing targets and checked budgets

These are proposed targets and failure bounds, **not measured results**:

| Boundary | Target or bound |
|---|---|
| Worker control/maintenance polling | At most 2 ms between iterations |
| Locally received privacy revoke to discarded frames and completed stop | Target at most 20 ms; measure on the physical backend |
| Port read start through final retention check | Checked 10 ms application budget, clipped to authority expiry |
| Host dequeue completion to publication/handoff availability | Target p95 at most 10 ms; expose call and completion timestamps |
| Start port operation | Checked 100 ms application budget, clipped to authority expiry |
| Stop port operation | Checked 20 ms application budget |
| First current frame after start begins | Fault at 2 s, with authority continuously refreshed |
| Silence after the last accepted frame | Fault at 1 s, with authority continuously refreshed |
| Retained host dequeue age | Strictly less than 250 ms |

A budget is checked only after a method returns. There is no kernel preemption
or hard deadline guarantee. `PortIo` implementations must be bounded and
nonblocking, perform no inference or provider I/O, and respect their
`OperationBudget`. A supervised dedicated process must detect/terminate stalls
at the process boundary. Cleanup makes one stop attempt, reports stop/clock/overrun failures, and
never loops. Explicit stop surfaces errors; Drop clears application buffers and
relies on the owned port to release resources without restart. The Linux backend also owns the descriptor, cooperative lock and MMAP resources.
Its cleanup limitations are described below.

## Concrete Linux port

The public boundary is:

```text
linux::LinuxConfig::new(device_path, private_lock_directory, source, expected_usb)
linux::LinuxPort::new(config)                         // no I/O
port.cancellation() -> Cancellation                   // sticky emergency cancel
Capture::new(port, linux::SystemClock, ..., capture_config)
capture.port().diagnostics() -> &linux::Diagnostics   // immutable metadata only
```

`UsbIdentity` requires VID, PID, USB port topology, interface number and capture
index; an expected serial is optional. Values must come from provisioning or a
read-only inventory. There is no default camera or expected identity. The
configured path is canonicalized only during permitted start. The character
node's device/inode identity and its bounded sysfs USB ancestry are checked
before and after open. This detects ordinary alias/hotplug changes; it is not
hardware authentication. Root must separately stop competing camera owners.

A nonblocking cooperative lock is taken **before** opening the camera, using
its major/minor identity. The pre-existing lock directory must be owned by the
effective user, mode 0700, under root/current-user-owned ancestors with no group
or world write permissions. Its lock file must be singly linked, owned by that
user and mode 0600. For a root worker, use an explicitly provisioned private
location under `/run`, not another user's writable home. The camera opens with
`O_RDWR | O_NONBLOCK | O_CLOEXEC | O_NOCTTY | O_NOFOLLOW`; fstat rechecks the
selected node and a second cooperative descriptor lock is taken. These locks
do not exclude an unrelated program that ignores them. There are no USB power,
unbind/rebind, discovery, fallback or camera-reset operations.

The node must support single-planar video capture and streaming. Format setting
and readback must both report the exact requested MJPG dimensions, with a
bounded nonzero image size. Actual frame interval comes from `G_PARM`; a zero
rational or unsupported query remains unknown. `S_PARM` is used only for an
explicit requested interval when the driver declares interval control, followed
by another readback and rational equality check. No capture FPS is derived from
a preview rate. The initial five controls are read-only: exposure mode, exposure
absolute, gain, brightness and automatic white balance. Metadata preserves
control type, flags (including inactive/volatile), range, default, current value
or unsupported/unavailable status. No legacy gain or exposure number is applied.

Only one tiny function reads a raw C union: `capture_parameters` checks the
returned capture discriminator and copies integer fields into an owned value.
The unsafe allowance is scoped to that function. No raw union or pointer is
exported. The audited binding's QBUF/MMAP APIs still require caller ownership
rules; the private queue enforces those rules rather than using its high-level
queue abstraction.

### Buffer and failure boundaries

The driver is asked for 2–4 buffers and must return 2 through the requested
count. **Every** returned layout is checked before the first mapping: expected
index, unique offset, checked offset/length, and an image-sized length of at most
2 MiB. There are at most four mappings (8 MiB mapped bytes) plus the three
application slots (6 MiB). These are application/mapping ceilings, not a claim
about total RSS or memory a faulty kernel allocated before returning REQBUFS.

Each slot moves through mapped, queued and dequeued ownership. Only a successful
validated DQBUF permits a bounded copy from that slot; the mapped borrow ends
before its single QBUF. Each poll attempts one dequeue. EAGAIN returns no frame
without changing ownership or requeueing anything. Unknown index/length,
corrupt-buffer flag, timestamp change, copy/requeue fault, EIO, disconnect,
cancellation or deadline overrun is terminal for that port. Copied destination
bytes are discarded and there is no automatic retry, queue reset or stale-frame
replay. Driver sequence ordering remains enforced by the outer lifecycle.

Raw flags are retained in diagnostics. Timestamp clock/source masks are decoded
as values, including zero-valued EOF. Only explicit MONOTONIC maps to the host
clock; UNKNOWN and COPY stay unknown, never assumed realtime. Invalid masks,
negative or overflowing timevals and changing clock/source flags fault. Driver
time, host port-call time and downstream publication time remain distinct.

Explicit stop invalidates local frames first. The backend stops once, attempts
STREAMOFF for a touched queue (including partial startup), unmaps all process
mappings, and frees driver buffers when the stop/deadline permits it. It closes
the camera and lock descriptors even if STREAMOFF fails. STREAMOFF, REQBUFS and
explicit close errors remain separately available; close is never retried.
Repeated stop preserves the original cleanup evidence and performs no new I/O.
A port fault requires a new worker; normal privacy stop requires an explicit new
capture epoch. Cancellation is sticky and cannot authorize or reopen capture.

Upstream `PlaneMapping::drop` logs munmap errors without returning them. Failed
open/setup paths that unwind before the owned wrapper exists also use ordinary
FD drop. Their successful resource release cannot be reported as verified.
Explicit stop is the observable path; process exit is the final resource boundary.
Drop makes a single best-effort cleanup attempt, never restarts, and cannot report
errors to a caller. There is no claim that a kernel ioctl/close/unmap can be
preempted by an application deadline.

Budgets are capped to 100 ms for start, 10 ms per read and 20 ms for stop, with
checks before and after each bounded operation. Reported timings are host
observations, not kernel deadline guarantees. Cold UVC start may exceed the
current 100 ms bound; this must be measured rather than quietly relaxing it.
Construction/open/STREAMON alone never means camera or listening readiness.

### Finite privacy-supervised inspection

The Linux-only CLI entry is:

```text
lamp-live camera-inspect /run/lampOS/camera.json 10 /run/lampOS/camera-report-001.json
```

This is explicit operator invocation, **not** a service, boot behavior, camera
permission bypass or automatic test. It spawns exactly the existing physical
privacy worker and one camera worker. It does not spawn Gemini, audio, ring or
motor workers. The temporary coexistence check refuses active `hal`, `os-server`,
`led-boot` or `led-shutdown` systemd units; it changes no service. Unrelated camera
owners must also be excluded by provisioning: the cooperative lock cannot
exclude applications that ignore it. The privacy worker exclusively acquires
`/dev/gpiochip1` line 9 as an input with pull-up; electrical low means muted.
Its switch-to-camera wiring and the actual physical response still require
qualification. No camera is opened until this real observation path permits it.

Provision the JSON configuration as an absolute, nonsymlink, singly linked
regular file, owned by the worker user with no group/other permissions and at
most 8192 bytes. Unknown fields are rejected. In particular, there is no
`allowed` flag. All of these fields have explicit meaning:

| Field | Required provisioned value |
|---|---|
| `device` | Absolute camera alias/node; no discovery or default |
| `lock_directory` | Existing private directory satisfying the Linux lock policy above |
| `source` | Checked source label, at most 128 ASCII characters |
| `expected_usb.vendor`, `.product` | Nonzero USB VID/PID as JSON integers from inventory |
| `expected_usb.topology` | Actual USB port topology, not an inferred label |
| `expected_usb.interface_number`, `.capture_index` | Exact interface and capture index |
| `expected_usb.serial` | Expected serial, or null when no serial is provisioned |
| `width`, `height` | Exact requested MJPG dimensions within the library ceiling |
| `interval` | Null for observed cadence, or `{ "numerator": 1, "denominator": 30 }` for an explicit seconds/frame request |
| `max_frame_bytes` | 4 through 2,097,152 bytes; bounds each of three application slots |
| `buffers` | 2 through 4 requested driver buffers |

The parent hashes the bounded configuration bytes and passes that SHA256 to the
camera child. The child rereads and verifies those exact bytes before constructing
the closed port. This catches a changed configuration between the two reads;
it does not authenticate hardware. No legacy configuration or state is read.
Do not substitute documentation examples for device inventory/calibration.

Worker startup `Ready` means only that the closed control loop is initialized.
Physical permission must be fresh within 50 ms and stable for 60 ms before the
parent sends camera authority followed by an explicit start on the same control
FIFO. The parent waits at most three seconds for that permission, in addition
to the existing finite child-handshake bounds. It leaves microphone permission
unknown and never claims listening readiness. Inspection runs for 1–60 seconds
starting when capture is requested, then revokes and stops. A stop, switch
revoke, privacy-worker loss or expired observation ends the run; it never reopens
or starts a second capture epoch automatically.

The camera loop services one shared budget of 16 priority control messages per
iteration, before port work, after return and before final handoff. Exhaustion
defers reads/handoff; a saturated control slice during start fails without
publishing start success. One original delivery token can be retained, in
addition to the library's newest pending slot. Duplicate/stale snapshots cannot
close newer valid capture or renew an old heartbeat. Malformed state, expired
control, read/start overrun or metadata transport failure invalidates retained
frames, attempts bounded cleanup and exits. A full metadata socket is an explicit
inspection failure; no hidden queue or silent image loss is introduced.

Only frame metadata enters IPC. Each metadata record retains the original
`CameraGrant`, worker/capture/frame identities, mode, byte count, driver sequence,
optional driver timestamp domain/point, and host dequeue/handoff times. The
worker's final delivery check runs under that original grant. The parent drains
physical privacy again after receiving a record and compares the original grant
with the currently permitted one before accepting it. A newly opened privacy
lineage cannot relabel an older frame. During shutdown, queued frame metadata is
discarded rather than counted as newly accepted; only stop/cleanup evidence is
retained. No JPEG is saved, transmitted, decoded or hashed.

Progress is emitted at most once per 10 ms, with a 30 ms parent watchdog based on
**producer monotonic time**, not receipt of an old queued packet. Start announces
its separate 100 ms checked budget; progress cannot extend an unfinished start.
The parent and worker use a 2 ms cooperative tick. Revocation is sent immediately,
and an unresponsive camera is killed/reaped after a 20 ms observed shutdown
window; the privacy worker has a 100 ms shutdown window. Forced termination,
abnormal exit and missing stop evidence are failures. This bounds application
waiting and retry work; scheduling and kernel kill/wait/ioctl/close calls are not
preemptible hard guarantees. A read's checked 10 ms budget is still enforced by
the capture library on return. The watchdog is not a claim of 10 ms preemption.

The private new output file is reserved before hardware work. Final JSON writing
happens after worker cleanup. The report includes requested configuration and
digest, checked USB identity, negotiated mode, five read-only controls, mapped
buffer lengths, start/stop/error evidence, frame counts and sequence gaps.
It records the last accepted physical switch acquisition and receipt times,
the parent's observed permission-revocation time when applicable, stop request
and worker exit times. These expose local revoke timing without pretending
that a GPIO sample timestamp is the unobserved instant a person moved the switch. It
retains at most 16 bounded shutdown error messages (counting omitted errors),
the first 64 frame metadata records, counts later omitted samples, and
accumulates exact min/max/sum/count for host dequeue-to-worker and
dequeue-to-parent latency across accepted frames. These first samples are not a
representative percentile estimator. A completed inspection requires at least
one current accepted frame and successful observed cleanup; opening the camera
alone is insufficient. It proves neither visible scene content nor useful
exposure, gaze, addressee detection, hearing or safe movement.

Portable tests exercise actual worker intake with a synthetic `PortIo`, real
private datagrams and finite child-process fixtures: startup/dequeue/handoff
privacy races, parent privacy arriving between control/data reads, original
lineage after reopening, stale snapshots, partial control slices, metadata
backpressure, timing/lease faults, bounded reports, explicit config/digest
validation, and hung-child failure/reaping. They do not open physical GPIO or
camera devices. The finite executable remains unqualified on hardware until its
own native build and authorized stationary inspection pass. The prior library
qualification below does not qualify this new process integration.

## Legacy provenance

The inherited contract was inspected in legacy commit
`d5efe9d7b73cc529b34cd4abe97624682a82ca94`. It is source provenance, not a build or
runtime dependency:

- `robots/lamp/rootfs/etc/udev/rules.d/99-lamp-device.rules:10–18`: role
  `/dev/device-camera`, capture index 0, USB profiles `1bcf:28cc` and `01da:5875`.
- `robots/lamp/rootfs/opt/hal/.env:32–40,73–82`: requested 1280x720, auto exposure
  and gain 64; control values are device-specific inherited settings.
- `hal/drivers/camera/video_capture_device.py:515–543` and
  `hal/runtime.py:391–399`: MJPG/dimension requests and queried actual mode;
  normal startup does not supply a capture FPS request. The 10 FPS at
  `hal/routes/camera.py:204–207` is preview cadence.
- `hal/drivers/camera/video_capture_device.py:223–231,670–673,692–736`: the old
  publication timestamp follows callbacks; still capture may return an older
  latest frame on timeout and uses a motor freeze lease. Those semantics are
  not reused.
- `hal/drivers/camera/video_capture_device.py:28–97,398–458,499–508`: legacy
  discovery/fallback, USB power controls and reset paths. This slice contains
  none of those actions.
- `hal/privacy.py:24–85,100–135` and `robots/lamp/privacy_button.json`: privacy
  gates before teardown, startup closed until a valid physical switch reading.

Before hardware use, verify the actual device and mode, exposure/gain behavior,
driver buffer lengths, clock flags, stale-buffer behavior, disconnect/STREAMOFF
latency, physical privacy routing, competing owners and interaction with any
composite USB microphone. Opening a camera is not permission to move Lamp.

## Verification and dependencies

Portable lifecycle dependency: local `lamp-interaction`. Linux adds local
`lamp-ipc`, exact `rustix 1.1.5` (fs/process features), and exact `v4l2r 0.0.8`.
All Lamp-owned code is Rust. There is no Python, OpenCV, libcamera, FFmpeg,
libv4l2 C runtime, inference library or legacy HAL dependency.

The audited v4l2r archive SHA256 is
`67d06043afd259b7e3f0b70bb8cc590b35c99006abc595399956392b4ebbe717`, upstream commit
`d4684ccc0d6b880c36d100ed8c09f47d6adb35c7` (crate path `lib`). The source review
covered `src/ioctl/{qbuf,mmap,g_parm,reqbufs,querybuf,dqbuf,streamon}.rs`,
`src/ioctl.rs` and `build.rs`. Its build uses bindgen 0.72.1; native ARM64 requires
a Rust/C toolchain, Linux UAPI headers and libclang (for example Debian's
`linux-libc-dev` and `libclang-dev`, with normal libc development headers).
The binding also unconditionally references these AV1 UAPI declarations even
though Lamp does not use AV1:

- `v4l2_ctrl_av1_sequence`, `v4l2_ctrl_av1_frame`, `v4l2_ctrl_av1_film_grain`,
  `v4l2_ctrl_av1_tile_group_entry`;
- `V4L2_CID_STATELESS_AV1_SEQUENCE`, `V4L2_CID_STATELESS_AV1_FRAME`,
  `V4L2_CID_STATELESS_AV1_FILM_GRAIN`,
  `V4L2_CID_STATELESS_AV1_TILE_GROUP_ENTRY`;
- corresponding `p_av1_sequence`, `p_av1_frame`, `p_av1_film_grain` and
  `p_av1_tile_group_entry` members in `v4l2_ext_control`'s payload union.

Lamp's initial inspected build environment lacked the required AV1 declarations
and libclang; the native qualification below records the resolved build inputs. A self-contained, build-only [Linux UAPI bundle](../vendor/linux-uapi-v6.12-adc218676eef/README.md)
now supplies the three media headers exported from Linux v6.12 commit
`adc218676eef25575469234709c2d87185ca223a` (255,190 bytes). Original notices and
license texts are retained. The pinned export is build input only; there is no
running-kernel or system-header replacement and no AV1 camera operation.

The root `.cargo/config.toml` forces `V4L2R_VIDEODEV2_H_PATH` to the bundle's
**include root**, using a Cargo-relative path. The upstream wrapper includes
`<linux/videodev2.h>` and watches `videodev2.h` at that root; a regular forwarding
header supplies the watch path. The camera build script checks the selected
canonical path and all four header lengths/SHA256 values using the already-locked
Rust `sha2 0.10.9` build dependency. Missing, tampered or redirected inputs fail
the build instead of silently accepting an incomplete bundle. Normal builds do
not fetch headers, run their export tools or open hardware.

Build from the lampOS root so Cargo loads its configuration; a `--manifest-path`
invocation from an unrelated directory is insufficient. No
`BINDGEN_EXTRA_CLANG_ARGS` or global include-path override is needed. Keep such
header-overriding options unset. Target libc and base Linux UAPI definitions
remain explicit toolchain dependencies (`sys/time.h`, `linux/{types,ioctl,const}.h`
and their architecture-specific includes). Native bindgen still needs a
compatible libclang shared library, version 9 or newer, plus its resource
headers. Installing a compiler executable alone is insufficient; ordinary
bindgen does not require that executable if the shared library is available.
Record exact native toolchain/package versions when qualifying the build.
Leave v4l2r `arch32` and `arch64` features OFF for ARM64: those features hardcode
i686/x86_64 bindgen targets.

The bundle is not a complete cross-compilation sysroot. Before opening a camera,
run native source-only tests/clippy and compare the existing capture ioctl
numbers, relevant struct layouts and timestamp constants with the installed
UAPI. Newer definitions do not prove older-kernel compatibility. The actual
camera mode, clock behavior, cleanup and privacy latency remain unverified.
No device is needed for this build qualification. Regeneration pins, output
digests, license handling and the older-binding comparison are recorded with
the bundle. v4l2r 0.0.6 avoids AV1 but still requires libclang, changes REQBUFS's
signature and predates later cache-flag/QBUF fixes; 0.0.7 still requires AV1.
The camera runtime remains on the reviewed 0.0.8 API.
Primary references: the [pinned binding source](https://github.com/Gnurou/v4l2r/tree/d4684ccc0d6b880c36d100ed8c09f47d6adb35c7/lib),
Linux [queue ownership and DQBUF errors](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-qbuf.html),
[STREAMOFF and failed STREAMON](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-streamon.html),
[returned buffer counts](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-reqbufs.html),
and [actual frame intervals](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-g-parm.html).

Run from the lampOS root:

```text
cargo +stable test --locked -p lamp-camera -p lamp-interaction
cargo +stable clippy --locked -p lamp-camera -p lamp-interaction --all-targets -- -D warnings
cargo +stable fmt -p lamp-camera -p lamp-interaction -- --check
cargo +stable test --locked -p lamp-live
cargo +stable clippy --locked -p lamp-live --all-targets -- -D warnings
cargo +stable fmt -p lamp-live -- --check
```

Synthetic regressions cover closed startup, no readiness without a first frame,
privacy races and coalesced reopening, stale authority and late delivery tokens,
expiry during reads, late heartbeat control gaps, initial/sustained frame
failure, clock and operation overruns, queue pressure, bounded slot reuse,
negotiation/size/source errors, sequence wrap and truthful timestamp domains.
Portable backend regressions additionally cover partial startup failure,
pre-mapping bounds, EAGAIN without duplicate QBUF, malformed dequeue results,
cancellation during copy/dequeue, checked deadlines, first-fault shutdown,
STREAMOFF/cleanup failures and timestamp mask semantics. Linux-only synthetic
tests cover the checked union, explicit identity/configuration and construction
without device access. Host tests do not compile the Linux adapter; isolated
native ARM64 compilation/test/clippy is required before any physical inspection.
There have been no camera device opens or camera hardware measurements for this
library slice. The finite `lamp-live camera-inspect` integration described above
is separate and remains a metadata-only qualifier, not a full vision runtime.


## Native build qualification, 2026-10-10

The isolated camera source `ec98fb1dad014fe2c1f1f2bee5c9d697f5d1ade7a624ccbf39aee42eaef00ce8`
now passes on Lamp's Linux ARM64 board: 58 camera tests plus 37 interaction tests,
strict camera Clippy with all targets, workspace formatting, and a release
library build. These are synthetic lifecycle/adapter tests and compilation;
no camera device was opened by this qualification. Source, exact commands,
logs and build identity are retained in `artifacts/native-camera-ec98fb1d/`.

The build added only `libclang1-14`, `libllvm14`, and `libclang-common-14-dev`, all
version `1:14.0.6-12`; no packages were upgraded or removed. Bindgen found the
shared library and resource headers. Existing target packages were
`libc6-dev 2.36-9+deb12u13` and `linux-libc-dev 6.1.158-1`; the C compiler reported
GCC 12.2.0. The vendored media headers remained confined to this build's include
path. No kernel or installed system header was replaced.

A separate compile-only probe compared installed and pinned headers on the
board: 11 structure sizes/alignments, 18 selected field offsets, and 17 ioctl or
format/queue constants matched exactly. These selected ABI checks support
compiling the existing capture operations against the bundle; they do not prove
camera driver behavior, optical freshness, frame quality, privacy timing, or
vision-assisted attention. Physical camera qualification remains pending.
