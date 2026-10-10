# Lamp hardware port contract

Source audit recorded 2026-10-10 for the first lampOS live-interaction release.
This is a reference for a Rust implementation, not evidence that a Rust driver
exists or that any posture, limit, latency, or room placement has passed a device
test. **This audit performed no SSH, hardware reads, motion, or deployment.**

## Evidence and independence

- **Legacy source observed:** `autonomous-ai/autonomous-os` commit
  `d5efe9d7b73cc529b34cd4abe97624682a82ca94`. Paths and line numbers below refer to
  that revision unless marked SDK. They are provenance identifiers, not imports,
  required local files, or links outside this project.
- **Local SDK source observed:** `feetech-servo-sdk` 1.0.0 and `lerobot` 0.3.4 in an
  existing development environment. Their code explains the legacy conversions
  and packet format; it is not a reading of the installed Lamp firmware or a
  qualification of these libraries on lamp-4ace.
- **Device-verified by this audit:** none. Device identity, wiring, enabled
  modules, calibration, register values and negotiated media modes must be
  confirmed during the implementation's authorized hardware qualification.
- lampOS must package its own Rust implementations and validated configuration.
  It must not require the legacy checkout, Python HAL, or these local SDK files
  to build or run. Per-unit calibration is device data to provision explicitly,
  not a reason to depend on the old runtime.

Source constants describe prior behavior. Inherited numeric limits are not
newly qualified safety limits. The cabinet edge, clearance, balance and safe
movement envelope have **not** been verified here.

## Microphone update

The later [microphone topology and qualification note](microphone-topology.md)
separates the team-reported three-capsule layout from the two host capture
interfaces and unverified hardware DSP features. Its dated device observation
is separate from this original source-only audit.

## Ring

| Item | Source-observed contract |
|---|---|
| Pixels | 32 WS2812 pixels, not the generic RGB service's default of 64. |
| Transport | Orange Pi `/dev/spidev3.0`, SPI mode 0, 6,400,000 Hz, 8-bit words. |
| Frame | Ten zero primer bytes, 32 encoded pixels, 250 zero reset bytes: 1,028 bytes total. |
| Pixel encoding | GRB order, each channel MSB first. Encode each source bit as one SPI byte: zero `0xC0`, one `0xFC`. |
| Brightness | Driver scales channels by its configured brightness fraction before encoding. Legacy final policy scales all RGB channels together to preserve hue while limiting their maximum. |
| Inherited ceiling | Channel maximum 120 normally, 40 during 22:00–07:00 in the device's policy clock. These are ceilings, not recommended conversational brightness. |
| Shutdown | Refuse late paint commands before blanking and closing the device. Legacy clearing sends black twice, 10 ms apart, then an additional low SPI transfer. |
| Observation | The driver's pixel readback is only its memory buffer. It does not confirm physical light output. |

Provenance: `robots/lamp/presets.json:4`, `hal/board/boards.json`,
`hal/drivers/rgb/rgb_service.py:82–132,207–238,250–307`,
`hal/safety/policy.py:226–256`, `robots/lamp/SAFETY.md`.

**Ownership hazard:** legacy `led-boot.service` and `led-shutdown.service` also
write SPI. `robots/lamp/rootfs/usr/local/libexec/led-off.py:38–43` checks only
whether HAL is inactive, not whether lampOS owns the ring. The deployment and
shutdown sequence must prevent these writers from overlapping the Rust owner.
A successful SPI write is not optical verification.

## Five servos, one bus owner

The legacy follower declares five STS3215 servos:

| ID | Joint |
|---:|---|
| 1 | `base_yaw` |
| 2 | `base_pitch` |
| 3 | `elbow_pitch` |
| 4 | `wrist_roll` |
| 5 | `wrist_pitch` |

Default serial path is `/dev/ttyACM0`. The Lamp udev profile also defines
`/dev/device-servo` for USB VID:PID `1a86:55d3`. Legacy transport is 1,000,000 baud,
8 data bits, no parity, one stop bit. One process must serialize all reads,
writes, pings, configuration and emergency handling on this shared bus.

Provenance: `hal/config.py:8`, `hal/follower/hal_follower.py:53–64`,
`robots/lamp/rootfs/etc/udev/rules.d/99-lamp-device.rules:21`; SDK
`lerobot/motors/feetech/feetech.py:36–40,120–135`,
`scservo_sdk/port_handler.py:91–108`.

### Packet format

