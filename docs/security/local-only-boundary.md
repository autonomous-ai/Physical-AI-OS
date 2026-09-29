# Security Audit: Local-only API Boundary for Lamp / HAL / OpenClaw

Date: 2026-05-16  
Repo: `lamp`  
Scope requested: identify issues and remediation plan only; do **not** patch runtime code in this report.

## Executive summary

The current project exposes several high-risk local-control surfaces too broadly:

1. **HAL Python hardware API** is started on `0.0.0.0:5001` in multiple places. This means any device on the same LAN/AP can call endpoints that control camera, mic, speaker, LED, servo, voice, bluetooth, system actions, etc.
2. **Nginx proxies `/hw/` to HAL** without access control. Even if HAL is later changed to bind only `127.0.0.1`, nginx can still expose it externally unless `/hw/` is blocked.
3. **Lamp Go API has wildcard CORS (`*`)** and exposes powerful endpoints including `system exec`, web shell, and OpenClaw config JSON through `/api/`.
4. **OpenClaw gateway `/gw/` is proxied by nginx** with no nginx-level LAN block. Depending on gateway auth/config and browser context, this can expose agent control surfaces.
5. **DL backend defaults to `0.0.0.0` and treats missing `DL_API_KEY` as auth disabled.** This is acceptable only for local dev, dangerous if reachable from LAN/Internet.

Recommended target posture:

- **HAL (`:5001`)**: callable only from same machine by Lamp Go server and OpenClaw.
- **DL backend (`:8001`)**: bind loopback by default; require `DL_API_KEY` for any non-loopback exposure.
- **Lamp Go server (`:5000` behind nginx `/api/`)**: keep externally reachable only for intended setup/UI APIs; block or authenticate dangerous admin endpoints.
- **Nginx**: deny external access to `/hw/` and `/gw/` unless an explicit authenticated remote-admin mode is designed.

---

## Finding 1 — HAL Python hardware API binds to all network interfaces

### Severity

**Critical** for LAN/AP threat model.

### Evidence

Current references found:

- `Makefile:52`
  ```make
  cd $(HAL_DIR) && PYTHONPATH=.. .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port $(HAL_PORT) --reload
  ```

- `scripts/provision/setup.sh:430`
  ```sh
  ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001
  ```

- `scripts/imager/build.sh:859`
  ```sh
  ExecStart=/opt/hal/.venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001
  ```

- `hal/server.py:707`
  ```py
  uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
  ```

### Why it is risky

HAL controls physical hardware and sensitive sensors:

- Camera snapshot and stream
- Microphone recording / voice pipeline
- Speaker/TTS/music playback
- Servo movement
- LED/display/emotion controls
- Bluetooth controls
- System routes such as reboot/shutdown depending on router implementation

If HAL listens on `0.0.0.0`, any host that can reach the device IP can call the API directly:

```sh
curl http://<device-ip>:5001/health
curl http://<device-ip>:5001/camera/snapshot?save=true
curl -X POST http://<device-ip>:5001/voice/speak -H 'Content-Type: application/json' -d '{...}'
```

This violates the desired boundary: only local Go server and OpenClaw should call HAL.

### Required remediation

Change every production/dev HAL start command from `0.0.0.0` to `127.0.0.1`.

#### File: `scripts/provision/setup.sh`

Replace:

```sh
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001
```

With:

```sh
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001
```

#### File: `scripts/imager/build.sh`

Replace:

```sh
ExecStart=/opt/hal/.venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001
```

With:

```sh
ExecStart=/opt/hal/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001
```

#### File: `Makefile`

Replace:

```make
cd $(HAL_DIR) && PYTHONPATH=.. .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port $(HAL_PORT) --reload
```

With:

```make
cd $(HAL_DIR) && PYTHONPATH=.. .venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port $(HAL_PORT) --reload
```

#### File: `hal/server.py`

Prefer introducing config instead of hardcoding:

```py
# hal/config.py
HTTP_HOST = os.environ.get("HAL_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HAL_HTTP_PORT", "5001"))
```

Then in `hal/server.py` import `HTTP_HOST` and replace:

```py
uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
```

With:

```py
uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
```

#### File: `hal/.env.example`

Add:

```env
HAL_HTTP_HOST=127.0.0.1
HAL_HTTP_PORT=5001
```

### Acceptance checks

On device after restart:

```sh
ss -ltnp | grep ':5001'
```

Expected:

```text
127.0.0.1:5001
```

Not acceptable:

```text
0.0.0.0:5001
```

From another LAN machine:

```sh
curl -i http://<device-ip>:5001/health
```

Expected: connection refused / timeout.

From the device itself:

```sh
curl -i http://127.0.0.1:5001/health
```

Expected: `200 OK`.

---

## Finding 2 — Nginx exposes `/hw/` proxy to HAL externally

### Severity

**Critical** because this bypasses HAL loopback binding if nginx is reachable.

### Evidence

Current `scripts/provision/setup.sh` nginx config:

```nginx
location /hw/ {
  proxy_pass http://lelamp/;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Prefix /hw;
  proxy_read_timeout 300s;
  proxy_send_timeout 300s;
}
```

Current `scripts/imager/build.sh` has equivalent `/hw/` proxy.

### Why it is risky

Even if HAL listens only on `127.0.0.1:5001`, nginx listens on port 80 and forwards external `/hw/*` requests to local HAL.

This means a LAN user can call:

```sh
curl http://<device-ip>/hw/health
curl http://<device-ip>/hw/camera/snapshot?save=true
curl http://<device-ip>/hw/docs
```

Nginx is local to the device, so HAL sees the proxy connection as local unless forwarded headers are checked. Therefore binding HAL to loopback alone is not sufficient.

### Required remediation

Block `/hw/` at nginx for non-loopback clients.

#### File: `scripts/provision/setup.sh`

Change `location /hw/` to:

```nginx
location /hw/ {
  # Hardware API is local-only. Only same-machine clients may access it.
  allow 127.0.0.1;
  allow ::1;
  deny all;

  proxy_pass http://lelamp/;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Prefix /hw;
  proxy_read_timeout 300s;
  proxy_send_timeout 300s;
}
```

#### File: `scripts/imager/build.sh`

Apply the same `allow/deny` block in its `location /hw/`.

### Optional alternative

If the web UI needs specific hardware status externally, do **not** expose raw `/hw/*`. Instead:

1. Keep `/hw/*` denied externally.
2. Add narrow, authenticated, sanitized endpoints in Lamp Go server, e.g.:
   - `/api/hardware/status`
   - `/api/hardware/snapshot` only if user is authenticated and explicitly allowed
3. Lamp Go calls HAL locally and returns a controlled response.

### Acceptance checks

From another LAN machine:

```sh
curl -i http://<device-ip>/hw/health
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

From the device itself:

```sh
curl -i http://127.0.0.1/hw/health
```

Expected:

```text
HTTP/1.1 200 OK
```

---

## Finding 3 — HAL has no defense-in-depth local-only middleware

### Severity

**High**.

### Evidence

`hal/server.py` currently has only request logging middleware:

```py
@app.middleware("http")
async def request_logging_middleware(request, call_next):
    ...
```

There is no check that rejects non-loopback clients or proxied external clients.

### Why it is risky

Operational mistakes happen. Someone may later change systemd, Makefile, Docker, or uvicorn host back to `0.0.0.0`. Without app-level checks, the hardware API immediately becomes remotely callable again.

Also, if nginx proxies external clients, the TCP peer appears as `127.0.0.1` unless the app checks `X-Forwarded-For` / `X-Real-IP`.

### Required remediation

Add a local-only middleware in `hal/server.py` and default-enable it via config.

#### File: `hal/config.py`

Add:

```py
LOCAL_ONLY_API = os.environ.get("HAL_LOCAL_ONLY_API", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
```

#### File: `hal/server.py`

Add imports:

```py
from ipaddress import ip_address, ip_network
from fastapi.responses import JSONResponse
```

Import config:

```py
from hal.config import LOCAL_ONLY_API
```

Add helper:

```py
_LOCAL_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
)


