# Security Audit: Lamp Go Server

Date: 2026-05-16  
Repo: `lamp`  
Scope: Lamp Go server only (`system/server`, `system/`, `system/domain`, nginx `/api/` wiring).  
Path note (2026-09-28): file paths below were updated to the current `autonomous-os` layout (`lamp/web/` → `system/web/`, `components/edit/*` → `pages/settings/*`, `system/server/openclaw/delivery/sse/` → `system/server/agent/delivery/http/`). `pages/EditConfig.tsx` has since been removed (settings now live under `system/web/src/pages/settings/`); `lib/hardwareApi.ts`, `lib/http.ts` and `lib/safeSearch.ts` are proposed files, not existing ones.  
Instruction: report issues and exact remediation guidance only; do **not** patch runtime code in this document.

## Executive summary

The Lamp Go server is currently acting as both:

1. A product/setup API exposed through nginx `/api/`.
2. A privileged local admin/control plane that can run shell commands, update config, read logs, expose OpenClaw config, trigger OTA, reconnect WiFi, restart services, and proxy agent state.

Those two roles are mixed under the same external `/api/` namespace with no visible authentication/authorization boundary.

Most important risks:

- **No global auth on `/api/*`** while nginx exposes `/api/` to LAN/AP clients.
- **Wildcard CORS** (`Access-Control-Allow-Origin: *`) allows arbitrary browser origins to call/read API responses.
- **Remote command execution endpoint**: `/api/system/exec` runs `sh -c` on user input.
- **Interactive shell endpoint**: `/api/system/shell` is exposed under `/api/`.
- **Raw OpenClaw config leak**: `/api/agent/config-json` returns `openclaw.json`, likely including gateway tokens and channel credentials.
- **Device config read/write endpoints expose/update secrets**: `/api/device/config` returns API keys, bot tokens, WiFi password, MQTT password, etc.; `PUT /api/device/config` can overwrite them and trigger service restarts / WiFi reconnects.
- **Setup and channel endpoints can re-provision or hijack messaging channels** without auth.
- **Logs endpoints can leak secrets** and internal events.
- **OTA trigger endpoint can start component updates** without auth.
- **Server binds `:<port>`**, i.e. all interfaces, which is okay only if nginx/firewall/auth are correct. Currently the API is intended to be reachable externally, so endpoint-level security is mandatory.

Recommended direction:

- Split endpoints into **public/setup**, **authenticated UI**, and **local-only admin** groups.
- Add auth middleware for all sensitive `/api/*` endpoints.
- Add local-only middleware for endpoints that should never be remote.
- Remove or build-gate shell/exec in production.
- Stop returning raw secrets in API responses.
- Replace wildcard CORS with same-origin or explicit allowlist.

---

## Current exposed routing model

### Nginx exposes Lamp Go server under `/api/`

In `scripts/provision/setup.sh`, nginx forwards external `/api/` to Lamp Go server:

```nginx
location /api/ {
  proxy_pass http://backend;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

`backend` is:

```nginx
upstream backend { server 127.0.0.1:5000; }
```

Equivalent config exists in `scripts/imager/build.sh`.

### Lamp Go server listens on all interfaces

In `system/server/server.go`:

```go
srv := &http.Server{
    Addr:    fmt.Sprintf(":%d", s.config.HttpPort),
    Handler: r,
}
```

This binds to all interfaces. If port `5000` is reachable directly, nginx is not the only exposure path.

### Routes are registered without auth middleware

In `system/server/server.go`, the router uses:

```go
r := gin.Default()
r.RedirectTrailingSlash = false
r.Use(corsMiddleware())
r.Use(gin.Recovery())

