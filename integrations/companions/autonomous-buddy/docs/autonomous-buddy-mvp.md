# Autonomous Buddy MVP — Implementation Plan

> **Status:** Ready to execute
> **Last updated:** 2026-05-21
> **Design doc:** [autonomous-buddy.md](./autonomous-buddy.md)
> **Target completion:** ~2 weeks (single dev)

This is the actionable plan for **MVP of Autonomous Buddy** — the macOS companion app that lets the device control the user's computer via voice. Full design rationale in [autonomous-buddy.md](./autonomous-buddy.md). This doc lists *what to build, in what order, with acceptance criteria*.

---

## Scope

**In scope:**
- macOS-only (macOS 13+)
- Swift Package Manager project at `autonomous-buddy/`
- Menu bar app (`NSStatusItem`, no Dock icon)
- mDNS discovery of lamp on LAN
- 6-digit pairing flow (lamp web UI shows code)
- Persistent WS connection (`buddy → lamp`)
- Command executors: `open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`
- Lamp Go: `system/buddy/` package + 7 HTTP routes + WS gateway
- OpenClaw skill `computer-use` (basic intent → command mapping)
- Web UI: "Paired Computers" page in `lamp/web/`
- Audit log (backend file only — no UI in MVP)

**Out of scope (defer to post-MVP):**
- Vision / screenshot commands
- AppleScript executor beyond simple `close_app`
- Windows / Linux ports
- Code signing / notarization (right-click → Open is the install method)
- Sparkle / auto-update
- TLS on WS (LAN-only + pairing seen as sufficient for self-hosted MVP)
- Multi-buddy per lamp
- Audit log UI
- Rate-limit UI
- Lamp restart push to buddy
- Buddy resource monitoring

---

## Phases

Each phase is independently shippable and reviewable.

### Phase 1A — Folder + Swift scaffold

**Status:** ✓ Done.

**Files:**
- `autonomous-buddy/README.md`
- `autonomous-buddy/macos/Package.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/main.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/AppDelegate.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/MenuBarController.swift`
- `autonomous-buddy/.gitignore`

**Acceptance:** `cd autonomous-buddy/macos && swift run` shows a status bar icon. Menu has "About Autonomous Buddy", "Quit". No crash. Process activation policy is `.accessory` (no Dock icon).

### Phase 1B — Lamp discovery (mDNS)

**Status:** ✓ Done — Bonjour browse for `_autonomous._tcp` works; manual hostname fallback also wired.

**Files:**
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Discovery/DeviceDiscovery.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Discovery/DeviceInfo.swift`
- Update `MenuBarController.swift` to show discovered lamps

**Acceptance:** When a lamp is running on LAN (advertises `_autonomous._tcp.local`), buddy menu shows e.g. `lamp-a1b2.local — 192.168.1.50` as a clickable item. Also: manual hostname entry option.

> Note: the device publishes both the host record `<device_type>-<last4hex>.local` (e.g. `lamp-a1b2.local`) AND the `_autonomous._tcp` service for browsability. The service comes from a static avahi file (`/etc/avahi/services/autonomous.service`, port 80) dropped at provisioning (`scripts/provision/setup.sh` + `scripts/imager/build.sh` + `scripts/imager/build-orangepi.sh`). It uses avahi's `%h` wildcard, so one file serves every device class.

### Phase 1C — Pairing flow

**Status:** ✓ Done — 6-digit code + token persistence in `buddies.json` + Keychain on the Mac. Includes `DELETE /api/buddy/self` (Bearer-auth) so a user-initiated unpair in the buddy app also drops the lamp's record, keeping both sides in sync.

**Buddy files:**
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Pairing/PairingManager.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Pairing/PairingStore.swift` (Keychain)
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Pairing/PairingWindow.swift` (code entry UI)

**Lamp Go files:**
- `system/buddy/types.go`
- `system/buddy/store.go`
- `system/buddy/pairing.go`
- `system/buddy/service.go`
- `system/server/buddy/delivery/http/handler.go`
- `system/server/buddy/delivery/http/handler_pair.go`
- `system/buddy/wire.go`
- Modify: `system/server/server.go` (register routes)
- Modify: `system/server/wire.go` (provider)
- Run: `make generate`

**Lamp web files:**
- `lamp/web/src/pages/PairedComputers.tsx` (initial — just code display)
- Update `lamp/web/src/App.tsx` (route)
- Update `lamp/web/src/lib/api.ts` (pair endpoints)

**Routes added:**
- `POST /api/buddy/pair/start`
- `POST /api/buddy/pair/confirm`
- `GET  /api/buddy/list`
- `DELETE /api/buddy/:id`

**Acceptance:**
1. User opens buddy menu → "Pair with device" → device web UI displays 6-digit code
2. User reads code, types into buddy code entry window
3. Buddy stores token in Keychain
4. Lamp persists buddy in `buddies.json`
5. Buddy menu now shows "Paired with lamp-xxxx"
6. `GET /api/buddy/list` returns paired buddy

### Phase 1D — WebSocket connection

**Status:** ✓ Done — persistent WS with backoff reconnect. Lamp fires a `ping` hello command immediately after connect so the user's Activity window shows one ✓ row right away, confirming end-to-end reachability.

**Buddy files:**
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Connection/DeviceConnection.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Connection/Reconnect.swift`

