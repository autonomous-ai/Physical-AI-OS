# Thêm một Agentic Runtime Backend

Cách thêm một "bộ não" agent mới (như OpenClaw, Hermes, PicoClaw) vào OS, và
**mọi thứ nó phải nối** để đạt parity. Viết từ bài học port Hermes — nơi quá
nhiều mảnh bị bỏ thành no-op âm thầm và cắn lại sau này (config không sống qua
factory-reset, skills mất không quay lại, đổi tên identity chẳng làm gì, skills
không tự cập nhật).

> **Một quy tắc duy nhất phải nhớ:** OS (`os-server`) là nền tảng; backend là bộ
> não thay được. Bất cứ thứ gì OpenClaw làm mà người dùng thấy được, backend mới
> phải hoặc làm theo, hoặc **quyết định bỏ một cách có ý thức — và ghi lý do
> trong comment.** No-op là một quyết định, không bao giờ là mặc định.

Nguồn chân lý cho hợp đồng: `system/domain/agent.go` (interface
`AgentGateway`). Doc này giải thích phần *nào* quan trọng và *cách* nối switch,
install, migration, skills, hooks, reset.

> **Nhóm docs agentic-backend:** file này (hợp đồng generic + cách thêm) ·
> [`hermes_vi.md`](hermes_vi.md) (Hermes, backend đầy đủ) ·
> [`picoclaw_vi.md`](picoclaw_vi.md) (PicoClaw, gateway chỉ-client kèm script install/presync) ·
> [`claudecode_vi.md`](claudecode_vi.md) (Claude Code sau một bridge cục bộ,
> channel plugin native cho Telegram/Discord, claude.ai OAuth login). Protocol/quirk
> đặc thù từng backend nằm ở các file kia; cơ chế generic + checklist nằm ở đây.

---

## 0. Mô hình tư duy

- `config.agent_runtime` (`/root/config/config.json`) chọn backend đang chạy.
- `system/agent/factory.go` `ProvideGateway` resolve lúc boot qua Wire DI:
  `config.agent_runtime` > ROBOT.md `gateway.default` > openclaw.
- **Seed-khi-rỗng:** lúc boot `device.ProvideService` gọi
  `SeedAgentRuntimeFromGateway` — khi `config.agent_runtime` rỗng/null **và**
  ROBOT.md `gateway.default` là runtime hợp lệ, giá trị đó được ghi vào config.json
  (idempotent; chỉ boot đầu của config fresh/legacy mới ghi). Khi đã có giá trị cụ
  thể trên đĩa, device **sở hữu** runtime của nó: dev đã set (qua switch hoặc sửa
  tay) thì để nguyên, và fallback-resolve ở trên thành no-op. Hệ quả: sửa
  `gateway.default` trong ROBOT.md về sau KHÔNG còn ảnh hưởng device đã seed — phải
  sửa config.json hoặc switch runtime.
- Switch lúc runtime đi qua một core — `device.Service.UpdateAgentRuntime` — kích
  bởi 3 trigger (MQTT `<runtime>.setup`, HTTP `/api/device/agent-runtime`, web
  Runtime section). Xem `docs/vi/agentic/hermes_vi.md` §10–§11.
- **Báo hiệu hoàn tất:** MQTT ack 3 pha cho mỗi `<runtime>.setup` — `starting`
  ngay lập tức, rồi `success` (publish *trước khi* os-server restart) hoặc
  `failure` (+error, switch-runtime đã tự rollback); uplink `info` mang
  `agent_runtime` + version từng runtime để confirm lại. HTTP
  `POST /api/device/agent-runtime` trả 200 = *chỉ là accepted* (switch chạy
  nền); nguồn truth là `GET /api/device/agent-runtime`, vì
  `config.agent_runtime` chỉ được persist sau khi switch land. Web Runtime
  section sau khi POST sẽ poll GET đó tới khi device báo đúng runtime đích
  (success) hoặc quá deadline 5 phút (hiển thị runtime thực đang active thay
  vì giá trị lạc quan).

---

## 1. Hợp đồng — implement `domain.AgentGateway`

Backend nằm ở `runtimes/<name>/`, `*Service` của nó phải thoả **toàn bộ**
interface `AgentGateway`. Các method chia nhóm:

| Nhóm | Ví dụ | Lập trường backend mới |
|------|-------|------------------------|
| **Turn lõi** | `SendChatMessage`, `SendSystemChatMessage`, `*WithImage`, `NextChatRunID`, `*WithRun`, `StartWS` | **BẮT BUỘC** — đây là agent. |
| **Sẵn sàng / busy** | `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`, `SetBusy`, `QueuePendingEvent` | **BẮT BUỘC** — os-server gate sensing theo đây. |
| **Định danh** | `Name`, `Version`, `GetSessionKey`/`SetSessionKey` | **BẮT BUỘC** — hiện ở web Status. |
| **HAL passthrough** | `SendToHALTTS*`, `StopTTS`, `SetVolume`, `StartHALVoice` | Thường giống nhau giữa các backend — share hoặc copy. |
| **Run markers** | `MarkGuardRun`/`Consume*`, `MarkBroadcastRun`, `MarkPoseBucketRun`, `MarkWebChatRun`, `MarkSilentRun`, `*PendingChatTrace*` | **BẮT BUỘC track** — os-server gắn nhãn turn theo runID; OS phụ thuộc chúng. |
| **Kênh** | `SupportedChannels`, `AddChannel`, `RefreshChannelConfig`, `PairWhatsapp`, `HasWhatsappSession`, `GetTelegram*`, `Broadcast`, `SendToUser*` | `SupportedChannels()` khai báo capability thật; `AddChannel`/`RefreshChannelConfig` **BẮT BUỘC** trả `domain.ErrChannelNotSupported` cho mọi kênh ngoài list (hết no-op âm thầm). Xem §9. |
| **Lifecycle / onboarding** | `SetupAgent`, `EnsureOnboarding`, `ResetAgent`, `RestartAgent`, `RefreshModelsConfig` | Quyết theo backend; ghi rõ lý do no-op. |
| **Cận-migration** | `UpdateIdentityName`, `StartSkillWatcher`, `WatchIdentity`, `StartModelSync`, `UpdatePrimaryModel`, `StartPrimaryModelWatch`, `CompactSession`, `NewSession`, `FetchChatHistory`, `WriteMCPEntry`/`RemoveMCPEntry`, `GetConfigJSON` | **Vùng nguy hiểm** — dễ no-op, đắt để phát hiện thiếu. Xem §4–§6. |

