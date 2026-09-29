"""TOUCH-DEBUG tracer: inert when off, correct arithmetic when on."""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


def _fresh(enabled: bool, out_dir: str = "", pads: str = ""):
    """Re-import the module with a given env (`_enabled` is cached)."""
    os.environ["HAL_TOUCH_DEBUG"] = "true" if enabled else "false"
    if out_dir:
        os.environ["HAL_TOUCH_DEBUG_DIR"] = out_dir
    else:
        os.environ.pop("HAL_TOUCH_DEBUG_DIR", None)
    if pads:
        os.environ["HAL_TOUCH_DEBUG_PADS"] = pads
    else:
        os.environ.pop("HAL_TOUCH_DEBUG_PADS", None)
    import hal.drivers.touch_debug as td

    importlib.reload(td)
    return td


class TestDisabled(unittest.TestCase):
    def test_every_entry_point_is_a_noop_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(False, tmp)
            td.start_cycle(0, [96, 98, 100])
            td.note_edge(96, 0)
            td.note_session_end(1)
            td.note_decision("TAP", "reason", 1)
            td.note_action("single_click_action", "TTP223")
            td.finish("TAP")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_disabled_never_opens_a_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(False, tmp)
            td.start_cycle(0, [96])
            self.assertIsNone(td._current)


class TestSessionArithmetic(unittest.TestCase):
    """Inter-pad deltas are recorded."""

    def _trace(self, tmp):
        return json.loads(next(Path(tmp).glob("*.json")).read_text())

    def test_cross_talk_burst_records_deltas_and_primary_pad(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp, pads="96=S1,98=S2,100=S4")
            td.start_cycle(0, [96, 98, 100])
            td.note_edge(96, 0)
            td.note_edge(98, 0)
            td.note_edge(100, 0)
            td.note_edge(96, 1)
            td.note_session_end(1)
            td.note_decision("TAP", "decision window expired at count=1", 1)
            td.finish("TAP")
            import time as _t
            _t.sleep(0.2)  # writer is a daemon thread

            tr = self._trace(tmp)
            s = tr["sessions"][0]
            self.assertEqual(s["distinct_pads"], ["S1", "S2", "S4"])
            self.assertEqual(s["primary_pad"], "S1")
            # Release edge is FastMode's auto-drop, not a second contact.
            self.assertEqual(s["steps"], 3)
            self.assertEqual(len(s["adjacent_deltas_ms"]), 2)

    def test_suppressed_edges_are_recorded_but_do_not_form_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp)
            td.start_cycle(0, [96, 98])
            td.note_edge(96, 0, suppressed=True)
            td.note_edge(98, 0, suppressed=True)
            td.note_session_end(0)
            td.finish("IGNORED-settle")
            import time as _t
            _t.sleep(0.2)

            tr = self._trace(tmp)
            self.assertEqual(len(tr["edges"]), 2)
            self.assertTrue(all(e["suppressed"] for e in tr["edges"]))
            self.assertEqual(tr["sessions"][0]["distinct_pads"], [])


class TestTouchOrderIsUnreduced(unittest.TestCase):
    """The deltas must be measured over every step, not one per pad."""

    def _trace(self, tmp):
        return json.loads(next(Path(tmp).glob("*.json")).read_text())

    def test_a_revisited_pad_still_contributes_its_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp, pads="96=S1,98=S2,100=S4")
            td.start_cycle(0, [96, 98, 100])
            # A stroke: S1 S2 S4 S2 S1 — 5 steps, 3 distinct pads, 4 gaps.
            for line in (96, 98, 100, 98, 96):
                td.note_edge(line, 0)
            td.note_session_end(1)
            td.finish("PET")
            import time as _t
            _t.sleep(0.2)

            s = self._trace(tmp)["sessions"][0]
            self.assertEqual([p for p, _ in s["touch_order"]],
                             ["S1", "S2", "S4", "S2", "S1"])
            self.assertEqual(s["steps"], 5)
            self.assertEqual(s["distinct_pads"], ["S1", "S2", "S4"])
            self.assertEqual(len(s["adjacent_deltas_ms"]), 4)

    def test_a_pad_refiring_under_a_still_finger_is_not_a_step(self):
        """Consecutive repeats collapse into one."""
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp, pads="96=S1,98=S2")
            td.start_cycle(0, [96, 98])
            for line in (96, 96, 96, 98):
                td.note_edge(line, 0)
            td.note_session_end(1)
            td.finish("TAP")
            import time as _t
            _t.sleep(0.2)

            s = self._trace(tmp)["sessions"][0]
            self.assertEqual([p for p, _ in s["touch_order"]], ["S1", "S2"])
            self.assertEqual(s["steps"], 2)


class TestClassifierBlock(unittest.TestCase):
    """The driver classifies; the tracer only records what it was told."""

    def _trace(self, tmp):
        return json.loads(next(Path(tmp).glob("*.json")).read_text())

    def test_classifier_fields_round_trip_into_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp, pads="96=S1,98=S2,100=S4")
            td.start_cycle(0, [96, 98, 100])
            td.note_edge(96, 0)
            td.note_session_end(1)
            td.note_classifier(
                is_swipe=False, moved=True, min_gap_ms=108.1, contacts=2,
                move_floor_ms=40.0, contact_pads=[["S2", "S4", "S1"]],
            )
            td.note_decision("PET", "2 contacts, the hand moved", 2)
            td.finish("PET")
            import time as _t
            _t.sleep(0.2)

            cl = self._trace(tmp)["classifier"]
            self.assertFalse(cl["is_swipe"])
            self.assertTrue(cl["moved"])
            self.assertEqual(cl["min_gap_ms"], 108.1)
            self.assertEqual(cl["move_floor_ms"], 40.0)
            self.assertEqual(cl["contact_pads"], [["S2", "S4", "S1"]])

    def test_a_trace_without_a_classifier_block_still_writes(self):
        """The settle burst resolves before any classification happens."""
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp)
            td.start_cycle(0, [96])
            td.note_edge(96, 1, suppressed=True)
            td.finish("IGNORED-settle")
            import time as _t
            _t.sleep(0.2)
            self.assertNotIn("classifier", self._trace(tmp))

    def test_the_tracer_no_longer_computes_traversal_itself(self):
        """The tracer records the driver's traversal instead of computing its own."""
        td = _fresh(True, tempfile.gettempdir())
        self.assertFalse(hasattr(td, "_traversal"))


class TestPadLabels(unittest.TestCase):
    """`_init` is called first because `_pad` reads the map it resolves."""

    def test_defaults_to_line_number_when_unmapped(self):
        td = _fresh(True, tempfile.gettempdir())
        td._init()
        self.assertEqual(td._pad(97), "L97")

    def test_malformed_env_entries_are_skipped_not_raised(self):
        td = _fresh(True, tempfile.gettempdir(), pads="96=S1,garbage,=X,98=S2")
        td._init()
        self.assertEqual(td._pad(96), "S1")
        self.assertEqual(td._pad(98), "S2")
        self.assertEqual(td._pad(100), "L100")


if __name__ == "__main__":
    unittest.main()
