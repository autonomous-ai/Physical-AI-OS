# Developer Platform Roadmap

Plan for opening up the device platform to third-party developers who already have device hardware. Covers SDK, MCP server, CLI, and skill marketplace.

> **Scope assumption:** developers own a device. No simulator/cloud-pool work in this roadmap.

## 1. Target personas

| Persona | Profile | Goal | Surface they touch |
|---------|---------|------|---------------------|
| **Hobbyist** | Single dev, weekend project | Make their device do fun things from laptop / Claude Desktop | HAL HTTP, MCP, CLI |
| **Enterprise integrator** | Team embedding the device into a product (kiosk, hotel, retail) | Predictable API, fleet control, SLA | Typed SDK, OpenAPI, webhooks, fleet API |
| **Skill author** | Writes OpenClaw skills for the marketplace | Extend agent behavior, ship to other device owners | Skill contract, scaffold CLI, registry |

## 2. Existing surfaces (from code audit)

| Service | Port | Stack | Surface | Auth today |
|---------|------|-------|---------|------------|
| OS Server | 5000 | Go / Gin | `/api/sensing/*`, `/api/agent/*` (incl. SSE), `/api/guard/*`, `/api/mood/*`, `/api/monitor/*` | None, 127.0.0.1 only |
| HAL | 5001 | Python / FastAPI | ~80 endpoints: `/servo/*`, `/led/*`, `/camera/*`, `/audio/*`, `/voice/*`, `/face/*`, `/emotion/*`, `/scene/*`, `/display/*`, `/sensing/*`, `/speaker/*` | None, 127.0.0.1 only |
| Buddy | 5002 | Go | `/status`, `/approve`, `/deny`, BLE peripheral | None, 127.0.0.1 only |
| DLBackend | varies | Python / FastAPI | `/api/dl/yoloworld`, `/api/dl/grounding-dino` | None |

Key implication: **everything is local-only with no auth**. That is the first blocker.

## 3. Phase plan

### Phase 0 — Foundation (blocker, ~1 sprint)

| Item | Why | Touches |
|------|-----|---------|
| API token auth on HAL + OS server | Without this, devs must SSH into device forever | `system/server`, `hal/server.py` |
| Bind 0.0.0.0 + firewall guidance | LAN access from dev laptop | systemd units, web UI doc |
| API versioning `/v1/*` | Lock contract before opening | All HTTP handlers |
| OpenAPI spec | Codegen feedstock for SDKs | FastAPI auto for HAL; swaggo / kin-openapi for the OS server |
| mDNS broadcast `lamp.local` | CLI / SDK auto-discover | systemd avahi config |
| Dev portal (`docs/dev/`) | Quickstart, auth, API ref | docs only |

**Product decision required:** per-device token (simpler) vs cloud-issued JWT (needed for marketplace). Recommendation: per-device token for P0–P2, cloud JWT layer added in P3.

### Phase 1 — Hobbyist (1–2 sprints)

Goal: "unbox → the device moves from Claude Desktop in 10 minutes".

| Deliverable | Tech | Notes |
|-------------|------|-------|
| `lamp-mcp` server | Python pkg | Wraps HAL HTTP. Tools: `move_servo`, `play_animation`, `set_led`, `set_emotion`, `snap_photo`, `speak`, `subscribe_sensing`. One-line install for Claude Desktop / Cursor |
| `lamp` CLI | Go single binary | `lamp pair`, `servo nod`, `led rainbow`, `emotion happy`, `events -f`, `shell` (REPL) |
| `lamp-examples` repo | Mixed | 5 recipes: time-of-day LED, Slack→emotion, presence light, voice memo, cron dance |
| Quickstart docs + 90s video | docs/dev/quickstart | EN + VI |

### Phase 2 — Enterprise (2–3 sprints)

Goal: integrate the device into a third-party product with stable contracts.

| Deliverable | Tech | Notes |
|-------------|------|-------|
| Typed SDKs | OpenAPI codegen | Python, TS, Go. No hand-written clients |
| Webhooks | OS server | Mirror SSE; retry + backoff; HMAC sig |
| Fleet API | Cloud (new) | Device registry, batch command, status rollup |
| Observability | OS server + HAL | `/metrics` Prometheus, structured JSON logs, `/v1/health/deep` |
| Stability commitment | Process | Semver on `/v1/*`, 6-month deprecation, CHANGELOG.md |
| Rate limit + quota | OS server gateway | Per-token |

### Phase 3 — Skill marketplace (3–4 sprints, longest)

Goal: external dev writes skill, publishes, user installs in 1 click.

| Deliverable | Tech | Notes |
|-------------|------|-------|
| Skill contract v1 | Spec | `SKILL.md` frontmatter, `KNOWLEDGE.md`, `AGENTS.md` priority — derived from existing guard/music/wellbeing/buddy |
| `skill.yaml` manifest | Spec | name, version, permissions, agent-target, required-services |
| `lamp skill init` | CLI | Scaffold template + handler stub |
| `lamp skill dev` | CLI | Watch + push to attached device, hot reload |
| `lamp skill build/publish` | CLI + registry | Zip + sign, upload to Autonomous registry |
| Registry | Cloud | `publish/install/list/remove` API + web UI |
| Permission prompt | OS server UI | User approves on install (mobile-app style) |
| Sandbox | Hard | Limit fs/network. Initially: only Autonomous-reviewed skills bypass |
| Marketplace web UI | `system/web` or new site | Browse, install, manage |

**Product/legal decisions required:** review process, revenue model, abuse policy, content guidelines.

## 4. Dependencies & critical path

| Phase | Depends on | Blocks |
|-------|-----------|--------|
| P0 | — | All other phases |
| P1 | P0 (auth, mDNS) | — |
| P2 | P0 (OpenAPI, versioning) | — |
| P3 | P0 (auth) + skill contract freeze | Marketplace launch |

P2 and P3 can run in parallel after P0 completes — they touch different surfaces.

## 5. Cross-cutting concerns

- **Internal API freeze**: before P1 ships, lock HAL / OS server v1 endpoints. Internal moves on `/internal/*`, public consumes only `/v1/*`. Affects internal velocity — accept the trade.
- **Sample skills**: promote guard / music / wellbeing / buddy to "official skill" status — they double as marketplace templates and contract validators.
- **Support channel**: Discord or GitHub Discussions, on-call rotation.
- **Docs discipline**: every phase ships with EN (`docs/`) and VI (`docs/vi/`) per CLAUDE.md rule.

## 6. Risks

| Risk | Phase | Mitigation |
|------|-------|------------|
| Skill sandbox is 6–12 months of work if done fully | P3 | Start with curated-only beta (P3a), open marketplace later (P3b) |
| API freeze slows internal team | P1+ | Strict `/internal/*` vs `/v1/*` split; deprecation policy |
| No-auth → security exposure when bound to LAN | P0 | Token rotation, optional WireGuard guidance for remote access |
| OpenClaw compaction warps skill rules | P3 | Skill contract must be code-enforced, not just markdown — see `docs/agent-compaction.md` |

## 7. First sprint scope (proposed)

Phase 0 deliverables only:
1. API token plumbing on HAL + OS server (5001 + 5000).
2. LAN binding + systemd unit changes.
3. `/v1/` route prefix migration.
4. OpenAPI spec committed (HAL auto, OS server handwritten).
5. Dev portal skeleton (planned `docs/dev/index.md` + `docs/vi/dev/index.md`; not created yet).

Outcome: third-party dev can `curl -H "Authorization: Bearer ..." http://lamp.local:5001/v1/servo/play` from their laptop. Everything else stacks on top.
