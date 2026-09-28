# Backend agent OpenCode

OpenCode là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Xem `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: OpenAI Codex CLI sau một WS bridge cục bộ. Xem `docs/agentic/codex.md` + `runtimes/codex`.
- **`claudecode`**: Claude Code CLI sau một WS bridge cục bộ. Xem `docs/agentic/claudecode.md` + `runtimes/claudecode`.
- **`opencode`**: **[opencode](https://opencode.ai) CLI** (agent coding AI mã nguồn mở) làm bộ não thiết bị, sau một WS bridge cục bộ. Tài liệu này. Code: `runtimes/opencode/`.

> Code là nguồn chân lý. Tài liệu này mô tả `runtimes/opencode/` đúng như đã triển
> khai; giữ đồng bộ khi thay đổi (EN: `docs/agentic/opencode.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · [`hermes_vi.md`](hermes_vi.md) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) · [`codex_vi.md`](codex_vi.md) ·
> [`claudecode_vi.md`](claudecode_vi.md) · file này (OpenCode).
>
> **Trạng thái: đã verify trên thiết bị** (2026-07-23, `intern-v2` trên opencode
> 1.18.4). Flow switch chạy end-to-end — install → presync → gatewayd → `opencode
> run` theo từng turn → reply được gửi. Đợt shakeout trên thiết bị đã sửa bốn giả
> định từ bản build ban đầu (giờ đều đã fix trong code + tài liệu này): dir
> override của installer, đường nối campaign-api (Responses API, không phải chat
> completions), cờ permission (`--auto`), và event kết thúc (step_finish, không có
> session.idle). Xem §10.

## 1. Tổng quan & chọn ra sao

opencode CLI được điều khiển **theo từng turn** (như codex), nên thiết bị chạy
một **WS bridge** mỏng cục bộ: unit systemd `opencode.service` chạy **`os-server
opencode-gatewayd`** — bridge được **compile thẳng vào binary os-server**
(`runtimes/opencode/gatewayd`; không có tiến trình riêng phải materialize, không
có Python). Bridge mở `ws://127.0.0.1:18793/opencode/ws/` (bearer token
`autonomous_opencode_token`) và spawn **một subprocess mỗi turn**:

```
opencode run --format json --auto --dir /root/.opencode/workspace [--session <id>] [--file <img>…] <prompt>
```

resume theo session id lưu trong `/root/.opencode/session.json` (`--session
<id>` — một cờ thường, không phải subcommand, nên không có bẫy thứ-tự-cờ kiểu
codex). Turn được serialize nghiêm ngặt (queue có buffer + một worker). Cờ
permissive là cố ý: appliance chạy root không bao giờ được block ở prompt
approval — `--auto` tự động approve các permission không bị deny tường minh (cờ
đã ship của bản 1.18.4; `--dangerously-skip-permissions` của nhánh dev không có
trong các bản release). Model/provider đến từ `opencode.json` (do presync sở hữu,
§1.2) — bridge không bao giờ truyền `--model`.

`agent_runtime` trong `config.json` chọn backend; việc phân giải nằm ở
`system/agent/factory.go` `ProvideGateway()` — `"opencode"` →
`opencode.ProvideService`, giá trị lạ rơi về OpenClaw. Khi khởi động, banner
`AGENT BACKEND ACTIVE → OPENCODE` in `ws_url` + `conversation`.

Hằng số kết nối (`runtimes/opencode/constants.go`, không có config theo máy):

| Hằng | Mặc định | Ý nghĩa |
|---|---|---|
| `WSURL` | `ws://127.0.0.1:18793/opencode/ws/` | Endpoint WebSocket của bridge cục bộ |
| `Token` | `autonomous_opencode_token` | Bearer token khi connect; bridge đọc cùng giá trị từ `/root/.opencode/.env` (`OPENCODE_WS_TOKEN`, do presync sở hữu) |
| `Conversation` | `device-main` | Chỉ là nhãn — opencode sở hữu session id của nó (§4) |

