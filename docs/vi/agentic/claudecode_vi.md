# Backend agent Claude Code

Cách `os-server` điều khiển **Claude Code** (agent CLI của Anthropic) làm
runtime agentic có thể hoán đổi của thiết bị, bên cạnh OpenClaw, Hermes,
PicoClaw, Codex và OpenCode. Cơ chế generic (flow switch, install-vs-presync, migration, checklist)
nằm ở [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md); file này là
protocol, layout và các quirk đặc thù claudecode.

> **Trạng thái:** đạt parity đầy đủ với checklist — install nhúng + presync,
> transport bridge WebSocket, adapter migrate persona/memory bằng Go (lossless
> với layout OpenClaw), skills (restore từ CDN + watcher, `.claude/skills`
> native), watch/rename identity, MCP thật (`.mcp.json`), factory reset,
> Telegram + Discord do device sở hữu (loop getUpdates `telegram_poll.go` /
> session discordgo `discord.go` — channel plugin native cố ý không dùng),
> **Slack inbound do device sở hữu** (HTTP mode, `domain.SlackBridge`),
> và flow **claude.ai OAuth login** (§7b) thay thế cho API key trong
> config.json. Các caveat đã biết được đánh dấu ⚠️ ở §11.

Code: `runtimes/claudecode/`.

| Thành phần | Vị trí trên thiết bị |
|------|-----------------|
| Claude Code CLI | `/usr/local/bin/claude` (symlink → `/root/.local/bin/claude`) |
| Bridge (systemd `claudecode.service`) | subcommand `os-server claudecode-gatewayd` (biên dịch sẵn trong `/usr/local/bin/os-server`; code `runtimes/claudecode/gatewayd/`) |
| Env khởi chạy (`ANTHROPIC_*`, cờ channel) | `/root/.claudecode/.env` (presync sở hữu) |
| Workspace (cwd của Claude) | `/root/.claudecode/workspace/` |
| Persona / memory | `workspace/{CLAUDE,SOUL,IDENTITY,USER,MEMORY,KNOWLEDGE}.md`, `workspace/memory/*.md` |
| Skills | `workspace/.claude/skills/<name>/` (skill Claude Code native) |
| MCP connector | `workspace/.mcp.json` |
| State resume session | `/root/.claudecode/session.json` |
| Offset poll Telegram (loop device sở hữu) | `/root/.claudecode/telegram_offset.json` |
| Credentials claude.ai OAuth (flow login) | `config.json` `claude_code_oauth_token` + `/root/.claude/.credentials.json` |
| Transcript hội thoại | `/root/.claude/projects/` (nội bộ Claude) |

---

## 1. Chọn + cài đặt

`config.agent_runtime: "claudecode"` (hoặc `gateway.default` trong ROBOT.md)
resolve backend trong `system/agent/factory.go`. Switch vào/ra đi qua flow
`switch-runtime` generic — không có gì đặc thù claudecode trong switcher.

**`install.sh`** (nhúng, chạy một lần ở lần switch đầu / khi `verify` thất bại):

1. tiền đề: `jq curl`;
2. Claude Code CLI qua installer native chính thức
   (`curl -fsSL https://claude.ai/install.sh | bash` → `~/.local/bin/claude`,
   binary standalone, linux arm64/amd64, không cần Node.js), symlink sang
   `/usr/local/bin/claude`;

   > **Update ngoài thực địa** không chạy lại đường cài này từ đầu: publish bằng
   > `make upload-claudecode <semver-trần>` + `make promote-claudecode`, bootstrap
   > worker sẽ chạy `software-update claudecode` (chạy lại installer đúng version
   > đó, trỏ lại symlink, restart `claudecode.service`) trên máy có
   > `agent_runtime` là `claudecode`. Không có backup rollback — installer vẫn giữ
   > `~/.local/share/claude/versions/<ver>`, nên muốn lùi thì publish bản cũ. Xem
   > `docs/vi/bootstrap-ota.md` §5.
3. **không bun, không channel plugin** — telegram + discord do device sở hữu
   (os-server tự chạy các receive loop, §7), nên bước plugin marketplace bỏ
   hẳn;
4. chạy hook presync một lần (bridge + env + sync channel);
5. ghi + start **`claudecode.service`** (tên unit == tên runtime — không cần
   file khai báo service). Unit chạy `os-server claudecode-gatewayd`
   (kèm `EnvironmentFile=-/root/.claudecode/.env`). Thân unit bị duplicate
   trong `gateway_unit.go` (self-heal của `EnsureOnboarding`) — **giữ hai bản
   đồng bộ**;
6. hook verify `/usr/local/lib/os-runtimes/claudecode/verify` =
   `command -v claude` + `/usr/local/bin/os-server` executable (gatewayd nằm
   trong đó; presync tự lành mọi thứ còn lại).

**`presync.sh`** (nhúng; materialize thành
`/usr/local/bin/runtime-claudecode-presync` mỗi lần switch, được switch-runtime
chạy trước khi start, install.sh chạy một lần, và `EnsureOnboarding` chạy trên
**mỗi lần os-server boot / config đổi** — pattern của hermes, nên thiết bị boot
thẳng vào claudecode hoặc sửa `llm_*` khi đang active sẽ tự lành mà
không cần switch):

