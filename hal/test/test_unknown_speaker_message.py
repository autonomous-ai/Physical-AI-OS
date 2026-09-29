"""An unknown speaker's turn must not carry an order to ask for their name."""

from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator


def _decorator():
    return SpeakerDecorator(wake_words=["lamp"], nudge_cooldown_s=0, enable_people_perception=False)


def test_a_short_fragment_carries_no_instruction_to_speak():
    msg = _decorator()._format_unknown_speaker_message(
        "program.", "/tmp/v.wav", duration_s=0.4, voiceprint_hash="voice_460"
    )

    assert "ask" not in msg.lower()
    assert "/tmp/v.wav" in msg
    assert "voice_460" in msg


def test_an_ordinary_request_does_not_trigger_a_name_question():
    msg = _decorator()._format_unknown_speaker_message(
        "Let's discuss the review of the Codex agent code for autonomous OS.",
        "/tmp/v.wav",
        duration_s=4.0,
        voiceprint_hash="voice_461",
    )

    assert "else ask" not in msg
    assert "auto enroll" not in msg
    assert "Otherwise handle the user's request" in msg
    assert "audio save at /tmp/v.wav" in msg
    assert "[voice:voice_461]" in msg


def test_a_speaker_asked_recently_is_not_asked_again():
    d = _decorator()
    d._nudge_cooldown_s = 300
    long_turn = "hello there I am a brand new person in this house"
    first = d._format_unknown_speaker_message(
        long_turn, "/tmp/a.wav", duration_s=4.0, voiceprint_hash="voice_462"
    )
    second = d._format_unknown_speaker_message(
        long_turn, "/tmp/b.wav", duration_s=4.0, voiceprint_hash="voice_462"
    )

    assert "clear self-introduction" in first
    assert "ask" not in second.lower()
    assert "audio saved at /tmp/b.wav" in second


def test_self_introduction_keeps_audio_and_conditional_enrollment_guidance():
    transcript = "My name is Alex and I would like you to remember my voice."
    msg = _decorator()._format_unknown_speaker_message(
        transcript, "/tmp/alex.wav", duration_s=6.0, voiceprint_hash="voice_463"
    )
    assert transcript in msg
    assert "audio save at /tmp/alex.wav" in msg
    assert "[voice:voice_463]" in msg
    assert "clear self-introduction" in msg
