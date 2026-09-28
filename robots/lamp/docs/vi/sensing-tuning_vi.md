# Tuning Sensing — Phần SER (Nhận Diện Cảm Xúc Giọng Nói)

> Tài liệu tuning đầy đủ (motion, face, sound, …) bằng tiếng Anh: [sensing-tuning.md](../sensing-tuning.md).  
> Kiến trúc SER: [speech-emotion_vi.md](../../../../docs/vi/speech-emotion_vi.md).

---

## Speech Emotion Recognition (SER)

**File:** `hal/config.py`, `hal/drivers/voice/voice_service.py` (`_submit_speech_emotion_from_session`, `_identify_and_decorate`, `_session_wav_for_ser`)

**Tích hợp voice (cuối phiên mic, độc lập transcript):** trong `finally` của `_stream_session`, `_identify_and_decorate(final_text, audio_buffer)` chạy **đúng 1 lần** để lấy đồng thời `final_msg` (cho Lamp POST khi STT có chữ) và `user_name` (cho SER submit). Sau đó gọi `_submit_speech_emotion_from_session(audio_buffer, user=...)` — chỉ build WAV và `SpeechEmotionService.submit`, không gọi speaker lần 2. Người không match / lỗi speaker vẫn enqueue SER dưới key dedup chung `unknown` nếu audio đủ dài.

```python
SPEECH_EMOTION_ENABLED = True
SPEECH_EMOTION_FLUSH_S = 10.0               # Chu kỳ drain buffer theo user
SPEECH_EMOTION_DEDUP_WINDOW_S = 300.0       # TTL (user, bucket) — 5 phút
SPEECH_EMOTION_MIN_AUDIO_S = 3.0            # Bỏ utterance ngắn hơn (mặc định config)
SPEECH_EMOTION_API_TIMEOUT_S = 15           # Timeout HTTP perception-service
DL_SER_ENDPOINT = "/lelamp/api/dl/ser/recognize"
```

Ngưỡng confidence **per-label** không nằm trong `config.py` — khai báo trong `hal/drivers/voice/speech_emotion/constants.py` qua `CONFIDENCE_THRESHOLD_BY_LABEL` (và `DEFAULT_CONFIDENCE_THRESHOLD` cho label không map). Negative emotion siết chặt hơn positive để giảm false positive:

```python
# constants.py
CONFIDENCE_THRESHOLD_BY_LABEL = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
```

Sửa trực tiếp dict để tune — không còn env override.

### Đọc log

Service gắn tag `[speech_emotion]`:

```
INFO lelamp.voice.speech_emotion: [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO lelamp.voice.speech_emotion: [speech_emotion] flushing alice: Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...) (mode of sad, fearful, sad)
INFO lelamp.voice.speech_emotion: [speech_emotion] sent to Lamp: Speech emotion detected: Sad. ...
INFO lelamp.voice.speech_emotion: [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

Dòng `flushing` hiển thị danh sách label thô — đó là mode trên các mẫu trong buffer.

### Tuning

| Triệu chứng | Cách chỉnh |
|-------------|------------|
| Cùng bucket fire quá thường xuyên | Tăng `SPEECH_EMOTION_DEDUP_WINDOW_S` (300 → 600) |
| Một utterance nhiễu vẫn lọt | Tăng entry tương ứng trong `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) — ví dụ `"sad": 0.6 → 0.7`. Chỉ tăng `DEFAULT_CONFIDENCE_THRESHOLD` khi nhiễu diện rộng |
| "Ừ" / "ok" ngắn bị flag | Tăng `SPEECH_EMOTION_MIN_AUDIO_S` (3.0 → 4.0) |
| Lamp phản ứng chậm sau đổi mood thật | Giảm `SPEECH_EMOTION_FLUSH_S` (10 → 5) |
| Cảnh báo worker queue full | Kiểm tra độ trễ perception-service; tăng queue không đủ nếu downstream kẹt |
| Quá nhiều `speech_emotion.detected` cho người lạ | **Kỳ vọng:** `user="unknown"`; siết entry per-label trong `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) hoặc dedup — **không** tắt SER chỉ vì transcript có `Unknown Speaker:` |

### Áp dụng thay đổi

Sau khi sửa `hal/config.py` hoặc `voice_service.py` trên Pi: restart service HAL (xem [os-server_vi.md](../../../../docs/vi/os-server_vi.md)).

---

## Nhận Diện Hoạt Động (Motion / Activity Recognition)

`MotionPerception` chạy nhận diện hành động Kinetics (qua perception-service) và phát event
`motion.activity` kèm các label hoạt động nhận được.

**File:** `hal/config.py`

```python
MOTION_CONFIDENCE_THRESHOLD = 0.3    # confidence tối thiểu để buffer 1 label
MOTION_FLUSH_S = 10.0                # nhịp xả buffer — tối đa 1 flush mỗi 10s
MOTION_EVENT_COOLDOWN_S = 900.0      # floor heartbeat cùng-class giữa 2 lần phát (15 phút)
MOTION_TRANSITION_MIN_GAP_S = 60.0   # gap tối thiểu cho bypass khi đổi class
```

**Các gate phát event (theo thứ tự, `motion.py`):**

1. **Nhịp flush** — detection buffer xả tối đa 1 lần mỗi `MOTION_FLUSH_S`.
2. **Gate presence** — không phát event nếu presence != PRESENT.
3. **Cooldown toàn cục** — không phát `motion.activity` quá 1 lần mỗi `MOTION_EVENT_COOLDOWN_S`
   khi **coarse activity class** (tập `ACTIVITY_GROUP`: sedentary/eat/drink/…) không đổi.
   Label thô flip cùng class (`writing → drawing`) vẫn bị floor đè — đó là nhiễu.
   Bypass khi: **đổi class** (`computer → eat` là thông tin thật, phát ngay khi đã qua
   `MOTION_TRANSITION_MIN_GAP_S` — gap này chặn detection chớp tắt mở lại spam mỗi flush),
   posture nudge (đã time-gate bởi pose window), và đổi user (user/phiên mới thấy event
   mới ngay lập tức).
4. **Dedup per-label** — kể cả khi qua được cooldown, cùng `(user, label-set)` trong
   cửa sổ 5 phút vẫn bị drop. Label Kinetics nhiễu flip set thường xuyên nên cooldown
   toàn cục ở trên mới là gate chính.

**Đọc log:**

```
INFO hal...motion: [motion] raw actions in window: ['writing', 'typing']
INFO hal...motion: [motion] flushing: Activity detected: writing.
INFO hal...motion: [motion] cooldown drop: ... (last event 42.1s ago < 900s floor, class unchanged)
INFO hal...motion: [motion] transition bypass: ['sedentary'] → ['eat'] (last event 312.4s ago)
```

**Tuning:**

| Triệu chứng | Cách chỉnh |
|-------------|------------|
| `motion.activity` fire liên tục (mỗi ~10s) | Tăng `MOTION_EVENT_COOLDOWN_S` — đây là floor cùng-class |
| Event lặp do detection chớp tắt (drink lúc có lúc không) | Tăng `MOTION_TRANSITION_MIN_GAP_S` (60 → 120+) |
| Không bắt được hoạt động nào | Giảm `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.2) |
| Label hoạt động rác | Tăng `MOTION_CONFIDENCE_THRESHOLD` (0.3 → 0.4) |
| Phản ứng chậm khi đổi hoạt động thật | Giảm `MOTION_FLUSH_S` (10 → 5) và/hoặc `MOTION_TRANSITION_MIN_GAP_S` — đổi class đã tự bypass cooldown |

---

## Nhận Diện Khuôn Mặt (Face Detection)

**File:** `hal/config.py`

```python
FACE_HEIGHT_RATIO_THRESHOLD = 0.10  # Bỏ qua mặt cao dưới 10% chiều cao frame
FACE_MAX_TRUNCATION = 0.05          # Bỏ qua mặt bị cắt >5% bbox ra ngoài frame
FACE_MIN_SHARPNESS = 100.0          # Bỏ qua mặt nhoè quá mức, không nhận diện được
FACE_STRANGER_MIN_TICKS = 2         # Số lần thấy trước khi cấp id cho mặt lạ
FACE_STRANGER_CORROBORATION_S = 6.0 # Ứng viên chờ được tính trong bao lâu
HAL_FACE_LANDMARK_CONF_THRESHOLD = 0.99  # Bỏ qua crop mà face mesh không chắc
FACE_MATCH_THRESHOLD = 0.40         # Ngưỡng cho match do ảnh upload đã enroll gánh
FACE_EXTENDED_THRESHOLD = 0.45      # Ngưỡng cho match do RIÊNG bank extended gánh
FACE_STRANGER_THRESHOLD = 0.45      # Ngưỡng để khớp một stranger_N đã biết
FACE_EXTEND_MIN_ENROLL_SIM = 0.45   # Ngưỡng ảnh upload phải đạt để tự thu view mới
FACE_COOLDOWN_S = 10.0              # Số giây tối thiểu giữa hai presence event
FACE_OWNER_FORGET_S = 3600.0        # Bắn lại presence sau N giây không thấy chủ
FACE_STRANGER_FORGET_S = 1800.0     # Tương tự cho người lạ
FACE_STRANGER_GAZE_TICKS = 3        # Chào người lạ: số nhịp face-ID trong lượt bỏ phiếu gaze
FACE_STRANGER_GAZE_MIN_FACING = 2   # Chào người lạ: số nhịp nhìn về đèn cần có
FACE_STRANGER_GAZE_WINDOW_S = 10.0  # Chào người lạ: tuổi tối đa của một phiếu gaze
```