Bố cục state: state device-local của bridge nằm dưới `/root/.opencode/`
(`.env`, `session.json`, `workspace/`, `attachments/`, `install.log`);
config/data riêng của opencode CLI nằm dưới **XDG** — `~/.config/opencode/`
(`opencode.json`, `AGENTS.md`, `skills/`) và `~/.local/share/opencode/`
(`auth.json`, sessions), với `HOME=/root`.

## 1.1 Cài đặt (`install.sh`)

Một lần switch `opencode.setup` chạy `system/device/switch_runtime.sh`
(generic), script này materialize các script nhúng của OpenCode. `install.sh`
(một lần, tự-đủ — chạy thẳng `bash install.sh` cũng cấu hình VÀ khởi động
backend đầy đủ):

1. prerequisites `jq` + `curl` + `tar`;
2. cài opencode CLI qua **installer chính thức đã pin**
   (`curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin
   bash -s -- --version <OPENCODE_VERSION>`) — nó tự phát hiện kiến trúc (linux
   arm64/x64), lo asset `.tar.gz` + giải nén, và idempotent. Hai cái bẫy học được
   trên thiết bị đã được xử lý: env var **phải đứng trước `bash`** (trong `VAR=x
   curl | bash` nó bind vào curl, không phải bash được pipe), và trên thiết bị
   test installer **vẫn** đặt binary vào mặc định `~/.opencode/bin` của nó — nên
   một bước belt-and-suspenders copy bất kỳ thứ gì installer tạo ra vào
   `/usr/local/bin/opencode` (đường dẫn mà unit + hook `verify` dùng).
   `OPENCODE_VERSION` được pin (hiện tại `1.18.4`) — chỉ là baseline cho image
   mới flash: máy đã ngoài thực địa update qua `make upload-opencode
   <semver-trần>` + `make promote-opencode`, bootstrap worker áp dụng bằng
   `software-update opencode` (`docs/vi/bootstrap-ota.md` §5);
3. chạy hook presync một lần (`/usr/local/bin/runtime-opencode-presync`, được
   os-server materialize TRƯỚC installer — §1.2);
4. ghi + enable **`opencode.service`** (`ExecStart=/usr/local/bin/os-server
   opencode-gatewayd`, `EnvironmentFile=/root/.opencode/.env`, `HOME=/root`,
   `Restart=always`) — bridge không cần materialize gì, nó nằm sẵn trong
   os-server; rồi drop hook `verify` rẻ + offline (`command -v opencode` + binary
   os-server tồn tại) để switch-runtime tự-heal.

Tên unit == tên runtime (`opencode.service`), nên không cần file khai báo
`os-runtimes/opencode/service`. Log install ghi vào `/root/.opencode/install.log`
(rootfs bền — `/var/log` là zram volatile trên các board này).

## 1.2 Presync (`presync.sh`) — nhúng, chạy mỗi lần switch + mỗi lần boot

`presync.sh` được nhúng trong os-server và materialize ra
`/usr/local/bin/runtime-opencode-presync`. Nó chạy trước mỗi lần opencode start
(switch-runtime), một lần cuối install, **và mỗi lần os-server boot /
config-change qua `EnsureOnboarding`** (pattern hermes): `EnsureOnboarding` hash
các file presync sở hữu (`opencode.json` + `.env`) quanh lần chạy và chỉ restart
gateway khi có thay đổi thật. Nó sở hữu mọi thứ stateful:

- **§1 MIGRATE** — copy persona/memory/skills một lần từ workspace openclaw,
  chốt bằng marker `/root/.opencode/.openclaw-migrated`. Stop openclaw trước
  (retry 3 lần, non-fatal), rồi copy nguyên văn `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **và `AGENTS.md`**
  (opencode đọc `AGENTS.md` natively — slot persona không cần dịch; Go onboarding
  vẫn inject lại khối OS), cộng `memory/` vào workspace và `skills/` vào
  **`~/.config/opencode/skills`** (thư mục discovery toàn cục của opencode) chỉ
  khi chưa có. Marker chỉ được ghi sau một lần copy sạch, nên migrate lỗi sẽ thử
  lại lần sau; factory reset xoá `/root/.opencode` sẽ xoá marker nên migrate chạy
  lại ở lần switch kế.
- **§2 CONFIG** — regenerate `~/.config/opencode/opencode.json` từ config.json
  qua `jq`. Nó ghi `model` top-level = `campaign/<llm_model>` (fallback
  `Auto-AI`) và một **custom provider** `provider.campaign` dùng npm adapter
  **`@ai-sdk/openai`** với `options.baseURL` từ `llm_base_url` (fallback
  `https://campaign-api.autonomous.ai/api/v1/ai/v1`) và `options.apiKey` =
  **tham chiếu** `"{env:LLM_API_KEY}"` (resolve từ `.env` lúc launch — key thật
  không bao giờ vào JSON). **Dùng `@ai-sdk/openai` (không phải
  `@ai-sdk/openai-compatible`) vì campaign-api nói OpenAI Responses API, không
  phải chat completions** — đã verify trên thiết bị: `{base}/chat/completions`
  trả 404, `{base}/responses` chạy được, và opencode route tới Responses API qua
  `@ai-sdk/openai` (theo docs provider của opencode). Object `"mcp"` sẵn có được
  **giữ nguyên văn** — `mcp.go` của os-server sở hữu các entry đó (§7), nên hai
  chủ sở hữu không giẫm nhau.
- **§3 ENV** — ghi `/root/.opencode/.env` (systemd EnvironmentFile, mode 0600):
  `OPENCODE_WS_TOKEN` (phải khớp `constants.go` `Token`), `OPENCODE_PORT=18793`,
  `OPENCODE_WORKSPACE=/root/.opencode/workspace`, và `LLM_API_KEY` từ
  `llm_api_key`.

Presync cũng ghi `/etc/profile.d/agent-cli-env.sh` (login shell tương tác source
`.env` của runtime đang active, nên một lệnh `opencode` trần trong SSH/web-CLI
shell tái dùng campaign key — resolve live từ `config.json` nên luôn đúng qua các
lần switch).

Trên nền lần chạy presync, `EnsureOnboarding` (`onboarding.go`) làm cùng phần
reconcile workspace như các backend khác: seed `KNOWLEDGE.md` từ template nhúng
chỉ khi chưa có, inject các khối OS-managed `<!-- OS DO NOT REMOVE -->` vào
`SOUL.md` / `AGENTS.md` / `HEARTBEAT.md`, refresh khối user AGENTS.md **toàn
cục** (`~/.config/opencode/AGENTS.md`), và capability-gate skills. Thay đổi
chỉ-markdown không bao giờ restart gateway — mỗi `opencode run` đọc lại
workspace; chỉ presync đổi config hoặc self-heal unit mới restart.

**Khối persona inline (AGENTS.md).** opencode tự nạp `AGENTS.md` vào context
(project `AGENTS.md` trong workspace `--dir` + `~/.config/opencode/AGENTS.md`
toàn cục). Giống codex, persona được inline THẲNG VÀO `AGENTS.md` của workspace
qua một khối OS idempotent (dựng từ `SOUL.md` + `IDENTITY.md`), dựng lại mỗi lần
`EnsureOnboarding` và ngay sau khi đổi tên (`UpdateIdentityName`) nên turn kế
tiếp thấy tên mới luôn.

### Skills — discovery toàn cục `~/.config/opencode/skills`

Skills của thiết bị nằm ở **`~/.config/opencode/skills/<name>/SKILL.md`** — thư
mục discovery toàn cục của opencode (nó cũng tôn trọng `.opencode/skills/` trong
thư mục project và `~/.claude/skills/` để tương thích Claude). Mọi nơi tạo skill
đều trỏ tới đường dẫn XDG đó: `presync.sh` §1 (migrate từ openclaw),
`skill_watcher.go` (tải CDN + thông điệp notify khi skill đổi), và
`pruneUnsupportedSkills` (capability gate). Factory reset xoá `~/.config/opencode`,
nên bộ skills được migrate lại từ openclaw ở lần `EnsureOnboarding` kế tiếp.
`EnsureOnboarding` cũng tải lại mọi skill được hỗ trợ từ CDN khi boot hoặc reconcile
config, tự phục hồi skill local đã cũ trước khi watcher chạy. Nó gửi thông báo skill
đổi sau khi gateway có thể đã restart.
Watcher log mỗi lần poll metadata thành công là `skill watcher: checked`; nếu tải ZIP
hoặc extract lỗi, version của skill đó vẫn pending để thử lại ở poll kế tiếp.

