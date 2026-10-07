# Điều khiển vật lý — Nút GPIO, TTP223 và MPR121

Lamp hỗ trợ các nút cơ học, touchpad TTP223 và bộ điều khiển cảm ứng điện dung MPR121 tùy chọn. Ngoài điều khiển MPR121 ở Harness mode, chúng dùng chung thư viện action (`hal/drivers/button_actions.py`) nên cùng một cử chỉ "single click" sẽ hành xử giống nhau dù đến từ nút bấm cơ học hay touchpad cảm ứng.

## Thiết bị đầu vào

| Thiết bị | Vai trò | Có ở |
|---|---|---|
| **Nút GPIO** | Nút cơ chính cho click và giữ, thêm nút reset riêng trên OrangePi. Action giữ destructive chỉ thực hiện khi nhả. | Pi 4/5 và OrangePi sun60 |
| **Touchpad cảm ứng TTP223** | Headpad chỉ để vuốt ve: chạm đơn, chạm đôi, vuốt một chiều và vuốt qua lại đều phản hồi PET. Gesture điều khiển thuộc GPIO/MPR121. | Chỉ OrangePi sun60 (4 Pro / A733) |
| **Bộ điều khiển cảm ứng MPR121** | Tối đa 12 electrode, hỗ trợ click và vuốt; đã tắt giữ để sleep/shutdown. Reboot bằng chạm 3 lần đã bị vô hiệu hóa. Không bao giờ factory-reset. | Lamp khai báo cấu hình I²C cụ thể trong `mpr121.json` |

## Wiring

| Thiết bị | Pi 4/5 | OrangePi sun60 |
|---|---|---|
| Nút GPIO chính | gpiochip0 BCM 17 (pull-up, active-LOW) | Pin vật lý 35 / PD3 / gpiochip0 line 99 (pull-up, active-LOW) |
| Nút GPIO reset | không wire | Pin vật lý 37 / PD4 / gpiochip0 line 100 (pull-up, active-LOW); giữ ≥5 s rồi nhả để factory-reset |
| Công tắc gạt mic | không wire | Pin vật lý 11 / PL9 / gpiochip1 line 9; pull-up, LOW=mute, HIGH=unmute |
| TTP223 | không wire | Bốn pin header ứng viên trên gpiochip0 — pin 27 / PB5 / line 37 (T1), pin 29 / PD0 / line 96 (T2), pin 31 / PD1 / line 97 (T3), pin 33 / PD2 / line 98 (T4) — chỉ hai pin có pad, khác nhau theo từng máy. `robots/lamp/ttp223.json` liệt kê cả bốn với `"detect": true`; HAL tự tìm cặp đang nối khi khởi động. **Pull-up, active-LOW** (pad nghỉ ở mức HIGH; chạm là edge xuống). |

Wiring nút cơ thuộc về từng device: `robots/lamp/gpio_button.json` và
`robots/intern-v2/gpio_button.json` đều khai báo map `boards` với các key
`raspberry_pi_4`, `raspberry_pi_5`, `orangepi_sun60`. Entry hỗ trợ dạng phẳng
cũ `chip`, `line`, `debounce_ns` hoặc list `buttons`. Entry OrangePi của Lamp:

```json
{
  "buttons": [
    {"name": "primary", "chip": 0, "line": 99, "debounce_ns": 200000000, "behavior": "standard", "factory_reset": false},
    {"name": "factory_reset", "chip": 0, "line": 100, "debounce_ns": 200000000, "behavior": "factory_reset", "hold_s": 5}
  ]
}
```

JSON của Intern v2 giữ nguyên, với gpiochip1 line 9 trên OrangePi.
Khi đổi wiring, sửa file của device tương ứng rồi restart HAL; hệ thống không
tự phát hiện đổi dây cắm. HAL xác định thư mục qua `DEVICES_DIR` và
`DEVICE_TYPE`. `load_button_configs` cấp cấu hình cho mỗi instance driver
dùng chung; HAL dừng tất cả instance khi cleanup. `load_button_config` vẫn
tương thích với caller chỉ cần nút đầu tiên (nút chính trong cấu hình Lamp). Cấu hình device được ưu tiên.
Thiếu file hoặc entry board thì fallback về đúng một nút mặc định `button`
cũ trong `hal/board/boards.json`: chip 0 / line 17 cho Pi 4, Pi 5, CM4 và
sim; chip 1 / line 9 cho OrangePi sun60; tất cả debounce 200 ms. Config sai,
tên trùng hoặc cặp chip/line trùng bị từ chối trước khi claim GPIO. Mô phỏng
bỏ qua các nút phần cứng. Pull-up, active-LOW và nhận diện cử chỉ vẫn ở driver
dùng chung.

Wiring TTP223 cũng do device quản lý: `robots/lamp/ttp223.json` khai báo
map `boards`. Intern v2 không có phần cứng TTP223 nên không kèm file này. Mỗi entry bật có `chip`,
`lines`, `axis` tùy chọn (cùng các line đó theo thứ tự vật lý trái sang phải)
và `detect` tùy chọn (boolean, mặc định `false`).
`hal/board/ttp223.py` chọn board đã detect và truyền `TouchConfig` cho driver
dùng chung. Thiếu file hoặc entry board thì fallback về `touch` cũ của board
trong `hal/board/boards.json` (OrangePi: chip 0, line 96/100);
`"enabled": false` tắt TTP223 rõ ràng. Config sai bị từ chối trước khi claim
GPIO. Restart HAL sau khi sửa JSON của device được chọn. Pull-up, active-LOW
và nhận diện cử chỉ vẫn ở driver dùng chung; mô phỏng bỏ qua phần cứng.

JSON của Lamp cấu hình `orangepi_sun60` là chip 0, line `[37, 96, 97, 98]`
(pin header 27/29/31/33, T1–T4), không có `axis`, `"detect": true`. Chỉ hai pad
được nối và hai pad nào thì khác nhau giữa các máy, nên `lines` là danh sách ứng
viên chứ không phải danh sách pad. Khi bật `detect`, driver:

1. **Probe khi khởi động.** Mỗi line ứng viên được claim làm input với pull-down
   trong 10 ms (`PROBE_SETTLE_S`) rồi đọc. TTP223 đã nối kéo output nghỉ lên HIGH
   nên đọc được 1; pin header trống tụt về 0. Sau đó line được giải phóng và log
   in `TTP223 detect: wired pads [...] of candidates [...]`.
2. **Claim mọi line ứng viên** với pull-up / cả hai edge như cũ. Pin trống luôn
   HIGH và không sinh edge.
3. **Học từ lần chạm đã xác nhận.** Edge trên line mà probe bỏ sót không được
   đưa vào trạng thái gesture cho tới khi line ở mức LOW ít nhất 20 ms
   (`LEARN_MIN_LOW_MS`; trace trên máy cho thấy một lần chạm giữ LOW 73–135 ms).
   Khoảng LOW được đo giữa timestamp kernel của hai edge (`tick` của callback
   lgpio, đơn vị nano giây), không phải thời điểm callback chạy, nên callback bị
   trễ hoặc dồn lại không thể biến nhiễu thành lần chạm hay ngược lại.
   Edge nhả kết thúc khoảng LOW đó sẽ thêm line vào tập pad đã nối, log
   `TTP223 pad on line N learned from a Xms touch`, và chuyển lần chạm đó thành
   gesture bình thường, tính thời điểm tại lúc nhả. Khoảng LOW ngắn hơn — nhiễu
   thoáng qua trên pin trống đang pull-up — bị bỏ: không học pad, không khởi động
   timer contact và không phát chime (chỉ log ở mức DEBUG). Trường hợp cần học
   xảy ra khi pad đang bị chạm lúc probe, hoặc output đọc LOW dưới pull-down.
   Line đã học được giữ là đã nối cho tới khi HAL restart.

Luật swipe ("mọi pad đã nối") và lý do TAP so sánh với tập pad đã nối; nếu chưa
detect hay học được pad nào thì fallback về toàn bộ `lines`. Không có `detect`
thì mọi line liệt kê đều được coi là đã nối (hành vi cũ). Pin 35 (line 99) vẫn
dành cho nút cơ. Fallback cũ vẫn dùng line 96/100, trùng với nút reset; cần
giữ JSON của Lamp trên device để tránh điều đó.

Board được detect qua `/proc/device-tree/model`:
- `"sun60iw2"` → OrangePi 4 Pro / A733
- `"raspberry pi 5"` → Pi 5
- `"raspberry pi 4"` → Pi 4
- phần cứng không nhận diện được hoặc không được hỗ trợ → bị board gate từ chối khi HAL khởi động

### Công tắc gạt microphone

`robots/lamp/privacy_button.json` khai báo chip 1 / line 9 trong `orangepi_sun60`,
với `settle_s: 0.06`, `muted_level: 0`, `watchdog_s: 30`. Driver dùng chung
`privacy_button.py` theo dõi vị trí công tắc, đồng bộ lúc boot và thực hiện mute/unmute
sau khi tiếp điểm ổn định. Watchdog chỉ đồng bộ lại khi GPIO đổi mức, giữ mute
bằng phần mềm khi công tắc đứng yên. Intern v2 không có JSON mic, giữ fallback
chip 0 / line 97 cũ trong code. Lamp thiếu JSON này vẫn tắt mic switch. Cần giữ
JSON nút chính của Lamp để fallback pin 11 cũ không trùng công tắc này.
Cấu hình mic Lamp đã deploy ngày 2026-09-11; startup xác nhận chip1/line9 ready
và LOW ban đầu áp dụng mute. Chờ live test thao tác gạt.

#### Các thiết bị được khóa bởi privacy trên Lamp

`privacy_button.json` đặt `disable_camera_on_mute: true` và
`mute_speaker_on_mute: true`. Toggle pin 11 vì vậy mute mic, dừng capture camera
và dừng/chặn phát speaker (TTS, nhạc và backchannel). Đèn đỏ mic-muted hiện có
tiếp tục làm đèn báo privacy.

Khi khóa, camera enable/snapshot và speaker unmute trả HTTP 409; camera stream,
realtime look, scene/wake và việc tạm bật camera để chụp không thể mở lại camera
hay speaker. Các consumer capture không đọc được frame camera lưu đệm. Lúc HAL
khởi động, các thiết bị đã cấu hình vẫn khóa cho tới khi đồng bộ GPIO; lỗi
đọc/claim ban đầu thì tiếp tục giữ privacy khóa.

Mở khóa dùng luồng wake/listening microphone hiện có và khôi phục camera/speaker
về trạng thái trước đó. Camera hoặc speaker đã tắt trước khi khóa thì vẫn tắt;
lệnh tắt thủ công trong lúc khóa cũng được giữ lại. Chime xác nhận ngắn tuân theo trạng thái mute loa đã khôi phục; cue listening bằng lời đã tắt. Mute do **scene** đặt không phải sở thích người
dùng: khi công tắc đánh thức thiết bị khỏi sleep (scene night: camera và
speaker tắt) tắt scene trong lúc còn đang khóa, `deactivate_scene()` đổi
snapshot của privacy (`privacy.speaker_before` / `privacy.camera_before` →
`False`) để lúc nhả khóa speaker và camera mở lại thay vì khôi phục mute của
scene — cùng pattern với mute do sleep sở hữu. Khóa vẫn giữ nguyên cho tới khi
nhả; override camera thủ công vẫn được tôn trọng. Tùy chọn được giữ qua restart HAL trong cùng boot,
không lưu khóa privacy tạm thời thành mute thủ công. Hai tùy chọn mặc định false
cho device khác; Intern giữ fallback chỉ mute mic, không cần JSON.
Cập nhật HAL trước khi upload JSON có các trường mới này.

