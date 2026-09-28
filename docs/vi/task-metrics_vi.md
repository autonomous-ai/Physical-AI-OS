# Metric hoàn tất tác vụ chat và sensing

Hai metric dùng cùng định nghĩa hoàn tất kỹ thuật với [KPI-3 voice](voice-metrics_vi.md#kpi-3-chạy-xong-không-đánh-giá-làm-đúng), nhưng có cohort chat và sensing riêng. **Completed nghĩa là chạy xong, không có lỗi thực thi terminal được quan sát.** Không đánh giá trả lời đúng, sự hài lòng, hiệu quả vật lý hay phát hết audio.

## Nhóm và điều kiện được tính

| Nhóm | Event tạo cohort | Tác vụ eligible |
|------|-----------------|-----------------|
| Voice | `voice_metrics_task_started` (cộng snapshot task HAL) | `voice`, `voice_command`, `voice_followup`; giữ nguyên cách chấm voice |
| Chat | `chat_metrics_task_started` | `web_chat`, `mqtt_chat` được OS nhận, kể cả đang xếp hàng |
| Sensing | `sensing_metrics_task_started` | Input sensing không phải nội bộ được chọn để thực sự dispatch tác vụ |

MQTT có `speak: true` được chuyển thành voice, chỉ thuộc nhóm voice. Notification nội bộ `voice_agent_handled`, `voice_listening`, `voice_listening_end`, `look.capture` không tạo lượt eligible.

Chat vào mẫu số ngay khi nhận, trước thực thi; khi vào queue được bind run cố định. Nhận vào queue chưa phải hoàn tất. Sensing chỉ vào mẫu số sau khi được chọn dispatch: input bị loại bởi sleep, cooldown, coalescing, expiry hay quyết định suppression khác không phải tác vụ. Sensing trong queue được tính khi replay chọn dispatch. Một batch ambient Hermes đã gộp là một tác vụ dispatch, không phải một lượt cho mỗi input sensor. Với sensing trong queue Hermes, chỉ phát start sau khi `SendChat` được nhận hoặc lỗi không retry; retry biết chắc chưa gửi có thể gộp lại batch và không phát start. Quy tắc áp dụng cả input đơn lẻ và batch đã gộp. Giới hạn: các tác vụ Hermes này chưa vào mẫu số khi send đang chờ kết quả hoặc process crash trước khi có kết quả. Năm runtime còn lại phát start cho sensing trong queue trước dispatch và giữ run id cố định qua retry biết chắc chưa gửi. Retry biết chắc chưa gửi không tạo terminal failure hay lượt mới.

Event start có `schema_version=1`, `interaction_id`, `run_id`, `event_type`, `task_started_at_ms` (Unix milliseconds). Event lúc nhận và event bind run sau đó là bản bổ sung cùng lượt. Gộp theo device + interaction id hoặc device + run id không rỗng; giữ thời điểm start sớm nhất.

Cả ba nhóm dùng chung **`voice_metrics_task_execution`** làm bằng chứng kết thúc. Tên lịch sử này không giới hạn event cho voice. Không có event riêng `chat_metrics_task_execution` hay `sensing_metrics_task_execution`; không đếm các execution row đứng riêng thành tác vụ. Chỉ join evidence với start của nhóm đang báo cáo bằng device + run id hoặc device + interaction id.

Evidence có `schema_version=1`, `interaction_id`, `run_id`, `outcome` (`completed`, `failed`, `unknown`, `cancelled`), `evidence`, `execution_at_ms`, boolean `error`. Runtime kết thúc rõ ràng không abort/error là completed; lỗi terminal lifecycle/chat/dispatch là failed. Recovery riêng lẻ là unknown. Bất kỳ terminal failure đã join trước cutoff đều được giữ: kết thúc cleanup sau đó không đổi lượt thành completed. Harness nhận `harness_correlated_summary` cancelled là terminal và đếm riêng `cancelled_turns`; cleanup sau đó không biến thành thành công. Failure ưu tiên hơn cancellation. Nếu không có failure/cancellation, chọn execution timestamp mới nhất (cùng timestamp thì completed ưu tiên hơn unknown). Quy tắc riêng HAL/realtime của voice nằm trong runbook voice.

## Query và report chỉ bằng AA

Agent chỉ cần quyền đọc/export AA event tracking và reporter trong repo; không cần truy cập device. Query dưới đây giả định bảng kiểu BigQuery `event_tracking` có `event_name`, `event_timestamp` Unix seconds, `data` chứa `user_pseudo_id` và mảng key/value `event_params`. Điều chỉnh tên bảng/accessor theo warehouse thực tế; đây là schema ví dụ, chưa xác minh contract warehouse.

```sql
SELECT event_name, event_timestamp, data
FROM event_tracking
WHERE event_name IN (
  'chat_metrics_task_started',
  'sensing_metrics_task_started',
  'voice_metrics_task_execution'
)
  AND event_timestamp >= UNIX_SECONDS(TIMESTAMP('2026-09-11 00:00:00+07'))
  AND event_timestamp <= UNIX_SECONDS(TIMESTAMP('2026-09-11 12:00:00+07'))
-- AND data.user_pseudo_id = '<device identity stored in AA>'
ORDER BY event_timestamp;
```

Export JSONL mỗi dòng một row đầy đủ, giữ identity device, thời điểm quan sát và mọi param. Lấy **toàn bộ start và bản bổ sung bind run**, không chỉ lượt completed, cùng evidence thực thi đến thời điểm as-of. Nếu chỉ report một nhóm, query có thể chỉ lấy start của nhóm đó cộng execution dùng chung. Không lọc execution dùng chung theo prefix của nhóm hay `event_type` (execution không bắt buộc có field này). Muốn report voice từ cùng export, thêm `voice_metrics_task_started` và `voice_metrics_interaction`.

```bash
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --group chat --settle-seconds 0 > chat-task-report.json
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --group sensing --settle-seconds 0 > sensing-task-report.json
```

Với snapshot lịch sử, thêm `--now-ms <cutoff in Unix milliseconds>`. Nhóm mặc định là `voice`; phải chỉ định chat/sensing. Reporter cũng nhận journal JSONL (`journalctl -u hal -u os-server -o json --no-pager`) từ phiên device đã được cho phép. `--device` chỉ bổ sung identity thiếu, không phải bộ lọc; lọc device khi export.

Dữ liệu đầu vào quyết định khoảng start; CLI chưa có bộ lọc cửa sổ start. Giữ đầy đủ lịch sử start/binding của cohort và phần execution đến as-of. Reporter lọc thời điểm quan sát trước khi chọn evidence. Export thiếu timestamp không chứng minh được snapshot lịch sử chính xác; cần báo thiếu timestamp/identity là coverage gap. Không thể khôi phục tác vụ lịch sử thiếu start chỉ bằng event terminal.

## Số đếm và cách diễn giải

Tính riêng từng nhóm:

```text
completion_pct = 100 * completed_turns / eligible_mature_turns
```

Horizon mặc định **0 giây**: mọi start eligible đến as-of nằm trong `eligible_mature_turns`, gồm failed, unknown và chưa xong. Ví dụ **85 lượt xong trên 100 eligible là 85%**. Mục tiêu mặc định **≥85%**, so sánh trước khi làm tròn. Không có lượt eligible là N/A (`completion_pct`, `meets_target` bằng null), không phải 0% hay 100%. `completion_pct` là alias của field cũ `kpi3_pct`; `group` xác định cohort. `--settle-seconds` dương là tùy chọn chuyển start mới hơn sang `fresh_pending_turns`, không phải timeout thực thi.

Report nhóm, khoảng thời gian, as-of, nguồn, horizon, completed/eligible, phần trăm, đạt mục tiêu hay chưa, failed/cancelled/unknown/incomplete và coverage. Gộp device bằng tổng completed chia tổng eligible, không lấy trung bình phần trăm. Giữ kết quả chat, sensing, voice riêng. Execution không join được có thể thuộc nhóm khác; không chứng minh nhóm đang report bị mất task. Loss counter telemetry là bộ đếm tích lũy toàn device, không quy về riêng nhóm; xem max và reset khi restart, không cộng giá trị từng event.

Interaction có prefix `vi-smoke-`, `chat-smoke-`, `sensing-smoke-` mặc định bị loại. Caller diagnostic phải dùng prefix smoke; request không đánh dấu không phân biệt được với tác vụ thật. `--include-synthetic` chỉ dùng kiểm tra instrumentation. Log `[telemetry] event` chứng minh ghi nhận local; `[telemetry] delivered` chứng minh HTTP ingestion AA thành công, chưa chứng minh query thấy row. Xác minh warehouse bằng event id và identity interaction/run trong export. Tài liệu mô tả implementation, không chứng minh device cụ thể đã deploy.

Lưu ý test sensing qua queue: replay tạo interaction ID từ run ID, không giữ smoke ID ở request đầu vào. Khi kiểm tra queue, loại các run ID test đã biết khỏi dữ liệu xuất trước khi tính KPI.
