#!/usr/bin/env bash
# runtimes/hermes/install.sh — installer for the Hermes agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/hermes/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to hermes. It
# is self-contained: nothing in the imager or os-server knows about hermes.
#
# This installer is self-sufficient: a direct `bash install.sh` fully configures
# AND starts Hermes — it no longer relies on switch-runtime to run the presync
# hook or to enable the unit afterwards. It still also drops the presync hook so
# later runtime switches re-sync the latest llm_* from config.json.
#
# What it does:
#   1. install the Hermes CLI, stop openclaw, then migrate skills from OpenClaw;
#   2. seed .env, then patch ONLY .model + .custom_providers in config.yaml
#      (yq, not a full-file overwrite), then run the presync hook once to pull
#      the device's real llm_model / llm_base_url / llm_api_key from config.json;
#   3. drop /usr/local/bin/runtime-hermes-presync for per-switch config sync
#      (since `hermes claw migrate` does NOT carry model config across);
#   4. install + start the gateway as a system service via
#      `hermes gateway install/start --system` (unit: hermes-gateway.service).
#
# Reference: hermes_setup.sh (~/Downloads) for the yq config patch + .env seeding.
#
# UNIT NAME: the gateway runs as hermes-gateway.service; we declare it in
#    /usr/local/lib/os-runtimes/hermes/service so switch_runtime.sh enables the
#    right unit.
#
# ⚠️ VERIFY ON DEVICE: `hermes gateway` must listen on 127.0.0.1:8642 to match
#    runtimes/hermes/constants.go BaseURL.
set -euo pipefail

# Tee all output (stdout+stderr) to a log file so the install can be followed
# live (`tail -f $HERMES_LOG`) and inspected after the fact. Console output is
# preserved. Override the path with HERMES_LOG=... before invoking if needed.
# NOTE: default lives under /root/.hermes (persistent rootfs), NOT /var/log —
# on these boards /var/log is a volatile zram mount (log2ram) that is wiped on
# reboot, which would lose the install log exactly when you need it.
HERMES_LOG="${HERMES_LOG:-/root/.hermes/install.log}"
mkdir -p "$(dirname "$HERMES_LOG")"
exec > >(tee -a "$HERMES_LOG") 2>&1
echo "[install-hermes] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $HERMES_LOG) ====="

HERMES_BIN="/usr/local/bin/hermes"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"
# config.yaml is owned by the presync hook (runtimes/hermes/presync.sh), not this
# installer — see the presync section below.

# API_SERVER_KEY MUST equal runtimes/hermes/constants.go APIKey — os-server sends
# it as the Bearer on every /v1/responses call. Mismatch ⇒ 401 on every turn.
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
# The upstream installer's `node-deps` stage runs `npm install` of browser-tool
# native modules (node-gyp prebuild/rebuild) that hangs indefinitely on this ARM
# board (CPU idle, no compiler children — it stalls, never returns), and a voice
# lamp never uses browser tools. So instead of the monolithic
# `curl | bash --skip-setup`, drive the installer STAGE BY STAGE and skip
# `node-deps`. These stages reproduce the monolithic runtime install minus
# node-deps and the interactive setup/gateway stages (we install the gateway
# ourselves below). See `bash <installer> --manifest` for the full stage list.
HERMES_INSTALLER="$(mktemp)"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$HERMES_INSTALLER"
for stage in prerequisites repository venv python-deps path config; do
  echo "[install-hermes] hermes installer stage: ${stage}"
  bash "$HERMES_INSTALLER" --stage "$stage" --non-interactive
done
rm -f "$HERMES_INSTALLER"
# Stamp the install method the monolithic flow writes at its end, so a later
# `hermes update` recognizes this as a git install (root layout default dir).
echo "git" >/usr/local/lib/hermes-agent/.install_method 2>/dev/null || true
if [ ! -x "$HERMES_BIN" ]; then
  echo "[install-hermes] ERROR: hermes not found at $HERMES_BIN after install" >&2
  command -v hermes || true
  exit 1
