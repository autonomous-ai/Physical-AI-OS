# Autonomous Buddy — Remote Computer Use for the device

> **Status:** Design — MVP scoping in progress
> **Last updated:** 2026-05-21
> **Owner:** Leo
> **Related:** [Autonomous Buddy MVP plan](./autonomous-buddy-mvp.md)

This document captures the full design discussion behind the **Autonomous Buddy** feature: a native companion app on the user's computer that lets the device control the desktop (open apps, navigate browser, type, etc.) — TeamViewer-style but driven by voice/AI through the device.

The MVP-only implementation plan lives in [`autonomous-buddy-mvp.md`](./autonomous-buddy-mvp.md). This doc is the long-form reference for *why* the architecture is what it is.

### One app: agent management and computer use (September 2026)

[Autonomous Buddy](./agent-manager.md) now packages the Electron/React workspace and Swift native helper in one `Autonomous Buddy.app`. Electron owns projects, worktrees, sessions, terminals and Git review; Swift owns pairing, the device WebSocket and computer-use executors. The embedded helper retains the native menu-bar icon without a second Dock icon, uses private JSONL child-process pipes, and exits when Electron closes its input pipe. Closing the workspace keeps Buddy running; the menu bar can reopen Agent Manager or quit the entire app. **Computer & device** in the main app exposes pairing, status, pause, permission management and Activity. `make build` / `make install` build and install both components together; users install one app. Paired-device `agent.*` commands route into managed sessions with explicit project/session IDs, request receipts and bounded event history; completion/attention notices return over the same WebSocket. See the [native bridge contract](./native-bridge.md). Live lamp verification remains separate from mock transport tests.

`make dmg` builds separate Apple Silicon (`arm64`) and Intel (`x64`) installers by default. OTA metadata stores a version and download URL per architecture; Buddy has no in-app updater yet. The upload target signs and notarizes both installers by default; local `make dmg` does not notarize. See [release signing and uploads](./release-signing.md) for architecture selection, signing and publication.

The [desktop Settings guide](./settings.md) covers the macOS Settings / ⌘, entry, searchable Appearance controls, persistent theme/font/zoom preferences, terminal styling and footer visibility.

The May design below is historical computer-use context, not the current packaging or UI contract. Its Swift-only/menu-bar decision is superseded by this single-app architecture. See the [agent-manager contract](./agent-manager.md) for current IPC and lifecycle behavior.

---

## 1. Goals & non-goals

### Goals
- The device can drive a user's computer via voice commands ("open Chrome", "go to Gmail", "join Google Meet", "type X", "close Slack")
- Works across any macOS app (not just browser)
- Computer-use commands and pairing confirmation stay on LAN; authorized MQTT clients can request a pairing code, revoke pairing, and query or observe Buddy status.
- Mac-first MVP; Windows/Linux deferred to v1.2+

### Non-goals (MVP)
- Vision-based control (Claude Computer Use API screenshot → click loop)
- Real-time screen streaming back to the device
- Windows / Linux support
- Multi-buddy per device (family / multiple devices)
- Code-signed / notarized binary — right-click → Open is acceptable for self-hosted MVP
- AppleScript executor beyond bundled simple cases
- Arbitrary shell command execution (too dangerous, defer behind explicit flag)

---

## 2. Use cases (MVP)

| Voice command (example) | Action chain |
|-------------------------|--------------|
| "Mở Chrome và vào Gmail" | `open_app(Google Chrome)` → `open_url(https://gmail.com)` |
| "Tự động join Google Meet" | `open_url(https://meet.google.com/<last-or-configured-link>)` |
| "Mở Spotify" | `open_app(Spotify)` |
| "Gõ 'hello world' vào ô đang focus" | `type_text("hello world")` |
| "Đóng Slack" | `close_app(Slack)` |
| "Bật chế độ Do Not Disturb" | `applescript(...)` (whitelisted) |
| "Hiện noti 'meeting in 5 min'" | `notification(title, body)` |

Out-of-scope MVP examples (defer to vision phase):
- "Đọc email mới nhất rồi tóm tắt cho tôi" (requires screen reading)
- "Tìm trên Google nội dung … rồi click vào kết quả đầu" (requires click-by-vision)
- "Highlight đoạn này rồi copy" (requires selection state awareness)

