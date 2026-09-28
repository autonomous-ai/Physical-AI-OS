# Nhận Diện Cảm Xúc Giọng Nói (SER)

HAL phân tích cảm xúc từ giọng nói **sau mỗi phiên mic** (VAD trigger → im lặng ~2.5 s đóng phiên), độc lập với việc STT có trả transcript hay không. Nhờ vậy, tiếng cười, thở dài, "ờ ờ" và các tín hiệu phi-lời-nói (vốn để lại transcript rỗng) vẫn được phân loại. `SpeakerDecorator.submit_speech_emotion_from_session` dựng một WAV mono 16 kHz từ **snapshot chưa cắt** của phiên và đưa vào `SpeechEmotionService`; service đó lọc trước (prefilter) audio ngay tại thiết bị, phân loại trên cloud, gom theo người dùng, khử trùng lặp theo nhóm phân cực, rồi gửi sự kiện `speech_emotion.detected` tới OS server. Speaker recognition cung cấp trường `user` (rơi về `unknown` khi không nhận diện được, buffer quá ngắn để tạo embedding, hoặc không khớp ai); nó **không phải là cổng chặn** trước SER.

Đây là bản sinh đôi phía giọng nói của nhận diện cảm xúc khuôn mặt (`emotion.detected`). Kiến trúc, cách phân nhóm phân cực và cửa sổ dedup được làm đối xứng có chủ đích để cả hai phương thức rơi vào cùng các skill phía sau (`user-emotion-detection/SKILL.md`, mood logging, music suggestion).

> Đừng nhầm với **Emotion Expression** (`emotion/SKILL.md`) — cái đó điều khiển đầu ra cảm xúc của *chính thiết bị* (servo + LED + mắt). SER là cảm nhận điều *người dùng* cảm thấy qua giọng nói; expression là cách *agent* thể hiện cảm xúc của nó.

**Tiếng Anh:** [docs/speech-emotion.md](../speech-emotion.md)

> **Trang này là tài liệu tham chiếu SER ở mức nền tảng** — kiến trúc, các cổng chặn, nhóm phân cực, cấu hình, audio debug, các chế độ lỗi. Bộ tài liệu speech-service đi sâu hơn theo hai trục:
>
> - **[docs/vi/speech/speech-emotion-pipeline_vi.md](speech/speech-emotion-pipeline_vi.md)** — từng chặng kèm file:line, bảng ngưỡng prefilter đầy đủ, bảng tra 18 lý do drop → dòng log, và sổ tay gỡ lỗi.
> - **[docs/vi/speech/speech-emotion-known-issues_vi.md](speech/speech-emotion-known-issues_vi.md)** — lỗi mở và vấn đề hiệu năng.
> - [docs/vi/speech/speech-emotion_vi.md](speech/speech-emotion_vi.md) — SER cắm vào một voice turn như thế nào.
>
> Khi tài liệu và code mâu thuẫn, **code thắng**.

