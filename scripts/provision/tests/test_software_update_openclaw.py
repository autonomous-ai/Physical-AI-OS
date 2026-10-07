"""Exercise agent updaters without network, package, or service changes."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "software-update"


def shell_function(name):
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$",
        SCRIPT.read_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing shell function: {name}")
    return match.group(0)


MOCKS = r'''
event() { printf '%s\n' "$*" >> "$EVENTS"; }
npm() {
  event "npm $*"
  if [ "$1" = view ]; then
    [ "$FAIL" != metadata ] || return 1
    case "$FAIL" in
      null_metadata) printf '%s\n' 'null' ;;
      empty_metadata) printf '%s\n' '""' ;;
      *) printf '%s\n' '">=24.16.0 <25 || >=26.1.0"' ;;
    esac
  elif [ "$1" = install ]; then
    [ "$FAIL" != install ] || return 1
  fi
}
node_satisfies_engine() {
  event "engine $*"
  [ "$FAIL" != checker ] || return 2
  [ "$SCENARIO" = compatible ] && return 0
  [ "$FAIL" != incompatible ] && [ -f "$UPGRADED" ]
}
node() { printf '%s\n' 'v22.23.1'; }
curl() {
  event "curl $*"
  [ "$FAIL" != download ]
}
bash() {
  event "bash $*"
  [ "$FAIL" != setup ]
}
apt-get() {
  event "apt-get $*"
  [ "$FAIL" != apt ] || return 1
  touch "$UPGRADED"
}
openclaw() {
  event "openclaw $*"
  case "$1" in
    doctor)
      event "doctor-env $HOME $OPENCLAW_HOME $OPENCLAW_STATE_DIR"
      [ "$FAIL" != doctor ] ;;
    gateway)
      event "gateway-env $HOME $OPENCLAW_HOME $OPENCLAW_STATE_DIR"
      PROBES=$((PROBES + 1))
      [ "$PROBES" -gt "$READINESS_FAILURES" ] ;;
  esac
}
systemctl() {
  event "systemctl $*"
  [ "$FAIL" != "$1" ]
}
sleep() { event "sleep $*"; }
hermes() {
  event "hermes $*"
  [ "$FAIL" != hermes ]
}
'''


class OpenClawUpdateTests(unittest.TestCase):
    def run_update(self, scenario="incompatible", failure="", readiness_failures=0,
                   component="openclaw"):
        functions = "\n".join(
            shell_function(name)
            for name in ("progress_report", "ensure_node_engine", "ensure_openclaw_node",
                         "update_openclaw", "update_hermes_package")
        )
        command = ('update_openclaw "2026.9.3"' if component == "openclaw"
                   else "update_hermes_package")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            events = directory / "events"
            result = subprocess.run(
                ["/bin/bash", "-c", "set -e\n" + functions + MOCKS
                 + "\n" + command + "\n"],
                env={
                    **os.environ,
                    "EVENTS": str(events),
                    "UPGRADED": str(directory / "upgraded"),
                    "TMPDIR": temporary,
                    "SCENARIO": scenario,
                    "FAIL": failure,
                    "PROBES": "0",
                    "READINESS_FAILURES": str(readiness_failures),
                    "HOME": str(directory / "ssh-home"),
                    "OPENCLAW_HOME": str(directory / "ssh-openclaw"),
                    "OPENCLAW_STATE_DIR": str(directory / "ssh-state"),
                },
                capture_output=True,
                text=True,
            )
            return result, events.read_text().splitlines() if events.exists() else []

    def test_compatible_node_installs_without_upgrade(self):
        result, events = self.run_update(scenario="compatible")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("npm view openclaw@2026.9.3 engines.node --json", events)
        self.assertIn("npm install -g openclaw@2026.9.3", events)
        self.assertIn("systemctl restart openclaw", events)
        self.assertFalse(any(line.startswith(("curl ", "bash ", "apt-get "))
                             for line in events), events)

    def test_incompatible_node_upgrades_and_rechecks_before_install(self):
        result, events = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any("setup_24.x" in line for line in events), events)
        install = events.index("npm install -g openclaw@2026.9.3")
        upgrade = events.index("apt-get install -y nodejs")
        checks = [index for index, line in enumerate(events)
                  if line.startswith("engine ")]
        self.assertEqual(len(checks), 2, events)
        self.assertLess(checks[0], upgrade)
        self.assertLess(upgrade, checks[1])
        self.assertLess(checks[1], install)
        self.assertLess(install, events.index("systemctl restart openclaw"))

    def test_failures_prevent_package_install_and_service_restart(self):
        for failure in ("metadata", "null_metadata", "empty_metadata", "checker",
                        "download", "setup", "apt", "incompatible"):
            with self.subTest(failure=failure):
                result, events = self.run_update(failure=failure)
                self.assertNotEqual(result.returncode, 0, events)
                self.assertFalse(any(line.startswith("npm install ")
                                     for line in events), events)
                self.assertFalse(any(line.startswith("systemctl ")
                                     for line in events), events)
                if failure in ("metadata", "null_metadata", "empty_metadata", "checker"):
                    self.assertFalse(any(line.startswith(("curl ", "bash ", "apt-get "))
                                         for line in events), events)

    def test_package_install_failure_does_not_restart(self):
        result, events = self.run_update(scenario="compatible", failure="install")
        self.assertNotEqual(result.returncode, 0, events)
        self.assertIn("npm install -g openclaw@2026.9.3", events)
        self.assertFalse(any(line.startswith(("systemctl ", "openclaw "))
                             for line in events), events)

    def test_migration_runs_stopped_with_system_state_before_readiness(self):
        result, events = self.run_update(scenario="compatible")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ordered = [
            "npm install -g openclaw@2026.9.3",
            "openclaw plugins install @openclaw/discord@2026.9.3 --force",
            "openclaw plugins install @openclaw/slack@2026.9.3 --force",
            "systemctl stop openclaw",
            "openclaw doctor --fix --non-interactive --no-workspace-suggestions",
            "doctor-env /root /root/.openclaw /root/.openclaw",
            "systemctl restart openclaw",
            "openclaw gateway status --require-rpc --timeout 5000",
            "gateway-env /root /root/.openclaw /root/.openclaw",
        ]
        positions = [events.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions), events)
        self.assertIn("openclaw updated to 2026.9.3", result.stdout)

    def test_migration_service_failures_abort_following_steps(self):
        for failure, forbidden in (
            ("stop", ("openclaw doctor", "systemctl restart", "openclaw gateway")),
            ("doctor", ("systemctl restart", "openclaw gateway")),
            ("restart", ("openclaw gateway",)),
        ):
            with self.subTest(failure=failure):
                result, events = self.run_update(scenario="compatible", failure=failure)
                self.assertNotEqual(result.returncode, 0, events)
                self.assertFalse(any(event.startswith(forbidden) for event in events), events)
                self.assertNotIn("openclaw updated to", result.stdout)
                if failure == "doctor":
                    self.assertIn("gateway left stopped", result.stderr)

    def test_readiness_retries_until_gateway_responds(self):
        result, events = self.run_update(scenario="compatible", readiness_failures=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events.count("openclaw gateway status --require-rpc --timeout 5000"), 3)
        self.assertEqual(events.count("sleep 5"), 2)
        self.assertIn("openclaw updated to 2026.9.3", result.stdout)

    def test_readiness_exhaustion_fails_without_success_message(self):
        result, events = self.run_update(scenario="compatible", readiness_failures=12)
        self.assertNotEqual(result.returncode, 0, events)
        self.assertEqual(events.count("openclaw gateway status --require-rpc --timeout 5000"), 12)
        self.assertEqual(events.count("sleep 5"), 11)
        self.assertNotIn("openclaw updated to", result.stdout)
        self.assertIn("did not become ready", result.stderr)

    def test_hermes_compatible_node_updates_without_upgrade(self):
        result, events = self.run_update(scenario="compatible", component="hermes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events, [
            "engine >=22.22.0 <23 || >=24.11.0 <25 || >=26.0.0",
            "hermes update",
        ])

    def test_hermes_incompatible_node_upgrades_and_rechecks_before_update(self):
        result, events = self.run_update(component="hermes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any("setup_24.x" in event for event in events), events)
        checks = [index for index, event in enumerate(events)
                  if event.startswith("engine ")]
        self.assertEqual(len(checks), 2, events)
        upgrade = events.index("apt-get install -y nodejs")
        self.assertLess(checks[0], upgrade)
        self.assertLess(upgrade, checks[1])
        self.assertLess(checks[1], events.index("hermes update"))

    def test_hermes_node_setup_failures_prevent_update(self):
        for failure in ("download", "setup", "apt", "checker", "incompatible"):
            with self.subTest(failure=failure):
                result, events = self.run_update(component="hermes", failure=failure)
                self.assertNotEqual(result.returncode, 0, events)
                self.assertNotIn("hermes update", events)
                if failure == "checker":
                    self.assertFalse(any(event.startswith(("curl ", "bash ", "apt-get "))
                                         for event in events), events)

    def test_hermes_update_failure_is_propagated(self):
        result, events = self.run_update(scenario="compatible", component="hermes",
                                         failure="hermes")
        self.assertNotEqual(result.returncode, 0, events)
        self.assertIn("hermes update", events)


if __name__ == "__main__":
    unittest.main()
