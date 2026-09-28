#!/bin/bash
# Production setup for Raspberry Pi 5: single-interface AP/STA switch, nginx setup web + API proxy, os-server backend.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------
# Utils
# ----------------------------------------------------------
retry() {
  local n=0
  local max=$2
  local delay=${3:-2}
  local cmd="$1"
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n+1))
    echo "Retry $n/$max..."
    sleep "$delay"
  done
  echo "ERROR: Command failed after $max attempts: $cmd"
  return 1
}

ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root"
    exit 1
  fi
}

# Wrapper so a single stage failure doesn't abort the whole script — critical
# for AP/recovery: stage_ap must always be reached so the device can re-provision
# via WiFi even if app stages (hal/buddy/openclaw) fail.
# Inside `if "$name"` bash suspends `set -e` for the function call, so failures
# inside a stage propagate up as the function's exit code without aborting.
FAILED_STAGES=""
run_stage() {
  local name=$1
  echo ""
  echo "[main] ==== $name ===="
  if "$name"; then
    echo "[main] ==== $name OK ===="
  else
    local rc=$?
    echo "[main] !!!! $name FAILED (exit $rc) — continuing so AP/recovery can still be set up"
    FAILED_STAGES="$FAILED_STAGES $name"
  fi
}

# Optional: AP band and channel. Pi 5 Bookworm: firmware config is /boot/firmware/config.txt;
# ensure dtoverlay=disable-wifi is not set or WiFi will stay off.
AP_BAND="${AP_BAND:-2.4}"       # 2.4 or 5 (5 GHz for better throughput)
AP_CHANNEL="${AP_CHANNEL:-}"    # default: 6 (2.4 GHz) or 36 (5 GHz); override e.g. AP_CHANNEL=11 or 40

# Device class this install is for — selects robots/<type>/{DEVICE,SOUL}.md at
# runtime (os-server + HAL) and drives the network identity (<type>-XXXX SSID +
# mDNS hostname). REQUIRED, no default — provisioning must declare the device class
# so one OS image can't silently brand itself "lamp". Pass via env, e.g.
# DEVICE_TYPE=intern (install.sh forwards it).
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE must be set (e.g. DEVICE_TYPE=lamp) — no default}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"

# ----------------------------------------------------------
# Stage -1: Locale (Bookworm hygiene)
# ----------------------------------------------------------
stage_locale() {
  echo "[stage] Fix locale (Bookworm)"
  unset LC_CTYPE
  apt update
  apt install -y locales
  sed -i 's/^# *\(C\.UTF-8 UTF-8\)/\1/' /etc/locale.gen 2>/dev/null || true
  grep -q '^C\.UTF-8 UTF-8' /etc/locale.gen || echo 'C.UTF-8 UTF-8' >> /etc/locale.gen
  locale-gen C.UTF-8 2>/dev/null || locale-gen
  echo "LC_ALL=C.UTF-8" > /etc/locale.conf
  echo "LANG=C.UTF-8" >> /etc/locale.conf
}

# ----------------------------------------------------------
# Stage 0: Prerequisites
# ----------------------------------------------------------
stage_prerequisites() {
  echo "[stage] Install system packages"
  apt update
  # openresolv ships the `resolvconf` binary that dhcpcd's 20-resolv.conf hook
  # needs to push DHCP-supplied nameservers into /etc/resolv.conf. Without it
  # (observed on OrangePi Armbian images) /etc/resolv.conf stays empty even
  # though dhcpcd writes the lease into /run/resolvconf/interface/wlan0.dhcp,
  # so `ping 8.8.8.8` works but every hostname lookup fails.
  apt install -y \
    hostapd dnsmasq nginx unzip curl jq openssl wpasupplicant dhcpcd iproute2 iptables \
    iw git xvfb xauth chromium chromium-sandbox openresolv \
    avahi-daemon avahi-utils libnss-mdns || true
  systemctl stop hostapd dnsmasq nginx 2>/dev/null || true
  systemctl unmask hostapd dnsmasq 2>/dev/null || true
  # Some base images (Armbian / older RPi OS images that once ran NetworkManager
  # or systemd-resolved) ship /etc/resolv.conf as a regular file with no
  # nameserver lines, so dhcpcd's lease never reaches the glibc resolver. Only
  # repair when actually broken: if a nameserver is already present (symlink to
  # /run/resolvconf/resolv.conf or /run/systemd/resolve/*, or a working static
  # file), leave it alone so we don't disrupt RPi OS / systemd-resolved setups.
  if ! grep -qE '^[[:space:]]*nameserver[[:space:]]+' /etc/resolv.conf 2>/dev/null; then
    echo "[stage] /etc/resolv.conf has no nameserver — repairing via resolvconf"
    rm -f /etc/resolv.conf
    mkdir -p /run/resolvconf
    ln -sf /run/resolvconf/resolv.conf /etc/resolv.conf
    resolvconf -u 2>/dev/null || true
  fi
  # Static fallback so /etc/resolv.conf is never completely empty — matters in
  # AP mode (hostapd up, no upstream DHCP lease for wlan0) and during the brief
  # window between dhcpcd start and the first lease. Appended via openresolv's
  # name_servers= so it joins, not replaces, the DHCP-supplied nameservers.
  if [ -f /etc/resolvconf.conf ]; then
    grep -q '^name_servers=' /etc/resolvconf.conf || echo 'name_servers="1.1.1.1 8.8.8.8"' >> /etc/resolvconf.conf
  else
    echo 'name_servers="1.1.1.1 8.8.8.8"' > /etc/resolvconf.conf
  fi
  resolvconf -u 2>/dev/null || true
  # Node.js 22 for OpenClaw CLI
  if ! command -v node &>/dev/null || ! node -v 2>/dev/null | grep -qE '^v(2[2-9]|[3-9][0-9])'; then
    echo "[stage] Install Node.js 22 (NodeSource)"
    curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" https://deb.nodesource.com/setup_22.x | bash -
    apt install -y nodejs
  fi
  # Keep wpa_supplicant running so STA (e.g. Pi Imager WiFi) stays connected during setup.
  # Global wpa_supplicant is stopped/masked only when we switch to AP in device-ap-mode.
}

# ----------------------------------------------------------
# Stage 0a: Disable IPv6 (RPi 5 STA-drop workaround; harmless on OrangePi)
# ----------------------------------------------------------
stage_rpi5_wifi_stability() {
  echo "[stage] Disable IPv6"

  mkdir -p /etc/sysctl.d
  cat >/etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
  sysctl -p /etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf 2>/dev/null || true
}

# ----------------------------------------------------------
# Stage 0b: OTA metadata (web, lamp, bootstrap URLs from GCS)
# ----------------------------------------------------------
# ----------------------------------------------------------
# Stage 0c: Enable SPI in firmware config
# ----------------------------------------------------------device-
stage_enable_spi() {
  echo "[stage] Enable SPI + I2C in firmware config"

  local cfg=""
  if [ -f /boot/firmware/config.txt ]; then
    cfg="/boot/firmware/config.txt"
  elif [ -f /boot/config.txt ]; then
    cfg="/boot/config.txt"
  else
    echo "[stage] No /boot/firmware/config.txt or /boot/config.txt found; skipping SPI/I2C enable"
    return 0
  fi

  # For each bus: if the dtparam is present but commented, uncomment it; otherwise append.
  local param
  for param in spi=on i2c_arm=on; do
    if grep -qE "^\s*#?\s*dtparam=$param" "$cfg" 2>/dev/null; then
      sed -i -E "s/^\s*#\s*(dtparam=$param)/\1/" "$cfg" 2>/dev/null || true
      echo "[stage] Ensured dtparam=$param is enabled in $cfg"
    else
      {
        echo ""
        echo "# Enabled by lamp setup.sh"
        echo "dtparam=$param"
      } >>"$cfg"
      echo "[stage] Added dtparam=$param to $cfg"
    fi
  done

  echo "[stage] SPI/I2C enablement will take effect after reboot"
}

