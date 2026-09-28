#!/usr/bin/env bash
# spike-bootstrap.sh — install the OTA bootstrap worker + software-update helper on a Reachy Mini.
# Usage: sudo [OTA_METADATA_URL=...] bash spike-bootstrap.sh [--no-start|--stop|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-bootstrap"
# Resolve from this file: the package runs from a staging dir or /opt/devices.
SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=spike-lib.sh
. "$SPIKE_DIR/spike-lib.sh"

SERVICE="bootstrap"
NO_START=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-start)  NO_START=1 ;;
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping the bootstrap worker"
  stop_unit "$SERVICE"
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -f "$BIN_DIR/bootstrap-server" "$BIN_DIR/software-update"
    info "removed the unit, $BIN_DIR/bootstrap-server and $BIN_DIR/software-update"
    info "kept $CONFIG_DIR/bootstrap.json — the other spike scripts read metadata_url from it"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/4  Seed $CONFIG_DIR/bootstrap.json"
ensure_bootstrap_config
BS_JSON="$CONFIG_DIR/bootstrap.json"
URL="$(jq -r '.metadata_url // empty' "$BS_JSON" 2>/dev/null || true)"

say "2/4  Install the binary from OTA"
# Stop first: replacing a running binary in place gives ETXTBSY.
stop_unit "$SERVICE"
ota_install_binary bootstrap "$BIN_DIR/bootstrap-server"

say "3/4  Install the software-update helper"
# The worker execs software-update to apply updates; fatal when missing from the package.
SU_SRC="$SPIKE_DIR/software-update"
[ -f "$SU_SRC" ] || SU_SRC="$DEVICES_DIR/$DEVICE_TYPE/software-update"
[ -f "$SU_SRC" ] || die "software-update is not in this device package.
Publish a current one:  make upload-device $DEVICE_TYPE"
bash -n "$SU_SRC" >/dev/null 2>&1 || die "$SU_SRC is not valid bash — refusing to install a broken updater"
install -m 0755 "$SU_SRC" "$BIN_DIR/software-update.new"
mv -f "$BIN_DIR/software-update.new" "$BIN_DIR/software-update"
info "installed $BIN_DIR/software-update (from $SU_SRC)"

say "4/4  Install the unit"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=Autonomous OTA Bootstrap Worker
# Needs a route to the CDN before its first poll; without this it burns its
# first interval failing DNS on a cold boot.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# Reads config/bootstrap.json by a RELATIVE path, the same convention os-server
# uses for config/config.json — the working directory is what makes it
# $CONFIG_DIR/bootstrap.json.
WorkingDirectory=/root
ExecStart=$BIN_DIR/bootstrap-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bootstrap

[Install]
WantedBy=multi-user.target
UNIT

if [ "$NO_START" = "1" ]; then
  systemctl enable "$SERVICE" >/dev/null 2>&1
  info "--no-start: installed and enabled, not started"
else
  start_unit "$SERVICE"
  sleep 2
  systemctl is-active --quiet "$SERVICE" \
    && info "running" \
    || info "WARN: not running — journalctl -u $SERVICE -n 50"
fi

cat <<EOF

========================================
  OTA bootstrap worker installed
    feed    : $URL
    poll    : $(jq -r '.poll_interval // "5m"' "$BS_JSON")
    logs    : journalctl -u $SERVICE -f
    stop    : sudo bash spike-bootstrap.sh --stop
========================================

From here the robot updates itself: new os-server / hal / web / device builds
are picked up on the next poll. Publish with the make upload-* targets.
EOF
