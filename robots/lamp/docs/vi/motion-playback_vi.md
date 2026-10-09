# Phát chuyển động (Motion Playback)

Recording servo trong `hal/recordings/` được biến thành chuyển động như thế nào.
Source: `hal/drivers/motors/animation_service.py`, còn luật tính nhịp nằm ở
`hal/drivers/motors/recording_timing.py`.

## Nhịp phát

Recording trong `hal/recordings/` là bản ghi teleop ở **20 Hz** (cột `timestamp`, cách nhau 0.05s). Vòng lặp phát trong `hal/drivers/motors/animation_service.py` bước đúng **1 frame mỗi tick** ở `self.fps` (30, `HAL_SERVO_FPS`), nên frame thô sẽ phát sai tốc độ. Vì vậy `_load_recording` resample mọi recording về đúng lưới 1/fps ngay lúc load, còn vòng phát giữ nguyên là một bước duyệt frame đơn giản — không có logic thời gian trong hot path.

Resample làm hai việc:

- **Tôn trọng `timestamp` gốc**, để recording dài đúng bằng lúc ghi. Trước đây bước frame 20 Hz ở nhịp 30 Hz làm mọi animation phát nhanh gấp 1.5×.
- **Giãn những đoạn vượt trần tốc độ** — lấy giá trị nhỏ hơn giữa `SERVO_MAX_DPS` (250 deg/s, `HAL_SERVO_MAX_DPS`; đặt 0 để tắt) và `motion.max_speed` mà body khai báo. STS3215 chỉ đạt tối đa khoảng 270 deg/s, trong khi nhiều recording được ghi vượt xa mức đó; trên Lamp thì mức khai báo 120 deg/s mới là mức có hiệu lực, và nó giãn `laugh` (+20%), `playful`, `headshake`, `acknowledge`. Không recording `music_*` nào bị ảnh hưởng nên các đoạn groove giữ nguyên nhịp.

Ngưỡng này không phải để làm đẹp. Đo trên lamp-0c89 bằng cách đọc `Present_Position` trong lúc phát `greeting`: khi lệnh đòi 554 deg/s, servo bị bỏ lại **55° so với mục tiêu** — nó bão hòa, lết, rồi giật, và đó chính là tiếng ồn nghe được. Việc giãn rất chọn lọc, chỉ chạm vào những đoạn bất khả thi:

| recording | gốc | tốc độ đòi hỏi | sau resample | giãn |
|---|---|---|---|---|
| `greeting` | 2.95s | 554 deg/s | 3.49s | +18% |
| `happy_wiggle` | 7.95s | 476 deg/s | 8.27s | +4% |
| `nod` | 1.95s | 302 deg/s | 2.00s | +2.6% |
| `idle` | 9.95s | 115 deg/s | 9.97s | không |

`idle` vốn đã nằm trong ngưỡng nên không bị giãn chút nào — nhưng nó lại hưởng lợi nhiều nhất, vì vòng lặp không còn tụt xuống 5 Hz khi idle đã settle. Mức giảm đó bước frame lưới fps chậm gấp sáu lần và biến nhịp thở thành năm cú giật thấy rõ mỗi giây; nó tốn về độ mượt nhiều hơn phần CPU tiết kiệm được.

Luật này chạy cả khi không có phần cứng. `MockMotionService` (`HAL_BOARD=sim`, thân máy chạy trên laptop qua `make sim`) import `resample_recording` từ đúng module đó và phát trên cùng lưới 30 Hz, nên một recording tốn đúng bằng thời gian thực như trên thân máy thật. `move_to`/`aim`/`nudge` của nó nội suy theo `duration` được lệnh và chặn cho tới khi tới nơi, y như driver dùng SDK. Thứ simulator vẫn KHÔNG mô hình hoá: quán tính, va chạm, torque thật, và calibration EEPROM riêng của từng con servo.

Hai hệ quả cần biết:

