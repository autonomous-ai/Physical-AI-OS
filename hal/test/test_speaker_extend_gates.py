"""Admission gates on the speaker auto-extend path."""

import numpy as np
import pytest

from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
    SpeakerRecognizer,
    _runner_up_mean,
)


@pytest.fixture
def recognizer(tmp_path):
    """A recognizer with a temp user dir and the default 0.5 match bar."""
    return SpeakerRecognizer(
        api_url="http://unused", api_key="k", users_dir=tmp_path,
        match_threshold=0.5,
    )


@pytest.fixture
def spy(recognizer, monkeypatch):
    """Record extend attempts instead of writing to disk."""
    written = []
    monkeypatch.setattr(
        recognizer, "_write_extended_sample",
        lambda norm, wav, emb, **kw: written.append(norm) or None,
    )
    return written


@pytest.fixture
def embed(recognizer, monkeypatch):
    """Stub the embedding API and record how it was called."""
    calls = []

    def _fake(audios_b64, *, use_sliding_window=False):
        calls.append((audios_b64, use_sliding_window))
        return np.ones(8, dtype=np.float32) / np.sqrt(8.0)

    monkeypatch.setattr(recognizer, "_call_embedding_api", _fake)
    return calls


def _extend(recognizer, **over):
    """Call _maybe_extend_user with all gates wide open unless overridden."""
    kwargs = dict(
        existing_rows=None,
        duration_s=10.0,         # clears the 2.0s floor
        margin=1.0,              # clears the 0.05 margin floor
        chunk_votes=7,
        num_chunks=7,
        min_chunk_cos=0.9,       # every chunk well above the 0.5 bar
    )
    kwargs.update(over)
    recognizer._maybe_extend_user(
        "leo", ["Y2xlYW5lZC13YXY="], b"RIFFfake", **kwargs,
    )


def test_a_clean_unanimous_turn_is_admitted(recognizer, spy, embed):
    _extend(recognizer)
    assert spy == ["leo"], "a clean turn must still extend"


def test_a_split_vote_is_rejected(recognizer, spy, embed):
    _extend(recognizer, chunk_votes=4, num_chunks=7)
    assert spy == [], "a split vote must never reach the bank"


def test_a_weak_chunk_is_rejected(recognizer, spy, embed):
    _extend(recognizer, min_chunk_cos=0.2)
    assert spy == [], "an unsure chunk must reject the whole turn"


def test_the_single_enrolled_speaker_hole_is_closed(recognizer, spy, embed):
    # One enrolled speaker: every chunk votes for them even at cos 0.2; A2 catches it.
    _extend(recognizer, chunk_votes=7, num_chunks=7, min_chunk_cos=0.2)
    assert spy == [], "unanimity alone must not admit a weak turn"


def test_a_short_single_chunk_turn_is_unchanged(recognizer, spy, embed):
    # <=10s of speech is one chunk; behaviour must be bit-identical to pre-fix.
    _extend(recognizer, chunk_votes=1, num_chunks=1, min_chunk_cos=0.55)
    assert spy == ["leo"], "short turns must be unaffected by the chunk gates"


def test_a_unanimous_near_tie_has_a_real_runner_up():
    # Per-chunk scores 0.01 apart: a winners-only margin would be inf.
    names = ["leo", "mia", "sam"]
    confs = np.array([[0.55, 0.54, 0.53],
                      [0.56, 0.55, 0.54],
                      [0.57, 0.56, 0.55]])
    best_conf = float(confs[:, 0].mean())
    margin = best_conf - _runner_up_mean(confs, names, "leo")
    assert margin == pytest.approx(0.01, abs=1e-6), (
        f"a 0.01 near-tie must surface as a 0.01 margin, got {margin}"
    )
    assert margin < 0.05, "and must therefore fail the 0.05 margin bar"


def test_a_single_enrolled_user_has_no_runner_up():
    # With nobody to compare against, `inf` is the correct margin.
    confs = np.array([[0.80], [0.82], [0.79]])
    assert _runner_up_mean(confs, ["leo"], "leo") == float("-inf")


def test_the_runner_up_ignores_the_winners_own_column():
    confs = np.array([[0.90, 0.10], [0.90, 0.10]])
    assert _runner_up_mean(confs, ["leo", "mia"], "leo") == pytest.approx(0.10)


def test_the_runner_up_is_the_strongest_loser():
    confs = np.array([[0.90, 0.20, 0.60], [0.90, 0.20, 0.60]])
    assert _runner_up_mean(confs, ["leo", "mia", "sam"], "leo") == pytest.approx(0.60)


