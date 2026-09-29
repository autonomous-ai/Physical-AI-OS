# Simulator — chạy cả stack trên laptop

Chạy **đúng những binary được ship lên board** trên máy dev: HAL với ngoại vi ảo
(hoặc thiết bị thật của máy), os-server, agent bridge, và web UI.

Không có build tag, không có nhánh code thứ hai. Chỉ các đường dẫn tuyệt đối của
thiết bị được dời đi, mỗi cái qua đúng một biến env (`system/lib/syspath` phía Go,
`HAL_*` phía Python). **Không set env = hành vi của board, giống từng byte** — đó
là thứ khiến binary được test *chính là* binary được ship.

> **Nguồn sự thật:** doc này phản ánh code. Hai bên lệch nhau thì code đúng.

### Đây KHÔNG phải cái gì

- **Không phải simulator vật lý.** Không khối lượng, quán tính, va chạm, torque.
  Một tư thế sẽ kẹt trên thân máy thật thì ở đây vẫn thành công.
- **Không khẳng định hình học render ra là đúng.** Repo không có joint hierarchy,
  pivot hay CAD zero offset đã hiệu chuẩn.
- **Không phải `robots/sim`.** Đó là một *thân máy* tối giản riêng (chỉ `motion` +
  `system`), dùng để chứng minh HAL mount đúng những gì `ROBOT.md` khai — xem
  `robots/sim/ROBOT.md`. Simulator trên laptop boot thân máy **lamp** đầy đủ.

---

# Cài đặt

## Bước 1 — Kiểm tra điều kiện

| Cần | Kiểm bằng | Nếu thiếu |
|---|---|---|
| `codex` CLI | `codex --version` | Tự cài — không có gì ở đây cài giúp |
| codex auth — **một trong hai** chế độ | `ls ~/.codex/auth.json` | Không bắt buộc. Có `auth.json` = subscription mode (`codex login`), codex dùng provider built-in; không có = api-key mode, codex chạy bằng `llm_api_key` + `llm_base_url` trong config.json. `runtimes/codex/presync.sh` chọn lại mỗi lần boot nên xoá `auth.json` là đổi chế độ |
| `ffmpeg` | `ffmpeg -version` | Cần cho phát nhạc |
| `uv` | `uv --version` | Dựng `hal/.venv` cho HAL. `make sim` tự tạo ở lần chạy đầu và sync lại mỗi khi `hal/uv.lock` hoặc `hal/pyproject.toml` mới hơn nó |
| `node` + `npm` | `node --version` | Chỉ cần cho `make web-dev` |

**Chỉ `codex` chạy được off-device.** Các runtime khác không có target `*-dev`.

## Bước 2 — Tạo config

```bash
make os-dev-config
```

Máy mới thì nó chép từ template, điền sẵn những gì suy ra được (`llm_base_url`,
`llm_model`, `admin_password_hash`), không đụng giá trị đã có trong file cũ, rồi
in ra đường dẫn cần sửa. `make os-dev` cũng chạy đúng bước seed này — target
riêng chỉ để làm trước.

## Bước 3 — Điền config

```bash
nano ~/.autonomous-os/config/config.json   # chỉ còn llm_api_key phải tự điền
```

### Bắt buộc

| Key | Giá trị | Thiếu thì |
|---|---|---|
`os-dev-seed.sh` ghi sẵn cả ba, nên config mới tạo đã có:

```json
"llm_api_key": "autonomous_api_key",
"llm_model": "Auto-AI",
"llm_base_url": "https://campaign-api.autonomous.ai/api/v1/ai/v1"
```

| Key | Giá trị | Thiếu thì |
|---|---|---|
| `llm_api_key` | **Xin team key thật** — `autonomous_api_key` chỉ là placeholder để web UI vào được, không phải credential chạy được | Không TTS, không STT, không Gemini Live, không mô tả ảnh. Để *rỗng* còn tệ hơn: `adminAuthMiddleware` (`system/server/middleware.go`) fallback dùng chính key này làm bearer, thiếu nó thì trả 503, và `AuthGate` đọc 503 là "chưa từng setup" — web UI đá về `/setup`, mà wizard đó không chạy xong được ngoài thiết bị (không có `mac`, không có `iw`) |
| `llm_base_url` | Điền sẵn gateway chung. Muốn base OpenAI-compatible khác thì tự đặt | Không sao — nó được điền sẵn |
| `llm_model` | Điền sẵn `Auto-AI` | Không sao — nó được điền sẵn |

### Chỉ bắt buộc nếu dùng web UI (`make web-dev`)

| Key | Giá trị |
|---|---|
| `admin_password_hash` | Để trống — `os-dev-seed.sh` tự điền bcrypt hash của `autonomous` khi key này rỗng. Muốn khác thì đặt hash của mình (xem *Đặt mật khẩu của riêng mình*) |
| `session_secret` | Để trống — os-server tự ghi chuỗi ngẫu nhiên ở lần đăng nhập đầu (`system/server/session/session.go`) |