## Bảng cử chỉ

| Cử chỉ | Nút GPIO chính | Touchpad TTP223 |
|---|---|---|
| **1 chạm** | Dừng object tracking đang chạy, rồi stop loa / unmute mic + speaker + chime ack (~120 ms ping) — tất cả fire ngay khi nhả nút (không đợi click window); sự kiện click window 0.4 s vẫn được xử lý nhưng cue "Nghe đây" bằng lời đã tắt | Phản hồi PET sau cửa sổ quyết định; lần chạm đầu giữ chime xác nhận và không ngắt lời đang nói. |
| **2 chạm** (≤ 0.4 s, nút) / (≤ 1.2 s, TTP223) | Không thêm gì ngoài single-click đã fire ở chạm 1 (panic-click guard) | Phản hồi PET cho cả chạm đôi nhanh và chậm; không đổi mute mic. |
| **3 chạm** (≤ 0.4 s, nút) | Reboot OS (TTS báo → `sudo reboot`) | Không có action riêng cho chạm ba lần; các chạm gom vào nhịp PET hoặc bị cooldown bỏ qua. |
| **Swipe** qua các pad | n/a | Phản hồi PET ở cả hai hướng; không gọi sleep. |
| **Giữ 2–5 s rồi nhả** | Phát thông báo sleep theo ngôn ngữ, rồi vào `sleepy`: LED tắt, camera/mic/speaker tắt; servo release sau 1 s. Khi đang giữ LED nháy tím sleepy. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 5–10 s rồi nhả** | Shutdown OS (TTS báo → release servo → `sudo shutdown -h now`). LED nháy đỏ khi đã arm. | n/a — phần cứng TTP223 không hold đáng tin được (xem "FastMode" dưới) |
| **Giữ 10 s+ rồi nhả** | Factory-reset: wipe state thiết bị + reboot vào AP setup (TTS báo → release servo → POST `/api/system/factory-reset` trên OS server). LED đỏ đứng khi đã arm. **Tắt trên Lamp** (`"factory_reset": false` ở nút chính): giữ 10 s+ vẫn chỉ shutdown vì nút reset riêng đảm nhiệm factory-reset. | n/a |

Bảng trên mô tả nút GPIO chính và TTP223. Nút reset riêng ở pin 37 chỉ factory-reset khi nhả sau khi giữ ít nhất 5 s. Giữ ngắn hơn và single/triple tap đều không làm gì; nút này không gọi sleep hoặc shutdown. LED giữ nguyên dưới 5 s và dùng preset factory-reset đỏ đứng chung từ 5 s trở lên.

Khi Harness OFF, MPR121 cũng hỗ trợ giữ rồi nhả để thực hiện action và cùng phản hồi LED theo mức giữ, xem phần detect riêng. Mức sleep và các mức destructive **commit khi nhả, không phải khi timer fire lúc đang giữ**. MPR121 dừng ở shutdown: không có mức factory-reset, nên giữ 10 s+ trên touch vẫn chỉ shutdown (`hold_release_action(..., factory_reset=False)`). Chỉ nút GPIO mới factory-reset.

## Cắt Lamp giữa câu (barge-in)

Ở chế độ input automatic, cue "Nghe đây" bằng lời tạm tắt để thử nghiệm độ trễ
tap/wake. Các nơi gọi gesture vẫn giữ nguyên; đoạn khởi chạy TTS cũ được comment
để có thể khôi phục. Chime xác nhận ngắn vẫn còn; nó xác nhận cử chỉ, không bảo
đảm mic hoặc Gemini đã sẵn sàng. Cơ chế chặn mic khi phát TTS trả lời và độ trễ
khởi động voice 0.5 s không đổi. Cue ghi âm của tap-to-talk thủ công và Harness
vẫn giữ hành vi hiện có.

Cử chỉ 1 chạm là **cơ chế barge-in và huỷ attention chính** của Lamp: trước hết nó dừng mọi session object tracking đang chạy; sau đó chạm mặt điều khiển MPR121 hoặc nhấn nút GPIO một lần khi Lamp đang nói → cắt câu TTS đang phát giữa chừng, dừng nhạc, unmute mic để Lamp lắng nghe câu kế. Nếu loa đang bị mute bởi user/scene thì cũng được gỡ (trừ khi đang ghi âm enroll giọng) để chime và câu trả lời nghe lại được. Dừng tracking vẫn hoạt động khi hardware mic kill switch đang tắt; nó không wake hoặc unmute mic. Cue "Nghe đây" bằng lời đã tắt; chime ngắn vẫn phát khi âm thanh được phép.

