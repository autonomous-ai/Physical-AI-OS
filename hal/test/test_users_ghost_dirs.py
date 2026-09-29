"""Ghost user dirs (#425)."""

import json
from pathlib import Path

import pytest

from hal.drivers.sensing.perceptions.processors import facerecognizer_v2
from hal.drivers.voice import music_service as ms
from hal.routes import sensing


@pytest.fixture
def users_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_USERS_DIR", tmp_path)
    return tmp_path


def test_unmatched_label_falls_back_to_unknown(users_dir):
    # "leo" is the literal example from music-suggestion/SKILL.md; nobody is enrolled.
    assert ms.canonicalize_person("leo") == "unknown"


def test_unmatched_label_with_no_users_dir_falls_back_to_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_USERS_DIR", tmp_path / "does-not-exist")
    assert ms.canonicalize_person("leo") == "unknown"


def test_exact_slug_match_on_existing_dir(users_dir):
    (users_dir / "gray").mkdir()
    assert ms.canonicalize_person("Gray") == "gray"


def test_longest_token_match_on_existing_dir(users_dir):
    (users_dir / "gray").mkdir()
    assert ms.canonicalize_person("i am gray") == "gray"


def test_telegram_id_match_via_metadata(users_dir):
    d = users_dir / "gray"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"telegram_id": "123456"}))
    assert ms.canonicalize_person("Some Display Name (123456)") == "gray"


def test_empty_label_is_unknown(users_dir):
    assert ms.canonicalize_person("") == "unknown"


def test_log_play_event_never_creates_ghost_dir(users_dir):
    ms._log_play_event("Weightless Marconi Union", "Weightless", 1.0, 200.0, "end", person="leo")
    assert not (users_dir / "leo").exists()
    files = list((users_dir / "unknown" / "audio_history").glob("*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text().splitlines()[0])
    assert entry["person"] == "unknown"


def test_log_play_event_keeps_enrolled_person(users_dir):
    (users_dir / "gray").mkdir()
    ms._log_play_event("chill acoustic", "Chill", 1.0, 60.0, "user", person="gray")
    assert list((users_dir / "gray" / "audio_history").glob("*.jsonl"))
    assert not (users_dir / "unknown").exists()


@pytest.fixture
def owners_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(facerecognizer_v2, "USERS_DIR", tmp_path)
    monkeypatch.setattr(sensing, "_require_face_recognizer", lambda: None)
    return tmp_path


def _mk(root: Path, name: str, *rel_files: str) -> Path:
    d = root / name
    d.mkdir()
    for rel in rel_files:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return d


def test_evidence_photo(owners_dir):
    assert sensing._has_enrollment_evidence(_mk(owners_dir, "a", "1711929600000.jpg"))


def test_evidence_voice_sample(owners_dir):
    assert sensing._has_enrollment_evidence(_mk(owners_dir, "b", "voice/sample_1.wav"))


def test_evidence_metadata(owners_dir):
    assert sensing._has_enrollment_evidence(_mk(owners_dir, "c", "metadata.json"))


def test_no_evidence_for_log_only_dir(owners_dir):
    d = _mk(owners_dir, "leo", "audio_history/2026-09-16.jsonl", "mood/2026-09-16.jsonl")
    assert not sensing._has_enrollment_evidence(d)


def test_no_evidence_for_empty_voice_dir(owners_dir):
    d = owners_dir / "d"
    (d / "voice").mkdir(parents=True)
    assert not sensing._has_enrollment_evidence(d)


def test_face_owners_skips_ghost_dirs(owners_dir):
    _mk(owners_dir, "leo", "audio_history/2026-09-16.jsonl")
    _mk(owners_dir, "gray", "1711929600000.jpg")
    _mk(owners_dir, "long", "voice/sample_1.wav")
    _mk(owners_dir, ".voice_registry.json")
    resp = sensing.face_owners_detail()
    labels = [p.label for p in resp.persons]
    assert labels == ["gray", "long"]
    assert resp.enrolled_count == 2


def test_face_owners_keeps_unknown_bucket_but_does_not_count_it(owners_dir):
    _mk(owners_dir, "unknown", "mood/2026-09-16.jsonl", "audio_history/2026-09-16.jsonl")
    _mk(owners_dir, "gray", "metadata.json")
    resp = sensing.face_owners_detail()
    labels = [p.label for p in resp.persons]
    assert labels == ["gray", "unknown"]
    assert resp.enrolled_count == 1
    unknown = next(p for p in resp.persons if p.label == "unknown")
    assert unknown.audio_history_days == ["2026-09-16"]
    assert unknown.mood_days == ["2026-09-16"]


def test_face_owners_empty_when_only_ghosts(owners_dir):
    _mk(owners_dir, "leo", "audio_history/2026-09-16.jsonl")
    resp = sensing.face_owners_detail()
    assert resp.persons == []
    assert resp.enrolled_count == 0