**Bài học (Hermes):** ~15 method bị stub no-op trong `runtimes/hermes/stubs.go`.
Một số đúng là N/A (`PairWhatsapp` — không có plugin). `WriteMCPEntry`/`RemoveMCPEntry`
ban đầu bị hoãn (`TODO(hermes-mcp)`) nhưng **sau đó được hiện thực** trên
`mcp_servers` của `config.yaml` (`runtimes/hermes/mcp.go`), kèm clone config→config
khi switch runtime (`system/agent/mcp_reconcile.go`). Nhưng `StartSkillWatcher`,
`UpdateIdentityName`, và đường config-sync là **gap chức năng**, không phải N/A —
ship dạng no-op và chỉ phát hiện khi skills bị cũ / đổi tên vô tác dụng / config
gãy sau reset. Soát mọi stub: ghi `// no-op because <lý do>` hoặc
`// TODO(<backend>-<feature>)`, đừng để thân hàm rỗng trơ.

**Không "thành công giả".** Method đúng-là-N/A mà có trả error thì **phải trả
`domain.ErrNotSupportedByRuntime`** (domain/agent.go), không bao giờ `nil` —
`nil` khiến caller tưởng thay đổi đã được áp dụng trong khi không có gì xảy ra.
Caller phân nhánh bằng `errors.Is`: đường save model/baseURL LLM
(`system/device/config_update.go`) log dạng informational rồi fallback
`EnsureOnboarding` (presync đọc lại `llm_*` từ config.json) — ở **cả hai** nhánh,
kể cả nhánh chỉ đổi model. Nhánh đó trước đây chỉ log, chấp nhận được khi mấy
runtime này còn ghim một alias model cố định; nhưng khi máy đã chạy được model
riêng của người dùng thì một lần lưu chỉ đổi model sẽ nằm im cho tới khi có thứ
khác restart gateway. Sentinel nghĩa là "không vá tại chỗ được", không bao giờ
có nghĩa "không cần làm gì". Còn endpoint
gw-config báo "no device-side config file" thay vì `{}`. Các stub hiện trả
sentinel: `UpdatePrimaryModel`, `RefreshModelsConfig`, `CompactSession`
(hermes + picoclaw), `GetConfigJSON` (chỉ hermes — picoclaw có config.json
thật). Cùng khuôn với rule `ErrChannelNotSupported` ở §9.

---

## 2. Đăng ký + nối switch

1. `domain/device.go`: thêm const `AgentRuntime<Name>` + entry trong `AgentRuntimes`.
2. `system/agent/factory.go`: thêm `case` trong `ProvideGateway`.
3. **Installer nhúng**: `runtimes/<name>/install.sh` + `install.go`
   (`//go:embed install.sh` → `runtimereg.Register(name, InstallScript)`).
4. `switch_runtime.sh` **generic** — không biết tên backend. **Đừng** sửa nó,
   imager, hay switch core của os-server để thêm backend.

Hợp đồng installer (`switch_runtime.sh` kỳ vọng):
- tạo systemd unit; khai tên nó ở `/usr/local/lib/os-runtimes/<name>/service` nếu
  khác `<name>.service`.
- tuỳ chọn drop `verify` hook ở `/usr/local/lib/os-runtimes/<name>/verify`
  (exit 0 = "đã cài & dùng được"). Giữ **rẻ** — xem §3 vì sao không được check quá tay.

### Luồng switch (chuyện gì xảy ra khi switch)

`device.Service.UpdateAgentRuntime` validate runtime, ghi nhận `old` đang chạy, rồi
chạy switcher dưới `systemd-run --wait` và **block chờ exit code**.
`config.agent_runtime` **chỉ được ghi sau khi exit 0 sạch** — nên crash/reboot giữa
chừng resolve về `old` vẫn đang cài, không có gì để revert. `switch-runtime <new>
<old>` (generic, `system/device/switch_runtime.sh`, materialize qua `go:embed` ra
`/usr/local/bin/switch-runtime`):

1. phân giải tên unit của `<new>` (mặc định `<new>.service`, hoặc tên khai trong
   `/usr/local/lib/os-runtimes/<new>/service`) và kiểm tra **đã cài VÀ dùng được** —
   unit tồn tại **và** `verify` hook pass (không có verify → chỉ cần unit). Nếu chưa
   thì chạy installer (bản embed trước, CDN fallback). Gỡ bẫy unit-mồ-côi (một
   `.service` còn sót mà binary đã mất).
2. chạy `/usr/local/bin/runtime-<new>-presync` (os-server materialize — §3).
3. `enable --now <new-unit>` rồi **assert nó thực sự active** (unit có thể enable
   sạch nhưng crash ngay vì thiếu binary). Nếu không lên mà vòng này chưa cài lại,
   **cài lại một lần rồi thử lại**. Xong mới stop unit cũ (tối đa 3 lần `disable
   --now`).
4. thoát 0 — **không** restart os-server (os-server đang block ở `--wait` sẽ ack kết
   quả thật rồi tự restart để `factory.go` resolve lại gateway). Khi lỗi, trap
   rollback chỉ restart unit **cũ**. Nó không bao giờ đụng `config.json`.

Nên `switch-runtime` hoàn toàn không-biết-backend — **không cần đụng
scripts/imager/setup.sh/switcher khi thêm backend.**

---

## 3. Quy tắc vàng: install-một-lần vs mỗi-switch (*activation gap*)

`install.sh` chạy **một lần** — `switch_runtime.sh` chỉ chạy nó lúc cài đầu hoặc
khi `verify` fail. Mọi switch sau đều **bị skip**.

