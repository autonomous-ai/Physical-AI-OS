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

`POST /audio/record` returns 409 when software mute or the hardware mic switch blocks capture. Duration is limited to 100–30000 ms; invalid values return 422. If mute is observed after capture, the recording is discarded. `POST /audio/play-tone` returns 409 for speaker mute or privacy lock. Tone duration is 1–5000 ms and frequency is 20–20000 Hz (422 outside these bounds). These checks apply before opening audio devices, including simulation. They do not promise immediate cancellation of a recording already in progress.

Diagnostic tone behavior during quiet hours is unchanged; the existing music quiet-hours policy is not broadened by this fix.

## Speaker enrollment privacy

`POST /speaker/record-enroll` returns 409 when software mute or the hardware mic switch blocks capture. It rechecks after releasing the voice listener and after capture; a newly muted recording is discarded before enrollment. The listener restarts only if it was running before enrollment and the microphone remains unmuted. Duration remains bounded to 1–60 seconds. This does not promise immediate interruption of an already running recorder.

## Camera snapshot privacy

`GET /camera/snapshot` returns 409 when the user manually disabled the camera or the physical privacy lock is active. Automatic camera pauses can still temporarily start capture. Snapshot requests serialize temporary start/capture/stop so one request cannot stop another snapshot. The snapshot mutex does not hold the privacy lock; manual/physical disable remains available during capture, and a disabled result is discarded. If another action enabled the camera during capture, snapshot cleanup does not stop it.

## Agent file symlinks

Web Chat (`GET /api/agent/file`) and MQTT (`chat.file.get`) share the resolver. Both requested and resolved target extensions must be allowed: a `.txt` symlink cannot expose a `.json`, `.log` or extensionless target, even inside an allowed root. Valid inside-root symlinks still work and use the target MIME type. Root, regular-file, 32 MiB and authentication checks remain unchanged.
