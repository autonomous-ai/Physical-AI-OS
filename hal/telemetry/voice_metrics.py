"""Voice metrics measurement — observations only, no behaviour change.

Acknowledgement and audio observations, timed on HAL:

  the response metric  Was the user's voice command acknowledged within
         ``ACK_DEADLINE_MS`` of the detected end of their speech?
  the stale-reply metric  Did an outdated reply actually get played after a suppression
         boundary (an explicit stop, or the realtime agent answering a newer
         question)?

Both targets (3 s / ≥95 % / <5 %) are **provisional**: every raw duration is
stored, so thresholds can be re-decided from the data instead of by
re-instrumenting devices.

KPI-3 reuses the utterance cohort with independent task eligibility and joins
explicit execution terminals from realtime or os-server. It measures finished
execution without observed terminal errors, not answer correctness. See
``docs/voice-metrics.md`` for the reporting horizon and the 85% target.

Design rules that decide whether a number is trustworthy:

* **Explicit acknowledgement evidence.** Schema 2 accepts either owned audio
  actually written to the stream or an OS receipt accepting this user task.
  Queued TTS, a model delegate tool alone and history sync are not receipts.
  First-audio latency stays separate; a stream write is not acoustic onset.
* **Explicit ownership.** Every playback carries the owner that claimed the
  speaker (``run:<turn_id>`` or ``interaction:<id>``). Audio with **no** owner
  is recorded as ``unknown`` and never counted as an acknowledgement — a
  guess is how an old filler gets credited to a new command.
* **Monotonic, one process.** Every elapsed time is a difference of two
  ``time.monotonic()`` reads inside HAL. os-server's clock is never
  subtracted from HAL's.
* **A detected endpoint, not the acoustic truth.** ``speech_end_method``
  records how the endpoint was decided, and the clock starts at that
  detection — before transcript assembly, not after it.
* **Incomplete is not success.** A suppression whose window closes while the
  suppressed turns are still alive reports ``observation_complete = false``,
  not "no stale reply".
"""

import logging
import math
import threading
import time

from hal.telemetry import client

logger = logging.getLogger("hal.telemetry")

# --- metrics thresholds (provisional) -------------------------------------------
ACK_DEADLINE_MS = 3000
# Keep watching past the target so a LATE ack is reported with its real
# latency instead of collapsing into "no ack at all".
ACK_OBSERVE_WINDOW_MS = 10000

# Playback does not stop the instant a boundary is stamped: TTSService.stop()
# sets an event and wakes the drain loops, and the worker still has to release
# the lock (device-observed on lamp-0c89: up to ~4s in the pathological case
# the wake-the-queues fix was written for; sub-second normally). Audio inside
# this grace is not counted as stale — but stop_to_silence_ms and every
# playback interval are recorded raw, so the choice stays auditable.
STALE_GRACE_MS = 2000
# How long a suppression is watched. A main-agent reply can arrive tens of
# seconds after the boundary, so this is deliberately long; when it expires
# with suppressed turns still alive the event says so instead of passing.
SUPPRESSION_OBSERVE_MS = 60000

# How long a turn is assumed to still be ABLE to speak. Separate from the
# acknowledgement window on purpose: reporting the the response metric verdict after 10s
# does not mean the main agent finished — it can answer much later, and a
# stop pressed at second 12 must still find the turn alive. The clock is
# refreshed by any playback that turn produces, so a turn that keeps talking
# keeps counting as active.
TURN_ACTIVE_TTL_MS = 45000

EVENT_INTERACTION = "voice_metrics_interaction"
EVENT_SUPPRESSION = "voice_metrics_suppression"

# --- Playback kinds (what was heard) ---------------------------------------
ACK_SCHEMA_VERSION = 2
KIND_DELEGATE_ACCEPTED = "delegate_accepted"
KIND_LOCAL_ACCEPTED = "local_accepted"

KIND_AGENT_REPLY = "agent_reply"          # main agent's answer, via the TTS queue
KIND_NATIVE_REALTIME = "native_realtime"  # realtime model's own voice
KIND_REALTIME_TTS = "realtime_tts"        # realtime text answer synthesized through TTS
KIND_WAITING_AUDIO = "waiting_audio"      # filler / "one moment"
KIND_SYSTEM_AUDIO = "system_audio"        # OS notice, cached phrase, greeting
KIND_UNKNOWN = "unknown"                  # nobody claimed this playback

