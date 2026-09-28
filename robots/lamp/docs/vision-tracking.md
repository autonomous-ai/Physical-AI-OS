# Vision Tracking — Object Follow with Servo

Lamp can track and follow any object the user names. A detector finds the object by name and seeds a ViT tracker, then a fast vision loop follows it in real time while a decoupled servo worker glides the head smoothly toward the target.

All tracking code lives in the `hal/drivers/tracking/` package:

| Module | Contents |
|--------|----------|
| `tracker_service.py` | `TrackerService` — session lifecycle (start/stop/status/update_bbox) + the fast vision loop |
| `constants.py` | All tuning knobs (imported as `C` by the other modules) |
| `detection.py` | `ObjectDetector` — YuNet face / local YOLOv8n / remote YOLOWorld chain |
| `vit_tracker.py` | OpenCV tracker backend (`create_tracker`, `vit_init`, `vit_update`, `get_tracking_score`) |
| `servo_follow.py` | `ServoFollower` — servo goal + SmoothDamp follow worker thread |
| `filters.py` | `AlphaBetaFilter2D`, `PID`, `smooth_damp`, `soft_deadband` |
| `frame_utils.py` | `downscale`, `scale_bbox` (coordinate mapping) |

## Voice intent routing

Unmatched voice requests can use the OS Jev fallback to select `servo_track`
with one of 23 canonical target labels, or `servo_track_stop`. The OS validates
the target and declared motion capability, then reuses the existing tracking
rule; Jev never supplies a free-form HAL payload. Missing/unsupported targets,
external cameras and low-confidence decisions defer to the main agent, which
retains the open-vocabulary skill path. See [Jev intent routing](../../../docs/os-server.md#jev-intent-fallback).

## Architecture

```
User: "Lamp, follow the cup"
         |
    POST /servo/track {"target": "cup"}
         |
    1. Freeze servos 0.3s → grab a sharp frame
         |
    2. Detect the object (YuNet face | local YOLOv8n | remote YOLOWorld) → bbox
         |
    3. TrackerVit init on the bbox
         |
    4. Two decoupled threads:
         |   a. Vision loop @ FAST_LOOP_FPS (15):
         |        ViT update → alpha-beta centroid filter → soft dead zone
         |        → PID + velocity feedforward → publish an absolute servo goal
         |        (background YOLO re-detect every 1.5s corrects drift)
         |   b. Servo worker: SmoothDamp glide toward the latest goal
         |        (ease-in/ease-out; coalesces tiny setpoint changes)
         |
    5. Lost / bloated / no-detect / timeout → auto-stop, then interpolate to idle
```

The vision loop never blocks on motor motion: it publishes an *absolute* servo goal and moves on to the next frame. The servo worker owns the physical motion and continuously eases toward whatever the latest goal is. This is what keeps both the tracker fps high and the head motion smooth.

### Downscaled vision, original-resolution math

The camera runs **1280×720**. Every heavy vision component — the ViT tracker and all three detectors — runs on a frame downscaled to `VISION_MAX_WIDTH` (640 px wide, 0.5× → ¼ the pixels) for speed. Each bbox they produce is mapped **back to original 1280×720 coordinates** before any servo/PID math (`frame_utils.downscale` / `scale_bbox`, `vit_tracker.vit_init` / `vit_update`, and `detect_object` is transparent). Because the coordinate contract downstream is always original resolution, none of the pixel-tuned constants (PID gains, gates, dead zones, feedforward thresholds) change when the downscale factor changes. Set `VISION_MAX_WIDTH = 0` to disable.

## Detection

`detect_object(frame, target)` returns a bbox `(x, y, w, h)` in original camera coords, trying three paths in order:

| Path | Detector | When | Speed (A523) |
|------|----------|------|--------------|
| 0 | **YuNet** face detector (`face_detection_yunet_2023mar.onnx`) | target ∈ {`face`, `human face`, `khuôn mặt`, `mặt`} | ~30 ms |
| 1 | **Local YOLOv8n** (COCO classes, `yolov8n.pt`, imgsz=448) | target maps to a COCO class | ~310–530 ms, median ~360 ms (measured on lamp-0c89, 1280x720 input) |
| 2 | **Remote YOLOWorld** open-vocab (`{DL_BACKEND_URL}/detect/yoloworld`) | non-COCO target, or local miss (fallback) | **~0.55 s median** (334 ms min, 1.1 s p90, 2.0 s max; n=49 on lamp-0c89) |

- COCO has no hand/face class, so `hand`/`face` intentionally fall through to YuNet/YOLOWorld instead of mapping to `person` (which locked onto the whole body).
- On a local-YOLO miss the code falls back to remote YOLOWorld, **throttled** to at most one attempt per `REMOTE_FALLBACK_MIN_INTERVAL` (2.0 s) so a genuinely unseeable target doesn't hit remote every redetect.
- **The fallback is off for routine redetects** (`allow_remote_fallback=False`, passed by the loop's background scan). Once a COCO target is locked, a local miss is ordinary — the object turned, one blurred frame — and the detect thread is single-flight, so a remote round-trip stretches a ~0.5 s confirm cycle to ~3 s, trips the trust gate and parks the servo with the object still in frame. Local is the authority for a class it was trained on; the next redetect confirms. Seeding and post-loss recovery (`_do_retry`) still allow remote, and an open-vocab target is unaffected — with no local path there is nothing to fall back from.
- Detection quality filters: confidence ≥ `DETECT_MIN_CONFIDENCE` (0.15), area between `DETECT_MIN_AREA_RATIO` (0.3%) and `DETECT_MAX_AREA_RATIO` (80%) of frame.
- **Lookalike guard (local path)** — local YOLO detects **unrestricted** (no `classes=` filter) so competing classes stay visible, then: (a) the confusion cluster cell phone / mouse / remote needs conf ≥ 0.35 (`_CONFUSABLE_CONF_FLOOR`) instead of the global 0.15; (b) **cross-class disambiguation** — if a box of another class overlaps the candidate (IoU ≥ 0.5) with *higher* confidence, the candidate is rejected ("that's probably a mouse, not the phone you asked for") and the code falls through to the remote fallback. The 0.35 floor applies only to the session-start detect (`strict=True`); mid-session redetects use the global 0.15 floor (a fast-moving phone reconfirms at conf 0.2–0.3, and the reinit gates already protect the lock) — cross-class disambiguation stays on in both modes.

Weights are checked into the repo (`hal/drivers/tracking/models/`) so deploy is one rsync and the Pi never needs internet at boot to start tracking.

## Tracker: TrackerVit

**Model:** `hal/drivers/tracking/models/vittrack.onnx` (checked into repo)

| Feature | Value |
|---------|-------|
| Speed | ~15–25 ms/frame on the downscaled frame |
| Confidence score | `getTrackingScore()` 0.0–1.0 per frame |
| Scale handling | Auto-adjusts bbox size |
| Loss detection | Returns `ok=False` + low score when object disappears |

**Fallback chain:** TrackerVit → CSRT → KCF → MIL. Only ViT exposes a confidence score (used for ghost-lock detection); the others return 1.0.

## Servo Control

Tracking drives 4 joints:

- **base_yaw** (ID 1) — left/right pan (100 % of yaw)
- **base_pitch** (ID 2) — up/down tilt, 10 % of pitch
- **elbow_pitch** (ID 3) — up/down tilt, 90 % of pitch
- **wrist_pitch** (ID 5) — up/down tilt, 0 %

Pitch is concentrated on the elbow (`PITCH_WEIGHT_ELBOW = 0.90`). Empirically only pure-rotation joints move the object toward center; base/wrist mostly translate the camera (kinematic coupling), so their weights are low/zero. The elbow motor's positive direction was reversed in hardware, so its contribution carries `ELBOW_PITCH_SIGN = -1.0`.

### Control law (vision loop → servo goal)

Each frame the loop turns the tracker bbox into an absolute servo goal:

1. **Alpha-beta filter on the centroid** (`AlphaBetaFilter2D`) — a constant-velocity steady-state Kalman. Smooths jitter, coasts through dropped/garbage frames on prediction, gates outlier teleports (`AB_GATE_PX`), and exposes a velocity estimate. A velocity lead (`AB_LEAD_S = 0.20 s`) aims ahead of the target — cinematic "lead room".
2. **Tiered dead zone** (`soft_deadband`) — three bands, continuous at both boundaries: true zero inside ±`DEAD_ZONE_INNER_PCT` (2 %, PID clears and, when there is no velocity-pursuit command, the follower is retargeted to its current pose so it cannot continue toward a stale goal); a gentle **creep band** up to the outer edge (`DEAD_ZONE_CREEP_GAIN` = 0.12 slope) so the camera lazily drifts toward center instead of freezing dead — a hard stop here produced the start-stop "security camera" feel; full error beyond the outer edge.
3. **Velocity feedforward first, PID second (smooth pursuit)** — the primary command is a feedforward term proportional to the target's measured pixel velocity (`VFF_GAIN` = 0.9): the camera *matches the target's speed* like human smooth pursuit, even at zero position error. A time-aware PID with anti-windup (KP deliberately small: 0.015 yaw / 0.02 pitch) only trims the residual position error. A position-centered but moving target keeps panning (does not freeze in the dead zone). Combined output is clamped to `PID_OUTPUT_MAX_DEG` (5°).
4. **Saccade vs pursuit profiles** — mirroring human gaze: offset > `SACCADE_OFFSET_FRAC` (22 % of frame width) switches the follow worker to the **saccade** profile (`SACCADE_SMOOTH_TIME` 0.20 s, `SACCADE_MAX_SPEED_DPS` 100) for a fast relocation; small offsets use the heavy **pursuit** profile (`SERVO_SMOOTH_TIME` 0.32 s, `SERVO_MAX_SPEED_DPS` 55) — fluid-head inertia. One compromise profile did both badly. The loop state logs `SACCADE` vs `CHASING`.
5. **Publish goal** — the resulting absolute joint target is handed to the servo worker (non-blocking).

### Servo worker (SmoothDamp follower)

`ServoFollower` (`servo_follow.py`) runs a worker on its own thread and continuously eases the joints toward the latest goal using **SmoothDamp** (`smooth_damp`, a critically-damped follower): each joint carries its own velocity, so every move accelerates smoothly and eases out into the target, and a fresh goal arriving mid-move retargets without a restart jerk — the cinematic "film camera" motion. The worker wakes at the bounded `SERVO_SUBSTEP_SLEEP` (30 ms) cadence but calculates SmoothDamp from the actual elapsed monotonic time, capped at `SERVO_SUBSTEP_MAX_DT_S` (60 ms) after a scheduler/serial stall. It sends one multi-joint bus command only when at least one servo command changes by `SERVO_COMMAND_MIN_DELTA` (0.08), coalescing only tiny normalized setpoint changes; the final target is always sent once.

Hardware motion during tracking: at every session start the HAL explicitly writes `TRACKING_GOAL_VELOCITY = 0` (unlimited) to clear any velocity cap left by an earlier mode. The software profiles therefore own the speed; the old 150 steps/s ≈ 13°/s cap flattened the SmoothDamp curves into a constant crawl. `TRACKING_ACCELERATION = 30` supplies the gentle hw ramp. When tracking stops, HAL reads the physical pose into the animation state and dispatches idle, whose normal interpolation continues directly from that pose. There is no intermediate return-to-zero move.

### Drift correction & lock management

- **Background YOLO re-detect** every `YOLO_REDETECT_S` (1.5 s) on a worker thread (never blocks the fast loop; result delivered via a `maxsize=1` queue). Forced immediately when the object nears a frame edge (>25 %) or on the first tracker miss.
- **Miss-coast** — when the tracker misses while the target was moving (fast wave, motion blur), the loop keeps panning along the target's last alpha-beta velocity for up to `MISS_COAST_FRAMES` (6) miss frames (state `COAST`) before falling back to the search sweep. Stopping dead on the first miss guaranteed a fast target exited the frame before the redetect landed.
- **Reinit gating (SORT/ByteTrack-style)** — a re-detect only reinitializes the tracker when it has clearly diverged, to avoid the reinit churn that whipsaws the servo:
  - **Area gate** `YOLO_AREA_GATE_MULT` (4.0) — reject a detection whose area is >4× or <¼ the median of the last 5; don't reinit to it.
  - **Reinit debounce** `REINIT_COOLDOWN_S` (0.5 s) — rate-limit reinits; bypassed only when the lock is clearly lost (`center_dist > frame_diag × LOST_CENTER_FRAC` = 0.5).
- **Bbox-trust guard (bloat hold)** — when the ViT lock dissolves into an oversized box the centroid is garbage, so the servo holds instead of chasing it:
  - `BBOX_FREEZE_RATIO` (1.0) — bbox ≥ full frame area ⇒ ViT dissolved.
  - `BLOAT_HOLD_MULT` (3.0) — bbox > 3× the last trusted lock area ⇒ hold and force a re-detect.
- **Servo confidence floor** — ViT confidence < `SERVO_MIN_CONF` (0.25) holds the servo (`LOW-CONF-HOLD`) even while the detector is still confirming the target; without it, conf 0.15–0.4 with a fresh confirm was a blind zone that kept the servo chasing a barely-held (often ghost) lock. The tracker keeps updating and the PID resumes when confidence recovers.
- **Detector-gated trust** — if no detector has confirmed within the trust window and ViT confidence < `TRACKER_TRUST_CONF` (0.4), hold the servo (`WAIT-YOLO`) rather than chase a phantom; high ViT confidence keeps firing even without a fresh detector confirm. The window is **sized from the detector's measured latency**, not fixed: `trust_window_s()` returns `max(TRUST_TRACKER_S, YOLO_REDETECT_S + 2 x latency + TRUST_MARGIN_S)`, where latency is an EMA maintained by the background scan. One loop is served by detectors three orders of magnitude apart (YuNet ~30 ms, local YOLO ~0.4 s, remote ~0.55 s median), so a single constant is only correct for the fastest — and 2.5 s meant every slower target sat in `WAIT-YOLO` with the object plainly visible. The floor keeps face behaviour identical.
- **Hold really holds** — every hold state (`LOW-CONF-HOLD`, `WAIT-YOLO`, `BLOAT-HOLD`, low-confidence skip frames) retargets the follow worker to the *current* pose (`ServoFollower.hold()`). Previously a hold only stopped publishing new goals, so the worker kept gliding toward the last stale goal — the arm visibly chased a ghost for a beat after the lock had gone bad.

### Pixel-to-Degree Conversion

```
deg_per_px = CAMERA_FOV_DEG / frame_width          (same on both axes for square pixels)

dx = filtered_lead_x - frame_width/2   (positive = right)
dy = filtered_lead_y - frame_height/2  (positive = below)

yaw_step         = clamp(PID(soft_deadband(dx)) + VFF·vx·deg_per_px·dt,  ±5°)
pitch_correction = clamp(PID(soft_deadband(dy)) + VFF·vy·deg_per_px·dt,  ±5°)
```

### Tuning Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `VISION_MAX_WIDTH` | 640 | Downscale width for ViT + detectors (0 = off) |
| `FAST_LOOP_FPS` | 15 | Vision loop frequency (servo commands are decoupled; the worker wakes at ~33 Hz and coalesces tiny setpoint changes) |
| `CAMERA_FOV_DEG` | 60 | Horizontal FOV, for px→deg |
| `DEAD_ZONE_INNER_PCT` | 0.02 | True-zero band (servo rests) |
| `DEAD_ZONE_YAW_PCT` / `_PITCH_PCT` | 0.07 / 0.05 | Outer dead-zone edge (creep band ends) |
| `DEAD_ZONE_CREEP_GAIN` | 0.12 | Lazy-drift slope inside the creep band |
| `PID_YAW_KP` / `PID_PITCH_KP` | 0.015 / 0.02 | PID proportional gains (trim only — VFF is primary) |
| `PID_OUTPUT_MAX_DEG` | 5.0 | Max degrees per fire (yaw & combined pitch) |
| `AB_ALPHA` / `AB_BETA` | 0.6 / 0.2 | Alpha-beta position/velocity gains |
| `AB_GATE_PX` | 200 | Reject a centroid teleport beyond this residual |
| `AB_LEAD_S` | 0.20 | Velocity lead (cinematic "lead room" ahead of the target) |
| `VFF_GAIN` | 0.9 | Fraction of target velocity fed forward (primary command) |
| `VFF_MAX_DT_S` | 0.20 | Cap on per-fire dt for feedforward |
| `VFF_MOVING_MIN_PXS` | 40 | Target speed above which a centered target keeps panning |
| `SERVO_SMOOTH_TIME` / `SERVO_MAX_SPEED_DPS` | 0.32 / 55 | Pursuit profile (heavy, fluid-head) |
| `SACCADE_SMOOTH_TIME` / `SACCADE_MAX_SPEED_DPS` | 0.20 / 100 | Saccade profile (fast relocation) |
| `SACCADE_OFFSET_FRAC` / `SACCADE_EXIT_FRAC` | 0.22 / 0.12 | Saccade enter/exit thresholds (hysteresis — no speed-cap flip-flop at the boundary) |
| `SERVO_SUBSTEP_SLEEP` / `SERVO_SUBSTEP_MAX_DT_S` | 0.030 / 0.060 | Servo-worker wake period / maximum measured SmoothDamp step after a stall |
| `SERVO_COMMAND_MIN_DELTA` | 0.08 | Coalesce only tiny normalized setpoint changes; the final target is sent once |
| `TRACKING_GOAL_VELOCITY` | 0 (unlimited) | Explicitly written at session start to clear a stale hardware cap; SmoothDamp profiles own the speed envelope (150 steps/s ≈ 13°/s flattened every ease curve into a robotic crawl) |
| `TRACKING_ACCELERATION` | 30 | Hardware acceleration ramp |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.10 / 0.90 / 0.0 | Pitch distribution across joints |
| `ELBOW_PITCH_SIGN` | -1.0 | Elbow polarity (hardware reversed) |
| `YOLO_REDETECT_S` | 1.5 | Background re-detect interval |
| `YOLO_AREA_GATE_MULT` | 4.0 | Reject re-detect area outliers |
| `REINIT_COOLDOWN_S` | 0.5 | Min seconds between tracker reinits |
| `BBOX_FREEZE_RATIO` | 1.0 | Bbox ≥ frame ⇒ ViT dissolved (hold) |
| `BLOAT_HOLD_MULT` | 3.0 | Bbox > 3× trusted lock ⇒ hold |
| `CONFIDENCE_THRESHOLD` | 0.15 | Below this = low-confidence frame |
| `LOW_CONF_WINDOW` / `LOW_CONF_STOP_COUNT` | 15 / 8 | Sliding window: ≥8 low frames in the last 15 → stop (a consecutive counter was reset by every above-threshold flicker, letting ghost locks live forever) |
| `SERVO_MIN_CONF` | 0.25 | Confidence floor for firing the servo PID at all |
| `TRACKER_TRUST_CONF` / `TRUST_TRACKER_S` / `TRUST_MARGIN_S` | 0.4 / 2.5 / 0.5 | Detector-gated trust (see above). `TRUST_TRACKER_S` is the **floor**, not the window — the window is derived per session from measured detector latency |
| `YOLO_MAX_MISS` | 30 | Consecutive tracker misses before retry |
| `MAX_TRACK_DURATION_S` | `HAL_TRACKING_MAX_DURATION_S` (10) | Auto-stop timeout (10 s default; configured per device) |
| `_LOCAL_IMGSZ` | 448 | Local YOLO inference size. This IS the detector's effective resolution — YOLO letterboxes any input down to it, so against the 1280-wide camera 448 reaches the model at 0.35x where 320 reached it at 0.25x. Small desk objects (cup, book, phone) arrived ~30px at 320 and were missed. Re-export the ONNX after changing it; the size is baked in. (640 measured 1.3–2.9 s on torch — still out of reach.) |

All knobs live in `hal/drivers/tracking/constants.py`. (The old dead `GIMBAL_*` / `EMA_ALPHA` proportional path was removed in the package split.)

Set `HAL_TRACKING_MAX_DURATION_S` in the Lamp's `/opt/hal/.env` to choose the wall-clock session limit; the installed Lamp default is `10`. Restart the `hal` service after changing it.

### Servo Position Limits

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Auto-Stop Conditions

| Condition | Action |
|-----------|--------|
| `confidence < 0.15` in ≥8 of the last 15 frames (sliding window) | Stop — lost target |
| Bbox shrinks below `DETECT_MIN_AREA_RATIO` | Stop — ghost-lock on a sliver |
| Bbox overflows frame + no detect for 3 s | Forced retry, then stop if unrecovered |
| No detector confirm for `STOP_NO_YOLO_S` (20 s) | Stop — ghost tracking |
| CSRT misses `YOLO_MAX_MISS` (30) after `MAX_TRACKING_RETRIES` (4) | Stop — object gone |
| Tracking duration > `HAL_TRACKING_MAX_DURATION_S` (10 s by default) | Stop — timeout to save motor/CPU |
| GPIO-button or TTP223 single-click | Stop — explicit user attention-cancel |

Note: a large bbox (e.g. a person filling the frame) is **not** a stop condition — PID drives off the centroid, not bbox size, so a close object still tracks. When tracking ends, idle interpolates from the arm's measured current pose instead of first moving through zero — see [Interaction with Other Systems](#interaction-with-other-systems).

### Auto-stop on gateway/network disconnect

Object tracking is driven by remote vision updates from the agent/cloud. When the gateway WebSocket disconnects (cloud or internet loss), the device auto-stops any in-flight servo tracking — `runtimes/openclaw/service_ws.go` calls `hal.StopServoTracking()` → HAL `POST /servo/track/stop` (best-effort, guarded by `SetUpCompleted`). Without fresh remote updates, continued tracking would keep aiming the body at a stale target it can no longer correct, so it is stopped as a safety reflex. Local idle animation continues (the device stays "alive", doesn't freeze) and recovery (`/servo/track/stop`, stop/release) stays available. See `robots/lamp/SAFETY.md` → `## fail-safe states` (Network/gateway loss row, enforced).

## API Endpoints

All under `/servo/track`.

### GET /servo/track/targets — List suggested targets

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

Detection is open-vocabulary via YOLOWorld (and YuNet for faces) — any text works, this list is just suggestions.

### POST /servo/track — Start tracking

`target` accepts either a single string or a list of candidate labels. When a list is passed, the first non-empty label is used. Useful when the caller (e.g. an LLM skill) is unsure which exact label will match.

```json
// Auto-detect, single label
{"target": "cup"}

// Auto-detect, list of candidate labels (preferred from LLM skills)
{"target": ["cup", "mug", "coffee cup"]}

// Manual bbox (skip detection — target is for display only)
{"bbox": [190, 50, 170, 300], "target": "cup"}

// Response
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [190, 50, 170, 300],
  "confidence": 1.0
}
```

### POST /servo/track/stop — Stop tracking

```json
{"status": "ok", "tracking": false}
```

### GET /servo/track — Check status

```json
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [195, 55, 175, 295],
  "confidence": 0.612
}
```

### POST /servo/track/update — Re-initialize bbox

Manual re-init of the tracker with a new bbox without stopping the session (the background YOLO re-detect handles drift automatically; this is for callers that want explicit control).

```json
{"bbox": [250, 160, 75, 95], "target": "cup"}
```

## End-to-End Flow

### Happy path

```
1. User: "Lamp, follow the cup"
2. Agent calls POST /servo/track {"target": "cup"}
3. HAL internally:
   a. Freezes servos 0.3s and snapshots a sharp frame
   b. Detects "cup" (local YOLOv8n, or remote YOLOWorld) → bbox
   c. TrackerVit init uses the same frame + bbox (coordinates match)
   d. Starts the vision loop + servo worker
4. Servo pans smoothly to follow the cup, background YOLO corrects drift
5. User: "OK stop" → agent calls POST /servo/track/stop
6. Idle interpolates directly from the final tracking pose
```

### Auto-stop on lost

```
1. Object leaves frame or is occluded
2. TrackerVit confidence stays below 0.15 for most of the recent window (or ViT lock dissolves)
3. Background YOLO can't re-find it → after the guards trip → auto-stop
4. Idle interpolates directly from the final tracking pose
5. Agent can notify user or re-issue the follow command
```

## Camera Stream Overlay

When tracking is active, the MJPEG stream (`/camera/stream`) draws:
- Green bounding box around the tracked object
- Target label above the box

## Web UI

Camera section shows:
- **Vision Tracking card** — target input, bbox input, Start/Stop/Status buttons
- **Stream badge** — "LIVE" or "TRACKING: {target}"
- **Confidence** — shown in tracking info panel
- **Polling** — status refreshes every 3 seconds

## Dependencies

- `opencv-python>=4.8.0` (already in `pyproject.toml`)
- `ultralytics` — local YOLOv8n inference
- `vittrack.onnx`, `yolov8n.pt`, `face_detection_yunet_2023mar.onnx` — checked into `hal/drivers/tracking/models/`, so deploy is one rsync and the device needs no internet at boot.

> **Don't export YOLO to ONNX to speed it up.** Tried and reverted. Benchmarked on lamp-0c89 at imgsz 448, interleaved: `.pt` via torch 310/360/427/526 ms (min/p50/p90/max, n=25) against `.onnx` via onnxruntime 179/245/561/607 ms — ONNX won the median, lost the tail, and a first sequential run had them the other way round. Contention with HAL's camera, voice and servo loops on four cores swamps the difference. It also saves no dependency (`ultralytics` pulls torch regardless and is what loads the `.onnx`), while costing 6 MB and baking `_LOCAL_IMGSZ` into the export.
- `requests` (already in project)
- **YOLOWorld API** — DL backend at `{DL_BACKEND_URL}/detect/yoloworld` (open-vocab fallback only)

## Interaction with Other Systems

| System | During tracking | After tracking |
|--------|----------------|----------------|
| Servo idle animation | Suppressed (`_hold_mode`) | Resumed |
| `/servo/play` | Blocked by `_hold_mode` | Resumed |
| Sensing (face, motion) | Continues — shares camera | Continues |
| Camera stream overlay | Green bbox drawn | Normal stream |
| TTS | Continues normally | Continues normally |

Resuming idle is an **explicit dispatch**, not a side effect of clearing the tracking flag. While `_tracking_active` is set, `AnimationService._continue_playback` drops the in-flight recording (`_current_recording = None`) so nothing fights the tracker. Clearing the flag does not put it back: the event loop returns at its first guard (`if not self._current_recording`), so without a dispatch the arm sits rigid at its last tracking pose with torque on until the next emotion or play command. The `_track_loop` `finally` first reads that physical pose into `_current_state`, then calls `animation_service.dispatch("play", animation_service.idle_recording)`. Dispatch rather than `_handle_play` keeps playback owned by the event thread, and the seeded state lets idle interpolate directly from the pose where tracking stopped.

## Performance Notes

- Fast-loop CPU floor on the Allwinner A523 is ViT inference + detector cost; the frame downscale (`VISION_MAX_WIDTH`) and local imgsz are the main levers. Note `VISION_MAX_WIDTH` is not a lever on detection *accuracy*: YOLO letterboxes to `_LOCAL_IMGSZ` regardless of the input size, so pre-downscaling the frame changes the tracker's cost, not the scale the detector sees.
- Motion smoothness comes from the decoupled servo worker + SmoothDamp + velocity feedforward; the alpha-beta filter + reinit gating keep the goal itself stable so the follower isn't chasing noise.
- Small/far objects (e.g. a cup across the room) can exceed both local and remote detector resolution — a perception limit, not a control bug.

---

## Look-aim — pointing the head before a visual question captures

Separate from object tracking above, and driven by a different trigger.

The realtime `look` tool takes **no parameters**: it captures whatever the head currently faces
(`orchestrator.py` — *"the model just signals intent to look; the device grabs the current frame"*).
So a visual question — *"what am I holding?"* — could be answered confidently from a picture of a
wall. `hal/drivers/tracking/aim.py` centres the subject first.

| | |
|---|---|
| **Trigger** | the `look` tool firing — **not** ordinary conversation |
| **Scope** | yaw only |
| **Budget** | `HAL_LOOK_AIM_DEADLINE_S` (8 s); on expiry it captures from wherever it reached |
| **Disable** | `HAL_LOOK_AIM=false` |

Ordinary chat is untouched: the body stays still through listening and thinking as before. Only a
`look` call releases it, because that is the moment the device was explicitly asked to look at
something.

**The body is owned for the whole look.** From the moment the aim starts until the shutter closes,
`servo_ownership()` takes a refcounted slot in the same `_tracking_active` lock the vision tracker
uses, which suppresses **all** emotion servo animation (`routes/emotion.py`) and makes the animation
loop drop any recording in progress.

This is not optional polish. Emotion presets play **recorded** poses that are absolute on every
joint — including `wrist_roll` — so one arriving between the aim and the capture re-poses the head
entirely, and the frame shows wherever the animation parked it rather than the user. A "curious"
reaction landing mid-question is enough to capture the ceiling. `nudge()` preempts an animation that
is already playing, but not one dispatched afterwards, which is exactly the window the capture sits in.

**Ownership is refcounted, not saved and restored.** `servo_ownership()` calls
`acquire_body()` on entry and `release_body()` on exit, so it releases only its own hold and a look
never ends a genuine object-tracking session that was already running. It used to save the previous
value and write it back, which lost the update whenever two of the six call sites overlapped — and
they run on three different threads (the gaze watcher, the realtime `look` tool, the sweep). The
owner that exited last re-asserted a stale `True`, wedging the lock with nobody holding it and
silently suppressing every emotion animation and all of gaze until a face-track session happened to
clear it (#312). `acquire_body`/`release_body` are part of the `MotionService` contract
(`hal/drivers/motors/base.py`), so every body answers for ownership the same way.

A wedge can no longer outlive its cause either: the flag set with no writer is an impossible state,
so `AnimationService` times it and self-clears after 30 s with an `ERROR` line
(`hal/drivers/motors/tracking_wedge.py`).

**Why the centring loop is yaw only.** The yaw sign is copied from the tracker's empirically
verified convention (`dx>0` → `base_yaw` increases). `AnimationService.nudge()` drives `base_pitch`,
whereas the tracker distributes pitch across base/elbow/wrist — so the pitch sign is **not**
validated on this path, and an inverted pitch is a bug this codebase has already hit once (see
`servo_follow.command_pid`).

The bearing restore in priority 3 is the exception, and it is safe for a specific reason: it sends an
**absolute** pose via `move_and_hold`, not a relative nudge. An absolute target has no sign to get
wrong. That is what lets a head left pointing at the floor recover its height — with yaw-only
correction it would sweep the floor in a circle no matter how right the direction was.

An aim that moved the head parks it the same way the search does (`nudge` and the bearing restore
both end in `move_and_hold`), so `aim_for_look` schedules the same
`release_to_idle_later(HOLD_AFTER_FIND_S)` from its result builder whenever `iterations` or
`bearing_steps` is non-zero — see *Handing the body back* under the search sweep for the guards.
Gaze usually retakes the body once a face is back in frame, which is why the freeze was less
visible here; without a face it stuck exactly like the search.

**Priority order:**

1. **Person visible** → centre it. Person box preferred over the face box: a held-up object often
   hides the face but rarely the whole body, and framing the person includes whatever they hold.
2. **Nothing visible, but a subject was confirmed at this pose seconds ago** → **hold and capture.**
   Treat the disappearance as *occlusion, not absence* — that is what a held-up object looks like to
   a detector, and turning away would abandon the very thing the user asked about.
3. **Nothing visible and nothing seen recently** → go to the remembered **pose** — direction and
   posture together — in a single absolute move. This used to advance in `BEARING_STEP_DEG` hops,
   re-detecting between them so it could not pass over someone standing en route; the hops were
   dropped because the lens sees ~110°, so anyone in between is already in frame before the head
   moves at all. They bought no coverage and cost a detect plus a settle each, roughly a second per
   hop out of the aim's own budget.
4. **Deadline** → capture from wherever the head reached. It never sweeps here.

Every move goes through `nudge()`, so `SAFETY.md`'s `max_speed` stretches the move rather than being
bypassed to meet the deadline. A physical-button single click aborts it (`button_actions.py`), since
that gesture means "stop moving and pay attention to me".

#### Which detection counts as "the person asking"

A `person` box is not enough. The detector's global floor is `DETECT_MIN_CONFIDENCE = 0.15`,
deliberately loose because it is tuned for the **tracker**, where losing a lock on a phone at an odd
angle costs more than a false positive. Aiming wants the opposite trade — a false positive turns the
lamp at a wall — so the aim applies its own two gates:

| gate | default | rejects |
|---|---|---|
| `HAL_LOOK_AIM_MIN_PERSON_HEIGHT_FRAC` | 0.15 | a colleague across the room (measured 0.10 of frame height) |
| `HAL_LOOK_AIM_MIN_FACE_HEIGHT_FRAC` | 0.08 | a spurious far face (measured 0.035) |
| `HAL_LOOK_AIM_MIN_CONFIDENCE` | 0.5 | low-confidence noise, e.g. a person rendered on a monitor |

Size is measured on **height**, not area or width: a close subject is routinely clipped left or right
by the frame edge, but their apparent height still scales with distance.

A rejected detection is reported as **no subject at all**, not as a target — so the aim falls through
to hold-or-consult-the-bearing rather than turning to a stranger at the other end of the room. Faces
come from YuNet, which enforces its own threshold and reports no confidence, so only the height gate
applies there.

#### Self-calibrating pixels-to-degrees

The aim does **not** trust a fixed FOV constant. It measures degrees-per-`dx_frac` from what its own
last move actually achieved, and uses that for the next correction; `HAL_LOOK_AIM_FOV_DEG` (100°) is
only the first-step guess before any measurement exists.

This exists because no single constant can be right. The lens is a fisheye: the same device measured
**91° near the frame centre and 229° at the edge**. A constant tuned for the centre crawls at the
edge (four iterations still not centred, then a timeout); one tuned for the edge overshoots the
centre and oscillates.

Guards, because dividing a small shift by a small move turns detector jitter into a wild scale: a
step is ignored unless the head moved >3° and the subject shifted >0.02 of frame, and unless the
shift went the **same** direction as the correction — a wrong-way shift means the subject walked or
the detector jumped to something else, which is not a measurement of optics. The result is clamped to
40–250° and damped by `SCALE_SAFETY` (0.7), deliberately biased low: the measurement is taken at the
current eccentricity but spent at a smaller one, and undershoot costs a step where overshoot
oscillates.

#### Capture timing

Two costs were paid inside the aim's budget before being moved out of it:

- **Detector warm-up.** The first `detect()` loads the model lazily and cost ~9 s on device — enough
  on its own to blow the deadline and the realtime turn's watchdog. It is now pre-warmed on a
  background thread at HAL start (`server.py`), so the first real look runs at the same speed as
  every later one.
- **Stale frames.** Reading `last_frame` straight after a move returns the **pre-move** image, so the
  next correction is computed from a pose the head has already left. On device that produced six
  identical +12.3° corrections with `dx` frozen at 0.241 while the head travelled 61°. The aim now
  holds the camera consumer for the whole aim (without one the device does not capture at full FPS)
  and requires a frame stamped after the servo settled. **No fresh feedback, no move.**

The shutter itself uses `capture_still`, which freezes the servos and waits for quiet. Its settle
scales with the size of the last correction (0.3 s base, +0.0067 s/deg, capped at 0.5 s), because an
aim that exits on its deadline does so immediately after a large swing and a lamp arm is still
ringing past a flat 300 ms — that was the difference between sharp captures on centred aims and
blurred ones on timed-out aims. The cap is deliberately tight: this delay is paid before the user
hears an answer.

#### Debugging a look

Off by default; when disabled every hook is a single cached bool check, so it costs nothing to leave
in place.

```bash
HAL_LOOK_DEBUG=true          # per-look trace dirs under drivers/tracking/look_logs/
HAL_LOOK_DEBUG_FRAMES=false  # keep the trace, skip the per-step JPEGs
```

Each look writes `<timestamp>_<status>/` containing:

| file | what it answers |
|---|---|
| `step_NN_*.jpg` | what the detector locked onto each iteration — green box, green line at box centre, red line at frame centre. The gap between the lines **is** `dx`. |
| `capture.jpg` | the frame actually sent to the model |
| `result.json` | the decision trail: per step `saw` / `dx_frac` / `conf` / `scale` / commanded yaw / resulting pose, plus the bearing consulted |
| `profile.json` | stage timings, and `waiting_on_model_ms` |

The status in the directory name (`OK_realtime_handled`, `OK_delegated`, `OK_fallback`) says which
path answered the turn, so a bad answer can be attributed before opening anything.

`waiting_on_model_ms` is the one to read first: it is total minus everything the device did itself,
and it separates "the lamp is slow" from "the lamp finished in 2 s and then waited 24 s for the
model". Sub-stages are nested inside their roll-up and excluded from the device total, so the
residual is honest. The same numbers appear on one `LOOK-PROFILE` log line per look.

### Search sweep — four ways in, all of them affordable

Distinct from the look-aim and still kept off the capture path: the aim runs inside a live turn under
a deadline, and a sweep takes seconds. What changed is that "affordable" now includes two cases the
lamp decides for itself. A sweep is entered when:

- the user asks outright — *"where are you?"*, *"can you find me?"*, *"find my keyboard"*
  (`skills/servo-control`). This one is a **blocking curl inside the turn**, not a
  `[HW:/servo/search:...]` marker. Markers fire after the reply text is already written, so a
  marker-driven search finishes into a turn that has ended: the lamp sweeps, finds the thing, and
  the answer goes nowhere. It is also a 40 s call against `fireHWCall`'s 5 s client, which used to
  abandon the request before reaching its own `flow.Log` — so the sweep did not even appear in the
  Monitor. The agent waits for the body and answers from it, the way `/api/vision/look` already
  works.
- they accept an offer after a failed look — *"I can't see it. Want me to look around?"*
- **the look-aim is about to give up** — before `look_lost` claims *"I can't find you"*, which until
  now it said having only turned toward a remembered bearing. A bearing is a guess about where
  someone *was*, not a search, so the phrase should be earned. The aim's deadline **stops counting**
  for the duration (`t_end += time.monotonic() - swept_at` in `aim_for_look`): that deadline exists so
  a live turn never stalls in *silence*, the `look_searching` announcement has already dealt with
  that, and charging the sweep against a budget it cannot fit in would mean never sweeping at all.
- **the gaze watcher has been alone too long** — `HAL_GAZE_SWEEP_AFTER_S` (30 s) with nobody seen, or
  a repoint that turned to the bearing and found nobody there. Nobody asked for this one, which is
  why it is the only entry with a cooldown — see *Looking around on its own*.

`POST /servo/search` — sweeps for a subject. Body (all optional): `{"target": "cup", "exhaustive": true}`.

- `target` defaults to `"person"`. `person`/`face` use the closest-subject policy with a face
  fallback; any other noun goes to the same YOLOv8n/YOLOWorld chain `/servo/track` uses. Before this
  the target was accepted by the function and dropped on the floor — every sweep looked for a person,
  so "look around for my keyboard" ended at the first bystander.
- `exhaustive` defaults to `false`, which returns at the first sighting — right for "where are you?"
  and for every object search. `true` is a **survey of people**: it walks the whole ring at every
  bearing (the only mode that looks above the horizon), counts sightings and returns home. It is
  **ignored for an object target** and logged as such: an agent that passed it for "find my doll"
  got a lamp that saw the doll five times, went home, and reported a find with no picture. A
  search for a thing always stops at the first sighting, centres on it and stays there.
- Coverage is `bearings x looks per bearing`: 3 x 6 = **18 looks** normally, 3 x 9 = **27**
  exhaustive. Budget roughly **2 seconds per look**.

The response is a structured body, not one prose string:

```json
{"status": "ok",
 "message": "found keyboard at yaw +85 after 7 look(s) across 2 bearing(s)",
 "found": true, "target": "keyboard", "kind": "keyboard",
 "found_at_yaw": 85.0, "found_at_roll": -45.0, "centred": true,
 "image_path": "/root/.codex/media/hal-snapshots/snap_1757500000000.jpg",
 "looks_visited": 7, "bearings_visited": 2}
```

- `looks_visited` counts LOOKS and `bearings_visited` counts base positions. They are different
  quantities and are reported apart: the field was once called `stops_visited`, counted looks, and
  was rendered as `"after N stop(s)"` against `MAX_STOPS = 3` — so an exhaustive sweep truthfully
  reported "after 27 stop(s)" out of a possible 3, and the agent narrated from that string.
- `centred` says whether the fine correction reached the deadband on **both axes**. `false` still
  means **found** — the aim is simply off. On a hit the sweep runs `aim.centre_on_box` *before*
  `_straighten_head_onto` (the object is proven in view from that pose; straightening first was
  device-observed to cost the find), then straightens from wherever the correction left the base.
  The loop is `aim_for_look`'s yaw correction plus gaze's pitch correction — gaze's
  `GAZE_PITCH_DEG_PER_FRAME` / `GAZE_PITCH_MAX_STEP_DEG`, its device-verified sign (a box above
  centre tilts the camera **up**, which is the *decreasing* direction on the pitch joints) and
  `servo_follow.distribute_pitch` across base/elbow/wrist — issued as one absolute move. Before any
  of this the head was pointed at `yaw + roll`, the look *direction* of the stop, leaving an edge
  detection ~50° off-axis while the call reported a find. Every exit that follows a move takes one
  more fresh frame (`_final_look`) so the result describes where the lamp points *now*, not the
  frame from before its last move. The probe is `_sticky_probe`: all candidates via
  `detect_candidates`, nearest to the previous box wins, and a candidate that moved **away** from
  centre on the same side — or further than one step could carry it — is another instance and
  counts as a miss. Two keyboards on one desk used to have the loop chase whichever scored higher
  each frame. The loop does **not** score the remembered bearing; a sweep finds an object as often
  as a person, and teaching the estimator that a keyboard is where the user sits is how the bearing
  quietly rots.
- `image_path` is the winning frame, written into the active runtime's `media/hal-snapshots` — the
  same capped, rotated pool `/camera/snapshot?save=true` uses (`_SNAPSHOT_MAX` = 20). It carries the
  detection box drawn on it, via `look_debug.encode_annotated(..., centre_lines=False)`: the box is
  the answer, while the green/red full-height dx lines are the aim's working and would land on top
  of each other in the middle of a centred frame. The frame and the box always come from the **same**
  grab — drawing a pre-correction box on a post-correction frame puts the rectangle beside the object.
  When the centring loop never got a frame (no fresh frame, an abort, a failed nudge) the sweep falls
  back to the frame that triggered the hit, so a find always has something to show. A miss writes
  nothing: `image_path` is `null`.
- The path is surfaced to the user as a thumbnail by os-server; the agent cannot read the JPEG.

At each bearing the base holds still and the head walks a ring of looks — centre, left, round the
bottom, out to the right, and (exhaustive only) over the top. The corners go to **full** roll and
**full** pitch rather than cos(45) of each, so a rounded square rather than a circle: that way each
corner sees as far to the side as the left/right looks and as far down as the bottom look, adding new
ground instead of re-covering the middle.

**Only `wrist_roll` and `wrist_pitch` move during a look.** The base turns once per bearing and the
arm never reshapes itself. An earlier design spread the tilt across `base_pitch`, `elbow_pitch` and
`wrist_pitch` via `servo_follow.distribute_pitch` — right for a tracking correction, wrong for a
sweep: device-observed 2026-09-09 the upward tier put `elbow_pitch` at +35.8 and `base_pitch` at
+10.6, extending the arm up and back far enough to look unstable, and it stepped the base through
every bearing again for each tier. `distribute_pitch` allocates against per-joint travel; nothing in
it knows about the arm's balance.

The upward half is clamped against `WRIST_PITCH_MIN`: resting near −73 there are only ~16 degrees of
headroom, less than the ring's 25. Measured on lamp-ac82 2026-09-09, `wrist_pitch` reaches −89.6 up
and −16.6 down without stalling, so `PITCH_TRAVEL_MIN/MAX` in `constants.py` (−33..+32) does not
describe this arm.

**Three stops: the remembered bearing first, then right, then left** — `seed`, `seed+90°`, `seed−90°`,
clamped to the mechanical range rather than dropped. The seed goes first because the sweep stops on
the *first* subject it sees, and "first" has to mean the person who was asked about: with pure
left-to-right ordering the sweep found a colleague at another desk (yaw −102°) while the user sat at
the seed, −12°, which it never reached. After the seed it goes right, then left, because the base
swinging back and forth across centre reads as agitation once the head is also looking around at each
stop. One reversal on the way out is enough.

**Where it leaves the arm.** Nothing found → back to the pose the sweep started from, rather than
frozen wherever the last look left the head. Aborted → the same: a click means "stop searching and
attend to me", and the pose an interrupted sweep freezes in is not a resting one — the head can be
cocked 45° over, facing a wall. Found → the head is straightened by turning the *base* as far as the
head was turned, so the camera keeps pointing at the subject with the head level.

**With no bearing yet** — a fresh unit, or one whose bearing was reset — the sweep first rests the arm
on the idle recording's own pose rather than starting from wherever it happens to stand. A loop that
has been walking the head around does not leave it in a pose anyone chose, and a sweep from a camera
aimed at the desk is thorough about the wrong hemisphere. The idle pose is by construction one the
lamp is designed to rest in, so "not aimed at the floor" comes from the pose itself and needs no
separate pitch check.

**At each stop the head looks around** — `wrist_roll` to −45°, 0°, +45°, always in that direction.
The smoothness comes from the stop order, not from reversing the head: a stop ends looking at
`yaw+45°`, and the next stop to the right opens at `yaw+90°` with the head at −45°, which is *the same
direction*. **The base turn and the head turn are sent as one move**, so the two rotations cancel and
the camera holds its line while the lamp rearranges itself underneath. Move the base first and the
head second and the view flies out to `yaw+135°` and comes back — traced on device as
+48° → +138° → +48°, a 90° out-and-back wobble at every handover. This is why the base can step 90° without leaving seams: with
a ~100° lens, one yaw stop sees a continuous `yaw±95°` (roll −45 covers `yaw−95…yaw+5`, roll 0 covers
`yaw±50`, roll +45 covers `yaw−5…yaw+95`), so three stops cover `seed±185°` — the whole circle. Each is a stop, not a pan-through: a head still moving gives a
blurred frame and a detector that misses what is plainly in view. `wrist_roll` rather than more
`base_yaw` because the two are not equivalent to watch — turning the whole lamp reads as a camera on a
turntable, turning the head at a fixed body reads as something looking around. Roll pans the view
while leaving the horizon level, so it cannot tilt the camera toward the floor part-way through.

Steps are `STEP_DEG` (90°). The tiles still overlap, but the overlap is bought by the head rather
than by a small base step: as the paragraph above works through, one yaw stop plus its three
`wrist_roll` looks sees a continuous `yaw±95°`, so a 90° step leaves no seam. Stops are
clamped to the ±135° mechanical range, and the head is given `SETTLE_S` to stop ringing before each
frame is read, since a moving head yields a blurred frame and a detector that misses what is in view.

Aborted by the physical button like the aim, and it never sweeps while the camera is disabled — a
search is a lot of conspicuous movement to perform when the user has asked the device not to look.

**Handing the body back.** Every exit of the sweep ends in `move_and_hold` — the hit centres on
the subject, a miss and a survey `_restore` the seed pose — and `move_and_hold` leaves the body
with nothing playing (`_current_recording = None`, `_idle_settled`), held "until the next
play/emotion/idle command". Nobody used to issue that command: on lamp-ac82 (2026-09-14) "Find my
keyboard" centred on the keyboard and stayed there until a HAL restart, the same
`[preempt] dropped recording 'idle' for a direct move` signature gaze had already fixed for its
speech reacquire. The sweep now calls `tracking/body.py: release_to_idle_later()` after it
releases servo ownership: a find keeps pointing at the object for `HOLD_AFTER_FIND_S` (8 s) so the
reply plays over the pose, a miss goes back to idle at once. The handback runs on a daemon timer
so `search_for_subject` still returns immediately to the turn waiting on it, and it dispatches
`play(idle)` only if nothing owns the body (tracking, hold mode, zero mode) **and** nothing has
started playing since — an emotion inside the window is left alone; the animation loop returns to
idle by itself when it ends. Scheduling a new handback cancels the previous one. The window is a
fixed constant, not tied to the end of speech: tracking has no speak-end hook to wait on.

> Not built: an LED cue while sweeping. Transient LED state lives behind the route request models, so
> driving it from here would mean HTTP loopback (which this codebase avoids) or duplicating the
> restore bookkeeping — and a cue that fails to restore would strand the lamp's LED. Worth doing
> properly rather than partially.

### Speaking while it searches

A lamp that silently swivels away mid-question looks broken. One that says *"where are you?"* while
doing it reads as trying to help.

os-server owns the phrases, the language resolution and the WAV cache
(`system/lib/i18n/fillers.go`, pools `look_searching` / `look_found` / `look_capturing`); HAL only
decides **when**, via `POST /api/sensing/filler` with `{"pool": "..."}`.

| State | When | Default |
|---|---|---|
| `look_searching` | the first step toward the remembered bearing | **on** (`HAL_LOOK_AIM_SPEAK`) |
| `look_found` | a subject appears **after** a search was announced | on (same flag) |
| `look_still_searching` | the midpoint of a sweep — stop 2 of 3, head centred (`_say_at_the_midpoint`) | on (`HAL_LOOK_AIM_SPEAK`) |
| `look_capturing` | the aim actually moved before the shutter | on (`HAL_LOOK_AIM_SPEAK_CAPTURE`) |

The gating matters more than the phrases. **Nothing is said when the subject is already centred** —
that capture completes in a few hundred milliseconds, so every phrase here is conditional on the aim
having actually moved. *"There you are"* only fires as the resolution of an announced search, never
on its own. Searching announces **once** per sweep rather than per step, plus a single
`look_still_searching` line at the midpoint — the sweep is ~20 s long, and without it the opening
phrase and the verdict sit either side of twenty seconds of silence, which reads as a lamp that has
stopped rather than one that is looking. And the capture line fires only when the aim actually moved
(`res.aimed and res.iterations > 0`): an aim that moved nothing says nothing, and — the part that was
wrong until this branch — neither does an aim that searched and **failed**, which used to follow
*"I can't find you"* with *"let me take a look"*.

A fast, silent, correct capture is already the good outcome — speech is reserved for the moments the
user is genuinely left waiting.

**Two ways a pool goes quiet without failing**, both fixed rather than worked around:

- **A key missing from a known language** used to resolve to nothing. `FillerForTool` fell back to
  English only for an unknown *language*, never for a missing *key* inside a known one — and the zh
  maps carried no `look_*` entries at all, so every look filler on a Chinese device was dropped with
  no log line and no error. Both zh maps now carry the pools, and a key missing anywhere falls back
  to English. An English phrase on a Chinese device is wrong, but wrong *out loud*: somebody hears it
  and reports it, where silence is invisible. `TestEveryPoolIsTranslatedInEveryLanguage` keeps the
  fallback from becoming the normal path.
- **A pool that was never prewarmed** renders live at the TTS provider on its first fire (~1–2 s) and
  the late audio races the speech after it — which the user perceives as the filler being cut off.
  `PrewarmFillers` used to walk a hand-maintained list of tool names that no `look_*` pool was ever
  added to. It now enumerates `i18n.AllPoolKeys()`, so a new pool is covered by existing.

Neither failure mode reports anything at any level: `PlayPoolFillerNow` returns at `len(phrases) == 0`
and `POST /api/sensing/filler` answers 200 either way. A pool that resolves empty is a lamp that
simply does not speak.

## Gaze framing — keeping the user in shot

Everything above is asked for: a look, a track, a search. This section is the watcher in
`hal/drivers/tracking/gaze.py` doing it unprompted, so that when the user does speak the camera is
already pointed somewhere useful. All of it is downstream of `HAL_GAZE_WAKE` (see
`physical-controls.md`) — that flag gates the whole watcher, not just the wake opener its name
suggests, so with it off none of the behaviour below runs.

The unifying constraint: **nobody asked for any of this**, so every loop is bounded — a dead zone, a
cooldown, a step budget. A lamp that corrects its framing is attentive; one that corrects constantly
is a head that nods along.

**The loops measure all the time, but only move while a conversation is open.** `_conversation_open()`
reads `voice_service.conversation_focus_active()` — the wake-word follow-up window, refreshed by every
opener (phrase, click, presence, gaze). Measuring cannot be gated: the wake gate reads the window
*before* speech, so those samples must already exist. Moving can, and must be.

Why: outside a conversation a correction cannot survive on this arm. `idle.csv` plays absolute frames
and pins `base_yaw` at −2.40 — **1.58° of swing across the whole recording** — so the next idle frame
overwrites the correction and the loop measures the same offset again. Device-observed 2026-08-26 with
nobody speaking: thirteen pan corrections in twenty minutes, alternating direction, every one starting
from idle's own band. Not drift — an immediate overwrite, repeating forever.

Release is therefore free: stop correcting and idle reclaims the arm within a frame, so there is no
pose to restore and no ownership to drop. The check fails **closed** when voice is unavailable. The one
exception is a *prompted* climb, which a speech-driven repoint asked for and which would otherwise
leave the head aimed at a chest for the whole utterance.

### Vertical centring, and why it reads a median

A desk lamp sits below head height, so its camera points at a chest. The correction is a median of
the vertical offset over `HAL_GAZE_PITCH_WINDOW_S`, **not the latest frame** — and that is the whole
reason this loop converges.

`wrist_roll` is a second *aiming* axis on this arm (device-proven by pinning every other joint and
varying only roll: the horizon stayed level while the view panned), and the idle recording sweeps it
~32° every ~10 s, forever. So the offset a single frame reports is the framing error **plus** a
periodic disturbance from wherever idle's roll happens to be. Measured on three frames with the
subject unmoved: `dy` +0.101 at roll −1.8° against +0.143 at roll +29.3° — 0.042 of frame height from
roll alone, about 28% of the dead zone, on a loop that used to fire every 4 s from one sample. A
median over an idle cycle cancels that periodic disturbance while a real framing error survives it.

**The window is also the loop's pacemaker.** `_dy_estimate` refuses to return anything until the
samples span `WINDOW_S × 0.8`, and the buffer is cleared after every correction — so the refill, not
`HAL_GAZE_PITCH_COOLDOWN_S`, is the real gap between steps. At the original 12 s that meant a ~9.6 s
wait before the head moved at all, which is a long time to sit visibly badly framed with the user
right there. The window is now **6 s**, giving ~4.8 s. The trade is explicit: half a roll period
instead of a whole one, so some of idle's disturbance survives into the median. The loop re-measures
after every move, so that costs an extra iteration rather than accuracy — but if the head starts
hunting, this is the number to put back.

The correction is spread across all three pitch joints by `distribute_pitch`
(`servo_follow.py`), weighted `base_pitch` 0.20 / `elbow_pitch` 0.60 / `wrist_pitch` 0.20 — the elbow
carries the most on a healthy arm. Allocation is **headroom-aware in the requested direction** and runs
two passes: the first honours the weights, the second hands any overflow to whichever joint still has
room. A single joint driven alone hits its mechanical stop while the face is still out of frame.

**A move that does not arrive is noticed.** `move_and_hold` reports nothing, so a stalled joint used
to be indistinguishable from a working one and the loop re-commanded the same unreachable target
every ~10 s forever — observed across six consecutive corrections with `elbow_pitch` reading +12.3
while being sent to +25.8. Worse, re-commanding a stall is what heats a servo into giving up, so the
loop manufactured the condition it kept tripping over. Now the arm is polled until it arrives; a joint
short by more than `HAL_GAZE_PITCH_LAND_TOL_DEG` is rested for `HAL_GAZE_PITCH_STALL_REST_S` and its
target backed off `HAL_GAZE_PITCH_STALL_BACKOFF_DEG`, so the retry does not lean on the stop again.

**Corrections are not held against idle.** The idle recording is absolute on every joint and loops
forever, so within one cycle it walks the camera back toward the pose it was recorded at — on a desk,
the keyboard. An idle anchor (`HAL_GAZE_IDLE_ANCHOR`) used to counter this by shifting the whole loop
onto the last good pose; **it has been removed**. So the pull-back is live: a correction decays over
an idle cycle rather than persisting, and the loop re-corrects on its next window. That is the main
reason the same offset can reappear after a successful correction.

| Knob | Default | Meaning |
|---|---|---|
| `HAL_GAZE_PITCH` | `true` | Vertical centring on/off. |
| `HAL_GAZE_PITCH_WINDOW_S` | 6 | Median window, and the loop's real cadence — a correction waits for samples spanning 80% of it (~4.8 s). Was 12, which spanned a whole idle roll cycle but made every step wait ~9.6 s. |
| `HAL_GAZE_PITCH_MIN_SAMPLES` | 8 | Floor for acting on a partly-filled window. |
| `HAL_GAZE_PITCH_PROMPT_MIN_SAMPLES` | 2 | Floor when the climb was asked for directly — the torso path reports a constant −0.5, so more samples add no information. |
| `HAL_GAZE_PITCH_DEAD_ZONE_FRAC` | 0.15 | Offset (fraction of frame height) that counts as centred enough. The aim is the face *inside* the frame with room around it, not perfectly centred. |
| `HAL_GAZE_PITCH_DEG_PER_FRAME` | 45 | Degrees per full frame height. A seed, not a calibration — the loop re-measures every step. |
| `HAL_GAZE_PITCH_MAX_STEP_DEG` | 15 | Largest single correction. |
| `HAL_GAZE_PITCH_COOLDOWN_S` | 4 | Floor between corrections. |
| `HAL_GAZE_PITCH_MOVE_S` | 1.0 | Move duration. Separate from the aim's 0.25 s: gaze makes one unrequested move every ~10 s and nothing waits on it, so it can afford to be gentle. |
| `HAL_GAZE_PITCH_SETTLE_S` | 1.8 | Settle before reading back — a read taken mid-glide reports a short move that is merely still moving. |
| `HAL_GAZE_PITCH_LAND_TOL_DEG` | 2.0 | Shortfall that counts as a stall. |
| `HAL_GAZE_PITCH_STALL_REST_S` | 60 | How long a stalled joint is left out. Matched to measured recovery. |
| `HAL_GAZE_PITCH_STALL_BACKOFF_DEG` | 2.0 | Stop short of where it stalled. |
| `HAL_GAZE_SNAPSHOT` / `_KEEP` | `true` / 40 | Annotated frame beside every correction, in `SNAPSHOT_PERSIST_DIR/sensing_gaze/`. The log says the median was −0.41 of frame height; it cannot say whether that was the user, a colleague, or a coat on a chair. |

### Climbing to find a face above the frame

A person box that **touches the top edge** means the body continues past it, so the head is above and
the camera is aimed too low. That is the only evidence used: an unclipped body with no face means the
head *is* in frame and simply was not detected — turned away, in profile, backlit — and climbing then
aims at the ceiling for no reason.

Fixed steps rather than proportional ones, because the torso says "the head is up there somewhere" and
never how far. Proportional control needs an error signal; this is a search.

| Knob | Default | Meaning |
|---|---|---|
| `HAL_GAZE_FACE_SEARCH_STEP_DEG` | 15 | One climb step. |
| `HAL_GAZE_FACE_SEARCH_MAX_STEPS` | 4 | ~60° of climb, then stop. The evidence stays true however far the neck has travelled, so acting on it forever is a loop, not a search. |

**Where a working height is remembered** — `hal/drivers/tracking/face_height.py`, at
`/var/lib/hal/face_height.json` (`HAL_FACE_HEIGHT_PATH`), deliberately **separate** from
`user_bearing.json`. The bearing answers *"which way is the user?"* and is read by look-aim, the
search and the repoint; writing height into it would change what look-aim restores on every call. This
answers a different question — *"how high must this camera aim to see a head from here?"* — and only
the gaze pitch loop reads it. They also go stale differently: a bearing is a guess about a person, who
moves, so it is retired when it stops working (three failed predictions); a height is a fact about
the furniture, and simply keeps. The full pose is recorded because a pitch
angle only means something alongside the rest of the posture, but **only the pitch joints are applied
on restore** — yaw belongs to the bearing and the pan loop, and handing it back here would give two
subsystems the same steering wheel.

### Panning, and why it is lazier than pitch

Vertical framing fails in one direction — a user stands and leaves the top of frame — so it is worth
chasing. Horizontal drift is mostly someone shifting in a chair, and a lamp that swings to follow
every lean is exactly the twitchiness this whole loop is damped against. Correction is shared between
`base_yaw` and `wrist_roll` via `distribute_yaw`.

The dead zone is nonetheless **narrower** than pitch's (0.10 against 0.15), which looks backwards until
you note that the value tested is the *median* over the window: the window is what rejects leaning and
fidgeting, and making the dead zone do that job a second time only costs the correction it was meant to
allow. It started at 0.22 on the opposite reasoning and device testing killed it — deliberately moving
side to side at a desk peaked at `dx` +20%, so the loop measured the movement correctly and declined
every time.

| Knob | Default | Meaning |
|---|---|---|
| `HAL_GAZE_YAW` | `true` | Pan correction on/off. |
| `HAL_GAZE_YAW_WINDOW_S` / `_MIN_SAMPLES` | 12 / 8 | As pitch. |
| `HAL_GAZE_YAW_DEAD_ZONE_FRAC` | 0.10 | Fraction of full frame width (`dx` runs −0.5 … +0.5). |
| `HAL_GAZE_YAW_DEG_PER_FRAME` | 40 | Degrees per full frame width. |
| `HAL_GAZE_YAW_MAX_STEP_DEG` | 12 | Largest single correction. |
| `HAL_GAZE_YAW_MOVE_S` | 1.0 | Neither pan joint fights gravity, but the whole lamp turning is a bigger visual event than a head tilt. |

### Repointing at the remembered bearing

**Speech drives this, and nothing else does.** When somebody speaks and the watcher has no usable face
evidence, it turns to the remembered bearing and then checks, for `HAL_GAZE_REPOINT_VERIFY_S`, whether
that worked. The request is made on the mic thread and consumed on the watcher thread
(`_consume_speech_repoint` → `_maybe_repoint(force=True)`), which skips both the absence wait and the
cooldown.

It used to *also* fire by itself after `HAL_GAZE_REPOINT_AFTER_S` (12 s) with nobody visible. That was
removed. A repoint scores the bearing, and firing on "nobody happens to be in frame" — constantly true
for a desk lamp when you lean out of view, turn to a colleague, or stand up — scored a bearing as wrong
on evidence that says nothing about whether it is. Three such strikes delete the estimate, so a correct
bearing was erodible by an empty chair. Scored only on utterances, each strike means something: someone
spoke, the lamp turned to where it thought they were, and they were not there.

Three further behaviours are worth stating because each was a bug first:

- **A body counts as finding the user.** The verifier tracks faces and bodies on separate clocks; a
  torso at the bearing means the bearing was *right*. Scoring it as a miss deleted correct bearings
  while the user sat in front of the lamp.
- **A repoint must end on a face.** Landing on a body is a half-success, so it prompts the climb
  above rather than returning "found them" — which is why the climb has a `_PROMPT_MIN_SAMPLES` of 2.
- **It will not turn away from a face already in frame.** If a face was seen within
  `HAL_GAZE_REPOINT_SKIP_IF_FACE_S`, a speech-triggered reacquire declines: after a climb has found
  the user's face *above* the bearing, obeying the bearing means turning back down to look at nobody.
- **The hold ends with the utterance.** A speech-triggered reacquire points the lamp with
  `move_and_hold`, which drops whatever recording was playing and sets `_idle_settled` — correct
  for the utterance, wrong afterwards, because nothing else re-arms idle. The lamp simply stopped
  moving and stayed frozen until a HAL restart (measured on lamp-0c89 03/09/2026: `[preempt]
  dropped recording 'idle' for a direct move` at 16:23:40, still motionless at 16:25:58, no further
  log). `on_speech_end` now hands the body back with `dispatch(play, idle)` — the same handover the
  tracker does when it ends — and skips it when tracking, hold/zero mode or a scene owns the body,
  since each of those has its own release.

Every decline is logged with its reason (`[gaze] no repoint: …`), throttled so a standing condition
prints once a minute rather than once a pass.

### Looking around on its own

If a repoint turns up nothing, `_verify_repoint` calls the same `/servo/search` sweep documented above
with `confirmed_miss=True`. Since the repoint above is speech-driven, so is the sweep: the lamp
searches because somebody spoke and it could not find them, never because a room merely looks empty.
An absence trigger (`HAL_GAZE_SWEEP_AFTER_S`) still exists in `_maybe_sweep` but nothing reaches it —
the watcher loop no longer calls the sweep at all. The cooldowns still apply, and the two exist because
the two situations are not alike.

Fifteen minutes is right for *"I have a bearing, it missed, stop thrashing"*. It is wrong for *"I have
no idea where you are"*, because then the sweep is the only way to find out and the lamp is forbidden
from trying — device-observed: three failed repoints dropped the estimate, and the lamp then sat unable
to repoint (nothing to turn to) and unable to sweep (11 minutes left) while the user was talking to it.

`confirmed_miss` skips the absence wait by design — a repoint that moved and missed is the strongest
evidence there is, so there is nothing to wait for. A successful sweep samples a fresh bearing on the
spot.

| Knob | Default | Meaning |
|---|---|---|
| `HAL_GAZE_SWEEP` | `true` | Autonomous look-around on/off. |
| `HAL_GAZE_SWEEP_AFTER_S` | 30 | Nobody seen for this long. Longer than `HAL_GAZE_REPOINT_AFTER_S` (12 s) so the cheap move is always tried first and the ~20 s sweep stays the escalation, not the reflex. |
| `HAL_GAZE_SWEEP_COOLDOWN_S` | 900 | Between sweeps when a bearing exists. |
| `HAL_GAZE_SWEEP_COOLDOWN_LOST_S` | 120 | Between sweeps when there is no bearing at all. |

### Remembered user bearing

`hal/drivers/tracking/user_bearing.py` folds sightings into one estimate at
`/var/lib/hal/user_bearing.json` (`HAL_USER_BEARING_PATH`). One place, not a histogram — the lamp
only ever needs one pose to return to.

**It stores a full servo pose, not a single angle** (schema v2; a v1 file migrates and keeps its
learned direction). `bearing_deg` remains as the yaw component so callers that only want a direction
need not know joint names, and it is *derived from* `pose["base_yaw.pos"]` so the two can never
disagree. Yaw alone is not enough to look at someone: pitch is spread across base/elbow/wrist, so a
head left pointing at the floor sweeps the floor in a circle no matter how right the yaw is. Each
joint gets its own EMA at the same rate as the yaw; a relocation replaces the pose outright rather
than averaging, since the old posture describes the old place.

**Confidence measures how well learned the estimate is, not how recent.** It rises with sightings —
~1.0 after `CONFIDENCE_FULL_SAMPLES` (8) — and then **stays there**. It does not decay with age.
Recency is still reported, as `age_s`, for any caller that wants it; nothing currently gates on it.

That is a deliberate reversal. Confidence used to halve every six hours, on the reasoning that a
stale estimate should report itself as stale rather than look authoritative. The reasoning was sound
and the arithmetic was not: sightings arrive far more slowly than the half-life consumed them, so on
a real device the estimate lost ground faster than it gained it and sat permanently below the
threshold that would let anything use it — a bearing nobody was allowed to consult is not a cautious
bearing, it is an absent one.

Staleness is now the prediction-failure path's job instead, which is a sharper signal: rather than
guessing from a clock that a bearing has gone bad, the lamp turns to it, looks, and scores what it
finds. Three clustered failures retire it outright — see *Noticing that the lamp has been moved*
below. A bearing either still works or it is dropped; it no longer fades into a grey zone where it is
too weak to use and too strong to replace.

Sightings reach it two ways:

- **From a look aim**, when the subject ends within **2%** of frame centre — tighter than the aim's
  own framing tolerance, and deliberately so: at frame centre the servo position **is** the bearing,
  with no pixel→angle conversion and therefore no dependency on the disputed camera FOV constant.
- **From the passive sampler** (`bearing_sampler.py`), every `HAL_BEARING_SAMPLE_INTERVAL_S` (300 s).
  The aim-only path recorded roughly two sightings a day, which is too slow to build an estimate the
  aim will act on — confidence grows with sightings, and at that rate a fresh device spends days
  below the threshold with the one thing that rescues a look when nobody is visible sitting unused.
  (It was worse still when confidence also decayed on a six-hour half-life: the estimate lost ground
  faster than it gained it and could never settle. That decay has since been removed — see below.)
  The sampler **never moves the lamp**: it reads a frame and the current servo positions, and
  recovers the bearing arithmetically as `yaw + dx × scale`.

The sampler declines rather than guess. Horizontal offset is tolerated only to
`HAL_BEARING_SAMPLE_MAX_DX_FRAC` (0.25), because that correction leans on the very FOV constant the
aim exists to avoid trusting. It also skips while the body is aiming or tracking, while the camera is
disabled, and takes the detector lock non-blocking so a user's question never waits on it.

**It learns from faces only, never from `person` boxes.** A person box says where a body is, and a
body fills the frame whenever the camera happens to be aimed low — so learning from one memorises
the posture that was pointing at the desk and calls it "where the user is". Device-observed: 22
samples, confidence 0.99, and a stored posture with `wrist_pitch -78` that could not see a face at
all. Every consumer downstream then restored that posture faithfully and found nobody, which reads
as the lamp being broken rather than as the bearing being wrong. A face in frame proves the opposite
by construction: this posture sees a head, so restoring it will see one again.

**The posture is recorded wherever in frame the face sat.** The vertical gate that used to guard it
(`HAL_BEARING_SAMPLE_MAX_DY_FRAC`, since deleted — it had stopped gating anything and only shaped a
log string) was written for person boxes, where a centred torso said nothing
about whether the head was in frame. Keeping it for faces was self-defeating: while the camera is
aimed low every face sits near the top edge, so every sighting failed the gate, so no posture was
ever stored, so there was nothing to restore and the camera stayed low — device-observed `dy` of
-15.8% then -41.2%, two sightings, and a remembered "pose" holding only a yaw. A posture that catches
the user at the frame edge is imperfect; it is also incomparably better than one pointing at the
desk, and the per-joint EMA walks it toward centre as the framing it enables improves.

Each sample writes an annotated frame to `/var/lib/hal/snapshots/sensing_bearing/`
(`HAL_BEARING_SNAPSHOT`, newest 30 kept, oldest evicted) — **including the detections it rejected**,
labelled with why, since "it ignored a far stranger" and "it saw nothing" look identical in the
estimate. Servable at `GET /api/sensing/snapshot/sensing_bearing/<name>`.

Angles are averaged **linearly, not circularly**: `base_yaw` is a bounded ±135° servo range that does
not wrap, so a circular mean would be wrong at the extremes.

Outliers are damped rather than accepted — someone crossing the room must not flip the estimate — but
`OUTLIER_STREAK` consecutive far sightings are treated as a genuine relocation and accepted wholesale.

**Consumed by aim priority 3** (above) once confidence passes `MIN_BEARING_CONFIDENCE`. Inspect it
to check the maths and the sign:

```bash
curl -s localhost:5001/servo/bearing     # includes the full pose
cat /var/lib/hal/user_bearing.json
```

A `known` bearing with an empty `pose` means the estimate predates the pose schema and has not been
re-sighted yet: a search will restore direction but not head height until the next sighting fills it
in.

`bearing_deg` should settle near where the user actually sits. An estimate sitting **mirrored about
zero** means the yaw sign is inverted — the failure this file is most exposed to, because it is
open-loop and nothing corrects it.

### Noticing that the lamp has been moved

The bearing is stored in **lamp-relative** coordinates, so picking the lamp up or rotating it on the
desk invalidates it instantly — while the file still looks perfectly valid.

Nothing on this device can observe that directly:

| Approach | Why not |
|---|---|
| IMU / accelerometer | none fitted — absent from the BOM and from HAL |
| Servo feedback | `base_yaw` measures the head against the **base**. Rotating the whole lamp moves the world, not the joint. |
| Reboot as a hint | weak both ways — lamps reboot without moving, and move without rebooting |

So it is **inferred from failed predictions**: when aim priority 3 turns to the remembered bearing
and finds nobody, that is a miss. `PREDICTION_MISS_LIMIT` misses drops the estimate, and it rebuilds
from live sightings.

Three guards keep ordinary life from looking like a relocation:

- **A single miss is not enough** — the user may simply be out of the room.
- **A hit resets the streak**, so occasional absences never accumulate.
- **Misses must be clustered** (`MISS_STREAK_WINDOW_S`). A moved lamp fails every attempt from the
  moment it moved; a user who is sometimes in another room produces isolated misses spread over
  weeks. Without the window those become indistinguishable once enough time passes.

And a miss is only ever counted when the lamp **actually looked and found nothing**. Camera disabled,
no frame, deadline, button abort, and the occlusion hold all return without scoring — privacy mode in
particular must never erase where the user sits.

This self-heals for **any** cause (lamp moved, furniture rearranged, user changed desk) without ever
needing to know which one happened.

#### How big a move has to be before anything needs to detect it

Most moves never reach the miss-counting above, because the estimate corrects itself:

| Lamp rotated by | What corrects it |
|---|---|
| **< half the camera FOV** (~30°) | the user is **still in frame** at the stale bearing, so the aim finds and centres them anyway — and that sighting records the new correct yaw. Plain EMA pulls the estimate over. **Nothing detects the move; nothing needs to.** |
| up to `OUTLIER_DEG` (45°) | not visible from the stale bearing, but found while stepping toward it. The sighting is still under the outlier threshold, so it is folded in at full weight. |
| beyond `OUTLIER_DEG` | sightings look like outliers, so `OUTLIER_STREAK` consecutive ones are accepted as a relocation. |
| far enough that the user is never found | the miss streak drops the estimate and it rebuilds from scratch. |

So the machinery above is only for the **last** case. A lamp nudged on the desk is handled by the
estimate's own update rule, which is why the small case is also the quietest — the lamp never learns
it moved, and does not need to.

**Inspect and reset:**

```bash
curl 127.0.0.1:5001/servo/bearing              # {"known":true,"bearing_deg":-18.5,...}
curl -X POST 127.0.0.1:5001/servo/bearing/reset
```

The reset is also wired to speech via `skills/servo-control` — *"I moved you"*, *"you're in a new
place"*. Automatic detection needs several failures before acting, which is right for avoiding false
positives but slow when the user already knows the lamp moved.
