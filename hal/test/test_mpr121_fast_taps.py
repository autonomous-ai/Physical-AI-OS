"""Lamp's sampled chip-status pulses, not simulated ADC or physical touch timing."""

from pathlib import Path

import pytest

from hal.board.mpr121 import load_mpr121_config
from hal.drivers.harness.gestures import harness_button_recognizer
from hal.drivers.mpr121 import _GestureRecognizer, _SpatialGestureRecognizer


@pytest.fixture(params=["standard", "device", "harness"])
def mode(request):
    return request.param


def replay(mode, samples):
    config = load_mpr121_config(Path(__file__).resolve().parents[2] / "robots/lamp", "orangepi_sun60")
    factory = {
        "standard": _GestureRecognizer,
        "device": lambda delay: _GestureRecognizer(delay, multi_click=False),
        "harness": harness_button_recognizer,
    }[mode]
    detector = _SpatialGestureRecognizer(config, factory, fast_stationary_tap=mode == "device")
    index, mask, events = 0, 0, []
    # Nominal polling of chip status only: no ADC or scheduler-stall model.
    period = config.poll_ms / 1000
    for tick in range(int((samples[-1][0] + .8) / period) + 1):
        now = tick * period
        while index < len(samples) and samples[index][0] <= now:
            mask = samples[index][1]
            index += 1
        events.extend((now, event.kind) for event in detector.update(mask, now)
                      if event.kind in {"single", "cue", "triple", "hold", "swipe"})
    return events


@pytest.mark.parametrize("phase_ms", [.05, 2.55, 5.05, 7.55])
@pytest.mark.parametrize("width_ms", [10, 25, 30, 40, 50, 80])
def test_short_chord_pulses_across_poll_phases(mode, phase_ms, width_ms):
    start = 1 + phase_ms / 1000
    end = start + width_ms / 1000
    events = replay(mode, [(start, 7), (end, 0)])
    if width_ms == 10:
        assert events == []
        return
    expected = ["single", "cue"] if mode == "standard" else ["single"]
    assert [kind for _, kind in events] == expected
    # Device feedback retains the 30 ms release floor; other modes retain 120 ms.
    delay = .030 if mode == "device" else .120
    assert delay <= events[0][0] - end < delay + .020


@pytest.mark.parametrize("mask", [1, 3])
def test_brief_single_or_two_pad_contacts_remain_rejected(mode, mask):
    assert replay(mode, [(1.001, mask), (1.081, 0)]) == []


def test_separated_spikes_do_not_accumulate_into_a_tap(mode):
    samples = [(1.001, 7), (1.011, 0), (1.041, 7), (1.051, 0),
               (1.081, 7), (1.091, 0)]
    assert replay(mode, samples) == []


def test_boot_hold_is_suppressed_then_next_short_tap_works(mode):
    assert replay(mode, [(0, 7), (3, 0)]) == []
    events = replay(mode, [(0, 7), (3, 0), (4.001, 7), (4.031, 0)])
    expected = ["single", "cue"] if mode == "standard" else ["single"]
    assert [kind for _, kind in events] == expected


def test_travel_still_resolves_as_one_swipe(mode):
    samples = [(1.001 + pad * .04, 1 << pad) for pad in range(8)] + [(1.401, 0)]
    assert [kind for _, kind in replay(mode, samples)] == ["swipe"]


def test_hold_does_not_turn_into_a_short_tap(mode):
    assert [kind for _, kind in replay(mode, [(1.001, 7), (3.101, 0)])] == ["hold"]
