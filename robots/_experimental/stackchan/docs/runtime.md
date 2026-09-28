# Running Stack-chan through HAL on a host

This experimental profile runs the repository's real `hal.server` with the
Stack-chan motion driver on a computer. It declares only motion and system;
it is not a complete Autonomous-compatible device, an OTA release target, or
a media integration. The inert `host` board supplies no local GPIO or servo
bus. `HAL_SIMULATE=0` selects the real remote transport.

## Start HAL

Run from the repository root with the existing HAL Python environment and
dependencies available at `hal/.venv`. The host-side configuration belongs to
this profile at `rootfs/opt/hal/.env`, following the other robot profiles.
Copy it to a private local file; `cp -n` preserves an existing configuration:

```bash
mkdir -p "$PWD/.local/stackchan"
cp -n robots/_experimental/stackchan/rootfs/opt/hal/.env "$PWD/.local/stackchan/.env"
chmod 600 "$PWD/.local/stackchan/.env"
```

Edit `.local/stackchan/.env` to set the firmware device ID, a distinct shared
token of at least 32 characters, and paths to an existing TLS certificate/key.
Keep real credentials out of the tracked profile. This file configures HAL on
the computer; the ESP32 requires its own matching firmware configuration.
Then start HAL with that env file:

```bash
export HAL_USERS_DIR="$PWD/.local/stackchan/users"
export HAL_STRANGERS_DIR="$PWD/.local/stackchan/strangers"
export HAL_LOG_DIR="$PWD/.local/stackchan/logs"
export HAL_STATE_DIR="$PWD/.local/stackchan/state"
export OS_CONFIG_PATH="$PWD/.local/stackchan/config.json"
mkdir -p "$HAL_USERS_DIR" "$HAL_STRANGERS_DIR" "$HAL_LOG_DIR" "$HAL_STATE_DIR"
HAL_BOARD=host DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  HAL_SIMULATE=0 HAL_MODE=developer PYTHONPATH=. \
  hal/.venv/bin/python -m uvicorn hal.server:app \
    --env-file "$PWD/.local/stackchan/.env" --host 127.0.0.1 --port 5001
```

`HAL_MODE=developer` disables HAL HTTP access restrictions. Keep the explicit
HTTP bind at `127.0.0.1`; the body listener is a separate authenticated TLS
service. The writable directories accommodate HAL's shared startup paths even
though this profile does not enable face recognition or other media features.
`OS_CONFIG_PATH` isolates this development run from the device's OS config;
the file may be absent for this standalone motion test. When pairing with a
local OS instance, point it at that instance's actual configuration instead.
Do not use multiple Uvicorn workers: each worker would try to bind port 8765.

Firmware must initiate `wss://<host-reachable-name>:8765/stackchan/body/v1`,
validate the certificate for that host, and send `Authorization: Bearer <token>`.
Its hello must match `STACKCHAN_DEVICE_ID` and advertise the required timed
motion, measured-position, halt/hold and torque-release capabilities. HAL
waits for the connection; startup does not automatically move the body.

For an isolated plain-WS experiment only, clear both TLS values in the local
env file and explicitly set `STACKCHAN_BODY_ALLOW_INSECURE_WS=1` there; the firmware must use the matching
`ws://` URL. TLS remains the normal configuration.

## Check without moving

```bash
curl --fail http://127.0.0.1:5001/device
curl --fail http://127.0.0.1:5001/health
```

`/device` should identify `stackchan` and the `stackchan` motion driver.
`/health` reports `servo: false` while disconnected and `servo: true` after
the authenticated handshake. That establishes connection availability only.
No media capability is expected. The OS Go HAL client currently uses the
constant `http://127.0.0.1:5001`, so this direct integration expects OS and HAL
on the same host; there is no `HAL_BASE_URL` override in that client.

## Firmware and qualification

The separate [Stack-chan integration repository at the inspected revision](https://github.com/glifocat/stackchan-autonomous/tree/9a5209596b97c257b6b2c1f6ff6bec91c44f8112)
contains a firmware patch and its own standalone HTTP bridge. Pulling this OS
repository does not switch that bridge to HAL, configure or flash the ESP32,
or install a service. Configure the firmware's destination for this HAL body
listener when testing this path.

The inspected firmware acknowledges a timed move as `completed` with state
`scheduled`; HAL renews the controller lease while interpolation runs.
`motion.halt` clears that lease, so HAL reacquires it before a subsequent move.
Compatible source does not identify what is flashed on a particular body.

The profile caps speed at an unqualified 10 degrees/second. Driver ranges are
yaw ±30 and pitch ±15 degrees around the legacy 45-degree pitch midpoint;
gravity-rest targets yaw 0, pitch -15. These are not a physical calibration.
Read [SAFETY.md](../SAFETY.md) before supervised motion qualification. Local
startup and protocol tests do not establish full CTS or physical safety.

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

For a supervised bench session, use the same environment and private env file as
the normal host startup above, replacing the Uvicorn target with this factory:

```bash
HAL_BOARD=host DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  HAL_SIMULATE=0 HAL_MODE=developer PYTHONPATH=. \
  hal/.venv/bin/python -m uvicorn \
    robots._experimental.stackchan.commissioning:create_app --factory \
    --env-file "$PWD/.local/stackchan/.env" --host 127.0.0.1 --port 5001
```

Run one HAL process only: this entrypoint reuses HAL's lifecycle and body
connection. It does not start a second bridge or alter the standard OS entrypoint.
Enable `STACKCHAN_HOME_COMMISSIONING_ENABLED=1` in the private env file only for
the supervised session, then disable it afterward. Existing `/servo/home*` bench
scripts must migrate to `/stackchan/home*`; there is no shared-route alias.

## Run the OS on the same host

Keep HAL running in the first terminal. In another terminal, use the existing
OS development target with the same profile directory and a separate state
folder:

```bash
make os-dev DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  OS_STATE_DIR="$PWD/.local/stackchan/os"
```

Point HAL's `OS_CONFIG_PATH` at `$PWD/.local/stackchan/os/config/config.json`
before starting HAL when sharing this configuration. The OS target defaults to
the Codex runtime; install/configure the chosen runtime and run its gateway
using the existing [simulator development instructions](../../../../docs/simulator.md).
The Stack-chan setup replaces the `make sim` HAL step: do not launch a second
HAL or the separate bridge on port 5001. No agent runtime or ESP32 firmware is
installed by this profile.