```text
FF FF ID LENGTH INSTRUCTION PARAMETERS... CHECKSUM
LENGTH   = number_of_parameter_bytes + 2
CHECKSUM = bitwise_not(sum(ID through last parameter)) & FF
```

A status packet places its error byte where an instruction packet places the
instruction. Register words are little-endian for this STS3215 configuration.
SDK `PacketHandler(0)` chooses byte order; it is not evidence of a separate
packet framing version.

| Instruction | Value | Parameters |
|---|---:|---|
| Ping | `01` | None |
| Read | `02` | Register, byte count |
| Write | `03` | Register, data bytes |
| Sync read | `82` | Register, bytes per servo, servo IDs |
| Sync write | `83` | Register, bytes per servo, repeated ID/data tuples |

Broadcast ID is `FE`. Validate the packet length, expected ID, checksum and
servo error flags. Keep incomplete reads and timeouts explicit. Sync write has
no individual status acknowledgment; successful transmission alone does not
prove that every joint accepted its target. Use bounded, monotonic deadlines;
the legacy SDK uses a patched timeout and wall-clock timing, which are not
requirements for the Rust implementation.

Provenance: SDK `scservo_sdk/protocol_packet_handler.py:69–155,241–299,344–460`,
`scservo_sdk/scservo_def.py`, `scservo_sdk/packet_handler.py`,
`lerobot/motors/feetech/feetech.py:86–98`.

### Registers and units

| Register | Address | Size | Interpretation observed in source |
|---|---:|---:|---|
| Homing offset | 31 | 2 | Sign-magnitude, sign bit 11 |
| Operating mode | 33 | 1 | Position mode is 0 |
| Torque enable | 40 | 1 | 0 off, 1 on |
| Acceleration | 41 | 1 | Raw device setting; physical scaling not qualified here |
| Goal position | 42 | 2 | Encoder coordinate after hardware homing offset |
| Goal time | 44 | 2 | Raw device setting; not qualified here |
| Goal velocity | 46 | 2 | Sign-magnitude, sign bit 15; zero can remove the speed cap |
| Torque limit | 48 | 2 | Legacy limits use 0.1% drive units |
| Present position | 56 | 2 | Encoder coordinate after hardware homing offset |
| Present velocity | 58 | 2 | Sign-magnitude, sign bit 15 |
| Present load | 60 | 2 | Magnitude `raw & 0x3FF`, 0.1% drive duty; bit 10 is direction |
| Voltage / temperature | 62 / 63 | 1 each | Raw registers; conversions require hardware qualification |
| Status / moving | 65 / 66 | 1 each | Device flags |
| Present current | 69 | 2 | Raw register; not calibrated force evidence |

Provenance: SDK `lerobot/motors/feetech/tables.py:41–91,206–217`;
`hal/drivers/motors/overload.py:6–12`,
`hal/drivers/motors/animation_service.py:209–229`.

### Calibration is per unit; normalized positions are not degrees

Legacy calibration records contain `id`, `drive_mode`, `homing_offset`,
`range_min` and `range_max`. The persistent legacy location defaults to
`/var/lib/hal/calibration/robots/hal_follower/<HAL_DEVICE_ID>.json`, optionally
overridden by `HAL_CALIBRATION_DIR`. The source explicitly warns that spline
mounting varies between units. Its fallback to repository `hal.json` must not
be treated as valid calibration for another Lamp.

The follower defaults to `RANGE_M100_100`, with `use_degrees = false`:

```text
n = 200 * (clamp(raw, range_min, range_max) - range_min)
          / (range_max - range_min) - 100
if drive_mode is set: n = -n

if drive_mode is set: requested_n = -requested_n
raw_goal = truncate((clamp(requested_n, -100, 100) + 100) / 200
                    * (range_max - range_min) + range_min)
```

Present position already incorporates the servo's homing offset; do not
subtract that offset a second time. Validate finite targets, joint identity,
nonzero calibration spans and the actual hardware calibration before any
write. A raw range is not sufficient evidence of collision clearance or balance.

**Known unit inconsistencies in the legacy call path:**

- `recording_timing.py:38–43` and `safety/policy.py:293–305` calculate a
  degrees-per-second limit directly from these position values.
- `animation_service.py:1242–1253` multiplies normalized goal/present difference
  by ten and labels it tenths of a degree.
- `recording_stability.py:141–152` feeds recording values directly into
  `radians()` for its center-of-gravity calculation.

