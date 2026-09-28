#!/usr/bin/env bash
# Add proxy_read/send_timeout to the /hw/ nginx location so long HAL endpoints avoid 504.
# Run directly on the Pi as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

DEVICE_TYPE="$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"
CONF="/etc/nginx/conf.d/${DEVICE_TYPE}.conf"

if [ ! -f "$CONF" ]; then
  echo "Error: $CONF not found"
  exit 1
fi

if grep -q "proxy_read_timeout 300s" "$CONF"; then
  echo "[skip]  /hw/ already has 300s timeout"
else
  sed -i '/proxy_set_header X-Forwarded-Prefix \/hw;/a \    proxy_read_timeout 300s;\n    proxy_send_timeout 300s;' "$CONF"
  echo "[patch] Added proxy_read_timeout/proxy_send_timeout to /hw/"
fi

nginx -t
systemctl reload nginx
echo "[done]  nginx reloaded"
