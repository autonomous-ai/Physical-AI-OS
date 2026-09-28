# LED Control — Documentation

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

### Concurrent frame writes and clear diagnostics

Solid, per-pixel paint and clear share a driver lock for the entire operation.
Clear holds it across both black-frame writes, the two 10 ms waits, SPI idle
and buffer read-back. An animation cannot repaint the buffer midway and cause
`LED clear did NOT take` to report a false clear failure. A later frame can
still paint after clear returns; effect ownership and cancellation remain the
caller's responsibility. The diagnostic reads software memory, not physical
LED feedback, so a black buffer does not prove the hardware is dark.

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

Deactivate: `POST /scene/off` — clears active scene, restores idle LED, re-enables camera/speaker, releases servo hold.

The active scene **survives HAL service restarts** (OTA, deploy, crash): it is persisted to a boot-scoped sidecar (`/tmp/hal-scene-state.json`, keyed to the kernel `boot_id`) and re-activated automatically when HAL comes back up, so the agent's belief ("focus mode is on") stays in sync. A full device reboot intentionally starts scene-less. Transient LED calls (`/led/solid`, `/led/off`, `/led/effect` with `"transient": true`, e.g. the boot breathing effect) overlay the strip without exiting the active scene; only non-transient LED overrides clear it.

When HAL restarts while sleeping, scene restoration retains only the active scene identity; it does not reapply LED, servo, camera, mic, or speaker settings. Sleep keeps ownership of the hardware and its mute flags. The saved user LED state is loaded separately; a subsequent normal wake clears the retained scene through the existing scene-off path.

| Scene | Bright | Color (K) | Servo | Camera | Mic | Speaker |
|-------|--------|-----------|-------|--------|-----|---------|
| `reading` | 80% | 4000K warm white | desk + hold | off | on | off |
| `focus` | 70% | 4200K warm-neutral | desk + hold | off | on | off |
| `relax` | 40% | 2700K warm | wall | on | on | on |
| `movie` | 15% | 2400K dim amber | wall | off | on | off |
| `night` | 5% | 1800K deep amber | down | off | on | off |
| `energize` | 100% | 5000K daylight | up | on | on | on |

### Scene peripheral control

When a scene activates, `POST /scene` applies in order:

1. **LED** — solid color = `preset.color × preset.brightness`
2. **Servo aim** — moves lamp head to preset direction (desk, wall, up, down)
3. **Servo hold** — if `"servo": "hold"`, freezes servo **after** aim completes (aim → hold in one thread). Released when switching to a scene without hold.
4. **Camera** — auto on/off via `_auto_camera_on`/`_auto_camera_off`
5. **Mic** — mute stops voice pipeline (STT), unmute restarts it
6. **Speaker** — `off` stops music at once and mutes speech on a **drain** (`_start_scene_speaker_drain`, see `sensing-behavior.md`): the scene's own confirmation line, sent by os-server after the `/scene` marker, still plays before the speaker closes; `sleepy` chained in the same reply takes the drain over so wake can restore the speaker. `on` re-enables output. Scene off under a held privacy lock retargets the lock's snapshot so release reopens the speaker/camera (see `physical-controls.md`).

