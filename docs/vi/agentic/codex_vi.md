# Backend agent Codex

Codex là một trong các **backend agentic có thể hoán đổi** mà os-server chạy
phía sau agent gateway. Bộ não có thể cắm rời (CLAUDE.md): os-server nói chuyện
với backend mà `config.agent_runtime` chọn thông qua một interface duy nhất
`domain.AgentGateway`, nên phần còn lại của pipeline (HAL TTS, marker phần cứng
`[HW:/…]`, Flow Monitor SSE, drain sensing, fan-out Telegram) không cần biết bộ
não nào đang chạy.

- **`openclaw`** (mặc định): WebSocket bền tới daemon OpenClaw. Xem `docs/os-server.md` + `runtimes/openclaw`.
- **`hermes`**: client HTTP + SSE tới Hermes API server cục bộ. Xem `docs/agentic/hermes.md` + `runtimes/hermes`.
- **`picoclaw`**: client WebSocket bền tới runtime PicoClaw cục bộ. Xem `docs/agentic/picoclaw.md` + `runtimes/picoclaw`.
- **`codex`**: **OpenAI Codex CLI** làm bộ não agent của thiết bị, sau một WS bridge cục bộ. Tài liệu này. Code: `runtimes/codex/`.

> Code là nguồn chân lý. Tài liệu này mô tả `runtimes/codex/` đúng như đã triển
> khai; giữ đồng bộ khi thay đổi (EN: `docs/agentic/codex.md`, VI: file này).

> **Nhóm docs agentic-backend:** [`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md)
> (hợp đồng generic + cách thêm) · [`hermes_vi.md`](hermes_vi.md) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) · file này (Codex).
>
> **Trạng thái: đã build xong, CHƯA verify trên thiết bị.** Toàn bộ stack
> (install, presync, gatewayd, translator, MCP, reset) compile và có unit-test,
> nhưng chưa có thiết bị nào chạy flow switch end-to-end. Hai điểm phải kiểm
> tra trên thiết bị được đánh dấu sẵn trong code: gatewayd phải listen ở
> `127.0.0.1:18792` với token trong `constants.go`, và ⚠️ campaign-api phải
> serve `{base}/responses` (Codex chỉ nói Responses API — xem §1.2).

## 1. Tổng quan & chọn ra sao

Thiết bị chạy một **WS bridge** mỏng cục bộ: unit systemd `codex.service` chạy
**`os-server codex-gatewayd`** — bridge được **compile thẳng vào binary
os-server** (`runtimes/codex/gatewayd`; **không có Python trên thiết bị**).
Gatewayd sở hữu một tiến trình Codex App Server JSON-RPC thường trú. Nó bắt đầu
turn bằng `turn/start` và gửi input bổ sung tương thích vào turn active bằng
`turn/steer`, thay vì tuần tự hoá input trực tiếp của user qua worker `codex
exec` theo từng turn.

Bridge mở `ws://127.0.0.1:18792/codex/ws/` (bearer token
`autonomous_codex_token`) cho os-server. Cấu hình permissive là có chủ ý:
appliance chạy root không bao giờ được block ở prompt approval
(`approval_policy = "never"` + `sandbox_mode = "danger-full-access"` trong
config.toml, §1.2).

**Phạm vi steering.** `turn/steer` chỉ hợp lệ cho thread Codex hiện đang có
turn active. Khi turn đã terminal, input kế tiếp đi theo đường start hoặc
resume bình thường. Điều này bỏ độ trễ FIFO do runtime tạo ra cho hội thoại
đang chạy, nhưng không vượt qua chính sách an toàn OS: sensing thụ động, cổng
an toàn speaker/TTS, giao nhận offline và input không thể ghép an toàn vào
thread active vẫn có thể bị os-server queue.

`agent_runtime` trong `config.json` chọn backend; việc phân giải nằm ở
`system/agent/factory.go` `ProvideGateway()` — `"codex"` →
`codex.ProvideService`, giá trị lạ rơi về OpenClaw. Khi khởi động, banner
`AGENT BACKEND ACTIVE → CODEX` in `ws_url` + `conversation`.

Giá trị kết nối (`runtimes/codex/constants.go`). Cả ba đường dẫn thiết bị dưới
đây được resolve một lần lúc khởi động process, từ CÙNG các biến môi trường mà
gatewayd và `presync.sh` đọc (`system/lib/syspath`); không set env thì ra đúng
mặc định của thiết bị, nên board không bị ảnh hưởng:

| Giá trị | Mặc định | Env | Ý nghĩa |
|---|---|---|---|
| `WSURL` | `ws://127.0.0.1:18792/codex/ws/` | `CODEX_PORT` | Endpoint WebSocket của bridge cục bộ |
| `Token` | `autonomous_codex_token` | `CODEX_WS_TOKEN` | Bearer token khi connect; bridge đọc cùng giá trị từ `$CODEX_HOME/.env` (do presync sở hữu) |
| `codexHome` | `/root/.codex` | `CODEX_HOME` | State dir mà mọi đường dẫn codex khác dẫn xuất ra — workspace, skills, sessions, config.toml, `.env`, các file state telegram |
| `Conversation` | `device-main` | — | Chỉ là nhãn — Codex sở hữu thread id của nó (§3) |

