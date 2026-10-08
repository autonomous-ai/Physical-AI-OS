"""Voice metrics measurement: observations only, no behaviour change.

Tracks the response (ack) metric, the stale-reply metric and task execution (KPI-3).
All elapsed times are HAL time.monotonic() differences; unowned audio never counts as an ack.
See docs/voice-metrics.md.
"""

import logging
import math
import threading
import time

from hal.telemetry import client

logger = logging.getLogger("hal.telemetry")

ACK_DEADLINE_MS = 3000
# Watch past the target so a late ack keeps its real latency.
ACK_OBSERVE_WINDOW_MS = 10000

# Grace after a boundary before audio counts as stale (stop can take up to ~4s to drain).
STALE_GRACE_MS = 2000
# A main-agent reply can arrive tens of seconds after the boundary.
SUPPRESSION_OBSERVE_MS = 60000

# How long a turn can still speak; refreshed by every playback it produces.
TURN_ACTIVE_TTL_MS = 45000

EVENT_INTERACTION = "voice_metrics_interaction"
EVENT_SUPPRESSION = "voice_metrics_suppression"

ACK_SCHEMA_VERSION = 2
KIND_DELEGATE_ACCEPTED = "delegate_accepted"
KIND_LOCAL_ACCEPTED = "local_accepted"

KIND_AGENT_REPLY = "agent_reply"          # main agent's answer, via the TTS queue
KIND_NATIVE_REALTIME = "native_realtime"  # realtime model's own voice
KIND_REALTIME_TTS = "realtime_tts"        # realtime text answer synthesized through TTS
KIND_WAITING_AUDIO = "waiting_audio"      # filler / "one moment"
KIND_SYSTEM_AUDIO = "system_audio"        # OS notice, cached phrase, greeting
KIND_UNKNOWN = "unknown"                  # nobody claimed this playback

# Audio only: the listening LED is not instrumented, so it is not counted.
_ACK_MODALITY = {
    KIND_AGENT_REPLY: "spoken_answer",
    KIND_NATIVE_REALTIME: "spoken_answer_realtime",
    KIND_REALTIME_TTS: "spoken_answer_realtime",
    KIND_WAITING_AUDIO: "waiting_audio",
    KIND_SYSTEM_AUDIO: "acknowledgement_audio",
}

_ANSWER_KINDS = (KIND_AGENT_REPLY, KIND_NATIVE_REALTIME, KIND_REALTIME_TTS)

OUTCOME_ACKED = "acknowledged"
OUTCOME_NO_ACK = "no_ack"
OUTCOME_EXCLUDED = "excluded"

EXCL_REJECTED_NOISE = "rejected_noise"
EXCL_REJECTED_NON_USER = "rejected_non_user"
EXCL_NO_TRANSCRIPT = "no_transcript"
EXCL_NOT_ADDRESSED = "not_addressed"
EXCL_SPEAKER_MUTED = "speaker_muted"
EXCL_INTERRUPTED = "interrupted_by_user"
# Not an exclusion: stays eligible so the failure is counted.
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
        # When the answer itself was heard (fillers count as acks, not answers).
        self.answer_latency_ms = None
        self.answer_played_at = None
        self.answer_kind = ""
        self.exclusion_reason = ""
        self.failure_reason = ""
        self.reported = False
        self.report_event_id = ""
        self.timer = None       # the response metric verdict deadline
        self.life_timer = None  # turn-lifetime deadline
        # `closed` means "can no longer speak", not "already reported".
        self.closed = False
        # Audio of a suppressed turn is refused at TTS admission (see is_suppressed).
        self.suppressed = False
        self.last_activity = speech_end


_lock = threading.RLock()
_interactions: "dict[str, _Interaction]" = {}
_order: "list[str]" = []
_by_run: "dict[str, str]" = {}
_watchers: "list[dict]" = []
# One at a time: the TTS service holds a single lock.
_playing = None
_unknown_owner_playbacks = 0


def _now() -> float:
    return time.monotonic()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def speech_end(method: str, at: float = 0.0, *, endpoint_known: bool = True,
               mode: str = "turn") -> str:
    """Register the detected end of a user utterance and return its id.

    ``at`` is the monotonic detection time (defaults to now); ``method`` e.g. ``silence_clock``, ``stt_final``.
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
            # Report before forgetting so an evicted interaction never vanishes.
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
    """(Re)start the turn-lifetime clock."""
    if it.life_timer is not None:
        it.life_timer.cancel()
    it.life_timer = threading.Timer(TURN_ACTIVE_TTL_MS / 1000.0, _retire_interaction, args=(it.id,))
    it.life_timer.daemon = True
    it.life_timer.start()


def _retire_interaction(iid: str) -> None:
    """Retire a turn silent for TURN_ACTIVE_TTL_MS (a still-playing turn is never silent)."""
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


def speech_end_at(iid: str) -> float:
    """Monotonic time the user stopped speaking for ``iid``, or 0.0 when unknown."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or not it.endpoint_known:
            return 0.0
        return float(it.speech_end)


