# Gợi Ý Nhạc Chủ Động (Music Suggestion)

> Lamp chủ động gợi ý nhạc phù hợp với mood của người dùng — **không auto-play**, chỉ gợi ý bằng giọng nói và chờ xác nhận.
>
> Bản tiếng Anh: [../music-suggestion.md](../music-suggestion.md)

---

## Tổng quan

Agent **tự quyết định thời điểm** gợi ý nhạc. Thiết kế là **event-driven** — không có cron job, không có timer ở backend:

- **Mood trigger (trigger duy nhất)** — `skills/user-emotion-detection/SKILL.md` là router cho event `emotion.detected` (camera) và `speech_emotion.detected` (giọng nói). Router chọn đúng một route mỗi turn (`music` / `checkin` / `action` / `silent`); `skills/music-suggestion/SKILL.md` chỉ tạo output khi router chọn `music`.
- **Activity event không bao giờ gợi ý nhạc** — event `motion.activity` / `[activity]` (sedentary, drink/break, celebrate) chỉ route sang `skills/wellbeing/SKILL.md`.
- **Suggestion history** — lưu lại mỗi lần suggest và việc user accept/reject, để AI học pattern.

Hoàn toàn **AI-driven**: agent tự quyết định dựa trên SKILL.md. Backend tính sẵn context và lưu/trả history.

---

## Luồng trigger

```
emotion.detected (camera) / speech_emotion.detected (giọng nói)
    ↓
Backend inject [context: current_user=X] + [emotion_context: {...}]
    ↓
user-emotion-detection/SKILL.md (router): log mood signal/decision (HW marker),
chọn MỘT route: music | checkin | action | silent
    ↓ (music)
Cổng của music-suggestion/SKILL.md — phải thỏa tất cả:
  ├── suggestion_worthy == true (mood ∈ sad, stressed, tired, excited, happy, bored)
  ├── audio_playing == false
  ├── last_suggestion_age_min ∉ [0, 7)   (cooldown, dùng chung với checkin)
  └── is_decision_stale == false, hoặc turn này vừa tổng hợp decision mới
    ↓ (pass hết)
Chọn genre: music_pattern_for_hour → bảng genre mặc định → override theo audio history
    ↓
Một câu nói + [HW:/emotion] (+ [HW:/dm] cho user quen)
+ [HW:/music-suggestion/log:{...}]  → POST /api/music-suggestion/log
```

### Context tính sẵn (`[emotion_context: ...]`)

Do `BuildEmotionContext` trong `system/skillcontext/emotion.go` dựng. Khi có block này agent **không gọi tool đọc nào**:

| Field | Thay cho |
|-------|----------|
| `audio_playing` | `GET /audio/status` |
| `last_suggestion_age_min` (`-1` nếu hôm nay chưa có) | music-suggestion history `last=1` |
| `prior_decision` + `is_decision_stale` | mood-history `kind=decision&last=1` |
| `audio_recent` (`{track,duration_s,stopped}`) | `GET /audio/history?last=1` |
| `music_pattern_for_hour` | đọc `habit/patterns.json` theo giờ hiện tại ±1 |
| `suggestion_worthy`, `mapped_mood` | cổng mood bucket đã áp sẵn |

Chỉ khi thiếu block này skill mới fallback sang batch GET song song (audio status, suggestion history, mood history, audio history, `patterns.json`).

### Unknown Users

Unknown users (người lạ) vẫn được suggest nhạc. Data lưu trong `/root/local/users/unknown/`. Khác biệt duy nhất: chỉ nói qua loa, không DM Telegram (không có telegram_id), và `/audio/play` bỏ field `person`.

### Cooldown

- **7 phút** trong skill hiện tại (`last_suggestion_age_min ∉ [0, 7)`); skill ghi chú cần tăng lên **30 phút** trước khi lên production.
- Backend tính từ suggestion log hôm nay — agent không check bằng tool call.
- Dùng chung với route `checkin` của router (cả hai log qua `music-suggestion/log`).

---

## Suggestion History

### Storage

`/root/local/users/{user}/music-suggestions/{YYYY-MM-DD}.jsonl`

Mỗi record:
```json
{"ts":1713359400.5,"seq":1713359400500000000,"hour":14,"trigger":"mood:tired","query":"","message":"How about some calm piano?","status":"pending","user":"gray"}
```

| Field | Ý nghĩa |
|-------|---------|
| `seq` | Unix nanoseconds — unique ID cho mỗi suggestion |
| `trigger` | Nguồn trigger: `mood:<mood>` (trigger gợi ý duy nhất); route check-in của router log `checkin:<emotion>` |
| `query` | YouTube search query (empty nếu chỉ text suggestion) |
| `message` | Text suggestion gửi cho user |
| `status` | `pending` → `accepted` / `rejected` / `expired` |

### API

| Endpoint | Method | Mục đích |
|----------|--------|----------|
| `/api/music-suggestion/log` | POST | Ghi suggestion (thường qua marker inline `[HW:/music-suggestion/log:{...}]`) |
| `/api/music-suggestion/status` | POST | Update status (`accepted` / `rejected`) theo `day` + `seq` |
| `/api/agent/music-suggestion-history` | GET | Query history (params: `user`, `date`, `last`; cần admin auth) |

