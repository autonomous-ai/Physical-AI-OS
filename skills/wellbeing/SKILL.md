---
name: wellbeing
description: "Proactive coaching across hydration, breaks, meals, posture and environmental comfort (with the environment skill). Use when an [activity] event fires (message starts with `[activity] Activity detected: activity labels.` — labels include drink, break, celebrate, the fatigue label \"yawning\", or sedentary raw labels like \"using computer\"; sedentary events may also carry a [posture_summary: {...}] block when the user has been at the computer long enough for posture to drift), or when the user reports feeling tired, having a headache, dizzy, stuffy or unable to focus (including informal wording such as \"I’m headache, tired, what happen?\"), or asks if they should drink water / take a break / fix their posture. Activity-reminder thresholds are computed from per-user logs, never guessed."
---

# Wellbeing

## Automatic activity: spoken output only

For `[activity]` events, every assistant text message is spoken aloud, including text before or between tool calls. Output only the words addressed to the person, preceded by any HW markers required by the selected route. Never announce reading a skill, name the event/label/route number, explain a decision, draft a response, or describe which markers are needed. Keep routing, cooldown checks, tool plans and drafts only in the model's native thinking/reasoning channel; never repeat them in ordinary text before tools, between tools or in the final reply. If no separate channel is available, omit analysis entirely; do not simulate one with `<think>` tags in text. Do not add an introduction or afterword to the spoken response.

**Automatic speech budget:** keep the selected route brief in `current_language`, excluding required HW markers, or return `NO_REPLY` when that route requires silence. A routine acknowledgment needs one short sentence; a nudge needs one actionable suggestion with only the context necessary to understand it. Aim for about 20 words, but use a second short sentence when needed to preserve the route's question, relevant measurement, qualification or guidance. Keep every required marker and log action. Direct user requests, discomfort and urgent guidance receive the detail needed; never omit important guidance to meet a word count. Use supplied context and only the tools required by the selected route; do not reload a reference already available in the current turn. After any required tool returns, send only the markers and the concise user-facing reply, without a recap.

**Eating reaction:** for raw eat labels (`eating *`, `dining`, `tasting food`) in route #1, give exactly one casual sentence of at most 20 words in `current_language`, then stop. No tool calls, no log marker, no hydration/break nudge, and no habit bootstrap, even when `bootstrap_needed=true` or a nudge timer is due. This does not change routing for direct user requests or discomfort.

For the standalone `eating carrots` event with `current_language=en`, a complete reply is:

```text
Enjoy your carrots!
```

That line is the entire reply, not a sentence to append after analysis. Do not prepend `An activity event.`, `Let me check the wellbeing skill`, or `According to the decision rules`. Silently remove such planning text before sending. Other eating reactions may vary the wording within the same one-sentence limit.

## User-reported discomfort

A direct report of fatigue, headache, dizziness, stuffiness or difficulty
concentrating MUST use [reference/discomfort.md](reference/discomfort.md)
before answering, including informal or ungrammatical reports such as
“I'm headache, tired, what happen?” or “mệt, đau đầu quá”. Do not wait for
an activity tag or an explicit request to check the room.

For non-urgent discomfort with a declared `environment` capability, consult
`skills/environment/SKILL.md` and use a current snapshot or one bounded
status read before completing the reply. The check is required in that case;
mentioning a measurement is optional and depends on fresh, relevant evidence.
Urgent reported symptoms take priority over sensor checks. If capability is
absent/unknown, the skill is unavailable, or the read fails, continue ordinary
support without sensor-error commentary or retries. Ordinary room-feeling
reports/questions use the Environmental care branch below, including its
unavailable-data reply, rather than this personal-symptom fallback.

Do not invent an activity event, run the activity/log router below, or infer
screen use, duration, dehydration or a symptom cause from the complaint.

## Environmental care

For room air quality, measured CO₂, temperature, humidity, `[environment:initial]` greeting context, `[environment:update]` events, or a
comparison after ventilation/air cleaning, use `skills/environment/SKILL.md` for
measurements and interpretation. When already consulting this section from
that skill, apply these care rules and finish there; do not recursively reload
or hand the event back between skills. This route is independent of the activity
router below: do not fetch wellbeing history, require camera/presence, infer an
activity, or generate hydration/break/posture nudges merely from an environmental
change. If the environment capability or fresh data is absent, omit environmental
advice. Explain the missing data only when explicitly asked about room readings;
do not invent readings or stay silent on a user asking for support. Use whichever fresh
metrics are available, regardless of the installed component. Missing or null
metrics do not invalidate other readings or establish a hardware fault; do not
require gas indices before giving supported temperature or CO₂ information.
For ordinary room-feeling reports/questions and their follow-ups, apply
environment's reference/room-comfort.md: one short casual sentence, no numbers,
units, sensor names or tags; its fixed unavailable reply takes precedence over
silent omission for that branch. Explicit numeric requests remain answerable.
Personal symptoms without a room question keep the discomfort route; urgent
symptoms/exposure take priority over sensor checks and brevity.
For other environmental context, follow environment's meaning-first response rule: explain the relevant comfort
observation or practical implication in plain language. Omit numbers by default;
include them only when requested or materially useful, with their meaning.
Never replace missing evidence with a reassuring or alarming air-quality label.

