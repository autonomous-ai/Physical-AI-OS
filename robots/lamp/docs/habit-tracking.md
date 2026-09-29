# Habit Tracking

Habit tracking adds **predictive behavior** to Lamp's wellbeing and music systems. Instead of only reacting to events (threshold nudges, mood-based music), Lamp learns personal patterns over time and acts proactively.

## How It Works

```
Data sources (input)                  Habit skill                    Consumers (output)
─────────────────────                ─────────────                  ──────────────────
Wellbeing logs (sensing)  ──┐                                      Wellbeing Step 3b
  drink, break, enter,      ├──→  Flow A: build patterns  ──→      (enrich nudge
  leave, sedentary          │      (invoked on nudge,                phrasing)
                            │       self-throttled <6h)
                            │       ↓
                            │    patterns.json               ──→  Music-suggestion
SOUL (conversation)     ──┘       per user                        (preferred genre)
  meal, coffee, sleep,
  exercise
```

**Flow A trigger:** wellbeing's Step 3b invokes Flow A only when Step 3 fires a threshold nudge (real behavioral inflection). Flow A self-throttles via mtime check — if `patterns.json` is fresh (<6h), it returns immediately without recomputing. Idle `motion.activity` ticks never trigger a rebuild.

## Data Sources

Two independent inputs feed into the same wellbeing JSONL logs:

### 1. Sensing data (via Wellbeing skill)
Camera detects physical actions → HAL logs to wellbeing JSONL automatically.

| Action | Source |
|--------|--------|
| `drink` | Camera activity detection |
| `break` | Camera activity detection |
| `celebrate` | Camera activity detection |
| `using computer`, `writing`, `reading`, `texting`, `drawing` | Camera sedentary detection |
| `enter` / `leave` | Presence detection (backend) |

### 2. Conversation intent (via SOUL)
User mentions daily activity in conversation → Lamp silently logs to wellbeing JSONL.

| User says | Action logged |
|-----------|---------------|
| "going to lunch", "dinner" | `meal` |
| "coffee break", "grab a coffee" | `coffee` |
| "good night", "going to sleep" | `sleep` |
| "gym", "workout", "going for a run" | `exercise` |

**Rule:** Only logs when user states intent NOW — not past tense or general discussion. Logging is silent; Lamp responds naturally without mentioning it.

## Pattern Building (Flow A)

The habit skill reads 14–30 days of wellbeing JSONL and computes patterns (pattern emission still gates on `days_observed ≥ 3`, so day-4 users get early signals — the wider window deepens accuracy as data accumulates):

1. **Group** events by `(action, hour)` across all days
2. **Count** frequency: `days_appeared / days_observed`
3. **Compute** typical minute (median of minute values at that hour)
4. **Assign** strength: weak (<0.5), moderate (0.5–0.75), strong (>0.75)
5. **Write** results to `patterns.json`

### Minimum data requirements

| Purpose | Min days | Min occurrences |
|---------|----------|-----------------|
| Habit detection | 3 | 2 |
| Proactive nudging | 5 | 3 |
| Music personalization | 3 | 2 accepted |

## Storage

Per-user file:
```
/root/local/users/{name}/habit/patterns.json
```

Rebuilt when:
- File does not exist
- File is older than 6 hours
- User explicitly asks about their habits

### Example patterns.json

```json
{
  "updated_at": "2026-04-22T10:01:00",
  "days_observed": 3,
  "wellbeing_patterns": [
    {
      "action": "meal",
      "typical_hour": 9,
      "typical_minute": 30,
      "window_minutes": 45,
      "frequency": 0.67,
      "days_observed": 3,
      "strength": "moderate"
    },
    {
      "action": "enter",
      "typical_hour": 8,
      "typical_minute": 30,
      "window_minutes": 45,
      "frequency": 0.67,
      "days_observed": 3,
      "strength": "moderate"
    }
  ],
  "music_patterns": [
    {
      "preferred_genre": "lofi hip hop",
      "peak_hour": 14,
      "acceptance_rate": 0.8,
      "days_observed": 5
    }
  ]
}
```

## Consumers

### Wellbeing — habit-aware nudge phrasing (Step 3b)