- bản thân bridge **không còn được materialize ở đây** — nó nằm trong binary
  os-server dưới dạng subcommand `claudecode-gatewayd`, nên một OTA os-server
  thường sẽ cập nhật nó;
- **§1 SEEDS** — `~/.claude.json` nhận `hasCompletedOnboarding` +
  `bypassPermissionsModeAccepted` (không có TTY để trả lời prompt tương tác);
  `workspace/.claude/settings.json` nhận `enableAllProjectMcpServers: true`
  (tin các entry `.mcp.json` do os-server ghi);
- **§2 ENV** — `/root/.claudecode/.env`, theo một trong hai **auth mode** (§7b):
    - *subscription* (`claude_code_oauth_token` đã set trong config.json, hoặc
      `~/.claude/.credentials.json` có trên đĩa): inject `CLAUDE_CODE_OAUTH_TOKEN`
      và **bỏ toàn bộ biến `ANTHROPIC_*`** — biến API-key đứng trên OAuth trong
      thứ tự ưu tiên credential của Claude Code, nên để chúng lại sẽ âm thầm giữ
      thiết bị trên đường API-key;
    - *api-key* (mặc định): `ANTHROPIC_BASE_URL` ← `llm_base_url` **đã cắt
      `/v1` ở cuối** (llm_base_url theo convention OpenAI nên kết thúc bằng
      `/v1`; Claude tự nối `/v1/messages`, không cắt thì proxy nhận
      `/v1/v1/messages` → 404), `ANTHROPIC_API_KEY` ← `llm_api_key`
      (**chỉ x-api-key** — campaign-api trả 401 với dạng `Authorization:
      Bearer`, mà claude ưu tiên `ANTHROPIC_AUTH_TOKEN` hơn
      `ANTHROPIC_API_KEY` khi set cả hai, nên biến bearer phải để trống),
      `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` ← `llm_model` (mặc định
      `Auto-AI`).

  Cả hai mode đều thêm `DISABLE_AUTOUPDATER=1` và
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. `CLAUDECODE_CHANNELS` không
  còn được ghi (không plugin channel nào chạy — §7);
- **§3 CHANNELS** — không còn gì để cấu hình: presync chỉ xoá state
  `~/.claude/channels` cũ từ các bản trước (xem §7).

## 2. Hằng số wire (`constants.go`)

| Hằng số | Giá trị | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18791/claude/ws/` | endpoint WebSocket của bridge |
| `Token` | `autonomous_claudecode_token` | bearer token khi connect; gatewayd mặc định dùng đúng hằng số này (biên dịch trong os-server) — hai bên PHẢI khớp |
| `Conversation` | `device-main` | chỉ là nhãn; Claude sở hữu session id thật |

## 3. Bridge (`os-server claudecode-gatewayd`)

Claude Code không có server mode, nên systemd unit chạy một gatewayd Go nhỏ
(`runtimes/claudecode/gatewayd/`, cấu trúc mirror gatewayd của codex — không
còn phụ thuộc python3/websockets), gatewayd này:

- giữ **một process Claude headless bền**:
  `claude --print --verbose --input-format stream-json --output-format
  stream-json --dangerously-skip-permissions`, cwd = workspace, env từ `.env`
  cộng `HOME` và `IS_SANDBOX=1` được assert sẵn (device chạy root, claude từ
  chối `--dangerously-skip-permissions` dưới uid 0 nếu thiếu escape hatch
  containerized-root này); `--resume <session_id>` khi respawn (session liên
  tục qua các lần bridge restart, state trong `session.json`). Gatewayd vẫn
  hỗ trợ passthrough `--channels <CLAUDECODE_CHANNELS>`, nhưng presync không
  còn set biến này (channel đều do device sở hữu, §7);
- serve WebSocket (gorilla/websocket), gate bằng bearer token (close code
  `4401` khi token sai); mỗi lúc một client — kết nối mới thay thế kết nối cũ;
- forward **nguyên văn** các event stream-json trên stdout của Claude tới
  client đang kết nối, convert frame `message.send` thành message `user`
  stream-json trên stdin (attachment ảnh data-URL → content block `image`
  base64), trả lời `ping` bằng `pong`;
- restart child khi exit (backoff 5 s); nếu đang có turn in-flight thì phát
  `bridge.error` để os-server đóng run thay vì chờ hết busy TTL; `session.new`
  restart child **không kèm** `--resume`;
- queue các frame `message.send` đến trong lúc child đang down và flush khi
  respawn; khi gateway dừng, nó chờ child loop reap process group Claude rồi
  mới giải phóng runtime state.

### Ranh giới bảo mật: Claude chạy root, không sandbox

Cả child gatewayd thường trú lẫn hand-off coding qua Telegram đều gọi Claude với
`--dangerously-skip-permissions` dưới uid 0. `IS_SANDBOX=1` chỉ đáp ứng kiểm tra
root-mode của Claude CLI; nó **không** sandbox tiến trình. Vì vậy, một yêu cầu coding
được chấp nhận có thể chạy tool với quyền tương đương root trên thiết bị: đọc credential
của device/agent, thay đổi root filesystem hoặc service, gọi hardware API nội bộ và gửi
network request.

Allowlist `telegram_user_id`, bearer token WebSocket của gateway và ranh giới service
chỉ-loopback là các lớp authorization cho quyền năng này. Hãy coi việc thêm một ID vào
allowlist, expose bridge hoặc đưa nội dung không tin cậy vào coding flow là cấp quyền
điều khiển thiết bị mức root. Không dùng runtime này khi mức authority đó không chấp nhận được.

Path, port và token override được qua các biến môi trường `CLAUDECODE_*`
(`CLAUDECODE_WS_TOKEN`, `CLAUDECODE_PORT`, `CLAUDECODE_HOME`,
`CLAUDECODE_WORKSPACE`, `CLAUDECODE_ENV_FILE`, `CLAUDECODE_SESSION_FILE`,
`CLAUDECODE_BIN`, `CLAUDECODE_RESTART_BACKOFF_S`); giá trị mặc định khớp layout
thiết bị ở trên, nên các deployment `/root/.claudecode` hiện có chạy y nguyên.

## 4. Gửi một lượt (`chat.go`)

`sendChat` tuần tự hóa socket write, đánh dấu busy và thêm cặp request/run vào FIFO **trước
khi** ghi frame, phát flow event `chat_input`/`chat_send`, và return ngay khi
frame được ghi — reply về trên read loop. Frame outbound:

```json
{"type":"message.send","id":"chat-42","run_id":"device-chat-42-…","payload":{
  "content":"...","attachments":[{"type":"image","url":"data:image/jpeg;base64,..."}]}}
