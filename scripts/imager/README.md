# imager — device golden image builder

Produces SD card images that boot OrangePi 4 Pro (or Raspberry Pi 4B / 5) directly
into the device AP/hotspot setup wizard. Flash, insert, power on — no
`scripts/provision/setup.sh` needed on the target.

`DEVICE_TYPE` and `OTA_METADATA_URL` are **required** (no default) — one device
class per golden image; preflight fails if either is unset.

```bash
make build DEVICE_TYPE=lamp OTA_METADATA_URL=…        # → output/lamp/golden-opi-lamp.img.xz
make build DEVICE_TYPE=intern-v2 OTA_METADATA_URL=…   # → output/intern-v2/golden-opi-intern-v2.img.xz
make upload                        # push image + release note to GCS, versioned
make upload-source                 # mirror stock OrangePi .7z → GCS (one-time)
make upload-source DEVICE_TYPE=lamp       # push hardware team's lamp base image → GCS
make upload-source DEVICE_TYPE=intern-v2  # push hardware team's intern-v2 base image → GCS
make clean                         # wipe output/ (input/ kept)
make clean-all                     # wipe both output/ and input/
```

First build takes ~25–40 min (download/decompress base image, qemu-arm64 chroot apt
install + HAL uv sync, OTA backend bake, xz compress). OrangePi reuses the source
archive in `input/`, but reruns all provisioning phases on each build.

## Base image — per device type

lamp and intern-v2 use the **hardware team's pre-built base image** instead of the stock vendor .7z:

| DEVICE_TYPE | Local path | GCS key |
|---|---|---|
| `lamp` | `input/lamp/golden-opi-dev.img.xz` | `os/imager/base/golden-opi-dev-lamp.img.xz` |
| `intern-v2` | `input/intern-v2/golden-opi-dev.img.xz` | `os/imager/base/golden-opi-dev-intern-v2.img.xz` |
| other | `input/orangepi.7z` (Google Drive) | `os/imager/source/Orangepi4pro_*.7z` |

When the hardware team ships a new base image, run `make upload-source DEVICE_TYPE=lamp` to push it to GCS so other devs can pull it for their next build.

**Manual download** (if you received the file via AirDrop / file share and need to place it yourself):

