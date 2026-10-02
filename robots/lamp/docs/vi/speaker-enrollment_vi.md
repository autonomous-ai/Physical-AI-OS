# Đăng ký giọng nói (Speaker Enrollment) — Tài liệu kỹ thuật

**Trạng thái: ĐÃ TRIỂN KHAI** (2026-04)

## Tổng quan

Lamp nhận diện người nói qua **WeSpeaker ResNet293** theo mặc định (vector nhúng 256 chiều, ONNX Runtime; server chọn được qua `AUDIO_EMBEDDER__MODEL`). Khi không nhận ra người nói, HAL lưu audio và tuỳ điều kiện sẽ yêu cầu AI agent đăng ký giọng nói. Đăng ký chỉ áp dụng **tự phục vụ** — mỗi người tự đăng ký giọng nói của mình.

## Kiến trúc

```
┌─────────────────────────────────────────────────────────────────────┐
│  HAL (Python, port 5001)                                         │
│                                                                     │
│  VoiceService._stream_session()                                     │
│    ├─ STT chuyển giọng nói → văn bản                                │
│    ├─ identify_and_decorate(transcript)                             │
│    │   ├─ audio_buffer → WAV → tiền xử lý tại thiết bị (cổng VAD)   │
│    │   │   └─ Mono→Resample→[HPF]→[NR]→VAD→[STOI]→RMS; loại clip kém rõ│
│    │   ├─ POST /audio-recognizer/embed  (preprocess=false)         │
│    │   │   └─ WeSpeaker ONNX → vector 256 chiều (chỉ lấy embedding) │
│    │   ├─ Bình chọn theo từng chunk so với embedding đã đăng ký     │
│    │   ├─ Khớp ≥ 0.5 cos gốc → "Speaker - Tên: transcript"          │
│    │   └─ Không khớp → _format_unknown_speaker_message()            │
│    │       ├─ _should_request_speaker_enroll() kiểm tra điều kiện   │
│    │       │   ├─ ≥ 10 từ trong transcript                          │
│    │       │   └─ ≥ 2 giây audio                                    │
│    │       ├─ ĐẠT → "Unknown Speaker: ... (audio save at <path>,   │
│    │       │          auto enroll ...)"                              │
│    │       └─ KHÔNG ĐẠT → "Unknown Speaker: ..." (không kèm yêu   │
│    │          cầu đăng ký)                                          │
│    └─ POST /api/sensing/event → Lamp (Go)                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Lamp (Go, port 5000)                                               │
│                                                                     │
│  Hai đường đi (cả hai gọi domain.AppendEnrollNudge):                │
│                                                                     │
│  1. Đường trực tiếp (handler.go)                                    │
│     └─ Agent rảnh → gửi thẳng tới OpenClaw                         │
│                                                                     │
│  2. Đường hàng đợi (service.go)                                     │
│     └─ Agent bận → xếp hàng → phát lại khi agent rảnh              │
│                                                                     │
│  AppendEnrollNudge(msg) — domain/voice.go:                          │
│    ├─ Kiểm tra: chứa "Unknown Speaker:" + "audio save at"          │
│    ├─ Cooldown: bỏ qua nếu < 5 phút kể từ lần nhắc trước          │
│    └─ Chèn: "[REQUIRED: Follow speaker-recognizer/SKILL.md ...]"   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  OpenClaw Agent                                                     │
│                                                                     │
│  speaker-recognizer/SKILL.md                                        │
│    ├─ Phát hiện tự giới thiệu ("I'm X", "tôi là X", "mình là X")  │
│    ├─ curl POST /speaker/enroll với wav_path + tên                  │
│    ├─ Hai lượt: hỏi "Bạn là ai?" → đăng ký với cả hai path        │
│    └─ Xác nhận: "Rất vui được biết bạn, Tên!"                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Chống spam — Bốn lớp bảo vệ

Bốn lớp ngăn agent hỏi "bạn là ai?" liên tục:

| Lớp | Vị trí | Điều kiện | Mục đích |
|-----|--------|-----------|----------|
| **Thời lượng audio** | HAL `_internal/speaker_decorate.py` | `duration_s < SPEAKER_MIN_AUDIO_S` (0.8s) | Bỏ qua nhận diện hoàn toàn cho audio quá ngắn |
| **Yêu cầu đăng ký** | HAL `_should_request_speaker_enroll()` | `≥ 10 từ VÀ ≥ 2s audio` | Không kèm instruction đăng ký đầy đủ cho câu ngắn (biến thể ngắn kèm gợi ý combine vẫn được gửi) |
| **Cooldown nhắc nhở phía Lamp** | Lamp `domain/voice.go` | `5 phút kể từ lần nhắc trước` | Không chèn SKILL.md instruction quá 1 lần mỗi 5 phút |
| **Cooldown theo voiceprint** | HAL `_internal/speaker_decorate.py` | `30 phút mỗi voiceprint_hash` (`HAL_ENROLL_NUDGE_COOLDOWN_S`) | Không lặp lại "hỏi tên user" cho cùng một cluster giọng lạ; gửi message `Unknown Speaker:` trần |

## Model & Embedding

| Thuộc tính | Giá trị |
|------------|---------|
| Model | WeSpeaker ResNet293-LM theo mặc định (`AUDIO_EMBEDDER__MODEL=resnet293`; lựa chọn khác `resnet34`, `ecapa-tdnn1024`, `campplus`) |
| Chiều embedding | 256 |
| Runtime | ONNX Runtime (CPU) trên perception-service (RunPod) |
| Endpoint | `POST {DL_BACKEND_URL}/hal/api/dl/audio-recognizer/embed` (`DL_SPEAKER_ENDPOINT`; prefix cũ `/lelamp/` vẫn được nginx ánh xạ) |
| Xác thực | Header `X-API-Key` |
| Timeout | 15 giây |

### Thuật toán nhận diện

1. Audio → tiền xử lý **tại thiết bị** trên HAL (`Mono → Resample → [HighPass] → [NoiseReduce] → VAD → [STOI] → RMS`). Clip không qua được cổng VAD/STOI/chất lượng sẽ bị loại ngay tại chỗ (coi như "không xác định") và **không gửi lên server**.
2. WAV đã làm sạch → `POST /audio-recognizer/embed` với `preprocess=false` **và `use_sliding_window=true`**; server bỏ qua tiền xử lý của nó và trượt các cửa sổ chồng lấn để trả embedding theo từng chunk `[M, 256]` (clip ≤ ~10 giây vẫn là một cửa sổ duy nhất)
3. Cosine similarity với tất cả embedding người nói đã đăng ký
4. Bình chọn theo chunk: mỗi chunk vote cho người khớp nhất
5. Người thắng = nhiều vote nhất (hoà thì so trung bình confidence)
6. `confidence ≥ SPEAKER_MATCH_COS` (0.5 cosine gốc) → khớp; ngược lại → không xác định

> **Enroll khác biệt:** bước đăng ký gọi cùng endpoint nhưng với **`use_sliding_window=false`**, nên server nhồi **nguyên** câu tham chiếu vào model một lần (một vector `[256]`, không chia cửa sổ/mean) — lưu thành một dòng mỗi WAV trong bank giọng nói. Khi recognize, các chunk truy vấn (đã chia cửa sổ) bỏ phiếu so với các vector enroll single-shot này (cả hai cùng không gian chuẩn hoá L2).

### Điều kiện nạp vào tầng mở rộng (extended tier)

Một lượt nói đã được nhận diện **có thể** giành một suất trong tầng **extended**
của người nói, để bank học được đặc tính âm thanh mà căn phòng thực sự tạo ra
thay vì chỉ có bản ghi lúc đăng ký. Được nhận diện là **chưa đủ**. Sáu cổng chặn
chạy theo đúng thứ tự dưới đây, và cổng nào trượt trước thì loại **cả lượt nói** —
không cứu vớt, không cắt lát, không nạp một phần.

| # | Cổng chặn | Từ chối khi | Vì sao |
|---|-----------|-------------|--------|
| 1 | **Đồng thuận tuyệt đối** | có bất kỳ chunk nào bầu cho người khác | Lượt nói được lưu thành MỘT vector cho toàn bộ câu nói, nên một lượt xác định bằng bỏ phiếu *đa số* có thể chứa người nói khác trong các chunk thua, và bản trộn đó thành một dòng truy hồi vĩnh viễn — mà trong vector đã lưu không còn dấu vết nào để phát hiện ra |
| 2 | **Chunk yếu nhất** | chunk thắng yếu nhất thấp hơn `HAL_SPEAKER_EXTEND_MIN_CHUNK_COS` | Chỉ đồng thuận thôi không chứng minh được gì: khi chỉ có **một người đã đăng ký**, ma trận chỉ có một cột, nên mọi chunk mặc nhiên bầu cho họ kể cả ở mức cos 0.2. Đây mới là cổng bắt được trường hợp phổ biến — một người khách trong phòng chưa đăng ký |
| 3 | **Anchor tự mang kết quả** | các dòng ghi danh không tự mang lại kết quả khớp (`HAL_SPEAKER_EXTEND_MIN_ANCHOR_COS`) | Kết quả khớp do tầng **extended** mang lại là bằng chứng về một lần đoán trước đó, không phải về con người. Nạp dựa trên nó sẽ để tầng extended tự bảo lãnh cho chính sự phình to của mình, khiến một mẫu xấu đẻ ra nhiều mẫu xấu. Neo vào mẫu ghi danh là thứ khiến nhiễm bẩn không nhân bản được |
| 4 | **Thời lượng** | giọng nói sau VAD ngắn hơn `SPEAKER_EXTEND_MIN_DURATION_SEC` | Quá ít thông tin người nói để đáng một suất vĩnh viễn. Đo trên waveform **đã làm sạch**, không phải lượt nói thô — caller nối nguyên một phiên mic (tới 30 giây) thành một WAV, nên ở lượt yên tĩnh thì phần lớn file là im lặng |
| 5 | **Biên (margin)** | người thắng dẫn trước người **không thắng** mạnh nhất ít hơn `SPEAKER_EXTEND_MIN_MARGIN_COS` | Một thế gần hoà giữa hai người đã đăng ký tuyệt đối không được ghi audio vào bank của bên nào |
| 6 | **Độ đa dạng** | cosine lớn nhất tới mẫu đã lưu vượt `SPEAKER_DIVERSITY_COS` | Mẫu này trùng lặp với mẫu đã có |

Cổng 1 và 2 là **no-op khi giọng nói sau VAD dưới ~10 giây**, lúc đó server chỉ
trả về một chunk: đồng thuận là 1/1, và chunk yếu nhất *chính là* confidence của
lượt nói, vốn đã vượt ngưỡng khớp rồi. Chúng chỉ kích hoạt ở các lượt dài — đúng
những lượt có thể chứa hai người.

**Hãy chuẩn bị tinh thần tỉ lệ extend ở các lượt dài sẽ giảm.** Đó là hiệu quả
mong muốn. Nếu nó giảm về 0, hãy kiểm tra cổng 2 với bản ghi thật trước khi nới
lỏng bất cứ thứ gì: tỉ lệ 0 nhiều khả năng nghĩa là `SPEAKER_MATCH_COS` sai, chứ
không phải cổng chặn sai.

Mỗi mẫu được nạp sẽ ghi kèm một file `.json` bên cạnh WAV, ghi lại nó đã qua
những cổng nào và dưới ngưỡng nào. Không có gì đọc file này lúc chạy — nó tồn tại
để lần đổi ngưỡng *sau này* có thể hỏi luật cũ đã nạp những mẫu nào. Các mẫu ghi
trước khi có cơ chế này không có bản ghi đó, và không thể dựng lại: phiếu bầu
từng chunk chưa bao giờ được lưu. Chúng sẽ tự đào thải qua giới hạn
`SPEAKER_MAX_EXTENDED_SAMPLES`.

Embedding của mẫu được tạo bằng **đúng lời gọi single-shot mà enroll dùng**
(`use_sliding_window=false`, nguyên câu nói, không chia cửa sổ, không lấy trung
bình), nên các dòng anchor và extended cùng một loại vector, và việc embed lại
một `ext_*.wav` trong lúc migration sẽ tái tạo đúng sidecar của chính nó. Các
mẫu ghi trước thay đổi này chứa trung bình các chunk cửa sổ trượt của bước nhận
diện và không có khoá `embedding_mode`; lần migration kế tiếp sẽ ghi lại chúng
theo kiểu single-shot.

> Đường ghi danh được **miễn trừ** một cách có chủ đích: audio lấy từ một cụm
> giọng trong lúc đăng ký là do chính người dùng xác nhận, nên nó bỏ qua toàn bộ
> các cổng chặn này.

### Tiền xử lý audio (tại thiết bị)

Pipeline lọc/VAD/chuẩn hoá trước đây chạy trong perception-service nay chạy trên HAL, ngay cạnh mic — cùng bộ processor, cùng thứ tự, được port sang `hal/drivers/voice/speaker_recognizer/audio_processors/` (khớp `AudioProcessorFactory` bên perception-service). Nhờ vậy audio bị loại không tốn băng thông và quyết định cổng nằm ngay tại thiết bị.

- **Chuỗi mặc định**: `MonoConverter → Resampler(16k) → VoiceActivityFilter(TEN-VAD) → SpeechIntelligibilityFilter(0.70) → RMSNormalizer(0.1)`. `HighPassFilter` và `NoiseReducer` có sẵn nhưng **tắt mặc định** (giống perception).
- **Cổng VAD** (TEN-VAD qua package vendored `hal/drivers/voice/ten_vad_lite/`): cắt phần không có tiếng ở đầu/cuối và loại clip khi VAD xoá hết speech, phần còn lại `< 0.5s`, hoặc tỉ lệ tiếng nói `< 0.25`. Clip bị loại sẽ raise `PreprocessRejected` → HAL trả "không xác định" khi recognize và bỏ qua mẫu khi enroll — đúng như hành vi khi perception trả HTTP 400 trước đây.
- **Vì sao TEN-VAD chứ không phải silero**: stage này chạy package torch `silero-vad` cho tới khi được thay bằng model ONNX FP32 ~300 KB của TEN-VAD, chạy trên numpy + onnxruntime (cả hai đều đã là dependency của HAL — chỉ dùng model gốc, không ship bản lượng tử hoá). Cùng tên class, cùng chữ ký constructor, cùng các lý do reject, nên phần còn lại của pipeline không đổi. Điểm được là **bỏ torch khỏi nhánh này**: import + nạp model tốn +43 MB thay vì +170 MB, khởi động nguội nhanh hơn ~27 lần, và onnxruntime có wheel aarch64 trong khi `libten_vad` dựng sẵn của TEN-VAD upstream không có bản Linux-arm64 nào. TEN-VAD chỉ hỗ trợ 16 kHz — điều mà `Resampler` phía trước đã bảo đảm.
- **Cổng chống dương tính giả** (silero không có): cổng **speaker-band** chỉ giữ các frame VAD nằm trong dải cao độ của chính clip, còn cổng **mức** loại các frame thấp hơn 20 dB so với mức tiếng nói của chính clip. Chúng quan trọng vì bộ lọc giữ *từ mẫu speech đầu tiên tới mẫu cuối cùng*, nên một dương tính giả muộn (tiếng cửa, tiếng gõ) sẽ kéo toàn bộ khoảng lặng phía trước vào clip. Cùng nhau, chúng nâng tỉ lệ phần được giữ thực sự là tiếng nói từ ~0.67 lên ~0.79, đổi lại recall giảm thật (0.98 → 0.76) — đánh đổi đúng cho bài toán nhận diện, nơi 2 giây sạch hơn 6 giây bẩn. Chúng giả định **mỗi clip chỉ có một người nói chính**: người nói nền bị loại (thường là điều mong muốn, vì sẽ làm nhiễu embedding), nhưng một người nói thứ hai thật sự mà nói nhỏ hơn cũng bị loại theo. Vì chúng cũng cắt phần không phải tiếng nói ở *bên trong* đoạn giữ lại, tỉ lệ tiếng nói giảm một cách máy móc — nên ngưỡng tỉ lệ tối thiểu đổi từ `0.4` xuống `0.25`. Đặt `HAL_SPEAKER_PROC_VAD_SPEAKER_BAND=false` và `HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB=` (rỗng) để chạy TEN-VAD thuần.
- **Cổng STOI** (`SpeechIntelligibilityFilter`, STOI SQUIM-OBJECTIVE không cần tham chiếu) — **mặc định tắt**, đặt `HAL_SPEAKER_PROC_ENABLE_STOI=true` để bật: chạy **sau VAD, trước RMS**. Chấm điểm clip đã cắt theo từng chunk 5 giây rồi lấy **trung bình**, sau đó loại khi STOI trung bình `< 0.70` (chunk NaN do im lặng cũng bị loại), raise `PreprocessRejected(reason="low_intelligibility")` → cùng đường audio-level reject như VAD (recognize → "không xác định", enroll → bỏ qua mẫu, giữ nguyên các mẫu đã có trên đĩa). Bộ ước lượng ONNX (~20 MB, tải về khi dùng lần đầu từ CDN vào `/root/local/models/squimm_stoi.onnx` — xem `audio_processors/model_store.py`, cùng quy ước với model pose/faceid — onnxruntime CPU với mem-arena tắt) nạp một lần dạng lazy singleton cùng TEN-VAD và chỉ chạy sau khi VAD đạt — tối đa một lần mỗi phát ngôn. Nếu không phân giải được weight (CDN không truy cập được / tên file lạ) thì bỏ qua cổng kèm cảnh báo (không crash).
- **Cờ server**: HAL gửi `preprocess=false`; `/embed` của perception chỉ để embed và nay cũng mặc định `preprocess=false` (HAL là caller duy nhất). Caller nào upload audio thô có thể truyền `preprocess=true` để server tự làm sạch.
- **Nhất quán**: enroll và recognize dùng chung một pipeline này, nên các đăng ký sau khi chuyển vẫn tự nhất quán. Giọng đã đăng ký dưới pipeline **server cũ** nên đăng ký lại nếu chất lượng khớp giảm.

### Theo dõi phiên bản model embedding & migration

Một embedding đã lưu chỉ so sánh được với embedding truy vấn do **cùng một** model server tạo ra. Nếu model embedding của perception-service bị đổi, mọi vector đã lưu trước đó âm thầm trở nên vô nghĩa khi so sánh — cosine vẫn ra một con số, nên lỗi biểu hiện là **khớp sai người**, không phải báo lỗi. HAL chặn việc này bằng cách đóng dấu định danh model lên từng hồ sơ và tính lại embedding khi định danh đổi. Vì mọi WAV đăng ký đều được giữ trên đĩa, đây là một tác vụ nền tự động — không ai phải thu âm lại.

- **Định danh model**: response `/audio-recognizer/embed` trả `embed_model_version` (và `/health` trả cùng giá trị dưới tên `audio_embedder_version`) — `<tên-model>:<sha256(trọng_số)[:12]>`, tính một lần khi model nạp. `<tên-model>` là giá trị config `AUDIO_EMBEDDER__MODEL` (`resnet293` / `resnet34` / `campplus` / `ecapa-tdnn1024`), ví dụ `resnet293:1a2b3c4d5e6f`. Hash file trọng số bắt được cả trường hợp **đổi checkpoint cùng số chiều** mà phép kiểm `embedding_dim` bỏ sót. Chỉ model được lấy vân tay; config tiền xử lý tại thiết bị **cố ý không** nằm trong đó.
- **Khi enroll**: HAL luôn lấy phiên bản mới nhất thấy được từ các lần gọi `/embed` của lần enroll đó và ghi vào `metadata.json` của giọng dưới khoá `embed_model_version` (đồng bộ vào registry).
- **Khi recognize**: sau khi embed truy vấn (làm mới phiên bản server đang biết), HAL so từng hồ sơ đã đăng ký với phiên bản đó. Hồ sơ có phiên bản **khác** bị **loại khỏi so khớp trong lượt đó** (nên trả về **"unknown"** thay vì match sai với vector model cũ, và đổi dim cũng không làm crash phép match), đồng thời châm một lần migration re-embed chạy **nền** — single-flight, trên daemon thread, nên bản thân lượt recognize **không chờ** re-embed. Hồ sơ còn khớp phiên bản vẫn nhận diện bình thường trong cùng lượt; hồ sơ bị loại tự trở lại bình thường sau khi migration nền re-embed xong.
- **Khi HAL khởi động lại**: một thread nền poll `/health` lấy `audio_embedder_version` hiện tại (thử lại vài lần để chờ server boot), quét metadata hồ sơ để tìm cái lỗi thời **trước khi** nạp model tiền xử lý nặng, rồi migrate các hồ sơ lỗi thời — để nhận diện đúng ngay từ turn đầu thay vì chờ một lần recognize phát hiện.
- **Migration (re-embed)**: với mỗi hồ sơ lỗi thời, HAL tính lại embedding cho **mọi** sample đã giữ — cả tầng anchor (`sample_*.wav`) lẫn extended (`extended_*.wav`) — dưới model mới (cùng đường `_prepare_wav_for_embedding` → `/embed` như enroll) và **ghi đè nguyên tử** file **sidecar** `.npy` của từng sample (file tạm + đổi tên); sample nào bị gate hôm nay từ chối thì xoá sidecar để không còn vector model cũ sót lại qua một lần đổi checkpoint cùng số chiều. Sau đó cập nhật `embed_model_version` / `embedding_dim` / `updated_at` và vô hiệu cache bank. Có khoá để mỗi lần chỉ chạy một migration, và một lần enroll cùng hồ sơ chạy song song giờ được **tuần tự hoá** với migration — đoạn commit ghi đĩa của enroll lấy chung khoá per-user đó. Khoá này quan trọng vì migration ghi đè sidecar (và `metadata.json`) của hồ sơ trong khi một lần enroll cùng người có thể đang ghi chính các file đó — không có khoá thì hai đoạn commit đĩa có thể xen kẽ và để lại tập mẫu không nhất quán; còn `metadata.json` — file chung duy nhất giữa hai bên ghi — có các số đếm được suy lại từ đĩa khi đọc. Chỉ đoạn ghi đĩa bị khoá — các lời gọi mạng embedding ở cả hai phía chạy ngoài khoá — nên tệ nhất enroll chỉ chờ đúng lần re-embed của một hồ sơ đó, không phải cả mẻ migration. Commit chỉ chạy **sau khi** mọi sample đã embed xong, nên nếu server mất giữa chừng (`EmbeddingAPIUnavailableError`) thì **dừng** sạch sẽ với hồ sơ còn nguyên trên model cũ và thử lại ở lần recognize hoặc restart sau.
- **Hồ sơ không migrate được thì để nguyên trạng thái stale, KHÔNG bao giờ xoá.** Một hồ sơ HAL không re-embed được — vì **không còn WAV nguồn** (hồ sơ legacy chỉ có `embedding.npy`, hoặc WAV bị xoá), hoặc vì **mọi WAV đã giữ đều bị gate hôm nay từ chối** — sẽ được để **stale** (loại khỏi match) cho tới khi người đó đăng ký lại, chứ không xoá. Trường hợp bị từ chối hết gần như luôn là do gate siết chặt hơn (các WAV này đã pass gate lúc enroll dưới config cũ) — một thay đổi có thể đảo ngược — nên hồ sơ tự re-migrate lại ngay khi có một sample pass gate; còn `embedding.npy`/WAV đã lưu có thể là bản sao duy nhất của lần đăng ký, nên chỉ đổi version thì tuyệt đối không được huỷ nó. Hồ sơ stale là vô hại — nó bị lọc khỏi match và chỉ tốn một phép kiểm tra rẻ mỗi lượt.

### Chất lượng đăng ký

1. Mỗi file WAV → tiền xử lý tại thiết bị (như trên) → embedding qua perception-service (`preprocess=false`, `use_sliding_window=false` → một vector nguyên câu mỗi mẫu)
2. Lọc theo ngưỡng consistency `0.7` (cosine similarity giữa các mẫu)
3. Tổng hợp embedding còn lại qua trung bình có trọng số
4. Lưu vector chuẩn hoá L2 tại `/root/local/users/{tên}/voice/embedding.npy`

### Theo dõi cụm giọng lạ (`voiceprint_hash`)

Mọi giọng lạ được gom cụm local để server biết "đây là cùng một người đã nghe cách đây 3 phút" mà không cần backend hỗ trợ. Cho phép agent gộp nhiều câu ngắn thành 1 lần enroll.

1. Sau khi embedding audio, recognizer tổng hợp embedding theo chunk thành 1 vector chuẩn hoá L2.
2. So với các centroid cụm stranger đã lưu (cosine similarity).
3. Match ≥ `SPEAKER_MATCH_COS` (mặc định `0.5` raw — **cùng** ngưỡng với khớp known-speaker; không có ngưỡng riêng cho người lạ) → dùng lại label `voice_N`, và nếu câu nói bổ sung điều gì mới thì thêm nó thành một hàng nữa. Một cụm giữ **nhiều hàng**, không phải một centroid trung bình, giới hạn bởi `SPEAKER_MAX_CLUSTER_SAMPLES` (mặc định `3`).
4. Không match → tạo label mới `voice_{counter}`, thêm centroid vào state trên đĩa.
5. Giới hạn `HAL_MAX_VOICE_STRANGERS` (mặc định `50`) — evict oldest khi vượt; eviction xoá **cả** hàng centroid **lẫn** thư mục WAV `voice_N/` của cụm đó trên đĩa (cụm bị evict không bao giờ match lại được nữa nên giữ folder là rác đĩa).
6. Hash được:
   - trả trong response recognize dưới field `voiceprint_hash: "voice_N"` (null cho known speaker)
   - gắn vào message nudge dạng tag `[voice:voice_N]` để skill đối chiếu qua các turn
   - dùng để group WAV vào subdir (xem Lưu trữ)

**Đổi model thì xoá sạch cluster stranger.** Centroid stranger chỉ so được với query của **cùng** model embedding — nên khác với profile đã enroll (được *re-embed* từ WAV giữ lại), toàn bộ store stranger bị **xoá** khi model đổi. Store được dán nhãn version model đã dựng nó (`voice_strangers/version.txt`); trước mỗi lần so, HAL **chỉ giữ store khi chứng minh được cùng model** — biết version server hiện tại, stamp của store **bằng** nó, **và** dim lưu bằng dim query. Còn lại — **thiếu** stamp, **khác** stamp, hoặc **khác** dim — đều không chứng minh được centroid do model hiện tại tạo, nên HAL bỏ centroid in-memory, xoá `embeds.npy`/`labels.npy` và mọi thư mục WAV `voice_N/` trên đĩa, rồi dán nhãn lại. (Store chưa stamp **không bao giờ được mặc định** là hiện tại: swap checkpoint cùng chiều dưới model khác sẽ lọt.) Khi server **không** báo version, HAL lùi về guard chỉ theo dim. `_stranger_counter` **giữ đơn điệu** để `voice_N` mới không đụng thư mục cũ còn sót. Chọn xoá (không re-embed) là có chủ đích: stranger ẩn danh và ngắn hạn, re-embed cụm vứt đi không đáng chi phí network.

**Trim silence cuối**: trước khi WAV đi qua embedding API, buffer speaker-ID được cắt tại frame speech cuối cùng + 200ms tail. Nếu không, câu 3s sẽ thành ~5.5s với ~45% silence, làm loãng embedding. Chỉ ảnh hưởng path speaker-ID — STT vẫn nhận đủ stream.

## Cấu hình

| Tham số | Mặc định | Biến môi trường | Mô tả |
|---------|----------|-----------------|-------|
| Ngưỡng khớp | 0.5 | `SPEAKER_MATCH_COS` | Cosine **gốc** tối thiểu để khớp; cũng dùng để ghép cặp clip trong lô enroll nhiều mẫu (trước là `SPEAKER_MATCH_THRESHOLD` = 0.75 scaled; `raw = 2 × scaled − 1`) |
| Độ đa dạng | 0.7 | `SPEAKER_DIVERSITY_COS` | Trên mức này lượt nói trùng với mẫu đã lưu → không giữ. Đo độ dư thừa, không phải danh tính — phải nằm trên ngưỡng khớp |
| Số mẫu extended tối đa | 3 | `SPEAKER_MAX_EXTENDED_SAMPLES` | Mẫu tự thu cho mỗi user. Cap an toàn: truy hồi là max-over-rows nên thêm hàng sẽ nâng điểm của mọi speaker |
| Số mẫu cụm tối đa | 3 | `SPEAKER_MAX_CLUSTER_SAMPLES` | Số hàng giữ cho mỗi cụm giọng lạ |
| Thời lượng embed tối đa | 20s | `HAL_SPEAKER_MAX_EMBED_AUDIO_S` | Clip speaker-ID đã làm sạch bị cắt còn 20 giây đầu trước khi gọi `/audio-recognizer/embed`. Clip enroll (≤ 15 giây) không bị ảnh hưởng; server cũng cắt ở 30 giây nên clip dài không thể làm TensorRT build lại engine (#555) |
| Thời lượng tối thiểu để mở rộng | 2.0s | `SPEAKER_EXTEND_MIN_DURATION_SEC` | Lượt nói cần chừng này **giọng nói sau VAD** mới được một suất extended. Đo trên waveform đã làm sạch, nên im lặng trong một phiên mic dài không được tính |
| Biên tối thiểu để mở rộng | 0.05 | `SPEAKER_EXTEND_MIN_MARGIN_COS` | ...và phải dẫn trước người **không thắng** mạnh nhất ít nhất bằng này. Là `inf` khi không có ai khác đã đăng ký — lúc đó thật sự không có á quân, và đó là lý do cổng 3 tồn tại |
| Bắt buộc đồng thuận khi mở rộng | bật | `HAL_SPEAKER_EXTEND_REQUIRE_UNANIMOUS_CHUNKS` | Mọi chunk phải bầu cho người thắng. Không tác dụng khi dưới ~10 giây giọng nói (chỉ một chunk) |
| Cosine chunk tối thiểu để mở rộng | _(= ngưỡng khớp)_ | `HAL_SPEAKER_EXTEND_MIN_CHUNK_COS` | Chunk thắng **yếu nhất** phải vượt mức này. Mặc định bằng `SPEAKER_MATCH_COS`, vốn đã chặt hơn cổng ở mức lượt nói — cổng đó chỉ xét trung bình |
| Cosine anchor tối thiểu để mở rộng | _(= ngưỡng khớp)_ | `HAL_SPEAKER_EXTEND_MIN_ANCHOR_COS` | Các dòng ghi danh phải tự mang lại kết quả khớp. Mặc định bằng `SPEAKER_MATCH_COS`, tức đúng nghĩa "chỉ riêng anchor cũng đã nhận ra người này" |
| Timeout API | 15s | `SPEAKER_EMBEDDING_API_TIMEOUT_S` | Timeout HTTP cho embedding API |
| Audio tối thiểu cho nhận diện | 0.8s | `HAL_SPEAKER_MIN_AUDIO_S` | Bỏ qua nhận diện dưới ngưỡng này |
| Số từ tối thiểu cho nudge đăng ký | 10 | Hardcoded trong `_should_request_speaker_enroll()` | Cổng số từ transcript |
| Thời lượng tối thiểu cho nudge đăng ký | 2.0s | Hardcoded trong `_should_request_speaker_enroll()` | Cổng thời lượng audio |
| Cooldown nhắc nhở phía Lamp | 5 phút | Hardcoded trong `domain/voice.go` | Không inject SKILL instruction toàn cục quá 1 lần/5 phút |
| Cooldown nhắc nhở theo voiceprint | 30 phút | `HAL_ENROLL_NUDGE_COOLDOWN_S` | Không hỏi lại tên cho cùng cluster voiceprint |
| Ngưỡng match voice stranger | _(dùng chung)_ | `SPEAKER_MATCH_COS` | Dùng lại ngưỡng khớp known-speaker để gom giọng lạ vào `voice_N` đã có — không có knob riêng |
| Số voice stranger tối đa | 50 | `HAL_MAX_VOICE_STRANGERS` | Giới hạn cluster; evict oldest khi vượt |
| Thư mục voice strangers | `/root/local/voice_strangers` | `HAL_VOICE_STRANGERS_DIR` | Persist embedding cluster (tồn tại qua reboot) |
| Bật/tắt nhận diện giọng nói | true | `HAL_SPEAKER_RECOGNITION_ENABLED` | Công tắc tổng (mặc định bật; gate theo capability `audio`) |

### Cấu hình tiền xử lý tại thiết bị

Khớp giá trị mặc định của `AudioProcessorSetting` bên perception; override qua env (đều có tiền tố `HAL_SPEAKER_PROC_`).

| Tham số | Mặc định | Env var | Mô tả |
|---------|----------|---------|-------|
| Sample rate đích | 16000 | `HAL_SPEAKER_PROC_TARGET_SR` | Đích của Resampler |
| Mono | bật | `HAL_SPEAKER_PROC_ENABLE_MONO` | Trộn về mono |
| Resample | bật | `HAL_SPEAKER_PROC_ENABLE_RESAMPLE` | Resample về SR đích |
| High-pass | tắt | `HAL_SPEAKER_PROC_ENABLE_HIGH_PASS` / `..._HIGH_PASS_CUTOFF_HZ` (80.0) | Lọc cao tần Butterworth |
| Noise reduce | tắt | `HAL_SPEAKER_PROC_ENABLE_NOISE_REDUCE` / `..._NOISE_STATIONARY` | `noisereduce` (import lazy) |
| VAD | bật | `HAL_SPEAKER_PROC_ENABLE_VAD` | Cổng TEN-VAD |
| VAD min duration | 0.5s | `HAL_SPEAKER_PROC_VAD_MIN_DURATION_SEC` | Loại nếu audio sau strip ngắn hơn |
| VAD min voice ratio | 0.25 | `HAL_SPEAKER_PROC_VAD_MIN_VOICE_RATIO` | Loại nếu tỉ lệ tiếng nói thấp hơn. Trước đây là 0.4 với silero — các cổng chống dương tính giả chia nhỏ segment nên làm tỉ lệ này giảm |
| VAD ngưỡng xác suất tiếng nói | 0.5 | `HAL_SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD` | Ngưỡng onset TEN-VAD (offset = −0.15); cao hơn thì cắt khoảng lặng đầu/cuối mạnh hơn. Trước đây là 0.6 với silero; điểm vận hành đo được của TEN-VAD là 0.45–0.5, và các cổng bên dưới nay đã đảm nhận việc cắt đó |
| VAD speaker band | bật | `HAL_SPEAKER_PROC_VAD_SPEAKER_BAND` | Chỉ giữ frame VAD nằm trong dải cao độ của chính clip |
| VAD mức giảm tối đa | 20.0 dB | `HAL_SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB` | Loại frame VAD thấp hơn mức tiếng nói của chính clip quá ngần này; **chuỗi rỗng để tắt** |
| Cổng STOI | **tắt** | `HAL_SPEAKER_PROC_ENABLE_STOI` | Cổng chất lượng SQUIM-OBJECTIVE (sau VAD, trước RMS). Mặc định tắt: nó loại nhầm người nói thật đủ thường xuyên để lượt thoại mất luôn người nói — tệ hơn là một kết quả nhận dạng độ tin cậy thấp. Bật lên khi phòng ồn. |
| Đường dẫn model STOI | `/root/local/models/squimm_stoi.onnx` | `HAL_SPEAKER_PROC_STOI_MODEL_PATH` | Bộ ước lượng ONNX (~20 MB), tải từ CDN khi dùng lần đầu; bỏ qua cổng nếu không phân giải được |
| Ngưỡng STOI | 0.70 | `HAL_SPEAKER_PROC_STOI_THRESHOLD` | Loại nếu STOI trung bình dưới ngưỡng này |
| Chunk STOI | 5.0s | `HAL_SPEAKER_PROC_STOI_CHUNK_SEC` | Độ dài chunk chấm điểm, rồi lấy trung bình |
| RMS normalize | bật | `HAL_SPEAKER_PROC_ENABLE_RMS_NORMALIZE` / `..._RMS_TARGET` (0.1) | Chuẩn hoá độ lớn cố định |

### Debug tracing (tạm thời)

`speaker_recognizer.py` có sẵn một bộ tracer chẩn đoán độc lập, đánh dấu `SPEAKER-DEBUG` xuyên suốt file, dùng để tinh chỉnh ngưỡng nhận diện trên audio thật. **Mặc định TẮT (an toàn cho production) — đặt `HAL_SPEAKER_DEBUG=true` để bật khi phát triển**, và nên xoá hẳn trước khi deploy chính thức. `grep -n "SPEAKER-DEBUG"` sẽ ra mọi dòng thuộc về nó; không đụng tới module hay file config nào khác.

Mỗi lần gọi `recognize()` / `enroll()` sẽ ghi ra một thư mục:

```
<root>/recognize/<ts>_<class>_<confidence>/     class = tên đã đăng ký | stranger-<N> | unknown
<root>/recognize/<ts>_FAIL-<reason>/            no-voice | low-voice | low-stoi | too-short | server-error | …
<root>/enroll/<ts>_<norm>_<cohesion>/           cohesion = sim trung bình của các mẫu giữ lại so với centroid
<root>/enroll/<ts>_FAIL-<reason>/
```

chứa `input.wav` (audio thô) cùng `preprocessed.wav` (sau VAD/STOI/RMS — chính là audio đã upload) / `sample_new_NN.wav`, các embedding dạng `.npy`, `result.json`, và `profile.json` (chỉ gồm độ trễ + bộ nhớ — xem bên dưới). Mỗi lần recognize ghi thêm khối `preprocessing` (thời lượng/RMS sau khi làm sạch, điểm STOI mà clip đã đạt, và ngưỡng nó vượt qua) để phân biệt "audio kém" với "nhận nhầm người"; clip bị cổng loại sẽ tạo thư mục `FAIL-<reason>` với `preprocessing_reject` chứa lý do có cấu trúc kèm số đo.

Khi bị cổng loại, trace còn ghi thêm **audio mà chuỗi xử lý đã tạo ra ngay tại thời điểm bị từ chối** — `after_<stage>.wav`, đặt tên theo stage cuối cùng chạy được, kèm khối `preprocessing_partial` (stage đó, stage đã từ chối, thời lượng/RMS/sample-rate). Đây chính là mục đích của nó: trước kia thư mục `FAIL-low-stoi` chỉ chứa `input.wav` thô, nên không có cách nào nghe được **đầu ra của TEN-VAD** mà cổng STOI thực sự chấm điểm — tức đúng cái clip mà quyết định loại nói về. Nay nó nằm ở `after_ten_vad.wav`. Tương tự, clip bị VAD loại sẽ có `after_resample.wav` (phần chạy trước đó). Thư mục `FAIL-no-valid-samples` của enroll cũng có tương ứng cho từng mẫu: `sample_NN_input.wav` + `sample_NN_after_<stage>.wav`, kèm lý do loại có cấu trúc của từng mẫu trong `per_sample_errors`.

Với recognize, file JSON mang **toàn bộ** diễn giải quyết định — không chỉ top-3 `candidates` mà API trả về, mà còn `speaker_summary` (số vote + sim trung bình/lớn nhất cho *mọi* người đã đăng ký, kể cả người 0 vote) và `per_chunk_scores` (từng chunk so với mọi người, kèm người mà chunk đó vote). Cùng ma trận đó được lưu ở `chunk_scores.npy` (`[chunks × speakers]`, cột theo thứ tự `enrolled_speakers`). Giọng lạ còn ghi thêm điểm khớp cụm stranger và cụm nào gần nhất.

#### Hồ sơ độ trễ, CPU + bộ nhớ

Mỗi thư mục trace còn chứa **`profile.json`** — thời gian thực thi, CPU và bộ nhớ theo từng stage, để quy trách nhiệm một lượt chậm hoặc ngốn bộ nhớ về đúng một stage thay vì cả pipeline. Nó nằm ở file riêng thay vì trộn vào `result.json` — file đó vốn đã dày đặc thông tin quyết định nhận diện: hai loại dữ liệu này được đọc vì mục đích khác nhau, gộp chung thì cái nọ lấp cái kia. Ngoài ra nó vẫn đi kèm tracer sẵn có: cùng thư mục, cùng công tắc, không thêm env var, chỉ bật khi `HAL_SPEAKER_DEBUG=true`. Một dòng tóm tắt cũng được ghi ra log (`SPEAKER-DEBUG profile [recognize]: total=… preprocess.stoi_gate=…ms/…%cpu/+…MB …`).

Các stage tạo thành **cây**: stage mở bên trong một stage khác trở thành con của nó, nên quan hệ bao hàm nằm ở cấu trúc chứ không phải ở quy ước đặt tên. Cộng cấp ngoài cùng sẽ ra tổng của cả lượt gọi mà không đếm trùng, và `self_ms` của mỗi node là thời gian riêng của nó trừ đi phần của các con — tức phần keo dán của cha, không phải công việc của con.

```
decode_input                đọc base64/file + chuẩn hoá WAV về 16 kHz mono
preprocess                  toàn bộ chuỗi xử lý on-device
├─ decode_wav / encode_wav    WAV bytes ↔ waveform float32, kèm bước bọc base64
├─ processor_init             dựng/khởi động lazy — lần đầu nạp TEN-VAD + ONNX STOI
├─ mono / resample / high_pass / noise_reduce / rms_normalize
├─ ten_vad                  ← stage TEN-VAD (các trace trước lần thay đổi này ghi là `silero_vad`)
└─ stoi_gate                ← cổng chất lượng STOI
embed_api                   lời gọi embedding
├─ request                  ← bản thân vòng gọi HTTP
└─ decode                     parse phản hồi + chuẩn hoá L2
load_enrolled / match_vote / stranger_cluster / save_input_wav
```

**Bộ nhớ.** RSS được lấy mẫu bằng một thread nền (~20 ms) suốt vòng đời lượt gọi, và mỗi stage báo cáo **đỉnh trong đúng cửa sổ của nó**. Chỉ đo hai đầu mút — đọc RSS lúc vào, đọc lại lúc ra — là sai với pipeline này: RSS chỉ thay đổi khi allocator xin thêm trang từ OS hoặc trả lại, nên stage nào cấp phát rồi giải phóng ngay trong cửa sổ của mình sẽ báo `0.00`, còn stage chạy đúng lúc một vùng cấp phát trước đó được giải phóng lại báo chi phí *âm*. Vì vậy mỗi stage giữ ba số:

| Trường | Ý nghĩa |
|--------|---------|
| **`rss_peak_delta_mb`** | đỉnh trong stage trừ RSS lúc vào — **đây là số về bộ nhớ cần đọc**. Không bị mất khi cấp phát rồi giải phóng. Với stage lặp lại, đây là lần tệ nhất chứ không phải tổng |
| `rss_end_delta_mb` | lúc ra trừ lúc vào — phần stage *giữ lại*. Âm là hợp lệ khi trang được trả về OS |
| `rss_peak_mb` / `rss_after_mb` | RSS đỉnh / RSS cuối trong stage |

**CPU.** `cpu_ms` là thời gian CPU của tiến trình, nhờ đó bắt được các thread pool intra-op của ONNX/torch — chính phần khiến `stoi_gate` đắt. `cpu_pct` là `cpu_ms/ms×100`, nên **>100% nghĩa là dùng nhiều hơn một core** và **~0% nghĩa là đang bị chặn chứ không phải đang làm việc** (`embed_api.request` nên gần bằng 0 — nó đang chờ mạng). `thread_cpu_ms` chỉ tính riêng thread gọi, nên chênh lệch giữa nó và `cpu_ms` xấp xỉ phần các pool đã làm. Cấp ngoài cùng có thêm `cpu_count` để diễn giải được con số >100%.

Vài điểm khác cần biết:

- **Stage bị loại vẫn được đo.** Clip bị VAD hay STOI loại sẽ tạo thư mục `FAIL-…` mà `profile.json` cho thấy hai cổng đó tốn bao nhiêu trước khi từ chối — một lần loại vẫn phải trả đúng chi phí suy luận như một lần cho qua.
- **Enroll cộng dồn.** Nó chạy preprocess + embed một lần cho mỗi mẫu, nên `ms` của các stage dùng chung được cộng lại, kèm `calls` và `ms_max` (cả hai được bỏ qua khi stage chỉ chạy đúng một lần).
- **RSS và CPU đều tính theo tiến trình.** Một thread HAL khác cấp phát hoặc ngốn CPU trong lúc stage đang chạy sẽ rơi vào số liệu của stage đó — không cách đo dựa trên RSS nào tránh được điều này. Hãy đọc bộ nhớ của một stage như một cận trên, và ưu tiên nhìn xu hướng qua nhiều lượt gọi.
- **`rss_source` quyết định ý nghĩa các con số.** `psutil` / `statm` (Linux trên thiết bị) là RSS hiện tại — trường hợp chính xác. `rusage` — phương án dự phòng trên macOS không có psutil — là mức đỉnh không thể giảm, nên `rss_end_delta_mb` ở đó sẽ bị thổi phồng.
- Bộ nhớ đo là RSS của tiến trình, không phải Python heap: các phiên ONNX của TEN-VAD và STOI cấp phát ngoài heap, nơi `tracemalloc` không thấy gì.

| Tham số | Mặc định | Env var | Mô tả |
|---------|----------|---------|-------|
| Debug tracing | **tắt** | `HAL_SPEAKER_DEBUG` | Đặt `true` để bật (áp dụng cho **cả** trace lẫn profile). Chỉ đọc một lần lúc khởi tạo — đổi xong phải restart HAL |
| Thư mục output | `speaker_logs/` cạnh `speaker_recognizer.py` | `HAL_SPEAKER_DEBUG_DIR` | Tự chuyển sang thư mục temp nếu source tree chỉ đọc (khi deploy lên thiết bị) |
| Số entry tối đa | 1000 | `HAL_SPEAKER_DEBUG_MAX_ENTRIES` | Giới hạn thư mục theo từng loại, xoá cũ nhất; `0` = không giới hạn |

Thư mục output mặc định đã được git-ignore — tuyệt đối không commit dữ liệu trace. Tracer tự nuốt mọi lỗi của chính nó, nên một lần trace hỏng không bao giờ làm hỏng luồng nhận diện.

## Lưu trữ

```
/root/local/users/{tên}/
  metadata.json                      # Danh tính chung (telegram, display_name)
  voice/
    embedding.npy                    # Vector chuẩn hoá L2 [256]
    metadata.json                    # num_samples, dim, timestamps,
                                     #   embed_model_version
    sample_{origin}_{ts}_{uuid}.wav  # Các mẫu đăng ký (16kHz mono)