### Tuỳ chọn

| Key | Mặc định | Tác dụng |
|---|---|---|
| `deepgram_api_key` | `""` | Điền vào để dùng STT Deepgram thay cho `AutonomousSTT` |
| `realtime.enabled` | `true` | `false` → mọi lượt đi thẳng main agent (chậm hơn, vẫn chạy) |
| `wakeword` | `true` | `false` → always-listening, khỏi gọi tên |
| `tts_voice` | `Rachel` | Giọng ElevenLabs |
| `stt_language` | `en` | Ngôn ngữ STT |
| `timezone` | `Asia/Ho_Chi_Minh` | IANA zone |

### Tự set — đừng sửa

`device_type` · `agent_runtime` · `set_up_completed` — `os-dev-seed.sh` ghi đè
mỗi lần chạy.

### Để trống

`device_id` · `mqtt_endpoint` · `mqtt_username` · `mqtt_password` ·
`fa_channel` · `fd_channel` — uplink backend đang tắt, xem *Uplink lên backend
đang tắt*.

`telegram_bot_token` — nếu chép config từ thiết bị thật thì **phải xoá trắng**.
Một bot token không thể có hai poller; laptop sẽ cướp tin nhắn của thiết bị.

## Bước 4 — Chạy

Bốn terminal, theo đúng thứ tự này.

```bash
make sim SIM_MEDIA=host           # 1. HAL          :5001
make codex-dev CODEX_PORT=18892   # 2. agent bridge :18892
make os-dev    CODEX_PORT=18892   # 3. os-server    :5000
make web-dev                      # 4. web UI       :5173   (tuỳ chọn)
```

| # | Đợi thấy dòng này rồi mới chạy cái tiếp |
|---|---|
| 1 | `Simulation mode enabled for device 'lamp' (media=host)` |
| 2 | `[codex-gatewayd] listening on ws://127.0.0.1:18892/codex/ws/` |
| 3 | `Codex connected` |
| 4 | `VITE … ready` |

Quy tắc:

- `make sim` phải chạy trước — os-server chờ `/health` của HAL tối đa 120s.
- `codex-dev` và `os-dev` phải dùng **cùng** `CODEX_PORT`. Mặc định `18792` đụng
  openclaw gateway nếu máy có cài.
- Lần đầu chạy `SIM_MEDIA=host`, macOS sẽ hỏi quyền **Microphone** và **Camera**.
  Cấp xong phải chạy lại `make sim`.
- Không cần giọng nói thì bỏ `SIM_MEDIA=host` — stack vẫn chạy, chỉ im lặng.
- `make os-dev-all CODEX_PORT=18892` thay cho terminal 2 và 3: chạy bridge ở nền
  và os-server ở foreground, os-server thoát thì bridge bị kill theo. Khi đó log
  của bridge chỉ nằm trong `$OS_STATE_DIR/codex-gatewayd.log` — muốn xem trực
  tiếp thì dùng hai target riêng.

## Bước 5 — Kiểm tra

```bash
# 1. os-server đã lên
curl -s :5000/api/health/live

# 2. HAL đã lên và đang dùng thiết bị của máy
curl -s :5001/simulator/state | jq   # mong đợi media:"host", media_reasons:{}

# 3. Đường os-server → HAL chạy được (không qua LLM, ~50ms)
curl -s -X POST :5000/api/sensing/event -H 'Content-Type: application/json' \
  -d '{"type":"voice_command","message":"turn on the light"}'

# 4. Agent trả lời (~15s) — câu trả lời hiện ở terminal 3
curl -s -X POST :5000/api/sensing/event -H 'Content-Type: application/json' \
  -d '{"type":"voice_command","message":"introduce yourself"}'

# 5. Nói vào mic: "hey lamp, what time is it"
grep '\[turn\] route=' ~/.autonomous-sim/log/server.log | tail
```

Mở:

- `http://127.0.0.1:5001/simulator` — thân máy 3D
- `http://localhost:5173/monitor` — Flow Monitor (**`localhost`**, không phải `127.0.0.1`)

## Bước 6 — Đặt tên thiết bị (tuỳ chọn)

Wake word đi theo tên agent. Ghi:

```bash
echo '- **Name:** Lumi' > ~/.codex/workspace/IDENTITY.md
```

Nhận trong 5 giây, không cần restart. Giờ `hey lumi` dùng được, song song với
`hey lamp` và `hey autonomous`.

---

# Tra cứu

## Mỗi target `make` làm gì

