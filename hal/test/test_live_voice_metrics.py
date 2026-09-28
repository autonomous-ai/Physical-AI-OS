"""Live pump and tracker integration with fake provider/audio, never a device."""

from types import SimpleNamespace
import time
import threading
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from hal import config
from hal.drivers.voice.voice_service import VoiceService
from hal.realtime.models.output import (
    AudioOutput, ExecutionOutput, InterruptedOutput, TextOutput, UserSpeechOutput,
)
from hal.realtime.models.signal import DelegateSignal, RejectSignal
from hal.telemetry import voice_metrics
from hal.telemetry.live_voice import LiveVoiceMetrics
from hal.test.test_voice_metrics import FakeTTS, kpi  # noqa: F401 -- fake clock/transport


def _pump(monkeypatch, kpi, batches, *, native=False, stop_delay_ms=0, harness_voice=None, sender=None, cues=None, main_reply=False, addressed=True, focus=None, opener=None, tts=None, strip_markers=None, stop_event=None):
    monkeypatch.setattr(config, "REALTIME_NATIVE_AUDIO", native)
    service = object.__new__(VoiceService)
    service._wakeword_focus = focus
    service._live_running = True
    service._live_generation = 1
    service._live_unprompted_replies = 0
    service._live_emotion_addressed = lambda text, harness: addressed
    service._decorator = SimpleNamespace(classify_wake_word=lambda text: (text, "voice"))
    service._sensing_sender = sender if sender is not None else object()
    service.strip_rt_markers = strip_markers or (lambda text: text)
    spoken = []
    batches = iter(batches)

    class Provider:
        output_sample_rate = 24000
        execution_completed = False
        execution_turn_id = ""

        def stream_output(self, *, stop_event=None):
            self.execution_completed = False
            self.execution_turn_id = ""
            batch = next(batches, None)
            if batch is None:
                service._live_running = False
                return
            outputs, key, completed = batch
            for output in outputs:
                if callable(output):
                    output()
                else:
                    yield output
            self.execution_completed = completed
            self.execution_turn_id = key

        def save_main_handoff(self, transcript):
            pass

    class Speaker:
        owner = ""
        realtime_feedback = main_reply
        realtime_speaking = not main_reply
        speaking = main_reply

        def speak(self, text, *, turn_id="", realtime_reply=False):
            spoken.append((text, turn_id))
            voice_metrics.playback_audio(
                "run:" + turn_id if turn_id else "",
                SimpleNamespace(realtime_reply=realtime_reply),
            )
            return True

        speak_queue = speak

        def stop(self, **kwargs):
            spoken.append(("__stop__", ""))
            kpi.clock.advance(stop_delay_ms)
            voice_metrics.playback_end()

        def stop_realtime_reply(self, *, turn_id=""):
            # Fake playback is instantaneous; no queued audio to cancel here.
            pass

        def native_play_begin(self, rate, owner=""):
            self.owner = owner
            spoken.append(("native", owner))
            return True

        def native_play_frame(self, audio):
            voice_metrics.playback_audio(self.owner, SimpleNamespace(native_mode=True))

        def set_native_playback_owner(self, owner):
            self.owner = owner

        def native_play_end(self, transcript=""):
            voice_metrics.playback_end()

    service._realtime = Provider()
    service._tts = tts if tts is not None else Speaker()
    service._live_out_pump(1, harness_voice=harness_voice, cues=cues, stop_event=stop_event, **({"opener": opener} if opener is not None else {}))
    return spoken


@pytest.mark.parametrize("chunks", [
    ["I'm right here! [cheerfully]"],
    ["I'm right here! [cheer", "fully]"],
    ["I'm right here! [HW:/led/off:{}]"],
])
def test_live_tagged_sentence_speaks_before_terminal(monkeypatch, kpi, chunks):
    from unittest.mock import Mock

    tts = Mock(speaking=False)

    def check_before_terminal():
        tts.speak_queue.assert_called_once()
        assert tts.speak_queue.call_args.args == ("I'm right here!",)
        assert tts.speak_queue.call_args.kwargs["realtime_reply"] is True

    outputs = [UserSpeechOutput(turn_id="u-tag")]
    outputs.extend(TextOutput(text=text, user_turn_id="u-tag") for text in chunks)
    outputs.append(check_before_terminal)
    _pump(monkeypatch, kpi, [(outputs, "u-tag", True)], tts=tts,
          strip_markers=VoiceService.strip_rt_markers)
    tts.speak_queue.assert_called_once()


