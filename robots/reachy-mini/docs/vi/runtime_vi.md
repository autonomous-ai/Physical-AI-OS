# Ghi Chú Runtime Reachy Mini

Đây là runbook riêng cho `robots/reachy-mini`. Tài liệu này chỉ ghi những phần
khác với nền tảng Autonomous chung và thiết bị tham chiếu Lamp.

## Tham Chiếu

Phần nào giống thì tham chiếu, không chép lại:

| Chủ đề | Tài liệu |
|--------|----------|
| Schema `ROBOT.md`, capability mounting, ngữ nghĩa `driver:` | [`robots/contract/ROBOT-SPEC.md`](../../../contract/ROBOT-SPEC.md) |
| Từ vựng capability | [`robots/contract/capabilities.md`](../../../contract/capabilities.md) |
| Layer capability/route/driver của HAL | [`docs/architecture/hal.md`](../../../../docs/architecture/hal.md) |
| Safety engine | [`docs/vi/safety_vi.md`](../../../../docs/vi/safety_vi.md) |
| Setup / AP mode / provisioning | [`docs/vi/setup-flow_vi.md`](../../../../docs/vi/setup-flow_vi.md) |
| Vision tracking của Lamp, hiện vẫn là reference cho phần tracking internals | [`robots/lamp/docs/vi/vision-tracking_vi.md`](../../../lamp/docs/vi/vision-tracking_vi.md) |

Nguồn hardware đã kiểm tra ngày 2026-07-21:

- Pollen / Hugging Face Space: <https://huggingface.co/spaces/pollen-robotics/Reachy_Mini>
- Trang chính thức Reachy Mini: <https://www.reachy-mini.org/>
- Datasheet hardware của Seeed Studio: <https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_hardware/>
- Memory Claude Code của dự án: `reachy-mini-port`

## Profile Này Khai Gì

`ROBOT.md` khai route surface sau:

| Capability | Routes | Required | Ghi chú riêng cho Reachy |
|------------|--------|----------|--------------------------|
| `audio` | `audio`, `speaker`, `voice` | yes | Bản Wireless có mảng 4 mic và loa 5 W |
| `vision` | `camera` | yes | Camera góc rộng nằm trong đầu |
| `motion` | `servo` | yes | `driver: reachy_sdk`; đầu Stewart-platform, body yaw, antenna |
| `expression` | `emotion` | yes | Biểu cảm bằng chuyển động, antenna và giọng nói |
| `media` | `music` | yes | Phát nhạc dùng chung card USB duy nhất với TTS |
| `sensing` | `sensing` | no | Perception stack optional; gate giống các device khác |
| `presence` | none | no | Chỉ là behavior gate |
| `lifelike` | none | no | Chỉ là behavior gate; idle suite routeless nằm ở os-server |
| `companion` | `buddy` | no | `buddy` là route của **os-server**, nên HAL không có driver cho nó |
| `system` | `system` | yes | HAL system route chung |

Profile này cố ý **không** khai `light`, `display`, hoặc `scene`.
Các nguồn Pollen/Hugging Face/Seeed hiện liệt kê motion, camera, mảng mic, loa,
compute, IMU, Wi-Fi, pin và antenna có animation, nhưng không liệt kê LED ring
hay màn hình có thể điều khiển như một capability của device. Nếu revision sau
có LED/screen addressable, chỉ thêm capability khi đã có HAL driver và hành vi
safety tương ứng.

**Khai không đồng nghĩa với mounted.** Lúc boot HAL giao nhau phần khai với
việc driver có nạp được hay không (`plan_mounts` trong `hal/board/device.py`):
khai + có driver thì mount, khai + *required* + thiếu thì fail loud, khai +
*optional* + thiếu thì skip. Vì vậy trên con Wireless `GET /device` trả về:

```
routes:  [audio, camera, emotion, music, sensing, servo, speaker, system, voice]
skipped: [buddy]
```

— `companion` được khai nhưng optional, và HAL không có driver `buddy` nào, nên
route đó bị skip chứ không làm sập boot.

## Deploy: cài chồng, không bao giờ flash

Reachy Mini ship kèm **OS Pollen** trên Pi, chứa daemon own serial bus, control
loop, inverse kinematics và safety clamp. **Flash golden image = xoá daemon =
cục gạch.** Autonomous luôn được cài chồng lên.

Bộ script spike **chạy trên chính robot**, không chạy từ máy dev. Copy nguyên thư
mục sang rồi gọi một lệnh:

```bash
scp -r robots/reachy-mini pollen@reachy-mini.local:~/
ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
```

Không có gì được build ở máy dev. Mọi artifact tải từ **OTA metadata**
(`https://cdn.autonomous.ai/os/ota/metadata.json`) — đúng nguồn mà
`scripts/imager/build-orangepi.sh` và `scripts/provision/setup.sh` đọc, nên robot
spike chạy đúng build cả fleet đang chạy, và bug tái hiện ở đây mới nói được điều
gì đó về máy người khác. Đổi feed bằng `OTA_METADATA_URL=…` hoặc field
`metadata_url` trong `/root/config/bootstrap.json`.

