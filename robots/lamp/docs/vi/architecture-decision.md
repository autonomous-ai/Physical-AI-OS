# Quyết Định Kiến Trúc: AI Lamp — Hybrid Hardware Control

> **Lưu ý:** bản tiếng Việt này không phải bản dịch 1-1 của [architecture-decision.md](../architecture-decision.md) — nó có thêm bảng phần cứng, bảng endpoint theo từng nhóm và mục trạng thái triển khai; hai bản có thể khác nhau về chi tiết.

## Ngày: 2026-03-24

---

## 1. Bối Cảnh & Hành Trình Quyết Định

Dự án AI Lamp trải qua nhiều giai đoạn tìm hướng kiến trúc trước khi đi đến quyết định cuối cùng:

1. **Ban đầu**: Dự định xây dựng project Go độc lập, sử dụng MCP protocol để giao tiếp với phần cứng.
2. **Phát hiện 1**: HAL runtime (Python) **đã chạy** trên Raspberry Pi 4 với đầy đủ hardware drivers — motor, LED, audio. Không cần viết lại driver trong Go.
3. **Phát hiện 2**: OpenClaw sử dụng **SKILL.md** (skill system native), **KHÔNG PHẢI MCP**. Skills là file Markdown mô tả API, LLM tự đọc và gọi.
4. **Quyết định cuối**: Kiến trúc Hybrid — OpenClaw skills gọi Lamp HTTP API, Lamp bridge đến HAL Python services.

### Phần Cứng (Raspberry Pi 4)

| Thiết bị | Chi tiết | Chức năng |
|---|---|---|
| 5 Servo Motors | Feetech | Chuyển động 5 trục (xoay, nghiêng, biểu cảm) |
| 64 WS2812 RGB LEDs | Grid 8x5 | Full color, điều khiển từng pixel |
| Camera | Trong lõi đèn | Thị giác máy tính |
| Microphone | — | Đầu vào giọng nói |
| Speaker | — | Đầu ra giọng nói |
| Display | GC9A01 1.28" tròn (SPI) | Mắt hoạt hình, thông tin, trạng thái |

---

## 2. Quyết Định Kiến Trúc Cuối Cùng

**Kiến trúc Hybrid 3 tầng**: OpenClaw (AI) → Lamp Server (Go) → HAL Runtime (Python) → Phần cứng.

Nguyên tắc cốt lõi:
- **Tầng hệ thống** (Go Lamp) hoạt động **KHÔNG cần OpenClaw** — thiết bị luôn phản hồi được.
- **Điều khiển hướng người dùng** thông qua OpenClaw skills gọi HTTP API.
- **HAL runtime** chỉ làm hardware drivers — không chứa logic AI.
- **Không dùng MCP** — dùng SKILL.md native của OpenClaw.
- **Hardware là plugin** — cắm vào thì play, không cắm thì bỏ qua.

### Hardware Plugin (Plug & Play)

Mọi thiết bị phần cứng là **plugin** — cắm vào thì driver load + skill available, không cắm thì hệ thống vẫn chạy bình thường.

Khi khởi động, Lamp server tự phát hiện phần cứng và:
1. Chỉ load driver cho phần cứng được phát hiện
2. Chỉ bật HTTP API endpoint tương ứng
3. Chỉ deploy SKILL.md liên quan cho OpenClaw

| Plugin | Cách phát hiện | Nếu không có |
|---|---|---|
| Servo Motors | Quét USB serial (Feetech) | Không body language, đèn tĩnh — vẫn là smart light |
| LED (WS2812) | Kiểm tra SPI (`/dev/spidev0.0`) | Không điều khiển ánh sáng — chỉ có system LED |
| Camera | Kiểm tra V4L2 (`/dev/video0`) | Không gesture, presence, tracking — chỉ voice control |
| Microphone | Quét ALSA device | Không voice input — chỉ điều khiển qua app/text |
| Speaker | Quét ALSA device | Không voice output — chế độ im lặng, chỉ LED feedback |
| Display | Quét I2C/SPI (GC9A01/SSD1306) | Không mắt/thông tin — chỉ LED status |

Cùng codebase hỗ trợ nhiều cấu hình:
- **Full lamp**: Tất cả plugin → AI companion đầy đủ
- **Simple lamp**: LED + Mic + Speaker → đèn thông minh có voice
- **Dev/test**: Không hardware → stub drivers, API vẫn hoạt động