def test_transcript_only_turn_counts_execution_without_fake_latency(monkeypatch, kpi):
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        TextOutput(text="Done.", user_turn_id="u1"),
    ], "u1", True)])
    kpi.close_all()
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert row["mode"] == "live"
    assert row["eligible"] is False
    assert row["exclusion_reason"] == "speech_endpoint_unavailable"
    assert row["ack_latency_ms"] is None
    assert row["answer_latency_ms"] is None
    assert row["task_eligible"] and row["task_eligibility_known"]
    terminal = kpi.one("voice_metrics_task_execution")
    assert terminal["interaction_id"] == row["interaction_id"]
    assert terminal["outcome"] == "completed"


def test_gemini_receive_time_is_not_speech_end_for_kpi(monkeypatch, kpi):
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1", endpoint_at=kpi.clock(), method="server_vad_receive"),
        TextOutput(text="Done.", user_turn_id="u1"),
    ], "u1", True)])
    kpi.close_all()
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert row["ack_kind"] == "realtime_tts"
    assert row["ack_latency_ms"] is None
    assert row["exclusion_reason"] == "speech_endpoint_unavailable"
    coverage = kpi.one("voice_metrics_live_coverage")
    assert coverage["endpoint_receive_only_observations"] == 1


def test_look_filler_pins_old_owner_and_unknown_never_uses_newest(monkeypatch, kpi):
    from hal.drivers.tracking.aim import _say, filler_ownership
    import requests

    payloads = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: payloads.append(kwargs["json"]))
    metrics = LiveVoiceMetrics()
    old = metrics.speech("old", kpi.clock(), "local_vad")
    with filler_ownership(voice_metrics.provider_interaction("old")):
        new = metrics.speech("new", kpi.clock(), "local_vad")
        _say("look_searching")
    _say("look_found")
    assert payloads == [{"pool": "look_searching", "owner": old}, {"pool": "look_found"}]
    assert voice_metrics.current_interaction() == new


def test_known_endpoint_native_playback_and_duplicate_input_share_one_turn(monkeypatch, kpi):
    at = kpi.clock()
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1", endpoint_at=at, method="server_vad"),
        UserSpeechOutput(turn_id="u1"),
        AudioOutput(audio=np.zeros(32, dtype=np.float32), user_turn_id="u1"),
    ], "u1", True)], native=True)
    kpi.close_all()
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert row["eligible"] is True
    assert row["ack_kind"] == "native_realtime"
    assert row["ack_latency_ms"] == 0
    assert len(kpi.of("voice_metrics_task_execution")) == 1


def test_timeout_keeps_identity_and_cannot_complete_execution(monkeypatch, kpi):
    _pump(monkeypatch, kpi, [
        ([UserSpeechOutput(turn_id="u1")], "", False),
        ([TextOutput(text="Finished.", user_turn_id="u1")], "u1", True),
        ([], "u1", True),  # Duplicate provider terminal is idempotent.
    ])
    kpi.close_all()
    assert len(kpi.of(voice_metrics.EVENT_INTERACTION)) == 1
    assert len(kpi.of("voice_metrics_task_execution")) == 1


def test_synthetic_audio_reset_is_not_a_suppression_or_failure(monkeypatch, kpi):
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        InterruptedOutput(reason="output_reset", user_turn_id="u1"),
        TextOutput(text="Done.", user_turn_id="u1"),
    ], "u1", True)])
    kpi.close_all()
    assert not kpi.of(voice_metrics.EVENT_SUPPRESSION)
    assert kpi.one("voice_metrics_task_execution")["outcome"] == "completed"


def test_barge_in_observes_stale_audio_without_changing_stop_or_chunking(monkeypatch, kpi):
    at = kpi.clock()
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="old", endpoint_at=at, method="server_vad"),
        TextOutput(text="Old answer.", user_turn_id="old"),
        TextOutput(text=" unfinished tail", user_turn_id="old"),
        UserSpeechOutput(turn_id="new", endpoint_at=at, method="server_vad"),
        InterruptedOutput(reason="server_interrupt", at=at, user_turn_id="old"),
        lambda: kpi.clock.advance(3000),
        TextOutput(text="New answer.", user_turn_id="new"),
    ], "new", True)], stop_delay_ms=3000)
    kpi.close_all()
    assert all(text != "__stop__" for text, _ in spoken)
    assert spoken[-1] == (" unfinished tailNew answer.", "")
    row = kpi.one(voice_metrics.EVENT_SUPPRESSION)
    assert row["suppression_reason"] == "server_barge_in"
    assert row["applicable_interactions"] == 1
    assert row["stale_observed"] is True
    assert len(kpi.of("voice_metrics_task_execution")) == 1


