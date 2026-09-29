# Device security boundaries

## HTTP credentials and request logs

OS HTTP access logs include method, path, status, duration and client IP, but omit
the entire query string. This protects legacy `?token=<llm_api_key>` requests and
provisioning links containing other credentials. Recovery logs retain a stack
trace without dumping request headers, query strings or panic values.

Authentication is unchanged: session cookies, supported Bearer credentials and
the legacy `token` query parameter still work. After authentication, both HAL
proxies (`/api/hardware/*` and `/openapi.json`) remove every `token` query value
before forwarding. Other query arguments remain available to HAL.

Prefer cookies or Authorization headers over query credentials. This protection
does not erase historical logs or sanitize external reverse-proxy logs, browser
history, or application-specific log messages. Rotate credentials if previously
recorded logs were exposed.

## Hardware microphone switch

The hardware microphone mute switch blocks voice startup and scene microphone activation even on devices without camera or speaker privacy overlays. `privacy.mic_locked()` checks the hardware state directly. Software unmute still cannot override a closed hardware switch.

## Audio diagnostic privacy

`POST /audio/record` returns 409 when software mute or the hardware mic switch blocks capture. Duration is limited to 1–30000 ms; invalid values return 422. If mute is observed after capture, the recording is discarded. `POST /audio/play-tone` returns 409 for speaker mute or privacy lock. Tone duration is 1–5000 ms and frequency is 20–20000 Hz (422 outside these bounds). These checks apply before opening audio devices, including simulation. They do not promise immediate cancellation of a recording already in progress.

Diagnostic tone behavior during quiet hours is unchanged; the existing music quiet-hours policy is not broadened by this fix.

## Speaker enrollment privacy

`POST /speaker/record-enroll` returns 409 when software mute or the hardware mic switch blocks capture. It rechecks after releasing the voice listener and after capture; a newly muted recording is discarded before enrollment. The listener restarts only if it was running before enrollment and the microphone remains unmuted. Duration remains bounded to 1–60 seconds. This does not promise immediate interruption of an already running recorder.

## Camera snapshot privacy

`GET /camera/snapshot` returns 409 when the user manually disabled the camera or the physical privacy lock is active. An explicit disable also persists manual ownership when the camera was already automatically paused. Automatic camera pauses can still temporarily start capture until that explicit disable. Snapshot requests serialize temporary start/capture/stop so one request cannot stop another snapshot. The snapshot mutex does not hold the privacy lock; manual/physical disable remains available during capture, and a disabled result is discarded. If another action enabled the camera during capture, snapshot cleanup does not stop it.

## Agent file symlinks

Web Chat (`GET /api/agent/file`) and MQTT (`chat.file.get`) share the resolver. Both requested and resolved target extensions must be allowed: a `.txt` symlink cannot expose a `.json`, `.log` or extensionless target, even inside an allowed root. Valid inside-root symlinks still work and use the target MIME type. Root, regular-file, 32 MiB and authentication checks remain unchanged.

## HAL file paths

Face photo/file routes resolve requested paths and require containment within `USERS_DIR`; matching-prefix sibling directories and symlink escapes are rejected. Voice enrollment uses a private randomly named temporary WAV independent of the supplied name and cleans it up while restoring service state even if temporary-file creation fails.

## Scope of the September 2026 corrections

The fixes address unauthorized ingestion, credential logging, privacy-state bypasses, pairing brute force, declared servo speed enforcement, plugin paths and file resolution. OTA signing policy/metadata, existing admin credentials, LAN onboarding, diagnostic quiet-hours behavior and undeclared joint-angle limits are unchanged. Broad CORS trust and archive supply-chain proposals remain separate review items, not claims of fixed vulnerabilities.

## Buddy handler ownership

The server receives `BuddyHandler` by pointer through Wire so the pairing confirmation limiter and its mutex are never copied during dependency injection. Pairing limits and endpoint behavior are unchanged.

## Servo response accuracy and Piper extraction

Sleeping move/aim/nudge/resume/track requests return 409 without waking the body. Move returns separate requested/observed positions and `clamped: null`; aim/nudge report requested targets separately from readback. Driver limits and calibration are unchanged. Driver-reported positions may be cached targets on drivers such as Reachy, so they are not proof of physical arrival. See [HAL API](../os-server.md) for response compatibility.

Piper engine archives are checked before extraction and filtered while extracting into a fresh staging directory. Paths and links must stay within the `piper` release tree; special files and directory/dangling symlinks are rejected. Valid contained shared-library file links remain supported. The fixed HTTPS release URL is unchanged; checksum pinning has not been added.