> **Do đó: bất cứ thứ gì phải sống qua factory-reset, hoặc phải refresh khi OTA
> `os-server` thường, KHÔNG được chỉ ghi bởi `install.sh`. Phải được os-server
> materialize MỖI switch.**

Đây là **activation gap** và Hermes dính 2 lần:
- Một fix nhét trong `install.sh` (hoặc bất kỳ file `install.sh` ghi ra — `verify`
  hook, presync hook) **không bao giờ tới được máy đã cài** qua OTA: bản cũ trên
  disk vẫn pass `verify`, nên `install.sh` không re-run, nên bản mới không bao giờ
  đáp.

Mẫu fix (dùng cho mọi thứ có state):
- Đặt logic vào **presync hook** (`runtime-<name>-presync`).
- Nhúng: `//go:embed presync.sh` → `runtimereg.RegisterPresync(name, PresyncScript)`.
- os-server materialize nó mỗi switch (`system/device/runtime_installers.go`
  `materializePresync`, gọi trong switch flow cạnh `materializeInstaller`).
- `switch_runtime.sh` chạy `runtime-<name>-presync` ngay trước khi backend start.

Presync của Hermes (`runtimes/hermes/presync.sh`) giờ làm chủ **cả** model wiring
trong `config.yaml` (idempotent — coerce `model: ''` bị reset về map, khẳng định
structure `provider`/`custom_providers`, sync `llm_*`/secrets) **lẫn** restore
skills (chạy lại `claw migrate` khi `skills/openclaw-imports` rỗng). Giữ `verify`
chỉ-CLI (`command -v <bin>`) — structure-check trong `verify` sẽ ép full reinstall
nặng trong khi presync tự lành đủ rồi.

---

## 4. Migrate persona + memory (Go, chạy mỗi switch)

**Hub-and-spoke, không phải per-pair.** Migration đi qua một `PersonaBundle`
trung lập: mỗi runtime có MỘT adapter **read** (layout đĩa → bundle) và MỘT
adapter **write** (bundle → layout), trong
`system/agent/migrate_persona/runtime_<name>.go`. Migrate = `read[from] →
write[to]` (`RunMigration(from, to, opts)`). Nên thêm runtime = **đúng 1 file
adapter**, tự động chạy với mọi runtime sẵn có, cả 2 chiều — số file **tuyến tính
(2/runtime)**, không phải N×(N-1) như per-pair. Đăng ký adapter vào map `adapters`
trong `migrator.go`; không cần `Direction` enum mới. openclaw, hermes, picoclaw,
codex, claudecode, và opencode đều có adapter, nên mọi cặp migrate được cả 2 chiều. Runtime không có adapter bị
`CanMigrate` bỏ qua — bộ reconcile lúc boot không migrate tới/từ nó.

Interface `runtimeAdapter` (`migrator.go`) còn có 2 method không liên quan nội
dung migration, nhưng mọi adapter vẫn phải implement vì các phần khác của hệ
thống cũng dựa vào cùng interface này:

- `memoryFilePath(opts)` — nơi runtime giữ `MEMORY.md` mà nó load mỗi phiên.
  Memory guard của OS (`docs/os-server.md`, "Memory guard") quét file này cùng
  với `userProfilePath(opts)` lúc boot và mỗi lần ghi; runtime thiếu method này
  sẽ không được guard, đó là lý do nó nằm trên interface chứ không phải một bảng.
- `workspaceRoot(opts)` — thư mục HAL coi là workspace của runtime
  (`<root>/realtime/` chứa `summary.md` v.v.). `POST /api/agent/memory/reset`
  backup rồi xóa memory dưới thư mục này cho mọi runtime đã cài.

Adapter của PicoClaw (`runtime_picoclaw.go`) mirror layout openclaw nhưng đọc/ghi
`memory/MEMORY.md` (picoclaw để MEMORY.md trong `memory/`, không ở gốc workspace).
Lưu ý skills chiều VÀO vẫn do presync `picoclaw migrate --workspace-only`
(`picoclaw_vi.md` §1.1) lo — reconciler Go chỉ mang persona/memory, không mang skills
— nên khi switch VÀO picoclaw, hai bên trùng phần persona (cùng nguồn, vô hại) còn
presync là đường duy nhất mang skills.

> **Template copy-là-chạy:** `system/agent/migrate_persona/runtime_example.go` là
> skeleton build-ignored, comment đầy đủ — copy sang `runtime_<name>.go`, xóa dòng
> `//go:build ignore`, rồi điền 5 bước wiring + read/write. Nó ghi sẵn quyết định
> từng field (slot riêng vs inline vs fold) ngay trong comment.

Chạy lúc os-server boot sau switch thật (`Reconcile`, khi `agent_state.json`
prev ≠ current). Mỗi adapter mang gì:

- **SOUL.md** → file identity của backend. Nếu backend **không có slot IDENTITY.md
  riêng** (Hermes không có), inline các field IDENTITY đã điền của owner thành
  block `## Your identity card` trong SOUL (xem `buildIdentityBlock`).
- **MEMORY.md + `memory/*.md` daily + KNOWLEDGE.md** → merge vào file long-term
  memory của backend. **Trước hết kiểm tra backend LOAD file nào THEO TÊN** —
  Hermes chỉ load `MEMORY.md` + `USER.md` (không glob `memories/*.md`), nên một
  `KNOWLEDGE.md` riêng sẽ bị bỏ qua; ta fold nó vào `MEMORY.md`.
