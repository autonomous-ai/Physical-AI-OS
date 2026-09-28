# Vision Tracking — Theo dõi vật thể bằng servo

Lamp có thể theo dõi và hướng theo bất kỳ vật thể nào mà người dùng gọi tên. Một detector tìm vật thể theo tên và seed một ViT tracker, sau đó một vòng lặp vision tốc độ cao bám theo nó real-time, trong khi một servo worker tách rời điều khiển đầu trượt mượt về phía target.

Toàn bộ code tracking nằm trong package `hal/drivers/tracking/`:

| Module | Nội dung |
|--------|----------|
| `tracker_service.py` | `TrackerService` — vòng đời session (start/stop/status/update_bbox) + vòng lặp vision nhanh |
| `constants.py` | Toàn bộ knob tuning (các module khác import với alias `C`) |
| `detection.py` | `ObjectDetector` — chuỗi YuNet face / YOLOv8n local / YOLOWorld remote |
| `vit_tracker.py` | Backend tracker OpenCV (`create_tracker`, `vit_init`, `vit_update`, `get_tracking_score`) |
| `servo_follow.py` | `ServoFollower` — servo goal + thread worker SmoothDamp |
| `filters.py` | `AlphaBetaFilter2D`, `PID`, `smooth_damp`, `soft_deadband` |
| `frame_utils.py` | `downscale`, `scale_bbox` (map tọa độ) |

## Định tuyến intent giọng nói

