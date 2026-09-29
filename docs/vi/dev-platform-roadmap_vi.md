# Lộ trình Developer Platform

Kế hoạch mở nền tảng thiết bị cho dev bên thứ ba đã sở hữu phần cứng. Bao gồm SDK, MCP server, CLI và skill marketplace.

> **Giả định scope:** dev đã có thiết bị. Lộ trình này không bao gồm simulator hay cloud pool.

## 1. Đối tượng dev (personas)

| Persona | Hồ sơ | Mục tiêu | Bề mặt sử dụng |
|---------|-------|----------|----------------|
| **Hobbyist** | Dev đơn lẻ, dự án cuối tuần | Cho thiết bị làm trò vui từ laptop / Claude Desktop | HAL HTTP, MCP, CLI |
| **Enterprise integrator** | Team nhúng thiết bị vào sản phẩm (kiosk, hotel, retail) | API ổn định, quản lý fleet, có SLA | Typed SDK, OpenAPI, webhooks, fleet API |
| **Skill author** | Viết skill OpenClaw cho marketplace | Mở rộng hành vi agent, ship cho user khác | Skill contract, scaffold CLI, registry |

## 2. Bề mặt hiện có (audit code)

| Service | Port | Stack | Bề mặt | Auth hiện tại |
|---------|------|-------|--------|---------------|
| OS Server | 5000 | Go / Gin | `/api/sensing/*`, `/api/agent/*` (kèm SSE), `/api/guard/*`, `/api/mood/*`, `/api/monitor/*` | Không có, chỉ 127.0.0.1 |
| HAL | 5001 | Python / FastAPI | ~80 endpoints: `/servo/*`, `/led/*`, `/camera/*`, `/audio/*`, `/voice/*`, `/face/*`, `/emotion/*`, `/scene/*`, `/display/*`, `/sensing/*`, `/speaker/*` | Không có, chỉ 127.0.0.1 |
| Buddy | 5002 | Go | `/status`, `/approve`, `/deny`, BLE peripheral | Không có, chỉ 127.0.0.1 |
| DLBackend | tuỳ deploy | Python / FastAPI | `/api/dl/yoloworld`, `/api/dl/grounding-dino` | Không có |

Hệ quả: **mọi service đang local-only, không auth**. Đây là blocker đầu tiên.

## 3. Phase plan

### Phase 0 — Nền tảng (blocker, ~1 sprint)

| Hạng mục | Tại sao | Ảnh hưởng |
|----------|---------|-----------|
| API token auth cho HAL + OS server | Không có thì dev phải SSH vào thiết bị mãi mãi | `system/server`, `hal/server.py` |
| Bind 0.0.0.0 + tài liệu firewall | Cho phép truy cập từ laptop dev qua LAN | systemd units, web UI doc |
| API versioning `/v1/*` | Khoá contract trước khi mở | Tất cả HTTP handler |
| OpenAPI spec | Đầu vào cho codegen SDK | HAL tự sinh từ FastAPI; OS server cần swaggo / kin-openapi |
| mDNS broadcast `lamp.local` | CLI / SDK auto-discover | systemd avahi |
| Dev portal (`docs/dev/`) | Quickstart, auth, API ref | docs only |

**Quyết định product cần chốt:** per-device token (đơn giản) vs cloud-issued JWT (cần cho marketplace). Đề nghị: per-device token cho P0–P2, thêm cloud JWT ở P3.

### Phase 1 — Hobbyist (1–2 sprint)

Mục tiêu: "mở hộp → thiết bị nhúc nhích từ Claude Desktop trong 10 phút".

| Deliverable | Tech | Ghi chú |
|-------------|------|---------|
| `lamp-mcp` server | Python pkg | Wrap HAL HTTP. Tools: `move_servo`, `play_animation`, `set_led`, `set_emotion`, `snap_photo`, `speak`, `subscribe_sensing`. One-line config cho Claude Desktop / Cursor |
| `lamp` CLI | Go single binary | `lamp pair`, `servo nod`, `led rainbow`, `emotion happy`, `events -f`, `shell` (REPL) |
| `lamp-examples` repo | Hỗn hợp | 5 recipe: LED theo giờ, Slack→emotion, presence light, voice memo, cron dance |
| Quickstart docs + video 90s | docs/dev/quickstart | EN + VI |

### Phase 2 — Enterprise (2–3 sprint)

Mục tiêu: tích hợp thiết bị vào sản phẩm bên thứ ba với contract ổn định.

