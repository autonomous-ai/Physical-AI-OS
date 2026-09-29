# Status LED — Specification

Status LEDs give users visual feedback about what Lamp is doing internally.
Without them, users cannot tell whether Lamp is booting, updating, disconnected from its AI brain, or otherwise impaired.

## Design Principles

1. **Glanceable** — each state has a unique color so the user never has to guess.
2. **Non-intrusive** — status LEDs yield to user-initiated scenes/emotions. When the status clears, the strip is restored to whatever the user (or agent) last set, and ambient resumes after silence.
3. **Priority-based** — when multiple states are active simultaneously, the highest-priority state wins.

## States

Effects come from the base table: most states use `breathing`, but `error` uses `pulse` (speed 1.5) and `wifi_connecting` uses `blink` (speed 0.5). The base table runs the breathing cues at speed 3.0, but on lamp the six long-lived cues — `ota`, `booting`, `connectivity`, `hal_down`, `agent_down` and `hardware` — breathe at speed 0.6 via the overlay. RGB values are the base `STATUS_LED_PRESETS` in `hal/presets.py` (the Go side sends only the state *name*) as overridden by the `status_led` block in `robots/lamp/presets.json`, the per-device overlay merged at boot by `hal/board/presets_overlay.py`. The overlay patches `color` and, for those six cues, `speed`; the effect name still comes from the base table, and other robots keep the base values. They are tuned to a low, hue-equalised luminance so a cue that stays lit does not glare.

| State (code constant) | Color | RGB | Meaning | Triggered by | Auto-clears |
|---|---|---|---|---|---|
| `StateConnectivity` | Orange | `(3, 1, 0)` | **No Internet** — Wi-Fi connected but no internet | Network monitor: 5 consecutive ping failures (~25s) | Yes — when ping succeeds |
| `StateError` | Red (`pulse`) | `(3, 0, 0)` | **Error** — System error (reserved; no caller sets it today) | — | — |
| `StateOTA` | Green | `(0, 3, 0)` | **Updating** (reserved enum; no caller sets it — bootstrap shows OTA with the `ota_progress` / `ota_success` / `ota_error` presets instead, see "OTA sub-states" below) | — | — |
| `StateWifiConnecting` | Blue (`blink`, speed 0.5) | `(0, 1, 3)` | **Joining Wi-Fi** — wlan0 is associating during `POST /api/device/setup` | `system/device/setup.go` (Wi-Fi setup paths only; a wired setup never sets it) | Yes — deferred `Clear` when the setup step returns |
| `StateBooting` | Blue | `(0, 1, 3)` | **Booting** — Lamp is starting up | `system/server/server.go` on startup | Yes — when the agent connects and is ready |
| `StateHALDown` | Purple | `(2, 0, 3)` | **HAL Down** — Hardware server unreachable. While HAL is down the LED is **dark** because the LED driver itself is down; the purple breathing only shows for ~3s on recovery | `healthwatch` poll fails to reach HAL `/health` | Auto-clears 3s after recovery |
| `StateAgentDown` | Cyan | `(0, 3, 3)` | **Agent Down** — AI brain disconnected | Agent runtime connection drops (`runtimes/openclaw/service_ws.go`, `runtimes/hermes/health.go`, and `client.go` in `runtimes/{picoclaw,codex,claudecode,opencode}`) | Yes — when WebSocket reconnects |
| `StateHardware` | Yellow | `(3, 3, 0)` | **Hardware Failure** — servo/LED/audio/voice component reports unhealthy via HAL `/health` | `healthwatch` poll (every 5s); camera and sensing excluded | Yes — when all monitored components report healthy |

### Brightness (lamp overlay)

For the current low-glare device test, every RGB channel in `status_led` is capped at 3. Alert recognition and breathing smoothness must be verified on the lamp.

