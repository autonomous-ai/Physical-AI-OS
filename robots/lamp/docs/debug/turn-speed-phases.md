# Reactive turn speed-up — 2 phases (2026-05-07)

Cùng mục tiêu: giảm latency tool turn cho `motion.activity` (wellbeing) và `emotion.detected` (mood + music-suggestion + user-emotion-detection). Cả hai pipeline đều có pattern "read tuần tự → think → write tuần tự" gây ~30-85s/turn.

## Insight gốc

**Bottleneck là LLM think, không phải tool call.** Mỗi tool turn gánh ~5-10s think pass. Tool call thật sự chỉ ~100-300ms. Giảm số tool turn = giảm số think pass.

## Phase 1 — Parallel batch trong cùng 1 tool turn

Kill "Step 1, 2, 3, 4" sequential framing trong SKILL.md. Replace bằng group "What to read / Decision rules / What to write". Backend inject mới cho `emotion.detected`: `[REQUIRED — run both skills, fire concurrent reads/writes in one bash with & wait]`.

**Đụng:**
- Backend `handler.go` + `service_events.go`: rewrite emotion.detected inject
- `skills/user-emotion-detection/SKILL.md`, `skills/mood/SKILL.md`, `skills/music-suggestion/SKILL.md`: drop step numbering
- `skills/wellbeing/SKILL.md`: gộp 3 reads (history + patterns stat + days count) vào 1 bash, bỏ load `skills/habit/SKILL.md`
- `skills/sensing/SKILL.md`: cập nhật event matrix
- Cleanup `<say>...</say>` wrapper khỏi 2 skill (consistency với 19 skill khác)

**Verified trên Pi:**

| Turn | Trước | Sau Phase 1 |
|---|---|---|
| `emotion.detected` (Sad) | ~36s | ~26s |
| `motion.activity` (using computer) | ~50s | ~23s |

Read batch chạy ~700-950ms cho 3-5 reads concurrent (với `& wait`). Write batch chạy ~500ms cho 2-3 POSTs concurrent.

**Save thực:** chủ yếu từ kill think pass khi gộp tool turn (3 turns → 2 turns = save ~9s think). `&` parallel curl chỉ save ~1s thuần network — phụ.

**Commit Phase 1:**
- `b00ee869` parallel-batch (3 emotion skill + backend inject)
- `45db216c` snapshot path fix (lelamp đổi `/tmp/lamp-snapshots` → `/root/.openclaw/media/lamp-snapshots`)
- `249e63ee` patterns.json fold vào read batch
- `f3b4b046` wellbeing batch + drop habit cache miss
- `2fc1c515` drop `<say>` wrapper
- `170db493` drop Output Format header

## Phase 2 — Pre-inject data từ Lamp backend

**Insight tiếp:** mỗi turn vẫn còn ~9s think "between tools" (giữa read batch và write batch) để: đọc 5 read response → synthesize mood → decide skip/genre → plan POST commands. Không loại bỏ được decision logic, nhưng có thể loại bỏ **plan-reads pass** nếu Lamp đọc data sẵn rồi đính vào prompt.

**Cách:**
- Backend handler.go khi nhận `motion.activity` / `emotion.detected`: pre-fetch toàn bộ data skill cần (wellbeing-history, mood-history, audio/status, music-suggestion-history, audio/history, patterns.json, days count).
- Encode JSON, inject vào message dạng `[wellbeing_context: history=[...], patterns=..., days=N]` hoặc `[emotion_context: ...]`.
- SKILL.md đổi "What to read" thành "Use pre-fetched context. DO NOT re-GET unless block missing".
- Fallback giữ — nếu context block thiếu (pre-fetch fail), agent vẫn GET như Phase 1 → graceful degradation.

**Estimated speedup:**

| Turn | Phase 1 | Phase 2 |
|---|---|---|
| `emotion.detected` | ~26s | ~12-13s (~50%) |
| `motion.activity` | ~23s | ~12s (~48%) |

**Trade-off:**
- Tight coupling Lamp backend ↔ skill data needs (backend phải biết shape data của skill).
- Token cost +2-3KB/event in prompt (cache stable, prompt cache absorb được).
- Race condition lý thuyết (pre-fetch lúc T+0, agent đọc lúc T+8s → data có thể stale 1-5s) — chấp nhận được cho wellbeing/mood (slow-changing).
- Iteration: skill đổi data needs → phải update Lamp binary + redeploy.

**Mitigation:**
- Fallback GET trong SKILL.md nếu context block thiếu/hỏng.
- Document tight coupling trong handler.go comment, link tới SKILL.md.

**Order:**
1. `motion.activity` trước (1 skill, đơn giản, test được nhanh).
2. Đo 1-2 ngày production.
3. Mở rộng `emotion.detected` (3-skill chain).

**Phase 2 status (motion.activity):** verified live trên .85 — turn ~14s (vs ~23s Phase 1).

## Phase 3 — HW marker cho write side-effects

**Insight tiếp:** sau Phase 2, vẫn còn ~6-8s/turn cho think pass sau khi POST `wellbeing/log` xong. Tool call protocol bắt buộc agent consume `tool_result` rồi mới generate reply tiếp — cho dù side-effect (log write) không có gì để wait.

**HW marker pattern** (đã có sẵn cho `/emotion`, `/audio/play`, `/dm`...): agent nhúng `[HW:/path:{json}]` vào TEXT reply. Lamp parse marker từ text, fire HTTP POST trong goroutine background, strip marker khỏi TTS. Agent không "thấy" như tool call → không có tool round-trip → không có post-write think pass.