def test_the_reported_duration_is_the_cleaned_length_not_the_raw_length(
    recognizer, monkeypatch,
):
    # VAD trims a 28s file to 1.5s of speech; the gate must see 1.5.
    pytest.importorskip(
        "scipy", reason="audio_processors requires scipy; run this on-device"
    )
    from hal.drivers.voice.speaker_recognizer import speaker_recognizer as sr_mod
    from hal.drivers.voice.speaker_recognizer.audio_processors.base import Audio

    sr = 16000
    raw = np.zeros(28 * sr, dtype=np.float32)
    raw[: int(1.5 * sr)] = 0.2

    class _TrimToSpeech:
        """Stand-in for the VAD chain: returns only the voiced part."""

        def process(self, audio):
            return Audio(
                waveform=audio.waveform[: int(1.5 * sr)], sample_rate=sr
            )

    monkeypatch.setattr(sr_mod, "_get_audio_processor", lambda: _TrimToSpeech())

    wav = sr_mod._float32_waveform_to_wav_bytes(raw)
    assert sr_mod._wav_duration_s(wav) == pytest.approx(28.0, abs=0.05), (
        "the raw WAV really is 28s -- this is what the gate used to see"
    )

    payload, cleaned_duration_s = recognizer._prepare_wav_for_embedding(wav)
    assert isinstance(payload, list) and payload, "payload shape must not change"
    assert cleaned_duration_s == pytest.approx(1.5, abs=0.05), (
        f"duration must be measured after VAD, got {cleaned_duration_s}"
    )
    assert cleaned_duration_s < 2.0, (
        "and must therefore fail the 2.0s extend floor that 28s cleared"
    )


def test_a_match_carried_only_by_the_extended_tier_cannot_extend(recognizer, spy, embed):
    _extend(recognizer, anchor_cos=0.31)
    assert spy == [], "an extended-carried match must not grow the bank"


def test_a_match_carried_by_the_anchors_still_extends(recognizer, spy, embed):
    _extend(recognizer, anchor_cos=0.72)
    assert spy == ["leo"], "an anchor-carried match must still extend"


def test_a_user_with_no_anchor_rows_does_not_extend(recognizer, spy, embed):
    _extend(recognizer, anchor_cos=float("-inf"))
    assert spy == [], "no anchor evidence means no extend"


def test_an_admitted_sample_records_why_it_was_admitted(recognizer, tmp_path):
    path = recognizer._write_extended_sample(
        "leo", b"RIFFfake", np.ones(8, dtype=np.float32),
        provenance={"min_chunk_cos": 0.61, "anchor_cos": 0.72},
    )
    assert path is not None
    import json
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["min_chunk_cos"] == 0.61
    assert meta["anchor_cos"] == 0.72


def test_deleting_a_sample_removes_its_provenance_too(recognizer):
    path = recognizer._write_extended_sample(
        "leo", b"RIFFfake", np.ones(8, dtype=np.float32),
        provenance={"anchor_cos": 0.72},
    )
    assert path is not None and path.with_suffix(".json").is_file()
    recognizer._delete_sample(path)
    assert not path.is_file(), "the wav must go"
    assert not path.with_suffix(".json").is_file(), "and so must its provenance"


def test_the_stored_vector_comes_from_a_single_shot_call(recognizer, spy, embed):
    _extend(recognizer)
    assert spy == ["leo"], "the turn should have been admitted"
    assert len(embed) == 1, f"expected exactly one embed call, got {len(embed)}"
    payload, sliding = embed[0]
    assert payload == ["Y2xlYW5lZC13YXY="], "must embed the cleaned WAV payload"
    assert sliding is False, (
        "use_sliding_window must be False -- a True call returns per-chunk "
        "vectors, which is the mean-of-chunks behaviour this replaces"
    )


def test_a_rejected_turn_costs_no_embed_call(recognizer, spy, embed):
    _extend(recognizer, chunk_votes=4, num_chunks=7)
    assert spy == [], "gate 1 should have rejected"
    assert embed == [], "a rejected turn must not call the embedding API"


def test_the_diversity_gate_tests_the_vector_being_stored(recognizer, spy, embed):
    # Stub returns ones/sqrt(8); an identical row gives cosine 1.0 -> redundant.
    existing = (np.ones((1, 8), dtype=np.float32) / np.sqrt(8.0))
    _extend(recognizer, existing_rows=existing)
    assert spy == [], "an identical stored row must read as redundant"
    assert len(embed) == 1, "but only after paying for the embedding"


def test_an_embedding_failure_skips_the_extend_without_raising(
    recognizer, spy, monkeypatch,
):
    from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
        EmbeddingAPIUnavailableError,
    )

    def _boom(audios_b64, *, use_sliding_window=False):
        raise EmbeddingAPIUnavailableError("embedding server down")

    monkeypatch.setattr(recognizer, "_call_embedding_api", _boom)
    _extend(recognizer)
    assert spy == [], "a failed embed must not write a sample"


def test_the_provenance_records_the_embedding_mode(recognizer, embed, tmp_path):
    written = {}
    real = recognizer._write_extended_sample

    def _capture(norm, wav, emb, provenance=None):
        written.update(provenance or {})
        return real(norm, wav, emb, provenance=provenance)

    recognizer._write_extended_sample = _capture
    _extend(recognizer)
    assert written.get("embedding_mode") == "single_shot"
