#!/bin/bash
# =============================================================================
# build.sh — Golden Image Builder for Raspberry Pi 4B / Pi 5
# =============================================================================
#
# PURPOSE
#   Produces a production-ready golden.img that boots straight into AP/hotspot
#   mode. Flash to SD card, insert into Pi 5, power on — done.
#
# TWO-PHASE BUILD (for fast iteration)
#   The build is split into a BASE phase and an OVERLAY phase.
#   Phase 1 (base) is slow (~20 min) and only runs when /output/base.img
#   does not exist. Phase 2 (overlay) is fast (~1 min) and always runs.
#
#   To force a full rebuild: delete /output/base.img
#   To iterate on backend/web: just re-run — only Phase 2 executes.
#   base.img is stamped with the DEVICE_TYPE + RPI_MODEL it was built for
#   (/output/base.img.built-for); a build for a different one aborts with
#   instructions rather than silently reusing the wrong cache.
#
# PHASE 1 — BASE IMAGE (skipped when /output/base.img exists)
#   1.  Download or use cached Raspberry Pi OS Lite (arm64, Trixie/Debian 13)
#   2.  Read partition offsets from the source image
#   3.  Copy source rootfs + boot to /work (before any modification)
#   4.  Create a fresh blank output image (8 G by default)
#   5.  Format: FAT32 boot partition + Btrfs root partition
#   6.  Create Btrfs @ subvolume (root)
#   7.  Mount subvolume and boot partition
#   8.  Restore rootfs from /work backup into the new image
#   9.  Disable RPi OS firstrun (prevents it overwriting our user setup)
#  10.  Create user system/12345 with sudo, home dir, correct groups
#  11.  Enable SSH (ssh flag file + systemd symlink)
#  12.  Disable NetworkManager — use wpa_supplicant + dhcpcd instead
#  13.  Set Wi-Fi regulatory country (modprobe, crda, raspi-config, firstrun)
#  14.  Enable persistent journal (survives reboots for debugging)
#  15.  Set hostname
#  16.  Set keyboard layout (US) and locale (en_US.UTF-8)
#  17.  Write /etc/fstab with Btrfs UUID (must be before chroot)
#  18.  Patch cmdline.txt for Btrfs root (rootfstype=btrfs rootflags=subvol=@)
#  19.  Enter chroot (arm64 via qemu):
#         - Install packages (btrfs-progs, hostapd, dnsmasq, nginx, etc.)
#         - Verify btrfs binary works (shared libs check)
#         - Generate locale
#         - stage_rpi5_wifi_stability: disable IPv6 (legacy RPi 5 workaround)
#         - stage_enable_spi: dtparam=spi=on + dtparam=i2c_arm=on in config.txt
#         - stage_backend_units: systemd services (bootstrap, os-server, hal) + software-update
#         - stage_pulseaudio: PulseAudio echo cancellation (WebRTC AEC for mic/speaker)
#         - stage_hal_uv: install uv (Python package manager for HAL)
#         - stage_nginx: write nginx config with backend/hal/openclaw upstreams
#         - stage_ap: hostapd, dnsmasq, dhcpcd, device-ap/sta-mode scripts
#         - stage_nodejs_openclaw: Node.js 22 + OpenClaw gateway
#         - stage_agent_runtimes: pre-bake the Hermes CLI + its (disabled)
#           gateway unit, and — for lamp / intern-v2 — the codex, claude,
#           picoclaw and opencode CLIs, so switch-runtime skips the slow
#           download on device
#  20.  Install btrfs-resize-once service
#  21.  Install fr-snapshot + fr-rollback
#       → Save as /output/base.img
#
# PHASE 2 — OVERLAY (always runs — fast)
#   Copy base.img → golden.img, mount, chroot:
#         - stage_ota_metadata: fetch build versions from GCS
#         - stage_backend: download bootstrap-server + os-server binaries
#         - stage_hal: download HAL Python app + uv sync
#         - stage_device_profile: install devices.<type> artifact, then copy its
#           rootfs/ overlay onto / (this is where /opt/hal/.env tuning lives)
#         - stage_default_agent: bake f_r_default_agent + apply the SSH policy
#           that follows DEFAULT_AGENT
#         - stage_web: download web UI zip
#   Take initial @factory snapshot (baked into image at build time)
#   QC checks (verify binaries, configs, services, subvolumes)
#
# BOOT SEQUENCE (on Pi after flash)
#   1. Kernel mounts Btrfs @ subvolume as root
#   2. firstrun-wifi.service: unblocks rfkill, sets Wi-Fi country (runs once)
#   3. btrfs-resize-once.service: resizes partition + Btrfs to full SD (runs once, self-destructs)
#      @factory is the build-time white board — never overwritten
#   4. User runs 'sudo device-ap-mode' to start AP hotspot "<device_type>-xxxx" at 192.168.100.1
#   5. nginx serves setup web UI, bootstrap/os-server/hal backends running
#
# BTRFS SUBVOLUME LAYOUT
#   @               — initial live root
#   @factory        — read-only snapshot, created by fr-snapshot
#   @restore-<ts>   — writable snapshot of @factory, created by fr-rollback
#
# FACTORY RESET FLOW
#   sudo fr-snapshot    → snapshot current root -> @factory (save known-good state)
#   sudo fr-rollback    → delete old @restore-* subvolumes
#                          snapshot @factory -> @restore-<ts>
#                          set @restore-<ts> as btrfs default subvolume
#                          update cmdline.txt rootflags=subvol=@restore-<ts>
#                          reboot
#
# REQUIREMENTS (Docker build host)
#   Docker with --privileged (for losetup, mount, btrfs)
#   qemu-user-static (for arm64 chroot on x86 host)
#   Internet access (for apt inside chroot + OTA downloads)
#
# USAGE
#   # Build Docker image
#   docker build -t pi-builder .
#
#   # Run (internet required for chroot apt + OTA)
#   docker run --rm --privileged \
#     -v $(pwd)/input:/input \
#     -v $(pwd)/output:/output \
#     pi-builder
#
#   # Flash to SD card (macOS)
#   diskutil unmountDisk /dev/diskN
#   sudo dd if=output/golden.img of=/dev/rdiskN bs=8m status=progress
#   sync && diskutil eject /dev/diskN
#
# ON-PI COMMANDS
#   sudo fr-snapshot              — save current state as @factory
#   sudo fr-rollback              — restore @factory and reboot
#   sudo device-ap-mode           — switch to hotspot mode
#   sudo device-sta-mode          — switch to station (client) mode
#   sudo connect-wifi SSID PASS   — connect to WiFi (switches to STA mode)
#   sudo software-update <bootstrap|os-server|hal|openclaw|web>  — OTA update a component
# =============================================================================
set -euo pipefail

# ── config — edit before building ────────────────────────────────────────────
RPI_MODEL="${RPI_MODEL:-5}"  # Target board: 5 = Raspberry Pi 5, 4 = Raspberry Pi 4B
WIFI_COUNTRY="US"           # Wi-Fi regulatory country code
PI_HOSTNAME="autonomous"    # Hostname of the Pi
PI_TIMEZONE="America/New_York"
USERNAME="system"           # Linux user created on the image
PASSWORD="12345"            # Password for the user
OUT_IMG_SIZE="8G"           # Output image size (expands to full SD on first boot)
# OTA metadata URL — per-deployment value, passed in by the Makefile (-e). No
# hardcoded default: fail fast if the caller did not provide one. Baked into the
# image's /root/config/bootstrap.json below.
OTA_METADATA_URL="${OTA_METADATA_URL:?OTA_METADATA_URL is required — build via 'make build OTA_METADATA_URL=...'}"
OTA_SIGNING_PUBLIC_KEY="${OTA_SIGNING_PUBLIC_KEY:-}"
# Device profile baked into this golden image: 1 device type = 1 image. The
# matching devices.<type> artifact (ROBOT.md + SOUL.md) is fetched from OTA
# metadata and staged into DEVICES_DIR/<type>. Forwarded by the Makefile (-e).
# REQUIRED, no default — a golden image must declare which device class it is.
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE is required — build via 'make build DEVICE_TYPE=...'}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"
# Per-image default agent runtime — bakes /root/config/f_r_default_agent, read
# by SeedAgentRuntimeFromGateway (system/device/runtime.go) with PRIORITY over
# ROBOT.md gateway.default, and — unlike gateway.default — it survives a
# Factory Reset (not in factoryreset.go's deviceWipePaths). So an image built
# with DEFAULT_AGENT re-seeds to the SAME runtime after an F_R instead of
# falling back to the device-type-wide ROBOT.md value shared by every build.
# Also gates SSH for intern-v2 — see the SSH stage in the overlay phase.
# OPTIONAL — unset (the default) bakes nothing and behavior is unchanged:
# seeding falls through to ROBOT.md gateway.default exactly as before.
# Mirrors build-orangepi.sh; forwarded by the Makefile (-e) for both targets.
DEFAULT_AGENT="${DEFAULT_AGENT:-}"
# Optional assembly within the device package; empty/standard keeps legacy defaults.
VARIANT="${VARIANT:-}"
if [[ -n "$VARIANT" && ! "$VARIANT" =~ ^[a-z][a-z0-9_-]{0,63}$ ]]; then
  echo "Invalid VARIANT: expected a lowercase name (1-64 characters)" >&2
  exit 1
fi
AP_BAND="${AP_BAND:-2.4}"   # 2.4 or 5 (5 GHz needs supported regulatory domain + chip)
AP_CHANNEL="${AP_CHANNEL:-}" # default: 6 for 2.4 GHz, 36 for 5 GHz
COUNTRY_CODE="US"           # Regulatory country code for hostapd
# ─────────────────────────────────────────────────────────────────────────────

MNT="/mnt/pi"
# Pi 5 uses Trixie (Debian 13); Pi 4 uses Bookworm (Debian 12) for broader compatibility.
# Cache files are named per-OS so Pi 4 and Pi 5 builds don't collide in /input/.
if [ "${RPI_MODEL}" = "4" ]; then
  RPI_IMG_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz"
  RPI_IMG_XZ="/input/raspios-bookworm.img.xz"
else
  RPI_IMG_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-12-04/2025-12-04-raspios-trixie-arm64-lite.img.xz"
  RPI_IMG_XZ="/input/raspios.img.xz"
fi
RPI_IMG="/work/raspios.img"           # extracted source image (temp)
OUT_IMG="/output/golden.img"          # final output image (Phase 2 applies overlay on copy of base)
BASE_IMG="/output/base.img"           # cached base image (Phase 1 output, kept clean for rebuilds)
ORIG_ROOT="/mnt/orig_root"            # mount point for source root partition
ORIG_BOOT="/mnt/orig_boot"            # mount point for source boot partition

# Btrfs mount options:
#   noatime    — don't update access times (reduces SD card writes)
#   compress=zstd:1 — transparent compression, level 1 (fast, saves ~30% space)
# Build-time mount options: commit=5 for frequent flushing (prevents data loss
# on loop devices). The on-device fstab uses Btrfs default commit=30.
BTRFS_BUILD_OPTS="defaults,noatime,compress=zstd:1,commit=5"
# On-device mount options written to fstab
BTRFS_FSTAB_OPTS="defaults,noatime,compress=zstd:1"

LOOP_DEV="" LOOP_BOOT="" LOOP_ROOT=""
OUT_LOOP_DEV="" OUT_LOOP_BOOT="" OUT_LOOP_ROOT=""

# cleanup() — called automatically on EXIT (success or failure)
# Unmounts all bind mounts and detaches loop devices to leave Docker clean
cleanup() {
  echo "==> Cleanup..."
  umount -lf ${MNT}/proc          2>/dev/null || true
  umount -lf ${MNT}/sys           2>/dev/null || true
  umount -lf ${MNT}/dev           2>/dev/null || true
  umount -lf ${MNT}/boot/firmware 2>/dev/null || true
  umount -lf ${MNT}               2>/dev/null || true
  umount -lf ${ORIG_ROOT}         2>/dev/null || true
  umount -lf ${ORIG_BOOT}         2>/dev/null || true
  umount -lf /mnt/btrfs-top       2>/dev/null || true
  [[ -n "${OUT_LOOP_ROOT}" ]] && losetup -d ${OUT_LOOP_ROOT} 2>/dev/null || true
  [[ -n "${OUT_LOOP_BOOT}" ]] && losetup -d ${OUT_LOOP_BOOT} 2>/dev/null || true
  [[ -n "${OUT_LOOP_DEV}"  ]] && losetup -d ${OUT_LOOP_DEV}  2>/dev/null || true
  [[ -n "${LOOP_ROOT}"     ]] && losetup -d ${LOOP_ROOT}     2>/dev/null || true
  [[ -n "${LOOP_BOOT}"     ]] && losetup -d ${LOOP_BOOT}     2>/dev/null || true
  [[ -n "${LOOP_DEV}"      ]] && losetup -d ${LOOP_DEV}      2>/dev/null || true
}
trap cleanup EXIT

# Detach stale loop devices from previous failed builds (Docker may not clean up on kill)
losetup -D 2>/dev/null || true

mkdir -p ${MNT} ${ORIG_ROOT} ${ORIG_BOOT} /output /work

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1: BASE IMAGE (only built when /output/base.img does not exist)
#   Includes: OS download, partitioning, user/network/system config, chroot
#   package installs, AP setup, Node.js/OpenClaw, systemd units, fr-snapshot/
#   fr-rollback. Excludes: OTA metadata fetch, backend binary downloads,
#   web UI download — those are applied in Phase 2 (overlay).
# ══════════════════════════════════════════════════════════════════════════════
# ── base.img cache guard ─────────────────────────────────────────────────────
# base.img is a single cached file, but Phase 1 bakes both DEVICE_TYPE-specific
# content (nginx conf name, hostapd SSID, dnsmasq drop-in, os-server
# Environment=DEVICE_TYPE, the pre-baked runtime CLIs) and board-specific content
# (Pi 4 = Bookworm, Pi 5 = Trixie, plus the Pi-5-only stages). Reusing a cache
# built for a different device type or board silently ships a wrong image, so
# stamp what it was built for and refuse a mismatched reuse.
BASE_STAMP="${BASE_IMG}.built-for"
BASE_STAMP_WANT="${DEVICE_TYPE}/rpi${RPI_MODEL}"
if [[ -f "${BASE_IMG}" ]]; then
  BASE_STAMP_HAVE="$(cat "${BASE_STAMP}" 2>/dev/null || echo unknown)"
  if [[ "${BASE_STAMP_HAVE}" != "${BASE_STAMP_WANT}" ]]; then
    echo "ERROR: cached ${BASE_IMG} was built for '${BASE_STAMP_HAVE}', this build wants '${BASE_STAMP_WANT}'." >&2
    echo "       base.img is device-type- and board-specific. Delete it and rebuild:" >&2
    echo "         rm -f ${BASE_IMG} ${BASE_STAMP}" >&2
    exit 1
  fi
  echo "==> Reusing cached base.img (built for ${BASE_STAMP_HAVE})"
fi

if [[ ! -f "${BASE_IMG}" ]]; then
echo "==> base.img not found — building base image from scratch..."

# ── 1. download or use cached source image ───────────────────────────────────
# The source image is Raspberry Pi OS Lite (arm64, Trixie/Debian 13).
# If already downloaded to /input/raspios.img.xz it is reused to save time.
# The image is copied to /work before extraction to avoid modifying the cache.
if [[ ! -f "${RPI_IMG_XZ}" ]]; then
  echo "==> Downloading RPi OS Lite..."
  mkdir -p /input
  wget -q --show-progress -O ${RPI_IMG_XZ} "${RPI_IMG_URL}"