## 2. Transport & gửi một turn

`client.go` giữ một WebSocket bền tới bridge (khuôn picoclaw: bearer token,
không có handshake pairing, keepalive ping 25s → `pong`, reconnect có backoff,
LED `StateAgentDown` khi rớt). `chat.go` `sendChat` ghi một frame rồi return;
reply về trên read loop:

```json
{ "type": "message.send", "id": "<reqID>", "run_id": "<runID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

Bridge lưu attachment vào `/root/.opencode/attachments` rồi truyền qua
`opencode run --file <path>`. Frame `{"type":"session.new"}` làm bridge bỏ session
id đã lưu (§4). Bridge chạy turn tuần tự và thêm `request_id`/`run_id` vào
frame của turn (kể cả retry resume và lỗi). Adapter giữ FIFO các cặp đang chờ,
ưu tiên tag khi nhận frame; bridge cũ không có tag dùng FIFO. Frame đã kết thúc
hoặc xen sai turn bị bỏ qua trước khi cập nhật session.

Queue đầy trả `bridge.rejected` gắn đúng request/run; chỉ lượt bị từ chối kết
thúc, không xóa busy/lượt hiện tại. Terminal không làm idle khi còn lượt đã gửi
đang chờ. Reconnect tự drain queue cục bộ **chưa gửi**, giữ run ID và speaker
gate. Callback offline giữ queue; socket mất trước write đưa phần chưa gửi
lại queue. Write đã thử nhưng lỗi có kết quả không chắc chắn, không tự replay.
Correlation đã gửi được xóa khi disconnect, không phải nguồn gửi lại.


## 3. Dịch event (`translator.go`)

Bridge forward các event JSONL của `opencode run --format json`, thêm correlation vào frame của turn
(cộng các frame riêng của nó `bridge.status` / `bridge.error` / `pong`); mỗi dòng
opencode mang một `sessionID`. Translator Go map chúng sang đúng khuôn
`domain.WSEvent` mà handler OpenClaw tiêu thụ:

| Event inbound | `domain.WSEvent` phát ra |
|---|---|
| dòng đầu mang `sessionID` | bắt session key |
| `step_start` | `agent` lifecycle `phase:start` (một lần mỗi turn) |
| `text` | **giữ làm câu trả lời** (khuôn thiết bị: `part.text`; `text` phẳng được chấp nhận làm fallback); không có stream token delta. Part mới đẩy part trước xuống `stream:thinking` (xem *Preamble* bên dưới) |
| `reasoning` | *(bỏ qua — suy nghĩ, không phải nội dung)* |
| `tool_use` | cặp `agent` tool `phase:start` + `phase:end` |
| `step_finish` / `message.updated` | bắt token usage theo turn (`part.tokens` / `info.tokens`) |
| `session.idle` (do gatewayd synthesize khi exit sạch) | `agent` `stream:assistant` (nguyên câu trả lời trong **một** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` kèm usage — kết thúc turn |
| `session.error` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — kết thúc turn |
| `bridge.rejected` | `lifecycle.error` chỉ cho request/run bị từ chối; giữ lượt hiện tại |
| `bridge.status` / `pong` | *(log / bỏ qua)* |

**Event kết thúc (đã verify trên thiết bị 1.18.4).** `opencode run --format json`
**không** phát `session.idle`/`turn.completed` — một turn kết thúc bằng một
`step_finish` có `part.reason == "stop"`, rồi process thoát. Vì `opencode run` là
subprocess theo từng turn, một **exit sạch (rc=0) chính là ranh giới turn**:
gatewayd (`turn.go`) đánh dấu turn đã kết thúc và **synthesize một frame
`{"type":"session.idle"}`** để translator finalize đúng một lần. Text `text` đang
giữ được đưa ra ở đó dưới dạng một assistant delta duy nhất **trước**
`chat.final` / `lifecycle.end` — trường hợp N=1 của hợp đồng streaming, chính nó
cho consumer chung flush TTS + marker phần cứng `[HW:/…]` tại `lifecycle.end`.

