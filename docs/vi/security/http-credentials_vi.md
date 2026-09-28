# Thông tin xác thực HTTP và log request

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