# Acknowledgement modality per kind — the user-perceivable receipt signal.
# All AUDIO. Visual feedback (the listening LED) is NOT counted: its real
# activation is not instrumented, and counting it unmeasured would overstate
# the response metric.
_ACK_MODALITY = {
    KIND_AGENT_REPLY: "spoken_answer",
    KIND_NATIVE_REALTIME: "spoken_answer_realtime",
    KIND_REALTIME_TTS: "spoken_answer_realtime",
    KIND_WAITING_AUDIO: "waiting_audio",
    KIND_SYSTEM_AUDIO: "acknowledgement_audio",
}

# Which playback kinds are the ANSWER rather than a receipt. Waiting audio and
# system phrases tell the user they were heard; these kinds tell them what
# the device actually has to say.
_ANSWER_KINDS = (KIND_AGENT_REPLY, KIND_NATIVE_REALTIME, KIND_REALTIME_TTS)

# --- Outcomes ---------------------------------------------------------------
OUTCOME_ACKED = "acknowledged"
OUTCOME_NO_ACK = "no_ack"
OUTCOME_EXCLUDED = "excluded"

EXCL_REJECTED_NOISE = "rejected_noise"
EXCL_REJECTED_NON_USER = "rejected_non_user"
EXCL_NO_TRANSCRIPT = "no_transcript"
EXCL_NOT_ADDRESSED = "not_addressed"
EXCL_SPEAKER_MUTED = "speaker_muted"
EXCL_INTERRUPTED = "interrupted_by_user"
# NOT an exclusion: the request was valid, the device simply failed to serve
# it. Recorded as a failure reason on an ELIGIBLE interaction — dropping it
# would inflate the success rate with exactly the cases that hurt the user.
FAIL_DISPATCH_FAILED = "dispatch_failed"

BOUNDARY_EXPLICIT_STOP = "explicit_stop"
BOUNDARY_AUTO_SUPERSEDE = "auto_supersede"
BOUNDARY_SERVER_BARGE_IN = "server_barge_in"

_MAX_TRACKED = 32


class _Interaction:
    __slots__ = (
        "id", "speech_end", "speech_end_method", "event_type", "route",
        "run_id", "ack_latency_ms", "ack_modality", "ack_kind",
        "exclusion_reason", "failure_reason", "reported", "report_event_id",
        "timer", "life_timer", "closed", "last_activity",
        "answer_latency_ms", "answer_kind", "ack_played_at", "answer_played_at",
        "task_started_at_ms", "task_revision", "task_exclusion_reason",
        "endpoint_known", "mode", "suppressed", "provider_turn_id",
        "ack_at", "audio_latency_ms", "audio_kind", "speaker_muted",
    )

    def __init__(self, iid: str, speech_end: float, method: str):
        self.id = iid
        self.endpoint_known = True
        self.mode = "turn"
        self.provider_turn_id = ""
        self.task_started_at_ms = int(time.time() * 1000) - _ms(_now() - speech_end)
        self.task_revision = 0
        self.task_exclusion_reason = ""
        self.speech_end = speech_end
        self.speech_end_method = method
        self.event_type = ""
        self.route = ""
        self.run_id = ""
        self.ack_latency_ms = None
        self.ack_played_at = None
        self.ack_at = None
        self.audio_latency_ms = None
        self.audio_kind = ""
        self.speaker_muted = False
        self.ack_modality = ""
        self.ack_kind = ""
        # When the ANSWER itself was heard, as opposed to the receipt. A
        # filler counts as an acknowledgement (a deliberate decision, see
        # docs/voice-metrics.md), so ack_latency_ms alone cannot say how long
        # the user waited for the actual reply — this can.
        self.answer_latency_ms = None
        self.answer_played_at = None
        self.answer_kind = ""
        self.exclusion_reason = ""
        # A failure that is NOT an exclusion: the request was valid and went
        # unserved. Stays in the denominator (see _interaction_params).
        self.failure_reason = ""
        self.reported = False
        self.report_event_id = ""
        self.timer = None       # the response metric verdict deadline
        self.life_timer = None  # turn-lifetime deadline
        # `closed` means "can no longer speak" — NOT "already reported".
        # Reporting the acknowledgement verdict must not retire a turn the
        # main agent is still working on, or a later stop finds nothing to
        # suppress.
        self.closed = False
        # Set by an explicit stop that covered this turn. Audio owned by a
        # suppressed turn is refused at TTS admission (see is_suppressed):
        # the user asked for silence, and a reply or filler that lands tens
        # of seconds later must not undo that.
        self.suppressed = False
        self.last_activity = speech_end


