# `chat_send` flow event thiếu `type` cho system messages

**Status:** FIXED 2026-04-22 (Option A) — `flow.Log("chat_send", ...)` giờ có `"type": "user"|"system"`, 3 system callers (skill watcher / wake greeting / `/compact`) dùng `SendSystemChatMessage`. Xem `docs/flow-monitor.md` section "Event → node labels". Doc này giữ lại làm historical context.

Observed 2026-04-22 qua run `lamp-chat-154-1776830220031` (skill watcher notify).

## Vấn đề

`flow.Log("chat_send", ...)` tại `runtimes/openclaw/service.go:2033` không có field `type`:

```go
flow.Log("chat_send", map[string]any{
    "run_id":      idempotencyKey,
    "has_session": sessionKey != "",
    "has_image":   hasImage,
    "image_bytes": len(imageBase64),
    "message":     message,
}, idempotencyKey)
```

Tất cả các system-level callers của `SendChatMessage` bị mờ type trong Flow Monitor — UI chỉ thấy `"message": "[system] ..."` với prefix text, không có metadata structured.

## 3 callers hiện tại

| Caller | File:line | Message | Nature |
|---|---|---|---|
| Skill watcher | `runtimes/openclaw/skill_watcher.go:100` | `[system] The following skills have been updated...` | system.skill_updated |
| Wake greeting | `system/server/server.go:414` | `You just woke up. Greet the user briefly.` | system.wake |
| Compact | `system/server/agent/delivery/http/handler.go:725` | `/compact` | system.compact |

So sánh với sensing events có structured type: `voice`, `voice_command`, `presence.enter`, `presence.leave`, `presence.away`, `motion.activity`, `emotion.detected` (`system/sensing/service.go:245-262`, since removed — the event types now live in `system/lib/sensingmsg/sensingmsg.go`).

## Fix idea

Option A (minimal): Thêm `type` param vào `sendChat` signature, default `"user"`; system callers truyền `"system"`.

```go
func (s *Service) SendChatMessage(message string) (string, error) {
    return s.sendChat(message, "", "", "", "user")
}
func (s *Service) SendSystemChatMessage(message string) (string, error) {
    return s.sendChat(message, "", "", "", "system")
}
```

Và thêm `"type": sourceType` vào payload của `flow.Log("chat_send", ...)`.

Option B (thorough): Subtype per caller: `system.skill_updated`, `system.wake`, `system.compact`. Dễ filter/badge trên UI.

## Scope

Chỉ ảnh hưởng Flow Monitor rendering — không thay đổi WS RPC `chat.send` params gửi sang OpenClaw. Agent vẫn nhận `[system]` prefix như cũ để hiểu context.
