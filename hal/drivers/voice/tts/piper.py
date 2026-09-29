"""Piper TTS backend — synthesis on the device, no network, no quota."""

import json
import logging
import re
import os
import shutil
import subprocess
import threading
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import TTSBackend, STREAM_CHUNK_SIZE

logger = logging.getLogger("hal.voice.tts")

PIPER_BIN = os.environ.get("HAL_PIPER_BIN", "/opt/piper/piper")
PIPER_VOICES_DIR = os.environ.get("HAL_PIPER_VOICES", "/opt/piper/voices")
PIPER_DEFAULT_VOICE = os.environ.get("HAL_PIPER_VOICE", "en_US-ljspeech-medium")

_PIPER_LIB_DIR = os.path.dirname(PIPER_BIN)

_FALLBACK_SAMPLE_RATE = 22050

_AUDIO_TAG_RE = re.compile(
    r"\[(?:laugh|sigh|whisper|gasp|gulp|nervous|excited|frustrated|sorrowful|calm)[^\]]*\]",
    re.IGNORECASE,
)


def _default_length_scale() -> float:
    """The length scale the warm spare is spawned with."""
    try:
        import hal.config as _cfg
        speed = float(getattr(_cfg, "TTS_SPEED", 1.0) or 1.0)
    except Exception:
        speed = 1.0
    return 1.0 / speed if speed > 0 else 1.0


