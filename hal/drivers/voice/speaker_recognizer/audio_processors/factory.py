"""Factory for building composite audio processors from config."""

import logging

from .composite import CompositeAudioProcessor
from .high_pass_filter import HighPassFilter
from .mono_converter import MonoConverter
from .noise_reducer import NoiseReducer
from .resampler import Resampler
from .rms_normalizer import RMSNormalizer
from .stoi_filter import SpeechIntelligibilityFilter
from .voice_activity_filter import VoiceActivityFilter

_logger = logging.getLogger(__name__)


class AudioProcessorFactory:
    """Factory that creates a CompositeAudioProcessor from config."""

    def __init__(
        self,
        target_sample_rate: int = 16000,
        enable_mono: bool = True,
        enable_resample: bool = True,
        enable_high_pass: bool = False,
        high_pass_cutoff_hz: float = 80.0,
        enable_noise_reduce: bool = False,
        noise_reduce_stationary: bool = False,
        enable_vad: bool = True,
        vad_min_duration_sec: float = 0.5,
        # 0.25, not perception-service's 0.4: TEN-VAD's false-positive gates trim
        # non-speech from inside the kept span, which mechanically lowers the ratio.
        vad_min_voice_ratio: float = 0.25,
        vad_speech_prob_threshold: float = 0.5,
        vad_speaker_band: bool = True,
        vad_max_level_drop_db: float | None = 20.0,
        enable_rms_normalize: bool = True,
        rms_target: float = 0.1,
        enable_stoi: bool = False,
        stoi_model_path: str = "",
        stoi_threshold: float = 0.70,
        stoi_chunk_sec: float = 5.0,
    ) -> None:
        self._target_sample_rate = target_sample_rate
        self._enable_mono = enable_mono
        self._enable_resample = enable_resample
        self._enable_high_pass = enable_high_pass
        self._high_pass_cutoff_hz = high_pass_cutoff_hz
        self._enable_noise_reduce = enable_noise_reduce
        self._noise_reduce_stationary = noise_reduce_stationary
        self._enable_vad = enable_vad
        self._vad_min_duration_sec = vad_min_duration_sec
        self._vad_min_voice_ratio = vad_min_voice_ratio
        self._vad_speech_prob_threshold = vad_speech_prob_threshold
        self._vad_speaker_band = vad_speaker_band
        self._vad_max_level_drop_db = vad_max_level_drop_db
        self._enable_rms_normalize = enable_rms_normalize
        self._rms_target = rms_target
        self._enable_stoi = enable_stoi
        self._stoi_model_path = stoi_model_path
        self._stoi_threshold = stoi_threshold
        self._stoi_chunk_sec = stoi_chunk_sec

    def create(self) -> CompositeAudioProcessor:
        processors = []
        if self._enable_mono:
            processors.append(MonoConverter())
        if self._enable_resample:
            processors.append(Resampler(target_sample_rate=self._target_sample_rate))
        if self._enable_high_pass:
            processors.append(HighPassFilter(cutoff_hz=self._high_pass_cutoff_hz))
        if self._enable_noise_reduce:
            processors.append(NoiseReducer(stationary=self._noise_reduce_stationary))
        if self._enable_vad:
            processors.append(VoiceActivityFilter(
                min_duration_sec=self._vad_min_duration_sec,
                min_voice_ratio=self._vad_min_voice_ratio,
                speech_prob_threshold=self._vad_speech_prob_threshold,
                speaker_band=self._vad_speaker_band,
                max_level_drop_db=self._vad_max_level_drop_db,
            ))
        # STOI intelligibility gate — AFTER VAD (only scores clips that already contain
        # speech), BEFORE RMS (score the raw-level signal the model was calibrated on).
        if self._enable_stoi:
            try:
                from .model_store import ensure_stoi_model
                model_path = ensure_stoi_model(self._stoi_model_path)
                processors.append(SpeechIntelligibilityFilter(
                    model_path=model_path,
                    threshold=self._stoi_threshold,
                    chunk_sec=self._stoi_chunk_sec,
                    expected_sample_rate=self._target_sample_rate,
                ))
            except Exception as e:
                _logger.warning(
                    "STOI gate unavailable (model resolve/download failed: %s) — "
                    "skipping the gate", e,
                )
        if self._enable_rms_normalize:
            processors.append(RMSNormalizer(target_rms=self._rms_target))
        return CompositeAudioProcessor(processors)
