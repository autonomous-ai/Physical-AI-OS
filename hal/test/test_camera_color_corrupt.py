"""Tests for the camera ISP color-corruption detector."""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from hal.drivers.camera.video_capture_device import LocalVideoCaptureDevice


def _bgr_from_hsv(h: int, s: int, v: int) -> tuple[int, int, int]:
    px = np.array([[[h, s, v]]], dtype=np.uint8)
    b, g, r = cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0, 0]
    return int(b), int(g), int(r)


def _frame(fill_bgr: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((23, 40, 3), dtype=np.uint8)
    frame[:] = fill_bgr
    return frame


def test_corrupt_green_plus_magenta_detected():
    frame = _frame((120, 128, 125))
    frame[0:5, :] = _bgr_from_hsv(60, 220, 180)
    frame[6, 0:1] = _bgr_from_hsv(150, 140, 180)
    frame[7, 0:1] = _bgr_from_hsv(155, 140, 170)
    frame[8, 0:12] = _bgr_from_hsv(150, 140, 180)
    assert LocalVideoCaptureDevice._looks_color_corrupt(frame)


def test_corrupt_red_plus_magenta_detected():
    frame = _frame((120, 128, 125))
    frame[0:7, :] = _bgr_from_hsv(2, 220, 180)
    frame[8:11, :] = _bgr_from_hsv(150, 180, 180)
    assert LocalVideoCaptureDevice._looks_color_corrupt(frame)


def test_corrupt_red_with_sparse_magenta_detected():
    frame = _frame((120, 128, 125))
    frame[0:6, :] = _bgr_from_hsv(2, 220, 180)
    frame[7, 0:15] = _bgr_from_hsv(150, 180, 180)
    assert LocalVideoCaptureDevice._looks_color_corrupt(frame)


def test_corrupt_magenta_deep_magenta_palette_detected():
    frame = _frame((120, 128, 125))
    frame[0:4, :] = _bgr_from_hsv(165, 220, 180)
    frame[5:7, :] = _bgr_from_hsv(145, 180, 180)
    assert LocalVideoCaptureDevice._looks_color_corrupt(frame)


def test_clean_desaturated_scene_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(_frame((120, 128, 125)))


def test_single_hue_green_wall_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(60, 220, 180))
    )


def test_single_hue_magenta_flood_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(150, 220, 180))
    )


def test_single_hue_red_led_spill_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(2, 220, 180))
    )


def test_dark_frame_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(60, 255, 20))
    )
