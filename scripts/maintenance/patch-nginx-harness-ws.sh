#!/usr/bin/env bash
# Patch existing device nginx to add /api/harness/ws WebSocket upgrade block.
#
# Devices flashed before the Harness direct-pair feature landed have nginx
# configs whose only "location = /api/..." blocks with WebSocket upgrade are
# /api/system/shell and /api/buddy/ws. /api/harness/ws then falls through to
# the generic "location /api/" block, which does NOT relay the Upgrade /
# Connection headers, so the Harness Desktop CLI cannot complete the WS
# handshake. Symptom: pair fails with a generic "Autonomous device management
# request failed" (the CLI's uncoded fallback), while os-server logs show only
# ordinary GET /api/harness/status polling.
#
# Run this script directly on the device as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

CONF=""
for candidate in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -f "$candidate" ] || continue
  if grep -q "location = /api/buddy/ws" "$candidate"; then
    CONF="$candidate"
    break
  fi
done

if [ -z "$CONF" ]; then
  echo "Error: could not find an nginx site with /api/buddy/ws to patch"
  exit 1
fi

if grep -q "location = /api/harness/ws" "$CONF"; then
  echo "[skip]  location = /api/harness/ws already present in $CONF"
  exit 0
fi

# Work on the real file so backups contain bytes, not a sites-enabled symlink.
# Editing and restoring this target also preserves the enabled symlink itself.
CONF="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$CONF")"

# Keep backups OUTSIDE the sites-enabled directory so nginx does not try to
# parse them and error out with "duplicate upstream".
BACKUP_DIR="/var/backups/nginx-harness-ws"
mkdir -p "$BACKUP_DIR"
BACKUP="$(mktemp "${BACKUP_DIR}/$(basename "$CONF").bak.XXXXXX")"
cp -a "$CONF" "$BACKUP"
echo "[patch] backup written to $BACKUP"

# Insert the harness ws block right after the /api/buddy/ws block. Matches the
# closing brace on its own line that terminates the buddy block (indented by
# two spaces, matching the imager templates).
python3 - "$CONF" <<'PY'
import re, sys
path = sys.argv[1]
with open(path, 'r') as fh:
    source = fh.read()
block = """
  # Direct Harness device connection, authenticated by PAKE and pinned E2EE keys.
  # Must come BEFORE the generic /api/ block so the WebSocket upgrade headers
  # actually reach os-server.
  location = /api/harness/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }
"""
pattern = re.compile(
    r'(location\s*=\s*/api/buddy/ws\s*\{[^{}]*?\}\s*\n)',
    re.MULTILINE,
)
if not pattern.search(source):
    sys.stderr.write("could not locate /api/buddy/ws block\n")
    sys.exit(1)
updated = pattern.sub(r'\1' + block, source, count=1)
with open(path, 'w') as fh:
    fh.write(updated)
PY

echo "[patch] added location = /api/harness/ws to $CONF"

if ! nginx -t 2>&1; then
  echo "[error] nginx -t failed, restoring backup"
  cp -a "$BACKUP" "$CONF"
  exit 1
fi

systemctl reload nginx
echo "[done]  nginx reloaded"
echo "  WS:   ws://$(hostname -I | awk '{print $1}')/api/harness/ws"