- **USER.md** → file user-profile của backend, qua `writeUserProfile` (KHÔNG
  phải `writeMemoryEntries`). USER.md vừa là bản ghi vừa là biểu mẫu: các field
  ĐƠN NHẤT (`Name`, `What to call them`, `Pronouns`, `Timezone`) dùng
  **replace-or-append** — mỗi field một bullet, bên đến thắng — còn lại giữ
  dedupe-union cộng dồn. Chỉ entry-merge thì `**Name:** Leo` và `**Name:** Long`
  là hai chuỗi khác nhau, nên profile chỉ thêm được tên chứ không bao giờ bỏ được
  tên cũ, và mỗi lần switch lại nhân đôi cặp đó (quan sát trên máy 2026-09-03).
  Hai bất biến được test khoá lại:
  - Field đến rỗng hoặc không có **không bao giờ xoá trắng** field đã điền ở đích
    — một nguồn chưa có profile không được phép xoá thứ thiết bị đã học được.
  - Chỉ FIELD bị retire. Văn xuôi tự do có nhắc tên người dùng cũ vẫn là nội dung
    đã học và được giữ; dọn phần đó là việc của prune theo enrollment, không phải
    của migration.
  Hãy coi USER.md là **biểu mẫu, không phải nhật ký**. Template của nó ship sẵn
  các slot trống (`- **Name:**`) kèm hướng dẫn điền vào, nên `writeUserProfile`
  điền **ngay tại chỗ slot đang đứng** rồi bỏ mọi bullet trùng field xuất hiện sau
  đó — đúng cách `setIdentityField` làm với IDENTITY.md. Toàn bộ template được giữ
  nguyên văn: câu hướng dẫn, các gợi ý Context, câu chốt "not building a dossier",
  slot còn để trống, và cả link `## Related`. USER.md là bootstrap file nên tất cả
  những thứ đó tới tay agent mỗi lượt, và nó chứa dòng duy nhất bảo agent phải duy
  trì chính file này. Đừng "dọn dẹp" template — việc của migration là giá trị, không
  phải cái khung.
- Đặt **`Overwrite = true`** cho copy soul khi switch: switch nghĩa là "lấy persona
  vừa dùng sang". `copyPersona` backup trước (`.bak-<nano>`).
