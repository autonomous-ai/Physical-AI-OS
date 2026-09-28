# Safety Engine

The safety engine enforces a device's `SAFETY.md` bounds **deterministically in the
runtime**, below the agent. It is the mechanism behind the first principle in
`robots/contract/SAFETY-SPEC.md`: *safety is below the brain.* The agent requests actions;
the engine decides — on every request, regardless of who issued it — whether the
hardware is allowed to honour them and within what limits.

> **Status:** Slice 1 (brightness ceiling) is **implemented and enforced** —
> `hal/safety/policy.py` + the LED gate in `rgb_service.py`, loaded from
> `robots/lamp/SAFETY.md`. Later slices reuse the same loader + gate. Each table
> row below is marked enforced / reserved. This document tracks the code, not the
> other way around.

## Why an engine, not prompting

Routing safety through the language model is unreliable — it can be argued out of a
refusal, it can hallucinate a limit, and it cannot guarantee an action *did not*
happen. Guard mode in this codebase was already rebuilt to deliver alerts
deterministically for exactly this reason. The safety engine generalises that: the
runtime, not the gateway, is the single point that clamps, blocks, or stops.

## Architecture

Three layers, mirroring the device layer (`ROBOT.md` → capability → route → driver):

```
SAFETY.md front matter        the declared bounds (machine contract; per capability group)
        │  resolved via ROBOT.md safety_ref (path or http), parsed at boot
        ▼
hal/safety/policy.py        pure SafetyPolicy + gate functions (no IO, unit-testable)
        │  clamp_brightness(requested) -> min(requested, ceiling)   [slice 1]
        ▼
HAL capability routes          call the gate BEFORE actuating (led, later servo/music)
        │  deterministic, in-process, cannot be bypassed by the agent
        ▼
hardware
```

- **`SAFETY.md` front matter** — the bounds, keyed by capability group. Schema and
  field table: `robots/contract/SAFETY-SPEC.md`.
- **`hal/safety/policy.py`** — a pure loader (regex front-matter parse,
  dependency-free, same discipline as `hal/board/device.py`) producing a typed
  `SafetyPolicy`, plus pure gate functions. No hardware, no clock side effects, fully
  unit-testable off-hardware.
- **The routes** — each capability route consults the gate at the point of actuation.
  The LED route clamps brightness; later the servo route clamps speed/accel and
  guarantees `stop`. Because the gate is a plain function call in the request path,
  there is no path around it.

The policy is loaded once at boot (alongside the `DeviceProfile`) and exposed via the
device endpoint so the declared bounds are observable: `GET /device` already returns
`safety_ref`; the resolved bounds surface there too.

## Fail-safe semantics

Per-capability criticality (full rule in `robots/contract/SAFETY-SPEC.md`):

| Capability | Bound absent / unloadable | Rationale |
|------------|---------------------------|-----------|
| light, audio, motion | pass-through (log only) | enforcement is presence-driven and uniform: a declared bound is enforced, an absent one is not — the engine never invents a limit nobody wrote |

`SAFETY.md` is optional. The schema tag is validated like `ROBOT.md`'s — a missing or
unknown-major `schema` aborts boot rather than enforce an ABI it cannot read.

There is no separate kill switch: because enforcement is presence-driven, removing a
section (or the whole front matter) turns that enforcement off. To run a device with
unrestricted motion during bring-up, ship no `motion:` bounds; that is the off state.
A *malformed* bound (present but out of range) still fails loud — only *absence* is
pass-through.

## Slice roadmap