```

Claude tự xử lý tuần tự các input. Adapter giữ mọi cặp request/run đang chờ
thay vì một biến bị ghi đè: success/error chỉ kết thúc lượt hiện tại; callback
idle không xóa busy khi còn lượt đang chạy hoặc đã gửi đang chờ.

Khi WebSocket kết nối lại, adapter gửi tiếp queue cục bộ **chưa gửi**, giữ
nguyên run ID và quy tắc speaker gate. Callback idle lúc offline giữ queue;
nếu socket biến mất trước write, phần chưa gửi được đưa lại vào queue. Lỗi
sau khi đã thử ghi có kết quả giao nhận không chắc chắn nên không tự gửi lại.
Bridge cũng không thử lại stdin write lỗi/thiếu byte; chỉ buffer khi child
chưa có stdin. Disconnect xóa correlation của lượt đã gửi, không replay nó.

Giới hạn: stdout Claude hiện không có request/run tag và adapter dùng FIFO.
Native channel xen vào một lượt đã gửi hoặc mất frame qua reconnect chưa thể
được đối chiếu chắc chắn; không suy diễn rằng đây là parity với bridge Codex
có tag. Khi không có lượt đã gửi đang chờ, lượt native nhận run ID mới.


`sendChat` cũng tái hiện hook `emotion-acknowledge` của OpenClaw **native bằng
Go** (`emotion_ack.go`, mirror codex/hermes/picoclaw): mỗi turn user-visible
bắn `{emotion:"thinking"}` sang HAL — cùng prefix skip, cùng intensity, cùng
capability gate (`skills.SupportedHooks`) như handler TS. Hook `turn-gate` đi
kèm cố ý không mirror (sendChat đã đánh dấu turn busy rồi). ⚠️ Giữ lockstep với
`runtimes/openclaw/hooks/emotion-acknowledge/handler.ts`.

## 5. Event inbound → `domain.WSEvent` (`translator.go`)

Event stream-json của Claude được dịch thành đúng các frame mà handler OpenClaw
tiêu thụ:

| Event Claude | Phát ra |
|---|---|
| `system` (subtype `init`) | — (bắt `session_id`) |
| `assistant` — block `text` | — (cất làm final text dự phòng) |
| `assistant` — block `tool_use` | `lifecycle.start` (một lần) + `tool.start` |
| `user` — block `tool_result` | `tool.end` (text kết quả, match theo `tool_use_id`) |
| `result` subtype `success` | assistant delta (nguyên reply, N=1) + `chat.final` + `lifecycle.end` (+ `usage` theo lượt) — delta là thứ consumer chung tích lũy rồi flush TTS/`tts_send` lúc `lifecycle.end` |
| `result` subtype `error*` / `is_error` | `lifecycle.error` |
| `bridge.error` | `lifecycle.error` |
| `stream_event` / `pong` / `bridge.status` | bỏ qua |

Các gotcha vòng đời turn:

- **Text cuối cùng là `result.result`**, không phải các block text của
  assistant — text assistant trung gian (giữa các tool call) không bao giờ được
  render, chỉ được cất làm fallback phòng thủ.
- `usage` là **theo từng lượt** (shape API Anthropic: `input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`) —
  khác `context_usage` cộng dồn của picoclaw.
- **Turn khởi phát từ bên ngoài nổi lên trên cùng stdout**: nếu có gì đó chạy
  bên trong session Claude mà không có pending runID (ví dụ một channel
  plugin, nếu có bật), `ensureTurnStarted` cấp một runID mới nên turn vẫn hiện
  trong Flow Monitor. Với tất cả kênh do device sở hữu (§7) đây chỉ còn là
  đường phòng thủ — turn telegram/discord/slack đều inject thành lượt
  `sendChat` thường có pending runID.

## 6. Session

Claude sở hữu session: id được bắt từ bất kỳ event nào mang `session_id` và
được bridge persist (`session.json`) cho `--resume`.
`NewSession` gửi `{"type":"session.new"}` (session mới, không resume).
`ShouldRotateSession` rotate theo **turn count (80) hoặc token spike 150k**
(`rotation.go`): auto-compaction của Claude Code giữ được *kích thước*
context nhưng không giữ được persona — sau đủ nhiều chu kỳ compaction, dòng
tên duy nhất trong IDENTITY.md trôi khỏi context nén (quan sát trên device
2026-07-08: agent tự bịa tên), mà `CLAUDE.md` @imports chỉ được đọc lại lúc
session start, nên rotation định kỳ chính là điểm neo lại. Ký ức dài hạn
(MEMORY.md/KNOWLEDGE.md) sống sót qua imports; chỉ mất hội thoại nguyên văn
trong session. `CompactSession` trả `domain.ErrNotSupportedByRuntime` (không
có compact RPC ngoài).

## 7. Kênh — tất cả do device sở hữu (telegram, discord, slack)

`SupportedChannels() = [telegram, slack, discord]`. Cả ba receive loop đều
chạy trong os-server, mirror `runtimes/codex` 1:1. Channel plugin native
telegram/discord của Claude Code **cố ý không dùng**: thực địa cho thấy không
debug được (bun child không log ra journal, allowlist drop im lặng, chết im
lặng khi race restart bridge), và chúng sẽ giành bot với các loop device sở
hữu (Telegram 409 khi có poller song song; Discord sẽ trả lời đúp). presync
xoá state `~/.claude/channels` cũ và install.sh không còn cài bun hay plugin
nào.

- **Telegram** (`telegram_poll.go`): một goroutine khởi động từ `StartWS`
  long-poll `getUpdates` và inject mỗi DM được chấp nhận (chat private, sender
  == `telegram_user_id`) thành một lượt chat thường với flow source
  `telegram`; run được track trong `telegramRuns` và **đánh dấu silent**
  (không TTS), typing keeper bắn `sendChatAction` trong lúc turn chạy, và
  `emitFinal` DM câu trả lời ngược về (marker `[HW:/...]` được strip qua
  `stripForChannel`). Offset persist ở
  `/root/.claudecode/telegram_offset.json`. Creds đọc tươi từ config.json mỗi
  vòng poll — đổi token/user không cần restart.
- **Discord** (`discord.go`): Discord không có API nhận kiểu long-poll, nên
  os-server sở hữu một **session gateway discordgo** (intent DM +
  guild-message + message-content), khởi động từ `StartWS`. Chấp nhận = không
  phải bot, sender == `discord_user_id` (allowlist rỗng = đóng), và hoặc là
  DM hoặc là message trong `discord_guild_id` có **@mention bot** (mention bị
  strip khỏi text). Message được chấp nhận inject thành turn với flow source
  `discord` (track trong `discordRuns`, silent, typing keeper native);
  `emitFinal` post reply ngược về **chunk theo giới hạn 2000 ký tự của
  Discord**. Token đọc tươi mỗi lần (re)connect; khi session đang mở,
  discordgo tự xử lý reconnect gateway.
- `AddChannel`/`RefreshChannelConfig` là no-op success trung thực cho cả ba
  kênh: các loop device sở hữu đọc creds tươi từ config.json mỗi lần dùng,
  nên chỉ cần persist creds là đủ (không presync, không restart bridge).
  Lưu ý discord: token lưu lần đầu được nhặt trong ~30 s, nhưng xoay token khi
  session đang mở thì có hiệu lực ở chu kỳ session kế. whatsapp →
  `domain.ErrChannelNotSupported`.
- **Slack do DEVICE SỞ HỮU** (`slack.go` + `slack_sender.go`, mirror của
  `runtimes/codex/slack.go`): Claude Code không có slack channel plugin
  ("Claude in Slack" là một tính năng cloud riêng, spawn web session từ mention
  `@Claude`, không phải một kênh của thiết bị). Thay vào đó proxy public
  bff-campaign-service nhận delivery từ Slack Events API và fan-out qua MQTT
  tới handler `slack_event` của thiết bị
  (`server/device/delivery/mqtt/slack_event_handler.go`), handler dedup theo
  event_id và type-assert gateway đang active thành `domain.SlackBridge` —
  `ClaudeCodeService` implement interface này, nên event chỉ route vào đây khi
  claudecode đang active. Message được chấp nhận (gate allowlist
  `slack_user_id`; event bot/subtype/user rỗng bị drop) chờ agent rảnh (poll
  500 ms, cap 2 phút) rồi được inject thành một lượt thường với flow source
  `slack`; run được track trong `slackRuns` và **đánh dấu silent** (không TTS),
  đồng thời thêm reaction ack `eyes` lên message của user. Câu trả lời cuối
  (event `result` → `emitFinal`, `translator.go`) được post ngược về đúng
  channel/thread gốc qua `chat.postMessage` (`slack_bot_token`), marker
  `[HW:/...]` + audio tag được strip (`stripForChannel`, `hal.go`); reaction
  ack được xoá khi reply hoặc lỗi. **Không streaming từng phần**
  (`StreamSlackDelta` là no-op — câu trả lời về nguyên khối). Reply thread theo
  thread sẵn có, không có thì thread dưới message của user.
- Outbound `Broadcast`/`SendToUser` (nudge chủ động) đi thẳng tới Telegram Bot
  API (`telegram_sender.go`); `SlackSender` post message chủ động tới kênh
  `slack_user_id` đã cấu hình khi đủ cả hai creds slack. Target store dùng chung
  (`/root/.lumi/telegram_targets.json`) được receive loop populate
  (`upsertTelegramTarget`) trên mỗi DM được chấp nhận; `GetTelegramTargets`
  fallback về owner id đã cấu hình khi store còn rỗng (`telegram.go`).

## 7b. Auth — claude.ai OAuth login (thay thế cho API key)

Thiết bị có thể xác thực bằng **Claude subscription của chính user** thay vì
`llm_api_key`. Flow này (`runtimes/claudecode/login.go`, interface tùy chọn
`domain.ClaudeLoginPairer`) mô phỏng flow pairing WhatsApp — stream các
`PairingEvent` — với thêm một chặng: OAuth code đi ngược trở lại flow.

1. `{"cmd":"claudecode_login"}` trên fa_channel khởi động flow: os-server chạy
   `claude setup-token` dưới một pty (`script -qec` — CLI từ chối chạy khi
   không có TTY) và quét output của nó.
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/…"}` được
   stream trên fd_channel; user mở URL, authorize, và copy code.