**Scene activation is the only path that aims.** An LED restore — after an emotion, at TTS end, at
music end, on mic unmute, on a listening cue clearing — repaints the strip and nothing else, and a
presence `IDLE/AWAY → PRESENT` restores the light only. Both used to re-aim while a scene was the
saved LED state, which killed the running animation and parked the head as `__aim_hold__` for 5s
(#314). Consequence: with a `hold` scene active the head no longer drifts back to the scene pose
after an animation ends — it interpolates to idle. Restoring that pose belongs in the scene's
`servo: hold`, not in an LED repaint.

### Emotion suppression during hold mode

When servo is in hold mode (reading/focus), **emotion animations are suppressed** to avoid distraction:

- `happy`, `thinking`, `curious`, `sad`, etc. → servo + LED skipped
- `greeting`, `sleepy`, `stretching` → **allowed** (these signal state changes: wake, sleep, scene transition) — **scene-preset holds only**

An **explicit `/servo/hold`** (agent command like "face the wall and stay there") sets `_hold_explicit` and suppresses the servo for **all** emotions, scene-change set included — a trailing `[HW:/emotion:greeting]` in the same reply used to ride the exemption and park the arm at the greeting pose instead of the commanded one. `/servo/resume` and scene transitions clear the flag.

This means during focus, sensing events (face emotion, motion) still reach OpenClaw but Lamp stays physically still and visually stable.

### Color temperature rationale

- **Focus 4200K/70%** (not 5000K/100%) — 4000-4300K optimizes alertness without visual fatigue for sustained work
- **Night 1800K deep amber** — blue-free wavelengths (>580nm) preserve melatonin production
- **Movie mic on** — allows voice control ("pause", "stop") while watching

## Status LED

See details: [status-led.md](status-led.md)

LED feedback for system states (all `breathing` at speed 3.0 unless noted):

| State | Color | RGB |
|-------|-------|-----|
| Connectivity (no internet) | Orange | `(16, 7, 0)` |
| Booting | Blue | `(0, 6, 16)` |
| HAL Down | Purple | `(11, 0, 16)` |
| Agent Down | Cyan | `(0, 12, 12)` |
| Hardware Failure | Yellow | `(12, 12, 0)` |
| OTA in progress (bootstrap) | Orange | `(16, 8, 0)` |
| OTA success (bootstrap) | Green flash | `(0, 12, 4)` |
| OTA failure (bootstrap) | Red pulse | `(16, 2, 2)` |

Managed by `system/statusled/Service` (lamp) and `lib/hal` directly (bootstrap).

None of these colors are hardcoded in Go anymore — `system/statusled` states, the
bootstrap OTA-progress colors, and the setup-needed white all flow through HAL. The OS
owns the state machine (WHEN a state shows) and sends the state *name* to HAL
(`POST /led/status`: booting/error/ota/connectivity/hal_down/agent_down/hardware/
ready_flash/ota_progress/ota_error/ota_success/setup); HAL resolves the color/effect/speed
from `STATUS_LED_PRESETS`, overridable per device via `presets.json`'s `status_led` section
(see [ROBOT-SPEC.md § Per-device presets](../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson)).
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
  Ambient's Go breathingLoop tracks its "running" flag locally and still fires StopEffect on
  pause/lock even though its start was skipped — before this guard covered all threads, that
  stop passed while an emotion effect held the strip (e.g. thinking's purple pulse) and killed
  it after ~one cycle, freezing the strip on the last ripple frame. Emotion effects settle
  back onto the red via their scheduled restore.
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

When lamp starts and `config.SetUpCompleted == false` (device in AP/provisioning mode), `server/server.go` spawns a background goroutine that polls HAL `GET /health` once per second up to 30s, and once `health.led == true` sends `POST /led/status` with state `setup` — HAL paints the strip solid white as a "device ready, connect to my hotspot" cue. Polling (not a single call) handles the cold-boot race where os-server's :5000 is up before HAL's :5001. This does not use the `statusled` state machine. The white is temporary: a successful `POST /api/device/setup` clears this saved setup state instead of retaining it as a user LED preference, then restore settles on the ambient resting look (currently dark/off). Booting blue-breathing still shows during init. See [setup-flow.md](../../../docs/setup-flow.md#ap-mode).

## Ambient Idle Behaviors

When Lamp is idle (no interaction):
- **Breathing LED** — sine-wave brightness. Breathes the current LED color; when none is set (e.g. just after boot), it falls back to the **resting look**, which is `(0, 0, 0)` — dark. A user/agent-set color is respected (breathing uses it; ambient never overrides a locked color).

Auto-pauses on interaction, resumes after 60s of silence.

### The resting look (default: off)

When no user LED state exists, the strip settles on the *resting look*, defined in **two
places that must be flipped together**:

| Side | Knob | Consumers |
|---|---|---|
| HAL | `AMBIENT_RESTING_LED` (`hal/presets.py`) | `POST /led/restore` with no user state; the settle after mic-unmute |
| os-server | `ambientRestingColor` (`system/ambient/service.go`) | `breathingLoop` fallback when `/led/color` reads black |

Both are currently **`(0, 0, 0)` — the resting state is dark**. A black resting color is
treated specially: the settle paths *clear* the strip instead of starting an effect (an
effect thread breathing black would burn 25 fps of SPI writes and make `GET /led/color`
report `on: true` over a dark strip), and the Go loop skips its tick entirely rather than
painting. Light is therefore opt-in — it comes on for an *action* (emotion, status cue,
explicit user/agent color, scene) and goes back to black when that action releases the
strip.

Two consequences worth knowing:

- An idle device looks **off**, not "resting". That is intended — status cues (`booting`,
  `connectivity`, …) are what tell the user something is happening.
- After a reboot the strip stays dark until something asks for light: the LED sidecar is
  boot-scoped, so every boot starts with no user state and lands on the resting look.

Every release path has to *ask* for the resting look — a path that paints its own "back to
normal" color silently opts out of the default. Two used to: scene-off dispatched the `idle`
preset color, and music-stop started idle breathing, both leftovers from when resting was a
warm white. With resting black, that left the strip glowing dim orange after a scene or a
song until some unrelated restore happened to clear it. Both now go through the shared
settle (`led.restore_led` / `ambient_resting_is_dark`), so turning a scene off turns the
light off.

Setting both knobs back to `(255, 200, 140)` (warm white ~2700K @ speed 0.3) restores the
previous behavior, where an idle lamp read as a cozy lamp turned on rather than a cold
"device booting" blue, and the warm tone stayed clear of every status color. That look is
what re-lit the strip ~60s after the user turned the light off, and what made every fresh
boot come up lit.

### "Off" is not a mode

`POST /led/off` **clears the user LED state** (`_save_user_led_state(None)`) rather than
saving an off flag. Since the resting look is already dark, no state IS off. There are only
two states:

| State | `_user_led_state` | At rest | On an action |
|---|---|---|---|
| **Default** | `None` | dark | lights up (emotion, status cue, mic-muted indicator) |
| **User colour** | solid / paint / effect / scene | that colour | effect runs over it, then settles back |

`led_should_stay_dark()` (`hal/app_state.py`) is the single predicate for "leave the lamp
alone", and everything that paints without the user asking checks it: the TTS/music waves,
the post-effect settle, `POST /led/restore`, `POST /led/effect/stop` (it clears the stopped
effect's last frame instead of leaving it frozen), presence restore/dim, and — on the
os-server side — ambient's breathing loop.

What is deliberately NOT gated: an explicit user/agent command (that IS the user asking, and
it overwrites the state), and cues that carry information the user needs — status overlays
(`POST /led/status`: connectivity orange, error red, OTA green) and the mic-muted indicator.
Those earn their light even on a resting strip.

Off used to be its own sticky state, and it was worse: it looked identical to the default
(both dark) but behaved differently, nothing could return the device to the default — an
explicit colour was the only way out — and a reboot silently dropped it, because the sidecar
is boot-scoped. A legacy sidecar holding `{"type": "off"}` is normalised to "no state" on
load.

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