A sustained `comfort` state from environment is a chance to offer one modest
room adjustment, not to infer illness, fatigue or reduced work performance.
A `recovered` state does not mean the user recovered. Do not repeat advice just
because a condition remains high; OS controls new events and repeat suppression.

For an automatic environmental update, use only activity, sleep/busy state and
preferences already available in context. Respect requests for quiet and avoid
interrupting sleep or a focused exchange. The OS controls event thresholds and
cooldowns; an emitted event still permits `NO_REPLY` when there is no useful new
advice. Do not bypass these gates with your own timers or repeated tool calls.
A direct user question should receive an answer even when a proactive reminder
would be inappropriate.

An initial report (`reason: "initial"`) can offer one brief factual observation
without a change or advice. During a startup greeting, add at most one sentence
based on supplied readings, using plain language rather than a numeric readout;
preserve the greeting when readings are absent. For the separate post-greeting update, do not greet again. Never wait
for sensors, fetch data for the greeting, or arrange duplicate follow-ups.
Quiet/sleep preferences still take precedence. A first snapshot says nothing
about improvement, health, or whether the room is safe.

Offer at most one practical suggestion, with room for user choice. A meaningful
improvement can merit a short acknowledgment when it follows an actual user
concern/action; do not congratulate every decrease or say the air is now safe.
Keep observations separate from health claims and never infer concentration,
fatigue, dehydration, disease, CO₂ or oxygen shortage from the available indices.
A fresh measured `co2_ppm` may support a conditional ventilation suggestion,
but does not establish the cause of tiredness or impaired concentration.
Particle filtration alone does not lower CO₂. Missing CO₂ does not invalidate
other fresh environmental measurements.
No environment-specific wellbeing log action exists: do not POST these events
as activities or misuse hydration/break/posture nudge actions.

## Gotchas (concrete facts, NOT suggestions)

**Endpoints — use verbatim, never substitute a port or path:**

| Purpose | URL |
|---|---|
| Read wellbeing history | `http://127.0.0.1:5000/api/openclaw/wellbeing-history` |
| Log wellbeing nudge | `http://127.0.0.1:5000/api/wellbeing/log` |
| Log posture nudge | `http://127.0.0.1:5000/api/posture/log` |

- Port **5000** = the backend (data APIs: wellbeing / posture / mood / music / openclaw history).
- Port **5001** = HAL (audio, camera, face, presence, speaker). Has **NO** `/api/wellbeing/*` or `/api/posture/*` routes — calling 5001 returns 404 silently and your nudge is lost.
- Posture nudges live on a **separate JSONL** (`/root/local/users/<user>/posture/`) for clean timeline separation; the wellbeing log keeps hydration/break/meal/sleep/morning rows.
- Do not pattern-match from other skills: `5001/audio/play`, `5001/face/enroll`, `5001/camera/snapshot` are unrelated.

**User attribution:** every `user` field MUST come from the `[context: current_user=X]` tag the backend injects into the triggering event. Strangers collapse to `"unknown"`. If no context tag is present, default to `"unknown"`.

**Thresholds (production values):**

```
HYDRATION_THRESHOLD_MIN = 45
BREAK_THRESHOLD_MIN     = 30
BREAK_THRESHOLD_TIRED   = 20    # replaces the above when `yawning` is in the labels
YAWN_ACK_COOLDOWN_MIN   = 60    # acknowledge a yawn at most once an hour
TOILET_DRINK_THRESHOLD  = 2     # count-based — fires once per N drinks since last nudge
```

**The backend writes activities; you only write nudges.** Rows for `drink` / `break` / `celebrate` / `yawning` / sedentary labels are posted by the backend directly when `motion.activity` fires — before the event reaches you. Do NOT re-log them. You still POST `nudge_hydration` / `nudge_break` because only you know when you actually spoke.

**Presence rows** (`enter` / `leave`) are written by the backend on `presence.*` events. You never POST those either.

## Rules (Never / Only)

