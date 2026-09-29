"""Text to Dialogue framing, errors, cleanup and local tempo."""
import base64
import json

import pytest

from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend
from hal.drivers.voice.tts.backend import TTSRateLimitError


@pytest.mark.parametrize('base,route', [
    ('https://api.elevenlabs.io/v1', '/v1/text-to-dialogue/stream-input'),
    ('https://proxy.example/v1', '/v1/ws/elevenlabs/text-to-dialogue/stream-input'),
])
def test_route(base, route, monkeypatch):
    monkeypatch.delenv('HAL_TTS_ELEVENLABS_WS_URL', raising=False)
    backend = ElevenLabsWSTTSBackend('test', base)
    assert route + '?' in backend._url_tmpl


def setup_socket(monkeypatch, messages):
    backend = ElevenLabsWSTTSBackend('test', 'https://proxy.example/v1')
    sent, closed, tempos = [], [], []

    class Socket:
        def send(self, data):
            sent.append(json.loads(data))

        def recv(self, timeout):
            assert timeout == 0.2
            return json.dumps(next(messages))

        def close(self):
            closed.append(True)

    def connect(url, **kwargs):
        assert kwargs['additional_headers'] == {
            'xi-api-key': 'test', 'Authorization': 'Bearer test',
        }
        assert 'model_id=eleven_v4_turbo' in url
        return Socket()

    backend._connect = connect

    def tempo(chunks, speed, rate, cancelled=None):
        tempos.append((speed, rate))
        yield from chunks

    monkeypatch.setattr('hal.drivers.voice.tts.elevenlabs_ws.change_tempo', tempo)
    return backend, sent, closed, tempos


def test_dialogue_and_final_audio(monkeypatch):
    backend, sent, closed, tempos = setup_socket(monkeypatch, iter([
        {'audio': base64.b64encode(b'\x01\x00').decode(), 'is_final_audio_for_turn': True},
    ]))
    assert b''.join(backend.stream_pcm('[crying] Hello', 'Rachel', '', 1.5)) == b'\x01\x00'
    voice = backend.VOICE_IDS['Rachel']
    assert sent == [
        {'voices': [voice], 'xi_api_key': 'test', 'voice_settings': {'speed': 1.0}},
        {'inputs': [{'text': '[crying] Hello', 'voice_id': voice}]},
        {'flush': True},
    ]
    assert closed == []
    backend.close()
    assert closed == [True]
    assert tempos == [(1.5, 24000)]


@pytest.mark.parametrize('error,exception', [('quota exceeded', TTSRateLimitError), ('invalid model', RuntimeError)])
def test_errors_close_socket(monkeypatch, error, exception):
    backend, _, closed, _ = setup_socket(monkeypatch, iter([{'error': error}]))
    with pytest.raises(exception):
        list(backend.stream_pcm('Hello', 'Rachel', '', 1.0))
    assert closed == [True]


def test_tags_only_skip_connection(monkeypatch):
    backend, sent, closed, _ = setup_socket(monkeypatch, iter([]))
    assert list(backend.stream_pcm('[crying]', 'Rachel', '', 1.0)) == []
    assert not sent and not closed


def test_reuses_session_between_sentences(monkeypatch):
    backend, sent, closed, _ = setup_socket(monkeypatch, iter([
        {'is_final_audio_for_turn': True}, {'is_final_audio_for_turn': True},
    ]))
    for text in ('First sentence.', 'Second sentence.'):
        list(backend.stream_pcm(text, 'Rachel', '', 1.0))
    assert sum('voices' in message for message in sent) == 1
    assert sum('flush' in message for message in sent) == 2
    assert not closed
    backend.close()


def test_abandoned_generator_discards_session(monkeypatch):
    backend, _, closed, _ = setup_socket(monkeypatch, iter([
        {'audio': base64.b64encode(b'\x01\x00').decode()},
    ]))
    stream = backend.stream_pcm('Hello', 'Rachel', '', 1.0)
    assert next(stream) == b'\x01\x00'
    stream.close()
    assert closed == [True]
    assert backend._ws is None


def test_cancelled_wait_discards_session(monkeypatch):
    backend, _, closed, _ = setup_socket(monkeypatch, iter([]))
    stopped = [False]
    original = backend._connect

    def connect(*args, **kwargs):
        socket = original(*args, **kwargs)
        def recv(timeout):
            stopped[0] = True
            raise TimeoutError()
        socket.recv = recv
        return socket

    backend._connect = connect
    assert list(backend.stream_pcm('Hello', 'Rachel', '', 1.0, cancelled=lambda: stopped[0])) == []
    assert closed == [True]


def test_reconnects_stale_session(monkeypatch):
    from websockets.exceptions import ConnectionClosedOK
    backend, sent, closed, _ = setup_socket(monkeypatch, iter([
        {'is_final_audio_for_turn': True}, {'is_final_audio_for_turn': True},
    ]))
    list(backend.stream_pcm('First', 'Rachel', '', 1.0))
    def broken_send(data):
        raise ConnectionClosedOK(None, None)
    backend._ws.send = broken_send
    list(backend.stream_pcm('Second', 'Rachel', '', 1.0))
    assert sum('voices' in message for message in sent) == 2
    assert closed == [True]
    backend.close()


def test_no_retry_after_partial_audio(monkeypatch):
    from websockets.exceptions import ConnectionClosedOK
    backend, _, closed, _ = setup_socket(monkeypatch, iter([]))
    original = backend._connect
    calls = []
    def connect(*args, **kwargs):
        calls.append(True)
        socket = original(*args, **kwargs)
        frames = iter([{'audio': base64.b64encode(b'\x01\x00').decode()}])
        def recv(timeout):
            try:
                return json.dumps(next(frames))
            except StopIteration:
                raise ConnectionClosedOK(None, None)
        socket.recv = recv
        return socket
    backend._connect = connect
    stream = backend.stream_pcm('Hello', 'Rachel', '', 1.0)
    assert next(stream) == b'\x01\x00'
    with pytest.raises(ConnectionClosedOK):
        next(stream)
    assert calls == [True]
    assert closed == [True]


def test_voice_change_registers_new_session(monkeypatch):
    backend, sent, closed, _ = setup_socket(monkeypatch, iter([
        {'is_final_audio_for_turn': True}, {'is_final_audio_for_turn': True},
    ]))
    list(backend.stream_pcm('Hello', 'Rachel', '', 1.0))
    list(backend.stream_pcm('Hello', 'other-voice-id', '', 1.0))
    registrations = [message['voices'] for message in sent if 'voices' in message]
    assert registrations == [[backend.VOICE_IDS['Rachel']], ['other-voice-id']]
    assert closed == [True]
    backend.close()
