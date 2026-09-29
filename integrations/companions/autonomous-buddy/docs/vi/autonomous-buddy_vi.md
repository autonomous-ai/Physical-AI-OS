# Autonomous Buddy — Remote Computer Use cho thiết bị

> **Trạng thái:** Đang thiết kế — đang scope MVP
> **Cập nhật:** 2026-05-21
> **Owner:** Leo
> **Liên quan:** [Kế hoạch MVP Autonomous Buddy](./autonomous-buddy-mvp_vi.md)

Tài liệu này lưu lại toàn bộ thảo luận thiết kế tính năng **Autonomous Buddy**: một app native chạy thường trú trên máy tính người dùng, cho phép thiết bị điều khiển desktop (mở app, vào web, gõ text, …) — kiểu TeamViewer nhưng được điều khiển bằng giọng nói/AI thông qua thiết bị.

Kế hoạch implement MVP nằm ở [`autonomous-buddy-mvp_vi.md`](./autonomous-buddy-mvp_vi.md). Doc này là tham chiếu dài về *lý do tại sao* kiến trúc lại như vậy.

### Một app: agent management và computer use (tháng 9/2026)

[Autonomous Buddy](./agent-manager_vi.md) hiện đóng gói workspace Electron/React và helper Swift native trong một `Autonomous Buddy.app`. Electron quản lý project, worktree, session, terminal và Git review; Swift giữ pairing, device WebSocket và executor computer-use. Khi chạy embedded, helper giữ icon menu bar native, không thêm Dock icon, dùng pipe JSONL riêng của process con và thoát khi Electron đóng pipe đầu vào. Đóng workspace vẫn giữ Buddy chạy; menu bar có thể mở lại Agent Manager hoặc thoát toàn bộ app. **Computer & device** trong app chính cung cấp pairing, trạng thái, pause, quản lý quyền và Activity. `make build` / `make install` build và cài cả hai thành phần cùng nhau; người dùng chỉ cài một app. Command `agent.*` từ device đã pair route vào managed session bằng project/session ID tường minh, receipt request và event history có giới hạn; notice hoàn tất/cần chú ý trả qua cùng WebSocket. Xem [contract native bridge](./native-bridge_vi.md). Kiểm chứng lamp thật vẫn tách biệt với test transport giả.

`make dmg` mặc định build bộ cài riêng cho Apple Silicon (`arm64`) và Intel (`x64`). Metadata OTA lưu version và URL tải riêng theo kiến trúc; Buddy hiện chưa có updater trong app. Target upload mặc định ký số và notarize cả hai bộ cài; `make dmg` local không notarize. Xem [ký số và upload release](./release-signing_vi.md) để chọn kiến trúc, ký số và xuất bản.

[Hướng dẫn cài đặt desktop](./settings_vi.md) mô tả mục Settings / ⌘, trong menu macOS, tìm kiếm Appearance, lưu theme/font/zoom, giao diện terminal và bật tắt footer.

Thiết kế tháng 5 bên dưới là bối cảnh computer-use lịch sử, không phải contract đóng gói/UI hiện tại. Quyết định Swift-only/menu-bar được thay bằng kiến trúc một app này. Xem [contract agent manager](./agent-manager_vi.md) để biết IPC và vòng đời hiện tại.

---

## 1. Mục tiêu & không phải mục tiêu

### Mục tiêu
- Thiết bị điều khiển được máy tính qua voice ("mở Chrome", "vào Gmail", "join Google Meet", "gõ X", "đóng Slack")
- Hoạt động với mọi app macOS (không chỉ browser)
- Command computer-use và xác nhận pairing chạy qua LAN; client MQTT có quyền có thể yêu cầu mã pair, thu hồi pairing, query hoặc theo dõi trạng thái Buddy.
- Mac-first cho MVP; Windows/Linux để v1.2+

### Không phải mục tiêu (MVP)
- Điều khiển dựa trên hình ảnh (Claude Computer Use API loop screenshot → click)
- Stream màn hình real-time về thiết bị
- Hỗ trợ Windows / Linux
- Nhiều buddy trên 1 thiết bị (gia đình nhiều máy)
- Binary có chữ ký Apple / notarize — right-click → Open là OK với MVP self-host
- AppleScript executor vượt khuôn khổ vài case có sẵn
- Chạy shell command tùy ý (quá nguy hiểm, để sau với flag rõ ràng)