/tmp/hal-unknown-voice/
  incoming_{ts}_{uuid}.wav           # Audio known-speaker (phẳng)
  voice_{N}/
    incoming_{ts}_{uuid}.wav         # Audio unknown — gom theo cụm voiceprint

/root/local/voice_strangers/
  embeds.npy                         # Centroid các cluster stranger [N, 256] (bị xoá khi store bị wipe)
  labels.npy                         # Label cluster ["voice_1", "voice_2", ...] (bị xoá khi store bị wipe)
  counter.npy                        # Counter tăng cho label mới (giữ qua wipe)
  version.txt                        # Version model embedding đã dựng centroid; lệch → wipe
```

## API Endpoints (HAL, port 5001)

| Method | Path | Mô tả |
|--------|------|-------|
| `POST` | `/speaker/enroll` | Đăng ký giọng nói từ wav_paths + tên |
| `POST` | `/speaker/record-enroll` | Thu từ mic của thiết bị (`arecord`, `duration_sec` 1–60, mặc định 15) rồi đăng ký bản thu đó |
| `POST` | `/speaker/recognize` | Nhận diện người nói từ wav_path |
| `POST` | `/speaker/identity` | Liên kết Telegram với profile giọng nói |
| `POST` | `/speaker/remove` | Xoá profile giọng nói theo tên |
| `POST` | `/speaker/reset` | Xoá tất cả profile giọng nói |
| `GET`  | `/speaker/list` | Liệt kê người nói đã đăng ký |

### Hợp đồng lỗi (error contract)

`/speaker/enroll` phân biệt hai loại thất bại:

| HTTP | Khi nào | Hành vi skill |
|------|---------|---------------|
| `400` | Audio bị reject (quá ngắn, im lặng, VAD không tìm thấy speech, STOI trung bình dưới ngưỡng → `low_intelligibility`, perception-service trả 4xx) | Yêu cầu user thu lại / nói rõ hơn |
| `503` | Embedding service không reachable (network, 5xx, response malformed) | Báo user thử lại sau — disk không bị thay đổi gì |

`/speaker/recognize` **không bao giờ** trả 5xx khi embedding API chết — nó trả `200` với `{name: "unknown", error: "<lý do>"}` để skill tự xử graceful. Chỉ lỗi input (thiếu WAV, base64 sai) mới trả `400`.

### Quyền sở hữu mic trong lúc record-enroll

ALSA capture là độc chiếm — chỉ một tiến trình được giữ mic. Nên `/speaker/record-enroll` dừng voice pipeline, thu bằng `arecord`, rồi khởi động lại pipeline trong khối `finally` của chính nó.

Bất kỳ đường nào khác khởi động pipeline trong lúc bản thu đang chạy sẽ cướp mất capture device, và **cả hai** bên cùng chết với `audio open error: Device or resource busy`: enroll trả `500`, còn voice loop cũng chết vì đúng lỗi đó. Vì vậy mọi caller đều đi qua `state.start_voice_service(reason)` — hàm này từ chối (và log lý do) khi `state._enrolling` đang bật. Ngoại lệ duy nhất là bước khôi phục của chính record-enroll: nó sở hữu lệnh stop và chạy sau khi cờ đã được xoá.

## Vị trí code chính

| Thành phần | File | Hàm/Struct |
|------------|------|------------|
| STT → nhận diện người nói | `hal/drivers/voice/_internal/speaker_decorate.py` | `identify_and_decorate()` |
| Cổng đăng ký | `hal/drivers/voice/_internal/speaker_decorate.py` | `_should_request_speaker_enroll()` |
| Định dạng message | `hal/drivers/voice/_internal/speaker_decorate.py` | `_format_unknown_speaker_message()` |
| Bộ nhận diện giọng nói | `hal/drivers/voice/speaker_recognizer/speaker_recognizer.py` | `SpeakerRecognizer` |
| Gate sở hữu mic | `hal/app_state.py` | `start_voice_service()` |
| Route thu + đăng ký | `hal/routes/speaker.py` | `speaker_record_enroll()` |
| Chèn instruction + cooldown | `system/domain/voice.go` | `AppendEnrollNudge()` |
| Đường trực tiếp | `system/server/sensing/delivery/http/handler.go` | `PostEvent()` |
| Đường hàng đợi/phát lại | `runtimes/openclaw/service_events.go` | `drainPendingEvents()` |
| Skill agent | `skills/speaker-recognizer/SKILL.md` | — |
| Model embedding | `integrations/perception-service/src/core/perception/audio/predictors/` (`resnet34.py`, `resnet293.py`, `ecapa_tdnn.py`, `campplus.py`) | `ResNet293Embedder` (mặc định), `ResNet34Embedder`, `EcapaTdnn1024Embedder`, `CamPPlusEmbedder` — chọn qua env `AUDIO_EMBEDDER__MODEL` (`resnet293` \| `resnet34` \| `ecapa-tdnn1024` \| `campplus`) |
| Endpoint embedding | `integrations/perception-service/src/dlserver/routes/audio.py` | `embed_audio()` |
| Cấu hình | `hal/config.py` | Các hằng số `SPEAKER_*` |

## Ví dụ luồng message

### Câu ngắn (bị chặn)
```
User nói: "hey" (2 từ, 0.9s audio)
→ HAL: bỏ qua nhận diện (< SPEAKER_MIN_AUDIO_S)
→ Message: "hey" (không prefix, không instruction đăng ký)
```

### Câu trung bình (nhận diện nhưng không nudge đăng ký)
```
User nói: "bật đèn lên đi" (4 từ, 3s audio)
→ HAL: nhận diện → unknown, _should_request_speaker_enroll(4 từ, 3s) = false
→ Message: "Unknown Speaker: bật đèn lên đi"
→ Lamp: không có "audio save at" → AppendEnrollNudge giữ nguyên
→ Agent: phản hồi bình thường, không hỏi user là ai
```

### Gộp nhiều turn ngắn (cùng cluster giọng)
```
Turn 1: "nice to meet you today. Okay." (5 từ)
→ HAL: recognize → unknown, voiceprint_hash=voice_5
→ WAV chuyển vào /tmp/hal-unknown-voice/voice_5/incoming_A.wav
→ Message: "Unknown Speaker: [voice:voice_5] nice to meet you today. Okay. (audio saved at ..._A.wav. Note: audio is too short for single enrollment. If prior turns tagged the same voice_5, combine their saved paths...)"
→ Agent: hỏi "Cho mình biết tên bạn với?"

