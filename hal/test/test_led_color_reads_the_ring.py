"""GET /led/color must describe the whole ring, not pixel 0."""

from unittest import mock

import hal.app_state as state
from hal.routes.led import _read_ring, get_led_color


class _Ring:
    """Minimal stand-in for RGBService: packed read-back, like the real one."""

    def __init__(self, pixels):
        self._pixels = pixels
        self.led_count = len(pixels)
        self.strip = self

    def getPixelColor(self, index):
        r, g, b = self._pixels[index]
        return (r << 16) | (g << 8) | b


def _no_effect(monkeypatch, service):
    monkeypatch.setattr(state, "rgb_service", service)
    monkeypatch.setattr(state, "_effect_name", None)
    monkeypatch.setattr(state, "_effect_thread", None)
    monkeypatch.setattr(state, "_effect_base_color", None)
    monkeypatch.setattr(state, "_active_scene", None)


def test_a_lit_ring_with_a_dark_first_pixel_is_not_reported_off(monkeypatch):
    pixels = [(0, 0, 0)] + [(0, 2, 2)] * 31
    _no_effect(monkeypatch, _Ring(pixels))

    resp = get_led_color()

    assert resp["on"] is True
    assert resp["color"] == [0, 2, 2]
    assert resp["uniform"] is False


def test_a_uniform_ring_answers_as_before(monkeypatch):
    _no_effect(monkeypatch, _Ring([(0, 0, 3)] * 32))

    resp = get_led_color()

    assert resp["on"] is True
    assert resp["color"] == [0, 0, 3]
    assert resp["uniform"] is True
    assert resp["hex"] == "#000003"


def test_a_dark_ring_is_still_off(monkeypatch):
    _no_effect(monkeypatch, _Ring([(0, 0, 0)] * 32))

    resp = get_led_color()

    assert resp["on"] is False
    assert resp["color"] == [0, 0, 0]
    assert resp["uniform"] is True


# breathing_fine is non-uniform by design: k pixels one unit above the rest.
def test_a_dithered_ring_reports_the_brightest_pixel(monkeypatch):
    pixels = [(0, 0, 2) if i % 3 == 0 else (0, 0, 1) for i in range(32)]
    _no_effect(monkeypatch, _Ring(pixels))

    resp = get_led_color()

    assert resp["color"] == [0, 0, 2]
    assert resp["uniform"] is False
    assert resp["brightness"] == round(2 / 255.0, 3)


def test_a_driver_that_cannot_read_back_reports_dark_instead_of_raising():
    class _Broken:
        led_count = 8

        def __init__(self):
            self.strip = self

        def getPixelColor(self, index):
            raise RuntimeError("no read-back on this driver")

    with mock.patch.object(state, "logger", mock.Mock()):
        assert _read_ring(_Broken()) == []


def _with_effect(monkeypatch, service, base_color):
    thread = mock.Mock()
    thread.is_alive.return_value = True
    monkeypatch.setattr(state, "rgb_service", service)
    monkeypatch.setattr(state, "_effect_name", "speaking_wave")
    monkeypatch.setattr(state, "_effect_thread", thread)
    monkeypatch.setattr(state, "_effect_base_color", base_color)
    monkeypatch.setattr(state, "_active_scene", None)


def test_an_effect_painting_nothing_is_not_reported_as_lit(monkeypatch):
    _with_effect(monkeypatch, _Ring([(0, 0, 0)] * 32), (0, 0, 0))

    resp = get_led_color()

    assert resp["on"] is False
    assert resp["effect"] == "speaking_wave"


def test_an_effect_actually_painting_is_reported_as_lit(monkeypatch):
    pixels = [(0, 0, 0)] * 30 + [(0, 4, 0), (0, 2, 0)]
    _with_effect(monkeypatch, _Ring(pixels), (0, 8, 0))

    resp = get_led_color()

    assert resp["on"] is True
    assert resp["color"] == [0, 8, 0]
    assert resp["uniform"] is False
