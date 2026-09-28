# Nạp trước skill bằng Jev trong OpenClaw

Khi bật, onboarding của OS cài plugin native `autonomous-jev` tại
`<OpenclawConfigDir>/extensions/autonomous-jev`. Plugin đăng ký hook
`before_prompt_build`, dùng cho cả yêu cầu qua OS và các kênh do OpenClaw xử lý
trực tiếp. **Mặc định tắt** bằng `const jevEnabled = false` trong `runtimes/openclaw/jev_plugin.go`. Khi tắt, bridge không tạo selector; onboarding native không cài asset hay tạo đăng ký Jev mới. Nếu có đăng ký Jev cũ, Go chỉ tắt đăng ký đó; các cấu hình khác được giữ nguyên. Không đọc skill, gọi provider hay thêm nội dung Jev vào yêu cầu khi tắt. Muốn bật sau kiểm chứng, đổi cờ Go, build và restart qua luồng quản lý runtime.

Nếu đã cấu hình `plugins.allow`, thêm `autonomous-jev` vào danh sách đó.
`plugins.deny`, trạng thái tắt plugin và `plugins.enabled: false` vẫn được tôn trọng.
Bật tính năng đồng nghĩa cho phép gửi **yêu cầu hiện tại cùng tên và mô tả các
skill đủ điều kiện** tới `llm_base_url` của OS cộng `/jev/decisions`, bằng API key
OS đang cấu hình. Không có provider hay endpoint dự phòng. File sidecar của plugin
chỉ giữ đường dẫn file config OS, không chứa credential. Nội dung đầy đủ của skill
giữ ở local và được thêm vào yêu cầu hiện tại trong runtime.

Plugin đọc snapshot skill do native tạo, kiểm tra ID session đang chạy, rồi lấy
giao giữa các skill đã resolve và danh sách native quảng bá. Sau khi chọn, plugin
kiểm tra lại cấu hình và file. Bản đầu chỉ hỗ trợ skill đơn giản trong thư mục
`skills/` của workspace hiện tại. Skill có metadata dependency/platform, policy
invocation riêng, tool policy chưa hỗ trợ, sandbox đang bật, symlink hoặc file quá
lớn được giao cho cơ chế nạp native. Snapshot thiếu hoặc không tương thích cũng
bỏ qua, kể cả runtime không cung cấp accessor session cần thiết. Không quét skill
trên toàn filesystem.

Tối đa 32 ứng viên được gửi tới provider. Có hơn 32 thì bỏ qua, không cắt danh sách
tùy ý. Ngưỡng choice ít nhất 0.70, margin ít nhất 0.20 và fit độc lập ít nhất 0.60,
giống chọn skill trong Hermes. Toàn thao tác có deadline 3 giây, không retry, tối đa
một thao tác đang chạy và cooldown 30 giây sau lỗi. Timeout, response sai schema,
abstain hay thay đổi điều kiện skill đều giữ nguyên yêu cầu ban đầu.

Chỉ phân loại yêu cầu hiện tại từ hook; không tìm yêu cầu thay thế trong history.
Lệnh chọn skill/slash rõ ràng, marker system/sensing, marker ảnh/attachment đã biết
và câu phụ thuộc ngữ cảnh như `brighter`, `continue`, `make it brighter` bỏ qua
preload. Main agent vẫn giữ nguyên ngữ cảnh hội thoại. Nội dung skill đầy đủ,
đường dẫn nguồn và thư mục tham chiếu được thêm bằng `prependContext` native,
không thay system prompt hay cấp quyền dùng tool. History native có thể giữ lần
đọc skill này; selector không dùng lại quyết định của lượt trước. Khi đối chiếu
run, OS chỉ bỏ envelope preload đã kiểm tra hợp lệ rồi so với yêu cầu đang chờ.

Log có dạng `[openclaw-jev] outcome=preloaded reason=accepted skill=...`, hoặc
`abstained`, `skipped`, `error`; không log prompt hay credential.

Kiểm chứng local bằng provider mock và fixture snapshot native:

```sh
node --test runtimes/openclaw/plugins/jev/index.test.mjs
go test ./runtimes/openclaw -run 'TestJev|TestPending' -count=1
```

Contract native được đối chiếu với OpenClaw 2026.2.23 đã cài và
[hook type upstream](https://github.com/openclaw/openclaw/blob/main/src/plugins/hook-before-agent-start.types.ts).
Các kiểm tra local không đồng nghĩa đã gọi Jev thật, deploy thiết bị hay test tích
hợp đầy đủ gateway/kênh. Hook bản cũ không cung cấp metadata attachment có cấu trúc;
chỉ nhận diện được field attachment được truyền vào và marker trong văn bản.
