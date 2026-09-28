# Claude Code Prompt — Lamp Emotion Spam Guard

## Goal

Review and fix Lamp/HAL camera emotion spam in `autonomous-ecm/ai-lamp-openclaw`.

This is **not** about making emotion detection 100% correct. Face/camera emotion is noisy by nature. The product goal is:

1. reduce bad/noisy emotion samples upstream, and
2. prevent raw unstable emotion labels from continuously triggering Lamp/OpenClaw downstream.

We need a pragmatic production guard. It is better to drop/merge uncertain emotion events than to keep injecting `emotion.detected` into Lamp and spam the agent/token queue.

---

## Status

- **Phase 1 — TTL map dedup**: ✅ done. `_last_sent_by_key: dict[tuple[str, str], float]` in `emotion.py`; old entries pruned each flush.
- **Phase 2 — polarity bucket dedup**: ✅ done. `EMOTION_BUCKETS` collapses fine-grained labels into `positive` / `negative` / `other`; dedup key is `(current_user, bucket)`.
- **Outbound message hedge** (the "more product-correct variant" below): ✅ done. Message now appends a parenthetical with `confidence`, `bucket`, and a bucket-tuned hedge clause. The raw `Emotion detected: <Label>.` prefix is preserved so `user-emotion-detection/SKILL.md` parser + Fear→stressed / Sad→sad mood mapping keep working unchanged. See `robots/lamp/docs/sensing-behavior.md` → `emotion.detected` event for the full spec.

What remains open:

- **Layer 1 (upstream sample quality)** — face-crop size/distance gating, multi-frame aggregation, top-2 margin treatment. Still requires perception-service coordination (@tnk2908). Not blocking downstream.
- **Skill side** — companion fix in `music-suggestion/SKILL.md` and `sensing/SKILL.md` forbids greeting openers on emotion events and adds tone-matched templates per mood. Tracks the same root cause: model over-commits on a noisy "Emotion detected: Fear." cue.

---

## Files to inspect first

- `hal/drivers/sensing/perceptions/processors/emotion.py`
- `hal/drivers/sensing/perceptions/processors/motion.py`
- `hal/drivers/sensing/perceptions/processors/faceid/` (face pipeline; `processors/facerecognizer_v2.py` is a back-compat re-export shim for it)
- `hal/drivers/sensing/sensing_service.py`
- `hal/config.py`

Relevant recent commits:

- `503aab58` — `lelamp/emotion: dedup theo motion (5min same user+emotion)`
- `c4f608fa` — `lelamp/emotion: dedup theo current_user (giống motion)`
- `e407f630` — `lelamp/emotion: skip flush khi current_user rỗng (no subject)`

---

## Current behavior confirmed from code

### Emotion flush

In `emotion.py`:

- `_process_face()` calls emotion backend per face crop.
- Results are buffered per `face.person_id`.
- `_flush_buffer()` runs every `EMOTION_FLUSH_S`.
- `Neutral` is ignored.
- dominant non-neutral label is selected by `Counter(...).most_common(1)[0]`.
- outbound event is:

```py
message = f"Emotion detected: {dominant_emotion}."
self._send_event("emotion.detected", message, "emotion", [snapshots_buffer[-1]], None)
```

### `current_user` behavior

Emotion dedup uses:

```py
current_user = self._perception_state.current_user.data or ""
```

`FaceRecognizer.current_user()` behavior:

- known friend within forget window -> lowercased friend name
- no friend but stranger within forget window -> `"unknown"`
- nobody recently -> `""`

So `stranger_1`, `stranger_2`, etc. collapse to `"unknown"`. Preserve this.

### Empty user behavior

Current code skips emotion event if:

```py
current_user == ""
```

Preserve this. If nobody is in scene, there is no subject to attribute emotion to.

### Global cooldown

In `sensing_service.py`, global cooldown is intentionally skipped for:

```py
("motion.activity", "emotion.detected")
```

So emotion spam must be controlled in the emotion processor itself.

---

## Current issue: dedup is only last-key

Current state in `emotion.py`:

```py
self._last_sent_key: tuple[str, str] | None = None  # (current_user, emotion)
self._last_sent_ts: float = 0.0
self._dedup_window_s: float = 300.0  # 5 min
```

Current drop condition:

```py
key = (current_user, dominant_emotion)
if self._last_sent_key == key and (cur_ts - self._last_sent_ts) < self._dedup_window_s:
    drop
```

This only blocks consecutive duplicate keys.

It blocks:

```text
unknown + sad
unknown + sad  # dropped within 5 min
```

It does **not** block alternating noisy labels:

```text
unknown + sad   # sent
unknown + fear  # sent
unknown + sad   # sent again because last key is fear
unknown + fear  # sent again because last key is sad
```

This is too weak for camera emotion, because emotion labels can flip `sad/fear/sad/fear` from the same ambiguous visual signal.

---

