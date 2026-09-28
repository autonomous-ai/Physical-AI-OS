# Tổng Quan Kiến Trúc — Autonomous

## Kiến Trúc 3 Tầng

```
Agentic Runtime (AI/LLM) → OS Server (Go, :5000) → HAL (Python, :5001) → Phần cứng
```

| Tầng | Ngôn ngữ | Port | Vai trò |
|------|----------|------|---------|
| Agentic Runtime | Go | WS | Bộ não AI, LLM, SKILL.md, memory, channels |
| OS Server | Go | 5000 | Hệ thống (mạng, OTA, MQTT, reset), sensing event routing, local intent |
| HAL | Python | 5001 | Hardware drivers (servo, LED, camera, audio, display), FastAPI |

## Tích hợp máy tính và giọng nói

- **[Harness](https://github.com/autonomous-ai/openharness):** `harness-use` giao việc lập trình và nghiên cứu cho agent trên máy tính đã ghép đôi qua kết nối LAN trực tiếp có xác thực. Câu hỏi và kết quả được trả về qua giọng nói hoặc chat. Chế độ Harness-only voice chuyển bản thu tap-to-record thủ công tới agent đang được chọn trong Harness. Xem [Harness](harness_vi.md).
- **Thao tác Mac:** `computer-use` cho phép agent trên thiết bị quan sát, thao tác và kiểm tra kết quả qua Autonomous Buddy, sử dụng Cua Driver tích hợp cùng hỗ trợ ảnh chụp màn hình. Buddy thực thi thao tác desktop; agent trên thiết bị chịu trách nhiệm suy luận và hoàn thành công việc. Harness và Buddy có ghép đôi và kết nối riêng. Xem [Computer use](../../integrations/companions/autonomous-buddy/docs/vi/computer-use_vi.md).
- **Jev:** plugin Hermes do OS quản lý chọn và nạp trước một skill đã cài trước lần gọi model đầu tiên; nếu không thành công thì dùng cơ chế tìm skill thông thường. Plugin này được bật trong bản build hiện tại; nhánh dự phòng cho voice intent tách biệt mặc định bật, còn endpoint gợi ý thao tác Buddy được bật nhưng vẫn thử nghiệm. Việc chọn không thực thi thao tác hay cấp quyền. Xem [Nạp trước skill Hermes](agentic/hermes_vi.md), [Intent fallback](os-server_vi.md), và [Gợi ý Buddy](os-server_vi.md).
- **Realtime voice:** HAL hỗ trợ Gemini Live (model mặc định `gemini-3.8-live`), OpenAI Realtime, GPT-Live và Pipecat v1. Pipecat điều phối STT → LLM tương thích OpenAI (mặc định `qwen/qwen3.6-35b-a3b`) → HAL TTS trên thiết bị; các lời gọi model vẫn dùng dịch vụ từ xa. Giọng nói thông thường có thể trả lời trực tiếp hoặc giao việc cho runtime chính.
- **Smart Turn:** suy luận ONNX cục bộ bổ sung cho phát hiện khoảng lặng ở đường thu âm rảnh tay non-Live dùng chung; Pipecat Live có pipeline Silero VAD + Smart Turn riêng. Cơ chế dự phòng theo khoảng lặng giới hạn thời gian chờ khi suy luận không khả dụng. Bản thu Harness thủ công kết thúc khi người dùng chạm. Extra `pipecat` được cài trong luồng thiết lập Lamp/Pi/OrangePi và không dùng trên Reachy do xung đột dependency ONNX. Xem [Realtime voice](realtime-voice_vi.md).

## Thư Mục Dự Án

```
system/
├── cmd/os-server/main.go              — Entry point OS Server
├── cmd/bootstrap/main.go         — OTA bootstrap worker
├── server/
│   ├── server.go                 — Gin HTTP server, route setup
│   ├── config/                   — JSON config management
│   ├── health/delivery/http/     — Health, system info, dashboard
│   ├── network/delivery/http/    — WiFi scan, connect
│   ├── device/delivery/          — Setup (HTTP + MQTT handlers)
│   ├── sensing/delivery/http/    — Sensing event → intent match / agent gateway
│   └── openclaw/delivery/sse/    — OpenClaw status, SSE events
├── agent/  ambient/  beclient/  buddy/  device/  healthwatch/
├── intent/  monitor/  network/  skills/  statusled/  vision/
│                                 — System managers, one folder per diagram chip
├── lib/mqtt/                     — MQTT client (Eclipse Paho autopaho)
├── domain/                       — Shared structs
├── bootstrap/                    — OTA worker
└── web/                          — React 19 + Vite + Tailwind CSS 4 SPA

runtimes/                   — Swappable brains: openclaw/ hermes/ picoclaw/ codex/ claudecode/ opencode/

hal/
├── server.py                     — FastAPI server
├── config.py                     — Hằng số runtime (ngưỡng sensing, timeout, URL)
├── board/                        — Profile thiết bị, pin map board và overlay
├── drivers/                      — Service phần cứng (camera, motor, RGB, sensing, voice, display)
├── routes/                       — Module FastAPI route theo capability
├── safety/                       — Safety policy đã parse và gate tất định
├── realtime/                     — Realtime voice agent và context manager
├── server_support/               — HTTP/security support dùng chung
└── pyproject.toml                — Python dependencies (opencv-python, onnxruntime)

robots/                          — Per-device configs and overlays
  contract/                       — Shared API contracts (+ cts/ compliance suite)
skills/                           — Built-in SKILL.md cho agent runtime, gồm cả
                                    skill-creator để chủ thiết bị tự tạo skill
integrations/                     — Off-device: companions/, chat-bridges/, perception-service/
```

Wiring nút GPIO cơ học thuộc về từng device trong
`robots/lamp/gpio_button.json` và `robots/intern-v2/gpio_button.json`.
HAL xác định thư mục device qua `DEVICES_DIR` và `DEVICE_TYPE`.
Entry của board hỗ trợ cấu hình phẳng cũ `chip`, `line`, `debounce_ns` hoặc
list `buttons` có tên cho từng input. `load_button_configs` cấp cấu hình cho
mỗi instance của driver dùng chung `hal/drivers/gpio_button.py`; HAL dừng tất
cả instance khi cleanup. `load_button_config` vẫn dùng được cho caller chỉ
cần nút đầu tiên (nút chính trong cấu hình Lamp). Lamp trên OrangePi có `primary` ở pin 37 / PD4 / gpiochip0
line 100 (`behavior: "standard"`) và `factory_reset` ở pin 35 / PD3 /
gpiochip0 line 99 (`behavior: "factory_reset"`, `hold_s: 5`). Nút reset bỏ
qua tap và giữ dưới 5 s; giữ đủ 5 s bật LED đỏ đứng dùng chung để báo đã arm,
rồi nhả mới gọi factory-reset dùng chung. Không reset khi còn giữ và không
gọi sleep, shutdown hay action single/triple-click.
Cấu hình device được ưu tiên; thiếu file hoặc entry board thì fallback về
đúng một nút mặc định `button` cũ trong `hal/board/boards.json`.
JSON của Intern v2 giữ nguyên. Khi đổi wiring, cần sửa JSON của device được
chọn rồi restart HAL; hệ thống không tự phát hiện đổi dây cắm. Config sai,
tên input trùng hoặc cặp chip/line trùng bị từ chối trước khi claim GPIO.
Chế độ mô phỏng bỏ qua nút phần cứng.

Công tắc privacy luôn điều khiển mute microphone. Hai trường boolean tùy chọn
`disable_camera_on_mute` và `mute_speaker_on_mute` khóa thêm camera và speaker;
cả hai mặc định `false`. Lamp bật cả hai trong JSON, còn Intern giữ fallback
chỉ mute mic và không kèm JSON privacy. HAL giữ các thiết bị đã chọn ở trạng thái
khóa cho tới lần đọc GPIO đầu tiên; lỗi đọc/claim thì tiếp tục khóa. Wrapper
capture camera, camera routes, auto-wake và các đường phát âm thanh dùng chung
`hal/privacy.py`. Mở khóa khôi phục trạng thái camera/speaker trước đó; khóa
tạm thời không ghi đè tùy chọn đã lưu trong sidecar theo boot.
Loader chỉ đọc file cũ `mic_button.json` khi chưa có `privacy_button.json`,
để có thể cập nhật HAL trước khi đổi tên JSON của device.
Công tắc gạt microphone dùng `privacy_button.json` do device quản lý, hiện chỉ
kèm file `robots/lamp/privacy_button.json` cho pin vật lý 11 / PL9 / gpiochip1 line 9. Entry của board được phát hiện
trong `boards` cung cấp `chip`, `line`, `settle_s`, `muted_level` và
`watchdog_s` cho `hal/board/privacy_button.py`, rồi truyền vào driver dùng chung
`hal/drivers/privacy_button.py`. Intern v2 không kèm JSON mic, tiếp tục dùng
fallback trong code: gpiochip0 line 97 (PD1), chờ ổn định 0,06 s, LOW (`0`)
là mute và watchdog 30 s. Lamp dùng cùng timing/polarity. Pull-up vẫn bật; `muted_level` nhận `0` hoặc `1`.
Thiếu file hoặc entry board thì giữ default cũ chỉ cho `intern-v2`, bao gồm
điều kiện device-type hiện có bất kể board ID. Device khác không có cấu hình
thì bỏ qua công tắc; `enabled: false` tắt rõ ràng. Cấu hình sai bị từ chối
trước khi claim GPIO. HAL đồng bộ mute theo vị trí công tắc lúc khởi động
và sau khi cạnh tín hiệu ổn định. Watchdog chỉ đồng bộ lại khi chân đổi mức,
giữ các thay đổi mute bằng phần mềm khi công tắc đứng yên. Restart HAL để
áp dụng JSON mới; simulation bỏ qua input phần cứng này. Cần giữ JSON nút GPIO
của Lamp vì fallback nút chính cũ cũng dùng pin 11, còn JSON device chuyển nút
chính sang pin 37. Đã xác minh startup với JSON mic Lamp ngày 2026-09-11: chip1/line9 ready,
vị trí LOW ban đầu áp dụng mute. Chờ live test thao tác gạt.

Wiring TTP223 do device quản lý trong `robots/lamp/ttp223.json`. Intern v2
không có phần cứng TTP223 nên không kèm file này. `hal/board/ttp223.py` chọn `chip`, `lines` và
`axis` tùy chọn của board đã detect từ `boards`, truyền `TouchConfig` cho driver
dùng chung. Thiếu file/board thì fallback về `touch` cũ trong
`hal/board/boards.json`; `enabled: false` tắt TTP223 rõ ràng. Config sai bị từ
chối trước khi claim GPIO. Restart HAL sau khi sửa config của device được
chọn; mô phỏng bỏ qua input phần cứng. Lamp dùng hai pad TTP223: S1 ở pin vật lý
29 / PD0 / gpiochip0 line 96, S3 ở pin 33 / PD2 / line 98. Nút cơ dùng pin 37 /
PD4 / line 100. Cần giữ JSON của Lamp trên device vì fallback touch cũ vẫn trùng
chân nút cơ. Intern v2 giữ wiring nút hiện có. Fallback board cũ vẫn giữ nguyên;
chỉ thiếu file JSON không có nghĩa là driver bị tắt.

Wiring cảm ứng MPR121 tùy chọn hiện chỉ thuộc Lamp, trong
`robots/lamp/mpr121.json`; Intern v2 không có khai báo MPR121. Cấu hình dùng
cùng cách chọn thư mục device và được `hal/board/mpr121.py` kiểm tra. Lamp
có sẵn entry bật cho `orangepi_sun60`, dùng bus 0 và địa chỉ 0x5A, đã kiểm tra
khởi tạo và polling trên Lamp `lamp-0c4e`. Script phần cứng dùng chân 3/5
(TWI0); cần xác minh wiring thực tế và bật
đúng controller I²C bên ngoài HAL. HAL không sửa boot overlay. Thiếu
`/dev/i2c-0` thì log lỗi khởi tạo và giữ các input hiện có hoạt động.
Entry trong `boards` bắt buộc có `bus` I²C cụ thể; không quét hay đoán bus MPR121 để fallback. Thiếu
file/board hoặc `enabled: false` thì bỏ qua MPR121, giữ input GPIO/TTP223
hiện có; chế độ mô phỏng cũng bỏ qua. Cấu hình bật nhưng sai bị từ chối khi
startup. Restart HAL sau khi đổi cấu hình wiring. Driver dùng chung
`hal/drivers/mpr121.py` gom các electrode được chọn thành một phiên chạm rồi
nhả đã debounce. Ngưỡng cử chỉ dùng chung GPIO trong
`hal/drivers/button_gestures.py`: lần nhả ngắn đầu tiên được phân giải gọi single-click
với `announce=False`; sau 0.4 s yên, 1/2/4+ click phát cue nghe. Đúng 3 click
không có action bổ sung hoặc cue: reboot đã bị vô hiệu hóa tại wrapper MPR121. Giữ chỉ thực hiện khi nhả: 2–<5 s sleepy, 5–<10 s shutdown,
≥10 s factory reset. Khi giữ, event mức giữ đã debounce dùng cùng `HoldLEDFeedback`
trong `hal/drivers/button_actions.py` và `BUTTON_LED_PRESETS` với GPIO: tím nháy 2 Hz ở 2–<5 s, đỏ nháy 2 Hz ở
5–<10 s và đỏ đứng từ 10 s. Nhả thì dừng nháy; action shutdown/reset được
chấp nhận đặt lại đỏ đứng trước khi chạy. Chạm giữ lúc startup không hiện
phản hồi; stop hoặc lỗi phần cứng hủy phản hồi. LED được kiểm tra bằng test
mock local, chưa kiểm tra trên device thật. Mặc định:
địa chỉ `0x5A`, electrode 0–11, ngưỡng chạm/nhả 2/1, bật autoconfig,
polling 10 ms và debounce 30 ms, chờ ổn định 100 ms lúc startup. Lỗi I²C hoặc
cờ quá dòng chỉ dừng driver này. Logger `hal.drivers.mpr121` ghi cấu hình,
thay đổi electrode, tap đã debounce, thực thi action và vòng đời driver trong
log/journal HAL thường dùng; poll không đổi trạng thái không tạo log INFO.
Lamp bật nhận diện vuốt với `swipe_axis: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]`.
Trục electrode này theo chuỗi di chuyển quan sát được trong log device; đảo
chiều trục không đổi action vì cả hai hướng gọi `swipe_action` dùng chung để sleep.
Hỗ trợ vuốt một phần dải. Chạm đứng yên giữ action click/hold; contact di chuyển
loại bỏ các action đó để vuốt không biến thành reset. Nhận diện swipe thêm tối đa
120 ms chờ nhả để nối chuyển tiếp giữa electrode. Thiếu/null `swipe_axis` giữ
hành vi chỉ click/hold cũ. Cài HAL mới trước JSON device mới vì loader cũ kiểm tra
nghiêm ngặt và từ chối trường chưa biết. Test swipe phát lại chuyển trạng thái
electrode đã ghi; runtime và cấu hình swipe mới đã deploy lên Lamp `lamp-0c4e`
ngày 2026-09-11, xác minh startup thành công. Chờ live test cử chỉ.
Xem [điều khiển vật lý](../../robots/lamp/docs/vi/physical-controls_vi.md) để
biết cấu hình và chi tiết cử chỉ.

## Nguyên Tắc

- **Hardware là plugin** — cắm vào thì play, không cắm thì skip
- **Tầng hệ thống chạy KHÔNG cần runtime** — thiết bị luôn phản hồi
- **Code là source of truth** — docs phản ánh code
- **HAL là hardware driver** — không chứa logic AI
- **SKILL.md native** — không dùng MCP, LLM tự đọc skill và gọi curl
- **Chủ thiết bị có thể tạo skill** — `skill-creator` built-in hướng dẫn soạn,
  kiểm thử và đóng gói skill để đưa lên Autonomous Skill Store.

## Lamp Simulator trên Laptop

> **Hướng dẫn cài đặt và chạy nằm ở [simulator_vi.md](simulator_vi.md)** — điều kiện
> chuẩn bị, file config và bản mẫu, bốn target `make`, và xử lý sự cố. Mục này nói
> simulator *là gì*.

`make sim` khởi động declaration `lamp` production trên laptop. HAL vẫn dùng
route và safety gate bình thường, nhưng thay motion, LED, camera, microphone,
speaker, voice và sensing bằng service ảo; nó không mở servo bus, camera/mic
macOS, GPIO hay gửi GELF log. Khi khởi động, lệnh in link local có thể click
cho HAL docs và, với body Lamp mặc định, `http://127.0.0.1:5001/simulator`.
Link sau có CAD assembly Lamp đã check-in để xem quanh, cùng giá trị live của
năm joint, playback recording và nút LED effect qua đúng endpoint `/servo/*`
và `/led/*` mà skill sử dụng: kéo để xoay camera, cuộn để zoom, double-click
để reset. Bảng state ghi rõ posture mode đang giữ body (`zero`, `hold`) bên
cạnh giá trị joint live, và nút Motor control đang giữ mode đó được làm sáng: một
animation bấm lúc motor đang hold sẽ hiện là bị bỏ qua kèm lý do, không phải
dấu tích, vì `/servo/play` trả `"ignored"` thay vì `"ok"` cho lệnh nó đã bỏ.
Motion preview của CAD phản hồi theo joint live, kể cả khi phát
recording; một control chuyển về assembly tĩnh nguyên bản để đối chiếu. Repo
chưa có hierarchy cơ khí, pivot, axis hay CAD zero offset đã calibration, nên
phản hồi hình ảnh này không khẳng định pose `down` hoặc `right` render ra đúng
với Lamp vật lý.

Đây là interface simulator, không phải physics model: không có khối lượng hay
va chạm; recording CSV có sẵn chỉ replay timing trong RAM. Nội dung
camera/audio ảo là deterministic. Muốn boot test body tối giản theo contract
thì chạy `make sim DEVICE_TYPE=sim`. Body đó chỉ khai `motion` và `system`,
không gì khác — đó là cách chứng minh HAL mount đúng những gì `ROBOT.md` khai
và không mount thêm; lamp không kiểm được điều này vì lamp khai đủ mọi thứ.
Xem `robots/sim/ROBOT.md`.

Muốn kiểm tra media thật trên Mac, chạy `make sim SIM_MEDIA=host`. Lệnh này
chủ động mở camera, microphone và speaker của máy: trang simulator hiển thị
camera stream, **Play test tone** dùng speaker, còn **Record 3 seconds** thu
WAV để phát lại. Mặc định `SIM_MEDIA=virtual` không xin permission và giữ test
deterministic.

Host mode không bao giờ crash. Lúc boot mỗi subsystem được probe — webcam mở và
đọc thử một frame, microphone thu vài mili-giây — cái nào thiếu, đang bị chiếm
hoặc bị từ chối permission thì tự rơi về thiết bị ảo kèm log `[sim-media]`.
`GET /simulator/state` báo kết quả theo từng subsystem (`media_camera`,
`media_audio`, `media_reasons`; `media` chỉ là "host" khi cả hai đều host), và
trang simulator in đúng lý do đó, đồng thời tắt nút tone/record khi audio là
ảo. Trên macOS, hai permission nằm ở System Settings > Privacy & Security >
Camera và Microphone, phải cấp cho ứng dụng terminal đang chạy HAL.

Camera ở host mode dùng driver chỉ-dành-cho-simulation
(`hal/drivers/camera/host_capture_device.py`, đăng ký tên `host`) mở webcam qua
backend OpenCV gốc của hệ điều hành — AVFoundation trên macOS, nơi đường V4L2
của production và phần healing power-cycle USB không tồn tại. Body thật vẫn
chọn `driver:` từ ROBOT.md.

Host mode còn chạy **pipeline giọng nói thật**: entry VAD, Silero, STT, realtime
agent (Gemini Live), wake word, và phần dispatch `[turn] route=…` forward sang
os-server — đúng code mà board chạy, không phải stub. Cổng quyết định là
`state.simulation_audio`, nên hai quyết định không bao giờ lệch nhau: laptop
đang dùng microphone của chính nó thì được đúng pipeline mà microphone đó sinh
ra, còn khi macOS từ chối permission thì cờ lật lại và rơi về
`VirtualVoiceService` kèm log lý do, thay vì dựng pipeline thật lên một thiết bị
đã chết. `SIM_MEDIA=virtual` vẫn giữ stub, nên test vẫn im lặng và offline.

Credential lấy từ chính file config.json mà HAL dùng chung với os-server, y như
trên board — trỏ `OS_CONFIG_PATH` vào state dir của `make os-dev` là một file
nuôi cả hai process. Riêng `llm_api_key` phủ cả LLM, `AutonomousSTT`, TTS
ElevenLabs, mô tả ảnh, **và cả** Gemini Live (key fallback về nó, endpoint là
`llm_base_url` + `/ws/gemini`); `deepgram_api_key` là tuỳ chọn, chỉ để đổi
provider STT.

Nhạc cũng phát được. `MusicService` stream yt-dlp → ffmpeg → **aplay** (ALSA),
hoặc **paplay** khi có sink Bluetooth — macOS không có cái nào, nên cả hai đường
đều chết ngay ở `Popen` và thiết bị nói ra "Sorry, I can't play that right now".
macOS được đường thứ ba: chính output device AudioToolbox của ffmpeg, chọn nó
thay vì `ffplay` vì ffmpeg vốn đã là dependency bắt buộc ở đây còn `ffplay` thì
không phải build nào cũng có. `SIM_MEDIA=virtual` giữ nguyên pipeline nhưng đổ
vào null sink — vẫn search và decode đầy đủ, chỉ là laptop im lặng.

Có hai đường dẫn của HAL là device-absolute và phải dời khi chạy trên laptop,
cả hai đều đã đọc từ env và được target `sim` set sẵn: `HAL_SNAPSHOT_DIR` (nơi
`GET /camera/snapshot?save=true` ghi file — bắt buộc nằm dưới home của chính
agent runtime, trên board là `/root/.codex/media/hal-snapshots`, nếu không agent
không đọc lại được frame và os-server không serve được thumbnail) và
`HAL_SNAPSHOT_PERSIST_DIR` (`/var/lib/hal/snapshots` chỉ root ghi được).

## Voice Pipeline

```
Mic (always on) → Local VAD (RMS energy, free)
    → Speech detected → Connect Deepgram STT
        → "hey lamp, tắt đèn" → voice_command → local intent → thực thi
        → "anh ơi đi ăn không" → voice (ambient) → OpenClaw
    → Silence 3s → Disconnect Deepgram
    → _submit_speech_emotion_from_session: WAV → perception-service SER → OS server event (luôn chạy, độc lập transcript)
    → _identify_and_decorate (1 lần) → if transcript: _send_to_lamp voice/voice_command
```

Chi tiết SER: [speech-emotion_vi.md](speech-emotion_vi.md).

## Sensing Flow

Thu nhận dữ liệu môi trường SEN55 là capability HAL tùy chọn riêng. Xem
[Cảm biến môi trường](../../robots/lamp/docs/vi/environment-sensing_vi.md) về đấu dây, cấu hình và
API snapshot; hiện chưa phát sensing event tới OS/agent.

```
HAL sensing loop (mỗi 2s) → Đọc 1 frame camera, chạy tất cả detectors:
    ├─ Motion detection (frame diff) → event nếu >8% pixel thay đổi
    ├─ Face recognition (InsightFace buffalo_sc) → phân loại friend/stranger
    │     → presence.enter (JPEG được annotate bbox: xanh=friend, đỏ=stranger)
    │     → presence.leave (3 tick liên tiếp không thấy mặt)
    ├─ Light level (mean brightness, mỗi 30s) → event nếu thay đổi >30/255
    └─ Sound detection (mic RMS) → event nếu > threshold

Event có ảnh? (large motion, face enter) → encode frame full-resolution JPEG q85
Ảnh face enter: frame gốc được vẽ bounding box + nhãn friend/stranger

POST /api/sensing/event {type, message, image?}
    → OS server (Go):
        1. Voice event + local intent match? → thực thi trực tiếp (~50ms)
        2. Không match → forward OpenClaw:
           - Có image → SendChatMessageWithImage (text + vision content block)
           - Không image → SendChatMessage (text only)
        3. OpenClaw AI nhìn ảnh + đọc context → quyết định hành động → gọi SKILL API
```

Cooldown bảo vệ chi phí LLM: motion/sound 60s, presence 10s, light.level 30s.
