# Motion Activity Whitelist

Only these Kinetics action classes are forwarded to OpenClaw as `motion.activity` events. All others are filtered at HAL level to save tokens.

Chỉ những action classes dưới đây được forward lên OpenClaw dạng `motion.activity`. Còn lại bị filter ở HAL để tiết kiệm token.

HAL does the categorisation before sending. On the `Activity detected:` line:
- Drink actions (listed below) collapse to the bucket name `drink`.
- Break actions (listed below) collapse to the bucket name `break`.
- Celebrate actions (listed below) collapse to the bucket name `celebrate`.
- Eat actions are emitted as raw Kinetics labels (no collapsing) so the agent can ground the reaction phrasing + per-food UI icon in the specific food.
- Sedentary actions are emitted as raw Kinetics labels (no collapsing), EXCEPT `reading book` + `reading newspaper` which both collapse to the generic label `reading` (a phone misread as "reading newspaper" is still truthfully "reading" — the collapse avoids asserting the wrong medium).
- Tired actions are emitted as raw Kinetics labels (no collapsing) — `yawning` is fatigue evidence the wellbeing skill reads directly.
- Emotional actions are filtered out entirely — they do not appear on `motion.activity`. A dedicated `motion.emotional` event will carry them later.

HAL đã categorize trước khi gửi. Trên dòng `Activity detected:`:
- Action drink (liệt kê dưới) gộp thành bucket name `drink`.
- Action break (liệt kê dưới) gộp thành bucket name `break`.
- Action celebrate (liệt kê dưới) gộp thành bucket name `celebrate`.
- Action eat giữ raw Kinetics label (không gộp) để agent có context món ăn cụ thể cho reaction + icon UI.
- Action sedentary giữ raw Kinetics label (không gộp), TRỪ `reading book` + `reading newspaper` gộp thành label chung `reading`.
- Action tired giữ raw Kinetics label (không gộp) — `yawning` là bằng chứng mệt mỏi, wellbeing skill đọc trực tiếp.
- Action cảm xúc bị filter hoàn toàn — không xuất hiện trên `motion.activity`. Sẽ có event `motion.emotional` riêng sau.

> Selection rule / Quy tắc chọn label: a class earns its place only if it is **not a magnet** for a look-alike action with no Kinetics class of its own. Collapsed buckets (`drink`, `break`, `celebrate`) carry the whole assertion the agent will speak, so any wrong member = a confident false statement — prune aggressively. Classes removed for this reason: `making tea`, `eating chips` (nail biting), and the reflex/social noise `sneezing`, `sniffing`, `hugging`, `kissing`, `headbanging`, `sticking tongue out`.
>
> Second rule / Quy tắc thứ hai: a class only belongs in a bucket if it **is** the bucket's action, not a step towards it. Preparation look-alikes assert something that never happened — `opening bottle` was removed from `drink` for this reason (a bottle opened is not a drink taken, yet it reset the hydration timer and made the agent count a drink). Một class chỉ thuộc bucket khi nó **chính là** hành động của bucket, không phải bước chuẩn bị.

## drink — reset hydration timer / Reset timer nhắc uống nước

- drinking — uống nước
- drinking beer — uống bia
- drinking shots — uống shot
- tasting beer — nếm bia

## break — reset break timer / Reset timer nhắc nghỉ

- stretching arm — vươn tay
- stretching leg — vươn chân

## celebrate — upbeat reaction / Phản ứng ăn mừng

- celebrating — ăn mừng
- clapping — vỗ tay
- applauding — vỗ tay (khen)

## eat — meal signal (raw labels kept) / Tín hiệu bữa ăn (giữ raw label)

- tasting food — nếm đồ ăn
- dining — ăn cơm
- eating burger, eating cake, eating carrots, eating doughnuts, eating hotdog, eating ice cream, eating spaghetti, eating watermelon

## sedentary — sedentary streak + posture window, fed to wellbeing nudges (no cron, no music trigger) / Ngồi yên — sedentary streak + pose window, cho wellbeing nhắc nhở (không cron, không kích hoạt gợi ý nhạc)

- using computer — dùng máy tính
- writing — viết
- texting — nhắn tin
- reading book — đọc sách → emitted as `reading`
- reading newspaper — đọc báo → emitted as `reading`
- drawing — vẽ
- playing controller — chơi game

## tired — fatigue evidence (raw label kept) / Bằng chứng mệt mỏi (giữ raw label)

- yawning — ngáp

Not in `sedentary` on purpose: `has_sedentary` starts the sedentary streak and opens the pose window, so a yawn mid-stretch would stop a real break from resetting either. It is also excluded from the coarse-class set used by the cooldown's transition bypass — a yawn modifies what the user is doing, it isn't a change of activity. Downstream: the agent acknowledges the yawn out loud like a drink or a meal (route #1b, throttled to once an hour via a `noted_yawn` marker, and only before 21h so the evening yawn goes to sleep wind-down instead), lowers the break-nudge threshold to `BREAK_THRESHOLD_TIRED` (20 min), and confirms sleep-winddown.

Cố ý không đặt vào `sedentary`: `has_sedentary` khởi động sedentary streak và mở pose window, nên một cái ngáp giữa lúc vươn vai sẽ khiến break thật không reset được. Nó cũng bị loại khỏi coarse-class set của transition bypass — ngáp là modifier chứ không phải đổi hoạt động. Downstream: agent nói ra cái ngáp như với drink hay bữa ăn (route #1b, giới hạn mỗi giờ một lần qua marker `noted_yawn`, và chỉ trước 21h để cái ngáp buổi tối nhường cho sleep wind-down), hạ ngưỡng break-nudge xuống `BREAK_THRESHOLD_TIRED` (20 phút), và xác nhận sleep-winddown.

## emotional — filtered out (future `motion.emotional`) / Bị filter (event `motion.emotional` sau)

- laughing — cười
- crying — khóc
- singing — hát