else
  echo "==> Using cached ${RPI_IMG_XZ}"
fi
echo "==> Extracting..."
cp ${RPI_IMG_XZ} /work/raspios.img.xz
xz -d /work/raspios.img.xz || {
  echo "FATAL: xz extraction failed — cached image may be corrupted"
  echo "Delete input/raspios.img.xz and rebuild"
  exit 1
}
[[ -f "${RPI_IMG}" ]] || { echo "FATAL: ${RPI_IMG} not found after extraction"; exit 1; }

# ── 2. read source image partition offsets ───────────────────────────────────
# The RPi OS image has two partitions:
#   p1: FAT32 boot (firmware, cmdline.txt, config.txt, kernel)
#   p2: ext4 root filesystem
# We attach the image as a loop device and read partition byte offsets so we
# can attach each partition as a separate loop device (no kpartx needed).
echo "==> Reading source image layout..."
LOOP_DEV=$(losetup --find --show ${RPI_IMG})
BOOT_START=$(parted -s ${LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
BOOT_SIZE=$( parted -s ${LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
ROOT_START=$(parted -s ${LOOP_DEV} unit B print | awk '/^ 2/{gsub(/B/,""); print $2}')
echo "    Boot: ${BOOT_START}B size ${BOOT_SIZE}B  Root: ${ROOT_START}B"
LOOP_BOOT=$(losetup --find --show --offset ${BOOT_START} --sizelimit ${BOOT_SIZE} ${RPI_IMG})
LOOP_ROOT=$(losetup --find --show --offset ${ROOT_START} ${RPI_IMG})

# ── 3. copy original content before touching anything ────────────────────────
# We rsync the source partitions to /work BEFORE reformatting anything.
# This is the safe order: copy first, then reformat.
# --no-acls: Docker's overlayfs doesn't support ACLs, skip to avoid errors.
# Excludes: /home (empty in base image), /var/log/journal (large, not needed).
echo "==> Copying source rootfs to /work..."
mount -o ro ${LOOP_ROOT} ${ORIG_ROOT}
mount -o ro ${LOOP_BOOT} ${ORIG_BOOT}
mkdir -p /work/rootfs_backup /work/boot_backup
rsync -aAX --no-acls --exclude=/home --exclude=/var/log/journal \
  ${ORIG_ROOT}/ /work/rootfs_backup/
rsync -aAX ${ORIG_BOOT}/ /work/boot_backup/
umount ${ORIG_ROOT}; umount ${ORIG_BOOT}
# Clear loop vars after detach so cleanup() doesn't double-detach
losetup -d ${LOOP_ROOT}; LOOP_ROOT=""
losetup -d ${LOOP_BOOT}; LOOP_BOOT=""
losetup -d ${LOOP_DEV};  LOOP_DEV=""

# ── 4. create blank output image ─────────────────────────────────────────────
# We create a fresh raw image (not modifying the source) with two partitions:
#   p1: 512 MiB FAT32 boot  — required by Pi bootloader, cannot be Btrfs
#   p2: rest    Btrfs root  — will be expanded to full SD on first boot
# Using msdos (MBR) partition table for maximum Pi compatibility.
echo "==> Creating ${OUT_IMG_SIZE} base image..."
qemu-img create -f raw ${BASE_IMG} ${OUT_IMG_SIZE}
OUT_LOOP_DEV=$(losetup --find --show ${BASE_IMG})
parted -s ${OUT_LOOP_DEV} mklabel msdos
parted -s ${OUT_LOOP_DEV} mkpart primary fat32  1MiB 513MiB
parted -s ${OUT_LOOP_DEV} mkpart primary btrfs 513MiB 100%
parted -s ${OUT_LOOP_DEV} set 1 boot on
OUT_BOOT_START=$(parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
OUT_BOOT_SIZE=$( parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
OUT_ROOT_START=$(parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 2/{gsub(/B/,""); print $2}')
OUT_LOOP_BOOT=$(losetup --find --show --offset ${OUT_BOOT_START} --sizelimit ${OUT_BOOT_SIZE} ${BASE_IMG})
OUT_LOOP_ROOT=$(losetup --find --show --offset ${OUT_ROOT_START} ${BASE_IMG})

# ── 5. format partitions ─────────────────────────────────────────────────────
# Boot partition must be FAT32 (Pi bootloader requirement).
# Root partition is Btrfs with label "rootfs".
echo "==> Formatting boot (FAT32) and root (Btrfs)..."
mkfs.fat -F 32 -n BOOT ${OUT_LOOP_BOOT}
mkfs.btrfs -L rootfs ${OUT_LOOP_ROOT}

# ── 6. create Btrfs subvolume @ ──────────────────────────────────────────────
# Btrfs uses subvolumes to enable snapshots. We create @ as the root subvolume.
# The kernel is told to mount subvol=@ via cmdline.txt (step 17).
# This separation (top-level → @) allows fr-snapshot/fr-rollback to swap
# the entire OS by replacing @ without touching the boot partition.
echo "==> Creating Btrfs @ subvolume..."
mount ${OUT_LOOP_ROOT} ${MNT}
btrfs subvolume create ${MNT}/@
umount ${MNT}

# ── 7. mount subvolume and boot partition ────────────────────────────────────
# Mount @ with production Btrfs options (noatime, zstd compression, 120s commit).
# Also create essential directories that need to exist before rsync.
echo "==> Mounting @ and boot partition..."
mount -o ${BTRFS_BUILD_OPTS},subvol=@ ${OUT_LOOP_ROOT} ${MNT}
mkdir -p ${MNT}/{home,boot/firmware,tmp,proc,sys,dev}
mount ${OUT_LOOP_BOOT} ${MNT}/boot/firmware

# ── 8. restore rootfs from backup ────────────────────────────────────────────
# Copy the source RPi OS rootfs (step 3 backup) into the new Btrfs image.
# This gives us a fully functional Debian Trixie base to build on top of.
echo "==> Restoring rootfs..."
rsync -aAX --no-acls /work/rootfs_backup/ ${MNT}/
rsync -aAX            /work/boot_backup/  ${MNT}/boot/firmware/
sync

# ── 9. disable RPi OS firstrun ───────────────────────────────────────────────
# RPi OS ships a firstrun.sh that runs on first boot to create the default
# "pi" user and configure the system interactively. We disable it because:
#   - We create our own user (step 10)
#   - It would overwrite our custom configuration
#   - It ends with a reboot which we don't want during our setup
echo "==> Disabling RPi OS firstrun..."
rm -f ${MNT}/boot/firmware/firstrun.sh
# Mask ALL RPi OS first-boot services — Trixie has multiple:
#   raspberrypi-sys-mods — old firstrun mechanism
#   userconfig           — Trixie's "Please enter new username" wizard
#   piwiz                — graphical first-boot wizard (if installed)
# Masking (symlink to /dev/null) is stronger than just removing the .wants symlink —
# it prevents the service from being started even manually or by other services.
for SVC in raspberrypi-sys-mods userconfig piwiz; do
  ln -sf /dev/null ${MNT}/etc/systemd/system/${SVC}.service 2>/dev/null || true
done
# Remove the systemd.run= kernel parameter that triggers firstrun on boot
# Note: avoid sed -i on FAT32 — its temp-file + rename corrupts vfat metadata
# and causes the kernel to remount the partition read-only.
if [[ -f ${MNT}/boot/firmware/cmdline.txt ]]; then
  CMDLINE=$(sed 's| systemd\.run[^ ]*||g' ${MNT}/boot/firmware/cmdline.txt)
  echo "${CMDLINE}" > ${MNT}/boot/firmware/cmdline.txt
fi

# ── 10. create user ──────────────────────────────────────────────────────────
# Create user "system" with password "12345" and uid/gid 1000.
# Why remove pi and system first:
#   - "pi" is the RPi OS default user we don't want
#   - "system" may exist as a Debian system account (uid < 1000) causing conflict
# userconf.txt is an RPi OS mechanism: if present on /boot/firmware at first
# boot, it creates the user listed there — acts as a safety net alongside our
# direct passwd/shadow edits.
echo "==> Creating user ${USERNAME}..."
HASH=$(openssl passwd -6 "${PASSWORD}")
# Remove conflicting entries from passwd, shadow, group
sed -i '/^pi:/d;/^system:/d'     ${MNT}/etc/passwd 2>/dev/null || true
sed -i '/^pi:/d;/^system:/d'     ${MNT}/etc/shadow 2>/dev/null || true
sed -i '/^pi:/d;/^system:/d'     ${MNT}/etc/group  2>/dev/null || true
rm -rf ${MNT}/home/pi 2>/dev/null || true
# Write user entries directly (uid=1000, gid=1000, home=/home/system, shell=bash)
echo "${USERNAME}:x:1000:1000:,,,:/home/${USERNAME}:/bin/bash" >> ${MNT}/etc/passwd
echo "${USERNAME}:${HASH}:19000:0:99999:7:::"                  >> ${MNT}/etc/shadow
echo "${USERNAME}:x:1000:"                                      >> ${MNT}/etc/group
# RPi OS firstrun backup mechanism
echo "${USERNAME}:${HASH}" > ${MNT}/boot/firmware/userconf.txt
# Home directory populated from /etc/skel (.bashrc, .profile, .bash_logout)
mkdir -p ${MNT}/home/${USERNAME}
cp -rp ${MNT}/etc/skel/. ${MNT}/home/${USERNAME}/ 2>/dev/null || true
chown -R 1000:1000 ${MNT}/home/${USERNAME}
chmod 755 ${MNT}/home/${USERNAME}
# Add to hardware/system groups for GPIO, USB serial, video, networking access
for GRP in sudo adm video gpio plugdev input netdev dialout; do
  grep -q "^${GRP}:" ${MNT}/etc/group && \
    grep -q "${USERNAME}" <<< $(grep "^${GRP}:" ${MNT}/etc/group) || \
    sed -i "/^${GRP}:/ s/$/,${USERNAME}/" ${MNT}/etc/group 2>/dev/null || true
done
# Passwordless sudo — required for device-ap-mode, fr-rollback, etc.
echo "${USERNAME} ALL=(ALL) NOPASSWD: ALL" > ${MNT}/etc/sudoers.d/010_${USERNAME}-nopasswd
chmod 440 ${MNT}/etc/sudoers.d/010_${USERNAME}-nopasswd

# ── 11. enable SSH ────────────────────────────────────────────────────────────
# Two methods for belt-and-suspenders SSH enablement:
#   1. Empty ssh file on /boot/firmware — RPi OS enables sshd when it sees this
#   2. Direct systemd symlink — ensures sshd starts even without RPi OS hook
echo "==> Enabling SSH..."
touch ${MNT}/boot/firmware/ssh
mkdir -p ${MNT}/etc/systemd/system/multi-user.target.wants
ln -sf /lib/systemd/system/ssh.service \
       ${MNT}/etc/systemd/system/multi-user.target.wants/ssh.service 2>/dev/null || true
# Remove any host keys from the base image so each device generates its own.
# Shared keys across devices would allow MITM attacks between them.
rm -f ${MNT}/etc/ssh/ssh_host_*
# Install a first-boot service that generates unique host keys before sshd starts.
cat > ${MNT}/etc/systemd/system/ssh-keygen-once.service <<'UNIT'
[Unit]
Description=Generate SSH host keys (runs once on first boot)
Before=ssh.service sshd.service
ConditionPathExistsGlob=!/etc/ssh/ssh_host_*_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
ln -sf /etc/systemd/system/ssh-keygen-once.service \
       ${MNT}/etc/systemd/system/multi-user.target.wants/ssh-keygen-once.service

# ── 12. disable NetworkManager ───────────────────────────────────────────────
# RPi OS Trixie ships NetworkManager by default. We replace it with:
#   wpa_supplicant@wlan0 — manages Wi-Fi association in STA mode
#   dhcpcd5             — gets IP address via DHCP in STA mode
# Reason: NM conflicts with hostapd in AP mode and makes AP/STA switching
# unreliable. wpa_supplicant + dhcpcd is simpler and more predictable.
# We mask NM services (symlink to /dev/null) rather than just disabling,
# so they cannot be accidentally started even by other services.
echo "==> Disabling NetworkManager (using wpa_supplicant + dhcpcd)..."
mkdir -p ${MNT}/etc/systemd/system
ln -sf /dev/null ${MNT}/etc/systemd/system/NetworkManager.service
ln -sf /dev/null ${MNT}/etc/systemd/system/NetworkManager-wait-online.service
ln -sf /dev/null ${MNT}/etc/systemd/system/NetworkManager-dispatcher.service
rm -rf ${MNT}/etc/NetworkManager/system-connections/ 2>/dev/null || true

# ── 13. Wi-Fi regulatory country ─────────────────────────────────────────────
# Wi-Fi is blocked by rfkill on RPi OS until a country code is set.
# We set it via multiple mechanisms to ensure at least one takes effect:
#
#   /etc/default/raspi-config  — RPi OS rfkill service reads COUNTRY= from here
#   /etc/default/crda          — older regulatory daemon reads REGDOMAIN=
#   /etc/modprobe.d/cfg80211   — kernel reads this at cfg80211 module load time
#
# firstrun-wifi.sh runs once on first boot (via sysinit.target, very early):
#   - rfkill unblock wifi               — unblocks the radio right now
#   - echo 0 > /var/lib/systemd/rfkill/*:wlan  — persists unblocked state
#   - raspi-config nonint do_wifi_country US   — sets country in all RPi places
# The service is ConditionPathExists-guarded and self-destructs after running.
echo "==> Setting Wi-Fi country ${WIFI_COUNTRY}..."
cat > ${MNT}/etc/default/raspi-config <<RCFG
RPICFG_TO_DISABLE=1
COUNTRY=${WIFI_COUNTRY}
RCFG
echo "REGDOMAIN=${WIFI_COUNTRY}" > ${MNT}/etc/default/crda
mkdir -p ${MNT}/etc/modprobe.d
echo "options cfg80211 ieee80211_regdom=${WIFI_COUNTRY}" > ${MNT}/etc/modprobe.d/cfg80211.conf

cat > ${MNT}/boot/firmware/firstrun-wifi.sh <<'FIRSTRUN'
#!/bin/bash
set +e
rfkill unblock wifi
for f in /var/lib/systemd/rfkill/*:wlan; do echo 0 > "$f"; done
raspi-config nonint do_wifi_country US
rm -f /boot/firmware/firstrun-wifi.sh
FIRSTRUN
chmod +x ${MNT}/boot/firmware/firstrun-wifi.sh

cat > ${MNT}/etc/systemd/system/firstrun-wifi.service <<'UNIT'
[Unit]
Description=Set Wi-Fi country and unblock rfkill (runs once)
After=systemd-rfkill.service
DefaultDependencies=no
ConditionPathExists=/boot/firmware/firstrun-wifi.sh

[Service]
Type=oneshot
ExecStart=/boot/firmware/firstrun-wifi.sh
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
UNIT
mkdir -p ${MNT}/etc/systemd/system/sysinit.target.wants
ln -sf /etc/systemd/system/firstrun-wifi.service \
       ${MNT}/etc/systemd/system/sysinit.target.wants/firstrun-wifi.service

# ── 14. persistent journal ───────────────────────────────────────────────────
# By default RPi OS uses volatile journal (lost on reboot).
# Persistent journal lets you debug boot failures with journalctl -b -1.
# Storage=persistent tells systemd-journald to write to /var/log/journal.
echo "==> Enabling persistent journal..."
mkdir -p ${MNT}/var/log/journal
mkdir -p ${MNT}/etc/systemd/journald.conf.d
cat > ${MNT}/etc/systemd/journald.conf.d/persistent.conf <<'JRN'
[Journal]
Storage=persistent
SystemMaxUse=100M
JRN

# ── 15. hostname ──────────────────────────────────────────────────────────────
echo "==> Setting hostname ${PI_HOSTNAME}..."
echo "${PI_HOSTNAME}" > ${MNT}/etc/hostname
cat > ${MNT}/etc/hosts <<HOSTS
127.0.0.1   localhost
127.0.1.1   ${PI_HOSTNAME}
::1         localhost ip6-localhost ip6-loopback
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
HOSTS

# ── 16. keyboard and locale ───────────────────────────────────────────────────
# Two separate keyboard configs are needed:
#   /etc/default/keyboard  — XKB config used by X11/Wayland and console-setup
#   /etc/vconsole.conf     — systemd-vconsole-setup reads this for tty keymap
#                            Without this, ~ | \ don't work in terminal
# locale.gen lists locales to compile; locale-gen is run in chroot (step 18).
echo "==> Setting keyboard / locale..."
cat > ${MNT}/etc/default/keyboard <<KB
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
KB
echo "KEYMAP=us" > ${MNT}/etc/vconsole.conf
echo "en_US.UTF-8 UTF-8" > ${MNT}/etc/locale.gen
cat > ${MNT}/etc/default/locale <<DEFLOC
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
LANGUAGE=en_US.UTF-8
DEFLOC

# ── 17. fstab ─────────────────────────────────────────────────────────────────
# IMPORTANT: fstab must be written BEFORE entering chroot.
# When apt installs packages inside chroot, it triggers update-initramfs which
# reads /etc/fstab to determine the root filesystem type. If fstab still shows
# the original PARTUUID/ext4, initramfs gets built incorrectly and boot fails
# with "Couldn't identify type of root file system" warnings.
echo "==> Writing fstab (before chroot so initramfs builds correctly)..."
ROOT_UUID=$(blkid -s UUID -o value ${OUT_LOOP_ROOT})
BOOT_UUID=$(blkid -s UUID -o value ${OUT_LOOP_BOOT})
cat > ${MNT}/etc/fstab <<EOF
# subvolid=0 defers to btrfs set-default, so fr-rollback works without editing fstab.
UUID=${ROOT_UUID}  /               btrfs  ${BTRFS_FSTAB_OPTS},subvolid=0  0  0
UUID=${BOOT_UUID}  /boot/firmware  vfat   defaults                  0  2
tmpfs              /tmp            tmpfs  defaults,nosuid,nodev      0  0
EOF

# ── 18. cmdline.txt ───────────────────────────────────────────────────────────
# Tell the kernel to mount Btrfs instead of ext4 and use the @ subvolume.
# We:
#   1. Replace root=PARTUUID=... with root=UUID=... (Btrfs uses UUID not PARTUUID)
#   2. Remove any existing rootfstype= and rootflags= parameters
#   3. Append rootfstype=btrfs rootflags=subvol=@
# Note: tr -s ' ' collapses multiple spaces left by the sed removals.
# Note: avoid sed -i on FAT32 — use read-then-write to prevent vfat remount-ro.
echo "==> Patching cmdline.txt for Btrfs..."
CMDLINE_FILE="${MNT}/boot/firmware/cmdline.txt"
CMDLINE_TXT=$(cat ${CMDLINE_FILE})
CMDLINE_TXT=$(echo "${CMDLINE_TXT}" | sed "s|root=PARTUUID=[^ ]*|root=UUID=${ROOT_UUID}|g")
CMDLINE_TXT=$(echo "${CMDLINE_TXT}" | sed "s|rootfstype=[^ ]*||g; s|rootflags=[^ ]*||g")
CMDLINE_TXT=$(echo "${CMDLINE_TXT}" | tr -s ' ' | sed 's/ *$//')
echo "${CMDLINE_TXT} rootfstype=btrfs rootflags=subvol=@" > ${CMDLINE_FILE}

# ── 19. chroot — install packages and run application stages ─────────────────
# We chroot into the arm64 image using qemu-aarch64-static as the interpreter.
# This allows running arm64 binaries (apt, locale-gen, etc.) on the x86 host.
#
# resolv.conf is temporarily replaced with the host's so apt can reach the
# internet. It is restored after the chroot exits.
#
# Packages are installed WITHOUT --no-install-recommends to ensure all shared
# library dependencies are included. This is critical for btrfs-progs —
# without its recommended deps, /usr/bin/btrfs fails with
# "cannot execute: required file not found" at runtime on the Pi.
echo "==> Entering chroot (arm64 via qemu)..."
echo "${PI_TIMEZONE}" > ${MNT}/etc/timezone
ln -sf /usr/share/zoneinfo/${PI_TIMEZONE} ${MNT}/etc/localtime

# Copy build resources into chroot so they are visible inside (Docker volume mounts
# are not visible inside chroot). Cleaned up after chroot exits.
if [ -d /resources ]; then
  cp -r /resources ${MNT}/resources
fi

cp /usr/bin/qemu-aarch64-static ${MNT}/usr/bin/qemu-aarch64-static
mount --bind /proc ${MNT}/proc
mount --bind /sys  ${MNT}/sys
mount --bind /dev  ${MNT}/dev
cp ${MNT}/etc/resolv.conf ${MNT}/etc/resolv.conf.bak 2>/dev/null || true
cp /etc/resolv.conf ${MNT}/etc/resolv.conf

# Silence debconf "unable to initialize frontend: Dialog" warnings.
# debconf falls back to Noninteractive which is what we want — these lines
# just prevent the noisy warning output during apt installs.
# Pre-seed debconf to prevent interactive prompts for keyboard-configuration
# and set Noninteractive frontend as default.
chroot ${MNT} debconf-set-selections 2>/dev/null <<'DBCONF' || true
debconf debconf/frontend select Noninteractive
keyboard-configuration keyboard-configuration/layoutcode string us
keyboard-configuration keyboard-configuration/xkb-keymap select us
keyboard-configuration keyboard-configuration/variant select English (US)
keyboard-configuration keyboard-configuration/model select Generic 105-key PC (intl.)
DBCONF
cat > ${MNT}/etc/apt/apt.conf.d/99-${DEVICE_TYPE}-silent <<'APT'
Dpkg::Use-Pty "false";
APT

# libgpiod SONAME bumped 2 → 3 between Bookworm (Pi 4) and Trixie (Pi 5).
# Pi 4 branch has only libgpiod2; Pi 5 branch has only libgpiod3.
if [ "${RPI_MODEL}" = "5" ]; then
  LIBGPIOD_PKG="libgpiod3"
else
  LIBGPIOD_PKG="libgpiod2"
fi

DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} apt-get update -qq
DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} apt-get install -y \
  btrfs-progs \
  parted util-linux \
  hostapd dnsmasq nginx \
  curl jq unzip openssl ca-certificates \
  wpasupplicant dhcpcd5 \
  iproute2 iptables iw rfkill \
  cloud-guest-utils \
  wireless-tools net-tools \
  systemd-sysv \
  xvfb xauth chromium chromium-sandbox git \
  fake-hwclock \
  libportaudio2 portaudio19-dev pulseaudio pulseaudio-utils pulseaudio-module-bluetooth ffmpeg \
  alsa-utils libasound2-dev \
  libopenblas0 libgomp1 liblapack3 \
  ${LIBGPIOD_PKG} \
  python3-dev python3-spidev \
  libsm6 libxext6 libgl1 \
  libjpeg-dev zlib1g-dev libfreetype6-dev libopenjp2-7-dev libtiff-dev \
  openresolv \
  avahi-daemon avahi-utils libnss-mdns \
  bluez
# Purge NetworkManager and its dependencies completely
DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} apt-get purge -y --auto-remove \
  network-manager network-manager-gnome 2>/dev/null || true
# Purge cloud-init — it ships with Trixie but interferes with our custom
# fstab/partition setup and can stall boot waiting for metadata services.
DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} apt-get purge -y --auto-remove \
  cloud-init 2>/dev/null || true
rm -rf ${MNT}/etc/cloud ${MNT}/var/lib/cloud
DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} apt-get clean

# Hard verify btrfs binary works inside chroot.
# If this fails, fr-snapshot/fr-rollback will fail on the Pi.
# Most common cause: missing shared libs (install without --no-install-recommends fixes this).
echo "==> Verifying /usr/bin/btrfs..."
chroot ${MNT} /usr/bin/btrfs version || {
  echo "FATAL: /usr/bin/btrfs cannot execute inside chroot"
  echo "Shared lib check:"
  chroot ${MNT} ldd /usr/bin/btrfs || true
  exit 1
}
echo "==> btrfs OK"

# Ensure initramfs includes btrfs module for root mount.
echo "==> Rebuilding initramfs with btrfs support..."

# Force MODULES=most and override any conf.d/ snippets that could reset it.
sed -i 's/^MODULES=.*/MODULES=most/' ${MNT}/etc/initramfs-tools/initramfs.conf
grep -q '^MODULES=' ${MNT}/etc/initramfs-tools/initramfs.conf || \
  echo 'MODULES=most' >> ${MNT}/etc/initramfs-tools/initramfs.conf
# Comment out any conf.d override that sets MODULES=dep
find ${MNT}/etc/initramfs-tools/conf.d/ -type f -exec grep -l '^MODULES=' {} \; 2>/dev/null | \
  while read f; do echo "==> Commenting MODULES override in $(basename $f)"; sed -i 's/^MODULES=/#&/' "$f"; done

# List btrfs explicitly in modules file.
grep -q '^btrfs$' ${MNT}/etc/initramfs-tools/modules 2>/dev/null || \
  echo 'btrfs' >> ${MNT}/etc/initramfs-tools/modules

# Install a hook that force-adds btrfs via manual_add_modules.
# This is the most reliable method — it bypasses MODULES= logic entirely and
# guarantees the module is copied into the initramfs.
cat > ${MNT}/etc/initramfs-tools/hooks/btrfs-force <<'HOOKEOF'
#!/bin/sh
set -e
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0;; esac
. /usr/share/initramfs-tools/hook-functions
manual_add_modules btrfs
HOOKEOF
chmod 755 ${MNT}/etc/initramfs-tools/hooks/btrfs-force

# Verify the kernel module file exists before rebuilding.
for KDIR in ${MNT}/lib/modules/*/; do
  [ -d "${KDIR}" ] || continue
  KVER=$(basename "${KDIR}")
  BTRFS_KO=$(find "${KDIR}" -name 'btrfs.ko*' 2>/dev/null | head -1)
  if [ -n "${BTRFS_KO}" ]; then
    echo "==> Found btrfs module for ${KVER}: $(basename ${BTRFS_KO})"
  else
    echo "WARNING: btrfs.ko not found under /lib/modules/${KVER}"
  fi
done

echo "==> initramfs.conf MODULES setting:"
grep '^MODULES=' ${MNT}/etc/initramfs-tools/initramfs.conf || true
echo "==> /etc/initramfs-tools/modules includes:"
grep -v '^#' ${MNT}/etc/initramfs-tools/modules | grep -v '^$' || true

chroot ${MNT} update-initramfs -u -k all

# Verify btrfs is in the initramfs by checking the archive directly.
# Avoids lsinitramfs chroot issues and SIGPIPE from piping through head.
BTRFS_IN_INITRD=false
for INITRD in ${MNT}/boot/initrd.img-*; do
  [ -f "${INITRD}" ] || continue
  INITRD_NAME=$(basename "${INITRD}")
  echo "==> Checking ${INITRD_NAME} for btrfs..."
  FOUND=false
  for DECOMP in "zstd -dc" "lz4 -dc" "gzip -dc" "xz -dc" "cat"; do
    if ${DECOMP} "${INITRD}" 2>/dev/null | cpio -t 2>/dev/null | grep -q 'btrfs\.ko'; then
      FOUND=true
      break
    fi
  done
  if [ "${FOUND}" = "true" ]; then
    echo "    OK — btrfs.ko present"
    BTRFS_IN_INITRD=true
  else
    echo "    NOT FOUND in ${INITRD_NAME}"
  fi
done
if [ "${BTRFS_IN_INITRD}" = "false" ]; then
  echo "WARNING: btrfs module not found in any initramfs — boot may fail"
fi

# Ensure auto_initramfs=1 is present so the Pi firmware automatically
# loads the correct initramfs for each kernel variant (initramfs_2712 for
# Pi 5, initramfs8 for Pi 4/3). This is the only reliable way — explicit
# "initramfs <file> followkernel" directives only keep the LAST line,
# so a Pi 5 would get the v8 initramfs (wrong modules → btrfs fails).
# Stock Raspberry Pi OS already sets auto_initramfs=1; this is a safety net.
if ! grep -q '^auto_initramfs=1' ${MNT}/boot/firmware/config.txt; then
  echo "" >> ${MNT}/boot/firmware/config.txt
  echo "# Auto-load correct initramfs per kernel (required for btrfs root mount)" >> ${MNT}/boot/firmware/config.txt
  echo "auto_initramfs=1" >> ${MNT}/boot/firmware/config.txt
  echo "==> Added auto_initramfs=1 to config.txt"
else
  echo "==> auto_initramfs=1 already present in config.txt"
fi
# Remove any explicit initramfs lines that would override auto_initramfs
if grep -q '^initramfs ' ${MNT}/boot/firmware/config.txt; then
  sed -i '/^initramfs /d' ${MNT}/boot/firmware/config.txt
  echo "==> Removed explicit initramfs directives (auto_initramfs handles it)"
fi

# Generate locale — must be done inside chroot so locale files exist on the Pi.
# Without this, every login shows "LC_ALL: cannot change locale (en_US.UTF-8)".
chroot ${MNT} /usr/sbin/locale-gen en_US.UTF-8 || true
chroot ${MNT} /usr/sbin/update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 || true

# ── application stages (inside chroot) ───────────────────────────────────────
# All stages run in a single chroot bash session to avoid repeated qemu overhead.
# The heredoc delimiter CHROOT_STAGES is unquoted so ${VAR} from the outer
# script are expanded — this passes config values into the chroot environment.
chroot ${MNT} /bin/bash <<CHROOT_STAGES
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export OTA_METADATA_URL="${OTA_METADATA_URL}"
export AP_BAND="${AP_BAND}"
export AP_CHANNEL="${AP_CHANNEL}"
export COUNTRY_CODE="${COUNTRY_CODE}"

# retry(cmd, max_attempts, delay_seconds) — retries a command on failure
retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-2}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n+1)); echo "Retry \$n/\$max..."; sleep "\$delay"
  done
  echo "ERROR: failed after \$max attempts: \$cmd"; return 1
}

# install_binary_from_zip(url, dest, name)
# Downloads a zip, finds the executable inside, installs to dest.
# Used for bootstrap-server and os-server OTA binaries.
install_binary_from_zip() {
  local url="\$1" dest="\$2" name="\$3"
  local ztmp="/tmp/\${name}-zip.$$" dtmp="/tmp/\${name}-dir.$$"
  mkdir -p "\$dtmp"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$ztmp' '\$url'" 5
  unzip -o -q "\$ztmp" -d "\$dtmp"; rm -f "\$ztmp"
  local bin
  bin=\$(find "\$dtmp" -type f -executable 2>/dev/null | head -1)
  [ -z "\$bin" ] && bin=\$(find "\$dtmp" -type f 2>/dev/null | head -1)
  [ -z "\$bin" ] && { echo "ERROR: no binary in \$url"; exit 1; }
  cp -f "\$bin" "\$dest"; chmod +x "\$dest"; rm -rf "\$dtmp"
}

# ── stage: NTP + fake-hwclock ────────────────────────────────────────────────
# RPi 5 has no battery-backed RTC — clock resets to 1970 on every boot.
# RPi 4 has an RTC but fake-hwclock is still useful as a fallback.
# Without a valid clock, TLS cert validation fails ("not yet valid").
#
# fake-hwclock: saves the clock to /etc/fake-hwclock.data on shutdown and
#   restores it on boot. This ensures the clock is at least as recent as the
#   last shutdown (or build time), which keeps TLS working until NTP syncs.
# systemd-timesyncd: syncs clock via NTP once internet is available.
echo "[stage] NTP + fake-hwclock"
DEBIAN_FRONTEND=noninteractive apt-get install -y systemd-timesyncd
systemctl enable systemd-timesyncd
# Seed fake-hwclock with current build timestamp so first boot has a valid clock.
# fake-hwclock uses a SysV init script (auto-enabled on install) — no systemctl enable needed.
date -u '+%Y-%m-%d %H:%M:%S' > /etc/fake-hwclock.data

# ── stage: WiFi stability (RPi 5 only) ───────────────────────────────────────
# Legacy RPi 5 workaround: disable IPv6 globally to avoid duplicate address
# detection delays on the Pi 5 WiFi chip. Not needed on Pi 4.
if [ "${RPI_MODEL}" = "5" ]; then
echo "[stage] WiFi stability (IPv6 off, Pi 5 only)"
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
sysctl -p /etc/sysctl.d/99-${DEVICE_TYPE}-wifi.conf 2>/dev/null || true
fi

# ── stage: SPI + I2C ──────────────────────────────────────────────────────────
# Enable the SPI and I2C buses in firmware config for hardware peripherals.
# Checks if each dtparam is already present (commented or not) before adding.
echo "[stage] Enable SPI + I2C"
CFG=""
[ -f /boot/firmware/config.txt ] && CFG=/boot/firmware/config.txt
[ -z "\$CFG" ] && [ -f /boot/config.txt ] && CFG=/boot/config.txt
if [ -n "\$CFG" ]; then
  for param in spi=on i2c_arm=on; do
    if grep -qE "^\s*#?\s*dtparam=\$param" "\$CFG" 2>/dev/null; then
      sed -i -E "s/^\s*#\s*(dtparam=\$param)/\1/" "\$CFG" || true
    else
      printf '\n# enabled by lamp build\ndtparam=%s\n' "\$param" >> "\$CFG"
    fi
  done
fi

# NOTE: OTA metadata fetch, backend binary downloads, and web UI download
# are NOT part of the base image — they run in Phase 2 (overlay) so that
# rebuilds with new backend versions are fast.

# ── stage: backend systemd units ─────────────────────────────────────────────
# Systemd service files and software-update script are static (no OTA dependency)
# so they belong in the base image. Binary downloads happen in Phase 2.

cat > /etc/systemd/system/bootstrap.service <<'EOF'
[Unit]
Description=Bootstrap Backend
After=network.target

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

cat > /etc/systemd/system/os-server.service <<EOF
[Unit]
Description=Autonomous OS Server
After=network.target

[Service]
User=root
WorkingDirectory=/root
Environment=DEVICE_TYPE=${DEVICE_TYPE}
Environment=DEVICES_DIR=${DEVICES_DIR}
ExecStart=/usr/local/bin/os-server
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=os-server

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/hal.service <<'EOF'
[Unit]
Description=HAL Hardware Runtime
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hal
Environment="PYTHONPATH=/opt"
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
EOF
systemctl enable bootstrap os-server hal

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

# /usr/local/bin/software-update is NOT written here — it is installed from
# the canonical scripts/provision/software-update on the host, after this
# chroot block (see "install canonical software-update" below). Keeping it
# out of the chroot heredoc also takes it out of the escaping regime.

# ── stage: nginx ──────────────────────────────────────────────────────────────
# nginx serves two things:
#   1. Static web UI at / (setup wizard — downloaded from OTA)
#   2. API proxy at /api/ → localhost:5000 (os-server), /hw/ → :5001 (hal), /gw/ → :18789 (openclaw)
# Captive portal detection endpoints return 204 (no content) to prevent
# the OS from auto-opening a browser when connecting to the AP.
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

echo "[stage] Setup nginx"
rm -f /etc/nginx/sites-enabled/default
mkdir -p /usr/share/nginx/html/setup
# Web UI download moved to Phase 2 (overlay) — only config is in base

cat > /etc/nginx/conf.d/${DEVICE_TYPE}.conf <<'EOF'
upstream backend  { server 127.0.0.1:5000; }
upstream hal   { server 127.0.0.1:5001; }
upstream openclaw { server 127.0.0.1:18789; }

server {
  listen 80 default_server;
  root /usr/share/nginx/html/setup;
  index index.html;
  # Monitor chat sends base64 attachments inside JSON; default 1 MB nginx
  # limit 413s anything past ~700 KB raw. Match scripts/provision/setup.sh.
  client_max_body_size 20M;

  # Security headers — mirror scripts/provision/setup.sh. Defends the device admin UI
  # from clickjacking + MIME-sniffing and shrinks future XSS blast radius.
  # SAMEORIGIN/'self' (not DENY/'none') so Monitor can embed in-house iframes.
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
  # Strict CSP. HAL self-hosts Swagger UI assets under /static/ (served
  # via the Lamp /api/hardware/* proxy), so no CDN whitelist or
  # 'unsafe-inline' script-src is needed. Mirrors scripts/provision/setup.sh.
  add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss: http:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'" always;

  location / { try_files \$uri /index.html; }
  # Interactive shell WebSocket (xterm.js PTY) — must come before generic /api/.
  location = /api/system/shell {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  # Autonomous Buddy (macOS companion) persistent WebSocket.
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
  # Must come BEFORE the generic /api/ block so the WebSocket upgrade headers
  # actually reach os-server.
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

  # Remote code execution endpoint — local callers only (OpenClaw agent on Pi).
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
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  }
  # Captive portal suppression — return 204 so OS does not auto-open browser
  location = /generate_204        { return 204; }
  location = /hotspot-detect.html { return 204; }
  location = /ncsi.txt            { return 204; }
  location = /connecttest.txt     { return 204; }
}
EOF
nginx -t
systemctl enable nginx

# ── stage: AP (Access Point) setup ───────────────────────────────────────────
# Configures the Pi to act as a Wi-Fi access point using:
#   hostapd   — manages the AP (SSID broadcast, client association)
#   dnsmasq   — DHCP server + DNS resolver for connected clients
#   dhcpcd5   — assigns static IP 192.168.100.1 to wlan0 in AP mode
#
# AP SSID is "<device_type>-xxxx" where xxxx = last 4 chars of Pi serial number.
# This is set at runtime by device-ap-mode (not hardcoded in config).
#
# Three helper scripts are installed:
#   device-ap-mode:  switches wlan0 to AP mode (stops STA, starts hostapd)
#   device-sta-mode: switches wlan0 to STA mode (stops AP, starts wpa_supplicant)
#   connect-wifi:    writes wpa_supplicant config and calls device-sta-mode
echo "[stage] Setup AP"

# Ignore any Pi Imager / Armbian WiFi credentials baked into the image. The
# stock wpa_supplicant.conf would otherwise be picked up by the global
# wpa_supplicant.service and pre-empt our per-interface AP/STA flow.
if [ -f /etc/wpa_supplicant/wpa_supplicant.conf ]; then
  mv /etc/wpa_supplicant/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf.bak 2>/dev/null || true
fi

# wpa_supplicant config for wlan0 — country code only, no network block.
# In AP mode we don't connect to any network; this file just provides the
# regulatory domain so the driver allows the AP to broadcast.
mkdir -p /etc/wpa_supplicant
cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf <<EOF
country=\$COUNTRY_CODE
ctrl_interface=DIR=/run/wpa_supplicant
update_config=1
EOF
chmod 600 /etc/wpa_supplicant/wpa_supplicant-wlan0.conf

# Override wpa_supplicant@wlan0 to use our config file (not the global one)
mkdir -p /etc/systemd/system/wpa_supplicant@wlan0.service.d
cat > /etc/systemd/system/wpa_supplicant@wlan0.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/sbin/wpa_supplicant -c /etc/wpa_supplicant/wpa_supplicant-wlan0.conf -i wlan0 -D nl80211,wext
Restart=on-failure
RestartSec=5
EOF

# hostapd config — SSID placeholder "<device_type>-xxxx" is replaced at runtime
# by device-ap-mode using the actual Pi serial number. AP_BAND switches between
# 2.4 GHz (hw_mode=g, default channel 6) and 5 GHz (hw_mode=a + ieee80211ac=1,
# default channel 36). 5 GHz needs a regulatory domain that permits it AND a
# chip/driver that supports AP mode on 5 GHz (Pi 5 yes, OrangePi 4 Pro yes,
# Pi 4 driver-dependent).
if [ "\${AP_BAND}" = "5" ]; then
  HWMODE=a
  CHANNEL="\${AP_CHANNEL:-36}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=\$HWMODE
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
else
  HWMODE=g
  CHANNEL="\${AP_CHANNEL:-6}"
  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${DEVICE_TYPE}-xxxx
hw_mode=\$HWMODE
channel=\$CHANNEL
country_code=\${COUNTRY_CODE}
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF
fi
echo "[stage] AP band=\${AP_BAND} channel=\$CHANNEL"
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

# dnsmasq config — DHCP range 192.168.100.50-150 on wlan0
# address=/#/192.168.100.1 redirects ALL DNS queries to the Pi (captive portal)
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

# dhcpcd: assign static IP 192.168.100.1/24 to wlan0 in AP mode
# nohook wpa_supplicant: prevent dhcpcd from managing wpa_supplicant
sed -i '/^interface wlan0\$/,/^\$/d' /etc/dhcpcd.conf 2>/dev/null || true
cat >> /etc/dhcpcd.conf <<'EOF'

interface wlan0
static ip_address=192.168.100.1/24
nohook wpa_supplicant
EOF

# device-ap-mode: switches wlan0 to Access Point mode
# Called by btrfs-resize-once on first boot and by os-server on demand
cat > /usr/local/bin/device-ap-mode <<'EOF'
#!/bin/bash
set -e
echo "==> Switching to AP mode..."
rfkill unblock wlan 2>/dev/null || true
# Stop STA services
systemctl stop wpa_supplicant@wlan0 2>/dev/null || true
systemctl disable wpa_supplicant@wlan0 2>/dev/null || true
systemctl mask wpa_supplicant@wlan0 2>/dev/null || true
killall wpa_supplicant 2>/dev/null || true
systemctl stop dhcpcd 2>/dev/null || true
# Pi 5: device-tree serial; Pi 4: cpuinfo Serial.
# Non-Pi boards (OrangePi 4 Pro etc.) lack both — fall back to the ethernet
# MAC so the AP SSID still gets a stable per-device suffix.
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
# Lowercase because URLs in the wild aren't case-normalized even though mDNS is.
AP_SSID="${DEVICE_TYPE}-\${SUFFIX_LC}"
[ -f /etc/hostapd/hostapd.conf ] && sed -i "s/^ssid=.*/ssid=\${AP_SSID}/" /etc/hostapd/hostapd.conf

# mDNS <device_type>-<suffix>.local so the web UI can redirect AP→STA via .local.
DEVICE_HOSTNAME="${DEVICE_TYPE}-\${SUFFIX_LC}"
hostnamectl set-hostname "\$DEVICE_HOSTNAME" 2>/dev/null || hostname "\$DEVICE_HOSTNAME" || true
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1 \$DEVICE_HOSTNAME/" /etc/hosts
else
  echo "127.0.1.1 \$DEVICE_HOSTNAME" >> /etc/hosts
fi
systemctl enable avahi-daemon 2>/dev/null || true
systemctl restart avahi-daemon 2>/dev/null || true
# Set regulatory domain from hostapd config
REG=\$(grep '^country_code=' /etc/hostapd/hostapd.conf 2>/dev/null | cut -d= -f2); [ -z "\$REG" ] && REG=US
iw reg set "\$REG" 2>/dev/null || true
# Reset interface to AP mode
ip link set wlan0 down 2>/dev/null || true; sleep 1
iw dev wlan0 set type __ap 2>/dev/null || true
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
ip addr add 192.168.100.1/24 dev wlan0
# Start AP services
systemctl unmask hostapd dnsmasq 2>/dev/null || true
systemctl enable hostapd dnsmasq
systemctl restart hostapd; sleep 2
if ! systemctl is-active --quiet hostapd; then
  systemctl restart hostapd; sleep 3
fi
if ! systemctl is-active --quiet hostapd; then
  echo "ERROR: hostapd failed to start"
  journalctl -u hostapd -n 30 --no-pager
  exit 1
fi
systemctl restart dnsmasq
systemctl restart nginx 2>/dev/null || true
echo "AP SSID: \$AP_SSID  IP: 192.168.100.1"
EOF
chmod +x /usr/local/bin/device-ap-mode

# device-sta-mode: switches wlan0 to Station (client) mode
# Called by connect-wifi after writing wpa_supplicant config
cat > /usr/local/bin/device-sta-mode <<'EOF'
#!/bin/bash
set -e
echo "==> Switching to STA mode..."
rfkill unblock wlan 2>/dev/null || true
# Stop AP services
systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl disable hostapd dnsmasq 2>/dev/null || true
killall hostapd dnsmasq 2>/dev/null || true
# Reset interface to managed mode
ip link set wlan0 down 2>/dev/null || true; sleep 1
iw dev wlan0 set type managed
ip link set wlan0 up; sleep 1
ip addr flush dev wlan0
# Remove AP static IP config from dhcpcd
sed -i '/static ip_address=192.168.100.1\/24/d;/nohook wpa_supplicant/d' /etc/dhcpcd.conf
# Start STA services
systemctl unmask wpa_supplicant@wlan0 2>/dev/null || true
systemctl enable wpa_supplicant@wlan0
systemctl restart wpa_supplicant@wlan0
systemctl enable dhcpcd
systemctl restart dhcpcd
echo "Waiting for IP..."; sleep 5
ip addr show wlan0 | grep -q 'inet ' && \
  echo "Connected: \$(ip -4 addr show wlan0 | awk '/inet/{print \$2}')" || \
  echo "WARNING: no IP — check: wpa_cli status"

# Rebuild /etc/resolv.conf from DHCP lease. On Trixie the dhcpcd
# 20-resolv.conf hook does not fire when switching AP → STA (dnsmasq stops but
# /etc/resolv.conf stays stuck at 'nameserver 127.0.0.1' → every service that
# resolves a domain fails, so os-server never contacts the backend). Read the
# DNS list straight from dhcpcd's current lease and write resolv.conf, with a
# public-DNS fallback for routers that don't hand out DNS via DHCP.
DNS_LINES=""
if command -v dhcpcd >/dev/null 2>&1; then
  LEASE_DNS=\$(dhcpcd -U wlan0 2>/dev/null | awk -F= '/^domain_name_servers=/{print \$2}')
  for ns in \$LEASE_DNS; do
    [ "\$ns" = "127.0.0.1" ] && continue
    DNS_LINES="\${DNS_LINES}nameserver \$ns\\n"
  done
fi
DNS_LINES="\${DNS_LINES}nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n"
printf "%b" "\$DNS_LINES" > /etc/resolv.conf
echo "resolv.conf:"; cat /etc/resolv.conf
echo "STA MODE ENABLED"
EOF
chmod +x /usr/local/bin/device-sta-mode

# connect-wifi: writes wpa_supplicant config then switches to STA mode
# Usage: connect-wifi SSID PASSWORD  (or  connect-wifi SSID  for open networks)
# Called by os-server API endpoint /api/network/setup
cat > /usr/local/bin/connect-wifi <<'EOF'
#!/bin/bash
set -e
WPA_CONF="\${WPA_CONF:-/etc/wpa_supplicant/wpa_supplicant-wlan0.conf}"
COUNTRY="\${COUNTRY:-US}"
[ "\$(id -u)" -ne 0 ] && { echo "Run as root."; exit 1; }
[ \$# -ge 2 ] && { SSID="\$1"; PASS="\$2"; } || \
  { [ \$# -eq 1 ] && { SSID="\$1"; PASS=""; } || \
    { read -r -p "SSID: " SSID; read -r -s -p "Password: " PASS; echo ""; }; }
[ -z "\${SSID:-}" ] && exit 1
[ -f "\$WPA_CONF" ] && { ec=\$(grep -E '^country=' "\$WPA_CONF" 2>/dev/null | head -1 | cut -d= -f2); [ -n "\$ec" ] && COUNTRY="\$ec"; }
mkdir -p "\$(dirname "\$WPA_CONF")"
if [ -z "\$PASS" ]; then
  NET="network={\n\tssid=\"\$SSID\"\n\tkey_mgmt=NONE\n\tscan_ssid=1\n}"
else
  NET="network={\n\tssid=\"\$SSID\"\n\tpsk=\"\$PASS\"\n\tscan_ssid=1\n}"
fi
printf "ctrl_interface=DIR=/run/wpa_supplicant\nupdate_config=1\ncountry=%s\nfast_reauth=1\nap_scan=1\n%b\n" \
  "\$COUNTRY" "\$NET" > "\$WPA_CONF"
chmod 600 "\$WPA_CONF"
/usr/local/bin/device-sta-mode
EOF
chmod +x /usr/local/bin/connect-wifi

# Mask global wpa_supplicant.service — we only use wpa_supplicant@wlan0
# The global service would conflict with our per-interface instance
systemctl mask wpa_supplicant.service 2>/dev/null || true

# ── stage: resolvconf DNS fallback ───────────────────────────────────────────
# Static fallback so /etc/resolv.conf is never completely empty — matters in AP
# mode (hostapd up, no upstream DHCP lease for wlan0) and during the brief
# window between dhcpcd start and the first lease. Appended via openresolv's
# name_servers= so it joins, not replaces, the DHCP-supplied nameservers.
# (Symlink /etc/resolv.conf → /run/resolvconf/resolv.conf is done post-chroot
# because the chroot's resolv.conf is bind-replaced with the host's during
# build and restored after — touching it here would have no effect.)
echo "[stage] resolvconf DNS fallback"
if [ -f /etc/resolvconf.conf ]; then
  grep -q '^name_servers=' /etc/resolvconf.conf || echo 'name_servers="1.1.1.1 8.8.8.8"' >> /etc/resolvconf.conf
else
  echo 'name_servers="1.1.1.1 8.8.8.8"' > /etc/resolvconf.conf
fi

# ── stage: PulseAudio echo cancellation (for HAL mic/speaker) ─────────────
# PulseAudio WebRTC AEC prevents speaker audio from feeding back into the mic.
# This is critical for HAL's voice interaction on the smart lamp hardware.
echo "[stage] PulseAudio echo cancellation"
PULSE_CONF="/etc/pulse/default.pa"
if [ -f "\$PULSE_CONF" ] && ! grep -q "module-echo-cancel" "\$PULSE_CONF"; then
  cat >> "\$PULSE_CONF" <<'PULSE_EOF'

### Echo cancellation (WebRTC AEC) for Lamp
load-module module-echo-cancel source_name=aec_source sink_name=aec_sink aec_method=webrtc aec_args="analog_gain_control=0 digital_gain_control=0" channels=1
set-default-source aec_source
set-default-sink aec_sink
PULSE_EOF
fi

# Keep PulseAudio off the lamp speaker codec. hal's TTS opens this card
# directly via ALSA hw for a persistent low-latency OutputStream, and aplay
# in the music pipeline also writes to it via plug:device_speaker. If PA
# auto-loads module-alsa-card for the same card, the device becomes
# exclusively held and every other consumer fails open with EBUSY.
# ATTR{id} values: sndi2s4 = OrangePi onboard ES8389 codec; wm8960soundcard
# = Raspberry Pi (Seeed wm8960 hat).
cat > /etc/udev/rules.d/91-pulseaudio-hal-ignore.rules <<'UDEV_EOF'
# Keep PulseAudio away from the lamp speaker codec so hal can own it.
SUBSYSTEM=="sound", ATTR{id}=="sndi2s4", ENV{PULSE_IGNORE}="1"
SUBSYSTEM=="sound", ATTR{id}=="wm8960soundcard", ENV{PULSE_IGNORE}="1"
UDEV_EOF

# ── stage: HAL (Python hardware runtime) ─────────────────────────────────
# HAL manages hardware drivers (LED, servo, camera, audio) via a Python
# FastAPI server on port 5001. Uses uv for Python env management.
# Binary download happens in Phase 2 (overlay) — only uv install is in base.
echo "[stage] Install uv (Python package manager for HAL)"
mkdir -p /opt/hal
if ! command -v uv &>/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:\$PATH"
fi

# ── stage: Node.js + OpenClaw ─────────────────────────────────────────────────
# Node.js 22 is required for the OpenClaw CLI (npm global package).
# Chromium + xvfb are already installed above for headless browser support.
echo "[stage] Install Node.js 22"
if ! command -v node &>/dev/null || ! node -v 2>/dev/null | grep -qE '^v(2[2-9]|[3-9][0-9])'; then
  curl -fsSL -H "Cache-Control: no-cache" https://deb.nodesource.com/setup_22.x | DEBIAN_FRONTEND=noninteractive bash -
  DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
fi
echo "node=\$(node -v) npm=\$(npm -v)"

echo "[stage] Install OpenClaw"
OPENCLAW_VERSION="\${OPENCLAW_VERSION:-2026.6.10}"
retry "npm install -g openclaw@\${OPENCLAW_VERSION} --omit=optional" 5
openclaw --version || true

# Onboard as root to create default config/state files before first service start.
# --skip-health: gateway cannot run inside chroot (no systemd, no network).
# Timeout: chroot has no systemd/network/udev — command may hang despite --skip-health.
timeout 60 openclaw onboard --non-interactive --accept-risk --skip-health || {
  echo "WARNING: openclaw onboard timed out or failed (non-fatal in chroot)"
  echo "Gateway will complete onboarding on first boot with network access."
}

# Install external plugins baked into the golden image.
openclaw plugins install @openclaw/discord@\${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: discord plugin install failed (non-fatal)"
openclaw plugins install @openclaw/slack@\${OPENCLAW_VERSION} --force 2>&1 || echo "WARN: slack plugin install failed (non-fatal)"

# Resolve chromium path for headless browser support
CHROME_PATH=\$(command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || echo /usr/bin/chromium)
OPENCLAW_BIN=\$(command -v openclaw)

# Write openclaw.service systemd unit
cat > /etc/systemd/system/openclaw.service <<OCUNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
Environment="HOME=/root"
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
OCUNIT
systemctl enable openclaw

# ── stage: Hermes CLI binary pre-bake ────────────────────────────────────────
# Run the same installer stages as Hermes' install.sh, minus gateway/config/
# migrate. Baking the binary + venv here means switch-runtime's install.sh skips
# the slow git-clone + uv-sync on the device (its stages fast-path because they
# detect the existing install). Everything else (service unit, presync, claw
# migrate) stays owned by install.sh at actual switch time via Go switch-runtime.
# Mirrors build-orangepi.sh — keep the two in sync.
echo "[stage] hermes CLI binary pre-bake"
HERMES_INSTALLER=\$(mktemp)
retry "curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o '\$HERMES_INSTALLER'" 5
for stage in prerequisites repository venv python-deps path config; do
  echo "[hermes-prebake] stage: \${stage}"
  bash "\$HERMES_INSTALLER" --stage "\$stage" --non-interactive
done
rm -f "\$HERMES_INSTALLER"
echo "git" >/usr/local/lib/hermes-agent/.install_method 2>/dev/null || true
hermes --version || true

# ── stage: Hermes gateway unit pre-bake (created, left DISABLED) ─────────────
# Pre-baking the binary above is not enough: IsReady()/device setup wait on the
# hermes-gateway HTTP /health, which needs the hermes-gateway.service unit to
# exist. switch-runtime/install.sh creates it on the first switch to hermes — but
# a hand-edited config.json agent_runtime=hermes flip never runs that path, so the
# unit is absent, the gateway never starts, WaitForAgentReady times out,
# SetUpCompleted stays false, the device falls back to AP mode, and the symptom
# reads as "WiFi won't connect". Create the unit here so it is ready to start.
# We do NOT enable it at boot: openclaw is the default active runtime and enabling
# both would run two agents. os-server's EnsureOnboarding and switch-runtime
# enable+start it when hermes actually becomes active. Best-effort: chroot has no
# running systemd, so if the CLI cannot write the unit here, EnsureOnboarding
# installs it at runtime instead — that is why we ship both.
echo "[stage] hermes-gateway.service unit pre-bake (created, left disabled)"
if command -v hermes >/dev/null 2>&1; then
  # Seed .env with API server keys before gateway install — mirrors install.sh.
  # Without this the gateway starts with API_SERVER_ENABLED unset and os-server's
  # Bearer auth fails (401 on every turn).
  HERMES_DIR="/root/.hermes"
  ENV_FILE="\$HERMES_DIR/.env"
  HERMES_API_SERVER_KEY="hermes-local-api-key"
  mkdir -p "\$HERMES_DIR"
  touch "\$ENV_FILE"
  for k in API_SERVER_ENABLED API_SERVER_KEY API_SERVER_CORS_ORIGINS; do
    sed -i "/^\${k}=/d" "\$ENV_FILE"
  done
  [ -s "\$ENV_FILE" ] && [ -n "\$(tail -c1 "\$ENV_FILE")" ] && printf '\n' >>"\$ENV_FILE"
  printf '%s\n' \\
    "API_SERVER_ENABLED=true" \\
    "API_SERVER_KEY=\$HERMES_API_SERVER_KEY" \\
    "API_SERVER_CORS_ORIGINS=http://localhost:3000" >>"\$ENV_FILE"
  echo "[stage] hermes .env pre-seeded (API_SERVER_ENABLED + API_SERVER_KEY + CORS)"
  set +o pipefail
  yes y | hermes gateway install --system --run-as-user root \\
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

# ── stage: Codex + Claude Code + PicoClaw + OpenCode CLI pre-bake ────────────
# Same fast-path trick as the Hermes binary pre-bake above: bake ONLY the raw
# CLI binaries — no systemd unit, no presync/onboard, no enable/start. Those
# stay owned entirely by each backend's own install.sh (runtimes/codex,
# runtimes/claudecode, runtimes/picoclaw, runtimes/opencode — embedded in
# os-server, fetched by switch-runtime on the first real switch to that runtime);
# each detects the binary already present and skips its own download. Versions
# are pinned here just like CODEX_VERSION/PICO_VERSION/OPENCODE_VERSION in their
# respective install.sh — bump both places together when upgrading. Gated to
# lamp + intern-v2, the two device types whose "Select frameworks" web UI
# actually offers these as switchable runtimes; other DEVICE_TYPEs stay unbaked
# until their own UI exposes the picker. Same gate as build-orangepi.sh.
if [ "${DEVICE_TYPE}" = "intern-v2" ] || [ "${DEVICE_TYPE}" = "lamp" ]; then
  echo "[stage] codex CLI binary pre-bake (${DEVICE_TYPE})"
  CODEX_VERSION="\${CODEX_VERSION:-rust-v0.142.5}"
  CODEX_ASSET="codex-aarch64-unknown-linux-musl.tar.gz"
  CODEX_TMP=\$(mktemp -d)
  retry "curl -fsSL 'https://github.com/openai/codex/releases/download/\${CODEX_VERSION}/\${CODEX_ASSET}' -o '\$CODEX_TMP/\$CODEX_ASSET'" 5
  tar -xzf "\$CODEX_TMP/\$CODEX_ASSET" -C "\$CODEX_TMP"
  install -m 0755 "\$CODEX_TMP/\${CODEX_ASSET%.tar.gz}" /usr/local/bin/codex
  rm -rf "\$CODEX_TMP"
  codex --version || true

  echo "[stage] Claude Code CLI binary pre-bake (${DEVICE_TYPE})"
  retry "curl -fsSL https://claude.ai/install.sh | bash" 3 10
  # Installer's landing path has drifted historically (~/.local/bin, ~/.claude/bin,
  # /usr/local/bin). Belt-and-suspenders: probe known locations, then symlink to
  # /usr/local/bin/claude so downstream services find it in PATH.
  CLAUDE_BIN=/usr/local/bin/claude
  if [ ! -x "\$CLAUDE_BIN" ]; then
    for CANDIDATE in /root/.local/bin/claude /root/.claude/bin/claude /root/.claude/local/claude; do
      if [ -x "\$CANDIDATE" ]; then
        install -m 0755 "\$CANDIDATE" "\$CLAUDE_BIN"
        echo "[stage] copied \$CANDIDATE → \$CLAUDE_BIN"
        break
      fi
    done
  fi
  if [ ! -x "\$CLAUDE_BIN" ]; then
    # Last resort: whatever `command -v` finds
    SRC="\$(command -v claude 2>/dev/null || true)"
    if [ -x "\$SRC" ] && [ "\$SRC" != "\$CLAUDE_BIN" ]; then
      install -m 0755 "\$SRC" "\$CLAUDE_BIN"
      echo "[stage] copied \$SRC → \$CLAUDE_BIN"
    fi
  fi
  "\$CLAUDE_BIN" --version || true

  echo "[stage] picoclaw CLI binary pre-bake (${DEVICE_TYPE})"
  PICO_VERSION="\${PICO_VERSION:-v0.3.1-fixvision}"
  PICO_ASSET="picoclaw-linux-arm64"
  PICO_TMP=\$(mktemp)
  retry "curl -fsSL 'https://github.com/autonomous-ai/picoclaw/releases/download/\${PICO_VERSION}/\${PICO_ASSET}' -o '\$PICO_TMP'" 5
  install -m 0755 "\$PICO_TMP" /usr/local/bin/picoclaw
  rm -f "\$PICO_TMP"
  # picoclaw has no --version flag (errors "unknown flag") — version is a
  # subcommand that also prints an ANSI banner.
  picoclaw --no-color version || true

  echo "[stage] opencode CLI binary pre-bake (${DEVICE_TYPE})"
  # Mirrors runtimes/opencode/install.sh exactly (same pinned version, same
  # official installer, same forced install dir) so switch-runtime's install.sh
  # detects the binary already present at the expected version and skips its own
  # download. OPENCODE_INSTALL_DIR must prefix the bash process running the
  # installer, not curl — env vars only bind to the command they prefix.
  OPENCODE_VERSION="\${OPENCODE_VERSION:-1.18.4}"
  OPENCODE_BIN=/usr/local/bin/opencode
  retry "curl -fsSL https://opencode.ai/install | OPENCODE_INSTALL_DIR=/usr/local/bin bash -s -- --version '\${OPENCODE_VERSION#v}'" 3 10
  # Belt-and-suspenders, same as install.sh: the official installer has a history
  # of ignoring OPENCODE_INSTALL_DIR and dropping the binary at its own default
  # (~/.opencode/bin) while only patching PATH into ~/.bashrc — which a
  # non-interactive shell like this one never sources.
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
else
  echo "[stage] DEVICE_TYPE=${DEVICE_TYPE} — skipping codex/claudecode/picoclaw/opencode pre-bake (no runtime picker in its web UI)"
fi

systemctl daemon-reload
echo "[stage] All stages complete"
CHROOT_STAGES

# ── install canonical software-update ────────────────────────────────────────
# The on-device OTA updater is one file in the repo (scripts/provision/
# software-update), staged into /input by the imager Makefile and installed
# here from the host — NOT written by heredoc inside the chroot. setup.sh
# inlines the same file at release time, so all fleets carry one version.
echo "[stage] install /usr/local/bin/software-update (canonical)"
if [ ! -f /input/software-update ]; then
  echo "ERROR: /input/software-update missing — run via 'make build' (it stages the file)" >&2
  exit 1
fi
install -m 0755 /input/software-update "${MNT}/usr/local/bin/software-update"

# Restore resolv.conf and clean up chroot artifacts
mv ${MNT}/etc/resolv.conf.bak ${MNT}/etc/resolv.conf 2>/dev/null || true

# Hand /etc/resolv.conf over to resolvconf so the static fallback in
# /etc/resolvconf.conf (1.1.1.1 / 8.8.8.8) is always present even with no
# DHCP lease (e.g. AP mode). Only rewrite when the restored file is a plain
# file with no nameservers — leave RPi OS / systemd-resolved-managed setups
# alone. resolvconf -u will run on first boot when dhcpcd starts.
if [ -e ${MNT}/etc/resolv.conf ] && [ ! -L ${MNT}/etc/resolv.conf ]; then
  if ! grep -qE '^[[:space:]]*nameserver[[:space:]]+' ${MNT}/etc/resolv.conf 2>/dev/null; then
    echo "==> Linking /etc/resolv.conf -> /run/resolvconf/resolv.conf in image"
    rm -f ${MNT}/etc/resolv.conf
    mkdir -p ${MNT}/run/resolvconf
    ln -sf /run/resolvconf/resolv.conf ${MNT}/etc/resolv.conf
  fi
fi

# Kill any processes still running inside the chroot (e.g. sshd, dbus-daemon
# spawned by apt post-install triggers). If these survive into the golden image
# they hold ports/sockets on first boot and block systemd from starting services.
echo "==> Killing stale chroot processes..."
for pid in $(lsof -t +D ${MNT} 2>/dev/null || true); do
  kill -9 "$pid" 2>/dev/null || true
done
# Also kill anything whose root is the chroot mount
fuser -k -M ${MNT} 2>/dev/null || true
# Remove stale PID files that could confuse systemd on first boot
rm -f ${MNT}/run/sshd.pid ${MNT}/run/dbus/pid 2>/dev/null || true
rm -rf ${MNT}/run/lock/* 2>/dev/null || true

umount ${MNT}/dev
umount ${MNT}/sys
umount ${MNT}/proc
rm -f ${MNT}/usr/bin/qemu-aarch64-static
rm -rf ${MNT}/resources

# ── 20. btrfs-resize-once service ────────────────────────────────────────────
# This service runs ONCE on first boot after flashing to SD card.
# Purpose: the golden.img is 8G but the SD card may be 32/64/128G+.
# The service expands the Btrfs partition and filesystem to fill the SD card,
# then immediately starts AP mode so the device is ready to use.
#
# Why growpart instead of parted?
#   parted -s shows an interactive "Partition in use, continue?" prompt even
#   with -s flag when the partition is mounted. growpart is specifically
#   designed for resizing mounted partitions with no prompts.
#
# Self-destructs by:
#   1. systemctl disable (removes symlink so it won't run again)
#   2. rm -f itself (ConditionPathExists check also prevents re-run)
echo "==> Installing btrfs-resize-once service..."
cat > ${MNT}/usr/local/bin/btrfs-resize-once <<'SCRIPT'
#!/bin/bash
# Do NOT use set -e here — partial failures should not prevent AP mode from starting
set -uo pipefail
log() { echo "==> $*"; }
fail() { echo "ERROR: $*" >&2; }

# Determine root partition (strip Btrfs subvolume path like [/@] from findmnt output)
ROOT_PART=$(findmnt -n -o SOURCE / | sed 's/\[.*//')
[ -z "${ROOT_PART}" ] && { fail "cannot determine root partition"; exit 1; }
log "Root partition: ${ROOT_PART}"

# Derive parent disk and partition number from device name pattern:
#   mmcblk0p2 → mmcblk0 + 2  (SD card / eMMC)
#   nvme0n1p2 → nvme0n1 + 2  (NVMe SSD)
#   sda2      → sda + 2       (USB SSD / SATA)
if [[ "${ROOT_PART}" =~ ^/dev/(mmcblk[0-9]+)p([0-9]+)$ ]]; then
  DISK="/dev/${BASH_REMATCH[1]}"; PART_NUM="${BASH_REMATCH[2]}"
elif [[ "${ROOT_PART}" =~ ^/dev/(nvme[0-9]+n[0-9]+)p([0-9]+)$ ]]; then
  DISK="/dev/${BASH_REMATCH[1]}"; PART_NUM="${BASH_REMATCH[2]}"
elif [[ "${ROOT_PART}" =~ ^/dev/([a-z]+)([0-9]+)$ ]]; then
  DISK="/dev/${BASH_REMATCH[1]}"; PART_NUM="${BASH_REMATCH[2]}"
else
  fail "unrecognised partition format: ${ROOT_PART}"; exit 1
fi
log "Disk: ${DISK}  Partition: ${PART_NUM}"
[[ -b "${DISK}" ]]      || { fail "disk ${DISK} not found"; exit 1; }
[[ -b "${ROOT_PART}" ]] || { fail "partition ${ROOT_PART} not found"; exit 1; }

# Expand partition to fill disk using growpart (handles mounted partitions)
log "Expanding partition ${PART_NUM} on ${DISK}..."
if command -v growpart &>/dev/null; then
  growpart "${DISK}" "${PART_NUM}" || log "growpart: partition may already be at max size"
else
  # Fallback: pipe 'y' to suppress the "partition in use" interactive prompt
  echo y | parted ---pretend-input-tty "${DISK}" resizepart "${PART_NUM}" 100% || {
    parted -s "${DISK}" print fix 2>/dev/null || true
    echo y | parted ---pretend-input-tty "${DISK}" resizepart "${PART_NUM}" 100% || \
      { fail "resize partition failed"; exit 1; }
  }
fi

# Notify kernel of new partition size
udevadm settle 2>/dev/null || true
blockdev --rereadpt "${DISK}" 2>/dev/null || true
sleep 2

# Expand Btrfs filesystem to fill the newly enlarged partition
log "Resizing Btrfs filesystem..."
btrfs filesystem resize max / || { fail "btrfs resize failed"; exit 1; }
log "Resize complete:"; df -h /

# @factory is taken at build time (step 23) and never overwritten.
# Btrfs resize is filesystem-level, not subvolume-level, so @factory
# automatically gets the full SD card space without re-snapshotting.

# Self-destruct — remove service and script so this never runs again
systemctl disable btrfs-resize-once.service 2>/dev/null || true
rm -f /usr/local/bin/btrfs-resize-once
log "Done. btrfs-resize-once complete."
log "Run 'sudo device-ap-mode' to start the hotspot when ready."
SCRIPT
chmod +x ${MNT}/usr/local/bin/btrfs-resize-once

cat > ${MNT}/etc/systemd/system/btrfs-resize-once.service <<'UNIT'
[Unit]
Description=Resize Btrfs + take factory snapshot (runs once on first boot)
After=local-fs.target
After=systemd-udevd.service
ConditionPathExists=/usr/local/bin/btrfs-resize-once

[Service]
Type=oneshot
ExecStart=/usr/local/bin/btrfs-resize-once
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
ln -sf /etc/systemd/system/btrfs-resize-once.service \
       ${MNT}/etc/systemd/system/multi-user.target.wants/btrfs-resize-once.service

# ── 21. fr-snapshot and fr-rollback ──────────────────────────────────────────
# These scripts implement the factory reset feature using Btrfs snapshots.
#
# fr-snapshot: saves the current system state as a read-only @factory snapshot
#   - Detects the currently mounted root subvolume (@ or @restore-<ts>)
#   - Uses 'btrfs subvolume show' to verify it is a real subvolume (not just dir)
#   - If @factory already exists as a valid subvolume, deletes it first
#   - Creates @factory as read-only (-r flag) so it cannot be accidentally modified
#
# fr-rollback: restores the system to the @factory snapshot state
#   - Pre-flight: verifies btrfs binary works (shared libs present)
#   - Prints subvolume list so failures are diagnosable from journal
#   - Deletes old @restore-* subvolumes (skips currently mounted root)
#   - Snapshots @factory -> @restore-<ts> (writable copy)
#   - Sets @restore-<ts> as btrfs default subvolume (by ID)
#   - Updates cmdline.txt rootflags to point to @restore-<ts>
#   - Reboots — kernel mounts @restore-<ts> as /
#   - Safe to run repeatedly: never deletes the currently mounted root
echo "==> Installing fr-snapshot and fr-rollback..."

cat > ${MNT}/usr/local/bin/fr-snapshot <<'SCRIPT'
#!/bin/bash
set -euo pipefail

BTRFS=/usr/bin/btrfs
TOP=/mnt/btrfs-top

# Verify btrfs binary has all shared libs — write output to /tmp not /dev/null
# (/dev/null may not exist if /dev is not mounted)
$BTRFS version > /tmp/btrfs-ver 2>&1 || {
  echo "ERROR: /usr/bin/btrfs cannot execute"
  cat /tmp/btrfs-ver
  exit 1
}

DEV=$(findmnt -n -o SOURCE / | cut -d'[' -f1)
[ -z "$DEV" ] && { echo "ERROR: cannot find root device"; exit 1; }

# Detect the currently mounted root subvolume name.
# After a rollback the root may be @restore-<ts> instead of @.
# findmnt shows e.g. /dev/mmcblk0p2[/@] or /dev/mmcblk0p2[/@restore-1234]
CURRENT_SUB=$(findmnt -n -o SOURCE / | sed 's/.*\[\/\(.*\)\]/\1/')
[ -z "$CURRENT_SUB" ] && CURRENT_SUB="@"
echo "==> Current root subvolume: $CURRENT_SUB"

mkdir -p $TOP
mount -o subvolid=5 $DEV $TOP

# If @factory exists as a valid subvolume, delete it before creating new one
if $BTRFS subvolume show "$TOP/@factory" > /dev/null 2>&1; then
  echo "==> Removing old @factory..."
  $BTRFS subvolume delete "$TOP/@factory"
elif [ -d "$TOP/@factory" ]; then
  # @factory exists as a plain directory (e.g. from a bad previous run) — remove it
  echo "WARNING: @factory is not a Btrfs subvolume, removing plain directory..."
  rm -rf "$TOP/@factory"
fi

# Verify the current root is a real Btrfs subvolume before snapshotting
$BTRFS subvolume show "$TOP/$CURRENT_SUB" > /dev/null 2>&1 || {
  umount $TOP
  echo "ERROR: $CURRENT_SUB is not a valid Btrfs subvolume"
  exit 1
}

echo "==> Snapshotting $CURRENT_SUB -> @factory (readonly)..."
$BTRFS subvolume snapshot -r "$TOP/$CURRENT_SUB" "$TOP/@factory"
echo "==> Current subvolumes:"
$BTRFS subvolume list $TOP  # list BEFORE unmount
umount $TOP
echo "Done. @factory saved. Run 'sudo fr-rollback' to restore."
SCRIPT

cat > ${MNT}/usr/local/bin/fr-rollback <<'SCRIPT'
#!/bin/bash
# fr-rollback — restore @factory using btrfs set-default
#
# Uses a timestamped subvolume name (@restore-<epoch>) so the script is safe
# to run repeatedly — the currently mounted root (which may itself be a
# previous @restore-*) is never deleted while in use.
#
# The approach:
#   1. Snapshot @factory -> @restore-<ts> (a fresh writable copy)
#   2. Set @restore-<ts> as the new default subvolume (by its ID)
#   3. Update cmdline.txt rootflags to point to @restore-<ts>
#   4. Reboot — kernel mounts @restore-<ts> as /
#
# Old @restore-* subvolumes from previous rollbacks remain on disk.
# To clean up: mount top-level (subvolid=5) and delete them manually.
# To re-snapshot after rollback: run fr-snapshot again.

BTRFS=/usr/bin/btrfs
TOP=/mnt/btrfs-top
CMDLINE=/boot/firmware/cmdline.txt
RESTORE_NAME="@restore-$(date +%s)"

err() { echo "ERROR: $1" >&2; }
log() { echo "==> $1"; }

# ── pre-flight ────────────────────────────────────────────────────────────────
$BTRFS version > /tmp/btrfs-ver 2>&1 || {
  err "/usr/bin/btrfs cannot execute:"
  cat /tmp/btrfs-ver
  exit 1
}

DEV=$(findmnt -n -o SOURCE / | cut -d'[' -f1)
[ -z "$DEV" ] && { err "cannot find root device"; exit 1; }

# ── mount top-level and verify ────────────────────────────────────────────────
mkdir -p $TOP
if ! mount -o subvolid=5 $DEV $TOP; then
  err "cannot mount Btrfs top level"; exit 1
fi

log "Current subvolumes:"
$BTRFS subvolume list $TOP

if ! $BTRFS subvolume show "$TOP/@factory" > /dev/null 2>&1; then
  err "@factory is not a valid Btrfs subvolume"
  err "Run 'sudo fr-snapshot' first"
  umount $TOP; exit 1
fi

# ── delete old @restore-* subvolumes ──────────────────────────────────────────
# The currently mounted root may be an old @restore-*, so skip it.
CURRENT_SUB=$(findmnt -n -o SOURCE / | sed 's/.*\[\/\(.*\)\]/\1/')
for OLD in "$TOP"/@restore-*; do
  [ -d "$OLD" ] || continue
  OLD_NAME=$(basename "$OLD")
  [ "$OLD_NAME" = "$CURRENT_SUB" ] && { log "Skipping $OLD_NAME (currently mounted)"; continue; }
  log "Deleting old subvolume $OLD_NAME..."
  $BTRFS subvolume delete "$OLD" || err "failed to delete $OLD_NAME (non-fatal)"
done

# ── snapshot @factory -> @restore-<ts> ────────────────────────────────────────
log "Snapshotting @factory -> $RESTORE_NAME..."
if ! $BTRFS subvolume snapshot "$TOP/@factory" "$TOP/$RESTORE_NAME"; then
  err "Failed to create $RESTORE_NAME snapshot"
  umount $TOP; exit 1
fi

# ── get subvolume ID ──────────────────────────────────────────────────────────
RESTORE_ID=$($BTRFS subvolume show "$TOP/$RESTORE_NAME" | awk '/Subvolume ID:/{print $3}')
[ -z "$RESTORE_ID" ] && { err "cannot get $RESTORE_NAME subvolume ID"; umount $TOP; exit 1; }
log "$RESTORE_NAME subvolume ID: $RESTORE_ID"

# ── set as default subvolume ──────────────────────────────────────────────────
log "Setting $RESTORE_NAME as default subvolume..."
if ! $BTRFS subvolume set-default "$RESTORE_ID" $TOP; then
  err "Failed to set default subvolume"
  $BTRFS subvolume delete "$TOP/$RESTORE_NAME"
  umount $TOP; exit 1
fi

log "Current subvolumes:"
$BTRFS subvolume list $TOP

umount $TOP

# ── update cmdline.txt ────────────────────────────────────────────────────────
log "Updating cmdline.txt rootflags=subvol=$RESTORE_NAME..."
CMDLINE_TMP=$(sed "s|rootflags=subvol=[^ ]*|rootflags=subvol=$RESTORE_NAME|" $CMDLINE)
echo "$CMDLINE_TMP" > $CMDLINE
log "cmdline.txt: $(cat $CMDLINE)"

log "Rollback ready. Rebooting in 3 seconds..."
sleep 3
/sbin/reboot
SCRIPT

chmod 700 ${MNT}/usr/local/bin/fr-snapshot
chmod 700 ${MNT}/usr/local/bin/fr-rollback

# ── save base image ─────────────────────────────────────────────────────────
# Unmount everything. Phase 1 builds directly into base.img, no copy needed.
echo "==> Finalizing base image..."
btrfs filesystem sync ${MNT} 2>/dev/null || true
sync
umount ${MNT}/boot/firmware
umount ${MNT}
sync
losetup -d ${OUT_LOOP_ROOT} 2>/dev/null || true; OUT_LOOP_ROOT=""
losetup -d ${OUT_LOOP_BOOT} 2>/dev/null || true; OUT_LOOP_BOOT=""
losetup -d ${OUT_LOOP_DEV}  2>/dev/null || true; OUT_LOOP_DEV=""
echo "${BASE_STAMP_WANT}" > "${BASE_STAMP}"
echo "==> base.img ready ($(du -h ${BASE_IMG} | cut -f1), built for ${BASE_STAMP_WANT})"

else
  echo "==> Using cached ${BASE_IMG}, skipping base build..."
fi  # end PHASE 1

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: OVERLAY (always runs — applies frequently changing parts)
#   OTA metadata fetch, backend binary downloads, web UI download.
#   Then: @factory snapshot + QC checks.
# ══════════════════════════════════════════════════════════════════════════════
echo "==> Phase 2: applying overlay (OTA + backend + web)..."
cp ${BASE_IMG} ${OUT_IMG}

# Attach golden.img and read partition layout
OUT_LOOP_DEV=$(losetup --find --show ${OUT_IMG})
OUT_BOOT_START=$(parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $2}')
OUT_BOOT_SIZE=$( parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 1/{gsub(/B/,""); print $4}')
OUT_ROOT_START=$(parted -s ${OUT_LOOP_DEV} unit B print | awk '/^ 2/{gsub(/B/,""); print $2}')
OUT_LOOP_BOOT=$(losetup --find --show --offset ${OUT_BOOT_START} --sizelimit ${OUT_BOOT_SIZE} ${OUT_IMG})
OUT_LOOP_ROOT=$(losetup --find --show --offset ${OUT_ROOT_START} ${OUT_IMG})

# Mount Btrfs @ subvolume + boot
mount -o ${BTRFS_BUILD_OPTS},subvol=@ ${OUT_LOOP_ROOT} ${MNT}
mount ${OUT_LOOP_BOOT} ${MNT}/boot/firmware

# Set up chroot environment for overlay stages
cp /usr/bin/qemu-aarch64-static ${MNT}/usr/bin/qemu-aarch64-static
mount --bind /proc ${MNT}/proc
mount --bind /sys  ${MNT}/sys
mount --bind /dev  ${MNT}/dev
cp ${MNT}/etc/resolv.conf ${MNT}/etc/resolv.conf.bak 2>/dev/null || true
cp /etc/resolv.conf ${MNT}/etc/resolv.conf

# ── overlay chroot: OTA metadata + backend binaries + web UI ─────────────────
# Ensure jq/curl/unzip are available — they should be in the base image from
# Phase 1 apt install, but a stale cached base.img might be missing them.
DEBIAN_FRONTEND=noninteractive TERM=xterm chroot ${MNT} bash -c \
  'command -v jq &>/dev/null && command -v curl &>/dev/null && command -v unzip &>/dev/null || \
   { apt-get update -qq && apt-get install -y jq curl unzip ca-certificates; apt-get clean; }'

chroot ${MNT} /bin/bash <<OVERLAY_STAGES
set -euo pipefail
trap 'echo "OVERLAY ERROR: command failed at line \$LINENO (exit code \$?): \$BASH_COMMAND"' ERR
export DEBIAN_FRONTEND=noninteractive
export OTA_METADATA_URL="${OTA_METADATA_URL}"
export DEVICE_TYPE="${DEVICE_TYPE}"
export VARIANT="${VARIANT}"
export DEVICES_DIR="${DEVICES_DIR}"
export PI5_NO_AUDIO="${PI5_NO_AUDIO:-0}"
export DEFAULT_AGENT="${DEFAULT_AGENT}"

retry() {
  local cmd="\$1" max="\${2:-5}" delay="\${3:-2}" n=0
  until [ "\$n" -ge "\$max" ]; do
    eval "\$cmd" && return 0
    n=\$((n+1)); echo "Retry \$n/\$max..."; sleep "\$delay"
  done
  echo "ERROR: failed after \$max attempts: \$cmd"; return 1
}

install_binary_from_zip() {
  local url="\$1" dest="\$2" name="\$3"
  local ztmp="/tmp/\${name}-zip.$$" dtmp="/tmp/\${name}-dir.$$"
  mkdir -p "\$dtmp"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$ztmp' '\$url'" 5
  unzip -o -q "\$ztmp" -d "\$dtmp"; rm -f "\$ztmp"
  local bin
  bin=\$(find "\$dtmp" -type f -executable 2>/dev/null | head -1)
  [ -z "\$bin" ] && bin=\$(find "\$dtmp" -type f 2>/dev/null | head -1)
  [ -z "\$bin" ] && { echo "ERROR: no binary in \$url"; exit 1; }
  cp -f "\$bin" "\$dest"; chmod +x "\$dest"; rm -rf "\$dtmp"
}

# ── stage: OTA metadata ─────────────────────────────────────────────────────
echo "[overlay] Fetch OTA metadata"
META="\$(mktemp)"
retry "curl -fsSL -H 'Cache-Control: no-cache' -o '\$META' '\$OTA_METADATA_URL'" 5
WEB_URL=\$(jq -r '.web.url // empty'         "\$META")
OS_SERVER_URL=\$(jq -r '."os-server".url // empty'       "\$META")
BOOTSTRAP_URL=\$(jq -r '.bootstrap.url // empty' "\$META")
HAL_URL=\$(jq -r '.hal.url // empty'   "\$META")
DEVICES_URL=\$(jq -r --arg t "\$DEVICE_TYPE" '.devices[\$t].url // empty' "\$META")
BUDDY_URL=\$(jq -r '."claude-desktop-buddy".url // empty' "\$META")
WEB_VER=\$(jq -r '.web.version // empty'     "\$META")
OS_SERVER_VER=\$(jq -r '."os-server".version // empty'   "\$META")
BOOTSTRAP_VER=\$(jq -r '.bootstrap.version // empty' "\$META")
HAL_VER=\$(jq -r '.hal.version // empty' "\$META")
BUDDY_VER=\$(jq -r '."claude-desktop-buddy".version // empty' "\$META")
rm -f "\$META"
[ -z "\$WEB_URL" ] || [ -z "\$OS_SERVER_URL" ] || [ -z "\$BOOTSTRAP_URL" ] && {
  echo "ERROR: OTA metadata missing web.url, os-server.url or bootstrap.url"; exit 1
}
echo "[overlay] web=\$WEB_VER os-server=\$OS_SERVER_VER bootstrap=\$BOOTSTRAP_VER hal=\$HAL_VER buddy=\$BUDDY_VER"

# ── stage: backend binaries ──────────────────────────────────────────────────
echo "[overlay] Install backend binaries"
install_binary_from_zip "\$BOOTSTRAP_URL" /usr/local/bin/bootstrap-server "bootstrap"
install_binary_from_zip "\$OS_SERVER_URL"      /usr/local/bin/os-server      "os-server"

# ── stage: HAL (Python hardware runtime) ──────────────────────────────────
echo "[overlay] Install HAL"
HAL_DIR="/opt/hal"
mkdir -p "\$HAL_DIR"
if [ -n "\$HAL_URL" ]; then
  echo "[overlay] HAL: downloading from \$HAL_URL"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/hal.zip '\$HAL_URL'" 5
  echo "[overlay] HAL: extracting zip to \$HAL_DIR"
  unzip -o -q /tmp/hal.zip -d "\$HAL_DIR"
  rm -f /tmp/hal.zip
  echo "[overlay] HAL: zip contents:"
  find "\$HAL_DIR" -maxdepth 2 -type f | head -30

  # Ensure uv is available (may be missing if base image was cached before uv stage)
  export PATH="/root/.local/bin:\$PATH"
  if ! command -v uv &>/dev/null; then
    echo "[overlay] HAL: uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "[overlay] HAL: uv installed at \$(command -v uv || echo /root/.local/bin/uv)"
  else
    echo "[overlay] HAL: uv found at \$(command -v uv)"
  fi

  # If zip extracted into a subdirectory, move contents up to HAL_DIR
  if [ ! -f "\$HAL_DIR/pyproject.toml" ]; then
    echo "[overlay] HAL: pyproject.toml not at root, searching subdirectories..."
    SUBDIR=\$(find "\$HAL_DIR" -maxdepth 2 -name pyproject.toml 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    if [ -n "\$SUBDIR" ] && [ "\$SUBDIR" != "\$HAL_DIR" ]; then
      echo "[overlay] HAL: moving from \$SUBDIR to \$HAL_DIR"
      shopt -s dotglob 2>/dev/null || true
      mv "\$SUBDIR"/* "\$HAL_DIR"/ 2>/dev/null || cp -a "\$SUBDIR"/. "\$HAL_DIR"/
      shopt -u dotglob 2>/dev/null || true
    else
      echo "[overlay] HAL: no pyproject.toml found anywhere under \$HAL_DIR"
    fi
  fi

  echo "[overlay] HAL: checking pyproject.toml..."
  if [ ! -f "\$HAL_DIR/pyproject.toml" ]; then
    echo "ERROR: pyproject.toml not found in \$HAL_DIR after extraction"
    echo "Directory listing:"
    ls -laR "\$HAL_DIR"/ | head -50
    exit 1
  fi
  echo "[overlay] HAL: pyproject.toml found OK"

  # Clean stale lerobot distutils egg-info that blocks uv uninstall
  find /root/.cache/uv -name 'lerobot.egg-info' -type d 2>/dev/null | xargs -r rm -rf || true
  rm -rf "\$HAL_DIR/.venv"
  cd "\$HAL_DIR"
  # Use the release lock when available; keep legacy archives installable.
  HAL_LOCK_ARG=""
  [ ! -f uv.lock ] || HAL_LOCK_ARG="--locked"
  echo "[overlay] HAL: running uv sync --python 3.12 --extra hardware --extra aec --extra pipecat"
  uv sync --python 3.12 --extra hardware --extra aec --extra pipecat \$HAL_LOCK_ARG 2>&1 || {
    echo "ERROR: uv sync failed (exit code \$?)"
    echo "[overlay] HAL: uv version: \$(uv --version 2>&1 || echo unknown)"
    echo "[overlay] HAL: python check: \$(python3 --version 2>&1 || echo not found)"
    echo "[overlay] HAL: pyproject.toml head:"
    head -30 "\$HAL_DIR/pyproject.toml" 2>/dev/null || true
    exit 1
  }
  echo "[overlay] HAL: uv sync complete"

  # Patch webrtcvad: replace pkg_resources import (removed in Python 3.12+).
  # Without this hal crashes on first import of webrtcvad on a fresh Py3.12 venv.
  WEBRTCVAD_PY=\$(find "\$HAL_DIR/.venv" -name "webrtcvad.py" -path "*/site-packages/*" 2>/dev/null | head -1)
  if [ -n "\$WEBRTCVAD_PY" ] && grep -q "import pkg_resources" "\$WEBRTCVAD_PY" 2>/dev/null; then
    echo "[overlay] HAL: patching webrtcvad for Python 3.12+ (pkg_resources removal)"
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
  echo "[overlay] WARN: No hal URL in OTA metadata, skipping HAL download"
fi

# Seed /opt/hal/.env with the baked device profile (idempotent grep-append).
# Read by HAL for capability mounting; mirrors os-server.service Environment=.
mkdir -p "\$HAL_DIR"
touch "\$HAL_DIR/.env"
grep -q "^DEVICE_TYPE=" "\$HAL_DIR/.env" \
  || echo "DEVICE_TYPE=\$DEVICE_TYPE" >> "\$HAL_DIR/.env"
grep -q "^DEVICES_DIR=" "\$HAL_DIR/.env" \
  || echo "DEVICES_DIR=\$DEVICES_DIR" >> "\$HAL_DIR/.env"

# ── stage: device profile ────────────────────────────────────────────────────
# Per-device: install ONLY this device_type's artifact (devices.<type> in OTA
# metadata) into DEVICES_DIR/<type>. Absent → skip (agent keeps the gateway's
# default soul; HAL mounts all routes). Read by os-server (soul) and HAL.
echo "[overlay] Install device profile (\$DEVICE_TYPE)"
DEVICE_DEST="\$DEVICES_DIR/\$DEVICE_TYPE"
mkdir -p "\$DEVICE_DEST"
if [ -n "\${DEVICES_URL:-}" ]; then
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/device.zip '\$DEVICES_URL'" 5
  unzip -o -q /tmp/device.zip -d "\$DEVICE_DEST"
  rm -f /tmp/device.zip
  echo "[overlay] Device profile '\$DEVICE_TYPE' installed at \$DEVICE_DEST"
  # Pi 5 V1 hardware (case đầu) không có loa/mic — audio capability declared
  # trong ROBOT.md/DEVICE.md sẽ khiến healthwatch chửi audio=false → yellow
  # status LED + HAL cố init PortAudio → crash log. Strip audio key khi build
  # với PI5_NO_AUDIO=1 để device chạy silent, LED clean. Artifact zip từ CDN
  # ship file dưới tên DEVICE.md (name it shipped under), repo dùng ROBOT.md
  # (canonical) — code readFrontMatter() thử ROBOT.md rồi fallback DEVICE.md,
  # nên strip cả 2 file nếu tồn tại để bulletproof.
  if [ "\${PI5_NO_AUDIO:-0}" = "1" ]; then
    for MDFILE in "\$DEVICE_DEST/ROBOT.md" "\$DEVICE_DEST/DEVICE.md"; do
      if [ -f "\$MDFILE" ]; then
        sed -i '/^[[:space:]]*audio:/d' "\$MDFILE"
        echo "[overlay] PI5_NO_AUDIO=1 — stripped audio capability from \$MDFILE"
      fi
    done
  fi
  # Bake the selection in the overlay, never in the reusable base image.
  if [ -n "\$VARIANT" ] && [ "\$VARIANT" != standard ]; then
    mkdir -p /etc/autonomous
    printf '%s\n' "\$VARIANT" > /etc/autonomous/hardware-profile
    [ -f "\$DEVICE_DEST/apply-overrides.py" ] \
      || { echo "[overlay] ERROR: Hardware overrides require an override-capable device package" >&2; exit 1; }
    python3 "\$DEVICE_DEST/apply-overrides.py" --profile "\$DEVICE_DEST" --root / \
      || { echo "[overlay] ERROR: Hardware override configuration failed" >&2; exit 1; }
  else
    rm -f /etc/autonomous/hardware-profile
  fi
  # Device rootfs overlay: robots/<type>/rootfs/ mirrors the target filesystem,
  # so copying it onto / lands each file at its real path. This is where the
  # device's HAL tuning lives (/opt/hal/.env — ALSA device names, VAD/camera
  # thresholds, realtime flags); without this copy HAL boots with only the two
  # keys seeded above and every tuned value falls back to its code default.
  # Runs AFTER the seed so the profile's .env wins, then re-asserts the two
  # keys idempotently for profiles that ship a rootfs/ without them.
  if [ -d "\$DEVICE_DEST/rootfs" ]; then
    cp -a "\$DEVICE_DEST/rootfs/." /
    echo "[overlay] Device rootfs overlay applied from \$DEVICE_DEST/rootfs"
    touch "\$HAL_DIR/.env"
    grep -q "^DEVICE_TYPE=" "\$HAL_DIR/.env" \
      || echo "DEVICE_TYPE=\$DEVICE_TYPE" >> "\$HAL_DIR/.env"
    grep -q "^DEVICES_DIR=" "\$HAL_DIR/.env" \
      || echo "DEVICES_DIR=\$DEVICES_DIR" >> "\$HAL_DIR/.env"
  else
    echo "[overlay] WARN: device profile has no rootfs/ overlay"
  fi
else
  echo "[overlay] ERROR: no devices.\$DEVICE_TYPE url in OTA metadata — device profile is required (one image = one device type). Run 'make upload-device \$DEVICE_TYPE' before building." >&2
  exit 1
fi

# ── stage: default agent runtime + SSH policy ────────────────────────────────
# Baked in the OVERLAY phase, not the base: base.img is cached and reused across
# rebuilds, so baking a DEFAULT_AGENT-dependent value into Phase 1 would silently
# keep whatever the previous build used. The overlay always runs, so this always
# matches the DEFAULT_AGENT this build was invoked with.
if [ -n "\${DEFAULT_AGENT:-}" ]; then
  mkdir -p /root/config
  echo "\${DEFAULT_AGENT}" > /root/config/f_r_default_agent
  echo "[overlay] f_r_default_agent baked: \${DEFAULT_AGENT}"
else
  echo "[overlay] DEFAULT_AGENT unset — no f_r_default_agent baked (runtime seeding falls through to ROBOT.md gateway.default)"
fi

# SSH: gated by DEFAULT_AGENT, but ONLY for intern-v2 — every other DEVICE_TYPE
# (lamp included) keeps today's behavior (SSH always enabled), regardless of
# DEFAULT_AGENT. claudecode (black case, Developer Edition) ships SSH open;
# every other default agent (hermes, openclaw, codex, picoclaw, opencode) ships
# it closed. Unset DEFAULT_AGENT → unchanged: SSH enabled. Mirrors the same gate
# in build-orangepi.sh, but must also clear /boot/firmware/ssh: Phase 1 enables
# SSH two ways (RPi OS boot flag file + systemd wants symlink) and leaving the
# flag file behind would let RPi OS re-enable sshd on first boot despite the
# masked unit.
if [ "\$DEVICE_TYPE" = "intern-v2" ] && [ -n "\${DEFAULT_AGENT:-}" ] && [ "\${DEFAULT_AGENT}" != "claudecode" ]; then
  echo "[overlay] DEFAULT_AGENT=\${DEFAULT_AGENT} (intern-v2 consumer edition) — SSH stays closed"
  rm -f /boot/firmware/ssh
  rm -f /etc/systemd/system/multi-user.target.wants/ssh.service
  systemctl disable ssh 2>/dev/null || true
  systemctl mask ssh 2>/dev/null || true
else
  echo "[overlay] SSH enabled (DEVICE_TYPE=\$DEVICE_TYPE DEFAULT_AGENT=\${DEFAULT_AGENT:-<unset>})"
  # unmask before enable: a stale base.img may carry the masked state from a
  # previous consumer-edition build of the same base.
  systemctl unmask ssh 2>/dev/null || true
  touch /boot/firmware/ssh
  ln -sf /lib/systemd/system/ssh.service \\
         /etc/systemd/system/multi-user.target.wants/ssh.service 2>/dev/null || true
fi

# ── stage: web UI ────────────────────────────────────────────────────────────
echo "[overlay] Download web UI"
retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/web.zip '\$WEB_URL'" 5
unzip -o -q /tmp/web.zip -d /usr/share/nginx/html/setup
rm -f /tmp/web.zip

# ── stage: Claude Desktop Buddy (BLE plugin, optional) ───────────────────────
# Optional BLE bridge that pairs the lamp with Claude Desktop. The Mac-side
# "Autonomous Buddy" Swift app is a separate component and is NOT installed here.
# Service name is claude-desktop-buddy.service for legacy parity with setup.sh.
if [ -n "\$BUDDY_URL" ]; then
  echo "[overlay] Install Claude Desktop Buddy"
  BUDDY_DIR="/opt/claude-desktop-buddy"
  mkdir -p "\$BUDDY_DIR" /root/config
  retry "curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/buddy.zip '\$BUDDY_URL'" 5
  unzip -o -q /tmp/buddy.zip -d /tmp/buddy-extract
  rm -f /tmp/buddy.zip
  if [ -f /tmp/buddy-extract/buddy-plugin ]; then
    cp -f /tmp/buddy-extract/buddy-plugin "\$BUDDY_DIR/buddy-plugin"
    chmod +x "\$BUDDY_DIR/buddy-plugin"
  fi
  if [ ! -f /root/config/buddy.json ] && [ -f /tmp/buddy-extract/config/buddy.json ]; then
    cp -f /tmp/buddy-extract/config/buddy.json /root/config/buddy.json
  fi
  echo "\$BUDDY_VER" > "\$BUDDY_DIR/VERSION_BUDDY"
  rm -rf /tmp/buddy-extract
  cat > /etc/systemd/system/claude-desktop-buddy.service <<UNIT
[Unit]
Description=Claude Desktop Buddy (BLE)
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=\$BUDDY_DIR
ExecStart=\$BUDDY_DIR/buddy-plugin -config /root/config/buddy.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-desktop-buddy

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable claude-desktop-buddy
else
  echo "[overlay] WARN: No claude-desktop-buddy URL in OTA metadata, skipping buddy install"
fi

echo "[overlay] All overlay stages complete"
OVERLAY_STAGES

# Clean up overlay chroot
mv ${MNT}/etc/resolv.conf.bak ${MNT}/etc/resolv.conf 2>/dev/null || true
umount ${MNT}/dev
umount ${MNT}/sys
umount ${MNT}/proc
rm -f ${MNT}/usr/bin/qemu-aarch64-static

# ── 22. take initial @factory snapshot ───────────────────────────────────────
# Take the factory snapshot at build time (not on first boot).
# This ensures a known-good factory state is always present on the image,
# even before fr-snapshot has ever been run on the Pi.
# The snapshot is read-only (-r) so it cannot be accidentally modified.
#
# Mount top-level (subvolid=5) WHILE subvol=@ is still mounted. Btrfs
# allows simultaneous mounts of different subvolumes. This avoids the
# unmount-remount cycle that fails in Docker --privileged (the kernel can
# auto-detach loop devices once the last filesystem is unmounted).
echo "==> Taking initial @factory snapshot..."
# Flush Btrfs transactions before snapshot to ensure all data is committed
btrfs filesystem sync ${MNT}
sync
mkdir -p /mnt/btrfs-top
mount -t btrfs -o subvolid=5 ${OUT_LOOP_ROOT} /mnt/btrfs-top
btrfs subvolume snapshot -r /mnt/btrfs-top/@ /mnt/btrfs-top/@factory

# ── 23. unmount ───────────────────────────────────────────────────────────────
echo "==> Flushing Btrfs and unmounting..."
# Flush Btrfs transaction log before unmount to ensure all metadata is on disk
btrfs filesystem sync /mnt/btrfs-top 2>/dev/null || true
sync
umount ${MNT}/boot/firmware
umount ${MNT}
umount /mnt/btrfs-top

# ── 24. QC checks ────────────────────────────────────────────────────────────
# Verify critical files and subvolumes exist in the image before declaring success.
echo "==> Running QC checks..."
QC_FAIL=0
mkdir -p /mnt/btrfs-top
mount -o subvolid=5 ${OUT_LOOP_ROOT} /mnt/btrfs-top

# Check Btrfs subvolumes
for SUB in @ @factory; do
  if btrfs subvolume show "/mnt/btrfs-top/$SUB" > /dev/null 2>&1; then
    echo "  [OK] subvolume $SUB"
  else
    echo "  [FAIL] subvolume $SUB missing"; QC_FAIL=1
  fi
done

# Mount @ for file checks
mount -o ${BTRFS_BUILD_OPTS},subvol=@ ${OUT_LOOP_ROOT} ${MNT}
mount ${OUT_LOOP_BOOT} ${MNT}/boot/firmware

# Check critical binaries
for BIN in /sbin/init \
           /usr/local/bin/os-server /usr/local/bin/bootstrap-server \
           /usr/local/bin/fr-snapshot /usr/local/bin/fr-rollback \
           /usr/local/bin/device-ap-mode /usr/local/bin/device-sta-mode \
           /usr/local/bin/connect-wifi /usr/local/bin/software-update \
           /usr/local/bin/btrfs-resize-once /usr/bin/btrfs; do
  if [ -f "${MNT}${BIN}" ]; then
    echo "  [OK] $BIN"
  else
    echo "  [FAIL] $BIN missing"; QC_FAIL=1
  fi
done

# Check critical config files
for CFG in /etc/fstab /etc/hostapd/hostapd.conf /etc/nginx/conf.d/${DEVICE_TYPE}.conf \
           /boot/firmware/cmdline.txt; do
  if [ -f "${MNT}${CFG}" ]; then
    echo "  [OK] $CFG"
  else
    echo "  [FAIL] $CFG missing"; QC_FAIL=1
  fi
done

# Check systemd services are enabled
for SVC in bootstrap os-server hal nginx openclaw btrfs-resize-once firstrun-wifi; do
  if [ -L "${MNT}/etc/systemd/system/multi-user.target.wants/${SVC}.service" ] || \
     [ -L "${MNT}/etc/systemd/system/sysinit.target.wants/${SVC}.service" ]; then
    echo "  [OK] ${SVC}.service enabled"
  else
    echo "  [FAIL] ${SVC}.service not enabled"; QC_FAIL=1
  fi
done

# Verify cmdline.txt has Btrfs params
if grep -q "rootfstype=btrfs" "${MNT}/boot/firmware/cmdline.txt" && \
   grep -q "rootflags=subvol=@" "${MNT}/boot/firmware/cmdline.txt"; then
  echo "  [OK] cmdline.txt Btrfs params"
else
  echo "  [FAIL] cmdline.txt missing Btrfs params"; QC_FAIL=1
fi

# Verify web UI was installed
if [ -f "${MNT}/usr/share/nginx/html/setup/index.html" ]; then
  echo "  [OK] web UI installed"
else
  echo "  [FAIL] web UI missing"; QC_FAIL=1
fi

# Verify the device rootfs overlay actually landed on / — this is where the
# device's /opt/hal/.env tuning lives (ALSA names, VAD/camera thresholds), and a
# silent miss ships an image whose HAL runs on code defaults.
DEV_ROOTFS="${MNT}${DEVICES_DIR}/${DEVICE_TYPE}/rootfs"
if [ -d "${DEV_ROOTFS}" ]; then
  OVERLAY_MISS=0
  while IFS= read -r f; do
    REL="${f#${DEV_ROOTFS}}"
    [ -e "${MNT}${REL}" ] || { echo "  [FAIL] rootfs overlay not applied: ${REL}"; OVERLAY_MISS=1; }
  done < <(find "${DEV_ROOTFS}" -type f)
  if [ ${OVERLAY_MISS} -eq 0 ]; then
    echo "  [OK] device rootfs overlay applied"
  else
    QC_FAIL=1
  fi
else
  echo "  [WARN] device profile '${DEVICE_TYPE}' ships no rootfs/ overlay"
fi

# Verify the baked default agent runtime matches what this build was invoked with
if [ -n "${DEFAULT_AGENT}" ]; then
  if [ "$(cat "${MNT}/root/config/f_r_default_agent" 2>/dev/null)" = "${DEFAULT_AGENT}" ]; then
    echo "  [OK] f_r_default_agent=${DEFAULT_AGENT}"
  else
    echo "  [FAIL] f_r_default_agent missing or != ${DEFAULT_AGENT}"; QC_FAIL=1
  fi
fi

# Verify pre-baked agent runtime CLIs. Hermes is baked for every device type;
# the rest only for the ones whose web UI exposes the runtime picker.
if [ -d "${MNT}/usr/local/lib/hermes-agent" ]; then
  echo "  [OK] hermes pre-baked"
else
  echo "  [FAIL] hermes not pre-baked"; QC_FAIL=1
fi
case "${DEVICE_TYPE}" in
  lamp|intern-v2)
    for RTBIN in codex claude picoclaw opencode; do
      if [ -e "${MNT}/usr/local/bin/${RTBIN}" ]; then
        echo "  [OK] ${RTBIN} pre-baked"
      else
        echo "  [FAIL] ${RTBIN} not pre-baked"; QC_FAIL=1
      fi
    done
    ;;
esac

btrfs filesystem sync /mnt/btrfs-top 2>/dev/null || true
sync
umount ${MNT}/boot/firmware
umount ${MNT}
umount /mnt/btrfs-top
sync
losetup -d ${OUT_LOOP_ROOT} 2>/dev/null || true; OUT_LOOP_ROOT=""
losetup -d ${OUT_LOOP_BOOT} 2>/dev/null || true; OUT_LOOP_BOOT=""
losetup -d ${OUT_LOOP_DEV}  2>/dev/null || true; OUT_LOOP_DEV=""
sync

if [ $QC_FAIL -ne 0 ]; then
  echo ""
  echo "❌  QC FAILED — image may not boot correctly"
  exit 1
fi

echo ""
echo "✅  ${OUT_IMG} ready (all QC checks passed)"
echo "    Size:      ${OUT_IMG_SIZE} (expands to fill SD on first boot)"
echo "    User:      ${USERNAME} / ${PASSWORD}"
echo "    Hostname:  ${PI_HOSTNAME}"
echo "    Timezone:  ${PI_TIMEZONE}"
echo "    AP:        ${DEVICE_TYPE}-xxxx (serial-based SSID) @ 192.168.100.1"
echo ""
echo "    Flash:"
echo "      diskutil unmountDisk /dev/diskN"
echo "      sudo dd if=output/golden.img of=/dev/rdiskN bs=8m status=progress"
echo "      sync && diskutil eject /dev/diskN"
echo ""
echo "    On Pi:"
echo "      sudo fr-snapshot              — save current state as @factory"
echo "      sudo fr-rollback              — restore @factory and reboot"
echo "      sudo device-ap-mode           — switch to hotspot"
echo "      sudo device-sta-mode          — switch to WiFi client"
echo "      sudo connect-wifi SSID PASS   — connect to WiFi"
echo "      sudo software-update <bootstrap|os-server|hal|openclaw|web>  — OTA update"
