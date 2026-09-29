#!/usr/bin/env bash
# recon.sh — read-only first-boot probe of a Reachy Mini (--audio-test adds a 3s loopback that makes sound).
# Usage: bash recon.sh [--audio-test]

# No `set -e`: probes are best-effort; a missing tool must not abort the run.

AUDIO_TEST=0
[ "${1:-}" = "--audio-test" ] && AUDIO_TEST=1

section() {
  printf '\n=============================================================\n'
  printf '## %s\n' "$1"
  printf '=============================================================\n'
}

step() { printf '\n--- %s ---\n' "$1"; }

# Run a command, labelling missing binaries instead of erroring out.
run() {
  if command -v "${1%% *}" >/dev/null 2>&1 || [ "${1:0:1}" = "/" ] || [ "${1:0:4}" = "cat " ] || [ "${1:0:3}" = "ls " ]; then
    eval "$@" 2>&1 || printf '(command failed: %s)\n' "$*"
  else
    printf '(not installed: %s)\n' "${1%% *}"
  fi
}

printf 'Reachy Mini recon — read-only probe\n'
printf 'host: %s   date: %s\n' "$(hostname 2>/dev/null)" "$(date 2>/dev/null)"
[ "$AUDIO_TEST" = "1" ] && printf 'audio loopback test: ENABLED (will play 3s of sound)\n'

section "1.1 OS & Kernel"
run "cat /etc/os-release"
step "kernel / arch"
run "uname -a"
step "boot config path"
if [ -f /boot/firmware/config.txt ]; then
  printf 'config at: /boot/firmware/config.txt\n'
  run "cat /boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
  printf 'config at: /boot/config.txt\n'
  run "cat /boot/config.txt"
else
  printf '(no /boot/firmware/config.txt or /boot/config.txt)\n'
fi
step "disk (eMMC)"
run "df -h"
step "RAM"
run "free -h"

section "1.2 Network Stack  [decides setup.sh AP/STA path]"
step "which stack is active?"
for svc in NetworkManager dhcpcd wpa_supplicant systemd-networkd; do
  printf '%-18s: %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
done
step "nmcli device status"
run "nmcli device status"
step "wlan0 address"
run "ip addr show wlan0"
step "wpa_supplicant configs"
run "ls -la /etc/wpa_supplicant/ 2>/dev/null; cat /etc/wpa_supplicant/*.conf 2>/dev/null"
step "dhcpcd.conf"
run "cat /etc/dhcpcd.conf 2>/dev/null"
step "NetworkManager connections"
run "ls -la /etc/NetworkManager/system-connections/ 2>/dev/null"
printf '\n>>> DECISION: NetworkManager active  -> setup.sh needs an nmcli-based AP/STA branch\n'
printf '    dhcpcd active (NM inactive)         -> current setup.sh flow works as-is\n'

section "1.3 Pollen Daemon  [never stop/restart this]"
step "reachy/pollen units"
run "systemctl list-units --all 2>/dev/null | grep -iE 'reachy|pollen'"
step "unit files on disk"
run "ls -la /etc/systemd/system/ 2>/dev/null | grep -iE 'reachy|pollen'; cat /etc/systemd/system/reachy* 2>/dev/null"
step "daemon HTTP surface (localhost:8000)"
run "curl -s -m 3 http://localhost:8000 | head -20"
run "curl -s -m 3 http://localhost:8000/api 2>/dev/null | head -20"
step "venvs"
run "ls -la /venvs/ 2>/dev/null; ls -la /restore/venvs/ 2>/dev/null"
step "reachy python packages"
run "pip list 2>/dev/null | grep -i reachy; pip3 list 2>/dev/null | grep -i reachy"

section "1.4 Audio  [-> .env HAL_AUDIO_*_ALSA, asound.conf]"
step "capture devices (mic array)"
run "arecord -l"
step "playback devices (speaker)"
run "aplay -l"
step "sound cards"
run "cat /proc/asound/cards"
step "existing asound.conf"
run "cat /etc/asound.conf 2>/dev/null"
if [ "$AUDIO_TEST" = "1" ]; then
  step "loopback test (record 3s @ 16k, play back)  [MAKES SOUND]"
  if arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/reachy_recon_test.wav 2>&1; then
    aplay /tmp/reachy_recon_test.wav 2>&1 || printf '(playback failed — try a different -D plughw card)\n'
    rm -f /tmp/reachy_recon_test.wav
  else
    printf '(capture failed on plughw:0,0 — pick the mic card from `arecord -l` above)\n'
  fi
