"""AmberNet spoken language identification over raw 16 kHz PCM16.

The ONNX graph (exported in the lang-identify research repo) takes raw mono
float32 audio and returns logits over VoxLingua107 labels; the mel front end is
inside the graph, so only onnxruntime + numpy are needed here.
"""

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from hal.presets import LANG_EN, LANG_JA, LANG_VI, LANG_ZH_CN, LANG_ZH_TW

logger = logging.getLogger("hal.voice.lang_id")

SAMPLE_RATE = 16000
# The export presized its buffers for ~80 s; LID never needs more than a few.
_MAX_SAMPLES = 10 * SAMPLE_RATE

# App language code -> model label. zh-CN and zh-TW are one spoken language
# (Mandarin); the script is chosen from config, not from audio.
_APP_TO_MODEL: Dict[str, str] = {
    LANG_EN: "en",
    LANG_VI: "vi",
    LANG_JA: "ja",
    LANG_ZH_CN: "zh",
    LANG_ZH_TW: "zh",
}


@dataclass(frozen=True)
class LanguageGuess:
    language: str  # app code, e.g. "ja" or "zh-TW"
    probability: float  # within the configured languages
    seconds: float  # audio length the guess was made on
    # Probability mass of the configured languages over all 107 labels. Low means
    # the audio is another language or not speech; the restricted guess is forced.
    in_set: float = 1.0


class LanguageIdentifier:
    """Restricted-set AmberNet classifier. Thread-safe; loads lazily."""

    def __init__(self, model_path: Path, labels_path: Path, languages: Sequence[str],
                 threads: int = 1, max_seconds: float = 10.0):
        self._model_path = Path(model_path)
        self._labels_path = Path(labels_path)
        self._threads = threads
        self._max_samples = int(min(max_seconds, _MAX_SAMPLES / SAMPLE_RATE) * SAMPLE_RATE)
        self._lock = threading.Lock()
        self._session = None
        self._index = None
        self._failed = False
        # Model label -> app code. When both zh variants are allowed, the first
        # one listed (the user's preference order) wins.
        self._model_to_app: Dict[str, str] = {}
        for code in languages:
            label = _APP_TO_MODEL.get(code)
            if label is None:
                logger.warning("Language ID: unsupported language %r ignored", code)
                continue
            self._model_to_app.setdefault(label, code)
        self._labels: List[str] = list(self._model_to_app)

    @property
    def languages(self) -> List[str]:
        return list(self._model_to_app.values())

    @property
    def usable(self) -> bool:
        """Whether there is anything to choose between and the model can load."""
        return len(self._labels) >= 2 and not self._failed and self._model_path.is_file()

    def warm_up(self) -> bool:
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> bool:
        if self._session is not None:
            return True
        if self._failed:
            return False
        try:
            import numpy as np
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self._threads
            self._session = ort.InferenceSession(
                str(self._model_path), opts, providers=["CPUExecutionProvider"],
            )
            all_labels = json.loads(self._labels_path.read_text())
            self._index = np.array([all_labels.index(label) for label in self._labels])
            logger.info(
                "Language ID loaded (%s, languages=%s)", self._model_path.name, self.languages,
            )
            return True
        except Exception as e:
            self._failed = True
            logger.error("Language ID disabled — model load failed: %s", e)
            return False

    def identify(self, pcm16: bytes) -> Optional[LanguageGuess]:
        """Classify mono 16 kHz PCM16 audio; None if unavailable or empty."""
        if len(pcm16) < 2:
            return None
        import numpy as np

        audio = np.frombuffer(pcm16[: (len(pcm16) // 2) * 2], dtype=np.int16)
        audio = audio[-self._max_samples:].astype(np.float32) / 32768.0
        with self._lock:
            if not self._load_locked():
                return None
            logits = self._session.run(
                ["logits"],
                {"audio": audio[None, :], "audio_len": np.array([len(audio)], dtype=np.int64)},
            )[0][0]
        full = np.exp(logits - logits.max())
        full /= full.sum()
        probs = full[self._index]
        in_set = float(probs.sum())
        probs = probs / in_set if in_set > 0 else probs
        best = int(probs.argmax())
        return LanguageGuess(
            language=self._model_to_app[self._labels[best]],
            probability=float(probs[best]),
            seconds=len(audio) / SAMPLE_RATE,
            in_set=in_set,
        )
