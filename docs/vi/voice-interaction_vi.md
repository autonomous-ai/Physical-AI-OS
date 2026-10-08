# Tương tác giọng nói: nói chuyện với Lamp như với một người

Tài liệu này là bản thiết kế cho vòng lặp giọng nói của Lamp: "tốt" nghĩa là
gì bằng con số, vòng lặp đang ship hôm nay làm gì, vì sao nó chưa đạt, và bản
thiết kế lại. Code là nguồn chân lý cho những gì đã được triển khai; cột
*Trạng thái* ở §6 cho biết phần nào đã có.

English: [`../voice-interaction.md`](../voice-interaction.md).
Liên quan: [`realtime-voice_vi.md`](realtime-voice_vi.md) (lớp realtime chi
tiết), [`voice-metrics_vi.md`](voice-metrics_vi.md) (KPI và tracker
telemetry), [`../benchmarks.md`](../benchmarks.md) (cách đo trên robot).

## 1. Chuẩn mực: một vòng lặp giọng nói giống người làm gì

Con người trả lời nhau trong khoảng 200 ms, và khi cần lâu hơn họ thể hiện
điều đó (một cái liếc, một cái gật, "hừm") thay vì đứng im. Lamp có mặt, vòng
đèn và đầu, nên nó có thể làm như vậy. Chuẩn mực, dưới dạng các hành vi kèm
ngân sách thời gian tính từ lúc người dùng ngừng nói:

| Hành vi | Ngân sách | Vì sao |
|---|---|---|
| **Chú ý**: vòng đèn và đầu cho thấy nó đang lắng nghe trong lúc người dùng nói | ≤ 200 ms sau khi bắt đầu nói | Tín hiệu cục bộ, không cần mạng |
| **Ghi nhận**: vòng đèn và đầu cho thấy lượt đã được tiếp nhận | ≤ 300 ms sau khi nói xong | Khiến khoảng chờ dài hơn vẫn có cảm giác được quan tâm |
| **Trả lời** (hội thoại, kiến thức, tra cứu) | từ đầu tiên p50 ≤ 1.2 s, p90 ≤ 2.0 s | Model ấm sẵn và speech streaming làm được điều này |
| **Tác vụ** (lịch, hành động thiết bị, research): nói nó đang làm gì, rồi kết quả | "đang làm" ≤ 1.5 s; kết quả được nói ra khi sẵn sàng, kể cả khi người dùng tán gẫu trong lúc đó | Cách một người xử lý việc vặt |
| **Nhận biết đối tượng**: trả lời khi được nói với, im lặng trước TV và người khác | lượt được admit được trả lời ≥ 95 %; hiếm khi trả lời nhầm tiếng nói nền | Tên, ánh nhìn, hội thoại đang mở hoặc câu trả lời cho chính câu hỏi của nó là bằng chứng |
| **Ngắt lời**: dừng khi người dùng nói chen vào (tap luôn được; giọng nói khi đường echo cho phép) | audio dừng ≤ 200 ms | Một người dừng giữa câu |
| **Không thất bại im lặng**: đã nghe nhưng không trả lời được → nói ra | luôn luôn | Im lặng là kết cục tệ nhất |
| **Thứ tự**: mỗi câu hỏi một câu trả lời, theo thứ tự; tán gẫu mới không bao giờ xóa câu trả lời của tác vụ đang chạy | luôn luôn | |

Các KPI hiện có trong [`voice-metrics_vi.md`](voice-metrics_vi.md) vẫn là bảng điểm:
KPI-1 (được ghi nhận trong 3 s) ≥ 95 %, KPI-2 (câu trả lời cũ bị phát) < 5 %,
KPI-3 (tác vụ thoại chạy xong) ≥ 85 %. Trên log thiết bị tháng 2026-09, KPI-1 là 65 %.

## 2. Những gì đang ship hôm nay

README mô tả thiết kế là *một giọng nói, ba cách hành động*: một model realtime
(Gemini Live) trả lời hội thoại trực tiếp; bất cứ thứ gì cần skills, memory hay
phần cứng được delegate (chuyển giao) sang runtime chính (mặc định là Hermes);
công việc trên máy tính đi qua Harness và kết quả trở về dưới dạng một bản tóm
tắt ngắn được đọc lên. Ý tưởng là đúng: một model nhanh cho khoảnh khắc, một
agent có năng lực cho công việc.

Lamp Standard đang ship (`robots/lamp/rootfs/opt/hal/.env`, `ROBOT.md`) chạy
**đường turn** (turn path): `HAL_LIVE_MODE=false`, wake word bật với config
mới, Gemini `gemini-3.8-live-extended-thinking` ở thinking level LOW, Google
Search grounding bật, native audio tắt (audio của Gemini bị bỏ và transcript
của nó được ElevenLabs đọc lại qua HTTP), STT qua gateway Autonomous trên một
WebSocket mới cho mỗi lượt.

Một câu nói đi qua những gì, từ đọc code cộng với số liệu thiết bị từ các
PR #276, #454, #473, #562, #602, #608 và `realtime-voice.md`:

