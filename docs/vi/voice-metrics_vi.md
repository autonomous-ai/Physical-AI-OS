# Đo phản hồi giọng nói (voice metrics)

Đây là một tracker chạy trên ống telemetry của thiết bị — xem
[`docs/vi/telemetry_vi.md`](telemetry_vi.md) để biết về chính cái ống đó, công
tắc bật/tắt, và các luật mà mọi tracker phải theo.

Chỉ **đo**, không đổi hành vi. Không có gì ở đây thay đổi việc thiết bị nói
gì, nói lúc nào hay im lúc nào — nó ghi lại những gì đã xảy ra để tính được
ba con số từ thiết bị thật:

| KPI | Câu hỏi | Mục tiêu |
|-----|---------|----------|
| **KPI-1** | Tỉ lệ lượt thoại đủ điều kiện được phản hồi trong **3 giây** kể từ lúc phát hiện người dùng nói xong | ≥ 95 % |
| **KPI-2** | Tỉ lệ tình huống suppression mà một câu trả lời **cũ** thực sự bị phát ra | < 5 % |
| **KPI-3** | Tỉ lệ lượt tác vụ thoại đủ điều kiện đã chạy xong | ≥ 85 % |

Mục tiêu KPI-1 và KPI-2 đều **tạm thời**. Mọi khoảng thời gian thô đều được lưu, nên
đổi ngưỡng là việc tính lại từ dữ liệu, không phải đo lại thiết bị.

## Code nằm ở đâu

| Lớp | Đường dẫn | Vai trò |
|-----|-----------|---------|
| Tracker ở HAL | `hal/telemetry/voice_metrics.py` | Interaction id, ack/audio cũ, cohort tác vụ và bằng chứng realtime chạy xong |
| Bằng chứng thực thi ở OS | `system/telemetry/voice_task.go` | Ghi nhận tác vụ thoại nhận vào, bind run, biên lifecycle agent và local intent trả về. |
| Reporter offline | `scripts/report_voice_task_metrics.py` | Ghép journal/AA đã lưu và báo cáo KPI-3, không gọi mạng. |
| Ống dẫn ở HAL | `hal/telemetry/client.py` | Dùng chung: log local, hàng đợi có giới hạn, POST nền |
| Nhận ở OS | `system/server/telemetry/delivery/http/handler.go` | `POST /api/telemetry/event` (loopback/LAN, cùng cổng chặn với `/api/sensing/event`) |
| Ống dẫn ở OS | `system/telemetry/telemetry.go` | Field chung, chống trùng, hàng đợi giới hạn, một sender, log local |
| Transport | `system/lib/analytics` | Client Autonomous Analytics `event_tracking` (chung với web/mobile) |

Code voice chỉ bị đụng đúng ở các biên nó vốn sở hữu: điểm kết thúc câu nói,
dispatch turn, playback bắt đầu/kết thúc, và cử chỉ huỷ. Luật, ngưỡng và hình
dạng event đều nằm trong hai package tracking.

## Interaction id

Một interaction = **một lượt người dùng nói**. Trên đường turn,
`voice_metrics.speech_end()` tạo
`interaction_id` (`vi-<16 hex>`) đúng lúc HAL kết luận người dùng đã nói xong,
rồi id đó đi xuyên realtime, POST sensing (gắn với `runId` os-server trả về từ
`/api/sensing/event`), câu trả lời của agent chính (qua `turn_id` của TTS
queue), filler và playback.

`voice_agent_handled` là **thông báo backend về một interaction đã được trả
lời**, không phải interaction mới: nó gắn vào cùng `interaction_id`, nên lượt
do realtime xử lý chỉ sinh đúng một mẫu KPI-1.

Mỗi **đoạn xếp hàng** của câu trả lời stream là một lần phát được đo riêng: lúc
drain, hook được arm lại theo owner của chính đoạn đó, vì nó phát trên stream do
turn nói trước mở ra.

**Chủ sở hữu là tường minh, không đoán.** Mỗi lần phát mang theo owner đã giành
loa: `run:<turn_id>` cho câu trả lời của agent hoặc filler được arm cho turn đó
(os-server truyền run id xuống cùng filler, và truyền ngược qua field `owner`
của `/api/sensing/filler` cho filler chờ của realtime), và `interaction:<id>`
cho giọng native realtime. Nhánh realtime trả lời bằng TTS (không phải audio
native) cũng gắn tag y hệt — lúc đó os-server chưa cấp run id nào, nên chính
interaction id là tag. Audio không ai nhận là `unknown` và **không bao giờ được tính
là đã phản hồi** — đoán "interaction mở mới nhất" chính là cách một filler cũ bị
tính thành phản hồi cho câu lệnh mới. Số đếm đi kèm mọi dòng interaction ở
`unknown_owner_playbacks`.

Ack schema 2 không loại interaction chỉ vì loa mute: tác vụ im lặng đã được
nhận xử lý vẫn có thể có ack. Mỗi đoạn trong hàng đợi mang metadata phân loại
riêng (câu trả lời/filler/hệ thống), không kế thừa loại của speech mở stream.
Snapshot này không thay đổi hành vi feedback hay ngắt phát.

## Độ phủ của phiên live

Phiên Gemini live dùng `hal/telemetry/live_voice.py` để ánh xạ mỗi user-turn ID
quan sát được của provider thành một
interaction HAL, độc lập với timeout của vòng nhận. Snapshot interaction có
`mode` (`live` hoặc `turn`) và `speech_endpoint_known` để tách độ phủ theo đường
xử lý. Delegate mang nguyên interaction ID qua OS; provider từ chối tiếng không
phải của người dùng thì task đó bị loại.

Gemini `ACTIVITY_END` hiện chuyển **thời điểm nhận** monotonic của HAL cho
voice cue hiện hữu. Mốc này mang nhãn `server_vad_receive`, **không** là endpoint
KPI-1: độ trễ mạng và thời gian server xác nhận im lặng sẽ làm latency đo được
ngắn giả. Không đổi cue hoặc VAD. Chỉ có transcript/thời điểm nhận thì giữ
`exclusion_reason=speech_endpoint_unavailable`, latency ack/answer null; eligibility
KPI-3 độc lập. Endpoint thật tới muộn được amendment nếu timestamp trước hoặc
bằng ack đầu tiên thuộc lượt đó. HAL giữ timestamp nhận xử lý và audio/answer
độc lập, kể cả filler đúng owner. Timestamp sai, NaN/Inf, tương lai hoặc sau ack không
được đổi thành latency 0.

