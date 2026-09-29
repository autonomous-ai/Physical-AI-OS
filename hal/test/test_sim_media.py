"""Host media mode for the laptop simulator (HAL_SIM_MEDIA=host)."""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from hal.drivers.camera.factory import resolve_camera_class
from hal.drivers.camera.host_capture_device import (
    HostCameraUnavailable,
    HostVideoCaptureDevice,
    probe_host_camera,
)


class TestHostCameraDriver(unittest.TestCase):
    def test_factory_resolves_host_driver(self):
        self.assertIs(resolve_camera_class("host", required=True), HostVideoCaptureDevice)

    def test_host_driver_skips_v4l2_index_resolution(self):
        # macOS has no /dev/video nodes.
        self.assertFalse(HostVideoCaptureDevice.requires_v4l2_index)

    def test_probe_raises_when_camera_never_opens(self):
        cap = mock.Mock()
        cap.isOpened.return_value = False
        with mock.patch("hal.drivers.camera.host_capture_device.cv2.VideoCapture", return_value=cap):
            with self.assertRaises(HostCameraUnavailable):
                probe_host_camera(0)
        cap.release.assert_called()

    def test_probe_raises_when_camera_opens_but_delivers_no_frame(self):
        cap = mock.Mock()
        cap.isOpened.return_value = True
        cap.read.return_value = (False, None)
        with mock.patch("hal.drivers.camera.host_capture_device.cv2.VideoCapture", return_value=cap), \
             mock.patch("hal.drivers.camera.host_capture_device.time.sleep"):
            with self.assertRaises(HostCameraUnavailable) as ctx:
                probe_host_camera(0)
        self.assertIn("no frame", str(ctx.exception))

    def test_probe_waits_out_the_capture_session_warmup(self):
        # AVFoundation misses the first reads while the session starts.
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        cap = mock.Mock()
        cap.isOpened.return_value = True
        cap.read.side_effect = [(False, None), (False, None), (True, frame)]
        with mock.patch("hal.drivers.camera.host_capture_device.cv2.VideoCapture", return_value=cap), \
             mock.patch("hal.drivers.camera.host_capture_device.time.sleep"):
            probe_host_camera(0)
        self.assertEqual(cap.read.call_count, 3)

    def test_macos_failure_names_the_permission_the_user_must_grant(self):
        cap = mock.Mock()
        cap.isOpened.return_value = False
        with mock.patch("hal.drivers.camera.host_capture_device.platform.system", return_value="Darwin"), \
             mock.patch("hal.drivers.camera.host_capture_device.cv2.VideoCapture", return_value=cap):
            with self.assertRaises(HostCameraUnavailable) as ctx:
                probe_host_camera(0)
        self.assertIn("Privacy & Security > Camera", str(ctx.exception))


class TestSimMediaFallback(unittest.TestCase):
    def setUp(self):
        from hal import app_state

        self.state = app_state
        self._saved = (
            app_state.sim_media_camera,
            app_state.sim_media_audio,
            app_state.simulation_audio,
            dict(app_state.sim_media_reasons),
        )
        app_state.sim_media_camera = "host"
        app_state.sim_media_audio = "host"
        app_state.simulation_audio = False
        app_state.sim_media_reasons.clear()

    def tearDown(self):
        (
            self.state.sim_media_camera,
            self.state.sim_media_audio,
            self.state.simulation_audio,
            reasons,
        ) = self._saved
        self.state.sim_media_reasons.clear()
        self.state.sim_media_reasons.update(reasons)

    def test_camera_fallback_leaves_audio_on_host(self):
        self.state.sim_media_fallback("camera", "camera did not open")
        self.assertEqual(self.state.sim_media_camera, "virtual")
        self.assertEqual(self.state.sim_media_audio, "host")
        self.assertFalse(self.state.simulation_audio)
        self.assertEqual(self.state.sim_media_reasons["camera"], "camera did not open")

    def test_audio_fallback_switches_routes_to_the_virtual_devices(self):
        self.state.sim_media_fallback("audio", "microphone unusable")
        self.assertEqual(self.state.sim_media_audio, "virtual")
        self.assertTrue(self.state.simulation_audio)

    def test_first_reason_wins(self):
        self.state.sim_media_fallback("camera", "original cause")
        self.state.sim_media_fallback("camera", "retry noise")
        self.assertEqual(self.state.sim_media_reasons["camera"], "original cause")

    def test_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.state.sim_media_fallback("servo", "nope")


if __name__ == "__main__":
    unittest.main()