---

## 3. Architecture

```
┌─────────────────────────┐         ┌────────────────────────────┐
│  User                   │         │  Mac (user's computer)     │
│    │ voice              │         │  ┌──────────────────────┐  │
│    ▼                    │         │  │ autonomous-buddy.app │  │
│  ┌─────────────────┐    │         │  │ (Swift, menu bar)    │  │
│  │ Lamp (Pi)       │    │         │  │                      │  │
│  │                 │    │         │  │  ┌────────────────┐  │  │
│  │  lelamp (Py)    │    │         │  │  │ Pairing & WS   │  │  │
│  │    └─ STT/TTS   │    │ ◀──WS───┼──┼──┤ client         │  │  │
│  │                 │    │         │  │  └────────────────┘  │  │
│  │  lamp (Go)      │    │         │  │  ┌────────────────┐  │  │
│  │    ├─ OpenClaw  │    │         │  │  │ Executors      │  │  │
│  │    │   └─ skill │    │         │  │  │  NSWorkspace   │  │  │
│  │    │   "compu-  │    │         │  │  │  CGEvent       │  │  │
│  │    │    ter-use"│    │         │  │  │  AppleScript   │  │  │
│  │    └─ buddy svc │    │         │  │  └────────────────┘  │  │
│  └─────────────────┘    │         │  └──────────────────────┘  │
│         ▲               │         │            │               │
│         │ mDNS          │         │            ▼               │
│         │ lamp-xxxx     │         │   macOS apps (Chrome,      │
│         │ .local        │         │   Finder, Spotify, …)      │
└─────────────────────────┘         └────────────────────────────┘
```

### Happy-path flow

1. User speaks: "Mở Chrome trên máy tính và vào Gmail"
2. Mic on lelamp → STT
3. Transcript → OpenClaw → matches skill `computer-use`
4. Skill parses to commands and calls Lamp Go:
   `POST /api/buddy/command {action:"open_app", params:{app:"Google Chrome"}}`
   `POST /api/buddy/command {action:"open_url", params:{url:"https://gmail.com"}}`
5. The device's Go buddy dispatcher looks up the paired buddy in the WS registry, forwards command over the open WS
6. autonomous-buddy decodes JSON, dispatches to executor (NSWorkspace, CGEvent, etc.), executes
7. Buddy returns `{ok:true}` over the same WS
8. Skill receives result, returns TTS-friendly confirmation
9. lelamp speaks: "Đã mở Gmail trên máy của bạn rồi"

### Connection direction: buddy → device

**Buddy is the WS client. The device is the WS server.** Reasons:

- Buddy does not need to open any port. The Mac firewall stays untouched. Lower attack surface.
- The device already has a stable mDNS hostname (`<device_type>-xxxx.local`, e.g. `lamp-a1b2.local`). Buddy resolves and connects.
- Single persistent WS → command latency = 1 round-trip (no TCP/TLS cold-start per command).
- Reconnect logic lives in buddy (simpler — buddy can detect device reboots and re-connect after).

**Alternative considered:** the device connects to buddy. Rejected because:
- Requires opening port on Mac (firewall prompt every time)
- Buddy must be mDNS-discoverable from the device (extra advertise)
- The device's HTTP layer is already the integration surface; this keeps things consistent

---

## 4. Components

### 4.1 `autonomous-buddy` (Swift macOS app)

- `NSStatusItem` in menu bar (no Dock icon; `setActivationPolicy(.accessory)`)
- Menu: pairing state, connection state, last command, "Pause", "Revoke pairing", "Quit"
- mDNS browser via `Network.framework` (`NWBrowser`)
- Pairing flow (6-digit code; token stored in `~/Library/Application Support/AutonomousBuddy/pairing.json`, mode 0600)
- Persistent `URLSessionWebSocketTask` to lamp
- Command executors:
  - **NSWorkspace** for app launch, URL open
  - **CGEvent** for keystrokes & key combos
  - **AppleScript / OSAScript** for "close app" and limited whitelisted scripts
  - **UNUserNotificationCenter** for desktop notifications
- Permission helpers (Accessibility prompt, Automation per-app prompts)
- Local audit log (file in `~/Library/Application Support/AutonomousBuddy/audit.log`); there is no device-side audit endpoint
- OSLog (unified logging) for diagnostics