| Target | Làm | **Không** làm |
|---|---|---|
| `make sim` | Boot HAL với thân lamp, ngoại vi ảo (hoặc thật) | — |
| `make hal-install` | `uv sync` — đồng bộ **chính xác** `hal/.venv`, gỡ mọi thứ không có trong `uv.lock`. `make sim` tự sync bằng `--inexact` nên pytest cài tay vẫn còn | Không cài extra `dev` (pyflakes); muốn thì thêm `--extra dev` |
| `make codex-dev` | **Chỉ** chạy `os-server codex-gatewayd`: một listener WebSocket loopback, spawn một `codex exec` mỗi lượt | Không onboarding, không presync, **không sync skill** |
| `make os-dev` | Ba việc theo thứ tự: `os-dev-build` (biên dịch), `os-dev-seed` (chuẩn bị state dir), rồi chạy API — chính nó cũng lo toàn bộ provisioning cho agent: `presync.sh`, seed `AGENTS.md`/`SOUL.md`/`KNOWLEDGE.md`/`HEARTBEAT.md`, `downloadSkills()`, skill watcher | — |
| `make web-dev` | Chạy Vite thay vai nginx (os-server không serve HTML) | — |

Workspace trống hoặc agent không có persona thì xem **`os-dev`**, không phải
`codex-dev`.

### `os-dev-seed` khác `os-dev` chỗ nào

`os-dev-seed` là target phụ thuộc của `os-dev`, không phải bước anh chạy riêng
trong lúc bình thường. Nó chỉ đụng state dir, không khởi động process nào:

| Nó làm | Nó KHÔNG làm |
|---|---|
| Tạo `config.json` từ `config.example.json` khi chưa có | Ghi đè `config.json` đã tồn tại — giá trị trong đó là của anh |
| Ghi lại `device_type`, `agent_runtime`, `set_up_completed` trong đó | Đụng bất kỳ key nào khác |
| Điền hash của `autonomous` khi `admin_password_hash` còn trống | Ghi đè hash đã có sẵn |
| Điền `https://campaign-api.autonomous.ai/api/v1/ai/v1` cho `llm_base_url` và `Auto-AI` cho `llm_model` khi chúng còn trống | Ghi đè giá trị đã có sẵn |
| Cảnh báo khi `llm_api_key` còn trống | — |
| Seed `config/bootstrap.json` (một lần) để skill tải được | — |
| Backup `config.toml` có sẵn thành `config.toml.pre-os-dev` (một lần) | — |

`make os-dev-seed` chạy riêng nó — hữu ích khi muốn kiểm lại state dir mà không
khởi động server.

## Các file config nằm ở đâu

| | Đường dẫn |
|---|---|
| File mẫu (trong repo) | `scripts/dev/config.example.json` |
| Config đang dùng | `$OS_STATE_DIR/config/config.json` — mặc định `~/.autonomous-os/config/config.json` |
| Metadata OTA (tự seed) | `$OS_STATE_DIR/config/bootstrap.json` |
| Workspace của agent | `$CODEX_HOME/workspace/` |

Một file config phục vụ **cả** HAL lẫn os-server, đúng vai
`/root/config/config.json` trên board: os-server giải `config/config.json` tương
đối cwd (`make os-dev` cd vào state dir), HAL đọc `OS_CONFIG_PATH`.

Seed chép file mẫu **một lần**. Các lần sau chỉ ghi đè `device_type`,
`agent_runtime`, `set_up_completed`; sửa tay của anh được giữ.

## Credential nào mở khoá cái gì

| Muốn có | Cần |
|---|---|
| Agent trả lời text | không cần gì — codex dùng login riêng |
| Skill tự cài | không cần gì — object trên CDN là public |
| Thiết bị nói ra tiếng (TTS) | `llm_api_key` + `llm_base_url` |
| Nói vào mic (STT) | `llm_api_key`, hoặc `deepgram_api_key` |
| Trả lời dưới một giây (Gemini Live) | `llm_api_key` + `realtime.enabled` |
| Agent nhìn được ảnh | `llm_api_key` |

Một key phủ hết: key realtime fallback về `llm_api_key`, endpoint fallback về
`llm_base_url` + `/ws/gemini` (`hal/config.py`), nên không cần credential Google
riêng.

## Chế độ media

`SIM_MEDIA` quyết định HAL có mở ngoại vi của máy dev hay không.

| | `virtual` (mặc định) | `host` |
|---|---|---|
| Camera | scene calibration tổng hợp, tất định | webcam của Mac (AVFoundation) |
| Mic / loa | device id ảo, không bao giờ xuống `sounddevice` | thiết bị thật qua PortAudio |
| **Pipeline giọng nói** | **stub, không chạy** | **thật** — VAD, Silero, STT, realtime agent, wake word, dispatch `[turn] route=…` |
| Nhạc | chạy đủ pipeline, đổ vào null sink | nghe được |
| Quyền | không cần | macOS hỏi Microphone + Camera |
| Hợp cho | test, CI, làm offline | kiểm tra end-to-end thủ công |