Chỉ cần set `CODEX_HOME` là dời được cả backend, ở cả phía client lẫn
`codex-gatewayd` (gatewayd lấy nó làm gốc cho mặc định từng file). Đó chính là
cách `make os-dev` / `make codex-dev` chạy binary được ship ở ngoài thiết bị —
xem [os-server_vi.md § Chạy off-device](../os-server_vi.md#chạy-off-device-laptop).

## 1.1 Cài đặt (`install.sh`)

Một lần switch `codex.setup` chạy `system/device/switch_runtime.sh`
(generic), script này materialize các script nhúng của Codex. `install.sh`
(một lần, tự-đủ — chạy thẳng `bash install.sh` cũng cấu hình VÀ khởi động
backend đầy đủ):

1. prerequisites `jq` + `curl`;
2. cài Codex CLI từ **GitHub release đã pin** (`rust-v0.142.5`, asset
   `codex-aarch64-unknown-linux-musl.tar.gz` — musl static, không cần runtime
   deps) vào `/usr/local/bin/codex`; idempotent (bỏ qua khi đúng version đã
   cài);

   > **Update máy đã ngoài thực địa** KHÔNG đi qua pin này: publish bằng
   > `make upload-codex <semver-trần>` + `make promote-codex`, bootstrap worker sẽ
   > chạy `software-update codex` (thay binary + restart) trên mọi máy có
   > `agent_runtime` là `codex`. Pin ở đây là baseline cho image mới flash — giữ
   > đồng bộ với `scripts/imager/build-orangepi.sh`. Xem `docs/vi/bootstrap-ota.md` §5.
3. chạy hook presync một lần (`/usr/local/bin/runtime-codex-presync`, được
   os-server materialize TRƯỚC installer — §1.2);
4. ghi + enable **`codex.service`** (`ExecStart=/usr/local/bin/os-server
   codex-gatewayd`, `EnvironmentFile=/root/.codex/.env`, `HOME=/root`,
   `Restart=always`) — bridge không cần materialize gì, nó nằm sẵn trong
   os-server; rồi drop hook `verify` rẻ + offline (`command -v codex` + binary
   os-server tồn tại) để switch-runtime tự-heal.

Tên unit == tên runtime (`codex.service`), nên không cần file khai báo
`os-runtimes/codex/service`. Log install ghi vào `/root/.codex/install.log`
(rootfs bền — `/var/log` là zram volatile trên các board này).

## 1.2 Presync (`presync.sh`) — nhúng, chạy mỗi lần switch + mỗi lần boot

`presync.sh` được nhúng trong os-server và materialize ra
`/usr/local/bin/runtime-codex-presync`. Nó chạy trước mỗi lần codex start
(switch-runtime), một lần cuối install, **và mỗi lần os-server boot /
config-change qua `EnsureOnboarding`** (pattern hermes): `EnsureOnboarding`
hash các file presync sở hữu (`config.toml` + `.env`) quanh lần chạy và chỉ
restart gateway khi có thay đổi thật. Nó sở hữu mọi thứ stateful:

- **§1 MIGRATE** — copy persona/memory/skills một lần từ workspace openclaw,
  chốt bằng marker `/root/.codex/.openclaw-migrated`. Stop openclaw trước
  (retry 3 lần, non-fatal), rồi copy nguyên văn `IDENTITY.md`, `SOUL.md`,
  `KNOWLEDGE.md`, `HEARTBEAT.md`, `MEMORY.md`, `USER.md` **và `AGENTS.md`**
  (Codex đọc `AGENTS.md` natively — slot persona không cần dịch; Go onboarding
  vẫn inject lại khối OS), cộng `memory/` + `skills/` chỉ khi đích chưa có.
  Marker chỉ được ghi sau một lần copy sạch, nên migrate lỗi sẽ thử lại lần
  sau; factory reset xoá `/root/.codex` sẽ xoá marker nên migrate chạy lại ở
  lần switch kế.
- **§2 CONFIG** — regenerate phần đầu của `/root/.codex/config.toml` từ
  config.json. **Cổng auth:** khi `/root/.codex/auth.json` tồn tại (login
  subscription ChatGPT, §9) phần đầu được ghi KHÔNG có `model` /
  `model_provider` / `[model_providers.autonomous]` — codex dùng provider +
  model mặc định built-in (chỉ giữ `approval_policy` + `sandbox_mode`, và vẫn
  giữ nguyên phần đuôi `[mcp_servers`). Ngược lại (chế độ api-key):
  `model` từ `llm_model` (fallback `Auto-AI`),
  `model_provider = "autonomous"` → `[model_providers.autonomous]` với
  `base_url` từ `llm_base_url` chuẩn hoá kết thúc bằng `/v1` (Codex tự append
  `/responses`), `env_key = "OPENAI_API_KEY"`, và **CHỈ
  `wire_api = "responses"`** — wire chat-completions đã bị gỡ upstream ~2/2026
  (⚠️ VERIFY TRÊN THIẾT BỊ: campaign-api phải serve `{base}/responses`). Kèm
  `approval_policy = "never"` + `sandbox_mode = "danger-full-access"`. **Phần
  đuôi từ dòng `[mcp_servers` đầu tiên trở xuống được giữ nguyên văn** —
  `mcp.go` của os-server sở hữu các entry đó (§7), nên hai chủ sở hữu không
  giẫm nhau.
- **§3 ENV** — ghi `/root/.codex/.env` (systemd EnvironmentFile, mode 0600):
  `CODEX_WS_TOKEN` (phải khớp `constants.go` `Token`), `CODEX_PORT=18792`,
  `CODEX_HOME=/root/.codex`, `CODEX_WORKSPACE=/root/.codex/workspace`, và
  `OPENAI_API_KEY` từ `llm_api_key` — **bỏ qua ở chế độ subscription** (API
  key sẽ lấn át/xung đột với auth ChatGPT).

Trên nền lần chạy presync, `EnsureOnboarding` (`onboarding.go`) làm cùng phần
reconcile workspace như các backend khác: seed `KNOWLEDGE.md` **và `AGENTS.md`**
từ template nhúng chỉ khi chưa có, inject các khối OS-managed `<!-- OS DO NOT REMOVE -->` vào
`SOUL.md` / `AGENTS.md` / `HEARTBEAT.md` (gốc OpenClaw, lược phần
chỉ-OpenClaw), refresh khối AGENTS.md **toàn cục** ở tầng user
(`ensureUserAgentsMDBlock`, xem bên dưới), và capability-gate skills. Thay đổi
chỉ-markdown không bao giờ restart gateway; chỉ presync đổi config hoặc
self-heal unit mới restart.

**Vì sao phải seed `AGENTS.md`.** Codex không có lệnh `setup` để sinh lại
`AGENTS.md` nền như openclaw, nên một **thiết bị chỉ chạy codex** — chưa từng
chạy openclaw, khiến presync §1 không có gì để migrate — hoàn toàn không có file
này. Mà `AGENTS.md` lại là file duy nhất codex tự nạp, nên không có file nghĩa là
không có khối OS **và không có persona**: agent tự giới thiệu là "Codex".
`EnsureOnboarding` giờ seed `runtimes/codex/resources/AGENTS.md` (một bản nền
ngắn, mang tiêu đề `Your Workspace` mà bộ inject khối bám vào) qua
`seedFileIfAbsent` — hàm này **không bao giờ ghi đè**, nên thiết bị đã migrate từ
openclaw vẫn giữ nguyên file của nó.

**Khối persona inline (AGENTS.md).** Codex chỉ tự nạp DUY NHẤT `AGENTS.md` vào
context; chỉ dẫn "Session Startup" bảo đọc `SOUL.md`/`IDENTITY.md` là tự
nguyện, và với turn ngắn model bỏ qua (đã xác minh trên thiết bị: "bạn tên gì"
→ "Tôi là Codex"). OpenClaw/Hermes inject soul vào system prompt ở tầng
runtime; codex không có tầng đó, nên `ensurePersonaInlineBlock` inline persona
THẲNG VÀO `AGENTS.md`: khối
`<!-- OS PERSONA INLINE — DO NOT EDIT (generated from SOUL.md + IDENTITY.md) -->`
… `<!-- /OS PERSONA INLINE -->` được upsert idempotent ở NGAY ĐẦU file (trên
khối OS mandatory), gồm phần mở đầu "Who you are" bắt buộc, tên agent parse từ
`IDENTITY.md` (`- **Name:** …`), và nguyên văn `SOUL.md` vừa được reconcile
(cắt tối đa 20 000 byte kèm ghi chú truncate để `AGENTS.md` nằm dưới trần
32 KiB project-doc của codex). Khối được dựng lại sau `ensureSoulMDBlock` mỗi
lần `EnsureOnboarding` và ngay sau khi đổi tên (`UpdateIdentityName`), nên
turn kế tiếp thấy tên mới luôn; `SOUL.md` biến mất thì khối bị gỡ. Ghi atomic
(tmp+rename), và chỉ ghi khi byte thực sự khác.

### Skills — discovery native `$CODEX_HOME/skills` (`codexSkillsDir`)

Skills của thiết bị nằm ở **`/root/.codex/skills/<name>/SKILL.md`** — thư mục
discovery **native** của codex-cli (`$CODEX_HOME/skills` trên 0.142.x). Codex tự
phát hiện mọi `<name>/SKILL.md` có YAML frontmatter hợp lệ ở đây, trong **mọi**
phiên bất kể cwd, và liệt kê trong picker skill `@` tương tác. Điều này giống fix
của claudecode (skills chuyển sang thư mục native `~/.claude/skills` của Claude
Code). KHÔNG đặt ở `workspace/skills` — codex không bao giờ quét nó; thiết bị để
skills ở đó sẽ có picker `@` rỗng và không nạp skill native. Mọi nơi tạo skill đều
trỏ tới `codexSkillsDir`: `presync.sh` §1 (migrate từ openclaw → `$CODEX_DIR/skills`),
`skill_watcher.go` (tải CDN + thông điệp `notifySkillChanges`), và
`pruneUnsupportedSkills` (capability gate). `EnsureOnboarding` cũng tải lại mọi
skill được hỗ trợ từ CDN khi boot hoặc reconcile cấu hình. Việc này sửa skill local
bị cũ nếu OS Server restart sau lúc CDN publish, trước khi watcher version năm phút
kịp thấy thay đổi; nội dung không đổi sẽ không bị thông báo lại. Watcher log mỗi
lần poll và chỉ ghi nhận version OTA sau khi tải/extract archive thành công, nên lỗi
tải tạm thời sẽ được thử lại ở lần poll năm phút sau. Sau một lần sync từ onboarding
cũng restart Codex bridge, thông báo yêu cầu đọc lại skill chờ bridge reconnect tối đa
một phút thay vì bị mất khi bridge chưa sẵn sàng.
`migrateSkillsToCodexHome` nâng bất kỳ
`workspace/skills` cũ do os-server đời trước để lại vào thư mục native rồi xoá bản
workspace (idempotent); factory reset xoá toàn bộ `/root/.codex`, nên bộ skills
được migrate lại từ openclaw ở lần `EnsureOnboarding` kế tiếp.

**AGENTS.md toàn cục — luật cấp thiết bị cho phiên coding.** `AGENTS.md` trong
workspace chỉ tới được phiên **device-chat**: gatewayd chạy
`codex exec --cd /root/.codex/workspace` (`gatewayd/turn.go`), nên vòng quét
AGENTS.md từ repo-root→cwd của codex tìm thấy nó. Một **phiên coding Telegram**
(`telegram_coding.go`, mục "Telegram remote coding-sessions") chạy
`codex exec --cd <folder>` ở thư mục tuỳ ý (`/root`, `/root/myapp`, …), vòng
quét không bao giờ chạm file trong workspace. Codex còn nạp thêm một file
user-instructions **toàn cục**, `$CODEX_HOME/AGENTS.md` = `/root/.codex/AGENTS.md`,
trong **mọi** phiên bất kể cwd (codex-rs `CodexHomeUserInstructionsProvider`,
merge trước vòng quét project). `ensureUserAgentsMDBlock` (`codexUserAgentsMD`)
inject một khối OS ở đó — cùng kỷ luật marker như file workspace — mang luật
**Connectors (BẮT BUỘC)** và ghi chú rằng skills của thiết bị nằm ở đường dẫn
**tuyệt đối** `/root/.codex/skills/<name>/SKILL.md`. Không có nó, phiên coding
không biết connectors của thiết bị tồn tại và báo chủ máy rằng Gmail/Calendar
"chưa kết nối". Như một fallback đọc-theo-path (độc lập với discovery native), khối
`AGENTS.md` workspace và `notifySkillChanges` đều dẫn skill bằng đường dẫn
**tuyệt đối** đó, không bao giờ dùng `skills/…` tương đối — vốn sẽ resolve dưới
`<folder>` của phiên coding. Không cần restart gateway (đọc lại mỗi turn); factory
reset xoá toàn bộ `/root/.codex`, nên file toàn cục được dựng lại ở lần
`EnsureOnboarding` kế tiếp.

## 2. Transport & gửi một turn

`client.go` giữ một WebSocket bền tới bridge (khuôn picoclaw: bearer token,
không có handshake pairing, keepalive ping 25s → `pong`, reconnect có backoff,
LED `StateAgentDown` khi rớt). `chat.go` `sendChat` ghi một frame rồi return;
reply về trên read loop:

```json
{ "type": "message.send", "id": "<reqID>", "payload": { "content": "<text>",
  "attachments": [{ "type": "image", "url": "data:image/jpeg;base64,…" }] } }
```

Bridge lưu attachment vào `/root/.codex/attachments` rồi đưa chúng vào input
của App Server. Frame `{"type":"session.new"}` xóa thread hiện hành (§4).
Message mới tương thích với thread active dùng `turn/steer`; trường hợp khác
gatewayd bắt đầu turn bình thường. Event vẫn correlate với `runID` khởi tạo;
steering không bao giờ âm thầm gán lại turn.

## 2.1 Thời lượng một turn — timeout và busy TTL

Một turn chat được kỳ vọng là chậm: người dùng mở chat đúng vào lúc việc cần nhiều
thời gian, và cùng prompt đó chạy qua Telegram/OpenClaw mất 35 phút mới xong. Có
hai con số chặn nó, và thứ tự giữa chúng là bắt buộc:

| Knob | Ở đâu | Mặc định | Nghĩa |
|---|---|---|---|
| `CODEX_TURN_TIMEOUT_S` | `gatewayd/gatewayd.go` | `600` (10 phút) | Kết thúc turn App Server đang active rồi gửi `bridge.error: timeout`. Gatewayd LUÔN kết thúc turn — xong, lỗi, hoặc timeout này. |
| `busyTTL()` | `events.go` | timeout đó **+ 5 phút** | Gỡ kẹt pipeline sensing khi frame cuối của turn bị RỚT. Suy ra từ chính env var trên nên nâng timeout không bỏ sót nó. |

**TTL hết hạn thì kết thúc turn cho ĐỦ, cả hai nửa.** Vừa bỏ runId, vừa bắn một
lifecycle error mang chính runId đó (`failStuckTurn`, đi qua `wsDispatch` vì việc
kiểm busy chạy trên đường sensing, nằm ngoài vòng đọc WS). Làm thiếu nửa nào cũng
thành bug: để runId lại thì turn KẾ TIẾP bị mồ côi (`ensureTurnStarted` thoát sớm
khi `currentRunID` khác rỗng, nên frame của turn mới bị gán vào run đã chết và
chat mới treo); còn xoá im lặng thì trình duyệt ngồi chờ tới hết hạn của chính nó
— đúng cái "no response" mà đường này sinh ra để chặn.

**TTL phải dài hơn timeout.** Trước đây TTL cố định 5 phút trong khi timeout là 10
phút, nên mọi turn chậm quá 5 phút đều rơi vào nhánh "frame bị rớt" — mà nhánh đó
còn gọi `clearTurn()`, xoá luôn runId của turn đang chạy. Run mà trình duyệt đang
chờ thành mồ côi: các frame lifecycle/error sau đó không thấy run hiện hành, cấp
runId mới và gắn vào một turn xếp hàng không liên quan, nên khung chat treo ở
trạng thái pending rồi báo "no response" trong khi backend vẫn chạy bình thường.

Đo trên lamp-0c89 ngày 3/9/2026: run `device-chat-139` bắt đầu 15:40:41, mất runId
lúc 15:45:41, và `timeout` lúc 15:50:41 bị báo dưới `device-chat-168`. `openclaw`
và `hermes` không hề xoá runId khi TTL hết hạn — đó là lý do turn Telegram dài
luôn chạy được ở hai runtime đó; đường codex giờ đã giống vậy.

**Lượt resume bị timeout thì BỎ LUÔN thread.** Việc xoay thread
(`ShouldRotateSession`) đọc kích thước context từ `turn.completed`, nên nó chỉ nổ
sau khi một lượt THÀNH CÔNG — thread mà mọi lượt resume đều treo thì không bao giờ
được xoay, và id thread nằm trong session file nên restart service lẫn reboot máy
đều resume đúng cái thread chết đó. Đo trên lamp-0c89 ngày 3/9/2026: thread
`01a06665` tạo lúc 15:31, mọi lượt sau 15:40 đều treo không có `turn end` suốt hơn
một tiếng, qua hai lần restart service và một lần reboot; trong khi `curl` thẳng
cùng endpoint trả lời đúng payload 75k token đó trong 57 giây, và `codex exec`
thread mới trả lời trong vài giây — nên endpoint chưa bao giờ là vấn đề. Gatewayd
giờ gọi `clearSession()` khi resume timeout, đây là đường thoát duy nhất không cần
người can thiệp.

Hạn bỏ cuộc của web chat (`REPLY_IDLE_TIMEOUT_MS`) được đặt dài hơn mức chặn
turn. Event App Server có thể báo tiến độ sống, nhưng một steer vẫn là một phần
của chính turn active, không phải một lời hứa hoàn tất độc lập.

### Gửi tiếp request còn nằm trong queue khi kết nối lại

Khi WebSocket và callback dispatch đã sẵn sàng, Codex client drain `pendingEvents`
ngay, không cần chờ một turn khác kết thúc. Nhờ vậy tin nhắn người dùng chưa gửi
không bị kẹt sau khi gateway restart cho tới lúc một lượt sensing không liên quan
hoàn tất. Các quy tắc chờ loa, hết hạn event, gộp và ưu tiên vẫn giữ nguyên.

Các callback drain từ reconnect, idle và loa được chạy tuần tự; khi offline,
queue được giữ lại. Nếu chưa hề thử ghi vào socket, event hiện tại và phần còn
lại tiếp tục nằm trong queue. Ghi socket thất bại có kết quả giao nhận chưa rõ,
**không** tự gửi lại. `pendingRuns` đã truyền chỉ dùng tương quan phản hồi, không
bao giờ làm nguồn replay. Nếu turn ở gateway vẫn chạy sau khi mất kết nối, FIFO
đặt các event chưa gửi được bổ sung sau turn đó. Queue vẫn ở RAM; thay đổi này
không lưu event chưa gửi qua một lần restart tiến trình os-server.

### Chặn assistant output lặp bất thường

Gateway chặn mẫu lặp âm tiết đã quan sát trước khi chuyển assistant item sang
translator, log câu trả lời hoặc TTS. Điều kiện bảo thủ yêu cầu ít nhất
**64 KiB** assistant text và **4096 từ tách bằng khoảng trắng**: ít nhất **95%**
được bao phủ bởi tối đa **tám token chỉ gồm chữ cái**, mỗi token dài tối đa
**tám ký tự Unicode** và xuất hiện ít nhất **128 lần**. Tập token ứng viên giới
hạn **1024**; văn bản đa dạng vẫn được chấp nhận. Đây là heuristic nhận diện lặp,
không phải giới hạn độ dài câu trả lời: code dài, fixture số và giải thích thông
thường vẫn có thể qua. Không phân loại output của tool.

Khi chặn, gateway kết thúc turn App Server hiện đang active, không chuyển item bị chặn
hay frame báo thành công phía sau, xóa thread lưu trước lượt kế tiếp trong queue,
và gửi `bridge.error` có tiền tố `degenerate_output:`. Áp dụng cho cả thread mới
và resume, **không tự chạy lại task**. Tool trước đó có thể đã tác động hệ thống;
cần quan sát trạng thái thật trước khi tiếp tục. Cách ly chỉ bỏ con trỏ resume,
không xóa rollout của CLI hoặc hoàn tác tác động.

CLI đang pin chỉ phát assistant text khi item hoàn tất, nên guard chưa thể ngắt
quá trình sinh lặp trước khi nhận item. Timeout của turn giữ nguyên. Batch này
cũng chưa bổ sung ngắt bằng giọng nói, tương quan request ID trong queue hay giới
hạn output token ở provider. Tests bao phủ các biến thể âm tiết có dấu đã gặp,
output dài hợp lệ, dừng tiến trình con, cách ly thread mới/resume và lượt tiếp
theo trong queue bắt đầu mới mà không chạy lại task lỗi.

## 3. Dịch event (`translator.go`)

Bridge map các notification JSON-RPC của Codex App Server bền (cộng các frame
riêng `bridge.status` / `bridge.error` / `pong`) sang đúng khuôn
`domain.WSEvent` mà handler OpenClaw tiêu thụ:

| Event inbound | `domain.WSEvent` phát ra |
|---|---|
| `thread.started` | bắt thread id làm session key + `agent` lifecycle `phase:start` (một lần mỗi turn) |
| `turn.started` | `agent` lifecycle `phase:start` (một lần mỗi turn) |
| `item.started` `command_execution` / `mcp_tool_call` | `agent` tool `phase:start` (`shell` / `server.tool`) |
| `item.completed` `command_execution` / `mcp_tool_call` | tool `phase:end` (phát start trước nếu chưa thấy) |
| `item.completed` `web_search` / `file_change` | cặp tool `phase:start` + `phase:end` |
| `item.completed` `agent_message` | giữ làm câu trả lời; cái mới đẩy cái trước xuống `stream:thinking` (xem *Preamble* bên dưới) |
| `item.*` `reasoning` / `todo_list` | *(bỏ qua — status, không phải nội dung)* |
| `turn.completed` | `agent` `stream:assistant` (nguyên câu trả lời trong **một** delta) **+** `chat` `state:final role:assistant` **+** lifecycle `phase:end` kèm usage — kết thúc turn |
| `turn.failed` / `error` / `bridge.error` | `agent` lifecycle `phase:error` — kết thúc turn |
| `bridge.status` / `pong` | *(log / bỏ qua)* |

Giống PicoClaw, text `agent_message` tích luỹ được đưa ra ở `turn.completed`
dưới dạng một assistant delta duy nhất **trước** `chat.final` /
`lifecycle.end` — trường hợp N=1 của hợp đồng streaming, chính nó cho consumer
chung flush TTS + marker phần cứng `[HW:/…]` tại `lifecycle.end`.

**Preamble.** Codex có thể tự thuật trước khi gọi tool, thành một item
`agent_message` riêng ("Using the sensing skill for this presence event.",
"Posture summary is present, so this is the posture-nudge route."). Gộp hết
`agent_message` lại là đọc cả chuỗi tự thuật đó ra loa — đúng lỗi leak thấy ở
`presence.enter` và nudge `motion.activity`. Nên chỉ `agent_message` **cuối
cùng** của turn mới là câu trả lời: mỗi cái trước đó bị đẩy xuống
`stream:thinking` (chỉ vào Flow Monitor, không bao giờ ra TTS hay reply kênh)
ngay khi có cái mới hơn chứng minh nó không phải câu trả lời. Ngoại lệ: message
không phải cuối mà mang marker `[HW:/…]` là hành động phần cứng thật nên vẫn
giữ trong reply. Chỉnh prompt không chặn preamble một cách đáng tin — đây mới là
chỗ cưỡng chế.

**Usage:** App Server KHÔNG đặt usage trên `turn/completed`. codex-rs 0.150.1
đẩy nó trên một notification riêng, dạng camelCase, trước sự kiện kết thúc turn:

```
thread/tokenUsage/updated  {"threadId":…,"turnId":…,"tokenUsage":{
   "total":{…tích luỹ cả thread…},
   "last":{"totalTokens":…,"inputTokens":…,"cachedInputTokens":…,
           "cacheWriteInputTokens":…,"outputTokens":…,"reasoningOutputTokens":…}}}
```

gatewayd giữ lại khối `last` (`storeAppUsage`), đổi camelCase về tên snake_case
mà đường exec JSONL dùng, rồi gắn vào frame `turn.completed` — đúng chỗ
translator đọc usage (`usageOf` vẫn nhận thêm khối `usage` nằm trên chính
notification hoặc lồng trong `turn`, cho các bản codex khác). Trước khi có phần
này, frame gửi ra là frame rỗng nên thẻ turn trong Flow Monitor **không hiện
token nào cả**. `takeAppUsage` xoá chỗ giữ tạm, nên một turn không báo gì sẽ
không thừa hưởng số của turn trước.

Codex nói OpenAI Responses API, mà ở đó `input_tokens` **đã bao gồm**
`cached_input_tokens`. Nên translator phải TRỪ: `input - cached → InputTokens`,
`cached → CacheReadTokens`, `cache_write → CacheWriteTokens`,
`output → OutputTokens`, `TotalTokens = fresh + cached + out` — đúng phép quy
đổi mà `runtimes/hermes/translator.go` làm cho dạng chat/completions, để monitor
hiển thị `↓fresh R<cache> Σtotal`. Rotation dùng `input_tokens` THÔ
(`lastContextTokens`) vì đó chính là toàn bộ prompt, tức kích thước context
sống; cộng thêm cached vào sẽ làm ngưỡng rotate giảm còn một nửa.

> Ba wire khác nhau: `/responses` (codex) và `chat/completions` (hermes) gộp
> phần cached VÀO `input_tokens`; `/messages` (claudecode, openclaw) và opencode
> báo tách riêng. `domain.TokenUsage` theo cách tách của Anthropic, nên chỉ hai
> loại đầu mới phải trừ.

## 4. Session

Codex sở hữu session: bridge bắt thread id của App Server làm session key.
`NewSession` gửi frame `session.new` → bridge xóa thread hiện hành →
`turn/start` kế tiếp là fresh. `turn/steer` chỉ append vào turn còn active của
chính thread đó, không bao giờ vào thread khác hoặc turn đã hoàn tất.

Codex **tự auto-compact context của nó** (`model_auto_compact_token_limit`),
nhưng thiết bị có lưới an toàn **120k token**: tại 134k, một lượt `codex exec`
đã mất 100 giây; các lượt resume sau đó phình lên 376k rồi 473k. Nó tính trên kích thước **context** đang sống —
`input_tokens + cached_input_tokens` của `turn.completed` gần nhất, được
translator lưu vào `lastContextTokens` — chứ không phải `totalTokens` mà handler
chung truyền vào (số đó cộng cả output của lượt này, là khối lượng turn chứ
không phải context). Tự đọc usage frame của mình giúp thay đổi này nằm gọn
trong codex: các backend khác không bị đụng tới.

Ngưỡng vẫn chừa một khoảng nhỏ trên sensing turn khỏe mạnh lớn nhất đã quan sát
(116k), nhưng chặn được vách độ trễ đã đo. Theo
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md) §4 "No fake
success", `CompactSession`, `GetConfigJSON` (config của Codex là TOML + secrets
trong `.env` — không có file JSON để lộ ra), `UpdatePrimaryModel`, và
`RefreshModelsConfig` đều trả `domain.ErrNotSupportedByRuntime` — không bao giờ
`nil`. Đây không phải ngõ cụt: caller của `RefreshModelsConfig` rơi về
`EnsureOnboarding`, presync của nó đọc lại `llm_*` từ config.json và hash gate
restart gateway — nên thay đổi llm **được áp dụng live**, chỉ là không qua
method đó.

