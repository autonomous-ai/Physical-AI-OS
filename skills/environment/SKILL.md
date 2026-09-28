---
name: environment
description: Interpret environmental sensor readings and sustained changes for room comfort and air quality. Use for [environment:initial] greeting context, [environment:update] events, questions about room temperature, humidity, measured CO2 or air quality, checking changes after ventilation or air cleaning, and room-feeling reports such as hot, cold, stuffy, dry or smoky, including discomfort follow-ups with the capability declared. Requires the environment capability. Link to wellbeing for considerate proactive advice; do not diagnose health conditions or infer CO2 or oxygen from other measurements.
---

# Environment

## Scope and data

Work from the device's declared `environment` capability and the measurements actually present. Do not require a camera, microphone, emotion marker, or a particular sensor model. If the capability is absent, skip environmental checks; ordinary room questions and room-feeling reports use the fixed unavailable reply in reference/room-comfort.md. Personal symptoms without a room question retain wellbeing support without sensor-error commentary. Do not enable hardware or edit its declaration.

For a direct room question or room-feeling report (even without a question), a requested current comparison, or a non-urgent wellbeing concern when the capability is declared, read the OS environment status API once (reuse a supplied current status snapshot when available):

```bash
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:5000/api/environment/status
```

This read-only route admits local device callers and checks the environment capability. It returns the OS envelope `{"status":1,"data":{...HAL snapshot...},"message":null}`; inspect `data`. A non-2xx response or `status:0` means unavailable. Do not bypass a denial through HAL or fetch credentials for the browser's authenticated hardware proxy.

Use the existing snapshot in an automatic event unless it is stale or the user explicitly requests a fresh check. Do not run a polling loop, start a cron job, invent a history endpoint, or promise a timed follow-up: OS owns polling, thresholds, sustained-change detection and cooldowns.

A usable snapshot has `enabled: true`, `state: "ready"`, `stale: false`, and at least one fresh numeric measurement in `sample`. The shared status sample always contains nine nullable metric keys; an object of null values is not usable data. Check `age_s` and `sample.timestamp` before describing it as current. Disabled, starting, stopped, error, missing or stale data is unavailable, not zero pollution. Say so briefly on an explicit room-data question; stay silent on an unusable automatic event. For a personal wellbeing concern without a room question, silently skip unavailable environmental data and continue support without sensor-error commentary. Ordinary room questions/reports use the unavailable reply in reference/room-comfort.md. Individual null or absent measurements are unknown: retain the other valid readings. For a multi-component snapshot, `sources[metric]` identifies its entry in `components`; check that component's state, age and stale flag, plus `metric_timestamps[metric]`. The aggregate timestamp may belong to another sensor and does not establish freshness for every field. One failed component does not invalidate another healthy component.

| Sample field | Meaning |
|---|---|
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Particle mass concentrations, µg/m³ |
| `temperature_c` | Temperature near the installed sensor, °C |
| `humidity_pct` | Relative humidity, % |
| `voc_index`, `nox_index` | Relative gas indices, not gas concentrations |
| `co2_ppm` | Measured carbon dioxide concentration, ppm, when a CO₂ component is available |

Interpret the available metric, independently of the component model. Unsupported metrics are null, just like currently unavailable readings; use component state and `sources` for diagnostics, not the null alone. A healthy component without VOC/NOx is normal and does not require waiting for those indices or reporting a fault. Missing CO₂ does not prevent using valid PM, temperature or humidity. CO₂ is distinct from CO and O₂; the supported measurements do not include those gases. Use CO₂ only when its own reading is fresh, never estimate it from VOC/NOx. Do not infer oxygen shortage, impaired cognition, a gas leak, a fire or a medical condition from environmental readings.

VOC Index adapts to recent history (roughly 24 hours, baseline 100); NOx Index uses a different baseline (1). Neither baseline certifies safe air. Do not identify a chemical, smell or pollution source from either index. Describe an increase relative to previous readings. Do not convert indices into ppm or apply concentration guidelines to them. Instantaneous PM readings cannot establish compliance with 24-hour or annual WHO exposure guidelines. Sensor temperature may be affected by the enclosure and nearby electronics; do not present it as body temperature or a guaranteed room-wide measurement.

