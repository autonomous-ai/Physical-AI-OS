# Hermes — backend agent

Jev preload kèm `lookup_name` có category và hướng dẫn dùng nguyên tên đó khi đọc reference (ví dụ `openclaw-imports/computer-use`). Skill bundled trùng tên ngắn vẫn có thể tồn tại; preload không xóa nó hay tự xử lý lời gọi tên ngắn mơ hồ thay model.

Hermes là một trong các **backend agent có thể hoán đổi** mà os-server chạy phía
sau agent gateway. Bộ não là pluggable (CLAUDE.md): os-server nói chuyện với bất
kỳ backend nào `config.agent_runtime` chọn, qua đúng một interface
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, sensing drain, Telegram fan-out) không cần biết não
nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ (Runs native khi được hỗ trợ; fallback OpenAI *Responses API*). Tài liệu này. Code: `runtimes/hermes/`.

> Nguồn sự thật là code. Tài liệu này mô tả `runtimes/hermes/` đúng như đã hiện
> thực; phải đồng bộ khi code đổi (EN: `docs/agentic/hermes.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · file này (Hermes) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) (PicoClaw). Cơ chế switch/install/migration
> generic nằm ở file đầu; protocol đặc thù từng backend nằm ở các file kia.

## 1. Được chọn khi nào và như thế nào

`agent_runtime` trong `config.json` chọn backend; resolve nằm ở
`system/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| không set | fallback về `gateway.default` trong `robots/<type>/ROBOT.md`, rồi OpenClaw nếu cái đó cũng trống |
| `"openclaw"` | OpenClaw (mặc định) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) — client WebSocket bền; giả định service PicoClaw đã chạy sẵn. Xem `docs/agentic/picoclaw.md` + `runtimes/picoclaw`. |
| giá trị khác | OpenClaw (log là `FALLBACK — unknown runtime=…`) |

Khi `agent_runtime` không được set trong `config.json`, backend lấy từ
`gateway.default` của thiết bị (`robots/<type>/ROBOT.md`); chỉ dùng OpenClaw nếu
giá trị đó cũng trống. Banner log thêm `source` để biết nguồn nào thắng.

Lúc khởi động, `ProvideGateway` in banner `AGENT BACKEND ACTIVE → HERMES` kèm
`base_url`, `conversation`, `model`, `api_key_set`. **Chưa có config theo từng
máy** cho các giá trị này — chúng là hằng số compile-time trong
`runtimes/hermes/constants.go`:

| Hằng số | Mặc định | Ý nghĩa |
|---|---|---|
| `BaseURL` | `http://127.0.0.1:8642` | Hermes API server cục bộ |
| `APIKey` | `hermes-api-key` | Bearer cho Hermes |
| `Conversation` | `device-main` | Kênh mà mọi lượt đổ vào |
| `Model` | `hermes-agent` | Model id gửi cho Hermes |

Giả định Hermes đã chạy sẵn trên thiết bị tại `BaseURL` với skills đã provision;
os-server chỉ là client theo từng request.

## 2. Khác gì OpenClaw — và giữ nguyên gì

| | OpenClaw | Hermes |
|---|---|---|
| Transport | một WebSocket bền | HTTP POST + SSE không trạng thái, mỗi lượt |
| Trạng thái kết nối | socket lên/xuống | goroutine poll `/health` (`health.go`) lái `ready`/`connectedAt` |
| Session | chính socket | UUID phía server qua header `X-Hermes-Session-Id` (§3) |
| Pipeline downstream | — | **giống hệt** — Hermes dịch SSE → cùng các frame `domain.WSEvent` |

Vì Hermes phát đúng shape `domain.WSEvent` mà handler OpenClaw
(`server/agent/delivery/http/handler_events.go`) đã tiêu thụ, nên HAL TTS, định
tuyến marker `[HW:/…]`, monitor SSE, sensing drain và Telegram fan-out đều giữ
nguyên. `*hermes.Service` thỏa mãn đầy đủ `domain.AgentGateway` (`Name()`="Hermes",
`IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`/`SetBusy`, `QueuePendingEvent`,
`SendChat*`, `StartWS`, …).

### Số liệu cache trong Flow Monitor

`input_tokens` của Hermes Responses gồm input chưa cache, cache read và cache
write. `translator.go` tách `input_tokens_details.cached_tokens` và
`input_tokens_details.cache_write_tokens` sang các nhóm riêng trong domain để
monitor hiện `R`/`W` mà không đếm trùng. Khi thiếu chi tiết, giữ nguyên tổng input
cũ; bỏ qua tổng cache không hợp lệ. Ví dụ 14.097 input, 13.824 cache read và 66
output trở thành 273 input chưa cache, `R13.8k` và 66 output (tổng 14.163).

Một số phiên bản Hermes cộng dồn cache nội bộ nhưng làm mất số liệu tại
`_finish_turn_result` và `_responses_usage_payload` trong
`gateway/platforms/api_server.py`. Onboarding local chạy `cache_usage_patch.py`
được embed bằng Python 3 để giữ `session_cache_read_tokens` và
`session_cache_write_tokens` trong Responses usage. Bản vá kiểm tra cấu trúc AST
đã biết, compile trước khi ghi atomic và không ghi lại file đã vá. Khi có thay
đổi, OS gộp vào quyết định restart gateway hiện có. Source không khớp chỉ ghi
cảnh báo, không sửa; bỏ qua cài đặt chưa tồn tại và endpoint remote. Người vận
hành Hermes remote cần áp dụng bản sửa tương đương trên server đó. Không sửa
dữ liệu lượt cũ; kiểm tra lượt mới sau restart. Không cần đổi frontend hay cấu
hình cache provider.

## 3. Mô hình session & conversation

Hermes không có socket, nên "session" nằm phía server:

- Mỗi response mang header `X-Hermes-Session-Id` — một UUID cho mỗi conversation,
  ổn định qua các lần reconnect. `Service.sessionUUID` lưu bóng của nó.
- `Conversation` (`device-main`) là kênh có tên mà mọi lượt đổ vào; tất cả lượt
  chat/sensing/Telegram dùng chung để agent giữ một context.
- `Service.lastResponseID` cache `response.id` mới nhất, dùng để nối lượt (kiểu
  continuation của Responses API).

Trạng thái chỉ in-memory (`sessionUUID`, `lastResponseID`, `reqCounter` + các run
tracker guard / broadcast / web_chat / pose-bucket); không gì tồn tại qua lần
restart os-server.

### Xoay conversation (`rotation.go`)

Gateway nhét **toàn bộ** lịch sử hội thoại vào một response blob **mỗi turn**, key
theo tên conversation. Vì vậy một tên cố định tích luỹ chuỗi vô hạn (đo được: blob
`device-main` **28 MB / ~6M token**, `response_store.db` **1.9 GB**), và gateway
phải dựng lại + nén lại nó mỗi turn — biến một turn đáng lẽ ~7 s thành **~50 s**.
Để chặn, os-server xoay tên conversation:

- `conversationName()` **boot-fresh**: seed một lần mỗi process thành
  `device-main-<bootUnix>`, nên restart os-server không bao giờ dính lại chuỗi cũ
  đã phình.
- `rotateConversation()` đổi sang `device-main-<bootUnix>-<seq>`, clear
  `lastResponseID`, để gateway bắt đầu chuỗi mới (nhỏ). Chuỗi cũ bị bỏ lại dưới tên
  cũ (prune riêng đòi lại đĩa).
- **Trigger:** handler lifecycle generic gọi `ShouldRotateSession(totalTokens,
  turnsSinceRotation)` mỗi turn (method của `domain.AgentGateway`). OpenClaw /
  PicoClaw xoay theo ngưỡng token thật (150k); **Hermes xoay theo số turn**
  (`rotateMaxTurns = 40`, hoặc spike `rotateTokenThreshold = 250_000`) vì token nó
  báo là sau-nén (~20–60 k), không phản ánh kích thước chuỗi thật — ngưỡng token là
  lưới an toàn, không phải cổng chính. `NewSession()` thực hiện việc xoay.
- **Lưới token là 50_000 cho tới 2026-09-09.** Nó nằm *trong* vùng vận hành bình
  thường: trên lamp-a0ae một hội thoại mới đã báo ~12,3 k, và một turn bình thường
  có đọc `SKILL.md` rồi chạy tool cộng thêm ~25 k (12,3 k → 41,3 k → 64,5 k →
  73,5 k), nên lưới nổ mỗi 2–3 turn. Vì đường đang nối là `maybeAutoNewSession`
  (compact bị tắt), mỗi lần nổ là **bỏ luôn lịch sử, không tóm tắt** — thiết bị
  quên mất thứ nó vừa nói. 250 k giữ được ~10 turn ở nhịp đó; lưới phải nằm **trên**
  mức mà cơ chế nén của gateway ổn định lại, không nằm trong đó (cùng giá trị và
  cùng lập luận với [`codex`](codex_vi.md)).

## 4. Giao thức request — Runs native và fallback Responses

Khi server công bố đủ `run_submission`, `run_events_sse`, `run_status`,
`run_stop`, `run_steer` qua `GET /v1/capabilities` và
`features.runs_idempotency.autonomous_run_events_v1=true`, lượt text đủ điều kiện dùng
`POST /v1/runs`. Body chứa `input`, `model`, `instructions` tùy chọn và
`session_id` hiện tại. Admission trả server `run_id`; nhận event qua
`GET /v1/runs/{run_id}/events`, đọc trạng thái qua `GET /v1/runs/{run_id}`.
Device run ID vẫn dùng để nối trace và metric. Khác với chuỗi conversation của
Responses, Runs nạp lịch sử session đã lưu theo `session_id`; server resolve
session tiếp nối sau compression khi mở run kế tiếp. Native run ID không phải
`previous_response_id` của Responses.

Server thiếu capability giữ transport Responses bên dưới. Sau khi xác minh native,
service giữ transport đó đến khi khởi động lại; lỗi dò capability sau đó không
được đưa hội thoại về lịch sử Responses cũ. Trên server hỗ trợ,
request có ảnh đợi run native riêng cùng session; adapter chuyển part
`input_text` / `input_image` của Responses thành `text` / `image_url` chuẩn mà
không bỏ ảnh. Không đưa request ảnh vào endpoint steer chỉ nhận text. Lỗi mạng sau admission
hoặc steer không cho phép gửi lại request: server có thể đã đang thực hiện.

### Fallback Responses — `POST /v1/responses`

`client.go` POST một `streamRequest` với `stream: true` rồi đọc luồng SSE:

```jsonc
{
  "model": "hermes-agent",
  "conversation": "device-main",
  "stream": true,
  "instructions": "…",        // text hệ thống/role, optional
  "input": "<text>",           // lượt text thường …
  "title": "…"                 // optional
}
```

Với lượt **vision**, `input` là mảng nhiều phần thay vì chuỗi — Hermes chấp nhận
cả hai dạng:

```jsonc
"input": [{ "role": "user", "content": [
  { "type": "input_text",  "text": "…" },
  { "type": "input_image", "image_url": "data:…" }
]}]
```

## 5. Dịch SSE → `domain.WSEvent`

Bộ tiêu thụ SSE (`client.go`) stream các event `response.*`; `translator.go` map
chúng thành frame `domain.WSEvent` và dispatch qua handler đăng ký bởi `StartWS`
— cùng đường OpenClaw dùng. Vòng đời lượt khớp OpenClaw: `activeTurn` bật true
khi gửi, false khi `response.completed`; kết quả completed mang `response.id`
(cache thành `lastResponseID`) và toàn bộ text assistant cho path send-and-wait.

