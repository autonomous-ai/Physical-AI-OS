# ROBOT.md Specification — `autonomous.device.v1`

`ROBOT.md` is the contract that describes a physical body to the Autonomous OS, the
agentic gateway, and skills. It is the device-side equivalent of `SKILL.md`: the YAML
front matter is the machine contract the runtime parses at boot; the prose below is
guidance the gateway and contributors read.

One file per device at `robots/<id>/ROBOT.md`. **Adding a device is writing a
`ROBOT.md`** (plus any missing drivers) — never a fork.

## How the OS consumes it

At boot the runtime reads the front matter and:

0. **Validates `schema`** — a missing, malformed, or unknown-major tag (e.g.
   `autonomous.device.v2` on a runtime that only understands `v1`) aborts boot.
   The runtime refuses to mount a body against an ABI it cannot read.
0b. **Verifies the `boards` gate** — resolves the physical board and aborts if it
   is unidentifiable or not in `boards`. Wrong board means wrong pin maps, a
   hardware fault, not a configuration choice.
0c. **Checks `id` against the folder** — `id` must equal the directory the
   profile is mounted from; a mismatch is a misplaced or mistyped profile and
   aborts boot. `id`/`name`/`type` are then exposed via HAL `GET /device`.
1. Brings up **only** the capability subsystems the device declares.
2. **Skips** undeclared capabilities silently — that is a different device, by design.
3. **Fails loudly** if a *declared* capability's driver is missing or won't initialize —
   that is a hardware fault, not a configuration choice.

This single rule is what turns "Intern" into "Lamp minus `motion` and `display`"
instead of a fork: the only difference between two devices is which capabilities they
declare. It replaces the old implicit `try/except ImportError` skip, which couldn't
tell "no servo by design" from "servo lib missing" from "servo broken."

## Front matter schema (v1)

| Field | Required | Meaning |
|-------|----------|---------|
| `schema` | yes | Contract version. `autonomous.device.v1`. |
| `id` | yes | Stable device id. **Must equal the device folder name** (`robots/<id>/`); the runtime aborts boot on a mismatch. |
| `name` | yes | Display name. Exposed via HAL `GET /device`. |
| `type` | yes | Free-form class (`desk_robot`, `desk_agent`). Exposed via HAL `GET /device`. |
| `boards` | yes | Supported boards. At boot the runtime resolves the physical board (`hal/board`) and aborts if it is unknown or not in this list. |
| `gateway` | yes | Default agentic gateway (`default`) + wire transport (`protocol`). The transport follows from the runtime (openclaw→websocket, hermes→sse); `protocol` is validated for consistency against `default` (a warning, not a driver). |
| `capabilities` | yes | Map of capability group → declaration (below). |
| `soul_ref` | no | Soul artifact for this body: a path read relative to the device folder (e.g. `SOUL.md`), or an `http(s)://` URL the runtime downloads. Absent → the gateway's default soul. |
| `urdf_ref` | no | The body's kinematic description (URDF): a path read relative to the device folder (e.g. `urdf/lamp.urdf`), or an `http(s)://` URL downloaded. Supplies the link offsets, joint axes and masses that geometric safety bounds are computed against — today `SAFETY.md` `motion.max_cog_offset_mm`. Absent → those bounds cannot be evaluated and pass through with a warning. Visual and collision meshes are optional and unused by the runtime. |
| `safety_ref` | no | The device's safety document: a path read relative to the device folder (e.g. `SAFETY.md`), or an `http(s)://` URL downloaded. Resolved at boot by HAL into pure gate functions (`hal/safety/policy.py`, see `SAFETY-SPEC.md`); the per-capability anchor-consistency check is a warning. |
| `manufacturer` | no | Informational, not parsed — who makes the body (`Pollen Robotics`, `Unitree`). |
| `extends` | no | Informational, not parsed — records which profile a declaration was copied from (`_base`). |
| `memory` | no | Memory backend declaration (`{ backend: <name> }`). Informational — the brain owns memory today; surfaced via HAL `GET /device`, not gated. |
| `startup_volume` | no | Speaker volume (0–100) the device boots at. os-server applies it once at startup, and HAL falls back to it when restoring the level after a media handover resets the mixer. Either way the level the user last set wins where one exists. Absent / out of range → `100` (software at max, so the hardware/alsactl level is the effective control). Lets a device with a loud speaker boot quieter instead of hardcoding the level. |
| `voice` | no | Voice defaults for this body (`{ tts_provider: <openai\|elevenlabs>, tts_voice: <name-or-id> }`). Both are **defaults** seeded into `config.json` once at startup, only when the user hasn't chosen them — so the Setup UI, HAL auto-start, and StartHALVoice all agree; the user's saved choice always wins. `tts_provider` absent / unknown → legacy default (`openai`). `tts_voice` pins the default voice explicitly (accepted verbatim). When the seeded provider is `elevenlabs` and `tts_voice` is absent, os-server picks a **language-aware** default (`vi`→Ngan, `zh`→Amy, else Rachel) so the voice matches the provider — an elevenlabs default with an openai voice like `nova` would otherwise 400. |