def test_delegate_preserves_interaction_and_has_no_realtime_success(monkeypatch, kpi):
    forwarded = []
    routing = {"enabled": False, "generation": "route-generation"}
    monkeypatch.setattr(
        "hal.drivers.voice.voice_service.dispatch_turn",
        lambda *args, **kwargs: forwarded.append(kwargs),
    )
    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        DelegateSignal(user_turn_id="u1", message="Search this", transcript="Search this"),
    ], "u1", True)], harness_voice=routing)
    kpi.close_all()
    assert forwarded[0]["interaction_id"] == kpi.one(voice_metrics.EVENT_INTERACTION)["interaction_id"]
    assert forwarded[0]["harness_voice"] == routing
    assert not kpi.of("voice_metrics_task_execution")


def test_rejection_and_unowned_reply_do_not_inflate_completion(monkeypatch, kpi):
    _pump(monkeypatch, kpi, [
        ([TextOutput(text="Unsolicited.")], "", True),
        ([UserSpeechOutput(turn_id="noise"), RejectSignal(user_turn_id="noise")], "", False),
    ])
    kpi.close_all()
    assert not kpi.of("voice_metrics_task_execution")
    assert kpi.one(voice_metrics.EVENT_INTERACTION)["task_eligible"] is False
    coverage = kpi.one("voice_metrics_live_coverage")
    assert coverage["unowned_output_chunks"] == 1
    assert coverage["unowned_completions"] == 1


def test_endpoint_can_upgrade_before_playback_but_never_after(kpi):
    metrics = LiveVoiceMetrics()
    first = metrics.speech("first", None, "provider_transcript")
    kpi.clock.advance(100)
    metrics.speech("first", kpi.clock(), "server_vad")
    kpi.clock.advance(400)
    voice_metrics.playback_audio("interaction:" + first, FakeTTS(realtime_feedback=True))
    voice_metrics.playback_end()
    second = metrics.speech("second", None, "provider_transcript")
    voice_metrics.playback_audio("interaction:" + second, FakeTTS(realtime_feedback=True))
    kpi.clock.advance(100)
    metrics.speech("second", kpi.clock(), "server_vad")
    kpi.close_all()
    rows = {e["params"]["interaction_id"]: e["params"]
            for e in kpi.of(voice_metrics.EVENT_INTERACTION)}
    assert rows[first]["ack_latency_ms"] == 400
    assert rows[first]["eligible"] is True
    assert rows[second]["ack_latency_ms"] is None
    assert rows[second]["eligible"] is False


def test_live_execution_report_counts_missing_endpoint_and_pending(kpi):
    # The reporter is a standalone repository script, not hal.scripts. Load
    # its path explicitly so this test also works with hal/ as the working dir.
    spec = importlib.util.spec_from_file_location(
        "live_metrics_reporter",
        Path(__file__).resolve().parents[2] / "scripts/report_voice_task_metrics.py",
    )
    reporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reporter)
    metrics = LiveVoiceMetrics()
    metrics.speech("complete", None, "provider_transcript")
    metrics.complete("complete", True)
    metrics.speech("pending", None, "provider_transcript")
    metrics.complete("pending", False)
    metrics.speech("rejected", None, "provider_transcript")
    metrics.reject("rejected")
    kpi.close_all()
    rows = [{"event_name": event["name"], "params": event["params"],
             "event_id": event["event_id"]} for event in kpi.events]
    result = reporter.report(rows, now_ms=int(time.time() * 1000) + 1000, default_device="test")
    totals = result["aggregate"]
    assert totals["eligible_mature_turns"] == 2
    assert totals["completed_turns"] == 1
    assert totals["failed_turns"] == 0
    assert totals["incomplete_turns"] == 1
    assert totals["ineligible_turns"] == 1
    assert totals["completion_pct"] == 50


def test_next_question_after_finished_silent_reply_is_not_suppression(kpi):
    metrics = LiveVoiceMetrics()
    iid = metrics.speech("done", kpi.clock(), "server_vad")
    voice_metrics.playback_audio("interaction:" + iid, FakeTTS(realtime_feedback=True))
    voice_metrics.playback_end()
    metrics.complete("done", True)
    metrics.interrupt("done")
    kpi.close_all()
    assert not kpi.of(voice_metrics.EVENT_SUPPRESSION)