**Preamble.** opencode tự thuật trước khi gọi tool, thành một part `text` riêng
("Using the sensing skill for this presence event."). Gộp hết các part lại là
đọc cả chuỗi tự thuật đó ra loa — đúng lỗi đã fix ở codex (xem
[codex_vi.md](codex_vi.md)). Nên chỉ part `text` **cuối cùng** của turn mới là
câu trả lời: mỗi part trước đó bị đẩy xuống `stream:thinking` (chỉ vào Flow
Monitor, không bao giờ ra TTS hay reply kênh) ngay khi có part mới hơn chứng
minh nó không phải câu trả lời. Ngoại lệ: part không phải cuối mà mang marker
`[HW:/…]` là hành động phần cứng thật nên vẫn giữ trong reply. Chỉnh prompt
không chặn preamble một cách đáng tin — đây mới là chỗ cưỡng chế.

**Usage:** số token đi theo `step_finish` dưới
`part.tokens.{input,output,cache.read}` (cũng đọc từ `message.updated`
`info.tokens` khi có). Translator giữ lại số mới nhất (`captureUsage` →
`lastUsage`) và đọc chúng tại `session.idle` được synthesize. opencode báo theo
kiểu Anthropic — `input` KHÔNG gồm phần cached — nên các trường giữ tách riêng:
`input → InputTokens`, `cache.read → CacheReadTokens`,
`cache.write → CacheWriteTokens`, `output → OutputTokens`,
`TotalTokens = input + cache.read + output`. Gộp cache read vào `InputTokens`
(cách map cũ) làm một turn trúng cache đọc như thể đã gửi lại toàn bộ context,
và giấu mất số `R` trên thẻ turn.

## 4. Session

opencode sở hữu session: `sessionID` có mặt trên mọi dòng JSONL, được bridge bắt
và lưu trong `/root/.opencode/session.json`, rồi replay qua
`opencode run --session <id>` (lịch sử nằm dưới `~/.local/share/opencode/` —
process thoát ≠ mất session). Một run resume mà session không còn tồn tại thì
được thử lại fresh (`resumeErrHints` của bridge bắt lỗi missing-session).
`NewSession` gửi frame `session.new` → bridge bỏ session id → turn kế tiếp là
fresh.

`ShouldRotateSession` là **lưới an toàn 150k token** cho session chạy hoang. Theo
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4 "No fake success",
`CompactSession`, `UpdatePrimaryModel`, và `RefreshModelsConfig` trả
`domain.ErrNotSupportedByRuntime` — không bao giờ `nil` (thay đổi llm vẫn được áp
dụng live: caller rơi về `EnsureOnboarding`, presync của nó đọc lại `llm_*` và
hash gate restart gateway). Khác codex, **`GetConfigJSON` làm việc thật**: config
của opencode LÀ JSON, nên nó trả `~/.config/opencode/opencode.json` nguyên văn
(an toàn — apiKey của provider là tham chiếu `{env:LLM_API_KEY}`; secret thật chỉ
nằm trong `.env`).

## 5. Kênh

Telegram, Slack và Discord đều **do device sở hữu** dưới OpenCode — y hệt codex
(`SupportedChannels()` → `["telegram", "slack", "discord"]`). os-server tự chạy
các receive loop, driven bởi token trong `config.json` đọc tươi mỗi lần dùng, nên
không có gì phía runtime để ghi và không cần restart. Toàn bộ hành vi (receive
loop, prefix metadata người gửi, tracking silent-run, làm sạch `stripForChannel`,
typing keeper, fan-out reply tại `session.idle`) mirror
[`codex_vi.md` §5](codex_vi.md) 1:1 — xem
`runtimes/opencode/{telegram_poll,slack,discord}.go`. `AddChannel` /
`RefreshChannelConfig` là no-op success trung thực cho các kênh được hỗ trợ và
trả `domain.ErrChannelNotSupported` cho mọi thứ khác (whatsapp).

