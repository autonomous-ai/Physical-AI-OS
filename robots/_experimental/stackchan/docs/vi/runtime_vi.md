# Chạy Stack-chan qua HAL trên máy host

Profile thử nghiệm này chạy `hal.server` thật của repository với driver chuyển
động Stack-chan trên máy tính. Profile chỉ khai báo motion và system; chưa là
thiết bị Autonomous tương thích đầy đủ, mục tiêu phát hành OTA hay tích hợp
media. Board `host` không có GPIO hoặc bus servo cục bộ. `HAL_SIMULATE=0`
chọn transport thật tới thân robot từ xa.

## Khởi động HAL

Chạy tại thư mục gốc repository, với môi trường Python HAL và dependencies
đã có ở `hal/.venv`. Cấu hình HAL trên host nằm trong profile tại
`rootfs/opt/hal/.env`, theo cấu trúc các robot khác. Sao chép thành file local
riêng; `cp -n` giữ nguyên cấu hình nếu file đích đã tồn tại:

```bash
mkdir -p "$PWD/.local/stackchan"
cp -n robots/_experimental/stackchan/rootfs/opt/hal/.env "$PWD/.local/stackchan/.env"
chmod 600 "$PWD/.local/stackchan/.env"
```

Sửa `.local/stackchan/.env`: đặt device ID firmware, token riêng dài ít nhất
32 ký tự và đường dẫn đến certificate/key TLS đã có. Không ghi credential thật
vào profile được Git theo dõi. File này cấu hình HAL trên máy tính; ESP32 cần
cấu hình firmware riêng tương ứng. Sau đó chạy HAL với file env này:

```bash
export HAL_USERS_DIR="$PWD/.local/stackchan/users"
export HAL_STRANGERS_DIR="$PWD/.local/stackchan/strangers"
export HAL_LOG_DIR="$PWD/.local/stackchan/logs"
export HAL_STATE_DIR="$PWD/.local/stackchan/state"
export OS_CONFIG_PATH="$PWD/.local/stackchan/config.json"
mkdir -p "$HAL_USERS_DIR" "$HAL_STRANGERS_DIR" "$HAL_LOG_DIR" "$HAL_STATE_DIR"
HAL_BOARD=host DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  HAL_SIMULATE=0 HAL_MODE=developer PYTHONPATH=. \
  hal/.venv/bin/python -m uvicorn hal.server:app \
    --env-file "$PWD/.local/stackchan/.env" --host 127.0.0.1 --port 5001
```

`HAL_MODE=developer` tắt hạn chế truy cập HTTP của HAL. Giữ bind HTTP tường
minh tại `127.0.0.1`; listener cho thân robot là dịch vụ TLS có xác thực riêng.
Các thư mục có quyền ghi phục vụ đường khởi động dùng chung của HAL dù profile
không bật nhận diện khuôn mặt hoặc media. `OS_CONFIG_PATH` tách lần chạy thử
khỏi config OS của thiết bị; file có thể chưa tồn tại khi chỉ thử motion độc
lập. Khi nối với OS cục bộ, đổi biến này tới config thực tế của OS đó.
Không chạy nhiều Uvicorn worker vì
mỗi worker sẽ cùng bind cổng 8765.

Firmware phải chủ động kết nối đến
`wss://<host-reachable-name>:8765/stackchan/body/v1`, xác minh certificate cho
host đó và gửi header `Authorization: Bearer <token>`. Hello phải khớp
`STACKCHAN_DEVICE_ID` và khai báo capability chuyển động theo thời lượng,
đọc vị trí đo được, halt/hold và nhả torque. HAL chờ kết nối; khởi động không
tự động di chuyển thân robot.

Chỉ khi thử plain WS trong mạng cô lập: để trống cả hai giá trị TLS trong file env local, đặt
tường minh `STACKCHAN_BODY_ALLOW_INSECURE_WS=1` trong file đó và dùng URL `ws://` tương ứng
trong firmware. Cấu hình thông thường vẫn dùng TLS.

## Kiểm tra không di chuyển

