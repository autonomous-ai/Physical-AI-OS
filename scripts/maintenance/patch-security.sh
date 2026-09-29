#!/usr/bin/env bash
# One-shot, idempotent security patch for existing Lamp devices; paste into the browser CLI (/monitor#cli).
# Requires the latest hal and os-server OTA first.

set -euo pipefail

HAL_SVC="/etc/systemd/system/hal.service"
HAL_UNIT="hal"
DEVICE_TYPE="$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"
NGINX_CONF="/etc/nginx/conf.d/${DEVICE_TYPE}.conf"

# Hash watched files so services restart only when something changed.
hash_file() { [ -e "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "missing"; }
NGINX_HASH_BEFORE=$(hash_file "$NGINX_CONF")
HAL_HASH_BEFORE=$(hash_file "$HAL_SVC")

echo "[patch] Starting security patch..."

# 1. LeLamp systemd: bind 127.0.0.1 instead of 0.0.0.0
if grep -q "\-\-host 0.0.0.0" "$HAL_SVC" 2>/dev/null; then
  sed -i 's/--host 0\.0\.0\.0/--host 127.0.0.1/' "$HAL_SVC"
  systemctl daemon-reload
  echo "[patch] ${HAL_UNIT}: bind changed to 127.0.0.1"
else
  echo "[patch] ${HAL_UNIT}: already on 127.0.0.1, skipping"
fi

# 2. nginx /hw/: add allow/deny if missing
if ! grep -q "allow 127.0.0.1" "$NGINX_CONF" 2>/dev/null; then
  python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

old = "  location /hw/ {\n    proxy_pass http://hal/;"
new = "  location /hw/ {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n    proxy_pass http://hal/;"
if old in content and new not in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("[patch] nginx /hw/: allow/deny added")
else:
    print("[patch] nginx /hw/: already patched, skipping")
PYEOF
else
  echo "[patch] nginx /hw/: already patched, skipping"
fi

# 3. nginx /api/system/exec: add allow/deny if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

marker = "location = /api/system/exec {"
allow  = "allow 127.0.0.1;"

if marker in content and allow not in content:
    content = content.replace(
        "  location = /api/system/exec {\n    proxy_pass",
        "  location = /api/system/exec {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass"
    )
    with open(path, "w") as f:
        f.write(content)
    print("[patch] nginx /api/system/exec: allow/deny added")
elif marker not in content:
    print("[patch] nginx /api/system/exec: block not found, may need manual check")
else:
    print("[patch] nginx /api/system/exec: already patched, skipping")
PYEOF

# 3a. nginx /api/system/shell: add WebSocket upgrade block if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "location = /api/system/shell" in content:
    print("[patch] nginx /api/system/shell: already present, skipping")
    sys.exit(0)

old = "  location /api/ {"
new = """  # Interactive shell WebSocket (xterm.js PTY) — must come before generic /api/.
  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location /api/ {"""

if old not in content:
    print("[patch] nginx /api/system/shell: /api/ block not found, may need manual check")
    sys.exit(0)

content = content.replace(old, new, 1)
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx /api/system/shell: WebSocket upgrade block added")
PYEOF

# 3a'. nginx /api/buddy/ws: add WebSocket upgrade block if missing (must precede the generic /api/ block)
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "location = /api/buddy/ws" in content:
    print("[patch] nginx /api/buddy/ws: already present, skipping")
    sys.exit(0)

old = "  location /api/ {"
new = """  # Autonomous Buddy (macOS companion) persistent WebSocket — must come before generic /api/.
  location = /api/buddy/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location /api/ {"""

if old not in content:
    print("[patch] nginx /api/buddy/ws: /api/ block not found, may need manual check")
    sys.exit(0)

content = content.replace(old, new, 1)
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx /api/buddy/ws: WebSocket upgrade block added")
PYEOF

# 3b'. nginx /openapi.json: add proxy block if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "location = /openapi.json" in content:
    print("[patch] nginx /openapi.json: already present, skipping")
    sys.exit(0)

# Anchor: insert just before `location /api/ {` (generic backend block).
anchor = "  location /api/ {"
if anchor not in content:
    print("[patch] nginx /openapi.json: /api/ block not found, skipping")
    sys.exit(0)

block = (
    "  # Top-level openapi.json proxied to Lamp backend so the in-iframe Swagger\n"
    "  # UI (loaded via /api/hardware/docs) can fetch its spec at the absolute\n"
    "  # path FastAPI hardcodes. Lamp adminAuthMiddleware gates the cookie/Bearer.\n"
    "  location = /openapi.json {\n"
    "    proxy_pass http://backend;\n"
    "    proxy_set_header Host $host;\n"
    "    proxy_set_header X-Real-IP $remote_addr;\n"
    "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
    "  }\n\n"
)

content = content.replace(anchor, block + anchor, 1)
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx /openapi.json: proxy block added")
PYEOF

# 3b. nginx /gw and /gw/: add allow/deny if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

patched_any = False
for marker, old, new in (
    (
        "location = /gw {",
        "  location = /gw {\n    proxy_pass http://openclaw/;",
        "  location = /gw {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass http://openclaw/;",
    ),
    (
        "location /gw/ {",
        "  location /gw/ {\n    proxy_pass http://openclaw/;",
        "  location /gw/ {\n    allow 127.0.0.1;\n    allow ::1;\n    deny all;\n\n    proxy_pass http://openclaw/;",
    ),
):
    if marker not in content:
        print(f"[patch] nginx {marker.split()[1]}: block not found, skipping")
        continue
    if new in content:
        print(f"[patch] nginx {marker.split()[1]}: already patched, skipping")
        continue
    if old in content:
        content = content.replace(old, new)
        patched_any = True
        print(f"[patch] nginx {marker.split()[1]}: allow/deny added")
    else:
        print(f"[patch] nginx {marker.split()[1]}: block shape differs, may need manual check")

if patched_any:
    with open(path, "w") as f:
        f.write(content)
PYEOF

# 3c. nginx security headers: inject after client_max_body_size if missing
python3 - "$NGINX_CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

if "Content-Security-Policy" in content:
    # Headers already present from an earlier patch run. Upgrade in place:
    #   - DENY → SAMEORIGIN (allow same-origin iframe embedding)
    #   - frame-ancestors 'none' → 'self' (CSP mirror of SAMEORIGIN)
    #   - Strict CSP: revert any prior CDN whitelist + `'unsafe-inline'`
    #     script-src that an earlier patch added. LeLamp now self-hosts the
    #     Swagger UI bundle (Lamp proxies it via /api/hardware/static/*) so
    #     no CDN allow-list is required.
    new_content = content
    new_content = new_content.replace(
        'add_header X-Frame-Options "DENY"',
        'add_header X-Frame-Options "SAMEORIGIN"',
    )
    new_content = new_content.replace(
        "frame-ancestors 'none'",
        "frame-ancestors 'self'",
    )
    # Revert script-src to strict 'self' (drop 'unsafe-inline' + CDN).
    new_content = new_content.replace(
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;",
        "script-src 'self';",
    )
    new_content = new_content.replace(
        "script-src 'self' https://cdn.jsdelivr.net;",
        "script-src 'self';",
    )
    # Revert style-src to base (keep 'unsafe-inline' for React style props).
    new_content = new_content.replace(
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;",
        "style-src 'self' 'unsafe-inline';",
    )
    # Revert img-src (drop FastAPI favicon allow).
    new_content = new_content.replace(
        "img-src 'self' data: blob: https://fastapi.tiangolo.com;",
        "img-src 'self' data: blob:;",
    )
    # Revert font-src CDN whitelist; keep data: for embedded fonts.
    new_content = new_content.replace(
        "font-src 'self' data: https://cdn.jsdelivr.net;",
        "font-src 'self' data:;",
    )
    # Revert connect-src CDN whitelist.
    new_content = new_content.replace(
        "connect-src 'self' ws: wss: https://cdn.jsdelivr.net;",
        "connect-src 'self' ws: wss:;",
    )
    # Migrate connect-src to allow plain-http cross-origin probes. The Setup
    # page probes http://<host>.local/api/health AND http://<lan_ip>/api/health
    # to auto-redirect after the AP→home-WiFi handoff; mDNS (.local) is often
    # blocked by the router so the lan_ip probe is the reliable fallback. Both
    # are cross-origin from the AP page (192.168.100.1), so 'self' alone blocks
    # them — without `http:` the "joining Wi-Fi…" screen never advances.
    # Idempotent: also upgrades an earlier *.local-only value to plain `http:`.
    new_content = new_content.replace(
        "connect-src 'self' ws: wss:;",
        "connect-src 'self' ws: wss: http:;",
    )
    new_content = new_content.replace(
        "connect-src 'self' ws: wss: http://*.local;",
        "connect-src 'self' ws: wss: http:;",
    )
    if new_content != content:
        with open(path, "w") as f:
            f.write(new_content)
        print("[patch] nginx security headers: upgraded to strict CSP (no CDN whitelist, no 'unsafe-inline' script-src)")
    else:
        print("[patch] nginx security headers: already up-to-date, skipping")
    sys.exit(0)

# Anchor: the client_max_body_size line is present on every device since
# the attachments PR; using it as the anchor keeps the headers grouped
# with other server-level settings.
import re
anchor = re.search(r"client_max_body_size\s+\S+;\s*\n", content)
if not anchor:
    print("[patch] nginx security headers: client_max_body_size anchor not found, may need manual add")
    sys.exit(0)

block = (
    "\n"
    "  # Security headers (clickjacking, MIME sniff, XSS containment).\n"
    "  # SAMEORIGIN/'self' lets Monitor embed in-house iframes (Swagger,\n"
    "  # gateway config); external sites still can't frame the device.\n"
    "  add_header X-Frame-Options \"SAMEORIGIN\" always;\n"
    "  add_header X-Content-Type-Options \"nosniff\" always;\n"
    "  add_header Referrer-Policy \"no-referrer\" always;\n"
    "  add_header Permissions-Policy \"camera=(), microphone=(), geolocation=(), payment=()\" always;\n"
    "  add_header Content-Security-Policy \"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'\" always;\n"
)

end = anchor.end()
content = content[:end] + block + content[end:]
with open(path, "w") as f:
    f.write(content)
print("[patch] nginx security headers: added")
PYEOF

# 4. Set HAL_MODE=production in .env (activates same-origin middleware)
HAL_ENV="/opt/hal/.env"
touch "$HAL_ENV"
if grep -q "^HAL_MODE=" "$HAL_ENV" 2>/dev/null; then
  sed -i "s/^HAL_MODE=.*/HAL_MODE=production/" "$HAL_ENV"
  echo "[patch] HAL_MODE set to production"
else
  echo "HAL_MODE=production" >> "$HAL_ENV"
  echo "[patch] HAL_MODE=production added to .env"
fi

# 5. Add EnvironmentFile to hal unit if missing
if ! grep -q "^EnvironmentFile=" "$HAL_SVC" 2>/dev/null; then
  sed -i '/^\[Service\]/a EnvironmentFile=\/opt\/hal\/.env' "$HAL_SVC"
  systemctl daemon-reload
  echo "[patch] ${HAL_UNIT}.service: EnvironmentFile added"
else
  echo "[patch] ${HAL_UNIT}.service: EnvironmentFile already present, skipping"
fi

# 6. Bind os-server to 127.0.0.1
OS_SVC="/etc/systemd/system/os-server.service"
OS_BIN="/usr/local/bin/os-server"

LAMP_VERSION=$("$OS_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
echo "[patch] os-server version: ${LAMP_VERSION:-unknown}"
echo "[patch] To close port 5000 on LAN: run 'sudo software-update os-server' to get the latest binary."

# 7. Apply: reload/restart only when files changed
NGINX_HASH_AFTER=$(hash_file "$NGINX_CONF")
HAL_HASH_AFTER=$(hash_file "$HAL_SVC")

if [ "$NGINX_HASH_BEFORE" != "$NGINX_HASH_AFTER" ]; then
  echo "[patch] nginx config changed → reloading nginx"
  nginx -t && nginx -s reload
else
  echo "[patch] nginx config unchanged, skipping reload"
fi

if [ "$HAL_HASH_BEFORE" != "$HAL_HASH_AFTER" ]; then
  LAMP_UNIT="os-server"
  echo "[patch] ${HAL_UNIT}.service changed → restarting ${HAL_UNIT} + ${LAMP_UNIT}"
  systemctl restart "$HAL_UNIT" "$LAMP_UNIT"
else
  echo "[patch] ${HAL_UNIT}.service unchanged, skipping service restart"
fi

echo "[patch] Done. Device is patched."