---

## 3. Software Stack

### OpenClaw — Bộ Não AI

Thay thế hoàn toàn LiveKit + OpenAI của HAL gốc.

- Personality & nhân cách cho đèn
- LLM multi-provider (Claude, GPT, Gemini, ...)
- Skill system (SKILL.md)
- Channels (giọng nói, text, ...)
- Memory (nhớ ngữ cảnh, sở thích người dùng)

### HAL Runtime (Python) — CHỈ Hardware Drivers

Giữ nguyên từ dự án HAL hiện tại, nhưng bỏ phần AI/LiveKit:

- **MotorsService** — điều khiển 5 servo Feetech
- **RGBService** — điều khiển 64 WS2812 LED (rpi_ws281x)
- **Camera** — OpenCV capture, chụp JPEG, MJPEG stream
- **Audio** — Seeed mic/speaker, amixer volume, thu WAV, phát tone
- Event-driven **ServiceBase** với priority dispatch

Tất cả hardware expose qua FastAPI trên `127.0.0.1:5001` (systemd: `hal.service`). Nginx proxy `/hw/*` chỉ cho caller trên cùng máy — client bên ngoài nhận 403. Swagger UI tại `/hw/docs` không truy cập được từ LAN.

### Lamp Server (Go) — Hệ Thống + HTTP API Bridge

- Tầng hệ thống: reset button, mạng, OTA, MQTT
- HTTP API bridge: nhận request từ OpenClaw skills, chuyển tiếp đến HAL Python services

---

## 4. Tầng 1: Hệ Thống (Lamp Server, Go, luôn chạy)

Hoạt động **KHÔNG cần OpenClaw**. Nếu OpenClaw ngừng, thiết bị vẫn khởi động, hiển thị trạng thái, và có thể cấu hình lại.

| Chức năng | Mô tả |
|---|---|
| Nút reset | GPIO 26 — nhấn giữ để factory reset |
| Quản lý mạng | AP/STA mode, cấu hình WiFi, quét mạng |
| Cập nhật OTA | Kiểm tra version, tải và cài đặt bản cập nhật |
| Giao tiếp MQTT | Kết nối backend, báo cáo trạng thái, nhận lệnh |
| Giám sát internet | Phát hiện mất kết nối, tự khôi phục |
| **Autonomous sensing** | Sensing loop nhẹ, chạy liên tục: camera (presence, light level), mic (sound level, silence, voice tone), time (schedules), plug-in sensors. Đẩy event cho OpenClaw khi phát hiện thay đổi đáng kể. |
| **Ambient life** | Hành vi idle tạo cảm giác "sống": breathing LED (sine-wave brightness), color drift (xoay palette ấm), micro-movements (servo recordings an toàn), TTS self-talk (tự lẩm bẩm). Tự pause khi có interaction, resume sau 60s im lặng. |

### Autonomous Sensing Loop (Tầng 1.5)

Lamp chạy sensing loop liên tục, chi phí thấp, phát hiện sự kiện trên thiết bị (**edge detection**). Khi phát hiện sự kiện đáng kể → đẩy context cho OpenClaw để AI quyết định hành động. Proactive behavior mà không tốn LLM tokens liên tục.

```
Sensing Loop (Lamp Server, luôn chạy):
  Camera → presence.enter / presence.leave / light.level
  Mic    → sound.level / sound.silence / sound.voice_tone
  Time   → time.schedule (cron-like)
  Sensor → sensor.* (plug-in: nhiệt độ, độ ẩm, ...)
       │
       │ event + context (chỉ khi có thay đổi đáng kể)
       ▼
  OpenClaw (AI Brain) → quyết định hành động → gọi Lamp HTTP API → phần cứng
```

**Rule-based** (không cần AI): auto-dim khi vắng, adjust brightness khi trời tối, idle animations.
**AI-driven** (OpenClaw quyết định): chào hỏi, phản ứng mood, empathy, gợi ý theo lịch.

**Lamp Server modules (trong `system/`; `runtimes/` nằm ở root repo):**