An explicit per-joint conversion and physical zero/sign mapping are therefore
required before accepting any speed, lag, or balance result. The local SDK's
optional degrees mode uses `(raw - midpoint) * 360 / 4095`, but that formula
alone does not establish the assembled robot's joint zero or safe envelope.

Provenance: `hal/follower/config_hal_follower.py:26–94`,
`hal/follower/hal_follower.py:53`; SDK `lerobot/motors/motors_bus.py:776–831`,
`lerobot/motors/feetech/feetech.py:286`.

### Startup, halt, overload and shutdown

- Normal legacy startup disables torque while configuring, then re-enables it
  and queues a move to hard-coded raw wake coordinates. This is **not** a safe
  no-motion handover procedure for the cabinet unit.
- Normal legacy animation-service disconnect deliberately retains torque.
  `halt()` stops queued motion and attempts to pin the measured current pose
  with torque on. An ordinary process exit must not silently become a release.
- Explicit legacy `release()` attempts a raw park move, then removes torque
  even if the park move failed. Do not import it as generic cleanup.
- Goal writes can re-engage torque. Block them at the final bus boundary while
  a stop, fault, privacy gate or canceled ownership prevents movement.

The old profile caps base yaw/base pitch/elbow drive at 600 (60%). Its fixed
contact thresholds are yaw550, wrist roll750, wrist pitch650, held for 50 ms;
contact causes halt/hold with a 3 s pause. Overload is load 800 for 1.5 s, followed
by torque removal and 120 s lockout. The monitor runs every 50 ms and retries
failed torque-off writes. Learned contact data lives at the legacy
`/var/lib/hal/contact_profile.json` and is specific to calibration/recordings.
Load is motor drive duty, **not measured contact force**. These inherited
thresholds and recovery motions are not qualified for lamp-4ace by this audit.

The declared legacy speed ceiling is 120 degrees/s and CoG ceiling 22 mm, but the
unit inconsistencies above prevent using those declarations as proof of safety.
Its CoG check also passes through when geometry is missing. A Rust safety
boundary must have explicit unavailable-data behavior.

Provenance: `hal/drivers/motors/animation_service.py:25–46,102–106,258–349,
717–730,992–1015,1110–1192`, `robots/lamp/servo_overload.json`,
`hal/config.py:843–847`, `hal/drivers/motors/recording_stability.py:178–219`.

## Camera

Legacy Lamp selects OpenCV over V4L2. The transport can be owned directly by a
Rust process without retaining OpenCV/Python orchestration.

| Item | Source-observed contract |
|---|---|
| Identity | Preferred role path `/dev/device-camera`, capture index 0. Udev profiles cover USB `1bcf:28cc` and `01da:5875`. |
| Requested mode | MJPEG 1280x720, auto exposure, gain 64 from the Lamp profile. Record negotiated dimensions, format and FPS; requests are not actual mode evidence. |
| Controls | Legacy V4L2 exposure values: auto 3, manual 1. Manual exposure/gain settings are device-specific, not universal brightness controls. |
| Freshness | Preserve acquisition timestamp and sequence independently of inference completion. Invalidate retained frames on privacy stop. |
| Stop | Close acquisition independently of slow inference; do not mark a stopped or stale worker ready. |

Legacy `capture_still()` returns the latest frame on timeout even if it failed
the freshness test. Do not claim freshness using that fallback. Legacy camera
recovery can unbind/rebind USB; qualify composite-device effects before using
it because a camera may share its USB device with a microphone. Do not silently
switch to an unrelated camera when a configured role device disappears.

Provenance: `hal/drivers/camera/video_capture_device.py:28–97,260–299,
490–543,692–736`, `robots/lamp/rootfs/opt/hal/.env:32–40,79–82`,
`robots/lamp/rootfs/etc/udev/rules.d/99-lamp-device.rules:10–18`.

## Physical privacy, buttons and touch

All GPIO line numbers below are chip-relative offsets, not header pin numbers.
Resolve the installed device profile before claiming lines. The Lamp override
avoids the apparent legacy board-default collision between touch and reset.

| Input | Device-profile wiring and behavior |
|---|---|
| Privacy slide switch | GPIO chip 1/line 9, pull-up, active-low mute. Settle 60 ms, watchdog 30 s. Mute locks microphone, speaker and camera. |
| Primary mechanical button | GPIO chip 0/line 99, pull-up/active-low, debounce 200 ms, `factory_reset:false`. |
| Dedicated reset button | GPIO chip 0/line 100, pull-up/active-low, debounce 200 ms; reset hold 5 s. Do not replay or synthesize destructive button actions during tests. |
| TTP223 pads | GPIO chip 0, lines [37,96,97,98], active-low. This replaces the legacy board default [96,100]. Candidate detection uses a weak pull-down for 10 ms, then both-edge/pull-up input. Only configured candidate lines may be probed. |
| MPR121 | `/dev/i2c-0`, address 0x5A, electrodes 0–11. This is an independent capacitive controller, not the TTP223 GPIO transport. |

