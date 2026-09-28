# Flow Monitor (tiếng Việt)

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

Tài liệu đầy đủ bằng tiếng Anh: [`docs/flow-monitor.md`](../flow-monitor.md).

## Tóm tắt

Flow Monitor là lớp quan sát end-to-end cho agent turn: ghi JSONL (`local/flow_events_YYYY-MM-DD.jsonl`), stream SSE tới UI. **Chỉ quan sát** — không đổi hành vi thiết bị hay business logic.

**Run ID từ thiết bị (`chat.send`):** idempotency dùng tiền tố `lamp-chat-*` (trước đây `lamp-sensing-*`). Đó là **mọi** tin gửi qua WebSocket từ thiết bị (sensing POST, wake greeting, …), **không** có nghĩa log đó chỉ là sound/voice — đừng nhầm với Telegram chỉ vì thấy chữ “sensing” trong log cũ.

**Map UUID → `lamp-chat-*`:** Hành vi runId của OpenClaw phụ thuộc version. **5.2** (và một số path 5.4 hiếm) generate UUID mới — thiết bị map UUID → idempotencyKey. **5.4** chủ yếu echo idempotencyKey trực tiếp làm runId — runId đã là device trace, không cần map. Một chat.send có thể tạo cả Phase 1 (echo) lẫn Phase 2 (UUID embedded run) trong burst/drain. SSE handler branch theo `payload.RunID` format: device-format → `RemovePendingChatTraceByRunID` (xoá entry match khỏi queue, không map); UUID → matcher message hiện có (exact/prefix và chọn entry cũ nhất). Trace lưu trước khi ghi WebSocket, xoá khi ghi lỗi; TTL routing vẫn 2 phút, pending-send busy vẫn 30 giây. Telemetry dùng buffer và alias riêng, tối đa 1024 entry trong 24 giờ, chỉ nhận đúng một match toàn bộ sau trim; không dùng phỏng đoán routing khi match mơ hồ. Alias metric không tham gia TTS hay dispatch. History lỗi vẫn có thể làm thiếu correlation. Sau đó `resolveRunID` dùng cho agent stream **và** luồng `chat` để tránh cùng một turn bị hai `run_id` trên Monitor.

**Pending-trace orphan (regression 0.0.465, fix 0.0.468):** Bản trước skip pop khi runId device-format → entry kẹt lại làm orphan → UUID lifecycle kế tiếp pop nhầm → 2 reply khác nhau bị gắn cùng 1 turn (cascade off-by-one ~2 min cho tới khi TTL hết). Fix: dùng `RemovePendingChatTraceByRunID` để xoá entry chính xác thay vì skip.

**Sensing `enter` vs `chat_send`:** Handler gọi `NextChatRunID` + `flow.SetTrace` **trước** `flow.Start` để dòng `enter` trong JSONL cùng `trace_id` với `chat_send`. Trước đây `SetTrace` chỉ chạy sau khi gửi WS nên `enter` còn dính turn trước (turn “ma” / export Pair lệch).

**Log tương quan:** grep `flow correlation` — các `op`: `ws_chat_send`, `hal_agent_out`, `openclaw_uuid_map`, `chat_run_resolve`. Chi tiết bảng trong `docs/flow-monitor.md`.

**Field `type` trong `chat_send`:** event `chat_send` có field `type` = `"user"` (user thật / sensing-driven) hoặc `"system"` (skill watcher, wake greeting). Phân biệt chỉ ở flow event — WS RPC `chat.send` gửi sang OpenClaw giống hệt nhau. Auto-compact **không** sinh `chat_send`; nó gọi RPC `sessions.compact` trực tiếp qua `CompactSession`.

**Đo TTFT / warmup:** Khoảng `lifecycle_start → first thinking/assistant delta` = LLM warmup thực (model reasoning silently trước khi token đầu chảy ra). OS server tính từ marker JSONL `agent_first_token` / `thinking_first_token` (xem dưới) hoặc fallback sang live delta event trong RAM nếu có.

**Stream summary events (re-added 2026-05-19):** Raw `assistant_delta` / `thinking` deltas chỉ ở RAM (monitorBus), KHÔNG ghi JSONL — để tránh ~50–500 dòng/turn. Hậu quả: load lại Flow Monitor cho turn cũ → pipeline rect mất hẳn row streaming. Fix: backend emit 4 flow event nhẹ thay thế:

| Node | Khi nào fire | `data.*` |
|---|---|---|
| `agent_first_token` | Delta `assistant` đầu tiên | `{run_id}` (ts = TTFT moment) |
| `agent_last_token` | `lifecycle.end` drain accumulator | `{run_id, text, chunks, chars}` |
| `thinking_first_token` | Delta `thinking` đầu tiên (chỉ extended thinking) | `{run_id}` |
| `thinking_last_token` | `lifecycle.end` | `{run_id, text, chunks, chars}` |
| `narration_demoted` | `tool` start tới khi buffer `assistant` đang có text | `{run_id, tool, text}` — text stream TRƯỚC một tool call là model kể kế hoạch ("Let me take a look."), không phải reply. Handler bỏ nó khỏi reply buffer (không tới web chat / TTS ở `lifecycle.end`) và hiện ở row thinking. Chung mọi runtime; bỏ qua nếu câu đầu đã stream ra TTS hoặc text có marker `[HW:...]` |

Tối đa 4 dòng JSONL bonus / turn (thực tế 0–2). Stream name từ OpenClaw vẫn là `"assistant"` ở code level — chỉ JSONL node dùng prefix `agent_` cho khớp các node hiện có (`agent_thinking`, `agent_call`, `agent_response`). State live trong `OpenClawHandler.streamStats`, độc lập với `assistantBuf` (phục vụ TTS flush). Drain ở `lifecycle.end`. Trước đây có `llm_first_token` event đã bị bỏ vì "redundant với pipeline aggregator" — lý do đó sai, aggregator không observe được khi raw deltas không bao giờ tới JSONL.

**Badge `⏱` vs `⚡` trên Turn card:**
- **⏱ total** = `turn.startTime → turn.endTime` (input event → `lifecycle_end` / `tts_send` / `chat_final`) — toàn bộ window server-side. Đây là **server-observed turn duration**.
- **⚡ TTFT** = `turn.startTime → first thinking/assistant_delta` — khớp với timestamp agent bubble trên chat page (lúc user **thấy** reply bắt đầu — delta đầu tiên với runtime stream; `chat_response`/`tts_send` cuối với runtime không stream như codex). Đây là **perceived latency**.
- Khoảng cách ⚡ ↔ ⏱ = tail-streaming các token còn lại + lifecycle close. Reply ngắn → 2 con gần bằng nhau; reply dài → gap rõ rệt.
- Ngưỡng màu: ⏱ green ≤5s / amber ≤15s / red >15s. ⚡ green ≤3s / amber ≤8s / red >8s.
- ⚡ ẩn khi không có LLM stream (local intent match, dropped, queued).

**Khoảng `chat_send → lifecycle_start`** = OpenClaw init (network + load session/context + boot agent), KHÔNG phải LLM. Đo từ `chat_send` (OS server) tới `lifecycle_start` (OpenClaw event đầu tiên).

**Agentic Runtime section trên diagram (2026-05-08 redesign):** 3 node cũ (LLM Start / Thinking / Tool Exec) đã được gộp thành 1 **Event Pipeline rect** chạy giữa Agent Call và Response. Rect hiển thị danh sách events do OpenClaw emit, gộp các delta liên tiếp cùng loại thành 1 dòng tóm tắt (`thinking · 5.2s · 200 chunks · ~4k chars`). Edges ra HW (LED/servo/emotion/audio/lamp_gate) anchor từ cạnh phải pipeline. Aggregation rules + lý do redesign: `docs/debug/flow-monitor-pipeline.md`.

## Sơ đồ Turn Pipeline (SVG)

Component `FlowDiagram` trong `system/web/src/pages/Monitor.tsx` vẽ **ba vùng** (màu viền nền). Có thể kéo bằng chuột hoặc một ngón tay để pan; chụm/mở hai ngón tay (hoặc dùng nút trừ/cộng trong canvas) để zoom, và dùng nút reset để về góc nhìn mặc định. Trên phone, stream LLM/tool được vẽ bằng SVG text thuần để hiển thị ổn định; nút **LLM / Tool / Curl details** mở panel native responsive cho payload dài và output node:

| Vùng | Màu | Node |
|------|-----|------|
| **OS Server** | Teal | Intent, Local, Cron, Gate |
| **HAL** | Amber | MIC, CAM, EMO, LED, SERVO, TTS |
| **Agentic Runtime** | Blue | Agent, TG In, Tool, Think, Response, TG Out |

### OS Server (hàng trên)

