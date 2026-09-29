# Lamp — Music Suggestion Feature Analysis
> Generated: 2026-04-09 | Scope: tính năng "suggest music" riêng biệt
>
> Bản tiếng Anh: [../lamp-music-suggestion-analysis.md](../lamp-music-suggestion-analysis.md)
>
> **Tài liệu phân tích lịch sử.** Thiết kế đã ship khác với cả timer HAL mô tả bên dưới lẫn cron của Option B: gợi ý nhạc là **event-driven, chỉ theo mood, không cron**. `skills/user-emotion-detection/SKILL.md` route event `emotion.detected` / `speech_emotion.detected` sang `skills/music-suggestion/SKILL.md` với context tính sẵn `[emotion_context: ...]` và cooldown 7 phút (dự kiến 30 phút cho production); event activity chỉ đi sang wellbeing. Timer `WellbeingPerception` và `HAL_WELLBEING_MUSIC_S` đã bị gỡ. Xem [music-suggestion_vi.md](music-suggestion_vi.md) cho thiết kế hiện tại.

---

## Tổng quan

Music suggestion là tính năng Lamp chủ động đề xuất nhạc phù hợp với mood/trạng thái của user — **không auto-play**, chỉ gợi ý bằng giọng nói và chờ xác nhận.

---

## Luồng hoạt động lúc đó (tính đến 2026-04-09)

```
[HAL - Python]
  WellbeingPerception timer (60 min)
        ↓ fire music.mood event
  POST /api/sensing/event  →  [Lamp Go server]
        ↓
  sensing handler → mood.Log() → QueuePendingEvent (nếu busy)
        ↓
  SendChatMessageWithImageAndRun → [OpenClaw agent]
        ↓
  sensing SKILL + music SKILL
        ↓
  LLM nhìn ảnh → assess mood → suggest 1-2 bài
        ↓
  User confirm ("yes", "play that") → HW:/audio/play:{query,person}
        ↓
  [HAL]
  POST /audio/play → MusicService.play(query, person)
        ↓
  yt-dlp search YouTube → ffmpeg → ALSA output device
```

---

## Chi tiết từng layer

### Layer 1 — Trigger (HAL Python)

**File (đã gỡ):** `WellbeingPerception` trong HAL sensing

- Timer chạy mỗi **60 phút** (default, config qua `HAL_WELLBEING_MUSIC_S`)
- Chỉ fire khi `PresenceState.PRESENT`
- Chụp ảnh (`capture_stable_frame`) → gửi kèm event
- Message được hardcode: *"User has been here for X min. Look at image — assess mood and suggest 1-2 songs..."*
- **Hardcode, không adaptive, không học thói quen**

### Layer 2 — Event Pipeline (Lamp Go server)

**File:** `system/server/sensing/delivery/http/handler.go`

- Nhận `POST /api/sensing/event` với `type: "music.mood"`
- Log vào mood history (`mood.Log`)
- Nếu agent đang busy → `QueuePendingEvent` (replay sau)
- Forward đến OpenClaw agent kèm ảnh

### Layer 3 — AI Decision (OpenClaw skill)

**Files:**
- `skills/sensing/SKILL.md` — nhận `[sensing:music.mood]` (lúc đó)
- `skills/music/SKILL.md` — mood → music mapping (lúc đó)

**Logic LLM:**

| Trạng thái | Hành động |
|---|---|
| Không thấy user trong ảnh | `NO_REPLY` |
| User đang họp/video call | `NO_REPLY` |
| Focused/working | Suggest lo-fi, ambient |
| Tired/fatigued | Suggest calm piano, acoustic |
| Happy/energetic | Suggest upbeat pop, jazz |
| Stressed/tense | Suggest soft jazz, classical |
| Relaxed/chill | Suggest bossa nova, R&B |

**Quy tắc quan trọng:**
- **KHÔNG auto-play** — chỉ nói gợi ý, chờ user confirm
- Max 2 bài mỗi lần suggest
- Ngôn ngữ conversational, không "based on analysis..."

### Layer 4 — Playback (HAL Python)

**File:** `hal/drivers/voice/music_service.py`

- `POST /audio/play` → `MusicService.play(query, person)` — per-user history at `/root/local/users/{person}/audio_history/`
- yt-dlp search YouTube → resolve audio URL
- ffmpeg stream → ALSA device (plughw:CARD,0 hoặc default)
- TTS có priority: nếu TTS đang nói → đợi xong mới play
- Music đang play → TTS request bị reject (HTTP 409)

---

## Mood History (data có sẵn nhưng chưa dùng)

**File:** `system/skillcontext/mood/mood.go`

Log 2 loại event vào `/root/local/mood_YYYY-MM-DD.jsonl` (lúc đó; nay là per-user `/root/local/users/{user}/mood/YYYY-MM-DD.jsonl`):
1. **Sensing input:** `music.mood`, `presence.enter`, `wellbeing.break`, etc.
2. **`mood.assessed`:** Kết quả LLM — `emotion`, `source`, `response`, `no_reply` flag

**Query API:**
```bash
curl -s "http://127.0.0.1:5000/api/agent/mood-history?date=$(date +%Y-%m-%d)&last=100"
```