def current_interaction() -> str:
    """The utterance currently being served, or "" (only still-active interactions count)."""
    with _lock:
        for iid in reversed(_order):
            it = _interactions.get(iid)
            if it is not None and not it.closed:
                return iid
    return ""


def set_route(iid: str, route: str, event_type: str = "") -> None:
    """Record how the turn was routed; late arrivals amend the verdict."""
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
    """Upgrade transcript-only evidence when a real provider endpoint arrives."""
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
    """Attach the os-server run id so the TTS-queue reply is attributed to its utterance."""
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
    """Observe confirmed OS admission of a user task (not completion or playback)."""
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
    """Realtime confirmed turn completion (execution, not correctness)."""
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
    """Record that this interaction could not be served; it stays eligible (not an exclusion)."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.failure_reason:
            return
        it.failure_reason = reason
        it.closed = True
        amendment = _amend_params(it, "late_failure") if it.reported else None
    if amendment:
        _report_amendment(amendment)
        return
    logger.warning("[voice-metrics] interaction unserved (interaction=%s reason=%s)", iid, reason)


def exclude(iid: str, reason: str) -> None:
    """Mark an interaction as not a metric sample, with a reason; late ones emit an amendment."""
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        # Muting or stopping speech does not cancel execution.
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


def playback_audio(owner: str, tts=None) -> None:
    """The first frame of a playback reached the audio stream.

    ``owner``: ``run:<turn_id>``, ``interaction:<id>``, or "" (recorded as unknown, never an ack).
    """
    started = _now()
    amendment = None
    with _lock:
        iid = _owner_interaction(owner)
        kind = _classify(owner, tts)
        global _playing, _unknown_owner_playbacks
        if _playing:
            # Queue segments share one stream; the next segment's first write closes the previous interval.
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
                # Restart the lifetime clock so a later stop still sees this turn.
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
    """The speaker refused this speech because the device is muted."""
    with _lock:
        iid = _owner_interaction(owner)
        it = _interactions.get(iid) if iid else None
        if it is None or it.speaker_muted:
            return
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
    """Resolve an owner tag (``run:<id>`` or interaction id) to an interaction."""
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
    """True when a playback's owner is a turn the user explicitly stopped (unowned: never)."""
    with _lock:
        it = _interactions.get(_owner_interaction(owner))
        return bool(it is not None and it.suppressed)


def _classify(owner: str, tts) -> str:
    """Classify what the user heard from the speaking service's segment snapshots."""
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


def boundary(reason: str, triggering_interaction_id: str = "", policy_applied: bool = True,
             *, target_interaction_ids: set[str] | None = None,
             at: float | None = None) -> None:
    """Stamp a suppression boundary and watch for stale audio.

    ``policy_applied`` must be the OS's answer; unapplied boundaries are not recorded.
    """
    if not policy_applied:
        logger.info("[voice-metrics] boundary skipped -- policy not applied (reason=%s)", reason)
        return
    at = _now() if at is None else at
    with _lock:
        applicable = _active_interactions_before(at, triggering_interaction_id)
        if target_interaction_ids is not None:
            # Server interruption identifies the cancelled response, not every queued input.
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
            # Re-evaluate an in-progress interval against this new boundary.
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
    """Active interactions the boundary applies to (stop: all; supersession: older only)."""
    trigger = _interactions.get(trigger_id)
    if trigger is None:
        return {iid for iid, it in _interactions.items()
                if it.speech_end <= at and not it.closed}
    return {
        iid for iid, it in _interactions.items()
        if it.speech_end < trigger.speech_end and iid != trigger_id and not it.closed
    }


def _observe_playback(kind: str, iid: str, started: float, ended) -> None:
    """Score one playback interval against every open boundary (audible past boundary + grace = stale)."""
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
        # Suppressed turns still able to speak at window close: "no stale reply" is not established.
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
            "observation_complete": not still_active,
            "unobserved_interactions": len(still_active),
        },
        event_id=state["event_id"],
    )


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
    """Build a corrected verdict row for a late routing/failure/exclusion.

    Returned, not sent: the caller holds the tracker lock, also taken on the audio thread.
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
    # A failure reason does not remove eligibility; only an exclusion does.
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
        "ack_schema_version": ACK_SCHEMA_VERSION,
        "ack_latency_ms": it.ack_latency_ms,
        "audio_latency_ms": it.audio_latency_ms,
        "audio_kind": it.audio_kind,
        "speaker_muted": it.speaker_muted,
        "ack_modality": it.ack_modality,
        "ack_kind": it.ack_kind,
        # null when the answer was not heard by window end (a finding, not missing data).
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