| Slice | Scope | Gate | Enforced where | Status |
|-------|-------|------|----------------|--------|
| 1 | `light.max_brightness` ceiling | `clamp_brightness` / `clamp_color` | LED gate (`rgb_service` `_handle_solid`/`_handle_paint`) | **enforced (v1)** |
| 2 | `quiet_hours` (light + audio) | `active_max_brightness` (time-aware) + `audio_quiet_now` | LED gate + music route | **enforced (v1)** |
| 3 | `motion.max_speed` (presence-driven) | `min_move_duration` | servo route | **enforced (v1)** (`max_accel` reserved) |
| 3b | `motion.stop_always` | — | — | **declared / structurally guaranteed** — no route-level gate consumes this field (see below) |
| 4 | fail-safe states (network/gateway loss → stop tracking; board fault → 503 isolation; `thermal.max_temp_c` → SoC over-temp health event + stop tracking; setup + servo over-current reserved) | WS-disconnect hook + per-capability `503` + thermal monitor (`thermal_over`/`read_soc_temp_c`) | `services` on WS disconnect + HAL routes/`/health` + `server.py` `_thermal_monitor` | **partially enforced (v1)** (setup + over-current reserved) |
| 5 | `audio.max_volume` ceiling | `clamp_volume` | `/audio/volume` route, above the speaker/BT-sink split | **enforced (v1)** |
| 6 | `motion.max_cog_offset_mm` (presence-driven) | whole-body centre-of-gravity score per frame (`recording_stability.py`) | recording load (`resample_recording`), physical + mock drivers | **enforced (v1)** |

Each slice adds fields to the `SafetyPolicy` and gate functions and wires one or more
routes; the loader and the front-matter contract do not change shape between slices
(fields are only added — the `autonomous.safety.v1` ABI).

## Verifying enforcement

A safety bound is only real if you can *prove* it holds and that the agent cannot get
around it. Each slice is verified at three levels; a bound is not "done" until all
three pass. (This is distinct from `robots/lamp/docs/security-test.md`, which covers
network/access-control security — ports, RCE, CORS — not actuation bounds.)

1. **Unit (pure gate, off-hardware).** The gate function is pure, so its limit is a
   table test: a request above the ceiling clamps to it, a request below passes
   through unchanged, an absent bound behaves per the fail-safe rule. Runs in CI with
   no device.
2. **Runtime (on the device, through the real route).** Issue the actuation request
   over HTTP and observe the hardware-bound value, not the requested one. The declared
   bound is also observable at `GET /device`, so the test asserts *request vs. ceiling
   vs. observed output* line up.
3. **Bypass audit (the safety-critical check).** Confirm there is **no** path to the
   actuator that skips the gate — issue the same action through every route that can
   drive it (agent path, direct route, any raw/low-level endpoint) and confirm each is
   clamped. A bound enforced on one path but reachable on another is not enforced.

### Slice 1 — brightness ceiling (checklist)

- [x] **Unit:** `clamp_brightness(255)` with `max_brightness: 180` → `180`;
      `clamp_brightness(120)` → `120`; no `max_brightness` → unchanged (pass-through).
      `clamp_color` scales hue-preserving (white→180,180,180; red→180,0,0). Schema
      missing/malformed/unknown-major fail-loud. (`hal/test/test_safety.py`, 21 tests.)
- [x] **Runtime:** verified on a real Lamp — `GET /device` returns
      `"safety": {"light": {"max_brightness": 180}}`; `POST /led/solid` at full white
      (255) reads back `[180,180,180]`, `[100,50,0]` passes unchanged, `[255,0,0]` →
      `[180,0,0]`.
- [x] **Bypass audit:** the gate sits in `rgb_service` `_handle_solid` / `_handle_paint`
      — the single chokepoint every pixel write funnels through. All callers (LED
      routes, effects, app_state, scene, gpio_button, presence, smooth_animation, main)
      use `dispatch(RGB_CMD_*)` → those handlers; the only direct `_driver` writes are
      inside them (post-clamp) and `clear()` (black). Grep confirmed no path skips it.
- [x] **Determinism:** `clamp_color` is pure and never consults the caller, so the
      clamp is identical for the agent, the Web UI, or a raw `curl`.

### Slice 2 — quiet hours (checklist)

Quiet hours add a **time** dimension: the LED ceiling drops and music is
suppressed inside a daily window (device-local wall-clock; the device runs all
day, so this is real time-of-day, not "off at night"). The gate reads the clock
on every request, so it flips at the boundary with no restart.