### Coding từ xa qua Telegram (`telegram_coding.go`, `coding_sessions.go`)

Một chat Telegram có thể khởi động một turn coding `opencode` phạm-vi-folder và
tiếp tục nó từ điện thoại, tách biệt với turn persona device-main. Mỗi turn được
chấp nhận spawn một `opencode run --format json --auto
--dir <folder> [--session <id>] <prompt>` mới thẳng trong os-server (độc lập với
child thường trú của gatewayd); reply được parse từ JSONL của opencode
(`parseOpenCodeResult`: `sessionID` → id, `text` → reply, `session.idle` → xong)
và DM chunk ở giới hạn 4000 ký tự của Telegram. Env exec khẳng định `HOME=/root`
+ các cặp `.env` presync (**không có** `OPENCODE_HOME` — opencode dùng XDG dưới
HOME), nên coding child resolve đúng `opencode.json` + auth mà gatewayd dùng.
Mutex theo folder tuần tự hoá các turn.

> ⚠️ **Khám phá session xuyên-folder bị degrade có chủ ý** trong lần này. codex
> liệt kê thread resume được bằng cách parse store JSONL "rollout" trên đĩa;
> opencode lưu session nội bộ dưới `~/.local/share/opencode/` và
> `opencode session list` chưa được xác nhận là có expose thư mục làm việc cần để
> resume in-folder xuyên mọi project. Nên `allCodingSessions()` trả rỗng (một
> `TODO(opencode-coding-sessions)` trong `coding_sessions.go`): `/new <folder>`
> và resume `--session` theo turn hoạt động, nhưng **danh sách** `/resume` /
> `/sessions` không hiện gì cho tới khi nối với một `opencode session list --json`
> đã verify (hoặc đọc trực tiếp session store) trên thiết bị (§10). Chỉ
> `telegram_user_id` allowlist chạm được tới bất kỳ phần nào của cái này; run là
> unsandboxed, nên allowlist là ranh giới bảo mật.

## 6. Hooks

opencode không ship hooks loader, nên hook `emotion-acknowledge` của OpenClaw
được tái hiện **native bằng Go** (`runtimes/opencode/emotion_ack.go`, mirror
codex/hermes): mỗi turn user-visible, `sendChat` bắn `{emotion:"thinking"}` sang
HAL — cùng prefix skip, cùng intensity, cùng capability gate
(`skills.SupportedHooks`) như handler TS. Hook `turn-gate` đi kèm cố ý không
mirror (`sendChat` đã đánh dấu turn busy rồi). ⚠️ Giữ lockstep với
`runtimes/openclaw/hooks/emotion-acknowledge/handler.ts` và các `emotion_ack.go`
anh em trong hermes/picoclaw/codex/claudecode.

## 7. Connector MCP (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (flow MQTT `connector.set`) sửa object `"mcp"`
top-level của `~/.config/opencode/opencode.json` qua `encoding/json`, ghi atomic
(temp + rename) dưới `mcpMu`, rồi restart gateway để lần `opencode run` kế nhận
server mới. Dịch shape từ entry canonical khuôn OpenClaw: một entry **http** →
`{type:"remote", url, headers, enabled:true}`; một entry **stdio** →
`{type:"local", command:[cmd, args…], environment:env, enabled:true}` (opencode
muốn một mảng `command` gộp duy nhất và đặt tên map env là `environment`).
`RemoveMCPEntry` idempotent (`removed=false`, không restart, khi entry vắng).
presync chỉ regenerate phần đầu provider/model và giữ nguyên object `"mcp"`
(§1.2), nên các entry sống sót qua mọi lần sync. Một lần switch **VÀO** opencode
cũng clone các MCP server của runtime trước qua `MCPReconcile` (đường ghi).

## 8. Factory reset (`reset.go`)

