"""Language-switching STT: open in the predicted language, swap on confident LID."""

import struct
import threading

from hal.drivers.voice.lang_id import LanguageGuess
from hal.drivers.voice.stt.lang_switch import LanguageSwitchingSTT
from hal.drivers.voice.stt.provider import STTProvider, STTSession

QUARTER = 4000  # samples in 0.25 s at 16 kHz
SPEECH = struct.pack("<h", 3000) * QUARTER
SILENCE = b"\x00\x00" * QUARTER


class FakeSession(STTSession):
    def __init__(self, language, connect_ok=True):
        self.language = language
        self.connect_ok = connect_ok
        self.audio = bytearray()
        self.cb = None
        self.closed = threading.Event()

    def start(self, on_transcript):
        self.cb = on_transcript
        return self.connect_ok

    def send_audio(self, data):
        self.audio += data

    def close(self):
        self.closed.set()

    def is_closed(self):
        return self.closed.is_set()


class FakeProvider(STTProvider):
    def __init__(self, connect_ok=True):
        self.sessions = []
        self.connect_ok = connect_ok

    def create_session(self, language=None):
        session = FakeSession(language, connect_ok=self.connect_ok or not self.sessions)
        self.sessions.append(session)
        return session

    @property
    def available(self):
        return True


class FakeIdentifier:
    """Returns scripted (language, probability[, in_set]) guesses; repeats the last."""

    languages = ["en", "ja"]

    def __init__(self, *guesses):
        self.guesses = list(guesses)
        self.calls = []

    def warm_up(self):
        return True

    def identify(self, pcm):
        seconds = len(pcm) / 32000
        self.calls.append(seconds)
        guess = self.guesses.pop(0) if len(self.guesses) > 1 else self.guesses[0]
        language, probability, *rest = guess
        return LanguageGuess(language, probability, seconds, rest[0] if rest else 1.0)


def make(identifier, provider=None, **kwargs):
    provider = provider or FakeProvider()
    kwargs.setdefault("checkpoints_s", (1.0, 2.0, 3.0))
    return provider, LanguageSwitchingSTT(provider, identifier, "en", **kwargs)


def settle(session):
    for name in ("_lid_thread", "_swap_thread", "_lid_thread"):
        thread = getattr(session, name)
        if thread is not None:
            thread.join()


def feed(session, seconds, chunk=SPEECH):
    for _ in range(int(seconds * 4)):
        session.send_audio(chunk)
        settle(session)


def open_session(stt, listener=None):
    session = stt.create_session()
    if listener is not None:
        session.set_switch_listener(listener)
    received = []
    session.start(lambda text, final: received.append(text))
    return session, received


def test_same_language_keeps_single_session():
    provider, stt = make(FakeIdentifier(("en", 0.95)))
    session, _ = open_session(stt)
    feed(session, 3)
    session.close()
    assert len(provider.sessions) == 1
    assert session.language == "en"


def test_confident_disagreement_swaps_and_drops_old_transcripts():
    provider, stt = make(FakeIdentifier(("ja", 0.95)))
    resets = []
    session, received = open_session(stt, lambda old, new: resets.append((old, new)))
    old = provider.sessions[0]
    feed(session, 1.5)
    assert resets == [("en", "ja")]
    new = provider.sessions[1]
    assert new.language == "ja"
    old.cb("hello", True)
    new.cb("こんにちは", True)
    assert received == ["こんにちは"]
    old.closed.wait(1)
    assert old.is_closed()


def test_silence_never_triggers_identification():
    identifier = FakeIdentifier(("ja", 0.99))
    provider, stt = make(identifier)
    session, _ = open_session(stt)
    feed(session, 5, SILENCE)
    assert identifier.calls == []
    assert len(provider.sessions) == 1


def test_out_of_set_audio_never_switches():
    # Noise or another language: the restricted guess is confident but forced.
    provider, stt = make(FakeIdentifier(("ja", 0.93, 0.05)))
    session, _ = open_session(stt)
    feed(session, 4)
    assert len(provider.sessions) == 1
    assert stt.predicted_language() == "en"


def test_unsure_guess_does_not_switch_before_final_checkpoint():
    provider, stt = make(FakeIdentifier(("ja", 0.6)))
    session, _ = open_session(stt)
    feed(session, 2.5)
    assert len(provider.sessions) == 1
    # The last checkpoint trusts the top guess (final_probability=0).
    feed(session, 1)
    assert [s.language for s in provider.sessions] == ["en", "ja"]


def test_identification_stops_after_final_checkpoint():
    identifier = FakeIdentifier(("en", 0.5))
    provider, stt = make(identifier)
    session, _ = open_session(stt)
    feed(session, 6)
    assert identifier.calls == [1.0, 2.0, 3.0]


def test_each_utterance_is_identified_in_a_long_session():
    # Live mode: one STT session spans turns; a silence gap starts a new utterance.
    identifier = FakeIdentifier(("en", 0.95), ("ja", 0.95))
    provider, stt = make(identifier)
    session, _ = open_session(stt)
    feed(session, 1.5)
    feed(session, 1.25, SILENCE)
    feed(session, 1.5)
    assert [s.language for s in provider.sessions] == ["en", "ja"]
    # Without a listener only the current utterance (plus pre-roll) is replayed.
    replayed = len(provider.sessions[1].audio) / 32000
    assert 1.0 <= replayed <= 2.0


def test_listener_owner_gets_whole_session_replayed():
    provider, stt = make(FakeIdentifier(("en", 0.6), ("ja", 0.95)))
    session, _ = open_session(stt, lambda old, new: None)
    feed(session, 1.25)
    feed(session, 1.25, SILENCE)
    feed(session, 1.5)
    assert provider.sessions[-1].language == "ja"
    assert len(provider.sessions[-1].audio) / 32000 >= 4.0


def test_confident_language_becomes_next_prediction():
    provider, stt = make(FakeIdentifier(("ja", 0.95)))
    session, _ = open_session(stt)
    feed(session, 1.5)
    session.close()
    assert stt.create_session().language == "ja"


def test_failed_replacement_keeps_original_session():
    provider, stt = make(FakeIdentifier(("ja", 0.95)), FakeProvider(connect_ok=False))
    session, _ = open_session(stt)
    feed(session, 1.5)
    assert session.language == "en"
    session.close()
    assert stt.predicted_language() == "en"


def test_explicit_language_bypasses_identification():
    provider, stt = make(FakeIdentifier(("ja", 0.95)))
    session = stt.create_session("vi")
    assert isinstance(session, FakeSession) and session.language == "vi"
