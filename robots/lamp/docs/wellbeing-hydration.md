# Proactive Wellbeing (Hydration + Break)

> Lamp proactively reminds the user to drink water and take breaks. The design is **event-driven**: every reminder is decided on an incoming `motion.activity` event from a per-user activity log. There are **no cron jobs** and no reminder timers.
>
> Vietnamese version: [vi/wellbeing-hydration_vi.md](vi/wellbeing-hydration_vi.md)
>
> The full behavioral spec (dedup, attribution, presence markers) lives in [sensing-behavior.md § Wellbeing](sensing-behavior.md). The skill itself is `skills/wellbeing/SKILL.md`.

---

## Overview

| Reminder | Purpose | Threshold (production) | Reset points |
|----------|---------|------------------------|--------------|
| **Hydration** | Remind to drink water | `HYDRATION_THRESHOLD_MIN = 45` | last `drink`, `enter`, `nudge_hydration` |
| **Break** | Remind to stand up / stretch | `BREAK_THRESHOLD_MIN = 30` (`BREAK_THRESHOLD_TIRED = 20` when `yawning` is in the labels) | last `break`, `enter`, `nudge_break` |

Other constants in the skill: `YAWN_ACK_COOLDOWN_MIN = 60`, `TOILET_DRINK_THRESHOLD = 2` (count-based: one toilet nudge per N drinks since the last one).

**Properties:**
- Per-user: each friend has their own timeline under `/root/local/users/{name}/wellbeing/`. Strangers collapse into `"unknown"`.
- Everyone is cared for — friends and strangers alike.
- The user is always taken from the `[context: current_user=X]` tag, never inferred.
- The agent does not always speak — most turns end in `NO_REPLY`.

---

## Flow

### 1. HAL logs activity, then fires the event

```
Camera → activity recognition (MotionPerception)
    ↓
HAL POSTs activity rows to /api/wellbeing/log   (drink / break / yawning /
    ↓                                             sedentary raw labels / eat labels)
HAL fires motion.activity → os-server
    ↓
os-server appends [context: current_user=X] + [wellbeing_context: {...}]
    ↓
Agent: skills/wellbeing/SKILL.md activity router
```

Presence rows (`enter` / `leave`) are written by HAL's face recognizer (`hal/drivers/sensing/perceptions/processors/faceid/perception.py`). HAL dedups activity for 5 minutes per `(current_user, labels)` key (`MOTION_DEDUP_WINDOW_S = 300`), so the agent is woken periodically even when nothing changes.

### 2. Pre-computed context

`BuildWellbeingContext` (`system/skillcontext/wellbeing.go`) injects `[wellbeing_context: {...}]` into every `motion.activity` turn, so the agent fires **no read tool calls**. Key fields: `hydration_delta_min`, `break_delta_min` (`-1` = no reset today), `count_today`, `time_of_day`, `current_hour`, `first_activity_today`, meal-window fields, `yawn_ack_age_min`, `drinks_since_toilet_nudge`, `patterns` (from habit `patterns.json`), `bootstrap_needed`, `last_posture_nudge_age_min`. Long sedentary streaks may also carry `[posture_summary: ...]`.

### 3. Activity router (first match wins, one route per turn)

| # | Condition | Route |
|---|-----------|-------|
| 1 | labels contain `drink`, `break`, `celebrate` or a raw eat label | reaction (one short acknowledgment, no log marker) |
| 1b | `yawning` and yawn cooldown clear, before 21:00 | yawn reaction + `noted_yawn` |
| 2 | first activity today, 05:00–11:00, not greeted yet | morning greeting |
| 3 | after 21:00, sedentary/`yawning`, not wound down yet | sleep wind-down |
| 4 | inside a meal window with no meal signal | meal reminder |
| 5 | `[posture_summary]` present | posture nudge (posture log) |
| 6 | `hydration_delta_min >= 45` | hydration nudge + `nudge_hydration` |
| 7 | `break_delta_min >= 30` (or `>= 20` with `yawning`) | break nudge + `nudge_break` |
| 8 | `drinks_since_toilet_nudge >= 2` | toilet nudge + `nudge_toilet` |
| 9 | anything else | `NO_REPLY` |

The agent logs nudges with an inline marker, e.g. `[HW:/wellbeing/log:{"action":"nudge_hydration","notes":"...","user":"<current_user>"}]` → `POST /api/wellbeing/log`. The `nudge_*` row is the next reset point, so the same reminder only fires again after another full threshold window — no separate cooldown.

