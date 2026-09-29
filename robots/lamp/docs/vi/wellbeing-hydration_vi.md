# Chăm Sóc Sức Khỏe Chủ Động (Wellbeing — Hydration + Break)

> Lamp chủ động nhắc uống nước và nghỉ ngơi. Thiết kế là **event-driven**: mỗi lần nhắc được quyết định khi có event `motion.activity`, dựa trên activity log của từng người. **Không có cron job** và không có timer nhắc nhở.
>
> Bản tiếng Anh: [../wellbeing-hydration.md](../wellbeing-hydration.md)
>
> Spec hành vi đầy đủ (dedup, attribution, presence marker) nằm ở [sensing-behavior_vi.md § Wellbeing](sensing-behavior_vi.md). Bản thân skill là `skills/wellbeing/SKILL.md`.

---

## Tổng quan

| Loại | Mục đích | Ngưỡng (production) | Điểm reset |
|------|----------|---------------------|------------|
| **Hydration** | Nhắc uống nước | `HYDRATION_THRESHOLD_MIN = 45` | `drink`, `enter`, `nudge_hydration` gần nhất |
| **Break** | Nhắc đứng dậy/vươn vai | `BREAK_THRESHOLD_MIN = 30` (`BREAK_THRESHOLD_TIRED = 20` khi label có `yawning`) | `break`, `enter`, `nudge_break` gần nhất |

Hằng số khác trong skill: `YAWN_ACK_COOLDOWN_MIN = 60`, `TOILET_DRINK_THRESHOLD = 2` (theo số lần: một lần nhắc đi vệ sinh mỗi N lần uống kể từ lần nhắc trước).

**Đặc điểm:**
- Per-user: mỗi người quen có timeline riêng trong `/root/local/users/{name}/wellbeing/`. Người lạ gộp chung vào `"unknown"`.
- Nhắc cho tất cả — friend và stranger đều được chăm sóc.
- User luôn lấy từ tag `[context: current_user=X]`, không bao giờ tự suy đoán.
- Agent không phải lúc nào cũng nói — phần lớn turn kết thúc bằng `NO_REPLY`.

---

## Luồng hoạt động

### 1. HAL ghi activity rồi mới fire event

```
Camera → nhận diện hoạt động (MotionPerception)
    ↓
HAL POST activity row lên /api/wellbeing/log   (drink / break / yawning /
    ↓                                           raw label sedentary / eat label)
HAL fire motion.activity → os-server
    ↓
os-server gắn [context: current_user=X] + [wellbeing_context: {...}]
    ↓
Agent: activity router của skills/wellbeing/SKILL.md
```

Presence row (`enter` / `leave`) do face recognizer của HAL ghi (`hal/drivers/sensing/perceptions/processors/faceid/perception.py`). HAL dedup activity trong 5 phút theo key `(current_user, labels)` (`MOTION_DEDUP_WINDOW_S = 300`), nên agent vẫn được "đánh thức" định kỳ kể cả khi không có gì thay đổi.

### 2. Context tính sẵn

`BuildWellbeingContext` (`system/skillcontext/wellbeing.go`) inject `[wellbeing_context: {...}]` vào mọi turn `motion.activity`, nên agent **không gọi tool đọc nào**. Field chính: `hydration_delta_min`, `break_delta_min` (`-1` = hôm nay chưa có reset), `count_today`, `time_of_day`, `current_hour`, `first_activity_today`, các field meal window, `yawn_ack_age_min`, `drinks_since_toilet_nudge`, `patterns` (từ habit `patterns.json`), `bootstrap_needed`, `last_posture_nudge_age_min`. Chuỗi ngồi lâu có thể kèm thêm `[posture_summary: ...]`.

### 3. Activity router (khớp đầu tiên thắng, một route mỗi turn)

| # | Điều kiện | Route |
|---|-----------|-------|
| 1 | label có `drink`, `break`, `celebrate` hoặc raw eat label | reaction (một câu ghi nhận ngắn, không log marker) |
| 1b | `yawning` và hết cooldown ngáp, trước 21:00 | yawn reaction + `noted_yawn` |
| 2 | activity đầu tiên hôm nay, 05:00–11:00, chưa chào | morning greeting |
| 3 | sau 21:00, sedentary/`yawning`, chưa wind-down | sleep wind-down |
| 4 | trong meal window mà chưa có meal signal | meal reminder |
| 5 | có `[posture_summary]` | posture nudge (posture log) |
| 6 | `hydration_delta_min >= 45` | hydration nudge + `nudge_hydration` |
| 7 | `break_delta_min >= 30` (hoặc `>= 20` khi có `yawning`) | break nudge + `nudge_break` |
| 8 | `drinks_since_toilet_nudge >= 2` | toilet nudge + `nudge_toilet` |
| 9 | còn lại | `NO_REPLY` |

Agent log nudge bằng marker inline, ví dụ `[HW:/wellbeing/log:{"action":"nudge_hydration","notes":"...","user":"<current_user>"}]` → `POST /api/wellbeing/log`. Row `nudge_*` là điểm reset tiếp theo, nên cùng loại nhắc chỉ fire lại sau một threshold window đầy đủ — không cần cooldown riêng.

