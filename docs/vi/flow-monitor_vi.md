# Flow Monitor

Sự kiện kết thúc lifecycle dùng cùng quy tắc trạng thái cho monitor trực tiếp và lịch sử JSONL. `lifecycle_error` đóng đúng lượt của nó thành **ERROR**, ghi thời điểm kết thúc và hiển thị `data.error` trong chi tiết Response (ví dụ `Hermes cancelled`). Lượt không còn kẹt ACTIVE sau khi tải lại và không đóng nhầm lượt khác chạy xen kẽ. `lifecycle_end` thành công vẫn là DONE; sự kiện kết thúc có lỗi vẫn là ERROR. Kiểm tra: `node --test system/web/tests/flow-lifecycle.test.cjs`.

Lượt history có ID `device-chat-context-` và envelope `[external-context]` / `[HANDLED]` / `[REPLY]` hợp lệ hiển thị **History sync**, route **Harness → Main** hoặc **Realtime → Main** (hoặc nguồn bên ngoài khác). Card hiện câu hỏi và câu trả lời gốc với nhãn **Context**, không coi là TTS mới. Tooltip route có tên agent; metadata thô giữ trong chi tiết event. Parser dùng chat-send đầy đủ khi preview chat-input bị cắt, không đổi trạng thái lifecycle.

Card voice Harness-only đọc `sensing_input.data.route: "harness_only"` để hiện **Harness** thay cho **Agent**. Sau khi gom event theo run ID, `harness_response` có nội dung đóng đúng lượt đó, cung cấp output và chi tiết node Response, kể cả khi tải lịch sử JSONL. Chỉ gửi input thành công thì vẫn active. Không ghép phản hồi Harness vào input gần đó có run ID khác; trạng thái lỗi đã có vẫn giữ lỗi. DONE nghĩa là đã nhận phản hồi cuối, không xác nhận phát âm thanh hay tác vụ thực tế thành công.

Kiểm tra hồi quy: chạy `node --test system/web/tests/flow-harness.test.cjs` từ repo root (sau khi cài dependency web).

Codex steering ghi `turn_merged` theo run ID của follow-up, với
`data.parent_run_id` trỏ đến host đang active. Follow-up giữ card input riêng
và mở pipeline chung của host khi được chọn. Trạng thái theo host đến khi có
terminal riêng; nếu thiếu host, UI báo pipeline nằm ngoài lịch sử đã tải.
Yêu cầu voice/web chờ hoàn tất thực sự, còn đồng bộ lịch sử realtime im lặng
có thể kết thúc ngay khi nhận xác nhận. Liên kết áp dụng cho event mới được ghi;
không tự suy đoán quan hệ của các trace cũ thiếu marker.
Pipeline chung cũng hiển thị kết quả Harness, TTS và sự kiện phần cứng của follow-up đang chọn, không lặp input hay lifecycle của nó.

Flow Monitor là lớp quan sát để theo dõi agent turn end-to-end. Nó ghi event vào file JSONL theo ngày (`local/flow_events_YYYY-MM-DD.jsonl`) và stream tới web UI qua SSE.

**Quan trọng**: Flow monitor chỉ thuần quan sát. Nó KHÔNG ảnh hưởng hành vi thiết bị, giao tiếp agent, TTS, LED hay bất kỳ business logic nào.

## Kiến trúc

```
HAL (Python)                       OS Server (Go)                     Web UI (React)
  sensing event ──POST──→ SensingHandler ──flow.Start/End──→ JSONL file
                            │                                    ↓
                            └─ agentGateway.SendChat ──→ Agentic Runtime (WS)
                                                           │
                        AgentHandler ←── WS events ───────┘
                            │
                            ├─ flow.Log("lifecycle_*") ──→ JSONL file ──→ /api/agent/flow-stream (SSE)
                            ├─ flow.Log("tool_call")                         ↓
                            ├─ flow.Log("tts_send")                    system/web/src/pages/monitor/FlowSection/
                            └─ monitorBus.Push() ──→ /api/agent/events (SSE)  └─ groupIntoTurns() (helpers.ts)
```

## Run ID theo từng event (refactor từ global trace)

### Trước (global trace)
```go
flow.SetTrace(runID)           // set global, affects ALL subsequent events
flow.Log("lifecycle_end", data) // picks up global trace
flow.ClearTrace()              // clear global
```

Vấn đề:
- Các turn chạy đồng thời ghi đè trace của nhau
- Server restart làm mất trace trong bộ nhớ
- `ClearTrace()` trong goroutine race với `SetTrace()` kế tiếp

### Sau (run ID theo từng event)
```go
flow.Log("lifecycle_end", data, payload.RunID)  // explicit per-event
flow.Log("tool_call", data, payload.RunID)      // each event carries its own ID
```

- `Start()`, `End()`, `Log()` nhận tham số variadic tùy chọn `runID`
- Nếu có, nó đè global trace cho riêng event đó
- `SetTrace`/`GetTrace` global vẫn giữ cho heuristic phát hiện Telegram
- `ClearTrace()` giảm trace active (đếm tham chiếu), được gọi sau `lifecycle_end` của runtime

### Heuristic phát hiện Telegram

Khi `lifecycle_start` tới mà không có device trace active (`flow.GetTrace() == ""`), handler kiểm tra xem đó có phải turn do channel khởi tạo (Telegram/Slack) không. Turn `chat.send` do thiết bị phát được loại trừ qua `lamp-chat-*` (và legacy `lamp-sensing-*`) để không bị gán nhầm là Telegram khi trace bị mất.

#### Lấy nội dung tin nhắn user qua RPC `chat.history`

Chat stream của runtime **không bao giờ broadcast event `role:"user"`** — nó chỉ emit `role:"assistant"` (delta/final/error). Để lấy text tin nhắn user và tên người gửi, OS server gọi WebSocket RPC `chat.history` trên cùng kết nối WS dùng để nhận event:

```
→  {"type":"req","id":"history-1","method":"chat.history",
    "params":{"sessionKey":"agent:main:telegram:group:-5139766247","limit":20}}

←  {"type":"res","id":"history-1","ok":true,
    "payload":{"sessionKey":"...","sessionId":"...","messages":[
      {"role":"user","content":[{"type":"text","text":"dừng phát nhạc đi"}],
       "senderLabel":"Leo (158406741)"},
      {"role":"assistant","content":[...]},
      ...
    ],"thinkingLevel":"low"}}
```

Chi tiết triển khai:

- **Async goroutine**: Fetch chạy trong goroutine riêng vì gọi đồng bộ trong handler của WS read loop sẽ deadlock (read loop chặn chờ handler return, nhưng response RPC chỉ tới được sau khi handler return).
- **Theo dõi pending RPC**: `pendingRPC map[string]chan json.RawMessage` trong `runtimes/openclaw/service.go` khớp frame `type:"res"` về đúng caller đang chờ theo request ID. `dispatchRPCResponse()` móc vào read loop trước bước xử lý event.
- **Emit hai phase**: `chat_input` đầu tiên fire ngay với placeholder trung tính `[chat]` (chưa có tin nhắn). Sau khi goroutine lấy được history, `chat_input` thứ hai fire với text tin nhắn thật và nhãn chọn theo `senderLabel` / kiểm tra prefix tin nhắn — UI chọn event có nội dung.
- **Định tuyến nhãn (emit thứ hai)**: (1) `senderLabel` không rỗng → `[telegram:Gray]` (user channel thật). (2) `senderLabel` rỗng + tin nhắn khớp prefix nội bộ thiết bị → `[voice]` / `[emotion]` / `[activity]` / `[wellbeing]` / `[music]` / `[sensing]` / `[system]` (sensing hoặc voice event thiết bị đã post qua chat.send mà OpenClaw merge vào UUID host turn này qua steer mode). (3) Còn lại → generic `[chat]`. Trước đây mọi UUID channel-turn đều bị gán nhãn vô điều kiện theo channel đã cấu hình (`[telegram]`), gán nhầm các turn self-fire bị steer-merge cho Telegram.
- **Best-effort**: timeout 3 giây. Nếu fetch lỗi, turn giữ placeholder generic `[chat]` — tốt hơn là gán nhầm vào một channel cụ thể.
- **Nhiễu heartbeat**: cron heartbeat của OpenClaw (mỗi 30 phút) cũng trigger `lifecycle_start`. Tin nhắn `role:"user"` cuối cùng trong các turn đó là system prompt của heartbeat (bắt đầu bằng `"System:"`), không phải tin nhắn user thật.
- **Token usage**: `chat.history` cũng được gọi lúc `lifecycle_end` để lấy token usage. Event `lifecycle_end` của OpenClaw không có dữ liệu `usage`. Tin nhắn `role:"assistant"` cuối trong history response chứa `usage: {input, output, totalTokens, cacheRead, cacheWrite}` cho turn vừa xong. Nó được emit thành flow event `token_usage` với `source: "chat_history"`. Chỉ xét assistant message MỚI NHẤT, và chỉ khi `timestamp` còn tươi (≤30s; retry 1 lần sau 2s) — fetch này đua với việc OpenClaw persist reply, và kiểu walk-back về assistant message cũ hơn từng gán nhầm usage (và thinking) của turn TRƯỚC cho các run tự nổ như heartbeat. Gate staleness tương tự (120s) áp cho fetch `chat.history` lúc lifecycle_start (gắn nhãn channel turn), nên heartbeat không còn nhân bản input text của turn trước. Run heartbeat (reply `HEARTBEAT_OK`) emit thêm flow event `heartbeat_run`; mục Flow trên web phân loại các turn đó là `heartbeat`. Footer token của turn card hiện `↓in ↑out R<cacheRead> Σtotal` — cache read là phần lớn context mỗi turn, và backend Autonomous tính cache read FULL giá, nên Σtotal (in + out + cache) chính là số billed (monitor không còn chỗ nào giảm 0.1×).

