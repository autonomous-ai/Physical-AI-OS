# Telemetry của thiết bị

Thiết bị tự báo số liệu về chính nó bằng cách nào. Đọc file này TRƯỚC khi thêm
tracker mới; `docs/vi/voice-metrics_vi.md` chỉ là một tracker dựng trên nền
này, không phải bản hợp đồng.

## Dùng để làm gì

Một tracker trả lời một câu hỏi về thiết bị mà ngồi ở laptop không trả lời
được: đèn mất bao lâu để đáp lời, bản cập nhật có áp dụng được không, một cú
chạy servo thực sự kéo dài bao lâu. Nó ghi mọi quan sát vào log của chính máy,
và khi được cấu hình thì gửi đúng những dòng đó lên Autonomous Analytics (AA).

Nó **không phải** event bus, không phải logger để debug, và không phải chỗ
stream dữ liệu từng frame. Mỗi việc xảy ra = một dòng.

## Các mảnh

| Lớp | Đường dẫn | Bạn có sửa không? |
|-----|-----------|-------------------|
| Tracker (đo một thứ) | `hal/telemetry/<tên>.py` | **Có — mỗi câu hỏi một file** |
| Ống ở HAL | `hal/telemetry/client.py` | Không |
| Hook playback dùng chung | `hal/telemetry/tts_hooks.py` | Chỉ khi thêm hook mới |
| Nhận ở OS | `POST /api/telemetry/event` (`system/server/telemetry/…`) | Không |
| Ống ở OS | `system/telemetry/telemetry.go` | Không |
| Transport AA | `system/lib/analytics` | Không |

## Thêm một tracker

```python
# hal/telemetry/ota_metrics.py
from hal.telemetry import client

client.report("ota_update", {
    "from_version": "1.2.3",
    "to_version": "1.2.4",
    "duration_ms": 48210,
    "outcome": "applied",          # applied | rolled_back | failed
})
```

Tích hợp chỉ có vậy. Từ os-server thì tương đương là
`telemetry.Report(telemetry.Event{Name: ..., ID: ..., Params: ...})`.

Ống đã lo sẵn, không tracker nào phải viết lại:

- ghi dòng đó ra log local **trước khi** gửi
- hàng đợi có giới hạn, gửi ở luồng nền — `report()` không bao giờ chặn hay ném lỗi
- chống trùng theo `event_id`
- đếm số bị drop / gửi lỗi và đính kèm con số đó vào các event sau
- os-server tự thêm field chung: `os_version`, `device_type`, `agent_runtime`, cờ policy
- không cấu hình endpoint thì không gửi gì cả

## Cấu hình

Hai key nằm trong `.env` của body (`robots/<device>/rootfs/opt/hal/.env`, khi
deploy là `/opt/hal/.env`):

| Key | Ý nghĩa |
|-----|---------|
| `AUTONOMOUS_ANALYTICS_URL` | Nơi bắn dòng dữ liệu — **đồng thời là công tắc**. Rỗng (mặc định) = không có gì rời khỏi thiết bị. Không có endpoint mặc định trong code. |
| `AUTONOMOUS_ANALYTICS_ID` | Key authorization của AA. |

Tắt không có nghĩa là mù: mọi thứ vẫn nằm trong log.

JSON local của HAL chứa `event_id` của envelope gửi đi bên cạnh các tham số,
để ghép event gốc với amendment khi đọc offline. Không thêm transcript hay
nội dung giọng nói vào telemetry.

```bash
journalctl -u hal -f | grep '\[telemetry\]'        # HAL: mọi dòng + lỗi POST
journalctl -u os-server -f | grep '\[telemetry\]'  # os-server: đã gửi / drop / lỗi
```

## Các luật đổi bằng bug mới có

Mỗi luật dưới đây đến từ một lỗi thật ở tracker đầu tiên. Theo thì rẻ, tự khám
phá lại thì đắt.

**1. Không bao giờ suy ra sự thật từ trạng thái đọc muộn hơn.**
Hỏi "lúc đó điều này có đúng không", đừng hỏi "bây giờ nó có đúng không". Hai
bug đến từ việc phá luật này: audio bị quy cho lượt nào đang mở tại thời điểm
đó (thay vì lượt thật sự sở hữu nó), và loa bị mute được phát hiện bằng cách
đọc cờ mute mười giây sau — lúc đó có người đã bật tiếng lại, nên một lượt bị
tắt tiếng bị ghi thành lượt không thèm trả lời. Cách chữa của cả hai giống
nhau: ghi nhận ngay tại nơi sự kiện xảy ra, mang theo chủ sở hữu tường minh.

**2. Không biết thì để là không biết.**
Khi không xác định được chủ sở hữu hay giá trị, ghi `unknown` / `null` và đếm
nó. Đừng đoán, và cũng đừng vứt dòng đó đi. Một chỉ số lặng lẽ bỏ những gì nó
không giải thích được sẽ luôn đẹp hơn sự thật.

**3. Đo kết quả, không đo lời yêu cầu.**
HTTP 200 không chứng minh điều gì đã xảy ra. Tracker voice đo frame audio đầu
tiên được ghi vào stream, vì `speak_queue()` cố tình trả thành công cho cả câu
nó sắp bỏ. Đo gì cũng vậy: tìm cho ra điểm mà sự việc thật sự diễn ra.