Ngưỡng height ratio lọc bỏ những khuôn mặt **quá nhỏ** so với frame — thường là người ở xa, hoặc false positive mà crop mặt quá thấp độ phân giải để nhận diện đáng tin. Mặt có chiều cao bbox dưới ngưỡng (theo tỉ lệ chiều cao frame) bị bỏ qua trước khi phân loại.

**Vì sao dùng height chứ không dùng area.** Diện tích giảm theo 1/d² còn kích thước dài giảm theo 1/d, nên gate theo diện tích nhạy gấp đôi với cùng một thay đổi tầm xa. Quan trọng hơn: xoay đầu (yaw — trường hợp phổ biến) làm hẹp **chiều rộng** bbox nhưng giữ nguyên chiều cao, nên gate theo diện tích loại mặt nghiêng mạnh tay hơn mặt chính diện ở cùng khoảng cách — đi ngược lại chính tính năng extended set vốn sinh ra để học các góc nghiêng đó.

**Cắt cụt (truncation).** `FACE_MAX_TRUNCATION` là gate riêng cho mặt bị mép frame cắt, chạy sau gate chiều cao và trước khi phân loại. Mặt bị cắt không phải là mặt nhỏ hơn — nó là mặt **thiếu bộ phận**. SCRFD vẫn trả về một box hợp lý (cạnh bị cắt đơn giản chạy ra ngoài frame, ví dụ `[573, -34, 710, 121]`), còn landmark mesh thì tự tin bịa ra phần nó không nhìn thấy. Đo trên một lamp ngày 2026-09-03: một khuôn mặt bị cắt phía trên lông mày sinh ra landmark mắt đặt nhầm lên gò má với độ tin cậy **0.90**, embedding chỉ giống ảnh enroll của chính người đó **0.007**, và vẫn ra verdict FRIEND hoàn toàn nhờ bank extended tự thu.

Kiểm tra landmark-trong-bbox bên trong aligner **không** bắt được ca này: nó clamp bbox về trong frame trước khi so, nên landmark không bao giờ có thể nằm "ngoài" ở đúng cái cạnh đã cắt mặt. Gate này đo tỉ lệ **diện tích** bbox nằm ngoài frame. Mặt bị loại cũng không bao giờ vào `extend_candidates`, nên một view bị cắt không thể tự động lọt vào extended set của user.

**Vì sao 0.05.** Replay 496 frame đã log từ một lamp (2026-09-04): mọi frame nhận diện tốt đều ở mức 0% tràn (similarity trung vị với ảnh enroll là 0.66), dải 0.1–3% ở 0.56, còn **dải 5–10% sụp xuống 0.32**. Một frame bị cắt 6.1% đã cấp một id `stranger_N` giả, và ba frame sau đó match vào chính id đó — tức một frame bị cắt gây ra bốn lần nhận nhầm. Hạ ngưỡng từ 0.10 xuống 0.05 xoá sạch cả bốn; cái giá là ba frame trên 492 vốn nhận diện đúng nay bị bỏ qua.

**Cái nó KHÔNG bắt được.** Gate chỉ thấy phần cắt mà detector chịu thừa nhận bằng cách trả box ra ngoài frame (ví dụ `y1 = -34`). Đôi khi SCRFD lại clamp về đúng mép (`y1 = 0`), đo ra 0% tràn và lọt qua dù mặt thực sự bị cắt. Cách "coi bbox chạm mép frame là bị cắt" đã được đo và loại: nó bỏ 25 frame để bắt 2, vì 23 frame chạm mép vẫn được nhận diện đúng.

**Nhoè (blur).** `FACE_MIN_SHARPNESS` là gate thứ ba không phụ thuộc hình học: phương sai Laplacian của **crop aligned 112×112**, dưới ngưỡng đó thì detection bị bỏ trước mọi quyết định. Nhoè do chuyển động — lamp đang quay, hoặc người đang cử động — phá huỷ khuôn mặt mà không làm nó nhỏ đi hay bị cắt, nên hai gate ở trên không thấy gì cả.