# OTA metadata URL must be provided by the caller (install.sh sets it). No
# fallback — fail fast here, before installing anything, if it is missing.
OTA_METADATA_URL="${OTA_METADATA_URL:?OTA_METADATA_URL is required — run via install.sh or export it before running setup.sh}"
OTA_SIGNING_PUBLIC_KEY="${OTA_SIGNING_PUBLIC_KEY:-}"

# verify_ota_metadata writes the authenticated metadata payload to its second
# argument. The public key is supplied by the trusted installer/image and is
# never read from the remotely fetched envelope.
verify_ota_metadata() {
  local envelope="$1" payload="$2" public_key="$3"
  local public_der signature
  public_der=$(mktemp)
  signature=$(mktemp)
  trap 'rm -f "$public_der" "$signature"' RETURN
  jq -er '(.signed // .) | .format == "autonomous-ota/v1" and .signature.algorithm == "ed25519"' "$envelope" >/dev/null \
    || { echo "ERROR: unsupported or unsigned OTA metadata" >&2; return 1; }
  # SubjectPublicKeyInfo DER prefix for a raw 32-byte Ed25519 public key.
  printf '\060\052\060\005\006\003\053\145\160\003\041\000' >"$public_der"
  printf '%s' "$public_key" | base64 -d >>"$public_der" \
    || { echo "ERROR: OTA_SIGNING_PUBLIC_KEY is not base64" >&2; return 1; }
  jq -r '(.signed // .).payload' "$envelope" | base64 -d >"$payload" \
    || { echo "ERROR: invalid OTA metadata payload" >&2; return 1; }
  jq -r '(.signed // .).signature.value' "$envelope" | base64 -d >"$signature" \
    || { echo "ERROR: invalid OTA metadata signature" >&2; return 1; }
  openssl pkeyutl -verify -pubin -keyform DER -inkey "$public_der" -rawin -in "$payload" -sigfile "$signature" >/dev/null \
    || { echo "ERROR: OTA metadata signature verification failed" >&2; return 1; }
}

stage_ota_metadata() {
  echo "[stage] Fetch OTA metadata"
  METADATA_ENVELOPE_TMP="/tmp/ota-metadata.$$.envelope"
  METADATA_TMP="/tmp/ota-metadata.$$.json"
  retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o \"$METADATA_ENVELOPE_TMP\" \"$OTA_METADATA_URL\"" 5
  if [ -n "$OTA_SIGNING_PUBLIC_KEY" ]; then
    verify_ota_metadata "$METADATA_ENVELOPE_TMP" "$METADATA_TMP" "$OTA_SIGNING_PUBLIC_KEY"
  else
    cp "$METADATA_ENVELOPE_TMP" "$METADATA_TMP"
    echo "[stage] WARN: OTA signing is not configured; using legacy unsigned metadata"
  fi
  export WEB_VERSION WEB_URL WEB_SHA256 OS_SERVER_VERSION OS_SERVER_URL OS_SERVER_SHA256 BOOTSTRAP_VERSION BOOTSTRAP_URL BOOTSTRAP_SHA256 HAL_SHA256 DEVICES_SHA256
  WEB_VERSION=$(jq -r '.web.version // empty' "$METADATA_TMP")
  WEB_URL=$(jq -r '.web.url // empty' "$METADATA_TMP")
  WEB_SHA256=$(jq -r '.web.sha256 // empty' "$METADATA_TMP")
  OS_SERVER_VERSION=$(jq -r '."os-server".version // empty' "$METADATA_TMP")
  OS_SERVER_URL=$(jq -r '."os-server".url // empty' "$METADATA_TMP")
  OS_SERVER_SHA256=$(jq -r '."os-server".sha256 // empty' "$METADATA_TMP")
  BOOTSTRAP_VERSION=$(jq -r '.bootstrap.version // empty' "$METADATA_TMP")
  BOOTSTRAP_URL=$(jq -r '.bootstrap.url // empty' "$METADATA_TMP")
  BOOTSTRAP_SHA256=$(jq -r '.bootstrap.sha256 // empty' "$METADATA_TMP")
  HAL_VERSION=$(jq -r '.hal.version // empty' "$METADATA_TMP")
  HAL_URL=$(jq -r '.hal.url // empty' "$METADATA_TMP")
  HAL_SHA256=$(jq -r '.hal.sha256 // empty' "$METADATA_TMP")
  BUDDY_VERSION=$(jq -r '."claude-desktop-buddy".version // empty' "$METADATA_TMP")
  BUDDY_URL=$(jq -r '."claude-desktop-buddy".url // empty' "$METADATA_TMP")
  DEVICES_URL=$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].url // empty' "$METADATA_TMP")
  DEVICES_SHA256=$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].sha256 // empty' "$METADATA_TMP")
  rm -f "$METADATA_ENVELOPE_TMP" "$METADATA_TMP"
  if [ -z "$WEB_URL" ] || [ -z "$OS_SERVER_URL" ] || [ -z "$BOOTSTRAP_URL" ] || { [ -n "$OTA_SIGNING_PUBLIC_KEY" ] && ! [[ "$WEB_SHA256$OS_SERVER_SHA256$BOOTSTRAP_SHA256" =~ ^[a-fA-F0-9]{192}$ ]]; }; then
    echo "ERROR: OTA metadata missing required URL or SHA-256. Check $OTA_METADATA_URL"
    exit 1
  fi
  echo "[stage] OTA versions: web=$WEB_VERSION os-server=$OS_SERVER_VERSION bootstrap=$BOOTSTRAP_VERSION hal=$HAL_VERSION buddy=$BUDDY_VERSION"

  # Seed metadata_url into the bootstrap worker's config so the OTA metadata URL
  # comes from /root/config/bootstrap.json (a per-deployment value) instead of a
  # compiled-in default. Merge-if-empty so re-running setup never clobbers a
  # custom URL an operator already set.
  mkdir -p /root/config
  local bs_json="/root/config/bootstrap.json"
  if [ -f "$bs_json" ]; then
    local bs_tmp; bs_tmp=$(mktemp)
    if jq --arg url "$OTA_METADATA_URL" --arg key "$OTA_SIGNING_PUBLIC_KEY" \
        'if (.metadata_url // "") == "" then .metadata_url = $url else . end | if (.signing_public_key // "") == "" then .signing_public_key = $key else . end' \
        "$bs_json" >"$bs_tmp" 2>/dev/null; then
      mv "$bs_tmp" "$bs_json"
    else
      rm -f "$bs_tmp"
      echo "[stage] WARN: could not seed metadata_url (invalid $bs_json), leaving as-is"
    fi
  else
    jq -n --arg url "$OTA_METADATA_URL" --arg key "$OTA_SIGNING_PUBLIC_KEY" \
      '{httpPort: 8080, metadata_url: $url, signing_public_key: $key, poll_interval: "5m", state_file: "/root/bootstrap/state.json"}' \
      >"$bs_json"
  fi
  echo "[stage] Seeded metadata_url=$OTA_METADATA_URL into $bs_json"
}

