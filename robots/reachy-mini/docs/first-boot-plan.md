# Reachy Mini First-Boot Plan

Step-by-step plan for the first real-device session. Run once when the Wireless
unit arrives, then update `runtime.md`, `.env`, and `setup.sh` with the findings.

## Phase 1: SSH Recon (Read-Only)

> **DONE — 2026-07-29** on the first Wireless unit (`hardware_id e4a0ef5f04fafb94`).
> Every `Record` block below is filled with measured values, and the full result
> table lives in [`runtime.md`](runtime.md#first-boot-recon-measured-2026-07-29).
> Re-run this phase per new unit; the values are per-unit only where noted.

SSH in and collect system info. **Do not change anything yet.**

```bash
ssh pollen@reachy-mini.local   # password: root
```

`root` cannot SSH in directly (`Permission denied (publickey,password)`) — log in
as `pollen` and `sudo` from there.

**Shortcut:** [`../recon.sh`](../recon.sh) runs every command in this phase in one
shot and prints a fill-in summary. Prefer it over typing the sections by hand:

```bash
scp robots/reachy-mini/recon.sh pollen@reachy-mini.local:/tmp/
ssh pollen@reachy-mini.local 'bash /tmp/recon.sh' | tee reachy-recon.txt
# add --audio-test to also run the 3s mic->speaker loopback (the only step that makes sound)
```

The manual sections below document what each probe checks and why.

### 1.1 OS & Kernel

```bash
cat /etc/os-release              # Bookworm or Bullseye?
uname -a                        # kernel version, arch
cat /boot/firmware/config.txt 2>/dev/null || cat /boot/config.txt
df -h                           # disk usage (16 GB eMMC)
free -h                         # RAM
```

**Why**: determines package availability, dtoverlay syntax, and whether
`/boot/firmware/` or `/boot/` is the config path.

### 1.2 Network Stack

```bash
systemctl is-active NetworkManager
systemctl is-active dhcpcd
systemctl is-active wpa_supplicant
systemctl is-active systemd-networkd
nmcli device status 2>/dev/null || echo "No NetworkManager"
ip addr show wlan0
cat /etc/wpa_supplicant/*.conf 2>/dev/null
cat /etc/dhcpcd.conf 2>/dev/null
ls /etc/NetworkManager/system-connections/ 2>/dev/null
```

**Why**: the single most important check. Determines whether our `setup.sh` can
reuse the existing dhcpcd/wpa_supplicant flow or needs a NetworkManager-aware
path. See [recovery.md](recovery.md) for risk analysis.

**Decision tree**:

```
NetworkManager active?
├── YES → write NM-aware setup.sh (nmcli for AP/STA, skip hostapd)
│         OR disable NM and install dhcpcd stack (riskier)
└── NO → dhcpcd active?
    ├── YES → current setup.sh works as-is
    └── NO → systemd-networkd? custom? → investigate
```

**Result (2026-07-29)**: NetworkManager **active**, `wpa_supplicant` active,
`dhcpcd` inactive → take the **nmcli branch**. Pollen already ships two NM
profiles, so setup.sh should extend them rather than install hostapd/dnsmasq:

| Profile | Role |
|---------|------|
| `Glinks` | STA — the WiFi the unit was provisioned onto |
| `Hotspot` | AP — `mode=ap`, ssid `reachy-mini-ap`, `ipv4=shared`, `autoconnect=false`, driven by `reachy-mini-daemon.service` (the "AP Launcher") |

### 1.3 Pollen Daemon

```bash
systemctl list-units | grep -i reachy
systemctl list-units | grep -i pollen
systemctl status reachy*
curl -s http://localhost:8000 | head -20
curl -s http://localhost:8000/api 2>/dev/null | head -20
ls /venvs/
ls /restore/venvs/
pip list 2>/dev/null | grep -i reachy
cat /etc/systemd/system/reachy* 2>/dev/null
```

**Why**: need to know the exact service name, port, API surface, and venv
layout so our HAL driver and setup.sh don't collide.

**Record** (2026-07-29):
- [x] Daemon service name: `reachy-mini-daemon.service` (plus
      `reachy-mini-bluetooth.service`, `gpio-shutdown-daemon.service`)
- [x] Daemon port: `8000` (REST + WS); also listens on `8443`
- [x] Daemon API base path: `/api/...`, WS at `/ws/sdk` (also `/ws/daemon`,
      `/ws/full`, `/ws/raw`, `/ws/set_target`, `/ws/apps`, `/ws/logs`, `/ws/updates`)
- [x] Python version in `/venvs/`: `3.12` (`/venvs/mini_daemon`, `reachy_mini` 1.9.0);
      system Python is `3.13.5`
- [x] `/restore/venvs/` exists: **yes**

`GET /` serves a "dashboard deprecated" page — the API is under `/api/`, and
`GET /openapi.json` enumerates it. Useful endpoints found: `/api/daemon/status`
(control-loop stats, wlan ip, hardware id), `/api/camera/specs` (resolutions +
K/D intrinsics), `/api/motors/status`, `/api/move/*`, `/api/state/*`,
`/api/volume/*`, `/wifi/*`, `/update/*`, `/api/apps/*`, and the media handover
described in 1.9.

### 1.4 Audio

```bash
arecord -l                       # list capture devices (mic array)
aplay -l                        # list playback devices (speaker)
cat /proc/asound/cards
cat /etc/asound.conf 2>/dev/null
# Quick test (record 3s, play back)
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:1,0 /tmp/test.wav
```

**Record** (2026-07-29):
- [x] Mic ALSA name: `plughw:0,0` → aliased to `plug:device_mic`
- [x] Speaker ALSA name: `plughw:0,0` → aliased to `plug:device_speaker`
      (**same card and device as the mic** — one USB audio interface)
- [x] Mic channels: mono capture verified at 1 ch; the array is exposed as a
      single USB Audio capture device, not per-mic channels
- [x] Sample rate works at 16 kHz: **yes** (`arecord -f S16_LE -r 16000 -c 1`)

Cards seen: `0: Audio [Reachy Mini Audio]` (USB, capture + playback),
`1: vc4hdmi0`, `2: vc4hdmi1`. No `/etc/asound.conf` shipped by Pollen, so ours
adds one without touching `pcm.!default`.

**Caveat**: these commands only succeed after the daemon releases media — see 1.9.

### 1.5 Camera

```bash
v4l2-ctl --list-devices
ls /dev/video*
# Quick test
v4l2-ctl -d /dev/video0 --all 2>/dev/null | head -30
# If libcamera is used instead of V4L2:
libcamera-hello --list-cameras 2>/dev/null
```

**Record** (2026-07-29):
- [x] Camera device index: `/dev/video0` is the **unicam raw Bayer** node —
      unusable as an OpenCV index. `HAL_CAMERA_INDEX` is inert on this body.
- [x] V4L2 or libcamera: **libcamera** (`imx708_wide` on CSI, `rpicam-apps` +
      `gstreamer1.0-libcamera` installed, `python3-picamera2` **not** installed)
- [x] Max resolution: sensor `4608x2592` 10-bit RGGB; daemon-exposed modes top
      out at `3840x2592@10fps`, default `1280x720@30fps`

Measured failure of the OpenCV path: `cv2.VideoCapture(0)` opens but `read()`
returns `False` (`select() timeout`), and the wheel-built `opencv-python` reports
`GStreamer: NO`, so a `libcamerasrc` pipeline is not available either. Camera
strategy options are compared in
[`runtime.md`](runtime.md#camera-stack-libcamera-not-uvc).

### 1.6 Existing Services & Ports

```bash
ss -tlnp                        # all listening TCP ports
systemctl list-units --type=service --state=running
# Check for port conflicts with our services
# HAL: 5001, os-server: 5000 (binds 127.0.0.1 only), nginx: 80
```

**Record** (2026-07-29):
- [x] Port 5001 free: **yes**
- [x] Port 5000 free: **yes** (os-server, loopback-only)
- [x] Port 80 free: **yes** — Pollen ships no nginx

Occupied by the daemon (pid of `python` from `/venvs/mini_daemon`): `8000` and
`8443`, plus ephemeral high ports per interface. `22` is sshd.

The stack has since grown two more listeners, both loopback and neither in the
daemon's range: the OpenClaw gateway on `18789` (`spike-agent.sh`) and the OTA
bootstrap worker on `8080` (`httpPort` in the `/root/config/bootstrap.json` seed
written by `spike-bootstrap.sh`). Re-check those two on any new unit.

### 1.7 System Dependencies

```bash
# Check if pygobject/pycairo build deps exist
dpkg -l | grep -E 'libcairo2-dev|libgirepository|pkg-config'
python3 --version
which uv 2>/dev/null
which pip3
```

### 1.8 Bluetooth

```bash
# Check BLE service for recovery
systemctl status bluetooth
hciconfig -a 2>/dev/null
```

**Record** (2026-07-29): `bluetoothd` running, adapter `hci0` UP, name
`reachy-mini`, BD `88:A2:9E:8C:DC:B7` → the BLE recovery path in
[recovery.md](recovery.md) Level B is available on this unit.

### 1.9 Media Ownership (who holds camera and audio)

The single most consequential probe, and the one this plan originally missed.
The Pollen daemon holds the camera and both ALSA PCMs while it is running:

```bash
sudo fuser -v /dev/video0 /dev/video1     # daemon python + pipewire + wireplumber
sudo fuser -v /dev/snd/*                  # daemon python on pcmC0D0c and pcmC0D0p
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 1 /tmp/t.wav
#   -> audio open error: Device or resource busy
curl -s http://localhost:8000/api/media/status
```

The daemon supports an explicit handover, which is what makes the "HAL owns
audio/camera" design viable:

```bash
curl -s -X POST http://localhost:8000/api/media/release   # frees camera + audio
# ... verify: arecord records, rpicam-jpeg captures ...
curl -s -X POST http://localhost:8000/api/media/acquire   # give it back
```

**Record** (2026-07-29):
- [x] Daemon holds camera + both audio PCMs by default: **yes**
- [x] `POST /api/media/release` frees them: **yes** (verified with `arecord` and
      `rpicam-jpeg`)
- [x] Daemon survives release/acquire: **yes** — stays `active`, HTTP 200, motion
      control unaffected
- [x] Wired into HAL startup/shutdown: **yes** — `ROBOT.md` declares
      `owner: pollen_daemon` on `audio` and `vision`, and `hal/server.py` releases
      in Phase 0 of lifespan startup and acquires on shutdown
      (`hal/drivers/media_owner/pollen.py`). No spike script calls `/api/media/*`;
      the one exception is a best-effort `acquire` in `spike-hal.sh --stop`, for a
      HAL killed too hard to run its own shutdown hook.

## Phase 2: Write Configs Based on Findings

After Phase 1, update these files **on the dev machine** (not on the Pi). They
reach the robot only by being published: `make upload-device reachy-mini` puts a
new `devices.reachy-mini` package on the OTA feed, and `spike-device.sh` (or the
bootstrap worker, or `setup.sh`) installs it and applies its `rootfs/` overlay.
Hand-editing `/opt/hal/.env` on the robot is a debugging move, not a change —
keep it with `spike-device.sh --keep-env`, then fold it back into the repo.

### 2.1 ALSA Config — DONE

`robots/reachy-mini/rootfs/etc/asound.conf` now exists with the measured
device. Mic and speaker are the same USB card, addressed by name so the HDMI
cards cannot shift the index:

```
pcm.device_mic {
    type plug
    slave.pcm "hw:CARD=Audio,DEV=0"
}

pcm.device_speaker {
    type plug
    slave.pcm "hw:CARD=Audio,DEV=0"
}
```

It deliberately omits `pcm.!default`: the daemon shares this hardware and must
keep the default it expects.

### 2.2 HAL .env — audio DONE, camera DONE via the `rpicam` driver

`robots/reachy-mini/rootfs/opt/hal/.env` now carries the measured values:

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic        # from 1.4
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker   # from 1.4
HAL_CAMERA_INDEX=0                          # inert — see 1.5, libcamera not V4L2
```

Audio only opens after the daemon releases media, which HAL now does for itself
(1.9).

The camera was never a config value, so no `.env` field could fix it: `/dev/video0`
is the raw Bayer unicam node, and the wheel-built `opencv-python` reports
`GStreamer: NO`, so neither `cv2.VideoCapture(0)` nor a `libcamerasrc` pipeline
can produce frames (1.5). Resolved with a second HAL camera backend instead:
`ROBOT.md` declares `driver: rpicam`, which `hal/drivers/camera/factory.py`
maps to `RpicamVideoCaptureDevice`
(`hal/drivers/camera/rpicam_capture_device.py`) — it reads MJPEG from an
`rpicam-vid` child process and decodes the newest frame with `cv2.imdecode`.
`HAL_CAMERA_INDEX` stays inert on this body. Details in
[`runtime.md`](runtime.md#camera-stack-libcamera-not-uvc).

### 2.3 setup.sh (New, Reachy-Specific)

Not written yet. There is no `robots/reachy-mini/setup.sh`: the only installer
in that folder is `robots/reachy-mini/install.sh`, which fetches the OTA device
package and hands over to `spike.sh` (installs alongside Pollen, no network
changes). Production provisioning needs a `DEVICE_TYPE=reachy-mini` branch in
the shared `scripts/provision/setup.sh`, which today still stops NetworkManager
(see [recovery.md](recovery.md#risk-assessment-settled-pollen-os-uses-networkmanager)). Recon settled the branch: **NetworkManager** —
take the "If NM" column, and reuse Pollen's existing `Hotspot` profile instead of
creating a parallel AP stack.

| Decision | If NM | If dhcpcd |
|----------|-------|-----------|
| AP mode | `nmcli` hotspot or install hostapd | Existing hostapd flow |
| STA mode | `nmcli con add` | Existing wpa_supplicant flow |
| DNS captive portal | dnsmasq drop-in (same) | dnsmasq drop-in (same) |
| Service masking | Don't mask NM, configure it | Mask global wpa_supplicant (same) |

Regardless of network stack, setup.sh must:

1. **Never stop or restart the Pollen daemon** during install
2. **Install into a separate venv** (`/opt/hal/.venv/`, not `/venvs/`)
3. **Install system deps**: `libcairo2-dev`, `libgirepository1.0-dev`, `pkg-config`
4. **Not conflict on ports**: verify 5001, 5000, 80 are free before binding
5. **Set hostname** to `reachy-mini-<suffix>` without breaking Pollen's mDNS
6. **Create systemd units** for `hal.service` and `os-server.service`
7. **Install nginx** with captive portal config (or skip if Pollen already runs nginx)

### 2.4 HAL .env Production Plan

The `TODO(spike)` placeholders that once sat in
`robots/reachy-mini/rootfs/opt/hal/.env` are gone — Phase 1 filled them in, and
`spike-device.sh` is what carries the file to `/opt/hal/.env` as part of the
device package's rootfs overlay.

**Full .env field plan** (fields marked `?` still need real-device tuning):

```bash
# --- Core ---
HAL_MODE=production
HAL_LOG_LEVEL=INFO
DEVICE_TYPE=reachy-mini
DEVICES_DIR=/opt/devices

# --- Pollen daemon ---
# Recon 1.3: confirm host/port. Currently commented out (defaults to
# localhost:8000 in reachy_service.py). Uncomment only if daemon runs
# on a different host or port.
#REACHY_DAEMON_HOST=localhost          # ? confirm daemon is on localhost
#REACHY_DAEMON_PORT=8000              # ? confirm port from `ss -tlnp`

# --- Audio ---
# Recon 1.4: fill from `arecord -l` / `aplay -l`
HAL_AUDIO_INPUT_ALSA=plug:device_mic  # ? actual ALSA name after asound.conf
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker  # ? actual ALSA name
HAL_VAD_THRESHOLD=500                 # may need tuning for 4-mic array
HAL_SPEECH_HOLDOFF=0.05
HAL_SILENCE_TIMEOUT=3.0
HAL_STT_KEEPALIVE=false
HAL_SILERO_ENABLED=true
HAL_SILERO_THRESHOLD=0.15             # ? tune on real hardware
HAL_SILERO_CHUNK_SIZE=512
HAL_WEBRTCVAD_ENABLED=true
HAL_WEBRTCVAD_AGGRESSIVENESS=2        # ? may need 3 for noisy servo motors
HAL_WEBRTCVAD_FRAME_MS=30
HAL_TTS_SPEED=1.1

# --- Camera ---
# Recon 1.5: confirm index from `v4l2-ctl --list-devices`
HAL_CAMERA_INDEX=0                    # ? confirm device index
HAL_CAMERA_WIDTH=1280                 # ? confirm max usable resolution
HAL_CAMERA_HEIGHT=720                 # ? with daemon CPU sharing
HAL_CAMERA_STREAM_WIDTH=960
HAL_CAMERA_STREAM_HEIGHT=540
HAL_CAMERA_AUTO_EXPOSURE=auto
# NOTE: if Pollen OS uses libcamera instead of V4L2, HAL's OpenCV backend
# may need LIBCAMERA_LOG_LEVELS=ERROR or a gstreamer pipeline. Check 1.5.

# --- Sensing ---
HAL_MOTION_ENABLED=true
HAL_EMOTION_ENABLED=true
HAL_POSE_MOTION_ENABLED=false         # no pose tracking until CPU budget known
HAL_MOTION_CONFIDENCE_THRESHOLD=0.4
HAL_EMOTION_CONFIDENCE_THRESHOLD=0.5
HAL_DL_ENCRYPTION=true
HAL_DL_ENCRYPTION_REQUIRED=false
SPEAKER_MATCH_THRESHOLD=0.75
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD=0.75

# --- Realtime voice ---
HAL_REALTIME_TURN_DETECTION=off
HAL_WARM_MIC=true
HAL_WARM_MIC_ECHO_SKIP_MAX_S=0.1
HAL_ECHO_RMS_FLOOR=300                # ? tune: Reachy's speaker is 5W,
                                      #   may need different floor vs Lamp's 3W

# --- CPU tuning ---
# RPi CM4: 4 cores shared with Pollen daemon (control loop measured at
# ~49 Hz on this unit, not the 100 Hz quoted by Pollen's docs).
# Keep thread count low to avoid starving the daemon.
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
# ? After thermal recon: consider adding CPU governor or thermal throttle
```

**Key tuning questions for real hardware**:

1. **VAD threshold**: Reachy's servo motors may produce more mechanical noise
   than Lamp's Feetech servos. The gate sits at the code default of 2; noisy
   servos may need `HAL_WEBRTCVAD_AGGRESSIVENESS=3`.
2. **Echo floor**: 5W speaker vs Lamp's 3W — `HAL_ECHO_RMS_FLOOR` may need
   raising to avoid self-triggering during TTS playback.
3. **Camera**: if Pi Camera v3 uses libcamera, OpenCV `VideoCapture(index)` may
   not work. Need gstreamer pipeline or `picamera2` integration.
4. **CPU budget**: the Pollen daemon's control loop measured **~49 Hz** on this
   unit (`/api/daemon/status`); 100 Hz is what Pollen's docs claim. Our sensing
   (emotion + motion detection) adds CPU load. Monitor with `htop` during
   concurrent motion + inference. If CPU > 80%, disable `HAL_POSE_MOTION_ENABLED`
   and reduce camera resolution.

### 2.5 Motion Driver TODOs

After Phase 1, resolve `TODO(spike)` items in `reachy_service.py`:

- [ ] Verify `wake_up()` / `goto_sleep()` behavior
- [ ] Verify sign conventions (positive yaw = left or right?)
- [ ] Test all 28 emotion→HF move mappings
- [ ] Check if recorded moves need Hugging Face network access on first run
- [ ] Measure thermal profile under load

## Phase 3: Deploy & Validate

### 3.1 Spike Deploy

The spike scripts **run on the robot**. Copy the folder over and run it there —
nothing is built on the dev machine:

```bash
scp -r robots/reachy-mini pollen@reachy-mini.local:~/
ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
```

Every component comes from the OTA metadata feed
(`https://cdn.autonomous.ai/os/ota/metadata.json`, or `OTA_METADATA_URL=…`, or
`metadata_url` in `/root/config/bootstrap.json`) — the same source the imager and
`scripts/provision/setup.sh` read, so the spike robot runs what the fleet runs.

`spike.sh` is only an orchestrator; it runs six component scripts in a fixed
order, each of which also runs standalone:

```bash
sudo bash ~/reachy-mini/spike-device.sh     # /opt/devices + rootfs overlay onto /
sudo bash ~/reachy-mini/spike-hal.sh        # HAL on 127.0.0.1:5001
sudo bash ~/reachy-mini/spike-os.sh         # os-server API on 127.0.0.1:5000
sudo bash ~/reachy-mini/spike-web.sh        # nginx + web UI on :80
sudo bash ~/reachy-mini/spike-agent.sh      # OpenClaw gateway on 127.0.0.1:18789
sudo bash ~/reachy-mini/spike-bootstrap.sh  # OTA worker — keeps the robot current
```

Start with **device then hal**: that is the smallest pair that validates the body.
`spike-device.sh` is what puts `ROBOT.md`, `/etc/asound.conf` and `/opt/hal/.env`
on the robot, and `spike-hal.sh` refuses to start without them. Nothing here
calls `POST /api/media/release` any more — `ROBOT.md` declares
`owner: pollen_daemon` on the audio and vision capabilities, so HAL borrows the
camera and both ALSA PCMs itself at startup and hands them back on shutdown
(see 1.9).

`spike-web.sh` is what makes anything reachable from a browser — os-server binds
loopback and serves no static files. Its vhost keeps `/hw/` loopback-only, like
production: hardware is reached through os-server's authenticated
`/api/hardware/*` proxy, never HAL directly.

`spike-bootstrap.sh` is deliberately last. It can restart os-server and HAL the
moment it finds a newer build, and doing that mid-install turns a clean bring-up
into a race. It is also what makes `/root/config/bootstrap.json` authoritative:
every other spike script reads `metadata_url` from that file.

Everything lands under systemd (`hal`, `os-server`, `openclaw`, `bootstrap`) and
survives a reboot. Teardown is `sudo bash ~/reachy-mini/spike.sh --stop` (or
`--uninstall`), which walks the steps in reverse so the OTA worker cannot
reinstall what a later step is still removing.

Found on 2026-07-29 and already fixed: the docs claimed os-server listens on
`:8080` (that port belongs to the bootstrap worker, see 1.6). The script's
`:5000` probe was right and the docs were wrong — os-server
binds `127.0.0.1:5000` (`system/server/config/config.go`,
`system/server/server.go`), so it is reachable only from the Pi until nginx fronts
it. `uv` is absent on Pollen OS; `spike-hal.sh` installs it to `/usr/local/bin`,
but production `setup.sh` must too.

The board gate also had to be taught this hardware: the unit reports
`Raspberry Pi Compute Module 4 Rev 1.1`, which matched no `boards.json` entry, so
HAL refused to boot. Fixed by adding `raspberry_pi_cm4` and declaring it in
`ROBOT.md`.

### 3.2 Smoke Test

Run these **on the robot**: HAL binds `127.0.0.1:5001`, so there is no `<IP>` to
aim at. From a browser it is reached through os-server's `/api/hardware/*` proxy,
and `spike-web.sh`'s `/hw/` location is loopback-only for the same reason.

```bash
# Health
curl -s localhost:5001/health
curl -s localhost:5001/device

# Motion (safe order)
curl -s localhost:5001/servo/position
curl -s -X POST localhost:5001/servo/aim \
  -H 'content-type: application/json' \
  -d '{"direction":"center","duration":1.0}'
curl -s -X POST localhost:5001/servo/zero
curl -s -X POST localhost:5001/servo/release

# Audio
curl -s -X POST localhost:5001/speaker/play \
  -H 'content-type: application/json' \
  -d '{"text":"Hello, I am Reachy"}'

# Camera
curl -s localhost:5001/camera/snapshot -o /tmp/snap.jpg

# The rest of the stack
curl -s localhost:5000/api/health/live      # os-server
curl -s -o /dev/null -w '%{http_code}\n' localhost/   # nginx + web bundle
```

### 3.3 Production Setup Test

Only after spike works:

```bash
ssh pollen@<IP>
DEVICE_TYPE=reachy-mini bash setup.sh   # scripts/provision/setup.sh, once its NM branch exists
# Reboot
sudo reboot
# Verify AP mode comes up
# Connect phone to reachy-mini-xxxx AP
# Complete setup flow via captive portal
```

## Phase 4: Update Docs

After everything works, update:

- [ ] `robots/reachy-mini/docs/runtime.md` — fill all TODO(spike) items
- [ ] `robots/reachy-mini/docs/vi/runtime_vi.md` — Vietnamese mirror
- [ ] `robots/reachy-mini/docs/recovery.md` — confirm BLE commands work
- [ ] `robots/reachy-mini/docs/vi/recovery_vi.md` — Vietnamese mirror
- [ ] `robots/reachy-mini/rootfs/opt/hal/.env` — actual values
- [ ] `robots/reachy-mini/rootfs/etc/asound.conf` — actual ALSA names
- [ ] `CLAUDE.md` — if new docs are created

## Phase 5: Golden Base Image (Capture from Device) & build-reachy.sh

The Reachy image build follows the **same pattern lamp/intern-v2 already use**:
the imager does not build on a stock vendor image — it builds on a base image
**captured from a known-good device**. See `scripts/imager/README.md`
("Base image — per device type"). The Pollen GitHub release image
(`recovery.md` Level D) is a **recovery fallback only**, not the build base.

### 5.1 Capture the base image FROM the device

Do this **before running `setup.sh`**, while the eMMC is still pristine Pollen OS
(Phase 1 recon is read-only, so it's fine to recon first, then capture). The CM4
eMMC has no SD slot, so capture goes through the same rpiboot USB path as a
reflash (`recovery.md` Level D) — but **reading** instead of writing:

```bash
# 1. shutdown robot → switch SW1 to DOWNLOAD → connect USB2 → start rpiboot → power on
sudo ./rpiboot -d mass-storage-gadget64          # eMMC appears as /dev/diskX (macOS) or /dev/sdX (Linux)

# 2. unmount the auto-mounted partitions first
sudo diskutil unmountDisk /dev/diskX             # macOS
# sudo umount /media/$USER/bootfs /media/$USER/rootfs   # Linux

# 3. raw-read the WHOLE disk (partition table included) and compress on the fly
sudo dd if=/dev/rdiskX bs=8m | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz    # macOS (note rdiskX)
# sudo dd if=/dev/sdX bs=8M | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz     # Linux
```

Store it as the imager base, following the lamp/intern layout:
`scripts/imager/input/reachy-mini/golden-reachy-dev.img.xz`. Optionally mirror it
to the Autonomous CDN like the other bases:
`gs://s3-autonomous-upgrade-3/os/imager/base/golden-reachy-dev.img.xz`.

### 5.2 Recover the device FROM the captured base

Same rpiboot USB path as 5.1, writing the captured image back. Two options:

```bash
# --- Option A: dd (simplest — the capture was a dd, so dd restores it verbatim) ---
# shutdown → SW1 DOWNLOAD → USB2 → rpiboot → power on → unmount (as in 5.1)
xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/rdiskX bs=8m    # macOS
# xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/sdX bs=8M     # Linux

# --- Option B: bmaptool (faster, sparse — generate a .bmap once, then flash) ---
xz -dc reachy-mini-base-v<pollen-ver>.img.xz > reachy-base.img
bmaptool create -o reachy-base.bmap reachy-base.img
sudo bmaptool copy reachy-base.img --bmap reachy-base.bmap /dev/rdiskX
```

Then restore normal boot: power off → switch back to **DEBUG** → disconnect USB →
power on. Verify with `reachyminios_check` (should print `Image validation PASSED`)
and confirm the robot actually moves correctly (calibration check, see 5.3).

### 5.3 Why capture, not download the Pollen release

- The shipped eMMC may carry drivers, config, or first-boot state the generic
  release lacks — capturing guarantees the base matches the exact hardware and
  daemon the unit runs.
- Same rationale lamp/intern-v2 use the hardware team's image over the stock `.7z`.
- **Caveat — per-unit state**: a dump of one unit's eMMC may include per-robot
  calibration (servo/IMU offsets) or identity. Restoring onto the **same** unit
  is always safe. Before reusing a captured image as the base for **other** units,
  verify what is per-unit and strip/regenerate it — recon question: does
  `reachyminios_check` pass **and** does the robot move correctly after flashing a
  captured image onto a *different* unit?

### 5.4 build-reachy.sh (future imager target — not yet written)

Would mirror `build-orangepi.sh` phases, adapted:

| build-orangepi.sh | build-reachy.sh delta |
|---|---|
| Phase 0 base: `gdown` stock `.7z` | decompress `input/reachy-mini/golden-reachy-dev.img.xz` (captured in 5.1) |
| Phase 2 chroot apt: hostapd/dnsmasq/dhcpcd | **settled by recon 1.2** — NetworkManager, `dhcpcd` inactive, so no hostapd/dnsmasq/dhcpcd; reuse the existing `Hotspot` NM profile for AP mode |
| Phase 2: bake full OS stack | **install on top — never wipe the Pollen daemon** |
| Flash: SD card via Imager | **rpiboot + bmaptool to eMMC** — no SD slot |
| Phase 3 OTA bake | same: os-server, bootstrap, HAL, device profile overlay |

Blocked on: the per-unit-state question (5.3) only. Recon 1.2 is answered —
NetworkManager is active and `dhcpcd` is off, with `Glinks` (STA) and `Hotspot`
(`reachy-mini-ap`, `ipv4=shared`) already on the unit. Only
worth building when shipping multiple Reachy units; for a single dev unit,
`spike.sh` + `setup.sh`-on-top is enough.

## References

- [Pollen OS build system](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini SDK](https://github.com/pollen-robotics/reachy_mini)
- [Hardware datasheet](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Reflash guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [BLE reset](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Autonomous setup.sh](../../../scripts/provision/setup.sh)
- [Autonomous imager](../../../scripts/imager/README.md)