**Đụng:**
- `system/server/agent/delivery/http/handler_hw.go`: route Lamp-bound markers (path bắt đầu `/wellbeing/`) tới `http://127.0.0.1:5000/api/...` thay vì lelamp `5001`. Thêm flow log `hw_wellbeing`.
- `skills/wellbeing/SKILL.md`: "What to write" rewrite — instruct `[HW:/wellbeing/log:{action,notes,user}]` thay curl exec. Giữ curl làm fallback nếu HW marker bị reject.

**Gotcha — regex limit:** `hwMarkerRe` = `\[HW:(/[^:]+):(\{[^}]*\})\]` cấm `}` trong body. Wellbeing log body flat (`{action,notes,user}`) → OK. Nếu `notes` chứa `}` regex break — agent cần escape hoặc fallback curl.

**Estimated speedup:** ~14s (Phase 2) → ~5-7s (Phase 3). Tổng từ Phase 0 (~50s) ≈ **giảm 85-90%**.

**Mở rộng emotion.detected (Part 2):**
- 3 HW marker mới cần thêm:
  - `[HW:/mood/log:{kind,mood,source,trigger,user}]` (signal + decision dùng chung body)
  - `[HW:/music-suggestion/log:{user,trigger,message}]`
- Cùng pattern routing trong `handler_hw.go` (path prefix `/mood/`, `/music-suggestion/` → port 5000).
- Đợi motion.activity chạy production 1-2 ngày, ổn rồi áp tương tự cho emotion.detected.

**Phase 3 status (motion.activity):** verified live trên .38 — turn ~10s. (vs Phase 2 ~14s, Phase 1 ~23s, Phase 0 ~50s.)

## Phase 2 + Phase 3 — emotion.detected pipeline (mood + music-suggestion)

Pattern y nguyên wellbeing, áp dụng cho 3-skill chain (`user-emotion-detection` + `mood` + `music-suggestion`). Chia 4 commit độc lập:

1. **commit 1/4** — Phase 2 pre-inject. Backend `BuildEmotionContext(detectedEmotion, user)` trong `system/skillcontext/emotion.go` → digest `[emotion_context: {mapped_mood, recent_signals, prior_decision, is_decision_stale, audio_playing, last_suggestion_age_min, audio_recent, music_pattern_for_hour, suggestion_worthy}]`. 3 SKILL.md đổi "What to read" → use context block với fallback bash batch. Decision rules vẫn ở agent (5 rules synthesis, threshold cooldown, genre pick, phrasing).
2. **commit 2/4** — Phase 3a HW route. `handler_hw.go` extend prefix list từ chỉ `/wellbeing/` → `/wellbeing/`, `/mood/`, `/music-suggestion/`. Thêm flow event types `hw_mood`, `hw_music_suggestion`. Backend-only no-op.
3. **commit 3/4** — Phase 3b SKILL.md HW marker. `skills/mood/SKILL.md` (signal + decision đều dùng `[HW:/mood/log:{...}]`, kind trong body), `skills/music-suggestion/SKILL.md` (`[HW:/music-suggestion/log:{...}]`), `skills/user-emotion-detection/SKILL.md` (signal cũng dùng HW marker với `mapped_mood` từ context). Curl POST giữ làm fallback nếu marker regex bị reject (notes/reasoning chứa `}`).
4. **commit 4/4** — UI flow events. `types.ts` + `helpers.ts` + `FlowDiagram.tsx` add `hw_mood`, `hw_music_suggestion` (stack với hw_wellbeing ở Lamp-side column).

**Estimated speedup emotion turn:**

| Phase | Time | Reduction vs Phase 0 |
|---|---|---|
| Phase 0 (no refactor) | ~36s | — |
| Phase 1 (parallel batch) | ~26s | -28% |
| Phase 2 (pre-inject) | ~12-13s | -64% |
| Phase 3 (HW markers) | **~5-7s** | **-83%** |

**Order deploy:**
1. Deploy commit 1 → 1 emotion turn → confirm `[emotion_context:]` xuất hiện trong chat_input → kỳ vọng turn ~12-13s.
2. Deploy commit 2 (no behavior change yet, chỉ chuẩn bị route).
3. Deploy commit 3 → 1 emotion turn → confirm `hw_mood` + `hw_music_suggestion` flow events thay cho `tool_call exec POST` → kỳ vọng turn ~5-7s.
4. Deploy commit 4 → UI hiện node mood/music-suggestion.

**Phase 2+3 emotion status:** all 4 commits ready in local — chờ deploy + verify trên Pi.

## Đo TTFT (time-to-first-token)

Để biết bottleneck nằm ở đâu — OpenClaw init, LLM warmup, hay tool execution — Flow Monitor có 3 mốc thời gian rõ ràng:

| Mốc → Mốc | Ý nghĩa | Color UI |
|---|---|---|
| `chat_send` → `lifecycle_start` | OpenClaw nhận RPC + init turn (network + load session/context + boot agent). **KHÔNG** phải LLM. | `openclaw init` (xanh dương) |
| `lifecycle_start` → first `thinking`/`assistant` delta | LLM warmup thực — model reasoning silently trước khi token đầu chảy ra. Đo trực tiếp từ stream events (`type === "thinking"` / `"assistant_delta"`). | `llm warmup` (xanh dương) |
| first delta → `first tool_call` | LLM streaming + planning trước tool call đầu. | `llm streaming` (tím) |

(Lưu ý: trước đây có `llm_first_token` flow event tự chế trong Lamp handler để mark warmup, đã bỏ — Lamp giờ đo trực tiếp từ stream events trong UI helpers. Pipeline aggregator + timing strip cùng nguồn truth.)

Khi optimize: nếu `openclaw init` lớn → check WS RTT + session context size; nếu `llm ttft` lớn → check prompt size / cache hit rate (auto-compact); nếu `llm streaming` lớn → giảm thinking budget hoặc số tool turn (xem Phase 1/2 ở trên).
