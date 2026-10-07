"""Fault injection for the real updater helpers, isolated from host services/files."""
import os
import json
import zipfile
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hal = self.root / "hal"
        self.web = self.root / "web"
        self.backup = self.root / "rollback"
        for path in (self.hal, self.web, self.backup, self.root / "bin"):
            path.mkdir()
        (self.hal / "VERSION_HAL").write_text("old")
        (self.hal / ".env").write_text("keep-me")
        (self.web / "index.html").write_text("old")
        (self.hal / "ROBOT.md").write_text("old profile")
        (self.backup / "hal.service-state").write_text("active")
        (self.backup / "web.nginx-state").write_text("active")
        (self.backup / "device.services").write_text("hal=active\nos-server=active\n")
        (self.root / "bootstrap.json").write_text('{"httpPort":8080}')
        self.full_source = SCRIPT.read_text()
        for original, replacement in (
            ("/root/config/bootstrap.json", self.root / "bootstrap.json"),
            ("/root/bootstrap/rollback", self.backup),
            ("/root/bootstrap/progress", self.root / "progress"),
            ("/usr/share/nginx/html/setup", self.web),
            ("/usr/local/bin", self.root / "bin"),
            ("/opt/hal", self.hal),
        ):
            self.full_source = self.full_source.replace(original, str(replacement))
        self.source = self.full_source.split('[ "$(id -u)"', 1)[0]
        self.mocks = r'''
sync() { :; }
nginx() { return 0; }
systemctl() {
  printf '%s\n' "$*" >> "$TEST_ROOT/events"
  case "$1" in
    is-enabled) return 0 ;;
    is-active) [ "$3" != "${DEAD_UNIT:-}" ]; return $? ;;
  esac
  return 0
}
wait_for_web() { [ "${BAD_HEALTH:-0}" = 0 ]; }
wait_for_url() {
  [ "${BAD_HEALTH:-0}" = 0 ] || return 1
  if [ -f "$HAL_DIR/VERSION_HAL" ] && [ "$(cat "$HAL_DIR/VERSION_HAL")" = broken ]; then return 1; fi
  if [ -n "${CORE_BINARY:-}" ] && grep -q BROKEN "$CORE_BINARY"; then return 1; fi
  return 0
}
# GNU install -D/timeout are unavailable on macOS; preserve their semantics here.
install() {
  [ "$1" != -D ] || shift
  local mode="$2" source="$3" dest="$4"
  mkdir -p "$(dirname "$dest")" && cp "$source" "$dest" && chmod "$mode" "$dest"
}
timeout() { shift; "$@"; }
'''

    def run_shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c", self.source + self.mocks + "\n" + body],
            env={**os.environ, "TEST_ROOT": str(self.root), **env},
            capture_output=True, text=True, timeout=15,
        )

    def test_full_hal_updater_rejects_bad_candidate_and_preserves_old_env(self):
        for failure in ("startup", "sync", "none"):
            with self.subTest(failure=failure):
                (self.hal / "VERSION_HAL").write_text("old")
                package = self.root / "hal.zip"
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr("VERSION_HAL", "broken" if failure == "startup" else "new")
                    archive.writestr("server.py", "# candidate")
                (self.root / "metadata.json").write_text(json.dumps({"hal": {"version": "new", "url": "test://hal.zip"}}))
                fake_bin = self.root / "fake-bin"
                fake_bin.mkdir(exist_ok=True)
                driver = fake_bin / "driver"
                driver.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
name=pathlib.Path(sys.argv[0]).name
root=pathlib.Path(os.environ["TEST_ROOT"])
a=sys.argv[1:]
if name == "id": print("0")
elif name == "uv": sys.exit(1 if os.environ["FAILURE"] == "sync" else 0)
elif name == "systemctl":
 with (root/"full-events").open("a") as f: f.write(" ".join(a)+"\\n")
elif name == "curl":
 if "-o" in a: shutil.copyfile(root/a[-1].split("/")[-1], a[a.index("-o")+1])
 else:
  v=root/"hal/VERSION_HAL"
  sys.exit(0 if v.exists() and v.read_text() != "broken" else 22)