- **Cron** là stage **OS server** (lịch/timer thuộc OS server), **không** nằm trong cụm Agentic Runtime. Trên SVG, Cron cùng hàng với Intent/Local nhưng **`x` trùng cột Agent** để cạnh Cron→Agent là **đường dọc**.

### HAL

- **MIC** và **CAM** là input nodes (hàng trên HAL). Node hình thoi **CAM**
  phía dưới là node riêng cho agent gọi `GET /camera/snapshot`.
- Output nodes xếp dọc trong 1 cột:
  - **EMO** (`hw_emotion`) — `/emotion` (phối hợp LED + servo + display eyes)
  - **LED** (`hw_led`) — `/led/solid`, `/led/effect`, `/scene`, `/led/off`
  - **SERVO** (`hw_servo`) — di chuyển hoặc chạy animation servo:
    `/servo/aim`, `/servo/play`, `/servo/nudge`, `/servo/search`, `/servo/demo`.
    Chi tiết node hiển thị đúng lệnh/API call agent đã chạy trong turn đang
    chọn. Các lệnh đọc (`/servo/position`, `/servo/status`, `/servo/bearing`)
    không tính — một event `hw_servo` nghĩa là đèn đã làm gì đó mà người ta
    nhìn thấy được.

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
  - **CAM** (`hw_camera`) — `GET /camera/snapshot`; ảnh đã lưu mà tool
    trả về (kể cả file workspace như `cam_face3.jpg`) hiện thumbnail bấm để
    phóng to, giúp debug đúng frame agent nhận được, không chụp lại ảnh mới.
    Thumbnail cần CẢ arguments LẪN result của tool, mà hai thứ này về ở hai
    event khác nhau trên các runtime CLI (codex và họ hàng gửi `arguments` chỉ
    ở `start`, `result` chỉ ở `end`) — nên args được mang qua theo `toolCallId`,
    xem `rememberToolArgs` trong
    `system/server/agent/delivery/http/camera_snapshot.go`.
  - **TTS** (`tts_speak`) — `/voice/speak`, text-to-speech
- Đây là hardware calls trực tiếp từ OpenClaw tools, không qua OS server.
- Đường nối từ LOCAL → output nodes dùng **elbow routing** (gấp khúc bên trái) để tránh cắt qua node trung gian.

### Gate

- **Gate** nằm giữa OpenClaw output và HAL TTS. OS server listen WS events để phối hợp:
  - Tool có `/audio/play` → KHÔNG suppress TTS nữa; thứ tự TTS-rồi-nhạc do HAL xử lý (music_service chờ TTS xong mới chiếm loa)
  - Tool có `/led/*` → pause ambient breathing (không ghi đè màu agent set)
  - Assistant text accumulate → flush sang TTS khi lifecycle_end
  - Loa thiết bị đang mute → HAL trả HTTP 200 `{"status":"suppressed"}` cho call TTS, không phát gì — surface thành flow event `tts_muted` (xem mục *TTS muted event*)

### Agentic Runtime (lưới 3 cột)

- **Cột 1:** Tool + Response (Response dưới Tool).
- **Cột 2:** Agent + Thinking (Think dưới Agent).
- **Cột 3:** Telegram In.
- **Hàng 1:** Agent và TG In cùng hàng.
- **Hàng 2:** Thinking và Tool cùng hàng (Think → Tool).
- **Hàng 3:** Response dưới cột 1.

Bảng tọa độ gần đúng và ASCII grid: xem mục *Turn Pipeline* và *Approximate coordinates* trong `docs/flow-monitor.md`.

## File liên quan

| File | Vai trò |
|------|---------|
| `system/lib/flow/flow.go` | Emit flow, JSONL, API runID từng event |
| `system/server/sensing/delivery/http/handler.go` | Sensing → flow.Start/End |
| `system/server/openclaw/delivery/sse/handler.go` | Agent → flow.Log, map runID |
| `runtimes/openclaw/service.go` | sendChat / idempotencyKey |
| `system/web/src/pages/Monitor.tsx` | `groupIntoTurns`, `FlowDiagram`, v.v. |

**Tải để so sánh:** nút **↓ Bundle** trên Flow Panel tải cùng lúc JSONL tail server, snapshot UI và OpenClaw debug payload (xem bảng *Turns list vs downloaded log* trong `docs/flow-monitor.md`).

### Lấy tin nhắn user từ Telegram

