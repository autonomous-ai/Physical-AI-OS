# LED Control — Tài Liệu

Trong lúc thu giọng Harness thủ công, khi recorder/STT sẵn sàng, LED chuyển sang preset listening hiện có (Lamp: xanh dương nhẹ `[0, 0, 3]`, tốc độ `0.3`), kể cả chưa có transcript đầu tiên. Kết thúc, hủy, timeout hoặc lỗi đều xóa trạng thái LED thu và khôi phục theo thứ tự ưu tiên bình thường; thinking/TTS sau đó giữ hành vi hiện có. Cue này chỉ đổi LED, không di chuyển servo hay đổi cài đặt đã lưu.

## Phần Cứng

- **32 WS2812 RGB LEDs** — một vòng ring
- Driver: `rpi_ws281x` (Python, HAL owns)
- FastAPI endpoints trên `:5001`

### Thời lượng xung SPI

Driver SPI mã hoá bit WS2812 ở 6.4 MHz: `_BIT0 = 0xC0` (~312ns mức cao),
`_BIT1 = 0xFC` (~937ns). Trước đây `_BIT1` là `0xF8` (~781ns) và vài pixel rớt
bit 1 — màu tĩnh mờ như cue setup `[16,16,16]` hiện ra một pixel xanh dương hoặc
vàng, tuỳ kênh nào bị pixel đó đọc nhầm. 781ns vẫn nằm trong dải 580-1000ns theo
datasheet, nhưng dải LED chạy 5V trong khi dây data chỉ 3.3V, sườn lên chậm ăn
mất phần mức cao hiệu dụng.

### Xoá dải LED lúc khởi động

`RGBService.__init__` xoá sạch dải LED ngay khi driver sẵn sàng, trước khi bất kỳ
route hay effect nào kịp vẽ. WS2812 giữ nguyên màu đã chốt khi không có dữ liệu
trên dây, và chân SPI bị cấu hình lại trong lúc kernel boot — xung nhiễu trên dây
data làm vài pixel chốt nhầm một màu rác (hay gặp nhất là xanh lá, vì G là byte
đầu tiên của mỗi frame WS2812). Không có bước xoá này thì màu rác đó sáng cho tới
lệnh LED đầu tiên, có thể vài phút sau khi boot.

### Ghi frame đồng thời và chẩn đoán clear

Solid, paint từng pixel và clear dùng chung khóa driver cho toàn bộ thao tác.
Clear giữ khóa qua hai lần ghi frame đen, hai khoảng chờ 10 ms, SPI idle và
đọc lại buffer. Animation không thể tô lại buffer giữa chừng khiến
`LED clear did NOT take` báo nhầm lỗi clear. Frame đến sau vẫn có thể tô màu
khi clear đã trả về; quản lý và hủy effect vẫn thuộc bên gọi. Chẩn đoán này đọc
bộ nhớ phần mềm, không phải phản hồi từ LED thật, nên buffer đen không chứng minh
phần cứng đã tắt.

