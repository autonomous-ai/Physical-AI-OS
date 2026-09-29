"""Privacy overlays preserve preferences and cannot be bypassed by consumers."""

from unittest import mock

import pytest
from fastapi import HTTPException

from hal import app_state as state, privacy
from hal.board.privacy_button import PrivacyButtonConfig
from hal.drivers.privacy_button import PrivacyButtonHandler
from hal.routes import camera, music


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key, value in {"camera_muted": False, "speaker_muted": False,
                       "camera_before": None, "speaker_before": None}.items():
        monkeypatch.setattr(privacy, key, value)
    for key, value in {
        "_camera_disabled": False, "_camera_manual_override": False,
        "_speaker_muted": False, "_mic_muted": False,
        "_hw_mic_switch_muted": None, "_mic_muted_led": False,
        "camera_capture": mock.Mock(), "tts_service": mock.Mock(),
        "music_service": mock.Mock(), "voice_service": mock.Mock(),
        "_save_boot_sidecar": mock.Mock(),
    }.items():
        monkeypatch.setattr(state, key, value)


def config():
    return PrivacyButtonConfig(disable_camera_on_mute=True, mute_speaker_on_mute=True)


@pytest.mark.parametrize("camera_disabled,speaker_muted", [(False, False), (True, False),
                                                          (False, True), (True, True)])
def test_lock_restore_preserves_preferences(camera_disabled, speaker_muted):
    state._camera_disabled = camera_disabled
    state._camera_manual_override = camera_disabled
    state._speaker_muted = speaker_muted
    privacy.apply(True, config())
    assert state._camera_disabled and state._speaker_muted
    state.camera_capture.stop.assert_called_once()
    state.tts_service.stop.assert_called_once()
    state.music_service.stop.assert_called_once()
    privacy.apply(True, config())
    assert privacy.camera_before == camera_disabled
    assert privacy.speaker_before == speaker_muted
    state._save_boot_sidecar.assert_any_call(state._CAMERA_STATE_PATH, {
        "disabled": camera_disabled, "manual_override": camera_disabled,
    })
    state._save_boot_sidecar.assert_any_call(state._SPEAKER_STATE_PATH, {"muted": speaker_muted})
    privacy.apply(False, config())
    assert state._camera_disabled == camera_disabled
    assert state._camera_manual_override == camera_disabled
    assert state._speaker_muted == speaker_muted
    assert state.camera_capture.start.call_count == (0 if camera_disabled else 1)


def test_default_intern_handler_never_applies_peripheral_policy():
    state._mic_muted = True
    with mock.patch.object(privacy, "apply") as apply:
        PrivacyButtonHandler(PrivacyButtonConfig())._apply_state_locked(True)
    apply.assert_not_called()
    assert state._hw_mic_switch_muted is True
    assert not state._camera_disabled and not state._speaker_muted


def test_lamp_locks_peripherals_when_mic_already_muted():
    state._mic_muted = True
    PrivacyButtonHandler(config())._apply_state_locked(True)
    assert privacy.camera_muted and privacy.speaker_muted
    assert state._camera_disabled and state._speaker_muted


def test_prepare_fails_closed_before_camera_and_audio_start():
    privacy.prepare(config())
    assert privacy.mic_locked()
    assert state._mic_muted and state._speaker_muted and state._camera_disabled
    guarded = privacy.GuardedCamera(state.camera_capture)
    guarded.start()
    state.camera_capture.start.assert_not_called()


@pytest.mark.parametrize("cfg", [None, PrivacyButtonConfig()])
def test_prepare_does_not_change_intern_or_unconfigured_device(cfg):
    privacy.prepare(cfg)
    assert state._hw_mic_switch_muted is None
    assert not state._mic_muted and not state._speaker_muted and not state._camera_disabled
    assert not state.camera_capture.mock_calls


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("sleeping", [False, True])
@pytest.mark.parametrize("sleep_owned_mute", [False, True])
def test_startup_open_switch_preserves_restored_sleep(monkeypatch, extended, sleeping,
                                                     sleep_owned_mute):
    # Only hardware, persistence and feedback are mocked, so a pipeline start cannot hide.
    cfg = PrivacyButtonConfig(disable_camera_on_mute=extended,
                              mute_speaker_on_mute=extended)
    for key, value in {
        "_sleeping": sleeping, "_mic_muted": True, "_speaker_muted": True,
        "_sleepy_auto_muted_mic": sleep_owned_mute,
        "_sleepy_auto_muted_speaker": sleep_owned_mute,
        "_mic_manual_override": not sleep_owned_mute, "_enrolling": False,
        "_clear_mic_muted_led": mock.Mock(),
    }.items():
        monkeypatch.setattr(state, key, value)
    privacy.prepare(cfg)
    with (
        mock.patch("hal.drivers.button_actions.single_click_action") as click,
        mock.patch("hal.drivers.button_actions.play_ack_chime") as chime,
        mock.patch("hal.drivers.button_actions.announce_listening_cue") as announce,
    ):
        PrivacyButtonHandler(cfg)._apply_state_locked(False, initial=True)
    assert state._hw_mic_switch_muted is False
    assert not privacy.mic_locked()
    assert state._sleeping is sleeping
    assert state._mic_muted is sleeping
    assert state._speaker_muted is True
    assert state._sleepy_auto_muted_mic is sleep_owned_mute
    assert state._sleepy_auto_muted_speaker is sleep_owned_mute
    if sleeping:
        state.voice_service.start.assert_not_called()
        assert state._mic_manual_override is (not sleep_owned_mute)
        state._clear_mic_muted_led.assert_not_called()
    else:
        state.voice_service.start.assert_called_once_with()
    click.assert_not_called()
    chime.assert_not_called()
    announce.assert_not_called()