**Lamp Go files:**
- `system/buddy/registry.go`
- `system/buddy/ws.go`
- `system/server/buddy/delivery/http/handler_ws.go`
- Update: `system/server/server.go` (register WS route)

**Routes added:**
- `GET /api/buddy/ws` (WS upgrade)
- `GET /api/buddy/status`

**Acceptance:**
- Buddy auto-connects WS on startup (and after pairing)
- Lamp logs `[buddy] connected: <fingerprint>` on connect
- Buddy menu shows green dot when connected, red when disconnected
- WS survives lamp reboot (buddy reconnects with backoff)
- `GET /api/buddy/status` returns `{"connected": [...], "paired": [...]}`

### Phase 1E — Command executors (buddy side)

**Status:** ✓ Done — 16 executors (the MVP set above plus `screenshot`, `click_at`, `scroll`, `mouse_move`, `drag`, `read_clipboard`, `write_clipboard`, `click_button` via Accessibility, `cursor_pos`, `list_displays`). The vision-shaped executors land here ahead of the formal vision phase so the bash+curl reference skill (`computer-use/references/vision.md`) can use them today.

**Files:**
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Command.swift` (types)
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/CommandDispatcher.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Executors/AppExecutor.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Executors/URLExecutor.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Executors/KeyboardExecutor.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Executors/NotificationExecutor.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Commands/Executors/PingExecutor.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Permissions/AccessibilityCheck.swift`
- `autonomous-buddy/macos/Sources/AutonomousBuddy/Audit/AuditLog.swift`

**Acceptance:**
- WS receives command JSON → dispatcher decodes → executor runs → response JSON returned
- All MVP actions implemented (`open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`)
- Permission denial returns clean error (not crash)
- Audit log file written to `~/Library/Application Support/AutonomousBuddy/audit.log`

### Phase 1F — Command dispatch (Lamp Go side)

**Status:** ✓ Done — sync `/api/buddy/command` (localOnly) + marker-friendly `/api/buddy/exec/:action`. Cross-compile `GOOS=linux GOARCH=arm64 go build ./...` clean. Debug log instrumentation across the chain (handler_hw → exec/command handler → dispatcher → ws read loop) so a failed turn is traceable to the exact stage.

**Files:**
- `system/buddy/dispatcher.go`
- `system/server/buddy/delivery/http/handler_command.go`
- Update: wire providers, run `make generate`

**Routes added:**
- `POST /api/buddy/command`

**Acceptance:**
- `curl -X POST http://lamp/api/buddy/command -H 'Authorization: Bearer <admin-token>' -d '{"action":"ping"}'` returns `{"ok":true,"result":{"pong":true}}`
- Timeout works (default 5s; 503 if buddy unresponsive)
- 404 if no buddy connected
- Concurrent commands handled (per-command ID matching for responses)

### Phase 1G — OpenClaw skill

