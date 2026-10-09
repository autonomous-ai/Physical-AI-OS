# Motion Playback

How servo recordings in `hal/recordings/` become motion. Source:
`hal/drivers/motors/animation_service.py`, with the timing rule itself in
`hal/drivers/motors/recording_timing.py`.

## Timing

Recordings in `hal/recordings/` are teleop captures authored at **20 Hz** (`timestamp` column, 0.05s apart). The playback loop in `hal/drivers/motors/animation_service.py` steps exactly **one frame per tick** at `self.fps` (30, `HAL_SERVO_FPS`), so raw frames would play at the wrong speed. `_load_recording` therefore resamples every recording onto the loop's own 1/fps grid at load time, and playback itself stays a plain frame-per-tick walk — no timing logic in the hot path.

Resampling does two things:

- **Honors the authored `timestamp`**, so a recording lasts as long as it was recorded to last. Stepping 20 Hz frames at 30 Hz previously played every animation 1.5× too fast.
- **Stretches segments that exceed the speed ceiling** — the lower of `SERVO_MAX_DPS` (250 deg/s, `HAL_SERVO_MAX_DPS`; set to 0 to disable) and the body's declared `motion.max_speed`. The STS3215 tops out around 270 deg/s, and several recordings were authored well past that; on Lamp the declared 120 deg/s is the binding one, and it stretches `laugh` (+20%), `playful`, `headshake` and `acknowledge`. No `music_*` recording is affected, so grooves keep their timing.

The cap is not cosmetic. Measured on lamp-0c89 by sampling `Present_Position` while playing `greeting`: commanding 554 deg/s left the servo **55° behind its goal** — it saturates, lags, then snaps, which is the audible grinding. Stretching is surgical, applied only to the segments that were impossible:

| recording | authored | peak demanded | after resample | stretch |
|---|---|---|---|---|
| `greeting` | 2.95s | 554 deg/s | 3.49s | +18% |
| `happy_wiggle` | 7.95s | 476 deg/s | 8.27s | +4% |
| `nod` | 1.95s | 302 deg/s | 2.00s | +2.6% |
| `idle` | 9.95s | 115 deg/s | 9.97s | none |

`idle` is already within budget, so it is not stretched at all — but it gained the most, because the loop no longer drops to 5 Hz once idle settles. That reduction stepped fps-grid frames six times too slowly and delivered breathing as five visible jerks per second; it cost more in smoothness than it saved in CPU.

The same rule runs off-hardware. `MockMotionService` (`HAL_BOARD=sim`, the `make sim` laptop body) imports `resample_recording` from the same module and replays on the same 30 Hz grid, so a recording takes the same wall-clock time in the simulator as on a body. Its `move_to`/`aim`/`nudge` interpolate over the commanded `duration` and block until arrival, as the SDK-backed driver does. What the simulator still does not model: inertia, collision, torque, and per-unit EEPROM calibration.

Two consequences worth knowing:

- Servo motion registers are left at their defaults (`Acceleration=254`, `Goal_Velocity=0`). Capping speed in the servo instead of in the trajectory was measured and rejected: it cut jerk ~33% but pushed tracking error from 55° to 71°, and the resulting lag walked the arm into a mechanical jam. Software knows the whole trajectory ahead of time and can stretch it under control; the servo can only lag.
- `add_recording()` (upload path) **invalidates** the cache rather than filling it. It receives frames already stripped of `timestamp`, so caching them would bypass resampling — the uploaded copy would play at raw frame rate while the identical file read from disk played correctly.

## Stability

