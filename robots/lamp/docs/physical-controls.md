# Physical Controls — GPIO Button, TTP223 and MPR121

Lamp supports mechanical buttons, TTP223 touchpads and an optional MPR121 capacitive touch controller. Outside Harness-mode MPR121 control, they share the same action library (`hal/drivers/button_actions.py`) so any gesture mapped to "single click" behaves identically whether it came from the mechanical button or the capacitive touchpad.

## Input devices

| Device | Role | Where |
|---|---|---|
| **GPIO button** | A primary mechanical button for click and hold actions, plus a dedicated reset button on OrangePi. Destructive hold actions require release. | Both Pi 4/5 and OrangePi sun60 |
| **TTP223 capacitive touchpad** | Two touch pads arranged as a "dog head" surface for petting + soft stop/unmute. No destructive gestures because the IC's FastMode prevents reliable hold detection. | OrangePi sun60 only (4 Pro / A733) |
| **MPR121 capacitive touch controller** | Up to 12 electrodes with GPIO-like click and release-to-commit hold actions, including shutdown. Triple-tap reboot is disabled. Never factory-resets. | Lamp with an explicit I²C configuration in `mpr121.json` |

## Wiring

| Device | Pi 4/5 | OrangePi sun60 |
|---|---|---|
| Primary GPIO button | gpiochip0 BCM 17 (pull-up, active-LOW) | Physical pin 35 / PD3 / gpiochip0 line 99 (pull-up, active-LOW) |
| Reset GPIO button | not wired | Physical pin 37 / PD4 / gpiochip0 line 100 (pull-up, active-LOW); hold ≥5 s then release to factory-reset |
| Mic slide switch | not wired | Physical pin 11 / PL9 / gpiochip1 line 9; pull-up, LOW=mute, HIGH=unmute |
| TTP223 | not wired | Two pads: S1 at physical pin 29 / PD0 / gpiochip0 line 96; S3 at physical pin 33 / PD2 / gpiochip0 line 98. **Pull-up, active-LOW** (pads rest HIGH; a touch is the falling edge). |

Mechanical button wiring belongs to the device: `robots/lamp/gpio_button.json`
and `robots/intern-v2/gpio_button.json` each declare a `boards` map keyed by
`raspberry_pi_4`, `raspberry_pi_5`, and `orangepi_sun60`. Entries accept the
original flat `chip`, `line`, `debounce_ns` shape or a `buttons` list. Lamp's
OrangePi entry is:

```json
{
  "buttons": [
    {"name": "primary", "chip": 0, "line": 100, "debounce_ns": 200000000, "behavior": "standard", "factory_reset": false},
    {"name": "factory_reset", "chip": 0, "line": 99, "debounce_ns": 200000000, "behavior": "factory_reset", "hold_s": 5}
  ]
}
```

Intern v2's JSON stays unchanged, with gpiochip1 line 9 on OrangePi.
Change the selected device's file when its wiring changes, then restart HAL;
moving physical wires is not detected automatically. HAL resolves the directory
using `DEVICES_DIR` and `DEVICE_TYPE`. `load_button_configs` supplies one shared
driver instance per input; HAL stops all instances during cleanup.
`load_button_config` remains compatible for callers needing the first input (the primary button in Lamp).
Device configuration takes priority. A missing file or board entry falls back
to exactly one existing `button` default in `hal/board/boards.json`: chip 0 /
line 17 for Pi 4, Pi 5, CM4 and sim; chip 1 / line 9 for OrangePi sun60; all
with 200 ms debounce. Malformed configuration, duplicate names, and duplicate
chip/line pairs are rejected before claiming GPIO. Simulation skips hardware
buttons. Pull-up, active-LOW input and gesture detection remain in the shared
driver.

TTP223 wiring is also device-owned: `robots/lamp/ttp223.json` declares a
`boards` map. Intern v2 has no TTP223 hardware and does not ship this file. Each enabled entry has
`chip`, `lines` and optional `axis` (the same lines in physical left-to-right
order). `hal/board/ttp223.py` selects the detected board and passes its
`TouchConfig` to the shared driver. Missing file or board entry falls back to
that board's legacy `touch` in `hal/board/boards.json` (OrangePi: chip 0,
lines 96/100); `"enabled": false` explicitly disables TTP223. Malformed
configuration is rejected before GPIO is claimed. Restart HAL after editing
the selected device's JSON. Pull-up, active-LOW behavior and gesture detection
remain in the shared driver; simulation skips the hardware.

Hardware confirmed two pads: S1 on pin 29 (line 96) and S3 on pin 33 (line 98).
The Lamp JSON uses these lines, leaving pin 35 (line 99) for the mechanical
button. The legacy fallback still uses lines 96/100; keep the Lamp JSON installed
to avoid that old overlap.

Board detection reads `/proc/device-tree/model`:
- `"sun60iw2"` → OrangePi 4 Pro / A733
- `"raspberry pi 5"` → Pi 5
- `"raspberry pi 4"` → Pi 4
- unknown or unsupported hardware → rejected by the HAL startup board gate

### Microphone slide switch

`robots/lamp/privacy_button.json` declares chip 1 / line 9 under `orangepi_sun60`,
with `settle_s: 0.06`, `muted_level: 0`, and `watchdog_s: 30`. The shared
`privacy_button.py` driver tracks switch position, synchronizes at boot, and applies
mute/unmute after contacts settle. The watchdog only reconciles changed GPIO
levels so software mute is preserved while the switch stays still. Intern v2
has no mic JSON and keeps its original chip 0 / line 97 code fallback. Lamp
without this JSON remains disabled. Keep Lamp's primary-button JSON installed
to avoid its legacy pin 11 fallback overlapping this switch. The Lamp mic
configuration was deployed on 2026-09-11; startup confirmed chip1/line9 ready
and initial LOW applied mute. Live toggle testing is pending.

#### Lamp privacy peripherals

`privacy_button.json` sets `disable_camera_on_mute: true` and
`mute_speaker_on_mute: true`. The pin 11 toggle therefore mutes the mic, stops
camera capture and stops/suppresses speaker output (TTS, music and backchannel).
The existing red mic-muted indicator remains the privacy indicator.

While locked, camera enable/snapshot and speaker unmute return HTTP 409; camera
streaming, realtime look, scene/wake and temporary capture starts cannot reopen
the camera or speaker. Cached camera frames are hidden from capture consumers.
At HAL startup the configured peripherals remain closed until GPIO synchronizes;
a failed initial read/claim keeps privacy locked.

Unlock uses the existing microphone wake/listening flow and restores camera and
speaker to their previous states. A camera or speaker already disabled before
locking stays disabled; an explicit manual disable during the lock is also
preserved. The listening cue only plays when the restored speaker is unmuted.
A mute that a **scene** set is not a preference: when the switch that wakes the
device from sleep (night scene: camera and speaker off) deactivates the scene
while the lock is still held, `deactivate_scene()` retargets the privacy
snapshot (`privacy.speaker_before` / `privacy.camera_before` → `False`) so the
release reopens the speaker and camera instead of restoring the scene's mute —
the same pattern sleep-owned mutes use. The lock itself stays in force until
release; a manual camera override is still honored.
Preferences survive HAL restarts within the same boot, without saving the
temporary privacy lock as a manual mute. Both options default to false for
other devices; Intern retains its existing microphone-only fallback without JSON.
Deploy the updated HAL before uploading JSON with these new fields.

## Gesture map