## 5. Kênh

### Telegram (receive loop device-owned)

Telegram dưới Codex là **device-owned**. Codex CLI không có channel layer riêng
(khác PicoClaw: binary runtime của PicoClaw tự poll Telegram Bot API — presync
của nó bật `channel_list.telegram` trong config riêng của PicoClaw), nên
os-server tự chạy receive loop inbound: `runtimes/codex/telegram_poll.go`, một
goroutine khởi động từ `StartWS` (nằm ngoài vòng reconnect nên sống qua các lần
WS rớt). Vì loop nằm trong lifecycle của service codex, nó chỉ chạy **khi codex
là runtime đang active** — không bao giờ tranh `getUpdates` với poller của
openclaw/hermes (Telegram trả 409 khi có poller song song).

Loop long-poll `getUpdates` (cửa sổ 50 s, client timeout 70 s) và đọc
`config.TelegramBotToken` / `TelegramUserID` **mới ở mỗi vòng lặp** — lưu hay
xoay credential không cần restart; khi token rỗng thì kiểm tra lại mỗi 30 s,
lỗi HTTP/mạng thì backoff 5 s. Một tin nhắn chỉ được chấp nhận khi là tin
**text** không rỗng, trong chat **private**, và `from.id` bằng
`TelegramUserID` (so sánh chuỗi sau `strconv`); mọi thứ khác bị bỏ qua ở mức
debug nhưng offset vẫn tiến (không re-deliver). Offset kế tiếp được ghi
nguyên tử (temp + rename) vào `/root/.codex/telegram_offset.json`, và chat id
của mỗi tin được chấp nhận được upsert vào
`/root/.codex/telegram_targets.json` để chiều outbound `Broadcast` (cảnh báo
chủ động sensing/guard) tới đúng chat.

