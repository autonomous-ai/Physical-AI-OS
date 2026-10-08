---
name: servo-control
description: "Use to aim/point/look the device in a DIRECTION, toggle servo state (hold/resume/release), or play a named servo animation (nod/shake/etc). In a movement/hold context, \"back to normal\", \"turn to normal position\" and \"return to idle\" mean resume; \"look straight ahead\" only means aim center. ALSO the skill for SEARCHING — \"where are you\", \"find my keyboard/cup/phone\", \"look around for X\", \"scan the whole room\" — which sweeps the room with the camera via /servo/search (run it with curl DURING the turn and answer from its result), and for DEMONSTRATING the movement range ONLY when the request names MOVEMENT — \"show me how far you can move\", \"how far can you turn\", \"show me your range of motion\" — via /servo/demo. A bare \"show me what you can do\" or \"what is your maximum capability\" is a general question about abilities, NOT a movement request, and must not start the demo. Never answer a search or a range demo with an emotion. Directions are fixed named locations or axes — supported: desk, wall, left, right, up, down, center, user. Furniture and surfaces (\"desk\", \"table\", \"floor\", \"ceiling\", \"wall\", \"door\", \"workspace\") are ALWAYS directions, never tracking targets — map them to the closest of the supported names (table/workspace → desk). MUST use /servo/aim (not /servo/track) for: \"look at the desk\"→desk, \"point at my table\"→desk, \"look at the wall\"→wall, \"look left\"→left, \"point up\"→up, \"look at me\"→user. For following a movable OBJECT by vision (cup, phone, hand, person, pet) use servo-tracking instead. Compound: if user names a direction AND an object (\"look at desk and follow cup\"), fire THIS aim skill first, then tracking."
---

# Servo Control

## Quick Start
Controls the device's servo motors for directional aiming and physical animations. Use `/servo/aim` for directional pointing, `/servo/play` for expressive animations.

## Workflow
1. Determine whether the user wants to **aim**, **play an animation**, or **change motion state** (hold, resume, release). Apply the resume-vs-direction rules below before choosing a marker.
2. Prefix reply with the appropriate `[HW:...]` marker — the device fires it before TTS.
3. Confirm the action to the user.

**Important**: For conversation reactions, use the **Emotion** skill instead — it combines servo + LED + eyes automatically.

## Examples

**Input:** "Point the light at my desk"
**Output:** `[HW:/servo/aim:{"direction":"desk"}]` Done, aimed the light at your desk.

**Input:** "Look at the desk" / "Look at my table" / "Look at my workspace"
**Output:** `[HW:/servo/aim:{"direction":"desk"}]` Looking at the desk now.

**Input:** "Look at the wall"
**Output:** `[HW:/servo/aim:{"direction":"wall"}]` Looking at the wall.

**Input:** "Look at me"
**Output:** `[HW:/servo/aim:{"direction":"user"}]` Looking at you!

**Input:** "Look to the left"
**Output:** `[HW:/servo/aim:{"direction":"left"}]` Looking left now.

**Input:** "Aim at the wall slowly"
**Output:** `[HW:/servo/aim:{"direction":"wall","duration":3.0}]` Aiming at the wall slowly.

**Input:** "Nod for me"
**Output:** `[HW:/servo/play:{"recording":"nod"}]` Nodding!

**Input:** "Turn left 15 degrees"
**Output:** `[HW:/servo/nudge:{"yaw":-15}]` Turned a bit to the left.

**Input:** "Tilt up a little"
**Output:** `[HW:/servo/nudge:{"pitch":10}]` Tilted up slightly.

**Input:** "Release the motors"
**Output:** `[HW:/servo/release:{}]` Servos released — you can move the device by hand now.

**Input:** "Stop moving" / "Hold still" / "Freeze" / "Stand still"
**Output:** `[HW:/servo/hold:{}]` OK, holding still.

**Input:** "Turn to the right and hold that position"
**Output:** `[HW:/servo/aim:{"direction":"right"}][HW:/servo/hold:{}]` Turned right and holding.
→ Compound movement: emit **one marker per clause**, in order. They fire sequentially before TTS. Dropping the `hold` half means the device drifts back — never confirm a step you did not fire.