- Thanh ghi chuyển động của servo giữ nguyên default (`Acceleration=254`, `Goal_Velocity=0`). Hướng kẹp tốc độ ngay tại servo thay vì tại trajectory đã được đo và loại bỏ: nó giảm jerk ~33% nhưng đẩy sai số bám từ 55° lên 71°, và chính độ lết đó làm cánh tay chúi vào kẹt cơ khí. Phần mềm biết trước toàn bộ trajectory nên giãn được có kiểm soát; servo thì chỉ biết lết.
- `add_recording()` (đường upload) **xoá cache** chứ không nạp vào. Nó nhận frame đã bị bỏ `timestamp`, nên nạp vào sẽ bỏ qua resample — bản upload phát ở frame rate thô trong khi cùng file đó đọc từ đĩa lại phát đúng.

## Ổn định

Tốc độ không phải cách duy nhất một recording làm hại thân máy. Từng khớp có thể
nằm trong dải của nó mà *tổ hợp* lại đẩy trọng tâm ra ngoài đế — đo trên lamp-0c89
ngày 4/9/2026, một CSV của bên thứ ba với mọi khớp đều trong dải đã làm lật con
máy nằm nghiêng ([#271]).

Không giới hạn per-joint nào diễn đạt được chuyện này, nên `recording_stability.py`
dựng lại trọng tâm toàn thân cho từng frame và từ chối recording nào vươn xa khỏi
trục đế quá mức thân máy cho phép. Kiểm tra chạy bên trong `resample_recording`, nên
thân máy thật và simulator từ chối cùng một clip.

Không có gì trong phần kiểm tra này là riêng của lamp. Nó cần hai **khai báo theo
từng thân máy**, thiếu một trong hai là gate nằm im:

| thứ gì | ở đâu | lamp |
|---|---|---|
| ngưỡng | `SAFETY.md` `motion.max_cog_offset_mm` | 22 mm |
| hình học | `ROBOT.md` `urdf_ref` → URDF trong thư mục device | `urdf/lamp.urdf` |

Presence-driven như mọi bound khác trong `robots/contract/SAFETY-SPEC.md`: thân máy
không khai gì thì không bị giới hạn, còn thân máy khai ngưỡng nhưng không có
`urdf_ref` dùng được sẽ ghi log rằng không chấm được tư thế rồi cho qua, chứ không
fail-closed. Reachy Mini hiện không khai cái nào nên không bị ảnh hưởng.

Ngưỡng này được kẹp giữa hai mốc đo được, không phải chọn bừa:

| | trọng tâm lệch khỏi trục đế (đỉnh) |
|---|---|
| clip đã làm lật đèn | 31,6 mm |
| `confused.csv` — recording nặng nhất đang ship | 17,7 mm |
| 28 recording còn lại | ≤ 17,6 mm |

Cả 29 recording đang ship đều qua với khoảng dư thoải mái. Thứ quyết định là tay
vươn ra trước: `base_yaw` xoay cánh tay quanh đúng cái trục dùng để đo nên không
đóng góp gì, còn `elbow_pitch` ảnh hưởng mạnh nhất (frame tệ nhất của clip làm lật
có tay gập ra trước ở `elbow_pitch` 53,8° trong khi `base_pitch` mới 6,9° — các giá
trị cực trị của từng khớp trong clip đó không bao giờ xảy ra cùng một frame).

Mọi lần nạp đều được ghi log: `INFO` kèm mức đỉnh và frame xảy ra, `WARNING` khi
clip qua được nhưng đã vượt 85% ngưỡng, và `ERROR` kèm nguyên tư thế vi phạm khi bị
từ chối — một lần từ chối phải giải thích được chỉ bằng journal. Recording bị từ
chối sẽ bị đường nạp thông thường bỏ qua, nên hỏng ở mức "thiếu một animation",
không phải crash.

Frame nào có khớp không nằm trong URDF của thân máy sẽ bị **bỏ qua và ghi log rõ là
bỏ qua** chứ không chấm điểm: mọi tên khớp lạ sẽ được đọc thành 0° và trả về một con
số dễ chịu cho tư thế chưa từng được đánh giá — một lần "pass" giả còn tệ hơn là
không kiểm tra.

Cho thân máy khác dùng gate này chỉ tốn hai khai báo, không phải code: ship URDF của
nó và tự suy ra ngưỡng từ thư viện animation của chính nó (clip rộng nhất cộng dư
địa). Đừng chép lại 22 mm — đó là milimét của hình học và khối lượng *của thân máy
này*, không phải hằng số chung.

`urdf/lamp.urdf` chỉ có động học và khối lượng; repo này không ship mesh nên phần
visual và collision đã được bỏ. Nó theo gói device profile lên máy qua
`make upload-device lamp`. Khối lượng từng link là số
ước lượng và các gốc inertial trong URDF đều bằng 0, nên khối lượng mỗi link được
đặt ngay tại gốc của nó: con số milimét tuyệt đối là xấp xỉ, còn thứ hạng giữa các
clip thì không. Phải tính lại hằng số này nếu phân bố khối lượng của thân máy thay đổi.

[#271]: https://github.com/autonomous-ai/autonomous-os/issues/271

## Dừng khi chạm và cắt torque khi quá tải

Playback cũng là nơi lộ ra việc tay đèn bị chặn: quỹ đạo vẫn tiến trong khi một khớp
không theo kịp, và servo đẩy hết mức. Vì vậy `AnimationService` lấy mẫu `Present_Load`
của mọi khớp ở 20 Hz (`hal/drivers/motors/overload.py`).

- **Dừng khi chạm.** `base_yaw` ở 55 %, `wrist_roll` ở 75 % hoặc `wrist_pitch` ở 65 %
  trong 0.05 s làm recording dừng tại chỗ, ghim tư thế với torque bật (tay không rơi),
  phát tiếng "uh-oh" nhẹ đi xuống (G5 rồi E5, `play_contact_chime`) và chặn lệnh ghi
  goal trong 3 s. Sau đó chạy tiếp vào idle từ tư thế
  đã dừng. Trên lamp-52e6 các khớp này đạt đỉnh 52 % khi chuyển động tự do, và lấy tay
  chặn `base_yaw` đẩy nó lên 94-100 %. `base_pitch` và `elbow_pitch` gánh trọng lượng
  tay đèn và lên 83-100 % khi chuyển động tự do, nên không được theo dõi; thay vào đó
  chúng (cùng `base_yaw`) bị giới hạn ở 60 % torque (`torque_limit`): trên lamp-52e6
  trần 70 % làm recording tệ nhất (`shock`) trễ từ 17° lên 18°, 50 % lên 24°; 60 % cho
  lực đẩy nhẹ hơn.
- **Envelope đã học.** Trong playback mọi khớp, kể cả các khớp gánh trọng lượng, bị giữ
  ở mức load mà một lượt chạy tự do đạt ở frame đó cộng 12 % (`contact.profile_margin`),
  và ở mức trễ so với goal tại đó cộng 3,5° (`contact.lag_margin`), nên vẫn thấy được
  tay chặn một khớp đã chạm trần torque. Envelope được học riêng từng máy bằng
  `POST /servo/contact/learn` (mỗi recording chạy tự do hai lần, giữ giá trị tối đa từng
  frame, lưu vào `/var/lib/hal/contact_profile.json`). Chỉ số frame được tra là frame
  playback, nên envelope bám theo recording sau khi resample theo giới hạn tốc độ; học
  lại sau khi hiệu chuẩn, đổi `torque_limit` hoặc có recording mới. Ngoài playback
  (gaze, tracking, `/servo/move`, giữ tư thế) dùng sàn `contact.off_playback`:
  `base_pitch` 57 %, `elbow_pitch` 52 %, lag 9-14° tuỳ khớp.
- **Cắt torque khi quá tải.** Khớp nào ở mức 80 % trở lên trong 1.5 s (tay đang giữ vẫn
  bị ép) sẽ cắt torque mọi servo, phát ack chime và chặn mọi lệnh ghi goal trong 120 s. Sau đó
  thân máy resume vào idle từ vị trí tay đang nằm. Với trần 60 %, thực tế cut-off chỉ
  còn trip được trên `wrist_roll` và `wrist_pitch`.

Frame recording gửi trong lúc goal bị chặn sẽ bị bỏ, không xếp hàng lại. Ngưỡng
(`contact.load`/`hold_s`/`pause_s`, `load`, `hold_s`, `retry_s`, `torque_limit`) do
device quản lý trong `robots/lamp/servo_overload.json`; hành vi đầy đủ nằm ở phần
fail-safe của [`docs/vi/safety_vi.md`](../../../../docs/vi/safety_vi.md).

## Demo tầm chuyển động — tính ra, không thu sẵn

`POST /servo/demo` (`hal/drivers/motors/range_demo.py`) trình diễn một vòng dạo
qua tầm chuyển động kèm lời thoại, mỗi lần một khớp: đế quay tới cả hai giới hạn
yaw rồi về giữa, đầu tự xoay, cổ tay ngẩng lên cúi xuống, khuỷu vươn ra, đế
nghiêng một chút, rồi về chỗ cũ. Không phát hiện gì và không báo cáo gì — chuyển
động cùng lời nói chính là toàn bộ sản phẩm.

Đây là chuyển động duy nhất trong tài liệu này cố ý **không** phải một bản thu,
và lý do nằm ở chính lỗi mà nó thay thế. Khi được yêu cầu trình diễn tầm hoạt
động, trước đây đèn trả lời bằng emotion `scan`: `hal/recordings/scanning.csv`,
360 frame trong 17.95 s, `base_yaw.pos` trải từ **−31.3…+23.1 — tức 54.4° trên
một tầm 270°** — mà lại thuyết minh là *"Tôi sẽ quét trọn tầm của mình… xoay một
vòng đây!"*. Một bản thu không thể đúng theo cấu trúc được. Nó là chuyển động tay
của ai đó bị đóng băng tại thời điểm thu, không có gì trong file nói nó với tới
đâu, và nó sai một cách âm thầm. Waypoint đọc từ `C.YAW_MIN` / `C.YAW_MAX` thì
đúng theo cấu trúc, vẫn đúng khi giới hạn thay đổi, và port sang hằng số của một
robot khác mà không tốn gì.

**Mỗi lần một khớp, và về giữa trước khi sang khớp kế.** `waypoints()` trả về
14 chặng trong năm nhóm, mọi độ vươn đều tính từ tư thế lúc demo bắt đầu
(`seed_pose`) và được kẹp trong tầm của khớp trừ `PITCH_MARGIN` (2°). Mỗi nhóm
kết thúc bằng một chặng im lặng quay về giá trị seed, nên đế đã hướng ra trước
mặt trước khi đầu bắt đầu xoay — bản đầu tiên nối thẳng yaw sang pitch từ tận
cuối vòng quét, và trên thiết bị, cảnh đầu ngẩng trong khi thân vẫn còn quay vào
tường trông như hai chuyển động không liên quan chứ không phải một vòng dạo.

| Khớp | Chặng | Độ vươn | Pool |
|------|-------|---------|------|
| `base_yaw` | −135 → +135 → seed | `YAW_MIN`..`YAW_MAX`, giới hạn thật | `demo_left`, `demo_right`, `demo_centre` |
| `wrist_roll` | seed −45 → +45 → seed | `ROLL_REACH_DEG` | `demo_head`, rồi im lặng |
| `wrist_pitch` | seed −25 (ngẩng) → +25 → seed | `PITCH_LOOK_DEG` | `demo_up`, `demo_down`, im lặng |
| `elbow_pitch` | seed +10 (ngẩng) → −10 → seed | `ELBOW_REACH_DEG` | `demo_neck`, rồi im lặng |
| `base_pitch` | seed −12 → seed | `BASE_PITCH_LEAN_DEG`, một chiều | `demo_lean`, rồi im lặng |

Chỉ yaw chạy tới giới hạn khai báo; các khớp còn lại đi theo tầm đã đo trên
thiết bị hoặc cố ý nhỏ. `WRIST_PITCH_MIN` / `WRIST_PITCH_MAX` khai báo ±90° còn
cánh tay thì không có chừng đó — trên lamp-ac82, `wrist_pitch` lên tới −89.55
khi ngẩng (bị chặn bởi soft limit chứ không phải bởi khớp) và chỉ tới −16.61 khi
cúi — nên cổ tay chỉ ngẩng 25°, đúng bằng độ lệch mà vòng nhìn của pha quét vẫn
đi mỗi lần chạy. Khuỷu giữ ở ±10 vì khuỷu +35.8 cùng đế hạ xuống +10.6 từng vươn
cánh tay ra sau xa tới mức trông như sắp lật; demo không bao giờ di chuyển hai
khớp đó cùng lúc. `base_pitch` chỉ nghiêng một chiều: tư thế nghỉ đã ở ~29.8 sát
trần tầm 30. Các câu thoại bám theo sự phân đôi đó — pool của yaw nói *"hết cỡ
bên trái"*, các pool còn lại chỉ nói *"lên như vầy nè"* hay *"nghiêng một chút nè"*.

**Đế được tăng tốc rồi trả lại.** `DEMO_YAW_SPEED` (1600 ≈ 100°/s) được ghi vào
`base_yaw` cho màn trình diễn, y như pha quét vẫn làm và cùng một lý do: nếu
không đụng tới, khớp này chỉ chạy ~14°/s, nên một chặng 135° mất ~9 s và câu nói
mô tả nó kết thúc trong khi đèn vẫn còn đang xoay. Mỗi chặng được gửi bằng một
`move_and_hold` dài `LEG_DURATION_S` (1.0 s), kèm một nhịp dừng `DWELL_S` (0.3 s)
khi servo đã đứng yên để giới hạn đọc như một tư thế chứ không phải một cú nảy.
Được khôi phục trong `finally`,
vì một mức giới hạn bị bỏ quên sẽ theo demo đi ra ngoài và bóp chậm cả idle lẫn
mọi emotion.

**Lời nói do HAL định thời, còn câu chữ thuộc về os-server.** Mỗi chặng có thoại gọi
`aim._say(pool)` → `POST /api/sensing/filler` → các pool `demo_*` trong
`system/lib/i18n/fillers.go`. Không cần lượt LLM nào và không tốn token. Một
marker `[HW:...]` không làm được việc này: marker bắn trước TTS, nên một demo
thuyết minh bằng marker sẽ mô tả màn trình diễn đã xong từ đời nào. Các chặng im
lặng (nửa sau của một cặp, chặng về giữa) bỏ qua hẳn endpoint filler. Câu nói đi
TRƯỚC chặng của nó (nói, rồi mới di chuyển) còn chặng kế tiếp thì chờ
`_wait_until_still` — `move_and_hold` trả về khi đã gửi xong frame chứ không phải
khi servo đã tới nơi, nên không có bước chờ đó thì kịch bản vượt mặt thân máy chỉ
sau hai chặng.

**Các cổng chặn.** Từ chối khi thiết bị đang ngủ và khi motion service đang bị
suppress; từ chối một demo thứ hai chồng lên demo đang chạy. `start()` trả về
ngay và màn trình diễn chạy trên thread riêng — agent đi tới đây qua marker
`[HW:/servo/demo:{}]`, và chỉ với yêu cầu nói rõ về CHUYỂN ĐỘNG (*"cho mình xem
bạn xoay được tới đâu"*, *"show me your range of motion"*): một câu *"show me
what you can do"* trống không là câu hỏi chung về khả năng, và skill dặn model
không khởi động demo cho câu đó. `fireHWCall` chỉ cho một POST phần cứng năm giây trong
khi demo dài ~25 s. Nút vật lý abort nó cùng với pha ngắm và pha quét
(`button_actions._stop_active_tracking`): một cú nhấn chỉ dừng cánh tay mà không
dừng lời thoại sẽ để lại một cái đèn đang mô tả những chặng nó không còn thực
hiện nữa. Một demo bị abort sẽ quay về đúng tư thế lúc bắt đầu.
