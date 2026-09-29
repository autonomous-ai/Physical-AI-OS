# Hệ Thống Plugin

Các ứng dụng Python độc lập mở rộng khả năng thiết bị Autonomous OS. Plugin
chạy như process riêng biệt, được quản lý bởi systemd, truy cập phần cứng
thông qua HTTP API của HAL.

Viết tháng 7/2026. Trạng thái: **v1 đã triển khai.**

## Kiến Trúc

HAL là kernel — plugin là userspace. OS điều phối mọi truy cập phần cứng:

```
┌─────────────────────────────────────────────┐
│  Agent Runtime (brain, luôn chạy)           │
├─────────────────────────────────────────────┤
│  Plugin A    Plugin B    Plugin C           │  ← ứng dụng userspace
│    ↓ HTTP      ↓ HTTP      ↓ HTTP          │
├─────────────────────────────────────────────┤
│  HAL :5001 (dịch vụ phần cứng, luôn bật)   │  ← kernel
├─────────────────────────────────────────────┤
│  LED  Servo  Audio  Camera  GPIO  Sensing   │
└─────────────────────────────────────────────┘
```

Plugin cùng tồn tại với HAL và agent runtime. HAL tuần tự hóa truy cập phần
cứng nên nhiều plugin có thể chạy đồng thời mà không xung đột tài nguyên.

Endpoint cài đặt và quản lý vòng đời plugin cần quyền admin. Tên manifest và thao tác quản lý cho phép chữ cái, chữ số, gạch dưới, gạch ngang và dấu chấm (kể cả tên cũ có chữ hoa). Tên rỗng, `.` và `..`, đường dẫn, khoảng trắng, glob và chỉ thị unit-file bị từ chối trước thao tác filesystem. URL cài đặt bắt đầu bằng `-` bị từ chối; Git nhận `--` trước URL để không hiểu URL là tùy chọn. Plugin vẫn chạy mã bên thứ ba được tin cậy; kiểm tra tên không phải sandbox.

## Định Dạng Plugin

Plugin là một thư mục (git repo) chứa:

```
my-plugin/
  plugin.json         # metadata (bắt buộc)
  main.py             # điểm vào (mặc định)
  requirements.txt    # phụ thuộc pip (tùy chọn)
  README.md           # mô tả + video demo
```

### plugin.json

```json
{
  "name": "dance-party",
  "version": "1.0.0",
  "description": "LED nhảy theo nhịp nhạc",
  "entry": "main.py"
}
```

| Trường | Bắt buộc | Mô tả |
|--------|----------|-------|
| `name` | Có | Tên định danh (dùng làm tên thư mục + tên systemd unit) |
| `version` | Không | Chuỗi semver |
| `description` | Không | Mô tả ngắn |
| `entry` | Không | Điểm vào Python, mặc định là `main.py` |

### main.py

Plugin truy cập phần cứng qua HTTP API của HAL. Biến môi trường `HAL_URL`
được inject bởi systemd unit (mặc định `http://localhost:5001`):

```python
import os, time, requests

HAL = os.environ.get("HAL_URL", "http://localhost:5001")

requests.post(f"{HAL}/led/effect", json={"effect": "rainbow"})
requests.post(f"{HAL}/voice/speak", json={"text": "Xin chào!"})
time.sleep(30)
requests.post(f"{HAL}/led/off")
```

## Cài Đặt & Vòng Đời

### Phân Phối

Plugin cài từ bất kỳ URL git nào — HuggingFace Spaces, GitHub, GitLab, Gitea,
hoặc repo tự host:

```bash
# Cài từ HuggingFace
POST /api/plugin/install {"url": "https://huggingface.co/spaces/user/my-plugin"}

# Cài từ GitHub
POST /api/plugin/install {"url": "https://github.com/user/my-plugin"}
```

### API Endpoints

Tất cả endpoint yêu cầu xác thực admin.

