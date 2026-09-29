"""Configured mic switch behavior without GPIO, route or thread side effects."""

from unittest import mock

import pytest

import hal.app_state as state
from hal.board.privacy_button import PrivacyButtonConfig
from hal.drivers.privacy_button import PrivacyButtonHandler


@pytest.fixture
def hardware():
    gpio = mock.Mock()
    gpio.gpiochip_open.return_value = 12
    gpio.gpio_read.return_value = 1
    with (
        mock.patch.dict("sys.modules", {"lgpio": gpio}),
        mock.patch("hal.drivers.privacy_button.threading.Thread") as thread,
        mock.patch("hal.drivers.privacy_button.threading.Timer") as timer,
    ):
        thread.return_value.is_alive.return_value = False
        yield gpio, thread, timer


def test_disabled_switch_does_not_import_or_claim_gpio(hardware):
    gpio, thread, _ = hardware
    handler = PrivacyButtonHandler(None)
    handler.start()
    handler.stop()
    assert not gpio.mock_calls
    thread.assert_not_called()


@pytest.mark.parametrize("muted_level,level,expected", [(0, 0, True), (0, 1, False), (1, 1, True), (1, 0, False)])
def test_injected_wiring_and_synchronous_boot_sync(hardware, muted_level, level, expected):
    gpio, thread, _ = hardware
    gpio.gpio_read.return_value = level
    handler = PrivacyButtonHandler(PrivacyButtonConfig(chip=3, line=42, muted_level=muted_level))
    handler._apply_state_locked = mock.Mock()
    handler.start()
    gpio.gpiochip_open.assert_called_once_with(3)
    gpio.gpio_claim_alert.assert_called_once_with(12, 42, gpio.BOTH_EDGES, gpio.SET_PULL_UP)
    gpio.gpio_read.assert_called_once_with(12, 42)
    handler._apply_state_locked.assert_called_once_with(expected, initial=True)
    assert handler._last_known_level == level
    thread.return_value.start.assert_called_once_with()
    handler.stop()


@pytest.mark.parametrize("muted_level", [0, 1])
def test_failed_initial_read_keeps_legacy_unmuted_default(hardware, muted_level):
    gpio, _, _ = hardware
    gpio.gpio_read.side_effect = OSError("read unavailable")
    handler = PrivacyButtonHandler(PrivacyButtonConfig(muted_level=muted_level))
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._apply_state_locked.assert_called_once_with(False, initial=True)
    assert handler._last_known_level == 1 - muted_level
    handler.stop()


@pytest.mark.parametrize("muted_level", [0, 1])
def test_extended_privacy_initial_read_failure_stays_muted(hardware, muted_level):
    gpio, _, _ = hardware
    gpio.gpio_read.side_effect = OSError("read unavailable")
    handler = PrivacyButtonHandler(PrivacyButtonConfig(
        muted_level=muted_level, disable_camera_on_mute=True, mute_speaker_on_mute=True,
    ))
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._apply_state_locked.assert_called_once_with(True, initial=True)
    assert handler._last_known_level == muted_level
    handler.stop()


def test_edge_restarts_configured_settle_then_reads_current_level(hardware):
    gpio, _, timer = hardware
    gpio.gpio_read.return_value = 0
    handler = PrivacyButtonHandler(PrivacyButtonConfig(line=43, settle_s=0.12, muted_level=1))
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._apply_state_locked.reset_mock()
    handler._on_edge(0, 43, 0, 100)
    handler._on_edge(0, 43, 0, 101)
    assert timer.call_count == 2
    timer.assert_called_with(0.12, handler._reconcile)
    timer.return_value.cancel.assert_called_once_with()
    handler._apply_state_locked.assert_not_called()
    gpio.gpio_read.return_value = 1
    timer.call_args.args[1]()
    handler._apply_state_locked.assert_called_once_with(True)
    gpio.gpio_read.assert_called_with(12, 43)
    handler.stop()


@pytest.mark.parametrize("sleeping", [False, True])
def test_initial_or_duplicate_edge_does_not_override_software_mute(hardware, sleeping):
    gpio, _, timer = hardware
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._apply_state_locked.reset_mock()
    with mock.patch.object(state, "_sleeping", sleeping), mock.patch.object(state, "_mic_muted", True):
        handler._on_edge(0, 97, 1, 100)
        timer.call_args.args[1]()
        handler._apply_state_locked.assert_not_called()
        assert state._sleeping is sleeping
        assert state._mic_muted is True
    handler.stop()