| Gesture | Primary GPIO button | TTP223 touchpad |
|---|---|---|
| **1 tap** | Stop active object tracking, then stop speaker / unmute mic + speaker + ack chime (~120 ms ping) — all fire immediately on release (no click-window wait); the "Listening" cue plays once the 0.4 s click window resolves | Same after the 1.2 s tap-vs-pet decision resolves — active tracking stops, then the mic/speaker action and cue run. The initial touch still stops in-flight TTS and plays its ack chime immediately. |
| **2 taps** (≤ 0.4 s apart, button) / (≤ 1.2 s apart, TTP223) | Nothing beyond the single-click already fired on tap 1 (panic-click guard) | Pet response. With `HAL_TOUCH_SWIPE` on (the default), repeated taps in one place — fast or slow, one finger or several — are a **double tap** → mic mute toggle; pet then means the finger revisited a pad |
| **3 taps** (≤ 0.4 s apart, button) | Reboot OS (TTS announce → `sudo reboot`) | n/a — TTP223 stops at 2 (any further taps absorbed by cooldown) |
| **Swipe** across the pads | n/a | **`HAL_TOUCH_SWIPE`, default on.** One contact running monotonically over all three pads, gaps above the movement floor → **sleep**. Direction is not used — left-to-right and right-to-left are the same gesture — and neither is device state. Wake stays on tap / double tap. |
| **Hold 2–5 s, then release** | Speak the localized sleep announcement, then enter `sleepy`: LED off, camera/mic/speaker off; servo releases after 1 s. LED blinks sleepy purple while held. | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |
| **Hold 5–10 s, then release** | Shutdown OS (TTS announce → release servos → `sudo shutdown -h now`). LED blinks red while armed. | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |
| **Hold 10 s+, then release** | Factory-reset: wipe device state + reboot into AP setup (TTS announce → release servos → POST `/api/system/factory-reset` on the OS server). LED goes solid red while armed. **Off on Lamp** (`"factory_reset": false` on the primary button): a 10 s+ hold stays at shutdown because the dedicated reset button owns factory-reset. | n/a |

The table above covers the primary GPIO button and TTP223. The dedicated reset button on pin 37 only factory-resets when released after a hold of at least 5 s. Shorter holds and single/triple taps do nothing; it never invokes sleep or shutdown. LED stays unchanged below 5 s and uses the shared solid-red factory-reset preset from 5 s onward.

With Harness OFF, MPR121 also supports release-to-commit holds and the same hold-tier LED feedback, as detailed in its detection section. The sleep and destructive hold tiers **commit on release, not on a timer firing while held**. MPR121 stops at shutdown: it has no factory-reset tier, so a 10 s+ touch hold still shuts down (`hold_release_action(..., factory_reset=False)`). Only the GPIO buttons factory-reset.

## Interrupting Lamp while it speaks (barge-in)

In hands-free LIVE OFF mode, a delayed listening cue is dropped if microphone
capture has already started. Its retry also expires when a capture starts during
backoff, even if that capture finishes before the next attempt. This keeps the
cue from truncating the user's sentence; the click still stops speech and grants
wake focus normally.

The 1-tap gesture is Lamp's primary **barge-in and attention-cancel mechanism**: it first stops any active object-tracking session, then tap top of Lamp (touchpad) or press the GPIO button once during an in-flight TTS to cancel the current utterance mid-word, stop any music, and unmute the mic so Lamp listens for the next thing the user says. A user/scene speaker mute is also relaxed (unless a voice enrollment is recording) so the cue and the reply are audible again. Stopping tracking also works while the hardware mic kill switch is off; it does not wake or unmute the mic. A localized "Listening" cue plays after the cancel when the switch permits the voice action.