Speed is not the only way a recording can hurt the body. Every joint can sit
inside its own range while the *combination* puts the centre of gravity outside
the base — measured on lamp-0c89 on 2026-09-04, a third-party CSV whose joints
were all in range tipped the unit onto its side ([#271]).

No per-joint bound expresses this, so `recording_stability.py` reconstructs the
whole-body centre of gravity for every frame and refuses a recording that
reaches further from the base axis than the body allows. The check runs inside
`resample_recording`, so a body and the simulator refuse the same clip.

Nothing in the check is lamp-specific. It needs two **per-body declarations**,
and is inert without either:

| what | where | lamp |
|---|---|---|
| the ceiling | `SAFETY.md` `motion.max_cog_offset_mm` | 22 mm |
| the geometry | `ROBOT.md` `urdf_ref` → a URDF in the device folder | `urdf/lamp.urdf` |

Presence-driven like every other bound in `robots/contract/SAFETY-SPEC.md`: a
body that declares neither is unrestricted, and one that declares a ceiling but
no usable `urdf_ref` logs that it cannot score a pose and passes through rather
than failing closed. Reachy Mini declares neither today and is unaffected.

The ceiling is bracketed by measurement rather than chosen:

| | peak CoG offset from the base axis |
|---|---|
| the clip that tipped the lamp | 31.6 mm |
| `confused.csv` — the worst shipped recording | 17.7 mm |
| the other 28 shipped recordings | ≤ 17.6 mm |

All 29 shipped recordings pass with room to spare. Forward reach is what
matters: `base_yaw` spins the arm about the very axis the offset is measured
from and cannot contribute, while `elbow_pitch` moves it most (the worst frame
of the clip that tipped had the arm folded forward at `elbow_pitch` 53.8° with
`base_pitch` at only 6.9° — the per-joint extremes of that clip never occurred
in the same frame).

Every load is logged: `INFO` with the peak and the frame it occurred at, `WARNING`
once a clip passes above 85% of the ceiling, and `ERROR` with the full offending
pose when one is refused — a refusal has to be explainable from the journal alone.
A refused recording is skipped by the normal load path, so the failure mode is a
missing animation, not a crash.

A frame naming joints that are not in the body's URDF is **skipped and logged as
skipped** rather than scored: every unknown joint would read as 0° and hand back
a comfortable number for a pose that was never evaluated, and a false pass is
worse than no check.

Giving another body this gate is two declarations, no code: ship its URDF and
derive its own ceiling from its own animation library (widest clip plus
headroom). Never copy 22 mm — it is millimetres of *this* body's geometry and
mass, not a universal constant.

`urdf/lamp.urdf` is kinematics and masses only; this repository ships no meshes,
so the visual and collision elements were removed. It is packaged to devices by
`make upload-device lamp` along with the rest of the device profile. Link masses are
estimates and the URDF's inertial origins are all zero, so each link's mass sits
at its own origin: the absolute millimetres are approximate, the ranking between
clips is not. Re-derive the constant if the body's mass distribution changes.

[#271]: https://github.com/autonomous-ai/autonomous-os/issues/271

## Overload cut-off

Playback is also where a blocked arm shows up: the trajectory keeps advancing while a
joint cannot follow, and the servo pushes at full drive. `AnimationService` therefore
samples every joint's `Present_Load` at 20 Hz (`hal/drivers/motors/overload.py`).

- **Contact stop.** `base_yaw` at 55 %, `wrist_roll` at 75 % or `wrist_pitch` at 65 %
  for 0.05 s halts the recording where it is, pins the pose with torque on (the arm
  does not drop), plays a soft "uh-oh" chime and blocks goal writes for 3 s. Then it eases back
  into idle from the pose it stopped at. On lamp-52e6 these joints peak at 52 % in free
  motion and a hand on `base_yaw` drives it to 94-100 %. `base_pitch` and `elbow_pitch`
  carry the arm's weight and reach 83-100 % in free motion, so they get no fixed floor;
  they and `base_yaw` are capped at 60 % torque (`torque_limit`; 70 % cost the worst
  recording, `shock`, 1° of extra lag, 50 % cost 7°).
- **Learned envelope.** During playback every joint, the weight-bearing ones included,
  is held to what a free run loaded at that frame plus 12 % (`contact.profile_margin`),
  and to how far it trailed its goal there plus 3.5° (`contact.lag_margin`), which
  still sees a hand on a joint already at its torque cap.
  The envelope is learned per unit by `POST /servo/contact/learn` (each recording
  played free twice, the per-frame max kept, saved to
  `/var/lib/hal/contact_profile.json`). The frame index it looks up is the playback
  frame, so the envelope follows the recording after speed-cap resampling; re-learn
  after recalibration, a `torque_limit` change or new recordings. Outside playback
  (gaze, tracking, `/servo/move`, holds) the fixed `contact.off_playback` floors apply.
- **Overload cut-off.** A joint at 80 % or more for 1.5 s (the arm is still forced while
  held) cuts torque on all servos, plays the ack chime and blocks every goal write for
  120 s. Afterwards the body resumes into idle from wherever the arm ended up.

Recording frames sent while goals are blocked are dropped, not queued. The thresholds
(`contact.load`/`hold_s`/`pause_s`, `load`, `hold_s`, `retry_s`, `torque_limit`) are device-owned in
`robots/lamp/servo_overload.json`;
the full behaviour is in the fail-safe section of
[`docs/safety.md`](../../../docs/safety.md).

## Aim hold

`POST /servo/aim` stops the playback loop, moves the head, and restarts the loop on a
one-frame recording, `__aim_hold__`: the turned pose, kept for 5 s (`_hold_until`)
before idle resumes (longer while a servo hold is active). The aim also drops any blend
left from before it (`_interpolation_frames` / `_interpolation_target`). The loop runs a
pending blend before any recording frame and does not check the hold while blending, so
an idle blend started just before the aim (e.g. idle resuming after a voice capture)
used to swing the head back toward idle and then snap it to the turned pose, about 1.5 s
into the hold; a turn-and-look photo taken in that window showed the front instead of
the requested side. `move_and_hold` and `halt` already drop the blend the same way.

## The range demo — computed, not recorded

`POST /servo/demo` (`hal/drivers/motors/range_demo.py`) performs a narrated tour
of the movement range, one joint at a time: the base to both yaw limits and back
to centre, the head rolled on its own, the wrist tilted up and down, the elbow
stretched, a lean of the base, and home. Nothing is detected and nothing is
reported — the movement and the words are the whole deliverable.

It is the one motion in this file that is deliberately **not** a recording, and
the reason is the failure it replaces. Asked to demonstrate its range, the lamp
used to answer with the `scan` emotion: `hal/recordings/scanning.csv`, 360 frames
over 17.95 s, `base_yaw.pos` spanning **−31.3…+23.1 — 54.4° of a 270° travel** —
narrated as *"I'll sweep my whole range… doing a full turn now!"*. A recording
cannot be right by construction. It is somebody's hand movement frozen at capture
time, nothing in the file states how far it reaches, and it stays wrong silently.
Waypoints read from `C.YAW_MIN` / `C.YAW_MAX` are right by construction, stay
right when the limits change, and port to another robot's constants for free.

**One joint at a time, and back to centre before the next.** `waypoints()`
returns 14 legs in five groups, every excursion measured from the pose the demo
started in (`seed_pose`) and clamped to the joint's travel minus `PITCH_MARGIN`
(2°). Each group ends on a silent return to the seed value, so the base is facing
front again before the head starts — the first cut chained yaw straight into
pitch from the far end of the sweep, and on device the head tilting while the
body still faced the wall read as two unrelated moves, not a tour.

| Joint | Legs | Reach | Pools |
|-------|------|-------|-------|
| `base_yaw` | −135 → +135 → seed | `YAW_MIN`..`YAW_MAX`, the real limits | `demo_left`, `demo_right`, `demo_centre` |
| `wrist_roll` | seed −45 → +45 → seed | `ROLL_REACH_DEG` | `demo_head`, then silent |
| `wrist_pitch` | seed −25 (up) → +25 → seed | `PITCH_LOOK_DEG` | `demo_up`, `demo_down`, silent |
| `elbow_pitch` | seed +10 (up) → −10 → seed | `ELBOW_REACH_DEG` | `demo_neck`, then silent |
| `base_pitch` | seed −12 → seed | `BASE_PITCH_LEAN_DEG`, one way | `demo_lean`, then silent |

Only yaw goes to a declared limit; the rest are device-proven or deliberately
small. `WRIST_PITCH_MIN` / `WRIST_PITCH_MAX` declare ±90° and the arm does not
have that — on lamp-ac82 `wrist_pitch` reached −89.55 going up (stopped by the
soft limit, not the joint) and only −16.61 going down — so the wrist tilts the
25° the search sweep's look ring walks every run. The elbow stays at ±10 because
an elbow at +35.8 with the base dropped to +10.6 extended the arm far enough back
to look like it might tip; the demo never moves the two together. `base_pitch`
leans one way only: rest sits at ~29.8 against a travel ceiling of 30. The
phrases match that split — the yaw pools say *"all the way left"*, the others
only *"up, like this"* or *"a little lean"*.

**The base is sped up and put back.** `DEMO_YAW_SPEED` (1600 ≈ 100°/s) is written
to `base_yaw` for the performance, exactly as the sweep does and for the same
reason: untouched, the joint manages ~14°/s, so a 135° leg takes ~9 s and the
phrase describing it finishes while the lamp is still swinging. Every leg is sent
as a `LEG_DURATION_S` (1.0 s) `move_and_hold`, with a `DWELL_S` (0.3 s) pause once
the servos are still so a limit reads as a pose rather than a bounce. Restored in a
`finally`, because a cap left behind would follow the demo out and throttle idle
and every emotion.

**Speech is HAL's timing, os-server's words.** Each narrated leg fires `aim._say(pool)` →
`POST /api/sensing/filler` → the `demo_*` pools in `system/lib/i18n/fillers.go`.
No LLM turn and no tokens. A `[HW:...]` marker could not do this: markers fire
before TTS, so a marker-narrated demo describes a performance that has already
finished. Silent legs (the second half of a pair, a return to centre) skip the
filler endpoint entirely. The phrase LEADS its leg (speak, then move) and the next leg waits on
`_wait_until_still` — `move_and_hold` returns when it has finished *sending*
frames, not when the servos arrive, so without that wait the script outruns the
body within two legs.

**Gates.** Refuses while the device sleeps and while the motion service is
suppressed; refuses a second demo on top of a running one. `start()` returns
immediately and the performance runs on its own thread — the agent reaches this
through a `[HW:/servo/demo:{}]` marker, and only for a request that names
MOVEMENT (*"show me how far you can move"*, *"show me your range of motion"*): a
bare *"show me what you can do"* is a general abilities question and the skill
text tells the model not to start the demo for it. `fireHWCall` allows a hardware POST
five seconds against a ~25 s demo. The physical button aborts it alongside the
aim and the sweep (`button_actions._stop_active_tracking`): a click that stopped
the arm but not the narration would leave the lamp describing legs it is no
longer performing. An aborted demo returns to the pose it started from.