3. `{"cmd":"claudecode_login_code","code":"…"}` đưa code cho CLI đang chờ
   (được ghi vào pty với một `\r` raw-mode).
4. Khi thành công, token dài hạn (`sk-ant-oat01-…`) được persist vào
   config.json (`claude_code_oauth_token`), `EnsureOnboarding` chạy lại presync
   (`.env` ở subscription-mode — xem §1), và bridge restart vào subscription
   auth. Fallback: nếu dòng token không được bắt nhưng CLI đã lưu
   `~/.claude/.credentials.json`, cái đó cũng tính là thành công — presync
   chấp nhận một trong hai tín hiệu.

Runtime không phải claudecode trả lời `claudecode_login` bằng một failure
one-shot ("claude login not supported on … backend"). Chi tiết hợp đồng MQTT:
`docs/vi/mqtt_vi.md` §`claudecode_login`. ⚠️ Thứ tự ưu tiên credential là cái bẫy ở
đây: `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` **đứng trên** OAuth token, đó
là lý do presync ở subscription-mode bỏ hẳn chúng.

## 7c. Coding từ xa qua Telegram (`telegram_coding.go`, `coding_sessions.go`)

Tách biệt với lượt persona device-main: một chat Telegram có thể **gắn vào phiên
`claude` interactive của một folder và tiếp tục code từ điện thoại**. Usecase —
ở nhà code trong terminal, ra ngoài thì nhắn Telegram làm tiếp, nhiều folder mỗi
folder một phiên riêng.

