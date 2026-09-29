# DEV — Proactive Skills Audit (2026-05-15)

Audit of 10 proactive OpenClaw skills (sensing/cron-driven, not user-initiated). Source: 3 parallel reviews + manual verification against current code. Each finding has a file:line ref for verification.

Skills covered:
- **Core proactive**: `wellbeing`, `posture`, `habit`
- **Sensing chain**: `sensing`, `sensing-track`, `guard`
- **Emotion chain**: `mood`, `emotion`, `user-emotion-detection`, `music-suggestion`

Verdict: **not tight**. 6 verified P0 bugs + 8 P1 cross-cutting issues. Guard and sensing-track have user-visible correctness gaps.

---

## P0 — verified behavior bugs

### 1. Guard tag never attaches to motion events

`system/server/sensing/delivery/http/handler.go:248`:

```go
guardActive := isPassive && h.config.GuardModeEnabled() &&
    (req.Type == "presence.enter" || req.Type == "motion")
```

HAL emits `motion.activity` (`hal/.../motion.py:488`), never bare `"motion"`. The `"type": "motion"` at `motion.py:555` is a health snapshot in `to_dict()`, not an event payload.

**Effect**: The guard reaction table row "Motion (no known face) → shock 0.9" in `skills/guard/SKILL.md:82` is unreachable. Strangers who appear behind the camera with no face detection generate zero alerts.

### 2. `guardTag` lost on queue drain

`runtimes/openclaw/service_events.go:202` calls `sensingmsg.Build(..., "")` — guard tag is always empty when a queued event is drained later. Comment at `handler.go:301-307` acknowledges this.

**Effect**: If a stranger walks in while the agent is busy, the event is queued. When drained (up to 5 min later via `busyTTL`), it arrives without `[guard-active]` and without `MarkGuardRun`. No Telegram broadcast, no snapshot delivery. Combined with the busy-stuck wedge (`robots/lamp/docs/debug/busy-stuck.md`), guard goes silent for the full 5 min window.

### 3. `sensing-track` SKILL claims 30-day retention; actual is 7

**Status (2026-05-15)**: resolved via code-side bump on branch `chore/bump-retention-wellbeing-mood-30d`. Wellbeing and mood retention bumped to 30 days; the `skills/sensing-track/SKILL.md:10` flow-events claim was corrected to "7-day" (flow events stay at 7 — debug log only). Mood line at `:168` already said "30-day" and now matches the new code.


`system/lib/flow/flow.go:56`:

```go
retentionDays = 7
```

`skills/sensing-track/SKILL.md:10` says "30-day retention". Multi-day recipes (`SKILL.md:105`) spanning more than 7 days will hit purged files.

**Effect**: Agent confidently answers "no events that day" when the file was purged 8+ days ago.

**Empirical confirmation (2026-05-15)**: asked Lamp "ngày 7 tháng 5 có ai vào nhà không?" (8 days ago — inside the 30-day claim but outside actual 7-day window). Response: "log ngày 7/5 không còn trên máy ... Hiện chỉ còn dữ liệu từ 8/5 đến 15/5". 8 files retained matches `cutoff = today - 7 days = 2026-05-08`. Agent self-corrected by listing the directory first; had it gone straight to `cat /root/local/flow_events_2026-05-07.jsonl` based on the SKILL's 30-day claim, the user would have gotten a silent empty answer.

### 4. `patterns_now` hardcoded `nil` — pre-emptive posture route unreachable

`system/skillcontext/posture/posture.go:175-178`:

```go
// PatternsNow stays nil until habit Flow A starts emitting
PatternsNow: nil,
```

`skills/posture/SKILL.md:221` (skill since removed in `1429864fc`) routes pre-emptive nudges via `patterns_now` non-empty. That field is always nil → route never fires.

### 5. Habit references nonexistent `wellbeing` anchors

`skills/habit/SKILL.md:56,109,127` and `habit/reference/{match-helper,build-patterns}.md` repeatedly cite "wellbeing/SKILL.md Step 3b" and "Step 4 phrasing".

`skills/wellbeing/SKILL.md` has **no numbered Steps** — only Routes 1-8 in the decision table. Cross-skill dispatch instruction points at a stale anchor; agent must guess the intent.

### 6. Stale comment in wellbeing handler