Ký OTA là tuỳ chọn. Khi `/root/config/bootstrap.json` có
`signing_public_key` Ed25519 base64 đã được pin, `install.sh`, các spike script
và `software-update` sẽ xác thực envelope `signed` của feed và `sha256` của mỗi
ZIP trước khi giải nén. Không có field này thì luồng metadata và tải legacy vẫn
được giữ nguyên, nên robot đã provision trước đó vẫn update bình thường.
Để fresh install có xác thực, truyền key vào one-liner, ví dụ
`curl -fsSL …/install.sh | sudo env OTA_SIGNING_PUBLIC_KEY=… bash`; key được pin
trong `bootstrap.json` trước khi bất kỳ component script OTA nào chạy.

Trước khi thay `os-server` hoặc `bootstrap-server`, `software-update` giữ binary
cũ tại `/root/bootstrap/rollback/`. Dùng `sudo software-update rollback
os-server` (hoặc `bootstrap`) để khôi phục; version lỗi bị chặn tới khi feed có
version khác.

Web OTA cũng theo cùng model recovery. Nó stage và xác thực bundle mới trước khi
thay `/usr/share/nginx/html/setup`, giữ bundle trước đó tại
`/root/bootstrap/rollback/web.previous`, và ghi lại nginx có đang chạy hay không.
Update yêu cầu có `index.html`, `nginx -t`, và `GET /` loopback khi nginx đang
active; check lỗi sẽ tự khôi phục bundle known-good. Dùng
`sudo software-update rollback web` khi operator cần rollback chủ động.

Device-profile OTA stage package trước khi swap
`/opt/devices/reachy-mini`, lưu profile trước đó và mọi rootfs target bị ảnh
hưởng, sau đó phục hồi trạng thái service `os-server` và HAL cũ. Nó giữ file
tuning local `/opt/hal/.env`. Profile mới phải có `ROBOT.md`; mỗi service trước
đó active phải qua health endpoint loopback, nếu không profile known-good sẽ tự
được phục hồi. Dùng `sudo software-update rollback device` để recovery thủ công.

HAL OTA giữ toàn bộ runtime `/opt/hal` trước đó—`.env`, virtual environment và
uv cache—tại `/root/bootstrap/rollback/hal.previous`. Runtime mới được giải nén
và sync trong thư mục staging kề bên, chỉ sau đó mới swap vào vị trí chạy. HAL
trở về trạng thái active/inactive trước đó và service đang active phải trả về
`http://127.0.0.1:5001/health`. Mọi lỗi staging hoặc health sẽ tự phục hồi
runtime known-good. Dùng `sudo software-update rollback hal` để recovery thủ công.

Layout là **layout production**, không phải cây riêng cho spike:

| Thành phần | Đường dẫn |
|------------|-----------|
| HAL | `/opt/hal` (venv `/opt/hal/.venv`, `.env` do device package ship) |
| Device profile | `/opt/devices/reachy-mini` |
| Binary Go | `/usr/local/bin/os-server`, `/usr/local/bin/bootstrap-server` |
| Config | `/root/config/config.json`, `/root/config/bootstrap.json` |
| Web bundle | `/usr/share/nginx/html/setup` |

`spike.sh` cố ý chỉ là **orchestrator mỏng**: nó gọi lần lượt sáu script con và
không làm lại việc của script nào. Bản trước đó chép lại logic của chúng, lệch
nhau trong vòng một tuần, và kết cục là chạy os-server trên sai thư mục config
trong khi mọi service vẫn báo healthy.

| Bước | Script | Làm gì |
|------|--------|--------|
| 1 | `spike-device.sh` | Tải `devices.reachy-mini` từ OTA về `/opt/devices/reachy-mini` **và** đắp `rootfs/` của gói đó lên `/` — đây là nguồn của `/etc/asound.conf` và `/opt/hal/.env`. Thiếu `ROBOT.md` thì HAL không boot |
| 2 | `spike-hal.sh` | Tải component `hal` về `/opt/hal`, `uv sync --python 3.12 --extra hardware --extra reachy`, chạy uvicorn ở `127.0.0.1:5001` |
| 3 | `spike-os.sh` | Tải binary `os-server` về `/usr/local/bin`, seed `/root/config/config.json` tối thiểu, chạy **dưới root với `WorkingDirectory=/root`** |
| 4 | `spike-web.sh` | Cài nginx, tải bundle `web` về `/usr/share/nginx/html/setup`, viết vhost `reachy-spike` |
| 5 | `spike-agent.sh` | Cài Node.js 22 (NodeSource) + `openclaw` đúng version OTA pin, seed `/root/.openclaw`, chạy gateway ở loopback `18789` |
| 6 | `spike-bootstrap.sh` | Worker OTA: seed `/root/config/bootstrap.json`, cài `software-update` được `upload-device.sh` stage sẵn ở gốc device package (bản canonical `scripts/provision/software-update`) → `/usr/local/bin/software-update` (worker exec script này để áp update; thiếu nó thì mọi lần apply đều fail `executable file not found in $PATH` trong khi mọi unit vẫn báo healthy), poll feed mỗi `5m` |

