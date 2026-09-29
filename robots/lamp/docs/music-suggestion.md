# Proactive Music Suggestion

> Lamp proactively suggests music that fits the user's mood — **never auto-plays**, it only suggests by voice and waits for confirmation.
>
> Vietnamese version: [vi/music-suggestion_vi.md](vi/music-suggestion_vi.md)

---

## Overview

The agent **decides for itself when** to suggest music. The design is **event-driven** — no cron jobs, no backend timers:

- **Mood trigger (the only trigger)** — `skills/user-emotion-detection/SKILL.md` is the router for `emotion.detected` (camera) and `speech_emotion.detected` (voice) events. It picks exactly one route per turn (`music` / `checkin` / `action` / `silent`); `skills/music-suggestion/SKILL.md` produces output only when the router picks `music`.
- **Activity events never suggest music** — `motion.activity` / `[activity]` events (sedentary, drink/break, celebrate) route to `skills/wellbeing/SKILL.md` only.
- **Suggestion history** — every suggestion and the user's accept/reject is logged so the AI can learn patterns.

It is fully **AI-driven**: the agent decides from the SKILL.md instructions. The backend pre-computes context and stores/serves the history.

---

## Trigger flow

```
emotion.detected (camera) / speech_emotion.detected (voice)
    ↓
Backend injects [context: current_user=X] + [emotion_context: {...}]
    ↓
user-emotion-detection/SKILL.md (router): log mood signal/decision (HW markers),
pick ONE route: music | checkin | action | silent
    ↓ (music)
music-suggestion/SKILL.md gate — all must hold:
  ├── suggestion_worthy == true (mood ∈ sad, stressed, tired, excited, happy, bored)
  ├── audio_playing == false
  ├── last_suggestion_age_min ∉ [0, 7)   (cooldown, shared with checkin)
  └── is_decision_stale == false, or a fresh decision was synthesized this turn
    ↓ (all pass)
Pick genre: music_pattern_for_hour → default genre table → audio history override
    ↓
One spoken sentence + [HW:/emotion] (+ [HW:/dm] for known users)
+ [HW:/music-suggestion/log:{...}]  → POST /api/music-suggestion/log
```

### Pre-fetched context (`[emotion_context: ...]`)

Built by `BuildEmotionContext` in `system/skillcontext/emotion.go`. The agent fires **no read tool calls** when the block is present:

| Field | Replaces |
|-------|----------|
| `audio_playing` | `GET /audio/status` |
| `last_suggestion_age_min` (`-1` if none today) | music-suggestion history `last=1` |
| `prior_decision` + `is_decision_stale` | mood-history `kind=decision&last=1` |
| `audio_recent` (`{track,duration_s,stopped}`) | `GET /audio/history?last=1` |
| `music_pattern_for_hour` | reading `habit/patterns.json` for the current hour ±1 |
| `suggestion_worthy`, `mapped_mood` | pre-applied mood bucket gate |

Only when the block is missing does the skill fall back to a concurrent GET batch (audio status, suggestion history, mood history, audio history, `patterns.json`).

### Unknown users

Unknown users (strangers) still get suggestions. Data is stored under `/root/local/users/unknown/`. The only difference: speak only, no Telegram DM (no `telegram_id`), and `/audio/play` omits the `person` field.

### Cooldown

- **7 minutes** in the current skill (`last_suggestion_age_min ∉ [0, 7)`); the skill notes it should be raised to **30 minutes** before production.
- Computed by the backend from today's suggestion log — the agent never checks it with a tool call.
- Shared with the router's `checkin` route (both log via `music-suggestion/log`).

---

## Suggestion History

### Storage

`/root/local/users/{user}/music-suggestions/{YYYY-MM-DD}.jsonl`

Each record:
```json
{"ts":1713359400.5,"seq":1713359400500000000,"hour":14,"trigger":"mood:tired","query":"","message":"How about some calm piano?","status":"pending","user":"gray"}
```

| Field | Meaning |
|-------|---------|
| `seq` | Unix nanoseconds — unique ID for each suggestion |
| `trigger` | Trigger source: `mood:<mood>` (the only suggestion trigger); the router's check-in route logs `checkin:<emotion>` |
| `query` | YouTube search query (empty for a text-only suggestion) |
| `message` | Suggestion text sent to the user |
| `status` | `pending` → `accepted` / `rejected` / `expired` |

### API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/music-suggestion/log` | POST | Log a suggestion (normally via the inline `[HW:/music-suggestion/log:{...}]` marker) |
| `/api/music-suggestion/status` | POST | Update status (`accepted` / `rejected`) by `day` + `seq` |
| `/api/agent/music-suggestion-history` | GET | Query history (params: `user`, `date`, `last`; admin-gated) |

