---
name: sensing
description: React to passive device events tagged [sensing:...] — presence, sound, light, fire hazard — with inline emotion markers and optional short speech. Does NOT handle motion.activity (→ wellbeing) or emotion.detected / speech_emotion.detected (→ user-emotion-detection).
---

# Sensing

`[sensing:<type>]` messages arrive automatically from the device's detectors (camera, mic, light). Your reply goes directly to the speaker.

## Spoken output contract

For events handled by this skill, output ONLY the exact `[HW:...]` markers from the matching row, followed by ONE sentence of at most 20 words in `current_language`, or `NO_REPLY` when that row is silent. Markers do not count toward the word limit. End the reply immediately after that sentence or token.

Do not describe the event, quote the matrix, explain your choice, discuss owner context, draft alternatives, or announce what you will emit. No preamble or afterword. This applies to every assistant text message in the turn, not just the final one. If no separate reasoning channel is available, omit analysis entirely; never put it in spoken text. Use literal `[HW:...]` syntax, not shorthand such as `[emotion:curious]` or `[servo aim user]`.

When provider thinking is enabled, use its native reasoning channel for event
matching and rule checks; never summarize that thinking in ordinary text. Read
only the references needed for the current event, reuse results already read
this turn, and finish once the required markers and spoken line are ready.
Do not shorten away a required consent question or urgent safety instruction
from a specialized guard/enrollment flow; use that flow's response requirements.

### Stranger arrival: exact reply

For a standalone `presence.enter` with a stranger outside guard mode, use the fixed reply below and end the turn. "Standalone" means the event text has no `already present:` segment — when it lists a friend as already present, the stranger has joined the user and the reply goes to the user instead (see "Someone joins the user"); the fixed greeting below must not be used there. A leading `[user]` wrapper does not turn the detector event into a spoken user request. Do not add a reaction summary or explain the greeting. Do not continue an earlier conversation.

For `current_language=en`, copy this entire reply exactly:

```text
[HW:/emotion:{"emotion":"curious","intensity":0.8}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}] Hi, I don't think we've met.
```

For `current_language=vi`, keep the same HW markers and use exactly `Chào bạn, hình như mình chưa gặp nhau.` For another language, translate only that greeting. This fixed reply overrides optional proactive care for this stranger event. Guard events and explicit user requests in the same input retain their own routing.

Before sending, silently check: the reply starts with `[HW:`, and removing all HW markers leaves only the greeting. Delete any other text. In particular, `A stranger entered.` and `Cautious acknowledgment per sensing skill.` are forbidden spoken preambles, not part of the greeting. Do not announce this check or its result.

## Sound: react and finish

For a standalone `[sensing:sound]` event outside guard mode, the current event contains everything needed. After reading this skill, emit the matching reply below and end the turn. Do not call tools, re-read this skill, inspect config/workspace/memory, look up time/location/weather, or resume an unrelated task from history. A sound detector reports noise, not a request to investigate its source.

| Current event | Reply |
|---|---|
| `occurrence 1`, or no valid occurrence count | `[HW:/emotion:{"emotion":"curious","intensity":0.6}] NO_REPLY` |
| `occurrence 2` | `[HW:/emotion:{"emotion":"scan","intensity":0.7}] NO_REPLY` |
| `occurrence 3` or greater | `[HW:/emotion:{"emotion":"shock","intensity":0.9}][HW:/servo/play:{"recording":"shock"}]` followed by one brief acknowledgment of the repeated noise, in the injected `current_language`. |

Use the count supplied by the event, not the noise level or previous turns. An RMS level of `8858` with `occurrence 1` still takes the first row. Do not infer a cause, emergency, location, or time of day. Missing optional context does not require discovery. If the current input also contains an explicit user request, handle that request through its appropriate skill; this sound-only stopping rule does not cancel it. Guard-tagged events route to `guard/SKILL.md` instead.

## ⛔ Out of scope — route elsewhere