`config.json` mà bước 3 seed **bắt buộc phải có `openclaw_config_dir`**. Key
thiếu trong file KHÔNG rơi về giá trị `Default()` ở
`system/server/config/config.go` — cả `Load` lẫn `ProvideConfig` đều unmarshal
vào struct zero-value, nên thiếu key nghĩa là `""` chứ không phải
`/root/.openclaw`. os-server tìm gateway token bằng
`filepath.Join(OpenclawConfigDir, "openclaw.json")`; dir rỗng cho ra đường dẫn
*tương đối* `openclaw.json` → `/root/openclaw.json`. File đó không bao giờ tồn
tại: token không đọc được, websocket của agent reconnect 5s/lần vô hạn,
`WaitForAgentReady` không bao giờ xong — và log không có lỗi nào, vì phép join
trả về một path hợp lệ, chỉ là sai chỗ. Lỗi này chỉ lộ khi cài sạch hoàn toàn,
do `config.json` bình thường vẫn sống sót qua uninstall.

Thứ tự có lý do. `device` phải chạy trước vì mọi thứ khác đọc file nó cài.
`bootstrap` để **cuối cùng**: nó có thể restart os-server và hal ngay khi thấy
build mới hơn, làm việc đó giữa lúc đang cài dở biến một lượt bring-up sạch thành
race.

Mỗi bước cài một **systemd unit** (`hal`, `os-server`, `openclaw`, `bootstrap`),
nên cả stack sống qua reboot. Không còn tmux.

```bash
sudo bash spike.sh                  # bring-up đầy đủ
sudo bash spike.sh --no-deps        # bỏ `uv sync` của HAL (chạy lại nhanh)
sudo bash spike.sh --skip agent     # bỏ một hoặc nhiều bước (lặp lại được)
sudo bash spike.sh --stop           # dừng tất cả, thứ tự ngược
sudo bash spike.sh --uninstall      # dừng + gỡ unit và artifact
```

Mọi script con đều nhận `--stop` và `--uninstall`. Cờ riêng: `spike-hal.sh
--no-deps`, `spike-device.sh --keep-env` (giữ nguyên `/opt/hal/.env` đang có),
`spike-bootstrap.sh --no-start` (cài và enable, không start).

`spike-lib.sh` là thư viện dùng chung mà mọi script source vào — fetch metadata,
`ota_unpack` / `ota_install_binary`, `write_unit` / `start_unit`, `wait_http`.
Snapshot metadata cache ở `/tmp/.spike-ota-metadata.json`; `spike.sh` xoá nó ở
đầu mỗi lượt rồi để sáu bước dùng chung **một** snapshot, nên một bản publish rơi
vào giữa lượt không thể đẩy os-server và hal sang hai build lệch nhau. Chạy lại
một script con lẻ thì cache không tự xoá — xoá tay hoặc mở shell mới.

Hai guard đáng nhớ:

- `spike-os.sh` **từ chối cài** nếu `set_up_completed` chưa `true` **và**
  `/usr/local/bin/device-ap-mode` tồn tại: bật os-server lúc boot khi đó sẽ gọi
  `SwitchToAPMode()`, phá WiFi station và mất robot qua SSH.
- `spike-hal.sh` đòi tối thiểu **4 GB** trống trên `/` trước khi `uv sync` (venv
  ~2 GB, cache uv chừng đó nữa, trên eMMC 14 GB đã đầy ~60%). Với `--no-deps` thì
  bỏ qua kiểm tra này.

Vhost spike **không phải** vhost production — bản thật do
`scripts/provision/setup.sh` viết vào `/etc/nginx/conf.d/<type>.conf`, bản spike
nằm ở `/etc/nginx/sites-available/reachy-spike`. `/hw/` trong đó **chỉ loopback**
(`allow 127.0.0.1; deny all`), giống production: trình duyệt chạm phần cứng qua
proxy có auth `/api/hardware/*` của os-server, không bao giờ gọi thẳng HAL.

`spike-agent.sh` cố tình lệch bản cài của Lamp hai chỗ: không chromium/xvfb (chỉ
phục vụ skill computer-use — tiết kiệm ~600 MB trên eMMC 14 GB), và không seed
skills (os-server tự sync + prune lúc boot). Version openclaw lấy từ
`ota_field openclaw version` — component duy nhất trong feed không có `url` vì nó
là package npm — rồi bỏ số 0 đệm (`2026.06.10` → `2026.6.10`) trước khi
`npm install`, nếu không npm đọc spec như dist-tag và chết với "No matching
version found". `OPENCLAW_VERSION=…` override được pin đó.

## Recon Máy Thật (đo ngày 2026-07-29)

Các số liệu dưới đây đo trên con Wireless đầu tiên (`hardware_id
e4a0ef5f04fafb94`) bằng [`../../recon.sh`](../../recon.sh) cộng vài probe bổ
sung. Chúng thay thế phần phỏng đoán mà tài liệu này từng ghi.

