#!/bin/bash
# =============================================================================
# build-orangepi.sh — Golden Image Builder for OrangePi 4 Pro v2 (A733/sun60iw2)
# =============================================================================
#
# Source image: Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147.7z
# (vendor "user-built" image from orangepi-xunlong/orangepi-build, hosted on
# Google Drive folder 1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS — there is no public
# mirror; the dev team uploaded it themselves).
#
# Flow:
#   Phase 0  download .7z from Google Drive (cached in /input/)
#   Phase 1  extract .img, expand to OUT_IMG_SIZE, partprobe, resize2fs
#   Phase 2  chroot apt install + write systemd units + helper scripts + configs
#   Phase 3  chroot OTA bake — backend binaries + hal + web UI + buddy
#   Phase 4  install resize-once.service for first-boot SD-fill expand
#   Phase 5  unmount + compress → /output/golden-opi.img.xz
#
# Run via Makefile (Docker container, --privileged for losetup/mount).
# =============================================================================

set -euo pipefail

# ── config ───────────────────────────────────────────────────────────────────
PI_HOSTNAME="autonomous"
PI_TIMEZONE="America/New_York"
USERNAME="system"
PASSWORD="12345"
OUT_IMG_SIZE="${OUT_IMG_SIZE:-14G}"
# OTA metadata URL — per-deployment value, passed in by the Makefile (-e). No
# hardcoded default: fail fast if the caller did not provide one. Baked into the
# image's /root/config/bootstrap.json.
OTA_METADATA_URL="${OTA_METADATA_URL:?OTA_METADATA_URL is required — build via 'make build OTA_METADATA_URL=...'}"
OTA_SIGNING_PUBLIC_KEY="${OTA_SIGNING_PUBLIC_KEY:-}"
AP_BAND="${AP_BAND:-2.4}"
AP_CHANNEL="${AP_CHANNEL:-}"
COUNTRY_CODE="${COUNTRY_CODE:-US}"
# Match OTA metadata checked on 2026-09-11.
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.9.3}"
# Release v2026.9.7 reports Hermes 0.21.1; pin the image checkout only.
HERMES_VERSION="0.21.1"
HERMES_COMMIT="2237be355906fbe6065ce1815711eee52b2d646e"
# Device class this golden image is for — bakes robots/<type>/{DEVICE,SOUL}.md
# so one DEVICE_TYPE = one golden image. Forwarded by the Makefile via docker -e.
# REQUIRED, no default — a golden image must declare which device class it is.
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE is required — build via 'make build DEVICE_TYPE=...'}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"

# Per-image default agent runtime — bakes /root/config/f_r_default_agent, read
# by SeedAgentRuntimeFromGateway (system/device/runtime.go) with PRIORITY over
# ROBOT.md gateway.default, and — unlike gateway.default — it survives Factory
# Reset (not in factoryreset.go's deviceWipePaths). So an image whose
# DEFAULT_AGENT was set here re-seeds to the SAME default after an F_R, instead
# of falling back to the device-type-wide ROBOT.md value shared by every
# build. OPTIONAL — unset (the default) bakes nothing, and behavior is 100%
# unchanged: seeding falls through to ROBOT.md gateway.default exactly as
# before. Also gates SSH for intern-v2 — see the "enable services" stage below.
#
# intern-v2 ships in 3 physical case colors, each defaulting to one agent —
# blue=hermes, orange=openclaw, black=claudecode (Developer Edition). The case
# color itself is COSMETIC and carries no logic of its own (there used to be a
# separate CASE_COLOR var here — removed: color and SSH policy were two
# different knobs an operator had to remember to keep in sync, and a
# blue-cased build with the wrong CASE_COLOR would silently ship with the
# wrong SSH state). DEFAULT_AGENT alone now drives both the seeded runtime AND
# the SSH policy, so there is exactly one thing to set per case color and no
# way for the two to disagree.
DEFAULT_AGENT="${DEFAULT_AGENT:-}"
# Optional assembly within the device package; empty/standard keeps legacy defaults.
VARIANT="${VARIANT:-}"
if [[ -n "$VARIANT" && ! "$VARIANT" =~ ^[a-z][a-z0-9_-]{0,63}$ ]]; then
  echo "Invalid VARIANT: expected a lowercase name (1-64 characters)" >&2
  exit 1
fi

# Google Drive file ID for the bookworm server image. Override via env var when
# the dev team rotates the .7z (new Orange Pi release).
OPI_FILE_ID="${OPI_FILE_ID:-1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn}"
OPI_FILE_NAME="Orangepi4pro_1.0.6_debian_bookworm_server_linux5.15.147"

# Per-device pre-built base image. lamp and intern-v2 ship hardware-team-baked
# .img.xz in input/<device>/. Other device types fall back to Google Drive stock.
case "${DEVICE_TYPE}" in
  lamp)
    DEVICE_BASE_IMG="${DEVICE_BASE_IMG:-/input/lamp/golden-opi-dev.img.xz}"
    ;;
  intern-v2)
    DEVICE_BASE_IMG="${DEVICE_BASE_IMG:-/input/intern-v2/golden-opi-dev.img.xz}"
    ;;
  *)
    DEVICE_BASE_IMG=""
    ;;
esac

MNT="/mnt/opi"
SRC_7Z="/input/orangepi.7z"
SRC_IMG="/work/base-${DEVICE_TYPE}.img"
OUT_DIR="/output/${DEVICE_TYPE}"
OUT_IMG="${OUT_DIR}/golden-opi.img"

LOOP_DEV=""
PART_LOOP=""

cleanup() {
  set +e
  mountpoint -q "${MNT}/dev"  && umount -lf "${MNT}/dev"
  mountpoint -q "${MNT}/sys"  && umount -lf "${MNT}/sys"
  mountpoint -q "${MNT}/proc" && umount -lf "${MNT}/proc"
  mountpoint -q "${MNT}"      && umount -lf "${MNT}"
  [ -n "${PART_LOOP}" ] && losetup -d "${PART_LOOP}" 2>/dev/null
  [ -n "${LOOP_DEV}" ]  && losetup -d "${LOOP_DEV}"  2>/dev/null
}
trap cleanup EXIT

log() { echo "==> $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }

if [ -n "${DEFAULT_AGENT}" ]; then
  case "${DEFAULT_AGENT}" in
    openclaw|hermes|picoclaw|codex|claudecode|opencode) ;;
    *) err "invalid DEFAULT_AGENT=${DEFAULT_AGENT} — must be one of: openclaw hermes picoclaw codex claudecode opencode" ;;
  esac
fi

# CASE_COLOR was removed (see the DEFAULT_AGENT comment above) — fail fast
# instead of silently ignoring it. Without this guard, an old invocation like
# `CASE_COLOR=blue` with no DEFAULT_AGENT would previously close SSH; today it
# would silently do nothing and SSH would ship OPEN instead — the opposite of
# what the caller asked for, with no error to catch it.
if [ -n "${CASE_COLOR:-}" ]; then
  err "CASE_COLOR is removed — set DEFAULT_AGENT=hermes|openclaw|claudecode instead (SSH now follows DEFAULT_AGENT directly)"
fi

retry() {
  local cmd="$1" max="${2:-5}" delay="${3:-3}" n=0
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n + 1))
    log "retry $n/$max in ${delay}s: $cmd"
    sleep "$delay"
  done
  return 1
}

# ── prereq check ─────────────────────────────────────────────────────────────
for bin in 7z losetup parted resize2fs e2fsck mkfs.ext4 qemu-aarch64-static gdown xz growpart; do
  command -v "$bin" >/dev/null || err "missing tool: $bin (check Dockerfile)"
done
mkdir -p /input /output "${OUT_DIR}" /work "${MNT}"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — Source base image: per-device pre-built or Google Drive stock
# ─────────────────────────────────────────────────────────────────────────────
if [ -n "${DEVICE_BASE_IMG:-}" ]; then
  log "Base image for ${DEVICE_TYPE}: ${DEVICE_BASE_IMG}"
  [ -f "${DEVICE_BASE_IMG}" ] || err "Base image not found: ${DEVICE_BASE_IMG} — place it at imager/input/${DEVICE_TYPE}/"
  log "Decompressing ${DEVICE_BASE_IMG} → ${SRC_IMG}…"
  xz -dkc --threads=0 "${DEVICE_BASE_IMG}" > "${SRC_IMG}"
else
  if [ ! -f "${SRC_7Z}" ]; then
    log "Downloading ${OPI_FILE_NAME}.7z (~734 MB) from Google Drive…"
    if ! retry "gdown 'https://drive.google.com/uc?id=${OPI_FILE_ID}' -O '${SRC_7Z}'" 3 5; then
      rm -f "${SRC_7Z}"
      cat >&2 <<MSG