### Capability declaration

Each entry under `capabilities` is a group from `robots/contract/capabilities.md`:

```yaml
capabilities:
  motion:
    routes: [servo]           # HAL routes this group mounts
    driver: feetech           # implementation family. On motion this is a
                              # SELECTOR: HAL boot resolves it to a service
                              # class via hal/drivers/motors/factory.py.
                              # On every other capability it stays
                              # informational (surfaced via GET /device).
    required: false           # if true, a missing driver is a boot failure
    safety: SAFETY.md#motion  # bounds that govern this capability
    owner: pollen_daemon      # optional: another process on the device holds
                              # this hardware and hands it over on request
```

**`driver:` selector semantics.** Two capabilities resolve a backend class from
this field — `motion` (`hal/drivers/motors/factory.py`) and `vision`
(`hal/drivers/camera/factory.py`). Both follow the same rules; the vision
default is `opencv` (UVC webcam over V4L2), the motion default is `feetech`:

- absent → falls back to the capability default (`feetech` for motion, with a
  warning; `opencv` for vision, silently — every pre-selector device is a UVC
  webcam, so saying so each boot would be noise)
- registered name → the mapped motion service class is used
- unknown name + `required: true` → **boot fails loud** naming the driver and
  the registered set (a deploy fault, not a silent fallback)
- unknown name + optional → warning, the capability's routes stay unmounted

Registered vision drivers: `opencv` (UVC/V4L2 through OpenCV) and `rpicam`
(Raspberry Pi CSI sensors behind libcamera, read as MJPEG from `rpicam-vid`).
A CSI sensor cannot be opened by the OpenCV path at all — its `/dev/video*`
node is raw Bayer — which is why the selector exists rather than one driver
probing its way to the right backend.

A new motion backend is one class conforming to the `MotionService` protocol
(`hal/drivers/motors/base.py`) plus one registry line in the factory.

**`owner:` semantics.** Declare it when a process shipped with the body already
holds this hardware and only yields it on request — a vendor runtime that boots
before HAL and keeps running alongside it. `driver:` answers *which
implementation opens the hardware*; `owner:` answers *who has to let go of it
first*. It resolves through `hal/drivers/media_owner/factory.py` to a class
implementing the `MediaOwner` protocol (`hal/drivers/media_owner/base.py`):

- absent → HAL opens the hardware directly. The normal case; nothing runs.
- registered name → HAL calls `release()` at the very top of startup, before any
  capture device is opened or ALSA is probed, and `acquire()` on shutdown once
  its own handles are closed
- unknown name → **boot fails loud**, with no required/optional split: a
  capability naming an owner that does not exist cannot be honoured, and
  carrying on means opening hardware someone else still holds — which surfaces
  as a "device busy" far from its cause

Ordering is the reason this belongs to HAL and not to a launcher script. On
Reachy Mini the Pollen daemon holds `/dev/video*` and both ALSA PCMs; with the
release left to run late, PortAudio cannot probe a sample rate, the configured
ALSA output never enumerates, and TTS silently settles on output device -1 while
every status endpoint still reports healthy.