# Download zip from URL, unzip, copy single binary to dest path (handles os-server, bootstrap-server in zip)
install_binary_from_zip() {
  local url="$1"
  local dest_binary="$2"
  local name="$3"
  local digest="$4"
  local zip_tmp="/tmp/${name}-zip.$$"
  local dir_tmp="/tmp/${name}-dir.$$"
  mkdir -p "$dir_tmp"
  retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o \"$zip_tmp\" \"$url\"" 5
  [ -z "$digest" ] || echo "$digest  $zip_tmp" | sha256sum -c - >/dev/null || { echo "ERROR: SHA-256 mismatch for $name"; rm -f "$zip_tmp"; return 1; }
  unzip -o -q "$zip_tmp" -d "$dir_tmp"
  rm -f "$zip_tmp"
  # Zip may contain os-server, bootstrap-server or bare binary (at root or in subdir)
  local bin_file
  bin_file=$(find "$dir_tmp" -type f -executable 2>/dev/null | head -1)
  [ -z "$bin_file" ] && bin_file=$(find "$dir_tmp" -type f 2>/dev/null | head -1)
  if [ -z "$bin_file" ] || [ ! -f "$bin_file" ]; then
    echo "ERROR: No binary found in zip from $url"
    rm -rf "$dir_tmp" 2>/dev/null || true
    exit 1
  fi
  cp -f "$bin_file" "$dest_binary"
  chmod +x "$dest_binary"
  rm -rf "$dir_tmp"
}

# ----------------------------------------------------------
# Stage 1: Backend (bootstrap + os-server from OTA metadata)
# ----------------------------------------------------------
stage_backend() {
  echo "[stage] Install backend (bootstrap + os-server)"

  # Migrate old openclaw config dir from /root/openclaw → /root/.openclaw
  if [ -d "/root/openclaw" ] && [ ! -d "/root/.openclaw" ]; then
    echo "[migrate] Moving /root/openclaw → /root/.openclaw"
    mv /root/openclaw /root/.openclaw
    # Update openclaw_config_dir in lamp config.json if it still points to old path
    if [ -f "/root/config/config.json" ]; then
      sed -i 's|"openclaw_config_dir"[[:space:]]*:[[:space:]]*"/root/openclaw"|"openclaw_config_dir": "/root/.openclaw"|g' /root/config/config.json
      echo "[migrate] Updated config.json openclaw_config_dir"
    fi
  fi

  install_binary_from_zip "$BOOTSTRAP_URL" /usr/local/bin/bootstrap-server "bootstrap" "$BOOTSTRAP_SHA256"
  install_binary_from_zip "$OS_SERVER_URL" /usr/local/bin/os-server "os-server" "$OS_SERVER_SHA256"

  cat >/etc/systemd/system/bootstrap.service <<EOF
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
EOF

  cat >/etc/systemd/system/os-server.service <<EOF
[Unit]
Description=Autonomous OS Server
After=network-online.target

[Service]
User=root
WorkingDirectory=/root
Environment=DEVICE_TYPE=$DEVICE_TYPE
Environment=DEVICES_DIR=$DEVICES_DIR
ExecStart=/usr/local/bin/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable bootstrap os-server
  # Do NOT start os-server here — it switches to AP mode when unconfigured, killing internet.
  # Services will start after reboot at the end of setup.
  # /usr/local/bin/software-update is written later by stage_ap (covers
  # all six components: os-server, openclaw, bootstrap, web, hal, claude-desktop-buddy).
}

# ----------------------------------------------------------
# Stage 1a: HAL (Python hardware runtime)
# ----------------------------------------------------------
stage_hal() {
  echo "[stage] Install HAL (Python hardware drivers)"

  HAL_DIR="/opt/hal"
  mkdir -p "$HAL_DIR"

  if [ -n "$HAL_URL" ]; then
    echo "[stage] Downloading HAL from OTA..."
    retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o /tmp/hal.zip \"$HAL_URL\"" 5
    [ -z "$HAL_SHA256" ] || echo "$HAL_SHA256  /tmp/hal.zip" | sha256sum -c - >/dev/null || { echo "[stage] ERROR: HAL SHA-256 mismatch"; return 1; }
    unzip -o -q /tmp/hal.zip -d "$HAL_DIR"
    rm -f /tmp/hal.zip
  else
    echo "[stage] WARN: No hal URL in OTA metadata, skipping download"
  fi

  # Install uv + system libs for audio/camera + PulseAudio echo cancellation
  apt update
  apt install -y libportaudio2 portaudio19-dev pulseaudio pulseaudio-utils pulseaudio-module-bluetooth bluez ffmpeg || true

  # PulseAudio WebRTC AEC (echo cancellation for mic/speaker loopback)
  PULSE_CONF="/etc/pulse/default.pa"
  if [ -f "$PULSE_CONF" ] && ! grep -q "module-echo-cancel" "$PULSE_CONF"; then
    echo "[stage] Configuring PulseAudio echo cancellation (WebRTC AEC)"
    cat >> "$PULSE_CONF" <<'PULSE_EOF'

### Echo cancellation (WebRTC AEC) for Lamp smart lamp
load-module module-echo-cancel source_name=aec_source sink_name=aec_sink aec_method=webrtc aec_args="analog_gain_control=0 digital_gain_control=0" channels=1
set-default-source aec_source
set-default-sink aec_sink
PULSE_EOF
  fi

  # Anonymous unix socket so the root-owned hal service can reach the
  # uid-1000 PulseAudio daemon (libpulse rejects cookie auth when the socket
  # owner differs from the connecting uid). Pairs with the PULSE_SERVER env
  # added to the hal.service unit below. Required for Bluetooth
  # headset routing (pactl set-default-sink to a bluez sink).
  if [ -f "$PULSE_CONF" ] && ! grep -q "pulse-anon" "$PULSE_CONF"; then
    echo "[stage] Configuring PulseAudio anonymous socket for root access"
    cat >> "$PULSE_CONF" <<PULSE_EOF

### Anonymous unix socket so root-owned hal can reach this PA daemon
load-module module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse-anon-${DEVICE_TYPE}
PULSE_EOF
  fi

  # Keep PulseAudio off the lamp speaker codec. hal's TTS opens this card
  # directly via ALSA hw for a persistent low-latency OutputStream, and `aplay`
  # in the music pipeline also writes to it via plug:device_speaker. If PA
  # auto-loads module-alsa-card for the same card (which it does once udev
  # finishes settling), the device becomes exclusively held and every other
  # consumer fails open with EBUSY / PaErrorCode -9985.
  # ATTR{id} values: sndi2s4 = OrangePi onboard ES8389 codec; wm8960soundcard
  # = Raspberry Pi (Seeed wm8960 hat).
  PA_IGNORE_RULE="/etc/udev/rules.d/91-pulseaudio-hal-ignore.rules"
  if [ ! -f "$PA_IGNORE_RULE" ]; then
    echo "[stage] Adding udev rule so PulseAudio ignores the lamp speaker card"
    cat > "$PA_IGNORE_RULE" <<'UDEV_EOF'
# Keep PulseAudio away from the lamp speaker codec so hal can own it.
SUBSYSTEM=="sound", ATTR{id}=="sndi2s4", ENV{PULSE_IGNORE}="1"
SUBSYSTEM=="sound", ATTR{id}=="wm8960soundcard", ENV{PULSE_IGNORE}="1"
UDEV_EOF
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=sound 2>/dev/null || true
  fi
  if ! command -v uv &>/dev/null; then
    echo "[stage] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi

  # Clean stale lerobot distutils egg-info that blocks uv uninstall, then recreate venv
  find /root/.cache/uv -name 'lerobot.egg-info' -type d 2>/dev/null | xargs rm -rf
  rm -rf "$HAL_DIR/.venv"

  # uv sync downloads Python 3.12 standalone (includes Python.h) + all deps
  cd "$HAL_DIR"
  uv sync --python 3.12 --extra hardware --extra aec --extra pipecat
  cd /

  # Patch webrtcvad: replace pkg_resources import (removed in Python 3.12+)
  WEBRTCVAD_PY=$(find "$HAL_DIR/.venv" -name "webrtcvad.py" -path "*/site-packages/*" 2>/dev/null | head -1)
  if [ -n "$WEBRTCVAD_PY" ] && grep -q "import pkg_resources" "$WEBRTCVAD_PY" 2>/dev/null; then
    echo "[stage] Patching webrtcvad for Python 3.12+ (pkg_resources removal)"
    cat > "$WEBRTCVAD_PY" <<'WEBRTCVAD_EOF'
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

  # Write default .env (production mode). Idempotent — only adds the line if absent.
  touch "$HAL_DIR/.env"
  grep -q "^HAL_MODE=" "$HAL_DIR/.env" \
    || echo "HAL_MODE=production" >> "$HAL_DIR/.env"
  grep -q "^DEVICE_TYPE=" "$HAL_DIR/.env" \
    || echo "DEVICE_TYPE=$DEVICE_TYPE" >> "$HAL_DIR/.env"
  grep -q "^DEVICES_DIR=" "$HAL_DIR/.env" \
    || echo "DEVICES_DIR=$DEVICES_DIR" >> "$HAL_DIR/.env"

  cat >/etc/systemd/system/hal.service <<EOF
[Unit]
Description=HAL Hardware Runtime
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$HAL_DIR
EnvironmentFile=$HAL_DIR/.env
Environment="PYTHONPATH=/opt"
# Anonymous PulseAudio socket — see /etc/pulse/default.pa. Lets root reach the
# desktop user's PulseAudio so the Bluetooth headset routing works.
Environment="PULSE_SERVER=unix:/tmp/pulse-anon-${DEVICE_TYPE}"
# --timeout-graceful-shutdown: without it uvicorn waits forever for open
# connections (an SSE/MJPEG stream holds SIGTERM until systemd's 90s SIGKILL).
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
TimeoutStopSec=30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable hal
  systemctl restart hal
}