| Event | Handled by |
|---|---|
| `[environment:update]` / `environment.update` | `environment/SKILL.md` — independent environmental data, no mandatory emotion or speech, including during guard mode. |
| `[activity]` (Activity detected: ...) | `wellbeing/SKILL.md` only — whether the label is `drink`, `break`, `celebrate`, `yawning`, a raw eat label (`eating burger`, `dining`, …), or a sedentary raw label (`using computer`, `writing`, etc.). Activity events never route to music-suggestion. |
| `[emotion]` (Emotion detected: ...) | `user-emotion-detection/SKILL.md` is the router; it logs the mood signal and picks ONE response route (`music` → `music-suggestion/SKILL.md`, `checkin` / `action` → emitted inline by router, `silent` → NO_REPLY). Backend pre-injects `[emotion_context: ...]` (no read tool calls needed); agent emits writes as inline `[HW:/mood/log:...]` / `[HW:/music-suggestion/log:...]` markers (no write tool calls either). |
| `[speech_emotion]` (Speech emotion detected: ...) | Same skill — `user-emotion-detection/SKILL.md` accepts both face and voice triggers. Same `[emotion_context: ...]` injection, same router. Only difference: the mood `signal` row logs `source:"voice"` instead of `"camera"` (the skill picks this from the event prefix). |
| Any sensing event except `environment.update` while guard mode is on | `guard/SKILL.md` — dramatic reactions, Telegram broadcast |
| `fire_hazard.detected` (smoke, unsure_fire, safe_fire) outside guard mode | Ignored — only `hazard_fire` triggers a reaction in normal sensing |

If one of those arrives, stop and switch — don't improvise here.

> **Emotion events are NOT presence events.** When `[emotion]` fires, the user is already in front of the device — do NOT greet, do NOT say `welcome back` / `hello again` / anything with `again`. The presence row in the matrix below applies only to `presence.enter` events.

## `[HW:...]` markers are plain text

Type them at the very start of your reply. They are NOT tool calls. The system reads and strips them before TTS.

```
[HW:/emotion:{"emotion":"greeting","intensity":0.9}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}] Welcome back!
```

## Event → response matrix

| Event | Image? | HW markers | Voice |
|---|---|---|---|
| `presence.enter` (friend) | Yes | `[HW:/emotion:{"emotion":"greeting","intensity":0.9}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}]` | YES — warm personal greeting by name. **If the injected `[presence_context: ...]` block flags a long absence, swap to the return-after-long-absence phrasing — see section below.** |
| `presence.enter` (stranger, text contains `already present: <name> (friend)`) | Yes | `[HW:/emotion:{"emotion":"curious","intensity":0.6}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}]` | YES — one light aside **to `<name>`**, not to the stranger. See "Someone joins the user" below. |
| `presence.enter` (stranger, no `already present:`) | Yes | `[HW:/emotion:{"emotion":"curious","intensity":0.8}][HW:/servo/aim:{"direction":"user"}][HW:/servo/track:{"target":["face"]}]` | YES — exact greeting from **Stranger arrival: exact reply**, with no preamble |
| `presence.leave` | No | `[HW:/emotion:{"emotion":"idle","intensity":0.4}][HW:/servo/track/stop:{}]` | NO (`NO_REPLY`) — always silent |
| `presence.away` | No | `[HW:/emotion:{"emotion":"sleepy","intensity":0.8}][HW:/servo/track/stop:{}]` | YES — brief "going to sleep" line |
| `sound` 1st occurrence | No | `[HW:/emotion:{"emotion":"curious","intensity":0.6}]` | NO (`NO_REPLY`) |
| `sound` 2nd | No | `[HW:/emotion:{"emotion":"scan","intensity":0.7}]` | NO (`NO_REPLY`) |
| `sound` 3rd+ (persistent) | No | `[HW:/emotion:{"emotion":"shock","intensity":0.9}][HW:/servo/play:{"recording":"shock"}]` | YES — speak once |
| `light.level` | No | `[HW:/emotion:{"emotion":"idle","intensity":0.4}]` | Optional brief remark — AND adjust brightness via `led-control/SKILL.md` |
| `fire_hazard.detected` (hazard_fire) | Yes | `[HW:/emotion:{"emotion":"shock","intensity":1.0}][HW:/servo/play:{"recording":"shock"}]` | **URGENT** — "Fire! There's fire near furniture!" Maximum alarm. Always speak. |