---

## 2. Use case (MVP)

| Voice (ví dụ) | Chuỗi action |
|---------------|--------------|
| "Mở Chrome và vào Gmail" | `open_app(Google Chrome)` → `open_url(https://gmail.com)` |
| "Tự động join Google Meet" | `open_url(https://meet.google.com/<last-or-configured>)` |
| "Mở Spotify" | `open_app(Spotify)` |
| "Gõ 'hello world' vào ô đang focus" | `type_text("hello world")` |
| "Đóng Slack" | `close_app(Slack)` |
| "Bật Do Not Disturb" | `applescript(...)` (whitelisted) |
| "Hiện noti 'meeting in 5 min'" | `notification(title, body)` |

Use case MVP KHÔNG hỗ trợ (chờ phase vision):
- "Đọc email mới nhất tóm tắt cho tôi" (cần đọc màn hình)
- "Search Google nội dung … rồi click kết quả đầu" (cần click theo vision)
- "Highlight đoạn này rồi copy" (cần biết trạng thái selection)

---

## 3. Kiến trúc

```
┌─────────────────────────┐         ┌────────────────────────────┐
│  User                   │         │  Mac (máy người dùng)      │
│    │ voice              │         │  ┌──────────────────────┐  │
│    ▼                    │         │  │ autonomous-buddy.app │  │
│  ┌─────────────────┐    │         │  │ (Swift, menu bar)    │  │
│  │ Lamp (Pi)       │    │         │  │                      │  │
│  │                 │    │         │  │  ┌────────────────┐  │  │
│  │  lelamp (Py)    │    │         │  │  │ Pairing & WS   │  │  │
│  │    └─ STT/TTS   │    │ ◀──WS───┼──┼──┤ client         │  │  │
│  │                 │    │         │  │  └────────────────┘  │  │
│  │  lamp (Go)      │    │         │  │  ┌────────────────┐  │  │
│  │    ├─ OpenClaw  │    │         │  │  │ Executors      │  │  │
│  │    │   └─ skill │    │         │  │  │  NSWorkspace   │  │  │
│  │    │   "compu-  │    │         │  │  │  CGEvent       │  │  │
│  │    │    ter-use"│    │         │  │  │  AppleScript   │  │  │
│  │    └─ buddy svc │    │         │  │  └────────────────┘  │  │
│  └─────────────────┘    │         │  └──────────────────────┘  │
│         ▲               │         │            │               │
│         │ mDNS          │         │            ▼               │
│         │ lamp-xxxx     │         │   App macOS (Chrome,       │
│         │ .local        │         │   Finder, Spotify, …)      │
└─────────────────────────┘         └────────────────────────────┘
```

### Luồng happy-path

1. User nói: "Mở Chrome trên máy tính và vào Gmail"
2. Mic ở lelamp → STT
3. Transcript → OpenClaw → match skill `computer-use`
4. Skill parse thành command, gọi Lamp Go:
   `POST /api/buddy/command {action:"open_app", params:{app:"Google Chrome"}}`
   `POST /api/buddy/command {action:"open_url", params:{url:"https://gmail.com"}}`
5. Buddy dispatcher của thiết bị (Go) tra cứu buddy đã pair trong registry WS, gửi command qua WS đang mở
6. autonomous-buddy decode JSON, dispatch cho executor (NSWorkspace, CGEvent, …), thực thi
7. Buddy trả về `{ok:true}` qua cùng WS
8. Skill nhận kết quả, trả về câu confirm TTS-friendly
9. lelamp đọc: "Đã mở Gmail trên máy của bạn rồi"

### Hướng kết nối: buddy → thiết bị

**Buddy là WS client. Thiết bị là WS server.** Lý do:

- Buddy không cần mở port nào. Firewall Mac không bị động chạm. Attack surface thấp.
- Thiết bị đã có hostname mDNS ổn định (`<device_type>-xxxx.local`, ví dụ `lamp-a1b2.local`). Buddy resolve và connect.
- WS persistent duy nhất → latency command = 1 round-trip (không có cold-start TCP/TLS mỗi command).
- Logic reconnect nằm ở buddy (đơn giản — buddy phát hiện thiết bị reboot và auto reconnect).

