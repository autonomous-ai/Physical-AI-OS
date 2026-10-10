"""Restore callbacks cannot outlive the LED state that scheduled them."""
import threading
from unittest import mock

import pytest
import hal.app_state as state


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(state, '_restore_lock', threading.RLock())
    monkeypatch.setattr(state, '_restore_generation', 0)
    monkeypatch.setattr(state, '_restore_timer', None)
    display = mock.Mock()
    monkeypatch.setattr(state, '_restore_user_led_display', display)
    timers = []

    class Timer:
        def __init__(self, delay, callback, args=()):
            self.callback, self.args = callback, args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            # A callback already dispatched by threading.Timer survives cancel.
            self.callback(*self.args)

    monkeypatch.setattr(state.threading, 'Timer', Timer)
    return timers, display


def test_old_dispatched_callback_does_not_repaint_or_erase_replacement(lifecycle):
    timers, display = lifecycle
    state._schedule_led_restore(1)
    state._schedule_led_restore(10)
    timers[0].fire()
    assert timers[0].cancelled
    display.assert_not_called()
    assert state._restore_timer is timers[1]
    timers[1].fire()
    display.assert_called_once_with()
    assert state._restore_timer is None


def test_cancel_and_immediate_restore_invalidate_old_callback(lifecycle):
    timers, display = lifecycle
    state._schedule_led_restore(1)
    state._cancel_pending_restore()
    timers[0].fire()
    display.assert_not_called()
    state._schedule_led_restore(1)
    state._restore_user_led()
    timers[1].fire()
    display.assert_called_once_with()


def test_replacement_waits_for_already_rendering_restore(lifecycle):
    timers, display = lifecycle
    entered, release, replacing, replaced = (threading.Event() for _ in range(4))
    order = []

    def render():
        entered.set()
        assert release.wait(2)
        order.append('old_finished')

    def replace():
        replacing.set()
        state._cancel_pending_restore()
        order.append('new_owner')
        replaced.set()

    display.side_effect = render
    state._schedule_led_restore(1)
    worker = threading.Thread(target=timers[0].fire)
    other = threading.Thread(target=replace)
    worker.start()
    try:
        assert entered.wait(2)
        other.start()
        assert replacing.wait(2)
        assert not replaced.is_set()
    finally:
        release.set()
        worker.join(2)
        if other.ident is not None:
            other.join(2)
    assert not worker.is_alive() and not other.is_alive()
    assert order == ['old_finished', 'new_owner']


def test_transient_solid_invalidates_before_paint(lifecycle, monkeypatch):
    from hal.routes.led import _set_led_solid
    from hal.models import LEDSolidRequest

    timers, display = lifecycle
    monkeypatch.setattr(state, '_sleeping', False)
    monkeypatch.setattr(state, '_stop_current_effect', mock.Mock())
    rgb = mock.Mock()
    monkeypatch.setattr(state, 'rgb_service', rgb)
    state._schedule_led_restore(1)
    # Force the old callback to arrive at the first new hardware write.
    rgb.dispatch.side_effect = lambda *args: timers[0].fire()
    _set_led_solid(LEDSolidRequest(color=[1, 2, 3], transient=True))
    display.assert_not_called()
    rgb.dispatch.assert_called_once()