## Định dạng Run ID & ánh xạ

```
sendChat() generates:
  reqID           = "chat-1"                       (WS message ID, local counter)
  idempotencyKey  = "lamp-chat-1-1774841927380"    (sent to OpenClaw, globally unique; not "sensing-only" — any outbound chat from the device uses this prefix)

sendChat returns idempotencyKey → used as trace_id in flow events
```

**Hành vi run_id của OpenClaw phụ thuộc version:**
- **5.2** (và một số path hiếm ở 5.4): gán UUID riêng (vd `a8a51f3c-b44f-434b-a4c9-cd1a2a1e3c30`) — OS server phải map UUID → idempotencyKey.
- **5.4** (đa số): echo thẳng idempotencyKey làm runId (đã verify ở `src/gateway/server-methods/chat.ts:2002`, `clientRunId = p.idempotencyKey`). runId của lifecycle ĐÃ là device trace; không cần map.
- **Lẫn lộn trong một session**: một chat.send có thể tạo tới hai phase lifecycle — Phase 1 với idempotencyKey được echo, Phase 2 với UUID mới cho embedded run thật (pattern drain/burst). Phải xử lý cả hai.

**Giải pháp: pending trace khớp theo message** trong SSE handler:
1. Sensing handler cấp `NextChatRunID()`, rồi gọi `flow.SetTrace(idempotencyKey)` **trước** `flow.Start("sensing_input", ...)` để dòng `enter` trong JSONL dùng cùng `trace_id` với `chat_send` của POST đó. (Trước đây chỉ gọi `SetTrace` sau `SendChatMessage` nên `enter` bị gắn id của turn **trước** — turn ma và export Pair lệch.)
2. Trước khi ghi `chat.send`, OS server lưu idempotencyKey và message đã gửi vào `pendingChatBuf`; ghi lỗi thì xoá trace. Thời gian giữ trace cho routing vẫn là 2 phút và cửa sổ pending-send busy vẫn là 30 giây. Một buffer telemetry riêng giữ tối đa 1024 trace trong 24 giờ.
3. Ở `lifecycle_start`, handler rẽ nhánh theo định dạng `payload.RunID`:
   - **Device-format** (`device-chat-*`, gồm cả legacy `lamp-chat-*`): xoá pending trace khớp theo run ID; không cần map.
   - **UUID**: fetch `chat.history` cho session thiết bị. Routing của runtime giữ nguyên matcher exact/prefix đã trim và cách chọn entry cũ nhất. Riêng telemetry chỉ nhận đúng một match exact sau trim và lưu alias bên ngoài routing map; bằng chứng thiếu/mơ hồ không kế thừa phỏng đoán của routing. Fetch history lỗi vẫn để correlation chưa giải được.
4. Mọi event **agent-stream** sau đó (`lifecycle`, `tool`, `thinking`, delta `assistant`, `tts_send`) dùng `resolveRunID(payload.RunID)` để `trace_id` khớp device key.
5. Event **chat stream** (`case "chat"`: text user/assistant từ chat feed song song) cũng gọi `resolveRunID` cho `flow.Log` và `RunID` của monitor. Không có bước này, OpenClaw có thể emit **UUID** trong chat payload trong khi JSONL từ bước 4 dùng **device id** — Monitor sẽ tách một turn thành hai ID.

### Log tương quan (grep: `flow correlation`)

Các dòng `slog.Info` có cấu trúc để căn chỉnh ID end-to-end (device idempotency key = `lamp-chat-*`):

| `op` | `section` (khi có) | Khi nào |
|------|------------------------|------|
| `ws_chat_send` | `lamp_to_openclaw_ws` | Mọi `chat.send` từ thiết bị (`device_run_id` = idempotency key). |
| `hal_agent_out` | `hal_to_openclaw` | Sensing handler sau `SetTrace` + `agent_call` (cùng `device_run_id`). |
| `openclaw_uuid_map` | `openclaw` | `lifecycle_start`: lưu UUID OpenClaw → device id. |
| `chat_run_resolve` | `openclaw_chat` | Event chat stream mà `resolveRunID` đã đổi id (UUID → device). |

## Gom turn (Frontend)

`groupIntoTurns()` trong `system/web/src/pages/monitor/FlowSection/helpers.ts` gom event thành turn:

1. **Phát hiện bắt đầu turn**: `sensing_input`, `chat_input`, `ambient_action`, `schedule_trigger`
2. **Gom theo run ID**: event cùng `runId` ở cùng turn
3. **Ghép mảnh**: các turn cùng `runId` được merge (xử lý event bị tách)
4. **Nâng cấp type placeholder**: `chat_input` của channel fire hai lần (xem "Emit hai phase" ở trên). Emit đầu pin `turn.type = "chat"` từ placeholder `[chat]`. Khi emit thứ hai tới với tin nhắn thật, `isTurnStart` derive lại type cụ thể (`emotion.detected`, `speech_emotion.detected`, `voice`, `telegram`, …) từ prefix tin nhắn; `groupIntoTurns` chỉ nâng cấp `turn.type` khi nó vẫn là placeholder `"chat"` (hoặc `"unknown"`) — giữ nguyên phân loại đã cụ thể. Prefix `[speech_emotion]` map về `speech_emotion.detected` và được xếp vào source `mic` (voice-driven), không phải `cam`, dù nhãn có chữ "emotion". `voice_agent_handled` (turn giọng nói realtime đã xử lý, replay cho main agent) cũng thuộc nhóm source `mic` — trước đây nó không thuộc nhóm nào nên toggle Mic/Cam không bao giờ ẩn được nó và nó trông như "thuộc" bất kỳ nhóm nào còn bật.
5. **Stitching**: turn chỉ có output mồ côi được merge với turn chỉ có input gần đó (xử lý bị tách do server restart)
6. **Ngắt session**: khoảng cách >60s giữa các turn đánh dấu ranh giới session

### Quy tắc stitching

| Turn trước | Turn hiện tại | Điều kiện | Hành động |
|---|---|---|---|
| Telegram fallback (không có tin nhắn) | Agent output | cách <30s | Merge |
| Sensing input (không có output) | Output mồ côi (không có input) | cách <30s | Merge |

## Turn Pipeline (SVG `FlowDiagram`)

Được vẽ bởi `FlowDiagram` trong `system/web/src/pages/monitor/FlowSection/FlowDiagram.tsx`. Sơ đồ **chỉ để quan sát** (zoom/pan, highlight node từ event gần đây). Kéo bằng chuột hoặc một ngón tay để pan; chụm/mở hai ngón tay (hoặc dùng nút trừ/cộng trong canvas) để zoom, và dùng nút reset để về góc nhìn mặc định. Trên điện thoại, stream LLM/tool được vẽ bằng SVG text thuần để hiển thị ổn định; **LLM / Tool / Curl details** mở một sheet native responsive cho payload dài và output của node. Ba vùng **cluster tô màu** gom các node:

| Vùng | Màu (theme) | Stage |
|--------|----------------|--------|
| **OS Server** | Teal (`--lm-teal`) | Dải trên: `intent_check`, `local_match`. Cột dọc tại `x=467`: `os_gate`, `tg_alert`, `hw_mood`, `hw_wellbeing`, `hw_music_suggestion`, `hw_posture` |
| **Device** (HAL) | Amber (`--lm-amber`) | `mic_input`, `cam_input`, `button_input`, `hw_camera`, `hw_emotion`, `hw_led`, `hw_servo`, `hw_audio`, `tts_speak` |
| **Agent** | Blue (`--lm-blue`) | `schedule_trigger`, `agent_call`, Event Pipeline rect (anchor `tool_exec` / `agent_thinking`), `agent_response` |

Các node channel bên ngoài nằm ngoài cluster, ở bên phải: `channel_input`, `webchat_input`, `tg_out`.

