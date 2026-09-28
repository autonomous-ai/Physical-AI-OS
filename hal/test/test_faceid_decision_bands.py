"""Where the face-ID decision bands sit, and why (#429, #299).

Three banks, three bars. The enrolled UPLOADS are a phone photo matched against
the device camera, so the same person scores lower there than camera-to-camera;
they match at FACE_MATCH_THRESHOLD. The two auto-captured banks — a user's
.extended views and the stranger bank — are camera-to-camera and were admitted
by the device's own guess, so both need the higher FACE_EXTENDED_THRESHOLD /
FACE_STRANGER_THRESHOLD bar. Below the owner banks' negative_threshold and below
the stranger bar, a face is corroborated and minted; the stranger bank is NOT
part of the negative test, because with N rows some row is nearly always above
0.2 and a genuinely new person could never mint (#429).
"""

import numpy as np
import pytest

import hal.config as config
import hal.drivers.sensing.perceptions.processors.faceid.recognizer as recognizer_mod
from hal.drivers.sensing.perceptions.models import PersonKind
from hal.drivers.sensing.perceptions.processors.faceid.recognizer import (
    FaceRecognizer,
)


def test_stranger_bank_is_matched_at_least_as_strictly_as_the_extended_bank():
    """Both are auto-captured, same-camera, single-view banks (#429)."""
    assert config.FACE_STRANGER_THRESHOLD >= config.FACE_EXTENDED_THRESHOLD


def test_upload_match_bar_sits_in_the_gap_between_three_quarter_and_frontal():
    """Measured on orange-lamp 2026-09-16: owner frontal 0.60-0.85, owner 3/4
    pose 0.29-0.34, strangers <= 0.28. 0.40 sits in the gap; 0.30 sat inside
    the 3/4 cluster where genuine and impostor overlap."""
    assert 0.35 <= config.FACE_MATCH_THRESHOLD <= 0.45


def test_becoming_a_reference_view_is_stricter_than_being_recognised():
    """Being recognised must not be enough to become a reference view (#299)."""
    assert config.FACE_EXTEND_MIN_ENROLL_SIM > config.FACE_MATCH_THRESHOLD


def test_stranger_bar_leaves_headroom_over_the_measured_false_accepts():
    """#429: stale rows false-accepted the same person at 0.346 / 0.385 / 0.318."""
    assert config.FACE_STRANGER_THRESHOLD - 0.385 >= 0.05


_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
_SHARP_CROP = np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8)


def _basis(n: int = 3, dim: int = 512) -> np.ndarray:
    """``n`` orthonormal rows — the owner row, the stranger row, and a noise
    direction — so a query's cosine to each is exactly the coefficient we give
    it."""
    rng = np.random.default_rng(42)
    q, _ = np.linalg.qr(rng.normal(size=(dim, n)))
    return q.T.astype(np.float32)


def _query(basis: np.ndarray, owner_sim: float, stranger_sim: float) -> np.ndarray:
    rest = max(0.0, 1.0 - owner_sim**2 - stranger_sim**2) ** 0.5
    q = owner_sim * basis[0] + stranger_sim * basis[1] + rest * basis[2]
    return (q / np.linalg.norm(q)).astype(np.float32)


class _StubPipeline:
    """Stands in for _EdgeFacePipeline: one detected face with a chosen
    embedding, passing every quality gate."""

    def __init__(self, embedding: np.ndarray):
        self.embedding = embedding

    def get(self, frame):
        return [
            {
                "bbox": np.array([100, 60, 300, 360], dtype=np.float32),
                "kps": None,
                "det_score": np.float32(0.95),
                "embedding": self.embedding,
                "emotion_box": None,
                "aligned": _SHARP_CROP,
                "landmarks": None,
                "landmark_score": 1.0,
            }
        ]


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """A recogniser with one enrolled user ('long'), one known stranger
    ('stranger_7'), no extended bank, stranger state redirected to tmp."""
    monkeypatch.setattr(recognizer_mod, "STRANGER_STATE_DIR", tmp_path)
    basis = _basis()
    r = FaceRecognizer()
    r._owner_embeddings = basis[0:1].copy()
    r._owner_labels = np.array(["friend_long"])
    r._stranger_embeddings = basis[1:2].copy()
    r._stranger_labels = np.array(["stranger_stranger_7"])
    r._stranger_counter = 7
    r._app = _StubPipeline(_query(basis, 0.0, 0.0))
    return r, basis


def _tick(r: FaceRecognizer, basis: np.ndarray, owner_sim: float, stranger_sim: float):
    r._app.embedding = _query(basis, owner_sim, stranger_sim)
    faces = r.detect(_FRAME)
    assert faces is not None and len(faces) == 1
    return faces[0]


def test_defaults_come_from_config():
    r = FaceRecognizer()
    assert r._threshold == config.FACE_MATCH_THRESHOLD
    assert r._stranger_threshold == config.FACE_STRANGER_THRESHOLD
    assert r._extended_threshold == config.FACE_EXTENDED_THRESHOLD