**Input:** "Turn right and tell me what you see" / "Look left, what's there?"
**Output:** ONE bash call — camera check, aim, look — then reply from the result:
```bash
c=$(curl -s http://127.0.0.1:5001/camera); case "$c" in
  *'"disabled":true'*) echo "CAMERA_OFF" ;;
  *'"has_frame":false'*) echo "CAMERA_UNAVAILABLE" ;;
  *) curl -sX POST http://127.0.0.1:5001/servo/aim -H 'Content-Type: application/json' -d '{"direction":"right"}' >/dev/null
     curl -sX POST http://127.0.0.1:5000/api/vision/look -H 'Content-Type: application/json' -d '{"question":"What do you see to the right?"}' ;;
esac
```
→ **No `/servo/hold`.** The user did not ask to hold: the look keeps the head still for the photo by itself, and the head returns to idle afterwards. `CAMERA_OFF` / `CAMERA_UNAVAILABLE` → say so and stop; the head did not move. Answer from the returned `description`, or open `path` with an image tool when only a path comes back. On error, do not guess.

**Input:** "Turn right, hold it there, and tell me what you see"
**Output:** the same single bash call with the hold after the aim:
```bash
c=$(curl -s http://127.0.0.1:5001/camera); case "$c" in
  *'"disabled":true'*) echo "CAMERA_OFF" ;;
  *'"has_frame":false'*) echo "CAMERA_UNAVAILABLE" ;;
  *) curl -sX POST http://127.0.0.1:5001/servo/aim -H 'Content-Type: application/json' -d '{"direction":"right"}' >/dev/null
     curl -sX POST http://127.0.0.1:5001/servo/hold -d '{}' >/dev/null
     curl -sX POST http://127.0.0.1:5000/api/vision/look -H 'Content-Type: application/json' -d '{"question":"What do you see to the right?"}' ;;
esac
```
→ The hold stays until the user says "back to normal" (`/servo/resume`).

Rules for both:
- Call `/servo/hold` **only** when the user asks to hold, stay, freeze or keep the position.
- Keep the camera check, the aim and the look in **one** bash call. A separate call costs a model round (~5 s); the turned pose lasts only about 5 s, so the photo would show idle.
- Do **not** use `[HW:...]` markers for these movements. Markers fire only *after* your reply is written, so the photo would show the OLD position. See the Camera skill.

**Input:** "Where are you?" / "Can you find me?" / "Look around for me" / "Where did I go?"
**Output:** run it DURING the turn, then answer from what comes back:
```bash
curl -sX POST http://127.0.0.1:5001/servo/search -H 'Content-Type: application/json' -d '{}'
```
→ Do **not** use `[HW:/servo/search:...]`. Markers fire only *after* your reply is
   written, so a marker-driven search finishes into a turn that has already ended and
   the person is told nothing. The response body **is** your answer.

**Input:** "Find my cup" / "Look around for my keyboard" / "Where did I leave my phone?"
**Output:**
```bash
curl -sX POST http://127.0.0.1:5001/servo/search -H 'Content-Type: application/json' -d '{"target":"cup"}'
```
→ `target` is any noun — COCO classes are found locally, anything else via open-vocab.
   Without it the sweep looks for a PERSON and will end on the first one it sees,
   which is why an object search must always name its target.
→ The sweep takes up to ~40 s and says "still looking" itself at the halfway point, so
   the wait is covered. Wait for it. Do not reply first and do not start a second one.
→ Read the body; do not narrate the movement:
```json
{"found": true, "kind": "cup", "found_at_yaw": 85.0, "centred": true,
 "image_path": "…/media/hal-snapshots/snap_*.jpg",
 "looks_visited": 7, "bearings_visited": 2}
```
   The picture at `image_path` is shown to the person automatically — you cannot see it,
   so say what you found and roughly where, and never describe the image itself.
   `looks_visited` counts camera looks, `bearings_visited` counts body turns. They are
   different numbers; do not call either one "stops".
→ `"found": false` is an answer too. Say you looked and could not find it — do not go quiet,
   and do not offer or run an `exhaustive` scan for a thing: a search always stops at the
   first sighting, and the server ignores `exhaustive` for an object target anyway.

**Input:** "Scan the whole room" / "Is anyone else here?" / "Check the shelf too" / "Do a full scan"
**Output:**
```bash
curl -sX POST http://127.0.0.1:5001/servo/search -H 'Content-Type: application/json' -d '{"exhaustive":true}'
```
→ A SURVEY of people: walks the whole look ring at every bearing, counts everyone it sees,
   and comes back home. It is the only mode that looks above the horizon.