Thứ chui ra từ một crop bị nhoè là embedding gần như ngẫu nhiên, không giống bất cứ thứ gì — và *"không giống gì cả"* chính là nhánh **người lạ mới**: một frame nhoè của chính chủ nhân lại cấp cho anh ta một id `stranger_N`. Đó đúng là chuyện đã xảy ra trên một lamp ngày 2026-09-04 — một frame chụp giữa lúc servo đang quét, similarity 0.10 với ảnh enroll của chính mình, nằm kẹp giữa hai lần nhận diện sạch 0.63 và 0.76.

Phải đo trên crop **aligned**, tuyệt đối không đo trên crop của detector: crop detector đổi kích thước theo từng frame, mà phương sai Laplacian lại co giãn theo độ phân giải, nên giá trị giữa các frame không so sánh được với nhau. Chi phí ~0.06 ms trên một mảng vốn đã tồn tại sẵn.

| gate | tỉ lệ frame bị bỏ |
|------|------|
| lapvar < 70 | 1.6% |
| **lapvar < 100** | **5.7%** |
| lapvar < 130 | 11.0% |

**Vì sao 100, chứ không chỉ vừa đủ để loại cái frame gây lỗi.** Cái giá hai phía không cân nhau. Gần như mọi frame bị bỏ đều vốn sẽ được nhận diện đúng — và mất một frame như vậy là im lặng, vì camera lấy mẫu lại mỗi `HAL_SENSING_INTERVAL` (2 giây) và `current_user()` vẫn giữ người đó trong `FACE_OWNER_FORGET_S` (1 giờ). Còn một stranger event sai ngay trên mặt chính chủ thì không hề im lặng.

Lưu ý gate này làm gì và không làm gì: nó tách **frame dùng được với frame không dùng được**, chứ không tách chủ nhân với người lạ. Frame nhoè của một người lạ thật cũng bị bỏ, khiến người đó chậm vài giây mới được cấp id — họ cũng sinh một frame mỗi 2 giây và sẽ được cấp id từ frame nét kế tiếp. Phương sai Laplacian co giãn theo ánh sáng và độ tương phản, nên con số này được hiệu chỉnh cho đúng camera này; hãy kiểm tra lại bằng các thư mục `FAIL-blurred` trong face debug log nếu phòng ốc hoặc quang học thay đổi.

**Độ tin cậy landmark.** `HAL_FACE_LANDMARK_CONF_THRESHOLD` (nằm trong `model_store.py`) là gate áp dụng ngay trong aligner: detection nào có độ tin cậy face-mesh dưới ngưỡng sẽ bị bỏ và không bao giờ được embed. **Hãy đọc con số này theo đúng thang mà model phát ra** — điểm số bão hoà, trung vị đúng bằng 1.000 và nhỏ nhất 0.613 trên 990 frame đã log, nên khoảng dùng được chỉ nằm ở phần trăm cuối. Giá trị mặc định cũ 0.60 không có nghĩa là "khá chặt"; nó có nghĩa là gate chưa từng chặn được gì, một lần cũng không.

Nó lọc ra những crop tuy là mặt nhưng không mang danh tính — thực tế gặp phải là SCRFD bắt trúng **vành tai** ở cự ly gần. 26 frame kiểu đó xuất hiện trong một phiên 40 phút với similarity ~0.0 so với ảnh enroll của chính chủ; một frame cấp id `stranger_N` và 25 frame còn lại match vào chính id đó. `landmark_score` tách chúng khỏi mặt thật ở mức AUC 0.98:

| gate | frame vào tới bước nhận diện | nhận diện đúng | id giả bị cấp |
|------|------|------|------|
| 0.60 (cũ) | 980 | 94.6% | 1 |
| 0.95 | 944 | 96.6% | 1 |
| **0.99** | 910 | **97.8%** | **0** |

Tỉ lệ nhận diện *tăng lên* khi siết gate, vì các crop vô danh rời khỏi mẫu số. Nó **không phải** bộ phát hiện che khuất: model ONNX chỉ là landmark regressor, không có detector head, nên nó nhận một ROI mà SCRFD đã khẳng định là mặt và trả về điểm cho bất cứ thứ gì bên trong — một khuôn mặt bị khăn giấy che vẫn đạt 0.9978.

**Tầm xa.** Ở 640×480 với FOV ngang ~65°, ngưỡng 0.10 tương ứng box mặt 48 px ở khoảng 2.2 m. Lưu ý gate này bất biến theo tỉ lệ: tăng `HAL_CAMERA_WIDTH`/`HEIGHT` không đổi việc mặt nào lọt qua, nhưng làm tăng chất lượng pixel của crop đưa vào recognizer. Ở 1280×720 cùng ngưỡng 0.10 cho crop 72 px thay vì 48 px.