**Phương án thay thế đã cân nhắc:** thiết bị connect tới buddy. Bỏ vì:
- Phải mở port trên Mac (firewall prompt mỗi lần)
- Buddy phải mDNS-advertise cho thiết bị thấy (thêm complexity)
- HTTP layer của thiết bị đã là integration surface; giữ consistent

---

## 4. Component

### 4.1 `autonomous-buddy` (app Swift macOS)

- `NSStatusItem` trên menu bar (không có Dock icon; `setActivationPolicy(.accessory)`)
- Menu: trạng thái pair, trạng thái kết nối, command gần nhất, "Pause", "Revoke pairing", "Quit"
- mDNS browser qua `Network.framework` (`NWBrowser`)
- Luồng pairing (6-digit code; token lưu ở `~/Library/Application Support/AutonomousBuddy/pairing.json`, mode 0600)
- `URLSessionWebSocketTask` persistent tới lamp
- Command executor:
  - **NSWorkspace** để launch app, mở URL
  - **CGEvent** để gõ phím & key combo
  - **AppleScript / OSAScript** cho "close app" và vài script trắng-list
  - **UNUserNotificationCenter** cho notification desktop
- Helper permission (prompt Accessibility, Automation per-app)
- Audit log local (`~/Library/Application Support/AutonomousBuddy/audit.log`) ; thiết bị không có endpoint audit
- OSLog (unified logging) cho debug

### 4.2 `lamp` Go server — package mới `system/buddy/`

| File | Trách nhiệm |
|------|-------------|
| `types.go` | Schema command / response, type pairing |
| `store.go` | Lưu paired buddy (token, fingerprint, name) — vào `config/buddies.json` |
| `pairing.go` | Sinh code, validate, cấp token |
| `registry.go` | Registry in-memory các buddy đang connect (WS handle) |
| `ws.go` | Handler WS upgrade, auth, vòng lặp message cho từng buddy |
| `dispatcher.go` | `Dispatch(buddyID, cmd) → response` — req/reply qua WS với timeout |
| `service.go` | Service tầng cao gộp các phần trên |
| `wire.go` | Wire provider set của Google |

Route HTTP (đăng ký trong `system/server/server.go`, handler ở `system/server/buddy/delivery/http/`). Thiết bị chỉ giữ một pairing:

| Route | Auth | Mục đích |
|-------|------|----------|
| `POST /api/buddy/pair/start` | admin (`adminAuthMiddleware`) | Cấp code 6-digit, hết hạn 60s → `{code, expires_in}` |
| `POST /api/buddy/pair/confirm` | không (code chính là credential) | Xác nhận code → `{token, buddy_id}` |
| `GET  /api/buddy/status` | admin | `{paired, connected}` kèm `buddy_id`, `name`, `os_version`, `fingerprint`, `paired_at` khi đã pair |
| `DELETE /api/buddy` | admin | Hủy pairing hiện tại |
| `DELETE /api/buddy/self` | Bearer token của buddy (kiểm trong handler) | Buddy tự unpair |
| `GET  /api/buddy/ws` | Bearer token của buddy (kiểm trong handler) | WS upgrade cho buddy |
| `POST /api/buddy/command` | chỉ loopback (`localOnlyMiddleware`) | Dispatch đồng bộ 1 command (mặc định 30 s; `timeout_ms` + 5 s nếu có) |
| `POST /api/buddy/exec/:action` | chỉ loopback | Biến thể HW marker (`[HW:/buddy/exec/<action>:{...}]`), 15 s |
| `POST /api/buddy/observe` | chỉ loopback | Screenshot + mô tả từ vision model phụ |
| `POST /api/buddy/suggest` | chỉ loopback | Endpoint gợi ý cho agent trên thiết bị |

Mã ghép đôi vẫn dùng một lần, hết hạn sau 60 giây và bị hủy sau năm lần nhập sai. Endpoint xác nhận cho phép tối đa mười request mỗi phút cho tất cả client cộng lại (kể cả request sai định dạng); vượt giới hạn trả HTTP 429 cùng `Retry-After: 60`. Cấp mã mới cần xác thực và không đặt lại giới hạn request này. Không dùng IP hoặc header chuyển tiếp để bỏ qua giới hạn. Giới hạn chung cho thiết bị cũng tạm chặn xác nhận hợp lệ khi bị spam; chờ 60 giây rồi xin mã mới nếu cần.

### 4.3 `lelamp` (Python) — **không sửa cho MVP**