`ResetAgent` (do `server/system/factoryreset.go` gọi trên gateway đang active)
không giữ lại gì — opencode.json/.env được presync regenerate ở lần switch kế:
**stop** `opencode.service` (+ verify inactive, poll 5s), **disable** nó (reboot
mặc định về openclaw), **xoá** thư mục state của bridge `/root/.opencode` **và**
các thư mục XDG của opencode `~/.config/opencode` (opencode.json, AGENTS.md,
skills/) + `~/.local/share/opencode` (auth.json, sessions) và marker
`.openclaw-migrated` (để presync §1 re-migrate ở lần switch kế) — rồi tạo lại các
thư mục baseline `workspace/` + `attachments/` (CLI tự tạo lại state XDG của nó ở
lần chạy đầu). `/root/config/agent_state.json` được xoá khoá-bước với
`config.json` bởi platform reset (theo `adding-agent-runtime_vi.md` §7).

## 9. Migration & nối nền tảng

- **Persona/memory** (`system/agent/migrate_persona/runtime_opencode.go`): một
  adapter read + một write trên workspace opencode, layout y hệt OpenClaw
  (presync seed nó như bản copy nguyên văn). Đăng ký trong map `adapters`, nên
  opencode migrate được cả 2 chiều với mọi runtime khác. SOUL → SOUL.md, identity
  → IDENTITY.md riêng của nó, MEMORY + daily + KNOWLEDGE + USER về slot native của
  chúng; `Overwrite=true` cho SOUL. `rebrandToOpenCode` map tên brand của runtime
  khác sang OpenCode, và `reOpenCode` được các hàm rebrand openclaw/hermes/picoclaw
  tiêu thụ cho chiều ngược.
- **LLM config** (`system/agent/migrate_config/runtime_opencode.go`): đọc/ghi
  `provider.campaign.options.baseURL` trong opencode.json + `LLM_API_KEY` trong
  `.env`, mirror adapter codex.
- **Version uplink**: `opencode --version` được probe lúc startup
  (`runtime.go` → `GetOpenCodeVersion`) và báo cáo dưới dạng `opencode_version`
  trên message MQTT `info` (`domain.MQTTInfoResponse`). Cache là một
  `versioncache.Cache`: `GetOpenCodeVersion` probe lại mỗi khi size/mtime của
  binary đổi, nên update áp trong lúc os-server đang chạy hiện ra ngay mà không
  cần restart os-server.
- **Trigger switch**: MQTT `opencode.setup` (`KindOpenCodeSetup`), HTTP
  `POST /api/device/agent-runtime`, và dropdown **Runtime** ở web Settings
  (`AgentRuntimeSection.tsx`). `domain.AgentRuntimes` bao gồm `opencode`, nên các
  đường switch/validate generic chấp nhận nó mà không cần code theo-runtime.
- **Logs**: các tab log "openclaw"/"openclaw-service" của Flow Monitor resolve về
  `journal:opencode.service` khi opencode đang active (`server/logs.go`).

## 10. Shakeout trên thiết bị (2026-07-23, `intern-v2`, opencode 1.18.4)

Đã verify chạy end-to-end; bốn fix ra đời từ đó (đều đã landed):

1. ✅ **Dir của installer** — installer chính thức bỏ qua `OPENCODE_INSTALL_DIR`
   (thả binary vào `~/.opencode/bin`); env var phải đứng trước `bash` và một bước
   fallback copy binary vào `/usr/local/bin/opencode` (§1.1).
2. ✅ **Cờ permission** — cờ `opencode run` đã ship là `--auto`, không phải
   `--dangerously-skip-permissions` của nhánh dev.
3. ✅ **Đường nối provider** — campaign-api serve **Responses API** (`/responses`),
   không phải chat completions (`/chat/completions` trả 404) → provider là
   `@ai-sdk/openai` (§1.2).
4. ✅ **Event kết thúc** — `opencode run` phát `text` (`part.text`) rồi
   `step_finish` (`part.reason:"stop"`, `part.tokens`) và thoát; không có
   `session.idle` từ CLI → gatewayd synthesize một cái khi exit sạch để translator
   finalize reply (§3).