**Ba bank, ba ngưỡng.** Một khuôn mặt là FRIEND khi ảnh **upload** đã enroll đạt trên `FACE_MATCH_THRESHOLD` (0.40), **hoặc** các view **extended** tự thu đạt trên `FACE_EXTENDED_THRESHOLD` (0.45). Nếu không, nó là một người lạ đã biết khi **bank người lạ** đạt trên `FACE_STRANGER_THRESHOLD` (0.45). Danh tính lấy từ bank nào *cho phép* match — một view extended dưới ngưỡng của chính nó thì không cấp cả quyết định lẫn cái tên.

| bank | chứa gì | miền so khớp | ngưỡng |
|------|------|------|------|
| upload | ảnh enroll của user | ảnh điện thoại ↔ camera | `FACE_MATCH_THRESHOLD` 0.40 |
| `.extended` | view thiết bị tự thu của một user đã nhận ra | camera ↔ camera | `FACE_EXTENDED_THRESHOLD` 0.45 |
| người lạ | một view cho mỗi `stranger_N` đã cấp | camera ↔ camera | `FACE_STRANGER_THRESHOLD` 0.45 |

Ảnh upload là ground truth, nhưng cũng là phép so khó nhất: ảnh điện thoại so với camera trần. Đo trên một lamp (16/09/2026) với ảnh enroll: chủ nhà đạt **0.60–0.85** khi chính diện, **0.29–0.34** khi nghiêng ¾, và **−0.31–0.28** khi nhìn nghiêng hẳn — cùng dải với người lạ và với một tấm ảnh giơ lên trên điện thoại. 0.40 nằm trong khoảng trống giữa cụm ¾ và cụm chính diện. Mức 0.30 cũ nằm ngay trong cụm ¾, nơi một frame nghiêng thật của chủ nhà và một người lạ trông giống chồng lên nhau; người lạ bị nhận thành chủ nhà là lỗi tệ hơn. Cái giá là dải ¾ — replay của #299 cho đường upload đạt recall 92.0% ở 0.30 và 86.4% ở 0.40 — và dải đó chính là thứ bank extended sinh ra để bù lại.

Hai bank tự thu là cùng một loại bằng chứng — một view duy nhất thiết bị tự chụp, camera so camera, không bao giờ được kiểm tra lại — nên cần cùng một ngưỡng cao hơn. Cùng một người được camera đó thấy lại thường đạt khoảng 0.6.

Một ngưỡng dùng chung không có giá trị nào an toàn. Đo trên 990 frame đã log (04/09/2026): riêng ảnh enroll giữ người lạ tốt nhất trong sáu người lạ đã xác minh ở 0.201, nhưng **bất kỳ** bank extended nào cũng nâng con số đó lên 0.32–0.40, vì mỗi view lưu thêm là một cơ hội nữa để người lạ khớp với thứ gì đó.

| `FACE_EXTENDED_THRESHOLD` | nhận diện đúng | biên so với người lạ tệ nhất (0.404) |
|------|------|------|
| 0.40 | 98.2% | **−0.004 — chấp nhận người lạ đó** |
| **0.45** | **97.8%** | **+0.046** |
| 0.50 | 97.5% | +0.096 |
| 0.60 | 96.8% | +0.196 |

Bốn frame mà 0.40 nhận thêm được đều có một lần nhận diện chắc chắn cách 2–6 giây, nên bỏ chúng không mất gì nhìn thấy được; một lần nhận nhầm thì có.