```
10:45  hydration overdue → nudge 💧 + log nudge_hydration → hydration delta = 0
11:20  wake-up → delta = 35 min < 45 → NO_REPLY
11:30  wake-up → delta = 45 min ≥ 45 → nudge 💧 again (user still hasn't drunk)
```

If the user drinks or takes a break in between, HAL's `drink` / `break` row resets the delta.

### 4. Habit enrichment

When a hydration/break nudge fires and `bootstrap_needed=true`, the skill invokes habit Flow A to build `patterns.json`; patterns then enrich the nudge phrasing ("you usually have water around now"). See [habit-tracking.md](habit-tracking.md).

### 5. Presence leave / away

HAL writes the `leave` row. There is nothing to cancel — the agent stays quiet (`NO_REPLY`).

---

## Per-user data

```
/root/local/users/{name}/
  ├── wellbeing/2026-04-10.jsonl   ← activity + nudge timeline (30-day retention)
  ├── posture/2026-04-10.jsonl     ← posture nudges/praise
  ├── mood/2026-04-10.jsonl        ← mood history
  └── habit/patterns.json          ← learned habit patterns
```

Row format: `{"ts": 1776658657.23, "seq": 42, "hour": 11, "action": "sedentary", "notes": ""}`.

Agent-written actions: `nudge_hydration`, `nudge_break`, `nudge_toilet`, `morning_greeting`, `sleep_winddown`, `meal_reminder`, `noted_yawn` (wellbeing log), `nudge_posture`, `praise_posture` (posture log). Everything else is written by HAL.

---

## Layers and files

| File | Role |
|------|------|
| `skills/wellbeing/SKILL.md` (+ `reference/*.md`) | Activity router, thresholds, phrasing, logging rules |
| `skills/habit/SKILL.md` | Flow A pattern build used to enrich nudges |
| `system/skillcontext/wellbeing/wellbeing.go` | Per-user JSONL logger, presence dedup, 30-day retention |
| `system/skillcontext/wellbeing.go` | `BuildWellbeingContext` — the `[wellbeing_context]` block |
| `system/lib/sensingmsg/sensingmsg.go` | Appends the context to `motion.activity` messages |
| `system/server/server.go` | `POST /api/wellbeing/log`, `POST /api/posture/log`, `GET /api/agent/wellbeing-history` |
| `hal/drivers/sensing/perceptions/processors/motion.py` | Activity recognition, 5-min dedup, posts activity rows |
| `hal/drivers/sensing/perceptions/processors/faceid/perception.py` | Posts `enter` / `leave` rows |
| `system/web/src/pages/monitor/UserTimelineModal.tsx` | Per-user timeline (wellbeing, mood, music suggestions, posture) |

---

## How to test

Prerequisites: os-server (port 5000), HAL (port 5001) with a working camera, agent connected.

1. **Hydration nudge** — sit at the computer without drinking. After 45 min (or temporarily lower `HYDRATION_THRESHOLD_MIN` in the skill), the next `motion.activity` produces one hydration sentence and a `nudge_hydration` row.
2. **Reaction** — drink in front of the camera. HAL logs `drink`; the agent answers with a short acknowledgment and writes no row.
3. **No re-nudge spam** — after a nudge, subsequent events within the window end in `NO_REPLY`.
4. **Multi-user** — two friends get separate `/root/local/users/<name>/wellbeing/` timelines; strangers go to `unknown`.

Restore production thresholds after testing.

```bash
# Timeline for today (admin-gated)
curl -s -H "Authorization: Bearer <admin-token>" \
  "http://<LAMP_IP>:5000/api/agent/wellbeing-history?user=<name>&last=50"

# On the device
cat /root/local/users/<name>/wellbeing/$(date +%Y-%m-%d).jsonl
journalctl -u os-server | grep -i "wellbeing"
```

---

## History: the cron-based design (retired)

An earlier version (April 2026) had the agent create two OpenClaw cron jobs per user (`Wellbeing: {name} hydration` every 45 min, `... break` every 30 min) when a sedentary activity was seen, reset them with `cron.remove` / `cron.add` when the user drank or stretched, judge each fire from a camera snapshot, cancel them on `presence.leave`, and keep a free-text `wellbeing.md` notebook. Before that, a hard-coded Python `WellbeingPerception` timer fired `wellbeing.hydration` / `wellbeing.break` events. Both were replaced by the event-driven design above: no crons to leak across reboots, deterministic deltas computed from the log, and no per-fire snapshot cost.
