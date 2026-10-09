# Camera Lifecycle — Reactive On/Off

Camera should be **reactive**: on when needed, off when idle. Saves CPU/RAM, respects privacy.

## Current State

- `POST /camera/disable` / `POST /camera/enable` — manual toggle from web monitor
- Camera feeds sensing: face recognition (ONNX InsightFace), pose/motion (ONNX), light level (pixel mean), presence (pixel diff)
- Voice pipeline (mic) runs independently of camera
- Sound perception runs independently of camera

## Design: Camera On/Off as the Only Switch

No new abstractions. Camera on = full sensing. Camera off = vision sensing stops, audio sensing continues.

### When camera is OFF

- `_tick()` skips all vision perceptions (face, pose, motion, light)
- Sound perception still runs (mic-based)
- Wake word detection still runs (voice_service)
- TTS still works
- Servo/LED still work
- Web monitor Camera tab shows "Disabled" with Enable button

### When camera is ON

- All perceptions run as normal
- Face/pose ONNX inference every other tick (existing optimization)

## Auto-Off Triggers

### 1. Scene: night

When `/scene` activates `night` → turn camera off.
- User going to sleep, no need for vision
- Sound perception stays for wake word / sound spike

### 2. Emotion: sleepy

When `/emotion` receives `sleepy` → turn camera off.
- Same as night, agent explicitly put lamp to sleep

### 3. Presence idle timeout

When presence state transitions to `away` (no motion for away_timeout seconds) → turn camera off.
- Nobody in the room, no point running vision
- Sound spike or wake word will turn it back on

### 4. Voice command: "don't look" / "stop watching"

User says "Lamp, đừng nhìn" / "don't watch me" / "privacy mode" → agent calls `[HW:/camera/disable:{}]`.
- Explicit user request for privacy
- Only voice command or web toggle can re-enable

### 5. Scene: focus, reading, movie

When `/scene` activates `focus`, `reading`, or `movie` → turn camera off.
- User already present and engaged, no need to keep detecting
- Presence is already known from the scene activation
- Saves CPU during long sessions
- Camera re-enables when scene changes or user leaves (detected by sound/wake word)

## Auto-On Triggers

### 1. Wake word detected

Voice service detects wake word ("Looney", etc.) → turn camera on.
- User is actively engaging, may need visual context
- Always works because mic runs independently

### 2. Sound spike (loud noise)

Sound perception detects RMS above threshold while camera is off → turn camera on.
- Someone may have entered the room
- Camera on → face detect → presence.enter if person found
- If no face detected after N seconds, camera off again (avoid false positive drain)

### 3. Scene change to active scene

When `/scene` changes from night/sleep to energize or relax → turn camera on.
- User or agent activated a daytime scene

### 4. Emotion change from sleepy to anything else

When `/emotion` receives non-sleepy emotion → turn camera on.
- Agent is actively interacting, may need vision

### 5. Morning cron / scheduled

Cron job at configured wake time (e.g. 6:00 AM) → turn camera on.
- Ready for morning routine before user says anything

### 6. Voice command: "look" / "nhìn xem"

User says "Lamp, nhìn xem" / "look at me" / "camera on" → agent calls `[HW:/camera/enable:{}]`.
- Explicit user request

### 7. Telegram/web chat with visual context needed

Agent needs snapshot (camera skill) → one shell call checks `GET /camera` first: `disabled` → the agent answers "camera is off" and stops (no auto-enable, no capture); `has_frame: false` → "camera is not working" and stops; otherwise `POST /api/vision/look` captures and answers the question (thinking off; `"read_text": true` only for reading text/labels/brands, which uses a 1280px frame and thinking). The check rides in the same tool call so it adds no model round. On a voice turn `/api/vision/look` says a short line before the photo ("Taking a look.") and another once it is taken ("Got it — give me a sec."), so the user knows when the picture was taken during the long describe wait. For "turn left/right and tell me what you see" the same shell call aims after the check and before the look (`servo-control` skill), so a camera that is off means no movement. `/api/vision/look` holds the body under HAL's internal `look` owner from the first cue through the photo, then releases it. The agent never calls `/servo/hold` in a turn-and-look ("hold it there" there means the look's own hold); it holds only for a pose request without a look.

## Manual Override