Hardware-only theo `feedback_lelamp_external.md`. STT → OpenClaw, OpenClaw → TTS đã có sẵn. Luồng buddy chỉ chạm tầng skill của OpenClaw — tầng đó nằm trong skill directory của OpenClaw, không phải Python source của lelamp.

### 4.4 OpenClaw skill: `computer-use`

Hành vi skill hiện tại (mọi runtime): trạng thái hiện tại đáng tin cậy xác nhận chưa pair, mất kết nối hoặc pause sẽ dừng tác vụ desktop trước mọi command hay việc đọc vision reference, kể cả HW marker đơn giản. Nếu chưa biết trạng thái, skill kiểm tra read-only `desktop_info` một lần và đọc đầy đủ kết quả cùng exit code. Kết quả kiểm tra thành công được dùng lại trong workflow. Lỗi kết nối/quyền và timeout kết thúc turn, không retry, tự pair/kết nối lại hoặc chuyển sang Harness/browser trên thiết bị. Phản hồi ngắn nêu đúng blocker, không hứa tự hoàn thành sau đó. User yêu cầu thử lại hoặc có cập nhật kết nối đáng tin cậy mới thì được kiểm tra lại. Đây là chỉ dẫn skill, không phải backend chặn thực thi; không bổ sung cơ chế inject trạng thái mới.

- Nằm ở `skills/computer-use/`
- `SKILL.md` mô tả trigger và tool surface
- Trigger pattern gồm tiếng Việt ("mở ... trên máy tính", "vào trang ... trên máy", "đóng app ...") và tiếng Anh ("open ... on my computer", "go to ... on my mac")
- Skill script (`scripts/buddy.py`) hoặc HW marker POST tới `http://127.0.0.1:5000/api/buddy/command` (hoặc `/api/buddy/exec/<action>`); các route này chỉ nhận loopback, không cần auth header
- Trả về kết quả TTS-friendly ("đã mở Chrome rồi", "không tìm thấy máy tính đã pair", …)

### 4.5 Web UI (`system/web/`)

`BuddyCard` trong phần pairing của trang Monitor (`system/web/src/pages/monitor/BuddyCard.tsx`):
- Hiển thị Mac đang pair duy nhất (tên, OS, buddy ID, trạng thái kết nối)
- Nút "Pair" → gọi `POST /api/buddy/pair/start` → hiển thị code 6-digit; poll `GET /api/buddy/status` mỗi 5 s để phát hiện pair thành công
- Nút "Revoke" → `DELETE /api/buddy`

---

## 5. Schema command v1

### Request (lamp → buddy)

```json
{
  "id": "uuid-v4",
  "action": "open_app",
  "params": { "app": "Google Chrome" },
  "timeout_ms": 5000,
  "issued_at": "2026-05-21T10:00:00Z",
  "issued_by": "computer-use:openclaw"
}
```

### Response (buddy → lamp)

```json
{
  "id": "uuid-v4",
  "ok": true,
  "result": { "pid": 1234 },
  "error": null,
  "duration_ms": 412
}
```

### Action MVP

| Action | Params | Executor |
|--------|--------|----------|
| `open_app` | `{app: string}` | `NSWorkspace.shared.launchApplication` |
| `close_app` | `{app: string, force?: bool}` | AppleScript `tell application "X" to quit` |
| `open_url` | `{url: string, browser?: "default"\|"chrome"\|"safari"}` | `NSWorkspace.shared.open(URL)` |
| `type_text` | `{text: string, delay_ms?: int}` | `CGEventKeyboardSetUnicodeString` |
| `key_combo` | `{keys: ["cmd","n"]}` | `CGEventCreateKeyboardEvent` với modifier |
| `notification` | `{title: string, body?: string}` | `UNUserNotificationCenter` |
| `ping` | `{}` | health-check, trả `{ok:true}` |

Định nghĩa sẵn nhưng để sau:
- `applescript` (chỉ whitelist)
- `screenshot` (phase vision)
- `click_at` (phase vision)
- `read_clipboard` / `write_clipboard`
- `focus_window` / `bring_to_front`

---

## 6. Discovery & pairing

### Discovery (mỗi lần buddy khởi động)

- Buddy duyệt `_autonomous._tcp.local` qua `NWBrowser`
- Mỗi service tìm thấy: resolve hostname → lưu vào danh sách
- MVP: giả định chỉ có 1 thiết bị trên LAN → auto chọn cái đầu
- Fallback: nhập hostname thủ công (`<device_type>-xxxx.local`) trong menu

