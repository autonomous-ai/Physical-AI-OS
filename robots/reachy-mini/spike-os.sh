#!/usr/bin/env bash
# spike-os.sh — install os-server on a Reachy Mini from OTA (runs on the robot).
# Usage: sudo bash spike-os.sh [--stop|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-os"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="os-server"
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
  say "Stopping os-server"
  stop_unit "$SERVICE"
  tmux kill-session -t os 2>/dev/null || true
  pkill -f "$BIN_DIR/os-server" 2>/dev/null || true
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -f "$BIN_DIR/os-server"
    info "removed the unit and $BIN_DIR/os-server"
    info "kept $CONFIG_DIR/config.json — it holds provisioning state"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/4  Seed $CONFIG_DIR (config.json + bootstrap.json)"
# bootstrap.json must exist before os-server starts: it is the only source of OTAMetadataURL.
ensure_bootstrap_config
# Never overwrite an existing config (provisioning state). openclaw_config_dir must be explicit:
# a missing key unmarshals to "" rather than Default(), so the gateway token is never found.
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/config.json" ]; then
# Backfill configs seeded by earlier runs that lack the key.
  if jq -e 'has("openclaw_config_dir") and .openclaw_config_dir != ""' \
      "$CONFIG_DIR/config.json" >/dev/null 2>&1; then
    info "config.json already present — keeping it"
  else
    tmp="$(mktemp)"
    jq '.openclaw_config_dir = "'"$OPENCLAW_CONFIG_DIR"'"' "$CONFIG_DIR/config.json" >"$tmp" \
      || die "failed to backfill openclaw_config_dir into $CONFIG_DIR/config.json"
    chmod 600 "$tmp"
    mv "$tmp" "$CONFIG_DIR/config.json"
    info "config.json kept; backfilled openclaw_config_dir=$OPENCLAW_CONFIG_DIR"
  fi
else
  cat >"$CONFIG_DIR/config.json" <<JSON
{
  "httpPort": 5000,
  "device_type": "$DEVICE_TYPE",
  "openclaw_config_dir": "$OPENCLAW_CONFIG_DIR"
}
JSON
  chmod 600 "$CONFIG_DIR/config.json"
  info "seeded config.json for device_type=$DEVICE_TYPE"
fi

say "2/4  Check the AP-mode hazard before enabling at boot"
# os-server switches to AP mode at startup when setup is incomplete, which drops WiFi/SSH.
if grep -q '"set_up_completed"[[:space:]]*:[[:space:]]*true' "$CONFIG_DIR/config.json" 2>/dev/null; then
  info "set_up_completed=true — no AP switch at boot"
elif [ -x /usr/local/bin/device-ap-mode ]; then
  die "set_up_completed is not true AND /usr/local/bin/device-ap-mode exists.
Enabling os-server at boot would drop this robot into AP mode and kill WiFi.
Finish setup in the web UI first, then re-run."
else
  info "WARN: set_up_completed is not true, but /usr/local/bin/device-ap-mode does not"
  info "WARN: exist, so the AP switch is a no-op. Safe for now — finish setup before"
  info "WARN: that script ever lands on this robot."
fi

say "3/4  Install the binary from OTA"
stop_unit "$SERVICE"
pkill -f "$BIN_DIR/os-server" 2>/dev/null || true
ota_install_binary os-server "$BIN_DIR/os-server"

say "4/4  Install the unit and start"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=Autonomous OS Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# config.Load reads config/config.json by a RELATIVE path — /root is what makes
# that $CONFIG_DIR/config.json, the same file HAL reads via OS_CONFIG_PATH.
WorkingDirectory=/root
# Explicit, so os-server still boots when config.json is missing or unreadable.
Environment=DEVICE_TYPE=$DEVICE_TYPE
Environment=DEVICES_DIR=$DEVICES_DIR
ExecStart=$BIN_DIR/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
wait_http "http://localhost:5000/api/health/live" 60 "os-server" || true
echo "--- live ---";      curl -s -m 5 localhost:5000/api/health/live; echo
echo "--- readiness ---"; curl -s -m 5 localhost:5000/api/health/readiness; echo

cat <<EOF

========================================
  os-server running under systemd
    version : $(ota_field os-server version)
    logs    : journalctl -u $SERVICE -f
    API     : loopback only — from the robot:  curl localhost:5000/api/health/live
    tunnel  : ssh -L 5000:localhost:5000 <user>@reachy-mini.local
    stop    : sudo bash spike-os.sh --stop
========================================

Footprint outside $BIN_DIR: $CONFIG_DIR/config.json and /var/log/os-server.log.
The web UI needs nginx — run spike-web.sh next.
EOF
