# Plan: HAL owns wellbeing log

Status: **Partially implemented** (2026-04-22). Bugs A+B fixed, Bug C still pending.

## Bugs to fix

**A. Duplicate `unknown: enter`** — ✅ FIXED. Added `_owners_session_start` / `_strangers_session_start` tracking, handler-level transition dedup, and wellbeing.go phase-2 dedup.

**B. `current_user()` wrong winner** — ✅ FIXED. Now sorts by `session_start` (not `last_seen`). Chloe 18:00 + An 18:30 → An wins (newer session_start).

**C. Stranger `leave` never fires** — ❌ PENDING. `_send_leave_event` for strangers still commented out at `faceid/perception.py`.

## Principle

**HAL owns the log. Skill owns the nudge decision.** Sensing emits clean events that encode "effective user". Skill just reads + decides + speaks.

## Changes

### HAL — `faceid/perception.py`

1. Add `_owners_session_start`, `_strangers_session_start` — set on first-seen-after-gap, clear on leave.
2. `current_user()`: among friends with live session_start, pick max session_start. No friend → `"unknown"` if any stranger live. Else `""`.
3. Uncomment `_send_leave_event` for strangers (line 609).
4. Track `_effective_user`; fire events only on transition:
   - `"" → X`: `presence.enter(X)`
   - `X → Y`: `presence.leave(X)` + `presence.enter(Y)`
   - `X → ""`: `presence.leave(X)`
   - `X → X`: no event
5. Replaces current per-face raw `presence.enter` spam.

### HAL — `motion.py`

- After 5-min dedup passes, POST each activity label to `http://127.0.0.1:5000/api/wellbeing/log` with `current_user`.
- POST **before** firing `motion.activity` so skill's history read is consistent.
- Still fire `motion.activity` to agent (it may nudge/comment — just not log).

### Lamp handler — `server/sensing/delivery/http/handler.go`

- Drop enter/leave log writes at `handler.go:124-137`. HAL events are clean; handler just forwards.
- Keep `mood.SetCurrentUser` / `ClearCurrentUser` for `[context: current_user=X]` tag.

### Wellbeing SKILL.md

- Remove Step 1 (log activity) — HAL writes now.
- Keep Step 2–5 (read, deltas, decide, log `nudge_*`). Agent only writes `nudge_*`.
- Update action table: `drink` / `break` / sedentary labels written by HAL, not agent.

### Music-suggestion SKILL.md

No logic changes. Benefits indirectly from stable `current_user`.

## Expected behaviour

| Scenario | Before | After |
|---|---|---|
| Stranger sits 17:00, new IDs at 17:30 | `unknown: enter@17:30` → no nudge | `unknown: enter@17:00` only → nudges at threshold |
| Chloe 18:00 + stranger flicker | `current_user` may flip to `unknown` | Stays `chloe` until Chloe actually leaves |
| Chloe 18:00 + An 18:30 | Arbitrary winner | `an` (newer session_start) |

## Open questions

1. Event shape: reuse `presence.enter/leave` vs new `presence.effective_change`. Lean **reuse** — minimal downstream churn.
2. Existing logs have duplicates from today — no cleanup migration; skills tolerate noise in tail.
3. Direct HAL POST failure (Lamp down): log-and-forget, same semantics as current agent behaviour.
4. **POST-before-fire for `motion.activity`?** Recommend **yes** — HAL POSTs `/api/wellbeing/log` first, then fires `motion.activity` to the agent. Guarantees history read by the skill already sees the new row, avoids agent-side race if the skill queries in parallel.
5. **Trim the `motion.activity` payload now that agent doesn't log?** The raw label is still useful to the agent for grounding nudge phrasing (see the SKILL.md table mapping labels → nudge examples). Keep the current format; revisit only if we see token pressure. No action needed for this PR.

## Next steps (when implementing)

1. Read `hal/drivers/sensing/perceptions/processors/motion.py` — locate the 5-min dedup boundary and the `motion.activity` fire point.
2. Read `hal/drivers/sensing/perceptions/processors/faceid/perception.py` in detail — map out the full state machine for session_start + effective_user transitions.
3. Draft patches and list diffs before applying. Confirm with user before editing.

## Related

- Memory: `project_presence_injection_rules.md`
- `robots/lamp/docs/motion-activity-whitelist.md`, `robots/lamp/docs/sensing-behavior.md` (may need small update)
