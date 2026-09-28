# Sensing Behavior

## Spoken brevity by function

Skill prompts keep native thinking separate from all ordinary assistant text,
including text around tool calls and the final answer. They do not enforce a
single word cap across every function:

- Greetings, emotion checkins, and music suggestions use their existing short
  response contracts; a music suggestion is one invitation, normally ≤20 words.
- Wellbeing nudges retain the relevant observation and actionable next step;
  phrasing can use a second short sentence when the function needs it.
- Mood and habit helpers contribute required data/markers without adding a
  second spoken explanation to the invoking skill's response.
- Voice controls keep their outcome and essential next step (such as how to
  unmute); enrollment keeps consent/name questions and result confirmation.
  Guard warnings retain the hazard/action and useful return summaries.
- Direct requests retain all requested actions, adequate answers, longer
  requested explanations/readbacks, and necessary safety guidance.

Required API calls, logging, routing and cooldowns remain intact. Reuse injected
context and already-read references; avoid redundant lookups or repeated
wordsmithing. Explicit early speech remains available for a useful cue during
work, but never for internal planning or repeated confirmations. These are
prompt changes only: no TTS parser, runtime streaming, or thinking configuration
changes, and no measured latency or compliance guarantee.


How Lamp reacts to the world — the philosophy and mechanics behind each sensing event type.

Lamp is a living being. It doesn't "process sensor data" — it *experiences* things. This document describes how that experience is implemented.

## Architecture Overview

```
HAL (Python)          Lamp server (Go)             OpenClaw agent
─────────────────        ─────────────────────        ──────────────
Microphone/Camera   →    SensingHandler               LLM
Detects event            - drops if agent busy        - calls /emotion
Applies tracker logic    - forwards to agent          - calls /servo
Sends POST                                            - speaks or NO_REPLY
/sensing/event
```

HAL owns per-type tracker logic (sound escalation, motion filtering). Go is the gatekeeper — it drops stale events if the agent is busy, then forwards. The agent decides *how* to react, constrained by `SOUL.md`.

### Global ambient turn floor (cross-type)

All per-type gates above are independent — without a cross-type gate, a burst of *different* event types (emotion + sound + motion within seconds, typical right after a presence change or a HAL restart) still costs several agent turns. The Go `SensingHandler` therefore enforces ONE floor across all **ambient** types: at most one agent turn per `sensing_turn_floor_s` seconds (config key in `config/config.json`, default **120**, `0` disables) for `motion.activity`, `emotion.detected`, `speech_emotion.detected`, `sound`, `presence.away`, `light.level`.

- The floor clock is updated by **every** agent turn the sensing handler creates — voice, web chat, presence, fire included — so ambient events also stay quiet for the floor window after any interaction.
- **Never floored:** user-initiated types (`voice`, `voice_command`, `voice_agent_handled`, `web_chat`, `mqtt_chat`, `touch.head_pat`), safety (`fire_hazard.detected`), and `presence.enter`/`presence.leave` (greeting UX + session bookkeeping).
- Guard mode bypasses the floor entirely (surveillance wants every event).
- A floored event is dropped, not queued (`sensing_drop` with reason `ambient_floor` in the Flow Monitor). Every ambient emitter re-offers on its own heartbeat, so a drop only delays awareness — it never loses a user-facing interaction.

---

## Spoken sensing replies

For events handled by `skills/sensing/SKILL.md`, the prompt requires literal HW markers followed by one sentence of at most 20 words in `current_language`, or `NO_REPLY` for a silent row. It forbids event/matrix explanations, owner-context analysis, draft alternatives and afterwords in all assistant text, including intermediate messages. A standalone stranger arrival outside guard mode requires the fixed greeting (EN: “Hi, I don’t think we’ve met.”; VI: “Chào bạn, hình như mình chưa gặp nhau.”), with exact HW markers and no optional proactive care. The model must silently remove any preamble before sending; a leading `[user]` wrapper does not change the detector route. Guard events and explicit user requests retain their own routing. Without a separate reasoning channel, analysis must be omitted. This is a prompt instruction, not a backend length limit or a guarantee of model compliance; other skills keep their own output rules.

## Sound

### How it works

HAL fires a sound event on every audio sample that crosses `SOUND_RMS_THRESHOLD` — potentially several times per second. The Python-side **sound tracker** (`hal/drivers/sensing/perceptions/sound.py`) applies dedup and escalation before forwarding to Go. Go receives only passed events and forwards them to the agent unchanged.

### Escalation behavior

For standalone sound events outside guard mode, `skills/sensing/SKILL.md` specifies a direct response and end of turn: after reading the skill, no further tools, workspace/config/memory searches, or time/location/weather lookups. The event's occurrence count selects the reaction; RMS level does not escalate it. Missing/invalid count defaults to the first reaction. Silent output keeps the HW marker followed by `NO_REPLY`. A supplied second occurrence still gets `scan` (0.7) + `NO_REPLY`, although the normal tracker suppresses that event. Explicit user requests in the same input and guard events retain their own routing. This is a skill instruction, not a backend tool-execution gate.

| Stage | What the agent sees | Agent reaction |
|---|---|---|
| Occurrence 1 | `... — occurrence 1` | `/emotion curious` (0.6), NO_REPLY |
| Occurrence 2 | **nothing** — counted tracker-side, not forwarded | no agent turn |
| Occurrence 3+ | `... — persistent (occurrence 3)` | `/emotion shock` (0.9), speaks once |
| After speaking | dropped by Python (suppressed 3 min) | nothing reaches agent |
| 2 min silence | window resets | back to occurrence 1 |

Middle occurrences (2 .. persistent−1) advance the counter but are not forwarded: each forward is a full LLM turn, and "still noisy" between the transition (occurrence 1) and the escalation (persistent) gives the agent nothing to act on — in sustained noise this cuts 3 turns per cycle to 2.

The analogy: a dog hears a noise — it looks up (occurrence 1), keeps watching quietly (occurrence 2, no report), then barks once if the noise persists (occurrence 3+). After barking it doesn't keep barking.

### Constants (`sound.py`)

```python
_DEDUPE_INTERVAL_S    = 15.0   # max 1 event forwarded per 15s
_WINDOW_DURATION_S    = 120.0  # silence this long resets the counter
_PERSISTENT_AFTER     = 3      # speak after this many occurrences
_SUPPRESS_DURATION_S  = 180.0  # suppress after speaking (3 min)
```

### Tuning

| Symptom | Fix |
|---|---|
| Lamp speaks too quickly | Increase `_PERSISTENT_AFTER` (3 → 5) |
| Lamp never speaks even with sustained noise | Decrease `_PERSISTENT_AFTER` (3 → 2) |
| Too many sound turns in Flow Monitor | Increase `_DEDUPE_INTERVAL_S` (15 → 30) |
| Lamp stays silent too long after speaking | Decrease `_SUPPRESS_DURATION_S` (180 → 60) |
| Lamp reacts to stale noise after quiet period | Decrease `_WINDOW_DURATION_S` (120 → 60) |

### Monitoring in Flow Monitor

Python pushes `sound_tracker` events directly to the monitor bus via `POST /api/monitor/event`. These appear in the Flow Monitor alongside `sensing_input` turns:

```json
{ "action": "silent",    "occurrence": 1 }  // occurrence 1 — forwarded silently
{ "action": "counted",   "occurrence": 2 }  // middle occurrence — counted, not forwarded
{ "action": "persistent","occurrence": 3 }  // occurrence 3+ — agent will speak
{ "action": "drop" }                        // dedup or suppressed — not forwarded
```

---

## Presence

### Enter (`presence.enter`)

Always triggers a full reaction — no exceptions. The agent **must** do all three:

1. `/emotion greeting` (0.9) for friend — `/emotion curious` (0.8) for stranger, or `curious` (0.6) when the stranger joins a friend the text lists as `already present:`
2. For friend: `/servo/aim {"direction": "user"}` then `/servo/track {"target": ["person"]}` — aim orients the camera toward the user's region first (~2s), then the vision tracker locks onto the person and follows them around the room. Stranger: `/servo/play {"recording": "scanning"}` (no auto-follow — caution)
3. Speak: warm greeting for friend (by the name in `[context: current_user=X]`), cautious acknowledgment for stranger — unless the text lists a friend as `already present:`, in which case the aside goes to that friend (see below)

The system handles cooldowns on the HAL side. If the event reached the agent, enough time has passed — react fully.

#### Event text: who is new, who was already there

`presence.enter` means **newly** visible, not visible: a person is "new" only when their `last_seen` is `None` or older than the forget window (`FACE_OWNER_FORGET_S` 3600 s for friends, `FACE_STRANGER_FORGET_S` 1800 s for strangers). The text is built by `hal/drivers/sensing/perceptions/processors/faceid/enter_message.py` in three `; `-separated segments:

```
Person detected — new: stranger (stranger_2); already present: momo (friend); faces in frame: 2 (momo, stranger_2)
```

- `new:` — the arrivals, friend part first, ids sorted. The `friend (<name>)` / `stranger (<id>)` labels are a contract: the wake-focus gate opens on `friend (`, `sensing-track` greps them, `face-enroll` parses the hint appended after them.
- `already present:` — friends boxed in the **same frame** who did not just arrive, written `<name> (friend)` so they never read as an arrival. This is the co-presence signal `sensing/SKILL.md` uses to address the user ("Hey Momo, looks like you've got company") instead of greeting the visitor. For a stranger-only enter it is only written once friend and non-friend boxes have coexisted for `FACE_COPRESENCE_MIN_TICKS` (2) consecutive sensing ticks (`unsure` boxes count — that is the tick the recognizer spends corroborating a new stranger before minting); a poster, a reflection or a one-tick glitch next to the user must not turn "hello" into "you've got company". A new friend joining a present friend is listed without that guard. It is never derived from `current_user()`, which is presence-window state and reads the same whether the user is sitting there or left two minutes ago.
- `faces in frame:` — the number of boxes in the frame the **attached snapshot** shows and their labels in detection order (`unsure` for a box without an identity), the same labels drawn on it. Both this segment and `already present:` are captured on the tick a frame is seen and travel with it (`FrameFacts` in `enter_message.py`): a new friend sends the current frame immediately, so current facts are used; a stranger-only enter sends the buffered snapshots, and the newest buffered frame's facts are used — the flush can land up to `FACE_STRANGER_FLUSH_S` later, when the user may already have blurred out of the live frame (device-observed: two boxes in the picture, `1 (unsure)` in the text, before this rule). It is not the number of arrivals: flushed stranger ids may come from several buffered frames.