Web monitor Camera tab toggle always works. Manual disable stays until:
- User manually re-enables
- OR a voice command explicitly re-enables

Manual override does NOT get auto-overridden by scene/emotion/presence triggers. Only explicit user action (voice command, web toggle) clears manual override.

## Implementation Plan

### HAL (Python)

1. **`server.py`**: ✅ Done — Already has `/camera/disable`, `/camera/enable`, `_camera_disabled` flag.

2. **`_camera_manual_override` flag**: ✅ Done — `/camera/disable` sets override, `/camera/enable` clears it. `_auto_camera_off()` / `_auto_camera_on()` helpers respect override.

3. **Scene endpoint** (`/scene`): ✅ Done — After setting scene:
   - `night`, `focus`, `reading`, `movie` → `_auto_camera_off("scene:{name}")`
   - `energize`, `relax` → `_auto_camera_on("scene:{name}")`

4. **Emotion endpoint** (`/emotion`): ✅ Done — preset "camera" field drives behavior:
   - `sleepy` has `"camera": "off"` → `_auto_camera_off("emotion:sleepy")`
   - Any non-off emotion when camera is auto-off → `_auto_camera_on("emotion:{name}")`

5. **Presence service**: ❌ Skipped — camera stays on when away. Turning off would break auto-greeting (face detect → presence.enter) when user returns. CPU cost not worth losing autonomous detection.

6. **Sound perception**: ❌ Skipped — camera off cases (scene/emotion/manual) all have explicit re-enable paths. Sound spike adds complexity (30s timer, face check) without covering new cases.

7. **`_tick()` in sensing_service**: ✅ Already works — `frame = None` when camera stopped, vision perceptions skip. No change needed.

### Lamp (Go)

8. **Voice service / wake word**: ❌ Skipped — wake word → agent → emotion preset `"camera": "on"` already re-enables camera automatically. No need for early enable.

9. **Healthwatch**: ✅ No change needed — camera state is independent of health monitoring.

### OpenClaw Skills

10. **Camera skill**: ✅ Done — voice/chat toggle; capture checks `/camera` in the same shell call and answers "off" / "not working" instead of enabling or capturing (auto-enable before capture removed 2026-09-16).

11. **Scene / Emotion SKILL.md**: ❌ Skipped — camera toggle is automatic in server.py via preset `"camera"` field. Agent doesn't need to know.

### Lamp Go (intent.go, lib/hal)

12. **intent.go + lib/hal/client.go**: ❌ Skipped — local intents call `/scene` endpoint which already handles camera via preset. No Go-side camera helpers needed.

### Web Monitor

13. ✅ Already done — Camera tab has Enable/Disable toggle.

## Skill Changes Needed

### Camera SKILL.md — ✅ Done

- ✅ Description updated with toggle trigger phrases
- ✅ Examples for disable/enable via `[HW:/camera/disable:{}]` and `[HW:/camera/enable:{}]`
- ✅ Auto-enable before capture rule added
- ✅ Rule: never toggle camera proactively without user request

### Servo-control SKILL.md

- No change needed — camera is separate from servo hold

### New consideration: agent should NOT call camera disable/enable proactively

- Only user-initiated voice commands or system triggers (scene, emotion, presence) should toggle
- Agent must never decide on its own to turn camera off/on without user asking

## Digital Zoom

Software zoom for focusing on small subjects (e.g. a laptop screen during a video call so Lamp can read it).

### API

- `POST /camera/zoom` body `{"zoom": <float>}` — sets zoom factor, range `1.0` (no zoom) to `5.0`. Returns updated `CameraInfoResponse`.
- `GET /camera` includes `zoom` field with current factor.

### How it works

Zoom is applied **inside the capture loop** (`drivers/camera/video_capture_device.py::_video_capture_loop`) right after rotate, before `last_response` is set. The loop center-crops the frame by `1/zoom` and resizes back to the original dimensions, so every downstream consumer reads the same zoomed buffer:

| Consumer | Source | Sees zoom? |
|---|---|---|
| `/camera/snapshot` (vision tool) | `camera_capture.last_frame` | ✅ |
| `/camera/stream` (web UI) | `camera_capture.last_frame` | ✅ |
| Sensing orchestrator (face recog, motion, pose, emotion) | `camera_capture.capture()` → `last_response` | ✅ |
| Tracker service | `camera_capture.last_frame` | ✅ |