Converter google-genai 2.12.1 giữ `voiceActivity` và `audioOffset`; test đi qua
SDK thật xác minh điều này. [SDK contract](https://googleapis.github.io/python-genai/genai.html#genai.types.VoiceActivity)
định nghĩa offset trên luồng input audio, không phải đồng hồ monotonic của HAL.
HAL chưa ánh xạ frame capture vào timeline này: cần xét preroll, prefix phát lại,
frame không gửi và reconnect. Ghi offset ra log chưa đủ để tính KPI.
[Live API reference](https://ai.google.dev/api/live) không bảo đảm thứ tự input
transcription. Không bật `explicit_vad_signal`: đây không phải cấu hình được tài
liệu xác nhận để xin server gửi endpoint. [Issue SDK đang mở](https://github.com/googleapis/python-genai/issues/2981)
báo 3.8 thiếu activity event, nhưng không phải bằng chứng cho device của chúng ta
hay kết luận nguyên nhân đã được Google xác nhận.

`[endpoint-wire]` ghi type/offset trước và sau SDK; `[endpoint-coverage]` ghi bộ
đếm cộng dồn theo socket, kể cả khi không có activity và có trường legacy.
Không ghi audio hay thêm nội dung transcript. `[voice-metrics] live ownership`
liên kết provider key với interaction ID để nối log ack sau playback thật.
Các trace giúp phân biệt server không gửi với SDK làm mất tín hiệu; không bật
manual VAD, đổi mic upload, wake gating, cancel, delegation hay LIVE OFF.

**Độ phủ chưa giải quyết:** cohort lamp-0c4e được cung cấp ngày 24/09/2026,
14:15–15:00 có 87 interaction sau dedup: 44 thiếu endpoint, 13 bị ngắt lời,
30 noise/rejected/no-transcript; không có mẫu đủ điều kiện. KPI-1 là N/A.
Patch không tạo endpoint giả cho 44 lượt này. Detector tiếng nói cục bộ chỉ quan
sát là phương án khác, nhưng cần hiệu chuẩn trên capture có timestamp và ghi rõ
là ước lượng. RMS/duck hiện tại không phải speech classifier, không phân biệt
người đang nói với thiết bị trong phòng ồn. Không thay bằng transcript arrival,
STT close, TTS start hay last-audio-sent.

Kiểm tra chỉ đọc trên device lúc 15:19 xác nhận google-genai 2.12.1 ánh xạ wire
`voiceActivity.type`/`audioOffset` sang SDK `voice_activity_type`/`audio_offset`.
Handler đang deploy bỏ qua offset, lấy `time.monotonic()` khi nhận END.
Server log 14:15–15:00 có 18 dòng ack, tất cả `latency_ms=None`; 77 dòng tạo
interaction bằng `provider_transcript`, 3 bằng `provider_delegate`. Đây là số
dòng log thô, không phải cohort 87 dòng sau dedup ở trên. Log cũ chưa trace raw
activity nên không chứng minh được server đã không gửi END.

Sau khi được phép deploy, thu bộ đếm wire/SDK và interaction của câu nói xác định;
đối chiếu first write/cancel theo cùng owner. Kiểm tra im lặng/ồn, filler hợp lệ,
barge-in, suppress trước write, output cũ tới muộn, delegate và LIVE OFF. Báo độ
phủ thiếu endpoint cùng `no_ack` đủ điều kiện và latency. Test local không chứng
minh độ phủ hay độ chính xác âm học trên device. Mốc audio vẫn là first successful
stream write; phần đệm ALSA/loa cần acoustic loopback để đo. Timer báo cáo
10 giây hiện bắt đầu khi tạo interaction (có thể lúc nhận transcript); amendment
có thể tới sau, nên không bảo đảm cửa sổ 10 giây sau kết thúc tiếng nói âm học.

Realtime chỉ ghi completed khi nhận terminal thành công của provider gắn đúng
interaction. Timeout khi nhận, tín hiệu done tự tạo và ngắt lời không phải bằng
chứng hoàn tất; không có terminal thành công thì task vẫn là incomplete.
Gemini Extended Thinking dùng `interactionStatus=IDLE` cùng text trả lời được
chấp nhận, không còn tool cục bộ hoặc continuation bị giữ làm bằng chứng thực
thi. Không cần verdict từ LLM thứ hai sau terminal này. Output rỗng và công việc
cục bộ chưa xong vẫn fallback. Session thiếu status giữ xác nhận/kiểm tra outcome
và grace có giới hạn hiện hữu. Provider kết thúc hoặc classifier xác nhận đều
không bảo đảm đúng nội dung. Playback KPI-1 và mọi ngưỡng KPI giữ nguyên.
Ngắt lời thật từ provider tạo biên suppression `server_barge_in`
chỉ áp dụng cho interaction bị huỷ, giữ grace **2 giây** và cửa sổ quan sát
**60 giây** hiện có. Audio không có owner không được tính là acknowledgement.
Câu hỏi tiếp theo bắt đầu khi execution trước đã completed, audio cũ đã im và
không còn phần tổng hợp/hàng đợi thuộc lượt đó thì không tạo mẫu suppression.
Speech còn trong hàng đợi vẫn tính trong khoảng nghỉ giữa các câu. Việc chặn
audio không phủ định terminal execution thành công gắn đúng lượt, kể cả khi
terminal tới sau ngắt lời.

Khi đóng phiên, `voice_metrics_live_coverage` ghi `observed_interactions`,
`completed_interactions` cùng các bộ đếm có phát sinh:
`unkeyed_user_observations`,
`unowned_output_chunks`, `unowned_completions`,
`unowned_interruptions`, `endpoint_receive_only_observations`.
Đọc chúng cùng kết quả KPI: luồng transcript Gemini không có input ID ổn định,
nên output tới muộn hoặc không có owner không được gán ngược cho câu nói mới nhất.
Filler look LIVE mang binding đúng provider turn, được giữ suốt thao tác aim; owner thiếu/mơ hồ giữ unclaimed. Thiếu endpoint là mất độ phủ, không phải pass hay fail KPI-1. Các hook này giữ
nguyên định nghĩa đường turn, bộ lọc tiếng ồn và cờ routing.
Metadata ngắt lời không thêm lệnh dừng playback, bỏ text trong bộ đệm, mở lại
audio native hay đổi lịch xử lý turn của provider. Lỗi đo lường được bắt để
không làm gián đoạn speech hoặc delegation. Khi các đoạn trong hàng đợi dùng
chung stream, lần ghi đầu của đoạn mới kết thúc khoảng đo của đoạn trước;
nó không đóng audio stream vật lý.

## Thế nào là "đã phản hồi"

**Ack schema 2** lấy xác nhận sớm nhất sau endpoint lời người dùng: audio đúng
owner thực sự được ghi, OS xác nhận đã nhận handoff sang main/harness (`run_id`
kèm `delivered`), hoặc kết quả đã xử lý local được xác nhận. Chỉ chọn route
delegate, gọi tool, bắt đầu POST hay nhận HTTP 200 chung chung chưa chứng minh
đã nhận xử lý. History-sync không được tính là ack nhận xử lý. Tác vụ main có
`NO_REPLY` hợp lệ vẫn có ack mà không cần phát tiếng hoặc hoàn tất execution.

Nhận xử lý dùng `ack_kind=delegate_accepted` / `local_accepted` và
`ack_modality=processing_accepted`. Filler/câu trả lời đúng owner phát sớm hơn
vẫn giữ ack sớm nhất. `audio_latency_ms`/`audio_kind` đo riêng lần ghi audio đúng
owner đầu tiên; `answer_latency_ms`/`answer_kind` vẫn cần câu trả lời thực sự.
Hook đo không đổi routing, playback, cancel hay thời điểm main chạy tác vụ.

Với audio, dùng frame đầu tiên **thực sự ghi vào audio stream** qua
`TTSService._note_audio_written` ở các đường synth streaming, drain queue, WAV
cached và native realtime. `on_speak_start` và HTTP nhận TTS không chứng minh
đã phát: queue có thể drop hoặc mute trước khi ghi. Đây khác với xác nhận đã
nhận tác vụ nêu trên.

**Version 1 chỉ đo audio.** Thiếu `ack_schema_version` nghĩa là 1. Không được
đổi nghĩa `no_ack` lịch sử thành version 2 khi thiếu bằng chứng nhận xử lý;
phải báo riêng từng version, kể cả khác biệt eligibility khi mute.

Ngay cả lần ghi đầu **cũng không phải thời điểm âm thanh ra loa**: đệm ALSA (và
Bluetooth còn hơn) nằm sau đó. Phép đo là "audio đã được giao cho thiết bị",
không phải "âm đã rời driver".

| Loại playback | Modality ghi lại | Cách phân loại |
|---------------|------------------|----------------|
| `native_realtime` | `spoken_answer_realtime` | ghi từ đường native frame — giọng của chính model realtime |
| `realtime_tts` | `spoken_answer_realtime` | câu trả lời dạng text của realtime được tổng hợp qua TTS, nhận diện bằng metadata playback độc lập với `realtime_feedback` |
| `agent_reply` | `spoken_answer` | `realtime_feedback` (chỉ câu trả lời của agent bật cờ này) |
| `waiting_audio` | `waiting_audio` | speech cached interruptible, tức dead-air filler |
| `system_audio` | `acknowledgement_audio` | các câu cached/hệ thống khác |
| `unknown` | — (không bao giờ là ack) | không ai nhận sở hữu lần phát này |

Tiếng chime xác nhận cử chỉ vật lý không phải phản hồi cho lệnh thoại.
Các lần ghi chime bỏ qua hook đo speech: không kế thừa owner của câu trả lời
đang chờ và không dùng mất hook ghi frame đầu tiên của câu trả lời đó.

**Waiting audio vẫn được tính là ack.** Filler có thể xác nhận trước handoff
hoặc câu trả lời, nhưng handoff đã được xác nhận không cần filler mới có ack.
Tách theo `ack_kind` và `ack_modality`: nhận xử lý không chứng minh người dùng
đã nghe gì. Báo audio latency và answer latency bên cạnh KPI-1 để giữ rõ khác
biệt UX này.

**Phản hồi bằng hình ảnh KHÔNG được tính.** LED listening đúng là một tín hiệu
người dùng cảm nhận được, nhưng thời điểm nó thật sự bật chưa được đo ở đây;
tính vào mà không đo là thổi phồng KPI-1. Muốn thêm thì phải đo thật
`_set_emotion_local(EMO_LISTENING)` và ghi `ack_modality = "visual_cue"` tường minh.

## Đồng hồ

Mọi khoảng thời gian đều là hiệu hai lần đọc `time.monotonic()` **trong cùng
tiến trình HAL**. Không bao giờ trừ clock HAL cho clock os-server. `event_timestamp`
(giây) của analytics client không dùng để đo latency — mọi khoảng thời gian là
field mili-giây riêng.

`speech_end_method` ghi cách xác định điểm kết thúc, vì đó là **phát hiện**,
không phải sự thật âm học: `silence_clock`, `tts_started`, `music_started`,
`max_duration`, `stt_error`. Đồng hồ chạy từ chính lúc phát hiện đó (lúc vòng
lặp capture break), **không phải** sau `finalize_session` — ráp transcript, cắt
đuôi im lặng và nhận diện người nói chạy ở giữa, tính vào là đổ oan cho thời
gian phản hồi của thiết bị.

## Event

### `voice_metrics_interaction` — snapshot lượt nói (KPI-1 và KPI-3)

| Field | Ý nghĩa |
|-------|---------|
| `interaction_id`, `run_id`, `event_type`, `route` | Định danh + turn đi đường nào (`handled` / `delegated` / fallback / noise-dropped) |
| `mode`, `speech_endpoint_known` | Đường `live`/`turn` và có quan sát được endpoint hay không |
| `speech_end_method` | Cách phát hiện điểm kết thúc |
| `eligible` | `false` khi có `exclusion_reason` |
| `outcome` | `acknowledged` \| `no_ack` \| `excluded` |
| `exclusion_reason` | `rejected_noise`, `rejected_non_user`, `no_transcript`, `not_addressed`, `interrupted_by_user`, `speech_endpoint_unavailable` (chỉ KPI-1). Schema 2 không loại chỉ vì mute; schema 1 lịch sử còn dùng `speaker_muted`. |
| `failure_reason` | `dispatch_failed`: dispatch thất bại vẫn eligible; nếu không có ack hợp lệ khác thì là `no_ack`. Kết quả OS xử lý local được xác nhận có `local_accepted`. |
| `ack_schema_version` | `2`: audio đúng owner hoặc xác nhận nhận xử lý; thiếu field nghĩa là `1` chỉ đo audio. |
| `ack_latency_ms` | Từ endpoint tới ack được xác nhận sớm nhất; null khi chưa có bằng chứng ack hoặc thiếu endpoint hợp lệ. |
| `ack_modality`, `ack_kind` | Loại audio đúng owner, hoặc `processing_accepted` với `delegate_accepted` / `local_accepted`. |
| `audio_latency_ms`, `audio_kind` | Đo độc lập từ endpoint tới lần ghi audio đúng owner đầu tiên và loại audio; null nếu chưa phát hoặc thiếu endpoint hợp lệ. |
| `answer_latency_ms`, `answer_kind` | Lúc nghe được **câu trả lời** (`agent_reply` / `native_realtime` / `realtime_tts`), khác với biên nhận. `null` = thiếu endpoint hoặc chưa ghi nhận câu trả lời trong cửa sổ — đó là phát hiện, không phải thiếu dữ liệu |
| `ack_deadline_ms`, `observe_window_ms` | 3000 / 10000 — ngưỡng (tạm thời) đang áp dụng lúc ghi dòng đó |
| `unknown_owner_playbacks` | Số lần phát không ai nhận — audio bị loại khỏi quyết định ack |
| `amends_event_id`, `amendment_reason` | Có ở dòng **đính chính**: route hoặc exclusion tới sau khi verdict đã gửi |

Cửa sổ quan sát **10 giây**, rộng hơn mục tiêu 3 giây một cách có chủ đích: câu
trả lời muộn được ghi kèm latency thật thay vì gộp thành "không trả lời", nên
sau này đổi ngưỡng vẫn tính lại được từ dữ liệu đã lưu.

**Ghi verdict KHÔNG có nghĩa turn đã kết thúc.** Dòng KPI-1 được ghi ở giây thứ
10; agent chính có thể vẫn đang chạy, và lệnh dừng bấm ở giây 12 vẫn phải tìm
thấy turn đó để suppress. Một turn còn *active* cho tới khi im lặng đủ
`TURN_ACTIVE_TTL_MS` (45 giây) — đồng hồ này được reset mỗi lần turn đó phát ra
tiếng, và không bao giờ hết hạn khi audio của turn đó vẫn đang phát — hoặc cho
tới khi bị loại/bị lỗi. Chỉ khi đó nó mới rời mẫu số KPI-2.
Verdict sai sau đó được sửa bằng dòng đính chính.

### `voice_metrics_suppression` — mỗi biên một event (KPI-2)

| Field | Ý nghĩa |
|-------|---------|
| `suppression_reason` | `explicit_stop` (user click) hoặc `auto_supersede` (realtime đã trả lời câu mới hơn), hoặc `server_barge_in` (provider ngắt interaction được xác định) |
| `interaction_id` | Lượt nói kích hoạt biên này |
| `applicable_interactions` | Có bao nhiêu interaction cũ đang bay bị biên này phủ |
| `old_audio_playing_at_boundary` | Lúc đóng mốc có đang phát audio cũ không |
| `stale_observed` | **Tử số KPI-2** |
| `stale_kind`, `stale_started_after_ms` | Audio loại gì, bắt đầu sau biên bao lâu (audio đang phát sẵn cũng tính) |
| `stale_audible_past_grace_ms` | Audio cũ còn nghe được bao lâu sau khi hết grace |
| `stop_to_silence_ms` | Audio cũ còn kêu bao lâu sau biên — ghi thô, kể cả khi nằm trong grace |
| `grace_ms`, `observe_window_ms` | 2000 / 60000 |
| `observation_complete` | `false` khi cửa sổ đóng lúc turn bị suppress vẫn còn nói được — dòng đó **không** phải bằng chứng suppression sạch |
| `unobserved_interactions` | Còn bao nhiêu turn bị suppress vẫn sống lúc đó |

**Stale = còn nghe được sau grace, dù bắt đầu lúc đó hay chỉ kéo dài sang.**
Mỗi khoảng phát được chấm với mọi biên đang mở, nên audio bắt đầu TRƯỚC biên mà
chạy quá grace vẫn bị tính — bản trước chỉ nhìn lần phát *bắt đầu* sau biên nên
cho qua một câu trả lời vẫn còn nói 3 giây sau lệnh dừng.

**Cửa sổ dài, và quan sát dở dang thì phải nói ra.** Câu trả lời của agent chính
có thể tới hàng chục giây sau biên, nên mỗi suppression được theo dõi 60 giây.
Nếu hết cửa sổ mà turn bị suppress vẫn nói được, dòng đó ghi
`observation_complete = false` thay vì "không có stale".
Nếu đã quan sát thấy audio stale thì vẫn là lỗi đã xác nhận, dù quan sát chưa
hoàn tất; quan sát thêm không thể xoá lần phát đó.

**Grace được ghi rõ nguồn, không bịa cho KPI đẹp.** `TTSService.stop()` set stop
event và đánh thức drain queue; worker vẫn phải nhả lock. Comment trong
`hal/drivers/voice/tts/service.py` ghi lại ca xấu đo trên lamp-0c89 mất tới ~4 s
(bình thường dưới 1 s) — chính là lý do bản vá đánh-thức-queue tồn tại.
`STALE_GRACE_MS = 2000` là lựa chọn báo cáo; `stop_to_silence_ms` được lưu thô ở
mọi biên nên lựa chọn đó luôn kiểm tra được và đổi được mà không phải đo lại thiết bị.

Cả hai biên đều đóng mốc **ở HAL**, nơi cả hai phát sinh: cử chỉ huỷ là button
action của HAL, còn `voice_agent_handled` do chính turn dispatcher của HAL gửi đi.

**Biên explicit stop còn được HAL cưỡng chế, không chỉ đo.** Watermark click
của os-server chỉ tắt được reply mà nó gán được vào run bị huỷ, nhưng có hai
đường tới loa không qua kiểm tra đó (đo trên thiết bị 17/9/2026,
`stale_started_after_ms` 12–28 s): kết quả Harness nói thẳng qua
`hal.SpeakReply`, và filler chờ realtime do HAL tự hẹn giờ. Nay biên đánh dấu
mọi interaction nó bao phủ là `suppressed`, và `TTSService` từ chối audio thuộc
turn đó ở mọi cửa vào (`speak`, `speak_queue`, `speak_cached`,
`native_play_begin`) qua `voice_metrics.is_suppressed(owner)`. Audio không có
chủ không bao giờ bị từ chối. Đường Harness thêm vào còn đi qua `deliverTTS`
như mọi reply khác. Auto supersede vẫn chỉ đo; cưỡng chế nó là watermark của
os-server.

**Chỉ ghi nhận biên khi os-server THẬT SỰ áp dụng.** Response của
`/api/sensing/event` cho post `voice_agent_handled` mang theo `speechSuppressed`
— chính câu trả lời của os-server cho "tôi có lấy loa khỏi turn cũ không". Khi
`OS_REALTIME_SUPERSEDES_MAIN_REPLY` tắt (mặc định) thì không có gì bị suppress,
POST lỗi cũng vậy; cả hai đều không được ghi, vì mẫu số đầy những tình huống
chưa từng suppress gì sẽ làm KPI-2 đẹp miễn phí.

**Mẫu số chỉ đếm turn còn active.** Interaction đã bị loại hoặc đã đóng thì
không thể phát ra câu trả lời cũ, nên không tính là thứ mà biên phải suppress.

## Định nghĩa KPI

**KPI-1 — phản hồi trong 3 giây**

- Mẫu số: verdict cuối của từng `(device, interaction_id, ack_schema_version)`
  có `eligible = true`, ưu tiên `task_revision` lớn nhất rồi amendment/thời gian.
  `no_ack` hợp lệ vẫn nằm trong mẫu số. Báo riêng từng schema version.
- Tử số: trong đó `outcome = 'acknowledged'` và `ack_latency_ms <= 3000`.
- Loại trừ (vẫn báo cáo, không bao giờ vứt): turn bị noise guard loại, turn
  model từ chối vì không phải người dùng, transcript rỗng, câu không nói với
  thiết bị (không wake word / ngoài cửa sổ follow-up), bị user ngắt và thiếu
  endpoint thật. Schema 2 không loại chỉ vì loa mute.
- **Lỗi thì GIỮ LẠI.** Lệnh hợp lệ mà thiết bị không phục vụ được (POST sang
  os-server không tới — `failure_reason = 'dispatch_failed'`) vẫn *eligible* và
  tính là `no_ack`. Loại nó ra là thổi phồng tỉ lệ thành công bằng đúng những
  ca người dùng thấy tệ nhất. Lượt chậm cũng vẫn nằm trong mẫu số.

**KPI-2 — câu trả lời cũ thực sự bị phát**

- Mẫu số: các dòng `voice_metrics_suppression` có `applicable_interactions > 0`
  **và** (`observation_complete = true` **hoặc** `stale_observed = true`). Lần
  phát stale đã xác nhận vẫn nằm trong cả tử số và mẫu số, kể cả khi turn bị
  suppress còn active lúc cửa sổ quan sát đóng. Chỉ dòng quan sát dở dang có
  `stale_observed = false` mới báo riêng như coverage loss — không được gộp
  vào phần "đạt".
- Tử số: trong đó `stale_observed = true`.
- Báo cáo `explicit_stop` và `auto_supersede` **riêng**: hai chính sách khác
  nhau. Supersede tự động chỉ xảy ra khi os-server bật
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1`. **Mặc định trong code là TẮT và thay
  đổi này không đụng tới default** — nhưng phải đọc `.env` của chính body trước
  khi kết luận: lamp xuất xưởng đã **BẬT** sẵn
  (`robots/lamp/rootfs/opt/hal/.env`), nên lamp có sinh mẫu `auto_supersede`.
  Body nào tắt thì mẫu số rỗng và lát cắt đó là **N/A**, không phải 100 %.
- Suppression đúng (`observation_complete = true`, `stale_observed = false`) là **đạt**, không phải "mất câu
  trả lời". Hành động phần cứng của turn bị auto-supersede vẫn hợp lệ theo
  thiết kế; chỉ speech và filler bị bỏ.
- Không có mẫu đủ điều kiện ⇒ **N/A**. Không bao giờ báo 0 % hay 100 % từ tập rỗng.

## KPI-3: chạy xong, không đánh giá làm đúng

Chat và sensing có cohort hoàn tất riêng; xem [runbook query AA chat/sensing](task-metrics_vi.md). Trang này chấm voice (`--group voice`, mặc định của reporter). Event dùng chung `voice_metrics_task_execution` còn chứa terminal ngoài voice; chỉ evidence join với cohort voice mới được tính ở đây. Report có `completion_pct` là alias của `kpi3_pct` và field `group`.

“Successfully complete” nghĩa là **tác vụ đã chạy xong, không ghi nhận lỗi kết thúc thực thi**. Agent vẫn có thể trả
lời sai hoặc làm hành động không hiệu quả mà đạt chỉ số thực thi này. KPI không
đánh giá đúng yêu cầu user, kết quả tool, tác động vật lý hay phát hết câu trả lời.

Cohort là hợp của interaction tác vụ HAL và tác vụ thoại OS nhận vào. OS phát
`voice_metrics_task_started` trước routing hoặc thực thi local cho request
`voice`, `voice_command`, `voice_followup`. Bao gồm delegate xuống main agent
và đường thoại không có snapshot task HAL. Run backend bất kỳ và thông báo
memory-sync `voice_agent_handled` không tạo lượt mới trong cohort.

`voice_metrics_task_started` chứa `schema_version=1`, `interaction_id`,
`run_id`, `event_type`, `task_started_at_ms` (Unix milliseconds). OS giữ id HAL
nếu có, nếu thiếu thì tạo `os-voice-<random>`. Event nhận request có run id rỗng;
event bind tới sau giữ cùng interaction id, thêm run id local/main, kể cả khi
dispatch chưa sẵn sàng. Mỗi lần phát có timestamp riêng; reporter lấy start
sớm nhất. Đây là cập nhật một lượt, không phải nhiều lượt. Request `voice` phải
chờ trong queue được gán run id cố định và bind trước enqueue để replay vẫn
ghép đúng. Lượt này ở mẫu số dưới dạng chưa xong cho tới khi có evidence
lifecycle; nhận vào queue chưa phải hoàn tất.

Gộp HAL và OS start bằng **thiết bị + interaction_id hoặc thiết bị + run_id khác
rỗng**, kể cả alias, trước khi đếm. Tác vụ thoại OS nhận vào làm row HAL legacy
hoặc excluded tương ứng trở thành eligible, nhưng không ghi đè quy tắc completion
`realtime_handled`. Thiếu snapshot HAL không được làm mất tác vụ OS khỏi mẫu số.
`voice_metrics_interaction` giữ các field độc lập:

| Field | Ý nghĩa |
|-------|---------|
| `task_schema_version` | `1`; dòng HAL cũ không ghép được OS task start được báo coverage, không tính là thất bại |
| `task_started_at_ms` | Unix milliseconds lúc phát hiện nói xong, hoặc lúc đầu quan sát được lượt người dùng nếu thiếu endpoint; chọn cohort, không dùng tính latency giữa hai process |
| `task_revision` | Revision snapshot tăng dần; lấy lớn nhất theo thiết bị + interaction, kể cả bind run-id muộn |
| `task_eligible` | Độc lập với eligibility ack: mute hoặc ngắt speech không loại tác vụ thực thi |
| `task_eligibility_known` | Đã biết routing/exclusion chưa; báo riêng coverage chưa rõ |
| `task_exclusion_reason` | Loại noise, non-user, transcript rỗng hoặc không hướng tới thiết bị; dispatch lỗi vẫn eligible |

`voice_metrics_task_execution` chứa `schema_version=1`, `run_id`,
`interaction_id`, `outcome` (`completed`, `failed`, `unknown`, hoặc `cancelled` của Harness), `evidence`,
`execution_at_ms` (Unix milliseconds), và boolean `error`. Không chứa nội dung
lỗi, transcript hay kết quả tool.

| Evidence | Outcome và ý nghĩa |
|----------|-------------------|
| `lifecycle_end` | `completed`: runtime xác nhận kết thúc, không có `data.aborted=true` hay `data.error` khác rỗng |
| `lifecycle_end_error` | `failed`: end có aborted hoặc error vẫn là lỗi; không tự suy diễn từ `stopReason` |
| `lifecycle_error`, `lifecycle_end_error`, `chat_error`, `local_intent_error`, `dispatch_error` | `failed`: lỗi thực thi, dispatch lỗi/chưa sẵn sàng hoặc local intent ghi nhận lỗi API hành động HAL |
| `lifecycle_error_recovered` | `unknown`: recover chưa chứng minh chạy xong; cần end tường minh |
| `local_intent_returned` | `completed`: handler local intent chạy xong, không ghi nhận lỗi hành động HAL; không chứng minh đúng ý user hay tác động vật lý |
| `chat_final_no_lifecycle` | `completed`: final có nội dung tiêu thụ pending trace mà không có lifecycle (ví dụ OpenClaw `/status`, `/new`); final rỗng không chứng minh completion |
| `execution_observation_lost` | `unknown`: runtime mất quan sát tác vụ đã gửi nhưng chưa kết thúc khi mất transport hoặc timeout; chưa chứng minh thực thi thất bại |
| `harness_delegated` | `unknown`: thực thi chuyển sang Harness; lifecycle end của runtime cục bộ không chứng minh tác vụ remote xong |
| `harness_turn_summary` | `completed`: summary cuối legacy được ghép an toàn, có nội dung; riêng `turn.done` không hoàn tất task |
| `harness_correlated_summary` | `completed`, `failed` hoặc `cancelled`: membership summary đã kiểm chứng và áp dụng atomic một lần; giữ outcome chính xác, độc lập với TTS |
| `harness_turn_error` | `failed`: `turn.error` hoặc `agent.error` Harness đã ghép đúng, kể cả thiếu text |
| `harness_question_open` | `unknown`: đang chờ trả lời câu hỏi, gồm thu thập câu trả lời từng phần cục bộ |
| `realtime_turn_done` | `completed`: terminal thành công của provider gắn đúng lượt hoàn tất turn đã handled; Gemini Extended Thinking dùng `IDLE` cùng text trả lời được chấp nhận và không còn công việc cục bộ chưa xong; session thiếu status giữ xác nhận/kiểm tra outcome, fallback không tính hoàn thành |

Đường `turn.summary` có correlation chỉ phát `harness_correlated_summary` cho result
mới lưu và đúng các run thành viên; replay không phát lại. `cancelled` giữ riêng và
có `error=true`, tuyệt đối không đổi thành thành công. Receipt và `turn.done` chỉ
là lifecycle, không chứng minh kết quả hoàn tất. Summary đơn thiếu metadata vẫn
qua bộ đối chiếu legacy an toàn; summary mơ hồ không được tính thành công.
Reporter nhận evidence này trong cohort Harness và đếm riêng `cancelled_turns`,
vẫn giữ trong mẫu số eligible nhưng không tính completed hay failed. Cancellation
là terminal: mất quan sát transport hay completion cleanup đến sau không xóa nó.
Giữ nguyên ưu tiên failure hiện có nếu đồng thời xuất hiện evidence failed mâu thuẫn.


Ghép bằng chứng bằng **thiết bị + run_id** hoặc **thiết bị + interaction_id**.
OS cũng ghi run không phải thoại: không tự đưa chúng vào mẫu số. Với
`route=realtime_handled`, chỉ chấp nhận `realtime_turn_done`; bỏ run backend
đồng bộ bộ nhớ. Bất kỳ evidence kết thúc `failed` đã ghép được tới as-of đều giữ nguyên lỗi:
`lifecycle_end` tới sau không xoá được, vì có thể chỉ là cleanup. Nếu không có
failed thì lấy execution timestamp mới nhất không vượt as-of; nếu trùng thời
gian ưu tiên completed hơn unknown. Lỗi recovered là `unknown`, nên end thật
tới sau có thể xác nhận completion. Đây là lỗi kết thúc thực thi, không phải
mọi lần gọi tool: lỗi tool được xử lý có thể recover mà không thành terminal
failure. Dispatch lỗi không có execution evidence được tính failed. `Result.ExecutionFailed`
ghi nhận lỗi API HAL của local intent vốn trước đây chỉ được log; handler trả
về sau lỗi này phát `local_intent_error`, không phát completion.

OpenClaw lưu pending trace trước khi ghi `chat.send`, rồi xoá khi ghi thất bại. Correlation telemetry giữ tối đa 1024 trace trong 24 giờ bằng buffer riêng. Routing giữ matcher và TTL 2 phút cũ, pending-send busy vẫn 30 giây. Alias UUID chỉ dành cho metric, không dùng cho TTS/dispatch, cần đúng một message khớp toàn bộ sau trim với nội dung thực gửi; match mơ hồ, không có match hoặc `chat.history` lỗi vẫn chưa xác định được ID. Thiếu terminal vẫn là `incomplete`; bản sửa không khôi phục event lịch sử hay đánh giá câu trả lời đúng/sai.

Codex, Claude Code, OpenCode và PicoClaw ghi `execution_observation_lost` cho run đã gửi, chưa kết thúc trước khi cleanup do mất transport/timeout. Không tạo lifecycle giả cho runtime. Evidence này không ghi đè completed/failed đã ghép được, kể cả final đến đồng thời; thiếu terminal thì giữ `unknown` trong mẫu số. Hermes đã xử lý kết thúc stream qua lifecycle.

Khi có `harness_delegated`, chỉ chọn evidence Harness, `dispatch_error` hoặc `execution_observation_lost`; bỏ lifecycle bàn giao của agent cục bộ. Marker bàn giao không ghi đè evidence tiếp theo, bất kể thứ tự nhận. Answer qua HTTP voice-mode là tác vụ Chat mới; answer nói qua microphone vẫn là Voice. Câu hỏi chờ trả lời vẫn nằm trong mẫu số với trạng thái unknown. Gemini `realtime_handled` vẫn chỉ nhận `realtime_turn_done`. Correlation Harness không đoán giữa các route đồng thời hay nhận run ID tường minh không khớp. Xem [telemetry Harness](harness_vi.md#telemetry-hoàn-tất-tác-vụ).

Horizon mặc định là **0 giây**: mọi tác vụ eligible bắt đầu tới `as_of_ms`
được đếm ngay. Lượt failed, unknown và chưa xong **vẫn ở mẫu số**. Tử số là lượt
có evidence được chọn là `completed`. Ví dụ 85 lượt xong trên 100 lượt eligible
là **85%**. So tỷ lệ chưa làm tròn với **85%**; mẫu số rỗng là **N/A**
(`kpi3_pct`, `meets_target` bằng JSON `null`). Eligibility chưa rõ được báo riêng.

`--settle-seconds 1800` là chính sách báo cáo tùy chọn, không phải mặc định hay
timeout thực thi. Khi chọn horizon dương, chỉ start tới
`as_of_ms - settle_seconds * 1000` vào `eligible_mature_turns`; lượt mới hơn là
`fresh_pending_turns`, kể cả đã xong. Với mặc định 0, tên field lịch sử
`eligible_mature_turns` nghĩa là tất cả lượt eligible tới as-of. Cửa sổ ack
10 giây và TTL speech-active 45 giây không kết thúc tác vụ. Completion muộn
được đếm khi chạy lại report với evidence mới. Lấy execution tới as-of,
không cắt ở thời điểm start cuối.

## Runbook cho agent: query và report KPI-3

Trong phiên truy cập device đã được phép, xuất cả hai service kèm hostname:

```bash
journalctl -u hal -u os-server --since '7 days ago' -o json --no-pager > /tmp/voice-task-journal.jsonl
```

Copy file về máy bằng quyền truy cập device đã có, rồi chạy từ repo root
(hoặc pipe export vào stdin):

```bash
python3 scripts/report_voice_task_metrics.py voice-task-journal.jsonl > voice-task-report.json
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --now-ms 1789088400000 --settle-seconds 0 > voice-task-report.json
```

Reporter lọc observation trước khi chọn revision và loss counter, dùng
`__REALTIME_TIMESTAMP` của journal (microseconds), `event_timestamp` của AA
(seconds), hoặc `observed_at_ms` của dòng chuẩn hóa (milliseconds). Dòng sau
cutoff được đếm trong `future_events_excluded`. Export thiếu timestamp vẫn đọc
được nhưng tăng `missing_observation_timestamp_events`; không thể bảo đảm
snapshot lịch sử chính xác, nên phải dùng export đã cắt tại thời điểm cần báo
cáo. CLI đọc JSONL từng dòng và chỉ giữ trạng thái metric, không nạp toàn bộ
journal vào RAM.

`--now-ms` cố định thời điểm as-of Unix milliseconds để tái lập; dùng cutoff
thật của export. `--device <hostname>` bổ sung identity khi dòng thiếu thiết
bị; **không phải bộ lọc thiết bị**. Hỗ trợ journal JSONL (cả ANSI màu và
`MESSAGE` dạng mảng byte của journald) và AA JSONL chứa
`event_name`, `data.user_pseudo_id`, `data.event_params` dạng key/value. Giữ
amendment và execution trong export. Reporter nhóm theo device, lấy task revision HAL lớn nhất,
gộp snapshot HAL và event start/bind OS bằng interaction/run. ID bắt đầu `vi-smoke-` mặc định bị loại;
`--include-synthetic --settle-seconds 0` chỉ để kiểm tra instrumentation,
không dùng công bố KPI sản phẩm. Caller diagnostic phải truyền interaction id
`vi-smoke-`: request voice API không đánh dấu vẫn vào cohort vì telemetry không
thể tự biết đó là người dùng thật hay test.

AA lịch sử thiếu cả snapshot task HAL có version lẫn OS task-start không thể
được reporter tự bổ sung. Flow Monitor `DONE` có thể chứng minh đã chạy xong
trong log local, nhưng không tái tạo cohort/evidence AA còn thiếu. Báo riêng
coverage này; không diễn giải không có lượt eligible được đo thành không có
tác vụ thực tế chạy xong.

Báo interval, as-of, horizon, nguồn (log local hay AA), từng device và tổng
`completed_turns / eligible_mature_turns`, phần trăm, `meets_target`. Kèm
`failed_turns`, `unknown_turns`, `incomplete_turns`, `fresh_pending_turns`,
`ineligible_turns`, số legacy/thiếu start bị loại và eligibility chưa rõ.
Ghi rõ **đã chạy xong; chưa đánh giá làm đúng**. Báo coverage malformed/thiếu
identity và counter mất telemetry; log local có thể chứa event AA bị thiếu.
Reporter lấy max theo device cho `hal_dropped_total`, `hal_failed_total`,
`telemetry_dropped_total`, `telemetry_failed_total`, `unknown_owner_playbacks`,
rồi cộng max các device ở aggregate. Đây là tín hiệu coverage, không phải tổng
mất mát chính xác qua restart/interaction; xem mốc reset trước khi diễn giải.

Dòng INFO `[telemetry] delivered` cho `voice_metrics_*` chứng minh HTTP gửi AA
thành công. `[telemetry] event` chỉ chứng minh ghi local. Cả hai chưa chứng
minh query thấy row trong kho. Muốn xác minh warehouse phải có quyền đọc và
query/export thấy `event_id` cùng interaction/run tương ứng; credential ghi
AA không tự cung cấp API đọc.

## Truy vấn kho dữ liệu

Giả định schema (ghi rõ vì schema kho AA không nằm trong repo này): event vào
bảng `event_tracking` với `event_name`, `event_timestamp`, và `event_params` là
mảng `{key, value}` với `value` lưu dạng chuỗi — đúng hình dạng thiết bị này
gửi lên và web/mobile đang gửi. Nếu kho của bạn trải params thành cột thì sửa
lại hàm trích xuất.

Xem các truy vấn SQL đầy đủ (KPI-1, KPI-2, KPI-3, coverage) trong bản tiếng Anh:
[`docs/voice-metrics.md`](../voice-metrics.md#warehouse-queries).

Agent **chỉ có AA event tracking** vẫn query và report được, không cần SSH
hay Flow Monitor. Query ba event dưới đây, giữ `event_timestamp`,
`data.user_pseudo_id` và toàn bộ `data.event_params` dạng mảng key/value khi xuất
JSONL. Đổi tên bảng/cách trích params theo schema warehouse thực tế.

```sql
-- BigQuery-style; thay khoảng thời gian và device cho báo cáo thực tế.
SELECT event_name, event_timestamp, data
FROM event_tracking
WHERE event_name IN (
  'voice_metrics_interaction',
  'voice_metrics_task_started',
  'voice_metrics_task_execution'
)
  AND event_timestamp >= UNIX_SECONDS(TIMESTAMP('2026-09-11 00:00:00+07'))
  AND event_timestamp <= UNIX_SECONDS(TIMESTAMP('2026-09-11 12:00:00+07'))
-- AND data.user_pseudo_id = '<device identity lưu trong AA>'
ORDER BY event_timestamp;
```

Lấy đủ start, snapshot cập nhật và execution tới as-of. Không chỉ lấy event
completed vì sẽ loại lượt chưa xong khỏi mẫu số. Nếu cohort cần chứa lượt bắt
đầu trước mốc dưới của export, lấy cả start của lượt đó. Reporter không có
CLI start-window; file đầu vào quyết định khoảng dữ liệu được chấm.

```bash
python3 scripts/report_voice_task_metrics.py aa-export.jsonl --settle-seconds 0 > voice-task-report.json
```

Với cutoff lịch sử, thêm `--now-ms <Unix milliseconds của cutoff>`. Reporter
là bộ chấm chuẩn: gộp HAL/OS bằng device + interaction/run trước khi đếm, lấy
start sớm nhất và revision HAL lớn nhất, ghép execution, giữ lỗi terminal,
chặn memory-sync realtime. Không duy trì SQL chấm riêng dễ đếm trùng hoặc bỏ
lượt chỉ có OS start. Field trong params cần giữ:

| Event | Field dùng chấm tác vụ |
|-------|------------------------|
| `voice_metrics_interaction` | `interaction_id`, `run_id`, `route`, `task_schema_version`, `task_revision`, `task_started_at_ms`, `task_eligible`, `task_eligibility_known`, `failure_reason` |
| `voice_metrics_task_started` | `schema_version`, `interaction_id`, `run_id`, `event_type`, `task_started_at_ms` |
| `voice_metrics_task_execution` | `schema_version`, `interaction_id`, `run_id`, `outcome`, `evidence`, `execution_at_ms` |

Báo **`completed_turns / eligible_mature_turns * 100`**, cả hai số đếm,
`meets_target`, failed/unknown/incomplete và coverage bị loại. Mặc định 0 giây
nghĩa là mọi lượt eligible đã bắt đầu tới as-of đều ở mẫu số; mẫu số rỗng là
N/A. `completed` nghĩa là hoàn tất kỹ thuật không có lỗi kết thúc được ghi
nhận, không chứng minh đúng yêu cầu user. Agent không có script phải áp dụng
đúng quy tắc gộp identity/evidence ở trên; đếm riêng số dòng execution là sai.
Tổng KPI dùng tổng completed chia tổng eligible, không lấy trung bình phần
trăm. Identity trống phải báo riêng, không gộp thành device giả. Counter mất
telemetry cộng dồn theo process, có thể reset khi restart: xem maxima và mốc
reset, không cộng từng event.

## Log local trên thiết bị

Mọi event được ghi log **trước khi** gửi, và lỗi gửi cũng được ghi — bản trên
kho mới là bản có thể thiếu.

```bash
journalctl -u hal -f | grep '\[telemetry\]'        # HAL: mọi event + lỗi POST
journalctl -u hal -f | grep '\[voice-metrics\]'       # quyết định ack / biên / stale
journalctl -u os-server -f | grep '\[telemetry\]'  # os-server: đã gửi, đã drop, đã lỗi
```

Ghép các dòng bằng `interaction_id`, không dựa vào vị trí gần nhau.
`Session END` và dòng `[turn] route=` đều có id này; verdict xuất hiện sau
timer quan sát 10 giây nên có thể xen giữa log của session tiếp theo.
JSON telemetry local của HAL cũng có `event_id` để ghép amendment với event
gốc ngay cả khi chưa bật gửi analytics.

```bash
journalctl -u hal -o cat --no-pager | grep -F 'vi-<interaction-id>'
```

`eligible=false` và `ack_latency_ms=null` là mẫu bị loại; xem
`exclusion_reason`. `eligible=true` và ack null là chưa ghi nhận acknowledge
trong cửa sổ quan sát. Có transcript không đồng nghĩa với đủ điều kiện: cổng
wake-word hoặc model realtime vẫn có thể loại lượt không hướng tới thiết bị.
Transcript chỉ nằm trong log voice local sẵn có, không được thêm vào payload
telemetry.

## Cấu hình

Cả hai giá trị nằm trong `/opt/hal/.env` của body, os-server load lúc khởi
động — đổi kho dữ liệu cho một thiết bị không cần build lại:

| Key | Ý nghĩa |
|-----|---------|
| `AUTONOMOUS_ANALYTICS_URL` | Nơi bắn event — **đồng thời là công tắc bật/tắt**. Rỗng (mặc định) = không có gì rời khỏi thiết bị. Không có endpoint mặc định trong code. |
| `AUTONOMOUS_ANALYTICS_ID` | Key authorization của AA. Thiếu ⇒ báo lỗi gửi rõ ràng (`[telemetry] delivery failed`). |

Cả hai tiến trình (HAL và os-server) đọc cùng một key, đọc mỗi lần gọi — điền
vào rồi restart service là đủ, không cần build lại. Event vẫn ghi log local
trong mọi trường hợp; URL rỗng chỉ bỏ chặng gửi mạng.

Thứ tự phân giải: env của process → `/opt/hal/.env`. Chặng HAL→os-server
(`http://127.0.0.1:5000/api/telemetry/event`) vẫn là hằng loopback như mọi lời
gọi HAL→OS khác.

## Riêng tư

Không bao giờ gửi transcript, text câu trả lời, audio thô hay credential. Chỉ
id, outcome trong danh sách cố định, và các khoảng thời gian. `user_pseudo_id`
là hostname thiết bị, `platform` là `device` (xem `system/lib/analytics`).

## Giới hạn

- **Điểm kết thúc là phát hiện, không phải âm học.** Latency tính từ silence
  clock / STT final, không phải từ lúc sóng âm thật sự dứt.
- **Chưa đo phản hồi bằng hình ảnh** (xem trên).
- **`auto_supersede` tuỳ theo body.** Mặc định trong code của
  `OS_REALTIME_SUPERSEDES_MAIN_REPLY` là TẮT (không đổi ở đây), nhưng lamp tự
  bật trong `.env` của nó; body nào tắt thì lát cắt đó không có mẫu nào.
- **Phát hiện stale ở mức playback, không ở mức mẫu audio.** Nó chấm các khoảng
  phát vượt mốc grace; không nói được còn bao nhiêu mili-giây PCM nằm trong đệm
  phần cứng.
- **Audio đã giao cho driver, không phải âm trong phòng.** Ack bằng audio và
  `audio_latency_ms` dùng lần ghi đầu; không đo đệm ALSA/Bluetooth sau đó.
  Ack nhận xử lý không chứng minh đã phát tiếng hay hoàn tất tác vụ.
- **Playback không ai nhận thì để không quy chủ, có chủ đích.** Nó được đếm
  (`unknown_owner_playbacks`) và loại khỏi quyết định ack, thay vì đoán.
- **Cửa sổ 60 giây vẫn có thể hết sớm.** Những dòng đó mang
  `observation_complete = false`. Lần phát stale đã xác nhận vẫn tính là lỗi;
  chỉ dòng chưa quan sát thấy stale mới là coverage loss.
- **State chỉ nằm trong RAM.** HAL restart thì mất các interaction còn trong
  cửa sổ quan sát; những mẫu đó thiếu chứ không sai.
- **Tracker giữ tối đa 32 interaction.** Cái cũ bị đẩy ra trước hạn ghi verdict
  sẽ được báo cáo sớm, gắn cờ `eviction = 'tracker_capacity'`, chứ không bị bỏ
  im — cửa sổ quan sát của nó bị cắt ngắn.
- **Mất mát khi gửi được báo cáo, không giấu**: `telemetry_dropped_total`,
  `telemetry_failed_total`, `hal_dropped_total`, `hal_failed_total` đi kèm mọi event.

## Lệnh kiểm chứng

```bash
go build ./...                                   # os-server + package tracking
go test ./system/telemetry/ ./system/server/telemetry/...
make hal-lint
cd hal && .venv/bin/python -m pytest test/test_voice_metrics.py -q
```

## Kiểm chứng device — 2026-09-11

Đã kiểm chứng trên `lamp-0c89` (`172.168.20.169`), version
`0.1.88-voice-task`, bằng deploy được cho phép và curl smoke request.
Không lưu transcript hoặc toàn bộ journal thiết bị vào repo.

| Quan sát | Định danh và kết quả |
|----------|---------------------|
| Local intent hỏi giờ giả lập | `vi-smoke-c5e84236c9` → `local_intent_returned`, event `vte-RAUD26IPOSIDNMCK3LF4F3PRVJ` |
| Tác vụ tính toán delegate giả lập | `vi-smoke-09bf638baa` → run `device-chat-4-1789090404539` → `lifecycle_end` completed, event `vte-CQ54WS3EVMVLQRQPSP6FPYCITP`; AA delivered lúc 08:33:29 +07 |
| Turn realtime tự nhiên | `vi-85eff37e57264e94` → `realtime_turn_done`, event `task-rt-vi-85eff37e57264e94`; AA delivered lúc 08:34:11 +07 |

Report offline chỉ lấy mẫu giả lập đạt **2/2** với
`--include-synthetic --settle-seconds 0`: chỉ kiểm tra instrumentation, không
chứng minh sản phẩm đạt 85%. Snapshot 08:35 +07 với horizon 1800 giây dùng lúc đó
cho **N/A**, hai lượt fresh pending. Đã xác minh HTTP gửi AA thành công;
chưa thực hiện query đọc warehouse.

Bản rollback trên device: `/tmp/os-server-before-voice-task` và
`/tmp/voice-task-hal-before.tgz`; đây không phải file trong repo.
Kiểm tra local gồm test Go tập trung cho domain, intent, telemetry và handler,
75 test HAL tập trung, 20 unittest reporter. HAL lint dùng test venv tạm dưới
`/tmp` có pyflakes. Không công bố toàn bộ HAL suite xanh:
`test_gemini_generation_complete.py:52` có lỗi baseline nhận `InterruptedOutput`
nhưng chờ `TextOutput`, đã tái hiện cả trên HEAD.

Xem [các lệnh kiểm chứng đầy đủ trong bản Anh](../voice-metrics.md#device-validation--2026-09-11):
focused Go test, 75 HAL pytest, 20 reporter unittest, HAL lint qua venv có
pyflakes và build Linux ARM64 gắn version `0.1.88-voice-task`. Binary ARM64 cuối
đã được cài lên device; os-server restart và active.

### Sửa cohort OS — kiểm tra trên device (2026-09-11)

Deploy `0.1.88-voice-task-cohort-fix` lên `.169` lúc 09:21:06 +07.
Hai request voice-followup bằng `curl` có interaction ID
`vi-smoke-cohort-fix-*` nhưng **không có HAL task snapshot**, tái hiện trường
hợp thiếu cohort. Local intent hoàn tất lúc 09:21:30; lượt delegate
`device-chat-3-1789093296472` kết thúc lúc 09:21:59 với `aborted=false`.
Reporter chạy với `--include-synthetic` chuyển từ **1/2 = 50%** khi main agent
còn chạy sang **2/2 = 100%** sau terminal event. Event nhận task, binding và
execution đều có log `[telemetry] delivered`. Đây là xác nhận HTTP ingestion,
không chứng minh dữ liệu đã query được ở warehouse hay KPI production.
Mặc định loại lượt smoke. Binary dự phòng trên device:
`/tmp/os-server-before-cohort-fix`.

Đã pass `go test ./system/telemetry ./system/server/sensing/delivery/http
./system/server/agent/delivery/http`; unittest reporter pass 29 test, gồm
100 lượt / 85 hoàn tất, lượt chỉ có OS, chống đếm trùng HAL/OS. Go integration
test kiểm tra binding khi queue và lỗi not-ready. Build os-server Linux ARM64
pass. Không tạo giả AA event cho dữ liệu lịch sử bị thiếu.

Truy vấn KPI-1 trong bản EN trả cùng lúc `observed_interactions`,
`endpoint_known_interactions`, `endpoint_unknown_interactions`, `endpoint_excluded_interactions`, `eligible_samples`,
`no_ack` và `kpi1_pct`, tách theo `ack_schema_version` (thiếu field = 1).
Dedup theo device, interaction và schema; ưu tiên `task_revision` rồi amendment/thời gian;
`no_ack` hợp lệ vẫn trong mẫu số, không có mẫu hợp lệ trả NULL (N/A).
