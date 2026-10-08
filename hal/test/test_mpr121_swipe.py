"""Spatial MPR121 gestures, including measured hardware mask traces."""

import unittest
from unittest import mock

from hal.board.mpr121 import MPR121Config
from hal.drivers.mpr121 import MPR121Handler, _GestureEvent, _SpatialGestureRecognizer

ACTIONS = {"single", "cue", "triple", "hold", "swipe"}


def replay(samples, axis=tuple(range(12)), debounce_ms=30, tap_min_electrodes=1, button_factory=None):
    detector = _SpatialGestureRecognizer(MPR121Config(bus=0, swipe_axis=axis, debounce_ms=debounce_ms, poll_ms=1, tap_min_electrodes=tap_min_electrodes), button_factory)
    detector.update(0, -1)
    events = []
    index = 0
    mask = 0
    for tick in range(int((samples[-1][0] + .7) * 1000) + 1):
        now = tick / 1000
        while index < len(samples) and samples[index][0] <= now:
            mask = samples[index][1]
            index += 1
        events.extend(detector.update(mask, now))
    return events


def kinds(events):
    return [e.kind for e in events if e.kind in ACTIONS]


class TestTapFootprint(unittest.TestCase):
    def test_small_contacts_never_produce_taps_or_burst_cues(self):
        for axis in (tuple(range(12)), None):
            for mask in (0x004, 0x006):
                samples = [(1, mask), (1.10, 0), (1.25, mask), (1.35, 0), (1.5, mask), (1.6, 0)]
                self.assertEqual(kinds(replay(samples, axis=axis, tap_min_electrodes=3)), [])

    def test_three_pad_tap_qualifies_even_when_release_is_staggered(self):
        for axis in (tuple(range(12)), None):
            events = replay([(1, 7), (1.08, 3), (1.10, 1), (1.12, 0)], axis=axis, tap_min_electrodes=3)
            self.assertEqual(kinds(events), ["single", "cue"])

    def test_short_third_pad_spike_does_not_qualify(self):
        self.assertEqual(kinds(replay([(1, 3), (1.05, 7), (1.06, 3), (1.12, 0)], tap_min_electrodes=3)), [])

    def test_irregular_poll_qualifies_elapsed_chord_on_falling_edge(self):
        for falling_edge, expected in ((1.074, ["single", "cue"]), (1.069, [])):
            with self.subTest(falling_edge=falling_edge):
                detector = _SpatialGestureRecognizer(MPR121Config(
                    bus=0, swipe_axis=tuple(range(12)), tap_min_electrodes=3,
                ))
                samples = [(0, 0), (1, 0x800), (1.020, 0xe00),
                           (1.040, 0xe00), (1.060, 0xe00),
                           (falling_edge, 0xa00), (1.10, 0),
                           (1.12, 0), (1.23, 0), (2, 0)]
                events = [event for now, mask in samples for event in detector.update(mask, now)]
                self.assertEqual(kinds(events), expected)

    def test_short_qualification_intervals_do_not_accumulate(self):
        detector = _SpatialGestureRecognizer(MPR121Config(
            bus=0, swipe_axis=tuple(range(12)), tap_min_electrodes=3,
        ))
        samples = [(0, 0), (1, 0x800), (1.020, 0xe00), (1.040, 0xe00),
                   (1.060, 0x800), (1.070, 0x800), (1.080, 0xe00), (1.090, 0xe00),
                   (1.110, 0x800), (1.13, 0), (1.15, 0), (1.26, 0), (2, 0)]
        events = [event for now, mask in samples for event in detector.update(mask, now)]
        self.assertEqual(kinds(events), [])

    def test_stationary_growth_into_three_pad_contact_is_a_tap(self):
        samples = [(1, 1), (1.06, 7), (1.18, 0)]
        self.assertEqual(kinds(replay(samples, tap_min_electrodes=3)), ["single", "cue"])

    def test_anchored_growth_and_shrinking_release_do_not_swipe(self):
        growth = [(1, 1), (1.06, 7), (1.10, 31), (1.14, 127)]
        for release in ([(1.22, 0)], [(1.20, 126), (1.22, 124), (1.24, 120), (1.26, 0)]):
            self.assertEqual(kinds(replay(growth + release, tap_min_electrodes=3)), ["single", "cue"])

    def test_expanding_footprint_can_then_travel_as_a_swipe(self):
        samples = [(1, 1), (1.06, 7), (1.10, 14), (1.14, 28),
                   (1.18, 56), (1.22, 112), (1.28, 0)]
        self.assertEqual(kinds(replay(samples, tap_min_electrodes=3)), ["swipe"])

    def test_qualification_does_not_leak_to_next_contact(self):
        events = replay([(1, 7), (1.1, 0), (2, 1), (2.1, 0)], tap_min_electrodes=3)
        self.assertEqual(kinds(events), ["single", "cue"])

    def test_single_pad_travel_still_swipes(self):
        samples = [(1 + i * .04, 1 << i) for i in range(8)] + [(1.4, 0)]
        self.assertEqual(kinds(replay(samples, tap_min_electrodes=3)), ["swipe"])

    def test_single_pad_hold_keeps_its_existing_behavior(self):
        self.assertEqual(kinds(replay([(1, 1), (3.1, 0)], tap_min_electrodes=3)), ["hold"])

    def test_harness_taps_filter_without_disabling_exit_hold(self):
        from hal.drivers.harness.gestures import harness_button_recognizer
        for mask, expected in ((1, []), (3, []), (7, ["single"])):
            events = replay([(1, mask), (1.1, 0)], tap_min_electrodes=3, button_factory=harness_button_recognizer)
            self.assertEqual(kinds(events), expected)
        events = replay([(1, 1), (3.2, 0)], tap_min_electrodes=3, button_factory=harness_button_recognizer)
        self.assertEqual(kinds(events), ["hold"])


