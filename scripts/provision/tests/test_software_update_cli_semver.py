"""cli_semver must return the FIRST version in a `--version` output."""

from pathlib import Path
import re
import subprocess
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


def cli_semver(output):
    result = subprocess.run(
        ["bash", "-c", shell_function("cli_semver") + "\ncli_semver"],
        input=output,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


class CliSemverTest(unittest.TestCase):
    def test_hermes_021_output_with_calendar_build(self):
        # The greedy sed returned "6.9.7" here and failed a correct install.
        out = (
            "Hermes Agent v0.21.1 (2026.9.7) · upstream 6da966f2 · "
            "local 2237be35 (+32598 carried commits)\n"
            "Install directory: /usr/local/lib/hermes-agent\n"
        )
        self.assertEqual(cli_semver(out), "0.21.1")

    def test_hermes_019_output(self):
        self.assertEqual(cli_semver("Hermes Agent v0.19.1 (2026.7.30)\n"), "0.19.1")

    def test_other_runtimes(self):
        self.assertEqual(cli_semver("codex-cli 0.142.5\n"), "0.142.5")
        self.assertEqual(cli_semver("2.1.220 (Claude Code)\n"), "2.1.220")
        self.assertEqual(cli_semver("OpenClaw 2026.6.10 (aa69b12)\n"), "2026.6.10")
        self.assertEqual(cli_semver("0.1.171\n"), "0.1.171")

    def test_keeps_prerelease_suffix(self):
        self.assertEqual(cli_semver("tool 1.2.3-rc.1 (build 4.5.6)\n"), "1.2.3-rc.1")

    def test_no_version(self):
        self.assertEqual(cli_semver("nightly-44-g1959045c-dirty\n"), "")
        self.assertEqual(cli_semver(""), "")


if __name__ == "__main__":
    unittest.main()