''')
                driver.chmod(0o755)
                for name in ("id", "uv", "systemctl", "curl", "sync", "sleep", "flock"):
                    link = fake_bin / name
                    if not link.exists(): link.symlink_to(driver)
                updater = self.root / "software-update"
                updater.write_text(self.full_source.replace("/var/lock/software-update", str(self.root / "software-update")))
                result = subprocess.run(
                    ["/bin/bash", str(updater), "hal"],
                    env={**os.environ, "PATH": str(fake_bin)+os.pathsep+os.environ["PATH"],
                         "TEST_ROOT": str(self.root), "SOFTWARE_UPDATE_DETACHED": "1",
                         "OTA_METADATA_URL": "test://metadata.json", "FAILURE": failure},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0 if failure == "none" else 1, result.stdout+result.stderr)
                progress = json.loads((self.root / "progress/hal.json").read_text())
                self.assertEqual(progress["phase"], "completed" if failure == "none" else "failed")
                if failure == "startup":
                    self.assertIn("HAL health check failed", result.stderr)
                elif failure == "sync":
                    self.assertIn("HAL staging failed", result.stderr)
                else:
                    self.assertIn("hal updated to new", result.stdout)
                self.assertEqual((self.hal / "VERSION_HAL").read_text(), "new" if failure == "none" else "old")
                self.assertEqual((self.hal / ".env").read_text(), "keep-me")
                self.assertFalse((self.backup / "pending-update.json").exists())
                self.assertEqual(list(self.root.glob(".hal.new.*")), [])

    def test_device_rejects_failed_hal_even_when_os_server_is_healthy(self):
        result = self.run_shell('DEVICE_DEST="$HAL_DIR"\nif check_device_profile; then exit 0; else exit 1; fi', DEAD_UNIT="hal")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_web_rejects_dead_nginx(self):
        result = self.run_shell('if check_web; then exit 0; else exit 1; fi', DEAD_UNIT="nginx")
        self.assertEqual(result.returncode, 1)

    def test_intentionally_disabled_web_can_skip_probe(self):
        result = self.run_shell('web_was_active() { return 1; }\nif check_web; then exit 0; else exit 1; fi', DEAD_UNIT="nginx", BAD_HEALTH="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_crash_at_each_publication_boundary_restores_original(self):
        for phase in ("prepared", "moved", "published"):
            with self.subTest(phase=phase):
                result = self.run_shell(r'''
mark_publish_pending "$HAL_DIR" "$ROLLBACK_DIR/hal.previous" restore_hal_service_state
if [ "$PHASE" != prepared ]; then mv "$HAL_DIR" "$ROLLBACK_DIR/hal.previous"; fi
if [ "$PHASE" = published ]; then mkdir "$HAL_DIR"; echo broken > "$HAL_DIR/VERSION_HAL"; fi
kill -KILL $$
''', PHASE=phase)
                self.assertEqual(result.returncode, -9)
                self.assertTrue((self.backup / "pending-update.json").is_file())
                restored = self.run_shell('recover_pending_update')
                self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
                self.assertEqual((self.hal / "VERSION_HAL").read_text(), "old")
                self.assertEqual((self.hal / ".env").read_text(), "keep-me")
                self.assertFalse((self.backup / "pending-update.json").exists())

    def test_postpublish_error_runs_rollback_in_exit_trap(self):
        result = self.run_shell(r'''
