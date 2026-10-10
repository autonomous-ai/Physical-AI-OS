# LED Control — Tài Liệu

Khi presence trở lại từ idle/away, đèn khôi phục theo trạng thái người dùng/đèn nghỉ dùng chung, gồm lựa chọn đèn nghỉ hiện tại và các điều kiện bảo vệ hiệu ứng đang chạy. Presence không giữ cache màu riêng hay khôi phục màu emotion. Khi idle, độ sáng giảm dựa trên màu nền người dùng đã lưu hoặc preset đèn nghỉ hiện tại; lệnh tắt đèn vẫn được giữ nguyên.

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

### LED báo boot sớm trên Orange Pi

Khi chuyển device đã có unit `lamp-led-*` cũ, stop HAL, sau đó stop/disable
`lamp-led-boot.service` và stop `lamp-led-shutdown.service` trước khi gỡ hai unit,
symlink enable và helper `lamp-led-boot.py`/`lamp-led-off.py` cũ. Áp dụng rootfs mới
(gồm drop-in shutdown của HAL), reload systemd rồi start HAL. Chép overlay không
tự xóa file đã đổi tên. HAL nhận cả hai tên boot unit trong lúc chuyển đổi, nhưng
không được enable hai bản indicator cùng lúc.

`led-boot.service` được kéo vào qua `sysinit.target.wants`, sau filesystem
local và trước các service thông thường, không chờ mạng hay os-server. Trên lamp
sun60iw2, nó điều khiển 32 LED qua SPI3.0, thở trắng chu kỳ ba giây, từ tắt đến
RGB **[3, 3, 3]**, 20 frame/giây. Script chờ node SPI tối đa mười giây nhưng không
chặn boot. Đây là báo đang khởi động, không phải sẵn sàng; không báo được lúc vừa
cấp điện hoặc lỗi trước khi Linux/systemd chạy tới unit này.

HAL dừng indicator đồng bộ ngay trước lúc khởi tạo RGB trong early LED lifespan,
sau phần Python import.
SIGTERM kết thúc vòng animation duy nhất, clear hai lần, flush LOW rồi đóng SPI;
systemd đợi tiến trình thoát trước khi HAL mở SPI. Timeout stop là ba giây. Hiệu ứng tiếp tục
trong lúc HAL import; bàn giao khi khởi tạo RGB, không đợi voice/camera sẵn sàng. Script từ chối start thủ công khi HAL đang active,
starting hoặc stopping. Indicator không tự restart và không tiếp quản lúc
shutdown. Fallback tắt LED trễ khi shutdown vẫn độc lập. Unit boot kéo fallback
vào và start sau nó; khi shutdown, thứ tự đảo lại nên boot writer thoát trước khi
fallback bắt đầu chờ năm giây. Khi chưa có ràng buộc này, log device cho thấy
fallback gửi frame đen ở giây 103 nhưng boot writer vẫn chạy tới giây 109.

Thành phần nằm trong rootfs device lamp; ZIP giữ symlink target.wants.
`ConditionPathExists` yêu cầu helper boot-led của HAL mới; HAL cũ bỏ qua indicator.
Cần cập nhật HAL trước khi bật service. Có hiệu lực
ở lần boot tiếp theo sau update device/daemon-reload. Không start đè lên HAL đang
chạy để test. Rollback: stop indicator, gỡ symlink sysinit, unit và helper của indicator, rồi daemon-reload. Test local kiểm tra frame, hủy, đóng driver
khi lỗi và điều kiện bàn giao; kiểm tra unit systemd chưa xác nhận màu/thời gian
LED trên phần cứng thật.

### Ghi frame đồng thời và chẩn đoán clear

Solid, paint từng pixel và clear dùng chung khóa driver cho toàn bộ thao tác.
Clear giữ khóa qua hai lần ghi frame đen, hai khoảng chờ 10 ms, SPI idle và
đọc lại buffer. Animation không thể tô lại buffer giữa chừng khiến
`LED clear did NOT take` báo nhầm lỗi clear. Frame đến sau vẫn có thể tô màu
khi clear đã trả về; quản lý và hủy effect vẫn thuộc bên gọi. Chẩn đoán này đọc
bộ nhớ phần mềm, không phải phản hồi từ LED thật, nên buffer đen không chứng minh
phần cứng đã tắt.