==============================================================================
gdown failed. Google Drive rate-limits popular files (~"Too many users have
viewed or downloaded this file recently"). The browser bypasses this because
it uses an authenticated session.

MANUAL FIX (one-time per machine):

  1. Open in your browser (authenticated to your Google account):
     https://drive.google.com/uc?id=${OPI_FILE_ID}
     or browse the folder:
     https://drive.google.com/drive/folders/1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS

  2. Click "Download anyway" past the "no virus scan" warning.

  3. Place the downloaded file at:
     $(pwd)/input/orangepi.7z
     (or imager/input/orangepi.7z on the host — the Docker mount sees it there)

  4. Re-run: make build

The .7z file is cached after this — gdown isn't called on subsequent builds.
==============================================================================
MSG
      exit 1
    fi
  else
    log "Source .7z cached at ${SRC_7Z}"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Extract (stock only), expand to OUT_IMG_SIZE, partprobe, resize2fs
# ─────────────────────────────────────────────────────────────────────────────
if [ -z "${DEVICE_BASE_IMG:-}" ]; then
  log "Extracting ${SRC_7Z}…"
  rm -f /work/*.img /work/*.sha
  7z x -y -o/work "${SRC_7Z}" >/dev/null
  EXTRACTED_IMG=$(find /work -maxdepth 2 -name '*.img' -type f | head -1)
  [ -n "${EXTRACTED_IMG}" ] || err "no .img found inside .7z"
  if [ "${EXTRACTED_IMG}" != "${SRC_IMG}" ]; then
    mv -f "${EXTRACTED_IMG}" "${SRC_IMG}"
  fi
fi
log "Source image: ${SRC_IMG} ($(du -h "${SRC_IMG}" | cut -f1))"

log "Copying source → ${OUT_IMG} and expanding to ${OUT_IMG_SIZE}…"
cp -f "${SRC_IMG}" "${OUT_IMG}"
truncate -s "${OUT_IMG_SIZE}" "${OUT_IMG}"

LOOP_DEV=$(losetup --find --show "${OUT_IMG}")
sleep 1

log "Resizing partition 1 to fill image…"
growpart "${LOOP_DEV}" 1 || parted -s "${LOOP_DEV}" resizepart 1 100%

# Docker Desktop on Mac (and minimal containers in general) ship without udev
# so /dev/loopXp1 device nodes don't appear after partition resize. Read the
# new partition byte offset + size via parted, then attach a second loop
# device pointing directly at the partition. Bypasses kernel partition device
# node creation entirely.
PART_START=$(parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
PART_SIZE=$( parted -s "${LOOP_DEV}" unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
log "Partition 1: start=${PART_START} size=${PART_SIZE}"
PART_LOOP=$(losetup --find --show --offset "${PART_START}" --sizelimit "${PART_SIZE}" "${OUT_IMG}")
PART="${PART_LOOP}"
[ -b "${PART}" ] || err "partition loop device ${PART} did not appear"

log "Filesystem check + resize…"
e2fsck -fy "${PART}" || true
resize2fs "${PART}"

log "Mounting at ${MNT}…"
mount "${PART}" "${MNT}"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Chroot: apt install, Node, OpenClaw, uv, systemd units, configs
# ─────────────────────────────────────────────────────────────────────────────
log "Setting up chroot…"
cp /usr/bin/qemu-aarch64-static "${MNT}/usr/bin/qemu-aarch64-static"
mount --bind /proc "${MNT}/proc"
mount --bind /sys  "${MNT}/sys"
mount --bind /dev  "${MNT}/dev"
cp -f "${MNT}/etc/resolv.conf" "${MNT}/etc/resolv.conf.bak" 2>/dev/null || true
cp -f /etc/resolv.conf "${MNT}/etc/resolv.conf"

# Suppress debconf interactive prompts during apt installs.
chroot "${MNT}" debconf-set-selections <<'DBCONF' || true
debconf debconf/frontend select Noninteractive
keyboard-configuration keyboard-configuration/layoutcode string us
DBCONF
cat > "${MNT}/etc/apt/apt.conf.d/99-${DEVICE_TYPE}-silent" <<'APT'
Dpkg::Use-Pty "false";
APT

# Pre-seed env passed into chroot heredoc — unquoted heredoc so ${VAR} expands.
chroot "${MNT}" /bin/bash <<CHROOT_STAGES
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export OTA_METADATA_URL="${OTA_METADATA_URL}"
export AP_BAND="${AP_BAND}"
export AP_CHANNEL="${AP_CHANNEL}"
export COUNTRY_CODE="${COUNTRY_CODE}"
export OPENCLAW_VERSION="${OPENCLAW_VERSION}"
export HERMES_VERSION="${HERMES_VERSION}"
export HERMES_COMMIT="${HERMES_COMMIT}"
export DEVICE_TYPE="${DEVICE_TYPE}"
export DEVICES_DIR="${DEVICES_DIR}"
export DEFAULT_AGENT="${DEFAULT_AGENT}"

retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-3}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n + 1))
    echo "retry \$n/\$max in \${delay}s: \$cmd"
    sleep "\$delay"
  done
  return 1
}

# ── apt: install OS runtime deps (matches setup.sh + production OPi list) ──
echo "[stage] apt update + install"
apt-get update -qq
apt-get install -y \\
  btrfs-progs \\
  hostapd dnsmasq nginx \\
  curl jq unzip openssl ca-certificates \\
  wpasupplicant dhcpcd5 \\
  iproute2 iptables iw rfkill \\
  cloud-guest-utils \\
  wireless-tools net-tools \\
  systemd-sysv \\
  xvfb xauth chromium chromium-sandbox git \\
  openresolv \\
  fake-hwclock \\
  libportaudio2 portaudio19-dev pulseaudio pulseaudio-utils pulseaudio-module-bluetooth ffmpeg \\
  alsa-utils libasound2-dev \\
  libopenblas0 libgomp1 liblapack3 \\
  libgpiod2 \\
  python3-dev python3-spidev \\
  libsm6 libxext6 libgl1 \\
  libjpeg-dev zlib1g-dev libfreetype6-dev libopenjp2-7-dev libtiff-dev \\
  avahi-daemon avahi-utils libnss-mdns \\
  bluez openssh-server

# Purge things that would conflict with our AP/STA flow on first boot.
apt-get purge -y --auto-remove network-manager network-manager-gnome 2>/dev/null || true
apt-get clean

# Disable IPv6 — RPi 5 STA-drop workaround; harmless on OrangePi.
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf <<'SYSCTL'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
SYSCTL

# resolvconf fallback DNS — ensures /etc/resolv.conf is never empty in AP mode.
if [ -f /etc/resolvconf.conf ]; then
  grep -q '^name_servers=' /etc/resolvconf.conf || echo 'name_servers="1.1.1.1 8.8.8.8"' >> /etc/resolvconf.conf
else
  echo 'name_servers="1.1.1.1 8.8.8.8"' > /etc/resolvconf.conf
fi

# ── Node.js 26 + OpenClaw CLI (npm global) ───────────────────────────────────
# Refresh even reused base images to the newest package on the Current branch.
echo "[stage] Node.js 26 + OpenClaw \${OPENCLAW_VERSION}"
NODE_SETUP=\$(mktemp)
curl -fsSL https://deb.nodesource.com/setup_26.x -o "\$NODE_SETUP"
bash "\$NODE_SETUP"
rm -f "\$NODE_SETUP"
apt-get install -y nodejs
hash -r
# Latest upstream at the OTA version check was 26.8.2. Fail if apt/PATH left an old Node.
node -e 'const [major, minor, patch] = process.versions.node.split(".").map(Number); if (major !== 26 || minor < 8 || (minor === 8 && patch < 2)) process.exit(1)'
node --version
retry "npm install -g openclaw@\${OPENCLAW_VERSION} --omit=optional" 5
openclaw --version || true
openclaw --version 2>/dev/null | tr -d '[:space:]' > /tmp/baked-openclaw-version || echo "unknown" > /tmp/baked-openclaw-version

# OpenClaw state dir. MUST be /root/.openclaw (with dot) — see openclaw memory
# note: any /root/openclaw mismatch causes WS close 1008 / token_mismatch.
mkdir -p \\
  /root/.openclaw \\
  /root/.openclaw/agents/main/agent \\
  /root/.openclaw/workspace \\
  /root/.openclaw/.cache \\
  /root/.openclaw/.config \\
  /root/.openclaw/.local/share

# Onboard creates default config files. --skip-health since chroot has no
# systemd/network; gateway completes onboarding on first device boot.
HOME=/root \\
OPENCLAW_HOME=/root/.openclaw \\
OPENCLAW_STATE_DIR=/root/.openclaw \\
XDG_CACHE_HOME=/root/.openclaw/.cache \\
XDG_CONFIG_HOME=/root/.openclaw/.config \\
XDG_DATA_HOME=/root/.openclaw/.local/share \\
timeout 60 openclaw onboard --non-interactive --accept-risk --skip-health || \\
  echo "WARN: openclaw onboard timed out (will retry on device first boot)"

# Install external plugins baked into the golden image.
openclaw plugins install @openclaw/discord@${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: discord plugin install failed (non-fatal)"
openclaw plugins install @openclaw/slack@${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: slack plugin install failed (non-fatal)"

curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.46.1/yq_linux_arm64" -o /usr/local/bin/yq
chmod +x /usr/local/bin/yq

# ── Hermes CLI binary pre-bake ────────────────────────────────────────────────
# Run the same installer stages as install.sh, minus gateway/config/migrate.
# Pin the installer and checkout, including on reused base images. A later
# hermes update can leave detached HEAD and follow upstream as usual.
# Everything else (service unit, presync, claw migrate)
# is handled by install.sh at actual switch time via Go switch-runtime.
echo "[stage] hermes CLI binary pre-bake"
HERMES_INSTALLER=\$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/\${HERMES_COMMIT}/scripts/install.sh" -o "\$HERMES_INSTALLER"
for stage in prerequisites repository venv python-deps path config; do
  echo "[hermes-prebake] stage: \${stage}"
  bash "\$HERMES_INSTALLER" --stage "\$stage" --non-interactive --commit "\$HERMES_COMMIT" --force-commit
done
rm -f "\$HERMES_INSTALLER"
echo "git" >/usr/local/lib/hermes-agent/.install_method 2>/dev/null || true
HERMES_ACTUAL_VERSION=\$(hermes --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+([-.+_][0-9A-Za-z.-]+)?' | head -1)
if [ "\$HERMES_ACTUAL_VERSION" != "\$HERMES_VERSION" ]; then
  echo "ERROR: Hermes reports \$HERMES_ACTUAL_VERSION, expected \$HERMES_VERSION"
  exit 1
fi
printf '%s\n' "\$HERMES_ACTUAL_VERSION" > /tmp/baked-hermes-version

# ── Hermes gateway unit pre-bake (A — created, left DISABLED) ────────────────
# Pre-baking the binary above is not enough: IsReady()/device setup wait on the
# hermes-gateway HTTP /health, which needs the hermes-gateway.service unit to
# exist. switch-runtime/install.sh creates it on the first switch to hermes — but
# a hand-edited config.json agent_runtime=hermes flip never runs that path, so the
# unit is absent, the gateway never starts, WaitForAgentReady times out,
# SetUpCompleted stays false, the device falls back to AP mode, and the symptom
# reads as "WiFi won't connect". Create the unit here so it is ready to start.
# We do NOT enable it at boot: openclaw is the default active runtime and enabling
# both would run two agents. os-server's EnsureOnboarding (B) and switch-runtime
# enable+start it when hermes actually becomes active. Best-effort: chroot has no
# running systemd, so if the CLI cannot write the unit here, EnsureOnboarding (B)
# installs it at runtime instead — that is why we ship both.
echo "[stage] hermes-gateway.service unit pre-bake (created, left disabled)"
if command -v hermes >/dev/null 2>&1; then
  # Seed .env with API server keys before gateway install — mirrors install.sh
  # lines 117-127. Without this the gateway starts with API_SERVER_ENABLED unset
  # and os-server's Bearer auth fails (401 on every turn).
  HERMES_DIR="/root/.hermes"
  ENV_FILE="\$HERMES_DIR/.env"
  HERMES_API_SERVER_KEY="hermes-local-api-key"
  mkdir -p "\$HERMES_DIR"
  touch "\$ENV_FILE"
  for k in API_SERVER_ENABLED API_SERVER_KEY API_SERVER_CORS_ORIGINS; do
    sed -i "/^\${k}=/d" "\$ENV_FILE"
  done
  [ -s "\$ENV_FILE" ] && [ -n "\$(tail -c1 "\$ENV_FILE")" ] && printf '\n' >>"\$ENV_FILE"
  printf '%s\n' \
    "API_SERVER_ENABLED=true" \
    "API_SERVER_KEY=\$HERMES_API_SERVER_KEY" \
    "API_SERVER_CORS_ORIGINS=http://localhost:3000" >>"\$ENV_FILE"
  echo "[stage] hermes .env pre-seeded (API_SERVER_ENABLED + API_SERVER_KEY + CORS)"
  set +o pipefail
  yes y | hermes gateway install --system --run-as-user root \
    || echo "WARN: hermes gateway unit write returned non-zero (chroot has no systemd; os-server EnsureOnboarding installs it at runtime)"
  set -o pipefail
  systemctl disable hermes-gateway 2>/dev/null || true
  if systemctl cat hermes-gateway >/dev/null 2>&1 || [ -f /etc/systemd/system/hermes-gateway.service ]; then
    echo "[stage] hermes-gateway unit present — declaring for switch-runtime"
    mkdir -p /usr/local/lib/os-runtimes/hermes
    echo "hermes-gateway" >/usr/local/lib/os-runtimes/hermes/service
    cat >/usr/local/lib/os-runtimes/hermes/verify <<'VERIFY'
#!/usr/bin/env bash
command -v hermes >/dev/null 2>&1
VERIFY
    chmod +x /usr/local/lib/os-runtimes/hermes/verify
  else
    echo "WARN: hermes-gateway unit not created in chroot — switch-runtime / EnsureOnboarding will install on first hermes activation"
  fi
fi

# ── Codex + Claude Code + PicoClaw + OpenCode CLI binary pre-bake (lamp + intern-v2) ───
# Same fast-path trick as the Hermes binary pre-bake above: bake ONLY the raw
# CLI binaries here — no systemd unit, no presync/onboard, no enable/start.
# Those stay owned entirely by each backend's own install.sh (runtimes/codex,
# runtimes/claudecode, runtimes/picoclaw, runtimes/opencode — embedded in
# os-server, fetched by switch-runtime on the first real switch to that
# runtime); each detects the binary already present and skips its own
# download, same as hermes above. Versions are pinned here just like
# CODEX_VERSION/PICO_VERSION/OPENCODE_VERSION in their respective install.sh —
# bump both places together when upgrading. Gated to lamp + intern-v2 — the
# two device types whose "Select frameworks" web UI actually offers these as
# switchable runtimes (see the Lamp screenshot in the PR — the picker is
# per-device, not intern-v2-only as first assumed). Other future DEVICE_TYPEs
# stay unbaked until their own UI exposes the picker.
#
# Checklist — "Select frameworks" web UI tiles vs. what's baked/available here:
#   [x] OpenClaw  — baked above (npm install -g openclaw), always (all devices)
#   [x] Hermes    — baked above (git-clone + uv-sync fast-path), always
#   [x] Claude Code — baked here, lamp + intern-v2
#   [x] Codex       — baked here, lamp + intern-v2
#   [x] PicoClaw    — baked here, lamp + intern-v2 (backend exists —
#                      runtimes/picoclaw + AgentGateway registered — UI badge
#                      is "coming soon" only pending product flip, not a
#                      missing backend)
#   [x] OpenCode    — baked here, lamp + intern-v2 (backend now exists —
#                      runtimes/opencode + AgentGateway registered, same as
#                      PicoClaw's situation above; previously a placeholder
#                      with no server-side code, now shipped)
if [ "\${DEVICE_TYPE}" = "intern-v2" ] || [ "\${DEVICE_TYPE}" = "lamp" ]; then
  echo "[stage] codex CLI binary pre-bake (\${DEVICE_TYPE})"
  CODEX_VERSION="\${CODEX_VERSION:-rust-v0.142.5}"
  CODEX_ASSET="codex-aarch64-unknown-linux-musl.tar.gz"
  CODEX_TMP=\$(mktemp -d)
  retry "curl -fsSL 'https://github.com/openai/codex/releases/download/\${CODEX_VERSION}/\${CODEX_ASSET}' -o '\$CODEX_TMP/\$CODEX_ASSET'" 5
  tar -xzf "\$CODEX_TMP/\$CODEX_ASSET" -C "\$CODEX_TMP"
  install -m 0755 "\$CODEX_TMP/\${CODEX_ASSET%.tar.gz}" /usr/local/bin/codex
  rm -rf "\$CODEX_TMP"
  codex --version || true
  codex --version 2>/dev/null | tr -d '[:space:]' > /tmp/baked-codex-version || echo "unknown" > /tmp/baked-codex-version

  echo "[stage] Claude Code CLI binary pre-bake (\${DEVICE_TYPE})"
  retry "curl -fsSL https://claude.ai/install.sh | bash" 3 10
  [ -x /root/.local/bin/claude ] && ln -sf /root/.local/bin/claude /usr/local/bin/claude
  claude --version || true
  claude --version 2>/dev/null | tr -d '[:space:]' > /tmp/baked-claudecode-version || echo "unknown" > /tmp/baked-claudecode-version

  echo "[stage] picoclaw CLI binary pre-bake (\${DEVICE_TYPE})"
  PICO_VERSION="\${PICO_VERSION:-v0.3.1-fixvision}"
  PICO_ASSET="picoclaw-linux-arm64"
  PICO_TMP=\$(mktemp)
  retry "curl -fsSL 'https://github.com/autonomous-ai/picoclaw/releases/download/\${PICO_VERSION}/\${PICO_ASSET}' -o '\$PICO_TMP'" 5
  install -m 0755 "\$PICO_TMP" /usr/local/bin/picoclaw
  rm -f "\$PICO_TMP"
  # picoclaw has no --version flag (errors "unknown flag") — version is a
  # subcommand that also prints an ANSI banner, so extract just the
  # "picoclaw <version>" token instead of capturing the whole thing.
  picoclaw --no-color version || true
  picoclaw --no-color version 2>/dev/null | sed -n 's/.*picoclaw \([^ ]*\).*/\1/p' | head -1 > /tmp/baked-picoclaw-version
  [ -s /tmp/baked-picoclaw-version ] || echo "unknown" > /tmp/baked-picoclaw-version

  echo "[stage] opencode CLI binary pre-bake (\${DEVICE_TYPE})"
  # Mirrors runtimes/opencode/install.sh's own install step exactly (same
  # pinned version, same official installer, same forced install dir) — the
  # goal is switch-runtime's install.sh detecting this binary already present
  # at the expected version and skipping its own download, not a parallel
  # install mechanism. OPENCODE_INSTALL_DIR must prefix \`bash\` (the process
  # running the installer), not \`curl\`, in the \`curl … | bash\` pipeline —
  # env vars only bind to the command they prefix.
  OPENCODE_VERSION="\${OPENCODE_VERSION:-1.18.4}"
  OPENCODE_BIN=/usr/local/bin/opencode
  retry "curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version '\${OPENCODE_VERSION#v}'" 3 10
  # Belt-and-suspenders, same as install.sh: the official installer has a
  # history of ignoring OPENCODE_INSTALL_DIR and dropping the binary at its
  # own default (~/.opencode/bin) while only patching PATH into ~/.bashrc —
  # which a non-interactive/non-login shell like this one never sources. Skip
  # only if the installer actually respected OPENCODE_INSTALL_DIR this time.
  if [ ! -x "\$OPENCODE_BIN" ]; then
    echo "[stage] \$OPENCODE_BIN missing — locating installer output"
    SRC="\$(command -v opencode 2>/dev/null || true)"
    [ -x "\$SRC" ] || SRC="/root/.opencode/bin/opencode"
    if [ -x "\$SRC" ]; then
      install -m 0755 "\$SRC" "\$OPENCODE_BIN"
      echo "[stage] copied \$SRC → \$OPENCODE_BIN"
    fi
  fi
  "\$OPENCODE_BIN" --version || true
  "\$OPENCODE_BIN" --version 2>/dev/null | tr -d '[:space:]' > /tmp/baked-opencode-version
  [ -s /tmp/baked-opencode-version ] || echo "unknown" > /tmp/baked-opencode-version
else
  echo "unbaked" > /tmp/baked-codex-version
  echo "unbaked" > /tmp/baked-claudecode-version
  echo "unbaked" > /tmp/baked-picoclaw-version
  echo "unbaked" > /tmp/baked-opencode-version
fi

# ── uv (Python pkg mgr for HAL) ───────────────────────────────────────────
echo "[stage] uv"
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="/root/.local/bin:\$PATH"
mkdir -p /opt/hal

# ── systemd units ────────────────────────────────────────────────────────────
echo "[stage] systemd units"

# Unquoted heredoc so \${DEVICE_TYPE}/\${DEVICES_DIR} expand from the chroot env
# (exported above). The unit has no other shell-expandable tokens, so leaving it
# unquoted is safe.
cat > /etc/systemd/system/os-server.service <<UNIT
[Unit]
Description=Autonomous OS Server
After=network-online.target

[Service]
User=root
WorkingDirectory=/root
Environment=DEVICE_TYPE=\${DEVICE_TYPE}
Environment=DEVICES_DIR=\${DEVICES_DIR}
ExecStart=/usr/local/bin/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bootstrap.service <<'UNIT'
[Unit]
Description=Bootstrap Backend
After=network-online.target

[Service]
User=root
ExecStart=/usr/local/bin/bootstrap-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bootstrap

[Install]
WantedBy=multi-user.target
UNIT

# Seed the bootstrap worker config so the OTA metadata URL comes from
# /root/config/bootstrap.json at runtime (single source of truth). The bootstrap
# binary has no compiled-in default and waits until this file provides
# metadata_url — baked here from the build-time OTA_METADATA_URL.
mkdir -p /root/config
cat > /root/config/bootstrap.json <<BSJSON
{
  "httpPort": 8080,
  "metadata_url": "${OTA_METADATA_URL}",
  "signing_public_key": "${OTA_SIGNING_PUBLIC_KEY}",
  "poll_interval": "5m",
  "state_file": "/root/bootstrap/state.json"
}
BSJSON

# Per-image default agent runtime (see DEFAULT_AGENT at the top of this
# script). Lives next to bootstrap.json — same directory, same "survives
# Factory Reset" property (neither is in factoryreset.go's deviceWipePaths).
# Read by SeedAgentRuntimeFromGateway (system/device/runtime.go) with priority
# over ROBOT.md gateway.default. Unset DEFAULT_AGENT (most builds) → no file
# written → seeding behavior is unchanged from before this feature.
if [ -n "\${DEFAULT_AGENT}" ]; then
  echo "\${DEFAULT_AGENT}" > /root/config/f_r_default_agent
  echo "[stage] f_r_default_agent baked: \${DEFAULT_AGENT}"
fi

cat > /etc/systemd/system/hal.service <<'UNIT'
[Unit]
Description=HAL Hardware Runtime
After=network.target

[Service]
EnvironmentFile=/opt/hal/.env
Type=simple
User=root
WorkingDirectory=/opt/hal
Environment="PYTHONPATH=/opt"
# Anonymous PulseAudio socket (see the default.pa drop-in below) so root-owned
# hal can reach the desktop user's PulseAudio for Bluetooth headset routing.
Environment="PULSE_SERVER=unix:/tmp/pulse-anon-${DEVICE_TYPE}"
# --timeout-graceful-shutdown: without it uvicorn waits forever for open
# connections (an SSE/MJPEG stream holds SIGTERM until systemd's 90s SIGKILL).
ExecStart=/opt/hal/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
TimeoutStopSec=30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
UNIT

# OpenClaw service — env block matches production OPi exactly.
CHROME_PATH=\$(command -v chromium 2>/dev/null || echo /usr/bin/chromium)
OPENCLAW_BIN=\$(command -v openclaw)
cat > /etc/systemd/system/openclaw.service <<UNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/.openclaw
Environment="OPENCLAW_HOME=/root/.openclaw"
Environment="OPENCLAW_STATE_DIR=/root/.openclaw"
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=/root/.openclaw/.cache"
Environment="XDG_CONFIG_HOME=/root/.openclaw/.config"
Environment="XDG_DATA_HOME=/root/.openclaw/.local/share"
Environment="PUPPETEER_EXECUTABLE_PATH=\$CHROME_PATH"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
Environment="CHROME_BIN=\$CHROME_PATH"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x800x24" \$OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# wpa_supplicant@wlan0 override → use per-interface config, not global.
mkdir -p /etc/systemd/system/wpa_supplicant@wlan0.service.d
cat > /etc/systemd/system/wpa_supplicant@wlan0.service.d/override.conf <<'OVR'
[Service]
ExecStart=
ExecStart=/sbin/wpa_supplicant -c /etc/wpa_supplicant/wpa_supplicant-wlan0.conf -i wlan0 -D nl80211,wext
Restart=on-failure
RestartSec=5
OVR

# ── helper scripts (verbatim from production OPi @ 100.111.149.69) ───────────
echo "[stage] helper scripts"

cat > /usr/local/bin/device-ap-mode <<'EOFSCRIPT'
#!/bin/bash
set -e
echo "Switching to AP mode..."
for cmd in ip iw systemctl hostapd dnsmasq rfkill; do
  command -v "\$cmd" >/dev/null 2>&1 || { echo "Missing required command: \$cmd"; exit 1; }
done
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true
systemctl stop wpa_supplicant@wlan0 2>/dev/null || true
systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
systemctl mask wpa_supplicant@wlan0 2>/dev/null || true
killall wpa_supplicant 2>/dev/null || true
# AP mode owns wlan0 — and ONLY wlan0. Do not stop/disable dhcpcd: this image
# purges NetworkManager at build time, so dhcpcd is the DHCP client for EVERY
# interface, and disabling it also kills the wired link. A device in AP mode
# (fresh out of the box, after a factory reset, or after "change WiFi") would
# then have no ethernet at all, and because the disable persists, it stays dead
# across reboots until device-sta-mode runs — i.e. until someone supplies WiFi
# credentials, which is enghaixactly what a wired user is trying to avoid.
# Instead tell dhcpcd to ignore wlan0 and keep serving eth0/end0; wlan0's AP
# address is assigned by hand further below.
touch /etc/dhcpcd.conf
if ! grep -q '^denyinterfaces wlan0\$' /etc/dhcpcd.conf; then
  # Prepend, never append: in dhcpcd.conf every option after an "interface X"
  # line belongs to that interface's block, and the base image's file may well
  # end inside one — appending there would scope this global option to a single
  # interface and silently do nothing.
  if [ -s /etc/dhcpcd.conf ]; then
    sed -i '1i denyinterfaces wlan0' /etc/dhcpcd.conf
  else
    echo 'denyinterfaces wlan0' > /etc/dhcpcd.conf
  fi
fi
systemctl enable dhcpcd 2>/dev/null || true
systemctl restart dhcpcd 2>/dev/null || true
systemctl stop NetworkManager systemd-networkd 2>/dev/null || true
rm -f /var/lib/dhcpcd5/dhcpcd-wlan0 2>/dev/null || true
rm -f /var/lib/dhcpcd/dhcpcd-wlan0 2>/dev/null || true

# SSID suffix from hardware serial (Pi family) or eth MAC (OPi family).
SERIAL=\$(tr -d '\0' </proc/device-tree/serial-number 2>/dev/null || true)
if [ -z "\$SERIAL" ]; then
  SERIAL=\$(awk '/^Serial/ {print \$3}' /proc/cpuinfo 2>/dev/null || true)
fi
if [ -z "\$SERIAL" ]; then
  for iface in eth0 end0; do
    mac=\$(cat "/sys/class/net/\$iface/address" 2>/dev/null | tr -d ':' || true)
    if [ -n "\$mac" ] && [ "\$mac" != "000000000000" ]; then
      SERIAL=\$mac
      break
    fi
  done
fi
SUFFIX=\${SERIAL: -4}
SUFFIX_LC=\$(echo "\$SUFFIX" | tr '[:upper:]' '[:lower:]')
# Network identity is device-type-driven: <device_type>-<suffix>, lowercase.
# DEVICE_TYPE is baked here at image-build time (one DEVICE_TYPE = one golden
# image); the suffix resolves at first boot from the hardware serial / eth MAC.
AP_SSID="${DEVICE_TYPE}-\${SUFFIX_LC}"
[ -f /etc/hostapd/hostapd.conf ] && sed -i "s/^ssid=.*/ssid=\${AP_SSID}/" /etc/hostapd/hostapd.conf

# mDNS <device_type>-<suffix>.local so the setup wizard's AP→.local handoff works.
DEVICE_HOSTNAME="${DEVICE_TYPE}-\${SUFFIX_LC}"
hostnamectl set-hostname "\$DEVICE_HOSTNAME" 2>/dev/null || hostname "\$DEVICE_HOSTNAME" || true
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1 \$DEVICE_HOSTNAME/" /etc/hosts
else
  echo "127.0.1.1 \$DEVICE_HOSTNAME" >> /etc/hosts
fi
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true

REG=\$(grep "^country_code=" /etc/hostapd/hostapd.conf 2>/dev/null | cut -d= -f2)
[ -z "\$REG" ] && REG=US
iw reg set "\$REG" 2>/dev/null || true

ip link set wlan0 down; sleep 1
iw dev wlan0 set type __ap
sleep 1
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
ip addr add 192.168.100.1/24 dev wlan0

command -v resolvconf >/dev/null 2>&1 && resolvconf -d wlan0.dhcp 2>/dev/null || true

grep -q '^address=/#/' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || echo 'address=/#/192.168.100.1' >> /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf

systemctl unmask hostapd dnsmasq 2>/dev/null || true
systemctl enable hostapd dnsmasq

systemctl restart nginx 2>/dev/null || true
systemctl restart dnsmasq

systemctl restart hostapd; sleep 2
if ! systemctl is-active --quiet hostapd; then
  echo "hostapd failed. Retrying..."
  systemctl restart hostapd; sleep 2
fi
if ! systemctl is-active --quiet hostapd; then
  echo "ERROR: hostapd still not running"
  journalctl -u hostapd -n 50 --no-pager || true
  exit 1
fi
echo "AP MODE ENABLED  SSID=\$AP_SSID  IP=192.168.100.1"
EOFSCRIPT
chmod +x /usr/local/bin/device-ap-mode

cat > /usr/local/bin/device-sta-mode <<'EOFSCRIPT'
#!/bin/bash
set -e
echo "Switching to STA mode..."
for cmd in ip iw systemctl rfkill; do
  command -v "\$cmd" >/dev/null 2>&1 || { echo "Missing required command: \$cmd"; exit 1; }
done
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true
systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true
killall hostapd 2>/dev/null || true
killall dnsmasq 2>/dev/null || true
ip link set wlan0 down 2>/dev/null || true; sleep 1
iw dev wlan0 set type managed
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
# Hand wlan0 back to dhcpcd — device-ap-mode denied it so the AP could own the
# interface while the wired link kept its lease.
sed -i '/static ip_address=192.168.100.1\\/24/d;/nohook wpa_supplicant/d;/^denyinterfaces wlan0\$/d' /etc/dhcpcd.conf 2>/dev/null || true
sed -i '/^address=\\/#\\//d' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || true
systemctl unmask wpa_supplicant@wlan0 2>/dev/null || true
systemctl enable wpa_supplicant@wlan0
systemctl restart wpa_supplicant@wlan0
systemctl enable dhcpcd
systemctl restart dhcpcd
echo "Waiting for IP..."; sleep 5
if ip addr show wlan0 | grep -q "inet "; then
  IP=\$(ip -4 addr show wlan0 | grep inet | awk '{print \$2}')
  echo "Connected. IP address: \$IP"
else
  echo "WARNING: wlan0 did not receive an IP address"
fi
systemctl restart avahi-daemon 2>/dev/null || true
echo "STA MODE ENABLED"
EOFSCRIPT
chmod +x /usr/local/bin/device-sta-mode

cat > /usr/local/bin/connect-wifi <<'EOFSCRIPT'
#!/bin/bash
set -e
WPA_CONF="\${WPA_CONF:-/etc/wpa_supplicant/wpa_supplicant-wlan0.conf}"
COUNTRY="\${COUNTRY:-US}"
[ "\$(id -u)" -ne 0 ] && { echo "Run as root or with sudo."; exit 1; }
if [ \$# -eq 0 ]; then read -r -p "SSID: " SSID; read -r -s -p "Password (empty=open): " PASS; echo ""; [ -z "\$SSID" ] && exit 1
elif [ \$# -eq 1 ]; then SSID="\$1"; PASS=""
else SSID="\$1"; PASS="\$2"; fi
ssid_esc="\${SSID//\\\\/\\\\\\\\}"; ssid_esc="\${ssid_esc//\\"/\\\\\\"}"
psk_esc="\${PASS//\\\\/\\\\\\\\}"; psk_esc="\${psk_esc//\\"/\\\\\\"}"
[ -f "\$WPA_CONF" ] && existing_country=\$(grep -E '^country=' "\$WPA_CONF" 2>/dev/null | head -1 | cut -d= -f2) && [ -n "\$existing_country" ] && COUNTRY="\$existing_country"
mkdir -p "\$(dirname "\$WPA_CONF")"
if [ -z "\$PASS" ]; then
  net_block="network={
	ssid=\\"\${ssid_esc}\\"
	key_mgmt=NONE
	scan_ssid=1
}"
else
  net_block="network={
	ssid=\\"\${ssid_esc}\\"
	psk=\\"\${psk_esc}\\"
	scan_ssid=1
}"
fi
cat >"\$WPA_CONF" <<WPA
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
country=\${COUNTRY}
fast_reauth=1
ap_scan=1
\${net_block}
WPA
chmod 600 "\$WPA_CONF"
/usr/local/bin/device-sta-mode
EOFSCRIPT
chmod +x /usr/local/bin/connect-wifi

# /usr/local/bin/software-update is NOT written here — it is installed from
# the canonical scripts/provision/software-update on the host, after this
# chroot block (see "install canonical software-update" below). Keeping it
# out of the chroot heredoc also takes it out of the escaping regime.

# ── network configs (hostapd, dnsmasq, wpa, dhcpcd) ──────────────────────────
echo "[stage] network configs"

# wpa_supplicant: country-only baseline. connect-wifi overwrites with creds.
mkdir -p /etc/wpa_supplicant
cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf <<EOF
country=\${COUNTRY_CODE}
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
EOF
chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf

# hostapd: SSID placeholder, device-ap-mode replaces at runtime.
if [ "\${AP_BAND}" = "5" ]; then
  CHANNEL="\${AP_CHANNEL:-36}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=a
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
else
  CHANNEL="\${AP_CHANNEL:-6}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=g
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
fi
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

mkdir -p /etc/dnsmasq.d
cat > /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf <<'EOF'
interface=wlan0
bind-interfaces
dhcp-range=wlan0,192.168.100.50,192.168.100.150,255.255.255.0,24h
address=/#/192.168.100.1
domain-needed
bogus-priv
no-resolv
EOF
[ -f /etc/dnsmasq.conf ] && sed -i 's/^interface=wlan0/#&/' /etc/dnsmasq.conf || true

# Keep the captive portal pointed at AP clients ONLY — never at ourselves.
#
# Debian's dnsmasq package registers itself with resolvconf as the system
# nameserver on start, which rewrites /etc/resolv.conf to "nameserver 127.0.0.1".
# Combined with the address=/#/ wildcard above, that means every hostname the
# DEVICE looks up resolves to 192.168.100.1 — itself. The symptom is brutal to
# read: `ping cdn.autonomous.ai` succeeds (it is pinging its own AP address) while
# `curl https://cdn.autonomous.ai` fails to connect, because nginx only listens on
# port 80. Everything that travels by name is dead: OTA / software-update, the LLM
# API, the MQTT broker, agent onboarding, the backend ping.
#
# It stayed invisible for as long as AP mode had no uplink at all, and the WiFi
# path never trips it because connect-wifi runs device-sta-mode — stopping
# dnsmasq — before the rest of setup needs a name. A device provisioned over
# ethernet keeps the AP (and dnsmasq) up while setup runs, so for it this is a
# hard blocker.
#
# DNSMASQ_EXCEPT="lo" is Debian's documented switch for exactly this: dnsmasq
# stops claiming loopback and stops registering as the system resolver, so the
# device resolves through its real upstream (the DHCP-supplied DNS, or the
# 1.1.1.1/8.8.8.8 fallback in resolvconf.conf). AP clients still get the full
# captive-portal wildcard on wlan0 — unchanged.
if [ -f /etc/default/dnsmasq ]; then
  sed -i '/^[[:space:]]*#*[[:space:]]*DNSMASQ_EXCEPT=/d' /etc/default/dnsmasq
fi
echo 'DNSMASQ_EXCEPT="lo"' >> /etc/default/dnsmasq

systemctl mask wpa_supplicant.service 2>/dev/null || true

# Advertise _autonomous._tcp via mDNS so the Autonomous Buddy (macOS) auto-finds
# this device. Static + device-agnostic: avahi's %h wildcard = the running
# hostname (<device_type>-<suffix>), so one baked file serves every device class.
# Port 80 = the nginx front door the buddy pairs through (/api/buddy/pair/confirm).
mkdir -p /etc/avahi/services
cat > /etc/avahi/services/autonomous.service <<'AVAHI'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_autonomous._tcp</type>
    <port>80</port>
  </service>
</service-group>
AVAHI

# ── nginx config (verbatim from production OPi) ──────────────────────────────
echo "[stage] nginx"
rm -f /etc/nginx/sites-enabled/default
cat > /etc/nginx/sites-enabled/default <<'NGINX'
upstream backend  { server 127.0.0.1:5000; }
upstream hal   { server 127.0.0.1:5001; }
upstream openclaw { server 127.0.0.1:18789; }

server {
  listen 80 default_server;
  root /usr/share/nginx/html/setup;
  index index.html;
  client_max_body_size 20M;

  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  # SPA cache policy. Vite fingerprints every asset (index-<hash>.js), so those
  # are safe to cache forever — the name changes whenever the content does. But
  # index.html is the pointer TO those names and must never be cached: a browser
  # holding a stale index.html asks for a bundle filename that the last web
  # deploy already deleted, gets a 404, and renders a blank page until the
  # operator hard-reloads. That is exactly what the setup popup hit.
  #
  # Uses `expires` rather than `add_header Cache-Control`: an add_header inside a
  # location cancels inheritance of ALL server-level add_header directives, which
  # would silently drop the CSP and the security headers above for these very
  # requests. `expires` sets Cache-Control without touching that inheritance.
  #
  # try_files below internal-redirects to /index.html, which re-runs location
  # matching, so the exact-match block covers the SPA-route case too — not just a
  # direct request for /index.html.
  location = /index.html { expires -1; }
  location /assets/      { expires 1y; }

  location / { try_files \$uri /index.html; }

  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location = /openapi.json {
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location = /api/system/exec {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location = /api/buddy/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  # Direct Harness device connection, authenticated by PAKE and pinned E2EE keys.
  location = /api/harness/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  location /api/ {
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  location /hw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://hal/;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /hw;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
  }

  location = /gw {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://openclaw/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
  }

  location /gw/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://openclaw/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
  }

  location = /generate_204       { return 204; }
  location = /hotspot-detect.html { return 204; }
  location = /ncsi.txt            { return 204; }
  location = /connecttest.txt     { return 204; }
}
NGINX
mkdir -p /usr/share/nginx/html/setup
echo '<h1>Device setup — flash the device and reboot.</h1>' > /usr/share/nginx/html/setup/index.html

# ── PulseAudio: WebRTC echo cancel + udev ignore for I2S codecs ──────────────
echo "[stage] PulseAudio"
PULSE_CONF="/etc/pulse/default.pa"
if [ -f "\$PULSE_CONF" ] && ! grep -q "module-echo-cancel" "\$PULSE_CONF"; then
  cat >> "\$PULSE_CONF" <<'PULSE_EOF'

### Echo cancellation (WebRTC AEC)
load-module module-echo-cancel source_name=aec_source sink_name=aec_sink aec_method=webrtc aec_args="analog_gain_control=0 digital_gain_control=0" channels=1
set-default-source aec_source
set-default-sink aec_sink
load-module module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse-anon-${DEVICE_TYPE}
PULSE_EOF
fi

# 91-pulseaudio-hal-ignore.rules — hardware team bakes udev rules into the base image.
# cat > /etc/udev/rules.d/91-pulseaudio-hal-ignore.rules <<'UDEV_EOF'
# # Keep PulseAudio away from the onboard I2S codecs so hal can own them.
# SUBSYSTEM=="sound", ATTR{id}=="sndi2s4", ENV{PULSE_IGNORE}="1"
# SUBSYSTEM=="sound", ATTR{id}=="wm8960soundcard", ENV{PULSE_IGNORE}="1"
# UDEV_EOF

# ── ALSA ─────────────────────────────────────────────────────────────────────
# /etc/asound.conf is hardware-team-owned and baked into the base image.
# It is NOT shipped in the device profile overlay.

# ── disable conflicting vendor services ──────────────────────────────────────
echo "[stage] mask conflicting vendor services"
systemctl mask orangepi-firstrun-config.service 2>/dev/null || true

# ── enable services (symlink, since chroot has no running systemd) ──────
echo "[stage] enable services"
for unit in os-server bootstrap hal openclaw avahi-daemon bluetooth; do
  systemctl enable "\$unit" 2>/dev/null || true
done

# SSH: gated by DEFAULT_AGENT, but ONLY for intern-v2 — every other DEVICE_TYPE
# (lamp included) keeps today's behavior (SSH always enabled), regardless of
# DEFAULT_AGENT. Per docs/developer-guide.md + the case-color ticket: claudecode
# (black case, Developer Edition) ships SSH open; every other default agent
# (hermes=blue, openclaw=orange, and codex/picoclaw) ships SSH closed. Unset
# DEFAULT_AGENT (most builds, and every non-intern-v2 build) → unchanged: SSH
# enabled. NOTE: codex/picoclaw closing SSH here is a judgment call (the
# ticket only named hermes/openclaw/claudecode) — revisit if codex should also
# open SSH as a second "dev" runtime.
if [ "\${DEVICE_TYPE}" = "intern-v2" ] && [ -n "\${DEFAULT_AGENT}" ] && [ "\${DEFAULT_AGENT}" != "claudecode" ]; then
  echo "[stage] DEFAULT_AGENT=\${DEFAULT_AGENT} (intern-v2 consumer edition) — SSH stays closed"
  systemctl disable ssh 2>/dev/null || true
  systemctl mask ssh 2>/dev/null || true
else
  echo "[stage] SSH enabled (DEVICE_TYPE=\${DEVICE_TYPE} DEFAULT_AGENT=\${DEFAULT_AGENT:-<unset>})"
  systemctl enable ssh 2>/dev/null || true
fi

# ── DT overlays (OrangePi 4 Pro A733): SPI3 for WS2812 LED ring, I2C0 (TWI0) ─
echo "[stage] enable SPI3 + I2C0 overlays"
if grep -q "^overlays=" /boot/orangepiEnv.txt 2>/dev/null; then
  sed -i "s/^overlays=.*/& spi3-cs0-cs1-spidev i2c0/" /boot/orangepiEnv.txt
else
  echo "overlays=spi3-cs0-cs1-spidev i2c0" >> /boot/orangepiEnv.txt
fi

echo "[stage] chroot Phase 2 complete"
CHROOT_STAGES

# ── install canonical software-update ────────────────────────────────────────
# The on-device OTA updater is one file in the repo (scripts/provision/
# software-update), staged into /input by the imager Makefile and installed
# here from the host — NOT written by heredoc inside the chroot. setup.sh
# inlines the same file at release time, so all fleets carry one version.
echo "[stage] install /usr/local/bin/software-update (canonical)"
[ -f /input/software-update ] || err "/input/software-update missing — run via 'make build' (it stages the file)"
install -m 0755 /input/software-update "${MNT}/usr/local/bin/software-update"

# Read runtime versions captured inside chroot (shell vars don't propagate out).
BAKED_OPENCLAW_VERSION=$(cat "${MNT}/tmp/baked-openclaw-version" 2>/dev/null | tr -d '[:space:]' || echo "unknown")
BAKED_HERMES_VERSION=$(cat "${MNT}/tmp/baked-hermes-version" 2>/dev/null | tr -d '[:space:]' || echo "unknown")
BAKED_CODEX_VERSION=$(cat "${MNT}/tmp/baked-codex-version" 2>/dev/null | tr -d '[:space:]' || echo "unbaked")
BAKED_CLAUDECODE_VERSION=$(cat "${MNT}/tmp/baked-claudecode-version" 2>/dev/null | tr -d '[:space:]' || echo "unbaked")
BAKED_PICOCLAW_VERSION=$(cat "${MNT}/tmp/baked-picoclaw-version" 2>/dev/null | tr -d '[:space:]' || echo "unbaked")
BAKED_OPENCODE_VERSION=$(cat "${MNT}/tmp/baked-opencode-version" 2>/dev/null | tr -d '[:space:]' || echo "unbaked")

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — OTA bake: backend binaries + hal + web UI + buddy
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 3 — OTA bake (OS binaries from metadata.json)"

chroot "${MNT}" /bin/bash <<OVERLAY_STAGES
set -euo pipefail
trap 'echo "OVERLAY ERROR: command failed at line \$LINENO (exit \$?): \$BASH_COMMAND"' ERR
export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:\$PATH"
export DEVICE_TYPE="${DEVICE_TYPE}"
export VARIANT="${VARIANT}"
export DEVICES_DIR="${DEVICES_DIR}"

retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-3}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n + 1))
    sleep "\$delay"
  done
  return 1
}