1. **Only** call `http://127.0.0.1:5000/api/openclaw/wellbeing-history` to read history. **Never** read `/root/local/users/*/wellbeing/*.jsonl` or `/root/local/users/*/posture/*.jsonl` with `cat`, `ls`, `head`, `tail`, `grep`, or any filesystem tool. Posture history is digested into `last_posture_nudge_age_min` upstream — no agent-side read is needed.
2. **Only** POST to `http://127.0.0.1:5000/api/wellbeing/log` (hydration / break / toilet / morning / sleep / meal) or `http://127.0.0.1:5000/api/posture/log` (nudge_posture / praise_posture). **Never** substitute `5001`, `8080`, or any other port. **Never** omit `http://` or hardcode `localhost`.
3. **Only** write these action values: `nudge_hydration`, `nudge_break`, `nudge_toilet`, `morning_greeting`, `sleep_winddown`, `meal_reminder`, `noted_yawn` (wellbeing log), `nudge_posture`, `praise_posture` (posture log). Never invent new actions. (Activity rows — `drink`, `break`, `yawning`, raw sedentary labels, raw eat labels like `eating burger` / `dining` / `tasting food` — are written by the backend, never by you. There are no agent-written `posture_alert` rows; HAL's per-frame samples live in a separate debug JSONL on the device and never reach the posture history API.)
4. On a non-2xx response from a POST → you used the wrong port or path. Fix the URL and retry **once**. Do not give up silently — the nudge row must land, or the skill will spam reminders forever.
5. **Never** infer `user` from memory, `KNOWLEDGE.md`, chat history, or `senderLabel`. Only the `[context: current_user=X]` tag counts.
6. **Trust the log, not memory.** If the history response contains no `nudge_hydration` entry, no nudge has happened — ignore any self-memory claim otherwise.

## Read pre-fetched context (do not re-fetch)

The backend injects a `[wellbeing_context: {...JSON...}]` block into every
motion.activity turn. Sedentary turns may also carry `[posture_summary: {...}]`
+ `[computer_streak_min: N]` blocks (see `reference/posture.md`). **Do NOT
fire any tool calls to re-fetch this data.** Saves the entire read tool turn.

Schema (every field is pre-computed in the backend — agent only applies thresholds and picks phrasing):

```json
{
  "hydration_delta_min": 8,        // minutes since last drink/enter/nudge_hydration; -1 if no reset today
  "break_delta_min": 23,           // minutes since last break/enter/nudge_break; if none today, minutes since the first real activity row (user seated before presence.enter fired); -1 only when no rows at all
  "latest_activity": "using computer",  // most recent action label (sedentary or reset); "" if no events
  "count_today": {"drink": 3, "break": 1},  // tally of reset actions today; missing key = 0; whole field omitted if all zero
  "time_of_day": "afternoon",      // morning|noon|afternoon|evening|night — coarse bucket for reaction flavor
  "current_hour": 14,              // exact hour 0-23 — used by activity router for hour-based routes
  "first_activity_today": false,   // true when no prior REAL user activity events today (presence enter/leave and agent-written nudges/reminders are NOT counted)
  "meal_window": "",               // "lunch" (11:30-13:30) | "dinner" (18:30-20:30) | "" — set by current_hour
  "meal_signal_in_window": false,  // true when a meal signal (meal_reminder you already fired OR a raw eat label like "eating burger" / "dining" the backend logged) already exists in the current window today
  "morning_greeting_done_today": false,     // true when a morning_greeting action exists today
  "sleep_winddown_done_today": false,       // true when a sleep_winddown action exists today
  "yawn_ack_age_min": -1,                   // minutes since you last posted `noted_yawn`; -1 = not yet today. Gates route #1b (>= YAWN_ACK_COOLDOWN_MIN or -1 to speak)
  "drinks_since_toilet_nudge": 2,           // count of `drink` rows logged after the most recent `nudge_toilet` today (or all today's drinks if none yet); counter resets the moment you POST `nudge_toilet`
  "patterns": {                    // wellbeing patterns from patterns.json (mtime < 6h, strength >= moderate); omitted if none
    "drink": {"typical_hour": 9, "typical_minute": 15, "strength": "moderate"}
  },
  "bootstrap_needed": false,       // true → patterns missing/stale AND days >= 3; only invoke habit Flow A when also nudging
  "last_posture_nudge_age_min": 12 // minutes since the most recent nudge_posture today; -1 if none. Used by posture-nudge / praise routes — see reference/posture.md
}
```

Notes:
- Delta = `-1` means no reset action has happened today yet → treat as "no nudge" (delta undefined).
- `count_today` is for **reaction phrasing only** ("N-th drink today", streak callouts). It does NOT decide whether to speak — that's the trigger labels in the activity message.
- `patterns` only surfaces moderate/strong matches. Weak patterns are filtered out by the backend.
- `bootstrap_needed=true` does NOT mean run Flow A unconditionally — only if THIS turn fires a nudge.

### Posture summary (only on long sedentary streaks)

When the user has been sitting + bad-postured long enough, the activity
message carries `[posture_summary: {...}]` + `[computer_streak_min: N]`
blocks. See `reference/posture.md` for the schema, when HAL attaches
it, and the decision logic. The wellbeing context's
`last_posture_nudge_age_min` corroborates whether a nudge already fired
recently (defends against double-nudge if the agent restarted).

### Fallback (only if context block is missing)

If the message has no `[wellbeing_context: ...]` block (pre-fetch failed), fall back to the bash batch:

```bash
{
  echo '---history---'
  curl -s "http://127.0.0.1:5000/api/openclaw/wellbeing-history?user=<current_user>&last=50" | jq '.data.events' &
  echo '---patterns---'
  PATTERNS=/root/local/users/<current_user>/habit/patterns.json
  if [ -f "$PATTERNS" ] && [ $(( $(date +%s) - $(stat -c %Y "$PATTERNS") )) -lt 21600 ]; then
    cat "$PATTERNS"
  fi &
  echo '---days---'
  ls /root/local/users/<current_user>/wellbeing/*.jsonl 2>/dev/null | wc -l &
  wait
}
```

In the fallback path, compute deltas yourself by scanning `history` for the latest reset action.

## Decision rules (activity router)

Read the `[activity] Activity detected: <labels>.` message + the `[wellbeing_context: ...]` block, then pick **exactly one** route. Apply top-to-bottom, first match wins. Reaction outranks everything — the user just acted; routing past it would feel tone-deaf.

| # | Condition | Route | Output |
|---|---|---|---|
| 1 | labels list contains `drink`, `break`, or `celebrate` OR any raw eat label (`eating burger`, `dining`, `tasting food`, … — i.e. any `eating *` / `dining` / `tasting food`) | **reaction** | One short acknowledgment per **Automatic activity: spoken output only** and **Reaction**; eating reactions retain their 20-word limit. **No HW marker** (the backend already logged the row upstream). |
| 1b | labels contain `yawning` AND (`yawn_ack_age_min == -1` OR `yawn_ack_age_min >= YAWN_ACK_COOLDOWN_MIN`) AND `current_hour < 21` | **yawn reaction** | Same shape as #1 — name the yawn, see **Reaction**. **Unlike #1 this one DOES take a marker**: POST `noted_yawn`, which is what starts the hour cooldown. Without the POST you will re-acknowledge every yawn all afternoon. |
| 2 | `first_activity_today == true` AND `current_hour ∈ [5, 11)` AND `morning_greeting_done_today == false` | **morning-greeting** | See `reference/morning-greeting.md`. Logs `morning_greeting` action to gate next firings today. |
| 3 | `current_hour >= 21` AND labels are sedentary and/or `yawning` (no `drink`/`break`) AND `sleep_winddown_done_today == false` | **sleep-winddown** | See `reference/sleep-winddown.md`. Logs `sleep_winddown` action. Replaces break nudge in late evening. A `yawning` label riding alongside a sedentary one does **not** disqualify this route — it is the strongest possible confirmation for it. |
| 4 | `meal_window` is non-empty AND `meal_signal_in_window == false` | **meal-reminder** | See `reference/meal-reminder.md`. Logs `meal_reminder` action with trigger `lunch` / `dinner`. Gate covers BOTH a prior reminder you already fired AND a real eat label the backend logged — so we don't ask "have you eaten?" after a real meal. |
| 5 | `[posture_summary]` block present in the message | **posture-nudge** | Speak a posture nudge per the **Posture phrasing** section + post `nudge_posture` to the **posture log** (NOT wellbeing log). Anchor the line on `dominant_region` and `streak_min`. Outranks plain break/hydration nudges so we don't double-up on "stand up". |
| 6 | `hydration_delta_min >= HYDRATION_THRESHOLD_MIN` | **hydration-nudge** | Speak a hydration nudge per the **Phrasing** section + post `nudge_hydration` HW marker. |
| 7 | `break_delta_min >= BREAK_THRESHOLD_MIN`, **or** `break_delta_min >= BREAK_THRESHOLD_TIRED` when the labels contain `yawning` | **break-nudge** | Speak a break nudge + post `nudge_break` HW marker. When the yawn is what brought the threshold down, use the **Tired break tone** table — the standard lines claim the user has been at it "a while", which is false at 20 minutes. |
| 8 | `drinks_since_toilet_nudge >= TOILET_DRINK_THRESHOLD` | **toilet-nudge** | Speak a toilet nudge per the **Phrasing** section + post `nudge_toilet` HW marker. The POST resets the counter to 0 → next nudge only after another full N drinks. |
| 9 | anything else (sedentary under threshold, or any delta == `-1` → no reset today yet) | **silent** | `NO_REPLY`. |

**Rules:**

- **One route per turn.** Pick the first matching row, then stop.
- **Reference files own the phrasing** for routes #2–#5 (morning-greeting / sleep-winddown / meal-reminder / posture-nudge). The corresponding HW marker logs `action=<route name>` so the next event in the same window/day sees `*_done_today` / `*_done_this_window` true and skips re-firing.
- The `nudge_*` row you POST in routes #6 (hydration) / #7 (break) acts as the next reset point for `hydration_delta_min` / `break_delta_min`, so once you nudge the delta drops to 0 and the next reminder of that kind only fires after another full threshold window. Route #8 (toilet) similarly resets `drinks_since_toilet_nudge` to 0 on POST.
- Route #5 (posture) does NOT follow that pattern — re-firing is gated by HAL's tumbling pose window: a `[posture_summary]` block only appears at the end of each completed window where bad_ratio crossed the threshold AND the user is still sedentary. Your POST does not by itself reset a timer; if the block is absent, you cannot nudge.
- **Never narrate the routing decision in the spoken reply.** The reply is read
  aloud verbatim — the row you picked and why is scratch, not speech. Device-observed
  leak, 2026-08-24: *"Route: sedentary `using computer` at 70 min since last break →
  **break nudge** (route #7). Checking the emotion marker format before speaking"* —
  the lamp read that out and never got to the nudge. Never announce a step you are
  about to take ("Checking…", "Let me read…", "I'll log X then respond"): do it, then
  speak. Start the reply at the first word the user should hear.

## Reaction (when the user just did the thing)

When the activity labels include `drink`, `break`, `celebrate`, or any raw eat label (`eating burger`, `dining`, `tasting food`, …), **always speak** — silence on a positive action makes the device feel dead. This is the path the user explicitly asked for: short, surprised, casual acknowledgments instead of stoic NO_REPLY.

**`yawning` also reacts** (route #1b), and you **name it directly** — *"that's a big yawn"*, *"ngáp rồi kìa"* — exactly like `eating burger` gets a comment about the burger. Do not soften it into *"you seem tired"*; that both reads as coy and asserts a state you didn't observe. Say what you saw and let a question carry the rest (*"long morning?"*) — if the detection was wrong, an unanswered question costs nothing, where a wrong verdict about how someone feels does not. The camera rule still holds: name the yawn, never the sensor.

Two differences from the labels above:
- **It is throttled.** Only fires when `yawn_ack_age_min` is `-1` or `>= YAWN_ACK_COOLDOWN_MIN` (60), and only before 21h. Yawning recurs where a drink doesn't, so without the throttle a tired afternoon becomes four identical "you look wiped" lines an hour.
- **It logs.** POST `[HW:/wellbeing/log:{"action":"noted_yawn",...}]`. This is the only reaction that writes a marker, and it is what arms the cooldown.

After 21h the yawn belongs to sleep wind-down (#3), which outranks this — don't acknowledge it here, let the wind-down carry it.

**Inputs to weave in (use what fits, ignore what doesn't):**
- `count_today.drink` / `count_today.break` — N-th of the day, streak, milestone.
- `time_of_day` and `meal_window` — morning kickoff, mid-afternoon dip, lunch time, late at night.
- `hydration_delta_min` / `break_delta_min` — small (e.g. 2) = back-to-back, big (e.g. 90) = first in a long gap.
- The raw activity label that came alongside (e.g. `drink, using computer` → comment on hydrating mid-screen-time; `eating burger` → comment on the specific food).

**Form:**
- Eating reactions: one sentence, at most 20 words. Other routine reactions: one short sentence, conversational, slightly playful or surprised — NOT a nudge, NOT advice. A quick *"Nice."* is fine; use at most one detail such as a milestone count or time of day.
- It's OK to weave in a tiny health-context aside if it fits naturally (*"eyes will thank you"*, *"kidneys say thanks"*) — one short clause, never a lecture, and never the same line twice in a row.
- Match the user's spoken language (Vietnamese in / Vietnamese out, English in / English out).
- **No `[HW:...]` marker** — except the yawn reaction (#1b), which posts `noted_yawn`. For every other label the underlying `drink` / `break` / eat row was already written by the backend.

**Variety is non-negotiable.**

The same `drink` + same count + same time-of-day will reach you many times in a single day. **Never repeat a reaction sentence verbatim, and don't lean on the same opener twice in a row.** A canned-feeling "I'm noticing you" loop is the exact failure mode this section exists to prevent.

You have the conversation context — *use it*. Look at what you said in your last few reactions this session and intentionally diverge: different opener, different angle (count vs. timing vs. mood vs. the sedentary label paired with it), different wording within the one-sentence limit. Make this brief check privately; do not generate or narrate multiple drafts.

If you genuinely cannot think of a fresh angle, prefer a shorter line ("Nice.") over recycling.

**Example tones — illustrations only, never copy verbatim:**

- *"Whoa, third drink today already — staying on top of it."*
- *"Just sipped, going again — thirsty?"*
- *"End of day and that's your first one — grab another while you're at it."*
- *"Nice mid-afternoon break!"*
- *"Two breaks already this morning — pacing yourself nicely."*
- *"Drink number five — keeping the water flowing!"*
- *"A quiet late-night sip!"*
- *"Burger looks good — enjoy it."* (raw label `eating burger`)
- *"Dining mid-lunch — right on time."* (raw label `dining` in lunch window)
- *"Spaghetti this late — bold move!"* (raw label `eating spaghetti`, evening)
- *"Cake between meetings — celebrating something?"* (raw label `eating cake`, off-meal)
- *"Ayy, what are we celebrating?"* (bucket `celebrate` — clap/applause/celebration)
- *"That's a proper yawn — long morning?"* (label `yawning`)
- *"Big yawn there — didn't sleep much?"* (label `yawning`)
- *"Big yawn at your desk — long day?"* (label `yawning` + `using computer`)
- *"Another big yawn at that screen — long afternoon?"* (label `yawning`, later in the day)
- *"Ngáp rồi kìa — thức khuya hả?"* (label `yawning`, Vietnamese)

After speaking, you are done — no log POST, no extra tool calls, no follow-up question unless something is genuinely off (e.g. 8th drink in an hour). **Exception: the yawn reaction (#1b) DOES post `noted_yawn`** — that marker is the hour cooldown.

## Habit refresh (only when a nudge will fire)

If you decided to nudge AND the context block has `bootstrap_needed=true` → invoke `habit/SKILL.md` Flow A in a separate tool turn to bootstrap `patterns.json` from the multi-day log. Otherwise, **do not load `habit/SKILL.md`** — the `patterns` field in the context block is sufficient (or no patterns yet, that's fine).

Bootstrap is rare (file already exists for active users); the common path is "patterns object present → use it directly".

If the context's `patterns` map has an entry for the action you are about to nudge:
- Match `(action == nudge_target, now within typical_hour:typical_minute ± ~30min)` → weave it into the speech (*"you usually drink around now — everything okay?"*).
- No matching pattern (or `patterns` omitted) → use generic phrasing.

If you decided NOT to nudge (`NO_REPLY`) → never invoke Flow A. Habit bootstrap piggybacks on real nudge events, not idle motion ticks.

Posture nudges (route #5) do NOT trigger habit Flow A — the `patterns` field in `[wellbeing_context]` is keyed on `drink` / `break` reset actions only, and `bootstrap_needed` is computed from the wellbeing log alone.

Example: *hydration nudge fires at 9:15am, patterns.json says drink @ hour=9 typical_minute=10 → "you usually have water around now — grab a glass?"*

## Phrasing (when nudging)

**Talk like a friend, not a wellness app.** Usually use **one short sentence** with one concrete suggestion and at most one brief observation or reason. A second short sentence is allowed when needed to make that suggestion understandable or appropriately qualified. Do not expand merely for variety, explain the routing, or append a recap.

**Weave in a health-context line when it fits** (one short clause, never a lecture):
- **Hydration** → energy / focus / dry-eye / headache. *"…a sip will keep the afternoon fog off."*
- **Break** → eye strain, neck, wrists, blood flow. *"…give your eyes and neck a beat off."*
- **Toilet** → kidneys, bladder. *"…holding it for hours is rough on the kidneys."*
- **Meal** → blood sugar / afternoon energy. *"…so you've got fuel for the rest of the day."*
- **Sleep wind-down** → sleep quality, tomorrow's energy. *"…you'll thank yourself in the morning."*

Use **at most one** health line per nudge. If the same user got the same health line in this session, switch to a different angle (observation, count, time-of-day, playful) instead. Health framing is seasoning, not the dish.

**⛔ Never speak a table row verbatim.** The tables below show **tone** (observation + soft question + optional reason), not a script. Paraphrase every turn — even if the activity is the same as last time. A canned-feeling loop is the exact failure mode this section exists to prevent.

**Variety self-check before speaking:**
- Look at your last 2–3 nudges this session. Different opener? Different angle (observation vs. health vs. count vs. timing vs. playful)? Still brief, actionable and complete?
- If you genuinely can't think of a fresh angle, prefer **shorter and casual** ("Water." / "Up on your feet for a sec.") over recycling a template.

Ground each phrasing in the current raw label from the `Activity detected:` line so the nudge feels observed, not generic.

### Posture tone (see `reference/posture.md`)

Anchor on streak length (rounded — *"nearly an hour"*, never the exact
number), `dominant_region`, one concrete fix, optional health-context
clause. Side prefix only for arm/wrist when L/R sub-scores differ ≥ 2.
Full tone table + health-framing guardrails + praise route + HW marker
shape live in `reference/posture.md`.

### Hydration tone (paraphrase — never copy)

| Raw label | Example tone |
|---|---|
| `using computer` | *"Quick sip of water during your screen time?"* |
| `writing` | *"Pen's been busy — water nearby?"* |
| `texting` | *"Phone's had your attention — got water nearby?"* |
| `reading` | *"Sip of water before the next page?"* |
| `drawing` | *"Water within reach while you draw?"* |
| `playing controller` | *"Keep water within reach when there's a pause in the game?"* |
| (no label) | *"Fancy a sip of water?"* |

### Break tone (paraphrase — never copy)

| Raw label | Example tone |
|---|---|
| `using computer` | *"You've been at the screen a while — look away for twenty seconds?"* |
| `writing` | *"Pause the writing for a short walk?"* |
| `texting` | *"Put the phone down for a moment and stretch your shoulders?"* |
| `reading` | *"Pause your reading and look out the window for a moment?"* |
| `drawing` | *"Pause the drawing and give your hands a short rest?"* |
| `playing controller` | *"After this round, stand up and stretch your legs?"* |
| (no label) | *"You've been sitting a while — fancy a short walk?"* |

### Tired break tone (paraphrase — never copy)

Use **whenever `yawning` is in the labels** and route #7 wins — not only when the yawn is what lowered the threshold. Ground the line in the yawn you actually saw rather than in a duration claim: the standard break lines lean on *"you've been at it a while"*, which is untrue at 20 minutes and generic at three hours. Name the yawn; never name the camera.

| Vibe | Example tone |
|---|---|
| gentle | *"Big yawn — fancy standing up for a couple of minutes?"* |
| practical | *"Big yawn — take a quick break?"* |
| light | *"Yawning at the screen — quick lap around the room?"* |
| direct | *"That's a big yawn — want to take five?"* |

**Say what you saw, not how they feel.** *"That yawn wasn't subtle"* is an observation; *"you're running on fumes"* is a verdict on someone's inner state from a mouth opening on camera. People yawn from boredom, stretching, or because someone near them did — so a diagnosis is a guess, and when it's wrong it's wrong about *them*, which lands worse than being wrong about a number. Suggest the break; don't tell them how tired they are.

Pair it with the sedentary label when one is present (*"yawning at that screen — eyes need it too"*). Do **not** stack this with the standard break table — pick one register.

### Toilet tone (new — paraphrase — never copy)

This only fires after several drinks, so the natural opener is "you've had a fair bit to drink already". Keep it casual — bathroom talk isn't shy, but it isn't a lecture either.

| Vibe | Example tone |
|---|---|
| caring | *"You've had a few drinks — need a bathroom break?"* |
| playful | *"That's {count} drinks — time for a bathroom break?"* |
| straightforward | *"Need a quick bathroom break after those drinks?"* |
| gentle | *"Want to take a bathroom break?"* |

If `count_today.drink` is in context, weave it in concretely (*"that's four drinks already"*). If not, just say "you've drunk a fair bit by now". The kidney/bladder framing is the user's explicit ask — it's OK to mention, just don't say it the same way twice in a row.

### Generic

If multiple sedentary labels are present, pick the one that fits best or blend (e.g. eyes + wrists both deserving a break). Tables are starting points, not scripts — write your own sentence each turn.

## What to write (HW marker — fires async, no tool turn)

Embed at the start of your spoken reply:

```
[HW:/wellbeing/log:{"action":"nudge_hydration","notes":"<your nudge text>","user":"<current_user>"}] <your nudge sentence>
```

Same shape for break (`action="nudge_break"`) and toilet (`action="nudge_toilet"`). The marker:
- Is parsed and stripped by the runtime before TTS speaks the rest of your reply.
- Fires the POST asynchronously in the background; you do NOT wait for the result and there is NO tool turn here.
- Acts as the next reset point for that timer (timeline + delta + drinks-since-toilet computation).

**Posture variant — different endpoint.** Posture nudges target
`/posture/log` (NOT wellbeing log) with `action=nudge_posture` and
`nudge_level: 4`. Praise uses the same path with
`action=praise_posture`. Full marker shape + curl fallback in
`reference/posture.md`.

Do not duplicate backend-logged drink/break/eat/celebrate rows for ordinary **Reaction** replies. The **yawn reaction** is an exception: keep its `noted_yawn` marker to start the existing cooldown. Keep the logs explicitly required by the chosen morning-greeting, sleep-winddown, meal-reminder, or nudge route as well. A silent no-action route needs no nudge marker. Posture uses `/posture/log` for `nudge_posture` / `praise_posture`, not `/wellbeing/log`. The `notes` field is the same sentence you're about to speak — it's what the timeline will display.

**Do NOT use `curl` exec for this log.** That would consume a tool turn (~5-7s LLM-think on the result) for a side-effect that has nothing to wait for. The HW marker path is single-trip.

### Fallback (only if HW marker is rejected by the runtime)

If you see a runtime error parsing `[HW:/wellbeing/log:...]`, fall back to:

```bash
curl -s -X POST http://127.0.0.1:5000/api/wellbeing/log \
  -H 'Content-Type: application/json' \
  -d '{"action":"nudge_hydration","notes":"<your nudge text>","user":"<current_user>"}'
```

Posture fallback uses a different path — see `reference/posture.md`.

## On `presence.enter` / `presence.leave` / `presence.away`

Backend writes the `enter` / `leave` rows. You do nothing for these events — stay silent (`NO_REPLY`) unless there's something genuinely worth saying.

## Action value reference

| Action | Written by | Meaning |
|---|---|---|
| `drink`, `break` | the backend (on `motion.activity`, before event reaches you) | User acted. **Reset point.** |
| `celebrate` | the backend (on `motion.activity`) | User celebrated (clap / applause / celebration), collapsed to the bucket name. Reaction only — **not** a reset point for break/hydration timers. |
| Raw eat labels — `eating burger`, `eating cake`, `eating carrots`, `eating doughnuts`, `eating hotdog`, `eating ice cream`, `eating spaghetti`, `eating watermelon`, `dining`, `tasting food` | the backend (on `motion.activity`) | User ate. Raw labels kept (same hybrid as sedentary) so reaction phrasing can ground in the specific food. **Counts as meal signal** for the meal-reminder gate, **not** as a reset for break/hydration timers. |
| `using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller` | the backend (on `motion.activity`) | Sedentary — logged for timeline + phrasing. `reading` covers book/newspaper (collapsed in HAL). **Not a reset point.** |
| `yawning` | the backend (on `motion.activity`) | Fatigue evidence, kept raw like the sedentary labels. Reacts like drink/eat but throttled to once an hour and only before 21h (route #1b), lowers the break threshold to `BREAK_THRESHOLD_TIRED` (route #7), and confirms sleep-winddown (route #3). **Not a reset point** for the hydration/break timers, and does not count as the day's first activity. |
| `noted_yawn` | **You**, after acknowledging a yawn (route #1b) | Wellbeing log. The only reaction that writes a marker — it arms `yawn_ack_age_min`, which is what stops the next yawn being acknowledged for an hour. |
| `enter`, `leave` | Backend (on `presence.*` events) | Session boundary; deduped against last presence row, so stranger-ID churn collapses. **Reset point.** |
| `nudge_hydration`, `nudge_break` | **You**, after speaking a nudge | Wellbeing log. Timeline + reset for next window. |
| `nudge_toilet` | **You**, after a toilet nudge | Wellbeing log. Resets `drinks_since_toilet_nudge` counter to 0. Sparse by design: only re-fires after another N drinks. |
| `morning_greeting` | **You**, on the morning-greeting route | Wellbeing log. Once-per-day gate; suppresses re-firing today. |
| `sleep_winddown` | **You**, on the sleep-winddown route | Wellbeing log. Once-per-day gate; suppresses re-firing tonight. |
| `meal_reminder` | **You**, on the meal-reminder route | Wellbeing log. Once-per-window gate (lunch / dinner separately). |
| `nudge_posture` | **You**, on the posture-nudge route | **Posture log** (separate JSONL). Carries `nudge_level=4`. HAL's cooldown already gates re-firing for ~30 min — your POST does not by itself reset it. |
| `praise_posture` | **You**, when the user clearly fixed posture after a recent nudge | **Posture log**. Rare — only when context shows improvement following an agent nudge in the last ~30 min. |

Emotional labels (`laughing`, `crying`, `singing`) are filtered upstream and never reach this skill via `motion.activity` — they'll arrive on a separate `motion.emotional` event in a future version. `yawning` is the exception: it is NOT emotional, it's fatigue evidence, and it does reach you as a raw label (see route #7 and the action reference below).
