# Cảm biến môi trường — component thay thế được

HAL cung cấp sensor môi trường qua một capability `environment` tùy chọn,
với driver, vòng đời và HTTP snapshot riêng của từng component,
cùng tầng với camera và audio. Tích hợp gồm thu nhận HAL, snapshot chỉ đọc qua
web/MQTT và OS phát hiện thay đổi môi trường kéo dài theo cấu hình để gửi agent.
SEN55 không đo chạm, lực, CO₂, CO hay O₂. Component SCD41 tùy chọn thêm
`co2_ppm` đo thật vào cùng capability; không công bố nhiệt độ/độ ẩm đọc trên
bus của SCD41. SEN63C cung cấp PM, nhiệt độ, độ ẩm và CO₂ đo thật trong
một component; không có chỉ số VOC/NOx. Không component được hỗ trợ nào đo
CO hay O₂. Software dùng key chỉ số và nguồn đo, không phụ thuộc tên sensor.

## Phạm vi hiện tại: số đo và sự kiện thay đổi kéo dài

HAL giữ snapshot gần nhất trong RAM. Worker môi trường của OS đọc snapshot
độc lập và gửi sự kiện `environment.update` đủ điều kiện qua
`POST /api/sensing/event`; không gọi agent cho từng mẫu cảm biến.
Skill `environment` diễn giải số đo, tham khảo `wellbeing` để đưa gợi ý phù hợp.
Web và MQTT vẫn chỉ đọc, không kích hoạt lượt agent.
Chưa có kho lịch sử môi trường, dịch vụ hẹn kiểm tra lại, tự điều khiển actuator
hay cảnh báo y tế. Chỉ hardware profile `pro`, `pro-respeaker-lite` và `pro-xvf3800` của Lamp khai
báo capability `environment` tùy chọn và bật SEN63C trên OrangePi
`orangepi_sun60`, bus `0`. Standard tắt cả hai; SEN55/SCD41 và board thiếu
entry tương ứng vẫn tắt trong cả bốn profile. Worker yêu cầu capability được khai báo.

## Đấu dây và lắp đặt