Mỗi tin được chấp nhận chờ agent rảnh (`IsBusy`, poll 500 ms, tôn trọng ctx)
rồi được inject qua `sendChat` với flow source `telegram`, nên `chat_input` /
`chat_send` phát như bình thường và Flow Monitor thấy rõ nguồn gốc. Văn bản
turn được inject có prefix metadata người gửi — format chính xác
`[telegram] Message from <FirstName LastName> (@username) [id:<numeric>]:\n<text>`,
do `tgUser.label()` dựng (phần `(@username)` bị bỏ khi không có, tên fallback
về `unknown`) — để agent biết ai đang nói và trên kênh nào, mirror hành vi
telegram plugin của openclaw. Run được
đánh dấu **silent** (reply không được đọc qua TTS) và theo dõi trong
`telegramRuns`; tại `turn.completed`, `emitFinal` consume tracker và DM văn
bản cuối về đúng chat gốc, sau khi strip marker phần cứng `[HW:/...]` và audio
tag TTS (`[laugh]`, `[sigh]`, …) — `stripForChannel` trong `hal.go`, mirror
`hwMarkerRe` phía downstream và whitelist audio-tag của HAL. Khi `turn.failed`,
tracker được consume mà không DM để map không leak.

Trong lúc turn chạy, một goroutine `telegramTypingKeeper` giữ chỉ báo
"đang nhập…" của Telegram: ngay sau khi turn được inject, nó bắn Bot API
`sendChatAction(typing)` lập tức rồi lặp lại mỗi 4 s (chỉ báo tự hết hạn sau
~5 s) cho tới khi run được consume — reply đã DM qua `emitFinal` hoặc turn
lỗi qua `handleError` — và bị chặn trần bởi `telegramTypingLifetime` = 10 phút
để một turn kẹt không thể làm chat "đang nhập…" mãi mãi. Việc gửi là
best-effort (lỗi chỉ log ở mức debug rồi bỏ qua).

