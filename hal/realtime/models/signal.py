from pydantic import BaseModel


class DelegateSignal(BaseModel):
    """Yielded by stream_output() when the model calls delegate_to_main."""

    message: str = ""
    transcript: str = ""
    handoff_context: str = ""
    user_turn_id: str = ""


class RejectSignal(BaseModel):
    """Yielded when the model explicitly rejects a non-user turn; only this suppresses the main-agent fallback."""
    user_turn_id: str = ""


class EndCallSignal(BaseModel):
    """Yielded when the model calls end_conversation; caller waits LIVE_HANGUP_GRACE_S so the farewell plays."""


class LookReplaySignal(BaseModel):
    """Yielded when `look` sent a fresh frame; Live queues mid-turn frames for the NEXT turn, so replay the audio."""