- **Khám phá phiên** (`coding_sessions.go`): claude lưu mỗi phiên thành transcript
  JSONL ở `~/.claude/projects/<cwd-mã-hoá>/<uuid>.jsonl` (root ⇒
  `/root/.claude/projects`). `allCodingSessions` quét cây đó; **cwd thật của mỗi
  phiên được đọc từ NỘI DUNG transcript** (trường `cwd` trong các record), KHÔNG
  giải mã từ tên thư mục — cách mã hoá `/`→`-` mất mát với folder có dấu `-`.
  Danh sách hiện **3 prompt người dùng gần nhất** (mới nhất trước, bỏ khối
  `<environment_context>`/`<system-reminder>` tổng hợp qua `isInjectedContext`).
  Phiên **gắn theo cwd**: `claude --resume <uuid>` chỉ tìm thấy phiên khi chạy đúng folder của
  nó (đã kiểm chứng trên device: resume sai cwd trả `No conversation found`).
- **Lệnh** (chặn trong `handleTelegramUpdate` TRƯỚC khi inject device-main):
  `/resume` (giống CLI claude — không tham số thì liệt kê folder, `/resume <số>`
  chọn theo số, `/resume <folder>` chọn phiên mới nhất) · alias `/sessions`
  (liệt kê) + `/use <số|folder>` (chọn) · `/sessions <folder>` (mọi phiên trong 1
  folder) · `/new <folder>` (phiên mới, tạo folder nếu chưa có) · `/here` (đang ở
  phiên nào) · `/device` (về persona device-main) · `/help`. Chat chưa chọn phiên
  và không phải lệnh thì rơi xuống
  device-main như cũ.
