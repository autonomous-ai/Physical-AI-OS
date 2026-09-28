"""Tests for the device-profile layer: ROBOT.md parsing + mount planning.

Pure logic, no hardware. Also parses the REAL committed robots/lamp and
robots/intern-v2 ROBOT.md files to guard the contract against drift.
"""
import os
import re
import shutil
import tempfile
import unittest

from hal.board.device import (
    DEFAULT_STARTUP_VOLUME,
    Capability,
    MountPlan,
    extract_front_matter,
    load_device,
    parse_capabilities,
    parse_device,
    plan_mounts,
    profile_path,
    validate_safety_refs,
    validate_schema,
)

HERE = os.path.dirname(os.path.abspath(__file__))
# hal/test -> hal -> repo root
DEVICES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "robots"))

SAMPLE = """---
schema: autonomous.device.v1
id: sample
name: Sample Device
type: test_device
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true }
  motion: { routes: [servo], driver: feetech, required: false, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: autonomous://souls/sample
---

# body text ignored
"""

# A body whose hardware is held by a vendor process — `owner:` alongside
# `driver:` on the same capability, which is how Reachy Mini declares it.
SAMPLE_OWNED = """---
schema: autonomous.device.v1
id: sample-owned
name: Sample Owned Device
type: test_device
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true, owner: pollen_daemon }
  vision: { routes: [camera], driver: rpicam, required: true, owner: pollen_daemon }
  system: { routes: [system], required: true }
soul_ref: autonomous://souls/sample
---

# body text ignored
"""


