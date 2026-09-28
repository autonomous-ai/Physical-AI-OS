"""Task eligibility and execution must not inherit audio-ack conclusions."""

from hal.telemetry import voice_metrics as vm
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- shared fixture


def test_mute_does_not_remove_task_from_denominator(kpi):
    iid = vm.speech_end("silence_clock")
    vm.set_route(iid, "delegated", "voice_command")
    vm.playback_muted("interaction:" + iid)
    vm._close_interaction(iid)
    row = kpi.one(vm.EVENT_INTERACTION)
    assert row["eligible"] is True
    assert row["task_eligible"] is True
    assert row["task_eligibility_known"] is True


def test_late_noise_exclusion_after_mute_updates_task_eligibility(kpi):
    iid = vm.speech_end("silence_clock")
    vm.playback_muted("interaction:" + iid)
    vm._close_interaction(iid)
    vm.exclude(iid, vm.EXCL_REJECTED_NOISE)
    rows = kpi.of(vm.EVENT_INTERACTION)
    assert rows[-1]["params"]["task_eligible"] is False
    assert rows[-1]["params"]["task_exclusion_reason"] == vm.EXCL_REJECTED_NOISE
    assert rows[-1]["params"]["task_revision"] > rows[0]["params"]["task_revision"]


def test_binding_after_ack_window_is_available_for_terminal_join(kpi):
    iid = vm.speech_end("silence_clock")
    vm.set_route(iid, "delegated", "voice_command")
    vm._close_interaction(iid)
    kpi.clock.advance(60000)
    vm.bind_run(iid, "long-task-run")
    row = kpi.of(vm.EVENT_INTERACTION)[-1]["params"]
    assert row["run_id"] == "long-task-run"
    assert row["amendment_reason"] == "late_run_binding"
    assert row["task_revision"] == 2
    assert row["task_started_at_ms"] > 0


def test_dispatch_failure_remains_eligible_and_interrupt_only_stops_speech(kpi):
    iid = vm.speech_end("silence_clock")
    vm.exclude(iid, vm.EXCL_INTERRUPTED)
    vm.mark_failed(iid, vm.FAIL_DISPATCH_FAILED)
    vm._close_interaction(iid)
    row = kpi.one(vm.EVENT_INTERACTION)
    assert row["task_eligible"] is True
    assert row["failure_reason"] == "dispatch_failed"


def test_realtime_terminal_survives_short_lived_playback_tracker_eviction(kpi):
    iid = vm.speech_end("silence_clock")
    for _ in range(vm._MAX_TRACKED):
        vm.speech_end("silence_clock")
    assert iid not in vm._interactions
    vm.task_execution_finished(iid)
    result = kpi.one("voice_metrics_task_execution")
    assert result["interaction_id"] == iid
    assert result["outcome"] == "completed"
    assert result["evidence"] == "realtime_turn_done"
    assert result["run_id"] == ""


def test_dispatch_requires_realtime_terminal_even_when_already_handled(kpi, monkeypatch):
    from unittest.mock import Mock
    from hal.drivers.voice._internal import turn_dispatch
    from hal.drivers.voice._internal.realtime_turn import RealtimeTurnResult, ROUTE_HANDLED

    monkeypatch.setattr(turn_dispatch, "_take_vision_handoff", lambda: ("", ""))
    monkeypatch.setattr(turn_dispatch, "_take_look_snapshot_marker", lambda: "")
    decorator = Mock()
    decorator.classify_wake_word.return_value = ("Hello", "voice_command")
    for completed in (False, True):
        iid = vm.speech_end("silence_clock")
        result = RealtimeTurnResult(False, True, "Hello", "", ROUTE_HANDLED,
                                    execution_completed=completed)
        sender = Mock()
        sender.send.return_value = turn_dispatch._NoResult
        turn_dispatch.dispatch_turn(decorator, sender, "Hello", [], [], result,
                                    identity=("Hello", "", ""), interaction_id=iid)
    rows = kpi.of("voice_metrics_task_execution")
    assert len(rows) == 1
    assert rows[0]["params"]["interaction_id"] == iid
