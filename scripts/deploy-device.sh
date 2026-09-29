#!/usr/bin/env bash
# Dev push of HAL and/or os-server from the working tree to one LAN device (not an OTA release).
# Usage: scripts/deploy-device.sh --host IP [--hal|--os-server|--hal-log|--os-log] [--dry-run] [--jump HOST] [--user U --pass P]
# Never touches the device's .env, .venv or calibration/; no --delete, but stale checkouts overwrite newer device files.
set -euo pipefail

HOST="${PI_HOST:-${IP:-}}"
JUMP="${PI_JUMP:-${J:-}}"
USER="${PI_USER:-orangepi}"
PASS="${PI_PASS-orangepi}"
DO_HAL=0
DO_OS=0
SKIP_BUILD=0
DRY=0
LOG_UNIT=""      # non-empty puts us in log mode: tail and exit, deploy nothing

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)      HOST="$2"; shift 2 ;;
    --jump)      JUMP="$2"; shift 2 ;;
    --user)      USER="$2"; shift 2 ;;
    --pass)      PASS="$2"; shift 2 ;;
    --hal)       DO_HAL=1; shift ;;
    --os-server) DO_OS=1; shift ;;
    --hal-log)   LOG_UNIT="hal"; shift ;;
    --os-log)    LOG_UNIT="os-server"; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help)   sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Log mode is not a component: without this guard `--hal-log` alone would deploy everything.
if [[ -z "$LOG_UNIT" && $DO_HAL -eq 0 && $DO_OS -eq 0 ]]; then DO_HAL=1; DO_OS=1; fi

if [[ -z "$HOST" ]]; then
  echo "ERROR: no target. Pass --host <ip> or set IP=<ip>." >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$REPO_ROOT/system/os-server"
STAGE="/home/$USER/.deploy-stage"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR -o ConnectTimeout=10)

# RSYNC_RSH is word-split, so the jump host must be a single unspaced token.
if [[ -n "$JUMP" ]]; then
  case "$JUMP" in
    *[[:space:]]*) echo "ERROR: jump host must not contain whitespace: '$JUMP'" >&2; exit 2 ;;
  esac
  SSH_OPTS+=(-o "ProxyJump=$JUMP")
fi

if [[ -n "$PASS" ]]; then
  SSHPASS_BIN="$(command -v sshpass || echo "$HOME/.local/bin/sshpass")"
  if [[ ! -x "$SSHPASS_BIN" ]]; then
    echo "ERROR: sshpass not found (needed for password auth)." >&2
    echo "  brew install hudochenkov/sshpass/sshpass   — or set PI_PASS=\"\" to use your SSH key." >&2
    exit 1
  fi
  SSH=(env "SSHPASS=$PASS" "$SSHPASS_BIN" -e ssh "${SSH_OPTS[@]}")
  SCP=(env "SSHPASS=$PASS" "$SSHPASS_BIN" -e scp "${SSH_OPTS[@]}")
  RSYNC_RSH="$SSHPASS_BIN -e ssh ${SSH_OPTS[*]}"
  RSYNC_ENV=(env "SSHPASS=$PASS")
  SUDO="echo '$PASS' | sudo -S -p ''"
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp "${SSH_OPTS[@]}")
  RSYNC_RSH="ssh ${SSH_OPTS[*]}"
  RSYNC_ENV=(env)
  SUDO="sudo"
fi

echo "=== Preflight: $USER@$HOST${JUMP:+ (via $JUMP)} ==="
# ICMP is advisory only: jump-host targets are never pingable; SSH is the real check.
ping -c 1 -W 2 "$HOST" >/dev/null 2>&1 \
  || echo "  (no ICMP reply — continuing; SSH is the authoritative check)"
"${SSH[@]}" "$USER@$HOST" true || {
  echo "ERROR: SSH to $USER@$HOST${JUMP:+ via $JUMP} failed (wrong user/password/key/jump host?)." >&2
  exit 1; }
echo "OK — $("${SSH[@]}" "$USER@$HOST" 'hostname; uname -m' | paste -sd' ' -)"

# Log mode must run before staging/build so it never touches the device's files.
if [[ -n "$LOG_UNIT" ]]; then
  log_lines="${LOG_LINES:-200}"          # LOG_LINES, not LINES: bash owns LINES
  follow="${FOLLOW:-1}"
  remote="journalctl -u $LOG_UNIT -n $log_lines --no-pager"
  [[ "$follow" == "1" ]] && remote+=" -f"
  [[ -n "${GREP:-}" ]] && remote+=" | grep --line-buffered -iE $(printf '%q' "$GREP")"
  banner="=== $LOG_UNIT journal on $HOST — last $log_lines line(s)"
  [[ -n "${GREP:-}" ]] && banner+=", filter: $GREP"
  [[ "$follow" == "1" ]] && banner+=", following (Ctrl-C to stop)"
  echo "$banner ==="
    # -t so Ctrl-C reaches journalctl on the device instead of orphaning it.
  exec "${SSH[@]}" -t "$USER@$HOST" "$remote"
