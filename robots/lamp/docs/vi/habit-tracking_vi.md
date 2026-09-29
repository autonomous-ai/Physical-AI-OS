# Theo dõi thói quen (Habit Tracking)

Habit tracking thêm **hành vi dự đoán** cho hệ thống wellbeing và music của Lamp. Thay vì chỉ phản ứng khi có sự kiện (nhắc theo threshold, nhạc theo mood), Lamp học thói quen cá nhân theo thời gian và hành động chủ động.

## Cách hoạt động

```
Nguồn dữ liệu (input)               Habit skill                    Consumer (output)
─────────────────────                ─────────────                  ──────────────────
Wellbeing logs (sensing)  ──┐                                      Wellbeing Step 3b
  drink, break, enter,      ├──→  Flow A: build patterns  ──→      (enrich phrasing
  leave, sedentary          │      (gọi khi có nudge,                cho nudge)
                            │       self-throttle <6h)
                            │       ↓
                            │    patterns.json               ──→  Music-suggestion
SOUL (conversation)     ──┘       per user                        (genre ưa thích)
  meal, coffee, sleep,
  exercise
```

**Trigger Flow A:** Step 3b của wellbeing chỉ invoke Flow A khi Step 3 fire một threshold nudge (sự kiện hành vi thực sự). Flow A self-throttle bằng mtime — nếu `patterns.json` còn fresh (<6h), trả ngay không recompute. `motion.activity` tick rảnh không bao giờ trigger rebuild.

## Nguồn dữ liệu

Hai nguồn độc lập cùng ghi vào wellbeing JSONL:

### 1. Dữ liệu sensing (qua Wellbeing skill)
Camera phát hiện hành động → HAL tự ghi vào wellbeing JSONL.

| Action | Nguồn |
|--------|-------|
| `drink` | Camera phát hiện uống nước |
| `break` | Camera phát hiện nghỉ |
| `celebrate` | Camera phát hiện ăn mừng |
| `using computer`, `writing`, `reading`, `texting`, `drawing` | Camera phát hiện ngồi yên |
| `enter` / `leave` | Phát hiện hiện diện (backend) |

### 2. Intent từ hội thoại (qua SOUL)
User nhắc đến hoạt động hàng ngày → Lamp âm thầm ghi vào wellbeing JSONL.

| User nói | Action ghi |
|----------|------------|
| "going to lunch", "dinner" | `meal` |
| "coffee break", "grab a coffee" | `coffee` |
| "good night", "going to sleep" | `sleep` |
| "gym", "workout", "going for a run" | `exercise` |

**Quy tắc:** Chỉ ghi khi user nói intent NGAY BÂY GIỜ — không ghi quá khứ hay nói chung chung. Ghi âm thầm, Lamp trả lời tự nhiên.

## Xây dựng Pattern (Flow A)

Habit skill đọc 14–30 ngày wellbeing JSONL và tính patterns (pattern vẫn emit khi `days_observed ≥ 3`, nên user mới có signal sớm từ ngày 4 — window rộng hơn chỉ tăng độ chính xác khi data tích lũy đủ):

1. **Group** events theo `(action, hour)` qua tất cả các ngày
2. **Đếm** tần suất: `số_ngày_xuất_hiện / tổng_ngày`
3. **Tính** phút điển hình (median của phút tại giờ đó)
4. **Gán** strength: weak (<0.5), moderate (0.5–0.75), strong (>0.75)
5. **Ghi** kết quả ra `patterns.json`

### Yêu cầu dữ liệu tối thiểu

| Mục đích | Tối thiểu ngày | Tối thiểu lần |
|----------|----------------|----------------|
| Phát hiện thói quen | 3 | 2 |
| Nhắc chủ động | 5 | 3 |
| Cá nhân hóa nhạc | 3 | 2 accepted |

## Lưu trữ

File per user:
```
/root/local/users/{name}/habit/patterns.json
```

Rebuild khi:
- File chưa tồn tại
- File cũ hơn 6 giờ
- User hỏi về thói quen của mình

### Ví dụ patterns.json

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


## Consumer

### Wellbeing — enrich phrasing nudge (Step 3b)