Turn 2: "I'm Alex." (2 từ)
→ HAL: voiceprint_hash=voice_5 (cùng cluster, sim=0.75)
→ WAV chuyển vào /tmp/hal-unknown-voice/voice_5/incoming_B.wav
→ Message: "Unknown Speaker: [voice:voice_5] I'm Alex. (audio saved at ..._B.wav...)"
→ Agent: quét các turn trước cùng tag [voice:voice_5] → tìm thấy path A
→ Agent: POST /speaker/enroll với wav_paths=[path_A, path_B], name="Alex"
→ Agent: "Rất vui được biết bạn, Alex!"
```

### Câu dài (luồng đăng ký đầy đủ)
```
User nói: "Xin chào mình là Leo, mình vừa đi làm về..." (30 từ, 8s audio)
→ HAL: nhận diện → unknown, _should_request_speaker_enroll(30 từ, 8s) = true
→ Message: "Unknown Speaker: Xin chào mình là Leo... (audio save at /tmp/hal-unknown-voice/incoming_xxx.wav, auto enroll...)"
→ Lamp: AppendEnrollNudge → cooldown OK → chèn "[REQUIRED: Follow speaker-recognizer/SKILL.md...]"
→ Agent: phát hiện "mình là Leo" → POST /speaker/enroll → "Rất vui được biết bạn, Leo!"
```

### Cooldown (bị chặn)
```
Cùng unknown speaker, 2 phút sau:
→ HAL: _should_request_speaker_enroll = true (đủ dài)
→ Message có "audio save at"
→ Lamp: AppendEnrollNudge → cooldown CHƯA hết (< 5 phút) → bỏ qua instruction
→ Agent: thấy "Unknown Speaker: ..." không có SKILL instruction → phản hồi bình thường
```
