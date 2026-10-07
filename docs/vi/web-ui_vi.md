# Web UI — Monitor Dashboard

Flow Monitor hiển thị lượt đồng bộ lịch sử thành **History sync · Harness → Main** (hoặc nguồn bên ngoài), với input và câu trả lời dễ đọc, nhãn **Context**. Chỉ dẫn nội bộ và JSON nằm trong chi tiết event; tooltip route cho biết agent bên ngoài.

Lượt voice Harness-only hiển thị route **Harness**. Event `harness_response` đã lưu cung cấp output, chi tiết Response và thời điểm hoàn tất cho đúng run, nên tải lại monitor không còn để lượt đã trả lời ở trạng thái ACTIVE. Gửi input mà chưa có phản hồi cuối vẫn giữ ACTIVE.

Flow Monitor liên kết follow-up Codex được steer với lượt thực thi gốc qua
`turn_merged` / `parent_run_id`. Chọn follow-up mở pipeline chung nhưng vẫn giữ
card input và trạng thái kết thúc riêng. UI ghi rõ turn đã gộp thay vì hiện
kết quả im lặng rỗng.

## Ngày cập nhật: 2026-08-25

---

## 1. Tổng Quan

Web UI của thiết bị là một React SPA (Single Page Application) được build bằng **React 19 + TypeScript + Vite + Tailwind CSS 4**, phục vụ hai mục đích:

1. **Setup flow** — Onboarding WiFi, LLM provider, messaging channel (các trang `/setup/*`)
2. **Monitor Dashboard** — Theo dõi trạng thái thiết bị real-time (`/monitor`)

File build output (`dist/`) được nginx serve tại root `/` trên thiết bị.

Trong lần setup đầu, **Channels** là tuỳ chọn và mặc định là **Not now**.
Chọn Telegram, Slack hoặc Discord sẽ hiện các field credential, nhưng user có
thể để trống hoặc cấu hình channel sau trong Settings.

### Chọn tiếng Nhật

Setup và Settings có **Japanese (日本語)** với mã `ja`. Setup nhận diện locale
Nhật của trình duyệt và `?stt_language=ja`; biến thể vùng tiếng Nhật được chuẩn hóa về
`ja`. Các chuỗi chat i18n, thời gian tương đối, hướng dẫn đăng ký giọng và cả ba
câu đăng ký đều có bản tiếng Nhật. Test Voice gửi câu mẫu tiếng Nhật. Nhãn tĩnh
trong Settings tiếp tục theo quy ước giao diện tiếng Anh hiện có.

Bộ lọc ngôn ngữ ElevenLabs có nhóm tiếng Nhật gồm Shizuka, Konoha, Rin, Asahi,
Hinata và Hiroki; Shizuka là mặc định. OpenAI và Gemini tiếp tục dùng bộ voice
đa ngôn ngữ của từng nhà cung cấp. Setup kiểm tra voice ban đầu với danh mục
theo ngôn ngữ, nên trình duyệt Nhật chọn Shizuka thay vì giữ Rachel. Voice hợp lệ
đã lưu hoặc lấy từ URL được giữ; phản hồi catalog cũ bị bỏ qua sau khi đổi
provider, ngôn ngữ hoặc voice.

### 1.1 Tiêu đề tab trình duyệt

Tiêu đề tab trình duyệt (`document.title`) hiển thị đúng theo page/tab đang focus, để mở nhiều tab thiết bị vẫn phân biệt được. Dùng hook chung `useDocumentTitle` (`system/web/src/hooks/useDocumentTitle.ts`); format: `Lamp · <segment>[· <sub-segment>]`.

| Route / trạng thái | Title |
|--------------------|-------|
| `/setup` (và `/` khi chưa provision) | `Lamp · Setup` |
| `/monitor#<section>` (theo section đang chọn) | `Lamp · <tên section>` — ví dụ `Lamp · Chat`, `Lamp · Overview`, `Lamp · Pairing`, `Lamp · Info`, `Lamp · Flow`, `Lamp · Users`, `Lamp · Camera`, `Lamp · Sensing`, `Lamp · Analytics`, `Lamp · Servo`, `Lamp · Logs`, `Lamp · CLI` |
| `/setting#<section>` (Settings, theo section đang chọn) | `Lamp · Settings · <tên section>` — ví dụ `Lamp · Settings · General`, `Lamp · Settings · Wi-Fi`, `Lamp · Settings · AI Brain`, `Lamp · Settings · Language`, `Lamp · Settings · Voice`, `Lamp · Settings · My Voice`, `Lamp · Settings · Face`, `Lamp · Settings · Channels`, `Lamp · Settings · MQTT`, `Lamp · Settings · Timezone` |
| `/gw-config` | `Lamp · GW Config` |

`<title>Lamp Setup</title>` tĩnh trong `index.html` chỉ là fallback trước khi React mount; hook sẽ ghi đè khi mount và khôi phục title cũ khi unmount.

### 1.2 Link đăng nhập kèm mật khẩu

Trang đăng nhập nhận mật khẩu từ URL query để truy cập trực tiếp có kiểm soát:
`/login?password=<mật-khẩu-đã-URL-encode>`. Tham số này cũng dùng được với mọi
route cần đăng nhập và alias cũ của chúng (`/`, `/monitor`, `/setting`, `/edit`,
`/gw-config`, và `/dashboard`), ví dụ
`/setting?password=<mật-khẩu-đã-URL-encode>#voice`.
Khi có query này, trang tự điền trường Admin Password và gửi form đăng nhập
ngay. Auth gate chuyển tham số tới Login, rồi sau khi thành công quay lại path
và hash đích đã được làm sạch. Parameter `password` được xoá bằng `safeSearch`,
nên secret không còn trong URL hiển thị hoặc history entry của trình duyệt.
Query phải đứng trước `#`; phần sau `#` là fragment phía client, không phải
query string.

Chỉ dùng với link tin cậy và tồn tại ngắn: mật khẩu trong URL vẫn có thể lộ qua
link được sao chép, lịch sử trình duyệt, hoặc log server/proxy trước khi trang
xóa nó.

---

## 2. Cấu Trúc Thư Mục

```
system/web/
├── src/
│   ├── pages/
│   │   ├── monitor/           # Dashboard monitor (index.tsx = khung + polling; mỗi section một file)
│   │   └── ...                # Các trang setup
│   ├── components/
│   │   └── ui/                # shadcn/ui components
│   ├── lib/
│   │   └── i18n.ts            # Bản địa hóa chuỗi UI (en/vi/zh-CN/zh-TW, fallback tiếng Anh)
│   ├── index.css              # Global styles + theme variables
│   └── main.tsx
├── vite.config.ts
└── package.json
```

---

## 3. Monitor Dashboard (`/monitor`)

### 3.1 Thiết Kế Tổng Thể

Monitor dùng dark theme riêng với class `.lm-root` (định nghĩa trong `index.css`), **không dùng Tailwind** — toàn bộ styling dùng inline styles với CSS variables `--lm-*`.

Layout: **Sidebar 216px cố định + Main area co giãn**, chiều cao 100vh.

### 3.2 Sidebar Navigation

Nhóm **Device** trên sidebar chứa các mục Monitor chính, trong đó **Pairing nằm
ngay dưới Servo**. Bluetooth vẫn được triển khai nhưng hiện ẩn khỏi navigation:

| Icon | Section | Nội dung |
|------|---------|---------|
| ◈ | Overview | Tổng quan toàn bộ hệ thống |
| ⟷ | Pairing | Kết nối/ghép đôi Autonomous Buddy và Harness |
| ⬡ | System | CPU/RAM/Temp chi tiết + lịch sử |
| ◎ | Workflow | OpenClaw event feed real-time |
| ⬟ | Camera | MJPEG stream + Display LCD |

Góc dưới sidebar hiển thị trạng thái OpenClaw (online/offline) và thời điểm cập nhật gần nhất.

**Tìm kiếm chức năng.** Một ô tìm kiếm (`SidebarSearch`, `system/web/src/pages/monitor/index.tsx`) nằm ở đầu sidebar để gọn gàng hoá danh sách nav vốn rất dài. Nó lọc các mục nav theo nhãn **hoặc** tên nhóm cha (không phân biệt hoa thường, khớp chuỗi con) và tuân theo đúng các điều kiện hiển thị như nav gốc — các mục chỉ-debug (`PUBLIC_SECTIONS`) và các tab thiếu phần cứng (`sectionVisible`) sẽ không xuất hiện trong kết quả. Khi đang có từ khoá, cây nhóm bị ẩn và được thay bằng danh sách kết quả phẳng; mỗi dòng tái sử dụng `.lm-snav-item` nên giữ nguyên hiệu ứng active/hover màu hổ phách, kèm một chip nhỏ ghi tên nhóm cha (ví dụ `General` · `Settings`). Biểu tượng kính lúp ở đầu chuyển sang màu hổ phách khi focus; nút xoá (×) ở cuối xuất hiện ngay khi có từ khoá (cũng xoá được bằng `Esc`). `Enter` nhảy tới kết quả đầu tiên.

### 3.3 Dark Theme Variables

Định nghĩa tại `.lm-root` trong `index.css`:

```css
--lm-bg:          #0C0B09   /* Background chính */
--lm-sidebar:     #111009   /* Sidebar */
--lm-card:        #17160F   /* Card background */
--lm-surface:     #1E1D14   /* Surface bên trong card */
--lm-border:      #2A2820   /* Border */
--lm-border-hi:   #3A3828   /* Border highlight */
--lm-amber:       #F59E0B   /* Màu chủ đạo (warm lamp) */
--lm-amber-dim:   rgba(245,158,11,0.12)
--lm-amber-glow:  rgba(245,158,11,0.35)
--lm-teal:        #2DD4BF
--lm-green:       #34D399
--lm-red:         #F87171
--lm-blue:        #60A5FA
--lm-purple:      #A78BFA
--lm-text:        #F0EEE8
--lm-text-dim:    #9A9080
--lm-text-muted:  #504A3C
```

### 3.4 Settings (`/setting`) — shell dùng chung

