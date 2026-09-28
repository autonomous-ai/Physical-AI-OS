"""Real staged rendering, including byte-identical legacy upgrades."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from hal.board.device import parse_device
from hal.drivers.environment.group import create_environment_group


REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "robots/lamp"
spec = importlib.util.spec_from_file_location("overrides", REPO / "scripts/provision/apply-overrides.py")
overrides = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overrides)


class DeviceOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "staged-device"
        for name in ("ROBOT.md", "SAFETY.md", "rootfs/opt/hal/.env", "rootfs/etc/asound.conf"):
            dest = self.profile / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, dest)
        shutil.copytree(SOURCE / "overrides", self.profile / "overrides")
        for source in SOURCE.glob("*.json"):
            shutil.copy2(source, self.profile / source.name)

    def select(self, value):
        path = self.root / overrides.PROFILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.profile)): p.read_bytes()
                for p in self.profile.rglob("*") if p.is_file()}

    def test_legacy_noop_for_missing_empty_or_standard_identity(self):
        shutil.rmtree(self.profile / "overrides")
        before = self.snapshot()
        self.assertIsNone(overrides.apply_overrides(self.profile, self.root))
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / overrides.PROFILE_PATH).exists())
        for value in ("", "\n", "standard\n", " standard \n"):
            self.select(value)
            self.assertIsNone(overrides.apply_overrides(self.profile, self.root))
            self.assertEqual(before, self.snapshot())

    def test_respeaker_lite_audio_preserves_other_settings_and_identity(self):
        identity = self.select("pro-respeaker-lite\n")
        self.assertEqual(overrides.apply_overrides(self.profile, self.root), "pro-respeaker-lite")
        env = (self.profile / "rootfs/opt/hal/.env").read_text()
        for value in ("HAL_AEC_ENABLED=false", "HAL_LIVE_MODE=true",
                      "HAL_SILERO_THRESHOLD=0.10",
                      "HAL_VOLUME_STATE_PATH=/root/config/.volume-pro-respeaker-lite", "HAL_TTS_SPEED=1.1",
                      "HAL_LIVE_UPLINK_DURING_PLAYBACK=always", "HAL_LIVE_DUCK_GAIN=0.12",
                      "HAL_LIVE_VAD_START_SENSITIVITY=high", "HAL_LIVE_VAD_END_SENSITIVITY=high",
                      "HAL_LIVE_VAD_PREFIX_PADDING_MS=100", "HAL_LIVE_VAD_SILENCE_MS=1000",
                      "HAL_GEMINI_USE_LANGUAGE_CODES=true"):
            self.assertIn(value + "\n", env)
        self.assertIn("startup_volume: 35", (self.profile / "ROBOT.md").read_text())
        self.assertIn("max_volume: 35", (self.profile / "SAFETY.md").read_text())
        self.assertIn("max_speed: 120", (self.profile / "SAFETY.md").read_text())
        alsa = (self.profile / "rootfs/etc/asound.conf").read_text()
        self.assertEqual(alsa, (SOURCE / "overrides/pro-respeaker-lite/rootfs/etc/asound.conf").read_text())
        self.assertIn('slave { pcm "respeaker_capture_shared" channels 2 }', alsa)
        self.assertIn("ctl.device_micro1 { type hw card sndi2s4 }", alsa)
        self.assertEqual(identity.read_text(), "pro-respeaker-lite\n")
        self.assertFalse((self.root / "opt/hal/.env").exists())

    def test_pro_only_adds_environment_to_standard(self):
        before = self.snapshot()
        self.select("pro")
        overrides.apply_overrides(self.profile, self.root)
        after = self.snapshot()
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        self.assertEqual(changed, {"ROBOT.md", "sen63c.json", "rootfs/opt/hal/.env"})
        # The generic renderer namespaces saved volume for every selected profile;
        # no microphone, playback, processing or volume-policy overrides exist.
        env = (SOURCE / "rootfs/opt/hal/.env").read_text()
        self.assertEqual(
            (self.profile / "rootfs/opt/hal/.env").read_text(),
            overrides.merge_env(env, "HAL_VOLUME_STATE_PATH=/root/config/.volume-pro"),
        )
        for name in ("rootfs/etc/asound.conf", "SAFETY.md"):
            self.assertEqual((self.profile / name).read_bytes(), (SOURCE / name).read_bytes())
        self.assertEqual(
            (self.profile / "ROBOT.md").read_text().replace("  environment:", "  # environment:"),
            (SOURCE / "ROBOT.md").read_text(),
        )
        self.assertFalse((self.profile / "overrides/pro/rootfs").exists())

    def test_pro_xvf3800_keeps_the_array_tuning(self):
        # The reSpeaker XVF3800 assembly tested before the ReSpeaker Lite: live
        # mode with every frame uplinked and no software AEC, 77% volume.
        self.select("pro-xvf3800\n")
        self.assertEqual(overrides.apply_overrides(self.profile, self.root), "pro-xvf3800")
        env = (self.profile / "rootfs/opt/hal/.env").read_text()
        for value in ("HAL_AEC_ENABLED=false", "HAL_LIVE_MODE=true",
                      "HAL_LIVE_UPLINK_DURING_PLAYBACK=always", "HAL_LIVE_DUCK_GAIN=0.12",
                      "HAL_LIVE_VAD_START_SENSITIVITY=high", "HAL_LIVE_VAD_END_SENSITIVITY=high",
                      "HAL_LIVE_VAD_PREFIX_PADDING_MS=100", "HAL_LIVE_VAD_SILENCE_MS=1000",
                      "HAL_GEMINI_USE_LANGUAGE_CODES=true",
                      "HAL_VOLUME_STATE_PATH=/root/config/.volume-pro-xvf3800"):
            self.assertIn(value + "\n", env)
        self.assertIn("max_volume: 77", (self.profile / "SAFETY.md").read_text())
        self.assertIn("card Array", (self.profile / "rootfs/etc/asound.conf").read_text())

    def test_profiles_select_aec_without_an_extra_adaptive_gate_flag(self):
        for profile in ("standard", "pro", "pro-xvf3800", "pro-respeaker-lite"):
            with self.subTest(profile=profile):
                self.select(profile)
                overrides.apply_overrides(self.profile, self.root)
                env = (self.profile / "rootfs/opt/hal/.env").read_text()
                self.assertNotIn("HAL_LITE_ADAPTIVE_GATE", env)
                self.assertIn("HAL_AEC_ENABLED=" + ("true" if profile in ("standard", "pro") else "false") + "\n", env)

    def test_arbitrary_profile_uses_package_data_without_product_logic(self):
        (self.profile / "overrides/pro").rename(self.profile / "overrides/studio")
        (self.profile / "overrides/studio/profile.json").write_text(json.dumps({"startup_volume": 42, "max_volume": 55}))
        self.select("studio")
        self.assertEqual(overrides.apply_overrides(self.profile, self.root), "studio")
        self.assertIn("startup_volume: 42", (self.profile / "ROBOT.md").read_text())
        self.assertIn("max_volume: 55", (self.profile / "SAFETY.md").read_text())
        self.assertIn(".volume-studio", (self.profile / "rootfs/opt/hal/.env").read_text())

    def test_invalid_or_unknown_profile_fails_without_changes(self):
        for value in ("../pro", "/pro", "PRO", "unknown"):
            self.select(value)
            before = self.snapshot()
            with self.assertRaises((OSError, ValueError)):
                overrides.apply_overrides(self.profile, self.root)
            self.assertEqual(before, self.snapshot())

    def test_profile_cannot_overwrite_identity(self):
        self.select("pro")
        target = self.profile / "overrides/pro/rootfs" / overrides.PROFILE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("standard")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "identity"):
            overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())

    def test_invalid_volume_policy_fails_without_changes(self):
        self.select("pro")
        (self.profile / "overrides/pro/profile.json").write_text('{"startup_volume":90,"max_volume":40}')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "exceeds"):
            overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())

    def test_repeat_render_and_new_ota_do_not_lose_selection(self):
        identity = self.select("pro")
        overrides.apply_overrides(self.profile, self.root)
        first = self.snapshot()
        overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(first, self.snapshot())
        for name in ("ROBOT.md", "SAFETY.md", "rootfs/opt/hal/.env", "rootfs/etc/asound.conf"):
            shutil.copy2(SOURCE / name, self.profile / name)
        overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(first, self.snapshot())
        self.assertEqual(identity.read_text(), "pro")

    def test_volume_state_is_selected_without_env_overlay(self):
        self.select("pro-respeaker-lite")
        (self.profile / "overrides/pro-respeaker-lite/rootfs/opt/hal/.env").unlink()
        overrides.apply_overrides(self.profile, self.root)
        self.assertIn("HAL_VOLUME_STATE_PATH=/root/config/.volume-pro-respeaker-lite", (self.profile / "rootfs/opt/hal/.env").read_text())

    def test_standard_environment_is_disabled(self):
        self.select("standard")
        overrides.apply_overrides(self.profile, self.root)
        robot = (self.profile / "ROBOT.md").read_text()
        self.assertNotRegex(robot, r"(?m)^  environment:")
        self.assertNotIn("environment", parse_device("lamp", robot).declared_routes())
        sensor = json.loads((self.profile / "sen63c.json").read_text())
        self.assertFalse(sensor["boards"]["orangepi_sun60"]["enabled"])

    def test_all_pro_profiles_enable_environment_and_sensor(self):
        for name in ("pro", "pro-respeaker-lite", "pro-xvf3800"):
            with self.subTest(profile=name):
                for filename in ("ROBOT.md", "sen63c.json"):
                    shutil.copy2(SOURCE / filename, self.profile / filename)
                self.select(name)
                overrides.apply_overrides(self.profile, self.root)
                self.assertRegex((self.profile / "ROBOT.md").read_text(), r"(?m)^  environment:")
                sensor = json.loads((self.profile / "sen63c.json").read_text())
                self.assertTrue(sensor["boards"]["orangepi_sun60"]["enabled"])
                self.assertEqual(sensor["boards"]["orangepi_sun60"]["bus"], 0)
                parsed = parse_device("lamp", (self.profile / "ROBOT.md").read_text())
                self.assertIn("environment", parsed.declared_routes())
                group = create_environment_group(str(self.profile), "orangepi_sun60")
                self.assertEqual([key for key, worker in group.components.items() if worker.enabled], ["sen63c"])
                first = self.snapshot()
                overrides.apply_overrides(self.profile, self.root)
                self.assertEqual(first, self.snapshot())

    def test_fresh_standard_package_removes_previous_pro_environment(self):
        self.select("pro")
        overrides.apply_overrides(self.profile, self.root)
        self.assertIn("environment", parse_device("lamp", (self.profile / "ROBOT.md").read_text()).capabilities)
        # Profile changes render a fresh package; they do not undo a prior render.
        for filename in ("ROBOT.md", "SAFETY.md", "rootfs/opt/hal/.env", "rootfs/etc/asound.conf"):
            shutil.copy2(SOURCE / filename, self.profile / filename)
        for source in SOURCE.glob("*.json"):
            shutil.copy2(source, self.profile / source.name)
        self.select("standard")
        before = self.snapshot()
        overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("environment", parse_device("lamp", (self.profile / "ROBOT.md").read_text()).capabilities)
        group = create_environment_group(str(self.profile), "orangepi_sun60")
        self.assertFalse(any(worker.enabled for worker in group.components.values()))

    def test_capabilities_preserve_unrelated_content_and_can_disable(self):
        text = "---\ncapabilities:\n  audio: { required: true }\n  # environment: { routes: [environment], required: false } # keep\nother:\n  environment: elsewhere\n---\nBody\n  environment: body\n"
        enabled = overrides.capability_fields(text, {"environment": True})
        self.assertEqual(enabled, text.replace("  # environment:", "  environment:", 1))
        self.assertEqual(overrides.capability_fields(enabled, {"environment": False}), text)
        self.assertEqual(overrides.capability_fields(text, {"environment": False}), text)

    def test_invalid_capabilities_fail_without_partial_writes(self):
        self.select("pro")
        settings = self.profile / "overrides/pro/profile.json"
        robot = self.profile / "ROBOT.md"
        original = robot.read_text()
        cases = [[], {"../environment": True}, {"environment": 1},
                 {"environment": "true"}, {"missing": True}]
        for capabilities in cases:
            with self.subTest(capabilities=capabilities):
                settings.write_text(json.dumps({"startup_volume": 42, "capabilities": capabilities}))
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    overrides.apply_overrides(self.profile, self.root)
                self.assertEqual(before, self.snapshot())
        settings.write_text(json.dumps({"startup_volume": 42, "capabilities": {"environment": True}}))
        robot.write_text(original.replace("capabilities:\n", "capabilities:\n  environment: { required: false }\n", 1))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "expected one declaration"):
            overrides.apply_overrides(self.profile, self.root)
        self.assertEqual(before, self.snapshot())

    def test_invalid_device_overrides_fail_without_partial_writes(self):
        self.select("pro")
        device = self.profile / "overrides/pro/device"
        for filename, content in (("sen63c.json", "not json"),
                                  ("sen63c.json", "[]"),
                                  ("sen63c.json", "null"),
                                  ("unknown.json", "{}"),
                                  ("ROBOT.md", "{}"),
                                  ("nested/sen63c.json", "{}")):
            with self.subTest(filename=filename, content=content):
                shutil.rmtree(device, ignore_errors=True)
                source = device / filename
                source.parent.mkdir(parents=True)
                source.write_text(content)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    overrides.apply_overrides(self.profile, self.root)
                self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