**Vấn đề:** Data thu thập được nhưng **không có component nào đọc lại để adjust timing hay personalize gợi ý**.

---

## Vấn đề hiện tại

### 1. Timer cứng, không adaptive
- Luôn fire mỗi 60 phút bất kể user có đang cần hay không
- Không biết user thường thích nhạc lúc mấy giờ
- Config chỉ qua env var, cần restart để thay đổi

### 2. Music context nghèo
- LLM chỉ nhìn **1 ảnh snapshot** tại thời điểm fire
- Không biết user đang làm gì suốt 60 phút trước đó
- Không có listening history ("bài nào đã nghe, genre nào user thích")

### 3. Vòng lặp học chưa đóng
- Mood history log đầy đủ (`no_reply`, `emotion`, hour...)
- Nhưng không có component nào đọc: "lần trước suggest lúc 15h → NO_REPLY liên tục → có thể user không thích nhạc buổi chiều"

### 4. Broadcast Telegram khi music.mood (0x0409 đã thêm)
- Commit `0b690cf`: broadcast music.mood agent response ra Telegram
- Tiện nhưng có thể gây noise nếu fire nhiều

---

## Đề xuất cải tiến

### Option A — Hybrid (ít risk nhất)

Giữ timer ở HAL, thêm API để AI điều chỉnh:

```
HAL timer fire (60 min default)
        ↓
AI decide → cũng ghi lại trong MEMORY.md: "User thường muốn nhạc lúc tối"
        ↓
Adaptive cron (1 lần/ngày): AI đọc mood history → suggest interval mới
        ↓
AI call: POST /sensing/wellbeing/config  {"music_interval_s": 7200}
        ↓
HAL update interval tại runtime
```

**Cần thêm:**
- `POST /sensing/wellbeing/config` endpoint (HAL)
- Skill instruction để AI biết cách call endpoint này

### Option B — Full AI-driven (clean hơn, risk cao hơn)

Bỏ WellbeingPerception timer cho music.mood, thay bằng OpenClaw cron:

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

**Ưu điểm:** AI tự decide cả timing, có thể reschedule "next in 90 min" sau khi suggest  
**Rủi ro:** Agent busy → miss; không có dedicated sensing pipeline

### Option C — Reactive-only (simplest)

Bỏ proactive timer, chỉ suggest khi:
1. User hỏi trực tiếp
2. User vừa finish một session dài (presence.leave sau presence.enter > 2h)
3. Light level giảm mạnh (tối → chill music mood)

**Trade-off:** Ít intrusive hơn, nhưng mất tính "proactive companion"

---

## Recommendation

~~**Short-term (sprint này):** Option A~~ → Đã chọn **Option B**.

### ✅ Option B — Implemented (2026-04-09)

**Thay đổi đã thực hiện:**

1. **HAL Python:**
   - Xóa `music.mood` timer khỏi `WellbeingPerception` (giữ hydration + break)
   - Xóa config `WELLBEING_MUSIC_S`
   - Thêm per-user audio play history tracking vào `MusicService` → JSONL log tại `/root/local/users/{person}/audio_history/music_YYYY-MM-DD.jsonl`
   - Thêm `GET /audio/history?person={name}` endpoint để AI đọc per-user listening history

2. **Lamp Go server:**
   - Log `music.play` event vào mood history mỗi khi `/audio/play` HW marker được detect
   - AI correlate `music.play` với thời điểm suggest để biết accepted/rejected

3. **OpenClaw Skills:**
   - Viết lại `music/SKILL.md` — AI tự schedule via `cron.add` *(về sau đã thay bằng `skills/music-suggestion/SKILL.md` event-driven, không cron)*, tự query mood-history + audio/history, tự learn thói quen user, tự adjust timing/genre
   - Cập nhật `sensing/SKILL.md` — bỏ reference đến `[sensing:music.mood]` event

4. **Docs:**
   - Cập nhật `sensing-behavior.md` (EN) + `sensing-behavior_vi.md` (VI)

---

## Files liên quan

Trạng thái hiện tại của các file được phân tích lúc đó:

| File | Layer | Mô tả |
|---|---|---|
| `WellbeingPerception` (HAL, đã gỡ) | Trigger | Timer 60 phút fire music.mood — đã bỏ |
| `hal/config.py` | Config | `HAL_WELLBEING_MUSIC_S` — đã bỏ |
| `hal/drivers/voice/music_service.py` | Playback | yt-dlp + ffmpeg + ALSA, per-user audio history |
| `hal/routes/music.py` | API | `POST /audio/play` (có `person`), `POST /audio/stop`, `GET /audio/history` |
| `system/skillcontext/mood/mood.go` | Data | Mood history logger |
| `system/server/sensing/delivery/http/handler.go` | Pipeline | Event routing + queueing, music-suggestion log/status |
| `skills/user-emotion-detection/SKILL.md` | AI | Router cảm xúc (thiết kế đã ship) |
| `skills/music-suggestion/SKILL.md` | AI | Gợi ý chủ động (thiết kế đã ship) |
| `skills/music/SKILL.md` | AI | Nhạc reactive, phát nhạc |
