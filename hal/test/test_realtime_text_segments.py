"""Text completion releases held speech without completing tool/routing work."""

from unittest.mock import Mock

import pytest

from hal.drivers.voice._internal import realtime_turn
from hal.drivers.voice.voice_service import VoiceService
from hal.realtime.models import TextOutput, TextSegmentEndOutput, UserSpeechOutput
from hal.realtime.models.signal import DelegateSignal, RejectSignal
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- shared fake clock


@pytest.mark.parametrize("chunks", [
    ["[giggle laugh warm] I'm right here."],
    ["[giggle la", "ugh wa", "rm] I'm right here."],
])
def test_live_combined_prefix_tag_stays_with_speech(monkeypatch, kpi, chunks):
    tts = Mock(_provider="elevenlabs", speaking=False)
    outputs = [UserSpeechOutput(turn_id="u-prefix")]
    outputs.extend(TextOutput(text=text, user_turn_id="u-prefix") for text in chunks)

    def not_spoken_yet():
        tts.speak.assert_not_called()
        tts.speak_queue.assert_not_called()

    outputs.extend([not_spoken_yet, TextSegmentEndOutput(user_turn_id="u-prefix")])
    _pump(monkeypatch, kpi, [(outputs, "u-prefix", False)], tts=tts,
          strip_markers=VoiceService.strip_rt_markers)
    assert tts.speak.call_count == 1
    assert tts.speak.call_args.args == ("[giggle laugh warm] I'm right here.",)
    tts.speak_queue.assert_not_called()


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("route", ["handled", "delegate", "reject"])
@pytest.mark.parametrize("parts", [[" [giggle]"], [" [giggle laugh warm]"], [" [giggle la", "ugh warm]"]])
def test_turn_flushes_late_tags_before_routing_terminal(monkeypatch, busy, route, parts):
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "gemini")
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    tts = Mock(_provider="elevenlabs")
    tts.speak.return_value = not busy
    provider = Mock(available=True, execution_completed=False)

    def output():
        yield TextOutput(text="I'm right here.")
        for part in parts:
            yield TextOutput(text=part)
        tts.speak.assert_not_called()
        yield TextSegmentEndOutput()
        tts.speak.assert_called_once_with("I'm right here." + "".join(parts), turn_id="vi-segment", realtime_reply=True)
        assert tts.speak_queue.call_count == int(busy)
        yield TextSegmentEndOutput()  # Duplicate metadata is not duplicate speech.
        if route == "delegate":
            yield DelegateSignal(message="Check my memory.")
        elif route == "reject":
            yield RejectSignal()

    provider.stream_output.return_value = output()
    result = realtime_turn.run_realtime_turn(
        provider, tts, VoiceService.strip_rt_markers, "Can you hear me over there?",
        [object()], 2, interaction_id="vi-segment", harness_followup=False,
    )
    assert result.route == {"delegate": realtime_turn.ROUTE_DELEGATED,
                            "reject": realtime_turn.ROUTE_AI_REJECTED,
                            "handled": realtime_turn.ROUTE_HANDLED}[route]
    if route == "reject":
        tts.stop_realtime_reply.assert_called_once_with(turn_id="vi-segment")
    else:
        tts.stop_realtime_reply.assert_not_called()
    assert not result.execution_completed
    assert tts.speak.call_count == 1
    assert tts.speak_queue.call_count == int(busy)


@pytest.mark.parametrize("suppressed", [False, True])
@pytest.mark.parametrize("parts", [[" [giggle]"], [" [giggle laugh warm]"], [" [giggle la", "ugh warm]"]])
def test_live_segment_flush_preserves_owner_and_tags(monkeypatch, kpi, suppressed, parts):
    tts = Mock(_provider="elevenlabs", speaking=False)
    tts.speak.return_value = True
    tts.speak_queue.return_value = True
    outputs = [UserSpeechOutput(turn_id="u-segment"),
               TextOutput(text="I'm right here.", user_turn_id="u-segment")]
    outputs.extend(TextOutput(text=part, user_turn_id="u-segment") for part in parts)
    outputs.append(TextSegmentEndOutput(user_turn_id="old-reply"))

    def before_boundary():
        tts.speak.assert_not_called()
        tts.speak_queue.assert_not_called()

    outputs.append(before_boundary)
    if suppressed:
        outputs.append(RejectSignal(user_turn_id="u-segment"))
    outputs.extend([TextSegmentEndOutput(user_turn_id="u-segment"),
                    TextSegmentEndOutput(user_turn_id="u-segment")])

    def after_boundary():
        spoken = tts.speak.call_args_list + tts.speak_queue.call_args_list
        assert len(spoken) == (0 if suppressed else 1)
        if spoken:
            assert spoken[0].args == ("I'm right here." + "".join(parts),)
            assert spoken[0].kwargs["realtime_reply"] is True
            assert spoken[0].kwargs["turn_id"]

    outputs.append(after_boundary)
    _pump(monkeypatch, kpi, [(outputs, "u-segment", False)], tts=tts,
          strip_markers=VoiceService.strip_rt_markers)
    after_boundary()


@pytest.mark.parametrize("text", ["", "[giggle]", "<no speech>", "<no spe"])
def test_live_boundary_is_not_speech_or_execution(monkeypatch, kpi, text):
    tts = Mock(_provider="elevenlabs", speaking=False)
    outputs = [UserSpeechOutput(turn_id="u-silent"),
               TextOutput(text=text, user_turn_id="u-silent"),
               TextSegmentEndOutput(user_turn_id="u-silent")]
    _pump(monkeypatch, kpi, [(outputs, "u-silent", False)], tts=tts,
          strip_markers=VoiceService.strip_rt_markers)
    tts.speak.assert_not_called()
    tts.speak_queue.assert_not_called()
