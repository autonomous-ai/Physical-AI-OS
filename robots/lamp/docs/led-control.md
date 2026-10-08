# LED Control — Documentation

Presence returning from idle/away restores the shared user/resting LED state, including the current resting-light preference and active overlay guards. It does not keep a separate color cache or restore an emotion color. Idle dimming uses the saved user base color or the current resting preset; explicit light-off remains off.

During manual Harness capture, recorder/STT readiness switches the indicator to the existing listening preset (Lamp: dim blue `[0, 0, 3]`, speed `0.3`). It stays active even before the first transcript. Finish, cancellation, timeout or failure clears this capture indicator and restores the normal priority policy; subsequent thinking/TTS cues keep their existing behavior. This LED-only cue does not move the servos or change saved preferences.

## Hardware

- **32 WS2812 RGB LEDs** — one ring
- Driver: `rpi_ws281x` (Python, HAL owns)
- FastAPI endpoints on `:5001`

### SPI bit timing

The SPI driver encodes WS2812 bits at 6.4 MHz: `_BIT0 = 0xC0` (~312ns high),
`_BIT1 = 0xFC` (~937ns high). `_BIT1` used to be `0xF8` (~781ns) and individual
pixels dropped 1-bits — a dim solid such as the setup cue `[16,16,16]` came up
with one pixel blue or yellow depending on which channel that pixel misread.
781ns is inside the 580-1000ns datasheet window on paper, but the strip runs at
5V off a 3.3V data line and the slow rise time eats the effective high.

### Boot clear

`RGBService.__init__` blanks the strip as soon as the driver is up, before any
route or effect can paint. WS2812 pixels hold their last latched colour with no
data on the wire, and the SPI pins are re-muxed during kernel boot — stray edges
on the data line leave a few pixels latched to a garbage colour (green shows up
most, being the leading byte of a WS2812 frame). Without the clear that garbage
stays lit until the first LED command, which may be minutes after boot.

### Early Orange Pi boot indicator

When migrating a device that has the earlier `lamp-led-*` units installed,
stop HAL, then stop/disable `lamp-led-boot.service` and stop
`lamp-led-shutdown.service` before removing those two unit files, their enablement
symlinks and the old `lamp-led-boot.py`/`lamp-led-off.py` helpers. Apply the new
rootfs (including the HAL shutdown drop-in), reload systemd and start HAL. A
rootfs overlay alone does not remove renamed files. HAL accepts either boot-unit
name during migration, but two copies of the boot indicator must not be enabled.

`led-boot.service` starts from `sysinit.target.wants` after local filesystems,
before the normal services and without waiting for network or os-server. On the
sun60iw2 lamp it drives 32 LEDs on SPI3.0 with a three-second white breath, ranging
from off to RGB **[3, 3, 3]**, at 20 frames/second. It waits up to ten seconds for
the SPI node without blocking boot. This indicates startup, not readiness; it
cannot signal power-on or a boot failure before Linux/systemd reaches this unit.

HAL synchronously stops the indicator immediately before initializing RGB in
its early LED lifespan, after Python imports. SIGTERM ends the single animation loop, clears twice, flushes LOW
and closes SPI; systemd waits for exit before HAL opens SPI. The stop timeout is
three seconds. Breathing continues through HAL imports; handoff is at RGB initialization,
not full voice/camera readiness. Manual starts while HAL is
active, starting or stopping are refused. The indicator does not restart itself
and does not take over during shutdown. The existing delayed shutdown blackout
remains separate. The boot unit pulls in and starts after the shutdown fallback;
shutdown ordering reverses, so the boot writer exits before the fallback begins
its five-second delay. Without this ordering, a device test showed the fallback
sending black at 103 seconds while the boot writer continued until 109 seconds.

This ships in the lamp device rootfs; package ZIPs preserve the target.wants
symlink. `ConditionPathExists` requires the matching HAL boot-led helper, so an
older HAL skips the indicator. Deploy the updated HAL before enabling it. It takes effect on the next boot after device update/daemon-reload.
Do not start it over a running HAL for testing. Rollback: stop the boot indicator,
remove its sysinit symlink, service and helper, then daemon-reload.
Local tests check frames, cancellation, error cleanup and ownership guards;
systemd unit validation does not establish physical LED timing or colour.