Runs native đặt tên event trong JSON `data` (`event`), không dùng dòng SSE
`event:`. Adapter chuyển `message.delta` và terminal thật `run.completed` /
`run.failed` / `run.cancelled` / `run.interrupted` vào pipeline event hiện có.
Acknowledgment `run.steered` không phải task completion. Khi mất stream, có thể
khôi phục kết quả bằng trạng thái terminal đã xác nhận. Nếu chưa kết thúc,
adapter yêu cầu stop và poll cho tới khi execution chốt, giữ quyền sở hữu remote
run khi trạng thái chưa rõ. EOF hoặc trạng thái còn chạy không được báo thành công.

Run chưa chốt đánh dấu conversation của nó là *không chắc*: request nào đã xếp
hàng trên conversation đó bị từ chối ("unknown acceptance ... start a new
session") vì run bị mất có thể vẫn đang thực thi prompt ấy. Sau đó adapter **tự
xoay conversation** để request kế đi trên conversation mới — trước đây không có
gì xoay nó, và lamp-0c4e (16/9/2026) hỏng mọi lượt suốt 6+ phút sau khi presync
restart gateway, tới khi os-server được restart. **Lỗi dial** khi `POST /v1/runs`
(connection refused lúc gateway đang restart) *không* phải không chắc: prompt chưa
hề rời thiết bị, nên request đó lỗi nhưng conversation vẫn dùng được.

Native mode còn yêu cầu marker tương thích OS ở trên: Runs chưa vá thiếu
ID/kết quả tool và chi tiết cache mà handler hiện tại cần. Bản vá tương thích
thêm `tool.call.started` / `tool.call.completed` với ID, arguments, kết quả thật,
cùng `cache_read_tokens`, `cache_write_tokens` trong usage. Adapter dùng lại
translator Responses và chuyển cache vào `input_tokens_details`; bỏ progress
`tool.started` / `tool.completed` cũ để callback không chạy trùng. Khi thiếu bản
vá hoặc source không khớp cấu trúc đã xác minh, giữ Responses cho tới khi cài
bản vá hợp lệ và restart gateway.

Marker sensing/pose bị strip trước khi gửi bằng đúng các regex như OpenClaw
(`[snapshot: …]`, `[pose_bucket: …]`, `[pose_worst: …]`) để agent không bao giờ
thấy marker phần cứng nội bộ.

## 6. Trạng thái kết nối & health

Không socket nên liveness phải poll. `health.go` chạy poller `/health` lái
`ready`/`connectedAt`, suy ra `agentStartedAt` từ `/health/detailed.uptime_s` nếu
có, và dùng `hasConnected` để bỏ qua chime TTS "đã reconnect" ở lần poll thành
công đầu tiên. `AgentUptime()` báo uptime tiến trình Hermes, độc lập os-server.

## 7. Trạng thái busy & sensing event chờ

Hợp đồng giống hệt OpenClaw: khi một lượt đang active (`IsBusy`), các sensing
event thụ động bị drop hoặc buffer (`QueuePendingEvent`, last-write-wins theo
loại) và replay khi rảnh, để tín hiệu ambient không cắt ngang lệnh đang chạy.

Với run native được quản lý, text người dùng mới đủ điều kiện được gửi qua
`POST /v1/runs/{run_id}/steer` thay vì tạo agent chạy song song. Hermes chèn chỉ
dẫn vào ranh giới iteration/tool; nhận request không đồng nghĩa cắt ngay model
call hoặc tool đang chạy. `POST /v1/runs/{run_id}/stop` là thao tác cancel riêng;
acknowledgment `stopping` chưa phải terminal. Request không thể steer, gồm ảnh, đợi run native riêng thay vì bị bỏ hoặc
chuyển sang conversation Responses không nối được lịch sử.

Steer đã nhận nhưng đến quá muộn có thể được trả lại trong `pending_steer` của
terminal. Phải giữ phần này cho lượt tiếp theo, không tính là công việc run vừa
kết thúc đã hoàn thành. Hermes nối nhiều text steer còn chờ bằng newline.
Quyền sở hữu request của thiết bị và kết quả execution tách biệt với các
acknowledgment transport này.

Lượt thông thường vẫn phát tiếng theo tiến độ. Sau khi nhận steer, adapter giữ
text tiếp theo đến terminal để xác định input đã được xử lý; request được phát
tiếng mới nhất nhận text qua assistant buffer hiện có trước khi lifecycle end
đẩy TTS. Các request voice trước giữ im lặng. Hardware marker và token usage
của execution chung chỉ phát một lần.

Sau explicit stop đã xác nhận terminal, request user tiếp theo trong cùng hội
thoại mang thông tin rằng yêu cầu trước đã bị hủy. Hermes có thể gộp các dòng
user liên tiếp sau khi ngắt model call; thông tin này phân biệt việc đã hủy với
việc cần tiếp tục. Request passive không tiêu thụ thông tin đó và hội thoại mới
không kế thừa nó.

**Merge-drain (chỉ Hermes).** Backend OpenClaw có steer mode
(`messages.queue.mode=steer`) gộp các message đồng thời vào lượt đang chạy tại
model boundary kế tiếp. Hermes nay dùng native steer cho text người dùng đủ
điều kiện khi server hỗ trợ; sensing thụ động vẫn gộp ở phía client: khi
`drainPendingEvents` chạy, các event sống sót được phân nhóm bởi
`standaloneDrain`. Event standalone giữ lượt riêng — lệnh voice thật
(`voice` / `voice_command`, trả lời trực tiếp), web chat (`web_chat`), `voice_agent_handled` (reply câm),
và event có ảnh. Phần sensing ambient thuần còn lại (presence / motion / emotion /
speech_emotion) được gộp thành **một lượt** qua `sendMergedPending` (một `runID`,
các dòng nối dưới `mergedSensingHeader`), nên prompt floor mỗi lượt chỉ trả một
lần thay vì mỗi event một lần. Cách này lấy lại phần lớn lợi ích cost của steer
nhưng không lấy được tính tức thì: batch chỉ bắn sau khi lượt hiện tại kết thúc,
không bao giờ giữa lượt. Bật/tắt bằng const `mergeDrainEnabled` (đặt `false` để
quay về replay mỗi event một lượt). Xem `runtimes/hermes/events.go`.

Event chưa gửi vẫn nằm trong hàng đợi khi Hermes mất kết nối. Khi health chuyển
sang sẵn sàng, drain tiếp qua cùng speaker gate; mỗi yêu cầu standalone kết thúc
rồi mới gửi yêu cầu tiếp theo. Chỉ giữ lại lỗi not-ready đồng bộ trước khi gửi.
Không replay HTTP POST đã thử gửi, vì thao tác desktop có thể đã thực hiện.

Mỗi HTTP/SSE stream đang chạy giữ trạng thái busy riêng. Một stream kết thúc
hoặc lỗi không được xóa busy của stream khác, kể cả khi quá busy TTL cũ.
Đọc SSE dừng tại `response.completed` hoặc `response.failed`; EOF trước terminal
phát lifecycle error thay vì báo thành công. Device run ID theo từng request đã
tách các phản hồi SSE, nên không sao chép giao thức request-ID WebSocket hay
heuristic output lặp dành riêng cho CLI Codex. Các bảo đảm này đã được kiểm tra
bằng test transport local; chưa chứng minh Hermes chạy desktop hoặc voice thực tế.

## 8. Channel (Telegram/Slack/Discord) — hiển thị inbound + fan-out

hermes gateway **sở hữu I/O của Telegram/Discord/WhatsApp**: nó tự poll các nền
tảng đó bằng token mà `presync` sync vào `~/.hermes/.env`, chạy turn, rồi reply
thẳng về chat. (**Slack là ngoại lệ** trên fleet này — app chạy ở chế độ
HTTP/Events, không phải Socket Mode, nên os-server bridge nó; xem mục Slack dưới
đây.) os-server không nằm trên đường channel của gateway, nên — khác OpenClaw (đẩy
WS event `session.message`) — một lượt channel do gateway xử lý sẽ KHÔNG hiện trong
Flow Monitor. Gateway cũng không có broadcast turn cross-platform để subscribe;
seam duy nhất là hệ thống **hook** của nó.

Vì vậy os-server cài một hook cho gateway, `os-server-observer`
(`runtimes/hermes/hooks/os-server-observer/{HOOK.yaml,handler.py}`, được
`ensureObserverHook` materialize vào `~/.hermes/hooks/` mỗi lần boot — xem §10).
Hook fire ở `agent:start` / `agent:end` cho **mọi** platform và POST lượt đó tới
endpoint loopback `POST /api/agent/channel-turn` (`handler_channel_turn.go`),
nơi emit đúng các flow event như một turn bình thường:

- `agent:start` → `chat_input` (source `channel`, kèm `sender` + `channel`) cùng
  `lifecycle_start`. Đồng thời bắn mặt **emotion-acknowledge** "thinking"
  (`FireChannelStartEmotion` → `fireAckEmotion`) cho các lượt Telegram/Discord do
  gateway sở hữu — chính là ack mà `sendChat` đã cấp cho lượt os-server trung gian,
  còn lượt kênh native thì không bao giờ đi qua `sendChat`. Slack/web (`api_server`)
  bị bỏ ở đây (xem dưới) nên chỉ nhận ack một lần từ `sendChat`, không double-fire.
- `agent:end` → `lifecycle_end` cùng `tts_suppressed` mang text phản hồi **đã loại
  bỏ marker** (reply đi về channel chứ không ra loa thiết bị — cùng node mà đường
  channel của OpenClaw dùng, để web turn render được), hoặc `no_reply` cho lượt rỗng
  / `NO_REPLY`, hoặc `hw_only_reply` khi lượt chỉ có marker mà không có text nói. Ở
  event này handler còn chạy `extractHWCalls` trên reply và bắn mọi marker `[HW:/…]`
  xuống **thiết bị cục bộ** (LED/emotion/servo/audio) qua `fireHWCalls`, để một lượt
  channel do gateway sở hữu (Telegram/Discord) điều khiển được phần cứng — khớp với
  đường `session.message` của OpenClaw và đường `/v1/responses` của thiết bị. (Slack
  không bị ảnh hưởng: lượt của nó chạy qua `/v1/responses` và bị skip ở đây dưới
  dạng `api_server`, nên `handler_event_agent` đã bắn marker của nó rồi.) **Lưu ý:**
  gateway có thể cắt `response` (~500 ký tự), nên marker nằm cuối một reply dài có
  thể bị cụt và mất — cần chỉnh truncation phía gateway nếu thực tế marker cuối reply
  bị thiếu.

Hai event dùng chung một `run_id`, tương quan qua `session_id`. Handler
channel-agnostic (dựa vào field `platform`) và **skip** lượt `api_server` / `cli`
— đó là các call `/v1/responses` của chính os-server, đã được `sendChat` log rồi;
emit lại sẽ nhân đôi các turn khởi từ thiết bị. (Các lượt qua bridge Slack dưới đây
được lái qua `/v1/responses`, nên đã được `sendChat` log và cũng bị hook skip — không
đếm trùng.)

Gửi outbound (chủ động) — `Broadcast` / `SendToUser` trong `telegram.go` /
`telegram_sender.go` — đi thẳng tới Telegram Bot API cho các cảnh báo do thiết bị
khởi tạo, dùng bot token và danh sách chat trong `telegramTargetsFile`.

### Slack — bridge HTTP-mode (cho runtime chỉ-Socket-Mode)

`domain.SlackBridge` (`system/domain/slack_bridge.go`) là một **cơ chế
generic**, không riêng cho hermes: nó là interface cho **bất kỳ** runtime nào mà
hỗ trợ Slack native **chỉ là Socket Mode** (hiện tại: hermes là ví dụ duy nhất) và
do đó **không có webhook HTTP Slack local** để nhận event. Với một runtime như
vậy, chính os-server trở thành **Slack frontend kiểu HTTP-mode** — nó parse event,
chạy lượt, và post phản hồi qua Bot API. OpenClaw và picoclaw tự phục vụ webhook
HTTP Slack của chúng (webhook local `127.0.0.1:18789/slack/events` của OpenClaw),
nên chúng **không** implement `SlackBridge` và giữ nguyên path POST webhook local
hiện có.

**Yêu cầu Slack app.** Slack app phải **bật "Agents & AI Apps"** cùng các scope
**`assistant:write`** (trạng thái typing của assistant), **`chat:write`** (stream +
post), và **`im:history`** (đọc DM). Thiếu `assistant:write` thì trạng thái typing
bị bỏ qua âm thầm (best-effort) nhưng văn bản vẫn stream qua `chat:write`.

**Inbound** — Slack → proxy bff-campaign-service → MQTT `slack_event` → device.
`server/device/delivery/mqtt/slack_event_handler.go` (`forwardSlackHTTP`)
type-assert gateway đang hoạt động sang `domain.SlackBridge`. Khi khớp (hermes) nó
gọi `HandleInboundSlack`; khi không khớp (openclaw, picoclaw) nó giữ nguyên path
POST webhook local hiện có.

`runtimes/hermes/slack.go` `HandleInboundSlack` / `parseSlackInbound` decode JSON
Slack Events (challenge `url_verification` — phòng thủ, proxy public mới là nơi
thực sự sở hữu kiểm tra Request URL của Slack; `event_callback` với `event.type`
`message`/`app_mention`). Nó bỏ qua tin nhắn bot (`bot_id`), event `subtype`
(sửa/join), và event `user` rỗng (chống loop); áp cổng allowed-user qua
`config.SlackUserID` (rỗng = mở); strip mention `<@Uxxx>` ở đầu; và bắt `channel`,
`thread_ts`, `ts` của tin nhắn user, cùng `team_id`. Với một tin nhắn user thật, nó:

1. ghi origin Slack (channel + `thread_ts` + `ts` của tin nhắn user) vào map
   `slackRunOrigin`;
2. đặt trạng thái assistant thành **"...is typing"** qua
   **`assistant.threads.setStatus`** (`setSlackAssistantStatus`, best-effort, async
   — cần `assistant:write`);
3. đăng ký một session stream **lazy** (`startSlackStreamSession`) — chưa call
   Slack, chỉ là một goroutine theo-run sẵn sàng stream;
4. gửi lượt qua `SendChatMessageWithRun`;
5. thêm một reaction 👀 (`eyes`) vào tin nhắn của user (`setSlackReaction`, hằng số
   `slackAckReaction = "eyes"`, async).

**Streaming.** Phản hồi được render bằng API streaming native của Slack — chỉ báo
"…is typing" thật cùng văn bản chảy dần vào. Trong lượt, handler SSE của agent
(`server/agent/delivery/http/handler_event_agent.go`) feed văn bản phản hồi **đã
được dọn (cleaned) tích lũy** (`cleanedSlackStreamText` trong `handler_state.go`,
hàm này strip các HW marker và hoãn khi gặp marker `[HW:` còn dở / bất kỳ wrapper
`<say>` nào / `NO_REPLY` / `HEARTBEAT_OK`) vào `StreamSlackDelta` ở mỗi delta. Một
goroutine theo-run riêng (`slack_stream.go`) mở stream **lazy** ngay khi có nội
dung đầu tiên qua **`chat.startStream`** — gieo (seed) chính văn bản đầu đó dưới
dạng một chunk `markdown_text`, nên bubble **không bao giờ rỗng** — rồi append phần
đuôi mới qua **`chat.appendStream`** (các chunk `markdown_text`), throttle ~650 ms
(flush đầu tiên là tức thì qua một kick). Nó chỉ append phần đuôi mới (chưa-append),
theo thứ tự, nên vòng lặp delta SSE không bao giờ block vào một call HTTP Slack.
`chat.startStream` nhận `channel`, `thread_ts` (bắt buộc — trả lời trong thread
đang có, nếu không thì thread dưới tin nhắn của user), và `recipient_team_id` (bắt
buộc với channel, lấy từ `team_id` của event).

**Finalize phản hồi** — `handler_event_agent.go` gọi `DeliverSlackReply(runID,
text)` (`runtimes/hermes/slack.go`) cho runID đã hoàn tất. Hàm này tiêu thụ origin,
**xóa trạng thái assistant** (`setSlackAssistantStatus` với `""`), gỡ reaction 👀,
rồi `finishSlackStream` làm một **flush cuối + `chat.stopStream`** (việc này cũng
xóa chỉ báo typing và đánh dấu tin nhắn hoàn tất). Khi stream chưa từng mở (không
có nội dung tới nó, hoặc `startStream` cứ thất bại), nó fallback về một
`chat.postMessage` đơn (`PostSlackReply`). Các call Web API đều đi qua helper
generic `slackAPI` trong `runtimes/hermes/slack_sender.go`.

**Chặn TTS (cả hai nửa của lượt).** Một lượt xuất phát từ Slack không bao giờ tới
loa thiết bị; việc chặn được áp tại **hai** điểm qua peek **không tiêu thụ**
`IsSlackOriginRun(runID)` (để cả hai chạy trước khi `DeliverSlackReply` tiêu thụ
origin lúc reply): stream câu đầu giữa lượt (`canStreamSentenceTTS` trong
`server/agent/delivery/http/handler_text.go`) **và** phần còn lại cuối
(`isChannelRun`, đặt từ `isSlackRun`, trong `handler_event_agent.go`). Phản hồi đi
tới Slack, không tới loa.

**Các method Bot API dùng.** `chat.startStream` / `chat.appendStream` /
`chat.stopStream` (phản hồi streaming), `assistant.threads.setStatus` (trạng thái
typing), `reactions.add` / `reactions.remove` (ack 👀), `chat.postMessage`
(fallback + proactive).

**Outbound / proactive** — một `SlackSender` (`domain.ChannelSender`, trong
`slack_sender.go`) post tin nhắn sensing/broadcast tới `config.SlackUserID` qua
`chat.postMessage`; nó được nối vào danh sách `channels` của hermes trong
`runtimes/hermes/service.go` cùng với `TelegramSender`.

**`.env`** — `SLACK_BOT_TOKEN` (đồng bộ từ `config.json` bởi hook presync) là cái
bridge dùng cho mọi call Bot API. `SLACK_APP_TOKEN` không liên quan tới bridge HTTP
(nó chỉ dùng cho Socket Mode native) nhưng vô hại.

**Giới hạn phạm vi v1.** Bridge bỏ qua re-verify chữ ký request của Slack (path
qua MQTT broker đã được device xác thực và proxy đã verify chữ ký rồi) và hoãn
slash command (`slack_command`). Bản thân phản hồi chỉ là văn bản và ảnh đính kèm
bị bỏ trên path proactive.

## 9. Voice

`hal.go` nối lượt Hermes vào path voice của HAL (TTS lúc speak-end, cùng entry
point `lib/hal` mà OpenClaw dùng), nên tương tác bằng giọng hoạt động như nhau
bất kể backend.

### Câu đệm khi chờ công cụ

`system/lib/i18n/fillers.go` ánh xạ tên công cụ Hermes sang các câu đệm ngắn theo
hoạt động. Phạm vi bám theo [tài liệu công cụ chính thức](https://hermes-agent.nousresearch.com/docs/reference/tools-reference)
(đối chiếu ngày 2026-09-11); `system/lib/i18n/fillers_test.go` giữ snapshot danh
sách công cụ và kiểm tra độ phủ tiếng Anh, tiếng Việt, tiếng Trung giản thể và
phồn thể. Các nhóm được ánh xạ gồm:

- Công cụ lõi terminal/process, đọc và sửa tệp/skill, tìm kiếm/trích xuất web,
  tra cứu bộ nhớ/phiên, giao việc, lập kế hoạch/cron và xử lý nội dung đa phương tiện.
  `terminal` dùng câu đệm thực thi, `search_files` dùng câu tra cứu trung tính,
  còn `skill_view` dùng câu đọc tài liệu. Ghi bộ nhớ và tạo giọng nói có pool
  riêng là `memory_store` và `audio_generate`.
- Các công cụ tùy chọn browser/CDP, desktop/preview, project/kanban, Home Assistant,
  Feishu, Discord, Spotify và Yuanbao, cùng tên tương thích Honcho.
  Công cụ có nhiều loại thao tác dùng câu kiểm tra trung tính vì chỉ tên công cụ
  chưa xác định được thao tác cụ thể hay kết quả thành công.

Tên đã biết cũng được nhận diện trong dạng `mcp__<server>__<tool>`. Công cụ chưa
biết vẫn dùng câu đệm tiếp diễn chung; ánh xạ này không cài đặt hoặc bật công cụ
tùy chọn nào. HAL làm nóng cache cho các pool tra cứu, ghi bộ nhớ và tạo giọng
nói mới cùng các pool hiện có.

Chỉ lượt được đánh dấu là lượt giọng nói mới đủ điều kiện. Bộ lập lịch dùng độ trễ
**1,5 giây** và cooldown **2,5 giây** khi lập lịch lại tại sự kiện công cụ, với
giới hạn **6 câu đệm mỗi lượt, tính cả lời xác nhận mở đầu** (tối đa 5 câu được
lập lịch sau đó). Văn bản assistant tạm dừng các câu đệm đang chờ; `tool.start`
tiếp theo cho phép lập lịch lại nhưng không đặt lại bộ đếm hoặc cooldown.
Kết thúc/lỗi/hủy lượt hoặc người dùng ngắt lời sẽ dừng hẳn câu đệm của lượt đó.
Khi gửi câu trả lời đầu tiên để phát giọng nói hoặc chặn công cụ TTS để xử lý,
câu đệm cũng dừng hẳn để tránh nói chồng lên âm thanh trả lời.
Phản ứng phần cứng cũng chặn câu đệm ở gần thời điểm phản ứng. Việc lập lịch bám
theo sự kiện vòng đời và công cụ, không bảo đảm nói định kỳ suốt một khoảng chờ dài.
Các nhãn Flow Monitor như `agent:first_token` và “OS Server waiting next event”
là sự kiện pipeline/khoảng chờ, không phải tên công cụ cần pool riêng.

## 10. Vận hành

Hermes được cài bởi `runtimes/hermes/install.sh` (đặt cạnh phần hiện
thực của nó). Script này được **embed trong os-server** (`go:embed`, đăng ký qua
`lib/runtimereg`), nên đi kèm + OTA chung với binary; os-server ghi nó ra
`/usr/local/lib/os-runtimes/hermes/install.sh` và switch-runtime chạy bản local
đó — hoàn toàn offline, không cần CDN. (Đường CDN
`${RUNTIMES_BASE_URL}/hermes/install.sh` vẫn là fallback cho backend không
compile vào binary.) Installer **cài Hermes CLI theo từng stage** (tải installer
upstream về một file tạm rồi chạy `bash <installer> --stage <name>
--non-interactive` cho lần lượt `prerequisites repository venv python-deps path
config`), **cố tình bỏ qua stage `node-deps`**: stage đó chạy `npm install` các
native module của browser-tool (node-gyp) bị treo vô hạn trên board ARM, mà một
voice lamp không bao giờ dùng browser tools (gateway thuần Python nên không cần).
Vì vậy nó **không** còn chạy bản monolithic `curl | bash --skip-setup`. Sau vòng
lặp stage, installer ghi `git` vào `/usr/local/lib/hermes-agent/.install_method`
để một lệnh `hermes update` về sau nhận ra đây là git install.

> **Hermes được OTA như các CLI khác, pin theo commit.** `hermes update` không
> nhận version đích (luôn lên upstream HEAD), nên entry metadata mang đúng commit
> upstream: `make upload-hermes 0.21.1 v2026.9.7` resolve tag theo ngày ra commit,
> `make promote-hermes` nâng sàn, rồi bootstrap worker chạy `software-update
> hermes` để checkout đúng commit đó qua installer upstream (`--commit
> --force-commit`, như imager). Nút trên card Versions của web hiện khi bootstrap
> báo có entry — tức entry có `commit` và updater trên máy là bản biết pin. Entry
> chưa pin thì vẫn chỉ SSH (`hermes update` lên HEAD, lệch thì cảnh báo). Xem
> `docs/vi/bootstrap-ota.md` §5. Tiếp đó installer
dừng `openclaw` (để import skills không tranh chấp state đang chạy của nó), seed
các key `API_SERVER_*` trong `~/.hermes/.env`, rồi **giao toàn bộ phần config.yaml
+ skills cho hook presync** (gọi inline), và cuối cùng cài + start gateway như một
**system service** qua `hermes gateway install --system --run-as-user root` +
`hermes gateway start --system` (unit: **`hermes-gateway.service`**). Vì presync
lo phần config + skills (xem dưới) và installer chạy nó inline, một lệnh
`bash install.sh` trực tiếp đã được cấu hình đầy đủ và chạy.

Installer tee toàn bộ stdout+stderr vào `$HERMES_LOG`, mặc định là
**`/root/.hermes/install.log`** (rootfs bền), **không** phải `/var/log/hermes/…`:
trên các board này `/var/log` là một mount zram (log2ram) dễ bay khi reboot →
sẽ mất log install đúng lúc cần. Theo dõi trực tiếp bằng
`tail -f /root/.hermes/install.log`, hoặc override đường dẫn qua env
`HERMES_LOG=…` trước khi gọi.

> Tên unit: gateway chạy dưới `hermes-gateway.service`. Installer khai báo tên
> này trong `/usr/local/lib/os-runtimes/hermes/service` để `switch-runtime`
> enable đúng unit (§11); `reset_hermes.go` nhắm tới cùng unit đó.

### Unit gateway được tự-vá (pre-bake trong image + backstop runtime)

`IsReady()` và cổng gác của device-setup (`WaitForAgentReady`,
`system/device/service.go`) đều chờ HTTP `/health` của gateway
(`127.0.0.1:8642`). Việc đó cần **unit `hermes-gateway.service` tồn tại** — chỉ có
binary `hermes` trên `PATH` (`hermes --version` chạy được) là **chưa đủ**. Unit
bình thường do `install.sh` tạo ở lần switch hermes đầu, nhưng device có thể tới
hermes mà *không* đi qua đường đó — ví dụ operator sửa tay `agent_runtime` trong
`config.json` thành `hermes` sau factory reset. Không có unit thì gateway không bao
giờ chạy, `WaitForAgentReady` time-out, `SetUpCompleted` vẫn `false`, device về AP
mode, và triệu chứng nhìn giống "**WiFi không kết nối được**" dù WiFi thực ra đã
associate thành công. Hai lớp khắc phục:

- **A — pre-bake trong image** (`scripts/imager/build-orangepi.sh`, `scripts/imager/build.sh`): ngay sau khi pre-bake
  binary Hermes CLI, image chạy `hermes gateway install --system` để ghi file unit,
  rồi `systemctl disable hermes-gateway` để nó **không** auto-start lúc boot
  (OpenClaw là runtime active mặc định; enable cả hai sẽ chạy 2 agent). Best-effort
  — chroot lúc build không có systemd đang chạy, nên nếu CLI không tạo được unit ở
  đó thì lớp B cài lúc runtime.
- **B — backstop runtime** (`ensureGatewayUnit`, `runtimes/hermes/gateway.go`, gọi
  từ `EnsureOnboarding`): khi unit vắng (`systemctl cat hermes-gateway` fail), nó
  chạy `hermes gateway install --system` theo nhu cầu và khai lại file
  `service`/`verify` cho switch-runtime. Nhanh — binary + venv đã pre-bake, nên chỉ
  ghi unit (không git clone / `uv sync`). `EnsureOnboarding` sau đó **`systemctl
  enable`** unit (factory reset disable nó — `reset_hermes.go` bước 4, "SetupAgent
  re-enables" — và unit vừa cài cũng chưa enable cho boot) và (re)start nó khi config
  đổi, khi unit vừa được cài, **hoặc** khi unit có nhưng không active (crash /
  disabled).

### Hook presync làm chủ `config.yaml` + skills

Model config trong `config.yaml` và skills openclaw-imported do **hook presync**
(`runtimes/hermes/presync.sh`) làm chủ, **không** phải `install.sh`. **os-server
materialize hook ra `/usr/local/bin/runtime-hermes-presync` mỗi switch**
(`materializePresync`, đăng ký qua `runtimereg.RegisterPresync`), nên OTA os-server
thường cũng refresh nó trên disk — khác với bản `install.sh` ghi một lần mà
`switch-runtime` skip ở switch sau (*activation gap*; xem
`docs/vi/agentic/adding-agent-runtime_vi.md` §3).

**Hook cũng chạy mỗi lần os-server boot VÀ lúc setup ban đầu**, không chỉ khi switch
— đều qua `EnsureOnboarding` (`runtimes/hermes/onboarding.go`), chạy `PresyncScript`
embed và restart `hermes-gateway` **chỉ khi** config thật sự đổi (guard content-hash
— không restart loop). Hash phát hiện thay đổi bao trùm **cả** `config.yaml` **và**
`.env` (`hermesEnvFile` = `/root/.hermes/.env`), nên một thay đổi chỉ-token-kênh (chỉ
đụng `.env`, vd thêm Slack live) cũng restart gateway — để Hermes server nhận kênh
mới:

- **Boot:** startup-sequence gọi `EnsureOnboarding`. Khắc ngách: device **boot thẳng
  vào Hermes** (`ROBOT.md gateway.default: hermes`, hoặc imager pre-install) chưa
  từng switch từ OpenClaw, hoặc `llm_*` đổi khi Hermes đang chạy, sẽ giữ `config.yaml`
  cũ không lấy `llm_api_key`/`base_url` thật từ `config.json`.
- **Setup:** `SetupAgent` (cũng trong `onboarding.go`) chỉ gọi `EnsureOnboarding`.
  Được vì **Hermes provision từ `config.json`, không từ `SetupRequest`** (khác
  OpenClaw, `SetupAgent` của nó viết `openclaw.json` thẳng từ request — nên OpenClaw
  cần *hai* hàm riêng, Hermes *một*). Device setup flow lưu `config.json` **trước**
  khi gọi `SetupAgent` (`system/device/setup.go` — call được cố ý đặt sau
  `config.Save()`), nên presync materialize `config.yaml`/`.env` từ key vừa nhập ngay
  lập tức thay vì chờ boot kế.

Cho Hermes khả năng self-heal config giống OpenClaw (`ensureAgentDefaults` +
`StartModelSync`), tái dùng đúng script presync thay vì viết lại sync trong Go. (Xoay
`llm_*` live qua `PUT /api/device/config` không reboot thì vẫn chờ boot kế — trigger
theo config-change là follow-up khả dĩ.)

Hook chạy ngay trước khi gateway start (lúc switch và boot, và inline lúc install),
làm 3 việc theo thứ tự:

1. **Restore skills** — khi `~/.hermes/skills/openclaw-imports` rỗng (cài đầu HOẶC
   sau factory-reset wipe), chạy `hermes claw migrate` (nó **copy** skills openclaw,
   không transform). Guard theo thư mục rỗng để switch thường là no-op (không
   re-import churn). `claw migrate` cũng đụng SOUL/MEMORY, nhưng vô hại: migrate
   persona Go (§12) chạy sau ghi đè sạch, và `EnsureOnboarding` dựng lại block
   persona trong `SOUL.md` cùng block luật trong `AGENTS.md` ngay sau presync
   (xem dưới) — chỉ skills trụ lại.

   Một migrate chạy khi bản canonical đã có trên đĩa (race lúc install với skill
   watcher, chạy tay) dùng `--skill-conflict rename` và để lại **bản trùng**
   `<name>-imported` — hai ứng viên cho một tên làm `skill_view` của Hermes từ
   chối load skill luôn ("Ambiguous skill name"), agent sẽ tự bịa mà không có
   skill. Vì vậy `EnsureOnboarding` prune chúng mỗi boot
   (`pruneImportedSkillDuplicates`): bản `-imported` bị xoá khi `<name>` tồn tại
   (bản CDN là canonical), hoặc rename thành `<name>` khi nó là bản duy nhất — và
   gateway được restart khi có thay đổi (skill index của session được build lúc
   gateway start).
2. **Đảm bảo structure `config.yaml`** (idempotent — tự lành sau khi
   `hermes setup --reset` xoá trắng). Coerce `model: ''` bị reset về map, rồi khẳng định:
   - `.model.provider = custom:autonomous`
   - `.model.default = "Auto-AI"` — alias model của campaign-api, proxy đó tự phân
     giải sang model nào tuỳ nó. Chỉ giữ nguyên **khi máy còn dùng proxy đó**;
     `llm_model` lúc ấy có thể đang là primary model của OpenClaw, vô nghĩa ở đây.

     Với `llm_base_url` khác, alias này là một model id không tồn tại, và phần
     DYNAMIC bên dưới thay nó bằng `llm_model` của người dùng. Đo trên
     intern-v2-d16f trỏ vào openrouter: mọi lượt đều trả
     `400 Auto-AI is not a valid model ID`, và vì presync tự chữa mỗi lần boot
     nên sửa tay `config.yaml` không sống qua nổi một lần khởi động lại.

     _(ghi chú cũ)_ os-server
     gửi model request cố định (`constants.go` `Model`) mỗi lượt, nên **không** lấy
     từ `llm_model` (đó là model chính của OpenClaw, không liên quan Hermes).
   - `.custom_providers[0]` → `name: autonomous`, `key_env: AUTONOMOUS_API_KEY`,
     `api_mode: anthropic_messages`, `base_url` (mặc định campaign-api, override dưới).
   - `.custom_providers[0].models.Auto-AI.prompt_caching = true` — **chỉ khi máy
     đang ở proxy campaign-api** (cùng điều kiện `llm_base_url` với alias ở trên).
     Hermes chỉ gắn breakpoint `cache_control` kiểu Anthropic cho custom provider
     khi model khai capability này; thiếu nó thì toàn bộ floor ~18k token (system
     prompt + 25 tool schema) bị gửi lại không cache mỗi lượt. Đo trên lamp-0c4e
     (16/9/2026), cùng câu "hello" trong một session: không marker 18.3s → 12.6s,
     `cache_read` 0; có marker 13.8s → 9.2s ổn định, `cache_read` ~85%. Brain tự
     mang (BYO) giữ nguyên chính sách cache riêng theo provider của Hermes. Lưu ý
   - `.prompt_caching.cache_ttl = "1h"` — cùng điều kiện chỉ-khi-ở-proxy. Người
     dùng đèn thường nói một câu rồi im 10-20 phút, vượt quá mặc định `5m` nên lượt
     kế phải trả tiền prefill đầy đủ (đo 16/9/2026: 3.6s khi cache còn ấm so với
     11.8s sau 8 phút nghỉ). Hermes chỉ nhận `5m` | `1h` và gửi `ttl: "1h"` trên
     mọi cache marker; gateway bỏ qua trường này thì cache âm thầm giữ 5m, nên
     thiết lập vô hại ở nơi chưa hỗ trợ. Mức 1h tính 2x giá input khi ghi cache
     (5m là 1.25x); đọc đều 0.1x.
   - `.auxiliary.vision` (**ghi đè trọn node**) → `provider: custom:autonomous`,
     `model: qwen/qwen3.6-plus`, `timeout: 120`, `download_timeout: 30`, `extra_body: {}`
     — model hiểu ảnh, định tuyến qua cùng custom provider autonomous.
   - `.agent.image_input_mode = "auto"` — để agent tự quyết khi nào đính kèm ảnh.
   - `.terminal.cwd = /root/.hermes` (mục "1b2. TERMINAL CWD" trong script) — đây là
     thứ làm `~/.hermes/AGENTS.md` **đọc được**: Hermes dò project-context file theo
     cwd *được cấu hình*, và `terminal.cwd: .` mặc định để lookup đó rỗng. Tiến trình
     vốn đã chạy ở thư mục này (`WorkingDirectory=/root/.hermes` trong unit), nên ghim
     đường dẫn tuyệt đối không đổi cwd shell của agent. Chi tiết ở mục block prompt
     phía dưới.
   - `.approvals.mode = "off"` — tắt hoàn toàn prompt duyệt lệnh của Hermes (card
     tirith / dangerous-command). Thiết bị chạy không giám sát trên kênh voice +
     chat, nơi card duyệt lệnh là ngõ cụt làm kẹt lượt (quyết định sản phẩm).
     Hardline blocklist của Hermes vẫn áp dụng. Ghi ép-nháy (`style="double"`):
     yq v4 (YAML 1.2) xuất `off` trần, nhưng Hermes parse config.yaml bằng PyYAML
     (YAML 1.1) — `off` trần là boolean `False`, mode không bao giờ khớp và prompt
     âm thầm vẫn bật.
   - Chỉ ghi `.auxiliary.vision`, `.agent.image_input_mode`, `.approvals.mode` và
     `.terminal.cwd`; **các key khác dưới `.auxiliary`/`.agent`/`.approvals`/`.terminal`
     được giữ nguyên** (ba node đầu được coerce từ scalar bị reset về map trước, giống
     `.model`).
3. **Sync giá trị theo máy** từ `config.json` (chỉ field khác rỗng, nên kênh chưa
   cấu hình giữ nguyên):

| `config.json` | → | Hermes |
|---|---|---|
| `llm_base_url` | → | `config.yaml` `.custom_providers[0].base_url` |
| `llm_api_key` | → | `.env` `AUTONOMOUS_API_KEY` |
| `telegram_bot_token` | → | `.env` `TELEGRAM_BOT_TOKEN` |
| `telegram_user_id` | → | `.env` `TELEGRAM_ALLOWED_USERS` |
| `slack_bot_token` / `slack_app_token` / `slack_user_id` | → | `.env` `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_ALLOWED_USERS` |
| `discord_bot_token` / `discord_guild_id` / `discord_user_id` | → | `.env` `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` / `DISCORD_ALLOWED_USERS` |
| `whatsapp_user_id` | → | `.env` `WHATSAPP_ALLOWED_USERS` |

`.env` `API_SERVER_KEY` phải bằng `constants.go` `APIKey` (`hermes-api-key`) nếu
không mọi lượt sẽ 401. Hermes phải listen tại `127.0.0.1:8642` để khớp `BaseURL`.

Để trỏ tới Hermes endpoint / key / model khác ở hiện tại, sửa
`runtimes/hermes/constants.go` rồi build lại (việc cho phép cấu hình theo từng máy
là phần làm sau).

### Hai file prompt do OS quản lý (persona trong `SOUL.md`, luật trong `AGENTS.md`)

Hermes đọc **hai** file prompt do OS quản lý: `~/.hermes/SOUL.md` giữ **persona**
của thiết bị, còn `~/.hermes/AGENTS.md` giữ **block luật của OS** — đúng cái slot
openclaw/picoclaw/codex/opencode vẫn dùng, nên bộ luật là một bản chung cho mọi
runtime thay vì một bản riêng cho Hermes. `EnsureOnboarding`
(`runtimes/hermes/onboarding.go`) dựng lại cả hai mỗi lần boot:

| Việc | File | Marker | Hàm |
|---|---|---|---|
| Persona thiết bị | `~/.hermes/SOUL.md` (**đầu file**) | `<!-- OS DO NOT REMOVE -->` (`soulOSMarker`) | `ensureSoulMDBlock()` → `upsertSoulPersonaBlock()` |
| Bộ luật OS | `~/.hermes/AGENTS.md` (**đầu file**) | cũng `soulOSMarker` | `ensureAgentsMDBlock()` → `upsertAgentsMDBlock()` (hằng `agentsMDBlock`) |
| Dọn bản luật cũ còn sót trong SOUL.md | `~/.hermes/SOUL.md` | `<!-- OS HERMES SKILL PRIORITY -->` (`soulSkillPriorityMarker`) **hoặc** `soulOSMarker` có sentinel | `pruneSoulOSRuleBlock()` → `stripSoulOSRuleBlock()` |

Cả ba chạy **sau** presync (§0 của presync có thể chạy `claw migrate` ghi đè soul),
đúng thứ tự trên: persona → luật → dọn bản cũ. Cả ba đều best-effort (lỗi chỉ
`slog.Warn`, không chặn boot) và **nằm ngoài quyết định restart gateway**: hai file
này được đọc theo từng session, không đọc lúc gateway start (cùng quy tắc mà
`UpdateIdentityName` dựa vào). Cả ba ghi qua `writeManagedFile` — atomic tmp+rename
dùng chung, tên cũ là `writeSoulFile`, đổi vì giờ nó ghi cả hai file — nên một crash
giữa chừng không cắt cụt file nào.

**Persona thiết bị.** `ensureSoulMDBlock()` resolve persona của máy từ
`soul_ref` trong `robots/<type>/ROBOT.md` qua `device.ResolveSoul` (xem mục kế) và ghi
nó thành block ở **đầu** `~/.hermes/SOUL.md` mỗi lần boot, nên một OTA đổi nội
dung persona là làm mới luôn (block cũ bị gỡ ở bất cứ chỗ nào nó đang nằm thay vì
chồng thêm bản thứ hai). Trước đây
Hermes **không hề làm việc này** — nó chỉ nhận persona qua migration persona giữa
runtime (§12), nên một máy boot thẳng vào Hermes (mặc định của lamp) hoàn toàn
**không có persona**, kéo theo mất luôn các quy tắc định tuyến biến `[sensing:*]`
thành lời gọi skill. Block có shape byte-for-byte giống cái openclaw/picoclaw ghi
(cùng `<!-- OS DO NOT REMOVE -->` … `---`), nên switch runtime theo chiều nào cũng
mang persona qua nguyên vẹn. Máy **không khai `soul_ref`** thì không ghi gì —
giữ nguyên soul mặc định Hermes ship (hoặc soul đã migrate).

Nội dung của chủ máy nằm dưới block được giữ lại, trừ đúng một ngoại lệ dùng chung
với các runtime khác: một **soul mặc định do OS/runtime quản lý** còn sót lại ở đó
bị **bỏ đi** thay vì giữ như chỉnh sửa của chủ máy — nếu không file sẽ mọc thêm một
persona thứ hai cạnh tranh. `isManagedDefaultSoul` (tương ứng với
`isDefaultSoulHeading` bên openclaw/picoclaw) khớp theo danh sách prefix trong
`managedDefaultSoulPrefixes`. Phần chủ máy tự viết dưới `## Personal` vẫn sống sót.

**Gateway Hermes tự seed lại persona mặc định của nó mỗi khi SOUL.md biến mất**, và
presync chạy **trước** `ensureSoulMDBlock` — nên trên một máy vừa flash, cái mà hàm
upsert persona nhìn thấy nằm dưới block nó vừa ghi chính là seed đó. Đây là dạng
default duy nhất mở đầu bằng **văn xuôi, không có heading**
(`You are Hermes Agent, built by Nous Research…`), và đó là lý do
`managedDefaultSoulPrefixes` là danh sách prefix chứ không phải phép kiểm heading
như các runtime khác. Để nguyên thì nó mâu thuẫn thẳng với persona thiết bị — cùng
một file, trên bảo agent nó là Lamp, vài dòng dưới bảo nó là Hermes Agent. Hai mục
còn lại là `hermesSoulFallback` (`# Hermes Agent Persona`, do factory reset ghi trên
máy chưa khai `soul_ref`) và hai dạng `# Soul` / `# SOUL.md` đi vào Hermes qua
migration.

Máy cài lần đầu được seed sẵn mục `## Personal` — **đúng từng chữ** như
openclaw/picoclaw/codex/opencode ghi — để chủ máy có chỗ viết mà OTA không ghi đè.

**Block luật trong `AGENTS.md` (skill của máy thắng skill bundled của Hermes).**

Với yêu cầu connector cần xử lý, `skills/connectors/SKILL.md` yêu cầu chạy
Discover trong một lần gọi terminal ngay sau khi đọc skill, trước các lần đọc
skill phụ như `input-branching` cho đầu vào giọng nói thông thường. Kiểm tra
thành công nhưng không có connector phù hợp thì kết thúc tác vụ dịch vụ đó bằng
câu trả lời ngắn; config không đọc được hoặc sai định dạng là lỗi xác minh,
không phải bằng chứng chưa kết nối. Tag chỉ ghi lịch sử vẫn được ưu tiên xử lý.
Đây là hướng dẫn skill, không phải cam kết độ trễ từ runtime.

Hermes có catalog skill bundled riêng, và nếu để mặc định nó coi các skill đó ngang
hàng với skill nền tảng của máy — nên request nào cả hai catalog cùng làm được có thể
bị route sang skill bundled thay vì skill của máy. Ví dụ: được nhờ "gửi email", nó có
thể chọn skill email bundled rồi bắt đầu cài CLI (himalaya) trong khi skill
`connectors` đã có sẵn credential Gmail của máy trên đĩa. `ensureAgentsMDBlock()`
strip bản cũ theo marker rồi ghi lại hằng `agentsMDBlock` nhúng sẵn ở **đầu**
`~/.hermes/AGENTS.md`; nội dung chủ máy viết dưới dấu `---` đóng block được giữ
nguyên. Nhờ marker mà OTA os-server làm mới được nội dung, và một `claw migrate` ghi
đè SOUL.md (presync §0) cũng tự-vá ở boot kế tiếp. Trong `AGENTS.md` mọi block mang
marker đều là block của OS, nên `upsertAgentsMDBlock` strip thẳng, không cần predicate.

Block này mang **trọn bộ 8 luật OS mà mọi runtime đều có** — đúng bộ mà
openclaw/picoclaw/codex/opencode nhận qua block `AGENTS.md` và claudecode qua
`CLAUDE.md` (trước đây Hermes chỉ có 3): ưu tiên skill
(`skills/openclaw-imports/` thắng mọi skill bundled của Hermes có mục đích trùng lặp;
dịch vụ bên-thứ-ba đi qua `connectors`; không cài client/CLI thay thế cho dịch vụ
connector đã cover), kỷ luật `memories/USER.md`, **skill scope** với protocol chọn
`SKILL.md` 4 nhánh (tag `[skills: a, b, c]` là whitelist **có thẩm quyền** — chỉ đọc
đúng những `SKILL.md` đó, không quét thêm "cho chắc"; không có tag mà là hành động /
hành vi phần cứng / workflow chuyên biệt thì chọn đúng một skill cụ thể nhất; nhiều
ứng viên thì lấy cái cụ thể nhất; không khớp rõ thì không đọc file nào và trả lời
bình thường — chat/Q&A thường không cần đọc `SKILL.md` nào cả), thứ tự
`Skills > memory > history`, ghi memory ngay trong lượt phát sinh (giữ cô đọng —
`MEMORY.md` nằm trong mọi prompt và không có gì xoay vòng nó, khác với `memory/*.md`
theo ngày của openclaw), `[user]` được trả lời trước
`[sensing:*]`/`[ambient]`/`[emotion]`, lệnh version-check, và `NO_REPLY` làm token
im lặng.

**Một luật bị bỏ có chủ ý:** luật `hooks/` của openclaw, mô tả trigger
`handler.ts` chạy khi `message:preprocessed`. Hermes không nạp những hook đó —
ack cảm xúc "thinking" của nó là hook phía os-server
(`runtimes/hermes/emotion_ack.go`), và `~/.hermes/hooks/` chỉ chứa
`os-server-observer`. Ship luật đó là mô tả một cơ chế không chạy. Đường dẫn cũng
được điều chỉnh: Hermes không có `KNOWLEDGE.md` lẫn `memory/*.md` theo ngày, cả
hai gộp vào `memories/MEMORY.md`.

**Vì sao `AGENTS.md` giờ tới được prompt — và trước đây thì không.** Hermes dò các
file project-context (`AGENTS.md`, `.cursorrules`) bằng cách đi ngược lên từ cwd
**được cấu hình**: `resolve_context_cwd()` trong `agent/runtime_cwd.py` của Hermes
trả `None` chứ **không** fallback về thư mục launch — chốt chặn có chủ ý, để một agent
tự spawn bên trong cây nguồn Hermes không nuốt `AGENTS.md` của chính repo đó. Mà
Hermes ship `terminal.cwd: .` — đường dẫn tương đối, không bao giờ được bridge sang
`TERMINAL_CWD` — nên lookup đó rỗng và bất kỳ `AGENTS.md` nào đặt ở đấy cũng vô hình.
Vì vậy `runtimes/hermes/presync.sh` giờ **ghim `terminal.cwd` về đúng thư mục nhà của
Hermes** bằng yq (mục "1b2. TERMINAL CWD"). Tiến trình **vốn đã chạy ở đó**
(`WorkingDirectory=/root/.hermes` trong unit do chính Hermes cài), nên thiết lập này
chỉ nói ra một sự thật sẵn có; cwd shell của agent không dịch chuyển.

Đã kiểm trên thiết bị (lamp-0c89): với `terminal.cwd: .`, một codeword cắm vào
`/root/.hermes/AGENTS.md` **không** xuất hiện trong prompt (agent trả lời nó không có
codeword nào); đổi sang đường dẫn tuyệt đối thì agent đọc ra đúng codeword. Sau khi
block thật lên máy, agent đọc vanh vách cả ba lệnh version-check — một luật chỉ có
trong `AGENTS.md` (0 lần xuất hiện trong `SOUL.md`).

**Dọn bản luật cũ trong `SOUL.md`.** Trước khi dời sang `AGENTS.md`, bộ luật này nằm
ngay trong `SOUL.md`, nên máy cập nhật từ os-server đời trước vẫn mang nó theo — hai
bản luật ở hai file prompt vừa tốn token vừa là mâu thuẫn chờ sẵn khi chỉ một bản
được cập nhật. `pruneSoulOSRuleBlock()` gỡ nó khỏi `SOUL.md` ở **cả hai dạng từng
ship**: bọc trong `soulSkillPriorityMarker` (`<!-- OS HERMES SKILL PRIORITY -->`),
hoặc bọc trong `soulOSMarker` dùng chung và khớp `soulSkillPrioritySentinel`
(`**Skill priority (MANDATORY):**`, dòng đầu của thân block) — sentinel là thứ bảo
đảm một **persona** đeo cùng marker không bao giờ bị đụng tới. `stripSoulOSRuleBlock()`
là helper thuần làm đúng hai bước đó.

Cơ chế nền là `stripSoulMarkedBlock(text, marker, match)` với predicate theo nội dung
block: chỉ block mà predicate chấp nhận mới bị gỡ, block bị từ chối giữ nguyên
verbatim (`isPersonaBody` / `isSkillPriorityBody`). Đó cũng là thứ cho phép persona và
block luật từng sống chung một file mà mỗi hàm chỉ đụng phần của mình — trước khi có
predicate, cả hai dùng chung `soulOSMarker` và hàm ghi block luật strip block **xuất
hiện trước tiên**, mà trên máy đã migrate persona thì cái đứng trước chính là persona,
nên persona bị xoá âm thầm ở lần boot kế (GitHub issue #403).

Nội dung của chủ máy nằm ngoài các block do OS ghi — persona tự viết thêm dưới
`## Personal`, identity card inline trong `SOUL.md`, ghi chú riêng dưới block trong
`AGENTS.md` — không bị đụng.

### `device.ResolveSoul` — resolver `soul_ref` dùng chung

`system/device/soul.go` `ResolveSoul(deviceType) (content, hasSoul, err)` là nửa
**runtime-agnostic** của hợp đồng `soul_ref`, để không runtime nào phải chép lại
phần tra cứu (Hermes ship thiếu chính chỗ này và máy của nó boot lên không có
persona):

- không khai `soul_ref` → `hasSoul=false`, **không phải lỗi** — thân máy "không
  hồn" giữ nguyên soul mặc định của runtime;
- `http(s)://` → tải artifact về, timeout **30 s** (`soulDownloadTimeout`) vì
  onboarding nằm trên đường boot, host artifact treo không được giữ gateway lại;
- giá trị khác → đọc file theo đường dẫn tương đối dưới `DevicesDir()/<type>/`
  (scheme lạ như `ftp://`, `s3://` bị từ chối thẳng thay vì bị hiểu nhầm là path);
- khai `soul_ref` nhưng resolve không được → **trả lỗi**, vì đó là lỗi deploy
  (thân máy gọi tên một nhân vật mà không ship nó) đáng phải lộ ra.

Hermes dùng nó ở **cả hai** đường: `ensureSoulMDBlock()` lúc onboarding, và
`resolveSoulContent` (`runtimes/hermes/reset.go`) lúc factory reset seed lại
SOUL.md. Khác biệt là cách xử lý lỗi: reset **ghi cảnh báo rồi fallback về**
`hermesSoulFallback` thay vì fail — một máy đang wipe phải quay lại với soul parse
được, và onboarding sẽ tiêm lại bản thật ở boot kế tiếp.

### Capability kênh & add/refresh live

Hermes là một **channel runtime hạng nhất** trong luồng capability generic
(`runtimes/hermes/channels.go`). Hermes Agent giao **telegram / slack / discord**
natively ngay trong server của nó — một kênh được bật khi token của nó có mặt trong
`~/.hermes/.env` (Slack dùng Socket Mode → `SLACK_APP_TOKEN`), mà bảng map `.env` ở
§10 phía trên đã đổ từ `config.json`. os-server **không chạy receive loop kênh** của
riêng mình; việc duy nhất của nó là đặt creds vào `.env` rồi bounce gateway.

- **`SupportedChannels()`** trả `[telegram, slack, discord]`. **WhatsApp KHÔNG được
  hỗ trợ trên Hermes** (pairing Baileys chỉ có ở OpenClaw) → `AddChannel` /
  `RefreshChannelConfig` cho `whatsapp` trả `domain.ErrChannelNotSupported` (gate
  capability qua `domain.ChannelSupported`).
- **`AddChannel` và `RefreshChannelConfig` không còn là no-op.** Trước kia là stub
  `return nil` im lặng; giờ chúng re-sync `~/.hermes/.env` từ `config.json` bằng cách
  tái dùng primitive presync và restart `hermes-gateway` **chỉ khi config đổi**. Cơ
  chế: `syncChannelsEnv()` → `EnsureOnboarding()` → `runPresync()` (upsert các biến
  kênh trong `.env`) → hash-diff `config.yaml`+`.env` → `restartHermesGateway()`. Cả
  hai đều quy về "re-sync `.env` + restart-if-changed" nên dùng chung một code path.
- **Persist-then-apply.** Lớp device (`system/device/channels.go` `AddChannel`)
  gate capability trước, rồi persist creds kênh vào `config.json` **trước khi** gọi
  `AddChannel` của gateway, để presync đọc lại `config.json` và thấy token mới. Một
  apply fail tạm thời để creds đã persist (chiều phục hồi được — presync lúc boot /
  `ChannelReconcile` re-apply lại).
- **Switch runtime vs add live.** Khi **switch vào Hermes**, hook presync đã chạy
  trước khi gateway start nên slack/discord tự mang qua; code mới khắc ngách
  add/refresh **live** (thêm kênh khi đang chạy trên Hermes). `ChannelReconcile` lúc
  khởi động (`system/agent/channel_reconcile.go`) cũng re-apply kênh sau switch,
  nhưng với Hermes nó thực chất là **no-op** — presync đã sync `.env` rồi nên hash-diff
  không thấy đổi và bỏ qua restart. Nó cũng ghi WhatsApp là không hỗ trợ
  (`ChannelsUnsupported`) cho info uplink, để creds đó lại cho lần switch về OpenClaw.

### MCP connectors (`mcp_servers` trong `config.yaml`)

Các remote-MCP connector (Notion, Linear, Asana, GitHub, Ahrefs, …) được nối bởi
luồng MQTT `connector.set` của backend là công dân hạng nhất trên Hermes:
`WriteMCPEntry`/`RemoveMCPEntry` (`runtimes/hermes/mcp.go`) upsert/xoá
`mcp_servers.<name>` trong `~/.hermes/config.yaml` và restart `hermes-gateway`,
mirror `runtimes/openclaw/mcp.go` (cái này sửa `mcp.servers` trong `openclaw.json`).

Connector writer giao cho gateway một entry chuẩn, shape kiểu OpenClaw —
`{type:"http", url, headers}` cho hosted MCP, hoặc `{command, args, env}` cho stdio.
`toHermesMCPEntry` dịch nó sang schema `mcp_servers` của Hermes: Hermes suy ra
transport từ việc có `url` hay `command`, nên discriminator `type` chỉ-OpenClaw bị
bỏ và `enabled: true` được khẳng định. Hook presync chỉ sửa
`.model`/`.custom_providers`/`.env` (qua `yq`) và để nguyên `mcp_servers`, nên hai
chủ sở hữu của `config.yaml` không đụng nhau; read-modify-write được tuần tự hoá
dưới `HermesService.mcpMu`. Như với OpenClaw, `config.yaml` phải đã tồn tại
(connector được cấu hình sau onboarding) — một `hermes setup --reset` xoá sạch
`mcp_servers` cùng với phần còn lại, và `connector.set` kế tiếp đẩy lại.

**Clone khi switch runtime.** `MCPReconcile` (`system/agent/mcp_reconcile.go`)
mirror `ChannelReconcile`: gate bởi `config.MCPAppliedRuntime`, nó fire một lần
trong startup-sequence khi quan sát thấy switch, đọc MCP entry của runtime **trước
đó** thẳng từ config trên-disk của nó (`mcp.servers` trong `openclaw.json` ↔
`mcp_servers` trong `config.yaml`, normalize mỗi cái về shape chuẩn), và đẩy lại
chúng qua `WriteMCPEntry` của gateway giờ-đang-hoạt-động. Mỗi entry tự đủ (header
auth mang token inline), nên clone là phép copy config→config thuần — không có cơ
chế token-file/refresh. Một lỗi clone để marker chưa-tiến nên boot kế thử lại (không
chiều switch nào xoá config của runtime kia).

## 11. Switch backend lúc runtime

Cơ chế switch là **generic** (không biết backend cụ thể) và được mô tả đầy đủ ở
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §2–§3: ba trigger (MQTT
`hermes.setup`, HTTP `POST /api/device/agent-runtime {"runtime":"hermes"}`, web
Settings → *Runtime*) dồn vào `device.Service.UpdateAgentRuntime`, chạy
`switch-runtime <new> <old>` dưới `systemd-run --wait` và **chỉ ghi
`config.agent_runtime` sau khi exit 0 sạch** (nên crash giữa chừng resolve về
backend cũ vẫn đang cài). Các điểm đặc thù Hermes mà switcher generic dựa vào:

- **Tên unit** `hermes-gateway.service` (không phải `hermes.service`) — khai trong
  `/usr/local/lib/os-runtimes/hermes/service` để `switch-runtime` enable đúng unit;
  `reset_hermes.go` nhắm cùng unit.
- **Verify hook** `/usr/local/lib/os-runtimes/hermes/verify` chạy `command -v
  hermes` (check CLI rẻ). Cố tình **không** check structure config — config tự lành
  qua presync (§10), nên verify fail sẽ ép full reinstall vô ích.
- **Presync** `runtime-hermes-presync` chạy trước khi gateway start (§10).
- Ack MQTT `hermes.setup` phản ánh **kết quả thật** (success chỉ sau khi switch
  thành công; ngược lại failure kèm lý do rollback), vì `UpdateAgentRuntime` block
  chờ exit code của switcher.

Xác nhận đã switch qua banner `AGENT BACKEND ACTIVE → HERMES` + một lần poll
`/health` khỏe.

## 12. Persona, memory & skills mang qua khi switch

Switch openclaw→hermes chạy một migration persona Go
(`system/agent/migrate_persona/openclaw_to_hermes.go`) lúc os-server boot —
**tách biệt với `claw migrate`**. Migration này **không còn là nguồn persona duy
nhất** của Hermes: `ensureSoulMDBlock()` tiêm persona của máy từ `soul_ref` mỗi
lần boot (§10), nên máy boot thẳng vào Hermes cũng có persona mà không cần đi qua
switch. Migration mang vào `~/.hermes/`:

- **SOUL.md** (rebrand) — và vì Hermes không có slot IDENTITY.md riêng, inline các
  field IDENTITY đã điền của owner thành block `## Your identity card` để tên tùy
  chỉnh (vd "Ngân") sống sót. `UpdateIdentityName` (đổi tên thiết bị) sửa block đó;
  `WatchIdentity` (`runtimes/hermes/identity.go`) poll SOUL.md và khi tên đổi thì
  đẩy wake words mới sang HAL + `i18n.SetDeviceName` — mirror `WatchIdentity` của
  OpenClaw, chỉ khác là watch SOUL.md thay vì IDENTITY.md. Block persona
  `<!-- OS DO NOT REMOVE -->` của OpenClaw đi theo nguyên vẹn vì hai runtime ghi
  cùng một shape; **bộ luật OS không đi qua đường này** — nó nằm trong
  `~/.hermes/AGENTS.md` và được `EnsureOnboarding` (chạy sau migration trong startup
  sequence) dựng lại, cùng lượt với việc gỡ bản luật cũ còn sót trong `SOUL.md`
  (xem *Hai file prompt do OS quản lý* phía trên).
- **MEMORY.md + daily `memory/*.md` + KNOWLEDGE.md** → merge vào `memories/MEMORY.md`.
  Hermes chỉ load `MEMORY.md` + `USER.md` **theo tên** (không glob `memories/*.md`),
  nên KNOWLEDGE được fold vào thay vì giữ thành file riêng bị bỏ qua.
- **USER.md** → `memories/USER.md`.

Parser tên và thao tác đổi tên chỉ nhận dòng trường `**Name:**` riêng, có thể
có bullet Markdown (`-` hoặc `*`) ở đầu. Cụm nhắc inline như
“Do NOT fill `**Name:**` or the other single-value fields” trong hướng dẫn SOUL
được bỏ qua, không trở thành wake word hay bị ghi đè khi đổi tên thiết bị.

Copy soul dùng `Overwrite=true` (switch lấy persona của runtime nguồn; backup
trước). Chiều ngược hermes→openclaw **strip identity card khỏi SOUL VÀ restore các
field của nó về `IDENTITY.md` của OpenClaw** (`restoreIdentityCard`, nghịch đảo của
inline) — nên tên đặt dưới Hermes sống sót cả chiều về, không chỉ chiều đi. Khi
đọc SOUL.md của Hermes, adapter còn gỡ luôn block luật legacy mang marker
`<!-- OS HERMES SKILL PRIORITY -->` (`stripHermesOSBlock` trong
`system/agent/migrate_persona/runtime_hermes.go`): đó là chỉ dẫn riêng của Hermes chứ
không phải persona, runtime đích tự tiêm bộ luật của nó, và marker Hermes-only ấy
không bị phép strip theo `<!-- OS DO NOT REMOVE -->` của runtime đích dọn giúp.
**Skills** được giữ tươi dưới Hermes qua hai đường bổ sung nhau:
`EnsureOnboarding` luôn gate theo capability và đồng bộ toàn bộ catalog được hỗ
trợ từ CDN vào `skills/openclaw-imports` (sửa cả file local cũ khi OTA đã publish
trước lúc watcher khởi động), còn `skill_watcher.go` poll metadata OTA mỗi năm phút
để bắt các bản publish sau đó. Cả hai dùng engine chung
`system/skills/skillzip.go`; khi nội dung thật sự đổi, gateway được restart rồi
agent nhận thông báo đọc lại các skill đã đổi. Download hoặc extract ZIP lỗi không
làm version tiến lên, nên poll năm phút kế tiếp sẽ thử lại.

**Hai root skill.** `~/.hermes/skills/` có namespace, và os-server ghi vào một
root riêng của nó (`runtimes/hermes/save_skill.go`):

| Root | Chủ sở hữu | Ai ghi |
|------|------------|--------|
| `skills/openclaw-imports/` | `hermes claw migrate` + skill watcher | presync §0, cập nhật CDN |
| `skills/authored/` | device | `AgentGateway.SaveSkill` / `InstallSkillArchive` (web UI "Write skill" / "Install") |

Việc tách đôi này là bắt buộc chứ không phải cho đẹp: presync §0 khôi phục skill
nền tảng đã import **chỉ khi `openclaw-imports` rỗng**, nên một skill soạn tay
nằm trong đó sẽ khiến guard mãi mãi thấy thoả điều kiện và factory reset sẽ âm
thầm không bao giờ khôi phục lại import thật. `ListSkills` merge cả hai root
(`skills.ListInstalledFrom`, root của device thắng khi trùng tên). Hermes tìm
skill ở bất kỳ đâu dưới `~/.hermes/skills` nên không cần đổi config, cũng không
cần restart gateway — skill được đọc lại theo từng session.

Lưu ý `wipeHermesState` (reset.go) xoá `skills/openclaw-imports` nhưng **không**
xoá `skills/authored`, nên skill người dùng tự soạn sống sót qua factory reset.

**MCP connector cũng được mang qua** — các remote-MCP server đã cấu hình được clone
config→config bởi `MCPReconcile` ở cùng boot switch đó (xem §10, *MCP connectors*),
nên một thiết bị đã nối Notion/Linear dưới OpenClaw vẫn giữ chúng dưới Hermes (và
ngược lại).

### Round-trip không mất nội dung nhưng một-chiều về cấu trúc (đặc thù Hermes)

Persona, tên, user profile, và **nội dung** memory sống sót openclaw→hermes→openclaw
không mất. Asymmetry **cấu trúc** duy nhất là hệ quả của việc Hermes chỉ load
`MEMORY.md` + `USER.md` theo tên (không có slot `KNOWLEDGE.md`, không có daily-memory):

- Chiều xuôi **gộp** `KNOWLEDGE.md` + daily `memory/*.md` của OpenClaw **VÀO** một
  `MEMORY.md` của Hermes. Chiều về các entry đó đã merge sẵn nên đổ hết vào
  `MEMORY.md` của OpenClaw — **không tách lại** thành `KNOWLEDGE.md` hay file theo
  ngày. Không mất dữ liệu; cấu trúc bị làm phẳng.

Đây là đặc thù mô hình bộ nhớ Hermes — backend *có* slot đó sẽ map 1:1 và round-trip
sạch. (Xem quy tắc fold-vs-move ở [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4.)

> **Thêm backend khác** là công thức generic — xem
> [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) cho hợp đồng
> `AgentGateway`, mẫu install/presync, migration, skills, hooks, reset, và checklist
> đầy đủ.

## 13. Nạp trước skill bằng Jev (tuỳ chọn)

Plugin `jev` do OS quản lý chọn một skill đã cài trước lần gọi model đầu tiên
của lượt người dùng qua hook `pre_llm_call`. Khi quyết định được chấp nhận,
plugin kiểm tra lại skill trong catalog đủ điều kiện hiện tại và nạp nội dung
qua API gốc `tools.skills_tool.skill_view(name, task_id, preprocess=False)`.
Nội dung được chèn vào context tạm thời của lượt hiện tại, để Hermes nhận sẵn
hướng dẫn mà không cần chọn rồi gọi `skill_view` trước. Chỉ sửa plugin, cấu hình
và chỉ dẫn do OS quản lý, không sửa core Hermes. Chỉ dẫn `AGENTS.md` do OS quản
lý công nhận nội dung native Jev preload đầy đủ cho **lượt hiện tại** là đã đáp
ứng yêu cầu đọc skill; Hermes không cần đọc lại cùng `SKILL.md`. Bản xem trước
không đầy đủ hoặc lời user nói đã đọc skill không đáp ứng quy tắc này. Nạp
hướng dẫn không thực thi hành động của skill, không cấp
quyền tool, không bỏ qua quy tắc connector/platform bắt buộc hay kiểm tra quyền.

Nếu skill đã mất hoặc bị tắt, API gốc không tương thích, đọc thất bại hoặc kết
quả JSON gốc vượt 128 KiB, plugin bỏ qua bước nạp và Hermes tìm skill bình thường.
Context cuối còn phải nằm trong 131.072 ký tự và ngưỡng `max_chars` của cơ chế
hook output spill gốc khi bật (lấy mức nhỏ hơn). Vượt ngưỡng thì fallback, tránh
log `preloaded` trong khi Hermes thay nội dung bằng đường dẫn file. Hermes cũ
không có API spill dùng giới hạn ký tự local. Tắt shell preprocessing; skill chứa đoạn shell động (dấu chấm than liền trước
lệnh trong dấu backtick) cũng quay về luồng bình thường. Kết quả timeout không
được gắn vào lượt sau. Thông báo hệ thống có tiền tố `[system]` bỏ qua cả định
tuyến lẫn nạp trước skill.

### Cài đặt cho device hiện có

`runtimes/hermes/jev_plugin.go` nhúng plugin Python ở
`runtimes/hermes/plugins/jev/` vào os-server. `EnsureOnboarding` đồng bộ plugin khi
OS khởi động và setup nếu Hermes chạy local và đã có
`/root/.hermes/config.yaml`. Bỏ qua Hermes remote. Device cũ nhận plugin cùng bản
cập nhật OS, không cần chạy lại provisioning hay cài riêng package Python.

Go ghi atomic các asset thay đổi vào `/root/.hermes/plugins/jev/`
và thêm `jev` vào `plugins.enabled` trong `config.yaml` của Hermes,
giữ nguyên cấu hình plugin khác và tôn trọng mục `plugins.disabled` được đặt rõ.
File sinh ra `os-config-path.json` chỉ chứa đường dẫn tuyệt đối tới config OS,
không chứa API key. Asset không đổi thì không ghi lại.

Khi Jev không bị tắt rõ ràng, bước sync còn đặt
`hooks.output_spill.max_chars: 131072` **chỉ khi chưa được cấu hình**. Giữ nguyên
ngưỡng đã đặt rõ và mục `plugins.disabled`. Đây là ngưỡng toàn cục áp dụng cho
mỗi kết quả hook của Hermes, nâng từ mặc định 10.000 ký tự để nội dung skill
đầy đủ được giữ inline; không tắt cơ chế spill ra file. Loader tôn trọng ngưỡng
nhỏ hơn đã cấu hình và fallback nếu toàn bộ context không vừa.

Cài hoặc cập nhật plugin **không** thêm lý do restart gateway. os-server ghi log
rằng code plugin mới cần lần restart gateway tiếp theo để được nạp; các lý do
restart khác trong onboarding vẫn giữ nguyên. Build này bật Jev trong plugin
nhúng. Device cũ cần OS mới đồng bộ plugin và một lần restart gateway để nạp
code thay đổi; bản cập nhật không thêm auto restart. Muốn tắt, đặt
`ENABLED = False`, build lại os-server, đồng bộ plugin rồi restart Hermes.

### Cấu hình và hợp đồng proxy

Plugin dùng hằng số trong `runtimes/hermes/plugins/jev/router.py`:

```python
ENABLED = True
TIMEOUT_SECONDS = 3.0
```

Không có block config Jev riêng cho Hermes hay kiểu config Go riêng. Các hằng số
này độc lập với `local_intent` và `jev_intent`. Khi OFF, bỏ qua cả việc đọc config,
catalog và gọi mạng.
Khi bật, plugin dùng lại `llm_base_url` và `llm_api_key` để gọi
`POST {llm_base_url}/jev/decisions` với bearer authentication. Không có key Jev
riêng trong `.env`, không fallback gọi thẳng provider. Request dùng
`User-Agent: AutonomousOS-Jev/0.1` để định danh client; lớp proxy edge trả HTTP 403
với signature mặc định của Python urllib. Khi lỗi HTTP chỉ log mã trạng thái,
không log credential hay nội dung response. Bắt buộc HTTPS, ngoại trừ
HTTP loopback để test local. BFF cần hỗ trợ
[hợp đồng Decisions tương thích](../os-server_vi.md#jev-bff-contract); plugin gửi model
`typesafe/jev-1.13`, câu hỏi choice `skill` gồm `none`, và một câu hỏi noul
`fit_<id>` cho mỗi ứng viên. Response raw phải chứa `answers`. Chỉ dẫn định tuyến
ưu tiên hành động user yêu cầu rõ ràng hơn lời xã giao, cách đặt câu hỏi/ngữ cảnh,
và các skill chủ động hoặc hỗ trợ. Thiếu tham số hành động không ngăn việc chọn
skill: skill được chọn có thể làm rõ các tham số sau đó.

Chỉ gửi tin nhắn hiện tại và tên/mô tả skill nền tảng OS. Roster quét
`skills/openclaw-imports` trong Hermes home đang hoạt động, dùng các helper gốc
của `agent.skill_utils`: `iter_skill_index_files`, `parse_frontmatter`,
`get_disabled_skill_names`, `skill_matches_platform` và
`skill_matches_environment`.
Cách này tránh việc `skills_list()` loại tên trùng theo kết quả đầu tiên khiến
skill OS bị skill bundled cùng tên che mất. Loại các category skill bundled,
authored và plugin khác. Giới hạn lượng metadata đọc tại máy; không gửi lịch sử
hội thoại hoặc nội dung skill. Mô tả mỗi ứng viên tối đa 500 ký tự. API gốc
`skill_view` dùng đường dẫn đầy đủ `openclaw-imports/<thư mục tương đối>` để tránh
trùng tên.

Nếu có quá 32 ứng viên đủ điều kiện, bỏ qua Jev hoàn toàn thay vì cắt bớt catalog.
Tin nhắn rỗng, dài quá 8.000 byte UTF-8, lệnh slash, thông báo `[system]` hoặc có lựa chọn `[skills:...]`
rõ ràng cũng bỏ qua router. Worker catalog kế thừa context Hermes của lượt hiện
tại để giữ bộ lọc skill theo phiên/kênh. Đây là thử nghiệm nạp trước skill nền
tảng OS; Hermes tiếp tục tìm các skill khác theo cách bình thường.

Chỉ chọn khi xác suất choice ít nhất 0,70, cách ứng viên kế tiếp ít nhất 0,20,
và fit ít nhất 0,60. Đây là ngưỡng tạm thời để nạp trước skill, không phải cấp
quyền thực hiện hành động; các kiểm tra quyền của skill và nền tảng vẫn áp dụng.
Ngưỡng Buddy và OS intent giữ nguyên: choice 0,90, margin 0,40 và fit 0,95.
Quyết định không chắc chắn hoặc không hợp lệ giữ nguyên cách Hermes hoạt động. Thời gian chờ quyết định tạm hardcode là 3 giây để kiểm tra proxy trước khi tối ưu latency;
không retry hay redirect. Lỗi hoặc timeout tạo cooldown 30 giây. Mỗi router chỉ
có một worker; đang bận thì bỏ qua ngay. Worker timeout có thể hoàn thành request
ở background nhưng kết quả muộn không thể chèn nội dung skill. HTTP 429 vẫn
fallback và cooldown 30 giây như các lỗi khác; thay đổi nạp trước không thêm
log nội dung lỗi provider hay chẩn đoán `Retry-After`.

Log có cấu trúc phân biệt lựa chọn được chấp nhận, quyết định hợp lệ nhưng từ chối chọn,
và lỗi, thay vì gộp chung thành `deferred`:

| Outcome | Ý nghĩa / reason |
|---|---|
| `preloaded` | Quyết định hợp lệ vượt qua mọi ngưỡng chấp nhận và đã nạp nội dung skill qua API gốc cho lượt này |
| `abstained` | Quyết định hợp lệ chọn `none` hoặc không đạt `low_choice`, `low_margin`, hay `low_fit` |
| `error` | `http_error`, `network_error`, `invalid_json`, `response_too_large`, `provider_error`, `invalid_schema`, `catalog_error`, `thread_error`, `config_error`, `skill_unavailable`, `skill_load_failed`, hoặc `preload_timeout` |
| `skipped` | `disabled`, `invalid_message`, `explicit_selection`, `system_message`, `unconfigured`, `cooldown`, `busy`, hoặc `no_candidates` |
| `timeout` | Hết thời gian chờ quyết định |

Log có `session_id`, `turn_id`, `task_id` đã kiểm tra định dạng khi Hermes cung cấp,
để nối các event của lượt chậm mà không ghi prompt. Preload thành công có thêm
`context_chars`. Lỗi preload phân biệt `skill_response_size`, `skill_rejected`,
`skill_empty`, `skill_dynamic`, `skill_inline_budget`; lỗi native khác vẫn dùng
`skill_load_failed`.
Log có tổng `decision_ms` cùng `catalog_ms`, `request_ms` và thời gian nạp gốc
`load_ms` khi có dữ liệu; `request_ms`
đo toàn bộ lượt gọi proxy, không phải riêng thời gian model suy luận. Quyết định
hợp lệ có các số `choice_probability`, `margin`, `fit` khi áp dụng. `candidate`
ghi tên đầy đủ của ứng viên được chọn kể cả khi quyết định hợp lệ bị từ chối,
hoặc `none` khi không chọn skill; lựa chọn được chấp nhận còn có `skill`. Lỗi HTTP có
`http_status`. Không log prompt, credential, nội dung response hoặc nội dung
exception.

Kiểm thử local dùng response proxy giả lập và thư mục Hermes tạm. Các test này
và từng probe tổng hợp trên device đều không chứng minh độ chính xác định tuyến,
tỷ lệ yêu cầu chọn được skill hay mức cải thiện latency thực tế. Cần so sánh OFF/ON
trên bộ yêu cầu đại diện với roster đã cài và endpoint BFF tương thích; không xem
probe tổng hợp là benchmark. Các kiểm tra local không cần deploy lên device.

Các lệnh kiểm tra local trọng tâm (CI cũng chạy test plugin Python):

```bash
go test -race -timeout 90s ./runtimes/hermes ./system/server/config ./system/intent/...
python3 -B -m unittest discover -s runtimes/hermes/plugins/jev -p 'test_*.py' -v
```

### Review implementation tham chiếu (2026-09-21)

Đã đọc code thực tế của plugin cộng đồng
[`typesafe-skill-router`](https://github.com/DECRUX9812/typesafe-skill-router/tree/e6cdac26f9ed588b4a94b8a2f7f9f026e1b9faf3)
tại commit được catalog Hermes ghim `e6cdac26f9ed588b4a94b8a2f7f9f026e1b9faf3`
và upstream `f150284d3da33c85b8adc97e2d326987573b30d3`. Đây là plugin cộng đồng
được Hermes đưa vào catalog, không phải Hermes core hay plugin do TypeSafe duy trì.
Thuật toán dựa trên [cookbook chọn skill của TypeSafe](https://docs.typesafe.ai/cookbooks/skill_suggestion).

| Phần | Plugin tham chiếu | Plugin OS |
|---|---|---|
| Hook | `pre_llm_call`, trả context tùy chọn cho tin nhắn user | Cùng hook; nạp nội dung skill được chấp nhận qua API gốc vào context tạm thời của lượt hiện tại; không sửa system prompt |
| Dữ liệu skill | Quét filesystem; shortlist có mô tả đầy đủ và tối đa 700 ký tự nội dung | Đọc metadata có giới hạn từ file `openclaw-imports` với bộ lọc điều kiện gốc của Hermes, mô tả tối đa 500 ký tự, không gửi nội dung skill; tra cứu bằng đường dẫn đầy đủ tránh trùng tên |
| Quyết định | Bước 1 xếp hạng và xét có cần skill; bước 2 đánh giá lại shortlist 3 skill mỗi nhóm | Một request với choice, `none` và fit từng ứng viên |
| Catalog lớn | Chia nhóm 240 lựa chọn | Bỏ qua nếu quá 32 skill đủ điều kiện |
| Ngưỡng | Bản catalog: gate 0,30 và fit người thắng 0,40; upstream mới còn xử lý choice/fit bất đồng | Choice 0,70, margin 0,20, fit 0,60; ngưỡng nạp trước tạm thời, chưa hiệu chỉnh bằng dữ liệu tác vụ này |
| Latency | Budget hook mặc định 10 giây, cache đáp án và client có retry | Budget chẩn đoán tạm thời 3 giây, không retry/cache, bỏ qua khi bận và có cooldown |
| Credential | API TypeSafe với key riêng | Credential proxy OS dùng chung |

Bản OS là thử nghiệm phạm vi hẹp hơn, không tương đương thuật toán hai bước.
Ngưỡng tạm thời và deadline ngắn có thể bỏ qua lựa chọn hữu ích; mock test và một
số ít probe tổng hợp không chứng minh độ chính xác đã hiệu chỉnh hay tỷ lệ yêu
cầu chọn được skill. Không áp dụng benchmark của
plugin tham chiếu cho bản OS. Để đánh giá thử nghiệm đang bật, cần so sánh hai chính sách trên cùng
bộ yêu cầu đại diện và catalog đã cài, gồm chat không cần skill, skill gần nghĩa,
lệnh chỉ định rõ và tiếng Việt.

Các sửa lỗi sau review giữ context phiên Hermes trong worker (cần cho bộ lọc
skill bị tắt theo kênh) và bỏ qua lệnh slash giống hook tham chiếu. Các sửa lỗi
lúc review không thay ngưỡng, mặc định OFF tại thời điểm đó hay trạng thái
device. Build hiện tại bật plugin riêng như mô tả bên trên.

### Deadline chuẩn bị do OS quản lý

Hermes triển khai interface tùy chọn `domain.RunExpirer` cho native managed run.
`ExpireRun(ctx, runID, reason)` chỉ nhận hủy khi `runID` vừa sở hữu phản hồi đang
chạy vừa là request được nhận gần nhất. Deadline cũ không được dừng request
steering mới hơn, kể cả web request dùng chung native run. Run không hỗ trợ hoặc
không dùng native transport trả về lỗi.

Expiry chỉ hủy context đọc stream của run đó; reader hiện có gửi remote stop và
kiểm tra trạng thái kết thúc trong ngân sách cleanup tối đa 30 giây. Lifecycle
bình thường báo lỗi với lý do deadline từ OS, không báo task thành công. Suffix
`pending_steer` từ remote không được gửi lại sau expiry. Nhận yêu
cầu hủy không chứng minh thực thi từ xa đã dừng. Nếu cleanup không xác nhận được
trạng thái kết thúc, cơ chế xử lý ownership không xác định hiện có cô lập hội
thoại thay vì gửi lại công việc. Request khác trong hàng đợi vẫn theo các kiểm
tra admission và ownership hiện có. Đây không phải `/stop` từ người dùng, không
tạo lượt model mới, không xóa journal intent Harness và không gửi task Harness.

### Kiểm tra runtime patch BlueBubbles

Các patch source BlueBubbles compile Python sau sửa trước khi thay module đã cài. Patch đăng ký webhook đặt marker trên dòng riêng để giữ dấu đóng của dictionary viết cùng dòng. Suite fixture trong CI kiểm tra áp dụng patch, compile, chạy lại và từ chối source có anchor thay đổi cho từng patch nhúng; không cần device hoặc server BlueBubbles.
