# Reachy Mini Kế Hoạch Khởi Động Lần Đầu

Kế hoạch từng bước cho phiên thử nghiệm thiết bị thật đầu tiên. Chạy một lần
khi máy Wireless về, sau đó cập nhật `runtime.md`, `.env`, và `setup.sh` với
kết quả.

## Giai Đoạn 1: SSH Trinh Sát (Chỉ Đọc)

> **ĐÃ XONG — 2026-07-29** trên con Wireless đầu tiên (`hardware_id
> e4a0ef5f04fafb94`). Mọi mục `Ghi lại` bên dưới đã điền số đo thật, bảng kết quả
> đầy đủ nằm ở [`runtime_vi.md`](runtime_vi.md#recon-máy-thật-đo-ngày-2026-07-29).
> Máy mới thì chạy lại giai đoạn này; giá trị chỉ khác nhau ở những chỗ có ghi chú.

SSH vào và thu thập thông tin hệ thống. **Chưa thay đổi gì.**

```bash
ssh pollen@reachy-mini.local   # mật khẩu: root
```

`root` không SSH thẳng vào được (`Permission denied (publickey,password)`) — đăng
nhập bằng `pollen` rồi `sudo`.

**Lối tắt:** [`../recon.sh`](../../recon.sh) chạy toàn bộ lệnh trong giai đoạn này
một phát và in ra bảng tóm tắt để điền. Ưu tiên dùng nó thay vì gõ tay từng mục:

```bash
scp robots/reachy-mini/recon.sh pollen@reachy-mini.local:/tmp/
ssh pollen@reachy-mini.local 'bash /tmp/recon.sh' | tee reachy-recon.txt
# thêm --audio-test để chạy luôn loopback mic->loa 3s (bước duy nhất phát ra tiếng)
```

Các mục thủ công bên dưới ghi lại từng probe kiểm tra gì và tại sao.

### 1.1 OS & Kernel

```bash
cat /etc/os-release              # Bookworm hay Bullseye?
uname -a                        # phiên bản kernel, kiến trúc
cat /boot/firmware/config.txt 2>/dev/null || cat /boot/config.txt
df -h                           # dung lượng ổ (16 GB eMMC)
free -h                         # RAM
```

**Tại sao**: xác định package khả dụng, cú pháp dtoverlay, và đường dẫn config
là `/boot/firmware/` hay `/boot/`.

### 1.2 Network Stack

```bash
systemctl is-active NetworkManager
systemctl is-active dhcpcd
systemctl is-active wpa_supplicant
systemctl is-active systemd-networkd
nmcli device status 2>/dev/null || echo "Không có NetworkManager"
ip addr show wlan0
cat /etc/wpa_supplicant/*.conf 2>/dev/null
cat /etc/dhcpcd.conf 2>/dev/null
ls /etc/NetworkManager/system-connections/ 2>/dev/null
```

**Tại sao**: kiểm tra quan trọng nhất. Quyết định `setup.sh` có thể dùng lại
flow dhcpcd/wpa_supplicant hiện tại hay cần đường dẫn NetworkManager. Xem
[recovery_vi.md](recovery_vi.md) để phân tích rủi ro.

**Cây quyết định**:

```
NetworkManager đang chạy?
├── CÓ → viết setup.sh tương thích NM (nmcli cho AP/STA, bỏ hostapd)
│         HOẶC tắt NM và cài dhcpcd stack (rủi ro hơn)
└── KHÔNG → dhcpcd đang chạy?
    ├── CÓ → setup.sh hiện tại hoạt động được
    └── KHÔNG → systemd-networkd? custom? → điều tra
```

**Kết quả (2026-07-29)**: NetworkManager **đang chạy**, `wpa_supplicant` chạy,
`dhcpcd` tắt → đi **nhánh nmcli**. Pollen đã có sẵn hai profile NM, nên setup.sh
nên mở rộng chúng thay vì cài hostapd/dnsmasq:

| Profile | Vai trò |
|---------|---------|
| `Glinks` | STA — mạng WiFi máy đã được provision vào |
| `Hotspot` | AP — `mode=ap`, ssid `reachy-mini-ap`, `ipv4=shared`, `autoconnect=false`, do `reachy-mini-daemon.service` ("AP Launcher") điều khiển |

### 1.3 Pollen Daemon

```bash
systemctl list-units | grep -i reachy
systemctl list-units | grep -i pollen
systemctl status reachy*
curl -s http://localhost:8000 | head -20
curl -s http://localhost:8000/api 2>/dev/null | head -20
ls /venvs/
ls /restore/venvs/
pip list 2>/dev/null | grep -i reachy
cat /etc/systemd/system/reachy* 2>/dev/null
```

**Tại sao**: cần biết chính xác tên service, port, API surface, và layout venv
để HAL driver và setup.sh không va chạm.

**Ghi lại** (2026-07-29):
- [x] Tên service daemon: `reachy-mini-daemon.service` (thêm
      `reachy-mini-bluetooth.service`, `gpio-shutdown-daemon.service`)
- [x] Port daemon: `8000` (REST + WS); còn listen cả `8443`
- [x] Đường dẫn API gốc: `/api/...`, WS ở `/ws/sdk` (còn `/ws/daemon`, `/ws/full`,
      `/ws/raw`, `/ws/set_target`, `/ws/apps`, `/ws/logs`, `/ws/updates`)
- [x] Phiên bản Python trong `/venvs/`: `3.12` (`/venvs/mini_daemon`,
      `reachy_mini` 1.9.0); Python hệ thống là `3.13.5`
- [x] `/restore/venvs/` tồn tại: **có**

`GET /` trả trang "dashboard deprecated" — API nằm dưới `/api/`, và
`GET /openapi.json` liệt kê hết. Endpoint đáng chú ý: `/api/daemon/status`
(thống kê control loop, wlan ip, hardware id), `/api/camera/specs` (độ phân giải +
intrinsics K/D), `/api/motors/status`, `/api/move/*`, `/api/state/*`,
`/api/volume/*`, `/wifi/*`, `/update/*`, `/api/apps/*`, và phần bàn giao media
mô tả ở 1.9.

### 1.4 Âm Thanh

```bash
arecord -l                       # liệt kê thiết bị thu (mic array)
aplay -l                        # liệt kê thiết bị phát (loa)
cat /proc/asound/cards
cat /etc/asound.conf 2>/dev/null
# Test nhanh (thu 3 giây, phát lại)
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:1,0 /tmp/test.wav
```

**Ghi lại** (2026-07-29):
- [x] Tên ALSA mic: `plughw:0,0` → alias thành `plug:device_mic`
- [x] Tên ALSA loa: `plughw:0,0` → alias thành `plug:device_speaker`
      (**cùng card, cùng device với mic** — chung một interface USB audio)
- [x] Số kênh mic: verify được mono 1 kênh; mảng mic lộ ra như một thiết bị
      capture USB Audio duy nhất, không tách kênh từng mic
- [x] Sample rate 16 kHz hoạt động: **có** (`arecord -f S16_LE -r 16000 -c 1`)

Card thấy được: `0: Audio [Reachy Mini Audio]` (USB, thu + phát), `1: vc4hdmi0`,
`2: vc4hdmi1`. Pollen không ship `/etc/asound.conf` nào, nên file của mình thêm
vào mà không đụng `pcm.!default`.

**Lưu ý**: các lệnh này chỉ chạy được sau khi daemon nhả media — xem 1.9.

### 1.5 Camera

```bash
v4l2-ctl --list-devices
ls /dev/video*
# Test nhanh
v4l2-ctl -d /dev/video0 --all 2>/dev/null | head -30
# Nếu dùng libcamera thay V4L2:
libcamera-hello --list-cameras 2>/dev/null
```

**Ghi lại** (2026-07-29):
- [x] Camera device index: `/dev/video0` là node **unicam Bayer thô** — không
      dùng được như index của OpenCV. `HAL_CAMERA_INDEX` vô tác dụng trên body này.
- [x] V4L2 hay libcamera: **libcamera** (`imx708_wide` trên CSI, có `rpicam-apps` +
      `gstreamer1.0-libcamera`, **chưa** cài `python3-picamera2`)
- [x] Độ phân giải tối đa: sensor `4608x2592` 10-bit RGGB; mode daemon công bố cao
      nhất là `3840x2592@10fps`, mặc định `1280x720@30fps`

Đường OpenCV đã đo là hỏng: `cv2.VideoCapture(0)` mở được nhưng `read()` trả
`False` (`select() timeout`), và bản `opencv-python` từ wheel báo `GStreamer: NO`
nên pipeline `libcamerasrc` cũng không dùng được. Các hướng chọn camera so sánh ở
[`runtime_vi.md`](runtime_vi.md#camera-stack-libcamera-không-phải-uvc).

### 1.6 Dịch Vụ & Cổng Đang Dùng

```bash
ss -tlnp                        # tất cả cổng TCP đang lắng nghe
systemctl list-units --type=service --state=running
# Kiểm tra xung đột cổng với dịch vụ của mình
# HAL: 5001, os-server: 5000 (chỉ bind 127.0.0.1), nginx: 80
```

**Ghi lại** (2026-07-29):
- [x] Cổng 5001 trống: **có**
- [x] Cổng 5000 trống: **có** (os-server, chỉ loopback)
- [x] Cổng 80 trống: **có** — Pollen không ship nginx

Daemon (tiến trình `python` của `/venvs/mini_daemon`) chiếm `8000` và `8443`, cộng
vài port ephemeral theo từng interface. `22` là sshd.

### 1.7 Dependency Hệ Thống

```bash
# Kiểm tra build deps cho pygobject/pycairo
dpkg -l | grep -E 'libcairo2-dev|libgirepository|pkg-config'
python3 --version
which uv 2>/dev/null
which pip3
```

### 1.8 Bluetooth

```bash
# Kiểm tra dịch vụ BLE cho recovery
systemctl status bluetooth
hciconfig -a 2>/dev/null
```

**Ghi lại** (2026-07-29): `bluetoothd` đang chạy, adapter `hci0` UP, tên
`reachy-mini`, BD `88:A2:9E:8C:DC:B7` → đường recovery BLE Level B trong
[recovery_vi.md](recovery_vi.md) dùng được trên con này.

### 1.9 Quyền Sở Hữu Media (ai đang giữ camera và audio)

Probe quan trọng nhất, và cũng là thứ plan ban đầu bỏ sót. Daemon Pollen giữ
camera và cả hai PCM ALSA trong lúc nó chạy:

```bash
sudo fuser -v /dev/video0 /dev/video1     # python của daemon + pipewire + wireplumber
sudo fuser -v /dev/snd/*                  # python của daemon giữ pcmC0D0c và pcmC0D0p
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 1 /tmp/t.wav
#   -> audio open error: Device or resource busy
curl -s http://localhost:8000/api/media/status
```

Daemon có API bàn giao rõ ràng — chính nó làm cho thiết kế "HAL sở hữu
audio/camera" khả thi:

```bash
curl -s -X POST http://localhost:8000/api/media/release   # nhả camera + audio
# ... verify: arecord thu được, rpicam-jpeg chụp được ...
curl -s -X POST http://localhost:8000/api/media/acquire   # trả lại
```

**Ghi lại** (2026-07-29):
- [x] Daemon giữ camera + cả hai PCM audio mặc định: **có**
- [x] `POST /api/media/release` nhả ra được: **có** (verify bằng `arecord` và
      `rpicam-jpeg`)
- [x] Daemon sống sót qua release/acquire: **có** — vẫn `active`, HTTP 200, motion
      không bị ảnh hưởng
- [x] Đã nối vào startup/shutdown của HAL: **rồi** — `ROBOT.md` khai
      `owner: pollen_daemon` trên `audio` và `vision`, `PollenDaemonMediaOwner`
      gọi `release` lúc startup (retry 5 lần cách 2 s) và `acquire` lúc shutdown.
      Không script spike nào gọi `/api/media/*` nữa

## Giai Đoạn 2: Viết Config Dựa Trên Kết Quả

Sau Giai đoạn 1, cập nhật các file này **trên máy dev** (không phải trên Pi):

### 2.1 ALSA Config — XONG

`robots/reachy-mini/rootfs/etc/asound.conf` đã có, dùng thiết bị đo được. Mic và
loa là cùng một card USB, địa chỉ theo tên để hai card HDMI không làm lệch index:

```
pcm.device_mic {
    type plug
    slave.pcm "hw:CARD=Audio,DEV=0"
}

pcm.device_speaker {
    type plug
    slave.pcm "hw:CARD=Audio,DEV=0"
}
```

File này cố ý không có `pcm.!default`: daemon dùng chung phần cứng và phải giữ
nguyên default mà nó cần.

### 2.2 HAL .env — audio XONG, camera XONG bằng driver `rpicam`

`robots/reachy-mini/rootfs/opt/hal/.env` đã mang giá trị đo được:

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic        # từ 1.4
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker   # từ 1.4
HAL_CAMERA_INDEX=0                          # vô tác dụng — xem 1.5, libcamera chứ không phải V4L2
```

Audio chỉ mở được sau khi daemon nhả media (1.9).

Camera chưa bao giờ là chuyện của một giá trị config, nên không field `.env` nào
sửa được: `/dev/video0` là node unicam Bayer thô, và bản `opencv-python` từ wheel
báo `GStreamer: NO`, nên cả `cv2.VideoCapture(0)` lẫn pipeline `libcamerasrc` đều
không ra frame (1.5). Đã giải quyết bằng một camera backend thứ hai của HAL:
`ROBOT.md` khai `driver: rpicam`, và `hal/drivers/camera/factory.py` map tên đó
tới `RpicamVideoCaptureDevice` (`hal/drivers/camera/rpicam_capture_device.py`) —
driver này đọc MJPEG từ tiến trình con `rpicam-vid` rồi decode frame mới nhất
bằng `cv2.imdecode`. `HAL_CAMERA_INDEX` vô tác dụng trên body này. Chi tiết ở
[`runtime_vi.md`](runtime_vi.md#camera-stack-libcamera-không-phải-uvc).

### 2.3 setup.sh (Mới, Riêng Cho Reachy)

Chưa viết. Không có `robots/reachy-mini/setup.sh`: installer duy nhất trong thư
mục đó là `robots/reachy-mini/install.sh`, tải device package từ OTA rồi giao cho
`spike.sh` (cài song song với Pollen, không đổi mạng). Provisioning production cần
nhánh `DEVICE_TYPE=reachy-mini` trong `scripts/provision/setup.sh` dùng chung,
script này hiện vẫn dừng NetworkManager (xem
[recovery_vi.md](recovery_vi.md#đánh-giá-rủi-ro-đã-chốt-pollen-os-dùng-networkmanager)). Trinh sát đã chốt nhánh: **NetworkManager** —
dùng cột "Nếu NM", và tái sử dụng profile `Hotspot` sẵn có của Pollen thay vì
dựng một AP stack song song.

| Quyết định | Nếu NM | Nếu dhcpcd |
|------------|--------|------------|
| AP mode | `nmcli` hotspot hoặc cài hostapd | Flow hostapd hiện tại |
| STA mode | `nmcli con add` | Flow wpa_supplicant hiện tại |
| DNS captive portal | dnsmasq drop-in (giống) | dnsmasq drop-in (giống) |
| Mask service | Không mask NM, cấu hình nó | Mask global wpa_supplicant (giống) |

Bất kể network stack nào, setup.sh phải:

1. **Không bao giờ dừng hoặc restart Pollen daemon** trong quá trình cài
2. **Cài vào venv riêng** (`/opt/hal/.venv/`, không phải `/venvs/`)
3. **Cài system deps**: `libcairo2-dev`, `libgirepository1.0-dev`, `pkg-config`
4. **Không xung đột cổng**: kiểm tra 5001, 5000, 80 trống trước khi bind
5. **Đặt hostname** thành `reachy-mini-<suffix>` không phá mDNS của Pollen
6. **Tạo systemd units** cho `hal.service` và `os-server.service`
7. **Cài nginx** với config captive portal (hoặc bỏ qua nếu Pollen đã chạy nginx)

### 2.4 Kế Hoạch .env HAL Production

File `.env` hiện tại ở `robots/reachy-mini/rootfs/opt/hal/.env` có nhiều
placeholder `TODO(spike)`. Sau Giai đoạn 1, điền giá trị thật.

**Bảng field .env đầy đủ** (field đánh dấu `?` cần dữ liệu từ thiết bị thật):

```bash
# --- Core ---
HAL_MODE=production
HAL_LOG_LEVEL=INFO
DEVICE_TYPE=reachy-mini
DEVICES_DIR=/opt/devices

# --- Pollen daemon ---
# Trinh sát 1.3: xác nhận host/port. Hiện đang comment (mặc định
# localhost:8000 trong reachy_service.py). Chỉ bỏ comment nếu daemon
# chạy trên host hoặc port khác.
#REACHY_DAEMON_HOST=localhost          # ? xác nhận daemon trên localhost
#REACHY_DAEMON_PORT=8000              # ? xác nhận port từ `ss -tlnp`

# --- Âm thanh ---
# Trinh sát 1.4: điền từ `arecord -l` / `aplay -l`
HAL_AUDIO_INPUT_ALSA=plug:device_mic  # ? tên ALSA thật sau asound.conf
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker  # ? tên ALSA thật
HAL_VAD_THRESHOLD=500                 # có thể cần chỉnh cho 4-mic array
HAL_SPEECH_HOLDOFF=0.05
HAL_SILENCE_TIMEOUT=3.0
HAL_STT_KEEPALIVE=false
HAL_SILERO_ENABLED=true
HAL_SILERO_THRESHOLD=0.15             # ? chỉnh trên phần cứng thật
HAL_SILERO_CHUNK_SIZE=512
HAL_WEBRTCVAD_ENABLED=true
HAL_WEBRTCVAD_AGGRESSIVENESS=2        # ? có thể cần 3 vì servo ồn
HAL_WEBRTCVAD_FRAME_MS=30
HAL_TTS_SPEED=1.1

# --- Camera ---
# Trinh sát 1.5: xác nhận index từ `v4l2-ctl --list-devices`
HAL_CAMERA_INDEX=0                    # ? xác nhận device index
HAL_CAMERA_WIDTH=1280                 # ? xác nhận độ phân giải tối đa
HAL_CAMERA_HEIGHT=720                 # ? khi chia sẻ CPU với daemon
HAL_CAMERA_STREAM_WIDTH=960
HAL_CAMERA_STREAM_HEIGHT=540
HAL_CAMERA_AUTO_EXPOSURE=auto
# LƯU Ý: nếu Pollen OS dùng libcamera thay V4L2, backend OpenCV của HAL
# có thể cần LIBCAMERA_LOG_LEVELS=ERROR hoặc pipeline gstreamer. Kiểm tra 1.5.

# --- Sensing ---
HAL_MOTION_ENABLED=true
HAL_EMOTION_ENABLED=true
HAL_POSE_MOTION_ENABLED=false         # không bật pose tracking đến khi biết CPU budget
HAL_MOTION_CONFIDENCE_THRESHOLD=0.4
HAL_EMOTION_CONFIDENCE_THRESHOLD=0.5
HAL_DL_ENCRYPTION=true
HAL_DL_ENCRYPTION_REQUIRED=false
SPEAKER_MATCH_THRESHOLD=0.75
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD=0.75

# --- Realtime voice ---
HAL_REALTIME_TURN_DETECTION=off
HAL_WARM_MIC=true
HAL_WARM_MIC_ECHO_SKIP_MAX_S=0.1
HAL_ECHO_RMS_FLOOR=300                # ? chỉnh: loa Reachy 5W,
                                      #   có thể cần floor khác so với Lamp 3W

# --- CPU tuning ---
# RPi CM4: 4 nhân chia sẻ với Pollen daemon (control loop đo được
# ~49 Hz trên con này, không phải 100 Hz như docs của Pollen ghi).
# Giữ số thread thấp để không bỏ đói daemon.
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
# ? Sau trinh sát nhiệt: cân nhắc thêm CPU governor hoặc thermal throttle
```

**Câu hỏi chỉnh sửa quan trọng trên phần cứng thật**:

1. **Ngưỡng VAD**: Servo motors của Reachy có thể tạo nhiều tiếng ồn cơ khí
   hơn servo Feetech của Lamp. Gate đang ở default của code là 2; servo ồn
   có thể cần `HAL_WEBRTCVAD_AGGRESSIVENESS=3`.
2. **Echo floor**: Loa 5W so với 3W của Lamp — `HAL_ECHO_RMS_FLOOR` có thể cần
   tăng để tránh tự kích hoạt khi phát TTS.
3. **Camera**: nếu Pi Camera v3 dùng libcamera, `VideoCapture(index)` của OpenCV
   có thể không hoạt động. Cần pipeline gstreamer hoặc tích hợp `picamera2`.
4. **Ngân sách CPU**: control loop của Pollen daemon đo được **~49 Hz** trên con
   này (`/api/daemon/status`); 100 Hz là con số docs của Pollen ghi. Sensing của
   chúng ta (phát hiện cảm xúc + chuyển động) tăng tải CPU. Theo dõi bằng `htop` khi
   chạy motion + inference đồng thời. Nếu CPU > 80%, tắt
   `HAL_POSE_MOTION_ENABLED` và giảm độ phân giải camera.

### 2.5 Motion Driver TODOs

Sau Giai đoạn 1, giải quyết các `TODO(spike)` trong `reachy_service.py`:

- [ ] Xác nhận hành vi `wake_up()` / `goto_sleep()`
- [ ] Xác nhận quy ước dấu (yaw dương = trái hay phải?)
- [ ] Test tất cả 28 ánh xạ emotion→HF move
- [ ] Kiểm tra recorded moves có cần truy cập Hugging Face lần đầu không
- [ ] Đo nhiệt độ dưới tải

## Giai Đoạn 3: Deploy & Kiểm Tra

### 3.1 Spike Deploy

Bộ script spike **chạy trên robot**, không chạy từ máy dev. Copy nguyên thư mục
sang rồi gọi một lệnh:

```bash
scp -r robots/reachy-mini pollen@reachy-mini.local:~/
ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
```

Không build gì ở máy dev: mọi artifact tải từ **OTA metadata**
(`https://cdn.autonomous.ai/os/ota/metadata.json`), đúng nguồn mà
`scripts/imager/build-orangepi.sh` và `scripts/provision/setup.sh` đọc. Đổi feed
bằng `OTA_METADATA_URL=…` hoặc `metadata_url` trong `/root/config/bootstrap.json`.
Cài vào **layout production** — `/opt/hal`, `/opt/devices`,
`/usr/local/bin/{os-server,bootstrap-server}`, `/usr/share/nginx/html/setup` —
chứ không phải một cây riêng cho spike.

`spike.sh` là orchestrator mỏng, gọi lần lượt sáu script con:

| Bước | Script | Làm gì |
|------|--------|--------|
| 1 | `spike-device.sh` | `devices.reachy-mini` từ OTA → `/opt/devices/reachy-mini`, rồi đắp `rootfs/` của gói lên `/` (nguồn của `/etc/asound.conf` và `/opt/hal/.env`) |
| 2 | `spike-hal.sh` | component `hal` → `/opt/hal`, `uv sync --python 3.12 --extra hardware --extra reachy`, uvicorn ở `127.0.0.1:5001` |
| 3 | `spike-os.sh` | binary `os-server` → `/usr/local/bin`, seed `/root/config/config.json`, chạy root với `WorkingDirectory=/root` |
| 4 | `spike-web.sh` | nginx + bundle `web` → `/usr/share/nginx/html/setup`, vhost `reachy-spike` |
| 5 | `spike-agent.sh` | Node.js 22 + `openclaw` theo pin OTA, seed `/root/.openclaw`, gateway ở loopback `18789` |
| 6 | `spike-bootstrap.sh` | worker OTA: seed `/root/config/bootstrap.json`, poll `5m` |

Chạy lẻ từng bước cũng được, cùng thứ tự đó — `device` **phải** trước vì HAL
không boot khi thiếu `ROBOT.md`, và `bootstrap` cố ý ở cuối vì nó có thể restart
os-server/hal ngay khi thấy build mới hơn:

```bash
sudo bash ~/reachy-mini/spike-device.sh
sudo bash ~/reachy-mini/spike-hal.sh
sudo bash ~/reachy-mini/spike-hal.sh --stop   # dừng HAL, trả media cho daemon
```

Cờ của orchestrator: `--no-deps` (bỏ `uv sync` của HAL), `--skip <bước>` (tên
bước: `device hal os web agent bootstrap`, lặp lại được), `--stop`,
`--uninstall`. Script con đều có `--stop` / `--uninstall`, thêm
`spike-hal.sh --no-deps`, `spike-device.sh --keep-env`,
`spike-bootstrap.sh --no-start`.

Mỗi bước cài một **systemd unit** (`hal`, `os-server`, `openclaw`, `bootstrap`),
nên cả stack sống qua reboot — không còn tmux. Xem log chung:

```bash
journalctl -u hal -u os-server -u openclaw -u bootstrap -f
```

`spike-web.sh` mới là thứ làm cho trình duyệt truy cập được: os-server bind
`127.0.0.1:5000` (`system/server/config/config.go`, `system/server/server.go`) và
không serve static, nên trước khi có nginx chỉ chạm được từ chính con Pi (hoặc
qua `ssh -L 5000:localhost:5000`). Vhost của nó giữ `/hw/` chỉ loopback, giống
production: phần cứng được chạm qua proxy có auth `/api/hardware/*` của os-server,
không gọi thẳng HAL.

`spike-os.sh` **từ chối cài** nếu `set_up_completed` chưa `true` **và**
`/usr/local/bin/device-ap-mode` tồn tại — bật os-server lúc boot khi đó sẽ đẩy
robot vào AP mode và mất WiFi. Pollen OS chưa có `uv`; `spike-hal.sh` tự cài vào
`/usr/local/bin`, nhưng `setup.sh` production cũng phải cài.

Board gate cũng phải được dạy phần cứng này: máy báo
`Raspberry Pi Compute Module 4 Rev 1.1`, không khớp entry nào trong `boards.json`
nên HAL từ chối boot. Đã sửa bằng cách thêm `raspberry_pi_cm4` và khai trong
`ROBOT.md`.

### 3.2 Smoke Test

Chạy **trên robot**: HAL bind `127.0.0.1:5001`, không nghe trên LAN. Từ máy dev
thì `ssh pollen@reachy-mini.local` trước, hoặc mở web UI ở `http://<IP>/`.

```bash
# Health
curl -s localhost:5001/health
curl -s localhost:5001/device

# Motion (thứ tự an toàn)
curl -s localhost:5001/servo/position
curl -s -X POST localhost:5001/servo/aim \
  -H 'content-type: application/json' \
  -d '{"direction":"center","duration":1.0}'
curl -s -X POST localhost:5001/servo/zero
curl -s -X POST localhost:5001/servo/release

# Âm thanh
curl -s -X POST localhost:5001/speaker/play \
  -H 'content-type: application/json' \
  -d '{"text":"Hello, I am Reachy"}'

# Camera
curl -s localhost:5001/camera/snapshot -o /tmp/snap.jpg

# os-server + web (cũng loopback cho tới khi nginx đứng trước)
curl -s localhost:5000/api/health/live
curl -s -o /dev/null -w '%{http_code}\n' localhost/
```

Xem ảnh trên máy dev: `scp pollen@reachy-mini.local:/tmp/snap.jpg .`

### 3.3 Test Production Setup

Chỉ sau khi spike hoạt động:

```bash
ssh pollen@<IP>
DEVICE_TYPE=reachy-mini bash setup.sh   # scripts/provision/setup.sh, khi đã có nhánh NM
# Khởi động lại
sudo reboot
# Xác nhận AP mode hoạt động
# Kết nối điện thoại vào AP reachy-mini-xxxx
# Hoàn tất setup flow qua captive portal
```

## Giai Đoạn 4: Cập Nhật Tài Liệu

Sau khi mọi thứ hoạt động, cập nhật:

- [ ] `robots/reachy-mini/docs/runtime.md` — điền tất cả TODO(spike)
- [ ] `robots/reachy-mini/docs/vi/runtime_vi.md` — bản tiếng Việt
- [ ] `robots/reachy-mini/docs/recovery.md` — xác nhận lệnh BLE hoạt động
- [ ] `robots/reachy-mini/docs/vi/recovery_vi.md` — bản tiếng Việt
- [ ] `robots/reachy-mini/rootfs/opt/hal/.env` — giá trị thật
- [ ] `robots/reachy-mini/rootfs/etc/asound.conf` — tên ALSA thật
- [ ] `CLAUDE.md` — nếu tạo thêm docs mới

## Giai Đoạn 5: Golden Base Image (Chụp Từ Device) & build-reachy.sh

Việc build image Reachy theo **đúng pattern lamp/intern-v2 đang dùng**: imager
không build trên stock vendor image — mà build trên base image **chụp từ một
device đã biết chạy tốt**. Xem `scripts/imager/README.md` ("Base image — per
device type"). Pollen GitHub release image (`recovery.md` Cấp D) chỉ là **phao
recovery**, KHÔNG phải build base.

### 5.1 Chụp base image TỪ device

Làm việc này **trước khi chạy `setup.sh`**, lúc eMMC còn là Pollen OS nguyên bản
(Phase 1 recon chỉ đọc nên recon trước rồi chụp cũng được). eMMC của CM4 không có
khe SD, nên việc chụp đi qua cùng đường rpiboot USB như khi reflash (`recovery.md`
Cấp D) — nhưng **đọc** thay vì ghi:

```bash
# 1. tắt robot → gạt SW1 sang DOWNLOAD → nối USB2 → chạy rpiboot → bật nguồn
sudo ./rpiboot -d mass-storage-gadget64          # eMMC hiện thành /dev/diskX (macOS) hoặc /dev/sdX (Linux)

# 2. unmount các phân vùng tự mount trước
sudo diskutil unmountDisk /dev/diskX             # macOS
# sudo umount /media/$USER/bootfs /media/$USER/rootfs   # Linux

# 3. đọc raw NGUYÊN đĩa (kèm bảng phân vùng) và nén luôn
sudo dd if=/dev/rdiskX bs=8m | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz    # macOS (chú ý rdiskX)
# sudo dd if=/dev/sdX bs=8M | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz     # Linux
```

Lưu thành base của imager theo layout lamp/intern:
`scripts/imager/input/reachy-mini/golden-reachy-dev.img.xz`. Tuỳ chọn mirror lên
CDN Autonomous như các base khác:
`gs://s3-autonomous-upgrade-3/os/imager/base/golden-reachy-dev.img.xz`.

### 5.2 Recover device TỪ base đã chụp

Cùng đường rpiboot USB như 5.1, ghi image đã chụp ngược lại. Hai cách:

```bash
# --- Cách A: dd (đơn giản nhất — chụp bằng dd thì restore bằng dd y hệt) ---
# tắt → SW1 DOWNLOAD → USB2 → rpiboot → bật nguồn → unmount (như 5.1)
xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/rdiskX bs=8m    # macOS
# xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/sdX bs=8M     # Linux

# --- Cách B: bmaptool (nhanh hơn, sparse — tạo .bmap 1 lần rồi flash) ---
xz -dc reachy-mini-base-v<pollen-ver>.img.xz > reachy-base.img
bmaptool create -o reachy-base.bmap reachy-base.img
sudo bmaptool copy reachy-base.img --bmap reachy-base.bmap /dev/rdiskX
```

Rồi khôi phục boot bình thường: tắt nguồn → gạt switch về **DEBUG** → rút USB →
bật nguồn. Kiểm tra bằng `reachyminios_check` (phải in `Image validation PASSED`)
và xác nhận robot cử động đúng (check calibration, xem 5.3).

### 5.3 Vì sao chụp, không tải Pollen release

- eMMC lúc ship có thể chứa driver, config, hoặc first-boot state mà release
  generic thiếu — chụp đảm bảo base khớp đúng phần cứng + daemon con máy đang chạy.
- Cùng lý do lamp/intern-v2 dùng image của hardware team thay vì stock `.7z`.
- **Cảnh báo — per-unit state**: dump eMMC của 1 con có thể chứa calibration riêng
  (servo/IMU offset) hoặc identity. Restore về **chính con đó** thì luôn an toàn.
  Trước khi tái sử dụng image đã chụp làm base cho **con khác**, phải verify cái gì
  là per-unit rồi strip/tạo lại — câu hỏi recon: sau khi flash image đã chụp sang
  một con *khác*, `reachyminios_check` có PASS **và** robot có cử động đúng không?

### 5.4 build-reachy.sh (imager target tương lai — chưa viết)

Sẽ mirror các phase của `build-orangepi.sh`, chỉnh lại:

| build-orangepi.sh | build-reachy.sh khác biệt |
|---|---|
| Phase 0 base: `gdown` stock `.7z` | giải nén `input/reachy-mini/golden-reachy-dev.img.xz` (chụp ở 5.1) |
| Phase 2 chroot apt: hostapd/dnsmasq/dhcpcd | **recon 1.2 đã chốt** — NetworkManager, `dhcpcd` tắt, nên không cần hostapd/dnsmasq/dhcpcd; tái dùng profile NM `Hotspot` sẵn có cho AP mode |
| Phase 2: bake nguyên stack OS | **cài chồng — không bao giờ xoá Pollen daemon** |
| Flash: SD card qua Imager | **rpiboot + bmaptool vào eMMC** — không có khe SD |
| Phase 3 OTA bake | như cũ: os-server, bootstrap, HAL, device profile overlay |

Chờ: chỉ còn câu hỏi per-unit-state (5.3). Recon 1.2 đã có đáp án —
NetworkManager đang chạy, `dhcpcd` tắt, và máy đã có sẵn `Glinks` (STA) cùng
`Hotspot` (`reachy-mini-ap`, `ipv4=shared`). Chỉ đáng build khi
ship NHIỀU con Reachy; với 1 con dev, `spike.sh` + `setup.sh`-cài-chồng là đủ.

## Tài Liệu Tham Khảo

- [Pollen OS build system](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini SDK](https://github.com/pollen-robotics/reachy_mini)
- [Thông số phần cứng](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Hướng dẫn reflash](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [BLE reset](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Autonomous setup.sh](../../../../scripts/provision/setup.sh)
- [Autonomous imager](../../../../scripts/imager/README.md)