Khi wake word đang bật, cú click cũng **được tính như một wake event**: `single_click_action` gọi `voice_service.grant_wakeword_focus(source)`, mở đúng cửa sổ follow-up focus (`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, mặc định 20 s) mà câu wake phrase mở ra. Không có nó thì thiết bị xác nhận cú chạm rồi lại bỏ câu trả lời của user vì thiếu wake phrase. Cửa sổ được kiểm tra lại ở thời điểm dispatch, không chỉ latch lúc mở mic session, nên click giữa lúc session đang chạy vẫn authorize câu user đang nói. No-op khi wake word tắt (mọi câu đã dispatch sẵn) hoặc timeout follow-up = 0.

### Chạm để nói với runtime trên thiết bị

Hành vi attention/wake ở trên áp dụng cho `voice_input_mode: "automatic"`, là mặc định. Chọn **Tap to talk** trong General (hoặc MQTT `voice.input_mode`) để chủ động chạm bắt đầu/kết thúc khi Harness OFF. Chế độ này giữ lựa chọn wake đã lưu nhưng bỏ qua wake gate và mọi trigger focus cho tới khi trở về automatic.

Tap ngắn GPIO và MPR121 đi qua `physical_short_tap`: tap đầu bắt đầu thu, tap tiếp theo dừng và gửi audio đã giữ trong bộ đệm cục bộ qua realtime sau khi xác thực owner của capture. Realtime trả lời hoặc delegate đến main agent; realtime tắt/không khả dụng thì fallback sang transcript STT đã chốt dưới dạng `voice_command`. Mỗi lần nhả ngắn riêng biệt đều được tính, kể cả hai tap trong cửa sổ multi-click thông thường; không phát lời Listening trì hoãn. Beep sẵn sàng và hiệu ứng listening chỉ xuất hiện khi STT sẵn sàng; beep kết thúc xác nhận tap gửi. Im lặng không gửi. Timeout (mặc định 30 giây), lỗi provider thu âm/STT, privacy/stop hoặc đổi route Harness trước tap kết thúc hợp lệ làm hủy bản ghi và không gửi sang realtime. Tap trước khi sẵn sàng hủy và không gửi.

Tap khi TTS đang phát chỉ ngắt; tap sau mới thu. Đèn đang ngủ được đánh thức trước mà chưa thu. Mic mute phần mềm có thể được mở để thu; khóa mic vật lý vẫn chặn. Hold/factory reset GPIO, swipe/hold MPR121 và cử chỉ pet TTP223 giữ vai trò hiện có. Startup và privacy-switch vẫn dùng action wake gốc và không giả lập tap ghi âm. Harness ON giữ chính sách cử chỉ riêng bên dưới.

### Presence enter và quay về phía đèn — trigger wake

Wake gate có **năm** cửa vào: wake phrase nói ra, single click, một người mới đã nhận diện, quay về phía đèn trước khi nói, và boot greeting (os-server gọi `POST /voice/wake-focus?source=boot_greeting` ngay sau khi gửi greeting, nên user trả lời được mà không cần wake phrase). Một `presence.enter` có identity đã enrolled sẽ mở đúng cửa sổ follow-up focus qua `SensingService`, nên người đã nhận diện có thể nói “hello, Leo” mà không cần gọi wake phrase trước. Event chỉ có stranger vẫn được Agent nhìn thấy nhưng mặc định không mở voice focus; họ vẫn có thể dùng wake phrase, click hoặc gaze. Đặt `HAL_PRESENCE_WAKE_STRANGERS=true` cho deployment ưu tiên guest, nơi stranger xuất hiện trong khung có thể bắt đầu hội thoại. Focus chỉ được grant sau khi event presence đã qua cooldown bình thường; nó không tự unmute hoặc tự khởi động mic đang không sẵn sàng.

**Quay mặt về phía đèn rồi nói** cũng mở cùng cửa sổ đó (`hal/drivers/tracking/gaze.py`), qua `voice_service.grant_wakeword_focus(source)` giống presence enter và cú click — mọi thứ phía sau gate không đổi.

Khi kiểm tra gaze vừa grant focus ở lúc VAD xác nhận có tiếng nói, Lamp lập tức hiện một **nhịp thở xanh dương mờ**. Đây chỉ là cue LED: không nhận là emotion `listening` và không dừng thân đèn. STT partial đầu tiên thay nó bằng cue listening đầy đủ; phiên không có partial sẽ restore LED trước đó khi đóng, kèm timeout an toàn 3 giây nếu kết nối STT bị treo. Cue sớm này chỉ dành cho gaze wake mới được grant và không ở shadow mode — VAD/nhiễu thông thường vẫn không làm LED phản hồi.

Lý do nằm ở hình dạng sản phẩm chứ không phải sở thích. Đèn bàn nằm cách user một cánh tay và trong tầm nhìn cả ngày, nên lặp wake phrase vài chục lần một ngày nghe như đang ra lệnh cho một thiết bị, còn bấm nút thì như đang vận hành máy. Giữa hai người, tín hiệu không phải hai thứ đó: người ta **quay về phía nhau rồi nói**. Các sản phẩm phổ biến hoá "hey <name>" đều không có camera và đặt ở đầu kia phòng, nên không so sánh trực tiếp được.

Hai đặc tính quyết định cách implement:

* **Người ta quay TRƯỚC khi nói, không bao giờ sau.** Bình thường tiếng nói chỉ kích hoạt việc watcher **đọc ngược** ring buffer (`HAL_GAZE_BUFFER_S`, mặc định 4 s) — đúng mô hình pre-roll lookback của mic để không mất âm đầu câu. Có một nhánh recovery: nếu lần đọc này có ít hơn hai mẫu mặt dùng được, VAD yêu cầu watcher khôi phục pose user đã nhớ mà không chặn việc thu audio. Trước khi dispatch **chính transcript đó**, gaze được kiểm tra thêm một lần. Đầu đã đo được là đang quay đi sẽ không vào nhánh này, nên tiếng nói nghe ké vẫn không làm lamp quay về ai đó rồi mở gate.
* **Có mặt người KHÔNG phải tín hiệu.** User ngồi cạnh đèn cả ngày nên "phát hiện có người" gần như luôn đúng và không lọc được gì; "phát hiện có mặt" cũng chỉ hơn chút — mặt quay về màn hình vẫn detect ra. Gate đặt trên **hướng đầu**, đủ chặt để loại tư thế rất thường gặp: nói chuyện với đồng nghiệp trong khi thân vẫn hướng về bàn.

Head yaw suy ra từ 5 landmark mà `YuNet` vốn đã trả về (`detect_face_with_landmarks` trong `detection.py`): độ lệch của mũi so với trung điểm hai mắt, đo **dọc theo đường nối hai mắt** và chuẩn hoá bằng nửa khoảng cách hai mắt, chính là `sin(yaw)` dưới phép chiếu pinhole. Đo dọc đường nối mắt thay vì theo trục x của ảnh là thứ giữ cho đầu **nghiêng** (chống tay lên má) không bị đọc thành đầu quay. Không load thêm model nào, không chạy thêm inference nào; ở `HAL_GAZE_SAMPLE_FPS` (mặc định 6) chi phí là số lẻ trên CPU 8 nhân — đo thật chứ không suy đoán: CPU idle 69.2% xuống 68.8% khi watcher chạy.

Landmark nằm ngoài khung không phải là một phép đo. `YuNet` trả về đủ 5 điểm cho một khuôn mặt bị mép khung cắt hệt như cho khuôn mặt nằm trọn trong khung, và những điểm bị cắt quay về với toạ độ ngoài khung — đo thật trên máy, user ngồi thẳng trước đèn còn camera thì ngắm quá thấp: box `[264, -1, 162, 92]`, hai mắt ở `y = -3.0` và `y = -1.3`. Đưa vào công thức yaw, các toạ độ đó đẩy tỉ số mũi vượt 1, chỗ mà lệnh clamp biến "không đo được" thành đúng `90.0` — không phân biệt được với một khuôn mặt nghiêng thật, và bị đếm là một phiếu **chống** hướng về đèn. Đó chính là lý do user đang nhìn thẳng vào đèn lại cho ra `trail=[90,90,90,90]` và bị từ chối. Nên mẫu nào có mắt hoặc mũi rơi ra ngoài khung sẽ được ghi là **không đo được** — không bỏ phiếu theo chiều nào, giống hệt frame không thấy mặt. Khoé miệng bị cắt thì bỏ qua — góc quay không bao giờ đọc tới chúng.

Trước tất cả những thứ trên, các dòng detector có bbox không phải số hữu hạn bị loại thẳng. YuNet có thể trả về toạ độ vô cực cho một khuôn mặt đang rời khung — quan sát thật trên máy khi đang tracking, `bbox_area` 1.9%, conf 0.29 — và `int()` trên nó ném `OverflowError`, giết luôn thread detect của tracker giữa phiên. Vô cực không phải là "mặt rất to", nó là detector nói rằng không có gì dùng được; nên bỏ dòng đó đi và để đường "frame này không thấy mặt" vốn có xử lý tiếp. Bộ lọc chạy **trước** bước chọn mặt to nhất / gần tâm nhất, vì chiều rộng vô cực thắng mọi cuộc so diện tích và sẽ che mất một khuôn mặt hoàn toàn dùng được.

Khi trong khung có nhiều mặt, mặt được tính là mặt **gần tâm khung nhất** trong số những mặt cao ít nhất `HAL_GAZE_BEARING_MIN_FACE_HEIGHT_FRAC` (15%) khung hình, và không bao giờ dưới `HAL_GAZE_MIN_FACE_PX` — không phải mặt to nhất. Lấy mặt to nhất tức là trao gate cho bất kỳ ai ghé vào gần hơn, và người đó là user chỉ theo thông lệ; chính hướng ngắm của đèn mới là tiên nghiệm tốt hơn cho câu hỏi nó đang chĩa vào mặt nào. Khi chỉ có một mặt đạt ngưỡng thì hai luật cho cùng kết quả, nên thay đổi này chỉ có tác dụng khi thực sự có người thứ hai chung bàn. Nếu không ai qua ngưỡng kích thước thì không có mặt nào (#567): mặt nhỏ hơn là của đồng nghiệp bên kia phòng, và việc vẫn trả nó về đã kéo phần pan về phía họ và che mất thân của chính user khỏi watcher. Khi đó mẫu đi theo đường không có mặt, nơi phát hiện người vẫn ghi nhận ai đang ở trước đèn. Lưu ý đường tracking chỉ lấy bbox (`_detect_face_yunet`, dùng cho object follow) vẫn giữ chính sách mặt-to-nhất của riêng nó — hai bên độc lập.

| Env var | Mặc định | Chỉnh cái gì |
|---|---|---|
| `HAL_GAZE_WAKE` | `false` | Công tắc tổng cho **toàn bộ watcher**, không chỉ riêng cửa gaze: `start()` thoát ngay khi tắt, nên canh giữa theo chiều dọc, leo tìm mặt, xoay ngang, repoint và quét tự động (xem `vision-tracking_vi.md`) cũng không chạy. Tắt vẫn giữ cửa wake phrase, click và presence enter. Image của lamp đặt `true`. |
| `HAL_PRESENCE_WAKE_STRANGERS` | `false` | Cho `presence.enter` chỉ có stranger mở voice focus. Để tắt nếu guest phải dùng tín hiệu nói ra, chạm hoặc gaze. |
| `HAL_GAZE_SHADOW` | `true` | Chỉ log quyết định, không mở gate. Không tốn gì — không turn nào mở nên không tốn LLM hay TTS. |
| `HAL_GAZE_MAX_YAW_DEG` | 25 | Nón chấp nhận ở giữa khung. |
| `HAL_GAZE_EDGE_CONE_SCALE` | 1.8 | Nón nới rộng bao nhiêu ở rìa khung, nơi barrel distortion thổi phồng góc. |
| `HAL_GAZE_MIN_FACE_PX` | 48 | Chiều cao mặt tối thiểu **tính bằng pixel của khung đã thu nhỏ**. Đây không phải ngưỡng duy nhất: bộ chọn mặt còn bỏ mọi mặt dưới `HAL_GAZE_BEARING_MIN_FACE_HEIGHT_FRAC` (15%) chiều cao khung (#567), nên trên khung 640×360 của lamp, ngưỡng thực tế cho phiếu bầu, pan và "có mặt trong khung" là 54 px — watcher nhận diện trên `frame_utils.downscale(frame)`, hàm này kẹp chiều rộng về `VISION_MAX_WIDTH` (640), nên ở 1280×720 ngưỡng này là 96 px trên ảnh gốc, còn ở 640 hoặc nhỏ hơn thì là 48 px trên cả hai. Dưới ngưỡng này landmark chỉ cách nhau vài pixel, góc tính ra là số học trên sai số làm tròn, nên mẫu đó không được bỏ phiếu. Khác `LOOK_AIM_MIN_FACE_HEIGHT_FRAC` vốn là tỉ lệ nên miễn nhiễm, giá trị này âm thầm gấp đôi hoặc giảm nửa nếu chế độ camera đổi. |
| `HAL_GAZE_WINDOW_S` | 1.5 | Cửa sổ bằng chứng, kết thúc tại thời điểm bắt đầu nói. |
| `HAL_GAZE_MIN_FACING_RATIO` | 0.6 | Tỉ lệ mẫu trong cửa sổ phải thấy đầu hướng về đèn. Là TỈ LỆ, không phải chuỗi liên tục — yaw từng mẫu nhiễu thật. |
| `HAL_GAZE_MIN_SAMPLES` | 2 | Dưới mức này không đủ bằng chứng để kết luận theo chiều nào. Vòng lặp thực tế chỉ đạt ~2 mẫu/s dù cấu hình bao nhiêu — nó bị chặn bởi việc lấy frame và chạy detector — nên để 3 là loại oan cả user mà mọi tầng khác đều đồng ý là đang nhìn đèn. Dòng log `[gaze] sampling at N/s` đếm số mẫu THỰC SỰ ghi được, và báo riêng số frame bị chặn trước khi kịp đo (đang chờ servo ổn định, hoặc detector đang bị một lệnh `look` giữ). Đếm số lần thử thay vì số mẫu từng báo 5.7/s trong khi buffer không có gì mới hơn cửa sổ 1.5 s — tức dưới 1 mẫu/s bằng chứng thật. |
| `HAL_GAZE_SAMPLE_FPS` | 6 | Tần suất lấy mẫu. Cử chỉ thì chậm, nhưng quyết định là một cuộc bỏ phiếu và chỉ mẫu đo được mới tính — ở 3 fps cửa sổ thường chỉ còn một mẫu dùng được, từ chối cả user đang nhìn thẳng vào đèn. |
| `HAL_GAZE_BUFFER_S` | 4.0 | Lịch sử yaw giữ lại. Phải lớn hơn `WINDOW_S` để phần đọc ngược nhìn đủ xa về trước. Đã có lúc phải gấp đôi, vì một phép kiểm tra transition nay đã bị gỡ bỏ; giữ 4.0 vì thêm một giây không tốn gì và `trail=` đọc dễ hơn khi có nhiều lịch sử phía sau. |
| `HAL_GAZE_WAKE_FOCUS_S` | 10 | Cửa sổ follow-up mà một lần wake bằng *gaze* mở ra, ngắn hơn 20 s của wake phrase hay click. Một cái liếc mắt đòi hỏi ít hơn một hành động có chủ ý. Bị chặn trên bởi `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, không bao giờ vượt qua. |
| `HAL_GAZE_COOLDOWN_S` | 5 | Khoảng cách tối thiểu giữa hai lần gaze mở gate. |
| `HAL_GAZE_REPOINT` | `true` | Quay về bearing đã nhớ khi lâu không thấy ai. |
| `HAL_GAZE_REPOINT_AFTER_S` | 12 | Phải vắng mặt bao lâu mới quay. Recovery do voice kích hoạt khi không có evidence sẽ bỏ qua khoảng chờ này, nhưng không bỏ qua cooldown di chuyển. |
| `HAL_GAZE_REPOINT_COOLDOWN_S` | 60 | Tối đa một lần quay trong khoảng này, kể cả recovery do voice kích hoạt. |
| `HAL_GAZE_REPOINT_MIN_CONFIDENCE` | 0.2 | Dưới confidence này thì bearing không đáng để quay. Khớp với ngưỡng của chính look-aim: ở 0.5 watcher từ chối đúng những bearing mà aim và search vẫn đang dùng bình thường — một bearing đủ tốt để ngắm cho một turn hội thoại đang chạy thì cũng đủ tốt để quay đầu về phía đó giữa hai turn. |
| `HAL_GAZE_REPOINT_SKIP_IF_FACE_S` | 3 | Từ chối reacquire do speech kích hoạt nếu vừa thấy mặt trong khoảng này. Sau khi leo tìm đã thấy mặt user *cao hơn* bearing, tuân theo bearing nghĩa là quay ngược xuống nhìn vào chỗ không có ai. |
| `HAL_GAZE_WELL_FRAMED_EDGE` | 0.6 | Mặt được lệch khỏi tâm khung bao nhiêu mà vẫn tính là "có người ở đây, không cần quay". Mặt sát rìa là mặt sắp ra khỏi khung; coi nó là đã vào khung tử tế chính là thứ khiến bộ đếm vắng mặt reset mãi mãi trong khi user trôi dần ra khỏi tầm nhìn — đo được ở edge 0,71–0,75 mà đèn vẫn từ chối repoint. |

Image của lamp chủ động override `HAL_GAZE_MAX_YAW_DEG` thành **60°**. Đây là calibration riêng cho thiết bị, không phải mặc định chung: trên lamp-0c89, YuNet đo user đang nhìn thẳng vào camera qua kính thành 55,7–59,1°. Ngưỡng tối thiểu hai mẫu hợp lệ và phiếu bầu 60% vẫn giữ nguyên, nên một frame đơn lẻ vẫn không đủ để mở gate.

Hai tham số trong đó là **đo ra**, không phải chọn. `MIN_FACE_PX` có vì probe trên thiết bị bắt được ba đồng nghiệp ở nền cỡ 8–18 px cho ra yaw 49 / 20 / 29 — nhiễu thuần — bên cạnh người dùng ngồi tại bàn cỡ 78 px với yaw 90 hoàn toàn đúng; hai nhóm không chồng lấn nên ngưỡng này xoá cả một lớp rác chứ không phải chỉnh cho vừa. `MIN_FACING_RATIO` có vì trail của một người ngồi yên đọc ra `[10,15,8,25,36,1,-,90]`, mức dao động mà không cái đầu nào làm được, nên mọi luật đòi MỌI mẫu phải đạt đều sẽ loại oan họ.

Lâu không thấy ai mà đèn tự quay: đó là `REPOINT`. Trước đây nó là thứ **duy nhất** trong watcher động vào thân đèn; giờ thì không còn — canh giữa theo chiều dọc, leo tìm mặt, xoay ngang và quét tự động đều động vào thân đèn, và tất cả được mô tả trong `vision-tracking_vi.md` chứ không phải ở đây, vì chúng nói về việc *đưa user vào khung hình* chứ không phải về việc mở gate. Recording idle là một vòng lặp các pose tuyệt đối, đảo `base_pitch` khoảng 17° mỗi chu kỳ, nên dù đặt đèn ở đâu thì idle cũng kéo camera về pose ghi sẵn của chính nó — trên bàn làm việc thì đó là bàn phím. Đặt pose đã nhớ một lần sẽ bị vòng lặp kế tiếp ghi đè; muốn đậu đúng ở bearing thì phải offset toàn bộ playback theo bearing, việc đó thuộc về motion playback chứ không thuộc tính năng này. Nên đèn làm điều mà con người làm: không thấy ai có thể đang nói với mình thì quay về chỗ người đó hay ngồi, một lần, rồi chờ.

Shadow mode tồn tại chính để một buổi chạy cạnh user thật cho ra số liệu (`[gaze] speech: yaw=… facing=…%/…% -> WOULD_WAKE`) đủ để chốt các ngưỡng trên.

**Thực sự phải đúng những gì thì gaze mới arm.** `HAL_GAZE_WAKE` tự gọi mình là công tắc tổng, và nó là điều kiện cần chứ không đủ — có bốn điều kiện, và ba trong số đó nằm ở chỗ khác chứ không phải bảng gaze:

| # | Điều kiện | Nằm ở đâu |
|---|---|---|
| 1 | `LOOK_AIM_ENABLED` | biến môi trường `HAL_LOOK_AIM` — cả watcher lẫn bearing sampler đều khởi động *bên trong* khối look-aim (`hal/server.py:816`) |
| 2 | có camera trong mount plan | khai báo thiết bị — `"camera" in _plan.mounted` |
| 3 | wake word đang bật | **`config.json` của os-server, key `wakeword`** — đọc qua `_os_cfg_get("wakeword", False)`. **Không có** biến môi trường `HAL_WAKEWORD_ENABLED`; đặt nó ra cũng không có tác dụng gì |
| 4 | `HAL_GAZE_WAKE` | bảng gaze ở trên |

Tắt look-aim là điều kiện dễ bất ngờ nhất: nó âm thầm tắt luôn cửa wake thứ ba *và* bộ học bearing thụ động, mà không chỗ nào trong hai thứ đó nhắc tới look-aim. Nếu watcher không chạy mà bảng trông vẫn đúng, hãy kiểm tra 1–3 trước khi nghi 4 — dòng log cần tìm là `[gaze] not starting: wake word disabled, nothing to gate`.

Suy biến sạch theo cả hai chiều. Máy **không có camera** thì gaze lẫn presence enter từ camera đều không thể arm, còn cửa wake phrase và click vẫn nguyên vẹn — không cần cấu hình riêng. Khi wake word **tắt** thì watcher không khởi động luôn: không có wake word thì mọi câu đã dispatch sẵn, không còn gate nào để mở, chạy tiếp chỉ tốn CPU để quyết định một thứ vô nghĩa. Một mẫu gaze cũng bị bỏ qua khi đầu đang **đổi chỗ**, khi camera bị tắt vì quyền riêng tư, và khi detector lock đang do một `look` đang chạy giữ.

**Đổi chỗ, chứ không phải chỉ đang ghi servo.** Có hai trạng thái ghi servo liên tục mà không đưa đầu đi đâu cả: vòng idle đang thở, và một phiên tracking đang bám mặt user. Coi hai thứ đó là "đang di chuyển" thì `last_servo_write` không bao giờ cũ và gần như mọi frame đều bị từ chối — đo thật, idle: ghi được 0.3 mẫu/s trên 4.9/s bị chặn; đo thật, tracking: 0.7/s trên 4.5/s, từ chối một user ở yaw 0.9° với mặt 130px ngay giữa khung chỉ vì cửa sổ có 1 mẫu thay vì 2. Tracking là trường hợp quan trọng nhất: đó chính là lúc đèn đang bám theo mặt user, nên từ chối nhận ra người ta đang nói với nó đúng lúc đó là khoảnh khắc trông hỏng nhất có thể — vì vậy test settle không được phép biến thành `_tracking_active` qua cửa sau. Cả hai đều là chỉnh nhỏ liên tục, góc yaw sống sót qua chúng. Dòng `[gaze] sampling at N/s; blocked: …` tách số frame bị chặn theo từng lý do, vì hai cổng đó sửa ở hai chỗ khác nhau.

Chuỗi end-to-end:
1. `gpio_button.py` / `mpr121.py` (Harness OFF) detect single click → gọi `single_click_action(source)` trong `button_actions.py`. TTP223 không nằm trong chuỗi này: mọi cử chỉ TTP223 gọi `head_pat_action` và không bao giờ dừng giọng nói.
2. `single_click_action` → `_cancel_agent_speech()` (thread fire-and-forget) + `tracker_service.stop()` nếu đang tracking + `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + chime xác nhận ngắn (cue listening bằng lời đã tắt)
2a. `_cancel_agent_speech()` → `POST /api/agent/speech/cancel` lên OS server. Cần vì `stop_tts()` chỉ bịt được thứ HAL đang giữ: câu đang phát cộng hàng đợi đã pre-synth. OS server đẩy câu trả lời theo từng câu, nên không có call này thì thiết bị im đúng một câu rồi nói tiếp. OS server bịt miệng mọi turn đang chạy (xem `docs/os-server.md`) nhưng vẫn cho turn bắt đầu sau cú click nói — nên user chạm xong nói câu mới được ngay kể cả khi còn backlog turn cũ đang chạy nốt. Turn không bị abort, chỉ là không được nói — cũng vì thế mà call này bỏ luôn filler dead-air còn treo của những turn đó: filler nói thẳng xuống HAL chứ không đi qua đường reply bị bịt, nên một turn đã huỷ mà vẫn chạy cứ tiếp tục rao "một giây nhé" cho câu trả lời nó sẽ không bao giờ nói. Chạy trên thread riêng và fire ở cả hai nhánh (unmute mic và stop loa), vì kiểu gì cú chạm cũng có nghĩa là user đang giành lượt nói.
2b. `state.note_music_cancel()` → đóng dấu watermark huỷ nhạc ở phía HAL, và `audio_stop()` chạy ở **cả hai** nhánh (unmute mic và stop loa), không chỉ nhánh stop loa. Cần vì cancel ở OS server chỉ tác động lên TTS: turn bị huỷ vẫn chạy tiếp và tool call nhạc còn treo của nó vẫn tới `POST /audio/play` ngay sau đó, nơi một thread `music-play` mới tự `_stop_event.clear()` — nên một cú stop tại một thời điểm luôn thua cuộc đua này, và user nghe đúng bài nhạc mình vừa huỷ sau khi `yt-dlp` resolve xong (1–5 s). Trong lúc watermark còn tươi (`app_state.MUSIC_CANCEL_GUARD_S`, 3 s), `/audio/play` trả `{"status": "suppressed"}` thay vì phát. Cửa sổ được chọn đủ phủ tool call đang bay nhưng vẫn dưới sàn của một yêu cầu mới thật sự (nói → STT → LLM → tool không bao giờ dưới ~3 s), nên "chạm xong xin bài hát" vẫn chạy bình thường.
3. `stop_tts()` → `tts_service.stop()` set `_stop_event`; mọi blocking loop trong TTS stream (synth, render, playback) check event và abort sạch, không để loa kẹt

### Cắt lời bằng giọng nói (không có)

Không có đường "nói trong lúc TTS đang phát để Lamp dừng". Một bộ phát hiện cục bộ trên mic đã khử vọng từng chạy trong thời gian ngắn rồi bị gỡ bỏ sau một kết luận đo đạc: trên thân máy này, phần dư vọng âm sót lại sau AEC3 nằm *trên* mức một lần cắt lời thật ở mọi âm lượng loa (trần vọng âm 9804 / 9969 / 13560 ở 25 / 40 / 65 %, so với cắt lời thật 6956–8027), và chấm 79 cửa sổ đã gán nhãn trên mọi đặc trưng sẵn có cho AUC tốt nhất 0.72 — không ngưỡng nào đạt 0 % tự cắt lời mà không bỏ sót 90–100 % lần cắt lời thật. Hồ sơ đo đạc, và bài kiểm tra chấp nhận mà mọi lần thử lại phải vượt qua, nằm ở `docs/vi/realtime-voice_vi.md` (*Vì sao không có cắt lời bằng giọng nói trên mic đã khử vọng*).

Vì vậy cắt lời chỉ đến từ hai nơi: **tap-to-interrupt** ở trên, trên đường lượt, và **VAD của nhà cung cấp** bên trong một phiên live (`HAL_LIVE_MODE`, xem *Chế độ live* trong `docs/vi/realtime-voice_vi.md`), phát ra `InterruptedOutput` khi người dùng nói đè lên câu trả lời.

## Detect nút GPIO (`hal/drivers/gpio_button.py`)

Cùng driver phục vụ từng nút được cấu hình một cách độc lập. Flow dưới đây mô tả `behavior: "standard"` (nút chính). Với `behavior: "factory_reset"`, nhả sau `hold_s` (5 s ở pin 37 của Lamp) gọi `factory_reset_action` dùng chung; giữ ngắn hơn và mọi chuỗi tap đều bị bỏ qua. Hold watcher chỉ chọn mức LED factory-reset dùng chung khi đạt ngưỡng đó.

Driver đếm edge nơi **mọi destructive action commit ở rising edge (nhả) dựa trên thời lượng giữ** — không timer nào fire lúc đang giữ. Đây chính là cái cho phép user huỷ giữa chừng (nhả trước ngưỡng) hoặc escalate (giữ tiếp quá 10 s).

1. **Falling edge (nhấn):** ghi `press_start` (đồng hồ monotonic) và spawn thread hold-LED watcher (mỗi lần nhấn 1 thread, có stop `Event` riêng). Không arm timer action nào.
2. **Rising edge (nhả):** dừng LED watcher, tính `held = now − press_start`, scrub click đang chờ cho mọi hold từ 2 s trở lên, rồi chốt LED feedback (đỏ đứng cho shutdown/factory reset). Sau đó nó truyền duration vào `hold_release_action(held, source)` off-thread. Mapping action này chọn:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → `factory_reset_action`, trừ khi nút khai `"factory_reset": false` (nút chính Lamp) thì giữ ở `shutdown_action` và không bao giờ hiện mức đỏ đứng.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → `shutdown_action`.
   - `held >= 2 s` (`SLEEP_HOLD_DURATION`) → `sleep_action`, hàm gọi pipeline emotion `sleepy` chuẩn.
   - khác (tap ngắn) → `click_count += 1` và (re)start click-window timer 0.4 s. Ở tap **đầu tiên** của chuỗi, phần im lặng của `single_click_action` (`announce=False`) fire ngay off-thread — nó không phá huỷ ("cho tôi nói"), nên không cần đợi window. Sự kiện listening-cue trì hoãn vẫn còn nhưng không khởi chạy TTS bằng lời.
3. Khi click window hết:
   - `count == 3` → `triple_click_action` (không cue — chỉ announce reboot)
   - count khác → `announce_listening_cue` nhận sự kiện trì hoãn đúng 1 lần mỗi chuỗi nhưng không nói; `count == 2` / `>= 4` log thêm ignored (panic-click guard — floor-grab đã chạy ở tap 1, không gì phá huỷ fire)

Release edge không có press khớp (press bị debounce nuốt) thì bỏ qua — `press_start` có thể là cũ, hành động theo nó có thể fire destructive action trên timestamp cũ vài phút. Destructive action chạy trên daemon thread riêng vì callback `lgpio` phải return ngay, nếu không các edge sau sẽ dồn hàng.

### LED feedback khi giữ

Với nút chính, thread watcher GPIO poll thời lượng giữ và chọn mức giữ. `HoldLEDFeedback` dùng chung trong `hal/drivers/button_actions.py`, cũng được MPR121 sử dụng, đẩy LED RGB ở priority HIGH (preempt emotion hiện tại) để user thấy đã arm tới đâu trước khi nhả:

| Thời gian giữ | LED | Ý nghĩa |
|---|---|---|
| < 2 s | giữ nguyên | một tap ngắn |
| 2–5 s | tím sleepy, nháy 2 Hz | đã arm sleepy; nhả ra sẽ vào sleep (LED sau đó tắt) |
| 5–10 s | đỏ, nháy 2 Hz | đã arm shutdown — nhả bây giờ là tắt máy |
| 10 s+ | đỏ, đứng | đã arm factory-reset — nhả bây giờ là wipe + reboot (bỏ qua khi `factory_reset: false`; vẫn đỏ nháy) |

Nút reset riêng chỉ dùng preset `factory_reset` đỏ đứng khi giữ ≥5 s; nhả trước 5 s không làm gì. Không factory-reset khi còn giữ. Cả hai nút GPIO dùng lại phần xử lý feedback này và thư viện action hiện có.

Màu tím nhận diện mức sleep; đỏ nháy vs đỏ đứng phân biệt shutdown với factory-reset. LED là no-op im lặng khi RGB service không có (máy dev) — nút vẫn hoạt động.

Ba màu này là preset chứ không phải hằng nhúng cứng trong driver: `BUTTON_LED_PRESETS` trong `hal/presets.py` (`sleep_warn` / `shutdown_warn` / `factory_reset`), device override được qua section `button_led` của `robots/<id>/presets.json` giống mọi bảng LED khác. `HoldLEDFeedback` dùng chung xử lý nháy, dọn phản hồi khi nhả và phản hồi chốt action; mỗi input cung cấp mức giữ đã detect. Nó đọc màu ngay lúc paint, vì overlay merge bảng tại chỗ lúc boot.

Debounce mỗi edge là 200 ms (tick nhấn và nhả track độc lập để tap nhanh không bị drop trong khi bounce lặp của cùng một edge bị lọc).

## Detect MPR121 (`hal/drivers/mpr121.py`)

Wiring MPR121 theo flow cấu hình GPIO do từng device quản lý: HAL đọc
`mpr121.json` trong thư mục được chọn qua `DEVICES_DIR` và `DEVICE_TYPE`
(hiện là `robots/lamp/`), rồi chọn board đã detect từ map `boards`. Hiện chỉ
Lamp có phần cứng này; Intern v2 không có khai báo MPR121. Phải khai báo
bus I²C đã xác minh; driver không quét bus hay đoán wiring. Cấu hình Lamp
`orangepi_sun60` dùng bus **0**, địa chỉ **0x5A**. Đã xác minh khởi tạo và
polling trên Lamp `lamp-0c4e` ngày 2026-09-11, gồm khởi động trong HAL thật.
Ba phiên chạm rồi nhả cũng tạo tap ID riêng và hoàn tất action single-click
thật trong service đó. Script phần cứng chỉ định chân header 3/5 (TWI0).
Kiểm tra wiring và bật đúng controller I²C bên ngoài HAL
trước khi dùng; HAL không tự sửa boot overlay:

```json
{
  "boards": {
    "orangepi_sun60": {
      "enabled": true,
      "bus": 0,
      "address": 90,
      "electrodes": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
      "swipe_axis": [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
      "touch_threshold": 6,
      "release_threshold": 4,
      "autoconfig": true,
      "poll_ms": 10,
      "debounce_ms": 30,
      "chip_debounce": 2,
      "tap_min_electrodes": 3,
      "ffi": 34,
      "sfi": 10,
      "esi_ms": 1
    }
  }
}
```

`bus` bắt buộc với entry bật. Lamp đặt rõ ngưỡng chạm/nhả `6 / 4` trong
`mpr121.json`; nếu bỏ qua ngưỡng thì vẫn dùng mặc định chung `2 / 1` của
`MPR121Config`. Các giá trị còn lại ở trên trừ `swipe_axis`, `ffi`, `chip_debounce` và `tap_min_electrodes` là mặc định;
địa chỉ 90 nghĩa là `0x5A` (cho phép 90–93). Electrode được chọn phải là
các số không trùng từ 0–11, có ít nhất một electrode. Ngưỡng phải thỏa
`0 <= release_threshold < touch_threshold <= 255`. Polling cho phép 1–1000 ms;
debounce cho phép 0–1000 ms. Cần chỉnh ngưỡng theo electrode đã lắp và nhiễu
motor. Cấu hình được đọc lúc khởi động; sửa xong phải restart HAL.

Lamp đặt `tap_min_electrodes: 3`: cần ít nhất ba điện cực được chọn chạm đồng
thời, sau lọc từng điện cực, liên tục đủ `debounce_ms` (30 ms) mới công nhận tap.
Khi đã đủ điều kiện thì giữ tới lúc nhả hết, nên nhấc ngón tay lần lượt vẫn chỉ
ra một tap. Chạm 1–2 điện cực hoặc điện cực thứ ba nhảy rất ngắn không tạo action
single, cue hay tăng đếm multi-tap. Đếm các điện cực đang active được chọn, không
đếm delta `touched` trong log hay cộng dồn các điện cực đã đi qua. Vuốt vẫn theo
luật di chuyển cũ, kể cả chỉ chạm một điện cực ở mỗi thời điểm. Nhận diện giữ,
gồm giữ hai giây để thoát Harness, không đổi. Bộ lọc cũng áp dụng cho tap capture
của Harness và cấu hình không có swipe axis.

Mặc định chung là 1 (hành vi cũ); chỉ nhận số nguyên từ 1 tới số điện cực được
chọn. Tap thật bằng đầu ngón tay chỉ phủ 1–2 điện cực cũng bị bỏ qua. Đây là lọc
gesture, chưa sửa nguyên nhân nhiễu cảm biến. Log startup ghi ngưỡng; contact
ngắn không đủ điều kiện ghi `event=tap_discarded reason=insufficient_electrodes`.

Cập nhật HAL hỗ trợ field này **trước** device declaration mới; HAL cũ sẽ từ chối
key mới vì cấu hình không hợp lệ. Restart HAL để nhận cấu hình.

Driver đặt bộ lọc baseline chiều xuống (`0x2F`–`0x32`) thành
`MHDF=1, NHDF=1, NCLF=255, FDLF=2`, theo
[giá trị quick-start NXP AN3944](https://www.nxp.com/docs/en/application-note/AN3944.pdf).
Thiết lập này làm chậm baseline khi giảm để tránh bám nhanh theo ngón tay
đang tiếp cận. Bộ lọc baseline chiều lên và khi đang chạm giữ nguyên.
Bộ lọc mẫu của chip đặt trong `mpr121.json`: `ffi` (số lần lọc cấp một,
`CONFIG1` `0x5C` và autoconfig `0x7B`; 6, 10, 18 hoặc 34), `sfi` (số mẫu lọc
cấp hai, `CONFIG2` `0x5D`; 4, 6, 10 hoặc 18) và `esi_ms` (chu kỳ lấy mẫu,
`0x5D`; 1–128 ms, lũy thừa của 2). Mặc định `6 / 10 / 1`. Dữ liệu electrode cập
nhật mỗi `sfi × esi_ms` ms; giữ gần chu kỳ poll 10 ms để vuốt vẫn nhạy. Thời
gian nạp giữ 0,5 µs. Lamp đặt `ffi: 34, sfi: 10, esi_ms: 1`: trên `lamp-a0ae`
(01/10/2026, dừng HAL, 7 s mỗi cấu hình, không chạm) delta dương cao nhất lúc
không chạm giảm từ 4 count (mặc định) xuống 0, vẫn cập nhật mỗi 10 ms;
`lamp-8e2c` từng có đỉnh nhiễu 8 so với ngưỡng chạm 6 ở mặc định, gây tự chạm.
Bộ lọc không làm đổi độ lớn delta khi chạm. Debounce trên chip (`0x5B`) lấy từ
`chip_debounce` (0–7, mặc định 0), ghi cho cả DR (nhả) và DT (chạm). Giá trị mã hóa N
yêu cầu N+1 lần phát hiện chạm hoặc nhả liên tiếp trước khi đổi trạng thái:
0 cần một lần; 2 cần ba lần. Lamp đặt `chip_debounce: 2`
(`0x5B = 0x22`) cùng ngưỡng `6 / 4`, là giá trị đã kiểm chứng trên phần cứng với
`mpr121_opi_test.py test --touch 6 --release 4 --debounce 2`. Debounce contact
(30 ms) và footprint vuốt (5 ms) ở phần mềm vẫn áp dụng thêm; mỗi chuyển trạng thái
chạm/nhả cần thêm hai lần phát hiện liên tiếp so với `chip_debounce: 0`.
Xem [NXP AN3892, trang 7](https://www.nxp.com/docs/en/application-note/AN3892.pdf#page=7).
HAL kiểm tra giá trị bộ lọc lúc khởi động.
Khi chỉnh ngưỡng, kiểm tra độ ổn định lúc không chạm, tap, giữ và vuốt trên
các pad đã lắp (script probe độc lập `mpr121_opi_test.py` mà phần này từng nhắc
tới không có trong repo; `hal/test/test_mpr121*.py` chỉ kiểm tra logic driver).
Dừng HAL trước khi probe bus thủ công; HAL giữ bus.

Thiếu file, thiếu entry board, hoặc `"enabled": false` thì bỏ qua MPR121 và
giữ các handler GPIO/TTP223 hiện có. Không có bus MPR121 cũ để fallback.
Cấu hình bật nhưng sai bị từ chối khi startup; chế độ mô phỏng bỏ qua phần cứng.
Nếu bus I²C đã cấu hình không tồn tại hoặc sensor không phản hồi ACK, khởi tạo ghi `MPR121 event=unavailable` ở mức WARNING kèm bus, address và errno, không có traceback. MPR121 không hoạt động nhưng GPIO/TTP223 vẫn chạy; không khởi chạy worker touch và đóng bus đã mở. Lỗi quyền truy cập và lỗi bất thường vẫn giữ traceback mức ERROR. Không đổi `enabled` hay tự retry. Kiểm tra bus và wiring, sửa `bus` nếu cần rồi restart HAL.

Sau khởi tạo, driver chờ cảm biến ổn định 100 ms trước khi đọc trạng thái
chạm ban đầu, rồi poll mỗi 10 ms theo mặc định. Chuyển trạng thái chạm và
nhả dùng debounce 30 ms. Chạm chồng nhau trên các electrode được chọn tính
là một contact; nhả nghĩa là **toàn bộ electrode được chọn** đã nhả.
Contact đang bị giữ khi startup bị bỏ qua đến khi nhả.

MPR121 dùng chung ngưỡng cử chỉ từ `hal/drivers/button_gestures.py` với GPIO
(được `button_actions.py` re-export) và gọi các action hiện có **khi Harness mode OFF**. Harness ON dùng chính sách riêng bên dưới:

| Cử chỉ | Action MPR121 (Harness OFF) |
|---|---|
| Lần nhả ngắn đầu tiên trong chuỗi click | `single_click_action(source="MPR121", announce=False)` dừng tracking/audio sau khi phân giải contact, unmute khi được phép và phát ack chime. |
| 1, 2 hoặc 4+ tap ngắn, rồi yên 0.4 s | Xử lý sự kiện cue nghe nhưng không nói; các tap lặp không gọi lại action single-click ban đầu. |
| Đúng 3 tap ngắn, rồi yên 0.4 s | Reboot bị vô hiệu hóa tại wrapper MPR121; không có action bổ sung hoặc cue nghe. Action single-click ở tap đầu vẫn chạy. |
| Giữ 2–<5 s rồi nhả | Đã tắt; không sleep. |
| Giữ ≥5 s rồi nhả | Đã tắt; không shutdown hay factory reset. |
| Vuốt phải sang trái rồi nhả (user ngồi đối diện lamp) | `swipe_action` sleep; contact di chuyển này không gọi click hoặc action destructive. |
| Vuốt trái sang phải rồi nhả (user ngồi đối diện lamp) | Bật Harness voice qua API Go; contact di chuyển này không gọi click hoặc action destructive. |

Contact ngắn kéo dài dưới 2 s. Cửa sổ click không phân giải khi còn bất kỳ
electrode được chọn nào đang chạm. Nhả sau giữ xóa chuỗi click đang chờ.
Action destructive không chạy khi còn giữ.

### Vuốt MPR121 theo hướng

Mọi hướng mô tả cho người dùng ở đây đều theo góc nhìn người ngồi **đối diện
lamp**, không phải trái/phải của bản thân lamp.

`swipe_axis` là list tùy chọn gồm 2–12 electrode khác nhau, thuộc `electrodes`.
`robots/lamp/mpr121.json` của Lamp khai báo E11…E0: trên cụm đã lắp, E11 nằm
bên phải của user, E0 nằm bên trái. Tăng vị trí trên trục (`+1`, user vuốt
phải sang trái) gọi `swipe_action(source="MPR121")` trong `button_actions.py`
để sleep. Giảm vị trí (`-1`, user vuốt trái sang phải) bật Harness voice-only
mode. Các action này áp dụng khi Harness OFF; khi ON, phải sang trái chọn
agent trước, trái sang phải chọn agent kế tiếp. Cần kiểm tra vị trí electrode
khi lắp lamp; thứ tự mảng xác định dấu hướng của detector, không phải chiều
trái sang phải theo góc nhìn user.
Không cần vuốt hết toàn bộ dải: tâm chạm phải dịch ít nhất 3 vị trí trong ít nhất 30 ms. Vuốt nhanh có thể bỏ qua pad có thời gian chạm ngắn hơn một poll cộng bộ lọc vùng chạm; tâm chạm nhảy quá 3 vị trí được chấp nhận khi đang di chuyển tiếp cùng hướng, ngược lại bị coi là ngón thứ hai và huỷ. Thiếu/null
`swipe_axis` chỉ tắt nhận diện vuốt, giữ nhận diện click/hold cũ.
Cài HAL hỗ trợ trước khi deploy JSON có trường này.

Debounce contact vẫn mặc định 30 ms; vùng chạm dùng tối đa 5 ms ổn định
(thường là hai poll liên tiếp cách 10 ms) để giữ các chuyển tiếp electrode nhanh.
Detector theo dõi vùng chạm đã debounce thay vì đếm mỗi electrode chạm chồng
thành một tap. Chạm nhiều electrode nhưng đứng yên vẫn giữ hành vi click/hold.
Khi phát hiện di chuyển, hủy kết quả tap/hold đang chờ và phản hồi LED giữ cho
contact đó; vuốt hợp lệ gọi action theo hướng một lần sau khi nhả. Di chuyển
đổi hướng trong cùng contact hoặc không hợp lệ không gọi reboot/shutdown/reset. Chờ nhả 120 ms để nối các đoạn
chuyển tiếp ngắn giữa electrode; khi bật swipe, tap/hold phân giải sau khoảng
chờ này. Contact giữ từ lúc boot vẫn bị bỏ qua. Log ghi hướng, độ dịch chuyển,
kết quả swipe và thực thi action. Test phát lại chuỗi mask đã đo cùng các ca
cử chỉ/vòng đời giả lập. Runtime và JSON swipe đã deploy lên Lamp `lamp-0c4e`
ngày 2026-09-11; startup xác nhận MPR121 ready với trục cấu hình, GPIO và TTP223
ready, mic switch Lamp ready trên chip1/line9 sau khi cài JSON. Chờ live test cử chỉ.

Đã tắt action giữ để sleep/shutdown của MPR121 cùng LED tím/đỏ báo sắp
thực hiện. Detector vẫn nhận chạm giữ để lúc nhả không bị đổi thành tap.
Giữ nút GPIO, vuốt theo hướng và giữ 2 giây thoát Harness khi Harness ON
không đổi. MPR121 không factory reset.

Worker action bất đồng bộ có hàng đợi giới hạn giữ polling phản hồi kịp thời.
Action dư có thể bị bỏ; chạm mới, stop hoặc lỗi phần cứng làm mất
hiệu lực các action destructive và cue nghe cũ đang chờ. Lỗi I²C hoặc cờ quá
dòng MPR121 (`OVCF`) được log và dừng driver này, các handler đầu vào hiện có
tiếp tục chạy.

Lần xác minh phần cứng ở trên chỉ kiểm tra hành vi single-click trước đây.
Phản hồi LED khi giữ được kiểm tra bằng test mock local, chưa kiểm tra trên
device thật. Các test này không thực thi reboot, shutdown hay reset thật.

Log hoạt động dùng logger `hal.drivers.mpr121` trong log/journal HAL thông
thường; không tạo file raw trace riêng. Log INFO gồm khởi tạo và cấu hình
(bus, địa chỉ, electrode, ngưỡng và thời gian), thay đổi chạm/nhả thô trên từng
electrode, chuyển trạng thái đã debounce, chạm lúc startup bị bỏ qua, xếp hàng
hoặc bỏ action, số click, thời lượng/mức giữ, bắt đầu/kết thúc action và
vòng đời driver. `gesture_id` liên kết chuỗi click hoặc giữ với action đã
xếp hàng, bỏ hoặc thực thi. Khi lỗi có log lỗi.
Các lần poll 10 ms không đổi trạng thái không tạo log INFO, tránh tràn log
khi không chạm. Theo dõi bằng `journalctl -u hal.service -f` và lọc
`hal.drivers.mpr121` khi cần tìm nguyên nhân mất hoặc lặp tap.

## Detect TTP223 (`hal/drivers/ttp223.py`)

Headpad TTP223 chỉ dùng để vuốt ve. Chạm đơn, chạm đôi nhanh/chậm, vuốt một
chiều và vuốt qua lại đều gọi `head_pat_action`. GPIO/MPR121 giữ mapping điều
khiển riêng. TTP223 không stop/unmute, đổi mute mic, sleep, reboot, shutdown
hay factory-reset thiết bị.

Pad FastMode không đo được giữ ngón tay tin cậy. Cross-talk cũng khiến một lần
chạm sinh nhiều edge, nên giữ phần gom và phân loại hiện có:

1. Mỗi edge đặt lại timer tiếp xúc **200 ms**.
2. Phản hồi tiếp xúc đầu của TTP223 dùng âm lướt xuống nhẹ 180 ms (520 → 360 Hz), thay tiếng ping xác nhận lệnh. Âm phát một lần đầu chuỗi chạm, tuân theo mute và âm lượng loa, không dừng lời đang nói. Âm GPIO, MPR121 và Harness giữ nguyên.
3. PET rõ ràng có thể phân giải sớm; các tiếp xúc khác đợi cửa sổ quyết định
   **1,2 s**. Mỗi gesture hợp lệ được phân giải gọi cùng action PET một lần.
4. Mỗi lần thử phản hồi đặt cooldown **1,5 s**. Tiếp xúc trong khoảng này kéo
   dài cooldown, tránh chạm liên tục tạo nhiều phản hồi nối nhau.

`HAL_TOUCH_SWIPE=true` giữ phân loại không gian và phát hiện PET sớm. Khi
`false`, gom theo số tiếp xúc. Cả hai vẫn chỉ phản hồi PET. Trace giữ nhãn nhận
diện `TAP`, `DOUBLE_TAP`, `SWIPE` hoặc `PET`, nhưng action luôn là
`head_pat_action`. Chime tiếp xúc đầu là phản hồi riêng. Driver không còn các
nhánh action điều khiển bị parked.

| Setting | Mặc định | Mục đích |
|---|---|---|
| `SESSION_GAP_S` | 0,2 s | Gom cross-talk và edge nhả tự động |
| `DECISION_WINDOW_S` | 1,2 s | Gom tiếp xúc trước khi phản hồi |
| `PET_SESSION_THRESHOLD` | 2 | Ngưỡng PET sớm theo số tiếp xúc |
| `PET_COOLDOWN_S` | 1,5 s | Khoảng yên cần có sau lần thử phản hồi |
| `HAL_TOUCH_SWIPE` | `true` | Phân loại không gian và nhận PET sớm |
| `HAL_TOUCH_SWIPE_MIN_GAP_MS` | 35 | Cận dưới phân loại di chuyển |
| `HAL_TOUCH_SWIPE_MAX_GAP_MS` | 150 | Cận trên phân biệt vuốt với lần chạm mới |
| `HAL_TOUCH_PRESS_MIN_EMPTY_MS` | 15 | Khoảng mặt pad trống để tính lần chạm mới |

`ttp223.json` cung cấp chip, lines, `axis` không gian tùy chọn và `detect` tùy
chọn; thiếu axis thì dùng thứ tự lines (tập con đã detect giữ nguyên thứ tự đó). Hình học chỉ ảnh hưởng phân loại/thời điểm, không đổi action
PET. Test `hal/test/test_ttp223.py` phủ hai trạng thái classifier, layout hai/ba
pad, các kiểu gesture, cooldown và không ngắt lời ở tiếp xúc đầu.

### Trace lại chuyện thực sự đã xảy ra (`HAL_TOUCH_DEBUG`)

Hai trong bốn dòng log quyết định ở trên là `logger.debug` nên không bao giờ xuất hiện ở mức `HAL_LOG_LEVEL=INFO` đang ship, còn `_on_edge` thì vứt bỏ thông tin pad nào đã bắn trước khi có gì kịp ghi lại. Vì vậy khi một cú chạm làm sai, bình thường không có cách nào biết được là pad bắn nhầm, tầng session gộp sai, hay action làm điều gì đó ngoài dự tính.

`hal/drivers/touch_debug.py` bịt khoảng trống đó. **Mặc định TẮT** — khi không đặt env, nó tốn đúng một phép kiểm tra boolean đã cache cho mỗi edge, không mở file nào và không tạo thread nào. Đặt `HAL_TOUCH_DEBUG=1` trong `/opt/hal/.env` rồi restart HAL để bật.

Nó ghi một file JSON cho mỗi cử chỉ đã phân giải, đặt tên `<timestamp>_<ACTION>.json` để một phân loại sai nhìn thấy được ngay từ `ls` (`20260827-114032_TAP.json`, `..._PET.json`, `..._IGNORED-pet_cooldown.json`, `..._IGNORED-settle.json`). Mỗi file chứa bốn tầng: `edges` (line nào, mức nào, lúc nào, và có bị chốt chặn `SETTLE_S` chặn không), `sessions` (tầng 200 ms đã gộp chúng ra sao, kèm `primary_pad`, `adjacent_deltas_ms` và `span_ms` của từng lần tiếp xúc), `traversal` (chuỗi pad trải qua nhiều session và số `reversals`) và `action` (cái gì đã chạy, trên trạng thái thiết bị nào). Ngoài ra còn một dòng tóm tắt `TOUCH-TRACE` ghi ở mức INFO.

Nó cố ý không bao giờ log vào journald: HAL log nhiều đến mức cửa sổ journal của `hal.service` chỉ tính bằng phút, nên một bản trace để ở đó sẽ bị đẩy mất trước khi kịp đọc.

| Biến env | Mặc định | Điều chỉnh |
|---|---|---|
| `HAL_TOUCH_DEBUG` | `false` | Công tắc chính. Tắt = mọi điểm vào đều là no-op. |
| `HAL_TOUCH_DEBUG_DIR` | `touch_logs/` cạnh module | Thư mục output. Rơi về thư mục tạm nếu cây mã chỉ đọc. |
| `HAL_TOUCH_DEBUG_MAX_ENTRIES` | 200 | Giới hạn số file, cũ nhất bị dọn ở mỗi lần ghi. 0 = không giới hạn. |
| `HAL_TOUCH_DEBUG_PADS` | _(không đặt)_ | Map line→nhãn, ví dụ `96=S1,98=S3`. Không đặt thì pad được đặt tên theo số line — các tên S lịch sử không đi theo thứ tự line sau hai lần dời chân, nên driver không đoán chúng. |


## Thư viện action chung (`hal/drivers/button_actions.py`)

Các action sống ở một chỗ để nút GPIO, TTP223, MPR121, và mọi input tương lai (touchpad, remote) hành xử giống nhau:

| Hàm | Làm gì | Cắt TTS đang phát? |
|---|---|---|
| `single_click_action(source)` | Dừng object tracking đang chạy. Sau đó gỡ mute loa do user/scene (bỏ qua khi `_enrolling`). Đóng dấu watermark hủy nhạc và dừng nhạc — ở **cả hai** nhánh, để một cú click luôn dập được thứ ồn nhất trong phòng. Rồi nếu mic bị mute → unmute; ngược lại thì stop TTS. Rồi mở cửa sổ follow-up wake word (no-op khi wake word tắt) và phát chime xác nhận ngắn; cue "Nghe đây" bằng lời đã tắt. Tracking vẫn dừng khi hardware mic kill switch đang tắt; action voice vẫn bị chặn. | Có — gọi `stop_tts()`. |
| `triple_click_action(source)` | Chỉ map gesture: gọi `reboot_action(source)`. | Có |
| `reboot_action(source)` | Nói "Đang khởi động lại" → đợi 5 s cho clip cached → `reboot_os()` (`sudo reboot`). | Có |
| `sleep_action(source)` | Phát thông báo sleep theo ngôn ngữ, rồi gọi `sleepy`: LED tắt, camera/mic/speaker tắt, rồi release servo sau 1 s. | Có — pipeline sleepy dừng TTS/nhạc đang phát sau thông báo. |
| `hold_release_action(held, source)` | Mapping signal hold: chọn sleep, shutdown hoặc factory reset theo duration lúc nhả. | Tuỳ action được chọn |
| `shutdown_action(source)` | Nói "Đang tắt máy" → đợi 5 s → `release_servos()` (để đèn không slam xuống giữa pose) → `shutdown_os()` (`sudo shutdown -h now`). | Có |
| `factory_reset_action(source)` | Nói "Đang khôi phục cài đặt gốc. Đang khởi động lại" → `release_servos()` → POST `/api/system/factory-reset` trên OS server (server lo phần wipe + reboot, xem dưới). | Có |
| `swipe_action(source)` | Luôn gọi `sleep_action`. Không dựa vào hướng (một cú swipe "sai chiều" sẽ không làm gì mà cũng không có phản hồi giải thích vì sao) và không dựa vào trạng thái (một cử chỉ mang hai nghĩa tùy vào thứ người dùng không nhìn thấy). Trên thiết bị đang ngủ, `sleep_action` thoát sớm. | Có |
| `mic_toggle_action(source)` | Toggle mute mic. **Hiện không có nơi gọi** — double tap TTP223 nay đi vào `head_pat_action`, còn GPIO/MPR121 không map double tap. Từ chối khi công tắc mic phần cứng đang tắt hoặc đang ghi âm enroll giọng. Sau khi lật, nó nói ra **trạng thái** kết quả, chọn ngẫu nhiên từ `MIC_MUTED_PHRASES_BY_LANG` / `MIC_UNMUTED_PHRASES_BY_LANG` bằng chính giọng của lamp ("[whispers] Suỵt, mình bịt tai lại rồi." / "[excited] Mình mở tai ra rồi nè!"), để giọng nói và đèn mic-muted khớp nhau; một lần toggle bị từ chối sẽ im lặng chứ không thông báo về một lệnh mute chưa từng xảy ra. | Không — không cắt lời, bỏ qua nếu TTS đang bận |
| `head_pat_action(source)` | Chọn câu PET local ngẫu nhiên, gọi `speak_cached` trên thread riêng rồi báo OS khi được nhận. Mọi gesture TTP223 đều gọi action này và không dừng lời nói trước đó. | Không chủ động stop; theo quy tắc nhận phát hiện có của TTS. |

### Factory-reset: wipe những gì

`factory_reset_action` chỉ **báo + uỷ quyền** — phần reset thật nằm ở OS server (`system/server/system/factoryreset.go`), gọi được từ thiết bị qua loopback không cần Bearer token (authoritative nhờ hiện diện vật lý: giữ có chủ ý 10 s trên nút GPIO chính hoặc 5 s trên nút reset riêng; MPR121 không kích được, rồi nhả). `POST /api/system/factory-reset` là reset **mềm** (wipe state, không reflash — kernel / package OS / binary / `.venv` HAL không bị đụng):

1. Wipe state của agent backend đang chạy (OpenClaw hoặc Hermes, auto-detect từ `config.json` `agent_runtime`).
2. Wipe các path state của thiết bị: `/root/config` (config.json — API key, channel token, MQTT creds), `/root/local/users` + `/root/local/strangers` (enrollment khuôn mặt/giọng), `/var/lib/hal/snapshots` (snapshot camera), và `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (WiFi nhà → ép vào AP mode lần boot kế).
3. Reboot. Thiết bị lên lại ở AP mode `<device_type>-XXXX` với setup wizard mới (~30 s).

Reset là **single-flight** + cooldown 5 phút (`FactoryResetMinInterval`) dùng chung cho mọi trigger (giữ GPIO, HTTP, MQTT) — circuit breaker chống caller chạy loạn và lặp do vô tình.

## Persist mute/disable qua HAL restart

**Sleep cũng persist theo cách này** (`/tmp/hal-sleep-state.json`). Nó cùng loại
với các switch người dùng thấy được: ai đó — hoặc một scene ban đêm — đã cho
thiết bị ngủ, và restart HAL không được phép huỷ điều đó. OTA thì restart HAL,
nên trước khi có sidecar này, một lần update lúc 3 giờ sáng là thiết bị tỉnh dậy:
đèn sáng lại, mic nghe lại, sensing hết bị gate. Sidecar này còn mang theo mute mic/loa **do chính sleep sở hữu** — chúng cố ý không nằm trong sidecar mic/speaker để lúc thức trả switch về đúng lựa chọn của user, mà hệ quả trước đây là restart xong máy nghe lại được và một turn agent còn đang bay vẫn nói thành tiếng. `POST /emotion` ghi cờ mỗi lần
nó đổi, và lifespan trong `server.py` express lại `sleepy` sau khi driver đã lên,
để thiết bị TRÔNG vẫn đang ngủ chứ không phải boot vào look nghỉ với cái cờ được
set âm thầm. Driver chuyển động cũng được yêu cầu khởi động **không** kèm chuỗi
thức dậy (`start(skip_wake=True)`): startup pose là một cú move 5 giây rồi tới
idle loop, nên sửa sau nghĩa là con lamp đang ngủ vẫn đứng dậy, cử động, rồi mới
nằm xuống lại. Khôi phục cờ ngay lúc import — trước khi driver start — chính là
thứ cho phép BỎ QUA thay vì hoàn tác. Reboot cả máy thì vẫn tỉnh như cũ.

### Lịch sử ngủ — file thứ hai, trả lời câu hỏi khác

Sidecar ở trên chỉ trả lời được *"ngay lúc này có đang ngủ không"*: nó giữ đúng
một bản ghi, mỗi lần chuyển trạng thái là ghi đè, và reboot thì xoá luôn. Nên
thiết bị không nói được nó đã ngủ bao nhiêu lần — hỏi thẳng thì agent không có
gì để đọc, và không biết là mình đã từng ngủ.

`_log_sleep_transition` (`app_state.py`) append mọi lần chuyển trạng thái vào
`/root/local/device/sleep/YYYY-MM-DD.jsonl` (`HAL_SLEEP_LOG_DIR`, giữ 30 ngày
theo `HAL_SLEEP_LOG_MAX_DAYS`):

```json
{"ts":1758000000.12,"local":"2026-09-16T22:00:00+07:00","tz":"Asia/Ho_Chi_Minh","date":"2026-09-16","hour":22,"event":"sleep","emotion":"sleepy","source":"api"}
{"ts":1758021600.45,"local":"2026-09-17T06:00:00+07:00","tz":"Asia/Ho_Chi_Minh","date":"2026-09-17","hour":6,"event":"wake","emotion":"stretching","source":"button"}
```

Nằm ở chỗ persistent chứ không phải `HAL_STATE_DIR`, vì reboot không được phép
xoá lịch sử — ngược hẳn với thứ sidecar cần. Không có gì trong HAL đọc lại file
này; nó tồn tại cho agent, và agent truy vấn qua skill Sensing Track.

Cả hai lệnh ghi đều nằm trong block chuyển trạng thái của `POST /emotion`, vì đó
là nơi **cả bốn** đường vào/ra giấc ngủ hợp lưu: marker của agent, nút bấm,
`presence.enter` → `greeting`, và web UI qua hardware proxy. Chính vị trí đó là
lý do file này tồn tại. Flow event `hw_emotion` của os-server chỉ ghi được những
marker do chính nó bắn, nên bỏ sót toàn bộ các lần ngủ vật lý — khoảng một nửa,
và đúng là nửa do con người trực tiếp gây ra. Lỗi ghi được log rồi nuốt: một bản
ghi về giấc ngủ không đáng giá bằng chính giấc ngủ đó.

`local` là giờ tường của chính thiết bị kèm offset UTC, lấy qua
`hal/clock.py` nên bám theo `/etc/timezone` HIỆN TẠI chứ không phải zone glibc
cache lúc process khởi động — người dùng đổi múi giờ từ web UI
(`/setting#timezone`) hoặc app lúc nào cũng được. `tz` ghi tên zone đó, và rỗng
đúng khi không resolve được và dòng đó rơi về giờ naive, nhờ vậy đồng hồ sai lộ
ra trong dữ liệu chứ không ẩn đi. `ts` vẫn là khoá sắp xếp và là trường duy nhất
trừ được an toàn khi múi giờ đổi giữa chừng.

`source` ghi nguyên nhân (`button` / `touch` / `MPR121`, hoặc `api` cho marker
và web UI — hai thứ này HAL chưa phân biệt được). Một `sleepy` gửi lại cho thiết
bị đang ngủ không phải là chuyển trạng thái nên không ghi gì, nhờ vậy mọi dòng
đều là thật và đếm trực tiếp được.

Mic mute, speaker mute và camera disable mỗi cái persist vào một sidecar
boot-scoped riêng — `/tmp/hal-mic-state.json`, `/tmp/hal-speaker-state.json`,
`/tmp/hal-camera-state.json` (cùng pattern `boot_id` với sidecar LED/scene) —
nên HAL restart (OTA, deploy, đổi config) không còn âm thầm unmute mic, mở lại
speaker hay bật lại camera. Mọi route flip switch đều persist (`/voice/mute|unmute`,
`/speaker/mute|unmute`, `/camera/disable|enable`, scene đổi mic/speaker,
`_auto_camera_on/off`); gesture nút/touchpad đi qua đúng các route đó. Khi
restore: `start_voice` tạo voice pipeline nhưng không mở mic, lifespan trong
`server.py` không start camera capture và vẽ lại đèn báo mic-muted, còn cờ
speaker không cần bước apply (TTS check lúc speak). Reboot nguyên máy thì bắt
đầu fresh (Intern v2 Pro có công tắc gạt tự apply lại). Mute speaker transient
của record-enroll chủ đích KHÔNG persist.

Tất cả sidecar này nằm trong `HAL_STATE_DIR` (mặc định `/tmp`, đúng các đường dẫn
ở trên). Nó sinh ra để trỏ đi chỗ khác: bộ test HAL cấp cho mỗi lần chạy một thư
mục riêng, vì các file này sống lâu hơn tiến trình — trước đó một lần chạy kết
thúc lúc thân máy đang ngủ sẽ khiến **mọi** lần chạy sau khởi động ở trạng thái
ngủ, và chạy suite trên máy thật thì ghi đè luôn công tắc thật của máy đó.

## Phrase local

Thông báo của các action đều local theo `stt_language` từ `config.json` của Lamp. Hằng số ngôn ngữ ở `hal/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Fallback về `DEFAULT_LANG` (English) khi ngôn ngữ hiện tại chưa có bản dịch.

### Thông báo an toàn (1 câu/ngôn ngữ)

Các câu xác nhận của **toggle mic** là những pool bằng giọng persona, giống các câu pet — nói đi nói lại đúng một câu chính là thứ khiến nó nghe như máy. Ràng buộc giữ cho chúng an toàn là mọi câu vẫn phải nói rõ *toggle đã đi theo chiều nào*: sự ấm áp nằm ở cách diễn đạt, không bao giờ nằm ở nghĩa. "Suỵt, mình bịt tai lại rồi" thì đạt; một câu "Suỵt!" trơ trọi thì không, vì một điều khiển riêng tư mà người dùng không giải mã được còn tệ hơn một câu máy móc. Có test ép buộc điều này.

`reboot`, `shutdown`, và `factory-reset` dùng phrase nghĩa-đen ("Đang khởi động lại", "Đang tắt máy", "Đang khôi phục cài đặt gốc. Đang khởi động lại") ở mọi ngôn ngữ vì user vừa làm cử chỉ destructive và cần xác nhận rõ ràng — đây là thông báo an toàn, không phải khoảnh khắc persona.

### Phrase pet (15 câu/ngôn ngữ, random)

Phrase pet chọn ngẫu nhiên từ pool 15 câu mỗi ngôn ngữ để Lamp không nói robot khi bị vuốt liên tục. Tone phản ánh tính cách Lamp (AI companion + smart light + expressive robot, "như pet/friend"):

- Nhột / cười nhỏ: "Hihi, nhột quá!" / "Hehe, that tickles!"
- Pet-like kêu rừ rừ: "Mình kêu rừ rừ nè!" / "I'm purring." / "我咕噜咕噜啦！"
- Light-themed (Lamp = luminous): "Mình sáng cả lên rồi nè!" / "You light me up."
- Tim ấm: "Tim mình ấm lên!" / "My heart's glowing."
- Xin thêm: "Vuốt nữa đi mà!" / "More, please!"
- Khen người vuốt: "Mình mê cái này lắm!" / "You're the best."
- Eo nũng: "Vuốt nhẹ thôi nha~" / "Stop it, you!"

Phrase cố tình ngắn — chúng fire giữa lúc vuốt nên cần cảm giác responsive.

## File

| Đường dẫn | Mục đích |
|---|---|
| `hal/drivers/gpio_button.py` | Handler nút GPIO (cơ học, cả hai board) |
| `hal/board/ttp223.py` | Đọc cấu hình TTP223 theo device, có fallback cũ |
| `hal/drivers/ttp223.py` | Handler touchpad cảm ứng TTP223 (chỉ OrangePi sun60) |
| `hal/board/mpr121.py` | Đọc và kiểm tra cấu hình MPR121 do device quản lý |
| `hal/drivers/mpr121.py` | Handler I²C MPR121 tùy chọn, detect click/giữ |
| `hal/drivers/harness/gestures.py` | Chính sách gesture riêng cho Harness mode |
| `hal/drivers/voice/_internal/harness_capture.py` | Quản lý quyền sở hữu capture Harness thủ công |
| `hal/drivers/button_gestures.py` | Ngưỡng cử chỉ dùng chung GPIO/MPR121 |
| `hal/drivers/button_actions.py` | Hàm action chung, `HoldLEDFeedback` cho GPIO/MPR121 và pool phrase local |
| `hal/presets.py` | Hằng số mã ngôn ngữ (`LANG_EN`, v.v.) |
| `hal/test_ttp223_probe_orangepi.py` | Probe pad độc lập (ioctl thuần stdlib, không cần gpiod). `info` đọc trạng thái line khi HAL vẫn chạy; `watch` map pad→line và cần dừng `hal.service`. Line lấy từ `ttp223.json` của device được chọn, fallback về board profile cũ giống HAL. Chọn device bằng `--device-type`. |
| `hal/test_gpio.py` | Probe độc lập để verify line nút GPIO |

Các handler đầu vào được khởi động trong startup lifespan `hal/server.py`. Thiếu cấu hình MPR121 tùy chọn thì bỏ qua driver đó; cấu hình bật nhưng sai bị từ chối khi startup. Lỗi driver phần cứng được log mà không dừng các handler còn lại.


### Gesture MPR121 theo Harness mode

Trên đèn MPR121, hướng vuốt theo góc nhìn user ngồi **đối diện lamp**. Khi Harness OFF, vuốt **trái sang phải** để bật Harness voice-only mode, **phải sang trái** để sleep. Harness ON dùng mapping riêng thay cho click, vuốt sleep và listening cue (giữ sleep/shutdown và triple tap reboot thường đã tắt): tap điều khiển capture hoặc ngắt TTS; giữ **đủ 2 giây** tắt Harness và thông báo ngay (kể cả offline), không cần nhả; phần chạm còn lại bị bỏ qua tới khi buông tay; vuốt **trái sang phải** chọn agent kế tiếp, **phải sang trái** chọn agent trước. `hal/drivers/harness/gestures.py` quản lý gesture riêng này; `hal/drivers/voice/_internal/harness_capture.py` quản lý quyền sở hữu capture thủ công. GPIO/TTP223 không đổi. `swipe_axis` E11…E0 của Lamp chạy từ phải sang trái theo góc nhìn user (`+1`); chiều ngược lại là `-1`. Python gọi API Go; Go quản lý mode/focus và route voice hiện có.

Khi Harness voice mode ON, yêu cầu sleep bị chặn. Nếu không đọc được mode từ OS, sleep cũng bị chặn tới khi xác nhận OFF; bước chuyển emotion kiểm tra lại sau lời thông báo sleep. Swipe khi đang ngủ không được bật Harness hoặc đánh thức lamp; cần tap wake trước khi swipe. Nếu Harness được bật từ bên ngoài (ví dụ UI/API) khi lamp đã ngủ, lamp không tự wake. Khi privacy microphone đang mở, tap hợp lệ đầu tiên chỉ wake và phục hồi thiết bị ngoại vi do sleep quản lý; không bắt đầu ghi âm, kể cả khi wake thất bại. Phải tap thêm lần nữa mới bắt đầu thu Harness. Khóa privacy microphone phần cứng vẫn được ưu tiên.

Harness ON dùng thu giọng thủ công bằng tap, không tự nghe môi trường. Tap khi TTS đang nói chỉ ngắt phát âm thanh. Ngoài trường hợp đó, tap đầu bắt đầu thu; beep sẵn sàng chỉ phát sau khi recorder/STT đã sẵn sàng. Tap tiếp đóng capture và gửi một transcript STT đã chốt qua route OS hiện có tới agent Harness đang focus. Im lặng không tự gửi. Đạt `MAX_SESSION_DURATION_S` (`HAL_MAX_SESSION_DURATION_S`, mặc định 30 giây) thì hủy, không dispatch. Khi rảnh, mode không ghi lời nói xung quanh. Đổi mode, generation hoặc focus và privacy/stop đều loại bỏ capture; vuốt chuyển focus hủy capture trước khi đổi focus. Sleep và khóa privacy microphone phần cứng vẫn có ưu tiên.

Action mode/focus dùng worker hiện có và API Go loopback; không tự retry HTTP. Kết quả dùng phrase đa ngôn ngữ trong `hal/i18n.py`, tôn trọng speaker mute và quyền LED sleep/privacy/TTS. Chuyển focus cần capability Harness `focus.step` đã thương lượng; CLI cũ trả lỗi rõ ràng, không chuyển transport. Phần CLI tương ứng đang chờ; chưa kiểm chứng tương thích trên thiết bị đã cài.

Khi HAL khởi động, đồng bộ vị trí privacy-switch không giả lập nhấn nút: vị trí cho phép mic khôi phục quyền mic/ngoại vi mà không đánh thức thiết bị, mở conversation focus, phát chime/câu đang nghe hoặc lên lịch LED listening. Thao tác gạt thật từ mute sang unmute vẫn giữ wake/focus và chime xác nhận ngắn, không phát cue listening bằng lời. Khởi động ở vị trí mute vẫn áp hardware privacy lock đồng bộ.

Khi sleep được khôi phục sau HAL restart (kể cả software update), privacy-switch đang mở không được unmute mic đang ngủ hoặc khởi chạy voice pipeline. Mic và speaker bị mute bởi sleep giữ nguyên cho đến khi wake thật. Nếu privacy đã lưu trạng thái speaker mute do sleep, wake gỡ mute tạm thời đó bên dưới privacy lock; âm thanh vẫn bị chặn cho đến khi mở privacy. Trạng thái speaker sau khi gỡ mute được lưu để HAL restart tiếp không khôi phục mute do sleep đã kết thúc. Speaker do người dùng mute trước sleep vẫn giữ mute.

Callback GPIO có mức chân sau debounce trùng vị trí đã biết (kể cả callback ban đầu lúc startup) giữ nguyên software mute và sleep. Chỉ thay đổi mức chân thực sự mới chạy action của switch.

Khi wake mở lại mic bị mute bởi sleep, HAL cũng xóa cờ LED mic mute đã khôi phục. Callback kết thúc emotion, TTS hoặc nhạc chạy sau đó không được bật lại màu đỏ privacy khi mic đã mở. Mic vẫn bị hardware privacy khóa thì giữ cờ LED mute.

Lệnh mute speaker thủ công trong lúc sleep chuyển quyền giữ mute từ sleep sang người dùng và được lưu ngay cả khi loa đã im lặng. Wake phải giữ lựa chọn này, kể cả khi privacy đang khóa.

Âm báo thu giọng Harness dùng hai nốt đi lên khi bắt đầu và hai nốt đi xuống khi kết thúc, riêng biệt với ping gesture thường. Âm kết thúc báo đã đóng thu giọng, không phải xác nhận agent từ xa đã nhận hoặc làm xong task. Tap ngắt TTS giữ tiếng ping xác nhận cũ và không mở thu giọng.

Khi Harness mode duy trì ON, watcher mode MPR121 giữ LED thở lime nhẹ từ `button_led.harness_on` trong preset thiết bị. OFF nháy nhẹ một lần theo `harness_off`. Đèn báo nhường sleep, riêng tư và phản hồi voice/nhạc, trở lại qua luồng restore LED, không thay đổi cài đặt đèn người dùng đã lưu. Thiết bị không có RGB bỏ qua phản hồi LED.