### Trạng thái emotion tạm thời

Biểu cảm tạm thời (như `laugh`, `shock`) đưa `current_emotion` của
`/emotion/status` về `idle` khi hết thời hạn biểu cảm: thời lượng recording cộng
0.5 giây, 3.5 giây nếu không có recording, hoặc 2 giây cho shock.
Timer trạng thái độc lập với restore LED, nên TTS hủy timer LED không làm nhãn
emotion bị kẹt. Mỗi biểu cảm được chấp nhận vô hiệu hóa thời hạn cũ, kể cả khi
lặp cùng loại biểu cảm. Hết hạn chỉ đổi trạng thái, không di chuyển servo hay
ngắt bên đang điều khiển giọng nói/LED. `idle`, `sleepy`, `listening`, `thinking`
giữ vòng đời hiện có. Thời hạn này không xác nhận servo vật lý đã chạy xong.

### Graceful shutdown

`RGBService.stop()` đánh dấu đang đóng dưới khóa driver, sau đó dừng/join worker
**không giữ khóa này**. Handler solid và paint kiểm tra lại trạng thái đang đóng
bên trong khóa, nên worker chạy tiếp sau timeout cũng không thể ghi frame muộn.
Cuối cùng giữ khóa trong suốt lần clear đen kép và đóng driver, rồi xóa tham chiếu
driver. Gọi stop nhiều lần hoặc clear muộn đều an toàn; clear lỗi vẫn đóng driver
và truyền lỗi ra ngoài. Trước đây clear/deinit chạy trước khi dừng worker, khiến
frame đang chờ có thể bật LED lại hoặc chạm vào SPI đã đóng. Regression test dùng
strip giả và worker thread thật; chưa chứng minh tín hiệu GPIO không bị nhiễu sau
khi kernel tắt.

### Fallback shutdown cho Orange Pi

Rootfs lamp có `led-shutdown.service`, được HAL kéo vào qua drop-in
`20-led-shutdown.conf`. Khi start, service không ghi LED. Khi shutdown/reboot,
thứ tự đảo lại: đợi HAL và boot LED writer thoát, chờ **5 giây**, rồi chạy
`/usr/local/libexec/led-off.py` khi filesystem vẫn còn mount. Timeout stop
là **15 giây**. Restart riêng HAL không chạy stop của service độc lập này.
`ExecCondition` bỏ qua board khác sun60iw2 hoặc thiếu SPI3.0; Raspberry Pi không
chạy fallback này.

Frame off độc lập khớp bản hardware: 32 pixel GRB đen, 6.4 MHz, primer LOW 8 byte
và reset LOW 64 byte. Script không import HAL, không chạy demo và không đổi mux
GPIO. Script từ chối ghi khi HAL còn active/đang dừng, báo lỗi SPI ra ngoài và
chỉ log gửi xong, không coi đó là đọc lại trạng thái LED thật. Trên `.142`, hai
lượt shutdown quan sát với delay 5 giây không còn đốm, gồm lượt đang TTS/emotion;
delay 300 ms chưa giải quyết được. Đây là fallback, chưa chứng minh nguyên nhân gốc.

Triển khai bằng **gói device/rootfs lamp**, không phải update riêng HAL. Sau khi
copy thủ công, chạy `systemctl daemon-reload` rồi restart HAL để kích hoạt.
Gỡ các unit thử LED/demo cũ trước để tránh nhiều fallback cùng ghi LED.
Rollback: stop HAL, stop fallback, xóa drop-in HAL, unit và helper, reload systemd
rồi start HAL. Không stop fallback thủ công khi HAL đang chạy.

Vòng đời HAL cũng đợi tối đa **20 giây** để cleanup phần cứng (trước là 5);
cleanup chưa xong hoặc lỗi sẽ báo shutdown thất bại và thoát khác 0, thay vì báo
hoàn tất. Timeout systemd HAL vẫn là 30 giây, có khoảng cho HTTP drain 5 giây.
Không chạy cleanup thứ hai song song với owner phần cứng còn sống. Thay đổi này
sửa lỗi báo shutdown hoàn tất quá sớm đã quan sát, chưa chứng minh nguyên nhân
của mọi lần LED sáng đốm.

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

