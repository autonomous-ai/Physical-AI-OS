# Safety Engine (Bộ máy An toàn)

Safety engine thực thi các **giới hạn (bounds)** trong `SAFETY.md` của thiết bị một
cách **tất định, ngay trong runtime**, *dưới* tầng agent. Đây là cơ chế hiện thực
nguyên tắc số một trong `robots/contract/SAFETY-SPEC.md`: *an toàn nằm dưới bộ não.* Agent
yêu cầu hành động; engine quyết định — trên **mọi** yêu cầu, bất kể ai phát ra —
phần cứng có được phép thực thi không và trong giới hạn nào.

> **Trạng thái:** Slice 1 (trần độ sáng) đã **được hiện thực và thực thi** —
> `hal/safety/policy.py` + gate LED trong `rgb_service.py`, nạp từ
> `robots/lamp/SAFETY.md`. Các slice sau tái dùng cùng loader + gate. Mỗi dòng bảng
> dưới được đánh dấu đã-thực-thi / dự-trữ. Tài liệu này bám theo code, không ngược lại.

## Vì sao cần engine, không phải nhắc (prompt) agent

Định tuyến an toàn qua mô hình ngôn ngữ là không đáng tin — nó có thể bị thuyết phục
bỏ qua việc từ chối, có thể bịa ra giới hạn, và **không** đảm bảo được một hành động
đã *không* xảy ra. Guard mode trong codebase này đã từng được dựng lại để gửi cảnh báo
một cách tất định cũng vì lý do đó. Safety engine tổng quát hóa điều này: **runtime**,
không phải gateway, là điểm duy nhất clamp/chặn/dừng.

## Kiến trúc

Ba tầng, soi gương tầng thiết bị (`ROBOT.md` → capability → route → driver):

```
SAFETY.md front matter        các giới hạn đã khai (contract máy đọc; theo capability group)
        │  resolve qua ROBOT.md safety_ref (path hoặc http), parse lúc boot
        ▼
hal/safety/policy.py        SafetyPolicy thuần + gate functions (không IO, unit-testable)
        │  clamp_brightness(requested) -> min(requested, ceiling)   [slice 1]
        ▼
HAL capability routes          gọi gate TRƯỚC khi actuate (led, sau này servo/music)
        │  tất định, in-process, agent không bypass được
        ▼
phần cứng
```

- **`SAFETY.md` front matter** — các bound, keyed theo capability group. Schema + bảng
  field: `robots/contract/SAFETY-SPEC.md`.
- **`hal/safety/policy.py`** — loader thuần (parse front-matter bằng regex,
  dependency-free, cùng kỷ luật với `hal/board/device.py`) tạo ra `SafetyPolicy`
  có kiểu, kèm các gate function thuần. Không phần cứng, không tác dụng phụ về đồng hồ,
  unit-testable hoàn toàn off-hardware.
- **Các route** — mỗi capability route hỏi gate tại điểm actuate. Route LED clamp độ
  sáng; sau này route servo clamp speed/accel và đảm bảo `stop`. Vì gate là một lời gọi
  hàm nằm trên đường yêu cầu, không có đường nào vòng qua nó.

Policy được nạp một lần lúc boot (cùng `DeviceProfile`) và lộ ra qua endpoint thiết bị
để các bound khai báo quan sát được: `GET /device` đã trả `safety_ref`; các bound
resolve cũng hiện ở đó.

## Ngữ nghĩa fail-safe

Theo mức tới hạn của từng capability (quy tắc đầy đủ ở `robots/contract/SAFETY-SPEC.md`):

| Capability | Bound vắng / không nạp được | Lý do |
|------------|------------------------------|-------|
| light, audio, motion | pass-through (chỉ log) | enforcement theo sự hiện diện và đồng nhất: bound có khai thì thực thi, vắng thì thôi — engine không bao giờ bịa ra giới hạn không ai khai |

`SAFETY.md` là tùy chọn. Tag `schema` được validate như của `ROBOT.md` — `schema`
thiếu hoặc major lạ sẽ abort boot thay vì thực thi một ABI không đọc được.

Không có kill-switch riêng: vì enforcement theo sự hiện diện, **xóa một section (hoặc cả
front matter) là cách tắt enforcement đó**. Muốn chạy device với motion không giới hạn
lúc bring-up thì đừng khai `motion:` bounds — đó chính là trạng thái *tắt*. Một bound
*sai định dạng* (có nhưng ngoài khoảng, vd `max_speed: 0`) vẫn fail loud; chỉ *vắng mặt*
mới là pass-through.

## Lộ trình slice