`system/server/sensing/delivery/http/handler.go:606` still reads "Bucket names (agent writes from motion.activity hybrid output)" — contradicts the current rule that HAL posts drink/break directly (`hal/.../motion.py:490-513`). A skill rewrite based on this comment would reintroduce duplicate rows.

---

## P1 — cross-cutting looseness

### HW marker regex bans `}` in body — 5 skills exposed

`system/server/agent/delivery/http/handler_hw.go:57`:

```go
var hwMarkerRe = regexp.MustCompile(`\[HW:(/[^:]+):(\{[^}]*\})\]`)
```

Skills that embed user-language one-liners in `notes`/`message` fields (mood, music-suggestion, user-emotion-detection, posture, wellbeing) can produce bodies containing `}` (chord notation, JSON-like phrases, certain language tokens). Marker body truncates at the first `}`, POST is malformed, log row is lost, cooldown never registers → re-suggestion in ~2 min.

No defensive detection; "rarely a problem" notes in `skills/mood/SKILL.md:118` and `skills/music-suggestion/SKILL.md:96` are aspirational.

### Thresholds live in SKILL prose, not code

| Skill | Threshold | Test | Prod | Where it lives |
|---|---|---|---|---|
| `wellbeing` | hydration | 5 min | 45 min | `SKILL.md:24` |
| `wellbeing` | break | 7 min | 30 min | `SKILL.md:24` |
| `posture` | upstream cooldown | 5 min | 5 min | lelamp `config.py:157` (`POSE_ERGO_COOLDOWN_S`) |
| `user-emotion-detection` | suggest cooldown | 7 min | 30 min | `SKILL.md:53,108` |
| `music-suggestion` | suggest cooldown | 7 min | 30 min | `SKILL.md:53` |

All thresholds now live either in SKILL.md prose or in lelamp's config — none in Lamp Go anymore (the posture voice budget was removed, see `57fb6d87`). The SKILL-prose ones rely on the LLM reading them; compaction-summary distortion (`robots/lamp/docs/debug/...`, see `project_openclaw_compaction_summary_risk` in memory) is a real risk — a re-summarized SKILL can change the threshold without code review.

### `mapped_mood` vocabulary mismatch

`system/skillcontext/emotion.go:105-112` `suggestionWorthyMoods` includes `tired` and `bored`, but the FER/voice → mood map (`emotion.go:92-103`) never produces them — only `happy`, `sad`, `stressed`, `excited`.

`skills/music-suggestion/SKILL.md:126-127` example "Mood: tired (known user)" cannot trigger from `emotion.detected` today. Only Telegram/conversation paths through `skills/mood/SKILL.md` can reach `tired/bored`.

### Self-replay amplification

NO_REPLY turns get replayed as fresh UUID runs ~1s later (`robots/lamp/docs/debug/openclaw-selfreplay.md`). All HW-marker-emitting skills replay their markers on the second turn:

- 1 real `emotion.detected` → 2 mood signals + 2 mood decisions + (potentially) 2 music-suggestion logs
- 1 real `motion.activity` (silent wellbeing route) → 2 nudge rows
- 1 real guard `presence.enter` → may rebroadcast via `chat.history` rebroadcast

No skill has an idempotency key on its log POST.

### No skill checks busy/sleep/presence gates

- **Busy wedge** (`robots/lamp/docs/debug/busy-stuck.md`): 5-min sensing+guard outage; no skill mentions it.
- **Sleep state**: only `handler.go:181-189` checks `h.isSleeping()` at the layer below skills; skills assume they always fire.
- **Presence freshness**: `presence.leave` arriving 1s after `motion.activity` still lets wellbeing nudge an empty room. No skill reads `session_start` or `latest enter` delta.

### Vietnamese leak in skill files

Violates `feedback_skill_files_english_only` (skill-files must be English; agent adapts language at runtime):

- `skills/posture/reference/reading-message.md:72,84` (skill removed in `1429864fc`) — "cổ tay lệch / gập", "cổ ngửa ra sau"
- `skills/posture/SKILL.md:215` (removed in `1429864fc`) — "Cổ."

### `usercanon.Resolve` vs `wellbeing.NormalizeUser` still parallel

`system/skillcontext/wellbeing/wellbeing.go:143` runs its own `NormalizeUser`. Memory `project_usercanon_refactor` flagged this; same regex today, but no test pins them together.

---

## Cross-skill conflicts

### Friend `presence.enter` while guard is ON

`skills/sensing/SKILL.md:17` routes `[guard-active]` to `skills/guard/SKILL.md`. But `handler.go:248` attaches the guard tag to *any* `presence.enter` regardless of friend vs stranger.