```
server/server.go          — HTTP server (Gin, port 5000)
server/config/            — Quản lý cấu hình JSON
(internal/resetbutton/ đã xóa — cử chỉ click/giữ nút GPIO giờ do HAL xử lý: hal/drivers/gpio_button.py, hal/drivers/button_actions.py)
system/network/         — WiFi AP/STA
runtimes/openclaw/        — Cấu hình OpenClaw & WebSocket
system/beclient/        — Backend client, báo cáo trạng thái
system/device/          — Setup, xử lý lệnh MQTT, báo cáo trạng thái
system/ambient/         — Hành vi idle "sinh vật sống" (breathing LED, servo micro-movements, TTS mumbles). Opt-in theo device qua capability `lifelike`. Gọi HAL HTTP API. Pause khi có tương tác, resume sau 60s yên lặng. Xem sensing-behavior_vi.md → "Hành vi Ambient khi idle".
lib/mqtt/                 — MQTT client, tự kết nối lại
bootstrap/                — OTA, kiểm tra version
domain/                   — Struct dùng chung (device, network, OTA, OpenClaw)
```

**MQTT commands** (nhận qua fa_channel): `info`, `add_channel`, `ota`

---

## 5. Tầng 2: OpenClaw Skills (SKILL.md + HTTP API)

Toàn bộ **điều khiển phần cứng hướng người dùng** thông qua skill system native của OpenClaw:

1. File **SKILL.md** trong `workspace/skills/` mô tả API cho LLM
2. OpenClaw tự phát hiện skills (`skills.load.watch: true`)
3. **LLM đọc SKILL.md** → hiểu API → tự gọi `curl` đến HAL HTTP API tại `127.0.0.1:5001` cho phần cứng (Lamp server `127.0.0.1:5000` phục vụ API hệ thống/agent)
4. HAL Python services → điều khiển phần cứng

Đây **KHÔNG phải MCP**.

### Cấu trúc Skills

```
workspace/skills/
├── led-control/SKILL.md       ← mở rộng cho 64 LED grid
├── servo-control/SKILL.md     ← MỚI
├── camera/SKILL.md            ← MỚI
├── audio/SKILL.md             ← MỚI
├── display/SKILL.md            ← MỚI (dual-mode: eyes + info)
└── emotion/SKILL.md           ← MỚI (quan trọng nhất, kết hợp tất cả)
```

### HTTP API Endpoints

#### LED Control

| Endpoint | Method | Mô tả |
|---|---|---|
| `/led` | GET | LED strip info |
| `/led/color` | GET | Màu LED hiện tại |
| `/led/solid` | POST | Fill toàn bộ 1 màu RGB |
| `/led/paint` | POST | Set từng pixel (array tối đa 64) |
| `/led/off` | POST | Tắt tất cả LED |
| `/led/effect` | POST | Bật effect (breathing, candle, rainbow, notification_flash, pulse) |
| `/led/effect/stop` | POST | Dừng effect |
| `/led/status` | POST | Effect status-LED |
| `/led/restore` | POST | Khôi phục trạng thái LED trước đó của user |

#### Servo Control

| Endpoint | Method | Mô tả |
|---|---|---|
| `/servo` | GET | Recordings + animation state |
| `/servo/play` | POST | Phát một recording trong `hal/recordings/` (liệt kê qua `GET /servo`), vd curious, nod, happy_wiggle, idle, sad, excited, shy, shock, headshake, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching |
| `/servo/move` | POST | Joint positions với smooth interpolation |
| `/servo/release` | POST | Tắt torque tất cả servo |
| `/servo/position` | GET | Vị trí servo hiện tại |
| `/servo/aim` | GET/POST | Aim đầu đèn (center, desk, wall, left, right, up, down, user) |
| `/servo/track` | POST/GET (+ `POST /servo/track/update`, `POST /servo/track/stop`) | Tracking vật thể bằng vision. Xem [vision-tracking_vi.md](vision-tracking_vi.md) |

#### Camera

| Endpoint | Method | Mô tả |
|---|---|---|
| `/camera` | GET | Camera availability + resolution |
| `/camera/snapshot` | GET | Chụp 1 frame JPEG. `?save=true` lưu file timestamp, trả JSON path |
| `/camera/stream` | GET | MJPEG live stream (multipart/x-mixed-replace) |

#### Audio

| Endpoint | Method | Mô tả |
|---|---|---|
| `/audio` | GET | Audio device availability |
| `/audio/volume` | GET/POST | Lấy/đặt âm lượng loa (0-100%) |
| `/audio/play-tone` | POST | Phát test tone |
| `/audio/record` | POST | Thu âm WAV |

#### Voice