Privacy gates must be active before the first capture/playback operation until
an authoritative initial switch read permits access. A failed initial read
must retain mute. Publish gates before slow teardown; queued audio and images
must become unusable immediately. A physical-open pin permits access but must
not repeatedly override a software mute or restored sleep. Button/touch wake
cannot override physical privacy. Startup-held inputs or orphan release edges
must not trigger gestures.

Legacy shared button timing is 400 ms multi-click, 2 s sleep, 5 s shutdown,
10 s factory reset, with profile-specific exceptions above. These are existing
behavior references, not a decision to carry all such UX into V2. TTP223's
source describes FastMode pulses and disables destructive hold gestures; do not
interpret that input as a reliable physical hold without confirming hardware.

Provenance: `robots/lamp/{privacy_button,gpio_button,ttp223,mpr121}.json`,
`hal/board/ttp223.py:18–51`, `hal/board/gpio_button.py:69–74`,
`hal/drivers/privacy_button.py:62–97,134–214`, `hal/privacy.py:24–85`,
`hal/drivers/button_gestures.py`, `hal/drivers/ttp223.py:1–59,91–160`.

### MPR121 initialization and reads

Use Linux repeated-start register transactions (`I2C_RDWR`). Register writes
are `[register,value]`; reads write the register pointer then read in one
combined transfer. Reject incomplete transfers.

The current Lamp profile initializes as follows (all register/value pairs hex):

1. `80=63` soft reset; wait 1 ms. `5E=00` stops electrodes. Read `5D`; require 0x24.
2. For each electrode 0–11: write touch threshold 0x06 at `41+2*electrode`,
   release threshold 0x04 at `42+2*electrode`.
3. Filter registers: `2B=01, 2C=01, 2D=0E, 2E=00, 2F=01, 30=01, 31=FF,
   32=02, 33=00, 34=00, 35=00`.
4. `5B=22` chip debounce, `5C=D0` FFI 34/16 uA,
   `5D=30` CDT 0.5 us/SFI 10/ESI 1 ms.
5. Autoconfiguration: `7D=C8, 7F=B4, 7E=82, 7B=CB`. Finally `5E=8F`.

Read two bytes at register 0x00, little-endian: bits 0–11 are electrode state;
bit 15 is an over-current fault. Poll 10 ms, software debounce 10 ms; seed an
initial held state without emitting a tap. Current profile requires three
electrodes for a palm tap, uses swipe axis 11 through 0, and maps a qualified 2 s
hold to voice-input-mode toggle. MPR121 must not trigger factory reset.

Provenance: `hal/drivers/mpr121.py:31–66,104–179,410–419,487–537`,
`hal/board/mpr121.py:10–12`, `robots/lamp/mpr121.json`.

## Environmental sensors and shared I2C

The **base Lamp profile disables SCD41, SEN55 and SEN63C**. Pro profile overrides
enable SEN63C. A supported driver is not proof that the sensor is installed.
Start only explicitly configured modules; do not scan/probe all bus addresses.

Each sensor has one acquisition owner. Separate processes may share the Linux
adapter's serialized transactions at different addresses, but shared bus-clock
configuration has one coordinator. MPR121 may share I2C 0 with an environmental
sensor; do not reset or reconfigure the adapter casually.

Commands are big-endian 16-bit values. Responses contain each big-endian 16-bit
word followed by CRC8: initial 0xFF, polynomial 0x31. Validate response length and
every CRC; reject partial/corrupt samples. A command argument word uses the same
word-plus-CRC encoding.

