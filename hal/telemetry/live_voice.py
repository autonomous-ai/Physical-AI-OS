"""Correlate live provider observations without guessing audio ownership.

Provider input IDs are session-local. Each maps to exactly one HAL interaction;
timeouts between receive calls do not close it or create another task. A missing
speech endpoint excludes only the latency KPI, never execution completion.
"""

from collections import Counter
from functools import wraps
import logging

from hal.telemetry import client, voice_metrics

logger = logging.getLogger("hal.telemetry")


def _observation(default=None):
    """A failed measurement must never change voice playback or delegation."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception:
                logger.exception("[voice-metrics] live %s observation failed", function.__name__)
                return default
        return wrapped
    return decorate


class LiveVoiceMetrics:
    def __init__(self):
        self.interactions: dict[str, str] = {}
        self.finished: set[str] = set()
        self._seeded: set[str] = set()
        self.interrupted: set[str] = set()
        self.coverage = Counter()

    @_observation()
    def seed(self, turn_id: str, interaction_id: str) -> None:
        """Attach a buffered STT opener without creating a second interaction."""
        if turn_id and interaction_id:
            self.interactions.setdefault(turn_id, interaction_id)
            voice_metrics.bind_provider_turn(self.interactions[turn_id], turn_id)
            self._seeded.add(turn_id)
            voice_metrics.set_route(interaction_id, "realtime_handled", "voice")

    @_observation("")
    def speech(self, turn_id: str, endpoint_at: float | None, method: str) -> str:
        if method == "server_vad_receive":
            # Preserve provider/cue behaviour while refusing a network arrival
            # timestamp as a substitute for the end of captured speech.
            self.coverage["endpoint_receive_only_observations"] += 1
            endpoint_at = None
        if not turn_id:
            self.coverage["unkeyed_user_observations"] += 1
            return ""
        iid = self.interactions.get(turn_id, "")
        if iid:
            if endpoint_at is not None and turn_id not in self._seeded:
                voice_metrics.set_endpoint(iid, method, endpoint_at)
            return iid
        iid = voice_metrics.speech_end(
            method, at=endpoint_at or 0.0,
            endpoint_known=endpoint_at is not None, mode="live",
        )
        self.interactions[turn_id] = iid
        voice_metrics.bind_provider_turn(iid, turn_id)
        logger.info("[voice-metrics] live ownership turn=%s interaction=%s endpoint_method=%s",
                    turn_id, iid, method)
        voice_metrics.set_route(iid, "realtime_handled", "voice")
        return iid

    @_observation("")
    def interaction(self, turn_id: str) -> str:
        return self.interactions.get(turn_id, "")

    @_observation("")
    def owner(self, turn_id: str) -> str:
        iid = self.interaction(turn_id)
        if not iid:
            self.coverage["unowned_output_chunks"] += 1
        return "interaction:" + iid if iid else ""

    @_observation()
    def complete(self, turn_id: str, completed: bool) -> None:
        if not completed or turn_id in self.finished:
            return
        iid = self.interaction(turn_id)
        if not iid:
            self.coverage["unowned_completions"] += 1
            return
        self.finished.add(turn_id)
        voice_metrics.task_execution_finished(iid)

    @_observation()
    def interrupt(self, turn_id: str, at: float | None = None, *, pending_audio=False) -> None:
        if turn_id in self.interrupted:
            return
        iid = self.interaction(turn_id)
        if not iid:
            self.coverage["unowned_interruptions"] += 1
            return
        if turn_id in self.finished and not voice_metrics.is_playing(iid) and not pending_audio:
            # An ordinary next question, after the previous response went
            # silent, is not something that had to suppress an old reply.
            return
        self.interrupted.add(turn_id)
        voice_metrics.boundary(
            voice_metrics.BOUNDARY_SERVER_BARGE_IN,
            target_interaction_ids={iid}, at=at,
        )
        # User interruption is an ack exclusion, not proof execution failed or
        # completed. Already completed execution remains completed.
        voice_metrics.exclude(iid, voice_metrics.EXCL_INTERRUPTED)

    @_observation()
    def reject(self, turn_id: str) -> None:
        iid = self.interaction(turn_id)
        if iid:
            voice_metrics.exclude(iid, voice_metrics.EXCL_REJECTED_NON_USER)

    @_observation()
    def close(self) -> None:
        client.report("voice_metrics_live_coverage", {
            "observed_interactions": len(self.interactions),
            "completed_interactions": len(self.finished),
            **self.coverage,
        })