Yêu cầu voice không khớp rule local có thể qua nhánh Jev của OS để chọn
`servo_track` với một trong 23 nhãn mục tiêu chuẩn hóa, hoặc `servo_track_stop`.
OS kiểm tra mục tiêu và capability motion đã khai báo rồi dùng lại rule tracking
hiện có; Jev không cấp payload HAL tự do. Mục tiêu thiếu/không hỗ trợ, camera
bên ngoài và quyết định chưa đủ điểm chuyển main agent, giữ nguyên đường skill
open-vocabulary. Xem [Định tuyến intent Jev](../../../../docs/vi/os-server_vi.md#fallback-intent-jev).

## Kiến trúc

```
User: "Lamp, follow the cup"
         |
    POST /servo/track {"target": "cup"}
         |
    1. Freeze servos 0.3s → grab a sharp frame
         |
    2. Detect the object (YuNet face | local YOLOv8n | remote YOLOWorld) → bbox
         |
    3. TrackerVit init on the bbox
         |
    4. Two decoupled threads:
         |   a. Vision loop @ FAST_LOOP_FPS (15):
         |        ViT update → alpha-beta centroid filter → soft dead zone
         |        → PID + velocity feedforward → publish an absolute servo goal
         |        (background YOLO re-detect every 1.5s corrects drift)
         |   b. Servo worker: SmoothDamp glide toward the latest goal
         |        (ease-in/ease-out; gộp các thay đổi setpoint rất nhỏ)
         |
    5. Lost / bloated / no-detect / timeout → auto-stop, rồi nội suy về idle
```

Vòng lặp vision không bao giờ block chờ motor di chuyển: nó publish một servo goal *tuyệt đối* rồi chuyển ngay sang frame kế tiếp. Servo worker sở hữu chuyển động vật lý và liên tục ease về phía goal mới nhất. Đây chính là cái giữ cho cả fps của tracker cao lẫn chuyển động đầu mượt.

### Vision downscale, tính toán ở độ phân giải gốc

Camera chạy **1280×720**. Mọi thành phần vision nặng — ViT tracker và cả ba detector — đều chạy trên frame đã downscale xuống `VISION_MAX_WIDTH` (640 px rộng, 0.5× → ¼ số pixel) để tăng tốc. Mỗi bbox chúng tạo ra được map **ngược về tọa độ gốc 1280×720** trước bất kỳ phép tính servo/PID nào (`frame_utils.downscale` / `scale_bbox`, `vit_tracker.vit_init` / `vit_update`, và `detect_object` là transparent). Vì hợp đồng tọa độ ở phía sau luôn là độ phân giải gốc, nên không hằng số nào được tune theo pixel (PID gains, gates, dead zones, ngưỡng feedforward) thay đổi khi hệ số downscale thay đổi. Đặt `VISION_MAX_WIDTH = 0` để tắt.

## Detection

`detect_object(frame, target)` trả về một bbox `(x, y, w, h)` theo tọa độ camera gốc, thử ba đường theo thứ tự:

| Path | Detector | Khi nào | Tốc độ (A523) |
|------|----------|---------|---------------|
| 0 | **YuNet** face detector (`face_detection_yunet_2023mar.onnx`) | target ∈ {`face`, `human face`, `khuôn mặt`, `mặt`} | ~30 ms |
| 1 | **Local YOLOv8n** (COCO classes, `yolov8n.pt`, imgsz=448) | target map tới một COCO class | ~310–530 ms, trung vị ~360 ms (đo trên lamp-0c89, input 1280x720) |
| 2 | **Remote YOLOWorld** open-vocab (`{DL_BACKEND_URL}/detect/yoloworld`) | target không thuộc COCO, hoặc local miss (fallback) | **~0.55 s trung vị** (min 334 ms, p90 1.1 s, max 2.0 s; n=49 trên lamp-0c89) |

- COCO không có class hand/face, nên `hand`/`face` cố ý rơi xuống YuNet/YOLOWorld thay vì map tới `person` (vốn khóa vào toàn thân).
- Khi local-YOLO miss, code fallback về remote YOLOWorld, **throttle** tối đa một lần mỗi `REMOTE_FALLBACK_MIN_INTERVAL` (2.0 s) để một target thật sự không thể thấy không gọi remote mỗi lần redetect.
- **Fallback bị tắt cho redetect thường kỳ** (`allow_remote_fallback=False`, do background scan của loop truyền vào). Khi target COCO đã khoá được rồi thì một lần local miss là chuyện bình thường — vật xoay đi, một khung hình nhoè — mà thread detect chỉ chạy một luồng, nên một vòng gọi remote kéo chu kỳ confirm từ ~0.5 s lên ~3 s, vượt cổng trust và làm servo đứng im trong khi vật vẫn còn trong khung. Local là thẩm quyền cho class mà nó được huấn luyện; để lần redetect sau xác nhận. Lúc seed và lúc phục hồi sau khi mất lock (`_do_retry`) vẫn cho phép remote, còn target open-vocab thì không ảnh hưởng — không có đường local thì chẳng có gì để fallback.
- Bộ lọc chất lượng detection: confidence ≥ `DETECT_MIN_CONFIDENCE` (0.15), diện tích nằm giữa `DETECT_MIN_AREA_RATIO` (0.3%) và `DETECT_MAX_AREA_RATIO` (80%) của frame.
- **Chống nhầm vật giống nhau (đường local)** — YOLO local detect **mở** (không filter `classes=`) để các class cạnh tranh còn hiện diện, sau đó: (a) cụm dễ nhầm cell phone / mouse / remote cần conf ≥ 0.35 (`_CONFUSABLE_CONF_FLOOR`) thay vì 0.15 chung; (b) **cross-class disambiguation** — nếu một box class khác đè lên candidate (IoU ≥ 0.5) với confidence *cao hơn*, candidate bị từ chối ("đó chắc là con chuột, không phải cái điện thoại anh hỏi") và code rơi xuống remote fallback. Floor 0.35 chỉ áp cho detect lúc *bắt đầu* session (`strict=True`); redetect giữa session dùng floor chung 0.15 (phone đang phẩy nhanh thường chỉ reconfirm ở conf 0.2–0.3, và các gate reinit đã bảo vệ lock) — cross-class check giữ nguyên cả hai chế độ.

Weights được check vào repo (`hal/drivers/tracking/models/`) nên deploy chỉ một lần rsync và Pi không bao giờ cần internet lúc boot để bắt đầu tracking.

## Tracker: TrackerVit

**Model:** `hal/drivers/tracking/models/vittrack.onnx` (đã check vào repo)

| Tính năng | Giá trị |
|-----------|---------|
| Tốc độ | ~15–25 ms/frame trên frame đã downscale |
| Confidence score | `getTrackingScore()` 0.0–1.0 mỗi frame |
| Xử lý scale | Tự động điều chỉnh kích thước bbox |
| Phát hiện mất | Trả về `ok=False` + score thấp khi vật thể biến mất |

**Chuỗi fallback:** TrackerVit → CSRT → KCF → MIL. Chỉ ViT phơi ra confidence score (dùng cho phát hiện ghost-lock); các tracker khác trả về 1.0.

## Servo Control

Tracking điều khiển 4 joint:

- **base_yaw** (ID 1) — pan trái/phải (100 % của yaw)
- **base_pitch** (ID 2) — tilt lên/xuống, 10 % của pitch
- **elbow_pitch** (ID 3) — tilt lên/xuống, 90 % của pitch
- **wrist_pitch** (ID 5) — tilt lên/xuống, 0 %

Pitch được dồn vào elbow (`PITCH_WEIGHT_ELBOW = 0.90`). Thực nghiệm cho thấy chỉ các joint xoay-thuần mới đưa vật thể về giữa; base/wrist chủ yếu tịnh tiến camera (kinematic coupling), nên weight của chúng thấp/bằng không. Chiều dương của motor elbow bị đảo ở phần cứng, nên đóng góp của nó mang `ELBOW_PITCH_SIGN = -1.0`.

### Control law (vision loop → servo goal)

Mỗi frame vòng lặp biến bbox của tracker thành một servo goal tuyệt đối:

1. **Alpha-beta filter trên centroid** (`AlphaBetaFilter2D`) — một Kalman steady-state vận tốc-hằng. Làm mượt jitter, coast qua các frame bị rớt/rác bằng prediction, gate các cú teleport outlier (`AB_GATE_PX`), và phơi ra ước lượng vận tốc. Một velocity lead (`AB_LEAD_S = 0.20 s`) nhắm trước mặt target — "lead room" kiểu điện ảnh.
2. **Dead zone 3 tầng** (`soft_deadband`) — liên tục tại cả hai ranh giới: zero thật bên trong ±`DEAD_ZONE_INNER_PCT` (2%, PID xả và, khi không có lệnh bám theo vận tốc, follower được retarget về pose *hiện tại* để không thể tiếp tục chạy theo goal cũ); **dải creep** tới rìa ngoài (độ dốc `DEAD_ZONE_CREEP_GAIN` = 0.12) để camera lững thững trôi về tâm thay vì đứng khựng — dừng cứng ở đây tạo cảm giác "camera an ninh" đi–dừng–đi; ngoài rìa ngoài là error đầy đủ.
3. **Velocity feedforward chính, PID phụ (smooth pursuit)** — lệnh chủ đạo là feedforward tỉ lệ với vận tốc pixel đo được của target (`VFF_GAIN` = 0.9): camera *khớp tốc độ vật* như mắt người bám mượt, kể cả khi sai số vị trí bằng 0. PID time-aware chống windup (KP cố tình nhỏ: 0.015 yaw / 0.02 pitch) chỉ sửa phần dư vị trí. Vật đang ở giữa nhưng di chuyển thì vẫn pan tiếp (không đóng băng trong dead zone). Output tổng clamp về `PID_OUTPUT_MAX_DEG` (5°).
4. **Hai profile saccade / pursuit** — mô phỏng mắt người: lệch > `SACCADE_OFFSET_FRAC` (22% bề ngang frame) thì follow worker chuyển sang profile **saccade** (`SACCADE_SMOOTH_TIME` 0.20s, `SACCADE_MAX_SPEED_DPS` 100) để lia nhanh về chỗ; lệch nhỏ dùng profile **pursuit** nặng (`SERVO_SMOOTH_TIME` 0.32s, `SERVO_MAX_SPEED_DPS` 55) — quán tính fluid-head. Một profile thỏa hiệp làm cả hai việc đều dở. State log ra `SACCADE` vs `CHASING`.
5. **Publish goal** — joint target tuyệt đối kết quả được giao cho servo worker (non-blocking).

### Servo worker (SmoothDamp follower)

`ServoFollower` (`servo_follow.py`) chạy worker trên thread riêng và liên tục ease các joint về phía goal mới nhất bằng **SmoothDamp** (`smooth_damp`, một follower critically-damped): mỗi joint mang vận tốc riêng, nên mọi cú di chuyển đều tăng tốc mượt và ease-out vào target, và một goal mới đến giữa cú di chuyển sẽ retarget mà không giật restart — chuyển động "film camera" điện ảnh. Worker thức dậy theo nhịp bị giới hạn bởi `SERVO_SUBSTEP_SLEEP` (30 ms), nhưng tính SmoothDamp từ thời gian monotonic thực tế đã trôi qua và cap ở `SERVO_SUBSTEP_MAX_DT_S` (60 ms) sau một lần scheduler/serial bị stall. Nó chỉ gửi một lệnh bus đa-joint khi có ít nhất một servo command đổi ít nhất `SERVO_COMMAND_MIN_DELTA` (0.08), chỉ gộp các thay đổi normalized rất nhỏ; final target luôn được gửi một lần.

Chuyển động phần cứng khi tracking: ở mỗi lần bắt đầu session, HAL luôn ghi rõ `TRACKING_GOAL_VELOCITY = 0` (unlimited) để xóa giới hạn vận tốc còn sót lại từ mode trước. Vì vậy software profile sở hữu tốc độ; cap cũ 150 steps/s ≈ 13°/s đã làm các đường cong SmoothDamp phẳng thành một chuyển động chậm đều. `TRACKING_ACCELERATION = 30` tạo ramp phần cứng nhẹ nhàng. Khi tracking dừng, HAL đọc pose vật lý vào state của animation rồi dispatch idle; interpolation bình thường của idle tiếp tục thẳng từ pose đó. Không còn bước trung gian quay về zero.

### Sửa drift & quản lý lock

- **Background YOLO re-detect** mỗi `YOLO_REDETECT_S` (1.5 s) trên một worker thread (không bao giờ block fast loop; kết quả gửi qua một queue `maxsize=1`). Bị buộc chạy ngay khi vật thể tiến gần rìa frame (>25 %) hoặc lần CSRT miss đầu tiên.
- **Miss-coast** — tracker miss trong lúc vật đang di chuyển (phẩy nhanh, motion blur) thì loop pan tiếp theo vận tốc alpha-beta cuối cùng tối đa `MISS_COAST_FRAMES` (6) frame miss (state `COAST`) rồi mới rơi về search sweep. Đứng khựng ngay miss đầu tiên là bảo đảm vật nhanh thoát khỏi frame trước khi redetect kịp về.
- **Reinit gating (kiểu SORT/ByteTrack)** — một re-detect chỉ reinit tracker khi nó đã rõ ràng phân kỳ, để tránh churn reinit làm servo quật qua lại:
  - **Area gate** `YOLO_AREA_GATE_MULT` (4.0) — loại một detection có diện tích >4× hoặc <¼ median của 5 cái gần nhất; đừng reinit về nó.
  - **Reinit debounce** `REINIT_COOLDOWN_S` (0.5 s) — rate-limit reinit; chỉ bypass khi lock rõ ràng đã mất (`center_dist > frame_diag × LOST_CENTER_FRAC` = 0.5).
- **Bbox-trust guard (bloat hold)** — khi ViT lock tan thành một box quá khổ thì centroid là rác, nên servo giữ nguyên thay vì đuổi theo nó:
  - `BBOX_FREEZE_RATIO` (1.0) — bbox ≥ diện tích cả frame ⇒ ViT đã tan.
  - `BLOAT_HOLD_MULT` (3.0) — bbox > 3× diện tích lock tin cậy gần nhất ⇒ hold và buộc re-detect.
- **Sàn confidence cho servo** — confidence ViT < `SERVO_MIN_CONF` (0.25) thì giữ servo (`LOW-CONF-HOLD`) kể cả khi detector vẫn đang confirm target; trước đây vùng conf 0.15–0.4 kèm confirm mới là vùng mù khiến servo đuổi theo một lock yếu (thường là ma). Tracker vẫn update và PID chạy lại khi confidence hồi.
- **Detector-gated trust** — nếu không detector nào xác nhận trong cửa sổ trust và confidence ViT < `TRACKER_TRUST_CONF` (0.4), giữ servo (`WAIT-YOLO`) thay vì đuổi một bóng ma; confidence ViT cao vẫn tiếp tục fire ngay cả khi không có detector confirm mới. Cửa sổ này **được tính theo latency đo được của detector**, không cố định: `trust_window_s()` trả về `max(TRUST_TRACKER_S, YOLO_REDETECT_S + 2 x latency + TRUST_MARGIN_S)`, với latency là EMA do background scan duy trì. Cùng một loop được phục vụ bởi các detector chênh nhau ba bậc độ lớn (YuNet ~30 ms, YOLO local ~0.4 s, remote ~0.55 s trung vị), nên một hằng số chỉ đúng cho cái nhanh nhất — và 2.5 s khiến mọi target chậm hơn nằm lì ở `WAIT-YOLO` dù vật hiện rõ trong khung. Cái sàn giữ hành vi với mặt người y như cũ.
- **Hold là hold thật** — mọi trạng thái hold (`LOW-CONF-HOLD`, `WAIT-YOLO`, `BLOAT-HOLD`, frame bị skip do low-confidence) đều retarget follow worker về pose *hiện tại* (`ServoFollower.hold()`). Trước đây hold chỉ ngừng publish goal mới, worker vẫn glide tiếp về goal cũ — nên arm vẫn "đuổi ma" thêm một nhịp sau khi lock đã hỏng.

### Chuyển đổi Pixel-sang-Degree

```
deg_per_px = CAMERA_FOV_DEG / frame_width          (same on both axes for square pixels)

dx = filtered_lead_x - frame_width/2   (positive = right)
dy = filtered_lead_y - frame_height/2  (positive = below)

yaw_step         = clamp(PID(soft_deadband(dx)) + VFF·vx·deg_per_px·dt,  ±5°)
pitch_correction = clamp(PID(soft_deadband(dy)) + VFF·vy·deg_per_px·dt,  ±5°)
```

### Hằng số tuning

| Hằng số | Giá trị | Mô tả |
|---------|---------|-------|
| `VISION_MAX_WIDTH` | 640 | Chiều rộng downscale cho ViT + detectors (0 = tắt) |
| `FAST_LOOP_FPS` | 15 | Tần số vòng lặp vision |
| `CAMERA_FOV_DEG` | 60 | FOV ngang, cho px→deg |
| `DEAD_ZONE_INNER_PCT` | 0.02 | Dải zero thật (servo nghỉ) |
| `DEAD_ZONE_CREEP_GAIN` | 0.12 | Độ dốc trôi lười trong dải creep |
| `DEAD_ZONE_YAW_PCT` / `_PITCH_PCT` | 0.07 / 0.05 | Soft dead zone theo tỉ lệ frame |
| `PID_YAW_KP` / `PID_PITCH_KP` | 0.015 / 0.02 | PID proportional gains |
| `PID_OUTPUT_MAX_DEG` | 5.0 | Số độ tối đa mỗi lần fire (yaw & pitch kết hợp) |
| `AB_ALPHA` / `AB_BETA` | 0.6 / 0.2 | Alpha-beta position/velocity gains |
| `AB_GATE_PX` | 200 | Loại một cú teleport centroid vượt residual này |
| `AB_LEAD_S` | 0.20 | Velocity lead (nhắm vượt trước target) |
| `VFF_GAIN` | 0.9 | Tỉ lệ vận tốc target được feed forward |
| `VFF_MAX_DT_S` | 0.20 | Cap trên dt mỗi lần fire cho feedforward |
| `VFF_MOVING_MIN_PXS` | 40 | Tốc độ target mà trên đó target ở giữa vẫn tiếp tục pan |
| `SERVO_SMOOTH_TIME` / `SERVO_MAX_SPEED_DPS` | 0.32 / 55 | Profile pursuit (nặng, fluid-head) |
| `SACCADE_SMOOTH_TIME` / `SACCADE_MAX_SPEED_DPS` | 0.20 / 100 | Profile saccade (lia nhanh) |
| `SACCADE_OFFSET_FRAC` / `SACCADE_EXIT_FRAC` | 0.22 / 0.12 | Ngưỡng vào/ra saccade (hysteresis — hết nhấp nháy cap tốc độ ở ranh giới) |
| `SERVO_SUBSTEP_SLEEP` / `SERVO_SUBSTEP_MAX_DT_S` | 0.030 / 0.060 | Chu kỳ worker thức dậy / bước SmoothDamp đo thực tế lớn nhất sau stall |
| `SERVO_COMMAND_MIN_DELTA` | 0.08 | Chỉ gộp các thay đổi normalized rất nhỏ; final target được gửi một lần |
| `TRACKING_GOAL_VELOCITY` | 0 (unlimited) | Được ghi rõ khi session bắt đầu để xóa hardware cap cũ; các profile SmoothDamp sở hữu speed envelope (150 steps/s ≈ 13°/s đã làm mọi đường cong ease thành một chuyển động chậm đều) |
| `TRACKING_ACCELERATION` | 30 | Ramp gia tốc phần cứng |
| `PITCH_WEIGHT_BASE/ELBOW/WRIST` | 0.10 / 0.90 / 0.0 | Phân bổ pitch qua các joint |
| `ELBOW_PITCH_SIGN` | -1.0 | Chiều elbow (phần cứng đảo) |
| `YOLO_REDETECT_S` | 1.5 | Khoảng thời gian background re-detect |
| `YOLO_AREA_GATE_MULT` | 4.0 | Loại re-detect có diện tích outlier |
| `REINIT_COOLDOWN_S` | 0.5 | Số giây tối thiểu giữa các lần reinit tracker |
| `BBOX_FREEZE_RATIO` | 1.0 | Bbox ≥ frame ⇒ ViT đã tan (hold) |
| `BLOAT_HOLD_MULT` | 3.0 | Bbox > 3× lock tin cậy ⇒ hold |
| `CONFIDENCE_THRESHOLD` | 0.15 | Dưới mức này = frame low-confidence |
| `LOW_CONF_WINDOW` / `LOW_CONF_STOP_COUNT` | 15 / 8 | Cửa sổ trượt: ≥8 frame low trong 15 frame gần nhất → dừng (đếm liên-tiếp cũ bị reset bởi mỗi frame nhấp nháy vượt ngưỡng, khiến ghost lock sống mãi) |
| `SERVO_MIN_CONF` | 0.25 | Sàn confidence để fire servo PID |
| `TRACKER_TRUST_CONF` / `TRUST_TRACKER_S` / `TRUST_MARGIN_S` | 0.4 / 2.5 / 0.5 | Detector-gated trust (xem trên). `TRUST_TRACKER_S` là **sàn**, không phải cửa sổ — cửa sổ được suy ra từng session theo latency đo được của detector |
| `YOLO_MAX_MISS` | 30 | Số lần CSRT miss liên tiếp trước khi retry |
| `MAX_TRACK_DURATION_S` | `HAL_TRACKING_MAX_DURATION_S` (10) | Timeout tự động dừng (mặc định 10 giây; cấu hình theo từng thiết bị) |
| `_LOCAL_IMGSZ` | 448 | Kích thước inference local YOLO. Đây CHÍNH LÀ độ phân giải hiệu dụng của detector — YOLO letterbox mọi input xuống giá trị này, nên với camera rộng 1280 thì 448 đưa vật tới model ở 0.35x còn 320 chỉ 0.25x. Vật nhỏ trên bàn (cup, book, phone) tới model ở ~30px với 320 nên bị bỏ sót. Đổi giá trị này thì phải export lại ONNX; kích thước được nướng vào file. (640 đo được 1.3–2.9 s trên torch — vẫn ngoài tầm.) |

Mọi knob nằm trong `hal/drivers/tracking/constants.py`. (Đường proportional chết `GIMBAL_*` / `EMA_ALPHA` đã bị xoá khi tách package.)

Đặt `HAL_TRACKING_MAX_DURATION_S` trong `/opt/hal/.env` của Lamp để chọn giới hạn thời gian thực cho một session; mặc định Lamp đã cài là `10`. Khởi động lại service `hal` sau khi đổi.

### Giới hạn vị trí servo

| Joint | Min | Max |
|-------|-----|-----|
| base_yaw | -135 | 135 |
| base_pitch | -90 | 30 |
| elbow_pitch | -90 | 90 |
| wrist_pitch | -90 | 90 |

## Điều kiện tự động dừng

| Điều kiện | Hành động |
|-----------|-----------|
| `confidence < 0.15` trong ≥8/15 frame gần nhất (cửa sổ trượt) | Dừng — mất target |
| Bbox co nhỏ dưới `DETECT_MIN_AREA_RATIO` | Dừng — ghost-lock trên một mảnh nhỏ |
| Bbox tràn frame + không detect trong 3 s | Buộc retry, rồi dừng nếu không phục hồi |
| Không detector confirm trong `STOP_NO_YOLO_S` (20 s) | Dừng — ghost tracking |
| CSRT miss `YOLO_MAX_MISS` (30) sau `MAX_TRACKING_RETRIES` (4) | Dừng — vật thể biến mất |
| Thời lượng tracking > `HAL_TRACKING_MAX_DURATION_S` (mặc định 10 giây) | Dừng — timeout để tiết kiệm motor/CPU |
| Single-click từ nút GPIO hoặc TTP223 | Dừng — user chủ động huỷ attention |

Lưu ý: một bbox lớn (ví dụ một người lấp đầy frame) **không** phải điều kiện dừng — PID chạy theo centroid, không phải kích thước bbox, nên một vật thể ở gần vẫn track. Khi tracking kết thúc, idle nội suy từ pose hiện tại đo được của cánh tay thay vì đưa tay qua zero trước — xem [Tương tác với các hệ thống khác](#tương-tác-với-các-hệ-thống-khác).

### Tự động dừng khi mất kết nối gateway/network

Object tracking được điều khiển bởi các cập nhật vision từ xa từ agent/cloud. Khi gateway WebSocket disconnect (mất cloud hoặc internet), thiết bị tự động dừng mọi servo tracking đang chạy — `runtimes/openclaw/service_ws.go` gọi `hal.StopServoTracking()` → HAL `POST /servo/track/stop` (best-effort, được guard bởi `SetUpCompleted`). Nếu không có cập nhật từ xa mới, tracking tiếp tục sẽ cứ nhắm thân về một target cũ mà nó không còn sửa được, nên nó bị dừng như một phản xạ an toàn. Idle animation local vẫn tiếp tục (thiết bị vẫn "sống", không đóng băng) và phục hồi (`/servo/track/stop`, stop/release) vẫn khả dụng. Xem `robots/lamp/SAFETY.md` → `## fail-safe states` (dòng Network/gateway loss, enforced).

## API Endpoints

Tất cả nằm dưới `/servo/track`.

### GET /servo/track/targets — Liệt kê target gợi ý

```json
{"targets": ["person", "cup", "bottle", "glass", "phone", "laptop", ...]}
```

Detection là open-vocabulary qua YOLOWorld (và YuNet cho khuôn mặt) — mọi text đều được, danh sách này chỉ là gợi ý.

### POST /servo/track — Bắt đầu tracking

`target` nhận hoặc một string đơn hoặc một list các label ứng viên. Khi truyền một list, label không rỗng đầu tiên được dùng. Hữu ích khi caller (ví dụ một LLM skill) không chắc label chính xác nào sẽ match.

```json
// Auto-detect, single label
{"target": "cup"}

// Auto-detect, list of candidate labels (preferred from LLM skills)
{"target": ["cup", "mug", "coffee cup"]}

// Manual bbox (skip detection — target is for display only)
{"bbox": [190, 50, 170, 300], "target": "cup"}

// Response
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [190, 50, 170, 300],
  "confidence": 1.0
}
```

### POST /servo/track/stop — Dừng tracking

```json
{"status": "ok", "tracking": false}
```

### GET /servo/track — Kiểm tra trạng thái

```json
{
  "status": "ok",
  "tracking": true,
  "target": "cup",
  "bbox": [195, 55, 175, 295],
  "confidence": 0.612
}
```

### POST /servo/track/update — Re-init bbox

Re-init thủ công tracker với một bbox mới mà không dừng session (background YOLO re-detect tự xử lý drift; cái này dành cho caller muốn kiểm soát tường minh).

```json
{"bbox": [250, 160, 75, 95], "target": "cup"}
```

## Luồng End-to-End

### Happy path

```
1. User: "Lamp, follow the cup"
2. Agent calls POST /servo/track {"target": "cup"}
3. HAL internally:
   a. Freezes servos 0.3s and snapshots a sharp frame
   b. Detects "cup" (local YOLOv8n, or remote YOLOWorld) → bbox
   c. TrackerVit init uses the same frame + bbox (coordinates match)
   d. Starts the vision loop + servo worker
4. Servo pans smoothly to follow the cup, background YOLO corrects drift
5. User: "OK stop" → agent calls POST /servo/track/stop
6. Idle nội suy thẳng từ pose tracking cuối cùng
```

### Tự động dừng khi mất

```
1. Object leaves frame or is occluded
2. Confidence TrackerVit ở dưới 0.15 trong phần lớn cửa sổ gần nhất (hoặc lock ViT tan rã)
3. Background YOLO can't re-find it → after the guards trip → auto-stop
4. Idle nội suy thẳng từ pose tracking cuối cùng
5. Agent can notify user or re-issue the follow command
```

## Overlay Camera Stream

Khi tracking đang chạy, MJPEG stream (`/camera/stream`) vẽ:
- Bounding box màu xanh quanh vật thể được track
- Label target phía trên box

## Web UI

Camera section hiển thị:
- **Vision Tracking card** — target input, bbox input, các nút Start/Stop/Status
- **Stream badge** — "LIVE" hoặc "TRACKING: {target}"
- **Confidence** — hiện trong panel thông tin tracking
- **Polling** — status refresh mỗi 3 giây

## Dependencies

- `opencv-python>=4.8.0` (đã có trong `pyproject.toml`)
- `ultralytics` — inference local YOLOv8n
- `vittrack.onnx`, `yolov8n.pt`, `face_detection_yunet_2023mar.onnx` — đã check vào `hal/drivers/tracking/models/`, nên deploy là một lệnh rsync và thiết bị không cần internet lúc boot.

> **Đừng export YOLO sang ONNX để tăng tốc.** Đã thử và đã revert. Đo trên lamp-0c89 ở imgsz 448, xen kẽ: `.pt` qua torch 310/360/427/526 ms (min/p50/p90/max, n=25) so với `.onnx` qua onnxruntime 179/245/561/607 ms — ONNX thắng trung vị, thua phần đuôi, và lần đo tuần tự đầu tiên cho kết quả ngược lại. Tranh chấp CPU với camera/voice/servo loop của HAL trên 4 nhân át mất chênh lệch. Nó cũng không tiết kiệm dependency nào (`ultralytics` vẫn kéo torch và chính nó là thứ nạp `.onnx`), trong khi tốn 6 MB và nướng cứng `_LOCAL_IMGSZ` vào bản export.
- `requests` (đã có trong project)
- **YOLOWorld API** — DL backend tại `{DL_BACKEND_URL}/detect/yoloworld` (chỉ open-vocab fallback)

## Tương tác với các hệ thống khác

| Hệ thống | Trong khi tracking | Sau khi tracking |
|----------|--------------------|------------------|
| Servo idle animation | Bị chặn (`_hold_mode`) | Tiếp tục |
| `/servo/play` | Bị chặn bởi `_hold_mode` | Tiếp tục |
| Sensing (face, motion) | Tiếp tục — chia sẻ camera | Tiếp tục |
| Camera stream overlay | Vẽ bbox xanh | Stream bình thường |
| TTS | Tiếp tục bình thường | Tiếp tục bình thường |

Việc quay lại idle là một **dispatch tường minh**, không phải hệ quả phụ của việc xoá cờ tracking. Khi `_tracking_active` đang bật, `AnimationService._continue_playback` bỏ recording đang chạy dở (`_current_recording = None`) để không có gì tranh servo với tracker. Xoá cờ không đặt lại giá trị đó: event loop return ngay ở guard đầu tiên (`if not self._current_recording`), nên nếu không dispatch thì cánh tay đứng cứng tại pose tracking cuối với torque vẫn bật cho tới lệnh emotion hoặc play kế tiếp. Khối `finally` của `_track_loop` đọc pose vật lý đó vào `_current_state` trước, rồi gọi `animation_service.dispatch("play", animation_service.idle_recording)`. Dùng dispatch thay vì `_handle_play` để playback vẫn thuộc event thread; state vừa seed giúp idle nội suy thẳng từ pose nơi tracking dừng.

## Ghi chú hiệu năng

- Sàn CPU của fast-loop trên Allwinner A523 là chi phí ViT inference + detector; frame downscale (`VISION_MAX_WIDTH`) và local imgsz là các đòn bẩy chính. Lưu ý `VISION_MAX_WIDTH` KHÔNG phải đòn bẩy cho *độ chính xác* detect: YOLO letterbox xuống `_LOCAL_IMGSZ` bất kể input to bao nhiêu, nên hạ scale frame trước chỉ đổi chi phí của tracker, không đổi tỉ lệ mà detector nhìn thấy.
- Độ mượt chuyển động đến từ servo worker tách rời + SmoothDamp + velocity feedforward; alpha-beta filter + reinit gating giữ cho bản thân goal ổn định để follower không đuổi theo nhiễu.
- Vật thể nhỏ/xa (ví dụ một cái ly ở đầu phòng) có thể vượt độ phân giải của cả detector local lẫn remote — đây là giới hạn perception, không phải bug điều khiển.
</content>
</invoke>

---

## Look-aim — ngắm đầu trước khi một câu hỏi thị giác chụp ảnh

Tách biệt với phần theo dõi vật thể ở trên, và do một trigger khác kích hoạt.

Tool `look` của realtime **không có tham số**: nó chụp đúng thứ đầu đang hướng tới
(`orchestrator.py` — *"the model just signals intent to look; the device grabs the current frame"*).
Nên một câu hỏi thị giác — *"tôi đang cầm gì đây?"* — có thể được trả lời rất tự tin từ một tấm ảnh
bức tường. `hal/drivers/tracking/aim.py` căn giữa đối tượng trước.

| | |
|---|---|
| **Trigger** | tool `look` được gọi — **không phải** hội thoại thông thường |
| **Phạm vi** | chỉ yaw |
| **Ngân sách** | `HAL_LOOK_AIM_DEADLINE_S` (8 s); hết hạn thì chụp từ đúng chỗ nó tới được |
| **Tắt** | `HAL_LOOK_AIM=false` |

Hội thoại thông thường không đổi: thân máy vẫn đứng yên suốt pha lắng nghe và suy nghĩ như trước.
Chỉ một lời gọi `look` mới giải phóng nó, vì đó đúng là khoảnh khắc thiết bị được yêu cầu tường minh
là hãy nhìn vào một vật.

**Thân máy được sở hữu trong suốt cả lượt look.** Từ lúc pha ngắm bắt đầu cho tới khi màn trập đóng,
`servo_ownership()` giữ một suất đếm tham chiếu trong đúng cái khóa `_tracking_active` mà vision
tracker vẫn dùng, khóa này chặn **toàn bộ** animation servo của emotion (`routes/emotion.py`) và
khiến vòng animation bỏ luôn bản ghi đang phát dở.

Đây không phải phần đánh bóng cho đẹp. Các preset emotion phát những tư thế **đã ghi sẵn**, tuyệt đối
trên mọi khớp — kể cả `wrist_roll` — nên chỉ cần một cái rơi vào giữa pha ngắm và lần chụp là đầu bị
đặt lại hoàn toàn, và khung hình cho ra đúng chỗ animation đã đỗ lại chứ không phải người dùng. Một
phản ứng "tò mò" rơi vào giữa câu hỏi là đủ để chụp lên trần nhà. `nudge()` chiếm quyền được một
animation *đang* phát, nhưng không chặn được cái được gọi *sau đó* — mà đó lại đúng là khoảng thời gian
lần chụp nằm trong.

**Quyền sở hữu được đếm tham chiếu, không lưu rồi khôi phục.** `servo_ownership()` gọi
`acquire_body()` khi vào và `release_body()` khi ra, nên nó chỉ nhả đúng phần mình giữ và một lượt
look không bao giờ kết thúc một phiên bám vật thể đang chạy thật. Trước đây nó lưu giá trị cũ rồi ghi
lại, và cách đó mất cập nhật mỗi khi hai trong sáu điểm gọi chồng lên nhau — chúng chạy trên ba
thread khác nhau (gaze watcher, tool `look` realtime, và sweep). Bên ra sau cùng ghi đè lại một giá
trị `True` đã cũ, làm khóa kẹt cứng dù không ai giữ, âm thầm chặn toàn bộ animation emotion và cả
gaze cho tới khi tình cờ có một phiên face-track chạy và xoá nó (#312). `acquire_body`/`release_body`
là một phần của hợp đồng `MotionService` (`hal/drivers/motors/base.py`), nên mọi thân máy đều trả lời
về quyền sở hữu theo cùng một cách.

Một lần kẹt cũng không còn sống lâu hơn nguyên nhân gây ra nó: cờ bật mà không có bên ghi nào là
trạng thái không thể xảy ra, nên `AnimationService` đếm giờ và tự xoá sau 30 s kèm một dòng `ERROR`
(`hal/drivers/motors/tracking_wedge.py`).

**Vì sao vòng lặp căn giữa chỉ dùng yaw.** Quy ước dấu của yaw được chép từ quy ước đã kiểm chứng
thực nghiệm của tracker (`dx>0` → `base_yaw` tăng). `AnimationService.nudge()` điều khiển
`base_pitch`, trong khi tracker phân bổ pitch trên base/elbow/wrist — nên dấu của pitch **chưa** được
kiểm chứng trên đường này, và pitch đảo dấu là lỗi codebase này đã từng mắc một lần (xem
`servo_follow.command_pid`).

Việc khôi phục bearing ở ưu tiên 3 là ngoại lệ, và nó an toàn vì một lý do cụ thể: nó gửi một tư thế
**tuyệt đối** qua `move_and_hold`, không phải nudge tương đối. Mục tiêu tuyệt đối thì không có dấu nào
để sai. Đó chính là thứ cho phép cái đầu đang chúi xuống sàn lấy lại đúng độ cao — nếu chỉ chỉnh yaw,
nó sẽ quét sàn theo vòng tròn dù hướng có đúng tới đâu.

Một lượt aim đã di chuyển đầu thì đỗ body y như search (`nudge` và khôi phục bearing đều kết thúc
bằng `move_and_hold`), nên `aim_for_look` lên lịch cùng `release_to_idle_later(HOLD_AFTER_FIND_S)`
từ hàm dựng kết quả mỗi khi `iterations` hoặc `bearing_steps` khác 0 — xem *Trả body về idle* ở
mục quét tìm kiếm để biết các guard. Gaze thường lấy lại body khi có mặt trong khung, vì thế chỗ
này ít lộ hơn; không có mặt thì nó kẹt y hệt search.

**Thứ tự ưu tiên:**

1. **Thấy người** → căn giữa. Ưu tiên khung bao người hơn khung bao mặt: vật giơ lên hay che mất mặt
   nhưng hiếm khi che cả người, và lấy khung cả người thì bao gồm luôn thứ họ đang cầm.
2. **Không thấy gì, nhưng vài giây trước vừa xác nhận có người ở đúng tư thế này** → **giữ nguyên và
   chụp.** Coi việc biến mất là *bị che khuất, không phải vắng mặt* — đó đúng là hình ảnh của một vật
   giơ lên dưới góc nhìn của bộ phát hiện, và quay đi lúc đó là bỏ rơi đúng thứ người dùng vừa hỏi.
3. **Không thấy gì và gần đây cũng không thấy** → về thẳng **tư thế** đã ghi nhớ — cả hướng lẫn dáng
   — trong một lệnh tuyệt đối duy nhất. Trước đây nó đi từng nhịp `BEARING_STEP_DEG` và phát hiện lại
   giữa các nhịp để không đi lướt qua người đang đứng giữa đường; các nhịp này đã bị bỏ vì ống kính
   nhìn được ~110°, nên ai đứng ở giữa thì đã nằm trong khung hình từ trước khi đầu nhúc nhích. Chúng
   không mua thêm vùng phủ nào mà mỗi nhịp tốn một lần detect cộng một lần chờ lắng, khoảng một giây,
   trừ thẳng vào ngân sách của aim.
4. **Hết hạn chót** → chụp từ đúng chỗ đầu tới được. Ở đây không bao giờ quét.

Mọi chuyển động đều đi qua `nudge()`, nên `max_speed` trong `SAFETY.md` sẽ kéo dãn thời gian di chuyển
chứ không bị bỏ qua để kịp hạn chót. Một cú click đơn trên nút vật lý sẽ hủy nó
(`button_actions.py`), vì cử chỉ đó nghĩa là "dừng lại và chú ý vào tôi".

#### Phát hiện nào mới được tính là "người đang hỏi"

Một khung bao `person` là chưa đủ. Ngưỡng toàn cục của bộ phát hiện là `DETECT_MIN_CONFIDENCE = 0.15`,
cố tình để lỏng vì nó được tinh chỉnh cho **tracker**, nơi mất khóa vào cái điện thoại đặt nghiêng còn
tệ hơn một lần nhận nhầm. Aim cần đánh đổi ngược lại — nhận nhầm là quay đèn vào tường — nên aim áp
hai cổng của riêng nó:

| cổng | mặc định | loại bỏ |
|---|---|---|
| `HAL_LOOK_AIM_MIN_PERSON_HEIGHT_FRAC` | 0.15 | đồng nghiệp ở đầu kia phòng (đo được 0.10 chiều cao khung) |
| `HAL_LOOK_AIM_MIN_FACE_HEIGHT_FRAC` | 0.08 | khuôn mặt xa bị nhận nhầm (đo được 0.035) |
| `HAL_LOOK_AIM_MIN_CONFIDENCE` | 0.5 | nhiễu độ tin cậy thấp, ví dụ người hiện trên màn hình |

Kích thước đo theo **chiều cao**, không theo diện tích hay chiều rộng: người ở gần thường bị mép khung
cắt mất bên trái hoặc phải, nhưng chiều cao biểu kiến vẫn tỉ lệ với khoảng cách.

Một phát hiện bị loại được báo là **không có đối tượng nào**, chứ không phải một mục tiêu — nên aim
rơi xuống nhánh giữ-nguyên hoặc tra bearing thay vì quay sang một người lạ ở cuối phòng. Khuôn mặt do
YuNet trả về, vốn tự áp ngưỡng riêng và không báo độ tin cậy, nên ở đó chỉ có cổng chiều cao.

#### Tự hiệu chuẩn pixel sang độ

Aim **không** tin vào một hằng số FOV cố định. Nó đo số độ trên mỗi `dx_frac` từ chính kết quả cú
chuyển động vừa rồi của nó, rồi dùng cho lần chỉnh kế tiếp; `HAL_LOOK_AIM_FOV_DEG` (100°) chỉ là phỏng
đoán cho bước đầu tiên khi chưa có phép đo nào.

Nó tồn tại vì không hằng số nào đúng được. Ống kính là mắt cá: cùng một thiết bị đo được **91° gần tâm
khung và 229° ở rìa**. Hằng số chỉnh cho tâm sẽ bò rất chậm ở rìa (bốn vòng lặp vẫn chưa căn giữa rồi
hết hạn chót); chỉnh cho rìa thì vọt quá ở tâm và dao động.

Có các chốt chặn, vì lấy một dịch chuyển nhỏ chia cho một cú di chuyển nhỏ sẽ biến nhiễu của bộ phát
hiện thành một tỉ lệ điên rồ: một bước bị bỏ qua trừ khi đầu đã quay >3° và đối tượng dịch >0.02 khung,
và trừ khi chiều dịch **cùng** chiều với lệnh chỉnh — dịch ngược chiều nghĩa là người đó đi chỗ khác
hoặc bộ phát hiện nhảy sang vật khác, không phải phép đo quang học. Kết quả bị kẹp trong 40–250° và
giảm chấn bởi `SCALE_SAFETY` (0.7), cố tình thiên thấp: phép đo lấy ở độ lệch tâm hiện tại nhưng đem
tiêu ở độ lệch nhỏ hơn, mà thiếu thì chỉ tốn thêm một bước còn thừa thì dao động.

#### Thời điểm chụp

Hai khoản chi phí từng nằm trong ngân sách của aim, nay đã được đưa ra ngoài:

- **Làm nóng bộ phát hiện.** Lần `detect()` đầu tiên nạp model kiểu lười và tốn ~9 s trên thiết bị —
  tự nó đã đủ thổi bay cả hạn chót lẫn watchdog của lượt realtime. Giờ nó được làm nóng sẵn trên một
  luồng nền lúc HAL khởi động (`server.py`), nên lần look thật đầu tiên chạy nhanh ngang mọi lần sau.
- **Khung hình cũ.** Đọc `last_frame` ngay sau một cú di chuyển sẽ trả về ảnh **trước khi di chuyển**,
  nên lần chỉnh kế tiếp được tính từ một tư thế mà đầu đã rời khỏi. Trên thiết bị, việc này tạo ra sáu
  lệnh chỉnh +12,3° giống hệt nhau với `dx` đứng yên ở 0,241 trong khi đầu đi hết 61°. Giờ aim giữ
  consumer camera suốt cả lần aim (không có nó thì thiết bị không chụp ở full FPS) và bắt buộc phải có
  khung hình đóng dấu sau khi servo đã lắng. **Không có phản hồi mới thì không di chuyển.**

Bản thân cú chụp dùng `capture_still`, vốn đóng băng servo và chờ yên tĩnh. Thời gian chờ lắng của nó
co giãn theo độ lớn của lần chỉnh cuối (0,3 s nền, +0,0067 s/độ, chặn trên 0,5 s), vì một lần aim hết
hạn chót sẽ thoát ra ngay sau một cú quay lớn, mà cần đèn vẫn còn rung quá mốc 300 ms cố định — đó
chính là khác biệt giữa ảnh nét ở các lần aim căn được giữa và ảnh nhòe ở các lần hết hạn chót. Trần
này cố tình để chặt: độ trễ đó người dùng phải chờ trước khi nghe được câu trả lời.

#### Gỡ lỗi một lần look

Mặc định tắt; khi tắt thì mọi hook chỉ là một phép kiểm tra bool đã cache, nên để nguyên trong code
không tốn gì.

```bash
HAL_LOOK_DEBUG=true          # thư mục trace mỗi lần look, dưới drivers/tracking/look_logs/
HAL_LOOK_DEBUG_FRAMES=false  # giữ trace, bỏ qua ảnh JPEG từng bước
```

Mỗi lần look ghi ra `<timestamp>_<status>/` gồm:

| file | trả lời câu hỏi gì |
|---|---|
| `step_NN_*.jpg` | bộ phát hiện đã khóa vào cái gì ở mỗi vòng lặp — khung xanh, vạch xanh ở tâm khung bao, vạch đỏ ở tâm khung hình. Khoảng cách giữa hai vạch **chính là** `dx`. |
| `capture.jpg` | khung hình thực sự đã gửi cho model |
| `result.json` | chuỗi quyết định: mỗi bước có `saw` / `dx_frac` / `conf` / `scale` / yaw đã lệnh / tư thế thu được, kèm bearing đã tra |
| `profile.json` | thời gian từng chặng, và `waiting_on_model_ms` |

Trạng thái trong tên thư mục (`OK_realtime_handled`, `OK_delegated`, `OK_fallback`) cho biết nhánh nào
đã trả lời lượt đó, nên một câu trả lời tệ có thể quy trách nhiệm trước khi mở bất cứ file nào.

`waiting_on_model_ms` là con số nên đọc đầu tiên: nó bằng tổng trừ đi mọi thứ thiết bị tự làm, và nó
tách bạch "lamp chậm" với "lamp xong trong 2 s rồi ngồi chờ model 24 s". Các chặng con nằm lồng trong
chặng cha và bị loại khỏi tổng của thiết bị, nên phần dư là trung thực. Cùng bộ số đó xuất hiện trên
một dòng log `LOOK-PROFILE` mỗi lần look.

### Quét tìm kiếm — bốn đường vào, đường nào cũng đủ thong thả

Khác với look-aim, và vẫn cố ý nằm ngoài đường chụp: pha ngắm chạy trong một lượt hội thoại đang diễn
ra với hạn chót, còn một pha quét mất vài giây. Điều đã thay đổi là "thong thả" giờ bao gồm cả hai
trường hợp đèn tự quyết định. Pha quét được vào khi:

- người dùng yêu cầu thẳng — *"bạn đang ở đâu?"*, *"tìm tôi được không?"*, *"tìm bàn phím của tôi"*
  (`skills/servo-control`). Trường hợp này là một lệnh **curl blocking ngay trong lượt**, không phải
  marker `[HW:/servo/search:...]`. Marker chỉ bắn sau khi phần text trả lời đã được viết xong, nên
  một lần tìm kiếm chạy bằng marker sẽ kết thúc vào một lượt đã đóng: đèn quét, tìm thấy vật, rồi câu
  trả lời không đi tới đâu cả. Đó cũng là một lệnh gọi dài 40 s đối đầu với client 5 s của
  `fireHWCall` — client này trước đây bỏ request trước cả khi kịp chạy `flow.Log` của chính nó, nên
  pha quét thậm chí không hiện ra trong Monitor. Agent chờ body rồi trả lời dựa trên đó, đúng theo
  cách `/api/vision/look` vẫn đang làm.
- họ đồng ý với đề nghị sau một lần nhìn thất bại — *"Tôi không thấy nó. Bạn có muốn tôi quay quanh tìm thử không?"*
- **look-aim sắp bỏ cuộc** — trước khi `look_lost` tuyên bố *"Tôi không tìm thấy bạn"*, câu mà đến giờ
  nó vẫn nói sau khi mới chỉ quay về một bearing đã ghi nhớ. Bearing là phỏng đoán về nơi người ta
  *từng* ở, không phải một lần tìm, nên câu đó phải được xứng đáng. Hạn chót của pha ngắm **ngừng đếm**
  trong suốt thời gian quét (`t_end += time.monotonic() - swept_at` trong `aim_for_look`): hạn chót tồn
  tại để một lượt đang chạy không bao giờ đứng im trong *im lặng*, câu thông báo `look_searching` đã lo
  chuyện đó rồi, và tính pha quét vào một ngân sách nó không thể vừa nghĩa là không bao giờ được quét.
- **watcher gaze ở một mình quá lâu** — `HAL_GAZE_SWEEP_AFTER_S` (30 s) không thấy ai, hoặc một lần
  repoint đã quay về bearing mà không thấy ai ở đó. Đường này không ai yêu cầu, nên nó là đường duy
  nhất có cooldown — xem *Tự quay quanh tìm*.

`POST /servo/search` — quét tìm một đối tượng. Body (tất cả đều tuỳ chọn): `{"target": "cup", "exhaustive": true}`.

- `target` mặc định là `"person"`. `person`/`face` dùng chính sách chọn người gần nhất kèm dự phòng
  khuôn mặt; mọi danh từ khác đi qua đúng chuỗi YOLOv8n/YOLOWorld mà `/servo/track` đang dùng. Trước
  đây tham số target được hàm nhận rồi bỏ đi — mọi lần quét đều tìm người, nên "look around for my
  keyboard" kết thúc ngay ở người đầu tiên đi ngang qua.
- `exhaustive` mặc định `false`, tức trả về ngay ở lần nhìn thấy đầu tiên — đúng cho câu "where are
  you?" và cho mọi lần tìm đồ vật. `true` là **khảo sát người**: đi trọn vòng nhìn tại mọi bearing
  (chế độ duy nhất có nhìn lên trên đường chân trời), đếm số lần thấy rồi quay về chỗ cũ. Nó
  **bị bỏ qua khi target là đồ vật** và có log rõ: một agent từng truyền nó cho "find my doll" và
  nhận về một cái đèn thấy búp bê năm lần, quay về nhà, rồi báo là tìm thấy mà không có ảnh. Tìm
  một món đồ luôn dừng ở lần thấy đầu tiên, canh giữa vào nó và giữ nguyên hướng đó.
- Độ phủ là `số bearing x số lần nhìn mỗi bearing`: 3 x 6 = **18 lần nhìn** ở chế độ thường,
  3 x 9 = **27** ở chế độ exhaustive. Hãy tính khoảng **2 giây mỗi lần nhìn**.

Response là một body có cấu trúc, không phải một chuỗi văn xuôi duy nhất:

```json
{"status": "ok",
 "message": "found keyboard at yaw +85 after 7 look(s) across 2 bearing(s)",
 "found": true, "target": "keyboard", "kind": "keyboard",
 "found_at_yaw": 85.0, "found_at_roll": -45.0, "centred": true,
 "image_path": "/root/.codex/media/hal-snapshots/snap_1757500000000.jpg",
 "looks_visited": 7, "bearings_visited": 2}
```

- `looks_visited` đếm số LẦN NHÌN còn `bearings_visited` đếm số vị trí của đế. Đó là hai đại lượng
  khác nhau và được báo tách riêng: trường này từng tên là `stops_visited`, đếm số lần nhìn, nhưng
  lại được render thành `"after N stop(s)"` trong khi `MAX_STOPS = 3` — nên một pha quét exhaustive
  thành thật báo "after 27 stop(s)" trên tổng tối đa 3, và agent thì thuật lại đúng theo chuỗi đó.
- `centred` cho biết bước hiệu chỉnh tinh có vào được deadband trên **cả hai trục** hay không.
  `false` vẫn có nghĩa là **tìm thấy** — chỉ là hướng ngắm bị lệch. Khi trúng, pha quét chạy
  `aim.centre_on_box` *trước* `_straighten_head_onto` (vật thể chắc chắn đang trong khung từ đúng tư
  thế đó; làm thẳng đầu trước từng được quan sát trên thiết bị là làm mất dấu), rồi làm thẳng đầu từ
  đúng chỗ bước hiệu chỉnh để lại đế. Vòng lặp này là hiệu chỉnh yaw của `aim_for_look` cộng với
  hiệu chỉnh pitch của gaze — `GAZE_PITCH_DEG_PER_FRAME` / `GAZE_PITCH_MAX_STEP_DEG` của gaze, dấu
  đã kiểm chứng trên thiết bị (box nằm trên tâm thì camera ngẩng **lên**, tức chiều *giảm* của các
  khớp pitch) và `servo_follow.distribute_pitch` chia cho base/elbow/wrist — phát ra thành một lệnh
  di chuyển tuyệt đối duy nhất. Trước tất cả những điều này, đầu được hướng tới `yaw + roll`, tức
  *hướng nhìn* của điểm dừng, để lại một phát hiện ở rìa khung lệch trục ~50° trong khi lệnh gọi
  vẫn báo là tìm thấy. Mọi lối thoát đi sau một lần di chuyển đều lấy thêm một frame mới
  (`_final_look`) để kết quả mô tả nơi đèn đang chỉ *lúc này*, chứ không phải frame trước lần di
  chuyển cuối. Probe là `_sticky_probe`: lấy mọi ứng viên qua `detect_candidates`, ứng viên gần box
  trước nhất thắng, và một ứng viên **dịch ra xa** tâm ở cùng một phía — hoặc xa hơn mức một bước có
  thể mang nó đi — là một cá thể khác và bị tính là một lần trượt. Hai bàn phím trên cùng một bàn
  từng khiến vòng lặp đuổi theo cái nào có điểm cao hơn ở mỗi frame. Vòng lặp **không** chấm điểm
  bearing đã ghi nhớ; pha quét tìm thấy đồ vật cũng thường xuyên như tìm thấy người, và dạy cho
  estimator rằng bàn phím là nơi người dùng ngồi chính là cách bearing mục ruỗng một cách âm thầm.
- `image_path` là frame trúng đích, được ghi vào `media/hal-snapshots` của runtime đang hoạt động —
  đúng cái pool có giới hạn và xoay vòng mà `/camera/snapshot?save=true` đang dùng (`_SNAPSHOT_MAX`
  = 20). Frame này có vẽ sẵn box của vật thể phát hiện được, qua
  `look_debug.encode_annotated(..., centre_lines=False)`: cái box mới là câu trả lời, còn hai đường
  dx cao hết khung màu xanh/đỏ là phần tính toán nội bộ của aim và sẽ nằm chồng lên nhau ngay giữa
  một frame đã canh giữa. Frame và box luôn đến từ **cùng một** lần chụp — vẽ box đo trước khi hiệu
  chỉnh lên frame chụp sau khi hiệu chỉnh sẽ đặt hình chữ nhật nằm cạnh vật thể. Khi vòng lặp canh
  giữa không lấy được frame nào (không có frame mới, bị abort, nudge lỗi), pha quét lùi về dùng
  frame đã kích hoạt lần trúng, nhờ vậy một lần tìm thấy luôn có cái để hiển thị. Khi không tìm
  thấy thì không ghi gì cả: `image_path` là `null`.
- Đường dẫn này được os-server hiển thị cho người dùng dưới dạng thumbnail; agent không đọc được file JPEG.

Tại mỗi bearing, đế đứng yên và cái đầu đi một vòng nhìn — tâm, trái, vòng qua đáy, ra phải, và (chỉ ở
chế độ exhaustive) vòng lên trên. Các góc chéo dùng **trọn** roll và **trọn** pitch chứ không phải
cos(45) của mỗi trục, tức một hình vuông bo góc chứ không phải hình tròn: nhờ vậy mỗi góc nhìn được xa
sang bên đúng bằng các lần nhìn trái/phải và xuống thấp đúng bằng lần nhìn đáy, tức thêm vùng đất mới
thay vì phủ lại phần giữa.

**Chỉ `wrist_roll` và `wrist_pitch` di chuyển trong một lần nhìn.** Đế chỉ xoay một lần cho mỗi bearing
và cánh tay không bao giờ tự đổi dáng. Một thiết kế trước đó rải độ nghiêng lên `base_pitch`,
`elbow_pitch` và `wrist_pitch` qua `servo_follow.distribute_pitch` — đúng cho một hiệu chỉnh tracking,
sai cho một phép quét: quan sát trên thiết bị 2026-09-09, tầng hướng lên đẩy `elbow_pitch` tới +35.8 và
`base_pitch` xuống +10.6, vươn cánh tay lên và ra sau đủ để trông mất thăng bằng, đồng thời bắt đế đi
lại toàn bộ cung cho từng tầng. `distribute_pitch` phân bổ theo tầm hoạt động của từng khớp; không có
gì trong đó biết về thăng bằng của cả cánh tay.

Nửa trên của vòng bị kẹp theo `WRIST_PITCH_MIN`: khi nghỉ ở khoảng −73 thì chỉ còn ~16 độ headroom, ít
hơn 25 độ mà vòng nhìn cần. Đo trên lamp-ac82 ngày 2026-09-09, `wrist_pitch` đạt −89.6 khi lên và −16.6
khi xuống mà không hề kẹt, nên `PITCH_TRAVEL_MIN/MAX` trong `constants.py` (−33..+32) không mô tả đúng
cánh tay này.

**Ba điểm dừng: bearing đã ghi nhớ trước, rồi sang phải, rồi sang trái** — `seed`, `seed+90°`,
`seed−90°`, bị kẹp vào giới hạn cơ khí chứ không bị loại bỏ. Seed đi trước vì pha quét dừng ngay ở đối
tượng ĐẦU TIÊN nhìn thấy, mà "đầu tiên" phải là người được hỏi tới: với thứ tự thuần trái-sang-phải,
pha quét đã tìm thấy một đồng nghiệp ở bàn khác (yaw −102°) trong khi người dùng ngồi ngay tại seed,
−12°, chỗ mà nó không bao giờ tới. Sau seed thì sang phải rồi sang trái, vì để đế lắc qua lắc lại quanh
tâm trông như bồn chồn khi đầu đèn cũng đang ngó quanh ở từng điểm dừng. Một lần đảo chiều trên đường
đi là đủ.

**Kết thúc thì tay dừng ở đâu.** Không tìm thấy ai → quay về đúng tư thế lúc bắt đầu quét, thay vì
đóng băng ở chỗ cái nhìn cuối cùng bỏ lại. Bị huỷ → cũng vậy: một cú nhấn nút nghĩa là "ngừng tìm và
để ý tới tôi", mà tư thế một pha quét bị cắt ngang đóng băng lại không phải tư thế nghỉ — đầu có thể
đang nghiêng 45°, mặt hướng vào tường. Tìm thấy → đầu được dựng thẳng lại bằng cách xoay ĐẾ đúng bằng
góc đầu đang nghiêng, nên camera vẫn hướng vào đối tượng mà đầu thì ngay ngắn.

**Khi chưa có bearing** — máy mới, hoặc bearing vừa bị reset — pha quét trước hết đưa tay về đúng tư
thế của bản ghi idle thay vì bắt đầu từ chỗ nó đang đứng. Một vòng lặp vừa dắt đầu đèn đi lòng vòng
không để lại tư thế nào do ai chọn cả, và quét từ một camera đang chúc xuống bàn thì có kỹ đến mấy
cũng là kỹ ở sai nửa không gian. Tư thế idle vốn dĩ là tư thế đèn được thiết kế để nghỉ, nên bảo đảm
"không chúc xuống sàn" đến từ chính tư thế đó, không cần thêm một phép kiểm tra pitch riêng.

**Ở mỗi điểm dừng, đầu đèn ngó quanh** — `wrist_roll` sang −45°, 0°, +45°, luôn theo chiều đó. Sự mượt
mà đến từ THỨ TỰ các điểm dừng chứ không phải từ việc đảo chiều đầu: một điểm dừng kết thúc khi đang
nhìn về `yaw+45°`, và điểm dừng kế bên phải mở ra ở `yaw+90°` với đầu ở −45° — *cùng một hướng nhìn*.
**Cú xoay đế và cú xoay đầu được gửi trong CÙNG một lệnh**, nên hai chuyển động triệt tiêu nhau và
camera giữ nguyên hướng trong khi cây đèn tự sắp xếp lại bên dưới. Nếu xoay đế trước rồi mới xoay đầu,
hướng nhìn sẽ văng ra `yaw+135°` rồi quay lại — đo trên máy thật là +48° → +138° → +48°, một cú lắc
90° đi-rồi-về ở mỗi lần chuyển điểm dừng. Đó cũng là lý do đế có thể bước 90° mà không để lại khe hở: với ống kính ~100°,
một điểm dừng yaw nhìn được liền mạch `yaw±95°` (roll −45 phủ `yaw−95…yaw+5`, roll 0 phủ `yaw±50`,
roll +45 phủ `yaw−5…yaw+95`), nên ba điểm dừng phủ `seed±185°` — trọn vòng tròn. Mỗi góc là một điểm DỪNG, không phải lướt qua: đầu còn đang động thì khung hình bị
nhoè và bộ nhận diện bỏ sót thứ đang hiện rành rành trong khung. Dùng `wrist_roll` thay vì xoay
`base_yaw` nhiều hơn vì hai thứ đó nhìn không giống nhau — xoay cả cây đèn trông như camera đặt trên
mâm xoay, còn xoay đầu trên một thân đứng yên trông như một sinh vật đang ngó quanh. Roll đảo hướng
nhìn nhưng giữ đường chân trời nằm ngang, nên không thể chúc camera xuống sàn giữa chừng.

Mỗi bước là `STEP_DEG` (90°). Các ô vẫn chồng lấn, nhưng phần chồng lấn do cái đầu tạo ra chứ không
phải do bước đế nhỏ: như đoạn trên đã tính, một điểm dừng yaw cùng ba lần ngoái `wrist_roll` nhìn được
liên tục `yaw±95°`, nên bước 90° không để lại khe hở. Các điểm dừng bị kẹp trong
tầm cơ khí ±135°, và đầu được cho `SETTLE_S` để hết rung trước mỗi lần đọc khung hình, vì đầu đang
chuyển động cho ảnh nhòe và bộ phát hiện sẽ bỏ sót thứ đang nằm ngay trong tầm nhìn.

Bị hủy bởi nút bấm vật lý giống như pha ngắm, và không bao giờ quét khi camera đang tắt — một pha quét
là rất nhiều chuyển động lộ liễu để thực hiện khi người dùng vừa yêu cầu thiết bị đừng nhìn.

**Trả body về idle.** Mọi đường ra của lượt quét đều kết thúc bằng `move_and_hold` — hit thì canh
giữa đối tượng, miss và survey thì `_restore` về pose seed — và `move_and_hold` để body không phát
gì (`_current_recording = None`, `_idle_settled`), giữ "cho tới lệnh play/emotion/idle kế tiếp".
Trước đây không ai gửi lệnh đó: trên lamp-ac82 (2026-09-14) "Find my keyboard" canh giữa bàn phím
rồi đứng yên tới khi restart HAL, đúng dấu hiệu `[preempt] dropped recording 'idle' for a direct
move` mà gaze đã sửa cho reacquire theo giọng nói. Lượt quét giờ gọi
`tracking/body.py: release_to_idle_later()` sau khi nhả quyền sở hữu servo: tìm thấy thì giữ hướng
vào vật trong `HOLD_AFTER_FIND_S` (8 s) để câu trả lời phát trên pose đó, không thấy thì về idle
ngay. Handback chạy trên daemon timer nên `search_for_subject` vẫn trả kết quả ngay cho lượt đang
chờ, và chỉ dispatch `play(idle)` khi không ai sở hữu body (tracking, hold mode, zero mode) **và**
chưa có gì bắt đầu phát từ đó — một emotion trong cửa sổ được để yên; vòng lặp animation tự về
idle khi nó kết thúc. Lên lịch handback mới sẽ hủy cái trước. Cửa sổ là hằng số cố định, không gắn
với lúc kết thúc nói: tracking không có hook speak-end để chờ.

> Chưa làm: tín hiệu LED trong lúc quét. Trạng thái LED transient nằm sau các request model của route,
> nên điều khiển nó từ đây sẽ phải đi vòng qua HTTP loopback (điều codebase này tránh) hoặc nhân bản
> phần bookkeeping khôi phục — và một tín hiệu không khôi phục được sẽ làm kẹt LED của lamp. Đáng làm
> cho tử tế thay vì làm nửa vời.

### Nói trong lúc đang tìm

Một chiếc lamp lặng lẽ xoay đi giữa câu hỏi trông như bị hỏng. Một chiếc vừa xoay vừa nói *"bạn đang ở
đâu?"* thì đọc ra là đang cố giúp.

os-server sở hữu các câu, phần phân giải ngôn ngữ và cache WAV (`system/lib/i18n/fillers.go`, các pool
`look_searching` / `look_found` / `look_capturing`); HAL chỉ quyết định **khi nào**, qua
`POST /api/sensing/filler` với `{"pool": "..."}`.

| Trạng thái | Khi nào | Mặc định |
|---|---|---|
| `look_searching` | bước đầu tiên về phía bearing đã ghi nhớ | **bật** (`HAL_LOOK_AIM_SPEAK`) |
| `look_found` | có người xuất hiện **sau khi** đã thông báo đang tìm | bật (cùng cờ) |
| `look_still_searching` | điểm giữa của pha quét — điểm dừng 2/3, đầu ở giữa (`_say_at_the_midpoint`) | bật (`HAL_LOOK_AIM_SPEAK`) |
| `look_capturing` | pha ngắm thực sự đã di chuyển trước khi bấm máy | bật (`HAL_LOOK_AIM_SPEAK_CAPTURE`) |

Phần chặn quan trọng hơn bản thân các câu nói. **Không nói gì khi đối tượng đã nằm giữa sẵn** — lần chụp
đó xong trong vài trăm mili giây, nên mọi câu ở đây đều có điều kiện là pha ngắm thực sự đã phải di
chuyển. *"Bạn đây rồi"* chỉ phát ra như phần kết của một lần tìm đã được thông báo, không bao giờ đứng
một mình. Trạng thái đang tìm chỉ thông báo **một lần** mỗi pha quét chứ không phải mỗi bước, cộng
thêm đúng một câu `look_still_searching` ở điểm giữa — pha quét dài ~20 s, và không có câu đó thì câu
mở đầu và câu kết luận nằm hai bên một khoảng lặng hai mươi giây, nghe như một cái đèn đã đứng máy chứ
không phải một cái đèn đang tìm. Còn câu lúc chụp chỉ phát khi pha ngắm thực sự đã di chuyển
(`res.aimed and res.iterations > 0`): pha ngắm không động gì thì không nói gì, và — phần đã sai cho tới
nhánh này — pha ngắm đã tìm rồi **thất bại** cũng không nói gì, trước đây nó nối ngay
*"Tôi không tìm thấy bạn"* với *"Để tôi nhìn thử"*.

Một lần chụp nhanh, im lặng và đúng vốn đã là kết quả tốt — lời nói chỉ dành cho những khoảnh khắc người
dùng thực sự phải chờ.

**Hai kiểu một pool im tiếng mà không hề báo lỗi**, cả hai đều đã được sửa tận gốc chứ không né:

- **Một key thiếu trong một ngôn ngữ đã biết** trước đây phân giải ra rỗng. `FillerForTool` chỉ lùi về
  tiếng Anh khi *ngôn ngữ* không xác định, chứ không bao giờ lùi khi *key* thiếu bên trong một ngôn ngữ
  đã biết — mà hai map zh lại hoàn toàn không có entry `look_*` nào, nên mọi filler của look trên một
  thiết bị tiếng Trung đều bị bỏ qua, không log, không lỗi. Giờ cả hai map zh đều có đủ pool, và một
  key thiếu ở bất kỳ đâu sẽ lùi về tiếng Anh. Một câu tiếng Anh trên thiết bị tiếng Trung là sai,
  nhưng là cái sai *phát ra thành tiếng*: sẽ có người nghe thấy và báo lại, còn im lặng thì vô hình.
  `TestEveryPoolIsTranslatedInEveryLanguage` giữ cho cơ chế lùi này không trở thành đường đi mặc định.
- **Một pool chưa từng được prewarm** sẽ phải render trực tiếp ở nhà cung cấp TTS ngay lần phát đầu
  (~1–2 s), và đoạn audio trễ đó đua với phần lời nói ngay sau nó — người dùng cảm nhận như filler bị
  cắt ngang. `PrewarmFillers` trước đây duyệt một danh sách tên tool bảo trì thủ công mà không pool
  `look_*` nào từng được thêm vào. Giờ nó duyệt `i18n.AllPoolKeys()`, nên một pool mới được phủ sẵn
  nhờ chính sự tồn tại của nó.

Không kiểu nào trong hai kiểu trên báo gì ở bất kỳ mức log nào: `PlayPoolFillerNow` return ngay tại
`len(phrases) == 0` còn `POST /api/sensing/filler` thì vẫn trả 200 trong cả hai trường hợp. Một pool
phân giải ra rỗng đơn giản là một cái đèn không nói gì.

## Canh khung gaze — giữ user trong khung hình

Mọi thứ ở trên đều là do được yêu cầu: một lệnh nhìn, một phiên track, một lần tìm. Phần này là
watcher trong `hal/drivers/tracking/gaze.py` tự làm những việc đó mà không ai bảo, để đến lúc user
thực sự cất tiếng thì camera đã hướng về chỗ có ích. Toàn bộ nằm dưới `HAL_GAZE_WAKE` (xem
`physical-controls_vi.md`) — cờ đó chặn cả watcher chứ không riêng cửa wake như tên gọi gợi ý, nên tắt
nó thì không hành vi nào dưới đây chạy.

Ràng buộc xuyên suốt: **không ai yêu cầu những việc này**, nên mọi vòng lặp đều bị chặn — một vùng
chết, một cooldown, một hạn mức số bước. Một cái đèn biết chỉnh khung hình thì có vẻ đang chú ý; một
cái chỉnh liên tục thì thành cái đầu gật gù theo.

**Các vòng lặp ĐO liên tục, nhưng chỉ DI CHUYỂN khi đang có một cuộc hội thoại mở.**
`_conversation_open()` đọc `voice_service.conversation_focus_active()` — cửa sổ follow-up của wake
word, được làm mới bởi mọi cửa wake (wake phrase, click, presence, gaze). Không thể chặn phần đo: cổng
wake đọc cửa sổ *trước* lúc nói, nên các mẫu đó phải có sẵn từ trước. Phần di chuyển thì chặn được, và
phải chặn.

Lý do: ngoài một cuộc hội thoại, một hiệu chỉnh không thể sống sót trên cánh tay này. `idle.csv` phát
các frame tuyệt đối và ghim `base_yaw` ở −2,40 — **1,58° dao động trên toàn bộ recording** — nên frame
idle kế tiếp ghi đè lên hiệu chỉnh và vòng lặp lại đo đúng độ lệch cũ. Quan sát trên máy thật
2026-08-26 khi không ai nói: mười ba lần hiệu chỉnh pan trong hai mươi phút, đổi chiều liên tục, lần
nào cũng bắt đầu từ đúng dải của idle. Không phải trôi dần — mà là bị ghi đè ngay lập tức, lặp mãi.

Vì thế "thả ra" là miễn phí: ngừng hiệu chỉnh thì idle tự đòi lại cánh tay trong vòng một frame, không
có pose nào phải khôi phục và không có quyền sở hữu nào phải nhả. Phép kiểm tra này **fail đóng** khi
không có voice. Ngoại lệ duy nhất là một lần leo *được yêu cầu* (`prompt`), vốn do một repoint từ
speech đòi, nếu từ chối thì đầu sẽ nằm chĩa vào ngực suốt cả câu nói.

### Canh giữa theo chiều dọc, và vì sao nó đọc trung vị

Đèn bàn đứng thấp hơn tầm đầu người, nên camera của nó chĩa vào ngực. Phép hiệu chỉnh lấy **trung vị**
độ lệch dọc trong `HAL_GAZE_PITCH_WINDOW_S`, **không phải khung hình mới nhất** — và đó chính là lý do
vòng lặp này hội tụ.

`wrist_roll` là một trục *ngắm* thứ hai trên cánh tay này (chứng minh trên máy thật bằng cách ghim mọi
khớp khác và chỉ đổi roll: đường chân trời giữ nguyên trong khi tầm nhìn quét ngang), và recording idle
quét khớp đó ~32° mỗi chu kỳ ~10 s, mãi mãi. Nên độ lệch mà một khung hình đơn lẻ báo về là sai số khung
hình **cộng** một nhiễu tuần hoàn đến từ chỗ roll của idle đang đứng. Đo trên ba khung hình với đối
tượng không nhúc nhích: `dy` +0,101 ở roll −1,8° so với +0,143 ở roll +29,3° — 0,042 chiều cao khung
hình chỉ do roll, khoảng 28% vùng chết, trên một vòng lặp trước đây bắn mỗi 4 s từ một mẫu duy nhất.
Trung vị qua một chu kỳ idle triệt tiêu nhiễu tuần hoàn trong khi sai số khung hình thật thì sống
sót qua nó.

**Cửa sổ cũng chính là nhịp tim của vòng lặp.** `_dy_estimate` từ chối trả về gì cho tới khi các mẫu
phủ được `WINDOW_S × 0.8`, và buffer bị xoá sạch sau mỗi lần hiệu chỉnh — nên chính thời gian nạp lại,
chứ không phải `HAL_GAZE_PITCH_COOLDOWN_S`, mới là khoảng cách thật giữa hai bước. Ở giá trị 12 s ban
đầu, điều đó nghĩa là chờ ~9,6 s trước khi đầu nhúc nhích, quá lâu để ngồi lệch khung thấy rõ trong khi
user đang ở ngay đó. Cửa sổ giờ là **6 s**, cho ra ~4,8 s. Cái đánh đổi thì nói thẳng: nửa chu kỳ roll
thay vì trọn một chu kỳ, nên một phần nhiễu của idle sống sót vào trung vị. Vòng lặp đo lại sau mỗi lần
di chuyển, nên cái giá là thêm một vòng lặp chứ không phải mất độ chính xác — nhưng nếu đầu bắt đầu
săn đuổi qua lại, đây là con số cần chỉnh về lại.

Phép hiệu chỉnh được chia cho cả ba khớp pitch bởi `distribute_pitch` (`servo_follow.py`), trọng số
`base_pitch` 0,20 / `elbow_pitch` 0,60 / `wrist_pitch` 0,20 — trên một cánh tay lành lặn thì khuỷu gánh
nhiều nhất. Việc chia có **tính đến khoảng trống còn lại theo đúng chiều được yêu cầu** và chạy hai
lượt: lượt đầu tôn trọng trọng số, lượt sau đưa phần tràn cho khớp nào còn chỗ. Một khớp chạy một mình
sẽ đụng chặn cơ khí trong khi mặt vẫn còn ngoài khung.

**Một lệnh không tới nơi sẽ bị phát hiện.** `move_and_hold` không báo lại gì, nên một khớp bị kẹt trước
đây không thể phân biệt với một khớp chạy tốt, và vòng lặp cứ ra lại đúng cái đích không với tới đó mỗi
~10 s mãi mãi — quan sát được qua sáu lần hiệu chỉnh liên tiếp với `elbow_pitch` đọc +12,3 trong khi
được gửi tới +25,8. Tệ hơn, ra lệnh lại vào một khớp đang kẹt chính là thứ làm nóng servo tới mức nó bỏ
cuộc, nên vòng lặp tự chế tạo ra đúng cái điều kiện mà nó liên tục vấp phải. Giờ cánh tay được hỏi liên
tục cho tới khi tới nơi; khớp nào hụt quá `HAL_GAZE_PITCH_LAND_TOL_DEG` sẽ được cho nghỉ
`HAL_GAZE_PITCH_STALL_REST_S` và lùi đích lại `HAL_GAZE_PITCH_STALL_BACKOFF_DEG`, để lần thử sau không
tì vào chặn cơ khí ngay lập tức nữa.

**Các hiệu chỉnh không được giữ lại trước idle.** Recording idle là tuyệt đối trên mọi khớp và lặp vô
tận, nên trong vòng một chu kỳ nó kéo camera về lại pose lúc ghi — trên bàn làm việc thì đó là bàn
phím. Trước đây có một idle anchor (`HAL_GAZE_IDLE_ANCHOR`) chống lại điều đó bằng cách dịch cả vòng
idle sang pose tốt gần nhất; **nó đã bị gỡ bỏ**. Nên lực kéo về vẫn còn: một hiệu chỉnh phai dần qua
một chu kỳ idle thay vì trụ lại, và vòng lặp sẽ hiệu chỉnh lại ở cửa sổ kế tiếp. Đó là lý do chính
khiến cùng một độ lệch có thể xuất hiện lại sau một lần hiệu chỉnh thành công.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `HAL_GAZE_PITCH` | `true` | Bật/tắt canh giữa theo chiều dọc. |
| `HAL_GAZE_PITCH_WINDOW_S` | 6 | Cửa sổ trung vị, và cũng là nhịp thật của vòng lặp — một lần hiệu chỉnh phải chờ các mẫu phủ 80% cửa sổ (~4,8 s). Trước là 12, phủ trọn một chu kỳ roll của idle nhưng khiến mỗi bước phải chờ ~9,6 s. |
| `HAL_GAZE_PITCH_MIN_SAMPLES` | 8 | Sàn để hành động trên một cửa sổ mới đầy một phần. |
| `HAL_GAZE_PITCH_PROMPT_MIN_SAMPLES` | 2 | Sàn khi việc leo tìm được yêu cầu trực tiếp — nhánh thân người báo hằng số −0,5, nên thêm mẫu cũng không thêm thông tin. |
| `HAL_GAZE_PITCH_DEAD_ZONE_FRAC` | 0.15 | Độ lệch (theo tỉ lệ chiều cao khung) được tính là đã đủ giữa. Mục tiêu là mặt *nằm trong* khung và có khoảng thở, không phải giữa hoàn hảo. |
| `HAL_GAZE_PITCH_DEG_PER_FRAME` | 45 | Số độ trên trọn chiều cao khung. Là mầm khởi tạo, không phải calibration — vòng lặp tự đo lại sau mỗi bước. |
| `HAL_GAZE_PITCH_MAX_STEP_DEG` | 15 | Bước hiệu chỉnh lớn nhất một lần. |
| `HAL_GAZE_PITCH_COOLDOWN_S` | 4 | Sàn khoảng cách giữa hai lần hiệu chỉnh. |
| `HAL_GAZE_PITCH_MOVE_S` | 1.0 | Thời lượng di chuyển. Khác 0,25 s của pha ngắm: gaze chỉ động một lần mỗi ~10 s mà không ai đợi, nên nó có thể đi nhẹ nhàng. |
| `HAL_GAZE_PITCH_SETTLE_S` | 1.8 | Chờ ổn định trước khi đọc lại — đọc giữa lúc đang trôi sẽ báo một cú đi ngắn mà thật ra chỉ là đang còn chạy. |
| `HAL_GAZE_PITCH_LAND_TOL_DEG` | 2.0 | Mức hụt được tính là kẹt. |
| `HAL_GAZE_PITCH_STALL_REST_S` | 60 | Khớp kẹt bị loại ra bao lâu. Khớp với thời gian hồi phục đo được. |
| `HAL_GAZE_PITCH_STALL_BACKOFF_DEG` | 2.0 | Dừng trước chỗ nó đã kẹt. |
| `HAL_GAZE_SNAPSHOT` / `_KEEP` | `true` / 40 | Lưu khung hình có chú thích cạnh mỗi lần hiệu chỉnh, trong `SNAPSHOT_PERSIST_DIR/sensing_gaze/`. Log nói trung vị là −0,41 chiều cao khung; nó không nói được đó là user, một đồng nghiệp, hay một cái áo khoác vắt trên ghế. |

### Leo tìm cái mặt nằm trên khung hình

Một khung thân người **chạm cạnh trên** nghĩa là thân còn tiếp tục vượt ra ngoài, tức đầu ở phía trên và
camera đang chĩa quá thấp. Đó là dạng bằng chứng duy nhất được dùng: một thân người không bị cắt mà
không có mặt nghĩa là đầu *đang ở trong khung* và chỉ là không được nhận diện — quay đi, nghiêng
nghiêng, ngược sáng — và leo lúc đó là chĩa vào trần nhà không vì lý do gì.

Bước cố định chứ không tỉ lệ, vì thân người nói "cái đầu ở đâu đó phía trên kia" chứ không bao giờ nói
xa bao nhiêu. Điều khiển tỉ lệ cần một tín hiệu sai số; đây là một cuộc tìm.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `HAL_GAZE_FACE_SEARCH_STEP_DEG` | 15 | Một bước leo. |
| `HAL_GAZE_FACE_SEARCH_MAX_STEPS` | 4 | Khoảng 60° rồi dừng. Bằng chứng vẫn đúng dù cổ đã đi bao xa, nên hành động theo nó mãi là một vòng lặp chứ không phải một cuộc tìm. |

**Chỗ ghi nhớ một độ cao đã dùng được** — `hal/drivers/tracking/face_height.py`, tại
`/var/lib/hal/face_height.json` (`HAL_FACE_HEIGHT_PATH`), cố ý **tách khỏi** `user_bearing.json`.
Bearing trả lời *"user ở hướng nào?"* và được look-aim, search và repoint đọc; ghi độ cao vào đó sẽ đổi
luôn thứ mà look-aim khôi phục ở mỗi lần gọi. File này trả lời câu khác — *"camera phải ngắm cao bao
nhiêu để thấy một cái đầu từ chỗ này?"* — và chỉ vòng lặp pitch của gaze đọc nó. Hai thứ cũng cũ đi theo
hai kiểu: bearing là phỏng đoán về một con người, mà người thì di chuyển, nên nó bị loại bỏ khi ngừng
hoạt động (ba lần dự đoán hỏng); độ cao là một sự thật về đồ đạc, và cứ thế giữ nguyên. Toàn bộ pose được ghi vì một góc pitch chỉ có nghĩa khi đi kèm phần còn lại của tư thế,
nhưng **chỉ các khớp pitch được áp khi khôi phục** — yaw thuộc về bearing và vòng xoay ngang, trả nó về
đây là giao cùng một vô-lăng cho hai hệ thống.

### Xoay ngang, và vì sao nó lười hơn pitch

Khung hình theo chiều dọc hỏng theo một chiều — user đứng dậy và ra khỏi mép trên — nên đáng đuổi theo.
Trôi ngang thì phần lớn là người ta xê dịch trên ghế, và một cái đèn đảo theo mọi cú nghiêng người đúng
là kiểu giật cục mà cả vòng lặp này được giảm chấn để tránh. Phép hiệu chỉnh chia cho `base_yaw` và
`wrist_roll` qua `distribute_yaw`.

Dù vậy vùng chết lại **hẹp hơn** của pitch (0,10 so với 0,15), nghe ngược đời cho tới khi để ý rằng giá
trị đem ra kiểm tra là *trung vị* trong cửa sổ: chính cửa sổ mới là thứ loại bỏ nghiêng người và cựa
quậy, và bắt vùng chết làm lại việc đó lần nữa chỉ tốn mất đúng phép hiệu chỉnh mà nó lẽ ra phải cho
qua. Nó khởi điểm ở 0,22 theo lập luận ngược lại và bị thử nghiệm trên máy thật bác bỏ — cố ý dịch qua
dịch lại ở bàn làm việc chỉ đạt đỉnh `dx` +20%, tức vòng lặp đo đúng chuyển động rồi từ chối mọi lần.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `HAL_GAZE_YAW` | `true` | Bật/tắt hiệu chỉnh xoay ngang. |
| `HAL_GAZE_YAW_WINDOW_S` / `_MIN_SAMPLES` | 12 / 8 | Như pitch. |
| `HAL_GAZE_YAW_DEAD_ZONE_FRAC` | 0.10 | Tỉ lệ trên trọn chiều rộng khung (`dx` chạy −0,5 … +0,5). |
| `HAL_GAZE_YAW_DEG_PER_FRAME` | 40 | Số độ trên trọn chiều rộng khung. |
| `HAL_GAZE_YAW_MAX_STEP_DEG` | 12 | Bước hiệu chỉnh lớn nhất một lần. |
| `HAL_GAZE_YAW_MOVE_S` | 1.0 | Cả hai khớp xoay ngang đều không chống trọng lực, nhưng cả cái đèn xoay là một sự kiện thị giác lớn hơn một cú nghiêng đầu. |

### Repoint về bearing đã ghi nhớ

**Chỉ speech mới kích hoạt cái này, không còn gì khác.** Khi ai đó nói mà watcher không có bằng chứng
mặt dùng được, nó quay về bearing đã ghi nhớ rồi kiểm tra trong `HAL_GAZE_REPOINT_VERIFY_S` xem có ăn
thua không. Yêu cầu được đặt trên thread mic và được tiêu thụ trên thread watcher
(`_consume_speech_repoint` → `_maybe_repoint(force=True)`), bỏ qua cả thời gian chờ vắng mặt lẫn
cooldown.

Trước đây nó *còn* tự bắn sau `HAL_GAZE_REPOINT_AFTER_S` (12 s) không thấy ai. Phần đó đã bị gỡ. Một
lần repoint sẽ chấm điểm bearing, và bắn vì "tình cờ không có ai trong khung" — điều luôn đúng với một
cái đèn bàn khi bạn nghiêng người ra khỏi khung, quay sang đồng nghiệp, hay đứng dậy — là chấm một
bearing là sai dựa trên bằng chứng không nói lên điều gì. Ba lần như vậy sẽ xoá ước lượng, nên một
bearing đúng có thể bị bào mòn bởi một cái ghế trống. Chỉ chấm điểm trên các câu nói thì mỗi lần trượt
đều có nghĩa: có người đã nói, đèn đã quay về nơi nó tưởng họ ở, và họ không ở đó.

Ba hành vi nữa đáng nói ra vì cái nào cũng từng là một con bug:

- **Thấy thân người cũng tính là tìm được user.** Bộ kiểm tra theo dõi mặt và thân trên hai đồng hồ
  riêng; một cái thân ở đúng bearing nghĩa là bearing *đúng*. Chấm nó là trượt đã xoá mất những bearing
  đúng trong khi user đang ngồi ngay trước đèn.
- **Một lần repoint phải kết thúc trên một cái mặt.** Dừng ở thân người mới là nửa thành công, nên nó
  kích hoạt phần leo tìm ở trên thay vì báo "đã thấy" — đó là lý do phần leo có `_PROMPT_MIN_SAMPLES`
  bằng 2.
- **Nó sẽ không quay đi khỏi một cái mặt đang có trong khung.** Nếu vừa thấy mặt trong
  `HAL_GAZE_REPOINT_SKIP_IF_FACE_S`, một lần reacquire do speech kích hoạt sẽ từ chối: sau khi leo tìm
  đã thấy mặt user *cao hơn* bearing, nghe theo bearing nghĩa là quay ngược xuống nhìn vào chỗ không có
  ai.
- **Hold kết thúc cùng câu nói.** Reacquire do speech kích hoạt ngắm đèn bằng `move_and_hold`,
  hàm này bỏ recording đang phát và set `_idle_settled` — đúng cho lúc đang nói, sai sau khi nói
  xong, vì không ai bật lại idle nữa. Đèn đứng im luôn cho tới khi restart HAL (đo trên lamp-0c89
  03/09/2026: `[preempt] dropped recording 'idle' for a direct move` lúc 16:23:40, tới 16:25:58 vẫn
  bất động, không thêm dòng log nào). `on_speech_end` giờ trả body về bằng `dispatch(play, idle)` —
  đúng cách tracker bàn giao khi nó kết thúc — và bỏ qua khi tracking, hold/zero mode hoặc scene
  đang giữ body, vì mỗi thứ đó có đường nhả riêng.

Mọi lần từ chối đều được log kèm lý do (`[gaze] no repoint: …`), có tiết chế để một điều kiện kéo dài in
ra mỗi phút một lần thay vì mỗi vòng một lần.

### Tự quay quanh tìm

Nếu một lần repoint không ra ai, `_verify_repoint` gọi chính pha quét `/servo/search` mô tả ở trên với
`confirmed_miss=True`. Vì repoint ở trên do speech kích hoạt, pha quét cũng vậy: đèn đi tìm vì có người
đã nói mà nó không tìm ra họ, chứ không bao giờ vì một căn phòng trông có vẻ trống. Cò kích hoạt theo
vắng mặt (`HAL_GAZE_SWEEP_AFTER_S`) vẫn còn trong `_maybe_sweep` nhưng không còn gì với tới nó — vòng
lặp watcher không còn gọi pha quét nữa. Các cooldown vẫn áp dụng, và có tới hai vì hai tình huống không
giống nhau.

Mười lăm phút là đúng cho *"tôi có bearing, nó trượt, thôi đừng quẫy nữa"*. Nó sai cho *"tôi không biết
bạn ở đâu cả"*, vì khi đó pha quét là cách duy nhất để biết mà đèn lại bị cấm thử — quan sát trên máy
thật: ba lần repoint hỏng đã xoá ước lượng, rồi đèn ngồi đó vừa không repoint được (không có gì để quay
về) vừa không quét được (còn 11 phút cooldown) trong khi user đang nói chuyện với nó.

`confirmed_miss` bỏ qua thời gian chờ vắng mặt một cách có chủ ý — một lần repoint đã di chuyển rồi
trượt là bằng chứng mạnh nhất có thể có, nên không còn gì để chờ. Một pha quét thành công sẽ lấy mẫu
một bearing mới ngay tại chỗ.

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `HAL_GAZE_SWEEP` | `true` | Bật/tắt tự quay quanh tìm. |
| `HAL_GAZE_SWEEP_AFTER_S` | 30 | Không thấy ai trong bao lâu. Dài hơn `HAL_GAZE_REPOINT_AFTER_S` (12 s) để nước đi rẻ luôn được thử trước và pha quét ~20 s vẫn là bước leo thang chứ không phải phản xạ. |
| `HAL_GAZE_SWEEP_COOLDOWN_S` | 900 | Giữa hai pha quét khi đã có bearing. |
| `HAL_GAZE_SWEEP_COOLDOWN_LOST_S` | 120 | Giữa hai pha quét khi chưa có bearing nào. |

### Bearing người dùng đã ghi nhớ

`hal/drivers/tracking/user_bearing.py` gộp các lần nhìn thấy thành một ước lượng duy nhất
tại `/var/lib/hal/user_bearing.json` (`HAL_USER_BEARING_PATH`). Một chỗ duy nhất, không phải histogram
— lamp chỉ bao giờ cần một tư thế để quay về.

**Nó lưu cả tư thế servo, không phải một góc đơn lẻ** (schema v2; file v1 sẽ được migrate và giữ lại
hướng đã học). `bearing_deg` vẫn còn như thành phần yaw để bên gọi chỉ cần hướng thì không phải biết
tên khớp, và nó được *suy ra từ* `pose["base_yaw.pos"]` nên hai giá trị không bao giờ mâu thuẫn. Chỉ
yaw thôi thì không đủ để nhìn vào ai đó: pitch trải trên base/elbow/wrist, nên cái đầu bị bỏ lại ở tư
thế chúi xuống sàn sẽ quét sàn theo vòng tròn dù yaw có đúng tới đâu. Mỗi khớp có EMA riêng với cùng
tốc độ như yaw; khi phát hiện dời chỗ thì tư thế bị thay hẳn chứ không lấy trung bình, vì tư thế cũ mô
tả chỗ cũ.

**Độ tin cậy đo mức ước lượng đã được HỌC tới đâu, không phải nó mới tới đâu.** Nó tăng theo số lần
nhìn thấy — đạt ~1.0 sau `CONFIDENCE_FULL_SAMPLES` (8) — rồi **đứng yên ở đó**. Nó không suy giảm theo
thời gian. Độ mới vẫn được báo ra, dưới dạng `age_s`, cho bất kỳ caller nào cần; hiện chưa có chỗ nào
chặn theo nó.

Đây là một cú đảo chiều có chủ ý. Trước đây độ tin cậy giảm một nửa mỗi sáu tiếng, theo lập luận rằng
một ước lượng cũ nên tự khai là cũ thay vì trông có vẻ đáng tin. Lập luận thì đúng, còn số học thì
không: các lần nhìn thấy tới chậm hơn nhiều so với tốc độ chu kỳ bán rã ăn mất chúng, nên trên máy
thật ước lượng mất nhanh hơn được và nằm lì dưới cái ngưỡng cho phép dùng nó — một bearing không ai
được phép tra tới không phải là một bearing thận trọng, đó là một bearing không tồn tại.

Việc phát hiện cũ nay là nhiệm vụ của đường dự-đoán-hỏng, vốn là tín hiệu sắc nét hơn: thay vì đoán từ
một cái đồng hồ rằng bearing đã hỏng, lamp quay về đó, nhìn, rồi chấm điểm thứ nó thấy. Ba lần hỏng
xảy ra cụm nhau sẽ loại bỏ nó hẳn — xem *Phát hiện lamp đã bị dời chỗ* bên dưới. Một bearing hoặc vẫn
chạy được, hoặc bị bỏ; nó không còn phai dần vào vùng xám vừa quá yếu để dùng vừa quá mạnh để thay.

Các lần nhìn thấy đi vào đây theo hai đường:

- **Từ một lần look aim**, khi đối tượng kết thúc trong phạm vi **2%** quanh tâm khung — chặt hơn cả
  dung sai căn khung của chính pha ngắm, và cố ý như vậy: ở tâm khung thì vị trí servo **chính là**
  bearing, không có phép quy đổi pixel→góc nào và do đó không phụ thuộc vào hằng số FOV đang tranh cãi.
- **Từ bộ lấy mẫu thụ động** (`bearing_sampler.py`), mỗi `HAL_BEARING_SAMPLE_INTERVAL_S` (300 s).
  Đường chỉ-qua-aim ghi được khoảng hai lần nhìn thấy một ngày, quá chậm để dựng nên một ước lượng mà
  pha ngắm chịu dùng — độ tin cậy lớn lên theo số lần nhìn thấy, và với nhịp đó một máy mới toanh mất
  nhiều ngày nằm dưới ngưỡng, trong khi thứ duy nhất cứu được một lần look khi không thấy ai thì nằm
  không. (Trước đây còn tệ hơn, khi độ tin cậy còn suy giảm theo chu kỳ bán rã sáu tiếng: ước lượng
  mất nhanh hơn được và không bao giờ ổn định nổi. Phần suy giảm đó nay đã bỏ — xem bên dưới.) Bộ lấy
  mẫu **không bao giờ làm lamp chuyển động**: nó đọc một khung hình và vị trí servo hiện tại, rồi suy
  ra bearing bằng số học `yaw + dx × scale`.

Bộ lấy mẫu thà từ chối còn hơn đoán. Độ lệch ngang chỉ được chấp nhận tới
`HAL_BEARING_SAMPLE_MAX_DX_FRAC` (0.25), vì phép hiệu chỉnh đó dựa vào đúng cái hằng số FOV mà aim
sinh ra để khỏi phải tin. Nó cũng bỏ qua khi thân đang aim hoặc đang bám, khi camera bị tắt, và lấy
khóa bộ phát hiện theo kiểu không chặn để câu hỏi của người dùng không bao giờ phải chờ nó.

**Nó chỉ học từ `face`, không bao giờ từ box `person`.** Box person cho biết một thân người ở đâu, mà
thân người thì lấp đầy khung mỗi khi camera tình cờ chĩa thấp — nên học từ nó là ghi nhớ đúng cái tư
thế đang nhìn xuống bàn rồi gọi đó là "chỗ user ngồi". Đo trên thiết bị: 22 mẫu, confidence 0.99, tư
thế lưu lại có `wrist_pitch -78` và không nhìn thấy nổi một khuôn mặt nào. Mọi nơi tiêu thụ phía sau
đều khôi phục trung thành tư thế đó rồi chẳng thấy ai — nhìn từ ngoài thì đó là đèn hỏng, chứ không
phải bearing sai. Thấy được mặt thì chứng minh điều ngược lại theo định nghĩa: tư thế này nhìn thấy
đầu người, khôi phục nó sẽ lại thấy.

**Tư thế được ghi bất kể mặt nằm đâu trong khung.** Cửa dọc từng gác nó
(`HAL_BEARING_SAMPLE_MAX_DY_FRAC`, nay đã xoá — nó không còn chặn gì nữa mà chỉ còn nắn một chuỗi
log) viết cho box person, nơi thân người ở giữa khung không nói lên
được đầu có trong khung hay không. Giữ cửa đó cho `face` là tự phá: khi camera đang chĩa thấp thì mặt
nào cũng nằm sát mép trên, nên mọi lần thấy đều trượt cửa, nên không tư thế nào từng được lưu, nên
không có gì để khôi phục và camera cứ ở nguyên chỗ thấp — đo được `dy` -15.8% rồi -41.2%, hai lần
thấy mặt, và một "pose" ghi nhớ chỉ có mỗi yaw. Tư thế bắt được người dùng ở rìa khung thì không hoàn
hảo; nhưng nó tốt hơn vô cùng so với tư thế chĩa vào mặt bàn, và EMA từng khớp sẽ kéo nó về giữa khi
khung hình mà chính nó tạo ra tốt dần lên.

Mỗi lần lấy mẫu ghi một khung hình có chú thích vào `/var/lib/hal/snapshots/sensing_bearing/`
(`HAL_BEARING_SNAPSHOT`, giữ 30 cái mới nhất, cũ nhất bị dọn) — **kể cả những phát hiện bị loại**, có
ghi rõ lý do, vì "nó bỏ qua một người lạ ở xa" và "nó không thấy gì" trông y hệt nhau trong ước lượng.
Xem được tại `GET /api/sensing/snapshot/sensing_bearing/<name>`.

Các góc được lấy trung bình **tuyến tính, không phải theo vòng tròn**: `base_yaw` là dải servo bị chặn
±135° và không quấn vòng, nên trung bình theo vòng tròn sẽ sai ở hai đầu dải.

Giá trị lệch (outlier) bị làm giảm ảnh hưởng chứ không bị nhận thẳng — một người đi ngang qua phòng
không được phép lật ngược ước lượng — nhưng `OUTLIER_STREAK` lần lệch liên tiếp sẽ được coi là dời chỗ
thật và được nhận nguyên vẹn.

**Được tiêu thụ bởi ưu tiên 3 của pha ngắm** (ở trên) khi độ tin cậy vượt `MIN_BEARING_CONFIDENCE`.
Hãy mở ra kiểm tra phép tính và dấu:

```bash
curl -s localhost:5001/servo/bearing     # có kèm cả tư thế đầy đủ
cat /var/lib/hal/user_bearing.json
```

Một bearing `known` nhưng `pose` rỗng nghĩa là ước lượng có từ trước schema tư thế và chưa được nhìn
thấy lại: một lần tìm kiếm sẽ khôi phục hướng nhưng chưa khôi phục độ cao đầu, cho tới khi lần nhìn
thấy kế tiếp điền vào.

`bearing_deg` phải hội tụ về gần nơi người dùng thực sự ngồi. Một ước lượng nằm **đối xứng gương qua
0** nghĩa là dấu yaw bị đảo — đây là kiểu hỏng mà file này dễ dính nhất, vì nó chạy vòng hở và không
có gì sửa sai cho nó.

### Phát hiện lamp đã bị dời chỗ

Bearing được lưu theo hệ quy chiếu **gắn với lamp**, nên nhấc lamp lên hay xoay nó trên bàn là nó sai
ngay lập tức — trong khi file vẫn trông hoàn toàn hợp lệ.

Không có gì trên thiết bị này quan sát được điều đó một cách trực tiếp:

| Cách tiếp cận | Vì sao không được |
|---|---|
| IMU / cảm biến gia tốc | không được lắp — không có trong BOM lẫn trong HAL |
| Phản hồi servo | `base_yaw` đo vị trí đầu so với **đế**. Xoay cả chiếc lamp làm thay đổi thế giới chứ không làm thay đổi khớp. |
| Dựa vào lần khởi động lại | yếu ở cả hai chiều — lamp reboot mà không bị dời, và bị dời mà không reboot |

Nên nó được **suy ra từ các dự đoán sai**: khi ưu tiên 3 của pha ngắm quay tới bearing đã ghi nhớ mà
không thấy ai, đó là một lần trượt. `PREDICTION_MISS_LIMIT` lần trượt sẽ hủy ước lượng, và nó tự dựng
lại từ các lần nhìn thấy mới.

Ba lớp bảo vệ giúp sinh hoạt bình thường không bị hiểu nhầm thành dời chỗ:

- **Một lần trượt là chưa đủ** — người dùng có thể chỉ đang ra ngoài.
- **Một lần trúng xóa sạch chuỗi trượt**, nên những lần vắng mặt lẻ tẻ không bao giờ cộng dồn.
- **Các lần trượt phải xảy ra gần nhau** (`MISS_STREAK_WINDOW_S`). Lamp bị dời sẽ trượt ở mọi lần thử
  kể từ lúc bị dời; còn người dùng thỉnh thoảng ở phòng khác thì tạo ra các lần trượt rời rạc trải dài
  hàng tuần. Không có cửa sổ thời gian thì sau đủ lâu hai thứ đó trông y hệt nhau.

Và một lần trượt chỉ được tính khi lamp **thực sự đã nhìn và không thấy gì**. Camera đang tắt, không
lấy được khung hình, hết hạn chót, bị nút bấm hủy, và cả trường hợp giữ nguyên vì bị che khuất — tất cả
đều thoát ra mà không chấm điểm; đặc biệt chế độ riêng tư tuyệt đối không được phép xóa dần nơi người
dùng hay ngồi.

Cách này tự chữa lành cho **mọi** nguyên nhân (lamp bị dời, đồ đạc sắp xếp lại, người dùng đổi bàn) mà
không bao giờ cần biết nguyên nhân nào đã xảy ra.

#### Phải dời bao nhiêu thì mới cần tới cơ chế phát hiện

Phần lớn các lần dời chỗ không bao giờ chạm tới cơ chế đếm trượt ở trên, vì ước lượng tự sửa lấy:

| Lamp bị xoay | Cái gì sửa nó |
|---|---|
| **nhỏ hơn nửa FOV camera** (~30°) | người dùng **vẫn nằm trong khung hình** ở bearing cũ, nên pha ngắm vẫn tìm thấy và căn giữa được — và chính lần nhìn thấy đó ghi lại góc yaw mới đúng. EMA thông thường kéo ước lượng theo. **Không có gì phát hiện ra việc dời chỗ; và cũng không cần.** |
| tới `OUTLIER_DEG` (45°) | không thấy được từ bearing cũ, nhưng tìm thấy trong lúc bước dần về phía đó. Lần nhìn thấy vẫn dưới ngưỡng outlier nên được nhận với trọng số đầy đủ. |
| vượt `OUTLIER_DEG` | các lần nhìn thấy trông như outlier, nên `OUTLIER_STREAK` lần liên tiếp sẽ được nhận là một lần dời chỗ. |
| xa tới mức không bao giờ tìm thấy người dùng | chuỗi trượt sẽ hủy ước lượng và nó dựng lại từ đầu. |

Nên toàn bộ cơ chế ở trên chỉ dành cho trường hợp **cuối cùng**. Một chiếc lamp bị xê dịch nhẹ trên bàn
được xử lý bởi chính quy tắc cập nhật của ước lượng — đó cũng là lý do trường hợp nhỏ lại là trường hợp
êm nhất: lamp không bao giờ biết là nó đã bị dời, và cũng không cần biết.

**Xem và đặt lại:**

```bash
curl 127.0.0.1:5001/servo/bearing              # {"known":true,"bearing_deg":-18.5,...}
curl -X POST 127.0.0.1:5001/servo/bearing/reset
```

Lệnh đặt lại cũng được nối với giọng nói qua `skills/servo-control` — *"tôi đã dời bạn đi"*, *"bạn
đang ở chỗ mới"*. Việc phát hiện tự động cần vài lần thất bại mới hành động, điều đó đúng để tránh báo
động giả nhưng chậm khi người dùng vốn đã BIẾT là lamp bị dời.
