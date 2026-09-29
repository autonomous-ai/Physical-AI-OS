#!/bin/bash
# Setup AP only: hostapd + dnsmasq, device-ap-mode, device-sta-mode, connect-wifi.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root"
    exit 1
  fi
}

AP_BAND="${AP_BAND:-2.4}"       # 2.4 or 5 (5 GHz for better throughput)
AP_CHANNEL="${AP_CHANNEL:-}"    # default: 6 (2.4 GHz) or 36 (5 GHz); override e.g. AP_CHANNEL=11 or 40

# Required; drives the <device_type>-<suffix> SSID.
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE must be set (e.g. DEVICE_TYPE=lamp) — no default}"

# Prerequisites
stage_prerequisites() {
  echo "[stage] Install AP packages"
  apt update
  apt install -y hostapd dnsmasq wpasupplicant dhcpcd iproute2 iw
  systemctl stop hostapd dnsmasq 2>/dev/null || true
  systemctl unmask hostapd dnsmasq 2>/dev/null || true
}

# Stage: Setup AP (hostapd + dnsmasq)
stage_ap() {
  echo "[stage] Setup WiFi AP"

  # Pi 5: prefer device-tree serial; fallback to cpuinfo
  SERIAL=$(tr -d '\0' </proc/device-tree/serial-number 2>/dev/null) || SERIAL=$(awk '/Serial/ {print $3}' /proc/cpuinfo)
  SUFFIX=${SERIAL: -4}
  SUFFIX_LC=$(echo "$SUFFIX" | tr '[:upper:]' '[:lower:]')
  AP_SSID="${DEVICE_TYPE}-${SUFFIX_LC}"
  echo "[stage] AP SSID = $AP_SSID"

  # Ignore Pi Imager WiFi credentials baked into the image.
  if [ -f /etc/wpa_supplicant/wpa_supplicant.conf ]; then
    mv /etc/wpa_supplicant/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf.bak 2>/dev/null || true
  fi

  # wlan0 stays down until the Wi-Fi country is set.
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
  if [ -f /etc/dnsmasq.conf ]; then
    sed -i 's|^interface=wlan0|#interface=wlan0  # use dnsmasq.d/99-${DEVICE_TYPE}.conf|' /etc/dnsmasq.conf 2>/dev/null || true
  fi

  # dhcpcd: remove only the wlan0 block so eth0/other blocks are preserved
  sed -i '/^interface wlan0$/,/^$/d' /etc/dhcpcd.conf
  cat >>/etc/dhcpcd.conf <<EOF

interface wlan0
static ip_address=192.168.100.1/24
nohook wpa_supplicant
EOF

  mkdir -p /usr/local/bin

  cat >/usr/local/bin/device-ap-mode <<'EOF'
#!/bin/bash
set -e

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

# Enable AP services
systemctl unmask hostapd dnsmasq 2>/dev/null || true
systemctl enable hostapd dnsmasq

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

# Restart DHCP server
systemctl restart dnsmasq

# Restart web service if using captive portal
systemctl restart nginx 2>/dev/null || true

echo "AP MODE ENABLED"
EOF

  chmod +x /usr/local/bin/device-ap-mode

  cat >/usr/local/bin/device-sta-mode <<'EOF'
#!/bin/bash
set -e

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

echo "STA MODE ENABLED"
EOF

  chmod +x /usr/local/bin/device-sta-mode

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
}

# Main
ensure_root
stage_prerequisites
stage_ap
