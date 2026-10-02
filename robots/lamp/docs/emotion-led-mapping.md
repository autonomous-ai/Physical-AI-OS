# Emotion → LED + Animation Mapping

Source: colors are what the **lamp** actually shows — `robots/lamp/presets.json` (the per-device overlay) merged over `hal/presets.py` `EMOTION_PRESETS`. The overlay patches any field, not just `color` — `presets_overlay.py` does `base[key].update(fields)`, and lamp uses that to override `speed` on `listening` and `servo` on `thinking`. Fields it does not mention keep the base value. The `Color source` column says whether a row's color comes from the lamp `overlay` or is still the untouched `base` value.

| Emotion | Color (RGB) | Hex | Color source | Effect | Speed | Servo Animation |
|---|---|---|---|---|---|---|
| `curious` | 0, 3, 3 | `#000303` dim cyan | overlay | candle | 0.3 | curious |
| `happy` | 3, 3, 0 | `#030300` dim yellow | overlay | candle | 0.2 | happy_wiggle |
| `sad` | 0, 1, 3 | `#000103` dim blue | overlay | breathing | 0.4 | sad |
| `thinking` | 2, 0, 3 | `#020003` dim violet | overlay | pulse | 0.3 | — (see note) |
| `idle` | 1, 2, 3 | `#010203` dim steel blue | overlay | breathing | 0.2 | idle |
| `excited` | 3, 1, 0 | `#030100` dim orange | overlay | candle | 0.5 | excited |
| `shy` | 3, 0, 1 | `#030001` dim rose | overlay | breathing | 0.3 | shy |
| `shock` | 2, 2, 2 | `#020202` soft white | overlay | notification_flash | 1.0 | shock |
| `listening` | 0, 0, 3 | `#000003` dim blue | overlay | breathing_fine (opens at peak) | 0.3 (base 1.2) | — (see note) |
| `laugh` | 2, 3, 0 | `#020300` dim lime | overlay | candle | 0.2 | laugh |
| `confused` | 3, 2, 0 | `#030200` dim amber | overlay | candle | 0.2 | confused |
| `sleepy` | 0, 0, 0 | `#000000` black (off) | base | solid | — | sleepy |
| `greeting` | 3, 0, 3 | `#030003` dim magenta | overlay | breathing | 0.3 | greeting \| wake_up |
| `goodbye` | 3, 0, 2 | `#030002` dim purple | overlay | breathing | 0.5 | goodbye |
| `caring` | 3, 1, 2 | `#030102` dim pink | overlay | breathing | 0.4 | nod |
| `acknowledge` | 0, 3, 0 | `#000300` dim green | overlay | breathing | 0.5 | acknowledge |
| `stretching` | 3, 2, 1 | `#030201` dim warm amber | overlay | breathing | 0.6 | idle |
| `music_strong` | base 8, 12, 8 (unused) | hue swept by the effect; level from `brightness` 0.012 — see below | overlay | rainbow | 1.0 | music_rock |
| `music_chill` | 0, 3, 2 | `#000302` dim aqua | overlay | breathing | 0.3 | music_rock \| music_groove \| music_jazz \| music_waltz |
| `scan` | 0, 2, 3 | `#000203` dim sky blue | overlay | pulse | 0.3 | scanning |
| `nod` | 2, 3, 1 | `#020301` dim yellow-green | overlay | breathing | 0.5 | nod |
| `headshake` | 3, 0, 0 | `#030000` dim red | overlay | breathing | 0.5 | headshake |

`music_strong`'s color is inert: it runs the `rainbow` effect, and `rainbow()` in `hal/drivers/rgb/effects.py` sweeps the whole hue circle itself. Its level comes from a separate `brightness` field (0.0-1.0, the same field the scene table uses), which the lamp sets to 0.012.

## Per-emotion low-glare palette

Every emotion has its own color rather than sharing a group color. The palette uses a single quantized RGB wheel: each channel is an integer from 0 to 3, keeping every cue below the tested glare cap while retaining as much hue separation as the low range allows. Effects, speeds, and servo mappings remain the mechanisms that add motion and personality; the table above is the current source of truth.