### 4.2 `lamp` Go server — new package `system/buddy/`

| File | Responsibility |
|------|----------------|
| `types.go` | Command / response schemas, pairing types |
| `store.go` | Persistent storage of paired buddies (token, fingerprint, name) — likely in `config/buddies.json` |
| `pairing.go` | Code generation, validation, token issuance |
| `registry.go` | In-memory registry of currently-connected buddies (WS handles) |
| `ws.go` | WS upgrade handler, auth, per-buddy message loop |
| `dispatcher.go` | `Dispatch(buddyID, cmd) → response` — request/reply over WS with timeout |
| `service.go` | High-level service tying the above together |
| `wire.go` | Google Wire provider set |

HTTP routes (registered in `system/server/server.go`, handlers in `system/server/buddy/delivery/http/`). The device holds a single pairing:

| Route | Auth | Purpose |
|-------|------|---------|
| `POST /api/buddy/pair/start` | admin (`adminAuthMiddleware`) | Issue 6-digit code, valid 60s → `{code, expires_in}` |
| `POST /api/buddy/pair/confirm` | none (the code is the credential) | Confirm code → `{token, buddy_id}` |
| `GET  /api/buddy/status` | admin | `{paired, connected}` plus `buddy_id`, `name`, `os_version`, `fingerprint`, `paired_at` when paired |
| `DELETE /api/buddy` | admin | Revoke the current pairing |
| `DELETE /api/buddy/self` | buddy Bearer token (checked in handler) | Buddy-initiated unpair |
| `GET  /api/buddy/ws` | buddy Bearer token (checked in handler) | WS upgrade for buddy |
| `POST /api/buddy/command` | loopback only (`localOnlyMiddleware`) | Dispatch one command synchronously (default 30 s; `timeout_ms` + 5 s when given) |
| `POST /api/buddy/exec/:action` | loopback only | HW-marker variant (`[HW:/buddy/exec/<action>:{...}]`), 15 s |
| `POST /api/buddy/observe` | loopback only | Screenshot + auxiliary vision-model description |
| `POST /api/buddy/suggest` | loopback only | Suggestion endpoint used by device-side agents |

Pairing codes remain single-use with a 60-second TTL and are invalidated after five wrong submissions. The anonymous confirmation endpoint permits at most ten requests per minute across all clients (including malformed requests), returning HTTP 429 with `Retry-After: 60` when exhausted. Issuing a new code requires authorization and does not reset this request budget. No IP or forwarded header is trusted to bypass the limit. This device-wide budget also temporarily delays legitimate confirmations during a flood; wait 60 seconds and request a fresh code if needed.

### 4.3 `lelamp` (Python) — **no changes for MVP**

Hardware-only per `feedback_lelamp_external.md`. STT → OpenClaw, OpenClaw → TTS already work. The buddy flow only touches OpenClaw's skill layer, which lives in OpenClaw's skill directory, not in lelamp Python source.

### 4.4 OpenClaw skill: `computer-use`

Current skill behavior (all runtimes): trusted current unpaired, disconnected, or paused status stops desktop work before any command or vision-reference read, including simple HW markers. If status is unknown, the skill performs one read-only `desktop_info` check and consumes its full result and exit code. Successful preflight is reused within the workflow. Connection/permission failures and timeouts stop the turn without retries, automatic pairing/reconnection, or fallback to Harness/device-local browsing. The response briefly states the actual blocker without promising automatic completion. An explicit retry or new trusted connection update permits a fresh check. These are skill instructions, not a backend execution gate; no new status injection is added.

- Lives in `skills/computer-use/`
- `SKILL.md` describes triggers and tool surface
- Trigger patterns include Vietnamese ("mở ... trên máy tính", "vào trang ... trên máy", "đóng app ...") and English ("open ... on my computer", "go to ... on my mac")
- Skill script (`scripts/buddy.py`) or HW marker posts to `http://127.0.0.1:5000/api/buddy/command` (or `/api/buddy/exec/<action>`); these routes are loopback-only, with no auth header
- Returns TTS-friendly result string ("đã mở Chrome rồi", "không tìm thấy máy tính đã pair", etc.)

### 4.5 Web UI (`system/web/`)

