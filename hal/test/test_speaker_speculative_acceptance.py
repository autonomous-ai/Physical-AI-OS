"""Rejected speculative embeddings must not write audio or mutate voice banks."""

import base64
from contextlib import nullcontext
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice.speaker_recognizer import speaker_recognizer as sr


@pytest.mark.parametrize("embedding_error", [False, True])
def test_rejected_embedding_has_no_persistent_side_effects(embedding_error):
    recognizer = sr.SpeakerRecognizer.__new__(sr.SpeakerRecognizer)
    recognizer._api_url = "http://unused"
    recognizer._debug_profile_start = Mock()
    recognizer._debug_stage = lambda *args: nullcontext()
    recognizer._debug = Mock(enabled=True)
    recognizer._prepare_wav_for_embedding = Mock(return_value=(["audio"], 1.0))
    recognizer._call_embedding_api = Mock(return_value=np.array([[1.0, 0.0]]))
    if embedding_error:
        recognizer._call_embedding_api.side_effect = sr.SpeakerRecognizerError("rejected")
    recognizer._save_incoming_audio = Mock()
    recognizer._load_bank = Mock()
    recognizer._start_migration = Mock()
    recognizer._debug_safe_assign_hash = Mock()
    recognizer._maybe_extend_user = Mock()
    wav = sr.pcm16_bytes_to_wav(b"\x00\x00" * 16000, 16000)
    accept = Mock(return_value=False)
    result = recognizer.recognize(base64.b64encode(wav).decode(), accept=accept)
    assert result["discarded"] is True
    recognizer._call_embedding_api.assert_called_once()
    accept.assert_called_once()
    for method in (recognizer._save_incoming_audio, recognizer._load_bank,
                   recognizer._start_migration, recognizer._debug_safe_assign_hash,
                   recognizer._maybe_extend_user, recognizer._debug.record):
        method.assert_not_called()
