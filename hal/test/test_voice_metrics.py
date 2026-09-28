"""Voice metrics measurement tests.

Every test uses a mock transport and a fake clock — no event leaves the
process, no test contacts the production analytics endpoint, and no test
sleeps for a real observation window.
"""

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
    """voice_metrics with a mock transport, a fake clock, and no real timers.

    Timers are captured instead of started so a test closes an observation
    window explicitly — that is what makes "the window expired while the turn
    was still alive" testable at all.
    """
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


# --- Finding 1: ownership, no guessing --------------------------------------

def test_unowned_audio_is_never_an_acknowledgement(kpi):
    """Regression: audio nobody claimed used to be credited to the newest open
    interaction, so an old filler could 'acknowledge' a new command."""
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


# --- Finding 2: long-lived suppression watching ------------------------------

def test_stale_reply_after_five_seconds_is_still_detected(kpi):
    """Regression: the watcher used to close after grace+3s, so a main-agent
    reply arriving later was scored as a clean suppression."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new)

    kpi.clock.advance(20000)          # far past the old 5s window
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

    # Close ONLY the suppression watcher; the old turn is still alive.
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


# --- Finding 3: audio that CONTINUES past the grace --------------------------

def test_audio_continuing_past_the_grace_is_stale(kpi):
    """Regression: old audio playing at the stop and ending 3s later (grace 2s)
    reported stop_to_silence_ms=3000 but stale_observed=false."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(500)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    kpi.clock.advance(3000)           # still talking 3s after the stop
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

    kpi.clock.advance(800)            # quiet well inside STALE_GRACE_MS
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


# --- Finding 4: only real boundaries, only active turns ----------------------

def test_boundary_is_not_recorded_when_the_policy_did_not_apply(kpi):
    """OS_REALTIME_SUPERSEDES_MAIN_REPLY off (or a failed POST) means nothing
    was suppressed — not a the stale-reply metric situation at all."""
    old = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(1000)
    new = voice_metrics.speech_end("silence_clock")
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, new, policy_applied=False)
    kpi.close_all()
    assert kpi.of(voice_metrics.EVENT_SUPPRESSION) == []
    assert old != new


def test_denominator_counts_only_active_turns(kpi):
    """A turn already excluded cannot produce a stale reply; keeping it in the
    denominator would dilute the stale-reply metric with situations that never existed."""
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


# --- Finding 5: first real audio write, not the callback ---------------------

def test_speech_end_clock_starts_at_the_detected_endpoint(kpi):
    """Latency is measured from the endpoint detection, not from the moment
    the tracker was told about it (transcript assembly runs in between)."""
    detected_at = kpi.clock.t
    kpi.clock.advance(400)                       # finalize_session work
    voice_metrics.speech_end("silence_clock", at=detected_at)
    kpi.clock.advance(600)
    _native(kpi, "")                             # unowned: no ack
    iid = voice_metrics._order[-1]
    voice_metrics.bind_run(iid, "run-x")
    _reply(kpi, "run:run-x")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_latency_ms"] == 1000           # 400 + 600, not 600


# --- Outcomes and exclusions -------------------------------------------------

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
    """Regression (device-observed 08/09/2026): the mute flag was sampled when
    the verdict was written, 10s later. Someone unmuting in between made a
    muted turn look like one the device simply never answered."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-m")
    kpi.clock.advance(700)
    voice_metrics.playback_muted("run:run-m")   # refused while muted
    kpi.clock.advance(9000)                     # user unmutes somewhere here
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_NO_ACK
    assert p["exclusion_reason"] == ""
    assert p["speaker_muted"] is True


def test_a_late_mute_amends_a_reported_verdict(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.timers[0].fire()                        # verdict written: no_ack
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
    assert p["ack_latency_ms"] == 7200            # eligible, just over target
    assert p["ack_deadline_ms"] == voice_metrics.ACK_DEADLINE_MS


def test_realtime_handled_is_counted_once(kpi):
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(600)
    _native(kpi, f"interaction:{iid}")
    voice_metrics.bind_run(iid, "run-handled")        # the voice_agent_handled POST
    voice_metrics.boundary(voice_metrics.BOUNDARY_AUTO_SUPERSEDE, iid)
    kpi.close_all()

    assert len(kpi.of(voice_metrics.EVENT_INTERACTION)) == 1


def test_late_exclusion_amends_an_already_reported_verdict(kpi):
    """A verdict that turns out wrong must be corrected in the warehouse, not
    left standing."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.close_all()                                # verdict reported: no_ack
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


# --- Review finding: turn lifetime is not the acknowledgement deadline -------

