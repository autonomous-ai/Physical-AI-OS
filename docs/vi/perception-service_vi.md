# DL Backend

Backend nhận thức (perception) tăng tốc GPU cho thiết bị Autonomous. Nó chạy các mô
hình deep-learning mà HAL trên thiết bị không chạy nổi cục bộ (nhận dạng hành động,
cảm xúc khuôn mặt và giọng nói, tư thế + công thái học, phát hiện vật thể, embedding
người nói) và expose qua WebSocket và HTTP, phía sau một load balancer mã hóa tùy chọn.

Trang này là **tổng quan nền tảng** — backend là gì và nằm ở đâu. **Tài liệu đầy đủ
nằm cạnh code** trong [`integrations/perception-service/docs/`](../../integrations/perception-service/docs/); schema, danh sách
model, endpoint và biến môi trường để ở đó, không để ở đây.

| Cần… | Đọc |
|------|-----|
| Topology tiến trình, cổng, prefix URL, vòng đời request | [`integrations/perception-service/docs/architecture.md`](../../integrations/perception-service/docs/architecture.md) |
| Từng endpoint kèm schema request/response | [`integrations/perception-service/docs/api.md`](../../integrations/perception-service/docs/api.md) |
| Mô hình perception, enum, kiểu output | [`integrations/perception-service/docs/perceptions.md`](../../integrations/perception-service/docs/perceptions.md) |
| Load balancer + mã hóa RSA/AES + nginx | [`integrations/perception-service/docs/crypto-and-loadbalancer.md`](../../integrations/perception-service/docs/crypto-and-loadbalancer.md) |
| Deploy: cài đặt, scale GPU, RunPod, Docker, TLS | [`integrations/perception-service/docs/deployment.md`](../../integrations/perception-service/docs/deployment.md) |
| Toàn bộ biến môi trường kèm giá trị mặc định | [`integrations/perception-service/docs/configuration.md`](../../integrations/perception-service/docs/configuration.md) |

## Kiến trúc tổng quát

Hai tiến trình FastAPI đứng sau cửa ngõ nginx:

```
HAL / client
   │  https / wss  :8899
   ▼
┌─────────┐   /lelamp/ → /hal/      ┌──────────┐  round-robin   ┌──────────┐
│  nginx  │ ─────────────────────▶ │ lbserver │ ─────────────▶ │ dlserver │
│  :8899  │     (nâng cấp WS)       │  :7999   │  giải mã →      │  :8001   │
└─────────┘                         └──────────┘   plaintext     └──────────┘
```

- **`dlserver`** (`:8001`) — nạp các mô hình ML, phục vụ các endpoint perception.
- **`lbserver`** (`:7999`) — proxy round-robin trước một hoặc nhiều `dlserver`; kết
  thúc (terminate) mã hóa RSA+AES để `dlserver` luôn nhận plaintext.
- **`nginx`** (`:8899`) — cửa ngõ công khai; ánh xạ prefix `/lelamp/` (phía thiết
  bị) sang prefix nội bộ `/hal/` và nâng cấp WebSocket.

Khi dev một node, có thể gọi thẳng `dlserver:8001` với mã hóa tắt.

## Cung cấp những gì

Các subsystem perception expose cho thiết bị: nhận dạng hành động, cảm xúc khuôn mặt,
cảm xúc giọng nói (SER), ước lượng tư thế (kèm công thái học RULA), phát hiện vật thể
và embedding người nói. `/emotion-recognize` nhận `raw: true`, trả về argmax chưa
qua gate cùng xác suất của mọi class; HAL dùng chế độ này và tự áp gate theo label
trên thiết bị. Phát hiện khuôn mặt và phát hiện người chạy nội bộ để cấp dữ
liệu cho các pipeline đó. Các request từ nhiều session đồng thời được gom lại (batch)
trước khi gửi lên GPU — cấu hình `BATCH_SIZE` và `BATCH_TIMEOUT` cho từng model.
Input được giới hạn kích thước (ảnh, audio) để chống tấn công DoS — xem
[`configuration.md#input-limits`](../../integrations/perception-service/docs/configuration.md#input-limits).
Đầu vào SER được giới hạn 2–8 giây với batch 1 phía server, nằm trong dải kích
thước của engine TensorRT đã cache, nên không độ dài clip nào có thể kích hoạt
việc build lại engine trong lúc xử lý request.
Lựa chọn model và output:
[`integrations/perception-service/docs/perceptions.md`](../../integrations/perception-service/docs/perceptions.md).

## Thiết bị dùng nó thế nào

HAL là client chính. Trỏ nó tới backend bằng `DL_BACKEND_URL` và `DL_API_KEY` dùng
chung (gửi qua `X-API-Key`), và tùy chọn bật mã hóa phía client. Sensing stream khung
hình camera tới các endpoint hành động/tư thế/cảm xúc; voice POST âm thanh cuối câu
nói tới endpoint cảm xúc giọng nói. Với đường embedding người nói, HAL chạy pipeline
lọc/VAD/chuẩn hoá audio **tại thiết bị** và gọi `/audio-recognizer/embed` với
`preprocess=false`, nên server chỉ tính embedding (endpoint chỉ để embed và mặc
định `preprocess=false`; chỉ truyền `true` nếu upload audio thô để server tự làm
sạch). HAL cũng đặt `use_sliding_window`: bước enroll gửi `false` để nhồi nguyên
câu vào model một lần (một vector sạch), còn bước recognize gửi `true` để trượt
cửa sổ chồng lấn và bỏ phiếu theo từng chunk. Response embed (và `/health`) còn
trả `embed_model_version` — vân tay của
trọng số model đang nạp, để HAL biết khi model server đổi và **tự tính lại
embedding** cho các hồ sơ giọng đã lưu thay vì so khớp với vector đã lỗi thời
(xem [`robots/lamp/docs/speaker-enrollment.md`](../../robots/lamp/docs/speaker-enrollment.md)). Endpoint và payload chính xác nằm trong
[`integrations/perception-service/docs/api.md`](../../integrations/perception-service/docs/api.md); mọi tham số cấu hình nằm trong
[`integrations/perception-service/docs/configuration.md`](../../integrations/perception-service/docs/configuration.md).

## Triển khai

`integrations/perception-service/` có sẵn `Dockerfile` (CUDA + nginx), `nginx.conf` / `nginx-ssl.conf` và
một `Makefile`; hai tiến trình chạy bằng `python -m dlserver` và `python -m lbserver`.
Cài đặt, single-node vs scale master/slave nhiều GPU, RunPod, Docker và TLS:
[`integrations/perception-service/docs/deployment.md`](../../integrations/perception-service/docs/deployment.md).

---

> **Đồng bộ tài liệu:** trang này chỉ là tổng quan. Khi code đổi, cập nhật tài liệu
> chi tiết trong [`integrations/perception-service/docs/`](../../integrations/perception-service/docs/) **và** bản tiếng Anh
> [`docs/perception-service.md`](../perception-service.md). Code là nguồn chân lý.
</content>