## Endpoints

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/led` | LED strip info (count, available) |
| GET | `/led/color` | Trạng thái cả ring: pixel sáng nhất, `on` khi CÓ pixel nào sáng, `uniform: false` khi các pixel khác nhau (effect dither, paint từng phần). Đọc mọi pixel — chỉ lấy pixel 0 thì một ring đang sáng bị báo "off" mỗi khi pixel 0 tình cờ tối. `on` đọc từ ring kể cả khi có effect đang chạy, nên effect thở trên nền đen sẽ báo `on: false` thay vì nhận vơ ánh sáng mà đèn không hề phát; `color` vẫn trả màu nền của effect vì vòng ambient bên Go dựa vào giá trị đó. |
| POST | `/led/solid` | Fill toàn bộ strip 1 màu |
| POST | `/led/paint` | Set từng pixel (array tối đa 32 items), hoặc gradient với `"gradient": true` |
| POST | `/led/off` | Tắt tất cả LED |
| POST | `/led/effect` | Bật effect |
| POST | `/led/effect/stop` | Dừng effect đang chạy |
| POST | `/led/restore` | Repaint LED state mà user đã set (hoặc tắt strip nếu không có) |

### Transient writes

`/led/solid`, `/led/paint`, `/led/effect`, `/led/off` chấp nhận flag tùy chọn `"transient": true`. Khi bật, call sẽ paint strip nhưng **không** ghi đè user LED state. State đã lưu sẽ được restore khi caller (vd Claude Desktop Buddy) xong việc — qua emotion restore timer tự nhiên, hoặc qua `POST /led/restore`. Pulse effect chạy với `transient: true` cũng overlay trên màu user thay vì nền đen.

### Xác nhận Harness voice

Mode Harness đọc `button_led.harness_on` / `button_led.harness_off` trong `robots/lamp/presets.json` qua bảng preset HAL tại lúc chạy. Khi ON, Lamp duy trì đèn thở hổ phách ấm nhẹ `breathing_fine`, RGB `[3, 1, 0]`, speed `0.6` (khoảng năm giây mỗi nhịp). OFF nháy trắng nhẹ một lần, RGB `[2, 2, 2]`, speed `1.0`, duration `300` ms, rồi khôi phục trạng thái đèn người dùng. Watcher mode và luồng khôi phục LED dùng chung `hal/drivers/harness/led.py`; sleep, riêng tư mic, TTS, nhạc và thinking có ưu tiên cao hơn. Thở ambient lúc nghỉ không được thay đèn báo mode. Không ghi đè tùy chọn LED đã lưu; không có RGB service thì bỏ qua. Overlay OFF khôi phục sau thời lượng cấu hình thêm 100 ms.

## Solid Color

```json
POST /led/solid
{"color": [255, 180, 100]}
```

`color` là array `[R, G, B]` (giá trị 0-255) hoặc int packed `0xRRGGBB`.

## Paint (Per-Pixel / Gradient)

```json
POST /led/paint
{"colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]]}
```

`colors` là array các pixel `[R, G, B]` (hoặc packed int) áp theo thứ tự index (0-63). Không có `gradient`, chỉ `len(colors)` pixel đầu được paint — phần còn lại của strip giữ màu cũ.

```json
POST /led/paint
{"colors": [[0, 200, 200], [150, 0, 255]], "gradient": true}
```

Với `"gradient": true`, các màu được coi là **stop** của gradient và nội suy tuyến tính trên toàn bộ strip (kiểu CSS gradient) — ví dụ trên fade cyan → tím qua cả 32 pixel. Chấp nhận số stop bất kỳ ≥ 1.

Paint tự dừng effect đang chạy trước (effect repaint strip mỗi ~40ms sẽ đè lên) và, trừ khi `"transient": true`, lưu danh sách pixel đã paint làm user LED state — nên emotion animation, TTS wave, và HAL restart trong cùng phiên boot đều restore đúng gradient. Với gradient, danh sách 32 pixel *đã expand* được lưu, không phải các stop.

## Effects

```json
POST /led/effect
{"effect": "breathing", "color": [255, 100, 50], "speed": 1.0}
```

| Effect | Mô tả | Params |
|--------|-------|--------|
| `breathing` | Sine-wave brightness lên xuống | color, speed, `start_at_peak` |
| `breathing_fine` | Vẫn nhịp thở đó, nhưng phần lẻ được rải ra cả ring (spatial dither) thay vì cắt cụt ở từng pixel — dành cho cue tối, nơi `breathing` chỉ còn 2 mức dùng được. Không bao giờ tối hơn `color` một nấc, không bao giờ sáng hơn `color`. | color, speed, `start_at_peak` |
| `candle` | Nến lung linh ngẫu nhiên | color |
| `rainbow` | Xoay hue qua toàn bộ strip | brightness (0.0-1.0, mức sáng), speed — tự sinh hue, bỏ qua `color` |
| `notification_flash` | Flash nhanh 3 lần | color |
| `pulse` | Pulse đơn từ tâm ra ngoài | color, speed |

## Lighting Scenes

```json
POST /scene
{"scene": "reading"}
```

Mỗi scene điều khiển **toàn bộ thiết bị ngoại vi** — không chỉ LED mà cả camera, mic, speaker và servo.

Tắt scene: `POST /scene/off` — xoá scene đang active, khôi phục LED idle, bật lại camera/speaker, nhả servo hold.

Scene đang active **sống sót qua các lần restart HAL service** (OTA, deploy, crash): trạng thái được persist vào sidecar theo phiên boot (`/tmp/hal-scene-state.json`, gắn với `boot_id` của kernel) và tự động kích hoạt lại khi HAL chạy trở lại, nên niềm tin của agent ("focus mode đang bật") luôn đồng bộ. Reboot toàn bộ thiết bị thì chủ đích khởi động không có scene. Các lệnh LED transient (`/led/solid`, `/led/off`, `/led/effect` với `"transient": true`, vd hiệu ứng breathing lúc boot) chỉ overlay lên strip mà không thoát scene đang active; chỉ LED override non-transient mới xoá scene.

Khi HAL restart trong lúc đang ngủ, restore scene chỉ giữ tên scene active, không áp dụng lại LED, servo, camera, mic hoặc loa. Sleep tiếp tục giữ quyền điều khiển phần cứng và các cờ mute. User LED state được load riêng; khi thức dậy bình thường, flow scene-off hiện có sẽ xoá scene đã giữ lại.

| Scene | Sáng | Màu (K) | Servo | Camera | Mic | Speaker |
|-------|------|---------|-------|--------|-----|---------|
| `reading` | 80% | 4000K trắng ấm | desk + hold | off | off | off |
| `focus` | 70% | 4200K trung tính ấm | desk + hold | off | off | off |
| `relax` | 40% | 2700K ấm | wall | on | on | on |
| `movie` | 15% | 2400K amber mờ | wall | off | on | off |
| `night` | 5% | 1800K amber đậm | down | off | off | off |
| `energize` | 100% | 5000K ánh sáng ban ngày | up | on | on | on |

### Điều khiển ngoại vi theo scene

Khi kích hoạt scene, `POST /scene` thực hiện theo thứ tự:

1. **LED** — màu đặc = `preset.color × preset.brightness`
2. **Servo aim** — xoay đầu đèn theo hướng preset (desk, wall, up, down)
3. **Servo hold** — nếu `"servo": "hold"`, freeze servo **sau khi** aim xong (aim → hold trong cùng 1 thread). Tự release khi chuyển sang scene không có hold.
4. **Camera** — tự động bật/tắt
5. **Mic** — mute dừng voice pipeline (STT), unmute khởi động lại
6. **Speaker** — `off` dừng nhạc ngay và mute giọng nói theo **drain** (`_start_scene_speaker_drain`, xem `sensing-behavior_vi.md`): câu xác nhận của chính scene, do os-server gửi sau marker `/scene`, vẫn phát xong rồi loa mới đóng; `sleepy` ghép trong cùng reply sẽ tiếp quản drain để wake trả loa lại được. `on` bật lại output. Tắt scene khi privacy đang khoá sẽ đổi snapshot của khoá để lúc nhả loa/camera mở lại (xem `physical-controls_vi.md`).

**Chỉ có kích hoạt scene mới aim.** Một lần restore LED — sau emotion, khi TTS kết thúc, khi nhạc
dừng, khi bỏ mute mic, khi tắt cue lắng nghe — chỉ vẽ lại strip chứ không làm gì khác, và một
chuyển trạng thái presence `IDLE/AWAY → PRESENT` chỉ khôi phục ánh sáng. Trước đây cả hai đều re-aim
khi trạng thái LED đã lưu là một scene, làm chết animation đang chạy và ghim đầu ở `__aim_hold__`
trong 5s (#314). Hệ quả: khi một scene `hold` đang bật, đầu không còn tự trôi về tư thế của scene sau
khi animation kết thúc — nó nội suy về idle. Muốn khôi phục tư thế đó thì việc ấy thuộc về
`servo: hold` của scene, không thuộc về một lần vẽ lại LED.

### Chặn emotion khi hold mode

Khi servo đang hold (reading/focus), **animation cảm xúc bị chặn** để tránh phân tâm:

- `happy`, `thinking`, `curious`, `sad`, v.v. → servo + LED bị bỏ qua
- `greeting`, `sleepy`, `stretching` → **cho qua** (đây là emotion thay đổi trạng thái: chào, ngủ, thức dậy) — **chỉ áp dụng cho hold do scene preset**

**`/servo/hold` tường minh** (lệnh agent kiểu "nhìn lên tường giữ đó") set `_hold_explicit` và chặn servo với **mọi** emotion, kể cả nhóm scene-change — trước đây `[HW:/emotion:greeting]` đứng cuối reply lợi dụng miễn trừ này, đè pose đã lệnh bằng pose cuối của animation greeting. `/servo/resume` và chuyển scene sẽ xoá cờ.

Nghĩa là khi focus, sensing event vẫn tới OpenClaw nhưng Lamp giữ nguyên trạng thái vật lý — không cử động, LED ổn định.

### Lý do chọn nhiệt độ màu

- **Focus 4200K/70%** (không phải 5000K/100%) — 4000-4300K tối ưu cho tập trung mà không gây mỏi mắt
- **Night 1800K amber đậm** — bước sóng >580nm không ảnh hưởng melatonin
- **Movie mic on** — cho phép điều khiển giọng nói ("pause", "stop") khi xem phim

## Status LED

Xem chi tiết: [status-led_vi.md](status-led_vi.md)

LED phản hồi trạng thái hệ thống (tất cả `breathing` speed 3.0 trừ khi ghi rõ):

| Trạng thái | Màu | RGB |
|-----------|-----|-----|
| Mất internet (Connectivity) | Cam | `(16, 7, 0)` |
| Đang khởi động (Booting) | Xanh dương | `(0, 6, 16)` |
| HAL Down | Tím | `(11, 0, 16)` |
| Agent Down | Cyan | `(0, 12, 12)` |
| Hardware Failure | Vàng | `(12, 12, 0)` |
| OTA đang chạy (bootstrap) | Cam | `(16, 8, 0)` |
| OTA thành công (bootstrap) | Flash xanh lá | `(0, 12, 4)` |
| OTA thất bại (bootstrap) | Đỏ pulse | `(16, 2, 2)` |

Quản lý bởi `system/statusled/Service` (lamp) và `lib/hal` trực tiếp (bootstrap).

Không còn màu nào hardcode trong Go nữa — trạng thái `system/statusled`, màu OTA-progress
của bootstrap, và màu trắng setup-needed đều đi qua HAL. OS giữ máy trạng thái (KHI nào hiện)
và gửi *tên trạng thái* xuống HAL (`POST /led/status`: booting/error/ota/connectivity/
hal_down/agent_down/hardware/ready_flash/ota_progress/ota_error/ota_success/setup); HAL tra
màu/effect/speed từ `STATUS_LED_PRESETS`, override per-device qua section `status_led` trong
`presets.json` (xem [ROBOT-SPEC.md § Per-device presets](../../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson)).
`setup` là solid bền khi được gửi qua `POST /led/status`; các trạng thái còn lại là overlay
transient. Nó tạo cue trắng AP/pre-setup mô tả bên dưới, và setup thành công sẽ xoá saved state
này thay vì giữ thành user LED preference.

### Đèn báo mic đang mute (idle indicator)

Overlay lamp đặt `STATUS_LED_PRESETS["mic_muted"]` thành đỏ sẫm `(3, 0, 0)`, breathing speed
0.8. Đây là resting look sáng liên tục suốt thời gian mic bị mute, lại thường chiếu về phía user,
nên chỉnh theo tiêu chí "liếc là thấy" chứ không phải "sáng". Màu đỏ cũng có lợi — ở cùng giá trị
nó chỉ mang khoảng một phần tư độ chói so với trắng. Key HAL-local
(không có state Go statusled tương ứng): bật bởi `POST /voice/mute`, tắt bởi `POST /voice/unmute`
(`app_state._mic_muted_led`). Đây là **trạng thái nghỉ** của strip khi mic đang mute —
không chặn gì cả:

- Emotion, effect, TTS/music wave, transient overlay vẫn chạy bình thường đè lên. Chạy
  xong thì mọi LED restore (`_restore_user_led`, `POST /led/restore`) lắng về màu đỏ
  thay vì user state — "không có gì xảy ra + đỏ breathing" nghĩa là mic đang mute.
- Lệnh LED explicit của user (non-transient `/led/solid|off|effect`, `/led/paint`)
  dismiss indicator — ý user thắng strip; mic vẫn mute.
- Nhường các lựa chọn ánh sáng chủ đích: user tắt đèn thì strip vẫn tối, scene active
  giữ nguyên ánh sáng chức năng (flag vẫn giữ, thoát scene mà còn mute thì đỏ quay lại
  ở lần restore kế). Các đường scene unmute mic (`/scene` với `mic:"on"`, `/scene/off`)
  cũng clear indicator.
- **Sleep được ưu tiên:** khi emotion `sleepy` đang hoạt động, strip luôn tắt. Flag mute
  vẫn được giữ, nhưng restore đến muộn từ emotion/TTS/music không thể vẽ lại indicator đỏ;
  nó chỉ có thể hoạt động lại sau khi một wake emotion thoát sleep.
- `_user_led_state` không bao giờ bị đụng — unmute là về lại đúng state user đã lưu.
- Khi indicator đang giữ strip, transient overlay bị skip (`POST /led/effect` với
  `transient:true`) và **mọi** `POST /led/effect/stop` cũng bị skip: không thể có transient
  overlay nào đang chạy (start của nó đã bị skip), nên stop nào tới lúc mute đều là caller
  cũ/lạc hậu. breathingLoop ambient bên Go giữ flag "running" cục bộ nên vẫn bắn StopEffect
  khi pause/lock dù start đã bị skip — trước khi guard phủ hết mọi thread, stop đó lọt qua
  lúc emotion effect đang giữ strip (vd pulse tím của thinking) và giết nó sau ~1 vòng,
  strip đứng hình ở frame ripple cuối. Emotion effect tự lắng về đỏ qua restore đã hẹn giờ.

### Sleep sở hữu strip (HTTP routes)

Khi `_sleeping` đang bật, các route **ghi** LED bị chặn ngay ở tầng HTTP chứ không chỉ ở
các đường repaint nội bộ: `POST /led/solid`, `/led/paint`, `/led/effect` và `/led/restore`
log `... skipped -- sleepy owns the strip` rồi trả `200` mà không đụng phần cứng.
`POST /led/status` được che gián tiếp (nó delegate xuống solid/effect). Không có guard này,
agent chạy xong một task cũ sẽ bật sáng strip trên thiết bị đang ngủ.

Lệnh ghi bị **bỏ luôn, không xếp hàng**: sleep nghĩa là "đừng làm phiền", không phải "tạm
dừng rồi báo bù" — cue tới lúc đang ngủ thì đến khi thức đã lạc hậu. Hệ quả: các status cue
của os-server (booting / error / OTA) không hiển thị khi đang ngủ — phần việc bên dưới vẫn
chạy bình thường, chỉ có phần báo hiệu bị nén lại, và không replay lúc thức dậy.

Các route dọn dẹp (`/led/off`, `/led/effect/stop`) cố ý **không** bị chặn: chúng đẩy strip
về tối, đúng cái sleep đang muốn.

### Setup-needed solid (lamp)

Khi lamp start và `config.SetUpCompleted == false` (device đang ở AP/provisioning mode), `server/server.go` spawn goroutine background poll `GET /health` của HAL mỗi giây tối đa 30s, khi `health.led == true` thì gửi `POST /led/status` với state `setup` — HAL paint strip trắng solid báo "device ready, vào hotspot đi". Phải poll (không phải call 1 lần) vì cold boot os-server bind :5000 trước HAL :5001. Không dùng state machine `statusled`. Trắng chỉ là tạm thời: `POST /api/device/setup` thành công sẽ xoá saved setup state này thay vì giữ nó thành user LED preference, rồi restore settle về ambient resting look (hiện đang tối/tắt). Blue-breathing booting vẫn show trong lúc init. Xem [setup-flow_vi.md](../../../../docs/vi/setup-flow_vi.md#ap-mode).

## Ambient Idle Behaviors

Khi Lamp idle (không có interaction):
- **Breathing LED** — sine-wave brightness. Thở theo màu LED hiện tại; khi chưa có màu nào (vd vừa boot xong), fallback về **resting look**, hiện là `(0, 0, 0)` — tối. Nếu user/agent đã đặt màu thì tôn trọng màu đó (breathing dùng màu đó; ambient không đè lên màu đã khóa).

Tự pause khi có interaction, resume sau 60s im lặng.

### Resting look (mặc định: tắt)

Khi chưa có user LED state, strip settle về *resting look*, được định nghĩa ở **hai chỗ và
phải đổi cùng lúc**:

| Phía | Knob | Nơi tiêu thụ |
|---|---|---|
| HAL | `AMBIENT_RESTING_LED` (`hal/presets.py`) | `POST /led/restore` khi không có user state; settle sau khi bỏ mic-mute |
| os-server | `ambientRestingColor` (`system/ambient/service.go`) | fallback của `breathingLoop` khi `/led/color` đọc ra đen |

Cả hai hiện là **`(0, 0, 0)` — trạng thái nghỉ là tối**. Màu resting đen được xử lý đặc biệt:
các đường settle sẽ *clear* strip thay vì start effect (một thread effect thở màu đen sẽ đốt
25 fps ghi SPI và làm `GET /led/color` báo `on: true` trong khi đèn tối thui), còn vòng lặp
bên Go thì skip nguyên tick thay vì paint. Nhờ vậy đèn thành opt-in — chỉ sáng khi có *action*
(emotion, status cue, màu do user/agent set, scene) và trở về đen khi action đó nhả strip.

Hai hệ quả cần biết:

- Device lúc idle trông như **đã tắt**, không phải "đang nghỉ". Đây là chủ ý — status cue
  (`booting`, `connectivity`, …) mới là thứ báo cho user biết có gì đang diễn ra.
- Sau reboot strip ở yên trong bóng tối cho tới khi có thứ gì cần đèn: sidecar LED là
  boot-scoped nên mỗi lần boot đều bắt đầu với no user state và rơi vào resting look.

Mọi đường release đều phải *hỏi* resting look — đường nào tự paint màu "về bình thường" của
riêng nó là tự ý bỏ qua default. Từng có hai đường như vậy: scene-off dispatch màu preset
`idle`, và music-stop khởi động idle breathing, đều là tàn dư từ thời resting còn là trắng
ấm. Với resting đen, chúng để strip sáng cam mờ sau một scene hoặc một bài hát cho tới khi
có restore nào đó tình cờ xoá đi. Cả hai giờ đi qua settle chung (`led.restore_led` /
`ambient_resting_is_dark`), nên tắt scene là tắt đèn.

Đổi cả hai knob về `(255, 200, 140)` (trắng ấm ~2700K @ speed 0.3) là khôi phục hành vi cũ:
đèn idle trông như một cái đèn ấm cúng đang bật thay vì màu xanh "thiết bị" lạnh, và tông ấm
đó tránh trùng với mọi màu status. Chính look này là thứ bật lại đèn ~60s sau khi user tắt,
và làm mỗi lần boot lên là đèn sáng.

### "Off" không phải một chế độ

`POST /led/off` **xoá user LED state** (`_save_user_led_state(None)`) chứ không lưu cờ off.
Vì resting look vốn đã tối, không-có-state chính là off. Chỉ còn hai trạng thái:

| Trạng thái | `_user_led_state` | Lúc nghỉ | Khi có action |
|---|---|---|---|
| **Mặc định** | `None` | tối | sáng lên (emotion, status cue, chỉ báo mic-muted) |
| **Màu user** | solid / paint / effect / scene | đúng màu đó | effect chạy đè lên rồi settle về màu cũ |

`led_should_stay_dark()` (`hal/app_state.py`) là predicate duy nhất cho "để yên cái đèn", và
mọi thứ vẽ mà không do user yêu cầu đều phải hỏi nó: TTS/music wave, settle sau effect,
`POST /led/restore`, `POST /led/effect/stop` (clear frame cuối của effect vừa dừng thay vì
để nó đông cứng trên strip), presence restore/dim, và bên os-server là vòng breathing của
ambient.

Cố tình **không** gate: lệnh tường minh của user/agent (đó chính là user đang yêu cầu, và
lệnh đó ghi đè state), cùng với các cue mang thông tin user cần biết — status overlay
(`POST /led/status`: cam mất mạng, đỏ lỗi, xanh OTA) và chỉ báo mic-muted. Mấy thứ đó xứng
đáng được sáng kể cả trên strip đang nghỉ.

Trước đây off là một trạng thái sticky riêng, và như vậy tệ hơn: nó nhìn giống hệt trạng thái
mặc định (đều tối) nhưng hành xử khác, không có cách nào đưa máy về lại mặc định — lối ra duy
nhất là đặt một màu cụ thể — và reboot thì âm thầm mất nó, vì sidecar là boot-scoped. Sidecar
cũ còn giữ `{"type": "off"}` sẽ được quy về "không có state" ngay lúc load.

## LED Trong Emotion

Mỗi emotion preset có LED color riêng:

| Emotion | LED Color |
|---------|-----------|
| curious | Vàng ấm |
| happy | Vàng sáng |
| sad | Xanh dương nhạt |
| thinking | Tím nhẹ |
| idle | Warm white mờ |
| excited | Cam sáng |
| shy | Hồng nhạt |
| shock | Trắng flash |

### Tên emotion không nhận diện được

`POST /emotion` (`hal/routes/emotion.py`) không bao giờ từ chối tên emotion khác rỗng. Tên được lowercase/trim; tên nào không có trong `EMOTION_PRESETS` sẽ fallback về `curious` (biểu cảm trung tính, luôn an toàn) kèm log warning — caller là AI agent đôi khi bịa tên emotion, trả 400 sẽ phí lượt mà thiết bị không hiển thị gì. Ngoại lệ: khi thiết bị đang ngủ, tên lạ bị **ignore** (`status: ignored`) thay vì fallback. `curious` không còn đánh thức (xem `_SLEEP_GATE_ALLOWED`), nên fallback cũng không vượt được sleep gate — nhưng nó vẫn resolve thành một emotion có servo/LED để rồi bị gate chặn, và log ra `curious` sẽ che mất tên bịa mà agent thực sự gửi. Ngoài trường hợp đó, downstream (servo, LED) dùng emotion đã resolve.

## Override preset theo từng thiết bị

Một thiết bị có thể ghi đè các giá trị emotion/scene/aim này (và kích thước vòng LED) mà
không đổi bảng mặc định dùng chung, qua file `robots/<type>/presets.json`. Đây là cơ chế
nền tảng — xem [ROBOT-SPEC.md § Per-device presets](../../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson).

### Trạng thái thoại LIVE

Thoại LIVE dùng cùng HW emotion `listening` và helper thinking của realtime
theo lượt, gồm hành vi LED, màn hình và thân hiện có. Không có lớp LED riêng
cho LIVE. Emotion cần transcript có chữ và cùng điều kiện hướng tới device;
tiếng ồn hay mở mic không tự bật emotion. Thinking cần bằng chứng kết thúc
từ provider, không dùng ước lượng im lặng local. Xem [realtime voice](../../../../docs/vi/realtime-voice_vi.md#phản-hồi-hw-emotion-trong-chế-độ-live) để biết thời điểm gọi và dọn trạng thái.

### Intent giảm sáng tương đối

Action `dim` local/Jev đọc `/led/color`, chia đôi từng kênh RGB, ghi
`/led/solid` rồi đọc lại kiểm chứng. Gọi tiếp giảm tiếp; đèn tắt giữ nguyên.
Làm tròn xuống có thể đưa về tắt. Effect/scene chuyển thành màu tĩnh từ màu nền
hoặc pixel sáng nhất được báo; không giữ pattern.

Voice được local/Jev xử lý (`handledLocally=true`) giải phóng cue thinking
realtime đang giữ mà không chờ TTS. Phản hồi mute hoặc không có lời nói không
giữ cue vô hạn. Cleanup giữ emotion mới, khôi phục LED đã lưu (kể cả tắt/dim);
TTS/nhạc đang phát giữ overlay tới teardown bình thường. Lượt do agent xử lý
vẫn giữ cue thinking.
