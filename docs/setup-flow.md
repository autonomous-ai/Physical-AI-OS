# Setup Flow — Documentation

## Overview

When the OS server is not yet configured (`SetUpCompleted = false`), the device runs in AP mode, serving a Web UI for user setup.

## Flow

```
1. Device boots → check config.json
2. Not set up → AP mode (WiFi hotspot)
3. User connects to WiFi → opens Web UI
4. Enters: WiFi SSID/password + LLM config + optional messaging channel
5. POST /api/device/setup
6. OS Server processes (async):
   a. Connect WiFi (connect-wifi CLI) — in parallel, an early-capture
      goroutine publishes the STA LAN IP into setup state as soon as wlan0
      gets one (before internet is up), so the Web UI can read it while the
      AP is still briefly alive (see "AP→STA Auto-Redirect")
   b. Wait for internet (up to 60s wall clock; ICMP to 8.8.8.8, falling back
      to a TLS handshake with the cloud API host for networks that drop ICMP)
   c. Save config, then resync the clock (no RTC: the device boots with a
      stale time and every TLS call fails "certificate is not yet valid"):
      chrony `online` + `refresh` (re-resolves NTP pool names that failed in
      AP mode) + `burst`, or a systemd-timesyncd restart; wait up to 15s for
      `NTPSynchronized`, else keep syncing in the background
      (`system/lib/clocksync`). A later backend ping that fails on certificate
      validity also starts a background resync, at most once a minute
   d. Early backend ping (fire-and-forget HTTP POST {llm_base}/ping, status
      "setting_up") — publishes the device's fresh LAN IP (local_ip) to the
      backend WITHOUT waiting for the agent setup below, so a page that
      opened the Setup popup can look the IP up and rescue the redirect
   e. Setup agent gateway
   f. Wait for agent ready (poll 120s)
   g. SetUpCompleted = true; clear the temporary setup-white LED state so it
      is not retained as a user LED preference and the strip returns to the
      ambient resting look (currently dark/off)
   h. Backend ping (status "working", setup_completed=true)
7. On failure → return to AP mode
8. Web UI auto-redirects the browser to http://<lan_ip>/setup once the
   operator is back on home Wi-Fi (IP-first; mDNS .local is a last-resort
   discovery fallback when the AP died before lan_ip could be read)
```

## API

### POST /api/device/setup

```json
{
  "device_id": "lamp-a1b2",
  "ssid": "MyWiFi",
  "password": "...",
  "llm_api_key": "sk-...",
  "llm_base_url": "https://api.anthropic.com",
  "llm_model": "claude-haiku-4-5-20251001",
  "channel": "telegram",
  "telegram_bot_token": "...",
  "telegram_user_id": "...",
  "admin_password": "...",
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/device123",
  "fd_channel": "fd/device123",
  "deepgram_api_key": "..."
}
```

Fields come from `SetupRequest` in `system/domain/device.go`. `device_id`, `llm_api_key` and `llm_base_url` carry `validate:"required"`; everything else is optional. `ssid` may be empty (wired/ethernet path, see below). `channel` is `telegram` (default when empty), `slack`, `discord` or `imessage`; the matching credential fields are `telegram_bot_token`/`telegram_user_id`, `slack_bot_token`/`slack_app_token`/`slack_user_id`, `discord_bot_token`/`discord_guild_id`/`discord_user_id`, or `bluebubbles_server_url`/`bluebubbles_password`/`bluebubbles_user_address`. Optional voice overrides: `stt_api_key`, `tts_api_key`, `stt_base_url`, `tts_base_url`, `stt_language`, `tts_provider`, `tts_voice`.

**Response:** Returns immediately `{"status": 1}`. Setup runs async in a goroutine after 2s delay.

**Messaging channel:** The entire Telegram, Slack, Discord, or iMessage configuration is optional during initial setup. Omitting it does not block setup; configure a channel later with `POST /api/device/channel`. Credentials supplied to `POST /api/device/setup` keep the existing setup path but are not channel-validated there; `POST /api/device/channel` validates the credentials required by its selected channel.

**Admin password default:** `admin_password` is optional. When empty on a first-time setup (`SetUpCompleted=false` and no `AdminPasswordHash` on file), the handler defaults it to the 4-char hardware suffix from `device.GetDeviceMac()` — the same suffix `scripts/provision/setup-ap.sh` uses for the AP SSID (`<DEVICE_TYPE>-<xxxx>`). The suffix is printed on the sticker at the bottom of the device, so operators can sign into the admin UI without picking a password. The V2 Setup Web UI hides the DEVICE PASSWORD field entirely and relies on this default; V1's dedicated Device step still asks the operator to pick one. Fails 400 (`device hardware ID unreadable`) when `GetDeviceMac()` returns empty (no `DEVICE_TYPE` env, no serial, no eth MAC) — silent fallback would give every unidentified device the same well-known password.

### POST /api/device/channel

Change messaging channel after setup is complete. Accepts `telegram`, `slack`, `discord`, `imessage`. A channel the active runtime cannot run is rejected with `400 <channel> not supported on the active runtime` (today `imessage` is Hermes-only).

**WhatsApp is rejected here** (`400 whatsapp pairing not supported via HTTP; use MQTT add_channel`) — WhatsApp pairing streams a rotating QR back to the caller, which HTTP's fire-and-forget shape can't carry. The canonical path is the MQTT `add_channel` command (see `docs/mqtt.md`) which publishes one fd_channel message per pairing event. Re-pairing without re-bootstrapping uses the MQTT `whatsapp_pair` command.

## Network Setup

`device.Setup` takes one of two network paths, chosen by whether the request carries
an SSID. Everything after the network phase (LLM config, channel, agent setup,
`SetUpCompleted`) is identical for both.

**Wi-Fi path** (`setupWiFi`, SSID present):

1. Call `connect-wifi` CLI tool with SSID + password
2. Poll checks:
   - SSID match? (`iwgetid`)
   - Internet OK? (`ping 8.8.8.8`, else a TLS handshake with the device's cloud
     API host — `system/network/reachability.go`)
3. Timeout 60s of wall-clock time → fail. (It used to count 60 attempts, and
   each failed ping waits 5s, so a network that drops ICMP kept the device
   trying for ~6 minutes after the web client gave up at 80s.)
4. Success → save SSID + password to config

`connect-wifi` ends by running `device-sta-mode`, which is what tears the AP down.

