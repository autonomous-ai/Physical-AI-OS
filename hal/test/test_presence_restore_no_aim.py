"""A presence light restore restores light. Nothing else (#314)."""

import inspect
from unittest import mock

import pytest

from hal.drivers.sensing.presence_service import PresenseService


def _present_service(rgb):
    svc = PresenseService(rgb_service=rgb)
    svc._light_is_off = lambda: False
    return svc


def test_a_presence_light_restore_does_not_move_the_body():
    rgb = mock.Mock()
    svc = _present_service(rgb)

    svc._restore_light()

    assert not hasattr(svc, "_on_restore_aim"), (
        "the aim callback is gone — a presence transition must not command a servo"
    )


def test_the_aim_callback_can_no_longer_be_wired_in():
    """The parameter is removed, not just left unused."""
    assert "on_restore_aim" not in inspect.signature(PresenseService.__init__).parameters

    with pytest.raises(TypeError):
        PresenseService(rgb_service=mock.Mock(), on_restore_aim=lambda: None)


def test_the_light_still_comes_back():
    rgb = mock.Mock()
    svc = _present_service(rgb)

    with mock.patch("hal.app_state._restore_user_led") as restore:
        svc._restore_light()

    restore.assert_called_once_with()


def test_a_dark_strip_is_left_dark():
    rgb = mock.Mock()
    svc = _present_service(rgb)
    svc._light_is_off = lambda: True

    svc._restore_light()

    assert not rgb.dispatch.called