_lock = threading.RLock()
_interactions: "dict[str, _Interaction]" = {}
_order: "list[str]" = []
_by_run: "dict[str, str]" = {}
_watchers: "list[dict]" = []
# The playback currently on the speaker: dict(kind, owner, interaction_id,
# started, ended). One at a time — the TTS service holds a single lock.
_playing = None
_unknown_owner_playbacks = 0


def _now() -> float:
    return time.monotonic()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


# --- Capture boundary -------------------------------------------------------

def speech_end(method: str, at: float = 0.0, *, endpoint_known: bool = True,
               mode: str = "turn") -> str:
    """Register the detected end of a user utterance. Returns its id.

    ``at`` is the monotonic timestamp of the detection itself (when the
    silence clock fired / the session was cut). Pass it so transcript
    assembly, trimming and speaker-ID do not inflate every latency. Defaults
    to now for callers that have no earlier stamp.

    ``method``: ``silence_clock``, ``stt_final``, ``tts_started``,
    ``music_started``, ``max_duration``, ``stt_error``.
    """
    if mode == "live" and not _valid_endpoint(at):
        endpoint_known = False
        at = 0.0
    iid = "vi-" + client.new_event_id()[:16]
    with _lock:
        it = _Interaction(iid, at or _now(), method)
        it.endpoint_known = endpoint_known
        it.mode = mode
        _interactions[iid] = it
        _order.append(iid)
        evicted = []
        while len(_order) > _MAX_TRACKED:
            oldest = _order[0]
            # Report before forgetting: an evicted interaction that never had
            # its verdict sent would otherwise vanish from the metrics entirely,
            # which is the one thing this module must never do.
            if not _interactions[oldest].reported:
                _interactions[oldest].report_event_id = "int-" + oldest
                evicted.append(_interaction_params(_interactions[oldest]))
                _interactions[oldest].reported = True
            _forget(oldest)
        it.timer = threading.Timer(ACK_OBSERVE_WINDOW_MS / 1000.0, _close_interaction, args=(iid,))
        it.timer.daemon = True
        it.timer.start()
        _arm_lifetime(it)
    for params in evicted:
        params["eviction"] = "tracker_capacity"
        logger.warning(
            "[voice-metrics] reporting evicted interaction early (interaction=%s)",
            params["interaction_id"],
        )
        client.report(EVENT_INTERACTION, params, event_id="int-" + params["interaction_id"])
    logger.info("[voice-metrics] speech end (interaction=%s method=%s)", iid, method)
    return iid


def _arm_lifetime(it: "_Interaction") -> None:
    """(Re)start the turn-lifetime clock. Any playback the turn produces is
    proof it is still alive, so the clock restarts from that moment."""
    if it.life_timer is not None:
        it.life_timer.cancel()
    it.life_timer = threading.Timer(TURN_ACTIVE_TTL_MS / 1000.0, _retire_interaction, args=(it.id,))
    it.life_timer.daemon = True
    it.life_timer.start()


def _retire_interaction(iid: str) -> None:
    """The turn has been silent for TURN_ACTIVE_TTL_MS: assume it can no
    longer speak. Only then does it leave the suppression denominator.

    A turn whose audio is STILL PLAYING is not silent, however long it has
    been running — the TTL restarts on the first frame of a playback, so a
    single long answer would otherwise time out mid-sentence and a stop
    pressed at second 46 would find nothing to suppress.
    """
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        if _playing and _playing.get("interaction_id") == iid:
            logger.info(
                "[voice-metrics] turn still speaking at its TTL -- keeping it active "
                "(interaction=%s)", iid,
            )
            it.last_activity = _now()
            _arm_lifetime(it)
            return
        it.closed = True


def bind_provider_turn(iid: str, provider_turn_id: str) -> None:
    """Bind an explicit provider key without inferring ownership from recency."""
    if not iid or not provider_turn_id:
        return
    with _lock:
        it = _interactions.get(iid)
        if it is not None and not it.provider_turn_id:
            it.provider_turn_id = provider_turn_id


def provider_interaction(provider_turn_id: str) -> str:
    """Resolve a provider key among the bounded tracked interactions only."""
    if not provider_turn_id:
        return ""
    with _lock:
        matches = [it.id for it in _interactions.values()
                   if it.provider_turn_id == provider_turn_id]
        # A reused provider key is ambiguous, never a reason to pick the newest.
        return matches[0] if len(matches) == 1 else ""


