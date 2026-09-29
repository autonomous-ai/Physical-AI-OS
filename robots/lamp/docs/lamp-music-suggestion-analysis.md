# Lamp — Music Suggestion Feature Analysis
> Generated: 2026-04-09 | Scope: the standalone "suggest music" feature
>
> Vietnamese version: [vi/lamp-music-suggestion-analysis_vi.md](vi/lamp-music-suggestion-analysis_vi.md)
>
> **Historical analysis.** The shipped design differs from both the HAL timer described below and the Option B cron: music suggestion is **event-driven, mood-only, no cron**. `skills/user-emotion-detection/SKILL.md` routes `emotion.detected` / `speech_emotion.detected` events to `skills/music-suggestion/SKILL.md` with a pre-computed `[emotion_context: ...]` block and a 7-minute cooldown (30 minutes planned for production); activity events go to wellbeing only. The `WellbeingPerception` timer and `HAL_WELLBEING_MUSIC_S` have been removed. See [music-suggestion.md](music-suggestion.md) for the current design.

---

## Overview

Music suggestion is the feature where Lamp proactively proposes music that fits the user's mood/state — **no auto-play**, it only suggests by voice and waits for confirmation.

---

## Current flow (as of 2026-04-09)

```
[HAL - Python]
  WellbeingPerception timer (60 min)
        ↓ fire music.mood event
  POST /api/sensing/event  →  [Lamp Go server]
        ↓
  sensing handler → mood.Log() → QueuePendingEvent (if busy)
        ↓
  SendChatMessageWithImageAndRun → [OpenClaw agent]
        ↓
  sensing SKILL + music SKILL
        ↓
  LLM looks at the image → assesses mood → suggests 1-2 songs
        ↓
  User confirms ("yes", "play that") → HW:/audio/play:{query,person}
        ↓
  [HAL]
  POST /audio/play → MusicService.play(query, person)
        ↓
  yt-dlp search YouTube → ffmpeg → ALSA output device
```

---

## Layer by layer

### Layer 1 — Trigger (HAL Python)

**File (removed):** `WellbeingPerception` in HAL sensing

- Timer runs every **60 minutes** (default, configured via `HAL_WELLBEING_MUSIC_S`)
- Fires only in `PresenceState.PRESENT`
- Captures a frame (`capture_stable_frame`) → attached to the event
- Hardcoded message: *"User has been here for X min. Look at image — assess mood and suggest 1-2 songs..."*
- **Hardcoded, not adaptive, does not learn habits**

### Layer 2 — Event Pipeline (Lamp Go server)

**File:** `system/server/sensing/delivery/http/handler.go`

- Receives `POST /api/sensing/event` with `type: "music.mood"`
- Logs into mood history (`mood.Log`)
- If the agent is busy → `QueuePendingEvent` (replayed later)
- Forwards to the OpenClaw agent with the image

### Layer 3 — AI Decision (OpenClaw skill)

**Files:**
- `skills/sensing/SKILL.md` — received `[sensing:music.mood]` (at the time)
- `skills/music/SKILL.md` — mood → music mapping (at the time)

**LLM logic:**

| State | Action |
|---|---|
| User not in the image | `NO_REPLY` |
| User in a meeting/video call | `NO_REPLY` |
| Focused/working | Suggest lo-fi, ambient |
| Tired/fatigued | Suggest calm piano, acoustic |
| Happy/energetic | Suggest upbeat pop, jazz |
| Stressed/tense | Suggest soft jazz, classical |
| Relaxed/chill | Suggest bossa nova, R&B |

**Key rules:**
- **NO auto-play** — only speak the suggestion, wait for user confirmation
- Max 2 songs per suggestion
- Conversational language, no "based on analysis..."

### Layer 4 — Playback (HAL Python)

**File:** `hal/drivers/voice/music_service.py`

- `POST /audio/play` → `MusicService.play(query, person)` — per-user history at `/root/local/users/{person}/audio_history/`
- yt-dlp searches YouTube → resolves the audio URL
- ffmpeg stream → ALSA device (plughw:CARD,0 or default)
- TTS has priority: if TTS is speaking → wait until done before playing
- Music playing → TTS requests are rejected (HTTP 409)

---

## Mood History (data available but unused)

**File:** `system/skillcontext/mood/mood.go`

Logged 2 kinds of events into `/root/local/mood_YYYY-MM-DD.jsonl` (at the time; now per-user `/root/local/users/{user}/mood/YYYY-MM-DD.jsonl`):
1. **Sensing input:** `music.mood`, `presence.enter`, `wellbeing.break`, etc.
2. **`mood.assessed`:** LLM result — `emotion`, `source`, `response`, `no_reply` flag

**Query API:**
```bash
curl -s "http://127.0.0.1:5000/api/agent/mood-history?date=$(date +%Y-%m-%d)&last=100"
```

