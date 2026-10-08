"""Voice metrics measurement tests."""

import threading

import pytest

from hal.telemetry import client, voice_metrics


class FakeClock:
    """Monotonic clock the tests drive by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


@pytest.fixture
def kpi(monkeypatch):
    """voice_metrics with a mock transport, a fake clock, and no real timers."""
    events = []
    timers = []

    def fake_report(event_name, params, event_id=""):
        events.append({"name": event_name, "params": params, "event_id": event_id})

    class FakeTimer:
        def __init__(self, interval, function, args=(), kwargs=None):
            self.interval, self.function, self.args = interval, function, args or ()
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            if self in timers:
                timers.remove(self)

        def fire(self):
            self.cancel()
            self.function(*self.args)

    clock = FakeClock()
    monkeypatch.setattr(voice_metrics.client, "report", fake_report)
    monkeypatch.setattr(client, "report", fake_report)
    monkeypatch.setattr(voice_metrics, "_now", clock)
    monkeypatch.setattr(voice_metrics.threading, "Timer", FakeTimer)
    voice_metrics.reset_for_test()

    class Harness:
        def __init__(self):
            self.events, self.timers, self.clock = events, timers, clock

        def close_all(self):
            for t in list(timers):
                t.fire()

        def of(self, name):
            return [e for e in self.events if e["name"] == name]

        def one(self, name):
            rows = self.of(name)
            assert len(rows) == 1, f"expected 1 {name}, got {len(rows)}: {rows}"
            return rows[0]["params"]

    yield Harness()
    voice_metrics.reset_for_test()


class FakeTTS:
    def __init__(self, realtime_feedback=False, interruptible=False, native_mode=False,
                 realtime_reply=False):
        self.realtime_feedback = realtime_feedback
        self.interruptible = interruptible
        self.native_mode = native_mode
        self.realtime_reply = realtime_reply


def _reply(kpi, owner):
    voice_metrics.playback_audio(owner, FakeTTS(realtime_feedback=True))


def _filler(kpi, owner):
    voice_metrics.playback_audio(owner, FakeTTS(interruptible=True))


def _native(kpi, owner):
    voice_metrics.playback_audio(owner, FakeTTS(native_mode=True))


def test_unowned_audio_is_never_an_acknowledgement(kpi):
    """Unclaimed audio is not credited to the newest open interaction."""
    voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(500)
    voice_metrics.playback_audio("", FakeTTS(interruptible=True))
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_NO_ACK
    assert p["ack_latency_ms"] is None
    assert p["unknown_owner_playbacks"] == 1


def test_old_turn_audio_does_not_acknowledge_a_new_command(kpi):
    """Explicit ownership sends the filler to the turn it belongs to."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(500)
    _filler(kpi, "run:run-old")
    kpi.close_all()

    rows = {e["params"]["interaction_id"]: e["params"] for e in kpi.of(voice_metrics.EVENT_INTERACTION)}
    assert rows[old]["outcome"] == voice_metrics.OUTCOME_ACKED
    assert rows[new]["outcome"] == voice_metrics.OUTCOME_NO_ACK