When Step 3's threshold check fires a nudge (drink > 45 min? break > 30 min?), wellbeing invokes habit Flow A. Flow A self-throttles (no-op if `patterns.json` < 6h old; bootstraps if missing and ≥3 days of data exist). The returned `wellbeing_patterns` are then used to enrich the nudge phrasing:

1. Is the nudge action a moderate+ habit (`frequency ≥ 0.5`)?
2. Is `now` within `typical_hour:typical_minute ± window_minutes` for that habit?
3. If yes → weave habit context into the speech (*"you usually drink around now — everything okay?"*)
4. Otherwise → use the generic phrasing table

There is no separate habit-only nudge — habit acts as a phrasing enricher on the threshold nudge, not a second trigger. This avoids double-nudging and keeps Flow A's bootstrap cost on the rare nudge path, not on every `motion.activity` tick.

**Example:** Leo's hydration timer crosses threshold at 9:15. Flow A returns `drink @ hour=9 typical_minute=10 strength=moderate`. Lamp speaks *"you usually have water around now — grab a glass?"* instead of the generic *"been a while — grab some water?"*.

### Music-suggestion — personal genre preference (Flow C)

Before picking a genre from the default mood table, music-suggestion reads `patterns.json → music_patterns`:

- If current hour matches `peak_hour ± 1` → use `preferred_genre`
- Otherwise → fall back to default genre table

**Example:** Leo usually accepts lo-fi between 14:00–16:00 → at 14:00, suggest lo-fi instead of generic mood-based pick.

## Open Habit Questions (Flow E)

When the user explicitly asks about a person's routines (*"What are Leo's habits?"*, *"Notice anything about my patterns?"*), the habit skill runs Flow A first, then picks one of three reply modes based on what Flow A returned:

| Flow A returned | Reply mode | What Lamp says |
|---|---|---|
| `days_observed ≥ 3` AND ≥1 moderate/strong pattern | **Pattern** | Names 2–3 strongest patterns with hour + frequency phrasing |
| `insufficient_data` OR all weak OR <2 patterns | **Narrative** | Reads raw `wellbeing/*.jsonl` last 7 days and describes concrete activity (dates/hours/actions) — ends with an honest line that it's not enough to call a habit yet |
| `insufficient_data` AND existing `patterns.json` mtime > 3 days old | **Honest-gap** | Acknowledges the data gap, refuses to recite stale patterns as current |

The honest-gap mode exists because Flow A's freshness guard preserves stale `patterns.json` even when current data is insufficient. Without this rule, Lamp would happily recite a 2-week-old pattern file as if it described today.

Flow E **overrides** the one-sentence OUTPUT RULE that governs nudge enrichment: 2–4 sentences are allowed, and concrete dates/hours/approximate counts are permitted in the spoken reply. Raw timestamps, JSON, and internal pattern math still stay in `thinking`.

## Window Sizes

| Action | Window |
|--------|--------|
| `drink` | ±30 min |
| `break` | ±30 min |
| `meal` | ±45 min |
| `coffee` | ±30 min |
| `sleep` | ±30 min |
| `exercise` | ±60 min |
| `enter` (arrival) | ±45 min |
| Sedentary labels | ±60 min |

## Testing the full E2E flow

Validates: Step 1 (read history) → Step 2 (compute delta) → Step 3 (fire nudge) → Step 3b (invoke Flow A) → Flow A bootstrap (`patterns.json` created) → Step 4 (speak) → Step 5 (log nudge).

