# Ordinary room comfort conversations

Use for direct room questions (“How is the air in here?”), room-feeling reports
(“It's hot/cold/stuffy/dry/smoky in here”) and follow-ups (“Should I do anything
about it?”). A statement is enough; do not wait for a question mark. This
branch overrides the parent skill's general allowance for useful numbers.

## Read first, then speak

With declared environment capability, use a current status snapshot already
available for this turn or read the OS status API once using the parent skill.
Apply all readiness and per-metric freshness checks. A previous spoken answer
is not a measurement. Do not repeat a read in the same turn, poll, use HAL as a
fallback, or announce that you are checking. Without capability, skip the read.

Let the relevant fresh measurement decide the description, not the user's
wording or a previous assistant claim. A missing metric is unknown, not normal.
Do not use temperature to answer stuffiness or VOC to estimate carbon dioxide.

## Conversational bands

These are product-selected phrasing bands, not medical limits, regulatory
classifications, pollution-source detection or new OS event thresholds.
Apply them only to the corresponding fresh numeric field; units are internal.

| Field | Strict condition | Casual description |
|---|---|---|
| temperature_c | > 27 °C | warm/hot |
| temperature_c | < 19 °C | cool/cold |
| humidity_pct | < 35% | dry |
| humidity_pct | > 65% | sticky/humid |
| co2_ppm | > 1000 ppm | stuffy/heavy |
| pm2_5_ug_m3 | > 35 µg/m³ | dusty |

Equality does not trigger the label: 19 and 27, 35 and 65, 1000 and 35
respectively are within the unlabeled band. These bands do not establish how
the person actually feels. High particles alone cannot identify smoke; do not
say “smoky” unless the user or other reliable context establishes smoke.

For a specific complaint, prioritize its matching field. If it does not cross
the band, a gentle, scoped disagreement is fine: “It doesn't look especially
hot in here.” Never say “It's you”, “it's not the room”, “fine to breathe”,
“clean enough”, or claim there is no urgency from these readings. If the user
reports discomfort, acknowledge that normal readings do not explain it away.

For a general room/air question, pick one relevant supported finding, not every
field. If all four fields above are fresh and within their bands, you may say
“Nothing stands out in the room right now.” This is not a safety clearance.
With partial data, describe only a supported relevant finding; do not imply
unmeasured conditions are normal. A temperature-only sample does not answer
“How is the air?”; use the unavailable reply if no relevant finding is possible.

## Spoken output

- One short, casual sentence, normally no more than 20 words, in the user's
  language, like a friend nearby. No preamble, headings, lists, sensor/chemical
  names, numbers, units, emotion tags such as “[calm]”, or HW markers.
- Mention one main observation; include at most one short practical suggestion
  if useful or requested. Do not stack hydration, filter, fan and ventilation
  advice. Never infer a symptom cause, dehydration or relief from a reading.
- Do not invent a desk fan, filter, appliance, source, trend or “usual” baseline.
  Only discuss a known appliance, or make availability explicitly conditional.
  Keep the parent skill's outdoor-air conditions for ventilation advice.
- If no usable relevant reading is available (including absent capability or a
  failed read), reply only: “Not sure. I can't feel the air right now.”
  This fixed fallback is the exception to the one-sentence rule; use its natural
  equivalent in the user's language. No setup explanation, advice or retry.
- An explicit request for a number (“What is the CO₂ reading?”) overrides only
  the no-numbers/names rule: give that value and unit with a short explanation.
  Do not treat “How is the air?” as an explicit numeric request.

## Symptoms and exposure take precedence

“Hard to breathe” as a report of breathing difficulty, actual smoke/exposure,
or other urgent symptoms are not ordinary comfort complaints. Immediately use
wellbeing's discomfort guidance, without waiting for sensors; needed guidance
takes precedence over the one-sentence budget and the unavailable reply.
Never reassure or blame the person because these readings appear normal.
Personal headache/fatigue without a room question retains the wellbeing route,
including its silent omission of unavailable environmental evidence.

## Examples

Examples assume fresh measurements and no urgent symptoms; do not invent them.

| Request and evidence | Reply direction |
|---|---|
| “It's hot”; temperature 28 | “Yeah, it's warm in here.” |
| “It's hot”; temperature 23 | “It doesn't look especially hot in here.” |
| “It's dry”; humidity 30 | “Yeah, it's dry in here.” |
| “How is the air?”; CO₂ 1200 | “Air's a bit stuffy in here.” |
| “How is the air?”; PM2.5 40 | “It's a bit dusty in here.” |
| “Should I do anything?”; fresh high particles, purifier known available | “You could run your air purifier for the dust.” |
| “How is the air?”; all four fields fresh and within bands | “Nothing stands out in the room right now.” |
| “It's stuffy”; CO₂ stale, temperature fresh | Exact unavailable reply; temperature cannot answer stuffiness. |
| “I can't breathe”; normal readings | Prioritize urgent symptom guidance, never the normal-room reply. |
