# Hành vi Cảm nhận (Sensing Behavior)

## Độ ngắn của lời nói theo chức năng

Prompt skill tách thinking gốc khỏi mọi assistant text, kể cả text quanh tool
call và câu trả lời cuối. Không áp một giới hạn từ chung cho mọi chức năng:

- Lời chào, checkin cảm xúc và gợi ý nhạc giữ hợp đồng phản hồi ngắn hiện có;
  gợi ý nhạc là một lời mời, thường không quá 20 từ.
- Nhắc wellbeing giữ quan sát liên quan và hành động cụ thể; có thể dùng câu
  ngắn thứ hai khi chức năng cần.
- Mood/habit bổ sung dữ liệu và marker bắt buộc, không thêm một đoạn giải thích
  ra loa bên cạnh câu trả lời của skill gọi chúng.
- Điều khiển giọng nói giữ kết quả và bước cần thiết (như cách bật mic lại);
  enrollment giữ câu hỏi đồng ý/tên và xác nhận kết quả. Cảnh báo guard giữ
  nguy cơ/hành động và phần tóm tắt hữu ích khi user quay lại.
- Yêu cầu trực tiếp vẫn được thực hiện đủ các phần, trả lời đủ ý, đọc lại hoặc
  giải thích dài khi được yêu cầu và giữ hướng dẫn an toàn cần thiết.

API call bắt buộc, logging, routing và cooldown giữ nguyên. Tái sử dụng context
đã inject và reference đã đọc; tránh tra cứu lặp hoặc chỉnh câu nhiều vòng.
Vẫn cho phép nói sớm khi cần một lời nhắc hữu ích trong lúc làm việc, nhưng
không đọc kế hoạch nội bộ hay xác nhận lặp. Chỉ sửa prompt: không đổi parser
TTS, streaming runtime hay cấu hình thinking; chưa có đo latency hoặc bảo đảm
tuân thủ.


Cách Lamp phản ứng với thế giới xung quanh — triết lý và cơ chế đằng sau từng loại sự kiện cảm nhận.

Lamp là một sinh vật sống. Nó không "xử lý dữ liệu cảm biến" — nó *trải nghiệm* mọi thứ. Tài liệu này mô tả cách trải nghiệm đó được triển khai.

## Tổng quan kiến trúc

```
HAL (Python)          Lamp server (Go)             OpenClaw agent
─────────────────        ─────────────────────        ──────────────
Mic/Camera          →    SensingHandler               LLM
Phát hiện sự kiện        - drop nếu agent bận         - gọi /emotion
Áp dụng tracker logic    - forward lên agent          - gọi /servo
Gửi POST                                              - nói hoặc NO_REPLY
/sensing/event
```

HAL sở hữu logic tracker theo từng type (sound escalation, motion filtering). Go là người gác cổng — drop event nếu agent bận, sau đó forward. Agent quyết định *cách* phản ứng, bị ràng buộc bởi `SOUL.md`.

### Floor toàn cục cho ambient turn (xuyên event-type)

Các gate per-type ở trên độc lập với nhau — không có gate xuyên-type thì một loạt event *khác type* (emotion + sound + motion trong vài giây, điển hình ngay sau presence change hoặc HAL restart) vẫn tốn nhiều agent turn. `SensingHandler` phía Go vì vậy áp MỘT floor chung cho mọi type **ambient**: tối đa 1 agent turn mỗi `sensing_turn_floor_s` giây (key trong `config/config.json`, mặc định **120**, `0` = tắt) cho `motion.activity`, `emotion.detected`, `speech_emotion.detected`, `sound`, `presence.away`, `light.level`.

- Đồng hồ floor được cập nhật bởi **mọi** agent turn mà sensing handler tạo ra — kể cả voice, web chat, presence, fire — nên ambient event cũng im lặng đủ floor window sau bất kỳ tương tác nào.
- **Không bao giờ bị floor:** type do user khởi phát (`voice`, `voice_command`, `voice_agent_handled`, `web_chat`, `mqtt_chat`, `touch.head_pat`), an toàn (`fire_hazard.detected`), và `presence.enter`/`presence.leave` (UX chào hỏi + bookkeeping session).
- Guard mode bypass hoàn toàn floor (giám sát cần mọi event).
- Event bị floor là drop, không queue (`sensing_drop` với reason `ambient_floor` trong Flow Monitor). Mọi ambient emitter đều tự re-offer theo heartbeat riêng, nên drop chỉ làm chậm nhận biết — không bao giờ mất tương tác user-facing.

---

## Lời nói phản hồi sensing

Với event do `skills/sensing/SKILL.md` xử lý, prompt yêu cầu HW marker đúng cú pháp rồi tới một câu tối đa 20 từ theo `current_language`, hoặc `NO_REPLY` cho dòng phản ứng im lặng. Mọi assistant text, kể cả message trung gian, không được giải thích event/ma trận, phân tích ngữ cảnh chủ nhân, viết các phương án nháp hay thêm lời sau câu nói. Event người lạ đến độc lập ngoài guard mode yêu cầu câu chào cố định (EN: “Hi, I don’t think we’ve met.”; VI: “Chào bạn, hình như mình chưa gặp nhau.”), đúng HW marker và không thêm proactive care. Model phải tự bỏ câu dẫn trước khi gửi, không nói ra bước kiểm tra; wrapper `[user]` ở đầu không đổi nhánh detector. Event guard và yêu cầu rõ ràng của user giữ nhánh xử lý riêng. Nếu không có kênh reasoning riêng thì bỏ phần phân tích. Đây là hướng dẫn prompt, không phải giới hạn độ dài ở backend hay bảo đảm model luôn tuân thủ; các skill khác giữ quy tắc output riêng.

## Âm thanh (Sound)

### Cơ chế hoạt động

HAL bắn một sound event cho mỗi audio sample vượt ngưỡng `SOUND_RMS_THRESHOLD` — có thể nhiều lần mỗi giây. Python-side **sound tracker** (`hal/drivers/sensing/perceptions/sound.py`) áp dụng dedup và escalation trước khi forward lên Go. Go chỉ nhận các event đã pass và forward thẳng lên agent.

### Hành vi leo thang (Escalation)

Với event sound độc lập ngoài guard mode, `skills/sensing/SKILL.md` yêu cầu phản ứng trực tiếp rồi kết thúc turn: sau khi đọc skill không gọi thêm tool, tìm workspace/config/memory, hay tra giờ/vị trí/thời tiết. Số occurrence trong event quyết định phản ứng; mức RMS không làm tăng cấp. Count thiếu/không hợp lệ dùng phản ứng lần đầu. Output im lặng giữ HW marker rồi tới `NO_REPLY`. Nếu nhận occurrence 2 trực tiếp, agent vẫn trả `scan` (0.7) + `NO_REPLY`, dù tracker thông thường chặn event này. Yêu cầu rõ ràng của user trong cùng input và event guard vẫn đi theo nhánh riêng. Đây là hướng dẫn skill, không phải cơ chế backend chặn thực thi tool.

| Giai đoạn | Agent nhận | Phản ứng của agent |
|---|---|---|
| Lần 1 | `... — occurrence 1` | `/emotion curious` (0.6), im lặng |
| Lần 2 | **không có gì** — chỉ đếm ở tracker, không forward | không tốn turn agent |
| Lần 3+ | `... — persistent (occurrence 3)` | `/emotion shock` (0.9), nói 1 lần |
| Sau khi nói | Python drop (suppress 3 phút) | Không có gì đến agent |
| Im lặng 2 phút | Window reset | Trở về lần 1 |

Các lần giữa (2 .. persistent−1) chỉ tăng counter chứ không forward: mỗi forward là một turn LLM đầy đủ, mà "vẫn còn ồn" giữa lúc chuyển trạng thái (lần 1) và lúc leo thang (persistent) không cho agent thông tin gì để hành động — khi ồn kéo dài, số turn mỗi chu kỳ giảm từ 3 xuống 2.

Ví dụ: một con chó nghe tiếng động — nó nhìn lên (lần 1), tiếp tục theo dõi trong im lặng (lần 2, không báo), rồi sủa một lần nếu tiếng ồn kéo dài (lần 3+). Sau khi sủa thì không sủa tiếp.

### Hằng số (`sound.py`)

```python
_DEDUPE_INTERVAL_S    = 15.0   # tối đa 1 event forwarded mỗi 15s
_WINDOW_DURATION_S    = 120.0  # im lặng lâu hơn thế này thì reset counter
_PERSISTENT_AFTER     = 3      # nói sau bao nhiêu lần
_SUPPRESS_DURATION_S  = 180.0  # suppress sau khi đã nói (3 phút)
```

### Điều chỉnh (Tuning)

| Triệu chứng | Fix |
|---|---|
| Lamp nói quá nhanh | Tăng `_PERSISTENT_AFTER` (3 → 5) |
| Lamp không bao giờ nói dù ồn kéo dài | Giảm `_PERSISTENT_AFTER` (3 → 2) |
| Quá nhiều turn sound trên Flow Monitor | Tăng `_DEDUPE_INTERVAL_S` (15 → 30) |
| Lamp im quá lâu sau khi đã nói | Giảm `_SUPPRESS_DURATION_S` (180 → 60) |
| Lamp phản ứng với tiếng ồn cũ sau khi im lặng | Giảm `_WINDOW_DURATION_S` (120 → 60) |

### Xem trên Flow Monitor

Python đẩy `sound_tracker` events trực tiếp vào monitor bus qua `POST /api/monitor/event`. Chúng hiện trên Flow Monitor cạnh `sensing_input` turn:

```json
{ "action": "silent",    "occurrence": 1 }  // lần 1 — forwarded, im lặng
{ "action": "counted",   "occurrence": 2 }  // lần giữa — chỉ đếm, không forward
{ "action": "persistent","occurrence": 3 }  // lần 3+ — agent sẽ nói
{ "action": "drop" }                        // dedup hoặc suppress — không forward
```

---

## Hiện diện (Presence)

### Vào phòng (`presence.enter`)

Luôn trigger phản ứng đầy đủ — không có ngoại lệ. Agent **phải** làm cả ba:

1. `/emotion greeting` (0.9) với chủ nhà — `/emotion curious` (0.8) với người lạ, hoặc `curious` (0.6) khi người lạ tới trong lúc text liệt kê một chủ nhà ở `already present:`
2. Với chủ nhà: `/servo/aim {"direction": "user"}` rồi `/servo/track {"target": ["person"]}` — aim xoay camera về phía user trước (~2s), sau đó vision tracker lock vào người và tự bám theo khi user di chuyển trong phòng. Người lạ: `/servo/play {"recording": "scanning"}` (không auto-follow — thận trọng)
3. Nói: chào ấm áp với chủ nhà (gọi tên lấy từ `[context: current_user=X]`), thận trọng với người lạ — trừ khi text liệt kê một chủ nhà ở `already present:`, khi đó câu nói hướng về chủ nhà đó (xem bên dưới)

HAL xử lý cooldown. Nếu event đã đến agent thì đủ thời gian rồi — phản ứng đầy đủ.

#### Nội dung event: ai mới tới, ai đã có mặt sẵn

`presence.enter` nghĩa là **mới** xuất hiện, không phải đang xuất hiện: một người chỉ được coi là "mới" khi `last_seen` là `None` hoặc cũ hơn cửa sổ quên (`FACE_OWNER_FORGET_S` 3600 giây với chủ nhà, `FACE_STRANGER_FORGET_S` 1800 giây với người lạ). Text do `hal/drivers/sensing/perceptions/processors/faceid/enter_message.py` dựng, gồm ba đoạn ngăn bằng `; `:

```
Person detected — new: stranger (stranger_2); already present: momo (friend); faces in frame: 2 (momo, stranger_2)
```