**Status:** ✓ Done — English-only `SKILL.md` following the led-control / scene style, intent-based fire-and-forget HW markers (`[HW:/buddy/exec/<action>:{...}]`). Plus an opt-in `references/vision.md` for tasks that genuinely require seeing the screen (bash + curl loop against `/api/buddy/command`). The vision reference was tuned with Anthropic Computer Use prompting guidance (anchor screenshots at ~1280px wide, evaluate after every step, prefer keyboard shortcuts when coord clicks are risky).

**Files (location depends on OpenClaw skill conventions):**
- `computer-use/SKILL.md`
- `computer-use/script.sh` (or whatever scripting OpenClaw uses)

**Acceptance:**
- User says to lamp: "Mở Chrome trên máy tính" → buddy launches Chrome → lamp speaks "đã mở Chrome rồi"
- User says: "Vào Gmail trên máy" → buddy opens gmail.com
- User says: "Join Google Meet" → buddy opens last-used meet URL (TBD — config)
- Skill handles "no buddy paired" gracefully ("chưa có máy tính nào kết nối")

### Phase 1H — Web UI polish

**Status:** ✓ Done — `BuddyCard` in the Monitor Overview shows pair/status/revoke. The buddy app side also got a native menu-bar Activity submenu plus a separate "Activity" window (terminal-tail style) so the user can audit recent commands without opening the audit log file. Audit log path: `~/Library/Application Support/AutonomousBuddy/audit.log`.

**Files:**
- Update `lamp/web/src/pages/PairedComputers.tsx`
- Update `lamp/web/src/components/` as needed

**Acceptance:**
- Page lists paired buddies with name, OS, last seen, online/offline
- "Add new" button starts pairing flow, displays 6-digit code with countdown
- "Revoke" button per row works (lamp removes; buddy gets 401 → drops session)
- Visual indicator if a command is in flight

### Phase 1I — Docs + housekeeping

**Status:** ⏳ Deferred — VERSION_BUDDY file, root Makefile `build-buddy` target, and per-doc drift checks remain. Skipped for now because Leo is iterating solo; revisit when the project is shared or about to be released.

**Files:**
- Verify `docs/autonomous-buddy.md` matches actual implementation (update if drift)
- Verify `docs/vi/autonomous-buddy_vi.md` matches
- Add `autonomous-buddy/README.md` build instructions
- Update root `CLAUDE.md`: doc table row for autonomous-buddy
- Update top-level `Makefile`: `build-buddy` target
- Add `VERSION_BUDDY` file at root → `0.0.1`
- Bump `VERSION_OS_SERVER`, `VERSION_WEB` as needed

**Acceptance:**
- Fresh-checkout dev can `cd autonomous-buddy/macos && swift run` and follow README to pair with lamp
- CLAUDE.md doc table includes the new row
- `make build-buddy` produces `autonomous-buddy/.build/release/AutonomousBuddy`

---

## Lamp-side prerequisites (verify before Phase 1B)

