# Lamp stuck in sleeping — motion.activity (và mọi event) bị suppress

Observed 2026-04-22 trên Pi (test device). Log lelamp lặp hàng phút:

```
[motion] dedup drop: Activity detected: using computer. (same as last send 246.1s ago)
```

→ Flow Monitor không hiện turn `motion.activity` nào mặc dù user đang thao tác máy tính.

## Root cause

Đèn ở `state._sleeping = True` từ lúc Lamp gửi `emotion=sleepy` (09:46:18, sau câu "Sleep tight. See you later.").

Khi `_sleeping` bật, `sensing_service._send_event` suppress tất cả event ngoại trừ `presence.enter`:

- `/opt/hal/drivers/sensing/sensing_service.py:316-319`
- Repo: `hal/drivers/sensing/sensing_service.py` (cùng logic)

Đồng thời `/opt/hal/routes/emotion.py:40-44`:

```python
if state._sleeping and req.emotion not in _WAKE_EMOTIONS:
    return {"status": "ignored", ...}
state._sleeping = req.emotion == EMO_SLEEPY
```

`_WAKE_EMOTIONS = {greeting, stretching, sleepy}`. Lamp chỉ POST `thinking/happy/curious/acknowledge` → bị bỏ qua sớm, không flip `_sleeping` về False → kẹt sleep vĩnh viễn cho tới khi có `greeting`/`stretching`.

User nói "No. I wake up." cũng không giúp: event `voice` gửi tới sensing xong bị sleep-gate chặn, không tới Lamp.

## Bug phụ: dedup log gây hiểu nhầm

`motion.py` update `_last_sent_ts` **trước** khi event thực sự đi qua sensing. Nếu sensing suppress (sleep), perception vẫn coi là đã gửi → tiếp tục log `dedup drop` mỗi 10 s, nhìn như đây là nguyên nhân chính.

Tham chiếu: `hal/drivers/sensing/perceptions/processors/motion.py:414-425` (sau khi flush message → check dedup window 300 s → set `_last_sent_ts` → gọi `send_event`).

## Evidence — verified 2026-04-22

| Time | Event |
|---|---|
| 09:46:18 | Lamp: "Sleep tight. See you later. [yawn]" + POST `emotion=sleepy` → `_sleeping=True` |
| 09:46:30 | User nói "No. I wake up." → voice event bị sleep-gate chặn |
| 09:46:43 → 10:05:13 | **Kẹt ~19 phút.** Lamp spam `thinking/happy/curious/acknowledge` — tất cả ignored |
| 10:04:14 | `[motion] flushing: Activity detected: reading newspaper` (label khác → pass dedup) nhưng vẫn `sleeping — suppressed motion.activity` → xác nhận dedup không phải bug chính |
| 10:05:13 | lelamp service **restart** (DisplayService/SensingService/VoiceService stop → start) |
| 10:05:24 | Service up, `_sleeping` reset về default False |

→ Đèn thoát sleep **chỉ nhờ service restart**, không phải wake logic. Không có `emotion=greeting/stretching` nào được gửi. Wake path chưa bao giờ hoạt động trong thực tế.

## Trạng thái hiện tại (2026-08-18) — set đã quay lại 3 phần tử, CÓ CHỦ ĐÍCH

Sau sự cố trên, commit `bc12f890` mở rộng set lên 12 (thêm
happy/excited/caring/laugh/curious/sad/shy/shock/confused) để agent trả lời là đèn thức.
Ngày 2026-08-18 việc đó bị **đảo lại**: set thu về đúng 3 và đổi tên thành
`_SLEEP_GATE_ALLOWED` (`hal/routes/emotion.py`) — tên cũ sai bản chất, vì `sleepy` nằm
trong set nhưng không đánh thức, nó chỉ đi qua cổng để re-arm timer auto-release.

Lý do đảo: suy ra "có người vừa tương tác" từ **tên emotion** là sai. Agent chạy xong một
task nền cũ rồi bắn `curious`/`happy` cũng lật được cờ sleep, nên đèn tự bừng dậy giữa lúc
user đã chủ động cho ngủ. Cùng đợt này, các route ghi LED và servo cũng được gate theo
`_sleeping` (xem [led-control.md](../led-control.md) mục "Sleep owns the strip").

Điều kiện đã khác tháng 4 nên rủi ro kẹt không còn như cũ:

| Đường thoát | Tháng 4/2026 | Hiện tại |
|---|---|---|
| Nút GPIO tap 1 cái | chưa có | có — `_wake_if_sleepy()` bắn `stretching` (`hal/drivers/button_actions.py`) |
| `presence.enter` (friend) | — | agent gửi `greeting` → thức (`skills/sensing/SKILL.md`) |
| Restart service | cách duy nhất | vẫn còn, nhưng không còn là cách duy nhất |

**Lỗ đã biết, chấp nhận:** `presence.enter` của **người lạ** map sang `curious`
(`skills/sensing/SKILL.md`), mà `curious` không còn đánh thức → người lạ bước vào thì đèn
nằm im. Đúng ý đồ "ngủ là do-not-disturb", nhưng nếu sau này muốn người lạ cũng đánh thức
thì sửa ở phía skill (đổi sang `greeting`) chứ đừng nới lại `_SLEEP_GATE_ALLOWED` — nới ra
là quay về đúng lỗi 2026-04-22.

## Fix ideas (chọn sau)

1. **Wake từ voice wake-phrase.** Nhận diện "wake up"/"dậy đi" ở lelamp (trước khi bọc sleep gate) và flip `state._sleeping = False` + gọi `greeting` anim. Không chờ Lamp.
2. **Lamp agent: wake flow chuẩn.** Khi user nói gì đó trong lúc lamp sleeping, agent phải POST `emotion=greeting` trước rồi mới `thinking`. Hiện agent không biết state sleep → cần expose `/state` hoặc gửi `sleeping=true` kèm mỗi event.
3. **Không tính dedup cho event bị suppress.** Cho `SensingService.send_event` trả về `False` khi drop vì sleep, và perception chỉ update `_last_sent_ts` khi `True`. Tránh log nhiễu + cho phép burst ngay khi wake.
4. **Timeout sleep.** `_sleeping` auto tắt sau N phút (ví dụ 30 min) để tránh kẹt vĩnh viễn khi Lamp/agent quên wake.

## Escape hatch (thủ công khi gặp)

```bash
curl -X POST http://127.0.0.1:5000/emotion \
  -H 'Content-Type: application/json' \
  -d '{"emotion":"greeting","intensity":0.7}'
```

Chạy trên Pi để flip `_sleeping=False` ngay.