### Pairing (1 lần)

1. User yêu cầu mã từ web UI thiết bị qua `POST /api/buddy/pair/start` có admin auth, hoặc backend đã kiểm tra quyền gửi `{"cmd":"data","kind":"buddy.pair.start","data":{}}` trên `fa_channel`.
2. Thiết bị trả mã 6 số. MQTT trả `{"type":"data","kind":"buddy.pair.start","status":"success","data":{"code":"123456","expires_in":60}}` trên `fd_channel`, kèm metadata chuẩn của `MQTTDataResponse`. `data` trong yêu cầu là tùy chọn và được bỏ qua.
3. HTTP và MQTT dùng chung Buddy service: mã dùng một lần được giữ trong RAM 60s; cấp mã mới qua một trong hai transport sẽ thay thế mã đang chờ.
4. User đọc mã từ UI đã yêu cầu rồi mở "Pair with device" trong Buddy.
5. User nhập code vào buddy
6. Buddy gọi `POST /api/buddy/pair/confirm {code, name, fingerprint, os_version}` qua LAN.
7. Thiết bị validate code, sinh bearer token long-lived, lưu `{buddy_id, token, fingerprint, name, os_version, paired_at}` vào `config/buddies.json`.
8. Buddy lưu `{buddy_id, device_host, token, paired_at}` vào `~/Library/Application Support/AutonomousBuddy/pairing.json` (file mode 0600, `Pairing/PairingStore.swift`). App desktop Electron không tự lưu token: pairing được giao cho Swift helper nhúng bên trong, dùng chung file này.
9. Buddy mở WS với `Authorization: Bearer <token>`

