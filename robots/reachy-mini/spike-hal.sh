#!/usr/bin/env bash
# spike-hal.sh — install HAL from OTA into /opt/hal on a Reachy Mini (run spike-device.sh first).
# Usage: sudo bash spike-hal.sh [--no-deps|--stop|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-hal"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="hal"
# Reuse the pollen user's HuggingFace cache to avoid a second copy on the small eMMC.
HF_HOME_PATH="${HF_HOME_PATH:-/home/pollen/.cache/huggingface}"

SKIP_DEPS=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-deps)   SKIP_DEPS=1 ;;
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping HAL"
  stop_unit "$SERVICE"
  tmux kill-session -t hal 2>/dev/null || true
  pkill -f 'uvicorn hal.server:app' 2>/dev/null || true
  sleep 1
# Return media to the Pollen daemon in case HAL was killed before releasing it.
  curl -s -m 5 -X POST "$DAEMON_URL/api/media/acquire" >/dev/null 2>&1 || true
  printf '[%s] media: ' "$SPIKE_TAG"; curl -s -m 5 "$DAEMON_URL/api/media/status" || true; echo
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -rf "${HAL_DIR:?}"
    info "removed the unit and $HAL_DIR"
    info "left behind: /root/local (users, strangers, models) and /var/log/hal"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

[ -f "$DEVICES_DIR/$DEVICE_TYPE/ROBOT.md" ] \
  || die "no $DEVICES_DIR/$DEVICE_TYPE/ROBOT.md — run: sudo bash spike-device.sh"

say "1/4  Preflight: disk space"
# A built venv leaves ~1.8 GB free, so re-runs need a smaller threshold than first install.
AVAIL_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
info "free space on /: $(awk -v k="$AVAIL_KB" 'BEGIN {printf "%.1f GB", k/1048576}')"
if [ "$SKIP_DEPS" = "0" ]; then
  if [ -x "$HAL_DIR/.venv/bin/uvicorn" ]; then
    NEED_KB=1048576   # 1 GB — incremental sync over an existing venv
    NEED_LABEL="1 GB (updating an existing venv)"
  else
    NEED_KB=4194304   # 4 GB — download + build from nothing
    NEED_LABEL="4 GB (building the venv from scratch)"
  fi
  if [ "$AVAIL_KB" -lt "$NEED_KB" ]; then
    die "need at least $NEED_LABEL.
Free space first (journalctl --vacuum-size=100M, uv cache clean), or re-run
with --no-deps to skip the sync entirely."
  fi
fi

say "2/4  Install HAL from OTA"
stop_unit "$SERVICE"
pkill -f 'uvicorn hal.server:app' 2>/dev/null || true
# Unzip over the top: .env and the venv must survive a redeploy.
mkdir -p "$HAL_DIR"
ota_unpack hal "$HAL_DIR"
[ -f "$HAL_DIR/.env" ] || die "no $HAL_DIR/.env — run spike-device.sh (it ships the overlay)"

if [ "$SKIP_DEPS" = "0" ]; then
  say "3/4  Build the Python venv"
  apt-get install -y --no-install-recommends \
    libcairo2-dev libgirepository1.0-dev pkg-config >/dev/null 2>&1 \
    || info "WARN: apt install failed — pygobject may fail to build"

  UV="$(command -v uv || true)"
  [ -n "$UV" ] || [ ! -x /home/pollen/.local/bin/uv ] || UV=/home/pollen/.local/bin/uv
  if [ -z "$UV" ]; then
    info "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
    UV=/usr/local/bin/uv
  fi

  cd "$HAL_DIR"
  export UV_CACHE_DIR="$HAL_DIR/.uv-cache"
# uv's 30s default timeout is too tight for large wheels.
  export UV_HTTP_TIMEOUT=180
  retry "'$UV' sync --python 3.12 --extra hardware --extra reachy" 3 5 \
    || die "uv sync failed 3 times (network?). The cache is kept — re-run to resume."
  "$UV" cache prune >/dev/null 2>&1 || true
  df -h / | awk 'NR==2 {print "['"$SPIKE_TAG"'] free space after sync: " $4}'
else
  say "3/4  Deps skipped (--no-deps)"
  [ -x "$HAL_DIR/.venv/bin/uvicorn" ] || die "no venv at $HAL_DIR/.venv — drop --no-deps"
fi

say "4/4  Install the unit and start"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=HAL Hardware Runtime
# The Pollen daemon owns the camera and both ALSA PCMs and must be up before HAL
# asks for them; it is also where the motion driver connects (localhost:8000).
After=network.target reachy-mini-daemon.service
Wants=reachy-mini-daemon.service

[Service]
Type=simple
User=root
WorkingDirectory=$HAL_DIR
# DEVICE_TYPE and DEVICES_DIR come from this file. Note systemd applies
# EnvironmentFile AFTER Environment=, so anything named in both takes the
# file's value — set it there, not below.
EnvironmentFile=$HAL_DIR/.env
# Safe as an Environment= line only because the .env does not set HF_HOME.
Environment=HF_HOME=$HF_HOME_PATH
# --timeout-graceful-shutdown: without it uvicorn waits forever for open
# connections (an SSE/MJPEG stream holds SIGTERM until systemd's 90s SIGKILL).
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
TimeoutStopSec=30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
wait_http "http://localhost:5001/health" 120 "HAL" || true
echo "--- health ---"; curl -s -m 5 localhost:5001/health; echo
echo "--- device ---"; curl -s -m 5 localhost:5001/device; echo
printf -- "--- media  --- "; curl -s -m 5 "$DAEMON_URL/api/media/status"; echo

cat <<EOF

========================================
  HAL running under systemd
    version : $(ota_field hal version)
    logs    : journalctl -u $SERVICE -f
    health  : curl localhost:5001/health
    routes  : curl localhost:5001/device
    stop    : sudo bash spike-hal.sh --stop   <- also returns media
========================================

Bound to 127.0.0.1: HAL is reached through os-server's admin proxy
(/api/hardware/*), not directly. For a browser, run spike-web.sh.

Footprint outside $HAL_DIR: /root/local (users, strangers, models), /var/log/hal.
EOF