def current_interaction() -> str:
    """The utterance currently being served, or "" if none is open.

    For audio that os-server starts on HAL's behalf (the look-aim's filler)
    and therefore has no turn id of its own to carry. Only an interaction that
    is still active counts: a stale id would credit new audio to a finished
    turn, which is the guessing this module exists to avoid.
    """
    with _lock:
        for iid in reversed(_order):
            it = _interactions.get(iid)
            if it is not None and not it.closed:
                return iid
    return ""


def set_route(iid: str, route: str, event_type: str = "") -> None:
    """Record how the turn was routed. Late arrivals amend an already-reported
    verdict rather than being silently lost (see _amend)."""
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.route = route
        if event_type:
            it.event_type = event_type
        amendment = _amend_params(it, "late_route") if it.reported else None
    if amendment:
        _report_amendment(amendment)


def _valid_endpoint(at: float) -> bool:
    """Only accept finite timestamps already reached by the monotonic clock."""
    return (
        isinstance(at, (int, float)) and not isinstance(at, bool)
        and math.isfinite(at) and 0 < at <= _now()
    )


def set_endpoint(iid: str, method: str, at: float) -> None:
    """Upgrade transcript-only evidence when a real provider endpoint arrives.

    Arrival may follow acknowledgement if it carries an earlier real endpoint.
    An endpoint after first acceptance/audio cannot establish an ack latency.
    """
    with _lock:
        it = _interactions.get(iid)
        if (it is None or it.endpoint_known or not _valid_endpoint(at)
                or (it.ack_at is not None and at > it.ack_at)):
            return
        it.speech_end = at
        it.speech_end_method = method
        it.endpoint_known = True
        if it.ack_at is not None:
            it.ack_latency_ms = _ms(it.ack_at - at)
        if it.ack_played_at is not None:
            it.audio_latency_ms = _ms(it.ack_played_at - at)
        if it.answer_played_at is not None:
            it.answer_latency_ms = _ms(it.answer_played_at - at)
        amendment = _amend_params(it, "late_endpoint") if it.reported else None
    if amendment:
        _report_amendment(amendment)


def bind_run(iid: str, run_id: str) -> None:
    """Attach the os-server run id so the reply that comes back through the
    TTS queue is attributed to the utterance that asked for it."""
    if not iid or not run_id:
        return
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.run_id = run_id
        _by_run[run_id] = iid
        amendment = _amend_params(it, "late_run_binding") if it.reported else None
    if amendment:
        _report_amendment(amendment)


def dispatch_accepted(iid: str, *, local: bool = False) -> None:
    """Observe confirmed OS admission, not task completion or audio playback.

    Call only for a user-task receipt, never history sync or an unconfirmed POST.
    Earlier owned audio wins. A late receipt amends the existing verdict.
    """
    at = _now()
    with _lock:
        it = _interactions.get(iid)
        if it is None or (it.ack_at is not None and it.ack_at <= at):
            return
        it.ack_at = at
        it.ack_kind = KIND_LOCAL_ACCEPTED if local else KIND_DELEGATE_ACCEPTED
        it.ack_modality = "processing_accepted"
        if it.endpoint_known:
            it.ack_latency_ms = _ms(at - it.speech_end)
        amendment = _amend_params(it, "late_dispatch_ack") if it.reported else None
        logger.info("[voice-metrics] ack (interaction=%s kind=%s latency_ms=%s)",
                    iid, it.ack_kind, it.ack_latency_ms)
    if amendment:
        _report_amendment(amendment)


def task_execution_finished(iid: str) -> None:
    """Realtime confirmed turn completion; this is execution, not correctness.

    No playback requirement: queued speech can drain after the model finishes.
    The backend memory-sync run must never be credited as task execution.
    """
    if iid:
        client.report("voice_metrics_task_execution", {
            "schema_version": 1,
            "interaction_id": iid,
            "run_id": "",
            "outcome": "completed",
            "evidence": "realtime_turn_done",
            "execution_at_ms": int(time.time() * 1000),
            "error": False,
        }, event_id="task-rt-" + iid)


def is_playing(iid: str) -> bool:
    """Whether this interaction currently owns the physical playback interval."""
    with _lock:
        return bool(_playing and _playing.get("interaction_id") == iid)


def mark_failed(iid: str, reason: str) -> None:
    """Record that this interaction could not be served (the POST to
    os-server never landed, a backend error, a timeout).

    Deliberately NOT an exclusion: the user asked a valid question and got
    nothing. It stays eligible, so the metric counts it as the failure it is.
    """
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.failure_reason:
            return
        it.failure_reason = reason
        # Nothing will answer it, so it can no longer produce stale speech.
        it.closed = True
        amendment = _amend_params(it, "late_failure") if it.reported else None
    if amendment:
        _report_amendment(amendment)
        return
    logger.warning("[voice-metrics] interaction unserved (interaction=%s reason=%s)", iid, reason)