**Tài liệu liên quan:** [Tuning sensing (SER)](../../robots/lamp/docs/sensing-tuning.md#speech-emotion-recognition-ser) · [perception-service](../perception-service.md) · [Sensing behavior](../../robots/lamp/docs/vi/sensing-behavior_vi.md)

---

## Kiến Trúc

```
voice_service._stream_session(...) finally:                      ← mỗi lần phiên mic kết thúc
    ├─ finalize_session(...) → (combined, ser_audio_buffer, buf_duration)
    │      ser_audio_buffer = snapshot CHƯA CẮT (SER giữ tiếng cười / thở dài);
    │      audio_buffer bị cắt tại chỗ, chỉ dùng cho speaker recognition
    │
    ├─ speaker-ID prepass: identify_and_decorate(final_text, audio_buffer)
    │      → (final_msg, se_user, display)      ← lần recognize DUY NHẤT của turn này
    │
    └─ dispatch_turn(...)                        (_internal/turn_dispatch.py)
           ├─ if combined: sensing_sender.send(final_msg, event_type)
           │      `voice` / `voice_command` / `voice_followup` / `voice_agent_handled`
           └─ submit_speech_emotion_from_session(ser_audio_buffer, user)   ← pipeline SER
                  └─ _session_wav_for_ser(buffer) → (wav, duration_s)
                  └─ SpeechEmotionService.submit(user, wav, duration_s)
                  ▼
SpeechEmotionService.submit(user, wav_bytes, duration_s)   ← non-blocking
    │  4 cổng (available / user khác rỗng / wav khác rỗng / duration ≥ 3.0 s)
    │  queue.put_nowait                          ← maxsize 32
    ▼
worker thread (daemon)
    │  Emotion2VecRecognizer.recognize(wav_bytes)
    │     ├─ prefilter — RMS trim + cổng voiced, rồi Silero VAD, sau đó cắt đoạn ≤8 giây nhiều giọng nói nhất   ← CỤC BỘ, loại phi-tiếng-nói
    │     ├─ POST {DL_BACKEND_URL}/hal/api/dl/ser/recognize
    │     │     ← { "label": "happy", "confidence": 0.78 }
    │     ├─ cổng confidence theo nhãn
    │     └─ _persist_wav() → clip debug ghi ra đĩa
    ▼
buffer[user].append(_Inference)              ← gom theo từng user
    ▲
    │  (flush thread thức dậy mỗi SPEECH_EMOTION_FLUSH_S)
    ▼
flush:
    ① bỏ các nhãn neutral / <unk> / other
    ② mode(label) trên các mẫu đã đệm của user này
    ③ bucket = polarity(mode)                ← positive | negative
    ④ dedup TTL: key=(user, bucket) trong SPEECH_EMOTION_DEDUP_WINDOW_S
    ⑤ POST OS server /api/sensing/event với type="speech_emotion.detected"
```

Pipeline voice của HAL **chỉ gọi `submit()`**. Prefilter, toàn bộ HTTP I/O tới perception-service, việc đệm, phân nhóm, dedup, retry, và POST tới OS server đều nằm gọn trong module `speech_emotion/` — chúng không bao giờ chặn đường STT.

Có một điểm gọi **thứ hai**: khi cổng wake-word từ chối một turn, `voice_service.py:1555` submit trực tiếp với `user="unknown"` mặc định. Nó chỉ chạm tới được khi `WAKEWORD_ENABLED` bật, mà mặc định là **false** (`hal/config.py:625`) — nên ở cấu hình always-listening đang ship, mọi phiên mic kết thúc đều tới SER qua `dispatch_turn`.

---

## Module `speech_emotion/`

```
hal/drivers/voice/speech_emotion/
├── __init__.py        # API công khai: SpeechEmotionService + ABC + engine + kiểu kết quả
├── constants.py       # mặc định, bộ nhãn, bản đồ bucket, ngưỡng prefilter, loại sự kiện
├── base.py            # BaseSpeechEmotionRecognizer (ABC), dataclass SpeechEmotionResult
├── emotion2vec.py     # Emotion2VecRecognizer — prefilter + lớp bọc HTTP cho /hal/api/dl/ser/recognize
├── utils.py           # normalize_label, is_neutral, bucket_for, hedge_for, format_message,
│                      # wav_to_pcm16, pcm16_to_wav, compute_frame_rms, compute_trim_and_voiced
├── debug_tracer.py    # Tracer SER-DEBUG + profiler theo stage — chỉ cho dev, mặc định TẮT, xoá trước khi deploy
└── service.py         # SpeechEmotionService — queue + worker + flush + dedup + gửi tới OS server
```

Các bên gọi và lân cận:

| Mối quan tâm | Đường dẫn |
|--------------|-----------|
| Điểm submit + khởi tạo service | `hal/drivers/voice/_internal/speaker_decorate.py` (`:117`, `:322`, `:343`) |
| Turn dispatch (cấp `user`) | `hal/drivers/voice/_internal/turn_dispatch.py:157` |
| Snapshot SER chưa cắt | `hal/drivers/voice/_internal/session_finalize.py:28` |
| Sidecar dedup theo boot | `hal/dedup_sidecar.py` |
| Model Silero dùng chung | `hal/drivers/voice/resources/silero_vad.onnx` |
| Model + phục vụ trên cloud | `integrations/perception-service/src/core/perception/audio_emotion/` |
| Bên tiêu thụ ở OS server | `system/server/sensing/delivery/http/handler.go` |

Thêm engine mới: kế thừa `BaseSpeechEmotionRecognizer` (một method: `recognize(wav_bytes) -> SpeechEmotionResult | None`) và tiêm vào qua `SpeechEmotionService(recognizer=...)` lúc khởi tạo. Factory mặc định dựng `Emotion2VecRecognizer` từ `config.SPEECH_EMOTION_API_URL`. **Lưu ý prefilter nằm trong engine, không phải trong service** — engine thay thế sẽ không thừa hưởng gì từ nó.

### `SpeechEmotionService`

Hai daemon thread, chỉ khởi động trong `__init__` khi `recognizer.available` là true:

| Thread | Vòng lặp | Rút từ | Tạo ra |
|--------|----------|--------|--------|
| `speech-emotion-worker` | `_worker_loop` | hàng đợi submit (`queue.Queue`, maxsize 32) | các mục trong buffer theo user |
| `speech-emotion-flush` | `_flush_loop` (chờ + đập mỗi `SPEECH_EMOTION_FLUSH_S`) | buffer theo user | các POST `speech_emotion.detected` tới OS server |

Cả hai thoát sạch khi gọi `stop()` — worker bị "đầu độc" bằng sentinel `None`, flush thread quan sát stop event ngay trong `Event.wait` (và do đó bỏ qua lần flush cuối, có chủ đích). Mọi state có thể thay đổi (`_buffer`, `_last_sent_by_key`, `_last_flush_ts`) được bảo vệ bởi một `threading.RLock`.

`submit()` là non-blocking theo thiết kế. Khi hàng đợi worker đầy (tồn đọng 32 job), submission **mới** bị bỏ kèm cảnh báo — đó là dấu hiệu quá tải thật (perception-service treo hoặc chết). Audio là từng câu nói rời rạc, không phải stream, nên mất một câu là chấp nhận được.

> `available` là `recognizer is not None and recognizer.available`, và với engine HTTP đó chỉ là `bool(url)` — kiểm tra **cấu hình**, không phải kiểm tra kết nối. Service có thể báo `available` trong khi backend chết; mọi lời gọi khi đó đều lỗi ở chặng HTTP và trả `None`.

Config mức module được đọc **lúc import** (`service.py:85-99`), nên test nào patch `hal.config` phải làm trước khi import module.

### `_Job` vs `_Inference`

| Kiểu | Tạo ở | Giữ gì |
|------|-------|--------|
| `_Job` | `submit()` | `user`, `wav_bytes`, `duration_s` — nằm trong hàng đợi chờ worker |
| `_Inference` | `_process_job()` | `user`, `label`, `confidence`, `duration_s`, `ts`, `audio_path` — nằm trong buffer chờ flush |

---

## Prefilter Cục Bộ (model duy nhất ở edge trên đường này)

Trước mọi I/O mạng, `Emotion2VecRecognizer.prefilter()` (`emotion2vec.py:215`) chặn clip. Mục đích là loại **audio dài-nhưng-thưa** — một clip 20 giây chứa hai giây tiếng TV — thứ mà emotion2vec sẽ gán nhãn tự tin và sai. Nó trả về WAV **đã trim và mã hóa lại**, nên cloud cũng nhận buffer sạch hơn.

Giải mã yêu cầu PCM 16-bit đúng 16 kHz; đa kênh được lấy trung bình về mono.

**Chặng 1 — RMS** (một lượt, `utils.compute_trim_and_voiced`). Một envelope RMS 20 ms phục vụ hai việc với hai ngưỡng có chủ đích: `PREFILTER_TRIM_RMS = 3500` (nghiêm) neo biên cắt đầu/đuôi, `PREFILTER_VOICED_RMS = 2500` (rộng rãi) đếm frame có tiếng bên trong vùng đó để giọng thì thầm/hơi vẫn được ghi nhận. Giữ 100 ms đệm quanh vết cắt. Drop khi clip sau trim `< 2.0 s`, tổng thời lượng có tiếng `< 1.0 s`, hoặc tỉ lệ voiced `< 0.30` (mẫu số là vùng trim đã đệm, nên đoạn im lặng dài ở đầu không làm giảm tỉ lệ).

**Chặng 2 — Silero VAD** trên buffer đã trim (`emotion2vec.py:417`). Hợp đồng Silero v5: chunk 512 mẫu ở 16 kHz với context 64 mẫu đặt phía trước; `state` LSTM và `context` được dựng lại từ zero mỗi lần gọi, nên các lần gọi độc lập không lẫn trạng thái vào nhau. Drop khi thời lượng Silero-voiced `< 1.0 s`. Khi Silero không khả dụng (thiếu model, ORT hỏng), ngưỡng RMS **siết** từ 1.0 s lên 3.0 s thay vì cho qua tất cả.

**Cắt clip gửi lên** — sau khi qua cả hai cổng lọc, `utils.select_voiced_span` chỉ giữ đoạn liên tục **8 giây** (`SER_MAX_CLIP_S`) có nhiều frame 20 ms có giọng nói nhất (`PREFILTER_VOICED_RMS`). Khi bằng nhau thì chọn đoạn muộn nhất. Clip ≤8 giây giữ nguyên. Đoạn được cắt nguyên khối, không ghép các mảnh có giọng nói lại với nhau. perception-service cũng giới hạn đầu vào SER ở 2–8 giây, nên phần dài hơn cũng sẽ bị cắt phía server (#492).

Lỗi mã hóa lại thì fail-open: WAV gốc được gửi đi.

Mọi ngưỡng là hằng số biên dịch trong `constants.py:87-135` — **không** override được bằng env. Bảng đầy đủ ở [docs/vi/speech/speech-emotion-pipeline_vi.md](speech/speech-emotion-pipeline_vi.md#chặng-5-6--prefilter-mô-hình-cục-bộ-duy-nhất).

> Đây là session Silero **thứ tư** trong tiến trình HAL (`voice_service._silero_vad`, `_rt_noise_vad` và `_silence_vad` đều nạp cùng file). Xem [known-issues #5](speech/speech-emotion-known-issues_vi.md#5--một-session-silero-onnx-thứ-tư-thừa).

---

## Tích Hợp `voice_service.py`

Được gọi từ `dispatch_turn` (`_internal/turn_dispatch.py:157`), vốn chạy từ khối `finally` của `VoiceService._stream_session`. Speaker recognize chạy **một lần** mỗi phiên — ở speaker-ID prepass tại `voice_service.py:1407` — và kết quả cấp cho cả phần trang trí message gửi OS server lẫn trường `user` của SER:

```python
# voice_service.py finally, sau finalize_session:

# 1. Speaker-ID prepass — lần recognize DUY NHẤT của turn. Chạy ở đây vì voiceprint
#    cần cả câu nói, nên đây là thời điểm sớm nhất có thể.
if combined:
    _final_text, _ = self._decorator.classify_wake_word(combined)
    turn_identity = self._decorator.identify_and_decorate(_final_text, audio_buffer)

# 2. Dispatch — tái dùng turn_identity, không bao giờ nhận dạng hai lần
dispatch_turn(self._decorator, self._sensing_sender, combined,
              audio_buffer, ser_audio_buffer, rt, identity=turn_identity)
```

```python
# turn_dispatch.py
user = UNKNOWN_USER_LABEL
if combined:
    final_msg, se_user, _ = identity          # hoặc identify_and_decorate(...) nếu None
    user = se_user if se_user else UNKNOWN_USER_LABEL
    sensing_sender.send(...)                  # định tuyến theo rt.handled / rt.delegated

# Submit SER — dùng snapshot CHƯA CẮT để tiếng cười / thở dài còn nguyên.
decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
```

`submit_speech_emotion_from_session` (`speaker_decorate.py:343`) là bên submit mỏng, không nhúng lời gọi speaker nào:

```python
session_audio = self._session_wav_for_ser(audio_buffer)
if session_audio is None:
    return                                          # buffer rỗng hoặc < SPEAKER_MIN_AUDIO_S
wav_bytes, duration_s = session_audio
self._speech_emotion.submit(user=user, wav_bytes=wav_bytes, duration_s=duration_s)
```

Toàn bộ lời gọi được bọc `try/except` — lỗi SER không bao giờ giết được một voice turn.

### Gán `user` cho SER

| Kết quả Speaker ID | `user` truyền vào `submit()` |
|--------------------|------------------------------|
| `match=True` với tên đã đăng ký | Nhãn speaker (ví dụ `alice`) |
| `match=False` / dưới ngưỡng (API OK, không `error`) | `unknown` — đặt trực tiếp bởi `identify_and_decorate` (`speaker_decorate.py:317`) |
| Recognize bị bỏ qua hoặc lỗi (`se_user` là `None`) | `unknown` — thay ở `turn_dispatch.py:105` |
| Hoàn toàn không có transcript (`if combined:` bị bỏ qua) | `unknown` — giá trị khởi tạo ở `turn_dispatch.py:94` còn nguyên |
| Cổng wake-word từ chối turn | `unknown` — tham số mặc định ở `voice_service.py:1555` |

SER không bao giờ được gọi từ bên trong `identify_and_decorate`.

> Năm trường hợp đó **không phân biệt được trên dây**, và OS server đọc `current_user` là "ai đang đứng trước thiết bị" (`handler.go:155` gọi `mood.SetCurrentUser`). Một sự kiện SER mà speaker-ID trả về `unknown` do đó sẽ ghi đè một danh tính tốt lấy từ khuôn mặt. Xem [known-issues #6](speech/speech-emotion-known-issues_vi.md#6--current_user-ghi-đè-danh-tính-toàn-thiết-bị).

### Chi phí dùng chung: một lần speaker recognize mỗi turn

Speaker recognize bắn **một lần** mỗi phiên mic. Kết quả `(final_msg, se_user, display)` duy nhất được tái dùng bởi:

1. POST tới OS server — khi STT có transcript.
2. Submit SER — luôn luôn.

Đó là lý do thứ tự là: finalize → phân loại wake-word → speaker-ID prepass (một lần) → dispatch (POST OS server → submit SER). `dispatch_turn` nhận kết quả prepass qua tham số `identity` và chỉ tính lại khi nó là `None`; SER không bao giờ tự phát lời gọi `/embed` riêng.

---

## Khi Nào **Không** Gọi SER

- Thiết bị không khai báo capability `audio` — voice people-perception (speaker-ID + SER) phụ thuộc mic, nên `SpeakerDecorator` được dựng với `enable_people_perception=False` và service SER không bao giờ khởi tạo (`speaker_decorate.py:117`). (Đây là capability `audio`, không phải `presence`: SER chỉ cần mic. Cảm xúc khuôn mặt trong vòng lặp sensing vẫn phụ thuộc `presence`.)
- `SPEECH_EMOTION_ENABLED=false`, hoặc `SpeechEmotionService` không `available` (thiếu `DL_BACKEND_URL`)
- `ser_audio_buffer` rỗng hoặc ngắn hơn `SPEAKER_MIN_AUDIO_S` (chặn `_session_wav_for_ser`)
- `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` (chặn ngay trong `submit()` — mặc định 3.0 s, đây là sàn ràng buộc)
- `submit()` drop (hàng đợi đầy, `user` rỗng sau normalize)
- Prefilter từ chối clip (xem phần trên) — bị loại trước lời gọi cloud

`wav_bytes` được dựng từ `ser_audio_buffer` — snapshot **chưa cắt** lấy trước khi `finalize_session` cắt im lặng đuôi khỏi bản dùng cho speaker-recognition. Đó là chủ đích: tiếng cười, thở dài và "hmm" ở đuôi mang sắc thái cảm xúc nhưng không phải từ ngữ, và phần trim của speaker-recognition sẽ cắt mất chúng.

Khởi tạo một lần mỗi tiến trình trong `SpeakerDecorator.__init__`, theo đúng mẫu của speaker recognizer: instance được tạo một lần, các thread chỉ chạy khi engine báo `available`.

---

## Sự Kiện OS Server

```http
POST http://127.0.0.1:5000/api/sensing/event
Content-Type: application/json
```

```json
{
  "type": "speech_emotion.detected",
  "message": "Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)",
  "current_user": "alice",
  "audio": "/tmp/hal-speech-emotion/1715587812413_alice_sad.wav"
}
```

Tiền tố thô `Speech emotion detected: <Label>.` là mỏ neo cho parser định tuyến phía OS server. Phần trong ngoặc là mệnh đề rào đón để LLM không kết luận quá mạnh trên một tín hiệu SER nhiễu — cùng mẫu với message `Emotion detected: …` của khuôn mặt. Nội dung rào đón chọn theo bucket: `do not over-celebrate` (positive), `do not assume the user is distressed` (negative), `do not over-react` (other).

Trường `audio` là một trường **riêng, tùy chọn** — đường dẫn trên đĩa của clip WAV tạo ra sự kiện này. Nó **không** được nhúng vào `message` và **không bao giờ** chuyển tiếp tới LLM (xem [Lưu Audio Để Gỡ Lỗi](#lưu-audio-để-gỡ-lỗi) bên dưới). Rỗng khi persistence bị tắt hoặc ghi lỗi.

Chính sách retry: timeout 5 s, 3 lần thử với back-off 2 s khi gặp `ConnectionError` hoặc HTTP `503`. Các mã 4xx/5xx khác được log và bỏ (mẫu mất luôn — ta không dội bom OS server). Lần ngủ back-off nằm trên flush thread, nên OS server không truy cập được sẽ làm trễ nhịp flush kế tiếp tới ~4 s.

### Lời gọi engine → perception-service

```http
POST {DL_BACKEND_URL}/hal/api/dl/ser/recognize
Headers:
  X-API-Key: <key>
  Content-Type: application/json
Body:
  {"audio_b64": "<base64 WAV (mono 16 kHz, đã prefilter + trim)>", "return_scores": false}
```

```json
{ "label": "happy", "confidence": 0.9981, "scores": null }
```

Bộ nhãn (emotion2vec_plus_large, từ `/api/dl/ser/labels`):

```
angry, disgusted, fearful, happy, neutral, other, sad, surprised, <unk>
```

Timeout là 15 s hardcode (`DEFAULT_API_TIMEOUT_S`) và **không có retry ở chặng này** — mọi lỗi transport, non-200, body không phải JSON, hoặc thiếu `label` đều trả `None` và mẫu bị bỏ qua. Khi `DL_ENCRYPTION_ENABLED` (mặc định **true**) và public key phân giải được, body request và response được bọc bởi `CryptoSession`; `DL_ENCRYPTION_REQUIRED` (mặc định false) biến việc thiếu key thành lỗi cứng lúc khởi tạo thay vì âm thầm rơi về plaintext.

---

## Lưu Audio Để Gỡ Lỗi

Để các kết quả SER nhiễu có thể truy vết được, service lưu lại clip WAV đứng sau mỗi sự kiện và hiện nó trong Flow Monitor UI dưới dạng player bấm-để-nghe. **Đây chỉ là công cụ gỡ lỗi — audio không bao giờ được gửi cho LLM.**

### Phía ghi (HAL)

Trong `_process_job`, mọi inference vượt cổng confidence theo nhãn đều được `_persist_wav()` ghi ra đĩa trước khi vào buffer:

- **Thư mục:** `SPEECH_EMOTION_AUDIO_DIR` (cấu hình trong `hal/config.py:563`, env `HAL_SPEECH_EMOTION_AUDIO_DIR`), mặc định `<tempdir>/hal-speech-emotion` (tức `/tmp/hal-speech-emotion`). Tạo bằng `os.makedirs(exist_ok=True)` lúc init; nếu tạo lỗi thì thư mục bị tắt và mọi POST mang trường `audio` rỗng (suy giảm êm — SER vẫn chạy).
- **Nội dung:** WAV **trước prefilter**, tức thứ mic bắt được, không phải buffer đã trim gửi cho model.
- **Tên file:** `<ms>_<user>_<label>.wav`, với `<ms>` là mốc thời gian inference tính bằng mili-giây và `<user>`/`<label>` được làm sạch về `[a-zA-Z0-9_-]` (ký tự khác gộp thành `_`). Chính việc làm sạch đó cho phép handler Go phục vụ các file này chỉ bằng basename.
- **Chọn khi flush:** khi flush của một user phát ra nhãn không-neutral chiếm ưu thế, nó đính clip **mới nhất** trong các inference cùng nhãn đó — `max(dom_inferences, key=lambda i: i.ts).audio_path` — vào trường `audio` của POST.

### Phía phục vụ (OS server)

OS server chỉ để lộ clip cho Flow Monitor UI qua `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`, `handler.go:821`). Nó phục vụ WAV theo **basename** (đường dẫn đầy đủ không bao giờ rời thiết bị) từ một trong:

```
/var/lib/hal/speech-emotion
/tmp/hal-speech-emotion
```

Basename được kiểm tra (đuôi `.wav`, không có `/`, `\`, hay `..`) trước khi phục vụ. Ở `PostSensingEvent`, đường dẫn `audio` thô được `audioURLForPath` (`handler.go:808`) ánh xạ sang URL phục vụ được (`/api/sensing/audio/<name>`) và gắn vào detail của sự kiện Monitor `sensing_input`; item trong Monitor render nó thành player bấm được. Đường dẫn thô không bao giờ lộ ra UI, và trường `audio` không bao giờ được nối vào text chat gửi đi.

### Giới hạn đã biết: không tự dọn dẹp

Mọi inference đủ điều kiện đều được lưu WAV — **kể cả các clip neutral vốn chắc chắn về mặt cấu trúc sẽ bị bỏ ở flush**, và các clip không chiếm ưu thế vốn không bao giờ trở thành sự kiện. **Không có cơ chế tự dọn** thư mục audio. Ở đường mặc định, thư mục đó nằm trên **tmpfs, tức RAM**, nên trên thiết bị chạy dài ngày nó phải được dọn bởi housekeeping bên ngoài. Theo dõi ở [known-issues #1](speech/speech-emotion-known-issues_vi.md#1--thư-mục-wav-debug-phình-vô-hạn-trên-tmpfs) và [#2](speech/speech-emotion-known-issues_vi.md#2--kết-quả-neutral-được-ghi-đĩa-và-đệm-rồi-luôn-bị-loại).

---

## Debug Tracer (SER-DEBUG)

Phần lưu audio ở trên chỉ giữ những clip **vượt** cổng confidence, và không giữ lại gì về **lý do** một clip bị loại. Tracer SER-DEBUG (`hal/drivers/voice/speech_emotion/debug_tracer.py`) lấp chỗ đó: với `HAL_SER_DEBUG=true`, nó ghi một thư mục cho mỗi câu nói và mỗi quyết định flush, chứa audio, mọi chỉ số đứng sau quyết định, cùng một profile latency/bộ nhớ theo từng stage. Nó phản chiếu tracer SPEAKER-DEBUG trong `speaker_recognizer.py` — cùng dạng env knob, cùng quy ước đặt tên thư mục, cùng cam kết "không bao giờ ném lỗi, không bao giờ đổi hành vi" — và cũng như nó, **đây là công cụ chỉ dành cho dev, dự kiến xoá trước khi deploy chính thức** (`grep -rn "SER-DEBUG" hal/drivers/voice/speech_emotion/`).

**Mặc định tắt.** Công tắc chỉ được đọc một lần lúc import; đổi xong phải restart HAL. Khi tắt, mỗi lời gọi trace chỉ tốn một lần tra thuộc tính và một `nullcontext` — không timer, không sampler RSS, không giải mã audio.

### Bố cục

```
<root>/recognize/<ts>_<label>_<confidence>/   một câu nói: submit → prefilter → HTTP → phán quyết
<root>/recognize/<ts>_FAIL-<reason>/          bị loại trước khi có bất kỳ nhãn nào
<root>/emit/<ts>_<label>_<confidence>/        một quyết định flush cho một user
<root>/emit/<ts>_FAIL-<reason>/               flush không tạo ra gì để gửi
```

`<root>` mặc định là `speech_emotion_logs/` cạnh `debug_tracer.py` (đã git-ignore); nếu cây nguồn chỉ-đọc, nó rơi về `<tempdir>/hal-ser-debug` thay vì âm thầm tự tắt. `<ts>` có dạng `YYYYMMDD-HHMMSS-ffffff`; cách đặt tên khớp với log speaker và facial emotion, nên cả ba đọc theo cùng một kiểu.

Thư mục được đặt tên theo nhãn **không** có nghĩa là sự kiện đã bắn — cả loại-vì-confidence-thấp lẫn loại-vì-dedup đều có một phân loại thật, và đặt tên theo nó chính là thứ khiến trace đáng mở ra xem. Trường `verdict` trong `result.json` mới mang kết cục thật (`buffered` / `emitted` / `dropped`, kèm `drop_reason`).

Mỗi thư mục chứa:

| File | Nội dung |
|------|----------|
| `input.wav` | WAV như lúc submit — thứ mic session bắt được, trước prefilter |
| `prefiltered.wav` | WAV đã trim thực sự được upload; không có khi prefilter đã loại mẫu |
| `result.json` | Toàn bộ quyết định: user, thống kê audio đầu vào, cấu hình engine (URL, mã hoá, Silero có nạp được không), mọi ngưỡng prefilter **và** các chỉ số đo được so với chúng, phiên HTTP, nhãn/confidence/ngưỡng/bucket, và phán quyết |
| `profile.json` | Wall-clock / CPU / RSS theo từng stage — `recognize` → `prefilter` (→ `decode_wav`, `rms_trim`, `silero_vad`, `encode_wav`), `encode_b64`, `api_request`, `api_decode`, cộng `persist_wav` và `send_to_sensing` |

Một câu nói tạo ra **một** thư mục dù phần code biết audio, biết phiên HTTP và biết phán quyết buffering nằm ở ba chỗ khác nhau: service mở một trace thread-local, engine điền vào đó khi audio đi qua, rồi service đóng lại. Các trường hợp loại ngay ở `submit()` xảy ra trên thread của caller, trước khi trace tồn tại, nên được ghi thành thư mục một-lần không kèm profile — chưa có gì chạy để mà profile.

### Đọc profile

`profile.json` trả lời "thời gian và bộ nhớ đi đâu". RSS được **lấy mẫu** trên một thread nền (~20 ms) và mỗi stage báo đỉnh trong cửa sổ của chính nó, vì cách chỉ đo hai đầu mút sẽ báo `0.0` cho một stage cấp phát rồi giải phóng ngay trong cửa sổ của nó. Hãy đọc `rss_peak_delta_mb` (stage đó tốn bao nhiêu ở lúc tệ nhất); `rss_end_delta_mb` là phần nó **giữ lại** và âm là hợp lệ khi allocator trả trang về OS. `cpu_pct > 100%` nghĩa là dùng hơn một core; `api_request` gần 0% là đúng — nó đang chờ mạng. Cả RSS lẫn `cpu_ms` đều tính trên toàn tiến trình, nên một thread HAL khác đang bận sẽ làm phồng số liệu; hãy đọc một stage như một cận trên và ưu tiên nhìn hình dạng qua nhiều lần gọi. Mỗi lần gọi cũng ghi một dòng tóm tắt vào log (`SER-DEBUG profile [recognize]: total=… recognize.prefilter.silero_vad=…ms/…%cpu/+…MB …`).

### Biến môi trường

| Biến env | Mặc định | Tác dụng |
|----------|----------|----------|
| `HAL_SER_DEBUG` | `false` | `true` để bật (áp dụng cho **cả** trace lẫn profile). Chỉ đọc một lần lúc import — đổi xong phải restart HAL |
| `HAL_SER_DEBUG_DIR` | `speech_emotion_logs/` cạnh `debug_tracer.py` | Thư mục gốc đầu ra |
| `HAL_SER_DEBUG_MAX_ENTRIES` | `1000` | Giới hạn thư mục theo từng loại, xoá cũ nhất; `0` = không giới hạn |

Các knob này được đọc thẳng từ `os.environ` bên trong `debug_tracer.py`, **không** đi qua `hal/config.py` — để cả khối vẫn xoá được mà không đụng tới config. Xem thêm [sổ tay gỡ lỗi ở tầng pipeline](speech/speech-emotion-pipeline_vi.md#sổ-tay-gỡ-lỗi).

---

## Nhóm Phân Cực (Polarity Buckets)

Cách phân nhóm phản chiếu pipeline khuôn mặt để các key dedup `(user, bucket)` diễn giải được xuyên phương thức:

| Bucket | Nhãn |
|--------|------|
| `positive` | happy, surprised |
| `negative` | angry, disgusted, fearful, sad |
| `other` | neutral, other, `<unk>` (bị **bỏ trước khi phân nhóm** — xem cổng chống spam #5, nên bucket này thực tế không chạm tới được) |

Vì sao dedup theo bucket chứ không theo nhãn: emotion2vec trên câu nói ngắn lật qua lại giữa sad/fearful/angry trong cùng một trạng thái cảm xúc. Dedup theo nhãn sẽ gửi quá nhiều. Dedup theo bucket gộp nhiễu trong cùng nhóm (sad ↔ fearful ↔ angry) thành một sự kiện negative mỗi cửa sổ; còn lật chéo nhóm (sad → happy) vẫn bắn như một thay đổi tâm trạng thật.

---

## Các Cổng Chống Spam

Xếp lớp, khớp với bộ xử lý cảm xúc khuôn mặt:

| # | Chặng | Điều kiện drop |
|---|-------|----------------|
| 1 | `submit()` | `wav_bytes` rỗng / `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` |
| 2 | `submit()` | `user` rỗng sau normalize (không có chủ thể để gán cảm xúc — phản chiếu `current_user==""` của khuôn mặt) |
| 3 | engine | **prefilter** — cổng RMS trim/voiced/ratio, rồi Silero VAD (xem trên) |
| 4 | worker | `confidence < CONFIDENCE_THRESHOLD_BY_LABEL[label]` (cổng theo nhãn, xem Cấu Hình) |
| 5 | flush | nhãn là `neutral` / `other` / `<unk>` |
| 6 | flush | `(user, bucket)` đã gửi cách đây chưa tới `SPEECH_EMOTION_DEDUP_WINDOW_S` giây |

Mỗi bucket giữ entry TTL độc lập của riêng nó trong `_last_sent_by_key`. Gửi một sự kiện positive KHÔNG reset cửa sổ negative (và ngược lại). Cùng ngữ nghĩa với cảm xúc khuôn mặt.

Map TTL được lưu vào sidecar theo boot (`/tmp/hal-ser-state.json`, `hal/dedup_sidecar.py`) để restart service HAL khôi phục cửa sổ dedup thay vì bắn lại cảm xúc gần nhất ở lần flush đầu sau deploy/OTA. Reboot toàn thiết bị thì khởi động sạch (tmpfs + kiểm tra `boot_id` của kernel). Cảm xúc khuôn mặt dùng cùng cơ chế với file riêng (`/tmp/hal-emotion-state.json`, `drivers/sensing/perceptions/processors/emotion.py:35`).

Còn **ba** lớp giới hạn tốc độ nữa ở phía server, sau dedup của chính SER: `speech_emotion.detected` thuộc `ambientFloorTypes` (`SensingTurnFloorSeconds`, mặc định 120 s), nó hết hạn khỏi hàng chờ của runtime sau 60 s, và nó gộp về bản cuối cùng của loại đó (`runtimes/hermes/events.go:124-150`). Một sự kiện sống sót ở edge vẫn có thể bị loại trước khi tới agent.

> **Thứ tự các cổng là vấn đề hiệu năng chính của pipeline.** Cổng 5 và 6 — hai cổng có sức chặn thực sự — chạy *sau khi* lời gọi cloud, lượt Silero cục bộ và lần ghi đĩa đã bị trả giá. Xem [known-issues #7](speech/speech-emotion-known-issues_vi.md#7--mọi-cổng-chặn-đều-nằm-sau-bước-tốn-kém-duy-nhất).

---

## Cấu Hình (`hal/config.py`)

Mọi knob nằm trong `hal/config.py` dưới dạng `SPEECH_EMOTION_*`, override được qua biến môi trường. Các mặc định phản chiếu `EMOTION_*` để hai phương thức hành xử giống nhau ngay từ đầu.

| Hằng số | Biến env | Mặc định | Mục đích |
|---------|----------|----------|----------|
| `SPEECH_EMOTION_ENABLED` | `HAL_SPEECH_EMOTION_ENABLED` | `true` | Công tắc tổng |
| `SPEECH_EMOTION_FLUSH_S` | `HAL_SPEECH_EMOTION_FLUSH_S` | `10.0` | Nhịp rút buffer |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | `HAL_SPEECH_EMOTION_DEDUP_WINDOW_S` | `300.0` | TTL cho `(user, bucket)` |
| `SPEECH_EMOTION_MIN_AUDIO_S` | `HAL_SPEECH_EMOTION_MIN_AUDIO_S` | `3.0` | Độ dài câu nói tối thiểu |
| `SPEECH_EMOTION_API_TIMEOUT_S` | `HAL_SPEECH_EMOTION_API_TIMEOUT_S` | `15` | **Config chết** — không bao giờ được truyền vào engine, vốn luôn dùng 15 s hardcode. Xem [known-issues #3](speech/speech-emotion-known-issues_vi.md#3--speech_emotion_api_timeout_s-là-config-chết) |
| `SPEECH_EMOTION_AUDIO_DIR` | `HAL_SPEECH_EMOTION_AUDIO_DIR` | `/tmp/hal-speech-emotion` | Thư mục WAV debug; `""` để tắt |
| `DL_SER_ENDPOINT` | `DL_SER_ENDPOINT` | `/hal/api/dl/ser/recognize` | Hậu tố path trên `DL_BACKEND_URL` |
| `SPEECH_EMOTION_API_URL` | — | dẫn xuất | `DL_BACKEND_URL` + `DL_SER_ENDPOINT` |
| `SPEECH_EMOTION_API_KEY` | — | sao từ `DL_API_KEY` | Gửi dưới dạng `X-API-Key` |
| `DL_ENCRYPTION_ENABLED` | `HAL_DL_ENCRYPTION` | `true` | Bọc body request/response |
| `DL_ENCRYPTION_REQUIRED` | `HAL_DL_ENCRYPTION_REQUIRED` | `false` | Lỗi cứng thay vì rơi về plaintext |
| `SPEAKER_MIN_AUDIO_S` | `HAL_SPEAKER_MIN_AUDIO_S` | `0.8` | Cũng chặn việc dựng WAV cho SER; vô hiệu ở đây vì sàn 3.0 s của `submit()` cao hơn |

Bộ nhãn, bản đồ bucket, ngưỡng prefilter, và **ngưỡng confidence theo từng nhãn** được khai báo trong `hal/drivers/voice/speech_emotion/constants.py` (không override được bằng env — sửa chúng cần thay code). Dict ngưỡng:

```python
# constants.py:38
CONFIDENCE_THRESHOLD_BY_LABEL: dict[str, float] = {
    SpeechEmotionLabel.HAPPY:     0.5,
    SpeechEmotionLabel.SURPRISED: 0.6,
    SpeechEmotionLabel.SAD:       0.7,
    SpeechEmotionLabel.ANGRY:     0.6,
    SpeechEmotionLabel.FEARFUL:   0.6,
    SpeechEmotionLabel.DISGUSTED: 0.6,
}
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5  # dự phòng cho nhãn không có trong bảng
```

Cảm xúc tiêu cực có ngưỡng cao hơn để tránh báo động giả; `happy` lỏng nhất vì bắn nhầm tích cực thì rẻ; `sad` có ngưỡng cao nhất (code chỉ nêu giá trị, không nêu lý do). Tra cứu đi qua `utils.threshold_for(label)`, rơi về `DEFAULT_CONFIDENCE_THRESHOLD` cho nhãn không ánh xạ — bao gồm cả `neutral`, nên nó vượt cổng ở mức 0.5 và chỉ bị loại sau đó ở flush.

Cũng không cấu hình được: `DEFAULT_QUEUE_MAXSIZE = 32`, các chuỗi rào đón, 3 lần retry OS server, và đường sidecar `/tmp/hal-ser-state.json`.

---

## Chế Độ Lỗi

| Lỗi | Hệ quả | Khắc phục |
|-----|--------|-----------|
| Chưa cấu hình `DL_BACKEND_URL` | `recognizer.available` là False, thread không bao giờ chạy, `submit()` là no-op (`SERVICE IDLE` lúc boot) | Đặt `llm_base_url` trong config OS server |
| perception-service chết (connection refused) | Worker log cảnh báo, bỏ mẫu, không retry ở chặng này | Câu nói kế tiếp tự thử lại |
| perception-service trả non-200 / không phải JSON / thiếu `label` | Worker log cảnh báo, bỏ mẫu | Như trên |
| Bắt buộc mã hóa nhưng thiếu public key | `RuntimeError` lúc khởi tạo → bị bắt trong `_init_speech_emotion` → service là `None` | Sửa `DL_PUBLIC_KEY_URL`/`DL_PUBLIC_KEY_FILE`, hoặc bỏ `HAL_DL_ENCRYPTION_REQUIRED` |
| Thiếu model Silero / ORT hỏng | Prefilter rơi về ngưỡng RMS **nghiêm hơn** (3.0 s voiced) | Khôi phục `resources/silero_vad.onnx`; xem cảnh báo nạp model một lần |
| Prefilter từ chối clip | Bỏ mẫu trước lời gọi cloud, kèm log các chỉ số dẫn tới quyết định | Bình thường với TV/nhạc/audio thưa; chỉnh `constants.py:87-135` nếu tiếng nói thật đang bị cắt |
| Hàng đợi worker đầy | `submit()` log cảnh báo, bỏ job **mới** | Dấu hiệu backend quá tải; xem [known-issues #4](speech/speech-emotion-known-issues_vi.md#4--hàng-đợi-bỏ-job-mới-nhất-và-không-bao-giờ-loại-job-cũ) |
| Endpoint sensing của OS server chết | 3 lần thử với back-off 2 s, rồi bỏ mẫu | Buffer tiếp tục đầy cho lần flush sau |
| `duration_s < MIN_AUDIO_S` | Bỏ trong `submit()` kèm một dòng log | Bình thường — câu quá ngắn không đáng phân loại |
| `mkdir` thư mục audio lỗi | Tắt persistence cho cả tiến trình; mọi POST mang `audio` rỗng | Kiểm tra quyền trên `HAL_SPEECH_EMOTION_AUDIO_DIR` |

Không thứ nào ở đây chặn đường STT hay speaker recognition — lỗi SER im lặng ở mức người dùng và chỉ thấy được trong log HAL.

---

## Gỡ Lỗi

Bảng tra đầy đủ lý do drop → dòng log và sổ tay gỡ lỗi từng bước nằm ở [docs/vi/speech/speech-emotion-pipeline_vi.md](speech/speech-emotion-pipeline_vi.md#mọi-lý-do-drop-theo-thứ-tự). Tóm tắt nhanh:

1. `SERVICE IDLE` lúc khởi động → chưa set `DL_BACKEND_URL`.
2. Có `SERVICE STARTED` nhưng không có `submit() called` → turn ngắn hơn 3.0 s, hoặc thiếu capability `audio`.
3. Có `submit() called` nhưng không có `POST` → đọc dòng `[prefilter]`; drop do audio thưa là phổ biến trong phòng yên tĩnh hoặc vang.
4. Có `recognize OK` nhưng không có `EMIT` → toàn `neutral`, hoặc còn trong cửa sổ dedup (thử `HAL_SPEECH_EMOTION_DEDUP_WINDOW_S=5`).
5. Có `SENT -> OS server 200 OK` nhưng agent không phản ứng → phía server: sàn ambient 120 s, hết hạn hàng đợi 60 s, hoặc gộp-về-bản-cuối.

### Kiểm chứng bằng tay

Hai script (thủ công, không phải unit test — cần mic và backend truy cập được):

```bash
python -m hal.test.test_speech_emotion_engine     # chỉ chặng cloud
python -m hal.test.test_speech_emotion_service    # toàn pipeline + OS server giả trên :5000
```

### Ảnh chụp chẩn đoán

`SpeechEmotionService.to_dict()` trả về ảnh chụp runtime cho các endpoint introspection:

```json
{
  "type": "speech_emotion",
  "available": true,
  "buffered_users": 2,
  "dedup_keys": 3,
  "queue_size": 0,
  "last_flush_ts": 1715587812.41
}
```

`dedup_keys` bị chặn bởi (số user đã thấy) × 2 bucket. Nó báo dư khi rảnh, vì key hết hạn chỉ được prune ở các nhịp flush có việc.

---

## Quan Hệ Với Các Hệ Thống Khác

| Pipeline | Phương thức | Kích hoạt | Loại sự kiện | Cùng skill tiêu thụ? |
|----------|-------------|-----------|--------------|----------------------|
| Cảm xúc khuôn mặt (`drivers/sensing/perceptions/processors/emotion.py`) | Frame camera → crop mặt | Mỗi khuôn mặt thấy được | `emotion.detected` | có — `user-emotion-detection/SKILL.md` |
| **Cảm xúc giọng nói (tài liệu này)** | Mic → WAV cuối phiên | Mỗi phiên mic có ≥ 3.0 s audio vượt prefilter — **độc lập với transcript STT** | `speech_emotion.detected` | có — cùng `user-emotion-detection/SKILL.md` (router nhận cả hai tiền tố) |
| Tổng hợp mood (skill Mood) | — | Bất kỳ tín hiệu cảm xúc nào | các dòng mood `signal` / `decision` | — |
| Sound (`sound.py` perception) | RMS mic | Tiếng ồn lớn | `sound` | leo thang tiếng chó sủa, skill riêng |

Cảm xúc giọng nói dùng chung từ vựng phân cực với cảm xúc khuôn mặt một cách có chủ đích. Sensing handler của OS server gắn tiền tố `[speech_emotion]` cho sự kiện đến (so với `[emotion]` cho khuôn mặt) tại `system/lib/sensingmsg/sensingmsg.go:78`, pre-fetch cùng khối `[emotion_context: …]` qua `skillcontext.BuildEmotionContext` (`sensingmsg.go:116-122`, một nhánh phục vụ cả hai loại), và định tuyến tới `user-emotion-detection/SKILL.md`. Bản đồ nhãn→mood bao phủ cả hai từ vựng (`Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`); hành vi khác biệt duy nhất theo phương thức trong skill là `source:"voice"` so với `source:"camera"` trên dòng mood signal. Cooldown gợi ý nhạc dùng chung xuyên phương thức nên voice không thể vượt qua một gợi ý gần đây do camera kích hoạt, và ngược lại.

`[speech_emotion]` cũng nằm trong `ackSkipPrefixes` của mọi runtime (`runtimes/*/emotion_ack.go`), nên các turn này **không** kích hoạt mặt "thinking" — chúng thường kết thúc bằng `NO_REPLY`, vốn sẽ làm khuôn mặt kẹt lại.

---

## Xem thêm

- [docs/vi/speech/speech-emotion-pipeline_vi.md](speech/speech-emotion-pipeline_vi.md) — luồng dữ liệu từng chặng, bảng ngưỡng đầy đủ, bảng tra lý do drop, sổ tay gỡ lỗi.
- [docs/vi/speech/speech-emotion-known-issues_vi.md](speech/speech-emotion-known-issues_vi.md) — lỗi mở và vấn đề hiệu năng.
- [docs/vi/speech/README_vi.md](speech/README_vi.md) — toàn bộ speech service (STT, TTS, speaker recognition, realtime).
- [docs/vi/speech/cloud-models_vi.md](speech/cloud-models_vi.md) — endpoint emotion2vec và chuỗi phục vụ ONNX/TensorRT.
- [docs/vi/perception-service_vi.md](perception-service_vi.md) — dịch vụ cloud DL inference, load balancer, mã hóa.
- [docs/vi/face-emotion/README_vi.md](face-emotion/README_vi.md) — bản sinh đôi phía camera.