Xác định chiều connector theo [datasheet SEN5x của Sensirion, mục 4
và 6](https://sensirion.com/media/documents/6791EFA0/62A1F68F/Sensirion_Datasheet_Environmental_Node_SEN5x.pdf),
không dựa vào màu dây. Đầu cáp tương thích là JST GHR-06V-S.

| Chân SEN55 | Tín hiệu | Kết nối |
|---|---|---|
| 1 | VDD | Nguồn 5 V |
| 2 | GND | GND chung với host |
| 3 | SDA | Dữ liệu I2C của host |
| 4 | SCL | Clock I2C của host |
| 5 | SEL | Nối GND trước khi cấp nguồn |
| 6 | NC | Để trống |

Ghi nhận từ bên hardware do chủ device cung cấp: **header phía OrangePi** dùng
**chân vật lý 3 là SDA**, **chân vật lý 5 là SCL**. Đây là vị trí chân header
host, không phải số chân connector SEN55: nối OrangePi pin 3 với SEN55 pin 3
(SDA), OrangePi pin 5 với SEN55 pin 4 (SCL). Trên OrangePi 4 Pro đã kiểm tra,
`gpio readall` xác định chân 3 là SDA.0 (PB3), chân 5 là SCL.0 (PB2).
Device tree đang chạy ánh xạ các chân này tới `/dev/i2c-0`, overlay `i2c0`
đã bật. Vì vậy `sen55.json` ghi `bus: 0`; thu nhận dữ liệu vẫn tắt.

`sen55.json` ghi vị trí chân vật lý phía host bằng `sda_pin: 3` và `scl_pin: 5`.
Đây là metadata dây nối; driver dùng `bus`, không tự cấu hình pin-mux GPIO
từ hai trường này.

SDA/SCL hỗ trợ logic 3,3 V. Dùng điện trở kéo lên 3,3 V với host 3,3 V;
5 V cấp nguồn cảm biến, không cấp cho GPIO host. Địa chỉ I2C là `0x69`,
tốc độ bus tối đa 100 kHz. Cần xác định board thực tế, sơ đồ chân header,
pin multiplexing, bus khả dụng và khả năng cấp nguồn trước khi đấu dây.
HAL không chọn chân header; SEN55 cần cấu hình tốc độ bus ở cấp board
(phần dưới mô tả xử lý Sunxi riêng cho SEN63C). Khi lắp, giữ thông thoáng
cửa hút/xả khí và tránh nhiệt từ host.

Kiểm tra device ngày 2026-09-11 đã xác nhận mapping bus phía host, nhưng
lệnh đọc tên SEN55 tại `0x69` không nhận ACK địa chỉ (driver Sunxi trả
`EINVAL`). Clock bus thực tế là 400 kHz, vượt giới hạn SEN55. Chưa xác nhận
được danh tính hay số đo sensor. Cần cấu hình bus tối đa 100 kHz và kiểm tra
nguồn, GND chung, SEL, dây nối trước khi bật thu nhận. Lần kiểm tra này
không thay đổi cấu hình trên device.

## Bật trong HAL

`ROBOT.md` gốc của Lamp giữ `environment` ở dạng comment; `sen63c.json` gốc
giữ `orangepi_sun60` tắt trên bus `0`. Vì vậy Standard không thu nhận số đo
môi trường, không ghi clock bus cho SEN63C, không đủ capability để chọn skill
`environment`; UI không polling sensor.

Các file `profile.json` trong `overrides/pro/`, `overrides/pro-respeaker-lite/`
và `overrides/pro-xvf3800/` đặt
`capabilities: {"environment": true}`. Renderer chung bật khai báo capability
sẵn có (driver `composite`, `routes: [environment]`, `required: false`) và
copy `device/sen63c.json` của override ra gốc package. HAL sau đó mount API
và nạp các component đã bật. Thiếu SEN63C trên một trong ba profile Pro thì
component báo `error` và thử lại; sensor tùy chọn không chặn khởi động.

HAL duyệt driver component đã đăng ký và đọc JSON riêng theo device/board:
`sen55.json`, `scd41.json`, `sen63c.json`. Cờ `enabled` điều khiển từng
component; entry tắt hoặc thiếu không truy cập hardware. Không có danh sách
chọn component riêng; file `environment.json` cũ bị bỏ qua. Mỗi component bật
có worker độc lập.

SEN63C là component mặc định trên OrangePi trong cả ba profile Pro. Để dùng SEN55 + SCD41 thay thế,
trước hết đặt SEN63C thành `"enabled": false`, rồi bật các entry thay thế với
dây nối đã xác nhận và restart HAL.
Hai component bật không được cùng sở hữu một chỉ số: SEN55 + SEN63C hoặc
SCD41 + SEN63C bị báo lỗi cấu hình thay vì âm thầm ghi đè dữ liệu. Component
tắt không tham gia kiểm tra trùng này. Thêm hardware sau này cần driver,
đăng ký các chỉ số cung cấp và config board; API environment và flow software
vẫn dùng chung.
Đăng ký tại `hal/drivers/environment/registry.py`: loader config, driver
(`start`, `read`, `close`), timing mặc định và các key chỉ số hỗ trợ.
Setup và `build-orangepi` giải nén toàn bộ archive profile device, nên file
JSON sensor mới không cần nhánh cài đặt riêng theo loại sensor.
Khi nâng cấp từ bản cũ, cần cập nhật cả gói HAL (gồm sửa clock) lẫn gói
profile device. Setup, build image và OTA áp override phần cứng đã chọn lên
package gốc mới giải nén. Cập nhật device thay profile và restart HAL cùng os-server.

`/etc/autonomous/hardware-profile` thiếu, rỗng hoặc `standard` chọn Standard,
kể cả máy từng được bật SEN63C bằng mặc định chung trước đây. Không tự chuyển
máy sang Pro. Với phần cứng Pro, chọn rõ `pro`, `pro-respeaker-lite` hoặc `pro-xvf3800` rồi cài lại
gói device; chỉ cập nhật HAL không đổi capability hay JSON sensor. Xem
[override phần cứng](../../../../docs/vi/bootstrap-ota.md#override-phần-cứng-tùy-chọn).

Cấu hình SEN55 thuộc device tại `robots/<device>/sen55.json`, dùng map `boards`
như `mpr121.json`. Board mục tiêu là OrangePi (`orangepi_sun60`); Lamp có entry tắt
(`{"enabled": false}`) cho board này với bus header đã xác nhận là `0`. Thiếu file hoặc
entry của board đang chọn thì cảm biến tắt. Entry tắt có thể bỏ `bus` hoặc để `null` khi chưa biết bus.
Khi bật, `bus` phải là số nguyên không âm.
Cấu hình sai, kể cả trường không được hỗ trợ, bị từ chối khi khởi động.

Để bật SEN55 sau khi xác nhận dây nối, trước hết tắt SEN63C để tránh trùng
chỉ số, sửa entry SEN55 của board thực tế rồi restart HAL. Mẫu sau có placeholder,
chưa phải JSON có thể nạp trực tiếp:

```text
{
  "boards": {
    "<actual board ID>": {"enabled": true, "bus": <actual bus number>}
  }
}
```

Thay board ID bằng board được nhận diện và placeholder bus bằng số bus Linux
thực tế, là số nguyên không âm. Không có bus mặc định. Bật interface I2C của
kernel, cấu hình pin multiplexing và tốc độ bus theo board đó; tích hợp này
đã xác nhận OrangePi 4 Pro dùng bus `0` nhưng vẫn cần kiểm chứng giao tiếp
sensor và clock bus phù hợp. HAL truy cập `/dev/i2c-N` bằng thư viện
chuẩn Python; process cần quyền mở thiết bị này. Không cần thêm package I2C
Python. Chế độ mô phỏng không bao giờ truy cập phần cứng, kể cả entry đã bật.

Với thời gian mặc định, worker SEN55 đọc mỗi 1 giây và thử lại sau 5 giây khi phần cứng lỗi. Không có dữ liệu
mới quá 30 giây sẽ kích hoạt phục hồi qua cùng luồng thử lại. Khi shutdown,
HAL dừng worker và giải phóng bus. Thu nhận dữ liệu chạy riêng với vòng sensing
camera/microphone. Privacy của camera/microphone và sleep không dừng thu nhận
dữ liệu môi trường. Capability này không thêm chính sách actuator hay giới hạn
`SAFETY.md` mới; nhiệt độ môi trường không phải nhiệt độ SoC.

## Component CO₂ SCD41

`robots/lamp/scd41.json` dùng cùng map `boards`. Entry OrangePi đang tắt,
`bus`, `sda_pin`, `scl_pin` đều `null` chờ hardware xác nhận. Khi bật cần số
bus Linux nguyên không âm; chân header là metadata tùy chọn, không cấu hình
pin-mux. Không sao chép dây hay điện áp nguồn SEN55 khi chưa xác nhận board/
breakout SCD41. SCD41 dùng I2C `0x62`, SEN55 dùng `0x69`, nên có thể chung bus
nếu hardware xác nhận dây, logic, nguồn và cấu hình bus tương thích.

Driver dùng đo định kỳ thông thường (mỗi 5 giây có kết quả mới), kiểm tra
data-ready và CRC, chỉ công bố `co2_ppm`. Mặc định `poll_interval_s: 5`,
`retry_interval_s: 5`, `stale_after_s: 15`, `no_data_timeout_s: 30`. Thay nhịp
poll HAL không thay nhịp đo nội bộ 5 giây này.

`automatic_self_calibration: null` giữ cài đặt cảm biến; `true`/`false` đặt
ASC trong RAM trước khi đo. HAL không lưu cài đặt bền vững hay chạy forced
recalibration. Xem điều kiện tiếp xúc không khí của ASC trước khi chọn giá trị;
warm-up OS 60 giây không phải hiệu chuẩn. Xem [datasheet SCD4x](https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf).
Chưa kiểm chứng dây nối hay số đo SCD41 trên hardware thật.

## Component kết hợp SEN63C

`robots/lamp/sen63c.json` dùng cùng map `boards`, tắt SEN63C cho
`orangepi_sun60` trên bus `0`. Cả ba override Pro cung cấp JSON thay thế bật
entry này. `sda_pin`, `scl_pin` vẫn là null; board khác thiếu entry tương ứng
(kể cả Raspberry Pi) vẫn tắt ngay cả với Pro. Xác nhận dây cho từng máy; ghi chú chân SEN55
ở trên không xác nhận dây SEN63C. Driver dùng I2C `0x6B`, kiểm tra product type
SEN63C, CRC từng word và đọc PM1/PM2.5/PM4/PM10, nhiệt độ, độ ẩm, `co2_ppm`
đo thật. VOC/NOx giữ null trong sample chung. CO₂ có thể chưa có trong 22–24
giây đầu dù các số đo khác đã dùng được; warm-up OS áp dụng độc lập.

Mặc định `poll_interval_s: 1`, `retry_interval_s: 5`, `stale_after_s: 5`,
`no_data_timeout_s: 30`. `automatic_self_calibration: null` giữ cài đặt sensor;
true/false tường minh cấu hình ASC CO₂. Đây là phần riêng với warm-up và ngưỡng
thay đổi của OS. Không gửi lệnh forced recalibration hay lưu bền vững. Xem
[tài liệu driver SEN63C của Sensirion](https://sensirion.github.io/python-i2c-sen63c/api.html).
Bus I2C phía host phải chạy ở **100 kHz hoặc thấp hơn**, theo
[datasheet SEN6x của Sensirion, mục 4.4](https://sensirion.com/resource/datasheet/SEN6x).
Bus OrangePi Sun60 có thể mặc định ở 400 kHz: trên device đã kiểm tra, tốc độ
này gây lỗi CRC khi đọc product type; chuyển bus 0 về 100 kHz đã khôi phục
phản hồi nhận dạng và số đo hợp lệ. Không bỏ kiểm tra CRC để nhận gói dữ liệu
hỏng. Kết quả này xác nhận bus của device đó, không xác nhận dây của các máy khác.

Trước khi mở sensor, mỗi lần khởi tạo driver SEN63C đều gọi helper clock I2C
chung cho bus đã cấu hình. Với adapter có `name` bắt đầu bằng `SUNXI TWI`, HAL
đọc `/sys/class/i2c-adapter/i2c-N/device/info` và trường `twi->freqency`
(đúng cách viết của kernel). Nếu tốc độ lớn hơn `100000` Hz, HAL hạ xuống
`100000` qua thuộc tính `device/freq` của controller, rồi đọc lại để xác nhận
không vượt `100000`. Tốc độ đã đạt yêu cầu được giữ nguyên. HAL cần quyền đọc
các thuộc tính này và ghi `freq` khi phải hạ clock. Dữ liệu controller thiếu/sai
định dạng, ghi thất bại hoặc đọc lại vẫn vượt giới hạn đều chặn truy cập sensor
và hiện trong `last_error` của component; worker khởi tạo lại driver theo cơ chế
retry hiện có để thử lại.

Bước chuẩn bị này chạy mỗi lần khởi tạo driver, gồm khi HAL khởi động lúc boot
hoặc phục hồi sau lỗi. Cài HAL mới qua setup hoặc OTA vì vậy mang theo bản sửa,
không cần cài riêng drop-in systemd. Component tắt và simulation không khởi tạo
driver phần cứng, không ghi clock. Loại adapter khác được giữ nguyên: cần cấu
hình bus tối đa 100 kHz bằng cơ chế được board/kernel hỗ trợ. Trường `bus` trong
JSON sensor chọn adapter, không đặt tốc độ. Dây nối, pin-mux, nguồn và bus đúng
vẫn cần xác nhận theo từng board. Hạ clock controller tác động mọi ngoại vi dùng
chung bus vật lý đó.

Sau khi cài HAL mới, có thể xóa workaround riêng trên device trước đây tại
`/etc/systemd/system/hal.service.d/20-sen63c-i2c.conf`; reload systemd sau khi xóa.
Restart HAL rồi kiểm tra `device/info` của controller đã chọn và
`/environment/status`: clock không vượt `100000`, sample mới và không có lỗi.
Trên OrangePi Sun60 đã thử, đường chạy tự động được kiểm chứng bằng cách gỡ
drop-in, đặt lại 400 kHz khi HAL đã dừng rồi khởi động HAL mới: driver ghi log
hạ xuống 100 kHz và đọc lại số đo hợp lệ. MPR121 trên cùng bus đọc trạng thái
thành công 100/100 lần khi SEN63C đang chạy (trung bình 0.823 ms, tối đa 5.040 ms).
Kiểm tra này xác nhận giao tiếp; chưa thử thao tác chạm vật lý và reboot toàn
board trong lần kiểm chứng đó.

Log vòng đời HAL dùng key `[sen55]`, `[scd41]` và `[sen63c]`: tắt/khởi động, mẫu hợp lệ
đầu tiên, bắt đầu đo, lỗi thử lại và dừng hiển thị ở INFO (lỗi có thể dùng
WARNING hoặc ERROR). Mỗi mẫu hợp lệ được log ở INFO với timestamp và số đo;
lần poll chưa có mẫu mới log trạng thái chờ và tuổi dữ liệu gần nhất.
`[environment]` ghi component đã đăng ký/simulation hoặc thiếu capability.

Để theo dõi log trên device, dùng file log hoặc journal systemd:

```sh
tail -F /var/log/hal/server.log | grep --line-buffered -E '\[(environment|sen55|scd41|sen63c)\]'
journalctl -u hal -f | grep --line-buffered -E '\[(environment|sen55|scd41|sen63c)\]'
```

Đây là hướng dẫn cho người vận hành; việc ghi tài liệu không thực hiện kết nối
device. Root logging mặc định INFO đã đủ để thấy các dòng này.

## HAL API

Các endpoint thuộc HAL tại port 5001, dùng cơ chế kiểm soát truy cập HAL
hiện có. Chúng khả dụng khi robot nạp route `environment`.

| Endpoint | Hành vi |
|---|---|
| `GET /environment/status` | Trả trạng thái, `last_error`, `sample` gần nhất, `age_s`, `stale` và chẩn đoán từng component, kể cả khi tắt hoặc chưa khả dụng |
| `GET /environment/sample` | Trả snapshot còn mới; HTTP 503 khi chưa khả dụng hoặc đã cũ |
| `GET /health` | Boolean `environment` cho biết có sample còn mới hay không |

Các trạng thái gồm `disabled`, `starting`, `ready`, `error`, `stopped`. Mỗi
entry trong `components` giữ state, enabled, sample, lỗi, tuổi mẫu, bus và
timing riêng. Dữ liệu component không ready hoặc hết hạn không khả dụng;
Component có thanh ghi trạng thái khác zero cũng bị loại khỏi sample tổng hợp.

Nhóm `ready` và không stale khi ít nhất một component đóng góp số đo còn mới.
`partial: true` nghĩa là component khác đang bật nhưng không khả dụng; một
component tắt không tự khiến nhóm partial. Số đo khỏe vẫn dùng được khi sensor
khác lỗi. `sample` cấp nhóm luôn là object với đủ chín key chỉ số bên dưới
và `timestamp`. Chỉ số không hỗ trợ, tắt, chưa ready hoặc stale đều là `null`.
Không có số đo dùng được thì mọi chỉ số, `sample.timestamp` và `age_s` là
`null`, `stale` là true. Sample thô từng component có thể giữ số đo cũ để
chẩn đoán; đó không phải số đo hiện tại của nhóm.

`sources` ánh xạ chỉ số thuộc component đang bật đến component đó; `metric_timestamps` giữ Unix timestamp
cho từng chỉ số dùng được. `sample.timestamp` và `age_s` cấp nhóm mô tả dữ liệu
mới nhất, không đại diện mọi chỉ số. Consumer cần kiểm tra trạng thái nguồn
và độ mới từng chỉ số. Bus/timing chỉ có thêm ở cấp cao nhất khi nhóm có một
component. `ready` không chứng nhận warm-up hay hiệu chuẩn cảm biến khí.

Một sample chứa:

| Trường | Ý nghĩa |
|---|---|
| `timestamp` | Unix time tính bằng giây, hoặc `null` khi không có số đo dùng được |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Nồng độ khối lượng bụi, đơn vị µg/m³ |
| `humidity_pct` | Độ ẩm tương đối, % |
| `temperature_c` | Nhiệt độ, °C |
| `voc_index`, `nox_index` | Chỉ số khí không có đơn vị, không phải nồng độ ppm |
| `co2_ppm` | Nồng độ CO₂ đo thật, ppm, từ component CO₂ đang bật |

`device_status` riêng của sensor nằm trong sample từng component để chẩn đoán.
Consumer dùng schema chỉ số chung và trạng thái component, không coi thanh ghi
một sensor là trạng thái chung của nhóm.

Giá trị đo chưa khả dụng được trả bằng JSON `null`; caller không được hiểu
là số không. Phần dưới mô tả phát hiện thay đổi ở OS và diễn giải của agent.

## Hiển thị trên web local

[Device → Sensing](../../../../docs/vi/web-ui_vi.md#58-device--sensing) và
card **Environment** luôn hiện, không cần debug. Đang tải hoặc thiếu capability
thì số đo hiện `N/A`, không gửi request tới sensor. Card camera vẫn yêu cầu
`vision`.

Khi có capability `environment`, trình duyệt đọc
`GET /api/hardware/environment/status` mỗi 3 giây qua proxy
hardware của OS đã có xác thực, chuyển tới HAL `GET /environment/status`.
Chu kỳ làm mới này độc lập với cấu hình nhịp đọc HAL bên dưới. Card hiển thị
trạng thái, chín trường chỉ số chung (gồm CO₂ ppm), nhãn nguồn, thời điểm sample, trạng thái dữ liệu cũ và lỗi.
Giá trị thiếu hoặc cũ hiện `N/A`; request thất bại được hiển thị rõ để không
trình bày số đo cũ như dữ liệu hiện tại. Lỗi component không che số đo còn tốt.
Bus, thanh ghi trạng thái và timing nằm trong mục kỹ thuật thu gọn theo từng
component ở `status.components`; vẫn hỗ trợ snapshot một sensor kiểu cũ. Đây là màn hình
chỉ đọc, không có ngưỡng tốt/xấu hay lưu lịch sử. Event OS → agent do worker
độc lập bên dưới tạo, không do trình duyệt làm mới.

Chỉ profile Pro khai báo capability, nên Standard hiện `N/A` mà không polling.
Trên Pro, số đo SEN63C OrangePi hiện khi còn mới; thiếu phần cứng thì hiện lỗi
và `N/A` trong khi worker thử lại. Tắt mọi component hoặc dùng board thiếu entry tương ứng sẽ
hiện trạng thái đã tắt và `N/A`. Hiển thị UI không bật thu nhận hay event agent.

## Đọc qua MQTT

Mobile/backend gửi `{"cmd":"data","kind":"environment.status","data":{}}`
trên `fa_channel`; OS trả `MQTTDataResponse` trên `fd_channel`, cùng `kind`,
`status: "success"` và snapshot HAL trong `data`. Handler yêu cầu capability
`environment` được khai báo, không yêu cầu model SEN55. HAL local có timeout
5 giây. Sensor đang tắt, lỗi hoặc dữ liệu cũ vẫn là snapshot hợp lệ: caller phải
kiểm tra `state`, `stale`, `sample`, `last_error` trong `data`.

Thiếu capability trả `status: "failure"`,
`error: "environment capability not declared"`. Lỗi kết nối HAL, HTTP khác 200
hoặc status JSON không hợp lệ cũng trả failure. Chỉ profile Pro khai báo
capability nên thiếu phần cứng trên Pro được báo trong snapshot; Standard trả
failure do thiếu capability. Đây là request/reply, không stream hay event tự
động, không gọi agent. Xem [giao thức MQTT](../../../../docs/vi/mqtt_vi.md)
để biết payload và quy tắc phản hồi.

## Cấu hình thời gian

Mỗi entry board nhận các trường thời gian tùy chọn sau (giây). Mặc định bên dưới dành cho SEN55; mặc định component khác được nêu ở trên. Giá trị phải là số dương hữu hạn; `stale_after_s` và `no_data_timeout_s` phải lớn hơn `poll_interval_s`. Khởi động lại HAL sau khi sửa. Đây là nhịp đọc của HAL, không thay đổi nhịp đo nội bộ của sensor.

```json
{
  "poll_interval_s": 1.0,
  "retry_interval_s": 5.0,
  "stale_after_s": 5.0,
  "no_data_timeout_s": 30.0
}
```

## Chính sách thay đổi của OS và API cho agent

OS cấu hình diễn giải trong object `environment` cấp cao nhất của
`config/config.json`, qua admin `GET`/`PUT /api/device/config` hiện có.
Cấu hình này tách biệt thời gian HAL trong JSON từng component. Giá trị mặc định:

```json
{
  "environment": {
    "enabled": true,
    "initial_report": true,
    "evaluate_interval_s": 10,
    "sustain_s": 60,
    "cooldown_s": 1800,
    "retry_interval_s": 60,
    "max_sample_age_s": 10,
    "metrics": {
      "pm1_0_ug_m3": {"delta": 10, "relative_delta_pct": 20, "warmup_s": 60},
      "pm2_5_ug_m3": {"delta": 10, "relative_delta_pct": 20, "warmup_s": 60, "comfort": {"above": 35, "hysteresis": 5, "sustain_s": 300}},
      "pm4_0_ug_m3": {"delta": 25, "relative_delta_pct": 25, "warmup_s": 60},
      "pm10_ug_m3": {"delta": 25, "relative_delta_pct": 25, "warmup_s": 60},
      "temperature_c": {"delta": 2, "relative_delta_pct": 0, "warmup_s": 60, "comfort": {"below": 19, "above": 27, "hysteresis": 1, "sustain_s": 300}},
      "humidity_pct": {"delta": 10, "relative_delta_pct": 0, "warmup_s": 60, "comfort": {"below": 35, "above": 65, "hysteresis": 5, "sustain_s": 300}},
      "voc_index": {"delta": 50, "relative_delta_pct": 0, "warmup_s": 3600},
      "nox_index": {"delta": 20, "relative_delta_pct": 0, "warmup_s": 21600},
      "co2_ppm": {"delta": 200, "relative_delta_pct": 20, "warmup_s": 60, "comfort": {"above": 1000, "hysteresis": 150, "sustain_s": 300}}
    }
  }
}
```

`EnvironmentMetricRule.delta` là mức sàn tuyệt đối dương, hữu hạn, theo đơn
vị số đo (độ ẩm dùng điểm phần trăm). `relative_delta_pct` hữu hạn, trong
khoảng 0–100 kể cả hai đầu. Ngưỡng thay đổi hiệu dụng là
`max(delta, abs(accepted_baseline) * relative_delta_pct / 100)`; bằng ngưỡng
vẫn đủ điều kiện. Dùng cùng ngưỡng cho tăng và giảm đến khi event được chấp
nhận cập nhật baseline của chỉ số đó. PM2.5 baseline 200 có ngưỡng 40: 220
không đủ, 240 hoặc 160 có thể đủ. CO₂ baseline 2000 có ngưỡng 400: 2200
không đủ, 2400 hoặc 1600 có thể đủ. Vẫn áp dụng mọi điều kiện readiness,
duy trì và dispatch khác.

Đây là **heuristic sản phẩm tạm thời, không phải giới hạn y tế hay chất lượng
không khí tuyệt đối**. Bỏ cả object environment hoặc cả map `metrics` thì
dùng mặc định mới ở trên. Gửi `metrics` thay toàn bộ map, cho phép chỉ theo
dõi một nhóm. Trong rule được gửi, thiếu `delta` hoặc `warmup_s` thì dùng mặc
định của chỉ số; riêng thiếu `relative_delta_pct` cố ý nghĩa là **0**, giữ
hành vi ngưỡng tuyệt đối cũ. Thiếu `comfort` trong rule được gửi nghĩa là
không có rule comfort, không kế thừa mặc định mới. Trường environment bị bỏ dùng mặc định. Từ chối
null tường minh, map rỗng, trường/chỉ số lạ và số không hợp lệ. Config mới lưu
đầy đủ object mặc định, kể cả `relative_delta_pct: 0` khi áp dụng.

Không tự ghi lại rule đã lưu: `delta: 15` cũ của bụi thô và trường tương đối
bị thiếu vẫn lần lượt là 15 và 0. Để áp dụng toàn bộ chính sách khuyến nghị,
cập nhật `os-server` trước (không cần sửa HAL), đọc config hiện tại, rồi ghép
thiết lập environment khuyến nghị với lựa chọn mong muốn của người dùng.
Chỉ gửi `{"environment": <object environment mong muốn đầy đủ>}` qua admin
`PUT /api/device/config`; không gửi lại toàn bộ kết quả GET vì có thể chứa
credential đã che. Bỏ `metrics` dùng mặc định; gửi `metrics` thay map nên cần
giữ đủ rule mong muốn trong object. Kiểm tra giá trị đã lưu bằng
`GET /api/device/config`.
Lưu bản sao config trước khi nâng cấp: `os-server` cũ từ chối trường mới
`relative_delta_pct` và `comfort` do giải mã JSON nghiêm ngặt. Trước khi rollback,
khôi phục config cũ hoặc bỏ cả hai trường mới khỏi mọi chỉ số, kể cả trường
tương đối có giá trị 0.
Chỉ upload skill không thay đổi được policy Go này.
Tài liệu này không triển khai hoặc ghi lại cấu hình thiết bị.

Chu kỳ đánh giá, duy trì, retry và tuổi mẫu tối đa phải từ 1–86400 giây;
duy trì và retry không nhỏ hơn chu kỳ đánh giá. Cooldown cho phép 0–604800
giây, warm-up 0–86400 giây. Sửa lúc chạy áp dụng ở tick worker tiếp theo
và reset baseline; không tự bật phần cứng HAL.

Chỉ số đo còn mới, enabled, ready và timestamp tiến lên mới đủ điều kiện.
Cả cờ `stale` của HAL lẫn tuổi mẫu tối đa OS áp dụng cho từng chỉ số và nguồn.
Snapshot tổng hợp dùng `sources`, `components`, `metric_timestamps` để sensor
khỏe không khiến dữ liệu cũ của sensor khác có vẻ mới. SEN55 lỗi chỉ reset
chỉ số của nó, không chặn CO₂ SCD41 còn tốt. Snapshot một sensor kiểu cũ vẫn
dùng timestamp/status cấp cao nhất. Sau khi có số đo hợp lệ liên tục hết warm-up
của từng chỉ số, giá trị đầu tiên tạo baseline phát hiện thay đổi. Mỗi component
HAL có thể trả `continuous_data_s`: thời gian từ mẫu thành công đầu tiên tới
mẫu thành công mới nhất trong đợt thu nhận liên tục. Giá trị là null khi không
hợp lệ, stale hoặc lỗi, và reset khi thu nhận bị gián đoạn. OS dùng thông tin
liên tục này cùng thời gian quan sát hợp lệ local để đáp ứng warm-up khi chỉ
OS restart; HAL cũ thiếu trường này dùng quan sát local. Vẫn kiểm tra độ mới và
tính hợp lệ từng chỉ số; chỉ biết component đã chạy lâu là chưa đủ. Chênh lệch phải đạt ngưỡng hiệu dụng cùng chiều trong `sustain_s`;
giảm dưới mức chênh lệch hoặc đảo chiều sẽ reset thời gian đang chờ.
Chỉ số null reset warm-up/baseline riêng; snapshot không khả dụng hay lỗi đọc
reset số đo của mọi chỉ số. Khoảng gián đoạn lớn cũng reset tính liên tục.
Warm-up là thời gian chờ của OS, không chứng nhận cảm biến đã hiệu chuẩn.

Mỗi chỉ số có thể thêm `comfort` gồm số `below` và/hoặc `above` tùy chọn,
cùng `hysteresis` và `sustain_s`. Mặc định khuyến nghị ở trên bật nhiệt độ
(dưới 19/trên 27, hysteresis 1), độ ẩm (dưới 35/trên 65, hysteresis 5), CO₂
đo thật (trên 1000, hysteresis 150) và PM2.5 (trên 35, hysteresis 5); tất cả
yêu cầu 300 giây. Chỉ số khác không có nhánh này. Cần ít nhất một cận hữu
hạn; có cả hai thì `below < above`. `hysteresis` phải hữu hạn, dương; với hai
cận không lớn hơn nửa khoảng cách giữa chúng. `sustain_s` trong khoảng từ
`evaluate_interval_s` đến 86400. `comfort: null` tường minh không hợp lệ;
bỏ trường này tắt nhánh trong rule được gửi. Nhánh chạy độc lập với điều
kiện delta sau kiểm tra readiness/warm-up.

Ứng viên cao cần `current > above`; thấp cần `current < below`. Bằng ngưỡng
kích hoạt không đủ điều kiện. Sau trạng thái cao được chấp nhận, phục hồi cần
`current <= above - hysteresis`; sau trạng thái thấp được chấp nhận, phục hồi
cần `current >= below + hysteresis`. Phục hồi cũng phải duy trì cùng 300 giây
với rule khuyến nghị. Cho phép chuyển kéo dài trực tiếp sang trạng thái đối
ngược. Dispatch comfort được chấp nhận chốt trạng thái mới; không thông báo
lặp khi vẫn ở trạng thái đã xác nhận đó. Dispatch bị từ chối giữ chuyển trạng
thái đang chờ. Event chỉ có delta không reset comfort. Dữ liệu thiếu, không
hợp lệ hoặc nguồn reset sẽ xóa trạng thái comfort tương ứng.

Event chỉ có comfort có thể có `changes: {}` và thêm map `comfort` theo tên
chỉ số. Mỗi entry chứa `state` (`high`, `low` hoặc `recovered`),
`previous_state` (`normal`, `high` hoặc `low`), `current`, `current_at`,
`threshold`, `sustained_s` và `source` tùy chọn. Các trường delta hiện có và
`reason: "initial"` không đổi. Bản tin ban đầu không xác nhận trạng thái
comfort, nên tình trạng kéo dài về sau có thể tạo thông báo. Hai nhánh dùng
chung cooldown 1800 giây (30 phút) cùng các điều kiện retry, busy, sleep và
conversation floor. Khoảng nghỉ này ưu tiên ít làm gián đoạn công việc; đây là
lựa chọn sản phẩm tạm thời, không phải tiêu chuẩn sức khỏe. Người dùng chủ động
hỏi trạng thái không phải chờ cooldown. Giá trị cooldown đã lưu tường minh
vẫn được giữ; đặt `cooldown_s: 1800` trong config environment hiện có để áp dụng. Bản tin khởi động được chấp nhận cũng bắt đầu cooldown: tình trạng đủ
năm phút có thể phải chờ thêm trước dispatch. Không bảo đảm phản hồi kịp thời
hay xử lý khẩn cấp sức khỏe. Skill diễn giải event comfort bằng lời thông
thường; payload số không phải kịch bản để đọc thành tiếng.

`environment.initial_report` mặc định `true`. Lời chào hệ thống không chờ sensor
hay gọi HAL: chỉ có thể đính kèm snapshot còn mới, đủ warm-up đã cache trong
worker OS bằng JSON `[environment:initial]`. Agent thêm tối đa một câu thực tế
với một hoặc hai số đo vào lời chào bình thường. Cold boot thường chào khi chưa
có dữ liệu môi trường. Sau khi lời chào hoàn tất, OS gửi snapshot đủ điều kiện
đầu tiên một lần qua `environment.update`, với `reason: "initial"` và
`changes: {}`, kể cả chưa có thay đổi đáng kể. Chỉ gửi chỉ số đủ điều kiện;
chỉ số còn warm-up không tạo thêm thông báo ban đầu riêng khi sẵn sàng muộn.
Lời chào thành công có kèm context sẽ tiêu thụ thông báo này; nếu không, nó
vẫn chờ. Dispatch bị từ chối thử lại theo `retry_interval_s`; dispatch được
nhận hoặc xếp queue tiêu thụ thông báo và bắt đầu cooldown chung. Queue là
best-effort, nên hết hạn về sau không tạo lại thông báo ban đầu. Event riêng
vẫn tuân theo capability, policy enabled, sleep, busy và conversation floor.
Trạng thái tồn tại trong tiến trình OS; sensor kết nối lại hoặc sửa config
không tạo lại thông báo đã tiêu thụ. Đặt `initial_report: false` tắt cả dữ liệu
kèm lời chào lẫn update ban đầu, vẫn giữ phát hiện thay đổi kéo dài. Snapshot
ban đầu không chứng minh xu hướng, chẩn đoán sức khỏe hay không khí an toàn.

Event thay đổi đủ điều kiện gồm `[environment:update]` rồi JSON với `observed_at`
(Unix giây), `sample`, `changes` theo tên chỉ số chứa `previous`, `current`,
`previous_at` (Unix giây của baseline), `current_at` (Unix giây của số đo hiện
tại), `source`, `delta` có dấu, và `sustained_s`. Event tổng hợp còn chứa
`sources`, `metric_timestamps`; số đo một sensor kiểu cũ có thể thiếu nguồn.
Body POST chứa event dạng chuỗi JSON trong `message`; bộ định dạng sensing
thêm `[environment:update]` khi chuyển cho agent.
`previous` là baseline ban đầu hoặc sau event
được xác nhận gần nhất của chỉ số đó; `current` là mẫu mới nhất, không phải
trung bình trượt. Cả tăng lẫn giảm đều có thể đủ điều kiện. Dispatch được xác
nhận cập nhật baseline của chỉ số đã gửi và bắt đầu cooldown chung; dispatch
bị từ chối giữ baseline và chờ ít nhất retry interval trước lần thử sau.
Thời lượng event là yêu cầu duy trì theo cấu hình, không phải số đo phơi nhiễm
y tế.

Dispatch kiểm tra capability khai báo rõ, sleep và conversation floor kể cả
khi bật guard mode. Queue lúc agent bận chỉ giữ event môi trường mới nhất,
hết hạn sau 60 giây, kiểm tra lại capability/sleep/policy enabled trước khi phát
lại. Đặt `environment.enabled: false` cũng khiến event handler trả
`dropped_disabled`; chỉ dừng diễn giải tự động, không dừng thu nhận HAL hoặc
đọc status chẩn đoán.
Xác nhận queued là best-effort: event vẫn có thể hết hạn hoặc bị bỏ sau đó;
không bảo đảm người dùng nghe thông báo. Event môi trường tự động không bắt
buộc phản ứng camera, emotion marker hay hành động vật lý.

Tool agent local đọc `GET http://127.0.0.1:5000/api/environment/status`.
Route OS chỉ cho loopback, kiểm tra capability và trả envelope chuẩn
`{"status":1,"data":{...HAL snapshot...},"message":null}`. Route đọc snapshot
chẩn đoán đã cache trong HAL, không ép phần cứng đo ngay; caller phải kiểm tra
readiness, độ mới và từng giá trị null. Thiếu capability trả HTTP 403; lỗi
đọc/định dạng HAL trả HTTP 502. Snapshot disabled/error/stale vẫn có thể là
response chẩn đoán thành công. Browser và MQTT tiếp tục dùng route xác thực
hiện có.

## Căn cứ và kiểm chứng chính sách tạm thời

Nguồn được xem lại ngày **2026-09-25**.
[Datasheet SEN6x v0.92, tháng 12/2025, §§1.2, 1.3, 1.5.2](https://sensirion.com/media/documents/FAFC548D/693FBB15/PS_DS_SEN6x.pdf)
nêu precision PM1/PM2.5 là ±(5 µg/m³ + 5% số đo) đến 100 µg/m³, sau đó
±10%; PM4/PM10 là ±25 µg/m³, sau đó ±25%. Đây là biến thiên giữa các linh
kiện, không phải nhiễu theo thời gian; đầu ra bụi thô được tính toán.
Repeatability nhiệt độ là 0,1°C, độ ẩm ±1 điểm phần trăm trong điều kiện quy
định. Accuracy CO₂ của SEN63C là ±(100 ppm + 10% số đo) trong 400–5000 ppm,
đáp ứng τ63 điển hình 60 giây; không dùng repeatability SEN66 thay thế.
Accuracy yêu cầu chạy ban đầu 12 giờ rồi tiếp xúc không khí tươi, đồng thời
hoạt động liên tục với ASC và tiếp xúc không khí tươi hàng tuần.

[Hướng dẫn kiểm tra và đánh giá SEN6x v1.0, tháng 7/2026](https://sensirion.com/media/documents/AE5564E4/6A58BFB5/PS_AN_SEN6x_Testing_And_Evaluation_Guide_D1.pdf)
phân biệt kiểm tra tích hợp với đánh giá trong phòng thí nghiệm; kết quả phụ
thuộc setup và tích hợp có thể ảnh hưởng nhiệt độ/độ ẩm.
[Hướng dẫn dùng máy đo CO₂ của HSE](https://www.hse.gov.uk/ventilation/using-co2-monitors.htm)
ủng hộ quan sát nhiều lần và chú ý vị trí đặt, không coi một số đo là kết luận
về thông gió. Không nguồn nào quy định chính sách event này.

Cần tách riêng ba quyết định:

| Khái niệm | Điều khiển gì | Căn cứ và giới hạn |
|---|---|---|
| Policy delta OS | Thay đổi kéo dài có đáng tạo event không | Heuristic sản phẩm theo baseline ở trên; không phân loại nồng độ hay sức khỏe. |
| Ngưỡng thoải mái trong phòng | Diễn đạt thân mật và hỗ trợ đồng hành kéo dài | Ngưỡng người dùng chọn trong `skills/environment/reference/room-comfort.md`, cũng dùng cho nhánh comfort tùy chọn; không phải ngưỡng WHO. |
| Hướng dẫn phơi nhiễm | Diễn giải theo kỳ lấy trung bình xác định | Cần lịch sử, độ bao phủ và kiểm chứng phù hợp; bộ phát hiện snapshot/thay đổi này chưa triển khai. |

[Hướng dẫn chất lượng không khí WHO 2021](https://www.who.int/news-room/questions-and-answers/item/who-global-air-quality-guidelines)
đưa mức PM2.5 trung bình năm/24 giờ là 5/15 µg/m³ và PM10 là 15/45 µg/m³;
khuyến nghị 24 giờ dùng phân vị 99. Đây không phải ngưỡng snapshot tức thời
hay delta.
[AQI hiện tại của AirNow](https://www.airnow.gov/aqi/aqi-basics/using-air-quality-index/)
dùng quan sát theo giờ và NowCast, không dùng một giá trị sensor đơn lẻ.
[EPA về máy đo không khí trong nhà giá thấp](https://www.epa.gov/indoor-air-quality-iaq/low-cost-air-pollution-monitors-and-indoor-air-quality)
nêu chưa có giới hạn nồng độ trong nhà được chấp nhận rộng rãi cho đa số
chất ô nhiễm, và nhà sản xuất đặt mức cảnh báo của máy đo.

[Quan điểm ASHRAE 2025 về CO₂ trong nhà](https://www.ashrae.org/file%20library/about/position%20documents/pd-on-indoor-carbon-dioxide-english.pdf)
không coi Standard 62.1 là giới hạn 1000 ppm chung hay CO₂ là đại diện cho
toàn bộ chất lượng không khí trong nhà. Hướng dẫn HSE về mức liên tục trên
1500 ppm trong phòng có người nhằm cải thiện thông gió, không phải ranh giới
an toàn.
[Khoảng độ ẩm ưu tiên 30–50% của EPA](https://www.epa.gov/indoor-air-quality-iaq/care-your-air-guide-indoor-air-quality)
là khuyến nghị điều kiện phòng, không phải delta thay đổi độ ẩm.

Thay đổi này triển khai thông báo thay đổi và tình trạng thoải mái kéo dài
có giới hạn, không phải bộ theo dõi phơi nhiễm. Chưa có lịch sử 24 giờ ở đây để xác lập vượt ngưỡng WHO hay
phơi nhiễm đạt hướng dẫn từ snapshot. Theo dõi phơi nhiễm sau này cần lịch sử
có timestamp, quy tắc độ bao phủ, lấy trung bình và kiểm chứng; chưa tuyên bố
đã hoàn tất. Không đổi nhãn ngưỡng diễn đạt do người dùng chọn thành khuyến
nghị WHO.

Mức sàn tuyệt đối, tỷ lệ phần trăm, đánh giá mỗi 10 giây, duy trì 60 giây,
cooldown 1800 giây, retry 60 giây và tuổi mẫu tối đa 10 giây đều là **heuristic
sản phẩm**, không phải thiết lập do hãng quy định hay bảo đảm loại hết nhiễu.
Warm-up không phải hiệu chuẩn. Mức sàn bụi thô lớn hơn và biên tương đối là
ứng xử tạm thời trước giới hạn đo, không phải bảo đảm đã hiệu chỉnh ngoài
thực tế. Ngưỡng lâm sàng không điều khiển bộ phát hiện này. Nhánh comfort bên trên
dùng ngưỡng diễn đạt người dùng đã chấp thuận để hỗ trợ liên tục về phòng,
không dùng giới hạn phơi nhiễm; thời gian năm phút và hysteresis là lựa chọn
sản phẩm tường minh, chưa kiểm chứng thực tế hay được nguồn trích dẫn chứng nhận.

Chỉ số bật comfort có thể thông báo tình trạng cao/thấp kéo dài dù không có
delta. Rule cũ không có `comfort` vẫn không thông báo mức đứng yên sau bản tin
ban đầu. Đây là tính năng đồng hành về sự thoải mái, không phải báo động sức
khỏe hay chứng nhận an toàn liên tục; khởi động và câu hỏi trực tiếp vẫn là
luồng riêng.

Trước khi tuyên bố đã kiểm chứng thực tế, ghi log thụ động hoạt động bình
thường trên nhiều thiết bị, phòng và ngày: giữ timestamp, trạng thái component,
độ mới từng chỉ số, config thực tế, baseline được chấp nhận, chiều thay đổi
đang chờ, ngưỡng, kết quả dispatch và thay đổi tự nhiên trong phòng. Replay
cùng log qua policy cũ và mới; so số event ứng viên/được nhận, thông báo lặp,
độ trễ và thay đổi đã chú thích bị bỏ sót. Replay offline gồm comfort cao/thấp
đứng yên, phục hồi kéo dài, chuyển thẳng sang trạng thái đối ngược, hành vi
chốt trạng thái, bằng ngưỡng, hai chiều, baseline cao, khoảng stale, restart, dispatch queued/bị từ
chối và cooldown. Xem kết quả theo từng chỉ số/thiết bị, rồi điều chỉnh có
chủ đích dựa trên bằng chứng đã ghi. Không tạo khói, aerosol hay thử bằng hơi
thở trong kế hoạch này; replay thụ động đánh giá hành vi thông báo, không
chứng minh accuracy sensor hoặc an toàn y tế.

## Skill environment và use case well-being

`skills/environment/SKILL.md` yêu cầu capability và sở hữu diễn giải dữ liệu.
Skill bị loại khi capability thiếu hoặc rỗng, kể cả khi skill cũ vẫn giữ cơ
chế khả dụng dự phòng.
Skill tham khảo mục environmental-care của `skills/wellbeing/SKILL.md`
để chọn thời điểm và lời nhắc. Khi người dùng nói khó chịu, dùng
`skills/wellbeing/reference/discomfort.md`; tái sử dụng hướng dẫn và dữ liệu
đã đọc, không chuyển lượt qua lại giữa các skill. Hỏi về phòng và hỗ trợ khi
khó chịu không cần camera, danh tính, log hoạt động hay bộ đếm uống nước.

Câu hỏi hoặc nhận xét thông thường về cảm giác trong phòng (nóng, lạnh, bí,
khô hoặc có vẻ có khói), cùng câu hỏi tiếp nối, dùng
`skills/environment/reference/room-comfort.md`: đọc status một lần hoặc dùng
snapshot hiện tại, rồi trả lời một câu ngắn, thân mật, không đọc số, đơn vị,
tên sensor hay tag cảm xúc. Khi hỏi rõ số đo, vẫn trả giá trị được yêu cầu.
Dùng số đo phù hợp còn hợp lệ để quyết định, không lặp lại lời than khi chưa
có căn cứ. Kiểm tra độ mới và trạng thái nguồn theo từng chỉ số, không chỉ
snapshot chung.

Ngưỡng diễn đạt do người dùng chọn: nhiệt độ trên 27°C là nóng, dưới 19°C là
lạnh; độ ẩm dưới 35% là khô, trên 65% là oi dính; CO₂ đo thật trên 1000 ppm
là bí/nặng; PM2.5 trên 35 µg/m³ là nhiều bụi. So sánh nghiêm ngặt: bằng ngưỡng
không kích hoạt nhãn. Đây là quy tắc diễn đạt, không phải giới hạn sức khỏe
hay ngưỡng sự kiện OS; bụi cao không chứng minh có khói. Số đo bình thường
chỉ cho phép nhận xét có phạm vi như “Trong này có vẻ không nóng,” không nói
“không khí an toàn” hoặc “do bạn thôi.” Không bịa xu hướng, nguồn gây ra hay
thiết bị sẵn có. Khi câu hỏi thông thường về phòng thiếu số đo phù hợp còn
dùng được, chỉ nói “Not sure. I can't feel the air right now.” hoặc bản tương
đương theo ngôn ngữ đang dùng: “Chưa rõ. Giờ mình không cảm nhận được không khí.”
Đây là ngoại lệ hai câu cố định, không thêm lời khuyên.

Nhận xét lúc khởi động và update tự động giữ nguyên quy tắc thời điểm, im
lặng và snapshot. Diễn giải ý nghĩa có căn cứ trước số, kèm tối đa một hành
động hữu ích. Triệu chứng cá nhân không kèm câu hỏi về phòng vẫn theo luồng
wellbeing bên dưới; khó thở hoặc khói/phơi nhiễm do người dùng báo được ưu
tiên trước việc đọc sensor và quy tắc trả lời ngắn. Số đo từng phần không
chứng nhận phòng an toàn, sạch hay xác định nguyên nhân triệu chứng.

Khi người dùng nói mệt, nhức đầu, chóng mặt, bí bách hoặc khó tập trung,
wellbeing bắt buộc đọc reference discomfort, kể cả câu không chuẩn ngữ pháp
như “I'm headache, tired, what happen?”. Không tự suy ra việc dùng màn hình,
thời lượng hay nguyên nhân triệu chứng từ lời than. Capability
thiếu/chưa biết thì không gọi công cụ môi trường; đọc lỗi, toàn null hoặc stale
thì bỏ qua gợi ý môi trường. Không nhắc lỗi sensor hay yêu cầu setup hardware
khi người dùng đang chia sẻ khó chịu. Câu hỏi/nhận xét thông thường về cảm
giác trong phòng dùng câu dự phòng khi thiếu dữ liệu ở trên. Khi có capability và không có dấu hiệu khẩn cấp, bắt buộc
tham khảo environment và đọc status một lần có timeout (hoặc dùng snapshot
hiện tại đã cung cấp) trước khi hoàn tất phản hồi. Việc thêm nhận xét phù hợp
và một gợi ý thoải mái/thông gió có điều kiện vẫn là tùy chọn. Số đo không xác định nguyên nhân triệu chứng
và không phủ nhận việc người dùng đang khó chịu.

Reference discomfort có hướng dẫn ưu tiên triệu chứng/phơi nhiễm do người dùng
báo trước việc kiểm tra sensor, kèm nguồn và tình huống minh họa. Đây là hướng
dẫn phản hồi của skill, không phải engine y tế/báo động trong OS. Hướng dẫn
CO₂ phân biệt tăng so với trước và vấn đề thông gió kéo dài; không thêm phân
loại nồng độ tự động hay ngưỡng OS. Dữ liệu thật đến sau có thể dùng so sánh,
nhưng không ngầm tạo lịch kiểm tra, log wellbeing mới hoặc quyền điều khiển
thiết bị.

- **Hỏi về phòng:** đọc status một lần hoặc dùng snapshot hiện tại, rồi trả lời
  ngắn theo quy tắc trên; thiếu số đo phù hợp còn dùng được thì chỉ nói câu dự
  phòng, không khẳng định không ô nhiễm hay an toàn.
- **Khởi động:** chào ngay; có thể thêm một câu từ số đo đã cache đủ điều kiện.
  Nếu chưa có, snapshot đầu tiên đủ điều kiện có thể tạo update riêng sau lời
  chào, không chào lần nữa hay gọi API cho bản tin này. Bỏ số đo ban đầu đã cũ.
- **Thay đổi kéo dài:** giải thích thay đổi có căn cứ, gợi ý tối đa một hành
  động hữu ích khi phù hợp. Tôn trọng yêu cầu yên tĩnh/ngủ; trả `NO_REPLY`
  nếu không có thông báo hữu ích.
- **Sau một hành động:** so với số đo trước có timestamp thật trong event/hội
  thoại. Báo tốt lên hoặc xấu đi mà không khẳng định quan hệ nhân quả. Thiếu
  baseline thì nói chưa so sánh được. Không hứa hẹn giờ kiểm tra lại, tự polling
  hay bịa kho lịch sử bền vững.

VOC/NOx là chỉ số tương đối, không phải ppm hay nhận diện hóa chất. Chỉ nói
về CO₂ khi `co2_ppm` đo thật còn mới, bất kể component; không suy ra CO₂ từ
VOC/NOx. Thiếu hai chỉ số khí trên SEN63C là bình thường, không phải lỗi sensor.
Không component được hỗ trợ nào đo O₂, CO hay tạo cảnh báo khói/cháy. PM tức thời không chứng minh đáp ứng hướng dẫn
WHO về phơi nhiễm 24 giờ hoặc cả năm. Xét điều kiện ngoài trời trước khi gợi
ý thông gió, và nhiệt vỏ máy trước khi diễn giải nhiệt độ. Skill không chẩn
đoán sức khỏe, tự bịa ngưỡng, tiếp tục việc không liên quan hay điều khiển máy
lọc/quạt/HVAC nếu thiếu tích hợp khả dụng và quyền từ người dùng.
Không thêm loại log well-being riêng cho môi trường.