The clock is read via `hal.clock.device_now()`, which resolves the device's
**current** timezone from `/etc/timezone` (zoneinfo) on every call — so if the
user changes the device timezone at runtime, quiet hours track the new zone
immediately, without restarting HAL. (A plain `datetime.now()` would keep the
timezone glibc cached at process start.) Falls back to naive system-local time
when `/etc/timezone` is absent (dev/macOS).

- [x] **Unit (injected clock):** `in_window` handles the midnight wrap
      (22:00→07:00 true at 23:00 and 06:00, false at 12:00, end-exclusive at 07:00);
      `active_max_brightness` returns the reduced ceiling (40) inside the window and
      the base (180) outside; `clamp_color((255,255,255), now=23:00)` → `(40,40,40)`,
      `now=12:00` → `(180,180,180)`; `audio_quiet_now` true in-window, false out /
      when no policy. (`hal/test/test_safety.py`.)
- [x] **Runtime:** `GET /device` reports `safety.light.quiet_hours` +
      `safety.audio.quiet_hours`. With the window set to the current time, the LED
      ring clamps to the reduced ceiling and `POST /audio/play` returns
      `{"status":"suppressed"}`; outside the window both behave normally.
- [x] **Bypass audit:** the LED quiet ceiling rides the same `rgb_service`
      `_handle_solid`/`_handle_paint` chokepoint as slice 1 (no new path). Music
      suppression sits in `/audio/play` — the only route that starts discretionary
      audio (TTS is intentionally exempt).
- [x] **Determinism:** the window check is pure given `now`; the only impurity is
      reading the system clock, isolated in `policy._now()` so tests inject time.

### Slice 3 — motion (checklist)