| Slice | Phạm vi | Gate | Thực thi ở đâu | Trạng thái |
|-------|---------|------|----------------|-----------|
| 1 | trần `light.max_brightness` | `clamp_brightness` / `clamp_color` | gate LED (`rgb_service` `_handle_solid`/`_handle_paint`) | **đã thực thi (v1)** |
| 2 | `quiet_hours` (light + audio) | `active_max_brightness` (theo giờ) + `audio_quiet_now` | gate LED + route music | **đã thực thi (v1)** |
| 3 | `motion.max_speed` (theo sự hiện diện) | `min_move_duration` | route servo | **đã thực thi (v1)** (`max_accel` dự trữ) |
| 3b | `motion.stop_always` | — | — | **được khai báo / đảm bảo theo cấu trúc** — chưa có route-level gate nào dùng field này (xem bên dưới) |
| 4 | trạng thái fail-safe (mất mạng/gateway → dừng tracking; lỗi board → cô lập `503`; `thermal.max_temp_c` → health event quá nhiệt SoC + dừng tracking; setup + quá dòng servo dự trữ) | hook WS-disconnect + `503` theo từng capability + monitor nhiệt (`thermal_over`/`read_soc_temp_c`) | `services` khi gateway WS disconnect + route HAL/`/health` + `server.py` `_thermal_monitor` | **thực thi một phần (v1)** (setup + quá dòng dự trữ) |
| 5 | trần `audio.max_volume` | `clamp_volume` | route `/audio/volume`, phía trên chỗ rẽ nhánh loa/sink BT | **thực thi (v1)** |
| 6 | `motion.max_cog_offset_mm` (theo sự hiện diện) | chấm trọng tâm toàn thân theo từng frame (`recording_stability.py`) | lúc load recording (`resample_recording`), cả driver thật lẫn mock | **thực thi (v1)** |

Mỗi slice thêm field vào `SafetyPolicy` và gate function rồi nối một/nhiều route;
loader và contract front-matter **không** đổi hình dạng giữa các slice (chỉ thêm field
— ABI `autonomous.safety.v1`).

## Kiểm chứng việc thực thi (verify enforcement)

Một bound chỉ thực sự tồn tại nếu bạn *chứng minh* được nó giữ vững **và** agent không
lách qua được. Mỗi slice được kiểm ở ba mức; bound chưa "xong" cho tới khi cả ba pass.
(Khác với `robots/lamp/docs/security-test.md` — cái đó về security mạng/kiểm soát truy
cập: port, RCE, CORS — không phải giới hạn actuation.)

1. **Unit (gate thuần, off-hardware).** Gate là hàm thuần nên giới hạn của nó là một
   bảng test: yêu cầu trên trần thì clamp về trần, yêu cầu dưới trần thì giữ nguyên,
   bound vắng thì hành xử theo quy tắc fail-safe. Chạy trong CI không cần thiết bị.
2. **Runtime (trên thiết bị, qua route thật).** Phát yêu cầu actuate qua HTTP và quan
   sát giá trị *thực tế đẩy ra phần cứng*, không phải giá trị đã xin. Bound khai báo
   cũng quan sát được ở `GET /device`, nên test khẳng định *request vs trần vs output
   quan sát* khớp nhau.
3. **Bypass audit (kiểm tra tới hạn của an toàn).** Xác nhận **không** có đường nào tới
   actuator né được gate — phát cùng hành động qua **mọi** route có thể điều khiển nó
   (đường agent, route trực tiếp, mọi endpoint raw/cấp thấp) và xác nhận mỗi đường đều
   bị clamp. Bound thực thi trên một đường nhưng chạm được qua đường khác = chưa thực thi.

### Slice 1 — trần độ sáng (checklist)

- [x] **Unit:** `clamp_brightness(255)` với `max_brightness: 180` → `180`;
      `clamp_brightness(120)` → `120`; không khai → giữ nguyên (pass-through).
      `clamp_color` scale giữ hue (trắng→180,180,180; đỏ→180,0,0). Schema
      thiếu/sai/major-lạ fail-loud. (`hal/test/test_safety.py`, 21 test.)
- [x] **Runtime:** đã verify trên Lamp thật — `GET /device` trả
      `"safety": {"light": {"max_brightness": 180}}`; `POST /led/solid` trắng đầy (255)
      đọc lại `[180,180,180]`, `[100,50,0]` giữ nguyên, `[255,0,0]` → `[180,0,0]`.