def exclude(iid: str, reason: str) -> None:
    """Mark an interaction as not a metric sample, with a reason. Reported, never
    silently discarded. An exclusion that arrives after the verdict was sent
    emits an amendment."""
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        # Muting or stopping speech does not cancel execution. Keep task
        # eligibility independent of KPI-1, including when mute arrived first.
        task_excluded = reason in (
            EXCL_REJECTED_NOISE, EXCL_REJECTED_NON_USER,
            EXCL_NO_TRANSCRIPT, EXCL_NOT_ADDRESSED,
        )
        changed = task_excluded and not it.task_exclusion_reason
        if changed:
            it.task_exclusion_reason = reason
        if it.exclusion_reason and not changed:
            return
        if not it.exclusion_reason:
            it.exclusion_reason = reason
        it.closed = True
        amendment = _amend_params(it, "late_exclusion") if it.reported else None
    if amendment:
        _report_amendment(amendment)
        return
    logger.info("[voice-metrics] excluded (interaction=%s reason=%s)", iid, reason)


# --- Playback boundary ------------------------------------------------------

def playback_audio(owner: str, tts=None) -> None:
    """The first frame of a playback actually reached the audio stream.

    ``owner`` is what claimed the speaker: ``run:<turn_id>`` (agent reply or a
    filler armed for that turn), ``interaction:<id>`` (realtime native voice),
    or "" when nobody claimed it. An unclaimed playback is recorded as
    ``unknown`` and NEVER counts as an acknowledgement.
    """
    started = _now()
    amendment = None
    with _lock:
        iid = _owner_interaction(owner)
        kind = _classify(owner, tts)
        global _playing, _unknown_owner_playbacks
        if _playing:
            # Queue segments can share one physical stream and therefore have
            # no intervening stream-end callback. The next segment's first
            # write closes the previous measured interval without touching audio.
            _observe_playback(
                _playing["kind"], _playing["interaction_id"], _playing["started"], started,
            )
        _playing = {
            "kind": kind, "owner": owner, "interaction_id": iid,
            "started": started, "ended": None,
        }
        if not iid:
            _unknown_owner_playbacks += 1
            logger.info(
                "[voice-metrics] playback with unknown owner (kind=%s) -- not counted as ack",
                kind,
            )
        else:
            it = _interactions.get(iid)
            if it is not None:
                # Proof the turn is still alive: restart its lifetime clock so
                # a stop pressed later still sees something to suppress.
                it.last_activity = started
                it.closed = False
                _arm_lifetime(it)
            changed = False
            if it is not None and it.ack_played_at is None:
                changed = True
                it.ack_played_at = started
                it.audio_kind = kind
                if it.endpoint_known:
                    it.audio_latency_ms = _ms(started - it.speech_end)
            if it is not None and (it.ack_at is None or started < it.ack_at):
                changed = True
                it.ack_at = started
                if it.endpoint_known:
                    it.ack_latency_ms = _ms(started - it.speech_end)
                it.ack_kind = kind
                it.ack_modality = _ACK_MODALITY.get(kind, kind)
                logger.info(
                    "[voice-metrics] ack (interaction=%s kind=%s latency_ms=%s)",
                    iid, kind, it.ack_latency_ms,
                )
            if it is not None and not it.answer_kind and kind in _ANSWER_KINDS:
                changed = True
                it.answer_played_at = started
                if it.endpoint_known:
                    it.answer_latency_ms = _ms(started - it.speech_end)
                it.answer_kind = kind
                logger.info(
                    "[voice-metrics] answer (interaction=%s kind=%s latency_ms=%s)",
                    iid, kind, it.answer_latency_ms,
                )
            if it is not None and changed and it.reported:
                amendment = _amend_params(it, "late_audio")
        _observe_playback(kind, iid, started, None)
    if amendment:
        _report_amendment(amendment)


def playback_muted(owner: str) -> None:
    """The speaker refused this speech because the device is muted.

    Record the observed refusal, not the mute flag at report time. Schema 2
    keeps valid silent tasks eligible: only accepted dispatch or owned audio
    acknowledges them. Unowned muted notices cannot mark another interaction.
    """
    with _lock:
        iid = _owner_interaction(owner)
        it = _interactions.get(iid) if iid else None
        if it is None or it.speaker_muted:
            return
        # Mute does not prevent accepting a silent task under ack schema 2.
        it.speaker_muted = True
        amendment = _amend_params(it, "late_mute") if it.reported else None
    if amendment:
        _report_amendment(amendment)
        return
    logger.info("[voice-metrics] speech muted (interaction=%s)", iid)