- **Mô hình HAND-OFF, KHÔNG đồng-biên-tập.** Mỗi lượt được chấp nhận spawn một
  `claude --print --output-format json [--resume <uuid>]
  --dangerously-skip-permissions` mới với `cmd.Dir` = folder của phiên và prompt
  ở stdin; `result`/`session_id` của reply được parse lại
  (`parseClaudeJSONResult`) và DM về (chunk ở giới hạn 4000 ký tự của Telegram).
  Env exec = env tiến trình + các cặp trong `.env` presync (`ANTHROPIC_*`) +
  `IS_SANDBOX=1` + `HOME=/root` (root cần `IS_SANDBOX` cho
  `--dangerously-skip-permissions`, giống child của gatewayd). uuid thật của phiên
  `/new` được bắt từ lượt đầu tiên.
- **Bảo vệ.** Một **mutex theo folder** tuần tự hoá các lượt để không có 2 lượt
  cùng ghi vào một transcript. Một **lượt quét `/proc`** (`procHoldsFolder`) từ
  chối chạy khi vẫn còn TUI `claude` interactive đang giữ folder đó làm cwd — 2
  writer sẽ làm hỏng transcript, nên mô hình là hand-off (đóng phiên terminal
  trước). Chỉ `telegram_user_id` trong allowlist chạm được (cùng cổng chặn với
  lượt Telegram thường); coding từ xa chạy `--dangerously-skip-permissions` nên
  allowlist chính là ranh giới bảo mật.
- **Trạng thái.** Lựa chọn phiên mỗi chat persist vào
  `/root/.claudecode/telegram_coding.json` (sống qua restart — chat vẫn ở nguyên
  phiên). Chạy thẳng trong os-server (mỗi lượt một tiến trình `claude` con riêng),
  độc lập với child thường trú của gatewayd, nên lượt coding và persona device-main
  không đụng nhau.
- **Phía terminal — picker `claude-sessions` (`cmd/os-server/cc.go`).** Picker
  `/resume` interactive của claude **loại phiên headless (`--print`) theo thiết
  kế** (lọc theo cách phiên được tạo), nên phiên tạo từ Telegram không bao giờ
  hiện trong đó — nhưng `claude --resume <id>` mở được MỌI phiên theo id (đã
  kiểm chứng trên device). Vì vậy device có picker riêng: `claude-sessions`
  (wrapper mỏng `/usr/local/bin/claude-sessions` do presync §5 cài, sudo-reexec
  vào `os-server claude-sessions`) liệt kê mọi phiên của **folder hiện tại**
  (`-a` cho mọi folder, `--json` cho script) qua đúng discovery
  `allCodingSessions` (một nguồn sự thật, export là
  `claudecode.ListCodingSessions`), rồi exec `claude --resume <id>` trong folder
  của phiên với `.env` presync + `IS_SANDBOX=1` + `HOME=/root` merge vào. Reply
  `/resume <n>`·`/here` bên Telegram kèm gợi ý `claude --resume <id>` /
  `claude-sessions` tương ứng. Codex không cần tương đương — picker của `codex
  resume` là global, đã liệt kê sẵn thread tạo từ Telegram.

## 8. Workspace, persona, skills, MCP

- **`CLAUDE.md`** là file memory Claude Code tự nạp; onboarding sở hữu một khối
  OS-managed (giới hạn bằng marker, ghi chú của owner được giữ bên dưới) chứa
  các **@import** persona (`@SOUL.md @IDENTITY.md @USER.md @MEMORY.md
  @KNOWLEDGE.md` — CLAUDE.md là file duy nhất Claude nạp theo tên) và prompt
  discipline (rule whitelist skills, rule connectors, rule memory, ưu tiên
  user), phỏng theo khối AGENTS.md của picoclaw.
- **Skills nằm ở phạm vi USER** (`/root/.claude/skills/`, `claudecodeSkillsDir`),
  không phải phạm vi project. Claude Code phân giải skill *project* theo
  `<cwd>/.claude/skills`, nên nếu chỉ cài trong workspace thì mọi session có cwd
  khác workspace sẽ không thấy — điển hình là **coding session** mà thiết bị tạo
  ở `/root`, `/root/myapp`, … (`coding_sessions.go`). Triệu chứng thực tế: một
  coding session báo Gmail/Calendar "không kết nối" và tự viết `send_email.py`,
  trong khi chat thiết bị (cwd = workspace) vẫn trả lời đúng từ cùng bộ token.
  Skill ở phạm vi user được nạp trong MỌI session, bất kể cwd.
  `migrateSkillsToUserScope()` chuyển các skill mà os-server cũ để lại trong
  workspace sang phạm vi user rồi xoá thư mục cũ (nếu giữ lại, mỗi skill sẽ bị
  đăng ký hai lần). Factory reset xoá hẳn `/root/.claude/skills`.
