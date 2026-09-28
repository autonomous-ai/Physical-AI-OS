# Skills

A skill is one folder with one `SKILL.md` inside. Two front-matter keys, then markdown telling the agent what to do and when — the same format OpenClaw and Claude skills use, so a markdown skill you already have drops in as-is (if it shells out to a CLI, that CLI has to be on the robot too). A *robot* skill is one that also writes `[HW:/…]` markers or calls HAL.

## Writing one

Here is the top of `guard`'s, trimmed:

```markdown
---
name: guard
description: Guard mode for security monitoring. Toggle on/off when a friend says "guard mode", "watch the house", "I'm going out" ...
---
# Guard Mode
1. Reply with `[HW:/emotion:{"emotion":"acknowledge","intensity":0.7}]` — the device nods and flashes green.
2. Enable guard mode: `curl -s -X POST http://127.0.0.1:5000/api/guard/enable`
3. Confirm verbally: "Guard mode on. I'll keep watch."
...
```

The `[HW:/path:{json}]` marker is the grammar: `{json}` is optional (`[HW:/led/off]` is fine), and the markdown-link mangling LLMs produce (`[Lights off](HW:/led/off)`) is rewritten to the canonical form — two regexes plus a normalizer in [`handler_hw.go`](../system/server/agent/delivery/http/handler_hw.go). Each skill maps to the capabilities it needs ([`system/skills/skills.go`](../system/skills/skills.go)), so the same file runs on any body that declares them.

## Putting one on your robot

```bash
make push-skill SKILL=./my-skill TARGET=pi@lamp-xxxx.local   # live on the next conversation, no reboot
make push-skill SKILL=./my-skill TARGET=pi@10.0.0.5 J=proxy-host   # via an SSH jump host
```

Or type what you want in the app, or tap one in the Skill Store. On the robot, skills live in `/root/.openclaw/workspace/skills/<name>/` — the same folder the agent engine already reads. No PR, no reboot, no Go.

## Shipping one to every robot

1. Drop the folder in `skills/<name>/`. If it needs hardware, add `skill.json`: `{"capabilities": ["motion"]}` — any of the [capability names](../robots/contract/capabilities.md), ANY-OF.
2. `python skills/skill-creator/scripts/quick_validate.py skills/<name>` checks the format; `make skills-catalog` regenerates the catalog from the tree and `go test ./system/skills/` fails if you forget.
3. Open the PR. On merge, [`publish-skills.yml`](../.github/workflows/publish-skills.yml) uploads the feed for you; every body's skill watcher pulls it within 5 min and tells the agent to re-read. `make upload-skills` still works by hand for an out-of-band push.

One folder, one PR, every robot — no maintainer in the middle. [`skill-creator`](skill-creator/) also ships an eval loop — with-skill vs baseline runs, a grader, a description optimizer — so you can measure a skill before you publish it.


## Catalog

This catalog assigns every platform skill one primary category and one or more
search tags. Categories are intended for store navigation; tags let a skill be
found in multiple relevant contexts without duplicating it across categories.

System-only skills may be hidden from the default storefront or shown with a
`System` badge.

| Skill | Primary category | Tags | Compatible devices |
| --- | --- | --- | --- |
| `audio` | Utilities | speaker, microphone, volume, hardware | Lamp, Intern v2, Reachy Mini |
| `music` | Entertainment | youtube, playback, speaker | Lamp, Intern v2, Reachy Mini |
| `music-suggestion` | Entertainment | proactive, mood, recommendation | Lamp, Intern v2, Reachy Mini |
| `voice` | Communication | tts, mute, privacy, microphone | Lamp, Intern v2, Reachy Mini |
| `camera` | Camera & Vision | snapshot, streaming, privacy, vision | Lamp, Reachy Mini |
| `face-enroll` | Camera & Vision | face-recognition, identity, presence | Lamp, Reachy Mini |
| `user-emotion-detection` | Health | emotion, speech-emotion, mood, sensing | Lamp, Intern v2, Reachy Mini |
| `speaker-recognizer` | Communication | voice-id, speaker-recognition, identity | Lamp, Intern v2, Reachy Mini |
| `display` | Home | lcd, eyes, expression, hardware | No current device |
| `emotion` | Home | personality, expression, led, servo, display | Lamp, Reachy Mini |
| `led-control` | Home | lighting, rgb, effects, smart-home | Lamp, Intern v2 |
| `scene` | Home | lighting, ambiance, focus, relax, smart-home | Lamp, Intern v2 |
| `servo-control` | Home | motion, aiming, gestures, hardware | Lamp, Reachy Mini |
| `servo-tracking` | Camera & Vision | vision-tracking, object-tracking, motion | Lamp, Reachy Mini |
| `sensing` | Home | presence, sound, light, fire-safety, events | Lamp, Intern v2, Reachy Mini |
| `environment` | Home | air-quality, temperature, humidity, wellbeing, events | Devices declaring environment (Lamp optional, disabled by default) |
| `sensing-track` | Home | history, logs, motion, presence | Lamp, Intern v2, Reachy Mini |
| `skill-creator` | Productivity | create, test, evaluate, package, publish | Lamp, Intern v2, Reachy Mini |
| `guard` | Safety | monitoring, presence, alerts, smart-home | Lamp, Reachy Mini |
| `wellbeing` | Health | posture, hydration, breaks, coaching | Lamp, Intern v2, Reachy Mini |
| `habit` | Health | routines, behavior, personalization | Lamp, Intern v2, Reachy Mini |
| `mood` | Health | emotion, user-state, personalization | Lamp, Intern v2, Reachy Mini |
| `computer-use` | Productivity | macos, browser, desktop, companion | Lamp, Intern v2, Reachy Mini |
| `claude-buddy` | Productivity | claude-code, approvals, companion, agent | Lamp, Intern v2, Reachy Mini |
| `harness-use` | Productivity | harness, coding-agent, research, delegation — app: <https://github.com/autonomous-ai/openharness> | Lamp, Intern v2, Reachy Mini |
| `connectors` | Productivity | gmail, calendar, drive, notion, github | Lamp, Intern v2, Reachy Mini |
| `faq` | Productivity | local-web, dashboard, settings, help | Lamp, Intern v2, Reachy Mini |
| `input-branching` | Not published | routing, realtime, internal | Lamp, Intern v2, Reachy Mini |

## Adding a skill

A skill is one folder: `skills/<name>/SKILL.md`. If it needs hardware, add a
sidecar next to it —

```json
{ "name": "guard", "capabilities": ["presence"] }
```

`capabilities` is ANY-OF: the skill installs on any body whose `ROBOT.md`
declares at least one of them (the names are in
`robots/contract/capabilities.md`). No sidecar means a platform skill with no
hardware dependency — it installs everywhere.

The tree is the catalog. `make skills-catalog` regenerates
`system/skills/catalog_gen.go` and the `skill_caps()` block in
`scripts/provision/setup.sh` from these folders; `go test ./system/skills/` and
`make skills-catalog-check` fail if the generated files drift from the tree.

Compatibility is the automatic built-in installation gate from
`system/skills.Capability`: a skill with no entry is platform logic and runs on
every current device. It does not describe optional integrations; for example,
`computer-use` additionally requires an owner-paired Mac before it can act.

## Per-body notes

Why some cells in the README's capability table are blank or ○ (declared in
`ROBOT.md`, driver not landed):

- **Intern** — no camera, so Sense is sound-only, Mood is voice-only, and
  Look-after-you is breaks and habits, not posture. Glow is colors and effects,
  not the six scenes: its `light` declares `led`, not `scene`.
- **Reachy Mini** — the tracker (`servo-tracking`) still speaks Lamp's joint
  names, so it installs but does not track yet. Emotion moves are verified on
  hardware; `aim` directions are still being tuned. HAL plays moves through the
  SDK's `no_media` client while it owns the speaker, so each move's sound clip
  is dropped for now.
- **Go2-W** — no board entry yet, so HAL will not boot on it until the port
  lands. Its `motion` routes are `locomotion`, not `servo`, so `servo-control`
  and `servo-tracking` would install and have nothing to call — blank in the
  table; the `motion.drive` sub-capability under Not built yet keeps them off
  rolling bodies. `claude-buddy` would install (it only needs `audio`);
  `computer-use` needs `companion`, which Go2-W does not declare.

## Categories

```text
Home
Health
Entertainment
Productivity
Safety
Camera & Vision
Communication
Utilities
```

`input-branching` is routing infrastructure rather than a user-installable
feature, so it has no Store category and must stay out of the storefront.
`claude-buddy` cannot be published under the current Store contract because its
slug contains the reserved word `claude`.