# ----------------------------------------------------------
# Stage 1c: Claude Desktop Buddy (BLE plugin, optional)
# ----------------------------------------------------------
stage_buddy() {
  echo "[stage] Install Claude Desktop Buddy"

  if [ -z "$BUDDY_URL" ]; then
    echo "[stage] WARN: No claude-desktop-buddy URL in OTA metadata, skipping"
    return
  fi

  # Ensure Bluetooth is available
  apt install -y bluez 2>/dev/null || true
  bluetoothctl power on 2>/dev/null || true

  BUDDY_DIR="/opt/claude-desktop-buddy"
  mkdir -p "$BUDDY_DIR"

  retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o /tmp/buddy.zip \"$BUDDY_URL\"" 5
  unzip -o -q /tmp/buddy.zip -d /tmp/buddy-extract
  rm -f /tmp/buddy.zip

  # Binary
  if [ -f /tmp/buddy-extract/buddy-plugin ]; then
    cp -f /tmp/buddy-extract/buddy-plugin "$BUDDY_DIR/buddy-plugin"
    chmod +x "$BUDDY_DIR/buddy-plugin"
  fi

  # Config (don't overwrite existing)
  if [ ! -f /root/config/buddy.json ] && [ -f /tmp/buddy-extract/config/buddy.json ]; then
    mkdir -p /root/config
    cp -f /tmp/buddy-extract/config/buddy.json /root/config/buddy.json
  fi

  # Version file
  echo "$BUDDY_VERSION" > "$BUDDY_DIR/VERSION_BUDDY"

  rm -rf /tmp/buddy-extract

  cat >/etc/systemd/system/claude-desktop-buddy.service <<EOF
[Unit]
Description=Claude Desktop Buddy (BLE)
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=$BUDDY_DIR
ExecStart=$BUDDY_DIR/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-desktop-buddy

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable claude-desktop-buddy
  # Don't start yet — starts on reboot
}

