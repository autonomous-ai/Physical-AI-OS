#!/usr/bin/env bash
# spike-device.sh — install the Reachy Mini device profile from OTA and apply its rootfs overlay (run first).
# Usage: sudo bash spike-device.sh [--force-env|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-device"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

FORCE_ENV=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --force-env) FORCE_ENV=1 ;;
# Deprecated no-op kept for old command lines.
    --keep-env)  info "--keep-env is the default now (use --force-env to replace .env)" ;;
    --uninstall) UNINSTALL=1 ;;
# Not a service; accepted so spike.sh teardown succeeds.
    --stop)      info "nothing to stop — the device profile is not a service"; exit 0 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$UNINSTALL" = "1" ]; then
  say "Removing the device profile"
  rm -rf "${DEVICES_DIR:?}/$DEVICE_TYPE"
  info "removed $DEVICES_DIR/$DEVICE_TYPE"
  info "left in place: /etc/asound.conf and $HAL_DIR/.env — other units still read them"
  exit 0
fi

say "1/3  Fetch the device profile from OTA"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
ota_unpack device "$STAGING"

[ -f "$STAGING/ROBOT.md" ] || die "the $DEVICE_TYPE package has no ROBOT.md"

say "2/3  Install to $DEVICES_DIR/$DEVICE_TYPE"
mkdir -p "$DEVICES_DIR/$DEVICE_TYPE"
# Mirror, not merge: files removed upstream must disappear here too.
rm -rf "${DEVICES_DIR:?}/$DEVICE_TYPE"
mkdir -p "$DEVICES_DIR/$DEVICE_TYPE"
cp -a "$STAGING/." "$DEVICES_DIR/$DEVICE_TYPE/"
info "profile version: $(cat "$DEVICES_DIR/$DEVICE_TYPE/VERSION" 2>/dev/null || echo unknown)"

say "3/3  Apply the rootfs overlay onto /"
OVERLAY="$DEVICES_DIR/$DEVICE_TYPE/rootfs"
if [ ! -d "$OVERLAY" ]; then
  info "WARN: this package has no rootfs/ — audio config will be missing."
  info "WARN: publish a newer device package (make upload-device $DEVICE_TYPE)."
  exit 0
fi

# Back up any pre-existing Pollen file once before overwriting it.
while IFS= read -r rel; do
  target="/$rel"
  src="$OVERLAY/$rel"
  if [ -f "$target" ] && ! cmp -s "$target" "$src" && [ ! -f "$target.pre-autonomous" ]; then
    cp -a "$target" "$target.pre-autonomous"
    info "backed up $target -> $target.pre-autonomous"
  fi
done < <(cd "$OVERLAY" && find . -type f -printf '%P\n')

# Keep an operator-tuned .env by default (install.sh is also the update path); --force-env replaces it.
ENV_SRC="$OVERLAY/opt/hal/.env"
ENV_DST="$HAL_DIR/.env"
PRESERVE_ENV=0
if [ -f "$ENV_DST" ] && [ "$FORCE_ENV" = "0" ]; then
  PRESERVE_ENV=1
  ENV_BACKUP="$(mktemp)"
  cp -a "$ENV_DST" "$ENV_BACKUP"
fi

mkdir -p "$HAL_DIR"
cp -a "$OVERLAY/." /

if [ "$PRESERVE_ENV" = "1" ]; then
  cp -a "$ENV_BACKUP" "$ENV_DST"
  rm -f "$ENV_BACKUP"
  if [ -f "$ENV_SRC" ] && ! cmp -s "$ENV_DST" "$ENV_SRC"; then
    info "kept the existing $ENV_DST — it differs from the package's"
    info "  (diff: $(diff <(sort "$ENV_SRC") <(sort "$ENV_DST") | grep -c '^[<>]') lines; --force-env to replace it)"
  else
    info "kept the existing $ENV_DST"
  fi
elif [ -f "$ENV_SRC" ]; then
  [ "$FORCE_ENV" = "1" ] && info "--force-env: replaced $ENV_DST from the package" \
                         || info "installed $ENV_DST from the package"
fi

info "overlay applied:"
(cd "$OVERLAY" && find . -type f -printf '  /%P\n')

cat <<EOF

========================================
  Device profile installed
    profile : $DEVICES_DIR/$DEVICE_TYPE
    board   : $(grep -oE 'boards:.*' "$DEVICES_DIR/$DEVICE_TYPE/ROBOT.md" 2>/dev/null || echo '?')
    next    : sudo bash spike-hal.sh
========================================
EOF