def _is_local_address(value: str | None) -> bool:
    if not value:
        return False
    host = value.strip()
    if not host:
        return False

    # X-Forwarded-For may contain a chain: client, proxy1, proxy2.
    # The first entry is the original client.
    if "," in host:
        host = host.split(",", 1)[0].strip()

    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]

    try:
        addr = ip_address(host)
    except ValueError:
        return host == "localhost"

    return any(addr in network for network in _LOCAL_NETWORKS)
```

Add middleware **before** normal request logging:

```py
@app.middleware("http")
async def local_only_api_middleware(request, call_next):
    if LOCAL_ONLY_API:
        client_host = request.client.host if request.client else None
        forwarded_for = request.headers.get("x-forwarded-for")
        forwarded_real_ip = request.headers.get("x-real-ip")

        if (
            not _is_local_address(client_host)
            or (forwarded_for and not _is_local_address(forwarded_for))
            or (forwarded_real_ip and not _is_local_address(forwarded_real_ip))
        ):
            logger.warning(
                "Blocked non-local HAL API request: client=%s xff=%s real_ip=%s path=%s",
                client_host,
                forwarded_for,
                forwarded_real_ip,
                request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "HAL API is local-only"},
            )

    return await call_next(request)
```

#### File: `hal/.env.example`

Add:

```env
HAL_LOCAL_ONLY_API=true
```

### Acceptance checks

Simulate an external client through nginx/proxy header from local machine:

```sh
curl -i http://127.0.0.1:5001/health -H 'X-Forwarded-For: 192.168.1.50'
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

Normal local request:

```sh
curl -i http://127.0.0.1:5001/health
```

Expected:

```text
HTTP/1.1 200 OK
```

---

## Finding 4 — Lamp Go server uses wildcard CORS

### Severity

**High**.

### Evidence

`system/server/server.go:196-205`:

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

Wildcard CORS allows any website loaded in a browser to issue cross-origin requests to the Lamp API and read responses if the browser can reach the device.

This is especially risky because the same server exposes setup/config/control endpoints under `/api/`.

### Required remediation

Do not use `Access-Control-Allow-Origin: *` globally. Use one of these approaches:

#### Recommended option A — Same-origin only

If the web UI is served by the same nginx origin as `/api/`, remove CORS entirely. Same-origin browser calls do not need CORS.

Change `corsMiddleware()` to only answer OPTIONS if needed, without wildcard origin.

#### Recommended option B — Allow same-host/loopback origins only

Add imports:

```go
import (
    "net"
    "net/url"
    "strings"
)
```

Replace CORS middleware with:

```go
func corsMiddleware() gin.HandlerFunc {
    return func(c *gin.Context) {
        origin := c.GetHeader("Origin")
        if origin != "" && isAllowedSameHostOrigin(origin, c.Request.Host) {
            c.Header("Access-Control-Allow-Origin", origin)
            c.Header("Vary", "Origin")
            c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            c.Header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept, Authorization")
        }

        if c.Request.Method == "OPTIONS" {
            if origin != "" && !isAllowedSameHostOrigin(origin, c.Request.Host) {
                c.AbortWithStatus(http.StatusForbidden)
                return
            }
            c.AbortWithStatus(http.StatusNoContent)
            return
        }

        c.Next()
    }
}

func isAllowedSameHostOrigin(origin, requestHost string) bool {
    u, err := url.Parse(origin)
    if err != nil || u.Scheme == "" || u.Host == "" {
        return false
    }
    originHost := normalizeHost(u.Host)
    host := normalizeHost(requestHost)
    return originHost != "" && (originHost == host || isLoopbackHost(originHost))
}

func normalizeHost(host string) string {
    h := strings.TrimSpace(strings.ToLower(host))
    if h == "" {
        return ""
    }
    if splitHost, _, err := net.SplitHostPort(h); err == nil {
        h = splitHost
    }
    return strings.Trim(h, "[]")
}

func isLoopbackHost(host string) bool {
    if host == "localhost" {
        return true
    }
    ip := net.ParseIP(host)
    return ip != nil && ip.IsLoopback()
}
```