### Prerequisites
- User has ≥3 days of wellbeing JSONL files (Flow A bootstrap requirement).
- Lamp + OpenClaw running on Pi.
- **Reset agent session first** (file edits don't propagate into a live session — see Files table below). One way: the OpenClaw web monitor "Reset session" button on `agent:main:main`.

### Seed today's wellbeing data

Direct-append to today's file (same path lelamp writes to). Use `enter` early, `drink` early, `using computer` recent — produces a hydration delta well above `HYDRATION_THRESHOLD_MIN` (45 min in `skills/wellbeing/SKILL.md`).

```bash
ssh pi@<lamp-ip> 'sudo bash' <<'EOF'
F=/root/local/users/<user>/wellbeing/$(date +%F).jsonl
> "$F"
ENTER_TS=$(date -d "today 09:00" +%s)
DRINK_TS=$(date -d "today 09:30" +%s)
UC_TS=$(date -d "today 11:00" +%s)
echo "{\"ts\":$ENTER_TS.0,\"seq\":1,\"hour\":9,\"action\":\"enter\",\"notes\":\"\"}"          >> "$F"
echo "{\"ts\":$DRINK_TS.0,\"seq\":2,\"hour\":9,\"action\":\"drink\",\"notes\":\"\"}"          >> "$F"
echo "{\"ts\":$UC_TS.0,\"seq\":3,\"hour\":11,\"action\":\"using computer\",\"notes\":\"\"}"   >> "$F"
EOF
```

### Fire the activity event (real lelamp pipeline path)

```bash
curl -s -X POST 'http://<lamp-ip>/api/sensing/event' \
  -H 'Content-Type: application/json' \
  -d '{"type":"motion.activity","message":"Activity detected: using computer.","current_user":"<user>"}'
```

### Expected agent behavior (verified 2026-04-28 on `lamp-002`)

| Stage | Observed |
|---|---|
| Step 1 query | `GET /api/agent/wellbeing-history?user=gray&last=50` (no slice) |
| Step 2 delta | hydration ~159 min vs threshold (5-min test value at the time; production is 45 min) — exceeds |
| Step 3 decision | nudge hydration (priority over break) |
| Step 3b invoke | `habit/SKILL.md` Flow A called |
| Flow A guard | mtime check passes (file missing → cold path) |
| Flow A bootstrap | reads 47 days of wellbeing logs, computes patterns |
| `patterns.json` written | `/root/local/users/gray/habit/patterns.json` (18 patterns, all weak — frequency ≤ 0.17) |
| Step 3b match | no moderate+ habit found → use generic phrasing |
| Step 4 speak | `<say>You've been at the screen a while. Want some water? [sigh]</say>` |
| Step 5 log | `nudge_hydration` row appended to today's wellbeing JSONL |

### Verify

```bash
ssh pi@<lamp-ip> 'sudo bash -c "
  cat /root/local/users/<user>/habit/patterns.json | jq .updated_at,.days_observed
  tail -1 /root/local/users/<user>/wellbeing/$(date +%F).jsonl | jq .action
"'
# expect: an ISO timestamp from today, days_observed ≥ 3, action == \"nudge_hydration\"
```

### Common pitfalls

- **Seeded `hour` must match `ts`** — agent reads the `hour` field for display but `ts` for delta math. Mismatched values (e.g. `ts` at 11:13 with `hour:12`) make the agent compute the wrong delta and skip the nudge.
- **Without session reset**, agent uses cached SKILL behavior from the prior session and may skip Step 3b entirely — even with the latest SKILL.md on disk.
- **patterns.json bootstrap requires ≥3 day files** under `wellbeing/` (Flow A's freshness guard exits early on `insufficient_data`).
- **No nudge fires → no Flow A** by design; bootstrap piggybacks on real behavioral inflection. To test Flow A in isolation, run its bash guard manually.

## Web Monitor

The Users tab shows a **habit** badge per user when `patterns.json` exists. The file is viewable in the folder tree under `habit/patterns.json`.

## Files

| File | Purpose |
|------|---------|
| `skills/habit/SKILL.md` | Skill definition — Flows A–D, algorithm, storage |
| `robots/lamp/SOUL.md` | "Observing Habits" section — conversation intent logging |
| `skills/wellbeing/SKILL.md` | Step 3b — invokes Flow A when a nudge fires and `bootstrap_needed=true`; uses patterns.json to enrich nudge phrasing |
| `system/skills/catalog_gen.go` | Registers habit in the skill catalog |
| `hal/models.py` | `habit_patterns` field in FacePersonDetail |
| `hal/routes/sensing.py` | Checks habit/patterns.json in face/owners API |
| `system/web/src/pages/monitor/face-owners/PersonCard.tsx` | Habit badge + folder in Users tab |