## Retired six-hue grouping

The following section records the prior grouping rationale and is not the active palette.

The table above is the base palette from `hal/presets.py`. On the lamp it is overridden, because at indicator brightness that palette has no usable color space left.

Measured on lamp-0c89 (19/08/2026): 22 emotions are packed into three hue clusters, and **12 of them sit between hue 20° and 44°** — caring 20, headshake 20, shy 21, greeting 26, goodbye 26, confused 28, music_chill 34, idle 38, laugh 38, nod 38, curious 40, happy 44. Some are not merely close but byte-identical: `idle` = `laugh` = `nod` = `[12, 8, 1]`, and `greeting` = `goodbye` = `[12, 8, 5]`. At a peak of 12–16 each channel has only 12–16 levels instead of 255, so a 1–4° hue difference is simply not resolvable by eye.

This is fallout from the dimming pass. Before 18/08 the presets ran at high peaks — greeting/goodbye `[255, 180, 100]`, caring `[255, 160, 120]`, music_chill `[252, 136, 3]`, acknowledge `[51, 230, 70]`, listening `[51, 121, 230]` — and at that amplitude 22 distinct colors read fine. Users complained about glare ("like a camera flash in your face": greeting ran full 255 across all 64 pixels exactly as someone walked up to the lamp), so everything was pulled down to 12–16. That cured the glare and collapsed the color space.

**Turning the brightness back up is not the fix.** With gamma 2.2, dropping peak from 255 to 90 costs only ~40% of *perceived* brightness while keeping 90 color levels; dropping 90 → 12 costs another ~40% of perceived brightness but throws away 7.5× the color resolution. Almost all of the anti-glare benefit is already won in the first step; the second step is nearly pure cost.

For the current low-glare device test, every RGB channel in the lamp overlay is capped at 3. The mixes below preserve each group's hue as closely as integer channels allow; `processing` remains at 320° to avoid the green emitter.

| Group | Hue | RGB | Emotions |
|---|---|---|---|
| negative | 0° dim red | `[3, 0, 0]` | `sad`, `shy`, `confused`, `headshake` |
| joy | 60° dim yellow | `[3, 3, 0]` | `happy`, `laugh`, `excited` |
| processing | 320° dim purple-rose | `[3, 0, 2]` | `curious`, `thinking`, `scan`, `acknowledge` |
| music | 180° dim cyan | `[0, 3, 3]` | `music_chill` |
| listening | 240° dim blue | `[0, 0, 3]` | `listening` |
| social | 300° dim purple | `[3, 0, 3]` | `greeting`, `goodbye`, `caring` |
| background | 40° dim amber | `[3, 2, 0]` | `idle`, `nod`, `stretching` |
| alarm | soft white | `[2, 2, 2]` | `shock` |
| sleep | off | `[0, 0, 0]` | `sleepy` (unchanged) |

**`processing` is at 320°, not the 120° green its slot originally held** (25/08/2026, reported by eye on lamp-0c89). Green was called glaring at `[0, 4, 0]`, which is already the bottom of the green tier: `intensity` 0.7 makes that `[0, 2, 0]` on the strip, and one step lower is `[0, 1, 0]`, where breathing and pulse truncate per frame and the cycle visibly steps. The group is now additionally dimmed to `[3, 0, 2]`, retaining its purple hue without waking the green die. The cause is the emitter rather than the amplitude — WS2812's green die is brighter than its red at the same value, the same finding that already put the green tier at 4 while every other tier sits at 5 — so *every* green-dominant hue, 60° through 180°, carries the problem and no amount of dimming escapes it. Moving the group off green is the only fix that keeps the whole ring lit (the "just light fewer pixels" alternative is ruled out; see `led-control.md`).

The dimmer `[3, 0, 2]` mix shifts processing toward purple while preserving the no-green constraint. Its peak red output is 40% lower than the previous `[5, 0, 2]` mix.

Three things are deliberate:

1. **Every color has at least one channel at 0** — maximum saturation. At a peak of 12–16 this is mandatory: a diluted color like `[12, 8, 1]` loses whatever made it itself, while `[0, 12, 0]` still reads unmistakably green no matter how faint it gets. For `processing` the zeroed channel is specifically *green*, for a second reason stacked on top of saturation: holding green at 0 is what keeps the bright green die dark. Red and blue mix freely inside that group — the rule is about the die, not about staying monochrome.
2. **`idle` / `nod` / `stretching` use `[3, 2, 0]`.** `idle` is the state the lamp spends the most time in, so it stays visibly dimmer than a full yellow cue.
3. **Within a group, emotions are told apart by effect + speed, not by color** — e.g. the joy group: `happy` candle 0.2, `laugh` candle 0.2, `excited` candle 0.5. The eye discriminates rhythm far better than it discriminates 4° of hue.

The trade-off, stated plainly: 22 emotions now share 6 colors. From the color alone you can read the **group**, not the specific emotion. That is accepted because the situation it replaced let you read nothing at all.
On 24/08/2026 the peaks came down again, in three passes on device the same day. Pass 1 halved everything (16/12/8 -> 8/6/4), because the previous level was still reported as glaring. Pass 2 dropped only the green-dominant tier, 6 -> 4: green at peak 6 was still called glaring while red at peak 8 read fine — WS2812's green die is brighter than its red at the same value by more than the base 12-vs-16 rule allows for. Pass 3 then lowered the low-green tier 8 -> 5 as well. Every pass scales all three channels proportionally, so the six hues never move; only the amplitude does. On top of that, emotion colors are multiplied by the request's `intensity` (default 0.7) before they reach the strip, so `listening`'s declared `[0, 0, 5]` arrives as `[0, 0, 3]` and `idle`'s `[4, 2, 0]` as `[2, 1, 0]`. That is at or below the peak-8 floor documented in `hal/presets.py`: breathing and pulse truncate per frame (`int(c * brightness)`), so the cycle has very few distinct levels left and visible stepping is the thing to watch for. Measured on lamp-0c89, 24/08/2026.


Technical notes:

- **`shock` is overridden even though it is not one of the six hue groups.** It uses `[2, 2, 2]`, keeping the white alarm cue below the test cap.
- **`music_strong` is overridden through `brightness`, not `color`.** It is the only cue on the `rainbow` effect, and `rainbow()` in `hal/drivers/rgb/effects.py` generates its own hue, so it never reads `color` — until `brightness` was added it painted at value 1.0 and was the one cue no per-device palette could reach, running straight to the `light.max_brightness` ceiling (120) on all 32 pixels at once, roughly 40x every other emotion. `brightness` (0.0-1.0, the same field the scene table uses) is now its only level.

  Two different cues light a rainbow around music, from two different places, and both read this one number:

  | Lit by | Effect | Duration |
  |--------|--------|----------|
  | the agent calling `POST /emotion {"emotion": "music_strong"}` (see `skills/emotion/SKILL.md`) | `rainbow` | one cue, ~16 s then restore |
  | `_on_music_play_start()` in `hal/app_state.py`, when the user has set no LED color | `speaking_wave_rainbow` | the whole song |

  `speaking_wave_rainbow` also generates its own hue, so it takes the same `music_strong` `brightness` and rides its VU envelope under it. `music/routes` never emits the `music_strong` emotion itself — the agent does.
- **This table lives in `robots/lamp/presets.json`**, the per-device overlay merged field by field at boot via `hal/board/presets_overlay.py` — `hal/presets.py` is *not* edited. Other robots (reachy, intern) therefore keep the base palette, and reverting the lamp to the base palette is just deleting the `emotion` section from that JSON file.
- **Do not confuse `EMO_IDLE` with `AMBIENT_RESTING_LED`.** The latter comes from `presets.json` → `ambient_led.resting` (Lamp: steady `[5, 4, 3]`); `EMO_IDLE` is an emotion the agent actively emits and still has a color.

### `listening` opens at the peak of its breath