**4. Định nghĩa mẫu số TRƯỚC khi viết code.**
Dòng nào được tính, dòng nào loại, và vì sao. Mọi loại trừ đều được báo cáo kèm
lý do — không bao giờ vứt im — để tỉ lệ luôn kiểm toán được. Lỗi thì vẫn nằm
trong mẫu số; chỉ "đây chưa từng là một mẫu hợp lệ" mới là loại trừ.

**5. Verdict ghi sớm thì phải sửa được.**
Tracker báo cáo theo hẹn giờ có thể sai khi thông tin tới muộn. Hãy phát một
dòng đính chính mang `amends_event_id`, và ghi trong doc của tracker rằng truy
vấn phải ưu tiên bản đính chính. Đừng để một dòng đã biết là sai đứng nguyên.

**6. Quan sát dở dang không phải là đạt.**
Nếu cửa sổ theo dõi đóng lại trong khi thứ đang theo dõi vẫn có thể xảy ra, ghi
rõ trong dòng đó (`observation_complete: false`) và loại nó khỏi tỉ lệ như một
phần coverage bị mất.

**7. Telemetry không bao giờ được làm hỏng tính năng.**
Hook tự nuốt exception của mình, việc báo cáo không nằm trong lock của đường
real-time, và không code sản phẩm nào rẽ nhánh theo dữ liệu tracker. Tracker
hỏng thì thiết bị vẫn chạy — mất số liệu, không mất hành vi.

## Đặt tên

- Event: `<domain>_<thing>` — `voice_metrics_interaction`, `ota_update`.
- Thời lượng kết thúc bằng `_ms`; `event_timestamp` của client analytics chỉ tới
  giây nên quá thô để đo độ trễ.
- Outcome dùng danh sách cố định (`applied` / `failed`), không phải chữ tự do.

## Riêng tư

Không bao giờ gửi transcript, text câu trả lời, audio thô, nội dung file hay
credential. Chỉ id, outcome trong danh sách cố định, và các khoảng thời gian.
`user_pseudo_id` là hostname thiết bị; `platform` là `device`.

## Các tracker hiện có

| Tracker | Trả lời câu hỏi gì | Doc |
|---------|--------------------|-----|
| `hal/telemetry/voice_metrics.py` | Thiết bị báo đã nghe nhanh cỡ nào, và có bao giờ phát câu trả lời mà người dùng đã bỏ qua không | [`docs/vi/voice-metrics_vi.md`](voice-metrics_vi.md) |
| `hal/telemetry/live_voice.py` | Cùng các KPI voice cho phiên realtime (live): gán mỗi turn của provider vào đúng một interaction HAL và cấp dữ liệu cho `voice_metrics` mà không đoán quyền sở hữu audio | [`docs/vi/voice-metrics_vi.md`](voice-metrics_vi.md) |
| `system/telemetry/voice_task.go` | Tác vụ voice/chat đã được nhận có thực sự chạy không (bắt đầu, gắn run, lifecycle end hoặc local intent trả về) | [`docs/vi/voice-metrics_vi.md`](voice-metrics_vi.md) |

## Lệnh kiểm chứng

```bash
go build ./...
go test ./system/telemetry/ ./system/server/telemetry/... ./system/lib/analytics/
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_telemetry_flag.py -q
```

Theo dõi tác vụ thoại phát `voice_metrics_task_started` khi OS nhận và bind run
cho `voice`, `voice_command`, `voice_followup`, cùng `voice_metrics_task_execution`
ở biên lifecycle/local-intent/dispatch lỗi của OS và realtime trả về ở HAL.
Gộp OS start với snapshot task HAL có version bằng device + interaction/run
trước khi chấm để lượt chỉ có OS vẫn được tính đúng một lần. Run backend bất
kỳ và memory-sync `voice_agent_handled` không tạo lượt mới. Horizon báo cáo mặc
định 0 giây: lượt eligible chưa xong vẫn ở mẫu số. “Completed” nghĩa là đã chạy xong, không đánh giá đúng yêu cầu hay phát
hết audio. Xem [runbook query/report KPI-3 cho agent](voice-metrics_vi.md#runbook-cho-agent-query-và-report-kpi-3).

Chat phát `chat_metrics_task_started` ngay khi nhận (kể cả queue); sensing phát `sensing_metrics_task_started` chỉ khi được chọn dispatch tác vụ, sau quyết định suppression/queue. Cả hai dùng chung `voice_metrics_task_execution` cho terminal evidence và join trong cohort được chọn. MQTT `speak: true` vẫn là voice. Xem [runbook query và report AA chat/sensing](task-metrics_vi.md) để biết eligibility, evidence dùng chung và lệnh `--group chat|sensing`.

Với `voice_metrics_*`, `chat_metrics_*`, `sensing_metrics_*`, INFO `[telemetry] delivered` xác nhận HTTP gửi AA thành
công, chưa chứng minh query thấy row trong warehouse. Cần query/export bằng
quyền đọc để xác minh; key ingestion không tự cấp quyền đọc.