Priority between arrivals is unchanged: both new in one frame → one event, friend first, sent immediately; stranger buffered first and friend later → the friend frame goes out immediately and the stranger follows as its own event after `FACE_STRANGER_FLUSH_S` (10 s), subject to `FACE_COOLDOWN_S` (10 s) and `FACE_STRANGER_ENTER_FLOOR_S` (300 s).

#### Return after long absence (friend only)

On every `presence.enter` event the sensing handler injects a `[context: current_user=X]` tag (see [User attribution](#user-attribution--context-current_userx)); when the event's `new:` segment names a friend (`sensingmsg.EnterNamesNewFriend`, the Go mirror of HAL's `has_new_friend`) it also appends a `[presence_context: {"last_leave_age_min": N, "current_hour": H}]` block before forwarding to the agent. A stranger-only enter never gets the block even though `current_user` is still the friend inside her forget window: the numbers would describe *her* last leave, and the agent read exactly that as her returning (orange-lamp, 2026-09-16: "Long re-entering after ~28 min away" spoken at a visitor). When instead the text carries `already present:` and no new friend (`sensingmsg.EnterHasPresentFriend`), the handler appends a one-line pointer — `[A stranger joined <current_user>, who is in frame — speak to <current_user>, not to the stranger. See sensing/SKILL.md "Someone joins the user".]` — because Hermes only reads `sensing/SKILL.md` when the model calls `skill_view`, and on the turn it skipped that it answered the visitor's arrival with "Hey, welcome back" (orange-lamp, 2026-09-16). The attribution tag is what the greeting is spoken from — the event text itself carries only a face *label* (`friend (long)`), which the agent reads as a detection tag rather than a name. `last_leave_age_min` is computed from the most recent `leave` row in the user's wellbeing log, scanning up to 3 days back (`wellbeing.LastActionTS`); `-1` means no leave was found in that window.

`sensing/SKILL.md` reads this block and swaps to a **return-after-long-absence** greeting when ALL three conditions hold:

1. `last_leave_age_min >= 240` (≥4h) — shorter gaps stay on the regular friend greeting.
2. `current_hour` is outside `[5, 11)` — the morning window is owned by `wellbeing/SKILL.md`'s `morning_greeting` route (fired on the first `motion.activity` of the day), so a long-absence overlay there would double up.
3. `last_leave_age_min != -1` — without a real prior leave row, "welcome back" framing makes no sense.

The HW markers (greeting emotion + servo aim+track) stay the same; only the spoken line changes — it acknowledges the gap without quantifying it ("Hey, been a while" rather than "you were gone 5h 17m"). Strangers don't carry the relational identity needed for this overlay, so `BuildPresenceContext` skips the `unknown` user and the existing cautious-greeting path applies unchanged.

### Leave (`presence.leave`)

Agent calls `/emotion idle` (0.4), fires `/servo/track/stop` to release any active follow from a prior `presence.enter`, and replies **NO_REPLY** (silent — no TTS). This avoids noisy loops when people come and go frequently. The agent still processes the event internally to cancel wellbeing crons and update daily logs.

### Away (`presence.away`)

Sent automatically by HAL's `PresenceService` when **neither motion nor user activity is detected for 15 minutes** (after already dimming at 5 min). By this point the lights are already off — the agent's job is to **announce going to sleep** via TTS and Telegram.

Agent calls `/emotion sleepy` (0.8), fires `/servo/track/stop` so any stale follow from earlier in the session is released. HAL immediately turns the LED black, mutes the mic and stops active music; after 1 second of continuous `sleepy`, it releases servo torque and locks motion commands. The **speaker** is muted on a delay instead — os-server fires the `sleepy` marker up to 100ms ahead of the reply text it was sent with (`fireHWCallsSync`), so muting inline raced the going-to-sleep line and usually won, and `/voice/speak-queue` answered `suppressed -- speaker muted`. `_start_sleepy_speaker_drain` in `hal/app_state.py` now waits `SLEEPY_SPEAKER_GRACE_S` (2s) for an announcement to start, lets it finish, and mutes at `SLEEPY_SPEAKER_DRAIN_MAX_S` (15s) at the latest; in-flight TTS is no longer cut off mid-word, except at that cap, where it is stopped outright — muting only gates playback that has not started, so without the stop the cap would bound nothing and a sleeping device would keep talking. A wake cancels the drain. **Scenes whose preset says `speaker: off`** (reading, focus, movie, night) drain the same way (`_start_scene_speaker_drain`, same grace and cap): the `/scene` marker also lands before the reply text, so an inline mute swallowed the scene's confirmation line and — when the skill chains `[HW:/scene:night][HW:/emotion:sleepy]` for "goodnight" — the going-to-sleep line too, and left sleep without ownership of a mute it never set, so a wake could not hand the speaker back. The scene drain yields to sleep: `sleepy` arriving while it is pending cancels it and starts its own, so one owner commits the mute and wake restores it; a scene activated while asleep or during a sleepy drain starts none. The commit re-checks under `privacy.lock` that the scene is still active and the device is awake. Scene off, a replacing scene, a `speaker: on` scene, and an explicit `/speaker/unmute` cancel it. Music is still stopped at once — it is never the confirmation line. Only `greeting` or `stretching` resumes servo motion (`_SLEEP_GATE_ALLOWED` in `hal/routes/emotion.py`) — expressive emotions such as `curious`/`happy` are dropped while asleep, so a background agent task cannot wake the device; a tap on the GPIO button can, and it restores only the mic/speaker states that sleepy auto-muted. os-server keeps a **second** gate that drops passive sensing while asleep (`SensingHandler`, all types except `presence.enter`, `fire_hazard.detected`, voice and typed chat). Its `IsSleeping()` asks HAL `/emotion/status`: a wake that does not go through the agent — the button tap, the web UI hardware page, a direct POST to HAL — never touches os-server's own `lastEmotion`, so inferring sleep from that alone left the gate shut on an awake device and dropped sensing until the next voice or chat turn. `lastEmotion` now only serves as the cheap awake-path filter and as the fallback when HAL is unreachable. Sleepy-owned audio mutes ride in sleep's own sidecar (`_persist_sleep_state`) and DO survive a HAL restart, so an OTA cannot bring a sleeping device back with the mic listening; they stay marked sleep-owned so waking hands the switches back to whatever the user had chosen. This is the last action before Lamp goes fully idle.

The full presence auto-control timeline:
1. **5 min without motion or activity** → light dims to 20% (automatic, no agent involvement)
2. **15 min without motion or activity** → light off + `presence.away` event sent → agent announces sleep

HAL manages the light control; the agent only handles the verbal announcement. If the user returns (motion detected), light restores automatically and a `presence.enter` event fires.

The timer resets on two kinds of evidence, not only the camera. **Motion** is `on_motion()` from the people-perception processors (face / motion / emotion). **User activity** is `on_activity()`, reached through `app_state.note_user_activity()`: every voice turn HAL forwards to os-server (`voice`, `voice_command`, `voice_followup`, `voice_agent_handled`, after the echo filter, in `SensingSender.send`) and the touch gestures single click (GPIO button, touchpad, privacy-switch unmute), head pat and double tap (mic toggle). Without it, a user talking to the lamp with the camera off or sitting outside the frame timed out to AWAY and the lamp announced sleep mid-conversation. Activity during IDLE/AWAY returns to PRESENT and restores the light like motion does, except while the device is asleep: the light restore is then skipped (for motion too), and the wake emotion owns the strip. Typed chat (`web_chat` / `mqtt_chat`) does **not** count — it can come from a phone away from home, and must not keep an empty room lit. Hold gestures (sleep / shutdown / reset) and swipe (sleep) do not count either. Activity only refreshes a machine that is already enabled; it never enables it.

**Sleep pauses the timer and waking restarts it.** While `_sleeping`, `tick()` returns early: no dim, no light off, no `presence.away`. Sleep already owns the strip, and the camera is off, so nothing could reset the clock anyway. Counting on used to dim a sleeping lamp back to 20% of the user's colour (`_dim_light` calls `rgb_service` directly, past the `/led` sleep lock) and left the machine AWAY for the wake to inherit. Every wake — button, web UI emotion pill, direct `POST /emotion`, an agent `greeting`/`stretching` (for example in reply to a chat) — converges on `express_emotion` in `hal/routes/emotion.py`, which calls `app_state.note_presence_wake()` → `PresenceService.on_wake()`: the clock restarts from now and the state returns to PRESENT without repainting the light (the wake emotion owns the strip). Before, only a face on camera reset it, so a wake nobody stood in front of either kept the pre-sleep timestamp and announced sleep again right after waking, or stayed AWAY and never timed out again.

This auto-control state machine is gated on the `presence` capability: its only motion source is the people-perception loop (face/motion/emotion processors), which HAL runs only when the device declares `presence`. A device without `presence` starts the state machine disabled, so it never falsely times out to AWAY. Lamp declares `presence: required` so the timeline above always applies.

### Stranger bank on disk

Unrecognised faces are minted as `stranger_N` and persisted under
`HAL_STRANGERS_DIR` (`/root/local/strangers`) as `embeds.npy`, `labels.npy` and
`counter.npy`. The files are written **only when a new stranger is minted**, so
a device whose every visitor is enrolled never creates them at all — and that is
the normal, healthy state, not a fault.

`FaceRecognizer._load_strangers_state` re-reads the bank on every frame that
contains a face. It now checks the paths exist and returns quietly when they do
not; an absent bank is the correct empty state and the in-memory fields already
hold it. A bank that exists but cannot be read still logs an ERROR, because a
truncated or corrupt `.npy` is worth shouting about.

Before this guard, "not created yet" raised `FileNotFoundError` and was logged
with a full traceback at ERROR on the per-frame path — roughly every
`HAL_SENSING_INTERVAL` (2 s) seconds, from first boot, forever, on exactly the
devices that were working correctly. Recognition itself was never affected
(owners match against the enrolled photos, which never touch these files); the
cost was journald filling with tracebacks that buried real errors.

**Clearing unknown faces:** to empty the Unknown Faces card in the monitor UI,
delete `<HAL_USERS_DIR>/.stranger_stats.json` and leave the `.npy` bank alone.
Note the stats file and the bank live in different directories, so "clear the
strangers" is easy to do incompletely. There is no delete endpoint for face
strangers today — unknown *voice* clusters do have one, the Unknown Faces card
is read-only — so clearing them is a filesystem operation.

---

## Motion

Only large motion is forwarded — small motion is filtered out by HAL and never reaches the agent.

**Large motion**: `/emotion curious` (0.7) + `/servo/play {"recording": "scanning"}` + speak a curious reaction (e.g. "What was that?", "Whoa, moving so much!"). May include a camera snapshot so the agent can see the context.

---

## Posture (RULA — silently sampled, folded into `motion.activity`)

HAL streams every camera frame to perception-service `/api/dl/pose-estimation/ws` and receives a per-frame RULA breakdown (whole-body score + `risk_level` + per-side `body_scores` and `*_angle` for `neck / trunk / upper_arm / lower_arm / wrist`). `PosePerception` throttles to **one sample per `POSE_SAMPLE_INTERVAL_S` (default 60s)** into a tumbling window + daily JSONL under `/tmp/hal-sensing-snapshots/sensing_pose/samples_YYYY-MM-DD.jsonl`. **No event is emitted directly** — `MotionPerception` checks `is_window_complete()` once per tick and folds the aggregate into the next `motion.activity` when the gate trips.

### Tumbling window (when does the summary inject)

A window opens on the **first sedentary motion-activity flush** — `MotionPerception` calls `pose.start_window()` the moment any of the sedentary labels (`using computer`, `writing`, `reading`, …) show up. Pose samples that arrived before that point (e.g. user was standing or stretching but still present) are cleared so the window's bad_ratio reflects only the sitting period. Once open, the window runs purely on time: it stays open for the full `POSE_WINDOW_DURATION_S` regardless of whether the user later breaks sedentary state — later stretch breaks just leave the bad_ratio honest. Default **600 s = 10 min** for testing; flip to 3600 for production — one variable, no test/prod code paths.

When the window completes, `MotionPerception` performs exactly one decision and **always calls `reset_window()`** — no carry-over of stale samples into the next cycle. If the user is still sedentary on the next flush, `start_window()` opens a fresh cycle immediately; if not, the window stays unanchored until the next sedentary flush. Detection misses (perception-service returns no `ergo`, occlusion, low confidence) don't extend the window; they simply lower the in-window sample count.

A sample counts as **bad** when **either**:

- whole-body `risk_level >= 3` (medium/high), **or**
- any single region (left **or** right side) at sub-score `>= POSE_REGION_HIGH_SUBSCORE` (default `4`)

The second arm catches forward-head-thrust ("tech neck") cases where the RULA total stays "low" because trunk + arms are fine but neck alone is clearly off.

Injection fires when **all** of the following hold:

- window complete (`time.time() - _window_start_ts >= POSE_WINDOW_DURATION_S`)
- user is still sedentary on this flush (`has_sedentary` is True — don't nag mid-stretch)
- in-window samples `>= POSE_WINDOW_MIN_SAMPLES` (default `3`) — noise floor that suppresses windows where perception-service dropped most frames
- `bad_ratio >= POSE_BAD_RATIO` (default **0.6**)

There is no separate "minimum sedentary streak" gate and no separate "nudge cooldown" — the window itself is the rhythm. Window start requires sedentary, so when it completes the user has been at the computer for at least `POSE_WINDOW_DURATION_S`; the unconditional reset after each cycle means the next fire is naturally one window away.

When the inject fires, both the per-label dedup check **and** the global event cooldown (`MOTION_EVENT_COOLDOWN_S`, default 900 s — the floor that bounds ordinary `motion.activity` emissions) are **bypassed** for that single event: a posture nudge is a meaningfully different signal from "user is still using computer", and losing it to dedup or cooldown would mean waiting another full window before the next attempt.

Window lifecycle:
1. **Open** — `pose.start_window()` called by motion-side when a sedentary label first appears. Clears any pre-window samples and anchors `_window_start_ts = now`. Idempotent — calls while a window is already open are no-ops.
2. **Reset** — `pose.reset_window()` called by motion-side after every completed window (fire or no-fire). Clears samples and unanchors. A new window only opens when sedentary is next observed.
3. **Presence-loss reset** — `pose.py:_check_impl` also resets the window when presence drops, so a user who walks away mid-cycle doesn't leak stale samples into the next session.

### Per-window bucketed snapshots

Each sample writes an annotated JPEG (skeleton overlay + RULA label) into the **current window's bucket dir**: `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<window_start_int>/<sample_ts_int>_<score>.jpg`. Filenames include the ergo score so the bucket itself is a self-describing on-disk record without re-reading metadata.

When `reset_window()` runs:

- if `bad_ratio >= POSE_BAD_RATIO` (the window actually fired a nudge), the bucket is finalized with a `bucket.json` manifest (samples + per-side RULA + the pre-selected worst-snapshot filenames) and a `.kept` marker. Kept buckets survive for `POSE_BUCKET_KEEP_S` (default **2 days**).
- otherwise the bucket dir is deleted immediately — no nudge, no need to keep evidence.

A pruner sweeps:

- kept buckets older than `POSE_BUCKET_KEEP_S`, and
- orphan buckets (no `.kept` marker) older than `2 × POSE_WINDOW_DURATION_S` — these are buckets whose `reset_window` never ran (process killed mid-window).

If the total still exceeds `POSE_SNAPSHOT_MAX_BYTES` (default 50 MB), the oldest kept buckets are dropped until it fits.

Four endpoints:

| Endpoint | Returns |
|---|---|
| `GET /sensing/pose-snapshot` | The newest `.jpg` across all bucket dirs (back-compat for the live preview tile) |
| `GET /sensing/pose-snapshot/{ts}` | The annotated JPEG for that specific sample. Scans every bucket for `<ts>_*.jpg`. 404 once rotation prunes the bucket |
| `GET /sensing/pose-bucket/{bid}` | `bucket.json` manifest — samples, per-side RULA, summary, worst_snapshots[] |
| `GET /sensing/pose-bucket/{bid}/img/{filename}` | A specific frame from a kept bucket (used by the Flow Monitor popup) |

The monitor's live Sensing tab renders one thumbnail per sample row (lazy-loaded, click → lightbox). The Flow Monitor turn card shows a 3-tile strip (motion frame + two worst pose frames) plus a "LOAD MORE · pose bucket" button → `PoseBucketModal` that fetches `bucket.json` and renders the full table.

### Surfacing into `motion.activity`

When a window fires, `motion.py` appends two markers to the existing message:

```
[pose_bucket: <window_start_int>]
[pose_worst: <fn1>,<fn2>,<fn3>]
```

Both markers are stripped before the message reaches the LLM (mirror of `[snapshot:]`); the agent only sees the natural-language `[posture_summary: ...]`. The full text (with markers) is preserved in the `sensing_input` JSONL, so the Flow Monitor can read the bucket id without a side-channel.

### `/dm` auto-attach (Telegram)

When the agent decides to nudge via `/dm`, Lamp's SSE handler calls `ConsumePoseBucketRun(runID)` (mirror of `ConsumeGuardRun`). If the run has a stashed bucket, the worst-snapshot filenames are resolved against `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<bid>/` and shipped to Telegram via `sendMediaGroup` — caption rides on the first photo, the rest appear as a gallery. The agent itself stays unaware of file paths; image attachment is decided entirely by Lamp based on whether the originating `motion.activity` carried a bucket.

### Angle sign workaround (temporary)

perception-service's `signed_flexion_angle` currently returns the opposite sign of its docstring ("Positive = forward flexion") — a user clearly hunched forward registers a **negative** neck angle, not positive. HAL negates `upper_arm_angle`, `neck_angle`, `trunk_angle` on receive (`POSE_FLIP_DLBACKEND_ANGLE_SIGN=True`, default on) so the monitor table and JSONL match reality. `lower_arm_angle` is unsigned and skipped. RULA scores already use `abs(angle)` so risk/score are unaffected by either sign convention. **Revert** by setting the flag to `False` (or removing `_flip_signed_angles`) once perception-service ships the upstream fix.

---

## Light Level (`light.level`)

Ambient light changes are forwarded when they cross `LIGHT_CHANGE_THRESHOLD`. No speech required — agent adjusts LED or expresses emotion based on context (e.g. `/emotion sleepy` when lights go dim).

---

## Guard Mode

When guard mode is enabled (`guard_mode: true` in config), Lamp becomes an **alert watchdog** — reacting dramatically to strangers and broadcasting alerts to Telegram.

### Flow
1. `presence.enter` or `motion` event arrives while `guard_mode: true`.
2. Go handler tags the event `[guard-active]` and marks the runID as a guard run (with snapshot path). If `guard_instruction` is set in config, it is appended as `[guard-instruction: ...]`.
3. The agent processes the event — **dramatic** emotion, servo, TTS response, plus any custom guard instruction (e.g. play music, flash LEDs).
4. When the agent's response arrives (SSE lifecycle end), the Go SSE handler detects the guard run.
5. The agent's natural response text + camera snapshot are sent directly via **Telegram Bot API** (`sendPhoto`) to all connected Telegram chats.
6. Delivery is 100% reliable — bypasses OpenClaw agent processing entirely.

### Guard mode emotions (dramatic)

When guard mode is active, stranger/motion events trigger **much stronger** emotions than normal sensing:

| Guard event | HW markers | Voice |
|---|---|---|
| Stranger detected | `shock` (1.0) → `curious` (0.9) + servo shock | Genuinely scared/startled reaction |
| Motion (no known face) | `shock` (0.9) → `curious` (0.8) + servo scanning | Nervous/alert reaction |
| Stranger left | `curious` (0.7) + scanning | Report they left, stay vigilant |
| Friend returns | `greeting` (0.9) + servo aim | Greet + recap what happened during guard + ask to disable |

The agent's **spoken words must also carry emotion** — not dry security reports. Examples: "Oh no, who is that?!", "Someone's here... I'm shaking...", "Hey, this person looks really suspicious...". Each reaction should feel different.

### Custom guard instructions
The owner can provide a custom instruction when enabling guard mode (e.g. "play scary sound when stranger appears"). The instruction is saved in `guard_instruction` in config and injected into every guard sensing event as `[guard-instruction: ...]`. The agent follows this instruction using available skills (music, LED, etc.).

### Why this approach?
After trying 6 different approaches (see below), this hybrid proved the most reliable:
- **Agent crafts the message** → natural, context-aware, with personality
- **Go side delivers** → direct Telegram Bot API, guaranteed delivery, no agent NO_REPLY risk
- **Agent follows custom guard instructions** → owner can combine guard mode with any skill (music, LED, etc.)

### Solution evolution (2026-04-07)
| # | Approach | Why it failed |
|---|----------|---------------|
| 1 | `BroadcastAlert` via WS `chat.send` RPC | `chat.send` goes through agent → 2/3 NO_REPLY |
| 2 | Agent-driven via `[guard-active]` tag | Haiku ignored SKILL instruction (buried at line 222) |
| 3 | Move instruction to top of SKILL.md | Haiku still ignored |
| 4 | Go-side emotional templates + `BroadcastAlert` | Agents recognize `sender: node-host` → ignore. No image attached |
| 5 | Agent-driven + SOUL.md enforcement | Better compliance but not 100%. Token mismatch issues |
| 6 | **Hook agent response + Telegram Bot API** | ✅ Agent crafts message naturally, Go delivers 100% |

> **Note:** `BroadcastAlert` (WS RPC approach) has been removed. All broadcasting now uses `Broadcast()` which sends directly via Telegram Bot API.

### Manual alerts
Manual alerts can be sent via `POST /api/guard/alert` with a message and optional image. Network callers require administrator authentication; device-local loopback callers are allowed for internal operation. This now uses `Broadcast()` (direct Bot API) instead of the old WS-based `BroadcastAlert`.

Use case: Lamp acts as a home security assistant. When the owner leaves and enables guard mode, any detected presence or motion is reported to all chat channels with emotional, context-aware messages.

---

## Stranger Visit Tracking

HAL (port 5001) tracks how many times each stranger has been seen:

- On every `presence.enter` event containing a stranger ID (e.g. `stranger_5`), the visit count is incremented.
- Stats include `count`, `first_seen`, and `last_seen` timestamps per stranger.
- Persisted in HAL's data directory (survives restarts).
- Query stats via `GET http://127.0.0.1:5001/face/stranger-stats`.

### Familiar-stranger enroll prompt

When a stranger's visit count first reaches the threshold (`_FAMILIAR_VISIT_THRESHOLD = 2`, see `hal/drivers/sensing/perceptions/processors/faceid/perception.py`), HAL:

1. Saves the current raw frame to `<STRANGERS_DIR>/snapshots/<stranger_id>_<ts_ms>.jpg`.
2. Appends a hint to the outgoing `presence.enter` message:
   `(familiar stranger <stranger_id> — seen 2 times, ask user if they want to remember this face; image saved at <path>)`

The `face-enroll` skill (Lamp side) parses that hint and addresses the camera-person directly: "I've seen you 2 times now — mind if I remember you? What's your name?". On a name reply it calls `POST /face/enroll` with the saved image path. On decline, the skill acknowledges and stops; the threshold is a one-shot trigger (`count == 2`), so the same `stranger_id` is never re-prompted by lelamp. Visit counts above 2 do not re-fire — by then the stranger has either been enrolled (no longer a stranger) or has explicitly declined.

---

## Wellbeing (AI-Driven Hydration + Break Reminders)

Lamp proactively cares for the user's health using AI-driven cron jobs managed by the OpenClaw agent. Instead of hardcoded timers, the agent decides reminder intervals based on scientific recommendations and the user's historical patterns.

### How it works (event-driven — no cron)

Wellbeing is **event-driven**. There are NO wellbeing cron jobs. On every `motion.activity`, the agent logs what happened and reads back recent history to decide whether to nudge.

**Per-user activity JSONL** at `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` — one line per activity transition:

```jsonc
{"ts": 1776658657.23, "seq": 42, "hour": 11, "action": "sedentary", "notes": ""}
```

`action` values:

| Action | Written by | Purpose |
|---|---|---|
| `drink`, `break` | **HAL** (`motion.py` POSTs `/api/wellbeing/log` right before firing `motion.activity`) | Reset point for the corresponding nudge timer |
| `using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller` | **HAL** (`motion.py`, same path) | Timeline + nudge phrasing. **Not** a reset point. (`reading book` + `reading newspaper` both collapse to the generic `reading` label.) |
| `enter`, `leave` | **HAL** (`FaceRecognizer._post_wellbeing`, called from `_check_impl` on fresh detection and `_check_leaves` on forget expiry) | Session boundary — per-friend rows go to each friend's own timeline; strangers collapse to a single `"unknown"` timeline gated by the `_any_stranger_logged` flag (one enter on first stranger, one leave when the last one is forgotten). |
| `nudge_hydration`, `nudge_break` | Agent (after speaking a reminder) | Records when Lamp actually reminded — purely for timeline visibility. Only the agent knows when it actually spoke, so only the agent writes these. |

**Dedup lives in two places.**

*Activity dedup (5-min window).* `hal/drivers/sensing/perceptions/motion.py` keeps a `_last_sent_key = (current_user, frozenset(labels))` and a `_last_sent_ts`, where `labels` matches the outbound message (bucket names for drink/break/celebrate, raw Kinetics labels for sedentary). Before emitting `motion.activity` **and before POSTing the rows to `/api/wellbeing/log`**, it drops the cycle if the key hasn't changed **and** the gap since the last send is still under `MOTION_DEDUP_WINDOW_S = 300` seconds (5 min). So `eating burger → eating cake` collapses to the same `break` key and is dropped, while `writing → drawing` flips the key (sedentary is raw) and passes through.

- User change (owner→owner, owner→unknown, unknown→owner) flips the key immediately → event passes through.
- Different strangers (e.g. `stranger_46` → `stranger_54`) collapse to `"unknown"` via `FaceRecognizer.current_user()`, so swapping strangers alone doesn't break dedup.
- After 5 min on the same state, the next event passes through even if nothing changed — this keeps the Lamp agent "woken up" periodically so the wellbeing threshold check still runs.

*Presence dedup (at-log safety net).* `system/skillcontext/wellbeing/wellbeing.go::LogForUser` scans the user's JSONL bottom-up for the most recent **presence** row (enter/leave, ignoring activity rows in between). `enter` while the last presence is already `enter` is dropped; `leave` with no matching open session is dropped. Since HAL already emits one enter per real session (per-friend + collapsed-unknown), this runs as a safety net for restarts or out-of-order edge cases rather than load-bearing dedup.

**Retention:** 30 days on the Lamp side. A goroutine started by `wellbeing.Init()` sweeps files older than the cutoff daily.

### On `motion.activity` — what the agent does

By the time the agent sees the event, HAL has already logged the activity rows for it (see "Written by" in the table above). The agent's job is just: read the history, decide whether to nudge, and log the nudge if it fired.

1. **Read recent history** via `GET /api/agent/wellbeing-history?user={current_user}&last=50`.
2. **Compute deltas** from the log, using the most recent reset point for each:

   ```
   hydration_reset = max(last drink entry, last enter entry, last nudge_hydration entry)
   break_reset     = max(last break entry, last enter entry, last nudge_break entry)
   ```

   Three reset points: the actual activity (`drink` / `break`), a fresh arrival (`enter`), or the last nudge of that kind (`nudge_*`). The nudge reset is the key: after Lamp reminds, the delta drops back to 0 so the next reminder only fires after another full threshold window — no separate cooldown variable needed.
3. **Decide path** (one response max per turn, reaction outranks nudge — the user just acted, nudging on top would feel tone-deaf):
   - **Reaction** — labels list contains `drink` or `break` → speak one short acknowledgment sentence (surprised / playful, not advice). Uses `count_today` ("lần thứ N hôm nay"), `time_of_day`, and the gap delta to flavor the line. **No log entry** — the underlying `drink` / `break` row was already written by HAL upstream.
   - **Hydration nudge** — else if hydration delta ≥ hydration threshold → hydration nudge.
   - **Break nudge** — else if break delta ≥ break threshold → break nudge.
   - Else (sedentary under threshold, or no reset today yet) → `NO_REPLY`.
4. **After speaking a nudge** (not a reaction), log a `nudge_hydration` or `nudge_break` entry — this is what resets the delta for the next window (and makes the nudge visible on the user's timeline).
5. **Never guess** time-since from memory — always compute from the log.

For automatic `[activity]` turns, the wellbeing skill forbids routing/skill-selection narration in every assistant text message, including before tools. Raw eat reactions (`eating *`, `dining`, `tasting food`) are limited to one sentence of at most 20 words in `current_language`, with no tools, log marker, extra nudge or habit bootstrap even when timers are due or `bootstrap_needed=true`. The complete carrots example is “Enjoy your carrots!”. This is prompt guidance, not deterministic TTS filtering.

The reaction path was added so positive actions don't fall into silence: drinking water that doesn't trigger a nudge used to produce `NO_REPLY`, which felt dead. The reaction is fed by two extra pre-computed fields in `[wellbeing_context: ...]` — `count_today` (tally of `drink` / `break` rows today) and `time_of_day` (`morning` / `noon` / `afternoon` / `evening` / `night`) — so phrasing has something specific to lean on without spawning extra tool calls. Visual captions (e.g. "blue water bottle") are intentionally NOT in scope yet — the vision pipeline returns class labels only.

### Thresholds

Hardcoded in `lamp/resources/openclaw-skills/wellbeing/SKILL.md`:

| Threshold | Test value | Production value |
|---|---|---|
| `HYDRATION_THRESHOLD_MIN` | **5** | 45 |
| `BREAK_THRESHOLD_MIN` | **7** | 30 |

> ⚠ **Release checklist:** before shipping, change both constants to the production values (45 / 30). Test values let us iterate within minutes instead of hours — hydration and break are intentionally offset (5 vs 7) so you can tell which path fired during testing.

**How re-nudge spam is prevented.** The `nudge_hydration` / `nudge_break` log entry the agent writes after speaking is also counted as a reset point for its threshold. After Lamp reminds, the delta drops back to 0 and the next reminder of that kind only fires after another full threshold window (45 min for hydration, 30 min for break in production).

```
10:45  hydration overdue → nudge 💧 + log nudge_hydration → hydration delta = 0
10:50  wake-up → delta = 5 min < 45 → SKIP
11:20  wake-up → delta = 35 min < 45 → SKIP
11:30  wake-up → delta = 45 min ≥ 45 → nudge 💧 again (user still hasn't drunk)
```

If the user drinks or takes a break before the next window, the regular `drink` / `break` entry resets the delta and nothing needs nudging.

### User attribution — `[context: current_user=X]`

The sensing handler injects a `[context: current_user=X]` tag into every `presence.enter`, `motion.activity`, `emotion.detected` and `speech_emotion.detected` message. `X` is the **friend with the newest session_start** among friends still in the forget window (see `FaceRecognizer.current_user()`), or `"unknown"` when face sees **only** strangers (no friend is still present). Crucially: if a friend is still within their forget window, `current_user()` returns that friend even if the most recent raw `presence.enter` was for a stranger — stranger flicker does not kick a friend out of the session.

Sorting by `session_start` (the timestamp of the re-enter after the last leave) rather than `last_seen` makes the answer deterministic when two friends are continuously present (Chloe 18:00, An 18:30 → An wins because her session started later), instead of depending on dict iteration order.

**Source of truth lives in HAL.** `sensing_service._send_event` attaches `face_recognizer.current_user()` to every outbound payload as the `current_user` field. Lamp's sensing handler reads `req.CurrentUser` directly instead of parsing it back out of the message text — this closed a class of bugs where a stranger-only `presence.enter` fired while a friend was still present would downgrade Lamp's `mood.CurrentUser()` to `"unknown"`.

External callers (web UI, skills) can query the same value via `GET http://127.0.0.1:5001/face/current-user` → `{"current_user": "<name>"}`. This is a dedicated endpoint; do NOT parse it out of `/face/cooldowns` (that endpoint is the friend/stranger cooldown debug view only).

The Wellbeing, Mood, and Music skills are all required to use this exact value for the `user` field in their API calls — never inferring from memory, KNOWLEDGE.md, chat history, or `senderLabel`.

Alongside `[context: current_user=X]`, the handler also injects `[user_info: {"name","is_friend","telegram_id","telegram_username"}]` (built by `system/skillcontext/BuildUserContext`, fetched from lelamp `/user/info`). Skills must read `telegram_id` from this block — never `curl /user/info`. Block is omitted on hard fetch failure or when `current_user` is `unknown`; SKILL.md fallback path stays.

### Presence markers written by HAL

HAL's `FaceRecognizer._post_wellbeing` writes `enter` / `leave` rows directly to Lamp's `POST /api/wellbeing/log` — the agent is not involved, and Lamp's sensing handler no longer writes them either.

- **Per-friend:** each friend gets their own timeline. Fresh friend detection (after gap > `FACE_OWNER_FORGET_S`) → `{"action": "enter", "user": "<name>"}`. Friend forgotten in `_check_leaves` → `{"action": "leave", "user": "<name>"}`. Chloe entering while Leo is still present produces `chloe: enter` only — does not touch Leo's timeline.
- **Strangers (collapsed to `"unknown"`):** gated by a `_any_stranger_logged` flag. First stranger of a session → `unknown: enter`. Flag stays true while any stranger is still within the forget window, so stranger_37 → stranger_38 → stranger_52 churn does not produce extra rows. When `_check_leaves` drops the last stranger → `unknown: leave`.

Result: every enter has a matching leave on the same timeline, and attribution in each timeline reflects only events that belong to that user.

**Two flows, two different rules.** It is important to see that presence rows and activity rows are attributed on different principles:

- **Enter/leave rows** are per-presence: each friend gets their own timeline, strangers collapse to one `unknown` timeline, and `current_user()` is NOT consulted — a new friend entering does not "kick" another friend's session, and a stranger appearing alongside a friend does not downgrade the friend.
- **Activity rows** (drink, break, sedentary labels) use `current_user()` with friend priority — when Chloe and a stranger are both visible, activities go only to Chloe's timeline, because she is the effective user.

Worked example — Chloe and Stranger_X overlap:

| Time | Event | Chloe timeline | Unknown timeline |
|---|---|---|---|
| 18:00 | Chloe fresh detected | `chloe: enter` | — |
| 18:15 | Stranger_X fresh detected | — | `unknown: enter` |
| 18:20 | `motion.activity` (using computer) — `current_user()=chloe` | `chloe: using computer` | — |
| 18:45 | Last stranger forgotten | — | `unknown: leave` |
| 19:00 | `motion.activity` (writing) — `current_user()=chloe` | `chloe: writing` | — |
| 20:00 | Chloe forgotten | `chloe: leave` | — |

Chloe's timeline is a complete session with activities; the unknown timeline records that a stranger was present in the room during 18:15–18:45 but has no activity rows, because Chloe was the effective user throughout. The two flows don't conflict — they answer different questions.

### Priority: Skills > Knowledge > History

AGENTS.md enforces a strict priority: **SKILL.md instructions always override KNOWLEDGE.md and conversation history**. This is critical because the agent self-accumulates "learnings" in KNOWLEDGE.md via heartbeat, and these can contain incorrect rules that conflict with developer-maintained skills. If the agent notices a conflict, it must update KNOWLEDGE.md to match the skill, not the other way around.

### On `presence.leave` / `presence.away`

Backend writes the `leave` marker to the log. Nothing else to do — there are no crons to cancel. The directive instructs the agent to stay quiet (`NO_REPLY`).

### Agent behavior

| Reminder | Emotion | Voice |
|---|---|---|
| Hydration cron | `caring` (0.5) | YES (remind water) or silent |
| Break cron | `caring` (0.6) | YES (remind stretch/walk) or silent |

The agent uses the camera snapshot to make a judgment call — it does NOT always speak. This prevents spamming the user when they seem fine.

### Music Suggestions (AI-Driven)

Music suggestions are **fully AI-driven** — no cron jobs, no backend triggers. The agent decides when to suggest based on two triggers:

- **Mood trigger:** After logging a suggestion-worthy mood (`sad`, `stressed`, `tired`, `excited`, `happy`, `bored`), the agent follows the Music skill to suggest music matching that mood — this includes the music branch of the emotion router (see "Agent behavior" below), as well as moods logged from conversation, wellbeing, or explicit asks.
- **Sedentary trigger:** When `motion.activity` carries a sedentary raw label (`using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller`), the agent suggests background music (lo-fi, ambient, instrumental).
- **Data-driven decisions:** Before suggesting, the agent queries:
  - `GET /audio/status` — is music already playing?
  - `GET /api/agent/music-suggestion-history` — the last entry is the reset point; fire only when `minutes_since_last_suggestion >= SUGGESTION_INTERVAL_MIN` (7 min test / 30 min prod)
  - `GET /audio/history?person={name}` — per-user listening history (genre preference, duration, satisfaction)
- **Learning loop:** Accepted suggestions reinforce genre/timing; rejected suggestions trigger approach adjustments. All logged via `/api/music-suggestion/log`.

See the Music skill (`resources/openclaw-skills/music/SKILL.md`) for full implementation details.

### Proactive care (piggyback on sensing events)

Beyond scheduled reminders, the agent is encouraged to **notice things** when receiving any event where the user is visible (presence.enter, motion.activity). Based on time of day, how long the user has been sitting, and what it sees, the agent may proactively mention meals, fatigue, or late nights — one short sentence, only when it feels natural. This is not mandatory but encouraged.

Examples: "Morning! Had breakfast?" on early `presence.enter`, "It's past noon — grab some lunch?" on `motion.activity` at 12:20, "It's almost 11 PM..." on late-night `motion.activity`.

### Speak and broadcast markers

Two control markers on channel-origin turns:

| Marker | Effect | When to use |
|---|---|---|
| `[HW:/speak:{}]` | Forces TTS on the speaker. No Telegram side-effect. | Proactive crons (wellbeing, music) running inside a Telegram/channel session so the reminder is also heard aloud. Usually combined with `[HW:/dm:{"telegram_id":"..."}]` for a targeted DM. |
| `[HW:/broadcast:{}]` | Forces TTS **and** fans out the reply text to every connected Telegram chat. | Guard mode alerts only. Never use in wellbeing/music — it will notify every chat, not just the person being reminded. |

By default, channel-origin turns (Telegram, webchat) suppress speaker TTS because the reply is routed as a channel message. `/speak` overrides that suppression without the fan-out side-effect.

**Cron-fire turns auto-force TTS.** When OpenClaw emits an `event:"cron"` with `action:"started"`, Lamp caches the `sessionKey` and the next `lifecycle_start` on that session within 10 s is marked as a cron fire — `isChannelRun` is overridden to `false` so the lamp speaker fires without requiring `[HW:/speak]` in the reply. The marker is still useful as a defense-in-depth fallback if the cron event is dropped (`dropIfSlow: true` on the OpenClaw side).

### Per-user mood history

Mood history tracks the **user's emotional state** only — not system events or lamp emotions. Stored per-user at `/root/local/users/{name}/mood/YYYY-MM-DD.jsonl` (30-day retention). Mood is logged by the agent via the Mood skill when it detects emotional actions (camera) or infers mood from conversation.

#### Mood sources

| Source | How it works |
|---|---|
| **Camera** (`source: "camera"`) | `motion.activity` detects emotional action (laughing, crying, singing) → Emotion Detection skill triggers → agent logs mood |
| **Conversation** (`source: "conversation"`) | Agent detects mood two ways: (1) **single message** — explicit ("I'm tired") or implied ("work is killing me" → stressed); (2) **conversation flow** — after chatting for a while, read the overall vibe (tone shifts, short/curt replies, repeated topics, rising/fading energy). Agent trusts its gut and infers boldly: a small hint is enough, better to log a maybe-mood than miss a real one. Works across all channels (Telegram, voice, web). |

#### Voice mood nudge

Voice events (`voice_command`, `voice`) include a `[MANDATORY: Follow Mood skill — log mood now.]` nudge in the message sent to the agent, plus `[Current user: {name}]` when face recognition knows who is present.

#### Storage format

JSONL (one JSON object per line) — chosen over JSON array for:
- **Append**: O(1) — just write a new line (no read-parse-rewrite)
- **Crash-safe**: worst case loses 1 line (array can corrupt entire file)
- **Read last N**: `Query()` reads all lines then slices — fast enough for daily files (tens of entries)

Each row carries a `kind` field — either a raw `signal` from one source or a
`decision` synthesized by the agent from the recent signals + previous decision.
The store never fuses anything; the Mood skill is responsible for writing both
rows on every detection.

```bash
# Write — raw signal (agent calls this on every camera/voice/telegram cue)
POST /api/mood/log  {"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}

# Write — synthesized decision (agent calls this right after, after reading recent history)
POST /api/mood/log  {"kind":"decision","mood":"happy","based_on":"3 signals last 20min","reasoning":"laughing reinforces previous happy decision"}

# Read — all kinds for a day (agent uses this to re-analyze)
GET /api/agent/mood-history?user=gray&date=2026-04-09&last=100

# Read — latest decision only (downstream skills use this for "current mood")
GET /api/agent/mood-history?user=gray&kind=decision&last=1
```

Each row: `{"ts":...,"seq":1,"hour":10,"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}` for signals,
or `{"ts":...,"seq":2,"hour":10,"kind":"decision","mood":"happy","source":"agent","based_on":"...","reasoning":"..."}` for decisions.

### Cross-channel identity

The agent links face recognition names to Telegram usernames by observing timing and context (e.g., "gray" is at the desk and "@GrayDev" messages on Telegram simultaneously). Confirmed mappings are stored in `USER.md` (for the enrolled person) or the user's folder notes. The agent asks for confirmation if unsure.

---

## Motion Activity Analysis (while present)

When the user is already present (PRESENT state), foreground motion triggers a `motion.activity` event instead of `motion`. Same cooldown (`MOTION_EVENT_COOLDOWN_S`, 3 min) — no separate timer. The system sends the detected action name(s) (no images — action names are sufficient for the agent to infer behavior).

### How it works

`MotionPerception` buffers snapshots and action names, flushing them periodically (`MOTION_FLUSH_S`). On flush it checks `PresenceService.state`:
- **PRESENT** → sends a single `motion.activity` event. Message format:
  - `Activity detected: <labels>.` — HAL already categorises: physical actions collapse to the bucket name (`drink`, `break`, `celebrate`), sedentary activities keep the raw Kinetics label (`using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller`). One exception to "keep the raw label": `reading book` + `reading newspaper` both collapse to the single generic `reading` label. The agent logs each label verbatim — no mapping required at the agent level.
  - Emotional X3D actions (`laughing`, `crying`, `singing`) are **intentionally dropped** here. A dedicated `motion.emotional` event type will be added later; until then emotional detections are silently ignored. `motion.activity` stays purely physical.
  - `yawning` is **not** emotional — it lives in its own `tired` bucket and IS emitted, as a raw label, for the wellbeing skill. The agent names the yawn out loud like a drink or a meal (throttled to once an hour by a `noted_yawn` marker, and only before 21h), shortens the break threshold to 20 min, and treats it as confirmation for sleep wind-down.
  - No images attached — saves tokens. Friend recognition is **not** required.
- **Otherwise** → event is **skipped** (logged, not sent). Lamp only expects `motion.activity` — plain `motion` from X3D/pose has no handler and wastes agent tokens.

Example messages:
```
Activity detected: drink, using computer.
Activity detected: break.
Activity detected: writing, reading.
```

### Wellbeing nudge flow (event-driven)

The agent reads the `Activity detected:` line, splits on comma, and POSTs each label verbatim as the `action` field — HAL already categorised, so there is no bucket mapping in the agent.

1. **Log** each label via `POST /api/wellbeing/log` with `{action, notes:"", user}` — one entry per label. Backend-side no-op; HAL already deduped on the outbound label set.
2. **Read history** via `GET /api/agent/wellbeing-history?user={name}&last=50`.
3. **Compute deltas** against the latest reset point for each kind (see Wellbeing SKILL Step 3).
4. **Decide nudge** per Wellbeing SKILL Step 4 — at most one hydration or break nudge per turn.
5. **Respond**: a single short caring sentence if there's a nudge / suggestion, otherwise `NO_REPLY`.

### Agent behavior

| Event | Emotion | Voice |
|---|---|---|
| `motion.activity` | `curious` (0.4) | YES (caring observation with context) or NO_REPLY (sedentary) |

### Per-Face Motion Activity (MotionPerFacePerception)

An optional alternative to `MotionPerception` that runs action recognition **per detected face** rather than on the full frame. Enabled via `HAL_MOTION_PER_FACE_ENABLED=true` (default `false`).

#### How it works

1. Subscribes to `detected_faces` updates (same as emotion perception).
2. For each face, **expands the bounding box** (1x face height up, 2x face width left/right/down) to capture upper body + hands — the region where desk activities happen.
3. Crops the expanded region from the frame.
4. Sends the crop to a **dedicated WS session** on the action recognition backend. Each `face_id` (e.g. `gray`, `stranger_5`) gets its own session with independent frame buffer.
5. Person detection is always **disabled** on these sessions — the face bbox expansion already isolates the person.

#### Per-action dedup

Each face session maintains independent per-action dedup. Within the dedup window (default 5 min), a repeated action label for the same face is suppressed — but other actions still fire.

Example:
```
t=0:00  leo → {drinking, using computer}     → SEND both
t=1:00  leo → {drinking, using computer}     → DROP both (within 5 min)
t=2:00  leo → {drinking, writing}            → SEND writing only (drinking still in window)
t=5:01  leo → {drinking}                     → SEND drinking (expired from window)
```

The example shows the per-face layer in isolation — the global cooldown floor below applies on top of it.

#### Global cooldown floor (shared across faces)

One cooldown floor is shared across ALL face sessions, with the same semantics and the same knobs as `MotionPerception`: within the same coarse activity class, at most one `motion.activity` per `MOTION_EVENT_COOLDOWN_S` (default 15 min); a coarse-class **transition** bypasses the floor once `MOTION_TRANSITION_MIN_GAP_S` (default 60s) has passed since the last emission; the floor is cleared on a real user change (`reset_dedup`), not on stranger flicker.

Without this floor, the per-face dedup alone would allow one event per person per flush — N people in frame = N events, and every newly detected face would fire immediately. With it, multi-person scenes still produce at most one same-class event per cooldown, no matter how many faces are tracked.

#### Minimum frames gate

A new face session must receive at least `MOTION_PER_FACE_MIN_FRAMES` frames (default 4) before its first event fires. This prevents noisy single-frame classifications from brief face detections.

#### Session lifecycle

- **Created** on first sight of a `face_id`.
- **Evicted** after `MOTION_PER_FACE_SESSION_TTL_S` (default 30s) without seeing that face.
- Eviction closes the WS connection to the backend and discards all buffered state.

#### Configuration

| Config | Env var | Default | Purpose |
|---|---|---|---|
| `MOTION_PER_FACE_ENABLED` | `HAL_MOTION_PER_FACE_ENABLED` | `false` | Enable per-face action recognition |
| `MOTION_PER_FACE_DEDUP_WINDOW_S` | `HAL_MOTION_PER_FACE_DEDUP_WINDOW_S` | `300` (5 min) | Per-action dedup window per face |
| `MOTION_PER_FACE_SESSION_TTL_S` | `HAL_MOTION_PER_FACE_SESSION_TTL_S` | `30` | Evict WS session after this long without seeing the face |
| `MOTION_PER_FACE_MIN_FRAMES` | `HAL_MOTION_PER_FACE_MIN_FRAMES` | `4` | Min frames before first event fires |

#### Message format

```
Activity detected (gray): using computer, writing.
Activity detected (stranger_5): drinking.
```

Includes the `face_id` in parentheses so the agent knows which person the activity belongs to. Uses the same `motion.activity` event type as `MotionPerception`.

#### When to use

- **Multi-person scenes** — each person gets independent action classification.
- **Camera ego-motion** — the face-anchored crop is more stable than the full frame when the servo is moving.
- **Trade-off**: opens one WS connection per tracked face. For single-person desk use, standard `MotionPerception` is simpler.

---

## Emotion Detection — User Emotion (UC-M1) ✅

Lamp detects the **user's** emotional state via three channels:

1. **Facial expression** (primary) — `emotion.detected` event from `hal/drivers/sensing/perceptions/processors/emotion.py`. Each detected face is cropped and POSTed to perception-service `/emotion-recognize` (HTTP, one call per face per sensing tick). Detects 7 emotions: Neutral, Happy, Sad, Surprise, Fear, Disgust, Anger. A reading passes three gates before it becomes an event:
   1. **Per-label gate (HAL side)** — HAL posts the crop with `raw: true` and gates the returned probabilities in `emotion_gating.py`: the argmax must clear its own bar (`anger 0.8`, `sad 0.8`, `happy 0.5`, `surprise 0.6`, `disgust 0.7`, `fear 0.5`; override per device with `HAL_EMOTION_LABEL_THRESHOLDS`, which replaces the whole map) or Neutral stands in carrying Neutral's own probability, and `EMOTION_CONFIDENCE_THRESHOLD` is applied to the result. A reading that fails is **no reading**, not Neutral. Against an older perception server that ignores `raw`, the server gates with its own `label_gating.py` map and HAL takes its label as-is. `result.json` records which side gated (`gate: hal|server`) and, for HAL, every class probability.
   2. **Occupancy (HAL side)** — on each `EMOTION_FLUSH_S` flush, `Happy` fires on a single frame, but every other label must hold a **strict majority of that person's recognition attempts in the trailing 10s**, counting attempts that returned nothing (an exact tie does not qualify), **and be at least 2 readings**. Attempts are counted per face detection, not per sensing tick, so without that floor a face seen once in the window makes a single reading a trivial 1-of-1 majority. A face turned toward a monitor reads as Anger, so a lone Anger frame among a dozen empty responses used to win the vote outright.
   3. **Bucket dedup** — `(user, positive|negative)` with a `EMOTION_DEDUP_WINDOW_S` TTL (300s), so each polarity reports at most once per 5 minutes.
2. **Speech emotion** (secondary) — `speech_emotion.detected` event from `hal/drivers/voice/speech_emotion/`. Runs at the end of every speaker-identified STT session against the same WAV used for speaker recognition. Uses `emotion2vec_plus_large` on perception-service via HTTP. See [Speech Emotion Recognition](../../../docs/speech-emotion.md) for the full pipeline.
3. **Body action** (tertiary) — emotional X3D actions from action recognition are **intentionally dropped** from `motion.activity` (which is purely physical: sedentary/drink/break/celebrate). A dedicated `motion.emotional` event type is planned for these.

> **Not to be confused with Emotion Expression** (`emotion/SKILL.md`) — which controls Lamp's own emotional output (servo + LED + eyes). Emotion Detection is about sensing what the *user* feels; Emotion Expression is how *Lamp* shows its feelings.

### `emotion.detected` event

Fired by HAL when the perception-service emotion classifier detects a facial expression above the confidence threshold. Message format:

```
Emotion detected: <Label>. (weak camera cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <bucket-tuned hedge>.)
```

Concrete examples:

```
Emotion detected: Fear. (weak camera cue; confidence=0.62; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Emotion detected: Happy. (weak camera cue; confidence=0.78; bucket=positive; treat as uncertain, do not over-celebrate.)
```

The raw `Emotion detected: <Label>.` prefix is preserved so `user-emotion-detection/SKILL.md`'s parser and the Fear→stressed / Sad→sad mood mapping keep working unchanged. The trailing parenthetical exists to stop the LLM from over-committing on noisy FER reads (the bug it fixed: Fear → "Oh hello there again" greeting). Hedge clauses by bucket: `negative` → "do not assume the user is distressed"; `positive` → "do not over-celebrate"; `other` → "do not over-react".

**Polarity-bucket dedup** (`EMOTION_BUCKETS` in `hal/drivers/sensing/perceptions/processors/emotion.py`) collapses fine-grained labels into `positive` / `negative` / `other` and dedups by `(current_user, bucket)` over a 5-min window. Within-bucket noise (Fear↔Sad↔Anger) becomes one event per window; cross-bucket flips (Fear→Happy) still fire as a genuine mood change. Confidence in the message is averaged over instances of the dominant label only — other labels' scores don't dilute it. The dedup map is persisted to a boot-scoped sidecar (`/tmp/hal-emotion-state.json`) so a HAL service restart doesn't re-fire the last-known emotion on the first flush; a full device reboot starts fresh.

The sensing handler (`handler.go`) routes `emotion.detected` events to the agent. When the agent is busy, these events are queued and replayed when idle.

### Agent behavior

The `user-emotion-detection/SKILL.md` handles `emotion.detected` events:

1. Maps facial emotion → mood signal (e.g. Happy → happy, Sad → sad, Angry → frustrated, Fear → stressed) and logs a `signal` row via `POST /api/mood/log`
2. Picks one response route from a 3-row table (first match wins):
   - **#1 `audio_playing == true`** → LED-only ack + `NO_REPLY` (don't talk over music)
   - **#2 `suggestion_worthy == true` AND decision fresh AND `last_suggestion_age_min ∉ [0, 7)`** → **music** — gentle one-liner with genre pick via `music-suggestion/SKILL.md`
   - **#3 anything else** → **checkin** — a short human reaction. See `user-emotion-detection/reference/checkin.md` for per-emotion examples (templates are keyed by raw FER label — Sad/Fear/Angry/Disgust/Happy/Surprise — with three style options: Ask, Comfort, Invite). Examples are inspiration only; agent improvises per turn.
3. **Cooldown only gates music, never checkin.** When the 7-min cooldown is active, row #2 fails its third clause and the event falls through to checkin (row #3). The agent still asks "what's up?" — it just doesn't suggest music two times in a row. `NO_REPLY` only fires on row #1 (active audio playback).
4. **Never greet on an emotion event.** `emotion.detected` is not a presence/arrival event — `sensing/SKILL.md` forbids openers like `hello`, `welcome back`, anything containing `again`. Greetings belong only to `presence.enter`.

Emotion skill output forbids assistant-text preambles before or between tool/skill calls, then requires the mood signal and selected-route markers followed by one sentence of at most 20 words or the route’s `NO_REPLY`. After reading the selected reference, the model should finish without rereading for phrasing. Explicit weak camera/voice cues must not be spoken as established feelings or expressions: a weak Happy checkin uses a neutral invitation, not an assertion that the user is happy or smiling. Logging and routing remain unchanged. These are prompt instructions, not a guarantee that the model will comply. With provider thinking enabled, route/cooldown/style/logging analysis must stay in the native reasoning channel, without a summary in ordinary text before or after tools or in the final response. Textual `<think>` tags are not a substitute; without a native channel, omit analysis. The checkin reference includes a complete weak Sad / unknown-user / two-minute music-cooldown example with required markers and only “Anything on your mind?” spoken, without assuming distress.

Both routes share one cooldown: music logs via `POST /api/music-suggestion/log` with `trigger:"<genre>:<mood>"` (mood bucket); checkin logs the same endpoint with `trigger:"checkin:<emotion>"` (raw FER label). `last_suggestion_age_min` reflects either channel, so a fresh music suggestion silences the music branch for 7 min but doesn't silence checkin. Checkin phrasing is keyed by raw emotion (not mood) so each FER label has its own ask/comfort/invite style options — see `reference/checkin.md`. Always prefix `[HW:/emotion:{"emotion":"caring","intensity":0.5}]` on checkin output.

### Mood pipeline

- **Mood history** (agent logs): On every signal the Mood skill writes a raw `signal` row, then reads recent history and writes a synthesized `decision` row (e.g. `{"kind":"decision","mood":"happy","based_on":"...","reasoning":"..."}`).
- Mood decisions trigger downstream skills: `music-suggestion` (proactive music), `wellbeing` (break/hydration nudges).

See `user-emotion-detection/SKILL.md` for the agent's full response rules.

### `speech_emotion.detected` event

Fired by HAL at the end of every speaker-identified STT session, after the same WAV bytes used for speaker `/embed` are forwarded to `perception-service /api/dl/ser/recognize` (emotion2vec_plus_large). Buffering, per-user aggregation, polarity-bucket dedup, and the Lamp POST are all handled inside `hal/drivers/voice/speech_emotion/SpeechEmotionService` — `voice_service.py` only calls `submit(user, wav, duration)`. Message format mirrors the facial pipeline:

```
Speech emotion detected: <Label>. (weak voice cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <bucket-tuned hedge>.)
```

Concrete examples:

```
Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Speech emotion detected: Happy. (weak voice cue; confidence=0.84; bucket=positive; treat as uncertain, do not over-celebrate.)
```

Labels (from emotion2vec_plus_large): `angry`, `disgusted`, `fearful`, `happy`, `neutral`, `other`, `sad`, `surprised`, `<unk>`. Neutral / other / `<unk>` are dropped before bucketing — same rule as facial Neutral.

**Anti-spam guards** (mirror the facial pipeline 1-to-1):

1. Short audio (`duration_s < SPEECH_EMOTION_MIN_AUDIO_S`) dropped at `submit()`.
2. Unknown speaker (`match=false` or `name=="unknown"`) dropped — no subject to attribute emotion to.
3. Low-confidence inferences (`< SPEECH_EMOTION_CONFIDENCE_THRESHOLD`) dropped by the worker.
4. Neutral labels dropped at flush time.
5. `(user, bucket)` TTL dedup over `SPEECH_EMOTION_DEDUP_WINDOW_S` (default 5 min). Each bucket keeps an independent timer — sending a positive event does not reset the negative window. The map is persisted to a boot-scoped sidecar (`/tmp/hal-ser-state.json`) so a HAL service restart doesn't re-fire the last-known speech emotion; a full device reboot starts fresh.

The event payload carries `current_user` explicitly so the Lamp sensing handler doesn't need to look it up.

### Agent behavior (shared with face emotion)

`speech_emotion.detected` routes through the **same** skill as `emotion.detected` — `user-emotion-detection/SKILL.md`. The sensing handler tags the incoming message with `[speech_emotion]` (vs `[emotion]` for face), pre-fetches the same `[emotion_context: ...]` block, and forwards to the agent. The skill:

1. Parses the prefix to decide `source` on the mood signal log (`"voice"` for `[speech_emotion]`, `"camera"` for `[emotion]`).
2. Maps the label via the shared table: `Happy → happy`, `Sad → sad`, `Angry → frustrated`, `Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`, `Neutral → normal`. Voice variants (`Fearful`, `Surprised`, `Disgusted`) bucket identically to their face counterparts.
3. Runs the same `music / checkin / action / silent` router on the same `[emotion_context: ...]` fields (`audio_playing`, `suggestion_worthy`, `last_suggestion_age_min`, `is_decision_stale`).
4. Shares one music-suggestion cooldown across both modalities — voice cannot bypass a cooldown that a recent camera signal set, and vice versa.

The hedge `(weak voice cue; ...)` in the message is the model's signal to prefer Comfort/Invite checkin phrasing over Ask on voice-only negative reads, since short-utterance emotion2vec is noisier than face FER. See `user-emotion-detection/SKILL.md` for the full rules.

See [Speech Emotion Recognition](../../../docs/speech-emotion.md) for the full architecture, threading model, configuration table, and failure modes.

---

## Snapshot Storage (two-tier)

Sensing events that include a camera frame (`motion`, `presence.enter`, `presence.leave`, `motion.activity`, `emotion.detected`, `music.mood`) save snapshots in two locations.

| Tier | Path | Rotation | Survives reboot |
|------|------|----------|-----------------|
| **Tmp buffer** | `/tmp/hal-sensing-snapshots/sensing_<prefix>/` | Count-based (max 50 files) | No |
| **Persistent** | `/var/lib/hal/snapshots/sensing_<prefix>/` | TTL (72h) + size (50 MB max) | Yes |

Each event kind writes to its own subdir (`sensing_<prefix>`, e.g. `sensing_presence/`, `sensing_motion_activity/`, `sensing_emotion/`). Filenames are `<ms>.jpg`. Every snapshot is saved to tmp first, then copied to the persistent dir. The persistent path is included in the event message (`[snapshot: /var/lib/hal/snapshots/sensing_<prefix>/<ms>.jpg]`) so the agent can reference it later — even after a device reboot. Monitor serves them via `GET /api/sensing/snapshot/<category>/<name>`.

Configuration constants are in `hal/config.py`:
- `SNAPSHOT_TMP_MAX_COUNT` — max files in tmp (default 50)
- `SNAPSHOT_PERSIST_TTL_S` — persistent file TTL in seconds (default 72h)
- `SNAPSHOT_PERSIST_MAX_BYTES` — max total size of persistent dir (default 50 MB)

---

## General Rules (all event types)

- **Pending event replay**: When the agent is busy, `presence.enter`, `presence.leave`, and `voice` events are queued and replayed when the agent becomes idle. The replay path (`drainPendingEvents` in `service.go`) applies the same nudge messages as the live handler (cron setup for presence.enter, cleanup for presence.leave, etc.).
- **Replay waits for the speaker, not just for the agent**: a runtime goes idle when its reply text is handed to the TTS queue, not when the speaker has finished with it — a long answer keeps playing for tens of seconds afterwards. Draining on that edge opened a new turn mid-utterance, and because HAL gives the speaker to the newest turn (`turn_seq`, see `motion`/TTS queue), the answer the user had asked for was cut off (device-observed, 28/8: a hunting-season reply was truncated by a queued `presence.enter` greeting). `drainPendingEvents` now asks `lib/speakergate` first: while `GET /voice/status` reports `tts_speaking`, the batch is put back and re-drained within 500 ms of the speaker going idle. Two kinds of event never wait — speech and chat (`voice`, `voice_command`, `voice_followup`, `voice_agent_handled`, `web_chat`, `mqtt_chat`), because a person talking over the device is exactly the barge-in the preemption rule exists for, and `fire_hazard.detected`, because a safety alert must not queue behind an answer. One exempt event releases its whole batch so the queue is not reordered behind it. A stuck `speaking` flag delays the replay but never cancels it: after 90 s the batch replays anyway, and a HAL that does not answer counts as idle.
- **The same rule applies with no turn in flight**: a runtime reports idle as soon as its reply text is queued for TTS, so an event can arrive while the agent is free but the speaker is still busy — and that window is the whole length of the reply, which is exactly when someone is most likely to be standing in front of the device. `PostEvent` therefore treats "the speaker is busy" as busy for the same passive types (`handler.go`), queues the event, and asks `speakergate` to replay it once the reply finishes. Without that last part it would never replay: the drain normally rides on a turn ending, and here no turn is running. `DrainPendingEvents()` is on `domain.AgentGateway` for this reason.
- **Passive sensing events** (`[sensing:*]`) are dropped if the agent is already busy with another turn (except presence and voice events which are queued).
- **Voice events** always pass through — the user is explicitly speaking. Voice messages include a mood scan nudge (`[MANDATORY: Follow Mood skill — log mood now.]`) so the agent remembers to detect mood from the conversation flow. Passing the event through does not require a reply: `SOUL.md` returns `NO_REPLY` for a filler, lone/repeated word, clipped fragment, garbled transcript, or other voice input without a clear meaning. It must not ask a clarification question, react, or log mood for such a turn.
- The `[sensing:type]` prefix in the message is how the agent knows it's an ambient event, not a user message.
- **Pre-turn `thinking` emotion**: The `emotion-acknowledge` hook fires `POST /emotion {thinking, 0.7}` server-side at `message:preprocessed` for every non-sensing message — the agent does not need to call it. Sensing events are skipped by the hook because each type has its own defined first emotion.
- **Image pruning echo**: OpenClaw strips old image payloads from conversation history to save tokens. Smaller models (Haiku) may echo the pruning markers as `[image description removed]` in their response text. `SOUL.md` instructs the agent to never echo these markers.

---

## Ambient Idle Behaviors

When nothing is happening, `system/ambient/` (Go, part of os-server) makes the device feel alive: breathing LED, occasional micro-movements, and quiet self-talk. All hardware control goes through the HAL HTTP API (:5001). This is separate from sensing events — no agent turns are created and no tokens are spent.

### Pause / resume lifecycle

- The service starts **paused** and resumes 5 s after os-server boot.
- Any real interaction pauses it immediately. Pause triggers (monitor bus events): `sensing_input`, `chat_response`, `intent_match`, `tts`, `chat_send`, `hw_emotion`.
- It resumes automatically after **60 s** with no interaction (`resumeDelay`, checked every 2 s).
- **Sleep mode**: when a `hw_emotion` event carries `sleepy`, all ambient behaviors are suppressed until the next real interaction (chat, wake word, sensing) wakes the device.
- Pause/resume transitions are logged to the Flow Monitor as `ambient_pause` / `ambient_resume`.

**Two timers, two layers** — the 60 s resume delay is *eligibility* ("ambient may act again"), while each loop below keeps its own *cadence* clock that ticks independently and is never reset by interactions; when a loop's clock fires while ambient is paused, that round is skipped, not queued. Example timeline: the user stops talking → 60 s later ambient resumes, so the LED starts breathing almost immediately (2 s tick) — but the next self-talk line lands whenever the mumble loop's 5–15 min clock happens to fire next, which may be right away or many minutes later.

### Behavior loops (capability-gated)

The whole suite is opt-in per device: the routeless `lifelike` capability (declared in the `capabilities:` block of `robots/<type>/ROBOT.md`, see `robots/contract/capabilities.md`) is the master switch — a device that declares capabilities without `lifelike` runs no idle behaviors at all (e.g. intern-v2 stays a quiet tool). Each loop additionally drives one peripheral and only runs if the device declares the matching hardware capability. A device with no declared capabilities at all runs everything (fail-open, legacy Lamp behavior).

| Loop | Capability | Cadence | What it does |
|------|-----------|---------|--------------|
| Breathing LED | `light` | continuous (2 s tick) | Starts HAL's built-in `/led/effect` `breathing` (speed 0.3) using the current LED color read from HAL; falls back to the resting look (`ambientRestingColor`, currently `(0, 0, 0)`) when the LED is black — a dark resting look means the tick is skipped and the strip stays unlit. See [led-control.md § The resting look](led-control.md#the-resting-look-default-off). Stops the effect while paused or LED-locked. |
| Micro-movements | `motion` | random 45–120 s | Plays one safe servo recording from `idle`, `curious`, `nod`. Servo only — never touches the LED. |
| Mumble (self-talk) | `audio` | random 5–15 min | Picks from six shared `PhraseMumble` entries per language (`system/lib/i18n/phrases.go`, EN/VI/zh-CN/zh-TW): short vocalizations mixed with playful self-talk, such as “No need to rush.” and “I like little moments like this.” One entry uses `[chuckle]`; none uses sigh or whisper tags. Uses `hal.SpeakCached` — the first render of each phrase hits the TTS provider, replays come from HAL's WAV cache, so idle mumbling costs no API calls. |

The shared wording is independent of robot type or body. The idle pool has no
sensor or conversation context, so it contains no claims about lighting,
quietness, listening, forgotten thoughts or sleepiness. Such
sentences require a separate context-aware behavior; this loop does not infer
those conditions. Cadence, pause/wake rules and random selection are unchanged
(consecutive repeats remain possible). Audition the actual cached device voice
before judging naturalness; text and punctuation cannot guarantee its tone.

### LED lock

When a user or the agent explicitly sets an LED color/scene, breathing must not trample it:

- Monitor bus event `led_set` locks the LED; `led_off` unlocks it (breathing may resume on idle).
- Web-UI LED writes go through the `/api/hardware` proxy and never emit those events, so the proxy calls `LockLED()` / `UnlockLED()` on the ambient service directly.

### Not to be confused with dead-air fillers

The mumble loop speaks when the device is **idle**. A different mechanism — dead-air fillers (`system/lib/i18n/fillers.go`, driven by `server/sensing/delivery/http/deadair_filler.go`) — uses brief conversational asides such as "Hmm, let's see." **mid-turn** while the agent is busy. Tool-aware overrides remain separate for recognised tools. Raw tool names are normalised first (`bash` / `shell` → `exec`, `file_changes` → `apply_patch`, and recognised MCP suffixes), so the same pool works across runtimes; unknown names use the generic continuation pool. Different trigger, different phrase pools.