### Slack (đường proxy HTTP-mode)

Slack cũng là **device-owned**, qua đường proxy HTTP-mode (mô phỏng bridge
của hermes, `runtimes/hermes/slack.go`): proxy công khai bff-campaign-service
nhận các delivery Slack Events API và fan-out qua MQTT tới handler
`slack_event` của thiết bị
(`server/device/delivery/mqtt/slack_event_handler.go`), handler dedup theo
`event_id` (LRU trong bộ nhớ, TTL 5 phút) rồi type-assert gateway đang active
sang `domain.SlackBridge`. `CodexService` implement bridge đó
(`runtimes/codex/slack.go`), nên event chỉ được route về đây **khi codex là
runtime đang active** — không sửa gì code dispatch phía server, cùng cách nối
dây với hermes. Không dùng Socket Mode; thiết bị không bao giờ mở WebSocket
tới Slack.

Xử lý event mirror hermes: `url_verification` echo lại challenge; chỉ event
`message` / `app_mention` từ user thật mới qua (tin của bot, event có subtype
và event không có user bị bỏ qua — loop guard); mention bot ở đầu bị strip; và
khi `config.SlackUserID` được set, chỉ user đó được phép tạo turn (rỗng = mở,
workspace/app đã tự giới hạn phạm vi). Reply đi vào thread sẵn có nếu tin nhắn
nằm trong thread, ngược lại thread dưới tin của user (`thread_ts` fallback =
`ts` của tin).

