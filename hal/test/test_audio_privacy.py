"""Mock regressions for HAL privacy boundaries."""

from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hal import app_state as state, privacy
from hal.routes import audio, speaker


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


@pytest.mark.parametrize('flag', ['_mic_muted', '_hw_mic_switch_muted'])
def test_recording_routes_refuse_muted_mic(monkeypatch, flag):
    monkeypatch.setattr(state, flag, True)
    with mock.patch.object(audio, 'sd') as sd, mock.patch.object(speaker, '_capture_enroll_wav') as capture:
        for call in (audio.record_audio,):
            with pytest.raises(HTTPException) as error:
                call()
            assert error.value.status_code == 409
        sd.rec.assert_not_called()
        capture.assert_not_called()
    state.voice_service.start.assert_not_called()


@pytest.mark.parametrize('gate', ['software', 'hardware'])
def test_tone_refuses_speaker_gate(monkeypatch, gate):
    monkeypatch.setattr(state, '_speaker_muted', gate == 'software')
    monkeypatch.setattr(privacy, 'speaker_muted', gate == 'hardware')
    with mock.patch.object(audio, 'sd') as sd, pytest.raises(HTTPException) as error:
        audio.play_tone()
    assert error.value.status_code == 409
    sd.play.assert_not_called()


@pytest.mark.parametrize('path', [
    '/audio/record?duration_ms=0', '/audio/record?duration_ms=30001',
    '/audio/play-tone?duration_ms=-1', '/audio/play-tone?duration_ms=5001',
    '/audio/play-tone?frequency=0', '/audio/play-tone?frequency=20001',
])
def test_audio_http_bounds(path):
    app = FastAPI()
    app.include_router(audio.router)
    with TestClient(app) as client:
        assert client.post(path).status_code == 422


def test_record_discards_audio_if_muted_during_capture(monkeypatch):
    sd = mock.Mock()
    sd.query_devices.return_value = {'default_samplerate': 16000}
    sd.wait.side_effect = lambda: monkeypatch.setattr(state, '_mic_muted', True)
    monkeypatch.setattr(audio, 'sd', sd)
    monkeypatch.setattr(audio, 'np', mock.Mock())
    with pytest.raises(HTTPException) as error:
        audio.record_audio()
    assert error.value.status_code == 409
    sd.rec.return_value.tobytes.assert_not_called()


@pytest.mark.parametrize("duration_ms", [1, 50, 100])
def test_valid_simulated_recording_still_returns_wav(monkeypatch, duration_ms):
    monkeypatch.setattr(state, 'simulation_audio', True)
    app = FastAPI()
    app.include_router(audio.router)
    with TestClient(app) as client:
        response = client.post(f'/audio/record?duration_ms={duration_ms}')
    assert response.status_code == 200
    assert response.content.startswith(b'RIFF')