def playback_end() -> None:
    """Playback finished or was interrupted."""
    ended = _now()
    with _lock:
        global _playing
        was = _playing
        _playing = None
        if not was:
            return
        was["ended"] = ended
        _observe_playback(was["kind"], was["interaction_id"], was["started"], ended)


def _owner_interaction(owner: str) -> str:
    """Resolve the owner tag to an interaction.

    ``run:<id>`` normally carries an os-server run id, but the realtime paths
    (its wait filler, and text replies spoken before the turn ever reaches
    os-server) have no run id yet and tag themselves with the interaction id
    directly. Both are accepted; anything unresolvable stays unknown.
    """
    if not owner:
        return ""
    if owner.startswith("run:"):
        value = owner[4:]
        return _by_run.get(value) or (value if value in _interactions else "")
    if owner.startswith("interaction:"):
        iid = owner[len("interaction:"):]
        return iid if iid in _interactions else ""
    return ""


def is_suppressed(owner: str) -> bool:
    """True when the owner of a playback is a turn the user explicitly stopped.

    The one admission gate for late audio: os-server mutes replies it can
    attribute to a cancelled run, but a Harness result or a realtime wait
    filler reaches the speaker without passing that check. Unowned audio is
    never suppressed -- guessing would silence the sentence the user just
    asked for.
    """
    with _lock:
        it = _interactions.get(_owner_interaction(owner))
        return bool(it is not None and it.suppressed)


def _classify(owner: str, tts) -> str:
    """What the user heard, read from the speaking service's segment snapshots.

    The audio code reports only WHO owns the playback; the vocabulary below is
    this module's business:

      native voice      → the realtime model answering in its own voice
      realtime_reply    → the realtime model's text answer spoken through TTS
      realtime_feedback → the agent's reply (the only speech fed back to the
                          realtime session)
      interruptible     → a filler, i.e. waiting audio
      anything else     → system audio (notices, greetings, cached phrases)
    """
    try:
        if tts is not None and getattr(tts, "native_mode", False):
            return KIND_NATIVE_REALTIME
        if not owner:
            return KIND_UNKNOWN
        if tts is not None and getattr(tts, "realtime_reply", False):
            return KIND_REALTIME_TTS
        if tts is not None and getattr(
                tts, "playback_realtime_feedback", getattr(tts, "realtime_feedback", False)):
            return KIND_AGENT_REPLY
        if tts is not None and getattr(
                tts, "playback_interruptible", getattr(tts, "interruptible", False)):
            return KIND_WAITING_AUDIO
    except Exception:
        logger.exception("[voice-metrics] playback classification failed")
    return KIND_SYSTEM_AUDIO if owner else KIND_UNKNOWN


# --- Suppression boundary (the stale-reply metric) ------------------------------------------

def boundary(reason: str, triggering_interaction_id: str = "", policy_applied: bool = True,
             *, target_interaction_ids: set[str] | None = None,
             at: float | None = None) -> None:
    """Stamp a suppression boundary and watch for stale audio.

    ``policy_applied`` must be the OS's ANSWER, not an assumption: automatic
    supersession only happens when os-server has
    ``OS_REALTIME_SUPERSEDES_MAIN_REPLY`` enabled and actually stamped its
    watermark. A boundary that was never applied is not a the stale-reply metric situation and
    is not recorded — counting it would inflate the denominator with
    situations where nothing was ever suppressed.
    """
    if not policy_applied:
        logger.info("[voice-metrics] boundary skipped -- policy not applied (reason=%s)", reason)
        return
    at = _now() if at is None else at
    with _lock:
        applicable = _active_interactions_before(at, triggering_interaction_id)
        if target_interaction_ids is not None:
            # Server interruption identifies the cancelled response, not every
            # input already waiting behind it in the receive queue.
            applicable = target_interaction_ids.intersection(_interactions)
        if reason == BOUNDARY_EXPLICIT_STOP:
            for iid in applicable:
                _interactions[iid].suppressed = True
        playing = dict(_playing) if _playing else None
        state = {
            "reason": reason,
            "at": at,
            "event_id": client.new_event_id(),
            "trigger_interaction_id": triggering_interaction_id,
            "applicable": applicable,
            "stale_observed": False,
            "stale_kind": "",
            "stale_started_after_ms": None,
            "stale_audible_past_grace_ms": None,
            "stop_to_silence_ms": None,
            "old_audio_playing_at_boundary": bool(
                playing and playing["interaction_id"] in applicable
            ),
        }
        _watchers.append(state)
        if playing and playing["interaction_id"] in applicable:
            # Interval already in progress: re-evaluate it against this new
            # boundary so audio that merely CONTINUES past the grace counts.
            _observe_playback(playing["kind"], playing["interaction_id"],
                              playing["started"], None)
    logger.info(
        "[voice-metrics] suppression boundary (reason=%s applicable=%d playing_old=%s)",
        reason, len(applicable), state["old_audio_playing_at_boundary"],
    )
    t = threading.Timer(SUPPRESSION_OBSERVE_MS / 1000.0, _close_boundary, args=(state,))
    t.daemon = True
    t.start()


