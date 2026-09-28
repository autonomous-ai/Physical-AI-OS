# Quyền riêng tư khi đăng ký giọng nói

`POST /speaker/record-enroll` trả 409 khi microphone bị mute bằng phần mềm hoặc khóa phần cứng. Kiểm tra lại sau khi dừng listener và sau khi thu; bản thu vừa bị mute bị loại trước khi đăng ký. Chỉ khởi động lại listener nếu trước đó đang chạy và microphone vẫn được phép hoạt động. Thời lượng vẫn giới hạn 1–60 giây. Không cam kết dừng tức thì tiến trình thu đã chạy.
