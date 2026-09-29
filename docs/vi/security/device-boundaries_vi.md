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

`POST /audio/record` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Thời lượng giới hạn 1–30000 ms; giá trị sai trả 422. Nếu phát hiện mute sau khi thu, bản thu bị loại bỏ. `POST /audio/play-tone` trả 409 khi loa bị mute hoặc khóa riêng tư. Âm thử giới hạn 1–5000 ms và 20–20000 Hz (ngoài giới hạn trả 422). Kiểm tra trước khi mở thiết bị âm thanh, cả trong simulation. Không cam kết hủy tức thì bản thu đã bắt đầu.

Âm thử trong giờ yên tĩnh giữ nguyên hành vi cũ; bản sửa không mở rộng chính sách giờ yên tĩnh của nhạc.

## Quyền riêng tư khi đăng ký giọng nói

`POST /speaker/record-enroll` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Kiểm tra lại sau khi dừng listener và sau khi thu; bản thu vừa bị mute bị loại trước khi đăng ký. Chỉ khởi động lại listener nếu trước đó đang chạy và microphone vẫn được phép hoạt động. Thời lượng vẫn giới hạn 1–60 giây. Không cam kết dừng tức thì tiến trình thu đã chạy.

## Quyền riêng tư khi chụp ảnh

`GET /camera/snapshot` trả 409 khi người dùng tắt camera thủ công hoặc khóa riêng tư phần cứng đang bật. Lệnh tắt thủ công vẫn lưu quyền kiểm soát thủ công khi camera đã tạm dừng tự động. Camera tạm dừng tự động vẫn có thể được bật tạm để chụp cho đến khi có lệnh tắt thủ công. Các yêu cầu snapshot tuần tự hóa bật/chụp/tắt để không dừng camera của nhau. Mutex snapshot không giữ khóa riêng tư; thao tác tắt thủ công/phần cứng vẫn hoạt động trong khi chụp và kết quả bị khóa được loại bỏ. Nếu thao tác khác đã bật camera trong khi chụp, bước dọn dẹp không tắt lại camera.

## Symlink của file agent

Web Chat (`GET /api/agent/file`) và MQTT (`chat.file.get`) dùng chung resolver. Phần mở rộng của đường dẫn yêu cầu và đích symlink đều phải được phép: symlink `.txt` không thể mở file `.json`, `.log` hoặc không có phần mở rộng, dù nằm trong thư mục hợp lệ. Symlink hợp lệ vẫn hoạt động và dùng MIME của đích. Kiểm tra thư mục, file thường, giới hạn 32 MiB và xác thực giữ nguyên.

## Đường dẫn file HAL

Các route ảnh/file khuôn mặt resolve đường dẫn và yêu cầu nằm trong `USERS_DIR`; từ chối thư mục ngoài trùng tiền tố và symlink thoát ra ngoài. Ghi danh giọng nói dùng WAV tạm riêng tư có tên ngẫu nhiên, độc lập tên người dùng; dọn file và khôi phục trạng thái service kể cả khi tạo file tạm thất bại.

## Phạm vi sửa lỗi tháng 9/2026

Các bản sửa xử lý ingestion thiếu xác thực, credential trong log, bỏ qua trạng thái privacy, đoán mã pairing, giới hạn tốc độ servo đã khai báo, đường dẫn plugin và file. Giữ nguyên chính sách ký/metadata OTA, credential admin hiện tại, onboarding LAN, quiet-hours của diagnostic và giới hạn góc chưa khai báo. CORS/HAL tin header và checksum pinning của archive vẫn là mục review riêng, không được coi là lỗi đã sửa. Phần giới hạn đường dẫn giải nén Piper đã được sửa như mô tả bên dưới.

## Quyền sở hữu Buddy handler

Server nhận `BuddyHandler` qua con trỏ từ Wire để không sao chép bộ giới hạn xác nhận pairing và mutex khi khởi tạo dependency. Giới hạn pairing và hành vi endpoint không thay đổi.

## Phản hồi servo và giải nén Piper

Lệnh move/aim/nudge/resume/track khi ngủ trả 409, không đánh thức body. Move tách mục tiêu yêu cầu và vị trí quan sát, trả `clamped: null`; aim/nudge cũng tách mục tiêu khỏi số đo. Không đổi giới hạn driver hoặc calibration. Vị trí driver báo có thể là mục tiêu cache trên driver như Reachy, không chứng minh body đã tới đích. Xem [HAL API](../os-server_vi.md) về tương thích phản hồi.

Archive engine Piper được kiểm tra trước và lọc trong lúc giải nén vào thư mục tạm mới. Đường dẫn và link phải nằm trong cây release `piper`; từ chối file đặc biệt, symlink thư mục hoặc đích không tồn tại. Vẫn hỗ trợ link file thư viện hợp lệ bên trong release. Giữ nguyên URL release HTTPS cố định; chưa thêm checksum pinning.

## Phạm vi kiểm chứng

Ngày 2026-09-29, lint HAL, toàn bộ test HAL local (3.093 pass; 3 skip; 140 subtest) và bộ test tập trung servo/Piper mới nhất (36 pass) đều đạt. Device `lamp-0c4e` đã pass chặn lệnh khi ngủ, phản hồi move/nudge nhỏ và archive Piper hợp lệ/độc hại trong thư mục tạm; đã khôi phục trạng thái sleep/mute ban đầu. Chưa test tải hoặc thay engine Piper thật. Device có camera `lamp-4ace` đã pass chụp JPEG thật, snapshot đồng thời, tắt thủ công qua restart và bật lại. Các ca local skip phần cứng/audio và Pipecat tùy chọn được tách biệt với test device có phạm vi này; chưa test vật lý các driver robot khác.