# ----------------------------------------------------------
# Stage 1b: OpenClaw (CLI + gateway service; runs as root for full system access) - TODO: remove this
# ----------------------------------------------------------
stage_openclaw() {
  echo "[stage] Install OpenClaw"
  OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.6.10}"
  retry "npm install -g openclaw@${OPENCLAW_VERSION}" 5
  openclaw --version || true

  # OpenClaw state root for root-run service (under root's home).
  # Must match the dot-prefixed path used everywhere else (lamp config default,
  # migrate-openclaw-path.sh, stage_backend migration). Mismatch causes
  # OpenClaw WS to close 1008 / token_mismatch.
  OPENCLAW_HOME="${OPENCLAW_HOME:-/root/.openclaw}"
  mkdir -p \
    "$OPENCLAW_HOME" \
    "$OPENCLAW_HOME/workspace" \
    "$OPENCLAW_HOME/agents/main/agent" \
    "$OPENCLAW_HOME/agents/main/sessions" \
    "$OPENCLAW_HOME/credentials" \
    "$OPENCLAW_HOME/.cache" \
    "$OPENCLAW_HOME/.config" \
    "$OPENCLAW_HOME/.local/share" \
    /var/log/openclaw
  for p in \
    "$OPENCLAW_HOME" \
    "$OPENCLAW_HOME/workspace" \
    "$OPENCLAW_HOME/agents" \
    "$OPENCLAW_HOME/agents/main" \
    "$OPENCLAW_HOME/agents/main/agent" \
    "$OPENCLAW_HOME/agents/main/sessions" \
    "$OPENCLAW_HOME/credentials" \
    "$OPENCLAW_HOME/.cache" \
    "$OPENCLAW_HOME/.config" \
    "$OPENCLAW_HOME/.local" \
    "$OPENCLAW_HOME/.local/share"; do
    chmod 700 "$p" 2>/dev/null || true
  done

  if [ -z "${GATEWAY_TOKEN:-}" ]; then
    if command -v openssl >/dev/null 2>&1; then
      GATEWAY_TOKEN="$(openssl rand -hex 24)"
    else
      GATEWAY_TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
  fi

  # Seed a minimal valid config so gateway can boot cleanly on first run.
  if [ ! -f "$OPENCLAW_HOME/openclaw.json" ]; then
    cat >"$OPENCLAW_HOME/openclaw.json" <<EOF
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
      "token": "$GATEWAY_TOKEN"
    },
    "controlUi": {
      "allowedOrigins": ["http://127.0.0.1", "http://localhost"],
      "allowInsecureAuth": false
    }
  }
}
EOF
    chmod 600 "$OPENCLAW_HOME/openclaw.json"
  fi

  CHROME_PATH=$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || true)
  : "${CHROME_PATH:=/usr/bin/chromium}"
  OPENCLAW_BIN=$(command -v openclaw)
  if [ -z "$OPENCLAW_BIN" ]; then
    echo "ERROR: openclaw binary not found after npm install"
    exit 1
  fi
  cat >/etc/systemd/system/openclaw.service <<EOF
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$OPENCLAW_HOME
Environment="OPENCLAW_HOME=$OPENCLAW_HOME"
Environment="OPENCLAW_STATE_DIR=$OPENCLAW_HOME"
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=$OPENCLAW_HOME/.cache"
Environment="XDG_CONFIG_HOME=$OPENCLAW_HOME/.config"
Environment="XDG_DATA_HOME=$OPENCLAW_HOME/.local/share"
Environment="PUPPETEER_EXECUTABLE_PATH=$CHROME_PATH"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
Environment="CHROME_BIN=$CHROME_PATH"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x800x24" $OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  # Download skills from GCS into workspace/skills. Derive the base from the OTA
  # metadata URL (single source of truth) — skills live at <base>/skills, where
  # <base> is the metadata URL minus "/ota/metadata.json". No hardcoded URL.
  #
  # Capability gate: a skill that needs a hardware capability (servo/light/...)
  # is only seeded when this device's ROBOT.md declares that capability. Skills
  # with no capability (platform/logic skills) are always seeded. This mirrors
  # the authoritative gate in os-server (system/skills.Supported); both read
  # ROBOT.md so a reduced device never ships skills it can't run. Fail-open:
  # if ROBOT.md declares no capabilities, legacy skills are seeded. Optional
  # environment sensing still requires its explicit capability.
  # os-server re-syncs + prunes on boot, so this is only the first-boot seed.
  SKILLS_GCS_PREFIX="${OTA_METADATA_URL%/ota/metadata.json}/skills"
  # ---8<--- GENERATED by scripts/skills/gen_catalog.py — edit skills/<name>/skill.json
  SKILLS_CATALOG="audio camera claude-buddy computer-use connectors display emotion environment face-enroll faq guard habit harness-use input-branching led-control mood music music-suggestion scene sensing sensing-track servo-control servo-tracking skill-creator speaker-recognizer user-emotion-detection voice wellbeing"
  # Echo the capabilities a skill requires (space-separated, ANY-OF: the skill
  # is kept when the device declares at least one). Empty = platform skill.
  skill_caps() {
    case "$1" in
      audio) echo audio ;;
      camera) echo vision ;;
      claude-buddy) echo audio ;;
      computer-use) echo companion ;;
      display) echo display ;;
      emotion) echo expression ;;
      environment) echo environment ;;
      face-enroll) echo presence ;;
      guard) echo presence ;;
      harness-use) echo companion ;;
      led-control) echo light ;;
      music) echo media ;;
      scene) echo light ;;
      sensing) echo sensing ;;
      sensing-track) echo sensing ;;
      servo-control) echo motion ;;
      servo-tracking) echo motion ;;
      speaker-recognizer) echo audio ;;
      user-emotion-detection) echo "audio presence" ;;
      voice) echo audio ;;
      *) echo "" ;;
    esac
  }
  # ---8<--- END GENERATED
  # Capability keys declared in this device's ROBOT.md (one per line).
  # ROBOT.md is canonical; DEVICE.md is the name it shipped under, still read
  # because device profiles already on disk carry it.
  DEVICE_MD="$DEVICES_DIR/$DEVICE_TYPE/ROBOT.md"
  [ -f "$DEVICE_MD" ] || DEVICE_MD="$DEVICES_DIR/$DEVICE_TYPE/DEVICE.md"
  DEVICE_CAPS="$(awk '
    /^capabilities:/ {f=1; next}
    f && /^[A-Za-z]/ {exit}
    f && /^[[:space:]]+[A-Za-z_]+:/ { s=$0; sub(/^[[:space:]]+/,"",s); sub(/:.*/,"",s); print s }
  ' "$DEVICE_MD" 2>/dev/null)"
  mkdir -p "$OPENCLAW_HOME/workspace/skills"
  for skill_name in $SKILLS_CATALOG; do
    caps_required="$(skill_caps "$skill_name")"
    if [ "$skill_name" = environment ] && ! echo "$DEVICE_CAPS" | grep -qx environment; then
      echo "[stage] skill environment requires explicit environment capability — skipping"
      continue
    fi
    if [ -n "$caps_required" ] && [ -n "$DEVICE_CAPS" ]; then
      cap_matched=""
      for c in $caps_required; do
        if echo "$DEVICE_CAPS" | grep -qx "$c"; then cap_matched=1; break; fi
      done
      if [ -z "$cap_matched" ]; then
        echo "[stage] skill $skill_name needs one of '$caps_required' not declared in ROBOT.md — skipping"
        continue
      fi
    fi
    skill_dir="$OPENCLAW_HOME/workspace/skills/$skill_name"
    mkdir -p "$skill_dir"
    echo "[stage] Downloading skill: $skill_name"
    curl -fsSL -H "Cache-Control: no-cache" -o "$skill_dir/SKILL.md" \
      "$SKILLS_GCS_PREFIX/$skill_name/SKILL.md" 2>/dev/null || echo "[stage] WARN: failed to download skill $skill_name"
  done

  systemctl daemon-reload
  systemctl enable openclaw
  systemctl restart openclaw
  sleep 3
  if ! systemctl is-active --quiet openclaw; then
    echo "ERROR: openclaw service is not active after start"
    systemctl status openclaw --no-pager || true
    journalctl -u openclaw -n 80 --no-pager || true
    exit 1
  fi

  # Install official external plugins. Must run after openclaw service is up
  # because `openclaw plugins install` talks to the local gateway to register
  # the plugin. Non-fatal: missing plugin only disables that channel, it does
  # not break the gateway or other channels.
  echo "[stage] Installing openclaw external plugins"
  export PATH="$(npm prefix -g)/bin:$PATH"
  openclaw plugins install @openclaw/discord@${OPENCLAW_VERSION} --force 2>&1 || echo "[stage] WARN: discord plugin install failed (non-fatal)"
  openclaw plugins install @openclaw/slack@${OPENCLAW_VERSION} --force 2>&1 || echo "[stage] WARN: slack plugin install failed (non-fatal)"
}

# ----------------------------------------------------------
# Stage 2: nginx (setup web + API proxy)
# ----------------------------------------------------------
stage_nginx() {
  echo "[stage] Setup nginx (setup web + API proxy)"

  rm -f /etc/nginx/sites-enabled/default
  mkdir -p /usr/share/nginx/html/setup
  chmod 755 /usr/share/nginx/html/setup

  retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o /tmp/setup.zip \"$WEB_URL\"" 5
  unzip -o -q /tmp/setup.zip -d /usr/share/nginx/html/setup
  rm -f /tmp/setup.zip

  cat >/etc/nginx/conf.d/${DEVICE_TYPE}.conf <<EOF
upstream backend  { server 127.0.0.1:5000; }
upstream hal   { server 127.0.0.1:5001; }
upstream openclaw { server 127.0.0.1:18789; }

server {
  listen 80 default_server;

  root /usr/share/nginx/html/setup;
  index index.html;

  # Monitor chat attaches files as base64 inside JSON (up to 10 MB raw → ~13 MB
  # after base64). Default nginx limit is 1 MB, which 413s any non-trivial
  # attachment. 20 MB leaves headroom for future bumps to the client-side cap.
  client_max_body_size 20M;

  # Security headers — applied to every response so the web monitor (a
  # privileged device-control UI) is robust against clickjacking and
  # MIME-sniffing, and any future XSS bug has a tight blast radius.
  #
  # 'unsafe-inline' on style-src is intentional: the React app uses
  # style={{ ... }} props heavily. script-src stays strict 'self' to keep
  # XSS contained; any future inline <script> needs a nonce or rewrite.
  # SAMEORIGIN (not DENY) so the Monitor page can embed in-house iframes
  # (Swagger API docs at /api/hardware/docs, gateway config at /gw-config).
  # External sites still can't frame the device — CSP frame-ancestors 'self'
  # mirrors this, and modern browsers prefer the CSP value over XFO.
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  # Strict CSP. HAL self-hosts Swagger UI assets under /static/ (served
  # via the Lamp /api/hardware/* proxy) so no CDN whitelist or
  # `'unsafe-inline'` is needed for the in-iframe docs to render. React app
  # 'unsafe-inline' stays only on style-src for its inline style props.
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  # No-cache the SPA shell so companion-app webviews (aggressive caching)
  # pick up new UI builds without needing manual cache-clear. Assets under
  # /assets/ are hash-named per build so they can (and should) stay cached.
  location = /index.html {
    add_header Cache-Control "no-cache, no-store, must-revalidate" always;
    expires 0;
  }
  location = / {
    add_header Cache-Control "no-cache, no-store, must-revalidate" always;
    expires 0;
    try_files /index.html =404;
  }

  location / {
    try_files \$uri /index.html;
  }

  # Interactive shell WebSocket (xterm.js PTY). Must come BEFORE the generic
  # /api/ block so the more-specific match wins. Needs HTTP/1.1 + Upgrade
  # forwarding and a long read timeout (sessions stay open while idle).
  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  # Autonomous Buddy (macOS companion) persistent WebSocket. Same Upgrade + long-
  # timeout requirements as /api/system/shell. Must come BEFORE the generic
  # /api/ block so the exact match wins.
  location = /api/buddy/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  # Remote code execution endpoint — local callers only (OpenClaw agent on Pi).
  # Must come BEFORE the generic /api/ block so the exact match wins.
  location = /api/system/exec {
    allow 127.0.0.1;
    allow ::1;
    deny all;

    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }

  # Top-level openapi.json proxied to Lamp backend so the in-iframe Swagger
  # UI (loaded via /api/hardware/docs) can fetch its spec at the absolute
  # path FastAPI hardcodes. Lamp adminAuthMiddleware gates the cookie/Bearer.
  location = /openapi.json {
    proxy_pass http://backend;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
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
    # Hardware endpoints can be long-running (record-enroll records up to
    # duration_sec seconds then calls the embedding API). Default 60s would
    # 504 even on a 15s enroll if the embedding round-trip is slow.
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
  }

  # Exact /gw match for WebSocket (WS doesn't follow 301 redirect to /gw/).
  # X-Real-IP / X-Forwarded-For are intentionally NOT set: OpenClaw 5.2's
  # device-identity guard treats any forwarded header as "non-local client"
  # and rejects the Control UI handshake even with allowInsecureAuth=true.
  # Without these headers, OpenClaw sees the loopback peer (nginx) and
  # accepts the request as local.
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

  # Return 204 so OS does not detect captive portal (no auto-open browser)
  location = /generate_204 { return 204; }
  location = /hotspot-detect.html { return 204; }
  location = /ncsi.txt { return 204; }
  location = /connecttest.txt { return 204; }
}
EOF

  nginx -t
  systemctl enable nginx
  systemctl restart nginx
}