`breathing` normally starts an arc at brightness 0 and rises. At the lamp's own settings that rise is unreadable: a full `0 -> 1 -> 0` arc takes **10s** at `speed` 0.3, and because each frame is truncated (`int(c * brightness)`) a peak of 3 renders as literal `(0, 0, 0)` for the first **~1.1s** and only reaches 2 at ~2.3s. Measured on device 28/08/2026: the listening cue fired on the FIRST STT partial (12:02:17.666) but was not visible until the last one (12:02:18.231) — the delay everyone read as "STT is slow" was the effect's rise time, not the transcript.

`listening` therefore carries `"start_at_peak": True` (`hal/presets.py`), which makes its opening arc start at full brightness and breathe *down*; every cycle after the first rises from 0 as usual. Peak level, hue and `speed` are untouched — nothing gets brighter than it already was, so this does not walk back the glare passes above.

It is opt-in per preset and `listening` is the only preset that sets it: an emotion is meant to fade in, and only a cue that answers the user's first words has to be readable on its first frame. The equivalent fix cannot live at the call site — painting the colour before the effect thread starts is erased milliseconds later by that thread's own `i=0` frame (`hal/drivers/rgb/effects.py`).

### The breath's resolution comes from the ring, not from the colour

Opening at the peak fixed the delay but exposed what was under it: at an effective peak of 2 the only reachable values are 0, 1 and 2, the top one lasts a single frame, and the strip sits at literal black for a third of every cycle. On device that reads as a blink followed by darkness — and darkness is precisely what the preset's own comment rules out ("stays lit for as long as the user is talking; pulse's dark gap between beats reads as an alert").

Raising the peak was tried first, since more levels normally come from more headroom: a ladder of effective 4 / 6 / 8 / 12 was run on lamp-0c89 on 28/08/2026 and **rejected as glaring**, consistent with every earlier attempt. A constant-luminance hue drift was tried next and read as stepped, because the green channel quantises into 3 values at this level exactly as brightness does.

`listening` therefore runs `breathing_fine` (`hal/drivers/rgb/effects.py`): the breath moves between `color` and one unit below it, and the fraction in between is rendered by raising SOME of the 32 pixels, scattered with a stride coprime to the ring size so the strip reads as one ring at an in-between level rather than a lit arc. The eye integrates the ring, so the cue gains ~32 sub-levels per unit. Peak brightness is unchanged, hue is unchanged (a channel already at 0 stays 0), and no pixel is ever dark — the resolution comes from the 32 pixels instead of from 8-bit colour.

## `thinking` and `listening` have no servo

Both set `"servo": None` — LED only, the lamp holds still:

- `listening` runs while the user is actually speaking; servo noise plus chassis vibration goes straight into the mic and dirties STT.
- `thinking` is fired by the emotion-ack hook on **every** preprocessed message, so a servo there means the body fidgets through the whole conversation. It also moves the camera: `thinking_deep.csv` sweeps wrist_pitch 34° and wrist_roll 32°, and the camera sits in the head — so when the realtime `look` tool fires (it fires during the model's turn, i.e. while `thinking` is showing) the user has to chase a moving, rolling camera to hold an object in frame. The same hook is why its LED sits behind the `_BACKGROUND_EMOTIONS` guard in `hal/app_state.py`, to keep a whole conversation from being repainted green.

On the lamp `thinking` gets `"servo": null` from `robots/lamp/presets.json`; the base preset in `hal/presets.py` still maps it to `thinking_deep` for bodies that want it.

On the lamp `stretching` gets `"servo": "idle"` from the same overlay. The emotion still performs the normal wake transition (clearing sleep and restoring the camera/audio state), but a tap that wakes Lamp now starts the idle recording instead of the stretching movement. Other robot types keep the base `stretching` recording.

`listening.csv` and `thinking_deep.csv` stay in `hal/recordings/` even with no emotion mapping to them: `/servo/play` can still call them by hand, and Reachy still maps them (`hal/drivers/motors/reachy_service.py`).

The code path handles `servo: None` natively — `hal/routes/emotion.py` skips the play branch and `POST /emotion` returns `"servo": null`, and `listening` schedules no LED restore at all.

### `servo: None` alone does NOT hold the lamp still

Not starting a new recording does not mean the lamp is quiet. The idle loop has been running since boot and repeats forever (`_continue_playback` in `hal/drivers/motors/animation_service.py`), and `idle.csv` is not gentle — each 10s cycle sweeps wrist_roll ~32°, wrist_pitch ~26°, base_pitch ~17°. Worse, an emotion that just finished **interpolates back to idle** over a few seconds, so the widest swing lands exactly while the user is talking.

So for a `servo: None` emotion (today only `listening`) the route calls `svc.halt()`: drop the running recording, pin the current pose, keep torque ON. No explicit un-halt is needed — the next emotion or `/servo/play` calls `_begin_motion()`, which clears the flag itself.

Two guards go with it:

- **Music is exempt**: while music plays the groove matters more, and a listening cue must not stop the dancing.
- **Auto-resume idle after 10s** (`STILL_IDLE_RESUME_SECONDS` in `hal/routes/emotion.py`): if the turn produces no emotion at all (LLM error, silence after the first partial), the body returns to idle instead of freezing mid-pose. Any `POST /emotion` cancels this timer. The 8s safety net in `voice_service` only clears the LED and never touches the servo — so this timer is the only thing looking after the body.
- **Clear thinking when the reply finishes speaking** (`_clear_thinking_after_reply` in `hal/app_state.py`, called from `_on_tts_speak_end`): the wait ends when the device stops talking, so that is when the `thinking` face goes away — no waiting on the agent to emit an emotion marker. Gated on `tts_service.realtime_feedback`, the flag only the runtime's own reply sets, so dead-air fillers and system notices spoken *during* the wait do not end the cue.
- **Auto-reset thinking after 25s** (`HAL_EMOTION_THINKING_RESET_S`, `0` disables) — the net for turns that never speak at all. `thinking` is the one face nothing clears on its own — it is set at the start of a wait and replaced by whatever the reply expresses, so a turn that dies before expressing one (realtime exception, a delegate the agent answers without an emotion marker) leaves it burning with no user input to break it out. After that many seconds of *continuous* thinking the route drops `_thinking_cue_active`, expresses `idle` and restores the user's LED state. Any other emotion cancels; a fresh `thinking` re-arms. The window is measured, not guessed: on device, realtime replies clear the cue in 0.4-8.6s and a delegated turn runs 6-22s end to end.

Measured on lamp-0c89: after `listening`, all 5 servo angles held at T+2s / T+5s / T+8s, and idle resumed around T+13s; a `happy` sent mid-halt played normally.

## Brightness budget (peak)

Emotion LEDs are an **indicator**, not illumination — they share a budget with `STATUS_LED_PRESETS`:

- green-leaning hues (green / yellow / cyan / white) → peak channel **4**
- little or no green (red / purple / orange / blue) → peak channel **5**

Each color is brought down by **scaling the original RGB proportionally** to its tier, so every emotion keeps its original hue. Dimming must be done by scaling, not by picking a new color — the hue is what the agent is trying to say; brightness is only how loudly it says it.

The `light.max_brightness` gate (lamp: 120) only scales peak **UP** toward the ceiling, never down, so dimming has to happen in the preset itself. After any change, check it by eye on a real device.

`listening` uses breathing rather than pulse: it is lit while the user speaks, and pulse's dark gaps between beats read as a warning when sustained.

If `blink` is ever used: `blink()` maps speed 1.0 → **~3 Hz** (`hal/drivers/rgb/effects.py`), fast enough to hurt the eyes. Keep it ≤ 0.5 (~1.5 Hz or slower).

## `candle` varies brightness, never hue

`candle()` (`hal/drivers/rgb/effects.py`) gives each pixel its own flicker level in `[CANDLE_FLICKER_MIN, 1.0]` (`CANDLE_FLICKER_MIN = 0.8`) and scales **all three channels by that one level**: `tuple(min(255, int(c * level)) for c in color)`. Every pixel is the same color, only at a different brightness — the strip reads as one flame breathing unevenly instead of a handful of different colors.

This is the same rule as the presets themselves: dim by scaling, never by picking a new color. The hue is what the agent is trying to say.

It used to break that rule. The old implementation treated the channels separately — `r = color[0]*flicker + random.randint(0, 20)`, `g = color[1]*flicker*random.uniform(0.6, 0.9)`, `b = color[2]*flicker*0.3` — which was survivable when emotion colors were bright, but not after they were dimmed to indicator levels where peaks are only 12–16. A `+20` added to red is then **larger than the color itself**. Measured on the lamp (19/08/2026): `happy`, declared `[12, 9, 1]` at hue 44° yellow, painted pixels ranging from `(9, 5, 0)` to `(26, 4, 0)` — red up to 2.2× its own declared value, hue collapsed to 5–20°, so the eye saw a scatter of oranges instead of an even yellow. `excited` `[12, 8, 12]` (pink-purple, hue 300°) was worst: blue crushed ×0.3 while red was inflated, and it came out orange. After the fix, measured hue held: happy 36–48°, curious 36–43°, excited exactly 300°.

Emotions affected: `curious`, `happy`, `excited`, `laugh`, `confused` — the five that use candle. `breathing` and `pulse` never had this bug because they only ever scale proportionally (`int(c * brightness)`).

### …and it no longer strobes

The same pass fixed a second, independent problem in `candle`: it used to pick a fresh random level per pixel **every frame**, at `0.05/speed` — 4 Hz for `happy`/`laugh`/`confused`, 10 Hz for `excited` — stepping anywhere between 0.4 and 1.0 of the base color. That is a 60% modulation depth at 4–10 Hz, and IEEE 1789-2015 places **any** flicker below 90 Hz in its high-risk band (at 100 Hz the limit is already 1.6%). The 3–70 Hz range is the one linked to headaches and visual discomfort, and on a desk lamp pointed at a face it was reported as dizzying.

Two changes bring it inside the guidance without changing what the effect is:

- **Fixed 30 Hz refresh with interpolation** (`CANDLE_REFRESH_HZ = 30`). Each pixel now travels toward its target level instead of jumping to it — `speed` sets how fast it travels rather than how often it teleports. Steps are what the eye locks onto; a smooth ramp of the same period reads as motion, not flicker.
- **Floor raised 0.4 → 0.8**, cutting modulation depth from 60% to 20%.

Measured on the lamp afterwards: `happy` 29.2 Hz refresh at 9.1% modulation, `excited` 29.1 Hz at 18.2%, and the perceived flicker rate lands between 0.85 Hz (speed 0.2) and 2.25 Hz (`excited`, speed 0.5) — under the 3 Hz start of the uncomfortable band. The `speed * 0.6` factor in the approach term is what keeps `excited` there; raising it puts the liveliest preset back into the band.

`CANDLE_FLICKER_MIN` and `CANDLE_REFRESH_HZ` are comfort bounds, not taste settings. Do not lower either without re-reading the standard.

## LED Restore Behavior

- **User has set a color/effect/scene** → after the emotion, restore the user's color/scene (LED only — a restore never moves the servo)
- **Light is off or never set** → the emotion LED stays after the animation ends
- **`shock`** → restore after 2.0s (notification_flash self-clears after ~1.5s)
- **`idle`** → no restore scheduled (it is the ambient resting state)

## Pulse Behavior

Emotion-driven pulse (thinking / listening / scan) runs on a **black base**: the purple/green wavefront stands out on a dark strip, so the agent's expression is visible no matter what color the user has set.

Transient pulse (Buddy busy, other driver overlays via `/led/effect` with `transient: true`) **overlays the user's color** instead: pixels outside the wavefront keep the user color, and wavefront pixels alpha-blend from user → emotion. The point is to keep the user's background color continuous underneath a quick overlay.

Source: `hal/drivers/rgb/effects.py:pulse()`; the emotion path is `hal/app_state.py:_apply_emotion_led_display()` (black base by default), the transient path is `hal/routes/led.py:start_led_effect()` (base = `_get_user_base_color()` when `transient=true`).