| Giai đoạn | Chờ tuần tự | Điển hình |
|---|---|---|
| Entry VAD → mở socket STT, frame mic được buffer | partial đầu tiên 1.5–2.5 s | ẩn sau lúc đang nói |
| Endpoint: im lặng 1.2 s, hoặc 0.8 s sau STT final; fallback Smart Turn 2.5 s | **0.8–2.5 s sau từ cuối cùng** | 0.8–2.5 s |
| Đóng STT và drain final (lượt mở hội thoại) | 0.2–1.0 s | |
| Noise guard (Silero trên toàn bộ buffer, ≤ 3 từ), join speaker-ID | 0.2–0.6 s | |
| Chuẩn bị session: bị park sau 45 s idle → reconnect; bị quarantine sau mỗi lần delegate → rebuild | 0–4 s | gặp ở ~45 % số lượt (8/18, #473) |
| Câu đầu tiên của Gemini | **trung vị 4.0 s, không lần nào dưới 3.0 s** (n=31, lamp-0c89) | 3–6 s |
| Đường ElevenLabs giữ câu đã xong đến khi có chunk kế tiếp hoặc generation kết thúc; clause streaming tắt | 0–2 s | |
| ElevenLabs HTTP byte đầu tiên, tempo 1.2×, ALSA | 1.8–2 s | |
| Sau khi trả lời: grace NON_BLOCKING 6 s cộng kiểm tra outcome **trên thread thu âm** | mic không được đọc | 0–16 s |

Đầu cuối, đo trên các lamp: hội thoại **trung vị 4 s** (3–8 s); lượt delegate
**6–22 s không có tiếng gì**; một follow-up ngay sau câu trả lời có thể bị bỏ
lỡ vì mic không được đọc. Khoảng 110 timer, ngưỡng và cờ nằm trên đường này, và
22 gate khác nhau có thể bỏ một câu nói trong im lặng (wake final không khớp,
chỉ toàn từ filler, ≤ 3 từ với tỉ lệ voiced thấp, transcript rỗng, echo prefix,
giống câu trả lời trước, session đang rebuild, lookback bị xóa sau playback, mic
bị drain trong lúc grace, …).

Năm chuỗi sửa → regression → sửa đã đi qua đường này từ 2026-09-03 đến
2026-10-08 (filler, live mode, nói clause đầu tiên, prompt delegate, gate
wake/focus), gần như tất cả được kiểm chứng bằng mock hoặc một lamp duy nhất.

## 3. Vì sao chưa đạt

1. **Hai bộ não nối tiếp, định tuyến bằng prompt.** Model realtime quyết định
   theo từng lượt, từ ~100 dòng prompt cho mỗi provider, rằng nên trả lời,
   delegate hay im lặng. Delegate nghĩa là: chờ model thinking, phát tool call,
   chuyển text tới os-server, chạy intent matching, chuyển tới Hermes, chờ câu
   đầu tiên của nó, tổng hợp giọng. Model được dặn không nói gì trong lúc chuyển
   giao, và filler của main agent đang bị tạm tắt, nên người dùng nghe im lặng.
   Mỗi lần delegate cũng quarantine session, nên lần thu tiếp theo phải trả giá
   reconnect.
2. **Đường commit tuần tự, không chồng lấn.** Socket STT lạnh, drain final,
   noise guard, join speaker-ID, chuẩn bị session và drain announcement đều chạy
   lần lượt giữa từ cuối cùng và commit. Model thinking, việc giữ câu và tổng
   hợp giọng hai lần rồi thêm vài giây giữa commit và âm thanh đầu tiên.
3. **Không có câu trả lời tất định cho "có phải nói với mình không?".** Khi wake
   word tắt, mọi trigger đều là "được nói với"; khi bật, cửa sổ follow-up 5 s và
   opener bằng gaze là bằng chứng phi ngôn ngữ duy nhất. Lamp có trả lời "yeah"
   sau khi đặt câu hỏi hay không, có bỏ qua TV hay không, đều phó mặc cho model
   và một danh sách từ.
4. **Không có turn identity chung giữa hai bộ não.** Câu trả lời, filler và lịch
   sử được khớp theo thời gian ("interaction mới nhất", watermark), đó là lý do
   câu trả lời cũ và trùng lặp cứ quay lại dưới nhiều dạng khác nhau.
5. **Half-duplex trên Lamp Standard.** Mic và loa là hai thiết bị USB riêng với
   clock chạy tự do; AEC phần mềm chỉ xử lý được ~6 dB, đỉnh echo cao hơn ngắt
   lời thật, nên barge-in bằng giọng đã bị gỡ và mic bị drain trong lúc lamp
   nói. Các profile Pro (mảng XVF3800 với AEC phần cứng) đã chạy live mode.
6. **Không có vòng đo lường.** Không có corpus audio ghi sẵn, không có harness
   replay, analytics tắt trên lamp, KPI-1 không tính được trong live mode. Chính
   sách thay đổi theo cảm giác.

## 4. Thiết kế lại: một hội thoại, ba độ trễ

Người dùng nên cảm nhận **một người** trả lời ngay lập tức, làm việc nhỏ trong
lúc nói, và nói "chờ tôi chút" cho việc lớn rồi quay lại. Phía sau, giữ hai
engine, nhưng đổi bên sở hữu hội thoại:

```
                 ┌──────────────── Voice agent (owns the conversation) ───────────────┐
 mic ──► local   │  always-warm session · native speech or streaming TTS · one state   │ ──► speaker
        gate ──► │  machine · admission evidence in, one answer stream out             │     ring/head
                 └──────┬──────────────────────┬──────────────────────┬───────────────┘
                        │ instant              │ fast (≤100 ms)       │ slow (seconds–minutes)
                        ▼                      ▼                      ▼
                  answer itself         device tools (HAL)      run_task → main runtime / Harness
                                        LED · look · move         result comes back INTO the
                                        volume · timers           conversation; the voice agent
                                                                  narrates: "I'll check" … "Two
                                                                  meetings tomorrow: …"
```

Ba route của README gộp thành một vòng lặp với tool thuộc ba lớp độ trễ.
Runtime chính không còn là người nói thứ hai; nó là một worker mà kết quả được
voice agent đọc lên, theo thứ tự, bằng một giọng.

### 4.0 Trải nghiệm, từ đầu đến cuối

Người dùng thấy và nghe gì ở mỗi khoảnh khắc, ngân sách, và nó nằm ở đâu.
Các hàng đánh dấu *thiết bị* đã được xác nhận trên một lamp; phần còn lại ở
mức code.

| Khoảnh khắc | Lamp làm gì | Ngân sách | Ở đâu |
|---|---|---|---|
| Có người bước vào / nhìn vào nó | Socket STT kết nối trước; session Gemini đang bị park được nối lại; quy tắc chào theo presence như trước | trước từ đầu tiên | `stt_warm.py`, `prewarm.py`, `gaze.record_sample` |
| Người dùng bắt đầu nói | vòng đèn mờ sang "đang nghe" ngay lập tức khi quay mặt về lamp (wake word tắt) hoặc khi có wake phrase; đầu bắt lại người nói | ≤ 200 ms | `_vad_loop`, `show_listening_pending_cue`, `gaze` |
| Người dùng gọi tên nó giữa câu | được tính là nói với nó, kể cả không có wake phrase | — | `turn_admission.name_mentioned` |
| Người dùng ngừng nói | endpoint sau 1.0 s im lặng / 0.6 s sau STT final; commit trên partial ≥ 4 từ; khuôn mặt thinking + vòng đèn | ≤ 300 ms tới cue | `_stream_session_impl`, `run_realtime_turn` |
| Trả lời nhanh (giờ, âm lượng, đèn) | quy tắc cục bộ, câu nói cache sẵn, không cần model | < 1 s | `system/intent` |
| Hội thoại / kiến thức / tra cứu | Gemini Live trả lời bằng chính giọng của nó (native audio); câu trả lời dài được stream | từ đầu tiên p50 ≤ 1.2 s mục tiêu; 4 s hiện nay (*thiết bị*) | `realtime_turn.py`, `gemini_live.py` |
| "yeah" / "no" cụt sau khi Lamp hỏi | được coi là câu trả lời | — | `turn_admission.device_question_pending` |
| Trả lời chậm | vòng đèn + khuôn mặt giữ "đang nghĩ"; một câu nối nói ra ("One sec.") chỉ sau 4 s | — | `_WaitFiller`, `fillers.go` |
| Tác vụ (lịch, thiết bị, nghiên cứu) | giao cho main agent; câu "Let me check…" tuỳ chọn; kết quả được đọc bằng cùng giọng Google khi sẵn sàng, không bao giờ bị chit-chat đến sau làm câm; lỗi được nói ra | ack ≤ 1.5 s mục tiêu; kết quả 6–22 s hiện nay (*thiết bị*) | `delegate_to_main`, `IsTaskRun`, `speakVoiceTurnFailure` |
| Nói tiếp ngay sau câu trả lời | mic được đọc lại ≤ 1 s sau khi playback kết thúc; cửa sổ hội thoại (8 s) giữ câu tiếp theo được tính là nói với nó mà không cần tên | — | grace cut, `CONVERSATION_WINDOW_S` |
| TV / người khác | không tên, không quay mặt, không cửa sổ, không câu hỏi đang chờ → model được báo "unknown — stay silent"; `strict` loại bỏ trước khi tới bất kỳ model nào | — | `addressed_hint`, `HAL_ADDRESSED_GATE` |
| Ngắt lời | tap dừng tiếng nói ≤ 250 ms (*thiết bị*); barge-in bằng giọng cần AEC phần cứng | — | `button_actions`, §4.5 |
| Lamp không trả lời được | nói thẳng ra ("Sorry, I couldn't finish that one.") thay vì im lặng | — | `agent.voice_turn_failed` |
| Phòng không có người | socket STT được giải phóng, session Gemini bị park (hoặc được giữ sống bằng ping khi bật) | — | `stt_warm`, `_maybe_keepalive` |

### 4.1 Bộ điều khiển lượt (turn controller)

Một state machine cho mỗi thiết bị, `IDLE → LISTENING → ENDPOINTING → THINKING →
SPEAKING → (follow-up window) → IDLE`, với một **quyết định admission** cho mỗi
câu nói, được log kèm bằng chứng. Mọi gate hiện nay trở thành đầu vào của quyết
định đó thay vì là các điểm drop độc lập:

| Bằng chứng | Nguồn | Tác dụng |
|---|---|---|
| Wake phrase (partial hoặc final) | STT | admit |
| Cửa sổ hội thoại đang mở (follow-up sau câu trả lời) | controller | admit |
| Thiết bị hoặc main agent vừa đặt câu hỏi | lịch sử TTS (`turn_admission.device_question_pending`, `main_followup`) | admit, kể cả "yes"/"no" cụt |
| Người dùng nhìn vào lamp trong lúc nói | gaze (`HAL_GAZE_WAKE`) | admit |
| Giọng đã đăng ký của người dùng quen | speaker ID | tăng độ tin cậy |
| Thời lượng và tỉ lệ voiced | Silero | từ chối tiếng ồn ngắn |
| Từ ngữ trùng với câu trả lời gần nhất của thiết bị trong cửa sổ echo | lịch sử TTS | cắt prefix, hoặc từ chối nếu chỉ toàn echo |
| Không có gì ở trên, wake word bật | — | từ chối (không được nói với) |
| Không có gì ở trên, wake word tắt | — | admit; model được báo `addressed: unlikely` và có thể im lặng |

Model vẫn nhận các trường hợp mơ hồ, nhưng với một gợi ý và một việc nhỏ hơn:
nó không còn phải tự suy luận từ đầu, ở mỗi lượt, rằng căn phòng có đang nói
với nó hay không.

### 4.2 Luôn ấm sẵn

Một người đang ở trong phòng thì sẵn sàng được bắt chuyện. Session được kết
nối trước bất cứ khi nào presence hoặc gaze cho biết có ai đó ở đây, không chỉ
khi bắt đầu nói; recycle và nén context chạy nền giữa các lượt, không bao giờ
trên đường commit; một keepalive ngăn proxy đóng socket idle, nên "bị park sau
45 s" biến mất với phòng có người. Việc này cần một thay đổi ngoài repo này:
timeout idle của proxy `/ws/gemini` được nâng lên bằng giới hạn session của
chính Live API.

### 4.3 Đường commit

Từ từ cuối cùng đến âm thanh đầu tiên:

1. **Endpoint** ở điều kiện đến trước: Smart Turn báo hoàn tất, 0.6 s im lặng
   sau STT final, hoặc một fallback im lặng có giới hạn. Ngập ngừng ("ừm…") giữ
   lượt.
2. **Ghi nhận cục bộ** ngay khi lượt được admit: vòng đèn thinking và một cử
   động đầu nhỏ, trước mọi khoảng chờ mạng.
3. **Commit ngay lập tức.** Một STT partial đủ tin cậy (≥ 4 từ) admit lượt;
   STT final drain song song và chỉ nạp vào lịch sử và fallback. Speaker ID và
   noise guard không bao giờ chặn commit của một câu nói dài.
4. **Stream giọng nói.** Clause đầu tiên ngay khi có; không giữ câu đã xong để
   chờ một tag có thể đến muộn; native audio (giọng riêng của Gemini) hoặc
   ElevenLabs qua WebSocket (`eleven_v4_turbo`, ~300 ms byte đầu tiên) thay vì
   HTTP; luồng output giữ mở giữa các câu.
5. **Không blackout sau câu trả lời.** Grace cho tool call muộn chạy trên một
   task nền; mic được đọc lại ngay khi playback kết thúc.
6. **Tắt thinking cho hội thoại.** Model extended-thinking được dùng vì model
   thường delegate quá đà hoặc im lặng; khi gate tất định đảm nhận admission và
   gợi ý định tuyến, thinking có thể giảm hoặc tắt với câu trả lời trực tiếp.
   Đo trên thiết bị, không giả định.

### 4.4 Tác vụ: được tường thuật, theo thứ tự, không bao giờ mất

`delegate_to_main` trở thành một **tool không chặn**: voice agent nói nó đang
làm gì bằng lời của mình ("Để tôi xem lịch của bạn"), tiếp tục lắng nghe, và
khi kết quả của runtime chính về, nó được giao dưới dạng response của tool, nên
voice agent đọc nó lên, cùng một giọng, đúng thời điểm. Runtime chính không bao
giờ tự nói trong voice mode; nó trả về text. Thứ tự là thứ tự hội thoại của
chính voice agent, nên không có watermark và không có transcript
"[voice-instruction]". Thất bại quay về theo cùng cách ("Tôi không kết nối được
với lịch của bạn"), không bao giờ là im lặng. Gemini Live hỗ trợ điều này qua
function call `NON_BLOCKING` với scheduled response; model extended-thinking
vốn đã yêu cầu tool NON_BLOCKING.

Cho đến khi phần đó hoàn thành, hai quy tắc giữ phòng tuyến trong code hiện
tại: câu trả lời của tác vụ đã delegate không bao giờ bị mute bởi tán gẫu đến
sau (`IsTaskRun`), và yêu cầu nói bị lỗi được thông báo
(`agent.voice_turn_failed`).

### 4.5 Full duplex và ngắt lời

Live mode (uplink liên tục, VAD của provider, ~100 ms tới audio đầu tiên) là
đích cho mọi thân máy có khử echo phần cứng; profile Pro XVF3800 đã chạy nó hôm
nay. Mic và loa USB rời của Lamp Standard không thể đạt mức đó với AEC phần
mềm. Hai lựa chọn, theo thứ tự ưu tiên:

1. **Phần cứng**: một codec audio USB tích hợp với AEC trên chip (mic và loa
   cùng một clock), cùng loại linh kiện mà loa hội nghị dùng. Đây là quyết định
   bill-of-materials cho SKU Standard.
2. **Phần mềm**: giữ đường turn nhưng mở mic trong lúc playback phía sau gate
   thích ứng (`live_gate.py`): giảm âm lượng loa khi một onset voiced được xác
   nhận, chỉ hủy câu trả lời khi VAD của provider xác nhận có tiếng nói.
   Backchannel ("ừ hử") giảm âm lượng nhưng không hủy.

Tap-to-interrupt vẫn là đường luôn hoạt động trên mọi thân máy.

### 4.6 Ghi nhận và hiện diện

Phi ngôn ngữ trước: vòng đèn và đầu mang các trạng thái "đang nghe", "đã nhận",
"đang nghĩ", "đang nói" và "không làm được"; một tiếng "Hmm" nói ra chỉ khi
model chậm (`HAL_REALTIME_FILLER_DELAY_S`, hiện là 1.5 s trên Lamp), không bao
giờ ở follow-up, và không bao giờ chồng lên câu trả lời thật. Âm backchannel
chỉ ở các lượt đã admit. Lamp quay về phía người nói khi bắt đầu lắng nghe
(gaze reacquire đã làm việc này) và giữ khuôn mặt "đang nghĩ" đến từ đầu tiên.

### 4.7 Đo lường

Mỗi câu nói ghi một timeline: `[voice-metrics] speech end`,
`[turn-timing] speech_end_to_commit_ms / commit_to_first_output_ms /
speech_end_to_first_speech_ms`, `[voice-metrics] ack/answer`, `[turn] route=`,
`[admission]`. `scripts/bench/voice_turns.py server.log` biến log của một ngày
thành bảng theo từng lượt với p50/p95 cho mỗi giai đoạn và lý do drop. Các
phiên trên thiết bị ghi lại audio trong phòng (`HAL_AEC_DUMP_DIR`,
`HAL_LIVE_UPLINK_DUMP_DIR`) để thay đổi endpoint và admission được replay trên
cùng các clip trước khi ship: `scripts/bench/voice_replay.py room.wav` chạy một
bản ghi qua cổng vào, đồng hồ im lặng và bộ lọc nhiễu, rồi in ra mọi câu nói
mà lamp sẽ mở và thời điểm nó quyết định người dùng đã nói xong. Analytics bật (`AUTONOMOUS_ANALYTICS_URL`) để
KPI-1/2/3 đến từ cả fleet.

## 5. Bài học từ các sản phẩm giọng nói tốt nhất

Từ một đợt rà soát sản phẩm và tài liệu ngày 2026-10-08 (ChatGPT Voice / OpenAI
Realtime và GPT-Live, Sesame, Kyutai Moshi và Unmute, Hume EVI, Gemini Live,
Alexa+ và các loa thông minh khác, các stack Pipecat / LiveKit / Vapi /
ElevenLabs, và nghiên cứu về turn-taking). Số liệu giữ nguyên như nguồn báo
cáo; "tuyên bố" (claimed) là con số của nhà cung cấp, "đo được" (measured) là
con số độc lập.

1. **Ngân sách là khoảng nghỉ 200 ms, voice-to-voice 800 ms, 4 s trước khi filler có ích.**
   Khoảng nghỉ giữa lượt của con người phổ biến nhất (mode) ở 0–200 ms (Stivers
   2009, Levinson & Torreira 2015); khoảng nghỉ bắt đầu bị cảm nhận là ngần
   ngại từ ~600–700 ms và ~1 s là "mức tối đa chuẩn". Các stack hội tụ về mục
   tiêu voice-to-voice trung vị 800 ms (mạng 200 / endpoint+STT 400 / LLM 500 /
   TTS 200 ms, Daily 2025); mẫu production lớn duy nhất (Hamming, tháng 1/2026)
   cho thấy p50 1.4–1.7 s, p95 4.3–5.4 s. Một nghiên cứu CUI'25 (n=54) thấy
   filler nói ra chỉ giúp cảm nhận về độ phản hồi khi độ trễ ≥ 4 s và không bao
   giờ cải thiện cảm nhận về năng lực; các hệ thống filler hấp tấp nói chồng
   lên người dùng 47.9 % số lần. → §1 ngân sách, §4.6.
2. **Chưa ai giải quyết được turn-taking; ai cũng xếp chồng nhiều lớp.** TurnBench
   của Sesame (tháng 8/2026, 14 hệ thống): "không hệ thống nào vừa nhanh, vừa
   chọn lọc, vừa có recall cao cùng lúc"; server VAD của OpenAI có recall cuối
   lượt 0.955 ở tỉ lệ dương tính giả 0.525. Mọi stack đều dùng một khoảng im
   lặng ngắn (200–500 ms) cộng một model end-of-turn đã học cộng một chốt chặn
   (backstop) dài (1.3–6 s); Smart Turn v3 nặng 8 MB và chạy 12–60 ms trên CPU;
   end-of-turn "eager" của Deepgram Flux bắn sớm hơn 150–250 ms với cái giá
   +50–70 % số lần gọi model. Alexa bỏ timer cố định từ 2018 để chuyển sang đặc
   trưng âm học + transcript partial + decoder và một bộ phân xử second-pass
   (2024) giúp giảm 16 % endpoint sớm. → §4.3 bước 1.
3. **Nói một câu ngắn, rồi gọi tool; delegate chạy nền.** Hướng dẫn prompting
   realtime của OpenAI: "Trước bất kỳ tool call nào, nói một câu ngắn … rồi gọi
   tool ngay lập tức." GPT-Live (tháng 7/2026) quyết định nhiều lần mỗi giây
   nên nói, nghe hay gọi tool, và delegate phần suy luận cho một model backend
   trong khi vẫn đang nói; mục tiêu thiết kế bị rò rỉ của Alexa+ là 2–4 s trong
   khi người đánh giá đo được tới 15–23 s im lặng ở các tác vụ agentic, đúng
   kiểu thất bại cần tránh. → §4.4.
4. **Precision cho lời nói, recall cho sự chú ý.** Nhìn vào robot không đồng
   nghĩa với nói với nó: chỉ 65 % câu nói khi quay mặt về robot là dành cho
   robot (Katzenmaier 2004), trong khi tư thế đầu + lời nói đạt 92 %.
   Conversation Mode của Amazon (VAD audio + hướng đầu, 2021) giảm 80 % wake
   nhầm do tiếng nền và 42 % wake nhầm do chính nó nói, không thêm độ trễ; các
   đặc trưng ngữ cảnh hội thoại của Apple giảm 20–40 % chấp nhận nhầm ở mức
   10 % từ chối nhầm. Âm thanh TV kích hoạt nhầm loa thông minh ~0.95 lần mỗi
   giờ (PETS 2020). Backchannel "ừ hử" theo lịch cố định trên Alexa làm tăng
   cảm nhận "nó có lắng nghe" (3.91 so với 2.56) nhưng cũng tăng sự khó chịu
   (3.70 so với 2.25). Chính sách rút ra: chú ý phi ngôn ngữ có thể hào phóng,
   trả lời bằng lời phải chắc chắn. → §4.1, §4.6.
5. **Khử echo là việc của client, và clock là vấn đề.** OpenAI không có tài
   liệu về xử lý echo phía server nào; thiết bị có cả loa lẫn mic sẽ lặp lại
   chính giọng của mình nếu không có AEC phía client, và cách xử lý tạm duy
   nhất ngoài thực địa là mute mic trong lúc playback. Echo của HomePod cao hơn
   tiếng nói far-field 30–40 dB và cần một bộ triệt dư (residual suppressor)
   DNN đặt trên AEC tuyến tính. Thạch anh hàng tiêu dùng trôi ±50–100 ppm (~96
   sample mỗi phút ở 16 kHz); một bộ lọc thích ứng 100 ms hấp thụ được ~20 ppm,
   nên một mic USB và một loa USB rời đánh bại AEC phần mềm ngay từ cấu trúc.
   XVF3800 giải quyết bằng cách đưa đầu ra loa về chính clock của mảng mic.
   → §4.5.
6. **Chính sách ngắt lời, không phải phát hiện ngắt lời.** LiveKit: tối thiểu
   0.5 s tiếng nói, barge-in thích ứng, timeout 2 s cho ngắt lời nhầm sẽ tiếp
   tục câu trả lời, và lịch sử được cắt về đúng phần thực sự đã được nghe. Vapi
   ship sẵn các danh sách cụm từ tường minh "không bao giờ ngắt" (các từ ghi
   nhận) và "luôn ngắt". GPT-4o Realtime dừng trong vòng 0.23 s khi bị ngắt lời
   nhưng cũng dừng với 91–93 % tiếng nói bên lề; Nova Sonic và Gemini bỏ qua
   93–99 % backchannel nhưng mất > 2 s mới nhường lời. Giảm âm lượng (duck) khi
   có onset, hủy khi tiếng nói được xác nhận. → §4.5.
7. **Stream mọi thứ; không giữ lại gì.** Gom theo câu tốn ~200–300 ms mỗi câu
   (Pipecat); ElevenLabs Flash qua WebSocket có inference ~75 ms và byte đầu
   tiên theo vùng 100–200 ms với lịch chunk [120, 160, 250, 290] ký tự; Gemini
   3.8-live được tài liệu hóa là chỉ có native audio, nên bỏ audio của nó để
   tổng hợp lại từ text là độ trễ cộng thêm thuần túy. Số liệu từ cộng đồng cho
   Gemini Live: token đầu tiên ~100 ms và vòng quay 1–1.5 s khi tắt tool,
   2–3.5 s khi bật Google Search grounding. → §4.3 bước 4 và 6.
8. **Ánh sáng và chuyển động mang trạng thái, earcon là tùy chọn.** Mọi loa
   đang ship đều có đèn riêng biệt cho đang nghe / đang nghĩ / đang nói / mic
   mở / lỗi (Echo: nhịp xanh điều biến theo giọng nói, một vệt sáng "đang nghĩ"
   chạy vòng; Nest: một nháy "không hiểu wake word"); phát cue lắng nghe từ
   trigger cục bộ (≤ 100 ms), giữ cửa sổ mic mở 5–8 s với màu sắc riêng, đóng
   sớm khi nghe "thanks" / "stop". Hướng dẫn của Amazon: xác nhận ngầm theo mặc
   định, tường minh chỉ cho hành động tốn kém, câu trả lời "một hơi"; của
   Google: không bao giờ lặp lại nguyên văn, kết thúc sau hai lần thất bại,
   không bao giờ nói "Tôi không nghe thấy bạn". → §4.6.
9. **Giữ một đường nhanh tất định.** Dừng, âm lượng, hẹn giờ và đèn không bao
   giờ nên chờ model (trần 800 ms p50 / 1,000 ms p90 của Alexa cho hành động
   smart-home). Bảng intent cục bộ đã có sẵn; nó cũng nên sở hữu việc ngắt lời.
   → §4 tool "fast".
10. **Người dùng gọi gì là sống động, gì là máy móc.** Sống động: tốc độ, chủ
    động có chừng mực, trí nhớ, sự không hoàn hảo phù hợp ngữ cảnh, một persona
    ổn định. Máy móc: nói quá nhiều và lấp đầy các khoảng lặng, lệch nhịp, câu
    cửa miệng và nịnh bợ, không có dải động, nghe thấy echo của chính mình.
    Post-mortem của chính Sesame (2025): thời điểm (timing) "sai nhiều hơn
    đúng" và tính cách thiếu nhất quán là thứ phá vỡ ảo giác. → §1, §4.6.

## 6. Trạng thái và lộ trình triển khai

| Hạng mục | Trạng thái (2026-10-08) |
|---|---|
| "yes/no/okay" cụt được admit khi thiết bị hoặc main agent vừa hỏi (`HAL_DEVICE_QUESTION_WINDOW_S`, 12 s) | **đã triển khai** |
| Cắt echo-prefix giới hạn ở các lần thu bắt đầu trong `HAL_ECHO_PREFIX_WINDOW_S` (4 s) kể từ khi playback kết thúc | **đã triển khai** |
| Commit trên STT partial đủ tin cậy (`HAL_EARLY_COMMIT_MIN_WORDS`, 4) trong lúc final drain | **đã triển khai** |
| Cue thinking phát trước các khoảng chờ của đường commit, không phải sau chúng | **đã triển khai** |
| Dòng `[turn-timing]` và `scripts/bench/voice_turns.py` | **đã triển khai** |
| Phát lại offline audio đã ghi qua cổng vào, đồng hồ im lặng và bộ lọc nhiễu | **đã triển khai** (`scripts/bench/voice_replay.py`) |
| Socket STT được giữ ấm khi có người quanh đó (`HAL_STT_KEEPALIVE=presence`): partial đầu tiên không còn phải trả giá cho một lần connect lạnh, câu nói ngắn sống sót | **đã triển khai** (`stt_warm.py`, `.env` của Lamp) |
| Ping keepalive session Gemini dưới dạng công tắc thử nghiệm (`HAL_GEMINI_KEEPALIVE_S`, mặc định tắt) | **đã triển khai** |
| Cửa sổ hội thoại khi wake word tắt (`HAL_CONVERSATION_WINDOW_S`, 8 s): câu tiếp theo sau một câu trả lời được tính là nói với nó mà không cần tên | **đã triển khai** |
| Tên thiết bị ở bất kỳ đâu trong câu được tính là nói với nó; tên xuất hiện muộn gửi một `[TURN CONTEXT UPDATE]` | **đã triển khai** |
| Cue listening ngay khi bắt đầu nói khi người dùng quay mặt về lamp (wake word tắt) | **đã triển khai** |
| Vòng đèn mờ giữ sáng khi cửa sổ hội thoại còn mở sau câu trả lời ("vẫn đang nghe bạn") | **đã triển khai** (`_show_conversation_window_cue`) |
| "Ồ, mình lại nghĩ được rồi!" chỉ sau khi não mất kết nối ≥ 20 s; khởi động lại có kế hoạch thì im lặng | **đã triển khai** (`system/lib/reconnect`, cả sáu runtime) |
| Câu trả lời của tác vụ đã delegate sống sót khi realtime trả lời một câu nói mới hơn | **đã triển khai** (os-server `IsTaskRun`) |
| Yêu cầu nói bị lỗi được thông báo thay vì im lặng | **đã triển khai** (`agent.voice_turn_failed`, debounce 20 s) |
| Ghi nhận phi ngôn ngữ trên Lamp: không nói "ừ hử" trong lúc người dùng nói (`HAL_BACKCHANNEL_FILLERS=`), chỉ một câu nối (bridge) nói ra sau 4 s (`HAL_REALTIME_FILLER_DELAY_S=4.0`), câu nối là từ ngữ ("One sec.", "Still thinking.") chứ không phải tiếng động | **đã triển khai** (`.env`, `fillers.go`) |
| Giọng của Google xuyên suốt trên Lamp: native audio của Gemini Live cho các lượt realtime, Gemini TTS (cùng một giọng) cho text của main agent | **đã triển khai** (`ROBOT.md` `tts_provider: gemini`) |
| Endpoint 1.0 s im lặng hoặc 0.6 s sau STT final trên Lamp | **đã triển khai** trong `.env`; đo lại trên thiết bị |
| Prewarm session đang bị park theo presence và gaze | **đã triển khai** (`prewarm_realtime` từ `presence.enter` và các mẫu gaze nhìn thẳng vào lamp) |
| Admission gate v2: bằng chứng (tên, cửa sổ hội thoại, câu hỏi đang chờ, đang nhìn thẳng, giọng quen) được gom một lần mỗi lượt, được log, gửi cho model dưới dạng một dòng `Addressed:`; chế độ `strict` loại bỏ tiếng nói hands-free không có bằng chứng trước khi tới bất kỳ model nào | **đã triển khai** (`HAL_ADDRESSED_GATE=hint` mặc định, `strict` cho thử nghiệm trên thiết bị) |
| "Tắt search" an toàn: một khối prompt định tuyến câu hỏi cần dữ kiện mới sang main agent khi `HAL_GEMINI_GOOGLE_SEARCH=false` | **đã triển khai** (`NO_SEARCH_PROMPT`) |
| Lời mở đầu khi uỷ thác (tuỳ chọn): model nói một câu ngắn về việc sắp làm, rồi gọi `delegate_to_main` trong cùng lượt | **đã triển khai** dưới dạng công tắc cho ngày thử trên thiết bị (`HAL_REALTIME_DELEGATE_PREAMBLE`, mặc định tắt) |
| Thinking level / model thường cho câu trả lời trực tiếp | thử nghiệm ngày thiết bị (`HAL_GEMINI_THINKING_LEVEL`, `HAL_GEMINI_LIVE_MODEL`) |
| Google Search bật so với tắt (độ trễ so với tra cứu trực tiếp) | A/B ngày thiết bị (`HAL_GEMINI_GOOGLE_SEARCH`) |
| Khoảng điếc sau trả lời: grace chờ tool gọi muộn kết thúc ngay khi câu trả lời đã phát xong và loa im 1 s, thay vì chạy đủ 6 s | **đã triển khai** (`HAL_REALTIME_GRACE_AFTER_PLAYBACK_S`) |
| Timeout idle của proxy cho phòng có người | dự kiến (liên nhóm); ping phía client đã có sẵn để thử nghiệm đối chiếu với nó |
| Delegate không chặn với inject kết quả | bị chặn bởi model: model extended-thinking đóng session (1007) khi nhận scheduled function response; phương án dự phòng là đường announcement, sẽ đo sau khi native audio đã bật |
| Live mode mặc định trên các thân máy có AEC phần cứng; quyết định codec cho Lamp Standard | quyết định sản phẩm (§8) |

## 7. Ngày thiết bị (phiên tiếp theo với một lamp)

1. Lấy `server.log` và `local/flow_events_*.jsonl`; chạy
   `python3 scripts/bench/voice_turns.py server.log` để có bảng baseline.
2. Mười lượt hội thoại, mười follow-up, mười tác vụ delegate, năm câu trả lời
   "yeah", năm clip TV/tiếng nền, với `HAL_AEC_DUMP_DIR` được đặt. Giữ lại các
   file WAV.
3. Lật từng cờ một, chạy lại cùng script, so sánh p50/p95:
   `HAL_GEMINI_THINKING_LEVEL`, `HAL_TTS_ELEVENLABS_WS=true`,
   `HAL_REALTIME_NATIVE_AUDIO=true`, `HAL_ENDPOINT_SILENCE_S=0.6`,
   `HAL_TURN_END_FALLBACK_S=1.5`, `HAL_REALTIME_NONBLOCKING_TOOL_GRACE_S=2`.
4. Từ log os-server, phân bố của `intent Jev decision … decision_ms`: bước phân
   loại đồng bộ đó (ngân sách `jev_intent.timeout_ms`, trần 3 s) đứng trước mọi
   lượt voice mà không rule cục bộ nào khớp. Đặt ngân sách ngay trên p95 đo được;
   không được cắt mù, vì một lần khớp sẽ xử lý lệnh thiết bị tại chỗ thay vì qua
   một lượt main agent 6–22 s.
5. Tiêu chí chấp nhận cho ngày đó: trả lời p50 ≤ 2 s ở lượt hội thoại, không
   bỏ rơi "yeah" nào sau câu hỏi, không câu trả lời tác vụ nào bị mute, mọi
   thất bại đều được nói ra.

## 8. Quyết định

Đã quyết (2026-10-08):

- **Danh tính giọng nói: Google.** Native audio của Gemini Live cho các lượt
  realtime và Gemini TTS, cùng một giọng, cho text của main agent. Không tổng
  hợp giọng lần thứ hai trên đường hội thoại; dòng "cho chọn giọng" trên landing
  page sẽ thay đổi.
- **Tốc độ trước hết.** Ở đâu một lựa chọn đánh đổi độ trễ lấy bất cứ thứ gì
  khác, chi phí độ trễ phải được nêu rõ và tốc độ thắng, trừ khi ảnh hưởng đến
  tính đúng đắn.
- **Không có filler nói ra.** Đang nghe và đang nghĩ được thể hiện bằng vòng
  đèn và đầu; một câu nối (bridge) nói ra phải là từ ngữ thật và chỉ sau 4 s
  chờ đợi.

Đề xuất, kèm lý do, để owner xác nhận:

- **Audio của Lamp Standard: đổi codec.** "Như nói chuyện với một người" đòi
  hỏi lamp phải nghe được trong lúc nó nói: ngắt lời, không có khoảng điếc sau
  câu trả lời, không có echo-skip. Với một mic USB và một loa USB rời, đó không
  phải vấn đề tinh chỉnh mà là vấn đề clock (trôi ±50–100 ppm đánh bại bộ lọc
  thích ứng; đo được ~6 dB khử echo), nên AEC phần mềm sẽ mãi chỉ là giải pháp
  nửa vời. Một linh kiện dùng chung clock với AEC trên chip (lớp XVF3800, loa
  được điều khiển từ board mảng mic) chính là thứ profile Pro đã chạy live mode
  trên đó. Cho đến khi SKU Standard thay đổi, ship half-duplex với mic có gate
  và tap-to-interrupt, và nói thẳng điều đó. Nếu BOM không thể đổi trước khi ra
  mắt, lên kế hoạch cho đợt sản xuất kế tiếp; đây là thay đổi phần cứng đơn lẻ
  có tác động UX lớn nhất.
- **Wake word: tắt theo mặc định, kèm gate bằng chứng.** Một người không cần
  mật khẩu; họ đáp lại khi bạn nhìn họ, gọi tên họ, đang dở câu chuyện với bạn,
  hoặc vừa hỏi bạn điều gì đó. Đó chính xác là bằng chứng mà gate thu thập. Cái
  giá là trả lời nhầm TV và người khác: không có gate nào, các hệ thống không
  dùng keyword chấp nhận nhầm hàng trăm câu nói mỗi giờ; với gating đa phương
  thức, Amazon giảm 80 % wake nhầm do tiếng nền. Vậy nên: mặc định tắt,
  `HAL_ADDRESSED_GATE=strict` trên Lamp Standard một khi một ngày log thiết bị
  cho thấy trả lời nhầm dưới một lần mỗi giờ trong phòng có TV (cho đến lúc đó
  dùng chế độ `hint`, mặc định an toàn hơn), và wake phrase luôn hoạt động như
  một override. Giữ "wake word bật" làm thiết lập cho phòng dùng chung.
- **Google Search: tắt trong session realtime trước mắt.** Số liệu từ cộng
  đồng đặt vòng quay của Gemini Live ở 1–1.5 s khi tắt tool so với 2–3.5 s khi
  bật Search, và trên thiết bị này câu trả lời có grounding đã được nói ra
  trước khi search trả về (#277). Với khối prompt no-search, câu hỏi cần dữ
  kiện mới đi sang main agent, chậm hơn nhưng đúng. Xác nhận bằng A/B ở §7;
  nếu chi phí đo được khi bật Search dưới 500 ms ở phần lớn các lượt, bật lại.

## 9. Nguồn nghiên cứu

Các bài học ở §5 đến từ một bản rà soát có trích nguồn (2026-10-08) được lưu
cùng ghi chú kỹ thuật: tài liệu và forum dành cho developer của OpenAI
(Realtime API, GPT-Live, hướng dẫn prompting), bài "Crossing the uncanny valley
of conversational voice" và TurnBench của Sesame, paper Moshi và các bài viết
về Unmute của Kyutai, tài liệu Hume EVI, tài liệu Live API của Google, Amazon
Science (end-pointing, tiếng nói hướng tới thiết bị, Conversation Mode), nghiên
cứu Apple ML, tài liệu và benchmark của Pipecat / LiveKit / Vapi / ElevenLabs /
Deepgram / Krisp, Full-Duplex-Bench, và tài liệu nghiên cứu về turn-taking
(Stivers 2009; Levinson & Torreira 2015; Katzenmaier 2004; nghiên cứu CUI'25 về
filler). Tuyên bố của nhà cung cấp và phép đo độc lập được đánh dấu rõ như vậy
ở §5; bản báo cáo không nằm trong repo này.
