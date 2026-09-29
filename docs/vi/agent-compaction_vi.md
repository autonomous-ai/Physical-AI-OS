# Agent session compaction — cách hoạt động và vì sao có thể đè SKILL.md

> **Tóm tắt:** Compaction do chính agentic runtime thực hiện (OpenClaw mode `safeguard`, gần giới hạn context của model). Trigger auto-compact của OS server **hiện đang tắt** — thay vào đó OS server xoay sang session mới (`/new`) khi vượt ngưỡng rotation của backend (150k reported tokens trên OpenClaw). Khi compaction xảy ra, kết quả compact là một chuỗi `summary` — chuỗi này được **chèn đầu mỗi turn kế tiếp** cho đến lần compact sau. Nếu rule vô tình bị copy/generalize vào summary, chúng có thể đè `SKILL.md` đang load — vì summary nằm trước trong prompt và được coi như "context đã chốt."
>
> Doc này là reference link từ nút **📋 Summary** ở Flow Monitor (modal: `system/web/src/pages/monitor/FlowSection/CompactionModal.tsx`).

## Vì sao có compact

Agentic runtime giữ conversation history dài. Mỗi turn là tập hợp các entry `user event`, `thinking`, `tool_call`, `tool_result`, `assistant reply` — tất cả được ghi trong session `.jsonl`. Sau vài giờ hoạt động, tokens tăng nhanh. Khi context gần chạm giới hạn model, runtime compact: gộp entry cũ thành 1 đoạn summary, xóa entry gốc, tiếp tục.

Hiện nay OS server cố giữ session thấp hơn nhiều so với mốc đó: sau mỗi turn nó gọi `maybeAutoNewSession`, hỏi `ShouldRotateSession(totalTokens, turns)` của backend và khi fire thì gửi `NewSession` (`/new` trên OpenClaw) — tức thì, không summary, external memory của device (mood, habit, owner) vẫn giữ. OpenClaw rotate khi vượt `sessionRotateTokenThreshold = 150_000` reported tokens (≈185k context thực). Compaction riêng của OpenClaw được `runtimes/openclaw/onboarding.go` cấu hình `mode: "safeguard"`, `reserveTokensFloor: 5000` (chốt chặn cuối ở ~195k với model 200k), nên compaction trên device giờ hiếm.

## Record compaction

Compaction lưu dưới dạng 1 dòng JSONL trong session file đang active:

```
/root/.openclaw/agents/main/sessions/<sessionId>.jsonl
```

Cấu trúc record (rút gọn):

```json
{
  "type": "compaction",
  "id": 17170331,
  "parentId": "369818c9",
  "timestamp": "2026-04-24T03:21:30.305Z",
  "summary": "<toàn văn summary, ≤ ~16000 chars>",
  "firstKeptEntryId": 17170331,
  "tokensBefore": 80458,
  "details": {
    "readFiles": ["...", "KNOWLEDGE.md", "..."],
    "modifiedFiles": ["..."]
  },
  "fromHook": true
}
```

Field chính:

| Field | Ý nghĩa |
|---|---|
| `summary` | Text được chèn đầu mỗi turn kế tiếp — chính là cái modal UI show. |
| `firstKeptEntryId` | Mốc chia: entry trước id này bị thay bằng `summary`; entry từ id này trở đi vẫn giữ. |
| `tokensBefore` | Tổng context tại thời điểm compact fire. |
| `details.readFiles` | File được đọc vào prompt compact (KNOWLEDGE.md, HEARTBEAT.md, SKILL active…). Méo có thể đến từ bất kỳ file nào trong đây. |
| `fromHook` | `true` khi trigger từ hook; xem phần trigger. |

## Quy trình compact

