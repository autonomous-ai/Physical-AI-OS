#!/usr/bin/env bash
# runtimes/opencode/install.sh — installer for the OpenCode agentic backend.
set -euo pipefail

# Tee all output to a log under /root/.opencode (persistent rootfs), NOT /var/log — on these boards
# /var/log is a volatile zram mount wiped on reboot.
OPENCODE_LOG="${OPENCODE_LOG:-/root/.opencode/install.log}"
mkdir -p "$(dirname "$OPENCODE_LOG")"
exec > >(tee -a "$OPENCODE_LOG") 2>&1
echo "[install-opencode] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $OPENCODE_LOG) ====="

export HOME=/root
OPENCODE_BIN="/usr/local/bin/opencode"

# Latest published tag as of this build; see https://github.com/anomalyco/opencode/releases for
# newer.
OPENCODE_VERSION="${OPENCODE_VERSION:-1.18.4}"

echo "[install-opencode] prerequisites (jq, curl, tar)"
apt-get update || true
apt-get install -y jq curl tar || true

# The official script handles arch detection (linux arm64/x64), the .tar.gz asset name + extraction,
# and is idempotent (it exits early when the pinned version is already installed).
WANT_VERSION="${OPENCODE_VERSION#v}"
if [ -x "$OPENCODE_BIN" ] && "$OPENCODE_BIN" --version 2>/dev/null | grep -qw "$WANT_VERSION"; then
  echo "[install-opencode] opencode ${WANT_VERSION} already installed at $OPENCODE_BIN — skipping download"
else
  echo "[install-opencode] install opencode CLI ${WANT_VERSION} → /usr/local/bin"
  curl -fsSL https://opencode.ai/install \
    | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version "$WANT_VERSION"
fi
# Belt-and-suspenders: if the installer ignored OPENCODE_INSTALL_DIR (its behavior has drifted
# before), locate the binary it produced and copy it into /usr/local/bin so the systemd unit +
# verify hook find it at a stable path.
if [ ! -x "$OPENCODE_BIN" ]; then
  echo "[install-opencode] $OPENCODE_BIN missing — locating installer output"
  SRC="$(command -v opencode 2>/dev/null || true)"
  [ -x "$SRC" ] || SRC="/root/.opencode/bin/opencode"
  if [ -x "$SRC" ]; then
    install -m 0755 "$SRC" "$OPENCODE_BIN"
    echo "[install-opencode] copied $SRC → $OPENCODE_BIN"
  fi
fi
"$OPENCODE_BIN" --version || {
  echo "[install-opencode] ERROR: opencode binary not runnable after install" >&2
  exit 1
}

mkdir -p /root/.opencode/workspace /root/.opencode/attachments /root/.config/opencode

# opencode.json + env + persona migration are owned ENTIRELY by the presync hook, NOT written here.
PRESYNC_HOOK="/usr/local/bin/runtime-opencode-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-opencode] sync config/env now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-opencode] WARN: presync failed (config.json missing? non-fatal — retried on next switch/boot)"
else
  echo "[install-opencode] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); config/env NOT synced"
fi

# MUST stay in sync with runtimes/opencode/gateway_unit.go opencodeUnitContent (EnsureOnboarding
# self-heals the same unit on a hand-switched device).
echo "[install-opencode] write systemd unit opencode.service"
cat >/etc/systemd/system/opencode.service <<UNIT
[Unit]
Description=OpenCode agent gateway (os-server opencode-gatewayd driving \`opencode run\` per turn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.opencode/.env
WorkingDirectory=/root/.opencode
ExecStart=/usr/local/bin/os-server opencode-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-opencode] enable + start opencode.service"
systemctl enable --now opencode.service
systemctl status opencode.service --no-pager || true

echo "[install-opencode] declare verify hook for switch-runtime (opencode + os-server)"
mkdir -p /usr/local/lib/os-runtimes/opencode
cat >/usr/local/lib/os-runtimes/opencode/verify <<'VERIFY'
#!/usr/bin/env bash
command -v opencode >/dev/null 2>&1 && [ -x /usr/local/bin/os-server ]
VERIFY
chmod +x /usr/local/lib/os-runtimes/opencode/verify

echo "[install-opencode] done — opencode gatewayd installed + started (opencode.service)."
echo "[install-opencode] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
