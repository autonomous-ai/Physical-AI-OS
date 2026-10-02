"""Consume inline expression commands before realtime text reaches any consumer."""

import json
import math
from collections.abc import Callable
from pathlib import Path


def marker_instructions(instructions: str) -> str:
    """Replace the tool-specific instruction without changing routing rules."""
    lines = [line for line in instructions.splitlines()
             if not line.startswith('* **express_emotion (only if the tool exists):**')]
    return '\n'.join(lines) + '\n\n' + (
        Path(__file__).parent / 'resources' / 'emotion_markers.md'
    ).read_text(encoding='utf-8')


class EmotionMarkerStream:
    """Buffer bracketed chunks; execute only validated emotion markers once per turn.

    Other complete delivery tags survive unchanged. Incomplete/oversized bracket
    content is dropped at the boundary, never flushed into speech. No arbitrary
    hardware endpoint from generated text is executed.
    """

    def __init__(self, emotions: list[str], fire: Callable[[str, float], None]):
        self.emotions = set(emotions)
        self.fire = fire
        self.reset()

    def reset(self) -> None:
        self.pending = ''
        self.discarding = False
        self.seen: set[tuple[str, float]] = set()

    def feed(self, text: str) -> str:
        out: list[str] = []
        for char in text:
            if self.discarding:
                if char == ']':
                    self.discarding = False
                continue
            if self.pending or char == '[':
                self.pending += char
                if len(self.pending) > 512:
                    self.pending = ''
                    self.discarding = char != ']'
                elif char == ']':
                    token, self.pending = self.pending, ''
                    if token[1:].lower().startswith('hw:'):
                        self._consume(token)
                    else:
                        out.append(token)
            else:
                out.append(char)
        return ''.join(out)

    def _consume(self, token: str) -> None:
        prefix = '[hw:/emotion:'
        if not token.lower().startswith(prefix):
            return
        try:
            payload = json.loads(token[len(prefix):-1])
            if not isinstance(payload, dict):
                return
            emotion = payload.get('emotion')
            intensity = payload.get('intensity', 0.8)
            if not isinstance(emotion, str) or emotion not in self.emotions:
                return
            if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
                return
            if not math.isfinite(intensity) or not 0 <= intensity <= 1:
                return
        except (ValueError, TypeError):
            return
        key = (emotion, float(intensity))
        if key not in self.seen:
            self.seen.add(key)
            self.fire(*key)
