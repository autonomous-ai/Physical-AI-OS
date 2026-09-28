from pydantic import BaseModel


class DelegateSignal(BaseModel):
    """Yielded by stream_output() when the model calls delegate_to_main."""

    message: str = ""
    transcript: str = ""
    handoff_context: str = ""
    user_turn_id: str = ""


class RejectSignal(BaseModel):
    """Yielded when the model explicitly rejects a non-user turn.

    This is intentionally distinct from a turn that merely ends with no output:
    only this signal is allowed to suppress the normal main-agent fallback.
    """
    user_turn_id: str = ""


class EndCallSignal(BaseModel):
    """Yielded when the model calls end_conversation to hang up a live session.

    Deliberately NOT an immediate stop: the caller waits LIVE_HANGUP_GRACE_S so
    the farewell the model is already speaking actually reaches the room. Ending
    the instant the tool call arrives cuts the goodbye off mid-word, which is
    the one thing a deliberate hangup must not do.
    """


class LookReplaySignal(BaseModel):
    """Yielded by stream_output() when the model called `look` and a FRESH
    camera frame was sent. The Live API queues a frame sent mid-turn for the
    NEXT turn (device-proven 2026-07-02: the model answered every look from
    the previous look's image), so the turn driver must replay the user's
    audio as a new turn — that new turn picks up the queued frame, and the
    model answers about what the user is holding NOW."""
