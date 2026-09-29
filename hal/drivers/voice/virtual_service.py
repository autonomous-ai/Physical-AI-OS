"""No-network voice services used by the laptop simulator."""

from __future__ import annotations


class _VirtualBackend:
    available = True


class VirtualTTSService:
    available = True
    speaking = False

    def __init__(self, voice: str = "nova", instructions: str | None = None, **_):
        self._voice = voice
        self._instructions = instructions
        self._provider = "virtual"
        self._backend = _VirtualBackend()
        self._sd = None
        self.spoken: list[str] = []

    def speak(self, text: str, **_):
        self.spoken.append(text)
        return True

    def speak_cached(self, text: str, **_):
        return self.speak(text)

    def speak_queue(self, text: str, **_):
        return self.speak(text)

    def stop(self):
        self.speaking = False

    def release_stream(self):
        pass


class VirtualVoiceService:
    available = True
    listening = True

    def __init__(self, tts_service=None, **_):
        self._tts = tts_service
        self._music_service = None

    def start(self):
        self.listening = True

    def stop(self):
        self.listening = False

    def set_music_service(self, service):
        self._music_service = service

    def set_wake_words(self, _):
        pass