## Event contract

`[environment:update]` is followed by an OS-generated JSON object containing
`observed_at` (Unix seconds), `sample` (measurements and timestamp), `changes`
(keyed by metric, each with `previous`, `previous_at`, `current`, `current_at`, `delta` and optional `source`), `sustained_s`, and per-metric `sources` / `metric_timestamps`. Older events may omit the added provenance fields.
Use the numeric change facts rather than guessing a trend from one raw sample.
Do not assume the event has the full status envelope or all metrics. A delayed
event describes its observation time, not necessarily the current room; obtain
current status before giving time-sensitive advice. `previous` is the detector's
previous acknowledged baseline and `previous_at` is its Unix timestamp;
`current_at` is that metric's observation time; `observed_at` is the newest valid reading in the snapshot and may belong to a different component. For older single-sensor events without `current_at`, use `observed_at`. `current` is the latest sample, not a rolling
average. `sustained_s` describes consecutive qualifying readings, not a health
exposure assessment. Missing context does not
justify invented thresholds, durations, health classifications or user activity.

A normal update can also contain a `comfort` map for sustained room conditions.
Each metric carries `state` (`high`, `low` or `recovered`), `previous_state`,
`current`, `current_at`, `threshold`, `sustained_s` and optional `source`.
An event with `changes: {}` and nonempty `comfort` is a valid sustained-condition
event, not an initial report or a missing trend. Its per-metric duration is
evidence of a persistent condition, not an exposure average. Use its measured
state and the room-comfort reference to choose one useful, short observation
and at most one action; no numbers, sensor names or spoken tags by default.
Do not diagnose symptoms, claim WHO/AQI exceedance, or equate `recovered` with
safe air or the person's recovery. A recovery acknowledgment is useful mainly
after a relevant concern/action; otherwise silence may be appropriate.
The OS owns persistence, hysteresis and repeat suppression; do not add timers.

An initial report has `reason: "initial"`, `changes: {}`, and only the metrics
that have passed OS freshness and warm-up checks. It is either embedded in the
startup greeting under `[environment:initial]`, or sent after the greeting as
`[environment:update]`. It is a first observation, not a significant-change
alert: empty `changes` is expected. Other metrics may still be warming up.

## Discomfort and ventilation context

For ordinary room-feeling reports or room questions, first apply
[reference/room-comfort.md](reference/room-comfort.md), including when the user
says it is hot, cold, stuffy, dry or smoky. Actual breathing difficulty or
reported smoke/exposure follows the urgent guidance in wellbeing's discomfort
reference; do not wait for sensors or dismiss symptoms based on normal values.

When the user says they feel tired, headachy, dizzy, stuffy or unable to focus,
consult `skills/wellbeing/reference/discomfort.md` for the response. Apply it
here without handing the turn back through the wellbeing activity router.
Do not inspect sensors before responding to an already apparent urgent symptom
or reported exposure. Otherwise, when the capability is declared, you MUST use a supplied current
status snapshot or one bounded status read before completing the reply.
The check is required; the room-comfort branch controls its short reply. For
personal symptoms without a room question, a failed read, all-null sample, stale
data or absent metric means omit that environmental explanation, not invent a
substitute.

