#!/usr/bin/env bash
# Hermes backend installer, run by switch-runtime on first switch; config.yaml is owned by presync.
# The gateway must listen on 127.0.0.1:8642 (runtimes/hermes/constants.go BaseURL).
set -euo pipefail

# Log to persistent /root/.hermes: /var/log is volatile zram (log2ram) on these boards.
HERMES_LOG="${HERMES_LOG:-/root/.hermes/install.log}"
mkdir -p "$(dirname "$HERMES_LOG")"
exec > >(tee -a "$HERMES_LOG") 2>&1
echo "[install-hermes] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $HERMES_LOG) ====="

HERMES_BIN="/usr/local/bin/hermes"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"

# Must equal runtimes/hermes/constants.go APIKey (401 on every turn otherwise).
HERMES_API_SERVER_KEY="hermes-local-api-key"

echo "[install-hermes] prerequisites (jq, yq, curl)"
apt-get update || true
apt-get install -y jq curl || true
if ! command -v yq >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64)        YQ_BIN="yq_linux_amd64" ;;
    aarch64|arm64) YQ_BIN="yq_linux_arm64" ;;
    armv7l|armv6l) YQ_BIN="yq_linux_arm" ;;
    *) echo "[install-hermes] ERROR: unsupported arch $(uname -m) for yq"; exit 1 ;;
  esac
  curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.46.1/${YQ_BIN}" -o /usr/local/bin/yq
  chmod +x /usr/local/bin/yq
fi

echo "[install-hermes] install Hermes CLI (staged — skip node-deps browser tools)"
# Run the upstream installer stage by stage, skipping `node-deps`: its npm native builds hang on ARM.
HERMES_INSTALLER="$(mktemp)"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$HERMES_INSTALLER"
for stage in prerequisites repository venv python-deps path config; do
  echo "[install-hermes] hermes installer stage: ${stage}"
  bash "$HERMES_INSTALLER" --stage "$stage" --non-interactive
done
rm -f "$HERMES_INSTALLER"
# Mark as a git install so a later `hermes update` recognizes it.
echo "git" >/usr/local/lib/hermes-agent/.install_method 2>/dev/null || true
if [ ! -x "$HERMES_BIN" ]; then
  echo "[install-hermes] ERROR: hermes not found at $HERMES_BIN after install" >&2
  command -v hermes || true
  exit 1
fi
"$HERMES_BIN" --version || true

echo "[install-hermes] stop openclaw before migrating (avoid racing its state)"
for attempt in 1 2 3; do
  systemctl stop openclaw 2>/dev/null || true
  if ! systemctl is-active --quiet openclaw; then
    echo "[install-hermes] openclaw stopped (attempt ${attempt}/3)"
    break
  fi
  echo "[install-hermes] WARN: openclaw still active after attempt ${attempt}/3"
  [ "$attempt" -eq 3 ] && echo "[install-hermes] WARN: openclaw still active after 3 attempts — continuing anyway"
  sleep 1
done


mkdir -p "$HERMES_DIR"
touch "$ENV_FILE"

echo "[install-hermes] seed $ENV_FILE (upsert — other vars preserved)"
for k in API_SERVER_ENABLED API_SERVER_KEY API_SERVER_CORS_ORIGINS; do
  sed -i "/^${k}=/d" "$ENV_FILE"
done

[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
cat >>"$ENV_FILE" <<EOF
API_SERVER_ENABLED=true
API_SERVER_KEY=${HERMES_API_SERVER_KEY}
API_SERVER_CORS_ORIGINS=http://localhost:3000
EOF

# config.yaml wiring is owned by the presync hook (materialized by os-server) so an OTA can fix it.
PRESYNC_HOOK="/usr/local/bin/runtime-hermes-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-hermes] ensure config.yaml structure + sync llm_* now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-hermes] WARN: presync failed (config.json missing? non-fatal — switch-runtime retries on next switch)"
else
  echo "[install-hermes] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); Hermes model config NOT set"
fi

echo "[install-hermes] install + start hermes gateway as a system service"
# `yes` gets SIGPIPE once install exits; under pipefail that would fail a successful install.
set +o pipefail
yes y | "$HERMES_BIN" gateway install --system --run-as-user root
set -o pipefail
# Keep optional gateway imports off the disk while HAL starts; the '-' prefix fails open.
HARDWARE_STARTUP_DIR=/etc/systemd/system/hermes-gateway.service.d
HARDWARE_STARTUP_DROPIN="$HARDWARE_STARTUP_DIR/20-hardware-startup.conf"
mkdir -p "$HARDWARE_STARTUP_DIR"
HARDWARE_STARTUP_TMP=$(mktemp "$HARDWARE_STARTUP_DIR/.hardware-startup-XXXXXX")
printf '[Service]\nExecStartPre=-/usr/local/bin/os-server --wait-hal-ready\n' >"$HARDWARE_STARTUP_TMP"
if cmp -s "$HARDWARE_STARTUP_TMP" "$HARDWARE_STARTUP_DROPIN"; then
  rm -f "$HARDWARE_STARTUP_TMP"
else
  chmod 0644 "$HARDWARE_STARTUP_TMP"
  mv -f "$HARDWARE_STARTUP_TMP" "$HARDWARE_STARTUP_DROPIN"
  systemctl daemon-reload
fi
"$HERMES_BIN" gateway start --system
"$HERMES_BIN" gateway status --system || true

echo "[install-hermes] declare unit name for switch-runtime (hermes-gateway)"
mkdir -p /usr/local/lib/os-runtimes/hermes
echo "hermes-gateway" >/usr/local/lib/os-runtimes/hermes/service

# Verify hook: switch-runtime reinstalls when this fails (orphaned unit, missing binary).
echo "[install-hermes] declare verify hook for switch-runtime (command -v hermes)"
cat >/usr/local/lib/os-runtimes/hermes/verify <<'VERIFY'
#!/usr/bin/env bash
command -v hermes >/dev/null 2>&1
VERIFY
chmod +x /usr/local/lib/os-runtimes/hermes/verify

echo "[install-hermes] done — hermes gateway installed + started as a system service (hermes-gateway.service)."
echo "[install-hermes] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
