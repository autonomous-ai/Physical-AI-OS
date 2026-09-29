#!/usr/bin/env bash
# rev: 2026-09-09T08:45:07Z
# Configure Hermes on this Mac to accept remote agent connections from an Intern 2 device on the LAN.
# Usage: curl -fsSL https://cdn.autonomous.ai/os/tools/hermes-lan-setup.sh | bash
# Env overrides: HERMES_ROOT, HERMES_PORT (8642), HERMES_HOST (0.0.0.0), HERMES_TOKEN

set -uo pipefail

log()  { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Locate the Hermes CLI and its Python interpreter
HERMES_BIN="${HERMES_BIN:-}"
if [ -z "$HERMES_BIN" ] && [ -n "${HERMES_ROOT:-}" ] && [ -x "$HERMES_ROOT/bin/hermes" ]; then
  HERMES_BIN="$HERMES_ROOT/bin/hermes"
fi
if [ -z "$HERMES_BIN" ]; then
  HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
fi
if [ -z "$HERMES_BIN" ]; then
  for candidate in \
    "$HOME/.grid/tools/hermes-agent/bin/hermes" \
    "$HOME/.local/bin/hermes" \
    "$HOME/.local/pipx/venvs/hermes-agent/bin/hermes" \
    "$HOME/.local/share/pipx/venvs/hermes-agent/bin/hermes" \
    "$HOME/Library/Python/3.13/bin/hermes" \
    "$HOME/Library/Python/3.12/bin/hermes" \
    "$HOME/Library/Python/3.11/bin/hermes" \
    "/opt/homebrew/bin/hermes" \
    "/usr/local/bin/hermes"; do
    if [ -x "$candidate" ]; then
      HERMES_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$HERMES_BIN" ]; then
  die "hermes CLI not found. If Hermes is installed but not on PATH, set HERMES_BIN=/path/to/hermes and re-run. If Hermes Desktop is installed, enable the API server in its Settings tab instead — this script drives the CLI install."
fi

# Resolve the Python that runs Hermes (HERMES_PY short-circuits the heuristics).
HERMES_PY="${HERMES_PY:-}"

if [ -z "$HERMES_PY" ]; then
  shebang="$(head -1 "$HERMES_BIN" 2>/dev/null)"
  candidate="$(printf '%s' "$shebang" | sed -n 's|^#![[:space:]]*\(/[^[:space:]]*\).*|\1|p')"
  case "$candidate" in
    */env)
      env_arg="$(printf '%s' "$shebang" | awk '{print $2}')"
      resolved="$(command -v "$env_arg" 2>/dev/null || true)"
      [ -n "$resolved" ] && HERMES_PY="$resolved"
      ;;
    */bash|*/sh|*/zsh|*/dash|*/ksh)
      resolved="$(grep -oE '/[[:alnum:]/._-]+/python(3(\.[0-9]+)?)?' "$HERMES_BIN" 2>/dev/null | head -1)"
      if [ -z "$resolved" ] || [ ! -x "$resolved" ]; then
        trace_py="$(bash -x "$HERMES_BIN" --help </dev/null 2>&1 | \
          grep -oE '/[[:alnum:]/._-]+/python(3(\.[0-9]+)?)?' | head -1)"
        [ -n "$trace_py" ] && [ -x "$trace_py" ] && resolved="$trace_py"
      fi
      [ -n "$resolved" ] && [ -x "$resolved" ] && HERMES_PY="$resolved"
      ;;
    /*)
      [ -x "$candidate" ] && HERMES_PY="$candidate"
      ;;
  esac
fi

if [ -z "$HERMES_PY" ]; then
  bin_dir="$(dirname "$HERMES_BIN")"
  for name in python3 python; do
    if [ -x "$bin_dir/$name" ]; then
      HERMES_PY="$bin_dir/$name"
      break
    fi
  done
fi

if [ -z "$HERMES_PY" ]; then
  for candidate in \
    "$HOME/.hermes/hermes-agent/venv/bin/python" \
    "$HOME/.hermes/venv/bin/python" \
    "$HOME/hermes-agent/venv/bin/python" \
    "$HOME/.local/pipx/venvs/hermes-agent/bin/python" \
    "$HOME/.local/share/pipx/venvs/hermes-agent/bin/python" \
    "/opt/homebrew/opt/hermes-agent/venv/bin/python" \
    "/usr/local/opt/hermes-agent/venv/bin/python"; do
    if [ -x "$candidate" ]; then
      HERMES_PY="$candidate"
      break
    fi
  done
fi

if [ -z "$HERMES_PY" ]; then
  for candidate in $(command -v python3 python 2>/dev/null); do
    if "$candidate" -c "import hermes" 2>/dev/null; then
      HERMES_PY="$candidate"
      break
    fi
  done
fi

if [ -n "$HERMES_PY" ] && ! "$HERMES_PY" -c 'import sys' 2>/dev/null; then
  HERMES_PY=""
fi

if [ -z "$HERMES_PY" ] || [ ! -x "$HERMES_PY" ]; then
  cat >&2 <<EOF
✗ Could not resolve Python interpreter for $HERMES_BIN.

Find the python that runs your Hermes install:
  find \$HOME/.hermes \$HOME/hermes-agent \$HOME/.local -name python -path '*/venv/*' 2>/dev/null
or:
  head -50 $HERMES_BIN | grep -o '/[^ ]*python[0-9.]*' | head -1

Then re-run with an override on the RIGHT side of the pipe (env must apply to bash, not curl):
  curl -fsSL "https://cdn.autonomous.ai/os/tools/hermes-lan-setup.sh?v=\$(uuidgen)" | HERMES_PY=/full/path/to/python bash
EOF
  exit 1
fi

log "Hermes CLI     : $HERMES_BIN"
log "Hermes Python  : $HERMES_PY"

# 2. Ensure aiohttp is available to the Hermes CLI's Python
if ! "$HERMES_PY" -c "import aiohttp" 2>/dev/null; then
  log "Installing aiohttp for Hermes (one-off)…"
  installed=0
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$HERMES_PY" aiohttp >/dev/null 2>&1 && installed=1
  fi
  if [ "$installed" -eq 0 ]; then
    if "$HERMES_PY" -m pip --version >/dev/null 2>&1 \
       || "$HERMES_PY" -m ensurepip >/dev/null 2>&1; then
      "$HERMES_PY" -m pip install --user aiohttp >/dev/null 2>&1 && installed=1
    fi
  fi
  [ "$installed" -eq 1 ] || die "Failed to install aiohttp. Install uv (https://docs.astral.sh/uv/) or ensure pip works for $HERMES_PY, then re-run."
fi
ok "aiohttp available to Hermes"

# 3. Write API_SERVER_* into ~/.hermes/.env
HERMES_DIR="$HOME/.hermes"
ENV_FILE="$HERMES_DIR/.env"
mkdir -p "$HERMES_DIR"
touch "$ENV_FILE"

HERMES_PORT="${HERMES_PORT:-8642}"
HERMES_HOST="${HERMES_HOST:-0.0.0.0}"

# Reuse an existing token so re-runs don't rotate an in-use key.
existing_token="$(awk -F= '/^API_SERVER_KEY=/{print $2; exit}' "$ENV_FILE" 2>/dev/null || true)"
if [ -n "${HERMES_TOKEN:-}" ]; then
  TOKEN="$HERMES_TOKEN"
elif [ -n "$existing_token" ]; then
  TOKEN="$existing_token"
else
  TOKEN="intern2-hermes-$(openssl rand -hex 8)"
fi

for k in API_SERVER_ENABLED API_SERVER_HOST API_SERVER_PORT API_SERVER_KEY; do
  sed -i.bak "/^${k}=/d" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
done
[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
{
  echo ""
  echo "# --- Intern 2 remote-agent bridge (added $(date -u +%FT%TZ)) ---"
  echo "API_SERVER_ENABLED=true"
  echo "API_SERVER_HOST=$HERMES_HOST"
  echo "API_SERVER_PORT=$HERMES_PORT"
  echo "API_SERVER_KEY=$TOKEN"
} >> "$ENV_FILE"
ok "~/.hermes/.env updated (API_SERVER_ENABLED + host + port + key)"

# 4. Reload Hermes: restart a running gateway, else install launchd service, else foreground start
running_pid="$(pgrep -f 'hermes gateway run' | head -1 || true)"
if [ -n "$running_pid" ]; then
  log "Foreground gateway running (pid=$running_pid) — restarting so it picks up the new env…"
  kill -TERM "$running_pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    kill -0 "$running_pid" 2>/dev/null || break
  done
  kill -0 "$running_pid" 2>/dev/null && kill -KILL "$running_pid" 2>/dev/null || true
    # macOS TIME_WAIT blocks a fresh bind; wait up to 15s for the port to clear.
  for _ in $(seq 1 15); do
    if ! lsof -iTCP:"$HERMES_PORT" -sTCP:LISTEN -n >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  nohup "$HERMES_BIN" gateway run >/tmp/hermes-gateway.log 2>&1 &
  ok "Foreground gateway restarted (log: /tmp/hermes-gateway.log)"
else
  log "Installing launchd service (persistent across reboots)…"
  if yes y | "$HERMES_BIN" gateway install >/tmp/hermes-install.log 2>&1; then
    ok "launchd service installed"
  else
    warn "launchd install returned non-zero — falling back to a foreground gateway"
    nohup "$HERMES_BIN" gateway run >/tmp/hermes-gateway.log 2>&1 &
    ok "Foreground gateway started (log: /tmp/hermes-gateway.log)"
  fi
fi

sleep 4
if lsof -iTCP:"$HERMES_PORT" -sTCP:LISTEN -n 2>/dev/null | grep -q "$HERMES_PORT"; then
  ok "Hermes API server listening on $HERMES_HOST:$HERMES_PORT"
else
  warn "Hermes API server didn't come up on $HERMES_PORT — check /tmp/hermes-gateway.log or /tmp/hermes-install.log"
fi

# 5. Print the fields the device needs
LAN_IP=""
for iface in en0 en1 en2 en3; do
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  if [ -n "$ip" ]; then
    LAN_IP="$ip"
    break
  fi
done
[ -n "$LAN_IP" ] || LAN_IP="<your-mac-lan-ip>"

echo
printf '\033[1m'
echo "════════════════════════════════════════════════════════════════"
echo "  Paste these into your Intern 2 web setup page"
echo "  (Settings → Runtime → Remote (external))"
echo "════════════════════════════════════════════════════════════════"
printf '\033[0m'
echo
echo "  Hermes URL : http://$LAN_IP:$HERMES_PORT"
echo "  API Key    : $TOKEN"
echo
echo "Verify from another machine on the LAN:"
echo "  curl -H 'Authorization: Bearer $TOKEN' http://$LAN_IP:$HERMES_PORT/health"
echo
