# Backend agent PicoClaw

PicoClaw là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Tài liệu này. Code: `runtimes/picoclaw/`.

> Code là nguồn chân lý. Tài liệu này mô tả `runtimes/picoclaw/` đúng như đã
> triển khai; giữ đồng bộ khi thay đổi (EN: `docs/agentic/picoclaw.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · [`hermes_vi.md`](hermes_vi.md) (Hermes) ·
> file này (PicoClaw).
>
> **Trạng thái: đạt parity về install; gateway vẫn chỉ-client.** PicoClaw nay đã có
> installer + hook pre-start phía thiết bị (`runtimes/picoclaw/install.sh` +
> `presync.sh`, được embed và đăng ký qua `install.go` → `runtimereg`), nên một lần
> switch `picoclaw.setup` sẽ cài, cấu hình và khởi động nó giống hermes (§1.1).
> Migrate persona/memory **2 chiều** qua reconciler Go — picoclaw có adapter
> `migrate_persona` (`runtime_picoclaw.go`), nên switch tới/từ nó mang
> SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE cả 2 chiều; **skills** chiều VÀO do `picoclaw
> migrate --workspace-only --force` trong hook presync lo (§1.1).
> Bản thân gateway Go vẫn **chỉ-client**: hầu hết method lifecycle in-process
> (`SetupAgent`, `RefreshModelsConfig` …) vẫn no-op (§8) vì provisioning xảy ra ngoài
> tiến trình trong install.sh/presync. Ngoại lệ là `EnsureOnboarding` (`onboarding.go`,
> giữ khối OS-managed trong SOUL/AGENTS/HEARTBEAT cập nhật), `StartSkillWatcher`
> (`skill_watcher.go`, auto-update skill từ CDN), identity (`identity.go`:
> `WatchIdentity`/`UpdateIdentityName` đọc/ghi `IDENTITY.md` như OpenClaw), và
> `ResetAgent` (`reset.go`, factory-reset xoá sạch `/root/.picoclaw` + onboard lại), và
> emotion-acknowledge (`emotion_ack.go`: turn kênh bắn mặt "thinking" tại `agent:start`
> của observer hook, gate theo capability `expression` như OpenClaw/Hermes) —
> đều là thật (§1.1, §8).
> Các gap còn lại (pin queue/steer) được theo dõi theo checklist
> [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) — xem đó trước khi nâng
> PicoClaw lên parity đầy đủ.

## 1. Khi nào và chọn ra sao

`agent_runtime` trong `config.json` chọn backend; việc phân giải nằm ở
`system/agent/factory.go` `ProvideGateway()`:

| `agent_runtime` | Backend |
|---|---|
| `"openclaw"` / để trống | OpenClaw (mặc định; hoặc `gateway.default` từ `ROBOT.md`) |
| `"hermes"` | Hermes (`hermes.ProvideService`) |
| `"picoclaw"` | PicoClaw (`picoclaw.ProvideService`) |
| giá trị khác | OpenClaw (log là `FALLBACK — unknown runtime=…`) |

Khi khởi động, `ProvideGateway` in banner `AGENT BACKEND ACTIVE → PICOCLAW` kèm
`ws_url`, `conversation`, và `source`.

## 1.1 Cài đặt + provisioning (`install.sh` + `presync.sh`)

Một lần switch `picoclaw.setup` chạy `system/device/switch_runtime.sh` (generic),
script này materialize các script nhúng của PicoClaw rồi điều phối chúng. Hai script
nằm cạnh backend và được embed + đăng ký trong `install.go`:

| Script | Đường dẫn trên đĩa | Chạy khi |
|---|---|---|
| `install.sh` | `/usr/local/lib/os-runtimes/picoclaw/install.sh` | lần switch đầu / `verify` thất bại |
| `presync.sh` | `/usr/local/bin/runtime-picoclaw-presync` | **trước mỗi lần** picoclaw start (và một lần cuối install) |

**`install.sh`** (một lần):
1. cài `jq` + `yq` + binary `picoclaw` đã pin (GitHub release,
   `picoclaw-linux-arm64`) vào `/usr/local/bin`;

   > Pin này chỉ là baseline của image. Máy ngoài thực địa update qua
   > `make upload-picoclaw <release-tag>` + `make promote-picoclaw`, bootstrap
   > worker áp dụng bằng `software-update picoclaw` trên máy có `agent_runtime`
   > là `picoclaw`. Lệnh đó còn ghi tag đã cài vào
   > `/usr/local/lib/os-runtimes/picoclaw/installed-version` — `picoclaw version`
   > in ra chuỗi build không có semver, nên stamp là cách DUY NHẤT worker biết
   > release nào đang cài. Xem `docs/vi/bootstrap-ota.md` §5.
2. `picoclaw onboard` (chỉ khi chưa có `config.json`) tạo `/root/.picoclaw` —
   workspace + `config.json` và `.security.yml` baseline;
3. ghi **`picoclaw.service`** (`ExecStart=/usr/local/bin/picoclaw gateway`,
   `HOME=/root`, `Restart=always`) — `picoclaw gateway` chỉ chạy foreground, nên
   khác hermes (có `gateway install --system`) ta tự bọc nó. Tên unit trùng tên
   runtime nên **không cần** file khai báo `os-runtimes/picoclaw/service`
   (switch-runtime mặc định lấy tên đó);
4. chạy hook presync một lần, rồi drop hook `verify` (`command -v picoclaw`) để
   switch-runtime phát hiện + tự-heal unit mồ côi.

**`presync.sh`** (mỗi lần switch — single owner của config model + channel, nên
tự-heal sau factory reset, giống presync của hermes):
- **§0 migrate** — chốt bằng marker `~/.picoclaw/.openclaw-migrated` (**không** check
  `workspace/skills` rỗng — PicoClaw có sẵn built-in skills nên thư mục đó luôn
  non-empty). Khi marker chưa có và `/root/.openclaw` tồn tại, stop openclaw rồi chạy
  `picoclaw migrate --workspace-only --force` để mang persona/memory/skills từ OpenClaw
  qua. **`--workspace-only`** nghĩa là migrate **không** đụng `config.json` — convert
  `openclaw.json` thành config picoclaw cho ra config hỏng, nên `config.json` giữ bản
  onboard hợp lệ và §1/§2 đắp model/channel/gateway lên trên. Sau đó làm các fixup
  migrate không làm:
  copy `HEARTBEAT.md` + `KNOWLEDGE.md` từ workspace openclaw (KNOWLEDGE.md là living-doc
  learnings của openclaw, seed từ template nhúng rồi append hằng ngày — migrate bỏ qua),
  xoá `AGENT.md` (để PicoClaw chạy đường legacy `AGENTS.md` — chế độ duy nhất đọc
  `IDENTITY.md`), và copy `IDENTITY.md` của openclaw qua (migrate cũng bỏ qua). Cuối
  cùng ghi marker. Factory reset xoá
  `/root/.picoclaw` sẽ xoá marker nên migrate chạy lại; migrate lỗi thì không ghi
  marker và thử lại ở lần switch sau.
- **§0.5 onboarding (`onboarding.go`)** — `EnsureOnboarding`, gọi lúc
  boot/config-change như openclaw/hermes, mirror reconcile của openclaw (rút gọn):
  - seed `KNOWLEDGE.md` từ template nhúng (`resources/KNOWLEDGE.md`) **chỉ khi chưa
    có** — bao case fresh device chỉ-picoclaw mà presync §0 không có bản openclaw để
    copy; không bao giờ overwrite;
  - inject khối managed `<!-- OS DO NOT REMOVE -->` vào `SOUL.md`
    (`ensureSoulMDBlock`, soul theo device-type từ `soul_ref` của ROBOT.md; giữ nội
    dung owner dưới `---`), `AGENTS.md` (`ensureAgentsMDBlock`, quy tắc
    skills/connectors/memory/priority — khối **Connectors (MANDATORY)** định tuyến mọi
    yêu cầu Gmail/Calendar/Drive/… qua skill `connectors` (credentials nằm trên đĩa tại
    `/root/.openclaw/workspace/configs/<code>_access_tokens.json`) để agent không tự cài
    mail client riêng), và `HEARTBEAT.md` (`ensureHeartbeatMDBlock`, synthesis
    hằng ngày) — mirror openclaw nhưng lược nội dung chỉ-openclaw, giữ các block cập
    nhật qua OTA os-server thường;
  - **capability-gate skills** (`pruneUnsupportedSkills`): xoá thư mục skill device
    không dùng được — skill được giữ nếu được `skills.Supported(caps)` hỗ trợ (gate y
    như openclaw) **hoặc** là built-in của picoclaw (`picoclawBuiltinSkills`:
    `agent-browser`, `github`, `hardware`, `skill-creator`, `summarize`, `tmux`,
    `weather`); còn lại trong `workspace/skills` thì xoá. Fail-open khi ROBOT.md không
    khai cap. Không reload (skill đọc per-turn); sau đó tải lại mọi skill được hỗ trợ
    từ CDN, nên skill local đã cũ trước khi watcher chạy sẽ tự phục hồi ở lần
    boot/config reconciliation. Nếu nội dung skill đổi, agent được báo sau khi gateway
    có thể đã restart để đọc lại các file `SKILL.md` mới;
  - khi có block đổi, **restart gateway** (`restartPicoclawGateway` → `systemctl
    restart picoclaw`) để nạp lại file workspace (log+skip nếu không có systemctl).
    Không dùng endpoint `/reload` của gateway — nó cần auth admin mình không có (token
    kênh pico bị từ chối) và chưa chắc re-read workspace markdown; restart thì chắc.
  - các bước đặc thù `openclaw.json` (đăng ký hooks/logging/controlUi) là N/A với
    `config.json` của picoclaw; pin queue/steer là TODO.

Một **skill watcher** riêng (`skill_watcher.go`, chạy lúc boot như openclaw) poll OTA
metadata mỗi 5 phút và tự cập nhật `workspace/skills/<name>` từ CDN khi version của
skill được hỗ trợ thay đổi (gate qua `skills.Supported`), rồi báo agent qua
`SendSystemChatMessage`. Mỗi lần poll thành công đều log `skill watcher: checked`;
nếu tải ZIP hoặc extract lỗi, watcher giữ version của skill đó ở trạng thái pending
và thử lại ở poll kế tiếp.
- **§1 cấu trúc** (`jq` trên `config.json`) — `agents.defaults` (provider
  `anthropic-messages`, `model_name "autonomous"`, `image_model "autonomous_vision"`,
  `restrict_to_workspace:false`, `allow_read_outside_workspace:true`), hai entry
  `autonomous` và `autonomous_vision` (`qwen/qwen3.6-plus`, cùng endpoint campaign-api)
  trong `model_list`, và khung `channel_list`. `channel_list.pico` luôn được bật.
  Trường `model` của mục `autonomous` là alias `Auto-AI`, chỉ proxy campaign-api
  phân giải được — §2 thay nó bằng `llm_model` của người dùng mỗi khi
  `llm_base_url` trỏ đi nơi khác, không thì host sẽ từ chối mọi lượt vì model lạ.
  Mục vision giữ model riêng của nó: đó là một lựa chọn khác, không phải cái này.
- **§2 động** (secrets lấy từ `/root/config/config.json` cấp **project**, thắng) —
  `model_list[autonomous,autonomous_vision].api_base` từ `llm_base_url` (PicoClaw cần
  đuôi `/v1`, khác hermes), `.security.yml` `model_list."autonomous:0".api_keys` +
  `"autonomous_vision:0".api_keys` từ `llm_api_key`, bearer token `pico` (phải khớp `constants.go` `Token`), và mỗi kênh non-pico **chỉ
  bật khi có credential**: telegram (`telegram_bot_token` + `telegram_user_id`),
  discord (`discord_bot_token` + `discord_user_id`), slack (`slack_bot_token` +
  `slack_app_token` + `slack_user_id`), whatsapp native (`whatsapp_user_id` →
  `allow_from`, không token, quét QR lần đầu). Secrets nằm trong `.security.yml` dưới
  `channel_list.<ch>.settings`; phần cấu trúc ở `config.json`.

Log của gateway xác nhận cấu hình khi boot (`Gateway started on 127.0.0.1:18790`,
health ở `/health` `/ready` `/reload`, `Channels enabled: [pico]`). Cảnh báo
`SECURITY: Channel allows EVERYONE (allow_from is empty) channel=pico` là bình
thường: `pico` là gateway native cục bộ của thiết bị và cố tình không có `allow_from`.

## 2. Hằng số kết nối

**Không có config theo từng máy**; endpoint là hằng số compile-time trong
`runtimes/picoclaw/constants.go`:

| Hằng | Mặc định | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18790/pico/ws/` | Endpoint WebSocket PicoClaw cục bộ |
| `Token` | `darren_pico_token` | Bearer token gửi trong header `Authorization` khi connect |
| `Conversation` | `device-main` | Nhãn session mặc định cho tới khi server cấp `session_id` |

## 3. Transport

`client.go` giữ **một WebSocket bền** (gorilla/websocket), giống vòng reconnect
của openclaw nhưng đơn giản hơn — PicoClaw **không có handshake challenge /
pairing**, chỉ là bearer token:

1. `StartWS` dial `WSURL` với `Authorization: Bearer <Token>`.
2. Khi connect, trạng thái sẵn sàng bật (`IsReady`/`ConnectedAt`), LED
   `StateAgentDown` được xóa, và lần reconnect (không phải lần đầu) phát TTS
   reconnect i18n.
3. Một goroutine keepalive gửi `{"type":"ping","id":…}` mỗi 25s; PicoClaw đáp
   `pong` (bỏ qua) để làm tươi read deadline 90s.
4. Vòng đọc dịch từng frame đến và đẩy vào `domain.AgentEventHandler` đã đăng ký
   (đồng bộ — an toàn vì `FetchChatHistory` ở đây là no-op, nên handler không bao
   giờ block chờ một WS RPC).
5. Khi rớt: xóa busy + id lượt đang chạy, vẽ `StateAgentDown`, dừng servo
   tracking (chỉ thiết bị có motion), chờ 5s, reconnect.

## 4. Gửi một lượt

`chat.go` `sendChat` chỉ nhận một lượt đang chạy. Khi rảnh, gửi frame ngay; khi
bận, giữ yêu cầu trong hàng đợi cục bộ và trả về run ID. Câu trả lời đến qua vòng đọc:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>" }, "session_id": "<nếu biết>" }
```

- Lượt có ảnh thêm `payload.attachments: [{ "type": "image", "url": "data:image/jpeg;base64,…" }]` (best-effort; phần text luôn được gửi nên lượt vẫn chạy kể cả khi schema attachment bị bỏ qua).
- Trước khi ghi: đánh dấu busy, lưu `runID` làm **pending run id**, ghi pending chat trace, và phát flow event `chat_input` / `chat_send` (parity với openclaw).

PicoClaw xử lý **mỗi lần một lượt** và không stream token, nên các lượt được liên
kết bằng một `runID` đang chạy duy nhất thay vì id theo từng frame: pending run id
được frame đến đầu tiên của lượt nhận lấy.

Adapter tuần tự hóa cả chat trực tiếp lẫn sự kiện đã đệm: yêu cầu thứ hai không
ghi đè pending run ID của yêu cầu đầu. Chat trực tiếp trong hàng đợi giữ nguyên
text, ảnh, request ID, run ID và source. Cả `chat.final` và `lifecycle.end` đều có
thể báo rảnh, nhưng adapter chỉ nhả lượt sau khi dispatch toàn bộ chuỗi terminal,
rồi gửi đúng một yêu cầu tiếp theo. Lỗi cũng nhả lượt sau dispatch.

Yêu cầu chưa gửi được giữ lại khi callback idle/speaker chạy lúc mất kết nối và
được gửi khi kết nối sẵn sàng, vẫn áp dụng speaker gate, ưu tiên voice và hết hạn/
gộp sự kiện thụ động. Nếu socket biến mất trước thao tác ghi, giữ yêu cầu trong
hàng đợi. Lỗi ghi đóng socket và được coi là chưa rõ đã đến server hay chưa:
không tự gửi lại yêu cầu đó; chỉ phần đuôi chưa thử gửi được giữ qua reconnect.
Khi busy hết hạn sau 45 phút, adapter cũng đóng socket trước khi nhận lượt mới,
tránh gắn phản hồi đến muộn vào yêu cầu tiếp theo.

Protocol không có request ID trong phản hồi hoặc xác nhận replay. Tuần tự hóa
cục bộ không phân biệt được lượt ngoài tự phát/xen kẽ hay server phát lại phản
hồi cũ trên kết nối mới. Không sao chép bộ lọc lặp output CLI và ghép request ID
riêng của Codex sang adapter này. Các nhánh trên đã được kiểm tra bằng WebSocket
cục bộ; vẫn cần xác minh với PicoClaw/device thực tế.


## 5. Ánh xạ protocol đến → `domain.WSEvent`

Đây là phần then chốt để Flow Monitor / web-chat render đúng. Chỉ nhìn `type`
**không đủ** — `message.create` / `message.update` phải phân loại theo payload
(`placeholder` / `kind` / `tool_calls` / `content`), theo đúng thứ tự ưu tiên này
(`translator.go` `categorize`):

| Frame đến | Phân loại | `domain.WSEvent` phát ra |
|---|---|---|
| `typing.start` | bắt đầu lượt | `agent` lifecycle `phase:start` (một lần mỗi lượt) |
| `message.create/update`, `placeholder:true` | đang nghĩ | *(không có — trạng thái, không phải nội dung)* |
| `message.create/update`, `kind:"thought"` / `thought:true` | reasoning | *(không có — chỉ là trạng thái)* |
| `message.create`, `kind:"tool_calls"` / có `tool_calls` | gọi tool | `agent` tool `phase:start` + `phase:end` mỗi call |
| `message.create/update`, `content` khác rỗng (không dính các mục trên) | **câu trả lời cuối** | `agent` `stream:assistant` (toàn bộ reply là một delta) **+** `chat` `state:final role:assistant` **+** `agent` lifecycle `phase:end` (kèm usage) — **kết thúc lượt** |
| `error` | lỗi | `agent` lifecycle `phase:error` — kết thúc lượt |
| `typing.stop` / `message.delete` / `pong` | — | *(bỏ qua)* |

### Lưu ý vòng đời lượt

- **`typing.stop` KHÔNG phải mốc kết thúc lượt.** Nó đến sớm, ngay sau giai đoạn
  nghĩ. Lượt chỉ kết thúc ở frame **final** đầu tiên (hoặc `error`).
- **Lượt không tool:** `typing.start → placeholder → typing.stop → message.update (final)`.
  Final là `message.update` dùng lại `message_id` của placeholder.
- **Lượt có tool:** `placeholder → typing.stop → message.delete (xóa placeholder)
  → message.create kind:"tool_calls" (×N) → message.create (sạch, final)`.
- PicoClaw không phát frame kết quả tool riêng, nên mỗi tool call phát `tool`
  `phase:start` rồi ngay sau là `phase:end` với result rỗng, chỉ để đóng trace.
- **Không stream → một assistant delta.** PicoClaw trả toàn bộ reply trong một frame
  final duy nhất, nhưng consumer dùng chung lại rút TTS + marker phần cứng `[HW:/…]`
  (và các node `tts_speak` / `hw_*` trên Flow Monitor) từ **stream assistant-delta**,
  flush ở `lifecycle.end`. Vì vậy câu trả lời cuối được phát dưới dạng `agent`
  `stream:assistant` — toàn bộ reply (giữ nguyên marker) là **một** delta — **trước**
  `chat.final` / `lifecycle.end`, chính là trường hợp N=1 của hợp đồng streaming
  openclaw/hermes. Thiếu nó thì reply vẫn hiện ở web chat nhưng không bao giờ ra loa
  hay phần cứng. Xem `translator.go` `emitFinal`.
- `media.create` có trong protocol nhưng server không bao giờ phát — media đi kèm
  trong `message.create` qua `attachments`.

### Cấu trúc tool call

Mỗi phần tử trong `tool_calls` theo kiểu OpenAI: tên + tham số nằm ở
`function.name` và `function.arguments` (là **chuỗi JSON**, không phải object).
Lời dẫn người-đọc-được của agent nằm ở `extra_content.tool_feedback_explanation`
(có thể lẫn ký tự điều khiển ANSI từ input terminal). Translator hiện chuyển tiếp
`name` + `arguments`; explanation chỉ được log chứ không hiển thị (device
`AgentPayload` không có chỗ cho nó).

### Token usage

`context_usage` (chỉ có ở frame final) là kích thước context tích lũy, không phải
input/output theo từng lượt. Ánh xạ thành `TokenUsage{ InputTokens: history_tokens,
TotalTokens: used_tokens }`.

## 6. Session

PicoClaw sở hữu session: `session_id` do server cấp được bắt từ frame đến bất kỳ
và lưu lại (`SetSessionKey`) để `message.send` kế tiếp gửi kèm. `NewSession` chỉ
xóa id cục bộ để lượt kế tiếp bắt đầu session server mới. Không có RPC compact nên
`CompactSession` trả `domain.ErrNotSupportedByRuntime` (caller log lại và xoay
session qua `NewSession` thay thế).

## 7. Khả năng kênh (channel capability)

PicoClaw **chỉ chạy telegram**. Vòng nhận Telegram do **thiết bị sở hữu** (điều
khiển bởi `config.TelegramBotToken`), và PicoClaw không có delivery slack/discord
riêng. Ba phương thức kênh trong `runtimes/picoclaw/channels.go` mã hóa điều này
một cách trung thực:

| Phương thức | telegram | slack / discord / whatsapp |
|---|---|---|
| `SupportedChannels()` | trả về `[telegram]` (mục duy nhất) | — |
| `AddChannel(…)` | **no-op thành công** trung thực — telegram do thiết bị sở hữu, nên không có gì để ghi vào runtime | trả về `domain.ErrChannelNotSupported` |
| `RefreshChannelConfig(…)` | `("", nil)` — no-op thành công (không cần re-apply runtime) | trả về `domain.ErrChannelNotSupported` |

Đây là một phần của **mô hình capability generic toàn repo**: mọi runtime khai báo
`SupportedChannels()` và trả về `domain.ErrChannelNotSupported` (chuỗi
`"channel_not_supported"`) cho các kênh nó không chạy được, thay cho no-op im lặng
kiểu cũ. Hành vi not-supported dùng chung và `ChannelReconcile` sau khi switch được
mô tả trong [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) — xem ở đó
thay vì lặp lại tại đây.

**Khi switch TỪ openclaw → picoclaw:** nếu openclaw đã cấu hình slack/discord, các
kênh đó trở thành không hỗ trợ dưới PicoClaw. Sau khi switch, `ChannelReconcile`
báo cáo chúng trong trường `unsupported_channels` của uplink MQTT info
(`domain.MQTTInfoResponse`), và creds của chúng **vẫn nằm trong `config.json`** —
switch ngược lại openclaw sẽ khôi phục chúng.

## 7.1 Lượt kênh trên Flow Monitor (observer hook)

Một lượt Telegram được xử lý hoàn toàn bên trong gateway PicoClaw (gateway sở hữu
I/O kênh) và **không** đi qua WebSocket, nên — y hệt Hermes — os-server sẽ không
thấy nó. Parity được khôi phục bằng một **observer hook**, tương tự trực tiếp hook
`os-server-observer` của Hermes: os-server ship một subprocess nhỏ mà gateway chạy
trên pipeline agent dùng chung, subprocess đó forward mỗi lượt về endpoint loopback
`POST /api/agent/channel-turn` mà handler `ChannelTurn` dùng chung đã phục vụ
(`handler_channel_turn.go`). Không đổi consumer/UI — text user vào làm sáng node
IN, và mọi marker `[HW:/…]` trong reply điều khiển phần cứng cục bộ, giống Hermes.

Hai điểm khác Hermes, do `runtimes/picoclaw/hooks.go` sở hữu:

1. **Transport.** Process hook của PicoClaw là một **subprocess nói NDJSON JSON-RPC
   qua stdio** (`resources/hooks/os-server-observer/observer.py`), không phải hàm
   `handle()` in-process được phát hiện bằng quét thư mục. PicoClaw gửi cho subprocess
   (đã verify trên device, picoclaw 0.2.9):
   - `hook.hello` — một REQUEST (có `id` JSON-RPC); script trả `{"action":"continue"}`.
     Mọi request (có `id`) đều được đáp ngay để không chặn lượt.
   - `hook.runtime_event` — một NOTIFICATION mà `params` là envelope sự kiện
     `{kind, scope{channel,chat_id,sender_id,session_key,turn_id}, payload}`. Script
     chỉ xử lý hai kind, bỏ phần còn lại (`agent.tool.*`, `agent.llm.*`):
     - `agent.turn.start` → `agent:start`, message = **`payload.UserMessage`**.
     - `agent.turn.end`   → `agent:end`, response = **`payload.FinalContent`** (reply,
       KÈM marker `[HW:/…]` — `agent.turn.end` mang cả user message lẫn reply cuối, nên
       **chỉ observe là đủ; không cần intercept**).
   - forward **mọi** kênh mặc định (channel-agnostic như hook Hermes —
     `OBSERVER_CHANNELS` là allow-list tùy chọn, rỗng = tất cả) VÀ bỏ sender nội bộ
     (`sender_id == heartbeat`). Lượt cục bộ `pico` mà os-server đã log qua
     `sendChat`/`session.message` bị loại ở phía sau bởi `skipPlatform`
     (`channelHookSkipPlatforms` nay có thêm `pico`, cạnh `api_server`/`cli`) — nên
     forward-all không thể double-count hay double-fire ack. Đúng cách Hermes loại
     lượt `api_server` của chính nó.
   - map `scope` → payload `ChannelTurn` (`platform=channel`, `chat_id`,
     `sender_id`→`user_id`, `session_key`→`session_id`). PicoClaw ghép 2 forward thành
     một lượt Flow theo `session_key`.
   - POST chạy trên daemon thread (audit log vẫn đồng bộ) nên os-server chậm không làm
     nghẽn event kế (`observer_timeout_ms` chỉ 500ms).
   - `OBSERVER_DEBUG=1` dump mỗi dòng stdin thô ra stderr (hiện trong log gateway dạng
     `Process hook stderr hook=os-server-observer`) — dùng để xác minh tên field trên.

2. **Đăng ký.** PicoClaw **không** quét thư mục hook; hook được đăng ký bằng một mục
   `config.json` dưới `hooks.processes.<name>`, gate bởi CẢ `hooks.enabled` toàn cục
   LẪN **`enabled` per-process** (đều mặc định false — cùng dạng gate toàn cục +
   per-server của `tools.mcp`; thiếu `enabled` per-process thì PicoClaw lặng lẽ KHÔNG
   spawn subprocess). Nên `ensureObserverHook()` (gọi từ `EnsureOnboarding`) làm hai
   việc ghi: materialize `observer.py` ra `/root/.picoclaw/hooks/os-server-observer/`
   (thay placeholder `__OS_SERVER_TURN_URL__`), và upsert:

   ```json
   "hooks": { "enabled": true, "processes": { "os-server-observer": {
     "enabled": true,
     "transport": "stdio",
     "command": ["python3", "/root/.picoclaw/hooks/os-server-observer/observer.py"],
     "env": { "OS_SERVER_TURN_URL": "http://127.0.0.1:<HttpPort>/api/agent/channel-turn", "OBSERVER_DEBUG": "1" },
     "observe": ["turn_start", "turn_end"]
   } } }
   ```

   Nó idempotent (so diff script + diff marshal config) và trả `changed`;
   `EnsureOnboarding` chỉ restart gateway khi có thay đổi, vì gateway chỉ nạp hook
   lúc khởi động. Việc ghi config được serialize dưới cùng `mcpMu` bảo vệ
   `config.json` (xem `mcp.go`).

## 8. Những phần để stub

Mọi thứ không nằm trên hot path của PicoClaw đều là no-op để thỏa interface
`domain.AgentGateway` mà không bịa ra tính năng backend không có: `SetupAgent`,
pairing WhatsApp, `RefreshModelsConfig`, `FetchChatHistory`,
`CompactSession`, watcher model
(`StartModelSync`/`StartPrimaryModelWatch`), `UpdatePrimaryModel`.
Các stub có trả error (`RefreshModelsConfig`, `UpdatePrimaryModel`,
`CompactSession`) trả `domain.ErrNotSupportedByRuntime` — không bao giờ `nil` —
để caller phân biệt "không có gì để áp dụng" với "đã áp dụng" (xem
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4 "Không thành công giả"). (`AddChannel` /
`RefreshChannelConfig` KHÔNG phải stub — trả `domain.ErrChannelNotSupported` cho kênh
không hỗ trợ, xem §7; `EnsureOnboarding` (§1.1) và `StartSkillWatcher` (auto-update
skill, §1.1) là thật.) Các hàm sau cũng là **thật**, không phải stub: `RestartAgent`
(restart systemd unit `picoclaw` qua `restartPicoclawGateway`), `ResetAgent`
(factory-reset wipe — xem §8.2), `GetConfigJSON` (trả
`/root/.picoclaw/config.json` — file structure; secrets ở `.security.yml` không bao
giờ lộ), `WriteMCPEntry` / `RemoveMCPEntry` (connector MCP — xem §8.1), và
`WatchIdentity` / `UpdateIdentityName` (`identity.go`) — `IDENTITY.md` của
PicoClaw copy 1-1 từ OpenClaw nên dòng card `**Name:**` được watch (→ wake words) và
ghi lại y hệt OpenClaw. HAL TTS/voice, fan-out
Telegram, hàng đợi/drain sensing-event, và các helper run-marker (guard / broadcast /
web-chat / silent / pose-bucket) đều backend-agnostic và hành xử y hệt backend Hermes.

Những phần này no-op **có chủ đích**: PicoClaw được provisioning ngoài tiến trình
bởi `install.sh` + `presync.sh` (§1.1), không phải bằng các lời gọi gateway
in-process. Cài đặt, cấu hình model/channel, và migrate persona đều diễn ra trong các
script đó trong luồng `switch-runtime`. Ngoại lệ duy nhất là **`EnsureOnboarding`**
(`onboarding.go`) — nó là thật: inject khối OS-managed vào `workspace/AGENTS.md` lúc
boot/config-change (§1.1), đúng hợp đồng như openclaw.

### 8.1 Connector MCP (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` nối các connector remote-MCP (luồng MQTT
`connector.set` — Notion, Asana, Linear, GitHub, Ahrefs, Figma) vào `config.json` của
PicoClaw. Đây là cùng các caller dùng chung mà OpenClaw/Hermes gọi; chỉ khác hình dạng
trên đĩa, và của PicoClaw khác ở hai điểm mà `mcp.go` xử lý:

1. **Lồng.** Server của PicoClaw nằm ở **`tools.mcp.servers.<name>`**, KHÔNG phải map
   top-level `mcp.servers` (OpenClaw) hay `mcp_servers` (Hermes). `applyMCPServerWrite`
   tạo chuỗi `tools` → `mcp` → `servers` nếu chưa có.
2. **Gate toàn cục.** `tools.mcp.enabled` mặc định **`false`** — server ghi dưới khối
   đang tắt sẽ bị bỏ qua âm thầm. `WriteMCPEntry` bật nó lên `true`.

Entry OpenClaw-shape đầu vào (`{type:"http", url, headers}` cho MCP hosted,
`{command, args, env}` cho stdio) được truyền qua kèm khẳng định **`enabled: true`**
(cờ per-server của PicoClaw). Key `type` được **giữ nguyên** — các giá trị transport
của PicoClaw (`stdio` / `sse` / `http`) đã khớp sẵn, và `type:"http"` tường minh tránh
suy luận empty-type→`sse` của PicoClaw. `RemoveMCPEntry` xóa server theo tên
(idempotent — `removed=false`, không restart, khi vắng mặt) và để `tools.mcp.enabled`
bật để các server khác vẫn nạp. Cả hai đường ghi `config.json` atomic (temp + rename,
không chown — PicoClaw chạy root) dưới `mcpMu`, rồi `restartPicoclawGateway`. Secrets ở
`.security.yml` không bao giờ bị đụng tới.

### 8.2 Factory reset (`reset.go`)

`ResetAgent` là factory-reset wipe của PicoClaw, được `server/system/factoryreset.go`
gọi trên gateway đang active. Khác OpenClaw (giữ `identity/` + `device-key.json`, để
`SetupAgent` tạo lại `openclaw.json`) và Hermes (reset `config.yaml`/`.env` tại chỗ,
giữ `SOUL.md`), **PicoClaw không giữ gì cả**: `config.json` + `.security.yml` của nó
được `presync.sh` tái tạo từ project `/root/config/config.json` ở lần switch kế tiếp,
nên reset **xóa sạch** `/root/.picoclaw` rồi onboard lại một baseline sạch.

`wipePicoclawState()` chạy 4 bước:

1. **`systemctl stop picoclaw` + verify** — systemd unit của gateway (do `install.sh`
   ghi) đặt `WorkingDirectory=/root/.picoclaw` và `Restart=always`, nên nó giữ mở data
   dir và sẽ tái tạo file dưới đó. Stop chủ động ghi đè `Restart=always` (không phải
   crash) nên gateway nằm yên trong lúc wipe. `waitForPicoclawStop` poll `is-active`
   tối đa 5s.
2. **`systemctl disable picoclaw`** — factory reset cũng xóa `/root/config/config.json`
   và reboot về runtime **mặc định (openclaw)**, nên PicoClaw KHÔNG được auto-start.
   `switch-runtime` chỉ re-enable khi user switch trở lại.
3. **`rm -rf /root/.picoclaw`** — config, `.security.yml`, workspace (persona/memory/
   skills), sessions, và **marker `.openclaw-migrated`** (để `presync.sh` §0 migrate
   lại persona/memory từ OpenClaw ở lần switch kế).
4. **`picoclaw onboard`** (`HOME=/root`) — tạo lại baseline hợp lệ (workspace +
   `config.json`/`.security.yml` cơ bản). Không-tử-vong: config chưa đúng cho tới khi
   `presync.sh` khẳng định lại model/channel thật ở lần switch kế, và `install.sh` cũng
   onboard khi thiếu `config.json`, nên lỗi ở đây tự lành.

Gateway được để **stopped + disabled** — wizard setup sau reboot chạy `SetupAgent` của
runtime mặc định, giống cách OpenClaw/Hermes disable unit của chính mình trong
`ResetAgent`.

### Nạp skill trước qua Jev

Khi bật, onboarding OS cài hook stdio native `hooks.processes.jev`, với
`intercept: ["before_llm"]`. Khi asset hoặc đăng ký thay đổi, dùng cơ chế restart
gateway hiện có của onboarding; reconcile không đổi không restart.
**Jev mặc định tắt** bằng `const jevEnabled = false` trong `runtimes/picoclaw/jev_hook.go`. Khi tắt, bridge không tạo selector; onboarding native không cài asset hay tạo đăng ký Jev mới. Nếu có đăng ký Jev cũ, Go chỉ tắt đăng ký đó; các cấu hình khác được giữ nguyên. Không đọc skill, gọi provider hay thêm nội dung Jev vào yêu cầu khi tắt. Muốn bật sau kiểm chứng, đổi cờ Go rồi build và restart qua luồng quản lý runtime. Observer vẫn quản lý global gate `hooks.enabled`; Jev không thay đổi gate đó.
Sidecar chỉ chứa đường dẫn tuyệt đối tới config OS; credential được đọc khi dùng.

Hook dùng chung selector Hermes: tối đa 32 ứng viên, ngân sách chọn/nạp tổng cộng
3 giây, choice >= 0.70, margin >= 0.20, fit độc lập >= 0.60. Lỗi, worker bận,
timeout, abstain, slash command hoặc chỉ dẫn `[skills: ...]` giữ cơ chế tìm skill
bình thường. Chỉ câu người dùng hiện tại và mô tả ứng viên được gửi tới Jev,
không gửi lịch sử chat. Log `[picoclaw-jev]` không ghi prompt, credential hay nội
dung skill.

Danh sách ứng viên lấy từ `<skills>` trong system prompt native của PicoClaw,
giới hạn ở file tồn tại trong `/root/.picoclaw/workspace/skills`. Không tự tạo
catalog bằng cách quét thư mục khác. Thiếu roster hoặc sai định dạng thì bỏ qua.
Toàn bộ SKILL.md được chọn, đường dẫn tuyệt đối và thư mục tham chiếu được nạp
vào context tạm của user message hiện tại; giữ nguyên system message và tools.
Từ chối file quá 128 KiB, đường dẫn vượt skill root, tên trùng không rõ ràng và
inline shell template; preload không thực thi nội dung skill. Cache RAM giới hạn 256 turn chỉ nhớ skill đã chọn và prompt gốc. Mỗi iteration
kiểm tra lại roster native hiện tại và đọc lại toàn bộ file an toàn; skill bị gỡ
hoặc lỗi đọc sẽ hủy lựa chọn. Turn có attachment/content-part, system, sensing
hay đã handled đều bỏ qua Jev. Đọc theo descriptor với no-follow chặn symlink
và file không phải regular; context sau đóng gói cũng giới hạn 128 KiB. Không ghi persona
hoặc file lịch sử. Turn thiếu ID/iteration và child turn được bỏ qua.

Hook native bao phủ kênh WebSocket `pico` (voice/Web/MQTT qua OS) lẫn kênh trực
tiếp của PicoClaw như Telegram; không đặt interceptor tại dispatcher OS.
Đối chiếu source checkout Autonomous PicoClaw commit
`3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488`, cụ thể
[`hooks.go`](https://github.com/autonomous-ai/picoclaw/blob/3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488/pkg/agent/hooks.go),
[`hook_process.go`](https://github.com/autonomous-ai/picoclaw/blob/3f6a5c9e7d31fde3b5096aa3b37f55b4f6292488/pkg/agent/hook_process.go),
`pipeline_llm.go` và `pkg/skills/loader.go`: hook before-LLM được sửa message
không phải system; sửa system message hoặc tool definition bị chặn. Pipeline
dùng message từ hook cho provider call, tách khỏi lịch sử được lưu.
Test local/mock kiểm tra độc lập kênh, cô lập turn, giới hạn đường dẫn/nội dung
và opt-out. Chưa gọi Jev thật, chạy binary native, task trả phí hay test robot;
release cài trên thiết bị vẫn cần native smoke test.

Follow-up ngắn/phụ thuộc ngữ cảnh như “brighter”, “Make it brighter”, “continue”
và “do it” bỏ qua preload để main model hiểu task đang tiếp tục; lệnh nhắm rõ
phần cứng và yêu cầu công việc số mới rõ ràng vẫn dùng chọn skill bình thường.

Chỉ `[voice-instruction]` duy nhất ở đầu envelope được gửi tới selector; transcript mâu thuẫn bị loại khỏi đầu vào chọn skill. Envelope sai hoặc instruction rỗng bỏ qua preload. Message gốc gửi runtime được giữ nguyên.
