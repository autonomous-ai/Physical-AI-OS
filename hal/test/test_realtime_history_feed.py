"""A reply the user never heard still has to reach the realtime agent.

os-server drops a cancelled turn's speech before it reaches TTS, and the normal
history feed rides on TTS completion — so without a speaker-free path the
realtime session keeps save_main_handoff's "its spoken reply follows"
placeholder and never learns the answer.
"""

from hal.drivers.voice.voice_service import VoiceService


class _RecordingRealtime:
    def __init__(self) -> None:
        self.fragments: list[str] = []
        self.sent: list[str] = []

    def save_main_agent_reply_fragment(self, text: str) -> None:
        self.fragments.append(text)

    def send_text(self, text: str) -> None:
        self.sent.append(text)


def _service() -> tuple[VoiceService, _RecordingRealtime]:
    realtime = _RecordingRealtime()
    service = object.__new__(VoiceService)
    service._realtime = realtime
    return service, realtime


def test_unspoken_reply_is_persisted_and_pushed_to_the_live_session():
    service, realtime = _service()

    assert service.feed_realtime_history("Your meeting starts at two.", spoken=False)

    assert realtime.fragments == ["Your meeting starts at two."]
    assert realtime.sent == ["[TTS HISTORY, not spoken] Your meeting starts at two."]


def test_spoken_reply_keeps_the_plain_marker():
    service, realtime = _service()

    service.feed_realtime_history("Your meeting starts at two.")

    assert realtime.sent == ["[TTS HISTORY] Your meeting starts at two."]


# The persisted fragment is the processed result and memory wants all of it;
# only the in-session line is capped, because that one is re-billed on every
# later turn until the session recycles.
def test_only_the_in_session_line_is_capped(monkeypatch):
    import hal.config as hal_config

    monkeypatch.setattr(hal_config, "REALTIME_TTS_HISTORY_MAX_CHARS", 10)
    service, realtime = _service()

    service.feed_realtime_history("abcdefghijklmnop", spoken=False)

    assert realtime.fragments == ["abcdefghijklmnop"]
    assert realtime.sent == ["[TTS HISTORY, not spoken] abcdefghij…"]


def test_empty_text_reaches_neither_sink():
    service, realtime = _service()

    assert not service.feed_realtime_history("", spoken=False)
    assert realtime.fragments == []
    assert realtime.sent == []


def test_partial_reply_does_not_claim_full_text_was_heard():
    service, realtime = _service()
    service.feed_realtime_history("The airplane is black.", spoken=False, interrupted=True)
    assert realtime.fragments == ["The airplane is black."]
    assert realtime.sent == [
        "[TTS HISTORY, interrupted; only part may have been heard] The airplane is black."
    ]