def test_a_stale_stranger_row_at_0_385_does_not_claim_a_new_person(rec):
    """#429 verbatim: the worst false accept measured on reachy-mini was 0.385
    against a row minted a month earlier for someone else. That must be a new
    person — unsure on the first tick, minted on the second."""
    r, basis = rec
    first = _tick(r, basis, owner_sim=0.05, stranger_sim=0.385)
    assert first.kind == PersonKind.UNSURE
    second = _tick(r, basis, owner_sim=0.05, stranger_sim=0.385)
    assert second.kind == PersonKind.STRANGER
    assert second.person_id == "stranger_8"
    assert len(r._stranger_embeddings) == 2


def test_a_returning_stranger_matches_its_own_row(rec):
    """Same person, same camera, re-sighted: ~0.6 (#429 measured 0.658)."""
    r, basis = rec
    face = _tick(r, basis, owner_sim=0.0, stranger_sim=0.65)
    assert face.kind == PersonKind.STRANGER
    assert face.person_id == "stranger_7"
    assert len(r._stranger_embeddings) == 1


def test_a_freshly_minted_stranger_is_recognised_on_the_next_tick(rec):
    """The whole point of minting: the new row carries the person from then on
    instead of the argmax flipping between stale rows."""
    r, basis = rec
    _tick(r, basis, owner_sim=0.0, stranger_sim=0.30)
    minted = _tick(r, basis, owner_sim=0.0, stranger_sim=0.30)
    assert minted.kind == PersonKind.STRANGER and minted.person_id == "stranger_8"
    again = _tick(r, basis, owner_sim=0.0, stranger_sim=0.30)
    assert again.kind == PersonKind.STRANGER and again.person_id == "stranger_8"
    assert len(r._stranger_embeddings) == 2


def test_stranger_score_between_negative_and_stranger_bar_still_mints(rec):
    """The second half of #429: raising the stranger bar alone would have moved
    people from 'wrong stranger' to 'never a stranger'. A stranger score in
    (negative_threshold, stranger_threshold) with no owner evidence is a new
    person, not a permanent '?'."""
    r, basis = rec
    _tick(r, basis, owner_sim=0.0, stranger_sim=0.30)
    face = _tick(r, basis, owner_sim=0.0, stranger_sim=0.30)
    assert face.kind == PersonKind.STRANGER
    assert face.person_id == "stranger_8"


def test_an_upload_score_in_the_three_quarter_band_is_unsure_not_friend(rec):
    """0.35 against the upload was a FRIEND at the old 0.30 bar. It is the 3/4
    cluster (orange-lamp: 0.29-0.34), where a genuine off-angle frame and a
    similar-looking stranger overlap — so it is neither named nor minted."""
    r, basis = rec
    for _ in range(3):
        face = _tick(r, basis, owner_sim=0.35, stranger_sim=0.0)
        assert face.kind == PersonKind.UNSURE
    assert len(r._stranger_embeddings) == 1


def test_an_upload_above_the_match_bar_is_a_friend(rec):
    r, basis = rec
    face = _tick(r, basis, owner_sim=0.50, stranger_sim=0.0)
    assert face.kind == PersonKind.FRIEND
    assert face.person_id == "long"


def test_owner_evidence_above_negative_never_mints_a_stranger(rec):
    """A face that resembles an enrolled user at 0.25 is not 'nobody we know',
    however it scores against the stranger bank. Three ticks, no new row."""
    r, basis = rec
    for _ in range(3):
        face = _tick(r, basis, owner_sim=0.25, stranger_sim=0.30)
        assert face.kind == PersonKind.UNSURE
    assert len(r._stranger_embeddings) == 1


def test_the_stranger_bar_is_read_from_the_constructor(rec):
    """The knob is a knob: a device that sets HAL_FACE_STRANGER_THRESHOLD=0.30
    gets the old matching back."""
    r, basis = rec
    r._stranger_threshold = 0.30
    face = _tick(r, basis, owner_sim=0.0, stranger_sim=0.385)
    assert face.kind == PersonKind.STRANGER and face.person_id == "stranger_7"


def test_face_carries_the_five_keypoints_flat(rec):
    """#531: the stranger greeting measures head yaw from SCRFD's 5 points."""
    r, basis = rec
    kps = np.array(
        [[150, 150], [250, 150], [200, 200], [160, 260], [240, 260]], dtype=np.float32
    )
    original_get = r._app.get

    def get_with_kps(frame):
        dets = original_get(frame)
        dets[0]["kps"] = kps
        return dets

    r._app.get = get_with_kps
    face = _tick(r, basis, owner_sim=0.0, stranger_sim=0.65)
    assert face.kps == [150.0, 150.0, 250.0, 150.0, 200.0, 200.0, 160.0, 260.0, 240.0, 260.0]


def test_face_kps_is_none_without_keypoints(rec):
    r, basis = rec
    face = _tick(r, basis, owner_sim=0.0, stranger_sim=0.65)
    assert face.kps is None