- [x] **Bypass audit:** gate nằm ở `rgb_service` `_handle_solid` / `_handle_paint` —
      chokepoint duy nhất mọi lệnh ghi pixel đi qua. Mọi caller (route LED, effects,
      app_state, scene, gpio_button, presence, smooth_animation, main) đều dùng
      `dispatch(RGB_CMD_*)` → 2 handler đó; chỉ còn `_driver` ghi trực tiếp bên trong
      chúng (sau clamp) + `clear()` (màu đen). Grep xác nhận không đường nào né.
- [x] **Tất định:** `clamp_color` thuần, không hỏi caller — clamp giống hệt dù từ agent, Web UI hay `curl` thô —
      gate không quan tâm ai xin.

### Slice 2 — quiet hours (checklist)

Quiet hours thêm chiều **thời gian**: trong khung giờ trong ngày, trần LED giảm và
nhạc bị chặn (giờ wall-clock local; máy chạy cả ngày nên đây là giờ thực, không
phải "tắt ban đêm"). Gate đọc đồng hồ mỗi request → đổi ngay ở ranh giới, không cần
restart.

Đồng hồ đọc qua `hal.clock.device_now()` — resolve **timezone hiện tại** của máy từ
`/etc/timezone` (zoneinfo) mỗi lần gọi. Nhờ vậy khi user đổi múi giờ lúc đang chạy,
quiet hours bám zone mới **ngay**, không cần restart HAL. (`datetime.now()` thường sẽ
giữ timezone glibc cache lúc process khởi động.) Thiếu `/etc/timezone` (dev/macOS) thì
fallback về giờ local naive.

- [x] **Unit (clock inject):** `in_window` xử lý wrap nửa đêm (22:00→07:00 đúng lúc
      23:00 và 06:00, sai lúc 12:00, loại trừ 07:00); `active_max_brightness` trả trần
      giảm (40) trong khung, base (180) ngoài khung; `clamp_color((255,255,255),
      now=23:00)` → `(40,40,40)`, `now=12:00` → `(180,180,180)`; `audio_quiet_now`
      true trong khung, false ngoài / khi không có policy. (`hal/test/test_safety.py`.)
- [x] **Runtime:** `GET /device` báo `safety.light.quiet_hours` +
      `safety.audio.quiet_hours`. Đặt khung trùng giờ hiện tại → vòng LED kẹp xuống
      trần giảm và `POST /audio/play` trả `{"status":"suppressed"}`; ngoài khung thì
      bình thường.
- [x] **Bypass audit:** trần quiet LED đi chung chokepoint `rgb_service`
      `_handle_solid`/`_handle_paint` như slice 1 (không thêm đường). Chặn nhạc nằm ở
      `/audio/play` — route duy nhất khởi động audio tùy ý (TTS cố ý miễn trừ).
- [x] **Tất định:** kiểm tra khung giờ thuần khi truyền `now`; chỗ duy nhất không
      thuần là đọc đồng hồ hệ thống, cô lập trong `policy._now()` để test inject giờ.

### Slice 3 — motion (checklist)