```bash
curl --fail http://127.0.0.1:5001/device
curl --fail http://127.0.0.1:5001/health
```

`/device` phải trả định danh `stackchan` và motion driver `stackchan`.
`/health` trả `servo: false` khi chưa kết nối và `servo: true` sau handshake
có xác thực. Kết quả này chỉ xác nhận kết nối khả dụng. Không kỳ vọng media.
HAL client Go của OS hiện dùng hằng số `http://127.0.0.1:5001`, nên tích hợp
trực tiếp này cần OS và HAL trên cùng host; client đó không có biến ghi đè
`HAL_BASE_URL`.

## Firmware và qualification

[Repo tích hợp Stack-chan riêng tại revision đã đối chiếu](https://github.com/glifocat/stackchan-autonomous/tree/9a5209596b97c257b6b2c1f6ff6bec91c44f8112)
chứa firmware patch và HTTP bridge độc lập. Pull repository OS này không đổi
bridge đó sang HAL, không cấu hình hay flash ESP32 và không cài service. Khi
thử đường này, cấu hình đích kết nối firmware tới body listener của HAL.

Firmware đã đối chiếu trả `completed` với state `scheduled` khi nhận chuyển
động theo thời lượng; HAL gia hạn controller lease trong lúc nội suy.
`motion.halt` xóa lease, nên HAL acquire lại trước chuyển động tiếp theo.
Source tương thích không xác định firmware thực tế đã flash lên từng thân máy.

Profile giới hạn tốc độ ở mức thử nghiệm chưa qualification là 10 độ/giây.
Driver cho phép yaw ±30, pitch ±15 độ quanh midpoint pitch legacy 45 độ;
gravity-rest nhắm yaw 0, pitch -15. Đây chưa là hiệu chuẩn vật lý. Đọc
[SAFETY.md](../../SAFETY.md) trước khi qualification chuyển động có giám sát.
Test startup và protocol trên host không xác nhận CTS đầy đủ hay an toàn vật lý.

### Commissioning home tùy chọn

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

Trong phiên thử nghiệm có giám sát, giữ môi trường và env file riêng như phần
khởi động host ở trên, nhưng thay target Uvicorn bằng factory này:

```bash
HAL_BOARD=host DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  HAL_SIMULATE=0 HAL_MODE=developer PYTHONPATH=. \
  hal/.venv/bin/python -m uvicorn \
    robots._experimental.stackchan.commissioning:create_app --factory \
    --env-file "$PWD/.local/stackchan/.env" --host 127.0.0.1 --port 5001
```

Chỉ chạy một process HAL: entrypoint này dùng lại lifecycle và kết nối body của
HAL, không khởi động bridge thứ hai hoặc đổi entrypoint OS chuẩn. Chỉ đặt
`STACKCHAN_HOME_COMMISSIONING_ENABLED=1` trong env file riêng cho phiên có giám
sát, rồi tắt sau đó. Script thử nghiệm dùng `/servo/home*` cần chuyển sang
`/stackchan/home*`; không có alias trong route chung.

## Chạy OS trên cùng host

Giữ HAL chạy trong terminal đầu tiên. Trong terminal khác, dùng target phát
triển OS hiện có với cùng thư mục profile và thư mục state riêng:

```bash
make os-dev DEVICE_TYPE=stackchan DEVICES_DIR="$PWD/robots/_experimental" \
  OS_STATE_DIR="$PWD/.local/stackchan/os"
```

Trỏ `OS_CONFIG_PATH` của HAL tới `$PWD/.local/stackchan/os/config/config.json`
trước khi khởi động HAL để dùng chung cấu hình này. Target OS mặc định dùng
runtime Codex; cài/cấu hình runtime đã chọn và chạy gateway theo
[hướng dẫn phát triển simulator hiện có](../../../../../docs/vi/simulator_vi.md).
Bước Stack-chan thay cho bước HAL `make sim`: không chạy thêm HAL thứ hai hoặc
bridge riêng trên cổng 5001. Profile không cài runtime agent hay firmware ESP32.