| Hạng mục | Đo được |
|----------|---------|
| Board | `Raspberry Pi Compute Module 4 Rev 1.1`, RAM 3.7 GiB, 46 °C lúc idle |
| OS / kernel | Debian 13.3 (trixie), `6.12.62+rpt-rpi-v8`, aarch64 |
| Ổ đĩa | eMMC 14 GB, dùng 7.7 GB / trống 5.5 GB (59 %) |
| Boot config | `/boot/firmware/config.txt` — `imx708` cam0+cam1, `uart3`, quạt i2c (`emc2301`), IMU trên i2c4 |
| Network stack | **NetworkManager** active (`wpa_supplicant` active, `dhcpcd` inactive) |
| Profile NM | `Glinks` (STA) + `Hotspot` (`mode=ap`, ssid `reachy-mini-ap`, `ipv4=shared`, `autoconnect=false`) |
| Unit của Pollen | `reachy-mini-daemon.service` (AP launcher → daemon), `reachy-mini-bluetooth.service` (GATT), `gpio-shutdown-daemon.service` |
| Daemon | `reachy_mini` 1.9.0 trong `/venvs/mini_daemon` (Python 3.12), chạy dưới user `pollen` |
| Port daemon | `:8000` REST+WS, `:8443`. Port của mình — `5001` (HAL), `5000` (os-server, chỉ loopback), `80` — đều trống |
| WS path daemon | `/ws/sdk`, `/ws/daemon`, `/ws/full`, `/ws/raw`, `/ws/set_target`, `/ws/apps`, `/ws/logs`, `/ws/updates` |
| Control loop | đo được ~**49 Hz** (`/api/daemon/status`), không phải 100 Hz như hay được nhắc |
| Audio | một card USB duy nhất: `card 0: Audio [Reachy Mini Audio], device 0` — vừa thu vừa phát |
| Camera | CSI `imx708_wide` (4608×2592 10-bit RGGB) qua unicam/libcamera; có `rpicam-apps` + `gstreamer1.0-libcamera`, **chưa** có `python3-picamera2` |
| Python hệ thống | 3.13.5; **chưa** cài `uv`; `libcairo2-dev` + `libgirepository1.0-dev` + `pkg-config` đã có sẵn |
| SSH | `pollen@reachy-mini.local` (mật khẩu `root`); SSH thẳng bằng `root@` bị từ chối |
| Recovery | có `/restore/venvs/`; BLE GATT đang chạy (`bluetoothd`, tên `reachy-mini`) |

Hai hệ quả không có trong plan port ban đầu, mô tả ngay bên dưới: daemon giữ
media, và camera không phải thiết bị UVC.

## Quyền Sở Hữu Media: daemon giữ camera và audio

Mặc định daemon Pollen mở sẵn `/dev/video0`, `/dev/video1`, các node ISP, và
**cả hai** PCM ALSA (`pcmC0D0c` thu, `pcmC0D0p` phát). Tiến trình khác không
giành được:

```bash
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 1 /tmp/t.wav
# arecord: main:850: audio open error: Device or resource busy
```

Daemon có sẵn API bàn giao đúng cho tình huống này:

```bash
curl -s -X POST http://localhost:8000/api/media/release   # {"status":"ok"}
curl -s        http://localhost:8000/api/media/status     # {"available":false,"released":true,"no_media":false}
# ... HAL sở hữu mic, loa, camera ...
curl -s -X POST http://localhost:8000/api/media/acquire   # trả lại
```

Đã verify trên máy: sau `release`, `arecord` thu được và `rpicam-jpeg` chụp được
khung 1280×720; sau `acquire`, media status trở lại
`{"available":true,"released":false}`. Daemon vẫn `active` và trả HTTP suốt quá
trình — nhả media **không** ảnh hưởng điều khiển motion.

**HAL tự làm việc bàn giao này** — không phải script, không phải launcher.
`ROBOT.md` khai `owner: pollen_daemon` trên capability `audio` và `vision`;
`hal/drivers/media_owner/factory.py` map tên đó tới `PollenDaemonMediaOwner`,
gọi `release` ở đầu startup và `acquire` khi shutdown. `release` retry 5 lần cách
nhau 2 s vì daemon là systemd service khởi động song song với HAL và có thể chưa
listen lúc cold boot; `acquire` chỉ thử một lần vì lúc đó systemd đang đếm ngược
tới SIGKILL, và lượt `release` sau sẽ tự sửa nếu lỡ.

Lý do phải nằm trong tiến trình HAL là **thứ tự**: release phải xong trước khi
HAL probe audio. Thua cuộc đua đó thì hỏng âm thầm và hỏng toàn bộ — card còn bị
daemon giữ nên PortAudio không probe nổi sample rate nào, output ALSA đã cấu hình
không enumerate, TTS chốt ở device -1 và raise mọi lần nói, trong khi mọi endpoint
status vẫn báo healthy.

Không script spike nào gọi `/api/media/*` nữa. Ngoại lệ duy nhất là một lệnh
`acquire` best-effort trong `spike-hal.sh --stop`, dành cho HAL bị kill đủ gắt để
bỏ qua bước trả media — thiếu nó thì daemon nằm im, câm và mù, kéo theo cả app
stack của Pollen.

Một tác dụng phụ đã đo: handler `release` của daemon reset luôn mixer của card về
mức của nó (90% trước khi gọi, 62% sau). Driver ghi mức đúng xuống qua route
`/audio/volume` ngay sau release, nếu không một lần restart HAL đơn thuần sẽ để
loa ở mức của Pollen trong khi slider, file state và agent đều vẫn báo mức của
người dùng. Có mức đã persist thì lấy mức đó; chưa có thì lấy `startup_volume`
trong ROBOT.md của thân máy — máy chưa ai đụng slider mà bỏ qua sẽ nằm luôn ở
mức −23 dB của daemon, và lần boot đầu của người dùng mới đọc y như hỏng loa.

Việc này đi kèm — chứ không thay thế — `media_backend="no_media"` của SDK, vì
tham số đó chỉ ngăn *SDK client* giành media.

## Camera Stack: libcamera, không phải UVC