### OS Server (dải trên)

- **Intent** và **Local** nằm trên **cùng hàng trên** (trái sang phải).
- **Gate** (`os_gate`) và các lần ghi log phía OS server (`tg_alert`, rồi `hw_mood`, `hw_wellbeing`, `hw_music_suggestion`, `hw_posture`) xếp chồng trong một cột teal thứ hai tại `x=467`, giữa cột device và cluster agent.
- **Cron** (`schedule_trigger`) được vẽ bên trong cluster Agent, trên hàng `agent_call`, bên trái Agent, nên Cron → Agent là một cạnh ngang ngắn.

### HAL (cột trái)

- **MIC** và **CAM** là input nodes (trên cùng của vùng Device); **BTN** (`button_input`, nút GPIO / touchpad TTP223) nằm dưới MIC. Node hình thoi **CAM**
  phía dưới là node riêng, đại diện cho việc agent tool gọi
  `GET /camera/snapshot`.
- Output nodes xếp dọc trong một cột:
  - **EMO** (`hw_emotion`) — gọi `/emotion` (phối hợp LED + servo + display eyes)
  - **LED** (`hw_led`) — `/led/solid`, `/led/effect`, `/scene`, `/led/off`
  - **SERVO** (`hw_servo`) — di chuyển hoặc chạy animation servo: `/servo/aim`,
    `/servo/play`, `/servo/nudge`, `/servo/search`, `/servo/demo`. Chi tiết
    node hiển thị đúng lệnh/API call agent đã chạy trong turn đang chọn.
    Các lệnh đọc (`/servo/position`, `/servo/status`, `/servo/bearing`) không tính —
    một event `hw_servo` nghĩa là đèn đã làm gì đó mà người ta nhìn thấy được.

  **Event phần cứng được so khớp theo endpoint đã phân giải, không theo text
  shell thô.** Arguments của tool call được quét tìm `127.0.0.1:500[01]/<path>`
  (`hwPathFromToolArgs` trong `handler_event_agent.go`) và các nhánh `/emotion`
  cùng `/servo/*` so sánh với path đó. Cách so khớp chuỗi con trước đây biến
  `cat …/skills/emotion/SKILL.md` thành một cặp `hw_emotion` + `led_set` cho một
  turn mà đèn không hề làm gì. Các nhánh `/led/*` và `/audio/play` vẫn dùng
  dạng chuỗi con — cùng điểm yếu, nhưng chưa quan sát thấy event ma ở đó.

  **`hw_failed`** — một marker `[HW:...]` mà OS đã cố bắn nhưng POST thất bại
  ở tầng transport: client timeout 5 s, kết nối bị từ chối. Mang theo `path`,
  `args`, `run_id` và `error`. Trước khi có event này, nhánh đó return trước
  bất kỳ `flow.Log` nào, nên một chuyển động thân máy dài 40 s không để lại gì
  trong monitor (`device-chat-44`: marker tìm kiếm đã bắn, HAL quét cả phòng,
  timeline hiện một cái đèn đứng yên). Cố ý **không** phải là một cancellation:
  nó làm sáng node OS-gate và thêm dòng `⚠ → HW call failed` vào chi tiết,
  nhưng không gắn badge cancelled cho turn và không tô đỏ node TTS — hai thứ đó
  nghĩa là *người dùng* đã làm turn im tiếng, và một timeout không bao giờ được
  đọc thành hành động của người dùng.
  - **CAM** (`hw_camera`) — `GET /camera/snapshot`; kết quả đã lưu được hiện
    thành thumbnail bấm được để operator debug đúng frame trả về cho agent
    (kể cả ảnh workspace của agent như `cam_face3.jpg`, không phải preview
    chụp mới). Thumbnail cần CẢ arguments LẪN result của tool, mà hai thứ này
    về ở hai event khác nhau trên các runtime CLI (codex và họ hàng gửi `arguments` chỉ
    ở `start`, `result` chỉ ở `end`), nên args được mang qua theo `toolCallId` — xem
    `rememberToolArgs` trong `system/server/agent/delivery/http/camera_snapshot.go`.
  - **AUDIO** (`hw_audio`) — `/audio/play` và các kiểu phát audio khác
  - **TTS** (`tts_speak`) — `/voice/speak`, output text-to-speech
- Đây là các hardware call trực tiếp từ tool của OpenClaw, không qua OS server.

### Quy tắc layout Agent (cột + hàng)

Đây là **quy tắc ổn định** cho các node bên trong và bên phải hình chữ nhật Agent; `positions` trong `FlowDiagram.tsx` theo lưới này.

**Cột (trái → phải)**

| Cột | Stage |
|-----|--------|
| **1** | Cron (`schedule_trigger`) |
| **2** | Agent Call (trên) → Event Pipeline rect (giữa) → Response (dưới). Pipeline chứa các dòng cho event thinking / assistant / tool / lifecycle / compaction / error theo thứ tự; `tool_exec` (cạnh trái) và `agent_thinking` là anchor ẩn trên rect. Xem `robots/lamp/docs/debug/flow-monitor-pipeline.md` để biết quy tắc gộp và lý do gộp chuỗi 3 node cũ `LLM Start / Thinking / Tool Exec` thành một rect. |
| **3** | Channel bên ngoài (ngoài cluster): Channel In, Web Chat, Channel Out (`tg_out`) |

**Hàng (trên → dưới)**

| Hàng | Quy tắc |
|-----|------|
| **1** | **Cron**, **Agent** và **Channel In** cùng một hàng ngang (Cron → Agent ← Channel In). |
| **2** | **Web Chat** (bên phải) đổ vào Agent; Event Pipeline rect chiếm giữa cột 2. |
| **3** | **Response** và **Channel Out** cùng hàng dưới, ngang với `os_gate`. |

**Lưới ASCII (Agent + channel)**

```
              Col1        Col2        Col3
         ┌──────────┬──────────┬──────────┐
    Row1 │   Cron   │  Agent   │  CH In   │
         ├──────────┼──────────┼──────────┤
    Row2 │          │ Pipeline │ Web Chat │
         ├──────────┼──────────┼──────────┤
    Row3 │          │ Response │  CH Out  │
         └──────────┴──────────┴──────────┘
```

### Tọa độ gần đúng (để bảo trì layout)

Giá trị là **tâm node** `(x, y)` trong view box SVG (xem `positions` trong `FlowDiagram.tsx`). Nếu di chuyển node thì chỉnh cluster theo.

| Stage | `(x, y)` | Ghi chú |
|-------|----------|------|
| `intent_check` | `(80, 50)` | OS server, hàng trên |
| `local_match` | `(200, 50)` | OS server, hàng trên |
| `os_gate` | `(467, 795)` | Cột OS server; giữa Device và Agent |
| `tg_alert` | `(467, 930)` | Cột OS server; broadcast alert |
| `hw_mood` | `(467, 1065)` | Cột OS server; ghi log mood |
| `hw_wellbeing` | `(467, 1200)` | Cột OS server; ghi log wellbeing |
| `hw_music_suggestion` | `(467, 1335)` | Cột OS server; ghi log gợi ý nhạc |
| `hw_posture` | `(467, 1470)` | Cột OS server; ghi log posture |
| `mic_input` | `(-40, 240)` | Device input |
| `cam_input` | `(80, 240)` | Device input |
| `button_input` | `(-40, 350)` | Device input; nút / touchpad |
| `hw_camera` | `(200, 345)` | Device output; agent gọi API camera |
| `hw_emotion` | `(200, 480)` | Device output; gọi emotion |
| `hw_led` | `(200, 615)` | Device output; điều khiển LED |
| `hw_servo` | `(200, 750)` | Device output; servo motor |
| `hw_audio` | `(200, 885)` | Device output; phát audio |
| `tts_speak` | `(200, 1020)` | Device output; TTS |
| `schedule_trigger` | `(750, 240)` | Agent hàng 1, cột 1 |
| `agent_call` | `(950, 240)` | Agent hàng 1, cột 2 |
| `tool_exec` | `(820, 600)` | Anchor cạnh trái pipeline rect (cạnh HW) |
| `agent_thinking` | `(1180, 480)` | Anchor trơ của pipeline rect |
| `agent_response` | `(950, 795)` | Agent hàng 3, cột 2 |
| `channel_input` | `(1300, 240)` | Bên ngoài; channel input |
| `webchat_input` | `(1300, 440)` | Bên ngoài; web chat input |
| `tg_out` | `(1300, 795)` | Bên ngoài; channel output |

### Cạnh