install_binary_from_zip() {
  local url="\$1" dest="\$2" name="\$3"
  local ztmp dtmp
  ztmp=\$(mktemp); dtmp=\$(mktemp -d)
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$ztmp' '\$url'" 5
  unzip -o -q "\$ztmp" -d "\$dtmp"; rm -f "\$ztmp"
  local bin
  bin=\$(find "\$dtmp" -type f -executable 2>/dev/null | head -1)
  [ -z "\$bin" ] && bin=\$(find "\$dtmp" -type f 2>/dev/null | head -1)
  [ -z "\$bin" ] && { echo "ERROR: no binary in \$url"; exit 1; }
  cp -f "\$bin" "\$dest"; chmod +x "\$dest"; rm -rf "\$dtmp"
  echo "[overlay] installed \$name → \$dest"
}

echo "[overlay] fetch OTA metadata"
META=\$(mktemp)
retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$META' '${OTA_METADATA_URL}'" 5
WEB_URL=\$(jq -r '.web.url // empty'               "\$META")
OS_SERVER_URL=\$(jq -r '."os-server".url // empty'             "\$META")
BOOTSTRAP_URL=\$(jq -r '.bootstrap.url // empty'   "\$META")
HAL_URL=\$(jq -r '.hal.url // empty'         "\$META")
BUDDY_URL=\$(jq -r '."claude-desktop-buddy".url // empty' "\$META")
DEVICES_URL=\$(jq -r --arg t "\$DEVICE_TYPE" '.devices[\$t].url // empty' "\$META")
WEB_VER=\$(jq -r '.web.version // empty'           "\$META")
OS_SERVER_VER=\$(jq -r '."os-server".version // empty'         "\$META")
BOOTSTRAP_VER=\$(jq -r '.bootstrap.version // empty' "\$META")
HAL_VER=\$(jq -r '.hal.version // empty'     "\$META")
BUDDY_VER=\$(jq -r '."claude-desktop-buddy".version // empty' "\$META")
# Save snapshot before removing — host reads it back after chroot exits to bake
# into /etc/autonomous-build.json. Must happen before rm -f below.
cp "\${META}" /tmp/metadata-baked.json 2>/dev/null || true
rm -f "\$META"
[ -z "\$WEB_URL" ] || [ -z "\$OS_SERVER_URL" ] || [ -z "\$BOOTSTRAP_URL" ] && {
  echo "ERROR: OTA metadata missing web.url / os-server.url / bootstrap.url"; exit 1
}
echo "[overlay] web=\$WEB_VER os-server=\$OS_SERVER_VER bootstrap=\$BOOTSTRAP_VER hal=\$HAL_VER buddy=\$BUDDY_VER"