Tin được chấp nhận sẽ được inject bất đồng bộ (goroutine): chờ agent rảnh
(`IsBusy`, poll 500 ms, chặn trần 2 phút — quá trần thì tin bị bỏ, vì MQTT
handler đã ack và Slack sẽ không gửi lại), rồi đi qua `sendChat` với flow
source `slack` và prefix metadata người gửi
`[slack] Message from <@U…> [channel:C…]:\n<text>`. Run được đánh dấu
**silent** (không TTS) và theo dõi trong `slackRuns` (runID →
channel/thread_ts/ts của tin); việc nhận tin được xác nhận bằng **reaction
eyes** trên tin của user (best-effort). Tại `turn.completed`, `emitFinal`
consume tracker và post reply — đã làm sạch bằng `stripForChannel`, như
telegram — về đúng channel/thread qua `chat.postMessage`
(`config.SlackBotToken`), đồng thời xoá reaction eyes; khi `turn.failed`,
tracker được consume và chỉ xoá reaction (không reply). Khác hermes,
**không có progressive streaming** (`chat.startStream`/`appendStream`) và
không có status "…is typing" của assistant — codex exec trả reply nguyên
khối, nên `StreamSlackDelta` là no-op và văn bản cuối chỉ được post một lần;
`DeliverSlackReply` (do shared agent handler gọi) là safety net
consume-nếu-còn, bình thường là no-op vì `emitFinal` đã consume tracker đồng
bộ trước khi dispatch các event lifecycle.

Yêu cầu (config.json): `slack_bot_token` (`chat.postMessage` + reactions) và
tuỳ chọn `slack_user_id` (allowlist + đích `Broadcast` chủ động của channel
sender `SlackSender`). `slack_signing_secret` của HTTP mode do proxy công khai
tiêu thụ, không phải trên thiết bị — codex, như bridge của hermes, tin đường
MQTT đã được xác thực.

### Discord (phiên gateway bot device-owned)

