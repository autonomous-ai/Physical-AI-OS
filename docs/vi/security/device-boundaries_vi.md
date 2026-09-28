# Ranh giới bảo mật thiết bị

## Thông tin xác thực HTTP và log request

Access log HTTP của OS ghi method, path, status, thời gian xử lý và IP client,
nhưng bỏ toàn bộ query string. Điều này bảo vệ request cũ dùng
`?token=<llm_api_key>` và các link provisioning chứa thông tin xác thực khác.
Log khi phục hồi sau panic giữ stack trace, không ghi header, query string
hoặc giá trị panic.

Cơ chế xác thực không đổi: session cookie, Bearer hợp lệ và query parameter
`token` cũ vẫn hoạt động. Sau xác thực, cả hai proxy HAL (`/api/hardware/*`
và `/openapi.json`) xóa mọi giá trị query `token` trước khi chuyển tiếp.
HAL vẫn nhận các query argument khác.

Ưu tiên cookie hoặc header Authorization thay vì đưa thông tin xác thực vào
query. Cơ chế này không xóa log lịch sử, không lọc log của reverse proxy bên
ngoài, lịch sử trình duyệt hay log riêng của ứng dụng. Đổi thông tin xác thực
nếu log đã ghi trước đó bị lộ.

## Công tắc microphone phần cứng

Công tắc tắt microphone phần cứng chặn khởi động voice và bật microphone qua scene, kể cả thiết bị không có khóa camera hoặc loa. `privacy.mic_locked()` kiểm tra trực tiếp trạng thái công tắc. Unmute bằng phần mềm không thể vượt khóa phần cứng.

## Quyền riêng tư của API kiểm tra âm thanh

`POST /audio/record` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Thời lượng giới hạn 100–30000 ms; giá trị sai trả 422. Nếu phát hiện mute sau khi thu, bản thu bị loại bỏ. `POST /audio/play-tone` trả 409 khi loa bị mute hoặc khóa riêng tư. Âm thử giới hạn 1–5000 ms và 20–20000 Hz (ngoài giới hạn trả 422). Kiểm tra trước khi mở thiết bị âm thanh, cả trong simulation. Không cam kết hủy tức thì bản thu đã bắt đầu.

Âm thử trong giờ yên tĩnh giữ nguyên hành vi cũ; bản sửa không mở rộng chính sách giờ yên tĩnh của nhạc.

## Quyền riêng tư khi đăng ký giọng nói

`POST /speaker/record-enroll` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Kiểm tra lại sau khi dừng listener và sau khi thu; bản thu vừa bị mute bị loại trước khi đăng ký. Chỉ khởi động lại listener nếu trước đó đang chạy và microphone vẫn được phép hoạt động. Thời lượng vẫn giới hạn 1–60 giây. Không cam kết dừng tức thì tiến trình thu đã chạy.

## Quyền riêng tư khi chụp ảnh

`GET /camera/snapshot` trả 409 khi người dùng tắt camera thủ công hoặc khóa riêng tư phần cứng đang bật. Camera tạm dừng tự động vẫn có thể được bật tạm để chụp. Các yêu cầu snapshot tuần tự hóa bật/chụp/tắt để không dừng camera của nhau. Mutex snapshot không giữ khóa riêng tư; thao tác tắt thủ công/phần cứng vẫn hoạt động trong khi chụp và kết quả bị khóa được loại bỏ. Nếu thao tác khác đã bật camera trong khi chụp, bước dọn dẹp không tắt lại camera.

## Symlink của file agent

Web Chat (`GET /api/agent/file`) và MQTT (`chat.file.get`) dùng chung resolver. Phần mở rộng của đường dẫn yêu cầu và đích symlink đều phải được phép: symlink `.txt` không thể mở file `.json`, `.log` hoặc không có phần mở rộng, dù nằm trong thư mục hợp lệ. Symlink hợp lệ vẫn hoạt động và dùng MIME của đích. Kiểm tra thư mục, file thường, giới hạn 32 MiB và xác thực giữ nguyên.