Separate three facts: how the user says they feel, what the sensor measured,
and which action might improve comfort. An increase is not itself a high or
harmful concentration. Two event endpoints or `sustained_s` do not establish a
long-term concentration average. For context only, HSE workplace guidance
flags CO₂ consistently above 1500 ppm in occupied rooms as a reason to improve
ventilation. This is not a symptom-causation threshold, universal home safety
boundary, or a new OS alarm rule; do not claim consistency from one sample.
See [HSE: using CO₂ monitors](https://www.hse.gov.uk/ventilation/using-co2-monitors.htm)
(reviewed 2026-09-14). Treat a lone value as a current observation, not a trend, and use
available context before suggesting action. Readings can be affected by
placement, nearby breath and calibration; do not label the room safe from a
low CO₂ value or diagnose poisoning from a high one.

For rising measured CO₂ with a useful ventilation opportunity, suggest one
practical option: an outside-facing opening when outdoor air/weather allow,
a fresh-air ventilation setting, or moving to a more comfortable ventilated
space. An interior door alone may not bring in outdoor air. Do not invent
outdoor air quality from indoor PM or fetch unrelated services to manufacture
certainty. If outdoor smoke/pollution or unsafe access makes opening a window
unsuitable, choose another option. Recirculation/HEPA is not CO₂ removal.
[HSE: improving ventilation](https://www.hse.gov.uk/ventilation/how-to-improve-ventilation.htm).

## Choose the response

For ordinary direct room questions/reports and their follow-ups, the
room-comfort reference below takes precedence over the general phrasing rules
in this section (including the allowance for useful numbers).
Do not speak a preamble about checking sensors; complete the bounded check
before the user-facing reply.

**Explain meaning before numbers.** Outside the stricter direct room-comfort
branch, for greetings, automatic updates and discomfort support, lead with one plain-language
observation about what the fresh evidence means for comfort or a practical
decision. Do not default to reciting a sensor inventory, units or gas indices. Omit numbers
by default; include relevant values when the user asks for readings or when a
value materially helps explain a decision, and explain its meaning alongside it.
Use at most one practical suggestion when warranted; do not manufacture an
action just to fill the reply.

Plain language must preserve the same evidence limits as numeric reporting.
A measured increase supports “particles have increased”, not “the air is bad”.
Use only the conversational comfort bands in reference/room-comfort.md; do not
invent other thresholds or label the whole room healthy, safe, clean or well
ventilated from partial readings. With temperature alone, describe
only temperature; it cannot answer whether the air is clean. If the evidence
does not support a useful interpretation, briefly explain that limit on a
direct room question rather than filling the answer with numbers. For automatic
updates or startup context, keep the silence/omission rules below. Use the measurement and ventilation
rules above for any interpretation; hiding the number does not relax them.

1. **Direct room-status question, room-feeling report or follow-up:** read [reference/room-comfort.md](reference/room-comfort.md) and apply its stricter output contract: check relevant fresh data first, then one short casual sentence, no numbers, units, sensor names or tags. It also defines the exact unavailable reply and explicit-number exception. Do not turn the answer into a multi-condition report or advice list. A single snapshot cannot establish a trend or that the whole room is safe.
2. **Initial report (`reason: "initial"`):** use the supplied snapshot to add at most one short plain-language factual observation supported by the supplied readings; omit numbers by default. In `[environment:initial]` greeting context, preserve the normal greeting and do not create another turn or fetch/wait for sensor data. Without usable greeting context, just greet normally. For a separate initial update, skip a second greeting; a simple observation can be useful even without a change or advice. If delayed data is no longer current, omit it rather than refresh or poll for this startup report. Respect quiet/sleep preferences with `NO_REPLY` for a separate update; in a greeting omit only the environmental sentence. Do not claim improvement, a trend, safety, or health effects from this first snapshot. OS owns one-time delivery and retries; do not schedule another report for missing or warming metrics.
3. **Automatic changed or sustained-condition `[environment:update]`:** use the supplied `changes` and/or `comfort` facts. A significant change means OS's configurable change policy fired; it is not automatically a harmful level. Consider whether the change merits action using the environmental-care section of `skills/wellbeing/SKILL.md`. If there is no useful new advice, output exactly `NO_REPLY`.
4. **After an action:** compare a fresh value with an actual earlier, timestamped value in the event or conversation. State the observed direction without claiming causation: “Particles are lower than before.” If there is no comparison point, say that a change cannot yet be established. A later significant-change event may support an update, but no event is a guarantee of scheduled follow-up.

Do not route environmental updates to sensing's camera/presence reaction matrix or guard alerts, even while guard mode is active. No mandatory emotion, servo, light, camera or speech action. An environmental event alone never establishes a person’s presence or authorizes a greeting; startup context only supplements an already requested system greeting. Process only this event; it does not authorize resuming an unrelated task.

## Useful, proportionate advice

- Increased particles: suggest checking an identifiable source or using an available particle filter. Do not assume cooking, smoking, occupancy or a fire without supporting context.
- Measured CO₂ increase: explain the supported change in plain language, with a value only when useful or requested; suggest checking ventilation when outside conditions allow. A recirculating fan or HEPA particle filter does not lower CO₂ by itself. The configured CO₂ delta is a change trigger, not a health limit; do not claim a cognitive effect or diagnose symptoms from a reading.
- Gas-index changes: suggest checking recent activities or sources; ventilation may help when outdoor conditions allow. A HEPA particle filter does not remove every gas.
- Temperature or humidity: offer a modest comfort adjustment when supported by readings and user preferences. Do not diagnose dehydration, illness, sleep quality or productivity from the sensor.
- Do not blindly recommend opening a window when outside pollution or weather is unknown. Phrase that condition explicitly when relevant.
- Recommend actions; operate a fan, purifier, humidifier or HVAC only through an available integration under the user's authorization. Do not invent actuator APIs or promise devices that are not connected.
- Respect requests to stop reminders. Do not change detection policy or disable the sensor merely because a reading is inconvenient.

## Coordination with wellbeing

Read `skills/wellbeing/SKILL.md`'s **Environmental care** section for proactive phrasing and timing. Environment owns measurement interpretation; wellbeing contributes the user's preferences and available activity context. This is a one-way consultation of that section, not a handoff back through its activity router; do not bounce between the two skills. No activity logs, camera observations, identity or hydration/break counters are required to answer a room question. Do not fabricate a wellbeing context block or log an unsupported `nudge_environment` action.

When speaking proactively, offer one useful observation and at most one action in the user's language. Keep analysis and skill names out of the spoken reply. Silence is the literal `NO_REPLY`, without an explanation or hardware markers.


## Example decisions

| Input | Response direction |
|---|---|
| “How is the room?”; temperature valid, VOC null | Use the room-comfort reference for a short supported temperature observation; missing VOC is not a fault. Temperature alone cannot answer a question about air quality. |
| “How is the air?”; fresh particle increase versus an earlier reading | Apply the room-comfort bands; only say “more than before” if the timestamped comparison supports it, and never call the air safe. |
| “What is the CO₂ reading?”; fresh measured CO₂ | Give the value and ppm, with a brief supported explanation of its ventilation relevance; no symptom-cause or safety claim. |
| Cold boot; greeting has no environmental context | Greet normally without waiting, checking the API, or claiming missing readings are zero. |
| Warm HAL; greeting includes initial temperature and CO₂ | Add at most one factual sentence using supplied fresh readings; no second notification. |
| First update has `reason: "initial"`, `changes: {}`, temperature only | Briefly describe supported temperature comfort when appropriate, without defaulting to a numeric readout; do not require a delta or wait for VOC/NOx. |
| Delayed initial update; readings are stale or unavailable | Omit the environmental report (`NO_REPLY` for a separate update); do not fetch or promise a retry. |
| “I feel tired and have a headache.” | Use wellbeing’s discomfort reference. With declared environment capability, use a current snapshot or read status once; optionally add one relevant fresh observation. Without capability or usable data, continue ordinary support. Never assign a cause from sensor data. |
| CO₂ component stale; PM fresh | Report PM if useful; CO₂ is unavailable. Do not reuse the aggregate timestamp to call CO₂ current. |
| CO₂ rises after a particle purifier starts | Explain that particle filtration does not address CO₂; consider ventilation conditionally, without assuming occupancy or health effects. |
| VOC Index returns to 100 | Describe the relative change if relevant; never say pollution has cleared or air is safe. |
| Automatic change while context says the user is asleep or wants quiet | `NO_REPLY`; do not wake them, trigger guard mode or emit an emotion marker. |
| “I turned the purifier on; is it helping?” | Read current status and compare with a real earlier value if available. Without one, explain that improvement cannot yet be established; add a supported current observation only if useful, not a default numeric readout. Do not promise a timed recheck. |
| Delayed event or stale status | Do not describe old measurements as current. A failed fresh check means current conditions are unknown. |