```
GET    /api/plugin/browse        — khám phá plugin cộng đồng (proxy HuggingFace Spaces API)
POST   /api/plugin/install       — clone git repo, tạo venv, tạo systemd unit
GET    /api/plugin               — danh sách plugin đã cài với trạng thái
POST   /api/plugin/:name/start   — khởi động plugin
POST   /api/plugin/:name/stop    — dừng plugin
DELETE /api/plugin/:name         — gỡ cài đặt (dừng + xóa file + systemd unit)
```

### Tích Hợp Systemd

Mỗi plugin chạy như systemd service (`os-plugin-<name>.service`):

- `Restart=on-failure` — tự phục hồi khi crash
- `MemoryMax=256M` — giới hạn tài nguyên trên thiết bị constrained
- `WorkingDirectory` trỏ đến thư mục plugin
- Biến `HAL_URL` được inject

### Giao Diện Web

Tab **Settings > Plugins** cung cấp:
- **Installed** — danh sách plugin đã cài với trạng thái (running/stopped/failed),
  nút Start/Stop/Uninstall
- **Browse** — khám phá plugin cộng đồng từ HuggingFace Spaces với tag
  `autonomous-os-plugin`, cài một click. Backend proxy HF API để tránh CORS
  (`GET /api/plugin/browse`)
- **Install from URL** — dán URL git cho plugin không phải HF (GitHub, GitLab, v.v.)

## Lộ Trình

### v1 — Pipeline + Store (đã triển khai)

Git URL → venv → systemd unit → HAL HTTP. Hệ thống plugin đầy đủ:
- Cài từ bất kỳ URL git nào (HuggingFace, GitHub, GitLab, v.v.)
- Quản lý vòng đời bằng systemd (start/stop/restart khi crash)
- Plugin store — duyệt plugin cộng đồng từ HuggingFace Spaces
  (tag `autonomous-os-plugin`), cài một click
- Cài thủ công bằng URL cho plugin không phải HF
- Giao diện web (Installed / Browse / Install from URL)
- Template plugin trên HuggingFace để community fork

### v2 — SDK + Tích Hợp Agent

- **Package `autonomous-sdk`** — bọc HAL HTTP thành API sạch:
  ```python
  from autonomous import Robot

  class RadioPlayer(AutonomousApp):
      async def play_radio(self, robot: Robot, genre: str = "lofi"):
          """Phát radio internet."""
          await robot.audio.stream_url(STATIONS[genre])
          robot.led.visualize_audio()
  ```
- **Tự đăng ký MCP tool** — method có docstring tự thành tool agent gọi được
  qua local MCP server. Agent có thể gọi plugin bằng giọng nói.
- **Định tuyến capability xuyên thiết bị** — `capabilities` trong plugin.json
  xác định thiết bị nào chạy được plugin.

### v3 — Hệ Sinh Thái

- **Quản lý tài nguyên** — HAL audio mixer, camera multiplexing
- **Chế độ exclusive** — `"exclusive": true` park HAL, plugin chiếm phần cứng
- **Plugin JS** — plugin chạy trong trình duyệt qua WebRTC (không cài đặt)

## Bảo Mật

- **Cài đặt yêu cầu admin** — mọi API plugin đều cần xác thực
- **Mô hình tin cậy: chạy cục bộ = tin tưởng hoàn toàn.** Cài plugin nghĩa là
  tin tưởng tác giả. Giống mô hình app của Pollen.
- Plugin truy cập HAL qua HTTP — không truy cập filesystem nội bộ HAL
- Giới hạn tài nguyên systemd ngăn cạn kiệt tài nguyên
- Tương lai: sandbox container/seccomp nếu hệ sinh thái mở rộng

## Template

Fork `integrations/plugin-template/` để bắt đầu. Chứa plugin hello-world
với demo LED + giọng nói.

## Tham Khảo

- Phân tích hệ sinh thái Pollen: `robots/reachy-mini/docs/pollen-ecosystem-analysis.md`
- API routes của HAL: `hal/routes/`
- Capability thiết bị: `robots/contract/capabilities.md`
- Template plugin: `integrations/community-apps/plugin-template/`
- HuggingFace template: https://huggingface.co/spaces/autonomous-os/autonomous-os-hello-robot