Cổng quyết định là `state.simulation_audio` (`hal/server.py`), không phải chuỗi
media thô. Điều đó quan trọng: `_sim_audio_probe()` chạy trước và lật cờ **trở lại
ảo** khi thiết bị thiếu, đang bận, hoặc bị chặn quyền — nên microphone bị từ chối
rơi về stub kèm log `[sim-media]` nói rõ lý do, thay vì dựng pipeline thật lên một
thiết bị đã chết. `routes/voice.py` key off đúng cờ đó, nên đường boot và
`POST /voice/start` không bao giờ lệch nhau.

Host mode không bao giờ crash. `GET /simulator/state` báo kết quả từng subsystem:

```json
{"media":"host","media_camera":"host","media_audio":"host","media_reasons":{}}
```

`media` chỉ là `"host"` khi cả hai đều host; `media_reasons` mang lý do hành động
được cho mỗi lần tụt hạng. Trên macOS, quyền nằm ở System Settings → Privacy &
Security → Camera / Microphone và phải cấp cho **ứng dụng terminal đang chạy HAL**.
Liệt kê thiết bị không phải là quyền — danh sách vẫn hiện dù chưa cấp, chỉ lần đọc
thật đầu tiên mới lỗi, nên HAL probe lúc boot thay vì để hỏng giữa một lượt.

---

## Wake word và đặt tên agent

Với `"wakeword": true` thì phải gọi tên thiết bị. Tiền tố là
`hello` `hey` `hi` `alo` `okay` `ok` `wake up`, ghép với:

- **tên agent** trong `$CODEX_HOME/workspace/IDENTITY.md`
- **device type** (`lamp`)
- bí danh cố định **`autonomous`**

Muốn đặt tên, ghi vào `$CODEX_HOME/workspace/IDENTITY.md`:

```markdown
- **Name:** Lumi
```

`WatchIdentity` poll file đó **mỗi 5 giây** và đẩy wake word mới xuống HAL —
**không cần restart**. HAL merge chúng với bộ cố định, nên `hey lumi`, `hey lamp`
và `hey autonomous` đều dùng được. Không có `IDENTITY.md` thì tên rơi về
`device_type`.

Cụm wake được chấp nhận ở **đầu hoặc cuối bất kỳ câu nào** trong lượt. Giữa câu bị
từ chối — tên thiết bị nằm giữa câu là người ta đang nói *về* thiết bị.