def test_stop_after_the_ack_window_still_finds_the_turn_to_suppress(kpi):
    """Reproduction: stop at second 12, old reply plays at second 20.

    The the response metric verdict is reported at 10s, but the main agent is still working
    — retiring the turn there made the stop find nothing to suppress
    (applicable_interactions=0, stale_observed=false)."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")

    kpi.clock.advance(10000)
    verdict_timer = kpi.timers[0]
    verdict_timer.fire()                       # the response metric verdict at 10s
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
    _reply(kpi, "run:run-old")                 # still speaking at 30s
    voice_metrics.playback_end()
    kpi.clock.advance(5000)
    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)

    assert voice_metrics._watchers[0]["applicable"] == {old}


# --- Review finding: an unserved command stays in the denominator ------------

def test_failed_dispatch_stays_eligible_as_a_failure(kpi):
    """A valid command the device never served must not be excluded — that
    would inflate the success rate with the cases that hurt most."""
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


# --- Review finding: realtime audio carries ownership ------------------------

def test_realtime_wait_filler_is_attributed_to_its_interaction(kpi):
    """The realtime dead-air filler tags itself with the interaction it is
    waiting for; the user heard it, so the metrics must see it."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(1500)
    # os-server plays it back with the owner HAL passed through the filler
    # request, i.e. run:<interaction id> — no os-server run exists yet.
    _filler(kpi, f"run:{iid}")
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["outcome"] == voice_metrics.OUTCOME_ACKED
    assert p["ack_modality"] == "waiting_audio"
    assert p["ack_latency_ms"] == 1500


def test_realtime_text_reply_is_attributed_to_its_interaction(kpi):
    """The realtime branch that answers via TTS (not native audio) tags the
    speech with the same interaction id."""
    iid = voice_metrics.speech_end("silence_clock")
    kpi.clock.advance(900)
    voice_metrics.playback_audio(f"run:{iid}", FakeTTS(realtime_reply=True))
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "spoken_answer_realtime"
    assert p["ack_latency_ms"] == 900
    assert p["answer_latency_ms"] == 900
    assert p["answer_kind"] == voice_metrics.KIND_REALTIME_TTS


# --- Transport ---------------------------------------------------------------

def test_report_never_raises_into_the_voice_path(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(client, "_ensure_worker", boom)
    client.report("voice_metrics_interaction", {"x": 1})    # must not raise


def test_report_does_not_block_the_caller(monkeypatch):
    """Telemetry stays off the audio critical path: a full queue drops."""
    monkeypatch.setenv(client.ENV_ANALYTICS_URL, "https://example.test/api")
    monkeypatch.setattr(client, "_ensure_worker", lambda: None)
    for _ in range(client.QUEUE_SIZE + 5):
        client.report("voice_metrics_interaction", {"x": 1})
    assert client.stats()["dropped"] >= 1
    assert threading.current_thread() is threading.main_thread()


# --- Self-review findings ----------------------------------------------------

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
    """A sentence queued behind another turn's speech plays on the stream that
    turn opened; it must still be credited to its OWN turn."""
    first = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(first, "run-first")
    kpi.clock.advance(500)
    second = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(second, "run-second")

    _reply(kpi, "run:run-first")          # stream opened by the first turn
    voice_metrics.playback_end()
    kpi.clock.advance(400)
    _reply(kpi, "run:run-second")         # drained queue segment, own owner
    kpi.close_all()

    rows = {e["params"]["interaction_id"]: e["params"] for e in kpi.of(voice_metrics.EVENT_INTERACTION)}
    assert rows[first]["outcome"] == voice_metrics.OUTCOME_ACKED
    assert rows[second]["outcome"] == voice_metrics.OUTCOME_ACKED
    assert rows[second]["ack_latency_ms"] == 400


# --- Review findings: local commands and long playbacks ----------------------

def test_a_turn_still_speaking_at_its_ttl_stays_active(kpi):
    """Reproduction: a long answer is still playing at second 46; a stop then
    must still find the turn to suppress."""
    old = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")                        # playback starts, never ends
    kpi.clock.advance(voice_metrics.TURN_ACTIVE_TTL_MS + 1000)
    for t in list(kpi.timers):
        if t.function is voice_metrics._retire_interaction:
            t.fire()

    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    kpi.clock.advance(3000)                           # still talking 3s after the stop
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
    voice_metrics.playback_end()                      # went quiet
    kpi.clock.advance(voice_metrics.TURN_ACTIVE_TTL_MS + 1000)
    for t in list(kpi.timers):
        if t.function is voice_metrics._retire_interaction:
            t.fire()

    voice_metrics.boundary(voice_metrics.BOUNDARY_EXPLICIT_STOP)
    assert voice_metrics._watchers[0]["applicable"] == set()


def test_current_interaction_serves_audio_os_server_starts(kpi):
    """The look-aim's filler is played BY os-server, so it has no turn id of
    its own — it asks for the open interaction and tags the request with it."""
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


# --- Answer latency: the wait for the reply, not for the receipt -------------

def test_answer_latency_is_measured_past_the_filler(kpi):
    """A filler acknowledges the user, but it is not the answer. Both waits are
    recorded so "we replied in 2s" cannot be claimed on the strength of a
    'one moment'."""
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "run-x")
    kpi.clock.advance(800)
    _filler(kpi, f"run:{iid}")            # receipt
    voice_metrics.playback_end()
    kpi.clock.advance(4200)
    _reply(kpi, "run:run-x")              # the actual answer
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
    voice_metrics.playback_audio(f"run:{iid}", FakeTTS())   # system audio
    kpi.close_all()

    p = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert p["ack_modality"] == "acknowledgement_audio"
    assert p["answer_latency_ms"] is None


# --- Explicit stop refuses late audio at admission ---------------------------

def test_explicit_stop_suppresses_late_audio_of_covered_turns_only(kpi):
    """Device-observed 2026-09-17: a Harness reply spoke 27 s after the click
    and a realtime wait filler 12 s after it. The boundary must gate TTS
    admission for every turn it covered, and only those."""
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