# ----------------------------------------------------------
# Stage 3: AP setup (hostapd + dnsmasq)
# ----------------------------------------------------------
stage_ap() {
  echo "[stage] Setup WiFi AP"

  # Pi 5: device-tree serial; Pi 4: cpuinfo Serial.
  # Non-Pi boards (OrangePi 4 Pro etc.) lack both — fall back to the ethernet
  # MAC so the AP SSID still gets a stable per-device suffix. Keep Pi paths
  # first so existing Pi devices keep their current SSID.
  SERIAL=$(tr -d '\0' </proc/device-tree/serial-number 2>/dev/null || true)
  if [ -z "$SERIAL" ]; then
    SERIAL=$(awk '/^Serial/ {print $3}' /proc/cpuinfo 2>/dev/null || true)
  fi
  if [ -z "$SERIAL" ]; then
    for iface in eth0 end0; do
      mac=$(cat "/sys/class/net/$iface/address" 2>/dev/null | tr -d ':' || true)
      if [ -n "$mac" ] && [ "$mac" != "000000000000" ]; then
        SERIAL=$mac
        break
      fi
    done
  fi
  SUFFIX=${SERIAL: -4}
  SUFFIX_LC=$(echo "$SUFFIX" | tr '[:upper:]' '[:lower:]')

  # Network identity (AP SSID + mDNS hostname) is device-type-driven so one OS
  # image serves N device classes: `<device_type>-<suffix>`. DEVICE_TYPE is the
  # immutable hardware identity (env → config.json → "lamp"), the same source Go's
  # GetDeviceMac and HAL's _resolve_device_type read. Lowercase throughout — avahi
  # publishes the hostname verbatim and `.local` URLs in the wild aren't
  # case-normalized, and the web UI derives the redirect target from the lowercase
  # `mac` field (GetDeviceMac).
  AP_SSID="${DEVICE_TYPE}-${SUFFIX_LC}"
  echo "[stage] AP SSID = $AP_SSID (serial=$SERIAL)"

  DEVICE_HOSTNAME="${DEVICE_TYPE}-${SUFFIX_LC}"
  hostnamectl set-hostname "$DEVICE_HOSTNAME" 2>/dev/null || hostname "$DEVICE_HOSTNAME"
  # Replace 127.0.1.1 line if present, otherwise append. /etc/hosts is required
  # for sudo/getent to resolve the hostname locally.
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $DEVICE_HOSTNAME/" /etc/hosts
  else
    echo "127.0.1.1 $DEVICE_HOSTNAME" >> /etc/hosts
  fi
  # Advertise an _autonomous._tcp mDNS service so the Autonomous Buddy (macOS)
  # auto-discovers this device instead of the user typing <hostname>.local by
  # hand. Static + device-agnostic: avahi's %h wildcard resolves to the running
  # hostname (<device_type>-<suffix>), so one file serves every device class.
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
  systemctl enable avahi-daemon 2>/dev/null || true
  systemctl restart avahi-daemon 2>/dev/null || true
  echo "[stage] mDNS hostname = $DEVICE_HOSTNAME.local"
  # Sanity check: confirm avahi actually publishes this name locally. A
  # warning here usually means the daemon failed to start (missing dbus,
  # masked service) or another device on the bench already claimed the
  # name (avahi would have renamed ours to ${DEVICE_HOSTNAME}-2). Two devices
  # with identical last-4 serial chars on the same LAN is rare (1/65536)
  # but possible — if it happens, the FE's redirect will hit the wrong
  # device, and we'd need to bump the suffix length here and in
  # system/device/hardware.go.
  sleep 1
  if command -v avahi-resolve-host-name >/dev/null 2>&1; then
    if ! avahi-resolve-host-name -4 "${DEVICE_HOSTNAME}.local" >/dev/null 2>&1; then
      echo "[stage] WARNING: ${DEVICE_HOSTNAME}.local not resolvable via mDNS yet (avahi may need a moment, or another device claimed the name)"
    fi
  fi

  # Ignore Pi Imager WiFi credentials baked into the image.
  if [ -f /etc/wpa_supplicant/wpa_supplicant.conf ]; then
    mv /etc/wpa_supplicant/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf.bak 2>/dev/null || true
  fi

  # Many Pi images keep wlan0 down until WiFi country is set. Create minimal config with country
  # so the system enables wlan0; connect-wifi and hostapd use the same country.
  COUNTRY_CODE="${COUNTRY_CODE:-US}"
  mkdir -p /etc/wpa_supplicant
  if [ ! -f /etc/wpa_supplicant/wpa_supplicant-wlan0.conf ]; then
    cat >/etc/wpa_supplicant/wpa_supplicant-wlan0.conf <<EOF
country=$COUNTRY_CODE
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
EOF
    chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf 2>/dev/null || true
    echo "[stage] Created /etc/wpa_supplicant/wpa_supplicant-wlan0.conf with country=$COUNTRY_CODE so wlan0 can appear"
  fi
  
  # Ensure wpa_supplicant@wlan0 uses the intended config file.
  mkdir -p /etc/systemd/system/wpa_supplicant@wlan0.service.d
  cat >/etc/systemd/system/wpa_supplicant@wlan0.service.d/override.conf <<'WPADROP'
[Service]
ExecStart=
ExecStart=/sbin/wpa_supplicant -c /etc/wpa_supplicant/wpa_supplicant-wlan0.conf -i wlan0 -D nl80211,wext
Restart=on-failure
RestartSec=5
WPADROP

  if [ "$AP_BAND" = "5" ]; then
    HWMODE=a
    CHANNEL="${AP_CHANNEL:-36}"
    cat >/etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=$AP_SSID
hw_mode=$HWMODE
channel=$CHANNEL
country_code=$COUNTRY_CODE
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
  else
    HWMODE=g
    CHANNEL="${AP_CHANNEL:-6}"
    cat >/etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=$AP_SSID
hw_mode=$HWMODE
channel=$CHANNEL
country_code=$COUNTRY_CODE
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
  fi
  echo "[stage] AP band=$AP_BAND channel=$CHANNEL"

  cat >/etc/default/hostapd <<EOF
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

  # dnsmasq: use .d drop-in so we don't break system config; bind range to wlan0 explicitly
  mkdir -p /etc/dnsmasq.d
  cat >/etc/dnsmasq.d/99-${DEVICE_TYPE}.conf <<EOF
interface=wlan0
bind-interfaces
dhcp-range=wlan0,192.168.100.50,192.168.100.150,255.255.255.0,24h
address=/#/192.168.100.1
domain-needed
bogus-priv
no-resolv
EOF
  # Remove any conflicting global interface in main config (leave rest intact)
  if [ -f /etc/dnsmasq.conf ]; then
    sed -i 's|^interface=wlan0|#interface=wlan0  # use dnsmasq.d/99-${DEVICE_TYPE}.conf|' /etc/dnsmasq.conf 2>/dev/null || true
  fi

  # dhcpcd: remove wlan0 block (including when it's at end-of-file with no trailing blank line)
  sed -i '/^interface wlan0$/,/^$\|^interface /{ /^interface [^w]/!d; }' /etc/dhcpcd.conf
  # Remove any leftover lines from previous runs
  sed -i '/^static ip_address=192\.168\.100\.1/d' /etc/dhcpcd.conf
  sed -i '/^nohook wpa_supplicant$/d' /etc/dhcpcd.conf
  cat >>/etc/dhcpcd.conf <<EOF

interface wlan0
static ip_address=192.168.100.1/24
nohook wpa_supplicant
EOF

  # AP mode scripts
  mkdir -p /usr/local/bin

  cat >/usr/local/bin/device-ap-mode <<'EOF'
#!/bin/bash
set -e

# Device class — drives the dnsmasq drop-in filename (99-<device_type>.conf).
# Resolved at runtime from the env baked at provisioning (always present).
DEVICE_TYPE="$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"

echo "Switching to AP mode..."

# Check required commands
for cmd in ip iw systemctl hostapd dnsmasq rfkill; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd"; exit 1; }
done

# Ensure WiFi is unblocked
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true

# Stop STA services
systemctl stop wpa_supplicant@wlan0 2>/dev/null || true
systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
systemctl mask wpa_supplicant@wlan0 2>/dev/null || true
killall wpa_supplicant 2>/dev/null || true

systemctl stop dhcpcd 2>/dev/null || true
systemctl disable dhcpcd 2>/dev/null || true

systemctl stop NetworkManager systemd-networkd 2>/dev/null || true

# Clear DHCP state
rm -f /var/lib/dhcpcd5/dhcpcd-wlan0 2>/dev/null || true
rm -f /var/lib/dhcpcd/dhcpcd-wlan0 2>/dev/null || true

# Set regulatory domain
REG=$(grep "^country_code=" /etc/hostapd/hostapd.conf 2>/dev/null | cut -d= -f2)
[ -z "$REG" ] && REG=US
iw reg set "$REG" 2>/dev/null || true

# Reset WiFi interface
ip link set wlan0 down
sleep 1

# switch to AP mode
iw dev wlan0 set type __ap
sleep 1

# Bring interface up
ip link set wlan0 up
sleep 1

# Assign static IP
ip addr flush dev wlan0
ip addr add 192.168.100.1/24 dev wlan0

# Clear stale DHCP nameserver from previous STA session — otherwise
# /etc/resolv.conf keeps pointing at the home router's DNS even though wlan0
# can no longer reach it. Best-effort: resolvconf may not be installed.
command -v resolvconf >/dev/null 2>&1 && resolvconf -d wlan0.dhcp 2>/dev/null || true

# Restore captive-portal DNS wildcard before starting dnsmasq — device-sta-mode
# strips this line so DNS resolves correctly in STA mode; must be re-added here
# so dnsmasq loads with the wildcard active on first start, not just after a
# second restart.
grep -q '^address=/#/' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || \
  echo 'address=/#/192.168.100.1' >> /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf

# Enable AP services — start nginx+dnsmasq first so web UI is ready before
# the SSID becomes visible, preventing a 404 on first connect.
systemctl unmask hostapd dnsmasq 2>/dev/null || true
systemctl enable hostapd dnsmasq

systemctl restart nginx 2>/dev/null || true
systemctl restart dnsmasq

systemctl restart hostapd
sleep 2

# Retry hostapd once if failed
if ! systemctl is-active --quiet hostapd; then
  echo "hostapd failed. Retrying..."
  systemctl restart hostapd
  sleep 2
fi

# If still failed → show debug
if ! systemctl is-active --quiet hostapd; then
  echo "ERROR: hostapd still not running"

  echo
  echo "Debug checks:"
  echo "rfkill status:"
  rfkill list || true

  echo
  echo "Regulatory domain:"
  iw reg get || true

  echo
  echo "wlan0 status:"
  ip addr show wlan0 || true

  echo
  echo "hostapd logs:"
  systemctl status hostapd --no-pager || true
  journalctl -u hostapd -n 50 --no-pager || true

  if [ -f /boot/firmware/config.txt ] && grep -q 'disable-wifi' /boot/firmware/config.txt 2>/dev/null; then
    echo
    echo "WiFi may be disabled in /boot/firmware/config.txt"
    echo "Remove dtoverlay=disable-wifi and reboot"
  fi

  exit 1
fi

echo "AP MODE ENABLED"
EOF

  chmod +x /usr/local/bin/device-ap-mode

  cat >/usr/local/bin/device-sta-mode <<'EOF'
#!/bin/bash
set -e

# Device class — drives the dnsmasq drop-in filename (99-<device_type>.conf).
# Resolved at runtime from the env baked at provisioning (always present).
DEVICE_TYPE="$(grep -E '^DEVICE_TYPE=' /opt/hal/.env 2>/dev/null | cut -d= -f2)"

echo "Switching to STA mode..."

# Check required commands
for cmd in ip iw systemctl rfkill; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing required command: $cmd"; exit 1; }
done

# Ensure WiFi is unblocked
rfkill unblock wlan 2>/dev/null || true
rfkill unblock wlan0 2>/dev/null || true

# Stop AP services
systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true

# Kill any leftover processes
killall hostapd 2>/dev/null || true
killall dnsmasq 2>/dev/null || true

# Reset interface
ip link set wlan0 down 2>/dev/null || true
sleep 1

# Ensure managed mode
iw dev wlan0 set type managed

ip link set wlan0 up
sleep 1

# Remove any AP static IP
ip addr flush dev wlan0

# Remove AP static IP config from dhcpcd if exists
sed -i '/static ip_address=192.168.100.1\/24/d' /etc/dhcpcd.conf
sed -i '/nohook wpa_supplicant/d' /etc/dhcpcd.conf

# Remove AP captive-portal DNS wildcard — redirects ALL queries to 192.168.100.1
# which breaks LLM/internet connectivity when switching to STA mode.
sed -i '/^address=\/#\//d' /etc/dnsmasq.d/99-${DEVICE_TYPE}.conf 2>/dev/null || true

# Enable STA services
systemctl unmask wpa_supplicant@wlan0 2>/dev/null || true
systemctl enable wpa_supplicant@wlan0
systemctl restart wpa_supplicant@wlan0

systemctl enable dhcpcd
systemctl restart dhcpcd

# Wait for DHCP
echo "Waiting for IP..."
sleep 5

if ip addr show wlan0 | grep -q "inet "; then
  IP=$(ip -4 addr show wlan0 | grep inet | awk '{print $2}')
  echo "Connected. IP address: $IP"
else
  echo "WARNING: wlan0 did not receive an IP address"
  echo "Check WiFi connection:"
  echo "  wpa_cli status"
  echo "  journalctl -u wpa_supplicant@wlan0 -n 50 --no-pager"
fi

# Re-announce mDNS on the new network so http://<device_type>-XXXX.local/ resolves
# from the user's computer once they reconnect to home Wi-Fi. Without this,
# avahi sometimes keeps stale records from the AP network and stays silent
# on the new subnet until the next service restart or reboot.
systemctl restart avahi-daemon 2>/dev/null || true

echo "STA MODE ENABLED"
EOF

  chmod +x /usr/local/bin/device-sta-mode

  # connect-wifi: write wpa_supplicant config then switch to STA (used by backend /api/network/setup)
  cat >/usr/local/bin/connect-wifi <<'CONNECTWIFI'
#!/bin/bash
set -e
WPA_CONF="${WPA_CONF:-/etc/wpa_supplicant/wpa_supplicant-wlan0.conf}"
COUNTRY="${COUNTRY:-US}"
[ "$(id -u)" -ne 0 ] && { echo "Run as root or with sudo."; exit 1; }
if [ $# -eq 0 ]; then read -r -p "SSID: " SSID; read -r -s -p "Password (empty=open): " PASS; echo ""; [ -z "$SSID" ] && exit 1
elif [ $# -eq 1 ]; then SSID="$1"; PASS=""
else SSID="$1"; PASS="$2"; fi
ssid_esc="${SSID//\\/\\\\}"; ssid_esc="${ssid_esc//\"/\\\"}"
psk_esc="${PASS//\\/\\\\}"; psk_esc="${psk_esc//\"/\\\"}"
[ -f "$WPA_CONF" ] && existing_country=$(grep -E '^country=' "$WPA_CONF" 2>/dev/null | head -1 | cut -d= -f2) && [ -n "$existing_country" ] && COUNTRY="$existing_country"
mkdir -p "$(dirname "$WPA_CONF")"
if [ -z "$PASS" ]; then
  net_block="network={
	ssid=\"${ssid_esc}\"
	key_mgmt=NONE
	scan_ssid=1
}"
else
  net_block="network={
	ssid=\"${ssid_esc}\"
	psk=\"${psk_esc}\"
	scan_ssid=1
}"
fi
cat >"$WPA_CONF" <<EOF
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
country=${COUNTRY}
fast_reauth=1
ap_scan=1
${net_block}
EOF
chmod 600 "$WPA_CONF"
/usr/local/bin/device-sta-mode
CONNECTWIFI
  chmod +x /usr/local/bin/connect-wifi

  # software-update: the on-device OTA updater. GENERATED — do not edit the body
  # here. The single source of truth is scripts/provision/software-update, which
  # scripts/release/upload-setup.sh inlines between the two markers below when it
  # publishes setup.sh to the CDN. The repo copy carries a placeholder that fails
  # loudly, so a setup.sh that skipped that step can never install a silently
  # broken updater onto a device.
  # >>> BEGIN software-update (generated)
  cat >/usr/local/bin/software-update <<'SOFTWAREUPDATE'
#!/bin/bash
echo "software-update: placeholder body — this setup.sh was published without inlining" >&2
echo "scripts/provision/software-update. Re-publish with: make upload-setup" >&2
exit 1
SOFTWAREUPDATE
  # <<< END software-update (generated)
  chmod +x /usr/local/bin/software-update

  # start in AP mode
  /usr/local/bin/device-ap-mode
}