→ Never for an object. "Find my doll" is the search above, which stops at the first sighting
   and stays pointed at it; the server ignores `exhaustive` when a `target` is given.
→ A request to SHOW how far you can move is the demo below, not a scan.

**Input:** "Show me how far you can move" / "How far can you turn?" / "Show me your range of motion" / "Show me your movement range" / "Demonstrate your movement"
**Output:** `[HW:/servo/demo:{}]` Sure — watch this!
→ ONLY when the request is about MOVEMENT — moving, turning, reaching, range of motion.
   A bare "show me what you can do", "what can you do", "show me your maximum capability"
   or "show me your skills" asks about abilities in general (camera, voice, memory, the
   lot) and does NOT start this demo: answer it in words, without any marker.
→ A narrated tour of the movement limits. The device speaks each leg BY ITSELF as it
   moves — "all the way left", "and all the way right", up, down — those lines are
   the hardware's, not yours. Keep your reply to one short opener and do not describe
   the movement in it: the words are already timed to the motion, and a reply that
   narrates it too says everything twice.
→ This is a DEMO. Nothing is detected and nothing is reported. Looking FOR something
   is `/servo/search` above; covering the room is `exhaustive`.
→ Never answer this with `/emotion` or `[HW:/servo/play:{"recording":"scanning"}]`:
   both are short canned animations that cover about 54° of a 270° range and never
   look at anything. Asked for a full turn, they perform a shrug.

**Input:** "I moved you" / "You're in a new place" / "I put you somewhere else" / "Forget where I sit"
**Output:** `[HW:/servo/bearing/reset:{}]` Got it — I'll forget where you usually are and learn it again.

**Input:** "Resume" / "Move again" / "You can move now" / "Start moving" / "Resume normal movement" / "Return to idle"
**Output:** `[HW:/servo/resume:{}]` Alright, back to normal!

## Tools

## How to Control Servo

**For a movement you just announce, no exec/curl is needed.** Inline markers at start of reply:

```
[HW:/servo/aim:{"direction":"desk"}] Aimed at your desk.
[HW:/servo/aim:{"direction":"left","duration":3.0}] Aiming left slowly.
[HW:/servo/play:{"recording":"nod"}] Nodding!
[HW:/servo/hold:{}] OK, holding still.
[HW:/servo/resume:{}] Back to normal!
[HW:/servo/release:{}] Servos released.
[HW:/servo/demo:{}] Sure — watch this!
```

**Use curl instead whenever the RESULT of the movement belongs in this turn** — a search,
or a move followed by looking. A marker fires after your reply is already written, so
anything it produces arrives too late for you to say. `/servo/search` is always curl.

`duration` on `/servo/aim` controls move speed in seconds (default 2.0, 0 = instant).

### Nudge by degrees (relative movement)

```
[HW:/servo/nudge:{"yaw":-15}] Turned left 15 degrees.
[HW:/servo/nudge:{"pitch":10}] Tilted up a bit.
[HW:/servo/nudge:{"yaw":30,"pitch":-5,"duration":1.5}] Moved right and down.
```

| Parameter | Range | Description |
|---|---|---|
| `yaw` | -180 to 180 | Negative = left, positive = right |
| `pitch` | -90 to 90 | Negative = down, positive = up |
| `duration` | 0 to 10 | Move speed in seconds (default 2.0) |

### Available directions

| Direction | What it does |
|---|---|
| `center` | Neutral position, straight ahead |
| `desk` | Tilts down toward the desk surface |
| `wall` | Tilts up toward the wall behind |
| `left` | Turns left |
| `right` | Turns right |
| `up` | Points upward |
| `down` | Points downward |
| `user` | Slightly toward the user (default interaction pose) |

### Play animation

Available animations:

| Animation | When to use |
|---|---|
| `curious` | Something interesting, questions |
| `nod` | Agreement, acknowledgment |
| `headshake` | Disagreement, saying no |
| `happy_wiggle` | Joy, good news |
| `idle` | Resting state |
| `sad` | Empathy, bad news |
| `excited` | High energy, celebrations |
| `shy` | Bashful moments |
| `shock` | Surprise |
| `scanning` | A short searching *gesture* — mood only. It is a 54° canned wiggle with the camera uninvolved, so it never answers "look around for X": that is `/servo/search` above |
| `wake_up` | Waking up, starting a new session |
| `music_groove` | Grooving to music (auto-triggered during playback) |
| `music_chill` | Chill/lo-fi vibe (auto-triggered during calm music) |
| `music_hype` | High-energy hype (auto-triggered during EDM/party music) |
| `listening` | Attentive lean forward, user is speaking |
| `thinking_deep` | Slow deliberate look side-to-side, processing |
| `laugh` | Quick body shake, something funny |
| `confused` | Dog-like head tilt, did not understand |
| `sleepy` | Slow droop with catches, winding down |
| `greeting` | Wave gesture, saying hello |
| `goodbye` | Farewell wave, seeing someone off |
| `acknowledge` | Quick micro-nod (1.5s), confirming |
| `stretching` | Big extension + settle, after waking up |

### Hold position (stop moving)

```
[HW:/servo/hold:{}] OK, holding still.
```

Suppresses idle and ambient animations — the device freezes in current pose. Emotions still play through (the device reacts when you talk, then holds still again). Call `/servo/resume` to return to normal.

**Triggers:** "stop moving", "hold still", "freeze", "don't move", "stand still"

### Resume from hold

```
[HW:/servo/resume:{}] Back to normal!
```

Exits hold mode and resumes idle animations while keeping motor torque on.

**Triggers:** "resume", "move again", "you can move now", "start moving", "resume normal movement", "return to idle".

**Normal movement vs direction:**
- In an established servo/hold context, "back to normal", "return to normal", or
  "turn to normal position" means leave hold and resume idle. Use `/servo/resume`,
  not just `/servo/aim` with `center` or `/servo/play` with `idle`: those do not
  clear hold. Do not make the user know the API word "resume".
- "Face forward", "look straight ahead", or "turn to center" requests only a
  direction. Use `/servo/aim` with `center`; preserve the current hold state.
- If the user explicitly requests both, such as "face forward and move normally
  again", emit aim center followed by resume, in that order.
- Explicit "keep holding", "stay still", or "don't resume" overrides an inferred
  return to normal motion. "Return to your normal position but stay still" means
  aim center then hold, never resume.
- A bare "back to normal" during an unrelated task is not a servo command. Use
  conversation context. If the request concerns this body's motion but its state
  is unknown, read `GET /servo` (`motion_mode`) before interpreting it as leaving
  hold; ask briefly if the intended action is still ambiguous.
- Never use `/servo/release` to return to idle: release disables motor torque.

**Input (currently holding, discussing posture):** "Turn to normal position"
**Output:** `[HW:/servo/resume:{}]` Back to normal movement.

**Input (currently holding):** "Look straight ahead"
**Output:** `[HW:/servo/aim:{"direction":"center"}]` Facing forward.

**Input:** "Face forward and move normally again"
**Output:** `[HW:/servo/aim:{"direction":"center"}][HW:/servo/resume:{}]` Back to normal movement.

**Input:** "Return to your normal position but stay still"
**Output:** `[HW:/servo/aim:{"direction":"center"}][HW:/servo/hold:{}]` Facing forward and holding still.

### Release servos (disable motors)

```
[HW:/servo/release:{}] Servos released.
```

Disables all servo motors so they can be moved freely by hand.

## Error Handling
- If the API returns an error or is unreachable, inform the user that servo control is temporarily unavailable.
- If an invalid direction is given, fall back to the closest matching direction from the available set.
- If an unknown animation is requested, list the available animations for the user.

## Rules
- **For conversation reactions, use the Emotion skill** — it calls servo automatically. Do not use this skill for emotional responses.
- Animations play once and return to rest position.
- Aim positions are persistent until changed.
- Use `/servo/aim` as the primary way to control light direction — do not use raw joint control unless testing.
- Always confirm the action to the user after execution.
- **Hold vs Release**: Hold keeps torque ON (the device stays rigid in place). Release turns torque OFF (the device goes limp). Use hold for "stop moving", release for "let me reposition the device by hand".
- **Hold is soft** — emotions still animate through, then the device holds still again. This keeps the device feeling alive during conversation while respecting the user's request to stop fidgeting.

## Output Template

```
[HW:/servo/aim:{"direction":"desk"}] Aiming at your desk.
```

Use the marker matching the requested action; for movement followed by a visual question, use the curl workflow above instead of repeating the movement in a marker.