**Vì sao bank người lạ không khớp ở ngưỡng upload (#429).** Trước đây thì có, và trên một thiết bị có bank 20 dòng một-view được cấp trước khi có các cổng chất lượng, một người khách bị báo là `stranger_9` → `stranger_10` → `stranger_4` chỉ trong vài phút: các frame của cô ấy đạt **0.346 / 0.385 / 0.318** với ba dòng cũ của ba người khác nhau — mỗi lần vừa đủ vượt 0.30, argmax đổi theo góc đầu — trong khi hai frame của chính cô ấy đạt **0.658** với nhau. Ở 0.45 không dòng cũ nào khớp, cô ấy được cấp id sau `FACE_STRANGER_MIN_TICKS`, và các frame tiếp theo khớp với dòng mới ở ~0.66.

**Cái gì được tự động thu.** Một frame đã nhận diện chỉ được vào bank extended của user khi **cả hai** điều kiện đúng: ảnh upload đã enroll là bên gánh match và đạt trên `FACE_EXTEND_MIN_ENROLL_SIM` (0.45), và view đó đủ khác so với những gì đã lưu để đáng giữ lại.

Cần cả hai. Trước đây chỉ có mỗi phép thử tính mới lạ, mà "khác xa mọi thứ ta đang có" cũng chính là dấu hiệu của một *người khác* — nên luật đó chọn đúng thứ lẽ ra nó phải loại. Bước prune farthest-point tỉa bank lại neo vào ảnh upload, nên nó xếp người lạ lên hạng cao nhất và đuổi các view thật đi để giữ họ lại. Trên một lamp, bank đã thành 6/10 là người khác, match ở mức 1.000; và khi chỉ cho ăn toàn frame thật của chính chủ, luật cũ vẫn dựng ra một bank chấp nhận một người lạ đã xác minh ở 0.362.

Bắt buộc ảnh upload phải gánh match cũng chặn luôn bản sao đời thứ hai: một frame được nhận diện **bởi** bank extended không còn được bổ sung vào chính nó, nên một view xấu không thể sinh sôi.

Điều này không làm bank trở nên vô dụng — view được lưu không phải để phục vụ chính nó. Một view mà ảnh upload nhận ra ở mức 0.45–0.69 trở thành **mỏ neo mới**, lấn thêm một bước ra vùng tư thế xa hơn. Replay 990 frame đã log: 879 frame được ảnh upload nhận trực tiếp, và **11 frame nữa được riêng bank extended cứu**, ở mức enroll similarity 0.157–0.298 — dưới ngưỡng upload (0.30 lúc đó, 0.40 hiện nay), tức nếu không có bank thì đã trượt. Một trong số đó là frame mà thiết bị thật đã gán nhãn `stranger_4`.

**Mỗi view lưu những gì.** Mỗi view tự thu nằm trong `<USERS_DIR>/<user>/.extended/` gồm ba file chung một stem:

| file | vai trò |
|------|------|
| `ext_<ms>_<seq>.jpg` | crop khuôn mặt |
| `ext_<ms>_<seq>.npy` | embedding của nó — thứ được nạp lại sau restart, nên một tư thế khó không phải detect lại |
| `ext_<ms>_<seq>.json` | **provenance**: vì sao view này được nhận |

Bản ghi provenance chứa các điểm số đã cho view đó vào (`enroll_similarity`, `extended_similarity`, `match_source`, `max_sim_to_existing`), các chỉ số chất lượng của frame (`det_score`, `landmark_score`, `face_height_ratio`, `truncation`, `bbox`), và các `thresholds` đang có hiệu lực lúc đó. Trường cuối mới là điểm mấu chốt: nó cho phép hỏi "luật *cũ* đã cho những view nào lọt vào" sau một lần đổi ngưỡng, mà không phải đoán config lúc ấy là gì.

Đây là tài liệu, không phải state. Bộ loader chỉ liệt kê `*.jpg`, nên một file `.json` thiếu hoặc hỏng không tốn gì lúc chạy, và việc ghi nó là best-effort — view vẫn hợp lệ nếu ghi thất bại. Khi bị đuổi, cả ba file bị xoá cùng nhau. Các view thu trước khi có cơ chế này không có `.json`, và bản thân điều đó đã là một tín hiệu hữu ích: chúng có trước bộ luật hiện tại.

Không có nó, một bank hoá ra chứa nhầm view thì chỉ có thể xoá sạch toàn bộ — đúng thứ đã phải làm trên một lamp khi 6 trên 10 view hoá ra là người khác.

**Cấp một danh tính cần được xác nhận lại.** Một khuôn mặt không nhận ra được sẽ không có `stranger_N` chỉ từ một frame. Nó phải được thấy `FACE_STRANGER_MIN_TICKS` lần (2) trong vòng `FACE_STRANGER_CORROBORATION_S` (6 giây), và khớp **theo embedding** chứ không theo vị trí, để chữ "lại" có nghĩa là đúng người đó chứ không phải một khuôn mặt khác ở cùng một góc.

Cấp id là verdict đắt nhất — một danh tính tồn tại lâu dài, một stranger presence event, một dòng trong card Unknown Faces — và đạt tới bằng cách chấm *dưới* ngưỡng âm (0.20) của cả hai bank chủ nhà **và** dưới `FACE_STRANGER_THRESHOLD` — bank người lạ không nằm trong phép thử âm, vì khi nó đã có hơn chục dòng thì gần như luôn có một dòng trên 0.20 và một người mới thật sự sẽ không bao giờ được cấp id (#429). Chấm ở vùng đó là nơi một người lạ thật trông y hệt một frame tạm thời không dùng được. Các gate ở trên loại những frame hỏng mà chúng đo được; phần còn lại được bắt bằng câu hỏi mà không frame đơn lẻ nào trả lời nổi: khuôn mặt này một nhịp sau còn ở đó không? Đo trên 990 frame đã log, 19 trong 28 chuỗi chạm nhánh này chỉ dài đúng một nhịp đơn lẻ.

Cái giá đã biết của cổng này: một người khách quay lại ở góc mà view duy nhất đã lưu của họ không phủ được (0.20–0.45 so với chính dòng của họ) sẽ nhận một `stranger_N` thứ hai. Một dòng không thể bảo chứng cho góc nó chưa từng thấy; id thứ hai là kết quả trung thực, và vẫn tốt hơn là bị gộp vào danh tính của người khác.

Một người khách thật không bị ảnh hưởng quá một nhịp: 2 giây sau họ vẫn ở đó và được cấp id ngay lúc ấy. Cửa sổ cố tình đặt ~3 nhịp sensing thay vì bắt buộc liền kề tuyệt đối, để một frame bị rớt hoặc bị nhoè ở giữa không reset số đếm của một người khách thật.

Việc kiểm tra ánh nhìn của người lạ dùng lại `GAZE_MAX_YAW_DEG`, `GAZE_EDGE_CONE_SCALE` và `GAZE_MIN_FACE_PX` của gaze wake, nên chỉnh các giá trị đó cho gaze wake cũng làm thay đổi thời điểm người lạ được chào.

**Điều chỉnh (Tuning):**

| Triệu chứng | Cách chỉnh |
|-------------|------------|
| Người ở xa không được nhận diện | Giảm `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.07) |
| Người khác bị nhận thành user đã enroll | Tăng `FACE_EXTENDED_THRESHOLD` (0.45 → 0.50) và kiểm tra `match_source` trong face debug log — `extended` nghĩa là một view tự thu đã gánh match đó |
| User đã enroll bị `unsure` khi quay đầu ¾ | Bình thường trong lúc bank extended đang lấp lại — các góc đó chỉ đạt 0.29–0.34 so với ảnh upload chính diện. Kiểm tra `.extended/` có đang lớn; chỉ giảm `FACE_MATCH_THRESHOLD` (0.40 → 0.35) sau khi đo lại với người lạ đã biết |
| Một người khách bị báo dưới nhiều id `stranger_N` trong vài phút | Các dòng người lạ cũ đang bị nhận nhầm (#429). Kiểm tra `stranger_similarity` trong face debug log chỉ vừa vượt ngưỡng; tăng `FACE_STRANGER_THRESHOLD` (0.45 → 0.50), và xoá `/root/local/strangers/*.npy` nếu các dòng có từ trước 04/09/2026 (chưa có cổng chất lượng) |
| Một người khách quay lại cứ bị cấp `stranger_N` mới | View duy nhất đã lưu của họ không phủ góc đó. Chỉ giảm `FACE_STRANGER_THRESHOLD` (0.45 → 0.40) khi debug log còn biên so với các lần nhận nhầm; dưới 0.40 thì #429 quay lại |
| User đã enroll bị trượt ở các góc mà ảnh chính diện không phủ được | Giảm `FACE_EXTENDED_THRESHOLD`, nhưng không xuống dưới 0.45 nếu chưa đo lại với người lạ đã biết |
| Bank extended đầy người khác | Lẽ ra không còn xảy ra; nếu vẫn có, tăng `FACE_EXTEND_MIN_ENROLL_SIM` và kiểm tra `match_source` của từng view đã lưu |
| Bank extended rỗng hoặc ngừng lớn | Giảm `FACE_EXTEND_MIN_ENROLL_SIM` (0.45 → 0.40). Chỉ frame mà *ảnh upload* nhận ra mới đủ điều kiện, nên user chỉ có một ảnh chính diện thì bank lớn chậm là đúng thiết kế |
| False detection từ các mảng nhỏ trông như mặt | Tăng `FACE_HEIGHT_RATIO_THRESHOLD` (0.10 → 0.15) |
| Nhận diện chập chờn / liên tục cấp id `stranger_N` mới | Crop quá nhỏ để embed đáng tin — tăng `FACE_HEIGHT_RATIO_THRESHOLD`, hoặc nâng độ phân giải camera lên 1280×720 |
| Nhận nhầm người khi ai đó ngồi sát mép frame | Mặt bị cắt — giảm `FACE_MAX_TRUNCATION` (0.05 → 0.03), hoặc chỉnh lại hướng camera để đầu luôn nằm trọn trong frame |
| Người ở mép frame hoàn toàn không được nhận diện | Tăng `FACE_MAX_TRUNCATION` (0.05 → 0.10); xem các thư mục `FAIL-truncated` trong face debug log để biết thực tế bị cắt bao nhiêu |
| Lamp cấp `stranger_N` cho chính chủ trong lúc đang quay | Nhoè do chuyển động — đó là thứ `FACE_MIN_SHARPNESS` lọc ra; xem thư mục `FAIL-blurred` để biết độ nét thực tế |
| Khách lạ mất quá lâu mới được ghi nhận | Giảm `FACE_STRANGER_MIN_TICKS` xuống 1 để cấp id ngay từ một frame (hành vi cũ) |
| Vẫn xuất hiện id `stranger_N` giả | Tăng `FACE_STRANGER_MIN_TICKS` lên 3; mỗi bậc khiến khách thật chậm thêm một nhịp sensing |
| Lamp không bao giờ chào một vị khách đã nhìn về phía nó | Giảm `FACE_STRANGER_GAZE_MIN_FACING` (2 → 1); kiểm tra `[face] stranger gaze:` trong log HAL |
| Khách nhìn một lúc ngắn nhưng không được chào vì nhịp chậm hoặc bị bỏ qua | Tăng `FACE_STRANGER_GAZE_WINDOW_S` (10 → 15); quá lớn thì các cái liếc cách nhau vài phút sẽ cộng dồn |
| Nhận diện chết hẳn trong phòng tối sau khi cập nhật | Phương sai Laplacian giảm theo ánh sáng; giảm `FACE_MIN_SHARPNESS` (100 → 70) rồi kiểm tra lại `FAIL-blurred` |
| Lamp cấp id `stranger_N` cho chính chủ ở cự ly gần | Detector đang bắt trúng vành tai hoặc tương tự — đó là thứ `HAL_FACE_LANDMARK_CONF_THRESHOLD` 0.99 lọc ra |
| Mặt rõ ràng bình thường lại ngừng được nhận diện sau khi cập nhật | Giảm `HAL_FACE_LANDMARK_CONF_THRESHOLD` (0.99 → 0.95); mặc định được tinh chỉnh trên một thiết bị |
| Presence event bắn quá thường xuyên | Tăng `FACE_COOLDOWN_S` (10 → 30) |
| Lamp quên chủ quá nhanh sau khi rời đi | Tăng `FACE_OWNER_FORGET_S` |

---

## Nhận Diện Hoạt Động Per-Face (Per-Face Motion)

**File:** `hal/config.py`

```python
MOTION_PER_FACE_ENABLED = false            # Bật nhận diện hành động per-face
MOTION_PER_FACE_DEDUP_WINDOW_S = 300.0     # Cửa sổ dedup per-action (5 phút)
MOTION_PER_FACE_SESSION_TTL_S = 30.0       # Xóa session sau bao lâu không thấy face
MOTION_PER_FACE_MIN_FRAMES = 4             # Số frame tối thiểu trước event đầu tiên
```

Per-face motion mở WS session riêng cho từng khuôn mặt và chạy action recognition trên crop mở rộng quanh mặt. Mỗi action dedup độc lập theo face. Trên lớp dedup per-face có MỘT cooldown floor toàn cục chung cho mọi face — cùng semantics và cùng knobs với motion thường (`MOTION_EVENT_COOLDOWN_S` floor cùng-class, `MOTION_TRANSITION_MIN_GAP_S` gap tối thiểu cho bypass đổi class, floor xóa khi user thực sự đổi) — nên N mặt trong frame vẫn chỉ tối đa 1 `motion.activity` cùng-class mỗi cooldown, không phải N.

**Tuning:**

| Triệu chứng | Cách chỉnh |
|-------------|------------|
| Quá nhiều event cho một người | Tăng `MOTION_PER_FACE_DEDUP_WINDOW_S` (300 → 600) |
| Quá nhiều event khi nhiều người | Tăng `MOTION_EVENT_COOLDOWN_S` — floor toàn cục dùng chung với motion thường |
| Phân loại nhiễu từ frame đơn lẻ | Tăng `MOTION_PER_FACE_MIN_FRAMES` (4 → 8) |
| Session tồn đọng cho face thoáng qua | Giảm `MOTION_PER_FACE_SESSION_TTL_S` (30 → 15) |
| WS connection chồng chất khi nhiều người | Tắt bằng `MOTION_PER_FACE_ENABLED=false` |
