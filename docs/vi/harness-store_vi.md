# Harness Store từ Autonomous OS

OS tích hợp contract Store v1 trong [OpenHarness PR #245](https://github.com/autonomous-ai/openharness/pull/245), ghim ở `f54a70a782b7a4215e50f000399d777eb689ff84`. [Schemas và fixture giả lập dùng chung](../contracts/autonomous-device-store-v1/README.md) được sao chép nguyên trạng từ revision đó. [English](../harness-store.md).

Đường gọi giữ nguyên `harness-use → /api/harness/request` qua loopback → kết nối trực tiếp E2EE đã xác thực → Harness CLI. Không thêm credential, pairing flow hoặc generic app/admin dispatcher. Các lệnh Store yêu cầu **đủ bốn** capability đã thương lượng qua hello: `store.list`, `store.inspect`, `agent.prepare`, `operation.get`. CLI cũ vẫn dùng các thao tác agent hiện có; lệnh Store báo cần cập nhật CLI, không chuyển sang script setup hay Buddy. Code tương thích không có nghĩa CLI đang chạy đã được cập nhật.

## Tìm package và chuẩn bị agent

Model chọn package từ metadata `store.list` và yêu cầu người dùng. Tìm kiếm khớp mọi từ khóa trong ID/tên/mô tả/category: dùng tên app hoặc lĩnh vực, không dùng cả câu task làm query. Mỗi trang tối đa mười package; dùng `nextOffset` khi cần. `store.inspect` trả package và tối đa năm agent có `packageId` được ghi nhận khớp package. `candidatesTruncated` nghĩa là có thể còn agent trong `agents.list`. Không suy package identity từ tên hoặc recap.

Chỉ chọn rõ candidate hiện có cho đúng project người dùng muốn, dựa trên package/workspace/runtime thực tế. Nếu không, preparation tạo session mới. Workspace `{kind:"new"}` dùng quy ước thư mục Harness; tên ASCII tùy chọn được kiểm tra theo contract. `{kind:"existing",path:"..."}` phải là thư mục tuyệt đối có sẵn do chủ sở hữu chọn rõ; không bịa đường dẫn desktop hay chiếm project của agent khác. Preparation không chứa nội dung task và không gửi task tới model.

Harness sở hữu cài đặt, xử lý dependency đã duyệt, setup, doctor, materialize workspace và khởi chạy agent. OS hiển thị dữ kiện `installation`, `readiness`, lỗi và guidance. `verified`/`installAllowed` không chứng nhận an toàn hay task thành công; package/dependency cộng đồng có thể cần chủ sở hữu duyệt trong Desktop. `requirements.applications` là null trong v1, không được tự bịa tên dependency. Doctor pass là quan sát tại thời điểm kiểm tra; `engineAuthentication` và thành công của task vẫn chưa biết.

## Lệnh helper trên thiết bị

Chạy `python3 scripts/harness.py ACTION -` trong `skills/harness-use`, đưa JSON qua stdin. Giữ `conversation_id` ổn định (mặc định `voice`) và `intent_id` qua retry. Run ID phản hồi thiết bị được cung cấp phù hợp làm intent ID ban đầu; lượt sau tiếp tục ID đã lưu, có thể đưa response route hiện tại trước dispatch. `response` là metadata local `{run_id,channel:"voice"|"web"}`, không thuộc schema Store trên đường truyền.

| Action | Tham số |
|---|---|
| `store-list` | `query`, `offset`, `limit` (1–10) tùy chọn |
| `store-inspect` | `packageId` từ discovery |
| `prepare` | Intent mới: `intent_id`, `text` gốc, `packageId`, `workspace`; hoặc `agentId` hiện có được chọn rõ thay workspace. `response` tùy chọn. Resume: cùng `intent_id`, không thay tham số bất biến. |
| `operation` | `intent_id`, `wait_seconds` (0–20) và `response` hiện tại tùy chọn; poll accepted/running cách hai giây, backoff rate limit tới hai mươi giây |
| `dispatch` | `intent_id`, `response` hiện tại tùy chọn; refresh readiness rồi dự trữ và gửi một lần |
| `workflow-status` | `intent_id` tùy chọn; đọc journal local, dùng được offline |
| `workflow-receipt` | `intent_id`; đối chiếu task đã lưu, không gửi lại |
| `workflow-resolve` | `intent_id`, `resolution:"do_not_retry"`; bỏ chờ delivery khi người dùng cho phép rõ, dùng được offline; không hủy từ xa, không dispatch lại intent đó |

State file mặc định `~/.local/state/autonomous/harness-voice.json` (ghi đè bằng `HARNESS_VOICE_STATE`). Lưu tối đa 64 conversation và 256 intent Store mỗi conversation, không tự loại bằng chứng delivery. Journal đầy thì từ chối intent mới. Task đã tồn tại chặn dispatch trùng kể cả sau khi bỏ chờ rõ ràng. Resolution không xóa dữ liệu preparation/task. Các lệnh status/list/recap chỉ đọc vẫn dùng để kiểm tra agent hiện có khi delivery chưa rõ.

## Intent lưu bền và khôi phục

Helper trên thiết bị lưu nguyên yêu cầu người dùng, intent ID ổn định, tham số/key preparation bất biến, operation ID, machine/agent/workspace được trả về, **task key riêng**, trạng thái dự trữ/receipt delivery và server instance trong journal riêng tư. Dùng cùng state path/khóa conversation với `harness-use` thường, ghi thay thế atomic, fsync file và thư mục. Không xóa journal để retry.

Timeout preparation có thể xảy ra sau khi operation đã được nhận. Tiếp tục intent đã lưu, retry đúng tham số và preparation key nếu chưa có operation ID. Nếu đã có ID, poll `operation.get`. Không tạo intent/key mới vì timeout, mất kết nối, rate limit, không tìm thấy operation hay `needs_user_action`. Intent mới có chủ ý có thể tạo session khác. Thay đổi pairing/machine identity không được đổi đích công việc đã lưu.

Dispatch dự trữ task key trước khi gửi nguyên nội dung cho agent đã chuẩn bị. Refresh preparation ready trước khi dispatch; đích trả về được chỉ định rõ, không phụ thuộc focus Desktop. Khi đã thử gửi task, gọi dispatch lại không gửi lần nữa. Đối chiếu bằng `workflow-receipt`; receipt mất sau khi daemon restart không chứng minh chưa giao. Báo chưa rõ và kiểm tra agent/task hiện có thay vì retry. Giữ receipt và server instance quan sát được; idempotency preparation không làm cơ chế chống trùng turn trong RAM của Harness trở thành bền vững.

Receipt task đã biết (`queued`, `delivered`, `started`, `completed`, `rejected`) kết thúc lượt skill bằng `NO_REPLY` khi có response route. Preparation `accepted` hay `ready` không phải receipt đó và không được làm task bị bỏ im lặng. `ready` chỉ cho phép dispatch riêng; kết quả agent thật vẫn đi qua lifecycle/question/recap hiện có.

## Tiến độ và yêu cầu người dùng thao tác

V1 không thêm progress event Store. Tiến độ đến từ snapshot `agent.prepare` và `operation.get`. Poll khoảng hai giây và backoff khi rate limit. Helper cho đọc trạng thái đã lưu để tiếp tục sau follow-up/restart; không phải scheduler nền tự chạy và không tự gửi khi kết nối trở lại.

Metadata local `response:{run_id,channel}` gắn quan sát preparation với lượt thiết bị hiện tại. OS bỏ metadata trước khi truyền E2EE, hiển thị tiến độ trong monitor/chat hiện có khi lượt main-agent gốc còn active và ghi `harness_store_progress`. Preparation không chiếm quyền phản hồi cuối hay chặn main agent, nên main vẫn giải thích `needs_user_action` và guidance. Các lượt poll không tự phát TTS. Snapshot liên tiếp có cùng nội dung hiển thị, operation và lượt active không nối thêm văn bản chat. Tiến độ thay đổi được ngăn bằng đoạn mới; chống lặp này chỉ ở RAM theo lượt active, không phải bằng chứng giao task. Main diễn đạt tiến độ thật bằng ngôn ngữ người dùng; không coi agent ready là sản phẩm đã hoàn thành.

`accepted` nghĩa là đã lưu intent, `running` là đang chuẩn bị, `ready` là chuẩn bị xong, `failed` cần xem lỗi, `needs_user_action` cần hiển thị lỗi/guidance và giữ operation. Agent ID có thể đã tồn tại khi cần người dùng thao tác: hướng chủ sở hữu tới agent đó thay vì tạo thêm. Giữ lỗi chưa biết để hiển thị, không coi là thành công. Duyệt tool vẫn thuộc Desktop; `question.answer` không phê duyệt quyền tool.

Thời gian chờ preparation có giới hạn riêng, ngoài timeout từng RPC. Helper lưu bền **budget 90 giây cho mỗi response run**; gọi lại hoặc reconnect không gia hạn. `PREPARATION_WAIT_EXPIRED` dừng poll, yêu cầu main giải thích task chưa được gửi; preparation từ xa và journal vẫn giữ nguyên. Lượt người dùng mới có thể tiếp tục cùng intent/operation/key với response route mới. Timeout không cho phép tạo agent khác hay gửi task muộn.

OS có thêm **deadline 120 giây** từ prepare/poll đầu tiên có response route. Hết hạn và nhận dispatch được đồng bộ: route hết hạn không được gửi task; task đã được nhận để gửi không bị watchdog preparation hủy vì delivery có thể chưa rõ. Native Hermes triển khai `RunExpirer` tùy chọn: chỉ dừng đúng owner hiện tại và kết thúc bằng `lifecycle.error` sau cleanup từ xa có giới hạn (tối đa thêm 30 giây), không báo task thành công. Deadline cũ không được hủy request mới được steer vào. Runtime khác hiện có budget helper và chặn dispatch, nhưng chưa bảo đảm hủy runtime; trường hợp không hỗ trợ được ghi log. Deadline OS ở RAM, budget helper lưu bền. Không tự retry dispatch hoặc đổi contract Store.


## Phạm vi kiểm chứng

Unit/contract tests OS dùng schemas đã ghim, fixture Blender giả lập, kết quả request giả và journal trong thư mục tạm. Kiểm tra recovery bền vững và hành vi transport/progress mà không cài package, chạy Blender, tạo session agent có phí hay kết nối robot. Các test không xác nhận CLI thực, đăng nhập engine hay artifact được tạo.

Kiểm thử end-to-end chờ cập nhật CLI được cho phép rõ và hello thực quảng bá đủ bốn capability. Sau đó kiểm discovery → inspect → prepare/poll → gửi task riêng trên thiết bị đã pair, ghi guidance và artifact thực tế. Thay đổi source này không deploy robot hoặc thực hiện lượt acceptance đó.

Chạy lại kiểm tra OS (chỉ dependency trong môi trường tạm):

```sh
python3 -m unittest discover -s skills/harness-use/tests
uv run --no-project --with jsonschema==4.26.0 python -m unittest discover -s skills/harness-use/tests -p contract_validation.py
go test -race ./system/harness ./system/server ./system/server/agent/delivery/http ./system/server/sensing/delivery/http
```

Lệnh đầu không cần dependency ngoài; schema validation chạy riêng để thiếu `jsonschema` không làm kiểm contract bị bỏ qua im lặng. Test fixture transport Go dùng WebSocket localhost thật và payload mã hóa với peer giả lập, không phải Harness CLI đang chạy.