| Endpoint | Method | Mô tả |
|---|---|---|
| `/voice/speak` | POST | TTS — chuyển text thành giọng nói |
| `/voice/start` | POST | Start voice pipeline (Deepgram STT + TTS) |
| `/voice/stop` | POST | Stop voice pipeline |
| `/voice/status` | GET | Trạng thái voice pipeline |

#### Display (GC9A01 1.28" LCD tròn)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/display` | GET | State hiện tại (mode, expression) |
| `/display/eyes` | POST | Set eye expression + pupil position |
| `/display/info` | POST | Chuyển sang info mode (text/subtitle) |
| `/display/eyes-mode` | POST | Chuyển về eyes mode (default) |
| `/display/snapshot` | GET | Frame hiện tại dưới dạng JPEG |

#### Emotion (Kết hợp servo + LED + display)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/emotion` | POST | Biểu cảm cảm xúc (preset trong `hal/presets.py` `EMOTION_PRESETS`, liệt kê qua `GET /emotion/presets`) |

#### Scene

| Endpoint | Method | Mô tả |
|---|---|---|
| `/scene` | GET | Danh sách scene presets |
| `/scene` | POST | Kích hoạt scene (reading, focus, relax, movie, night, energize) |

#### Presence

| Endpoint | Method | Mô tả |
|---|---|---|
| `/presence` | GET | State hiện tại (present/idle/away) |
| `/presence/enable` | POST | Bật auto presence control |
| `/presence/disable` | POST | Tắt auto presence (manual mode) |

---

## 5b. Monitor Dashboard & System API

Web UI tại `/monitor` cung cấp khả năng quan sát real-time hoạt động của đèn.

### Monitor API Endpoints (Lamp Server, port 5000)

| Endpoint | Method | Mô tả | Nguồn dữ liệu |
|---|---|---|---|
| `/api/system/info` | GET | CPU load, RAM, nhiệt độ, uptime, version | `/proc/` |
| `/api/system/network` | GET | WiFi SSID, IP, signal, internet | `iwgetid`, `ping` |
| `/api/system/dashboard` | GET | Snapshot trạng thái tổng hợp | OpenClaw + config |
| `/api/agent/status` | GET | Trạng thái kết nối OpenClaw WS | `openclaw.Service.IsReady()` |
| `/api/agent/events` | GET | SSE stream events real-time | Event bus |
| `/api/agent/recent` | GET | 100 events gần nhất | Ring buffer |

### OpenClaw Event Bus

Ring buffer 200 events trong `openclaw.Service`, broadcast qua SSE đến browser.

**Các loại event:**

| Type | Mô tả |
|---|---|
| `sensing_input` | HAL phát hiện motion/sound |
| `chat_send` | Tin nhắn gửi đến OpenClaw |
| `lifecycle` | Agent start/end/error |
| `thinking` | Chain-of-thought reasoning |
| `tool_call` | Tool invocation (tên + args) |
| `assistant_delta` | Streaming text generation |
| `chat_response` | Response partial/final |
| `tts` | Gửi đến speaker |

**Flow quan sát được:**
```
👁 Sensing → ➜ Send → ⚙ Agent → 🧠 Think → 🔧 Tool → ✏ Write → 💬 Response → 🔊 TTS
```

### Web Monitor Page (`/monitor`)

Dashboard gồm 4 phần:
1. **Status grid** (4 cards): OpenClaw, System (CPU/RAM/temp/uptime), Network (SSID/IP/signal), Hardware badges
2. **Presence bar**: Present/idle/away từ HAL `/hw/presence`
3. **Workflow timeline**: SSE event stream real-time, color-coded
4. **Camera**: Collapsible MJPEG stream + snapshot

### HAL Status Endpoints (qua nginx `/hw/*`)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/hw/health` | GET | Trạng thái HW: servo, LED, camera, audio, sensing |
| `/hw/presence` | GET | Presence: present/idle/away, last motion |
| `/hw/camera/stream` | GET | MJPEG live stream |
| `/hw/camera/snapshot` | GET | Chụp JPEG |

## 6. Sơ Đồ Kiến Trúc