else
  step "loopback test skipped (re-run with --audio-test to hear it)"
fi

section "1.5 Camera  [-> .env HAL_CAMERA_INDEX]"
step "v4l2 devices"
run "v4l2-ctl --list-devices"
step "/dev/video*"
run "ls -la /dev/video* 2>/dev/null"
step "video0 caps"
run "v4l2-ctl -d /dev/video0 --all 2>/dev/null | head -30"
step "libcamera (if used instead of V4L2)"
run "libcamera-hello --list-cameras 2>/dev/null"
printf '\n>>> NOTE: if libcamera (not V4L2) drives the camera, HAL OpenCV VideoCapture(index)\n'
printf '    may need a gstreamer pipeline or picamera2 — see first-boot-plan.md 2.4.\n'

section "1.6 Ports & Services  [our ports: HAL 5001, os-server 5000 (loopback), nginx 80]"
step "listening TCP ports"
run "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"
step "our ports free?"
for p in 5001 5000 80; do
  if ss -tln 2>/dev/null | grep -q ":$p " || netstat -tln 2>/dev/null | grep -q ":$p "; then
    printf 'port %-5s: IN USE  <-- conflict, inspect owner above\n' "$p"
  else
    printf 'port %-5s: free\n' "$p"
  fi
done
step "running services"
run "systemctl list-units --type=service --state=running 2>/dev/null"

section "1.7 System Deps  [pygobject/pycairo build for the reachy extra]"
step "cairo / gobject / pkg-config"
run "dpkg -l 2>/dev/null | grep -E 'libcairo2-dev|libgirepository|pkg-config' || echo '(none of the build deps installed — setup.sh must apt-install them)'"
step "python / uv / pip"
run "python3 --version"
run "which uv 2>/dev/null || echo '(uv not installed)'"
run "which pip3 2>/dev/null"

section "1.8 Bluetooth  [BLE recovery path]"
run "systemctl status bluetooth 2>/dev/null | head -8"
run "hciconfig -a 2>/dev/null"

# Read-only: arecord is expected to fail "busy" while the daemon holds media; never release here.
section "1.9 Media Ownership  [who holds camera + audio — decides HAL startup]"
step "camera holders"
run "fuser -v /dev/video0 /dev/video1 2>&1 | head -12"
step "ALSA holders"
run "fuser -v /dev/snd/* 2>&1 | head -16"
step "can a second process open the mic? (expected: busy while media is held)"
run "arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 1 /tmp/_recon_mic.wav 2>&1 | head -3"
rm -f /tmp/_recon_mic.wav 2>/dev/null
step "daemon media status"
run "curl -s --max-time 3 http://localhost:8000/api/media/status"
printf '\n'
step "daemon media handover endpoints (do NOT call them during recon)"
run "curl -s --max-time 3 http://localhost:8000/openapi.json | grep -o '/api/media/[a-z_]*' | sort -u"

section "SUMMARY — copy these into runtime.md / .env / setup.sh"
cat <<'SUMMARY'
Network stack  : NetworkManager | dhcpcd | systemd-networkd   (from 1.2)
Daemon service : ____________________                          (from 1.3)
Daemon port    : ____________________                          (from 1.3)
Mic ALSA       : plughw:__,__   channels:__                    (from 1.4)
Speaker ALSA   : plughw:__,__                                  (from 1.4)
Camera         : /dev/video__   V4L2|libcamera   maxres:_____  (from 1.5)
Ports free     : 5001 __  5000 __  80 __                       (from 1.6)
Cairo deps     : present | missing                             (from 1.7)
Media held by daemon : yes | no   release endpoint: yes | no   (from 1.9)
SUMMARY

printf '\nrecon complete.\n'