Phân quyền MQTT dựa trên credentials của broker và ACL của topic hiện có;
backend phải kiểm tra quyền chủ sở hữu thiết bị trước khi gửi `buddy.pair.start`
hoặc `buddy.pair.revoke`. Xác nhận vẫn qua HTTP; trạng thái còn có qua
MQTT `buddy.status`. Mobile có quyền có thể kết nối broker trực tiếp.
Xem [contract MQTT pairing](../../../../../docs/vi/mqtt_vi.md#buddypairstart--cấp-mã-pair-buddy).

Để thu hồi pairing hiện tại, backend đã kiểm tra quyền gửi
`{"cmd":"data","kind":"buddy.pair.revoke","data":{}}` trên `fa_channel` (`data`
là tùy chọn và được bỏ qua). Lệnh gọi `buddy.Service.Unpair`, cũng được dùng bởi
`DELETE /api/buddy`: đóng WebSocket đang hoạt động, xóa pairing hiện tại và dữ liệu
pairing đã lưu, đồng thời vô hiệu hóa token đã pair. Gọi lại khi chưa có pairing
cũng thành công; mã pairing đang chờ không bị hủy. Phản hồi trên `fd_channel` là
`{"type":"data","kind":"buddy.pair.revoke","status":"success","data":{"revoked":true}}`
kèm metadata chuẩn của `MQTTDataResponse`. Khi lỗi, gồm service không khả dụng
hoặc lỗi lưu dữ liệu, phản hồi dùng `status:"failure"` và `error`.
Xem [contract MQTT thu hồi pairing](../../../../../docs/vi/mqtt_vi.md#buddypairrevoke--thu-hồi-pairing-buddy).

### Trạng thái mobile qua MQTT

Gửi `{"cmd":"data","kind":"buddy.status","data":{}}` lên `fa_channel`.
Phản hồi và event thay đổi tự phát dùng `type:"data"`, `kind:"buddy.status"`,
`status:"success"`, với `data` gồm `paired`, `connected`, `instance_id`,
`revision`, và các field tùy chọn `buddy_id`, `name`, `os_version`, `paired_at`
(RFC3339). Chưa pair thì bỏ các field của Mac; chuỗi tùy chọn rỗng cũng bị bỏ.
Không có secret, mã pairing hoặc fingerprint. `paired` khác `connected`:
Mac pause/offline vẫn có thể còn paired.

Thông báo gồm trạng thái khởi động, HTTP confirm thành công, revoke qua HTTP/MQTT
(kể cả Buddy tự revoke), và thay đổi kết nối WebSocket hiện tại. Revision bắt đầu
0, tăng sau mỗi thao tác pair/revoke/connect/current-disconnect thành công.
Chỉ so revision trong cùng `instance_id`, đổi khi service khởi động lại; bỏ bản
trùng/cũ. Gửi bất đồng bộ qua queue có giới hạn, gộp thành trạng thái mới nhất,
QoS 1 không retain. Publish lỗi chỉ log/bỏ nên phải subscribe FD và chờ SUBACK
trước khi query lúc mở màn hình, reconnect, resume. Query lỗi dùng
`status:"failure"` và `error`. Status có thể đến trước phản hồi lệnh.

Mobile có thể kết nối trực tiếp bằng cấu hình broker theo device và client ID
riêng duy nhất cho app, không cần sửa BFF. Vẫn cần kiểm chứng broker reachability
và ACL topic trên triển khai thật. Luồng cập nhật foreground, không phải push
notification khi đóng app. Xem
[contract MQTT status đầy đủ](../../../../../docs/vi/mqtt_vi.md#buddystatus--đọc-và-theo-dõi-trạng-thái-buddy)
và [prompt bàn giao mobile](../../../../../docs/vi/buddy-mobile-handoff_vi.md).


### Reconnect

- Buddy giữ WS mở + ping định kỳ (15s/lần)
- Khi disconnect: backoff exponential (1s, 2s, 4s, … cap 30s)
- Khi thiết bị reboot: thiết bị thấy token cũ trong `buddies.json` và accept WS ngay (token persistent)
- Khi revoke: thiết bị xóa khỏi `buddies.json`, từ chối với HTTP 401 → buddy phát hiện, drop session, prompt re-pair

---

## 7. Security model

| Layer | Cơ chế |
|-------|--------|
| Pairing | Yêu cầu user thao tác thực sự trên Mac (gõ code). Không thể pair lén. |
| Lưu token | `~/Library/Application Support/AutonomousBuddy/pairing.json` (file mode 0600, không mã hóa; bỏ Keychain vì build ad-hoc-signed gặp prompt mật khẩu không duyệt được) bên buddy; `config/buddies.json` bên lamp (file mode 0600) |
| Transport | WS over HTTP trên LAN (MVP). **TLS để sau** — risk được document, mitigate bằng LAN-only + pairing. v1.1: self-signed cert + pinning. |
| Indicator session | Chấm đỏ trên menu bar khi buddy đang connect + active. Luôn nhìn thấy. |
| Audit log | Mọi command đều log (timestamp, action, hash params, source). Chỉ file local (`~/Library/Application Support/AutonomousBuddy/audit.log`). |
| Kill switch | "Pause" trên menu = drop WS giữ token. "Revoke pairing" = drop token + bảo lamp xóa. |
| Permission gating | Command fail clean với error rõ ràng nếu permission macOS bị deny (không silent fail). |
| Blast radius | **Document rõ với user**: Lamp có quyền tương đương account user trên Mac. Trust ask là vấn đề UX trung tâm. |
| Rate limit | Lamp side: max N command/sec/buddy. Tránh runaway loop. |

### Threat đã cân nhắc

1. **Attacker trên LAN** → không pair được nếu không có mã từ luồng HTTP hoặc MQTT đã kiểm tra quyền. Không replay token được nếu không đọc được `pairing.json` (0600) của user.
2. **Lamp bị compromise** → chạy được command tùy ý trên Mac (= blast radius). Mitigation: user revoke bất cứ lúc nào từ menu bar, không cần truy cập lamp.
3. **Buddy bị compromise** (malware Mac hijack WS) → có thể gửi response giả về lamp. Mitigation: command ID + signed response (v1.1+).
4. **Eavesdrop trên LAN** → MVP không mã hóa WS. Chấp nhận với LAN nhà, phải fix trước khi deploy network không tin cậy.

---

## 8. macOS permission

| Permission | Vì sao | Khi nào trigger | Nếu deny |
|------------|--------|-----------------|----------|
| Accessibility | `CGEvent` inject input (type_text, key_combo) | Command `type_text` / `key_combo` đầu tiên | Command trả error; menu bar hiện "⚠ Cần Accessibility" |
| Automation (per-app) | AppleScript điều khiển từng app (close_app) | Lần đầu close_app cho từng app target | Command trả error |
| Notifications | Hiện trạng thái buddy & action noti | Lần đầu app launch (request ngay) | Action `notification` fail; indicator status vẫn chạy |

**Không cần Screen Recording** ở MVP (không screenshot).

---

## 9. Roadmap tương lai

| Version | Feature | Ghi chú |
|---------|---------|---------|
| v1.1 | Vision fallback qua Claude Computer Use | Cho command không match intent |
| v1.1 | TLS cho WS | Self-signed + cert pinning |
| v1.2 | Port Windows | Khả năng cao là Tauri/Rust |
| v1.3 | Port Linux (X11 trước) | Wayland để sau |
| v1.4 | Nhiều buddy trên 1 lamp | Kịch bản gia đình |
| v1.5 | Screen Recording + screenshot on-demand | Lamp request frame |
| v2.0 | Code signing + notarization + Sparkle | Distribution thực sự |
| v2.0 | Helper riêng theo app | Tích hợp first-party: Chrome extension, Slack bot, … |

---

## 10. Risk & câu hỏi mở

1. **Trust** — vấn đề UX trung tâm. Cần indicator session rõ ràng, truy cập audit log, kill switch dễ.
2. **macOS API churn** — Accessibility / AppleScript API hay thay đổi giữa các major version. Cần pin OS tối thiểu và test với version mới khi release.
3. **Latency** — voice → STT → OpenClaw → intent → WS → buddy → action ≈ 2–4s end-to-end. Sẽ thấy chậm so với Siri. Mitigation: TTS feedback ("ok, đang mở Chrome…") che lag.
4. **Distribution không notarize** — lần chạy đầu phải right-click → Open. Chấp nhận cho MVP. Plan ký ở v2.0.
5. **Command nhập nhằng** — "mở app" thiếu target cần TTS clarify lại. Skill phải xử lý.
6. **Mac sleep / khóa máy** — buddy không drive được nếu màn khóa. Định behavior: queue + retry hay error? MVP: error với message rõ ràng.
7. **Nhiều Mac trên cùng LAN** — nhiều buddy, 1 lamp. MVP cho phép 1 buddy đã pair. Document rõ.
8. **Lamp khác mạng** — buddy không connect được. Để khi nào có VPN/relay phase.

---

## 11. Quyết định tech stack

| Quyết định | Chọn | Lý do |
|------------|------|-------|
| Ngôn ngữ buddy | **Swift (native)** | API Mac tốt nhất (CGEvent, AppleScript, NSWorkspace đều native); binary nhỏ nhất; không cần lớp plugin |
| Project format | **Swift Package Manager** lúc đầu | Không cần Xcode; iterate nhanh. Chuyển Xcode `.xcodeproj` khi cần `.app` bundle + signing |
| Min macOS | **macOS 13 (Ventura)** | API `NWBrowser` modern, `Network.framework` chín; cũ hơn phải backport |
| Network protocol | **WebSocket (raw, không TLS MVP)** | Persistent, hai chiều, support tốt qua URLSessionWebSocketTask. TLS ở v1.1 |
| Discovery | **mDNS qua `Network.framework` `NWBrowser`** | Native, không cần dep thứ 3 |
| Codec command | **JSON** | Đồng style với serializer của lamp; dễ debug |
| Lưu token | **File, mode 0600** (`pairing.json`) | Keychain prompt không duyệt được với build ad-hoc-signed; xem lại khi có Developer ID signed |
| Lưu pref | **UserDefaults / plist** | Chuẩn macOS cho non-secret |
| Logging | **OSLog (unified)** | Native, xem được trong Console.app |
| Test framework | **XCTest** | Chuẩn |
| Go side | **Pattern hiện có** (Gin, Wire, internal/) | Theo `runtimes/openclaw/`, `server/<domain>/delivery/http/` |
| Vị trí folder autonomous-buddy | **Root repo** cạnh `lamp/`, `lelamp/` | Self-contained, build độc lập |

---

## 12. Discussion log (2026-05-21)

### Brief ban đầu của Leo

> Mình muốn làm 1 chức năng remote computer use. Tưởng tượng Lamp sẽ remote vào MacBook của mình, điều khiển máy tính, app, browser Chrome … giống cách supporter qua TeamViewer điều khiển máy của mình.
>
> Máy MacBook cần chạy 1 software lamp-buddy trên top status bar, sau đó quét xem có Lamp đang chạy LAN IP gì đó, thì accept cho Lamp được connect và computer use máy MacBook.
>
> VD mình nói "mở Chrome trên máy tính và vào Gmail lấy mail ra", hay "tự động join Google Meet" … cần code thêm 1 folder riêng `lamp-buddy` (chưa rõ dùng ngôn ngữ gì swift hay flutter …). Sau đó install vào Mac/Windows/Linux. Software này paring với Lamp và cho phép Lamp điều khiển.

### Các phương án đã cân nhắc

**1. Dùng protocol có sẵn (VNC, RustDesk, TeamViewer)**
- Pro: không phải code
- Con: các app này hướng đến controller là người, không phải AI/tool-API. Lamp vẫn phải "nhìn màn hình + click/type" qua chúng → tương đương build TeamViewer client trong Lamp.
- **Bỏ.**

**2. Screen Sharing / VNC built-in của macOS**
- Pro: không cần cài thêm gì trên Mac
- Con: giống trên — thiết kế cho controller là người. Gắn vision-action loop của Lamp lên trên rất gượng.
- **Bỏ cho MVP.**

**3. Mac Companion riêng (chọn)**
- Pro: tiếp cận API native, latency nhỏ nhất, command intent-level mà không cần vision-loop, có thể tích hợp sâu macOS (Spotlight, Shortcuts, …)
- Con: phải build & maintain. Code per-platform (về sau).
- **Chọn.**

### Quyết định: intent-based vs vision-loop

**Intent A (chọn):** parse voice intent → map sang command structured (`open_app`, `type_text`) → buddy chạy local, không screenshot.
- Latency: <500ms/command
- Cost: 0 LLM call thêm (chỉ dùng intent parsing có sẵn của OpenClaw)
- Robustness: giòn với request lạ
- Coverage: 80% use case đã nêu (mở Chrome, vào Gmail, join Meet, type, close app)

**Vision B (để v1.1):** screenshot → Claude Computer Use API → click(x,y) → lặp.
- Latency: 3–10s/turn
- Cost: mỗi screenshot là message to (đắt)
- Robustness: bền — Claude tự nhìn và thích nghi
- Coverage: bất cứ thứ gì hiện trên màn hình

**Hybrid C (để v1.1):** A mặc định, fallback B cho command không nhận diện được.

### Quyết định: LLM chạy ở đâu?

**Option a (chọn):** OpenClaw (trên lamp) parse intent. Buddy là dumb executor nhận command structured.

**Option b (để sau):** Buddy host LLM riêng cho task phức tạp ("đọc email mới nhất tóm tắt"). Để đến khi Vision B cần.

### Quyết định: ngôn ngữ

MVP computer-use ban đầu chọn **Swift native**. Kiến trúc tháng 9 giữ Swift cho thực thi native và nhúng vào [agent manager](./agent-manager_vi.md) Electron/React, tạo một app cài đặt duy nhất. Quyết định cũ loại Electron khỏi toàn sản phẩm không còn áp dụng. Helper native Windows/Linux vẫn là bước sau.

### Quyết định: hướng kết nối

**Buddy → lamp** (chọn) — xem §3 cho lý do. Buddy là WS client, lamp là WS server.

### Quyết định: distribution (MVP)

**Không sign** — user right-click → Open ở lần chạy đầu. Apple Developer account ($99/năm) để v2.0.

### Quyết định với `lelamp` và `lamp` Go

- `lelamp` (Python) — **không sửa**. Chỉ hardware.
- `lamp` (Go) — **package mới** `system/buddy/`, route HTTP mới, WS gateway mới.
- `OpenClaw` — skill mới `computer-use`.
- `system/web` — `BuddyCard` trong trang Monitor (pair / status / revoke).
- `CLAUDE.md` — thêm row docs.

---

## 13. Tham chiếu

- mDNS hostname — thiết bị publish `<device_type>-<last4hex>.local` (ví dụ `lamp-a1b2.local`)
- `feedback_lelamp_external.md` — hardware code ở Python, không phải Go
- `project_security_login_ui_batch.md` — security audit mới đóng; pattern cookie HMAC + bcrypt admin có thể reuse cho auth buddy
- [Tài liệu Anthropic Computer Use](https://docs.anthropic.com/en/docs/build-with-claude/computer-use) — cho phase vision v1.1
- [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) — cho khả năng screenshot v1.5
- [Apple Accessibility API](https://developer.apple.com/documentation/applicationsystem/axuielement_h) — để target window/button
