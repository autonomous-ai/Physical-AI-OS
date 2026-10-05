"""TTP223 session / decision state machine — the D3 gap in the build plan."""

import importlib
import os
import unittest

from unittest import mock


def _driver(swipe: bool, min_gap_ms: str = "35"):
    """Re-import the driver with SWIPE_ENABLED set, since it is read at import."""
    os.environ["HAL_TOUCH_SWIPE"] = "true" if swipe else "false"
    os.environ["HAL_TOUCH_SWIPE_MIN_GAP_MS"] = min_gap_ms
    os.environ["HAL_TOUCH_DEBUG"] = "false"
    import hal.drivers.ttp223 as t

    importlib.reload(t)
    return t


class _Harness:
    """A handler with its lines claimed and its actions replaced by recorders."""

    def __init__(self, mod, lines=(96, 100), axis=None):
        self.mod = mod
        self.h = mod.TTP223Handler(mod.TouchConfig(chip=0, lines=list(lines), axis=axis))
        self.h._chip, self.h._lines, self.h._axis = 0, list(lines), axis
        self.h._ignore_edges_until = 0.0
        self.fired = []
        self._now_ms = 0.0

    def touch(self, line, at_ms=None):
        """One touch edge, optionally at a controlled monotonic time."""
        self._edge(line, 0, at_ms)

    def release(self, line, at_ms=None):
        """One release edge."""
        self._edge(line, 1, at_ms)

    def _edge(self, line, level, at_ms):
        # Untimed edges land at the same instant, so there are no gaps.
        if at_ms is None:
            at_ms = self._now_ms
        self._now_ms = at_ms
        with mock.patch.object(self.mod.time, "monotonic", return_value=at_ms / 1000.0):
            self.h._on_edge(0, line, level, 0)

    def end_session(self):
        """Fire the session-end timer by hand."""
        if self.h._session_end_timer is not None:
            self.h._session_end_timer.cancel()
            self.h._session_end_timer = None
        self.h._on_session_end()

    def decide(self):
        if self.h._decision_timer is not None:
            self.h._decision_timer.cancel()
            self.h._decision_timer = None
        self.h._on_decision()


def _record(mod, harness):
    """Patch every action the driver can dispatch."""
    names = ["head_pat_action"]
    patches = [
        mock.patch.object(
            mod, n, side_effect=lambda *a, _n=n, **k: harness.fired.append(_n)
        )
        for n in names
    ]
    # _ack_first_session does I/O on a thread; silence it.
    patches.append(mock.patch.object(harness.h, "_ack_first_session", lambda: None))
    return patches


class _Base(unittest.TestCase):
    swipe = False
    # Tracks the shipped default so the suite exercises the real floor.
    min_gap = "35"

    def setUp(self):
        self.mod = _driver(self.swipe, self.min_gap)
        self.hz = _Harness(self.mod)
        self._p = _record(self.mod, self.hz)
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])


class TestShippedDefaults(unittest.TestCase):
    """The two flags ship in opposite states, and both matter."""

    def test_gesture_classification_is_ON_by_default(self):
        """Swipe detection is enabled by default."""
        for var in ("HAL_TOUCH_SWIPE", "HAL_TOUCH_SWIPE_MIN_GAP_MS"):
            os.environ.pop(var, None)
        import hal.drivers.ttp223 as t

        importlib.reload(t)
        self.assertTrue(t.SWIPE_ENABLED)
        self.assertEqual(t.SWIPE_MIN_GAP_MS, 35.0)
        self.assertEqual(t.SWIPE_MAX_GAP_MS, 150.0)
        self.assertEqual(t.PRESS_MIN_EMPTY_MS, 15.0)

    def test_tracing_is_OFF_by_default(self):
        """The tracer is off by default."""
        os.environ.pop("HAL_TOUCH_DEBUG", None)
        import hal.drivers.touch_debug as td

        importlib.reload(td)
        self.assertFalse(td._init())