Registered owners: `pollen_daemon` (Pollen Robotics' `reachy_mini` daemon, via
its `/api/media/{release,acquire}` endpoints). One owner usually holds several
capabilities — on Reachy both `audio` and `vision` — and HAL releases once per
distinct owner, not once per capability.

`required: true` means "this device is not itself without this capability." Audio is
`required` on both Lamp and Intern; motion is `required` on neither.

## Per-device presets (`presets.json`)

The emotion / scene / aim preset *values* and the LED ring size live in
`hal/presets.py` as the **platform default** every device inherits. A device
overrides only the values it wants different by shipping an optional
`robots/<id>/presets.json` — a sibling of `ROBOT.md`, discovered by convention
(no front-matter field declares it). At boot HAL (`board/presets_overlay.py`)
deep-merges it onto the base tables in place, before any route or driver reads
them; a device with no file keeps the defaults verbatim. This is the same
"declare what's different" inheritance as `robots/_base`, applied to look-and-feel.

```json
{
  "led_count": 60,
  "emotion":    { "listening": { "color": [255, 120, 0] } },
  "scene":      { "relax":     { "brightness": 0.3 } },
  "aim":        { "desk":      { "base_pitch.pos": 8.0 } },
  "status_led": { "booting":   { "color": [0, 60, 200] } },
  "button_led": { "sleep_warn": { "color": [20, 12, 40] } }
}
```

- Every section (`led_count`, `emotion`, `scene`, `aim`, `status_led`, `button_led`, `ambient_led`) is optional.
- `ambient_led.resting` overrides the idle LED preset (`effect`, `color`, optional `speed`).
  `solid` paints once without animation. Missing overrides keep the dark platform default;
  Lamp declares solid `[1,1,1]`; intern-v2 keeps solid `[5,4,3]`. OS asks HAL to
  restore this preset rather than maintaining a second ambient color. The owner can
  replace it from the web UI (`PUT /led/resting`: default, off, or a custom solid);
  HAL saves that choice in `/var/lib/hal/resting_led.json` and re-applies it on boot.
- `status_led` restyles the os-server system-status feedback (booting/error/ota/
  connectivity/hal_down/agent_down/hardware/ready_flash + bootstrap OTA
  ota_progress/ota_error/ota_success + setup) — the OS owns the state machine,
  HAL owns the color/effect/speed. `setup` is a persistent solid; the rest are
  transient effect overlays. `button_led` restyles the hold-warning colors shown
  while a physical button is held (`sleep_warn`/`shutdown_warn`/`factory_reset`,
  needs the `button` capability) — the driver owns the staging (blink vs solid),
  the preset owns only the color.
- Each entry patches the matching base entry **field-by-field** — only the named
  fields change; the rest stay at the default.
- Naming a preset absent from the base table (a typo), a malformed file, or a
  non-positive `led_count` **fails loud at boot**, like an invalid `ROBOT.md`.
- **HAL-only:** presets are LED/servo look-and-feel; the OS core (Go) never reads
  them — unlike capabilities there is no second parser to keep in sync. An override
  only takes effect for routes the device mounts: `emotion` needs the `expression`
  capability, `scene` needs `light`, `aim` needs `motion`.
- Copy-paste reference: `robots/_base/presets.example.json` (annotated; the
  `.example.json` name is never loaded — rename to `presets.json` to activate).

## Versioning — the frozen contract

`schema` is an ABI. Within a major version fields are only **added**, never removed or
repurposed — a `v1` `ROBOT.md` must keep booting on every later `v1` runtime. Breaking
changes bump to `autonomous.device.v2`, and the runtime supports both across a
deprecation window. The capability vocabulary in `robots/contract/capabilities.md` follows the
same rule: **names are forever.**

This is the Autonomous equivalent of the Linux syscall ABI / Android API level — the
contract facing devices and skills is stable; the drivers behind it churn freely.

## Network-controlled bodies

A profile may describe a robot whose firmware stays on a microcontroller while
OS and HAL run on a separate host. See the [Wi-Fi body integration pattern](WIFI-BODY.md)
and its [Vietnamese version](vi/WIFI-BODY_vi.md) for driver ownership, host
selection, the experimental Stack-chan example, and qualification boundaries.