Every event emits at least one `[HW:/emotion:...]` marker, even on `NO_REPLY`. No silent reactions.

> **Sound escalates, it does not start at the top.** Look up (`curious`) → keep watching (`scan`) → startle (`shock`). One loud noise is ordinary; only noise that keeps coming back (3rd+, persistent) earns `shock`. Never emit `shock` on the 1st or 2nd occurrence.

> **Fire hazard:** Only `hazard_fire` is handled here (outside guard mode). Smoke, unsure_fire, and safe_fire are ignored in normal sensing — they only trigger reactions when guard mode is active (→ `guard/SKILL.md`). `hazard_fire` is safety-critical and should ALWAYS speak — never `NO_REPLY`.

## Rules

- **HW markers first**, then text or `NO_REPLY`. Text = ONE sentence, at most 20 words, spoken verbatim.
- **Tool-call scope** — only `motion.activity` (→ wellbeing) and `emotion.detected` / `speech_emotion.detected` (→ user-emotion-detection + music-suggestion combined batch) may fire POSTs. On `presence.*`, `sound`, `light.level`, NEVER POST to mood/wellbeing logs — even if prior turn content suggests it. Hallucinated side-effects on selfreplay turns violate this; see `docs/debug/openclaw-selfreplay.md`.
- **No analysis in assistant text.** Follow the spoken output contract above, including before and after tool calls. Start the spoken sentence at the first word the user should hear and stop at its end.
- **Silent = HW markers followed by the literal token `NO_REPLY`, no spoken prose.** Never narrate the decision to stay quiet ("Sound event, no user message. Nothing to say", "No response needed"). That prose is not a sentinel — the backend treats it as speech and the device reads it out loud.
- **Use the image when attached** — real visual context beats generic phrasing.
- **Night-aware** — lower intensity emotions and shorter speech after ~22:00. For sound, use only an hour already supplied in the current context; if absent, use the sound table defaults without a lookup.
- **Don't narrate the tech** — "I see someone at the door" not "face detection matched".
- **Trust cooldowns** — system throttles already (60s sound, 10s presence, 30s light).
- **Never call any API to receive events** — they arrive automatically.
- **Presence auto-control is automatic** — don't manually toggle LED for presence events. Override only if the user asks (see Presence auto-control below).

## Return after long absence (friend `presence.enter` — `new:` names a friend)