echo "[overlay] backend binaries"
install_binary_from_zip "\$BOOTSTRAP_URL" /usr/local/bin/bootstrap-server "bootstrap"
install_binary_from_zip "\$OS_SERVER_URL"      /usr/local/bin/os-server      "os-server"

echo "[overlay] HAL"
HAL_DIR="/opt/hal"
if [ -n "\$HAL_URL" ]; then
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/hal.zip '\$HAL_URL'" 5
  unzip -o -q /tmp/hal.zip -d "\$HAL_DIR"
  rm -f /tmp/hal.zip
  # If zip nested into subdir, hoist up.
  if [ ! -f "\$HAL_DIR/pyproject.toml" ]; then
    SUBDIR=\$(find "\$HAL_DIR" -maxdepth 2 -name pyproject.toml 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    [ -n "\$SUBDIR" ] && [ "\$SUBDIR" != "\$HAL_DIR" ] && \\
      { shopt -s dotglob 2>/dev/null || true; mv "\$SUBDIR"/* "\$HAL_DIR"/; shopt -u dotglob 2>/dev/null || true; }
  fi
  find /root/.cache/uv -name 'lerobot.egg-info' -type d 2>/dev/null | xargs -r rm -rf || true
  rm -rf "\$HAL_DIR/.venv"
  cd "\$HAL_DIR"
  # Use the release lock when available; keep legacy archives installable.
  HAL_LOCK_ARG=""
  [ ! -f uv.lock ] || HAL_LOCK_ARG="--locked"
  retry "uv sync --python 3.12 --extra hardware --extra aec --extra pipecat \$HAL_LOCK_ARG" 3 10
  # webrtcvad pkg_resources patch (Py3.12+ killed pkg_resources).
  WEBRTCVAD_PY=\$(find "\$HAL_DIR/.venv" -name "webrtcvad.py" -path "*/site-packages/*" 2>/dev/null | head -1)
  if [ -n "\$WEBRTCVAD_PY" ] && grep -q "import pkg_resources" "\$WEBRTCVAD_PY"; then
    cat > "\$WEBRTCVAD_PY" <<'WEBRTCVAD_EOF'
try:
    import pkg_resources
    __version__ = pkg_resources.get_distribution('webrtcvad').version
except Exception:
    __version__ = '2.0.10'

import _webrtcvad

class Vad(object):
    def __init__(self, mode=None):
        self._vad = _webrtcvad.create()
        _webrtcvad.init(self._vad)
        if mode is not None:
            self.set_mode(mode)
    def set_mode(self, mode):
        _webrtcvad.set_mode(self._vad, mode)
    def is_speech(self, buf, sample_rate, length=None):
        length = length or int(len(buf) / 2)
        if length * 2 > len(buf):
            raise IndexError('buffer has %s frames, but length argument was %s' % (int(len(buf) / 2.0), length))
        return _webrtcvad.process(self._vad, sample_rate, buf, length)

def valid_rate_and_frame_length(rate, frame_length):
    return _webrtcvad.valid_rate_and_frame_length(rate, frame_length)
WEBRTCVAD_EOF
  fi
  cd /
else
  echo "[overlay] WARN: no hal URL — skipping"
fi

echo "[overlay] device profile (\$DEVICE_TYPE)"
# Bake the device profile so one DEVICE_TYPE = one golden image. The chroot's
# root is the image rootfs, so \$DEVICES_DIR/\$DEVICE_TYPE/ is the in-image path
# (same convention as /opt/hal above). No URL → WARN + skip, never fail.
if [ -n "\$DEVICES_URL" ]; then
  DEVICE_PROFILE_DIR="\$DEVICES_DIR/\$DEVICE_TYPE"
  mkdir -p "\$DEVICE_PROFILE_DIR"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/device-profile.zip '\$DEVICES_URL'" 5
  unzip -o -q /tmp/device-profile.zip -d "\$DEVICE_PROFILE_DIR"
  rm -f /tmp/device-profile.zip
  echo "[overlay] device profile baked → \$DEVICE_PROFILE_DIR"
  # Bake the selection in the overlay, never in the reusable base image.
  if [ -n "\$VARIANT" ] && [ "\$VARIANT" != standard ]; then
    mkdir -p /etc/autonomous
    printf '%s\n' "\$VARIANT" > /etc/autonomous/hardware-profile
    [ -f "\$DEVICE_PROFILE_DIR/apply-overrides.py" ] \
      || { echo "[overlay] ERROR: Hardware overrides require an override-capable device package" >&2; exit 1; }
    python3 "\$DEVICE_PROFILE_DIR/apply-overrides.py" --profile "\$DEVICE_PROFILE_DIR" --root / \
      || { echo "[overlay] ERROR: Hardware override configuration failed" >&2; exit 1; }
  else
    rm -f /etc/autonomous/hardware-profile
  fi
  # Device rootfs overlay: robots/<type>/rootfs/ mirrors the target filesystem.
  # Copy the whole tree onto / for device-specific system config (udev rules, …).
  if [ -d "\$DEVICE_PROFILE_DIR/rootfs" ]; then
    cp -a "\$DEVICE_PROFILE_DIR/rootfs/." /
    echo "[overlay] device rootfs overlay applied"
  else
    echo "[overlay] WARN: device profile has no rootfs/ overlay"
  fi
else
  echo "[overlay] ERROR: no devices.\$DEVICE_TYPE url in OTA metadata — device profile is required (one image = one device type). Run 'make upload-device \$DEVICE_TYPE' before building." >&2
  exit 1
fi

echo "[overlay] web UI"
retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/web.zip '\$WEB_URL'" 5
rm -rf /usr/share/nginx/html/setup/*
unzip -o -q /tmp/web.zip -d /usr/share/nginx/html/setup
rm -f /tmp/web.zip

if [ -n "\$BUDDY_URL" ]; then
  echo "[overlay] Claude Desktop Buddy"
  BUDDY_DIR="/opt/claude-desktop-buddy"
  mkdir -p "\$BUDDY_DIR" /root/config
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/buddy.zip '\$BUDDY_URL'" 5
  unzip -o -q /tmp/buddy.zip -d /tmp/buddy-extract
  rm -f /tmp/buddy.zip
  if [ -f /tmp/buddy-extract/buddy-plugin ]; then
    cp -f /tmp/buddy-extract/buddy-plugin "\$BUDDY_DIR/buddy-plugin"
    chmod +x "\$BUDDY_DIR/buddy-plugin"
  fi
  [ ! -f /root/config/buddy.json ] && [ -f /tmp/buddy-extract/config/buddy.json ] && \\
    cp -f /tmp/buddy-extract/config/buddy.json /root/config/buddy.json
  echo "\$BUDDY_VER" > "\$BUDDY_DIR/VERSION_BUDDY"
  rm -rf /tmp/buddy-extract
  cat > /etc/systemd/system/claude-desktop-buddy.service <<'UNIT'
[Unit]
Description=Claude Desktop Buddy (BLE)
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/claude-desktop-buddy
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-desktop-buddy

[Install]
WantedBy=multi-user.target
UNIT
  systemctl enable claude-desktop-buddy
else
  echo "[overlay] no buddy URL — skipping"
fi

echo "[overlay] Phase 3 complete"

# Persist OTA versions to a file inside the image; host script reads it back
# out after chroot exits to build the manifest. Key=value format so a shell
# 'source' on the host pulls them into variables.
cat > /tmp/ota-versions.env <<MANIFEST
WEB_VER=\${WEB_VER}
OS_SERVER_VER=\${OS_SERVER_VER}
BOOTSTRAP_VER=\${BOOTSTRAP_VER}
HAL_VER=\${HAL_VER}
BUDDY_VER=\${BUDDY_VER}
MANIFEST

OVERLAY_STAGES

# Capture OTA versions for the build manifest before they get wiped by Phase 5.
BAKED_WEB_VER=""; BAKED_OS_SERVER_VER=""; BAKED_BOOTSTRAP_VER=""; BAKED_HAL_VER=""; BAKED_BUDDY_VER=""
if [ -f "${MNT}/tmp/ota-versions.env" ]; then
  # shellcheck disable=SC1090
  . "${MNT}/tmp/ota-versions.env" || true
  BAKED_WEB_VER="${WEB_VER:-}"
  BAKED_OS_SERVER_VER="${OS_SERVER_VER:-}"
  BAKED_BOOTSTRAP_VER="${BOOTSTRAP_VER:-}"
  BAKED_HAL_VER="${HAL_VER:-}"
  BAKED_BUDDY_VER="${BUDDY_VER:-}"
  rm -f "${MNT}/tmp/ota-versions.env"
fi

# Write the build manifest. Makefile `upload` target reads this to populate
# the per-release note with OTA versions actually baked in.
SRC_7Z_SHA=$(sha256sum "${SRC_7Z}" 2>/dev/null | cut -d' ' -f1 || echo unknown)
cat > /output/manifest-opi.json <<MANIFEST_JSON
{
  "build_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "target": "opi",
  "openclaw_version": "${OPENCLAW_VERSION}",
  "out_img_size": "${OUT_IMG_SIZE}",
  "ota_metadata_url": "${OTA_METADATA_URL}",
  "ota_versions": {
    "web": "${BAKED_WEB_VER}",
    "os-server": "${BAKED_OS_SERVER_VER}",
    "bootstrap": "${BAKED_BOOTSTRAP_VER}",
    "hal": "${BAKED_HAL_VER}",
    "claude-desktop-buddy": "${BAKED_BUDDY_VER}"
  },
  "source_image": {
    "file_id": "${OPI_FILE_ID}",
    "name": "${OPI_FILE_NAME}.7z",
    "sha256": "${SRC_7Z_SHA}"
  }
}
MANIFEST_JSON
log "Manifest: /output/manifest-opi.json"

# Bake a build snapshot into the image so anyone SSH-ing in can see exactly
# what was flashed: when, from which git commit, what the hardware team's
# manifest contained, and what OTA metadata was live at build time.
# Check with: cat /etc/autonomous-build.json | jq .
METADATA_FOR_SNAPSHOT="${MNT}/tmp/metadata-baked.json"
HW_MANIFEST_FILE="/input/${DEVICE_TYPE}/manifest-opi-dev.json"
if [ -f "${METADATA_FOR_SNAPSHOT}" ]; then
  HW_MANIFEST_JSON="null"
  if [ -f "${HW_MANIFEST_FILE}" ]; then
    HW_MANIFEST_JSON=$(cat "${HW_MANIFEST_FILE}")
  else
    log "WARN: no hardware manifest at ${HW_MANIFEST_FILE} — hardware_manifest will be null"
  fi
  jq -n \
    --arg build_date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg git_commit "${BUILD_GIT_SHA:-unknown}" \
    --arg hermes_version "${BAKED_HERMES_VERSION:-unknown}" \
    --arg openclaw_version "${BAKED_OPENCLAW_VERSION:-unknown}" \
    --arg codex_version "${BAKED_CODEX_VERSION:-unbaked}" \
    --arg claudecode_version "${BAKED_CLAUDECODE_VERSION:-unbaked}" \
    --arg picoclaw_version "${BAKED_PICOCLAW_VERSION:-unbaked}" \
    --arg opencode_version "${BAKED_OPENCODE_VERSION:-unbaked}" \
    --argjson hw_manifest "${HW_MANIFEST_JSON}" \
    --slurpfile ota_metadata "${METADATA_FOR_SNAPSHOT}" \
    '{
      build_date: $build_date,
      git_commit: $git_commit,
      hardware_manifest: $hw_manifest,
      baked_runtimes: { hermes: $hermes_version, openclaw: $openclaw_version, codex: $codex_version, claudecode: $claudecode_version, picoclaw: $picoclaw_version, opencode: $opencode_version },
      ota_metadata: $ota_metadata[0]
    }' > "${MNT}/etc/autonomous-build.json"
  rm -f "${METADATA_FOR_SNAPSHOT}"
  log "Build snapshot: /etc/autonomous-build.json"
else
  log "WARN: skipping build snapshot — metadata missing"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Install resize-once.service (first-boot SD-fill expand)
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 4 — resize-once (first-boot expand)"

cat > "${MNT}/usr/local/bin/resize-once" <<'RESIZE_EOF'
#!/bin/bash
# Runs ONCE at first boot. Expands root partition + ext4 to fill the SD card,
# then disables itself. Compares root partition device to deduce the parent
# disk + partition number — works for mmcblk1p1 (SD), mmcblk0p1 (eMMC), etc.
set -uo pipefail
log() { echo "==> $*"; }
ROOT_PART=$(findmnt -n -o SOURCE /)
[ -z "${ROOT_PART}" ] && { echo "ERROR: cannot determine root partition"; exit 1; }
log "root partition: ${ROOT_PART}"
# Strip /dev/, then split into disk + part number.
DEV_NAME=$(basename "${ROOT_PART}")
case "${DEV_NAME}" in
  mmcblk*p*|nvme*p*) PARENT="${DEV_NAME%p*}"; PART_NUM="${DEV_NAME##*p}" ;;
  sd?[0-9]*)         PARENT="${DEV_NAME%%[0-9]*}"; PART_NUM="${DEV_NAME##*[a-z]}" ;;
  *) echo "ERROR: unrecognised root device naming ${DEV_NAME}"; exit 1 ;;
esac
DISK="/dev/${PARENT}"
log "parent disk=${DISK} partition=${PART_NUM}"

# growpart needs the partition unmounted-ish; on a mounted root this works
# because growpart only edits the partition table, not data blocks.
growpart "${DISK}" "${PART_NUM}" || { log "growpart already at max — nothing to do"; }
resize2fs "${ROOT_PART}" || { log "WARN resize2fs failed"; }
log "resize complete"

# Self-disable so this service never runs again, even if image is re-cloned.
systemctl disable resize-once.service 2>/dev/null || true
rm -f /etc/systemd/system/resize-once.service
rm -f /etc/systemd/system/multi-user.target.wants/resize-once.service
rm -f /usr/local/bin/resize-once
RESIZE_EOF
chmod +x "${MNT}/usr/local/bin/resize-once"

cat > "${MNT}/etc/systemd/system/resize-once.service" <<'UNIT'
[Unit]
Description=Expand root filesystem to fill SD card on first boot (self-destructing)
ConditionPathExists=/usr/local/bin/resize-once
DefaultDependencies=no
After=local-fs.target systemd-remount-fs.service
Before=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/resize-once

[Install]
WantedBy=multi-user.target
UNIT

# Manually link into wants (systemctl enable inside chroot also works, but we
# already exited the chroot — symlink is the equivalent + no DBus needed).
mkdir -p "${MNT}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/resize-once.service \
  "${MNT}/etc/systemd/system/multi-user.target.wants/resize-once.service"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 — Restore resolv.conf, unmount, compress
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 5 — finalize"

mv -f "${MNT}/etc/resolv.conf.bak" "${MNT}/etc/resolv.conf" 2>/dev/null || true

# Kill any stale chroot processes (apt post-install spawns dbus/sshd sometimes).
for pid in $(lsof -t +D "${MNT}" 2>/dev/null || true); do
  kill -9 "$pid" 2>/dev/null || true
done
fuser -k -M "${MNT}" 2>/dev/null || true
rm -f "${MNT}/run/sshd.pid" "${MNT}/run/dbus/pid" 2>/dev/null || true
rm -rf "${MNT}/run/lock"/* 2>/dev/null || true
rm -f "${MNT}/usr/bin/qemu-aarch64-static"

umount "${MNT}/dev"
umount "${MNT}/sys"
umount "${MNT}/proc"

# Flush + unmount root before xz so the on-disk filesystem is consistent.
sync
umount "${MNT}"
losetup -d "${LOOP_DEV}"; LOOP_DEV=""

# COMPRESS=0 skips the .xz step (single-threaded under Docker's ~2 GB memory
# cap, so it can take longer than the whole build). The raw .img is complete
# and flashable via `make sd-card-flash-raw`.
if [ "${COMPRESS:-1}" = "0" ]; then
  log "DONE: ${OUT_IMG} (COMPRESS=0, skipped .xz)"
  log "Flash:  make sd-card-flash-raw DEVICE_TYPE=${DEVICE_TYPE} DISK=N"
  exit 0
fi
log "Compressing ${OUT_IMG} → ${OUT_IMG}.xz (this takes a few minutes)…"
rm -f "${OUT_IMG}.xz"
# -k keeps the original .img alongside the .xz so operator can verify/inspect
# or flash raw before deciding to delete. Manual cleanup: rm -f output/golden-opi.img
xz -9 -k --threads=0 "${OUT_IMG}"

log "DONE: ${OUT_IMG}.xz ($(du -h "${OUT_IMG}.xz" | cut -f1))"
log "Flash:  make sd-card-flash DISK=N    (decompresses on the fly via xz | dd)"