### Trade-off

Zoom > 1 narrows the effective field of view for **every** consumer:

- ✅ Faces on a small surface (laptop screen) become large enough for InsightFace to detect → presence.enter can trigger from a video-call participant.
- ✅ Vision tool snapshot reads on-screen content clearly.
- ❌ People/objects outside the center crop are invisible to face recog / motion / pose / tracker.
- ❌ Active tracking can lose target if it moves outside the cropped region.

Treat zoom > 1 as a **temporary mode** for a specific subject. Reset to `1.0` (web UI Reset button or `POST /camera/zoom {"zoom": 1.0}`) when finished to restore wide sensing.

### Storage

Zoom state lives on the device instance (`LocalVideoCaptureDevice.zoom`). Not persisted — resets to `1.0` on server restart. No auto-reset on camera disable/enable.

### Web UI

Monitor → Camera tab → Live Stream card has a Zoom slider (1.0×–5.0×, step 0.1, debounced 200 ms POST) with a Reset button. Slider value shows amber when zoomed to warn about narrowed FOV.

## Exposure & Frame Rate

The USB camera's auto-exposure stretches integration time in low light (~60ms), capping delivery at **~16fps at every resolution** — this is the exposure clock, not USB bandwidth (720p and 4K both cap at 16fps). Pinning **manual** exposure avoids that throttle, but manual with high gain drives the camera ISP into an unstable state that corrupts colors (green/magenta posterized frames) and sticks for the whole capture session — observed on multiple devices with gain 255 and gain 192. HAL therefore defaults to **auto** exposure; switch to manual only when a stable frame rate matters more than adaptive brightness, and keep gain ≤ ~144.

### Config (env, read by `config.py`)

| Var | Default | Meaning |
|---|---|---|
| `HAL_CAMERA_AUTO_EXPOSURE` | `auto` | `auto` uses the camera's adaptive auto-exposure (default; brighter/adaptive but throttles fps in low light). `manual` pins exposure using the values below — risks the ISP color corruption with high gain. |
| `HAL_CAMERA_EXPOSURE` | `330` | Manual exposure time, V4L2 `exposure_absolute` ×100µs: `200`=20ms (30fps), `330`=33ms (≈30fps ceiling), `500`=50ms (≈20fps). |
| `HAL_CAMERA_GAIN` | `96` | Sensor gain (camera-specific, e.g. 0–255). Brightens without costing fps, but adds noise; values above ~144 risk the ISP color corruption. Written in both modes (see below). |
| `HAL_CAMERA_BRIGHTNESS` | _(unset)_ | Brightness offset (camera-specific, e.g. -64..64). Digital lift. |

The defaults apply even with no `.env` entries. To pin the frame rate on a device, set `HAL_CAMERA_AUTO_EXPOSURE=manual` per device — the manual fallbacks (330 / 96) are values verified color-stable; the old defaults (`manual` / 500 / 255) are the known-toxic combo.

### How it works

`_apply_camera_controls()` (`drivers/camera/video_capture_device.py`) runs after the resolution is set on open **and on every device reopen** — a fresh open resets the camera to defaults, which would otherwise silently drop manual exposure and re-introduce the FPS throttle. It maps to V4L2/UVC controls via OpenCV: `CAP_PROP_AUTO_EXPOSURE` (1=manual, 3=auto), `CAP_PROP_EXPOSURE`, `CAP_PROP_GAIN`, `CAP_PROP_BRIGHTNESS`.

In `auto` mode the control is actively set to 3 (aperture-priority) on every open, not left untouched: UVC cameras retain manual exposure/gain across HAL restarts, so a leftover manual state from an earlier configuration would otherwise survive an `.env` switch to `auto`. **Gain** is pinned to `HAL_CAMERA_GAIN` in `auto` mode too: UVC auto-exposure only moves integration time, so a gain the camera retained at max from an earlier manual run blows out a lit room regardless of auto-exposure (lamp-4ace, 2026-09-18: gain 128/128, ceiling and windows white; 64 restored the image). Note `.env` changes only take effect after `systemctl restart hal` — the running process keeps the env it started with.

### Trade-off