| DEVICE_TYPE | Download | Place at |
|---|---|---|
| `lamp` | [golden-opi-dev-lamp.img.xz](https://storage.googleapis.com/s3-autonomous-upgrade-3/os/imager/base/golden-opi-dev-lamp.img.xz) | `imager/input/lamp/golden-opi-dev.img.xz` |
| `intern-v2` | [golden-opi-dev-intern-v2.img.xz](https://storage.googleapis.com/s3-autonomous-upgrade-3/os/imager/base/golden-opi-dev-intern-v2.img.xz) | `imager/input/intern-v2/golden-opi-dev.img.xz` |

After placing the file, run `make build DEVICE_TYPE=lamp OTA_METADATA_URL=…` as usual.

## Config ownership

| Config | Owner | How it gets on the Pi |
|---|---|---|
| `hal.env` | **OS team** | Written into the image by the device rootfs overlay — build-orangepi.sh Phase 3, build.sh Phase 2 |
| `asound.conf` | **OS team** (hardware team reviews) | `robots/<type>/rootfs/etc/asound.conf` — device rootfs overlay, copied onto `/` at build and on every device-profile OTA |
| udev rules | **OS team** (hardware team reviews) | `robots/lamp/rootfs/etc/udev/rules.d/` — same overlay; vid:pid → role (device-camera, device_speaker, …). Edit in repo, never on the device |
| SPI3 overlay | **Hardware team** | Baked into base image |

The device profile overlay (`robots/<type>/rootfs/`) only contains files owned by the OS team.

## Targets

| Board | TARGET | RPI_MODEL | Builder | Output | Status |
|-------|--------|-----------|---------|--------|--------|
| **OrangePi 4 Pro v2 (Allwinner A733)** *default* | `opi` | — | `build-orangepi.sh` | `output/<type>/golden-opi-<type>.img.xz` | **working** |
| Raspberry Pi 5 | `rpi` | `5` (default) | `build.sh` | `output/golden-<type>.img` | working |
| Raspberry Pi 4B | `rpi` | `4` | `build.sh` | `output/golden-<type>.img` | code-only, untested on HW |

## OrangePi build flow

```
Phase 0  Source base image
         - lamp/intern-v2: decompress input/<device>/golden-opi-dev.img.xz → /work/base-<type>.img
         - other device types: gdown Google Drive .7z → input/orangepi.7z (cached, 734 MB)
Phase 1  Extract (stock only) + expand
         - Stock: 7z e → /work/*.img; lamp/intern-v2 skip this step (already .img from Phase 0)
         - Reject source larger than OUT_IMG_SIZE (default 14 GiB)
         - cp → /output/<type>/golden-opi.img.building, expand to OUT_IMG_SIZE
         - growpart, losetup --offset/--sizelimit, writable e2fsck -fp (exit 0/1 only), resize2fs
         - mount /mnt/opi
Phase 2  chroot qemu-arm64:
         - apt install (hostapd, dnsmasq, nginx, avahi, bluez, pulseaudio, alsa-utils,
           chromium, xvfb, …)
         - Node.js 26 latest package (at least 26.8.2) + OpenClaw 2026.9.3 + Discord/Slack plugins
         - Hermes 0.21.5 pinned to release v2026.9.24; verify CLI version before continuing
         - openclaw onboard --skip-health (180 s cap), then clear the workspace attestation
           in state/openclaw.sqlite so first setup can reseed the workspace (OpenClaw >= 2026.9)
         - uv (Python pkg mgr for HAL)
         - systemd units: os-server, bootstrap, hal, openclaw
         - helper scripts: device-ap-mode, device-sta-mode, connect-wifi, software-update
         - hal.env (OS team owned): HAL_AUDIO_*, HAL_VAD_THRESHOLD, DEVICE_TYPE, …
         - configs: hostapd, dnsmasq, dhcpcd, nginx (CSP + WS + captive-portal),
           PulseAudio (WebRTC AEC + anon socket)
         - NOTE: asound.conf + udev rules NOT written here — they ship in the device rootfs overlay (Phase 3)
         - mask orangepi-firstrun-config.service
Phase 3  OTA bake from metadata.json:
         - bootstrap-server + os-server binaries
         - device profile (devices/<type>) → /opt/devices/<type>/
           rootfs/ overlay (udev rules etc.) copied onto /
         - HAL Python app + uv sync --python 3.12 --extra hardware
         - Web UI → /usr/share/nginx/html/setup
         - Claude Desktop Buddy BLE plugin (optional)
Phase 4  resize-once.service (first-boot self-destructing growpart + resize2fs)
Phase 5  Finalize
         - OTA versions were recorded in output/manifest-opi.json during Phase 3
         - Sync, unmount, strict e2fsck -fn; detach both loop devices
         - Atomically publish output/<type>/golden-opi.img from .building
         - Unless COMPRESS=0, xz → golden-opi.img.xz.building, then rename to .xz
         - Makefile renames .xz to golden-opi-<type>[-<agent>][-<variant>].img.xz
```

The raw image defaults to 14 GiB; compressed size depends on the base and installed
software. A failed filesystem check stops the build without publishing the staged
image. An older completed output can remain after a failed build or `COMPRESS=0`;
check that the chosen raw or compressed artifact is the intended build.

## Raspberry Pi build flow

Different shape from OPi: the source is the **stock Raspberry Pi OS Lite arm64** image
downloaded from `downloads.raspberrypi.com` (Pi 5 → Trixie/Debian 13, Pi 4 → Bookworm/
Debian 12), the root filesystem is converted to **Btrfs**, and the build is split into a
slow cached base phase and a fast overlay phase.

```
Phase 1  BASE — runs only when /output/base.img is absent (~20 min)
         - download/extract stock RPi OS Lite → new 8 GB image, FAT32 boot + Btrfs root (@ subvol)
         - user, SSH, disable NetworkManager, WiFi country, hostname, locale, fstab, cmdline
         - chroot qemu-arm64: apt install, systemd units, PulseAudio, uv, nginx,
           hostapd/dnsmasq/dhcpcd + device-ap/sta-mode + connect-wifi + software-update
         - Node.js 22 + openclaw + Discord/Slack plugins
         - agent runtime pre-bake: hermes CLI + its (disabled) gateway unit always;
           codex / claude / picoclaw / opencode for lamp + intern-v2
         - btrfs-resize-once, fr-snapshot, fr-rollback  → /output/base.img
Phase 2  OVERLAY — always runs (~1 min)
         - OTA metadata → bootstrap-server + os-server binaries, HAL + uv sync
         - device profile (devices/<type>) → /opt/devices/<type>/,
           then its rootfs/ overlay copied onto / (this is where /opt/hal/.env lands)
         - f_r_default_agent + SSH policy (both follow DEFAULT_AGENT)
         - web UI, Claude Desktop Buddy
         - @factory Btrfs snapshot + QC checks → /output/golden-<type>.img
```

**base.img is cached but not generic.** Phase 1 bakes both device-type-specific content
(nginx conf name, hostapd SSID, dnsmasq drop-in, `Environment=DEVICE_TYPE`, the pre-baked
runtime CLIs) and board-specific content (Debian release, Pi-5-only stages). The builder
stamps `/output/base.img.built-for` with `<device_type>/rpi<model>` and **aborts** if a
later build asks for a different combination — delete `base.img` (and the stamp) to
rebuild. Anything that must follow `DEFAULT_AGENT` lives in Phase 2 for this reason.

### First boot on the device

1. `resize-once.service` runs once: `growpart + resize2fs` → ext4 fills real SD size. Self-disables.
2. Bootstrap triggers `device-ap-mode` if no STA association after timeout.
3. SSID: `<device_type>-xxxx` where `xxxx` = last 4 hex chars of eth MAC.
4. mDNS: `<device_type>-xxxx.local` via avahi-daemon.
5. Connect to AP → http://192.168.100.1/ → setup wizard → `device-sta-mode`.

## Flashing an SD card

**Recommended: Raspberry Pi Imager**

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Choose OS → "Use custom" → `output/<type>/golden-opi-<type>.img.xz`
3. Choose Storage → select SD card → Write

The Makefile flash targets require Python 3. Run `make sd-card-list` to identify
the disk, then choose the intended artifact:

```bash
make sd-card-flash DEVICE_TYPE=lamp DISK=N       # compressed image; add DEFAULT_AGENT/VARIANT if used at build time
make sd-card-flash-raw DEVICE_TYPE=lamp DISK=N   # latest completed raw image for this device type
```

To skip raw-image readback, add `VERIFY=0` to `sd-card-flash-raw`. This also skips its Python requirement; sync and eject still run. Verification is enabled by default.

Both targets sync writes, then use `lib/verify_flash.py` to read back and compare
all image bytes before reporting success and ejecting. The compressed target
also propagates decompression failures through Bash `pipefail`. A mismatch,
truncated read, or invalid compressed stream fails the target. Verification covers
the image length, not unused space on a larger card, and adds a full read pass.

Builder filesystem checks validate ext4 structure; flash readback checks that the
card matches the selected image. Neither guarantees a successful boot or by itself
identifies the cause of an initramfs/ext4 failure.

## Configuration knobs

| Variable | Default | Effect |
|----------|---------|--------|
| `TARGET` | `opi` | `opi`, `rpi` — picks builder script |
| `RPI_MODEL` | `5` | `rpi` only — `5` = Trixie source image, `4` = Bookworm |
| `DEVICE_TYPE` | **(required)** | `lamp`, `intern-v2`, … — one image per device class |
| `DEFAULT_AGENT` | *(unset)* | Bakes `/root/config/f_r_default_agent` (survives factory reset, wins over `ROBOT.md` `gateway.default`) and gates SSH for `intern-v2`. Unset = unchanged behavior |
| `OTA_METADATA_URL` | **(required)** | Baked into `/root/config/bootstrap.json` |
| `OUT_IMG_SIZE` | `14G` | OPi partition size after expansion |
| `COMPRESS` | `1` | `0` skips the `.xz` step; flash the raw `.img` with `make sd-card-flash-raw` |
| `OPI_FILE_ID` | `1CYfOaY6f5DozJBNvPJ0Gx1jBIFlGe8fn` | Google Drive ID for stock OPi .7z |
| `OPENCLAW_VERSION` | `2026.9.3` (OrangePi) | npm package version pin |
| `AP_BAND` | `2.4` | `2.4` or `5` — hostapd hw_mode |
| `COUNTRY_CODE` | `US` | Regulatory domain |

## Upload to GCS

```bash
gcloud auth login      # if token expired
make upload            # image + release note + RELEASES.md ledger
```

Versioned filename: `golden-opi-<type>-<UTC-timestamp>-<git-sha>.img.xz`

### Mirror source/base images to GCS

```bash
make upload-source                        # stock OrangePi vendor .7z (one-shot)
make upload-source DEVICE_TYPE=lamp       # lamp hardware team base image (overwrite OK)
make upload-source DEVICE_TYPE=intern-v2  # intern-v2 hardware team base image
```

### Upload device profile ZIPs (after changing robots/<type>/)

```bash
cd scripts/release && bash upload-device.sh lamp
cd scripts/release && bash upload-device.sh intern-v2
```

Run this after any change to `robots/<type>/` so OTA (`software-update device`) picks it up.

## File layout

```
imager/
├── Dockerfile             — Ubuntu 24.04 + qemu-aarch64-static + p7zip-full + gdown
├── Makefile               — build / flash / upload / upload-source targets
├── build-orangepi.sh      — OrangePi 4 Pro builder (~1200 lines)
├── build.sh               — Raspberry Pi 4B / 5 builder
├── input/                 — cached source images (gitignored)
│   ├── orangepi.7z        — stock vendor image (Google Drive fallback)
│   ├── lamp/golden-opi-dev.img.xz     — hardware team's lamp base image
│   └── intern-v2/golden-opi-dev.img.xz — hardware team's intern-v2 base image
├── output/                — built images per device type (gitignored)
│   ├── lamp/golden-opi-lamp.img.xz
│   └── intern-v2/golden-opi-intern-v2.img.xz
└── README.md              — this file
```

## Sanity checks after first flash

SSH: `orangepi/orangepi` (hardware team user) or `system/12345` (OS user).

```bash
systemctl is-enabled os-server hal openclaw avahi-daemon
ls /usr/local/bin/{os-server,bootstrap-server,device-ap-mode,connect-wifi,software-update}
ls /opt/hal/.venv/bin/uvicorn
openclaw --version
cat /opt/hal/.env | grep HAL_AUDIO      # audio vars present
cat /etc/asound.conf                    # from robots/<type>/rootfs overlay
ls /etc/udev/rules.d/                   # 99-lamp-device.rules + 91-pulseaudio-hal-ignore.rules from robots/lamp/rootfs overlay
grep overlays /boot/orangepiEnv.txt     # spi3 + i2c0, no uart8 (uart8 claims PL9 = GPIO button pin)
findmnt /                               # ext4, expanded to full SD
systemctl is-enabled resize-once 2>&1 | grep -q "not found" && echo "OK: self-destructed"
```

> **uart8 warning**: if `grep overlays` shows `uart8`, the GPIO button will fail with
> `bad event request`. uart8 overlay claims PL8+PL9; PL9 = gpiochip1 line 9 = button pin.
> Report to hardware team to remove uart8 from base image.

## Troubleshooting

### Docker on Mac: losetup / udev quirks

`--privileged + losetup` is flaky on Docker Desktop. If `losetup: cannot find
unused loop device` appears, switch to OrbStack (`brew install orbstack`) or
run on a Linux host.

### SSL `docker-credential-desktop` not in PATH

```bash
jq 'del(.credsStore)' ~/.docker/config.json > /tmp/c && mv /tmp/c ~/.docker/config.json
```

### gdown: Google Drive "Quota exceeded" / "Too many users"

1. Open https://drive.google.com/drive/folders/1AzF-uTwA328qDFPaVBaKpiP4VjZjkmbS
2. Right-click the `.7z` → **Add shortcut to My Drive** → download from your copy
3. Drop at `imager/input/orangepi.7z` and re-run `make build`

Or mirror to GCS once: `make upload-source`, then pull from GCS (set `OPI_SOURCE_URL`).

### `make upload` warning about parallel composite uploads

Warning, not fatal. To silence: `gcloud config set storage/parallel_composite_upload_enabled False`

### Final image fails to boot OPi

```bash
xz -dc output/lamp/golden-opi-lamp.img.xz | head -c 16M | hexdump -C | head -20
```

Expected: non-zero bytes near offsets `0x2000` (SPL) and `0x20000` (U-Boot).

## Recent changes

**2026-07-27** — `build.sh` (Pi) brought to parity with `build-orangepi.sh`:
- Device `rootfs/` overlay is now copied onto `/` — both at build time and in
  `software-update device`. Previously the Pi image installed the device profile but
  never applied its overlay, so `/opt/hal/.env` held only `DEVICE_TYPE`/`DEVICES_DIR`
  and every HAL tuning value (ALSA device names, VAD/camera thresholds) fell back to
  its code default
- Agent runtime pre-bake: hermes CLI + its disabled `hermes-gateway.service` unit
  (always), and codex / claude / picoclaw / opencode CLIs (lamp + intern-v2)
- `DEFAULT_AGENT` honored: bakes `/root/config/f_r_default_agent` and gates SSH for
  intern-v2 — the Makefile already passed the variable, only `build.sh` ignored it
- `base.img` cache is stamped with `<device_type>/rpi<model>` and a mismatched reuse
  now aborts instead of silently shipping the wrong image
- QC checks extended to cover all of the above

**2026-09-18** — udev rules move into the device profile overlay (`robots/lamp/rootfs/etc/udev/rules.d/`), next to `asound.conf`: hardware swaps are a vid:pid line in the repo, OTA-delivered; hand edits on a device are overwritten by the next device-profile OTA.

**2026-06-17** — Config ownership clarified, per-device base image:
- `asound.conf` removed from device profile overlay — hardware team bakes it into base image
- udev rules (`91-pulseaudio-hal-ignore.rules`) commented out in build script — hardware team owns
- lamp + intern-v2 now use hardware team's pre-built base image (`input/<device>/golden-opi-dev.img.xz`)
  instead of stock Google Drive .7z
- Per-device output subfolder: `output/<device_type>/golden-opi-<type>.img.xz`
- `make upload-source DEVICE_TYPE=lamp/intern-v2` to push per-device base images to GCS
- uart8 overlay conflict with GPIO button documented in sanity checks

**2026-06-08** — Raspberry Pi 4B support:
- `RPI_MODEL` env var: `4` = Pi 4B Bookworm, `5` = Pi 5 Trixie (default 5)

**2026-06-06** — nginx local-only block for `/api/system/exec`

**2026-06-04** — `software-update` full implementation (os-server, bootstrap, hal, openclaw, web, buddy)

**2026-06-03** — SPI3 overlay baked in for WS2812 LED ring

**2026-06-02** — Discord + Slack plugins baked at build time
