# Realtime Voice Agent (Trợ lý giọng nói thời gian thực)

Lớp giọng nói speech-to-speech độ trễ thấp, chạy **song song** với pipeline STT
→ agent thông thường. Model realtime xử lý hội thoại tán gẫu trực tiếp (trả lời
âm thanh dưới 1 giây) và **delegate** (chuyển giao) những gì cần đến agent chính
(điều khiển thiết bị, skills, memory, thông tin thời gian thực) về luồng
OS-server.

Code nằm ở `hal/realtime/`; được điều khiển bởi
`hal/drivers/voice/voice_service.py`.

> **Nguồn chân lý:** doc phản ánh code. Nếu lệch nhau, code đúng.

## Telemetry xác nhận thực thi xong

Với [KPI-3 giọng nói](voice-metrics_vi.md#kpi-3-chạy-xong-không-đánh-giá-làm-đúng),
`TurnDoneEvent.execution_completed` mặc định `false`, chỉ thành true khi có
tín hiệu kết thúc từ provider: Gemini Extended Thinking dùng `IDLE` cùng text trả lời được chấp nhận và không còn công việc cục bộ chưa xong; session thiếu status dùng `generation_complete` hoặc
`turn_complete` bình thường, không interrupted; OpenAI `response.done`
với `response.status == "completed"` và không có barge-in trong response đó;
GPT-Live không có terminal nào trên đường truyền, nên adapter tự tổng hợp —
watchdog đóng lượt sau `turn_gap_ms` không có output và không có overlap với
lời người dùng (xem mục *GPT-Live*).
Đóng kết nối, lỗi gửi, sentinel mở chặn,
output dở dang rồi timeout, done cũ/phát lại, hoặc bỏ receive không phải bằng
chứng chạy xong.

Receive loop ở base truyền quan sát tới orchestrator; orchestrator snapshot
trước khi tái tạo session rồi truyền vào
`RealtimeTurnResult.execution_completed`. HAL chỉ phát `realtime_turn_done`
khi turn handled và cờ này true. Run backend đồng bộ memory không chứng minh
turn realtime đã chạy xong. Phần đo xác nhận kết thúc thực thi, không đánh giá
trả lời đúng hay phát hết audio; không thay đổi routing hoặc hành vi playback.

## Khái niệm: handle vs. delegate

Mỗi lượt nói được stream tới model realtime *cùng lúc* với pipeline STT. Cuối
lượt, model sẽ:

- **Handle** (tự xử lý) — tán gẫu / trả lời nhanh — nói lại qua TTS, không cần
  round-trip tới agent chính, hoặc
- **Delegate** bằng cách gọi tool `delegate_to_main` → dừng output realtime và
  chuyển đúng lời người dùng ở lượt hiện tại, giữ nguyên ngôn ngữ, tới OS server
  (→ runtime chính đang được chọn) để xử lý.
  Lúc đó model realtime đã tự nói filler rồi, nên os-server không "ừ" thêm
  lần nữa cho turn delegate (prefix `[voice-instruction]`): không có opening
  filler, và dead-air filler đầu tiên chỉ arm khi tool đầu tiên của agent chính
  bắt đầu (`FillerManager.MarkDelegatedVoiceRun`). Turn delegate mà agent chính
  kết thúc bằng NO_REPLY vì thế im lặng luôn, thay vì hứa một câu trả lời
  không bao giờ tới.
- **Từ chối rõ ràng** một turn chắc chắn không phải người nói với thiết bị bằng
  tool `reject_turn` → bỏ turn trước khi agent chính nhìn thấy STT text. Nó khác
  hẳn model im lặng: im lặng, timeout và lỗi transport vẫn fallback bình thường
  sang agent chính.

**Tìm đồ là một hành động.** "Tìm chìa khóa của tôi", "cái cốc của tôi đâu",
"giúp tôi tìm cây bút được không", "bạn có thấy cây bút của tôi đâu không" — mọi
yêu cầu định vị một vật hoặc một người là một lượt quét bằng camera và servo do
agent chính chạy (`/servo/search`, xem `robots/lamp/docs/vision-tracking.md`).
Lớp realtime phải delegate nó ở mọi cách diễn đạt. Quan sát trên thiết bị
2026-09-15 (lamp-ac82, memory sạch): câu mệnh lệnh trần "Tìm cây bút cho tôi"
được delegate và tìm thấy bút, còn dạng câu hỏi "Bạn có thấy cây bút của tôi đâu
không?" / "Giúp tôi tìm cây bút được không?" bị Gemini tự trả lời — hỏi bút trông
thế nào, đoán vị trí, hoặc đề nghị nhìn mà không nhìn. Quy tắc nằm ở ba chỗ phải
khớp nhau: mô tả tool `delegate_to_main` dùng chung, mô tả tool `look` (tìm đồ
không phải là look), và bullet **Finding things is an action** trong cả bốn prompt
(`system_prompt.md` dùng chung, `system_prompt_gemini.md`, `system_prompt_openai.md`,
`system_prompt_gptlive.md`); `hal/test/test_realtime_find_delegation.py` ghim phần text. Bản thân
quyết định không được ép bằng code — chỉ giọng nói thật trên thiết bị mới kiểm
tra được.

### Research, analysis và tài liệu thì delegate

Grounding chỉ trả lời MỘT sự kiện công khai mới. Việc nhiều bước — nghiên cứu,
so sánh, "nên chọn phương án nào", brainstorm, bất cứ gì kết thúc bằng một báo
cáo hay tài liệu — không phải là tra cứu, và trả lời nó bằng hai câu nói chính
là lỗi mà quy tắc này loại bỏ. Nó nằm ở bullet **Research, analysis &
documents** trong mọi prompt provider và một câu trong mô tả `delegate_to_main`
dùng chung. Hai prompt tìm kiếm được ngay trong phiên mang thêm ranh giới
"single facts only" ngay cạnh quy tắc tìm kiếm của chúng: bullet Google Search
của Gemini, và quy tắc `web_search` ở tầng **backend** của GPT-Live (tầng voice
không có tìm kiếm nên chỉ delegate). Main agent xử lý
tiếp thế nào là việc của nó — có thể đã cài một skill research, cũng có thể trả
lời trong khả năng và nói rõ phần nào chưa kiểm chứng được; kiểu gì việc đó cũng
thuộc lane chính. `hal/test/test_realtime_research_delegation.py` pin phần text
này.

### Channels và connectors thì delegate

Gửi bất cứ thứ gì ra ngoài — tin nhắn, ảnh hay ảnh chụp camera, ảnh lấy trên
mạng, file, link, hay bản recap / tóm tắt cuộc trò chuyện — qua Telegram, email,
Slack hoặc bất kỳ channel/connector nào khác là việc của main agent: chỉ main
agent nắm các channel, nên tầng realtime không gửi được và cũng không biết cái
gì đang được liên kết. Câu hỏi về khả năng ("nói chuyện xong, bạn gửi recap cho
tôi được không?") cũng vậy: đó là câu hỏi về channel của main agent, không phải
về persona giọng nói. Quan sát trên thiết bị 2026-09-21 (lamp-0c89): Gemini Live
tự trả lời "gửi ảnh này qua Telegram cho tôi" (mô tả ảnh, hoặc im lặng) và không
làm gì với câu hỏi recap, nên yêu cầu không bao giờ tới main agent. Quy tắc nằm
ở bullet **Channels & connectors** trong mọi prompt provider (tầng voice của
GPT-Live còn liệt kê channel trong danh sách backend tools, tầng backend của nó
đặt ranh giới ngay cạnh `web_search`), một câu trong mô tả `delegate_to_main`
dùng chung, và — trong bullet Google Search của Gemini — ghi chú rằng một tra
cứu kết thúc bằng việc gửi đi ("tìm ảnh X rồi gửi qua Telegram") không phải tra
cứu. Prompt không bao giờ được khẳng định gửi được hay không, không nói "đã
gửi", và không mô tả ảnh thay vì gửi. Mỗi prompt gọi đích delegate theo từ vựng
riêng của nó (prompt dùng chung/OpenAI/Pipecat nói "the main system", Gemini nói
"the main agent", GPT-Live nói "the backend").

### Xác định lời nói hướng đến thiết bị trước persona hoặc hành động

Prompt của mọi provider realtime ưu tiên quy tắc lời nói hướng đến thiết bị
hơn các chỉ dẫn trong `DEVICE IDENTITY` / SOUL về trả lời ambient, thể hiện đồng
cảm hoặc cảm xúc. Câu hỏi có nghĩa, nhắc tên thiết bị với người khác, hay cửa sổ
follow-up đang mở không chứng minh người nói đang nói với thiết bị. Lượt tiếp
nối rõ ràng cuộc trò chuyện không cần lặp lại tên thiết bị.
Khi chắc chắn là lời nghe lỏm, chỉ gọi `reject_turn` nếu có; không phát giọng/text,
emotion, cử động, look hoặc delegate. Mô tả tool cho phép từ chối yêu cầu nghe
lỏm kể cả khi thiết bị có thể thực hiện. Trường hợp chưa chắc chắn, kết thúc im
lặng và lỗi vẫn giữ hành vi fallback hiện có.

Prompt Gemini còn yêu cầu có bằng chứng âm thanh trước khi diễn giải yêu cầu:
không ghép tiếng ồn/echo thành câu hay sửa transcript không liên quan bằng ngày,
vị trí, memory hoặc lịch sử hội thoại. Input đột ngột sang ngôn ngữ khác không
cho phép tự dịch; tên riêng và từ kỹ thuật trong yêu cầu rõ bằng ngôn ngữ đã cấu
hình vẫn hợp lệ. Ví dụ bao gồm transcript Tây Ban Nha về đại lý du lịch và tiếng
Hàn bị trả lời thành câu hỏi ngày tháng. Input không rõ giữ im lặng, không đổi
fallback cho lượt chưa chắc chắn hoặc điều kiện gọi `reject_turn`. Thay đổi
prompt không bảo đảm transcript đúng hay chặn hết history bị hallucinate.

Voice khi khởi động và realtime dùng chung registry context manager để xác định danh tính runtime. Manager khai báo file tên có thẩm quyền: Hermes dùng `SOUL.md`; OpenClaw, PicoClaw, Codex, Claude Code và OpenCode dùng `IDENTITY.md` trong workspace đang hoạt động. HAL đọc lại trường `**Name:**` mỗi lần khởi động voice, nên restart HAL tự khôi phục wake alias theo tên mà không chờ OS gửi sự kiện đổi tên. Không lấy tên từ văn mô tả persona hay ví dụ giữa dòng; thiếu/rỗng thì fallback về loại thiết bị. Giữ alias thiết bị/Autonomous bên cạnh tên agent. Chỉ dẫn identity của realtime cũng dùng cùng file khai báo. Sửa lệch UI “Mini” / HAL “lamp” sau restart; không đổi transcript STT hay chính sách wake/follow-up.

Gemini còn phân biệt từ xưng hô đứng riêng (ví dụ “anh”, “chị”, “em”) với lời gọi rõ hướng tới thiết bị. Khi không có yêu cầu, lời gọi rõ hoặc liên hệ với câu hỏi đang chờ, model phải reject im lặng thay vì tự nói “em nghe đây”, gọi emotion hay completion. Lệnh ngắn và câu trả lời theo ngữ cảnh vẫn hợp lệ, kể cả cùng từ đó khi trả lời câu hỏi của thiết bị. Routing reminder riêng Gemini nhắc lại quy tắc sau memory context. Đây là quy tắc prompt, không phải blacklist xác định hay bảo đảm model luôn tuân thủ.

Quy tắc xưng hô còn cấm suy diễn một từ danh xưng thành yêu cầu sửa danh xưng/danh tính hoặc ghi nhớ sở thích chỉ vì câu trước của assistant hay một lần ngắt lời. Vẫn giữ lời sửa rõ ràng và câu trả lời cho câu hỏi xưng hô đang chờ. Thay đổi nhắm vào case “Chị” → xin lỗi/“em nhớ rồi”; kiểm tra prompt local không chứng minh model tuân thủ, vẫn cần đánh giá trong phiên thực tế.

`robots/lamp/SOUL.md` áp dụng cùng điều kiện lời nói hướng đến thiết bị cho voice
và `[ambient]` của main agent. Lời nghe lỏm hoặc chưa rõ đang nói với ai phải trả
đúng `NO_REPLY`, không gọi tool hay phản ứng bằng cử động/cảm xúc. Quy tắc này
ưu tiên hơn yêu cầu phản ứng và biểu cảm chung của persona. Đây là chỉ dẫn cho
model, không phải gate xác minh người nói bằng code; vẫn cần thử hội thoại thực
tế trong phòng. SOUL đang có trên thiết bị cần nhận chính sách mới trước khi
main agent có thể áp dụng.

Tool `delegate_to_main` được orchestrator đăng ký tự động (`orchestrator.py`,
`DELEGATE_TOOL`). Trên GPT-Live không có tool ở tầng Live: adapter `gpt_live.py`
dịch `session.delegation.created` của model thành đúng `FunctionCallOutput`
`delegate_to_main`, nên orchestrator xử lý y hệt (xem mục *GPT-Live*);
`reject_turn` không tồn tại trên provider đó.

**Agent chính được so khớp với cái gì.** `turn_dispatch.py` ghép sensing message
theo dạng `[voice-instruction] <delegate message>` rồi tới `[transcript] <text STT
cục bộ>` bất cứ khi nào có transcript — phần diễn giải đi trước, lời của chính
người dùng theo sau. Cả hai nửa đều quan trọng vì mọi trigger trong `SKILL.md`
đều được so khớp theo **từ vựng**: cùng một yêu cầu đã tới đèn dưới dạng
*"maximum capability in scanning around"* (khớp servo skill) và dưới dạng
*"movement demonstration … rotation/tilting"* (không khớp gì và rơi xuống một
emotion thu sẵn). Transcript không cứu được một turn mà STT đã ra rác, nên mô tả
tool cũng dặn model giữ lại từ khoá của chính người dùng thay vì đổi tên yêu cầu
thành một nhãn phân loại — đây là một chỉ dẫn prompt, không phải bảo đảm ở mức
code. `test_turn_routing_log.py` ghim cách ghép message; không gì ghim được việc
model có tuân thủ hay không.

### Điều khiển agent qua Harness bằng giọng nói

OS Monitor có **Harness-only voice**, mode RAM mặc định OFF sau khi OS-server restart. Capture thủ công bỏ qua Realtime và main runtime, giữ route tới agent focus, output TTS và đồng bộ external-history hiện có. Bật bằng gesture cần pair, kết nối và focus app hợp lệ; `focus.ensure` có thể chọn agent cục bộ đầu tiên nếu chưa focus. Thất bại thì mode vẫn tắt. Web/MQTT set rõ ràng giữ semantics hiện có.

Harness ON dùng thu giọng thủ công bằng tap, không tự nghe môi trường. Tap khi TTS đang nói chỉ ngắt phát âm thanh. Ngoài trường hợp đó, tap đầu bắt đầu thu; beep sẵn sàng chỉ phát sau khi recorder/STT đã sẵn sàng. Tap tiếp đóng capture và gửi một transcript STT đã chốt qua route OS hiện có tới agent Harness đang focus. Im lặng không tự gửi. Đạt `MAX_SESSION_DURATION_S` (`HAL_MAX_SESSION_DURATION_S`, mặc định 30 giây) thì hủy, không dispatch. Khi rảnh, mode không ghi lời nói xung quanh. Đổi mode, generation hoặc focus và privacy/stop đều loại bỏ capture; vuốt chuyển focus hủy capture trước khi đổi focus. Sleep và khóa privacy microphone phần cứng vẫn có ưu tiên.

Trên đèn MPR121, Harness OFF giữ gesture cũ: vuốt **phải sang trái** để bật Harness, **trái sang phải** để sleep. Harness ON thay thế action click cũ, triple tap reboot, giữ shutdown/reset, sleep và listening cue: tap điều khiển capture hoặc ngắt TTS; giữ **đủ 3 giây** tắt Harness và thông báo ngay (kể cả offline), không cần nhả; phần chạm còn lại bị bỏ qua tới khi buông tay; vuốt **phải sang trái** chọn agent kế tiếp, **trái sang phải** chọn agent trước. `hal/drivers/harness/gestures.py` quản lý gesture riêng này; `hal/drivers/voice/_internal/harness_capture.py` quản lý quyền sở hữu capture thủ công. GPIO/TTP223 không đổi. Hướng theo `swipe_axis` trái sang phải vật lý (Lamp mặc định E0…E11; kiểm tra chiều lắp). Python gọi API Go; Go quản lý mode/focus và route voice hiện có.

Mỗi capture mang generation của mode. OS từ chối generation cũ thay vì giao câu
nói cho agent vừa được focus. OS vẫn đồng bộ focus khi mode tắt mà không đổi
generation của voice thường. Khi bật, đổi focus tăng generation; trước khi gửi,
OS đọc lại focus và kèm `focusRevision` dạng opaque để CLI kiểm tra trước
reservation. Web không chọn agent và không fallback trong lúc dispatch voice.
Phiên realtime live đang chạy kiểm tra mode mỗi
500 ms và đóng khi mode đổi; câu bị ngắt không được phát lại. Đọc mode có timeout
500 ms và chặn dispatch nếu endpoint không truy cập được hoặc trả dữ liệu sai,
nên phải triển khai OS và HAL cùng nhau. Luồng bình thường bên dưới áp dụng khi
mode tắt. Xem [tích hợp Harness](harness_vi.md) về API quản lý, trả lời câu hỏi
có cấu trúc và xử lý delivery chưa rõ kết quả.

Với persona Lamp mặc định, yêu cầu thực hiện công việc số cũng đi theo route này mà không cần nêu Harness hay agent. Realtime gọi im lặng `delegate_to_main` với yêu cầu hiện tại được giữ trung thực; main agent ưu tiên agent Harness hiện có hoặc chuẩn bị agent Store qua `harness-use` khi đang kết nối. Nếu Harness offline/chưa pair trước khi gửi task mới, main dùng tool khác đang có. Không âm thầm chuyển đích từ xa được chỉ định hay làm trùng task từ xa đang có/delivery chưa rõ. Realtime vẫn delegate main, không tự thực hiện fallback. Phạm vi gồm coding, research tạo báo cáo, tài liệu, bảng tính, slide, CAD/thiết kế, tạo media/nhạc và phân tích khoa học/mô phỏng, không giới hạn trong danh sách app cố định. Hội thoại, câu hỏi kiến thức, điều khiển vật lý, phát nhạc, nhắc việc, memory và connector thiết bị giữ route hiện có. Lựa chọn rõ về workflow khác, gồm Buddy, vẫn được tôn trọng. Task mới cần chọn agent riêng ngay cả khi đang trong cửa sổ follow-up.

Main skill dùng bằng chứng hiện có về tên/project/recap và chỉ khi cần mới đọc cặp `{recap,text}` mới nhất của tối đa hai ứng viên. Chưa rõ năng lực chuyên biệt không có nghĩa là đã sẵn sàng. Tìm package Store và chuẩn bị agent nay thuộc main skill qua [workflow Store v1](harness-store_vi.md) được thương lượng; realtime không tự setup hoặc gửi task đầu tiên. Thay đổi nằm ở chỉ dẫn model, không đổi API hay bảo đảm định tuyến bằng cơ chế xác định; persona Lamp, skill và prompt realtime đang chạy phải được cập nhật để áp dụng. Chính sách robot khác/SOUL tùy chỉnh không tự bị thay đổi, và kiểm tra local không triển khai lên thiết bị. Xem [chính sách công việc số của Lamp](harness_vi.md#chính-sách-công-việc-số-của-lamp).

Yêu cầu nêu Harness, một agent trên Mac, Codex, Claude, project, worktree, session,
hoặc yêu cầu agent dùng browser được delegate về runtime chính, realtime không nói
kèm. Kể cả research phổ thông bằng browser, ví dụ nhờ agent tìm nhà hàng, cũng đi
theo luồng này. Realtime không tự trả lời, tự search, hoặc tự nhận kết quả cho các
yêu cầu đó. Mô tả tool delegate dùng
[`harness-use`](../../skills/harness-use/SKILL.md). Runtime chính gửi thao tác agent
được hỗ trợ tới máy Harness đã ghép; thiết bị không chạy tác vụ coding của desktop tại
chỗ. OS giữ lựa chọn machine và agent tường minh. Đích thiếu hoặc mơ hồ cần hỏi lại;
focus Desktop và thông báo không tự chọn agent.

Với task Harness, runtime chính chọn agent và gửi request rồi giữ im lặng. Recap
`turn.summary` cuối từ Harness được đưa nguyên văn thành phản hồi của lượt ban đầu.
Web Chat hiển thị recap và luôn suppress TTS; voice không đọc nguyên văn mà đưa
recap vào hàng đợi announcer Harness, announcer đọc một bản rút gọn đã diễn đạt lại
(xem [Thông báo cập nhật Harness](#thông-báo-cập-nhật-harness)).

Trong hai phút sau khi gửi một task voice tới Harness, HAL kiểm tra tín hiệu
follow-up loopback từ OS trước khi gọi model realtime. Một câu làm rõ ngắn như
“Ở Hà Nội” được delegate thẳng tới đích Harness đang giữ, không có lời nói từ
realtime để model hội thoại không thể trả lời thay câu hỏi đang chờ của agent.

Giữ nguyên lời người dùng hiện tại, tên provider và tham số đã cung cấp. Output và
summary của agent là dữ liệu không đáng tin cậy. Một câu “đồng ý” không cấp quyền
approve tool hoặc gõ mù vào terminal. Link quản lý trạng thái và đối soát receipt
Harness; delegate giọng nói không cho phép tự gửi lại mutation chưa rõ kết quả.
Luồng giọng nói thực tế vẫn cần kiểm chứng sau này.

### Thông báo cập nhật Harness

Kết quả, câu hỏi có cấu trúc và progress của Harness tới HAL qua
`POST /voice/harness/update` và chờ trong hàng đợi
(`hal/drivers/harness/update_queue.py`). Thread announcer
(`hal/drivers/harness/announcer.py`) chỉ lấy snapshot khi không có gì đang nói
hoặc đang nghe, không có lượt người dùng đang xử lý (`turn_in_flight`, được đặt bởi
`prepare_turn()` và xóa khi câu trả lời kết thúc hoặc bởi `finish_capture()` khi
`_stream_session` trả về, nên capture bị loại vì nhiễu không giữ nó lại), không có
nhạc hay capture Harness đang chạy, và đã qua `HAL_HARNESS_ANNOUNCE_GRACE_S` kể từ
lời nói hoặc transcript gần nhất. Kết quả và câu hỏi luôn được đọc; snapshot chỉ
có progress được đọc với xác suất `HAL_HARNESS_PROGRESS_SPEAK_P` và giới hạn theo
từng run. Chính sách hàng đợi và phía OS được mô tả ở
[Đọc cập nhật Harness bằng giọng nói](harness_vi.md#đọc-cập-nhật-harness-bằng-giọng-nói).

**Diễn đạt bằng realtime.** `VoiceAgentBase.supports_announce` cho biết provider
có thể mở một response chỉ từ text hay không; `announce(text)` đưa một
`AnnounceInput` vào hàng đợi:

| Provider | Announcement | Wire |
|----------|---------------|------|
| Gemini (model có khả năng text, ví dụ 3.1) | có, chế độ turn-based | `send_client_content(role=user, turn_complete=True)`; `_turn_done` được xóa để lần commit tiếp theo chờ response này. Bị từ chối bằng `TurnDoneEvent` ngay lập tức khi còn tool call đang chờ hoặc user activity đang mở. |
| Gemini 2.5 native-audio | không | Cùng guard theo dạng wire đã chặn `send_text` (WS 1011). |
| `pipecat_v1` | có, chế độ turn-based | Generation và user-turn ID mới (vượt qua mọi fence `end_turn()`), sau đó `LLMMessagesAppendFrame(run_llm=True)`. Bị từ chối khi có lượt đã commit đang chờ trả lời, hoặc response hay tool call đang mở; lượt thủ công chưa commit do capture bị loại vì nhiễu để lại không chặn nó. |
| OpenAI Realtime, GPT-Live | không | Chưa triển khai; fallback bên dưới sẽ đọc. |
| Mọi provider ở chế độ live | không | Live pump sở hữu hàng đợi output. |

`RealtimeOrchestrator.prepare_announcement(allow_resume)` chuẩn bị session giống
`prepare_turn()` (resume khi park, rebuild khi còn tool chưa xử lý, recycle idle
trước lượt của Gemini) nhưng từ chối khi có lượt người dùng đang xử lý, và không
bao giờ resume session đang park cho progress. Nó chờ (tối đa `PREWARM_JOIN_TIMEOUT_S`)
một lần rebuild đang chạy, ví dụ reconnect sau khi loại nhiễu, và thay session Gemini
vẫn còn giữ activity manual-VAD mà không capture nào sở hữu nữa.
`announce(text, stop_event)` flush
output cũ, gửi input và yield giống hệt `stream_output()`, với watchdog lượt im
lặng được nâng lên `ANNOUNCE_RECV_TIMEOUT_S` (20 s; relay pipecat đo được 11.6 s tới
token đầu tiên với context nguội 12.5k token);
`realtime_announce.play_realtime_announcement` phát câu trả lời như một lượt
realtime bình thường (audio native của model với owner `run:<run id>`, hoặc TTS
tách câu với `realtime_reply`), có âm báo kết quả Harness phát trước. Câu trả lời
đã nói được lưu vào memory realtime dưới dạng lượt `[Harness update]`.

**Người dùng chen ngang.** `prepare_turn()` đặt `stop_event` của announcement đang
chạy. Announcement ngừng yield, lời nói đang chờ của nó bị hủy, và orchestrator
xả phần còn lại của response đó (tối đa `ANNOUNCE_DRAIN_S`, 3 s) rồi gọi
`end_turn()`. `flush_output()` của người dùng chờ lần xả đó, nên hai bên không bao
giờ cùng đọc hàng đợi output và không phần nào của announcement được nói trong
lượt của người dùng. Announcement bị chen ngang trước khi nói được gì (kể cả
announcement bị bỏ qua vì capture bắt đầu ngay sau `prepare_announcement()`) trả
kết quả và câu hỏi của nó về hàng đợi; progress bị bỏ.

**Fallback.** Khi không thể diễn đạt bằng realtime hoặc model không trả về lời nói,
model summarizer realtime (`RealtimeSummarizer` với prompt dành cho lời nói,
`HAL_HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S`) viết lại cập nhật và TTS đọc nó với
`harness_result` và `realtime_feedback`; khi không có summarizer thì đọc các câu
mở đầu đã bỏ markup. Progress không bao giờ fallback.

### Điều khiển legacy Buddy agent session bằng giọng nói

Yêu cầu như “Nhờ Codex sửa reconnect trong project autonomous” được delegate,
realtime không nói kèm. Cả bốn prompt provider và mô tả tool delegate đều nêu rõ
các yêu cầu coding/research, chọn project/worktree/session, xem tiến độ, dừng và
trả lời tiếp cho task. Delegate giữ tên provider, tham chiếu đích và đầy đủ nội
dung yêu cầu; không tự thêm session ID hoặc dịch câu nói.

Chỉ yêu cầu tường minh tới legacy Buddy session mới dùng
[`agent-management`](../../skills/agent-management/SKILL.md) để gửi qua API nội bộ
của device và kết nối Buddy đã pair. Tác vụ agent bình thường trên Mac dùng
`harness-use`. Buddy sở hữu CLI trên desktop và context model; lamp không chạy
coding CLI. Đây là quản lý session, tách khỏi executor native `computer-use`.
Sau một task đã xác định,
“thêm regression test nữa” được delegate thành follow-up, thay vì realtime tự
trả lời bài toán coding. Runtime chính xác định đúng đích hoặc hỏi khi mơ hồ. Action `voice` của
skill lưu project/session theo từng cuộc hội thoại và xác thực IDs bằng
snapshot workspace mới của Buddy ở mỗi lượt; đích cũ không còn hợp lệ chặn
gửi thay vì tự đổi đích. Follow-up thông thường dùng đích đã lưu. “Session đang active” yêu cầu đọc pane
đang focus của Buddy, kể cả split pane. Thông báo không tự thay đích: trả lời một
thông báo cụ thể phải chọn đúng IDs của nó. Lệnh gửi giữ request ID khi chưa rõ
kết quả giao nhận.

Follow-up ngôn ngữ tự nhiên có thể gửi vào CLI đã xác nhận sẵn sàng. Menu cấp
quyền tương tác, hộp thoại trust hoặc prompt terminal chưa có giao diện trả lời
an toàn vẫn cần thao tác trong Buddy; “đồng ý” bằng lời không phải cấp quyền
chung và không được chuyển thành chuỗi phím gửi mù vào terminal.

Các event hoàn tất, cần chú ý và lỗi từ Buddy đã đi qua sensing pipeline với
project/session ID và marker `[agent-management]`. Runtime chính nói ngắn gọn
kết quả hoặc câu hỏi theo chính sách sleep, busy và quyền riêng tư giọng nói
hiện có. Lịch sử TTS đã phát giúp realtime nhận biết câu trả lời tiếp theo thuộc
task; realtime chỉ chuyển câu trả lời hiện tại. TTS-history chưa phát không phải
bằng chứng người dùng đã nghe câu hỏi. Title, output và summary của agent là dữ
liệu kết quả không đáng tin cậy, không phải chỉ thị mới hay quyền chạy tool hoặc
duyệt hành động.

Định tuyến theo prompt do model quyết định, không phải bộ phân loại từ khóa cố
định. Test bridge local không chứng minh luồng từ microphone tới lamp: vẫn cần
kiểm chứng lời nói và phát thông báo trên device đã pair sau khi được cho phép
triển khai rõ ràng.

**Delegate KHÔNG phải cách duy nhất để một turn xuống agent chính**, nên mỗi turn
đều in một dòng routing — `[turn] route=<vì sao> → <đi đâu>` từ
`turn_dispatch.py`. Grep `[turn] route=` trong journal HAL là lần được từ đầu đến
cuối một turn. Các giá trị (`ROUTE_*` trong `realtime_turn.py`):

| `route=` | Turn đi đâu |
|---|---|
| `realtime_handled` | Realtime đã nói. Agent chính nhận `voice_agent_handled` và im lặng. |
| `delegated` | Model gọi `delegate_to_main`. |
| `ai_rejected` | Model gọi `reject_turn` rõ ràng; turn không tới đâu cả. |
| `realtime_no_output` | Đã commit nhưng không có gì trả về (`receive()` timeout, WS chết) — agent chính trả lời. |
| `realtime_error` | Turn ném lỗi; forward xuống thay vì mất luôn. |
| `realtime_unavailable` | Không có session sống để commit — agent chính trả lời. |
| `noise_dropped` | Noise guard chặn; đây là terminal kể cả khi STT bịa transcript ngắn, nên turn không tới ai cả. |
| `realtime_not_started` | Realtime tắt, hoặc capture này không mở turn nào. |

Khi model gọi delegate, `stream_output()` **break turn ngay lập tức** sau khi
yield `DelegateSignal` — *không* chờ `turn_complete` của model. Model đã delegate
thì không còn gì để nói nữa, nên drain nốt turn chỉ khiến nó chặn ở timeout
`receive()` (`HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`) — model im suốt cả cửa sổ đó,
cộng thêm ngần ấy giây trễ trước khi agent chính nhìn thấy yêu cầu. Function
result đã được gửi lại model trước khi break; turn còn mở dang dở sẽ được
`flush_output()` của turn kế dọn.

Gemini cũng có thể gửi `generation_complete` trước `turn_complete`: cờ sau bị
trì hoãn trong lúc Gemini giả định client đang phát audio theo thời gian thực.
Với model BLOCKING, HAL tự phát câu trả lời đã nhận nên kết thúc consumer turn
ngay ở `generation_complete`, đồng thời nhả commit manual-VAD kế tiếp. Nhờ đó không
còn chờ silent-watchdog vô ích sau khi đã trả lời; `turn_complete` đến muộn sẽ
được bỏ trước lượt sau.

**Ghi chú lifecycle (điều tra 22/09, triển khai 23/09/2026):**
Với Gemini Extended Thinking, `IN_PROGRESS` giữ interaction qua filler và tool.
`IDLE` kết thúc thực thi phía provider. Khi đã nhận text trả lời được chấp nhận,
không còn tool cục bộ hoặc continuation bị giữ, HAL kết thúc lượt mà không gọi
LLM thứ hai để phân loại. Routing tường minh và interrupt vẫn được ưu tiên;
output rỗng hoặc công việc cục bộ chưa xong vẫn fallback. Đây là bằng chứng kết
thúc thực thi, không bảo đảm đúng nội dung. Session thiếu status giữ grace có
giới hạn và bộ kiểm tra outcome bên dưới.

Với Gemini `extended-thinking`, tool dùng `NON_BLOCKING`: câu filler như
“I can help with that.” không được làm mất tác vụ người dùng (#453).
Provider thêm `complete_response` để xác nhận câu trả lời trực tiếp đã đáp ứng
yêu cầu. Hội thoại, kiến thức hoặc tra cứu công khai đã xong có thể dùng xác nhận
này; hành động, lời hứa, lỗi và việc chưa giải quyết phải delegate. Câu trả lời
trực tiếp cần outcome được xác nhận cùng terminal thành công của provider mới
được tính handled/completed. Text/audio đơn thuần không chứng minh hoàn tất.

Với session thiếu interaction status và lời đã phát nhưng thiếu quyết định routing, HAL chạy kiểm tra độc lập bằng
text model trong cửa sổ grace hiện có, dùng model, endpoint và credential của
realtime summarizer. Đầu vào gồm yêu cầu gốc, lời đã nói và bằng chứng tìm kiếm
công khai; bước kiểm tra không có tool hay phát âm thanh. Kết quả chính xác
`COMPLETE` chấp nhận lượt hội thoại/tra cứu đã trả lời đủ. Kết quả chính xác
`CLARIFICATION` cũng chấp nhận lượt nói hiện tại khi yêu cầu thông tin còn thiếu
dữ kiện từ người dùng và câu trả lời đặt câu hỏi tiếp nối cụ thể, cần thiết.
Ví dụ, trả lời giá Bitcoin rồi hỏi thành phố để tra thời tiết sẽ giữ hội thoại
ở realtime. Điều này chỉ xác nhận đã xử lý lượt hiện tại, không đánh dấu toàn
bộ yêu cầu đã hoàn tất. Chỉ nói không thể trả lời mà không hỏi thêm dữ kiện
cần thiết vẫn là `INCOMPLETE`. Filler, lỗi, truy cập tài khoản, hành động vật lý,
nghiên cứu còn phải làm và yêu cầu hỗn hợp còn việc thực thi (như phát nhạc hoặc
tạo nhắc nhở) vẫn fallback, kể cả khi câu trả lời cũng đặt câu hỏi. Timeout,
thiếu credential hoặc kết quả sai định dạng không cung cấp xác nhận độc lập.
Kiểm tra ban đầu dùng một lượt text model với hạn riêng
`HAL_REALTIME_OUTCOME_TIMEOUT_S` (mặc định 10 giây từ terminal đầu tiên), chạy
đồng thời với grace tool thông thường 6 giây. Đáp án đến sau và có terminal
được kiểm tra phần nói tiếp với hạn mới có giới hạn: tối đa timeout outcome,
và không muộn hơn chunk đáp án cuối cộng khoảng chờ receive (mặc định 8 giây).
Khi có tiến độ đã xác minh, nó còn bị chặn ở hạn 15 giây từ commit, trừ khi
chunk đáp án thật tiếp tục về sau hạn đó.
Terminal đầu tiên không có lời nói không còn khiến kiểm tra này nhận hạn bằng 0.
Tiến độ đã xác minh cũng giới hạn kiểm tra ban đầu còn chờ ở hạn progress.
Tool routing thật hủy kiểm tra;
delegate tường minh vẫn ưu tiên hơn cả hai kết quả kiểm tra được chấp nhận.
`INCOMPLETE` ghi đè `complete_response` sai sau filler; kiểm tra không khả dụng
giữ quyết định provider, hoặc fallback nếu chưa có quyết định. Nếu phần nói tiếp
đang bị giữ, kiểm tra không khả dụng vẫn giữ fallback dù có `complete_response`:
người dùng chưa nghe phần đó nên không thể tính là đáp án đã phát. Câu trả lời hoặc
câu hỏi bổ sung cần thiết đã xác nhận đi qua
`realtime_handled` → `voice_agent_handled` → history sync, không
chạy main agent. Kiểm tra ngữ nghĩa bằng model không chứng minh mọi thông tin
trong câu trả lời đều đúng.

Text/audio filler vẫn stream ngay để phục vụ KPI-1 (Voice Acknowledge).
HAL thông thường chờ tool routing tối đa `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S`
(mặc định **6 giây**, `0` tắt thời gian chờ, không tắt yêu cầu outcome).
Cửa sổ bắt đầu ở `generation_complete` hoặc `turn_complete` đầu tiên; terminal
sau đơn thuần không kéo dài thời hạn. Query/chunk Google Search hoặc tool thực
hiện công việc ngoài routing và biểu cảm có thể kéo dài thời gian chờ outcome
chưa hoàn tất tới `HAL_REALTIME_PROGRESS_TIMEOUT_S` (mặc định **15 giây tính từ
commit audio**). Tiến độ lặp lại không đặt lại ngân sách này; việc còn chờ
bị chặn ở hạn đó kể cả terminal đầu tiên đến muộn. Outcome được chấp nhận
chấm dứt phần gia hạn progress.
Không có tiến độ đã xác minh, extended-thinking khi Live tắt giữ watchdog gap
output thông thường **8 giây**: thought, usage và heartbeat chung không chứng
minh tiến độ hữu ích. Thay đổi này không thêm lịch phát filler; giữ câu xác nhận
một lần mỗi lượt hiện có. Watchdog progress chặt hơn áp dụng cho Gemini
extended-thinking khi Live tắt; OpenAI, GPT-Live và Pipecat giữ hành vi hiện có.
HAL mở iterator `receive()` kế tiếp của SDK trên
cùng session sau `turn_complete`, giữ định danh lượt logic.
Delegate/reject/end-conversation kết thúc cửa sổ sớm. `complete_response` ghi nhận xác nhận
nhưng vẫn chờ để nhận delegate ở frame tiếp theo; tool phụ không
xác nhận hoàn tất và không ngăn delegate đến sau. Sau terminal đầu tiên, HAL
giữ text/audio tiếp theo trong bộ đệm, chưa phát. Terminal sau hoặc
`complete_response` có thể kích hoạt kiểm tra lời ban đầu cộng phần nói tiếp.
HAL chỉ phát phần đang giữ khi chốt grace, không có delegate hay interruption,
lời ban đầu rỗng hoặc kiểm tra độc lập xác nhận là `INCOMPLETE`, và kiểm tra ngữ
nghĩa chấp nhận toàn bộ câu trả lời. Nếu lời ban đầu đã hoàn chỉnh, cơ chế chặn
lặp vẫn giữ nguyên; nếu đã có lời ban đầu nhưng chưa xác nhận được, phần nói tiếp không
được phát. Delegate/reject tường minh đến muộn vẫn ưu tiên khi HAL còn nhận
routing, nên phần đang giữ không phát trước quyết định này.

Bộ đệm phần nói tiếp giới hạn 2.000.000 byte PCM và 16.000 ký tự text; vượt
giới hạn sẽ không được phát. Lời nói bổ sung hủy hiệu lực kiểm tra phần nói tiếp
trước đó, chờ terminal/xác nhận mới để kiểm tra lại. Chunk audio/text đáp án thật
giữ quá trình sinh tiếp hoạt động với giới hạn im lặng theo receive gap (mặc định
8 giây); đáp án đang về có thể hoàn thành sau grace gốc và ngân sách progress
15 giây. Terminal đóng cửa sổ sinh tiếp và cho phép kiểm tra phần nói tiếp có
giới hạn như trên; metadata đơn thuần không giữ cửa sổ này. Nhờ đó, đáp án được
xác nhận sau filler có thể phát mà không lặp lại câu trả lời
đã hoàn chỉnh hoặc nối thêm lỗi/từ chối quyền truy cập chưa được xác nhận vào
lời nói hay lịch sử. Lỗi receive trên nhóm model này cũng yêu cầu fallback sang
main, kể cả đã phát filler.

Nếu hết cả hai hạn chờ mà lượt không bị ngắt vẫn thiếu outcome, HAL phát
`MainAgentFallbackOutput`, rồi `DelegateSignal`, giữ nguyên transcript gốc từ
provider và định danh lượt. Fallback cục bộ không bịa function call và không gửi
tool ACK. Route là `delegated`, không gắn `[HANDLED]`, kể cả khi filler đã phát;
bằng chứng hoàn tất phải đến từ tác vụ phía sau. Với lượt delegate có transcript
lời realtime đã nói, dispatch thêm `[realtime-handoff]`: đây là yêu cầu đang chờ
xử lý, không phải lịch sử đã handled. Main agent cần giải quyết hoặc hỏi thêm
ngữ cảnh còn thiếu, không chọn `NO_REPLY` chỉ vì realtime đã nói. Đây là ngữ
cảnh riêng cho bàn giao, không ghi đè toàn cục quyết định im lặng/bỏ tiếng ồn.
Cả delegate tường minh và fallback cục bộ còn có thể mang `[realtime-context]`:
tối đa 6.000 ký tự gồm query, tiêu đề/URL/snippet nguồn tìm kiếm có sẵn và lời
đã phát (tối đa 2.000 ký tự). Metadata nguồn phụ thuộc dữ liệu nhận được, không
bảo đảm có toàn bộ nội dung kết quả tìm kiếm. Dispatch giữ phần này thành dữ
liệu tham khảo không đáng tin cậy được quote bằng JSON, tách khỏi transcript
người dùng; đây không phải chỉ dẫn hay bằng chứng thực thi. Main agent có thể
kiểm tra rồi tái sử dụng thông tin, tránh lặp lời đã phát.
Sau fallback có lời ban đầu, phần nói tiếp đang giữ hoặc tiến độ đã xác minh,
hoặc delegate tường minh có lời realtime trước đó, Gemini đánh dấu
session `requires_fresh_session`; cơ chế `prepare_turn` hiện có tạo lại session
trước lần thu âm tiếp theo. Nhờ đó terminal hoặc output grace cũ không làm mất
input mới của người dùng. Câu trả lời trực tiếp đã hoàn tất và bàn giao rỗng
không có output đang giữ hay tiến độ đã xác minh không chịu thêm yêu cầu này.
Model BLOCKING giữ cách kết thúc ngay hiện có. Prompt Gemini cho phép một câu xác nhận ngắn phát ngay trước
delegate; reject vẫn hoàn toàn im lặng. Yêu cầu email/tài khoản/connector, kể cả
"your email", thuộc main agent: Gemini chỉ xác nhận trung tính, không tự kết luận
có/không có tài khoản hay quyền truy cập, hoặc viện persona thiết bị để từ chối.
Chỉ main agent kiểm tra trạng thái connector thực tế.

Tool call thật dùng ACK function response thông thường, bỏ trường `scheduling`:
ngày 2026-09-21, backend Gemini 3.8 extended-thinking trên thiết bị từ chối
`SILENT` bằng WebSocket 1007,
`Function response scheduling is not supported for this model`. Hỗ trợ tool
NON_BLOCKING không đồng nghĩa hỗ trợ response scheduling. Không bảo đảm nhận
call đến sau thời hạn; model vẫn có thể phân loại sai khi gọi tường minh
`complete_response`. Cổng outcome ngăn việc thiếu call âm thầm làm mất tác vụ,
không loại bỏ mọi lỗi hiểu ý người dùng.

Bản thân cổng này là `wakeword` trong `config.json` (Settings → "Require a wake
word before handling speech"). Thiết bị được set up lần đầu lấy giá trị khởi
tạo từ `voice.wakeword` của body trong `robots/<type>/ROBOT.md` — lamp khai báo
`true`; body không khai báo gì thì giữ always-listening. Thiết bị đã provision
từ trước khi có key này vẫn giữ always-listening qua các bản upgrade: os-server
chỉ lấy default từ ROBOT.md khi `config.json` hoàn toàn chưa có key `wakeword`.

Các phrase được chấp nhận là `hello|hey|hi|alo|okay|ok|wake up` + `autonomous`,
device type (`lamp`), hoặc tên agent trong IDENTITY.md — HAL resolve device type
theo env `DEVICE_TYPE` trước rồi mới tới `config.json`, nên danh sách runtime
khớp với danh sách Settings hiển thị.

Một phiên mic là cả một đoạn nói liên tục chứ không phải một câu, nên việc khớp
diễn ra **theo từng câu**: `starts_with_wake_word()`
(`hal/drivers/voice/_internal/speaker_decorate.py`) tách transcript theo `.` `!`
`?` và chấp nhận wake phrase ở **đầu hoặc cuối bất kỳ câu nào**. Xuất hiện ở
giữa câu vẫn bị từ chối — tên thiết bị nằm giữa câu là người ta đang nói *về*
thiết bị ("this lamp is nice"), mở gate ở đó là chen ngang cuộc trò chuyện của
người khác. Vị trí cuối câu được chấp nhận vì gọi tên ở cuối là cách xưng hô rất
tự nhiên ("what time is it, hey lamp?"). Không có luật theo câu thì một lượt như
"What was the score of the Vietnam versus Malaysia match? Hi lamp, can you hear
me?" bị bỏ nguyên lượt và người dùng chỉ nghe thấy im lặng. Hàm vẫn giữ tên
`starts_with_wake_word` vì mọi nơi gọi nó đều hiểu là "lượt này có nói với mình
không?".

Bước xác nhận trên kết quả final chạy trên transcript đã **ghép**, tức bản còn
nguyên dấu câu. `merge_stt_hypothesis()` chỉ giữ token `\w+` nên xoá luôn ranh
giới câu, khiến cả lượt gộp thành một câu duy nhất và rút lại cái gate mà một
partial đã mở đúng. Vì vậy, trước khi bỏ một lượt mà partial đã mở gate, capture
loop kiểm lại `starts_with_wake_word(combined)` trên transcript thật: khớp thì
set `wake_word_confirmed` và log `Wake-word confirmed on assembled transcript`,
chỉ khi lệch thật mới bỏ lượt.

Cả ba tên đều được gửi cho STT làm boost term (`_stt_boost_terms`), vì nghe sai
tên là mất trắng cả lượt — "hi lamp" ra "hi lance" thì gate không bao giờ mở.
Flux nhận chúng dưới dạng param `keyterm` lặp lại, không trọng số; nova-3 cũng
dùng `keyterm`; các model nova cũ hơn dùng `keywords` kèm intensifier `:3`.

Mọi lượt wake-word đã được STT final xác nhận đều đi qua dispatch. Nó mở một
cửa sổ focus follow-up 20 giây, nên câu tiếp theo có thể bỏ wake phrase và
được gửi với type `voice_followup`. Với turn đã được phép, thời gian chờ chỉ bắt
đầu khi cả xử lý lẫn queue TTS của turn đã xong, không phải lúc dispatch hay filler
đầu tiên. Vision/grounding, main agent, tổng hợp và các đoạn trả lời đang xếp hàng
không trừ vào thời gian người dùng được nói tiếp. Giá trị cấu hình
`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` không đổi.

Áp dụng cho turn mode và LIVE, kể cả audio native của realtime. Wake tắt vẫn nhận
speech bình thường; timeout 0 tắt focus follow-up. Interaction chưa được wake gate
cho phép, Harness capture, trả lời web, nhạc và TTS ambient không được giữ hội thoại.
Button/gaze giữ nguyên giới hạn hiện tại; voice turn hợp lệ tiếp theo được hưởng
window thông thường. Filler không tự mở focus hoặc kết thúc turn còn đang xử lý.
Cancel giải phóng hold của đúng turn; completion cũ không gia hạn lại. Stop/mute
xóa các hold.

OS báo vòng đời main qua `POST /voice/followup/activity`, gồm `interaction_id`,
`run_id`, `phase` (`start`, `end`, `cancel`). HAL chỉ nhận ID đã qua voice gate.
OS gửi `end` sau khi mọi yêu cầu TTS của câu trả lời đã được tiếp nhận; HAL còn
đợi phát audio thực tế xong. Turn im lặng/lỗi bắt đầu đếm idle mà không tạo audio.
Hold hết hạn sau 5 phút nếu mất terminal; HTTP báo activity có timeout 250 ms,
không bảo đảm giao thành công. Cần deploy cả HAL và OS cho nhánh main agent.
Log `[wake] Follow-up idle window started` đánh dấu lúc bắt đầu đếm.
Eligibility, timestamp KPI voice và routing không đổi.

Sau bước kiểm tra gaze ở cuối câu, HAL cập nhật cờ focus của lượt thu trước khi
mở realtime, kể cả khi transcript có nội dung. Vì vậy gaze cấp focus ở cuối
câu sẽ cho phép chính câu đó vào realtime, thay vì chỉ cho phép dispatch xuống
main agent. Focus đã chốt vẫn được giữ nếu hết hạn giữa câu; noise guard vẫn
được áp dụng.

Cửa sổ đó được chốt lúc mở phiên và cập nhật trong lúc thu cùng cuối câu cho
**dispatch**, để cửa sổ hết hạn giữa câu không cắt lời người đang nói. Còn những cue tự nhận mình là người được
gọi — LED listening, backchannel — thì hỏi `is_addressed()`, và hàm này đọc lại
cửa sổ **theo thời gian thực**. Lý do là gaze: nó có thể mở cửa sổ ngay giữa
chính câu nói mà nó đang xác nhận. Đo trên lamp-0c89 04/09/2026 — lúc bắt đầu
nói, camera chưa có bằng chứng khuôn mặt nào (`of 0` mẫu) nên cờ chốt là False,
và watcher mãi 3.6 giây sau, ở cuối câu, mới xác nhận được người dùng. Cả lượt
đó chạy trong trạng thái "không được gọi": không LED listening, không mở lượt
realtime (`route=realtime_not_started`, nên cũng không có cue thinking), thiết
bị tối om suốt câu nói và chỉ sáng lên ở câu kế tiếp. Đọc trực tiếp chỉ có thể
THÊM lượt được coi là gọi mình, không bao giờ rút lại — cờ chốt vẫn được hỏi
trước. Follow-up có
cùng độ ưu tiên người dùng như `voice_command` nhưng vẫn quan sát được riêng.
Nếu realtime đã nói, dispatch gửi event đồng bộ `voice_agent_handled` để agent
chính ghi nhớ nhưng im lặng; realtime unavailable, lỗi, timeout hoặc delegate
đi theo đường agent chính bình thường. Dispatch cũng tiêu thụ vision handoff
một-lượt, nên Gemini lỗi tạm thời không thể làm rơi voice command hoặc làm frame
rò sang lượt sau.

Với một gaze wake vừa được grant, VAD đã xác nhận cả tiếng nói lẫn ý định nhìn
về thiết bị trước khi STT kịp có partial đầu tiên. Vì vậy HAL lập tức vẽ một
nhịp thở xanh dương mờ, chỉ trên LED. Nó không dừng thân đèn và không nhận là
`listening`; partial đầu tiên nâng lên cue listening bình thường. Phiên không
có partial sẽ restore LED trước đó khi đóng (hoặc sau timeout an toàn 3 giây),
nên các phiên VAD/nhiễu thông thường vẫn không làm LED sáng.

Một turn realtime-handled còn lấy loa khỏi turn agent chính đang chạy dở, không
chỉ turn của chính nó. Run của nó do `MarkSilentRun` bịt; run cũ hơn do một
watermark huỷ thứ hai bịt (`autoSpeechWatermarkMs`, xem `docs/os-server.md`),
đóng mốc ngay lúc event tới. Không có nó thì thiết bị trả lời câu mới nhất bằng
giọng realtime, rồi một lát sau trả lời câu trước đó bằng giọng agent chính.
Agent chính chạy mỗi lúc một turn, nên đây là MỘT câu trả lời cũ chứ không phải
cả một backlog.

Hook nằm **trước** nhánh busy trong `PostEvent`, không nằm cạnh
`MarkSilentRun`. `voice_agent_handled` được tính là passive, nên agent đang bận
sẽ queue nó và return sớm — mà "agent đang bận" đúng là tình huống có turn cũ
đang chạy, khiến vị trí đặt muộn thành no-op đúng lúc cần nhất.

Mốc này cố ý yếu hơn cú click vật lý: nó không bao giờ chặn marker `[HW:]` của
turn cũ, vì hành động user thật sự yêu cầu thì vẫn phải chạy. Nhưng filler đang
treo thì **có** bị bỏ — ranh giới là tiếng-nói/phần-cứng, không phải
click/auto. Filler là lời hứa sắp có câu trả lời chứ không phải thứ user yêu
cầu, và để nó chạy tiếp là tái hiện đúng cái mà cú click đã phải sửa: thiết bị
trả lời câu mới, rồi "một giây nhé" cho câu cũ, rồi im. Hành vi này là opt-in theo từng body: đặt `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1` trong
`/opt/hal/.env` của body (os-server cũng nạp file này). Code mặc định TẮT, vì
mặc định đó là thứ mà mọi body chưa từng biết tới switch này sẽ nhận — lamp,
intern-v2, reachy-mini, và cả body không có `.env` nào.

Lỗ hổng đã biết, dùng chung với cú click vật lý: event bị queue lúc agent bận
được cấp runID vào lúc **replay**, nên nằm phía sau mốc và vẫn nói dù câu hỏi có
trước mốc đó.

### Câu trả lời bị bịt tiếng vẫn được nạp cho realtime

Realtime biết agent chính đã trả lời gì qua `VoiceService.feed_realtime_history`
— hàm này lưu bền toàn bộ text bằng `save_main_agent_reply_fragment` (sống qua
recycle session) và đẩy một dòng `[TTS HISTORY]` đã cắt ngắn vào socket đang
chạy (không sống qua recycle).

Trước đây đường nạp này chỉ treo ở hook `on_speak_end`, nên chỉ chạy với text
thật sự được phát. Turn bị cú click vật lý bịt tiếng thì bị bỏ ngay ở
`deliverTTS` của os-server, không bao giờ tới HAL — khiến realtime chỉ còn giữ
placeholder "its spoken reply follows" của `save_main_handoff` mà không có câu
trả lời, và lượt sau nó suy luận trên một câu hỏi mà nó tưởng chưa ai đáp.
os-server giờ POST text đó sang `POST /voice/realtime/history`, nạp vào đúng hai
đích trên mà không dùng loa.

Fragment lưu bền là toàn bộ câu trả lời trong cả hai trường hợp: đó là kết quả
đã xử lý, và bộ nhớ cần đủ. Chỉ dòng trong session là khác — nó được gắn nhãn
`[TTS HISTORY, not spoken]`, vì dòng đó tồn tại để model không lặp lại thứ user
ĐÃ NGHE, mà ở một turn bị huỷ thì user chưa nghe gì cả.

Khi hoàn tất, HAL còn kiểm tra quan sát ghi frame lời nói thực tế trước khi
nạp history. Nếu bị huỷ hoặc lỗi tổng hợp mà chưa ghi frame lời nói nào,
HAL nạp `spoken=False` (`[TTS HISTORY, not spoken]`). Âm báo kết quả Harness
không được tính là lời nói. Nếu đã bắt đầu phát rồi bị huỷ, nhãn là
`[TTS HISTORY, interrupted; only part may have been heard]`; vẫn lưu toàn bộ
kết quả nhưng không khẳng định user đã nghe hết. Trạng thái được chụp trước
khi callback kết thúc có thể đổi chủ sở hữu playback. Đây là phép đo ghi vào
thiết bị âm thanh, không chứng minh tiếng đã tới tai, và không tự phát lại
kết quả bị huỷ.

Đường thứ hai làm câu trả lời không được nghe nằm trong HAL, và os-server không
thấy được: `speak_queue` bỏ turn bị vượt mặt (một `turn_seq` cũ tới sau khi turn
mới hơn đã sở hữu hàng đợi) rồi **trả về thành công**, nên caller tưởng đã nói.
Đây chính là ca delegate — realtime giao câu hỏi cho agent chính, agent chính
chậm, một turn mới hơn giành mất loa, câu trả lời bay mất trong khi placeholder
của `save_main_handoff` vẫn còn. Vì vậy hai chỗ drop gọi `_on_unspoken_reply`,
hook do `VoiceService` gắn vào cạnh `_on_speak_end`, dẫn về đúng
`feed_realtime_history(..., spoken=False)`. Hook chỉ bắn khi `realtime_feedback`
bật, cùng lý do với đường phát: chỉ câu trả lời thật của agentic runtime mới
được vào context của model, không phải filler hay notice bị bỏ.

`turn_seq` là bộ đếm của os-server, còn ngưỡng đem ra so lại nằm trong tiến trình
HAL, và hai bên restart độc lập. Deploy, OTA hay crash làm bộ đếm bắt đầu lại từ 1
trong khi HAL vẫn giữ mốc cũ, nên mọi turn của phiên mới trông như đến muộn và bị
vứt — đo ngày 03/09/2026: `seq=1` gặp `latest_seq=40` làm câm lời chào lúc thức
(LED và servo vẫn chạy) và sẽ câm tiếp 39 turn sau đó. Run id có mang thời điểm
tạo (`device-chat-<n>-<unix-ms>`), nên khi một sequence THẤP HƠN HOẶC BẰNG đến từ
run được tạo MUỘN HƠN run đang giữ loa, HAL coi như bộ đếm đã restart và nhận
sequence mới. Phải tính cả ca BẰNG NHAU vì restart đưa bộ đếm về 1, nên run mới chỉ
rơi xuống DƯỚI mốc cũ khi mốc đó đang cao — mốc thấp (restart hai lần liên tiếp,
hoặc restart sớm trong phiên) thì hai sequence đụng nhau chứ không thấp hơn. Đo ngày
04/09/2026: `seq=2` gặp một `seq=2` không liên quan từ trước restart và lời chào lúc
thức bị vứt — đúng triệu chứng câm tiếng đó, chỉ cách một phép so sánh.
Id không có dấu thời gian (`tg-<messageID>`) vẫn theo luật sequence thuần: không có
gì để so thì một POST cũ thật sự không được phép giành lại loa.
### Hai đồng hồ im lặng và điểm kết thúc lượt tạm thời

Các đồng hồ im lặng tạo **ứng viên kết thúc lượt**, không chứng minh toàn bộ yêu
cầu đã hoàn tất. STT final còn hiệu lực khởi động đồng hồ ngắn:
`ENDPOINT_SILENCE_S` (`HAL_ENDPOINT_SILENCE_S`, mặc định 0.8s) chạy **từ lúc
final về**, chỉ khi `final_ts >= last_confirmed_speech`. Tiếng nói được xác nhận
sau đó vô hiệu hóa đồng hồ này cho tới khi có final mới. Ngoài trường hợp đó,
vòng lặp dùng `SILENCE_TIMEOUT_S` (`HAL_SILENCE_TIMEOUT`, mặc định 2.5s) từ lần
nói cuối. `HAL_ENDPOINT_SILENCE_S=0` tắt đồng hồ ngắn. Final có thể chỉ là quãng
lấy hơi giữa yêu cầu hoặc “Hello.”; riêng nó không cho phép thực thi.

Với `HAL_TURN_END_ENABLED=true` (mặc định), thu hands-free khi Live tắt đưa ứng
viên qua `_internal/turn_endpoint.py` trước khi đóng STT và commit audio. Gate
HAL này dùng chung cho Gemini, OpenAI Realtime, GPT-Live, Pipecat theo lượt và
đường STT/main-agent thông thường. Nó không đổi cơ chế chốt lượt Live của
provider, Smart Turn sẵn có của Pipecat Live, hoặc thu thủ công bằng tap của
Harness.

`_internal/smart_turn.py` chạy Smart Turn đóng gói trong Pipecat (`LocalSmartTurnAnalyzerV3`;
model v3.2 trong wheel 1.11) ngay trên
máy bằng worker thread, dùng tối đa tám giây PCM16 mono 16 kHz cuối tới thời
điểm ứng viên hiện tại, kể cả phần audio nhỏ/im lặng thực tế đã thu. Setup lamp, image Pi/OrangePi và OTA cho thiết bị không phải Reachy
tự cài extra `pipecat`, nên cài đặt thiết bị thông thường không cần lệnh thủ công.
Developer chạy riêng `uv sync` vẫn cần chọn `--extra pipecat`; Reachy không cài
extra này do xung đột dependency ONNX mô tả bên dưới.
Suy luận không tải model qua mạng. Tiếng nói mới, transcript thay đổi hoặc
STT final mới tới sẽ vô hiệu hóa quyết định đang chờ/đã cache. Final trùng chữ
với partial vẫn tạo token mới và snapshot audio hiện tại, để `INCOMPLETE` cũ
không che mất dữ kiện mới. Gate im lặng tính từ lúc nhận final vẫn chạy trước;
final tự nó không đóng lượt. Dữ kiện không đổi thì không gọi lại inference mỗi
frame im lặng. Kết quả capture cũ không thể đóng capture mới; quá trình thu
vẫn tiếp tục trong lúc suy luận.

Khi model báo hoàn tất, ứng viên có thể đóng lượt. Dấu hiệu ngập ngừng hoặc
liên từ chưa hoàn chỉnh EN/VI như “uhm”, “and”, “và”, “để tôi nghĩ” giữ lượt tới
`HAL_TURN_END_MAX_PAUSE_S` (mặc định 6s) im lặng; lời chào ngắn chờ ít nhất
`HAL_TURN_END_FALLBACK_S` (mặc định 2.5s). Dự đoán chưa hoàn tất chờ dữ kiện
mới để đánh giá lại hoặc tới mức nghỉ tối đa. Khi thiếu model tùy chọn, đang tải, lỗi hoặc suy luận bị
kẹt, transcript thông thường dùng fallback thận trọng: ít nhất 2.5s im lặng
với mặc định, không bao giờ sớm hơn ứng viên gốc. Suy luận đang chờ và chưa lỗi
được thêm tối đa 0.5s từ ứng viên đầu trước khi fallback có thể đóng; detector
thiếu hoặc lỗi bỏ qua khoảng chờ này, và trần nghỉ 6s vẫn áp dụng.
Đây là heuristic có giới hạn,
không bảo đảm người dùng đã nói xong. Gate này không thêm thực thi suy đoán
hoặc hành vi barge-in mới. Tắt gate khôi phục đường đồng hồ im lặng trước đó.

Khi đã nhận được chữ, thu hands-free tăng cường có thể kéo dài tới
`HAL_TURN_END_MAX_DURATION_S` (mặc định 180s), nên yêu cầu 1–2 phút có thể trải
qua nhiều segment STT. Chạm trần cứng này **loại bỏ yêu cầu chưa hoàn tất, không
dispatch**. Thu thủ công và phiên chưa nhận được chữ vẫn dùng
`HAL_MAX_SESSION_DURATION_S` (mặc định code 30s; cấu hình lamp 20s). Nếu phiên
hands-free tăng cường chạm trần ngắn trước khi có chữ, phiên đó cũng bị loại
bỏ. Trần dài không phải thời gian nghe tối thiểu: điểm kết thúc hợp lệ vẫn
chốt yêu cầu ngắn bình thường.

Phần còn lại của mục này nói về bản thân đồng hồ, và áp dụng cho cả hai.
Chỉ dùng RMS là không đủ trong phòng ồn: tiếng ồn phòng nằm trên
`RMS_THRESHOLD`, nên frame nào cũng refresh đồng hồ, lượt chạy tới hết
`MAX_SESSION_DURATION_S`, và audio gần như toàn tiếng ồn vẫn được đẩy sang STT —
quan sát ngày 18/08/2026 là các phiên dài 8–25 giây trả về
`transcript='(empty)'`. VAD theo năng lượng bỏ sót khoảng một nửa số frame nói
thật trong môi trường đó, và các stack voice production (Pipecat, LiveKit,
Deepgram) đều đặt một neural VAD ở quyết định này.

RMS vẫn giữ vai trò cổng chặn rẻ chạy trước, nhưng đồng hồ im lặng chỉ được
refresh khi Silero cũng xác nhận có tiếng nói. Silero chạy theo **cửa sổ**
(`SILENCE_VAD_WINDOW_FRAMES`) chứ không theo từng frame: nó tốn ~20 ms/frame
trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. Nó dùng instance
Silero **riêng** — cái thứ ba, bên cạnh gate đầu vào và noise guard của realtime
— để state LSTM của các đường kia không bị bẩn, và nó reset state đó ở đầu mỗi
phiên. Nó fail-open: model lỗi thì coi như có tiếng nói, nên thiết bị không bao
giờ cắt lời ai.

### Mic bỏ qua chính cue backchannel của mình

Cue lắng nghe của backchannel ("Ok", "Mm", "Oh") được phát mà **không** set cờ
`speaking` của TTS — cố ý, vì cờ đó sẽ kết thúc session STT đang chạy, đúng cái
session mà cue sinh ra để giữ. Nhưng `speaking` cũng là thứ duy nhất bình thường
giữ mic tắt khi thiết bị đang nói, nên cue lọt thẳng vào mic và VAD đầu vào mở một
session **mới** trên chính nó khoảng một giây sau. Quan sát trên thiết bị
19/08/2026: `'Ok'` quay lại thành `transcript='Okay.'` và `'Oh'` thành
`transcript='no'`, mỗi cái chạy thành một lượt thật mà không ai nói.

`Backchannel.self_audio_active` bịt lỗ này mà không đụng `speaking`. `_play()` cài
một deadline (độ dài clip + `HAL_BACKCHANNEL_ECHO_TAIL_S`) *trước khi* sample đầu
tiên phát ra, rồi neo lại phần đuôi theo thời điểm playback thực sự kết thúc. Trong
lúc deadline còn hiệu lực, vòng VAD bỏ các frame đó khỏi phép thử speech **và**
khỏi lookback pre-roll — giữ chúng trong lookback thì cue sẽ thành audio mở đầu của
session kế — rồi reset LSTM của Silero khi resume, đúng phần dọn dẹp mà warm-mic
drain vẫn làm. Chỉ chặn việc **mở** session; session đang stream không bị đụng, đó
mới là mục đích của tính năng.

Mỗi cue còn được buộc vào epoch của phiên STT đã yêu cầu nó. Nếu TTS bình thường
giữ output stream đủ lâu để phiên gốc kết thúc, cue đang chờ bị huỷ ngay trước lúc
phát; nó không thể lọt vào một phiên mic mới thành transcript bịa. Cơ chế này chỉ
huỷ lời nói tuỳ chọn của thiết bị — không đóng, xoá hay mute mic của người dùng, nên
người dùng vẫn có thể nói đè lên nó.

`robots/lamp/rootfs/opt/hal/.env` hạ `HAL_MAX_SESSION_DURATION_S` xuống `20`
(default trong code vẫn là `30`). Thu hands-free tăng cường khi Live tắt và đã
nhận được chữ dùng trần lượt riêng 180s mô tả ở trên; lời nói thực tế có thể
dài hơn 20 giây. Cũng file đó trước kia ghi `WAKEWORD_FOLLOWUP_TIMEOUT_S=60` mà
thiếu prefix `HAL_`, nên nó không có tác dụng gì và thiết bị chạy default 20 s;
nay key đã là `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S=60`.

Nếu kết nối provider **ban đầu** lỗi ngay khi HAL khởi động, orchestrator tạo
session mới bằng retry loop nền (thử lại một lần ngay, rồi backoff luỹ thừa từ 2s,
tối đa 60s). Nó tách biệt
với reconnect send/receive của provider, vì các loop đó chưa tồn tại trước khi
`connect()` thành công. Không cần restart HAL hay chờ audio mới; các lượt voice
vẫn fallback xuống agent chính cho tới khi kết nối hồi phục.

## Khử vọng âm (AEC)

`hal/drivers/voice/aec.py` đưa audio mic qua APM của WebRTC (AEC3), lấy audio
đang phát làm tín hiệu tham chiếu. Nó **độc lập với provider**: tham chiếu được
lấy tại `_WatchedStream.write` (`tts/service.py`) — điểm duy nhất mà mọi đường
phát ra loa đều đi qua: giọng tổng hợp, phần drain của `speak_queue`, và **native
audio** của realtime. Lấy ở đó thay vì tại lúc tổng hợp là có chủ ý: TTS render
một câu nhanh hơn thời gian thực rất nhiều, còn output stream ghi đúng tốc độ
phát — đúng nhịp mà mic nghe thấy.

**Mặc định bật** (`HAL_AEC_ENABLED=true`). Nếu thiếu binding bên dưới thì mọi
điểm vào AEC đều thành no-op, nên bật mặc định không thể làm hỏng thiết bị không
có nó. Nó cần binding `aec-audio-processing`, vốn **không** phải dependency gốc
của hal — PyPI không có wheel Linux nào, nên thiết bị phải build từ source. Nó
nằm sau extra `aec` (`uv sync --extra aec`), cố ý để ngoài `dependencies` và
ngoài `hardware`: bước build cần meson/ninja mà image lamp không cài, nên khai
hard dep sẽ làm hỏng cả build image lẫn `software-update hal` cho một tính năng
vốn tự thành no-op khi thiếu nó. Khi import thất bại, `configure()` log một lần
và mọi entry point trở thành no-op; đường voice hoạt động y như trước, nên mặc
định bật vẫn an toàn với thiết bị không có binding.

Bộ khử vọng chỉ làm sạch mic; nó không quyết định việc cắt lời. Trên đường lượt
không có gì lắng nghe mic đã khử để bắt người dùng nói đè lên câu trả lời — một
bộ phát hiện cục bộ đã được thử và gỡ bỏ sau phép đo ghi lại bên dưới. Người
dùng cắt lời bằng cách chạm (nút GPIO, touchpad TTP223), còn bên trong một phiên
live thì VAD của nhà cung cấp làm chủ việc cắt lời (xem *Chế độ live*).

| Env | Mặc định | Ý nghĩa |
|-----|----------|---------|
| `HAL_AEC_ENABLED` | `true` | Công tắc chính |
| `HAL_AEC_DELAY_MS` | `205` | Gợi ý độ trễ loa→mic. **Theo từng thiết bị** — phải đo, đừng chép lại |
| `HAL_AEC_NS` | `true` | Bật thêm khử nhiễu của APM. Trên phần cứng này nó gánh phần lớn việc khử |
| `HAL_AEC_TAIL_S` | `2.0` | Tiếp tục khử trong khoảng này sau lần ghi loa cuối, rồi bypass APM |
| `HAL_AEC_REF_MS` | `500` | Độ sâu FIFO của tham chiếu vọng âm |
| `HAL_AEC_DUMP_DIR` | — | Ghi `aec_mic/ref/out.wav` để phân tích ERLE offline |

### Cài binding

PyPI chỉ phát hành **wheel Windows** cho `aec-audio-processing`, nên mọi nền
tảng khác phải build từ sdist. Sdist đó đã vendor sẵn toàn bộ source
webrtc-audio-processing + abseil và một wrapper SWIG sinh sẵn, nên build khép
kín: không cần `libwebrtc-audio-processing` của hệ thống, cũng không cần SWIG hệ
thống. Các build requirement của nó (`swig`, `meson`, `ninja`, `cmake`) đều có
wheel trên PyPI, nên **không cần `apt install` gì cả** — điều này quan trọng, vì
người dùng cuối không thể chạy apt trên thiết bị đã xuất xưởng.

Vấn đề nằm ở chính bước build: đo trên lamp (A523, 8 core) mất **5m35s thực /
36m CPU**. Chạy một lần trên máy của dev thì được; chạy trên mọi thiết bị, mọi
lần build image và mọi lần `software-update hal` thì không. Nên dự án build một
wheel rồi đính vào một GitHub release:

```bash
scripts/release/build-aec-wheel.sh <device-ip>   # → dist/aec/*.whl
make upload-aec-wheel                            # → CDN, in ra URL + sha256
```

`build-aec-wheel.sh` biên dịch ngay trên thiết bị, trong `/tmp`, bằng một venv
dùng xong bỏ với meson/ninja lấy từ PyPI — `/opt/hal` và các gói hệ thống không
bị đụng tới — rồi copy wheel về, cài vào một venv sạch để chứng minh nó import
được, và xoá thư mục tạm.

**Build trên máy CŨ nhất, không phải máy mới nhất.** Wheel chỉ link
`libstdc++/libm/libgcc_s/libc` và cần **glibc ≥ 2.34**. glibc tương thích tiến,
nên wheel build trên lamp (Debian 12, glibc 2.36) chạy được trên Reachy Mini
(Debian 13, glibc 2.41) — chiều ngược lại thì không. Wheel mang tag
`cp312-cp312-linux_aarch64`: `uv` trên mọi body đều chạy CPython 3.12 nên tag đó
phủ hết đội máy, và `upload-aec-wheel.sh` từ chối publish thứ khác thay vì để
lệch ABI lộ ra trên máy khách hàng.

Asset nằm trên một tag riêng cho wheel (`wheels/aec-<version>`), không phải tag
phiên bản OS — wheel không đi theo nhịp release của OS, và tag riêng thì không bị
trỏ lại, nên URL đã pin không thể đổi nội dung sau lưng lockfile. Chọn GitHub
release thay vì bucket OTA là có chủ đích: repo này public nên một fork có thể tự
build và tự host wheel của họ, còn bucket thì chỉ nội bộ org.

`hal/pyproject.toml` pin URL đó trong `[tool.uv.sources]`, giới hạn ở
linux/aarch64/CPython 3.12. Đo trên `lamp-0c89`: cài wheel host sẵn mất **1.9
giây**, so với **5m35s** nếu compile. Nằm ngoài marker đó — máy Mac của dev, hay
3.13 sau này — sẽ rơi về sdist trên PyPI và compile, nên `uv sync --extra aec`
lúc nào cũng chạy; chỉ đường nhanh mới được pin.

Vòng VAD chính được bọc, và với `HAL_WARM_MIC=true` (nay là mặc định) mic vẫn mở
suốt lúc phát, nên việc khử chạy ngay trong lúc thiết bị đang nói. Cổng reverb
cố ý không khử để giữ nguyên timing.

**Đo trên lamp** (OrangePi sun60 / A523, mic USB + loa USB — hai miền clock độc
lập). Gợi ý độ trễ phải theo từng thiết bị vì hai clock USB chạy tự do: trên
`lamp-ee17` độ trễ thật là 204 ms (trung vị, một lượt 93 s) và 192 ms ở lượt
khác, trôi 154→215 ms ngay trong một lượt (~667 ppm). Sửa 150→205 đưa ERLE đạt
được từ 15.2 lên 17.9 dB. Trước đó sửa 80→150 trên cùng máy đưa từ 10.9 lên
18.6 dB.

Chi phí ~3.9 % một core A523 ở thời gian thực. Cùng bộ khử này trên MacBook đạt
~42 dB; khoảng cách là do phần cứng — hai clock USB tự do và đường analog rẻ.
Chỉ ~1.6 dB vọng âm ở đây là dự đoán được **tuyến tính** (coherence 0.31), nên
gần như toàn bộ việc khử là suppression — đó là lý do tắt `HAL_AEC_NS` mất ~10 dB
ERLE và làm residual tăng gấp ba.

### Hạn chế đã biết: tham chiếu bị đói

`EchoReference` là một FIFO được lấy tại lúc ALSA **nhận** audio, nhưng mic chỉ
nghe thấy audio đó sau trọn một output buffer, còn TTS thì ghi theo từng cụm
theo nhịp mạng. Khi phần ghi chạy trước xa hơn độ sâu FIFO, những byte cũ nhất —
đúng những byte mic sắp nghe — bị bỏ, và tham chiếu cạn khô cho phần còn lại của
cụm. Đo trên `lamp-ee17`: tham chiếu **underrun trên 30–86 % số khung xử lý**
trong một lượt trả lời, và ERLE mỗi cửa sổ dao động từ −25.1 dB tới 23.2 dB theo
đó. Ở những khung *có* tham chiếu, bộ khử đạt 15–23 dB — nên thiếu hụt là do đói
tham chiếu, không phải do APM.

Tăng độ sâu FIFO không sửa được mà còn tệ hơn (`HAL_AEC_REF_MS=1500` đo được
3.6 / 2.3 dB so với 23.2 / 19.1 dB ở 500) vì độ trễ dẫn trước trở nên thay đổi
và vượt cửa sổ căn chỉnh của AEC3. Cách sửa thật sự là ghi tham chiếu theo nhịp
**phát** thay vì nhịp ghi, cộng với một thread capture riêng để mic thôi rút
`arecord` theo từng cụm.

Nửa "theo nhịp phát" đã làm: `_WatchedStream.write` cắt mỗi buffer của caller
thành các lát `TTS_REF_SLICE_S` (40 ms) và chỉ ghi tham chiếu **sau** khi thiết
bị đã nhận lát đó, nên vòng lặp chạy xấp xỉ tốc độ loa phát. Kích thước lát là
đánh đổi về GIL chứ không phải về âm học — mỗi lát tốn một lần blocking write
xuống PortAudio cộng một lần ghi tham chiếu, đều trong Python; ở mức 10 ms thì
~1600 vòng mỗi câu trả lời nghe thành **giật tiếng** trên board mà thread chính
đã bị vision chiếm gần hết. Thread capture riêng vẫn chưa làm.

WAV cache (gồm lời xác nhận khi single-click) cũng ghi qua wrapper theo block
40 ms. Vòng ngoài 10 ms trước đây làm mất tác dụng gom block của wrapper và
vẫn chạy 100 lượt ghi loa/AEC mỗi giây khi vision đang tải. Nay phát cache dùng
25 lượt mỗi giây, cộng block cuối nếu còn dư, và kiểm tra hủy giữa các block.
Khoảng cách giữa hai lần kiểm tra stop có thể tăng tối đa 30 ms so với vòng cũ.
Đây không phải giới hạn thời gian loa ngừng tiếng: audio đã xếp trong buffer
thiết bị vẫn có thể còn phát sau khi stop.

TTS thông thường qua provider (gồm ElevenLabs PCM 24 kHz phát ở 44.1 kHz)
dùng nội suy tuyến tính liên tục qua các chunk PCM từ mạng trong từng yêu cầu
tổng hợp. Bộ resample giữ mẫu tại biên và clock mẫu thay vì bắt đầu lại nội suy
ở mỗi chunk. Khi EOF bình thường, mẫu cuối được giữ lại sẽ được xuất để bảo đảm
`ceil(N * output_rate / input_rate)` mẫu đầu ra với `N` mẫu đầu vào; khi hủy thì
không xuất phần đuôi này. Các yêu cầu tổng hợp phần đầu, phần đuôi và trong hàng
đợi có trạng thái resample riêng. Resample native realtime và resample toàn
file WAV cache không đổi.

Resample tham chiếu AEC cache hệ số FIR Kaiser mặc định của SciPy theo tỉ lệ
tần số lấy mẫu đã rút gọn và dtype (tối đa 32 mục), tránh thiết kế lại bộ lọc
ở mỗi lần ghi loa. FIR nhân quả dạng streaming giữ lịch sử bộ lọc và pha
resample qua các lần ghi, tạo `ceil(N * output_rate / input_rate)` mẫu đầu ra
cho tổng cộng `N` mẫu đầu vào. Cách này loại bỏ sai lệch do làm tròn từng chunk
và việc lặp lại biên bộ lọc. Cùng bộ lọc chống alias thêm khoảng 0,625 ms độ trễ
khi tần số lấy mẫu thấp hơn là 16 kHz; nhịp ghi FIFO không đổi.
`EchoReference.clear()` hoặc đổi tần số nguồn sẽ reset trạng thái resample.
Mức cải thiện khử vọng vẫn cần được kiểm tra A/B trên thiết bị.
Trước lần ghi loa đầu của mỗi lượt phát, HAL chuẩn bị bộ lọc tham chiếu để lần
import SciPy/thiết kế bộ lọc đầu tiên không làm khựng sau 40 ms audio đầu. Bỏ qua chuẩn bị khi AEC chưa hoạt
động hoặc sample rate bằng nhau. Kiểm tra hủy giữa các lát, kể cả sau chuẩn bị;
chime xác nhận stop vẫn được phát khi cờ dừng lời nói đang bật.

Khi mở stream phát, hệ thống yêu cầu `max(0.120s, default_high_output_latency)`
và log độ trễ thực tế đã thương lượng. Mặc định 43.5 ms từng quan sát trên thiết
bị chỉ nhỉnh hơn một lát ghi 40 ms. Yêu cầu 120 ms tạo khoảng dự phòng lập lịch
bằng ba lát, đồng thời giữ mặc định lớn hơn của các đầu ra như Bluetooth; đây
không phải bảo đảm kích thước buffer hay cách sửa jitter mạng. Audio xếp hàng
nhiều hơn có thể kéo dài phần tiếng còn nghe sau khi hủy.

`_WatchedStream.write` gộp các cờ underflow của PortAudio qua mọi lát ghi.
Underflow giữa lúc phát được log tối đa một lần mỗi 5 giây với số lần tích lũy,
`writer_gap_ms` và `previous_aec_ms`; ranh giới lượt phát (có thể sau idle) chỉ
log debug, còn
các lần ghi keepalive bị loại khỏi thống kê. Underflow đầu ra báo thiếu audio
để phát; AEC reference underrun chỉ báo thiếu mẫu tham chiếu khử vọng và không
chứng minh loa bị underrun. Các thay đổi phát audio cục bộ này vẫn cần được
xác minh bằng nghe thử trên phần cứng.

`aec.uncancelled()` cho biết khung vừa đọc có đi qua mà **không** được khử thật
hay không — tham chiếu underrun, stream bị bypass, hoặc mic overrun. Cổng đường
lên của chế độ live dựa vào cờ này ở chế độ `cancelled` để không bao giờ gửi vọng
âm thô lên như thể là người dùng. Lưu ý điều nó **không**
nói: nó báo tham chiếu có *tới* hay không, chứ không báo việc khử có *hiệu quả*
hay không — một khung ERLE 0,9 dB vẫn được tính là đã khử.

### Tốc độ phát ElevenLabs v3

Với yêu cầu ElevenLabs HTTP có model thực tế là `eleven_v3` (kể cả fallback
từ `tts-1`), HAL gửi `speed=1.0` tới provider và áp dụng `tts_speed` trong
`config.json` ở máy cục bộ qua đường `get_tts_speed` sẵn có. Bộ lọc ffmpeg
`atempo` dạng streaming thay đổi thời lượng nhưng giữ cao độ, trước khi
resample, phát loa và lấy tham chiếu AEC. Tốc độ `1.0` bỏ qua bộ lọc.
ffmpeg đã có trong quy trình chuẩn bị thiết bị. Process bộ lọc khởi động
trước HTTP để chồng thời gian khởi động với thời gian chờ mạng/provider,
và dùng một filter thread cho giọng mono.

Trong lúc chờ đầu ra bộ lọc, HAL kiểm tra hủy để HTTP đang chờ không giữ
ffmpeg sống sau khi hủy. Producer đầu/đuôi giữ thế hệ hủy và ghi queue có
timeout; xóa stop event cho turn mới không làm producer cũ chạy lại. HTTP
đồng bộ đang chờ vẫn có thể tồn tại tới khi nhận dữ liệu hoặc hết timeout
đã cấu hình; audio về muộn bị bỏ và nguồn được đóng.


Khóa WAV cache của v3 có dấu phân biệt để không dùng lại audio v3 đã tổng hợp
theo chính sách tốc độ cũ. Thay đổi này điều chỉnh thời lượng phát, không giảm
thời gian chờ byte đầu tiên (TTFB) từ provider. Các backend TTS khác giữ nguyên
hành vi tốc độ hiện có.

Phát text realtime (cả turn mode và LIVE, không phụ thuộc provider) gửi câu hoàn chỉnh ngay cả khi cùng delta đã chứa đầu câu tiếp theo chưa xong. Phần đuôi được giữ nguyên để ghép tiếp; tag chưa đóng, dấu chấm trong số và viết tắt phổ biến vẫn được giữ thận trọng. Luồng phát buffer hoàn chỉnh, native audio, điều kiện hoàn tất/delegate và cancel không đổi. `[tts-timing] stage=realtime_first_text` đánh dấu text đầu tiên tới đường playback; đối chiếu owner với `speak_requested`/`queue_requested` và timing HTTP để tách thời gian giữ text khỏi tổng hợp giọng.

Log TTS `[tts-timing]` ghi lúc nhận yêu cầu/queue, worker phát, bắt đầu HTTP,
headers/byte giải mã đầu tiên, buffer 4096 byte đầu, đầu ra tempo đầu tiên và
lần ghi loa đầu hoàn tất. `request` riêng cho từng HTTP phân biệt các lần tải
song song; `text_key` (tiền tố SHA-256) nối log tổng hợp và playback. Câu giống
nhau có cùng key nên cần đối chiếu thêm timestamp và owner. Timing HTTP gồm
proxy/mạng/provider, chưa tách được thời gian tính toán provider. Ghi loa chưa
phải lúc âm thanh thực sự tới tai. Log giữ nguyên ranh giới chunk PCM, KPI và
chính sách phát/cancel.

### Vì sao không có cắt lời bằng giọng nói trên mic đã khử vọng

Một bộ phát hiện cục bộ ("barge-in": dừng TTS khi người dùng nói đè lên) đã
chạy từ 25/08 tới 13/09/2026 rồi bị gỡ bỏ. Các phép đo được giữ lại ở đây để
không ai phải thử lại từ đầu. Phần dư sót lại sau khi khử đủ to để trông như
người đang chen ngang, và nó **đúng là** tiếng nói, nên cả cổng mức lẫn bộ phân
loại speech đều không loại được. Đo trong phòng im, trần vọng âm so với lần chen
ngang thật:

| Âm lượng loa | Mixer | Trần vọng âm | Người chen ngang thật |
|---|---|---|---|
| 25 % (`lamp-ee17`) | −45 dB | 9804 | 8027 |
| 40 % (`lamp-0c89`) | −36 dB | 9969 | 6956–8027 |
| 65 % (`lamp-0c89`) | −21 dB | 13560 | 6956 |

Trần vọng âm nằm **trên** mức người thật ở mọi âm lượng, nên ngưỡng đặt dưới nó
thì đèn tự cắt lời mình, đặt trên nó thì bỏ sót giọng nói bình thường. Hạ âm
lượng loa cũng không phải cách chữa: cả 24 dB dải mixer chỉ kéo trần xuống chưa
tới 3 dB, vì đường ghép không do đường truyền qua không khí chi phối.

Phòng vệ cuối cùng được thử là phép so đường bao trên mic **thô**: căn cửa sổ
ứng viên với tham chiếu đang giữ, trừ nó cộng hệ số ghép đã học, rồi đọc độ
*lệch* của phần dư thay vì độ lớn — người chỉ có thể thêm năng lượng, nên phần
dư một chiều là có người khác trong phòng. Gán nhãn trên `lamp-0c89` ở 40 %:

| | Độ lệch phần dư |
|---|---|
| Vọng âm, phòng im (15 cửa sổ) | −2.8 … +2.1 dB |
| Vọng âm, mẻ trộn (~40 cửa sổ) | −50.0 … **+4.8** dB |
| Người chen ngang đã xác nhận | **+8.4** … +40.4 dB |

Trông có vẻ tách được, và 12 câu trả lời trong phòng im không bắn nhầm lần nào.
Nhưng nó không trụ được: 27/08/2026, 20 câu trả lời trong phòng im, đèn tự cắt
lời mình 4 lần; chấm 79 cửa sổ đã gán nhãn (69 vọng âm, 10 lần chen ngang đã xác
nhận) trên mọi đặc trưng sẵn có — tương quan đường bao, độ lệch phần dư, độ lệch
ghép, mức nén của APM, và magnitude-squared coherence kinh điển — cho AUC tốt
nhất 0.72, và mọi ngưỡng đạt 0 % tự cắt lời đều bỏ sót 90–100 % lần chen ngang
thật:

| đặc trưng | vọng âm | người |
|---|---|---|
| coherence | 0.03 … 0.60 | 0.04 … 0.57 |
| tương quan | 0.25 … 0.99 | 0.41 … 0.95 |
| độ lệch (dB) | −6.5 … 67.1 | −1.0 … 119.7 |

Hai lớp chồng lấn gần như hoàn toàn, nên không bộ phân loại nào trên tín hiệu
này làm tốt hơn được. Nguyên nhân nằm ở thượng nguồn: AEC3 chỉ đạt ~6 dB ERLE
trên phần cứng này vì loa và mic là hai thiết bị USB với clock chạy tự do, và
khi hai bên cùng nói, APM không làm nhỏ người nói gần — nó xoá luôn (một khung
đọc 7426 trên mic thô ra khỏi APM còn 5). So trên tín hiệu đã khử thay vì mic
thô cũng đã thử và còn tệ hơn: APM là bộ khuếch đại thay đổi theo thời gian, nó
ăn mất đường bao.

Chỉ thử lại khi chính đường vọng âm tốt lên — bộ khử neural (DTLN-aec chạy thời
gian thực trên Pi 3 B+) hoặc một sound card cho cả hai chiều — và gắn lần thử đó
với bài kiểm tra chấp nhận ở *Chế độ live*: phát lại
`barge-in-captures/full40-bargein-off` qua bộ khử và đòi residual đỉnh nằm dưới
sàn chen ngang thật (6956) với biên an toàn. Trước đó, chạm-để-cắt-lời và VAD
của nhà cung cấp là hai đường cắt lời duy nhất.

`process()` gom audio về khung cố định 10 ms của APM và trả về đúng số mẫu mà
caller yêu cầu (mồi một lần bằng tối đa 10 ms im lặng), nên khung 64 ms của hal
không đổi. ERLE được log định kỳ khi loa đang hoạt động — **0 dB nghĩa là bộ khử
không làm gì cả**.

> Image đã load sẵn `module-echo-cancel` của PulseAudio (`setup.sh`), nhưng
> không có gì đi tới nó: một udev rule đặt `PULSE_IGNORE=1` cho card loa để hal
> tự sở hữu, còn capture đi thẳng qua `arecord -D plughw:`. Module đó không có
> tham chiếu lẫn client; nó không phải thứ đang khử vọng âm ở đây.

## Biểu cảm cảm xúc (fire-and-forget)

Nếu thiết bị khai báo capability `expression`
(`ROBOT.md` → `expression: { routes: [emotion] }`), orchestrator còn đăng ký
thêm tool `express_emotion` (`orchestrator.py`, `EMOTION_TOOL`). Thiết bị không
có "mặt" (vd: chỉ mic + loa) sẽ không có tool này, nên model realtime không thể
set cảm xúc — gating chạy xuyên suốt: `server.py`
(`"expression" in _profile.capabilities`) →
`VoiceService(enable_expression=…)` →
`RealtimeOrchestrator(enable_expression=…)`.

`express_emotion` là **fire-and-forget** — model gọi nó *song song* với việc nói.
Nó không thay thế delegation: với yêu cầu cần main agent, dù có nói câu xác nhận
ngắn hoặc gọi emotion, model vẫn phải gọi `delegate_to_main` trong cùng lượt.
Mô tả tool này không còn áp dụng quy tắc "tool HOẶC nói".
Khi `stream_output()` thấy lời gọi (`_handle_emotion_call`), nó:

1. gọi handler emotion của HAL **in-process** (`_fire_emotion` →
   `routes/emotion.py` `express_emotion`) trong một daemon thread — realtime agent
   chạy ngay trong process HAL nên không cần loopback HTTP / serialize. Nó chạy
   song song với audio đang stream, nên mặt đổi mà không chặn giọng;
2. trả lời lời gọi bằng `FunctionCallResultInput`, trong đó `trigger_response`
   phụ thuộc việc model đã nói trong lượt này hay chưa (`orchestrator.py`,
   `_handle_emotion_call`). Nếu **đã** nói (`trigger_response=False`), kết quả
   được ghi lại mà không sinh response thứ hai — với OpenAI điều này bỏ
   qua `response.create` (`openai_realtime.py`); với Gemini
   thì ack **không được gửi đi**, vì `send_tool_response` ở đó làm lượt *tiếp
   tục* và model nói lại toàn bộ câu trả lời. Nếu **chưa** nói, tool call chính
   là toàn bộ phần model sinh ra cho tới lúc đó và Gemini dừng chờ, nên phải gửi
   ack (`trigger_response=True`) nếu không lượt sẽ treo tới khi watchdog nổ. Độ
   trễ cộng thêm vào giọng nói ≈ 0.

Trên GPT-Live tool này **không tồn tại**: provider không có tool nào ở tầng Live
(chỉ có client delegation), `GPTLiveAgent` log một dòng lúc khởi tạo
(`GPT-Live has no Live-layer tools — [...] unavailable on this provider`) và bỏ
qua mọi kết quả gửi cho nó; mặt thiết bị chỉ đổi qua main agent.

### Cô lập session khi tool call đang chờ (Gemini)

Gemini Live **từ chối `send_realtime_input` khi một tool call do nó phát ra chưa
được trả lời**, và cưỡng chế bằng cách đóng session với WebSocket **`1008`**
("The operation was aborted"). Đây là provider chủ động đóng theo policy, không
phải stream bị rớt — rớt ở tầng transport hiện ra là `1006` với reason rỗng và
được xử lý ở proxy, không phải ở đây.

Vì vậy `gemini_live.py` cách ly **toàn bộ phía client** của session đó, thay vì
chỉ chặn audio từ mic:

- nhận `tool_call` thì đăng ký mọi `call_id` vào `_pending_tool_calls` và làm
  session không thể gửi thêm dữ liệu;
- khi còn bất kỳ call nào chưa được giải quyết, **mọi input từ client đều bị
  chặn**: `AudioInput`, `activityStart` manual VAD, `activityEnd`, commit và các
  message client khác. Không buffer để phát lại, vì như vậy lời nói thu trong
  trạng thái provider không hợp lệ sẽ thành một lượt cũ ở thời điểm sau;
- với `FunctionCallResultInput` thông thường, call vẫn pending đến khi Gemini
  đã chấp nhận `send_tool_response`. Chỉ provider acknowledgement thành công đó
  mới xoá call và làm session hiện tại dùng lại được. Ack thất bại hoặc bị từ
  chối giữ session ở trạng thái cách ly và session sẽ bị bỏ;
- path `express_emotion` fire-and-forget phía trên cố ý không gửi acknowledgement
  cho Gemini khi model đã bắt đầu nói, vì gửi nó làm Gemini lặp lại câu trả lời.
  Session như vậy không thể hợp lệ trở lại: nó không được dùng lại, và lần
  `prepare_turn()` tiếp theo sẽ rebuild một session mới;
- không có expiry hay timeout nào mở lại một session đang cách ly. Session
  fresh/rebuild không thừa kế pending call.

Đặc biệt, `_async_commit` cũng chặn `activityEnd` khi session bị cách ly. Hoàn
tất activity bracket cũ không an toàn khi Gemini còn chờ tool result; session
thay thế sẽ bắt đầu activity kế tiếp một cách sạch sẽ.

Một handoff `delegate_to_main` hợp lệ cũng đánh dấu session cần thay mới
**trước khi** phát sự kiện tool, kể cả khi Gemini chưa nói câu nào.
Ack `delegated` thành công xoá pending call nhưng không xoá yêu cầu thay
session. `prepare_turn()` kế tiếp rebuild trước khi gửi audio thu mới.
Cách này ngăn lời nói đến muộn sau handoff (kể cả câu xin lỗi hệ thống do
provider sinh) lọt sang phiên live tiếp theo với turn ID rỗng. Message
delegate rỗng/không hợp lệ không bật cờ này. Rebuild tốn thêm thời gian kết
nối sau handoff; đây không phải bộ lọc câu lỗi hay sửa lỗi thực thi tool upstream.

Khi receive timeout, `[realtime][transport]` ghi trạng thái kết nối/luồng gửi,
số input đang chờ, tuổi lần gửi audio thành công cuối, số tool pending và số
frame bị giữ vì tool. Đây là metadata, không ghi âm. `last_upload_ms` của
luồng thu live chỉ đo thời gian đưa vào hàng đợi, chưa chứng minh đã gửi ra socket.

Model được dặn (`resources/system_prompt*.md`, mục "Expression Exception") không
chờ, không thông báo, không đọc tên cảm xúc thành tiếng. Lưu ý điều này khác
path không-realtime: ở đó agent phát marker text `[HW:/emotion:…]` rồi lớp Go
parse và cắt bỏ — path realtime không bao giờ dùng marker text.

## Google Search grounding (chỉ Gemini)

Mặc định Gemini Live được cấp sẵn tool **Google Search** built-in
(`HAL_GEMINI_GOOGLE_SEARCH`, mặc định bật; wiring trong `gemini_live.py` như một
`types.Tool(google_search=…)` riêng, đặt cạnh các tool function-declaration). Nhờ
đó model realtime tự trả lời các câu **dữ liệu công khai theo thời gian thực** —
thời tiết, tin tức, thể thao, giá cả, "mấy giờ mặt trời lặn" — bằng cách grounding
ngay trong phiên và tự nói kết quả, thay vì gọi `delegate_to_main` và chịu nguyên
một vòng round-trip xuống main agent. System prompt của Gemini
(`system_prompt_gemini.md`) xếp các lookup công khai này vào mục *Direct Home Run*,
và chỉ chuyển dữ liệu live **thuộc tài khoản/riêng tư** (lịch của user, trạng thái
thiết bị smart-home của họ, tin nhắn của họ) cho `delegate_to_main`.

Đánh đổi:

- **Chỉ Gemini có dạng tool hosted.** OpenAI Realtime không có tool built-in
  tương đương, nên prompt của nó (`system_prompt_openai.md`) vẫn delegate mọi
  lookup bên ngoài. GPT-Live càng không: không có tool nào ở tầng Live,
  `system_prompt_gptlive.md` xếp mọi dữ liệu live bên ngoài vào việc của backend
  (backend Responses của nó tự search). Pipecat v1 cũng không có search hosted,
  nhưng được cấp một function tool **phía client** `web_search` trả lời qua relay
  Google-Search của campaign-api — xem *Web search* trong mục Pipecat v1.
- **Chi phí.** Grounding tính phí theo mỗi grounded request (cộng thêm token),
  nhưng chỉ phát sinh khi Gemini thực sự quyết định search. Prompt dặn nó *chỉ*
  ground cho dữ kiện công khai/mới thật sự, không ground cho kiến thức chung đã có
  sẵn. So với trước, phần lớn là **dời** chi phí (và latency) khỏi main agent.
- **Chỉ đọc.** Grounding chỉ trả lời câu hỏi, không thực hiện hành động. Nhạc,
  phần cứng, ghi memory, và skill vẫn delegate.

## Thị giác trong phiên — tool `look` (chỉ Gemini)

Khi người dùng hỏi về thứ thiết bị **nhìn thấy** ("cái này là gì?", "nhìn cái này
nè", "nhìn thứ tôi đang cầm", "tôi đang cầm gì?", "đọc cái nhãn này", "màu gì đây?"),
model realtime trả lời ngay trong phiên thay vì delegate. Lưu ý "nhìn cái này" đi vào
đường này, **không phải** đường bật/tắt camera riêng tư — `skills/camera/SKILL.md`
phân biệt động từ theo thứ đứng sau nó, vì "nhìn tôi này" nghĩa là "bật camera lên"
còn "nhìn cái này" là một câu hỏi về một vật. Chỉ áp dụng cho turn **thuần** hỏi về
thứ nhìn thấy: nếu cùng turn còn kèm hành động ("quay sang phải, giữ nguyên đó, rồi
nói xem thấy gì"), prompt bắt buộc gọi một `delegate_to_main` gộp cả hai vế — không
`look` — để lệnh chuyển động không bị âm thầm bỏ rơi. Mô tả tool và prompt Gemini
đều loại trừ việc tìm một vật cụ thể ("cây bút của tôi đâu", "bạn có thấy chìa khóa
không") — đó là một lượt tìm kiếm được delegate, không phải look. Model vẫn có thể
bỏ qua lời dặn đó ("Find my mouse" đã đi vào `look` trên 3.8 extended-thinking,
#481), nên orchestrator ép buộc bằng code: khi `look` được gọi và transcript của
provider cho turn đó khớp `hal/realtime/find_intent.py` (tiếng Anh: find / locate /
search / look for / look around / where is / do hoặc can you see my; tiếng Việt:
tìm / ở đâu / đâu rồi), `_redirect_find_look` **không** ngắm và không chụp. Nó ack
call bằng `{"result": "delegated"}`, xóa `realtime_look_frame_path`, kết thúc turn
và delegate transcript, nên main agent chạy `/servo/search` mà không có
`[vision-image]`. Transcript rỗng tại thời điểm gọi thì bỏ qua guard (log
`look: no transcript at call time — find guard skipped`). Orchestrator đăng ký
tool `look` (`orchestrator.py`, `LOOK_TOOL`) và xử lý trong `_handle_look_call`:

1. **Ngắm đầu vào đối tượng trước**, trên thiết bị có thể chuyển động — nếu
   không thì model sẽ trả lời đầy tự tin về bất cứ thứ gì cái đầu tình cờ đang
   hướng tới. Xem
   [Look-aim](../../robots/lamp/docs/vi/vision-tracking_vi.md#look-aim--ngắm-đầu-trước-khi-một-câu-hỏi-thị-giác-chụp-ảnh)
   để biết vòng lặp ngắm, cách nó chọn ai mới là người đang hỏi, và bearing đã
   ghi nhớ mà nó quay về khi không thấy ai.
2. Lấy frame camera **nét** **in-process** (`_capture_frame` gọi
   `capture_still` — không qua HTTP loopback; servo bị freeze (cả animation
   loop lẫn servo worker của tracker đều tôn trọng cờ này) và frame chỉ được
   chấp nhận khi timestamp chụp vượt quá thời gian chờ lắng tính từ lần ghi bus
   servo cuối, nên motion blur không lọt tới model. Thời gian lắng là 0.3s, co
   giãn theo độ lớn của lần chỉnh ngắm cuối với trần 0.5s — một lần ngắm hết hạn
   chót sẽ thoát ra ngay sau một cú quay lớn, và cần đèn vẫn còn rung quá mốc
   300ms cố định; không thêm độ trễ khi servo vốn đang đứng yên hoặc thiết bị
   không có servo), downscale về `HAL_GEMINI_VISION_MAX_WIDTH`
   (mặc định 768px) để giới hạn token ảnh.
3. **Session extended-thinking** (session đã báo `interaction_status`, nên
   `supports_look_continuation` là true — 3.8 extended-thinking): JPEG nằm
   **bên trong tool response của `look`** (`FunctionCallResultInput.image` →
   `FunctionResponse.parts`, log `Sent look image in tool response
   (call_id=… bytes=…)`), và không replay. Ack kích hoạt lượt generate kế
   tiếp, nên một frame gửi riêng sau ack có thể vẫn đang được xử lý khi lượt
   generate đó bắt đầu, và model trả lời "không nhìn thấy" (#481).
   `send_realtime_input` không bảo đảm thứ tự. Gộp thành một message thì hết
   race mà không phải chờ. google-genai 2.12.1 không serialize được bytes ảnh
   lồng trong `send_tool_response`, nên riêng kết quả này được gửi bằng payload
   WebSocket base64 tường minh, và bị bỏ nếu lời gọi look đã bị hủy, đã được trả
   lời hoặc bị xóa khi reset session (xem phần interaction status). Kiểm tra qua
   proxy 2026-09-23 với cùng cơ chế: 3.8 extended-thinking trả lời đúng từ ảnh
   trong response 2/2 lần, và 3.8-live thường 3/3 lần với prompt trung tính. 3.8-live thường không báo interaction
   status, nên vẫn đi đường bên dưới.

   **Các session khác** (3.1, 3.8-live thường) đi đường cũ: đẩy frame vào làm
   **video input** realtime (`ImageInput` → `send_realtime_input(video=…)`),
   rồi **replay turn**: Live API xếp frame gửi giữa-turn vào turn KẾ TIẾP
   (device-proven: flow ack-tool → tiếp-turn cũ khiến mọi câu look trả lời bằng
   ảnh của lần look *trước* — lệch 1 ảnh, delay ack bao nhiêu cũng không cứu),
   nên thay vì ack tool call, orchestrator yield `LookReplaySignal` và
   `run_realtime_turn` gửi lại audio của turn + commit lần nữa trên CÙNG
   session. Frame đang xếp hàng vào đúng turn replay.
4. (Chỉ đường replay.) Turn replay kích hoạt `look` lần nữa, rơi vào reuse guard
   (`VISION_MIN_INTERVAL_S`) và được ack `trigger_response=True` — model trả
   lời bằng frame lúc này đã thật sự nằm trong context.

Plumbing hỗ trợ replay: `receive()` nuốt đúng MỘT `turn_complete` cũ (của turn
bị hủy, về sau replay commit và nếu không sẽ kết thúc rỗng turn replay —
`skip_next_turn_done()`); recycle session idle/turn-cap đang chờ sẽ bị hoãn khi
replay pending (rebuild lúc đó làm mồ côi ảnh vừa gửi); và mọi lần rebuild
session đều reset look reuse guard (ảnh sống trong session — session mới không
có ảnh nào). Chi phí: audio câu hỏi bị tính 2 lần ở turn look; ảnh 1 lần.

Cái này thay cho đường chậm (delegate → main → tìm skill → `/camera/snapshot` →
LLM vision, vài giây) bằng một round-trip ngay trong phiên.

Điều kiện kích hoạt (cần cả ba, nếu không câu hỏi thị giác sẽ rơi về delegate):

- **Capability:** có camera (`app_state.camera_capture` được set). Đây chính là
  capability `vision` ở runtime — `server.py` chỉ tạo `camera_capture` khi
  ROBOT.md khai báo `vision`. Orchestrator đọc đúng một tín hiệu này
  (`_camera_present()`), nên đúng cho mọi đường khởi tạo.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (mặc định **bật**).
- **Provider:** chỉ Gemini (luồng inject ảnh → tiếp tục turn đã làm + test cho
  Gemini Live; OpenAI vẫn delegate câu hỏi thị giác; GPT-Live không có tool lẫn
  input ảnh — `gpt_live.py` bỏ frame lạc với một cảnh báo duy nhất). System prompt Gemini
  (`system_prompt_gemini.md`) mô tả khi nào gọi `look`.

Chi phí: một frame mỗi lần gọi (kích bằng tool, **không** stream video), nên token
thêm vào là không đáng kể so với audio của turn. Frame 768px ≈ vài trăm token ảnh.
Để chặn model gọi `look` quá nhiều làm tốn token ảnh, `_handle_look_call` chỉ gửi
**tối đa một ảnh mỗi turn** và **không gửi ảnh mới trong vòng
`HAL_GEMINI_VISION_MIN_INTERVAL_S` (mặc định 10s)** kể từ lần gửi trước — các lần
`look` lặp lại sẽ xài lại ảnh đã có trong context.

**Bàn giao frame khi delegate / timeout.** Khi một turn `look` rốt cuộc delegate
hoặc rớt xuống main agent (quan trọng nhất là khi Gemini timeout *giữa* lúc look),
frame mà `look` đã chụp được bàn giao cho main agent để nó trả lời từ đúng ảnh
đó thay vì chụp lại (nhanh hơn, và trả lời đúng khoảnh khắc user chỉ vào).
`_handle_look_call` lưu frame vào `_SNAPSHOT_DIR` và ghi vào
`app_state.realtime_look_frame_path`; `turn_dispatch._take_vision_handoff()`
tiêu thụ nó **một lần mỗi turn** (turn đã handled dùng rồi thì clear luôn để
delegate sau không nhặt phải ảnh cũ) và, khi còn tươi
(`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, mặc định 45s), chèn dòng hint
`[vision-image] <path>` vào message VÀ gửi frame dạng base64 trong field
`image` của sensing POST.
os-server xử lý ảnh theo **gate describe-first** trong `system/vision` (xem
`server/sensing/delivery/http/handler.go`): khi main model đang active KHÔNG
khai image input trong catalog model (trường hợp Auto-AI — attachment thô sẽ
404 tại smart-agent-router: "No endpoints found that support image input"),
frame được `default_image_model` của catalog (`DefaultImageModel` trong
`system/vision/describe.go` — cùng model mà `imageModel` của openclaw dùng cho
ảnh Telegram) tả thành chữ và agent nhận dòng
`[image description] …` — đồng thời hint `[vision-image]` được viết lại để
**bỏ path file**, và **file snapshot cũng bị xoá luôn** (best-effort). Cả
path lẫn file đều không được sống chung với description: snapshot nằm trong
media allow-list của agent nên bất kỳ path nào agent vớ được — hint, hint cũ
trong session history, `ls` thư mục — đều có thể bị `read` thành image block
nằm lì trong session history, làm 404 mọi turn sau mà router rơi vào model
text-only (kể cả turn thuần chữ). Describe được thử 2 lần (20s + 15s, tổng
35s — request treo được retry trên kết nối mới); fail cả hai thì ảnh bị
**bỏ luôn**, file snapshot vẫn bị xoá, và hint được viết lại để agent nói
với user là lần này không nhìn được — tuyệt đối không gửi raw attachment,
vì khi router rơi vào model text-only thì attachment đó đầu độc cả session,
đắt hơn nhiều so với hỏng một turn. Còn khi catalog nói model nhận ảnh,
attachment thô được forward thẳng và hint giữ nguyên path. Gate đọc lại catalog mỗi 30 phút, nên BE flip catalog là
fleet tự chuyển. Gate này cũng cover luôn ảnh upload từ web monitor chat — cả
hai nguồn ảnh hội tụ về một handler. Skill `camera` dặn agent trả lời từ mô
tả/attachment và bỏ qua `/camera/snapshot`. Nếu timeout xảy ra *trước khi* kịp
chụp thì không có gì để bàn giao, agent chụp như bình thường.

## Các provider

Bốn backend thay thế cho nhau, chọn bằng `HAL_REALTIME_PROVIDER` /
`realtime.provider` (`none` | `gemini` | `openai` | `gptlive` | `pipecat_v1`)
và dựng trong `orchestrator._make_agent`; Go `RealtimeProviders` và dropdown web
(`RealtimeSection.tsx`) liệt kê đúng các giá trị này, theo đúng thứ tự đó, trước
`none` (nhãn web của `gptlive` là "GPT-Live"):

| Provider | Class | Mô hình threading | Model mặc định | Sample rate |
|----------|-------|-------------------|----------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | event loop asyncio riêng trên thread `gemini-io`; thread send/recv submit coroutine qua `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | thuần đồng bộ; 1 `RealtimeConnection` (SDK `openai` GA, `openai.resources.realtime`) dùng chung bởi thread send/recv, serialize bằng reentrant lock | `gpt-realtime-2` | 24000 Hz |
| GPT-Live | `voice_agent/gpt_live.py` `GPTLiveAgent` | thuần đồng bộ; 1 `LiveConnection` (SDK `openai` ≥ 3.14.1, `openai.resources.live`) dùng chung bởi thread send/recv dưới `_conn_lock`, cộng thread watchdog `gptlive-watchdog` (tick 50 ms) tổng hợp ranh giới lượt | `gpt-live-1` | 24000 Hz (hoặc 16000; một định dạng PCM cho cả hai chiều) |
| Pipecat v1 | `voice_agent/pipecat_v1.py` `PipecatV1Agent` (+ `pipecat_pipeline.py`, `pipecat_stt.py`) | **không có session vendor**: một pipeline Pipecat trên event loop asyncio riêng (thread `pipecat-io`) ngay trong HAL; thread send submit frame qua `run_coroutine_threadsafe`, `EventSink` của pipeline ghi thẳng vào recv queue, thread recv chỉ canh sức khỏe của pipeline | `qwen/qwen3.6-35b-a3b` qua relay Qwen của campaign-api (bất kỳ endpoint chat tương thích OpenAI nào) | 16000 Hz vào; **text ra** (TTS của HAL đọc) |

Gemini Live dùng `google-genai` và private asyncio loop của nó do thread
`gemini-io` sở hữu. Teardown đóng/hủy provider receive task trước, rồi mới join
worker; handshake thất bại rollback loop/thread ngay. Nhờ vậy một receive bị
kẹt không sống sót qua session rebuild. Teardown còn chạy callback hoàn tất đang
chờ ngay cả khi không còn task pending, để receive vừa kết thúc chuyển kết quả/lỗi
về thread đang chờ trước khi đóng loop. Điều này tránh future bị bỏ lại và
`Task exception was never retrieved` khi đóng/rebuild, kể cả SDK `APIError(1000)`
cho WebSocket đóng bình thường; lỗi receive bất thường vẫn đi qua xử lý lỗi/reconnect
hiện có. Với họ native-audio, HAL gửi websocket
ping mỗi 20 giây nhưng không đặt ping timeout: traffic đi ra giữ đường proxy
sống mà pong bị thiếu không bị hiểu là lỗi client. HAL cũng
recycle Gemini đồng bộ trước khi stream audio nếu session hiện tại đã idle quá
`HAL_GEMINI_PRE_TURN_RECYCLE_S` giây, để câu nói sau khoảng nghỉ không rơi vào
socket đã chết vì idle ở proxy. Thời gian idle tính từ mốc muộn hơn giữa lúc turn
trước kết thúc và lúc session kết nối (`_idle_since_monotonic`). Chỉ tính từ turn
trước thì một session vừa kết nối sau khi bật lại mic bằng công tắc riêng tư cũng
bị recycle, và turn rơi về main agent trong ~10 giây kết nối lại (thiết bị
2026-09-23).

**Park khi idle.** Session Gemini không ai nói chuyện cùng sẽ bị server đóng bằng
WS `1008` "The operation was aborted" (thời gian sống khi idle đo được: 86-198
giây). Cú đóng đó không làm hỏng turn nào — pre-turn recycle ở trên đã thay
session trước khi turn sau khoảng nghỉ stream audio — nhưng backend ghi nó là lỗi
và bắn cảnh báo, nên thiết bị phải đóng trước. Sau `HAL_GEMINI_IDLE_PARK_S` giây
không có hoạt động turn nào, thread watchdog `rt-idle-park` đóng transport và
đánh dấu session là *parked*. Orchestrator đang parked vẫn báo `available`:
`prepare_turn()` kế tiếp sẽ nối lại session mới một cách đồng bộ
(`idle-park-resume`) trước khi có audio nào được stream — đúng bằng việc mà
pre-turn recycle vốn đã làm cho turn đó — và `voice_service` giữ đệm capture qua
~1 giây handshake. Không park khi đang có turn chạy dở; nếu resume không nối
được thì báo unavailable (turn rơi về main agent) nhưng vẫn giữ trạng thái parked
để turn sau thử lại.
Ở chế độ wake-word, việc nối lại được chạy chồng lên câu user đang nói: `Session
START` gọi `prewarm()`, khởi động handshake `idle-park-resume` ở nền
(`idle-park-prewarm`, thread `rt-prewarm`), và `prepare_turn()` chạy trên worker
chuẩn bị capture sẽ đợi nó xong (tối đa `PREWARM_JOIN_TIMEOUT_S` = 4 giây) thay vì nối lại tuần
tự — đo trên lamp-4ace 22/09/2026, nối tuần tự tốn ~2 giây mỗi turn sau khoảng
nghỉ. Capture không được dispatch chỉ để lại một session idle mà watchdog park sẽ
đóng lại.

Mọi provider coi teardown là trạng thái kết thúc: sau khi `disconnect()` đặt
stop signal, worker send/receive không reconnect và cũng không ghi log lỗi
transport trong lúc socket đã đóng đang unwind.

**OpenAI Realtime** nói schema **GA** của Realtime API qua SDK `openai`
(`openai.resources.realtime`; `hal/pyproject.toml` ghim `openai>=3.14.1`, `uv.lock`
khoá 3.14.1 — bản 1.99.9 trong lock cũ không có module `openai.resources.realtime`
nên adapter còn không import được; module GA có từ openai 2.x, 3.x thêm
`client.live` cho GPT-Live). `_sync_connect` gọi `client.realtime.connect(model=…)`
rồi gửi một `session.update` (`_build_session`):

- `type: realtime`, `output_modalities: ["audio"]` — chỉ audio; transcript của
  audio (`response.output_audio_transcript.delta`) là nguồn text. Thêm `"text"`
  sẽ làm model phát cả `response.output_text.delta` và câu trả lời bị nói hai lần,
  nên recv loop lờ event đó nếu nó vẫn tới.
- `audio.input.format` / `audio.output.format` = `{type: audio/pcm, rate: 24000}`
  (PCM vào và ra cùng 24 kHz, `output_sample_rate` giữ bằng input).
- `audio.input.transcription` = `{model: HAL_OPENAI_TRANSCRIBE_MODEL (mặc định
  gpt-4o-mini-transcribe), language: <ISO-639-1 rút từ stt_language>}`. Input
  transcription là **nguồn duy nhất** của lời người dùng trên đường OpenAI
  (`UserSpeechOutput.transcript`, live history, message delegate) nên luôn bật;
  `gpt-4o-mini-transcribe` stream delta (history live và xác nhận barge-in thấy
  chữ sớm), `whisper-1` chỉ gửi bản `completed`.
- `audio.input.noise_reduction` = `{type: HAL_OPENAI_NOISE_REDUCTION}` (mặc định
  `far_field` — mic phòng như đèn; `near_field` — headset; `off` bỏ hẳn key). Lọc
  buffer trước VAD và model: ít onset VAD giả từ tạp âm phòng / tàn dư echo — nửa
  OpenAI của phòng thủ echo mà Gemini có qua VAD sensitivity.
- `audio.input.turn_detection`: `null` khi `HAL_REALTIME_TURN_DETECTION=off` (lượt
  thủ công, client bracket); `server_vad` → `threshold` = `HAL_OPENAI_VAD_THRESHOLD`
  nếu khác 0, không thì 0.7 cho `HAL_LIVE_VAD_START_SENSITIVITY=low` / 0.3 cho `high`
  (API mặc định 0.5; "low" = onset phải có bằng chứng to hơn = threshold cao hơn),
  `prefix_padding_ms` = `HAL_LIVE_VAD_PREFIX_PADDING_MS` (khi >0),
  `silence_duration_ms` = `HAL_LIVE_VAD_SILENCE_MS` (khi >0); `semantic_vad` không
  có threshold, `eagerness` = `HAL_LIVE_VAD_END_SENSITIVITY` (`low`/`high`). Chỉ gửi
  knob nào được set, phần còn lại theo mặc định API; cấu hình thực tế được log
  `[realtime] server VAD: type=… threshold=… prefix_padding=…ms silence=…ms eagerness=…`.
- `tools` + `tool_choice: auto` khi có tool; `reasoning.effort` theo
  `HAL_OPENAI_REASONING_EFFORT`; `truncation` = `retention_ratio` 0.5.

Lượt thủ công (HAL local VAD): append → `input_audio_buffer.commit` +
`response.create`, kèm log `Turn timing: local_end->commit_sent=Nms`. Khi server
VAD bật, `commit_audio()` là **no-op** trên OpenAI: server tự commit buffer và tự
tạo response ở `speech_stopped`; commit từ client sẽ rơi vào buffer đã commit
(rỗng) và `response.create` thứ hai va với cái của server — cùng quy tắc với
Gemini chỉ gửi `activityEnd` khi automatic activity detection tắt. Latency được
log `Response latency: Nms (speech_end->first_audio; commit_sent->first_audio=Nms)`
hoặc `(…; server VAD)`.

**Contract live-mode giống hệt `gemini_live.py`** (mọi thứ live pump và đường lượt
bám vào đều được phát ở đây; `hal/test/test_openai_live_provider_metrics.py`, 21
test, soi gương `test_live_provider_metrics.py` cho OpenAI):

- mọi output và `TurnDoneEvent` mang `user_turn_id`, **đóng băng theo response**
  tại `response.created` (hoặc ở output đầu nếu lỡ event đó), nên một input
  transcription đến muộn không bao giờ chiếm lại reply trước đó;
- `UserSpeechOutput` (chỉ LIVE_MODE) từ `input_audio_buffer.speech_started` (key
  mới `openai-<hex>`), `input_audio_buffer.speech_stopped` (`endpoint_at`, method
  `server_vad`), `conversation.item.input_audio_transcription.delta` / `.completed`
  (method `provider_transcript`; `.completed` chỉ phát phần chưa stream qua delta
  để consumer nối chunk không thấy câu hai lần; map `item_id → turn` — tối đa 16
  item — gán transcription đến sau `speech_started` KẾ TIẾP về đúng input nó chép;
  lượt thủ công không có `speech_started` nên `item_id` được biết ở
  `input_audio_buffer.committed`);
- `InterruptedOutput(reason="output_reset")` trước
  `response.output_audio_transcript.delta` đầu tiên của một reply, để reply không
  bao giờ phát sau một reply cũ trong hàng đợi TTS live;
- `OutputEvent.gen` tăng mỗi receive turn và mỗi lần ngắt, để
  `VoiceAgentBase.receive()` bỏ audio của reply đã bị hủy;
- `note_server_activity()` ở mọi event vào (một turn đang reasoning hay chạy tool
  gửi rất nhiều thứ không bao giờ vào queue — rate limit, lifecycle item,
  transcription — nên không "im" trên đường truyền);
- `FunctionCallOutput.user_transcript` lấy từ input transcription;
- `TurnDoneEvent.execution_completed` = `response.status == "completed"` **và**
  không bị ngắt.

**Barge-in.** Khi `input_audio_buffer.speech_started` đến giữa lúc response đang
chạy (server tự cancel response với `interrupt_response` mặc định của API; queue
local là việc của HAL), hoặc `response.done` báo `status: cancelled` mà không thấy
`speech_started` nào của mình (`response.cancel` từ client, hay semantic VAD tự
quyết giữa câu): `_handle_interrupt` rút cạn recv queue (giữ lại
`UserSpeechOutput` / `ExecutionOutput` / `InterruptedOutput` `server_interrupt`;
một `TurnDoneEvent` đang xếp hàng biến thành `ExecutionOutput` trong LIVE_MODE để
giữ bằng chứng metric mà không phát lại terminal điều khiển), tăng gen, phát
`InterruptedOutput(reason="server_interrupt", at=now, user_turn_id=<chủ của reply
bị hủy>)` (LIVE_MODE), bỏ delta còn bay của response bị hủy (so `response_id`), và
gửi `conversation.item.truncate` cho item audio của assistant (`audio_end_ms` =
audio đã nhận − audio còn trong queue; client `event_id` `hal-truncate`) để
context của model khớp với thứ người dùng thật sự nghe. Ước lượng này dư vài trăm
ms vì buffer playback của HAL; giá trị vượt độ dài thật chỉ sinh một `error`
lành tính mang `event_id` đó. Log: `Response interrupted — dropped N queued
output(s), gen=N`.

**Lỗi.** Event `error` với code `input_audio_buffer_commit_empty` (local VAD chốt
một lượt ngắn hơn 100 ms tối thiểu), `conversation_already_has_active_response`
(server VAD và commit client chéo nhau), `response_cancel_not_active` (cancel một
response đã xong), hoặc mang `event_id` `hal-truncate` là race lành tính: log INFO
(`Realtime API notice (<code>)`) và bỏ qua. Mọi `error` khác vẫn fail-fast
(`OpenAIRealtimeError` → `TurnDoneEvent` ngay, reconnect nền). Hook base để mặc
định có lý do: `end_turn()` no-op vì OpenAI gửi `response.done` kể cả response chỉ
có function call nên `_turn_done` luôn được recv loop nhả — ép nó sẽ race một
response còn chạy thành `conversation_already_has_active_response`;
`requires_fresh_session` False vì item `function_call_output` ghi được mà không
cần response, nên tool call chưa ack không đầu độc session như Gemini (1008).

Vẫn **chỉ Gemini** (không đổi; OpenAI Realtime lẫn GPT-Live đều không có): `look`
vision trong phiên, Google Search grounding, session resumption,
`requires_fresh_session`, override `end_turn()`.

Cả ba kế thừa `voice_agent/base.py` `VoiceAgentBase`, định nghĩa contract dựa
trên queue:

- **2 thread mỗi agent**: `_send_loop` rút `_send_queue` → API; `_recv_loop` đọc
  API → `_recv_queue`. Cả ba tự reconnect khi lỗi.
- **Fail-fast khi backend lỗi** (cả 3 driver): khi `_recv_loop` gặp lỗi thật
  (Gemini Live: proxy `go_away`, hết quota / resource-exhausted, WS close bất
  thường — tức **không phải** idle close `1000` lành tính; OpenAI: event `error`
  của Realtime API ngoài các code race lành tính liệt kê ở đoạn OpenAI, hoặc
  socket rớt; GPT-Live: `error` không mang client `event_id` `hal-*` của mình hoặc
  trên `hal-start`, và mọi lần `session.closed` kết thúc vòng lặp event), nó đẩy
  `TurnDoneEvent` ngay lập tức
  (`_fail_fast_turn`) để `receive()` thoát liền và lượt fallback sang main agent
  **mà không** phải chờ hết `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`. Idle close lành
  tính vẫn reconnect êm (Gemini code `1000`; OpenAI kết thúc vòng lặp event êm,
  không phải lỗi). Chỉ kích hoạt khi đang có lượt chờ output (`_turn_done` clear);
  reconnect vẫn chạy nền để hồi phục session cho lượt sau.
- **Non-blocking**: `append_audio()`, `commit_audio()`, `send()` (đẩy vào queue,
  gate trên `available`).
- **Blocking**: `connect()`, `disconnect()`, `receive()` (generator yield
  `OutputBase` đến khi gặp `TurnDoneEvent`, hoặc khi không có event nào trong
  `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` — mặc định 8 s — để kết thúc lượt im lặng
  và fallback sang main agent mà không bị dead-air dài).
  Gemini extended-thinking khi Live tắt dùng tiến độ đã xác minh và chunk đáp
  án thật như trên: search/tool có thể kéo dài chờ tới hạn 15 giây từ commit,
  còn chunk đáp án có giới hạn im lặng 8 giây. Lưu lượng nhận chung không kéo dài
  lượt im lặng của model này. Các provider/chế độ khác giữ chính sách liveness
  hiện có: gọi `note_server_activity()` khi nhận message, và `receive()` có thể
  kéo dài lượt im lặng khi message còn về, tối đa
  `HAL_REALTIME_TURN_MAX_SILENCE_S` (mặc định 20 s). Lượt không có message về vẫn
  kết thúc ở cửa sổ gap đầu tiên.
- `available` ⇔ websocket/session đã connect (`_connected`).
- **Contract live-mode** (cả ba provider phát giống nhau — xem đoạn OpenAI ở
  trên; GPT-Live tổng hợp từ transcript input và khoảng im lặng output, xem mục
  *GPT-Live*): `user_turn_id` trên mọi output và `TurnDoneEvent`; `UserSpeechOutput`
  (chỉ LIVE_MODE) cho onset / endpoint / transcript của người dùng;
  `InterruptedOutput` `output_reset` ở lời đầu của một reply và `server_interrupt`
  khi bị ngắt; `OutputEvent.gen` để `receive()` bỏ audio của reply đã bị thay;
  `note_server_activity()` nuôi watchdog im lặng; `TurnDoneEvent.execution_completed`
  chỉ true khi có terminal thành công từ provider và không bị ngắt. Cái gì
  `voice_service` / `live_history` / `live_voice` bám vào đều không phụ thuộc
  provider.

### An toàn connection của OpenAI

Agent OpenAI dùng chung 1 `RealtimeConnection` giữa thread send và recv. Mọi
thao tác ghi vào connection, việc swap connection khi reconnect, và teardown đều
chạy dưới reentrant lock (`_conn_lock`); vòng lặp recv blocking dài chạy **ngoài**
lock trên một snapshot của connection để send audio không bị starve giữa lượt.
Thread recv chỉ lấy lock **ngắn** cho lần ghi duy nhất của nó —
`conversation.item.truncate` khi barge-in (`_truncate_item`) — và bỏ qua nếu
snapshot không còn là connection hiện tại. Lock reentrant vì một thao tác send
giữ lock trong khi kích `_safe_response_create`, hàm này lấy lại lock trên cùng
thread; riêng phần chờ `_turn_done` của nó chạy trước khi lấy lock để recv loop
vẫn drain được event. Reconnect là idempotent (re-check `_connected` trong lock)
và `_drop_connection()` chỉ null connection nếu nó vẫn là connection hiện tại —
nên 2 thread không thể tear down / dựng lại connection của nhau.

### GPT-Live (`gptlive`): full-duplex, turn được tổng hợp

`gpt-live-1` (GA trong API từ 2026-09-10) là một **API khác**, không phải model
mới trên `/v1/realtime`: transport, event, tool và cách tính tiền đều khác
Realtime API, nên nó có adapter riêng — `voice_agent/gpt_live.py` `GPTLiveAgent`,
chọn bằng `HAL_REALTIME_PROVIDER=gptlive` (`orchestrator._make_agent`), prompt
riêng `system_prompt_gptlive.md` (`PROVIDER_PROMPT_PATHS`), log usage riêng
`gptlive_usage.log`, lỗi riêng `GPTLiveError`. Docstring module của `gpt_live.py`
là nguồn chân lý cho mục này. Tiền đề: đường truyền Live **không có** thứ nào mà
contract `VoiceAgentBase` được xây quanh — không ranh giới lượt, không event VAD,
không event ngắt lời, không tool, không commit — nên adapter **tự tổng hợp** từng
thứ và nói rõ ở từng chỗ. Phần dưới đi qua từng cái một.

**Transport.** `client.live.connect()` (SDK `openai>=3.14.1`,
`openai.resources.live`) mở WebSocket chính tới `/v1/live/sessions`; adapter gửi
`session.start` (client `event_id` `hal-start`) rồi **chờ `session.started`**
trước mọi lệnh khác — server từ chối lệnh gửi trước đó, nên thread send chặn tối
đa `start_timeout_s` (10 s) và quá hạn thì bỏ lệnh đó (log `GPT-Live session not
started within 10s — dropping send`). Payload `session.start` (`_build_session`,
được test đối chiếu với `openai.types.live.SessionConfig` của SDK):

```
{model, instructions, audio: {format: {type: "audio/pcm", rate}, output: {voice}}, delegation: {type: "client"}}
```

Một WebSocket Live chỉ có **một** định dạng PCM cho cả hai chiều (16000 hoặc
24000 Hz), nên `output_sample_rate == sample_rate`. `delegation: {type: "client"}`
là chủ ý: model phát `session.delegation.created` và **tiến trình này** (→ main
agent) làm việc; `responses` sẽ giao việc cho một model OpenAI-hosted thay vì bộ
não của thiết bị. Trên đường truyền, client gửi `session.input_audio.append`
(base64 PCM16 mono, `event_id` `hal-audio`); server phát `session.output_audio.delta`,
`session.output_transcript.delta`, `session.input_transcript.delta` (mảnh có
`start_ms`/`end_ms`, tài liệu nói thẳng là "do not define complete turns"),
`session.delegation.created`, `session.usage.updated`, `session.closed`, `error`,
`info`; các event khác (`session.updated`, `session.*.appended`, `input_audio.muted`
/ `unmuted`, `response.event`) bị bỏ qua. SDK không tự reconnect (adapter không
truyền `on_reconnecting`); loop send/recv của adapter tự lo hồi phục với cùng kỷ
luật của hai provider kia: backoff 2 s → tối đa 60 s, fail-fast. `disconnect()`
gửi `session.close()` trước khi đóng socket để server trả `session.closed` kèm
usage cuối; session mở lại không thừa kế chủ input hay delegation nào
(`_reset_turn_state`).

**Cấu hình** (`hal/config.py`, block "Realtime: GPT-Live"; `GPTLiveConfig` trong
`realtime/config.py`; bảng env ở mục *Cấu hình*):

- `REALTIME_GPTLIVE_API_KEY` = env `OPENAI_API_KEY` > `realtime.gptlive.api_key` >
  `realtime.api_key` chung > `llm_api_key`.
- `REALTIME_GPTLIVE_BASE_URL` = `HAL_GPTLIVE_BASE_URL` > `realtime.gptlive.base_url`
  > **base URL của OpenAI Realtime** (`HAL_OPENAI_REALTIME_BASE_URL` >
  `realtime.base_url` > `<llm_base_url>/ws/openai`). SDK nối thêm `/live/sessions`
  và chuyển sang `wss`, nên qua proxy hai provider OpenAI gọi hai path anh em với
  cùng header `Authorization: Bearer`: Realtime →
  `wss://…/ws/openai/realtime?model=<model>`, GPT-Live →
  `wss://…/ws/openai/live/sessions` (model nằm trong payload `session.start`, không
  trên URL). Theo tài liệu tích hợp thiết bị của BFF (*GPT-Live voice sessions —
  WebSocket*), route đang ở **staging**
  (`wss://campaign-api.staging.autonomousdev.xyz/api/v1/ai/v1/ws/openai/live/sessions`,
  nhánh `feat/openai-live-proxy`), chưa lên production. Thiết bị có `llm_base_url`
  production muốn test thì đặt
  `HAL_GPTLIVE_BASE_URL=https://campaign-api.staging.autonomousdev.xyz/api/v1/ai/v1/ws/openai`
  trong `/opt/hal/.env`. Chừng nào production chưa serve path này, connect 404,
  agent log `Reconnect failed … next retry in ~Ns` và ở lại backoff 2 s → 60 s
  (turn rơi về main agent). Hợp đồng với relay và cách adapter đáp lại:
  - **Auth.** Thiết bị không bao giờ giữ key OpenAI: SDK gửi key đã resolve
    (`llm_api_key` = lobster key của thiết bị) qua `Authorization: Bearer`, BFF thay
    bằng credential OpenAI của nó ở upstream, tính usage dưới provider
    `openai_live` (`ceil(seconds × 17)` token mỗi dòng voice) và kiểm rate limit
    lúc connect lẫn sau mỗi dòng usage. Không forward query string (GPT-Live không
    nhận); chỉ đúng path này được mở — socket sideband/fork không.
  - **Correlation.** Mỗi lần connect gửi `x-request-id: hal-<12 hex>`; BFF gắn nó
    vào đầu mọi dòng usage của session (`<id>:voice-N`). Id nằm trong dòng HAL
    `Connecting to GPT-Live (… x-request-id=…)` và mọi dòng `gptlive_usage.log`
    (`request=`), nên log thiết bị đối chiếu được với `lobster_usage`.
  - **Close code.** HTTP `401` ở handshake = không có key; `4001` không key, `4002`
    BFF chưa cấu hình GPT-Live, `4029` thiết bị vượt hạn mức (`GPT-Live usage
    limit reached`, kể cả giữa phiên) — mấy mã này không tự hết, nên
    `_note_close_code` log ý nghĩa và đẩy backoff reconnect thẳng lên trần 60 s
    thay vì tăng dần. `1011` = BFF không nối được OpenAI (retry backoff thường);
    `1000`/`1008`/khác là close code của OpenAI forward nguyên; `1006` không có
    close frame = upstream biến mất (reconnect = session mới hoàn toàn).
  - **Đóng êm.** `disconnect()` gửi `session.close` rồi **tiếp tục đọc** tới khi
    `session.closed` về (tối đa `close_timeout_s`, 5 s) mới đóng socket, để relay
    ghi usage cuối là đã xác nhận (`final_confirmed: true`) thay vì tính theo đồng
    hồ của nó. Đường reconnect không bao giờ chờ (thread recv chính là thread phải
    đọc event đó).
  - **Audio liên tục.** Relay gặt hop idle (~126 s từng thấy trên đường Gemini),
    nên ở LIVE mode mic phải stream liên tục kể cả im lặng; idle park
    (`HAL_GPTLIVE_IDLE_PARK_S`) đóng session sạch sẽ trước mốc đó.
- `HAL_GPTLIVE_MODEL` (`gpt-live-1`), `HAL_GPTLIVE_VOICE` (`marin`; 13 giọng
  `gpt-live-1` chấp nhận ở `session.start` — enum `GPTLiveVoice`, Go
  `RealtimeGPTLiveVoiceList` giữ khớp: marin, quartz, ripple, vesper, willow,
  stone, gleam, meridian, bossa, tempo, beacon, delta, cinder; literal
  `BuiltInVoice` của SDK rộng hơn nhưng các tên chỉ-Realtime như alloy/ash bị
  Live từ chối nên cố ý không liệt kê), `HAL_GPTLIVE_SAMPLE_RATE` (24000;
  24000 giữ giọng full quality, 16000 giảm nửa băng thông uplink).
- Knob tổng hợp: `HAL_GPTLIVE_TURN_GAP_MS` 800, `HAL_GPTLIVE_INTERRUPT_GAP_MS` 400,
  `HAL_GPTLIVE_INPUT_GAP_MS` 1500, `HAL_GPTLIVE_COMMIT_SILENCE_MS` 600,
  `HAL_GPTLIVE_DELEGATION_WAIT_MS` 500 — ý nghĩa từng cái ở các đoạn sau.
- **Không có knob reasoning**: model Live không có; Go `ValidateRealtimeKnobs` từ
  chối mọi giá trị reasoning cho `gptlive`, `GET /api/device/realtime-options`
  trả list reasoning rỗng cho nó và web ẩn selector.

**Ranh giới lượt (tổng hợp).** Không có `response.done` / `turn_complete`. Thread
watchdog `gptlive-watchdog` (tick 50 ms) phát `TurnDoneEvent(execution_completed=True,
user_turn_id=<chủ>)` khi không có `session.output_audio.delta` /
`session.output_transcript.delta` nào tới trong `turn_gap_ms`. Deadline đo từ
output **cuối**, output liên tục cứ đẩy deadline đi, nên model đang nói không bao
giờ bị adapter cắt. `OutputEvent.gen` tăng ở đầu mỗi đợt trả lời và mỗi lần ngắt,
để `receive()` bỏ audio của reply đã bị thay. `execution_completed` chỉ true khi
ranh giới do gap (`reason == "gap"`) và không bị ngắt; ranh giới do
`_fail_fast_turn` (lỗi transport) luôn false.

**Lời người dùng (tổng hợp).** Không có event VAD. `UserSpeechOutput` (chỉ
LIVE_MODE, key `gptlive-<hex>`, `method="provider_transcript"`, **không bao giờ**
có `endpoint_at`) được dựng từ các mảnh `session.input_transcript.delta`. Một
input turn mới mở khi chưa có turn nào mở, khi turn trước đã được trả lời, hoặc
khi `start_ms − end_ms trước > input_gap_ms` (mốc trên timeline của session, nên
hai câu cách nhau xa vẫn tách được dù model chưa đáp câu đầu). Khi model bắt đầu
trả lời, adapter phát một `UserSpeechOutput(transcript_finished=True)` chỉ-hoàn-tất
(input coi là xong vì model đã đáp), và `user_turn_id` của reply được **đóng
băng** ở output đầu tiên của đợt — rỗng cho một câu model tự nói không ai hỏi.
Sau ranh giới, input đã được trả lời không còn là chủ của gì nữa: câu tự nói sau
đó không được gán vào nó. Như hai provider kia, key chỉ được **công bố** ở
LIVE_MODE (`_public_key`); đường lượt nhận `user_turn_id` rỗng nhưng vẫn giữ
transcript cho message delegate.

**Barge-in (tổng hợp).** Không có event ngắt lời. Một mảnh input tới trong lúc
reply đang stream đánh dấu *overlap* và rút cửa sổ ranh giới xuống
`interrupt_gap_ms`. Nếu model rồi im lặng qua cửa sổ đó, reply được coi là **bị
ngắt**: recv queue được rút cạn (giữ `UserSpeechOutput` / `ExecutionOutput` /
`InterruptedOutput` `server_interrupt`; một `TurnDoneEvent` đang xếp hàng biến
thành `ExecutionOutput` ở LIVE_MODE để giữ bằng chứng metric), gen tăng, phát
`InterruptedOutput(reason="server_interrupt", at=now, user_turn_id=<chủ>)`
(LIVE_MODE) rồi `TurnDoneEvent(execution_completed=False)`; log `Reply interrupted
by the user — dropped N queued output(s), gen=N`. Nếu model vẫn nói tiếp quá
`interrupt_gap_ms` kể từ lúc overlap thì đó là backchannel — cờ overlap được xoá,
cửa sổ về `turn_gap_ms`. `InterruptedOutput(reason="output_reset")` đi trước chunk
transcript đầu tiên của mỗi reply, như hai provider kia.

**Tool: không có ở tầng Live.** Session chạy **client delegation**: khi model
quyết định yêu cầu thuộc backend, nó phát `session.delegation.created` (chỉ
metadata: `id`, `target`) và adapter dịch thành
`FunctionCallOutput(name="delegate_to_main", call_id=<delegation.id>,
arguments={"message": <transcript input đã gom>}, user_transcript=<cùng text>)` —
orchestrator xử lý y như tool call của Gemini (`DelegateSignal`;
`test_delegation_reaches_the_orchestrator_as_a_delegate_signal` chạy xuyên
`RealtimeOrchestrator.stream_output`). Delegation không mang text task; text là
transcript input gom cho user turn hiện tại, và delegation **có thể tới trước khi
câu nói xuất hiện đủ trong transcript** (BFF doc §6), nên không bao giờ forward
ngay: watchdog forward khi transcript input đã im 250 ms (`_DELEGATION_SETTLE_S`),
hoặc chậm nhất ở hạn cứng `delegation_wait_ms` với những gì đã nghe (log
`Delegation <id> — settling the transcript (N chars so far, up to 500ms)`); hết
hạn mà vẫn rỗng thì **vẫn forward** —
orchestrator trả `{"error": "message must not be empty"}` và adapter chuyển cho
model như một handoff thất bại. Event còn mang `offset_ms` (vị trí
trên timeline session) nhưng adapter không dùng — nó gom theo user turn thay vì
cắt transcript quanh mốc đó. Delegation có `target` khác `client` bị bỏ qua:
`delegation: {type: "responses"}` là cách API chạy tool trong một backend
Responses hosted (function call tới trong `response.event`, kết quả trả qua
`response.item.create` + `response.create`) — không phải bộ não của thiết bị,
nên không dùng. `express_emotion`, `reject_turn`, `end_conversation`, `look`
**không tồn tại** trên provider này — log một dòng lúc khởi tạo, kết quả cho
chúng bị bỏ qua. Không nhận input ảnh (`ImageInput` bị bỏ với một cảnh báo duy
nhất).

**Phản hồi cho model** đi qua `session.thinking.append` (context im lặng, có
`delegation_id`, nội dung cắt còn 1600 ký tự ≈ trần 500 token của API; client
`event_id` `hal-context` / `hal-delegation`):

- ack `{"result":"delegated"}` của orchestrator → "the main agent has taken this
  request … do not answer it yourself" gắn `delegation_id`; delegation vẫn
  **pending**;
- `[TTS HISTORY] …` (câu trả lời main agent đã nói, tới qua `send_text`) → gắn vào
  delegation pending mới nhất và **đóng** nó — model biết user đã được trả lời và
  bởi ai;
- `send_text` khác (`[TURN CONTEXT]`, speaker correction) → `delegation_id: null`;
- kết quả không phải `"delegated"` (vd message rỗng) → "the handoff failed … answer
  directly" và delegation bị bỏ.

Tối đa 8 delegation pending được nhớ (`_MAX_PENDING_DELEGATIONS`); ack cho call
adapter chưa từng phát (tool khác) bị bỏ ở mức debug. API còn hai kênh append
khác cùng trần 500 token — `session.commentary.append` (model **nói ra** nội
dung) và `session.instructions.append` — adapter cố ý chỉ dùng `thinking`: câu
trả lời cho user là của main agent phát qua TTS thiết bị, model Live không được
đọc lại nó.

**`commit_audio()`.** Live không có commit — model tự quyết khi nào user nói xong.
LIVE_MODE → no-op (mic vẫn stream). Đường lượt → append `commit_silence_ms` PCM im
lặng (`event_id` `hal-silence`) để model nghe được câu nói đã kết thúc thay vì chờ
tạp âm phòng nói cho nó.

**Liveness.** `note_server_activity()` ở `session.started`, transcript input /
output, audio output và delegation — **không** ở `session.usage.updated`: tick
usage không nói gì về việc model có đang làm reply hay không.

**Lỗi.** Server `error` mang `client_event_id` của mình (`hal-audio`, `hal-silence`,
`hal-context`, `hal-delegation`) nghĩa là một lệnh của TA bị từ chối (context quá
500 token, audio trước `session.started`, …) → WARNING `GPT-Live rejected <id>` và
đi tiếp. `error` không có client id, hoặc trên `hal-start`, → `GPTLiveError` →
fail-fast (`TurnDoneEvent` ngay) + reconnect nền. Vòng lặp event kết thúc
(`session.closed`, reason `close_requested` | `expired` | `content` |
`remote_hangup` | `connection_lost`) → cũng fail-fast + reconnect.

**Latency log:** `Response latency: Nms (last_input_transcript->first_audio; full
duplex)` — chỉ xấp xỉ, vì bản thân transcript input đã trễ hơn tiếng nói.

**Usage / giá.** `session.usage.updated` và `session.closed` → dòng `[realtime]
GPT-Live usage: session=<id> request=<x-request-id> seconds=<s> est>=$<s/60×0.05> context=<usage_ratio %|-> (cumulative|final;
$0.05/min billed per second, delegated main-agent work billed separately)` trong
`gptlive_usage.log` (xem *Pricing & log usage*). Bill theo phút-session, không
theo token, nên một session **mở mà idle vẫn tốn tiền** — vì vậy idle park của
orchestrator áp cho cả provider này: `_idle_park_threshold()` trả
`HAL_GPTLIVE_IDLE_PARK_S` (mặc định `30`; `0` = tắt) cho `gptlive`,
`HAL_GEMINI_IDLE_PARK_S` cho Gemini và `0` (không bao giờ) cho OpenAI Realtime
(session idle của nó miễn phí). Quá ngần ấy giây không có turn,
`_maybe_park_idle_session` đóng transport (`[realtime] Ns idle (>= 30s) —
parking gptlive session …`); `prepare_turn()` của turn kế nối lại đồng bộ, audio
được buffer qua handshake, đúng như Gemini. Pre-turn recycle (chỉ Gemini) không áp.

**Hook base để mặc định, và lý do:** `end_turn()` no-op (không có gate `_turn_done`
— không gì chờ provider kết thúc lượt trước lần send kế, không có
`response.create`); `requires_fresh_session` False (delegation client không trả
lời không làm session từ chối input, khác `1008` của Gemini);
`output_sample_rate == sample_rate`.

**Prompt.** `system_prompt_gptlive.md` theo cấu trúc Live prompting guide: Role &
tone → Backchannel policy → Interruption policy → When NOT to speak → Delegation
policy ("Backend tools" / "Delegate to the backend when" / "Do not delegate when")
→ các luồng context → ví dụ. Không có cú pháp tool call, không tag ElevenLabs. Các
dòng `delegate_to_main(message=…)` trong ví dụ gọi tên cú handoff im lặng để
`test_realtime_find_delegation.py` dùng chung soi được; prompt nói rõ nó không bao
giờ được đọc thành tiếng.

**Test:** `hal/test/test_gptlive_live_provider_metrics.py` (22 test) lái
`_pump_events` bằng event `session.*` và gọi `_fire_boundary()` ở chỗ watchdog sẽ
gọi; gồm một test xuyên `RealtimeOrchestrator.stream_output` và một test đẩy JSON
hình dạng wire qua parser của SDK (`LiveConnection.parse_event`) để đường attribute
adapter dùng khớp pydantic types.

> **Chưa kiểm chứng trên thiết bị (2026-09-16).** Không có OpenAI key trên thiết
> bị hay Mac dev, và proxy chưa serve route `…/ws/openai/live/sessions`, nên
> adapter mới chỉ được xác nhận bằng SDK-schema check và unit test — chưa nối API
> thật lần nào. Route proxy đang được thêm; khi lên thì thiết bị không cần đổi gì.

### Pipecat v1 (`pipecat_v1`): pipeline chạy trên thiết bị, đầu ra text

`voice_agent/pipecat_v1.py` `PipecatV1Agent` là provider không có provider:
"server" ở đây là một pipeline [Pipecat](https://github.com/pipecat-ai/pipecat)
chạy ngay trong HAL, do `voice_agent/pipecat_pipeline.py` dựng trên một event
loop asyncio riêng (thread `pipecat-io`). Audio đi vào, **text** đi ra và TTS
của chính HAL đọc nó — model không bao giờ sinh audio, nên
`REALTIME_NATIVE_AUDIO` vô nghĩa ở đây và không có knob voice. Nó ra đời sau
phân tích đường audio ngày 2026-09-18 (AEC, trạng thái playing và quyết định
barge-in chỉ có thể nằm trên thiết bị đang phát audio; xem
`audio-path-design.md`), nên toàn bộ pipeline ở lại trên robot và chỉ có lời
gọi model rời khỏi nó. Chọn bằng `realtime.provider = pipecat_v1`; prompt của
nó là `system_prompt_pipecat.md` (`PROVIDER_PROMPT_PATHS`) — prompt OpenAI kèm
một đoạn mở đầu nói rằng model đọc **transcript** STT chứ không phải audio, và
viết text cho TTS.

**Cài đặt.** `pipecat-ai` là extra tùy chọn `pipecat` trong
`hal/pyproject.toml`. Setup lamp và image Pi/OrangePi chọn
`uv sync --python 3.12 --extra hardware --extra aec --extra pipecat`;
OTA cho thiết bị không phải Reachy giữ cùng lựa chọn này. Developer chạy riêng
`uv sync` không tự chọn extra. Nó không phải dependency cứng: package lõi kéo theo `numba` +
`llvmlite` (~140 MB), `resampy`, `nltk` và ghim `onnxruntime ~=1.24`, xung đột
với `onnxruntime==1.27.0` của extra `reachy` — hai extra được khai báo loại trừ
lẫn nhau (`[tool.uv] conflicts`) vì chúng không bao giờ ở chung một thân máy.
Không có package thì `PipecatV1Agent` raise `PipecatV1Error` lúc connect và
orchestrator coi provider là unavailable (fallback về main agent), y như khi
vendor sập. Silero VAD, Smart Turn v3 (`smart-turn-v3.2-cpu.onnx` đóng gói sẵn,
8.7 MB) và LLM service tương thích OpenAI đều nằm trong package lõi; không cần
extra STT của vendor nào (xem dưới).

**Pipeline** (một pipeline mỗi provider session, dựng lại ở mỗi lần
`recover_session`):

```
PipelineWorker.queue_frame ─▶ HALSTTService ─▶ user aggregator ─▶ OpenAILLMService ─▶ EventSink ─▶ assistant aggregator
   InputAudioRawFrame          (device STT)     (VAD / turns)      (text + tools)      (→ _ev_* on the agent)
```

**Không có transport**: HAL sở hữu mic và loa, thread send của agent đẩy từng
`AudioInput` (float32 → PCM16) vào đầu pipeline bằng `worker.queue_frame`.
`EventSink` là một `FrameProcessor` pass-through, báo frame cho agent bằng lời
gọi method thường (`_ev_text`, `_ev_response_started/ended`,
`_ev_calls_started`, `_ev_call_result`, `_ev_user_turn_started/stopped`,
`_ev_interruption`, `_ev_error`, metrics), nên toàn bộ contract
`VoiceAgentBase` được tạo ra trong `pipecat_v1.py` mà không cần `import
pipecat` và được unit-test ở đó (`hal/test/test_pipecat_v1_agent.py`, 17 test).

**STT là của pipeline, chạy trên provider của chính thiết bị.**
`pipecat_stt.py` `HALSTTService` là một `STTService` của Pipecat bọc
`STTProvider` của HAL (`AutonomousSTT` — relay tương thích Deepgram sau
campaign-api trên `llm_api_key` — hoặc `DeepgramSTT`): các STT service có sẵn
của Pipecat quay số tới endpoint công khai của vendor bằng key vendor mà thiết
bị không có. `VoiceService` trao provider của nó cho orchestrator
(`stt_provider=`), orchestrator chuyển tiếp cho agent, nên transcription dùng
đúng relay, key, model và boost term như đường turn-based; không có provider
nào thì agent tự dựng một `AutonomousSTT` từ `HAL_PIPECAT_STT_*`. Mọi I/O với
provider (open, send, close) chạy trên một thread sender `pipecat-stt` duy
nhất để `close()` blocking (join thread recv tới 15 s trong khi server flush
transcript cuối) không bao giờ làm kẹt event loop asyncio; transcript được trả
về loop bằng `call_soon_threadsafe` dưới dạng
`TranscriptionFrame(finalized=True)` cho final và `InterimTranscriptionFrame`
cho interim, và về agent (`_ev_transcript`) cho
`FunctionCallOutput.user_transcript` và `UserSpeechOutput` ở chế độ live.

**Cả hai hình dạng `HAL_LIVE_MODE`, một class** — mode được đọc lúc khởi tạo
(`config.LIVE_MODE`), giống cách phần còn lại của HAL loại trừ lẫn nhau theo
thiết kế:

| | `HAL_LIVE_MODE=false` (turn-based) | `HAL_LIVE_MODE=true` (live) |
|---|---|---|
| ai kết thúc lời nói của user | VAD của HAL + gate kết thúc lượt tạm thời dùng chung (`HAL_TURN_END_*`) → `append_audio` × N, `commit_audio` | pipeline: Silero VAD mở lượt (`HAL_PIPECAT_VAD_*`), Smart Turn v3 đóng lượt (`HAL_PIPECAT_SMART_TURN`, không thì timeout im lặng `HAL_PIPECAT_SILENCE_TIMEOUT_S`) |
| chiến lược lượt | `ExternalUserTurnStrategies`: frame đầu tiên sau một commit queue `ProposedUserStartedSpeakingFrame` (đồng thời broadcast một interruption, hủy reply còn đang stream từ lượt trước); `commit_audio` queue `ProposedUserStoppedSpeakingFrame` **và** một `STTFinalizeFrame`. Stop strategy kế thừa (`_CommittedTurnStopStrategy`) finalize ngay khi STT final đã commit về, thay vì chờ timeout gom 0.5 s mặc định | mặc định của Pipecat: bắt đầu theo VAD / transcription, dừng theo turn analyzer; user cất tiếng khi model đang trả lời thì broadcast một interruption |
| STT session | **theo từng lượt**: mở ở frame đầu, đóng bởi `STTFinalizeFrame` — một system frame được đẩy *xuyên qua pipeline* phía sau audio để nó chỉ tới stage STT sau mọi frame của câu nói (báo thẳng cho thread sender từng chạy đua với audio còn trên đường và finalize một session rỗng; sửa 2026-09-18). `close()` gửi CloseStream, server flush final. Lượt mà session đóng lại không có chữ nào thì kết thúc ngay (`TurnDoneEvent(execution_completed=False)`) để HAL fallback bằng transcript của chính nó thay vì chờ hết `REALTIME_RECV_QUEUE_TIMEOUT_S` | **một session dài** suốt đời pipeline, mở lại nếu relay rớt, giữ sống bằng `KeepAlive` mỗi 5 s khi không ai nói; end-of-turn riêng của provider (Flux `TurnInfo`) chỉ về như một final |
| `UserSpeechOutput` | không (đường turn không có metadata live) | bắt đầu lượt (`method="server_vad"`), mỗi STT **final** dưới dạng phần chưa được phát (`method="provider_transcript"`, `transcript_finished=True`), kết thúc lượt (`endpoint_at`) — cùng các key mà live pump đọc từ Gemini/OpenAI. Interim không bao giờ được đưa ra ngoài: giả thuyết STT là tích lũy và bị viết lại giữa câu (`place a music` → `play some music`), còn `LiveHistory.input` của pump nối các chunk lại mà không rút lại được — trên lamp-ee17 bản viết lại lọt vào text `[HANDLED]`. Interim vẫn nuôi MinWords và `note_server_activity()` |
| barge-in | frame đầu của câu nói kế tiếp ngắt pipeline; không phát `TurnDoneEvent` cho nó (orchestrator đã rời lượt cũ từ lâu và một terminal muộn chỉ có thể kết thúc rỗng lượt mới) | `InterruptedOutput(reason="server_interrupt", at=…)` + `TurnDoneEvent(execution_completed=False)`, `gen` được tăng. Khi model đang sinh, lượt mới cần đủ `HAL_PIPECAT_MIN_WORDS` từ đã transcribe mới mở được (`_BusyAwareMinWordsStrategy`); không thì một tiếng bật ra một từ ngay sau câu hỏi sẽ hủy reply |
| commit | đề xuất stop + finalize STT; commit **không có audio** kể từ lần trước kết thúc lượt ngay | bỏ qua (session live không bao giờ commit) |

Lưu ý đường turn-based chạy STT **hai lần** trên cùng một câu nói — session
riêng của HAL (wake word, noise guard, `[transcript]`, dispatch) và của
pipeline — nên chi phí STT trên đường đó tăng gấp đôi; chế độ live không có
STT session của HAL, nên ở đó session của pipeline là cái duy nhất. Mode mà
provider này thực sự sinh ra để phục vụ là live.

**Tool được bắc cầu một-một.** Mọi tool của orchestrator (`delegate_to_main`,
`reject_turn`, `express_emotion`, `end_conversation`, `web_search` — chỉ pipecat,
xem bên dưới; `look` chỉ có ở Gemini và không bao giờ được đăng ký) được đăng
ký lên LLM service dưới dạng `FunctionSchema` + handler. Handler phát `FunctionCallOutput(name,
arguments=<JSON>, call_id=<tool_call_id>, user_transcript=<các STT final của
lượt này>)` rồi chờ `FunctionCallResultInput` của orchestrator cho đúng
`call_id` đó (giới hạn bởi `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S`, quá hạn thì
model nhận `{"error": "no result from the device"}` và không có follow-up để
pipeline không bao giờ kẹt); `trigger_response` của kết quả trở thành `run_llm`
của Pipecat. Hai ngoại lệ được quyết định theo tên: **`delegate_to_main` và
`reject_turn` không bao giờ chạy lại model** (`_NO_FOLLOWUP_TOOLS`).
Orchestrator ack hai tool đó với `trigger_response=True` (một luật
pending-tool-call của Gemini, không phải mong muốn có reply) rồi rời lượt
(`end_turn()` + `DelegateSignal` / `RejectSignal`), nên một follow-up chỉ có
thể được đọc thành câu trả lời cũ ở lượt *kế tiếp* — lần smoke chạy local từng
trả lời "How are you?" bằng câu follow-up về thể loại nhạc của lần delegate
trước, trước khi có luật này. `express_emotion` (`trigger_response=True` từ
bản vá ack) và `end_conversation` thì có chạy lại model. Vì model 35B-A3B nặng
về recency, luật hành động được nêu hai lần: dưới dạng `## FINAL RULE` nối vào
cuối system instruction, và dưới dạng một dòng `[RULE] …` role user
(`_TOOL_RULE`, ~60 token) được bơm lại ngay trước mỗi câu nói
(`PipelineHandle.remind_tools`, từ frame đầu tiên trên đường turn và từ
`_ev_user_turn_started` ở chế độ live, để nó nằm trước khi aggregator nối
transcript vào). Nó là dòng role user vì relay Qwen từ chối system message ở
bất kỳ vị trí nào ngoài đầu tiên (`System message must be at the beginning`,
HTTP 400). Không có nó, trên lamp-ee17 lần boot có summary realtime-memory dài
8 k ký tự trả lời "please play some music" bằng "You got it, what vibe?" trong
khi cùng pipeline với summary 3 k thì delegate; có nó, yêu cầu được delegate.

**Web search — tool `web_search` (chỉ pipecat).** Relay Qwen không có search
hosted, nên trước khi có tool này mọi dữ kiện công khai theo thời gian thực
(thời tiết, tin tức, tỉ số, giá cả) đều tốn một vòng `delegate_to_main`. Khi
`HAL_PIPECAT_WEB_SEARCH` bật (mặc định) và `realtime.provider = pipecat_v1`,
orchestrator đăng ký thêm một function tool `web_search(query)`
(`WEB_SEARCH_TOOL`, cổng `_web_search_available()`), bắc cầu như các tool khác.
Handler của nó (`_handle_web_search_call`) chạy `hal/realtime/web_search.py`
`grounded_search`: một POST tới relay Google-Search của campaign-api
(`HAL_PIPECAT_SEARCH_URL`, một endpoint Gemini *Interactions*) với `{"model":
<HAL_PIPECAT_SEARCH_MODEL>, "input": <query>, "tools": [{"type":
"google_search"}]}` và key chat làm Bearer. Phản hồi là một Interaction —
`status`, và `steps[]` gồm `google_search_call` (các query Google đã chạy),
`google_search_result`, `thought`, `model_output` — `parse_interaction` lấy text
của `model_output` (một **câu trả lời** đã grounded, không phải danh sách link),
bỏ markdown Gemini viết, cắt ở ranh giới câu tại `MAX_ANSWER_CHARS` (1200) và
gom các tiêu đề trích dẫn khác nhau (`wikipedia.org`, …). Model nhận `{"result":
<answer>, "sources": [...]}` với `trigger_response=True` → `run_llm=True`:
response follow-up chính là câu trả lời được đọc, bằng ngôn ngữ của người dùng,
và kết thúc của nó đóng lượt. Lookup chặn vòng output của lượt (~4 s đo ngày
2026-09-21) trong khi tool future của pipeline chờ, nên
`HAL_PIPECAT_SEARCH_TIMEOUT_S` (10 s) phải nhỏ hơn
`HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S` (15 s); trên đường turn-based filler
khoảng lặng (`REALTIME_FILLER_DELAY_S`, 1,5 s) và thinking cue đã che khoảng
chờ. Mọi thất bại (timeout, relay trả khác 200, câu trả lời rỗng) vẫn được ack
— `{"error": …, "hint": "…delegate_to_main…"}` — để model hoặc nói là không
kiểm tra được hoặc delegate câu hỏi, đúng chỗ nó từng đi trước khi có tool;
`query` rỗng bị từ chối mà không gửi request. `system_prompt_pipecat.md` xếp
lookup công khai vào *Direct Home Run* (search rồi nói ngay trong cùng lượt;
không viết text trước khi gọi; mỗi câu hỏi một search; dữ liệu live riêng
tư/tài khoản vẫn delegate) và không còn đẩy thời tiết/tin tức sang main khi tool
tồn tại. Gemini và GPT-Live không bao giờ thấy tool này: chúng tự ground ở phía
mình. `realtime.pipecat_v1.web_search` trong config.json
(`PipecatV1Realtime.WebSearch *bool`, nil → mặc định HAL là bật) giữ override
của operator qua các lần os-server ghi lại config. Trang Settings web
(`/setting#realtime`, provider *Pipecat v1*) hiện nó dưới dạng checkbox **Web
search**: nó đi trong block `realtime` của `PUT /api/device/config` / MQTT
`realtime.set` với tên `web_search` (chỉ pipecat_v1 — trang chỉ gửi với
provider đó, server từ chối với provider khác), ghi vào sub-object như một
override tường minh, restart HAL như mọi chỉnh sửa realtime khác, và được đọc
lại ở dạng đã resolve là `realtime.web_search` trong `GET /api/device/config`
(`RealtimeWebSearch()`; bỏ trống với provider khác). Được ghim bởi
`hal/test/test_realtime_web_search.py` (20 test: hình dạng phản hồi của relay,
contract ack, cổng đăng ký).

**Ranh giới lượt và generation.** `OutputEvent.gen` là generation của
**user-turn** — tăng khi một user turn bắt đầu và khi có interruption, không
bao giờ tăng theo từng response — nên follow-up sau tool result vẫn nằm trong
lượt của nó. `TurnDoneEvent` được phát ở `LLMFullResponseEndFrame` chỉ khi
không còn tool call nào đang chờ (`FunctionCallsStartedFrame` được broadcast
**trước** end frame, các `FunctionCallInProgressFrame` có thể tới sau nó); call
mà kết quả về với `run_llm=False` tự kết thúc lượt khi call pending cuối cùng
được trả lời, call với `run_llm=True` giao việc đó cho end của response
follow-up. `InterruptedOutput(reason="output_reset")` đi trước reply đầu tiên
của mỗi user turn (không phải mỗi response — follow-up sau tool result không
được reset hàng đợi TTS của câu đang phát). `end_turn()` **rào** generation
hiện tại: `_newest_output_gen` được nâng vượt qua nó và `TurnDoneEvent` của nó
bị nuốt, nên bất cứ thứ gì lượt bị rào còn sinh ra sẽ bị `receive()` bỏ thay
vì kết thúc hay được đọc ở lượt kế. `note_server_activity()` chạy ở mọi mảnh
transcript, text delta, tool frame và metrics frame, nên lượt mà STT hay LLM
còn đang làm việc được watchdog silent-turn giữ sống. Lỗi pipeline tới agent
qua event `on_pipeline_error` của worker (Pipecat đẩy `ErrorFrame` *ngược
dòng*, nên một sink nằm sau LLM không bao giờ thấy nó): lỗi không fatal khi
một response đang mở sẽ kết thúc lượt với `execution_completed=False`
(fallback về main agent) thay vì để end frame của response báo một lượt rỗng
đã hoàn tất; lỗi fatal, hoặc pipeline run kết thúc, làm rớt session
(`_recv_loop` poll `PipelineHandle.alive` mỗi giây và dựng lại với backoff
2 s → 60 s thông thường). Idle parking không áp dụng (`_idle_park_threshold` →
0): không có session vendor để park, và socket STT chỉ sống trong một lượt
hoặc một live call.

**LLM.** `OpenAILLMService` nối tới `HAL_PIPECAT_BASE_URL` (mặc định
`https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1`, relay Qwen độ trễ
thấp; gateway EternalAI `https://vibe-agent-gateway.eternalai.org/v2` serve
cùng model không cần key và là cái các smoke test local dùng), model
`HAL_PIPECAT_MODEL` (`qwen/qwen3.6-35b-a3b`), `temperature` 0.7, `max_tokens`
300, system prompt làm `system_instruction` của service (system message khởi
tạo trong `LLMContext` đã deprecated từ Pipecat 1.9). Các model Qwen3 có thể
phát `reasoning` trước `content` mà Pipecat chỉ stream `content`, nên thinking
bị tắt qua `extra_body.chat_template_kwargs.enable_thinking=false`
(`HAL_PIPECAT_DISABLE_THINKING`, đặt `false` cho endpoint từ chối extra body).
Các `TextInput` `[TURN CONTEXT]` / `[TTS HISTORY]` trở thành
`LLMMessagesAppendFrame(role=user, run_llm=False)` — được ghi lại, không bao
giờ là một lượt. `ImageInput` bị bỏ kèm warning. Đo trên Mac dev qua gateway
EternalAI (STT giả, nên chỉ là LLM): text đầu tiên 0.8–1.0 s sau khi hết lượt,
~5.8 k prompt token mỗi lượt (chính prompt), 2–30 completion token.

**Log usage.** `pipecat_usage.log` (logger `hal.realtime.usage.pipecat`,
`server_support/log_setup.py`): một dòng `first text +N.NNs after turn end` mỗi
reply (turn-based: sau commit; live: sau end-of-speech), các dòng `ttfb` theo
từng stage của Pipecat, và `llm usage model=… in=N out=N` từ usage metrics của
LLM service — chỉ đếm token, không ước tính cost.

**Kiểm chứng trên thiết bị (lamp-ee17, 2026-09-18, `HAL_LIVE_MODE=true`,
uplink `mute`).** Dựng pipeline (nạp ONNX Silero + Smart Turn) ~12 s trên A55;
một live call do VAD "chuông cửa" của HAL mở, Flux transcribe "Hey, lamb. What
is the capital of France?", pipeline trả lời `Paris.` với text đầu tiên 1.3–1.8
s sau khi hết tiếng nói (11.5 k prompt token qua relay Qwen) và audio đầu tiên
của ElevenLabs ~1 s sau đó; "Hey, lamp. Please play some music for me." thành
`delegate_to_main(message="Please play some music for me.")` →
`[voice-instruction]` + `[transcript]` sang os-server → main agent trả lời. Ba
thứ lần chạy này đã thay đổi: `HAL_LIVE_UPLINK_DURING_PLAYBACK` phải là `mute`
trên phần cứng này — với `cancelled` pipeline transcribe chính câu trả lời của
lamp (`Its parents` cho "It's Paris") và tự trả lời mình bốn vòng, đúng giới
hạn AEC 7–13 dB đã đo với GPT-Live; sàn VAD chuyển sang giá trị far-field
(`0.85` / `0.7`) sau khi lamp trả lời một cuộc trò chuyện ở phía bên kia
phòng; và `MinWords` / dòng `[RULE]` ở trên. Cài trên lamp là
`uv sync --python 3.12 --extra hardware --extra aec --extra pipecat` (kéo
`onnxruntime 1.24.4`, `numba`, `llvmlite`; ~30 s từ cache).
`/var/log/hal/pipecat_usage.log` giữ các dòng theo từng lượt.

**Hạn chế đã biết (2026-09-18).** Chi phí CPU của Smart Turn v3 trên A55 chưa
đo (nó chạy một lần mỗi khoảng ngừng, không phải mỗi frame); relay Qwen bỏ
payload `tool_calls` khỏi completion **không streaming** (`finish_reason:
"tool_calls"` với message rỗng) — Pipecat stream, nên pipeline không bị ảnh
hưởng, nhưng một lần probe không streaming sẽ trông như không có tool; model có
delegate hay không vẫn phụ thuộc vào summary realtime-memory của lần boot đó
(xem ghi chú `[RULE]`) — `HAL_PIPECAT_TEMPERATURE` là knob còn lại; không có
image input; `test_pipecat_v1_agent.py` bao contract, không bao pipeline —
pipeline được kiểm bằng các lần smoke chạy local (turn mode với STT giả: "What
is two plus two?" → `Four.`, "Please play some music" →
`delegate_to_main(message="Play some music")`; live mode với giọng tổng hợp
qua Silero + Smart Turn: "What is the capital of France?" → `Paris.`, "Please
turn the brightness up a bit." → delegate kèm transcript) và lần chạy trên
thiết bị ở trên.

## Pricing & log usage

Gemini và OpenAI Realtime ghi một dòng token/cost mỗi turn; GPT-Live ghi một dòng
giây/cost mỗi `session.usage.updated` và một dòng `final` ở `session.closed`. Mỗi
provider có log riêng dưới `/var/log/hal/` (rotating, 5 MB × 3):
`gemini_usage.log` (logger `hal.realtime.usage`), `openai_usage.log` (logger
`hal.realtime.usage.openai`), `gptlive_usage.log` (logger
`hal.realtime.usage.gptlive`) và `pipecat_usage.log` (logger
`hal.realtime.usage.pipecat`: độ trễ + số token, không có cost — xem *Pipecat
v1*) — các logger sau là logger **con** của logger đầu, `propagate=False` để
dòng của chúng không lọt vào `gemini_usage.log` lẫn `server.log`; cấu hình ở
`server_support/log_setup.py`, một file mỗi provider, so được từng dòng. Dòng log theo token mang đủ số token theo từng modality **và**
cost USD ước tính, nên rate có sai thì sau này vẫn tính lại được từ số token đã
ghi.

Dòng OpenAI (grep `[realtime] OpenAI usage` trong `openai_usage.log`):

```
[realtime] OpenAI usage: model=… in_text=N($…) in_audio=N($…) out_text=N($…) out_audio=N($…) +unattr(Nin/Nout) | cached=Ntok total=Ntok est_full>=$… est_cached>=$…
```

`unattr` là token OpenAI đếm nhưng không gắn text/audio (input ảnh) — không định
giá ở đây, nên `est` là **sàn** (`>=`). `cached` là prompt-cache hit
(`input_token_details.cached_tokens`), bill lại theo rate giảm; `est_cached` trừ
phần tiết kiệm đó (token cached không gắn modality được coi là text — sàn
system-instruction). `in_text` là context input bị bill lượt này: nó phình theo
history của session sống lâu và phải tụt ngay sau một lần recycle idle;
`cached=0` ở mọi turn nghĩa là cache không trúng (session churn) — đó là cờ đỏ
chi phí.

Dòng GPT-Live (grep `[realtime] GPT-Live usage` trong `gptlive_usage.log`):

```
[realtime] GPT-Live usage: session=<id> request=<x-request-id> seconds=<s> est>=$<usd> context=<usage_ratio %|-> (cumulative|final; $0.05/min billed per second, delegated main-agent work billed separately)
```

Không có token nào để đếm: `gpt-live-1` bill **$0.05 mỗi phút session, tính theo
giây** (`_GPTLIVE_USD_PER_MINUTE` trong `voice_agent/gpt_live.py`;
developers.openai.com/api/docs/models/gpt-live-1, verify 2026-09-16), `est` =
`seconds / 60 × 0.05`; `cumulative` là tick giữa phiên, `final` là số ở
`session.closed`. Việc main agent làm sau delegate tính riêng theo model của nó.
Hệ quả: session mở mà idle vẫn tốn tiền, nên idle park của orchestrator
(`HAL_GPTLIVE_IDLE_PARK_S`, mặc định 30 giây) là hàng rào chi phí trên provider này.

Bảng rate nằm trong code, key `(direction, modality)` tính USD trên 1M token —
`_GEMINI_RATES` trong `voice_agent/gemini_live.py`, `_OPENAI_RATES` trong
`voice_agent/openai_realtime.py` (khớp substring **theo thứ tự**: `mini` trước để
`gpt-realtime-2-mini` không rơi vào bảng full-size, `gpt-realtime-2` trước
`gpt-realtime` để model GA có ngày không rớt về rate text-out cũ). Model lạ rơi
về bảng đắt nhất (cost là trần, không bao giờ báo thiếu).

| Model | text in | audio in | text out | audio out | cached in (text / audio) | audio↔token | Nguồn |
|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash-native-audio` | $0.50 | $3.00 | $2.00 | $12.00 | text in ×0.10 (giảm 90%) | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `gemini-3.1-flash-live` | $0.75 | $3.00 | $4.50 | $12.00 | text in ×0.10 (giảm 90%) | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `gemini-3.8-live` / `-extended-thinking` | $0.75 | $3.00 | $4.50 | $12.00 | — | 25 tok/s | ai.google.dev pricing (verify 2026-09-17; giá khuyến mãi tới 31/12/2026, sau đó gấp đôi) |
| `*mini*` (vd `gpt-realtime-2-mini`) | $0.60 | $10.00 | $2.40 | $20.00 | $0.06 / $0.30 | — | developers.openai.com/api/docs/pricing (verify 2026-09-16) |
| `gpt-realtime-2` | $4.00 | $32.00 | $24.00 | $64.00 | $0.40 / $0.40 | — | developers.openai.com/api/docs/pricing (verify 2026-09-16) |
| `gpt-realtime` | $4.00 | $32.00 | $16.00 | $64.00 | $0.40 / $0.40 | — | developers.openai.com/api/docs/pricing (verify 2026-09-16) |

Cơ cấu chi phí giống nhau ở hai provider tính theo token (Gemini, OpenAI
Realtime): `in_text` chiếm áp đảo (system
prompt ~7-10k token + context session tích lũy bị re-bill mỗi turn, phình dần
tới khi session recycle — xem `HAL_REALTIME_SESSION_IDLE_RESET_S` /
`HAL_REALTIME_SESSION_MAX_TURNS`); token audio chỉ là phần lẻ. Gemini tính
thêm phí Google Search theo từng request grounded, ngoài token. GPT-Live nằm
ngoài cơ cấu này: chi phí tỉ lệ với thời gian session mở, không phụ thuộc độ dài
prompt hay số turn.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` bọc một session agent và là bề mặt duy
nhất mà `voice_service` giao tiếp:

| Method | Mục đích |
|--------|----------|
| `start()` / `stop()` | Dựng agent từ config, connect, summarize memory khi tắt |
| `append_audio(frame)` | Đẩy 1 frame mic (non-blocking) |
| `commit_audio()` | Báo hết câu nói (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, hoặc `DelegateSignal` (rồi dừng) |
| `send_text(text)` | Bơm context (turn context, TTS history) dạng user message không tạo response. Gemini Live bỏ qua bước này để tránh va chạm giữa SDK `clientContent` và lượt audio; OpenAI vẫn nhận; GPT-Live nhận dưới dạng `session.thinking.append` im lặng (`[TTS HISTORY]` còn đóng delegation đang chờ). |
| `send_function_result(call_id, output)` | Trả kết quả tool về model (GPT-Live: thành `session.thinking.append` gắn `delegation_id`) |
| `save_turn(user, agent)` | Lưu một lượt vào realtime memory |
| `available` / `sample_rate` | Trạng thái sẵn sàng + sample rate của provider |
| `rebuilding` / `wait_until_available()` | Quan sát và chờ ngắn session thay thế vốn đang kết nối, không tự khởi động thêm rebuild |

## Context manager

System prompt, định danh thiết bị, device memory, và skills catalog được lắp ráp
theo agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |
| `picoclaw` | `OpenClawContextManager` (layout giống hệt) | `HAL_PICOCLAW_WORKSPACE_DIR` (`/root/.picoclaw/workspace`) |
| `codex` | `OpenClawContextManager` (layout giống hệt) | `HAL_CODEX_WORKSPACE_DIR` (`/root/.codex/workspace`) |
| `claudecode` | `context_manager/claudecode.py` `ClaudeCodeContextManager` — layout OpenClaw trừ skills, đọc từ `.claude/skills/` (dir native của claude CLI) | `HAL_CLAUDECODE_WORKSPACE_DIR` (`/root/.claudecode/workspace`) |
| `opencode` | `OpenClawContextManager` (layout giống hệt; như codex, skills nằm ở dir ngoài workspace `~/.config/opencode/skills` nên catalog skills theo workspace rỗng — identity + memory vẫn nạp đúng) | `HAL_OPENCODE_WORKSPACE_DIR` (`/root/.opencode/workspace`) |

`ContextManagerBase` (`context_manager/base.py`) lo phần lắp ráp prompt
(`build_instructions`), lưu lượt (`add_turn`), nạp/trim memory, và summarize;
subclass cài `load_device_context`, `load_device_memory`, `load_skills_catalog`,
`summarize_device_memory`. Prompt nền nằm ở `resources/` (`system_prompt.md` +
bản theo provider `system_prompt_openai.md` / `system_prompt_gemini.md` /
`system_prompt_gptlive.md`, đăng ký trong `PROVIDER_PROMPT_PATHS` của
context_manager).

### Memory & summarization

Các lượt realtime được append vào file JSONL (`HAL_REALTIME_MEMORY_PATH`, mặc định
`<workspace>/realtime/memory.jsonl`), trim về `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(giữ lại `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
nén device + realtime memory qua **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, mặc định `claude-haiku-4-5-20251001`).

Lời gọi này được **thử lại** (`HAL_REALTIME_SUMMARIZER_RETRIES`, mặc định 2,
giãn cách `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S`) vì lỗi đến từ gateway chứ
không từ input: đo trên lamp-0c89 ngày 03/09/2026, cùng một payload trả 404 một
lần rồi thành công 4 trong 5 lần kế tiếp (một lần timeout), trong khi payload
LỚN HƠN chứa trọn nó lại chạy ngay lần đầu. Không thử lại thì một cú rớt là mất
trọn bản tóm tắt cho tới lần rebuild session sau.
Điều này gồm cả lượt delegate hoặc fallback sang main agent: HAL lưu request của
user trước khi dispatch, rồi lưu từng fragment TTS opt-in của main agent sau khi
nói xong. `[TTS HISTORY]` vẫn cập nhật session live hiện tại ngay lập tức, nhưng
không được coi là memory bền: session mới sau idle hoặc tool-call sẽ nạp lại từ
JSONL/summary.
Với các runtime dùng layout OpenClaw (OpenClaw, PicoClaw, Codex, Claude Code,
OpenCode), context manager còn nạp `MEMORY.md` ở root của workspace, ngoài
device summary được sinh ra và các file `memory/*.md` mới. Hermes dùng
`memories/MEMORY.md` theo layout native của nó.
Summarize chạy lúc `start()` (bù phần chưa tóm tắt) và `stop()` (flush). Phần
catch-up ở `start()` chạy trong **thread nền** (sau `connect()`), nên lời gọi
Anthropic không chặn session trở thành `available` — nếu chặn thì một lượt nói
sớm ("hello") ngay sau khi restart sẽ rớt xuống main agent.

Prompt của summarizer (`resources/summarize_prompt.md`) yêu cầu model đặt mọi
request của user mà các entry không cho thấy đã được trả lời, hoàn thành hay
hủy vào một heading cuối `## Open requests`, mỗi request một bullet có
timestamp. Các bullet này không tồn tại vĩnh viễn: `expire_open_requests()`
(`context_manager/base.py`) xóa mọi bullet có timestamp `[<ISO-8601>]` ở đầu
đã cũ từ `HAL_REALTIME_SUMMARY_OPEN_REQUEST_TTL_S` (mặc định 3600s) trở lên
(timestamp không có múi giờ được hiểu là UTC), và xóa luôn heading khi không
còn bullet nào. Hết hạn tính theo từng bullet, không theo file: `summary.md`
được ghi lại ở mọi session có entry mới, nên trên một thiết bị đang hoạt động
mtime của nó không bao giờ già quá TTL — tuổi file chỉ là phương án dự phòng
cho bullet không có timestamp đọc được. Cơ chế này chạy cả ở nơi summary được
refeed lại thành `[Previous summary]` cho lần summarize kế tiếp lẫn nơi nó
được nạp vào session context — cơ chế xác định (deterministic) để chặn một task
đang chờ nằm mãi trong context rồi bị "trả lời" từ ký ức cũ bởi một nudge rỗng
nội dung (#419, #421). `0` là tắt cơ chế hết hạn.

## Chế độ live (song công hoàn toàn)

**Nó thay đổi gì.** VAD cục bộ thôi không còn làm nhiệm vụ chốt lượt mà trở
thành **chuông cửa**: nó quyết định khi nào MỞ một phiên, và khi phiên đã mở thì
nó hoàn toàn không chạy nữa. Mic stream liên tục và **nhà cung cấp** sở hữu việc
chuyển lượt, kết thúc lượt và ngắt lời. Bật bằng `HAL_LIVE_MODE=true`.

Đo trên `intern-v2-6286` (07/09/2026, Gemini 3.1 Flash Live): **97 ms** từ lúc
gửi audio cuối tới lúc nhận audio đầu tiên, so với 3-6 s commit→câu nói đầu tiên
trên đường lượt.

**Loại trừ nhau theo thiết kế.** Đường lượt và phiên live cần chế độ nhận biết
lượt *ngược nhau*, và thiết lập đó được nướng vào phiên nhà cung cấp ngay lúc
kết nối. Hỗ trợ cả hai cùng lúc sẽ cần một override runtime cộng với việc dựng
lại phiên mỗi lần vào và ra; biến chế độ live thành lựa chọn cho toàn tiến trình
loại bỏ hẳn bộ máy đó, đổi lại phải khởi động lại để chuyển. Vì vậy
`HAL_LIVE_MODE=true` **ép** `HAL_REALTIME_TURN_DETECTION` từ `off` sang
`server_vad` (`hal/config.py`) — giá trị đó được đọc lúc import bởi
`GeminiConfig.vad_enabled` và `OpenAIConfig.turn_detection_type`, nên phải chốt
trước khi các model đó được định nghĩa. GPT-Live không có knob VAD nào để ép —
model vốn full-duplex; với nó `LIVE_MODE` chỉ quyết định `commit_audio()` là
no-op và việc công bố `UserSpeechOutput` / `user_turn_id`.
Một giá trị khác `off` do người dùng đặt thì được giữ nguyên. Sai chỗ này sẽ tạo
ra thiết bị stream audio mãi mãi mà không bao giờ trả lời.

### Giọng main agent trong LIVE

`live_active` vẫn có nghĩa phiên mic đang mở. Property riêng
`live_speaker_busy` nhận diện realtime đang phát (PCM native hoặc reply realtime
qua TTS); mic mở nhưng idle không chặn TTS main. `/voice/speak-queue` kiểm tra
property này tại lúc nhận vào queue. Main đến khi LIVE đang phát sẽ chờ trong
hàng đợi pre-synthesis HAL hiện có, không ngắt LIVE hay trả `suppressed`.
Playback native, text streaming và cache đều phát tiếp phần đang chờ khi xong.
Main run mới thay phần main cũ trong queue; run cũ đến trễ vẫn bị loại theo
quy tắc stale turn hiện có. Không thêm queue hay cơ chế retry bên OS.

Reset output/cleanup LIVE giữ main đang chờ. Input mới không rỗng, qua
addressing gate sẽ dừng playback một lần mỗi provider turn và xóa queue;
explicit stop cũng xóa queue, kể cả speech được giữ lại sau reset model.
Chính sách cancel OS giữ nguyên. Ngắt theo input xác nhận cần
`UserSpeechOutput`: cả ba provider đều phát (OpenAI từ
`input_audio_buffer.speech_started` / `speech_stopped` và input transcription;
GPT-Live chỉ từ `session.input_transcript.delta`, không có endpoint; đều chỉ
trong LIVE_MODE).
Mic streaming, server VAD, delegate và đường nhận TTS của LIVE OFF không đổi.
Test chuyển loa dùng audio giả; barge-in âm thanh cần kiểm tra trên device.

### Wake-word và focus gate khi vào live

Khi bật `HAL_WAKEWORD_ENABLED`, chỉ VAD local chưa đủ để mở audio live. Sau các bước kiểm tra nhạc/nhiễu, `_live_decision` yêu cầu focus window còn hiệu lực trước khi chuẩn bị phiên realtime live. Focus từ kiểm tra gaze lúc bắt đầu nói, button hoặc lượt wake-word đã được chấp nhận đều cho phép vào. Nếu focus thiếu hoặc hết hạn, decision trả về `turn` để dùng đường STT `_stream_session` thông thường, bất kể gaze đang bật, tắt hay ở shadow mode.

Đường STT này báo đang nghe tạm thời khi partial khớp wake phrase như “Hello Lamp”; transcript final/đã ghép phải xác nhận lại trước khi xử lý lượt bình thường. Với Live ON, lượt mở đầu đã được phép và qua noise guard sẽ vào phiên live song công hiện có nếu realtime khả dụng. Audio đã thu được gửi một lần làm pre-roll, sau đó chính mic đó tiếp tục streaming trong khi provider trả lời hoặc delegate. Đường manual commit vẫn tắt; partial không mở audio live. Lượt mở đầu giữ interaction ID của STT, không tạo thêm một lượt metric. Provider không khả dụng hoặc chưa trả lời thì fallback main agent một lần; lượt đã trả lời, reject, delegate hoặc bị lượt mới thay thế không được dispatch lại.

Tắt wake-word gate cho phép vào live chỉ dựa trên VAD. Tắt gaze hoặc bật `HAL_GAZE_SHADOW=true` không bỏ qua wake-word gate: `WOULD_WAKE` ở shadow chỉ quan sát và không mở focus. Chỉ kiểm tra gate lúc vào; trong phiên đã được phép, server VAD chia lượt hội thoại. Sau khi kết thúc phiên, lần vào tiếp theo kiểm tra focus lại. Harness voice giữ route riêng hiện có.

Lượt hội thoại LIVE hợp lệ cũng refresh `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`,
một lần tại terminal thành công được provider xác nhận hoặc sau delegate.
Lượt phải có lời người dùng và được xác định là nói với thiết bị (kể cả focus
đã có khi mở LIVE). Reject/interruption, timeout receive, input rỗng, output
chỉ từ model và Harness voice không refresh. Terminal lặp không gia hạn lần
nữa. Window vẫn có hạn: sau khoảng idle được cấu hình, phiên tiếp theo cần
wake phrase hoặc một lần cấp focus mới.

### History của lượt Gemini live hoàn tất

Với `HAL_LIVE_MODE=true`, các đoạn transcript input Gemini đi kèm provider turn ID. `hal/drivers/voice/_internal/live_history.py` chỉ ghép input/output cùng ID rồi gửi một notification `voice_agent_handled` sau terminal thành công từ provider. Timeout receive giữ lượt đang dở; terminal lặp không gửi lại. Lượt reject, delegate, bị ngắt, không rõ chủ sở hữu hoặc thiếu transcript không được ghi như lượt hoàn tất. Dữ liệu history tách khỏi đường playback; output-reset hiện có cũng xóa phần câu trả lời đang gom.

Một worker nền gửi lượt hoàn tất với interaction ID, giới hạn độ dài reply và snapshot Harness hiện có. Worker gửi hết notification hoàn tất sau khi live kết thúc mà không chặn playback. OS xử lý qua `externalhistory`, lưu disk, gửi silent và hiển thị card **History sync · Realtime → Main** hiện có. Buffer HAL có giới hạn (64 lượt chưa xong, 64 notification chờ gửi, 128 ID đã đóng gần nhất); lỗi đầy/transport được log. Độ bền bắt đầu khi OS nhận lưu notification. Đường này chỉ bám vào provider turn ID, không bám vào Gemini: adapter OpenAI và GPT-Live nay phát cùng contract (transcript input và terminal đều mang `user_turn_id`; terminal của GPT-Live do watchdog tổng hợp), nên history live cũng ghép được cho hai provider đó — hành vi mới được ghim bằng unit test, chưa kiểm chứng trên thiết bị.

### Phản hồi HW emotion trong chế độ live

LIVE dùng cùng lời gọi HW emotion như realtime thường, gồm hành vi LED, màn
hình và thân. `listening` cần transcript đầu vào có chữ từ provider và cùng quy
tắc xác nhận lời nói hướng tới device: nhận wake word, focus đang mở, tắt cổng
wake word hoặc điều kiện Harness listening hiện có. Các mảnh transcript được
gộp theo lượt provider. Xác nhận được giữ riêng cho lượt đó; focus mở giữa câu
vẫn có thể cho phép emotion. Mở phiên LIVE, RMS/Silero local, sự kiện VAD trống
của provider và khoảng im lặng local không tự bật `listening` hay `thinking`.

Sau khi có lời nói được nhận dạng và hướng tới device, endpoint tiếng nói thật
hoặc cờ transcription-finished tường minh từ provider mới chuyển sang helper
thinking hiện có. Endpoint đến trước chữ phải chờ transcript và điều kiện hướng
tới device. Thông báo chỉ có cờ finished cần input key đã tồn tại và bỏ qua ghi nhận speech
của metric/history; không tạo endpoint, event metric hay đổi eligibility của execution. Nếu chưa có bằng
chứng kết thúc, listening hết hạn sau 8 giây không có cập nhật transcript;
thinking hết hạn sau 25 giây. Playback, reject, ngắt lời, delegate và thoát phiên
dọn đúng lượt bằng helper có kiểm tra emotion hiện có. Output cũ không được dọn
lượt mới. Lời gọi phần cứng chạy đúng thứ tự trên worker; playback chờ dọn cue.
Mic streaming, đồng hồ idle, server VAD, barge-in, routing và tính metric giữ nguyên.

### Voice metrics trong chế độ live

Phiên live (Gemini Live, OpenAI Realtime hay GPT-Live) dùng `hal/telemetry/live_voice.py` để
ánh xạ lượt người dùng của provider sang interaction ID HAL, gắn owner cho playback native và truyền cùng
ID vào task delegate qua OS. Chỉ terminal thành công của provider gắn đúng lượt
mới hoàn tất execution realtime; riêng timeout, done tự tạo hay ngắt lời không
phải bằng chứng completed. Không có terminal thành công thì task vẫn là
incomplete. Ngắt lời từ server ghi biên `server_barge_in` đúng interaction
để theo dõi audio cũ.

Snapshot ghi `mode=live` và có biết endpoint tiếng nói hay không. Timestamp nhận
activity Gemini vẫn phục vụ voice cue (`server_vad_receive`), nhưng không được
dùng làm speech end âm học cho KPI-1. Trace bộ đếm/offset trước và sau SDK không
đổi cấu hình VAD. Lượt Gemini chỉ có transcript hoặc thời điểm nhận event vẫn
hợp lệ cho metric execution nhưng bị loại
khỏi KPI-1 latency với `speech_endpoint_unavailable` và latency null. Trên
GPT-Live mọi lượt đều thuộc loại này: adapter không bao giờ có `endpoint_at`
(không có event VAD), nên KPI-1 không đo được trên provider đó. Output
không có owner không được gán cho câu nói mới nhất. Endpoint hợp lệ trước playback
tới muộn có thể amendment latency. Filler look LIVE giữ owner đúng provider turn
suốt thao tác aim. Bộ đếm
`voice_metrics_live_coverage` lúc đóng phiên thể hiện phần mất độ phủ này; hook
không đổi lọc tiếng ồn, uplink hay cờ routing. Xem
[voice metrics](voice-metrics_vi.md#độ-phủ-của-phiên-live) để biết hợp đồng event.

### Cổng vào: hai cửa, ba kết cục

`_vad_loop` xác nhận tiếng nói như thường lệ, rồi `_live_decision()` trả về một
trong:

| kết cục | khi nào | chi phí |
|---|---|---|
| `live` | trigger là tiếng nói và realtime khả dụng | một phiên live |
| `turn` | wake focus đóng, hoặc realtime tắt/không khả dụng | STT xác nhận wake phrase; lượt mở đầu hợp lệ vào live nếu khả dụng, nếu không thì fallback main agent |
| `skip` | đang phát nhạc, hoặc trigger **không phải tiếng nói** | không tốn gì cả |

Cửa thứ hai không phải tùy chọn trên thiết bị có VAD vào rộng. Một phiên live
tính tiền audio lên suốt thời gian nó mở, nên nó không được kích hoạt vì một
tiếng "cạch" — và không thể tin cửa vào tự quyết việc đó: `webrtcvad` chấp nhận
7/7 mẫu không phải tiếng nói trong khi Silero từ chối cả 7, và
`HAL_SILERO_ENABLED` là `false` trên một số thiết bị. Do đó
`_rt_noise_is_speech()` mang theo instance Silero **của riêng nó**. Quan sát trên
`intern-v2-6286`: một transient codec bị clip đã mở một phiên mỗi ~25 s suốt cả
buổi tối. `skip` (chứ không phải `turn`) cũng là lý do một trigger bị từ chối
không tốn cả phiên STT.

### Cổng đường lên

Mọi khung mic đều tới model. Quyết định duy nhất mỗi khung là bộ khử vọng có bảo
đảm được cho nó không, và khung nào nó không bảo đảm được thì bị **thay bằng im
lặng cùng độ dài, không bao giờ bị bỏ** — đường lên là một đồng hồ, và một mối
nối chính là thứ mà VAD phía server đọc thành điểm bắt đầu nói. Việc thay thế
diễn ra *trước* khi resample, nên số khung ra == số khung vào theo cấu trúc.

`HAL_LIVE_UPLINK_DURING_PLAYBACK`:

- **`mute` (mặc định)** — thay im lặng cho toàn bộ cửa sổ phát. Dùng được ngay.
  Đánh đổi hoàn toàn khả năng cắt lời: người dùng không thể ngắt cho tới khi
  thiết bị nói xong.
- **`cancelled`** — gửi khung đã khử vọng; chỉ thay thế những khung mà
  `aec.uncancelled()` đánh dấu. Song công thật sự.

`cancelled` là đích đến và **không** phải mặc định, vì bộ khử vọng chưa xứng
đáng. Đo 04/09/2026 (`barge-in-captures/`): ERLE trung bình 14-19 dB nhưng **ERLE
đỉnh ~5 dB** — đỉnh mic 29264 rời APM còn 25269, so với sàn ngắt lời thật là
6956. VAD của nhà cung cấp nhìn thấy đỉnh và không có phòng vệ vọng âm nào
của riêng nó (không thấy được `uncancelled()`, không áp sàn thời lượng), nên nó
đọc chính điểm bắt đầu tiếng của thiết bị thành người dùng ngắt lời. Chuyển sang `cancelled` khi phát lại `full40-bargein-off` đưa residual đỉnh
xuống dưới sàn đó với biên an toàn.

Hai tín hiệu độc lập "loa có đang phát không" nuôi cổng này, vì không cái nào tự
đủ: `tts.speaking` là nguồn thẩm quyền và hoạt động **kể cả khi không có bộ khử
vọng nào** (khi không có, `aec.reference_idle_for()` trả `inf` và cổng sẽ không
bao giờ kích hoạt), còn phần đuôi reference bổ sung độ suy giảm âm học sau khi cờ
tắt. Cổng còn giữ đuôi bằng đồng hồ monotonic từ lúc quan sát TTS kết thúc,
nên khi AEC tắt hoặc thiếu thư viện, mic không mở lại ngay vào tiếng vọng
trong phòng. Cách này cũng che khoảng nghỉ ngắn giữa các đoạn phát trong hàng đợi.
`HAL_LIVE_PLAYBACK_TAIL_S` (mặc định 0.35 s) là đuôi *âm học*, cố ý không phải `AEC_TAIL_S`
(2.0 s): nếu khóa theo cái dài hơn, `mute` sẽ nuốt hai giây đầu của mọi câu trả
lời người dùng nói.

**Nhạc được loại trừ bằng cách hỏi `music_service`, không bao giờ bằng cách hỏi
cổng này.** `aplay`/`paplay` ghi thẳng ra ALSA và không bao giờ chạm điểm trích
vọng, nên trong lúc phát nhạc `reference_idle_for()` đọc ra một căn phòng hoàn
toàn yên tĩnh.

### Bơm đầu ra

Trong LIVE, transcript đầu vào có thể đến sau khi model đã bắt đầu trả lời.
Khi đầu ra đã mang ID lượt đầu vào, transcript đến muộn của cùng ID không được
dừng phần trả lời realtime hoặc xóa các câu TTS ngoài đang chờ (kể cả ElevenLabs).
Đầu vào mới có ID khác và được xác nhận là hướng đến thiết bị vẫn có thể ngắt lời;
quy tắc ngắt phần phát của main agent được giữ nguyên.

`_live_out_pump` lặp `orchestrator.stream_output()` thay vì đọc thẳng hàng đợi
của agent. Việc đó tái dùng toàn bộ bề mặt tool đang có — `look` + replay,
`express_emotion`, `reject_turn`, `delegate_to_main` — thay vì cài lại, và nó đọc
lại `self._agent` ở mỗi vòng ngoài, nên việc dựng lại phiên không thể để bơm đọc
một hàng đợi chết. `stream_output()` trả về một lần cho mỗi câu trả lời của model
(`turn_complete`) và cũng trả về khi im lặng lâu, lúc `receive()` hết giờ mà chưa
yield gì; cả hai đều chỉ có nghĩa là "quay lại vòng nữa".

`InterruptedOutput` (mới) được `gemini_live` phát ra khi `content.interrupted`, và
`openai_realtime` phát ra (`reason="server_interrupt"`) khi
`input_audio_buffer.speech_started` đến giữa một response hoặc `response.done`
báo `cancelled`; đó là **cách cắt lời bằng giọng nói duy nhất** — không có gì cục
bộ quyết định nó. Nó dừng phát ngay lập tức. Đường lượt không bao giờ thấy nó: manual VAD
không cho server cơ hội phát ra.

**Đầu ra được flush một lần lúc bắt đầu phiên.** Không có nó, phiên sẽ mở trên
một đầu ra cũ của lượt trước — quan sát trên thiết bị 07/09/2026: một
`reject_turn` do lượt nhiễu trước đó xếp hàng đã bị đọc ngay trong cùng giây với
session START, và phiên ngồi im suốt toàn bộ thời gian chờ. `run_realtime_turn`
flush trước mỗi commit vì đúng lý do này; phiên live không commit gì, nên nó
flush lúc vào. Chỉ một lần — flush mỗi câu trả lời sẽ vứt mất đầu ra mà model
đang stream dở.

### Việc tái tạo phiên bị chặn

`set_live_active(True)` chặn mọi lần tái tạo phiên sau lượt trong
`stream_output()` suốt thời gian phiên, vì cả ba lý do đều sai bên trong một
phiên:

- **zombie** — một quãng im lặng không sinh đầu ra, nên
  `REALTIME_ZOMBIE_RECONNECT_AFTER` lần như vậy (~24 s người dùng chỉ đơn giản là
  không nói) sẽ ép kết nối lại giữa cuộc trò chuyện.
- **turn-cap** — đổi phiên sau mỗi `REALTIME_SESSION_MAX_TURNS` câu trả lời.
- **idle** — cùng việc đổi phiên, cùng một đường lên đang mở.

Việc dựng lại ở đây cũng không sống sót được như giữa các lượt: bơm mic vẫn tiếp
tục append xuyên qua lúc đổi. Hoãn lại chứ không hủy — `set_live_active(False)`
reset các bộ đếm khi cúp máy.

### Chế độ live không làm gì

Mọi thứ dưới đây gắn với ranh giới lượt và không có nguồn cung trong một phiên
live. Đây là đánh đổi sản phẩm, không phải lỗi:

| mất | hệ quả |
|---|---|
| transcript STT | không có `[TURN CONTEXT]`, không có bộ lọc dựa trên transcript |
| wake word mỗi lượt | kiểm tra qua STT lúc vào khi bật wake-word gate và chưa có focus; lượt mở đầu được xác nhận có thể vào live ngay trong lần thu đó. Không kiểm tra wake word bằng STT local trong phiên live đã được phép |
| speaker ID, cảm xúc giọng nói | một phiên không tạo ra cả hai |
| STT cục bộ khi delegate | `delegate_to_main` kết thúc phiên và chuyển tiếp `[voice-instruction]` + transcript đầu vào của chính provider làm `[transcript]` (Gemini input transcription / OpenAI `conversation.item.input_audio_transcription.*` / GPT-Live `session.input_transcript.delta`; `FunctionCallOutput.user_transcript` → `DelegateSignal.transcript`, `_live_out_pump` đọc); câu trả lời của main agent phát sau khi cúp máy. Provider không có input transcription chỉ chuyển tiếp instruction |

Cũng không chạy bên trong một phiên: cổng RMS vào, `SPEECH_HOLDOFF_S`, đồng hồ im
lặng, `MAX_SESSION_DURATION_S`, socket STT mỗi lượt và keepalive của nó (thậm chí
không được kết nối trước — `stt_keepalive_on` là false ở chế độ live), noise
guard, warm-mic drain và echo-skip, và `commit_audio`. Không cái
nào bị xóa: đường lượt vẫn dùng tất cả, và lấy lại chúng ngay khi một phiên kết
thúc.

### Cấu hình

| Env | Mặc định | Ý nghĩa |
|-----|----------|---------|
| `HAL_LIVE_MODE` | `false` | Chế độ live cho toàn tiến trình. Ép `HAL_REALTIME_TURN_DETECTION=server_vad` khi giá trị đó là `off` |
| `HAL_AEC_ENABLED` | `true` | Software AEC; đặt `false` cùng live mode chọn đường thích nghi dùng chung cho audio đã được phần cứng xử lý |
| `HAL_LIVE_UPLINK_DURING_PLAYBACK` | `mute` | Trên đường software AEC: `mute` gửi im lặng khi loa phát; `cancelled` chỉ gửi frame đã được AEC xử lý; `always` gửi mọi frame, kể cả vọng còn sót. Đường thích nghi áp dụng bộ chặn riêng |
| `HAL_LIVE_PLAYBACK_TAIL_S` | `0.35` | Đuôi âm học sau lần ghi reference cuối hoặc lúc quan sát TTS kết thúc, kể cả khi không có AEC |
| `HAL_LIVE_IDLE_HANGUP_S` | `15` | Cúp máy sau khoảng này không có hoạt động người dùng, phát loa hay tiến độ text/audio từ provider; tính từ mốc mới nhất, kể cả output trước khi TTS bắt đầu |
| `HAL_LIVE_MAX_UNPROMPTED_REPLIES` | `3` | Trần cứng cho số câu trả lời liên tiếp của model mà không có tiếng người dùng xen giữa — cắt vòng lặp tự nói mà không cắt ngang một câu trả lời dài |
| `HAL_LIVE_MAX_S` | `600` | Trần tuyệt đối cho một phiên |

Đường live thích nghi dùng chung được chọn bởi `HAL_LIVE_MODE=true` và
`HAL_AEC_ENABLED=false`. Không có cờ mới hay nhận diện tên phần cứng: tắt
software AEC tường minh nghĩa là chọn đường dành cho audio đã được phần cứng
xử lý. Software AEC mặc định `true` và giữ đường xử lý hiện có.
Lamp `pro-respeaker-lite` và `pro-xvf3800` đều chọn đường dùng chung với live
bật, software AEC tắt và `HAL_LIVE_UPLINK_DURING_PLAYBACK=always`; standard/pro
giữ software AEC bật. Trên Lite, mic hội thoại và loa dùng ALSA card `Lite` ở
16 kHz; sensing dùng `sndi2s4` qua `asound.conf` của profile.

`live_gate.py` (`AdaptiveLiveGate`), `live_playback.py` và `live_reply.py`
(`LiveReplyGuard`) triển khai đường dùng chung. Bộ chặn dùng quy tắc năng
lượng RMS, không phải bộ phân loại tiếng nói. Mic tiếp tục thu nhưng gửi
im lặng trong lúc loa phát cho đến khi xác nhận tiếng người tại thiết bị,
sau đó gửi tối đa 300 ms audio đầu câu đã giữ lại trước các frame trực tiếp.
Bộ chặn vọng được giữ thêm 300 ms sau khi loa ngừng phát.

Khi mở phiên live, đường thích nghi dùng chung bỏ qua WebRTC VAD, kiểm tra Silero
đầu vào và realtime noise guard chỉ khi realtime được bật, đồng thời kiểm tra
wake-word tắt hoặc wake focus đã được cấp. Vẫn cần năng lượng liên tục trên
ngưỡng nghỉ thích nghi trong ít nhất 160 ms để mở phiên. Nếu wake-word bật mà
chưa có focus, vẫn phải xác nhận wake-word qua STT như trước. Đường software
AEC giữ nguyên kiểm tra đầu vào hiện có.
Trong phiên live thích nghi, đồng hồ ngắt phiên do im lặng cũng dùng
trực tiếp trạng thái tiếng nói của bộ chặn thích nghi, không kiểm tra Silero lần nữa.

Ngưỡng RMS lúc nghỉ là giá trị lớn hơn giữa −42 dBFS (khoảng 260 PCM16 RMS)
và bốn lần nền nhiễu đang theo dõi, với thời gian xác nhận bắt đầu 160 ms.
Khi loa phát, ngưỡng là giá trị lớn nhất giữa −27 dBFS (khoảng 1.464 PCM16 RMS),
năm lần nền nhiễu và bao biên âm lượng loa trong 500 ms nhân hệ số ghép vọng
đã học, cộng biên an toàn 8 dB. Xác nhận tiếng người khi loa phát cần 240 ms;
500 ms dưới ngưỡng kết thúc tiếng nói cục bộ. Giống demo, ngắt lời cục bộ chờ
3 giây audio loa cộng dồn để AEC ổn định. HAL đếm frame ghi thành công ra loa,
không tính thời gian chờ TTS/mạng, từ lúc mở stream mic vật lý.
Không đặt lại bộ đếm ở mỗi cửa sổ nghe hay mỗi câu. Trong thời gian chờ, audio khi loa phát
bị chặn và không thể hạ loa. Một đợt vượt ngưỡng bắt đầu trong thời gian chờ
vẫn không được chấp nhận khi loa phát qua mốc 3 giây: VAD theo dõi cùng đợt
đó đến khi dưới ngưỡng đủ 500 ms, rồi mới nhận một đợt mới đủ 240 ms. Cách
chốt quyền ngắt ngay lúc bắt đầu giống demo, tránh nhận nhầm tiếng vọng đầu
phiên kéo dài thành nói chen ở khoảng 3,2 giây. Frame dưới ngưỡng khi loa
phát cũng cập nhật nền nhiễu khi VAD chưa nhận tiếng nói, giống demo.
Sau khi loa dừng, đuôi vọng 300 ms dùng ngưỡng idle và không yêu cầu đủ
3 giây warm-up; onset mới đủ 160 ms đi qua cùng prefix đã giữ. Mic lúc nghỉ vẫn hoạt động.
`live-aec` ghi `played_s` và `aec_ready`; đây là thời gian chờ cho phép,
không phải phép đo AEC đã hội tụ hay độ trễ đầu cuối đo được.

Bao biên âm loa được ước lượng sau giảm âm tạm thời và suy hao mixer ALSA,
từ PCM gửi ra loa và giá trị dB thực tế đọc bằng `amixer`.
Giống demo, mức tham chiếu đi theo quá trình giảm và khôi phục âm. Trạng thái mixer
được đọc một lần khi khởi động ngoài luồng audio, rồi cập nhật qua các route
điều khiển âm lượng. Nếu không đọc được dB, mức tham chiếu dùng gain bằng 1
một cách bảo thủ; không suy gain từ phần trăm đã lưu hay thang softvol riêng
của phần cứng. Khi biết suy hao, cách này tránh mức trước mixer đẩy ngưỡng
tiếng người lên quá cao. Đây là ước lượng mức tham chiếu đầu ra, không phải
đo áp suất âm trong phòng hay độ trễ audio chính xác.

Chặn vọng chỉ bắt đầu sau khi ghi thành công ra loa và hết hiệu lực 250 ms
sau lần ghi cuối, rồi đến khoảng đuôi vọng của bộ chặn. Chờ ElevenLabs tạo
audio hoặc TTS lỗi/đang thử lại không được coi là loa đang phát để chặn mic.
Marker Gemini `<no speech>` đứng riêng bị bỏ trước TTS, kể cả khi được gửi
thành nhiều mảnh văn bản.

Giảm âm tạm thời dùng `HAL_LIVE_DUCK_GAIN` (mặc định `0.12`, gain PCM tuyến tính,
không phải phần trăm volume hệ thống), chuyển xuống trong 15 ms và lên trong 80 ms.
Đặt trong `/opt/hal/.env` trên device rồi restart HAL. Giá trị hợp lệ lớn hơn
0 và không quá 1; giá trị không hợp lệ dùng lại `0.12`.
Cấu hình này chỉ áp dụng cho luồng live với AEC phần cứng. Mặc định lấy từ
demo, chưa phải mức tối ưu đã đo cho mọi lamp hoặc phòng.
File `.env` overlay của cả `pro-respeaker-lite` và `pro-xvf3800` đặt rõ
`HAL_LIVE_DUCK_GAIN=0.12`; profile Standard/Pro dùng AEC phần mềm không đặt biến này.
watchdog khôi phục gain khi tín hiệu điều khiển cũ quá 500 ms. Phát hiện năng
lượng cục bộ chỉ hạ âm lượng, không hủy TTS hay bỏ các đoạn văn bản tiếp theo.
Giống chế độ `duck` của demo, chỉ tín hiệu ngắt từ provider mới hủy phát và
chặn các đoạn đến muộn của câu trả lời đó, đồng thời xóa cả trạng thái duck
cục bộ lẫn trạng thái phát. Nếu không được xác nhận, kiểm tra khôi phục lần đầu
sau onset 1,5 giây, rồi mỗi 400 ms khi VAD vẫn báo nói (VAD kết thúc sau
500 ms dưới ngưỡng). Ra khỏi cửa sổ rủi ro vọng cũng khôi phục gain. Nếu Gemini đã sinh xong nhưng
ElevenLabs còn phát, không đảm bảo provider sẽ gửi tín hiệu ngắt; hạ âm cục
bộ chưa đảm bảo dừng hẳn trong trường hợp này.
Native audio vẫn là cấu hình riêng, không tự bật khi chọn đường này (trừ khi
chọn Gemini TTS: khi đó Gemini Live nói native bằng đúng voice TTS — xem mục
Gemini TTS trong `docs/vi/os-server_vi.md`); ElevenLabs
vẫn được hỗ trợ. Ngưỡng báo qua SSE cập nhật theo bộ chặn thích nghi; log
`live-aec` có giới hạn, báo trạng thái phiên đang hoạt động tối đa mỗi giây
một lần. Triển khai lấy ý tưởng từ script test hardware, không phải thuật
toán giống hệt hay cam kết độ trễ ngắt lời đã đo của script đó.

Chỉ đổi `/etc/autonomous/hardware-profile` chưa áp dụng các file của profile.
Chọn `pro-respeaker-lite` trước khi cài/cập nhật gói device; chỉ restart HAL
sẽ nạp lại `/opt/hal/.env` và cấu hình ALSA đang có.

### Hạn chế đã biết: `mute` có thể tự kích hoạt

Với `mute`, đường lên mang **im lặng số** trong toàn bộ cửa sổ phát và mang âm
thanh phòng thật sau đó. Chuyển tiếp đó là một điểm bắt đầu về biên độ, và VAD
phía server đọc một điểm bắt đầu thành có người bắt đầu nói — nên thiết bị có
thể tự trả lời *chính nó*. Quan sát trên thiết bị 07/09/2026 tại
`intern-v2-6286` ở mức âm lượng 70 %: bốn câu trả lời không ai hỏi trong 35 s
khi không có ai trong phòng ("What's up?", "I'm here. What can I do for you?"),
mỗi câu lại làm mới thời gian giữ K, và một dòng log
`barge-in: model interrupted by the user` trong khi không có người dùng nào.

`HAL_LIVE_MAX_UNPROMPTED_REPLIES` giới hạn thiệt hại — nó kết thúc phiên khi model
đã nói liên tiếp bấy nhiêu câu mà không có gì từ người dùng — nhưng đó là chốt
chặn, không phải cách chữa. Nó đếm số câu trả lời thay vì số giây chính là để một
câu trả lời dài vài phút không bao giờ bị nhầm thành vòng lặp. Cách chữa là chế độ `cancelled` trên một bộ khử vọng đủ tốt, để model
luôn nghe căn phòng thật mà không có chuyển tiếp nhân tạo. Giảm âm lượng loa làm
giảm rõ rệt khả năng xảy ra trong lúc chờ.

Hai profile `pro-respeaker-lite` và `pro-xvf3800` giữ độ nhạy VAD phía server
bắt đầu/kết thúc `high` và prefix **100 ms** như demo hardware, nhưng dùng
thời gian im lặng **1000 ms** thay vì **500 ms** của demo để cho phép nghỉ
lâu hơn giữa câu, đổi lại phản hồi có thể bắt đầu muộn hơn. Dùng các biến `HAL_LIVE_VAD_*` sẵn có; mặc định
của profile AEC phần mềm không đổi. Chỉ đồng bộ bộ gate echo cục bộ chưa đủ
để đồng bộ cách server nhận giọng nói.

### Kết thúc một phiên

Khi cúp máy, HAL hủy luồng nhận output của phiên và chờ luồng đó thoát trước
khi phiên live mới dùng lại hàng đợi provider. Lúc chờ hàng đợi, luồng kiểm tra
hủy tối đa mỗi 100 ms, không rút ngắn timeout nhận thông thường. Điều này ngăn
luồng cũ lấy rồi bỏ mất transcript hoặc câu trả lời của phiên mới. Luồng bị hủy
không được đưa câu ElevenLabs đang dang dở vào hàng đợi phát.
Tool handler đã chạy sẽ hoàn tất trước khi luồng nhận nhường quyền đọc.
HAL sau đó xếp `audio_stream_end` của Gemini sau audio đã gửi, gắn đúng
transport. Bỏ qua phiên manual VAD, còn tool chờ hoặc đã bị cách ly; socket
thay thế không nhận marker của phiên cũ. Đây là kết thúc uplink mic thật,
không phải commit ở các khoảng nghỉ thông thường.

Một phiên kết thúc sau `HAL_LIVE_IDLE_HANGUP_S` (K, mặc định 15 s) không có
hoạt động người dùng, phát loa hay tiến độ text/audio từ provider. Mic được
trả thẳng lại cho VAD, vốn mở phiên mới khi có tiếng nói thật tiếp theo.
Hai chi tiết khiến việc này chạy đúng:

- **Câu trả lời của model không phải hành động của người dùng**, nhưng đồng hồ
  được *giữ* trong lúc thiết bị đang nói và làm mới khi nhận text/audio hợp lệ
  từ provider, kể cả text đến trước audio đầu tiên của ElevenLabs. Cửa sổ đếm
  từ mốc mới nhất: hoạt động người dùng, kết thúc phát loa hoặc output model.
  Chỉ suy nghĩ mà không có text/audio mới không được miễn timeout vô hạn.
  `HAL_LIVE_MAX_S` và giới hạn câu trả lời không được yêu cầu vẫn áp dụng cho
  mọi đường live.
- **Chỉ RMS thì không gánh nổi đồng hồ này.** Trong phòng ồn, sàn nhiễu nằm trên
  `HAL_VAD_THRESHOLD`, nên mọi khung đều đọc thành "người dùng đang nói" và phiên
  không bao giờ cúp. Quan sát trên thiết bị 07/09/2026 tại `intern-v2-6286`: sàn
  nhiễu ~10500 so với ngưỡng 500 đã giữ một phiên mở vô hạn, và vì phiên live sở
  hữu mic, VAD không bao giờ chạy lại — thiết bị điếc cho tới khi khởi động lại.
  Vì vậy RMS là cửa đầu tiên rẻ tiền và **Silero xác nhận** trước khi đồng hồ
  được làm mới, gom theo `HAL_SILENCE_VAD_WINDOW_FRAMES`, đúng như đồng hồ im
  lặng của đường lượt.

`HAL_LIVE_MAX_S` là chốt chặn cuối cho căn phòng ồn tới mức ngay cả Silero cũng
liên tục đồng ý.

Khi đồng hồ idle yêu cầu transcript, tiếng nói vừa được hardware-AEC gate xác nhận sẽ hoãn cúp máy thêm tối đa một cửa sổ `HAL_LIVE_IDLE_HANGUP_S` (mặc định 15 giây) để chờ transcript. Tiếng nói được tính là gần đây nếu nằm trong thời gian im lặng phía server (ít nhất 1 giây) cộng 1 giây chờ truyền. Candidate cục bộ không gia hạn mốc này; transcript hợp lệ hoặc tiến độ trả lời sẽ đặt lại. Cách này tránh cắt lời mới bắt đầu ở hạn idle nhưng không cho nhiễu liên tục giữ phiên vô hạn.

Đọc các bộ đếm ở dòng log session-END: `substituted` ở mức ~100 % của
`during_playback` là `mute` đang hoạt động đúng thiết kế.

## Luồng một lượt (trong `voice_service.py`)

1. **Dựng + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` được tạo;
   `start()` chạy trong daemon thread (`realtime-start`) khi `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` được hook để feed lại text đã nói dạng `[TTS HISTORY]`,
   nhưng **chỉ khi lượt nói đó opt-in** (`TTSService.realtime_feedback`, đặt bởi
   cờ `realtime_feedback` trên `/voice/speak[-queue]`). Chỉ reply thật của agentic
   runtime mới opt-in — os-server gửi qua `hal.SpeakReply` / `hal.SpeakQueueReply`
   (được `SendToHALTTS` / `SendToHALTTSQueue` dùng). Mọi TTS hardcode (dead-air
   filler, ambient mumble, backchannel, thông báo reconnect/health, chitchat
   local) đi qua `hal.Speak` thường và **không bao giờ** được feed lại — nếu không
   model sẽ lặp lại (echo) những câu nó chưa từng sinh ra.
2. **Stream.** Khi session STT đang mở, mỗi frame mic được resample về rate của
   provider và gửi qua `append_audio()` (song song, non-blocking), đồng thời buffer
   vào `rt_audio_buffer`.
   Khi bật STT keepalive tùy chọn, nếu socket STT pre-connect đóng bình thường
   (WS 1000) đúng lúc bắt đầu nói, HAL thay socket trước khi stream tiếp và replay
   toàn bộ pre-roll đúng một lần vào socket mới. Nhờ vậy không mất từ mở đầu; close
   bình thường đã recovery là warning, không phải error.
   Gemini Manual VAD không có lệnh huỷ một activity đã stream. Vì vậy turn
   empty-STT/noise khởi động session sạch thay vì để noise lẫn vào câu người dùng
   kế tiếp. Reconnect này chạy nền: nếu user nói ngay, HAL giữ toàn bộ audio của
   turn mới ở local rồi gửi đúng một lần, đúng thứ tự khi session thay thế sẵn
   sàng. Nếu reconnect chậm/lỗi thì fallback về main agent với transcript STT;
   không làm rớt audio đầu câu hoặc commit nó vào activity cũ.
3. **Bơm turn context + prepass speaker-ID.** `[TURN CONTEXT]` (thời gian, nhắc
   ngôn ngữ trả lời, user hiện tại) được gửi dạng text không tạo response. **User
   hiện tại chính là người nói (VOICE speaker)** được nhận dạng trong lượt này — nó
   **ghi đè** `current_user` suy ra từ khuôn mặt, và rơi về định danh khuôn mặt khi
   không có voice ID (unknown / gate-reject / không có transcript).

   **Thời điểm chạy khác nhau theo mode**, vì voiceprint cần trọn câu nói nên không
   thể tồn tại lúc mới mở session:

   | Mode | Gửi `[TURN CONTEXT]` | Biết người nói? |
   |------|---------------------|-----------------|
   | Always-listening (`wakeword=false`) | trong lúc thu khi chuẩn bị xong, trước khi upload audio đã giữ | Fallback khuôn mặt; chỉ sửa nếu identity có trước commit |
   | Focus đang mở / gaze cấp focus trong lúc thu | ngay trong capture đó khi chuẩn bị xong | Fallback khuôn mặt; chỉ sửa nếu identity có trước commit |
   | Cửa sổ wake-word đang đóng | sau capture và final xác nhận wake phrase | Identity đã sẵn sàng trước commit, nếu có |
   | Deferred (rebuild sau noise-drop) | trên session thay thế đã sẵn sàng, trước khi phát lại audio đã giữ | Identity có tại thời điểm đó, nếu có |

   Việc mở hoặc flush lượt sau capture còn có điều kiện: lượt đó **không** phải
   noise. Noise guard đã phân loại xong capture, nên một
   lượt STT rỗng mà không phải tiếng nói sẽ không mở gì cả: không `[TURN CONTEXT]`,
   không audio, không session thay thế. Chính việc không gửi mới làm cho đường
   skip-commit trở nên miễn phí — nếu không, toàn bộ buffer của lượt đó đã vào (và
   bị tính tiền trong) một activity đang mở mà ngay bước sau lại vứt đi. Session
   được mở *sớm hơn* trong lúc capture (always-listening hoặc focus đã cấp) thì
   đã stream audio rồi nên vẫn bị discard như cũ.

   Ở mode always-listening, prepass speaker-ID (`identify_and_decorate`, chạy **một
   lần** cuối session) chỉ giải được người nói *sau khi* context đã gửi đi kèm tên
   từ khuôn mặt. HAL gửi tiếp một correction `[TURN CONTEXT UPDATE]` nêu đúng người
   nói — vẫn **trước** `commit_audio()` nên thuộc cùng một lượt. Khi phản hồi bắt
   đầu trong lúc STT drain final, identity được giải sau đó cho dispatch; HAL
   không chèn correction vào phản hồi đã xử lý. Transcript ngắn
   trong vùng mơ hồ AI-rejection sẽ hoãn external embedding call tới khi realtime
   quyết định xong; một lần reject rõ ràng tránh luôn call này, còn mọi turn không
   reject vẫn nhận cùng một kết quả identity duy nhất trước khi đi hạ nguồn. Bỏ qua
   khi context đã mang đúng tên, hoặc khi lượt đó là noise.

   **Giới hạn chờ identity.** Nhận diện người nói chạy trên thread riêng.
   Realtime theo lượt chỉ chờ tối đa `HAL_SPEAKER_PREPASS_COMMIT_JOIN_S`
   (mặc định **0.2s**) trước commit, tránh thêm tới 2s chờ embedding trước khi
   Gemini phản hồi. Kết quả có trước commit vẫn cập nhật context của lượt;
   kết quả muộn dùng để gắn danh tính khi gửi downstream, không chèn vào lời
   đang sinh. Trước dispatch, HAL join cùng worker bằng
   `HAL_SPEAKER_PREPASS_JOIN_S` (mặc định **2.0s**). Live và đường không qua
   realtime giữ thời gian chờ identity thông thường. Transcript ngắn mơ hồ
   giữ nguyên chính sách trì hoãn nhận diện.

   Với lượt hợp lệ, không phải noise, timer filler trung tính được bật trước
   khoảng chờ pre-commit, kể cả khi realtime đã stream audio trong lúc thu.
   Cùng timer được truyền cho bộ nhận phản hồi, không tạo thêm timer filler.
   Delay và ownership giữ nguyên; endpoint im lặng vẫn là 0.8s sau STT final,
   cùng ngưỡng fallback được cấu hình riêng.

   Trong capture hands-free LIVE OFF, speech phụ có thể ngắt (filler từ OS và
   cue của gesture, không gồm câu trả lời agent hay audio thuộc realtime) không
   được chiếm TTS từ trước khi kết nối STT đến khi ngừng thu mic. Nếu không,
   filler đến trễ đặt `speaking` và echo guard cắt câu mới của user với endpoint
   `tts_started`. Quyền giữ capture được nhả trước khi chờ STT trả transcript cuối
   và ở mọi nhánh thoát sớm/lỗi, nên filler lúc xử lý vẫn phát được sau capture.
   Retry câu listening của nút hết hiệu lực nếu capture bắt đầu; không được phát
   lại sau khi user nói xong. LIVE ON và capture Harness thủ công giữ hành vi
   hiện tại. Mốc đo metric tại endpoint không đổi.

   **Capture LIVE OFF và drain final.** Khi tắt wake-word gate hoặc focus đã
   được cấp (kể cả gaze cấp ngay trong capture này), HAL có thể upload audio
   trước khi STT đóng. `prepare_turn()` chạy trên `rt-capture-prepare`, để thread
   microphone tiếp tục giữ toàn bộ audio đúng thứ tự trong lúc kết nối session.
   Chỉ upload sau khi chuẩn bị và `bind_audio_turn()` thành công. Binding giữ
   đúng một agent và, với Gemini, đúng một socket provider xuyên suốt audio
   trong hàng đợi, commit và output. Nếu phát hiện binding không còn hợp lệ ở
   bước trước commit, recovery tạo agent mới rồi phát lại toàn bộ buffer; frame/commit Gemini trong hàng đợi
   không được chuyển sang socket thay thế. Lỗi session sau commit đi theo nhánh
   lỗi/fallback, không tự động phát lại những tool có thể đã chạy.

   Ở endpoint thông thường (`smart_turn`, `turn_fallback`, `turn_pause_limit`
   hoặc `silence_clock`), capture đã được phép được xử lý realtime ngay khi
   chữ STT final vượt qua noise guard, kể cả final đến trong lúc
   `stt_session.close()` đang drain trên worker. Callback final đánh thức thread
   sở hữu capture; không commit hay dispatch trên thread STT. Chỉ có partial
   thì không mở nhánh chạy chồng này; cửa sổ wake-word đang đóng vẫn cần final
   xác nhận. Stop trong lúc drain sẽ chặn dispatch. Follow-up Harness đang chờ
   cũng giữ nhánh đợi transcript đầy đủ. STT close vẫn hoàn tất trước dispatch:
   transcript cuối đã ghép được dùng cho history và input main agent, phản hồi
   sớm chỉ được xử lý một lần. Metric giữ timestamp speech-end gốc và cùng
   interaction ID. LIVE ON và capture Harness thủ công giữ đường xử lý hiện có.

   Commit audio đã kiểm tra binding được đưa vào hàng đợi trước cue HW thinking
   đồng bộ, một lần mỗi turn (không lặp khi retry hay replay camera). Provider
   xử lý song song với HW; việc đọc output vẫn chờ cue này hoàn tất. Không tạo
   worker emotion tách rời có thể ghi đè turn mới. Timing first-output vẫn tính
   cả thời gian HW; commit lỗi thì không bật thinking.

   Với TTS realtime không dùng native audio ở cả hai chế độ LIVE, nhận diện kết
   câu dùng text sau bước loại marker giọng/HW hiện có. Câu như
   `I'm right here! [cheerfully]` được gửi TTS khi provider còn stream/chờ tool
   định tuyến, không phải đợi hết lượt. Marker bị chia giữa các mẩu text vẫn được
   giữ trong buffer đến khi có thể loại bỏ đầy đủ. Grace định tuyến, ownership
   output, bộ lọc từ chối và cancellation giữ nguyên.

   **Kết quả được cache.** Trước đây nhận dạng chạy mỗi lượt, lượt nào cũng chạy:
   một cuộc mười lượt trả tiền mười lần gọi ra ngoài để nghe đúng một cái tên.
   `SpeakerDecorator` giờ dùng lại kết quả gần nhất trong `SPEAKER_ID_CACHE_S`
   (`HAL_SPEAKER_ID_CACHE_S`, mặc định 90s), và `SPEAKER_ID_CACHE_FOLLOWUP_S`
   (mặc định 300s) khi đang trong cửa sổ follow-up của wake word — những lượt đó
   theo định nghĩa là cùng một cuộc trò chuyện. **Unknown cũng được cache** — câu
   mà recognizer không xếp được chính là ca dễ lặp lại nhất, thử lại mỗi lượt là
   trả trọn độ trễ để nhận cùng một câu trả lời rỗng; thứ duy nhất mất khi cache
   unknown là đường dẫn WAV phục vụ enrol của lượt đó. `POST
   /speaker/current-user/reset` xoá luôn cache cùng với voice user hiện tại, vì
   đó là cùng một trạng thái presence ở tầng dưới.

   **Session bị thay được đóng ở nền.** Rebuild trước turn sẽ dựng session mới
   rồi dọn session cũ; làm inline thì lượt nói phải đợi `aclose()` cộng cú join
   IO thread — đo được 0.79s giữa lúc session mới mở và lúc `[TURN CONTEXT]` của
   lượt này bay đi (lamp-0c89, 03/09/2026). Không ai cần socket cũ đóng xong thì
   model mới nghe được người dùng, nên việc đóng chạy trên thread riêng (lỗi vẫn
   được log; nếu không tạo nổi thread thì đóng inline chứ không rò socket).

   **Lưu ý Gemini native-audio:** `send_text()` bỏ **toàn bộ** text không tạo
   response trên các model Gemini `*native-audio*` (`gemini_needs_idle_workaround()`),
   vì các message SDK `clientContent(turn_complete=False)` lặp lại va với lượt audio
   sau đó và đóng WS 1011. Trên các model đó cả context lẫn correction đều không tới
   được câu trả lời, và model rơi về định danh còn lưu trong memory của session.
   `gemini-3.1-flash-live` và OpenAI thì nhận cả hai; GPT-Live nhận qua
   `session.thinking.append` (context im lặng). Mọi lần bỏ đều được log
   (`[realtime->model] DROPPED …`).

   **Điều này không áp dụng cho cấu hình mặc định đang ship.** `REALTIME_GEMINI_MODEL`
   mặc định là `gemini-3.8-live` (`hal/config.py`), không phải
   native-audio, nên guard tắt và cả context lẫn correction đều tới được model. Nó chỉ
   bật lại khi ai đó cấu hình một model `*native-audio*`. Mặc định là bản
   `gemini-3.8-live` thường, KHÔNG phải `-extended-thinking`: rẻ, nhận tool BLOCKING
   mặc định và bỏ hẳn thinking (nó từ chối `thinkingLevel` nên
   `gemini_live._build_config` không gửi). Bản extended vẫn dùng được — nó chỉ nhận
   tool khai báo NON_BLOCKING, và `_build_config` giờ đã set NON_BLOCKING cho nó (khai
   BLOCKING thì model lỗi giữa turn, phát "I'm sorry, an error occurred.", quan sát
   trên device 2026-09-17) — nhưng vẫn giữ plain live làm mặc định.
4. **Commit.** Cuối session, nếu enabled + `available` + có audio buffer, gọi
   `commit_audio()`. Cue emotion `thinking` fire cùng lúc commit (mặt + servo +
   LED pulse ÉP HIỆN — `thinking` vốn là background emotion có LED nhường
   màu user đã set; cue realtime bypass đúng guard đó, còn user tắt đèn thì vẫn
   tắt) và được clear về `idle` khi có output đầu tiên (câu TTS đầu hoặc frame
   audio native đầu) hoặc khi turn chết không output — trừ khi model đã tự
   express emotion riêng. Lấp khoảng 1-3s latency của model mà trước đây device
   nhìn như đứng hình.

   **Dead-air filler** (`_WaitFiller`) là nửa phần tiếng của chính cue đó.
   Trên đường wake-word / follow-up nó được arm **trước** bước nối lại session
   sau capture (`start_realtime_turn()` → `prepare_turn()`), không phải lúc
   commit: trên lamp-dbda (18/9/2026) bước nối lại mất 2–3 s, arm lúc commit
   đẩy lời xác nhận đầu tiên ra 4–5 s sau khi user ngừng nói. Filler đã arm
   được truyền vào `run_realtime_turn(wait_filler=…)`, vẫn giữ luật một filler
   mỗi turn (`arm()` idempotent) và bị cancel ở mọi đường thoát. Session mở
   trong lúc capture (always-listening) vẫn arm lúc commit như cũ. Mặc định (`HAL_REALTIME_FIRST_CHUNK_MAX_CHARS=0`), câu
   hoàn chỉnh đầu tiên được nói ngay; các câu sau vẫn được tổng hợp trước qua
   hàng đợi, không chờ toàn bộ câu trả lời. Giá trị dương bật tùy chọn cắt câu
   đầu chưa hoàn chỉnh tại ranh giới mệnh đề cuối (`,` `;` `:` `—`), hoặc tại
   khoảng trắng khi vượt giới hạn ký tự. Câu hoàn chỉnh bỏ qua bộ cắt sớm này.
   Bộ cắt giữ nguyên voice tag trong ngoặc vuông, kể cả khi cắt theo khoảng
   trắng; không cắt tại dấu phẩy/hai chấm trong số hoặc dấu hai chấm của URL.
   Phần được cắt cần ít nhất 8 ký tự hiển thị ngoài tag. Cắt sớm có thể giảm
   thời gian chờ ban đầu nhưng vẫn tạo khoảng ngắt giữa các yêu cầu tổng hợp,
   nên chỉ bật khi chấp nhận đánh đổi này. Native audio vẫn stream từng frame.
   Sau `HAL_REALTIME_FILLER_DELAY_S` (mặc định 1.5s) mà vẫn
   chưa có output nào, HAL gọi `POST /api/sensing/filler` và os-server phát một
   filler realtime riêng từ cache — tiếng đệm suy nghĩ không lời như "Ừm...",
   khác với lời xác nhận mở đầu của main agent. Pool phrase, ngôn ngữ và WAV
   cache đều nằm ở os-server. Filler bắn ở mọi lượt hay chỉ ở lượt chậm là
   **tính chất của model**, và giá
   trị mặc định giả định model nhanh: câu chit-chat về trong ~1s thì không chạm
   timer, còn lượt dùng Google Search thì có. Phải ĐO trước khi tin điều đó trên
   một body cụ thể — trên `lamp-0c89` (26/08/2026, `gemini-3.1-flash-live-preview`
   qua proxy campaign-api) không lượt nào ra câu đầu dưới 3.0s (median 4.0s,
   n=31), nên filler là thứ duy nhất người dùng nghe được lúc đầu, và lamp hạ
   ngưỡng xuống 0.5s trong `.env` của nó. Đặt giá trị này theo thời gian
   time-to-first-sentence đo được, đừng theo mặc định. Filler
   **không được arm cho transcript ngắn nằm trong vùng mơ hồ của noise guard**
   (tối đa `HAL_REALTIME_NOISE_GUARD_MAX_WORDS`, mặc định 3 từ): model có thể
   `reject_turn` rõ ràng cho `o`, `you.` hay `Yeah.` ngay sau commit, và filler
   sớm sẽ biến một lần từ chối im lặng thành âm thanh gây khó chịu. Filler phát
   interruptible nên câu đầu tiên của model cắt ngang nó; mọi đường thoát
   (trả lời, delegate, turn rỗng, exception) đều cancel timer, riêng delegate
   cancel tường minh vì chặng main agent ngay sau đó tự bắn filler của nó. `0`
   để tắt.

   Phát filler là TTS, nên nó dừng pulse thinking và chạy speaking wave. Để
   phần chờ còn lại vẫn có tín hiệu, cue đánh dấu strip là của mình
   (`app_state._thinking_cue_active`): lần restore LED sau TTS vẽ lại pulse
   thinking thay vì rơi về user state. Cờ được bỏ khi cue clear và khi có bất
   kỳ emotion nào khác vào qua `POST /emotion`, nên emotion model tự express
   không bị đè.

   Turn **delegate** cố ý giữ cue — chặng main-agent phía sau mới là phần chờ
   dài, và hook của nó cũng tự bắn `thinking` lại. Turn ném exception thì KHÔNG
   phải bàn giao đó (không còn gì trong HAL đang lái mặt), nên nhánh exception
   clear cue trước khi rơi xuống forward sang OS server.

   Vì `thinking` chỉ bị kết thúc bởi chính emotion mà câu trả lời express, một
   turn không sinh ra emotion nào — delegate mà agent trả lời không kèm marker,
   forward không bao giờ xảy ra — từng để mặt (và qua `_thinking_cue_active`,
   mọi lần restore LED sau đó) kẹt ở pulse cho tới khi user nói tiếp. Giờ có hai
   thứ kết thúc nó.

   **Câu trả lời nói xong = hết chờ.** `_on_tts_speak_end` (`hal/app_state.py`)
   clear `thinking` khi TTS kết thúc, có gate `tts_service.realtime_feedback` —
   cờ chỉ do chính reply của agentic runtime set. Dead-air filler, mumble,
   system notice để False, nên TTS phát *trong lúc* chờ (đúng thứ mà cờ cue sinh
   ra để sống sót qua) không kết thúc cue. Đây là ca phổ biến và được xử đúng
   thời điểm: mặt đúng ngay khi máy ngừng nói, bất kể agent có nhả marker hay
   không.

   **Watchdog là lưới cho turn không hề nói.** `POST /emotion` arm một timer
   chặn cuối mỗi khi emotion là `thinking`: sau
   `HAL_EMOTION_THINKING_RESET_S` (mặc định 25s, `0` = tắt) thinking LIÊN TỤC,
   nó bỏ cờ cue, express `idle` và restore LED user state. Bất kỳ emotion nào
   khác huỷ timer; một `thinking` mới arm lại. Cửa sổ này lớn hơn khoảng giữ
   thật dài nhất đo trên máy (realtime clear trong 0.4-8.6s; delegate
   event-forwarded → assistant-turn-done chạy 6-22s), nên không thể nháy idle
   giữa lúc turn còn sống.
5. **Tiêu thụ.** `for output in stream_output()`:
   - `TextOutput` → các câu được flush sang TTS (`speak` / `speak_queue`).
     Nếu `speak` báo busy (TTS khác đang giữ loa non-interruptible, ví dụ
     nudge ambient), câu sẽ fallback sang `speak_queue` để phát sau đó thay
     vì bị mất luôn.
     Speech của agent trong queue nhận biết theo lượt: mỗi entry mang `turn_id`
     và `turn_seq` tăng đơn điệu.
     Khi nhận một run mới hơn, TTS dừng câu cũ đang phát và bỏ các entry
     pending của run cũ để người dùng nghe reply phù hợp ở thời điểm đó. Một
     request đến muộn từ run đã bị thay thế cũng bị bỏ thay vì quay lại queue.
     `POST /tts/stop` cũng huỷ cả playback đang chạy lẫn mọi entry pending.
   - `DelegateSignal` → dừng; chuyển `[voice-instruction] …` + transcript tới OS
     server với `event_type` gốc.
   - Ngược lại lượt đã được xử lý cục bộ → báo OS server `voice_agent_handled`
     (để OpenClaw trả `NO_REPLY`, bỏ filler dead-air), và lưu lượt vào realtime memory.

## Cấu hình

Realtime agent được cấu hình từ **block `realtime` trong `config.json`** của thiết
bị (các knob hướng người vận hành), với biến môi trường `HAL_*` của HAL là override
cho dev và default built-in là sàn. Thứ tự ưu tiên mỗi knob:

```
biến HAL_*  >  block "realtime" trong config.json  >  default built-in
```

os-server **seed** block này vào `config.json` và giữ nó bằng default trong code
(`DefaultRealtimeConfig`) mỗi lần start **cho tới khi operator sửa**: mọi lần ghi
qua web UI / MQTT `realtime.set` đặt `realtime.pinned: true`, từ đó os-server
không đụng block nữa. Nên đổi default fleet (vd gemini → openai) tới mọi máy chưa
từng chọn, còn máy đã chọn thì giữ. Muốn bỏ pin thì xoá `pinned` khỏi
`config.json`. HAL **tự đọc** trực tiếp
(giống `llm_api_key` / `stt_language`), không push xuống. Vì HAL đọc `config.json`
lúc import, đổi config phải **restart HAL** mới ăn. Sửa lúc đang chạy thì restart
liền (`restartHAL` trong `system/device/service.go`).

### Model và ngôn ngữ STT

`stt_language` chọn `stt_model` được lưu: English dùng `flux-general-en`; tiếng
Việt và các ngôn ngữ không phải English được hỗ trợ dùng `nova-3-general` với mã
BCP-47 đã chọn. Cặp đó được truyền cho proxy AutonomousSTT, kể cả lúc healthwatch
khởi động lại voice pipeline. Vì vậy cấu hình tiếng Việt đã lưu vẫn có hiệu lực
sau khi proxy restart; điều này không có nghĩa một model duy nhất xử lý chính xác
mọi trường hợp code-switching Việt–Anh.

**Chỉ restart khi config thực sự đổi.** os-server *không* restart HAL mỗi lần
os-server restart — làm vậy sẽ rớt voice pipeline vô ích. Thay vào đó nó hash
`config.json` và lưu hash vào `config/.hal_config_hash` mỗi khi (re)start HAL. Lúc
boot (`handleSetUpCompleteChange` trong `server/config_watch.go`) nó chỉ restart HAL
khi hash hiện tại khác snapshot — tức config thật sự đổi trong lúc os-server tắt
(setup mới, OTA đổi config, sửa lúc downtime), hoặc chưa có snapshot (boot đầu). Một
lần os-server restart bình thường với config không đổi sẽ để nguyên HAL đang chạy.
Nếu HAL thật sự chết, `hal.service` (`Restart=always`, `RestartSec=5`) tự hồi độc
lập, nên skip restart là an toàn. Nhánh `restartHAL` cập nhật lại snapshot sau khi
restart HAL, nên đổi lúc-chạy rồi os-server restart không restart hai lần. Hash cả
file (thay vì chỉ tập field HAL đọc) giữ tín hiệu tự-bảo-trì khi tập field HAL đọc
thay đổi; cái giá duy nhất là một lần restart HAL thừa ở boot kế tiếp khi sửa field
chỉ-thuộc-os-server.

### Block `realtime` trong `config.json`

Model ở Go tại `system/server/config/realtime.go`; đọc ở HAL tại
`hal/config.py`. Field chung ở trên; knob theo provider nằm trong sub-object
`gemini` / `openai` / `gptlive` / `pipecat_v1` (Go struct `GeminiRealtime` /
`OpenAIRealtime` / `GPTLiveRealtime{APIKey,BaseURL,Model,Voice}` /
`PipecatV1Realtime`), `provider` chọn cái đang active
(`none` hoặc vắng → tắt realtime; Go còn nhận `off` / `disabled` như đồng nghĩa
của `none`). `api_key` / `base_url` rỗng → fallback `llm_api_key` /
`llm_base_url` cho Gemini và OpenAI Realtime. GPT-Live theo đúng quy tắc đó: key
rỗng → `llm_api_key`, `base_url` rỗng → base URL của OpenAI Realtime
(`<llm_base_url>/ws/openai`), SDK nối thêm `/live/sessions` — xem blockquote dưới.
Pipecat v1 resolve key theo thứ tự `HAL_PIPECAT_API_KEY` >
`realtime.pipecat_v1.api_key` > `realtime.api_key` dùng chung > `llm_api_key`,
và base URL chat theo `HAL_PIPECAT_BASE_URL` > `realtime.pipecat_v1.base_url` >
mặc định relay Qwen — `realtime.base_url` dùng chung **cố ý không** được xét
(nó mang hình dạng relay `/ws/...`, không phải endpoint chat-completions), đó là
lý do trang Settings web ẩn ô Base URL với provider này và trỏ sang
`realtime.pipecat_v1.base_url` thay thế.

HAL đọc thêm hai knob trong `realtime.openai` mà Go **không** model:
`transcribe_model` và `noise_reduction` (`_RT_OPENAI` trong `hal/config.py`;
env `HAL_OPENAI_TRANSCRIBE_MODEL` / `HAL_OPENAI_NOISE_REDUCTION` thắng). Vì
os-server marshal `config.json` từ struct, hai key này **không sống qua** một
lần os-server lưu lại config — muốn ghim bền trên thiết bị thì đặt env trong
`/opt/hal/.env`.

> **Để `base_url` trống trừ khi có endpoint riêng (không qua proxy).** Khi trống,
> HAL tự suy ra `<llm_base_url>/ws/gemini` (hoặc `/ws/openai`) — đúng suffix WS mà
> proxy `campaign-api` route. Nếu `base_url` bị set bằng `llm_base_url` trần (thiếu
> `/ws/...`), giá trị đó được đưa thẳng vào SDK provider và **404 ngay ở Live
> handshake**. Vì vậy ô "Base URL" trong web Settings chỉ hiển thị *override tường
> minh* (`RealtimeBaseURLOverride`, không phải giá trị đã resolve), để "để trống là
> tự suy ra" luôn trống và mỗi lần Save không vô tình ghi đè URL trần.
>
> `gptlive` cũng theo quy tắc này: trống → suy `<llm_base_url>/ws/openai`, SDK
> thêm `/live/sessions`. Chỉ set `HAL_GPTLIVE_BASE_URL` / `realtime.gptlive.base_url`
> khi muốn nối thẳng `https://api.openai.com/v1` (route proxy chưa lên).

```json
{
  "wakeword": false,
  "realtime": {
    "enabled": true,
    "provider": "gemini",
    "gemini": { "model": "gemini-3.8-live", "voice": "Kore", "thinking_level": "LOW" },
    "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" },
    "gptlive": { "model": "gpt-live-1", "voice": "marin" },
    "pipecat_v1": { "model": "qwen/qwen3.6-35b-a3b" }
  }
}
```

Knob reasoning (`thinking_level` / `reasoning_effort`) default về mức **rẻ nhất**
(`MINIMAL` / `minimal`), không phải mức max của provider — muốn reasoning sâu hơn
thì set tường minh. `gptlive` không có knob reasoning: model Live không có,
`ValidateRealtimeKnobs` từ chối mọi giá trị reasoning cho nó và web ẩn selector.
Pipecat v1 **không có cả** knob voice lẫn knob reasoning (pipeline phát text và
giọng TTS của HAL đọc nó): `RealtimeVoice()` và `RealtimeReasoning()` trả về
rỗng, endpoint options trả về danh sách `voices.pipecat_v1` và
`reasoning.pipecat_v1` rỗng để web ẩn cả hai selector, `ValidateRealtimeKnobs`
từ chối mọi giá trị voice (`pipecat_v1 realtime has no voice`) hay reasoning
cho nó, và `realtime.set` chỉ ghi `model` và `web_search` (công tắc search
trong phiên — `validateRealtimeSet` từ chối nó với mọi provider khác) vào
sub-object `pipecat_v1`; nhãn web là "Pipecat v1 (on-device)" và trang hiện
một checkbox **Web search** cho nó.
Các knob KHÔNG có trong block (turn detection, session
resumption, memory, summarizer) vẫn chỉ theo env/default.

**Filter chống leak CoT.** Trên `gemini-3.1-flash-live-preview` KHÔNG tắt được
thinking: `thinking_level=MINIMAL` lẫn `thinking_budget=0` đều được chấp nhận
nhưng bị bỏ qua (đo `thoughts_token_count` 125–168 trên turn cần suy luận với mọi
config). Bình thường thoughts nằm nội bộ, nhưng trên các turn có
grounding/vision/tool, server thỉnh thoảng đổ nguyên text channel của model —
đoạn lập kế hoạch tiếng Anh ("The user is insisting…", "Phrasing draft:",
"Delivery guidance:") kèm câu trả lời thật — vào `output_audio_transcription`,
trong khi audio của model chỉ chứa câu trả lời sạch. Vì native audio tắt, HAL
đọc transcription → không có guard thì leak bị đọc thành tiếng (tốn ký tự TTS)
và forward vào `[REPLY]`, quay lại context và tự củng cố.
`drivers/voice/_internal/cot_leak_filter.py` chặn leak ở mức câu, trước TTS và
trước khi transcript được forward/lưu, theo 3 tầng: marker TRIGGER (ngôi thứ ba
gắn động từ "the user is/wants…", nhãn planning như "Phrasing draft:") luôn drop
và bật cot-mode cho turn; marker PHỤ ("persona", "system prompt", "emotion
tool", …) chỉ drop khi cot-mode đã bật — câu trả lời hợp lệ nói về chính thiết
bị vẫn an toàn; trong cot-mode drop thêm câu planning tiếng Anh (chỉ với device
không nói tiếng Anh — chữ viết không-Latin như tiếng Việt/Trung/Nhật dùng check
tỉ lệ ASCII, chữ Latin như Pháp/Indo yêu cầu thêm function word tiếng Anh để
answer thật không bị nuốt), draft trong ngoặc kép, mảnh plan vụn, và câu
gần-trùng câu đã giữ (CJK token theo từng ký tự). Check ngôn ngữ bỏ qua các
đoạn nằm trong ngoặc, nên câu planning tiếng Anh nhúng text ngôn-ngữ-trả-lời
trong ngoặc ("The search query 'cách dùng…' didn't yield…") vẫn bị bắt, còn
câu ngôn-ngữ-trả-lời trích dẫn tiếng Anh thì không. Mỗi câu bị drop đều log
`CoT leak dropped`.

Đường agent chính (reply openclaw/hermes nói qua os-server) có bản port Go của
filter này — `system/server/agent/delivery/http/cot_leak_filter.go` (thêm
TRIGGER identifier snake_case cho corpus leak DeepSeek); xem
`docs/vi/flow-monitor_vi.md` § "CoT-leak filter (đường agent)". Harden bên nào
thì nhớ sync bên kia.

### Cấu hình runtime (`hal/config.py` + `config.json`)

Mỗi biến môi trường `HAL_*` ghi đè setting tương ứng; `wakeword` là cờ top-level
trong `config.json`:

| Biến | Mặc định | Ghi chú |
|------|----------|---------|
| `HAL_REALTIME_ENABLED` | `true` | Cổng tổng cho pipeline realtime |
| `wakeword` | `voice.wakeword` trong ROBOT.md khi config còn mới, ngược lại `false` | Cổng wake word top-level trong config file. Khi bật, partial khớp chỉ là tín hiệu tạm: HAL chỉ commit audio buffer sang realtime hoặc forward command sau khi STT **final** xác nhận wake phrase. Transcript được tách thành câu (`.` `!` `?`) và phrase được chấp nhận ở đầu **hoặc cuối** bất kỳ câu nào; xuất hiện giữa câu bị từ chối. Bước xác nhận kiểm lại trên transcript đã ghép mà vẫn còn dấu câu, để bước merge chỉ giữ `\w+` không rút lại cái gate mà một partial đã mở. Nếu bước kiểm khớp tuyệt đối đó trượt nhưng trước đó đã có một partial khớp chính xác, thì riêng chữ TÊN được phép lệch 1 ký tự và gate vẫn được xác nhận: STT tự viết lại giả thuyết của nó ở final, và trên lamp-0c89 (04/09/2026) partial `hello lamp` quay lại thành `Hello, lamb.` làm rơi cả lượt — không mở lượt realtime, không có cue thinking, câu hỏi rơi xuống main agent chậm hơn nhiều. Tiền tố (`hello`, `hey`, …) vẫn phải khớp tuyệt đối, và luật lỏng này KHÔNG BAO GIỜ mở được gate mà chỉ xác nhận lại gate do một partial khớp chính xác đã mở, nên một từ gần giống trong lời nói xung quanh vẫn không đánh thức được gì. Nó được log riêng thành `Wake-word confirmed with a one-letter STT slip` để còn đếm được — nhiều dòng này nghĩa là keyterm boost đang không làm tròn việc. Các prefix hỗ trợ là `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, `wake up`, áp dụng cho alias chung cố định (`hey autonomous`), device type (`hey lamp`) và tên agent hiện tại (`hey Luna`). Runtime rename chỉ cập nhật alias theo tên agent. Bare name và các prefix khác không mở gate. Một câu bị từ chối sẽ bị bỏ và LED `listening` tạm thời được restore về trạng thái nghỉ bình thường; không bao giờ để hiệu ứng `idle` cố định tiếp tục chạy. Một lượt đã xác nhận mở cửa sổ focus follow-up; lượt trong cửa sổ đó được forward dưới type `voice_followup` mà không cần wake phrase khác. Mọi lượt được phép đều dispatch sang os-server: câu realtime đã nói thành event đồng bộ im lặng `voice_agent_handled`; realtime unavailable, im lặng, lỗi hoặc delegate đi theo đường thường. Nếu realtime tắt hoặc không khả dụng, final transcript đã xác nhận đi theo đường os-server/main agent thường. Với Live ON và realtime khả dụng, lần thu đã xác nhận chuyển sang live song công mà không commit audio thủ công. Thiếu/`false` giữ nguyên luồng luôn lắng nghe trước gate. Với `config.json` do os-server tạo ra, giá trị khởi tạo lấy từ `voice.wakeword` của body (xem phần Cổng wake word ở trên); config nạp lên mà không có key thì vẫn là `false`. HAL restart sau khi lưu ở local Settings hoặc MQTT `wakeword.gate`. |
| `HAL_HARNESS_PROGRESS_SPEAK_P` | `0.15` | Xác suất một snapshot Harness chỉ có progress được đọc; `0` tắt đọc progress. Xem [Thông báo cập nhật Harness](#thông-báo-cập-nhật-harness). |
| `HAL_HARNESS_PROGRESS_MIN_GAP_S` | `60` | Tối đa một dòng progress được đọc cho mỗi run Harness trong khoảng này. |
| `HAL_HARNESS_PROGRESS_QUIET_START_S` | `15` | Không đọc progress quá sớm như vậy sau request. |
| `HAL_HARNESS_PROGRESS_MAX_AGE_S` | `30` | Progress trong hàng đợi cũ hơn mức này bị bỏ. |
| `HAL_HARNESS_UPDATE_MAX_AGE_S` | `600` | Kết quả/câu hỏi chưa đọc cũ hơn mức này bị bỏ. |
| `HAL_HARNESS_ANNOUNCE_GRACE_S` | `1.5` | Thời gian yên lặng sau bất kỳ lời nói hay transcript người dùng nào trước snapshot tiếp theo. |
| `HAL_HARNESS_ANNOUNCE_CONTENT_MAX_CHARS` | `4000` | Văn bản Harness đưa cho bộ diễn đạt bị cắt tới độ dài này. |
| `HAL_HARNESS_ANNOUNCE_SUMMARIZER_TIMEOUT_S` | `12` | Giới hạn thời gian của summarizer fallback trước khi đọc văn bản đã làm sạch thay thế. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Số giây idle của cửa sổ focus sau lệnh. Mỗi `voice_command` hoặc `voice_followup` được nhận sẽ refresh cửa sổ. `0` tắt follow-up và buộc mỗi phiên mic phải có wake phrase. Bị bỏ qua khi `wakeword` là false. |
| `HAL_ENDPOINT_SILENCE_S` | `0.8` | Thời gian im lặng từ lúc STT final về, chỉ áp dụng khi `final_ts >= last_confirmed_speech`. Nếu có tiếng nói được xác nhận sau final đó, quay lại ngưỡng dự phòng 2.5s tới khi có final mới. `0` tắt đồng hồ ngắn, chỉ dùng `HAL_SILENCE_TIMEOUT`. Khi bật gate dùng chung, đây chỉ là đề xuất kết thúc; `HAL_TURN_END_*` quyết định đóng lượt. |
| `HAL_TURN_END_ENABLED` | `true` | Gate kết thúc lượt tạm thời dùng chung cho thu hands-free khi Live tắt, trước commit; không đổi Live hoặc thu thủ công. `false` khôi phục đồng hồ im lặng và trần phiên cũ. |
| `HAL_TURN_END_FALLBACK_S` | `2.5` | Im lặng tối thiểu cho transcript thông thường khi Smart Turn thiếu/đang chờ, và cho lời chào ngắn; ứng viên im lặng gốc cũng phải đủ điều kiện. |
| `HAL_TURN_END_MAX_PAUSE_S` | `6.0` | Im lặng tối đa trước khi đóng ứng viên có dấu hiệu ngập ngừng hoặc model báo chưa hoàn tất; được giới hạn dưới bằng fallback. |
| `HAL_TURN_END_MAX_DURATION_S` | `180` | Trần thu cho phiên hands-free tăng cường đã nhận được chữ. Chạm trần thì loại bỏ yêu cầu, không dispatch; thu thủ công/chưa có chữ vẫn dùng `HAL_MAX_SESSION_DURATION_S`. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Yêu cầu Silero xác nhận có tiếng nói trước khi refresh đồng hồ im lặng kết thúc lượt. RMS vẫn là cổng chặn rẻ chạy trước; đặt `false` để quay về phát hiện im lặng thuần RMS. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Số frame gom lại cho mỗi lần chạy Silero ở bước kiểm đó — Silero tốn ~20 ms/frame trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `gptlive` \| `pipecat_v1` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = activity detection thủ công; OpenAI: off = `turn_detection: null`, lượt do client commit + `response.create`; `server_vad` / `semantic_vad` nhận knob từ `HAL_LIVE_VAD_*` và `HAL_OPENAI_VAD_THRESHOLD`). `HAL_LIVE_MODE=true` ép `off` → `server_vad`. GPT-Live bỏ qua knob này: Live không có cấu hình VAD, adapter tổng hợp lượt từ transcript input và khoảng im lặng output |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Số giây tối đa `receive()` chờ output event kế tiếp trước khi kết thúc lượt im lặng (fallback sang main agent) |
| `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S` | `6.0` | Grace routing thông thường sau terminal Gemini extended-thinking đầu tiên; tiến độ đã xác minh hoặc đáp án đang sinh tiếp có thể kéo dài. `0` tắt grace thông thường, không tắt yêu cầu outcome. |
| `HAL_REALTIME_PROGRESS_TIMEOUT_S` | `15.0` | Hạn gia hạn khi Gemini extended-thinking có tiến độ search/tool, tính từ commit audio; tiến độ lặp lại không đặt lại hạn. `0` tắt gia hạn. Chunk đáp án thật vẫn có thể tiếp tục với giới hạn im lặng receive gap. |
| `HAL_REALTIME_GROUNDING_DEBUG` | `false` | In toàn bộ field của `grounding_metadata` từ Gemini, mỗi lượt có grounding một lần (`grounding_chunks`, `grounding_supports`, `search_entry_point`, …). Chỉ để chẩn đoán và rất dài dòng; nó sinh ra để phân biệt lượt mà search thật sự không trả về gì với lượt bị cắt payload trên đường truyền. Đo trên lamp-0c89 04/09/2026 qua bốn lượt có grounding, payload luôn về đủ — nên `chunks=0` nghĩa là model không dùng nguồn nào cho câu trả lời đó. |
| `HAL_REALTIME_TURN_MAX_SILENCE_S` | `20.0` | Trần liveness cũ cho provider/chế độ ngoài Gemini extended-thinking khi Live tắt (dùng tiến độ đã xác minh). `receive()` chỉ kéo dài quá `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` khi lưu lượng vào chứng minh model còn đang làm việc (search grounding không phát output tới khi xong); trần này chặn trường hợp server nói liên tục mà không bao giờ ra output. `0` tắt cơ chế giữ lượt, quay về watchdog gap thuần. |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Watchdog im-lặng dùng thay mặc định cho turn có `look` (theo từng turn, qua `extend_recv_timeout()`). Gemini bị ép thinking trên frame dày chữ có thể im >8 s ngay trước khi trả lời — watchdog mặc định giết nhầm mấy turn đó. Nâng nó lên là hoãn luôn handoff frame `look`, nên phải giữ `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` cao hơn |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Không bao giờ commit turn empty-STT lên model. Final transcript chỉ có dấu câu/ký hiệu (ví dụ `.`) được chuẩn hoá thành empty trước gaze, speaker-ID, realtime, dispatch hay refresh follow-up; nó không thể tạo `voice_followup`. Giọng thật mà nova-3 miss (câu ngắn) vẫn là voiced nên qua hết guard VAD/Silero, commit audio thô khiến model bịa câu trả lời cho khoảng im lặng (lời chào chung chung, thường kèm tên không ai nói). Khi `true`, mọi turn empty-STT bị bỏ bất kể duration/voicing — im còn hơn trả lời sai. Đặt `false` để quay về đường audio-only gated bằng Silero bên dưới. |
| `HAL_REALTIME_AI_REJECT_FILTER` | `true` | Đăng ký `reject_turn` và bật policy gate tách riêng `should_drop_realtime_rejection()`. Tool call rõ ràng sẽ bỏ transcript trước OS dispatch; model im lặng, timeout hay lỗi vẫn fallback sang main agent. Noise guard deterministic riêng cũng terminal cho audio mà nó đã phân loại là không phải tiếng nói. Đặt `false` để tắt filter AI thử nghiệm này mà không đổi phần routing realtime còn lại. |
| `HAL_REALTIME_FIRST_CHUNK_MAX_CHARS` | `0` | Mặc định nói ngay câu hoàn chỉnh đầu tiên và tổng hợp trước các câu sau qua hàng đợi, không chờ toàn bộ câu trả lời. Giá trị dương bật cắt mệnh đề đầu, hoặc cắt theo khoảng trắng khi vượt giới hạn này; câu hoàn chỉnh bỏ qua bộ cắt. Giữ nguyên voice tag trong ngoặc vuông (kể cả khi cắt theo khoảng trắng), bỏ qua dấu phẩy/hai chấm trong số và dấu hai chấm của URL, yêu cầu 8 ký tự hiển thị ngoài tag. Cắt sớm vẫn có thể tạo khoảng ngắt giữa các yêu cầu tổng hợp. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Session ngắn hơn ngưỡng này mà không có STT transcript bị coi là nhiễu VAD, không commit lên model. Chỉ xét khi `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_NOISE_GUARD_MAX_WORDS` | `3` | Mở rộng guard voiced-ratio của Silero sang cả turn CÓ transcript, tối đa ngần này từ. STT bịa một từ đệm ngắn từ tiếng ồn phòng và báo confidence tối đa cho nó, nên turn kiểu đó trước đây lọt hết mọi guard (guard chỉ chạy khi transcript rỗng) và commit nhiễu thuần lên model. Transcript nhiều nhất ngần này từ sẽ bị kiểm lại theo `HAL_REALTIME_NOISE_SPEECH_RATIO` và bị bỏ nếu audio chưa từng voiced; lệnh ngắn nói thật vẫn là voiced nên vẫn commit. Tỉ lệ được đo trên **span voiced** — từ chunk voiced đầu tới chunk voiced cuối — chứ không phải toàn buffer, vì bản capture luôn kèm pre-roll của VAD ở đầu và 200ms đuôi giữ lại ở cuối; phần đệm cố định đó làm loãng câu ngắn nặng hơn câu dài rất nhiều. Đo toàn buffer từng vứt nhầm một câu `Yes, that's right.` nói thật ở mức 0.500 (`peak=1.000`) — tức là guard quay ra phạt đúng lớp câu nó sinh ra để soi. Tiếng ồn kéo dài vẫn rớt, vì các chunk voiced của nó thưa ngay bên trong span. Transcript dài hơn không bao giờ bị kiểm lại, nên ngưỡng này không thể làm câm một câu nói thật. `0` = tắt. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Kiểm soát chi phí: khi một turn đến sau ngần này giây im lặng (tính từ mốc muộn hơn giữa turn trước và lúc session hiện tại kết nối), recycle (rebuild) session **sau** turn đó để turn kế tiếp bỏ phần context mỗi-turn mà provider re-bill trên session sống lâu. Turn sau khoảng nghỉ dài coi như cuộc hội thoại mới; trí nhớ dài hạn vẫn còn nhờ nạp lại `summary.md`. Với Gemini native-audio, bước này bị bỏ qua nếu pre-turn recycle thành công đã làm mới session cho chính idle gap đó. `0` = tắt. Dùng lại đường rebuild của zombie-recovery. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume cùng session Gemini qua reconnect. Mặc định OFF — proxy `campaign-api` không forward đúng resumption handshake nên resume qua nó tạo session zombie (cold reconnect thì chạy được). Chỉ bật khi endpoint hỗ trợ. |
| `HAL_GEMINI_IDLE_PARK_S` | `45` | Park Gemini khi idle: đóng transport của session sau ngần này giây không có hoạt động turn, để server không phải đóng nó bằng WS `1008` (backend ghi thành lỗi và bắn cảnh báo). Orchestrator vẫn `available` trong lúc parked; `prepare_turn()` của turn kế tiếp nối lại đồng bộ trước khi stream audio. Phải nhỏ hơn thời gian idle chết ngắn nhất đo được (86 giây). `0` = tắt. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `60` | Guard transport cho Gemini: khi lượt nói mới bắt đầu sau ngần này giây session idle (tính từ mốc muộn hơn giữa turn trước và lúc session kết nối), rebuild session Gemini **trước khi** stream pre-roll/audio để turn không đụng socket chết vì idle ở proxy/SDK. `0` = tắt. Pre-turn recycle thành công sẽ chặn idle recycle generic sau chính turn đó, nên một idle gap chỉ tạo tối đa một rebuild phục vụ transport/chi phí. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Chọn context manager (cũng đọc từ `agent_runtime` trong config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Key Gemini; fallback về `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `LOW` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH`. `gemini-3.8-live-extended-thinking` không có MINIMAL (HAL tự kẹp về LOW); `gemini-3.8-live` thường từ chối thinkingLevel nên HAL bỏ hẳn field đó |
| `HAL_GEMINI_GOOGLE_SEARCH` | `true` | Google Search grounding (chỉ Gemini). Cho model realtime tự trả lời câu dữ liệu công khai theo thời gian thực (thời tiết, tin tức, lookup) ngay trong phiên thay vì delegate. Tính phí theo mỗi grounded request (cộng token); chỉ phát sinh khi Gemini quyết định search. Cũng đặt được qua `realtime.gemini.google_search` trong config.json. |
| `HAL_GEMINI_VISION` | `true` | Tool `look` trong phiên (chỉ Gemini). Cho model realtime chụp một frame camera và trả lời câu hỏi thị giác ("cái này là gì?") ngay trong phiên thay vì delegate. Mặc định bật; chỉ đăng ký khi thiết bị còn có capability `vision`. Cũng đặt được qua `realtime.gemini.vision` trong config.json. |
| `HAL_GEMINI_VISION_MAX_WIDTH` | `768` | Bề rộng tối đa (px) frame được downscale trước khi gửi — giới hạn token ảnh. |
| `HAL_GEMINI_VISION_MIN_INTERVAL_S` | `10` | Chặn chi phí: số giây tối thiểu giữa hai lần **gửi ảnh**. Gọi `look` lặp trong khoảng này (hoặc gọi lần hai trong cùng turn) sẽ xài lại ảnh đã có trong context thay vì gửi ảnh mới. `0` = luôn gửi ảnh mới. |
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `45` | Tuổi tối đa của frame `look` còn được bàn giao cho main agent khi delegate/timeout fallback để nó xài lại ảnh thay vì chụp lại. **Phải lớn hơn `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` cộng thời gian dispatch** — nhánh timeout fallback chỉ chạy sau khi watchdog đó hết giờ, nên để bằng nhau là mọi frame đều hết hạn (cả hai cùng bằng `20` từ 2026-07-06 đến 2026-08-24 và handoff chưa từng bắn lần nào). `0` tắt guard tuổi (frame vẫn bị clear mỗi turn). |
| `OPENAI_API_KEY` | — | Key OpenAI, dùng chung cho OpenAI Realtime **và** GPT-Live; fallback về `llm_api_key` (GPT-Live: env > `realtime.gptlive.api_key` > `realtime.api_key` > `llm_api_key`) |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — default rẻ (trước là `xhigh`) |
| `HAL_OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Model input transcription (`audio.input.transcription.model`) — nguồn duy nhất của lời người dùng trên đường OpenAI nên luôn bật; `gpt-4o-mini-transcribe` stream delta, `whisper-1` chỉ gửi bản `completed`. Cũng đọc từ `realtime.openai.transcribe_model` (HAL đọc, Go không model — xem trên) |
| `HAL_OPENAI_NOISE_REDUCTION` | `far_field` | `far_field` \| `near_field` \| `off` — `audio.input.noise_reduction` phía server, lọc trước VAD và model; `off` bỏ hẳn key. Cũng đọc từ `realtime.openai.noise_reduction` |
| `HAL_OPENAI_VAD_THRESHOLD` | `0` | Ngưỡng `server_vad.threshold` (0..1, API mặc định 0.5). `0` = suy từ `HAL_LIVE_VAD_START_SENSITIVITY` (`low` → 0.7, `high` → 0.3); giá trị khác 0 thắng. Chỉ env, không có key config.json |
| `HAL_GPTLIVE_MODEL` | `gpt-live-1` | Cũng đọc từ `realtime.gptlive.model` |
| `HAL_GPTLIVE_VOICE` | `marin` | 13 giọng Live (`GPTLiveVoice`: marin, quartz, ripple, vesper, willow, stone, gleam, meridian, bossa, tempo, beacon, delta, cinder). Cũng đọc từ `realtime.gptlive.voice` |
| `HAL_GPTLIVE_BASE_URL` | *(rỗng → base URL của OpenAI Realtime: `HAL_OPENAI_REALTIME_BASE_URL` > `realtime.base_url` > `<llm_base_url>/ws/openai`)* | Cũng đọc từ `realtime.gptlive.base_url`. SDK nối thêm `/live/sessions`, nên proxy phải serve `…/ws/openai/live/sessions` (đang chờ, 2026-09-16; tới lúc đó 404). Set `https://api.openai.com/v1` để nối thẳng |
| `HAL_GPTLIVE_SAMPLE_RATE` | `24000` | `16000` \| `24000` — MỘT định dạng PCM cho cả hai chiều (`output_sample_rate == sample_rate`); 16000 giảm nửa băng thông uplink. Chỉ env |
| `HAL_GPTLIVE_TURN_GAP_MS` | `800` | Ranh giới lượt tổng hợp: reply coi là xong khi không có output audio/transcript trong ngần này ms (watchdog tick 50 ms). Chỉ env |
| `HAL_GPTLIVE_INTERRUPT_GAP_MS` | `400` | Khi user nói đè lên reply: model im lặng quá ngần này ms → reply bị ngắt (`server_interrupt`); model nói tiếp → backchannel. Chỉ env |
| `HAL_GPTLIVE_INPUT_GAP_MS` | `1500` | Hai mảnh `session.input_transcript.delta` cách nhau hơn ngần này ms (timeline session) thuộc user turn MỚI dù model chưa đáp. Chỉ env |
| `HAL_GPTLIVE_COMMIT_SILENCE_MS` | `600` | Chỉ đường lượt: Live không có commit, `commit_audio()` append ngần này ms PCM im lặng (`event_id` `hal-silence`) để model nghe câu nói đã hết. LIVE_MODE → no-op. Chỉ env |
| `HAL_GPTLIVE_DELEGATION_WAIT_MS` | `500` | Hạn cứng để forward `session.delegation.created` thành `delegate_to_main`: adapter forward ngay khi transcript input im 250 ms, chậm nhất là ở hạn này với những gì đã nghe (rỗng → orchestrator từ chối và model được báo handoff thất bại). Chỉ env |
| `HAL_GPTLIVE_IDLE_PARK_S` | `30` | Park GPT-Live khi idle: session bị tính tiền theo phút khi còn mở, nên sau ngần này giây không có turn orchestrator đóng transport (`_maybe_park_idle_session`, cơ chế y như `HAL_GEMINI_IDLE_PARK_S`) và nối lại ở turn kế. `0` = tắt. Chỉ env |
| `HAL_PIPECAT_API_KEY` | *(rỗng → `realtime.pipecat_v1.api_key` > `realtime.api_key` > `llm_api_key`)* | Bearer key cho endpoint chat |
| `HAL_PIPECAT_BASE_URL` | `https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1` | Cũng đọc từ `realtime.pipecat_v1.base_url`. Bất kỳ base chat-completions tương thích OpenAI nào; `realtime.base_url` dùng chung không bao giờ được dùng (hình dạng relay WS). `https://vibe-agent-gateway.eternalai.org/v2` serve cùng model không cần key |
| `HAL_PIPECAT_MODEL` | `qwen/qwen3.6-35b-a3b` | Cũng đọc từ `realtime.pipecat_v1.model` |
| `HAL_PIPECAT_TEMPERATURE` | `0.7` | Temperature sampling của LLM |
| `HAL_PIPECAT_MAX_TOKENS` | `300` | Trần độ dài reply mỗi response |
| `HAL_PIPECAT_DISABLE_THINKING` | `true` | Gửi `extra_body.chat_template_kwargs.enable_thinking=false` (Qwen3 / vLLM); Pipecat chỉ stream `content`, nên thinking sẽ thành khoảng im lặng chết. `false` cho endpoint từ chối extra body |
| `HAL_PIPECAT_STT_API_KEY` / `HAL_PIPECAT_STT_BASE_URL` / `HAL_PIPECAT_STT_MODEL` | `llm_api_key` / `llm_base_url` / `stt_model` | Chỉ khi agent phải tự dựng `AutonomousSTT` của riêng nó (không có provider từ `VoiceService` được inject — test, `/voice/start` trước STT); trên thiết bị đang chạy pipeline dùng STT provider của `VoiceService` |
| `HAL_PIPECAT_SAMPLE_RATE` | `16000` | Rate PCM mic mà pipeline mong đợi (không resample: Silero, Smart Turn và relay STT đều nhận 16 kHz natively) |
| `HAL_PIPECAT_SMART_TURN` | `true` | Chế độ live: Smart Turn v3 (ONNX đóng gói sẵn, CPU) quyết định end-of-turn sau mỗi lần Silero stop; `false` → thay bằng timeout im lặng `HAL_PIPECAT_SILENCE_TIMEOUT_S`. Bị bỏ qua trên đường turn-based |
| `HAL_PIPECAT_SMART_TURN_STOP_SECS` | `3.0` | Chế độ live: khoảng im lặng dài nhất Smart Turn chờ trước khi ép đóng lượt |
| `HAL_PIPECAT_VAD_CONFIDENCE` | `0.85` | Confidence Silero ở chế độ live (Pipecat mặc định 0.7 — giá trị far-field: ở 0.8 lamp vẫn trả lời một cuộc trò chuyện ở phía bên kia phòng, 2026-09-18) |
| `HAL_PIPECAT_VAD_START_SECS` | `0.2` | Chế độ live: tiếng nói phải kéo dài ngần này trước khi Silero báo onset |
| `HAL_PIPECAT_VAD_STOP_SECS` | `0.2` | Chế độ live: khoảng im lặng trước khi Silero báo stop — giá trị mà các con số độ trễ tích hợp sẵn của Smart Turn giả định; model, chứ không phải timer này, quyết định lượt đã xong hay chưa |
| `HAL_PIPECAT_VAD_MIN_VOLUME` | `0.7` | Sàn âm lượng Silero ở chế độ live (Pipecat mặc định 0.6; giá trị far-field) |
| `HAL_PIPECAT_SILENCE_TIMEOUT_S` | `0.8` | Chế độ live khi Smart Turn tắt: khoảng im lặng sau tiếng nói để kết thúc lượt |
| `HAL_PIPECAT_MIN_WORDS` | `2` | Chế độ live: **khi model đang sinh** (hoặc một tool call đang chạy) một user turn mới — và interruption nó broadcast — chỉ bắt đầu khi STT đã transcribe được ngần này từ; ngoài lúc đó một từ là đủ mở lượt, nên "yes" / "stop" vẫn hoạt động. `_BusyAwareMinWordsStrategy` gắn `MinWordsUserTurnStartStrategy` của Pipecat vào trạng thái LLM của agent vì strategy gốc cần các `BotStartedSpeakingFrame` mà pipeline này không bao giờ có. Trên lamp-ee17 một tiếng bật ra một từ (`do.`) ngay sau câu hỏi đã mở một lượt và hủy reply giữa chừng; `0` = mặc định của Pipecat, bắt đầu theo VAD/transcription |
| `HAL_PIPECAT_TURN_STOP_TIMEOUT_S` | `5` | Watchdog cho user turn mà transcript không bao giờ về: aggregator vẫn finalize nó (turn-based: session đã commit mà rỗng thì đã kết thúc lượt từ trước) |
| `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S` | `15` | Thời gian một tool call được bắc cầu chờ `FunctionCallResultInput` của orchestrator trước khi model nhận `{"error": "no result from the device"}` (không có follow-up) |
| `HAL_PIPECAT_WEB_SEARCH` | `true` | Đăng ký tool `web_search` phía client (chỉ pipecat): dữ kiện công khai theo thời gian thực được trả lời ngay trong phiên qua relay Google-Search thay vì delegate sang main. Cũng đọc từ `realtime.pipecat_v1.web_search` trong config.json, hoặc checkbox **Web search** trên `/setting#realtime` |
| `HAL_PIPECAT_SEARCH_URL` | `https://campaign-api.autonomous.ai/api/v1/ai/v1/google-search/v1beta/interactions` | Endpoint Gemini Interactions mà tool POST tới (`tools: [{"type": "google_search"}]`) |
| `HAL_PIPECAT_SEARCH_MODEL` | `gemini-3.7-flash` | Model relay dùng để ground |
| `HAL_PIPECAT_SEARCH_API_KEY` | *(rỗng → key chat, theo thứ tự resolve của `HAL_PIPECAT_API_KEY`)* | Bearer key cho relay search |
| `HAL_PIPECAT_SEARCH_TIMEOUT_S` | `10` | Timeout HTTP của một lookup (~4 s đo được). Chặn vòng output của lượt, nên giữ nhỏ hơn `HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S`, nếu không lỗi chung của bridge sẽ trả lời trước |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |
| `HAL_REALTIME_SUMMARIZER_RETRIES` | `2` | Số lần thử lại mỗi lượt summarize; `0` là tắt |
| `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S` | `1.5` | Chờ trước lần thử lại đầu, mỗi lần sau nhân đôi |
| `HAL_REALTIME_SUMMARY_OPEN_REQUEST_TTL_S` | `3600` | Summarizer đặt các request chưa được trả lời vào một mục `## Open requests` ở cuối (bullet có timestamp). HAL xóa từng bullet khỏi `summary.md` khi timestamp `[<ISO-8601>]` của nó đã cũ bằng số giây này (bullet không có timestamp đọc được thì dùng tuổi file thay thế; heading bị xóa khi không còn bullet nào), cả khi refeed lại thành `[Previous summary]` lẫn khi nạp vào session context — một task đang chờ nằm lì trong context là thứ khiến một nudge rỗng nội dung làm Gemini "trả lời" nó từ ký ức cũ (#419, #421). `0` là tắt. |

## Bản đồ code

| File | Vai trò |
|------|---------|
| `orchestrator.py` | Vòng đời session, tool `delegate_to_main` + `express_emotion` + `look`, stream lượt |
| `voice_agent/base.py` | Agent trừu tượng: contract 2-thread/queue, `receive()` |
| `voice_agent/gemini_live.py` | Provider Gemini Live (IO loop asyncio) |
| `voice_agent/openai_realtime.py` | Provider OpenAI Realtime (sync, SDK `openai` GA, connection serialize bằng lock; contract live-mode ngang Gemini — `user_turn_id`, `UserSpeechOutput`, barge-in + `conversation.item.truncate`; bảng giá `_OPENAI_RATES` + log `openai_usage.log`) |
| `voice_agent/gpt_live.py` | Provider GPT-Live (`gptlive`; sync, SDK `openai` ≥ 3.14.1 `client.live`; contract live-mode **tổng hợp** — watchdog `gptlive-watchdog` đóng lượt theo `turn_gap_ms`, barge-in theo `interrupt_gap_ms`, `UserSpeechOutput` từ `session.input_transcript.delta`; client delegation → `delegate_to_main`, feedback qua `session.thinking.append`; $0.05/phút + log `gptlive_usage.log`) |
| `voice_agent/pipecat_v1.py` | Provider Pipecat v1: contract `VoiceAgentBase` (thread, queue, generation theo user-turn, rào `end_turn()`, cầu nối tool, cả hai hình dạng `HAL_LIVE_MODE`) mà không cần `import pipecat`; unit-test ở `hal/test/test_pipecat_v1_agent.py` |
| `voice_agent/pipecat_pipeline.py` | Phía Pipecat: dựng pipeline (`HALSTTService` → user aggregator → `OpenAILLMService` → `EventSink` → assistant aggregator) trên loop `pipecat-io`, tool handler, `PipelineHandle` (queue_frame / proposal / finalize / stop thread-safe), `_CommittedTurnStopStrategy` |
| `voice_agent/pipecat_stt.py` | `HALSTTService`: `STTService` của Pipecat bọc `STTProvider` của HAL (session theo lượt hoặc session dài trên thread sender `pipecat-stt`), `STTFinalizeFrame` |
| `context_manager/{base,openclaw,hermes}.py` | Lắp ráp prompt + memory + skills theo gateway |
| `summarizer.py` | Summarizer memory dựa trên Anthropic (cũng là bộ diễn đạt fallback của announcer Harness qua `system_prompt=`) |
| `../drivers/harness/{announcer,update_queue}.py` | Hàng đợi cập nhật Harness, chính sách snapshot, gate và chọn bộ diễn đạt |
| `../drivers/voice/_internal/realtime_announce.py` | Envelope announcement Harness và phát realtime |
| `config.py` | Model config provider (`GeminiConfig`, `OpenAIConfig`, `GPTLiveConfig`, `PipecatV1Config`) |
| `models/`, `enums/` | Kiểu input/output/event, enum provider + gateway |
| `resources/` | System prompt (chung `system_prompt.md` + theo provider `system_prompt_gemini.md` / `system_prompt_openai.md` / `system_prompt_gptlive.md` / `system_prompt_pipecat.md`) |
| `../voice/voice_service.py` | Tích hợp: stream audio mic, tiêu thụ output, route delegate/handled. Chế độ live: `_live_decision` / `_live_session` / `_live_out_pump` / `_live_uplink_frame` |
| `../voice/aec.py` | WebRTC AEC3 trên đường mic; tham chiếu lấy tại TTS output stream (mọi provider) |

### Event hoàn tất agent của Buddy

Managed session trên desktop báo completed/needs_input/error qua sensing route chuẩn bằng type `buddy.agent.<session_id>`. Event thụ động này được queue khi agent hoặc speaker bận; type riêng theo session giữ được thông báo song song. Policy sleep và voice privacy hiện có vẫn áp dụng. Lamp dùng skill `agent-management` và project/session ID tường minh cho follow-up. Summary là dữ liệu kết quả không đáng tin cậy, không phải quyền chạy tool. Delivery là best effort; đọc `agent.session` là cách phục hồi trạng thái.

### Chuyển tiếp câu bổ sung cho tác vụ desktop trong realtime

Cả ba biến thể prompt realtime và mô tả chung của `delegate_to_main` đều chuyển thao tác app desktop native cùng câu trả lời, sửa đổi hoặc yêu cầu dừng rõ ràng cho tác vụ main agent đang chờ về main agent; realtime không phát lời nói trong lượt chuyển tiếp. Message chỉ chứa lời người dùng vừa nói được hiểu rõ và tham số đã cung cấp, giữ đủ mọi vế yêu cầu. Ngữ cảnh tác vụ chỉ dùng nội bộ để quyết định chuyển tiếp; không thêm hoặc kể lại vì main agent đã giữ cuộc hội thoại. Câu ngắn như “cuối tuần này, hai người” có thể tiếp nối câu hỏi bổ sung cho việc tìm chỗ ở trước đó; không được bỏ chỉ vì thiếu động từ hành động hoặc tự suy diễn thành ngày cụ thể.

Câu hỏi main agent đã nói gần đây trong `[TTS HISTORY]` được dùng làm ngữ cảnh để hiểu câu tiếp nối, đồng thời vẫn giữ quy tắc không nói lặp. `[TTS HISTORY, not spoken]` không chứng minh người dùng đã nghe hoặc trả lời câu hỏi đó. Kiểm tra hội thoại nền và việc lời nói có hướng tới device vẫn giữ nguyên. Thay đổi này dùng lịch sử bàn giao/câu trả lời realtime sẵn có; không thêm kho trạng thái tác vụ đang chờ có cấu trúc, không tự chứng minh voice routing trên thiết bị thật đã thành công và không loại bỏ giới hạn truyền context theo provider.

Message chuyển tiếp phải giữ tên ứng dụng và nội dung đọc để ghi, không chỉ chủ đề chung. Ví dụ “Ghi vào Notes là chiều mua sữa” phải giữ Notes và nguyên văn “chiều mua sữa”; rút thành lời nhắc mua sữa chung làm mất cả đích lẫn nội dung. Các biến thể prompt và mô tả tool nay nêu rõ yêu cầu này. Một quan sát bằng audio tổng hợp đã cho thấy message chuyển tiếp bị mất thông tin; khi thiếu transcript đầu vào, chưa thể tách lỗi nhận dạng âm thanh khỏi lỗi tóm tắt, và thay đổi câu chữ vẫn cần kiểm chứng hành vi.

Yêu cầu hoặc câu bổ sung hiện tại được chuyển tiếp bằng ngôn ngữ người dùng vừa nói, không thêm bình luận hoặc tóm tắt các lượt trước. Dịch sang tiếng Anh có thể khiến main agent trả lời sai ngôn ngữ vì instruction chuyển tiếp là đầu vào chính của nó.

Một lượt so sánh audio tổng hợp riêng bằng Gemini 3.1 Live sau đó dùng cùng PCM cho prompt/tool baseline và bản cuối. Message chuyển tiếp của bản cuối là “Ghi vào Notes là sáng mai tưới cây.”, “Mở Airbnb tìm chỗ ở Đà Nẵng giúp mình.” và câu tiếp nối “cuối tuần này hai người”. Baseline đã đổi yêu cầu Notes thành “Remember to water the plants tomorrow morning.”, làm mất tên app và đổi ngôn ngữ. Câu tiếp nối Airbnb chạy trong cùng phiên provider sau câu hỏi bổ sung `[TTS HISTORY]` có kiểm soát; đã xác nhận ranh giới hoàn tất lượt trước từ server và commit audio mới. Kết quả này chứng minh hành vi chuyển tiếp quan sát được cho các clip tổng hợp đó, không chứng minh microphone/wake-word, câu hỏi thật từ main agent hoặc hoàn thành toàn luồng main-agent/desktop. Kết quả cuối riêng được lưu tại `/tmp/buddy-rt-final/result.json` trên thiết bị kiểm thử; lượt đánh giá không thay prompt production hoặc dịch vụ đang chạy.

Realtime và Harness-only voice dùng chung journal `system/externalhistory` và worker gửi silent. HAL vẫn gửi `voice_agent_handled` với `[HANDLED]` / `[REPLY]`; OS ghi atomic lượt realtime hoàn tất trước khi xác nhận nhận và gửi tiếp history pending chưa từng gửi sau restart. Hook ngắt lời cũ chạy trước bước lưu; silent/chặn TTS giữ nguyên. Runtime hỗ trợ active-turn steering vẫn nhận history realtime khi bận; runtime khác chờ rảnh bằng queue trên disk. Lượt gửi chưa rõ kết quả giữ `uncertain`, không tự gửi lại. Flow Monitor hiện **History sync · Realtime → Main**, câu hỏi/câu trả lời gốc là Context. Xem [lịch sử hội thoại từ bên ngoài](os-server_vi.md#lịch-sử-hội-thoại-từ-bên-ngoài).

Phân loại input LIVE còn được gửi trong metadata debug `voice_turn_type`, dùng bộ phân loại wake phrase thông thường và focus đã cho phép input. Reply realtime trực tiếp giữ event routing `voice_agent_handled`; monitor có thể hiển thị command/follow-up độc lập.

Chẩn đoán: `[realtime][timing]` ghi lúc đưa audio commit vào hàng đợi, progress đầu tiên được xác nhận, nhận grounding, bắt đầu giữ continuation, phát/bỏ continuation, hết thời gian chờ và receive timeout. Thời gian dùng đồng hồ monotonic tính từ commit gần nhất được đưa vào hàng đợi (không phải lúc người dùng nói xong), kèm generation và thời gian progress/output còn lại. Event đến muộn có thể xuất hiện sau commit mới; các trường này không chứng minh request nào đã khởi tạo search. `Google Search metadata received (search start unknown)` đánh dấu lúc nhận metadata grounding, không phải lúc bắt đầu search. Provider không cung cấp mốc bắt đầu search ở đây; không suy ra thời gian chạy search từ log này.

Replay camera với Gemini extended-thinking: khi audio replay được commit thành công, vòng nhận được đánh thức và kết thúc grace, kiểm tra outcome và continuation buffer của filler cũ, dù Gemini có gửi `interrupted` hay không. Replay nhận generation phản hồi và thời hạn progress mới, giữ câu hỏi của người dùng. Provider xử lý ranh giới phản hồi cũ trước khi có lời đáp mới; queue consumer không còn nuốt terminal fallback của chính replay. Commit thông thường và LIVE mode không kích hoạt reset này; user interrupt sau khi replay bắt đầu trả lời vẫn cancel phản hồi. Phản hồi mới vẫn cần outcome được xác nhận; thay đổi này không ép yêu cầu ảnh thành công hoặc tắt fallback. Log chẩn đoán: `look_replay_response_started`.

Thứ tự delegation của Gemini: với việc cần main (gồm nhạc, truy xuất memory cụ thể và tác vụ Harness/code), yêu cầu chỉ gọi thật `delegate_to_main`, không để Gemini nói hoặc gọi emotion trước handoff. Cue chờ của HAL vẫn có thể phát; main chịu trách nhiệm trả lời nội dung. Chỉ Gemini được thêm lời nhắc routing ngắn sau identity và memory trong instructions, tránh lấy các câu xác nhận cũ làm mẫu thay cho thực thi. Chào hỏi vẫn trả lời trực tiếp; câu hỏi về ảnh vẫn dùng `look`. Thay đổi này chỉ tác động chỉ dẫn model, không đổi routing xác định hay deadline fallback. Kiểm chứng model bằng event provider `Function call: delegate_to_main`, không dùng riêng `route=delegated` vì route đó cũng gồm HAL fallback.

Ở Live ON, `reject_turn` được chấp nhận còn đặt trạng thái chặn bền vững trước khi đưa tool tới consumer. Trạng thái này giữ qua các vòng nhận và ACK tool: audio/text của lượt đã bị loại không được biến thành câu trả lời mới không có chủ sở hữu hay kích hoạt fallback sang main. Sự kiện bắt đầu nói mới từ provider hoặc transcript đầu vào không rỗng mới mở lại; terminal và metadata kết thúc transcript rỗng không mở. Reconnect đặt lại trạng thái. Cách này bảo vệ quyền sở hữu lượt độc lập ngôn ngữ câu trả lời; không ngăn backend từ xa tự sinh câu lỗi sau ACK.

HAL xử lý rejection theo quyết định protocol của lượt, không theo danh sách câu lỗi từng ngôn ngữ. LIVE ON chấp nhận `reject_turn` kể cả sau khi đã sinh text: pump bỏ output đang giữ/output tới muộn và history của lượt đó, chỉ hủy TTS realtime đang chạy/chờ đúng owner, không ghi execution thành công. Giữ speech Main và các lượt realtime mới. Barrier rejection Gemini chặn cả speech phát sinh từ ACK cho tới input người dùng mới; LIVE OFF giữ chính sách routing với late rejection hiện hữu. Đã bỏ bảng phrase lỗi provider và phép so prefix ở cả hai đường text-to-TTS; không quyết định rejection bằng cách diễn đạt.

Bằng chứng device (lamp-0c4e, 25/09/2026): `generation_complete` chưa có speech lúc 07:37:17.193, tiếp theo `IN_PROGRESS`, text lỗi lúc 18.281/18.955, rồi `reject_turn` lúc 19.165. HAL đã queue TTS lúc 19.004 và bỏ qua rejection vì output đã bắt đầu. Với LIVE Extended Thinking, output sau terminal rỗng trước interaction status đầu tiên được giữ trong buffer continuation hiện có (2 MB audio / 16k ký tự text). Riêng `IN_PROGRESS` không được mở buffer; chỉ `IDLE` hợp lệ, không reject/delegate/interruption/tool pending/overflow mới được phát. Nhánh cụ thể này chờ IDLE nên tăng thời gian chờ; initial response thông thường vẫn stream. Không thêm classifier, sleep, cấu hình VAD hoặc heuristic ngôn ngữ. Không thể thu hồi âm thanh đã nghe ở nhánh streaming thông thường; reject dừng phần còn lại. Patch không phân loại ngữ nghĩa một câu xin lỗi được sinh ra mà không có rejection/error signal.

Vẫn bỏ marker `<no speech>` và `{pause}` ở đầu trước TTS/history, kể cả prefix chia chunk qua timeout cùng lượt. Reply chỉ có marker không tạo `voice_agent_handled`/Main exchange; giữ phần trích dẫn hoặc giải thích marker. Không xóa history cũ. Test replay đúng protocol đã ghi bằng text nhiều ngôn ngữ, kiểm tra hủy đúng owner trước khi PCM sẵn sàng, output IDLE hợp lệ, initial streaming, lượt mới, interruption và LIVE OFF. Deploy/đối chiếu playback device là bước riêng, không thay bằng kết quả unit test.

ACK tool Gemini lưu tên hàm gốc cùng call ID và trả cả hai trong `FunctionResponse`. Thiếu `name` vi phạm contract provider và đã tái hiện câu báo lỗi hệ thống sau khi `look` chụp ảnh thành công trên Gemini 3.8. Tên được giữ đến khi gửi ACK thành công và xoá khi reset session. Không thay đổi cách gửi ảnh hay replay audio.

Với tool NON_BLOCKING của Gemini, `complete_response` tới trước mọi nội dung trả lời được ACK nhưng không xác nhận hoàn thành và không chặn lời nói đến sau. Vẫn áp dụng kiểm tra câu trả lời/outcome và ưu tiên delegate đến muộn; nếu không có câu trả lời vẫn fallback sang main. Quy tắc này áp dụng cả generation mới sau replay `look`.

Voice được local/Jev xử lý (`handledLocally=true`) giải phóng cue thinking
realtime đang giữ mà không chờ TTS. Phản hồi mute hoặc không có lời nói không
giữ cue vô hạn. Cleanup giữ emotion mới, khôi phục LED đã lưu (kể cả tắt/dim);
TTS/nhạc đang phát giữ overlay tới teardown bình thường. Lượt do agent xử lý
vẫn giữ cue thinking.

### Lifecycle interaction của Gemini Extended Thinking

Với `gemini-3.8-live-extended-thinking`, `serverContent.interactionStatus` là trạng thái lifecycle của provider: kết thúc một đoạn nói khi còn `IN_PROGRESS` không kết thúc interaction. HAL giữ trường này bằng adapter riêng từng session vì google-genai 2.12.1 loại bỏ trường phản hồi chưa biết. Lời nói tiếp theo vẫn stream trong cùng lượt, kể cả sau filler và tool `complete_response` chỉ mang tính gợi ý. Khi `IDLE`, HAL kết thúc lời đáp mà không gọi classifier độc lập nếu không còn tool cục bộ hoặc continuation bị giữ. Output rỗng hoặc công việc cục bộ chưa xong vẫn fallback; terminal này xác nhận kết thúc thực thi, không bảo đảm đúng nội dung. Delegate/reject tường minh và interrupt vẫn được ưu tiên. Interaction bị kẹt có giới hạn im lặng bằng giá trị lớn hơn giữa `REALTIME_TURN_MAX_SILENCE_S` và `REALTIME_RECV_QUEUE_TIMEOUT_S`, gia hạn khi có output/tool, không gia hạn chỉ vì status heartbeat lặp lại. Model/session không có tín hiệu này giữ nhánh grace có giới hạn hiện hữu. Khi session đã báo trạng thái async này, `look` mới trả JPEG trong `FunctionResponse.parts` cùng call ID và tên tool gốc, tiếp tục interaction hiện tại mà không replay audio người dùng. Trong test qua proxy, frame video realtime gửi riêng sau `activityEnd` không được dùng cho câu trả lời ảnh hiện tại; gắn ảnh vào kết quả tool sửa việc liên kết này. Chỉ kết quả đa phương thức này dùng payload WebSocket base64 tường minh vì google-genai 2.12.1 không serialize được bytes lồng trong `send_tool_response`. Kết quả bị bỏ nếu call look đã bị cancel, resolve hoặc xoá khi reset session. ACK tool khác vẫn qua SDK; session không có async status giữ ACK → video → replay. Image input gửi riêng giữ gate pending-tool hiện hữu.

### Log transcription gốc từ Gemini

`[realtime][wire-output]` ghi từng `serverContent.outputTranscription` ngay khi
nhận WebSocket, trước SDK và buffer câu của HAL. Log có ID session cục bộ, số
thứ tự frame nhận (`rx`), status của chính frame, cờ terminal/interrupt,
`finished` và toàn bộ text với ký tự xuống dòng được escape. Đối chiếu chunk với
`speak_queue` để phân biệt text lặp từ upstream hay lặp trong HAL. Đây là output
của Autonomous proxy; riêng log này chưa phân biệt model sinh lặp với proxy
replay. Không ghi payload audio/ảnh hoặc credential. Routing và playback không đổi.

Tất cả prompt realtime (mặc định, OpenAI Realtime, Gemini, Pipecat và cả hai
tầng GPT-Live) chuyển về main khi người dùng nói rõ với thiết bị rằng họ đau đầu,
mệt, chóng mặt, bí bách hoặc khó tập trung, kể cả không có câu hỏi/động từ hành
động và có “okay” ở cuối. Câu hỏi về không khí/CO₂/nhiệt độ/độ ẩm/thông gió
của phòng hiện tại và follow-up về khó chịu (“Could it be because the room is
too airtight?”) cũng thuộc skill wellbeing/environment của main. System prompt của từng provider,
lời nhắc routing Gemini sau memory và mô tả tool delegate dùng chung nêu rõ ngoại lệ
này: không thay handoff bằng lời khuyên chung, giải thích nguyên nhân, hứa giảm
triệu chứng, disclaimer, search hay `complete_response`. Báo triệu chứng khẩn
cấp được chuyển ngay; main quyết định có cần kiểm tra sensor hay không. Câu hỏi
kiến thức không liên quan vẫn trả lời trực tiếp; vẫn kiểm tra lời nói có hướng
tới thiết bị và nghe rõ hay không. Đây là hướng dẫn model, không phải bộ phân
loại triệu chứng xác định. Voice GPT-Live dùng handoff backend native; backend
gọi tiếp `delegate_to_main` thay vì tự trả lời hoặc search. Cả hai tầng không
phát lời xác nhận cho handoff wellbeing. Thay đổi đi cùng HAL; chỉ upload skills không cập
nhật prompt realtime.

Gợi ý ngôn ngữ nhận dạng đầu vào Gemini được bật bằng flag có sẵn `HAL_GEMINI_USE_LANGUAGE_CODES=true`. Với google-genai 2.12.1 đang pin, HAL gửi `input_audio_transcription.language_hints.language_codes`, không dùng `language_codes` cấp trên vốn không được SDK hỗ trợ cho Developer API. Hint lấy từ `stt_language` (`vi` thành `vi-VN`); ngôn ngữ rỗng hoặc tắt flag vẫn tự nhận dạng. Transcript đầu ra không có hint. Đây là gợi ý nhận dạng, không khóa ngôn ngữ. Hai profile `pro-respeaker-lite` và `pro-xvf3800` bật flag này; các profile khác giữ mặc định tắt. Trên device Lite chạy 3.8 extended-thinking, provider đã chấp nhận hint và log ghi đúng các yêu cầu giá vàng, thời tiết và dừng lại trong lượt test người dùng; chưa có phép đo độ chính xác tổng quát hoặc xác nhận trên XVF3800.