Motion **theo sự hiện diện**, cùng quy tắc với light/audio: bound có khai thì thực
thi, vắng thì pass-through (device không khai `motion:` bounds sẽ chạy không giới
hạn — đó là trạng thái *tắt*, không phải từ chối). `max_speed` enforce bằng **kéo
dài duration** (move vẫn tới target, chỉ chậm lại) — không cắt cụt đích. Recovery
(`release`/`zero`/`hold`/`stop`) không bao giờ bị gate để luôn safe được body.
`stop_always` được parse và trả về như một phần policy đã khai báo, nhưng hiện chưa
có HAL route nào dùng field này: tính chất recovery là cấu trúc (các route không bị
gate), không phải policy gate được thực thi độc lập. `POST /servo/stop` chính là
route mà tính chất đó tồn tại vì nó — cắt ngang move hoặc recording đang chạy rồi
*giữ nguyên vị trí* (torque vẫn ON), không đọc bound nào, không nhận tham số nào.
Nó KHÔNG phải `/servo/release` — cái đó đi về tư thế nghỉ rồi mới tắt torque; một
lệnh dừng mà lại di chuyển trước là sai với mọi thân có bánh hoặc có chân (#201).

Vòng lặp vision-tracking cũng đã được gate, nhưng cần cơ chế riêng:
`min_move_duration` không bound được nó vì không có đích nào để kéo dài duration,
chỉ có speed profile theo từng frame. `cap_speed_dps` kẹp trần pursuit/saccade của
chính vòng lặp (55 / 100 deg/s, `hal/drivers/tracking/constants.py`) xuống
`motion.max_speed` đã khai báo, ngay tại chỗ duy nhất profile được chọn
(`tracker_service.set_profile`). Hai hằng số đó là tuning, không phải giấy phép —
trên Lamp (`max_speed: 120`) hôm nay không có gì bị kẹp; gate này dành cho thân nào
khai trần thấp hơn.

Recorded animation được gate lúc load chứ không phải từng frame. Recording được
resample lên đúng lưới fps của vòng phát, và đoạn nào đòi hơn trần thì bị *giãn
theo thời gian* — cùng kiểu suy giảm mà gate ở route dùng, áp theo từng đoạn nên
recording chỉ chậm lại đúng chỗ bất khả thi, không chỗ nào khác
(`hal/drivers/motors/recording_timing.py`, dùng chung cho driver thật và mock để
simulator phát đúng thứ body phát). Trần là giá trị nhỏ hơn giữa giới hạn đo được
của servo (`SERVO_MAX_DPS`, 250 deg/s) và `motion.max_speed` đã khai.

Recorded animation còn được gate về **độ ổn định** chứ không chỉ tốc độ, tại đúng
chokepoint lúc load. Mỗi joint có thể nằm trong dải của chính nó trong khi *tổ hợp*
lại đẩy trọng tâm ra ngoài đế, và không bound theo từng joint nào diễn đạt được
điều đó, nên `hal/drivers/motors/recording_stability.py` dựng lại trọng tâm toàn
thân cho từng frame và **TỪ CHỐI** recording nào vươn xa trục đế hơn mức
`motion.max_cog_offset_mm` cho phép. Khác với tốc độ, không có cách suy giảm nào
làm một tư thế sắp lật trở nên an toàn — recording bị bỏ qua, nên chế độ hỏng là
thiếu một animation, không phải crash. Hình học lấy từ chính URDF của thân qua khoá
`urdf_ref` trong `ROBOT.md`; kiểm tra này theo sự hiện diện và bất hoạt nếu thiếu
một trong hai khai báo, còn thân nào khai trần nhưng không có `urdf_ref` dùng được
thì ghi log là không chấm được tư thế rồi pass-through chứ không fail-closed. Lamp
khai 22 mm (đo được: clip bên thứ ba làm lật lamp-0c89 đạt đỉnh 31.6 mm, recording
tệ nhất trong bộ ship là 17.7 mm, 28 cái còn lại ≤ 17.6 mm — xem
`robots/lamp/docs/motion-playback.md`); chưa thân nào khác khai, nên không có gì
khác đổi hành vi. Mỗi thân phải tự suy ra con số từ thư viện animation của chính
nó — đó là milimet theo hình học và khối lượng của thân đó, không phải hằng số để
chép lại.

Recorded move trên đường SDK của Reachy được gate theo kiểu khác, vì driver
KHÔNG sở hữu vòng phát: `ReachyMotionService` giao trajectory cho
`mini.play_move()` rồi daemon Pollen stream nó. Không có frame nào để làm chậm,
nên move được **quét trước khi phát và bị TỪ CHỐI** nếu tốc độ xoay đầu đỉnh
vượt trần đã khai (`peak_head_dps`, `_move_refused`). Tốc độ đo bằng góc quay
thật giữa hai hướng đầu trên cửa sổ ≥20 ms — sai phân từng trục euler bị vọt ở
điểm gimbal, còn lấy từng cặp frame thô thì biến nhiễu timestamp thành tốc độ
mà đầu không bao giờ đạt. Chuyển động **tịnh tiến** của đầu không bị bound:
`max_speed` tính bằng deg/s còn `head_x/y/z` là milimet, và chưa body nào khai
giới hạn tịnh tiến — không enforce thì không tuyên bố là có.

Từ chối áp cho MỌI nguồn, kể cả thư viện chính chủ của Pollen, và đó là chủ ý:
một move chính chủ rớt gate là bằng chứng con số đã khai bị sai, đặc cách theo
nguồn sẽ che mất tín hiệu đó. `robots/reachy-mini/SAFETY.md` ghi rõ trần đó suy
ra từ đâu và thứ gì vẫn cần robot thật mới xác nhận được.

Đường Wi-Fi của Stack-chan giữ nguyên gate theo `duration` xuyên qua ranh giới
process và mạng. `StackChanMotionService` gửi duration đã được kéo dài dưới dạng
`duration_ms`, gia hạn một controller lease ngắn trong lúc firmware nội suy theo
thời gian, rồi kết thúc lease bằng cách giữ tại vị trí đo được. Lệnh nhả torque
ưu tiên hơn move đang chạy, đưa đầu về tư thế nghỉ cúi xuống thận trọng, rồi mới
xác nhận tư thế đó từ phản hồi vị trí đo được trước khi tắt torque. Nếu không tới
được tư thế nghỉ, driver halt-hold và giữ torque bật. Handshake bắt buộc
firmware khai `motion.timed_move`, đọc vị trí thật, halt-and-hold và torque
release. Nếu HAL, process hoặc Wi-Fi biến mất, firmware ESP32 hết hạn lease rồi
dừng và giữ tại vị trí đo được; còn khi WebSocket bị đóng, firmware đang cài sẽ
nhả torque và khởi động lại, vì vậy HAL giữ kết nối mở khi commissioning không
đạt target; firmware chỉ có tham số spring
speed chưa hiệu chuẩn sẽ bị từ chối vì không thể giữ đúng trần độ/giây đã khai.
Đường này đã có test protocol phía host; fault injection trên Wi-Fi và phần cứng
thật vẫn là bước qualification trên thiết bị.

Driver cũng triển khai contract đếm số owner `MotionService.acquire_body()` /
`release_body()` dùng cho look, capture và tracking. Khi các owner chồng lấn,
`_tracking_active` giữ giá trị true cho tới khi owner cuối cùng thoát; xoá cờ
tracking-session riêng không thể nhả quyền của owner khác. Ownership chỉ điều
phối gaze và emotion trong HAL, tách biệt với controller lease của firmware.
Nó không ngăn owner điều khiển chuyển động và không chặn đường halt luôn sẵn có.

Transport Stack-chan kiểm tra lại mọi chuyển động có thời lượng dựa trên vị trí
đo được và `motion.max_speed`, bao gồm về zero và gravity-rest. Khi release thay
thế chuyển động đang chạy, driver halt-hold trước khi đo tư thế bắt đầu. Nếu
không đọc được vị trí hoặc thời lượng an toàn vượt 60 giây, driver báo lỗi và
không gửi chuyển động. Gateway chỉ công bố kết nối sau khi gửi `hello.accepted`,
để lệnh chuyển động không vượt trước handshake.

[Repo tích hợp Stack-chan](https://github.com/glifocat/stackchan-autonomous/tree/9a5209596b97c257b6b2c1f6ff6bec91c44f8112)
cung cấp firmware patch và HTTP bridge độc lập. Demo Docker không khởi động
`hal.server`, nên không phải test startup cho driver này. HAL đầy đủ vẫn cần
profile khai báo `motion.driver: stackchan` và board của host. Firmware đã đối
chiếu trả `completed` với state `scheduled` khi cài lịch chuyển động, chưa có
nghĩa thân máy đã đến đích; `motion.halt` xóa lease, nên host acquire lại trước
chuyển động tiếp theo. Việc khớp source không chứng minh qualification phần
cứng hoặc xác định firmware thực tế đã flash trên thiết bị.

Mọi chuyển động Stack-chan xác nhận vị trí đo được đã tới đích trước khi nhả
controller lease. Trễ lập lịch firmware có thể kéo dài nội suy quá thời lượng
yêu cầu, nên chỉ thời gian trên host không chứng minh chuyển động hoàn tất.
HAL gia hạn lease trong lúc kiểm tra tới đích (sai số tối đa 1 độ), dùng khoảng
chờ ổn định 2 giây trước khi halt và báo lỗi thay vì báo thành công. Từng request
protocol vẫn chịu command timeout đã cấu hình.

Commissioning home thuộc công cụ thử nghiệm riêng của Stack-chan, không thuộc
contract motion chung của OS. Entrypoint chuẩn `hal.server:app` không có endpoint
`/stackchan/*` hoặc `/servo/home*`. Schema HTTP và route thử nghiệm nằm trong
`robots/_experimental/stackchan/commissioning.py`; driver Stack-chan thực thi
frame tọa độ và giới hạn commissioning riêng. Route servo, model và contract
`MotionService` dùng chung được giữ nguyên.

Entrypoint thử nghiệm cung cấp `GET /stackchan/home` để discovery capability thụ
động và `GET /stackchan/home/position` để đọc pan/tilt đo được trong
`calibrated_home_deg_v1`; cả hai không acquire motion lease. Chuyển động mặc định
bị tắt (`STACKCHAN_HOME_COMMISSIONING_ENABLED=0`). Khi được bật rõ ràng,
`POST /stackchan/home/move` yêu cầu capability firmware `motion.home_degrees.v1`,
frame tọa độ tường minh, chỉ target tilt từ 7 đến 10 độ và duration yêu cầu từ
2 đến 10 giây. Safety policy có thể kéo dài duration tới 60 giây. Feedback ban
đầu phải có pan trong ±30 độ và tilt trong [0, 5). Torque yaw tắt trong chuyển
động chỉ có pitch này. Dưới 5 độ, stop hoặc mất transport có thể khiến torque
tắt hoặc session lỗi; vẫn cần hỗ trợ cơ khí và giám sát trực tiếp.

Trong mỗi chuyển động, kể cả lần lặp lại tùy chọn, driver đọc feedback sau khoảng
chờ tối đa 250 ms hoặc nửa TTL của lease, lấy giá trị ngắn hơn. Độ trễ request
cộng thêm vào khoảng này; command timeout đã cấu hình vẫn áp dụng. Feedback lỗi
hoặc yaw lệch quá 1 độ sẽ kích hoạt halt mà không đợi hết duration. Giám sát theo
mẫu không thể phát hiện mọi sai lệch giữa các lần đọc. Khi không tới đích sau
settling, driver halt và trả các mẫu đo settling nhưng giữ kết nối. Lỗi firmware
cũng giữ kết nối; command timeout vẫn đóng kết nối, và firmware đang dùng sẽ
nhả torque rồi reboot khi mất kết nối. Không thể bảo đảm giữ vị trí thành công
cho mọi lỗi, nhất là dưới ngưỡng giữ của firmware.

Thành công yêu cầu pitch đo được trong sai số 1 độ so với target, ít nhất 6 độ
và đã tiến dương ít nhất 1 độ. Nếu đầu dừng thiếu nhưng ổn định (các mẫu cuối
trong 0,2 độ, đã tiến ít nhất 1 độ, đạt ít nhất 6 độ, thiếu hơn 1 nhưng không
quá 3 độ), driver lặp lại đúng target tối đa một lần. Phản hồi ghi `recommands`.
Chỉ khi đo được đã tới đích, `lease.release` mới cấp torque lại cho cả hai trục;
sau đó driver đọc lại vị trí.

Dùng `POST /stackchan/stop` và `POST /stackchan/release` của công cụ để chẩn đoán.
Lỗi firmware trả 502 với `op`, `code`, `message`; các lỗi nhả lực khác được báo,
gồm không tới đích và bị hủy, trả 502 với `message`, `errors`. Endpoint nhả lực
thử nghiệm không báo thành công khi thao tác thất bại. `/servo/stop` và
`/servo/release` chung giữ hành vi OS hiện có; chỉ phản hồi thành công của
endpoint release cũ chưa chứng minh torque đã được nhả. Công cụ này không xác
minh calibration hoặc qualification phần cứng để sử dụng thường xuyên.

Driver Stack-chan vẫn ở giai đoạn thử nghiệm. Trước khi qualification thiết bị:

- Xác định và pin bản firmware tương thích có các motion capability bắt buộc;
  driver HAL này không bổ sung các capability đó vào firmware stock.
- Kiểm tra WSS có xác thực và xác minh certificate trên ESP32 thật.
- Xác nhận chuyển động có giới hạn hoàn tất, vị trí đo được và giới hạn duration
  trên cụm pan/tilt thật.
- Thử halt, mất Wi-Fi, mất process HAL và hết hạn lease khi đang chuyển động,
  xác minh hành vi giữ vị trí và phục hồi trên phần cứng.
- Hiệu chỉnh tư thế gravity-rest và xác nhận thực tế rằng torque chỉ được nhả
  sau khi đã đo được tư thế đó.

Test host và loopback, cũng như test face, LED hoặc speaker riêng, không hoàn tất
các bước qualification chuyển động này.

Chọn driver bằng `driver: stackchan` trong capability `motion` của device
profile. Đặt `STACKCHAN_DEVICE_ID`, một `STACKCHAN_BODY_TOKEN` riêng dài ít nhất
32 ký tự, cùng `STACKCHAN_BODY_TLS_CERT` và `STACKCHAN_BODY_TLS_KEY`.
`STACKCHAN_BODY_HOST` (mặc định `0.0.0.0`) và `STACKCHAN_BODY_PORT` (mặc định
`8765`) cấu hình listener; `STACKCHAN_BODY_COMMAND_TIMEOUT` (mặc định `3.0` giây)
và `STACKCHAN_BODY_LEASE_TTL_MS` (mặc định `1500`, chấp nhận `250..5000`) cấu
hình thời gian phát hiện lỗi. TLS là bắt buộc, trừ khi
`STACKCHAN_BODY_ALLOW_INSECURE_WS=1` bật WebSocket không mã hoá một cách tường
minh cho mạng phát triển cô lập. Không dùng tuỳ chọn này trong production.

### Interface learned-policy (dry run)

`POST /policy/run` hiện chỉ là interface cho learned controller như ACT hoặc
SmolVLA. Endpoint validate và ghi log `policy` cùng `task`, trả về run record
`dry_run`, và **không** import runtime inference, không lấy observation, cũng
không gửi motor target. Vì chưa có target nên đây không phải đường vòng qua
safety gate. Khi có execution thật, mọi target phải đi qua motion gate và
`POST /servo/stop` phải huỷ policy worker trước rồi mới giữ nguyên body. Route
`POST /policy/stop` xoá dry-run request mà không đụng motor; trên body có mount
`/servo/stop`, route dừng phần cứng hiện có cũng gọi cancellation như vậy trước
rồi mới giữ nguyên body.

- [x] **Unit:** `min_move_duration` kéo dài move quá nhanh (120° @120 deg/s → 1.0s),
      giữ move chậm, kẹp request tức thì (duration 0), pass-through khi không có
      `max_speed` và khi không có policy nào, bỏ qua joint không biết vị trí đầu;
      comment `# stop_always` không bị parse; `max_speed:0` fail-loud.
      (`hal/test/test_safety.py`.)
- [ ] **Runtime (CHƯA verify trên máy — bạn deploy):** `GET /device` báo
      `safety.motion`; `/servo/move` duration nhỏ → trả `duration` đã kéo dài, move
      ở tốc độ kẹp; device không có `motion:` bounds → chạy không giới hạn (không 403).
- [x] **Bypass audit (route):** cap tốc độ áp ở `/servo/move` qua `min_move_duration`
      (endpoint duy nhất nhận duration). LƯU Ý: animation nội bộ (idle/emotion do
      runtime, không phải agent) chưa gate.
- [x] **Tất định:** `min_move_duration` thuần (không clock, không xét caller).

### Slice 4 — trạng thái fail-safe (checklist)

Fail-safe **theo trạng thái** chứ không clamp từng request: khi thiết bị mất một phụ
thuộc tới hạn, nó rơi về tư thế an toàn một cách tất định, *dưới* tầng agent. Ba điều
kiện đã thực thi; setup-incomplete và over-current servo còn dự trữ.

- [x] **Mất mạng / gateway → dừng tracking do agent điều khiển.** Khi gateway
      WebSocket disconnect, `runtimes/openclaw/service_ws.go` gọi
      `hal.StopServoTracking()` (`system/lib/hal`) → HAL `POST /servo/track/stop`,
      để body thôi đuổi theo một target không còn cập nhật vision mới. Best-effort và
      được gác bởi `SetUpCompleted`. Lưu ý then chốt: thiết bị **không** đóng băng hay
      "giữ pose" — animation idle local vẫn tiếp tục (nó local và vô hại) và các reflex
      phục hồi (`motion.stop`/release, mute, sleep, wake) luôn sẵn sàng; chỉ tracking và
      motion mới *do agent điều khiển* mới dừng.
- [x] **Lỗi board / driver → cô lập `503`.** Đã có sẵn: capability bị lỗi trả `503`
      theo từng route trong khi phần còn lại vẫn phục vụ, lộ ra qua `/health`. Không cơ
      chế mới — contract fail-safe tái dùng cô lập đã có.
- [x] **Nhiệt (quá nhiệt SoC) → health event + dừng tracking.** Theo sự hiện diện trên
      `thermal.max_temp_c`: một monitor nền (`server.py` `_thermal_monitor`) đọc nhiệt độ
      SoC từ `/sys/class/thermal` mỗi ~10s qua `policy.read_soc_temp_c`; gate hysteresis
      thuần `policy.thermal_over` trip tại `max_temp_c` và clear tại `resume_temp_c`
      (mặc định `max−10`). Khi trip nó set `state.thermal_over`, log, và dừng tracking tùy
      ý; idle vẫn sống. Lộ ra ở `GET /health.thermal`. Ngưỡng đặc thù theo SoC — đọc
      critical trip riêng của board, không đoán chung chung.
- [ ] **Setup chưa xong → dự trữ.** Chưa gate trong runtime (chỉ reflex setup/identity
      là ý định đã khai, chưa thực thi).
- [ ] **Quá dòng (servo) → dự trữ.** Không có cảm biến dòng servo trên phần cứng; dự trữ
      cho phần cứng/telemetry lộ ra được nó.
- [x] **Unit:** `thermal_over` trip tại/trên `max_temp_c`, giữ qua hysteresis khi còn
      trên `resume_temp_c`, clear tại/dưới nó, và là False khi không có policy / không có
      section thermal / nhiệt không đọc được; `read_soc_temp_c` parse millidegrees → °C và
      trả None khi đọc lỗi; parse mặc định `resume = max − 10` và fail-loud khi
      `max_temp_c ≤ 0` hoặc `resume ≥ max`. (`hal/test/test_safety.py`.)
- [ ] **Runtime (CHƯA verify trên máy):** đường WS-disconnect → `/servo/track/stop` và
      monitor nhiệt mới ở mức repo; chưa xác nhận trên thiết bị sống (ngắt link gateway;
      đốt nóng SoC vượt `max_temp_c`).

### Slice 5 — `audio.max_volume` (checklist)

Trần âm lượng áp dụng cả ngày, độc lập với `audio.quiet_hours`: khung giờ chỉ chặn
output *tuỳ ý* (nhạc), còn trần giới hạn *mọi thứ to tới đâu*, kể cả câu trả lời nói.

- [x] **Gate:** `policy.clamp_volume` — thuần tuý, không đọc đồng hồ, không phân biệt
      caller. Không khai = pass-through (chỉ còn kẹp thang 0–100), đúng luật
      presence-driven như `clamp_brightness`.
- [x] **Thực thi ở đâu:** `POST /audio/volume` (`hal/routes/audio.py`), kẹp **phía trên**
      chỗ rẽ nhánh sink nên trần có hiệu lực như nhau với loa tích hợp (amixer/DAC dB) và
      tai nghe Bluetooth. Giá trị lưu lại (`config/.volume`, os-server khôi phục lúc boot)
      là giá trị đã kẹp, nên mức cao đặt từ trước không sống sót qua lần reboot.
- [x] **Rà đường vòng (caller):** mọi đường đặt volume đều chui qua đúng route này —
      agent/skill, `system/intent` (`rules_audio.go` "volume up"/"quieter"), slider web
      Monitor, và bước khôi phục lúc boot của os-server (`config_watch.go` →
      `hal.SetVolume`). Gate nằm trong route chứ không nằm ở caller, nên không caller nào
      là thứ quyết định.
- [x] **Báo ra ngoài:** cả `GET` lẫn `POST /audio/volume` đều trả `max_volume` (null khi
      không khai); riêng reply của `POST` còn mang giá trị thực đã áp, nên caller không phải
      đoán request có vào nguyên vẹn hay không và UI tự sửa ngay thay vì trôi tới vòng poll
      kế tiếp.
      `system/lib/hal.MaxVolume()` và slider Monitor đọc trường này — chỉ mang tính tham
      khảo, client bỏ qua vẫn không vượt được trần, chỉ mất khả năng chia bước theo dải
      thật. Slider giới hạn track đúng bằng trần thay vì để tay kéo vào vùng chết rồi bị
      bật ngược, còn `rules_audio.go` diễn đạt "louder"/"quieter" theo tỉ lệ của dải dùng
      được (nếu không, trên máy có trần 40 thì "quieter" cố định 30 gần như không khác
      "louder").
- [x] **Unit:** kẹp trên/dưới trần, pass-through khi không có bound và không có policy,
      kẹp thang 0–100, `max_volume` lồng trong `quiet_hours` không bị đọc thành trần cả
      ngày, và fail-loud khi ngoài dải. (`hal/test/test_safety.py`.)
- [ ] **Runtime (CHƯA verify trên thiết bị):** mới ở mức repo — chưa xác nhận trên loa
      thật (đặt 100 qua API rồi đọc lại trần; kiểm giá trị amixer/sink BT).

## Quan hệ với enforcement hardcode rời rạc hiện có

Một số hành vi an toàn đã tồn tại, hardcode và rải rác: `motion.stop()` trong service
motors/animation, clamp vị trí cơ học của lerobot, config scale độ sáng LED. Clamp
vị trí cơ học đó vẫn là giới hạn vị trí theo từng joint duy nhất: đường lệnh **không**
có bound vị trí nào được khai báo (recording được gate theo tốc độ và, từ slice 6,
theo độ ổn định toàn thân — cả hai đều không phải clamp theo từng joint). Engine
**không** gỡ hết một lúc — nó *tập trung hóa* chúng vào policy khai báo từng slice một,
để các bound trở thành **dữ liệu thiết bị khai** thay vì hằng số chôn trong driver.
Slice 1 giới thiệu engine bằng một bound **chưa** từng được thực thi (trần độ sáng độc
lập với agent), chứng minh đường đi đầu-cuối trước khi di trú các mảnh đã có.
