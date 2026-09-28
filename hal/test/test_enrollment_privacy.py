"""Mock regressions for HAL privacy boundaries."""

from unittest import mock
from pathlib import Path
import stat

import pytest
from fastapi import HTTPException

from hal import app_state as state, privacy
from hal.routes import speaker


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
def test_enrollment_mute_during_capture_does_not_restart(monkeypatch, flag):
    monkeypatch.setattr(speaker.time, 'sleep', lambda _: None)
    def capture(*_):
        monkeypatch.setattr(state, flag, True)
    monkeypatch.setattr(speaker, '_capture_enroll_wav', capture)
    with pytest.raises(HTTPException) as error:
        speaker.speaker_record_enroll(speaker.RecordEnrollRequest(name='tester'))
    assert error.value.status_code == 409
    state.voice_service.start.assert_not_called()
    assert not state._enrolling


@pytest.mark.parametrize('flag', ['_mic_muted', '_hw_mic_switch_muted'])
def test_enrollment_refuses_muted_mic(monkeypatch, flag):
    monkeypatch.setattr(state, flag, True)
    with mock.patch.object(speaker, '_capture_enroll_wav') as capture:
        with pytest.raises(HTTPException) as error:
            speaker.speaker_record_enroll(speaker.RecordEnrollRequest(name='tester'))
        assert error.value.status_code == 409
        capture.assert_not_called()
    state.voice_service.start.assert_not_called()


def test_enrollment_temp_file_is_private_and_name_independent(monkeypatch, tmp_path):
    monkeypatch.setattr(speaker.time, 'sleep', lambda _: None)
    original_mkstemp = speaker.tempfile.mkstemp
    monkeypatch.setattr(speaker.tempfile, 'mkstemp',
                        lambda **kwargs: original_mkstemp(dir=tmp_path, **kwargs))
    captured = []

    def capture(path, _duration):
        wav = Path(path)
        captured.append(wav)
        assert wav.parent == tmp_path
        assert stat.S_IMODE(wav.stat().st_mode) == 0o600
        assert wav.name.startswith('voice-enroll-')
        # An error after opening the file must still clean up and restore voice.
        raise HTTPException(503, 'mock capture failure')

    monkeypatch.setattr(speaker, '_capture_enroll_wav', capture)
    with pytest.raises(HTTPException) as error:
        speaker.speaker_record_enroll(speaker.RecordEnrollRequest(name='../../outside'))
    assert error.value.status_code == 503
    assert len(captured) == 1
    assert not captured[0].exists()
    assert not state._enrolling
    assert not state._speaker_muted
    state.voice_service.start.assert_called_once()


def test_enrollment_temp_creation_failure_restores_state(monkeypatch):
    monkeypatch.setattr(speaker.time, 'sleep', lambda _: None)
    with mock.patch.object(speaker.tempfile, 'mkstemp', side_effect=OSError('no space')):
        with mock.patch.object(speaker, '_capture_enroll_wav') as capture:
            with pytest.raises(HTTPException) as error:
                speaker.speaker_record_enroll(speaker.RecordEnrollRequest(name='tester'))
    assert error.value.status_code == 500
    capture.assert_not_called()
    assert not state._enrolling
    assert not state._speaker_muted
    state.voice_service.start.assert_called_once()