```
┌─────────────────────────────────────────────────────────────────────┐
│                        NGƯỜI DÙNG                                   │
│                  (Giọng nói / Cử chỉ / App)                        │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     OpenClaw (Bộ não AI/LLM)                        │
│                                                                     │
│  • Personality & nhân cách        • Memory                          │
│  • LLM multi-provider             • Channels (giọng nói, text)      │
│                                                                     │
│  workspace/skills/                                                  │
│  ├── led-control/SKILL.md                                           │
│  ├── servo-control/SKILL.md                                         │
│  ├── camera/SKILL.md                                                │
│  ├── audio/SKILL.md                                                 │
│  └── emotion/SKILL.md             ← skill quan trọng nhất          │
│                                                                     │
│  LLM đọc SKILL.md → gọi curl → 127.0.0.1:5000                     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ HTTP (curl)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Lamp Server (Go, port 5000)                      │
│                                                                     │
│  ┌──────────────────────┐    ┌────────────────────────────────────┐ │
│  │  TẦNG 1: HỆ THỐNG    │    │  HTTP API (Tầng 2)                 │ │
│  │  (luôn chạy)         │    │                                    │ │
│  │                      │    │  /api/led      → LED control       │ │
│  │  • Nút reset GPIO 26 │    │  /api/servo    → Servo control     │ │
│  │  • Quản lý mạng      │    │  /api/camera/* → Camera            │ │
│  │  • Cập nhật OTA      │    │  /api/audio/*  → Audio             │ │
│  │  • MQTT backend      │    │  /api/emotion  → Emotion (kết hợp) │ │
│  │  • Giám sát internet │    │                                    │ │
│  │                      │    │  Bridge đến HAL Python ──────┐  │ │
│  │                      │    │                                 │  │ │
│  │  Hoạt động KHÔNG     │    │                                 │  │ │
│  │  cần OpenClaw        │    │                                 │  │ │
│  └──────────────────────┘    └─────────────────────────────────┘  │ │
│                                                                │  │ │
└────────────────────────────────────────────────────────────────┼──┘ │
                                                                 │
                            ┌────────────────────────────────────┘
                            │ HTTP/gRPC/subprocess (bridge)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  HAL Runtime (Python, Raspberry Pi 4)            │
│                                                                     │
│  • MotorsService  — 5 servo Feetech (xoay, nghiêng, biểu cảm)     │
│  • RGBService     — 64 WS2812 LED grid 8x5 (rpi_ws281x)           │
│  • Camera         — OpenCV capture, snapshot, MJPEG stream          │
│  • Audio          — Seeed mic/speaker, amixer volume, thu WAV       │
│  • ServiceBase    — Event-driven, priority dispatch                 │
│                                                                     │
│  FastAPI :5001 | systemd: hal.service                       │
│  nginx: /hw/* → 127.0.0.1:5001 (local-only, external → 403)       │
│                                                                     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        PHẦN CỨNG                                    │
│                                                                     │
│  🔧 5 Servo Motors (Feetech)    💡 64 WS2812 RGB LEDs (grid 8x5)  │
│  📷 Camera (trong lõi đèn)      🎤 Microphone                     │
│  🔊 Speaker                                                        │
└─────────────────────────────────────────────────────────────────────┘
```

> **Implementation hiện tại:** bridge qua Lamp server (`/api/led`, `/api/servo`, `/api/emotion`, …) ở sơ đồ trên là thiết kế ban đầu. Hiện tại OpenClaw skills gọi thẳng HAL tại `127.0.0.1:5001` (vd `POST /led/solid`, `POST /servo/aim`, `POST /emotion`); Lamp server (`:5000`) chỉ phục vụ API hệ thống/agent.

---

## 7. Emotion Skill — Điểm Khác Biệt Quan Trọng

Emotion là skill **mới quan trọng nhất** — kết hợp tất cả phần cứng để tạo **generative body language** cho đèn.

### API

```
POST /api/emotion
{"emotion": "curious", "intensity": 0.8}
```

(Implement thực tế: HAL `POST http://127.0.0.1:5001/emotion` với cùng body.)

### Cách Hoạt Động

Lamp server nhận emotion request → chuyển đổi thành tổ hợp:

| Thành phần | Ví dụ "curious" (intensity 0.8) |
|---|---|
| **Servo** | Nghiêng nhẹ sang phải, ngẩng lên một chút |
| **LED** | Chuyển sang tông vàng ấm, nhấp nháy nhẹ |
| **Audio** | Phát âm thanh "hmm" nhỏ (tùy chọn) |

Mỗi lần gọi tạo ra biểu cảm **unique** — không lặp lại y hệt nhờ randomized parameters trong preset.

### Tại Sao Quan Trọng

