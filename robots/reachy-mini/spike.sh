#!/usr/bin/env bash
# spike.sh — bring the full Autonomous stack up on a Reachy Mini from OTA (runs on the robot).
# Usage: sudo bash spike.sh [--no-deps] [--skip STEP]... [--stop|--uninstall]
# Order matters: bootstrap runs last because it can restart os-server/hal mid-install.
set -euo pipefail

SPIKE_TAG="spike"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=spike-lib.sh
. "$HERE/spike-lib.sh"

STEPS=(device hal os web agent bootstrap)

PASS_ARGS=()
SKIP=()
MODE="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-deps)   PASS_ARGS+=("--no-deps") ;;
    --skip)      shift; [ $# -gt 0 ] || die "--skip needs a step name"; SKIP+=("$1") ;;
    --stop)      MODE="stop" ;;
    --uninstall) MODE="uninstall" ;;
    *) die "unknown flag: $1 (steps: ${STEPS[*]})" ;;
  esac
  shift
done

ensure_root "$@"

script_for() { echo "$HERE/spike-$1.sh"; }

skipped() {
  local s
  for s in ${SKIP[@]+"${SKIP[@]}"}; do [ "$s" = "$1" ] && return 0; done
  return 1
}

# Teardown in reverse order so bootstrap cannot reinstall a component being removed.
if [ "$MODE" != "install" ]; then
  flag="--stop"; [ "$MODE" = "uninstall" ] && flag="--uninstall"
  say "Tearing down (${MODE})"
  for (( i=${#STEPS[@]}-1 ; i>=0 ; i-- )); do
    step="${STEPS[$i]}"
    s="$(script_for "$step")"
    [ -f "$s" ] || continue
    bash "$s" "$flag" || info "WARN: $step $flag failed — continuing"
  done
  echo
  info "done. Pollen OS is untouched apart from what each script's header lists."
  exit 0
fi

say "Reachy Mini — full spike"
ensure_tools
# One feed snapshot per run so all steps install matching builds.
clear_metadata_cache
info "OTA feed : $(metadata_url)"
info "device   : $DEVICE_TYPE"
info "steps    : ${STEPS[*]}"
[ ${#SKIP[@]} -eq 0 ] || info "skipping : ${SKIP[*]}"

for step in "${STEPS[@]}"; do
  [ -f "$(script_for "$step")" ] || die "missing $(script_for "$step") — copy the whole folder over"
done

START_TS=$SECONDS
for step in "${STEPS[@]}"; do
  if skipped "$step"; then
    info "skip $step"
    continue
  fi
  say "STEP: $step"
# --no-deps is HAL-only; other steps reject unknown flags.
  if [ "$step" = "hal" ]; then
    bash "$(script_for "$step")" ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}
  else
    bash "$(script_for "$step")"
  fi
done

say "Bring-up complete in $(( (SECONDS - START_TS) / 60 ))m $(( (SECONDS - START_TS) % 60 ))s"
# Summary to stderr so it does not interleave with block-buffered stdout.
{
  printf 'hal       : '; curl -sf -m 5 localhost:5001/health >/dev/null 2>&1 && echo up || echo DOWN
  printf 'os-server : '; curl -sf -m 5 localhost:5000/api/health/live >/dev/null 2>&1 && echo up || echo DOWN
  printf 'web       : '; curl -sf -m 5 localhost/ >/dev/null 2>&1 && echo up || echo DOWN
  printf 'media     : '; curl -s -m 5 "$DAEMON_URL/api/media/status" 2>/dev/null || echo '?'; echo
  printf 'motors    : '; curl -s -m 5 "$DAEMON_URL/api/motors/status" 2>/dev/null || echo '?'; echo
} >&2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat >&2 <<EOF

========================================
  Open http://${IP:-<robot-ip>}/setup?debug=true&device_id=reachy-1 in a browser to finish setup
  (debug=true shows the AI-key and chat steps the phone app would otherwise fill in).

  logs   : journalctl -u hal -u os-server -u openclaw -u bootstrap -f
  stop   : sudo bash spike.sh --stop
  remove : sudo bash spike.sh --uninstall
========================================

Everything survives a reboot — each step installs a systemd unit.
EOF