def _active_interactions_before(at: float, trigger_id: str) -> "set[str]":
    """Which ACTIVE interactions the boundary applies to.

    Only turns that can still speak count: an interaction that was excluded or
    already closed cannot produce a stale reply, so keeping it in the
    denominator would dilute the stale-reply metric with situations that never existed.
    Explicit stop covers everything in flight; automatic supersession covers
    only what is older than the utterance that triggered it.
    """
    trigger = _interactions.get(trigger_id)
    if trigger is None:
        # Explicit stop: everything still able to speak, including an
        # utterance that just ended (the user can say "stop" the moment they
        # finish talking).
        return {iid for iid, it in _interactions.items()
                if it.speech_end <= at and not it.closed}
    return {
        iid for iid, it in _interactions.items()
        if it.speech_end < trigger.speech_end and iid != trigger_id and not it.closed
    }


def _observe_playback(kind: str, iid: str, started: float, ended) -> None:
    """Score one playback interval against every open boundary.

    Stale means audio of a suppressed turn was audible AFTER the boundary plus
    the grace — whether it STARTED then, or merely kept going. Scoring the
    interval (not just its start) is what catches audio that began before the
    boundary and ran past the grace.
    """
    if not iid:
        return
    for state in _watchers:
        if iid not in state["applicable"]:
            continue
        limit = state["at"] + STALE_GRACE_MS / 1000.0
        stop = ended if ended is not None else _now()
        if ended is not None and state["stop_to_silence_ms"] is None:
            state["stop_to_silence_ms"] = _ms(ended - state["at"])
        if stop <= limit:
            continue  # went quiet inside the documented grace
        audible_past = _ms(stop - limit)
        if state["stale_observed"] and (state["stale_audible_past_grace_ms"] or 0) >= audible_past:
            continue
        state["stale_observed"] = True
        state["stale_kind"] = kind
        state["stale_started_after_ms"] = _ms(started - state["at"])
        state["stale_audible_past_grace_ms"] = audible_past
        logger.warning(
            "[voice-metrics] STALE playback past %s boundary "
            "(kind=%s interaction=%s started_after_ms=%d audible_past_grace_ms=%d)",
            state["reason"], kind, iid, state["stale_started_after_ms"], audible_past,
        )


def _close_boundary(state: dict) -> None:
    """Close one boundary observation and report it (the stale-reply metric denominator)."""
    with _lock:
        playing = dict(_playing) if _playing else None
        if playing and playing["interaction_id"] in state["applicable"]:
            _observe_playback(playing["kind"], playing["interaction_id"],
                              playing["started"], None)
        # Honest verdict: if a suppressed turn can still speak when the window
        # closes, "no stale reply" has not been established.
        still_active = [
            iid for iid in state["applicable"]
            if iid in _interactions and not _interactions[iid].closed
        ]
        try:
            _watchers.remove(state)
        except ValueError:
            pass

    client.report(
        EVENT_SUPPRESSION,
        {
            "interaction_id": state["trigger_interaction_id"],
            "suppression_reason": state["reason"],
            "applicable_interactions": len(state["applicable"]),
            "old_audio_playing_at_boundary": state["old_audio_playing_at_boundary"],
            "stale_observed": state["stale_observed"],
            "stale_kind": state["stale_kind"],
            "stale_started_after_ms": state["stale_started_after_ms"],
            "stale_audible_past_grace_ms": state["stale_audible_past_grace_ms"],
            "stop_to_silence_ms": state["stop_to_silence_ms"],
            "grace_ms": STALE_GRACE_MS,
            "observe_window_ms": SUPPRESSION_OBSERVE_MS,
            # False = the window closed with suppressed turns still able to
            # speak. Such a row is NOT evidence of a clean suppression.
            "observation_complete": not still_active,
            "unobserved_interactions": len(still_active),
        },
        event_id=state["event_id"],
    )