```
10:45  quá hạn hydration → nhắc 💧 + log nudge_hydration → hydration delta = 0
11:20  wake-up → delta = 35 phút < 45 → NO_REPLY
11:30  wake-up → delta = 45 phút ≥ 45 → nhắc 💧 lần nữa (user vẫn chưa uống)
```

Nếu user uống nước hoặc nghỉ giữa chừng, row `drink` / `break` của HAL reset delta.

### 4. Enrich bằng habit

Khi fire hydration/break nudge mà `bootstrap_needed=true`, skill gọi habit Flow A để dựng `patterns.json`; pattern sau đó làm câu nhắc cá nhân hơn ("bạn thường uống nước giờ này"). Xem [habit-tracking_vi.md](habit-tracking_vi.md).

### 5. Presence leave / away

HAL ghi row `leave`. Không có gì để cancel — agent im lặng (`NO_REPLY`).

---

## Dữ liệu per-user

```
/root/local/users/{name}/
  ├── wellbeing/2026-04-10.jsonl   ← timeline activity + nudge (giữ 30 ngày)
  ├── posture/2026-04-10.jsonl     ← posture nudge/praise
  ├── mood/2026-04-10.jsonl        ← mood history
  └── habit/patterns.json          ← pattern thói quen đã học
```

Định dạng row: `{"ts": 1776658657.23, "seq": 42, "hour": 11, "action": "sedentary", "notes": ""}`.

Action do agent ghi: `nudge_hydration`, `nudge_break`, `nudge_toilet`, `morning_greeting`, `sleep_winddown`, `meal_reminder`, `noted_yawn` (wellbeing log), `nudge_posture`, `praise_posture` (posture log). Mọi thứ còn lại do HAL ghi.

---

## Các layer và file liên quan

| File | Vai trò |
|------|---------|
| `skills/wellbeing/SKILL.md` (+ `reference/*.md`) | Activity router, ngưỡng, câu nói, quy tắc log |
| `skills/habit/SKILL.md` | Flow A dựng pattern để enrich câu nhắc |
| `system/skillcontext/wellbeing/wellbeing.go` | Logger JSONL per-user, dedup presence, giữ 30 ngày |
| `system/skillcontext/wellbeing.go` | `BuildWellbeingContext` — block `[wellbeing_context]` |
| `system/lib/sensingmsg/sensingmsg.go` | Gắn context vào message `motion.activity` |
| `system/server/server.go` | `POST /api/wellbeing/log`, `POST /api/posture/log`, `GET /api/agent/wellbeing-history` |
| `hal/drivers/sensing/perceptions/processors/motion.py` | Nhận diện hoạt động, dedup 5 phút, POST activity row |
| `hal/drivers/sensing/perceptions/processors/faceid/perception.py` | POST row `enter` / `leave` |
| `system/web/src/pages/monitor/UserTimelineModal.tsx` | Timeline per-user (wellbeing, mood, music suggestion, posture) |

---

## Cách test

Điều kiện: os-server (port 5000), HAL (port 5001) có camera hoạt động, agent đã kết nối.

1. **Hydration nudge** — ngồi máy tính, không uống nước. Sau 45 phút (hoặc tạm hạ `HYDRATION_THRESHOLD_MIN` trong skill), `motion.activity` kế tiếp tạo một câu nhắc uống nước và một row `nudge_hydration`.
2. **Reaction** — uống nước trước camera. HAL log `drink`; agent đáp một câu ghi nhận ngắn và không ghi row.
3. **Không spam nhắc lại** — sau một lần nhắc, các event trong window kết thúc bằng `NO_REPLY`.
4. **Multi-user** — hai người quen có timeline `/root/local/users/<name>/wellbeing/` riêng; người lạ vào `unknown`.

Nhớ trả ngưỡng production sau khi test.

```bash
# Timeline hôm nay (cần admin auth)
curl -s -H "Authorization: Bearer <admin-token>" \
  "http://<LAMP_IP>:5000/api/agent/wellbeing-history?user=<name>&last=50"

# Trên thiết bị
cat /root/local/users/<name>/wellbeing/$(date +%Y-%m-%d).jsonl
journalctl -u os-server | grep -i "wellbeing"
```

---

## Lịch sử: thiết kế dựa trên cron (đã bỏ)

Phiên bản trước (tháng 4/2026) để agent tạo hai OpenClaw cron job cho mỗi user (`Wellbeing: {name} hydration` mỗi 45 phút, `... break` mỗi 30 phút) khi thấy hoạt động tĩnh, reset bằng `cron.remove` / `cron.add` khi user uống nước hay vươn vai, đánh giá mỗi lần fire bằng ảnh camera, cancel khi `presence.leave`, và ghi notebook `wellbeing.md` dạng text tự do. Trước nữa là timer Python cứng `WellbeingPerception` fire event `wellbeing.hydration` / `wellbeing.break`. Cả hai đã được thay bằng thiết kế event-driven ở trên: không còn cron sót lại sau reboot, delta tính xác định từ log, và không tốn chi phí chụp ảnh mỗi lần fire.