### Retention

7 days — older files are deleted automatically.

---

## Detailed flow

### User confirms → play music

```
User says "yes, play it" (voice or Telegram reply)
    ↓
Agent:
  1. POST /api/music-suggestion/status → status="accepted"
  2. [HW:/audio/play:{"query":"...","person":"gray"}]
    ↓
Go handler intercepts HW markers → POST /audio/play → HAL
    ↓
HAL: yt-dlp search → ffmpeg → ALSA speaker
```

### User rejects → log rejection

```
User says "no" or "not now"
    ↓
Agent: POST /api/music-suggestion/status → status="rejected"
```

Ignored suggestions get no status update.

---

## Layers and files

### Go server (os-server)

| File | Role |
|------|------|
| `system/skillcontext/musicsuggestion/suggestion.go` | Per-user, per-day JSONL logger: Log, Query, UpdateStatus, retention |
| `system/skillcontext/emotion.go` | `BuildEmotionContext` — builds the `[emotion_context: ...]` block |
| `system/skillcontext/mood/mood.go` | Mood event logger |
| `system/server/sensing/delivery/http/handler.go` | `PostMusicSuggestionLog` / `PostMusicSuggestionStatus` handlers |
| `system/server/agent/delivery/http/handler_api_history.go` | `MusicSuggestionHistory` GET handler |
| `system/server/agent/delivery/http/handler_hw.go` | Routes the `[HW:/music-suggestion/...]` marker to `POST :5000/api/music-suggestion/...` |
| `system/server/server.go` | Routes: `/api/music-suggestion/*`, `/api/agent/music-suggestion-history` |

### Agent skills

| File | Role |
|------|------|
| `skills/user-emotion-detection/SKILL.md` | Emotion router — picks music / checkin / action / silent |
| `skills/music-suggestion/SKILL.md` | Proactive suggestion: gate, genre choice, logging, learning |
| `skills/music/SKILL.md` | Reactive music (user-initiated requests), playback |
| `skills/mood/SKILL.md` | Mood logging |

### HAL (Python)

| File | Role |
|------|------|
| `hal/models.py` | `FacePersonDetail`: includes `music_suggestion_days` |
| `hal/routes/sensing.py` | `/face/owners` endpoint: reads `music_suggestion_days` from the JSONL folder |
| `hal/routes/music.py` | `/audio/play`, `/audio/stop`, `/audio/status`, `/audio/history` |

### Frontend (React)

| File | Role |
|------|------|
| `system/web/src/pages/monitor/types.ts` | `music_suggestion_days` field |
| `system/web/src/pages/monitor/face-owners/PersonCard.tsx` | Shows the music-suggestion-days badge + folder tree |

---

## Data the AI uses

### Music suggestion history

| Field | Used for |
|-------|----------|
| `trigger` | Which mood produced the suggestion |
| `status` | Learn accept/reject patterns |
| `hour` | Which hours the user tends to accept |
| `message` | Avoid repeating the same suggestion |

### Audio history (`audio_recent`, or `GET /audio/history?person={name}&last=1` as fallback)

| Field | Used for |
|-------|----------|
| `query` / `track` | Genre/artist signal |
| `duration_s` | Satisfaction: > 180 s = enjoyed |
| `stopped_by` | `"end"` = liked, `"user"` < 30 s = disliked |

### Learning rules

- Song ended naturally + listened > 3 min → suggest a similar artist/genre
- Stopped manually + listened < 30 s → try a different direction
- Several `rejected` in the suggestion history → reduce frequency / change approach

---

## Speaker conflict

Lamp has a single speaker shared between TTS and music:

| Situation | Behavior |
|-----------|----------|
| AI suggests by voice | TTS speaks the suggestion → user hears it |
| User confirms → play | TTS is suppressed so it does not talk over the music |
| Music playing + TTS request | HAL returns 409 — music keeps priority |
| User says "stop" | `[HW:/audio/stop:{}]` → music stops |

---

## Monitoring & Debug

### API checks

```bash
# Today's suggestion history (admin-gated)
curl -s -H "Authorization: Bearer <admin-token>" "http://<LAMP_IP>:5000/api/agent/music-suggestion-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Mood history (admin-gated)
curl -s -H "Authorization: Bearer <admin-token>" "http://<LAMP_IP>:5000/api/agent/mood-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Audio status
curl -s "http://<LAMP_IP>:5001/audio/status"

# Audio history
curl -s "http://<LAMP_IP>:5001/audio/history?person=gray&last=10"
```

### Web UI

Monitor page → Users section → click a user → open the `music-suggestions/` folder → click a day for details.

### Logs

```bash
journalctl -u os-server | grep -i "suggestion\|music"
```
