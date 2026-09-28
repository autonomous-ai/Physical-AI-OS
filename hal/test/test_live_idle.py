"""Provider progress must prevent idle hangup before ElevenLabs playback."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from hal.drivers.voice import voice_service as module
from hal.drivers.voice.voice_service import VoiceService
from hal.realtime.models.output import TextOutput
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401


def test_provider_text_renews_idle_window_before_first_sentence(monkeypatch, kpi):
    services = []
    original = VoiceService._live_out_pump
    def run(service, *args, **kwargs):
        services.append(service)
        return original(service, *args, **kwargs)
    monkeypatch.setattr(VoiceService, '_live_out_pump', run)
    _pump(monkeypatch, kpi, [([TextOutput(text='A still unfinished response', user_turn_id='u')], 'u', False)])
    service = services[0]
    progress = service._live_last_model_output
    assert progress > 0
    assert service._live_quiet_for(progress + 1, progress - 20, progress - 20) == 1
    assert service._live_quiet_for(progress + 16, progress - 20, progress - 20) == 16
    assert service._live_quiet_for(progress + 16, progress - 20, progress + 15) == 1


def test_cancelled_live_reader_does_not_speak_buffered_tail(monkeypatch, kpi):
    stop = threading.Event()
    spoken = _pump(monkeypatch, kpi, [([
        TextOutput(text='An unfinished sentence', user_turn_id='u'),
        stop.set,
    ], 'u', False)], stop_event=stop)
    assert spoken == []


def test_live_hangup_joins_cancelled_reader_before_releasing_session(monkeypatch):
    service = object.__new__(VoiceService)
    service._running = True
    service._live_generation = 0
    service._silence_vad = None
    service._np = np
    service._tts_is_speaking = lambda: False
    service._music_is_playing = lambda: False
    service._live_uplink_frame = lambda data: data
    service._to_realtime = lambda frame: frame
    service._live_stop_output = Mock()
    reader_started, reader_exited = threading.Event(), threading.Event()

    def pump(generation, mode, cues, opener, stop_event):
        reader_started.set()
        assert stop_event.wait(2), 'hangup did not cancel the blocked reader'
        reader_exited.set()

    def set_live_active(active):
        if not active:
            assert reader_exited.is_set(), 'released session while reader still owns queue'

    service._live_out_pump = pump
    service._realtime = SimpleNamespace(set_live_active=set_live_active,
                                       flush_output=Mock(), append_audio=Mock(),
                                       end_live_audio=Mock())

    class Mic:
        def read(self, size):
            assert reader_started.wait(1)
            service._running = False
            return np.zeros((size, 1), np.int16), False

    monkeypatch.setattr(module.voice_cfg, 'LIVE_UPLINK_DUMP_DIR', '')
    monkeypatch.setattr(module, 'LiveVoiceCues', Mock())
    monkeypatch.setattr(module.aec, 'active', lambda: False)
    service._live_session(Mic(), 320, 16000, [], harness_voice={'enabled': False})
    assert reader_exited.is_set()
    service._realtime.end_live_audio.assert_called_once()
    service._live_stop_output.assert_called_once()


def test_hardware_speech_near_idle_deadline_gets_bounded_transcript_grace(monkeypatch):
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_REQUIRES_TRANSCRIPT', True)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_HANGUP_S', 15)
    monkeypatch.setattr(module.hal_config, 'LIVE_VAD_SILENCE_MS', 1000)
    service = object.__new__(VoiceService)
    service._live_gate = object()
    service._live_idle_speech_deadline = 0.0
    # Same ordering as the device: confirmed speech, then the idle deadline,
    # then a delayed provider transcript. No session end may split that speech.
    assert service._live_idle_pending_speech(115.1, 15.1, 114.9)
    assert service._live_idle_pending_speech(116, 16, 115.5)
    # A constant noise candidate must not extend the original grace forever.
    assert not service._live_idle_pending_speech(130.2, 30.2, 130.1)
    # A provider transcript/output renews the real idle window.
    assert not service._live_idle_pending_speech(131, 1, 130.1)
    assert service._live_idle_speech_deadline == 0
    assert service._live_idle_pending_speech(146, 16, 145.9)


def test_idle_grace_requires_recent_hardware_speech(monkeypatch):
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_REQUIRES_TRANSCRIPT', True)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_HANGUP_S', 15)
    monkeypatch.setattr(module.hal_config, 'LIVE_VAD_SILENCE_MS', 1000)
    service = object.__new__(VoiceService)
    service._live_gate = object()
    service._live_idle_speech_deadline = 0.0
    assert not service._live_idle_pending_speech(116, 16, 100)
    # Speech just ended: allow the configured server silence plus delivery.
    assert service._live_idle_pending_speech(116, 16, 114.5)
    service._live_gate = None
    assert not service._live_idle_pending_speech(117, 17, 116.9)


def test_live_capture_keeps_uploading_speech_until_delayed_transcript(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        time=lambda: clock.now, monotonic=lambda: clock.now,
    ))
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_REQUIRES_TRANSCRIPT', True)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_IDLE_HANGUP_S', 15)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_MAX_S', 600)
    monkeypatch.setattr(module.voice_cfg, 'LIVE_UPLINK_DUMP_DIR', '')
    monkeypatch.setattr(module.voice_cfg, 'STT_RATE', 16000)
    monkeypatch.setattr(module.hal_config, 'LIVE_VAD_SILENCE_MS', 1000)
    mode = {'enabled': False}
    monkeypatch.setattr(module, 'read_voice_mode', lambda: mode)
    monkeypatch.setattr(module, 'LiveVoiceCues', Mock())
    monkeypatch.setattr(module.aec, 'active', lambda: False)
    monkeypatch.setattr(module.live_playback, 'duck', Mock())
    monkeypatch.setattr(module.live_playback, 'played_seconds', lambda: 0.0)
    monkeypatch.setattr(module.live_playback, 'level', lambda: 0.0)

    class Gate:
        speaking = False
        duck = False
        risk = False
        barge_in = False
        threshold = 0.01

        def reset(self):
            self.speaking = False

        def process(self, data, *args, **kwargs):
            self.speaking = 114.5 <= clock.now <= 116.5
            return data

    service = object.__new__(VoiceService)
    service._running = True
    service._live_generation = 0
    service._silence_vad = None
    service._np = np
    service._live_gate = Gate()
    service._aec_live_diag_next = float('inf')
    service._tts_is_speaking = lambda: False
    service._music_is_playing = lambda: False
    service._to_realtime = lambda frame: frame
    service._live_stop_output = Mock()
    service._live_out_pump = lambda generation, mode, cues, opener, stop: stop.wait(2)
    uploaded = []

    def append_audio(frame):
        uploaded.append((clock.now, frame))
        # Gemini delivers the utterance only after local speech has ended.
        if clock.now == 117.5:
            service._live_last_transcript_at = clock.now

    service._realtime = SimpleNamespace(
        set_live_active=Mock(), flush_output=Mock(),
        append_audio=append_audio, end_live_audio=Mock(),
    )
    times = [114.0, 114.5, 115.5, 116.5, 117.5, 118.5, 130.5, 131.0]

    class Mic:
        def read(self, size):
            clock.now = times[len(uploaded)]
            if clock.now == times[-1]:
                service._running = False
            return np.full((size, 1), len(uploaded) + 1, np.int16), False

    service._live_session(Mic(), 320, 16000, [], harness_voice=mode)

    assert [timestamp for timestamp, _ in uploaded] == times
    assert service._live_last_transcript_at == 117.5
    # Every captured frame, including the end of speech across the original
    # idle deadline, reaches the provider unchanged and in order.
    for index, (_, frame) in enumerate(uploaded, start=1):
        assert np.all(np.frombuffer(frame, dtype=np.int16) == index)
    service._realtime.end_live_audio.assert_called_once()