1. **mDNS browsability** — ✓ Done. The device publishes `_autonomous._tcp` for `NWBrowser` via a static avahi service file (`/etc/avahi/services/autonomous.service`, port 80) baked at provisioning (`setup.sh` + `scripts/imager/build*.sh`), alongside the `<device_type>-xxxx.local` host record. The `%h` wildcard keeps it device-agnostic.
2. **Admin auth header convention** — confirm whether new buddy endpoints should use `Authorization: Bearer <token>` (cookie or bearer); reuse `project_security_login_ui_batch.md` patterns.
3. **OpenClaw skill location** — find where existing skills live, naming convention, how lamp registers them. (Possibly in lamp's filesystem `~/.openclaw/skills/<name>/SKILL.md`.)

---

## File inventory (final state after MVP)

### Swift (`autonomous-buddy/macos/`)
```
autonomous-buddy/
├── README.md
├── .gitignore
├── docs/                          # design + MVP plan (EN + VI)
└── macos/
    ├── Package.swift
    └── Sources/AutonomousBuddy/
        ├── main.swift
        ├── AppDelegate.swift
        ├── MenuBarController.swift
        ├── Discovery/
        │   ├── DeviceDiscovery.swift
        │   └── DeviceInfo.swift
        ├── Pairing/
        │   ├── PairingManager.swift
        │   ├── PairingStore.swift
        │   └── PairingWindow.swift
        ├── Connection/
        │   ├── DeviceConnection.swift
        │   └── Reconnect.swift
        ├── Commands/
        │   ├── Command.swift
        │   ├── CommandDispatcher.swift
        │   └── Executors/
        │       ├── AppExecutor.swift
        │       ├── URLExecutor.swift
        │       ├── KeyboardExecutor.swift
        │       ├── NotificationExecutor.swift
        │       └── PingExecutor.swift
        ├── Permissions/
        │   └── AccessibilityCheck.swift
        └── Audit/
            └── AuditLog.swift
```

Subfolders `autonomous-buddy/windows/` and `autonomous-buddy/linux/` will host future ports (v1.2+). Each platform self-contained so toolchains don't cross-contaminate.

### Go (`lamp/`)
```
system/buddy/
├── types.go
├── store.go
├── pairing.go
├── registry.go
├── ws.go
├── dispatcher.go
├── service.go
└── wire.go

system/server/buddy/delivery/http/
├── handler.go
├── handler_pair.go
├── handler_ws.go
└── handler_command.go
```

Modified:
- `system/server/server.go` (route registration)
- `system/server/wire.go` (provider set)
- `system/server/wire_gen.go` (regenerated)

### Web (`lamp/web/`)
```
lamp/web/src/
├── pages/PairedComputers.tsx (new)
├── App.tsx (modified — add route)
└── lib/api.ts (modified — add buddy endpoints)
```

### OpenClaw skill
```
<openclaw-skills-dir>/computer-use/
├── SKILL.md
└── script.sh (or equivalent)
```

### Other
- `CLAUDE.md` — doc table row added
- `Makefile` — `build-buddy` target
- `VERSION_BUDDY` (root) — `0.0.1`

---

## End-to-end acceptance test

1. Mac boots, user starts `autonomous-buddy.app` (or `swift run` for dev)
2. Lamp is running on LAN
3. Buddy menu shows `lamp-xxxx.local` discovered
4. User clicks "Pair with device" → device web UI displays 6-digit code
5. User types code into buddy → "Paired ✓"
6. Buddy menu shows "Connected to lamp-xxxx" with green dot
7. User says to lamp: "Mở Chrome trên máy tính của tôi"
8. Lamp dispatches command via WS
9. Chrome launches on Mac
10. Lamp speaks: "Đã mở Chrome trên máy bạn rồi"
11. User says: "Vào Gmail" → Chrome navigates to gmail.com
12. User says: "Đóng Chrome" → Chrome quits
13. User opens buddy menu → "Pause" → next command from lamp returns "máy tính tạm dừng"
14. User "Resume" → next command works again
15. User from lamp web UI → "Revoke" → buddy gets 401 → menu shows "Unpaired"

---

## Things to confirm with Leo before starting

- [x] **Mac-only MVP** — confirmed
- [x] **Intent-based (A), not vision** — confirmed
- [x] **Build from scratch** (not fork Open Interpreter / Computer Use demo) — confirmed
- [x] **No code signing for MVP** — right-click → Open OK — confirmed
- [ ] **Pairing model** — 1 lamp ↔ 1 buddy (MVP). Confirm? (Leo's reply implied yes, but worth confirming)
- [ ] **"Join Google Meet" — fixed URL or remembered last?** — for MVP, suggest a configurable URL in buddy preferences (so user can set their team's recurring meeting room)
- [ ] **OpenClaw skill directory location** — need to look up where existing skills live in this repo
- [ ] **Versioning** — should `VERSION_BUDDY` follow same scheme as `VERSION_OS_SERVER`?

---

## Risks specific to MVP

1. **mDNS service publishing** — ✓ Resolved. The device publishes `_autonomous._tcp` via `/etc/avahi/services/autonomous.service` (dropped at provisioning), so buddy can browse without manual entry.
2. **OpenClaw skill conventions** — unknown until inspected. May affect phase 1G design.
3. **Permission UX on first launch** — Accessibility prompt is one-shot; if user denies and we don't re-prompt cleanly, keyboard actions silently fail. Need fallback UX.
4. **WS keepalive across Mac sleep** — Mac sleep kills WS. Reconnect must handle gracefully.
5. **Bundling** — `swift run` works for dev but for production install we eventually need a `.app` bundle with `Info.plist`. Can defer but document the gap.