class TestSpatialGestures(unittest.TestCase):
    def test_both_directions_overlap_and_partial_strip(self):
        for direction in (1, -1):
            order = range(8) if direction == 1 else range(11, 3, -1)
            samples = [(1 + i * .04, (1 << pad) | (1 << (pad + direction)))
                       for i, pad in enumerate(order)]
            samples.append((1.4, 0))
            events = replay(samples)
            self.assertEqual(kinds(events), ["swipe"])
            self.assertEqual([e.direction for e in events if e.kind == "swipe"], [direction])

    def test_two_pads_handoff_and_independent_taps(self):
        self.assertEqual(kinds(replay([(1, 1), (1.05, 0), (1.09, 2), (1.16, 0)], axis=(0, 1))), ["swipe"])
        result = kinds(replay([(1, 1), (1.06, 0), (1.3, 2), (1.36, 0)], axis=(0, 1)))
        self.assertNotIn("swipe", result)

    def test_stationary_cluster_and_simultaneous_whole_hand(self):
        for mask in (1, 15, 4095):
            self.assertEqual(kinds(replay([(1, mask), (1.08, mask & ~2), (1.1, 0)])), ["single", "cue"])

    def test_hold_thresholds_and_no_early_led_tier(self):
        for duration in (1.999, 2, 4.999, 5, 9.999, 10):
            events = replay([(1, 15), (1 + duration, 0)])
            actions = [e for e in events if e.kind in ACTIONS]
            if duration < 2:
                self.assertEqual([e.kind for e in actions], ["single", "cue"])
                self.assertFalse(any(e.kind == "hold_tier" for e in events))
            else:
                self.assertEqual([e.kind for e in actions], ["hold"])
                self.assertAlmostEqual(actions[0].held_s, duration)

    def test_reversal_and_slow_drag_never_fall_back_to_hold(self):
        for samples in (
            [(1, 1), (1.07, 2), (1.14, 4), (1.21, 8), (1.28, 16),
             (1.35, 8), (1.42, 4), (1.49, 2), (1.56, 1), (1.65, 0)],
            [(1, 1), (4, 2), (7, 4), (11, 8), (15, 0)],
            [(1, 1), (2, 2), (15, 0)],
        ):
            self.assertEqual(kinds(replay(samples)), [])

    def test_stationary_chatter_and_unselected_electrodes(self):
        self.assertEqual(kinds(replay([(1, 1), (1.002, 0)])), [])
        config = MPR121Config(bus=0, electrodes=(0, 1), swipe_axis=(0, 1))
        detector = _SpatialGestureRecognizer(config)
        self.assertEqual(detector.update(1 << 11, 0), [])
        self.assertEqual(detector.update(1 << 11, 10), [])

    def test_boot_hold_and_cancel(self):
        detector = _SpatialGestureRecognizer(MPR121Config(bus=0, swipe_axis=(0, 1)))
        for mask, now in ((1, 0), (2, 1), (2, 10), (0, 11), (0, 11.04)):
            self.assertEqual(kinds(detector.update(mask, now)), [])
        detector.update(1, 12)
        detector.update(1, 12.04)
        detector.cancel()
        self.assertEqual(kinds(detector.update(0, 30)), [])

    def test_shared_action_and_led_barrier(self):
        handler = MPR121Handler(MPR121Config(bus=0, swipe_axis=(0, 1)))
        feedback = mock.Mock()
        handler._hold_led = feedback
        with mock.patch("hal.drivers.button_actions.swipe_action") as action:
            handler._execute(_GestureEvent("swipe", 1, direction=1))
            feedback.commit.assert_called_once_with(0)
            action.assert_called_once_with(source="MPR121")
            feedback.commit.return_value = False
            handler._execute(_GestureEvent("swipe", 2, direction=1))
            self.assertEqual(action.call_count, 1)

    def test_direction_routes_to_one_action_after_release(self):
        for direction in (1, -1):
            handler = MPR121Handler(MPR121Config(bus=0, swipe_axis=tuple(range(12))))
            order = range(8) if direction == 1 else range(11, 3, -1)
            samples = [(1 + i * .04, (1 << pad) | (1 << (pad + direction)))
                       for i, pad in enumerate(order)]
            samples.append((5, 0))
            events = [e for e in replay(samples) if e.kind in ACTIONS]
            self.assertEqual([e.kind for e in events], ["swipe"])
            with mock.patch("hal.drivers.button_actions.swipe_action") as sleep, \
                    mock.patch("hal.drivers.harness.actions.toggle_harness_voice") as harness:
                for event in events:
                    handler._execute(event)
                self.assertEqual(sleep.call_count, int(direction == 1))
                self.assertEqual(harness.call_count, int(direction == -1))

    def test_harness_swipe_respects_cancelled_feedback_and_invalid_direction(self):
        handler = MPR121Handler(MPR121Config(bus=0, swipe_axis=(0, 1)))
        handler._hold_led = mock.Mock()
        handler._hold_led.commit.return_value = False
        with mock.patch("hal.drivers.harness.actions.toggle_harness_voice") as harness, \
                mock.patch("hal.drivers.button_actions.swipe_action") as sleep:
            handler._execute(_GestureEvent("swipe", 1, direction=-1))
            harness.assert_not_called()
            handler._hold_led.commit.return_value = True
            with self.assertLogs("hal.drivers.mpr121", level="WARNING"):
                handler._execute(_GestureEvent("swipe", 2))
            harness.assert_not_called()
            sleep.assert_not_called()

    def test_axis_order_defines_left_and_right(self):
        samples = [(1 + i * .06, 1 << pad) for i, pad in enumerate(range(5))] + [(1.4, 0)]
        forward = [e.direction for e in replay(samples) if e.kind == "swipe"]
        reversed_axis = [e.direction for e in replay(samples, axis=tuple(reversed(range(12)))) if e.kind == "swipe"]
        self.assertEqual(forward, [1])
        self.assertEqual(reversed_axis, [-1])

    def test_tap_after_harness_swipe_remains_a_tap(self):
        samples = [(1 + i * .04, 1 << pad) for i, pad in enumerate(range(11, 3, -1))]
        samples += [(1.4, 0), (2.2, 4), (2.3, 0)]
        self.assertEqual(kinds(replay(samples)), ["swipe", "single", "cue"])

    def test_optional_axis_validation(self):
        self.assertIsNone(MPR121Config(bus=0).swipe_axis)
        self.assertEqual(MPR121Config(bus=0, swipe_axis=[2, 1, 0]).swipe_axis, (2, 1, 0))
        for axis in ([1], [0, 0], [0, 12], [True, 2], "01"):
            with self.assertRaises(ValueError):
                MPR121Config(bus=0, swipe_axis=axis)
        with self.assertRaises(ValueError):
            MPR121Config(bus=0, electrodes=(0, 1), swipe_axis=(0, 2))

    def test_recorded_hardware_traces_at_10ms_poll(self):
        traces = [
            [(0.000000, 0x00f), (0.068565, 0x00d), (0.080512, 0x000)],
            [(0.000000, 0xc00), (0.032394, 0xe00), (0.108233, 0xf00), (0.140405, 0xd00), (0.151704, 0x780), (0.194663, 0x3c0), (0.223980, 0x1c0), (0.235201, 0x1e0), (0.246512, 0x0f0), (0.268403, 0x070), (0.279260, 0x078), (0.290454, 0x038), (0.301521, 0x03c), (0.323502, 0x01c), (0.338959, 0x01e), (0.350452, 0x00f), (0.372375, 0x007), (0.384148, 0x003), (0.395206, 0x001), (0.407554, 0x000)],
            [(0.000000, 0x001), (0.064855, 0x003), (0.097039, 0x007), (0.109994, 0x006), (0.121576, 0x00e), (0.132669, 0x00c), (0.156368, 0x01c), (0.169716, 0x018), (0.181015, 0x030), (0.192587, 0x070), (0.203921, 0x060), (0.215169, 0x0c0), (0.226225, 0x180), (0.247988, 0x100), (0.259447, 0x000)],
            [(0.000000, 0x300), (0.046940, 0x100), (0.058170, 0x180), (0.069253, 0x080), (0.080455, 0x0c0), (0.091656, 0x060), (0.102706, 0x020), (0.113845, 0x030), (0.124995, 0x018), (0.146249, 0x006), (0.159542, 0x003), (0.170677, 0x001), (0.181714, 0x000)],
            [(0.000000, 0x01e), (0.012110, 0x01f), (0.055090, 0x00f), (0.066287, 0x00a), (0.077446, 0x000)],
        ]
        expected = [["single", "cue"], ["swipe"], ["swipe"], ["swipe"], ["single", "cue"]]
        for phase in (0, .005):
            for samples, outcome in zip(traces, expected):
                detector = _SpatialGestureRecognizer(MPR121Config(bus=0, swipe_axis=tuple(range(12))))
                detector.update(0, -1)
                index = 0
                mask = 0
                events = []
                for tick in range(int((samples[-1][0] + .7) * 100) + 1):
                    now = tick / 100 + phase
                    while index < len(samples) and samples[index][0] <= now:
                        mask = samples[index][1]
                        index += 1
                    events.extend(detector.update(mask, now))
                self.assertEqual(kinds(events), outcome)

    def test_raw_touch_blocks_pending_triple_deadline(self):
        samples = [(1, 1), (1.06, 0), (1.2, 1), (1.26, 0),
                   (1.4, 1), (1.46, 0), (1.85, 1), (4, 0)]
        self.assertEqual(kinds(replay(samples)), ["single", "hold"])

    def test_swipe_cancels_pending_triple(self):
        samples = [(1, 1), (1.06, 0), (1.2, 1), (1.26, 0),
                   (1.4, 1), (1.46, 0), (1.7, 1), (1.76, 2),
                   (1.82, 4), (1.88, 8), (1.94, 16), (2.02, 0)]
        self.assertEqual(kinds(replay(samples)), ["single", "swipe"])

    def test_mixed_axis_and_off_axis_drag_cannot_reset(self):
        for first, second in ((1 << 11, 1), (1, 1 << 11)):
            self.assertEqual(kinds(replay([(1, first), (2, first | second), (12, 0)], axis=(0, 1))), [])

    def test_release_chatter_preserves_stationary_hold(self):
        events = replay([(1, 1), (1.5, 0), (1.51, 1), (3.2, 0)])
        actions = [e for e in events if e.kind in ACTIONS]
        self.assertEqual([e.kind for e in actions], ["hold"])
        self.assertAlmostEqual(actions[0].held_s, 2.2)

    def test_distant_independent_touches_are_not_swipes(self):
        self.assertEqual(kinds(replay([(1, 1), (1.06, 0), (1.08, 1 << 11), (1.15, 0)])), [])