Khi Step 3 threshold check fire một nudge (uống nước > 45 min? nghỉ > 30 min?), wellbeing invoke habit Flow A. Flow A self-throttle (no-op nếu `patterns.json` < 6h; bootstrap nếu thiếu file và có ≥3 ngày data). `wellbeing_patterns` trả về được dùng để enrich câu nói:

1. Action đang nudge có phải habit moderate+ (`frequency ≥ 0.5`) không?
2. `now` có nằm trong `typical_hour:typical_minute ± window_minutes` của habit đó không?
3. Có → lồng habit context vào câu nói (*"bạn thường uống nước giờ này — ổn không?"*)
4. Không → dùng câu generic từ bảng

Không có nudge habit-only riêng — habit chỉ enrich phrasing cho threshold nudge, không phải trigger thứ hai. Tránh double-nudge và giữ chi phí bootstrap của Flow A trên hot path nudge (rare), không phải trên mỗi `motion.activity` tick.

**Ví dụ:** Hydration timer của Leo vượt threshold lúc 9h15. Flow A trả `drink @ hour=9 typical_minute=10 strength=moderate`. Lamp nói *"bạn thường uống nước giờ này — làm 1 ly nhé?"* thay vì câu generic *"lâu rồi chưa uống nước — làm 1 ly nha?"*.

### Music-suggestion — genre ưa thích (Flow C)

Trước khi chọn genre từ bảng mood mặc định, music-suggestion đọc `patterns.json → music_patterns`:

- Giờ hiện tại khớp `peak_hour ± 1` → dùng `preferred_genre`
- Không khớp → dùng bảng genre mặc định

**Ví dụ:** Leo hay chấp nhận lo-fi lúc 14:00–16:00 → lúc 14:00, suggest lo-fi thay vì chọn theo mood.

## Câu hỏi mở về habit (Flow E)

Khi user hỏi thẳng về thói quen của ai đó (*"What are Leo's habits?"*, *"bạn biết thói quen của Chloe không?"*, *"Notice anything about my patterns?"*), habit skill chạy Flow A trước rồi chọn 1 trong 3 chế độ trả lời theo kết quả Flow A:

| Flow A trả về | Reply mode | Lamp nói gì |
|---|---|---|
| `days_observed ≥ 3` VÀ ≥1 pattern moderate/strong | **Pattern** | Kể tên 2–3 pattern mạnh nhất kèm giờ + tần suất |
| `insufficient_data` HOẶC tất cả pattern weak HOẶC <2 pattern | **Narrative** | Đọc raw `wellbeing/*.jsonl` 7 ngày gần nhất, mô tả hoạt động cụ thể (ngày/giờ/action) — kết bằng câu thành thật là chưa đủ ngày để gọi là habit |
| `insufficient_data` VÀ `patterns.json` cũ > 3 ngày | **Honest-gap** | Thừa nhận thiếu data, không đọc lại pattern stale như là sự thật hiện tại |

Honest-gap mode tồn tại vì freshness guard của Flow A giữ lại `patterns.json` cũ ngay cả khi data hiện tại không đủ. Không có rule này, Lamp sẽ vô tư đọc patterns 2 tuần cũ như là pattern hôm nay.

Flow E **override** OUTPUT RULE 1-câu (chỉ áp dụng cho nudge enrichment): cho phép 2–4 câu, được nói ngày/giờ/tần suất xấp xỉ trong câu thoại. Raw timestamp, JSON, pattern math thô vẫn ở trong `thinking`.

## Kích thước cửa sổ (Window Sizes)

| Action | Cửa sổ |
|--------|--------|
| `drink` | ±30 phút |
| `break` | ±30 phút |
| `meal` | ±45 phút |
| `coffee` | ±30 phút |
| `sleep` | ±30 phút |
| `exercise` | ±60 phút |
| `enter` (đến nơi) | ±45 phút |
| Nhãn sedentary | ±60 phút |

## Test full E2E flow

Validate: Step 1 (đọc history) → Step 2 (tính delta) → Step 3 (fire nudge) → Step 3b (invoke Flow A) → Flow A bootstrap (`patterns.json` được tạo) → Step 4 (speak) → Step 5 (log nudge).

