stage_prerequisites() {
  echo "[stage] Install system packages"
  apt update
  apt install -y \
    hostapd dnsmasq nginx unzip curl wpasupplicant dhcpcd iproute2 iptables \
    git dash xvfb chromium chromium-sandbox || true
  systemctl stop hostapd dnsmasq nginx 2>/dev/null || true
  systemctl unmask hostapd dnsmasq 2>/dev/null || true
  if ! command -v node &>/dev/null || ! node -v 2>/dev/null | grep -qE '^v(2[2-9]|[3-9][0-9])'; then
    echo "[stage] Install Node.js 22 (NodeSource)"
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt install -y nodejs
  fi
  # Keep wpa_supplicant running so STA stays connected during setup.
}

stage_openclaw() {
  # OPENCLAW_VERSION pins the first install; the watcher reconciles to the OTA target later.
  OPENCLAW_VERSION="${OPENCLAW_VERSION:-latest}"
  echo "[stage] Install OpenClaw (npm registry, version=${OPENCLAW_VERSION})"
  npm install -g "openclaw@${OPENCLAW_VERSION}"
  openclaw --version || true

  # Must match the state dir os-server writes the token to, or WS closes 1008 token_mismatch.
  mkdir -p /root/.openclaw /var/log/openclaw

  CHROME_PATH=$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || true)
  : "${CHROME_PATH:=/usr/bin/chromium}"
  OPENCLAW_BIN=$(command -v openclaw)
  cat >/etc/systemd/system/openclaw.service <<EOF
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
Environment="PUPPETEER_EXECUTABLE_PATH=$CHROME_PATH"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
Environment="CHROME_BIN=$CHROME_PATH"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x800x24" $OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=append:/var/log/openclaw/output.log
StandardError=append:/var/log/openclaw/error.log

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable openclaw
  systemctl start openclaw
}

stage_prerequisites
stage_openclaw