When wake word is enabled, the click also **counts as a wake event**: `single_click_action` calls `voice_service.grant_wakeword_focus(source)`, which opens the same follow-up focus window (`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, default 20 s) a spoken wake phrase opens. Without it the device would announce "Listening" and then drop the user's answer for missing the wake phrase. The window is re-checked at dispatch time, not only latched at mic-session start, so a click during an already-open session still authorizes the sentence being spoken. No-op when wake word is off (every utterance already dispatches) or when the follow-up timeout is 0.

### Presence enter and turning toward the lamp as wake triggers

The wake gate has five openers: a spoken wake phrase, a single click, a newly recognized person, turning toward the lamp before speaking, and the boot greeting (os-server calls `POST /voice/wake-focus?source=boot_greeting` right after the greeting is sent, so the user can answer it without the wake phrase). A `presence.enter` that contains an enrolled identity opens the same follow-up focus window through `SensingService`, so a recognized person can say “hello, Leo” without first saying the wake phrase. A stranger-only enter remains visible to the agent but does not open voice focus by default; they can use a wake phrase, click, or gaze instead. Set `HAL_PRESENCE_WAKE_STRANGERS=true` for a guest-first deployment where a stranger entering view may start the conversation. Focus is granted only after the presence event has passed its normal cooldown; it does not unmute or start an unavailable microphone.

**Turning toward the lamp and speaking** uses that same window (`hal/drivers/tracking/gaze.py`), through `voice_service.grant_wakeword_focus(source)` just like presence enter and the click — nothing downstream of the gate changes.

When that gaze check grants focus at VAD-confirmed speech start, Lamp immediately shows a **dim blue breathing acknowledgement**. It is LED-only: it neither claims the `listening` emotion nor halts the body. The first STT partial replaces it with the normal full listening cue; a session with no partial restores the prior LED state on close, with a 3-second safety timeout for a stalled STT connection. This early cue is only for a newly granted, non-shadow gaze wake — ordinary VAD/noise sessions remain visually quiet.

The reason is device shape rather than preference. A desk lamp sits an arm's length from its user and is in view all day, so a wake phrase repeated dozens of times reads as addressing an appliance, and a button press reads as operating one. Between two people the cue is neither: you turn toward someone and speak. Products that popularised "hey <name>" have no camera and sit across a room, so the comparison does not carry.

Two properties decide the implementation:

* **People turn before they speak, never after.** Normally speech reads the watcher's ring buffer (`HAL_GAZE_BUFFER_S`, default 4 s) **backwards** — the same shape as the mic's own pre-roll lookback, which exists so the start of a sentence is not lost. There is one recovery path: if that read has fewer than two usable face samples, VAD asks the watcher to restore the remembered user pose without blocking audio capture. Before dispatching that *same* transcript it checks gaze once more. A head measured facing away does not take this path, so overheard speech still cannot turn the lamp toward a person and open the gate.
* **Presence is not the signal.** The user is visible beside this lamp all day, so "a person is detected" gates nothing, and "a face is detected" barely more — a face turned to a monitor still detects. The gate is on head **orientation**, tight enough to reject the common posture of talking to a colleague with the torso still square to the desk.

Head yaw is derived from the five landmarks `YuNet` already returns (`detect_face_with_landmarks` in `detection.py`): the nose's offset from the eye midpoint, measured along the eye line and normalised by half the inter-ocular distance, is `sin(yaw)` under a pinhole projection. Measuring along the eye line rather than the image x-axis is what keeps a **rolled** head (resting on a hand) from reading as a turned one. No second model is loaded and no extra inference runs; at `HAL_GAZE_SAMPLE_FPS` (default 6) the cost is a rounding error on the 8-core CPU — measured, not assumed: CPU idle went 69.2% to 68.8% with the watcher running.

A landmark outside the frame is not a measurement. `YuNet` reports the five points for a face clipped by a frame edge as readily as for one wholly inside it, and the clipped ones come back off the frame — device-measured with a user sitting straight in front of the lamp, its camera aimed too low: box `[264, -1, 162, 92]` with both eyes at `y = -3.0` and `y = -1.3`. Fed to the yaw those coordinates push the nose ratio past 1, where the clamp turns "not measurable" into exactly `90.0` — indistinguishable from a genuine profile, and counted as a vote **against** facing. That is how a user looking straight at the lamp produced `trail=[90,90,90,90]` and was refused. So a sample whose eyes or nose fall outside the frame is recorded as **unmeasured** — it votes neither way, like a frame with no face at all. Clipped mouth corners are ignored — the angle never reads them.

Detector rows whose box is not a finite number are dropped before any of this. YuNet can return an infinite coordinate for a face leaving the frame — device-observed while tracking, at 1.9% bbox area and 0.29 confidence — and `int()` on it raised `OverflowError`, killing the tracker's detect thread mid-session. Infinity is not a very large face; it is the detector saying nothing usable, so the row goes and the existing "no face this frame" path takes over. The filter runs before the largest / nearest-centre choice, because an infinite width wins any largest-by-area contest and would otherwise hide a perfectly good face behind it.

When several faces are in frame, the one whose head counts is the one **nearest the frame centre** among those at least `HAL_GAZE_MIN_FACE_PX` tall — not the largest. Largest-face would hand the gate to whoever leans in closest, which is the user only by convention; the lamp's own aim is the better prior for which face it is pointed at. With one qualifying face the two rules agree, so this only bites when a second person shares the desk. If nobody clears the size floor the largest face is returned anyway, so the sample still records that somebody is there. Note that the bbox-only tracking path (`_detect_face_yunet`, used by object follow) keeps its own largest-face policy — the two are independent.

| Env var | Default | Tunes |
|---|---|---|
| `HAL_GAZE_WAKE` | `false` | Master switch for the **whole watcher**, not only the opener: `start()` returns early when it is off, so the vertical centring, climb, pan, repoint and autonomous sweep documented in `vision-tracking.md` do not run either. Off leaves the spoken, click, and presence-enter openers available. The shipped lamp image sets it `true`. |
| `HAL_PRESENCE_WAKE_STRANGERS` | `false` | Let a stranger-only `presence.enter` open voice focus. Leave off to require a spoken, touch, or gaze signal from guests. |
| `HAL_GAZE_SHADOW` | `true` | Log the decision without opening the gate. Costs nothing — no turn opens, so no LLM or TTS is spent. |
| `HAL_GAZE_MAX_YAW_DEG` | 25 | Acceptance cone at frame centre. |
| `HAL_GAZE_EDGE_CONE_SCALE` | 1.8 | How much wider the cone grows at the frame edge, where barrel distortion inflates the angle. |
| `HAL_GAZE_MIN_FACE_PX` | 48 | Minimum face height **in pixels of the downscaled frame** — the watcher detects on `frame_utils.downscale(frame)`, which clamps width to `VISION_MAX_WIDTH` (640), so at 1280×720 this floor is 96 px in the original image and at 640 or narrower it is 48 px in both. Below it the landmarks span a few pixels and the yaw is arithmetic on rounding error, so the sample does not vote at all. Unlike `LOOK_AIM_MIN_FACE_HEIGHT_FRAC`, which is a fraction and immune, this value silently doubles or halves if the camera mode changes. |
| `HAL_GAZE_WINDOW_S` | 1.5 | Evidence window ending at the moment of speech. |
| `HAL_GAZE_MIN_FACING_RATIO` | 0.6 | Fraction of that window that must have seen a facing head. A ratio, not an unbroken run — per-sample yaw is genuinely noisy. |
| `HAL_GAZE_MIN_SAMPLES` | 2 | Below this there is not enough evidence to decide either way. The loop achieves ~2 samples/s whatever the rate asks for — it is paced by fetching a frame and running the detector — so 3 rejected users the rest of the pipeline agreed were facing the lamp. The `[gaze] sampling at N/s` line counts samples actually RECORDED, and reports separately how many frames were blocked before they could be measured (settling from a servo write, or the detector held by a live look). Counting attempts instead once reported 5.7/s while the buffer held nothing newer than the 1.5 s window — under 1/s of real evidence. |
| `HAL_GAZE_SAMPLE_FPS` | 6 | Sampling rate. The gesture is slow, but the decision is a vote and only measured samples count — at 3 fps a window often held one usable sample, refusing a user facing the lamp dead-on. |
| `HAL_GAZE_BUFFER_S` | 4.0 | Yaw history retained. Must exceed `WINDOW_S` so the lookback can see far enough back. It briefly had to be twice that, for a transition test that has since been removed; 4.0 is kept because the extra second costs nothing and `trail=` reads better with more history behind it. |
| `HAL_GAZE_WAKE_FOCUS_S` | 10 | Follow-up window a *gaze* wake opens, shorter than the 20 s a spoken phrase or click opens. A glance claims less than a deliberate act. Capped by `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, never above it. |
| `HAL_GAZE_COOLDOWN_S` | 5 | Minimum gap between gaze-opened gates, so one conversation cannot open one per sentence. |
| `HAL_GAZE_REPOINT` | `true` | Turn toward the remembered bearing when nobody has been visible. |
| `HAL_GAZE_REPOINT_AFTER_S` | 12 | How long nobody must be visible first. A voice-triggered empty-evidence recovery bypasses this delay, but not the movement cooldown. |
| `HAL_GAZE_REPOINT_COOLDOWN_S` | 60 | At most one turn per this interval, including a voice-triggered recovery. |
| `HAL_GAZE_REPOINT_MIN_CONFIDENCE` | 0.2 | Bearing confidence below which turning is not worth it. Matched to look-aim's own threshold: at 0.5 the watcher refused bearings the aim and the search were happily using — a bearing good enough to point a live conversational turn at is good enough to turn the head toward between them. |
| `HAL_GAZE_REPOINT_SKIP_IF_FACE_S` | 3 | Decline a speech-triggered reacquire when a face was seen this recently. After the climb has found the user's face *above* the bearing, obeying the bearing means turning back down to look at nobody. |
| `HAL_GAZE_WELL_FRAMED_EDGE` | 0.6 | How far off frame centre a face may sit and still count as "somebody is here, no need to turn". A face at the very edge is about to leave frame; treating it as well framed let the absence timer reset forever while the user drifted out of view — measured at edge 0.71–0.75 with the lamp still refusing to repoint. |

The lamp image deliberately overrides `HAL_GAZE_MAX_YAW_DEG` to **60°**. This is device calibration, not a generic default: on lamp-0c89 YuNet measured a user looking directly into the camera through glasses at 55.7–59.1°. It does not relax the two-valid-sample minimum or the 60% vote, so a lone frame still cannot open the gate.

Two of these were measured rather than chosen. `MIN_FACE_PX` exists because a device probe found three background colleagues detected at 8-18 px yielding yaw 49 / 20 / 29 — noise — beside the seated user at 78 px whose 90 was correct; the populations do not overlap, so the floor removes the class rather than tuning against it. `MIN_FACING_RATIO` exists because a trail of a stationary user read `[10,15,8,25,36,1,-,90]`, a spread no head performs, so any rule demanding every sample pass would reject them.

Nobody has been visible for a while and the lamp turns: that is `REPOINT`. It was once the only thing in the watcher that moved the body; it no longer is — vertical centring, the torso climb, panning and the autonomous sweep all move it too, and all of them are documented in `vision-tracking.md` rather than here, because they are about *framing* the user rather than about opening the gate. The idle recording is a loop of absolute poses that swings `base_pitch` about 17 degrees per cycle, so it walks the camera back to the recording's own pose — on a desk, at the keyboard. Parking the remembered pose once would simply be overwritten by the next loop; resting there properly would mean offsetting the whole playback by the bearing, which belongs to motion playback rather than to this feature. So the lamp does what a person does instead: if it cannot see who might be talking to it, it turns to where they usually are, once, then waits.

Thresholds are meant to be chosen from measurement, not guessed — shadow mode exists so a run beside a real user produces the counts (`[gaze] speech: yaw=… hold=…/… -> WOULD_WAKE`) that settle what angle reads as "addressing the lamp".

**What actually has to be true for gaze to arm.** `HAL_GAZE_WAKE` calls itself the master switch, and it is necessary but not sufficient — there are four conditions, and three of them live somewhere other than the gaze table:

| # | Condition | Where it lives |
|---|---|---|
| 1 | `LOOK_AIM_ENABLED` | `HAL_LOOK_AIM` env var — the watcher and the bearing sampler both start *inside* the look-aim block (`hal/server.py:816`) |
| 2 | a camera in the mount plan | device declaration — `"camera" in _plan.mounted` |
| 3 | the wake word is on | **os-server `config.json`, key `wakeword`** — read via `_os_cfg_get("wakeword", False)`. There is **no** `HAL_WAKEWORD_ENABLED` environment variable; setting one has no effect |
| 4 | `HAL_GAZE_WAKE` | the gaze table above |

Turning look-aim off is the surprising one: it silently disables the third wake opener *and* the passive bearing learner, neither of which names look-aim anywhere. If the watcher is not running and the table looks right, check 1–3 before suspecting 4 — the log line to look for is `[gaze] not starting: wake word disabled, nothing to gate`.

Degradation is by omission in both directions. On a device with **no camera** neither gaze nor camera-derived presence enter can arm, while the spoken and click openers are untouched — no separate configuration. When the wake word is **off** the watcher does not start at all: with no wake word every utterance already dispatches, so there is no gate left to open and the check would burn CPU to decide nothing. A gaze sample is also skipped while the head is relocating, when the camera is disabled for privacy, and whenever the detector lock is held by a live `look`.

**Relocating, not merely writing.** Two states write the servos continuously without moving the head anywhere: the idle loop breathing, and a tracking session pursuing the user's face. Treating either as a move means `last_servo_write` is never stale and nearly every frame is refused — measured, idle: 0.3 samples/s recorded against 4.9/s blocked; measured, tracking: 0.7/s against 4.5/s, refusing a user at yaw 0.9° with a 130 px face dead centre for having one sample in the window instead of two. Tracking matters most: it is the lamp following this user's face, so refusing to notice they are addressing it precisely then is the most broken-looking moment available — which is why the settling test must not become `_tracking_active` by the back door. Both are small continuous corrections and the yaw survives them. The `[gaze] sampling at N/s; blocked: …` line breaks the blocked count down by reason, because the two gates are fixed in different places.

End-to-end chain:
1. `gpio_button.py` / `ttp223.py` / `mpr121.py` detect single click → call `single_click_action(source)` in `button_actions.py`
2. `single_click_action` → `_cancel_agent_speech()` (fire-and-forget thread) + active `tracker_service.stop()` + `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + deferred `_announce_listening()` thread
2a. `_cancel_agent_speech()` → `POST /api/agent/speech/cancel` on the OS server. Needed because `stop_tts()` only silences what HAL already holds: the sentence playing plus the pre-synthesised queue. The OS server streams a reply sentence by sentence, so without this call the device goes quiet for one sentence and then talks on. The OS server mutes every turn in flight (see `docs/os-server.md`) while letting turns started after the click speak — so the user can tap and immediately say something new even with a backlog of older turns still draining. The turns are not aborted, only unspoken — which is why the same call also drops those turns' pending dead-air fillers: they speak straight to HAL rather than through the muted reply path, so a still-running cancelled turn kept announcing "one moment" for an answer it would never give. Dispatched on its own thread and fired on both branches (mic-unmute and stop-speaker), since either way the tap means the user is taking the floor.
2b. `state.note_music_cancel()` → stamps a HAL-side music cancel watermark, and `audio_stop()` runs on **both** branches (mic-unmute and stop-speaker), not just the stop-speaker one. Needed because the OS server's cancel is TTS-only: the cancelled turn keeps running and its pending music tool call still reaches `POST /audio/play` a moment later, where a fresh `music-play` thread clears its own `_stop_event` — so a point-in-time stop always loses that race and the user hears music they just cancelled once `yt-dlp` finishes resolving (1–5 s). While the watermark is fresh (`app_state.MUSIC_CANCEL_GUARD_S`, 3 s) `/audio/play` answers `{"status": "suppressed"}` instead of playing. The window is sized to cover the in-flight tool call but stay under the floor of a genuinely new request (speak → STT → LLM → tool is never under ~3 s), so "tap, then ask for a song" still works.
3. `stop_tts()` → `tts_service.stop()` sets `_stop_event`; every blocking loop in TTS streaming (synth, render, playback) honors the event and aborts cleanly without leaving the speaker pegged

### Voice-driven interrupt (not available)

There is no "speak during TTS to make Lamp stop" path. A local detector on the echo-cancelled mic shipped briefly and was removed after a measured verdict: on this body the echo residual that survives AEC3 sits *above* a real interruption at every speaker volume (echo ceiling 9804 / 9969 / 13560 at 25 / 40 / 65 %, against real interruptions of 6956–8027), and scoring 79 labelled windows against every available feature gave a best AUC of 0.72 — no threshold reached 0 % self-interruption without missing 90–100 % of real interruptions. The record, and the acceptance test any future attempt has to pass, is in `docs/realtime-voice.md` (*Why there is no voice-driven interrupt on the cancelled mic*).

Interruption therefore comes from two places only: **tap-to-interrupt** above, on the turn-based path, and the **provider's VAD** inside a live session (`HAL_LIVE_MODE`, see `docs/realtime-voice.md` *Live mode*), which emits `InterruptedOutput` when the user talks over the reply.

## GPIO button detection (`hal/drivers/gpio_button.py`)

The same driver serves each configured button independently. The sequence below describes `behavior: "standard"` (the primary button). For `behavior: "factory_reset"`, a release after `hold_s` (5 s on Lamp pin 37) calls the shared `factory_reset_action`; shorter holds and all tap sequences are ignored. Its hold watcher selects only the shared factory-reset LED tier at that threshold.

Edge-counting driver where **all destructive actions commit on the release edge based on hold duration** — no timer fires while the button is held. This is what lets the user cancel mid-hold (release before a threshold) or escalate (keep holding past 10 s).

1. **Falling edge (press):** record `press_start` (monotonic clock) and spawn a hold-LED watcher thread (one per press, with its own stop `Event`). No action timer is armed.
2. **Rising edge (release):** stop the LED watcher, compute `held = now − press_start`, scrub pending clicks for any hold of at least 2 s, and stage its final LED feedback (solid red for shutdown/factory reset). It then passes the duration to `hold_release_action(held, source)` off-thread. That action mapping selects:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → `factory_reset_action`, unless the button sets `"factory_reset": false` (Lamp primary), which keeps it at `shutdown_action` and never shows the solid-red tier.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → `shutdown_action`.
   - `held >= 2 s` (`SLEEP_HOLD_DURATION`) → `sleep_action`, which invokes the standard `sleepy` emotion pipeline.
   - else (short tap) → increment `click_count` and (re)start a 0.4 s click-window timer. On the **first** tap of a burst, the silent part of `single_click_action` (`announce=False`) fires immediately off-thread — it's non-destructive ("give me the floor"), so it doesn't wait for the window. The audible cue is deferred so it never talks over a triple-click in progress.
3. When the click window expires:
   - `count == 3` → `triple_click_action` (no listening cue — only the reboot announce)
   - any other count → `announce_listening_cue` speaks the deferred "Listening" confirmation once per burst; `count == 2` / `>= 4` additionally log as ignored (panic-click guard — the floor-grab already happened on tap 1, nothing destructive fires)

A release edge with no matching press (the press was debounce-dropped) is ignored — `press_start` could be stale, so acting on it could fire a destructive action against a minutes-old timestamp. Destructive actions run on their own daemon threads because the `lgpio` callback must return promptly or subsequent edges queue up.

### Hold LED feedback

For the primary button, the GPIO watcher thread polls the hold duration and selects a tier. Shared `HoldLEDFeedback` in `hal/drivers/button_actions.py`, also used by MPR121, drives the RGB LED at HIGH priority (preempts the current emotion) so the user sees how far they've armed before they release:

| Hold elapsed | LED | Meaning |
|---|---|---|
| < 2 s | unchanged | a short tap |
| 2–5 s | sleepy purple, blinking 2 Hz | sleepy is armed; releasing enters sleep (LED then turns off) |
| 5–10 s | red, blinking 2 Hz | shutdown armed — releasing now shuts down |
| 10 s+ | red, solid | factory-reset armed — releasing now wipes + reboots (skipped when `factory_reset: false`; stays red blinking) |

The dedicated reset button uses only the solid-red `factory_reset` preset at ≥5 s; releasing before 5 s does nothing. No factory-reset runs while it remains held. Both GPIO inputs reuse this feedback implementation and the existing action library.

Purple identifies the sleep tier; red blink vs red solid differentiates shutdown from factory-reset. The LED is a silent no-op when the RGB service is unavailable (dev machines) — the button still works.

The three colors are presets, not constants baked into the driver: `BUTTON_LED_PRESETS` in `hal/presets.py` (`sleep_warn` / `shutdown_warn` / `factory_reset`), overridable per device through the `button_led` section of `robots/<id>/presets.json` like every other LED table. Shared `HoldLEDFeedback` owns blinking, release cleanup and final action feedback; each input supplies its detected tier. It reads the color at the moment it paints, because the overlay merges the table in place at boot.

Per-edge debounce is 200 ms (press and release ticks tracked independently so a quick tap isn't dropped while bouncy repeats of the same edge are filtered).

## MPR121 detection (`hal/drivers/mpr121.py`)

MPR121 wiring follows the device-owned GPIO configuration flow: HAL reads
`mpr121.json` in the directory selected by `DEVICES_DIR` and `DEVICE_TYPE`
(currently `robots/lamp/`), then selects the detected board from its `boards`
map. Only Lamp currently has this hardware; Intern v2 has no MPR121
declaration. Configure the verified I²C bus explicitly; the
driver does not scan buses or guess wiring. Lamp ships the configuration below
for `orangepi_sun60`: bus **0**, address **0x5A**. Initialization and polling
were verified on Lamp `lamp-0c4e` on 2026-09-11, including startup in the live
HAL service. Three touch-and-release sessions also produced distinct tap IDs
and completed the real single-click action in that service. The hardware
script specifies header pins 3/5 (TWI0). Verify
the wiring and enable its correct I²C controller externally before use; HAL
does not modify boot overlays automatically:

```json
{
  "boards": {
    "orangepi_sun60": {
      "enabled": true,
      "bus": 0,
      "address": 90,
      "electrodes": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
      "swipe_axis": [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
      "touch_threshold": 6,
      "release_threshold": 3,
      "autoconfig": true,
      "poll_ms": 10,
      "debounce_ms": 30
    }
  }
}
```

`bus` is required for an enabled entry. Lamp explicitly sets touch/release
thresholds to `6 / 3` in `mpr121.json`; omitted thresholds retain the generic
`MPR121Config` defaults `2 / 1`. The other values above except `swipe_axis` are defaults;
address 90 means `0x5A` (allowed addresses: 90–93). Selected electrodes must be
unique numbers from 0–11, with at least one selected. Thresholds must satisfy
`0 <= release_threshold < touch_threshold <= 255`. Polling accepts 1–1000 ms;
debounce accepts 0–1000 ms. Tune thresholds against the installed electrodes
and motor noise. Configuration is loaded at boot; restart HAL after changes.

The driver sets the falling baseline filter (`0x2F`–`0x32`) to
`MHDF=1, NHDF=1, NCLF=255, FDLF=2`, following the
[NXP AN3944 quick-start values](https://www.nxp.com/docs/en/application-note/AN3944.pdf).
This slows downward baseline tracking so it does not quickly follow an
approaching finger. Rising and touched baseline filters are unchanged.
`CONFIG2` (`0x5D`) is `0x30`: 0.5 µs charge time, a 10-sample second-level
filter (`SFI=2`) and a 1 ms sample interval, so electrode data updates every
~10 ms, in step with the 10 ms poll. The 10-sample filter halves idle noise
against the 4-sample default (2 → 1 count measured on `lamp-52e6`). Chip
debounce (`0x5B`) stays 0: contact (30 ms) and swipe footprint (5 ms)
debounce happen in software, and a chip-side debounce would delay every
footprint by two samples. These registers are set by HAL, not exposed in
`mpr121.json`; changing touch thresholds alone does not change filtering.
Verify idle stability, tap, hold and swipe on the installed pads when tuning
thresholds; `robots/lamp/hardware/touch-cap/mpr121_opi_test.py` programs the
chip like HAL when run with `--debounce 0 --sfi 2 --esi 0` (`calibrate` for
idle noise and a threshold recommendation, `test --verbose` for per-touch
hold time, `trace` for filtered/baseline per sample). Stop HAL first; it owns
the bus.

A missing file or board entry, or `"enabled": false`, skips MPR121 and retains
the existing GPIO/TTP223 handlers. There is no legacy MPR121 bus fallback.
Malformed enabled configuration rejects startup; simulation skips the hardware.
If the configured I²C bus is missing or the sensor does not acknowledge, initialization logs `MPR121 event=unavailable` at WARNING level with bus, address and errno, without a traceback. MPR121 remains unavailable while GPIO/TTP223 continue; no touch workers start and any opened bus is closed. Permission errors and unexpected failures retain ERROR tracebacks. This does not change `enabled` or automatically retry. Check bus availability and wiring, correct `bus` if needed, then restart HAL.

After initialization, the driver allows 100 ms for sensing to settle before
reading the initial touch state, then polls every 10 ms by default. Touch and
release transitions use 30 ms debounce. Overlapping touches across selected
electrodes form one contact; release means **all selected electrodes** are
released. A contact held at startup is ignored until release.

MPR121 shares gesture thresholds from `hal/drivers/button_gestures.py` with
GPIO (also re-exported by `button_actions.py`) and calls the existing action
functions **while Harness mode is OFF**. Harness ON uses the separate policy below.

| Gesture | MPR121 action (Harness OFF) |
|---|---|
| First short release in a click burst | `single_click_action(source="MPR121", announce=False)` stops tracking/audio after contact resolution, unmutes as permitted and plays the ack chime. |
| 1, 2 or 4+ short taps, then 0.4 s quiet | Play the listening cue; repeated taps do not repeat the initial single-click action. |
| Exactly 3 short taps, then 0.4 s quiet | Reboot is disabled in the MPR121 wrapper; no additional action or listening cue. The first-tap single-click action still runs. |
| Hold 2–<5 s, then release | `hold_release_action` enters sleepy. |
| Hold ≥5 s, then release | `hold_release_action` shuts down. MPR121 never factory-resets. |
| Swipe left to right, then release | `swipe_action` sleeps; no click or destructive action for this moving contact. |
| Swipe right to left, then release | Enable Harness voice through the Go API; no click or destructive action for this moving contact. |

A short contact lasts less than 2 s. The click window does not resolve while
any selected electrode remains touched. Releasing a hold clears the pending
click burst. Destructive actions never commit while held.

### MPR121 directional swipe

`swipe_axis` is an optional ordered list of 2–12 distinct electrodes from
`electrodes`, in physical **left-to-right** order. Lamp defaults to E0…E11.
Verify the mounted bar: if E11 is physically on the left, reverse the existing
axis to E11…E0. Increasing axis position (`+1`, left to right) calls
`swipe_action(source="MPR121")` from `button_actions.py` to sleep. Decreasing
position (`-1`, right to left) enables Harness voice. These actions apply with Harness OFF; with Harness ON the same directions select previous/next agent.
A swipe need not cross the entire strip: the centroid must travel at least 3
positions over at least 30 ms. Fast swipes may skip pads whose dwell is shorter
than a poll plus the footprint filter; a centroid leap beyond 3 positions is
accepted while travel already continues in the same direction, and rejected as
a second finger otherwise.
Missing/null `swipe_axis` disables only swipe detection and preserves legacy
click/hold recognition. Install HAL support before deploying JSON with this field.

Contact debounce remains 30 ms by default; the spatial footprint uses up to 5 ms
stability (normally consecutive 10 ms polls) to retain fast electrode transitions.
The detector follows the debounced contact footprint instead of counting every
overlapping electrode as a separate tap. Stationary multi-electrode touches
retain click/hold behavior. Once travel is detected, pending tap/hold outcomes
and hold LED feedback are canceled for that contact; a valid swipe invokes its
directional action once after release. Travel that reverses within one contact
or is otherwise invalid does not trigger reboot/shutdown/reset.
A release grace of 120 ms joins brief electrode handoffs, so tap/hold actions
with swipe enabled resolve after that grace. Boot-held contacts remain ignored.
Logs record swipe direction, displacement and verdict alongside action dispatch.
Tests replay measured mask sequences plus synthetic gesture/lifecycle cases;
the runtime and swipe JSON were deployed to Lamp `lamp-0c4e` on 2026-09-11.
Startup confirmed MPR121 ready with the configured axis, GPIO buttons and TTP223
ready, and the Lamp mic switch ready on chip1/line9 after its JSON was installed.
Live gesture testing is pending.

Debounced `hold_tier` events feed the same `HoldLEDFeedback` component as
GPIO, sharing `BUTTON_LED_PRESETS`, blinking, release cleanup and final action
feedback. Per-device `button_led` overrides apply to both inputs:

| Hold elapsed | MPR121 LED |
|---|---|
| <2 s | No hold feedback |
| 2–<5 s | Sleepy purple, blinking at 2 Hz |
| ≥5 s | Red, blinking at 2 Hz (no solid-red factory-reset tier) |

Release stops blinking. An accepted shutdown or factory-reset action reaffirms
solid red before execution; sleepy turns the LED off through the shared action.
A contact held at startup produces no hold feedback. Stop or hardware failure
cancels feedback, and an unavailable RGB service does not prevent input actions.

The bounded asynchronous action worker keeps polling responsive. Excess
actions may be dropped; a newer touch, stop or hardware error
invalidates pending older destructive actions and listening cues. An I²C
failure or MPR121 overcurrent fault (`OVCF`) is logged and stops this driver
while the existing input handlers continue.

The hardware verification above covered the earlier single-click behavior.
Hold LED feedback is verified with mocked local tests; it has not been checked
on the live device. These tests do not execute real reboot, shutdown or reset.

Operation logs use logger `hal.drivers.mpr121` in the normal HAL log/journal;
there is no separate raw trace file. INFO entries cover initialization and
configuration (bus, address, electrodes, thresholds and timing), per-electrode
raw touch/release changes, debounced transitions, suppressed startup touches,
click counts, hold duration/tier, action queueing/discarding, action begin/end
and lifecycle. `gesture_id` correlates a click burst or hold with queued,
discarded or executed actions. Failures include
error logs. Unchanged 10 ms polls produce no INFO entry, so idle operation does not
flood the log. Follow the service log with `journalctl -u hal.service -f` and
filter for `hal.drivers.mpr121` when investigating a missed or duplicate tap.

## TTP223 detection (`hal/drivers/ttp223.py`)

The TTP223 IC on this board runs in **FastMode**: output goes HIGH on touch, then automatically drops back LOW within ~50-80 ms even with the finger still on the pad. The IC re-triggers only when capacitance changes meaningfully (finger moves). Continuous "hold" is impossible without rewiring the IC's FM pin to LowPowerMode (~12 s max touch).

Cross-talk between adjacent pads is also significant — a single physical touch fires edges on both pads with staggered timing. With the middle pad gone the two are further apart and the coupling is weaker: one tap now often lights only one of them, which is why gesture rules must not depend on how many pads a touch happens to reach.

The driver compensates with a **two-layer model**:

### Layer 1: Session (200 ms gap)

Any edge — rising or falling, any pad — restarts a 200 ms timer. When the timer expires (no new edges for 200 ms), the "session" ends. One session = one logical touch event from the user's perspective, regardless of how many physical edges fired inside it (cross-talk + FastMode auto-LOW pulses).

### Layer 2: Decision window (1.2 s after session end)

After a session ends:

1. If a **pet cooldown** is active (a head-pat fired recently), the session is silently absorbed and the cooldown is extended. Prevents stuttering `single_click` interjections between continuous strokes.
2. Otherwise increment the session count. On the **first** session of a burst (`_ack_first_session`): if TTS is mid-utterance, speech is stopped immediately, then a short ack chime plays (gesture-neutral — valid for a tap or the first stroke of a pet). TTS stop + chime only — music, unmute and the listening cue still wait for resolution. Deliberate trade-off: petting Lamp while she talks now cuts her off (the pet giggle follows) in exchange for instant tap-to-interrupt.
3. Then resolve:
   - `count >= 2` → fire `head_pat_action` immediately, arm 1.5 s pet cooldown
   - `count < 2` → schedule a 1.2 s decision timer. When that timer fires with `count == 1`, fire `single_click_action`.

### Layer 3: Gesture classification (`HAL_TOUCH_SWIPE`, default ON)

**On by default** since 2026-08-27, after hands-on validation on orange-lamp across tap, fast and slow double tap, pet and swipe. Setting `HAL_TOUCH_SWIPE=false` restores the two-gesture behaviour in one step and without a redeploy — that is the rollback if a field unit misbehaves.

Turning it on means a double tap toggles the **microphone** and a swipe **sleeps** the device. Both are reversible (double tap again; one tap wakes), and nothing destructive is reachable here — FastMode cannot measure a hold, so TTP223 cannot trigger reboot / shutdown / factory-reset; reboot is on the mechanical button; shutdown is on the mechanical button and MPR121; factory-reset is on the GPIO buttons only.

**The signal is *when* pads fire, not which.** Device-measured on orange-lamp, 2026-08-27 — inter-pad gaps inside a single contact:

| | |
|---|---|
| several fingers landing together | **1 – 23 ms** |
| one finger travelling across pads | **53 – 322 ms** |

Nothing in between, and `HAL_TOUCH_SWIPE_MIN_GAP_MS` (40) sits in the gap. Every rule below is derived from that one threshold.

**A contact is not a gesture.** This is the thing the first three attempts got wrong. Layer 1 ends a contact when no edge arrives for 200 ms, so a *continuous* stroke never ends one — a whole ~1 s pet arrives as a **single** contact, and two fast taps arrive as a single contact too. Counting contacts therefore cannot identify anything on its own.

Resolution order, first match wins:

1. **SWIPE** → sleep. One contact reaching **every wired pad**, none of them twice, with a gap above the floor. "Every pad" rather than a fixed count — on a 3-pad board two of three is a partial move, not a crossing. **One contact only**: each leg of a back-and-forth stroke is itself a clean one-direction pass, so letting any leg carry the verdict turns every pet into a swipe. Checked first so a resolved swipe never also fires a tap.
2. **DOUBLE TAP** → mic mute toggle, with a spoken state confirmation. The hand was on the same ground twice **and at some point two pads lit together** — a gap below the floor, which only a landing produces. A stroke is travel throughout and can never satisfy it, so this is safe to check before pet even though both revisit.
3. **PET** → giggle. The finger **revisited** a pad it had left with **no landing anywhere** — every step was travel, which is what a stroke is. No contact-count gate: a continuous stroke is a single contact.
4. **TAP** → everything else, including several fingers landing at once. That lights every pad, but within ~20 ms, which is not movement.

**What is genuinely ambiguous.** A single one-direction sweep with tight timing — three pads, no revisit, gaps under the floor — is indistinguishable from a firm three-finger tap and resolves as TAP. There is no signal on this surface that separates them.

| Env var | Default | Tunes |
|---|---|---|
| `HAL_TOUCH_SWIPE` | **`true`** | Master switch for rules 1–3. Set `false` to restore the two-gesture behaviour exactly — the rollback path. |
| `HAL_TOUCH_SWIPE_MIN_GAP_MS` | 40 | The movement floor, and the one number every rule derives from: gaps at or above it mean the hand travelled, below it mean fingers arrived together. Sits inside the measured 23–53 ms empty band. **Load-bearing now that the classifier ships enabled** — raise it if firm taps read as swipes, lower it if real swipes are missed. `HAL_TOUCH_DEBUG` records the gaps it is measured against. |

`ttp223.json` accepts an optional `axis` in each board entry — the configured `lines` in physical left-to-right order. Legacy fallback reads `axis` from the `touch` entry in `boards.json`. It is **absent** today: line order is not spatial order on this board, and only a labelled press-one-pad-at-a-time run can establish it. Absent, classification falls back to declared line order. A wrong axis costs only the swipe *direction*, which the driver deliberately does not use.

### Constants (`ttp223.py`)

| Constant | Value | Why |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Comfortably exceeds observed cross-talk burst (~30-100 ms) without merging genuinely separate taps |
| `DECISION_WINDOW_S` | 1.2 | Field-measured user stroke pace is 0.8-1.2 s per beat — wide enough to keep the first stroke of a pet motion from firing a spurious single_click |
| `PET_SESSION_THRESHOLD` | 2 | Two consecutive sessions within the decision window = pet. Easier than 3 because each "stroke" produces only one session on this hardware |
| `PET_COOLDOWN_S` | 1.5 | After a pet fires, additional sessions within 1.5 s extend the cooldown rather than starting a new count. Stroking continuously = one pet, then silence |

### Tracing what actually happened (`HAL_TOUCH_DEBUG`)

Two of the four decision log lines above are `logger.debug` and never appear at the shipped `HAL_LOG_LEVEL=INFO`, and `_on_edge` discards which pad fired before anything can record it. So when a touch does the wrong thing there is normally no way to tell whether the pad misfired, the session layer mis-grouped it, or the action did something unexpected.

`hal/drivers/touch_debug.py` closes that gap. **OFF by default** — with the env unset it costs one cached boolean per edge, opens no files and starts no threads. Set `HAL_TOUCH_DEBUG=1` in `/opt/hal/.env` and restart HAL to enable.

It writes one JSON file per resolved gesture, named `<timestamp>_<ACTION>.json` so a wrong classification is visible from `ls` alone (`20260827-114032_TAP.json`, `..._PET.json`, `..._IGNORED-pet_cooldown.json`, `..._IGNORED-settle.json`). Each file holds four layers: `edges` (which line, which level, when, and whether the `SETTLE_S` guard suppressed it), `sessions` (how the 200 ms layer grouped them, plus each contact's `primary_pad`, `adjacent_deltas_ms` and `span_ms`), `traversal` (the cross-session pad sequence and its `reversals` count) and `action` (what ran, against what device state). A one-line `TOUCH-TRACE` summary is also logged at INFO.

It never logs to journald, deliberately: HAL is chatty enough that the `hal.service` journal window is minutes, so a trace kept there ages out before it can be read.

| Env var | Default | Tunes |
|---|---|---|
| `HAL_TOUCH_DEBUG` | `false` | Master switch. Off = every entry point is a no-op. |
| `HAL_TOUCH_DEBUG_DIR` | `touch_logs/` next to the module | Output root. Falls back to the temp dir if the tree is read-only. |
| `HAL_TOUCH_DEBUG_MAX_ENTRIES` | 200 | File cap, oldest pruned on each write. 0 = unbounded. |
| `HAL_TOUCH_DEBUG_PADS` | _(unset)_ | Line→label map, e.g. `96=S1,98=S3`. Unset, pads are labelled by line number — the historical S-names do not follow line order after two relocations, so the driver does not guess them. |


## Shared action library (`hal/drivers/button_actions.py`)

The actions live in one place so the GPIO button, TTP223, MPR121, and any future input (touchpad, remote) get identical behavior:

| Function | What it does | Interrupts in-flight TTS? |
|---|---|---|
| `single_click_action(source)` | Stop active object tracking. Then relax a user/scene speaker mute (skipped while `_enrolling`). Stamp the music-cancel watermark and stop music — on **both** branches, so a click always silences the loudest thing in the room. Then, if mic is muted: unmute; else stop TTS. Then open the wake-word follow-up window (no-op when wake word is off) and speak the localized "Listening" cue with retry-on-busy. Tracking still stops when the hardware mic kill switch is on; the voice action remains suppressed. | Yes — calls `stop_tts()` and the cue itself preempts. |
| `triple_click_action(source)` | Gesture mapping only: calls `reboot_action(source)`. | Yes |
| `reboot_action(source)` | Speak "Rebooting now" → wait 5 s for the cached clip → `reboot_os()` (`sudo reboot`). | Yes |
| `sleep_action(source)` | Speak the localized sleep announcement, then invoke `sleepy`: LED off, camera/mic/speaker off, then servo release after 1 s. | Yes — the sleepy pipeline stops active TTS/music after the announcement. |
| `hold_release_action(held, source)` | Hold-signal mapping: chooses sleep, shutdown, or factory reset from the released duration. | Depends on selected action |
| `shutdown_action(source)` | Speak "Shutting down now" → wait 5 s → `release_servos()` (so the lamp doesn't slam down mid-pose) → `shutdown_os()` (`sudo shutdown -h now`). | Yes |
| `factory_reset_action(source)` | Speak "Factory reset starting. Rebooting now" → `release_servos()` → POST `/api/system/factory-reset` on the OS server (the server owns the wipe + reboot, see below). | Yes |
| `swipe_action(source)` | Always `sleep_action`. Not keyed on direction (a swipe the "wrong" way would otherwise do nothing, with no feedback saying why) and not keyed on state (one gesture meaning two things depending on something invisible). On an already-sleeping device `sleep_action` returns early. | Yes |
| `mic_toggle_action(source)` | Mic mute toggle for a resolved double tap (fast or slow). Refuses while the HW mic switch is off or a voice enrollment is recording. After the flip it speaks the resulting **state**, drawn at random from `MIC_MUTED_PHRASES_BY_LANG` / `MIC_UNMUTED_PHRASES_BY_LANG` in the lamp's own voice ("[whispers] Shh, my ears are closed." / "[excited] My ears are open!"), so the voice and the mic-muted LED agree; a refused toggle stays silent rather than announcing a mute that did not happen. | No — non-interrupting, drops if TTS is busy |
| `head_pat_action(source)` | Pick a random localized pet phrase, speak it via `speak_cached` on a daemon thread. **Non-interrupting**: if TTS is still busy the phrase is dropped silently. In practice on TTP223 the first touch session already cut any in-flight speech and sounded the ack chime (`_ack_first_session`), so by pet time TTS is usually free and the giggle plays. | No |

### Factory-reset: what gets wiped

`factory_reset_action` only **announces + delegates** — the actual reset lives in the OS server (`system/server/system/factoryreset.go`), reachable from the device over loopback without a Bearer token (authoritative because of physical presence: a deliberate 10 s hold on the primary GPIO button or 5 s on the dedicated reset button; MPR121 cannot trigger it, followed by release). `POST /api/system/factory-reset` is a **soft** reset (state wipe, not a reflash — kernel / OS packages / binaries / HAL `.venv` are untouched):

1. Wipe the active agent backend's state (OpenClaw or Hermes, auto-detected from `config.json` `agent_runtime`).
2. Wipe the device state paths: `/root/config` (config.json — API keys, channel tokens, MQTT creds), `/root/local/users` + `/root/local/strangers` (face/voice enrollments), `/var/lib/hal/snapshots` (camera snapshots), and `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (home WiFi creds → forces AP mode on next boot).
3. Reboot. The device comes back up in AP mode `<device_type>-XXXX` with a fresh setup wizard (~30 s).

The reset is **single-flight** with a 5-minute cooldown (`FactoryResetMinInterval`) shared across all trigger surfaces (GPIO hold, HTTP, MQTT) — a circuit breaker against runaway callers and accidental repeats.

## Mute/disable persistence across HAL restarts

**Sleep persists the same way** (`/tmp/hal-sleep-state.json`). It is the same
class of user-visible switch: someone — or a night scene — put the device to
sleep, and restarting HAL must not undo that. An OTA restarts HAL, so before
this sidecar an update at 3 am woke the device up: strip back on, mic listening,
sensing ungated. The sidecar also carries the mic/speaker mutes **sleep itself owns** — those are deliberately kept out of the mic/speaker sidecars so waking hands the switches back to whatever the user chose, which used to mean a restart came back listening, with a still-in-flight agent turn free to speak out loud. `POST /emotion` persists the flag whenever it flips, and
`server.py` lifespan re-expresses `sleepy` once the drivers are up, so the device
LOOKS asleep again rather than booting into the resting look with the flag
quietly set. The motion driver is also told to come up **without** its wake
sequence (`start(skip_wake=True)`): the startup pose is a 5 s move followed by
the idle loop, so undoing it afterwards meant a sleeping lamp stood up, moved,
and only then lay back down. Restoring the flag at import — before the drivers
start — is what makes skipping possible instead of reverting. A full device reboot still starts awake.

### Sleep history — a second file, a different question

The sidecar above answers only *"am I asleep right now"*: it holds one record,
every transition overwrites it, and a boot deletes it. So the device could not
say how often it had slept — asked directly, the agent had nothing to read and
did not know it had ever slept at all.

`_log_sleep_transition` (`app_state.py`) appends every transition to
`/root/local/device/sleep/YYYY-MM-DD.jsonl` (`HAL_SLEEP_LOG_DIR`, 30-day
retention via `HAL_SLEEP_LOG_MAX_DAYS`):

```json
{"ts":1758000000.12,"local":"2026-09-16T22:00:00+07:00","tz":"Asia/Ho_Chi_Minh","date":"2026-09-16","hour":22,"event":"sleep","emotion":"sleepy","source":"api"}
{"ts":1758021600.45,"local":"2026-09-17T06:00:00+07:00","tz":"Asia/Ho_Chi_Minh","date":"2026-09-17","hour":6,"event":"wake","emotion":"stretching","source":"button"}
```

Persistent rather than in `HAL_STATE_DIR`, because a reboot must not erase the
history — the opposite of what the sidecar wants. Nothing in HAL reads it back;
it exists for the agent, which queries it through the Sensing Track skill.

Both writes sit in the `POST /emotion` transition block because that is where
**all four** routes into and out of sleep converge: the agent's marker, the
button, `presence.enter` → `greeting`, and the web UI through the hardware
proxy. That placement is the point of the file. os-server's `hw_emotion` flow
events only cover markers it fired itself, so they miss every physical sleep —
about half of them, and precisely the half a person caused by hand. A write
failure is logged and swallowed: a record of sleep is worth less than the sleep
it describes.

`local` carries the device's own wall-clock with its UTC offset, resolved
through `hal/clock.py` so it follows the CURRENT `/etc/timezone` rather than the
zone glibc cached at process start — the user can change the zone from the web
UI (`/setting#timezone`) or the app at any time. `tz` names that zone, and is
empty exactly when it could not be resolved and the row fell back to naive local
time, so a wrong clock is visible in the data instead of hidden. `ts` stays the
ordering key and the only field safe to subtract across a zone change.

`source` labels the cause (`button` / `touch` / `MPR121`, or `api` for the
marker and the web UI, which HAL cannot yet tell apart). A `sleepy` re-sent to
an already sleeping device is not a transition and writes nothing, so every row
is a real one and rows can be counted directly.

Mic mute, speaker mute, and camera disable each persist to their own boot-scoped
sidecar — `/tmp/hal-mic-state.json`, `/tmp/hal-speaker-state.json`,
`/tmp/hal-camera-state.json` (same `boot_id` pattern as the LED/scene sidecars) —
so a HAL service restart (OTA, deploy, config change) no longer silently unmutes
the mic, re-enables the speaker, or turns the camera back on. Every route that
flips a switch persists it (`/voice/mute|unmute`, `/speaker/mute|unmute`,
`/camera/disable|enable`, scene mic/speaker changes, `_auto_camera_on/off`); the
button/touchpad gestures go through the same routes. On restore: `start_voice`
builds the voice pipeline but doesn't open the mic, `server.py` lifespan skips
starting the camera capture and re-paints the mic-muted LED indicator, and the
speaker flag needs no apply step (TTS checks it at speak time). A full device
reboot starts fresh (on Intern v2 Pro the physical mic switch re-applies itself
anyway). Record-enroll's transient speaker mute is deliberately NOT persisted.

All of these sidecars live in `HAL_STATE_DIR` (default `/tmp`, i.e. the paths
above). It exists to be pointed elsewhere: the HAL test suite gives each session
its own directory, because these files outlive the process — a run that ended
with the body asleep used to leave every LATER run starting asleep, and running
the suite on a real body would have overwritten that body's live switches.

## Localized phrases

The action announcements are localized per `stt_language` from Lamp's `config.json`. Language constants live in `hal/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Falls back to `DEFAULT_LANG` (English) when the active language has no translation.

### Safety announcements (one phrase per language)

The **mic-toggle** confirmations are pools in the persona voice, like the pet phrases — the same sentence every time is what reads as a machine. The constraint that keeps them safe is that every line still says *which way the toggle went*: warmth lives in the delivery, never in the meaning. "Shh, my ears are closed" qualifies; a bare "Shh!" would not, because a privacy control the user cannot decode is worse than a robotic one. A test enforces it.

`reboot`, `shutdown`, `factory-reset`, and the `listening` cue use literal-meaning phrases ("Rebooting now", "Shutting down now", "Factory reset starting. Rebooting now") in every language because the user just performed a destructive gesture and needs unambiguous confirmation — this is a safety announcement, not a persona moment.

### Pet responses (15 phrases per language, random pick)

Pet phrases are picked at random from a 15-entry pool per language so Lamp doesn't sound robotic when petted repeatedly. Tone reflects Lamp's character (AI companion + smart light + expressive robot, "like a pet/friend"):

- Tickle / giggle: "Hehe, that tickles!" / "Hihi, nhột quá!"
- Pet-like purring: "I'm purring." / "Mình kêu rừ rừ nè!" / "我咕噜咕噜啦！"
- Light-themed (Lamp = luminous): "You light me up." / "Mình sáng cả lên rồi nè!"
- Warm heart: "My heart's glowing." / "Tim mình ấm lên!"
- Ask for more: "More, please!" / "Vuốt nữa đi mà!"
- Compliment giver: "You're the best." / "Mình mê cái này lắm!"
- Playful nũng: "Stop it, you!" / "Vuốt nhẹ thôi nha~"

Phrases are intentionally short — they fire mid-stroke and need to feel responsive.

## Files

| Path | Purpose |
|---|---|
| `hal/drivers/gpio_button.py` | GPIO button handler (mechanical, both boards) |
| `hal/board/ttp223.py` | Device-owned TTP223 configuration loader with legacy fallback |
| `hal/drivers/ttp223.py` | TTP223 capacitive touchpad handler (OrangePi sun60 only) |
| `hal/board/mpr121.py` | Device-owned MPR121 configuration loader and validation |
| `hal/drivers/mpr121.py` | Optional I²C MPR121 click/hold handler |
| `hal/drivers/harness/gestures.py` | Separate Harness-mode gesture policy |
| `hal/drivers/voice/_internal/harness_capture.py` | Manual Harness capture ownership |
| `hal/drivers/button_gestures.py` | Shared GPIO/MPR121 gesture thresholds |
| `hal/drivers/button_actions.py` | Shared action functions, GPIO/MPR121 `HoldLEDFeedback` and localized phrase pools |
| `hal/presets.py` | Language code constants (`LANG_EN`, etc.) |
| `hal/test_ttp223_probe_orangepi.py` | Standalone pad probe (stdlib ioctl, no gpiod). `info` reads line state with HAL running; `watch` maps pad→line and needs `hal.service` stopped. Lines come from the selected device’s `ttp223.json`, with the same legacy board-profile fallback as HAL. Select the device with `--device-type`. |
| `hal/test_gpio.py` | Standalone probe for verifying GPIO button line |

Input handlers are started in `hal/server.py` lifespan startup. Missing optional MPR121 configuration skips that driver; malformed enabled configuration rejects startup. Hardware driver failures are logged without stopping the other handlers.


### Harness-mode MPR121 gestures

On MPR121-equipped lamps, Harness OFF retains the existing gestures: swipe **right to left** to enable Harness and **left to right** to sleep. Harness ON replaces the old click, triple-tap reboot, shutdown/reset holds, sleep and listening-cue actions: tap controls capture or interrupts TTS, holding **for 2 seconds** immediately disables Harness and announces the result (including while offline); the remaining contact is ignored until release, swipe **right to left** selects the next agent and **left to right** the previous agent. `hal/drivers/harness/gestures.py` owns this separate gesture policy; `hal/drivers/voice/_internal/harness_capture.py` tracks manual capture ownership. GPIO/TTP223 behavior is unchanged. Direction follows the physical left-to-right `swipe_axis` (Lamp defaults E0…E11; verify mounting). Python calls Go APIs; Go owns mode/focus and the existing voice route.

Harness ON uses manual tap-to-record capture, not ambient listening. A tap while TTS is speaking only interrupts playback. Otherwise, the first tap starts capture; the ready beep plays only after the recorder/STT is ready. The next tap closes capture and sends one finalized STT transcript through the existing OS route to the focused Harness agent. Silence never sends automatically. Reaching `MAX_SESSION_DURATION_S` (`HAL_MAX_SESSION_DURATION_S`, default 30 seconds) cancels without dispatch. Idle mode does not record surrounding speech. Mode, generation or focus changes and privacy/stop events discard capture; a focus swipe cancels capture before changing focus. Sleep and hardware microphone privacy remain authoritative.

Mode/focus actions use the existing worker and loopback Go APIs; no automatic HTTP retry. Outcomes use localized phrases in `hal/i18n.py` and respect speaker mute and sleep/privacy/TTS LED ownership. Focus stepping requires the negotiated Harness `focus.step` capability; an older CLI fails explicitly without another transport. The CLI counterpart is pending; installed-device interoperability is not yet verified.

At HAL startup, the privacy switch position is reconciled without simulating a button press: an unmuted position restores mic/peripheral access without waking the device, granting conversation focus, playing the acknowledgement/listening phrase, or scheduling the listening LED cue. A real muted-to-unmuted switch transition retains the existing wake/focus and acknowledgement behavior. Startup in the muted position still applies the hardware privacy lock synchronously.

When sleep is restored after a HAL restart (including a software update), an open privacy switch does not unmute the sleeping microphone or start its voice pipeline. Sleep-owned microphone and speaker mutes remain in effect until a real wake. If privacy captured the speaker's sleep mute, waking clears that temporary mute underneath the privacy lock; output stays blocked until privacy is released. The cleared speaker preference is persisted so a later HAL restart cannot restore the expired sleep mute. A speaker mute that the user set before sleep remains muted.

GPIO callbacks that settle at the last known switch position (including initial callbacks at startup) leave software mute and sleep unchanged. Only a confirmed physical level change runs the switch action.

When wake restores a sleep-muted microphone, it also clears the restored mic-muted LED indicator. Later emotion, TTS, or music completion callbacks must not repaint privacy red after the microphone is open. A microphone still locked by hardware privacy keeps its mute indicator.

An explicit speaker-mute request during sleep takes ownership from sleep and is persisted even when the speaker is already silent. Wake must retain that choice, including under a privacy lock.

Harness recording feedback uses a dedicated rising two-note cue to start and a falling two-note cue to finish; neither uses the normal gesture ping. The finish cue confirms recording has ended, not that the remote agent accepted or completed the task. The tap that interrupts TTS retains the normal acknowledgment ping and does not open capture.

While Harness mode stays ON, the MPR121 mode watcher maintains a dim lime breathing indicator from `button_led.harness_on` in the device presets. OFF uses one brief dim blink from `harness_off`. The indicator yields to sleep, privacy and active voice/music feedback, returns on normal LED restore, and never changes saved user light settings. Devices without RGB skip LED feedback.