OpenClaw chat stream **không bao giờ broadcast `role:"user"`** — chỉ emit `role:"assistant"`. Để lấy nội dung tin nhắn + tên người gửi, OS server gọi `chat.history` **WebSocket RPC** trên cùng WS connection đang dùng nhận events:

```
→  {"type":"req","id":"history-1","method":"chat.history",
    "params":{"sessionKey":"agent:main:telegram:group:...","limit":20}}

←  {"type":"res","id":"history-1","ok":true,
    "payload":{"messages":[
      {"role":"user","content":[{"type":"text","text":"dừng phát nhạc đi"}],
       "senderLabel":"Leo (158406741)"},
      ...
    ]}}
```

Chi tiết:
- **Async goroutine**: Fetch chạy trong goroutine riêng (gọi đồng bộ trong read loop sẽ deadlock).
- **Pending RPC tracking**: `pendingRPC` map match response về đúng caller qua request ID.
- **Hai phase emit**: `chat_input` đầu tiên fire ngay với placeholder trung tính `[chat]` (chưa có text). Goroutine lấy xong → fire `chat_input` thứ 2 với message + label chọn theo `senderLabel` / prefix message → UI pick event có content.
- **Frontend type upgrade**: emit đầu tiên pin `turn.type = "chat"` (từ summary `[chat]`). Khi emit thứ 2 tới, `groupIntoTurns` chạy lại `isTurnStart` để derive type cụ thể từ message prefix (`emotion.detected` / `speech_emotion.detected` / `voice` / `telegram` / …) và upgrade `turn.type` — **chỉ** khi đang còn ở placeholder `"chat"` (hoặc `"unknown"`), không đè type đã specific. Trước fix này, type bị kẹt ở `"chat"` (label CHAT, icon ❓) vì `refineTurnTypeFromSensingInputs` không nhận `"chat"` là channel type. Prefix `[speech_emotion]` map về `speech_emotion.detected` và được gom vào source `mic` (voice-driven), không phải `cam`, dù label có chữ "emotion". `voice_agent_handled` (turn giọng nói realtime đã xử lý, replay cho main agent) cũng thuộc source `mic` — trước đây nó không thuộc nhóm nào nên toggle Mic/Cam không giấu được nó, trông như "thuộc" bất kỳ nhóm nào còn đang bật.
- **Label routing (emit thứ 2)**: (1) `senderLabel` có → `[telegram:Gray]` (real channel user). (2) `senderLabel` rỗng + message khớp prefix device-internal → `[voice]` / `[emotion]` / `[speech_emotion]` / `[activity]` / `[wellbeing]` / `[music]` / `[sensing]` / `[system]` (sensing/voice event thiết bị đã post qua chat.send, OpenClaw merge vào UUID host turn này qua steer mode). (3) Còn lại → generic `[chat]`. Trước đây mọi UUID channel-turn đều bị gán nhãn theo configured channel (`[telegram]`), nhận nhầm steer-merged self-fire là Telegram.
- **Best-effort**: timeout 3 giây, fail thì giữ nguyên placeholder generic `[chat]` — tốt hơn là gán nhầm vào channel cụ thể.
- **Heartbeat**: Cron 30 phút cũng trigger `lifecycle_start` — last user message sẽ là system prompt, không phải user thật.
- **Token usage**: `chat.history` cũng được gọi lúc `lifecycle_end` để lấy token usage. OpenClaw `lifecycle_end` không có field `usage`. Token nằm trong last `role:"assistant"` message của history response: `usage: {input, output, totalTokens, cacheRead, cacheWrite}`. Emit thành `token_usage` flow event với `source: "chat_history"`. Chỉ xét assistant message MỚI NHẤT, và chỉ khi `timestamp` còn tươi (≤30s; retry 1 lần sau 2s) — fetch này đua với việc OpenClaw persist reply, và kiểu walk-back về assistant message cũ hơn từng gán nhầm usage (và thinking) của turn TRƯỚC cho các run tự nổ như heartbeat. Gate staleness tương tự (120s) áp cho fetch `chat.history` lúc lifecycle_start (gắn nhãn channel turn), nên heartbeat không còn nhân bản input text của turn trước. Run heartbeat (reply `HEARTBEAT_OK`) emit thêm flow event `heartbeat_run`; web Flow phân loại các turn đó là `heartbeat`. Footer token của turn card hiện `↓in ↑out R<cacheRead> Σtotal` — cache read là phần lớn context mỗi turn, và backend Autonomous tính cache read FULL giá, nên Σtotal (in + out + cache) chính là số billed (monitor không còn chỗ nào giảm 0.1×).

