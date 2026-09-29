"""Focused tests for surfacing the look frame in the Flow Monitor."""

import os
import re
import tempfile
from unittest import mock

import hal.config as config
from hal.realtime import look_monitor as lm

# The regex the Flow Monitor uses to turn a marker into a thumbnail URL.
UI_MARKER_RE = re.compile(
    r"\[snapshot:\s*(?:/tmp/(?:lamp|hal)-(?:sensing|emotion|motion)-snapshots"
    r"|/var/lib/hal/snapshots)/((?:sensing|emotion|motion)_[^\]]+\.jpg)\]"
)


def _src(tmp):
    p = os.path.join(tmp, "look_1.jpg")
    with open(p, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0jpegbytes")
    return p


def test_marker_matches_what_the_monitor_actually_parses():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "SNAPSHOT_PERSIST_DIR", tmp, create=True):
            dst = lm.persist_for_monitor(_src(tmp))
            marker = lm.snapshot_marker(dst)
    assert dst is not None
    m = UI_MARKER_RE.search(marker.replace(tmp, "/var/lib/hal/snapshots"))
    assert m, f"monitor would not render this marker: {marker}"
    assert m.group(1).startswith("sensing_look/")


def test_category_is_sensing_prefixed():
    assert lm.MONITOR_CATEGORY.startswith("sensing_")


def test_frame_lands_where_the_snapshot_route_serves_from():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "SNAPSHOT_PERSIST_DIR", tmp, create=True):
            dst = lm.persist_for_monitor(_src(tmp))
    assert os.path.dirname(dst).endswith(lm.MONITOR_CATEGORY)
    assert dst.endswith(".jpg")


def test_missing_source_is_not_fatal():
    assert lm.persist_for_monitor(None) is None
    assert lm.persist_for_monitor("/nope/missing.jpg") is None


def test_no_frame_yields_no_marker():
    assert lm.snapshot_marker(None) == ""


def test_old_frames_are_pruned():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "SNAPSHOT_PERSIST_DIR", tmp, create=True):
            src = _src(tmp)
            for _ in range(lm.KEEP_LAST + 8):
                lm.persist_for_monitor(src)
            kept = os.listdir(os.path.join(tmp, lm.MONITOR_CATEGORY))
    assert len(kept) <= lm.KEEP_LAST, f"pruning failed, {len(kept)} files kept"