**Wired path** (`setupWired`, SSID empty):

An empty `ssid` is a valid request meaning *"the device already reaches the network,
don't join any Wi-Fi"* — the ethernet case. `SSID`/`Password` therefore carry no
`validate:"required"` tag (an empty password with a non-empty SSID is likewise a valid
open network, though the setup web UI still asks for one).

1. `CheckInternet()` must succeed — the claim is verified, not trusted. The
   provisioning AP has no uplink, so a device with neither ethernet nor Wi-Fi fails
   here with *"no WiFi credentials given and the device has no working internet
   connection"* and stays in AP mode.
2. Publish `lan_ip` into setup state **before** touching the AP — teardown restarts
   dhcpcd and can briefly interrupt the very connection the client is talking over, so
   the address has to be readable from `GET /api/device/setup/status` first.
3. `LeaveAPMode()` runs `device-sta-mode` — the same script the Wi-Fi path reaches via
   `connect-wifi`, keeping one implementation of AP teardown. **This step is the whole
   reason the wired path can't just skip the network phase:** nothing else would ever
   stop `hostapd`/`dnsmasq`, and the device would keep broadcasting its open,
   password-less setup hotspot with the captive-portal DNS wildcard forever. A failure
   here is logged at error level but does not abort setup — aborting would leave that
   same AP up anyway.

`config.NetworkSSID`/`NetworkPassword` stay empty on this path.

**Web UI.** `useWifiConnected` already probes `GET /api/network/check-internet` and
`GET /api/network/current` (both public, no admin session needed). Internet **with** an
SSID means "already on home Wi-Fi"; internet **without** one means a wired uplink, which
marks the Wi-Fi step satisfied, shows a notice above the picker, and lets submit through
with an empty SSID. Picking a network still works — the operator can add Wi-Fi on top of
the cable.

The offer is driven by **live connectivity, not by hardware**: nothing asks whether the
device has an ethernet port. A device with no port, or with a port and no cable, simply
fails `check-internet` while in AP mode (the provisioning AP has no uplink), so the
notice never appears and the Wi-Fi step blocks exactly as before. Two consequences worth
knowing:

- `GET /api/network/current` returns `null` both for "associated to nothing" and when
  the probe fails, so the hook keeps those outcomes apart and only treats a *successful*
  empty answer as a wired uplink. A failed probe stays undecided and retries — undecided
  means the step keeps asking for credentials, the safe default.
- A USB modem or a phone tether is indistinguishable from ethernet here, and behaves the
  same way (no Wi-Fi needed, AP torn down), which is why the copy says "online without
  Wi-Fi" rather than asserting a cable.

**A wired device belongs in the *initial* wizard.** `SetupGate` (`App.tsx`)
picks initial vs continue from `set_up_completed` on the open
`GET /api/device/setup/status`. It used to infer it from `check-internet`: the
provisioning AP has no uplink, so "the device has internet" meant it had already
left AP mode and been set up. **Ethernet breaks that invariant** — a brand-new
device with a cable in it has internet from first boot, so it opened the
*continue* wizard, whose Wi-Fi step is a read-only "you're online" row and whose
forward button is a plain Next. There was no Setup button anywhere on the page,
so nothing ever POSTed `/api/device/setup` and the device could not be
provisioned over ethernet at all. With the flag, the wired device gets the same
initial wizard as a Wi-Fi one; the only difference is that its Wi-Fi step arrives
already satisfied (`wiredUplink`), so the operator goes straight to **Setup**.
The connectivity check remains as a second condition, so this only tightens the
continue path; an older os-server that doesn't publish the flag falls back to the
old inference.

**Same wizard, same events.** Past the network step the wired path is the Wi-Fi
path — one `device.Setup`, one set of phases, one bridge event sequence
(`setup_submitted` → `setup_connecting` → `setup_connected` → … →
`setup_done`). A parent window needs no wired-specific handling. Two things make
that true rather than merely intended: the `run` counter (see *Stale-verdict
guard*), without which the wired `connected` verdict resolves too fast for the
poll to accept, and the continue-mode gates below, without which the reopened
popup lands back on the Wi-Fi step and reports a failure instead.

The **post-submit screen** does swap its copy when the submit carried no SSID
(`wiredRun`, latched at submit so the screen keeps describing the run it is
reporting on): a cable icon, *"Finishing setup on your wired connection"* and a
note that the device is turning off its setup hotspot, since there is no Wi-Fi
join to narrate. The failure branch likewise drops the password/2.4GHz/distance
checklist — the only way this path fails is the uplink check — for cable, router
port, and "or pick a Wi-Fi network instead".

**Re-setup caveat.** `mergeMissingFromConfig` refills an empty `ssid` from
`config.NetworkSSID`, so re-running setup on a device already provisioned over Wi-Fi
takes the Wi-Fi path even if the operator leaves the field blank. Only devices with no
saved SSID (fresh, or previously set up wired) reach the wired path that way.

## AP Mode