trap on_exit EXIT
mark_publish_pending "$HAL_DIR" "$ROLLBACK_DIR/hal.previous" restore_hal_service_state
mv "$HAL_DIR" "$ROLLBACK_DIR/hal.previous"
mkdir "$HAL_DIR"; echo broken > "$HAL_DIR/VERSION_HAL"
false
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.hal / "VERSION_HAL").read_text(), "old")
        self.assertFalse((self.backup / "pending-update.json").exists())
        self.assertEqual((self.root / "events").read_text().splitlines().count("restart hal"), 1)

    def test_committed_health_does_not_restart_again_on_exit(self):
        result = self.run_shell(r'''
trap on_exit EXIT
mark_publish_pending "$HAL_DIR" "$ROLLBACK_DIR/hal.previous" restore_hal_service_state
mv "$HAL_DIR" "$ROLLBACK_DIR/hal.previous"
mkdir "$HAL_DIR"; echo healthy > "$HAL_DIR/VERSION_HAL"
restore_hal_service_state
check_hal
clear_publish_pending
disarm_service_restore
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.backup / "pending-update.json").exists())
        self.assertEqual((self.root / "events").read_text().splitlines().count("restart hal"), 1)

    def test_recovery_retry_does_not_discard_restored_runtime(self):
        result = self.run_shell(r'''
mark_publish_pending "$HAL_DIR" "$ROLLBACK_DIR/hal.previous" restore_hal_service_state
mv "$HAL_DIR" "$ROLLBACK_DIR/hal.previous"
mkdir "$HAL_DIR"; echo broken > "$HAL_DIR/VERSION_HAL"
recover_pending_update
''', BAD_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.hal / "VERSION_HAL").read_text(), "old")
        self.assertTrue((self.backup / "pending-update.json").exists())
        retried = self.run_shell('recover_pending_update')
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertFalse((self.backup / "pending-update.json").exists())

    def test_legacy_missing_live_tree_preserves_sole_backup(self):
        self.hal.rename(self.backup / "hal.previous")
        result = self.run_shell('recover_pending_update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.hal / "VERSION_HAL").read_text(), "old")

    def test_failed_first_install_does_not_leave_installed_version(self):
        result = self.run_shell(r'''
rm -rf "$HAL_DIR"
trap on_exit EXIT
mark_publish_pending "$HAL_DIR" "$ROLLBACK_DIR/hal.previous" restore_hal_service_state
mkdir "$HAL_DIR"; echo broken > "$HAL_DIR/VERSION_HAL"
exit 1
''')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.hal.exists())
        self.assertTrue((self.backup / "hal.failed" / "VERSION_HAL").exists())
        self.assertFalse((self.backup / "pending-update.json").exists())

    def test_device_restart_failure_is_not_masked_by_hal_restart(self):
        result = self.run_shell(r'''
systemctl() { [ "$1 $2" != "restart os-server" ]; }
if restore_device_service_state; then exit 0; else exit 1; fi
''')
        self.assertEqual(result.returncode, 1)

    def test_core_binary_failed_start_rolls_back_and_success_commits(self):
        for unit, restore, check in (("os-server", "restore_os_server_service", "check_os_server"),
                                     ("bootstrap", "restore_bootstrap_service", "check_bootstrap")):
            for broken in (True, False):
                with self.subTest(unit=unit, broken=broken):
                    dest = self.root / "bin" / ("bootstrap-server" if unit == "bootstrap" else unit)
                    dest.write_text("#!/bin/sh\necho old\n")
                    dest.chmod(0o755)
                    candidate = self.root / "candidate"
                    candidate.write_text("#!/bin/sh\n# " + ("BROKEN" if broken else "HEALTHY") + "\necho new\n")
                    result = self.run_shell(f'trap on_exit EXIT\nupdate_core_binary {unit} "{dest}" "{candidate}" {restore} {check}', CORE_BINARY=str(dest))
                    self.assertEqual(result.returncode, 1 if broken else 0, result.stdout + result.stderr)
                    self.assertIn("echo old" if broken else "HEALTHY", dest.read_text())
                    self.assertFalse((self.backup / "pending-update.json").exists())

    def test_device_rootfs_copy_failure_retains_recovery_journal(self):
        result = self.run_shell(r'''
DEVICE_DEST="$HAL_DIR"
mkdir -p "$ROLLBACK_DIR/device.previous.rootfs/files"
printf 'tmp/first\tpresent\ntmp/second\tpresent\n' > "$ROLLBACK_DIR/device.previous.rootfs/manifest"
mark_publish_pending "$DEVICE_DEST" "$ROLLBACK_DIR/device.previous" restore_device_service_state
mv "$DEVICE_DEST" "$ROLLBACK_DIR/device.previous"
mkdir "$DEVICE_DEST"; echo broken > "$DEVICE_DEST/ROBOT.md"
# Do not touch host /tmp: fail the first restore copy, permit any later call.
mkdir() { return 0; }
cp() { [ "$2" != "$ROLLBACK_DIR/device.previous.rootfs/files/tmp/first" ]; }
if recover_pending_update; then exit 0; else exit 1; fi
''')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue((self.backup / "pending-update.json").exists())


if __name__ == "__main__":
    unittest.main()
