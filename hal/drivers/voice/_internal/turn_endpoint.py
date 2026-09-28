"""A provisional endpoint may be revoked while microphone capture continues."""

import logging
import re
import uuid


logger = logging.getLogger("hal.voice")

def needs_more_time(text: str) -> bool:
    """Conservative EN/VI hesitation hints, not a semantic classifier.

    Keep fillers in the transcript: removing them before this check loses the
    speaker's signal that they are still composing their request.
    """
    words = re.findall(r"[^\W_]+", text.casefold())
    tail = " ".join(words[-5:])
    return bool(re.search(
        r"(?:^| )(?:uh+|um+|uhm+|hmm+|ờ|ừm|ậm ừ|à|và|and|or|but|"
        r"because|rồi|với|hoặc|nhưng|để tôi nghĩ|để mình nghĩ|let me think)$", tail
    ))


def is_greeting(text: str) -> bool:
    words = re.findall(r"[^\W_]+", text.casefold())
    return len(words) <= 3 and bool(words) and (
        words[0] in {"hello", "hi", "hey", "chào"}
        or words[:2] == ["xin", "chào"]
    )


class TurnEndpoint:
    """One capture's state; the detector itself is shared across captures.

    Only run this after the legacy silence candidate fires. Speech/text/final changes
    invalidate both provisional completion and an in-flight model result.
    A bounded timeout also covers a missing, failed or stalled optional model.
    """

    def __init__(self, detector, *, fallback_s=2.5, max_pause_s=6.0):
        self.detector = detector
        self.fallback_s = max(0.0, fallback_s)
        self.max_pause_s = max(self.fallback_s, max_pause_s)
        self._session = uuid.uuid4().hex
        self._key = None
        self._generation = 0
        self._submitted = False
        self._complete = None
        self._candidate_at = 0.0
        self.reason = ""

    def should_close(self, *, now, last_speech, text, pcm, final_at=0.0):
        # A final may confirm exactly the preceding partial. It still marks
        # new evidence: re-evaluate the current audio, not the old prediction.
        key = (last_speech, text, final_at)
        if key != self._key:
            if self._key is not None and final_at != self._key[2]:
                logger.info(
                    "[turn-end] STT final updated; refreshing Smart Turn (previous=%s)",
                    self._complete,
                )
            self._key = key
            self._generation += 1
            self._submitted = False
            self._complete = None
            self._candidate_at = now
        token = (self._session, self._generation)
        silence = now - last_speech
        if not any(c.isalnum() for c in text):
            self.reason = "silence_clock"
            return True
        if silence >= self.max_pause_s:
            self.reason = "turn_pause_limit"
            return True
        if not self._submitted and self.detector is not None and not self.detector.failed:
            self._submitted = self.detector.submit(token, pcm)
        if self._submitted and self._complete is None:
            self._complete = self.detector.poll(token)
        if needs_more_time(text):
            return False
        if is_greeting(text) and silence < self.fallback_s:
            return False
        if self._complete is not None:
            self.reason = "smart_turn"
            return self._complete
        # Pending initialization/inference cannot hold a turn indefinitely.
        # Absence of a model is explicitly a silence fallback, never a claimed
        # semantic decision. Incomplete model predictions wait to max_pause_s.
        self.reason = "turn_fallback"
        if self.detector is not None and not self.detector.failed and now - self._candidate_at < 0.5:
            # Without an STT final the legacy candidate starts at the fallback
            # deadline itself. Give the asynchronous model one bounded chance
            # to answer rather than submitting and immediately closing.
            return False
        return silence >= self.fallback_s