- When not set up or setup fails → automatically switches to AP mode
- Already configured devices also enter a recovery hotspot after 90s of WiFi link loss, retaining setup and credentials. With no hotspot clients, saved WiFi is retried every 2 minutes; see [Connectivity monitor](os-server.md#network) for recovery timing and the `/wifi` page.
- Device broadcasts WiFi hotspot
- Web UI serves setup page
- `SwitchToAPMode()` in `system/network/service.go`
- **AP mode owns `wlan0` only — the wired link keeps working.** `device-ap-mode` used to
  `systemctl stop`/`disable dhcpcd`, but the golden image purges NetworkManager, so dhcpcd
  is the DHCP client for *every* interface — disabling it also killed `eth0`/`end0`, and the
  disable persisted across reboots until `device-sta-mode` ran (i.e. until someone supplied
  WiFi credentials). It now appends `denyinterfaces wlan0` to `/etc/dhcpcd.conf` and keeps
  dhcpcd running; `device-sta-mode` strips that line to hand `wlan0` back. Consequence: a
  device in AP mode that is also plugged into ethernet holds a LAN address, and because
  avahi advertises on every interface, the setup page is reachable from the wired LAN at
  `http://<device_type>-<suffix>.local/` as well as at the AP's `192.168.100.1`.
  That wired address is also what makes the ethernet-only setup below possible.
- **LED indicator:** once HTTP server is listening, if `SetUpCompleted == false` the OS server spawns a background goroutine (`waitAndPaintSetupReady` in `system/server/config_watch.go`) for devices with a light capability. It calls HAL `POST /led/status` with state `setup` immediately, then retries after 1, 2, 4, 8 and at most 10 seconds between attempts, without a 30-second cutoff. The LED endpoint is available before full HAL health; HAL resolves state `setup` to a solid white strip. HTTP errors or missing `status: "ok"` acknowledgements are retried. The worker exits after acknowledgement, when setup completion is observed, or on server shutdown (which also cancels in-flight HTTP requests). Setup completion is checked before each LED request and after failed requests. The poll exists because os-server typically binds :5000 before HAL's FastAPI is up on :5001 (Python and the LED driver still need to initialize) — a fire-and-forget paint would silently drop on `connection refused`. This white is a **temporary AP/pre-setup cue**, not a user preference: after a successful `POST /api/device/setup`, HAL clears its saved `source: "status:setup"` LED cue on the existing `/led/off` call and restores the owner’s resting-light choice (or the device preset), instead of saving an explicit user OFF. The booting blue-breathing still shows during init.
- **AP-mode LED suppression:** the openclaw WS reconnect loop (`runtimes/openclaw/service_ws.go`) skips `StateAgentDown` Set/Clear while `config.SetUpCompleted == false`, so the cyan disconnect overlay doesn't fight the setup-needed white during provisioning. WS still runs (`device.Setup` needs it ready to satisfy `WaitForAgentReady` before flipping `SetUpCompleted=true`), only the LED side-effect is gated.
- **Wi-Fi association LED cue (`StateWifiConnecting`):** the moment the setup handler enters `device.Setup()`, it activates `statusled.StateWifiConnecting` (HAL preset `wifi_connecting` = blue `[0,135,255]` blink at speed 0.5) so the ring visibly switches from the setup-white to a blue blink while `wlan0` associates. A `defer` in `Setup()` clears it on every return path, so a failure that falls through to `SwitchToAPMode()` doesn't leave the strip blinking. Priority sits above `Booting` and below `OTA`/`Error`/`Connectivity` — the cue outranks residual boot state but never masks a real fault. Devices without the `light` capability short-circuit inside statusled (no-op).

## AP→STA Auto-Redirect (the "joining Wi-Fi…" screen)

After the operator submits, the Setup page shows a "Your device is joining
Wi-Fi…" screen and tries to **automatically redirect the browser to the
device's new home-Wi-Fi address** once it's reachable, so the operator doesn't
have to hunt for an IP. This section documents how that redirect works, why it
used to get stuck, and what changed.

### The hard constraint

The device has **one Wi-Fi radio (`wlan0`)**. During provisioning it runs as an
access point at `192.168.100.1`. To join the home network it must switch the
same radio AP→STA — which **tears the AP down**. The moment the AP dies, the
browser (still associated to the AP SSID) **loses all network access to the
device** until the operator manually rejoins their home Wi-Fi. There is no
window in which the browser is on both networks at once.

Consequence: the browser can only learn the device's new LAN IP **before** the
AP tears down, or via mDNS **after** the operator is back on home Wi-Fi.

### Redirect channels (`useSetupStatusPolling.ts`)

Redirect is **IP-first by design.** The device's `.local` mDNS name is not a
primary redirect target: many home/office routers block mDNS multicast (and
Android Chrome has no native mDNS), so `.local` silently fails to resolve. A
raw LAN IP resolves on every network, so it's the preferred single source of
truth for "where the device now lives." mDNS exists only as a **last-resort
discovery fallback** for the case where the IP channel provably never got an
IP (see channel 3).

1. **Phase poll** — polls `GET /api/device/setup/status` against the AP IP while
   the AP is alive. Reads `phase` + `lan_ip` + `run`. Goes dark the instant the AP tears
   down. (The backend captures the STA IP early so this poll can return a
   `lan_ip` during the brief window the AP is still alive — see
   `system/device/setup.go`.) A wall-clock watchdog flips an `apLost` flag
   when the poll has been unanswered >5s while setup is in flight — wall-clock,
   not consecutive-failure counting, because fetches to a vanished AP can hang
   in the browser's TCP retry for many seconds.
2. **LAN-IP probe** — once `lan_ip` is known, probes `http://<lan_ip>/api/health`
   from the browser; when it succeeds (operator is back on home Wi-Fi and the
   device is up) it redirects to `http://<lan_ip>/setup?<params>`. This is the
   **primary** redirect channel and stays authoritative whenever it has a
   target.
3. **mDNS `.local` fallback probe (discovery only)** — rescues the
   first-time-setup race: on a fresh join the AP often tears down **before**
   wlan0 has a DHCP lease, so the phase poll never reads `lan_ip`, the
   operator's machine auto-rejoins home/office Wi-Fi, and channel 2 has no
   target — previously the page was stranded on the dead AP IP forever. When
   `setupWorking && apLost && !lan_ip` and the hostname (`<type>-<xxxx>`, from
   the open setup-status endpoint's `mac`) is known, the FE probes
   `http://<host>.local/api/health` every 2s; on success it redirects to
   `http://<host>.local/setup?<params>`. Gated on `apLost` because while the
   AP is alive, avahi may answer mDNS **over the AP link** and a premature
   redirect would reload the page mid-join, losing the "Setting up…" state.
4. **`.local` landing seed** — when the page is served from a `.local` host
   (i.e. right after channel 3 fired), it fetches `lan_ip` once from the open
   setup-status endpoint, which lets channel 2 canonical-upgrade the URL onto
   the raw IP. `.local` is only the discovery bootstrap; the raw IP is the
   durable home, since mDNS can stop resolving at any time. A guard rejects
   the AP's own `192.168.100.1` so this can never "upgrade" onto the AP
   address.

**Pre-submit canonical-URL upgrade.** The LAN-IP probe above also runs *before*
submit: when the page is on the AP IP (`http://192.168.100.1`) and a `lan_ip` is
already known, it bounces the browser off the soon-to-die AP IP onto
`http://<lan_ip>/setup`, which survives the AP→STA switch. **Before submit, with
wlan0 still serving the AP and no STA IP yet, `lan_ip` is empty and the page
simply stays on `192.168.100.1`** — the mDNS fallback (channel 3) is gated on
`setupWorking`, so it never fires pre-submit either.

### Root cause of the "stuck forever" bug

Two independent defects made the screen hang indefinitely even though the device
had successfully joined Wi-Fi:

1. **CSP blocked every cross-origin probe.** The device's nginx served
   `Content-Security-Policy: …; connect-src 'self' ws: wss:`. The Setup page is
   served from the AP IP, so `'self'` is `http://192.168.100.1`. Both the
   `lan_ip` probe (`http://172.x.x.x/…`) and the mDNS probe
   (`http://…​.local/…`) are a **different origin**, so the browser refused the
   `fetch` with *"Refused to connect because it violates the document's Content
   Security Policy"* — `mode: "no-cors"` does **not** bypass CSP. Every channel's
   probe died before leaving the browser.

2. **The LAN-IP channel almost never had a `lan_ip` to use.** `lan_ip` was only
   published into setup state *after* `SetupNetwork()` finished its **up-to-60s**
   wait for internet. But the AP tears down within ~2s of that call starting, so
   the phase poll went dark long before `lan_ip` existed → the LAN-IP channel
   stayed disabled → only the mDNS channel remained → and on mDNS-blocking
   networks that one can't resolve either. Result: **no channel ever fires.**

So on a router that blocks mDNS multicast (the exact field case), the page was
permanently stuck on "joining Wi-Fi…" despite a fully successful join.

### The fix

| Layer | Change | Why |
|-------|--------|-----|
| **CSP** (`scripts/imager/build*.sh`, `scripts/provision/setup.sh`, `scripts/maintenance/patch-security.sh`) | `connect-src 'self' ws: wss:` → `connect-src 'self' ws: wss: http:` | Lets the browser `fetch` the cross-origin LAN-IP probe. `http:` (not `http://*.local`) is required because **CSP can't express an IP range** — a single `http:` token is the only way to allow `http://<any-lan-ip>/…`, so the fix is independent of the customer's subnet (`172.x`, `192.168.x`, `10.x`). |
| **Backend** (`system/device/setup.go`) | A goroutine polls `GetCurrentIP()` once per second **in parallel with** `SetupNetwork()` and publishes the STA IP into setup state the instant it appears (skipping the AP's own `192.168.100.1`), before the 60s internet wait completes. | Gives the FE the **largest possible window** to read `lan_ip` during the brief overlap where it's still polling the AP — so the LAN-IP channel actually has an IP to redirect to. A guard keeps an already-captured IP from being clobbered by a later empty read during AP teardown. |
| **Frontend** (`useSetupStatusPolling.ts`) | Removed the `.local` mDNS redirect channel as a *primary* target. The primary redirect is the LAN-IP probe, which carries `pathname + search` and targets `http://<lan_ip>/setup?<params>`; it also serves as the pre-submit canonical-URL upgrade. (A discovery-only `.local` fallback was later re-added for the AP-died-before-lan_ip race — see channels 3–4 above.) | `.local` is unreliable on mDNS-blocking networks, so it can't be the primary redirect target. The IP is read dynamically from the backend — **no hardcoded subnet, no mDNS dependency on the happy path**. |
| **Frontend** (`Setup.tsx`) | The "save this address" copy field and the "Continue setup" link now use the **raw-IP URL** (`http://<lan_ip>/setup`); they gate on `setupLanIP` instead of the mDNS host, falling back to a router-admin hint when no IP is known yet. (The copy field has since been removed from the *connecting* screen — it remains on the connected one.) | IP-only, end to end — the operator is never handed a `.local` address that can't resolve on their network. |
| **Frontend** (`Setup.tsx`) | The Copy button gained a `document.execCommand("copy")` fallback (hidden textarea) for when `navigator.clipboard` is unavailable. | The Setup page is served over plain HTTP (`http://192.168.100.1`), where `navigator.clipboard` is `undefined` (it needs a secure context) — so the modern API silently no-op'd and the button did nothing. The legacy path works on `http://` origins. |

### Redirect target

The happy path now redirects to **`http://<lan_ip>/setup?<params>`** (e.g.
`http://172.168.20.145/setup?…`) — the raw IP, which works regardless of mDNS.

The copy field survives only on the **connected** screen. The one that used to
appear on the "joining Wi-Fi…" screen — a safety net for when the AP dropped
before the phase poll flipped — was removed as UI noise, along with its "This
page disconnects when you rejoin home Wi-Fi" line. While the join is in flight
the screen now shows just the spinner, the message and the elapsed counter.

### Assessment & trade-offs

- **What it fixes:** the auto-redirect (and the manual copy link) now work on
  mDNS-blocking networks, which was the reported field failure. The solution is
  **subnet-agnostic** — it doesn't assume any particular private IP range.
- **What it still depends on:** the auto-redirect via the LAN-IP channel only
  fires if the FE captured `lan_ip` during the ~2s AP-alive overlap — on a
  first-time setup that window usually closes before DHCP completes. The mDNS
  fallback (channel 3) covers that case on networks where `.local` resolves;
  on mDNS-blocking networks with no captured `lan_ip`, **no automatic channel
  can fire**. The in-page manual IP entry that used to cover this case was
  removed as UI noise, so the page now rides out the join and lands on the
  `JOIN_TIMEOUT_SEC` failure screen; recovery is rejoining the device AP and
  re-running setup (the failure is adopted on mount — see "Failed joins").
- **Backend rendezvous (device side ready):** the early backend ping (step 6d)
  publishes `local_ip` the moment WiFi is up, so a page that opened the Setup
  popup (e.g. autonomous.ai) can poll the backend by `mac` and navigate the
  popup to `http://<ip>/setup?<params>` — cross-origin popup *navigation* by
  the opener is allowed even though reads aren't. This covers the
  mDNS-blocking case automatically, but needs the backend to store/expose the
  IP and the parent page to poll it; neither exists in this repo.
- **Security trade-off of `http:` in CSP:** `connect-src http:` permits the
  Setup page to `fetch` any plaintext-HTTP origin, not just the device.
  Acceptable here because the Setup bundle is served only on the LAN/AP, ships
  no secrets in these health probes, and CSP offers no narrower way to allow an
  arbitrary LAN IP. Tracked in `docs/security/CHECKLIST.md` (F9).
- **Why not avoid the AP teardown entirely:** a dual-radio or wired path would
  remove the constraint, but the target hardware has a single Wi-Fi radio — so
  the "learn the IP before AP dies, or via mDNS after" model is inherent.

### Failed joins (wrong Wi-Fi password)

A wrong Wi-Fi password is the most common setup failure, and it exposes the same
single-radio constraint from the other side: the operator never learns why.

**Why the backend's verdict goes unheard.** `SetupNetwork` polls for up to 60s
before returning an error, but the AP tears down ~2s after submit. From that
moment the phase poll can no longer reach `192.168.100.1`, and the operator's
machine has usually auto-rejoined its home Wi-Fi. The backend sets
`phase="failed"` at t≈62s and `handler.Setup` calls `SwitchToAPMode()`, which
restores the hotspot ~5-8s later — but by then nothing in the browser is
listening. Before the fix below, the page spun on "joining Wi-Fi…" forever.

Three mechanisms now cover it:

1. **Client-side join timeout** (`useSetupStatusPolling.ts`,
   `JOIN_TIMEOUT_SEC = 80`). When the join is still `connecting`, the AP has
   been unreachable (`apLost`), and no `lan_ip` was ever captured, the FE
   declares failure itself. 80s sits past the backend's 60s network timeout plus
   the AP restore, so a merely-slow join is never called dead. The message is
   hedged ("couldn't be reached… usually the Wi-Fi password was wrong, or the
   network is 5GHz-only") because the real reason never arrived — asserting a
   specific cause would be guessing.

2. **Failure adoption on mount** (`useSetupController.ts`). `setupState` lives
   in os-server's memory and survives the AP→STA→AP bounce, and
   `GET /api/device/setup/status` is public (a device that failed setup never
   wrote an admin hash, so an admin-gated endpoint would be unreadable). The
   controller reads it once on mount; if the last attempt `failed`, it drops
   straight into the failure screen carrying the backend's **real** error
   message. This is what rescues a reopened popup, a closed tab, or a manual
   reload — all of which previously mounted a blank Wi-Fi form with no hint that
   anything had gone wrong. Only `failed` is adopted; a stale `connecting` would
   hijack the tab into a progress screen it cannot drive.

   **Adoption also clears the failed attempt's state.** Rejoining the device AP
   after a failure lands the operator on a genuinely clean wizard: `ssid`,
   `password`, `adminPassword`, `error`, `stepError` and `setupLanIP` are reset
   and the wizard returns to the Wi-Fi step. A Wi-Fi failure bails before
   `device.Setup` writes any config, so none of it was ever persisted
   device-side — keeping it would only let the operator resubmit the credentials
   that just failed without noticing. The persisted params snapshot is dropped
   too (`clearStoredSetupParams()`): `sessionStorage` is per-tab but **survives
   F5**, so an operator who leaves the tab open, rejoins the AP and reloads
   would otherwise rehydrate the failed attempt's query string into a form that
   looks clean but still ships the old `llm_api_key`. Clearing at mount (rather
   than reloading immediately) keeps the current document usable for the
   in-place "Back to Wi-Fi" retry while guaranteeing the *next* load starts from
   nothing.

   An adopted failure sets its own `adoptedFailure` flag rather than reusing
   `setupWorking`. The two mean different things: `setupWorking` says *this tab
   has a join in flight* and starts the 600ms phase poll, the elapsed ticker and
   the bridge's connecting/failed emits. Raising it for a join we merely read
   about would restart the poll against a backend still reporting
   `phase="failed"` — nothing resets `setupState` to `idle` — so the poll would
   re-assert the failure every 600ms and bounce the operator out of the Wi-Fi
   form each time they pressed "Back to Wi-Fi". The screen renders on
   `showProgressScreen = setupWorking || adoptedFailure`; the retry action
   clears both.

   **Adoption is skipped in continue mode.** Continue mode means `SetupGate`
   already proved the device is online and serving the page from its LAN
   address, so a `failed` verdict still sitting in `setupState` cannot be
   describing it — nothing ever resets the phase to `idle`, so an old failure
   survives until the next run or a reboot. Adopting it there wiped the form,
   dropped the operator back on the Wi-Fi step of a device whose network is
   demonstrably fine, and emitted `setup_failed` to the parent for a setup that
   had already succeeded (on `intern-v2`, where the failure *screen* is skipped,
   that was the whole visible symptom: the popup silently bounced to Wi-Fi). The
   adoption path exists for the offline case — a failed join leaves the device
   back on its AP with no internet, which resolves to **initial** mode, where it
   still runs.

   **Stale-verdict guard in the poll.** `handler.Setup` answers `200`
   immediately but defers `device.Setup` by 2s, and nothing sets `setupState`
   back to `idle` in between — so for ~2s after a resubmit the backend still
   reports the *previous* attempt's `phase="failed"`. The poll therefore ignores
   terminal verdicts until it has confirmed the current run started; without
   that guard the first poll after a retry would throw the operator straight
   back to the failure screen.

   That confirmation is the **`run` counter** in the status payload:
   `setupState.begin()` bumps it on every `device.Setup` call, the poll records
   the value it sees on its first tick (≈2s before the run starts) and treats
   any higher value as "this is our run". `phase === "connecting"` still latches
   it too, as a fallback for a device on an older os-server build that omits
   `run`. The counter is what makes the **wired** path work: its network step is
   a single `CheckInternet()` ping, so `connecting` can begin and end between two
   600ms polls. Latching only on that phase meant the wired `connected` verdict
   was discarded as stale — the parent never received `setup_connected`, the
   `lan_ip` published alongside it was dropped, and the screen sat on
   "connecting" until the 80s timeout declared a *successful* setup failed. (The
   page still recovered via the mDNS fallback, which is why the wired flow
   appeared to work while emitting the wrong events.)

3. **Retry that actually validates.** `mergeMissingFromConfig` is no longer
   gated on `SetUpCompleted`. A Wi-Fi failure bails before `device.Setup` writes
   any config, so the device stays `SetUpCompleted=false` — while the browser
   may have lost the pushed credentials (sessionStorage dies with the tab; a
   reopened popup without the original query string comes back empty). The gate
   meant retry failed validation on `LLMAPIKey` and showed *"Missing: AI Brain
   API key"* to someone who had only mistyped a Wi-Fi password. The merge only
   fills empty slots from the device's own config, so it can neither override
   what the operator sent nor expose anything new.

**Recovery UI.** The failure screen shows the error, a three-item checklist of
the common causes (password case, 2.4GHz vs 5GHz, distance to the router), and
one action:

- **Back to Wi-Fi** — in-place retry onto a clean wizard. Wipes every field the
  failed attempt left behind — `ssid`, `password`, `adminPassword`, `error`,
  `stepError`, `setupLanIP`, `elapsed`, both failure flags — and drops the
  sessionStorage params snapshot (`clearStoredSetupParams()`), returning to the
  Wi-Fi step. This mirrors the adoption path's cleanup field for field: those
  are the only two routes back to the form, and clearing different things would
  make "retry in this tab" and "reopen after rejoining the AP" behave
  differently for no reason the operator could predict.

  Safe to wipe because a Wi-Fi failure bails inside `SetupNetwork`, before
  `device.Setup` writes any config — nothing was persisted device-side. The
  password in particular must go, or the operator can resubmit the exact value
  that just failed without noticing.

  It is **not** a reload: the current document, and the params the parent pushed
  into it (`llm_api_key`, `channel`, `device_id`), stay alive, so the operator
  re-enters Wi-Fi credentials rather than the whole setup.

  **Nothing to clear server-side.** The device never wrote config for a failed
  join. Its in-memory `setupState` does still report `phase="failed"` — no code
  path resets it to `idle` — which is why the poll ignores terminal verdicts
  until it has seen `phase="connecting"` confirm the new run started. Without
  that guard this reset would be undone within 600ms by the previous attempt's
  verdict.

A failed join leaves the operator on their home network, so the button can't
reach the device until they rejoin the hotspot the device restored on its own
(`handler.Setup` → `SwitchToAPMode`). The screen no longer names that SSID: the
rejoin hint and the second **Start over** action were removed as UI noise. The
`startOver` handler and its `apSsid` prop went with them; `resetSetupSession()`
and the `start_over_clicked` bridge event remain in place but currently have no
caller.

**Per-device: skipping the failure screen.** `intern-v2` does not show the
failure screen at all. A failed join drops the operator straight back on the
Wi-Fi form — the same end state as pressing "Back to Wi-Fi", including the full
state wipe above — with no error banner or checklist. This is a deliberate
product decision for that device class; `lamp`, `reachy-mini` and `unitree-go2w`
keep the full screen. The auto-return effect calls `retryFromFailure()` verbatim,
so the reset can never drift between the two paths.

The class comes from `mac` (`"<device_type>-<4 hex>"`, e.g. `intern-v2-d94b`),
stripped of its hex suffix — never from a URL param, which the operator's
browser must not be able to influence (see `DeviceTypeOrDefault`, which treats
`DEVICE_TYPE` as immutable hardware identity). Both routes into a failure are
covered:

- **Timeout in this tab** — `showProgressScreen` suppresses the screen and an
  effect calls the same `retryFromFailure()` the button uses, so the two paths
  cannot drift.
- **Adoption on mount** — the mount effect reads the class from the *same*
  `setup/status` response that carried the verdict, rather than waiting on the
  `mac` state (filled by a separate request), which would otherwise let the
  screen paint for a frame before being suppressed. It clears the attempt's
  state as usual but never raises the failure flags.

Both still emit `setup_failed` on the bridge. The screen is hidden from the
*operator*, not from the companion app — the adoption path fires the emit
directly, since the effect that normally sends it is gated on `setupWorking`,
which that path deliberately never raises.

**Setup failure reasons.** Every failed setup is logged with a stable
`setup_failure_reason` field — on `network setup failed` (component `network`)
for the WiFi join, and on `setup failed` (component `device`) for every stage —
so failed setups can be grouped by cause in Graylog
(`_exists_:setup_failure_reason AND spooled:true` finds the ones replayed after the
device came online). The WiFi join is classified from `wpa_supplicant`'s own log
for the attempt plus the last observed state (`system/network/setup_failure.go`):

| `setup_failure_reason` | Evidence | Setup-screen message |
|---|---|---|
| `wrong_password` | `4-Way Handshake failed` / `CTRL-EVENT-SSID-TEMP-DISABLED … reason=WRONG_KEY` | wrong WiFi password: the router rejected the key |
| `association_rejected` | `CTRL-EVENT-ASSOC-REJECT`, `reason=AUTH_FAILED`/`CONN_FAILED` | the router refused the connection |
| `ssid_not_found` | `CTRL-EVENT-NETWORK-NOT-FOUND`, or no association attempt at all | WiFi network not found: out of range or on a channel the device cannot use |
| `no_dhcp` | `wpa_state=COMPLETED` but no address from the router | connected to WiFi but the router gave no IP address |
| `no_internet` | address on the requested SSID, but neither ICMP nor the cloud TLS probe gets out | connected to WiFi but there is no internet (captive portal or firewall?) |
| `ssid_too_long` / `wifi_connect_error` | SSID over 32 bytes / the `connect-wifi` helper failed | as before |
| `agent_setup_failed` / `agent_timeout` | the agent runtime could not be set up / never became ready (after the WiFi join) | as before |
| `unknown` | none of the above could be established | no internet or SSID did not match within 60s |

A rejected key fails fast: once `wpa_supplicant` has rejected it twice the loop
stops instead of waiting out the 60s window. The `network setup failed` line
also carries `wpa_state`, `has_ip`, `ssid_matched`, `wrong_key_failures` and
`elapsed_s`. 5 GHz networks are not a failure cause on Intern 2 hardware — the
wrong-password capture above was on a 5745 MHz network.

### Marking the Wi-Fi step done after the reload

The auto-redirect lands a **full page reload on the new LAN-IP origin**
(`http://<lan_ip>/setup?…`), which is a different origin from the AP page. All
of the Setup form's React state — including the `ssid` / `password` the operator
just typed — resets to empty. `/api/device/config`, which would rehydrate the
saved SSID, is admin-gated and a fresh device has no admin session yet (401), so
the reloaded page can't read it back. Left alone, the Wi-Fi step would therefore
re-render "Choose your Wi-Fi + enter its password" even though the device is
already on home Wi-Fi.

To avoid re-prompting, the Wi-Fi step's done-state is derived from the
**device's live network state**, not the form fields (`useWifiConnected.ts`):

- `GET /api/network/check-internet` — the device has an uplink. The setup AP has
  no uplink, so internet == the device left AP mode and joined a real network.
- `GET /api/network/current` — the SSID `wlan0` is presently associated with
  (`iwgetid -r`); non-empty == station mode.

Both are **public** (no admin auth), matching the same internet signal
`SetupGate` (`App.tsx`) already uses to pick continue vs initial mode. When both
are satisfied, `sectionDone.wifi` short-circuits to done and `WifiSection`
collapses the picker into a read-only **"Connected to `<ssid>`"** row instead of
the empty selector. There is no way back to the picker from that row: switching
networks is a `/setting#wifi` concern (`pages/settings/WifiSection.tsx`, which
always renders the full picker), not something the setup wizard offers. The
associated SSID also prefills the picker. The Wi-Fi *password* never leaves the device — only
the associated SSID name (which the device already scans and broadcasts) and the
`check-internet` boolean are read.

While that first probe is still in flight (`checking`), `WifiSection` renders a
**skeleton** for the network + password fields instead of the empty picker, so
the step never flashes "Choose your Wi-Fi" for a beat before resolving into the
connected state. Later background retries (covering the DHCP-lease race) don't
re-raise the skeleton, so the picker stays interactive once shown.

**The continue-mode auto-scroll waits for that probe too** (`if (wifiChecking)
return;`). It jumps the operator to the first incomplete step and then spends
`autoScrolledRef` so it can't fight them afterwards — but its first run happens
*before* the probe answers, when `sectionDone.wifi` is still false. It therefore
parked every reopened popup on the Wi-Fi step and burned the one-shot ref, so
the answer arriving a moment later could no longer move it. A wired device felt
this worst: the step it was pinned to is one it has nothing to enter on, and
being stuck short of the last step also meant the wizard never reached the
button that emits `setup_done`.

### Enrollment steps are capability-gated

"My Voice" and "Face" are hardware, not preference: one records the operator
through the mic, the other photographs them through the camera. Each is offered
only when the device declares the capability that makes it possible —
`Cap.Audio` for Voice, `Cap.Vision` for Face — read from
`GET /api/system/info` (`useCapabilities`), which is os-server's parse of
`robots/<type>/ROBOT.md` and the same contract Monitor gates its tabs on. The
gate covers the sidebar entry, the mounted section (so a section that can't work
never issues its hardware requests), **and** the `required` / `order` lists that
drive completion, since an enrollment the device can't perform is not a pending
step — leaving `face` in `required` on a camera-less device made "everything is
done" unreachable, which is the branch that emits `setup_done` and bounces to
`/monitor`.

Concretely: `intern-v2` declares `audio`, `sensing`, `companion`, `system`,
`light`, `media`, `connectivity` — no `vision` — so it shows **My Voice** and
never **Face**. Fail-open while `/api/system/info` is in flight (an unknown
capability set answers `true`), matching every other capability gate in the web.

### Deep-linking into a step via the URL hash

A URL like `http://<lan_ip>/setup?<params>#voice` opens the **Voice** tab
directly. Every redirect path carries the hash through — `SetupGate` in
`App.tsx` and both probes (LAN-IP, mDNS `.local`) in `useSetupStatusPolling.ts`
now append `window.location.hash` to the rebuilt target, matching what
`scrubLocationSecrets()` in `lib/api.ts` already did.

The deep-link effect in `useSetupController.ts` reads `window.location.hash` and
selects the named step when it maps to a **currently-visible** section, so a
stale or hidden id can't strand the operator on a blank step (`#force` is a test
flag, not a step, and is skipped). Voice/Face only exist in continue mode, which
`SetupGate` resolves from two awaited requests (`checkInternet` +
`getSetupStatus`) — so the effect **cannot** assume the mode is known by the
time Setup mounts. It re-runs keyed on `visibleSections.length` rather than
firing once on mount; it lives below the `visibleSections` declaration because
it now reads it as a dependency.

Two rules keep the re-runs from fighting the operator:

- **A hash naming a step that isn't visible yet leaves the URL untouched.** The
  effect returns and sets `awaitingDeepLink` instead. Overwriting the hash here
  was the actual defect: seeding `#wifi` over the requested `#voice` via
  `history.replaceState` destroyed the only record of the target, so no later
  pass could recover it and the wizard sat on Wi-Fi. The "seed the default first
  step" branch now runs only when there is **no** hash at all.
- **A hash is honored at most once** (`deepLinkedRef`), so a later change to
  `visibleSections` can't yank the operator off a tab they navigated to by hand.

While the target step is unknown, Setup renders `SetupSkeleton` — a placeholder
mirroring the real chrome (192px sidebar + topbar + card) at matching
dimensions, so resolving swaps content in without a layout jump. It is
theme-aware via `useTheme`. Two render sites: `SetupGate` uses it while
`provisioned === null` (previously `return null`), and `Setup.tsx` while
`awaitingDeepLink && !showProgressScreen` — it never blocks the post-submit
progress screen, which owns the page once a join is in flight. The skeleton is
deliberately **not** a fixed delay: it appears only while the target step is
genuinely unknown and disappears the instant it is known, so a fast network
barely sees it and a slow one never flashes the wrong tab. `awaitingDeepLink` is
seeded from the initial hash — true when the hash names anything other than
`force` or the always-present `wifi` — so even the first paint is correct.

The continue-mode auto-scroll — which otherwise jumps the operator to the first
*incomplete* step — is suppressed when a valid deep-link hash was honored
(`deepLinkedRef`), so `#voice` isn't immediately overridden back to Wi-Fi. The
"all required steps done → bounce to `/monitor`" behavior still applies.

## Post-Setup

After `SetUpCompleted = true`:
1. Connect OpenClaw WebSocket
2. Connect MQTT (subscribe fa_channel)
3. Start voice pipeline (if Deepgram key present)
4. Start ambient idle behaviors
5. Start sensing loop

## Config

Config stored at `config/config.json`. Managed by `system/server/config/config.go`.

| Field | Description |
|-------|-------------|
| `SetUpCompleted` | `true` when setup is done |
| `NetworkSSID` | WiFi SSID |
| `NetworkPassword` | WiFi password |
| `LLMAPIKey` | LLM API key |
| `LLMBaseURL` | LLM API base URL |
| `LLMModel` | Model name |
| `Channel` | telegram (default when empty), slack, discord, whatsapp, imessage |
| `TelegramBotToken` / `TelegramUserID` | Telegram credentials (Slack/Discord/WhatsApp/BlueBubbles have their own `Slack*`, `Discord*`, `WhatsappUserID`, `Bluebubbles*` fields) |
| `DeepgramAPIKey` | Deepgram STT API key |
| `LocalIntent` | Enable/disable local intent matching (default: true) |
| `MQTTEndpoint` | MQTT broker host |
| `MQTTPort` | MQTT broker port |
| `FAChannel` | MQTT subscribe topic (server→device) |
| `FDChannel` | MQTT publish topic (device→server) |

## Parent-window event bridge

When the Setup page is opened as a popup/iframe from another site (e.g.
`autonomous.ai`), it reports each milestone back to the opener via
`window.postMessage`. This is the only cross-origin channel that works
popup→opener, since Setup is served from the device's AP IP
(`http://192.168.100.1`) or its `<type>-<id>.local` host — a different origin.

The opener should pass its origin so the device knows where to post and the
payload isn't broadcast to `*`:

```js
const origin = encodeURIComponent(window.location.origin);
window.open(`http://192.168.100.1/setup?parent_origin=${origin}&...`, "_blank");
```

Origin resolution order: `?parent_origin=` → `document.referrer` origin → `*`.

Every message is a flat JSON envelope:
`{ source: "autonomous-device-setup", v: 1, event, ts, ...data }`. Filter on
`source` and switch on `event`:

| `event` | When | Extra fields |
|---------|------|--------------|
| `setup_opened` | Wizard mounted | `mode`, `deviceId`, `mac` |
| `step_changed` | Operator changed wizard step | `step` |
| `wifi_selected` | A WiFi network was chosen | `ssid` |
| `setup_submitted` | "Setup" clicked, request about to send | `ssid`, `channel` |
| `setup_error` | Validation/backend error surfaced | `message` |
| `setup_connecting` | Device is joining WiFi (post-submit) | — |
| `setup_connected` | Device online + reachable | `mdns_host`, `lan_ip` |
| `setup_failed` | WiFi join failed | `message` |
| `retry_clicked` | "Back to Wi-Fi" after a failure | — |
| `start_over_clicked` | **Never emitted** — the "Start over" button was removed. The event and `resetSetupSession()` are still defined; a caller would hard-reload the popup and **drop every param it was opened with** | — |
| `continue_clicked` | "Continue setup →" clicked | `mdns_host` |
| `monitor_clicked` | "Go to monitor →" clicked | — |
| `setup_done` | Wizard finished — the terminal event a parent waits on to close the popup | — |

`setup_done` fires on **every** route out of a finished wizard: the last-step
button whichever label it carries ("Skip & finish" when the operator declined
the final optional enrollment, "Go to monitor →" when they completed it), and
the continue-mode auto-bounce to `/monitor` when nothing was left to do. It is
ref-guarded, so at most one is emitted per page. It used to fire only for the
skip variant, which meant an operator who actually *finished* the last step — or
a popup that reopened with every step already satisfied, the normal shape after
a wired setup — left the parent with no terminal event at all and the popup
stayed open.

Emits are best-effort: with no opener/parent they're a no-op, and postMessage
failures are swallowed, so the bridge never affects the setup flow itself. A
full listener example lives in the file header of `lib/setupBridge.ts`.

## Code

| File | Role |
|------|------|
| `system/device/setup.go` | Setup orchestration + early LAN-IP capture goroutine |
| `system/web/src/lib/setupBridge.ts` | Parent-window event bridge (postMessage) |
| `system/web/src/pages/setup/Setup.tsx` | Setup wizard UI + bridge emit call sites + IP-first copy link |
| `system/web/src/pages/setup/useSetupController.ts` | Setup page state: sections, step navigation, deep-link hash handling (`deepLinkedRef`, `awaitingDeepLink`) |
| `system/web/src/pages/setup/SetupSkeleton.tsx` | Chrome-shaped placeholder rendered while the target step is unknown (mode resolving / pending deep-link) |
| `system/web/src/hooks/setup/useSetupStatusPolling.ts` | AP→STA auto-redirect: phase poll + LAN-IP probe + mDNS probe |
| `system/web/src/hooks/setup/useWifiConnected.ts` | Post-reload Wi-Fi-done detection from live device state (`check-internet` + `network/current`) |
| `system/network/service.go` | WiFi connect, AP mode, `CurrentNetwork()` (associated SSID) |
| `system/server/device/delivery/http/handler.go` | HTTP setup handler (async goroutine) |
| `system/server/config/config.go` | Config load/save |
| `scripts/imager/build-orangepi.sh`, `scripts/imager/build.sh`, `scripts/provision/setup.sh` | nginx config baked into the image (incl. `connect-src` CSP) |
| `scripts/maintenance/patch-security.sh` | OTA security patch for already-provisioned devices (migrates CSP) |

### iMessage setup credentials

Setup and Wi-Fi provisioning accept the BlueBubbles server URL, password, and operator address for `channel=imessage`. These credentials must be persisted before runtime setup; Wi-Fi provisioning forwards the saved credentials into `SetupAgent`. Hermes consumes the saved config through presync.

## Setup logs reach Graylog even when setup fails

A first setup runs with no device key and, until WiFi joins, no internet, so a
failed setup used to ship **nothing**: os-server and HAL armed the log relay
only at process start, bootstrap never armed it, and a record that could not be
sent was dropped. Now:

- **Spool from boot.** os-server, bootstrap and HAL write records that cannot
  ship yet — no key, no internet, relay or collector error — to a bounded
  on-disk spool in `OS_GELF_SPOOL_DIR` (default `/var/lib/autonomous/gelf-spool` — on the
  SD card, not `/var/log`, which devices keep in RAM (zram) — so a power cycle
  keeps it): `<service>.jsonl` + `<service>.1.jsonl`, at most
  1 MiB per service, oldest records dropped first.
- **Armed as soon as a key exists.** os-server re-arms the relay from its
  config-change listener (the moment setup saves the key, and again when a
  re-setup replaces it); bootstrap and HAL re-read `config.json` every
  minute / 30s. Only the device's Autonomous credential is used, never an
  owner's own provider.
- **Replayed in order.** Once a send succeeds the spool drains before live
  records, paced (~10 records/s — the cloud relay drops silently past its
  in-flight ceiling), each tagged `_spooled: "true"` and moved from the
  pre-config host onto the device id, so a failed first attempt shows up under
  the device next to the attempt that worked.
- **Never another device's records.** Replay ships only records logged before
  the device id was known or under the current device id; records under any
  other id (a previous setup or a previous owner) are dropped, never sent with
  the current key. Factory reset also wipes `OS_GELF_SPOOL_DIR`.

A device that never gets online again cannot deliver its spool; it stays on the
device until it does.