## Required fix: two layers

### Layer 1 — upstream detect/sampling quality

Do not aim for perfect confidence. Instead reduce bad samples before they become events.

Discuss/inspect whether current code/backend can support:

- only sample if face crop is large/clear enough
- only sample when face is near enough
- aggregate multiple frames before deciding
- if backend exposes top-2 scores or margin, treat close calls as uncertain
- skip or log-only if ambiguous/uncertain

This may require integrations/perception-service/@tnk2908 work. Do not block downstream guard on this.

### Layer 2 — downstream spam guard in HAL

This should be implemented now.

Replace last-key dedup with TTL memory so repeated `(current_user, emotion)` is dropped within the window even if another emotion was sent in between.

Suggested phase-1 state:

```py
self._last_sent_by_key: dict[tuple[str, str], float] = {}
self._last_sent_key: tuple[str, str] | None = None  # optional: keep for debug/status
self._last_sent_ts: float = 0.0                    # optional: keep for debug/status
```

Suggested phase-1 drop logic:

```py
key = (current_user, dominant_emotion)

# prune old dedup entries
cutoff = cur_ts - self._dedup_window_s
self._last_sent_by_key = {
    k: ts for k, ts in self._last_sent_by_key.items() if ts >= cutoff
}

last_ts = self._last_sent_by_key.get(key)
if last_ts is not None and (cur_ts - last_ts) < self._dedup_window_s:
    logger.info(
        "[activity.emotion] dedup drop: %s (key seen %.1fs ago)",
        message,
        cur_ts - last_ts,
    )
    continue

self._last_sent_by_key[key] = cur_ts
self._last_sent_key = key
self._last_sent_ts = cur_ts
send
```

Expected behavior after phase 1:

```text
unknown + sad   # sent
unknown + fear  # sent
unknown + sad   # dropped if inside 5 min, because (unknown, sad) already seen
unknown + fear  # dropped if inside 5 min, because (unknown, fear) already seen
```

---

## Reset behavior

Current `reset_dedup(new_user)` clears dedup only when the visible user actually changes. Preserve that intent.

Important case:

```text
stranger_79 -> stranger_77
```

Both should still be `current_user == "unknown"`, so dedup must **not** reset just because stranger internal ID flickered.

For TTL map, acceptable options:

- If `new_user == last_user`, do nothing.
- If user actually changed, clear whole map or remove entries for old user.

Keep it simple unless tests indicate otherwise.

---

## Optional phase 2 if phase 1 is still too noisy

If production still sees `sad/fear` alternating spam, use emotion buckets for dedup.

Example bucket map:

```py
negative = {"Sad", "Fear", "Angry", "Disgust"}
positive = {"Happy", "Surprise"}
neutral = {"Neutral"}  # already skipped
```

Then dedup key can be:

```py
(current_user, emotion_bucket)
```

Minimal-risk variant:

- bucket only for dedup key
- keep outbound message raw, e.g. `Emotion detected: Sad.`

More product-correct variant:

- outbound message becomes less overconfident, e.g. `Emotion cue detected: concern.`
- only do this after checking downstream parsing/skills.

---

## Acceptance criteria

Required:

- `unknown + sad`, `unknown + fear`, `unknown + sad` within 5 minutes should drop the second `unknown + sad` after phase 1.
- `unknown + fear`, repeated within 5 minutes with other emotions in between, should also drop.
- Stranger ID flicker must not reset dedup because all strangers collapse to `unknown`.
- `current_user == ""` still skips sending.
- `Neutral` still does not emit.
- Dedup map is pruned and cannot grow forever.
- Do not accidentally change motion behavior unless explicitly needed.

Preferred:

- Add tests around `EmotionPerception._flush_buffer()` if feasible.
- At minimum run syntax check/tests relevant to lelamp.
- Logging should clearly explain drops.
- Avoid overengineering config knobs unless needed. `HAL_EMOTION_DEDUP_WINDOW_S` is okay if simple; complex modes can wait.

---

## What not to do

- Do not assume emotion model can be made 100% confident.
- Do not rely only on upstream detection to solve spam.
- Do not reintroduce per-stranger IDs into dedup.
- Do not let raw camera emotion labels trigger unlimited agent runs.
- Do not paste large summaries into chat; keep this as a repo doc/prompt for Claude Code review.

---

## Recommended next step

Phase 1 + Phase 2 + message hedge are landed (see Status at top). Remaining work:

- **Layer 1 upstream sample quality** — needs perception-service coordination. Track separately.
- **Tune `EMOTION_DEDUP_WINDOW_S`** if production still shows cross-bucket flips spamming the agent (e.g. genuine Fear↔Happy oscillation that the bucket key can't collapse).
- **Watch confidence distribution** — now that confidence is in the outbound message, decide whether to gate sends on a higher floor than `EMOTION_CONFIDENCE_THRESHOLD` if low-confidence reads are still leaking through.