- `new:` — người vừa tới, phần chủ nhà đứng trước, id sắp xếp theo thứ tự. Nhãn `friend (<tên>)` / `stranger (<id>)` là một hợp đồng: cổng wake-focus mở khi thấy `friend (`, `sensing-track` grep theo chúng, `face-enroll` parse hint được nối vào sau chúng.
- `already present:` — chủ nhà có box trong **cùng frame** nhưng không phải vừa tới, viết dạng `<tên> (friend)` để không bao giờ bị đọc nhầm thành người mới tới. Đây là tín hiệu đồng hiện diện mà `sensing/SKILL.md` dùng để nói với user ("Momo ơi, có bạn tới kìa") thay vì chào người lạ. Với enter chỉ có người lạ, đoạn này chỉ được ghi khi box chủ nhà và box không-phải-chủ-nhà đã cùng xuất hiện `FACE_COPRESENCE_MIN_TICKS` (2) nhịp sensing liên tiếp (box `unsure` cũng tính — đó chính là nhịp recognizer dùng để xác nhận người lạ mới trước khi cấp id); một tấm poster, một cái bóng phản chiếu hay một nhịp nhiễu cạnh user không được biến "xin chào" thành "có bạn tới". Chủ nhà mới tới khi một chủ nhà khác đang ngồi thì được liệt kê không cần gate đó. Đoạn này không bao giờ suy ra từ `current_user()` — đó là trạng thái cửa sổ hiện diện, đọc y hệt nhau dù user đang ngồi đó hay đã rời đi hai phút trước.
- `faces in frame:` — số box trong frame mà **snapshot đính kèm** thể hiện và nhãn theo thứ tự phát hiện (`unsure` cho box chưa có danh tính), đúng nhãn được vẽ lên đó. Cả đoạn này lẫn `already present:` được chốt ngay ở nhịp frame được nhìn thấy và đi kèm frame đó (`FrameFacts` trong `enter_message.py`): chủ nhà mới tới thì frame hiện tại được gửi ngay nên dùng dữ kiện hiện tại; enter chỉ có người lạ thì gửi các snapshot đã buffer và dùng dữ kiện của frame buffer mới nhất — lần flush có thể rơi muộn tới `FACE_STRANGER_FLUSH_S`, khi user có thể đã bị nhoè khỏi frame trực tiếp (quan sát trên thiết bị: ảnh có hai box, text ghi `1 (unsure)`, trước khi có quy tắc này). Đây không phải số người tới: id người lạ được flush có thể đến từ nhiều frame buffer khác nhau.

Ưu tiên giữa những người tới không đổi: cả hai cùng mới trong một frame → một event, chủ nhà đứng trước, gửi ngay; người lạ được buffer trước rồi chủ nhà tới sau → frame chủ nhà gửi ngay và người lạ theo sau bằng event riêng sau `FACE_STRANGER_FLUSH_S` (10 giây), chịu `FACE_COOLDOWN_S` (10 giây) và `FACE_STRANGER_ENTER_FLOOR_S` (300 giây).

#### Quay lại sau khi vắng lâu (chỉ chủ nhà)