Tắt scene: `POST /scene/off` — xoá scene đang active, khôi phục LED idle, bật lại camera/speaker, nhả servo hold của scene. Một lệnh LED không transient (`/led/solid`, `/led/paint`, `/led/off`, `/led/effect`) cũng kết thúc scene và nhả hold của scene.

Scene đang active **sống sót qua các lần restart HAL service** (OTA, deploy, crash): trạng thái được persist vào sidecar theo phiên boot (`/tmp/hal-scene-state.json`, gắn với `boot_id` của kernel) và tự động kích hoạt lại khi HAL chạy trở lại, nên niềm tin của agent ("focus mode đang bật") luôn đồng bộ. Reboot toàn bộ thiết bị thì chủ đích khởi động không có scene. Các lệnh LED transient (`/led/solid`, `/led/off`, `/led/effect` với `"transient": true`, vd hiệu ứng breathing lúc boot) chỉ overlay lên strip mà không thoát scene đang active; chỉ LED override non-transient mới xoá scene.

Khi HAL restart trong lúc đang ngủ, restore scene chỉ giữ tên scene active, không áp dụng lại LED, servo, camera, mic hoặc loa. Sleep tiếp tục giữ quyền điều khiển phần cứng và các cờ mute. User LED state được load riêng; khi thức dậy bình thường, flow scene-off hiện có sẽ xoá scene đã giữ lại.

| Scene | Sáng (base) | Sáng (lamp) | Màu (K) | Servo | Camera | Mic | Speaker |
|-------|------|------|---------|-------|--------|-----|---------|
| `reading` | 80% | 19% | 4000K trắng ấm | desk + hold | off | on | off |
| `focus` | 70% | 15% | 4200K trung tính ấm | desk + hold | off | on | off |
| `relax` | 40% | 10% | 2700K ấm | wall | on | on | on |
| `movie` | 15% | 4% | 2400K amber mờ | wall | off | on | off |
| `night` | 5% | 1.2% | 1800K amber đậm | down | off | on | off |
| `energize` | 100% | 24% | 5000K ánh sáng ban ngày | up | on | on | on |

"Base" là `SCENE_PRESETS` trong `hal/presets.py`. Trên lamp, khối `scene` của
`robots/lamp/presets.json` chỉ override **độ sáng** (0.19 / 0.15 / 0.10 / 0.04 / 0.012 / 0.24);
màu, hướng aim và thiết bị ngoại vi giữ giá trị base. Ở mức base, reading/focus/energize vượt trần
`max_brightness` của lamp (120) nên cả ba bị kẹp về cùng một peak; overlay cũng tính tới việc scene
thắp cả 32 pixel (peak trên lamp: energize 61, reading 48, focus 38, relax 25, movie 10, night 3).

### Điều khiển ngoại vi theo scene

Khi kích hoạt scene, `POST /scene` thực hiện theo thứ tự:

1. **LED** — màu đặc = `preset.color × preset.brightness`
2. **Servo aim** — xoay đầu đèn theo hướng preset (desk, wall, up, down)
3. **Servo hold** — nếu `"servo": "hold"`, giữ servo **sau khi** aim xong (aim → hold trong cùng 1 thread), với chủ sở hữu là `scene`. Không giữ nếu scene đã kết thúc trong lúc tay đèn còn đang di chuyển. Được nhả khi chuyển sang scene không có hold, khi tắt scene, hoặc khi có lệnh LED không transient.
4. **Camera** — tự động bật/tắt qua `_auto_camera_on`/`_auto_camera_off`
5. **Mic** — mute dừng voice pipeline (STT), unmute khởi động lại
6. **Speaker** — `off` dừng nhạc ngay và mute giọng nói theo **drain** (`_start_scene_speaker_drain`, xem `sensing-behavior_vi.md`): câu xác nhận của chính scene, do os-server gửi sau marker `/scene`, vẫn phát xong rồi loa mới đóng; `sleepy` ghép trong cùng reply sẽ huỷ drain và mute ngay, chặn câu xác nhận đến muộn; wake khôi phục mute do sleep sở hữu. `on` bật lại output. Tắt scene khi privacy đang khoá sẽ đổi snapshot của khoá để lúc nhả loa/camera mở lại (xem `physical-controls_vi.md`).

