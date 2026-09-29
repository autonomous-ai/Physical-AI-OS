# Tài liệu Autonomous OS

Mục lục tài liệu nền tảng bằng tiếng Việt trong `docs/vi/`. Tài liệu chưa có bản
tiếng Việt được đánh dấu **(chỉ EN)** và trỏ tới bản tiếng Anh. Bản tiếng Anh của
mục lục này: [`../README.md`](../README.md).

## Bắt đầu

- [Tổng quan kiến trúc](overview_vi.md) — stack 3 lớp (agentic runtime → OS server → HAL → phần cứng).
- [Hướng dẫn cho developer](../developer-guide.md) **(chỉ EN)** — SSH, deploy code, xem log, gọi API HAL và OS Server.
- [Simulator](simulator_vi.md) — chạy toàn bộ stack trên laptop.
- [Tự mang robot của bạn](../bring-your-own-robot.md) **(chỉ EN)** — ba file markdown và một driver, bảy bước.
- [Port một robot](../porting-a-robot.md) **(chỉ EN)** — hướng dẫn port chi tiết.
- [Mỗi robot làm được gì](../robot-comparison.md) **(chỉ EN)** — bảng skill theo capability.
- [Hosted và local](../hosted.md) **(chỉ EN)** — những gì kết nối về cloud, những gì ở lại mạng nội bộ, và khi offline.

## Kiến trúc & OS server

- [Thiết kế Autonomous](../architecture/DESIGN.md) **(chỉ EN)** — nguyên tắc kiến trúc để nhiều thiết bị dùng chung một OS.
- [Kiến trúc phân lớp](../architecture/overview.md) **(chỉ EN)** — các lớp và giao diện giữa chúng.
- [HAL](../architecture/hal.md) **(chỉ EN)** — giao diện capability cố định giữa OS và phần cứng.
- [Kernel](../architecture/kernel.md) **(chỉ EN)** — kernel là Linux kernel của vendor.
- Script vẽ hình: [`build_figures.py`](../architecture/build_figures.py) và [`figs.py`](../architecture/figs.py) **(chỉ EN)**.
- [OS Server API](os-server_vi.md) — endpoint và khởi động os-server (:5000).
- [MQTT](mqtt_vi.md) — MQTT với backend: báo trạng thái, lệnh OTA, quản lý channel.
- [Web UI](web-ui_vi.md) — SPA cấu hình và monitor.
- [Flow Monitor](flow-monitor_vi.md) — pipeline turn, JSONL, SSE.
- [Compaction session agent](agent-compaction_vi.md) — cơ chế tóm tắt tự động và vì sao có thể lấn SKILL.md.
- [Hệ thống plugin](plugin-system_vi.md) — app Python độc lập mở rộng thiết bị qua HAL.
- [Model weights trên thiết bị](hal-models_vi.md) — nơi lưu và cách tải ONNX weights của HAL.
- [Telemetry thiết bị](telemetry_vi.md) — pipe telemetry và quy tắc thêm tracker.
- [Chỉ số phản hồi giọng nói](voice-metrics_vi.md) — tracker KPI voice.
- [Chỉ số hoàn thành tác vụ](task-metrics_vi.md) — cohort chat và sensing.
- [Chi phí một turn](../benchmarks.md) **(chỉ EN)** — phương pháp benchmark.

## Setup, OTA & provisioning

- [Luồng setup](setup-flow_vi.md) — onboarding chế độ AP: WiFi, LLM provider, channel.
- [Bootstrap & OTA](bootstrap-ota.md) — các thành phần cài đặt và OTA worker.

## Giọng nói & realtime

- [Realtime voice agent](realtime-voice_vi.md) — lớp speech-to-speech và delegate sang agent chính.
- [Nhận diện cảm xúc giọng nói](speech-emotion_vi.md) — SER từ giọng người dùng.
- [Quy trình test Gemini idle-session recycle](../testing-gemini-idle-recycle.md) **(chỉ EN)**.

## Agent runtime

- [Thêm agentic runtime](agentic/adding-agent-runtime_vi.md) — hợp đồng AgentGateway và những gì backend mới phải nối.
- [OpenClaw](agentic/openclaw_vi.md) — plugin preload skill Jev.
- [Hermes](agentic/hermes_vi.md)
- [Remote Hermes](agentic/remote-hermes_vi.md) — dùng thiết bị làm voice frontend cho Hermes trên Mac.
- [PicoClaw](agentic/picoclaw_vi.md)
- [Codex](agentic/codex_vi.md)
- [Claude Code](agentic/claudecode_vi.md)
- [OpenCode](agentic/opencode_vi.md)
- [Tích hợp Harness](harness_vi.md) — ghép cặp thiết bị với máy Harness.
- [Harness Store](harness-store_vi.md) — hợp đồng Store v1; schema: [`contracts/autonomous-device-store-v1`](../contracts/autonomous-device-store-v1/README.md) **(chỉ EN)**.
- [Review skills — 2026-09-10](skills-review_vi.md) — review mã nguồn toàn bộ `skills/*/SKILL.md`.

## Perception & sensing

- [Perception service](perception-service_vi.md) — suy luận DL trên cloud, load balancer, mã hóa, model. Tài liệu service: [`integrations/perception-service/docs`](../../integrations/perception-service/docs/README.md) **(chỉ EN)**.

## An toàn & bảo mật

- [Safety engine](safety_vi.md) — thực thi `SAFETY.md` một cách tất định, bên dưới agent.
- Audit bảo mật **(chỉ EN)**: [checklist](../security/CHECKLIST.md), [Go server](../security/go-server-audit.md), [ranh giới local-only](../security/local-only-boundary.md), [web frontend](../security/web-frontend-audit.md).

## Phát triển & CI

- [Phát triển đa IDE](../DEV-MULTI-IDE.md) **(chỉ EN)** — quy ước Cursor + Claude Code.
- [CI](ci_vi.md) — CI chạy gì và cách chạy local.

## Kế hoạch & roadmap

- [Roadmap nền tảng developer](dev-platform-roadmap_vi.md) — SDK, MCP server, CLI, marketplace skill.
- [Chưa làm — nhận việc](../not-built-yet.md) **(chỉ EN)** — các issue `claim-me`.
- [Prompt bàn giao Buddy MQTT cho mobile](buddy-mobile-handoff_vi.md) — chỉ có bản tiếng Việt.

## Tham khảo & marketing

- [Trang sản phẩm](../autonomous-os-product-page.html) — trang HTML độc lập (ảnh động: [`media/hero.gif`](../media/hero.gif)).

## Robot, tích hợp và companion

- [Hợp đồng robot](../../robots/contract/ROBOT-SPEC.md) **(chỉ EN)** — kèm [WIFI-BODY (VI)](../../robots/contract/vi/WIFI-BODY_vi.md).
- [Tài liệu Lamp](../../robots/lamp/README.md#docs) — có liên kết VI cho từng tài liệu.
- Reachy Mini: [runtime](../../robots/reachy-mini/docs/vi/runtime_vi.md), [recovery](../../robots/reachy-mini/docs/vi/recovery_vi.md), [first-boot](../../robots/reachy-mini/docs/vi/first-boot-plan_vi.md), [hệ sinh thái Pollen](../../robots/reachy-mini/docs/vi/pollen-ecosystem-analysis_vi.md).
- [Tài liệu Autonomous Buddy](../../integrations/companions/autonomous-buddy/README.md#docs) — có liên kết VI.