### CoT-leak filter (đường agent)

Một số model chạy sau openclaw/hermes (điển hình DeepSeek) xả nguyên đoạn suy luận tiếng Anh thành assistant text trước câu trả lời thật ("The `[emotion_context]` shows … Route = **music**. I need to log the signal … [nhẹ nhàng] Có vẻ hơi trầm …"). `server/agent/delivery/http/cot_leak_filter.go` — bản port Go của `drivers/voice/_internal/cot_leak_filter.py` bên HAL (bản Python chỉ chặn đường transcript Gemini Live) — cắt các câu đó trước khi text tới TTS, web chat (`full_text`) và channel fan-out (Telegram DM/broadcast, Slack reply cuối). Giữ nguyên 3 tier như bản Python (TRIGGER marker → bật CoT mode + drop; SECONDARY chỉ drop khi CoT mode đã bật; CoT-mode continuation drop câu tiếng Anh trên thiết bị non-English, draft trong ngoặc kép, mẩu plan cụt, câu trùng mờ), cộng thêm 1 TRIGGER riêng phía Go: identifier snake_case (`emotion_context`, `telegram_id`, …) — corpus DeepSeek mở đầu bằng kiểu này. Ngôn ngữ reply lấy từ `stt_language` trong `config.json`; chưa set → chế độ English (chỉ áp marker tier). Áp ở 3 điểm:

- **First-sentence stream** (`tryFirstSentenceFlush`): candidate được lọc với state mới mỗi lần thử; nếu toàn bộ là CoT thì hoãn flush (KHÔNG đánh dấu streamed offset) để câu sạch tới sau vẫn được stream sớm.
- **lifecycle:end**: full text lọc với state mới (nuôi `full_text`, DM/broadcast, Slack reply); phần remainder cho TTS lọc bằng instance thứ 2 được seed bằng prefix đã stream để CoT mode + bộ nhớ dedup nối liền qua ranh giới. Chỉ thay `text` khi có câu bị drop, turn sạch giữ nguyên whitespace gốc.
- **Channel-turn finalize** (đường `session.message`): channel turn không TTS, nhưng text vẫn lên web chat / Flow Monitor qua `chat_response` và `tts_suppressed` — lọc luôn ở đó.

Khi có câu bị drop, lifecycle:end emit event `cot_leak_filtered` (`data.dropped` = số câu, `data.preview` = preview giới hạn). Raw delta trong `agent_last_token` giữ nguyên không lọc để debug. Slack streaming giữa turn (`chat.appendStream`) không lọc (diff append-only không rút lại text được); reply Slack cuối thì có.

Với yêu cầu nhạc được delegate, `skills/music/SKILL.md` yêu cầu HW marker đứng đầu, sau đó đúng một câu xác nhận ngắn; việc chọn bài, diễn giải transcript nhiễu và xử lý danh tính chưa biết phải giữ nội bộ. `skills/input-branching/SKILL.md` cũng không cho đọc quyết định định tuyến. Cả bộ lọc Go và HAL nhận diện nhãn lập kế hoạch đầu câu `They named a/the song:` và ghi chú `Speaker [identity] is unknown`, kể cả ở chế độ English. Kiểm thử hồi quy dùng đầu ra “Eternal Flame” được báo cáo, kiểm tra cả stream câu đầu lẫn toàn bộ câu trả lời. Đây là lớp chặn có phạm vi cụ thể; muốn xác định model hay proxy đưa reasoning vào assistant text vẫn cần raw response từ runtime/provider.

### NO_REPLY suppression

OpenClaw agent trả `NO_REPLY` (hoặc dạng cắt ngắn `NO`, `NO_RE`, `NO_...`) khi quyết định không cần trả lời — thường cho passive sensing events (sound, motion). `isAgentNoReply()` trong `handler.go` suppress: không phát TTS, không hiện output. Match: `"NO"` chính xác, hoặc bắt đầu bằng `"NO_"` / `"NO_RE"` (case-insensitive).

**Chặn "kể lại việc im lặng".** Model đôi khi tả quyết định im lặng bằng văn xuôi thay vì trả sentinel (vd `Sound event, no user message. Nothing to say`). Đoạn đó lọt `isAgentNoReply()` nên `isMetaNonReply()` trong `handler_text.go` chặn thêm: text ≤ 100 byte, không có `?`, khớp một trong các cụm meta (`nothing to say/add/report`, `no (user) message/reply/response/comment (needed)`, `no need to reply/respond/speak/say`, `staying|remaining silent`, `no action needed`). Áp ở cuối lượt (`handler_event_agent.go`) và trong `tryFirstSentenceFlush()` (`handler_state.go`) — chỗ này defer mà KHÔNG đánh dấu đã stream, để câu thật sau đó vẫn giữ được lợi thế first-audio. Cả hai đều log `WARN` và lượt được báo là `no_reply`.