**Chỉ có kích hoạt scene mới aim.** Một lần restore LED — sau emotion, khi TTS kết thúc, khi nhạc
dừng, khi bỏ mute mic, khi tắt cue lắng nghe — chỉ vẽ lại strip chứ không làm gì khác, và một
chuyển trạng thái presence `IDLE/AWAY → PRESENT` chỉ khôi phục ánh sáng. Trước đây cả hai đều re-aim
khi trạng thái LED đã lưu là một scene, làm chết animation đang chạy và ghim đầu ở `__aim_hold__`
trong 5s (#314). Hệ quả: khi một scene `hold` đang bật, đầu không còn tự trôi về tư thế của scene sau
khi animation kết thúc — nó nội suy về idle. Muốn khôi phục tư thế đó thì việc ấy thuộc về
`servo: hold` của scene, không thuộc về một lần vẽ lại LED.

### Chủ sở hữu của hold (#544)

Servo hold có chủ sở hữu: `scene`, `tracking`, `explicit` (`POST /servo/hold`) và `look`, được quản lý
trong `hal/drivers/motors/hold.py`. `_hold_mode` là true khi còn ít nhất một chủ sở hữu, và mỗi
đường chỉ nhả phần giữ của chính nó. Kết thúc scene không bao giờ nhả hold của tracking hay
explicit, và một phiên tracking kết thúc giữa lúc scene reading đang bật vẫn để nguyên hold của
scene và không khởi động lại idle: tay đèn ở lại chỗ tracking để lại. `POST /servo/resume` xoá mọi
chủ sở hữu. Lệnh LED kết thúc scene cũng xoá scene đã lưu, nên HAL khởi động lại sẽ không bật lại nó.
Log khi nhả cho biết tay đèn đã rảnh chưa: `Scene off: servo released` khi không còn chủ sở hữu nào,
`Scene off: scene hold released, servo still held by explicit` khi vẫn còn. Tương tự, gaze log
`framing released (servo held by scene, idle waits)` thay cho `(idle has the arm)` khi kết thúc
một cuộc hội thoại lúc servo đang bị giữ.

`look` là nội bộ của `POST /api/vision/look` ở os-server: nó claim `POST /servo/hold/claim {"owner":"look"}` trước cue "taking a look" và nhả bằng `POST /servo/hold/release {"owner":"look"}` ngay khi chụp ảnh xong, để timer idle hoặc still-emotion không thể xoay đầu giữa lúc chụp. Nhả `look` không bao giờ nhả chủ sở hữu khác: sau "quay phải và giữ nguyên ở đó" thì hold explicit vẫn giữ tay đèn. Nếu os-server không nhả (nó chết giữa chừng), HAL tự nhả hold `look` sau 30 s (`LOOK_HOLD_MAX_S`). Khi nhả, idle resume của still emotion bị hoãn trong lúc body bị giữ sẽ chạy ngay, trừ khi còn chủ sở hữu khác giữ tay đèn hoặc thiết bị đang ngủ. Khi không có resume bị hoãn nào chạy, việc nhả cũng trả tay đèn về idle (`body.release_to_idle("look hold released")`) nếu đường khác (hand-back của `/servo/search`, tracker kết thúc) đã bỏ qua việc restart idle của nó trong lúc `look` đang giữ, trừ khi body đang bị listening-halt, đang phát một recording hoặc còn owner khác giữ. Chỉ `look` được chấp nhận trên hai route này (422 nếu khác); agent vẫn dùng `POST /servo/hold`.

**Lưới an toàn.** Một hold `scene` mà không có scene nào đang active là hold cũ (stale). Nó được
nhả, kèm log `[hold] scene hold released -- no scene is active (stale)`, ở lần kế tiếp có chỗ đọc
hold: `GET /servo`, `/servo/play`, `/servo/demo`, bước trả thân về idle, hoặc một chuyển động gaze.

**Hold chặn những gì.** Animation idle và ambient, và các chuyển động tự động của gaze watcher
(pan và tilt khi canh khung, leo tìm mặt, repoint khi bắt đầu nói, nhìn quanh). Mỗi cái log lý do,
ví dụ `[gaze] no pan: servo held by scene`. Các lệnh di chuyển tường minh vẫn chạy: `/servo/aim`,
`/servo/nudge`, `/servo/move`, `/servo/search` và aim của lệnh look realtime. Trong lúc có scene,
hold ở lại tư thế mới và scene vẫn active, nên "chỉnh đèn sang trái một chút" là tinh chỉnh chế độ
đọc chứ không tắt nó.

**Camera.** Một emotion có preset bật camera sẽ để camera tắt khi scene đang active giữ nó tắt
(`reading`, `focus`, `movie`, `night`).

### Chặn emotion khi hold mode

Khi servo đang hold (reading/focus), **animation cảm xúc bị chặn** để tránh phân tâm:

- `happy`, `thinking`, `curious`, `sad`, v.v. → servo + LED bị bỏ qua
- `greeting`, `sleepy`, `stretching` → **cho qua** (đây là emotion thay đổi trạng thái: chào, ngủ, thức dậy) — **chỉ áp dụng cho hold do scene preset**

**`/servo/hold` tường minh** (lệnh agent kiểu "nhìn lên tường giữ đó") set `_hold_explicit` và chặn servo với **mọi** emotion, kể cả nhóm scene-change — trước đây `[HW:/emotion:greeting]` đứng cuối reply lợi dụng miễn trừ này, đè pose đã lệnh bằng pose cuối của animation greeting. `/servo/resume` sẽ xoá cờ. Đổi scene và lệnh LED không đụng tới hold tường minh.

Nghĩa là khi focus, sensing event vẫn tới OpenClaw nhưng Lamp giữ nguyên trạng thái vật lý — không cử động, LED ổn định.

### Lý do chọn nhiệt độ màu

- **Focus 4200K/70% base** (không phải 5000K/100%; overlay lamp 15%) — 4000-4300K tối ưu cho tập trung mà không gây mỏi mắt
- **Night 1800K amber đậm** — bước sóng >580nm không ảnh hưởng melatonin
- **Movie mic on** — cho phép điều khiển giọng nói ("pause", "stop") khi xem phim

## Status LED

Xem chi tiết: [status-led_vi.md](status-led_vi.md)

LED phản hồi trạng thái hệ thống. HAL tra từng tên state trong `STATUS_LED_PRESETS` của
`hal/presets.py`; trên lamp, khối `status_led` trong `robots/lamp/presets.json` override màu (mọi
channel cap ở 3) và, với sáu cue breathing sống lâu, cả speed. Tên effect luôn lấy từ bảng base.
Giá trị trên lamp:

| Trạng thái (preset) | Màu | Effect / speed (lamp) | RGB lamp | RGB / speed base |
|-----------|-----|-----|-----|-----|
| Mất internet (`connectivity`) | Cam | breathing 0.6 | `(3, 1, 0)` | `(16, 7, 0)` / 3.0 |
| Lỗi (`error`, dự trữ) | Đỏ | pulse 1.5 | `(3, 0, 0)` | `(16, 0, 0)` / 1.5 |
| OTA (`ota`, dự trữ) | Xanh lá | breathing 0.6 | `(0, 3, 0)` | `(0, 12, 0)` / 3.0 |
| Đang vào Wi-Fi (`wifi_connecting`, setup) | Xanh dương | blink 0.5 | `(0, 1, 3)` | `(0, 6, 16)` / 0.5 |
| Đang khởi động (`booting`) | Xanh dương | breathing 0.6 | `(0, 1, 3)` | `(0, 6, 16)` / 3.0 |
| HAL Down (`hal_down`) | Tím | breathing 0.6 | `(2, 0, 3)` | `(11, 0, 16)` / 3.0 |
| Agent Down (`agent_down`) | Cyan | breathing 0.6 | `(0, 3, 3)` | `(0, 12, 12)` / 3.0 |
| Hardware Failure (`hardware`) | Vàng | breathing 0.6 | `(3, 3, 0)` | `(12, 12, 0)` / 3.0 |
| OTA đang chạy (`ota_progress`, bootstrap) | Cam | breathing 0.4 | `(3, 1, 0)` | `(16, 8, 0)` / 0.4 |
| OTA thành công (`ota_success`, bootstrap) | Xanh lá | notification_flash 1.0 | `(0, 3, 1)` | `(0, 12, 4)` / 1.0 |
| OTA thất bại (`ota_error`, bootstrap) | Đỏ | pulse 1.5 | `(3, 1, 1)` | `(16, 2, 2)` / 1.5 |

Ưu tiên, trigger và caller xem ở [status-led_vi.md](status-led_vi.md).

Quản lý bởi `system/statusled/Service` (lamp) và `system/lib/hal` trực tiếp (bootstrap).

Không còn màu nào hardcode trong Go nữa — trạng thái `system/statusled`, màu OTA-progress
của bootstrap, và màu trắng setup-needed đều đi qua HAL. OS giữ máy trạng thái (KHI nào hiện)
và gửi *tên trạng thái* xuống HAL (`POST /led/status`: booting/error/ota/connectivity/
wifi_connecting/hal_down/agent_down/hardware/ready_flash/ota_progress/ota_error/ota_success/setup); HAL tra
màu/effect/speed từ `STATUS_LED_PRESETS`, override per-device qua section `status_led` trong
`presets.json` (xem [ROBOT-SPEC.md § Per-device presets](../../../contract/ROBOT-SPEC.md#per-device-presets-presetsjson)).
Trạng thái solid từ `/led/status` lưu thêm `source: "status:<name>"` trong
sidecar LED. Khi `/led/off` không transient gặp `source: "status:setup"`, HAL
hiểu đây là dọn cue setup: xoá cue đã lưu rồi restore đèn nghỉ đã cấu hình,
vẫn tôn trọng sleep và quyền giữ LED của privacy. API và chuỗi gọi setup của
os-server không đổi. Màu user chọn không mang source hệ thống, kể cả RGB trùng
cue setup; `/led/off` của user vẫn lưu solid đen. Off transient giữ nguyên source setup.

Không suy đoán nguồn của sidecar cũ chưa có tag. Với máy đã dính lỗi, chọn lại
resting light mong muốn trong Settings để xoá override đen cũ; tự xoá mọi solid
đen sẽ làm mất cả lựa chọn OFF thật của user.

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
  Ambient nay gọi restore thay vì tự start/stop effect. Emotion settle về indicator đỏ
  qua lịch restore của nó.

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

Khi lamp start và `config.SetUpCompleted == false` (device đang ở AP/provisioning mode), `system/server/server.go` spawn goroutine background (`waitAndPaintSetupReady` trong `system/server/config_watch.go`, chỉ trên device có capability `light`) gửi `POST /led/status` với state `setup` và retry có backoff (1 s, nhân đôi, tối đa 10 s) cho tới khi HAL xác nhận, setup hoàn tất, hoặc server tắt — HAL paint strip trắng solid báo "device ready, vào hotspot đi". Không chờ `/health` (route LED có thể xác nhận trước khi các driver không liên quan healthy); retry xử lý race lúc cold boot khi os-server bind :5000 trước HAL :5001. Không dùng state machine `statusled`. Trắng chỉ là tạm thời: `POST /api/device/setup` thành công sẽ xoá saved setup state này thay vì giữ nó thành user LED preference, rồi restore settle về ambient resting look (trắng ấm mờ, sáng đều). Blue-breathing booting vẫn show trong lúc init. Xem [setup-flow_vi.md](../../../../docs/vi/setup-flow_vi.md#ap-mode).

## Ambient Idle Behaviors

Khi Lamp nghỉ, mặc định là trắng **[1, 1, 1]**, khoảng 0,4% dải giá trị RGB,
sáng đều, không thở và không chạy thread animation. Độ sáng cảm nhận còn phụ
thuộc phần cứng LED.

### Resting look (mặc định: trắng mờ)

Cấu hình riêng theo device tại `robots/<type>/presets.json`:

```json
"ambient_led": {"resting": {"effect": "solid", "color": [1, 1, 1]}}
```

Lamp dùng giá trị trên; intern-v2 giữ [5, 4, 3]. Nếu không khai báo thì
fallback platform vẫn tắt. HAL merge vào `AMBIENT_RESTING_LED` khi khởi động.
Preset solid chỉ ghi màu một lần, không tạo effect worker. Khi emotion/TTS/music
kết thúc hoặc bỏ mic-mute, cùng resting look được khôi phục nếu chưa có tùy chọn
LED của user. Quyền ưu tiên của status, sleep và mic-privacy vẫn giữ nguyên.

OS ambient pause khi tương tác, resume sau 60 giây yên lặng (tick hai giây).
`restingLEDLoop` gọi `POST /led/restore` một lần khi resume, không tự chọn màu
hay bật breathing. HAL là nơi duy nhất quyết định resting look và màu/effect đã lưu.

Speaking wave giữ RGB nền của preset solid lúc nghỉ hoặc emotion solid khi
chưa lưu màu của user. Với Lamp nghỉ ở [1, 1, 1], wave biến thiên trên màu mờ
này, không chuyển sang trắng ấm sáng mạnh. Màu user đã lưu vẫn được ưu tiên;
tắt đèn rõ ràng vẫn giữ tối.

### Chủ máy chọn trên web UI

Settings → **Resting light** (`/setting#led`) cho chủ máy thay mặc định của device
mà không phải sửa `presets.json`. HAL cung cấp `GET /led/resting` và
`PUT /led/resting {mode, color}`:

| `mode` | Resting look |
|--------|--------------|
| `default` | Preset của device trong `presets.json` |
| `off` | Tắt (solid [0, 0, 0]) |
| `custom` | Solid `color` [R, G, B], 0-255; màu đen được chuẩn hóa thành `off` |

`hal/resting_led.py` chụp lại preset của device lúc boot, rồi ghi đè
`AMBIENT_RESTING_LED` tại chỗ, nên mọi đường restore ở trên đều theo lựa chọn này.
Lựa chọn lưu ở `/var/lib/hal/resting_led.json` (`HAL_RESTING_LED_PATH`), giữ qua
reboot và OTA, khác với user LED state chỉ sống trong một lần boot. `PUT` cũng xoá
user LED state đã lưu (kể cả lệnh tắt đèn) và vẽ lại strip trừ khi device đang
ngủ; hardware proxy của os-server mở khóa ambient restore trong cùng request.
Trang có các ô màu gợi ý, thanh trượt hue / trắng↔màu / độ sáng (tối đa kênh 64),
và cảnh báo khi màu bão hòa nằm trong 20° hue của một màu trạng thái (đỏ, vàng,
xanh lá, cyan, xanh dương, tím).

`POST /led/resting/preview {color}` vẽ thử một màu mà không lưu (bộ chọn màu
trực tiếp trên app, MQTT `led.resting.preview`). Nó không bao giờ cắt ngang lúc
ngủ, đang nói, phát nhạc hay đèn báo tắt mic (`painted: false`), và đèn quay về màu đã lưu 10 giây
sau lần preview cuối, trừ khi có `PUT` lưu trước đó.

### User tắt đèn

`POST /led/off` lưu tùy chọn solid đen [0, 0, 0], nên ambient và restore sau
hiệu ứng không tự bật đèn lại. Transient off chỉ xóa hiển thị, không thay tùy chọn.
Lệnh đặt màu, scene hay effect thay tùy chọn như cũ. State tồn tại qua restart
HAL trong cùng lần boot; reboot xóa state boot-scoped và dùng mặc định device.
Sidecar cũ `{"type":"off"}` vẫn được đổi thành không có state.

`led_should_stay_dark()` nhận cả solid đen do user chọn lẫn default tối, để
music wave và presence restore tôn trọng tắt đèn. Trạng thái voice đang hoạt
động vẫn hiển thị: listening và thinking dùng preset emotion của device dù đã
lưu tùy chọn tắt đèn; TTS dùng màu listening mờ nếu màu nền là đen. Các luồng
restore cue/kết thúc/hủy hiện có trả về tùy chọn mới nhất, kể cả off. Không ghi
đè tùy chọn, không thêm network request hoặc thời gian chờ. Sleep vẫn chặn
các cue này; mic privacy giữ ưu tiên indicator lúc nghỉ hiện có. Chưa có
tùy chọn full-blackout riêng. Intent `light on` vẫn dùng trắng ấm [255, 220, 180], không lấy
preset ambient mờ.

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

### Quyền sở hữu timer khôi phục

Mỗi timer khôi phục LED mang một mã thế hệ. Thay thế hoặc hủy sẽ vô hiệu hóa cả callback timer đã được đưa vào chạy; callback cũ không được đổi đèn hay xóa timer mới. Khôi phục trực tiếp cũng vô hiệu hóa timer đang chờ. Nếu lượt khôi phục đã bắt đầu vẽ, lượt đó hoàn tất trước khi bên mới nhận quyền, thông qua khóa vòng đời tái nhập. Không thêm cửa chờ hay network call, nhưng hủy có thể phải đợi phần vẽ/dừng hiệu ứng hiện tại; thay đổi này chưa loại bỏ giới hạn chờ thread hiệu ứng hai giây đang có.