```
mic_input / cam_input / button_input → intent_check → local_match → hw_emotion / hw_led / hw_servo / tts_speak
intent_check → agent_call
channel_input / webchat_input / schedule_trigger → agent_call
agent_call → tool_exec [Event Pipeline rect — thinking/assistant/tool rows] → agent_response
tool_exec → hw_emotion         (agent /emotion call → HAL)
tool_exec → hw_led             (agent /led/* or /scene call → HAL)
tool_exec → hw_servo           (agent /servo/* call → HAL)
tool_exec → hw_audio           (agent /audio/* call → HAL)
tool_exec → hw_camera          (agent GET /camera/snapshot → HAL; saved frame shown below node)
tool_exec → os_gate            (OS server listens: pause ambient if LED; TTS-vs-music ordering is handled in HAL, not suppressed here)
agent_response → os_gate       (OS server accumulates assistant text for TTS)
os_gate → hw_emotion / hw_led / hw_servo / hw_audio   ([HW:...] markers fired by the OS server)
os_gate → hw_mood / hw_wellbeing / hw_music_suggestion / hw_posture   (OS-server log writes)
os_gate → tts_speak            (Gate passes if not suppressed → HAL TTS)
os_gate → tg_out               (Channel output)
os_gate → tg_alert → tg_out    (Broadcast alert)
```

Nếu loa thiết bị đang mute, HAL trả call TTS bằng HTTP 200 `{"status":"suppressed"}` và không phát gì — surface thành flow event `tts_muted` (xem bên dưới).

**Elbow routing**: Cạnh từ `local_match` tới output nodes (hw_emotion, hw_led, hw_servo, tts_speak) dùng đường gấp khúc đi vòng **bên trái** cột output để không cắt qua node trung gian. Cạnh từ `os_gate` tới các node ghi log đi phải → xuống → trái để không chạy xuyên qua `tg_alert`.

### Event → nhãn node (ô chi tiết runtime)

Thông tin node rút ra từ event của turn:
- `sensing_input` → node Sensing (type + message). Detail: `{ type }`.
- **Khung hình `look` đi kèm lượt thoại.** Khi tool `look` realtime chụp, HAL chép khung hình sang `/var/lib/hal/snapshots/sensing_look/` (`hal/realtime/look_monitor.py`, giữ 20 cái mới nhất) và gắn marker `[snapshot: ...]` vào chính message mà lượt đó đã gửi — nên tấm ảnh hiện **ngay trong lượt đã hỏi nó**, cạnh transcript và câu trả lời, thay vì là một event riêng không gắn với câu hỏi nào. os-server bóc marker trước khi text tới model nhưng vẫn giữ trong flow JSONL, giống hệt cách `motion.activity` làm. Khác với snapshot của `motion.activity`, **khung hình này CHÍNH LÀ thứ model đã thấy**, nên nó là vật chứng để đối chiếu với câu trả lời của model. (Nhánh `look.capture` chỉ-cho-monitor vẫn còn trong `system/server/sensing/delivery/http/handler.go` từ thiết kế trước; HAL không còn gửi nữa.)
- `chat_send` → `chat.send` gửi đi từ thiết bị. Detail: `{ type, run_id, has_session, has_image, image_bytes, message }`. `type` là `"user"` cho input user thật / sensing-driven, hoặc `"system"` cho thông báo nội bộ (skill watcher, wake greeting). Payload WS RPC gửi sang OpenClaw giống hệt nhau ở cả hai trường hợp — `type` chỉ gắn nhãn flow event để UI phân biệt. Xoay session **không** emit flow event `chat_send` (nó log `new_session_triggered`); đường auto-compact đang tắt nếu bật sẽ gọi thẳng RPC `sessions.compact` qua `CompactSession`.
- `sound_tracker` → được HAL Python push trực tiếp qua `POST /api/monitor/event`. Xuất hiện cạnh các turn `sensing_input` để hiện trạng thái escalation:
  - `{ action: "silent", occurrence: 1 }` — đã chuyển tiếp, agent giữ im lặng
  - `{ action: "persistent", occurrence: 3 }` — đã chuyển tiếp, agent sẽ nói
  - `{ action: "drop" }` — bị bỏ bởi dedup hoặc cửa sổ suppression
- `chat_input` → node Telegram In
- `intent_match` → node Local Match
- `lifecycle_start` → node Agent Call + dòng đầu tiên trong Event Pipeline.
- `tool_call` → mỗi lần gọi tool là một dòng Event Pipeline, kind=`tool`,
  label=`tool · <name>`, với phase `start`/`result` gộp vào thời lượng của
  dòng. Các cạnh HW đi ra (LED / servo / emotion / audio /
  os_gate) neo tại anchor `tool_exec` của pipeline.
  - **Cứu marker bị echo (2026-07-23):** marker `[HW:...]` chỉ tới HAL khi
    agent xuất nó ra **text trả lời** (interceptor Go chạy `extractHWCalls`
    trên assistant message). Thỉnh thoảng agent lại bọc marker trong một tool
    call shell — ví dụ `echo '[HW:/audio/play:{...}]'` — lệnh này chỉ in ra
    stdout trong sandbox và không bao giờ fire, nhưng tool args có chứa chuỗi
    marker nên node HW (ví dụ `hw_audio`) vẫn sáng: nhìn như đã chạy mà không
    phát gì. `fireEchoedHWMarkers`
    (`handler_hw.go`, gọi từ cả nhánh `tool` device-chat trong
    `handler_event_agent.go` lẫn nhánh `session.tool`) giờ phát hiện mọi
    marker `[HW:...]` trong tool args và fire thật, kèm log WARN.
    Nó chỉ khớp đúng ngữ pháp `[HW:/path:{json}]`, nên một lệnh
    `curl .../audio/play` hợp lệ (không có marker) không bị đụng và vẫn phát qua
    request riêng. Khi rescue chạy, bước emit node HW cosmetic chỉ-dựa-args bị
    bỏ qua để không nhân đôi node.
- `lifecycle_end` → node Response + dòng cuối trong Event Pipeline.
- `tts_send` → node TTS Speak + Output. Text output đọc từ `detail.data.full_text` (toàn bộ reply), fallback `detail.data.text`. Khi câu đầu của agent được stream tới TTS giữa turn (`tts_stream_send`, gửi sớm để giảm latency), `data.text` chỉ chứa **phần còn lại** (câu 1 đã bị cắt để không phát hai lần); `data.full_text` mang câu 1 + phần còn lại để web chat và flow Output hiện đủ reply. `data.streamed_len` là byte offset nơi phần còn lại bắt đầu.
- `tts_suppressed` → marker 🔇 ở cột gate. `data.reason` phân biệt: `channel_run` (turn user Telegram thật — phát hiện qua prefix runID `tg-` tự sinh trong handler `session.message`, hoặc mark trong map `channelRuns` từ fallback chat.history; reply fan-out qua session OpenClaw thay vì loa thiết bị), `already_spoken` (built-in tts tool đã route trước), `voice_agent_handled` (realtime voice agent đã nói turn này), `web_chat` (chat gõ tay — reply chỉ hiện trong UI chat; turn MQTT `mqtt_chat` cũng dùng đúng reason `web_chat` vì cả hai đều mark run qua `MarkWebChatRun`). Emit *thay cho* `tts_send` khi call `SendToHalTTS` thật sự bị bỏ qua — tránh UI báo sai là đã TTS. Lưu ý: không còn reason `music_playing` — phát nhạc không còn nuốt câu reply; OS server luôn gửi reply TTS và HAL serialize nó trước nhạc (reply nói trước, rồi mới phát nhạc). Classifier chỉ dùng bằng chứng dương: UUID run từ steer-mode self-fire của OpenClaw, cron fire và heartbeat KHÔNG phải `channel_run` và VẪN phát trên loa thiết bị.
- `tts_muted` → node TTS tô đỏ (giống suppressed); panel chi tiết hiện "🔇 speaker muted — reply not spoken", dòng gate "🔇 → TTS muted (speaker)". Emit ngay *sau* `tts_send` cùng run: reply đã gửi sang HAL, nhưng loa thiết bị đang mute nên HAL trả HTTP 200 `{"status":"suppressed"}` và KHÔNG synthesize hay phát gì (không tốn API call TTS). Go HAL client (`lib/hal` `SpeakReply`/`SpeakQueueReply`) decode body đó thành sentinel error `hal.ErrSpeakerMuted`, và handler log `tts_muted` `{run_id, text}`. Khác `tts_suppressed` (quyết định phía Go *trước* khi gửi, không có `tts_send` đi kèm — text chat lấy từ chính event suppress), text reply vẫn tới web chat qua `tts_send`; chỉ có tiếng là im.
- `token_usage` → node Response (số token).
- `cot_leak_filtered` → emit ở lifecycle:end khi bộ lọc CoT-leak đã bỏ câu khỏi reply. `data.dropped` là số câu, `data.preview` là preview có giới hạn của phần bị bỏ. Xem "CoT-leak filter" bên dưới.

### CoT-leak filter (đường agent)