fi
"$HERMES_BIN" --version || true

echo "[install-hermes] stop openclaw before migrating (avoid racing its state)"
# Retry the stop up to 3 times, verifying openclaw actually went inactive between
# tries. After 3 attempts proceed regardless so a stuck openclaw never blocks the
# install — claw migrate is non-fatal anyway.
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

# OpenClaw skill import (`hermes claw migrate`) is owned by the presync hook now —
# it restores skills/openclaw-imports whenever that dir is empty (first install OR
# after a factory reset wiped it). openclaw was stopped above so the presync call
# below runs the import without racing OpenClaw's state. See presync.sh §0.

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
# AUTONOMOUS_API_KEY (LLM key) + model + base_url + TELEGRAM_ALLOWED_USERS are
# written by the presync hook from config.json on every switch, so the device's
# real values win.

# config.yaml model wiring (static provider STRUCTURE + dynamic llm_*/channel sync
# from config.json) is owned ENTIRELY by the presync hook, NOT patched here. The
# hook is materialized to /usr/local/bin/runtime-hermes-presync by os-server BEFORE
# this installer runs (see runtimes/hermes/presync.sh + system/device materialize).
# Owning it there — not in this installer — is what lets a plain os-server OTA refresh
# it: this installer only re-runs on a first install / failed verify, so a config fix
# baked in here would never reach an already-installed hermes. Running the hook now
# configures this fresh install; switch-runtime re-runs it before every later start,
# so the config self-heals (e.g. after a factory reset's `hermes setup --reset` wiped
# .model/.custom_providers).
PRESYNC_HOOK="/usr/local/bin/runtime-hermes-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-hermes] ensure config.yaml structure + sync llm_* now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-hermes] WARN: presync failed (config.json missing? non-fatal — switch-runtime retries on next switch)"
else
  echo "[install-hermes] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); Hermes model config NOT set"
fi

echo "[install-hermes] install + start hermes gateway as a system service"
# Auto-answer the installer's interactive [Y/n] prompts. `yes` outruns the prompt
# count and takes SIGPIPE/EPIPE the moment `hermes gateway install` exits (you see
# "yes: standard output: Broken pipe"). Under `pipefail` that broken pipe makes the
# pipeline non-zero even when the install SUCCEEDED, which `set -e` then turns into
# a failed install — and switch-runtime rolls the whole switch back to openclaw
# despite hermes-gateway being up. Drop pipefail for just this line so the pipeline
# status reflects `hermes gateway install` (the rightmost command); a genuine
# install failure is still non-zero and still aborts via `set -e`.
set +o pipefail
yes y | "$HERMES_BIN" gateway install --system --run-as-user root
set -o pipefail
# Keep optional gateway imports off the disk while HAL starts. Append to any
# upstream ExecStartPre entries; older OS binaries fail open via the '-' prefix.
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

# Declare our unit name (hermes-gateway, not the default <runtime>.service) so
# switch-runtime enables/disables the right unit. Best-effort: the dir exists
# when os-server materialized this installer; create it for standalone runs too.
echo "[install-hermes] declare unit name for switch-runtime (hermes-gateway)"
mkdir -p /usr/local/lib/os-runtimes/hermes
echo "hermes-gateway" >/usr/local/lib/os-runtimes/hermes/service

# Drop a verify hook so switch-runtime can distinguish a real install from an
# orphaned hermes-gateway.service whose hermes binary is gone/broken. When this
# fails, switch-runtime reinstalls instead of skipping. Keep it cheap + offline —
# just confirm the CLI is on PATH.
echo "[install-hermes] declare verify hook for switch-runtime (command -v hermes)"
cat >/usr/local/lib/os-runtimes/hermes/verify <<'VERIFY'
#!/usr/bin/env bash
command -v hermes >/dev/null 2>&1
VERIFY
chmod +x /usr/local/lib/os-runtimes/hermes/verify

echo "[install-hermes] done — hermes gateway installed + started as a system service (hermes-gateway.service)."
echo "[install-hermes] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