| Deliverable | Tech | Ghi chú |
|-------------|------|---------|
| Typed SDKs | OpenAPI codegen | Python, TS, Go. Không viết tay client |
| Webhooks | OS server | Mirror SSE; retry + backoff; HMAC signature |
| Fleet API | Cloud (mới) | Device registry, batch command, status rollup |
| Observability | OS server + HAL | `/metrics` Prometheus, structured JSON logs, `/v1/health/deep` |
| Stability commitment | Process | Semver cho `/v1/*`, deprecation 6 tháng, CHANGELOG.md |
| Rate limit + quota | OS server gateway | Per-token |

### Phase 3 — Skill marketplace (3–4 sprint, dài nhất)

Mục tiêu: dev ngoài viết skill, publish, user cài 1-click.

| Deliverable | Tech | Ghi chú |
|-------------|------|---------|
| Skill contract v1 | Spec | `SKILL.md` frontmatter, `KNOWLEDGE.md`, `AGENTS.md` priority — chuẩn hoá từ guard/music/wellbeing/buddy hiện có |
| Manifest `skill.yaml` | Spec | name, version, permissions, agent-target, required-services |
| `lamp skill init` | CLI | Scaffold template + handler stub |
| `lamp skill dev` | CLI | Watch + push lên thiết bị attached, hot reload |
| `lamp skill build/publish` | CLI + registry | Zip + sign, upload lên Autonomous registry |
| Registry | Cloud | API `publish/install/list/remove` + web UI |
| Permission prompt | OS server UI | User duyệt khi cài (kiểu mobile app) |
| Sandbox | Khó | Giới hạn fs/network. Ban đầu: chỉ skill được Autonomous review mới bypass |
| Marketplace web UI | `system/web` hoặc site mới | Browse, install, quản lý |

**Quyết định product/legal cần chốt:** quy trình review, revenue model, chính sách chống lạm dụng, content guideline.

## 4. Phụ thuộc & critical path

| Phase | Phụ thuộc | Chặn |
|-------|-----------|------|
| P0 | — | Mọi phase còn lại |
| P1 | P0 (auth, mDNS) | — |
| P2 | P0 (OpenAPI, versioning) | — |
| P3 | P0 (auth) + skill contract freeze | Marketplace launch |

P2 và P3 chạy song song được sau khi P0 xong vì đụng bề mặt khác nhau.

## 5. Vấn đề xuyên suốt

- **Đóng băng API nội bộ**: trước khi P1 ship, khoá v1 endpoint của HAL / OS server. Nội bộ chạy trên `/internal/*`, public chỉ tiêu thụ `/v1/*`. Sẽ chậm velocity nội bộ — chấp nhận trade-off.
- **Sample skills**: nâng guard / music / wellbeing / buddy thành "official skill" — vừa làm template marketplace vừa kiểm chứng contract.
- **Kênh hỗ trợ**: Discord hoặc GitHub Discussions, có lịch trực.
- **Docs discipline**: mỗi phase ship đồng thời EN (`docs/`) + VI (`docs/vi/`) theo rule CLAUDE.md.

## 6. Rủi ro

| Rủi ro | Phase | Giảm thiểu |
|--------|-------|------------|
| Skill sandbox ngốn 6–12 tháng nếu làm đầy đủ | P3 | Bắt đầu beta curated-only (P3a), mở marketplace sau (P3b) |
| API freeze làm chậm team nội bộ | P1+ | Tách `/internal/*` vs `/v1/*` nghiêm; chính sách deprecation rõ |
| Không auth → lộ bảo mật khi bind LAN | P0 | Token rotation, hướng dẫn WireGuard cho remote |
| Compaction OpenClaw làm méo skill rules | P3 | Skill contract phải enforce bằng code, không chỉ markdown — xem `docs/agent-compaction.md` |

## 7. Scope sprint đầu (đề xuất)

Chỉ Phase 0:
1. Plumb API token cho HAL + OS server (5001 + 5000).
2. Bind LAN + sửa systemd unit.
3. Migrate route sang prefix `/v1/`.
4. Commit OpenAPI spec (HAL tự sinh, OS server viết tay).
5. Skeleton dev portal (dự kiến `docs/dev/index.md` + `docs/vi/dev/index.md`; chưa được tạo).

Kết quả: dev bên ngoài có thể `curl -H "Authorization: Bearer ..." http://lamp.local:5001/v1/servo/play` từ laptop. Mọi thứ khác xếp lên trên nền này.