class TestFastSwipeSkipsPads(unittest.TestCase):
    def test_fast_swipe_that_skips_pads_still_resolves(self):
        # Recorded on lamp-0c4e: pads dwelt ~12 ms, so the centroid leaped 5.5 -> 9.0.
        samples = [(0.000, 0x00c), (0.061, 0x038), (0.074, 0x060), (0.097, 0x380), (0.109, 0x600), (0.122, 0x000)]
        for phase in (0, .0025, .005, .0075):
            detector = _SpatialGestureRecognizer(MPR121Config(bus=0, swipe_axis=tuple(range(12))))
            detector.update(0, -1)
            index, mask, events = 0, 0, []
            for tick in range(int((samples[-1][0] + .7) * 100) + 1):
                now = tick / 100 + phase
                while index < len(samples) and samples[index][0] <= now:
                    mask = samples[index][1]
                    index += 1
                events.extend(detector.update(mask, now))
            self.assertEqual(kinds(events), ["swipe"], phase)
            self.assertEqual([e.direction for e in events if e.kind == "swipe"], [1], phase)

    def test_second_finger_far_away_before_travel_is_not_a_swipe(self):
        samples = [(1, 0x002), (1.1, 0x102), (1.3, 0x000)]
        self.assertNotIn("swipe", kinds(replay(samples)))
