# Bootstrap & OTA

## Snapshot tiến độ cập nhật

`software-update` ghi nguyên tử `/root/bootstrap/progress/<component>.json` cho mỗi lượt cài đặt được nhận (không bao gồm lệnh rollback/recovery thủ công). Lượt gần nhất được giữ khi refresh frontend hoặc restart service. Các trường gồm `target`, `run_id`, `phase`, `pid`, `boot_id`, `updated_at` (giây Unix), `downloaded_bytes`, `total_bytes` và `message` ngắn. Ghi tiến độ là best effort, không làm thay đổi kết quả cài đặt hoặc rollback.

Các bước gồm preparing, downloading, verifying, installing, restarting, checking, completed, failed và rolling_back. Chỉ ghi `completed` khi updater hiện có thoát thành công sau kiểm tra riêng của component; không phải runtime installer nào cũng kiểm tra health gateway. Journal rollback còn pending luôn khiến lượt update được tính là thất bại dù khôi phục thành công. Snapshot cũ hơn 30 giây mà PID updater đã mất (hoặc boot ID thay đổi) được trả về là `interrupted`, không phải thành công.

Tải qua `download_verified` lấy số byte đã ghi mỗi giây và Content-Length của HTTP 200 cuối cùng. Kích thước redirect, thiếu độ dài, chunked transfer hoặc tổng byte không khớp không tạo phần trăm. Kiểm tra checksum trước cài đặt. Package manager/upstream installer chỉ báo bước thực tế, không dựng phần trăm tổng. Không theo dõi lượt rollback/recovery thủ công hoặc lượt tải bên trong installer bên thứ ba.

Khi cài HAL, updater còn báo giải nén, kích hoạt và hoạt động `uv` nhận diện được: tên package đang tải/build/cài và số package resolved/prepared/installed/audited. Trường tùy chọn `activity_at` là thời điểm Unix của hoạt động nhận diện gần nhất (bằng 0 hoặc vắng mặt nếu không có). UI chỉ nhận tên/số lượng đã nhận diện; log thô vẫn nằm trong log installer. Ghi hoạt động tối đa mỗi giây một lần và thêm lần cuối. Log không nhận diện được vẫn giữ bước cài đặt; theo dõi không thay đổi exit code của `uv`. Hoạt động dependency không phải phần trăm theo byte hoặc phần trăm tổng cài đặt.

`GET /api/system/ota-updating` giữ `data.updating`, thêm `data.progress` (map component, có alias `agent` đang dùng) và `data.bootstrap_available`. OS Server đọc snapshot local kể cả khi bootstrap không truy cập được; khi chính OS Server restart thì API vẫn tạm ngắt. Cần cập nhật cả updater, OS Server và frontend để có giao diện chi tiết. Worker cũ vẫn được hỗ trợ qua danh sách updating.