The lamp levels were tuned by eye on lamp-0c89 on 24/08/2026, in three passes. Pass 1 halved every cue from the base peaks (12 for green-dominant cues, 16 for low-green ones), which were still reported as too bright in a dim room. Pass 2 dropped the green tier again, 6 → 4: green at 6 was still glaring while red at 8 read fine, because the WS2812's green die outruns its red at equal value by more than the base 12-vs-16 rule allows for. Pass 3 took the low-green tier 8 → 5. Every step scales all three channels proportionally, so hue never moves and only luminance drops. The other half of the fix was rhythm, not level: these cues ran at speed 3.0, the fastest in the file, and long-lived fast breathing reads as pulsing rather than as light — the same thing that fixed `listening` on 21/08, where lowering the color alone had already failed — hence speed 0.6. `setup` came down 16 -> `(3, 3, 3)` on 25/08/2026, checked by eye at 8, then 5, then 3. It had sat out all three passes above, and it ended up BELOW the other cues rather than above them because it is the only `solid` one: it lights all 32 pixels at once, so its total flux is far higher than its peak suggests — the same trap documented for the scene table, where judging a full-ring look by its peak came out too bright. Peak 3 still reads across a room because 32 lit pixels is a large source, which is what onboarding needs. The per-frame truncation floor below does not apply here either, since `solid` never scales per frame. `mic_muted` was deliberately left alone: it stays readable in a daylit room because it is a privacy indicator. Note the base file's stated floor of peak ~8: below it, `breathing`/`pulse` truncate per frame (`int(c * brightness)` in `hal/drivers/rgb/effects.py`) so the cycle has few distinct levels and the strip can visibly step, which is what to check by eye on device. With the cap at 3, `error` and `mic_muted` share the same color `(3, 0, 0)`; they are told apart by shape — `error` pulses, `mic_muted` breathes — which is the separation the base file already relies on.

### Ready flash

After boot completes (Booting cleared and no other state active), `statusled.FlashReady()` fires a brief **white** `(3, 3, 3)` `notification_flash` for ~1s to indicate the agent is ready to accept commands. Suppressed if any status state is active.

### OTA sub-states (driven by bootstrap)

The bootstrap binary calls `lib/hal` directly (it does not go through `statusled.Service`). `Bootstrap.progressLED(name)` sends a preset *name* via `hal.SetStatus` → `POST /led/status`, only on bodies with the `light` capability; HAL resolves the look from `STATUS_LED_PRESETS` plus the lamp overlay:

| Phase | Preset | LED behavior (lamp) | Source |
|---|---|---|---|
| Downloading + installing | `ota_progress` | Orange `(3, 1, 0)` `breathing` speed 0.4 | `system/bootstrap/bootstrap.go` |
| Success | `ota_success` | Green `(0, 3, 1)` `notification_flash`, then `RestoreLED` after 1 s | `system/bootstrap/bootstrap.go` |
| Failure | `ota_error` | Red `(3, 1, 1)` `pulse` speed 1.5, then `RestoreLED` after a short display | `system/bootstrap/bootstrap.go` |

These are separate presets from the reserved `ota` / `error` statusled states — bootstrap is a separate binary that owns the LED while OTA is in progress.

## Priority

When multiple `statusled.Service` states are active simultaneously, the highest-priority state is shown:

```
Connectivity (highest) > Error > OTA > Wi-Fi connecting > Booting > HAL Down > Agent Down > Hardware (lowest)
```

Priority numbers (from `priority` map in `system/statusled/service.go`):

| State | Priority |
|---|---|
| `StateConnectivity` | 8 (highest) |
| `StateError` | 7 |
| `StateOTA` | 6 |
| `StateWifiConnecting` | 5 |
| `StateBooting` | 4 |
| `StateHALDown` | 3 |
| `StateAgentDown` | 2 |
| `StateHardware` | 1 (lowest) |

`StateWifiConnecting` sits just above Booting so the setup blue blink outranks the boot state still active from server start.

Example: if Lamp has no internet AND the agent is down, **No Internet** (orange) wins because it has higher priority.

Bootstrap's OTA LED writes bypass this priority queue — they run while bootstrap owns the strip, typically when lamp is being restarted.

## Behavior Details

### Booting (Blue)
- Activated by `server.go` at startup, before the agent is ready
- Cleared when the agent connects and is ready to accept commands
- Followed by a brief white `FlashReady` flash (fired from `system/server/config_watch.go`) to signal "ready to listen"

### Connectivity / No Internet (Orange)
- Network service pings every 5 seconds
- After 5 consecutive failures (~25 seconds), `StateConnectivity` is set
- Cleared immediately when a ping succeeds
- Lamp continues to function locally but cloud features are unavailable

### Agent Down (Cyan)
- Activated when the active agent runtime's connection drops (OpenClaw WebSocket, Hermes health check, or the PicoClaw/Codex/Claude Code/OpenCode bridge client)
- Cleared when it reconnects successfully
- Voice commands and AI features are unavailable; local LED scenes and servo still work
- TTS announces "Brain reconnected!" on recovery

### HAL Down (Purple — or dark/black)
- When HAL crashes the LED goes **dark** because the LED driver itself is down
- `healthwatch` polls every 5 seconds and tracks the outage
- On recovery, purple breathing flashes for ~3s as the state clears, then normal LED resumes
- TTS announces "Hardware recovered!" on recovery
- LED control, servo, camera, mic, and speaker are all unavailable while HAL is down