Motion is **presence-driven**, the same rule as light/audio: a declared bound is
enforced, an absent one is pass-through (a device that ships no `motion:` bounds
moves unrestricted — that is the off state, not a refusal). `max_speed` is enforced
by *stretching a move's duration* (the move still reaches its target; only speed is
capped) — never by truncating the destination. Recovery actions
(`release`/`zero`/`hold`/`stop`) are never gated so you can always safe the body.
`stop_always` is parsed and reported as part of the declared policy, but no HAL
route currently consumes that field: this recovery property is structural (the
routes are ungated), not an independently enforced policy gate. `POST
/servo/stop` is the route that property exists for — it aborts a move or a
recording in flight and *holds* (torque stays on), reads no bound, and takes no
arguments. It is not `/servo/release`, which travels to a rest pose before
cutting torque; a stop that moves first is wrong for anything with wheels or
legs (#201).

The vision-tracking loop is gated too, and needs its own mechanism:
`min_move_duration` cannot bound it because there is no destination to stretch a
move toward, only a per-frame speed profile. `cap_speed_dps` clamps the loop's
own pursuit/saccade ceilings (55 / 100 deg/s, `hal/drivers/tracking/constants.py`)
to the declared `motion.max_speed` at the single place the profile is chosen
(`tracker_service.set_profile`). Those constants are tuning, not permission — on
Lamp (`max_speed: 120`) nothing is clamped today; a body declaring a lower
ceiling is what the gate is for.

Recorded animations are gated at load time rather than per frame. A recording
is resampled onto the playback loop's own fps grid, and any segment demanding
more than the ceiling is *stretched in time* — the same degradation the route
gate uses, applied per segment so a recording slows down exactly where it was
impossible and nowhere else (`hal/drivers/motors/recording_timing.py`, shared by
the physical and mock drivers so the simulator plays what the body plays). The
ceiling is the lower of the servo's measured limit (`SERVO_MAX_DPS`, 250 deg/s)
and the declared `motion.max_speed`.

Recorded animations are gated for **stability** as well as speed, at the same
load-time chokepoint. Every joint can sit inside its own range while the
*combination* puts the centre of gravity outside the base, and no per-joint bound
expresses that, so `hal/drivers/motors/recording_stability.py` reconstructs the
whole-body centre of gravity for every frame and **refuses** a recording that
reaches further from the base axis than `motion.max_cog_offset_mm` allows. Unlike
speed, there is no degradation that makes a tipping pose safe — the recording is
skipped, so the failure mode is a missing animation, not a crash. The geometry
comes from the body's own URDF via the `ROBOT.md` `urdf_ref` key; the check is
presence-driven and inert without both declarations, and a body that declares a
ceiling but no usable `urdf_ref` logs that it cannot score a pose and passes
through rather than failing closed. Lamp declares 22 mm (measured: the
third-party clip that tipped lamp-0c89 peaked at 31.6 mm, the worst shipped
recording at 17.7 mm, the other 28 at or below 17.6 mm — see
`robots/lamp/docs/motion-playback.md`); no other body declares it, so nothing
else changes behaviour. Derive the number per body from its own animation
library — it is millimetres of that body's geometry and mass, never a constant to
copy.

Recorded moves on the Reachy SDK path are gated differently, because the driver
does not own the playback loop: `ReachyMotionService` hands a trajectory to
`mini.play_move()` and the Pollen daemon streams it. There is no frame to slow
down, so the move is **scanned before playback and refused** if its peak head
rotation exceeds the declared ceiling (`peak_head_dps`, `_move_refused`). Speed
is measured as the true angle between head orientations over a ≥20 ms window —
per-axis euler deltas spike at gimbal crossings, and raw frame pairs turn
timestamp jitter into speeds the head never reaches. Head *translation* is not
bounded: `max_speed` is deg/s and `head_x/y/z` are millimetres, and no body
declares a translation limit — not enforced, so not claimed.

Refusal applies to every source including Pollen's own library, deliberately: an
official move tripping the gate is evidence the declared number is wrong, and
excusing it by origin would hide that signal. `robots/reachy-mini/SAFETY.md`
records how its ceiling was derived and what still needs a real robot.

The Stack-chan Wi-Fi path keeps the same duration gate across a process and
network boundary. `StackChanMotionService` sends the already-stretched duration
as `duration_ms`, renews a short controller lease while that timed interpolation
runs, and releases the lease into a measured-position hold at the end. A torque
release supersedes any active move, travels to the conservative head-down rest
pose, confirms that pose from measured joint feedback, and only then disables
torque. If the pose is not reached, it halt-holds with torque enabled. Its
handshake requires `motion.timed_move`, measured position, halt-and-hold and
torque-release capabilities. If HAL, its process or Wi-Fi disappears, the ESP32
firmware expires the lease and halt-holds the measured pose; on a WebSocket
close the installed firmware instead releases torque and restarts, which is
why HAL keeps the transport open on a commissioning miss;
firmware that only exposes an uncalibrated spring-speed parameter is rejected as
incompatible because it cannot honor the declared degree-per-second ceiling.
This path is covered by host protocol tests; physical fault-injection over a
real Wi-Fi link remains a device qualification step.

The driver also implements the refcounted `MotionService.acquire_body()` /
`release_body()` contract used by look, capture and tracking. Overlapping owners
keep `_tracking_active` true until the last owner exits; clearing the separate
tracking-session flag cannot release another owner's claim. Ownership is local
coordination for gaze and emotion routes, distinct from the firmware controller
lease. It does not prevent the owner from moving or bypass the always-available
halt path.

The Stack-chan transport rechecks every timed move against measured position
and `motion.max_speed`, including zero and gravity-rest moves. A superseding
release halt-holds before measuring its start pose. Unreadable position or a
required duration over 60 seconds fails closed without sending the move. The
gateway publishes a connection only after sending `hello.accepted`, so motion
commands cannot overtake the handshake.

The companion [Stack-chan integration repository](https://github.com/glifocat/stackchan-autonomous/tree/9a5209596b97c257b6b2c1f6ff6bec91c44f8112)
ships firmware patches and a separate standalone HTTP bridge. Its Docker demo
does not boot `hal.server` and is not a startup test for this driver. Full HAL
still needs a device profile declaring `motion.driver: stackchan` and the host
board. The inspected firmware returns `completed` with state `scheduled` when
it installs a timed move, not when the body arrives; `motion.halt` clears the
lease, so the host reacquires it before the next move. Source agreement does
not establish physical qualification or identify the firmware flashed on a body.

All Stack-chan moves confirm measured arrival before releasing the controller
lease. Firmware scheduling delays can extend interpolation past the requested
duration, so elapsed host time alone is not completion. HAL renews the lease
while checking arrival (within 1 degree), using a 2-second settling window
before halting and returning an error instead of reporting success. Individual
protocol requests remain subject to the configured command timeout.

Home commissioning belongs to the experimental Stack-chan bench tool, not the
shared OS motion contract. The standard `hal.server:app` entrypoint exposes no
`/stackchan/*` or `/servo/home*` endpoints. The bench HTTP schema and routes live in
`robots/_experimental/stackchan/commissioning.py`; the Stack-chan driver enforces
its coordinate frame and commissioning bounds. Shared servo routes, models, and
the `MotionService` contract remain unchanged.

The explicit bench entrypoint exposes `GET /stackchan/home` for passive capability
discovery and `GET /stackchan/home/position` for measured pan/tilt in
`calibrated_home_deg_v1`, both without a motion lease. Motion remains disabled by
default (`STACKCHAN_HOME_COMMISSIONING_ENABLED=0`). When explicitly enabled,
`POST /stackchan/home/move` requires firmware capability `motion.home_degrees.v1`,
an explicit coordinate frame, only a tilt target from 7 to 10 degrees, and a
requested duration from 2 to 10 seconds. The speed policy may stretch duration
up to 60 seconds. Starting feedback must show pan within ±30 degrees and tilt
in [0, 5). Yaw torque is off during this pitch-only move. Below 5 degrees, stop
or transport loss can leave torque off or the session faulted; mechanical
support and direct supervision remain required.

During each trajectory, including its optional repeat, the driver reads feedback
after waits of at most 250 ms or half the lease TTL, whichever is shorter.
Request latency adds to this interval; configured command timeouts still apply.
Invalid feedback or yaw drift over 1 degree triggers a halt without waiting for
the motion duration to end. This sampled check cannot detect every excursion
between reads. A missed target after settling is halted and reported with measured
settle samples while preserving the connection. Firmware rejections also keep the
connection open; command timeouts still close it, and the installed firmware
releases torque and reboots on disconnect. A successful hold is not guaranteed
for every failure, particularly below the firmware's hold floor.

Success requires measured pitch within 1 degree of target, at least 6 degrees,
and at least 1 degree of positive progress. If the head settles short but stable
(last samples within 0.2 degrees, at least 1 degree of progress, at least 6 degrees,
and short by more than 1 but no more than 3 degrees), the driver repeats the same
target at most once. The response reports `recommands`. Only measured arrival
allows `lease.release` to re-energize both axes; the driver then rechecks position.

Use bench `POST /stackchan/stop` and `POST /stackchan/release` for diagnostics.
Firmware rejections return 502 with `op`, `code`, and `message`; other reported
release failures, including missed arrival and cancellation, return 502 with
`message` and `errors`. A failed release never reports successful torque-off on
this bench endpoint. Shared `/servo/stop` and `/servo/release` retain their existing
OS behavior; the latter's legacy success response alone does not prove release.
This tool does not verify calibration or qualify hardware for routine use.

The Stack-chan driver remains experimental. Before device qualification:

- Identify and pin a matching firmware build with the required motion
  capabilities; this HAL driver does not add those capabilities to stock firmware.
- Verify authenticated WSS and certificate validation on the real ESP32.
- Confirm completed bounded moves, measured position and duration limits on
  the actual pan/tilt assembly.
- Exercise halt, Wi-Fi loss, HAL process loss and lease expiry during motion,
  verifying the physical hold and recovery behavior.
- Calibrate the gravity-rest pose and physically confirm torque release only
  after that pose has been measured.

Host and loopback tests, and separate face, LED or speaker tests, do not complete
these motion qualification steps.

Select it with `driver: stackchan` in the device profile's `motion` capability.
Set `STACKCHAN_DEVICE_ID`, a distinct `STACKCHAN_BODY_TOKEN` of at least 32
characters, and both `STACKCHAN_BODY_TLS_CERT` and `STACKCHAN_BODY_TLS_KEY`.
`STACKCHAN_BODY_HOST` (default `0.0.0.0`) and `STACKCHAN_BODY_PORT` (default
`8765`) configure the listener; `STACKCHAN_BODY_COMMAND_TIMEOUT` (default `3.0`
seconds) and `STACKCHAN_BODY_LEASE_TTL_MS` (default `1500`, accepted `250..5000`)
configure failure timing. TLS is mandatory unless
`STACKCHAN_BODY_ALLOW_INSECURE_WS=1` explicitly enables plain WebSocket for an
isolated development network. Never use that opt-in in production.

### Learned-policy interface (dry run)

`POST /policy/run` is an interface-only endpoint for a learned controller such
as ACT or SmolVLA. It validates and logs `policy` plus `task`, returns a
`dry_run` run record, and does **not** import an inference runtime, capture an
observation, or issue a motor target. This is deliberately not a safety bypass:
there are no targets to pass through it yet. When execution is added, every
target must traverse the motion gate and `POST /servo/stop` must first cancel
the policy worker, then hold the body. `POST /policy/stop` clears a dry-run
request without touching a motor; on a body that also mounts `/servo/stop`,
that existing hardware stop first makes the same cancellation call, then holds
the body.

- [x] **Unit:** `min_move_duration` stretches a too-fast move (120° at 120 deg/s →
      1.0s), passes a slow one, bounds an instant (duration 0) request, passes
      through with no `max_speed` and with no policy at all, ignores joints with no
      known start; commented `# stop_always` is not parsed as a bound; `max_speed:
      0` fails loud. (`hal/test/test_safety.py`.)
- [ ] **Runtime (NOT yet verified on device — user deploys):** `GET /device`
      reports `safety.motion`; `/servo/move` at a tiny duration returns a stretched
      `duration` and the ring/arm moves at the capped speed; a device with no
      `motion:` bounds moves unrestricted (no 403).
- [x] **Bypass audit (routes):** the speed cap is applied at `/servo/move` via
      `min_move_duration` (the one endpoint that takes a duration). NOTE: internal
      animation (idle/emotion poses driven by the runtime, not the agent) is not
      gated — that is device-controlled, not agent-requested; revisit if needed.
- [x] **Determinism:** `min_move_duration` is pure (no clock, no caller identity).

### Slice 4 — fail-safe states (checklist)

Fail-safe is **state-driven** rather than per-request clamping: when the device loses a
critical dependency it falls into a safe posture deterministically, below the agent.
Three conditions are enforced today; setup-incomplete and servo over-current are reserved.

- [x] **Network / gateway loss → stop agent-driven tracking.** On gateway WebSocket
      disconnect, `runtimes/openclaw/service_ws.go` calls
      `hal.StopServoTracking()` (`system/lib/hal`) → HAL `POST /servo/track/stop`,
      so the body stops chasing a target it has no fresh vision for. Best-effort and
      guarded by `SetUpCompleted`. Key nuance: the device does **not** freeze or "hold
      pose" — local idle animation continues (it is local and harmless) and recovery
      reflexes (`motion.stop`/release, mute, sleep, wake) stay available; only
      *agent-driven* tracking and new motion stop.
- [x] **Board / driver fault → 503 isolation.** Already met: a faulting capability
      returns `503` per-route while the rest keep serving, surfaced via `/health`. No
      new mechanism — the fail-safe contract reuses the existing isolation.
- [x] **Thermal (SoC over-temp) → health event + stop tracking.** Presence-driven on
      `thermal.max_temp_c`: a background monitor (`server.py` `_thermal_monitor`) reads
      the SoC temp from `/sys/class/thermal` every ~10s via `policy.read_soc_temp_c`; the
      pure `policy.thermal_over` hysteresis gate trips at `max_temp_c` and clears at
      `resume_temp_c` (default `max−10`). On trip it sets `state.thermal_over`, logs, and
      stops discretionary tracking; idle stays alive. Surfaced at `GET /health.thermal`.
      Threshold is SoC-specific — read the board's own critical trip, not a generic guess.
- [ ] **Setup incomplete → reserved.** Not gated in the runtime yet (setup/identity
      reflexes only is declared intent, not enforced).
- [ ] **Over-current (servo) → reserved.** No servo current sensor wired; reserved for
      hardware/telemetry that exposes it.
- [x] **Unit:** `thermal_over` trips at/above `max_temp_c`, holds through hysteresis
      above `resume_temp_c`, clears at/below it, and is False with no policy / no thermal
      section / unreadable temp; `read_soc_temp_c` parses millidegrees → °C and returns
      None on any read error; parse defaults `resume = max − 10` and fails loud on
      `max_temp_c ≤ 0` or `resume ≥ max`. (`hal/test/test_safety.py`.)
- [ ] **Runtime (NOT device-verified):** the WS-disconnect → `/servo/track/stop` path and
      the thermal monitor are repo-level only; not yet confirmed on a live device (pull the
      gateway link; heat the SoC past `max_temp_c`).

### Slice 5 — `audio.max_volume` (checklist)

An all-day speaker ceiling, independent of `audio.quiet_hours`: the window suppresses
*discretionary* output (music), while the ceiling bounds *how loud anything gets*,
including spoken replies.

- [x] **Gate:** `policy.clamp_volume` — pure, no clock, no caller identity. Absent bound
      = pass-through (only the 0–100 scale clamp), the same presence-driven rule as
      `clamp_brightness`.
- [x] **Enforced where:** `POST /audio/volume` (`hal/routes/audio.py`), clamped **above**
      the sink split so the ceiling holds on the built-in speaker (amixer/DAC dB) and on a
      Bluetooth headset sink alike. The persisted volume (`config/.volume`, restored by
      os-server at boot) stores the clamped value, so a pre-existing higher setting cannot
      survive a reboot as an unbounded one.
- [x] **Bypass audit (callers):** every path that sets volume goes through this one route
      — agent/skill, `system/intent` (`rules_audio.go` "volume up"/"quieter"), the Monitor
      web slider, and os-server's boot restore (`config_watch.go` → `hal.SetVolume`). The
      gate sits in the route rather than in the callers, so none of them can be the thing
      that decides.
- [x] **Reported:** both `GET` and `POST /audio/volume` return `max_volume` (null when
      undeclared); the `POST` reply also carries the volume actually applied, so a caller
      never has to assume its request landed verbatim and a UI can correct itself at once
      instead of drifting until the next poll.
      `system/lib/hal.MaxVolume()` and the Monitor slider read it — advisory only, so a
      client that skips it still cannot exceed the bound; it just loses the ability to
      scale its steps. The slider bounds its track to the ceiling instead of letting the
      handle snap back from a dead zone, and `rules_audio.go` expresses "louder"/"quieter"
      as fractions of the usable range (otherwise, on a device whose ceiling is 40, a
      fixed "quieter" of 30 stops being distinguishable from "louder").
- [x] **Unit:** clamp above/below ceiling, pass-through with no bound and no policy,
      0–100 scale clamp, a `max_volume` nested inside `quiet_hours` not read as the all-day
      bound, and fail-loud on out-of-range. (`hal/test/test_safety.py`.)
- [ ] **Runtime (NOT device-verified):** repo-level only — not yet confirmed against a
      live speaker (set 100 via API, read back the ceiling; check the amixer/BT sink value).

## Relationship to existing ad-hoc enforcement

Some safety behaviour already exists, hardcoded and scattered: `motion.stop()` in the
motors/animation services, lerobot's mechanical position clamp, the LED brightness
scaling config. That mechanical position clamp is still the only per-joint position
limit: the command path has **no** declared position bound (recordings are gated for
speed and, since slice 6, for whole-body stability — neither is a per-joint clamp).
The engine does not rip these out at once — it *centralises* them
into the declared policy one slice at a time, so the bounds become data a device
declares rather than constants buried in drivers. Slice 1 introduces the engine with
a bound that has **no** prior enforcement (an agent-independent brightness ceiling),
proving the path end to end before migrating the existing pieces.