### Output text & first-sentence streaming

Web (chat + flow Output) đọc text reply từ event `tts_send`, ưu tiên `data.full_text` (toàn bộ reply) rồi fallback `data.text`. Khi câu đầu được stream sớm tới TTS giữa turn (event `tts_stream_send`, gửi trước để giảm latency), `data.text` chỉ chứa **phần còn lại** (câu 1 đã bị cắt để không phát 2 lần), còn `data.full_text` mới giữ câu 1 + phần còn lại. Web không đọc `tts_stream_send`, nên thiếu `full_text` thì câu 1 sẽ không hiện. `data.streamed_len` là byte offset nơi phần còn lại bắt đầu.

### TTS suppress event

Khi `SendToHalTTS` thật sự bị skip (loa không phát), OS server emit `tts_suppressed` thay vì `tts_send`. Field `data.reason` discriminate: `channel_run` (real Telegram user turn — detect qua runID có prefix `tg-` OS server tự sinh trong `session.message` handler, hoặc `channelRuns` map mark từ chat.history fallback; reply đi qua OpenClaw session fan-out thay vì loa thiết bị), `already_spoken` (built-in tts tool đã route trước), `voice_agent_handled` (realtime voice agent đã nói turn này), `web_chat` (chat gõ tay — reply chỉ hiện trong UI chat; turn MQTT `mqtt_chat` cũng dùng đúng reason `web_chat` này vì cả hai đều mark run qua `MarkWebChatRun`). UI hiển thị 🔇 ở Gate column thay vì 🔊 — tránh case trước đây log nói "TTS" nhưng loa im. Lưu ý: KHÔNG còn reason `music_playing` — phát nhạc không còn nuốt câu reply; OS server luôn gửi reply TTS và HAL serialize cho nói trước rồi mới phát nhạc. Classifier chỉ dùng positive evidence: UUID runs từ OpenClaw steer-mode self-fire, cron fire, heartbeat KHÔNG bị coi là `channel_run` và VẪN phát loa.

### TTS muted event

Node TTS tint đỏ (giống suppressed); detail panel hiện "🔇 speaker muted — reply not spoken", dòng gate "🔇 → TTS muted (speaker)". Emit NGAY SAU `tts_send` cùng run: reply đã gửi sang HAL, nhưng loa thiết bị đang mute nên HAL trả HTTP 200 `{"status":"suppressed"}` và KHÔNG synthesize/không phát gì (không tốn API call TTS). Go HAL client (`lib/hal` `SpeakReply`/`SpeakQueueReply`) decode body đó thành sentinel error `hal.ErrSpeakerMuted`, handler log `tts_muted` `{run_id, text}`. Khác `tts_suppressed` (quyết định phía Go TRƯỚC khi gửi, không có `tts_send` đi kèm — text chat lấy từ chính event suppress), text reply vẫn lên web chat qua `tts_send`; chỉ có tiếng là im.

### Cron-fire auto-force TTS

Khi OpenClaw emit `event:"cron"` với `action:"started"` (xem `src/cron/service/state.ts` của OpenClaw), OS server cache `sessionKey` → mark `lifecycle_start` kế tiếp trên session đó (trong vòng 10 s) là cron fire → `isChannelRun` bị override thành `false` để loa thiết bị tự nói mà không cần marker `[HW:/speak]`. Marker vẫn giữ trong skill làm defense-in-depth fallback nếu cron event bị drop (`dropIfSlow: true` ở phía OpenClaw).

### Pose bucket trên Turn card

Với `motion.activity` mà window pose vừa fire, turn card hiển thị:

```
IN   <input text>
[snapshot strip — tối đa 3 thumbnail: 1 motion + 2 worst pose]
[🪑 LOAD MORE · pose bucket <id> · N worst]
OUT  🔊 <output text>
```