Một số model chạy sau openclaw/hermes (điển hình DeepSeek) xả nguyên đoạn suy luận tiếng Anh thành assistant text trước câu trả lời thật ("The `[emotion_context]` shows … Route = **music**. I need to log the signal … [nhẹ nhàng] Có vẻ hơi trầm …"). `server/agent/delivery/http/cot_leak_filter.go` — bản port Go của `drivers/voice/_internal/cot_leak_filter.py` bên HAL (bản Python chỉ chặn đường transcript Gemini Live) — cắt các câu đó trước khi text tới TTS, web chat (`full_text`) và channel fan-out (Telegram DM/broadcast, Slack reply cuối). Giữ nguyên 3 tier như bản Python (TRIGGER marker → bật CoT mode + drop; SECONDARY chỉ drop khi CoT mode đã bật; CoT-mode continuation drop câu trông như tiếng Anh trên thiết bị non-English, draft trong ngoặc kép, mẩu plan cụt, câu trùng mờ), cộng thêm một TRIGGER riêng phía Go: identifier snake_case (`emotion_context`, `telegram_id`, …) — corpus DeepSeek mở đầu bằng kiểu này. Ngôn ngữ reply lấy từ `stt_language` trong `config.json`; chưa set → chế độ English (chỉ áp marker tier). Áp ở ba điểm:

- **First-sentence stream** (`tryFirstSentenceFlush`): candidate flush được lọc với state mới mỗi lần thử; nếu toàn bộ tới giờ là CoT thì hoãn flush (KHÔNG đánh dấu streamed offset) để câu sạch tới sau vẫn được stream sớm.
- **lifecycle:end**: full text được lọc với state mới (nuôi `full_text`, DM/broadcast, Slack reply); phần remainder cho TTS lọc bằng instance thứ hai được seed bằng prefix đã stream để CoT mode và bộ nhớ dedup nối liền qua ranh giới. Chỉ thay `text` khi có câu bị drop, nên turn sạch giữ nguyên whitespace gốc.
- **Channel-turn finalize** (đường `session.message`): channel turn không bao giờ TTS, nhưng text vẫn tới web chat / Flow Monitor qua `chat_response` và `tts_suppressed` — lọc luôn ở đó.

Raw delta trong `agent_last_token` giữ nguyên không lọc để debug. Slack streaming giữa turn (`chat.appendStream`) không lọc (diff append-only không rút lại text được); reply Slack cuối thì có.

Với yêu cầu nhạc được delegate, `skills/music/SKILL.md` yêu cầu HW marker đứng đầu, sau đó đúng một câu xác nhận ngắn; việc chọn bài, diễn giải transcript nhiễu và xử lý danh tính chưa biết phải giữ nội bộ. `skills/input-branching/SKILL.md` cũng không cho đọc quyết định định tuyến. Cả bộ lọc Go và HAL nhận diện nhãn lập kế hoạch đầu câu `They named a/the song:` và ghi chú `Speaker [identity] is unknown`, kể cả ở chế độ English. Kiểm thử hồi quy dùng đầu ra “Eternal Flame” được báo cáo, kiểm tra cả stream câu đầu lẫn toàn bộ câu trả lời. Đây là lớp chặn có phạm vi cụ thể; muốn xác định model hay proxy đưa reasoning vào assistant text vẫn cần raw response từ runtime/provider.

### NO_REPLY suppression

Agent có thể trả `NO_REPLY` (hoặc dạng cắt ngắn `NO`, `NO_RE`, `NO_...`) khi quyết định không trả lời — thường với passive sensing event như sound/motion. Chúng bị `isAgentNoReply()` trong `handler_text.go` suppress: không phát TTS, không hiện output. Match: đúng `"NO"`, hoặc chuỗi bắt đầu bằng `"NO_"` hay `"NO_RE"` (không phân biệt hoa thường sau khi trim). Nguồn: payload `lifecycle_end` nếu có, nếu không thì lấy từ RPC `chat.history` lúc `lifecycle_end` (async goroutine, best-effort). `lifecycle_end` của OpenClaw hiện không có dữ liệu usage, nên `chat.history` là nguồn chính.

**Chặn "kể lại việc im lặng".** Model đôi khi tả quyết định im lặng bằng văn xuôi thay vì trả sentinel (vd `Sound event, no user message. Nothing to say`). Đoạn đó lọt `isAgentNoReply()` nên `isMetaNonReply()` trong `handler_text.go` chặn thêm: text ≤ 100 byte, không có `?`, khớp một trong các cụm meta (`nothing to say/add/report`, `no (user) message/reply/response/comment (needed)`, `no need to reply/respond/speak/say`, `staying|remaining silent`, `no action needed`). Áp ở cuối lượt (`handler_event_agent.go`) và trong `tryFirstSentenceFlush()` (`handler_state.go`) — chỗ này defer mà KHÔNG đánh dấu run đã stream, để câu thật sau đó vẫn giữ được lợi thế latency first-audio. Cả hai đều log `WARN` và lượt được báo là `no_reply`.

### Cron-fire tự ép TTS

Khi OpenClaw emit `event:"cron"` với `action:"started"`, OS server đẩy thời điểm hiện tại vào một hàng đợi FIFO (`cronFireExpected`; OpenClaw bỏ `sessionKey` với job `sessionTarget="main"`, nên tương quan theo thời gian chứ không theo session). `lifecycle_start` kế tiếp có run ID dạng UUID lấy entry cũ nhất nếu nằm trong `cronFireWindowMs` (10 s) và đánh dấu run đó trong `cronFireRuns`; khi thuộc tập này, `isChannelRun=false` bị ép, nên loa thiết bị nói reply mà không cần marker `[HW:/speak]`. Marker vẫn giữ trong skill làm fallback defense-in-depth phòng khi cron event bị drop.

## Stream summary events (`agent_*_token` / `thinking_*_token`)

Delta thô `assistant_delta` và `thinking` được đẩy vào monitorBus (RAM) nhưng **không bao giờ ghi vào JSONL** — nếu không, lớp persist sẽ phình thêm ~50–500 dòng mỗi turn. Vì Flow Monitor đọc JSONL khi tải lại, pipeline rect của turn cũ sẽ không có dòng streaming nào.

Để lấp khoảng trống đó, stream handler của OpenClaw emit bốn flow event tóm tắt nhẹ cho mỗi run:

| Node | Khi nào | Payload (`data.*`) | Mục đích |
|---|---|---|---|
| `agent_first_token` | Delta `assistant` không rỗng đầu tiên trong run | `{run_id}` | Marker TTFT — field `ts` = thời điểm latency reply cảm nhận được |
| `agent_last_token` | `lifecycle.end` (drain accumulator) | `{run_id, text, chunks, chars}` | Đóng dòng streaming assistant trong pipeline rect |
| `thinking_first_token` | Delta `thinking` không rỗng đầu tiên (chỉ extended-thinking) | `{run_id}` | Như trên, cho stream thinking |
| `thinking_last_token` | `lifecycle.end` | `{run_id, text, chunks, chars}` | Như trên, cho stream thinking |
| `narration_demoted` | Một `tool` start tới trong khi buffer `assistant` đang có text | `{run_id, tool, text}` | Text stream TRƯỚC một tool call là model kể kế hoạch ("Let me take a look."), không phải reply. Handler bỏ nó khỏi reply buffer (nên không bao giờ tới web chat / TTS ở `lifecycle.end`) và hiện ở dòng thinking. Chung mọi runtime; bỏ qua nếu câu đầu đã stream ra TTS hoặc text có marker `[HW:...]` |

Tối đa thêm 4 dòng JSONL mỗi turn (thường 0–2). Stream từ OpenClaw trong code vẫn gọi là `"assistant"` (`handler_event_agent.go: case "assistant"`); chỉ tên node JSONL dùng prefix `agent_` cho nhất quán với các node `agent_thinking` / `agent_call` / `agent_response` hiện có.

State nằm trong `AgentHandler.streamStats` (bộ đếm + text tích lũy theo run), độc lập với `assistantBuf` (phục vụ TTS flush). Drain ở `lifecycle.end`. Xem `recordAssistantDelta` / `recordThinkingDelta` / `drainStreamStats` trong `handler_state.go`.

Frontend (`aggregateEvents` trong `helpers.ts`) dựng dòng pipeline từ `*_first_token` (mở dòng) + `*_last_token` (đóng dòng với `chunks`/`chars`). `extractTurnTiming` và `turnFirstTokenMs` đều fallback sang các marker này khi delta live không có trong `turn.events`.

Flow event legacy `llm_first_token` từng bị bỏ vì "trùng với pipeline aggregator" thực chất được đưa lại ở đây — tách thành hai stream (`agent_*` và `thinking_*`), vì aggregator không quan sát được thời điểm streaming khi delta thô không bao giờ tới JSONL.

## Hiển thị Turn Item