1. Trigger fire (xem phần sau) — thường là safeguard của runtime gần giới hạn context.
2. Runtime đọc history gần đây + các file trong `details.readFiles`.
3. Gọi 1 LLM riêng để tóm tắt input đó thành 1 chuỗi (≤ ~16000 chars — cap cứng quan sát được).
4. Ghi record compaction vào session `.jsonl` với `type:"compaction"`.
5. Từ turn kế tiếp, entry trước `firstKeptEntryId` **không** còn gửi lên LLM; `summary` được nhét vào vị trí đó trong prompt.

## Prompt: trước vs sau compact

```
TRƯỚC compact                            SAU compact
─────────────────                        ─────────────────
[system prompt]                          [system prompt]
[SOUL.md / AGENTS.md]                    [SOUL.md / AGENTS.md]
[history entries                         [📋 SUMMARY ~3-4k tokens]  ← MỚI
 ... turn 1                              [kept entries sau
 ... turn 2                               firstKeptEntryId]
 ...                                     [SKILL.md load theo event]
 ... turn N]                             [user event mới]
[SKILL.md load theo event]
[user event mới]
```

Vì summary **đứng trước** SKILL.md trong prompt, LLM có xu hướng coi nó là "context đã chốt" và cho trọng số cao hơn.

## Trigger compact (phân biệt manual vs auto)

Có 3 nguồn khả dĩ; hiện chỉ nguồn đầu tiên đang active:

| Nguồn | Trigger | Side-effects | `fromHook` quan sát |
|---|---|---|---|
| **Hook nội bộ OpenClaw** | OpenClaw mode `safeguard` gần giới hạn context (`reserveTokensFloor` 5000) | — | `true` |
| **OS server RPC — hiện đang tắt** (`maybeAutoCompact` trong `system/server/agent/delivery/http/handler_session_lifecycle.go`) | Call site bị comment trong `handler_event_agent.go`, thay bằng `maybeAutoNewSession`. Nếu bật lại: fire khi `ShouldRotateSession` trả true, gọi `agentGateway.CompactSession(sessionKey)` | TTS nói câu `PhraseCompactNotice`; cooldown 2 phút qua `h.compacting` atomic; flow event `compact_triggered` | chưa rõ — cần verify từ source OpenClaw |
| **Manual / debug** | Ai đó gọi `sessions.compact` RPC trực tiếp | — | nhiều khả năng `false` |

**Heuristic để phân biệt:** khi trigger OS server đang tắt, mọi record mới đều từ runtime (hoặc gọi tay). Nếu bật lại trigger, record có `timestamp` cách vài giây sau log `"sessions.compact sent"` của OS server cho cùng `sessionKey` → OS server initiate.

Tương lai có thể: modal correlate timestamp của compact mới nhất với log OS server để label trigger.

## Tần suất thực tế (mẫu 48h lịch sử, session main)

Ghi lại khi cả runtime và OS server đều compact ở ~80k; giữ để tham khảo.

| Pattern | Interval giữa các lần compact |
|---|---|
| Busy ban ngày | 1–3 h |
| Idle qua đêm | 10–13 h |
| Burst bất thường | nhiều lần compact trong vài phút ở `tokensBefore ≈ 45–60k` (dưới ngưỡng 80k thời đó) |

Burst bất thường chưa rõ nguyên nhân — có thể session restart / checkpoint restore làm hook fire spurious, hoặc có tool nào đó re-issue `sessions.compact`. Cần điều tra khi tái diễn.

## Vì sao summary có thể làm agent sai

1. **Priority inversion.** Summary đứng trước SKILL.md trong prompt; LLM coi là fact cao hơn.
2. **Generalization.** Quá trình tóm tắt thường biến case hẹp (vd *"drink activity cho known user → warm acknowledgement"*) thành rule chung (*"known-user activity events → warm acknowledgement"*) — rồi agent áp sang case không liên quan.
3. **Staleness.** Summary đóng băng giá trị field tại thời điểm compact. Ví dụ: SKILL.md update `last=50` → `last=200`, nhưng summary cũ vẫn nói `last=50` và agent làm theo đến lần compact tiếp theo.
4. **Generational loss.** Mỗi compaction đọc summary *trước đó* như input. Rule méo bị summarize lại → drift dồn, kiểu JPEG-save-JPEG.
5. **Hard cap.** Summary cap quanh 16000 chars (quan sát: 3 record riêng biệt đều chạm đúng số này). Nội dung bị drop không xác định được khi đụng cap.

