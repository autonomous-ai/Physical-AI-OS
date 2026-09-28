"""Mock regressions for HAL privacy boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import mock

import pytest
from fastapi import HTTPException

from hal import app_state as state, privacy
from hal.routes import camera


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


def test_snapshot_refuses_manual_disable(monkeypatch):
    monkeypatch.setattr(state, '_camera_disabled', True)
    monkeypatch.setattr(state, '_camera_manual_override', True)
    with pytest.raises(HTTPException) as error:
        camera.camera_snapshot()
    assert error.value.status_code == 409
    state.camera_capture.start.assert_not_called()


def test_concurrent_auto_disabled_snapshots_do_not_stop_each_other(monkeypatch):
    from hal.drivers.camera import video_capture_device
    monkeypatch.setattr(state, '_camera_disabled', True)
    monkeypatch.setattr(camera, 'cv2', mock.Mock())
    camera.cv2.imencode.return_value = (True, mock.Mock(tobytes=lambda: b'jpeg'))
    entered, release = Event(), Event()
    calls = []
    def capture(*_, **__):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            state.camera_capture.stop.assert_not_called()
        return mock.Mock()
    monkeypatch.setattr(video_capture_device, 'capture_still', capture)
    def snapshot():
        return camera.camera_snapshot(width=None, height=None, quality=85)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(snapshot)
        assert entered.wait(3)
        second = pool.submit(snapshot)
        release.set()
        assert first.result().body == b'jpeg'
        assert second.result().body == b'jpeg'
    assert state.camera_capture.start.call_count == 2
    assert state.camera_capture.stop.call_count == 2


def test_manual_disable_during_snapshot_discards_frame(monkeypatch):
    from hal.drivers.camera import video_capture_device
    monkeypatch.setattr(camera, 'cv2', mock.Mock())
    def capture(*_, **__):
        monkeypatch.setattr(state, '_camera_disabled', True)
        monkeypatch.setattr(state, '_camera_manual_override', True)
        return mock.Mock()
    monkeypatch.setattr(video_capture_device, 'capture_still', capture)
    with pytest.raises(HTTPException) as error:
        camera.camera_snapshot(width=None, height=None, quality=85)
    assert error.value.status_code == 409
    camera.cv2.imencode.assert_not_called()


def test_disable_auto_paused_camera_persists_manual_override(monkeypatch):
    monkeypatch.setattr(state, '_camera_disabled', True)
    persist = mock.Mock()
    monkeypatch.setattr(state, '_persist_camera_state', persist)
    assert camera.disable_camera() == {'status': 'already_disabled'}
    assert state._camera_disabled and state._camera_manual_override
    persist.assert_called_once_with()
    with pytest.raises(HTTPException) as error:
        camera.camera_snapshot()
    assert error.value.status_code == 409
    state.camera_capture.start.assert_not_called()
    state.camera_capture.stop.assert_not_called()