### Acceptance checks

From malicious origin:

```sh
curl -i http://<device-ip>/api/health/live -H 'Origin: https://evil.example'
```

Expected:

- No `Access-Control-Allow-Origin: *`
- Ideally `403` for preflight OPTIONS from disallowed origin

From same origin or no origin:

```sh
curl -i http://<device-ip>/api/health/live
```

Expected: works normally.

---

## Finding 5 — Dangerous Lamp endpoints are externally reachable through `/api/`

### Severity

**Critical** for `exec` and `shell`; **High** for config JSON.

### Evidence

`system/server/server.go` routes:

```go
system.POST("exec", s.execCommand)
system.GET("shell", systemshell.ShellHandler)
oc.GET("config-json", s.openclawHandler.ConfigJSON)
```

Nginx proxies all `/api/` externally:

```nginx
location /api/ {
  proxy_pass http://backend;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

`execCommand` runs arbitrary shell:

```go
cmd := exec.CommandContext(ctx, "sh", "-c", body.Cmd)
```

### Why it is risky

- `/api/system/exec` is remote command execution if reachable without strong auth.
- `/api/system/shell` is interactive shell over WebSocket if reachable.
- `/api/agent/config-json` may expose OpenClaw gateway token and config secrets.

Even if intended for local monitor/dev UI, these should not be reachable from arbitrary LAN clients.

### Required remediation

Choose one of the following strategies.

#### Recommended strategy A — Remove from production builds

If these are only dev/bench tools:

- Delete or compile-gate `system.POST("exec", ...)`
- Delete or compile-gate `system.GET("shell", ...)`
- Delete or compile-gate `oc.GET("config-json", ...)`

Use build tags or config flag, e.g. `LAMP_ENABLE_DEV_ADMIN=false` default.

#### Recommended strategy B — Local-only middleware

Add a local-only middleware in `system/server/server.go`:

```go
func localOnlyMiddleware() gin.HandlerFunc {
    return func(c *gin.Context) {
        remoteHost := c.Request.RemoteAddr
        if host, _, err := net.SplitHostPort(remoteHost); err == nil {
            remoteHost = host
        }

        xff := strings.TrimSpace(strings.Split(c.GetHeader("X-Forwarded-For"), ",")[0])
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

Then change routes:

```go
system.POST("exec", localOnlyMiddleware(), s.execCommand)
system.GET("shell", localOnlyMiddleware(), systemshell.ShellHandler)
oc.GET("config-json", localOnlyMiddleware(), s.openclawHandler.ConfigJSON)
```

#### Recommended strategy C — Require strong admin auth

If remote admin is required:

- Add device-local admin token or session auth.
- Store secret with `0600` permissions.
- Require auth on `exec`, `shell`, `config-json`.
- Audit log every use.
- Consider disabling `sh -c` and allowing only a small command allowlist.

For this product, Strategy A or B is safer. If the requirement is “only Go and OpenClaw local can call HAL”, then Strategy B is enough for local internals but not enough for general remote UI admin.

### Acceptance checks

From LAN:

```sh
curl -i http://<device-ip>/api/agent/config-json
curl -i -X POST http://<device-ip>/api/system/exec -H 'Content-Type: application/json' -d '{"cmd":"id"}'
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

From local device:

```sh
curl -i http://127.0.0.1:5000/api/agent/config-json
```

Expected: works only if still needed locally.

---

## Finding 6 — OpenClaw gateway `/gw/` is proxied externally

### Severity

**High**.

### Evidence

`/gw/` nginx config in `scripts/provision/setup.sh`:

```nginx
location = /gw {
  proxy_pass http://openclaw/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
}

location /gw/ {
  proxy_pass http://openclaw/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
}
```

`scripts/imager/build.sh` also has `/gw/` proxy.

OpenClaw config created in `scripts/provision/setup.sh` includes:

```json
"gateway": {
  "mode": "local",
  "bind": "loopback",
  "port": 18789,
  "auth": {
    "mode": "token",
    "token": "$GATEWAY_TOKEN"
  },
  "controlUi": {
    "allowedOrigins": ["*"],
    "allowInsecureAuth": true
  }
}
```

### Why it is risky

The OpenClaw gateway is an agent control plane. Even if it has token auth, exposing it through nginx increases the attack surface. The config also allows wildcard origins and insecure auth for control UI.

The comment says forwarded headers are intentionally not set because OpenClaw treats forwarded headers as non-local. That means nginx may cause OpenClaw to see the peer as loopback, weakening the gateway’s own local-client protection.

### Required remediation

#### File: `scripts/provision/setup.sh`

Add nginx local-only deny rules to both `/gw` and `/gw/`:

```nginx
location = /gw {
  allow 127.0.0.1;
  allow ::1;
  deny all;

  proxy_pass http://openclaw/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
}

location /gw/ {
  allow 127.0.0.1;
  allow ::1;
  deny all;

  proxy_pass http://openclaw/;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
}
```

#### File: `scripts/imager/build.sh`

Apply same local-only deny block to `/gw/`.

#### File: `scripts/maintenance/patch-nginx-gw.sh`

If this script can add `/gw/` later, update it too so it does not reintroduce an exposed gateway. The generated block should include:

```nginx
allow 127.0.0.1;
allow ::1;
deny all;
```

### Acceptance checks

From LAN:

```sh
curl -i http://<device-ip>/gw/
```

Expected:

```text
HTTP/1.1 403 Forbidden
```

From local device:

```sh
curl -i http://127.0.0.1/gw/
```

Expected: reachable only if local UI/debug flow requires it.

---

## Finding 7 — DL backend default bind is public and API key can be disabled accidentally

### Severity

**High** if deployed anywhere reachable; **Medium** for local-only dev.

### Evidence

`integrations/perception-service/src/server.py`:

```py
async def verify_api_key(api_key: str = Security(api_key_header)):
    """Validate the X-API-Key header against DL_API_KEY."""
    if not settings.dl_api_key:
        return
    if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
```

`integrations/perception-service/src/server.py` CLI default:

```py
parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
```

`integrations/perception-service/Makefile`:

```make
HOST ?= 0.0.0.0
```

WebSocket routers are included without FastAPI dependencies and rely on websocket key checks in protocol utilities:

```py
app.include_router(action_ws_router, prefix="/api/dl")
app.include_router(emotion_ws_router, prefix="/api/dl")
app.include_router(pose_ws_router, prefix="/api/dl")
```

### Why it is risky

The current auth behavior means:

- If `DL_API_KEY` is set, HTTP protected routes require it.
- If `DL_API_KEY` is empty/missing, auth is disabled.
- Default host is `0.0.0.0`, so a developer/deployment can accidentally expose unauthenticated model endpoints.

This is especially risky for endpoints that process images/audio and may consume GPU/CPU resources or expose behavior as an inference service.

### Required remediation

#### File: `integrations/perception-service/src/server.py`

Change CLI default host to loopback:

```py
parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
```

Change docs in module header accordingly:

```py
python server.py                    # default 127.0.0.1:8001
python server.py --host 0.0.0.0     # expose externally; requires DL_API_KEY
```

#### File: `integrations/perception-service/Makefile`

Change:

```make
HOST ?= 0.0.0.0
```

To:

```make
HOST ?= 127.0.0.1
```

#### File: `integrations/perception-service/src/server.py`

Make missing `DL_API_KEY` safe. Recommended behavior:

- If request is from loopback and no key is configured: allow for local dev.
- If request is non-loopback and no key is configured: reject.

Pseudo-implementation:

```py
from ipaddress import ip_address, ip_network
from fastapi import Request

_LOCAL_NETWORKS = (ip_network("127.0.0.0/8"), ip_network("::1/128"))


def _is_local_address(value: str | None) -> bool:
    if not value:
        return False
    host = value.strip()
    if "," in host:
        host = host.split(",", 1)[0].strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        addr = ip_address(host)
    except ValueError:
        return host == "localhost"
    return any(addr in network for network in _LOCAL_NETWORKS)


async def verify_api_key(request: Request, api_key: str = Security(api_key_header)):
    if not settings.dl_api_key:
        client_host = request.client.host if request.client else None
        if _is_local_address(client_host):
            return
        raise HTTPException(status_code=401, detail="DL_API_KEY is required for non-local clients")

    if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
```

#### File: `integrations/perception-service/src/dlserver/utils/common.py`

Update WebSocket API key verification similarly:

```py
async def verify_ws_api_key(websocket: WebSocket) -> bool:
    api_key = websocket.headers.get("x-api-key", "")
    client_host = websocket.client.host if websocket.client else None

    if not settings.dl_api_key:
        if _is_local_address(client_host):
            return True
        await websocket.close(code=1008, reason="DL_API_KEY is required for non-local clients")
        return False

    if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
        await websocket.close(code=1008, reason="Invalid or missing API key")
        return False

    return True
```

### Acceptance checks

Local no-key dev:

```sh
curl -i http://127.0.0.1:8001/api/dl/health
```

Expected: allowed if local and no key.

External no-key:

```sh
curl -i http://<device-ip>:8001/api/dl/health
```

Expected: connection refused if bound loopback, or `401` if intentionally bound public.

External with key when intentionally exposed:

```sh
curl -i http://<device-ip>:8001/api/dl/health -H "X-API-Key: $DL_API_KEY"
```

Expected: `200 OK`.

---

## Finding 8 — OpenClaw generated config uses wildcard origins and insecure auth

### Severity

**Medium to High**, depending on whether `/gw/` is externally reachable.

### Evidence

`script/setup.sh` (now `scripts/provision/setup.sh`, which writes the tightened `controlUi` values — see CHECKLIST F8) wrote:

```json
"controlUi": {
  "allowedOrigins": ["*"],
  "allowInsecureAuth": true
}
```

### Why it is risky

Wildcard origins and insecure auth are acceptable only if the gateway is strictly loopback-only and never proxied to LAN clients. Because nginx currently proxies `/gw/`, this becomes part of the exposed attack surface.

### Required remediation

Preferred:

1. Block `/gw/` externally at nginx (Finding 6).
2. Then tighten config if possible:

```json
"controlUi": {
  "allowedOrigins": ["http://127.0.0.1", "http://localhost"],
  "allowInsecureAuth": false
}
```

If the web UI truly needs to access gateway through the device origin, use the exact origin(s), not `*`, e.g.:

```json
"allowedOrigins": ["http://lamp.local", "http://192.168.4.1"]
```

But do this only with proper token/session auth.

---

## Finding 9 — Documentation currently says `/hw/*` is externally exposed for Swagger/debug

### Severity

**Medium** because docs guide future implementation and QA behavior.

### Evidence

Docs contain language equivalent to:

- HAL exposes API on `127.0.0.1:5001`
- Nginx exposes it externally at `/hw/*` for Swagger/debug

Examples found around:

- `robots/lamp/docs/architecture-decision.md`
- `docs/bootstrap-ota.md`
- `robots/lamp/docs/vi/architecture-decision.md`
- `docs/vi/bootstrap-ota.md`

### Why it is risky

Even if code is fixed, docs telling engineers that external `/hw/docs` is expected may cause future re-exposure.

### Required remediation

Update docs to say:

```md
HAL Python runtime exposes HTTP API on `127.0.0.1:5001` only. OS Server (Go) and OpenClaw on the same device may call this API. LAN/Internet clients must not reach hardware endpoints directly. Nginx denies external `/hw/*` access.
```

Replace diagrams like:

```text
External → http://<device-ip>/hw/docs → nginx → http://127.0.0.1:5001/docs
```

With:

```text
External → http://<device-ip>/hw/* → nginx → 403
```

---

## Prioritized remediation plan

### Phase 0 — Confirm current exposure before changes

Run on the device:

```sh
ss -ltnp | grep -E ':(5000|5001|8001|18789)'
```

From a separate LAN machine:

```sh
curl -i http://<device-ip>:5001/health
curl -i http://<device-ip>/hw/health
curl -i http://<device-ip>/gw/
curl -i http://<device-ip>/api/agent/config-json
curl -i -X POST http://<device-ip>/api/system/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"id"}'
```

Document baseline status codes.

### Phase 1 — Close hardware control plane immediately

1. Change HAL bind host to `127.0.0.1` in:
   - `scripts/provision/setup.sh`
   - `scripts/imager/build.sh`
   - `Makefile`
   - `hal/server.py` via `HTTP_HOST`
2. Block nginx `/hw/` externally in:
   - `scripts/provision/setup.sh`
   - `scripts/imager/build.sh`
3. Add HAL app-level local-only middleware.

### Phase 2 — Close agent/admin control plane

1. Block `/gw` and `/gw/` externally in nginx.
2. Restrict dangerous Lamp endpoints:
   - `/api/system/exec`
   - `/api/system/shell`
   - `/api/agent/config-json`
3. Remove wildcard CORS.

### Phase 3 — Harden DL backend

1. Default DL backend to `127.0.0.1`.
2. Require `DL_API_KEY` for non-loopback clients.
3. Apply same rule to WebSocket endpoints.

### Phase 4 — Docs and regression tests

1. Update docs to state local-only boundary.
2. Add regression tests/check script.

---

## Suggested regression script

Create `scripts/security-check-local-only.sh` (proposed; not yet in the repo):

```sh
#!/usr/bin/env sh
set -eu

DEVICE_HOST="${1:-127.0.0.1}"

check_forbidden() {
  name="$1"
  url="$2"
  code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
  if [ "$code" != "403" ] && [ "$code" != "000" ]; then
    echo "FAIL: $name expected 403/000, got $code ($url)"
    exit 1
  fi
  echo "OK: $name blocked ($code)"
}

check_ok_local() {
  name="$1"
  url="$2"
  code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
  if [ "$code" != "200" ]; then
    echo "FAIL: $name expected 200, got $code ($url)"
    exit 1
  fi
  echo "OK: $name local works ($code)"
}

check_ok_local "HAL health direct local" "http://127.0.0.1:5001/health"
check_ok_local "Lamp health local" "http://127.0.0.1:5000/api/health/live"

if [ "$DEVICE_HOST" != "127.0.0.1" ] && [ "$DEVICE_HOST" != "localhost" ]; then
  check_forbidden "HAL direct external" "http://$DEVICE_HOST:5001/health"
  check_forbidden "HAL nginx /hw external" "http://$DEVICE_HOST/hw/health"
  check_forbidden "OpenClaw /gw external" "http://$DEVICE_HOST/gw/"
  check_forbidden "OpenClaw config-json external" "http://$DEVICE_HOST/api/agent/config-json"
fi
```

Run:

```sh
sh scripts/security-check-local-only.sh <device-ip>
```

---

## Final desired security contract

After remediation, this should be true:

| Surface | Local device | LAN / AP clients | Notes |
|---|---:|---:|---|
| `127.0.0.1:5001` HAL | Allowed | Not reachable | Hardware API local-only |
| `/hw/*` via nginx | Allowed only if needed | Denied 403 | Prefer no external raw hardware API |
| `127.0.0.1:18789` OpenClaw gateway | Allowed | Not reachable | Gateway local-only |
| `/gw/*` via nginx | Allowed only if needed | Denied 403 | Avoid exposing agent control |
| `/api/system/exec` | Local-only or disabled | Denied 403 | Prefer disabled in production |
| `/api/system/shell` | Local-only or disabled | Denied 403 | Prefer disabled in production |
| `/api/agent/config-json` | Local-only | Denied 403 | Can leak tokens/config |
| DL backend `:8001` | Allowed | Requires `DL_API_KEY` or not reachable | Default bind loopback |
| Lamp regular UI `/api/*` | Allowed | Allowed only for intended UI/setup APIs | No wildcard CORS |

---

## Quick patch checklist

Files to edit:

- `hal/config.py`
  - Add `HTTP_HOST=127.0.0.1` default.
  - Add `LOCAL_ONLY_API=true` default.

- `hal/server.py`
  - Import `HTTP_HOST`, `LOCAL_ONLY_API`.
  - Add local-only middleware checking client IP and forwarded headers.
  - Change `uvicorn.run(... host=HTTP_HOST ...)`.

- `hal/.env.example`
  - Add `HAL_HTTP_HOST=127.0.0.1`.
  - Add `HAL_LOCAL_ONLY_API=true`.

- `Makefile`
  - Change `lelamp-dev` uvicorn host to `127.0.0.1`.

- `scripts/provision/setup.sh`
  - Change HAL systemd host to `127.0.0.1`.
  - Add nginx `allow/deny` to `/hw/`.
  - Add nginx `allow/deny` to `/gw` and `/gw/`.
  - Optionally tighten generated OpenClaw `controlUi` config.

- `scripts/imager/build.sh`
  - Change HAL systemd host to `127.0.0.1`.
  - Add nginx `allow/deny` to `/hw/` and `/gw/`.

- `scripts/maintenance/patch-nginx-gw.sh`
  - Ensure any generated `/gw/` block includes local-only deny rules.

- `system/server/server.go`
  - Replace wildcard CORS.
  - Add local-only middleware.
  - Protect or remove `system exec`, `system shell`, `openclaw config-json`.

- `integrations/perception-service/src/server.py`
  - Default host `127.0.0.1`.
  - Missing `DL_API_KEY` should allow loopback only, reject non-loopback.

- `integrations/perception-service/src/dlserver/utils/common.py`
  - Apply same non-loopback API key requirement to WebSocket auth.

- `integrations/perception-service/Makefile`
  - Default `HOST ?= 127.0.0.1`.

- `integrations/perception-service/README.md`
  - Update default host and warning for public bind.

- Docs:
  - `robots/lamp/docs/architecture-decision.md`
  - `docs/bootstrap-ota.md`
  - `robots/lamp/docs/vi/architecture-decision.md`
  - `docs/vi/bootstrap-ota.md`
  - Remove “external `/hw/docs` for Swagger/debug” as expected behavior.

---

## Notes / open decisions

1. Decide whether the web monitor genuinely needs raw camera stream via `/hw/camera/stream` from browser. If yes, do not expose raw `/hw/*`; create a narrow authenticated Go proxy endpoint.
2. Decide whether remote shell/exec should exist at all in production. Recommendation: remove/disable by default.
3. Decide whether `/gw/` must be reachable from browser UI. Recommendation: keep gateway local-only and route required status through sanitized Lamp endpoints.
4. If any external admin mode is required, design it explicitly with authentication, authorization, audit logging, and CSRF/CORS rules. Do not rely on LAN trust.
