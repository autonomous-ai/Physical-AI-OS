"""Focused tests for LOOK-DEBUG tracing."""

import json
import os
import tempfile
from unittest import mock

from hal.drivers.tracking import look_debug as ld


def _fresh(tmp, enabled="true"):
    """Reset the module's one-time config resolution."""
    ld._enabled = None
    ld._base = None
    ld._current = None
    return mock.patch.dict(os.environ, {"HAL_LOOK_DEBUG": enabled, "HAL_LOOK_DEBUG_DIR": tmp})


class _Aim:
    aimed, reason, iterations = True, "centred on person", 2
    yaw_moved_deg, final_dx_frac, bearing_steps = 7.5, 0.01, 0


def test_a_full_look_is_written_with_question_and_answer():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.note_aim(_Aim())
        ld.note_capture(None)
        ld.finish("OK_realtime_handled", question="what is this?", answer="a mug")
        dirs = os.listdir(tmp)
        assert len(dirs) == 1 and dirs[0].endswith("OK_realtime_handled")
        with open(os.path.join(tmp, dirs[0], "result.json"), encoding="utf-8") as f:
            r = json.load(f)
    assert r["question"] == "what is this?"
    assert r["answer"] == "a mug"
    assert r["aim"]["reason"] == "centred on person"
    assert r["aim"]["iterations"] == 2


def test_failures_are_visible_from_the_directory_name():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.abandon("no_camera_frame")
        assert any("FAIL-no_camera_frame" in d for d in os.listdir(tmp))


def test_the_captured_frame_is_copied_into_the_trace():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        src = os.path.join(tmp, "src.jpg")
        with open(src, "wb") as f:
            f.write(b"jpegbytes")
        ld.start()
        ld.note_capture(src)
        ld.finish("OK_realtime_handled")
        d = [x for x in os.listdir(tmp) if x.endswith("OK_realtime_handled")][0]
        assert os.path.exists(os.path.join(tmp, d, "capture.jpg"))


def test_disabled_by_default_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp, enabled="false"):
        ld.start()
        ld.note_aim(_Aim())
        ld.finish("OK_realtime_handled", answer="x")
        assert os.listdir(tmp) == []


def test_finishing_without_a_look_is_a_no_op():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.finish("OK_realtime_handled", answer="no look happened")
        assert os.listdir(tmp) == []


def test_tracing_never_raises_on_bad_input():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.note_aim(object())
        ld.note_capture("/nope.jpg")
        ld.finish("OK_realtime_handled")


def test_old_traces_are_pruned():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        with mock.patch.dict(os.environ, {"HAL_LOOK_DEBUG_MAX_ENTRIES": "3"}):
            for i in range(7):
                ld.start()
                ld.finish(f"OK_{i}")
        assert len(os.listdir(tmp)) <= 3


def test_the_turn_replay_extends_the_trace_instead_of_replacing_it():
    # One visual question calls look() twice by design: capture, then replay.
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.note_aim(_Aim())
        src = os.path.join(tmp, "f.jpg")
        with open(src, "wb") as f:
            f.write(b"jpegbytes")
        ld.note_capture(src)

        ld.start()
        ld.note_event("reused recent frame (already looked this turn)")

        ld.finish("OK_realtime_handled", question="what is this?", answer="a mug")

        dirs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        assert len(dirs) == 1, f"one look must write one trace, got {dirs}"
        assert dirs[0].endswith("OK_realtime_handled")
        d = os.path.join(tmp, dirs[0])
        assert os.path.exists(os.path.join(d, "capture.jpg")), "capture was lost"
        with open(os.path.join(d, "result.json"), encoding="utf-8") as f:
            r = json.load(f)
    assert r["look_calls"] == 2
    assert r["aim"]["reason"] == "centred on person"
    assert r["answer"] == "a mug"
    assert any("replay" in e["msg"] for e in r["events"])


def test_an_orphaned_trace_is_eventually_replaced():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld._current["_t0"] -= ld.STALE_TRACE_S + 1
        ld.start()
        assert ld._current["look_calls"] == 1, "stale trace should have been replaced"


