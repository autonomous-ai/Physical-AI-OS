#!/usr/bin/env bash
# spike-agent.sh — install the OpenClaw gateway (version pinned by OTA metadata) on a Reachy Mini.
# Usage: sudo [OPENCLAW_VERSION=x.y.z] bash spike-agent.sh [--stop|--uninstall]
set -euo pipefail

SPIKE_TAG="spike-agent"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="openclaw"
# Must match os-server's path, else the gateway mints its own token (WS 1008 token_mismatch).
OPENCLAW_HOME="/root/.openclaw"

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
  say "Stopping the OpenClaw gateway"
  stop_unit "$SERVICE"
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    npm uninstall -g openclaw >/dev/null 2>&1 || true
    info "removed the unit and the global openclaw package"
    info "kept $OPENCLAW_HOME — it holds the gateway token os-server has cached,"
    info "plus agent sessions and credentials; deleting it forces a re-onboard"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/5  Resolve the version from OTA"
ensure_tools
# Strip zero-padded months ("2026.06.10" -> "2026.6.10"): npm rejects leading zeros.
OTA_VERSION="$(ota_field openclaw version)"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-$OTA_VERSION}"
[ -n "$OPENCLAW_VERSION" ] || die "OTA metadata has no openclaw version, and OPENCLAW_VERSION is unset"
NPM_SPEC="$(echo "$OPENCLAW_VERSION" | sed -E 's/(^|\.)0+([0-9])/\1\2/g')"
info "OTA pin: ${OTA_VERSION:-<none>}   installing: $NPM_SPEC"

say "2/5  Install Node.js 22"
avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
info "free space: $(awk -v k="$avail_kb" 'BEGIN {printf "%.1f", k/1048576}') GB"
[ "$avail_kb" -ge 1572864 ] || die "need at least 1.5 GB free on / for node + the openclaw package"

# NodeSource, not apt: Debian ships node 20 and openclaw refuses to start below 22.19.
node_major() { node --version 2>/dev/null | sed 's/^v//; s/\..*//'; }
if [ "$(node_major)" ] && [ "$(node_major)" -ge 22 ] 2>/dev/null; then
  info "node $(node --version), npm $(npm --version) already present"
else
  info "installing Node.js 22 (NodeSource)"
  retry "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -" 3 \
    || die "NodeSource setup script failed"
  apt-get install -y nodejs || die "apt-get install nodejs failed"
  info "installed node $(node --version), npm $(npm --version)"
fi
# An apt pin or leftover /usr/local/bin/node can still win on PATH.
[ "$(node_major)" -ge 22 ] 2>/dev/null || die "node is $(node --version 2>/dev/null || echo missing) — openclaw needs >= 22.19"

say "3/5  Install openclaw@$NPM_SPEC"
# Skip browser downloads at install time (no browser capability on this device).
installed="$(openclaw --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "$installed" = "$NPM_SPEC" ]; then
  info "openclaw $NPM_SPEC already installed"
else
  [ -n "$installed" ] && info "installed openclaw is $installed — replacing with $NPM_SPEC"
  retry "env PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --no-fund --no-audit --omit=optional 'openclaw@$NPM_SPEC'" 3 30 \
    || die "npm install openclaw@$NPM_SPEC failed"
fi
command -v openclaw >/dev/null || die "openclaw is not on PATH after install"
info "openclaw version: $(openclaw --version 2>&1 | head -1)"

say "4/5  Seed $OPENCLAW_HOME"
# 700 up front keeps credentials and sessions private regardless of the gateway's umask.
mkdir -p \
  "$OPENCLAW_HOME" "$OPENCLAW_HOME/workspace" \
  "$OPENCLAW_HOME/agents/main/agent" "$OPENCLAW_HOME/agents/main/sessions" \
  "$OPENCLAW_HOME/credentials" "$OPENCLAW_HOME/.cache" "$OPENCLAW_HOME/.config" \
  "$OPENCLAW_HOME/.local/share" /var/log/openclaw
for p in "$OPENCLAW_HOME" "$OPENCLAW_HOME/workspace" "$OPENCLAW_HOME/agents" \
         "$OPENCLAW_HOME/agents/main" "$OPENCLAW_HOME/agents/main/agent" \
         "$OPENCLAW_HOME/agents/main/sessions" "$OPENCLAW_HOME/credentials" \
         "$OPENCLAW_HOME/.cache" "$OPENCLAW_HOME/.config" "$OPENCLAW_HOME/.local" \
         "$OPENCLAW_HOME/.local/share"; do
  chmod 700 "$p" 2>/dev/null || true
done

# Never regenerate the token: os-server caches it and the file holds onboarding state.
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
  info "openclaw.json already present — keeping it (token preserved)"
else
  TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat >"$OPENCLAW_HOME/openclaw.json" <<JSON
{
  "agents": {
    "defaults": {
      "workspace": "$OPENCLAW_HOME/workspace"
    }
  },
  "gateway": {
    "mode": "local",
    "bind": "loopback",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "$TOKEN"
    },
    "controlUi": {
      "allowedOrigins": ["http://127.0.0.1", "http://localhost"],
      "allowInsecureAuth": false
    }
  }
}
JSON
  chmod 600 "$OPENCLAW_HOME/openclaw.json"
  info "seeded openclaw.json with a fresh gateway token"
fi

say "5/5  Install the unit and start"
OPENCLAW_BIN="$(command -v openclaw)"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$OPENCLAW_HOME
Environment="OPENCLAW_HOME=$OPENCLAW_HOME"
Environment="OPENCLAW_STATE_DIR=$OPENCLAW_HOME"
# HOME stays /root while the XDG dirs point into $OPENCLAW_HOME: the gateway
# scatters caches and state across all three, and letting them land in /root
# means --uninstall leaves them behind and a token can outlive its config.
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=$OPENCLAW_HOME/.cache"
Environment="XDG_CONFIG_HOME=$OPENCLAW_HOME/.config"
Environment="XDG_DATA_HOME=$OPENCLAW_HOME/.local/share"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=$OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
# Wait past the first RestartSec=5 so a crash-loop is not reported as healthy.
sleep 8
if systemctl is-active --quiet "$SERVICE"; then
  info "running"
else
  info "WARN: not running — journalctl -u $SERVICE -n 50"
  info "WARN: 'Node.js v22.19+ is required' there means something outranks NodeSource on PATH"
fi
# Check the port too: the unit can be active with plugin load wedged.
if command -v ss >/dev/null && ! ss -ltn 2>/dev/null | grep -q ':18789'; then
  info "WARN: nothing is listening on 18789 yet — os-server's WS dial will fail until it is"
fi

cat <<EOF

========================================
  OpenClaw gateway running under systemd
    version : $(openclaw --version 2>&1 | head -1)
    OTA pin : ${OTA_VERSION:-<none>}
    logs    : journalctl -u $SERVICE -f
    port    : 18789 (loopback) — os-server dials ws://127.0.0.1:18789
    state   : $OPENCLAW_HOME
    stop    : sudo bash spike-agent.sh --stop
========================================

Once the gateway reports ready, os-server's status reporter starts pinging and
the backend assigns device_id + the MQTT fa/fd channels. If os-server is not
installed yet, run spike-os.sh.
EOF
