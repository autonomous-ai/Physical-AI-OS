"""Exercise device override install hooks with isolated files and mocked system operations."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


PROVISION = Path(__file__).resolve().parents[1]


class DeviceOverridesInstallTests(unittest.TestCase):
    def run_install(self, *, setup=False, selection="pro", helper=True, failure="",
                    active=True, enabled=True, device_type="example"):
        if setup:
            source = (PROVISION / "setup.sh").read_text()
            branch = re.search(r"^stage_devices\(\) \{\n.*?^\}$", source,
                               re.MULTILINE | re.DOTALL).group(0)
            # Match run_stage: explicit failure guards must work with set -e suspended.
            branch += "\nif stage_devices; then exit 0; else exit 1; fi"
        else:
            source = (PROVISION / "software-update").read_text()
            branch = source.split('elif [ "$APP" = "device" ]; then\n', 1)[1]
            branch = branch.rsplit("\nfi", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            devices = root / "devices"
            installed = devices / device_type
            (installed / "rootfs").mkdir(parents=True)
            (installed / "ROBOT.md").write_text("old profile")
            package = root / "package"
            (package / "rootfs").mkdir(parents=True)
            (package / "ROBOT.md").write_text("new legacy profile")
            (package / "rootfs/env").write_bytes(b"legacy env\n")
            if helper:
                (package / "apply-overrides.py").touch()
            rollback = root / "rollback"
            rollback.mkdir()
            marker_file = root / "hardware-profile"
            if selection is not None:
                marker_file.write_text(selection)
            events = root / "events"
            # Replace only the fixed production marker path; never touch host /etc.
            branch = branch.replace("/etc/autonomous/hardware-profile", str(marker_file))
            mocks = r'''
set -e
event() { printf '%s\n' "$*" >> "$EVENTS"; }
resolve_device_profile() { DEVICE_DEST="$DEVICES_DIR/$DEVICE_TYPE"; }
download_verified() { :; }
retry() { :; }
unzip() {
  local destination="$DIR_TMP"
  [ "$SETUP" = 0 ] || destination="$DEVICES_DIR/$DEVICE_TYPE"
  /bin/cp -R "$PACKAGE/." "$destination"
}
rm() {
  # setup uses a fixed download path; leave the host file alone.
  [ "${2:-}" != /tmp/device.zip ] || return 0
  command rm "$@"
}
python3() {
  event render
  [ "$FAIL" != render ] || return 1
  local profile="${1%/apply-overrides.py}"
  [ "$2" = --profile ] && [ "$3" = "$profile" ] && [ "$4" = --root ] && [ "$5" = / ] || return 1
  printf 'new pro profile' > "$profile/ROBOT.md"
  printf 'pro env\n' > "$profile/rootfs/env"
}
save_device_service_state() { event save-services; }
arm_service_restore() { :; }
systemctl() {
  event "systemctl $*"
  case "$1" in
    is-active) [ "$ACTIVE" = 1 ]; return $? ;;
    is-enabled) [ "$ENABLED" = 1 ]; return $? ;;
    restart) [ "$FAIL" != restart ]; return $? ;;
  esac
  return 0
}
snapshot_device_rootfs() {
  event "snapshot $(cat "$2/env")"
}
mark_publish_pending() { pending=1; }
publish_mode() { :; }
clear_publish_pending() { pending=0; }
cp() {
  if [ "$3" = / ]; then event apply-rootfs; [ "$FAIL" != copy ];
  else command cp "$@"; fi
}
restore_device_service_state() { event restore-services; }
check_device_profile() { event check-health; [ "$FAIL" != health ]; }
restore_device_backup() {
  event rollback
  command rm -rf "$DEVICE_DEST"
  mv "$ROLLBACK_DIR/device.previous" "$DEVICE_DEST"
}
disarm_service_restore() { :; }
cleanup() {
  local code=$?
  if [ "${pending:-0}" = 1 ] && [ "$code" -ne 0 ]; then
    restore_device_backup
    restore_device_service_state
  fi
  [ -z "${DIR_TMP:-}" ] || command rm -rf "$DIR_TMP"
  [ -z "${ZIP_TMP:-}" ] || command rm -f "$ZIP_TMP"
}
trap cleanup EXIT
'''
            result = subprocess.run(
                ["bash", "-c", mocks + branch],
                env={**os.environ, "DEVICES_DIR": str(devices),
                     "ROLLBACK_DIR": str(rollback), "DEVICE_TYPE": device_type,
                     "URL": "unused", "SHA256": "unused", "VERSION": "test",
                     "DEVICES_URL": "unused", "DEVICES_SHA256": "",
                     "PACKAGE": str(package), "EVENTS": str(events), "FAIL": failure,
                     "SETUP": str(int(setup)), "ACTIVE": str(int(active)),
                     "ENABLED": str(int(enabled))},
                capture_output=True, text=True,
            )
            return (result, events.read_text().splitlines() if events.exists() else [],
                    (installed / "ROBOT.md").read_text(),
                    (installed / "rootfs/env").read_bytes() if (installed / "rootfs/env").exists() else None,
                    list(devices.glob(".*.new.*")))

    def test_legacy_ota_does_not_render_or_change_package_bytes(self):
        for selection in (None, "", "  \n", "standard", " standard\n"):
            for helper in (False, True):
                with self.subTest(selection=selection, helper=helper):
                    result, events, robot, env, staging = self.run_install(selection=selection, helper=helper)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("render", events)
                    self.assertEqual(robot, "new legacy profile")
                    self.assertEqual(env, b"legacy env\n")
                    self.assertEqual(staging, [])

    def test_override_render_precedes_snapshot_and_service_stop(self):
        result, events, robot, env, staging = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(events.index("render"), events.index("systemctl stop hal"))
        self.assertLess(events.index("render"), events.index("snapshot pro env"))
        self.assertLess(events.index("snapshot pro env"), events.index("apply-rootfs"))
        self.assertEqual(robot, "new pro profile")
        self.assertEqual(env, b"pro env\n")
        self.assertEqual(staging, [])

    def test_override_missing_helper_or_failed_render_does_not_stop_services(self):
        for helper, failure in ((False, ""), (True, "render")):
            with self.subTest(helper=helper, failure=failure):
                result, events, robot, _, staging = self.run_install(helper=helper, failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(event.startswith("systemctl") for event in events))
                self.assertEqual(robot, "old profile")
                self.assertEqual(staging, [])

    def test_copy_or_health_failure_rolls_back(self):
        for failure in ("copy", "health"):
            with self.subTest(failure=failure):
                result, events, robot, _, staging = self.run_install(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rollback", events)
                self.assertEqual(robot, "old profile")
                self.assertEqual(staging, [])
                if failure == "copy":
                    self.assertLess(events.index("rollback"), events.index("restore-services"))

    def test_named_override_applies_for_any_device_type(self):
        result, events, robot, env, _ = self.run_install(device_type="other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("render", events)
        self.assertEqual(robot, "new pro profile")
        self.assertEqual(env, b"pro env\n")

    def test_legacy_setup_keeps_existing_flow(self):
        for selection in (None, "", " standard\n"):
            with self.subTest(selection=selection):
                result, events, _, env, _ = self.run_install(setup=True, selection=selection)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(events, [])
                self.assertEqual(env, b"legacy env\n")

    def test_override_setup_restarts_after_overlay_and_preserves_disabled_state(self):
        for active, enabled in ((True, True), (False, True), (False, False)):
            with self.subTest(active=active, enabled=enabled):
                result, events, _, _, _ = self.run_install(setup=True, active=active, enabled=enabled)
                self.assertEqual(result.returncode, 0, result.stderr)
                if active or enabled:
                    self.assertLess(events.index("apply-rootfs"), events.index("systemctl restart hal"))
                else:
                    self.assertNotIn("systemctl restart hal", events)

    def test_override_setup_failures_never_report_success(self):
        for helper, failure in ((False, ""), (True, "render"), (True, "copy"), (True, "restart")):
            with self.subTest(helper=helper, failure=failure):
                result, events, _, _, _ = self.run_install(setup=True, helper=helper, failure=failure)
                self.assertNotEqual(result.returncode, 0)
                if failure != "restart":
                    self.assertNotIn("systemctl restart hal", events)


class DeviceOverridesImageTests(unittest.TestCase):
    def test_image_selection_requires_helper_and_legacy_runs_no_helper(self):
        for name in ("build-orangepi.sh", "build.sh", "build-pi5.sh"):
            source = (PROVISION.parent / "imager" / name).read_text()
            block = source.split("  # Bake the selection in the overlay", 1)[1]
            block = block[block.index("\n") + 1:].split("  # Device rootfs overlay:", 1)[0]
            # These blocks are inside a chroot heredoc; expand the escaped variables.
            block = block.replace("\\$", "$")
            for selection, helper, fail in ((None, False, False), (None, True, False),
                                           ("", False, False), ("standard", False, False),
                                           ("pro", False, False), ("pro", True, False),
                                           ("pro", True, True)):
                with self.subTest(script=name, selection=selection, helper=helper, fail=fail):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        selected = selection is not None and selection.strip() not in ("", "standard")
                        # A reused base must not leak a previous assembly selection.
                        (root / "hardware-profile").write_text("stale-profile\n")
                        if helper:
                            (root / "apply-overrides.py").touch()
                        events = root / "events"
                        result = subprocess.run(
                            ["bash", "-c", 'python3() { echo render >> "$EVENTS"; '
                             'test "$(cat "$MARKER")" = "$VARIANT" || exit 2; [ "$FAIL" = 0 ]; }\n'
                             + block.replace("/etc/autonomous", str(root))],
                            env={**os.environ, "DEVICE_TYPE": "example",
                                 "DEVICE_PROFILE_DIR": temporary, "DEVICE_DEST": temporary,
                                 "EVENTS": str(events), "FAIL": str(int(fail)),
                                 "VARIANT": selection or "", "MARKER": str(root / "hardware-profile")},
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode == 0,
                                         not selected or (helper and not fail), result.stderr)
                        self.assertEqual(events.exists(), selected and helper)
                        self.assertEqual((root / "hardware-profile").exists(), selected)

    def test_variant_validation_before_build(self):
        for name in ("build-orangepi.sh", "build.sh", "build-pi5.sh"):
            source = (PROVISION.parent / "imager" / name).read_text()
            block = 'VARIANT="${VARIANT:-}"' + source.split('VARIANT="${VARIANT:-}"', 1)[1].split("\nfi", 1)[0] + "\nfi\n"
            self.assertNotIn("HARDWARE_PROFILE", source)
            for variant in ("", "standard", "pro", "usb-v2", "../pro", "Pro", "a" * 65):
                with self.subTest(script=name, variant=variant):
                    result = subprocess.run(["bash", "-c", block], env={**os.environ, "VARIANT": variant},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, variant in ("", "standard", "pro", "usb-v2"))

    def test_make_forwards_variant_and_keeps_default_image_names(self):
        for variant in ("", "standard", "pro"):
            with self.subTest(variant=variant):
                result = subprocess.run(
                    ["make", "-n", "build", "TARGET=opi", "DEVICE_TYPE=lamp", "OTA_METADATA_URL=https://example.test/meta.json",
                     "VARIANT=" + variant], cwd=PROVISION.parent / "imager", capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("-e VARIANT=" + variant + " ", result.stdout)
                suffix = "-pro" if variant == "pro" else ""
                self.assertIn("golden-opi-lamp" + suffix + ".img.xz", result.stdout)


if __name__ == "__main__":
    unittest.main()
