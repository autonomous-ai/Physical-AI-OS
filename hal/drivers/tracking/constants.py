"""Tuning knobs shared across the tracking package."""

from hal.config import TRACKING_MAX_DURATION_S

# Vision-pipeline max width (px). The ViT tracker AND the detectors (YuNet / local YOLO
# / remote YOLOWorld) run on a frame downscaled to at most this width.
VISION_MAX_WIDTH = 640

# Fast loop target FPS — tracker update on Pi runs ~15-25ms/frame. Servo commands are
# decoupled: the follower wakes at ~33 Hz and coalesces tiny setpoint changes, so loop
# fps no longer directly sets bus-write/click rate.
FAST_LOOP_FPS = 15

# Hardware velocity limit for tracking (Feetech STS3215 Goal_Velocity register, unit
# steps/s on a 4096-step revolution → 150 ≈ 13°/s!).
TRACKING_GOAL_VELOCITY = 0
# Hardware acceleration for tracking (Feetech STS3215 Acceleration register).
# 254 = max (default, snappy). Lower = gentler ramp up/down → less jerk.
# Range: 0-254. ~30 gives smooth glide without being too sluggish.
TRACKING_ACCELERATION = 30

# Camera field-of-view in degrees (horizontal). Used to convert px offset → degrees.
CAMERA_FOV_DEG = 60.0

# Tiered dead zone (per-axis, as fraction of frame). Inside ±INNER: true zero — servo
# rests, PID integral clears (CENTERED).
DEAD_ZONE_INNER_PCT = 0.02
DEAD_ZONE_YAW_PCT   = 0.07
DEAD_ZONE_PITCH_PCT = 0.05
DEAD_ZONE_CREEP_GAIN = 0.12

# Steady-state Kalman for a constant-velocity model. Replaces the plain EMA so the servo
# follows a *predicted, gated* centroid instead of the raw ViT bbox center.
AB_ALPHA = 0.6
AB_BETA = 0.2
AB_GATE_PX = 200.0
AB_LEAD_S = 0.20
# Velocity decay applied when a measurement is gated, so a persistent bad lock
# coasts to a stop instead of running away on stale velocity.
AB_GATE_DECAY = 0.7
AB_MAX_GATED_STREAK = 3

# YOLO background re-detect interval (seconds). Local YOLOv8n runs ~300-700ms/call on
# Allwinner A523. At 500ms interval it saturated all CPU cores → camera MJPEG stream
# stalled.
YOLO_REDETECT_S = 1.5

YOLO_MAX_MISS = 30

MISS_COAST_FRAMES = 6

# Cooldown after servo fire (seconds) — ignore motion detection while camera
# stabilises after a move. Prevents servo shake → fake MOVE → immediate re-fire loop.
SERVO_COOLDOWN_S = 0.10

# Servo-worker wake interval. SmoothDamp uses measured monotonic elapsed time;
# commands smaller than SERVO_COMMAND_MIN_DELTA are coalesced, so a wake does
# not necessarily produce a bus write.
SERVO_SUBSTEP_SLEEP = 0.030
# Clamp a delayed worker iteration so a USB/serial stall cannot be turned into one
# oversized SmoothDamp step when the loop resumes.
SERVO_SUBSTEP_MAX_DT_S = 0.060
SERVO_COMMAND_MIN_DELTA = 0.08

# The servo worker used to step a FIXED number of degrees toward the goal each tick,
# then snap the last step.
SERVO_SMOOTH_TIME   = 0.32
SERVO_MAX_SPEED_DPS = 55.0
SACCADE_SMOOTH_TIME   = 0.20
SACCADE_MAX_SPEED_DPS = 100.0
SACCADE_OFFSET_FRAC = 0.22
SACCADE_EXIT_FRAC = 0.12

# Pitch distribution across 3 joints — the PREFERENCE, not the whole story.
PITCH_WEIGHT_BASE  = 0.20
PITCH_WEIGHT_ELBOW = 0.60
PITCH_WEIGHT_WRIST = 0.20