```
[icon] TYPE  PATH  STATUS  👤 user  ⏱ total  ⚡ ttft
id: run-id
IN   <input text>
[snapshot strip — up to 3 thumbnails]
[🪑 LOAD MORE · pose bucket <id>]   ← only when motion.activity carried a posture nudge
OUT  🔊 <output text>
N events
```

- **IN**: lấy từ summary của `sensing_input` hoặc detail.message của `chat_input`
- **OUT**: từ `intent_match` (local) hoặc `tts_send` (agent). Intent match là nguồn chính thức và không bị tts_send cũ của run khác ghi đè.
- **Path badge**: LOCAL (xanh lá) / AGENT (xanh dương) — chỉ set từ event thuộc cùng run
- **⏱ total**: `turn.startTime → turn.endTime` (toàn bộ cửa sổ server quan sát: input event → lifecycle_end / tts_send / chat_final). Xanh ≤5s, amber ≤15s, đỏ >15s.
- **⚡ TTFT** (time-to-first-token): `turn.startTime → first thinking/assistant_delta`. Khớp với timestamp agent bubble trên trang chat — lúc user *thấy* reply bắt đầu (delta đầu với runtime stream; `chat_response`/`tts_send` cuối với runtime không stream như codex). Khoảng giữa ⚡ và ⏱ = streaming phần đuôi + đóng lifecycle. Xanh ≤3s, amber ≤8s, đỏ >8s. Ẩn khi không có LLM stream (vd local intent match).
- **Snapshot strip**: lấy từ marker `[snapshot:]` trong `sensing_input`. Với `motion.activity` có pose bucket, strip giới hạn 3 ô (snapshot activity + hai snapshot pose tệ nhất). Click một ô mở lightbox inline.
- **Popup pose bucket**: khi có `[pose_bucket:]`, nút `LOAD MORE` mở `PoseBucketModal`, fetch `/api/hardware/sensing/pose-bucket/<id>` (proxy về lelamp) và render bảng đầy đủ từng sample — cùng lưới monospace + click-thumbnail-mở-lightbox như tab Sensing live. Dòng có filename trong `worst_snapshots` được highlight (viền đỏ, ⭐).
- **Clip audio debug** (`speech_emotion.detected`): một player `<audio controls>` click-to-play gắn nhãn `🎙 debug` được render cho mỗi audio URL, để nghe chính xác clip đã tạo ra emotion được detect. Đường dẫn clip trên Pi tới qua field `audio` (tùy chọn) trong body `POST /api/sensing/event`. `system/server/sensing/delivery/http/handler.go` chuyển basename của path thành URL servable (`audioURLForPath` → `/api/sensing/audio/<file>.wav`) và lưu vào `Detail` của monitor event ở key `audio` — **chỉ URL basename, không bao giờ là raw path**. Frontend `turnIO()` (`helpers.ts`) rút các URL này vào `audioUrls` từ `detail.audio` của event `sensing_input`; `TurnBadge.tsx` render player. **Đây là affordance CHỈ-ĐỂ-DEBUG — audio KHÔNG BAO GIỜ gửi cho LLM.** Path nằm trong field JSON riêng, không bao giờ nằm trong text tin nhắn chat, nên tự nhiên bị loại khỏi những gì agent thấy — giống cách snapshot `motion.activity` được hiện trên UI nhưng bị strip trước khi tới LLM.
  - **Route**: `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`) serve file `.wav` theo basename từ `/var/lib/hal/speech-emotion` hoặc `/tmp/hal-speech-emotion`, với validation basename nghiêm ngặt — tên phải kết thúc `.wav` và không chứa `/`, `\`, hay `..` (nếu không → `404`).

Hai badge nên đọc cùng nhau: ⚡ là latency *cảm nhận* (user thấy), ⏱ là latency *server* (ops thấy). Khoảng cách lớn = streaming phần đuôi nhiều; khoảng cách nhỏ = reply ngắn hoặc đóng lifecycle nhanh.

## Trạng thái memory theo turn (`lifecycle_start.memory`, `memory_changed`)

Issue #421: một dòng `USER.md` do agent tự ghi đã làm hỏng skill routing gần
cả ngày vì không có gì cho thấy turn đó chạy với memory nào. Hai bổ sung:

- Flow data của `lifecycle_start` mang thêm `memory`: `{"USER.md": {"size",
  "sha8"}, "MEMORY.md": …, "KNOWLEDGE.md": …}` cho runtime **đang active** —
  fingerprint do memory guard của OS publish (`docs/os-server.md`, mục "Memory
  guard"). Chỉ có size và 8 hex đầu của sha256; không bao giờ có nội dung.
- `memory_changed` (kind `event`) được guard emit từ fsnotify watch mỗi khi có
  ghi vào `USER.md` / `MEMORY.md` của bất kỳ runtime nào. Event phát ra ~2 s
  sau lần ghi (debounce) và gắn trace đang active tại thời điểm đó — thường là
  turn đã ghi, nhưng có thể là turn sau nếu turn mới đã bắt đầu, hoặc không có
  trace nếu turn đã kết thúc. Data: `file`, `runtime`, `path`, `size`, `sha8`,
  `quarantined` (số block bị gỡ — hoặc, khi `execute` là false, số block guard
  lẽ ra đã gỡ), `reasons` (`free-prose` | `unknown-label` | `prescriptive`),
  `execute` (false ở chế độ chỉ quan sát, `agent.memory_guard=false`),
  `trigger` (`startup` | `watch` | `rescan`).

Footer của turn card gate badge theo state (#463) — `flow` là section public và
debug chỉ là toggle một cú click trên header, không phải URL param ẩn:

| State | Nghĩa | Chế độ thường | Chế độ debug |
|---|---|---|---|
| xám | turn chỉ đọc memory | ẩn | `USER.md 251B · MEMORY.md 1.2kB` |
| amber | agent có ghi, guard chấp nhận | ẩn | `… ✎ memory changed` |
| đỏ | guard đã gỡ block | `✎ memory updated · 1 entry removed in hermes` | như trên, thêm size phía trước |

Xám hiện size của một file mà chủ máy không mở được, trên mọi turn; amber sáng
ở mọi lần ghi thành công sẽ tập cho người dùng bỏ qua dòng này, và rồi đỏ cũng
không còn được chú ý — nên cả hai là công cụ debug. Đỏ thì luôn hiện: đây là
chỗ duy nhất trong sản phẩm mà chủ máy thấy được OS đã xoá thứ agent viết ra
(phần còn lại của việc gỡ là atomic rename, file `.quarantine.txt` và
`.bak-<nano>`, chỉ xem được qua SSH, và endpoint reset của #421 ship mà không
có UI). Chữ dùng là "entry removed", không phải "quarantined" — từ này
đọc lên giống một sự cố bảo mật — và badge nêu luôn `runtime` lấy từ
`memory_changed`: guard quét cả sáu runtime tree trong khi size bên cạnh là của
runtime **đang active**, nên nếu không nêu tên thì một lần gỡ ở runtime không
active sẽ làm card đỏ lên cạnh size của file chẳng liên quan.

Size là byte với đơn vị dính liền số (`251B`, `1.2kB`) — không dùng `k` cơ số
1000 như các số token LLM cùng hàng. Tên file giữ nguyên đuôi `.md` và ngăn
nhau bằng dấu chấm giữa, để dòng này đọc ra hai file hai size chứ không phải
một nhãn dính liền. Ở chế độ chỉ quan sát (`agent.memory_guard=false`) badge
vẫn amber và ghi `· would remove 1 entry`; chưa gỡ gì cả, file vẫn còn block đó.
Hover để xem số byte chính xác, `sha8`, runtime và reasons. So `sha8` giữa hai
turn cho biết memory có thay đổi giữa hai turn đó hay không.

Logic của badge nằm ở `system/web/src/pages/monitor/FlowSection/memory.ts` và có
unit test: `cd system/web && node --test tests/memory.test.mjs`.

## Edge case đã biết

### 1. OpenClaw gán run_id khác
OpenClaw 5.2 (và một số path 5.4 hiếm) sinh UUID cho embedded run; 5.4 phần lớn echo `idempotencyKey`. Logic ánh xạ phải xử lý cả hai.
- **Fix**: SSE handler chọn nhánh theo định dạng `payload.RunID` — device-format → tìm-và-xoá entry trong queue; UUID → matcher message hiện có + map. Telemetry dùng một match exact duy nhất riêng. Xem mục ở trên.
- **Edge case**: Nếu server restart giữa `sendChat` và `lifecycle_start`, global trace bị mất và không tạo được ánh xạ. Stitching ở frontend xử lý như fallback.
- **Trạng thái**: Reply nhanh đăng ký trace trước khi ghi. Turn xếp hàng lâu giữ bằng chứng telemetry trong giới hạn riêng 24 giờ/1024 entry mà không kéo dài TTL routing. Restart, fetch history lỗi, và match thiếu hoặc mơ hồ vẫn có thể để KPI execution không khớp; stitching UI không phải bằng chứng execution.

### 1a. Gán nhầm do pending-trace mồ côi (regression ở 0.0.465, fix ở 0.0.468)
Một lần thử trước (commit `1897dfee`) bỏ hẳn bước pop FIFO khi runId là device-format, nhưng để entry lại trong queue. UUID lifecycle kế tiếp pop entry mồ côi đó và bị gán nhầm — pattern quan sát: reply Phase 1 + reply Phase 2 cùng render dưới chat-N của Phase 1, trong khi nội dung Phase 2 thực ra thuộc về chat kế tiếp trong đợt drain.
- **Trigger**: drain/burst flush nhiều chat.send; cái đầu trả Phase 1 với runId device-format; UUID lifecycle (Phase 2) của chat kế tiếp kéo entry mồ côi.
- **Triệu chứng**: turn assistant hiện hai reply không liên quan dưới cùng một runId; lệch-một dây chuyền khoảng ~2 phút cho tới khi TTL entry mồ côi hết.
- **Fix**: thay skip bằng `RemovePendingChatTraceByRunID(payload.RunID)` để xoá đúng entry khớp thay vì để lại làm mồ côi.

### 2. sensing_input enter không có run_id
`flow.Start("sensing_input")` fire trước khi `sendChat()` trả run ID. Event đầu tiên của turn không có trace_id.
- **Giảm thiểu**: Frontend gán runId cho turn từ các event sau (`sensing_input` exit có ID).
- **Trạng thái**: Hoạt động. `isTurnStart` phát hiện event, `extractEventRunId` từ các event sau điền ID.

### 3. Sensing event đồng thời
Hai sensing event tới sát nhau: `SetTrace` của turn B ghi đè global trace của turn A. Lifecycle event của turn A có thể mang trace của turn B.
- **Giảm thiểu**: runID theo từng event nghĩa là mỗi `flow.Log` mang ID riêng bất kể global state. Global trace chỉ dùng cho heuristic Telegram.
- **Trạng thái**: Phần lớn đã fix. `sensing_input enter` vẫn chưa có ID theo event (trước sendChat).

### 4. TTS kép
Cả agent stream (flush ở `lifecycle_end`) và chat stream (`chat final assistant`) đều có thể gửi TTS cho cùng một response.
- **Trạng thái**: Trước đây được theo dõi bằng một TODO trong agent handler; TODO đó không còn trong `system/server/agent/delivery/http/`. Hướng fix dự định: dedup bằng guard theo runID (trạng thái dedup hiện tại chưa được kiểm lại).

### 5. Server restart mỗi ~20s
Reconnect WebSocket gây restart cấp process (bộ đếm seq reset). Nhiều khả năng đây là vấn đề ổn định riêng, không phải bug của monitor.
- **Ảnh hưởng**: Mất trace giữa turn, event bị tách qua các lần restart.
- **Giảm thiểu**: runID theo từng event + stitching frontend xử lý phần lớn trường hợp.

### 6. Tool `tts` built-in của OpenClaw bỏ qua loa HAL (ĐÃ FIX)
Agent gọi tool `tts` built-in của OpenClaw thay vì trả assistant text. OpenClaw sinh audio phía server (`"Generated audio reply."`) nhưng không bao giờ route tới loa vật lý (`/voice/speak` trên HAL). Sau đó agent trả `NO_REPLY`, nên OS server không có assistant text để flush → im lặng.
- **Nguyên nhân gốc**: OpenClaw cung cấp tool `tts` built-in khi `tools.profile = "full"`. SKILL.md sensing hướng dẫn agent gọi `/voice/speak`, agent map nhầm sang tool `tts` built-in thay vì dùng `curl` tới HAL.
- **Fix**: (1) Deny tool `tts` built-in của OpenClaw qua `tools.deny: ["tts"]` trong config. `tools.disabled` KHÔNG phải key hợp lệ của OpenClaw — dùng `tools.deny` (deny thắng `tools.profile`). Lưu ý: `runtimes/openclaw` không còn ghi key này; trong code chỉ còn interceptor bên dưới. (2) Intercept fallback (`toolName == "tts"` trong `handler_event_agent.go` và `handler_event_session_tool.go`): nếu agent vẫn gọi tool `tts`, lấy text và route sang `SendToHalTTS()`. (3) Cập nhật SKILL.md sensing và SOUL.md để agent trả text thường — pipeline tích lũy assistant-delta của OS server tự route sang HAL TTS.
- **Trạng thái**: Đã fix ở v0.0.138.

### 7. Khoảng trống hiển thị tool-call của OpenClaw (có action mà không có `tool_call`)
Đã gặp ở nhiều turn Telegram: user yêu cầu một action thiết bị (vd đổi màu LED) và trạng thái/output thiết bị xác nhận action, nhưng log flow/debug chỉ có lifecycle + assistant/tts và không có event `tool_call`.

- **Ảnh hưởng**: node `TOOL` có thể không sáng dù action có vẻ đã chạy.
- **Trạng thái hiện tại**: đã bật log raw payload của OpenClaw (`source: "openclaw_raw"`), nhưng vẫn có run không thấy payload `stream:"tool"`.
- **Câu hỏi mở**: OpenClaw có thể chạy nhánh nội bộ không emit tool stream, hoặc action chỉ được suy ra từ assistant text mà không có tool invocation tường minh.

## Compaction summary inspector

Compaction do chính runtime thực hiện (OpenClaw mode `safeguard` gần giới hạn context); trigger auto-compact riêng của OS server hiện đang tắt, thay bằng xoay session (`new_session_triggered`, 150k reported tokens trên OpenClaw) — xem [agent-compaction_vi.md](agent-compaction_vi.md). Mỗi lần compact ghi một record `type:"compaction"` vào `/root/.openclaw/agents/main/sessions/<sessionId>.jsonl`, chứa chuỗi `summary` được chèn đầu prompt của mọi turn kế tiếp cho đến lần compact sau. Rule bị copy/generalize nhầm vào summary có thể đè SKILL.md (summary nằm trước trong prompt và được coi như "context đã chốt").

**UI:** header Flow Monitor có nút `📋 Summary`. Click → fetch + render modal hiện record compaction mới nhất: timestamp, `tokensBefore`, `summaryChars`, `compactionCount`, `readFiles` được đưa vào prompt compaction, và toàn văn `summary`.

**Endpoint:** `GET /api/agent/compaction-latest?session=<key>&at=<iso-ts>` (admin auth; session key mặc định `agent:main:main`; `at` rỗng = record mới nhất). Trả về:

```json
{
  "status": 1,
  "data": {
    "found": true,
    "sessionKey": "agent:main:main",
    "sessionFile": "/root/.openclaw/agents/main/sessions/<id>.jsonl",
    "compactionCount": 18,
    "id": 17170331,
    "parentId": "369818c9",
    "timestamp": "2026-04-24T03:21:30.305Z",
    "nextTimestamp": "",
    "tokensBefore": 80458,
    "summaryChars": 14263,
    "summary": "...",
    "details": { "readFiles": ["..."], "modifiedFiles": ["..."] },
    "fromHook": true,
    "firstKeptEntryId": 17170331,
    "atQuery": ""
  }
}
```

Dùng khi agent viện rule mà không tìm thấy trong bất kỳ `skills/**/SKILL.md` nào — nguồn gần như luôn là compaction summary, không phải skill đang load. Handler: `system/server/agent/delivery/http/handler_api_compaction.go`.

## Danh sách Turns vs log tải về

| Nguồn | Phạm vi |
|--------|--------|
| **Danh sách Turns** (Monitor) | Được cấp bởi SSE `GET /api/agent/flow-stream`: mỗi khi `flow_events_*.jsonl` của hôm nay thay đổi, server đẩy snapshot đầy đủ của **500 dòng cuối** (`recentFlowFromJSONL(day, 500, …)`); client giữ tối đa `FLOW_EVENTS_MAX` (10000) dòng, nên cửa sổ thực tế là 500 dòng. `groupIntoTurns` sau đó trả **mọi** turn (không giới hạn). |
| Nút **↓ Bundle** | Một click tải **hai** file (tooltip của nút ghi ba; `downloadFlowBundle` chỉ làm hai): (1) `GET /api/agent/flow-logs?last=10000` qua `fetch` + lưu blob (`flow_YYYY-MM-DD_last10000.jsonl`) — server giới hạn `last` ở **2000**, nên đây là đuôi 2000 dòng, **rộng hơn** feed UI 500 dòng; (2) JSON phía client gồm `events[]` + `turns[]` đã gom (`flow_ui_snapshot_*.json`, format `os-monitor-ui-snapshot-v1`). |
| Link **Full day** | `GET /api/agent/flow-logs` — cả file của ngày; có thể **dài hơn** cửa sổ UI, nên Turns **không** phải bản dựng lại của toàn bộ file. |
| `GET /api/agent/flow-events` | Query JSON (không phải SSE): `date`, `last` (mặc định 500, tối đa 10000). Được Chat dùng (`last=500`), không phải danh sách Turns. |

Turns hiện mọi turn dựng được từ các event đã stream. So sánh server với UI nên dùng **↓ Bundle** (nhớ rằng đuôi JSONL rộng hơn cửa sổ UI) hoặc `flow-logs?last=500` + JSON snapshot UI để khớp chính xác.

## File

| File | Vai trò |
|---|---|
| `system/lib/flow/flow.go` | Emit flow event, persist JSONL, API runID theo từng event |
| `system/server/sensing/delivery/http/handler.go` | Sensing input → flow.Start/End kèm runID |
| `system/server/agent/delivery/http/handler_event_agent.go` | Agent event → flow.Log với payload.RunID, phát hiện turn |
| `system/server/agent/delivery/http/handler_api_flow.go` | Endpoint `flow-events`, `flow-stream` (SSE), `flow-logs` |
| `runtimes/openclaw/service_chat.go` | sendChat trả idempotencyKey làm runID |
| `system/web/src/pages/monitor/FlowSection/helpers.ts` | `groupIntoTurns`, `turnIO`, `extractNodeInfo`, `aggregateEvents` |
| `system/web/src/pages/monitor/FlowSection/FlowDiagram.tsx` | `FlowDiagram` (node SVG, `positions`, `edges`) |
| `system/web/src/pages/monitor/FlowSection/index.tsx` | Mục Flow, tải Bundle / Full day |

Bản tiếng Anh: [`docs/flow-monitor.md`](../flow-monitor.md).

Kết quả cuối Harness được ghi vào flow JSONL bằng `harness_response`, giữ run ID thiết bị gốc và `text` đầy đủ. Web Chat dùng sự kiện này khôi phục kết quả đang chờ sau khi SSE ngắt hoặc tải lại trang. Luồng trực tiếp vẫn phát `chat_response` với state `final`. Câu hỏi Harness và thông báo xin quyền gửi tới một run thiết bị cũng ghi `harness_question` (`run_id`, `question_id`, `text`); Web Chat bổ sung dòng còn thiếu vào bubble đang chờ mà không đóng lượt, nên tab bị ẩn (SSE đã đóng) vẫn thấy.

Lượt voice do realtime xử lý và lượt history sync dùng ID riêng: `device-realtime-…` cho hội thoại gốc, `device-chat-context-…` cho đồng bộ. Event `realtime_response` lưu câu hỏi/câu trả lời và đóng card gốc; card History sync theo lifecycle riêng. `history_run_id` liên kết mà không gộp hai lượt. Event cũ đã lưu cùng ID vẫn giữ cách hiển thị gộp trước đây.

### Nhãn voice command và follow-up

`sensing_input` và `realtime_response` có thể mang `data.voice_turn_type` (`voice`, `voice_command`, hoặc `voice_followup`). Flow Monitor ghép trường này với loại event handled: wake command do realtime xử lý hiện `VOICE_COMMAND_HANDLED`, follow-up hiện `VOICE_FOLLOWUP_HANDLED`, lượt handled thường hoặc cũ giữ `VOICE_AGENT_HANDLED`. Badge, bộ lọc subtype và search dùng cùng nhãn; search cũng nhận loại event gốc. Bộ lọc loại trừ đã lưu được mở rộng sang các subtype mới. Đây chỉ là thay đổi hiển thị; loại event, gom run, đường realtime/main, queue và cancel giữ nguyên. LIVE ON/OFF dùng cùng bộ phân loại wake phrase của HAL. Follow-up LIVE đã được cho phép giữ phân loại wake focus dù window hết trước khi provider trả lời. Reply realtime vẫn giữ `voice_agent_handled` để đồng bộ history im lặng. Row cũ thiếu metadata giữ nhãn cũ; history sync không lấy phân loại từ lượt voice bên cạnh.

## Tiến độ Harness Store

Snapshot quan sát được từ `agent.prepare` / `operation.get` ghi `harness_store_progress` với run ID local, operation ID, state, phase và nội dung hiển thị có giới hạn. Khi lượt main-agent còn active, luồng `assistant_delta` hiện có hiển thị các quan sát; preparation không chặn phản hồi main hay bật TTS. Khi delivery task đã sở hữu phản hồi, progress preparation đến muộn không được thay thế. Đây là dữ kiện từ polling, không phải protocol event Harness mới. `ready` chỉ nghĩa chuẩn bị agent, không hoàn thành task người dùng. Main vẫn nhận guidance/lỗi cần thao tác. Không thêm card frontend hoặc polling nền tự động; xem [Harness Store](harness-store_vi.md).

### Hết hạn chờ preparation Harness

`harness_preparation_wait_expired` ghi deadline lượt phản hồi local, với `task_dispatched:false`; không phải lỗi operation từ xa hay kết quả task. Native Hermes kết thúc đúng owner qua `lifecycle_error` sau hủy có giới hạn. Lỗi có prefix `OS_RUN_EXPIRED:` bỏ qua recovery câu trả lời dở để không biến lượt chờ hết hạn thành thành công được khôi phục. Progress preparation bỏ snapshot liên tiếp trùng theo run/operation active và ngăn các thông báo thay đổi bằng đoạn mới.

### Log đo thời gian TTS của main agent

Log dịch vụ OS-server có các mốc `[tts-timing]` cho câu trả lời streaming và gửi TTS cuối turn. Đây là log chẩn đoán, không phải event Flow Monitor hay endpoint KPI acknowledge mới:

- `sentence_ready`: `first_delta_to_ready_ms` đo từ assistant delta không rỗng đầu tiên OS-server nhận được đến khi có câu đầu đủ điều kiện đọc. Bao gồm thời gian gom câu và các gate an toàn hiện có, không bao gồm thời gian model trước delta đầu.
- `sentence_dispatch`: `ready_to_dispatch_ms` bao gồm gọi hardware ở đầu câu và hủy filler trước khi gửi TTS.
- `delivery_start` / `delivery_complete`: `dispatch_to_send_ms`, `send_ms`, `dispatch_to_complete_ms` tách thời gian chờ goroutine khỏi lời gọi gửi qua runtime.
- `hal_post_start` / `hal_post_complete`: `http_ms` đo HTTP request tới HAL đến khi đọc response. `success` nghĩa là lời gọi không trả lỗi (bao gồm kiểm tra response muted hiện có), không chứng minh loa đã phát tiếng.

Các mốc có `run_id` và `text_key` là 12 ký tự hex đầu SHA-256, không lặp lại nội dung câu hay credential. Hash ở handler dùng text trước bước làm sạch của runtime; hash HAL POST dùng payload cuối và nối được với HAL `queue_requested` khi text không đổi. Nếu runtime bỏ markdown/tag, dùng hash POST cuối để đối chiếu. Lời nói không gắn turn theo đường cũ có thể có run ID rỗng. Không có mốc câu đầu nếu streaming không đủ điều kiện hoặc chưa có câu hoàn chỉnh an toàn trước final flush. Không thay đổi gate ownership/silence hay cách buffer TTS.

`assistant_end` và event `agent_last_token` hiện có bổ sung `first_delta_to_end_ms` khi biết thời điểm delta đầu. Mốc này bao phủ câu trả lời chỉ phát lúc cuối và đo toàn bộ khoảng xuất assistant text kể cả khi câu đầu đã streaming. `final_dispatch` ghi `end_to_buffer_ready_ms`, `buffer_ready_to_dispatch_ms`; `streamed_len` cho biết có phải phần còn lại hay không. “Buffer ready” là lúc lấy text cuối từ assistant buffer; khoảng sau đó bao gồm làm sạch text, gọi hardware và kiểm tra routing. Chỉ ghi dispatch khi thực sự đi vào đường gửi TTS, không ghi cho câu bị suppress hoặc rỗng. Nếu không có thời điểm delta đầu thì bỏ trường đo, không thay bằng số 0.


### Nhãn kết quả chung Harness

Flow card gắn nhãn `Harness` cho kết quả và `Shared result` cho tham chiếu từ các
input còn lại; tooltip tham chiếu có run ID nhận kết quả đầy đủ. `harness_response`
là giao kết quả, không phải bằng chứng đã phát TTS. Nội dung tham chiếu vẫn đóng
đúng turn pending và khôi phục qua JSONL/SSE; chỉ input mới nhất trong nhóm hiển
thị toàn bộ câu trả lời. Kết quả chung được gửi TTS một lần theo input mới nhất,
tuân thủ điều kiện voice và cancellation. Các nhãn không khẳng định đã nghe âm thanh.