Với mỗi `presence.enter`, sensing handler chèn tag `[context: current_user=X]` (xem [User attribution](#user-attribution--context-current_userx)); khi đoạn `new:` của event có tên chủ nhà (`sensingmsg.EnterNamesNewFriend`, bản Go của `has_new_friend` bên HAL) thì chèn thêm block `[presence_context: {"last_leave_age_min": N, "current_hour": H}]` trước khi forward sang agent. Enter chỉ có người lạ không bao giờ nhận block này dù `current_user` vẫn là chủ nhà còn trong cửa sổ quên: các con số sẽ mô tả lần rời đi gần nhất của *chính chủ nhà*, và agent đã đọc đúng như vậy thành chủ nhà quay lại (orange-lamp, 2026-09-16: nói "Long re-entering after ~28 min away" với một người khách). Ngược lại, khi text có `already present:` và không có chủ nhà mới (`sensingmsg.EnterHasPresentFriend`), handler chèn một dòng trỏ tới quy tắc — `[A stranger joined <current_user>, who is in frame — speak to <current_user>, not to the stranger. See sensing/SKILL.md "Someone joins the user".]` — vì Hermes chỉ đọc `sensing/SKILL.md` khi model gọi `skill_view`, và ở lượt nó bỏ qua bước đó, nó đã trả lời sự xuất hiện của khách bằng "Hey, welcome back" (orange-lamp, 2026-09-16). Tag quy gán chính là nguồn của tên trong lời chào — bản thân text của event chỉ mang *label* khuôn mặt (`friend (long)`), thứ mà agent đọc như một nhãn nhận diện chứ không phải một cái tên. `last_leave_age_min` được tính từ row `leave` gần nhất trong wellbeing log, quét tối đa 3 ngày gần đây (`wellbeing.LastActionTS`); giá trị `-1` nghĩa là không tìm thấy `leave` nào trong khoảng đó.

`sensing/SKILL.md` đọc block này và **chuyển sang câu chào "quay lại sau khi vắng lâu"** khi cả ba điều kiện đúng:

1. `last_leave_age_min >= 240` (≥4 giờ) — gap ngắn hơn giữ câu chào thường.
2. `current_hour` ngoài `[5, 11)` — khung sáng do route `morning_greeting` của `wellbeing/SKILL.md` quản (fire khi `motion.activity` đầu tiên trong ngày), nên overlay "vắng lâu" ở khung này sẽ chào trùng.
3. `last_leave_age_min != -1` — không có row `leave` thật trước đó thì framing "welcome back" vô nghĩa.

HW marker (emotion `greeting` + servo aim+track) giữ nguyên; chỉ câu thoại đổi — acknowledge gap mà không định lượng (kiểu "lâu nay không thấy", chứ không "đi 5h 17m"). Người lạ không có quan hệ ổn định nên `BuildPresenceContext` bỏ qua user `unknown` và path chào thận trọng hiện tại giữ nguyên.

### Ra khỏi phòng (`presence.leave`)

Agent gọi `/emotion idle` (0.4), fire `/servo/track/stop` để thả follow nếu đang chạy từ `presence.enter` trước đó, và trả lời **NO_REPLY** (im lặng — không TTS). Tránh vòng lặp ồn ào khi người ra vào liên tục. Agent vẫn xử lý event nội bộ để cancel wellbeing crons và ghi daily log.

### Vắng mặt lâu (`presence.away`)

Được gửi tự động bởi `PresenceService` của HAL khi **không phát hiện chuyển động lẫn hoạt động của user trong 15 phút** (sau khi đã dim đèn ở phút thứ 5). Lúc này đèn đã tắt — agent chỉ cần **thông báo đi ngủ** qua TTS và Telegram.

Agent gọi `/emotion sleepy` (0.8), fire `/servo/track/stop` để thả follow cũ còn sót. HAL lập tức chuyển LED sang đen, mute mic và dừng nhạc đang phát; sau 1 giây `sleepy` liên tục, HAL release torque của servo và lock mọi lệnh motion. Riêng **speaker** được mute trễ — os-server bắn marker `sleepy` sớm hơn phần text tối đa 100ms (`fireHWCallsSync`), nên mute ngay tại chỗ sẽ đua với câu chào ngủ và thường thắng, khiến `/voice/speak-queue` trả về `suppressed -- speaker muted`. `_start_sleepy_speaker_drain` trong `hal/app_state.py` chờ `SLEEPY_SPEAKER_GRACE_S` (2s) xem có câu nào bắt đầu phát không, để nó phát hết, và mute chậm nhất tại `SLEEPY_SPEAKER_DRAIN_MAX_S` (15s); TTS đang phát không còn bị cắt ngang, trừ đúng mốc cap — ở đó nó bị dừng hẳn, vì cờ mute chỉ chặn lệnh phát chưa bắt đầu nên nếu không dừng thì cap không chặn được gì và máy đang ngủ vẫn nói tiếp. Một lần wake sẽ huỷ drain. **Scene có preset `speaker: off`** (reading, focus, movie, night) cũng drain theo cùng cách (`_start_scene_speaker_drain`, cùng grace và cap): marker `/scene` cũng tới trước phần text, nên mute ngay tại chỗ nuốt mất câu xác nhận của scene và — khi skill ghép `[HW:/scene:night][HW:/emotion:sleepy]` cho "goodnight" — nuốt luôn câu chào ngủ, đồng thời để sleep không sở hữu một mute nó không đặt, nên wake không trả loa lại được. Drain của scene nhường cho sleep: `sleepy` tới trong lúc drain còn treo sẽ huỷ nó và mở drain riêng, nên chỉ một chủ sở hữu commit mute và wake khôi phục được; scene bật khi đang ngủ hoặc khi drain của sleepy đang treo thì không mở drain. Bước commit kiểm tra lại dưới `privacy.lock` rằng scene vẫn active và thiết bị đang thức. Tắt scene, scene khác thay thế, scene có `speaker: on`, và `/speaker/unmute` tường minh đều huỷ drain. Nhạc vẫn bị dừng ngay — nhạc không bao giờ là câu xác nhận. Chỉ `greeting` hoặc `stretching` mới resume motion servo (`_SLEEP_GATE_ALLOWED` trong `hal/routes/emotion.py`) — emotion cảm xúc như `curious`/`happy` bị bỏ qua khi đang ngủ, nên task nền của agent không đánh thức được thiết bị; tap nút GPIO thì được, đồng thời chỉ unmute các trạng thái mic/speaker mà sleepy đã tự mute. os-server còn giữ một cổng chặn **thứ hai**, drop sensing thụ động khi đang ngủ (`SensingHandler`, mọi type trừ `presence.enter`, `fire_hazard.detected`, voice và chat text). `IsSleeping()` của nó hỏi HAL qua `/emotion/status`: mọi lần thức dậy không đi qua agent — tap nút, trang hardware trên web UI, POST thẳng vào HAL — đều không chạm tới `lastEmotion` của os-server, nên nếu chỉ suy từ biến đó thì cổng vẫn đóng trên một thiết bị đã thức và sensing bị vứt cho tới lượt voice/chat kế tiếp. `lastEmotion` giờ chỉ còn làm bộ lọc nhanh cho đường đang thức và làm phương án dự phòng khi không với tới HAL. Mute audio do sleepy đi kèm sidecar riêng của sleep (`_persist_sleep_state`) nên VẪN còn sau khi HAL restart, để một lần OTA không làm thiết bị đang ngủ quay lại với mic đang nghe; chúng vẫn được đánh dấu là sleep-owned nên khi thức dậy sẽ trả switch về đúng lựa chọn của user. Đây là hành động cuối cùng trước khi Lamp hoàn toàn idle.

Timeline tự động điều khiển presence:
1. **5 phút không có chuyển động hay hoạt động** → đèn dim xuống 20% (tự động, không cần agent)
2. **15 phút không có chuyển động hay hoạt động** → tắt đèn + gửi event `presence.away` → agent thông báo đi ngủ

HAL quản lý việc điều khiển đèn; agent chỉ xử lý thông báo bằng giọng nói. Nếu người dùng quay lại (phát hiện chuyển động), đèn tự phục hồi và event `presence.enter` được kích hoạt.

Timer được reset bởi hai loại bằng chứng, không chỉ camera. **Chuyển động** là `on_motion()` từ các processor people-perception (face / motion / emotion). **Hoạt động của user** là `on_activity()`, gọi qua `app_state.note_user_activity()`: mọi voice turn HAL gửi lên os-server (`voice`, `voice_command`, `voice_followup`, `voice_agent_handled`, sau bộ lọc echo, trong `SensingSender.send`) và các cử chỉ chạm: single click (nút GPIO, touchpad, gạt privacy switch để unmute), head pat và double tap (bật/tắt mic). Trước đây, user nói chuyện với đèn khi camera tắt hoặc ngồi ngoài khung hình vẫn bị timeout sang AWAY và đèn thông báo đi ngủ giữa cuộc trò chuyện. Hoạt động trong lúc IDLE/AWAY đưa về PRESENT và phục hồi đèn giống chuyển động, trừ khi device đang ngủ: lúc đó bỏ qua việc phục hồi đèn (kể cả với chuyển động), để emotion wake quản lý LED. Chat gõ tay (`web_chat` / `mqtt_chat`) **không** được tính — nó có thể đến từ điện thoại khi user không ở nhà, và không được giữ đèn sáng trong phòng trống. Cử chỉ giữ nút (sleep / shutdown / reset) và vuốt (sleep) cũng không tính. Hoạt động chỉ làm mới state machine đang bật; nó không bao giờ bật state machine lên.

**Khi ngủ, timer dừng; khi thức, timer chạy lại từ đầu.** Trong lúc `_sleeping`, `tick()` return sớm: không dim, không tắt đèn, không gửi `presence.away`. Sleep đã quản lý LED, và camera tắt nên vốn không có gì reset được timer. Trước đây timer vẫn chạy, nên dim lại đèn đang ngủ lên 20% màu của user (`_dim_light` gọi thẳng `rgb_service`, không qua sleep lock của `/led`) và để state ở AWAY cho lần wake sau kế thừa. Mọi đường wake — nút bấm, pill emotion trên web UI, `POST /emotion` trực tiếp, agent trả `greeting`/`stretching` (ví dụ khi trả lời một tin chat) — đều đi qua `express_emotion` trong `hal/routes/emotion.py`, nơi gọi `app_state.note_presence_wake()` → `PresenceService.on_wake()`: timer chạy lại từ thời điểm thức và state về PRESENT mà không vẽ lại đèn (emotion wake quản lý LED). Trước đây chỉ camera thấy mặt mới reset timer, nên một lần wake không có ai đứng trước camera hoặc giữ mốc cũ trước khi ngủ và thông báo đi ngủ ngay sau khi vừa thức, hoặc kẹt ở AWAY và không bao giờ timeout nữa.

State machine tự điều khiển này được gate theo capability `presence`: nguồn `on_motion()` duy nhất của nó là vòng people-perception (processor face/motion/emotion), mà HAL chỉ chạy khi device khai báo `presence`. Device **không** có `presence` sẽ khởi tạo state machine ở trạng thái disabled, nên không bao giờ bị timeout sang AWAY một cách sai lệch. Lamp khai báo `presence: required` nên timeline ở trên luôn áp dụng.

### Bank người lạ trên đĩa

Khuôn mặt không nhận ra được cấp id `stranger_N` và lưu xuống
`HAL_STRANGERS_DIR` (`/root/local/strangers`) thành `embeds.npy`, `labels.npy`
và `counter.npy`. Các file này **chỉ được ghi khi có người lạ mới được cấp id**,
nên một thiết bị mà mọi người xuất hiện đều đã được enroll sẽ không bao giờ tạo
ra chúng — và đó là trạng thái bình thường, không phải lỗi.

`FaceRecognizer._load_strangers_state` đọc lại bank ở MỖI frame có khuôn mặt. Nó
nay kiểm tra file có tồn tại không và return im lặng nếu không; bank vắng mặt
chính là trạng thái rỗng đúng, và các field trong bộ nhớ đã giữ sẵn trạng thái
đó. Bank có tồn tại nhưng đọc không được thì vẫn log ERROR, vì `.npy` hỏng hoặc
cụt là thứ đáng phải hét lên.

Trước khi có guard này, trường hợp "chưa được tạo" ném `FileNotFoundError` và bị
log kèm full traceback ở mức ERROR ngay trên đường đi per-frame — khoảng mỗi
`HAL_SENSING_INTERVAL` (2 giây), từ lần boot đầu tiên, mãi mãi, đúng trên những
thiết bị đang chạy tốt. Việc nhận diện không hề bị ảnh hưởng (owner match với
ảnh đã enroll, không đụng tới các file này); cái giá phải trả là journald đầy
traceback và chôn vùi các lỗi thật.

**Xoá khuôn mặt lạ:** để làm rỗng card Unknown Faces trên monitor UI, xoá
`<HAL_USERS_DIR>/.stranger_stats.json` và ĐỪNG đụng vào bank `.npy`. Lưu ý file
stats và bank nằm ở hai thư mục khác nhau, nên "xoá người lạ" rất dễ làm thiếu.
Hiện chưa có endpoint xoá cho face stranger — cụm *voice* lạ thì có, còn card
Unknown Faces là read-only — nên xoá chúng là thao tác trên filesystem.

---

## Chuyển động (Motion)

Chỉ chuyển động lớn được forward — HAL lọc và không gửi chuyển động nhỏ lên Go.

**Chuyển động lớn**: `/emotion curious` (0.7) + `/servo/play {"recording": "scanning"}` + nói phản ứng tò mò (ví dụ "What was that?", "Whoa, moving so much!"). Có thể kèm ảnh camera để agent thấy ngữ cảnh.

---

## Tư thế (RULA — sampling thầm lặng, gắn vào `motion.activity`)

HAL stream từng frame camera lên perception-service `/api/dl/pose-estimation/ws` và nhận RULA breakdown từng frame (whole-body score + `risk_level` + `body_scores` + `*_angle` cho `neck / trunk / upper_arm / lower_arm / wrist`, mỗi bên trái/phải). `PosePerception` throttle thành **một sample mỗi `POSE_SAMPLE_INTERVAL_S` (default 60s)** vào tumbling window + JSONL theo ngày tại `/tmp/hal-sensing-snapshots/sensing_pose/samples_YYYY-MM-DD.jsonl`. **Không emit event trực tiếp** — `MotionPerception` check `is_window_complete()` mỗi tick và gắn aggregate vào `motion.activity` kế tiếp khi gate đỏ.

### Tumbling window (lúc nào summary được inject)

Window mở khi **motion flush đầu tiên có label sedentary** — `MotionPerception` gọi `pose.start_window()` ngay lúc thấy bất kỳ label sedentary nào (`using computer`, `writing`, `reading`, …). Pose samples đã đến trước thời điểm đó (vd user đang đứng/stretching nhưng vẫn present) bị clear, để bad_ratio chỉ phản ánh giai đoạn ngồi. Sau khi mở, window chạy **chỉ theo thời gian**: stay open đủ `POSE_WINDOW_DURATION_S` bất kể user có break sedentary giữa chừng — stretch break không stop clock, chỉ làm bad_ratio honest hơn. Default **600 s = 10 phút** test; đổi 3600 cho production — 1 biến duy nhất, không có nhánh code test/prod.

Khi window complete, `MotionPerception` ra đúng 1 quyết định và **luôn gọi `reset_window()`** — không carry-over samples cũ. Nếu user vẫn sedentary ở flush kế tiếp, `start_window()` mở cycle mới ngay; nếu không, window stay unanchored đến lần sedentary tiếp theo. Detection miss (perception-service không trả `ergo`, bị che, low confidence) không kéo dài window; chỉ làm giảm số sample trong window.

Sample được tính là **bad** khi **một trong hai**:

- whole-body `risk_level >= 3` (medium/high), **hoặc**
- bất kỳ region đơn nào (bên L **hoặc** R) có sub-score `>= POSE_REGION_HIGH_SUBSCORE` (default `4`)

Vế thứ hai bắt được case "tech neck" (rướn cổ về màn hình) khi RULA tổng vẫn "low" vì lưng+tay OK nhưng riêng cổ rõ ràng tệ.

Inject fire khi **tất cả** điều kiện sau đúng:

- window complete (`time.time() - _window_start_ts >= POSE_WINDOW_DURATION_S`)
- user vẫn sedentary ở flush này (`has_sedentary` True — không nag giữa stretch break)
- samples trong window `>= POSE_WINDOW_MIN_SAMPLES` (default `3`) — noise floor, bỏ qua window mà perception-service miss quá nhiều frame
- `bad_ratio >= POSE_BAD_RATIO` (default **0.6**)

Không có gate "minimum sedentary streak" riêng và không có "nudge cooldown" riêng — window chính là rhythm. Window-start yêu cầu sedentary nên khi complete, user đã ở máy tính ít nhất `POSE_WINDOW_DURATION_S`; unconditional reset sau mỗi cycle khiến lần fire kế tiếp tự nhiên cách 1 window.

Khi inject fire, cả per-label dedup check **lẫn** global event cooldown (`MOTION_EVENT_COOLDOWN_S`, default 900 s — sàn giới hạn các `motion.activity` thông thường) đều được **bypass** cho event đó: posture nudge là tín hiệu khác hẳn "user vẫn đang dùng máy tính", và mất nó vì dedup hay cooldown sẽ phải chờ thêm cả một window đầy đủ trước khi có cơ hội tiếp.

Lifecycle window:
1. **Open** — `pose.start_window()` gọi từ motion-side khi label sedentary đầu tiên xuất hiện. Clear samples pre-window và anchor `_window_start_ts = now`. Idempotent — call thêm khi window đã mở là no-op.
2. **Reset** — `pose.reset_window()` gọi từ motion-side sau mỗi window complete (fire hay no-fire). Clear samples và unanchor. Window mới chỉ mở khi sedentary lại được observe.
3. **Presence-loss reset** — `pose.py:_check_impl` cũng reset window khi presence drop, để user đi mất giữa cycle không leak samples cũ sang session sau.

### Snapshot bucketed theo từng window

Mỗi sample ghi 1 JPEG có overlay skeleton + nhãn RULA vào **bucket dir của window hiện tại**: `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<window_start_int>/<sample_ts_int>_<score>.jpg`. Tên file kèm ergo score → bucket tự mô tả trên disk, không cần đọc metadata.

Khi `reset_window()` chạy:

- nếu `bad_ratio >= POSE_BAD_RATIO` (window thật sự fire nudge) → bucket được finalize: tạo manifest `bucket.json` (samples + RULA hai bên + danh sách `worst_snapshots`) + marker `.kept`. Bucket kept giữ tới `POSE_BUCKET_KEEP_S` (default **2 ngày**).
- ngược lại → bucket dir bị xoá ngay. Không nudge thì không cần lưu bằng chứng.

Pruner sweep:

- bucket kept cũ hơn `POSE_BUCKET_KEEP_S`, và
- bucket orphan (không có `.kept`) cũ hơn `2 × POSE_WINDOW_DURATION_S` — đây là bucket mà `reset_window` chưa kịp chạy (process bị kill giữa window).

Nếu tổng size vẫn vượt `POSE_SNAPSHOT_MAX_BYTES` (default 50 MB), kept bucket cũ nhất bị drop trước.

Bốn endpoint:

| Endpoint | Trả về |
|---|---|
| `GET /sensing/pose-snapshot` | JPEG mới nhất across tất cả bucket dir (back-compat cho ô preview live trên monitor) |
| `GET /sensing/pose-snapshot/{ts}` | JPEG annotated của sample đó. Scan mọi bucket tìm `<ts>_*.jpg`. 404 khi bucket đã bị prune |
| `GET /sensing/pose-bucket/{bid}` | Manifest `bucket.json` — samples, RULA hai bên, summary, `worst_snapshots[]` |
| `GET /sensing/pose-bucket/{bid}/img/{filename}` | Frame cụ thể từ kept bucket (dùng cho popup Flow Monitor) |

Sensing tab live trên monitor render thumbnail từng row sample (lazy-loaded, click → lightbox). Flow Monitor turn card show 3-tile strip (motion frame + 2 worst pose) + nút "LOAD MORE · pose bucket" → `PoseBucketModal` fetch `bucket.json` và render full table.

### Surface vào `motion.activity`

Khi window fire, `motion.py` append 2 marker vào message:

```
[pose_bucket: <window_start_int>]
[pose_worst: <fn1>,<fn2>,<fn3>]
```

Cả 2 marker đều bị strip trước khi message tới LLM (mirror `[snapshot:]`); agent chỉ thấy `[posture_summary: ...]` ngôn ngữ tự nhiên. Text gốc (kèm marker) được giữ trong `sensing_input` JSONL → Flow Monitor đọc được bucket id mà không cần side channel.

### `/dm` tự đính ảnh (Telegram)

Khi agent quyết định nudge qua `/dm`, SSE handler của Lamp gọi `ConsumePoseBucketRun(runID)` (mirror `ConsumeGuardRun`). Nếu run có bucket stash, các `worst_snapshots` filenames được resolve thành path tuyệt đối dưới `/tmp/hal-sensing-snapshots/sensing_pose/buckets/<bid>/` rồi gửi Telegram qua `sendMediaGroup` — caption nằm trên photo đầu, còn lại hiển thị thành gallery. Agent không biết bất kỳ file path nào; việc đính ảnh hoàn toàn do Lamp quyết định dựa trên việc `motion.activity` gốc có mang bucket hay không.

### Workaround sign góc (tạm thời)

`signed_flexion_angle` ở perception-service hiện trả về dấu ngược với docstring ("Positive = forward flexion") — user chúi cổ rõ ràng lại ra **góc âm**, không phải dương. HAL negate `upper_arm_angle`, `neck_angle`, `trunk_angle` khi nhận từ perception-service (`POSE_FLIP_DLBACKEND_ANGLE_SIGN=True`, mặc định bật) để bảng monitor + JSONL khớp thực tế. `lower_arm_angle` unsigned nên bỏ qua. RULA score đã dùng `abs(angle)` nên risk/score không đổi dù theo convention nào. **Revert** bằng cách set flag `False` (hoặc xóa `_flip_signed_angles`) ngay khi perception-service ship fix upstream.

---

## Ánh sáng (`light.level`)

Thay đổi ánh sáng môi trường được forward khi vượt `LIGHT_CHANGE_THRESHOLD`. Không cần nói — agent điều chỉnh LED hoặc biểu đạt cảm xúc theo ngữ cảnh (ví dụ `/emotion sleepy` khi đèn tắt).

---

## Chế độ canh gác (Guard Mode)

Khi guard mode được bật (`guard_mode: true` trong config), Lamp trở thành **chó canh gác cảnh giác** — phản ứng mạnh mẽ với người lạ và broadcast alert lên Telegram.

### Luồng xử lý
1. Sự kiện `presence.enter` hoặc `motion` đến khi `guard_mode: true`.
2. Go handler gắn tag `[guard-active]` và đánh dấu runID là guard run (kèm snapshot path). Nếu `guard_instruction` có trong config, nó được thêm vào dưới dạng `[guard-instruction: ...]`.
3. Agent xử lý event — emotion **mạnh mẽ** (shock + curious), servo, TTS, cộng thêm custom instruction nếu có (vd: play nhạc, flash LED).

### Cảm xúc Guard Mode (dramatic)

Khi guard mode bật, stranger/motion events trigger cảm xúc **mạnh hơn nhiều** so với sensing thường:

| Guard event | HW markers | Voice |
|---|---|---|
| Stranger detected | `shock` (1.0) → `curious` (0.9) + servo shock | Hoảng sợ, giật mình, nghi ngờ |
| Motion (unknown) | `shock` (0.9) → `curious` (0.8) + servo scanning | Lo lắng, cảnh giác |
| Stranger left | `curious` (0.7) + scanning | Báo cáo đã rời đi, vẫn cảnh giác |
| Owner/friend về | `greeting` (0.9) + servo aim | Chào + kể lại chuyện gì xảy ra + hỏi tắt guard |

**Lời nói cũng phải đầy cảm xúc** — không phải báo cáo khô khan. Agent phải thể hiện sợ hãi, nghi ngờ, run rẩy thật sự.
4. Khi agent response trả về (SSE lifecycle end), Go SSE handler phát hiện guard run.
5. Text tự nhiên của agent + ảnh camera được gửi thẳng qua **Telegram Bot API** (`sendPhoto`) đến tất cả Telegram chat.
6. Delivery 100% đáng tin — bypass hoàn toàn OpenClaw agent.

### Custom guard instruction
Chủ nhà có thể đưa instruction tùy chỉnh khi bật guard mode (vd: "play tiếng rùn rợn khi có người lạ"). Instruction được lưu trong `guard_instruction` trong config và inject vào mỗi guard sensing event dưới dạng `[guard-instruction: ...]`. Agent sẽ thực hiện instruction này bằng các skill có sẵn (music, LED, v.v.).

### Tại sao approach này?
Sau khi thử 6 approaches khác nhau, hybrid này đáng tin nhất:
- **Agent viết message** → tự nhiên, có ngữ cảnh, có tính cách
- **Go side delivery** → Telegram Bot API trực tiếp, đảm bảo gửi, không rủi ro NO_REPLY
- **Agent thực hiện custom guard instruction** → chủ nhà có thể kết hợp guard mode với skill bất kỳ (music, LED, v.v.)

### Quá trình thử nghiệm (2026-04-07)
| # | Approach | Tại sao fail |
|---|----------|--------------|
| 1 | `BroadcastAlert` qua WS `chat.send` RPC | `chat.send` đi qua agent → 2/3 NO_REPLY |
| 2 | Agent-driven qua tag `[guard-active]` | Haiku bỏ qua SKILL instruction (chôn ở dòng 222) |
| 3 | Đưa instruction lên đầu SKILL.md | Haiku vẫn bỏ qua |
| 4 | Go-side template + `BroadcastAlert` | Agent nhận ra `sender: node-host` → ignore. Không có ảnh |
| 5 | Agent-driven + ép buộc trong SOUL.md | Tốt hơn nhưng không 100%. Lỗi token |
| 6 | **Hook agent response + Telegram Bot API** | ✅ Agent viết tự nhiên, Go gửi 100% |

> **Ghi chú:** `BroadcastAlert` (WS RPC) đã bị xóa. Tất cả broadcast giờ dùng `Broadcast()` gửi trực tiếp qua Telegram Bot API.

### Cảnh báo thủ công
Vẫn có thể gửi cảnh báo thủ công qua `POST /api/guard/alert` với message và ảnh tùy chọn. Caller từ mạng phải xác thực quản trị; caller nội bộ qua loopback được phép cho hoạt động nội bộ. Giờ dùng `Broadcast()` (Bot API trực tiếp) thay vì `BroadcastAlert` cũ.

Trường hợp sử dụng: Lamp hoạt động như trợ lý an ninh nhà. Khi chủ nhà rời đi và bật guard mode, mọi sự hiện diện hoặc chuyển động được báo cáo đến Telegram với message có cảm xúc và nhận biết ngữ cảnh.

---

## Theo dõi người lạ (Stranger Visit Tracking)

HAL (port 5001) theo dõi số lần mỗi stranger đã xuất hiện:

- Mỗi sự kiện `presence.enter` chứa stranger ID (ví dụ `stranger_5`), số lần xuất hiện được tăng lên.
- Stats bao gồm `count`, `first_seen`, và `last_seen` timestamps cho mỗi stranger.
- Lưu trữ tại thư mục data của HAL (giữ qua restart).
- Truy vấn stats qua `GET http://127.0.0.1:5001/face/stranger-stats`.

### Prompt enroll khi quen mặt (familiar-stranger)

Khi visit count của một stranger lần đầu chạm ngưỡng (`_FAMILIAR_VISIT_THRESHOLD = 2`, xem `hal/drivers/sensing/perceptions/processors/faceid/perception.py`), HAL:

1. Lưu raw frame hiện tại ra `<STRANGERS_DIR>/snapshots/<stranger_id>_<ts_ms>.jpg`.
2. Thêm hint vào message `presence.enter` đang gửi:
   `(familiar stranger <stranger_id> — seen 2 times, ask user if they want to remember this face; image saved at <path>)`

Skill `face-enroll` (phía Lamp) parse hint đó và nói trực tiếp với người trước cam: "I've seen you 2 times now — mind if I remember you? What's your name?" (skill chạy bằng tiếng Anh, agent tự dịch nếu cần). Khi có tên trả về thì gọi `POST /face/enroll` với image path đã lưu. Nếu từ chối, skill chỉ ghi nhận rồi dừng; threshold là one-shot (`count == 2`), nên cùng `stranger_id` đó sẽ KHÔNG bị lelamp prompt lại lần nữa. Count vượt qua 2 không re-fire — lúc đó stranger hoặc đã được enroll (không còn là stranger) hoặc đã chủ động từ chối.

---

## Chăm sóc sức khỏe (Wellbeing — Nhắc uống nước + Nghỉ ngơi, AI-Driven)

Lamp chủ động chăm sóc sức khỏe người dùng bằng cron jobs do AI agent tự quản lý qua OpenClaw. Thay vì timer cứng, agent tự quyết interval dựa trên khoa học và thói quen user.

### Cơ chế hoạt động (event-driven — không cron)

Wellbeing hoạt động **event-driven**. **KHÔNG còn cron wellbeing** nào. Mỗi khi nhận `motion.activity`, agent log hoạt động và đọc lại history gần đây để quyết định có cần nhắc hay không.

**Activity JSONL theo từng user** tại `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` — mỗi dòng là 1 transition:

```jsonc
{"ts": 1776658657.23, "seq": 42, "hour": 11, "action": "sedentary", "notes": ""}
```

`action` values:

| Action | Do ai ghi | Mục đích |
|---|---|---|
| `drink`, `break` | **HAL** (`motion.py` POST `/api/wellbeing/log` ngay trước khi fire `motion.activity`) | Reset point cho nudge timer tương ứng |
| `using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller` | **HAL** (`motion.py`, cùng đường đó) | Timeline + nudge phrasing. **KHÔNG** phải reset point. (`reading book` + `reading newspaper` đều gộp về label chung `reading`.) |
| `enter`, `leave` | **HAL** (`FaceRecognizer._post_wellbeing`, gọi từ `_check_impl` khi fresh detection và `_check_leaves` khi forget hết) | Session boundary — mỗi friend có timeline riêng; stranger gộp chung vào 1 timeline `"unknown"` duy nhất qua flag `_any_stranger_logged` (1 enter khi stranger đầu xuất hiện, 1 leave khi stranger cuối cùng forget). |
| `nudge_hydration`, `nudge_break` | Agent (sau khi nhắc) | Ghi lại thời điểm Lamp nhắc — hiện lên timeline. Chỉ agent biết khi nào nó thực sự nói, nên chỉ agent ghi entry này. |

**Dedup nằm ở 2 nơi.**

*Activity dedup (window 5 phút).* `hal/drivers/sensing/perceptions/motion.py` giữ `_last_sent_key = (current_user, frozenset(labels))` và `_last_sent_ts`, trong đó `labels` khớp với outbound message (bucket names cho drink/break/celebrate, raw Kinetics labels cho sedentary). Trước khi gửi `motion.activity` **và trước khi POST các row tới `/api/wellbeing/log`**, nếu key không đổi **và** khoảng cách từ lần gửi cuối chưa vượt `MOTION_DEDUP_WINDOW_S = 300` giây (5 phút) → drop cả chu kỳ. Nên `eating burger → eating cake` gộp thành cùng key `break` và bị drop, còn `writing → drawing` lật key (sedentary giữ raw) nên pass qua.

- Đổi user (owner→owner, owner→unknown, unknown→owner) lật key ngay → event pass qua.
- Stranger khác nhau (`stranger_46` → `stranger_54`) đều collapse về `"unknown"` qua `FaceRecognizer.current_user()` → đổi stranger không phá dedup.
- Sau 5 phút cùng state, event tiếp theo vẫn pass — để Lamp agent "thức dậy" định kỳ chạy threshold check.

*Presence dedup (safety net tại log).* `system/skillcontext/wellbeing/wellbeing.go::LogForUser` scan file JSONL của user từ dưới lên để tìm **presence row** gần nhất (enter/leave, bỏ qua activity rows xen giữa). `enter` khi presence cuối đã là `enter` → drop; `leave` khi chưa có session mở → drop. Vì HAL đã fire 1 enter / 1 session thật (per-friend + unknown gộp), layer này chỉ là safety net cho restart / out-of-order edge case, không load-bearing.

**Retention:** 30 ngày. Goroutine trong `wellbeing.Init()` xoá file cũ hàng ngày.

### Khi nhận `motion.activity` — agent làm gì

Tới thời điểm agent thấy event, HAL đã tự log mọi label activity rồi (xem bảng "Do ai ghi" phía trên). Agent chỉ còn đọc history, quyết định nhắc, và nếu nhắc thì log `nudge_*`.

1. **Đọc history gần đây** qua `GET /api/agent/wellbeing-history?user={current_user}&last=50`.
2. **Tính delta** từ log, dùng **điểm reset gần nhất** cho mỗi loại:

   ```
   hydration_reset = max(last drink entry, last enter entry, last nudge_hydration entry)
   break_reset     = max(last break entry, last enter entry, last nudge_break entry)
   ```

   Ba điểm reset: hoạt động thực tế (`drink`/`break`), mới vào session (`enter`), hoặc lần nhắc gần nhất (`nudge_*`). Nudge reset là điểm mấu chốt: sau khi Lamp nhắc, delta về 0 → lần nhắc tiếp theo chỉ fire sau 1 threshold window nữa — không cần cooldown constant riêng.
3. **Chọn path** (tối đa 1 phản hồi/turn, reaction ưu tiên hơn nudge — user vừa làm rồi, nudge tiếp sẽ thấy vô duyên):
   - **Reaction** — labels có `drink` hoặc `break` → nói một câu acknowledge ngắn (kiểu "quao uống nước thứ 3 hôm nay rồi đó", playful/ngạc nhiên, KHÔNG phải lời khuyên). Dùng `count_today` ("lần thứ N hôm nay"), `time_of_day`, và gap delta để biến hoá phrasing. **Không log entry** — row `drink` / `break` đã được HAL ghi sẵn upstream rồi.
   - **Hydration nudge** — else nếu hydration delta ≥ hydration threshold → nhắc uống nước.
   - **Break nudge** — else nếu break delta ≥ break threshold → nhắc nghỉ/stretch.
   - Else (sedentary chưa qua threshold, hoặc chưa có reset nào hôm nay) → `NO_REPLY`.
4. **Sau khi nhắc** (chỉ nudge, không phải reaction), log entry `nudge_hydration` hoặc `nudge_break` — đây là cái reset delta cho window tiếp theo (và hiện lên timeline user).
5. **KHÔNG BAO GIỜ đoán** time-since từ memory — luôn tính từ log.

Với lượt `[activity]` tự động, skill wellbeing cấm đọc phần chọn nhánh/skill trong mọi assistant text, kể cả trước tool. Phản ứng ăn uống (`eating *`, `dining`, `tasting food`) giới hạn một câu tối đa 20 từ theo `current_language`, không gọi tool, thêm log marker, nhắc việc khác hay bootstrap habit dù timer đã tới hạn hoặc `bootstrap_needed=true`. Ví dụ cà rốt có toàn bộ phản hồi là “Enjoy your carrots!”. Đây là hướng dẫn prompt, không phải bộ lọc TTS tất định.

Reaction path được thêm vào để hành động tích cực không bị im lặng: trước đây user uống nước mà chưa qua threshold thì Lamp `NO_REPLY`, cảm giác như đèn chết. Reaction được nuôi bởi 2 field thêm trong `[wellbeing_context: ...]` — `count_today` (đếm số lần `drink` / `break` hôm nay) và `time_of_day` (`morning` / `noon` / `afternoon` / `evening` / `night`) — để câu thoại có cái cụ thể bám vào mà không tốn thêm tool call. Visual caption (kiểu "chai Lavie xanh") cố ý CHƯA làm — vision pipeline hiện chỉ trả class label, không có free-text mô tả.

### Ngưỡng

Hardcode trong `lamp/resources/openclaw-skills/wellbeing/SKILL.md`:

| Threshold | Giá trị test | Giá trị production |
|---|---|---|
| `HYDRATION_THRESHOLD_MIN` | **5** | 45 |
| `BREAK_THRESHOLD_MIN` | **7** | 30 |

> ⚠ **Release checklist:** trước khi ship, đổi cả 2 ngưỡng về production (45 / 30). Hydration và break cố ý lệch nhau (5 vs 7) để test phân biệt nhánh nào fire.

**Cách chặn spam re-nudge.** Entry `nudge_hydration` / `nudge_break` mà agent log sau khi nhắc cũng tính là reset point cho threshold. Sau khi Lamp nhắc, delta về 0 → lần nhắc tiếp theo cùng loại chỉ fire sau một threshold window nữa (45 min cho hydration, 30 min cho break trong production).

```
10:45  hydration overdue → nhắc 💧 + log nudge_hydration → hydration delta = 0
10:50  wake-up → delta = 5 min < 45 → SKIP
11:20  wake-up → delta = 35 min < 45 → SKIP
11:30  wake-up → delta = 45 min ≥ 45 → nhắc 💧 lại (user vẫn chưa uống)
```

Nếu user uống hoặc nghỉ trước window tiếp theo, entry `drink`/`break` tự reset delta → không cần nhắc nữa.

### User attribution — `[context: current_user=X]`

Sensing handler inject tag `[context: current_user=X]` vào mọi message `presence.enter`, `motion.activity`, `emotion.detected` và `speech_emotion.detected`. `X` là **friend có session_start mới nhất** trong số các friend còn trong forget window (xem `FaceRecognizer.current_user()`), hoặc `"unknown"` khi face **chỉ** thấy stranger (không có friend nào còn present). Quan trọng: nếu có friend còn chưa bị "forget", `current_user()` trả về friend đó kể cả khi event `presence.enter` vừa rồi là của stranger — stranger-flicker không đá friend khỏi session.

Chọn theo `session_start` (thời điểm re-enter sau leave gần nhất) chứ không phải `last_seen`, để trường hợp 2 friend cùng present liên tục (Chloe 18:00, An 18:30) luôn chọn friend enter mới nhất (An) — deterministic, không phụ thuộc thứ tự dict.

**Nguồn duy nhất nằm ở HAL.** `sensing_service._send_event` đính kèm `face_recognizer.current_user()` vào mọi payload gửi đi dưới field `current_user`. Lamp sensing handler đọc thẳng `req.CurrentUser`, không parse lại từ message text nữa — khép lại lớp bug: `presence.enter` chỉ-có-stranger bắn khi friend vẫn còn present từng khiến `mood.CurrentUser()` của Lamp bị downgrade về `"unknown"`.

Caller ngoài (web UI, skill) có thể query cùng giá trị qua `GET http://127.0.0.1:5001/face/current-user` → `{"current_user": "<name>"}`. Đây là endpoint riêng; **không** parse ra từ `/face/cooldowns` (endpoint đó chỉ phục vụ debug view friend/stranger cooldown).

Các skill Wellbeing, Mood, Music đều bắt buộc dùng đúng giá trị này cho field `user` trong API call — **cấm** suy luận từ memory, KNOWLEDGE.md, chat history, hay `senderLabel`.

Cùng với `[context: current_user=X]`, handler còn inject thêm `[user_info: {"name","is_friend","telegram_id","telegram_username"}]` (build bởi `system/skillcontext/BuildUserContext`, fetch từ lelamp `/user/info`). Skill phải đọc `telegram_id` từ block này — **cấm** `curl /user/info`. Block bị bỏ khi fetch fail hoặc `current_user` là `unknown`; SKILL.md vẫn giữ fallback path.

### Marker presence do HAL tự ghi

`FaceRecognizer._post_wellbeing` của HAL ghi thẳng row `enter` / `leave` qua `POST /api/wellbeing/log` — agent không tham gia, sensing handler của Lamp cũng không ghi nữa.

- **Per-friend:** mỗi friend có timeline riêng. Fresh friend detection (sau gap > `FACE_OWNER_FORGET_S`) → `{"action": "enter", "user": "<name>"}`. Friend bị forget trong `_check_leaves` → `{"action": "leave", "user": "<name>"}`. Chloe enter khi Leo còn present chỉ ghi `chloe: enter` — không đụng timeline của Leo.
- **Stranger (gộp về `"unknown"`):** gate bởi flag `_any_stranger_logged`. Stranger đầu tiên → `unknown: enter`. Flag giữ true khi còn bất kỳ stranger nào trong forget window, nên stranger_37 → stranger_38 → stranger_52 không sinh thêm row. Khi `_check_leaves` drop stranger cuối → `unknown: leave`.

Kết quả: mọi enter có matching leave trên cùng timeline, attribution trong mỗi timeline chỉ phản ánh event thuộc về user đó.

**Hai flow, hai rule khác nhau.** Quan trọng là presence row và activity row attribute theo nguyên tắc khác:

- **Enter/leave row** = per-presence: mỗi friend có timeline riêng, stranger gộp chung 1 timeline `unknown`, **không** xét `current_user()` — friend mới enter không đá friend đang present ra, stranger xuất hiện cạnh friend cũng không downgrade friend.
- **Activity row** (drink/break/sedentary) = dùng `current_user()` với friend priority — Chloe + stranger cùng visible → activity vào mỗi Chloe timeline vì cô là effective user.

Ví dụ — Chloe và Stranger_X overlap:

| Time | Event | Chloe timeline | Unknown timeline |
|---|---|---|---|
| 18:00 | Chloe fresh detected | `chloe: enter` | — |
| 18:15 | Stranger_X fresh detected | — | `unknown: enter` |
| 18:20 | `motion.activity` (using computer) — `current_user()=chloe` | `chloe: using computer` | — |
| 18:45 | Stranger cuối forget | — | `unknown: leave` |
| 19:00 | `motion.activity` (writing) — `current_user()=chloe` | `chloe: writing` | — |
| 20:00 | Chloe forget | `chloe: leave` | — |

Timeline Chloe đầy đủ session + activities; timeline unknown ghi lại "có stranger ghé qua 18:15–18:45" nhưng không có activity row, vì Chloe là effective user suốt khoảng đó. 2 flow không conflict — chúng trả lời 2 câu hỏi khác nhau.

### Ưu tiên: Skills > Knowledge > History

AGENTS.md quy định thứ tự ưu tiên: **SKILL.md luôn override KNOWLEDGE.md và conversation history**. Điều này rất quan trọng vì agent tự tích lũy "kinh nghiệm" vào KNOWLEDGE.md qua heartbeat, và những ghi chú này có thể chứa rules sai xung đột với skills do developer duy trì. Nếu agent phát hiện xung đột, nó phải cập nhật KNOWLEDGE.md cho khớp với skill, không phải ngược lại.

### Khi `presence.leave` / `presence.away`

Backend ghi marker `leave` vào log. Không có gì khác để làm — **không có cron để cancel**. Directive yêu cầu agent im lặng (`NO_REPLY`).

Agent dùng ảnh camera để đánh giá — KHÔNG phải lúc nào cũng nói. Tránh spam user khi họ trông ổn.

### Gợi ý nhạc (AI-Driven)

Gợi ý nhạc **không còn** được kích hoạt bởi timer cứng. Thay vào đó, AI agent **tự schedule** music check qua OpenClaw cron jobs và **tự học** thói quen user theo thời gian:

- **Tự schedule:** Khi phát hiện **hoạt động tĩnh đầu tiên** trong `motion.activity` (không phải `presence.enter`), AI tạo cron job (mặc định: mỗi 20 phút / 1200000ms, `sessionTarget: "current"`, `payload.kind: "systemEvent"`). AI tự điều chỉnh interval dựa trên phản hồi của user.
- **Quyết định dựa trên dữ liệu:** Trước khi gợi ý, AI query:
  - `GET /audio/status` — nhạc đang phát chưa?
  - `GET /api/agent/mood-history` — mood mới nhất để chọn genre
  - `GET /audio/history?person={name}` — lịch sử nghe nhạc per-user (genre ưa thích, thời lượng, mức độ hài lòng)
- **Vòng lặp học:** AI so sánh thời điểm gợi ý với `music.play` events trong mood history. Gợi ý được chấp nhận → củng cố timing/genre; bị từ chối → điều chỉnh schedule.
- **Cá nhân hóa:** Theo thời gian, AI học được khi nào user thích nghe nhạc, thể loại nào, nghe bao lâu — và điều chỉnh gợi ý cho phù hợp.

**Dữ liệu AI sử dụng để học thói quen:**

| Câu hỏi | Nguồn dữ liệu |
|----------|----------------|
| User ngồi vào bàn mấy giờ? | `presence.enter` events → field `hour` |
| Ngồi bao lâu thì muốn nghe nhạc? | Khoảng cách giữa `presence.enter` và `music.play` |
| Nghe thể loại gì? | `audio/history` → fields `query`, `title` |
| Nghe bao lâu thì tắt? | `audio/history` → field `duration_s` |
| Thời điểm nào thích nghe nhạc nhất? | `music.play` events → field `hour` |

Xem skill Music (`resources/openclaw-skills/music/SKILL.md`) để biết chi tiết implementation.

### Chăm sóc chủ động (piggyback trên sensing events)

Ngoài nhắc nhở theo lịch, agent được khuyến khích **chú ý** khi nhận bất kỳ event nào có user visible (presence.enter, motion.activity). Dựa vào giờ, thời gian ngồi, và hình ảnh → agent có thể chủ động nhắc ăn, nghỉ, hoặc hỏi thăm. Một câu ngắn, chỉ khi tự nhiên. Không bắt buộc nhưng khuyến khích.

Ví dụ: "Ăn sáng chưa?" khi presence.enter sáng sớm, "Trưa rồi, ăn gì chưa?" khi motion.activity lúc 12:20, "Khuya rồi đó..." khi motion.activity khuya.

### Speak và broadcast markers

Hai control marker cho turn channel-origin:

| Marker | Tác dụng | Khi nào dùng |
|---|---|---|
| `[HW:/speak:{}]` | Force TTS trên loa. Không ảnh hưởng Telegram. | Proactive crons (wellbeing, music) chạy trong Telegram/channel session để nhắc phát qua loa. Thường kèm `[HW:/dm:{"telegram_id":"..."}]` để DM đúng 1 người. |
| `[HW:/broadcast:{}]` | Force TTS **và** fan-out text tới tất cả Telegram chat. | Chỉ dành cho guard mode alert. Không dùng cho wellbeing/music — sẽ notify mọi chat, không phải chỉ người được nhắc. |

Mặc định turn channel-origin (Telegram, webchat) suppress TTS loa vì reply đi qua channel message. `/speak` override suppression đó mà không kèm fan-out.

**Cron-fire tự force TTS.** Khi OpenClaw emit `event:"cron"` với `action:"started"`, Lamp cache `sessionKey` và `lifecycle_start` kế tiếp trên session đó trong vòng 10 s sẽ bị mark là cron fire — `isChannelRun` bị override thành `false` nên loa lamp tự nói mà không cần `[HW:/speak]` trong reply. Marker vẫn hữu ích như defense-in-depth fallback nếu cron event bị drop (`dropIfSlow: true` ở phía OpenClaw).

### Mood history per-user

Mood history lưu per-user tại `/root/local/users/{name}/mood/YYYY-MM-DD.jsonl` (30 ngày retention). Hệ thống tracking ai đang ngồi qua `presence.enter` (face recognition) và log mood events vào thư mục user đó.

#### Nguồn mood

| Source | Cách hoạt động |
|---|---|
| **Camera** (`source: "camera"`) | `motion.activity` detect action cảm xúc (laughing, crying, singing) → Emotion Detection skill trigger → agent log mood |
| **Conversation** (`source: "conversation"`) | Agent detect mood theo 2 cách: (1) **single message** — explicit ("I'm tired") hoặc implied ("work is killing me" → stressed); (2) **conversation flow** — sau khi nói chuyện một lúc, đọc vibe tổng thể (tone shift, reply ngắn cộc lốc, topic lặp lại, năng lượng tăng/giảm). Agent tin trực giác và mạnh dạn suy luận: chỉ cần một gợi ý nhỏ là đủ, log nhầm còn hơn bỏ sót. Hoạt động trên mọi channel (Telegram, voice, web). |

#### Voice mood nudge

Voice events (`voice_command`, `voice`) kèm nudge `[MANDATORY: Follow Mood skill — log mood now.]` trong message gửi lên agent, cộng `[Current user: {name}]` khi face recognition biết ai đang ngồi.

#### Định dạng lưu trữ

JSONL (mỗi dòng 1 JSON object) — chọn thay vì JSON array vì:
- **Append**: O(1) — ghi thêm 1 dòng (không cần đọc-parse-ghi lại cả file)
- **Crash-safe**: tệ nhất mất 1 dòng (array có thể corrupt cả file)
- **Đọc N cuối**: `Query()` đọc tất cả rồi slice — đủ nhanh cho file daily (vài chục entry)

Mỗi row có field `kind` — hoặc raw `signal` từ 1 nguồn (camera/voice/telegram),
hoặc `decision` do agent tổng hợp từ các signal gần đây + decision trước đó.
Server không bao giờ tự fuse — Mood skill chịu trách nhiệm ghi cả 2 row mỗi lần
phát hiện mood.

```bash
# Ghi raw signal (agent gọi mỗi lần camera/voice/telegram báo mood)
POST /api/mood/log  {"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}

# Ghi decision sau khi đọc history và tổng hợp
POST /api/mood/log  {"kind":"decision","mood":"happy","based_on":"3 signals last 20min","reasoning":"laughing reinforces previous happy decision"}

# Đọc tất cả kind cho 1 ngày (agent dùng để re-analyze)
GET /api/agent/mood-history?user=gray&date=2026-04-09&last=100

# Đọc decision mới nhất (Music/Wellbeing dùng để biết "current mood")
GET /api/agent/mood-history?user=gray&kind=decision&last=1
```

Signal row: `{"ts":...,"seq":1,"hour":10,"kind":"signal","mood":"happy","source":"camera","trigger":"laughing"}`
Decision row: `{"ts":...,"seq":2,"hour":10,"kind":"decision","mood":"happy","source":"agent","based_on":"...","reasoning":"..."}`

### Nhận diện cross-channel

Agent liên kết tên face recognition với Telegram username bằng cách quan sát timing và context (ví dụ: "gray" đang ngồi và "@GrayDev" nhắn Telegram cùng lúc). Mapping được lưu vào USER.md (cho friend) hoặc notes trong folder user. Agent hỏi xác nhận nếu chưa chắc.

---

## Phân tích Motion Activity (khi đang có mặt)

Khi user đang ở trạng thái PRESENT và camera phát hiện chuyển động, hệ thống gửi event `motion.activity` thay vì `motion`. Hệ thống gửi tên action đã detect (không kèm ảnh — tên action đủ để agent suy luận).

### Cách hoạt động

`MotionPerception` buffer snapshots và action names, flush theo interval (`MOTION_FLUSH_S`). Khi flush, check `PresenceService.state`:
- **PRESENT** → gửi 1 event `motion.activity` duy nhất. Format message:
  - `Activity detected: <labels>.` — HAL đã categorize: physical actions gộp thành bucket (`drink`, `break`, `celebrate`); sedentary activities giữ raw Kinetics label (`using computer`, `writing`, `texting`, `reading`, `drawing`, `playing controller`). Một ngoại lệ của "giữ raw label": `reading book` + `reading newspaper` đều gộp về một label chung `reading`. Agent log từng label nguyên văn — không map gì thêm ở phía agent.
  - Emotional X3D actions (`laughing`, `crying`, `singing`) **cố ý bị drop** ở đây. Một event type riêng `motion.emotional` sẽ được thêm sau; cho đến khi đó, detection emotional bị bỏ qua im lặng. `motion.activity` giữ thuần vật lý.
  - `yawning` **không** phải emotional — nó nằm trong bucket `tired` riêng và ĐƯỢC phát ra dưới dạng nhãn thô cho wellbeing skill. Agent nói ra cái ngáp như với drink hay bữa ăn (giới hạn mỗi giờ một lần bằng marker `noted_yawn`, và chỉ trước 21h), rút ngắn ngưỡng break xuống 20 phút, và coi đó là xác nhận cho sleep wind-down.
  - Không gửi ảnh — tiết kiệm tokens. **Không** yêu cầu nhận diện friend.
- **Còn lại** → event bị **skip** (log, không gửi). Lamp chỉ expect `motion.activity` — plain `motion` từ X3D/pose không có handler và lãng phí agent tokens.

Ví dụ message:
```
Activity detected: drink, using computer.
Activity detected: break.
Activity detected: writing, reading.
```

### Flow wellbeing nudge (event-driven)

Agent đọc dòng `Activity detected:`, split theo dấu phẩy, rồi POST từng label nguyên văn vào field `action` — HAL đã categorize sẵn nên agent không map gì.

1. **Log** từng label qua `POST /api/wellbeing/log` với `{action, notes:"", user}` — một entry/label. HAL đã dedup trên outbound label set nên agent không phải check.
2. **Đọc history** qua `GET /api/agent/wellbeing-history?user={name}&last=50`.
3. **Tính delta** so với reset point gần nhất cho mỗi loại (xem Wellbeing SKILL Step 3).
4. **Quyết định nudge** theo Wellbeing SKILL Step 4 — tối đa 1 hydration hoặc break nudge mỗi turn.
5. **Phản hồi**: 1 câu chăm sóc ngắn nếu có nudge/suggestion, không thì `NO_REPLY`.

### Hành vi Agent

| Event | Emotion | Voice |
|---|---|---|
| `motion.activity` | `curious` (0.4) | CÓ (nhận xét caring có context) hoặc NO_REPLY (ngồi yên) |

### Per-Face Motion Activity (MotionPerFacePerception)

Tùy chọn thay thế cho `MotionPerception` — chạy nhận diện hành động **riêng cho từng khuôn mặt** thay vì toàn bộ frame. Bật qua `HAL_MOTION_PER_FACE_ENABLED=true` (mặc định `false`).

- Mở rộng bbox khuôn mặt (1x lên, 2x trái/phải/xuống) để lấy phần thân trên + tay.
- Mỗi `face_id` có WS session riêng với backend action recognition.
- Person detection luôn **tắt** trên các session này.
- Dedup theo từng action riêng biệt cho mỗi face (mặc định 5 phút).
- **Cooldown floor toàn cục chung cho MỌI face** — cùng semantics và cùng knobs với `MotionPerception`: cùng coarse class thì tối đa 1 `motion.activity` mỗi `MOTION_EVENT_COOLDOWN_S` (mặc định 15 phút); **transition** đổi coarse class bypass floor khi đã qua `MOTION_TRANSITION_MIN_GAP_S` (mặc định 60s); floor được xóa khi user thực sự đổi (`reset_dedup`), không xóa khi stranger chớp tắt. Không có floor này, N người trong frame = N event mỗi flush.
- Session cần tối thiểu 4 frame trước khi gửi event đầu tiên.
- Session bị xóa sau 30 giây không thấy face đó.

| Config | Env var | Mặc định | Mô tả |
|---|---|---|---|
| `MOTION_PER_FACE_ENABLED` | `HAL_MOTION_PER_FACE_ENABLED` | `false` | Bật nhận diện hành động per-face |
| `MOTION_PER_FACE_DEDUP_WINDOW_S` | `HAL_MOTION_PER_FACE_DEDUP_WINDOW_S` | `300` | Cửa sổ dedup per-action (5 phút) |
| `MOTION_PER_FACE_SESSION_TTL_S` | `HAL_MOTION_PER_FACE_SESSION_TTL_S` | `30` | Xóa session sau bao lâu không thấy face |
| `MOTION_PER_FACE_MIN_FRAMES` | `HAL_MOTION_PER_FACE_MIN_FRAMES` | `4` | Số frame tối thiểu trước khi gửi event |

Định dạng message: `Activity detected (gray): using computer, writing.`

---

## Nhận diện cảm xúc người dùng — User Emotion (UC-M1) ✅

Lamp nhận diện trạng thái cảm xúc **của người dùng** qua ba kênh:

1. **Biểu cảm khuôn mặt** (chính) — event `emotion.detected` từ `hal/drivers/sensing/perceptions/processors/emotion.py`. Mỗi mặt phát hiện được sẽ crop và POST lên perception-service `/emotion-recognize` (HTTP, mỗi mặt 1 call mỗi sensing tick). Nhận diện 7 cảm xúc: Neutral, Happy, Sad, Surprise, Fear, Disgust, Anger. Một reading phải qua 3 cổng trước khi thành event:
   1. **Gate theo label (phía HAL)** — HAL gửi crop với `raw: true` và tự gate xác suất trả về trong `emotion_gating.py`: argmax phải vượt ngưỡng riêng của nó (`anger 0.8`, `sad 0.8`, `happy 0.5`, `surprise 0.6`, `disgust 0.7`, `fear 0.5`; chỉnh theo từng thiết bị bằng `HAL_EMOTION_LABEL_THRESHOLDS`, giá trị này thay thế toàn bộ map), nếu không sẽ bị thay bằng Neutral mang **xác suất của chính Neutral**, rồi áp `EMOTION_CONFIDENCE_THRESHOLD`. Reading không qua gate là **không có reading**, không phải Neutral. Với perception server cũ (bỏ qua `raw`), server tự gate bằng map `label_gating.py` và HAL dùng nguyên label đó. `result.json` ghi bên nào đã gate (`gate: hal|server`) và, khi HAL gate, xác suất của mọi class.
   2. **Occupancy (phía HAL)** — mỗi lần flush theo `EMOTION_FLUSH_S`, `Happy` fire chỉ với 1 frame, nhưng mọi label khác phải chiếm **đa số tuyệt đối số lần nhận diện của người đó trong 10s gần nhất**, tính cả những lần không trả về gì (hòa nhau không tính là đa số), **và phải có ít nhất 2 reading**. Số lần nhận diện đếm theo mỗi lần phát hiện mặt, không phải mỗi sensing tick, nên nếu không có sàn này thì một khuôn mặt chỉ xuất hiện 1 lần trong cửa sổ sẽ khiến 1 reading đơn lẻ trở thành "đa số" 1-của-1. Mặt quay về phía màn hình bị đọc thành Anger, nên trước đây 1 frame Anger đơn lẻ giữa cả chục response rỗng vẫn thắng vote.
   3. **Dedup theo bucket** — `(user, positive|negative)` với TTL `EMOTION_DEDUP_WINDOW_S` (300s), nên mỗi cực chỉ báo tối đa 1 lần mỗi 5 phút.
2. **Cảm xúc giọng nói** (phụ) — event `speech_emotion.detected` từ `hal/drivers/voice/speech_emotion/`. Chạy ở cuối mỗi phiên STT đã nhận diện được speaker, cùng WAV bytes đã dùng cho speaker recognition. Dùng `emotion2vec_plus_large` trên perception-service qua HTTP. Xem [Speech Emotion Recognition](../../../../docs/speech-emotion.md) cho pipeline đầy đủ.
3. **Body action** (cấp 3) — emotional X3D actions từ action recognition **cố ý bị loại** khỏi `motion.activity` (giờ thuần vật lý: sedentary/drink/break/celebrate). Một event type `motion.emotional` riêng đang được lên kế hoạch.

> **Đừng nhầm lẫn với Emotion Expression** (`emotion/SKILL.md`) — cái đó điều khiển cảm xúc đầu ra của Lamp (servo + LED + eyes). Emotion Detection là cảm nhận *user* đang cảm thấy gì; Emotion Expression là cách *Lamp* thể hiện cảm xúc của chính nó.

### Event `emotion.detected`

Được HAL fire khi perception-service emotion classifier nhận diện biểu cảm khuôn mặt vượt ngưỡng confidence. Format message:

```
Emotion detected: <Label>. (weak camera cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <hedge theo bucket>.)
```

Ví dụ thực tế:

```
Emotion detected: Fear. (weak camera cue; confidence=0.62; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Emotion detected: Happy. (weak camera cue; confidence=0.78; bucket=positive; treat as uncertain, do not over-celebrate.)
```

Prefix `Emotion detected: <Label>.` được giữ nguyên để parser của `user-emotion-detection/SKILL.md` và mood mapping Fear→stressed / Sad→sad vẫn chạy như cũ. Phần ngoặc đơn phía sau là để LLM không over-commit khi FER read nhiễu (bug từng gặp: Fear → "Oh hello there again"). Hedge theo bucket: `negative` → "do not assume the user is distressed"; `positive` → "do not over-celebrate"; `other` → "do not over-react".

**Dedup theo polarity bucket** (`EMOTION_BUCKETS` trong `hal/drivers/sensing/perceptions/processors/emotion.py`) gộp các label chi tiết thành `positive` / `negative` / `other` và dedup theo `(current_user, bucket)` trong window 5 phút. Nhiễu trong cùng bucket (Fear↔Sad↔Anger) gộp thành 1 event/window; flip giữa hai bucket (Fear→Happy) vẫn fire như mood change thật. Confidence trong message được average **chỉ trên các lần xuất hiện của dominant label** — confidence của label khác không pha loãng. Map dedup được persist vào sidecar boot-scoped (`/tmp/hal-emotion-state.json`) nên restart HAL service không re-fire emotion cuối cùng ở flush đầu tiên; reboot cả device thì bắt đầu sạch.

Sensing handler (`handler.go`) route `emotion.detected` events tới agent. Khi agent đang bận, events được queue và replay khi agent rảnh.

### Agent behavior

`user-emotion-detection/SKILL.md` xử lý `emotion.detected` events:

1. Map facial emotion → mood signal (vd: Happy → happy, Sad → sad, Angry → frustrated, Fear → stressed) và log `signal` row qua `POST /api/mood/log`
2. Chọn 1 response route từ bảng 3 row (first match wins):
   - **#1 `audio_playing == true`** → LED ack + `NO_REPLY` (đang có nhạc, không nói chồng lên)
   - **#2 `suggestion_worthy == true` AND decision fresh AND `last_suggestion_age_min ∉ [0, 7)`** → **music** — 1 câu nhẹ kèm genre, xem `music-suggestion/SKILL.md`
   - **#3 còn lại** → **checkin** — 1 phản ứng người ngắn. Xem `user-emotion-detection/reference/checkin.md` cho example theo raw FER label (Sad/Fear/Angry/Disgust/Happy/Surprise), mỗi label có 3 style: Ask/Comfort/Invite. Examples chỉ là gợi ý — agent tự improvise mỗi turn.
3. **Cooldown chỉ chặn music, không chặn checkin.** Khi cooldown 7 phút còn hiệu lực, row #2 fail vế thứ ba và event rơi xuống checkin (row #3). Agent vẫn hỏi "có chuyện gì" — chỉ không suggest nhạc 2 lần liên tiếp. `NO_REPLY` chỉ xảy ra ở row #1 (đang phát nhạc).
4. **Không bao giờ chào trên emotion event.** `emotion.detected` không phải presence/arrival event — `sensing/SKILL.md` cấm openers như `hello`, `welcome back`, mọi câu chứa `again`. Greeting chỉ dành cho `presence.enter`.

Output của skill emotion cấm lời dẫn trong assistant text trước hoặc giữa các lần gọi tool/skill; phản hồi gồm marker mood signal và marker của nhánh đã chọn, rồi một câu tối đa 20 từ hoặc `NO_REPLY` theo nhánh. Sau khi đọc reference đã chọn, model kết thúc mà không đọc lại để chỉnh câu. Cue camera/voice được đánh dấu yếu không được diễn đạt thành cảm xúc hay biểu cảm chắc chắn: checkin Happy yếu dùng lời mời trung tính, không khẳng định user vui hay đang cười. Logging và routing giữ nguyên. Đây là hướng dẫn prompt, không bảo đảm model luôn tuân thủ. Khi provider bật thinking, phần phân tích route/cooldown/giọng điệu/log phải nằm trong kênh reasoning gốc, không tóm tắt lại vào text trước/sau tool hay phản hồi cuối. Tag `<think>` trong text không thay thế kênh này; nếu không có kênh riêng thì bỏ phân tích. Reference checkin có ví dụ đầy đủ cho Sad yếu / user unknown / cooldown nhạc hai phút, giữ marker bắt buộc và chỉ nói “Anything on your mind?”, không mặc định user đang đau buồn.

Cả 2 route share chung 1 cooldown: music log qua `POST /api/music-suggestion/log` với `trigger:"<genre>:<mood>"` (mood bucket); checkin log cùng endpoint với `trigger:"checkin:<emotion>"` (raw FER label). `last_suggestion_age_min` phản ánh cả 2 kênh nên music suggestion mới sẽ im lặng nhánh music trong 7 phút, nhưng checkin vẫn fire. Checkin phrasing keyed theo raw emotion (không phải mood) — mỗi FER label có 3 style: Ask / Comfort / Invite. Xem `reference/checkin.md`. Output checkin luôn prefix `[HW:/emotion:{"emotion":"caring","intensity":0.5}]`.

### Mood pipeline

- **Mood history** (agent ghi): Mỗi signal, Mood skill ghi 1 row raw `signal`, rồi đọc history gần đây và ghi 1 row `decision` tổng hợp (vd: `{"kind":"decision","mood":"happy","based_on":"...","reasoning":"..."}`).
- Mood decisions trigger downstream skills: `music-suggestion` (nhạc chủ động), `wellbeing` (nhắc uống nước/nghỉ).

Xem `user-emotion-detection/SKILL.md` để biết rules phản hồi đầy đủ.

### Event `speech_emotion.detected`

Được HAL fire ở cuối mỗi phiên STT đã nhận diện được speaker, sau khi WAV bytes (cùng bytes đã dùng cho speaker `/embed`) được forward sang `perception-service /api/dl/ser/recognize` (emotion2vec_plus_large). Buffering, aggregation theo từng user, dedup theo polarity bucket, và POST sang Lamp đều nằm trong `hal/drivers/voice/speech_emotion/SpeechEmotionService` — `voice_service.py` chỉ gọi `submit(user, wav, duration)`. Format message giống pipeline khuôn mặt:

```
Speech emotion detected: <Label>. (weak voice cue; confidence=<0.00-1.00>; bucket=<positive|negative|other>; treat as uncertain, <hedge theo bucket>.)
```

Ví dụ thực tế:

```
Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; treat as uncertain, do not assume the user is distressed.)
Speech emotion detected: Happy. (weak voice cue; confidence=0.84; bucket=positive; treat as uncertain, do not over-celebrate.)
```

Labels (từ emotion2vec_plus_large): `angry`, `disgusted`, `fearful`, `happy`, `neutral`, `other`, `sad`, `surprised`, `<unk>`. Neutral / other / `<unk>` bị drop trước khi bucket — cùng quy tắc với face Neutral.

**Anti-spam guards** (mirror 1-1 với pipeline khuôn mặt):

1. Audio quá ngắn (`duration_s < SPEECH_EMOTION_MIN_AUDIO_S`) drop ở `submit()`.
2. Unknown speaker (`match=false` hoặc `name=="unknown"`) drop — không có subject để gán cảm xúc.
3. Confidence thấp (`< CONFIDENCE_THRESHOLD_BY_LABEL[label]`, fallback `DEFAULT_CONFIDENCE_THRESHOLD` — đều ở `speech_emotion/constants.py`) bị worker drop.
4. Neutral labels drop ở flush.
5. TTL dedup `(user, bucket)` trong `SPEECH_EMOTION_DEDUP_WINDOW_S` (mặc định 5 phút). Mỗi bucket có timer riêng — gửi event positive KHÔNG reset window của negative. Map được persist vào sidecar boot-scoped (`/tmp/hal-ser-state.json`) nên restart HAL service không re-fire speech emotion cuối cùng; reboot cả device thì bắt đầu sạch.

Payload event gửi kèm `current_user` rõ ràng nên sensing handler Lamp không phải tra cứu lại.

### Agent behavior (chung với face emotion)

`speech_emotion.detected` route qua **cùng skill** với `emotion.detected` — `user-emotion-detection/SKILL.md`. Sensing handler gắn prefix `[speech_emotion]` (vs `[emotion]` cho face), pre-fetch cùng block `[emotion_context: ...]`, rồi forward tới agent. Skill:

1. Parse prefix để chọn `source` trên mood signal log (`"voice"` cho `[speech_emotion]`, `"camera"` cho `[emotion]`).
2. Map label qua bảng chung: `Happy → happy`, `Sad → sad`, `Angry → frustrated`, `Fear`/`Fearful → stressed`, `Surprise`/`Surprised → excited`, `Disgust`/`Disgusted → frustrated`, `Neutral → normal`. Biến thể voice (`Fearful`, `Surprised`, `Disgusted`) bucket giống hệt counterpart face.
3. Chạy cùng router `music / checkin / action / silent` trên cùng các field của `[emotion_context: ...]` (`audio_playing`, `suggestion_worthy`, `last_suggestion_age_min`, `is_decision_stale`).
4. Share 1 cooldown music-suggestion duy nhất giữa 2 modality — voice không thể bypass cooldown mà camera vừa set, và ngược lại.

Hedge `(weak voice cue; ...)` trong message là tín hiệu cho model nghiêng về Comfort/Invite ở nhánh checkin thay vì Ask khi voice-only negative read, vì emotion2vec trên utterance ngắn nhiễu hơn face FER. Xem `user-emotion-detection/SKILL.md` cho rules đầy đủ.

Xem [Speech Emotion Recognition](../../../../docs/speech-emotion.md) cho kiến trúc đầy đủ, threading model, configuration, và failure mode.

---

## Lưu trữ Snapshot (hai tầng)

Các sensing event có kèm camera frame (`motion`, `presence.enter`, `presence.leave`, `motion.activity`, `emotion.detected`, `music.mood`) lưu snapshot ở hai nơi.

| Tầng | Đường dẫn | Rotation | Giữ qua reboot |
|------|-----------|----------|-----------------|
| **Tmp buffer** | `/tmp/hal-sensing-snapshots/sensing_<prefix>/` | Theo số lượng (tối đa 50 file) | Không |
| **Persistent** | `/var/lib/hal/snapshots/sensing_<prefix>/` | TTL (72h) + dung lượng (tối đa 50 MB) | Có |

Mỗi loại event ghi vào subdir riêng (`sensing_<prefix>`, ví dụ `sensing_presence/`, `sensing_motion_activity/`, `sensing_emotion/`). Tên file là `<ms>.jpg`. Snapshot được lưu vào tmp trước, rồi copy sang persistent dir. Đường dẫn persistent được ghi trong event message (`[snapshot: /var/lib/hal/snapshots/sensing_<prefix>/<ms>.jpg]`) để agent có thể xem lại — kể cả sau khi thiết bị reboot. Monitor phục vụ ảnh qua `GET /api/sensing/snapshot/<category>/<name>`.

Các hằng số cấu hình nằm trong `hal/config.py`:
- `SNAPSHOT_TMP_MAX_COUNT` — số file tối đa trong tmp (mặc định 50)
- `SNAPSHOT_PERSIST_TTL_S` — TTL file persistent tính bằng giây (mặc định 72h)
- `SNAPSHOT_PERSIST_MAX_BYTES` — dung lượng tối đa thư mục persistent (mặc định 50 MB)

---

## Quy tắc chung (tất cả event type)

- **Passive sensing events** (`[sensing:*]`) bị drop nếu agent đang bận xử lý turn khác.
- **Replay chờ loa, không chỉ chờ agent**: runtime báo idle khi text trả lời được đẩy vào TTS queue, chứ không phải khi loa đọc xong — câu trả lời dài còn phát thêm hàng chục giây. Drain ngay lúc đó sinh ra turn mới giữa chừng, và vì HAL trao loa cho turn mới nhất (`turn_seq`), câu trả lời user thật sự hỏi bị cắt ngang (quan sát trên máy 28/8: câu trả lời hunting-season bị lời chào từ `presence.enter` đang xếp hàng cắt mất). `drainPendingEvents` giờ hỏi `lib/speakergate` trước: khi `GET /voice/status` còn báo `tts_speaking`, cả batch được trả lại hàng đợi và drain lại trong vòng 500 ms sau khi loa im. Hai loại event không bao giờ chờ — lời nói và chat (`voice`, `voice_command`, `voice_followup`, `voice_agent_handled`, `web_chat`, `mqtt_chat`), vì người nói đè lên máy chính là barge-in mà luật cướp loa sinh ra để phục vụ; và `fire_hazard.detected`, vì cảnh báo an toàn không được xếp hàng sau một câu trả lời. Một event được miễn sẽ giải phóng cả batch của nó, để hàng đợi không bị đảo thứ tự. Cờ `speaking` bị kẹt chỉ làm chậm replay chứ không huỷ: sau 90 s batch vẫn replay, và HAL không trả lời được coi là loa rảnh.
- **Luật đó áp cả khi không có turn nào đang chạy**: runtime báo idle ngay khi text trả lời được xếp vào TTS, nên event có thể tới lúc agent rảnh mà loa vẫn đang đọc — và cửa sổ đó dài đúng bằng câu trả lời, tức đúng lúc dễ có người đứng trước mặt máy nhất. Vì vậy `PostEvent` coi "loa đang đọc" là bận với cùng nhóm event passive (`handler.go`), xếp event vào hàng đợi, rồi nhờ `speakergate` replay khi đọc xong. Thiếu vế cuối thì nó nằm lại vĩnh viễn: drain vốn bám vào lúc một turn kết thúc, mà ở đây không có turn nào chạy. `DrainPendingEvents()` nằm trên `domain.AgentGateway` chính vì lý do này.
- **Voice events** luôn pass through — người dùng đang chủ động nói chuyện. Voice messages kèm mood scan nudge (`[MANDATORY: Follow Mood skill — log mood now.]`) để agent nhớ detect mood từ conversation flow. Pass event không có nghĩa bắt buộc phải trả lời: `SOUL.md` trả `NO_REPLY` cho filler, một từ đơn/lặp lại, mảnh câu bị cắt, transcript nhận dạng sai, hoặc input giọng nói không có nghĩa rõ ràng. Với turn đó không được hỏi làm rõ, phản ứng, hoặc log mood.
- Prefix `[sensing:type]` trong message là cách agent biết đây là ambient event, không phải message từ người dùng.
- **Pre-turn `thinking` emotion**: Hook `emotion-acknowledge` tự fire `POST /emotion {thinking, 0.7}` server-side ở event `message:preprocessed` cho mọi non-sensing message — agent không cần gọi. Hook skip sensing events vì mỗi type đã có emotion đầu tiên riêng.
- **Image pruning echo**: OpenClaw strip image payload cũ khỏi conversation history để tiết kiệm token. Model nhỏ (Haiku) có thể echo marker dưới dạng `[image description removed]` trong response. `SOUL.md` hướng dẫn agent không được echo các marker này.

---

## Hành vi Ambient khi idle

Khi không có gì diễn ra, `system/ambient/` (Go, thuộc os-server) làm thiết bị có cảm giác "đang sống": LED thở, thỉnh thoảng cựa mình, và tự lẩm bẩm khe khẽ. Mọi điều khiển phần cứng đi qua HAL HTTP API (:5001). Cơ chế này tách biệt với sensing event — không tạo agent turn, không tốn token.

### Vòng đời pause / resume

- Service khởi động ở trạng thái **paused**, resume sau 5 giây kể từ khi os-server boot.
- Bất kỳ tương tác thật nào cũng pause ngay lập tức. Trigger pause (event trên monitor bus): `sensing_input`, `chat_response`, `intent_match`, `tts`, `chat_send`, `hw_emotion`.
- Tự resume sau **60 giây** không có tương tác (`resumeDelay`, kiểm tra mỗi 2 giây).
- **Sleep mode**: khi event `hw_emotion` mang emotion `sleepy`, toàn bộ hành vi ambient bị suppress cho tới khi có tương tác thật tiếp theo (chat, wake word, sensing) đánh thức thiết bị.
- Chuyển trạng thái pause/resume được log vào Flow Monitor dưới dạng `ambient_pause` / `ambient_resume`.

**Hai đồng hồ, hai tầng** — 60 giây resume là *điều kiện đủ tư cách* ("ambient được phép hoạt động lại"), còn mỗi loop bên dưới có đồng hồ *nhịp riêng* chạy độc lập, không bị reset khi có tương tác; nếu đồng hồ của loop điểm giờ đúng lúc ambient đang paused thì lượt đó bị bỏ qua, không xếp hàng chờ. Ví dụ timeline: user ngừng nói chuyện → 60 giây sau ambient resume, LED bắt đầu thở gần như ngay (tick 2 giây) — còn câu tự lẩm bẩm kế tiếp rơi vào lúc đồng hồ 5–15 phút của mumble loop điểm giờ, có thể ngay sau đó hoặc nhiều phút sau.

### Các behavior loop (gate theo capability)

Toàn bộ suite là opt-in theo từng device: capability routeless `lifelike` (khai trong block `capabilities:` của `robots/<type>/ROBOT.md`, xem `robots/contract/capabilities.md`) là công tắc tổng — device có khai capabilities nhưng không khai `lifelike` thì không chạy hành vi idle nào cả (ví dụ intern-v2 là công cụ im lặng). Mỗi loop còn điều khiển một peripheral và chỉ chạy khi device khai capability phần cứng tương ứng. Device không khai capability nào hết thì chạy tất cả (fail-open, giữ hành vi Lamp cũ).

| Loop | Capability | Nhịp | Hoạt động |
|------|-----------|------|-----------|
| Breathing LED | `light` | liên tục (tick 2 giây) | Bật effect `breathing` có sẵn của HAL qua `/led/effect` (speed 0.3) với màu LED hiện tại đọc từ HAL; fallback về resting look (`ambientRestingColor`, hiện là `(0, 0, 0)`) khi LED đang tắt (đen) — resting look tối nghĩa là skip tick và strip ở yên không sáng. Xem [led-control_vi.md § Resting look](led-control_vi.md#resting-look-mặc-định-tắt). Dừng effect khi paused hoặc LED đang bị lock. |
| Micro-movements | `motion` | ngẫu nhiên 45–120 giây | Phát một servo recording an toàn trong bộ `idle`, `curious`, `nod`. Chỉ servo — không đụng vào LED. |
| Mumble (tự lẩm bẩm) | `audio` | ngẫu nhiên 5–15 phút | Chọn trong sáu mục `PhraseMumble` dùng chung mỗi ngôn ngữ (`system/lib/i18n/phrases.go`, EN/VI/zh-CN/zh-TW): xen âm ngắn với lời tự nói có chút tinh nghịch, như “Cứ thong thả thôi.” và “Mình thích những lúc thế này.” Một mục dùng `[chuckle]`; không có tag thở dài hay thì thầm. Dùng `hal.SpeakCached` — lần render đầu của mỗi câu mới gọi TTS provider, các lần sau phát lại từ WAV cache của HAL, nên lẩm bẩm lúc idle không tốn API. |

Câu từ dùng chung, không phụ thuộc loại robot hay cơ thể. Pool idle không có
ngữ cảnh sensor hay hội thoại, nên không nhận xét ánh sáng,
độ yên tĩnh, việc đang nghe, quên suy nghĩ hay buồn ngủ. Những câu đó cần hành vi
riêng có kiểm tra ngữ cảnh; loop này không suy ra các điều kiện đó. Nhịp phát,
quy tắc pause/wake và cách chọn ngẫu nhiên giữ nguyên (vẫn có thể lặp liên tiếp).
Cần nghe giọng thực tế được cache trên device để đánh giá độ tự nhiên; chữ và
dấu câu không bảo đảm được ngữ điệu.

### LED lock

Khi user hoặc agent chủ động set màu/scene cho LED, breathing không được đè lên:

- Event `led_set` trên monitor bus lock LED; `led_off` unlock (breathing được phép chạy lại khi idle).
- LED set từ Web UI đi qua proxy `/api/hardware` và không phát các event trên, nên proxy gọi thẳng `LockLED()` / `UnlockLED()` trên ambient service.

### Đừng nhầm với dead-air filler

Mumble loop nói khi thiết bị **idle**. Một cơ chế khác — dead-air filler (`system/lib/i18n/fillers.go`, điều khiển bởi `server/sensing/delivery/http/deadair_filler.go`) — nói các aside ngắn, tự nhiên như "Ừm, để coi." **giữa turn** khi agent đang bận. Pool theo tool vẫn tách riêng cho các tool đã nhận diện. Tên tool thô được chuẩn hoá trước (`bash` / `shell` → `exec`, `file_changes` → `apply_patch`, cùng hậu tố MCP nhận diện được), nên cùng một pool dùng được qua các runtime; tên lạ rơi về continuation chung. Khác trigger, khác pool câu.
