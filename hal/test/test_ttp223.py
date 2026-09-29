"""TTP223 session / decision state machine — the D3 gap in the build plan.

Until now the touch path had no test at all: `test_board.py` pins the wiring and
nothing covered the state machine that turns edges into gestures. Phase 3 adds
traversal classification and a second wake entrance to that machine, so the
coverage stops being optional.

Everything here drives `_on_edge(chip, gpio, level, tick)` directly with
synthetic edge sequences — the same signature lgpio calls — so no hardware, no
lgpio, and no timers wait in real time: the session and decision timers are
fired by hand.

Pads rest HIGH (pull-up), so level 0 is a touch and level 1 a release.

The lamp runs TWO pads (96 and 100) since 2026-08-28, so that is the default
geometry here. TestGeometryIndependent re-runs the core gestures on three pads:
the classifier must not care how many are wired, and pad 98 silently vanishing
from a 2-pad profile is exactly how a 3-pad test can pass while proving nothing.
"""

import importlib
import os
import unittest

from unittest import mock


def _driver(swipe: bool, min_gap_ms: str = "35"):
    """Re-import the driver with the flag in a given state — SWIPE_ENABLED is
    resolved at import, which is what makes `flag off == today's behaviour`
    testable at all."""
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
        self.h._ignore_edges_until = 0.0  # settle window already elapsed
        self.fired = []
        self._now_ms = 0.0

    def touch(self, line, at_ms=None):
        """One touch edge, optionally at a controlled monotonic time."""
        self._edge(line, 0, at_ms)

    def release(self, line, at_ms=None):
        """One release edge. Release times matter too: how long the surface
        stays empty is what separates a lift from a slide between pads."""
        self._edge(line, 1, at_ms)

    def _edge(self, line, level, at_ms):
        # An untimed edge lands at the same instant as the previous one, so a
        # sequence that never mentions time has no gaps anywhere.
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
    # Tracks the shipped default so the suite exercises the real floor; override
    # per-class only when a test is specifically about a different threshold.
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
        """Enabled 2026-08-27 after hands-on validation. A double tap toggles
        the microphone and a swipe sleeps the device, so this default is a
        user-visible commitment, not an implementation detail."""
        for var in ("HAL_TOUCH_SWIPE", "HAL_TOUCH_SWIPE_MIN_GAP_MS"):
            os.environ.pop(var, None)
        import hal.drivers.ttp223 as t

        importlib.reload(t)
        self.assertTrue(t.SWIPE_ENABLED)
        self.assertEqual(t.SWIPE_MIN_GAP_MS, 35.0)
        self.assertEqual(t.SWIPE_MAX_GAP_MS, 150.0)
        self.assertEqual(t.PRESS_MIN_EMPTY_MS, 15.0)

    def test_tracing_is_OFF_by_default(self):
        """The tracer sits in the lgpio callback path and writes files. It must
        cost nothing on a device nobody is debugging."""
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
        self.assertEqual(self.hz.fired, ["head_pat_action"])  # nothing new

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
        """Left-to-right and right-to-left both resolve to head_pat_action, which
        gives the same pet response in either direction."""
        for lines in ((96, 100), (100, 96)):
            self.hz.fired.clear()
            self.hz.h._reset_cycle()
            self.hz.h._pet_cooldown_until = 0.0  # Independent gesture scenario.
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=i * 100)
            self.hz.end_session()
            self.hz.decide()
            self.assertEqual(self.hz.fired, ["head_pat_action"], lines)

    def test_a_RIGHT_TO_LEFT_swipe_registers_despite_a_bridged_landing(self):
        """The reported bug, 2026-08-27: L->R swipes worked, R->L never did.

        The surface is not symmetric. Starting from the right lands on L100 and
        L98 together — adjacent pads, cross-talk bridges them in 5-28ms — and
        only then travels to L96 over 65-104ms. Requiring EVERY gap to clear the
        floor made that impossible. Real timings from trace 164804.
        """
        for at, line in ((0, 100), (109, 96)):
            self.hz.touch(line, at_ms=at)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_cross_talk_burst_is_not_a_swipe(self):
        """Device-measured shape: three pads inside ~20ms from one finger.
        Ordering alone is not evidence — the gaps have to clear the floor."""
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
        """The reported bug, 2026-08-27: six pets in a row came back TAP.

        A finger that never leaves the surface never lets the 200ms session
        lapse, so a whole ~1s stroke arrives as a SINGLE contact and the
        contact-count gate discarded it. What identifies it is the revisit —
        the finger goes back over a pad it had left. Real sequence from trace
        162337.
        """
        for i, line in enumerate((96, 100, 96, 100, 96, 100)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_back_and_forth_stroke_is_a_pet_not_a_swipe(self):
        """Device trace 160546: the user stroked left-right-left and got
        DOUBLE_TAP. Each leg of a stroke is itself a clean one-direction pass,
        so a swipe has to be the whole gesture — one contact — or every pet
        resolves as a swipe."""
        for lines in ((96, 100), (100, 96)):
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=len(self.hz.h._contacts) * 1000 + i * 120)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_hand_moving_between_contacts_fires_pet_immediately(self):
        """A stroke is contacts in DIFFERENT places. Once they share no pad the
        gesture cannot be a double tap, so pet keeps its fast path rather than
        waiting out the decision window."""
        for n, line in enumerate((96, 100), 1):
            self.hz.touch(line)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_revisit_that_LANDED_is_a_double_tap_not_a_pet(self):
        """The rule that replaced burst clustering. A revisit alone is not a
        stroke: if two pads ever lit together the hand landed, so it tapped
        twice. Device-measured 2026-08-28 — three double taps on the two-pad
        lamp were read as PET because the old rule demanded every burst be
        multi-pad, and one tap lit only one pad."""
        for i, line in enumerate((96, 100, 96)):
            self.hz.touch(line, at_ms=i * 10)     # 10ms steps -> landings
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
        """The reported bug, 2026-08-27: fast multi-finger double taps came back
        PET, slow ones worked.

        Tapping quickly never lets the 200ms session lapse, so both taps arrive
        in a SINGLE contact — and the second tap re-touches the same pads, which
        the revisit rule reads as a stroke. What separates them is shape: a
        double tap is two TIGHT bursts with a gap between (fingers landing
        together), a stroke is evenly spread. Real sequence from trace 163036.
        """
        # burst 1 at 0/2/12 ms, burst 2 at 300/302/312 ms
        for base in (0, 300):
            for off, line in ((0, 96), (8, 100)):
                self.hz.touch(line, at_ms=base + off)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_an_evenly_spread_stroke_is_NOT_read_as_burst_pairs(self):
        """The other side of the same rule. A stroke's steps are evenly spaced,
        so it has no multi-pad clusters and can never look like repeated taps —
        this is what keeps the fast-double-tap fix from eating pets."""
        for i, line in enumerate((96, 100, 96, 100, 96, 100)):
            self.hz.touch(line, at_ms=i * 120)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_repeated_contact_on_one_pad_gives_one_pet(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.hz.decide()
        # Same pad twice: the case ttp223.py:70-76 documented as a known false
        # positive now resolves to its own gesture.
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_MULTI_FINGER_double_tap_in_one_place_still_gives_one_pet(self):
        """Double tap must not need a single fingertip. Three fingers light up
        three pads, but they land TOGETHER — device-measured spread 1-23ms,
        against 53-322ms for a finger that travels. Timing is what separates
        them; pad count cannot."""
        for c in range(2):
            for i, line in enumerate((96, 100)):
                self.hz.touch(line, at_ms=c * 1000 + i * 8)   # ~8ms apart
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_three_finger_tap_is_one_tap_not_a_swipe(self):
        """Three fingers light every pad at once. Without the timing test that
        looks identical to a hand crossing the surface."""
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 8)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_contact_that_recorded_no_pads_still_counts(self):
        """Device trace 154507: the second contact's touch edge fell the other
        side of a session boundary, so it recorded only a release and no pads.
        Counting contacts by pad-set would drop it to a single tap; the session
        count is what the gate reads."""
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.end_session()          # a contact that recorded nothing
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_contacts_in_different_places_are_a_pet_not_a_double_tap(self):
        """The other side of the same rule — sharing no pad means the hand
        moved, so multi-pad contacts must not all collapse into double tap."""
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
    """TAP is the fall-through, so "decision window expired" is true and
    useless — it describes the branch that caught the gesture, not the rule that
    declined it. Device traces 102922/102935 (2026-08-28) were swipes missing
    the movement floor by 1.1 ms and read identically to a deliberate tap.
    """

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
        """FastMode re-triggers on a stationary finger; counting the repeat
        would invent a direction change."""
        for line in (96, 96, 96, 100):
            self.hz.touch(line)
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 100])

    def test_release_edges_never_enter_the_sequence(self):
        self.hz.touch(96)
        self.hz.release(96)
        self.hz.touch(100)
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 100])

    def test_contacts_are_cleared_once_a_gesture_resolves(self):
        """Stale contacts would leak the previous gesture into the next one
        and misclassify it."""
        for i, line in enumerate((96, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.h._contacts, [])
        self.assertEqual(self.hz.h._contact, [])


class TestAxis(_Base):
    """Axis only bites when three or more pads are wired — with two there is
    exactly one ordering, so these declare a three-pad profile explicitly."""

    swipe = True

    def test_absent_axis_falls_back_to_line_order(self):
        hz = _Harness(self.mod, lines=(96, 98, 100))
        self.assertIsNone(hz.h._axis)
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        is_swipe, *_ = hz.h._classify()
        self.assertTrue(is_swipe)

    def test_a_measured_axis_reorders_the_traversal_test(self):
        """With a real axis where line order != spatial order, the same edge
        sequence reads as a turn rather than a straight pass."""
        hz = _Harness(self.mod, lines=(96, 98, 100), axis=[96, 100, 98])
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        is_swipe, *_ = hz.h._classify()
        self.assertFalse(is_swipe)


class TestTravelBand(_Base):
    """A swipe's gap must be a JOURNEY — not a landing, and not a lift.

    Two deliberate taps landing on different pads produce exactly the shape of
    a swipe: each pad once, no revisit, a gap above the floor. Only the size of
    the gap tells them apart. Measured 2026-08-28 on labelled gestures:

        landing   6.4  7.6  27.2 ms
        travel   69.7  80.6 ms
        lift    196.7  273.7  291.6  291.8 ms
    """

    swipe = True

    def test_a_gap_inside_the_band_is_a_swipe(self):
        for at, line in ((0, 96), (80, 100)):
            self.hz.touch(line, at_ms=at)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_gap_above_the_ceiling_is_a_lift_not_a_swipe(self):
        """Device trace 120736: two taps on separate pads, 196.7 ms apart, read
        as a swipe because the rule had a floor but no ceiling."""
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
    """A touch following a RELEASE of the same pad is a real second contact.

    A line cannot emit two falling edges without a rising edge between, so the
    repeat-collapse that swallows FastMode chatter was also swallowing genuine
    re-touches. Device trace 120736: tap L100, release, tap L100 again, and the
    second one vanished — leaving one long journey instead of a double tap.
    """

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
    """A PRESS is the hand arriving on the surface — nothing held, then held.

    During a swipe the finger reaches the far pad before the near one
    auto-releases, so the surface never empties: the whole gesture is ONE press.
    Two taps always empty it in between. Device-measured 2026-08-28 over every
    labelled gesture — swipes 1 press, double taps 2, no overlap at all.

    This is what catches two taps on DIFFERENT pads, which have no revisit and
    no landing and are otherwise shaped exactly like a swipe (traces 131615 and
    131625: tap L96, lift, tap L100).
    """

    swipe = True

    def test_a_swipe_is_one_press(self):
        self.hz.touch(96, at_ms=0)
        self.hz.touch(100, at_ms=80)      # far pad before the near one releases
        self.hz.release(96)
        self.hz.release(100)
        self.assertEqual(self.hz.h._presses, 1)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_two_taps_on_DIFFERENT_pads_is_a_double_tap(self):
        """No revisit and no landing — the press count is the only evidence."""
        self.hz.touch(96, at_ms=0)
        self.hz.release(96, at_ms=92)     # surface empties, and stays empty
        self.hz.touch(100, at_ms=272)
        self.hz.release(100, at_ms=345)
        self.assertEqual(self.hz.h._presses, 2)
        self.hz.end_session(); self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_second_press_stops_it_being_a_swipe(self):
        """Even inside the travel band, the hand having left means it did not
        cross the surface — it tapped twice."""
        self.hz.touch(96, at_ms=0)
        self.hz.release(96, at_ms=20)     # ...and the hand stayed off for 60 ms
        self.hz.touch(100, at_ms=80)      # gap is inside the band
        self.hz.end_session(); self.hz.decide()
        self.assertNotIn("swipe_action", self.hz.fired)

    def test_a_slide_between_pads_is_not_a_second_press(self):
        """The finger leaves the near pad microseconds after reaching the far
        one, so the surface is briefly empty mid-swipe. Counting that hole as
        the hand arriving again turned a swipe into a double tap — device trace
        135217, a textbook 105.8 ms travel gap lost on a 0.7 ms hole."""
        self.hz.touch(100, at_ms=0)
        self.hz.release(100, at_ms=0)  # surface empty...
        self.hz.touch(96, at_ms=0.7)  # ...for 0.7 ms
        self.assertEqual(self.hz.h._presses, 1)

    def test_a_real_lift_still_counts(self):
        """Every genuine lift in the captured traces left the surface empty for
        at least 23.8 ms; the floor sits at 15."""
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
    """The same rules must hold on three pads.

    The classifier was rewritten for a three-pad lamp and the lamp then dropped
    to two, so "works on the current hardware" is not the property worth
    testing — "does not depend on how many pads are wired" is. These re-run the
    core gestures with a three-pad profile.

    They also guard a trap: when the board profile went to two pads, every
    existing three-pad test kept passing, because a touch on the unwired middle
    pad is silently dropped from the position map. They proved nothing until the
    wiring was declared explicitly, as it is here.
    """

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
