# Quyền riêng tư của API kiểm tra âm thanh

`POST /audio/record` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Thời lượng giới hạn 100–30000 ms; giá trị sai trả 422. Nếu phát hiện mute sau khi thu, bản thu bị loại bỏ. `POST /audio/play-tone` trả 409 khi loa bị mute hoặc khóa riêng tư. Âm thử giới hạn 1–5000 ms và 20–20000 Hz (ngoài giới hạn trả 422). Kiểm tra trước khi mở thiết bị âm thanh, cả trong simulation. Không cam kết hủy tức thì bản thu đã bắt đầu.

Âm thử trong giờ yên tĩnh giữ nguyên hành vi cũ; bản sửa không mở rộng chính sách giờ yên tĩnh của nhạc.