# Travel each pitch joint actually has, measured on lamp-ac82 2026-08-25 by commanding
# each joint alone and reading the position error `/servo/move` reports.
PITCH_TRAVEL_MIN = {
    "base_pitch.pos":  -18.0,
    "elbow_pitch.pos":  -4.0,
    "wrist_pitch.pos": -33.0,
}
YAW_TRAVEL_MIN = {
    "base_yaw.pos":   -100.0,
    "wrist_roll.pos":  -55.0,
}
YAW_TRAVEL_MAX = {
    "base_yaw.pos":    100.0,
    "wrist_roll.pos":   55.0,
}
PITCH_TRAVEL_MAX = {
    "base_pitch.pos":   30.0,
    "elbow_pitch.pos":  58.0,
    "wrist_pitch.pos":  32.0,
}

# Elbow servo polarity.
ELBOW_PITCH_SIGN = -1.0

MAX_TRACK_DURATION_S = TRACKING_MAX_DURATION_S

# Yaw distribution across the two joints that PAN the camera.
YAW_WEIGHT_BASE = 0.75
YAW_WEIGHT_ROLL = 0.25

# Servo position limits (degrees).
YAW_MIN, YAW_MAX = -135.0, 135.0
WRIST_ROLL_MIN, WRIST_ROLL_MAX = -90.0, 90.0
BASE_PITCH_MIN, BASE_PITCH_MAX = -90.0, 30.0
ELBOW_PITCH_MIN, ELBOW_PITCH_MAX = -90.0, 90.0
WRIST_PITCH_MIN, WRIST_PITCH_MAX = -90.0, 90.0

# Detection quality filters (applied by every detector AND by the loop's
# ghost-lock sliver / bloat checks).
DETECT_MIN_AREA_RATIO = 0.003
DETECT_MAX_AREA_RATIO = 0.80
DETECT_MIN_CONFIDENCE = 0.15

# Bbox-trust guard (ViT bloat protection). ViT can dissolve its lock into a box that
# overflows the whole frame — it stops tracking the object and "tracks" everything.
BBOX_FREEZE_RATIO = 1.0
# Relative bloat: the frame-overflow check above misses the common failure where ViT
# balloons to 20–45% of the frame (still < 1 frame) while the real target is a small
# face (~3%).
BLOAT_HOLD_MULT = 3.0

# Detection gating + reinit debounce (SORT/ByteTrack-style outlier rejection).
YOLO_AREA_GATE_MULT = 4.0
REINIT_COOLDOWN_S = 0.5
# Detection-tracker center distance beyond this fraction of the frame diagonal
# means the lock is genuinely lost → reinit immediately, ignoring the cooldown.
LOST_CENTER_FRAC = 0.5

# Ghost-lock detection via tracker confidence (ViT only).
CONFIDENCE_THRESHOLD = 0.15
LOW_CONF_WINDOW = 15
LOW_CONF_STOP_COUNT = 8
# Floor for firing the servo PID at all, even while the detector still confirms the
# target.
SERVO_MIN_CONF = 0.25
# When the detector (YuNet / YOLO) hasn't confirmed for TRUST_TRACKER_S, fall back to
# ViT's own confidence. Below this → freeze servo, wait for detector.
TRACKER_TRUST_CONF = 0.4
TRUST_TRACKER_S = 2.5
# Slack on top of one redetect cycle, so ordinary scheduling jitter (a frame
# that arrived late, a detect thread that started a beat behind) does not read
# as a missed confirm.
TRUST_MARGIN_S = 0.5
# No detector confirm at all for this long → the lock is a ghost, stop.
STOP_NO_YOLO_S = 20.0

# PID gains for servo control (industry pattern: PyImageSearch face tracking).
PID_YAW_KP, PID_YAW_KI, PID_YAW_KD = 0.015, 0.002, 0.002
PID_PITCH_KP, PID_PITCH_KI, PID_PITCH_KD = 0.02, 0.002, 0.0025
PID_OUTPUT_MAX_DEG = 5.0
PID_INTEGRAL_MAX = 30.0

# The position PID only reacts to accumulated error, so a target moving at a steady
# speed is always chased in catch-up bursts (the "follows in jerks, not a smooth pan"
# feel).
VFF_GAIN = 0.9
# Cap on the per-fire dt used to turn the feedforward rate (deg/s) into a
# per-fire step (deg) — a long gap between fires can't inject a huge lurch.
VFF_MAX_DT_S = 0.20
VFF_MOVING_MIN_PXS = 40.0