**Remote MCP Tools** (`/setting#mcp`). Hoàn tất onboarding runtime đã chọn,
sau đó đăng nhập giao diện admin của thiết bị. Nhập **Name** duy nhất, **URL**
của MCP server từ xa và **Headers** cần thiết, rồi bấm **Add Tool**. Ví dụ,
người vận hành có tài khoản [Pushary](https://pushary.com) và điện thoại đã kết
nối có thể nhập:

| Trường | Giá trị |
|---|---|
| Name | `pushary` |
| URL | `https://pushary.com/api/mcp/mcp` |
| Header name | `Authorization` |
| Header value | `Bearer YOUR_OPERATOR_API_KEY` — thay placeholder bằng key Pushary của bạn |

Pushary là dịch vụ hosted riêng, có tài khoản, trial/subscription và chính sách
delivery riêng. Đăng nhập admin của thiết bị khác với key của server này.
OS lưu entry trong `mcp_tools` và đồng bộ vào runtime đang dùng với tên
`tool_pushary`. OpenCode nhận `mcp.tool_pushary` với `type: remote`; Hermes nhận
`mcp_servers.tool_pushary`. Cả hai giữ URL, headers và bật entry.

Add có thể báo lỗi sau khi OS đã lưu entry, nếu config runtime thiếu hoặc không
hợp lệ. Tải lại danh sách trước khi thử lại: tên trùng sẽ bị từ chối. Sửa setup
runtime rồi kiểm tra log của lần startup sync tiếp theo. Entry trong danh sách
chưa chứng minh xác thực server, khám phá tool hay gateway restart thành công.
Để kiểm tra delivery, yêu cầu runtime khám phá các tool của Pushary và gửi một
notification vô hại; xác nhận điện thoại dự kiến đã nhận được.

Thêm MCP server không cài native approval hook. Cụ thể, presync của Hermes do
OS quản lý đặt `approvals.mode: "off"`; model gọi `ask_user` là câu hỏi hợp tác,
không phải cơ chế bắt buộc duyệt trước mọi tool. Dùng **Remove** để xóa server
đã cấu hình, rồi kiểm tra log runtime nếu server vẫn còn khả dụng.

**Speech speed** trong Voice (`/setting#tts`) tải `tts_speed` hiệu lực,
hiển thị khoảng có thể chọn (`0.5–2.0×` cho mọi provider), bước `0.05`. **Save Changes** lưu tốc độ qua
`PUT /api/device/config`. **Test Voice** gửi ngay tốc độ trên slider mà không cần
lưu; tốc độ thử chỉ áp dụng cho câu preview. Giá trị đã lưu
ưu tiên hơn `HAL_TTS_SPEED` (mặc định `1.2`); ElevenLabs HTTP v3 áp dụng tốc độ ở HAL và gửi provider speed `1.0`;
các model ElevenLabs khác giới hạn tốc độ gửi đi trong `0.7–1.2`.

**Mirror key/URL từ AI Brain.** Panel tự điền ô TTS hoặc STT còn trống bằng key
và base URL của AI Brain, để lần setup đầu chỉ phải nhập một bộ. Riêng phần key
còn phải thoả điều kiện máy **chưa có key nào** (`has_tts_api_key` /
`has_stt_api_key`): mấy ô đó là write-only nên lần mở trang nào cũng trống bất
kể máy đã lưu key hay chưa, tức "trống" không thể hiểu là "chưa đặt" như với
base URL — thứ luôn load lên kèm giá trị thật. Thiếu điều kiện đó thì gõ một key
AI Brain mới là ghi đè im lặng lên key TTS/STT đang cố tình khác: đã có máy giữ
key openrouter đi kèm URL proxy autonomous, một cặp không thể chạy.

**Quyền sở hữu key TTS.** Thiết bị chỉ lưu đúng một key TTS (`ttsAPIKey`), và nó
luôn thuộc về provider đang được chọn trong Voice. `Autonomous (proxy)` và
`Custom (BYO URL)` không lưu gì cả mà kế thừa key AI Brain qua
`Config.GetTTSAPIKey()` (`system/server/config/config.go:605`) — ô chọn vendor
của nó có OpenAI, ElevenLabs và Gemini; `Piper` không cần
key; `OpenAI (direct)` và `ElevenLabs (direct)` **bắt buộc phải có key riêng** —
JWT Autonomous kế thừa sẽ bị từ chối bằng 401, HAL retry rồi bỏ cuộc và trả về 0
sample, tức là thiết bị câm mà UI không báo lỗi gì. Bốn quy tắc giữ bất biến này:

- Save bị **từ chối** khi provider direct không có key vừa nhập lẫn key đã lưu
  thuộc đúng provider đó. Nhãn API Key hiển thị "required" với provider direct và
  "optional — leave blank to reuse AI brain key" với các lựa chọn còn lại.
- Badge `✓ configured` và placeholder `•••••••• saved` chỉ hiện khi key đã lưu
  thuộc về provider đang chọn. Quyền sở hữu được suy ra từ cặp `tts_base_url` +
  `tts_provider` lúc load qua `detectChoice()`
  (`system/web/src/pages/settings/ttsProvider.ts`) — không thêm field config nào.
- Mirror key AI Brain chỉ điền vào ô key TTS đang trống với `autonomous` /
  `custom` (`ttsInheritsLlmKey()` trong `SettingsPanel`). Mirror vào provider
  direct sẽ đặt một JWT Autonomous vào ô đó — trông như đã cấu hình và lưu trót
  lọt, nhưng vendor trả về 401. Điều này khớp với điều kiện
  `sttProvider === "autonomous"` mà các mirror phía STT vốn đã có.
- Đổi provider khác với chủ sở hữu của key sẽ gửi `clear_tts_api_key: true`, và
  `applyVoicePipelineFields` xử lý bằng cách xoá rỗng `ttsAPIKey`. Gửi
  `tts_api_key: ""` không làm được việc này: mọi field trong
  `UpdateConfigRequest` theo ngữ nghĩa PATCH, `""` nghĩa là "không gửi". Key sau
  khi xoá được đẩy live xuống HAL dưới dạng giá trị *đã resolve*
  (`GetTTSAPIKey()`), vì `/voice/tts/config` của HAL hiểu key rỗng là "giữ
  nguyên key hiện tại".

Key đã gõ nhưng chưa lưu được cache theo từng provider **chỉ trong tab trình
duyệt** (một `useRef` trong `TTSSection`), để đổi qua lại giữa các provider trước
khi lưu không phải gõ lại. Cache này mất khi reload và khi Save; thiết bị không
bao giờ giữ quá một key.

> Luồng Setup (`system/web/src/components/setup/TTSSection.tsx`) vẫn chưa expose
> key và base URL của TTS — cả hai mirror từ AI Brain — nên provider direct chọn
> trong lúc setup không thể nhập key hợp lệ ở đó. Hãy cấu hình sau trong
> Settings → Voice.

Settings **không phải là một trang riêng**. Nó là một khu vực (area) của chính shell Monitor (`system/web/src/pages/monitor/index.tsx`), truy cập tại route `/setting`. Trong `App.tsx`, `/monitor` và `/setting` là các route con của một layout route duy nhất có element render `<Monitor/>`; React Router giữ element đó luôn mounted khi chỉ đường dẫn con thay đổi, nên sidebar **không** bị remount khi chuyển giữa Monitor và Settings (không có hiện tượng nháy toàn trang). Shell suy ra khu vực — `"monitor"` hoặc `"setting"` — từ `useLocation().pathname`.

Nhóm Settings có thể thu gọn nằm trong `NAV` của sidebar dùng chung (`system/web/src/pages/monitor/types.ts`). Bấm một mục Settings sẽ điều hướng tới `/setting` và render `SettingsPanel` (`system/web/src/pages/settings/SettingsPanel.tsx`) ở khu vực chính; bấm một mục Monitor sẽ điều hướng tới `/monitor`.

**Sơ đồ URL hash** — section trong bộ nhớ vẫn giữ id nội bộ `settings:*`, nhưng URL hash dùng nhãn ngắn trong khu vực setting (các helper `sectionToHash`/`hashToSection` trong `types.ts`):

| Mục | URL |
|-----|-----|
| General | `/setting#general` (nội bộ `settings:device`) |
| Wi-Fi | `/setting#wifi` |
| My Voice | `/setting#voice` |
| Face | `/setting#face` |
| AI Brain | `/setting#llm` |
| Runtime | `/setting#runtime` |
| Language | `/setting#stt` |
| Voice | `/setting#tts` |
| Realtime | `/setting#realtime` |
| Channels | `/setting#channel` |
| MQTT | `/setting#mqtt` |
| MCP Tools | `/setting#mcp` |
| Plugins | `/setting#plugins` |
| Timezone | `/setting#timezone` |
| Resting light | `/setting#led` |

Các mục Monitor được serialize thành id thuần, ví dụ `/monitor#overview`, `/monitor#pairing`, `/monitor#system`, `/monitor#flow`. Mặc định: `/monitor` không có hash / hash không hợp lệ → `overview`; `/setting` không có hash / hash không hợp lệ → `general` (URL được chuẩn hóa thành `/setting#general`). Deep-link (ví dụ `/setting#wifi`) và nút back/forward của trình duyệt được tôn trọng qua một effect dựa trên `useLocation`. Người dùng không-debug chỉ thấy các mục trong `PUBLIC_SECTIONS` (gồm Chat, Overview, **Pairing**, Info, Flow, Camera, **Sensing**, Users, **Logs**, **CLI**, và các mục Settings công khai General/Wi-Fi/My Voice/Face/MCP Tools/Plugins/Timezone/Resting light); Bluetooth vẫn truy cập được bằng URL trực tiếp nhưng bị ẩn khỏi navigation. `?debug=true` mở khóa phần còn lại (Analytics, Servo, API Docs, Agent gateway, và các mục Settings sâu hơn AI Brain/Runtime/Language/Voice/Realtime/Channels/MQTT). Bấm `update` là nút đổi ngay thành `updating…` — nút KHÔNG bao giờ báo "OK", vì chữ đó đọc như "xong rồi" trong khi request mới chỉ KHỞI ĐỘNG việc cài (và với component chạy vài giây thì nó còn hiện trước cả lúc dòng kịp báo tiến trình). Khi lỗi thì hiện đúng lý do server trả về (`rate-limited, retry in 8s`, `bootstrap unreachable`) thay vì chữ "Failed" trống rỗng. Trong lúc đang cài, dòng đó hiện `updating…` thay cho nút (một lần cài mất vài chục giây — component dừng, build lại, khởi động lại — và một dòng đứng im khiến người dùng bấm lần hai, chính là cách một máy từng mất sạch HAL runtime). Các nút `update` trong card **Versions** ở Overview (dòng Web / OS / HAL / Agent, cộng Bootstrap và Device ở debug) cũng bị chặn theo cách này — người xem thường không có nút kích OTA một chạm. Toggle **Debug** trên top bar, ngay cạnh nút Dark/Light, bật/tắt query parameter này nhưng vẫn giữ hash của mục đang mở và các query parameter khác; màu amber cho biết debug mode đang bật.

Card **Versions** ở Overview có cột thao tác với nút `restart` cho OS Server và HAL, kể cả ngoài debug. Mỗi nút gọi `POST /api/system/restart/:target` có bảo vệ admin (`os-server` hoặc `hal`). Server hẹn restart sau 2 giây và trả HTTP 202. Nút hiện `queued`, khóa bấm lại trong 15 giây; trạng thái này chỉ xác nhận đã lên lịch, chưa xác nhận service phục hồi. Polling sẵn có của monitor cập nhật trạng thái/uptime sau khi kết nối lại. Lỗi được giữ hiển thị cạnh nút. Restart bị vô hiệu hóa khi biết dòng đó đang cập nhật; hoạt động OTA được poll cả ở chế độ thường. Card hẹp cuộn ngang để truy cập đủ sáu cột.

Card Versions hiển thị **Current** và **Latest** cạnh nhau. Latest lấy từ `target` của từng component trong `/api/system/ota-versions`, gồm runtime đang dùng qua alias `agent`; đây là bản được publish trong OTA feed của thiết bị, không phải tra release upstream. Metadata được tải ở cả chế độ thường và debug, rồi làm mới sau cập nhật. Target thiếu hoặc rỗng, gồm Host, hiện `—`; component đã ở bản hiện tại vẫn hiển thị target đã publish. Hai hàng Bootstrap, Device và nút update vẫn chỉ hiện trong debug.

Sidebar Monitor/Settings chỉ mở một nhóm cấp cao nhất: mở nhóm mới sẽ đóng nhóm trước, bấm nhóm đang mở sẽ thu lại, và điều hướng tự mở nhóm chứa trang đích. Sidebar có danh sách điều hướng cuộn độc lập, giữ thao tác cuộn trong danh sách và footer Logout không bị co. Trên điện thoại, menu dùng chiều cao viewport động (`100dvh`) để truy cập được mọi mục debug.

**Form Realtime** (`/setting?debug=true#realtime`) dùng nhãn checkbox ngắn; tên provider nằm trong dropdown Provider. Hướng dẫn cho API Key và Base URL tùy chọn nằm dưới ô nhập để nhãn trường gọn trên màn hình hẹp.

Cách bố trí nhãn ngắn cũng áp dụng cho attention trigger trong General, thông tin xác thực STT, trường provider/key của TTS và Resting light. Trạng thái key TTS giữ nguyên cụm khi hàng xuống dòng. Trang cấp Wi-Fi đặt giải thích Advanced dưới nút; Manual Move tách mô tả khỏi checkbox Live drag.

**Speech attention gate** nằm trong card **General** công khai, không nằm ở mục Realtime chỉ-debug. Checkbox vẫn ghi cờ `wakeword` top-level; lưu giá trị wake đã đổi hoặc đang chờ áp dụng sẽ restart HAL để áp dụng. Khi bật, speech phải đi sau một attention trigger: wake phrase nói ra, single click, quay về phía lamp rồi nói, hoặc một người đã enrolled xuất hiện trong khung (`presence.enter`). Event chỉ có stranger không mở voice gate, trừ khi deployment đặt `HAL_PRESENCE_WAKE_STRANGERS=true`. Card liệt kê các phrase **nói ra** hiện được chấp nhận, gồm tên agent hiện tại chính xác cùng các alias cố định `autonomous` và device type; hệ thống quản lý danh sách này. Tải lại Settings sau khi đổi tên agent để thấy tên mới. Khi tắt, mọi câu nói được xử lý mà không cần trigger.

Các cập nhật wake từ HTTP Settings và MQTT được xử lý tuần tự. Lưu Settings chờ lệnh `systemctl restart` HAL cần thiết hoàn tất (riêng lệnh restart có timeout 30 giây), chưa xác nhận voice pipeline sẵn sàng. Lỗi lưu hoặc áp dụng wake được trả qua phản hồi lỗi Settings hiện có; config có thể đã được lưu nếu restart thất bại. Trạng thái wake chờ áp dụng được giữ trong tiến trình os-server hiện tại, nên lưu lại cùng giá trị sẽ thử áp dụng lại. Sau khi thành công, giá trị wake không đổi không gây thêm lần restart. Cập nhật config không có wake chờ áp dụng giữ cách áp dụng HAL bất đồng bộ hiện có. Cập nhật đồng thời wake và cấu hình khác dùng một lần restart HAL; các trường khác đã lưu vẫn được thực hiện các tác vụ đi kèm như bình thường.

**Timezone** (`/setting#timezone`, nội bộ `settings:timezone`, `TimezoneSection.tsx`) — một mục chỉ-admin mà, giống Agent Runtime, **không** nằm trong luồng "Save Changes" của form: nó có nút **Apply** riêng. Mục này tải zone hiện tại và danh sách zone IANA chọn được qua `GET /api/device/timezone`, cho người vận hành chọn một zone từ một dropdown duy nhất (`<select>` nhóm theo khu vực bằng `<optgroup>`, mỗi dòng ghi `(GMT+7) Ho Chi Minh` và sắp theo offset UTC, giống cách các trình chọn timezone phổ biến trên web làm), và hiển thị preview trực tiếp giờ địa phương theo zone đã chọn. Khi nhấn **Apply** nó gọi `POST /api/device/timezone {timezone}`; thay đổi áp dụng ngay lập tức (không cần khởi động lại thiết bị).

**Resting light** (`/setting#led`, nội bộ `settings:led`, `LedSection.tsx`) — chỉ hiện trên device có capability `light` và, giống Timezone, nằm ngoài luồng "Save Changes". Mục này đọc `GET /api/hardware/led/resting` và ghi `PUT /api/hardware/led/resting` thẳng tới HAL: checkbox bật/tắt đèn lúc nghỉ, các ô chọn mặc định của device hoặc một màu mờ có sẵn (Warm white, Candle, Clean white, Peach, Brighter warm), và các thanh trượt hue / trắng↔màu / độ sáng tạo màu tùy chỉnh, lưu sau 250 ms khi ngừng kéo để đèn trên máy đổi theo ngay. Một thông báo vàng cảnh báo khi màu giống đèn trạng thái. Ô màu được phóng lên độ sáng tối đa vì giá trị thật trên strip quá tối để hiển thị. Xem [led-control_vi.md](../../robots/lamp/docs/vi/led-control_vi.md#chủ-máy-chọn-trên-web-ui).

Trang `/edit` độc lập (cũ) đã bị gỡ bỏ; `SettingsPanel` của nó giờ chỉ truy cập được qua các tab `/setting` bên trong Monitor. `/edit` (và link "update →" trong Setup) giờ redirect tới `/setting`.

---

#### Voice — panel Piper

Mọi nút trong panel này đều đặt `type="button"`. Panel nằm trong `<form>` của
trang settings, mà trong form thì `<button>` mặc định là `type="submit"` — nên
Download, Use và Remove đều submit cả form settings, lưu config và restart HAL
ngay dưới chân chính cái request chúng vừa gửi đi. Đó là thứ giết lượt tải giữa
chừng, làm cú Remove ăn 502, và khiến máy nói *"Be right back"* cho một cú bấm
lẽ ra chỉ đụng tới `/opt/piper`. Triệu chứng trông như ba lỗi rời rạc, hoá ra là
một thuộc tính bị thiếu.

`TTSSection` (`system/web/src/pages/settings/TTSSection.tsx`) có thêm provider
thứ tư, **Piper (Local — free)**. Nó khác ba cái kia ở chỗ không có base URL và
không có API key, nên chọn nó là hai ô đó ẩn đi — và bộ lọc ngôn ngữ cũng ẩn
luôn, vì với Piper thì *model chính là ngôn ngữ*, lọc chỉ khiến người dùng không
thấy model mà chính họ đã cài.

Thay vào đó panel hiện trạng thái cài đặt, theo đúng thứ tự phụ thuộc thật:
engine trước, giọng sau. Danh sách giọng chỉ hiện **sau khi** engine tồn tại, vì
tải một model 63 MB mà máy chưa chạy được là 63 MB đổ đi. Mỗi dòng hiện license
ngay cạnh tên model — mọi giọng được mời tải đều dùng thương mại được, nhưng
license khác nhau, và người chọn nên nhìn thấy điều đó.

Tiến độ được poll mỗi hai giây và **chỉ khi có job đang chạy**; đây là phần duy
nhất của trang có trạng thái tự đổi mà người dùng không đụng vào. Việc tải diễn
ra trên thiết bị, nên panel đọc trường `job` từ
`GET /api/voice/piper/status` chứ không tự theo dõi gì.

Lượt tải đang chạy có **dòng riêng** — tên giọng, thanh tiến trình, và
`13.2 / 60.3 MB · 24%` — chứ không phải một con số nhét trên cái nút vừa bấm.
Với đường truyền gia đình, 63 MB mất vài phút, và suốt từng ấy phút đây là thứ
duy nhất đang diễn ra trên trang. Có bộ đếm byte vì phần trăm gần như đứng yên
trên mạng chậm, còn byte thì nhích thấy rõ — đó là khác biệt giữa *đang tải* và
*treo* trong mắt người ngồi nhìn. Dòng này cũng nói rõ việc tải chạy dưới máy và
không mất khi rời trang hay F5, điều đúng sự thật mà nhìn vào không đoán ra.

Panel lấy luôn job trong phản hồi của POST thay vì đợi lượt poll kế tiếp phát
hiện ra. Đó chính là thứ làm cái nút phản ứng lại cú bấm.

Giọng đã cài có hai nút **Use** và **Remove**; riêng giọng đang dùng thì không
có nút nào, vì phải đổi giọng trước rồi mới xoá được. Remove cần bấm hai lần —
lần đầu nút đổi thành *Confirm* — vì model 63 MB tải lại mất vài phút, và xác
nhận ngay trong dòng thì gọn hơn là bật hộp thoại của trình duyệt. Lần bấm thứ
hai cập nhật dòng đó **ngay lập tức** rồi mới đối chiếu lại với máy: một cái nút
đứng yên sau cú xác nhận có chủ đích khiến người ta tưởng cú bấm không ăn, mà
vòng đi-về thì dài vài giây mỗi khi HAL đang restart.

Cách làm là **che** status poll cho riêng giọng đó, chứ không sửa nó. Poll vẫn
chạy suốt lượt xoá, và mỗi lần poll đều báo giọng vẫn còn cho tới khi lệnh xoá
xong — nên một bản đã sửa chỉ bị lần poll sau đó hai giây ghi đè lại, nút Remove
hiện lại và cú xác nhận trông như bị bỏ qua. Lớp che chỉ được gỡ khi status mới
đã về. Xoá giọng **không** làm HAL restart; chỉ lưu cấu hình voice mới làm.
Nút Remove cũng bị ẩn ở giọng cuối cùng còn lại, vì HAL sẽ từ chối — ẩn đi vẫn
hơn là một cái nút mười giây sau trả về một lời từ chối.

Với Piper, dropdown Voice lấy dữ liệu từ **chính status của panel**, không phải
lượt fetch giọng ở tầng trang. Panel poll thẳng HAL, nên danh sách bám theo một
lượt tải hay một lượt xoá ngay khi nó xong, và một lần poll hỏng thì giữ nguyên
câu trả lời trước đó chứ không làm rỗng dropdown.

Khi một thao tác trong panel thất bại vì HAL đang restart — mỗi lần lưu voice là
có một lần restart, và cú bấm rơi vào đúng cửa sổ đó thì mất trắng — panel nói
rõ là máy đang khởi động lại và **không có gì thay đổi**, chứ không chỉ nói
"đang kết nối lại". Chỉ báo kết nối lại khiến người dùng tin là giọng đã bị xoá
trong khi không hề. Nút Download và Remove cũng bị khoá trong lúc mất kết nối,
để cú bấm không rơi vào đó ngay từ đầu.

Dòng trạng thái engine **chỉ hiện khi engine chưa có**. Cài xong rồi thì nó
không nói thêm được gì mà danh sách giọng bên dưới chưa hàm ý sẵn, và một dấu
tick xanh nằm vĩnh viễn trên một bước setup đã xong chỉ là thứ để mắt lướt qua
mỗi lần.

Hai chi tiết nên giữ nếu sau này refactor. Ngôn ngữ câu thử được suy ra từ **tên
giọng** (`vi_VN-…` → `vi`) chứ không lấy từ bộ lọc ngôn ngữ vốn đã bị Piper ẩn —
thiếu chỗ này thì nút Test Voice gửi ngôn ngữ STT của máy và đọc câu mẫu tiếng
Anh bằng model tiếng Việt, nghe ra như giọng hỏng chứ không phải như chọn sai
ngôn ngữ. Và phần ghi công **cố tình không** hiện ở đây: nghĩa vụ đó thuộc về
bên phân phối giọng, không phải người bật nó lên, và `CREDITS.md` mới là chỗ
hoàn thành nghĩa vụ.

**Nút Test Voice bị chặn — không phải báo lỗi — cho tới khi giọng đã có trên
máy.** Bấm giữa lúc đang tải là chạm tới một backend không có model nào để nạp,
và câu trả lời đúng sự thật (503) khi tới tay người dùng lại giống như API sập.
Thay vào đó nút xám lại và nói rõ đang xảy ra chuyện gì (*That voice is still
downloading*, hoặc *Download a voice first* khi chưa chọn gì). Việc chuyển
provider sang Piper cũng không bao giờ tự bịa ra tên giọng: nó chọn một giọng
trong danh sách đã cài, hoặc để trống — vì một cái tên được lưu mà máy không có
sẽ cấu hình thiết bị trỏ tới model nó không nạp được.


## 4. Polling & Data Sources

Monitor poll API system/HW của section đang mở mỗi **5 giây** (`usePolling` trong `system/web/src/pages/monitor/index.tsx`; `GET /api/agent/status` ở sidebar chạy mỗi 10 giây, và một số card tự poll theo nhịp riêng). Flow được cấp dữ liệu bởi stream live theo file (`GET /api/agent/flow-stream`), mỗi frame là snapshot đầy đủ của 500 event mới nhất.

### 4.1 OS Server (Go, port 5000, prefix `/api`)

| Endpoint | Dữ liệu |
|----------|---------|
| `GET /api/system/info` | CPU load, RAM (KB), nhiệt độ, uptime, goroutines, version, deviceId, capabilities (tên các capability đã khai báo — cả Monitor lẫn trang Edit/Settings đều ẩn/hiện tab phần cứng theo danh sách này; xem hook dùng chung `useCapabilities`) |
| `GET /api/system/network` | SSID, IP, public IP, Tailscale IP, signal (dBm), internet (bool), pingMs (RTT của probe internet, 0 = chưa đo) |
| `GET /api/agent/status` | tên runtime đang active, connected (bool), sessionKey (bool), version, emotion, uptime (uptime kết nối runtime phía OS server, giây), agentUptime (uptime tiến trình runtime khi runtime cung cấp, giây — không reset khi OS server restart). Hàng Agent trong card Versions probe phiên bản CLI bất đồng bộ và retry khi boot tạm thời lỗi. |
| `GET /api/agent/recent` | 500 flow event cuối từ JSONL của ngày hiện tại (`local/flow_events_<date>.jsonl`) |
| `GET /api/agent/flow-events?date=YYYY-MM-DD&last=500` | API flow theo file (`last` mặc định 500, tối đa 10000); dùng để seed history cho section Chat |
| `GET /api/agent/flow-stream` | Stream live theo file (SSE): mỗi lần JSONL đổi, gửi 500 event mới nhất dưới dạng một snapshot đầy đủ; section Flow thay toàn bộ danh sách event bằng snapshot đó |
| `GET /api/agent/events` | SSE từ monitor bus, giữ để tương thích |
| `POST /api/agent/restart` | Recovery "start + enable + restart": backend gọi best-effort `systemctl enable <unit>` (để fix vẫn còn sau reboot) rồi gọi `RestartAgent()` của runtime (chạy `systemctl restart <unit>` — start nếu đang stopped). Là nút icon restart nhỏ ở góc phải-dưới card Agent Gateway. |
| `GET /api/system/ota-versions` | Trả `{current, target, min_version, update_available, held_by_floor}` cho từng component (proxy bootstrap `/versions`, gồm device profile đang cài từ `devices.<device_type>`, kèm alias `agent` cho CLI của runtime đang chạy). Card Versions hiện nút `update` ở mọi chỗ `update_available` = true (`held_by_floor` vẫn được trả về nhưng KHÔNG dùng để quyết định nút: nút cài bản đã publish lên chính máy này, giống `software-update <key>` qua SSH, còn sàn chỉ dùng để staging rollout tự động) |
| `GET /api/system/ota-updating` | Các component worker đang cài ngay lúc này (`{updating: [...]}`, kèm alias `agent`). Cố ý làm rẻ — không fetch metadata — vì card Versions poll nó mỗi 2 giây trong lúc cài và hiện `updating…` ở dòng đó thay cho nút |
| `POST /api/system/software-update/:target` | Cài OTA cho một component (trigger OTA duy nhất từ web; os-server gọi bootstrap `POST localhost:8080/force-update/<target>`). `target`: `os-server` \| `bootstrap` \| `web` \| `hal` \| `device` \| `agent`. Bootstrap tự cập nhật bằng cách chạy installer nền, nên có thể restart worker thay thế an toàn; `device` cài profile `devices.<device_type>` đã resolve. **`agent` là target ảo** — os-server tự phân giải sang CLI của runtime đang chạy (`codex`/`claudecode`/`opencode`/`picoclaw` hoặc `hermes`) để trình duyệt không cần biết runtime nào. Hermes nằm trong allowlist, nhưng dòng của nó chỉ có nút `update` khi entry metadata đã pin theo commit (entry chưa pin không được `/versions` báo về). Giới hạn 1 lần / target / 30 giây |
| `POST /api/system/reboot` | Request reboot cần admin auth. OS server trả `202` trước, rồi gọi action reboot có cue của HAL. |
| `POST /api/system/shutdown` | Request shutdown cần admin auth. OS server trả `202` trước, rồi gọi action shutdown có cue và release servo của HAL. |

> **Lưu ý format**: OS server API trả `{ status: 1, data: <payload>, message: null }` khi thành công.

### 4.2 HAL (Python/FastAPI, port 5001, qua os-server `/api/hardware/*`)

Web UI không bao giờ gọi nginx `/hw/*`: mọi request tới HAL đi qua reverse proxy có bảo vệ admin của os-server `/api/hardware/<path>` (`HW` trong `system/web/src/pages/monitor/types.ts`), proxy này chuyển tiếp tới HAL `:5001/<path>` qua loopback. Ở các phần khác của tài liệu, `/hw/<path>` chỉ cùng route HAL đó.

| Endpoint | Dữ liệu |
|----------|---------|
| `GET /api/hardware/health` | Trạng thái 8 hardware: servo, led, camera, audio, sensing, voice, tts, display |
| `GET /api/hardware/presence` | state, enabled, seconds_since_motion |
| `GET /api/hardware/voice/status` | voice_available, voice_listening, tts_available, tts_speaking |
| `GET /api/hardware/servo` | available_recordings, current, bus_connected, robot_connected |
| `POST /api/hardware/servo/upload` | Upload recording CSV (`timestamp` + cột `<joint>.pos`) để thêm/replace animation |
| `GET /api/hardware/display` | mode, hardware, available_expressions |
| `GET /api/hardware/audio/volume` | control, volume (0–100) |
| `GET /api/hardware/voice/mic-level` | SSE stream (~10Hz): level (RMS mic nói, thang int16), threshold (VAD), active, muted, sensing_level / sensing_age_s / sensing_threshold (mic tiếng ồn — mẫu SoundPerception gần nhất, null khi sensing tắt), tts_speaking / music_playing (trạng thái phát live — audio card tắt "Speaking…/Playing music" theo stream thay vì đợi poll status 5s) |
| `GET /api/hardware/led/color` | led_count, color [R,G,B], hex (#rrggbb) |

---

## 5. Các Section Chi Tiết

### 5.1 Overview Section

Khi quay lại Overview, dữ liệu được refresh ngay thay vì chờ nhịp poll 5 giây; dữ liệu card đã có vẫn hiển thị trong lúc tải. Monitor giữ snapshot phiên bản OTA và emotion preset tải thành công qua các lần đổi tab, hiển thị ngay khi quay lại rồi cập nhật nền. Cache chỉ nằm trong bộ nhớ và mất khi monitor unmount. Đổi section sẽ hủy poll của section cũ; không giữ stream của section đang ẩn.

Gồm các card:

**OpenClaw AI**
- Trạng thái connected/disconnected
- Tên agent
- Session key: Acquired / Pending
- **Nút restart icon** ở góc phải-dưới card (nút `RotateCw` nhỏ 24×24). Hỏi `confirm()` rồi POST `/api/agent/restart` — backend làm "start + enable + restart": (1) `systemctl enable <unit>` best-effort để fix vẫn còn sau reboot, (2) `RestartAgent()` của runtime → `systemctl restart <unit>` (start ngay cả khi đang stopped). Icon quay khi request đang chạy; nhãn `OK` / `Failed` hiện ~2.5s sau khi xong. Dùng để phục hồi gateway đã stopped+disabled không cần SSH.

**Network**
- SSID + Signal bars (4 mức dựa trên dBm)
- IP address
- Tailscale IP (chỉ hiện khi `tailscale ip -4` trả về địa chỉ — hoạt động
  cả ở kernel mode lẫn userspace-networking mode)
- Internet status

> Setup gate (`App.tsx`) tự redirect từ AP/host khác sang LAN IP của thiết bị,
> nhưng bỏ qua redirect khi hostname nằm trong dải Tailscale CGNAT
> `100.64.0.0/10` — truy cập qua Tailscale được coi là remote access có chủ ý.

**Presence**
- State (active/idle)
- Sensing enabled/disabled
- Thời gian kể từ lần detect chuyển động cuối

**Voice & TTS**
- Mic available + đang listening (badge LIVE)
- TTS available + đang speaking (badge SPEAKING)
- Volume hiển thị và điều chỉnh theo thang chuẩn hóa 0–100%: 100% tương ứng giới hạn safety `max_volume` của robot (ví dụ volume mixer 50% trên `pro-respeaker-lite`). Khi đọc, chia volume mixer cho giới hạn; khi ghi, nhân với giới hạn và làm tròn thành số nguyên. UI dùng giá trị HAL thực sự áp dụng sau khi ghi và không hiển thị trần nội bộ. Nếu thiếu giới hạn, mặc định là 100; giới hạn bằng 0 thì hiển thị 0% và vô hiệu hóa slider. HAL tiếp tục áp dụng safety cho mọi caller.
- **Thanh VU mic level** (ngay dưới slider volume), lấy từ SSE stream `GET
  /hw/voice/mic-level` (~10Hz, qua proxy `/api/hardware`); RMS thô được map
  sang phần trăm theo thang dBFS (-60dBFS → 0%, 0dBFS → 100%), mỗi bar có
  vạch amber đánh dấu ngưỡng trigger tương ứng kèm số `RMS hiện tại /
  ngưỡng` bên phải label:
  - **Mic level** — mic của voice pipeline (STT); nhảy realtime khi user nói
    vào device. Vạch = ngưỡng VAD (giọng phải vượt vạch thì device mới bắt
    đầu nghe). Rơi về 0 khi TTS/nhạc đang phát (mic đang drain); mờ đi kèm
    chữ "muted" khi mic bị mute.
  - **Noise mic** — mic sensing (SoundPerception): mỗi vòng sensing poll chỉ
    lấy 1 mẫu RMS 0.5s, nên bar này nhảy nấc vài giây/lần chứ không mượt
    (mẫu cũng tạm dừng trong lúc/sau khi TTS nói). Vạch = ngưỡng tiếng ồn
    lớn. Ẩn hoàn toàn khi device không chạy sound perception; về 0 khi mẫu
    cuối cũ hơn 60s.
  Stream vẫn mở khi mic nói bị mute (mic sensing độc lập với nút mute), và
  đóng khi tab trình duyệt bị ẩn.

Ở độ rộng điện thoại **từ 480px trở xuống**, bốn card trạng thái của Overview
xếp một cột. Cách này giữ đủ chỗ cho control và VU meter của Audio, đồng thời
không kéo giãn card Presence ngắn theo card Audio cao hơn.

**Hardware** (card ngang)
- 8 badge: Servo / LED / Camera / Audio / Sensing / Voice / TTS / Display
- **LED color swatch**: ô màu vuông bo góc hiển thị màu hiện tại của dải LED, kèm hex code. Lấy từ `GET /hw/led/color`.

**Scene** (preset ánh sáng)
- Hiển thị danh sách scene preset (reading, focus, relax, movie, night, energize). Lấy từ `GET /hw/scene`.
- Bấm nút để kích hoạt scene qua `POST /hw/scene` với `{"scene": "<tên>"}`.
- Scene đang active được highlight màu amber.

**Power**
- Card gọn trên Overview có hai nút **Reboot** và **Shut down**, mỗi nút hỏi xác nhận; sau khi request được nhận, cả hai bị disable để trang không xếp thêm power operation thứ hai.
- Nút gọi endpoint os-server cần admin auth, không gọi HAL trực tiếp. Server trả ACK trước và có single-flight guard, rồi HAL chạy cùng chuỗi action tường minh với physical control: reboot phát cue rồi restart; shutdown phát cue và release servo trước khi tắt nguồn.

**Servo Pose**
- Pose đang chạy (current)
- Danh sách servo recordings/animations (từ `GET /hw/servo`)
- Mỗi recording có thể phát qua `POST /hw/servo/play` (tên recording)
- UI có nút `Upload CSV` để thêm/replace recording qua `POST /hw/servo/upload` (multipart: `file`, `recording_name`)

> **Bố cục & pill cloud.** Cụm thiết bị (hàng 2) chia thành hai cột bằng nhau:
> cột phải cho các card biểu cảm (Emotion, Servo Pose, Versions) và cột trái cho
> các card trạng thái gọn (Hardware, Scene, Power); dưới ~860px hai cột gộp về
> một. Versions nằm ở cột phải để hai cột cân chiều cao, tránh cột phải bị cụt
> dưới Servo Pose. Danh sách preset Emotion và danh sách recording Servo render
> dưới dạng **pill cloud** — pill đang active được đẩy lên đầu để trạng thái hiện
> tại đọc trước tiên, toàn bộ wrap tự do (không cuộn). Cả card Emotion và Servo Pose chia
> hai cột: phần tóm tắt trạng thái hiện tại (Emotion: emoji + tên; Servo: pose
> hiện tại + nút Release) ở cột trái rộng cố định, còn pill cloud preset/recording
> lấp phần còn lại bên phải. Dưới ~360px hai cột xếp chồng. Tên Emotion luôn dùng
> màu chữ tương phản cao của theme, nằm trong pill có nền rõ ràng; màu LED của
> preset chỉ làm chấm, viền và tint nhẹ. Vì vậy preset tối như `sleepy` vẫn đọc
> rõ ở dark mode. Phần tóm tắt chừa đủ chỗ cho emoji và tên dài như
> `acknowledge`; khi card hẹp, pill cloud sẽ xuống hàng dưới thay vì đè lên
> trạng thái hiện tại.

### Khả năng đọc của Settings

Dropdown Settings dùng menu Radix cùng theme, mục chọn màu amber, tự đặt vị trí theo viewport, có cuộn, điều hướng bàn phím và đóng bằng Escape. Các lựa chọn Auto/default có giá trị rỗng vẫn chọn được. General giữ control hiện có.

Các mục Settings ngoài General có focus bàn phím rõ, chiều cao control nhất quán và chữ 16px cho ô nhập/select trên mobile. Language, Voice, Realtime và Channels dùng chữ hướng dẫn lớn hơn. MCP Tools và Plugins áp dụng thao tác riêng nên không còn hiện nút Save Changes chung.

Trên điện thoại, trường key/value của MCP header xếp dọc với nhãn rõ; nhóm thao tác Plugin và Scheduled nằm dưới tên và trạng thái. My Voice xử lý tên dài và đặt bản ghi bên dưới tên file; nút xóa ảnh Face có vùng bấm lớn hơn. Các thao tác kết nối Facebook có thể xuống hàng. Nhãn Schedule editor liên kết với control và nút chọn ngày công bố trạng thái nhấn. Nút trong hướng dẫn Runtime không submit form Settings; dialog hỗ trợ focus bàn phím và đóng bằng bàn phím.

### Khả năng đọc của Monitor

History của Chat phủ vùng chat trên điện thoại thay vì ép hẹp hội thoại; Escape đóng panel, focus bàn phím nằm trong history đang mở trên mobile, chọn hoặc tạo hội thoại sẽ trở lại chat. Export và Clear được gom vào menu thao tác hỗ trợ bàn phím. Chọn hội thoại và đổi tên/ghim/xóa dùng được bằng bàn phím và touch. Nút thao tác tin nhắn có vùng bấm lớn và luôn thấy, metadata xuống hàng, nút xuống cuối luôn nằm phía trên composer kể cả khi có bản nháp nhiều dòng hoặc tệp đính kèm. Enter chỉ gửi khi không đang xác nhận ký tự IME; Shift+Enter vẫn xuống dòng.

Overview hiển thị trạng thái đang tải trung tính trước khi nhận trạng thái agent; các hàng phiên bản xếp lại kèm nhãn trên điện thoại. System xếp đồng hồ đo phía trên biểu đồ lịch sử tại 768px. Camera đặt snapshot dưới stream trên điện thoại, thêm nhãn cho ô tracking, thu gọn bounding box tùy chọn vào Advanced tracking, hiển thị lỗi thao tác và khóa nút khi đang gửi yêu cầu.

Logs dùng dropdown chọn nguồn tại 640px, chữ log lớn hơn và timestamp rõ hơn. Flow tăng kích thước ô tìm kiếm và nút toolbar. CLI có tab phiên dùng được bằng bàn phím, phím terminal trên mobile và nút Reconnect khi mất kết nối; kết nối lại mở shell mới và xóa lịch sử cuộn của terminal trước đó. API Docs có trạng thái đang tải/lỗi cùng Reload và Open in new tab. Monitor và Settings dành hàng riêng cho footer để liên kết mã nguồn không che nội dung.

Servo dùng tiêu đề lớn hơn, số đo tương phản rõ và nút thoáng trong overview hai cột, chuyển một cột tại 900px. Tại 600px, mỗi joint xếp tên, góc hiện tại, ô target và slider toàn chiều ngang; live drag và giới hạn góc giữ nguyên. Users có metadata lớn hơn, nút thao tác luôn hiện, card người dùng gọn trong lưới tự xếp cột (cột tối thiểu 280px, card tối đa 340px) và lưới quan sát hai cột; cả hai chuyển một cột toàn chiều ngang tại 640px, profile files thu gọn và panel cooldown nhận diện thu gọn riêng. Pairing dùng tiêu đề kết nối lớn hơn, badge trạng thái, khối thao tác/lỗi rõ và hàng metadata responsive; ID kết nối Buddy nằm trong Connection details thu gọn.

### 5.2 Section Pairing

`/monitor#pairing` là khu vực riêng cho kết nối và ghép đôi. Nó render cả
`BuddyCard.tsx` (Autonomous Buddy cho Mac) và `HarnessCard.tsx`, tách các tích hợp
máy tính này khỏi các card trạng thái thiết bị trên Overview. Trang có header kết
nối gọn và bố cục card hai cột, chuyển thành một cột khi nhỏ hơn 760px.

**Autonomous Buddy (Mac)**
- Hiển thị máy Mac đã ghép và trạng thái kết nối. Khi chưa có máy Mac nào, **Pair new Mac** tạo mã để nhập trong Autonomous Buddy → *Pair with device…*.
- Thu hồi ghép đôi yêu cầu xác nhận và gọi `DELETE /api/buddy`.

**Ghép đôi Harness**
- Nhãn kết nối chỉ hiển thị **CONNECTED** khi đọc được trạng thái kết nối sống, **OFFLINE** khi còn pairing nhưng không có kết nối, và **STATUS UNAVAILABLE** khi đọc trạng thái lỗi. Pairing đã lưu được giải thích riêng, không đồng nghĩa máy đang online. Hướng dẫn offline cho biết yêu cầu mới chưa thể tới máy và không cần ghép đôi lại chỉ vì offline.

- **Generate pairing code** gọi `POST /api/harness/pair` có xác thực admin, không cần
  chọn máy tính. OS tạo mã sáu ký tự có hiệu lực 60 giây.
- Trên cùng mạng nội bộ, mở Harness Desktop → Settings → Devices, chọn thiết bị
  Autonomous được tự tìm thấy rồi nhập mã của thiết bị. Với CLI, chạy
  `harness autonomous-device discover --json`, sau đó
  `harness autonomous-device pair --device <discoveryId> --code-stdin`.
  CLI kết nối trực tiếp đến `/api/harness/ws` của thiết bị; việc tìm thiết bị dùng
  dịch vụ `_autonomous._tcp` đã có. Không nhập IP hoặc cần tài khoản backend.
- `GET /api/harness/pair/status` chỉ dành cho admin, trả mã đang hoạt động, `expires_at`,
  `state`, `pairing` với `Cache-Control: no-store`. OS Monitor đọc route này và `/status`
  mỗi hai giây. Mã bị xoá khi hết hạn, huỷ hoặc hoàn tất; không lưu lâu dài.
  `POST /api/harness/pair/cancel` huỷ lần ghép đang chờ.
- Unpair cần xác nhận, gọi `DELETE /api/harness` có xác thực admin. Harness dùng giao thức
  pairing/phiên E2EE gốc và giữ khóa riêng, độc lập với Buddy.

**Giọng nói Harness-only**

Trang Pairing chỉ hiện công tắc Harness-only voice và mô tả khi OS trả `supported:true` (board hiện tại có cấu hình MPR121). Thiếu metadata hỗ trợ thì ẩn công tắc. Pairing và thông tin agent đang focus vẫn hiển thị; API/MQTT cũng chặn bật nếu không đáp ứng phần cứng.


- Khi có máy đã ghép đôi, `HarnessCard.tsx` hiển thị `HarnessVoiceMode.tsx`. **Focused Harness agent** đồng bộ pane agent đang focus trong app Harness, kể cả khi mode tắt. Web không có bộ chọn agent; target hội thoại của `harness-use` thông thường vẫn độc lập.
- Bật **Harness-only voice** để gửi yêu cầu giọng nói thẳng đến agent đang focus; kết quả vẫn qua TTS thiết bị. Chat text giữ hành vi hiện có. Trạng thái nằm trong RAM; restart tắt mode và focus đồng bộ lại sau reconnect. Đổi focus giữa capture từ chối capture cũ và yêu cầu nói lại. Task đã gửi giữ route phản hồi gốc.
- Mode/focus refresh mỗi 2 giây qua `GET /api/harness/voice-mode`. Công tắc chỉ gửi `{enabled}` bằng `PUT`, dùng được khi offline hoặc chưa có focus; nếu đọc mode lỗi thì khóa đến khi refresh thành công. UI giải thích rõ khi offline, thiếu focus hoặc CLI chưa hỗ trợ capability nên không thể delivery giọng nói.
- Delivery chưa rõ chặn mutation giọng nói Harness mới nhưng focus hiển thị vẫn đồng bộ. **Check delivery** đọc receipt hiện có, không gửi lại. **Continue without retrying** yêu cầu xác nhận rồi gửi `resolution:"do_not_retry"` cùng `idempotencyKey` pending chính xác đến `/api/harness/voice-mode/resolve`; task trước vẫn có thể chạy.
- `HarnessQuestion.tsx` refresh câu hỏi live mỗi 10 giây khi focus khả dụng, kèm **Refresh agent question**. Form hỗ trợ chọn một, chọn nhiều và text tự nhập. **Send answer** gửi đủ key câu hỏi chính xác với request ID live và revision focus, từ chối focus cũ. Người dùng cũng có thể trả lời lần lượt bằng giọng nói.
- Thay đổi và endpoint câu hỏi/receipt cần xác thực admin. Xem [Tích hợp Harness](harness_vi.md#chế-độ-giọng-nói-harness-only) về API và yêu cầu rollout OS/HAL/CLI tương thích.

Section Pairing có mặt với người dùng không-debug.

**Display Eyes**
- Expression đang hiển thị (mode)
- Danh sách expressions available

> **Card gate theo capability.** Các card phần cứng ở Overview bị ẩn trên thiết bị
> không có capability tương ứng, nên trang chỉ hiển thị thứ thiết bị thực sự làm được
> (ví dụ intern-v2 không có servo, scene, hay expression):
> - **Emotion** và **Servo Pose** gate theo capability đã khai báo (`expression` / `motion`) lấy từ `GET /api/system/info` → `capabilities`.
> - **Scene** là route *bên trong* capability `light` (lamp khai báo `light:[led,scene]`; intern-v2 khai báo `light:[led]`), nên không phân biệt được qua danh sách capability — card chỉ render khi `GET /hw/scene` trả về danh sách scene.

**System quick stats**
- CPU, RAM, Temp, Uptime dạng pill

### Sidebar Footer

Dưới nav items và trạng thái OpenClaw, sidebar hiển thị version của cả 3 repo:
- **Web** (teal): inject lúc build từ `package.json` qua Vite `define` (`__WEB_VERSION__`)
- **OS server** (amber): từ `GET /api/system/info` → field `version` (Go ldflags)
- **HAL** (blue): từ `GET /api/system/info` → field `halVersion`. OS server tự gọi `:5001/version` của HAL qua loopback mỗi phút 1 lần (cache) rồi re-expose qua API của OS server, browser không cần truy cập trực tiếp `/hw/*` (nginx chặn `/hw/` chỉ cho loopback).

### 5.3 System Section

**Performance** — 3 GaugeRing SVG:
- CPU: màu amber, hiện `%`
- Memory: màu blue, detail `used/total MB` (chuyển đổi từ KB: `value / 1024`)
- Temp: màu teal (< 70°C) hoặc red (≥ 70°C), scale 0–85°C

**CPU History / RAM History** — Sparkline chart (area + line):
- Lưu 60 điểm lịch sử (`HISTORY_LEN = 60`)
- Cập nhật mỗi 5 giây

**Process**: goroutines, uptime, version, deviceId
**Network Detail**: SSID, IP, signal, internet

### 5.4 Workflow Section

Flow feed hybrid theo file:

| Type | Màu | Ý nghĩa |
|------|-----|---------|
| `lifecycle` | amber | Agent bắt đầu / kết thúc run |
| `tool_call` | teal | AI gọi một tool |
| `thinking` | purple | AI đang suy nghĩ (streaming) |
| `assistant_delta` | blue | AI đang trả lời (streaming delta) |
| `chat_response` | green | Chat response final |

Mỗi event hiển thị: type badge, phase (nếu có), runId (8 ký tự đầu), timestamp, summary text, error (nếu có).

- Cả load ban đầu lẫn update live đều đến từ `GET /api/agent/flow-stream` (SSE bắn khi file đổi); mỗi frame là snapshot đầy đủ của 500 event mới nhất và thay thế danh sách. Stream chỉ mở khi section Flow hoặc Chat đang active và đóng khi tab bị ẩn; không có poll fallback.
- Turn/event hiển thị được suy ra hoàn toàn từ JSONL flow log.

**Turn Pipeline (SVG)** — `FlowDiagram` trong `system/web/src/pages/monitor/FlowSection/FlowDiagram.tsx`. Bố cục đầy đủ (ba vùng OS server / HAL / OpenClaw, lưới cột OpenClaw, Cron thuộc OS server, hàng HAL thẳng Tool, bảng tọa độ) nằm trong **`docs/flow-monitor.md`**; tóm tắt tiếng Việt: **`docs/vi/flow-monitor_vi.md`**.

Hành vi gom nhóm Turn Pipeline:
- Turn vẫn bắt đầu từ các event input/trigger (`sensing_input`, `chat_input`, `schedule_trigger`, ...).
- UI giờ neo mỗi turn theo `run_id` đầu tiên phát hiện được (ở root event hoặc trong `detail`).
- Với user mic actions: mỗi `sensing_input` dạng `[voice]` / `[voice_command]` (và `voice_pipeline_start`) tạo một turn riêng, ngay cả khi các event có thể đang chung `run_id`.
- Với chat gõ tay: mỗi `sensing_input` dạng `[web_chat]` (composer của monitor, icon 🖥) hoặc `[mqtt_chat]` (MQTT `chat.send` từ app điện thoại, icon 📱) tạo boundary turn riêng nên không bị merge chung với turn voice/sensing kề nhau. Cả hai nằm trong filter category **Web**, có chip sub-type riêng (`web` / `mqtt`), badge phân biệt được nguồn — còn lại phía server hai loại y hệt nhau.
- Với user chat actions: mỗi `chat_input` (telegram input) tạo một boundary turn riêng, nên sẽ không bị merge chung với turn voice kề nhau dù OpenClaw có reuse `run_id`.
- Nếu event phía sau có `run_id` khác, Monitor sẽ tách thành một turn agent suy diễn mới.
- **Badge loại turn** (`motion`, `voice`, …): cùng một `run_id` có thể vừa motion (camera) vừa voice; trước đây segment đầu quyết định badge nên dễ hiện `motion` dù user vừa nói. Sau khi gom turn, nếu có bất kỳ `sensing_input` kiểu `[voice]` / `[voice_command]` thì badge ưu tiên voice hơn motion.
- `OUT` chỉ lấy từ `tts_send`/`intent_match` cùng `run_id` với turn (hoặc event không có run_id), tránh ghép nhầm IN/OUT giữa các turn.
- Token LLM hiển thị trên các node LLM (Agent Call / Thinking / Response): `in/out` và nếu có `token_usage` thì thêm `cache read/write` + `total`.
- Với Telegram input, summary placeholder kiểu `[telegram]` sẽ không còn khóa cứng trường `IN`; nếu event đến sau cùng `run_id` có message thật, UI sẽ thay placeholder bằng nội dung đó (và sẽ override cả sensing_input text như SOUND nếu cùng nằm trong một UI turn). Nếu message Telegram bị thiếu hoàn toàn (ghost turn) thì turn type sẽ thành `unknown` để tránh hiểu nhầm “TG IN”.
- Fallback tạm thời: khi không lấy được text Telegram, UI sẽ hiển thị `Message content from telegram`.
- Turn badge luôn render dòng `IN`; nếu thiếu input, UI sẽ hiển thị `Input not captured`.
- **Icon trên turn-badge** (`FlowSection/TurnBadge.tsx`) — mọi glyph đều là icon Lucide cho nhất quán với header: icon nguồn ở row 1 lấy từ `TYPE_LUCIDE` (theo turn type), `BROADCAST→Megaphone`, duration`→Timer`, queued`→PauseCircle`, audio-debug`→Mic`, pose-bucket`→Armchair`, no-reply`→Ban`, channel-out`→MessageSquare` / TTS`→Volume2`, dropped & queued`→PauseCircle`, closed-stream`→TriangleAlert`, silent`→Moon`, HW`→Lightbulb`, lightbox-close`→X`, View-pipeline`→Workflow`. Không còn emoji.
- **Chip user theo turn** — current-user ghi nhận của turn render qua component dùng chung `UserAvatar` (tên + ảnh khuôn mặt đã enroll, fallback `UserRound`), cùng component mà chip header dùng, nên "ai" hiển thị giống hệt ở header và mọi badge. Map ảnh (`/face/owners`, tên→tên file) được truyền xuống từ `FlowSection` qua prop `userPhotos`.
- Header Flow Panel dùng icon Lucide nhất quán (brand `Hexagon`, `Summary→ClipboardList`, `Canvas→LayoutDashboard`, `Bundle→PackageOpen`, `Full day→CalendarDays`, `Clear→Trash2`) — không còn emoji.
- **Chip current-user** — khi thiết bị nhận diện một người đã enroll, chip header hiển thị **tên + ảnh khuôn mặt** của người đó (ảnh enroll đầu tiên qua `GET /face/photo/<label>/<file>`, poll `/face/owners` mỗi 30s để map tên→tên file); khi `unknown` hoặc ảnh thiếu/lỗi thì fallback về icon Lucide `UserRound` chung chung. Tên lấy từ `GET /identity/current-user` (poll mỗi 5s), **không** phải `/face/current-user`: endpoint đó chỉ trả lời "camera đang thấy ai", nên chip bị trống mỗi khi không có ai trong khung hình — và trống vĩnh viễn trên thiết bị không có camera — kể cả ngay sau khi speaker-ID vừa nhận ra một người đã enroll. HAL giải theo thứ tự face-rồi-voice, nên chip hiển thị user từ khuôn mặt bất cứ khi nào camera có người, và người vừa nói trong các trường hợp còn lại. Giá trị là nhãn đã chuẩn hoá (`long`), nên `capitalize` của chip và việc tra ảnh hoạt động y hệt nhau cho cả hai giác quan — tiền tố `Speaker - ` trong transcript không bao giờ lọt tới đây. Tooltip cho biết danh tính đến từ giác quan nào (*nhìn thấy* hay *nghe thấy*).
- Header Flow Panel: `↓ Bundle`, `full day`, `🗑 Log`.
- `↓ Bundle` = **một lần bấm tải hai file**: JSONL server (fetch + blob, `GET /api/agent/flow-logs?last=10000`; server kẹp `last` về 2000 nên file là `flow_<date>_last2000.jsonl`) và JSON snapshot trong browser (`events` + `groupIntoTurns` → `flow_ui_snapshot_*.json`).
- `full day` = cả file JSONL trong ngày.
- Nút `🗑 Log` sẽ hỏi xác nhận trước, gọi `DELETE /api/agent/flow-logs` để truncate flow log, rồi xóa events đang hiển thị trong Flow UI.
- **Modal Filters** (`FlowSection/FiltersModal.tsx`) — header của danh sách turn chỉ giữ ô tìm kiếm text và nút **Filters** (gắn badge `Filters · N` với số nhóm filter đang bật). Bấm nút mở một modal căn giữa chứa toàn bộ bộ lọc: **Sources** (quick-toggle Mic / Cam / Btn / CH / Web / Cron / Sys, kèm Dropped khi có), **Sort** (Newest / Oldest / Slowest / Fastest / ↑↓ Tokens), **Sub-types** (toggle theo từng type kèm shortcut All-on / Enable-all), và **Time range** (preset nhanh Last 15m / 1h / 6h / Today, cùng hai pill From/To có nhãn và icon đồng hồ nối bằng mũi tên; native `<input type="time">` được bỏ chrome qua `.lm-time-input` và bound đang bật sẽ tô màu amber). Footer có **Reset all** và **Done**. Modal được render bên trong cây FlowSection (dưới `.lm-root`) nên token `--lm-*` hoạt động ở cả dark và light mode; đóng bằng click overlay, nút ✕, **Done**, hoặc `Esc`. Mọi state filter nằm ở `FlowSection/index.tsx` và truyền vào qua props, nên việc mở/đóng không bao giờ reset filter.
- **Icon Lucide cho sub-types** — các chip source và sub-type dùng icon Lucide (`TYPE_LUCIDE` trong `FlowSection/types.ts`, ví dụ `voice→Mic`, `cmd→Mic2`, `motion→Eye`, `activity→Activity`, `voice_emo→Speech`, `emotion→Smile`, `web→Monitor`, `sys→Settings`) thay cho emoji, kế thừa `currentColor` và độ mờ on/off của chip.
- Danh sách Turn history: hiển thị tất cả turn (mới nhất ở trên) suy ra từ các event đang có trong bộ nhớ — tức 500 event mới nhất mà flow stream gửi, không phải cả ngày (dùng **`full day`** để xem cả ngày).
- Bộ nhớ event của Flow được giới hạn `FLOW_EVENTS_MAX` = 10 000 event, nhưng snapshot của stream chỉ có 500 nên thực tế không chạm tới giới hạn này.
- Heuristic ghép turn Telegram: nếu turn Telegram fallback (không có text input thật) đứng ngay trước turn có output agent trong vòng 30 giây, Monitor sẽ ghép thành 1 turn để câu trả lời đi cùng input Telegram.

### 5.5 Camera Section

- **Camera Stream**: MJPEG live stream từ `GET /hw/camera/stream` (downscaled + throttled; mặc định ~10fps, ~320px chiều ngang). Thẻ `<img>` remount bằng kết nối mới (cache-buster `streamEpoch` tăng lên) mỗi khi camera chuyển sang enabled — qua nút Enable hoặc auto-enable phát hiện bởi polling — nên video live trở lại ngay, khỏi refresh trang. Lỗi stream xảy ra ngay sau enable (loop capture của HAL cần ~1-2s để có frame đầu) không bị latch: nó tự retry sau khoảng trễ ngắn tới khi load được frame.
- **Camera Snapshot**: Ảnh tĩnh từ `GET /hw/camera/snapshot`, có nút Capture để chụp mới.

### 5.6 Logs Section

- Tab log runtime: HAL, OS (os-server), Buddy, **Bootstrap**, cùng **Agent** và **Agent Service** (source id `bootstrap`, `openclaw` / `openclaw-service`). Tab **Bootstrap** đọc journal của systemd unit `bootstrap.service`.
- Tab **Agent**/**Agent Service** là runtime-aware — backend (`resolveLogSource` trong `server/logs.go`) trỏ chúng tới backend agentic nào đang chạy:
  - openclaw: `Agent` → `/var/log/openclaw/agent.log` (fallback file `/tmp/openclaw/openclaw-*.log` mới nhất), `Agent Service` → `journal:openclaw.service`
  - hermes: `Agent` → `/root/.hermes/logs/agent.log`, `Agent Service` → `journal:hermes-gateway.service`
  - picoclaw: `Agent` → `/root/.picoclaw/logs/gateway.log`, `Agent Service` → `journal:picoclaw.service`
  - codex: `Agent` → `journal:codex.service`, `Agent Service` → `journal:codex.service` (bridge gatewayd không có file log — chỉ journal)
- Tab **Bootstrap** nạp `N` dòng gần nhất qua `GET /api/logs/tail?source=bootstrap&lines=N`, rồi nhận dòng mới qua SSE `GET /api/logs/stream?source=bootstrap`; cả hai request đều yêu cầu xác thực.
- Mỗi panel stream qua SSE (`GET /api/logs/stream?source=<source>`); lần nạp đầu và nút làm mới đọc `GET /api/logs/tail?source=<source>&lines=N`.
- Hỗ trợ filter theo level (ALL/DEBUG/INFO/WARN/ERROR) và tìm kiếm text/regex.

> **Lưu ý**: Camera có vai trò kép — (1) hiển thị live stream cho user xem, (2) nguồn dữ liệu sensing tự động. Sensing service đọc frame từ camera mỗi 2s để detect motion, face (Haar cascade), và light level. Khi phát hiện sự kiện đáng kể (người xuất hiện, chuyển động lớn), auto-snapshot full-resolution JPEG được gửi kèm event tới OpenClaw AI để phân tích bằng vision.

### 5.7 Chat Section

Giao diện chat tương tác với agent. Layout: sidebar (danh sách hội thoại) + vùng chat chính.

**Hội thoại**
- Nhiều hội thoại lưu trong localStorage (tối đa 50, mỗi cái 200 tin nhắn).
  Ảnh đính kèm quá lớn so với quota localStorage nên data-URL bị strip lúc
  save và được lưu riêng trong **IndexedDB** (`lib/chatImageStore.ts`, key
  theo message id); một effect lúc mount gắn lại ảnh sau reload và prune các
  entry mà message không còn tồn tại. Xóa hội thoại (hoặc Clear/history-TTL)
  cũng xóa luôn ảnh đã lưu.
- Sidebar: tìm kiếm, ghim, đổi tên (double-click), xóa (xác nhận 2 lần), xuất TXT
- Nhóm và sắp xếp theo **hoạt động tin nhắn gần nhất**: Today / Yesterday / This week / Older, ghim lên đầu. Mỗi header nhóm có đường kẻ mảnh và số lượng item. Chính thứ tự này cũng dùng khi giữ 50 hội thoại local, nên một thread cũ nhưng đang hoạt động không bị đẩy ra trước một thread mới nhưng không dùng.
- Mỗi dòng hiển thị một chấm avatar màu (hash từ id hội thoại, theo palette), tiêu đề, nhãn thời gian tương đối đã bản địa hóa cho hoạt động mới nhất (`vừa xong` / `5 phút` / `2 giờ` / `hôm qua` / `3 ngày`, ẩn khi hover), và preview tin nhắn cuối. Hội thoại đang mở được đánh dấu bằng thanh dọc amber bên trái. Tìm kiếm hiện số kết quả; Escape hoặc nút xóa sẽ đưa ô tìm kiếm về trống.
- Phím tắt: Cmd/Ctrl+N tạo chat mới
- Sidebar thu gọn được

**Nhập tin nhắn**
- Textarea, Shift+Enter xuống dòng, Enter gửi
- **Menu "+"** (`chat/PlusMenu.tsx`) ở mép trái ô nhập. Mở hướng lên trên (ô nhập ghim đáy); đóng khi click ra ngoài / Escape, và luôn ở trạng thái đóng khi đang gửi turn. Các mục:
  - **Attach file** — nút paperclip cũ, nay nằm trong menu. Kéo thả và dán clipboard vẫn đính kèm trực tiếp, không cần qua menu.
  - **Skills** ▸ — sub-menu bay ra, chứa 4 màn hình bên dưới, có một đường kẻ tách 2 mục thêm skill của chính mình (Write / Upload) khỏi 2 mục làm việc với skill đã có sẵn (Browse / Manage). Mỗi cái mở một modal portal (`chat/ModalShell.tsx` shell dùng chung + `chat/styles.ts` style field); bản thân các dòng menu nằm ở `chat/MenuPanel.tsx`, dùng chung với menu trong header Manage skills.
- Đính kèm file/ảnh (tối đa 10 MB): "+" → Attach file, kéo thả, dán từ clipboard
- Gửi qua `POST /api/sensing/event` với `type: "web_chat"`. Handler mark run qua `MarkWebChatRun(runID)` để reply của agent bị suppress TTS (chỉ hiện trong UI này) và bỏ qua wake greeting / opening filler. **Ảnh** đính kèm đi trong mảng `images` của payload (raw base64, mỗi ảnh một phần tử — composer nhận nhiều ảnh); với từng ảnh, handler (1) lưu vào `/tmp/web-chat-<ms>-<i>.jpg` (có index nên ảnh trong CÙNG một lượt không đè tên nhau) và chèn tag `[image: <path>]` để tool đọc file trực tiếp (vd face enrollment), và (2) chạy gate describe-first trong `system/vision` (xem `docs/realtime-voice.md`, phần "Bàn giao frame"): main model text-only nhận dòng `[image description]` do vision model của catalog tả, model có vision nhận attachment thô. Cả hai bước chạy TRƯỚC fork queue lúc agent bận, nên turn bị queue replay với description đã nằm sẵn trong message.
- Khi đang chờ reply, mũi tên Send đổi thành **Stop**. Stop hủy HTTP send nếu server chưa nhận; nếu run đã được nhận thì browser đóng bubble đó và bỏ qua mọi event đến muộn của run. Nó không interrupt task đang chạy trong agent và không xóa session/memory của agent.
- **File không phải ảnh** chỉ đi bằng mảng `files` riêng — mỗi phần tử `{name, mime, content}`, base64 — tuyệt đối không đi vào field `images`; `agentfile.SaveInbound` xử lý chúng. Chúng rơi vào `/tmp` với **đúng đuôi thật** và turn mang tag `[file: <path> (<name>)]`. Tách hai field chứ không gộp một, vì cách xử lý ngược nhau: ảnh phải qua gate describe-first, tài liệu thì không được. Trước khi tách, mọi thứ composer nhận đều gửi trong một field `image` duy nhất và ghi thành `/tmp/web-chat-*.jpg`, nên đính một file PDF sẽ tạo ra file bị gán nhãn sai là ảnh rồi fail ở gate vision — composer nhận mọi loại file, nhưng thực tế chỉ ảnh là chạy được. `name` của client chỉ dùng để lấy đuôi (phải là hậu tố chữ-số ngắn, không thì `.bin`) và làm nhãn hiển thị; tên file ghi ra là tự sinh, nên tên kiểu `../../etc/passwd` không lái được chỗ ghi. Giới hạn 10 MB sau decode, khớp với check của composer. Đường MQTT `chat.send` mang đúng các field đó và đi ngược vào chính handler này qua loopback, nên điện thoại và browser đính file bằng chung một bản cài đặt (`docs/mqtt.md`).
- **File đi NGƯỢC ra khỏi turn** (`chat/AgentFiles.tsx`). Một turn chỉ có thể *gọi tên* file nó tạo ra — bảo "chụp hình" thì nó kết thúc bằng một path tuyệt đối trên device kiểu `/root/.openclaw/media/hal-snapshots/snap_*.jpg`, thứ mà browser không đọc được. Mỗi message agent đã xong được quét tìm path dạng đó, mỗi cái tìm thấy render ngay dưới bubble: ảnh hiện inline, còn lại là chip download, cả hai trỏ vào `GET /api/agent/file?path=…`.
  - **Quét 3 chỗ, không chỉ text của reply.** Bảo agent gửi ảnh thì nó thường gọi tool channel — `message {"action":"send","media":"/root/.openclaw/media/…jpg"}` — còn câu trả lời không hề nhắc path nào, chỉ quét text là không thấy gì. **args** của tool có path (server log nguyên vẹn trong `detail.args` của flow event; chỉ phần HIỂN THỊ trên chip bị cắt), còn `curl /camera/snapshot` thì path nằm trong **result** của tool.
  - **Detect ở client, nhưng enforce thì không.** Quét trong browser thì không phải móc hook nào vào turn pipeline và chạy được với cả hội thoại đang nằm trong localStorage — thứ mà scan phía server lúc turn kết thúc không bao giờ với tới. Danh sách root trong `AgentFiles.tsx` chỉ là bộ lọc để UI khỏi bắn request chắc chắn hỏng, **không phải** quyền. Path mất hoặc bị từ chối thì attachment tự biến mất (`onError`), path vẫn còn đọc được dưới dạng text.
  - Khác với `/api/sensing/agent-snapshot/<runtime>/<source>/<name>` (`camera_snapshot.go`) mà Flow Monitor dùng cho snapshot từ tool result: cái đó serve thứ *device* tự resolve từ các segment, còn cái này serve thứ *agent* gọi tên.

**Menu Skills (ô nhập "+" → Skills)**

| Mục | File | Trạng thái hiện tại |
|-----|------|---------------------|
| **Create with Agent** | `chat/PlusMenu.tsx` | Điền sẵn prompt skill-creator vào ô chat và focus ô đó; không tự gửi tin nhắn. |
| **Write skill** | `chat/WriteSkillModal.tsx` | Form 3 field — Skill name / Description / Instructions — đúng cấu trúc một `SKILL.md` (name + description → front-matter, instructions → body). Lưu qua `POST /api/agent/skills`; thành công thì modal hiện đường dẫn đã ghi. Xem "Viết + cài skill" bên dưới. |
| **Upload a skill** | `chat/UploadSkillModal.tsx` | Cài file `.skill`/`.zip` mà người dùng chọn từ máy họ — vùng kéo-thả hoặc chọn file, giới hạn 16 MB phía client khớp với phía device. Cùng đích và cùng luật thay-thế-khi-trùng-tên với nút Install từ store; chỉ khác nguồn của bytes. |
| **Browse skills** | `chat/BrowseSkillsModal.tsx` | Chạy thật với catalog Autonomous Agent Skills — xem "Catalog skill" bên dưới. |
| **Manage skills** | `chat/ManageSkillsModal.tsx` | Skills đang có trong thư mục skill của runtime đang chạy (`GET /api/agent/skills`), 2 view: ô search cộng một **list 4 cột** — skill (`/music` kèm description ở dòng dưới), Store, số file, last updated — click vào mở view detail dùng đúng trình duyệt file 2 pane của Browse skills. Store hiện **In store** khi catalog có slug/name khớp, **Device only** khi không có, hoặc **Unknown** khi device không đọc được toàn bộ catalog; đây là độ sẵn có hiện tại trên catalog, không phải nguồn đã cài skill. Dùng list chứ không dùng lưới card như Browse, vì với skill đã cài thì câu hỏi có ích là đang có gì và đổi lần cuối lúc nào, cái đó đọc theo cột thẳng hàng dễ hơn. Cột "Last updated" hiện ngày cố định dạng `MM/DD/YYYY` — cột này sinh ra để nhìn phát biết skill nào cũ, mà ngày tuyệt đối rộng cố định thì so theo cột dễ hơn hẳn kiểu tương đối trộn đơn vị ("3d ago" cạnh "12m ago"); thứ tự MM/DD/YYYY hardcode chứ không theo locale để cột luôn thẳng hàng và cùng một ảnh chụp không bị người khác đọc thành ngày khác. Timestamp chính xác nằm ở tooltip của dòng. Search lọc **phía client** theo name + description — khác Browse là gửi keyword lên catalog, ở đây `ListSkills` đã trả về cả bộ nên không cần hỏi device thêm. Mọi skill runtime đang có đều hiện, bất kể từ đâu ra — soạn tay, cài từ store, role bundle, OTA push đều chung một cây. Có nút reload; list rỗng ghi "chưa cài skill nào", khác với 501 khi runtime không list được. Footer của view detail có nút **Uninstall**, cần 2 click: click đầu kích hoạt và nói rõ sẽ xoá gì, click thứ hai mới thực thi. Thành công thì list fetch lại để skill vừa xoá không còn sót. Header của view list có dropdown **New** ngay bên trái nút đóng, lặp lại Write skill / Upload a skill của menu composer — cả hai mở *đè lên* modal này chứ không thay thế nó, và đóng cái nào thì list cũng fetch lại, nên thêm skill xong là quay về đúng list đã refresh. Escape chỉ đóng lớp trên cùng. |

Dropdown **New** cũng có **Create with Agent**. Chọn mục này sẽ đóng Manage skills, focus ô chat và điền sẵn `Let's create a skill together using your skill-creator skill. First ask me what the skill should do.`; tin nhắn **không tự gửi**, nên owner có thể xem lại hoặc sửa trước.

**Catalog skill (Browse skills)**

Catalog là public read API của `bff-web-service` (`agent-skills-public-api.md`), được bọc phía device bởi `system/server/agent/delivery/http/handler_skills.go`. Cả hai chặng đều đi qua os-server, không bao giờ từ browser — cùng lý do với `GET /api/plugin/browse` (hiện đang tạm gác, bị comment trong `system/server/server.go`): khỏi CORS và host catalog nằm phía server. Base URL mặc định `https://apiv2.autonomous.ai`, override bằng `SKILL_STORE_BASE_URL`; mọi call upstream đều kèm header `location: en-US` mà middleware của catalog bắt buộc.

| Endpoint device | Upstream | Ghi chú |
|-----------------|----------|---------|
| `GET /api/agent/skills/browse` | `GET /api/v1/agent-skills` | Forward `keyword` / `category_id` / `plan` / `page` / `limit`. `status` **cố ý không** forward — upstream không phân biệt được "chưa set" với `0`, gửi lên là lọc mất listing. Trả `{data: [Skill], total}` (`domain.StoreSkillList`). |
| `GET /api/agent/skills/bundle?id=<id>` | `GET /api/v1/agent-skills/:id/download` | Tải file `.skill` về thư mục temp, unzip tại đó, rồi trả `domain.SkillBundle` — danh sách file kèm nội dung UTF-8 inline. Thư mục temp bị xoá trước khi ghi response: đây là **preview**, không cài gì cả. |

Catalog trả lỗi nghiệp vụ dưới dạng **HTTP 200 với `status` khác 1**, nên proxy kiểm tra status trong envelope chứ không chỉ nhìn HTTP code, và đẩy message upstream ra thành `502`. Id đi bằng query param thay vì path segment để route không đụng route tĩnh anh em `/api/agent/skills/browse`.

Phần giải nén được siết: chặn zip-slip (bất kỳ entry `..` hoặc absolute nào cũng làm hỏng cả bundle), lọc `.DS_Store` / `__MACOSX/`, và giới hạn 16 MB mỗi archive, 2 MB mỗi file, 512 KB inline text (file dài hơn bị đánh dấu `truncated`), 500 file. Entry không phải UTF-8 trả về với cờ `binary`, chỉ có metadata.

UI: modal có 2 view. View **list** search phía server (debounce 300 ms trên param `keyword`, 50 item/trang), xếp kết quả thành lưới co giãn — 2 card mỗi hàng, tự rớt về 1 khi cột hẹp dưới ~250 px — mỗi card hiện name có tiền tố gạch chéo (`/algorithmic-art`, giống listing skill đã cài) kèm chip plan, ngay dưới là tên author, rồi description, rồi chip compatibility. Card **không** hiện version và không có mũi tên: version chưa mở skill ra thì cũng không nói lên gì (header của view detail vẫn giữ), còn cả card đã là vùng click. Click một card mở view **detail** — shell rộng hơn, bên trái là danh sách file trong archive, bên phải là nội dung file đang chọn, mặc định chọn `SKILL.md`, và nút **Install** ở footer. Nút back ở header (và Escape) quay lại list chứ không đóng modal.

**Yêu cầu file khi upload** (enforce phía device, nên upload sai định dạng trả `400` chứ không tạo ra một skill mà agent không bao giờ load được). Khớp format upstream — xem [algorithmic-art của anthropics/skills](https://github.com/anthropics/skills/tree/main/skills/algorithmic-art):

| Input | Yêu cầu |
|-------|---------|
| `.md` | Front-matter YAML phải có **`name`** và **`description`**. Chính `name` đó là thư mục skill được cài, nên không phải suy đoán gì từ tên file. |
| `.zip` / `.skill` | Phải chứa **`SKILL.md` ở gốc skill**. Một thư mục top-level chung sẽ là tên skill và bị strip; archive phẳng thì fallback về phần tên file qua `skills.SlugifySkillName`. |

`skills.ParseSkillFrontMatter` là bộ đọc duy nhất cho header đó. Key ngoài name/description được **chấp nhận** (ví dụ upstream có `license:`), chỉ key ở cấp cao nhất được tính (một `name:` lồng dưới `metadata:` bị bỏ qua), và listing dùng chung scanner đó qua một lớp bọc lỏng hơn — ở đó thiếu `name:` là bình thường vì thư mục đã cho biết tên. Kiểm tra `SKILL.md` chạy trên bản **staging**, nên archive bị từ chối không bao giờ chạm vào cây thật.

**Đọc, viết + cài skill (theo từng runtime, qua AgentGateway)**

Cả ba đường đều đi qua abstraction của agent — tầng device không bao giờ hardcode thư mục skill, vì mỗi agentic runtime giữ thư mục riêng:

| Endpoint device | Method gateway | Hành vi |
|-----------------|----------------|---------|
| `GET /api/agent/skills` | `ListSkills() ([]InstalledSkill, error)` | Duyệt thư mục skill của runtime: mỗi thư mục skill là một entry kèm cây file, sort theo tên, thư mục trước file. Description đọc từ front-matter của SKILL.md. `updated_at` (Unix giây) là **mtime mới nhất ở bất kỳ đâu trong cây skill đó**, không phải mtime của chính thư mục skill — mtime thư mục chỉ đổi khi thêm/xoá file, nên nó sẽ báo một SKILL.md vừa sửa là "không đổi"; giá trị này đi kèm ngay trong lượt duyệt cây chứ không duyệt lần hai, và fallback về mtime thư mục cho skill rỗng. `store_availability` là `in_store`, `device_only`, hoặc `unknown`: device đọc mọi trang catalog rồi so sánh tên runtime đã chuẩn hoá với slug/name catalog; catalog không truy cập được hoặc chưa đọc đủ luôn là `unknown`, không bao giờ là `device_only`. Thư mục skill **không tồn tại** (runtime chưa provision) trả list rỗng, không phải lỗi. |
| `GET /api/agent/skills/files?name=<skill>` | `ReadSkillFiles(name) ([]SkillBundleFile, error)` | File của một skill đã cài, dạng phẳng, nội dung UTF-8 inline. Trả **cùng envelope `domain.SkillBundle`** với preview từ store để hai view detail render bằng chung một component. Skill không còn (list cũ) trả 404. |
| `POST /api/agent/skills/publish?name=<skill>` | — | Chỉ có từ Manage skills cho entry `device_only`. Device đóng gói toàn bộ thư mục skill thành archive `.skill` rồi proxy multipart upload tới `/api/v1/skills/publish` trên host suy ra từ `llm_base_url`; `llm_api_key` luôn ở server dưới header `x-api-key`. Upload thành công đi vào trạng thái review pending trên Store. |
| `POST /api/agent/skills` | `SaveSkill(domain.SkillDraft) (path, error)` | Ghi `<name>/SKILL.md` do người dùng soạn. **Từ chối ghi đè** skill đã tồn tại (`skills.ErrSkillExists` → 400) để một lỗi soạn thảo không phá skill cài từ store/OTA. |
| `DELETE /api/agent/skills?name=<skill>` | `DeleteSkill(name) (path, error)` | Xoá thư mục skill và toàn bộ bên trong. **Không idempotent**: skill chưa cài trả `404` (`skills.ErrSkillNotFound`) chứ không phải success ngầm, để caller biết list của nó đã cũ. Cũng từ chối khi thứ nằm ở path đó không phải thư mục skill. |
| `POST /api/agent/skills/upload` | `InstallSkillArchive(...)` / `InstallSkillMarkdown(content)` | Skill từ máy người dùng, **multipart** field `file` (không dùng base64-trong-JSON như face enroll: cái đó chở một ảnh JPEG nhỏ, còn archive skill nặng vài MB và base64 sẽ phình thêm 1/3). Giới hạn 16 MB. Nhận `.zip`/`.skill` **hoặc** một file `.md` trơn, và enforce đúng yêu cầu của format upstream — xem bên dưới. |
| `GET /api/agent/file?path=<abs>` | — (không gọi gateway) | Serve file nằm trên device mà agent gọi tên, để chat hiển thị được. `path` là **do client gửi lên nên bị coi là input thù địch**: 2 cổng độc lập, sai một cái là từ chối — path phải resolve (`EvalSymlinks`, nên cả `..` lẫn symlink thoát ra đều chết) vào trong một root thuộc allowlist, và đuôi file phải nằm trong danh sách được serve. Cả hai cổng nằm ở **`system/agentfile`** chứ không nằm trong handler: đường MQTT đẩy đúng những file đó sang điện thoại (`chat.file`, xem `docs/mqtt.md`) phải khớp tuyệt đối với endpoint này, mà allow-list có hai bản là hai cơ hội nới lỏng nhầm một bên. Root gồm `media/` + `workspace/` của từng runtime cộng `/tmp` — **cố ý không** lấy thư mục config của runtime, vì JSON trong đó chứa gateway token. `.json` và `.log` **không** được serve cũng vì lý do đó. Thư mục, file không phải file thường, và file quá 32 MB trả 404; sai loại hoặc sai root trả 403. Ảnh/PDF trả `inline`, còn lại `attachment`, luôn kèm `nosniff`. |
| `POST /api/agent/skills/install` | `InstallSkillArchive(archivePath, fallbackName) (dir, error)` | Device tải file `.skill` từ catalog về temp, rồi runtime giải nén vào thư mục skill của nó. **Cố ý thay thế** skill trùng tên — cài là hành động chủ động của người dùng. |

Cả hai view detail — preview từ store và skill đã cài — đều là cùng một component, `chat/SkillFilesView.tsx`: danh sách file bên trái (thư mục cha hiện mờ phía trên basename), nội dung file đang chọn bên phải, mặc định mở `SKILL.md`, file nhị phân báo "no preview". Backend làm được vậy vì trả cùng một shape cho cả hai nguồn; `skills.BuildFilePreview` là nơi duy nhất quyết định text-hay-binary và cắt bớt, bất kể byte đến từ entry zip hay từ đĩa.

Phần dùng chung nằm ở `system/skills`: `list.go` duyệt thư mục skill, `read.go` đọc file của một skill, `authored.go` render + ghi SKILL.md, `install.go` giải nén archive. Chỉ **thư mục đích** là khác nhau giữa các backend, đúng lý do khiến skill watcher của từng runtime gần như là bản sao của nhau — nên `save_skill.go` của mỗi backend chỉ là 3 hàm một dòng trỏ vào path của nó:

| Runtime | Thư mục skill |
|---------|---------------|
| openclaw | `{OpenclawConfigDir}/workspace/skills` — dùng chung với `InstallRoleSkills` / `EnsureMCPSkill` |
| picoclaw | `{picoclawWorkspaceDir}/skills` |
| codex | `codexSkillsDir` (`~/.codex/skills`) |
| claudecode | `claudecodeSkillsDir` (`~/.claude/skills`) |
| opencode | `opencodeSkillsDir` (`$XDG_CONFIG_HOME/opencode/skills`) |
| hermes | **ghi** → `~/.hermes/skills/authored`; **list** → thư mục đó cộng `~/.hermes/skills/openclaw-imports` |

Hermes là backend duy nhất namespace thư mục skill, nên cũng là cái duy nhất cần nhiều hơn một path. Device cố ý ghi **ra ngoài `openclaw-imports`**: `presync.sh` §0 khôi phục skill nền tảng đã import bằng cách chạy `claw migrate` *chỉ khi thư mục đó rỗng*, nên một skill soạn tay nằm trong đó sẽ khiến guard mãi mãi thấy thư mục có nội dung và factory reset sẽ âm thầm không bao giờ khôi phục lại. `ListSkills` merge cả hai root qua `skills.ListInstalledFrom`, root của device đứng trước để skill người dùng không bị skill import trùng tên che mất. Hermes tìm skill ở bất kỳ đâu dưới `~/.hermes/skills` nên root mới không cần đổi config.

Listing bỏ qua `<name>.new` / `<name>.old` (thư mục staging + backup của InstallSkillArchive) và thư mục bắt đầu bằng dấu chấm: đó là chi tiết cài đặt, không phải skill. Cây file giới hạn độ sâu 6 và 200 entry mỗi thư mục để một cây bất thường không tạo response vô hạn, và một skill đọc lỗi chỉ trả cây rỗng thay vì làm trắng cả list.

`ErrNotSupportedByRuntime` → **HTTP 501** kèm tên runtime đang chạy vẫn là contract cho backend không làm được một trong ba việc này, UI hiển thị inline — nhưng tính đến giờ mọi runtime đang ship đều implement đủ cả ba, nên 501 chỉ còn khả dĩ với backend mới trong tương lai.

Xử lý archive trong `skills.InstallSkillArchive`: nếu archive có đúng một thư mục top-level chung thì đó là tên skill và bị strip (bundle `.skill` của catalog có dạng `<name>/SKILL.md`); nếu file nằm ngay gốc archive thì dùng fallback name của caller (zip kiểu OTA). Giải nén được stage ở `<skill>.new` và chỉ swap vào khi thành công trọn vẹn, bản cũ dời sang `<skill>.old` và được khôi phục nếu swap lỗi — một bản tải hỏng không bao giờ để lại skill cài dở hay phá skill đang chạy. Chặn zip-slip, lọc `.DS_Store` / `__MACOSX/`, giới hạn 500 file và 4 MB mỗi entry (ép bằng `LimitReader` nên `UncompressedSize64` khai gian cũng không làm đầy đĩa).

Cả hai đường đều KHÔNG restart runtime: backend nào có thư mục skill thì nhặt file mới theo từng session, đúng contract mà `InstallRoleSkills` dựa vào.

**Streaming real-time**
- **Thinking indicator**: khối tím thu gọn được, hiển thị reasoning tokens của LLM khi stream (`thinking` events). Click mở rộng toàn bộ (max-height 200px, scroll). Tự ẩn khi response hoàn tất.
- **Assistant delta streaming**: text response hiện từng token qua `assistant_delta` events, thay vì đợi response cuối cùng. Fallback sang `chat_response` partial cho đường non-agent.
- **Dòng Harness trong chat**: khi turn đã giao cho Harness, chữ trả lời bị hoãn của agent trên đèn (thường là `NO_REPLY`) không còn được stream. Delta tiến độ, tool, câu hỏi và thông báo xin quyền của Harness mỗi cái kết thúc bằng xuống dòng (`pushHarnessLine`, `system/server/agent/delivery/http/handler_harness.go`); dòng tiến độ/tool trùng với dòng ngay trước của run thì bỏ. Dòng tiến độ/tool chỉ có realtime (không ghi JSONL), tab bị ẩn sẽ lỡ mất; câu hỏi và thông báo xin quyền được phát lại từ flow event `harness_question`.
- **Tool call chips**: badge màu teal hiển thị các tool agent gọi trong response (emotion, LED, servo, audio, v.v.). Hiển thị phía trên bubble tin nhắn khi đang stream, lưu lại trên tin nhắn đã hoàn tất. Một tool hiển thị thành một chip; **từ hai tool trở lên sẽ gom thành một pill tóm tắt** ("N steps" với các icon tool xếp chồng + marker đang chạy/`DONE`), bấm vào để mở ra các chip riêng lẻ.

**Xử lý response**
- Theo dõi response qua `runId` correlation trên SSE events
- HW control markers inline (`[HW:/emotion:...]`) được lọc bỏ khỏi text hiển thị; dạng markdown-link một số LLM emit (`[label](HW:/led/off:{})`) cũng được lọc, giữ lại label. Cả hai pattern lọc mirror đúng grammar của executor os-server — biến thể malformed mà executor không fire sẽ hiển thị nguyên văn
- Timeout 50 phút tính theo **idle**: mỗi SSE event thuộc run đang chờ (`assistant_delta`, `thinking`, tool call) đều đẩy lùi hạn, nên một turn chạy nhiều phút vẫn ở trạng thái pending chừng nào agent còn làm việc; chỉ 50 phút im lặng thật sự mới bỏ cuộc. Đặt dài hơn mức chặn turn của backend (`CODEX_TURN_TIMEOUT_S`, 45 phút) chứ không phải đoán xem câu trả lời nên mất bao lâu: gatewayd luôn kết thúc turn và frame kết thúc đó mang đúng runId đang chờ, nên đây chỉ là lưới an toàn cuối. Không rút ngắn được — `codex exec --json` không emit gì trong lúc chạy, nên mọi cửa sổ ngắn hơn chính turn đó sẽ chốt nhầm một turn khoẻ mạnh thành "no response" (xem `docs/vi/agentic/codex_vi.md` §2.1). Khi bỏ cuộc: giữ phần text đã stream làm câu trả lời, không có thì báo lỗi kèm nút retry. Cố ý KHÔNG phải hạn tuyệt đối — hạn tuyệt đối từng chốt một turn build dài thành "no response" trong khi run vẫn đang chạy; MQTT chat và Telegram không dính vì chúng không có deadline phía client.
- **Khôi phục turn đang chờ và fallback live**: message lưu kèm epoch `ts`; bubble reply đang pending dưới 10 phút sẽ sống sót qua reload thay vì bị chốt thành lỗi. Ở lần render đầu khi tab Chat active, UI re-attach vào `runId` đã lưu và backfill câu trả lời từ flow JSONL replay (`/api/agent/flow-stream` gửi lại 500 event cuối trong ngày mỗi lần connect — `tts_send` / `tts_suppressed` / `no_reply` / `harness_response`). Polling khôi phục bắt đầu khi bubble pending được gắn `runId` đã nhận, kể cả khi POST hoàn tất sau lúc `sending` đã được bật. UI theo dõi reply pending của mọi conversation, nên chuyển hoặc tạo chat không làm mất reply cũ. Cửa sổ flow gần nhất được fetch lại ba giây sau khi request trước hoàn tất, không chồng request, để khôi phục khi SSE live bị mất. Kết quả chỉ cập nhật bubble pending khớp run; bubble đã hoàn tất hoặc bị người dùng dừng không được mở lại. Recovery dùng cùng ngân sách idle 12 phút với turn đang chạy: reload khi Harness hoặc Codex vẫn làm việc không được chốt lỗi trước khi terminal event được ghi. Event live làm mới deadline này.
- Local intent fast path: response dưới 50ms bypass agent
- Busy/dropped: hiển thị "busy — try again"
- Markdown: bold, italic, inline code (tô màu amber), code block (monospace), link `[label](url)`, URL trần (scheme http/https bị gõ lỗi như `hthtps://` từ banner giới hạn quota upstream được sửa lại trước khi linkify; scheme lạ giữ nguyên plain text), danh sách, và bảng (header có nền + hàng zebra). Bubble agent render đủ markdown; bubble user giữ nguyên văn bản, riêng URL được linkify với cùng cơ chế sửa scheme

**Empty State & Gợi ý**
- Khi cuộc trò chuyện chưa có tin nhắn, khu vực chat hiển thị một quả cầu assistant lớn đang "thở", tiêu đề/phụ đề đã bản địa hóa, và bốn **chip gợi ý** bấm được. Bấm một chip sẽ điền sẵn vào ô nhập (không tự gửi) để người dùng chỉnh trước.

**Bản địa hóa (i18n)**
- Các chuỗi UI riêng của chat (tiêu đề/phụ đề empty-state, chip gợi ý, trạng thái "đang suy nghĩ"/"trực tuyến" trên thanh trên cùng) được bản địa hóa qua `src/lib/i18n.ts` — một module nhẹ tự viết, mô phỏng convention của backend Go `system/lib/i18n` (mã chuẩn `en` / `vi` / `zh-CN` / `zh-TW`, chuẩn hóa alias, **fallback về tiếng Anh** theo từng key).
- Ngôn ngữ active được lấy từ trường `stt_language` trong device config (cùng nguồn mà `i18n.Lang()` của Go đọc từ `config.STTLanguage`) qua `setLanguage()` trong `App.tsx` khi load config lần đầu, và Chat section áp lại từ lần fetch config của chính nó. Các component đọc chuỗi qua hook `useT()`, hook này re-render khi ngôn ngữ được xác định.
- Module i18n này hiện chỉ phủ các chuỗi chat thêm vào trong đợt redesign; phần còn lại của UI Monitor vẫn hardcode tiếng Anh.

**Luồng dữ liệu**
```
Chat UI → POST /api/sensing/event → SensingHandler
  → openclaw.SendChatMessage() → WebSocket chat.send → OpenClaw
  → Response stream qua WebSocket (thinking → assistant deltas → lifecycle end)
  → SSE /api/agent/flow-stream → Chat UI cập nhật tin nhắn real-time
```

---

### 5.8 Device → Sensing

Trang mở đầu bằng tóm tắt presence và các card dễ đọc về người, chuyển động, ánh sáng và sự kiện âm thanh. Ước lượng biểu cảm và tư thế xuất hiện tiếp theo khi có dữ liệu. Trạng thái backend, buffer, timeout, sự kiện thô và sample tư thế nằm trong **Technical details** thu gọn. Dịch vụ tạm dừng được ghi rõ; request lỗi hiển thị lỗi thay vì quan sát cũ. Card dùng một cột khi chiều rộng tối đa 600px. Environment phía dưới ưu tiên nhiệt độ, độ ẩm, CO₂ và PM2.5; **More measurements** mở năm chỉ số còn lại. Không thêm ngưỡng chất lượng không khí hay điểm sức khỏe suy ra.

Menu Sensing và card chỉ đọc **Environment** luôn hiển thị, không cần debug,
kể cả khi device không khai báo capability sensing. Card camera vẫn yêu cầu
`vision`. Khi đang tải capabilities hoặc thiếu `environment`, card môi trường
hiện thông báo không khả dụng gọn và không gửi request tới sensor. Sensor đang tắt
hoặc chưa có sample cũng dùng trạng thái trống gọn.

Khi có capability `environment`, card đọc `GET /api/hardware/environment/status`
mỗi 3 giây qua reverse proxy hardware của OS đã có xác thực, chuyển tới HAL
`GET /environment/status`. Chu kỳ làm mới trình duyệt độc lập với
`poll_interval_s` cấu hình trong HAL, không thay đổi nhịp thu nhận dữ liệu.
Đọc từ browser không kích hoạt lượt agent. Worker phát hiện thay đổi độc lập
và API status cho agent local được mô tả trong
[tài liệu môi trường Lamp](../../robots/lamp/docs/vi/environment-sensing_vi.md#chính-sách-thay-đổi-của-os-và-api-cho-agent).

Card hiển thị trạng thái cảm biến, thời điểm sample, trạng thái dữ liệu
cũ, lỗi và đủ chín chỉ số chuẩn: nhiệt độ (°C), độ ẩm (%),
PM1 / PM2.5 / PM4 / PM10 (µg/m³), VOC index, NOx index và CO₂ (ppm).
UI dựa vào key chỉ số và metadata nguồn, không phụ thuộc tên model cảm biến,
nên đổi component cấu hình (ví dụ sang SEN63C) vẫn dùng cùng card. Chỉ số không
được hỗ trợ hoặc chưa sẵn sàng vẫn hiện `N/A` tương ứng giá trị `null`.
Nhãn nguồn chỉ rõ component; từng chỉ số có timestamp riêng. Component lỗi
không che số đo còn tốt từ component khác. Giá trị chưa khả dụng hiện `N/A`, không hiện số 0.
Số đo cũ cũng được thay bằng `N/A`; request thất bại được hiển thị là lỗi để
không nhầm số đo trước đó với dữ liệu hiện tại. Không gán nhãn chất lượng không khí
tốt/xấu, ngưỡng hay cảnh báo. Sensor details dùng từng khối có chữ tương phản rõ, hàng nhãn/giá trị thẳng cột và badge trạng thái. Sensor hoạt động xuất hiện trước; sensor tắt không có lỗi nằm trong danh sách con thu gọn. Mục kỹ thuật thu gọn hiển thị trạng thái, bus I2C,
thanh ghi trạng thái và timing đọc/thử lại/đánh dấu cũ/phục hồi của từng
component trong `status.components`. Vẫn hỗ trợ snapshot một sensor kiểu cũ
với `status.timing` cấp cao nhất và nhãn cảm biến chung. Sample hoặc timestamp
sample là `null` sẽ hiện trạng thái chờ, không hiển thị ngày epoch. Giải thích về
gas index chỉ xuất hiện khi VOC hoặc NOx có nguồn được khai báo hoặc giá trị đo.

Chỉ hardware profile `pro`, `pro-respeaker-lite` và `pro-xvf3800` của Lamp khai báo `environment`
tùy chọn (`required: false`) và bật SEN63C trên `orangepi_sun60`, bus `0`.
Standard hiện `N/A` mà không polling vì không có capability này. SEN55/SCD41
và board thiếu entry tương ứng vẫn tắt, ngay cả trên Pro. Trên Pro, thiếu
SEN63C thì hiện lỗi và `N/A` trong khi HAL thử lại, không chặn khởi động.
Tắt SEN63C trước khi bật SEN55 + SCD41 thay thế. Xem
[tài liệu cảm biến môi trường của Lamp](../../robots/lamp/docs/vi/environment-sensing_vi.md)
về đấu dây, bật cảm biến và contract dữ liệu HAL.

## 6. LED Color API

### Vấn đề
`GET /hw/led` gốc chỉ trả `{ led_count: 64 }` — không có thông tin màu hiện tại.

### Giải pháp
Thêm `GET /hw/led/color` vào `hal/server.py`:

```python
@app.get("/led/color", response_model=LEDColorResponse, tags=["LED"])
def get_led_color():
    """Get the current LED color (last color set on the strip)."""
```

**Ưu tiên lấy màu:**
1. `sensing_service.presence._last_color` — màu base được track khi AI set
2. Fallback: `rgb_service.strip.getPixelColor(0)` — đọc trực tiếp từ hardware

**Tracking đã được bổ sung cho:**
- `POST /led/solid` ✅ (đã có từ trước)
- `POST /scene` ✅ (đã có từ trước)
- `POST /emotion` ✅ (bổ sung thêm — đây là path AI dùng nhiều nhất)

> **Lưu ý**: `GET /hw/led/color` là **read-only**, monitor chỉ đọc, không set màu.

---

## 7. Reusable Components (`system/web/src/pages/monitor/components.tsx`)

| Component | Mô tả |
|-----------|-------|
| `GaugeRing` | SVG ring chart với drop-shadow glow, transition 0.7s |
| `Sparkline` | SVG area + line chart, nhận mảng số |
| `HWBadge` | Badge xanh/đỏ cho hardware status |
| `StatusDot` | Chấm tròn xanh/đỏ với glow |
| `SignalBars` | 4 bar WiFi signal (ngưỡng: -50/-65/-75/-85 dBm) |
| `StatPill` | Row label + value trong card |

---

## 8. Global Source Footer (Tuân thủ GPL v3 §6)

`system/web/src/components/SourceFooter.tsx` là một link nhỏ `position: fixed`, mount tại App root (`App.tsx`, ngoài `<Routes>`), nên xuất hiện ở mọi trang — Setup, Login, Monitor, GwConfig.

Render tại `bottom: 6px, right: 8px` với chữ monospace 10px và opacity `0.7` — ai cần là thấy nhưng không đè form action buttons (Back / Next / Setup / Save) hoặc scroll. Link target: `https://github.com/autonomous-ai/autonomous-os`.

Lý do tồn tại: HAL Python (`hal/`) ship dưới GPL v3, bake sẵn vào image board. GPL §6 yêu cầu người nhận binary phải biết source code tương ứng ở đâu. Footer thỏa mãn lựa chọn "written offer" bằng cách expose URL repo public ngay trên thiết bị. Xem thêm `scripts/release/tag-release.sh` + `Makefile:tag-release` cho phần map version → commit.

---

## 9. Build & Deploy

```bash
# Build production
make web-build        # tsc + vite build → system/web/dist/

# Deploy lên MỘT thiết bị theo IP (dev push — KHÔNG phải đường OTA cho cả fleet)
IP=172.168.20.255 make device-deploy   # hal + os-server
IP=172.168.20.255 make hal-deploy      # chỉ hal, không cần build
IP=172.168.20.255 make os-deploy       # cross-compile + thay binary

# Vẫn các target đó nhưng đi qua SSH jump host, cho thiết bị không route tới được
IP=10.0.0.5 J=proxy-host make hal-deploy
IP=10.0.0.5 J=proxy-host make hal-log
```

Chạy bằng `scripts/deploy-device.sh`. `PI_USER` mặc định `orangepi` và `PI_PASS`
mặc định `orangepi` (cần `sshpass`); đặt `PI_PASS=""` để dùng SSH key của bạn và
sudo tương tác. Có thể dùng `PI_HOST` thay cho `IP`.

`J=<host>` (hoặc `PI_JUMP`, hoặc `--jump <host>` khi gọi thẳng script) đặt một
`ProxyJump` trước mọi hop — `ssh`, `scp` và `rsync` đều nhận — nên
`hal-deploy`, `os-deploy`, `device-deploy`, `hal-log` và `os-log` tới được
thiết bị không route trực tiếp từ máy bạn. Hop đó xác thực bằng SSH key/agent
và `~/.ssh/config` của bạn; `PI_PASS` chỉ là mật khẩu **thiết bị**, nên bastion
đòi mật khẩu riêng sẽ không chạy tự động được. `make push-skill` dùng cùng knob
`J=`. Cả hai thứ tự đều được: `J=... make hal-deploy` và
`make hal-deploy J=...` — riêng `PI_PASS` phải là biến môi trường
(`PI_PASS="" IP=... make hal-deploy`).

`.env`, `.venv` và `calibration/` trên thiết bị không bao giờ bị ghi đè, và bước
swap chạy không có `--delete`, nên các đường dẫn riêng của thiết bị (ngoài repo)
vẫn còn nguyên.

> **Chạy `--dry-run` trước khi nhánh của bạn có thể cũ hơn thiết bị.** Bước swap
> ghi đè, nên một checkout cũ có thể âm thầm hoàn tác phần việc chỉ tồn tại trên
> thiết bị:
> `IP=<DEVICE_IP> bash scripts/deploy-device.sh --hal --dry-run`

Các target trên dành cho một thiết bị trong LAN. Để phát hành cho cả fleet, dùng
đường OTA — `make upload-hal` rồi `make promote-hal`, vốn đánh version cho
artifact và roll out.

Kết quả cuối Harness được ghi vào flow JSONL bằng `harness_response`, giữ run ID thiết bị gốc và `text` đầy đủ. Web Chat dùng sự kiện này khôi phục kết quả đang chờ sau khi SSE ngắt hoặc tải lại trang. Luồng trực tiếp vẫn phát `chat_response` với state `final`.

### Cấu trúc component Sensing

`monitor/SensingSection.tsx` chỉ ghép các phần theo capability. Component nằm
trong `monitor/sensing/`: mỗi card một file, dùng chung `CardHeader`, types và
hàm định dạng. `useVisionSensing` poll một lần cho toàn bộ card vision;
`useEnvironment` poll snapshot môi trường chung độc lập. Client `visionApi.ts` và
`environmentApi.ts` quản lý request OS-server, kiểm tra response và lỗi;
card không trực tiếp fetch. Cả hai dùng reverse proxy có xác thực
`/api/hardware/*` sẵn có. Lỗi HTTP/response của vision hiển thị thông báo lỗi,
không giữ màn hình loading hoặc số liệu cũ.

Cảnh báo delivery Harness không còn chặn câu nói mới hoặc khóa trả lời câu hỏi. Check delivery kiểm receipt pending hiện tại; Dismiss without retrying bỏ cảnh báo mà không hủy hoặc gửi lại task.

### Địa chỉ người vận hành iMessage

Handle trong settings iMessage xác định hội thoại home của người vận hành. Đây không phải cam kết giới hạn người gửi: presync Hermes mặc định nhận tin khách hàng từ địa chỉ khác (`BLUEBUBBLES_ALLOW_ALL_USERS=true` nếu không override rõ ràng).

### Lưu Wi-Fi trong Settings khi chuyển mạng

Tại `/setting#wifi` (kể cả với `?debug=true`), việc đổi Wi-Fi có thể làm trình duyệt mất kết nối. Phản hồi lưu thành công chỉ xác nhận cấu hình đã được lưu, chưa xác nhận thiết bị đã kết nối mạng. Settings hiển thị tên SSID cần nối lại và kiểm tra mạng thực tế qua `GET /api/network/current` mỗi ba giây, tối đa hai phút (mỗi request giới hạn bốn giây). Khi rời panel, kiểm tra bị hủy; không tự gửi lại yêu cầu lưu cấu hình.

Lỗi truyền tải fetch khi sửa Wi-Fi hoặc lưu từ tab Wi-Fi được hiển thị là **chưa xác định kết quả lưu**, không khẳng định thành công hay thất bại. Các trường đang sửa được giữ trong bộ nhớ; dù thiết bị đã báo đúng SSID, UI vẫn yêu cầu tải lại để xác nhận toàn bộ cấu hình. Lỗi HTTP/API vẫn hiển thị lỗi từ server. Nếu hết thời gian mà chưa truy cập được mạng/địa chỉ mới, thông báo hướng dẫn người dùng nối mạng đích rồi mở lại trang thiết bị. Không lưu thông tin đăng nhập vào browser storage. Trang provisioning độc lập `/wifi` không thay đổi.
