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
kiểm tra lại cấu hình và file. Skill đơn giản từ workspace, bundled, managed và
plugin đều có thể tham gia qua đường dẫn `SKILL.md` canonical chính xác do snapshot
native cung cấp, không giới hạn thư mục workspace. Thứ tự snapshot native quyết
định ưu tiên khi trùng tên. Skill có metadata dependency/platform, policy
invocation riêng, tool policy chưa hỗ trợ, sandbox đang bật, symlink hoặc file quá
lớn được giao cho cơ chế nạp native. Snapshot thiếu hoặc không tương thích cũng
bỏ qua, kể cả runtime không cung cấp accessor session cần thiết. Không quét skill
trên toàn filesystem.

Không còn giới hạn 32 ứng viên. Request đầy đủ sau serialize UTF-8 phải nằm trong
256 KiB; vượt mức thì bỏ qua trước khi đọc credential hay gọi mạng, không cắt danh
sách tùy ý. File skill vẫn giới hạn 32 KiB; response và envelope preload giới hạn
64 KiB. Ngưỡng choice ít nhất 0.70, margin ít nhất 0.20 và fit độc lập ít nhất 0.60,
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

## Dấu xác nhận workspace (OpenClaw >= 2026.9)

Khi tạo workspace, OpenClaw ghi một dấu xác nhận (attestation) vào
`/root/.openclaw/state/openclaw.sqlite` (bảng `workspace_setup_state` và các bảng
liên quan). Trong 24 giờ sau đó, `openclaw onboard` từ chối tạo lại một workspace
trông như đã bị xoá (`WorkspaceVanishedError: … Refusing to reseed BOOTSTRAP.md
over a recently attested workspace`), làm setup thất bại với `agent_setup_failed`.

- **Factory reset** (`runtimes/openclaw/reset.go`) xoá `/root/.openclaw/state`
  cùng với workspace, nên setup ngay sau reset vẫn onboard lại được.
- **Build ảnh** (`scripts/imager/build*.sh`) chạy `openclaw onboard` trong chroot
  (tối đa 180 giây), rồi xoá các dòng workspace trong database state và các file
  dấu xác nhận kiểu cũ. Vì vậy ảnh build vài giờ trước lần setup đầu tiên không
  mang theo dấu xác nhận mới, kể cả khi onboard trong chroot bị timeout và để lại
  workspace trống.

Máy đã bị kẹt lỗi này: chạy `sudo rm -rf /root/.openclaw/state
/root/.openclaw/workspace-attestations` rồi setup lại.

## Tiền tố câu trả lời và trả lời heartbeat

- **Không gắn tiền tố.** Trước đây setup ghi `messages.responsePrefix: "auto"`,
  OpenClaw hiểu là tên định danh của agent; agent không đặt tên thì dùng id, nên
  mọi câu trả lời trong chat bắt đầu bằng `[main]`. Lệnh `doctor` của OpenClaw
  còn chép giá trị này xuống `channels.<channel>.responsePrefix`. Giờ setup không
  ghi nữa, và onboarding (`ensureMessagesQueueConfig`) xoá `"auto"` khỏi
  `messages`, mọi channel và mọi account của channel trên máy đã setup, rồi
  restart gateway. Tiền tố người dùng tự đặt được giữ nguyên.
- **Heartbeat kết thúc bằng `NO_REPLY`.** Khối OS trong `workspace/HEARTBEAT.md`
  trước đây ghi "skip silently", và model đôi khi trả về tin rỗng. OpenClaw coi
  heartbeat rỗng là `agent-runner-failure` và gửi `[main] ⚠️ Agent couldn't
  generate a response. Please try again.` lên chat gần nhất. Giờ khối này dặn
  agent trả lời đúng `NO_REPLY` khi xong, là token heartbeat im lặng của OpenClaw.
  Onboarding ghi lại khối này trên máy đã setup vì nội dung đã đổi.
- **SOUL của thiết bị phải nằm trong giới hạn bootstrap.** Setup và onboarding
  cùng dùng `agents.defaults.bootstrapMaxChars = 24000` và
  `agents.defaults.bootstrapTotalMaxChars = 48000`, lấy từ các hằng số chung trong
  `runtimes/openclaw/onboarding.go`. Đây là ngân sách ký tự, không phải giới hạn token.
  Mức 24k mỗi file chứa đủ SOUL lamp (18.615 ký tự), có chỗ cho marker OS và chỉnh
  sửa của chủ máy. SOUL lamp cộng các khối AGENTS và HEARTBEAT do OS quản lý có
  tổng khoảng 27,9k ký tự; mức 48k dành thêm khoảng 20k cho các file bootstrap khác
  và nội dung bổ sung. Đây là phần dự phòng theo kích thước, chưa phải mức tối ưu
  độ trễ đã đo hay bảo đảm mọi workspace lớn đều vừa.
  Giới hạn mỗi file và giới hạn tổng đều áp dụng; file dài vẫn có thể mất phần
  giữa (OpenClaw giữ đầu và cuối). Thiết bị hiện có nhận giới hạn mới khi chạy
  onboarding sau triển khai; onboarding yêu cầu restart gateway khi defaults
  thay đổi. `TestDeviceSoulsFitTheBootstrapCap` kiểm tra SOUL của mọi thiết bị,
  kể cả lamp, với mức 23.000 ký tự, dành 1.000 cho marker OS và mục `## Personal`
  của chủ máy. SOUL intern-v2 vẫn khoảng 10,5k; thẻ âm thanh chỉ dùng cho câu trả
  lời được đọc lên, không dùng trong chat qua channel hoặc web.