- **Khung hình `look` đi kèm lượt thoại.** Khi tool `look` realtime chụp, HAL chép khung hình sang `/var/lib/hal/snapshots/sensing_look/` (`hal/realtime/look_monitor.py`, giữ 20 cái mới nhất) và gắn marker `[snapshot: ...]` vào chính message mà lượt đó đã gửi — nên tấm ảnh hiện **ngay trong lượt đã hỏi nó**, cạnh transcript và câu trả lời, thay vì là một event riêng không gắn với câu hỏi nào. os-server bóc marker trước khi text tới model nhưng vẫn giữ trong flow JSONL, giống hệt cách `motion.activity` làm. Khác với snapshot của `motion.activity`, **khung hình này CHÍNH LÀ thứ model đã thấy**, nên nó là vật chứng để đối chiếu với câu trả lời của model. (Nhánh `look.capture` chỉ-cho-monitor vẫn còn trong `handler.go` từ thiết kế trước; HAL không còn gửi nữa.)
- Strip được extract từ marker `[snapshot:]` + `[pose_bucket:]` / `[pose_worst:]` trong `sensing_input`. Click thumbnail mở lightbox inline (giống cũ).
- Nút **LOAD MORE** mở `PoseBucketModal` → fetch `/api/hardware/sensing/pose-bucket/<id>` (proxy về lelamp) → render bảng từng sample (monospace + cột joint giống Sensing tab). Row có filename trong `worst_snapshots` được highlight (viền đỏ + ⭐) để xem nhanh khung tệ nhất.
- Khi /dm fire, OS server tự đính các worst snapshot vào Telegram qua `sendMediaGroup` — caption nằm trên ảnh đầu tiên, agent không cần biết file path. Xem `robots/lamp/docs/sensing-behavior.md` mục "/dm auto-attach".

### Clip audio debug trên Turn card

Với `speech_emotion.detected`, turn card hiển thị một player `<audio controls>` click-to-play gắn nhãn `🎙 debug` cho mỗi audio URL, để nghe chính xác clip đã tạo ra emotion được detect.