# --- Reporting --------------------------------------------------------------

def _forget(iid: str) -> None:
    it = _interactions.pop(iid, None)
    if iid in _order:
        _order.remove(iid)
    if it is not None:
        if it.run_id:
            _by_run.pop(it.run_id, None)
        if it.timer is not None:
            it.timer.cancel()
        if it.life_timer is not None:
            it.life_timer.cancel()


def _close_interaction(iid: str) -> None:
    """Report one interaction (the response metric sample) once its observation window is up."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.reported:
            return
        it.reported = True
        it.report_event_id = "int-" + iid
        params = _interaction_params(it)
    client.report(EVENT_INTERACTION, params, event_id="int-" + iid)


def _amend_params(it: "_Interaction", why: str) -> dict:
    """Build a corrected verdict for an interaction whose routing, failure or
    exclusion arrived after its row was already sent. The warehouse keeps
    both; the amendment wins (see the docs' metrics queries).

    Returns the row instead of sending it: the caller holds the tracker lock,
    and that lock is also taken from the audio thread — reporting under it
    would put telemetry work on the playback path.
    """
    params = _interaction_params(it)
    params["amends_event_id"] = it.report_event_id
    params["amendment_reason"] = why
    return params


def _report_amendment(params: dict) -> None:
    logger.info(
        "[voice-metrics] amending reported verdict (interaction=%s why=%s)",
        params["interaction_id"], params["amendment_reason"],
    )
    client.report(EVENT_INTERACTION, params, event_id="amend-" + client.new_event_id()[:12])


def _interaction_params(it: "_Interaction") -> dict:
    # A failure reason does NOT remove eligibility: the command was valid and
    # was not served. Only an exclusion (noise, not addressed, muted, …) means
    # "this was never a metric sample".
    it.task_revision += 1
    exclusion_reason = it.exclusion_reason or (
        "speech_endpoint_unavailable" if not it.endpoint_known else ""
    )
    eligible = not exclusion_reason
    if exclusion_reason:
        outcome = OUTCOME_EXCLUDED
    elif it.ack_latency_ms is not None:
        outcome = OUTCOME_ACKED
    else:
        outcome = OUTCOME_NO_ACK
    return {
        "interaction_id": it.id,
        "run_id": it.run_id,
        # KPI-3 reuses the utterance cohort, not the 10-second ack verdict.
        # Terminal execution observations are joined separately by id/run id.
        "task_schema_version": 1,
        "task_started_at_ms": it.task_started_at_ms,
        "task_revision": it.task_revision,
        "task_eligible": not it.task_exclusion_reason,
        "task_eligibility_known": bool(it.route or it.task_exclusion_reason),
        "task_exclusion_reason": it.task_exclusion_reason,
        "failure_reason": it.failure_reason,
        "event_type": it.event_type,
        "route": it.route,
        "speech_end_method": it.speech_end_method,
        "speech_endpoint_known": it.endpoint_known,
        "mode": it.mode,
        "eligible": eligible,
        "outcome": outcome,
        "exclusion_reason": exclusion_reason,
        # Raw observation, kept whatever the verdict, so the (provisional)
        # threshold can change without re-instrumenting the device.
        "ack_schema_version": ACK_SCHEMA_VERSION,
        "ack_latency_ms": it.ack_latency_ms,
        "audio_latency_ms": it.audio_latency_ms,
        "audio_kind": it.audio_kind,
        "speaker_muted": it.speaker_muted,
        "ack_modality": it.ack_modality,
        "ack_kind": it.ack_kind,
        # The wait for the real reply, independent of the receipt above. null
        # when the answer had not been heard by the end of the window — which
        # is itself the finding, not missing data.
        "answer_latency_ms": it.answer_latency_ms,
        "answer_kind": it.answer_kind,
        "ack_deadline_ms": ACK_DEADLINE_MS,
        "observe_window_ms": ACK_OBSERVE_WINDOW_MS,
        # Coverage: playbacks nobody claimed. A climbing count means some
        # audio could not be attributed and was excluded from ack decisions.
        "unknown_owner_playbacks": _unknown_owner_playbacks,
        "amends_event_id": "",
        "amendment_reason": "",
    }


def reset_for_test() -> None:
    """Clear all state. Tests only."""
    with _lock:
        for iid in list(_order):
            _forget(iid)
        _interactions.clear()
        _order.clear()
        _by_run.clear()
        _watchers.clear()
        global _playing, _unknown_owner_playbacks
        _playing = None
        _unknown_owner_playbacks = 0