Discord cũng là **device-owned**: nó yêu cầu một phiên bot Gateway WebSocket
(không có API nhận kiểu long-poll), nên os-server chạy phiên đó qua
[discordgo](https://github.com/bwmarrin/discordgo)
(`runtimes/codex/discord.go`). Như loop telegram, phiên được khởi động từ
`StartWS` (`go s.startDiscordBot(ctx)`) và sống trong vòng đời của service
codex — nó chạy **chỉ khi codex là runtime đang active**, nên không bao giờ
tranh phiên bot với runtime khác trên cùng token. Loop đọc
`config.DiscordBotToken` **mới trên mỗi lần thử kết nối** (rỗng → kiểm tra
lại mỗi 30 s; open lỗi → back off 15 s); mở xong thì discordgo tự xử lý
reconnect gateway, và phiên được đóng khi ctx của gateway kết thúc. Intents:
direct messages + guild messages + message content.

Tin chỉ được chấp nhận nếu không phải từ bot (loop guard, bao cả tin của
chính bot), id người gửi bằng `config.DiscordUserID` (**allowlist là bắt
buộc** — rỗng thì từ chối tất cả, vì ai cũng có thể chung guild với bot), và
là **DM** hoặc tin trong `config.DiscordGuildID` có **@mention bot** (mirror
hành vi phổ biến của plugin openclaw: guild yêu cầu @mention, DM thì không —
bản thân mention bị strip khỏi văn bản turn). Mọi thứ khác bị bỏ qua ở mức
debug.

Mỗi tin được chấp nhận chờ agent rảnh (`IsBusy`, poll 500 ms, tôn trọng
ctx), rồi được inject qua `sendChat` với flow source `discord` và prefix
metadata người gửi
`[discord] Message from <Username> [id:<id>]:\n<text>`. Run được đánh dấu
**silent** (không TTS) và theo dõi trong `discordRuns` (runID → channel id);
trong lúc turn chạy, một goroutine `discordTypingKeeper` giữ **chỉ báo đang
nhập native** của Discord (`ChannelTyping` lập tức rồi mỗi 8 s — chỉ báo kéo
dài ~10 s — chặn trần 10 phút). Tại `turn.completed`, `emitFinal` consume
tracker và post reply — đã làm sạch bằng `stripForChannel`, như telegram — về
đúng channel qua `ChannelMessageSend`, **chia khúc theo giới hạn cứng 2000 ký
tự mỗi tin của Discord** (ưu tiên cắt ở ranh giới xuống dòng khi có thể); khi
`turn.failed`, tracker được consume mà không reply nên map không thể leak.
Bên gửi reply dùng handle phiên có mutex bảo vệ trên service (phiên nil → log
+ bỏ).

Yêu cầu (config.json): `discord_bot_token`, `discord_user_id` (allowlist), và
`discord_guild_id` khi cần mention trong guild hoạt động (setup chỉ dùng DM
có thể để trống).

### Channel API

`SupportedChannels()` trả `["telegram", "slack", "discord"]`. `AddChannel` /
`RefreshChannelConfig` cho cả ba đều là no-op success trung thực — mọi
consumer đọc credential mới từ config.json mỗi lần dùng (loop telegram mỗi
vòng lặp, bridge Slack mỗi event / lần gọi Web API, bot discord mỗi lần thử
kết nối / mỗi tin), nên không có gì phía agent để ghi và không cần restart.
`AddChannel(discord)` kiểm tra thêm rằng `discord_bot_token` và
`discord_user_id` phải có mặt (đường nhận không thể hoạt động nếu thiếu —
chấp nhận sẽ là fake success). Whatsapp không có đường nhận và trả
`domain.ErrChannelNotSupported`. Xem thêm
[`adding-agent-runtime_vi.md`](adding-agent-runtime_vi.md).

### Coding từ xa qua Telegram (`telegram_coding.go`, `coding_sessions.go`)

Mirror `runtimes/claudecode/telegram_coding.go` 1:1 — một chat Telegram có thể
**gắn vào thread `codex` interactive của một folder và code tiếp từ điện thoại**,
nhiều folder mỗi folder một thread. Tách biệt với lượt persona device-main.

- **Khám phá** (`coding_sessions.go`): codex lưu mỗi thread thành "rollout" JSONL
  ở `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
  (`/root/.codex/sessions`). `allCodingSessions` quét cây đó; **thread id
  (`payload.id`) và cwd (`payload.cwd`) lấy từ record `session_meta` đầu tiên**;
  danh sách hiện **3 prompt người dùng gần nhất** (mới nhất trước, bỏ khối
  `<environment_context>` tổng hợp). Dedupe theo thread id (rollout mới nhất thắng).
  Khác claude, codex resume **theo thread-id toàn cục**: `codex exec --cd <dir>
  resume <id>` đặt cwd độc lập, không cần khớp cwd gốc (đã verify device —
  resume thread cũ echo lại id; lỗi 404 trong test đó là do endpoint
  campaign-api `/responses` chưa có, KHÔNG phải cơ chế resume).
- **Lệnh** (chặn trong `handleTelegramUpdate` trước khi inject device-main):
  `/resume` (giống CLI codex — không tham số thì liệt kê folder, `/resume <n>`
  chọn theo số, `/resume <folder>` chọn mới nhất) · alias `/sessions` (liệt kê) +
  `/use <n|folder>` (chọn) · `/sessions <folder>` (mọi thread trong 1 folder) ·
  `/new <folder>` · `/here` · `/device` · `/help`. Chat chưa chọn và không phải
  lệnh thì rơi xuống device-main như cũ.
- **Mô hình hand-off.** Mỗi lượt spawn một `codex exec --json
  --dangerously-bypass-approvals-and-sandbox --cd <folder> [resume <thread>]
  <prompt>` (thứ tự cờ quan trọng — `--cd` bị từ chối sau `resume` nên phải đứng
  trước). Reply là các item `agent_message` gộp lại, parse từ JSONL
  (`parseCodexResult`: `thread.started`→id, `item.completed`/`agent_message`→
  text, `turn.completed`→xong), DM chunk ở 4000 ký tự. Env exec **mirror
  `turnEnv` của gatewayd** (env tiến trình + cặp `.env` presync + `HOME=/root` +
  `CODEX_HOME=/root/.codex`), nên codex dùng đúng `config.toml` + auth — chạy
  được **cả 2** auth mode (`OPENAI_API_KEY` qua `/responses`, hoặc `auth.json`
  subscription ChatGPT) mà không phải sửa runner.
- **Bảo vệ.** Mutex theo folder tuần tự hoá các lượt; lượt quét `/proc`
  (`procHoldsFolder`) từ chối chạy khi còn TUI `codex` interactive giữ folder
  (2 writer làm hỏng rollout). Chỉ `telegram_user_id` allowlist chạm được; coding
  chạy `--dangerously-bypass-approvals-and-sandbox` nên allowlist là ranh giới
  bảo mật.
- **Trạng thái.** Lựa chọn mỗi chat persist vào `/root/.codex/telegram_coding.json`
  (sống qua restart). Chạy thẳng trong os-server (mỗi lượt một `codex exec`
  riêng), độc lập với child thường trú của gatewayd.

## 6. Hooks

Codex không có hooks loader, nên hook `emotion-acknowledge` của OpenClaw được
tái hiện **native bằng Go** (`runtimes/codex/emotion_ack.go`, mirror
`emotion_ack.go` của hermes): mỗi turn user-visible, sendChat bắn
`{emotion:"thinking"}` sang HAL — cùng prefix skip, cùng intensity, cùng
capability gate (`skills.SupportedHooks`) như handler TS. Hook `turn-gate` đi
kèm cố ý không mirror (sendChat đã đánh dấu turn busy rồi). ⚠️ Giữ lockstep với
`runtimes/openclaw/hooks/emotion-acknowledge/handler.ts`.

## 7. Connector MCP (`mcp.go`)

`WriteMCPEntry` / `RemoveMCPEntry` (flow MQTT `connector.set`) sửa
`/root/.codex/config.toml` `[mcp_servers.<name>]` qua **go-toml/v2**, ghi atomic
(temp + rename) dưới `mcpMu`, rồi restart gateway để lần `codex exec` kế nhận
server mới. Dịch shape từ entry canonical khuôn OpenClaw: **entry http map
`headers` → `http_headers` và key `type` bị bỏ** (Codex suy transport từ `url`
vs `command`); entry stdio đi qua nguyên vẹn. `RemoveMCPEntry` idempotent
(`removed=false`, không ghi, không restart khi entry vắng). presync chỉ
regenerate phần **đầu** của config và giữ nguyên đuôi `[mcp_servers` (§1.2),
nên các entry sống sót qua mọi lần sync.

## 8. Factory reset (`reset.go`)

`ResetAgent` (do `server/system/factoryreset.go` gọi trên gateway đang active)
không giữ lại gì — config.toml/.env được presync regenerate ở lần switch kế:
**stop** `codex.service` (+ verify inactive, poll 5s), **disable** nó (reboot
mặc định về openclaw), **xoá sạch `/root/.codex`** — config, auth CLI, thread
(`sessions/`), workspace, và marker `.openclaw-migrated` (để presync §1
re-migrate ở lần switch kế) — rồi tạo lại các thư mục baseline `workspace/` +
`attachments/` (Codex không có subcommand onboard; CLI tự tạo lại state dưới
`CODEX_HOME` ở lần chạy đầu).

## 9. Auth — ghi chú phase 2

Phase 1 (hiện tại) xác thực bằng **API key qua campaign-api**:
`OPENAI_API_KEY` = `llm_api_key` trong config.json, `base_url` của provider =
`llm_base_url` (§1.2). Auth theo subscription ChatGPT
(`codex login --device-auth`) **dời sang phase 2** — sẽ dùng chung plumbing
login-pairing với `ClaudeLoginPairer` của nhánh claudecode khi nhánh đó merge.

### Auth subscription (thủ công)

Đã dùng được ngay hôm nay mà không cần flow pairing phase-2: chạy
`codex login --device-auth` trên thiết bị, hoặc copy `~/.codex/auth.json` sẵn
có từ máy khác sang `/root/.codex/auth.json` (`chmod 600`). Presync tự phát
hiện `auth.json` ở mỗi lần chạy (tức mỗi lần boot): bỏ khối provider tuỳ chỉnh
khỏi config.toml và bỏ `OPENAI_API_KEY` khỏi `.env`, nên codex nói chuyện
thẳng với OpenAI bằng provider + model mặc định built-in — chế độ này **né
hoàn toàn blocker 404 `/responses` của campaign-api**. Xoá `auth.json` để quay
về chế độ api-key; việc chuyển đổi tự động ở lần presync kế.

### Đối chiếu turn active và steering

`message.send` mang request `id` và `run_id` gốc trên device. Gatewayd giữ các
ID đó trên event của turn. Message tương thích nhắm thread active dùng
`turn/steer` thay vì chờ phía sau. Trace device riêng nhận xác nhận
`bridge.steered` và flow event `turn_merged` chứa `parent_run_id`. Yêu cầu voice
và web chat đã gộp giữ active đến reply cuối hoặc lỗi chung, kể cả mất kết nối
và timeout. Follow-up có tiếng mới nhất phát câu trả lời theo run ID và route
Harness gốc; host và các voice input trước chặn TTS lặp. Marker phần cứng chạy
một lần trên host và được bỏ khỏi reply con. Lifecycle đã gộp không thay hoặc
xóa tracking phiên của host. Việc này ngăn follow-up đã gộp giữ pending run ma và làm kẹt busy
state. Control frame (`pong`, `bridge.status`) độc lập.
History sync `voice_agent_handled` từ realtime cũng steer khi Codex đang active:
tầng voice đã nói rồi nên trace im lặng kết thúc ngay theo xác nhận, thay vì xếp
hàng sau task đang chạy.
Yêu cầu người dùng được steer giữ nguyên nội dung và địa chỉ phản hồi, thêm
routing context ưu tiên chúng hơn sensing thụ động và hướng task cho agent trên
máy tính qua `harness-use`. Không thêm context này vào đồng bộ lịch sử im lặng;
đây là chỉ dẫn, không phải bằng chứng Harness đã nhận task.
Nếu restart gateway để lại thread ID đã lưu nhưng App Server mới không còn biết,
gatewayd sẽ xoá session cũ đó và thử lại chính turn ấy một lần trên thread mới.

Adapter giữ đối chiếu request/run cho event terminal và từ chối steer không thể
áp dụng cho thread active, thay vì âm thầm ghép sang nơi khác. Turn đã hoàn tất
không thể steer: message kế tiếp đi theo đường start/resume bình thường. Queue
ở tầng OS vẫn tồn tại vì an toàn và giao nhận, gồm sensing thụ động và lúc loa
đang bận; steering không phải cơ chế cancellation hay interruption tổng quát.

Nếu bridge đang kết nối bị rớt khi turn còn active, client phát lifecycle error
có tương quan (`codex gateway restarted; turn cancelled`) trước khi xoá tương
quan cục bộ. Nhờ vậy Monitor và web chat không hiện turn active vĩnh viễn; task
không tự gửi lại vì có thể đã tạo side effect.


## Nạp trước skill bằng Jev trong bridge được OS quản lý

Bridge runtime Codex dùng Jev chọn tối đa một skill đủ điều kiện trước khi chuyển yêu cầu người dùng chỉ có văn bản. Bridge đọc thư mục skill native do OS cài, `$CODEX_HOME/skills`, và thêm toàn bộ skill cùng đường dẫn thư mục vào yêu cầu đó. Đây là adapter của runtime, không phải bộ định tuyến intent phần cứng hay plugin được cài vào CLI upstream độc lập. Voice, Web/MQTT chat và hội thoại thông thường trên các kênh nhắn tin do OS quản lý cùng đi qua bridge này. CLI độc lập và chế độ coding-session trên Telegram chạy ngoài bridge, tiếp tục tìm skill theo cơ chế native.

Adapter dùng chung mã `system/lib/jevskills`, giới hạn ba giây và tiếp tục bình thường khi Jev không chọn, thiếu thông tin xác thực, trả kết quả không hợp lệ hoặc không đọc được skill. Tin hệ thống, slash command và attachments bỏ qua bước nạp trước. Khi chạy CLI, nếu toàn bộ preload cộng yêu cầu đạt giới hạn 128 KiB cho một đối số trên Linux, bridge bỏ preload, log `reason=argument_budget` rồi gửi yêu cầu gốc; không cắt bớt skill. Yêu cầu đã chuẩn bị dùng lại context khi retry do phiên cũ không còn; không ghi context vào AGENTS.md hay system prompt toàn cục. Runtime upstream có thể giữ lượt này trong lịch sử hội thoại thông thường; nội dung nạp trước ghi rõ chỉ áp dụng cho yêu cầu hiện tại.

**Jev mặc định tắt** cho runtime này trong khi chờ kiểm chứng native. Cờ build `const jevEnabled = false` nằm trong `runtimes/codex/gatewayd/jev.go`. Muốn bật sau kiểm chứng, đổi cờ Go rồi build và restart bằng luồng quản lý runtime; biến môi trường không bật được Jev. `JEV_CONFIG_PATH` mặc định là `/root/config/config.json`; thông tin xác thực được đọc từ file, không đưa vào context. Skill có metadata thực thi chưa hỗ trợ bị loại. Cấu hình native sai định dạng, cú pháp JSONC chưa hỗ trợ, biến môi trường đổi thư mục cấu hình và thư mục skill riêng của project cũng khiến adapter bỏ qua nạp trước. Adapter kiểm tra cấu hình ở thư mục cha để giữ chính sách kế thừa và thứ tự ưu tiên skill. Khi cấu hình native có điều khiển skill hoặc quyền tùy chỉnh, adapter nhường cho bộ nạp native thay vì bỏ qua chính sách đó. Việc chọn skill không cấp quyền thực thi hay thay đổi cơ chế phê duyệt tool hiện có.

Kiểm chứng local bao gồm cách ly yêu cầu, bỏ qua nguồn không phải người dùng và attachments, nhường khi có chính sách native, cùng dữ liệu gửi vào tiến trình runtime. Test dùng selector mock và tiến trình giả; Jev thật, hành vi model native và delivery trên thiết bị cần kiểm thử tích hợp riêng.