Camera trong đầu là **CSI `imx708_wide`** (Camera Module 3 wide) chạy qua pipeline
unicam + libcamera của Raspberry Pi. `/dev/video0` là node unicam Bayer thô, không
phải luồng YUV đọc thẳng được:

| Probe | Kết quả |
|-------|---------|
| `cv2.VideoCapture(0)` | `isOpened() == True`, `read() -> False` (`select() timeout`) |
| `cv2.getBuildInformation()` | `GStreamer: NO` — bản `opencv-python` từ wheel không dùng được pipeline `libcamerasrc` |
| `rpicam-jpeg -o t.jpg --width 1280 --height 720` | chạy được (JPEG 216 KB) |
| `python3-picamera2` | chưa cài; apt có candidate `0.3.33-1` |

**Đã giải quyết (2026-07-29)** bằng một camera backend thứ hai, không phải bằng
giá trị config. `ROBOT.md` chọn backend giống hệt cách motion chọn driver:

```yaml
vision:
  routes: [camera]
  driver: rpicam
  required: true
```

`hal/drivers/camera/factory.py` map tên đó tới `RpicamVideoCaptureDevice`: chạy
`rpicam-vid --codec mjpeg -o -` như tiến trình con, tách luồng theo marker JPEG,
decode frame mới nhất bằng `cv2.imdecode`. Cả hai backend đều thoả
`VideoCaptureDeviceBase`, nên routes, sensing và tracker không hề biết đang chạy
cái nào. Lamp khai `driver: opencv` cho đường UVC.

Vì sao không chọn hai hướng kia:

- **`python3-picamera2`** — gói apt build cho python hệ thống (3.13 trên trixie),
  còn venv của HAL là 3.12; extension nhị phân của libcamera không nạp chéo
  phiên bản. Dùng nó đồng nghĩa đẩy toàn bộ HAL sang 3.13.
- **Camera qua daemon** — `/api/media/release` bàn giao camera **và** audio cùng
  lúc, nên lấy frame từ daemon sẽ kéo theo phải cho audio đi qua daemon nốt,
  thay thế một đường đang chạy tốt.

Đo trên máy: 1280×720 MJPEG yêu cầu 15 fps cho ra ~14 fps, tốn ~21% một core,
trong khi daemon vẫn chạy control loop. Driver idle ở 5 fps và chỉ lên 15 khi có
consumer đăng ký (`acquire_consumer()`) — lúc đó nó respawn tiến trình con vì
frame rate là tham số dòng lệnh. `HAL_CAMERA_INDEX` vô tác dụng với backend này
(libcamera chọn sensor theo pipeline), và `requires_v4l2_index = False` giúp boot
không đi dò `/dev/video*` cho node sẽ không bao giờ mở.

Điểm còn phải tune: ở mức idle 5 fps, libcamera có thể chọn thời gian phơi sáng
đủ dài để làm nhoè vật thể chuyển động. Nâng idle rate hoặc chặn trần
`--shutter` khi cần ảnh tĩnh nét hơn là cần tiết kiệm CPU.

## Motion Driver

Reachy chọn motion backend của HAL qua:

```yaml
motion:
  routes: [servo]
  driver: reachy_sdk
  required: true
  safety: SAFETY.md#motion
```

HAL resolve cấu hình này tới `hal/drivers/motors/reachy_service.py` qua
`hal/drivers/motors/factory.py`. Driver implement contract chung
`MotionService`, nên `hal/routes/servo.py` vẫn hardware-neutral.

Driver là client mỏng tới daemon của Pollen:

```bash
REACHY_DAEMON_HOST=localhost
REACHY_DAEMON_PORT=8000
```

Joint keys của Reachy trong HAL dùng độ/mm, dù SDK dùng radian/mét:

| Joint key | Ý nghĩa |
|-----------|---------|
| `head_x.pos`, `head_y.pos`, `head_z.pos` | Dịch chuyển đầu, mm |
| `head_roll.pos`, `head_pitch.pos`, `head_yaw.pos` | Xoay đầu, độ |
| `body_yaw.pos` | Xoay thân, độ |
| `antenna_left.pos`, `antenna_right.pos` | Góc antenna, độ |

Được hỗ trợ qua các endpoint `/servo` chung:

- pose/readiness: `/servo`, `/servo/position`, `/servo/status`
- chuyển động: `/servo/move`, `/servo/aim`, `/servo/nudge`
- recovery/mode: `/servo/zero`, `/servo/hold`, `/servo/release`, `/servo/resume`
- expression moves: `/servo/play` khi recorded-move library của Reachy sẵn sàng

### Emotion → HF Move Mapping

Tên emotion của HAL (CSV stem trên Lamp) được map sang HF move của Pollen trong
`_MOVE_MAP` (reachy\_service.py). Map dùng hằng số preset từ `hal/presets.py` và
nhắm tới các move trong
[pollen-robotics/reachy-mini-emotions-library](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library)
(85 move). Ví dụ:

| HAL emotion | HF move |
|-------------|---------|
| `curious` | `curious1` |
| `happy_wiggle` | `cheerful1` |
| `sad` | `sad1` |
| `greeting` | `welcoming1` |
| `nod` | `yes1` |
| `headshake` | `no1` |
| `music_groove` | `dance1` |

