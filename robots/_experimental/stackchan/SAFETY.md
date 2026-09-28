---
schema: autonomous.safety.v1
motion:
  max_speed: 10
  stop_always: true
---

# Stack-chan experimental safety bounds

## motion

The 10 degree/second ceiling is a conservative, unqualified development
default, not a measured hardware limit. HAL stretches timed moves against
measured positions, including zero and gravity-rest moves; a required duration
over 60 seconds fails closed. Stop remains available independently of motion
ownership. Remote stop depends on transport delivery; the firmware must also
halt and hold on disconnect or controller-lease expiry.

The current driver accepts `base_yaw.pos` in -30..30 degrees and
`base_pitch.pos` in -15..15 degrees. Pitch uses the legacy 45-degree midpoint:
HAL pitch 0 maps to firmware pitch angle 450 in tenths of a degree. Release
targets yaw 0, pitch -15, measures arrival, then requests torque-off. These
ranges and the head-down rest pose are not physically calibrated for a
particular assembly. Confirm calibration, gravity support, torque behavior,
timed speed, stop, Wi-Fi loss and process-loss behavior before qualification.

### Optional home commissioning

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

TLS and a distinct shared body token are required for the normal connection.
The explicit insecure-WS option is restricted to isolated development use.
Host tests and matching firmware source do not qualify physical safety.