Còn mở:
- **`OPENCODE_VERSION`** được pin về `1.18.4` — bump khi các release
  `anomalyco/opencode` tiến lên.
- **Khám phá coding-session** bị degrade (§5) — nối nó với một
  `opencode session list --json` đã verify (hoặc đọc trực tiếp session-store) có
  expose thư mục làm việc, rồi bỏ `TODO(opencode-coding-sessions)`.
- **Lưu ý deploy:** gatewayd chạy như một process **riêng**
  (`os-server opencode-gatewayd` dưới `opencode.service`); một bản update chỉ-binary
  cũng cần `systemctl restart opencode.service` — restart mình `os-server.service`
  sẽ để gatewayd cũ chạy tiếp (một OTA cũng bump config presync sẽ tự động trigger
  restart gateway có hash-gate).


## Nạp trước skill bằng Jev trong bridge được OS quản lý

Bridge runtime OpenCode dùng Jev chọn tối đa một skill đủ điều kiện trước khi chuyển yêu cầu người dùng chỉ có văn bản. Bridge đọc thư mục skill native do OS cài, `$HOME/.config/opencode/skills`, và thêm toàn bộ skill cùng đường dẫn thư mục vào yêu cầu đó. Đây là adapter của runtime, không phải bộ định tuyến intent phần cứng hay plugin được cài vào CLI upstream độc lập. Voice, Web/MQTT chat và hội thoại thông thường trên các kênh nhắn tin do OS quản lý cùng đi qua bridge này. CLI độc lập và chế độ coding-session trên Telegram chạy ngoài bridge, tiếp tục tìm skill theo cơ chế native.

Adapter dùng chung mã `system/lib/jevskills`, giới hạn ba giây và tiếp tục bình thường khi Jev không chọn, thiếu thông tin xác thực, trả kết quả không hợp lệ hoặc không đọc được skill. Tin hệ thống, slash command và attachments bỏ qua bước nạp trước. Khi chạy CLI, nếu toàn bộ preload cộng yêu cầu đạt giới hạn 128 KiB cho một đối số trên Linux, bridge bỏ preload, log `reason=argument_budget` rồi gửi yêu cầu gốc; không cắt bớt skill. Yêu cầu đã chuẩn bị dùng lại context khi retry do phiên cũ không còn; không ghi context vào AGENTS.md hay system prompt toàn cục. Runtime upstream có thể giữ lượt này trong lịch sử hội thoại thông thường; nội dung nạp trước ghi rõ chỉ áp dụng cho yêu cầu hiện tại.

**Jev mặc định tắt** cho runtime này trong khi chờ kiểm chứng native. Cờ build `const jevEnabled = false` nằm trong `runtimes/opencode/gatewayd/jev.go`. Muốn bật sau kiểm chứng, đổi cờ Go rồi build và restart bằng luồng quản lý runtime; biến môi trường không bật được Jev. `JEV_CONFIG_PATH` mặc định là `/root/config/config.json`; thông tin xác thực được đọc từ file, không đưa vào context. Skill có metadata thực thi chưa hỗ trợ bị loại. Cấu hình native sai định dạng, cú pháp JSONC chưa hỗ trợ, biến môi trường đổi thư mục cấu hình và thư mục skill riêng của project cũng khiến adapter bỏ qua nạp trước. Adapter kiểm tra cấu hình ở thư mục cha để giữ chính sách kế thừa và thứ tự ưu tiên skill. Khi cấu hình native có điều khiển skill hoặc quyền tùy chỉnh, adapter nhường cho bộ nạp native thay vì bỏ qua chính sách đó. Việc chọn skill không cấp quyền thực thi hay thay đổi cơ chế phê duyệt tool hiện có.

Kiểm chứng local bao gồm cách ly yêu cầu, bỏ qua nguồn không phải người dùng và attachments, nhường khi có chính sách native, cùng dữ liệu gửi vào tiến trình runtime. Test dùng selector mock và tiến trình giả; Jev thật, hành vi model native và delivery trên thiết bị cần kiểm thử tích hợp riêng.