### Concurrent frame writes and clear diagnostics

Solid, per-pixel paint and clear share a driver lock for the entire operation.
Clear holds it across both black-frame writes, the two 10 ms waits, SPI idle
and buffer read-back. An animation cannot repaint the buffer midway and cause
`LED clear did NOT take` to report a false clear failure. A later frame can
still paint after clear returns; effect ownership and cancellation remain the
caller's responsibility. The diagnostic reads software memory, not physical
LED feedback, so a black buffer does not prove the hardware is dark.

### Transient emotion status

Transient expressions (such as `laugh` and `shock`) return `/emotion/status`'s
`current_emotion` to `idle` at the scheduled expression deadline (recording duration
plus 0.5 seconds, 3.5 seconds without a recording, or 2 seconds for shock).
The status timer is independent of LED restoration, so TTS cancelling the LED
restore timer cannot leave the emotion label stuck. Each accepted expression
invalidates the previous deadline, including repeated expressions of the same kind.
Expiry updates status only; it does not move servos or interrupt speech/LED owners.
`idle`, `sleepy`, `listening`, and `thinking` retain their existing lifecycles.
This deadline is not a physical servo completion acknowledgement.

### Graceful shutdown

`RGBService.stop()` first marks the service as closing under the driver lock,
then stops/joins the event worker **without holding that lock**. Solid and paint
handlers recheck the closing state inside the lock, so even a worker that exceeds
the join timeout cannot apply a late frame. Finally stop holds the lock across the
last double-black clear and driver deinitialization, then removes the driver
reference. Repeated stops and late clears are harmless; a clear failure still
closes the driver and propagates the error. Previously clear and deinit ran before
stopping the worker, allowing queued frames to relight the strip or touch a closed
SPI handle. Regression tests use a fake strip and real worker threads; they do not
prove that GPIO remains electrically quiet after the kernel powers down.

### Orange Pi shutdown fallback

The lamp rootfs ships `led-shutdown.service`, pulled in by the HAL unit's
`20-led-shutdown.conf` drop-in. The service starts without touching the LEDs.
At shutdown/reboot, reversed ordering waits for HAL and the boot LED writer to
exit, waits **5 seconds**,
then runs `/usr/local/libexec/led-off.py` while local filesystems remain
mounted. Its stop timeout is **15 seconds**. A HAL-only restart does not run this
independent service's stop action. `ExecCondition` skips boards other than
sun60iw2 or a missing SPI3.0 device; this fallback is not enabled for Raspberry Pi.

The standalone off frame matches the hardware team's reference: 32 black GRB
pixels, 6.4 MHz, 8 LOW primer bytes and 64 LOW reset bytes. It does not import HAL,
run a demo, or change GPIO muxing. It refuses writes while HAL is active or
stopping, propagates SPI failures, and only logs transfer completion, not physical
LED readback. On device `.142`, two observed shutdowns with the five-second
fallback had no residual dot, including TTS/emotion playback; 300 ms did not
resolve the issue. This is a fallback, not proof of the underlying cause.

Deployment is through the **lamp device package/rootfs**, not a HAL-only update.
After manual copying, run `systemctl daemon-reload` then restart HAL to arm it.
Remove older experimental LED/demo units before enabling it so only one fallback
owns the strip. To roll back, stop HAL, stop the fallback, remove its HAL drop-in,
unit and helper, reload systemd, then start HAL. Do not manually stop the fallback
while HAL is running.

