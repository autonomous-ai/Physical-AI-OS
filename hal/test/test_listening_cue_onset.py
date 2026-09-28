"""The listening cue must be visible on its first frame, not after the breath rises."""

import threading
from unittest import mock

import hal.app_state as state
from hal.drivers.rgb.effects import (
    RGB_CMD_PAINT,
    RGB_CMD_SOLID,
    _dither_ring,
    breathing,
    breathing_fine,
)
from hal.presets import (
    EMOTION_PRESETS,
    EMO_GREETING,
    EMO_LISTENING,
    FX_BREATHING,
    FX_BREATHING_FINE,
)


class _RecordingService:
    """Collects dispatched frames and stops the loop after `limit` of them."""

    def __init__(self, stop_event, limit):
        self.frames = []
        self._stop = stop_event
        self._limit = limit

    def dispatch(self, cmd, color):
        assert cmd == RGB_CMD_SOLID
        self.frames.append(color)
        if len(self.frames) >= self._limit:
            self._stop.set()


def _run(color, start_at_peak, limit=3):
    stop = threading.Event()
    svc = _RecordingService(stop, limit)
    breathing(color, 100.0, None, stop, svc, start_at_peak=start_at_peak)
    return svc.frames


# Dim enough that int() truncation renders the arc start as black.
LAMP_LISTENING = (0, 0, 3)


def test_breathing_still_fades_in_by_default():
    assert _run(LAMP_LISTENING, start_at_peak=False)[0] == (0, 0, 0)


def test_start_at_peak_paints_full_brightness_on_the_first_frame():
    assert _run(LAMP_LISTENING, start_at_peak=True)[0] == LAMP_LISTENING


def test_start_at_peak_breathes_down_and_never_exceeds_the_preset():
    frames = _run((0, 0, 30), start_at_peak=True, limit=10)
    assert frames[0] == (0, 0, 30)
    assert all(f[2] <= 30 for f in frames), frames
    assert frames[-1][2] < frames[0][2], frames


def test_only_the_opening_arc_starts_at_the_peak():
    # The opening arc is the peak-to-0 half only: 50 frames.
    frames = _run((0, 0, 30), start_at_peak=True, limit=62)
    assert frames[49][2] == 0, frames[45:62]
    assert frames[50][2] == 0, frames[45:62]
    assert frames[61][2] > frames[51][2], frames[45:62]
    assert max(f[2] for f in frames[50:]) < frames[0][2], frames[45:62]


def test_listening_is_the_only_preset_that_opens_at_the_peak():
    assert EMOTION_PRESETS[EMO_LISTENING]["start_at_peak"] is True
    opted_in = [e for e, p in EMOTION_PRESETS.items() if p.get("start_at_peak")]
    assert opted_in == [EMO_LISTENING], opted_in
    assert "start_at_peak" not in EMOTION_PRESETS[EMO_GREETING]


def test_emotion_led_passes_the_flag_to_the_effect_thread(monkeypatch):
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "rgb_service", mock.Mock())
    monkeypatch.setattr(state, "display_service", None)
    monkeypatch.setattr(state, "sensing_service", None)
    monkeypatch.setattr(state, "_stop_current_effect", lambda: None)
    started = {}

    class _Thread:
        def __init__(self, *args, **kwargs):
            started.update(kwargs.get("kwargs") or {})
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(state.threading, "Thread", _Thread)
    state._apply_emotion_led_display(EMO_LISTENING, force_led=True)
    assert started.get("start_at_peak") is True

    started.clear()
    state._apply_emotion_led_display(EMO_GREETING, force_led=True)
    assert started.get("start_at_peak") is False


class _PaintRec:
    """Collects painted rings and stops the loop after `limit` frames."""

    led_count = 32

    def __init__(self, stop, limit):
        self.frames = []
        self._stop = stop
        self._limit = limit

    def dispatch(self, cmd, colors):
        assert cmd == RGB_CMD_PAINT
        self.frames.append(list(colors))
        if len(self.frames) >= self._limit:
            self._stop.set()


def _run_fine(color, limit, start_at_peak=True):
    stop = threading.Event()
    svc = _PaintRec(stop, limit)
    breathing_fine(color, 100.0, None, stop, svc, start_at_peak=start_at_peak)
    return svc.frames


def test_no_pixel_ever_goes_dark():
    for frame in _run_fine(LAMP_LISTENING, limit=120):
        assert all(px[2] >= 1 for px in frame), frame[:4]


def test_no_pixel_ever_exceeds_the_preset_peak():
    for frame in _run_fine(LAMP_LISTENING, limit=120):
        assert all(px[2] <= LAMP_LISTENING[2] for px in frame), frame[:4]


def test_opening_frame_is_the_whole_ring_at_the_peak():
    assert _run_fine(LAMP_LISTENING, limit=1)[0] == [LAMP_LISTENING] * 32


def test_the_ring_average_moves_in_many_more_steps_than_the_colour_allows():
    sums = {sum(px[2] for px in f) for f in _run_fine(LAMP_LISTENING, limit=100)}
    assert len(sums) > 20, sorted(sums)


def test_raised_pixels_are_scattered_not_a_lit_arc():
    frame = _dither_ring((0, 0, 1), (0, 0, 2), 16, 32)
    raised = [i for i, px in enumerate(frame) if px[2] == 2]
    runs = sum(1 for a, b in zip(raised, raised[1:]) if b != a + 1) + 1
    assert runs > 4, raised


def test_hue_is_preserved_channels_that_are_off_stay_off():
    for frame in _run_fine((0, 4, 8), limit=60):
        assert all(px[0] == 0 for px in frame), frame[:4]


def test_listening_uses_the_dithered_breath():
    assert EMOTION_PRESETS[EMO_LISTENING]["effect"] == FX_BREATHING_FINE
    assert EMOTION_PRESETS[EMO_GREETING]["effect"] == FX_BREATHING