- Chiều ngược phải **strip artifact riêng của backend** mà nó đã thêm (vd identity
  card — OpenClaw giữ tên trong IDENTITY.md riêng) **VÀ khôi phục thứ chiều xuôi đã
  inline về đúng slot gốc của nó**. Inline tên vào SOUL của Hermes nhưng chiều ngược
  chỉ strip thì **mất tên** — chiều ngược phải parse card và ghi field về OpenClaw
  `IDENTITY.md` (`restoreIdentityCard`, nghịch đảo của `ensureIdentityInlined`:
  thay-hoặc-thêm dòng, giữ template sẵn có; dựa trên cùng đảm bảo "onboard không
  ghi đè IDENTITY.md đã tồn tại" mà `UpdateIdentityName` dùng). Mỗi inline ở chiều
  xuôi cần một restore tương ứng ở chiều ngược, nếu không round-trip sẽ âm thầm mất
  state.

**ĐỪNG** mang file riêng-runtime: `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`,
`hooks/` — chúng thuộc runtime nguồn. **Bộ nhớ sâu** của backend (DB
episodic/semantic, dream-diary, grounded-short-term) **không portable** — bản
distilled `MEMORY.md`/`USER.md` mới là dạng mang đi được.

### Phân loại mỗi bước migration: fold vs move

Switch có round-trip không mất hay không là **tuỳ từng backend**, quyết định bởi
backend đích có slot nào. Đừng giả định đối xứng — phân loại mỗi bước xuôi:

- **Move / inline** (field sang slot *khác*, vd tên IDENTITY → inline vào SOUL):
  đảo ngược được, và chiều ngược **BẮT BUỘC** restore về slot gốc. Bỏ restore là âm
  thầm mất state — chính là bug mất tên. **Mỗi inline cần một restore tương ứng.**
- **Fold** (hai cấu trúc nguồn gộp vào *một* đích, vì backend đích thiếu slot cho
  một trong số đó): cố hữu mất **cấu trúc** (không mất dữ liệu), và **không có
  nghịch đảo trung thực** — đã gộp thì không tách lại được. Chỉ fold khi đích thực
  sự thiếu slot, và ghi nó như một asymmetry một-chiều đã biết **trong doc riêng
  của backend đó** (đây là tính chất của backend đó, không phải của framework
  migration — backend khác *có* slot sẽ map 1:1 và round-trip sạch).

Vậy tính đối xứng migration của một backend là fact về *slot của backend đó*, ghi
trong doc backend (vd `docs/agentic/hermes.md`), không phải đảm bảo chung ở đây.

---

## 5. Skills

- Skills tới backend bằng cách **copy** (kiểm tra: copy hay convert! `claw migrate`
  của Hermes là `shutil.copytree`, không transform) vào thư mục skill của backend.
- **Restore-sau-reset** thuộc **presync**, guard theo thư mục rỗng (để switch
  thường là no-op — không churn). Xem §3.
- **Skill watcher** (auto-update từ CDN, gate theo capability): plumbing generic
  fetch/extract/hash share ở `system/skills/skillzip.go`
  (`FetchSkillVersions`/`DownloadToTempFile`/`FolderHash`/`ExtractSkillZip`).
  Tạo thông báo đọc lại bằng `skills.SkillUpdatePrompt(agentVisibleSkillsDir, changedSkills)`
  trong `system/skills/skill_update_prompt.go`; cả sáu runtime dùng chung nội dung và
  định dạng danh sách. Giữ đường dẫn riêng, kiểm tra danh sách rỗng, logging
  và gửi qua `SendSystemChatMessage` ở từng runtime.
  Sau khi đọc lại, agent trả đúng `NO_REPLY`; cập nhật nội bộ không cần đọc
  lời xác nhận cho người dùng. Thêm `runtimes/<name>/skill_watcher.go` mỏng song song với
  `runtimes/openclaw/skill_watcher.go` — chỉ khác **thư mục đích** và **đường
  notify**. Gate bằng `skills.Supported(device.Capabilities(...))`. Chỉ cập nhật
  version đã theo dõi sau khi download và extract thành công, để lỗi tạm thời được
  retry ở poll kế tiếp. Notify agent bằng `SendSystemChatMessage`.

---

### Nạp trước skill bằng Jev trên các runtime

Bước nạp skill nằm trong hook hoặc bridge do OS quản lý của từng runtime, sau
routing intent phần cứng. Đây là tích hợp riêng với `jev_intent`: chọn skill
không thực thi tool và không cấp quyền. Giữ plugin native hiện tại của Hermes
khi thêm runtime khác; không phân loại cùng lượt thêm lần nữa ở os-server.

- Hermes dùng hook native `pre_llm_call` và loader `skill_view`.
- PicoClaw dùng process hook native `before_llm` và danh sách skill do system
  message của runtime công bố. Nó dùng chung bộ chọn Python của Hermes.
- Codex, Claude Code và OpenCode nạp skill trong bridge do OS quản lý. Các bridge
  dùng thư viện `system/lib/jevskills`, với adapter và kiểm tra policy riêng từng runtime.
  Phạm vi gồm chat thông thường qua bridge; CLI độc lập và session Telegram
  `/coding` giữ cơ chế tìm skill native.
- Plugin native OpenClaw cùng yêu cầu opt-in/tương thích được mô tả tại
  [tài liệu Jev OpenClaw](openclaw_vi.md).

Các adapter Go gọi proxy OS đã cấu hình tại `llm_base_url + /jev/decisions`, dùng
`llm_api_key`; chỉ text yêu cầu hiện tại và tên/mô tả skill rời thiết bị. Nội dung
skill được đọc local. Với envelope voice đã nhận diện, bộ chọn chỉ phân loại
instruction có thẩm quyền; bỏ transcript mâu thuẫn và metadata routing/kết quả
Harness được nối thêm. Envelope rỗng/sai định dạng và câu phụ thuộc ngữ cảnh
(như “brighter”, “continue”) chuyển cho runtime. Yêu cầu gốc chuyển tiếp không đổi. Không retry, redirect, fallback provider trực tiếp hoặc
upload lịch sử hội thoại. Tổng ngân sách chọn tối đa 3 giây, cooldown lỗi 30 giây
và bỏ qua ngay khi bộ chọn đang bận. Ngưỡng xác suất >= 0.70, margin >= 0.20 và
fit độc lập >= 0.60. Hơn 32 skill đủ điều kiện thì abstain, không cắt danh sách.
Đây là ngưỡng tìm skill, khác ngưỡng intent phần cứng nghiêm ngặt hơn.

Preload chứa toàn bộ SKILL.md trong giới hạn kích thước và thư mục tuyệt đối.
Adapter Go chỉ nạp skill tĩnh đơn giản từ thư mục cài đặt của runtime; template
động, frontmatter điều khiển riêng, thư mục skill project xung đột và policy
native chưa hỗ trợ đều chuyển về tìm skill native. Sau inference, adapter kiểm
tra lại điều kiện và nội dung skill. Lượt system/slash/attachments giữ đường cũ.
Các tích hợp mới ngoài Hermes đều **mặc định tắt** trong khi chờ kiểm chứng native;
Hermes giữ nguyên. Mỗi runtime mới có cờ build Go `const jevEnabled = false`: bridge dùng `gatewayd/jev.go`, PicoClaw dùng `jev_hook.go`, OpenClaw dùng `jev_plugin.go`. Khi tắt, bridge không tạo selector; onboarding native không cài asset hay tạo đăng ký Jev mới. Nếu có đăng ký Jev cũ, Go chỉ tắt đăng ký đó; các cấu hình khác được giữ nguyên. Không đọc skill, gọi provider hay thêm nội dung Jev vào yêu cầu khi tắt. Muốn bật phải đổi cờ, build và restart qua luồng quản lý runtime; env/config không thay thế cờ build. `JEV_CONFIG_PATH` chọn file cấu hình OS của bridge.

Gắn kết quả với đúng yêu cầu, giữ source/session và chỉ tái sử dụng khi retry
chính yêu cầu đó. Không ghi skill đã chọn vào AGENTS.md hay persona. Lịch sử
native có thể giữ nội dung preload như một lần đọc skill thông thường; điều đó
không chọn skill cho lượt sau. Test bằng mock các nhánh selected/abstain/error,
opt-out native, timeout, tool loop, retry, cách ly session và routing theo
source/attachments trước khi phát hành.

## 6. Hooks — reimplement phía OS cho Hermes (ví dụ mẫu)

Hooks OpenClaw (`hooks/<name>/{HOOK.md, handler.ts}`) là handler TypeScript fire
trên event `message:preprocessed` của OpenClaw — `emotion-acknowledge` (mặt
"thinking" ngay khi nhận tin) và `turn-gate` (set busy cho turn từ kênh). Chúng
**riêng-runtime**, không portable. Backend mới **không** thừa kế chúng.

**Vì sao OpenClaw cần hook còn Hermes thì không.** Trong OpenClaw mọi turn chạy
bên trong daemon; os-server đẩy tin qua WebSocket rồi mất dấu, nên chỗ duy nhất để
chen "thinking" đúng thời điểm là hook. Với Hermes, điểm chặn đã nằm phía Go —
**mọi turn gửi tới Hermes đều đi qua `runtimes/hermes/chat.go:sendChat`** (voice,
sensing, web, và cả Telegram — receive loop nằm phía Lumi, xem `telegramRunOrigin`).
Nên ta reimplement hook native bằng Go và fire từ `sendChat`, thay vì materialize
file hook vào workspace (Hermes không có onboarding Go để làm việc đó, và không có
loader `~/.hermes/hooks/HOOK.yaml` để chạy — `handler.ts` mà `claw migrate` copy
vào là đồ chết dưới Hermes).

Đã làm:
- **`emotion-acknowledge` → Go native** trong `runtimes/hermes/emotion_ack.go`
  (`fireAckEmotion`, gọi từ `sendChat`). Mirror `handler.ts` 1:1: cùng emotion
  (`thinking`, intensity `0.7`), cùng skip prefix
  (`[sensing:`/`[activity]`/`[emotion]`/`[speech_emotion]` + rỗng), và cap-gate đi
  qua **registry dùng chung** `skills.SupportedHooks`
  (→ `HookCapability["emotion-acknowledge"] = expression`), resolve một lần lúc
  khởi tạo (`Service.ackHookEnabled`). Skip `[HANDLED]` của hook TS ánh xạ sang
  check Go-native `IsSilentRun(runID)` — os-server báo turn realtime-handled qua
  `MarkSilentRun`, không phải marker trong body.
- **`turn-gate` → không mirror (thừa).** `sendChat` đã set busy
  (`busySince`/`activeTurn`) trước round-trip mạng, nên gate riêng sẽ trùng lặp.

> ⚠️ **Coupling bảo trì — không có liên kết compile-time.** `runtimes/openclaw/hooks/emotion-acknowledge/
> handler.ts` (OpenClaw) và các file `emotion_ack.go` trong
> `runtimes/{hermes,picoclaw,codex,claudecode,opencode}` là các bản cài đặt
> độc lập của cùng một hành vi. **Sửa một cái phải sửa tất cả** — skip rules,
> tên/intensity emotion, và cap-gate phải y hệt, nếu không các backend lệch
> nhau trong im lặng. Giữ comment chéo trong mọi file.

**Khi nào *mới cần* hook native (Python).** Chỉ cho turn phát sinh **bên trong**
backend và không bao giờ qua `sendChat` — ví dụ heartbeat của backend, hoặc một
kênh backend tự lắng nghe trực tiếp (không do Lumi proxy). Lamp hiện chưa có turn
loại này, nên path Python-plugin (`pre_gateway_dispatch` trong `~/.hermes/plugins/`,
do Hermes discover; **không có** loader `~/.hermes/hooks/HOOK.yaml` trong bản ship
— verify loader thật trước khi giả định) được **hoãn theo YAGNI**, không phải gap
parity. Chỉ làm khi xuất hiện nguồn turn như vậy.

---

## 7. Factory reset

- Implement wipe của backend **trong `ResetAgent()`** (`runtimes/<name>/reset.go`).
  `server/system/factoryreset.go` resolve **gateway đang active** rồi gọi
  `gw.ResetAgent()` — **không có `switch` per-backend** để đồng bộ (thêm backend =
  implement `ResetAgent`, không đụng `server/system`). Backend mà state do bên
  ngoài sở hữu (PicoClaw) ship **`ResetAgent` no-op** → để yên đúng (switch cũ
  default PicoClaw về wipe OpenClaw — latent bug, refactor này xoá). Primitive dùng
  chung duy nhất `osreset.WipePath` nằm ở `lib/osreset` để backend dùng được mà
  không phải import `server/system`.
- **Persona được wipe hộ bạn — nhưng chỉ khi `personaPaths` đúng.**
  `ResetAgent()` chỉ với tới backend *đang chạy*, trong khi file persona là các
  **bản sao**: mỗi lần switch runtime, SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE được
  migrate sang đích và bản của nguồn vẫn nằm nguyên đó — nên một thiết bị đã từng
  đổi backend giữ cùng một profile ở nhiều cây thư mục. Chỉ dọn cây đang active
  thì lần switch kế tiếp sẽ migrate ngược một bản cũ vào (quan sát trên máy
  2026-09-03: tên một chủ cũ nằm trong bốn file `USER.md` giống hệt nhau từng
  byte, cũ nhất từ 2026-07-08, và đèn vẫn gọi tên đó).
  Vì vậy `factoryreset.go` còn gọi `migratepersona.PersonaPaths`, hàm này union
  `personaPaths(opts)` của **mọi** adapter đã đăng ký. Method đó nằm trên
  interface `runtimeAdapter` nên compiler bắt bạn phải viết — nhưng nó không kiểm
  được bạn có liệt kê đúng file hay không. Hai quy tắc:
  - Liệt kê **file**, cộng thư mục `memory/`. **Không bao giờ liệt kê workspace
    hay home root** — ở đó còn `skills/`, `configs/`, và với Hermes là cả bản cài
    đặt. `TestPersonaPathsNeverWipeARuntimeRoot` chặn việc này.
  - Trả về `nil` vẫn compile được và âm thầm để profile sống sót qua reset;
    `TestEveryAdapterContributesPersonaPaths` bắt lỗi đó.
- **Wipe `/root/config/agent_state.json` khoá-bước với `config.json`** — chúng là
  một cặp (runtime hiện tại + lịch sử switch). Để lại `agent_state.json` trong khi
  `config.json` reset làm `prev` cũ lệch với `current` bị reset → kích **migration
  persona giả** lan state đã-wipe/stub.
- Giữ thứ phải sống (`bootstrap.json` = state OTA).
- Một wipe path xoá nội dung đã migrate (skills, config) phải có **restore path
  chạy SAU reset** (presync, §3/§5) — không thì nội dung mất luôn khi `install.sh`
  ngừng re-run.

---

## 8. Gate theo capability

Dùng metadata nền tảng runtime-agnostic trong `system/skills`:
- `skills.Supported(deviceCaps)` cho skills, `skills.SupportedHooks(deviceCaps)`
  cho hooks, với `deviceCaps = device.Capabilities(config.DeviceTypeOrDefault())`.
- Đừng hardcode danh sách skill/hook theo backend — gate y như OpenClaw.

---

## 9. Kênh — mô hình capability + re-apply khi switch

Kênh là một **capability được khai báo**, không phải best-effort âm thầm. Mỗi
backend tuyên bố nó chạy được những kênh nhắn tin nào, và OS dùng khai báo đó để
vừa từ chối các setup không hỗ trợ ngay từ đầu, vừa re-apply những kênh còn sống
khi runtime đổi.

### Khai báo capability

- **`SupportedChannels() []string`** (method mới của `AgentGateway`,
  `domain/agent.go`) — trả các kênh runtime chạy được. Theo backend:
  - openclaw → `[telegram, slack, discord, whatsapp]` (`runtimes/openclaw/channels.go`)
  - hermes → `[telegram, slack, discord]` (`runtimes/hermes/channels.go`)
  - picoclaw → `[telegram]` (`runtimes/picoclaw/channels.go`)
  - claudecode → `[telegram, slack, discord]` (`runtimes/claudecode/channels.go`
    — tất cả do device sở hữu, mirror codex: loop getUpdates
    `telegram_poll.go`, session discordgo `discord.go`, bridge MQTT `slack.go`;
    channel plugin native của Claude Code cố ý không dùng)
  - opencode → `[telegram, slack, discord]` (`runtimes/opencode/channels.go`
    — tất cả do device sở hữu, mirror codex/claudecode)
- Helper `domain.ChannelSupported(gw, channel) bool` (`domain/channel.go`) — chỗ
  duy nhất caller kiểm tra thành viên.
- Sentinel dùng chung trong package `domain` (`domain/channel.go`):
  `ErrChannelNotSupported` (`"channel_not_supported"`) và
  `ErrChannelCredentialsMissing` (`"channel_credentials_missing"`). Được so sánh
  bởi các runtime, lớp device, và các MQTT handler, nên ai cũng test cùng một giá
  trị.

> **Hết no-op âm thầm.** Hành vi cũ là backend nhận mọi kênh rồi lặng lẽ trả `nil`
> cho kênh nó không chạy được. Điều đó giấu config chết. Nay
> `AddChannel`/`RefreshChannelConfig` **BẮT BUỘC** trả `domain.ErrChannelNotSupported`
> cho kênh không có trong `SupportedChannels()`.

### `AddChannel` / `RefreshChannelConfig` nhận biết capability

Kênh **được hỗ trợ** thì áp dụng; kênh **không hỗ trợ** thì gateway trả
`domain.ErrChannelNotSupported`. Lớp device (`system/device/channels.go`
`AddChannel`) giờ gate theo `SupportedChannels()` **TRƯỚC** khi persist
credentials, nên một kênh không hỗ trợ không bao giờ để lại token chết trong
`config.json`. Thứ tự là:

1. **Cổng capability** — `domain.ChannelSupported(gw, channel)`; từ chối với
   `ErrChannelNotSupported` trước khi đụng `config.json`.
2. **Persist creds → `config.json`** (token của kênh).
3. **`gateway.AddChannel`** — áp dụng trong runtime đang chạy.

Persist-**trước**-apply quan trọng vì một apply đọc-config (presync của hermes
đọc lại `config.json` để dựng lại `~/.hermes/.env`) phải thấy token mới, và nếu
apply lỗi tạm thời thì creds vẫn được persist — đúng chiều hồi phục được (presync
lúc boot / `ChannelReconcile` re-apply lại).

### Tự lành khi switch: `ChannelReconcile`

`ChannelReconcile` (`system/agent/channel_reconcile.go`) là anh em của
`PersonaMigration`. Nó chạy trong startup sequence **ngay sau**
`personaMigration.Reconcile()` (`server/config_watch.go`). Nó **không chặn**.

Khi **switch runtime** — phát hiện khi `config.AgentRuntime` !=
`config.ChannelsAppliedRuntime` — nó re-apply từng kênh đã cấu hình trong
`config.json` sang runtime mới qua `AddChannel` của gateway (**đường provision
đầy đủ**). Ca quan trọng là switch **sang openclaw**: plugin slack/discord của nó
được cài theo nhu cầu bởi `AddChannel`, nên refresh chỉ-config sẽ **không** cài
chúng (`install.sh` không pre-bundle các plugin đó). Switch sang hermes phần lớn
đã tự lành — presync của nó re-sync `.env` trước khi gateway start, nên re-apply
là no-op idempotent.

- Các kênh runtime mới **không chạy được** được gom vào
  `config.ChannelsUnsupported` và hiện trên info uplink MQTT (field
  `unsupported_channels` trên `MQTTInfoResponse`, `domain/device.go`).
- Creds của kênh không hỗ trợ được **giữ lại trong `config.json`**, nên switch
  ngược lại sẽ khôi phục chúng.
- Marker `config.ChannelsAppliedRuntime` tiến **một lần mỗi switch khi pass
  sạch**; apply lỗi tạm thời để marker chưa tiến nên boot sau retry.

### Field `config.json` mới

`channels_applied_runtime` và `channels_unsupported`
(`server/config/config.go`).

### Khi thêm runtime mới

- Implement `SupportedChannels()` để khai báo capability **thật**.
- Cho `AddChannel`/`RefreshChannelConfig` trả `domain.ErrChannelNotSupported` cho
  mọi thứ không có trong list — không bao giờ no-op âm thầm.
- **Telegram do device sở hữu** trên hermes/picoclaw: receive loop được driven bởi
  `config.TelegramBotToken`, nên runtime không cần ghi gì — một success no-op
  trung thực trong `AddChannel`/`RefreshChannelConfig` (vẫn gate theo list).
  Trên claudecode các loop do **runtime sở hữu** (channel plugin của Claude
  Code): `AddChannel` chạy lại presync nhúng, presync ghi token + allowlist vào
  `~/.claude/channels/<ch>/` và chỉ restart bridge khi có hash-diff
  (xem `docs/vi/agentic/claudecode_vi.md` §7).

---

## Checklist cho backend mới

- [ ] Package `runtimes/<name>/`; `*Service` implement **toàn bộ** `AgentGateway`.
- [ ] Mọi stub đều `// no-op because …` hoặc `// TODO(<name>-…)` — không thân rỗng.
- [ ] Stub N/A có trả error thì trả `domain.ErrNotSupportedByRuntime`,
      không bao giờ `nil` (§4 "Không thành công giả").
- [ ] `domain.AgentRuntime<Name>` + entry `AgentRuntimes`; `factory.go` case.
- [ ] `install.sh` + `install.go` (`//go:embed` + `runtimereg.Register`).
- [ ] **Setup có-state → `presync.sh`** (`//go:embed` + `runtimereg.RegisterPresync`),
      materialize bởi os-server mỗi switch. Không gì reset-fragile chỉ nằm trong
      `install.sh`.
- [ ] `verify` hook rẻ (CLI có mặt), không phải structure-check.
- [ ] `migrate_persona/runtime_<name>.go` (copy `runtime_example.go`): MỘT adapter
      read + MỘT write (bundle ↔ layout), đăng ký vào map `adapters`. `read` xuất SOUL, field identity,
      MEMORY/USER, và slot KNOWLEDGE/daily nếu có; `write` restore từng cái về slot
      gốc (identity → file riêng, hoặc inline nếu không có slot) và fold slot mà
      backend thiếu. `Overwrite=true` cho SOUL. Không cần `Direction` enum mới.
- [ ] Skills: copy-import + **restore-trong-presync** (guard) + `skill_watcher.go`
      (song song openclaw, share `system/skills/skillzip.go`).
- [ ] Hooks: native-backend hoặc OS-side — đã quyết & ghi (không thiếu âm thầm).
      Nếu reimplement OS-side bằng Go (không liên kết compile-time với hook TS),
      thêm comment chéo trong cả hai file để sửa cái này cảnh báo cái kia.
- [ ] `ResetAgent()` trong `runtimes/<name>/reset.go` (factory-reset gọi
      `gw.ResetAgent()` trên gateway active — không có switch ở `factoryreset.go`); **`agent_state.json` wipe cùng
      `config.json`**.
- [ ] `personaPaths(opts)` trên persona adapter liệt kê **file** persona của
      runtime này + thư mục `memory/` — không bao giờ workspace/home root — để
      factory reset dọn profile ở *mọi* cây thư mục, không chỉ cây active (§7).
- [ ] `userProfilePath(opts)` trỏ đúng `USER.md` thật của runtime (Hermes để ở
      `memories/`) để reconcile theo enrollment lúc khởi động có thể retire
      profile của người không còn enrollment khuôn mặt/giọng nói (§7).
- [ ] Adapter implement memoryFilePath + workspaceRoot; chạy
      `go test ./system/agent/migrate_persona/` (`TestMemoryFilePathsCoverEveryAdapter`).
- [ ] **People sync** trong block hướng dẫn OS-managed của chính runtime này:
      giữ mục `## Users` trong `USER.md` luôn mới, dạng `- **<label> (friend)**: …`
      khoá theo enrollment label, chỉ thêm/cập nhật, không bao giờ gán chéo người,
      không điền `**Name:**`. Vị trí đặt khác nhau tùy runtime — HEARTBEAT.md nếu
      có heartbeat loop, CLAUDE.md cho claudecode (không có loop), block SOUL.md
      cho hermes (không loop, không KNOWLEDGE.md).
      `TestEveryRuntimeTeachesThePeopleSync` fail nếu một runtime thiếu nó.
- [ ] **Soul của device được inject mỗi lần boot**, lấy từ
      `device.ResolveSoul(deviceType)` (`system/device/soul.go`) — bộ phân giải
      `soul_ref` dùng chung. Làm trong onboarding, không chỉ trong factory reset,
      và KHÔNG dựa vào persona migration: migration chép từ runtime TRƯỚC ĐÓ, nên
      một device boot thẳng vào runtime của bạn không có gì để chép và lên nguồn
      với persona rỗng. Hermes từng ship như vậy và lamp mất toàn bộ luật định
      tuyến skill (`[sensing:*]` → `skills/sensing/SKILL.md`) cho tới khi
      `ensureSoulMDBlock` được thêm.
- [ ] **Bỏ soul mặc định của chính backend** trước khi giữ phần nằm dưới block
      của bạn. Đa số backend tự seed lại persona mặc định mỗi khi file prompt của
      nó biến mất, và presync chạy trước onboarding — nên một máy vừa flash đưa
      cho bạn cái seed đó chứ không phải file rỗng. Giữ lại thì nó thành persona
      thứ hai mâu thuẫn với persona thiết bị. Phải kiểm hình dạng trên máy thật:
      seed của Hermes mở đầu bằng văn xuôi chứ không phải heading, nên
      `managedDefaultSoulPrefixes` là danh sách prefix, trong khi openclaw chỉ cần
      `isDefaultSoulHeading`.
- [ ] **Mỗi block OS-managed một delimiter riêng.** Mọi runtime đều bọc block của
      mình bằng `<!-- OS DO NOT REMOVE -->`…`---`. Nếu runtime của bạn sở hữu
      block THỨ HAI trong cùng một file, hãy cho nó marker riêng — dùng chung
      marker sẽ khiến mỗi bên strip mất block của bên kia. Hermes dính đúng lỗi
      này: block skill-priority xoá mất persona ở lần boot kế tiếp.
- [ ] Gate capability qua `skills.Supported` / `SupportedHooks`.
- [ ] **Kênh (§9):** `SupportedChannels()` khai báo capability thật;
      `AddChannel`/`RefreshChannelConfig` trả `domain.ErrChannelNotSupported` cho
      kênh ngoài list (không no-op âm thầm). Telegram do device sở hữu trên
      hermes/picoclaw (success no-op, không ghi runtime); do runtime sở hữu trên
      claudecode (presync re-sync + restart theo hash-diff).
- [ ] Notify agent khi skill đổi qua `SendSystemChatMessage`.
- [ ] Docs: cập nhật backend doc kiểu `docs/agentic/hermes.md` + checklist này nếu hợp đồng đổi.

---

## Trạng thái parity Hermes (sổ trung thực)

**Xong:** nối switch, install nhúng + presync (config self-heal, restore skills),
migrate persona/memory (SOUL + inline identity, MEMORY + daily + KNOWLEDGE, USER),
`UpdateIdentityName`, skill watcher, factory-reset khoá-bước `agent_state.json`,
hooks (`emotion-acknowledge` reimplement OS-side bằng Go; `turn-gate` thừa — §6),
`WriteMCPEntry`/`RemoveMCPEntry` (`mcp_servers` trong `config.yaml`) + clone MCP
khi-switch (`MCPReconcile`).

**Còn mở / no-op:** hook native (Python) cho nguồn turn nội-bộ-backend (hoãn YAGNI
— §6), `CompactSession`, `FetchChatHistory`, và nhóm model-sync (`StartModelSync`,
`UpdatePrimaryModel`, `StartPrimaryModelWatch`, `RefreshModelsConfig` — phần lớn N/A
vì os-server gửi model cố định tới custom provider campaign-api). Các stub có trả
error (`CompactSession`, `UpdatePrimaryModel`, `RefreshModelsConfig`,
`GetConfigJSON`) trả `domain.ErrNotSupportedByRuntime` thay vì thành công giả (§4).
