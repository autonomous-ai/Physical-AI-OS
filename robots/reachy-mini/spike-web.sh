#!/usr/bin/env bash
# spike-web.sh — serve the OTA web bundle via a spike-only nginx vhost on a Reachy Mini.
# Usage: sudo bash spike-web.sh [--stop|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-web"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

VHOST="reachy-spike"
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Disabling the spike vhost"
  rm -f "/etc/nginx/sites-enabled/$VHOST"
  systemctl reload nginx 2>/dev/null || true
  if [ "$UNINSTALL" = "1" ]; then
    rm -f "/etc/nginx/sites-available/$VHOST"
    rm -rf "${WEB_ROOT:?}"
    info "removed the vhost and $WEB_ROOT (nginx package left installed)"
  else
    info "vhost disabled; nginx and the bundle are still installed"
  fi
  exit 0
fi

say "1/4  Install nginx"
if ! [ -x /usr/sbin/nginx ] && ! command -v nginx >/dev/null; then
  info "installing nginx"
  apt-get update -qq
  apt-get install -y --no-install-recommends nginx || die "nginx install failed"
fi
# Debian's default vhost also claims default_server on :80.
rm -f /etc/nginx/sites-enabled/default

say "2/4  Install the bundle from OTA"
rm -rf "${WEB_ROOT:?}"
mkdir -p "$WEB_ROOT"
ota_unpack web "$WEB_ROOT"
[ -f "$WEB_ROOT/index.html" ] || die "no index.html in the web package — wrong artifact?"
chmod 755 /usr/share/nginx /usr/share/nginx/html "$WEB_ROOT" 2>/dev/null || true

say "3/4  Write the spike vhost"
cat >"/etc/nginx/sites-available/$VHOST" <<NGINX
upstream spike_backend { server 127.0.0.1:5000; }
upstream spike_hal     { server 127.0.0.1:5001; }

server {
  listen 80 default_server;
  root $WEB_ROOT;
  index index.html;

  # Monitor chat attaches base64 payloads; nginx's 1 MB default 413s them.
  client_max_body_size 20M;

  # Same security posture as the production vhost (scripts/imager/build-orangepi.sh)
  # — this is a device-control UI on a shared network, not a demo page.
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  # Cache policy uses \`expires\`, NOT \`add_header Cache-Control\`: an add_header
  # inside a location cancels inheritance of every server-level add_header,
  # which would silently drop the CSP and security headers above.
  # index.html must never be cached — it is the pointer to fingerprinted asset
  # names, and a stale copy asks for bundles the last deploy deleted (blank page).
  location = /index.html { expires -1; }
  location /assets/      { expires 1y; }

  # SPA fallback — client-side routes are not files on disk.
  location / {
    try_files \$uri /index.html;
  }

  location /api/ {
    proxy_pass http://spike_backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    # No proxy_buffering off here: os-server sets X-Accel-Buffering: no on its
    # own SSE responses (flow, monitor, logs), so nginx unbuffers exactly those.
  }

  # WebSocket endpoints need an explicit upgrade + a long read timeout.
  location = /api/system/shell {
    proxy_pass http://spike_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
  }

  location = /api/buddy/ws {
    proxy_pass http://spike_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
  }

  # HAL is loopback-only, exactly as in production. The browser must reach
  # hardware through os-server's authenticated /api/hardware/* proxy — exposing
  # /hw/ to the LAN would hand every host on the network unauthenticated servo,
  # camera, and audio control.
  location /hw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://spike_hal/;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /hw;
    # Hardware calls can run long (enroll records for seconds, then embeds).
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
  }
}
NGINX

ln -sfn "/etc/nginx/sites-available/$VHOST" "/etc/nginx/sites-enabled/$VHOST"
nginx -t || die "nginx config test failed"
systemctl reload nginx 2>/dev/null || systemctl restart nginx
info "nginx reloaded"

say "4/4  Verify"
code() { curl -s -o /dev/null -w "%{http_code}" -m 10 "$1"; }
info "GET /                 -> $(code http://localhost/)"
info "GET /api/health/live  -> $(code http://localhost/api/health/live)"
info "GET /hw/health        -> $(code http://localhost/hw/health)"

cat <<EOF

========================================
  Web UI served by nginx
    version : $(ota_field web version)
    browser : http://<robot-ip>/setup?debug=true&device_id=reachy-1   (or http://reachy-mini.local/setup?debug=true&device_id=reachy-1)
    vhost   : /etc/nginx/sites-available/$VHOST  (spike-only, NOT production)
    bundle  : $WEB_ROOT
    stop    : sudo bash spike-web.sh --stop
========================================

A 502 on /api means os-server is not up — run spike-os.sh.
A 502 on /hw means HAL is not up — run spike-hal.sh.
EOF