Sau một lượt được phép, cửa sổ follow-up mở ra
(`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, default code **20s**; `robots/lamp/rootfs/opt/hal/.env` của image lamp cũng đặt 20) và
**reset sau mỗi lượt được phép**, nên nói liên tục thì không bao giờ phải gọi lại
tên. Đặt `"wakeword": false` để always-listening.

---

## Hai web UI

Chúng là hai thứ khác nhau và rất dễ nhầm.

### `http://127.0.0.1:5001/simulator` — thân máy

Do HAL serve. Chỉ mở khi `HAL_SIMULATE=1` **và** thân máy là `lamp`; ngoài ra 404.
Một khung nhìn xoay được của mesh GLB có rig — năm joint node đặt tên đúng như HAL
đặt (`base_yaw`, `base_pitch`, `elbow_pitch`, `wrist_pitch`, `wrist_roll`) — được
điều khiển bởi giá trị khớp live, kèm replay recording và các nút LED gọi đúng
`/servo/*`, `/led/*` mà một skill gọi.

Kéo để xoay, lăn để zoom, double-click để reset. Camera mở ra ở mặt trước đèn, lệch 3/4
so với chính diện, ngắm vào tâm của hình học đang được nạp và lùi đủ xa để lọt
hết khung, nên đầu đèn không bị cắt mất trên
màn hình rộng. Zoom chạy 0,3x-4x của khoảng cách đó qua ống kính 38 deg, độ cao
camera từ ngang sàn tới gần thẳng đứng trên đỉnh. Thân đèn đứng trên lưới sàn bán kính 1,6 m (ô
50 mm, sáng hơn mỗi 250 mm, mờ dần ra mép tròn) để thấy rõ hướng quay và độ
nghiêng; canvas render theo pixel ratio của màn hình, chặn ở 2x. Vòng LED đọc
`/simulator/pixels` (chính buffer của strip), không phải `/led/color`, vì endpoint đó trả màu base tĩnh
của effect và sẽ render breathing, candle, rainbow thành một màu chết.

Tư thế render ra là **phản ứng thị giác** theo giá trị khớp live, **không phải**
khẳng định rằng nó đúng về cơ khí.

### `http://localhost:5173/monitor` — UI sản phẩm

Flow Monitor, Settings, Logs — đúng SPA chạy trên board.

os-server **không serve HTML**: trên thiết bị, nginx serve `web/dist` và proxy
`/api` xuống `:5000` (còn `/hw/` tới HAL ở `127.0.0.1:5001`). `make web-dev` đặt Vite vào đúng vai nginx, với
`LAMP_PROXY` là thiết bị mà SPA nói chuyện cùng. File `.env` trong `system/web/`
vẫn thắng (vite.config đọc nó trước `process.env`), nên trỏ vào Pi thật không đổi
gì.

> **Vite chỉ bind `[::1]`** — `127.0.0.1:5173` bị từ chối và trông như server chưa
> chạy. Dùng `localhost`.

Đăng nhập bằng mật khẩu có bcrypt hash nằm trong `admin_password_hash` — ngoài
thiết bị mặc định là `autonomous` (do `os-dev-seed.sh` điền vào hash rỗng), trừ
khi anh tự đặt hash khác. Hoặc thêm
`?llm_api_key=<key trong config.json>` — nhưng lưu ý cách này **hụt ở lần load ĐẦU
trên tab mới** (`api.ts` khởi tạo token từ `sessionStorage` lúc load module, mà
effect của `AuthGate` chạy trước `useBearerFromQuery` của `App`), nên vào lại
`/monitor` lần hai trong cùng tab.

Không có cách thứ ba: `admin_password_hash` rỗng **không** phải là cửa mở — đó
là lý do `os-dev-seed.sh` điền sẵn một hash thay vì để trống.
`VerifyAdminPassword` (`system/device/config_update.go`) từ chối thẳng khi chưa
đặt hash, và nó từ chối ngoài thiết bị y như trên board — simulator chạy đúng
binary được ship nên không có đường tắt auth nào để bật.

#### Đặt mật khẩu của riêng mình

Config copy từ thiết bị mang theo hash của thiết bị đó. Muốn đăng nhập bằng mật
khẩu của riêng mình thì sinh hash cost 10 rồi đặt vào `admin_password_hash`:

```bash
htpasswd -bnBC 10 "" 'mat-khau-cua-ban' | tr -d ':\n'
```

`htpasswd` có sẵn trên macOS. Nó phát prefix `$2y$` trong khi `bcrypt` của Go ghi
`$2a$`; cả hai cùng thuật toán và `CompareHashAndPassword` chấp nhận cả hai, nên
dán nguyên đầu ra. Cặp `""` ở đầu là trường username mà os-server không dùng —
`tr` cắt nó cùng với ký tự xuống dòng.

---

## Cái gì được mô phỏng

Boot với thân lamp mount 12 route và bỏ qua 1:

```
mounted=['audio','bluetooth','camera','emotion','led','music','scene',
         'sensing','servo','speaker','system','voice']  skipped=['buddy']  failed_required=[]
```

### Chạy y hệt board

- toàn bộ bề mặt route — vẫn do `ROBOT.md` quyết định, không phải simulator
- safety gate — `motion.max_speed`, gate ổn định toàn thân
  (`motion.max_cog_offset_mm`), trần độ sáng LED. `/servo/move` từ chối tên khớp
  lạ, nhưng không có bound nào clamp góc khớp về một khoảng.
- mount plan theo declaration
- emotion preset, scene, music, bluetooth, system
- `TrackerService`, volume, kho user/stranger

### Bị thay thế

| Hệ | Board | Simulator |
|---|---|---|
| Servo | `feetech` qua bus nối tiếp | `MockMotionService` — khớp là float trong dict; move nội suy theo đúng `duration` và block tới khi tới nơi; `aim`/`nudge` tuân `max_speed` của `SAFETY.md`; recording CSV replay qua đúng lưới stretch-and-resample 30 Hz, nên một animation tốn đúng bằng thời gian thực — và qua đúng `resample_recording`, nên gate ổn định từ chối trong `make sim` đúng clip mà thân máy thật từ chối |
| LED | WS2812 qua SPI | `_MemoryStrip` — buffer pixel thật trong RAM, effect chạy thật, vẫn qua đúng cổng clamp độ sáng |
| Camera | `opencv`/V4L2 | `virtual` (scene tổng hợp) hoặc `host` (webcam của Mac) |
| Sensing | `SensingService` + face recognition | `VirtualSensingService` — giữ presence state và contract của route; không gọi perception-service, không có face identity |
| Board profile | đọc device tree | profile `sim` inert |
| Đầu ra nhạc | `aplay` (ALSA) hoặc `paplay` (PulseAudio) | output device AudioToolbox của ffmpeg trên macOS |
| Thu âm cho voice enroll | `arecord` qua alias ALSA | PortAudio (`sounddevice`) trên đúng input device mà pipeline giọng nói đang thu; WAV mang theo sample rate của chính nó và recognizer tự resample |
| GELF logging | bắn về log server | tắt |
| GPIO button / touchpad | thật | bỏ qua (gate `_board_id != "sim"`) |

`HAL_BOARD=sim` bị **từ chối** nếu thiếu `HAL_SIMULATE=1` — HAL không boot driver
vật lý trên một board ảo.

---

## Tra cứu biến môi trường

### Phía Go — `system/lib/syspath`

Mỗi accessor giữ nguyên default production và được ghi đè bởi đúng một env.
`make os-dev` và `make codex-dev` dùng chung một bộ, nên bridge và client của nó
không bao giờ bất đồng.

| Env | Mặc định (board) | Dùng cho |
|---|---|---|
| `CODEX_HOME` | `/root/.codex` | Gốc mọi path codex, ở **cả** client lẫn gatewayd |
| `CODEX_PORT` | `18792` | Listener của bridge + `WSURL` |
| `CODEX_WS_TOKEN` | `autonomous_codex_token` | Bearer token giữa os-server và bridge |
| `OS_AGENT_HOME` | `/root` | Gốc để coding session resolve `~` |
| `OS_AGENT_STATE_PATH` | `/root/config/agent_state.json` | Lịch sử chuyển runtime |
| `OS_BOOTSTRAP_CONFIG` | `/root/config/bootstrap.json` | Nguồn `metadata_url` — base cho skill zip |
| `OS_LOG_FILE` | `/var/log/os-server.log` | Log xoay vòng của os-server |
| `OS_BACKEND_UPLINK` | `on` | Ping backend + MQTT. `make os-dev` set `off` |
| `OS_HAL_LOG_FILE` | `/var/log/hal/server.log` | Nơi web UI đọc log HAL |
| `OS_AGENT_BRIDGE_LOG` | `""` (dùng journal) | File để đọc bridge khi không có systemd |
| `DEVICES_DIR` | `/opt/devices` | Gốc `robots/<type>/` |

Chỉ đúng chữ `"off"` mới tắt `OS_BACKEND_UPLINK` — mọi giá trị khác, kể cả gõ sai,
đều giữ nó bật, nên một đợt fleet upgrade không bao giờ vô tình cắt uplink của cả
đội máy.

### Phía HAL

HAL vốn đã đọc tất cả những biến này; target `sim` chỉ trỏ chúng về chỗ laptop ghi
được. Chúng hỏng ở rất xa nguyên nhân — riêng TTS cache lộ ra dưới dạng
`POST /voice/speak 409` còn `PermissionError` thật thì nằm lẫn trong traceback của
một thread nền — nên phải set thành một khối.

| Env | Mặc định (board) | `make sim` |
|---|---|---|
| `OS_CONFIG_PATH` | `/root/config/config.json` | `$OS_STATE_DIR/config/config.json` — file dùng chung với os-server |
| `HAL_SNAPSHOT_DIR` | `/root/.<runtime>/media/hal-snapshots` | `$CODEX_HOME/media/hal-snapshots` — bắt buộc nằm dưới home của agent, nếu không nó không đọc lại được frame và os-server không serve được thumbnail |
| `HAL_CODEX_WORKSPACE_DIR` | `/root/.codex/workspace` | `$CODEX_HOME/workspace` — `memory.jsonl` của realtime agent suy ra từ đây |
| `HAL_LOG_DIR` | `/var/log/hal` | `$SIM_STATE_DIR/log` |
| `HAL_SNAPSHOT_PERSIST_DIR` | `/var/lib/hal/snapshots` | `$SIM_STATE_DIR/snapshots` |
| `HAL_TTS_CACHE_DIR` | `/var/lib/hal/tts_cache` | `$SIM_STATE_DIR/tts_cache` |
| `HAL_CALIBRATION_DIR` | `/var/lib/hal/calibration/…` | `$SIM_STATE_DIR/calibration/…` |
| `HAL_USER_BEARING_PATH` | `/var/lib/hal/user_bearing.json` | `$SIM_STATE_DIR/user_bearing.json` |
| `HAL_FACE_HEIGHT_PATH` | `/var/lib/hal/face_height.json` | `$SIM_STATE_DIR/face_height.json` |
| `HAL_USERS_DIR` / `HAL_STRANGERS_DIR` / `HAL_VOICE_STRANGERS_DIR` | `/root/local/…` | `$SIM_STATE_DIR/…` |
| `HAL_BT_STATE_DIR` / `HAL_VOLUME_STATE_PATH` | `/var/lib/hal`, `…/.volume` | `$SIM_STATE_DIR/…` |
| `HAL_DL_STALL_LOG` | `/root/local/dl_ws_stall.log` | `$SIM_STATE_DIR/dl_ws_stall.log` |
| `HAL_SIMULATE` / `HAL_BOARD` / `HAL_SIM_MEDIA` | unset | `1` / `sim` / `$(SIM_MEDIA)` |

Hai default của thiết bị **cố ý không đụng**: `/root/local/models` và `/opt/piper`
là đường dẫn model chỉ-đọc. Laptop không có thì tính năng cần chúng đơn giản là
tắt.

### Núm Makefile

| Núm | Mặc định |
|---|---|
| `DEVICE_TYPE` | `lamp` |
| `SIM_MEDIA` | `virtual` |
| `SIM_STATE_DIR` | `~/.autonomous-sim` |
| `OS_STATE_DIR` | `~/.autonomous-os` |
| `OS_AGENT_RUNTIME` | `codex` |
| `CODEX_HOME` | `$HOME/.codex` |
| `CODEX_PORT` | `18792` |
| `CODEX_BIN` | `codex` đầu tiên trên `PATH` |
| `OS_BACKEND_UPLINK` | `off` |
| `HAL_PORT` | `5001` |
| `LAMP_PROXY` | `http://127.0.0.1:5000` |

Mọi path trong bảng biến môi trường ở trên cũng là núm — mỗi cái là một biến
`?=` riêng, nên dời được một cái mà không đụng phần còn lại:

```bash
make sim HAL_TTS_CACHE_DIR=/Volumes/sd/tts     # một path
make sim SIM_STATE_DIR=~/work/sim-a            # cả bộ
```

Biến export sẵn trong shell cũng thắng mặc định, cùng lý do. Hai cặp được giữ
khớp qua biến thay vì lặp lại chuỗi, nên override một nửa thì nửa kia đi theo:
`OS_HAL_LOG_FILE` dẫn từ `HAL_LOG_DIR` (HAL ghi, os-server đọc cho tab HAL trên
web UI), và đích `tee` của `codex-dev` chính là `OS_AGENT_BRIDGE_LOG` (bridge
ghi, os-server đọc cho tab Agent).

---

## Uplink lên backend đang tắt

`make os-dev` set `OS_BACKEND_UPLINK=off`, chặn hai thứ: ping trạng thái 15 giây
(`system/device/status_reporter.go`) và kênh lệnh MQTT (`system/server/mqtt.go`).

Đây không phải sở thích của simulator — nó là **chốt an toàn**. **Backend nhận
diện thiết bị qua `llm_api_key`, không qua `device_id`**, nên một laptop cầm bản
copy config của thiết bị là không phân biệt được với chính thiết bị đó. Đo được
khi cả hai cùng chạy:

- ping ghi đè `local_ip`, `mac`, `version`, `skills` của thiết bị thật **mỗi 15
  giây**
- client ID MQTT — sinh từ `device_id` mà backend trả về — trùng nhau, và hai
  client đá nhau khỏi broker **khoảng 1,5 lần/giây, liên tục**

Sửa config không tránh được: để trống `device_id` thì backend cấp lại đúng id cũ,
còn để trống `mqtt_endpoint` thì bị ping response ghi đè — nó lưu cấu hình broker
xuống đĩa rồi kích hoạt `restartMQTT`. Chặn subscriber là đủ cho cả chuỗi: hai
client publisher đều kết nối lazy, và chat stream chỉ publish cho run do một
`chat.send` từ backend tạo ra.

Không thứ gì một dev cần đi qua uplink: web UI, Flow Monitor, pipeline giọng nói,
agent, skills và Telegram (thiết bị poll thẳng) đều là cục bộ. Cái mất là **điều
khiển từ xa**: chat từ app điện thoại, OTA từ xa, cài skill từ xa, và proxy webhook
Slack.

`make os-dev OS_BACKEND_UPLINK=on` bật lại. Chỉ làm vậy khi laptop **không** đang
cầm credential của một thiết bị đang sống.

---

## Log

| Tab web UI | Off-device |
|---|---|
| HAL | ✅ `$SIM_STATE_DIR/log/server.log` |
| OS | ✅ `$OS_STATE_DIR/os-server.log` |
| Agent / Agent Service | ✅ `$OS_STATE_DIR/codex-gatewayd.log` — `make codex-dev` tee bridge ra file (`2>&1`, vì Go `slog` ghi ra stderr) do laptop không có journal |
| Bootstrap | ❌ OTA worker không có target off-device |
| Claude Desktop Buddy | ❌ app Mac riêng, không ghi log ở đây |

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân |
|---|---|
| `codex CLI not found on PATH` | Chưa cài agent — xem *Bước 1* |
| `curl :5000` trả binary plist hoặc 403 | os-server chưa chạy — AirPlay Receiver của macOS trả lời trên `*:5000`. Tắt nó (System Settings → General → AirDrop & Handoff) hoặc đổi `httpPort` |
| `listen failed: address already in use` | `CODEX_PORT` để nguyên `18792`, đang bị openclaw gateway giữ |
| `bad handshake (status 404)` | `codex-dev` và `os-dev` lệch `CODEX_PORT` |
| `dial 127.0.0.1:5001: connection refused` | HAL chưa lên |
| `127.0.0.1:5173` không kết nối được | Vite bind `[::1]` — dùng `localhost:5173` |
| Nói vào mic không phản ứng | Thiếu `SIM_MEDIA=host`, hoặc macOS chặn microphone. Kiểm `media_reasons` trong `/simulator/state`. Tên biến là `SIM_MEDIA` — `make sim MEDIA=host` chỉ đặt một biến không ai đọc và make không báo gì, nên phải xác nhận dòng khởi động ghi `media=host` |
| Voice enroll trả 503 `needs a real microphone` | `SIM_MEDIA=virtual` — enroll từ chối mở mic thật ở chế độ đã hứa là không đụng tới |
| Voice enroll trả 400 `vad_removed_all` | Clip không có tiếng nói. Đọc to các câu mẫu, gần mic hơn, suốt thời gian đếm ngược |
| STT nghe sai tên | `flux-general-en` nghe nhầm danh từ riêng; "hi lamp" từng ra "hi lance", và nghe nhầm là **rớt cả lượt trong im lặng**. Các từ wake đã được đẩy làm STT boost term, nhưng vẫn nên nói rõ |
| `POST /voice/speak 409` + `PermissionError: /var/lib/hal` | Chưa set `HAL_TTS_CACHE_DIR` — target `sim` bản cũ |
| "Sorry, I can't play that right now" | Nhạc: macOS không có `aplay`/`paplay`. Cần `ffmpeg` trên `PATH` cho đường AudioToolbox |
| `POST /audio/volume` trả 503 | Bình thường — macOS không có ALSA mixer |
| Agent tự xưng "Codex", không persona | `$CODEX_HOME/workspace` phải có `AGENTS.md`, `SOUL.md`, `KNOWLEDGE.md`, `HEARTBEAT.md`. Chúng do **`os-dev`** tạo, không phải `codex-dev` |
| Workspace rỗng, không thấy log `seeded file` | `set_up_completed` chưa true nên chuỗi khởi động không chạy |
| `skill download skipped: no ota_metadata_url` | Thiếu `config/bootstrap.json` |


## Cái gì KHÔNG chạy off-device

| | Vì sao |
|---|---|
| Uplink backend | Tắt có chủ đích — xem *Uplink lên backend đang tắt* |
| Bootstrap / OTA | `bootstrap-server` không có target off-device |
| Log của Claude Desktop Buddy | App Mac riêng |
| Mọi runtime khác codex | Không có target `*-dev`; chưa từng chạy trên laptop |
| `SensingService` thật (face, motion perception) | Module của nó import driver Feetech, kéo theo `lerobot`. `VirtualSensingService` đứng thay |
| Face recognition, speech emotion, speaker ID | Cần perception service / endpoint embedding |
| GPIO button, touchpad, mic button | Không có phần cứng; bỏ qua lúc boot |
| Nhiệt độ SoC | `temp_c: null`, nên không test được đường thermal throttle |

---

## Vì sao board không bị ảnh hưởng

Mọi hành vi off-device đều nằm sau một biến env mà giá trị unset là của board,
hoặc sau một kiểm tra platform mà board không bao giờ thoả:

| Thay đổi | Board |
|---|---|
| Cổng pipeline giọng nói | `state.simulation_audio` = False khi `HAL_SIMULATE` unset — y hệt kiểm tra `_simulation` mà nó thay thế |
| Đường nhạc macOS | Guard `sys.platform == "darwin"` |
| Backend thu âm của `record-enroll` | `shutil.which("arecord")` tìm thấy nó trên board, nên nhánh PortAudio không bao giờ chạy |
| `BackendUplink()` | Mặc định bật; biến không xuất hiện ở unit file, rootfs hay script image nào |
| Mọi accessor `syspath` | Env unset trả về đúng literal đã thay |
| Makefile, docs | Không ship xuống thiết bị |

Được khoá bởi các test assert hợp đồng **của board**, không phải của laptop:
`system/lib/syspath/syspath_test.go` (`TestDeviceDefaults`, `TestAgentRuntimeHome`,
`TestBackendUplink`), `system/server/logs_source_test.go`
(`TestResolveLogSourceBoardDefaults`) và `runtimes/codex/paths_default_test.go`.

---

## Tham chiếu code

- `Makefile` — mục `── Off-device run (laptop) ──`, `SIM_HAL_ENV`, `sim`, `web-dev`
- `scripts/dev/os-dev-seed.sh` — seed config.json + bootstrap.json
- `system/lib/syspath/syspath.go` — mọi env override phía Go
- `system/server/logs.go` — `resolveLogSource`
- `runtimes/codex/gatewayd/gatewayd.go` — bridge mà `codex-dev` chạy
- `runtimes/codex/onboarding.go` — những gì `os-dev` seed
- `hal/server.py` — các cổng simulation, mount plan, `_sim_audio_probe`
- `hal/drivers/motors/mock_service.py` — thân máy mock
- `hal/drivers/camera/host_capture_device.py` — backend webcam của máy host
- `hal/static/lamp-simulator.html` — trang 3D
- `robots/sim/ROBOT.md` — thân máy tối giản để test contract

Liên quan: [overview_vi.md](overview_vi.md) · [os-server_vi.md](os-server_vi.md) ·
[agentic/codex_vi.md](agentic/codex_vi.md) · [realtime-voice_vi.md](realtime-voice_vi.md)