- LLM chỉ cần gọi **1 endpoint** thay vì phối hợp 3 endpoint riêng lẻ (servo + LED + audio)
- Biểu cảm tự nhiên, không máy móc — nhờ randomization
- Mở rộng dễ dàng: thêm emotion preset mới không cần thay đổi SKILL.md

### Ví Dụ Sử Dụng

Người dùng nói: *"Bạn nghĩ gì về bức tranh này?"*

OpenClaw LLM:
1. Gọi `POST /emotion {"emotion": "curious", "intensity": 0.7}` (HAL :5001) — đèn nghiêng, đổi màu
2. Gọi `GET /camera/snapshot` — nhìn bức tranh / người dùng
3. Trả lời bằng giọng nói + gọi `POST /emotion {"emotion": "thinking", "intensity": 0.5}`

---

## 8. Luồng Giao Tiếp

```
Người dùng nói
    │
    ▼
OpenClaw (AI/LLM)
    │ đọc SKILL.md, quyết định hành động
    │
    ▼
curl HAL HTTP API (127.0.0.1:5001)
    │
    ▼
HAL Python Services
    │ MotorsService / RGBService / Audio
    │
    ▼
Phần cứng (Servo / LED / Camera / Speaker)
```

**Ví dụ cụ thể:**

Người dùng: *"Chiếu đèn xuống bàn, chế độ tập trung"*

```bash
# OpenClaw LLM đọc servo-control/SKILL.md + scene/SKILL.md, rồi gọi:

curl -s -X POST http://127.0.0.1:5001/servo/aim \
  -H "Content-Type: application/json" \
  -d '{"direction": "desk"}'

curl -s -X POST http://127.0.0.1:5001/scene \
  -H "Content-Type: application/json" \
  -d '{"scene": "focus"}'
```

Không cần logic parse lệnh — **LLM tự hiểu từ mô tả trong SKILL.md**.

---

## 9. Trạng Thái Triển Khai

Tất cả hardware endpoints chạy trực tiếp trên HAL FastAPI (:5001). OpenClaw skills gọi qua `127.0.0.1:5001`.

| Thành phần | Trạng thái |
|---|---|
| SKILL.md files | ✅ `skills/` (root repo) |
| HAL endpoints | ✅ `hal/server.py` + `hal/routes/` |
| Sensing event routing | ✅ `system/server/sensing/` |
| Local intent matching | ✅ `system/intent/` |
| Voice pipeline (VAD + Deepgram) | ✅ `hal/drivers/voice/` |
| Ambient idle behaviors | ✅ `system/ambient/` |

### Phần Cứng ↔ Tầng Mapping

| Phần cứng | Tầng 1 (Hệ thống) | Tầng 2 (OpenClaw Skills) |
|---|---|---|
| **LED (64 WS2812)** | Khởi động, lỗi, trạng thái hệ thống | Màu, độ sáng, scene, hiệu ứng, pattern |
| **Servo (5 trục)** | — | Xoay, nghiêng, preset, biểu cảm |
| **Camera** | — | Hiện diện, khuôn mặt, cử chỉ, ánh sáng |
| **Microphone** | — | Đầu vào giọng nói (OpenClaw xử lý) |
| **Speaker** | — | TTS, thông báo, âm thanh môi trường |
| **Nút Reset** | Nhấn giữ → factory reset | — |
| **Mạng** | AP/STA, WiFi, giám sát internet | — |

---

## 10. Câu Hỏi Mở

- [x] **Bridge Go ↔ Python**: HTTP proxy. HAL chạy FastAPI trên `127.0.0.1:5001`, Lamp Server proxy request từ port 5000. Đơn giản, dễ debug, không tight coupling.
- [x] **Xử lý camera**: On-device OpenCV trong HAL Python. Frame diff cho motion detection trong sensing loop.
- [x] **Đầu vào audio**: HAL owns mic. Local VAD (RMS energy) + on-demand Deepgram STT. Wake word "Hey Lamp" detected trong transcript.
- [x] **LED driver**: HAL Python rpi_ws281x driver sở hữu toàn bộ LED control. Go SPI driver đã xóa khỏi Lamp — đèn này dùng LED driver của HAL.
- [x] **Generative body language**: Emotion presets với randomized parameters. Danh sách preset trong `hal/presets.py` (`EMOTION_PRESETS`, 22 preset lúc viết). Mỗi lần gọi tạo biểu cảm unique nhờ randomization.