| Sensor | Address | Startup and measurement contract |
|---|---:|---|
| SCD41 | `62` | Stop `3F86`, wait 500 ms. Optional ASC setting `2416` only when explicitly configured. Start `21B1`. Ready `E4B8`, mask 0x07FF. Read `EC05`, three words; existing driver exposes nonzero CO2 ppm from the first word. Stop before close. |
| SEN55 | `69` | Stop `0104`, wait 200 ms. Read product `D014`, 16 words, require `SEN55`. Start `0021`, wait 50 ms. Ready `0202`, mask 0x01. Read `03C4`, 8 words. Status `D206`, 2 words. Stop before close. |
| SEN63C | `6B` | Enforce shared bus clock <=100 kHz. Stop `0104`, wait 1.4 s. Read product `D002`, 16 words, require `00085700`. Optional ASC `6711` only when explicitly configured. Start `0021`, wait 50 ms. Ready `0202`, mask 0xFF. Read `0471`, 7 words. Status `D206`, 2 words. Stop before close. |

SEN55 fields: PM1/2.5/4/10 in ug/m3 divide raw by 10; signed humidity divides by 100,
signed temperature divides by 200; VOC/NOx indices divide by 10. SEN63C has the four
PM fields, humidity, temperature and CO2 ppm. Preserve unavailable sentinels:
legacy code uses 0xFFFF for the PM fields, 0x7FFF for subsequent fields. Its CO2
signed-value interpretation beyond 32767 ppm requires protocol qualification;
do not silently treat every returned word as a valid physical reading. SEN63C
CO2 is unavailable during its initial 22–24 s while other fields may be usable.

| Timing | SCD41 | SEN55 / SEN63C |
|---|---:|---:|
| Poll | 5 s | 1 s |
| Stale after | 15 s | 5 s |
| Retry | 5 s | 5 s |
| No-data timeout | 30 s | 30 s |

Publish per-field sample times, validity and provenance. A fresh temperature
must not make absent CO2 appear fresh. Keep faults/status nonzero distinct from
valid data, reject conflicting enabled sources for the same field, and preserve
sensor calibration unless an explicit calibration operation is requested.

For Sunxi, legacy clock limiting reads actual `twi->freqency` from
`/sys/class/i2c-adapter/i2c-N/device/info`, changes `device/freq` if needed, and
verifies readback. Device-tree `clock-frequency` alone is not live readback.

Provenance: `hal/drivers/environment/{i2c,scd41,sen55,sen63c,service,group}.py`,
`robots/lamp/{scd41,sen55,sen63c}.json`,
`robots/lamp/overrides/pro/device/sen63c.json` and corresponding Pro variants.

Legacy SoC thermal sensing reads millidegrees from
`/sys/class/thermal/thermal_zone0/temp`. Its 95 C trip and 85 C resume defaults
are marked provisional; actual thermal-zone identity and kernel critical trips
need verification. They are not room-temperature readings. Provenance:
`hal/safety/policy.py:310–332`, `robots/lamp/SAFETY.md`.

## Qualification still required

- Identify this device's profile and installed peripherals, confirm bus/device
  identities, and inventory all existing processes/services holding them.
- Export and validate this unit's calibration and current servo registers without
  changing torque or pose. Establish raw-to-physical joint zero/sign mappings.
- Define and verify a no-motion takeover/rollback; preserve load-bearing hold.
  Audit actual installed shutdown code rather than assuming the source above
  matches the device.
- Verify placement, edge clearance and postures visually before and after motion;
  then qualify position, velocity, contact, fault, expiry and cancellation limits.
  No automatic room scan, calibration sweep or unobserved recovery motion.
- Exercise privacy at startup, while speaking/capturing, on process failure and
  on reconnect; prove stale media cannot escape a closed privacy gate.
- Measure actual ring timing/light output, servo-control jitter, camera frame
  age/FPS, sensor freshness, contention and faults under concurrent audio load.
  Source inspection and successful writes do not establish these results.

## Local SDK provenance fingerprints

These identify the development-environment source inspected in this audit.
They are not firmware hashes or dependencies of lampOS.

| Local SDK file | SHA256 |
|---|---|
| `scservo_sdk/protocol_packet_handler.py` | `9932c85b9e2ac671a7e33f39e8e80b05d6592280d9f2bcebd85103845e36e655` |
| `scservo_sdk/scservo_def.py` | `068f7ad5c453ad6e338543c706047253495fcd6bf9a632465501ace12fee0627` |
| `scservo_sdk/port_handler.py` | `c8dbb189ca98705bd71066d5a9a691b4c80b484c77ebe8a9ccc2d6c46321e999` |
| `lerobot/motors/motors_bus.py` | `abb070ea6e58812a6e51da7adc438c95c33a0b21020c519af4b1090568e048a2` |
| `lerobot/motors/feetech/tables.py` | `3030a82ee3e4775993792a22f4ed4385c798fa3e5385049a77a5c17196445fc8` |