Frame rate vs brightness is a hard physical trade-off in a dark room: the max exposure that still holds 30fps is ~33ms (`HAL_CAMERA_EXPOSURE=330`); a brighter image needs a longer exposure (fewer fps) or more gain (noisier). The stream endpoint is separately capped at `HAL_CAMERA_STREAM_FPS` (default 10), so the monitor's live view does not reflect the capture rate.

## Device Selection

By default the camera is opened by index: `HAL_CAMERA_INDEX` (default `0`) → `/dev/video0`, with a fallback scan (`/dev/cam` udev symlink, then indexes 0–5). A bare index is fragile — plugging another USB device or a changed boot enumeration order can shuffle `/dev/video<N>`.

`HAL_CAMERA_NAME` (optional) selects the camera by role alias or hardware name instead, mirroring how audio devices are picked (`resolve_camera_device_id()` in `drivers/camera/video_capture_device.py`). It is either an absolute path or a case-insensitive substring of the v4l2 device name (e.g. `OPENAICAM`). Resolution order:

0. **Absolute path** (e.g. `/dev/device-camera`, the udev `SYMLINK` keyed on the camera's vid:pid in `99-lamp-device.rules`) — returned as-is when it exists. This is the camera counterpart of `asound.conf`'s `device_speaker`: `.env` names a role, udev decides which hardware fills it, so a camera swap is a udev change only. The lamp standard `.env` uses this. A missing path falls back to the index (step 3).
1. **`/dev/v4l/by-id` capture symlink** (`...-video-index0`) whose name contains the needle — returned as the symlink path, so reopens keep following it even when the kernel renumbers `/dev/video<N>` after a replug or USB power-cycle.
2. **sysfs name scan** — `/sys/class/video4linux/video<N>/name` match (lowest N first), skipping UVC metadata sibling nodes (same name, non-zero `index` attribute, cannot capture).
3. **Legacy index fallback** with a warning when nothing matches (camera absent or renamed).

Unset `HAL_CAMERA_NAME` keeps the exact legacy index behavior.

## Failure Recovery

The capture loop (`drivers/camera/video_capture_device.py`) recovers from two distinct device failures, both by releasing and reopening the V4L2 device via `_reopen_with_backoff()` (retry with exponential backoff 1s→30s, never permanently exits the loop while HAL runs; MJPEG, resolution and exposure are re-applied on every reopen):

- **`read()` failure** — USB autosuspend or a transient V4L2 error makes `read()` return `ret=False`. One 1s retry, then reopen.
- **USB runtime power management** — when HAL opens a USB camera, it resolves that camera's own sysfs device dynamically and writes `power/control=on`, so the kernel does not autosuspend it during an idle capture interval. The setting is repeated after HAL starts and after a watchdog USB rebind. CSI/non-USB cameras, kernels without that control, and permission failures are skipped without interrupting capture. This prevents a known source of UVC resume/ISP wedges; the color watchdog remains the fallback for firmware faults that occur for another reason.
- **ISP freeze** — the camera keeps delivering the **same buffer** with `ret=True` (seen on the UVC cam with manual exposure/gain), so the `read()`-failure path never fires while every consumer (realtime look, sensing, tracking, snapshot) silently works on a stale scene. A watchdog compares a subsampled signature of each frame; byte-identical frames for 10s (`_FREEZE_REOPEN_S`) cannot come from a live sensor and trigger a reopen. Log line: `Camera frozen — identical frames for Ns, reopening device`.
- **ISP color corruption** — the same wedged ISP can instead keep delivering **changing** frames whose chroma is garbage: posterized green+magenta, red+magenta, or a broad magenta/deep-magenta palette, with every v4l2 control correct (seen live on the SunplusIT cam right after a close/open cycle). The freeze watchdog cannot see this, so a second watchdog checks the subsampled frame in HSV (throttled to ~1 check/s): a frame is corrupt when either (a) extreme-saturation green covers ≥10% (`_COLOR_GREEN_FRAC`) **and** magenta ≥0.8% (`_COLOR_MAGENTA_FRAC`), (b) red ≥15% (`_COLOR_RED_FRAC`) **and** magenta ≥1% (`_COLOR_RED_MAGENTA_FRAC`), or (c) broad magenta ≥15% (`_COLOR_MAGENTA_DOMINANT_FRAC`) **and** deep magenta (HSV hue 155–169) ≥10% (`_COLOR_DEEP_MAGENTA_FRAC`), at saturation ≥100 (`_COLOR_SAT_MIN`, value ≥60). The companion hue requirements keep a legitimate single-hue red or pink LED spill from triggering recovery. Corruption must be uninterrupted for 12s (`_COLOR_CORRUPT_REOPEN_S`; one clean frame resets) before triggering the same recovery as a freeze. Thresholds were calibrated from live corrupt captures: green 0.19 / magenta 0.012, red 0.23–0.42 / magenta 0.10–0.35, the first stream failure red 0.258 / magenta 0.016, and the blue-LED specimen magenta 0.24 / deep-magenta 0.16. Log line: `Camera color corruption — posterized green/magenta frames for Ns, reopening device`.

### ISP deep-stuck → USB power-cycle escalation

Sometimes the ISP wedges **deeper** than a V4L2 reopen can fix: frames come back posterized green/pink or freeze again right after the reopen, even though every v4l2 control is correct (auto_exposure=3, sane gain). Observed on the SunplusIT UVC cam (`1bcf:28cc`); the only verified fix short of a reboot is power-cycling the USB port.

Both ISP watchdogs (freeze and color corruption) share one escalation ladder via `_recover_isp_fault()`:

- **Trigger** — the **first** ISP fault (`_ISP_FAULT_ESCALATE_COUNT` = 1) escalates immediately. A plain V4L2 reopen does **not** reset the ISP — device-verified, reopening a wedged ISP re-triggers the exact green/posterized glitch, so the watchdog fires again seconds later and the loop hammers itself into a self-perpetuating churn. Going straight to a power-cycle avoids that; the 10-minute cooldown below (not a fault count) is what bounds how often an actually-dead camera gets cycled. `read()`-failure reopens do **not** count (those a plain reopen does fix). Faults are still tracked over a sliding window (`_ISP_FAULT_WINDOW_S`, 600s) for the cooldown accounting.
- **USB path resolution** — dynamic, never hardcoded: walk up the sysfs parent chain from `/sys/class/video4linux/video<N>/device` until the node carrying `idVendor` (the USB device), take its basename (e.g. `1-1`). If the camera is not USB-backed (e.g. a CSI sensor), escalation is skipped with a log line and the plain reopen path is kept.
- **Power-cycle** — write the bus path to `/sys/bus/usb/drivers/usb/unbind`, wait ~3s (`_USB_REBIND_DELAY_S`), write it to `.../bind` (HAL runs as root), then wait up to 15s (`_USB_DEVNODE_TIMEOUT_S`) for `/dev/video<N>` to re-enumerate. **Once the node reappears, settle a further 5s (`_USB_SETTLE_AFTER_BIND_S`) before handing control back to `_reopen_with_backoff()`** — the node can reappear in <1s but the ISP firmware is still booting, and reopening mid-boot brings the frame back corrupt again; device-verified, a power-cycle + immediate reopen comes back green, the same cycle with the settle comes back clean. Best-effort: any sysfs failure logs and falls back to the plain reopen.
- **Cooldown** — at most one power-cycle per 10 minutes (`_USB_POWER_CYCLE_COOLDOWN_S`); a physically dead camera must not loop unbind/bind forever. While in cooldown, faults keep taking the plain reopen path.
- **Log line** — `Camera USB power-cycle (ISP fault — plain reopen won't clear an ISP wedge; N fault(s) in Ws)`, then `Camera USB power-cycled (1-1) — /dev/video0 is back, settling 5s`.

## Edge Cases

- **Guard mode + camera off**: ✅ Done — guard SKILL.md step 1: `[HW:/camera/enable:{}]` before enabling guard. Overrides manual disable.
- **Face enroll while camera off**: `/face/enroll` uses uploaded image, not live camera. No conflict.
- **Snapshot request while camera manually disabled or privacy-locked**: Return 409. Automatic pauses still allow temporary snapshots; concurrent snapshot requests serialize capture so their temporary stops cannot interrupt each other.
- **Multiple rapid triggers**: Debounce camera start/stop — don't restart if already starting. `camera_capture.start()` already handles "already started" case.
- **Sound spike false positive loop**: After sound spike auto-on, if no face detected within 30s → auto-off again. Prevents camera staying on from random noise.