Khi Flow Monitor cho thấy agent viện rule mà `grep` không tìm thấy trong bất kỳ `skills/**/SKILL.md` → nguồn gần như luôn là compaction summary, không phải skill đang load.

## Cách xem summary đang active

**UI.** Flow Monitor header → nút **📋 Summary** → modal show `timestamp`, `summary chars`, `session file`, và toàn văn summary.

**API.** `GET /api/agent/compaction-latest?session=<key>&at=<iso-ts>` (admin auth; session key mặc định: `agent:main:main`; `at` rỗng = record mới nhất, ngược lại là compaction đang active tại thời điểm đó). Chỉ cho OpenClaw: đọc `sessions.json` và scan session `.jsonl`. Đây là viewer read-only — OS server không có HTTP endpoint nào trigger compaction. Response schema:

```json
{
  "status": 1,
  "data": {
    "found": true,
    "sessionKey": "agent:main:main",
    "sessionFile": "/root/.openclaw/agents/main/sessions/<id>.jsonl",
    "compactionCount": 18,
    "id": 17170331,
    "parentId": "369818c9",
    "timestamp": "2026-04-24T03:21:30.305Z",
    "nextTimestamp": "",
    "tokensBefore": 80458,
    "summaryChars": 14263,
    "summary": "...",
    "details": { "readFiles": ["..."], "modifiedFiles": ["..."] },
    "fromHook": true,
    "firstKeptEntryId": 17170331,
    "atQuery": ""
  }
}
```

**Trực tiếp (Pi SSH).** Tất cả compaction record nằm trong session `.jsonl`. Pull kèm timestamp + metadata:

```bash
sudo grep '"type":"compaction"' \
  /root/.openclaw/agents/main/sessions/<sessionId>.jsonl \
  | python3 -c 'import json,sys
for l in sys.stdin:
    d=json.loads(l)
    print(d["timestamp"], d.get("tokensBefore"), len(d.get("summary","")))'
```

## File liên quan

| File | Vai trò |
|---|---|
| `system/server/agent/delivery/http/handler_api_compaction.go` | HTTP handler: đọc `sessions.json`, scan session `.jsonl` tìm record `type:"compaction"` active tại `?at` (mặc định mới nhất). |
| `system/server/agent/delivery/http/handler_session_lifecycle.go` | `maybeAutoNewSession` (active: `/new` khi rotate, cooldown 30 s) và `maybeAutoCompact` (đang tắt: TTS notice, cooldown 2 phút). |
| `system/server/agent/delivery/http/handler_event_agent.go` | Hook token-usage sau mỗi turn; gọi `maybeAutoNewSession`, lời gọi `maybeAutoCompact` bị comment. |
| `runtimes/openclaw/service_chat.go` | `CompactSession(sessionKey)` — sender của `sessions.compact` RPC; `sessionRotateTokenThreshold = 150_000`. |
| `runtimes/openclaw/onboarding.go` | Ép OpenClaw `compaction.mode = "safeguard"`, `reserveTokensFloor = 5000`. |
| `system/domain/agent.go` | Interface `AgentGateway.CompactSession`, `NewSession`, `ShouldRotateSession`. |
| `system/web/src/pages/monitor/FlowSection/CompactionModal.tsx` | UI modal — show timestamp, summary chars, session file, toàn văn summary; link về doc này. |
| `docs/flow-monitor.md` | Doc cha — cross-reference doc này. |

Bản tiếng Anh đầy đủ: [`docs/agent-compaction.md`](../agent-compaction.md).