class PiperTTSBackend(TTSBackend):
    """Local Piper synthesis, streamed as raw PCM int16."""

    def __init__(self, voice: str = "", voices_dir: str = "", binary: str = ""):
        self._bin = binary or PIPER_BIN
        self._voices_dir = voices_dir or PIPER_VOICES_DIR
        self._default_voice = voice or PIPER_DEFAULT_VOICE
        self._rate_cache: dict = {}
        self._warned_voices: set = set()
        self._current_voice: str = ""
        self._spare: Optional[subprocess.Popen] = None
        self._spare_key: tuple = ()
        self._spare_lock = threading.Lock()
        if self.available:
            logger.info(
                "Piper TTS backend ready (bin=%s, voice=%s, rate=%dHz)",
                self._bin, self._default_voice, self.sample_rate,
            )
            threading.Thread(
                target=self._prewarm,
                args=(self._model_path(self._default_voice), _default_length_scale()),
                daemon=True,
            ).start()
        else:
            logger.warning(
                "Piper TTS unavailable (bin=%s exists=%s, voice=%s)",
                self._bin, os.path.exists(self._bin), self._model_path(self._default_voice),
            )

    def _model_path(self, voice: str) -> str:
        """Resolve a voice name to its .onnx."""
        voice = (voice or self._default_voice).strip()
        if os.path.isabs(voice):
            return voice
        if not voice.endswith(".onnx"):
            voice += ".onnx"
        return os.path.join(self._voices_dir, voice)

    def _any_installed_voice(self) -> str:
        """Any model actually present, newest-installed order not required.

        Used as the last fallback so a device that has *a* voice is never silent.
        """
        try:
            names = sorted(
                f[: -len(".onnx")]
                for f in os.listdir(self._voices_dir)
                if f.endswith(".onnx")
            )
        except OSError:
            return ""
        return names[0] if names else ""

    @property
    def available(self) -> bool:
        """Binary present, plus at least one voice — not specifically the configured one."""
        have_bin = (
            (os.path.isfile(self._bin) and os.access(self._bin, os.X_OK))
            or shutil.which(self._bin) is not None
        )
        if not have_bin:
            return False
        return (
            os.path.isfile(self._model_path(self._default_voice))
            or bool(self._any_installed_voice())
        )

    def rate_for(self, voice: str = "") -> int:
        """Sample rate of a specific model."""
        model = self._model_path(voice)
        return self._rate_of(model)

    @property
    def sample_rate(self) -> int:
        """Rate of the currently-selected voice, for the service's resampler."""
        return self._rate_of(self._model_path(self._current_voice or self._default_voice))

    def _rate_of(self, model: str) -> int:
        if model in self._rate_cache:
            return self._rate_cache[model]
        rate = _FALLBACK_SAMPLE_RATE
        try:
            with open(model + ".json", "r", encoding="utf-8") as fh:
                rate = int(json.load(fh)["audio"]["sample_rate"])
        except Exception as e:
            logger.warning("Piper: cannot read sample rate from %s.json (%s), assuming %d",
                           model, e, rate)
        self._rate_cache[model] = rate
        return rate

    @property
    def volume_boost(self) -> float:
        """1.0, not the 2.5 the hosted backends use."""
        return 1.0

    def _spawn(self, model_path: str, length_scale: float) -> subprocess.Popen:
        """Start Piper and let it load the model."""
        env = dict(os.environ)
        if _PIPER_LIB_DIR:
            env["LD_LIBRARY_PATH"] = (
                _PIPER_LIB_DIR + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            ).rstrip(os.pathsep)
        return subprocess.Popen(
            [self._bin, "--model", model_path, "--output-raw",
             "--length_scale", f"{length_scale:.3f}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )

    def _prewarm(self, model_path: str, length_scale: float) -> None:
        key = (model_path, round(length_scale, 3))
        try:
            proc = self._spawn(*key)
        except Exception as e:
            logger.warning("Piper prewarm failed: %s", e)
            return
        with self._spare_lock:
            # Another prewarm won the race, or the voice changed underneath us.
            if self._spare is not None:
                proc.kill()
                return
            self._spare, self._spare_key = proc, key

    def _take_process(self, model_path: str, length_scale: float) -> subprocess.Popen:
        """Hand out the warm process if it matches, then immediately start its replacement
        so the next utterance is warm too.
        """
        key = (model_path, round(length_scale, 3))
        proc = None
        with self._spare_lock:
            if self._spare is not None and self._spare_key == key and self._spare.poll() is None:
                proc, self._spare = self._spare, None
        if proc is None:
            proc = self._spawn(*key)
        threading.Thread(target=self._prewarm, args=key, daemon=True).start()
        return proc

    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Yield raw PCM int16 chunks as Piper produces them."""
        text = _AUDIO_TAG_RE.sub("", (text or "")).strip()
        if not text:
            return

        self._current_voice = voice or self._default_voice
        model_path = self._model_path(voice)
        if not os.path.isfile(model_path):
            if voice not in self._warned_voices:
                self._warned_voices.add(voice)
                logger.warning("Piper: voice %r not found at %s, using %s instead",
                               voice, model_path, self._default_voice)
            fallback = self._default_voice
            if not os.path.isfile(self._model_path(fallback)):
                fallback = self._any_installed_voice()
                if not fallback:
                    logger.warning("Piper: no voice models installed, nothing to speak")
                    return
            model_path = self._model_path(fallback)
            self._current_voice = fallback

        length_scale = 1.0 / speed if speed and speed > 0 else 1.0

        proc = self._take_process(model_path, length_scale)

        # Drain stderr on a thread. Piper logs its realtime factor per line and
        # will block on a full pipe if nobody reads it — which would stall
        # synthesis mid-sentence and look like a hang.
        stderr_tail: list = []

        def _drain():
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_tail.append(line.decode("utf-8", "replace").rstrip())
                    del stderr_tail[:-5]
            except Exception:
                pass

        threading.Thread(target=_drain, daemon=True).start()

        try:
            proc.stdin.write(text.encode("utf-8") + b"\n")  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]

            while True:
                chunk = proc.stdout.read(STREAM_CHUNK_SIZE)  # type: ignore[union-attr]
                if not chunk:
                    break
                yield chunk
        except BrokenPipeError:
            logger.warning("Piper: process closed early: %s", "; ".join(stderr_tail))
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass
            # The consumer may abandon the generator mid-utterance (barge-in),
            # so never wait indefinitely on a process nobody is reading.
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            if proc.returncode not in (0, None) and proc.returncode > 0 and stderr_tail:
                logger.warning("Piper exited %s: %s", proc.returncode, "; ".join(stderr_tail))