# ----------------------------------------------------------
# Main
# ----------------------------------------------------------
ensure_root

# Install this device's profile (ROBOT.md + SOUL.md) into DEVICES_DIR/<type>.
# Per-device: downloads ONLY this device_type's artifact (devices.<type> in OTA
# metadata). Absent → skip (agent keeps the gateway's default soul; HAL mounts
# all routes). Read by os-server (soul) and HAL (capability mounting).
stage_devices() {
  echo "[stage] Install device profile ($DEVICE_TYPE)"
  local dest="$DEVICES_DIR/$DEVICE_TYPE"
  mkdir -p "$dest"
  # Device profile is required — one install = one device type, and the device
  # is useless without its ROBOT.md/SOUL.md/SAFETY.md. set -e is suspended
  # inside run_stage's `if stage_devices`, so guard each step explicitly and
  # return non-zero so the stage is recorded as FAILED (not silently OK).
  if [ -z "${DEVICES_URL:-}" ]; then
    echo "[stage] ERROR: no devices.$DEVICE_TYPE url in OTA metadata — device profile required. Run 'make upload-device $DEVICE_TYPE'." >&2
    return 1
  fi
  retry "curl -fsSL -H \"Cache-Control: no-cache\" -H \"Pragma: no-cache\" -o /tmp/device.zip \"$DEVICES_URL\"" 5 \
    || { echo "[stage] ERROR: failed to download device profile for $DEVICE_TYPE from $DEVICES_URL" >&2; return 1; }
  [ -z "$DEVICES_SHA256" ] || echo "$DEVICES_SHA256  /tmp/device.zip" | sha256sum -c - >/dev/null \
    || { echo "[stage] ERROR: device profile SHA-256 mismatch" >&2; return 1; }
  unzip -o -q /tmp/device.zip -d "$dest" \
    || { echo "[stage] ERROR: failed to extract device profile for $DEVICE_TYPE" >&2; return 1; }
  rm -f /tmp/device.zip
  HARDWARE_PROFILE=""
  [ ! -e /etc/autonomous/hardware-profile ] || HARDWARE_PROFILE=$(sed 's/^[[:space:]]*//;s/[[:space:]]*$//' /etc/autonomous/hardware-profile) \
    || { echo "Cannot read hardware profile selection" >&2; return 1; }
  if [ -n "$HARDWARE_PROFILE" ] && [ "$HARDWARE_PROFILE" != standard ]; then
    [ -f "$dest/apply-overrides.py" ] \
      || { echo "[stage] ERROR: Hardware overrides require an override-capable device package" >&2; return 1; }
    python3 "$dest/apply-overrides.py" --profile "$dest" --root / \
      || { echo "[stage] ERROR: Hardware override configuration failed" >&2; return 1; }
    cp -a "$dest/rootfs/." / \
      || { echo "[stage] ERROR: Hardware overlay failed" >&2; return 1; }
    # stage_hal ran before this overlay; reload the hardware configuration.
    if systemctl is-active --quiet hal || systemctl is-enabled --quiet hal; then
      systemctl restart hal \
        || { echo "[stage] ERROR: HAL restart after hardware overlay failed" >&2; return 1; }
    fi
  fi
  echo "[stage] Device profile '$DEVICE_TYPE' installed at $dest"
}