`BuddyCard` in the Monitor page's pairing section (`system/web/src/pages/monitor/BuddyCard.tsx`):
- Shows the single paired Mac (name, OS, buddy ID, connection status)
- "Pair" button → calls `POST /api/buddy/pair/start` → displays 6-digit code; polls `GET /api/buddy/status` every 5 s to detect a successful pair
- "Revoke" button → `DELETE /api/buddy`

---

## 5. Command schema v1

### Request (lamp → buddy)

```json
{
  "id": "uuid-v4",
  "action": "open_app",
  "params": { "app": "Google Chrome" },
  "timeout_ms": 5000,
  "issued_at": "2026-05-21T10:00:00Z",
  "issued_by": "computer-use:openclaw"
}
```

### Response (buddy → lamp)

```json
{
  "id": "uuid-v4",
  "ok": true,
  "result": { "pid": 1234 },
  "error": null,
  "duration_ms": 412
}
```

### MVP actions

| Action | Params | Executor |
|--------|--------|----------|
| `open_app` | `{app: string}` | `NSWorkspace.shared.launchApplication` |
| `close_app` | `{app: string, force?: bool}` | AppleScript `tell application "X" to quit` |
| `open_url` | `{url: string, browser?: "default"\|"chrome"\|"safari"}` | `NSWorkspace.shared.open(URL)` |
| `type_text` | `{text: string, delay_ms?: int}` | `CGEventKeyboardSetUnicodeString` |
| `key_combo` | `{keys: ["cmd","n"]}` | `CGEventCreateKeyboardEvent` with modifier flags |
| `notification` | `{title: string, body?: string}` | `UNUserNotificationCenter` |
| `ping` | `{}` | health-check, returns `{ok:true}` |

Reserved for later (defined but not implemented MVP):
- `applescript` (whitelisted only)
- `screenshot` (vision phase)
- `click_at` (vision phase)
- `read_clipboard` / `write_clipboard`
- `focus_window` / `bring_to_front`

---

## 6. Discovery & pairing

### Discovery (each buddy startup)

- Buddy browses `_autonomous._tcp.local` via `NWBrowser`
- For each found service, resolve hostname → store in candidate list
- MVP: assume single device on LAN → auto-pick the first
- Fallback: manual hostname entry (`<device_type>-xxxx.local`) in menu

### Pairing (one-time)

1. User requests a code from the device web UI via admin-authenticated `POST /api/buddy/pair/start`, or the authorized backend sends `{"cmd":"data","kind":"buddy.pair.start","data":{}}` on `fa_channel`.
2. The device returns a 6-digit code. MQTT returns `{"type":"data","kind":"buddy.pair.start","status":"success","data":{"code":"123456","expires_in":60}}` on `fd_channel`, with the standard `MQTTDataResponse` metadata. Request `data` is optional and ignored.
3. HTTP and MQTT use the same Buddy service: the single-use code stays in memory for 60s; issuing another code through either transport replaces the pending code.
4. User reads the code from the requesting UI and opens "Pair with device" in Buddy.
5. User types code into buddy
6. Buddy calls `POST /api/buddy/pair/confirm {code, name, fingerprint, os_version}` over LAN.
7. The device validates code, generates a long-lived bearer token, and persists `{buddy_id, token, fingerprint, name, os_version, paired_at}` in `config/buddies.json`.
8. Buddy stores `{buddy_id, device_host, token, paired_at}` in `~/Library/Application Support/AutonomousBuddy/pairing.json` (file mode 0600, `Pairing/PairingStore.swift`). The Electron desktop app does not store the token itself: pairing is delegated to its embedded Swift helper, which uses the same file.
9. Buddy opens WS with `Authorization: Bearer <token>`