**Problem:** the data is collected but **no component reads it back to adjust timing or personalize suggestions**.

---

## Current problems

### 1. Fixed, non-adaptive timer
- Always fires every 60 minutes whether or not the user needs it
- Does not know when the user usually likes music
- Configurable only via env var, needs a restart to change

### 2. Poor music context
- The LLM sees only **1 snapshot** at fire time
- Does not know what the user did during the previous 60 minutes
- No listening history ("which songs were played, which genres the user likes")

### 3. Learning loop not closed
- Mood history is fully logged (`no_reply`, `emotion`, hour...)
- But nothing reads it: "last suggestion at 15:00 → repeated NO_REPLY → maybe the user doesn't want afternoon music"

### 4. Telegram broadcast on music.mood
- Commit `0b690cf`: broadcasts the music.mood agent response to Telegram
- Convenient but may create noise if it fires often

---

## Proposed improvements

### Option A — Hybrid (lowest risk)

Keep the timer in HAL, add an API so the AI can adjust it:

```
HAL timer fires (60 min default)
        ↓
AI decides → also notes in MEMORY.md: "User usually wants music in the evening"
        ↓
Adaptive cron (once/day): AI reads mood history → proposes a new interval
        ↓
AI calls: POST /sensing/wellbeing/config  {"music_interval_s": 7200}
        ↓
HAL updates the interval at runtime
```

**Needs:**
- `POST /sensing/wellbeing/config` endpoint (HAL)
- Skill instructions so the AI knows how to call it

### Option B — Full AI-driven (cleaner, higher risk)

Drop the WellbeingPerception timer for music.mood, replace it with an OpenClaw cron:

```json
{
  "name": "Music: gray",
  "schedule": {"kind": "every", "everyMs": 420000},
  "payload": {
    "kind": "systemEvent",
    "text": "[MUST-SPEAK][music-proactive][person:gray] Proactive music check for gray. Either suggest a song or reply NO_REPLY."
  }
}
```

**Pros:** the AI decides timing too, and can reschedule "next in 90 min" after a suggestion
**Risks:** agent busy → missed; no dedicated sensing pipeline

### Option C — Reactive-only (simplest)

Drop the proactive timer, suggest only when:
1. The user asks directly
2. The user just finished a long session (presence.leave after presence.enter > 2h)
3. Light level drops sharply (dark → chill music mood)

**Trade-off:** less intrusive, but loses the "proactive companion" feel

---

## Recommendation

~~**Short-term (this sprint):** Option A~~ → **Option B** was chosen.

### ✅ Option B — Implemented (2026-04-09)

**Changes made:**

1. **HAL Python:**
   - Removed the `music.mood` timer from `WellbeingPerception` (kept hydration + break)
   - Removed the `WELLBEING_MUSIC_S` config
   - Added per-user audio play history tracking to `MusicService` → JSONL log at `/root/local/users/{person}/audio_history/music_YYYY-MM-DD.jsonl`
   - Added the `GET /audio/history?person={name}` endpoint so the AI can read per-user listening history

2. **Lamp Go server:**
   - Log a `music.play` event into mood history whenever an `/audio/play` HW marker is detected
   - The AI correlates `music.play` with suggestion times to infer accepted/rejected

3. **OpenClaw Skills:**
   - Rewrote `music/SKILL.md` — AI self-schedules via `cron.add`, queries mood-history + audio/history, learns user habits, adjusts timing/genre *(later replaced by the event-driven `skills/music-suggestion/SKILL.md`, no cron)*
   - Updated `sensing/SKILL.md` — removed the reference to the `[sensing:music.mood]` event

4. **Docs:**
   - Updated `sensing-behavior.md` (EN) + `sensing-behavior_vi.md` (VI)

---

## Related files

Current status of the files analyzed at the time:

| File | Layer | Description |
|---|---|---|
| `WellbeingPerception` (HAL, removed) | Trigger | 60-min timer firing music.mood — removed |
| `hal/config.py` | Config | `HAL_WELLBEING_MUSIC_S` — removed |
| `hal/drivers/voice/music_service.py` | Playback | yt-dlp + ffmpeg + ALSA, per-user audio history |
| `hal/routes/music.py` | API | `POST /audio/play` (with `person`), `POST /audio/stop`, `GET /audio/history` |
| `system/skillcontext/mood/mood.go` | Data | Mood history logger |
| `system/server/sensing/delivery/http/handler.go` | Pipeline | Event routing + queueing, music-suggestion log/status |
| `skills/user-emotion-detection/SKILL.md` | AI | Emotion router (shipped design) |
| `skills/music-suggestion/SKILL.md` | AI | Proactive suggestion (shipped design) |
| `skills/music/SKILL.md` | AI | Reactive music, playback |
