"""Mock regressions for HAL privacy boundaries."""

from unittest import mock

import pytest

from hal import app_state as state, privacy


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ('camera_muted', 'speaker_muted'):
        monkeypatch.setattr(privacy, name, False)
    for name, value in {
        '_mic_muted': False, '_hw_mic_switch_muted': None,
        '_speaker_muted': False, '_camera_disabled': False,
        '_camera_manual_override': False, 'simulation_audio': False,
        'safety_policy': None, 'voice_service': mock.Mock(_running=True),
        'camera_capture': mock.Mock(), 'tts_service': None, 'music_service': None,
        '_enrolling': False, 'audio_input_device': 1, 'animation_service': None,
    }.items():
        monkeypatch.setattr(state, name, value)


def test_hardware_only_switch_blocks_voice_start(monkeypatch):
    monkeypatch.setattr(state, '_hw_mic_switch_muted', True)
    assert privacy.mic_locked()
    assert not state.start_voice_service('test')
    state.voice_service.start.assert_not_called()


def test_scene_honors_hardware_only_switch(monkeypatch):
    from hal.routes import scene
    monkeypatch.setattr(state, '_hw_mic_switch_muted', True)
    monkeypatch.setattr(state, '_mic_muted', True)
    monkeypatch.setattr(state, 'rgb_service', mock.Mock())
    monkeypatch.setattr(state, 'sensing_service', None)
    monkeypatch.setattr(state, '_stop_current_effect', mock.Mock())
    monkeypatch.setattr(state, '_save_user_led_state', mock.Mock())
    monkeypatch.setattr(state, '_active_scene', None)
    monkeypatch.setattr(scene, '_persist_scene', mock.Mock())
    monkeypatch.setattr(scene, 'SCENE_PRESETS', {'reading': {'color': [255,255,255], 'brightness': 1, 'mic': 'on'}})
    scene.activate_scene(scene.SceneRequest(scene='reading'))
    assert state._mic_muted
    state.voice_service.start.assert_not_called()
