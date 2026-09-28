#!/usr/bin/env bash
# runtimes/codex/install.sh — installer for the Codex agentic backend.
set -euo pipefail

# Tee all output to a log under /root/.codex (persistent rootfs), NOT /var/log — on these boards
# /var/log is a volatile zram mount wiped on reboot.
CODEX_LOG="${CODEX_LOG:-/root/.codex/install.log}"
mkdir -p "$(dirname "$CODEX_LOG")"
exec > >(tee -a "$CODEX_LOG") 2>&1
echo "[install-codex] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $CODEX_LOG) ====="

export HOME=/root
CODEX_BIN="/usr/local/bin/codex"

# Pinned release; bump here and re-OTA os-server to upgrade.
CODEX_VERSION="${CODEX_VERSION:-rust-v0.142.5}"
CODEX_REPO="${CODEX_REPO:-openai/codex}"

echo "[install-codex] prerequisites (jq, curl)"
apt-get update || true
apt-get install -y jq curl || true

echo "[install-codex] install Codex CLI ${CODEX_VERSION}"
case "$(uname -m)" in
  aarch64|arm64) CODEX_ASSET="codex-aarch64-unknown-linux-musl.tar.gz" ;;
  x86_64)        CODEX_ASSET="codex-x86_64-unknown-linux-musl.tar.gz" ;;
  *) echo "[install-codex] ERROR: unsupported arch $(uname -m) for codex"; exit 1 ;;
esac
# Idempotency: skip the download when the pinned version is already installed.
WANT_VERSION="${CODEX_VERSION#rust-v}"
if command -v codex >/dev/null 2>&1 && [ "$(codex --version 2>/dev/null | awk '{print $2}')" = "$WANT_VERSION" ]; then
  echo "[install-codex] codex ${WANT_VERSION} already installed — skipping download"
else
  CODEX_URL="https://github.com/${CODEX_REPO}/releases/download/${CODEX_VERSION}/${CODEX_ASSET}"
  CODEX_TMP="$(mktemp -d)"
  echo "[install-codex] downloading ${CODEX_URL}"
  curl -fsSL "$CODEX_URL" -o "$CODEX_TMP/$CODEX_ASSET"
  tar -xzf "$CODEX_TMP/$CODEX_ASSET" -C "$CODEX_TMP"
  install -m 0755 "$CODEX_TMP/${CODEX_ASSET%.tar.gz}" "$CODEX_BIN"
  rm -rf "$CODEX_TMP"
fi
"$CODEX_BIN" --version || {
  echo "[install-codex] ERROR: codex binary not runnable after install" >&2
  exit 1
}

mkdir -p /root/.codex/workspace /root/.codex/attachments

# config.toml + env + persona migration are owned ENTIRELY by the presync hook, NOT written here.
PRESYNC_HOOK="/usr/local/bin/runtime-codex-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-codex] sync config/env now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-codex] WARN: presync failed (config.json missing? non-fatal — retried on next switch/boot)"
else
  echo "[install-codex] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); config/env NOT synced"
fi

# MUST stay in sync with runtimes/codex/gateway_unit.go codexUnitContent (EnsureOnboarding
# self-heals the same unit on a hand-switched device).
echo "[install-codex] write systemd unit codex.service"
cat >/etc/systemd/system/codex.service <<UNIT
[Unit]
Description=Codex agent gateway (os-server codex-gatewayd driving Codex App Server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.codex/.env
WorkingDirectory=/root/.codex
ExecStart=/usr/local/bin/os-server codex-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-codex] enable + start codex.service"
systemctl enable --now codex.service
systemctl status codex.service --no-pager || true

echo "[install-codex] declare verify hook for switch-runtime (codex + os-server)"
mkdir -p /usr/local/lib/os-runtimes/codex
cat >/usr/local/lib/os-runtimes/codex/verify <<'VERIFY'
#!/usr/bin/env bash
command -v codex >/dev/null 2>&1 && [ -x /usr/local/bin/os-server ]
VERIFY
chmod +x /usr/local/lib/os-runtimes/codex/verify

echo "[install-codex] done — codex gatewayd installed + started (codex.service)."
echo "[install-codex] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