- Đường dẫn clip trên Pi tới qua field `audio` (tùy chọn) trong body `POST /api/sensing/event`. `system/server/sensing/delivery/http/handler.go` chuyển basename của path thành URL servable (`audioURLForPath` → `/api/sensing/audio/<file>.wav`) và lưu vào `Detail` của monitor event ở key `audio` — **chỉ URL basename, không bao giờ là raw path**.
- Frontend `turnIO()` (`helpers.ts`) rút các URL này vào `audioUrls` từ `detail.audio` của event `sensing_input`; `TurnBadge.tsx` render player.
- **Đây là affordance CHỈ-ĐỂ-DEBUG — audio KHÔNG BAO GIỜ gửi cho LLM.** Path nằm trong field JSON riêng, không nằm trong text tin nhắn chat, nên tự nhiên bị loại khỏi những gì agent thấy — giống cách snapshot `motion.activity` được hiện trên UI nhưng strip trước khi tới LLM.
- **Route**: `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`) serve file `.wav` theo basename từ `/var/lib/hal/speech-emotion` hoặc `/tmp/hal-speech-emotion`, với validation basename nghiêm ngặt — tên phải kết thúc `.wav` và không chứa `/`, `\`, hay `..` (nếu không → `404`).

### Tool call display

- Chỉ hiện tool events phase `"start"` (có args). Phase `update`/`result` không có args nên bỏ qua.
- Hiện full curl command từ `args.command` (OpenClaw agent tự generate).
- Mỗi tool entry có nút 📋 copy riêng — click copy curl command.
- OpenClaw gửi tool name ở `data.name` (không phải `data.tool`), args là object `data.args` (e.g. `{"command":"curl ..."}`).

Chi tiết run ID, `runIDMap`, stitching turn, edge case: đọc bản tiếng Anh.

## Compaction summary inspector

Session agent auto-compact khi context vượt ~80k tokens. Mỗi lần compact ghi 1 record `type:"compaction"` vào `/root/.openclaw/agents/main/sessions/<sessionId>.jsonl`, chứa field `summary` dạng text — text này được **chèn đầu mỗi turn kế tiếp** cho đến lần compact sau. Rule bị copy/generalize nhầm vào summary có thể đè SKILL.md (summary nằm trước trong prompt, đóng vai trò "context đã chốt").

**UI:** header Flow Monitor có nút `📋 Summary`. Click → fetch + render modal show: `timestamp`, `tokensBefore`, `summaryChars`, `compactionCount`, `readFiles` (file nào được đọc vào compaction prompt), và toàn văn `summary`.

**Endpoint:** `GET /api/agent/compaction-latest?session=<key>` (mặc định `agent:main:main`). Response format: `{status:1, data:{found, sessionFile, timestamp, tokensBefore, summary, details:{readFiles}, ...}}`.

Dùng khi agent viện rule mà grep không thấy trong bất kỳ `skills/**/SKILL.md` — gần như 100% nguồn là compaction summary, không phải skill đang load. Handler: `system/server/openclaw/delivery/sse/handler_api_compaction.go`.

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
debug chỉ là toggle trên header, không phải URL param ẩn:

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
có UI). Chữ dùng là "entry removed", không phải "quarantined" — từ nội bộ này
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

## Issue đang mở

### OpenClaw built-in `tts` tool bypass speaker HAL (ĐÃ FIX)
Agent gọi `tts` built-in tool của OpenClaw thay vì trả assistant text. OpenClaw generate audio phía server (`"Generated audio reply."`) nhưng không route tới speaker HAL (`/voice/speak`). Agent trả `NO_REPLY` → OS server không có text → im lặng.
- **Nguyên nhân**: OpenClaw cung cấp `tts` tool khi `tools.profile = "full"`. Sensing SKILL.md hướng dẫn gọi `/voice/speak`, agent map nhầm sang built-in `tts` tool thay vì `curl` tới HAL.
- **Fix**: (1) Deny `tts` tool qua `tools.deny: ["tts"]` trong config (`service.go`). `tools.disabled` KHÔNG hợp lệ — dùng `tools.deny` (deny thắng `tools.profile`). (2) Intercept fallback trong handler.go: nếu agent vẫn gọi `tts` tool, extract text và route sang `SendToHalTTS()`. (3) Cập nhật sensing SKILL.md và SOUL.md — agent trả text bình thường, OS server pipeline tự TTS qua HAL.
- **Trạng thái**: Đã fix v0.0.138.

### OpenClaw không thấy `tool_call` dù có action
Đã gặp nhiều turn (nhất là Telegram): user yêu cầu action (ví dụ đổi màu đèn), kết quả OUT/TTS xác nhận đã đổi, nhưng flow/debug không có `tool_call`.

- **Ảnh hưởng**: node `TOOL` có thể không sáng dù nhìn như đã có action.
- **Trạng thái hiện tại**: đã bật raw dump full-stream (`source: "openclaw_raw"`), nhưng vẫn có run không thấy payload `stream:"tool"`.
- **Chưa chốt**: có thể OpenClaw chạy nhánh nội bộ không emit tool stream, hoặc action chỉ được suy ra từ assistant text mà không có tool invocation tường minh.

### Node HW sáng nhưng không phát — marker bị "echo" trong tool (2026-07-23)
Marker `[HW:...]` chỉ tới HAL khi agent xuất nó ra **text trả lời** (Go chạy `extractHWCalls` trên assistant message). Thỉnh thoảng agent lại bọc marker trong một lệnh shell — ví dụ `echo '[HW:/audio/play:{...}]'` — lệnh này chỉ in ra stdout trong sandbox, **không bao giờ fire**, nhưng vì tool args có chứa chuỗi marker nên node HW (ví dụ `hw_audio`) vẫn sáng: nhìn như đã phát mà loa im.

- **Ảnh hưởng**: "node sáng mà câm loa" — hay gặp nhất với nhạc (Music skill).
- **Fix**: `fireEchoedHWMarkers` (`handler_hw.go`, gọi từ cả nhánh `tool` device-chat trong `handler_event_agent.go` lẫn nhánh `session.tool`) phát hiện marker `[HW:...]` trong tool args rồi fire thật sang HAL, kèm log WARN. Nó chỉ khớp đúng ngữ pháp `[HW:/path:{json}]`, nên `curl .../audio/play` hợp lệ (không có marker) không bị đụng và vẫn phát qua request riêng. Khi rescue chạy thì bỏ qua phần emit node cosmetic để không nhân đôi node.
- **Chống tận gốc**: `skills/music/SKILL.md` đã cấm rõ echo/exec/bash bọc marker — marker phải là text trả lời, không "chạy" nó.

Kết quả cuối Harness được ghi vào flow JSONL bằng `harness_response`, giữ run ID thiết bị gốc và `text` đầy đủ. Web Chat dùng sự kiện này khôi phục kết quả đang chờ sau khi SSE ngắt hoặc tải lại trang. Luồng trực tiếp vẫn phát `chat_response` với state `final`.

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