### Điều kiện
- User có ≥3 ngày wellbeing JSONL files (Flow A cần ≥3 ngày).
- Lamp + OpenClaw chạy trên Pi.
- **Reset agent session trước** (file edit không tự propagate vào session đang chạy). Cách: dùng nút "Reset session" trong OpenClaw web monitor cho `agent:main:main`.

### Seed data hôm nay

Append trực tiếp vào file ngày hôm nay (cùng path lelamp ghi). `enter` sáng + `drink` sáng + `using computer` gần đây — tạo hydration delta vượt xa `HYDRATION_THRESHOLD_MIN` (45 phút trong `skills/wellbeing/SKILL.md`).

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

### Fire activity event (cùng path lelamp pipeline)

```bash
curl -s -X POST 'http://<lamp-ip>/api/sensing/event' \
  -H 'Content-Type: application/json' \
  -d '{"type":"motion.activity","message":"Activity detected: using computer.","current_user":"<user>"}'
```

### Hành vi agent kỳ vọng (verified 2026-04-28 trên `lamp-002`)

| Stage | Observed |
|---|---|
| Step 1 query | `GET /api/agent/wellbeing-history?user=gray&last=50` (no slice) |
| Step 2 delta | hydration ~159 phút vs threshold (giá trị test 5 phút lúc đó; production là 45 phút) — vượt |
| Step 3 decision | nudge hydration (ưu tiên hơn break) |
| Step 3b invoke | gọi `habit/SKILL.md` Flow A |
| Flow A guard | mtime check pass (file thiếu → cold path) |
| Flow A bootstrap | đọc 47 ngày wellbeing log, tính patterns |
| `patterns.json` write | `/root/local/users/gray/habit/patterns.json` (18 patterns, all weak — frequency ≤ 0.17) |
| Step 3b match | không có habit moderate+ → dùng generic phrasing |
| Step 4 speak | `<say>You've been at the screen a while. Want some water? [sigh]</say>` |
| Step 5 log | row `nudge_hydration` append vào wellbeing JSONL hôm nay |

### Verify

```bash
ssh pi@<lamp-ip> 'sudo bash -c "
  cat /root/local/users/<user>/habit/patterns.json | jq .updated_at,.days_observed
  tail -1 /root/local/users/<user>/wellbeing/$(date +%F).jsonl | jq .action
"'
# expect: ISO timestamp hôm nay, days_observed ≥ 3, action == \"nudge_hydration\"
```

### Bẫy thường gặp

- **`hour` phải khớp `ts`** — agent đọc field `hour` để hiển thị nhưng dùng `ts` để tính delta. Nếu lệch (vd `ts=11:13` nhưng `hour=12`), agent tính delta sai → skip nudge.
- **Không reset session** thì agent dùng SKILL cached từ session cũ, có thể skip Step 3b — kể cả khi SKILL.md mới đã trên disk.
- **patterns.json bootstrap cần ≥3 day files** trong `wellbeing/` (Flow A freshness guard exit sớm với `insufficient_data`).
- **Không có nudge → không có Flow A** theo design; bootstrap chỉ chạy khi có behavioral inflection thật. Muốn test Flow A độc lập, chạy bash guard tay.

## Web Monitor

Tab Users hiện badge **habit** cho mỗi user khi `patterns.json` tồn tại. File xem được trong folder tree `habit/patterns.json`.

## Files

| File | Mục đích |
|------|----------|
| `skills/habit/SKILL.md` | Skill definition — Flow A–D, algorithm, storage |
| `robots/lamp/SOUL.md` | Section "Observing Habits" — ghi intent từ hội thoại |
| `skills/wellbeing/SKILL.md` | Step 3b — invoke Flow A khi có nudge và `bootstrap_needed=true`; dùng patterns.json để enrich phrasing nudge |
| `system/skills/catalog_gen.go` | Đăng ký habit vào skill catalog |
| `hal/models.py` | Field `habit_patterns` trong FacePersonDetail |
| `hal/routes/sensing.py` | Check habit/patterns.json trong face/owners API |
| `system/web/src/pages/monitor/face-owners/PersonCard.tsx` | Habit badge + folder trong tab Users |