def test_realtime_native_audio_is_owned_by_its_interaction(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(800)
    _native(kpi, f"interaction:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "spoken_answer_realtime"
    assert p["ack_latency_ms"] == 800


def test_stale_reply_after_five_seconds_is_still_detected(kpi):
    """A stale reply arriving after grace+3s is still detected."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)

    kpi.clock.advance(20000)
    _reply(kpi, "run:run-old")
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_metrics.KIND_AGENT_REPLY
    assert p["stale_started_after_ms"] == 20000


def test_window_closing_on_a_live_turn_reports_incomplete_not_clean(kpi):
    """An unfinished observation must not read as 'no stale reply'."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)

    boundary_timer = kpi.timers[-1]
    boundary_timer.fire()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["observation_complete"] is False
    assert p["unobserved_interactions"] == 1


def test_completed_turns_make_the_observation_complete(kpi):
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["observation_complete"] is True


def test_audio_continuing_past_the_grace_is_stale(kpi):
    """Old audio ending after the grace period marks stale_observed."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(500)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    kpi.clock.advance(3000)
    voice_metrics.playback_end()
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["old_audio_playing_at_boundary"] is True
    assert p["stop_to_silence_ms"] == 3000
    assert p["stale_observed"] is True
    assert p["stale_audible_past_grace_ms"] == 3000 - voice_metrics.STALE_GRACE_MS


def test_audio_stopping_inside_the_grace_is_not_stale(kpi):
    """The documented grace stays a pass — and the raw timing is still kept."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(500)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    kpi.clock.advance(800)
    voice_metrics.playback_end()
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["stop_to_silence_ms"] == 800
    assert p["grace_ms"] == voice_metrics.STALE_GRACE_MS


def test_stale_filler_counts_too(kpi):
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)
    kpi.clock.advance(voice_metrics.STALE_GRACE_MS + 500)
    _filler(kpi, "run:run-old")
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_metrics.KIND_WAITING_AUDIO


def test_boundary_is_not_recorded_when_the_policy_did_not_apply(kpi):
    """No boundary is recorded when supersede is off or its POST failed."""
    old = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new, policy_applied=False)
    kpi.close_all()
    assert kpi.of(voice_metrics.EVENT_SUPPRESSION) == []
    assert old != new


def test_denominator_counts_only_active_turns(kpi):
    """An excluded turn is not counted in the stale-reply denominator."""
    done = voice_metrics.speech_end("silence_clock")
    voice_metrics.exclude(done, voice_metrics.EXCL_REJECTED_NOISE)
    kpi.clock.advance(500)
    live = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(live, "run-live")
    kpi.clock.advance(500)
    new = voice_metrics.speech_end("silence_clock")

    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)
    p_state = voice_metrics._watchers[0]
    assert p_state["applicable"] == {live}


def test_explicit_stop_covers_every_turn_in_flight(kpi):
    a = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(100)
    b = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["suppression_reason"] == voice_metrics.BOUNDARY_EXPLICIT_STOP
    assert p["applicable_interactions"] == 2
    assert a != b


def test_speech_end_clock_starts_at_the_detected_endpoint(kpi):
    """Latency is measured from endpoint detection."""
    detected_at = kpi.clock.t
    kpi.clock.advance(400)
    voice_metrics.speech_end("silence_clock", at=detected_at)
    kpi.clock.advance(600)
    _native(kpi, "")
    iid = voice_metrics._order[-1]
    voice_metrics.bind_run(iid, "run-x")
    _reply(kpi, "run:run-x")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_latency_ms"] == 1000


@pytest.mark.parametrize("reason", [
    voice_metrics.EXCL_REJECTED_NOISE,
    voice_metrics.EXCL_REJECTED_NON_USER,
    voice_metrics.EXCL_NO_TRANSCRIPT,
    voice_metrics.EXCL_NOT_ADDRESSED,
])
def test_excluded_inputs_are_reported_with_a_reason(kpi, reason):
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.exclude(iid, reason)
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_EXCLUDED
    assert p["eligible"] is False
    assert p["exclusion_reason"] == reason


def test_muted_speech_records_audio_refusal_without_excluding_task(kpi):
    """Mute is audio evidence, not an exclusion from task acceptance."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-m")
    kpi.clock.advance(700)
    voice_metrics.playback_muted("run:run-m")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["exclusion_reason"] == ""
    assert p["speaker_muted"] is True
    assert p["eligible"] is True


def test_unmuting_before_scoring_preserves_observed_mute(kpi):
    """The mute flag is sampled at turn time, not at verdict time."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-m")
    kpi.clock.advance(700)
    voice_metrics.playback_muted("run:run-m")
    kpi.clock.advance(9000)
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_NO_ACK
    assert p["exclusion_reason"] == ""
    assert p["speaker_muted"] is True