def test_camera_failure_cannot_prevent_speaker_mute():
    state.camera_capture.stop.side_effect = RuntimeError("camera failed")
    privacy.apply(True, config())
    assert privacy.camera_muted and privacy.speaker_muted
    state.tts_service.stop.assert_called_once()
    state.music_service.stop.assert_called_once()


def test_software_cannot_unlock_or_take_snapshot():
    privacy.apply(True, config())
    for action in (camera.enable_camera, camera.camera_snapshot, music.unmute_speaker):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 409
    assert not state._auto_camera_on("wake")
    assert not state._auto_camera_off("scene")
    state.camera_capture.start.assert_not_called()


def test_manual_disable_during_lock_remains_after_unlock():
    privacy.apply(True, config())
    camera.disable_camera()
    music.mute_speaker()
    privacy.apply(False, config())
    assert state._camera_disabled and state._camera_manual_override and state._speaker_muted
    state.camera_capture.start.assert_not_called()


def test_guarded_camera_blocks_retained_frames_and_temporary_starts():
    raw = mock.Mock(last_frame="old frame", last_response="old response", last_frame_ts=12)
    guarded = privacy.GuardedCamera(raw)
    assert guarded.last_frame == "old frame"
    state.camera_capture = guarded
    privacy.apply(True, config())
    guarded.start()
    assert guarded.last_frame is None
    assert guarded.last_response is None
    assert guarded.last_frame_description is None
    assert guarded.last_frame_ts == 0
    assert guarded.capture() is None
    raw.start.assert_not_called()
    raw.capture.assert_not_called()
    assert raw.last_response is None
    privacy.apply(False, config())
    raw.start.assert_called_once()


def test_guarded_camera_drops_capture_if_muted_during_read():
    raw = mock.Mock()
    def capture():
        privacy.apply(True, config())
        return "frame"
    raw.capture.side_effect = capture
    assert privacy.GuardedCamera(raw).capture() is None


def test_sleep_wake_cannot_unmute_privacy_speaker(monkeypatch):
    monkeypatch.setattr(state, "_sleepy_auto_muted_speaker", True)
    monkeypatch.setattr(state, "_sleepy_auto_muted_mic", False)
    privacy.apply(True, config())
    state._wake_sleepy_peripherals()
    assert state._speaker_muted


@pytest.mark.parametrize("sleep_owned_mute", [False, True])
@pytest.mark.parametrize("unlock_before_wake", [False, True])
def test_restart_privacy_overlay_does_not_restore_sleep_mute_after_wake(
        monkeypatch, sleep_owned_mute, unlock_before_wake):
    monkeypatch.setattr(state, "_sleepy_auto_muted_speaker", sleep_owned_mute)
    monkeypatch.setattr(state, "_sleepy_auto_muted_mic", False)
    state._speaker_muted = True
    privacy.prepare(config())
    assert privacy.speaker_before is True
    if unlock_before_wake:
        privacy.apply(False, config())
    state._wake_sleepy_peripherals()
    if not unlock_before_wake:
        assert state._speaker_muted
        privacy.apply(False, config())
    assert state._speaker_muted is (not sleep_owned_mute)
    assert not state._sleepy_auto_muted_speaker
    state._save_boot_sidecar.assert_any_call(
        state._SPEAKER_STATE_PATH, {"muted": not sleep_owned_mute},
    )


@pytest.mark.parametrize("privacy_locked", [False, True])
def test_manual_speaker_mute_during_sleep_survives_wake(monkeypatch, privacy_locked):
    monkeypatch.setattr(state, "_sleeping", True)
    monkeypatch.setattr(state, "_sleepy_auto_muted_speaker", True)
    monkeypatch.setattr(state, "_sleepy_auto_muted_mic", False)
    state._speaker_muted = True
    if privacy_locked:
        privacy.apply(True, config())
    music.mute_speaker()
    assert not state._sleepy_auto_muted_speaker
    state._save_boot_sidecar.assert_any_call(state._SPEAKER_STATE_PATH, {"muted": True})
    state._sleeping = False
    state._wake_sleepy_peripherals()
    privacy.apply(False, config())
    assert state._speaker_muted