Tên chưa map sẽ được thử nguyên văn (caller có thể gửi thẳng tên HF). 8 style
nhạc được chia cho 3 dance move của Pollen. Test bảo vệ việc phủ đủ preset và
kiểm tra mọi giá trị trong map có trong thư viện HF.

`GET /servo` trả `available_recordings` cùng vốn từ với field `current`: move đã
map thì liệt kê theo tên HAL (`music_groove`, không phải `dance1`), phần còn lại
của thư viện giữ nguyên tên HF. Trước đây list toàn tên HF nên hai field lệch
nhau — web monitor highlight mục trùng `current`, thành ra không bao giờ
highlight khi đang chạy move đã map.

### Vòng lặp groove theo nhạc

`POST /audio/play` phát `music_start` (kèm style đã detect) lúc bắt đầu và
`music_stop` lúc kết thúc — `hal/routes/music.py`. Emotion tương ứng trên cùng
đường đó chỉ áp LED/display (`_apply_emotion_led_display`), nên phần servo
hoàn toàn đến từ hai event này.

Driver xử lý cả hai: `music_start` set groove và thread play lặp lại move đó cho
tới khi `music_stop`, giống backend Feetech
(`animation_service._continue_playback`). Một dance move chỉ dài vài giây, nên
nếu không lặp thì robot nhảy một lần rồi đứng im hết bài. Emotion phát giữa bài
chạy one-shot xong trả servo lại cho groove; `hold`, `zero`, `release` và
shutdown thì dừng hẳn. Mọi thứ còn lại vẫn one-shot — `/servo/play` thường
không bao giờ lặp.

### Mỗi lúc chỉ một nguồn ghi

Recorded move và `goto_target`/`set_target` là hai luồng target độc lập vào
daemon — daemon nhận cả hai, mỗi chu kỳ điều khiển bên nào ghi sau thì thắng,
nên aim giữa lúc đang chạy animation trông như motor đánh nhau. Vì vậy mọi lệnh
pose trực tiếp (`move_to`, `send_positions`, `aim`, `nudge`, `zero`, `hold`,
`release`, `freeze`) phải giành servo trước: vô hiệu thread play và huỷ move
đang chạy. Lệnh one-shot trả servo lại cho groove sau khi hết duration; còn
`send_positions` của tracker thì giữ luôn. Hai thread play cũng không thể chồng
nhau — mỗi pass giữ lock và kiểm tra lại quyền sở hữu trước khi bắt đầu, nên
thread đang kẹt trong lần tải thư viện HF đầu tiên (chậm) không bao giờ stream
đè lên thread mới.

Backend Feetech có sẵn tính chất này: `aim` dừng event loop trước khi move, và
loop đó là nguồn ghi duy nhất.

### Thư viện move

Move được tra trên nhiều dataset HF theo thứ tự — dataset ĐẦU TIÊN có tên đó
thắng. Mặc định tìm trong hai thư viện, `pollen-robotics/reachy-mini-emotions-library`
và `pollen-robotics/reachy-mini-dances-library`; cả hai đều được daemon tải sẵn
lúc khởi động nên tra thêm không tốn lượt tải nào.

`HAL_REACHY_MOVES` chèn dataset lên đầu thứ tự đó:

```bash
HAL_REACHY_MOVES="me/my-moves,someone/their-moves"
```

Move bạn tự ghi rồi đẩy lên Hub sẽ phát được bằng tên qua `/servo/play`, và gắn
vào `_MOVE_MAP` thì đứng sau một emotion. Vì dataset tự chọn được xếp trước, đặt
trùng tên với move chính chủ sẽ **che** move đó — đây chính là cách thay một
emotion mặc định bằng bản của mình.

Hai hệ quả cần biết trước khi thêm:

- Dataset daemon chưa tải sẵn sẽ được tải lúc dùng lần đầu, ngay trong thread
  phát. Nên lần phát đầu của một community move có thể chậm; robot không treo,
  nó đang tải.
- Thư viện nạp lỗi (đổi tên, bị gỡ khỏi Hub, mất mạng) chỉ bị bỏ qua kèm warning
  chứ không kéo theo các thư viện khác — mất một dataset cộng đồng không được
  phép làm robot mất luôn emotion chính chủ.

Mọi move đều đi qua speed gate bất kể đến từ đâu, xem **Safety Delta**.

### Ramp khi vào animation

Move của Pollen là quỹ đạo tuyệt đối bắt đầu ở frame 0 của chính nó, mà
`initial_goto_duration` của `play_move` mặc định `0.0` — daemon giật đầu tới đó
từ vị trí move trước bị cắt ngang. Giờ mọi lần play đều truyền ramp
(`HAL_REACHY_PLAY_RAMP_S`, mặc định 0.5s — ngắn hơn `HAL_SERVO_PLAY_RAMP_S` 2.0s
của Feetech vì move Pollen chỉ ~3s và ramp bị tính thêm vào). Khi frame 0 ở xa
pose hiện tại, ramp được kéo dài theo `motion.max_speed` trong SAFETY.md, đúng
hàm `min_move_duration` mà aim/nudge dùng — vì vậy driver nhận safety policy
ngay lúc khởi tạo (`server.py`), do không có route nào mang giúp.

Frame 0 đọc qua `RecordedMove.evaluate(0.0)`, trả về đúng bộ ba (head pose,
antennas, body yaw) mà hàm chuyển joint đang dùng.

