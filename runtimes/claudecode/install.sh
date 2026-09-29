#!/usr/bin/env bash
# runtimes/claudecode/install.sh — installer for the Claude Code agentic backend.
set -euo pipefail

# Tee all output to a log under /root/.claudecode (persistent rootfs), NOT /var/log — on these
# boards /var/log is a volatile zram mount wiped on reboot.
CC_LOG="${CC_LOG:-/root/.claudecode/install.log}"
mkdir -p "$(dirname "$CC_LOG")"
exec > >(tee -a "$CC_LOG") 2>&1
echo "[install-claudecode] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $CC_LOG) ====="

export HOME=/root
CLAUDE_BIN="/usr/local/bin/claude"

echo "[install-claudecode] prerequisites (jq, curl)"
apt-get update || true
apt-get install -y jq curl || true

echo "[install-claudecode] install Claude Code CLI"
if ! command -v claude >/dev/null 2>&1 && [ ! -x /root/.local/bin/claude ]; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
if [ -x /root/.local/bin/claude ]; then
  ln -sf /root/.local/bin/claude "$CLAUDE_BIN"
fi
command -v claude >/dev/null 2>&1 || {
  echo "[install-claudecode] ERROR: claude CLI not found after install" >&2
  exit 1
}
claude --version || true

# Native channel plugins are not used: they would compete with the device-owned loops for the bot.

# Env + channel config are owned ENTIRELY by the presync hook, NOT written here (the bridge itself
# ships inside the os-server binary).
PRESYNC_HOOK="/usr/local/bin/runtime-claudecode-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-claudecode] sync env/channels now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-claudecode] WARN: presync failed (config.json missing? non-fatal — retried on next switch/boot)"
else
  echo "[install-claudecode] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); env NOT configured"
fi

# KEEP IN SYNC with runtimes/claudecode/gateway_unit.go.
echo "[install-claudecode] write systemd unit claudecode.service"
cat >/etc/systemd/system/claudecode.service <<UNIT
[Unit]
Description=Claude Code agent bridge (os-server claudecode-gatewayd holding one headless claude)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.claudecode/.env
WorkingDirectory=/root/.claudecode
ExecStart=/usr/local/bin/os-server claudecode-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-claudecode] enable + start claudecode.service"
systemctl enable --now claudecode.service
systemctl status claudecode.service --no-pager || true

echo "[install-claudecode] declare verify hook for switch-runtime (claude + os-server)"
mkdir -p /usr/local/lib/os-runtimes/claudecode
cat >/usr/local/lib/os-runtimes/claudecode/verify <<'VERIFY'
#!/usr/bin/env bash
command -v claude >/dev/null 2>&1 && [ -x /usr/local/bin/os-server ]
VERIFY
chmod +x /usr/local/lib/os-runtimes/claudecode/verify

echo "[install-claudecode] done — claudecode gatewayd installed + started (claudecode.service)."
echo "[install-claudecode] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
