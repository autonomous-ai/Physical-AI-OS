# Tích hợp liên tục

`.github/workflows/ci.yml` chạy Go build/vet/test, web build/lint và kiểm tra
Python trên push và pull request. Python dùng 3.12, cùng phiên bản với HAL.

Chạy các kiểm tra Python ở local:

```bash
python3 -B scripts/ci/check_python_syntax.py
python3 -B -m unittest discover -s scripts/ci -p 'test_*.py' -v
python3 -B -m unittest hal.test.test_route_contracts -v
python3 -B -m unittest discover -s runtimes/hermes -p '*_test.py' -v
```

Bước syntax compile **mọi file `.py` được Git quản lý**, bao gồm router HAL
mới, không import thư viện phần cứng và không ghi bytecode. Cần add file mới
vào Git trước khi chạy local. Sai cú pháp decorator làm CI fail; decorator đúng
cú pháp nhưng lỗi khi thực thi vẫn cần test import hoặc test hành vi.

Test khai báo `/emotion` xác nhận handler POST được đăng ký bắt buộc nhận
`EmotionRequest` và khai báo `EmotionResponse`. Nó bắt lỗi decorator từng gắn
nhầm vào `harness_blocks_sleep` dù source vẫn compile được. Đây là hợp đồng
cho endpoint cụ thể, không phải test import toàn bộ route.

Patch Python nhúng của Hermes phải có suite `<patch_stem>_test.py` ở thư mục
gốc `runtimes/hermes/`. CI từ chối script nhúng `*_patch.py` hoặc script trong
`patches/` nếu thiếu suite được discovery tìm thấy hoặc suite không có test.
Suite phải áp dụng patch lên fixture theo cấu trúc upstream, compile source
sau patch, kiểm tra chạy lại và trường hợp anchor không còn phù hợp. Compile
script patch không đủ để kiểm tra Python nằm trong chuỗi replacement.

Test hợp đồng config channel chạy trong `go test ./system/device`, kiểm tra
request rỗng hoặc thay đổi setting không liên quan vẫn giữ config channel,
kể cả field/channel mới được test tự phát hiện. Chúng không thay thế test setup,
chuyển credentials hoặc test channel end-to-end trong PR feature.

CI pass xác nhận các hợp đồng được kiểm tra, không bảo đảm hết mọi bug hoặc
tương thích mọi revision Hermes tương lai. Workflow mới chỉ có hiệu lực khi
được đưa vào branch/merge đang test; kết quả xanh cũ không chứng minh đã chạy
các kiểm tra mới.