def test_profile_does_not_double_charge_nested_stages():
    """`aim.detect` is not double-counted inside `aim.total`."""
    trace = {
        "stages": {
            "aim.total": {"ms": 2000.0, "n": 1},
            "aim.detect": {"ms": 1500.0, "n": 3},
            "aim.move": {"ms": 400.0, "n": 3},
            "capture": {"ms": 300.0, "n": 1},
        },
        "total_ms": 25000,
    }
    profile = ld._take_profile(trace)
    assert profile["device_ms"] == 2300.0
    assert profile["waiting_on_model_ms"] == 22700.0
    assert profile["stages"]["aim.detect"]["nested_in"] == "aim.total"
    assert profile["stages"]["aim.detect"]["avg_ms"] == 500.0
    assert "stages" not in trace


def test_profile_written_as_its_own_file(tmp_path):
    """profile.json sits beside result.json, per the speaker_logs convention."""
    with mock.patch.object(ld, "_enabled", True), mock.patch.object(ld, "_base", tmp_path):
        ld.start()
        with ld.stage("capture"):
            pass
        ld.finish("OK_test")
    dirs = list(tmp_path.iterdir())
    assert len(dirs) == 1
    written = {p.name for p in dirs[0].iterdir()}
    assert "profile.json" in written and "result.json" in written
    profile = json.loads((dirs[0] / "profile.json").read_text())
    assert "capture" in profile["stages"]
    assert profile["waiting_on_model_ms"] >= 0


def test_step_frames_written_and_annotated(tmp_path):
    """Per-step frames are saved to expose wrong-subject locks."""
    import numpy as np

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with mock.patch.object(ld, "_enabled", True), mock.patch.object(ld, "_base", tmp_path):
        ld.start()
        ld.note_step_frame(1, frame, (100, 200, 40, 200), "iter 1: person dx=+20.0%")
        ld.note_step_frame(2, frame, None, "iter 2: no detection")
        ld.finish("OK_test")
    written = sorted(p.name for p in next(tmp_path.iterdir()).iterdir())
    assert any(n.startswith("step_01") for n in written), written
    assert any(n.startswith("step_02") for n in written), written
    result = json.loads((next(tmp_path.iterdir()) / "result.json").read_text())
    assert "_step_frames" not in result


def test_step_frames_can_be_disabled(tmp_path, monkeypatch):
    """Frames cost disk on a long soak; the knob must actually gate them."""
    import numpy as np

    monkeypatch.setenv("HAL_LOOK_DEBUG_FRAMES", "false")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with mock.patch.object(ld, "_enabled", True), mock.patch.object(ld, "_base", tmp_path):
        ld.start()
        ld.note_step_frame(1, frame, (100, 200, 40, 200), "iter 1")
        ld.finish("OK_test")
    written = [p.name for p in next(tmp_path.iterdir()).iterdir()]
    assert not any(n.startswith("step_") for n in written), written


def test_the_aim_view_stays_vertical_only():
    """The aim view draws only the vertical line by default."""
    import cv2
    import numpy as np

    frame = np.zeros((80, 120, 3), dtype=np.uint8)
    img = cv2.imdecode(
        np.frombuffer(ld.encode_annotated(frame, (20, 10, 40, 30), "x"), np.uint8),
        cv2.IMREAD_COLOR,
    )
    mid_row, mid_col = img.shape[0] // 2, img.shape[1] // 2
    assert int(img[5, mid_col][2]) > 80, "vertical frame-centre line missing"
    assert int(img[mid_row, 5][2]) < 60, "horizontal line must be opt-in"


def test_the_gaze_view_adds_the_axis_it_servos_on():
    """The pitch loop corrects dy, so its view needs the horizontal pair."""
    import cv2
    import numpy as np

    frame = np.zeros((80, 120, 3), dtype=np.uint8)
    img = cv2.imdecode(
        np.frombuffer(
            ld.encode_annotated(frame, (20, 10, 40, 30), "x", both_axes=True),
            np.uint8,
        ),
        cv2.IMREAD_COLOR,
    )
    mid_row, mid_col = img.shape[0] // 2, img.shape[1] // 2
    assert int(img[5, mid_col][2]) > 80, "vertical frame-centre line missing"
    assert int(img[mid_row, 5][2]) > 80, "horizontal frame-centre line missing"