### Hardware Failure (Yellow)
- Activated when servo, LED driver, audio, or voice pipeline reports unhealthy via HAL `/health`
- Per-servo online check via `hal.GetServoStatus()` — any offline servo trips it
- Camera and sensing are excluded (may be intentionally off by scene preset)
- Health watcher polls every 5 seconds
- Cleared automatically when all monitored components report healthy
- Check the web monitor for specific component details

### OTA Update (Orange / Green / Red — bootstrap)
- See "OTA sub-states (driven by bootstrap)" above
- Device reboots after a successful update — LED transitions to Booting (blue) on the new boot

### Error (Red — reserved)
- `StateError` enum is defined in `statusled.Service` but is not currently set by any caller in lamp
- Bootstrap uses the `ota_error` red `pulse` preset to indicate OTA failure (not via `statusled.Service`)

## Architecture

### Lamp (os-server)

`system/statusled/Service` manages active states with a priority map. Callers `Set` and `Clear` named states; the service applies the LED effect for the highest-priority active state.

Concrete callers (verified against code):

```
system/server/server.go             → Set/Clear StateBooting
system/server/config_watch.go       → Set/Clear StateConnectivity (network monitor callbacks) + FlashReady
system/device/setup.go              → Set/Clear StateWifiConnecting
runtimes/openclaw/service_ws.go,
runtimes/hermes/health.go,
runtimes/{picoclaw,codex,claudecode,opencode}/client.go → Set/Clear StateAgentDown
system/healthwatch/service.go       → Set/Clear StateHALDown + StateHardware
```

The service sends the highest-priority state *name* to HAL's `POST /led/status` via `hal.SetStatus` in `system/lib/hal` (shared HTTP client); HAL owns the color/effect/speed.

### Bootstrap (bootstrap-server)

Bootstrap is a separate binary. It calls `lib/hal` **directly** in the `reconcile` function (not through `statusled.Service`):

```
reconcile detects update → progressLED("ota_progress")   // orange breathing
        ↓ applies update...
success → progressLED("ota_success"); sleep 1 s; restoreLED()   // green flash
failure → showOTAErrorLED() → progressLED("ota_error"), restore scheduled   // red pulse
```

## Integration with Ambient

The ambient service (`system/ambient`) pauses on interaction events (`chat_send`, `chat_response`, etc.). When `statusled.Service` clears the last active state, it calls `hal.RestoreLED()`, which hands the strip back to whatever color/effect the user (or agent) last set via `/led/solid`, `/led/effect`, or `/scene`. If no user state exists, the strip clears to off and ambient resumes its breathing LED after 60s of silence.

All `statusled.Service` writes use `transient=true` so they do not clobber the user's saved LED state — emotion's restore-after-animation reads back the user's color, not the status color. (Bootstrap's direct `lib/hal` calls are also transient.)

## Shared HAL Client

`system/lib/hal/client.go` provides a thin HTTP wrapper used by all Go code that controls LEDs:

| Function | Endpoint | Purpose |
|---|---|---|
| `SetStatus(state)` | `POST /led/status` | Show a named status preset (HAL resolves color/effect/speed) |
| `SetEffect(effect, r, g, b, speed)` | `POST /led/effect` (transient) | Start a named effect — does not save user LED state |
| `StopEffect()` | `POST /led/effect/stop` | Stop running effect |
| `RestoreLED()` | `POST /led/restore` | Hand strip back to user's saved state |
| `SetSolid(r, g, b)` | `POST /led/solid` | Set solid color |
| `Off()` | `POST /led/off` | Turn off LEDs |

All calls are fire-and-forget with a 5s timeout. Hardware unavailability is silently ignored.

## Normal Operation

When no status state is active, the LED is controlled by:

1. **Emotion presets** — colors driven by the AI agent's emotional state (see [emotion-led-mapping.md](emotion-led-mapping.md))
2. **Scene presets** — user-selected lighting scenes (reading, focus, relax, etc.)
3. **Ambient breathing** — gentle warm breathing when idle

A status state **overrides** all of the above when active. Once it clears, normal LED behavior resumes automatically.

## User Experience

| User sees | What's happening |
|---|---|
| Blue breathing | Lamp is booting |
| Blue blink | Joining Wi-Fi during setup |
| Brief white flash | Lamp is ready to listen |
| Cyan breathing | AI brain is disconnected (Lamp can still control lights/servo locally) |
| Purple breathing (after dark) | HAL recovered from a crash |
| Dark / no LED | HAL crashed (LED driver is down) |
| Orange breathing | No internet (Lamp is offline) |
| Yellow breathing | A hardware component is unhealthy |
| Orange breathing (`ota_progress`) | OTA firmware update in progress |
| Green flash | OTA update completed successfully |
| Red pulse | OTA update failed |
| Warm breathing (normal) | Lamp is idle, just vibing |
