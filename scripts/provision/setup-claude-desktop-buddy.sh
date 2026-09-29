#!/bin/bash
# Install the Claude Desktop Buddy plugin as a systemd service (run as root after setup.sh).
# Usage: bash setup-claude-desktop-buddy.sh
set -euo pipefail

# Metadata URL from the bootstrap config; OTA_METADATA_URL env overrides. No default.
BOOTSTRAP_JSON="/root/config/bootstrap.json"
if [ -z "${OTA_METADATA_URL:-}" ]; then
  OTA_METADATA_URL="$(jq -r '.metadata_url // empty' "$BOOTSTRAP_JSON" 2>/dev/null || true)"
fi
if [ -z "$OTA_METADATA_URL" ]; then
  echo "[buddy] ERROR: no metadata_url in $BOOTSTRAP_JSON and OTA_METADATA_URL unset (run setup.sh first)"
  exit 1
fi
echo "[buddy] OTA metadata: $OTA_METADATA_URL"
BUDDY_DIR="/opt/claude-desktop-buddy"

ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root"
    exit 1
  fi
}

retry() {
  local n=0
  local max=$2
  local delay=${3:-2}
  local cmd="$1"
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n+1))
    echo "Retry $n/$max..."
    sleep "$delay"
  done
  echo "ERROR: Command failed after $max attempts: $cmd"
  return 1
}

ensure_root

# 1. Bluetooth
echo "[buddy] Checking Bluetooth..."
if ! command -v bluetoothctl &>/dev/null; then
  echo "[buddy] Installing bluez..."
  apt update && apt install -y bluez
fi
bluetoothctl power on 2>/dev/null || true
echo "[buddy] Bluetooth OK"

# 2. OTA metadata
echo "[buddy] Fetching OTA metadata..."
METADATA_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP"' EXIT
retry "curl -fsSL -H 'Cache-Control: no-cache' -o '$METADATA_TMP' '$OTA_METADATA_URL'" 5

BUDDY_VERSION=$(jq -r '."claude-desktop-buddy".version // empty' "$METADATA_TMP")
BUDDY_URL=$(jq -r '."claude-desktop-buddy".url // empty' "$METADATA_TMP")

if [ -z "$BUDDY_URL" ]; then
  echo "ERROR: No claude-desktop-buddy entry in OTA metadata."
  echo "Upload first: make upload-claude-desktop-buddy"
  exit 1
fi

echo "[buddy] Version: $BUDDY_VERSION"
echo "[buddy] URL: $BUDDY_URL"

# 3. Download and install
echo "[buddy] Downloading..."
ZIP_TMP=$(mktemp)
DIR_TMP=$(mktemp -d)
retry "curl -fsSL -H 'Cache-Control: no-cache' -o '$ZIP_TMP' '$BUDDY_URL'" 5
unzip -o -q "$ZIP_TMP" -d "$DIR_TMP"
rm -f "$ZIP_TMP"

mkdir -p "$BUDDY_DIR"

if [ -f "$DIR_TMP/buddy-plugin" ]; then
  cp -f "$DIR_TMP/buddy-plugin" "$BUDDY_DIR/buddy-plugin"
  chmod +x "$BUDDY_DIR/buddy-plugin"
else
  echo "ERROR: buddy-plugin binary not found in zip"
  rm -rf "$DIR_TMP"
  exit 1
fi

# Don't overwrite an existing user config.
if [ ! -f "/root/config/buddy.json" ] && [ -f "$DIR_TMP/config/buddy.json" ]; then
  mkdir -p /root/config
  cp -f "$DIR_TMP/config/buddy.json" /root/config/buddy.json
fi

echo "$BUDDY_VERSION" > "$BUDDY_DIR/VERSION_BUDDY"

rm -rf "$DIR_TMP"

# 4. systemd service
echo "[buddy] Creating systemd service..."

cat >/etc/systemd/system/claude-desktop-buddy.service <<EOF
[Unit]
Description=Claude Desktop Buddy (BLE)
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=$BUDDY_DIR
ExecStart=$BUDDY_DIR/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-desktop-buddy

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable claude-desktop-buddy
systemctl restart claude-desktop-buddy

echo ""
echo "======================================"
echo "Claude Desktop Buddy installed!"
echo "Version:  $BUDDY_VERSION"
echo "Binary:   $BUDDY_DIR/buddy-plugin"
echo "Config:   $BUDDY_DIR/config/buddy.json"
echo "Service:  systemctl status claude-desktop-buddy"
echo "Logs:     journalctl -u claude-desktop-buddy -f"
echo "======================================"