class TestParsing(unittest.TestCase):
    def test_extract_front_matter(self):
        fm = extract_front_matter(SAMPLE)
        self.assertIn("capabilities:", fm)
        self.assertNotIn("body text ignored", fm)

    def test_parse_capabilities_flow_style(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(set(caps), {"audio", "motion", "system"})
        self.assertEqual(caps["audio"].routes, ["audio", "speaker", "voice"])
        self.assertTrue(caps["audio"].required)
        self.assertEqual(caps["motion"].routes, ["servo"])
        self.assertFalse(caps["motion"].required)

    def test_parse_capability_driver(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(caps["motion"].driver, "feetech")  # informational family
        self.assertIsNone(caps["audio"].driver)             # none declared

    def test_parse_capability_owner(self):
        # `owner:` is optional and absent on a device HAL owns outright, which
        # is what makes the media handover a no-op everywhere but Reachy.
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertIsNone(caps["audio"].owner)
        self.assertIsNone(caps["motion"].owner)

        owned = parse_capabilities(extract_front_matter(SAMPLE_OWNED))
        self.assertEqual(owned["audio"].owner, "pollen_daemon")
        self.assertEqual(owned["vision"].owner, "pollen_daemon")
        # Parsed alongside driver on the same line, not instead of it.
        self.assertEqual(owned["vision"].driver, "rpicam")
        self.assertIsNone(owned["system"].owner)

    def test_lamp_declares_no_media_owner(self):
        # Lamp opens its own hardware; an owner here would make HAL wait on a
        # handover that never answers.
        for cap in load_device("lamp", DEVICES_DIR).capabilities.values():
            self.assertIsNone(cap.owner, f"lamp {cap.group} should have no owner")

    def test_reachy_declares_pollen_daemon_owner(self):
        # The Pollen daemon holds the camera and both ALSA PCMs until asked to
        # let go. Undeclared, HAL opens them "busy" and TTS lands on device -1.
        caps = load_device("reachy-mini", DEVICES_DIR).capabilities
        self.assertEqual(caps["audio"].owner, "pollen_daemon")
        self.assertEqual(caps["vision"].owner, "pollen_daemon")

    def test_lamp_real_drivers(self):
        caps = load_device("lamp", DEVICES_DIR).capabilities
        self.assertEqual(caps["motion"].driver, "feetech")
        self.assertEqual(caps["light"].driver, "ws2812")
        # Lamp has no screen — declaring display would make HAL run a
        # framebuffer-only render loop nobody sees (see ROBOT.md).
        self.assertNotIn("display", caps)

    def test_so101_declares_only_the_policy_interface(self):
        profile = load_device("so101", DEVICES_DIR)
        self.assertEqual(set(profile.capabilities), {"vision", "policy", "system"})
        self.assertEqual(profile.capabilities["vision"].routes, ["camera"])
        self.assertEqual(profile.capabilities["policy"].routes, ["policy"])
        self.assertTrue(profile.capabilities["policy"].required)
        self.assertNotIn("motion", profile.capabilities)
        plan = plan_mounts(
            profile.declared_routes(), {"camera": True, "policy": True, "system": True}
        )
        self.assertEqual(set(plan.mounted), {"camera", "policy", "system"})

    def test_safety_ref_parsed(self):
        # SAMPLE declares no top-level safety_ref; lamp declares SAFETY.md.
        self.assertEqual(parse_device("sample", SAMPLE).safety_ref, "")
        self.assertEqual(load_device("lamp", DEVICES_DIR).safety_ref, "SAFETY.md")

    def test_memory_backend_parsed(self):
        # SAMPLE declares no memory block; lamp declares { backend: local }.
        self.assertEqual(parse_device("sample", SAMPLE).memory_backend, "")
        self.assertEqual(load_device("lamp", DEVICES_DIR).memory_backend, "local")

    def test_startup_volume_parsed(self):
        # Real bodies declare their own level; the parser must not flatten them
        # to one number — restoring the speaker after a media handover reads it.
        self.assertEqual(load_device("lamp", DEVICES_DIR).startup_volume, 75)
        self.assertEqual(load_device("reachy-mini", DEVICES_DIR).startup_volume, 100)

    def test_startup_volume_defaults_when_absent_or_out_of_range(self):
        # SAMPLE declares none. Fail-safe to max, never to silent.
        self.assertEqual(parse_device("sample", SAMPLE).startup_volume, DEFAULT_STARTUP_VOLUME)
        for bad in ("101", "-5", "loud"):
            md = SAMPLE.replace("soul_ref:", f"startup_volume: {bad}\nsoul_ref:")
            self.assertEqual(
                parse_device("sample", md).startup_volume, DEFAULT_STARTUP_VOLUME, bad
            )

    def test_startup_volume_matches_go_default(self):
        # Two runtimes parse this field (hal/board/device.py and Go's
        # system/device/devicemd.go) and both restore the speaker. A drift
        # between their defaults is a device that boots at two levels.
        go_src = os.path.join(
            os.path.dirname(DEVICES_DIR), "system", "device", "devicemd.go"
        )
        with open(go_src) as f:
            m = re.search(r"DefaultStartupVolume\s*=\s*(\d+)", f.read())
        self.assertIsNotNone(m, "DefaultStartupVolume not found in devicemd.go")
        self.assertEqual(int(m.group(1)), DEFAULT_STARTUP_VOLUME)

    def test_declared_routes_required_rollup(self):
        dev = parse_device("sample", SAMPLE)
        routes = dev.declared_routes()
        self.assertTrue(routes["audio"])      # required cap
        self.assertFalse(routes["servo"])     # optional cap
        self.assertTrue(routes["system"])


class TestSchemaValidation(unittest.TestCase):
    def test_parse_device_sets_schema(self):
        self.assertEqual(parse_device("sample", SAMPLE).schema, "autonomous.device.v1")

    def test_valid_schema_returns_tag(self):
        self.assertEqual(
            validate_schema("schema: autonomous.device.v1\n"), "autonomous.device.v1"
        )

    def test_missing_schema_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("id: x\ncapabilities:\n")

    def test_malformed_schema_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: autonomous.device.1\n")  # no 'v'
        with self.assertRaises(ValueError):
            validate_schema("schema: some.other.v1\n")        # wrong namespace

    def test_unknown_major_fails_loud(self):
        with self.assertRaises(ValueError):
            validate_schema("schema: autonomous.device.v2\n")

    def test_real_devices_declare_v1(self):
        self.assertEqual(load_device("lamp", DEVICES_DIR).schema, "autonomous.device.v1")
        self.assertEqual(load_device("intern-v2", DEVICES_DIR).schema, "autonomous.device.v1")


class TestIdentityFields(unittest.TestCase):
    def test_parse_id_name_type(self):
        dev = parse_device("sample", SAMPLE)
        self.assertEqual(dev.id, "sample")
        self.assertEqual(dev.name, "Sample Device")
        self.assertEqual(dev.type, "test_device")

    def test_id_must_match_folder(self):
        # SAMPLE declares id: sample; loading it as a different device_type aborts.
        with self.assertRaises(ValueError):
            parse_device("other", SAMPLE)

    def test_real_devices_id_equals_folder(self):
        for t in ("lamp", "intern-v2", "unitree-go2w"):
            self.assertEqual(load_device(t, DEVICES_DIR).id, t)


class TestBoardsField(unittest.TestCase):
    def test_parse_boards_flow_list(self):
        dev = parse_device("sample", SAMPLE)
        self.assertEqual(dev.boards, [])  # SAMPLE declares none

    def test_lamp_declares_its_boards(self):
        lamp = load_device("lamp", DEVICES_DIR)
        self.assertIn("orangepi_sun60", lamp.boards)
        self.assertIn("raspberry_pi_5", lamp.boards)


class TestRealDeviceFiles(unittest.TestCase):
    def test_lamp_is_maximal(self):
        lamp = load_device("lamp", DEVICES_DIR)
        groups = set(lamp.capabilities)
        # Lamp is the maximal device: it has motion and vision — but no
        # display (no screen; expression goes through /emotion instead).
        self.assertIn("motion", groups)
        self.assertIn("vision", groups)
        self.assertNotIn("display", groups)
        self.assertTrue(lamp.capabilities["audio"].required)

    def test_intern_v2_capabilities(self):
        intern = load_device("intern-v2", DEVICES_DIR)
        groups = set(intern.capabilities)
        # Intern-v2 declares exactly these: a desk agent with voice, ambient
        # sensing, an LED ring, music, and Bluetooth — but no camera, no servo,
        # no screen, and no /emotion route (it drives its LED via `light`).
        self.assertEqual(groups, {"audio", "sensing", "companion", "system", "light", "media", "connectivity"})
        self.assertNotIn("vision", groups)
        self.assertNotIn("motion", groups)
        self.assertNotIn("display", groups)
        self.assertTrue(intern.capabilities["audio"].required)


class TestSafetyRefs(unittest.TestCase):
    def test_parse_capabilities_sets_safety(self):
        caps = parse_capabilities(extract_front_matter(SAMPLE))
        self.assertEqual(caps["motion"].safety, "SAFETY.md#motion")

    def test_capability_without_safety_defaults_none(self):
        cap = Capability(group="audio", routes=["audio"], required=True)
        self.assertIsNone(cap.safety)

    def test_validate_clean_when_anchor_exists(self):
        dev = parse_device("sample", SAMPLE)
        problems = validate_safety_refs(dev, "# Safety\n\n## motion\n\nrules here\n")
        self.assertEqual(problems, [])

    def test_validate_warns_when_anchor_missing(self):
        dev = parse_device("sample", SAMPLE)
        problems = validate_safety_refs(dev, "# Safety\n\n## light\n\nrules here\n")
        self.assertTrue(problems)
        self.assertIn("motion", problems[0])

    def test_validate_warns_when_safety_md_empty(self):
        dev = parse_device("sample", SAMPLE)
        self.assertTrue(validate_safety_refs(dev, ""))

    def test_lamp_real_refs_validate_clean(self):
        lamp = load_device("lamp", DEVICES_DIR)
        safety_path = os.path.join(DEVICES_DIR, "lamp", "SAFETY.md")
        with open(safety_path, "r") as f:
            self.assertEqual(validate_safety_refs(lamp, f.read()), [])

    def test_so101_interface_ref_validates_clean(self):
        so101 = load_device("so101", DEVICES_DIR)
        safety_path = os.path.join(DEVICES_DIR, "so101", "SAFETY.md")
        with open(safety_path, "r") as f:
            self.assertEqual(validate_safety_refs(so101, f.read()), [])


class TestMountPlanning(unittest.TestCase):
    def test_declared_present_is_mounted(self):
        plan = plan_mounts({"audio": True, "servo": False}, {"audio": True, "servo": True})
        self.assertEqual(set(plan.mounted), {"audio", "servo"})
        self.assertTrue(plan.ok)

    def test_declared_required_but_missing_fails_loud(self):
        plan = plan_mounts({"audio": True}, {"audio": False})
        self.assertEqual(plan.failed_required, ["audio"])
        self.assertFalse(plan.ok)

    def test_declared_optional_but_missing_skips_gracefully(self):
        plan = plan_mounts({"servo": False}, {"servo": False})
        self.assertIn("servo", plan.skipped)
        self.assertEqual(plan.failed_required, [])
        self.assertTrue(plan.ok)

    def test_undeclared_is_skipped_not_mounted(self):
        # Intern: servo driver present on the image but NOT declared -> never mounts.
        plan = plan_mounts({"audio": True}, {"audio": True, "servo": True})
        self.assertIn("servo", plan.skipped)
        self.assertNotIn("servo", plan.mounted)


class TestInternBootProof(unittest.TestCase):
    """Batch C boot-proof (no hardware): the same router set, gated by each
    device's ROBOT.md, yields different mounts — Intern is Lamp-minus, not a fork."""

    ALL_ROUTERS = {
        "servo", "led", "camera", "audio", "emotion", "scene",
        "sensing", "display", "voice", "music", "system", "bluetooth",
    }

    def _mounted(self, device_type):
        declared = set(load_device(device_type, DEVICES_DIR).declared_routes())
        return self.ALL_ROUTERS & declared

    def test_lamp_mounts_servo_but_not_display(self):
        m = self._mounted("lamp")
        self.assertIn("servo", m)
        # No display declared -> HAL never mounts /display on lamp.
        self.assertNotIn("display", m)

    def test_intern_mounts_neither_servo_nor_display(self):
        m = self._mounted("intern-v2")
        self.assertNotIn("servo", m)
        self.assertNotIn("display", m)

    def test_both_mount_the_shared_audio_stack(self):
        lamp, intern = self._mounted("lamp"), self._mounted("intern-v2")
        for route in ("audio", "voice", "system"):
            self.assertIn(route, lamp)
            self.assertIn(route, intern)

    def test_intern_is_a_strict_subset_of_lamp(self):
        self.assertTrue(self._mounted("intern-v2") < self._mounted("lamp"))


if __name__ == "__main__":
    unittest.main()


class TestProfileFilename(unittest.TestCase):
    """ROBOT.md is canonical; DEVICE.md still loads.

    Robots in the field have DEVICE.md on disk and OTA device profiles carry
    it, so dropping the old name would brick an update, not just rename a file.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.body = (
            "---\n"
            "schema: autonomous.device.v1\n"
            "id: bot\n"
            "name: Bot\n"
            "type: desk_robot\n"
            "boards: [raspberry_pi_5]\n"
            "gateway: { default: openclaw }\n"
            "capabilities:\n"
            "  audio: { routes: [audio], required: true }\n"
            "  system: { routes: [system], required: true }\n"
            "---\n"
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, filename):
        d = os.path.join(self.root, "bot")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "w") as fh:
            fh.write(self.body)
        return d

    def test_canonical_name(self):
        d = self._write("ROBOT.md")
        self.assertEqual(os.path.basename(profile_path(d)), "ROBOT.md")
        self.assertEqual(set(load_device("bot", self.root).capabilities), {"audio", "system"})

    def test_legacy_name_still_loads(self):
        d = self._write("DEVICE.md")
        self.assertEqual(os.path.basename(profile_path(d)), "DEVICE.md")
        self.assertEqual(set(load_device("bot", self.root).capabilities), {"audio", "system"})

    def test_canonical_wins_when_both_exist(self):
        d = self._write("DEVICE.md")
        with open(os.path.join(d, "ROBOT.md"), "w") as fh:
            fh.write(self.body.replace("audio: { routes: [audio], required: true }",
                                       "vision: { routes: [camera], required: true }"))
        self.assertEqual(os.path.basename(profile_path(d)), "ROBOT.md")
        self.assertEqual(set(load_device("bot", self.root).capabilities), {"vision", "system"})

    def test_missing_both_names_reports_the_canonical_one(self):
        d = os.path.join(self.root, "bot")
        os.makedirs(d, exist_ok=True)
        self.assertTrue(profile_path(d).endswith("ROBOT.md"))