api := r.Group("api")
```

No authentication middleware is applied to `api` or sensitive subgroups.

---

## Finding 1 — No authentication/authorization boundary on `/api/*`

### Severity

**Critical**.

### Evidence

`system/server/server.go` creates `api := r.Group("api")` and then registers sensitive routes directly. Examples:

```go
system.POST("software-update/:target", s.softwareUpdate)
system.POST("exec", s.execCommand)
system.GET("shell", systemshell.ShellHandler)

device.POST("setup", s.deviceHandler.Setup)
device.GET("config", s.deviceHandler.GetConfig)
device.PUT("config", s.deviceHandler.UpdateConfig)
device.POST("channel", s.deviceHandler.ChangeChannel)

oc.GET("config-json", s.openclawHandler.ConfigJSON)

logs.GET("tail", s.logTail)
logs.GET("stream", s.logStream)
```

Nginx exposes `/api/` to the network.

### Why it is risky

Any LAN/AP client that can reach the device can call these endpoints unless another layer blocks them. This turns the product web API into an unauthenticated admin interface.

Possible impacts:

- Run arbitrary commands as the Lamp service user/root, depending on service config.
- Read or modify device config and secrets.
- Hijack Telegram/Slack/Discord bot tokens/user IDs.
- Change LLM/STT/TTS API keys/base URLs to attacker-controlled endpoints.
- Trigger OTA update flows.
- Read logs and event streams containing sensitive data.
- Exfiltrate OpenClaw gateway token/config.
- Reconfigure WiFi and disrupt connectivity.

### Required remediation

Introduce explicit API zones:

1. **Public setup endpoints**: minimal endpoints needed before setup, only while device is in provisioning mode/AP mode.
2. **Authenticated UI endpoints**: normal monitor/config endpoints after setup, requiring a device admin session/token.
3. **Local-only admin endpoints**: shell, exec, raw config, internal control, never remotely accessible.
4. **Internal-only ingestion endpoints**: sensing/monitor events from HAL/OpenClaw; must validate loopback or a shared internal token.

### Suggested implementation pattern

#### File: `system/server/server.go`

Create middleware helpers:

```go
func localOnlyMiddleware() gin.HandlerFunc { ... }
func adminAuthMiddleware(cfg *config.Config) gin.HandlerFunc { ... }
func setupWindowMiddleware(cfg *config.Config) gin.HandlerFunc { ... }
```

Then group routes:

```go
api := r.Group("api")

public := api.Group("")
public.GET("health/live", s.healthHandler.Live)
public.GET("health/readiness", s.healthHandler.Readiness)
public.GET("device/setup/status", s.deviceHandler.SetupStatus)
public.POST("device/setup", setupWindowMiddleware(s.config), s.deviceHandler.Setup)

admin := api.Group("", adminAuthMiddleware(s.config))
admin.GET("device/config", s.deviceHandler.GetConfig)
admin.PUT("device/config", s.deviceHandler.UpdateConfig)
admin.POST("device/channel", s.deviceHandler.ChangeChannel)
admin.GET("openclaw/status", s.openclawHandler.Status)
// ... normal monitor UI routes after auth

local := api.Group("", localOnlyMiddleware())
local.POST("system/exec", s.execCommand)
local.GET("system/shell", systemshell.ShellHandler)
local.GET("openclaw/config-json", s.openclawHandler.ConfigJSON)
```

### Acceptance checks

From another LAN machine:

```sh
curl -i http://<device-ip>/api/device/config
curl -i http://<device-ip>/api/agent/config-json
curl -i -X POST http://<device-ip>/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"id"}'
```

Expected after fix:

- `401 Unauthorized` or `403 Forbidden` for unauthenticated remote clients.
- `exec`, `shell`, and raw config should be `403 Forbidden` even with normal UI auth unless local-only/admin break-glass is explicitly enabled.

---

## Finding 2 — Wildcard CORS on all API responses

### Severity

**High**.

### Evidence

`system/server/server.go`:

```go
func corsMiddleware() gin.HandlerFunc {
    return func(c *gin.Context) {
        c.Header("Access-Control-Allow-Origin", "*")
        c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        c.Header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept, Authorization")
        if c.Request.Method == "OPTIONS" {
            c.AbortWithStatus(http.StatusNoContent)
            return
        }
        c.Next()
    }
}
```

### Why it is risky

Any website can make browser requests to the device API and read responses if the victim browser can reach the lamp. This is a classic browser-based LAN attack pattern.

Examples:

- User visits malicious site on the same network as the lamp.
- Site JavaScript calls `http://lamp.local/api/device/config`.
- Because CORS is `*`, browser permits reading the response.
- Secrets from config/logs can be exfiltrated.

Even if some endpoints move behind bearer auth, wildcard CORS still increases risk if tokens are accessible in browser storage or if future cookies are introduced.

### Required remediation

Best option: **remove CORS entirely** if web UI and `/api/` are same-origin through nginx.

If CORS is needed for dev, make it opt-in and restricted.

#### File: `system/server/server.go`

Replace wildcard CORS with same-origin/explicit allowlist:

```go
func corsMiddleware(allowedOrigins map[string]bool) gin.HandlerFunc {
    return func(c *gin.Context) {
        origin := c.GetHeader("Origin")
        if origin != "" && allowedOrigins[origin] {
            c.Header("Access-Control-Allow-Origin", origin)
            c.Header("Vary", "Origin")
            c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            c.Header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept, Authorization")
        }
        if c.Request.Method == "OPTIONS" {
            if origin != "" && !allowedOrigins[origin] {
                c.AbortWithStatus(http.StatusForbidden)
                return
            }
            c.AbortWithStatus(http.StatusNoContent)
            return
        }
        c.Next()
    }
}
```

For production, allowed origins should be exact, e.g.:

```go
map[string]bool{
    "http://127.0.0.1": true,
    "http://localhost": true,
    "http://lamp.local": true,
}
```

Better: generate allowed origin from device hostname and setup origin; do not use `*`.

### Acceptance checks

```sh
curl -i http://<device-ip>/api/health/live -H 'Origin: https://evil.example'
```

Expected:

- No `Access-Control-Allow-Origin: *`.
- For preflight:

```sh
curl -i -X OPTIONS http://<device-ip>/api/device/config \
  -H 'Origin: https://evil.example' \
  -H 'Access-Control-Request-Method: GET'
```

Expected: `403 Forbidden`.

---

## Finding 3 — `/api/system/exec` is unauthenticated remote command execution

### Severity

**Critical**.

### Evidence

Route registration in `system/server/server.go`:

```go
system.POST("exec", s.execCommand)
```

Handler:

```go
// POST /api/system/exec  body: {"cmd": "..."}
func (s *Server) execCommand(c *gin.Context) {
    var body struct {
        Cmd string `json:"cmd"`
    }
    ...
    cmd := exec.CommandContext(ctx, "sh", "-c", body.Cmd)
    ...
}
```

### Why it is risky

This endpoint allows arbitrary shell command execution:

```sh
curl -X POST http://<device-ip>/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"id; uname -a; cat config/config.json"}'
```

If the Lamp service runs as root, this is full root RCE. Even if non-root, it can read local secrets, call local privileged services, or trigger destructive behavior.

### Required remediation

Preferred: **remove this endpoint from production**.

Options:

#### Option A — Delete route from production

In `system/server/server.go`, remove:

```go
system.POST("exec", s.execCommand)
```

Keep it only under a dev build tag or local-only debug binary.

#### Option B — Local-only plus disabled-by-default

Add config field:

```go
EnableDebugExec bool `json:"enable_debug_exec,omitempty"`
```

Guard route:

```go
if s.config.EnableDebugExec {
    system.POST("exec", localOnlyMiddleware(), s.execCommand)
}
```

#### Option C — Replace shell with an allowlist

Do not use `sh -c`. Expose specific operations with fixed command/args only:

```go
allowed := map[string][]string{
    "status": {"systemctl", "status", "lamp", "--no-pager"},
}
```

Then run with `exec.CommandContext(ctx, argv[0], argv[1:]...)`.

### Acceptance checks

From LAN:

```sh
curl -i -X POST http://<device-ip>/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"id"}'
```

Expected:

- `404 Not Found` if removed, or
- `403 Forbidden` if local-only, or
- `401 Unauthorized` if admin-auth protected and disabled for normal admins.

---

## Finding 4 — `/api/system/shell` exposes an interactive shell

### Severity

**Critical**.

### Evidence

Route registration:

```go
system.GET("shell", systemshell.ShellHandler)
```

Nginx has a special WebSocket location:

```nginx
location = /api/system/shell {
  proxy_pass http://backend;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_read_timeout 86400s;
  proxy_send_timeout 86400s;
}
```

### Why it is risky

This intentionally creates a long-lived interactive shell channel through the product API. Without strong auth and local-only restrictions, it is equivalent to giving shell access over HTTP/WebSocket.

### Required remediation

Same as `/api/system/exec`, but even stricter:

1. Remove from production by default.
2. If retained for bench/dev:
   - Require local-only middleware.
   - Require explicit config flag: `EnableDebugShell`.
   - Require admin token even locally if possible.
   - Log all connection attempts.
3. Update nginx special location to deny external clients:

```nginx
location = /api/system/shell {
  allow 127.0.0.1;
  allow ::1;
  deny all;

  proxy_pass http://backend;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_read_timeout 86400s;
  proxy_send_timeout 86400s;
}
```

### Acceptance checks

From LAN browser or curl:

```sh
curl -i http://<device-ip>/api/system/shell
```

Expected: `403`, `404`, or WebSocket rejection before shell starts.

---

## Finding 5 — `/api/agent/config-json` returns raw `openclaw.json`

### Severity

**High to Critical** depending on contents.

### Evidence

Route:

```go
oc.GET("config-json", s.openclawHandler.ConfigJSON)
```

Handler:

```go
func (h *OpenClawHandler) ConfigJSON(c *gin.Context) {
    data, err := h.agentGateway.GetConfigJSON()
    ...
    c.JSON(http.StatusOK, serializers.ResponseSuccess(data))
}
```

Service:

```go
func (s *Service) GetConfigJSON() (json.RawMessage, error) {
    path := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
    data, err := os.ReadFile(path)
    ...
    return json.RawMessage(data), nil
}
```

### Why it is risky

`openclaw.json` can include:

- Gateway auth token
- Channel tokens
- Model provider keys
- Local paths
- Control UI config
- Agent configuration details

Returning it raw to the web UI means any network client with `/api/` access can potentially steal secrets.

### Required remediation

Do **not** expose raw config over remote API.

#### Option A — Local-only only

```go
oc.GET("config-json", localOnlyMiddleware(), s.openclawHandler.ConfigJSON)
```

#### Option B — Return redacted config

Create a sanitized endpoint:

```go
oc.GET("config-summary", adminAuthMiddleware(s.config), s.openclawHandler.ConfigSummary)
```

Redact secret-like fields recursively:

```go
func redactJSON(v any) any {
    switch x := v.(type) {
    case map[string]any:
        out := map[string]any{}
        for k, val := range x {
            lk := strings.ToLower(k)
            if strings.Contains(lk, "token") || strings.Contains(lk, "key") || strings.Contains(lk, "secret") || strings.Contains(lk, "password") {
                out[k] = "***REDACTED***"
            } else {
                out[k] = redactJSON(val)
            }
        }
        return out
    case []any:
        for i := range x {
            x[i] = redactJSON(x[i])
        }
        return x
    default:
        return x
    }
}
```

#### Frontend changes

Files currently fetching config-json:

- `system/web/src/pages/GwConfig.tsx`
- `system/web/src/pages/monitor/index.tsx`
- `system/web/src/pages/monitor/ChatSection.tsx`

Change them to call sanitized endpoint or require local-only dev mode.

### Acceptance checks

From LAN:

```sh
curl -i http://<device-ip>/api/agent/config-json
```

Expected:

- `403 Forbidden`, or
- redacted output with no tokens/API keys/passwords.

---

## Finding 6 — `/api/device/config` exposes device secrets

### Severity

**Critical**.

### Evidence

Route:

```go
device.GET("config", s.deviceHandler.GetConfig)
```

Handler:

```go
func (h *DeviceHandler) GetConfig(c *gin.Context) {
    cfg := h.service.GetConfig()
    c.JSON(http.StatusOK, serializers.ResponseSuccess(cfg))
}
```

Response struct includes secrets in `system/domain/device.go`:

```go
type ConfigResponse struct {
    TelegramBotToken string `json:"telegram_bot_token"`
    SlackBotToken    string `json:"slack_bot_token"`
    SlackAppToken    string `json:"slack_app_token"`
    DiscordBotToken  string `json:"discord_bot_token"`
    LLMAPIKey        string `json:"llm_api_key"`
    DeepgramAPIKey   string `json:"deepgram_api_key"`
    STTAPIKey        string `json:"stt_api_key"`
    TTSAPIKey        string `json:"tts_api_key"`
    NetworkPassword  string `json:"network_password"`
    MQTTPassword     string `json:"mqtt_password"`
    ...
}
```

### Why it is risky

This endpoint is a single-call secret dump. If reachable externally, an attacker can steal:

- Messaging bot tokens and user/channel IDs
- LLM API key/base URL/model
- Deepgram/STT/TTS credentials
- WiFi password
- MQTT credentials
- Device ID and MAC

### Required remediation

Split config into two APIs:

1. **Public/sanitized config view**: no secrets, only booleans or masked values.
2. **Secret update endpoint**: write-only; never returns stored secret values.

#### File: `system/domain/device.go`

Create sanitized response:

```go
type ConfigPublicResponse struct {
    Channel            string `json:"channel"`
    TelegramConfigured bool   `json:"telegram_configured"`
    SlackConfigured    bool   `json:"slack_configured"`
    DiscordConfigured  bool   `json:"discord_configured"`
    LLMConfigured      bool   `json:"llm_configured"`
    LLMModel           string `json:"llm_model"`
    LLMBaseURL         string `json:"llm_base_url"` // optionally hide host if sensitive
    DeepgramConfigured bool   `json:"deepgram_configured"`
    STTConfigured      bool   `json:"stt_configured"`
    TTSConfigured      bool   `json:"tts_configured"`
    TTSProvider        string `json:"tts_provider"`
    TTSVoice           string `json:"tts_voice"`
    DeviceID           string `json:"device_id"`
    NetworkSSID        string `json:"network_ssid"`
    MQTTConfigured     bool   `json:"mqtt_configured"`
    MQTTPort           int    `json:"mqtt_port"`
}
```

Use booleans or masked values:

```go
func maskSecret(v string) string {
    if v == "" { return "" }
    if len(v) <= 6 { return "***" }
    return v[:3] + "***" + v[len(v)-3:]
}
```

#### File: `system/device/config_update.go`

Change `GetConfig()` to return sanitized response for remote UI. If a full config is needed internally, expose a separate internal method not bound to HTTP.

#### File: `system/server/device/delivery/http/handler.go`

Protect config endpoint:

```go
device.GET("config", adminAuthMiddleware(s.config), s.deviceHandler.GetConfig)
```

Even after auth, do not return raw secrets by default.

### Acceptance checks

```sh
curl -s http://<device-ip>/api/device/config | grep -Ei 'api_key|token|password'
```

Expected:

- No raw secret values.
- Ideally no fields named `*_api_key`, `*_token`, `network_password`, `mqtt_password` in the public response.

---

## Finding 7 — `PUT /api/device/config` can overwrite secrets and trigger side effects

### Severity

**High**.

### Evidence

Route:

```go
device.PUT("config", s.deviceHandler.UpdateConfig)
```

Handler accepts `domain.UpdateConfigRequest` and calls `h.service.UpdateConfig(req)`.

`UpdateConfig` can update:

- API keys and base URLs
- Channel tokens/IDs
- WiFi SSID/password
- MQTT credentials
- Device ID
- STT/TTS settings

It can trigger:

```go
s.networkService.SetupNetwork(ssid, password)
s.agentGateway.RefreshModelsConfig()
s.agentGateway.NewSession(key)
s.restartHAL("voice config change")
```

`restartHAL` restarts service:

```go
exec.Command("systemctl", "restart", "hal").CombinedOutput()
```

### Why it is risky

An unauthenticated remote caller can:

- Replace API keys with attacker keys.
- Change base URLs to attacker-controlled endpoints for credential/data interception.
- Hijack Telegram/Slack/Discord channel configuration.
- Reconnect WiFi to attacker SSID or break connectivity.
- Force service restarts repeatedly.
- Change DeviceID and disrupt backend pairing/reporting.

### Required remediation

1. Require admin auth.
2. Add CSRF protection if using cookies.
3. Validate and constrain base URLs.
4. Apply rate limits.
5. Split high-risk operations into explicit endpoints.
6. Avoid immediate side effects unless the request is confirmed.

#### Recommended endpoint split

Instead of one large `PUT /api/device/config`, use:

```text
PUT /api/device/model-config       # LLM/STT/TTS model/base URL settings
PUT /api/device/channel-config     # Telegram/Slack/Discord tokens
PUT /api/device/network            # WiFi only; requires physical/setup mode confirmation
PUT /api/device/mqtt               # MQTT only
PUT /api/device/display-name       # device_id/name only
```

#### Validate URLs

For base URL fields:

- Require `https://` unless explicitly local.
- Reject link-local metadata IPs, loopback unless local mode, private IPs if cloud-bound.
- Reject malformed URL.

Example:

```go
func validateExternalBaseURL(raw string) error {
    u, err := url.Parse(raw)
    if err != nil || u.Scheme == "" || u.Host == "" {
        return fmt.Errorf("invalid URL")
    }
    if u.Scheme != "https" {
        return fmt.Errorf("base URL must use https")
    }
    return nil
}
```

#### Rate limit restarts

Do not let config updates restart `hal` unlimited times. Add debounce:

```go
// only restart once after config settles, e.g. after 2s debounce
```

### Acceptance checks

Unauthenticated:

```sh
curl -i -X PUT http://<device-ip>/api/device/config \
  -H 'Content-Type: application/json' \
  -d '{"llm_base_url":"https://evil.example","llm_api_key":"x"}'
```

Expected: `401`/`403`.

Authenticated but invalid URL:

Expected: `400 Bad Request`.

---

## Finding 8 — `POST /api/device/setup` and `/api/device/channel` can hijack provisioning/channel

### Severity

**High**.

### Evidence

Routes:

```go
device.POST("setup", s.deviceHandler.Setup)
device.POST("channel", s.deviceHandler.ChangeChannel)
```

`SetupRequest` requires many secrets:

```go
SSID, Password, TelegramBotToken, SlackBotToken, SlackAppToken, DiscordBotToken,
LLMBaseURL, LLMAPIKey, DeviceID, MQTTPassword, ...
```

`ChangeChannel` accepts `AddChannelRequest` and runs `h.service.AddChannel(req)`.

### Why it is risky

If exposed after initial provisioning:

- Attacker can rebind the lamp to their messaging bot/channel.
- Attacker can replace LLM key/base URL.
- Attacker can force WiFi reconfiguration.
- Attacker can break owner access.

### Required remediation

#### Setup endpoint

Only allow `POST /api/device/setup` when:

- `SetUpCompleted == false`, or
- device is physically in setup/reset mode, or
- request has strong admin auth.

Implement middleware:

```go
func setupOnlyMiddleware(cfg *config.Config) gin.HandlerFunc {
    return func(c *gin.Context) {
        if cfg.SetUpCompleted {
            c.JSON(http.StatusForbidden, serializers.ResponseError("setup already completed"))
            c.Abort()
            return
        }
        c.Next()
    }
}
```

Route:

```go
device.POST("setup", setupOnlyMiddleware(s.config), s.deviceHandler.Setup)
```

#### Channel endpoint

Require admin auth:

```go
device.POST("channel", adminAuthMiddleware(s.config), s.deviceHandler.ChangeChannel)
```

Also log channel changes without logging token values.

### Acceptance checks

After setup completed:

```sh
curl -i -X POST http://<device-ip>/api/device/setup -H 'Content-Type: application/json' -d '{...}'
```

Expected: `403 Forbidden` unless physical reset/setup mode is active.

---

## Finding 9 — Logs endpoints can leak secrets and private data

### Severity

**Medium to High**.

### Evidence

Routes:

```go
logs.GET("tail", s.logTail)
logs.GET("stream", s.logStream)
```

Allowed sources include service logs, OpenClaw logs, and journals:

```go
allowedLogs := map[string]string{
    ...
    "openclaw-service": "journal:openclaw.service",
    ...
}
```

Handlers return or stream log lines directly.

### Why it is risky

Logs may include:

- API keys in debug output
- Bot tokens
- User messages
- Tool calls
- URLs with tokens
- Internal file paths
- Errors containing config content
- Network SSIDs or other personal data

Even if logs are whitelisted by source, they are not redacted or auth-gated.

### Required remediation

1. Require admin auth for logs.
2. Consider local-only for OpenClaw raw logs.
3. Redact secret patterns before returning/streaming.
4. Limit line count and rate.
5. Do not expose journal source remotely unless needed.

#### Redaction helper

```go
var secretPatterns = []*regexp.Regexp{
    regexp.MustCompile(`(?i)(api[_-]?key|token|secret|password)(["'=:\s]+)([^"'\s,}]+)`),
    regexp.MustCompile(`(?i)(authorization:\s*bearer\s+)([^\s]+)`),
}

func redactLogLine(line string) string {
    out := line
    for _, re := range secretPatterns {
        out = re.ReplaceAllString(out, `$1$2***REDACTED***`)
    }
    return out
}
```

Use this in both `logTail` and `logStream` before sending output.

### Acceptance checks

```sh
curl -s http://<device-ip>/api/logs/tail?source=openclaw-service | grep -Ei 'token|api_key|password|Bearer'
```

Expected:

- Endpoint requires auth.
- Any secret-like values are redacted.

---

## Finding 10 — `/api/system/software-update/:target` can trigger OTA updates remotely

### Severity

**Medium to High**.

### Evidence

Route:

```go
system.POST("software-update/:target", s.softwareUpdate)
```

Handler:

```go
allowed := map[string]bool{"lamp": true, "web": true, "lelamp": true}
url := "http://127.0.0.1:8080/force-check/" + target
http.DefaultClient.Do(req)
```

Frontend exposes buttons:

- `system/web/src/pages/monitor/components.tsx`

### Why it is risky

An unauthenticated client can repeatedly trigger OTA checks/updates. Potential impacts:

- Denial of service via repeated update attempts.
- Service restarts at attacker-chosen times.
- Increased load/bandwidth.
- If OTA metadata or network is compromised, update path becomes a high-value target.

### Required remediation

Require admin auth and rate limit:

```go
system.POST("software-update/:target", adminAuthMiddleware(s.config), rateLimitMiddleware(...), s.softwareUpdate)
```

Also validate target as currently done; keep allowlist.

### Acceptance checks

From LAN without auth:

```sh
curl -i -X POST http://<device-ip>/api/system/software-update/lamp
```

Expected: `401`/`403`.

---

## Finding 11 — Sensing, mood, wellbeing, posture, monitor event ingestion endpoints appear unauthenticated

### Severity

**Medium to High**.

### Evidence

Routes in `system/server/server.go`:

```go
sensing.POST("event", s.sensingHandler.PostEvent)
sensing.GET("snapshot/:category/:name", s.sensingHandler.GetSnapshot)

guard.POST("enable", s.sensingHandler.EnableGuard)
guard.POST("disable", s.sensingHandler.DisableGuard)
guard.POST("alert", s.sensingHandler.PostGuardAlert)

moodGroup.POST("log", s.sensingHandler.PostMoodLog)
wellbeingGroup.POST("log", s.sensingHandler.PostWellbeingLog)
postureGroup.POST("log", s.sensingHandler.PostPostureLog)
musicSuggGroup.POST("log", s.sensingHandler.PostMusicSuggestionLog)
musicSuggGroup.POST("status", s.sensingHandler.PostMusicSuggestionStatus)
monitor.POST("event", s.sensingHandler.PostMonitorEvent)
```

### Why it is risky

If externally reachable, attackers can:

- Inject fake sensing events.
- Trigger guard alerts.
- Pollute mood/wellbeing/posture history.
- Cause agent actions by crafting events that flow into OpenClaw.
- Retrieve snapshots if `snapshot/:category/:name` is guessable and not protected.

### Required remediation

Classify each endpoint:

1. **Internal ingestion from HAL/OpenClaw**: require local-only or internal shared token.
2. **UI read endpoints**: require admin auth.
3. **User-facing logging endpoints**: require admin auth and validate payload shape.

Recommended route guards:

```go
sensing.POST("event", internalOnlyMiddleware(s.config), s.sensingHandler.PostEvent)
monitor.POST("event", internalOnlyMiddleware(s.config), s.sensingHandler.PostMonitorEvent)

moodGroup.POST("log", internalOnlyMiddleware(s.config), s.sensingHandler.PostMoodLog)
wellbeingGroup.POST("log", internalOnlyMiddleware(s.config), s.sensingHandler.PostWellbeingLog)
postureGroup.POST("log", internalOnlyMiddleware(s.config), s.sensingHandler.PostPostureLog)

sensing.GET("snapshot/:category/:name", adminAuthMiddleware(s.config), s.sensingHandler.GetSnapshot)
```

Where `internalOnlyMiddleware` checks either:

- Loopback client IP and forwarded headers, or
- `X-Lamp-Internal-Token` shared secret.

### Acceptance checks

From LAN:

```sh
curl -i -X POST http://<device-ip>/api/monitor/event -H 'Content-Type: application/json' -d '{}'
curl -i -X POST http://<device-ip>/api/guard/alert -H 'Content-Type: application/json' -d '{}'
```

Expected: `401`/`403`.

---

## Finding 12 — Server bind address should be explicit and configurable

### Severity

**Medium**.

### Evidence

`system/server/server.go`:

```go
Addr: fmt.Sprintf(":%d", s.config.HttpPort)
```

`system/server/config/config.go` has only:

```go
HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`
```

No `HttpHost` / bind address setting.

### Why it is risky

Binding to all interfaces is okay for the public UI server only if every exposed route is properly protected. Today `/api/` mixes public UI and privileged admin/internal endpoints.

Even after auth hardening, it is better to make bind intent explicit.

### Required remediation

Add config:

```go
type Config struct {
    HttpHost string `json:"httpHost" yaml:"httpHost"`
    HttpPort int    `json:"httpPort" yaml:"httpPort" validate:"required"`
}
```

Default:

```go
HttpHost: "127.0.0.1",
HttpPort: 5000,
```

Server:

```go
host := s.config.HttpHost
if host == "" {
    host = "127.0.0.1"
}
srv := &http.Server{
    Addr: net.JoinHostPort(host, strconv.Itoa(s.config.HttpPort)),
    Handler: r,
}
```

If the product requires direct LAN access to Go server, set `HttpHost: "0.0.0.0"` only in a profile with auth enabled.

### Important compatibility note

Current nginx points to `127.0.0.1:5000`, so setting Lamp Go to `127.0.0.1` should not break nginx-based UI access.

### Acceptance checks

```sh
ss -ltnp | grep ':5000'
```

Expected if using nginx only:

```text
127.0.0.1:5000
```

From LAN:

```sh
curl -i http://<device-ip>:5000/api/health/live
```

Expected: connection refused/timeout if direct port is not intended.

Through nginx:

```sh
curl -i http://<device-ip>/api/health/live
```

Expected: still works.

---

## Finding 13 — Bootstrap server force-check API also binds all interfaces

### Severity

**Medium** if port `8080` is reachable directly.

### Evidence

`system/bootstrap/bootstrap.go`:

```go
srv := &http.Server{Addr: fmt.Sprintf(":%d", port), Handler: r}
```

Routes:

```go
r.POST("/force-check", ...)
r.POST("/force-check/:target", ...)
```

Lamp Go calls it via:

```go
http://127.0.0.1:8080/force-check/<target>
```

### Why it is risky

If bootstrap port is reachable from LAN, external clients can trigger update checks directly, bypassing Lamp Go protections.

### Required remediation

Bind bootstrap health/update server to loopback:

```go
srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port), Handler: r}
```

Or add `HttpHost` to bootstrap config and default `127.0.0.1`.

If external health check is required, expose only `/health` via nginx with no `/force-check`.

### Acceptance checks

From LAN:

```sh
curl -i -X POST http://<device-ip>:8080/force-check/lamp
```

Expected: connection refused/timeout.

From local device:

```sh
curl -i -X POST http://127.0.0.1:8080/force-check/lamp
```

Expected: works.

---

## Recommended middleware implementations

### Local-only middleware

File: `system/server/server.go` or new file `system/server/security.go`.

```go
func isLoopbackHost(host string) bool {
    host = strings.Trim(host, "[]")
    if host == "localhost" {
        return true
    }
    ip := net.ParseIP(host)
    return ip != nil && ip.IsLoopback()
}

func firstForwardedFor(v string) string {
    if v == "" {
        return ""
    }
    return strings.TrimSpace(strings.Split(v, ",")[0])
}

func hostOnly(addr string) string {
    if h, _, err := net.SplitHostPort(addr); err == nil {
        return h
    }
    return strings.Trim(addr, "[]")
}

func localOnlyMiddleware() gin.HandlerFunc {
    return func(c *gin.Context) {
        remoteHost := hostOnly(c.Request.RemoteAddr)
        xff := firstForwardedFor(c.GetHeader("X-Forwarded-For"))
        realIP := strings.TrimSpace(c.GetHeader("X-Real-IP"))

        if !isLoopbackHost(remoteHost) ||
            (xff != "" && !isLoopbackHost(xff)) ||
            (realIP != "" && !isLoopbackHost(realIP)) {
            c.JSON(http.StatusForbidden, serializers.ResponseError("local-only endpoint"))
            c.Abort()
            return
        }
        c.Next()
    }
}
```

Important: checking `X-Forwarded-For` matters because nginx is local to the service. Without this check, a LAN request proxied through nginx may look like a loopback TCP peer.

### Admin token middleware

Store an admin token in config or a separate root-only file. Prefer separate file to avoid returning it with device config.

Example file:

```text
/root/config/lamp-admin-token
```

Permissions:

```sh
chmod 600 /root/config/lamp-admin-token
```

Middleware:

```go
func adminAuthMiddleware(tokenProvider func() string) gin.HandlerFunc {
    return func(c *gin.Context) {
        expected := tokenProvider()
        if expected == "" {
            c.JSON(http.StatusForbidden, serializers.ResponseError("admin auth not configured"))
            c.Abort()
            return
        }
        got := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
        if got == "" || subtle.ConstantTimeCompare([]byte(got), []byte(expected)) != 1 {
            c.JSON(http.StatusUnauthorized, serializers.ResponseError("unauthorized"))
            c.Abort()
            return
        }
        c.Next()
    }
}
```

### Internal token middleware

For HAL/OpenClaw -> Lamp internal ingestion, either use local-only or a shared header:

```go
func internalTokenMiddleware(expected string) gin.HandlerFunc {
    return func(c *gin.Context) {
        got := c.GetHeader("X-Lamp-Internal-Token")
        if expected == "" || subtle.ConstantTimeCompare([]byte(got), []byte(expected)) != 1 {
            c.JSON(http.StatusUnauthorized, serializers.ResponseError("unauthorized"))
            c.Abort()
            return
        }
        c.Next()
    }
}
```

Do not put this token in frontend JavaScript.

---

## Route classification proposal

| Route group / endpoint | Current exposure | Recommended classification | Recommended protection |
|---|---|---|---|
| `GET /api/health/live` | External | Public | No auth, no secrets |
| `GET /api/health/readiness` | External | Public or admin | Public only if no secrets |
| `GET /api/system/info` | External | Admin | Admin auth; redact sensitive fields |
| `GET /api/system/network` | External | Admin | Admin auth |
| `GET /api/system/dashboard` | External | Admin | Admin auth |
| `POST /api/system/software-update/:target` | External | Admin | Admin auth + rate limit |
| `POST /api/system/exec` | External | Local-only dev | Remove or local-only + flag |
| `GET /api/system/shell` | External | Local-only dev | Remove or local-only + flag |
| `POST /api/device/setup` | External | Setup-only | Only before setup / physical setup mode |
| `GET /api/device/setup/status` | External | Setup public | No secrets |
| `POST /api/device/channel` | External | Admin | Admin auth |
| `GET /api/device/config` | External | Admin | Admin auth + redacted response |
| `PUT /api/device/config` | External | Admin | Admin auth + validation + rate limit |
| `POST /api/sensing/event` | External | Internal | Local-only or internal token |
| `GET /api/sensing/snapshot/:category/:name` | External | Admin | Admin auth; validate path/name |
| `POST /api/guard/*` | External | Admin/internal | Admin auth or internal token |
| `POST /api/mood/log` | External | Internal/admin | Internal token/admin auth |
| `POST /api/wellbeing/log` | External | Internal/admin | Internal token/admin auth |
| `POST /api/posture/log` | External | Internal/admin | Internal token/admin auth |
| `POST /api/monitor/event` | External | Internal | Internal token/local-only |
| `GET /api/agent/status` | External | Admin or public summary | Redact sensitive values |
| `GET /api/agent/events` | External | Admin | Admin auth; may leak event content |
| `GET /api/agent/config-json` | External | Local-only dev | Remove/raw local-only; provide redacted summary |
| `GET /api/logs/tail` | External | Admin | Admin auth + redaction |
| `GET /api/logs/stream` | External | Admin | Admin auth + redaction |

---

## Suggested implementation order

### Phase 1 — Immediate critical fixes

1. Remove or local-only guard:
   - `/api/system/exec`
   - `/api/system/shell`
   - `/api/agent/config-json`
2. Change `/api/device/config` to auth + redacted response.
3. Remove wildcard CORS.
4. Add admin auth middleware for config/channel/update/log/monitor routes.

### Phase 2 — Internal boundary

1. Add local-only or internal token middleware for ingestion endpoints:
   - sensing event
   - monitor event
   - mood/wellbeing/posture/music logs
2. Bind Go server to `127.0.0.1` if nginx is the only intended entrypoint.
3. Bind bootstrap server to `127.0.0.1`.

### Phase 3 — Defense-in-depth

1. Add rate limits to:
   - config updates
   - OTA trigger
   - logs stream
   - setup/channel endpoints
2. Add secret redaction utility shared by config/log/openclaw responses.
3. Add security regression tests.
4. Update docs to describe which APIs are public/admin/local-only.

---

## Suggested regression tests

### 1. No unauthenticated secret dump

```sh
curl -s -o /tmp/config.json -w '%{http_code}' http://<device-ip>/api/device/config
```

Expected: `401`/`403`, or redacted body if authenticated.

### 2. No remote RCE

```sh
curl -i -X POST http://<device-ip>/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"id"}'
```

Expected: `403`/`404`.

### 3. No remote shell

```sh
curl -i http://<device-ip>/api/system/shell
```

Expected: `403`/`404` or WebSocket rejection.

### 4. No raw OpenClaw config remotely

```sh
curl -i http://<device-ip>/api/agent/config-json
```

Expected: `403`/`404` or redacted response.

### 5. No wildcard CORS

```sh
curl -i http://<device-ip>/api/health/live -H 'Origin: https://evil.example'
```

Expected: no `Access-Control-Allow-Origin: *`.

### 6. Setup locked after provisioning

```sh
curl -i -X POST http://<device-ip>/api/device/setup \
  -H 'Content-Type: application/json' \
  -d '{"ssid":"x","password":"x","llm_base_url":"https://x","llm_api_key":"x","device_id":"x","telegram_bot_token":"x","telegram_user_id":"x"}'
```

Expected after setup complete: `403`.

### 7. Logs require auth/redaction

```sh
curl -i http://<device-ip>/api/logs/tail?source=openclaw-service
```

Expected: `401`/`403` without auth; redacted with auth.

---

## Files to edit checklist

### Core server security

- `system/server/server.go`
  - Replace wildcard CORS.
  - Add `localOnlyMiddleware`.
  - Add/admin wire auth middleware.
  - Protect/remove `system exec`, `system shell`, `openclaw config-json`.
  - Protect logs and OTA routes.
  - Optionally bind `Addr` using explicit `HttpHost`.

- `system/server/config/config.go`
  - Add `HttpHost` if direct bind control is desired.
  - Add admin token path/config if implementing token auth.
  - Avoid exposing admin token via `ConfigResponse`.

### Device config/secret handling

- `system/domain/device.go`
  - Replace `ConfigResponse` for HTTP with redacted/sanitized response.
  - Keep internal config struct separate from API response.

- `system/device/config_update.go`
  - Update `GetConfig()` to return sanitized data for HTTP.
  - Add validation to `UpdateConfig()` for base URLs and high-risk fields.
  - Debounce/rate-limit service restarts triggered by config updates.

- `system/server/device/delivery/http/handler.go`
  - Require auth for `GetConfig`, `UpdateConfig`, `ChangeChannel`.
  - Restrict `Setup` to setup mode only.

### OpenClaw config exposure

- `runtimes/openclaw/service_chat.go`
  - Avoid returning raw `openclaw.json` to remote handlers.
  - Add redacted config summary method.

- `system/server/agent/delivery/http/handler_api_monitor.go`
  - Replace raw `ConfigJSON` response or local-only guard it.

- Frontend callers to update if endpoint changes:
  - `system/web/src/pages/GwConfig.tsx`
  - `system/web/src/pages/monitor/index.tsx`
  - `system/web/src/pages/monitor/ChatSection.tsx`

### Logs

- `system/server/server.go`
  - Protect `logs.GET("tail")` and `logs.GET("stream")`.
  - Redact secret-like patterns in log output.

### Bootstrap

- `system/bootstrap/bootstrap.go`
  - Bind health/update server to `127.0.0.1` or protect `/force-check`.

### Nginx integration

- `scripts/provision/setup.sh`
  - If shell remains, add `allow/deny` to `location = /api/system/shell`.
  - Consider restricting `/api/` by route at nginx only as defense-in-depth; app auth should be primary.

- `scripts/imager/build.sh`
  - Mirror production nginx restrictions.

---

## Open decisions

1. Should the Lamp web UI be remotely accessible on LAN after setup, or only during setup/AP mode?
2. If remotely accessible, what admin authentication model should be used?
   - Static bearer token?
   - Pairing code shown physically/on display?
   - Session cookie with CSRF?
3. Should shell/exec exist in production at all?
   - Recommendation: no. Use SSH or OpenClaw local admin tooling instead.
4. Should `/api/device/config` ever return raw secrets?
   - Recommendation: no. Use write-only secret fields and masked status.
5. Should OpenClaw raw config be editable from web UI?
   - Recommendation: only local dev, or use redacted structured config editor with auth.
6. Which endpoints are called by HAL/OpenClaw locally and can be marked internal-only?
   - Need final route inventory with frontend and internal callers.

---

## Bottom line

The Lamp Go server should not treat LAN as trusted. Today it exposes privileged control-plane routes under the same `/api/` surface as normal UI/setup routes. The highest-priority fixes are:

1. Remove/local-only guard `exec` and `shell`.
2. Stop exposing raw `device/config` and `openclaw/config-json` secrets.
3. Add authentication for admin UI routes.
4. Replace wildcard CORS.
5. Add internal/local-only guards for ingestion/control endpoints.
6. Bind direct Go/bootstrap ports to loopback where possible.