# Stop os-server if running from a previous setup — it switches to AP mode when unconfigured, killing internet.
systemctl stop os-server.service 2>/dev/null || true
systemctl disable os-server.service 2>/dev/null || true

run_stage stage_locale
run_stage stage_prerequisites
run_stage stage_rpi5_wifi_stability
run_stage stage_enable_spi
run_stage stage_ota_metadata
run_stage stage_backend
run_stage stage_hal
run_stage stage_devices
run_stage stage_buddy
run_stage stage_openclaw
run_stage stage_nginx
run_stage stage_ap

# Disable global wpa_supplicant; only wpa_supplicant@wlan0 is used in STA mode
systemctl stop wpa_supplicant.service 2>/dev/null || true
systemctl disable wpa_supplicant.service 2>/dev/null || true
systemctl mask wpa_supplicant.service 2>/dev/null || true

echo ""
echo "======================================"
echo "Setup complete!"
echo "AP SSID: ${DEVICE_TYPE}-xxxx (actual: ${AP_SSID:-unknown — stage_ap may have failed})"
echo "Setup page: http://192.168.100.1 (AP) — or http://${DEVICE_HOSTNAME:-${DEVICE_TYPE}-xxxx}.local once on home Wi-Fi"
echo "Backends: systemctl status bootstrap os-server hal claude-desktop-buddy"
echo "Updates:  software-update <bootstrap|os-server|openclaw|hal|claude-desktop-buddy|web>"
if [ -n "$FAILED_STAGES" ]; then
  echo ""
  echo "WARNING: the following stages FAILED:$FAILED_STAGES"
  echo "Re-run setup.sh after fixing, or run individual stages manually."
fi
echo "======================================"

echo ""
echo "Rebooting in 10 seconds so SPI and WiFi firmware changes take effect..."
sleep 10
reboot