- **Memory phạm vi user** (`/root/.claude/CLAUDE.md`, `userClaudeMDBlock`):
  `CLAUDE.md` trong workspace chỉ đến được session chat thiết bị, nên các rule
  connector ở mức thiết bị cũng được inject vào đây — Claude Code nạp file này ở
  mọi session, trong mọi thư mục. Cố ý giữ nhỏ: rule persona/memory vẫn thuộc
  phạm vi workspace; chỉ những sự thật phải "sống sót" qua một lệnh `cd` mới nằm ở đây.
- **Rule connectors (trong cả hai khối CLAUDE.md).** Claude Code tự phát hiện
  skill `connectors`, nhưng chỉ phát hiện thôi là chưa đủ: model
  không chọn skill đó, và trả lời "không có Gmail/Calendar nào được kết nối" dựa
  trên `.mcp.json` trong khi token `google_calendar` hợp lệ vẫn nằm trên đĩa. Vì
  vậy khối này nêu thẳng các sự thật về connector — credential nằm ở
  `/root/.openclaw/workspace/configs/<code>_access_tokens.json`, các connector
  dạng token (Gmail/Calendar/Drive) **không** có MCP server nên `.mcp.json`
  không phải danh sách connector, và câu hỏi *về* một dịch vụ đã liên kết không
  phải "chat thường" được miễn dùng skill. Khối cũng phân biệt rõ "my events"
  (Google Calendar) với `/root/local/flow_events_*.jsonl` (event của thiết bị).
- **Khối SOUL**: cùng cơ chế inject device-soul theo `soul_ref` như
  openclaw/picoclaw.
- **Migrate persona** (`migrate_persona/runtime_claudecode.go`): layout **cố ý
  giống hệt OpenClaw** (IDENTITY.md có slot riêng, MEMORY.md ở gốc workspace,
  KNOWLEDGE.md + `memory/*.md` hằng ngày), nên mọi slot map 1:1 và round-trip
  với các runtime có slot là lossless về cấu trúc. Bản thân `CLAUDE.md` là
  đặc thù runtime và không bao giờ được mang theo.
- **Không có HEARTBEAT.md** — Claude Code không có heartbeat loop nào sẽ đọc
  nó; một quyết định bỏ có ý thức, không phải bỏ sót.
- **Skills** nằm trong `/root/.claude/skills/` (native, tự-discover). Mỗi lần
  onboarding đều áp dụng capability gate và đồng bộ toàn bộ catalog được hỗ trợ
  từ CDN, sửa cả file local cũ khi watcher khởi động sau một lần OTA publish.
  `skill_watcher.go` tiếp tục poll metadata OTA mỗi năm phút để bắt các bản
  publish sau đó. Khi nội dung thật sự đổi, bridge được restart rồi agent được
  thông báo qua `SendSystemChatMessage` để đọc lại skill. Download hoặc extract
  ZIP lỗi giữ version ở trạng thái chờ cho poll tiếp theo.
- **MCP là thật** (`mcp.go`): `WriteMCPEntry`/`RemoveMCPEntry` upsert
  `mcpServers` trong `workspace/.mcp.json` (entry canonical `{command,args,env}` /
  `{type,url,headers}` pass through nguyên văn) + restart bridge;
  `MCPReconcile` clone connector khi switch runtime qua
  `claudecode.ReadMCPEntries`.
- **Migrate config LLM** (`migrate_config/runtime_claudecode.go`): đọc/ghi
  `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` trong `/root/.claudecode/.env`.

## 9. Gì là thật vs no-op (`stubs.go`)

Thật (ngoài transport): `SetupAgent`/`EnsureOnboarding` (chạy presync + các
khối workspace + restore skills + self-heal unit, restart theo hash-diff),
`ResetAgent`, `RestartAgent`, `WatchIdentity`/`UpdateIdentityName`,
`StartSkillWatcher`, `WriteMCPEntry`/`RemoveMCPEntry`, `NewSession`,
`GetConfigJSON` (settings.json của workspace — không bao giờ là `.env` chứa
secret), `Version` (probe `claude --version`), và surface
`domain.ClaudeLoginPairer` (`StartClaudeLogin`/`SubmitClaudeLoginCode` — §7b).

No-op kèm lý do: `HasWhatsappSession`/`PairWhatsapp` (Baileys chỉ có ở
OpenClaw), `FetchChatHistory` (`TODO(claudecode-history)` — JSONL session
không có API đọc ổn định), `StartModelSync`/`StartPrimaryModelWatch` (model
cố định qua env). `RefreshModelsConfig`/`UpdatePrimaryModel` (model =
`ANTHROPIC_MODEL`, do presync sở hữu) và `CompactSession` (auto-compaction,
§6) trả `domain.ErrNotSupportedByRuntime` để caller fallback sang
`EnsureOnboarding` — presync của nó áp dụng thay đổi model;
`ShouldRotateSession=false`.

