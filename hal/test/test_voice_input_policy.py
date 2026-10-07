"""Input policy keeps automatic and Harness behavior separate from device taps."""
from unittest.mock import Mock, patch

import pytest

from hal.drivers.voice._internal.harness_capture import HarnessCapture
from hal.drivers.voice._internal.input_policy import (
    InputPolicy, device_snapshot, requires_manual_capture, same_capture_target,
)

LOCAL = {"enabled": False, "generation": 4}
HARNESS = dict(LOCAL, enabled=True, focusAvailable=True,
               machineId="mac", agentId="agent", focusRevision=1)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("mode", ["automatic", "tap_to_talk"])
def test_automatic_and_manual_turn_policy(mode, live, monkeypatch):
    from hal import config
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", mode)
    assert requires_manual_capture(LOCAL) == (mode == "tap_to_talk")
    automatic = InputPolicy.for_turn(LOCAL, None, live_mode=live)
    assert automatic.automatic
    assert automatic.realtime_allowed == (not live)
    assert not automatic.dispatch_directly
    with patch("hal.drivers.harness.led.set_capturing") as led:
        emotion = Mock()
        automatic.set_capturing(True, emotion)
        automatic.set_capturing(False, emotion)
    emotion.assert_not_called()
    led.assert_not_called()

    for snapshot in (HARNESS, device_snapshot(LOCAL)):
        policy = InputPolicy.for_turn(snapshot, object(), live_mode=live)
        assert not policy.automatic
        assert policy.realtime_allowed == (snapshot == device_snapshot(LOCAL))
        assert policy.dispatch_directly


def test_default_harness_controller_does_not_accept_device_capture(monkeypatch):
    from hal import config
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")
    assert not HarnessCapture().start(device_snapshot(LOCAL))
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(device_snapshot(LOCAL))
    assert not control.start(HARNESS)
    assert control.claim(HARNESS) is None
    assert not control.active
    assert control.start(HARNESS)
    assert control.claim(LOCAL) is None
    assert not control.active


def test_harness_visuals_and_focus_change_keep_existing_behavior(monkeypatch):
    from hal import config
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk")
    policy = InputPolicy.for_turn(HARNESS, object(), live_mode=False)
    with patch("hal.drivers.harness.led.set_capturing") as led:
        emotion = Mock()
        policy.set_capturing(True, emotion)
        policy.set_capturing(False, emotion)
    assert [c.args for c in led.call_args_list] == [(True,), (False,)]
    emotion.assert_not_called()
    control = HarnessCapture(target_matches=same_capture_target)
    assert control.start(HARNESS)
    assert control.claim(dict(HARNESS, focusRevision=2)) is None
    assert not control.active