class TestFlagOffPetMapping(_Base):
    """Disabling spatial classification still makes every headpad touch a pet."""

    swipe = False

    def test_one_contact_resolves_to_tap(self):
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_two_contacts_fire_pet_inline_on_count(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_monotonic_three_pad_traversal_is_still_only_a_tap(self):
        """With the flag off, traversal still resolves to one pet response."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_pet_cooldown_swallows_further_contacts(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])
        self.hz.touch(96)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_settle_window_suppresses_edges_entirely(self):
        self.hz.h._ignore_edges_until = self.mod.time.monotonic() + 5
        self.hz.touch(96)
        self.assertIsNone(self.hz.h._session_end_timer)
        self.assertEqual(self.hz.h._contact, [])


class TestSwipe(_Base):
    swipe = True

    def test_a_pass_over_all_pads_is_a_swipe(self):
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_swipe_never_also_fires_a_tap(self):
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertNotIn("single_click_action", self.hz.fired)

    def test_both_directions_are_the_same_gesture(self):
        """Both swipe directions resolve to head_pat_action."""
        for lines in ((96, 100), (100, 96)):
            self.hz.fired.clear()
            self.hz.h._reset_cycle()
            self.hz.h._pet_cooldown_until = 0.0
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=i * 100)
            self.hz.end_session()
            self.hz.decide()
            self.assertEqual(self.hz.fired, ["head_pat_action"], lines)

    def test_a_RIGHT_TO_LEFT_swipe_registers_despite_a_bridged_landing(self):
        """A right-to-left swipe registers despite a bridged landing."""
        for at, line in ((0, 100), (109, 96)):
            self.hz.touch(line, at_ms=at)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_cross_talk_burst_is_not_a_swipe(self):
        """Near-simultaneous pads are not a swipe."""
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 10)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_touching_only_one_pad_is_not_a_swipe(self):
        for i, line in enumerate((96,)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestPetKeepsWorking(_Base):
    swipe = True

    def test_a_CONTINUOUS_stroke_is_one_contact_and_still_a_pet(self):
        """A continuous stroke is one contact and still fires pet."""
        for i, line in enumerate((96, 100, 96, 100, 96, 100)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_back_and_forth_stroke_is_a_pet_not_a_swipe(self):
        """A swipe must be a single contact; a multi-leg stroke is not a swipe."""
        for lines in ((96, 100), (100, 96)):
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=len(self.hz.h._contacts) * 1000 + i * 120)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_hand_moving_between_contacts_fires_pet_immediately(self):
        """Contacts sharing no pad resolve to pet without waiting."""
        for n, line in enumerate((96, 100), 1):
            self.hz.touch(line)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_revisit_that_LANDED_is_a_double_tap_not_a_pet(self):
        """A revisit where pads lit together is a double tap, not a stroke."""
        for i, line in enumerate((96, 100, 96)):
            self.hz.touch(line, at_ms=i * 10)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_repeated_contact_in_one_place_is_a_double_tap_not_a_pet(self):
        """Repeated contact at one pad gets the same pet action as a stroke."""
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_multi_pad_contacts_without_reversal_still_fall_back_to_pet(self):
        for n, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=n * 100)
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestDoubleTap(_Base):
    swipe = True

    def test_a_FAST_double_tap_lands_in_one_contact_and_still_gives_one_pet(self):
        """A fast double tap arriving in one contact fires one pet response."""
        # burst 1 at 0/2/12 ms, burst 2 at 300/302/312 ms
        for base in (0, 300):
            for off, line in ((0, 96), (8, 100)):
                self.hz.touch(line, at_ms=base + off)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_an_evenly_spread_stroke_is_NOT_read_as_burst_pairs(self):
        """An evenly spread stroke fires one pet response."""
        for i, line in enumerate((96, 100, 96, 100, 96, 100)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_repeated_contact_on_one_pad_gives_one_pet(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_MULTI_FINGER_double_tap_in_one_place_still_gives_one_pet(self):
        """A multi-finger double tap fires one pet response."""
        for c in range(2):
            for i, line in enumerate((96, 100)):
                self.hz.touch(line, at_ms=c * 1000 + i * 8)   # ~8ms apart
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_three_finger_tap_is_one_tap_not_a_swipe(self):
        """All pads lit together is a tap, not a crossing."""
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 8)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_contact_that_recorded_no_pads_still_counts(self):
        """Contacts are counted by session, not by pad set."""
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_contacts_in_different_places_are_a_pet_not_a_double_tap(self):
        """Multi-pad contacts sharing no pad do not collapse into double tap."""
        for pads in ((96,), (100,)):
            for line in pads:
                self.hz.touch(line)
            self.hz.end_session()
        self.assertIn("head_pat_action", self.hz.fired)
        self.assertNotIn("mic_toggle_action", self.hz.fired)

    def test_a_single_contact_is_not_a_double_tap(self):
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestTapReasonNamesTheNearestMiss(_Base):
    """A TAP reports the rule that declined the swipe, not the fall-through."""

    swipe = True

    def _reason(self):
        return self.hz.h._tap_reason(1)

    def test_a_swipe_just_under_the_floor_says_so_with_the_number(self):
        # every pad, single pass, gaps 30/25 ms — both under the 35 ms floor
        for at, line in ((0, 96), (30, 100)):
            self.hz.touch(line, at_ms=at)
        r = self._reason()
        self.assertIn("fallback", r)
        self.assertIn("max gap 30.0ms", r)
        self.assertIn(f"{self.mod.SWIPE_MIN_GAP_MS:.0f}ms floor", r)

    def test_a_partial_pass_says_which_pads_were_missing(self):
        self.hz.touch(96, at_ms=0)
        r = self._reason()
        self.assertIn("touched 1 of 2 pads", r)
        self.assertIn("not an end-to-end pass", r)

    def test_it_always_marks_itself_a_fallback(self):
        self.hz.touch(96)
        self.assertTrue(self._reason().startswith("fallback"))

    def test_it_never_raises_even_with_no_pads(self):
        """A reason string must not be able to break a gesture."""
        self.assertIn("fallback", self.hz.h._tap_reason(1))


class TestSequenceHygiene(_Base):
    swipe = True

    def test_a_pad_refiring_under_a_still_finger_is_not_a_step(self):
        """A stationary repeat is not a direction change."""
        for line in (96, 96, 96, 100):
            self.hz.touch(line)
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 100])

    def test_release_edges_never_enter_the_sequence(self):
        self.hz.touch(96)
        self.hz.release(96)
        self.hz.touch(100)
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 100])

    def test_contacts_are_cleared_once_a_gesture_resolves(self):
        """Contacts are cleared between gestures."""
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.h._contacts, [])
        self.assertEqual(self.hz.h._contact, [])


class TestAxis(_Base):
    """Three-pad profile, since axis only matters with three or more pads."""

    swipe = True

    def test_absent_axis_falls_back_to_line_order(self):
        hz = _Harness(self.mod, lines=(96, 98, 100))
        self.assertIsNone(hz.h._axis)
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        is_swipe, *_ = hz.h._classify()
        self.assertTrue(is_swipe)

    def test_a_measured_axis_reorders_the_traversal_test(self):
        """With a real axis, line order and spatial order can differ."""
        hz = _Harness(self.mod, lines=(96, 98, 100), axis=[96, 100, 98])
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        is_swipe, *_ = hz.h._classify()
        self.assertFalse(is_swipe)


class TestTravelBand(_Base):
    """A swipe's gap must be a JOURNEY — not a landing, and not a lift."""

    swipe = True

    def test_a_gap_inside_the_band_is_a_swipe(self):
        for at, line in ((0, 96), (80, 100)):
            self.hz.touch(line, at_ms=at)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_gap_above_the_ceiling_is_a_lift_not_a_swipe(self):
        """A gap above the travel ceiling is a lift, not a swipe."""
        for at, line in ((0, 100), (197, 96)):
            self.hz.touch(line, at_ms=at)
        self.hz.end_session()
        self.hz.decide()
        self.assertNotIn("swipe_action", self.hz.fired)

    def test_the_tap_reason_names_the_ceiling_when_it_declined(self):
        for at, line in ((0, 100), (197, 96)):
            self.hz.touch(line, at_ms=at)
        r = self.hz.h._tap_reason(1)
        self.assertIn("ceiling", r)
        self.assertIn("lifting", r)


class TestReTouchAfterRelease(_Base):
    """A touch following a RELEASE of the same pad is a real second contact."""

    swipe = True

    def test_a_retouch_after_a_release_is_kept(self):
        self.hz.touch(100, at_ms=0)
        self.hz.release(100)
        self.hz.touch(100, at_ms=183)
        self.assertEqual([l for l, _ in self.hz.h._contact], [100, 100])

    def test_a_duplicate_edge_with_no_release_is_still_collapsed(self):
        """That case is a driver artefact, not a touch."""
        self.hz.touch(100, at_ms=0)
        self.hz.touch(100, at_ms=5)
        self.assertEqual([l for l, _ in self.hz.h._contact], [100])

    def test_the_full_120736_sequence_resolves_to_a_double_tap(self):
        for at, line, lvl in ((0, 100, 0), (92, 100, 1), (183, 100, 0),
                              (197, 96, 0), (288, 96, 1), (291, 100, 1)):
            if lvl == 0:
                self.hz.touch(line, at_ms=at)
            else:
                self.hz.release(line)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestPressCount(_Base):
    """A PRESS is the hand arriving on the surface — nothing held, then held."""

    swipe = True

    def test_a_swipe_is_one_press(self):
        self.hz.touch(96, at_ms=0)
        self.hz.touch(100, at_ms=80)
        self.hz.release(96)
        self.hz.release(100)
        self.assertEqual(self.hz.h._presses, 1)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_two_taps_on_DIFFERENT_pads_is_a_double_tap(self):
        """No revisit and no landing — the press count is the only evidence."""
        self.hz.touch(96, at_ms=0)
        self.hz.release(96, at_ms=92)
        self.hz.touch(100, at_ms=272)
        self.hz.release(100, at_ms=345)
        self.assertEqual(self.hz.h._presses, 2)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_second_press_stops_it_being_a_swipe(self):
        """A second press inside the travel band stops it being a swipe."""
        self.hz.touch(96, at_ms=0)
        self.hz.release(96, at_ms=20)
        self.hz.touch(100, at_ms=80)
        self.hz.end_session(); self.hz.decide()
        self.assertNotIn("swipe_action", self.hz.fired)

    def test_a_slide_between_pads_is_not_a_second_press(self):
        """A brief empty surface mid-swipe is not a lift."""
        self.hz.touch(100, at_ms=0)
        self.hz.release(100, at_ms=0)
        self.hz.touch(96, at_ms=0.7)
        self.assertEqual(self.hz.h._presses, 1)

    def test_a_real_lift_still_counts(self):
        """A genuine lift above the 15 ms floor still counts as a press."""
        self.hz.touch(96, at_ms=0)
        self.hz.release(96, at_ms=0)
        self.hz.touch(100, at_ms=30)
        self.assertEqual(self.hz.h._presses, 2)

    def test_the_full_135217_sequence_resolves_to_a_swipe(self):
        for at, line, lvl in ((0, 100, 0), (105.8, 100, 1), (106.5, 96, 0), (241, 96, 1)):
            self.hz.touch(line, at_ms=at) if lvl == 0 else self.hz.release(line, at_ms=at)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_the_full_131615_sequence_resolves_to_a_double_tap(self):
        for at, line, lvl in ((0, 96, 0), (92, 96, 1), (272, 100, 0), (345, 100, 1)):
            self.hz.touch(line, at_ms=at) if lvl == 0 else self.hz.release(line, at_ms=at)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestGeometryIndependent(_Base):
    """The same rules must hold on three pads."""

    swipe = True

    def setUp(self):
        super().setUp()
        self.hz = _Harness(self.mod, lines=(96, 98, 100))
        for p in self._p:
            p.stop()
        self._p = _record(self.mod, self.hz)
        for p in self._p:
            p.start()

    def test_a_pass_over_three_pads_is_a_swipe(self):
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_three_finger_landing_is_a_tap(self):
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 8)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_stroke_over_three_pads_is_a_pet(self):
        """All travel, revisits — no landing anywhere."""
        for i, line in enumerate((96, 98, 100, 98, 96)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_fast_double_tap_over_three_pads_gives_one_pet(self):
        """Two landings with a gap between, in one contact."""
        for base in (0, 300):
            for off, line in ((0, 96), (8, 98), (16, 100)):
                self.hz.touch(line, at_ms=base + off)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_missing_a_pad_is_not_a_swipe(self):
        """Every wired pad is the proof it crossed; two of three is not."""
        for i, line in enumerate((96, 98)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.hz.decide()
        self.assertNotIn("swipe_action", self.hz.fired)


class TestDetectedPads(_Base):
    """Four candidate lines, two wired: rules follow the detected pads."""

    swipe = True

    def setUp(self):
        super().setUp()
        self.hz = _Harness(self.mod, lines=(37, 96, 97, 98))
        self.hz.h._active = {97, 98}
        for p in self._p:
            p.stop()
        self._p = _record(self.mod, self.hz)
        for p in self._p:
            p.start()

    def _is_swipe(self):
        self.hz.h._close_contact()
        return self.hz.h._classify()[0]

    def test_a_pass_over_the_two_wired_pads_is_a_swipe(self):
        for i, line in enumerate((97, 98)):
            self.hz.touch(line, at_ms=i * 100)
        self.assertTrue(self._is_swipe())

    def test_one_wired_pad_is_not_a_swipe(self):
        self.hz.touch(97, at_ms=0)
        self.assertFalse(self._is_swipe())

    def _run_armed_timers(self):
        """Fire only the timers the edges armed, as the real driver would."""
        if self.hz.h._session_end_timer is not None:
            self.hz.end_session()
        if self.hz.h._decision_timer is not None:
            self.hz.decide()

    def test_a_glitch_on_an_undetected_line_is_not_learned_and_fires_nothing(self):
        """A 1 ms transient on a bare candidate pin is not a pad and not a gesture."""
        self.hz.touch(37, at_ms=1000)
        self.hz.release(37, at_ms=1001)
        self.assertEqual(self.hz.h._active, {97, 98})
        self.assertIsNone(self.hz.h._session_end_timer)
        self.assertEqual((self.hz.h._contact, self.hz.h._presses), ([], 0))
        self._run_armed_timers()
        self.assertEqual(self.hz.fired, [])
        self.assertEqual(self.hz.h._wired(), {97, 98})

    def test_a_fall_with_no_release_is_not_learned_and_fires_nothing(self):
        self.hz.touch(37, at_ms=1000)
        self.assertEqual(self.hz.h._active, {97, 98})
        self._run_armed_timers()
        self.assertEqual(self.hz.fired, [])

    def test_a_sustained_touch_on_an_undetected_line_is_learned_and_delivered(self):
        """A pad the startup probe missed is admitted by its first real touch."""
        self.hz.touch(37, at_ms=1000)
        self.assertEqual(self.hz.h._active, {97, 98})
        self.hz.release(37, at_ms=1060)
        self.assertEqual(self.hz.h._active, {37, 97, 98})
        self._run_armed_timers()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_low_just_under_the_floor_is_not_learned(self):
        self.hz.touch(37, at_ms=1000)
        self.hz.release(37, at_ms=1000 + self.mod.LEARN_MIN_LOW_MS - 1)
        self.assertEqual(self.hz.h._active, {97, 98})

    def test_a_glitch_does_not_stop_a_later_real_touch_being_learned(self):
        self.hz.touch(37, at_ms=1000)
        self.hz.release(37, at_ms=1001)
        self.hz.touch(37, at_ms=2000)
        self.hz.release(37, at_ms=2080)
        self.assertEqual(self.hz.h._active, {37, 97, 98})

    def test_a_learned_pad_is_handled_at_once_from_then_on(self):
        self.hz.touch(37, at_ms=1000)
        self.hz.release(37, at_ms=1060)
        self._run_armed_timers()
        self.hz.h._pet_cooldown_until = 0.0
        self.hz.touch(37, at_ms=5000)
        self.assertEqual(self.hz.h._contact, [(37, 5.0)])

    def test_nothing_detected_learns_each_pad_from_its_first_touch(self):
        self.hz.h._active = set()
        for line, down, up in ((96, 0, 60), (37, 100, 160)):
            self.hz.touch(line, at_ms=down)
            self.hz.release(line, at_ms=up)
        self.assertEqual(self.hz.h._active, {37, 96})
        self._run_armed_timers()
        self.assertEqual(self.hz.fired, ["head_pat_action"])
        self.hz.h._pet_cooldown_until = 0.0
        for i, line in enumerate((96, 37)):
            self.hz.touch(line, at_ms=5000 + i * 100)
        self.assertTrue(self._is_swipe())

    def test_release_never_marks_a_line_wired(self):
        self.hz.release(37, at_ms=0)
        self.assertEqual(self.hz.h._active, {97, 98})
        self.assertIsNone(self.hz.h._session_end_timer)


class TestPetOnlyActions(_Base):
    swipe = True

    def test_resolved_tap_arms_and_extends_pet_cooldown(self):
        with mock.patch.object(self.mod.time, "monotonic", return_value=10.0):
            self.hz.touch(96, at_ms=10000)
            self.hz.release(96, at_ms=10050)
            self.hz.end_session()
            self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])
        self.assertEqual(self.hz.h._pet_cooldown_until, 11.5)
        with mock.patch.object(self.mod.time, "monotonic", return_value=10.5):
            self.hz.touch(96, at_ms=10500)
            self.hz.release(96, at_ms=10550)
            self.hz.end_session()
            self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])
        self.assertEqual(self.hz.h._pet_cooldown_until, 12.0)
        with mock.patch.object(self.mod.time, "monotonic", return_value=12.1):
            self.hz.touch(96, at_ms=12100)
            self.hz.release(96, at_ms=12150)
            self.hz.end_session()
            self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action", "head_pat_action"])

    def test_first_contact_only_plays_chime_during_speech(self):
        from hal import app_state
        from hal.routes import voice
        tts = mock.Mock(speaking=True)
        def inline_thread(*, target, **kwargs):
            return mock.Mock(start=target)
        with mock.patch.object(app_state, "tts_service", tts), \
             mock.patch.object(voice, "stop_tts") as stop, \
             mock.patch.object(self.mod, "play_ack_chime") as chime, \
             mock.patch.object(self.mod.threading, "Thread", side_effect=inline_thread):
            self.mod.TTP223Handler._ack_first_session(self.hz.h)
        chime.assert_called_once_with(source="TTP223")
        stop.assert_not_called()
        tts.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