## 10. Factory reset (`reset.go`)

Stop + verify `claudecode.service`, disable nó, rồi wipe **dữ liệu user +
creds**: `workspace/`, `.env`, `session.json`, `~/.claude/projects`,
`~/.claude/channels`, `~/.claude/todos`, `~/.claude/history.jsonl`, và
`~/.claude/.credentials.json` (phần login claude.ai — nửa nằm trong
config.json, `claude_code_oauth_token`, bị wipe cùng config). **Giữ lại**
(phần mềm đã cài): claude CLI, `~/.claude.json` (bản thân bridge nằm trong
binary os-server). Mọi thứ bị wipe đều có đường restore chạy sau reset
(presync/EnsureOnboarding dựng lại env từ config.json được nhập lại; các loop
kênh device sở hữu không cần gì ngoài config; `ensureSkills` tải lại skills;
flow login chạy lại cho subscription auth).

## 11. ⚠️ Cần verify trên thiết bị

- **Format output của `claude setup-token`**: scanner của login match URL
  OAuth, token `sk-ant-oat01-…`, và các marker thành công dạng text — verify
  với build CLI trên thiết bị (flow degrade thành
  `failure (last output: …)` kèm dòng cuối của CLI khi parse trượt). Các lần
  ghi pty dùng `\r` cho Enter; xác nhận prompt dán code chấp nhận nó.
- **Tương thích campaign-api**: Claude Code dùng đầy đủ Anthropic Messages API
  (system prompt, vòng lặp tool_use, streaming) trên `ANTHROPIC_BASE_URL` —
  verify proxy pass được các thứ này qua (hermes đã dùng cùng endpoint ở mode
  `anthropic_messages`).
- **First-run headless**: presync seed các flag trong `~/.claude.json`; nếu CLI
  thêm gate tương tác mới, child của bridge có thể exit ngay khi start — check
  `journalctl -u claudecode` và các dòng stderr của `claude` trong log bridge.


## Nạp trước skill bằng Jev trong bridge được OS quản lý

Bridge runtime Claude Code dùng Jev chọn tối đa một skill đủ điều kiện trước khi chuyển yêu cầu người dùng chỉ có văn bản. Bridge đọc thư mục skill native do OS cài, `$HOME/.claude/skills`, và thêm toàn bộ skill cùng đường dẫn thư mục vào yêu cầu đó. Đây là adapter của runtime, không phải bộ định tuyến intent phần cứng hay plugin được cài vào CLI upstream độc lập. Voice, Web/MQTT chat và hội thoại thông thường trên các kênh nhắn tin do OS quản lý cùng đi qua bridge này. CLI độc lập, chế độ coding-session trên Telegram và tin nhận trực tiếp qua plugin kênh native của Claude chạy ngoài bridge, tiếp tục tìm skill theo cơ chế native. Adapter chỉ áp dụng cho tin nhắn OS chuyển qua `message.send`.

Adapter dùng chung mã `system/lib/jevskills`, giới hạn ba giây và tiếp tục bình thường khi Jev không chọn, thiếu thông tin xác thực, trả kết quả không hợp lệ hoặc không đọc được skill. Tin hệ thống, slash command và attachments bỏ qua bước nạp trước. Yêu cầu đã chuẩn bị dùng lại context khi retry do phiên cũ không còn; không ghi context vào AGENTS.md hay system prompt toàn cục. Runtime upstream có thể giữ lượt này trong lịch sử hội thoại thông thường; nội dung nạp trước ghi rõ chỉ áp dụng cho yêu cầu hiện tại.

**Jev mặc định tắt** cho runtime này trong khi chờ kiểm chứng native. Cờ build `const jevEnabled = false` nằm trong `runtimes/claudecode/gatewayd/jev.go`. Muốn bật sau kiểm chứng, đổi cờ Go rồi build và restart bằng luồng quản lý runtime; biến môi trường không bật được Jev. `JEV_CONFIG_PATH` mặc định là `/root/config/config.json`; thông tin xác thực được đọc từ file, không đưa vào context. Skill có metadata thực thi chưa hỗ trợ bị loại. Cấu hình native sai định dạng, cú pháp JSONC chưa hỗ trợ, biến môi trường đổi thư mục cấu hình và thư mục skill riêng của project cũng khiến adapter bỏ qua nạp trước. Adapter kiểm tra cấu hình ở thư mục cha để giữ chính sách kế thừa và thứ tự ưu tiên skill. Khi cấu hình native có điều khiển skill hoặc quyền tùy chỉnh, adapter nhường cho bộ nạp native thay vì bỏ qua chính sách đó. Việc chọn skill không cấp quyền thực thi hay thay đổi cơ chế phê duyệt tool hiện có.

Kiểm chứng local bao gồm cách ly yêu cầu, bỏ qua nguồn không phải người dùng và attachments, nhường khi có chính sách native, cùng dữ liệu gửi vào tiến trình runtime. Test dùng selector mock và tiến trình giả; Jev thật, hành vi model native và delivery trên thiết bị cần kiểm thử tích hợp riêng.
