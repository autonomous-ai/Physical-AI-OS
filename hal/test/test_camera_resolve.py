"""Tests for camera device resolution by hardware name (HAL_CAMERA_NAME)."""
import pytest

pytest.importorskip("cv2")

from hal.drivers.camera.video_capture_device import resolve_camera_device_id


def _make_sysfs(tmp_path, nodes):
    """nodes: {"video0": ("OPENAICAM: OPENAICAM", "0"), ...} name + index attr."""
    sysfs = tmp_path / "v4l"
    for node, (name, index) in nodes.items():
        d = sysfs / node
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        if index is not None:
            (d / "index").write_text(index + "\n")
    return str(sysfs)


def test_no_name_passes_index_through(tmp_path):
    assert resolve_camera_device_id(None, 3, str(tmp_path / "x"), str(tmp_path / "y")) == 3


def test_by_id_capture_symlink_preferred(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-SunplusIT_Inc_OPENAICAM-video-index0").write_text("")
    (by_id / "usb-SunplusIT_Inc_OPENAICAM-video-index1").write_text("")
    got = resolve_camera_device_id("OPENAICAM", 0, str(by_id), str(tmp_path / "none"))
    assert got == str(by_id / "usb-SunplusIT_Inc_OPENAICAM-video-index0")


def test_sysfs_fallback_skips_metadata_sibling(tmp_path):
    # UVC metadata node shares the name but cannot capture; the resolver must skip it.
    sysfs = _make_sysfs(
        tmp_path,
        {
            "video0": ("cedrus", "0"),
            "video2": ("OPENAICAM: OPENAICAM", "1"),
            "video3": ("OPENAICAM: OPENAICAM", "0"),
        },
    )
    got = resolve_camera_device_id("openaicam", 0, str(tmp_path / "none"), sysfs)
    assert got == "/dev/video3"


def test_no_match_falls_back_to_index(tmp_path):
    sysfs = _make_sysfs(tmp_path, {"video0": ("cedrus", "0")})
    assert resolve_camera_device_id("OPENAICAM", 5, str(tmp_path / "none"), sysfs) == 5


def test_absolute_path_returned_as_is_when_present(tmp_path):
    link = tmp_path / "device-camera"
    link.write_text("")
    got = resolve_camera_device_id(str(link), 0, str(tmp_path / "none"), str(tmp_path / "none"))
    assert got == str(link)


def test_absolute_path_missing_falls_back_to_index(tmp_path):
    missing = str(tmp_path / "device-camera")
    assert resolve_camera_device_id(missing, 4, str(tmp_path / "none"), str(tmp_path / "none")) == 4
