"""Recorded moves resolve across several HF libraries (#287)."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _install_sdk_stub():
    if "reachy_mini" in sys.modules:
        return
    pkg = types.ModuleType("reachy_mini")
    pkg.ReachyMini = object
    utils = types.ModuleType("reachy_mini.utils")
    utils.create_head_pose = lambda **kwargs: None
    motion = types.ModuleType("reachy_mini.motion")
    recorded = types.ModuleType("reachy_mini.motion.recorded_move")
    recorded.RecordedMoves = object
    sys.modules.update({
        "reachy_mini": pkg,
        "reachy_mini.utils": utils,
        "reachy_mini.motion": motion,
        "reachy_mini.motion.recorded_move": recorded,
    })


_install_sdk_stub()

from hal.drivers.motors import reachy_service as rs  # noqa: E402


class FakeLib:
    """Stands in for RecordedMoves: get() raises on a miss, like the SDK's."""

    def __init__(self, dataset, moves):
        self.dataset = dataset
        self._moves = moves

    def get(self, name):
        if name not in self._moves:
            raise ValueError(f"Move {name} not found in {self.dataset}")
        return self._moves[name]

    def list_moves(self):
        return list(self._moves)


def _svc(libs):
    svc = rs.ReachyMotionService.__new__(rs.ReachyMotionService)
    svc._moves = libs
    return svc


class TestDatasetList(unittest.TestCase):
    def test_defaults_are_the_two_pollen_libraries(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rs._move_datasets(),
                             [rs._EMOTES_DATASET, rs._DANCES_DATASET])

    def test_env_datasets_come_first(self):
        with mock.patch.dict(os.environ, {"HAL_REACHY_MOVES": "me/mine, me/other"}):
            self.assertEqual(rs._move_datasets(),
                             ["me/mine", "me/other", rs._EMOTES_DATASET, rs._DANCES_DATASET])

    def test_duplicates_collapse_keeping_first_position(self):
        with mock.patch.dict(os.environ, {"HAL_REACHY_MOVES": rs._DANCES_DATASET}):
            self.assertEqual(rs._move_datasets(),
                             [rs._DANCES_DATASET, rs._EMOTES_DATASET])

    def test_blank_entries_ignored(self):
        with mock.patch.dict(os.environ, {"HAL_REACHY_MOVES": " , ,"}):
            self.assertEqual(rs._move_datasets(),
                             [rs._EMOTES_DATASET, rs._DANCES_DATASET])


class TestFindMove(unittest.TestCase):
    def test_first_library_holding_the_name_wins(self):
        svc = _svc([FakeLib("a", {"x": "from-a"}), FakeLib("b", {"x": "from-b"})])
        self.assertEqual(svc._find_move("x"), "from-a")

    def test_falls_through_to_a_later_library(self):
        svc = _svc([FakeLib("a", {}), FakeLib("b", {"y": "from-b"})])
        self.assertEqual(svc._find_move("y"), "from-b")

    def test_missing_everywhere_is_none(self):
        svc = _svc([FakeLib("a", {}), FakeLib("b", {})])
        self.assertIsNone(svc._find_move("nope"))

    def test_no_library_loaded(self):
        self.assertIsNone(_svc(False)._find_move("x"))


class TestAvailableRecordings(unittest.TestCase):
    def test_lists_the_union_of_every_library(self):
        svc = _svc([FakeLib("a", {"alpha": 1}), FakeLib("b", {"beta": 1})])
        self.assertEqual(svc.get_available_recordings(), ["alpha", "beta"])

    def test_one_broken_library_does_not_hide_the_others(self):
        class Broken(FakeLib):
            def list_moves(self):
                raise RuntimeError("dataset pulled from the Hub")

        svc = _svc([Broken("bad", {}), FakeLib("b", {"beta": 1})])
        self.assertEqual(svc.get_available_recordings(), ["beta"])


if __name__ == "__main__":
    unittest.main()