MQTT authorization relies on existing broker credentials and topic ACLs; the
backend must authorize the device owner before sending `buddy.pair.start` or
`buddy.pair.revoke`. Confirmation remains HTTP; status is also available through
MQTT `buddy.status`. Authorized mobile clients can use the broker directly.
See [MQTT pairing contract](../../../../docs/mqtt.md#buddypairstart--issue-a-buddy-pairing-code).

To revoke the current pairing, the authorized backend sends
`{"cmd":"data","kind":"buddy.pair.revoke","data":{}}` on `fa_channel` (`data` is
optional and ignored). This calls `buddy.Service.Unpair`, also used by
`DELETE /api/buddy`: it closes the active WebSocket, clears the current pairing
and persisted store, and invalidates the paired token. Repeating it when already
unpaired succeeds; a pending pairing code is not cancelled. The response on
`fd_channel` is `{"type":"data","kind":"buddy.pair.revoke","status":"success","data":{"revoked":true}}`
with standard `MQTTDataResponse` metadata. Failures, including an unavailable
service or persistence failure, use `status:"failure"` and `error`.
See [MQTT revocation contract](../../../../docs/mqtt.md#buddypairrevoke--revoke-buddy-pairing).

### Mobile status over MQTT

Send `{"cmd":"data","kind":"buddy.status","data":{}}` on `fa_channel`.
The response and unsolicited change events use `type:"data"`,
`kind:"buddy.status"`, `status:"success"`, with `data` containing `paired`,
`connected`, `instance_id`, `revision`, and optional `buddy_id`, `name`,
`os_version`, `paired_at` (RFC3339). Unpaired state omits the paired Mac fields;
empty optional strings are omitted. Secrets, pairing codes, and fingerprints are
never included. `paired` and `connected` are independent: a paused/offline Mac
can remain paired.

Notifications include startup state, successful HTTP confirmation, HTTP/MQTT
revocation (including Buddy self-revocation), and current WebSocket connection
changes. Revision starts at 0 and increases for successful pair/revoke/connect/
current-disconnect operations. Compare revisions only within the same
`instance_id`, which changes on service restart; ignore duplicate/older revisions.
Delivery is asynchronous, bounded, coalesces to latest state, and uses QoS 1
without retain. Failed publishes are logged/dropped, so subscribe to FD and wait
for SUBACK before querying on entry, reconnect, and resume. Query failure uses
`status:"failure"` and `error`. Status can arrive before a command response.

Mobile can connect directly using existing per-device broker configuration and
a unique app client ID, with no BFF code changes. Broker reachability and topic
ACLs still need deployment verification. This covers foreground updates, not
push notifications after the app closes. See the
[full MQTT status contract](../../../../docs/mqtt.md#buddystatus--query-and-observe-buddy-state)
and [mobile handoff prompt](../../../../docs/vi/buddy-mobile-handoff_vi.md).


### Reconnect

- Buddy keeps WS open with periodic ping (every 15s)
- On disconnect: exponential backoff (1s, 2s, 4s, … capped at 30s)
- On device reboot: the device sees old token in `buddies.json` and accepts WS immediately (token persistent)
- On revoke: the device removes from `buddies.json` and rejects with HTTP 401 → buddy detects, drops session, prompts re-pair

---

## 7. Security model

| Layer | Mechanism |
|-------|-----------|
| Pairing | Requires explicit user action on Mac (typing the code). Cannot be initiated silently. |
| Token storage | `~/Library/Application Support/AutonomousBuddy/pairing.json` (file mode 0600, not encrypted; Keychain was dropped because ad-hoc-signed builds hit an unapprovable password prompt) on buddy side; `config/buddies.json` on lamp (file mode 0600) |
| Transport | WS over HTTP on LAN (MVP). **TLS deferred** — documented risk, mitigated by LAN-only and pairing requirement. v1.1: self-signed cert + pinning. |
| Session indicator | Red dot in menu bar when buddy is connected and active. Always-visible. |
| Audit log | Every command logged (timestamp, action, params hash, source). Local file only (`~/Library/Application Support/AutonomousBuddy/audit.log`). |
| Kill switch | "Pause" in menu = drop WS but keep token. "Revoke pairing" = drop token + tell lamp to remove. |
| Permission gating | Commands fail clean with descriptive error if macOS permission denied (no silent failures). |
| Blast radius | **Documented explicitly to user**: Lamp gets the same access level as the user account on the Mac. Trust ask is the central UX concern. |
| Rate limiting | Lamp side: max N commands/sec/buddy. Prevents runaway loops. |

### Threats considered

1. **Malicious LAN attacker** → cannot pair without a code from the authorized HTTP or MQTT flow. Cannot replay token without reading the user's `pairing.json` (0600).
2. **Compromised lamp** → can run arbitrary commands on Mac (= blast radius). Mitigation: user can revoke at any time from menu bar without needing lamp access.
3. **Compromised buddy** (malware on Mac that hijacks the WS) → could send fake responses to lamp. Mitigation: command IDs + signed responses (v1.1+).
4. **Eavesdropping on LAN** → MVP doesn't encrypt WS. Acceptable for home LAN, must fix before any non-trusted-network deployment.

---

## 8. macOS permissions

| Permission | Why | When triggered | If denied |
|------------|-----|----------------|-----------|
| Accessibility | `CGEvent` input injection (type_text, key_combo) | First `type_text` / `key_combo` command | Command returns error; menu bar shows "⚠ Accessibility needed" |
| Automation (per-app) | AppleScript control of specific app (close_app) | First close_app on each target app | Command returns error |
| Notifications | Showing buddy status & noti commands | First app launch (request permission immediately) | `notification` action fails; status indicator still works |

**No Screen Recording permission** in MVP (no screenshots).

---

## 9. Future roadmap

| Version | Feature | Notes |
|---------|---------|-------|
| v1.1 | Vision fallback via Claude Computer Use | For commands without intent match |
| v1.1 | TLS for WS | Self-signed + cert pinning |
| v1.2 | Windows port | Tauri/Rust most likely |
| v1.3 | Linux port (X11 first) | Wayland deferred |
| v1.4 | Multi-buddy per lamp | Family scenario |
| v1.5 | Screen Recording + on-demand screenshot | Lamp can request frame |
| v2.0 | Code signing + notarization + Sparkle | Real distribution |
| v2.0 | App-specific helpers | First-party integrations: Chrome extension, Slack bot, etc. |

---

## 10. Risks & open questions

1. **Trust** — central UX concern. Need clear session indicators, audit log access, easy kill switch.
2. **macOS API churn** — Accessibility / AppleScript APIs shift between major macOS versions. Need to pin minimum OS and test against new versions on release.
3. **Latency** — voice → STT → OpenClaw → intent → WS → buddy → action ≈ 2–4s end-to-end. Will feel slow vs Siri. Mitigation: TTS feedback ("ok, đang mở Chrome…") to cover latency.
4. **Distribution without notarization** — first-launch requires right-click → Open. Acceptable for MVP. Plan signing at v2.0.
5. **Command ambiguity** — "mở app" without target needs a clarifying TTS turn. Skill design must handle this.
6. **Mac sleep / login lock** — buddy can't drive Mac if screen is locked. Define behavior: queue + retry? error out? For MVP: error out with clear message.
7. **Multiple Macs on same LAN** — multiple buddies, single lamp. MVP allows only one paired buddy. Document this.
8. **Lamp on different network** — buddy can't connect. Defer until VPN/relay phase.

---

## 11. Tech stack decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Buddy language | **Swift (native)** | Best Mac API surface (CGEvent, AppleScript, NSWorkspace all native); smallest binary; no plugin layer |
| Project format | **Swift Package Manager** initially | No Xcode dependency; faster iteration. Migrate to Xcode `.xcodeproj` when `.app` bundle + signing is needed |
| Min macOS | **macOS 13 (Ventura)** | `NWBrowser` modern API, `Network.framework` mature; older versions need backports |
| Network protocol | **WebSocket (raw, no TLS in MVP)** | Persistent, bi-directional, well-supported by URLSessionWebSocketTask. TLS in v1.1 |
| Discovery | **mDNS via `Network.framework` `NWBrowser`** | Native, no third-party deps |
| Command codec | **JSON** | Matches lamp's existing serializer style; easy debug |
| Token storage | **File, mode 0600** (`pairing.json`) | Keychain prompted unapprovably for ad-hoc-signed builds; revisit once Developer ID signed |
| Pref storage | **UserDefaults / plist** | Standard macOS for non-secrets |
| Logging | **OSLog (unified)** | Native, viewable in Console.app |
| Test framework | **XCTest** | Standard |
| Go side | **Existing patterns** (Gin, Wire, internal/) | Follow `runtimes/openclaw/`, `server/<domain>/delivery/http/` |
| Autonomous-buddy folder location | **Repo root** alongside `lamp/`, `lelamp/` | Self-contained, can be built independently |

---

## 12. Discussion log (2026-05-21)

### Initial brief from Leo

> Mình muốn làm 1 chức năng remote computer use. Tưởng tượng Lamp sẽ remote vào MacBook của mình, điều khiển máy tính, app, browser Chrome … giống cách supporter qua TeamViewer điều khiển máy của mình.
>
> Máy MacBook cần chạy 1 software lamp-buddy trên top status bar, sau đó quét xem có Lamp đang chạy LAN IP gì đó, thì accept cho Lamp được connect và computer use máy MacBook.
>
> VD mình nói "mở Chrome trên máy tính và vào Gmail lấy mail ra", hay "tự động join Google Meet" … cần code thêm 1 folder riêng `lamp-buddy` (chưa rõ dùng ngôn ngữ gì swift hay flutter …). Sau đó install vào Mac/Windows/Linux. Software này paring với Lamp và cho phép Lamp điều khiển.

### Approach options considered

**1. Use existing protocol (VNC, RustDesk, TeamViewer)**
- Pro: zero implementation
- Con: those are human-controller-focused, not AI/tool-API-friendly. Lamp would still need to "see screen + click/type" through them, which is essentially building a TeamViewer client inside Lamp.
- **Rejected.**

**2. macOS built-in Screen Sharing / VNC**
- Pro: zero install on Mac
- Con: same as above — designed for human controller. Adding Lamp vision-action loop on top is awkward.
- **Rejected for MVP.**

**3. Custom Mac Companion (chosen)**
- Pro: native API access, smallest latency, intent-level commands without vision-loop overhead, can integrate deeply with macOS (Spotlight, Shortcuts, etc.)
- Con: must build & maintain. Per-platform code (eventually).
- **Chosen.**

### Decision: intent-based vs vision-loop

**Intent A (chosen):** parse voice intent → map to structured command (`open_app`, `type_text`) → buddy executes locally without screenshots.
- Latency: <500ms per command
- Cost: 0 extra LLM calls (just OpenClaw's existing intent parsing)
- Robustness: brittle to unusual requests
- Coverage: 80% of stated use cases (open Chrome, go to Gmail, join Meet, type, close app)

**Vision B (deferred to v1.1):** screenshot → Claude Computer Use API → click(x,y) → repeat.
- Latency: 3–10s per turn
- Cost: every screenshot is a large message (expensive)
- Robustness: bend doesn't break — Claude can see and adapt
- Coverage: anything visible on screen

**Hybrid C (deferred to v1.1):** A by default, B fallback for unrecognized commands.

### Decision: where does the LLM run?

**Option a (chosen):** OpenClaw (on lamp) does intent parsing. Buddy is dumb executor receiving structured commands.

**Option b (deferred):** Buddy hosts its own LLM for complex tasks ("read latest email and summarize"). Defer until Vision B needed.

### Decision: language

The original computer-use MVP chose **Swift native**. The September architecture retains Swift for native execution and embeds it inside the Electron/React [agent manager](./agent-manager.md), forming one installed app. The earlier rejection of Electron as the whole product no longer applies. Windows/Linux native helpers remain future work.

### Decision: connection direction

**Buddy → lamp** (chosen) — see §3 for rationale. Buddy is WS client, lamp is WS server.

### Decision: distribution (MVP)

**No code signing** — user does right-click → Open on first launch. Apple Developer account ($99/year) deferred until v2.0.

### Decision on `lelamp` and `lamp` Go

- `lelamp` (Python) — **no changes**. Hardware-only.
- `lamp` (Go) — **new package** `system/buddy/`, new HTTP routes, new WS gateway.
- `OpenClaw` — new skill `computer-use`.
- `system/web` — `BuddyCard` in the Monitor page (pair / status / revoke).
- `CLAUDE.md` — new doc row.

---

## 13. References

- mDNS hostname — the device publishes `<device_type>-<last4hex>.local` (e.g. `lamp-a1b2.local`)
- `feedback_lelamp_external.md` — hardware code lives in Python, not Go
- `project_security_login_ui_batch.md` — recent security audit closed; cookie HMAC + bcrypt admin patterns to reuse for buddy auth
- [Anthropic Computer Use docs](https://docs.anthropic.com/en/docs/build-with-claude/computer-use) — for vision phase v1.1
- [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) — for screenshot capability v1.5
- [Apple Accessibility API](https://developer.apple.com/documentation/applicationsystem/axuielement_h) — for window/button targeting
