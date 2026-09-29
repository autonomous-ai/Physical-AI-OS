# imager/lib — image helpers

`defer_orangepi_update_count.py` postpones only the vendor boot-time MOTD
package-count scan by 120 seconds. It accepts `--root` for mounted images and
can also run on an existing device; daily/package update schedules are unchanged.

The shared bash libraries below are planned for a refactor that extracts the chroot
stages out of the inline heredocs in `build.sh` (Pi 5) and `build-orangepi.sh`
(OPi 4 Pro) into a sourceable bash file so the two builders stop drifting.

## Why not yet

Doing the extraction cleanly requires a working baseline on **both** boards
(so we can verify the refactor doesn't regress either). OPi golden image is
currently unverified on real hardware — it needs at least one successful flash
+ boot + setup-wizard run before we lock in the shared interface.

When that's done, candidate files:

| File | Purpose |
|------|---------|
| `chroot-phase1.sh` | Board-agnostic Phase 1 chroot block — apt install, AP setup (`device-ap-mode` etc.), PulseAudio AEC + udev, lelamp uv prep, Node.js + OpenClaw npm global, systemd unit writes. |
| `chroot-overlay.sh` | Board-agnostic Phase 2 (overlay) chroot block — OTA metadata fetch, backend/lelamp/web/buddy install, webrtcvad pkg_resources patch. |
| `device-ap-mode.sh` / `device-sta-mode.sh` / `connect-wifi.sh` / `software-update.sh` | The four runtime helper scripts currently embedded as heredocs in both builders. Once extracted they can be reused verbatim (already byte-identical between the two). |

## Caller protocol

Both `build.sh` and `build-orangepi.sh` would:

1. `cp imager/lib/chroot-phase1.sh ${MNT}/tmp/chroot-phase1.sh`
2. `chroot ${MNT} env OTA_METADATA_URL=... AP_BAND=... OPENCLAW_VERSION=... /bin/bash /tmp/chroot-phase1.sh`
3. Same pattern for `chroot-overlay.sh` after the base apt install layer.

This way the heredoc-with-`\$`-escaping mess inside the current builders goes
away — the extracted scripts are normal bash files that read env, no nested
heredoc semantics to track.

## Maintenance rule until the refactor lands

When you change a chroot stage in **either** builder script, mirror the change
in the **other** if the affected lines are board-agnostic (apt list, AP
helper scripts, PulseAudio, lelamp uv flow, OpenClaw env block, OTA bake).
The headers of both scripts list the duplicated regions; search for
`heredoc` to find them.

Board-specific lines that should NOT be mirrored:

- `build.sh`: anything touching `/boot/firmware/config.txt`, `cmdline.txt`,
  Btrfs `@`/`@factory` subvolumes, `auto_initramfs`, `dtparam=spi=on` + `dtparam=i2c_arm=on`.
- `build-orangepi.sh`: anything touching `/boot/orangepiEnv.txt`, ES8389
  ALSA aliases (sndi2s4-specific), `orangepi-firstrun-config.service` mask,
  ext4-single-partition assumptions.
