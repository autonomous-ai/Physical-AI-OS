"""The final LED frame must stay black even when producers outlive shutdown."""

import threading
from unittest import mock

import pytest

from hal.drivers.base import ServiceBase
from hal.drivers.rgb.rgb_service import RGBService
from hal.presets import RGB_CMD_PAINT, RGB_CMD_SOLID


class Strip:
    def __init__(self):
        self.pixels = [(0, 12, 0)] * 2
        self.frames = []
        self.closed = False
        self.close_count = 0

    def fill(self, color, count):
        assert not self.closed
        self.pixels[:count] = [color] * count

    def setPixelColor(self, index, color):
        assert not self.closed
        self.pixels[index] = color

    def getPixelColor(self, index):
        assert not self.closed
        return self.pixels[index]

    def show(self):
        assert not self.closed
        self.frames.append(tuple(self.pixels))

    def deinit(self):
        assert not self.closed
        self.closed = True
        self.close_count += 1


@pytest.fixture
def rgb():
    strip = Strip()
    svc = RGBService.__new__(RGBService)
    ServiceBase.__init__(svc, "rgb-shutdown-test")
    svc.led_count = 2
    svc._driver = strip
    svc._driver_lock = threading.RLock()
    svc._closing = False
    svc._safety = None
    yield svc, strip
    ServiceBase.stop(svc, timeout=1)


@pytest.mark.parametrize("event,payload", [
    (RGB_CMD_SOLID, (0, 12, 0)),
    (RGB_CMD_PAINT, [(0, 12, 0)] * 2),
])
def test_late_frame_cannot_relight_final_clear(rgb, event, payload):
    svc, strip = rgb
    svc.start()
    clear = svc.clear

    def clear_with_late_producer():
        clear()
        assert not svc.is_running
        assert not svc._worker_thread.is_alive()
        # Both queued producers and direct handlers must be harmless now.
        svc.dispatch(event, payload)
        svc.handle_event(event, payload)

    with mock.patch.object(svc, "clear", side_effect=clear_with_late_producer):
        svc.stop(timeout=1)
    assert strip.frames[-1] == ((0, 0, 0),) * 2
    assert strip.closed
    assert svc._driver is None
    svc.handle_event(event, payload)
    svc.clear()
    svc.stop()
    assert svc.getPixelColor(0) == 0
    assert strip.close_count == 1


def test_worker_resuming_after_join_timeout_cannot_touch_closed_driver(rgb):
    svc, strip = rgb
    entered, release = threading.Event(), threading.Event()
    handle = svc.handle_event

    def delayed_handler(event, payload):
        entered.set()
        assert release.wait(2)
        handle(event, payload)

    svc.handle_event = delayed_handler
    svc.logger = mock.Mock()
    svc.start()
    try:
        svc.dispatch(RGB_CMD_SOLID, (0, 12, 0))
        assert entered.wait(1)
        svc.stop(timeout=0.01)
        assert strip.closed
        frames_at_close = list(strip.frames)
        release.set()
        svc._worker_thread.join(timeout=1)
        assert not svc._worker_thread.is_alive()
        assert strip.frames == frames_at_close
        assert strip.frames[-1] == ((0, 0, 0),) * 2
        svc.logger.error.assert_not_called()
    finally:
        release.set()
        svc._worker_thread.join(timeout=1)


def test_shutdown_still_closes_driver_when_clear_fails(rgb):
    svc, strip = rgb
    svc.start()
    with mock.patch.object(svc, "clear", side_effect=RuntimeError("SPI failure")):
        with pytest.raises(RuntimeError, match="SPI failure"):
            svc.stop(timeout=1)
    assert not svc.is_running
    assert strip.closed
    assert svc._driver is None
