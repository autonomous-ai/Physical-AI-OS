# Quyền riêng tư khi chụp ảnh

`GET /camera/snapshot` trả 409 khi người dùng tắt camera thủ công hoặc khóa riêng tư phần cứng đang bật. Camera tạm dừng tự động vẫn có thể được bật tạm để chụp. Các yêu cầu snapshot tuần tự hóa bật/chụp/tắt để không dừng camera của nhau. Mutex snapshot không giữ khóa riêng tư; thao tác tắt thủ công/phần cứng vẫn hoạt động trong khi chụp và kết quả bị khóa được loại bỏ. Nếu thao tác khác đã bật camera trong khi chụp, bước dọn dẹp không tắt lại camera.