### Retention

7 ngày — tự động xóa files cũ hơn.

---

## Luồng hoạt động chi tiết

### User confirm → Play music

```
User nói "ừ phát đi" (voice hoặc Telegram reply)
    ↓
Agent:
  1. POST /api/music-suggestion/status → status="accepted"
  2. [HW:/audio/play:{"query":"...","person":"gray"}]
    ↓
Go handler intercept HW markers → POST /audio/play → HAL
    ↓
HAL: yt-dlp search → ffmpeg → ALSA speaker
```

### User reject → Log rejection

```
User nói "không" hoặc "not now"
    ↓
Agent: POST /api/music-suggestion/status → status="rejected"
```

User lờ đi thì không update status.

---

## Các layer và file liên quan

### Go server (os-server)

| File | Vai trò |
|------|---------|
| `system/skillcontext/musicsuggestion/suggestion.go` | Logger JSONL per-user per-day: Log, Query, UpdateStatus, retention |
| `system/skillcontext/emotion.go` | `BuildEmotionContext` — dựng block `[emotion_context: ...]` |
| `system/skillcontext/mood/mood.go` | Logger mood events |
| `system/server/sensing/delivery/http/handler.go` | Handler `PostMusicSuggestionLog` / `PostMusicSuggestionStatus` |
| `system/server/agent/delivery/http/handler_api_history.go` | Handler GET `MusicSuggestionHistory` |
| `system/server/agent/delivery/http/handler_hw.go` | Route marker `[HW:/music-suggestion/...]` → `POST :5000/api/music-suggestion/...` |
| `system/server/server.go` | Routes: `/api/music-suggestion/*`, `/api/agent/music-suggestion-history` |

### Agent skills

| File | Vai trò |
|------|---------|
| `skills/user-emotion-detection/SKILL.md` | Router cảm xúc — chọn music / checkin / action / silent |
| `skills/music-suggestion/SKILL.md` | Gợi ý chủ động: cổng, chọn genre, log, học |
| `skills/music/SKILL.md` | Nhạc reactive (user yêu cầu), phát nhạc |
| `skills/mood/SKILL.md` | Log mood |

### HAL (Python)

| File | Vai trò |
|------|---------|
| `hal/models.py` | `FacePersonDetail`: có `music_suggestion_days` |
| `hal/routes/sensing.py` | Endpoint `/face/owners`: đọc `music_suggestion_days` từ thư mục JSONL |
| `hal/routes/music.py` | `/audio/play`, `/audio/stop`, `/audio/status`, `/audio/history` |

### Frontend (React)

| File | Vai trò |
|------|---------|
| `system/web/src/pages/monitor/types.ts` | Field `music_suggestion_days` |
| `system/web/src/pages/monitor/face-owners/PersonCard.tsx` | Hiển thị badge music-suggestion-days + folder tree |

---

## Dữ liệu AI sử dụng

### Music suggestion history

| Field | Dùng để |
|-------|---------|
| `trigger` | Biết suggestion đến từ mood nào |
| `status` | Learn accept/reject pattern |
| `hour` | Pattern thời gian nào user hay accept |
| `message` | Tránh suggest trùng lặp |

### Audio history (`audio_recent`, hoặc fallback `GET /audio/history?person={name}&last=1`)

| Field | Dùng để |
|-------|---------|
| `query` / `track` | Genre/artist signal |
| `duration_s` | Satisfaction: > 180s = enjoyed |
| `stopped_by` | `"end"` = liked, `"user"` < 30s = disliked |

### Learning Rules

- Bài hát hết tự nhiên + nghe > 3 phút → suggest artist/genre tương tự
- User tự dừng + nghe < 30s → thử hướng khác
- Nhiều `rejected` trong suggestion history → giảm tần suất / đổi cách tiếp cận

---

## Speaker conflict

Lamp chỉ có 1 speaker chia sẻ giữa TTS và music:

| Tình huống | Hành vi |
|-----------|---------|
| AI suggest bằng giọng nói | TTS nói suggestion → user nghe |
| User confirm → play | TTS bị suppress để không đè lên nhạc |
| Music đang play + TTS | HAL trả 409 — music giữ priority |
| User nói "stop" | `[HW:/audio/stop:{}]` → dừng music |

---

## Monitoring & Debug

### API kiểm tra

```bash
# Suggestion history hôm nay (cần admin auth)
curl -s -H "Authorization: Bearer <admin-token>" "http://<LAMP_IP>:5000/api/agent/music-suggestion-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Mood history (cần admin auth)
curl -s -H "Authorization: Bearer <admin-token>" "http://<LAMP_IP>:5000/api/agent/mood-history?user=gray&date=$(date +%Y-%m-%d)&last=50"

# Audio status
curl -s "http://<LAMP_IP>:5001/audio/status"

# Audio history
curl -s "http://<LAMP_IP>:5001/audio/history?person=gray&last=10"
```

### Web UI

Monitor page → Users section → click vào user → xem `music-suggestions/` folder → click ngày để xem chi tiết.

### Logs

```bash
journalctl -u os-server | grep -i "suggestion\|music"
```