def test_input_and_interruption_metadata_do_not_block_model_rejection(monkeypatch):
    from hal.test.test_realtime_rejection_filter import _orchestrator_for_reject
    from hal.realtime.models import FunctionCallOutput

    monkeypatch.setattr(config, "REALTIME_SESSION_MAX_TURNS", 0)
    orchestrator, agent = _orchestrator_for_reject()

    def receive(stop_on_done=True):
        yield InterruptedOutput(reason="server_interrupt", user_turn_id="old")
        yield UserSpeechOutput(turn_id="noise")
        yield FunctionCallOutput(
            name="reject_turn", arguments="{}", call_id="r", user_turn_id="noise",
        )

    agent.receive = receive
    outputs = list(orchestrator.stream_output())
    assert outputs[-1] == RejectSignal(user_turn_id="noise")
    assert agent.end_turn_calls == 1


def test_metric_exception_cannot_prevent_speech(monkeypatch, kpi):
    def broken_tracker(*args, **kwargs):
        raise RuntimeError("metric unavailable")

    monkeypatch.setattr(voice_metrics, "speech_end", broken_tracker)
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"), TextOutput(text="Still speaking.", user_turn_id="u1"),
    ], "u1", True)])
    assert spoken == [("Still speaking.", "")]


def test_discarded_control_terminal_is_counted_without_ending_output(monkeypatch, kpi):
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        ExecutionOutput(user_turn_id="u1", execution_completed=True),
        TextOutput(text="Still speaking.", user_turn_id="u1"),
    ], "", False)])
    assert spoken[0][0] == "Still speaking."
    assert len(kpi.of("voice_metrics_task_execution")) == 1


def test_owner_change_does_not_reopen_native_audio(monkeypatch, kpi):
    spoken = _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="u1"),
        AudioOutput(audio=np.zeros(32, dtype=np.float32), user_turn_id="u1"),
        UserSpeechOutput(turn_id="u2"),
        AudioOutput(audio=np.zeros(32, dtype=np.float32), user_turn_id="u2"),
    ], "u2", True)], native=True)
    assert sum(text == "native" for text, _ in spoken) == 1


def test_audio_interruption_does_not_override_confirmed_execution(kpi):
    metrics = LiveVoiceMetrics()
    iid = metrics.speech("u1", None, "provider_transcript")
    metrics.interrupt("u1")
    metrics.complete("u1", True)
    assert kpi.one("voice_metrics_task_execution")["interaction_id"] == iid


def test_completed_reply_still_queued_is_a_suppression_sample(kpi):
    metrics = LiveVoiceMetrics()
    metrics.speech("u1", None, "provider_transcript")
    metrics.complete("u1", True)
    metrics.interrupt("u1", pending_audio=True)
    kpi.close_all()
    assert kpi.one(voice_metrics.EVENT_SUPPRESSION)["applicable_interactions"] == 1


def test_pending_audio_ownership_survives_queue_pop_before_first_frame():
    from hal.drivers.voice.tts.service import TTSService

    speaker = object.__new__(TTSService)
    speaker._pending_queue_lock = threading.Lock()
    speaker._stop_event = threading.Event()
    speaker._speaking = True
    speaker._playback_owner = "run:previous"
    observed = []

    class PendingFrames:
        def get(self, timeout):
            # The drain popped the item, but synthesis has not delivered audio.
            observed.append(speaker.has_pending_speech("run:waiting"))
            return None

    speaker._pending_queue = [SimpleNamespace(
        owner="run:waiting", frame_queue=PendingFrames(), failed=False, text="Waiting",
    )]
    speaker._drain_pending_queue(object())
    assert observed == [True]
    assert not speaker.has_pending_speech("run:waiting")


def test_live_bundled_sentence_starts_before_next_delta(monkeypatch, kpi):
    from unittest.mock import Mock
    monkeypatch.setattr(config, 'REALTIME_FIRST_CHUNK_MAX_CHARS', 0)
    tts = Mock(speaking=False)

    def assert_first_already_sent():
        tts.speak_queue.assert_called_once()
        assert tts.speak_queue.call_args.args == ('I am right here.',)

    outputs = [UserSpeechOutput(turn_id='u-prefix'),
               TextOutput(text='I am right here. Let me', user_turn_id='u-prefix'),
               assert_first_already_sent,
               TextOutput(text=' help you.', user_turn_id='u-prefix')]
    _pump(monkeypatch, kpi, [(outputs, 'u-prefix', True)], tts=tts,
          strip_markers=VoiceService.strip_rt_markers)
    assert [call.args[0] for call in tts.speak_queue.call_args_list] == [
        'I am right here.', 'Let me help you.']