Rootfs device lamp có service tắt LED sau HAL dành riêng Orange Pi (chờ 5 giây).
Device OTA reload systemd sau khi áp overlay và sau khi khôi phục rollback,
trước khi restart service; setup cũng reload sau khi áp hardware override.
Image builder đã copy rootfs này. Update riêng HAL không cài fallback.
Xem [vòng đời shutdown LED](../../robots/lamp/docs/vi/led-control_vi.md#fallback-shutdown-cho-orange-pi)
để biết cách kích hoạt, giới hạn và rollback.

Image OrangePi trì hoãn tác vụ đếm cập nhật cho MOTD của vendor (`orangepi-apt-updates`) 120 giây sau boot để giảm tranh chấp đọc storage với HAL. Chỉ dòng cron chính xác `@reboot root /usr/lib/orangepi/orangepi-apt-updates` được đổi; lịch đếm hằng ngày và lịch cập nhật APT/bảo mật giữ nguyên. Device hiện có có thể áp dụng bằng `sudo python3 scripts/imager/lib/defer_orangepi_update_count.py` sau khi chép script lên device. Helper chạy lại không đổi thêm, bỏ qua dòng thiếu/đã tùy chỉnh và lưu bản gốc tại `/var/backups/autonomous/orangepi-updates.before-boot-delay`. Chép bản gốc về `/etc/cron.d/orangepi-updates` để hoàn tác. Đây là thay đổi cấu hình image/device, không nằm trong OTA chỉ cập nhật HAL.

Build image OrangePi dùng `golden-opi.img.building`, từ chối source lớn hơn
`OUT_IMG_SIZE` và chạy `e2fsck -fp` có ghi trước khi resize (chỉ chấp nhận mã 0/1).
Sau khi unmount cuối cùng, `e2fsck -fn` chỉ đọc bắt buộc trả mã 0.
Cả hai loop device được detach trước khi công bố raw image
bằng đổi tên nguyên tử; bước nén cũng ghi file staging rồi mới đổi tên. Build lỗi
có thể để lại artifact hoàn tất cũ; `COMPRESS=0` không cập nhật image nén đã có.
Khi flash cần chọn đúng bản build mong muốn.

Với flash raw, `make sd-card-flash-raw DEVICE_TYPE=lamp DISK=17 VERIFY=0` bỏ bước đọc lại đối chiếu và yêu cầu Python của bước này; vẫn sync và eject. Mặc định vẫn bật verify.

Cả hai target flash trong Makefile cần Python 3 và dùng
`scripts/imager/lib/verify_flash.py` để đọc lại SD, đối chiếu toàn bộ byte của image
trước khi báo thành công/eject. Flash image nén dùng Bash `pipefail` để lỗi giải nén
làm target thất bại; dữ liệu khác biệt hoặc đọc thiếu cũng gây thất bại. Kiểm tra
ext4 đánh giá cấu trúc filesystem; đọc lại kiểm tra byte đã ghi. Hai phép kiểm tra
không bảo đảm boot thành công và không tự xác định nguyên nhân hỏng filesystem.
Xem [quy trình imager](../../scripts/imager/README.md#flashing-an-sd-card).

## 1. Tổng Quan

Thiết bị chạy **5 thành phần phần mềm** trên board được hỗ trợ (Raspberry Pi 4, Pi 5, hoặc OrangePi). Tất cả được cài đặt qua script setup ban đầu và cập nhật tự động qua OTA worker chạy nền.

| Thành phần | Loại | Cách cài | Service | Đường dẫn |
|---|---|---|---|---|
| **OS Server** | Go binary (ARM64) | Tải zip từ OTA | `os-server.service` | `/usr/local/bin/os-server` |
| **Bootstrap Server** | Go binary (ARM64) | Tải zip từ OTA | `bootstrap.service` | `/usr/local/bin/bootstrap-server` |
| **Web (Setup SPA)** | React/Vite | Tải zip từ OTA | nginx serve static | `/usr/share/nginx/html/setup/` |
| **OpenClaw** | Node.js package | `npm install -g` | `openclaw.service` | Global npm |
| **HAL** | Python package | Tải zip từ OTA | `hal.service` | `/opt/hal/` |

HAL hỗ trợ CPython **3.12.x** (`requires-python = ">=3.12,<3.13"`).
`uv sync --python 3.12` chọn interpreter nhưng vẫn resolve toàn bộ dải Python
và optional extras mà project khai báo. Dải `>=3.12` không có cận trên có thể làm
OTA ARM64 thất bại ở nhánh Python 3.14 (LeRobot đã pin cần Torch <2.8, không có
wheel phù hợp cho nhánh đó). Giữ dải Python và `hal/uv.lock` đồng bộ; resolve lỗi
ở staging sẽ khôi phục HAL cũ.

Gói HAL mang theo `uv.lock`; script release chạy
`uv lock --python 3.12 --check` trước khi tăng version hoặc upload. Updater dùng
`uv sync --locked` khi archive có lockfile; archive cũ không có lock giữ đường
resolve cũ. Loại lock khỏi ZIP buộc device resolve lại từ đầu: với uv 0.12.15,
HAL 0.1.169 lỗi ở nhánh Python >=3.13 ARM64/Reachy dù project đã giới hạn 3.12;
cùng archive thêm lock đã kiểm tra thì dry-run cài đặt locked trên device đạt.

Các trình build image OrangePi, Pi 4 và Pi 5 cũng dùng `--locked` khi gói HAL
tải về có `uv.lock`. HAL được lấy từ OTA metadata, không phải checkout local;
chạy lại build sau khi phát hành gói đã sửa.

Setup lamp và image Pi/OrangePi cài HAL với `--extra hardware --extra aec
--extra pipecat`. OTA cho thiết bị không phải Reachy chọn cùng các extra này,
nên Smart Turn được cài tự động, không cần lệnh riêng trên thiết bị. Reachy giữ
`--extra hardware --extra reachy`: yêu cầu ONNX runtime của nó xung đột với
Pipecat. Extra `pipecat` vẫn tùy chọn khi developer chạy riêng `uv sync`.

### Sơ đồ hệ thống

```
                    ┌──────────────────────────────┐
                    │   OTA Metadata (GCS JSON)     │
                    │                                │
                    │  os-server: {version, url}     │
                    │  bootstrap: {version, url}     │
                    │  web:       {version, url}     │
                    │  openclaw:  {version}          │
                    │  hal:       {version, url}     │
                    └───────────────┬────────────────┘
                                    │ poll mỗi 5 phút
                                    ▼
┌───────────────────────────────────────────────────────────────────┐
│                    Bootstrap Server (Go, port 8080)               │
│                                                                   │
│  checkLoop() → với mỗi thành phần:                               │
│    1. Phát hiện version hiện tại đang cài                        │
│    2. So sánh với version mục tiêu trong OTA metadata            │
│    3. Nếu khác → applyUpdate()                                   │
│       → tải zip / npm install                                     │
│       → giải nén vào đường dẫn cài đặt                           │
│       → systemctl restart {service}                               │
│    4. Lưu trạng thái vào /root/bootstrap/state.json              │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. OTA Metadata

File JSON duy nhất trên GCS. Tất cả thành phần tham chiếu file này.

> Trong các URL bên dưới, `{BUCKET}` và `{PREFIX}` là bucket + namespace path:
> `GCS_BUCKET` (mặc định `s3-autonomous-upgrade-3`) và `BUCKET_PREFIX` (mặc định
> `os`), đều set trong `scripts/release/ota-config.sh`. Upload scripts đọc từ đó; consumer
> trên device derive cùng path từ `ota_metadata_url` đã provisioning.

**URL**: `https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json`

Tài liệu publish vẫn giữ component legacy ở top level và thêm envelope Ed25519
`signed` theo `autonomous-ota/v1`. `signed.payload` là JSON base64;
`signed.signature.value` ký đúng bytes payload sau khi decode. Public key 32
byte dạng base64 được provision cục bộ trong `bootstrap.json`, không bao giờ
lấy từ feed. Payload đã decode có dạng:

```json
{
  "os-server": {
    "version": "1.2.3",
    "min_version": "1.2.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/os-server/1.2.3.zip",
    "sha256": "<64 ký tự hex thường>"
  },
  "bootstrap": {
    "version": "1.0.5",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/bootstrap/1.0.5.zip"
  },
  "web": {
    "version": "0.9.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/web/0.9.0.zip"
  },
  "openclaw": {
    "version": "2026.6.10"
  },
  "hal": {
    "version": "1.0.0",
    "url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/hal/1.0.0.zip"
  }
}
```

### Tạo key ký OTA

Chạy một lần trên máy của release operator:

```bash
make ota-keygen
```

Mặc định lệnh tạo private PEM Ed25519 ở ngoài repo, tại
`~/.config/autonomous/ota/ota-YYYYMMDD.pem`, rồi in ba dòng `export`. Giữ kín
`OTA_SIGNING_PRIVATE_KEY`; dùng nó cùng `OTA_SIGNING_KEY_ID` khi chạy
`make upload-*`. Provision `OTA_SIGNING_PUBLIC_KEY` được in ra cho device mới.
Có thể đổi thư mục hoặc ID:

```bash
make ota-keygen OTA_SIGNING_KEY_DIR=/secure/ota-keys OTA_SIGNING_KEY_ID=prod-2026-08
```

**Domain types** — `system/domain/ota.go`:

```go
const (
    OTAKeyOSServer  = "os-server"
    OTAKeyBootstrap = "bootstrap"
    OTAKeyWeb       = "web"
    OTAKeyOpenClaw  = "openclaw"
    // Agent-runtime CLIs. Each value is also the runtime name in config.json
    // `agent_runtime` — that equality is how bootstrap updates only the CLI the
    // device actually runs. Hermes rides along only when its entry is
    // commit-pinned (OTAComponent.Commit) — see OTAKeyHermes.
    OTAKeyHermes     = "hermes"
    OTAKeyCodex      = "codex"
    OTAKeyClaudeCode = "claudecode"
    OTAKeyOpenCode   = "opencode"
    OTAKeyPicoClaw   = "picoclaw"
    // OTAKeyLeLamp's value is "hal" — the HAL OTA metadata key
)

type OTAMetadata map[string]OTAComponent

type OTAComponent struct {
    Version    string `json:"version"`
    MinVersion string `json:"min_version,omitempty"`
    URL        string `json:"url,omitempty"`
    SHA256     string `json:"sha256,omitempty"`
}
```

### Staged rollout — `version` vs `min_version`

`version` là bản mới nhất; `min_version` là **sàn đã duyệt** mà worker tự động
đẩy cả fleet lên tới. Hai trường tách bạch "đã publish" và "đã auto-push":

- **Auto OTA (bootstrap worker)** chỉ cập nhật thiết bị khi version hiện tại
  **thấp hơn hẳn `min_version`**. Nếu thiếu `min_version` thì mặc định bằng
  `version` (worker bám latest — hành vi cũ).
- **`software-update <key>` thủ công** (qua SSH) bỏ qua `min_version`, luôn cài
  `version` — để test bản mới trên vài thiết bị trước.

Quy trình:

1. `scripts/release/upload-<component>.sh` bump `version` và **giữ nguyên**
   `min_version` (skills/hooks không có `min_version`). Fleet **không** đổi —
   chỉ `version` thay đổi.
2. SSH vào thiết bị, chạy `software-update <key>` → kéo `version`. Test.
3. Ổn? `make promote-<component> [V=<version>]` (vd `make promote-hal`,
   `make promote-os-server V=1.4.0`, `make promote-device DT=lamp`) nâng
   `min_version` (mặc định = `version`). Bootstrap sẽ tự cập nhật mọi thiết bị
   dưới sàn mới ở lần check kế tiếp.

So sánh version theo số trên từng đoạn (`bootstrap.compareVersions`):
`2026.6.10 > 2026.5.9`; bỏ qua hậu tố pre-release/build; version hiện tại rỗng
hoặc không parse được xem là thấp nhất (luôn dưới mọi sàn → cập nhật).

---

## 3. Setup Ban Đầu (`scripts/provision/setup.sh`)

Script chạy **1 lần duy nhất** trên Pi mới. Thực thi tuần tự theo stages.

**Cài nhanh từ CDN:**
```bash
curl -fsSL https://cdn.autonomous.ai/os/install.sh | sudo bash
```

### Tổng quan stages

| Stage | Tên | Mô tả |
|---|---|---|
| -1 | Locale fix | Đảm bảo encoding `C.UTF-8` |
| 0 | Prerequisites | Packages hệ thống, Node.js 22 |
| 0a | WiFi stability | Tắt IPv6, WiFi power saving (RPi5) |
| 0b | Bật SPI + I2C | Cho WS2812 LED driver + GC9A01 display; bus I2C cho ngoại vi |
| 1 | Fetch OTA metadata | Tải metadata.json, trích xuất versions và URLs |
| 1b | Install binaries | Tải + cài os-server, bootstrap-server, tạo systemd services |
| 2 | Install OpenClaw | `npm install -g openclaw`, tạo config, systemd service |
| **2b** | **Install HAL** | **Tải + cài HAL Python runtime, tạo systemd service** (MỚI) |
| 3 | Setup nginx | Tải web bundle, cấu hình reverse proxy + captive portal |
| 4 | Setup WiFi AP | Cấu hình hostapd, dnsmasq, bật AP mode cho provisioning |

### Stage 2b: Cài HAL Runtime (`stage_hal`)

```bash
stage_hal() {
    HAL_DIR="/opt/hal"
    mkdir -p "$HAL_DIR"

    # 1. Tải zip theo metadata .hal.url (đối chiếu .hal.sha256)
    curl -fsSL -o /tmp/hal.zip "$HAL_URL"
    unzip -o -q /tmp/hal.zip -d "$HAL_DIR"

    # 2. Thư viện audio, PulseAudio AEC + socket anonymous, udev rule giữ PulseAudio tránh codec loa

    # 3. Tạo lại venv bằng uv (tự tải Python 3.12 standalone)
    rm -rf "$HAL_DIR/.venv"
    cd "$HAL_DIR" && uv sync --python 3.12 --extra hardware --extra aec --extra pipecat

    # 4. Ghi $HAL_DIR/.env (HAL_MODE=production, DEVICE_TYPE, DEVICES_DIR) nếu chưa có

    # 5. systemd unit
    cat >/etc/systemd/system/hal.service <<EOF
[Service]
WorkingDirectory=$HAL_DIR
EnvironmentFile=$HAL_DIR/.env
Environment="PYTHONPATH=/opt"
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
Restart=always
...
EOF
    systemctl daemon-reload && systemctl enable hal && systemctl restart hal
}
```

### Systemd Services trên thiết bị

| Service | Lệnh chạy | Port | Ghi chú |
|---|---|---|---|
| `os-server.service` | `/usr/local/bin/os-server` | 5000 | HTTP API chính, luôn chạy |
| `bootstrap.service` | `/usr/local/bin/bootstrap-server` | 8080 | OTA worker, poll cập nhật. Expose `POST /force-check` để kích hoạt kiểm tra OTA ngay lập tức và `GET /security` cho trạng thái tin cậy OTA |
| `openclaw.service` | `xvfb-run ... openclaw gateway run` | — | AI brain, memory limit 1500M |
| `hal.service` | `uvicorn hal.server:app --host 127.0.0.1 --port 5001` | 5001 | Hardware drivers (servo, LED, camera, audio) |
| nginx | `nginx` | 80 | Setup SPA + reverse proxy (`/api/` → OS Server 5000, `/hw/` → HAL 5001) |

### Thứ tự khởi động

```
boot
  → os-server.service   (tầng hệ thống, LED boot animation)
  → bootstrap.service   (bắt đầu poll cập nhật)
  → hal.service          (hardware drivers sẵn sàng)
  → openclaw.service    (AI brain, kết nối os-server qua HTTP)
  → nginx               (web UI cho setup)
```

---

## 4. Bootstrap OTA Worker

### Config (`/root/config/bootstrap.json`)

Bootstrap worker giữ file config riêng, tách khỏi `config.json` của os-server,
nhưng nằm cùng thư mục `/root/config/`.

```json
{
  "httpPort": 8080,
  "metadata_url": "https://storage.googleapis.com/{BUCKET}/{PREFIX}/ota/metadata.json",
  "signing_public_key": "<Ed25519 public key 32-byte base64>",
  "rollback_versions": {"os-server": "1.2.3"},
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
```

`metadata_url` **không có default hardcode** — đây là giá trị tùy theo deployment,
được `setup.sh` (`stage_ota_metadata`) seed vào file này lúc provisioning. File
được load dạng overlay lên các default vận hành (`httpPort` 8080, `poll_interval`
5m, `state_file`), nên file một phần (chỉ có `metadata_url`) vẫn chạy được, và
file thiếu thì dùng default với URL rỗng.

Khi được truyền vào, `setup.sh` và image builder lưu `OTA_SIGNING_PUBLIC_KEY`
thành `signing_public_key`. Khi đó Bootstrap xác thực envelope trước khi đọc
component; updater được provision xác thực lần nữa và hash mọi ZIP trước khi
giải nén. Khi release operator truyền `OTA_SIGNING_PRIVATE_KEY` và
`OTA_SIGNING_KEY_ID`, release writer ký lại envelope; thiếu chúng thì vẫn giữ
format legacy unsigned.
Với hai binary tự chứa, mỗi lần update giữ
`/root/bootstrap/rollback/<component>.previous`; chạy
`software-update rollback os-server` hoặc `software-update rollback bootstrap`
để khôi phục. Updater ghi version vừa gỡ vào `rollback_versions`; bootstrap chỉ
bỏ qua đúng target đó nên release lỗi không bị cài lại ở lần poll sau. Khi feed
có version khác, OTA của component đó tự tiếp tục. Bản thân rollback không cần
metadata URL hoặc mạng. Bootstrap đọc lại `rollback_versions` trước mỗi lần
reconcile component, nên rollback HAL/web/device thủ công có hiệu lực mà không
cần restart bootstrap. Rules không đọc hoặc parse được sẽ dừng lần reconcile đó.
Nếu reconcile HAL hoặc os-server thất bại, chu kỳ tự động bỏ qua device profile
để tránh áp khai báo cần phiên bản core chưa cập nhật thành công.

Trước khi update `os-server`, `web` hoặc `device`, updater bảo đảm nginx có route
WebSocket Harness. Nó tìm trong `/etc/nginx/conf.d/*.conf` và mọi entry của
`/etc/nginx/sites-enabled/*`, gồm `reachy-spike` trên Reachy. Route dùng lại HTTP
upstream hiện có của `/api/` (`backend` hoặc `spike_backend`); cấu trúc proxy
không hỗ trợ hoặc có rewrite URI bị từ chối. Site dạng symlink được sửa tại
file đích thực, giữ nguyên link enabled. Updater kiểm tra `nginx -t` và chỉ
reload nginx sau khi thêm route.

Để vá thủ công image cũ đã có `/api/buddy/ws`, chạy
`scripts/maintenance/patch-nginx-harness-ws.sh` bằng root trên device. Script
resolve site dạng symlink trước khi sửa, lưu bản sao riêng của file gốc tại
`/var/backups/nginx-harness-ws/`, và khôi phục file đó nếu `nginx -t` thất bại,
giữ nguyên symlink enabled. Nếu đã có route Harness thì không thay đổi.

Các component cài theo thư mục cũng có cùng hợp đồng recovery. Trước khi update
web, updater dừng nginx, swap bundle đã giải nén hoàn chỉnh từ thư mục staging,
và giữ bundle trước đó tại `/root/bootstrap/rollback/web.previous` cùng trạng
thái active/inactive trước đó của nginx. Sau đó nó đòi hỏi có `index.html`,
`nginx -t` hợp lệ và—khi nginx vốn active hoặc đang enabled—service active trước
và sau khi `GET /` loopback thành công. Service bắt buộc chạy nhưng đã dừng hoặc
failed sẽ làm update thất bại, không được bỏ qua health check. Nếu thất bại, updater tự khôi phục bundle và trạng thái service đã lưu. Operator cũng
có thể chạy `software-update rollback web`; version bị loại được ghi vào
`rollback_versions` giống rollback binary.

Với device profile, updater stage ZIP, dừng `os-server` và `hal`, rồi giữ profile cũ tại `/root/bootstrap/rollback/device.previous`. Nó
cũng snapshot chính xác các file thuộc `rootfs/` của profile cũ hoặc mới trong
`device.previous.rootfs`; rollback vì vậy khôi phục file bị ghi đè và xoá file
chỉ được profile lỗi thêm vào. OTA thành công thay thế `.env` HAL được sinh.
Profile bắt buộc có `ROBOT.md`; mỗi service vốn active hoặc đang enabled phải
active trước và sau probe health loopback. HAL hoặc os-server lỗi đều làm update
profile thất bại. Check lỗi sẽ tự phục hồi profile trước đó và trạng
thái service cũ. Dùng `software-update rollback device` khi operator rollback;
version profile bị loại sau đó sẽ bị chặn.

### Override phần cứng tùy chọn

OS đọc tên trên một dòng trong `/etc/autonomous/hardware-profile`. Thiếu file,
rỗng hoặc `standard` chọn package device gốc mới giải nén, không áp override:
không thêm kiểm tra USB, không đổi mặc định audio/Live của máy cũ. Tên khác phải khớp
`[a-z][a-z0-9_-]{0,63}`, chọn `overrides/<tên>/` bên trong package của device đó.
File định danh thuộc máy, không được đóng gói trong overlay.

`scripts/provision/apply-overrides.py` được đưa vào mọi ZIP device khi release.
Helper merge `rootfs/opt/hal/.env` của override lên env chung, copy các file
`rootfs/` khác vào rootfs staging, rồi áp hai trường số nguyên tùy chọn
`startup_volume`/`max_volume` từ `profile.json` vào `ROBOT.md`/`SAFETY.md`.
Map `capabilities` tùy chọn nhận giá trị boolean, bật hoặc comment các entry
capability sẵn có trong frontmatter `ROBOT.md`; không tự tạo khai báo mới.
Các file `device/*.json` được chọn thay toàn bộ JSON device cấp cao nhất đã
tồn tại, không merge nội dung. Renderer kiểm tra các input này trước khi ghi
và từ chối capability hay file thay thế không hợp lệ. Giá trị riêng sản phẩm
chỉ nằm trong package device. Ví dụ:

Gain loa C-Media của Lamp standard được cố định ở mức mixer **9 (-28 dB)**,
tăng 3 dB so với mức 6 trước đây. Override rootfs `pro` giữ **6 (-31 dB)**;
các profile ReSpeaker giữ đường audio riêng. Slider softvol (-30..0 dB) và
volume user đã lưu không đổi. Cấu hình nằm trong gói device/rootfs; mức gain
được áp dụng khi `lamp-cmedia-speaker.service` chạy lại (ví dụ reboot), không
phải qua update riêng HAL.

| Profile | Mic hội thoại | Mic sensing | Loa | Âm lượng khởi động / tối đa | SEN63C |
|---|---|---|---|---|---|
| `standard` | Jieli `device_micro2` | CMedia `device_cmedia` | Loa gốc | Mặc định gốc | Tắt |
| `pro` | Jieli `device_micro2` | CMedia `device_cmedia` | Loa gốc | Mặc định gốc | Bật trên `orangepi_sun60`, bus `0` |
| `pro-respeaker-lite` | Kênh trái đã xử lý của ReSpeaker Lite | ES8389 trên board (`sndi2s4`) | ReSpeaker Lite softvol `Speaker` | 35 / 35 | Bật trên `orangepi_sun60`, bus `0` |
| `pro-xvf3800` | XVF3800 (`Array`) | ES8389 trên board (`sndi2s4`) | XVF3800 | 77 / 77 | Bật trên `orangepi_sun60`, bus `0` |

Mỗi thư mục Pro có `profile.json` chứa
`capabilities: {"environment": true}`, cùng `device/sen63c.json` bật sensor
OrangePi. Chỉ hai bản Lite và XVF3800 ghi đè mặc định âm lượng. Package Standard gốc comment capability này và tắt SEN63C, nên
không thu nhận, ghi clock bus cho SEN63C, polling UI môi trường hay đủ điều
kiện chọn skill environment. SEN55/SCD41 và board thiếu entry tương ứng vẫn
tắt ngay cả trên Pro.

`pro-respeaker-lite` giữ nguyên toàn bộ overlay `pro` trước đây, gồm file ALSA,
udev và service softvol: bật AEC HAL, ngưỡng Silero `0.10`, tắt Live, uplink
luôn mở. `pro` chỉ là audio Standard thêm SEN63C: không có override `rootfs/`,
`.env`, ALSA, udev, service hay âm lượng. Cấu hình ALSA và giới hạn safety
sau render giống Standard từng byte; mic, loa, xử lý và mặc định âm lượng
đều dùng package gốc. Renderer chung chỉ thêm namespace lưu âm lượng của
profile (`HAL_VOLUME_STATE_PATH=/root/config/.volume-pro`) vào môi trường HAL được sinh. `pro-xvf3800` giữ
XMOS AEC, tắt AEC phần mềm, bật Live và uplink luôn mở.

ReSpeaker Lite (XMOS XU316, USB `2886:0019`, card ALSA `Lite`) dùng audio
S16_LE 2 kênh 16 kHz. `pro-respeaker-lite` cần nối loa qua Lite.
Card không có mixer ALSA; oneshot do udev kích mở PCM trước `hal.service`
để tạo control softvol cho bước khôi phục âm lượng lúc boot. Mức 35% được
chỉnh trên bộ Lite, không phải mức âm lượng tương đương đã hiệu chuẩn giữa
các thiết bị. Standard giữ mặc định audio và ceiling. Renderer không dò,
flash hoặc tune phần cứng được gắn.

Máy `pro` hiện có dùng mic Lite phải chọn rõ `pro-respeaker-lite` trước khi
cài package device mới để giữ toàn bộ cấu hình audio. Không tự chuyển marker. Chỉ đổi
marker không thay file đã cài; cần cài lại gói device.

Image builder/cài mới áp override trước khi cài rootfs. OTA bắt đầu từ
archive gốc mới giải nén, không từ cây Pro đã render, nên chuyển về Standard
sẽ khôi phục mặc định tắt sensor và capability. OTA render trước khi
dừng service và snapshot rootfs; thiếu helper/profile hoặc render lỗi không
đụng package đang chạy. Lỗi copy hay health check rollback đồng bộ profile và
rootfs thực tế. Rollback không đổi lựa chọn hardware-profile của máy.

Với image mới, chọn phần cứng lúc build:

```bash
make -C scripts/imager build TARGET=opi DEVICE_TYPE=lamp VARIANT=pro OTA_METADATA_URL=...
```

`VARIANT` là input build duy nhất để chọn phần cứng. Bước overlay ghi
`/etc/autonomous/hardware-profile` trước khi áp dụng `overrides/pro`; OTA đọc lại
file này. Bỏ trống hoặc `VARIANT=standard` sẽ xóa lựa chọn cũ trong image và dùng
nguyên package mặc định. Lựa chọn không nằm trong base cache dùng lại. Variant
khác standard được thêm vào tên image/release cuối cùng. Variant không tồn tại
hoặc package thiếu helper override sẽ làm build thất bại.

Máy thiếu marker hardware-profile vẫn là Standard, kể cả máy từng nhận mặc
định bật SEN63C chung; không tự chuyển sang Pro. Để giữ SEN63C trên phần cứng
Pro, chọn rõ `pro`, `pro-respeaker-lite` hoặc `pro-xvf3800` rồi cài lại gói device. Nâng cấp từ bản
cũ cần cả HAL (sửa clock I2C) lẫn gói device (capability và cấu hình sensor).
Chỉ cập nhật HAL không cài các mặc định profile này.

Khi sản xuất, ghi tên profile trước khi cài. Với máy đang chạy, cài updater và
HAL/os-server mới trước, sau đó:

```sh
sudo mkdir -p /etc/autonomous
printf 'pro\n' | sudo tee /etc/autonomous/hardware-profile
sudo software-update device
```

Kiểm tra cập nhật thành công trước khi dùng. Nếu chuyển đổi lỗi, phục hồi lựa
chọn hardware-profile cũ cho khớp package vừa rollback. Muốn về phần cứng cũ,
xóa lựa chọn rồi OTA package mới; chỉ xóa file không hoàn tác overlay đã render.
Updater cũ chưa biết áp override: bootstrap tự động refresh updater trước,
nhưng cập nhật thủ công hoặc refresh thất bại phải cài updater mới trước khi
chọn profile.

Mọi profile được chọn lưu volume riêng trong `config/.volume-<tên>`
(HAL và os-server thống nhất), kể cả `pro` chỉ override capability/JSON device.
Renderer chung sinh `HAL_VOLUME_STATE_PATH` tương ứng; thiếu lựa chọn hoặc
Standard giữ `config/.volume`. Nhờ vậy mức phần trăm của loa
cũ không ghi đè volume khởi động của bộ mới. HAL chỉnh đúng cả `PCM,0` và
`PCM,1`, không chỉnh control capture; ghi mixer lỗi trả 503 thay vì lưu thành
công giả. UI, lệnh giọng nói và volume khởi động vẫn qua ceiling đã chọn.
Không thêm UI Live hay thay đổi nói chen trong TTS delegate. Sửa trực tiếp `.env`
được sinh vẫn bị device OTA thành công ghi đè như trước.

Device không có `signing_public_key` chủ ý ở legacy mode: nó đọc component top
level và chỉ log cảnh báo, không làm OTA lỗi. Đây là compatibility bridge, không
phải bảo đảm trust; pin public key để bật verified OTA.

### Trạng thái bảo mật OTA (operator nhìn thấy được)

Cảnh báo ở trên chỉ là một dòng log, nên một device kẹt ở legacy mode là vô hình
với người vận hành fleet. Cả hai tiến trình đều expose trạng thái đó dưới dạng dữ liệu:

| Endpoint | Do ai phục vụ | Ghi chú |
|----------|---------------|---------|
| `GET http://127.0.0.1:8080/security` | `bootstrap-server` | Nguồn sự thật: worker giữ key đã pin và thực hiện verify. Chỉ loopback. |
| `GET /api/system/ota-security` | `os-server` | Proxy nguyên văn endpoint trên, bọc trong wrapper chuẩn `{"status":1,"data":…}`. Trả `502` khi bootstrap worker không với tới được. |

```json
{
  "mode": "verified",
  "metadata_format": "autonomous-ota/v1",
  "key_fingerprint": "9f2c1ab30d4e5f60",
  "artifact_checksums": true,
  "last_metadata_fetch": {
    "at": "2026-08-24T09:12:03Z",
    "verified": true
  }
}
```

- `mode` là `verified` khi đã provision `signing_public_key`, ngược lại là
  `legacy`. Đây là field duy nhất cần cảnh báo trên toàn fleet.
- `key_fingerprint` là 16 ký tự hex đầu của SHA-256 của key đã pin, để operator
  biết device đang tin **key nào** (và phát hiện device còn kẹt ở key đã xoay
  vòng) mà endpoint không phải phát lại key material. Không có ở legacy mode.
- `artifact_checksums` cho biết SHA-256 từng component có được enforce không. Nó
  đi theo `mode`: digest chỉ có ý nghĩa khi metadata mang nó là xác thực.
- `last_metadata_fetch` là kết quả lần fetch gần nhất, gồm cả lỗi transport
  (`error` được set, `verified` là `false`). Nó vắng mặt cho tới khi lần fetch
  đầu tiên sau restart hoàn tất, nên device chưa từng với tới feed phân biệt
  được với device có feed khoẻ mạnh.

### Cutover sang signed-only

Metadata ký được publish dạng superset: component entry của payload vẫn nằm ở
top level cho các worker đã deploy, còn tài liệu xác thực nằm dưới `signed`. Bản
copy tương thích đó chính là thứ giữ cho legacy mode còn khai thác được, nên
phải gỡ. Lộ trình:

1. **Publish có ký** (đã xong). Release writer thêm envelope `signed` mỗi khi có
   `OTA_SIGNING_PRIVATE_KEY` và `OTA_SIGNING_KEY_ID`. Device bỏ qua nó tới khi
   được pin key.
2. **Provision key.** Device mới nhận `OTA_SIGNING_PUBLIC_KEY` lúc setup; fleet
   hiện có thì ghi vào `/root/config/bootstrap.json`. Không cần redeploy —
   worker đọc key ở lần load config kế tiếp.
3. **Xác nhận toàn fleet.** Poll `GET /api/system/ota-security` và yêu cầu
   `mode == "verified"` cùng đúng `key_fingerprint` trên mọi device. Bước này là
   cổng chặn: còn device nào báo `legacy` thì không đi tiếp.
4. **Cutover.** Publish với `OTA_METADATA_SIGNED_ONLY=1`, cờ này bỏ bản copy top
   level nên tài liệu chỉ còn `signed`. Device chưa migrate sẽ dừng update (và
   kêu to) thay vì update từ nguồn không xác thực — đúng hướng lỗi mong muốn.

Rollback cho bước 4 là publish lại một lần nữa mà không bật cờ; device đã ở
verified mode không bị ảnh hưởng theo hướng nào, vì chúng chỉ đọc `signed`.

**Đợi-rồi-retry khi chưa provisioning**: nếu `metadata_url` rỗng (device chưa
setup), `Serve()` không khởi động poll loop lẫn healthcheck server. Nó log
`waiting for metadata_url in bootstrap config` và reload
`/root/config/bootstrap.json` mỗi 30s tới khi có URL rồi mới chạy tiếp. Không có
gì silent.

**Gửi log**: bootstrap ghi log qua logger dùng chung với
`_service_name: "bootstrap"` (trước đây bị ghi là `os-server`). Bootstrap không có
tín hiệu thay đổi config, nên `RunLogRelay` (`system/bootstrap/log_relay.go`) đọc
lại `/root/config/config.json` mỗi phút và bật relay bằng key Autonomous của thiết
bị (cùng quy tắc với os-server). Khi chưa có key — thiết bị mới, hoặc OTA chạy trong
lúc setup — record nằm chờ trong spool (`/var/lib/autonomous/gelf-spool/bootstrap.jsonl`) và
được replay khi gửi được. Xem [setup-flow_vi.md](setup-flow_vi.md).

### State (`/root/bootstrap/state.json`)

Lưu version đã cài của mỗi thành phần:

```json
{
  "components": {
    "os-server": "1.2.3",
    "bootstrap": "1.0.5",
    "web": "0.9.0",
    "openclaw": "2026.6.10",
    "hal": "1.0.0"
  }
}
```

Việc lưu state là atomic: bootstrap ghi và sync file tạm trong cùng thư mục rồi
mới rename vào vị trí chính thức. Nếu state cũ bị lỗi định dạng (ví dụ do lần ghi
legacy bị gián đoạn), bootstrap giữ lại thành `state.json.corrupt-<timestamp>`,
log cảnh báo và tiếp tục với state rỗng thay vì không khởi động OTA polling.

### Luồng xử lý chính (`system/bootstrap/bootstrap.go`)

```
checkLoop():
  1. checkOnce() ngay khi khởi động
  2. Sleep poll_interval (mặc định 5 phút)
  3. Lặp lại

checkOnce():
  1. Tải OTA metadata JSON
  2. Với mỗi key [os-server, bootstrap, web, hal, buddy, openclaw, codex,
     claudecode, opencode, picoclaw, hermes]:
     → reconcile(key, metadata[key])
     (hermes chỉ khi entry có `commit` — hermesPinned)
  3. reconcileDevice() cho entry lồng devices.<device_type>
  4. Lưu state (chỉ khi có thay đổi)

reconcile(key, target):
  1. Phát hiện version hiện tại đã cài
  2. Nếu current == "" VÀ component không được cài → bỏ qua im lặng
  3. floor = target.min_version (mặc định target.version nếu rỗng)
  4. Nếu current >= floor → đồng bộ state, return (đã ở/trên sàn duyệt)
  5. Nếu current < floor →
     a. Bật LED cam breathing (đang update)
     b. applyUpdate(key, target)   # cài target.version qua software-update
     c. Thành công → flash xanh lá rồi khôi phục LED | Thất bại → đỏ pulse 10 giây rồi khôi phục LED
```

> `software-update <key>` thủ công qua SSH KHÔNG đi qua `reconcile` — nó cài
> thẳng `target.version`, bỏ qua sàn `min_version`.

#### Bước 2 — component thiết bị này không có

Metadata liệt kê mọi component đã publish; không thiết bị nào chạy đủ hết. Reachy
Mini không có `claude-desktop-buddy`; thiết bị chạy runtime khác OpenClaw thì
không có `openclaw`. Với mấy cái đó `detectVersion` trả `""`, mà `""` xếp dưới
mọi sàn — nên nếu không chặn, worker sẽ báo "update available", phát câu thông
báo qua loa, bật LED cam rồi cài thất bại **mỗi lần poll, vĩnh viễn**.

`componentInstalled(key)` hỏi câu thô hơn `detectVersion`: *artifact có tồn tại
không*, chứ không phải *version nào*. Component có mặt nhưng đọc không được vẫn
tính là đã cài, nên OTA vẫn tự chữa được (binary `os-server` hỏng `--version` vẫn
được update); chỉ cái thực sự vắng mặt mới bị bỏ qua.

| Component | Tính là đã cài khi |
|---|---|
| `bootstrap` | luôn luôn (chính là worker đang chạy, để nó tự update được) |
| `os-server` | `os-server` có trong `$PATH` |
| `openclaw` | `openclaw` có trong `$PATH` |
| `web` | tồn tại `/usr/share/nginx/html/setup/` |
| `hal` | tồn tại `/opt/hal/` |
| `claude-desktop-buddy` | tồn tại `/opt/claude-desktop-buddy/` |
| `device` | tồn tại `$DEVICES_DIR/<type>/` (mặc định `/opt/devices`) |

### OTA LED Feedback

Bootstrap dùng `lib/hal` để báo trạng thái update qua LED. Xem chi tiết: [status-led_vi.md](../../robots/lamp/docs/vi/status-led_vi.md).

| Giai đoạn | LED |
|----------|-----|
| Đang tải + cài | Cam breathing `[16, 8, 0]` |
| Thành công | Flash xanh lá `[0, 12, 4]` trong 1 giây, rồi khôi phục LED look user đã chọn hoặc ambient resting look nếu chưa có user state |
| Thất bại | Đỏ pulse `[16, 2, 2]` trong 10 giây, rồi khôi phục LED look user đã chọn hoặc ambient resting look nếu chưa có user state |

### Bất biến mà updater phải giữ (trả giá mới biết)

Một máy mất sạch `/opt/hal`, thư mục staging VÀ bản backup rollback chỉ vì bấm
nút update trên web 2 lần cách nhau 30 giây. Bốn lỗi xếp chồng; cả bốn đã vá
trong `scripts/provision/software-update`, và đều rất dễ tái phạm:

1. **Mỗi lúc chỉ một lần chạy** (`flock` trên `/var/lock/software-update.lock`).
   Mọi nhánh đều publish bằng cách `mv` cây đang chạy sang `<name>.previous`, nên
   lần chạy thứ hai `rm -rf` mất backup duy nhất của lần thứ nhất rồi chết vì
   không còn nguồn. Rate-limit 30 giây/target ở os-server KHÔNG chặn được.
2. **Thiếu thư mục cài đặt là REINSTALL, không phải lỗi.** `mv "$HAL_DIR"` abort
   khi `/opt/hal` vắng nghĩa là lệnh duy nhất có thể cứu máy lại từ chối chạy.
3. **Publish theo đúng mode của cây đang sống** (`publish_mode`). `mktemp -d` cho
   0700; `mv` thư mục đó lên `/usr/share/nginx/html/setup` làm nginx (www-data)
   trả 403 cho mọi request → health check fail → update tự rollback, mãi mãi,
   trên mọi máy.
4. **Virtualenv không relocatable** (`relocate_venv_scripts`). Build `.venv`
   trong staging làm mọi console script mang shebang
   `#!/opt/.hal.new.XXXXXX/.venv/bin/python`; staging biến mất là unit chết
   `203/EXEC`. Không lộ khi kế thừa `.venv` cũ — chỉ cắn đúng lúc cài mới, tức
   đúng đường khôi phục.
5. **Phản hồi lỗi phải tạm thời.** OTA fail có thể pulse đỏ để báo lỗi, nhưng
   sau 10 giây phải khôi phục LED look user đã chọn hoặc ambient resting look;
   phản hồi lỗi không được giữ strip ở đỏ.

Cộng thêm một lỗi làm mọi thứ trên khó thấy: **trạng thái service được chụp ở đầu
mỗi lần chạy**, nên lần chạy bắt đầu khi update trước đó đang fail sẽ ghi
"inactive", và mọi update sau khôi phục trung thành trạng thái "chết" đó.
`unit_wanted_active` giờ coi unit còn `enabled` trong systemd là "phải chạy", và
`check_web`/`check_hal` dò đúng thứ unit ĐANG làm thay vì bỏ qua khi snapshot bảo
nó nên tắt (dạng cũ báo thành công trong khi web UI trả 403).

### `POST /force-update/:target` vs `POST /force-check/:target` (bootstrap, loopback)

Hai việc khác nhau, và lẫn lộn chúng chính là thứ làm nút web trông như hỏng:

| Endpoint | Nghĩa | `min_version` |
|---|---|---|
| `force-update/<key>` | Cài `version` đã publish NGAY — đúng thứ `software-update <key>` làm qua SSH | **bỏ qua** (sàn để staging cả fleet, không phải để chặn operator chủ đích) |
| `force-check/<key>` | Chạy lại quyết định TỰ ĐỘNG cho component đó | **tôn trọng** — component đã bằng/cao hơn sàn thì không làm gì |

Nút `update` trong card Versions là `force-update` (qua
`POST /api/system/software-update/:target`). Cả hai dùng chung allowlist target —
`os-server`, `bootstrap`, `web`, `hal`, `device`, `openclaw`, `codex`, `claudecode`,
`opencode`, `picoclaw`, `hermes` — được giữ ở hai nơi và phải khớp nhau:
`ota.allowedTargets` của os-server và `forceTargetAllowed` của bootstrap. Alias `agent`
được đổi sang runtime đang cấu hình trước, nên trên thiết bị OpenClaw nút của dòng
Agent đến nơi dưới dạng `openclaw`. Bootstrap tách installer nền để worker thay thế
restart an toàn. `componentInstalled` vẫn từ chối component thiết bị không có.

### `GET /versions` (bootstrap, loopback)

Trả `{current, target, min_version, update_available, held_by_floor}` cho mọi
component thiết bị THỰC SỰ có (`componentInstalled`), nên mục CLI chính là runtime
nó đang chạy, không có cái nào khác. `held_by_floor` nghĩa là đã publish bản mới
nhưng `min_version` chưa được promote lên — worker sẽ từ chối, nên card Versions
trên web coi component bị giữ là "không có update". os-server proxy thành
`GET /api/system/ota-versions` và, cho cloud, thành MQTT `data` kind
`system.ota_versions` (cả hai qua `system/ota`; `system.software_update` là bản
MQTT của nút trên card Versions). Device profile đang cài được báo thành `device`,
resolve từ `metadata.devices.<device_type>` lồng nhau thay vì danh sách component phẳng.

### Phát hiện version hiện tại

| Thành phần | Cách phát hiện |
|---|---|
| `os-server` | Chạy `os-server --version`, parse output |
| `bootstrap` | Hằng số compile-time `config.BootstrapVersion` (ldflags) |
| `web` | Đọc file `/usr/share/nginx/html/setup/VERSION` |
| `device` | Đọc `/opt/devices/<device_type>/VERSION` |
| `openclaw` | Chạy `openclaw --version`, trích xuất semver bằng regex |
| `hal` | Đọc file `/opt/hal/VERSION_HAL` |
| `codex` / `claudecode` / `opencode` | Chạy `<cli> --version`, lấy semver ở dòng đầu (`cliSemver`) |
| `picoclaw` | Đọc `/usr/local/lib/os-runtimes/picoclaw/installed-version` — output `version` của nó không có semver |
| `hermes` | Chạy `hermes --version` ("Hermes Agent v0.21.1 (2026.9.7)"), lấy semver ở dòng đầu (`cliSemver`) |

### Cách cập nhật từng thành phần

| Thành phần | Các bước |
|---|---|
| `os-server` | Chạy `software-update os-server` (block tối đa 10 phút) |
| `bootstrap` | Spawn detached `software-update bootstrap` (tự cập nhật, sống sót sau restart) |
| `web` | Chạy `software-update web` |
| `device` | Chạy `software-update device` cho profile `devices.<device_type>` đã resolve; rootfs overlay được áp dụng nhưng vẫn giữ `.env` HAL local của thiết bị |
| `openclaw` | Chạy `software-update openclaw` (npm install đúng version trong metadata → restart `openclaw`); chỉ khi binary `openclaw` có trong PATH |
| `hal` | Chạy `software-update hal` → `systemctl restart hal` |
| `codex` / `claudecode` / `opencode` / `picoclaw` | Chạy `software-update <key>` — CHỈ trên thiết bị có `agent_runtime` đúng bằng runtime đó |
| `hermes` | Chạy `software-update hermes` — chỉ khi `agent_runtime` là hermes, entry metadata có `commit`, VÀ updater trên máy là bản biết pin (`updaterSupportsHermesPin`: có đọc `.hermes.commit`). Entry chưa pin (không có `commit`) bị cả loop lẫn `/versions` bỏ qua, nên nút trên web không hiện — `hermes update` sẽ lên upstream HEAD và không bao giờ đạt sàn. |

Cập nhật OpenClaw thủ công và force update chạy `software-update openclaw`.
Trước khi cài phiên bản trong OTA metadata, updater đọc `engines.node` của
package npm đó và kiểm tra bằng thư viện semver đi kèm npm. Node đang tương
thích được giữ nguyên; nếu chưa tương thích, updater cài Node hệ thống nhánh
24.x qua NodeSource và apt, rồi kiểm tra lại trước khi cài OpenClaw và restart
dịch vụ. Thiếu engine metadata, lỗi kiểm tra, hoặc nâng Node thất bại/vẫn chưa
tương thích đều dừng trước bước cài OpenClaw và restart. Node là dependency
chung của hệ thống; nếu đã nâng Node thành công nhưng cài OpenClaw thất bại,
updater không rollback Node. Xử lý prerequisite này không thay đổi gate cập
nhật tự động ở trên.

Image OrangePi mặc định dùng OpenClaw `2026.9.3` và Hermes `0.21.5` (khớp
bản Hermes OTA công bố ngày 2026-10-01). Builder cài package NodeSource 26.x mới nhất kể cả
khi dùng lại base image, và yêu cầu tối thiểu Node `26.8.2` (bản upstream mới nhất
tại thời điểm kiểm tra). Installer và checkout Hermes được pin vào release
`v2026.9.24`, commit `f97608f178d1ffeca59860195ab7da295f7c8e5f`; version CLI báo về
khác sẽ làm build thất bại. Đây là mặc định lúc build image; presync và hành vi
`software-update` sau đó giữ nguyên.

Sau khi cập nhật package OpenClaw và plugin, updater dừng `openclaw.service`
rồi chạy `openclaw doctor --fix --non-interactive --no-workspace-suggestions`
với `HOME=/root` và OpenClaw home/state là `/root/.openclaw`, để migrate
workspace/state cũ trước khi khởi động gateway mới. Nếu dừng service hoặc
chạy doctor thất bại, updater thoát và không restart; migration thất bại sẽ
để gateway dừng chờ sửa. Sau restart, chỉ báo thành công khi probe có xác thực
`gateway status --require-rpc --timeout 5000` thành công (tối đa 12 lần,
cách nhau 5 giây). Hết lượt probe sẽ báo cập nhật thất bại dù systemd thấy
process còn active. Package/state không tự rollback khi migration hoặc
kiểm tra readiness thất bại.

`software-update hermes` có hai chế độ, chọn theo entry metadata:

- **Pinned** (có `hermes.commit` — do `scripts/release/upload-hermes.sh <version>
  <tag|sha>` ghi; script tự resolve tag theo ngày của upstream, ví dụ `v2026.9.7`
  → `2237be35…` = 0.21.1): tải installer upstream ĐÚNG commit đó và chạy các
  stage `repository`, `venv`, `python-deps`, `path` với `--commit <sha>
  --force-commit` — đúng cờ mà `scripts/imager/build-orangepi.sh` dùng khi bake
  image. `--force-commit` còn cho phép LÙI một checkout đã bị `hermes update`
  chưa pin kéo qua bản phát hành. `hermes --version` sau đó phải bằng semver đã
  publish; lệch là fail (cặp version/commit trong metadata sai). Đây là chế độ
  bootstrap tự apply.
- **Unpinned** (không có `commit`, entry publish trước khi có pin): `hermes
  update` lên upstream HEAD; version publish chỉ là "mong đợi", lệch thì cảnh
  báo. Chỉ chạy tay qua SSH — bootstrap không bao giờ apply.

Cả hai chế độ kiểm tra Node trước. Dải phiên bản theo installer upstream Hermes:
Node 22.22+ trong nhánh 22.x, 24.11+ trong nhánh 24.x, hoặc stable 26+. Chưa
tương thích thì nâng Node hệ thống lên 24.x bằng helper dùng chung với OpenClaw
rồi kiểm lại; lỗi kiểm tra hoặc nâng Node dừng trước khi đổi gì.
Cả hai chế độ kết thúc bằng restart `hermes-gateway` rồi **os-server**: os-server
vá hai file của Hermes lúc `EnsureOnboarding` (`cache_usage.go` →
`api_server.py`, `runs_patch.go` → `api_server_runs.py`), checkout mới làm mất
vá — không restart thì các lượt sau rơi về `/v1/responses` và Flow Monitor
không có cột cache.
`runtimes/hermes/install.sh` (lúc switch runtime) vẫn cài upstream HEAD chưa
pin; vòng reconcile sẽ đưa máy về commit đã pin ở lần poll kế, y như mọi
component lệch version khác.

**Vì sao CLI của agent gate theo `agent_runtime` chứ không theo binary:**
`scripts/imager/build-orangepi.sh` bake CLI của MỌI agent lên mọi image lamp /
intern-v2 bất kể `DEFAULT_AGENT`, nên `inPath("codex")` vẫn đúng trên máy đang
chạy Hermes. Gate theo sự hiện diện của binary sẽ khiến mỗi vòng poll phát TTS
"thiết bị đang cập nhật", chuyển LED cam, tải một CLI thiết bị không dùng, rồi
restart một unit không tồn tại — lặp mãi mãi. Nên `componentInstalled` so key với
`agent_runtime` trong `/root/config/config.json`; đọc không được hoặc rỗng nghĩa
là "không phải runtime này" (bỏ qua) — hướng an toàn.

**Cửa thứ hai** chặn hướng còn lại: `updaterSupports(key)` đọc
`/usr/local/bin/software-update` và đòi đúng branch guard
(`[ "$APP" = "<key>" ]`) phải có mặt. `software-update` chỉ vào máy qua imager
hoặc `setup.sh` — KHÔNG bao giờ qua OTA — nên máy provision trước khi có các key
này sẽ giữ mãi bản updater trả `Unknown app: codex`. Không có cửa này thì mỗi
vòng poll (5 phút) máy đó sẽ nói "thiết bị đang cập nhật", thở cam, rồi fail lúc
apply; LED chỉ pulse đỏ trong 10 giây trước khi tự khôi phục. Có nó thì máy cũ
đơn giản là không nhận update CLI — kết cục duy nhất chúng có thể có — và im lặng.
Khớp theo branch guard chứ không theo key trần: key còn xuất hiện trong comment
và trong usage string của bản updater không hề implement nó.

**Chữa máy đang dùng updater cũ.** `make upload-setup` còn publish bản raw tại
`{CDN}/software-update`. Bootstrap giờ **tự động** làm mới bản trên máy từ đó, ở
đầu mỗi vòng kiểm tra, trước khi reconcile bất kỳ component nào — updater không
phải OTA component nên đây là đường tự động duy nhất nó có
(`system/bootstrap/updater_refresh.go`). URL được suy ra từ `metadata_url`
(`{base}/ota/metadata.json` → `{base}/software-update`) chứ không cấu hình riêng,
nên hai thứ không thể trỏ về hai bản khác nhau.

Cơ chế làm mới này cố ý rụt rè. Nó bỏ qua khi đang có force update chạy (bash
đọc script theo kiểu lười, đè lên updater *đang chạy* sẽ khiến nó thực thi tiếp ở
một offset bất kỳ — cũng chính là lý do script không được tự cập nhật chính nó),
nó validate bản tải về bằng `bash -n` **trước** khi đụng vào file thật, nó ghi
file tạm cùng thư mục rồi `rename` nên việc thay thế là atomic, và mọi thất bại
đều giữ nguyên updater cũ. Thiết bị không bao giờ được rơi vào cảnh mất updater.

Lệnh thủ công bên dưới vẫn dùng được, và vẫn là cách đúng khi bạn cần bản updater
mới **ngay** thay vì chờ vòng poll kế tiếp:

```bash
sudo curl -fsSL https://cdn.autonomous.ai/os/software-update -o /tmp/su \
  && sudo bash -n /tmp/su \
  && sudo install -m 0755 /tmp/su /usr/local/bin/software-update
```

`bash -n` trước `install` chính là điểm mấu chốt: tải dở dang không được phép đè
lên một updater đang chạy tốt. Sau đó máy sẽ nhận update CLI ở vòng poll kế
tiếp — không cần restart, bootstrap đọc lại file mỗi vòng. OpenClaw giữ nguyên kiểm
tra `inPath`: nó cài bằng npm theo từng máy chứ không bake sẵn, và provisioning
cũ có thể chưa set `agent_runtime`.

---

## 5. Script Cập Nhật (`/usr/local/bin/software-update`)

Bash script được cài bởi setup.sh (và được imager bake sẵn vào image). Bootstrap
worker gọi script này để thực hiện cập nhật.

Khi cài đặt, script đọc URL metadata OTA từ `metadata_url` trong `/root/config/bootstrap.json`
(biến môi trường `OTA_METADATA_URL` nếu set sẽ override, dùng cho chạy thủ công/debug),
và exit lỗi nếu cả hai đều rỗng — không có URL hardcode.

### Update bị gián đoạn và binary core

Với HAL, web, device profile, os-server và bootstrap, updater ghi và flush
`/root/bootstrap/rollback/pending-update.json` trước khi publish bản thay thế.
Journal được giữ tới khi bản mới vượt qua các health check bắt buộc. Lỗi thông
thường hoặc signal có trap sẽ khôi phục bản đã lưu; recovery lỗi giữ journal để
thử tiếp.

Update device profile giữ một snapshot rootfs riêng cho transaction đang chạy.
Snapshot dành cho rollback thủ công chỉ được thay sau khi profile mới vượt qua
health check; recovery sử dụng snapshot được ghi trong journal.

`software-update recover` không cần metadata OTA hay tải mạng. Mỗi lượt updater
phục hồi transaction còn dang dở trước khi cập nhật tiếp. Bootstrap cũng chạy
recovery trước khi kiểm tra metadata/version ở poll tự động, component check và
force update. Nó còn nhận ra trường hợp legacy: `/opt/hal` mất nhưng còn
`hal.previous` mà chưa có journal, tránh version đã lưu che mất runtime bị thiếu.
SIGKILL hoặc mất điện được phục hồi ở lần chạy updater hoặc poll bootstrap tiếp
theo; cơ chế này không bảo đảm khôi phục mọi lỗi dữ liệu thẻ SD hoặc máy không
boot được tới bước chạy các service đó.

Nếu cài HAL lần đầu mà không có runtime hay backup trước đó, recovery đưa
candidate lỗi ra khỏi đường dẫn live và giữ HAL dừng. Nó không báo đã phục hồi
một bản cũ vốn không tồn tại.

Update binary core stage executable cạnh đường dẫn đích, yêu cầu probe
`--version` thành công trong 15 giây, lưu binary cũ rồi publish bằng atomic
rename. Restart đơn thuần chưa đủ: os-server phải trả `/api/health/live` ở port
5000; bootstrap phải trả `/health` trên `httpPort` cấu hình (mặc định 8080).
Cả hai check đều yêu cầu service còn active. Thất bại sẽ khôi phục binary đã lưu
và kiểm tra service vừa phục hồi.

Phát hành thay đổi bằng **cả** `make upload-setup` (script updater) và
`make upload-bootstrap` (Go phát hiện recovery, đọc lại rollback rules và chặn
profile khi core lỗi). Chỉ upload HAL sẽ không phân phối các thay đổi này.

### Xử lý HAL

Updater tìm `uv` trong `PATH`, rồi `/root/.local/bin/uv`, rồi
`/home/pollen/.local/bin/uv` (vị trí bộ cài Reachy sử dụng). Trước khi dừng HAL,
script chọn Python extras theo `DEVICE_TYPE` trong `/opt/hal/.env`, fallback sang
`device_type` trong `/root/config/config.json`: `reachy-mini` dùng `hardware + reachy`
để giữ Pollen SDK; các thiết bị khác dùng `hardware + aec + pipecat`.

> **Cache uv nằm NGOÀI cây runtime** (`/opt/.uv-cache-hal`, cạnh `/opt/hal` để uv
> hardlink vào venv mới). Trước đây nó ở `/opt/hal/.uv-cache` nên mỗi lần update
> đều copy nó — đo được 2.5 GB, cạnh `.venv` 2.3 GB — sang staging trước khi sync:
> ~4.8 GB chép qua eMMC trước khi làm việc thật, HAL chết vài phút, và cache bị
> nhân bản mỗi lần update. Giờ không copy thư mục nào; cache in-tree được migrate
> một lần. Đo trên lamp: **5-6 phút → 41 giây**, và `/opt/hal` từ ~4.8 GB còn
> 98 MB (venv hardlink vào cache dùng chung).

Publish HAL theo cơ chế journal ở trên: giữ nguyên runtime cũ, tạo venv mới
trong staging với cache dùng chung, giữ `.env`, rename thư mục staging vào
`/opt/hal`, rồi khôi phục trạng thái service cần chạy và kiểm tra health. Chỉ
xoá journal khi thành công; lỗi staging, publish hoặc health đều đi qua recovery.
Operator cũng có thể chạy `software-update rollback hal`.

Bắt buộc kiểm tra runtime nếu HAL chạy trước update hoặc service đang enabled.
Khi đó service inactive/failed làm update thất bại, không được bỏ qua health check.
HAL phải active trước và sau khi `/health` thành công; lỗi sẽ kích hoạt rollback.
Chỉ HAL trước đó inactive và disabled mới bỏ qua kiểm tra runtime. Rollback trả
về bản cài trước, không đảm bảo sửa được dependency hoặc cache dùng chung đã hỏng.
Sau khi khởi động lại HAL thành công để phục hồi, updater tắt cleanup lúc thoát
để việc trả mã lỗi không restart service đã phục hồi thêm lần nữa.

### Xử lý Codex

Codex CLI là binary musl tĩnh publish trên GitHub releases, nên khác mọi thành
phần khác: không có artifact nào của mình nằm trên GCS. Metadata chỉ chứa
`codex.version` (không `url`/`sha256`), URL release được ghép từ chính version
đó. Metadata lưu **semver trần** (`0.149.1`) vì đó là thứ `codex --version` in
ra; tiền tố tag upstream (`rust-v`) được ghép lại lúc tải. Giữ đoạn tải này
đồng bộ với `runtimes/codex/install.sh` và `scripts/imager/build-orangepi.sh`.

```bash
"codex")
    curl -fsSL https://github.com/openai/codex/releases/download/rust-v${VERSION}/codex-aarch64-unknown-linux-musl.tar.gz
    tar -xzf …            # bước giải nén CHÍNH LÀ kiểm tra toàn vẹn (artifact upstream không có sha256)
    install -D -m 0755 /usr/local/bin/codex /root/bootstrap/rollback/codex.previous
    install -m 0755 …     /usr/local/bin/codex
    codex --version       # abort nếu binary mới không chạy được
    # codex.service chỉ tồn tại sau khi đã switch runtime sang codex; mọi image
    # lamp/intern-v2 đều bake sẵn BINARY bất kể runtime, nên restart là có điều
    # kiện. Operator có thể rollback: software-update rollback codex
    systemctl restart codex
    ;;
```

Publish version bằng `make upload-codex <semver-trần>`, thả cho fleet bằng
`make promote-codex`.

Chỉ binary bị đụng: `config.toml`, `.env` và persona vẫn do presync hook sở hữu,
nên update không thể ghi đè cấu hình Codex của thiết bị.

### Xử lý Claude Code

Khác Codex, Claude Code **không** phải binary mình tự đặt: installer của
Anthropic sở hữu `/root/.local/share/claude/versions/<ver>` và trỏ
`/root/.local/bin/claude` vào đó, còn `/usr/local/bin/claude` chỉ là symlink của
mình. Nên update = chạy lại installer với version đã publish (installer nhận
version qua tham số vị trí: `install.sh [stable|latest|VERSION]`).

```bash
"claudecode")
    HOME=/root curl -fsSL https://claude.ai/install.sh | bash -s -- "$VERSION"
    ln -sf /root/.local/bin/claude /usr/local/bin/claude   # installer có thể trỏ lại symlink của nó
    claude --version                                       # abort nếu không chạy được
    systemctl restart claudecode                           # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

Cố ý **không có backup `.previous` / rollback target**: installer vẫn giữ thư mục
`versions/<ver>` cũ, nên muốn lùi thì publish version cũ rồi chạy
`software-update claudecode`, chứ không phải khôi phục file mình lưu. Publish
bằng `make upload-claudecode <semver-trần>`, thả bằng `make promote-claudecode`.

### Xử lý OpenCode

OpenCode dùng installer chính chủ (arch detection + giải nén là việc của
upstream); mình chỉ pin version và ép thư mục cài. Khớp với
`runtimes/opencode/install.sh` và bake trong imager — giữ cả ba đồng bộ.

```bash
"opencode")
    install -D -m 0755 /usr/local/bin/opencode /root/bootstrap/rollback/opencode.previous
    curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version "$VERSION"
    # …rồi bước copy dự phòng y như install.sh, phòng khi installer bỏ qua
    # OPENCODE_INSTALL_DIR và dùng ~/.opencode/bin.
    opencode --version
    systemctl restart opencode      # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

`OPENCODE_INSTALL_DIR` PHẢI đứng trước `bash`, không phải `curl` — trong pipeline
`VAR=x curl … | bash` biến chỉ bind vào `curl`. Vì đây đúng là binary ở đường dẫn
cố định nên nó CÓ backup `.previous`: `software-update rollback opencode` chạy
được, khác claudecode. Publish bằng `make upload-opencode <semver-trần>`, thả
bằng `make promote-opencode`.

### Xử lý Hermes (chỉ auto-apply khi entry được pin theo commit)

Hermes cài kiểu git; `runtimes/hermes/install.sh` ghi
`/usr/local/lib/hermes-agent/.install_method=git` chính là để updater upstream
nhận ra. Updater đó **không nhận version đích** — `hermes update` luôn nhảy lên
HEAD của upstream — nên nhánh này có hai mode như mô tả ở trên: entry **pinned**
(`hermes.commit`) chạy installer upstream tại đúng commit đó, entry **unpinned**
quay về `hermes update`. Bootstrap chỉ auto-apply entry pinned (`hermesPinned` +
`updaterSupportsHermesPin` trong `system/bootstrap/bootstrap.go`); entry unpinned
bị loop và `/versions` bỏ qua, chỉ áp dụng được bằng cách chạy
`software-update hermes` qua SSH.

```bash
"hermes")
    HERMES_COMMIT=$(jq -r '.hermes.commit // empty' metadata)
    if [ -n "$HERMES_COMMIT" ]; then
        update_hermes_pinned "$HERMES_COMMIT"   # các stage installer tại commit, --force-commit
    else
        hermes update                          # unpinned: HEAD upstream
    fi
    hermes --version                           # abort nếu không chạy được
    # Version thực tế != version metadata → pinned: FAIL (cặp metadata sai);
    # unpinned: chỉ CẢNH BÁO.
    systemctl restart hermes-gateway           # chỉ khi unit tồn tại; sau đó restart os-server
    ;;
```

Tên unit lấy đúng theo khai báo trong `/usr/local/lib/os-runtimes/hermes/service`
(`hermes-gateway`), và lần chạy báo về version Hermes THỰC SỰ đạt được, không
phải version yêu cầu. Không có backup `.previous`: giống claudecode, bản cài do
tool upstream sở hữu chứ không phải file mình copy.

### Xử lý PicoClaw (ca cá biệt về version)

PicoClaw là binary trần từ GitHub releases của CHÍNH MÌNH (không tarball, khác
codex). `version` publish ra là **TAG** release (`v0.3.1-fixvision`), không phải
semver: `picoclaw version` in ra một chuỗi build không liên quan
(`nightly-44-g1959045c-dirty`), nên không thể suy ngược tag từ binary. Vì vậy
bản update ghi tag đã cài vào
`/usr/local/lib/os-runtimes/picoclaw/installed-version` — mọi chỗ kiểm version
phải đọc stamp này, không phải `picoclaw version`.

```bash
"picoclaw")
    curl -fsSL https://github.com/autonomous-ai/picoclaw/releases/download/${VERSION}/picoclaw-linux-arm64
    picoclaw --no-color version         # chạy TỪ THƯ MỤC TẠM trước: cổng kiểm tra toàn vẹn duy nhất
    install -D -m 0755 /usr/local/bin/picoclaw /root/bootstrap/rollback/picoclaw.previous
    install -m 0755 …                   /usr/local/bin/picoclaw
    echo "$VERSION" > /usr/local/lib/os-runtimes/picoclaw/installed-version
    systemctl restart picoclaw          # có điều kiện: unit chỉ tồn tại khi runtime đang active
    ;;
```

`make upload-picoclaw <release-tag>` kiểm tra tag có thật asset
`picoclaw-linux-arm64` trước khi publish — gõ sai tag nếu không sẽ chỉ lộ ra
dưới dạng OTA fail trên mọi thiết bị đang poll. Rollback dùng được
(`software-update rollback picoclaw`).

---

## 6. HAL Runtime — Nguồn & Tích Hợp

### Chiến lược: Copy code + Track thủ công

Code HAL runtime được **copy** từ project upstream open-source vào mono-repo này, rồi sửa đổi nhiều.

**Tại sao copy, không dùng submodule/subtree:**
- Cần **bỏ** phần LiveKit/OpenAI (thay bằng OpenClaw)
- Cần **thêm** HTTP API server (FastAPI) để OS Server bridge đến
- Cần **thêm** DisplayService (GC9A01 eyes + info, không có trong upstream)
- Cần **sửa** services cho phù hợp kiến trúc mới
- Phần overlap chỉ là drivers (~30-40% code upstream), phần còn lại viết lại

**Theo dõi upstream:**
- Nguồn: `https://github.com/humancomputerlab/lelamp_runtime`
- Ghi commit hash upstream vào `hal/UPSTREAM.md` khi copy
- Định kỳ check upstream cho driver-level fixes (servo protocol, LED timing, ...)
- Cherry-pick thủ công khi cần
- Bỏ qua thay đổi AI/LiveKit upstream (mình đã thay thế hoàn toàn)

**Các bước thực hiện:**
1. Clone `humancomputerlab/lelamp_runtime` về thư mục tạm
2. Copy driver code (`services/motors.py`, `services/rgb.py`, `services/audio.py`, `services/service_base.py`) vào `hal/drivers/` (nay là `hal/drivers/motors/`, `hal/drivers/rgb/`, `hal/drivers/voice/`, `hal/drivers/base.py`)
3. Xoá toàn bộ code LiveKit, OpenAI, conversation
4. Thêm `hal/server.py` — HTTP API server mới (FastAPI)
5. Thêm `hal/drivers/display/display_service.py` — DisplayService mới cho GC9A01
6. Tạo `hal/UPSTREAM.md` ghi commit hash nguồn và ngày copy
7. Test trên thiết bị với phần cứng thật

### Cấu trúc Mono-repo

HAL nằm trong repo này dưới dạng subfolder Python, cùng với Go và TypeScript:

```
autonomous-os/
├── system/               # Go os-server + bootstrap worker
│   ├── cmd/              # Go entrypoints (os-server, bootstrap)
│   ├── server/           # Go HTTP layer
│   ├── bootstrap/        # Go OTA worker
│   ├── domain/           # Struct dùng chung
│   └── web/              # TypeScript/React SPA
├── runtimes/             # Các agent runtime thay thế được (openclaw, hermes, codex, …)
├── hal/                  # Python hardware runtime
│   ├── server.py         # FastAPI app (hal.server:app) — không từ upstream
│   ├── routes/           # Module route FastAPI (servo, led, camera, audio, display, …)
│   ├── drivers/
│   │   ├── base.py       # Event-driven ServiceBase (từ upstream)
│   │   ├── motors/       # MotorsService — Feetech servos (từ upstream)
│   │   ├── rgb/          # RGBService — WS2812 LEDs (từ upstream)
│   │   ├── voice/        # Audio, STT, TTS
│   │   └── display/      # DisplayService — GC9A01 LCD (display_service.py, không từ upstream)
│   ├── config.py         # Runtime config (env vars)
│   ├── pyproject.toml    # Python dependencies (uv; khoá trong uv.lock)
│   ├── VERSION_HAL       # Version string dạng text
│   └── UPSTREAM.md       # Track commit nguồn từ humancomputerlab/lelamp_runtime
├── skills/               # Các file SKILL.md của agent
├── scripts/
│   └── provision/setup.sh
├── go.mod
├── Makefile
└── CLAUDE.md
```

3 ngôn ngữ (Go, Python, TypeScript), 1 repo. Mỗi cái build riêng, quản lý chung.

### HAL OTA Package

Để phân phối qua OTA, `scripts/release/upload-hal.sh` zip nội dung folder `hal/` (loại `.venv/`, `__pycache__/`, `.git/`, `*.pyc`, `.env`, `.python-version`, `test/`):

```
hal-{version}.zip         # nội dung hal/ nằm ở gốc zip → giải nén vào /opt/hal
├── server.py, routes/, drivers/, …
├── pyproject.toml, uv.lock
└── VERSION_HAL
```

### HAL HTTP API (FastAPI trên port 5001)

HAL Python runtime expose HTTP API trên `127.0.0.1:5001`. Agent skill trên thiết bị gọi thẳng qua loopback; web UI đi qua reverse proxy `/api/hardware/*` của OS Server (có admin auth, `system/server/proxy.go`). Nginx proxy `/hw/*` chỉ cho caller trên cùng máy — client bên ngoài nhận 403. Swagger UI tại `/hw/docs` không truy cập được từ LAN.

```
Agent skill → curl http://127.0.0.1:5001/servo/play → HAL Python → Phần cứng
Web UI       → /api/hardware/servo/play → OS Server (admin auth) → http://127.0.0.1:5001/servo/play → HAL Python
Bên ngoài    → http://<device-ip>/hw/docs    → nginx → 403 Forbidden
```

#### Endpoints (v0.2.0)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/health` | GET | Kiểm tra hardware (servo, led, camera, audio) |
| `/servo` | GET | Recordings hiện có + trạng thái |
| `/servo/play` | POST | Chạy animation theo tên |
| `/led` | GET | Thông tin LED strip |
| `/led/solid` | POST | Đổ 1 màu |
| `/led/paint` | POST | Set màu từng pixel |
| `/led/off` | POST | Tắt tất cả LED |
| `/camera` | GET | Thông tin camera (resolution, availability) |
| `/camera/snapshot` | GET | Chụp 1 frame JPEG |
| `/camera/stream` | GET | MJPEG stream |
| `/audio` | GET | Thông tin audio device (Seeed mic/speaker) |
| `/audio/volume` | GET | Lấy volume hiện tại |
| `/audio/volume` | POST | Set volume (0-100%) |
| `/audio/play-tone` | POST | Phát test tone |
| `/audio/record` | POST | Thu âm từ mic, trả WAV |

---

## 7. Scripts Upload / Publish

### `scripts/release/upload-hal.sh`

Chạy qua `make upload-hal`. Script source `ota-config.sh` / `ota-metadata.sh`, rồi:

1. `uv lock --project hal --python 3.12 --check` — từ chối publish khi lockfile lỗi thời.
2. Tăng patch version trong `hal/VERSION_HAL` (khởi tạo `1.0.0` nếu chưa có).
3. Zip nội dung `hal/` thành `hal-<version>.zip` (loại `.venv/`, `__pycache__/`, `.git/`, `*.pyc`, `.env`, `.python-version`, `test/`).
4. Upload lên `gs://${GCS_BUCKET}/${BUCKET_PREFIX}/ota/hal/<version>.zip` (no-cache).
5. Giải nén `ota/metadata.json` đã ký, set `hal.version`, `hal.url`, `hal.sha256`, `hal.updated_at` (giữ nguyên `min_version` đang có — nâng bằng `promote-ota.sh`), ký lại và upload.

### Tất cả upload scripts

| Script | Thành phần | Pattern |
|---|---|---|
| `scripts/release/upload-os-server.sh` | OS Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-bootstrap.sh` | Bootstrap Server binary | Build → zip → GCS → update metadata |
| `scripts/release/upload-web.sh` | Web SPA bundle | Build → zip → GCS → update metadata |
| `scripts/release/upload-hal.sh` | HAL Python runtime | Lock check → zip → GCS → update metadata |
| `scripts/release/upload-setup.sh` | Script setup | Upload lên GCS |
| `scripts/release/upload-setup-ap.sh` | Script setup AP | Upload lên GCS |
| `scripts/release/upload-skills.sh` | OpenClaw skill files | Upload lên GCS |
| `scripts/release/upload-openclaw.sh` | Version OpenClaw npm | Chỉ metadata (device chạy `npm install -g`) |
| `scripts/release/upload-codex.sh` | Version Codex CLI | Chỉ metadata (device tải tarball GitHub release) |
| `scripts/release/upload-claudecode.sh` | Version Claude Code CLI | Chỉ metadata (device chạy installer Anthropic) |
| `scripts/release/upload-opencode.sh` | Version OpenCode CLI | Chỉ metadata (device chạy installer opencode.ai) |
| `scripts/release/upload-picoclaw.sh` | TAG release PicoClaw | Chỉ metadata (device tải asset GitHub); kiểm tra tag có thật |
| `scripts/release/upload-hermes.sh` | Version Hermes + tag/commit upstream | Chỉ metadata: `hermes.version` + `hermes.commit` (device checkout commit qua installer upstream) |
| `scripts/provision/install.sh` | CDN install shortcut | `curl ... \| sudo bash` trên Pi |
| `scripts/release/tag-release.sh` | Git release tag kèm OTA metadata snapshot | Fetch metadata.json → annotated tag → `git push origin <tag>` |

### `scripts/release/tag-release.sh` — Truy nguồn theo GPL v3 §6

Sau khi các upload component xong (`make upload-os-server upload-hal upload-web ...`), script này neo OTA metadata snapshot vào một git tag duy nhất:

```bash
make tag-release v0.0.8
# → curl https://cdn.autonomous.ai/os/ota/metadata.json
# → git tag -a v0.0.8 -F - (annotation = JSON metadata đẹp)
# → git push origin v0.0.8
```

Người mua chạy `os-server --version` trên thiết bị — giá trị lấy từ `git describe --tags --always --dirty` lúc build (`Makefile:VERSION`), nên resolve về tag gần nhất. Họ mở repo public (`github.com/autonomous-ai/autonomous-os`), tìm tag đúng, đọc annotation để xem chính xác version `os-server`/`hal`/`web`/`bootstrap` đã bake vào release đó, rồi checkout commit tương ứng để có source.

Guards trong script: từ chối nếu tag đã tồn tại local hoặc trên remote, từ chối nếu fetch metadata fail hoặc JSON invalid (`set -euo pipefail` + `jq .`). Override qua env: `OTA_METADATA_URL` (mặc định: `https://cdn.autonomous.ai/os/ota/metadata.json`), `TAG_REMOTE` (mặc định: `origin`).

---

## 8. Build & Version

### Go binaries (ldflags)

```makefile
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Go symbol giữ config.LampVersion (nội bộ, không thuộc deploy identity).
LDFLAGS_OS   := -X go.autonomous.ai/os/system/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT := -X go.autonomous.ai/os/system/bootstrap/config.BootstrapVersion=$(VERSION)

os-build-bootstrap:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap

os-build:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS_OS)" -o os-server ./cmd/os-server
```

### HAL (file VERSION_HAL)

Version của HAL là file text `hal/VERSION_HAL` (cài thành `/opt/hal/VERSION_HAL`), bootstrap đọc trực tiếp file này. Không sửa tay khi release — `make upload-hal` (`scripts/release/upload-hal.sh`) tự tăng patch version.

---

## 9. Khác Biệt So Với Lobster

| Khía cạnh | Lobster (gốc) | Autonomous (project này) |
|---|---|---|
| Số thành phần | 4 (lamp, bootstrap, web, openclaw) | **5** (+ hal) |
| OTA keys | lamp, bootstrap, web, openclaw | + **hal** |
| Setup stages | 7 (stage -1 đến 4) | **8** (+ stage 2b: HAL) |
| Systemd services | 4 | **5** (+ hal.service) |
| Python runtime | Không có | **HAL** tại /opt/hal/ với venv |
| Hardware bridge | Không có | OS server HTTP → HAL HTTP (localhost proxy) |
| SPI usage | Chỉ LED | LED + **Display (GC9A01)** |

---

## 10. Câu Hỏi Mở

- [x] **HAL source**: Mono-repo. Driver code copy từ `humancomputerlab/lelamp_runtime` vào `hal/`, bỏ LiveKit/OpenAI, thêm HTTP API + DisplayService. Track upstream thủ công qua `hal/UPSTREAM.md`.
- [x] **HAL HTTP port**: `5001` (OS Server là `5000`).
- [x] **Bridge protocol**: HTTP proxy đơn giản. HAL chạy FastAPI trên `127.0.0.1:5001`, OS Server proxy từ port 5000.
- [x] **Python version**: Pin Python 3.12.x (`pyproject.toml`, `.python-version`, `setup.sh` dùng `uv sync --python 3.12`).
- [x] **Đóng gói HAL**: Tạo venv trên thiết bị qua `uv sync --python 3.12 --extra hardware`, thêm `--extra reachy` cho Reachy Mini hoặc `--extra aec --extra pipecat` cho thiết bị khác. OTA tạo venv mới bằng cache dùng chung, giữ `.env` và runtime cũ để rollback.
- [x] **Display driver**: DisplayService (GC9A01) là một phần của HAL Python tại `hal/drivers/display/display_service.py`.
- [x] **HAL config**: Dựa trên biến môi trường (`config.py` đọc env vars). Hỗ trợ file `.env` qua `python-dotenv`. Không cần config file riêng.

---

> Tài liệu này mô tả toàn bộ hệ thống OTA và bootstrap.
> Xem [architecture-decision.md](../../robots/lamp/docs/vi/architecture-decision.md) cho quyết định kiến trúc.
> Xem [product-vision.md](../../robots/lamp/docs/vi/product-vision.md) cho tầm nhìn sản phẩm.