### Suppression, hold và freeze

Suppression được tách đúng theo cách các route đọc, không gộp thành một cờ:

| Cờ | Set bởi | Tác dụng |
|----|---------|----------|
| `_released` | `/servo/release` | tắt torque — mọi play bị từ chối tới khi `/servo/resume` |
| `_zero_mode` | `/servo/zero` | đã park; `/servo/play` bị từ chối (`is_suppressed`) |
| `_hold_mode` | `/servo/hold`, scene preset | không có motion tự phát; `/servo/play` bị từ chối |
| `_hold_explicit` | chỉ `/servo/hold` | `routes/emotion.py` từ chối cả emotion scene-change |
| `_frozen` | camera capture | không motion tự phát khi consumer còn giữ freeze |

`is_suppressed` = zero ∨ hold ∨ released, khớp backend Feetech. Emotion là quyết
định của emotion route: route đọc `_hold_mode`/`_hold_explicit`, driver tôn trọng
những gì route cho qua. Trước đây Reachy thiếu cả hai cờ nên route tưởng không
hold, vẫn dispatch, còn driver thì drop kèm mỗi dòng log `debug` — robot im lặng
sau khi hold mà không rõ lý do.

`freeze()` không bao giờ huỷ move đang chạy: snapshot vision diễn ra thường
xuyên, chặt move cho từng lần vừa phá animation đang xem vừa làm groove khởi động
lại từ frame 0 mỗi vài giây. Thay vào đó nó chặn pass **kế tiếp**, nên đầu đứng
yên ngay khi move hiện tại kết thúc và giữ yên suốt thời gian freeze;
`unfreeze()` chỉ bật lại groove nếu freeze thật sự kéo dài qua hết một pass.
Feetech đạt cùng mục tiêu bằng cách tạm ngưng ghi servo rồi chạy tiếp giữa
recording — điều mà player nằm ở daemon không làm được.

Khác biệt đã biết so với Lamp:

- Upload CSV servo recording là concept của Feetech/Lamp; `add_recording` của
  Reachy hiện no-op cho tới khi quyết định uploaded moves có cần cho body này
  hay không.
- Idle/ambient motion do daemon hoặc recorded-move library quản lý, không phải
  event loop Feetech — groove theo nhạc là vòng lặp client-side duy nhất.
- `/servo/track` chưa production-ready cho Reachy. `tracker_service` chung vẫn
  chạm vào internals kiểu Lamp/Feetech và cần chuyển sang accessor của
  `MotionService` trước.

## Safety Delta

Machine bounds hiện tại trong `SAFETY.md`:

```yaml
motion:
  max_speed: 800
  stop_always: true
```

Safety layer chung của HAL kéo dài duration để giữ `max_speed` cho move được
yêu cầu (aim/nudge/goto). **Recorded move** thì không kéo dài được: daemon
stream cả trajectory còn HAL đứng ngoài vòng, nên move bị quét trước khi phát và
bị từ chối nếu tốc độ xoay đầu đỉnh vượt trần — `peak_head_dps()` /
`_move_refused()` trong driver.

`max_speed` từng là `60` khi nó mới chỉ là số tạm; quét cả 85 move của
`pollen-robotics/reachy-mini-emotions-library` cho ra median 111 deg/s, p90 293
và max 766 — tức 60 sẽ từ chối 63 move của chính hãng. 800 nằm ngay trên trần
của chính hãng và VẪN cần đo trên phần cứng thật, xem `robots/reachy-mini/SAFETY.md`.

Tốc độ đo bằng góc quay thật giữa hai hướng đầu trên cửa sổ tối thiểu 20 ms. Sai
phân từng trục euler bị vọt ở điểm gimbal, còn lấy từng cặp frame thô thì biến
nhiễu timestamp thành tốc độ đầu không bao giờ đạt (`wake-mini-up` đọc ra 17226
deg/s từ một khoảng 1 ms, đo đúng thì là 766). Chuyển động tịnh tiến của đầu
không bị bound: `max_speed` tính bằng deg/s còn `head_x/y/z` là milimet.

`stop`, `zero`, `hold`, và `release` vẫn là hành động recovery deterministic.

Chưa thêm block `thermal` cho tới khi đo được thermal profile của Raspberry Pi
trên bản Wireless thật.

## Checklist Khởi Động Kiểm Tra

1. Kiểm tra static profile:

   ```bash
   python3 -m unittest devices.contract.cts.test_compatibility
   ```

2. Cài dependency Reachy — chỉ trên robot, và `spike-hal.sh` đã làm sẵn bước này:

   ```bash
   cd /opt/hal
   uv sync --python 3.12 --extra hardware --extra reachy
   ```

   Giữ `reachy` tách khỏi extra `hardware` chung. Reachy SDK kéo các dependency
   Linux GUI/media như pygobject/pycairo, không nên ép vào image Lamp.

3. Boot HAL với profile Reachy. Unit `hal` chạy đúng lệnh này với `DEVICE_TYPE`
   và `DEVICES_DIR` lấy từ `EnvironmentFile=/opt/hal/.env`; chạy tay để debug thì:

   ```bash
   systemctl status hal          # đường bình thường
   # hoặc chạy tay:
   cd /opt/hal && DEVICE_TYPE=reachy-mini DEVICES_DIR=/opt/devices \
     .venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
   ```

   Bind `127.0.0.1`, không phải `0.0.0.0`: trình duyệt vào HAL qua proxy
   `/api/hardware/*` của os-server, giống production.