- `sensing/SKILL` → "switch to guard skill"
- `guard/SKILL` → "greet friend, summarize, ask to disable"

Both skills claim the same tagged message; resolution is non-deterministic and depends on which skill's rules win in the agent's prompt stack.

### Activity → music? Doc vs SKILL disagree

- `skills/music-suggestion/SKILL.md:12`: "Activity events route to `skills/wellbeing/SKILL.md` and never to this skill."
- `robots/lamp/docs/sensing-behavior.md:342-345`: "agent should suggest music on sedentary activity."

SKILL is stricter; doc is permissive. Resolve one direction.

### Wellbeing reaction route has no cooldown

`skills/wellbeing/SKILL.md` Route #1 (reaction) speaks on every drink/eat event. If HAL dedup misfires, agent chatters back-to-back. "Variety self-check" in the SKILL is prompt-only.

### Habit Flow A freshness guard + Flow E "honest gap"

Flow A returns stale `patterns.json` (`build-patterns.md:8-13`: `cat $PATTERNS; exit 0` when mtime < 6h). Flow E (`open-question.md:38-40`) tells agent "don't recite stale patterns as current — re-check mtime". Two opposite instructions for the same file; easy to slip.

---

## False alarm noted

The emotion-chain audit claimed `[HW:/dm:...]` is silently broken because `fireHWCalls` skips it (`handler_hw.go:85`). **Incorrect** — `/dm` is handled separately in the lifecycle path (`handler_events.go:696-703`), which extracts `telegram_id` and routes via `agentGateway`. Channel-aware delivery works.

---

## Recommended fix order