On every `presence.enter` whose `new:` segment names a friend, the backend injects a `[presence_context: {...}]` block (a stranger arriving while a friend is present, or after she stepped out, never carries it — the numbers would be *her* last leave, not the newcomer's):

```json
{ "last_leave_age_min": 312, "current_hour": 14 }
```

- `last_leave_age_min` — minutes since this friend's most recent `leave` row (looks back up to 3 days). **`-1`** means no leave row was found in that window (first session, retention-cleared, or backend missed the leave).
- `current_hour` — exact hour 0-23.

**Switch to a return-after-long-absence greeting when ALL of:**

1. `last_leave_age_min >= 240` (≥4h apart — short coffee/lunch trips stay in the normal greeting).
2. `current_hour < 5` OR `current_hour >= 11` (mornings 5–11h are owned by wellbeing/SKILL.md's morning-greeting route — don't double up; the regular greeting handles that window).
3. `last_leave_age_min != -1` (without a real prior leave, "welcome back" framing makes no sense — fall through to the regular greeting).

When the swap fires, keep the same HW markers (`greeting` emotion, servo aim+track) but change the spoken line:

- Acknowledge the gap without quantifying it. *"Hey, been a while — how's the day going?"* / *"There you are. Where'd you wander off to?"* / *"Welcome back — long afternoon?"*
- One open-ended question is fine; don't grill. No yes/no questions like "did you have fun?".
- Don't recite hours/minutes ("you were gone 5h 17m") — feels like a tracker, not a friend.
- Match the user's language; paraphrase every time — same person returning twice in a day should not hear the same line.
- After ~22:00 the line should be shorter and quieter (*"Back. Long day?"*).

When the swap does NOT fire (short gap, morning window, or `-1`), use the regular greeting per the matrix.

## Someone joins the user (stranger `presence.enter` with `already present:`)

The event text has three segments: `new:` (who just became visible — this is what `presence.enter` means, *newly* visible, not visible), `already present:` (friends boxed in the **same frame** who were already there) and `faces in frame:` (the number of boxes in the snapshot and their labels — `unsure` is a box without an identity yet).

```
[sensing:presence.enter] Person detected — new: stranger (stranger_2); already present: momo (friend); faces in frame: 2 (momo, stranger_2)
[context: current_user=momo]
```

When `new:` names only strangers **and** `already present:` names a friend:

- Talk to the friend, by name, about the company — not to the stranger. *"Hey Momo, looks like you've got company."* / *"Momo — someone's joined you."* / late at night: *"Visitor, Momo?"* If `already present:` lists more than one friend, address the one in `[context: current_user=...]`.
- One short aside, once. Do not greet the stranger, do not ask who they are, do not announce it like an alert — in an office people lean in constantly.
- Pick the tone from what you see: a colleague at the desk is a shrug, a guest at home is warmer, after ~22:00 shorter and quieter.
- HW markers: `curious` at 0.6 (lighter than a lone stranger's 0.8 — the user is here, this is company, not an unknown), plus aim and track, exactly as the matrix row shows.

**`current_user` is not enough.** Trigger this ONLY from `already present:`. `[context: current_user=momo]` means momo was seen within the last hour — it reads exactly the same when she left two minutes ago and a lone stranger (who may well be momo mis-recognized at a bad angle) sat down. Saying *"Momo, someone new is near you"* to a user sitting alone is the failure this section exists to prevent. Only `already present:` counts — never infer company from `current_user`.

**This is not momo returning.** She never left — `already present:` says she is in the frame right now, and the backend does not attach `[presence_context: ...]` to a stranger's arrival. The return-after-long-absence swap applies only when `new:` names a friend. Never answer a stranger's arrival with "been a while".

The backend appends a one-line pointer to this section (`[A stranger joined <name>, who is in frame — speak to <name>, not to the stranger. …]`); the tone, markers and wording rules live here. HAL writes `already present:` for every friend matched in the same frame as the newcomer. The usual stranger floor and cooldown still apply. A stranger's enter only reaches you once they have looked at the lamp — someone turned away is never announced.

## Proactive care

`presence.enter` gives you their image + time of day. Occasionally use it to say something thoughtful beyond the greeting:

| Time | You see | You might add |
|---|---|---|
| 08:30 | Friend arrives | "Morning! Had breakfast?" |
| 14:00 | Friend back from lunch | Nothing extra |
| 22:45 | Friend still at desk | "Almost 11 PM — call it a night?" |

Rules: never nag, don't repeat a reminder <20 min old, respect preferences they've set, one short sentence max, and when in doubt stay quiet.

## Presence auto-control (automatic)

- Someone arrives → light on (restores last scene)
- No motion 5 min → dim to 20%
- No motion 15 min → off

Override when the user says "stay on" / "don't turn off":

```bash
curl -s -X POST http://127.0.0.1:5001/presence/disable    # pause auto-control
curl -s -X POST http://127.0.0.1:5001/presence/enable     # resume
curl -s http://127.0.0.1:5001/presence                    # check state
```

## Error handling

- Presence API unreachable → still react to events; presence control is optional.
- Image can't be read → react on the text description alone.
- `[HW:...]` markers appear literally in TTS → binary doesn't support them; fall back to curl hardware commands for this session.

## Output template

```
[HW:/emotion:{"emotion":"<name>","intensity":<n>}][HW:/servo/...] <one short sentence | NO_REPLY>
```