The HAL lifecycle also waits up to **20 seconds** for hardware cleanup (previously
5); unfinished or failed cleanup reports shutdown failure and exits nonzero rather
than reporting completion. The systemd HAL limit remains 30 seconds, with room
for the 5-second HTTP drain. No second cleanup is started against a still-running
hardware owner. This fixes observed premature shutdown completion but does not
establish the cause of every residual LED dot.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/led` | LED strip info (count, available) |
| GET | `/led/color` | Ring state: brightest pixel, `on` if ANY pixel is lit, `uniform: false` when the pixels differ (dithered effects, partial paints). Reads every pixel — sampling only pixel 0 reported a lit ring as "off" whenever pixel 0 happened to be dark. `on` is read from the ring even while an effect runs, so an effect painting a black base reports `on: false` instead of claiming light the lamp is not showing; `color` still reports the effect's base colour, which os-server's ambient loop keys off. |
| POST | `/led/solid` | Fill entire strip with one color |
| POST | `/led/paint` | Set per-pixel colors (array up to 32 items), or a gradient with `"gradient": true` |
| POST | `/led/off` | Turn off all LEDs |
| POST | `/led/effect` | Start an effect |
| POST | `/led/effect/stop` | Stop running effect |
| POST | `/led/restore` | Repaint user's saved LED state (or clear if none) |

### Transient writes

`/led/solid`, `/led/paint`, `/led/effect`, and `/led/off` accept an optional `"transient": true` flag. When set, the call paints the strip but does **not** overwrite the saved user LED state. The saved state is restored when the caller (e.g. Claude Desktop Buddy) is done — either via the natural emotion restore timer, or by an explicit `POST /led/restore`. Pulse effects launched with `transient: true` also overlay on the user's saved color instead of black.

### Harness voice confirmation

Harness mode reads `button_led.harness_on` / `button_led.harness_off` from `robots/lamp/presets.json` through the live HAL preset table. While ON, Lamp maintains a dim warm amber `breathing_fine` indicator at RGB `[3, 1, 0]`, speed `0.6` (about five seconds per breath). OFF gives one dim white blink at RGB `[2, 2, 2]`, speed `1.0`, duration `300` ms, then restores the saved user state. The mode watcher and normal LED restoration share `hal/drivers/harness/led.py`; sleep, mic privacy, TTS, music and thinking take priority. Idle ambient breathing cannot replace the indicator. User LED preferences are not overwritten; no RGB service means no LED work. The OFF overlay restores 100 ms after its configured duration.

## Solid Color

```json
POST /led/solid
{"color": [255, 180, 100]}
```

`color` is an `[R, G, B]` array (values 0-255) or a packed `0xRRGGBB` int.

## Paint (Per-Pixel / Gradient)

```json
POST /led/paint
{"colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]]}
```

`colors` is an array of `[R, G, B]` (or packed-int) pixels applied in index order (0-63). Without `gradient`, only the first `len(colors)` pixels are painted — the rest of the strip keeps its previous color.

```json
POST /led/paint
{"colors": [[0, 200, 200], [150, 0, 255]], "gradient": true}
```

With `"gradient": true` the colors are treated as gradient **stops** and linearly interpolated across the whole strip (CSS-gradient style) — the example above fades cyan → purple over all 32 pixels. Works with any number of stops ≥ 1.

Paint stops any running effect first (an effect repaints the strip every ~40ms and would overwrite it) and, unless `"transient": true`, saves the painted pixel list as the user LED state — so emotion animations, TTS waves, and HAL restarts within the same boot restore the exact gradient. For gradients the *expanded* 32-pixel list is saved, not the stops.

## Effects

```json
POST /led/effect
{"effect": "breathing", "color": [255, 100, 50], "speed": 1.0}
```

| Effect | Description | Params |
|--------|-------------|--------|
| `breathing` | Sine-wave brightness up/down | color, speed, `start_at_peak` |
| `breathing_fine` | Same breath, but the fractional level is spread across the ring (spatial dither) instead of truncated per pixel — for dim cues where `breathing` has only 2 usable levels. Never darker than one unit below `color`, never brighter than `color`. | color, speed, `start_at_peak` |
| `candle` | Random flickering candle | color |
| `rainbow` | Hue rotation across strip | brightness (0.0-1.0, level), speed — generates its own hue, ignores `color` |
| `notification_flash` | Quick flash 3 times | color |
| `pulse` | Single pulse from center outward | color, speed |

## Lighting Scenes

```json
POST /scene
{"scene": "reading"}
```

Each scene controls **all peripherals** — not just LED, but also camera, mic, speaker, and servo.

Deactivate: `POST /scene/off` — clears active scene, restores idle LED, re-enables camera/speaker, releases the scene's servo hold. A non-transient LED override (`/led/solid`, `/led/paint`, `/led/off`, `/led/effect`) also ends the scene and releases the scene's hold.

The active scene **survives HAL service restarts** (OTA, deploy, crash): it is persisted to a boot-scoped sidecar (`/tmp/hal-scene-state.json`, keyed to the kernel `boot_id`) and re-activated automatically when HAL comes back up, so the agent's belief ("focus mode is on") stays in sync. A full device reboot intentionally starts scene-less. Transient LED calls (`/led/solid`, `/led/off`, `/led/effect` with `"transient": true`, e.g. the boot breathing effect) overlay the strip without exiting the active scene; only non-transient LED overrides clear it.

When HAL restarts while sleeping, scene restoration retains only the active scene identity; it does not reapply LED, servo, camera, mic, or speaker settings. Sleep keeps ownership of the hardware and its mute flags. The saved user LED state is loaded separately; a subsequent normal wake clears the retained scene through the existing scene-off path.

| Scene | Bright (base) | Bright (lamp) | Color (K) | Servo | Camera | Mic | Speaker |
|-------|--------|--------|-----------|-------|--------|-----|---------|
| `reading` | 80% | 19% | 4000K warm white | desk + hold | off | on | off |
| `focus` | 70% | 15% | 4200K warm-neutral | desk + hold | off | on | off |
| `relax` | 40% | 10% | 2700K warm | wall | on | on | on |
| `movie` | 15% | 4% | 2400K dim amber | wall | off | on | off |
| `night` | 5% | 1.2% | 1800K deep amber | down | off | on | off |
| `energize` | 100% | 24% | 5000K daylight | up | on | on | on |

"Base" is `SCENE_PRESETS` in `hal/presets.py`. On lamp the `scene` block of
`robots/lamp/presets.json` overrides **brightness only** (0.19 / 0.15 / 0.10 / 0.04 / 0.012 / 0.24);
color, aim and peripherals stay at the base values. The base levels pushed reading/focus/energize
past lamp's `max_brightness` ceiling (120), so all three collapsed to the same peak; the overlay
also accounts for a scene lighting all 32 pixels (lamp peaks: energize 61, reading 48, focus 38,
relax 25, movie 10, night 3).

### Scene peripheral control

When a scene activates, `POST /scene` applies in order:

1. **LED** — solid color = `preset.color × preset.brightness`
2. **Servo aim** — moves lamp head to preset direction (desk, wall, up, down)
3. **Servo hold** — if `"servo": "hold"`, holds the servo **after** the aim completes (aim → hold in one thread), as the `scene` owner. Not claimed if the scene ended while the arm was moving. Released when switching to a scene without hold, on scene off, or on a non-transient LED override.
4. **Camera** — auto on/off via `_auto_camera_on`/`_auto_camera_off`
5. **Mic** — mute stops voice pipeline (STT), unmute restarts it
6. **Speaker** — `off` stops music at once and mutes speech on a **drain** (`_start_scene_speaker_drain`, see `sensing-behavior.md`): the scene's own confirmation line, sent by os-server after the `/scene` marker, still plays before the speaker closes; `sleepy` chained in the same reply cancels the drain and mutes immediately, suppressing the late confirmation; wake restores the sleep-owned mute. `on` re-enables output. Scene off under a held privacy lock retargets the lock's snapshot so release reopens the speaker/camera (see `physical-controls.md`).

**Scene activation is the only path that aims.** An LED restore — after an emotion, at TTS end, at
music end, on mic unmute, on a listening cue clearing — repaints the strip and nothing else, and a
presence `IDLE/AWAY → PRESENT` restores the light only. Both used to re-aim while a scene was the
saved LED state, which killed the running animation and parked the head as `__aim_hold__` for 5s
(#314). Consequence: with a `hold` scene active the head no longer drifts back to the scene pose
after an animation ends — it interpolates to idle. Restoring that pose belongs in the scene's
`servo: hold`, not in an LED repaint.

### Hold ownership (#544)

The servo hold has owners: `scene`, `tracking`, `explicit` (`POST /servo/hold`) and `look`, kept in
`hal/drivers/motors/hold.py`. `_hold_mode` is true while at least one owner remains, and each
path releases only its own claim. Ending a scene never drops a tracking or explicit hold, and a
tracking session that ends during a reading scene leaves the scene's hold in place and does not
restart idle: the arm stays where tracking left it. `POST /servo/resume` clears every owner. An LED
override that ends a scene also deletes the persisted scene, so a HAL restart does not bring it back.
The release logs say whether the arm is free: `Scene off: servo released` when no owner is left,
`Scene off: scene hold released, servo still held by explicit` when one is. Likewise gaze logs
`framing released (servo held by scene, idle waits)` instead of `(idle has the arm)` at the end of
a conversation under a hold.

`look` is internal to os-server's `POST /api/vision/look`: it claims `POST /servo/hold/claim {"owner":"look"}` before the "taking a look" cue and releases it with `POST /servo/hold/release {"owner":"look"}` once the photo is taken, so an idle or still-emotion timer cannot swing the head mid-shot. Releasing `look` never drops another owner: after "turn right and hold it there" the explicit hold keeps the arm. If os-server never releases (it died mid-look), HAL drops the `look` hold after 30 s (`LOOK_HOLD_MAX_S`). On release, a still emotion's idle resume that was parked while the body was held runs now, unless another owner still holds the arm or the device is sleeping. Only `look` is accepted on these two routes (422 otherwise); agents keep using `POST /servo/hold`.

**Safety net.** A `scene` hold with no active scene is stale. It is released, with
`[hold] scene hold released -- no scene is active (stale)`, the next time something reads the
hold: `GET /servo`, `/servo/play`, `/servo/demo`, the idle handback, or a gaze mover.

**What a hold stops.** Idle and ambient animation, and the gaze watcher's automatic moves
(framing pan and tilt, face climb, speech-start repoint, look-around). Each logs the reason,
e.g. `[gaze] no pan: servo held by scene`. Explicit moves still run: `/servo/aim`,
`/servo/nudge`, `/servo/move`, `/servo/search` and a realtime look aim. During a scene the hold
stays at the new pose and the scene stays active, so "aim a bit left" refines reading mode
instead of ending it.

**Camera.** An emotion whose preset turns the camera on leaves it off while the active scene
keeps it off (`reading`, `focus`, `movie`, `night`).

### Emotion suppression during hold mode

When servo is in hold mode (reading/focus), **emotion animations are suppressed** to avoid distraction:

- `happy`, `thinking`, `curious`, `sad`, etc. → servo + LED skipped
- `greeting`, `sleepy`, `stretching` → **allowed** (these signal state changes: wake, sleep, scene transition) — **scene-preset holds only**

An **explicit `/servo/hold`** (agent command like "face the wall and stay there") sets `_hold_explicit` and suppresses the servo for **all** emotions, scene-change set included — a trailing `[HW:/emotion:greeting]` in the same reply used to ride the exemption and park the arm at the greeting pose instead of the commanded one. `/servo/resume` clears the flag. Scene changes and LED overrides leave an explicit hold alone.

This means during focus, sensing events (face emotion, motion) still reach OpenClaw but Lamp stays physically still and visually stable.

### Color temperature rationale

- **Focus 4200K/70% base** (not 5000K/100%; lamp overlay 15%) — 4000-4300K optimizes alertness without visual fatigue for sustained work
- **Night 1800K deep amber** — blue-free wavelengths (>580nm) preserve melatonin production
- **Movie mic on** — allows voice control ("pause", "stop") while watching

## Status LED

See details: [status-led.md](status-led.md)

LED feedback for system states. HAL resolves each state name from `STATUS_LED_PRESETS` in
`hal/presets.py`; on lamp the `status_led` block of `robots/lamp/presets.json` overrides the color
(every channel capped at 3) and, for the six long-lived breathing cues, the speed. Effect names always
come from the base table. Lamp values:

| State (preset) | Color | Effect / speed (lamp) | Lamp RGB | Base RGB / speed |
|-------|-------|-----|-----|-----|
| Connectivity (`connectivity`, no internet) | Orange | breathing 0.6 | `(3, 1, 0)` | `(16, 7, 0)` / 3.0 |
| Error (`error`, reserved) | Red | pulse 1.5 | `(3, 0, 0)` | `(16, 0, 0)` / 1.5 |
| OTA (`ota`, reserved) | Green | breathing 0.6 | `(0, 3, 0)` | `(0, 12, 0)` / 3.0 |
| Wi-Fi connecting (`wifi_connecting`, setup) | Blue | blink 0.5 | `(0, 1, 3)` | `(0, 6, 16)` / 0.5 |
| Booting (`booting`) | Blue | breathing 0.6 | `(0, 1, 3)` | `(0, 6, 16)` / 3.0 |
| HAL Down (`hal_down`) | Purple | breathing 0.6 | `(2, 0, 3)` | `(11, 0, 16)` / 3.0 |
| Agent Down (`agent_down`) | Cyan | breathing 0.6 | `(0, 3, 3)` | `(0, 12, 12)` / 3.0 |
| Hardware Failure (`hardware`) | Yellow | breathing 0.6 | `(3, 3, 0)` | `(12, 12, 0)` / 3.0 |
| OTA in progress (`ota_progress`, bootstrap) | Orange | breathing 0.4 | `(3, 1, 0)` | `(16, 8, 0)` / 0.4 |
| OTA success (`ota_success`, bootstrap) | Green | notification_flash 1.0 | `(0, 3, 1)` | `(0, 12, 4)` / 1.0 |
| OTA failure (`ota_error`, bootstrap) | Red | pulse 1.5 | `(3, 1, 1)` | `(16, 2, 2)` / 1.5 |

Priorities, triggers and callers are in [status-led.md](status-led.md).

Managed by `system/statusled/Service` (lamp) and `system/lib/hal` directly (bootstrap).

None of these colors are hardcoded in Go anymore — `system/statusled` states, the
bootstrap OTA-progress colors, and the setup-needed white all flow through HAL. The OS
owns the state machine (WHEN a state shows) and sends the state *name* to HAL
(`POST /led/status`: booting/error/ota/connectivity/wifi_connecting/hal_down/agent_down/hardware/
ready_flash/ota_progress/ota_error/ota_success/setup); HAL resolves the color/effect/speed
from `STATUS_LED_PRESETS`, overridable per device via `presets.json`'s `status_led` section
(see [ROBOT-SPEC.md § Per-device presets](../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson)).
Solid `/led/status` states carry `source: "status:<name>"` in the saved LED
sidecar. When non-transient `/led/off` finds `source: "status:setup"`, it treats
the call as setup teardown: clear that saved cue and restore the configured
resting look, respecting sleep and privacy ownership. This preserves the existing
os-server setup sequence without changing its API. Ordinary user colors carry
no status source, even if their RGB matches the setup cue; their `/led/off` still
saves explicit black. Transient off leaves the setup source intact.

This does not guess ownership for old, untagged sidecars. On an already affected
device, reapply the intended resting-light choice in Settings to clear the old
black override; automatically deleting all saved black would erase real user OFF choices.

`setup` is a persistent solid when sent through `POST /led/status`; the rest are transient
overlays. It supplies the AP/pre-setup white cue described below, and successful setup clears
that saved state rather than retaining it as a user LED preference.

### Mic-muted idle indicator

The lamp overlay sets `STATUS_LED_PRESETS["mic_muted"]` to dark red `(3, 0, 0)`, breathing at
speed 0.8. It is a resting look that stays lit for as long as the mic is muted, often pointed
at the user, so it is tuned to be glanceable rather than bright. Red helps — at the same value
it carries about a quarter the luminance of white. HAL-local
key (no Go statusled state): applied by `POST /voice/mute`, cleared by `POST /voice/unmute`
(`app_state._mic_muted_led`). It is the strip's **resting look** while the mic is muted —
nothing is blocked:

- Emotions, effects, TTS/music waves, and transient overlays all run normally on top.
  When they finish, every LED restore (`_restore_user_led`, `POST /led/restore`) settles
  back on the red instead of the user state — "nothing happening + red breathing" means
  the mic is muted.
- An explicit user LED command (non-transient `/led/solid|off|effect`, `/led/paint`)
  dismisses the indicator — the user's ask wins the strip; the mic stays muted.
- Yields to an active scene, which keeps its functional lighting (the flag persists, so
  leaving the scene while still muted brings the red back on the next restore). Scene
  mic-unmute paths (`/scene` with `mic:"on"`, `/scene/off`) also clear it. It does NOT
  yield to a resting (dark) strip: the indicator is the only signal that the mic is off,
  so it outranks "the lamp is idle".
- **Sleep wins:** while the `sleepy` emotion is active, the strip stays off. The muted
  flag still persists, but a late emotion/TTS/music restore cannot repaint the red
  indicator; it may resume only after a wake emotion clears sleep.

- `_user_led_state` is never touched — unmute restores the user's saved look.
- While the indicator owns the strip, transient overlay writes are skipped (`POST /led/effect`
  with `transient:true`) and so is **every** `POST /led/effect/stop`: no transient overlay can
  be running (its start was skipped), so any stop arriving while muted is a stale caller.
  Ambient now requests restore instead of starting/stopping effects. Emotion effects
  settle back onto the red indicator via their scheduled restore.
### Sleep owns the strip (HTTP routes)

While `_sleeping` is set, the LED **write** routes are gated at the HTTP layer too, not
just the internal repaint paths: `POST /led/solid`, `/led/paint`, `/led/effect` and
`/led/restore` log `... skipped -- sleepy owns the strip` and return `200` without
touching the hardware. `POST /led/status` is covered transitively (it delegates to
solid/effect). Without this, an agent finishing a stale task would light the strip on a
sleeping device.

Writes are **dropped, not queued**: sleep means "do not disturb", not "pause and report later",
so a cue that arrives during sleep is stale by the time the device wakes. Consequence:
os-server status cues (booting / error / OTA) are invisible while asleep — the underlying
work still runs normally, only its indicator is suppressed, and it is not replayed on wake.

Clearing routes (`/led/off`, `/led/effect/stop`) are deliberately **not** gated: they drive
the strip toward dark, which is what sleep already wants.

### Setup-needed solid (lamp)

When lamp starts and `config.SetUpCompleted == false` (device in AP/provisioning mode), `system/server/server.go` spawns a background goroutine (`waitAndPaintSetupReady` in `system/server/config_watch.go`, only on devices with the `light` capability) that sends `POST /led/status` with state `setup` and retries with backoff (1 s, doubling, capped at 10 s) until HAL acknowledges it, setup completes, or the server shuts down — HAL paints the strip solid white as a "device ready, connect to my hotspot" cue. It does not wait on `/health` (LED routes can acknowledge before unrelated drivers are healthy); retrying handles the cold-boot race where os-server's :5000 is up before HAL's :5001. This does not use the `statusled` state machine. The white is temporary: a successful `POST /api/device/setup` clears this saved setup state instead of retaining it as a user LED preference, then restore settles on the ambient resting look (dim steady white). Booting blue-breathing still shows during init. See [setup-flow.md](../../../docs/setup-flow.md#ap-mode).

## Ambient Idle Behaviors

When Lamp is idle, its default is steady white RGB **[1, 1, 1]**, approximately
0.4% of the RGB channel range. There is no breathing effect or animation thread.
Actual perceived brightness depends on the LEDs, not just the channel percentage.

### The resting look (default: dim white)

The device owns this setting in `robots/<type>/presets.json`:

```json
"ambient_led": {"resting": {"effect": "solid", "color": [1, 1, 1]}}
```

Lamp uses the value above; intern-v2 keeps [5, 4, 3]. Missing
configuration retains the dark platform fallback. HAL merges this into
`AMBIENT_RESTING_LED` at startup. A solid preset paints once; it does not start
an effect worker. Emotion/TTS/music release and mic-unmute restore the same look
when no user LED state exists. Existing status, sleep and mic-privacy ownership
still takes priority.

OS ambient pauses on interaction and resumes after 60 seconds of quiet (checked
on a two-second tick). Its `restingLEDLoop` requests `POST /led/restore` once on
resume instead of choosing a color or starting breathing. HAL remains the single
source of the resting look and the user's saved color/effect.

Speaking waves preserve the base RGB of a solid resting preset or solid emotion
when no user color is saved. With Lamp resting at [1, 1, 1], the wave modulates
that dim color; it does not fall back to bright warm white. A saved user color
still takes priority, and explicit off stays dark.

### Owner choice from the web UI

Settings → **Resting light** (`/setting#led`) lets the owner replace the device
default without editing `presets.json`. HAL exposes it as `GET /led/resting` and
`PUT /led/resting {mode, color}`:

| `mode` | Resting look |
|--------|--------------|
| `default` | The device preset from `presets.json` |
| `off` | Dark (solid [0, 0, 0]) |
| `custom` | Solid `color` [R, G, B], 0-255; black normalizes to `off` |

`hal/resting_led.py` snapshots the device preset at boot, then rewrites
`AMBIENT_RESTING_LED` in place, so every restore path above follows the choice.
The choice is saved in `/var/lib/hal/resting_led.json` (`HAL_RESTING_LED_PATH`)
and survives reboots and OTA, unlike the boot-scoped user LED state. A `PUT`
also clears that saved user state (including an explicit off) and repaints the
strip unless the device is asleep; os-server's hardware proxy unlocks ambient
restore for the same request. The page offers preset chips, hue / white↔colour /
brightness sliders capped at channel 64, and warns when a saturated colour sits
within 20° of hue of a status cue (red, yellow, green, cyan, blue, purple).

`POST /led/resting/preview {color}` paints a candidate colour without saving it
(the phone app's live picker, MQTT `led.resting.preview`). It never interrupts
sleep, speech, music or the mic-muted indicator (`painted: false`), and the strip returns to the saved
look 10 s after the last preview unless a `PUT` saves first.

### Explicit off

`POST /led/off` saves a solid black preference [0, 0, 0]. Ambient and post-effect
restores therefore keep an explicitly switched-off lamp dark. Transient off only
clears the current display; it does not change that preference. Explicit colors,
scenes and effects replace the saved preference normally. State survives a HAL
restart within the same boot; reboot clears the boot-scoped state and returns to
the device default. Legacy `{"type":"off"}` sidecars still normalize to no state.

`led_should_stay_dark()` covers explicit solid black and a dark default, so
music waves and presence restoration respect off. Active voice status remains
visible: listening and thinking use their device emotion presets even with a
saved off preference; TTS uses the dim listening color when its base would be
black. The existing cue/completion/cancellation restore paths restore the latest
saved preference, including off. No preference is overwritten and no network
request or additional delay is introduced. Sleep still blocks these cues; mic
privacy retains its existing resting-indicator priority. There is no separate
full-blackout preference. The `light on` intent remains
warm white [255, 220, 180]; it does not use the dim ambient preset.

## LED in Emotion

See [emotion-led-mapping.md](emotion-led-mapping.md) for the full emotion → LED color + effect + servo mapping.

### Unknown emotion names

`POST /emotion` (`hal/routes/emotion.py`) never rejects a non-empty emotion name. Names are lowercased/trimmed; anything not in `EMOTION_PRESETS` falls back to `curious` (a neutral, always-safe expression) with a warning logged — callers are AI agents that sometimes invent emotion names, and a 400 would waste their turn with nothing showing on the device. Exception: while the device is sleeping, an unknown name is **ignored** (`status: ignored`) instead of falling back. `curious` no longer wakes (see `_SLEEP_GATE_ALLOWED` below), so the fallback could not lift the sleep gate anyway — but it would still resolve to a servo/LED-bearing emotion that the gate then drops, and logging it as `curious` would hide which invented name the agent actually sent. Otherwise everything downstream (servo, LED) uses the resolved emotion.

## Per-device preset overrides

A device can override these emotion/scene/aim values (and the LED ring size) without
changing the shared defaults, via a `robots/<type>/presets.json` file. This is a
platform mechanism — see [ROBOT-SPEC.md § Per-device presets](../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson).

### LIVE voice status

LIVE voice uses the same `listening` HW emotion and realtime thinking helper
as turn-based voice, including their existing LED, display and body behavior.
It requires recognized input text and the regular addressing gate; noise or
opening the mic cannot start these emotions. Thinking requires provider end
evidence, never a local silence estimate. There is no separate LIVE LED overlay. See [realtime voice](../../../docs/realtime-voice.md#hw-emotion-feedback-in-live-mode) for timing and cleanup.

### Relative intent dimming

The local/Jev `dim` action reads `/led/color`, halves each RGB channel, writes
`/led/solid`, and verifies the readback. Repeated requests dim again; black
stays black. Integer rounding may reach off. This stops effects and scenes,
using the reported base color or brightest pixel; it does not preserve patterns.

Local/Jev voice completion (`handledLocally=true`) releases the retained
realtime thinking cue without waiting for TTS. Muted or silent replies therefore
do not leave the cue active. Cleanup preserves newer emotions and restores the
saved LED state (including off/dim); active speech/music retains its overlay
until normal playback teardown. Agent-owned turns retain their thinking cue.