def test_a_late_mute_amends_a_reported_verdict(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.timers[0].fire()
    voice_metrics.playback_muted(f"run:{iid}")

    rows = kpi.of(voice_metrics.EVENT_INTERACTION)
    assert len(rows) == 2
    assert rows[1]["params"]["amendment_reason"] == "late_mute"
    assert rows[1]["params"]["exclusion_reason"] == ""
    assert rows[1]["params"]["speaker_muted"] is True


def test_mute_after_the_user_already_heard_something_changes_nothing(kpi):
    """A later mute must not retract an acknowledgement that really happened."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(600)
    _filler(kpi, f"run:{iid}")
    voice_metrics.playback_muted(f"run:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_ACKED
    assert p["exclusion_reason"] == ""


def test_slow_reply_keeps_its_real_latency(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-slow")
    kpi.clock.advance(7200)
    _reply(kpi, "run:run-slow")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_ACKED
    assert p["ack_latency_ms"] == 7200
    assert p["ack_deadline_ms"] == voice_metrics.ACK_DEADLINE_MS


def test_realtime_handled_is_counted_once(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(600)
    _native(kpi, f"interaction:{iid}")
    voice_metrics.bind_run(iid, "run-handled")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, iid)
    kpi.close_all()

    assert len(kpi.of(voice_metrics.EVENT_INTERACTION)) == 1


def test_late_exclusion_amends_an_already_reported_verdict(kpi):
    """A wrong verdict is corrected in the warehouse."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.close_all()
    voice_metrics.exclude(iid, voice_metrics.EXCL_NOT_ADDRESSED)

    rows = kpi.of(voice_metrics.EVENT_INTERACTION)
    assert len(rows) == 2
    amendment = rows[1]["params"]
    assert amendment["amends_event_id"] == "int-" + iid
    assert amendment["amendment_reason"] == "late_exclusion"
    assert amendment["exclusion_reason"] == voice_metrics.EXCL_NOT_ADDRESSED


def test_duplicate_report_is_suppressed_by_event_id(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics._close_interaction(iid)
    voice_metrics._close_interaction(iid)
    assert len(kpi.of(voice_metrics.EVENT_INTERACTION)) == 1


def test_stop_after_the_ack_window_still_finds_the_turn_to_suppress(kpi):
    """A stop after the ack window still finds the turn to suppress."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")

    kpi.clock.advance(10000)
    verdict_timer = kpi.timers[0]
    verdict_timer.fire()
    assert kpi.one(voice_metrics.EVENT_INTERACTION)["outcome"] == voice_metrics.OUTCOME_NO_ACK

    kpi.clock.advance(2000)                    # second 12: user presses stop
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    kpi.clock.advance(8000)                    # second 20: the old reply speaks
    _reply(kpi, "run:run-old")
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["applicable_interactions"] == 1
    assert p["stale_observed"] is True
    assert p["stale_started_after_ms"] == 8000


def test_a_turn_silent_past_its_lifetime_leaves_the_denominator(kpi):
    """The other half: a turn that really is over must not inflate the stale-reply metric."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(voice_metrics.TURN_ACTIVE_TTL_MS)
    for t in list(kpi.timers):
        if t.function is voice_metrics._retire_interaction:
            t.fire()

    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    kpi.close_all()
    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["applicable_interactions"] == 0


def test_playback_keeps_a_turn_alive(kpi):
    """A turn that is still talking is still alive, however long it has run."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(30000)
    _reply(kpi, "run:run-old")
    voice_metrics.playback_end()
    kpi.clock.advance(5000)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    assert voice_metrics._watchers[0]["applicable"] == {old}


def test_failed_dispatch_stays_eligible_as_a_failure(kpi):
    """An unserved valid command is not excluded."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.mark_failed(iid, voice_metrics.FAIL_DISPATCH_FAILED)
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["eligible"] is True
    assert p["outcome"] == voice_metrics.OUTCOME_NO_ACK
    assert p["failure_reason"] == voice_metrics.FAIL_DISPATCH_FAILED
    assert p["exclusion_reason"] == ""


def test_late_failure_amends_a_reported_verdict(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.timers[0].fire()
    voice_metrics.mark_failed(iid, voice_metrics.FAIL_DISPATCH_FAILED)

    rows = kpi.of(voice_metrics.EVENT_INTERACTION)
    assert len(rows) == 2
    assert rows[1]["params"]["amendment_reason"] == "late_failure"
    assert rows[1]["params"]["failure_reason"] == voice_metrics.FAIL_DISPATCH_FAILED


def test_realtime_wait_filler_is_attributed_to_its_interaction(kpi):
    """The realtime dead-air filler is credited to its interaction."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(1500)
    # Played back with owner run:<interaction id>; no os-server run exists yet.
    _filler(kpi, f"run:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_ACKED
    assert p["ack_modality"] == "waiting_audio"
    assert p["ack_latency_ms"] == 1500


def test_realtime_text_reply_is_attributed_to_its_interaction(kpi):
    """Realtime TTS answers carry the interaction id."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(900)
    voice_metrics.playback_audio(f"run:{iid}", FakeTTS(realtime_reply=True))
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "spoken_answer_realtime"
    assert p["ack_latency_ms"] == 900
    assert p["answer_latency_ms"] == 900
    assert p["answer_kind"] == voice_metrics.KIND_REALTIME_TTS


def test_report_never_raises_into_the_voice_path(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(client, "_ensure_worker", boom)
    client.report("voice_metrics_interaction", {"x": 1})


def test_report_does_not_block_the_caller(monkeypatch):
    """Telemetry stays off the audio critical path: a full queue drops."""
    monkeypatch.setenv(client.ENV_ANALYTICS_URL, "https://example.test/api")
    monkeypatch.setattr(client, "_ensure_worker", lambda: None)
    for _ in range(client.QUEUE_SIZE + 5):
        client.report("voice_metrics_interaction", {"x": 1})
    assert client.stats()["dropped"] >= 1
    assert threading.current_thread() is threading.main_thread()


def test_evicted_interaction_is_reported_before_being_forgotten(kpi):
    """Capacity eviction must not make a sample disappear silently."""
    first = voice_metrics.speech_end("silence_clock")
    for _ in range(voice_metrics._MAX_TRACKED):
        kpi.clock.advance(10)
        voice_metrics.speech_end("silence_clock")

    reported = [e["params"]["interaction_id"] for e in kpi.of(voice_metrics.EVENT_INTERACTION)]
    assert first in reported
    assert kpi.of(voice_metrics.EVENT_INTERACTION)[0]["params"]["eviction"] == "tracker_capacity"


def test_queued_segment_is_attributed_to_the_turn_that_queued_it(kpi):
    """A queued sentence is credited to its own turn."""
    first = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(first, "run-first")
    kpi.clock.advance(500)
    second = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(second, "run-second")

    _reply(kpi, "run:run-first")
    voice_metrics.playback_end()
    kpi.clock.advance(400)
    _reply(kpi, "run:run-second")
    kpi.close_all()

    rows = {e["params"]["interaction_id"]: e["params"] for e in kpi.of(voice_metrics.EVENT_INTERACTION)}
    assert rows[first]["outcome"] == voice_metrics.OUTCOME_ACKED
    assert rows[second]["outcome"] == voice_metrics.OUTCOME_ACKED
    assert rows[second]["ack_latency_ms"] == 400


def test_a_turn_still_speaking_at_its_ttl_stays_active(kpi):
    """A turn still speaking at its TTL stays active for stop suppression."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(voice_metrics.TURN_ACTIVE_TTL_MS + 1000)
    for t in list(kpi.timers):
        if t.function is voice_metrics._retire_interaction:
            t.fire()

    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    kpi.clock.advance(3000)
    voice_metrics.playback_end()
    kpi.close_all()

    p = kpi.of(voice_metrics.EVENT_SUPPRESSION)[0]["params"]
    assert p["applicable_interactions"] == 1
    assert p["stale_observed"] is True
    assert p["stop_to_silence_ms"] == 3000


def test_a_silent_turn_still_retires_at_its_ttl(kpi):
    """The keep-alive must not make every turn immortal."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    voice_metrics.playback_end()
    kpi.clock.advance(voice_metrics.TURN_ACTIVE_TTL_MS + 1000)
    for t in list(kpi.timers):
        if t.function is voice_metrics._retire_interaction:
            t.fire()

    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    assert voice_metrics._watchers[0]["applicable"] == set()


def test_current_interaction_serves_audio_os_server_starts(kpi):
    """The look-aim filler is tagged with the open interaction."""
    iid = voice_metrics.speech_end("silence_clock")
    assert voice_metrics.current_interaction() == iid

    kpi.clock.advance(1200)
    _filler(kpi, f"run:{voice_metrics.current_interaction()}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "waiting_audio"
    assert p["ack_latency_ms"] == 1200


def test_current_interaction_is_empty_once_the_turn_is_done(kpi):
    """A finished turn must not lend its id to unrelated later audio."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.exclude(iid, voice_metrics.EXCL_REJECTED_NOISE)
    assert voice_metrics.current_interaction() == ""


def test_answer_latency_is_measured_past_the_filler(kpi):
    """Filler and answer waits are recorded separately."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-x")
    kpi.clock.advance(800)
    _filler(kpi, f"run:{iid}")
    voice_metrics.playback_end()
    kpi.clock.advance(4200)
    _reply(kpi, "run:run-x")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_latency_ms"] == 800
    assert p["ack_modality"] == "waiting_audio"
    assert p["answer_latency_ms"] == 5000
    assert p["answer_kind"] == voice_metrics.KIND_AGENT_REPLY


def test_a_direct_answer_fills_both_numbers(kpi):
    """No filler: the answer IS the receipt, so both are the same instant."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(1500)
    _native(kpi, f"interaction:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_latency_ms"] == 1500
    assert p["answer_latency_ms"] == 1500
    assert p["answer_kind"] == voice_metrics.KIND_NATIVE_REALTIME


def test_answer_latency_is_null_when_only_a_filler_was_heard(kpi):
    """Acknowledged but never answered — null is the finding, not a gap."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(900)
    _filler(kpi, f"run:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_ACKED
    assert p["answer_latency_ms"] is None
    assert p["answer_kind"] == ""


def test_system_audio_is_not_an_answer(kpi):
    """A cached notice is a receipt, never the reply."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(500)
    voice_metrics.playback_audio(f"run:{iid}", FakeTTS())
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "acknowledgement_audio"
    assert p["answer_latency_ms"] is None


def test_explicit_stop_suppresses_late_audio_of_covered_turns_only(kpi):
    """The boundary gates TTS admission only for the turns it covered."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(500)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    assert voice_metrics.is_suppressed("run:run-old") is True
    assert voice_metrics.is_suppressed(f"interaction:{old}") is True
    assert voice_metrics.is_suppressed("") is False
    assert voice_metrics.is_suppressed("run:never-seen") is False

    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(new, "run-new")
    assert voice_metrics.is_suppressed("run:run-new") is False


def test_auto_supersede_does_not_gate_admission(kpi):
    """Automatic supersession is os-server's watermark; HAL only measures it."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)
    assert voice_metrics.is_suppressed("run:run-old") is False


def test_explicit_tap_suppresses_delayed_filler_interaction_owner(kpi):
    """Fillers use interaction IDs rather than timestamped OS run IDs."""
    from hal.drivers.voice.tts.service import TTSService

    old = voice_metrics.speech_end("manual_tap")
    assert not TTSService._owner_suppressed("run:" + old)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    new = voice_metrics.speech_end("manual_tap")
    assert TTSService._owner_suppressed("run:" + old)
    assert not TTSService._owner_suppressed("run:" + new)