1. **Guard motion type** (P0 #1) — change `req.Type == "motion"` to `"motion.activity"` in `handler.go:248`.
2. **Guard tag on queue drain** (P0 #2) — preserve `guardTag` through the queue so busy-window strangers still alert.
3. **Retention number in SKILL** (P0 #3) — ✅ applied on branch `chore/bump-retention-wellbeing-mood-30d`: wellbeing+mood bumped to 30 days, posture to 60 days, habit Flow A read window to 14-30 days. Flow-events SKILL line corrected to "7-day". Habit min-data threshold kept at 3 days (pattern emission stays early).
4. **Habit Step refs** (P0 #5) — rename to current Route numbers, or add Step anchors to wellbeing.
5. **Stale comment** (P0 #6) — drop or rewrite `handler.go:606` comment.
6. **patterns_now** (P0 #4) — either remove the pre-emptive route from `skills/posture/SKILL.md` or wire the field.
7. **P1 thresholds-in-prose** — move into `BuildEmotionContext` / `BuildWellbeingContext` as injected fields.
8. **P1 HW marker regex** — allow `}` in body (non-greedy or base64) and reject malformed markers cleanly.
9. **P1 self-replay idempotency** — add an event UUID to marker bodies; server-side dedup window.

---

## Habit formation science vs current retention (2026-05-15)

### Research consensus

| Source | Median | Mean | Range |
|---|---|---|---|
| Lally et al. 2010 (UCL, n=96) | **66 days** | — | 18-254 days |
| Systematic review + meta-analysis 2024 (n=2601, 20 studies) | **59-66 days** | 106-154 days | 4-335 days |

Findings:
- The popular "21-day rule" is a myth; no peer-reviewed study supports it.
- Median time to behavioral automaticity ≈ 2 months.
- Morning habits form more reliably than evening ones; self-chosen behaviors form faster than assigned; simple actions with clear triggers form faster than complex ones.

Sources:
- Lally et al. 2010 — *How are habits formed: Modelling habit formation in the real world* (European Journal of Social Psychology).
- Singh et al. 2024 — *Time to Form a Habit: A Systematic Review and Meta-Analysis of Health Behaviour Habit Formation and Its Determinants* (Healthcare, MDPI; PMC11641623).
- UCL "Health Chatter" blog, Dr Pippa Lally Q&A (University of Surrey, 2024).

### Current numbers vs research

| Field | Code | Lally / Meta-analysis | Verdict |
|---|---|---|---|
| Wellbeing retention | `wellbeing.go:41` = **30 days** (was 7, bumped 2026-05-15) | ≥60 days to cover median formation | 🟡 acceptable — covers ~½ of Lally median, cheap to bump further if needed |
| Mood retention | `mood.go:58` = **30 days** (was 7, bumped 2026-05-15) | ≥30 days for trend | ✅ matches recommended |
| Habit min data threshold | `skills/habit/SKILL.md:49` = 3 days | 18 days (Lally minimum) | 🟡 intentional — kept at 3 so patterns gen early; threshold and retention are orthogonal |
| Habit Flow A read window | `build-patterns.md:25` = **14-30 days** (was 7-14, bumped 2026-05-15) | 30-60 days to bracket formation | ✅ leverages new wellbeing retention; emission still gates at `days_observed ≥ 3` |
| Posture retention | `posture.go:69` = **60 days** (was 30, bumped 2026-05-15) | 60 days better | ✅ matches recommendation |
| Music-suggestion retention | `suggestion.go:39` = 7 days | n/a (cooldown only reads today+yesterday) | ✅ fine |
| Flow events retention | `flow.go:56` = 7 days | n/a (debug log only) | ✅ fine |

### Conceptual note: pattern detection ≠ habit formation

The current code at 3-day threshold + 7-day retention is doing **weekly pattern detection**, not habit formation tracking. These are two distinct concepts:

| Concept | Definition | Data needed |
|---|---|---|
| **Pattern** | Detectable consistency across a few observations | ≥3 data points (current threshold) |
| **Stable habit** | Behavior triggered semi-automatically | ≥18 days (Lally minimum), median 66 |
| **Automaticity plateau** | Stops getting more automatic | ~66 days median, up to 254 |

The 3-day threshold and the retention window are **orthogonal** — they answer different questions:
- *Threshold*: "Do we have enough data to compute anything at all?"
- *Retention*: "How far back can we look for trends?"

Bumping retention to 90 days does **not** require changing the 3-day threshold. A new user can still get a weak day-4 nudge ("you usually drink water around now this week") while the system gathers enough samples to detect a real habit at day 18+ and a stable plateau at day 66+.

### Two paths forward

**Option A — Rename to match scope** (zero storage cost):
- `habit/` skill → `pattern/` (or keep `habit/` but tighten phrasing)
- Replace user-facing "habit" wording with "your usual pattern this week" / "tendency"
- Reserve word "habit" for situations where we actually have ≥18 days of consistent data
- Keep all retention numbers as-is

**Option B — Bump retention to match the word "habit"**:
- Wellbeing retention: 7 → 90 days (1.5 MB/user at ~16 KB/day per `posture.go:65-68` comment; negligible)
- Mood retention: 7 → 30 days
- Habit Flow A read window: 7-14 → 30-60 days
- Habit threshold tiers in SKILL: "early pattern" 3-13 days, "habit" ≥14 days, "stable habit" ≥66 days
- Posture: keep 30 (or bump to 60 for tighter alignment)
- Flow events: keep 7

Disk cost for Option B is negligible (under 5 MB per user for everything). Risk is mostly cognitive complexity for the agent and SKILL prose.

**Hybrid recommendation**: bump wellbeing → 30 days and mood → 30 days (cheap, immediately useful), keep habit threshold at 3 but expose tiered labels in `[wellbeing_context]` (e.g., `data_maturity: "pattern" | "habit" | "stable"`) so the agent's phrasing can grade itself.

---

## Files referenced

- `system/server/sensing/delivery/http/handler.go` — guard tag, wellbeing/mood/posture/music-suggestion log endpoints
- `system/server/agent/delivery/http/handler_hw.go` — HW marker dispatcher, regex
- `system/server/agent/delivery/http/handler_events.go` — `/broadcast`, `/speak`, `/dm` lifecycle handling
- `runtimes/openclaw/service_events.go` — `busyTTL`, queue drain, `guardTag` loss
- `system/lib/flow/flow.go` — `retentionDays`
- `system/skillcontext/{wellbeing,posture,emotion}.go` — pre-fetched context blocks
- `system/lib/sensingmsg/sensingmsg.go` — context injection
- `system/skillcontext/wellbeing/wellbeing.go` — parallel `NormalizeUser`
- `hal/drivers/sensing/perceptions/processors/motion.py` — `motion.activity` emitter, wellbeing log poster
- `skills/{guard,sensing,sensing-track,wellbeing,habit,mood,emotion,user-emotion-detection,music-suggestion}/SKILL.md` (the standalone `posture` skill was removed in `1429864fc`; posture guidance now lives in `skills/wellbeing/reference/posture.md`)