4. Xác nhận mounted routes:

   ```bash
   curl -s http://localhost:5001/device
   curl -s http://localhost:5001/health
   ```

   Khi các driver required đều sẵn sàng, expected routes là `audio`, `camera`,
   `emotion`, `music`, `sensing`, `servo`, `speaker`, `system`, `voice`, còn
   `buddy` nằm trong `skipped` (khai optional, HAL không có driver). `led` và
   `display` phải vắng mặt hoàn toàn — chúng không được khai.

5. Verify motion theo thứ tự an toàn:

   ```bash
   curl -s http://localhost:5001/servo/position
   curl -s -X POST http://localhost:5001/servo/aim \
     -H 'content-type: application/json' \
     -d '{"direction":"center","duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/nudge \
     -H 'content-type: application/json' \
     -d '{"yaw":5,"pitch":0,"duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/zero
   curl -s -X POST http://localhost:5001/servo/release
   ```

## File .env và ALSA Của Device

`.env` production nằm ở `robots/reachy-mini/rootfs/opt/hal/.env` (pattern rootfs
overlay, giống Lamp). Mic và loa là **cùng một** card USB
(`card 0: Audio [Reachy Mini Audio], device 0`), nên
`robots/reachy-mini/rootfs/etc/asound.conf` alias cả hai về đó, địa chỉ theo
**tên** card để hai card HDMI không làm lệch index:

```
pcm.device_mic     { type plug; slave.pcm "hw:CARD=Audio,DEV=0" }
pcm.device_speaker { type plug; slave.pcm "hw:CARD=Audio,DEV=0" }
```

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker
```

File đó cố ý không set `pcm.!default` — daemon Pollen dùng chung phần cứng và
phải giữ nguyên default mà nó cần.

Hai file này lên robot qua **device package**, không phải copy tay:
`spike-device.sh` tải `devices.reachy-mini` từ OTA rồi `cp -a rootfs/. /`, nên
`/etc/asound.conf` và `/opt/hal/.env` là bản đúng version của gói. Script backup
một lần bất kỳ file nào của Pollen mà nó ghi đè (`<file>.pre-autonomous`), và
`--keep-env` giữ nguyên `.env` mà người vận hành đã chỉnh trên máy. Thiếu
`/etc/asound.conf` thì PortAudio không có output device nào và mọi lần TTS chết ở
"device -1", trong khi `aplay` từ shell vẫn chạy — nên đây là bước 1, không phải
bước phụ.

Một khác biệt hành vi so với Lamp: vì thu và phát là cùng một thiết bị USB, chúng
dùng chung clock domain. Mic và loa của Lamp nằm trên hai bus USB khác nhau nên
trôi clock — đó là lý do barge-in bị tắt ở Lamp. Reachy không có kiểu trôi đó,
nên cần test lại echo cancellation trên body này thay vì bê nguyên mặc định
"barge-in off" của Lamp.

## Board Gate

Con Wireless báo `Raspberry Pi Compute Module 4 Rev 1.1`, chuỗi này không chứa
`pi 4` — nên trước ngày 2026-07-29 `assert_board_supported()` từ chối boot HAL:

```
RuntimeError: Unknown board: device-tree model 'raspberry pi compute module 4 rev 1.1'
matches no entry in boards.json ... Refusing to boot on unidentified hardware
```

Đã sửa bằng cách thêm entry `raspberry_pi_cm4` (`match: ["compute module 4"]`) vào
`hal/board/boards.json` và khai nó trong `boards` của `ROBOT.md`. Phần wiring
`led`/`button` của entry này kế thừa từ `raspberry_pi_4` và **chưa được verify** —
Reachy không khai `light` lẫn GPIO button nên hiện chưa ai đọc tới. Phải verify
trước khi đấu hai peripheral đó trên CM4.

## TODO Khi Spike Hardware

Recon ngày 2026-07-29 đã chốt xong: tên ALSA, loại phần cứng camera, board id,
network stack, port/API của daemon. Bàn giao media và đường camera cũng đã đóng —
HAL tự release/acquire qua `owner: pollen_daemon`, camera đi backend `rpicam`.
Còn lại:

- sign convention cho `head_yaw.pos`, `head_pitch.pos`, và thứ tự antenna
- `wake_up` / `goto_sleep` có phát âm thanh không khi dùng `media_backend="no_media"`
- first-run behavior của `pollen-robotics/reachy-mini-emotions-library`
- verify bảng map emotion→HF move chạy đúng cảm giác trên robot
- test lại echo cancellation / barge-in (chung clock USB, khác Lamp)
- thermal limits trước khi bật `SAFETY.md` `thermal`
- Pollen OS chưa cài `uv`; `spike-hal.sh` tự cài vào `/usr/local/bin`, nhưng
  `setup.sh` production cũng phải cài
- bộ spike đã có systemd, nginx và OTA bootstrap; phần còn thiếu so với production
  là vhost captive portal và nhánh mạng của `setup.sh`
- `setup.sh` phải đi nhánh NetworkManager (`nmcli`), và có thể tái dùng profile
  `Hotspot` sẵn có (`reachy-mini-ap`, `ipv4=shared`) thay vì cài hostapd/dnsmasq