def test_extended_unlock_restores_after_shared_wake_without_unmuting_output():
    state._mic_muted = True
    state._speaker_muted = True
    handler = PrivacyButtonHandler(config())
    handler._apply_state_locked(True)
    with (
        mock.patch("hal.drivers.button_actions.single_click_action") as click,
        mock.patch("hal.drivers.button_actions.play_ack_chime"),
        mock.patch("hal.drivers.button_actions.announce_listening_cue"),
        mock.patch.object(state, "_clear_mic_muted_led"),
        mock.patch("hal.drivers.privacy_button.threading.Thread"),
    ):
        handler._apply_state_locked(False)
    click.assert_called_once_with("privacy-switch", announce=False, chime=False, unmute_output=False)
    assert not privacy.camera_muted and not privacy.speaker_muted
    assert state._speaker_muted


def test_direct_audio_consumers_respect_speaker_lock():
    from hal.drivers.voice.music_service import MusicService
    from hal.drivers.voice.tts.service import TTSService
    privacy.apply(True, config())
    state._speaker_muted = False
    assert TTSService._speaker_muted()
    player = MusicService.__new__(MusicService)
    assert player.play("song") is False
    assert player.play_file("/unused.wav") is False


@pytest.mark.parametrize("camera_option,speaker_option", [(True, False), (False, True)])
def test_optional_peripherals_are_independent(camera_option, speaker_option):
    cfg = PrivacyButtonConfig(disable_camera_on_mute=camera_option,
                              mute_speaker_on_mute=speaker_option)
    privacy.apply(True, cfg)
    assert state._camera_disabled == camera_option
    assert state._speaker_muted == speaker_option
    assert state.camera_capture.stop.call_count == int(camera_option)
    assert state.tts_service.stop.call_count == int(speaker_option)
    privacy.apply(False, cfg)
    assert not state._camera_disabled and not state._speaker_muted


def test_scene_release_cannot_reopen_privacy_peripherals(monkeypatch):
    from hal.routes import scene
    monkeypatch.setattr(state, "_active_scene", "night")
    monkeypatch.setattr(state, "animation_service", None)
    monkeypatch.setattr(state, "rgb_service", None)
    monkeypatch.setattr(state, "_save_user_led_state", mock.Mock())
    monkeypatch.setattr(scene, "_persist_scene", mock.Mock())
    state._hw_mic_switch_muted = True
    state._mic_muted = True
    privacy.apply(True, config())
    scene.deactivate_scene()
    assert state._mic_muted and state._speaker_muted and state._camera_disabled
    state.camera_capture.start.assert_not_called()
    state.voice_service.start.assert_not_called()


@pytest.mark.parametrize("camera_manual_override", [False, True])
def test_scene_release_inside_privacy_lock_reopens_scene_muted_peripherals(
        monkeypatch, camera_manual_override):
    """Scene mutes captured by the privacy lock do not survive scene off."""
    from hal.routes import scene
    monkeypatch.setattr(state, "_active_scene", "night")
    monkeypatch.setattr(state, "animation_service", None)
    monkeypatch.setattr(state, "rgb_service", None)
    monkeypatch.setattr(state, "_save_user_led_state", mock.Mock())
    monkeypatch.setattr(scene, "_persist_scene", mock.Mock())
    state._speaker_muted = True
    state._camera_disabled = True
    state._camera_manual_override = camera_manual_override
    monkeypatch.setattr(state, "_sleepy_auto_muted_speaker", False)
    monkeypatch.setattr(state, "_sleepy_auto_muted_mic", False)
    privacy.apply(True, config())
    assert privacy.speaker_before is True and privacy.camera_before is True
    state._wake_sleepy_peripherals()
    scene.deactivate_scene()
    assert state._speaker_muted and state._camera_disabled
    state.camera_capture.start.assert_not_called()
    state._save_boot_sidecar.assert_any_call(state._SPEAKER_STATE_PATH, {"muted": False})
    privacy.apply(False, config())
    assert not state._speaker_muted
    assert state._camera_disabled == camera_manual_override
    assert state.camera_capture.start.call_count == (0 if camera_manual_override else 1)


def test_scene_release_without_privacy_lock_unmutes_directly(monkeypatch):
    from hal.routes import scene
    monkeypatch.setattr(state, "_active_scene", "night")
    monkeypatch.setattr(state, "animation_service", None)
    monkeypatch.setattr(state, "rgb_service", None)
    monkeypatch.setattr(state, "_save_user_led_state", mock.Mock())
    monkeypatch.setattr(scene, "_persist_scene", mock.Mock())
    state._speaker_muted = True
    state._camera_disabled = True
    scene.deactivate_scene()
    assert not state._speaker_muted and not state._camera_disabled
    assert privacy.speaker_before is None and privacy.camera_before is None
    state.camera_capture.start.assert_called_once()