fi

"${SSH[@]}" "$USER@$HOST" "mkdir -p '$STAGE'"

if [[ $DO_OS -eq 1 && $SKIP_BUILD -eq 0 ]]; then
  echo
  echo "=== Build os-server (linux/arm64) ==="
  make -C "$REPO_ROOT" os-build
fi
if [[ $DO_OS -eq 1 && ! -f "$BIN" ]]; then
  echo "ERROR: $BIN missing — run 'make os-build'." >&2; exit 1
fi

HAL_EXCLUDES=(--exclude __pycache__ --exclude '*.pyc' --exclude .pytest_cache
  --exclude .venv --exclude '.venv-*' --exclude .env
  --exclude calibration --exclude recordings)

if [[ $DRY -eq 1 ]]; then
  echo
  if [[ $DO_HAL -eq 1 ]]; then
    echo "=== DRY RUN: hal/ files that would change on $HOST ==="
      # No sudo: piping a password via --rsync-path corrupts the rsync protocol stream.
    if ! out="$("${RSYNC_ENV[@]}" rsync -rn --checksum --itemize-changes "${HAL_EXCLUDES[@]}" \
        -e "$RSYNC_RSH" "$REPO_ROOT/hal/" "$USER@$HOST:/opt/hal/" 2>&1)"; then
      printf '%s\n' "$out" >&2
      echo "ERROR: dry run failed — refusing to report 'in sync'." >&2
      exit 1
    fi
    changed="$(printf '%s\n' "$out" | grep '^[<>ch]' || true)"
    if [[ -n "$changed" ]]; then
      printf '%s\n' "$changed"
      printf '  → %s file(s) would be overwritten on the device\n' \
        "$(printf '%s\n' "$changed" | wc -l | tr -d ' ')"
    else
      echo "  (in sync)"
    fi
  fi
  if [[ $DO_OS -eq 1 ]]; then
    echo "=== DRY RUN: would install $BIN → /usr/local/bin/os-server ==="
  fi
  echo "(nothing was changed)"
  exit 0
fi

if [[ $DO_HAL -eq 1 ]]; then
  echo
  echo "=== 1) rsync hal/ → $STAGE ==="
    # Staged first: /opt/hal is root-owned and rsync cannot write there as $USER.
  "${RSYNC_ENV[@]}" rsync -a --info=stats1 "${HAL_EXCLUDES[@]}" \
    -e "$RSYNC_RSH" "$REPO_ROOT/hal/" "$USER@$HOST:$STAGE/hal/"

  echo
  echo "=== 2) swap into /opt/hal + restart hal ==="
  "${SSH[@]}" "$USER@$HOST" "$SUDO sh -c '
    rsync -a --exclude __pycache__ --exclude .venv --exclude .env --exclude calibration \
      $STAGE/hal/ /opt/hal/ &&
    find /opt/hal -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null;
    systemctl restart hal'"

  echo "   waiting for hal ..."
  "${SSH[@]}" "$USER@$HOST" '
    for i in $(seq 1 60); do
      if [ "$(curl -s -o /dev/null -w %{http_code} --max-time 3 http://127.0.0.1:5001/health)" = "200" ]; then
        echo "   hal healthy after ${i}s"; exit 0
      fi
      sleep 1
    done
    echo "   WARNING: hal did not answer /health within 60s"; exit 1'
fi

if [[ $DO_OS -eq 1 ]]; then
  echo
  echo "=== 3) scp os-server + restart ==="
  "${SCP[@]}" "$BIN" "$USER@$HOST:$STAGE/os-server.new"
  "${SSH[@]}" "$USER@$HOST" "$SUDO sh -c '
    install -m 0755 -o root -g root $STAGE/os-server.new /usr/local/bin/os-server &&
    systemctl restart os-server'"
fi

echo
echo "=== Result ==="
"${SSH[@]}" "$USER@$HOST" '
  printf "  hal        %s (v%s)\n" "$(systemctl is-active hal)" "$(cat /opt/hal/VERSION_HAL 2>/dev/null || echo ?)"
  printf "  os-server  %s\n" "$(systemctl is-active os-server)"'
echo "✔ deploy complete."