@pytest.mark.parametrize("physical_level,expected_calls", [(1, 0), (0, 1)])
def test_watchdog_only_reconciles_changed_level(hardware, physical_level, expected_calls):
    gpio, _, _ = hardware
    handler = PrivacyButtonHandler(PrivacyButtonConfig(watchdog_s=8.5))
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._apply_state_locked.reset_mock()
    gpio.gpio_read.return_value = physical_level
    with (
        mock.patch.object(handler._stopped, "wait", side_effect=[False, True]) as wait,
        mock.patch.object(state, "_mic_muted", True),
    ):
        handler._watchdog_loop()
        assert state._mic_muted is True
    assert wait.call_args_list == [mock.call(8.5), mock.call(8.5)]
    assert handler._apply_state_locked.call_count == expected_calls
    handler.stop()


def test_stop_cancels_work_and_releases_handle_once(hardware):
    gpio, thread, timer = hardware
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    handler._apply_state_locked = mock.Mock()
    handler.start()
    handler._on_edge(0, 97, 0, 1)
    handler.stop()
    handler.stop()
    timer.return_value.cancel.assert_called_once_with()
    gpio.callback.return_value.cancel.assert_called_once_with()
    thread.return_value.join.assert_called_once_with(timeout=2.0)
    gpio.gpiochip_close.assert_called_once_with(12)
    reads = gpio.gpio_read.call_count
    handler._apply_state_locked.reset_mock()
    handler._on_edge(0, 97, 1, 2)
    handler._reconcile()
    assert timer.call_count == 1
    assert gpio.gpio_read.call_count == reads
    handler._apply_state_locked.assert_not_called()


def test_failed_claim_closes_open_handle(hardware):
    gpio, thread, _ = hardware
    gpio.gpio_claim_alert.side_effect = OSError("busy")
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    handler.start()
    gpio.gpiochip_close.assert_called_once_with(12)
    thread.assert_not_called()
    assert handler._stopped.is_set()


def test_boot_publishes_hardware_lock_even_when_software_already_matches(hardware):
    gpio, _, _ = hardware
    gpio.gpio_read.return_value = 0
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    with (
        mock.patch.object(state, "_mic_muted", True),
        mock.patch.object(state, "_hw_mic_switch_muted", None),
    ):
        handler.start()
        assert state._hw_mic_switch_muted is True
    handler.stop()


def test_stop_continues_cleanup_when_gpio_operations_fail(hardware, caplog):
    gpio, thread, _ = hardware
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    handler._apply_state_locked = mock.Mock()
    handler.start()
    gpio.callback.return_value.cancel.side_effect = OSError("cancel unavailable")
    gpio.gpiochip_close.side_effect = OSError("close unavailable")
    handler.stop()
    handler.stop()
    thread.return_value.join.assert_called_once_with(timeout=2.0)
    gpio.gpiochip_close.assert_called_once_with(12)
    assert handler._callback is None
    assert handler._handle is None
    assert "callback cancel failed" in caplog.text
    assert "gpiochip_close failed" in caplog.text


def test_start_waits_for_previous_watchdog_to_exit(hardware):
    gpio, thread, _ = hardware
    handler = PrivacyButtonHandler(PrivacyButtonConfig())
    handler._apply_state_locked = mock.Mock()
    handler.start()
    thread.return_value.is_alive.return_value = True
    handler.stop()
    handler.start()
    assert gpio.gpiochip_open.call_count == 1
    assert handler._stopped.is_set()
    thread.return_value.is_alive.return_value = False
    handler.start()
    assert gpio.gpiochip_open.call_count == 2
    handler.stop()


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("initial", [False, True])
def test_boot_restore_does_not_masquerade_as_user_unmute(hardware, extended, initial):
    config = PrivacyButtonConfig(disable_camera_on_mute=extended, mute_speaker_on_mute=extended)
    handler = PrivacyButtonHandler(config)
    with (
        mock.patch.object(state, "_sleeping", False),
        mock.patch.object(state, "_mic_muted", True),
        mock.patch.object(state, "_hw_mic_switch_muted", None),
        mock.patch.object(state, "_clear_mic_muted_led"),
        mock.patch("hal.drivers.privacy_button.privacy.apply") as apply_privacy,
        mock.patch("hal.routes.voice.unmute_mic") as unmute,
        mock.patch("hal.drivers.button_actions.single_click_action") as click,
        mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
        mock.patch("hal.drivers.button_actions.announce_listening_cue") as announce,
    ):
        handler._apply_state_locked(False, initial=initial)
        assert state._hw_mic_switch_muted is False
        if initial:
            unmute.assert_called_once_with()
            click.assert_not_called()
            chime.assert_not_called()
            announce.assert_not_called()
            hardware[1].assert_not_called()
        else:
            unmute.assert_not_called()
            click.assert_called_once()
            if extended:
                chime.assert_called_once_with("privacy-switch")
                announce.assert_called_once_with("privacy-switch")
        if extended:
            apply_privacy.assert_called_once_with(False, config)
        else:
            apply_privacy.assert_not_called()
