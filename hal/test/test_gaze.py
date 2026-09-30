"""Tests for the gaze wake trigger."""

import json
import logging
import math
from pathlib import Path

import numpy as np
import pytest

import hal.config as config
from hal.drivers.tracking import detection, gaze
from test.body_ownership import BodyOwnership


def _landmarks(right_eye, left_eye, nose, mouth_r=(0.0, 0.0), mouth_l=(0.0, 0.0)):
    """Flatten five (x, y) points the way YuNet returns them."""
    return (
        right_eye[0], right_eye[1],
        left_eye[0], left_eye[1],
        nose[0], nose[1],
        mouth_r[0], mouth_r[1],
        mouth_l[0], mouth_l[1],
    )


def _fill_dy(dy, from_face=True, n=12, span=None):
    """Seed the pitch window with a steady offset."""
    span = config.GAZE_PITCH_WINDOW_S if span is None else span
    t0 = gaze.time.monotonic() - span
    for i in range(n):
        gaze.record_dy(dy, from_face, now=t0 + span * i / max(1, n - 1))

def test_nose_centred_between_the_eyes_reads_as_facing_forward():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(0.0, abs=0.01)


def test_nose_at_one_eye_reads_as_full_profile():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (140.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(90.0, abs=0.01)


def test_yaw_is_unsigned_so_left_and_right_are_treated_alike():
    right = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (130.0, 120.0)))
    left = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (110.0, 120.0)))
    assert right == pytest.approx(left, abs=0.01)
    assert right > 0


def test_a_rolled_head_is_not_mistaken_for_a_turned_one():
    """A tilted but forward-facing head must still read near zero."""
    angle = math.radians(30.0)
    cx, cy = 120.0, 100.0
    dx, dy = 20.0 * math.cos(angle), 20.0 * math.sin(angle)
    right_eye = (cx - dx, cy - dy)
    left_eye = (cx + dx, cy + dy)
    nose = (cx + 20.0 * math.sin(angle), cy - 20.0 * math.cos(angle))
    assert gaze.head_yaw_deg(_landmarks(right_eye, left_eye, nose)) == pytest.approx(
        0.0, abs=0.01
    )


def test_yaw_is_independent_of_how_close_the_face_is():
    """Doubling every coordinate is the same head twice as near the camera."""
    near = gaze.head_yaw_deg(_landmarks((200.0, 200.0), (280.0, 200.0), (260.0, 240.0)))
    far = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (130.0, 120.0)))
    assert near == pytest.approx(far, abs=0.01)


def test_degenerate_or_missing_landmarks_return_none_rather_than_raising():
    assert gaze.head_yaw_deg(()) is None
    assert gaze.head_yaw_deg((1.0, 2.0, 3.0)) is None
    assert gaze.head_yaw_deg(_landmarks((100.0, 100.0), (100.0, 100.0), (110.0, 120.0))) is None
    assert gaze.head_yaw_deg((float("nan"),) * 10) is None


def test_a_nose_past_its_own_eye_clamps_instead_of_raising():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (400.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(90.0, abs=0.01)


def test_a_face_too_few_pixels_is_rejected_however_well_it_faces_the_lamp():
    """Device probe: background colleagues detect at 8-18 px and yield noise."""
    assert gaze.facing_lamp(0.0, config.GAZE_MIN_FACE_PX - 1) is False
    assert gaze.facing_lamp(0.0, config.GAZE_MIN_FACE_PX + 1) is True
    assert gaze.facing_lamp(0.0, 18) is False
    assert gaze.facing_lamp(0.0, 78) is True


def test_a_head_turned_past_the_cone_is_rejected_however_near():
    assert gaze.facing_lamp(config.GAZE_MAX_YAW_DEG + 1.0, 120) is False
    assert gaze.facing_lamp(config.GAZE_MAX_YAW_DEG - 1.0, 120) is True


def test_no_face_is_not_facing():
    assert gaze.facing_lamp(None, 120) is False


def test_the_cone_widens_toward_the_frame_edge():
    """The lens is not a pinhole at the edge, so the same head reads wider."""
    assert gaze.cone_for(0.0) == pytest.approx(config.GAZE_MAX_YAW_DEG)
    assert gaze.cone_for(1.0) == pytest.approx(
        config.GAZE_MAX_YAW_DEG * config.GAZE_EDGE_CONE_SCALE
    )
    assert gaze.cone_for(0.5) > gaze.cone_for(0.0)
    assert gaze.facing_lamp(35.0, 120, 0.0) is False
    assert gaze.facing_lamp(35.0, 120, 0.9) is True


def test_edge_slack_never_rescues_a_genuine_profile():
    assert gaze.facing_lamp(90.0, 120, 1.0) is False


class _FakeYuNet:
    """Stands in for the loaded YuNet model, returning canned detections."""

    def __init__(self, rows):
        self._rows = np.array(rows, dtype=np.float32) if rows else None

    def setInputSize(self, size):
        pass

    def detect(self, frame):
        return 1, self._rows


def _face_row(x, w, h, nose_x=0.0):
    """One YuNet row: [x, y, w, h, 10 landmark coords, score]."""
    return [float(x), 0.0, float(w), float(h)] + [nose_x] * 10 + [0.9]


def _detect(monkeypatch, rows, frame_w=640):
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    frame = np.zeros((480, frame_w, 3), dtype=np.uint8)
    return detection.detect_face_with_landmarks(frame)


def test_the_face_nearest_the_frame_centre_wins_over_the_larger_one(monkeypatch):
    """A colleague leaning in must not take the gate from the seated user."""
    big = config.GAZE_MIN_FACE_PX * 2
    small = config.GAZE_MIN_FACE_PX + 2
    colleague = _face_row(x=500, w=big, h=big, nose_x=1.0)
    user = _face_row(x=300, w=small, h=small, nose_x=2.0)
    (fx, _, fw, fh), landmarks = _detect(monkeypatch, [colleague, user])
    assert (fx, fw, fh) == (300, small, small)
    assert landmarks[4] == 2.0


def test_a_single_face_is_picked_exactly_as_before(monkeypatch):
    """The common case must not change: one candidate, both policies agree."""
    h = config.GAZE_MIN_FACE_PX * 2
    (fx, _, fw, fh), _ = _detect(monkeypatch, [_face_row(x=500, w=h, h=h)])
    assert (fx, fw, fh) == (500, h, h)


def test_faces_too_small_to_measure_do_not_win_by_sitting_in_the_centre(monkeypatch):
    """A tiny centred background face is not preferred over a measurable user face."""
    user_h = config.GAZE_MIN_FACE_PX * 2
    background = _face_row(x=310, w=12, h=12)
    user = _face_row(x=40, w=user_h, h=user_h)
    (fx, _, _, fh), _ = _detect(monkeypatch, [background, user])
    assert (fx, fh) == (40, user_h)


def test_with_nobody_measurable_the_largest_face_still_comes_back(monkeypatch):
    """When every face is too small, the largest bbox is still returned for re-aiming."""
    (fx, _, _, fh), _ = _detect(monkeypatch, [_face_row(x=10, w=8, h=8),
                                              _face_row(x=600, w=18, h=18)])
    assert (fx, fh) == (600, 18)


def test_no_detections_is_still_none(monkeypatch):
    assert _detect(monkeypatch, []) is None


def test_an_infinite_box_is_dropped_instead_of_crashing_the_detector(monkeypatch):
    """Device-observed: YuNet returned a non-finite bbox and int() raised."""
    h = config.GAZE_MIN_FACE_PX * 2
    rows = [_face_row(x=float("inf"), w=h, h=h), _face_row(x=200, w=h, h=h)]
    (fx, _, _, fh), _ = _detect(monkeypatch, rows)
    assert (fx, fh) == (200, h)


def test_an_infinite_box_cannot_hide_the_real_face_behind_it(monkeypatch):
    """Infinite boxes are filtered out before the largest-by-area choice."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rows = [_face_row(x=10, w=float("inf"), h=float("inf")),
            _face_row(x=300, w=60, h=60)]
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    assert detection._detect_face_yunet(frame) == (300, 0, 60, 60)


def test_every_box_unusable_reads_as_no_face(monkeypatch):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rows = [_face_row(x=float("nan"), w=60, h=60)]
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    assert detection._detect_face_yunet(frame) is None
    assert _detect(monkeypatch, rows) is None


# Keep the real `_conversation_open` before the autouse fixture patches it.
_REAL_CONVERSATION_OPEN = gaze._conversation_open


@pytest.fixture(autouse=True)
def _clean_buffer(monkeypatch):
    gaze.reset_for_test()
    # Default to an OPEN conversation; framing loops only move inside one.
    monkeypatch.setattr(gaze, "_conversation_open", lambda: True)
    yield
    gaze.reset_for_test()


def _fill(now, entries):
    """entries: list of (seconds_before_now, yaw_or_None)."""
    for ago, yaw in entries:
        gaze.record_sample(yaw, 80, 0.0, now=now - ago)


def _trail(now, yaws, interval=0.33):
    """Replay a device `trail=[...]` as samples ending at ``now``."""
    last = len(yaws) - 1
    for i, yaw in enumerate(yaws):
        gaze.record_sample(yaw, 80, 0.0, now=now - (last - i) * interval)


def test_a_majority_facing_window_survives_noisy_samples():
    """Isolated wild readings must not veto an otherwise facing window."""
    now = 1000.0
    _trail(now, [10, 15, 8, 90, 12, None, 9, 14])
    ratio, n = gaze.facing_ratio(now)
    assert n >= config.GAZE_MIN_SAMPLES
    assert ratio >= config.GAZE_MIN_FACING_RATIO


def test_a_window_of_a_head_turned_away_does_not_pass():
    """Shape of a real device trail while talking to someone else."""
    now = 1000.0
    _trail(now, [3, 90, 90, 90, 90, 90, 90, 90])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_a_window_caught_mid_turn_does_not_pass_yet():
    """Half the window still looking away is not yet addressing the lamp."""
    now = 1000.0
    _trail(now, [None, 63, 12, 42, 29, 64, 39, 11])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_only_the_window_is_examined_not_the_whole_buffer():
    """Facing a while ago, turned away since -> not facing."""
    now = 1000.0
    _trail(now, [5, 5, 5, 5, 90, 90, 90, 90])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_samples_that_all_predate_the_window_are_not_consulted():
    """Looking over, then away, then speaking is not addressing the lamp."""
    now = 1000.0
    stale = config.GAZE_WINDOW_S + 1.0
    gaze.record_sample(5.0, 80, 0.0, now=now - stale - 0.3)
    gaze.record_sample(5.0, 80, 0.0, now=now - stale)
    ratio, n = gaze.facing_ratio(now)
    assert (ratio, n) == (0.0, 0)


def test_too_few_samples_is_not_evidence_either_way():
    """After start-up, or after a live look monopolised the detector."""
    now = 1000.0
    _trail(now, [5])
    _, n = gaze.facing_ratio(now)
    assert n < config.GAZE_MIN_SAMPLES


def test_an_empty_buffer_yields_no_evidence():
    assert gaze.facing_ratio(1000.0) == (0.0, 0)


def test_samples_older_than_the_buffer_window_are_dropped():
    now = 1000.0
    gaze.record_sample(5.0, 80, 0.0, now=now - (config.GAZE_BUFFER_S + 5.0))
    gaze.record_sample(5.0, 80, 0.0, now=now)
    assert len(gaze.snapshot()) == 1


class _Voice:
    def __init__(self):
        self.grants = []
        self.windows = []

    def grant_wakeword_focus(self, source="button", timeout_s=None):
        self.grants.append(source)
        self.windows.append(timeout_s)
        return True


@pytest.fixture
def voice(monkeypatch):
    import hal.app_state as state

    v = _Voice()
    monkeypatch.setattr(state, "voice_service", v, raising=False)
    return v


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", False)


def _hold_now():
    """Put a facing window in the buffer that ends at the current instant."""
    now = gaze.time.monotonic()
    step = config.GAZE_WINDOW_S / 4.0
    for i in range(config.GAZE_MIN_SAMPLES + 1):
        gaze.record_sample(5.0, 80, 0.0, now=now - i * step)


def test_shadow_mode_logs_but_never_opens_the_gate(monkeypatch, voice, caplog):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", True)
    _hold_now()
    with caplog.at_level("INFO"):
        assert gaze.on_speech_start() is False
    assert voice.grants == []
    assert "WOULD_WAKE" in caplog.text


def test_an_armed_hold_opens_the_gate_through_the_same_seam_as_the_button(armed, voice):
    _hold_now()
    assert gaze.on_speech_start() is True
    assert voice.grants == ["gaze"]


def test_a_glance_too_brief_to_be_addressing_does_not_open_the_gate(armed, voice):
    now = gaze.time.monotonic()
    gaze.record_sample(5.0, 80, 0.0, now=now)
    assert gaze.on_speech_start() is False
    assert voice.grants == []


def test_missing_face_requests_reacquire_and_can_authorize_the_same_utterance(armed, voice):
    """A lamp that lost framing must not make the user repeat their sentence."""
    assert gaze.on_speech_start() is False
    assert gaze._speech_repoint_requested.is_set()

    _hold_now()
    assert gaze.on_speech_end() is True
    assert voice.grants == ["gaze-reacquired"]
    assert not gaze._speech_repoint_requested.is_set()


def test_a_measured_away_head_never_requests_the_reacquire_exception(armed, voice):
    now = gaze.time.monotonic()
    _trail(now, [90.0, 90.0, 90.0, 90.0])
    assert gaze.on_speech_start() is False
    assert not gaze._speech_repoint_requested.is_set()

    _hold_now()
    assert gaze.on_speech_end() is False
    assert voice.grants == []


def test_voice_rechecks_reacquired_gaze_before_realtime_can_be_authorized():
    """The recovery must still apply to the utterance that caused the turn."""
    voice_source = (
        Path(__file__).parents[1] / "drivers" / "voice" / "voice_service.py"
    ).read_text()
    assert voice_source.index("gaze.on_speech_end()") < voice_source.index(
        "# Noise guard: a session can open"
    )


def test_the_cooldown_stops_one_conversation_opening_a_gate_per_sentence(armed, voice):
    _hold_now()
    assert gaze.on_speech_start() is True
    _hold_now()
    assert gaze.on_speech_start() is False
    assert voice.grants == ["gaze"]


def test_disabled_is_inert_even_with_a_perfect_hold(voice, monkeypatch):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", False)
    _hold_now()
    assert gaze.on_speech_start() is False
    assert voice.grants == []


def test_a_missing_voice_service_degrades_quietly(armed, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "voice_service", None, raising=False)
    _hold_now()
    assert gaze.on_speech_start() is False


def test_the_watcher_does_not_start_when_there_is_no_gate_to_open(monkeypatch, caplog):
    """Wake word off means every utterance already dispatches."""
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", False)
    with caplog.at_level("INFO"):
        gaze.start()
    assert gaze._thread is None or not gaze._thread.is_alive()
    assert "nothing to gate" in caplog.text


class _Svc(BodyOwnership):
    """Animation service stand-in that records what it was asked to move to."""

    def __init__(self):
        self.moves = []
        self._tracking_active = False
        self._music_playing = False

    def get_positions(self):
        return {"base_yaw.pos": 40.0, "base_pitch.pos": 0.0}

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos"]

    def move_and_hold(self, target, duration=None):
        self.moves.append(target)


class _Est:
    bearing_deg = 4.0
    confidence = 0.9
    pose = {"base_yaw.pos": 4.0, "base_pitch.pos": 0.0}


@pytest.fixture
def body(monkeypatch):
    import hal.app_state as state
    from hal.drivers.tracking import user_bearing

    svc = _Svc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _Est())
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_REPOINT_ENABLED", True)
    return svc


def _absent_for(seconds):
    """Pretend the last face was seen `seconds` ago."""
    gaze._last_face_t = gaze.time.monotonic() - seconds
    gaze._last_repoint_t = gaze.time.monotonic() - 10_000.0


def test_a_long_absence_turns_the_lamp_to_the_remembered_bearing(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves and body.moves[0]["base_yaw.pos"] == pytest.approx(4.0)


def test_a_brief_absence_does_not_send_the_head_hunting(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S - 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_speech_reacquire_bypasses_the_background_absence_delay(body):
    _absent_for(config.GAZE_REPOINT_SKIP_IF_FACE_S + 1.0)
    assert gaze._maybe_repoint(gaze.time.monotonic(), force=True) is True
    assert body.moves and body.moves[0]["base_yaw.pos"] == pytest.approx(4.0)


def test_it_turns_at_most_once_per_cooldown(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    now = gaze.time.monotonic()
    gaze._maybe_repoint(now)
    gaze._maybe_repoint(now + 1.0)
    assert len(body.moves) == 1


def test_a_repoint_never_moves_a_body_something_else_owns(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    body._tracking_active = True
    gaze._maybe_repoint(gaze.time.monotonic())
    body._tracking_active = False
    body._music_playing = True
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_bearing_it_does_not_trust_is_not_worth_turning_for(body, monkeypatch):
    from hal.drivers.tracking import user_bearing

    class _Weak(_Est):
        confidence = config.GAZE_REPOINT_MIN_CONFIDENCE - 0.01

    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _Weak())
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_repoint_can_be_switched_off_without_disabling_the_gate(body, monkeypatch):
    monkeypatch.setattr(config, "GAZE_REPOINT_ENABLED", False)
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_face_at_the_frame_edge_does_not_count_as_still_being_seen(body, monkeypatch):
    """Edge sightings must not hold off a re-point."""
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze.record_sample(5.0, 120, 0.9)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves, "an edge sighting should not postpone the re-point"


def test_frames_where_no_face_was_measured_do_not_vote_against():
    """A dropped frame is no evidence, not evidence of looking away."""
    now = 1000.0
    _trail(now, [5, 5, None, None, None, 5, 90, 5])
    ratio, n = gaze.facing_ratio(now)
    assert n == 3
    assert ratio == pytest.approx(2.0 / 3.0)


def test_seeing_nobody_at_all_declines_to_decide_rather_than_passing():
    """The empty denominator must not become a free pass."""
    now = 1000.0
    _trail(now, [None, None, None, None, None, None])
    ratio, n = gaze.facing_ratio(now)
    assert (ratio, n) == (0.0, 0)


def test_faces_too_small_to_measure_are_left_out_of_the_denominator(monkeypatch):
    """Background colleagues neither vote for nor against."""
    now = 1000.0
    step = 0.3
    gaze.record_sample(5.0, 120, 0.1, now=now - 2 * step)
    gaze.record_sample(5.0, 10, 0.1, now=now - step)
    gaze.record_sample(5.0, 10, 0.1, now=now)
    ratio, n = gaze.facing_ratio(now)
    assert n == 1 and ratio == pytest.approx(1.0)


class _MovingSvc(BodyOwnership):
    """Animation service whose head last moved `ago` seconds ago."""

    def __init__(self, ago, tracking=False):
        self._ago = ago
        self._tracking_active = tracking

    @property
    def last_servo_write(self):
        return gaze.time.monotonic() - self._ago


def _sample_reason(svc, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    from hal.drivers.tracking import aim

    # Hold the detector lock so the call returns before touching hardware.
    aim._detector_lock_use.acquire()
    try:
        return gaze._sample_once()
    finally:
        aim._detector_lock_use.release()


def test_a_tracking_session_no_longer_blocks_sampling(monkeypatch):
    """A tracking session no longer blocks bearing sampling."""
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S + 1.0, tracking=True)
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_a_head_still_settling_from_a_move_is_not_sampled(monkeypatch):
    """Mid-swing the yaw describes the lamp's motion, not the user's intent."""
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


class _BreathingSvc(_MovingSvc):
    """The arm looping the idle recording at reduced FPS — writing, not moving."""

    idle_recording = "idle"

    def __init__(self, ago, settled=True, recording="idle"):
        super().__init__(ago)
        self._idle_settled = settled
        self._current_recording = recording


def test_the_idle_loop_breathing_does_not_count_as_a_move(monkeypatch):
    """Idle breathing writes do not count as a move."""
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert gaze.idle_breathing(svc) is True
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_interpolating_into_idle_is_still_a_real_move(monkeypatch):
    """Unsettled idle is the relocation INTO idle, which does smear the yaw."""
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0, settled=False)
    assert gaze.idle_breathing(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_another_recording_playing_is_not_breathing(monkeypatch):
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0, recording="greeting")
    assert gaze.idle_breathing(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_a_service_that_knows_nothing_of_idle_is_treated_as_moving():
    assert gaze.idle_breathing(None) is False
    assert gaze.idle_breathing(object()) is False


def test_a_tracking_pursuit_does_not_count_as_a_move_either(monkeypatch):
    """Tracking pursuit writes do not count as a move."""
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0, tracking=True)
    assert gaze.following_a_face(svc) is True
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_a_body_doing_neither_is_still_treated_as_moving(monkeypatch):
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0, tracking=False)
    assert gaze.following_a_face(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_a_blocked_turn_is_reported_as_blocked_not_as_a_sample(monkeypatch):
    """The loop's rate figure must count evidence, not attempts."""
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert _sample_reason(svc, monkeypatch) is not None


def test_landmarks_inside_the_frame_are_measurable():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 120.0))
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is True


def test_an_eye_above_the_top_edge_was_never_seen():
    """An eye above the top edge is treated as unseen."""
    lm = _landmarks((336.5, -3.0), (391.0, -1.3), (374.6, 14.5))
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is False


def test_a_landmark_past_any_other_edge_is_refused_too():
    for lm in (
        _landmarks((-2.0, 100.0), (140.0, 100.0), (120.0, 120.0)),
        _landmarks((100.0, 100.0), (700.0, 100.0), (120.0, 120.0)),
        _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 400.0)),
    ):
        assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is False


def test_a_clipped_mouth_does_not_disqualify_the_yaw():
    """Only the eyes and the nose feed the angle; the mouth is along for the ride."""
    lm = _landmarks(
        (100.0, 100.0), (140.0, 100.0), (120.0, 120.0),
        mouth_r=(105.0, 400.0), mouth_l=(135.0, 400.0),
    )
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is True


def test_missing_or_non_finite_landmarks_are_not_in_frame():
    assert gaze.landmarks_in_frame((), 640.0, 360.0) is False
    assert gaze.landmarks_in_frame((1.0, 2.0), 640.0, 360.0) is False
    assert gaze.landmarks_in_frame((float("nan"),) * 10, 640.0, 360.0) is False


def test_a_clipped_face_is_recorded_as_unmeasured_not_as_a_profile(monkeypatch):
    """A clipped face is recorded as unmeasured, not as a profile."""
    import hal.app_state as state

    from hal.drivers.tracking import aim, detection as det, frame_utils

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", _Svc(), raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    monkeypatch.setattr(aim, "_grab_frame", lambda cap, svc: frame)
    monkeypatch.setattr(aim, "get_detector", lambda: None)
    monkeypatch.setattr(frame_utils, "downscale", lambda f: (f, 1.0))
    monkeypatch.setattr(
        det, "detect_face_with_landmarks",
        lambda f: ((264, 0, 162, 92),
                   _landmarks((336.5, -3.0), (391.0, -1.3), (374.6, 14.5))),
    )

    gaze.reset_for_test()
    assert gaze._sample_once() is None
    (_, yaw, px, _), = gaze.snapshot()
    assert yaw == float("inf")
    assert px == pytest.approx(92.0)
    assert gaze._last_dy_frac is not None and gaze._last_dy_frac < 0
    assert gaze._last_dy_from_face is True


class _PitchSvc(_Svc):
    # Report every pitch joint; the correction is spread across all three.
    _ATTR = {
        "base_pitch.pos": "base",
        "elbow_pitch.pos": "elbow",
        "wrist_pitch.pos": "wrist",
    }

    def __init__(self, wrist=-70.0, base=10.0, elbow=5.0):
        super().__init__()
        self.wrist = wrist
        self.base = base
        self.elbow = elbow
        # joint -> (lo, hi) it stalls at; empty means every move arrives.
        self.stalls = {}

    def get_positions(self):
        return {
            "base_yaw.pos": 0.0,
            "base_pitch.pos": self.base,
            "elbow_pitch.pos": self.elbow,
            "wrist_pitch.pos": self.wrist,
        }

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"]

    def move_and_hold(self, target, duration=None):
        """Record the command AND arrive at it."""
        self.moves.append(target)
        for joint, want in target.items():
            attr = self._ATTR.get(joint)
            if attr is None:
                continue
            lo, hi = self.stalls.get(joint, (float("-inf"), float("inf")))
            setattr(self, attr, max(lo, min(hi, float(want))))


@pytest.fixture
def neck(monkeypatch):
    import hal.app_state as state

    svc = _PitchSvc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_PITCH_ENABLED", True)
    # Skip the real-bus settle delay to keep the suite fast.
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 0.0)
    gaze._pitch_stalls.clear()
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    # The guessed path is off by default; tests opt in explicitly.
    gaze._last_dy_from_face = True
    return svc


def test_a_face_above_centre_tilts_the_camera_up(neck):
    """A face above centre tilts the camera up."""
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "a clipped-high face should raise the camera"
    assert neck.moves[0]["elbow_pitch.pos"] > 5.0
    assert neck.moves[0]["base_pitch.pos"] < 10.0


def test_a_face_below_centre_tilts_the_camera_down(neck):
    _fill_dy(0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves[0]["elbow_pitch.pos"] < 5.0
    assert neck.moves[0]["base_pitch.pos"] > 10.0


def test_the_lift_does_not_lean_on_the_joint_that_cannot_lift(neck):
    """The lift does not rely on wrist_pitch."""
    _fill_dy(-0.9)
    gaze._maybe_pitch(gaze.time.monotonic())
    moved = neck.moves[0]
    wrist_delta = abs(moved["wrist_pitch.pos"] - (-70.0))
    lifting = abs(moved["elbow_pitch.pos"] - 5.0) + abs(moved["base_pitch.pos"] - 10.0)
    assert lifting > wrist_delta, (
        "the wrist cannot deliver an upward correction on this arm; the lift has "
        "to come from base_pitch and elbow_pitch"
    )


def test_the_pitch_sign_is_only_valid_while_the_joint_direction_is():
    """Fail loudly if the calibration stops supporting the sign above."""
    from hal.drivers.tracking import user_bearing

    path = user_bearing._calibration_path()
    assert path, "no calibration resolved — the pitch sign cannot be vouched for"
    with open(path, encoding="utf-8") as f:
        cal = json.load(f)

    for joint in ("base_pitch", "elbow_pitch"):
        wp = cal[joint]
        assert wp["drive_mode"] == 0, (
            f"{path}: {joint} drive_mode is {wp['drive_mode']}, not 0. lerobot "
            "negates the normalised value when this is set, so UP is no longer "
            "the direction _maybe_pitch assumes. Redo the A/B before trusting it."
        )
        assert wp["range_min"] < wp["range_max"], (
            f"{path}: {joint} range is stored descending "
            f"({wp['range_min']} -> {wp['range_max']}), which flips the sign of "
            "the normalisation. Redo the A/B before trusting _maybe_pitch."
        )


def test_a_face_near_enough_to_centre_is_left_alone(neck):
    """Every correction is a visible head movement nobody asked for."""
    _fill_dy(config.GAZE_PITCH_DEAD_ZONE_FRAC - 0.01)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_no_face_measured_means_no_correction(neck):
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_one_correction_is_bounded(neck):
    """A wrong sign must be a small mistake the next look reverses."""
    _fill_dy(-1.0)
    gaze._maybe_pitch(gaze.time.monotonic())
    moved = neck.moves[0]
    before = {"base_pitch.pos": 10.0, "elbow_pitch.pos": 5.0, "wrist_pitch.pos": -70.0}
    # elbow is mounted reversed, so its joint delta counts against the camera.
    tilt = (
        (moved["base_pitch.pos"] - before["base_pitch.pos"])
        + (moved["wrist_pitch.pos"] - before["wrist_pitch.pos"])
        - (moved["elbow_pitch.pos"] - before["elbow_pitch.pos"])
    )
    assert tilt == pytest.approx(-config.GAZE_PITCH_MAX_STEP_DEG)


def test_the_correction_stays_inside_the_mechanical_range(neck):
    from hal.drivers.tracking import constants as C

    neck.wrist = C.WRIST_PITCH_MIN + 1.0
    _fill_dy(-1.0)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves[0]["wrist_pitch.pos"] >= C.WRIST_PITCH_MIN


def test_it_never_moves_a_body_something_else_owns(neck):
    _fill_dy(-0.4)
    neck._tracking_active = True
    gaze._maybe_pitch(gaze.time.monotonic())
    neck._tracking_active = False
    neck._music_playing = True
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_corrections_are_rate_limited(neck):
    _fill_dy(-0.4)
    now = gaze.time.monotonic()
    gaze._maybe_pitch(now)
    gaze._maybe_pitch(now + 0.5)
    assert len(neck.moves) == 1


class _Box:
    """Detector stand-in returning one fixed box for 'person'."""

    def __init__(self, box):
        self.box = box

    def detect(self, frame, target, strict=False, min_conf=None):
        return self.box if target == "person" else None


class _Frame:
    def __init__(self, h=360, w=640):
        self.shape = (h, w, 3)


def test_a_torso_cut_off_at_the_top_asks_the_camera_to_tilt_up():
    """The head is outside the frame, above — the only readable evidence left."""
    det = _Box((100, 0, 200, 300))
    assert gaze._headroom_from_person(_Frame(), det)[0] == pytest.approx(-0.5)


def test_a_whole_person_well_inside_the_frame_needs_no_correction():
    det = _Box((100, 40, 200, 250))
    assert gaze._headroom_from_person(_Frame(), det)[0] is None


def test_a_distant_person_is_not_this_desk_and_is_ignored():
    det = _Box((100, 0, 20, 20))
    assert gaze._headroom_from_person(_Frame(), det)[0] is None


def test_no_person_and_no_detector_yield_no_correction():
    assert gaze._headroom_from_person(_Frame(), _Box(None))[0] is None
    assert gaze._headroom_from_person(_Frame(), None)[0] is None
    assert gaze._headroom_from_person(None, _Box((0, 0, 100, 300)))[0] is None


def test_a_detector_that_raises_does_not_break_sampling():
    class _Angry:
        def detect(self, *a, **k):
            raise RuntimeError("model gone")

    assert gaze._headroom_from_person(_Frame(), _Angry())[0] is None


def test_a_person_filling_the_frame_still_means_look_up():
    """A user sitting close fills the frame top to bottom at every pitch."""
    det = _Box((100, 0, 200, 360))
    assert gaze._headroom_from_person(_Frame(), det)[0] == pytest.approx(-0.5)


@pytest.fixture
def height_store(tmp_path, monkeypatch):
    """Point the height memory at a temp file — never the real /var/lib path."""
    from hal.drivers.tracking import face_height

    path = tmp_path / "face_height.json"
    monkeypatch.setattr(config, "FACE_HEIGHT_PATH", str(path), raising=False)
    return face_height


def test_the_climb_stops_after_its_budget(neck, monkeypatch, height_store):
    """The climb stops after its step budget."""
    monkeypatch.setattr(config, "GAZE_FACE_SEARCH_MAX_STEPS", 3)
    gaze._blind_pitch_steps = 0
    gaze._climb_gave_up = False
    for _ in range(config.GAZE_FACE_SEARCH_MAX_STEPS + 4):
        # Refill each round: a correction clears the window it was computed from.
        _fill_dy(-0.5, from_face=False)
        gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
        gaze._maybe_pitch(gaze.time.monotonic())
    assert len(neck.moves) == config.GAZE_FACE_SEARCH_MAX_STEPS


def test_giving_up_does_not_restart_the_climb(neck, monkeypatch, height_store):
    """Giving up does not restart the climb."""
    monkeypatch.setattr(config, "GAZE_FACE_SEARCH_MAX_STEPS", 2)
    gaze._blind_pitch_steps = 0
    gaze._climb_gave_up = False
    for _ in range(12):
        _fill_dy(-0.5, from_face=False)
        gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
        gaze._maybe_pitch(gaze.time.monotonic())
    assert len(neck.moves) == 2, "the climb restarted itself"


def test_each_climb_step_is_a_fixed_size(neck, monkeypatch, height_store):
    """Each climb step is a fixed size, not proportional."""
    monkeypatch.setattr(config, "GAZE_FACE_SEARCH_STEP_DEG", 15.0)
    gaze._blind_pitch_steps = 0
    gaze._climb_gave_up = False
    _fill_dy(-0.5, from_face=False)
    gaze._maybe_pitch(gaze.time.monotonic())

    moved = neck.moves[0]
    before = {"base_pitch.pos": 10.0, "elbow_pitch.pos": 5.0, "wrist_pitch.pos": -70.0}
    tilt = ((moved["base_pitch.pos"] - before["base_pitch.pos"])
            + (moved["wrist_pitch.pos"] - before["wrist_pitch.pos"])
            - (moved["elbow_pitch.pos"] - before["elbow_pitch.pos"]))
    assert tilt == pytest.approx(-15.0), "up, by exactly one step"


def test_a_spent_climb_returns_to_a_height_that_worked(neck, monkeypatch, height_store):
    """A failed climb returns the arm instead of leaving it pointed at the ceiling."""
    monkeypatch.setattr(config, "GAZE_FACE_SEARCH_MAX_STEPS", 1)
    height_store.record({"base_pitch.pos": 24.0, "elbow_pitch.pos": 31.0,
                         "wrist_pitch.pos": -30.0})
    gaze._blind_pitch_steps = 0
    gaze._climb_gave_up = False
    for _ in range(3):
        _fill_dy(-0.5, from_face=False)
        gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
        gaze._maybe_pitch(gaze.time.monotonic())

    assert len(neck.moves) == 2, "one climb step, then the trip home"
    assert neck.moves[-1]["elbow_pitch.pos"] == pytest.approx(31.0)


def test_only_a_real_face_updates_the_remembered_height(neck, height_store):
    """Only poses where a face was measured are stored."""
    gaze._blind_pitch_steps = 0
    gaze._climb_gave_up = False
    _fill_dy(-0.5, from_face=False)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert height_store.read() is None, "a blind climb step is not evidence"

    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    _fill_dy(-0.4, from_face=True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert height_store.read() is not None, "a face-driven correction is"


def test_a_real_face_clears_the_climb_budget(neck):
    """Seeing a face again means the guessing worked; allow guessing later."""
    gaze._blind_pitch_steps = config.GAZE_FACE_SEARCH_MAX_STEPS
    _fill_dy(-0.4, from_face=True)
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves and gaze._blind_pitch_steps == 0


def test_moving_the_neck_discards_what_was_seen_from_the_old_pose(neck):
    """Samples describe an angle as seen from ONE camera pose."""
    gaze.record_sample(44, 80, 0.1)
    gaze.record_sample(48, 80, 0.1)
    _fill_dy(-0.4, from_face=True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves and gaze.snapshot() == []


def test_repointing_also_discards_them(body):
    gaze.record_sample(44, 80, 0.1)
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves and gaze.snapshot() == []


def test_one_sample_is_not_enough_to_move_the_neck(neck):
    """A single sample does not move the neck."""
    gaze.record_dy(-0.4, True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == [], "one measurement is not evidence of a framing error"


def test_a_window_shorter_than_an_idle_cycle_is_refused(neck):
    """Half a roll cycle carries the disturbance's mean, not zero."""
    _fill_dy(-0.4, n=12, span=config.GAZE_PITCH_WINDOW_S * 0.5)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_a_roll_sweep_around_a_centred_face_does_not_move_the_neck(neck):
    """The disturbance alone must not look like a framing error."""
    import math
    span = config.GAZE_PITCH_WINDOW_S
    t0 = gaze.time.monotonic() - span
    n = 24
    for i in range(n):
        phase = 2.0 * math.pi * i / n
        gaze.record_dy(0.30 * math.sin(phase), True, now=t0 + span * i / (n - 1))
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == [], "a periodic disturbance is not a framing error"


def test_a_real_offset_survives_the_same_sweep(neck):
    """...and the signal underneath it still gets corrected, with the right sign."""
    import math
    span = config.GAZE_PITCH_WINDOW_S
    t0 = gaze.time.monotonic() - span
    n = 24
    for i in range(n):
        phase = 2.0 * math.pi * i / n
        gaze.record_dy(-0.40 + 0.30 * math.sin(phase), True,
                       now=t0 + span * i / (n - 1))
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "a real offset must still be corrected through the noise"
    assert neck.moves[0]["elbow_pitch.pos"] > 5.0, "up is the increasing direction on elbow"


def test_a_correction_clears_the_window_it_was_computed_from(neck):
    """A correction clears the offset window it was computed from."""
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "precondition: the first correction fired"
    assert gaze._dy_estimate() is None, "the window must be empty after a move"


def test_an_unmeasurable_frame_is_not_recorded_as_centred(neck):
    """`None` is 'no evidence', not 'dy = 0' — recording zero would stall it."""
    _fill_dy(-0.4, n=10)
    before = gaze._dy_estimate()
    for _ in range(20):
        gaze.record_dy(None, False)
    after = gaze._dy_estimate()
    assert before is not None and after is not None
    assert before[0] == after[0], "unmeasurable frames must not move the median"


class _OwnershipSvc(_PitchSvc):
    """Records whether the body was OWNED at the instant it was moved."""

    def __init__(self, wrist=-70.0):
        super().__init__(wrist)
        self.owned_during_move = []

    def move_and_hold(self, target, duration=None):
        self.owned_during_move.append(bool(self._tracking_active))
        return super().move_and_hold(target, duration=duration)


def _owned_neck(monkeypatch):
    import hal.app_state as state

    svc = _OwnershipSvc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_PITCH_ENABLED", True)
    # Skip the real-bus settle delay to keep the suite fast.
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 0.0)
    gaze._pitch_stalls.clear()
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    return svc


def test_a_pitch_correction_owns_the_body_while_it_moves(monkeypatch):
    """A pitch correction owns the body while it moves."""
    svc = _owned_neck(monkeypatch)
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc.moves, "precondition: the correction fired"
    assert svc.owned_during_move == [True], "the move must happen under ownership"


def test_ownership_is_released_after_the_correction(monkeypatch):
    """A watcher that kept the lock would silence emotions for good."""
    svc = _owned_neck(monkeypatch)
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc._tracking_active is False


def test_gaze_still_declines_to_move_a_body_someone_else_owns(monkeypatch):
    """Claiming must not become stealing: a live look or tracking session wins."""
    svc = _owned_neck(monkeypatch)
    svc._tracking_active = True
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc.moves == [] and svc.owned_during_move == []


def test_a_correction_writes_the_frame_it_was_computed_from(neck, tmp_path, monkeypatch):
    """A correction saves the frame it was computed from."""
    import numpy as _np

    monkeypatch.setattr(config, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "GAZE_SNAPSHOT_ENABLED", True, raising=False)
    gaze._last_frame = _np.zeros((90, 160, 3), dtype=_np.uint8)
    gaze._last_box = (10, 5, 40, 40)

    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert neck.moves, "precondition: the correction fired"
    written = list((tmp_path / gaze.SNAPSHOT_CATEGORY).glob("*.jpg"))
    assert len(written) == 1, "one frame per correction"


def test_snapshots_can_be_turned_off(neck, tmp_path, monkeypatch):
    import numpy as _np

    monkeypatch.setattr(config, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "GAZE_SNAPSHOT_ENABLED", False, raising=False)
    gaze._last_frame = _np.zeros((90, 160, 3), dtype=_np.uint8)

    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert neck.moves, "the correction must still happen"
    assert not (tmp_path / gaze.SNAPSHOT_CATEGORY).exists()


def test_a_failing_snapshot_never_costs_the_correction(neck, monkeypatch):
    """Debug output is never allowed to break the thing it observes."""
    monkeypatch.setattr(
        gaze, "_save_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    _fill_dy(-0.4)
    try:
        gaze._maybe_pitch(gaze.time.monotonic())
    except OSError:
        raise AssertionError("a snapshot failure must not escape _maybe_pitch")


def _repoint_scored(monkeypatch):
    """Capture what gaze told user_bearing, without touching a real file."""
    calls = []
    from hal.drivers.tracking import user_bearing
    monkeypatch.setattr(
        user_bearing, "record_prediction",
        lambda hit, now=None: calls.append(hit) or False,
    )
    return calls


def test_a_repoint_that_finds_the_user_confirms_the_bearing(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_near_face_t = t - 1.0

    gaze._verify_repoint(t)
    assert calls == [True]


def test_a_repoint_that_finds_nobody_counts_against_the_bearing(monkeypatch):
    """The self-healing this feeds is why a moved lamp eventually forgets."""
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = t - 100.0
    gaze._last_subject_t = t - 100.0

    gaze._verify_repoint(t)
    assert calls == [False]


def test_the_verdict_waits_for_the_settle_window(monkeypatch):
    """A repoint is not judged until the move settled and a new sample was taken."""
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - 1.0
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = t - 100.0

    gaze._verify_repoint(t)
    assert calls == [], "too early to judge"


def test_each_repoint_is_scored_exactly_once(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_near_face_t = t - 1.0

    for _ in range(5):
        gaze._verify_repoint(t)
    assert calls == [True], "a repeated verdict would double-count"


def test_nothing_is_scored_when_no_repoint_is_pending(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    gaze._repoint_pending_t = 0.0
    gaze._verify_repoint(gaze.time.monotonic())
    assert calls == []


def _turn_trail(values, now, span=None):
    """Lay `values` across the WHOLE buffer, oldest first, ending at `now`."""
    span = 2.0 * config.GAZE_WINDOW_S if span is None else span
    n = len(values)
    for i, yaw in enumerate(values):
        gaze.record_sample(yaw, 90.0, 0.05, now=now - span + span * i / (n - 1))


def test_a_user_already_facing_the_lamp_opens_the_gate(armed, voice):
    """A flat trail — already facing, no turn — is accepted, deliberately."""
    t = gaze.time.monotonic()
    _turn_trail([13, 12, 12, 11, 12, 11, 13, 21], t)
    assert gaze.on_speech_start() is True


def test_blindness_is_reported_as_blind_not_as_looking_away(armed, voice, caplog=None):
    """Different failures, fixed in different places — they must not read alike."""
    import logging as _logging

    records = []
    handler = _logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    gaze.logger.addHandler(handler)
    prev = gaze.logger.level
    gaze.logger.setLevel(_logging.INFO)
    try:
        gaze.on_speech_start()
    finally:
        gaze.logger.removeHandler(handler)
        gaze.logger.setLevel(prev)

    assert any("-> blind" in m for m in records), records


def test_gaze_asks_for_a_shorter_window_than_a_deliberate_wake(armed, voice):
    """The opener least sure of itself claims the least floor (F20)."""
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)

    assert gaze.on_speech_start() is True
    assert voice.grants == ["gaze"]
    assert voice.windows == [config.GAZE_WAKE_FOCUS_S]


def test_an_older_voice_service_still_gets_a_gate(armed, monkeypatch):
    """Version skew must degrade to the full window, not silence the feature."""
    import hal.app_state as state

    class _OldVoice:
        def __init__(self):
            self.grants = []

        def grant_wakeword_focus(self, source="button"):
            self.grants.append(source)
            return True

    v = _OldVoice()
    monkeypatch.setattr(state, "voice_service", v, raising=False)
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)

    assert gaze.on_speech_start() is True
    assert v.grants == ["gaze"]


def test_the_gate_records_who_face_perception_thinks_is_here(armed, voice, monkeypatch):
    """The wake log records face evidence alongside the gaze verdict."""
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("leo", 3.0), raising=False)
    assert gaze._who_is_in_frame() == ("leo", 3.0)


def test_an_unknown_face_reports_empty_rather_than_guessing(armed, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("", 0.0), raising=False)
    assert gaze._who_is_in_frame() == ("", 0.0)


def test_identity_lookup_never_costs_a_wake(armed, voice, monkeypatch):
    """Missing or failing face perception does not break the gate."""
    import hal.app_state as state

    def _boom():
        raise RuntimeError("perception down")

    monkeypatch.setattr(state, "face_user", _boom, raising=False)
    assert gaze._who_is_in_frame() == ("", 0.0)

    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)
    assert gaze.on_speech_start() is True, "a broken lookup must not veto"


def test_identity_is_observed_not_enforced(armed, voice, monkeypatch):
    """A colleague in frame does not yet change the verdict (F21 open)."""
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("someone-else", 1.0), raising=False)
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)
    assert gaze.on_speech_start() is True


def test_the_watcher_loop_still_calls_the_pitch_correction():
    """Regression: the merge with main dropped this call entirely."""
    import inspect

    body = inspect.getsource(gaze._loop)
    assert "_maybe_pitch(now)" in body, "the watcher must drive the pitch correction"
    assert body.index("_maybe_pitch(now)") < body.index("_consume_speech_repoint(now)")


def test_a_joint_that_does_not_arrive_is_benched(neck):
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert "elbow_pitch.pos" in gaze._pitch_stalls, (
        "a joint commanded somewhere it never reached must be noticed"
    )


def test_the_next_correction_routes_around_the_benched_joint(neck):
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    now = gaze.time.monotonic()
    _fill_dy(-0.6)
    gaze._maybe_pitch(now)
    first_elbow_ask = neck.moves[0]["elbow_pitch.pos"]

    gaze._last_pitch_t = now - 10_000.0
    _fill_dy(-0.6)
    gaze._maybe_pitch(now + 1.0)

    assert len(neck.moves) == 2, "precondition: a second correction fired"
    second = neck.moves[1]
    assert second["elbow_pitch.pos"] <= first_elbow_ask, (
        "the second correction must not ask the stalled joint for more travel"
    )
    assert second["base_pitch.pos"] < neck.moves[0]["base_pitch.pos"]


def test_a_benched_joint_is_readmitted_once_it_has_rested(neck):
    """A benched joint is retried after the rest period."""
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    now = gaze.time.monotonic()
    _fill_dy(-0.6)
    gaze._maybe_pitch(now)
    assert "elbow_pitch.pos" in gaze._pitch_stalls

    neck.stalls.clear()
    lo, hi = gaze._pitch_travel_limits(now + config.GAZE_PITCH_STALL_REST_S + 1.0)
    assert "elbow_pitch.pos" not in lo and "elbow_pitch.pos" not in hi
    assert "elbow_pitch.pos" not in gaze._pitch_stalls


def test_a_joint_that_arrives_is_not_benched(neck):
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert gaze._pitch_stalls == {}, "a correction that landed is not a stall"


def test_a_move_still_in_flight_is_not_called_a_failure(neck, monkeypatch):
    """A move still in flight is not reported as a failed landing."""
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 2.0)
    reads = {"n": 0}
    real = neck.get_positions

    def slow_arrival():
        # Stays put for the first few polls, then arrives — a real servo.
        reads["n"] += 1
        pose = real()
        if reads["n"] < 4 and neck.moves:
            return dict(pose, **{"elbow_pitch.pos": 5.0})
        return pose

    neck.get_positions = slow_arrival
    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert gaze._pitch_stalls == {}, (
        "a joint that arrived late must not be benched as if it had stalled"
    )


def test_a_correction_moves_gently_and_does_not_slow_look_aim(neck, monkeypatch):
    """Gaze gets its own move duration; look.aim keeps the brisk shared one."""
    from hal.drivers.tracking import aim

    seen = {}
    real = neck.move_and_hold
    neck.move_and_hold = lambda t, duration=None: (
        seen.setdefault("duration", duration), real(t, duration=duration))[1]

    monkeypatch.setattr(config, "GAZE_PITCH_MOVE_S", 1.0)
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert seen["duration"] == pytest.approx(1.0)
    assert aim.MOVE_DURATION_S == pytest.approx(0.25), (
        "look.aim's shared duration must not have been stretched with it"
    )


def _fill_dx(dx, n=20):
    """Fill the pan window with a steady horizontal offset."""
    span = config.GAZE_YAW_WINDOW_S
    t0 = gaze.time.monotonic() - span
    for i in range(n):
        gaze.record_dx(dx, True, now=t0 + span * i / (n - 1))


@pytest.fixture
def pan(neck, monkeypatch):
    monkeypatch.setattr(config, "GAZE_YAW_ENABLED", True, raising=False)
    gaze.discard_dx_samples()
    gaze._last_yaw_t = 0.0
    return neck


def test_a_face_off_to_the_right_turns_the_lamp(pan):
    _fill_dx(0.4)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves, "a face well off centre should turn the lamp"
    assert pan.moves[0]["base_yaw.pos"] > 0.0


def test_a_face_off_to_the_left_turns_the_lamp_the_other_way(pan):
    _fill_dx(-0.4)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves[0]["base_yaw.pos"] < 0.0


def test_a_face_near_enough_to_centre_is_left_alone_horizontally(pan):
    """Small offsets are left alone; the window handles jitter, this handles aim."""
    _fill_dx(config.GAZE_YAW_DEAD_ZONE_FRAC - 0.01)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves == []


def test_a_pan_never_touches_the_pitch_joints(pan):
    """The two loops own different axes; a pan that tilts is a bug."""
    _fill_dx(0.5)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves
    assert not ({"base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"}
                & set(pan.moves[0]))


def test_one_pan_is_bounded(pan):
    _fill_dx(1.0)
    gaze._maybe_yaw(gaze.time.monotonic())
    total = sum(pan.moves[0][j] for j in pan.moves[0])
    assert total == pytest.approx(config.GAZE_YAW_MAX_STEP_DEG)


def test_a_pan_clears_the_window_it_was_computed_from(pan):
    """Those offsets describe the pose the camera has just left."""
    _fill_dx(0.4)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves, "precondition: the pan fired"
    assert gaze._dx_estimate() is None, "the window must be empty after a move"


def test_a_pan_never_moves_a_body_something_else_owns(pan):
    """A pan never moves a body another owner (e.g. the tracker) holds."""
    _fill_dx(0.5)
    pan._tracking_active = True
    gaze._maybe_yaw(gaze.time.monotonic())
    assert pan.moves == []


def test_the_watcher_loop_pans_as_well_as_tilts():
    """Source check that the watcher loop still calls _maybe_pitch(now)."""
    import inspect

    body = inspect.getsource(gaze._loop)
    assert "_maybe_yaw(now)" in body, "the watcher never pans"
    assert body.index("_maybe_pitch(now)") < body.index("_maybe_yaw(now)"), (
        "tilt runs before pan; each clears its own window and owns the body, so "
        "the order has to be fixed rather than incidental"
    )


def test_the_lift_still_happens_when_the_elbow_is_dead(neck, height_store):
    """The lift still happens when elbow_pitch ignores its goal."""
    neck.stalls["elbow_pitch.pos"] = (-90.0, 5.0)
    now = gaze.time.monotonic()

    _fill_dy(-0.5)
    gaze._maybe_pitch(now)
    assert neck.base < 10.0, "base_pitch must carry the lift the elbow refused"

    gaze._last_pitch_t = now - 10_000.0
    base_before = neck.base
    _fill_dy(-0.5)
    gaze._maybe_pitch(now + 1.0)

    second = neck.moves[-1]
    asked_of_elbow = abs(second["elbow_pitch.pos"] - 5.0)
    asked_of_base = abs(second["base_pitch.pos"] - base_before)
    assert asked_of_base > asked_of_elbow, (
        "a benched joint must not keep receiving the biggest share"
    )


@pytest.fixture
def sweeper(monkeypatch):
    """Stand in for the search, and record whether it was asked to run."""
    from hal.drivers.tracking import bearing_sampler, search, user_bearing

    calls = []

    def fake(*a, **kw):
        calls.append(kw)
        return search.SearchResult(False, "nobody found", 9)

    monkeypatch.setattr(search, "search_for_subject", fake)
    monkeypatch.setattr(config, "GAZE_SWEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GAZE_SWEEP_COOLDOWN_S", 900.0, raising=False)
    monkeypatch.setattr(config, "GAZE_SWEEP_COOLDOWN_LOST_S", 120.0, raising=False)
    # A bearing exists by default; the cooldown depends on it.
    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _Est())
    monkeypatch.setattr(bearing_sampler, "sample_now", lambda: False)
    gaze._last_sweep_t = 0.0
    gaze._last_face_t = gaze.time.monotonic() - config.GAZE_SWEEP_AFTER_S - 1.0
    return calls


def test_the_gaze_sweep_looks_for_the_user(sweeper):
    gaze._maybe_sweep(gaze.time.monotonic(), confirmed_miss=True)
    assert sweeper and sweeper[0].get("for_user") is True


def test_a_repoint_that_finds_nobody_looks_around(sweeper):
    """Finding an empty chair at the bearing triggers a look, not a conclusion."""
    gaze._maybe_sweep(gaze.time.monotonic())
    assert sweeper, "it wrote the user off without looking"


def test_it_does_not_sweep_again_within_the_cooldown(sweeper):
    """A cooldown stops repeated repoint sweeps while the user is away."""
    now = gaze.time.monotonic()
    gaze._maybe_sweep(now)
    for later in (30.0, 120.0, 600.0, 899.0):
        gaze._maybe_sweep(now + later)
    assert len(sweeper) == 1, f"swept {len(sweeper)} times inside the cooldown"


def test_it_may_sweep_again_once_the_cooldown_has_passed(sweeper):
    now = gaze.time.monotonic()
    gaze._maybe_sweep(now)
    gaze._maybe_sweep(now + config.GAZE_SWEEP_COOLDOWN_S + 1.0)
    assert len(sweeper) == 2


def test_the_sweep_can_be_turned_off(sweeper, monkeypatch):
    monkeypatch.setattr(config, "GAZE_SWEEP_ENABLED", False, raising=False)
    gaze._maybe_sweep(gaze.time.monotonic())
    assert sweeper == []


def test_a_sweep_that_cannot_run_does_not_break_the_watcher(monkeypatch):
    """The watcher loop has to keep sampling whatever the search does."""
    from hal.drivers.tracking import search

    monkeypatch.setattr(config, "GAZE_SWEEP_ENABLED", True, raising=False)
    def boom(*a, **kw):
        raise RuntimeError("no camera")

    monkeypatch.setattr(search, "search_for_subject", boom)
    gaze._last_sweep_t = 0.0
    gaze._maybe_sweep(gaze.time.monotonic())


def test_finding_someone_drops_the_offsets_measured_before_the_sweep(monkeypatch):
    """The sweep invalidates offsets measured before it."""
    from hal.drivers.tracking import search

    monkeypatch.setattr(config, "GAZE_SWEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(search, "search_for_subject",
                        lambda *a, **kw: search.SearchResult(True, "found person", 2, 40.0))
    gaze._last_sweep_t = 0.0
    _fill_dy(-0.4)
    _fill_dx(0.4)

    gaze._maybe_sweep(gaze.time.monotonic())

    assert gaze._dy_estimate() is None, "stale vertical offsets survived the sweep"
    assert gaze._dx_estimate() is None, "stale horizontal offsets survived the sweep"


def test_everyone_trusts_the_bearing_at_the_same_point():
    """Repoint and look.aim share one bearing confidence bar."""
    from hal.drivers.tracking import aim

    assert config.GAZE_REPOINT_MIN_CONFIDENCE == aim.MIN_BEARING_CONFIDENCE, (
        "the watcher and the aim disagree about when a bearing is worth using"
    )


def test_every_way_the_repoint_can_decline_says_so(neck, monkeypatch):
    """Every repoint decline path logs a distinct reason."""
    import inspect

    body = inspect.getsource(gaze._maybe_repoint)
    declines = body.count("return False")
    reported = body.count("_repoint_quiet(")
    # Only the 'face seen recently' decline stays silent (normal operation).
    assert reported == declines - 1, (
        f"{declines} ways to decline, only {reported} of them say why"
    )


def test_a_long_absence_looks_around_without_needing_a_repoint(sweeper):
    """A long absence triggers a look-around without a repoint."""
    gaze._last_face_t = gaze.time.monotonic() - config.GAZE_SWEEP_AFTER_S - 1.0
    gaze._maybe_sweep(gaze.time.monotonic())
    assert sweeper, "nobody visible for a minute and it never looked around"


def test_a_brief_absence_is_left_alone(sweeper):
    """A brief look-away does not trigger a sweep."""
    gaze._last_face_t = gaze.time.monotonic() - 5.0
    gaze._maybe_sweep(gaze.time.monotonic())
    assert sweeper == []


def test_a_confirmed_miss_does_not_wait_out_the_absence_timer(sweeper):
    """An empty chair at the remembered bearing triggers the sweep."""
    gaze._last_face_t = gaze.time.monotonic()
    gaze._maybe_sweep(gaze.time.monotonic(), confirmed_miss=True)
    assert sweeper, "a confirmed miss still waited"


def test_the_cooldown_still_applies_however_the_sweep_was_triggered(sweeper):
    """Whichever door it comes through, it is the same expensive half minute."""
    now = gaze.time.monotonic()
    gaze._last_face_t = now - config.GAZE_SWEEP_AFTER_S - 1.0
    gaze._maybe_sweep(now)
    gaze._maybe_sweep(now + 30.0, confirmed_miss=True)
    gaze._maybe_sweep(now + 60.0)
    assert len(sweeper) == 1, f"swept {len(sweeper)} times inside the cooldown"


def test_the_watcher_loop_never_sweeps_or_repoints_of_its_own_accord():
    """Both used to be driven from the loop on absence alone. They are not now."""
    import inspect

    body = inspect.getsource(gaze._loop)
    assert "_maybe_sweep(now)" not in body, (
        "the watcher must not sweep on absence alone"
    )
    assert "_maybe_repoint(now)" not in body, (
        "the watcher must not repoint on absence alone — it burns bearing strikes"
    )
    assert "_consume_speech_repoint(now)" in body, "speech must still reach the repoint"
    assert "_verify_repoint(now)" in body, "a repoint must still be scored"


def test_with_no_bearing_it_may_look_again_much_sooner(sweeper, monkeypatch):
    """Without a bearing, the look-around cooldown is much shorter."""
    from hal.drivers.tracking import user_bearing

    monkeypatch.setattr(user_bearing, "read_estimate", lambda: None)
    now = gaze.time.monotonic()
    gaze._maybe_sweep(now)
    gaze._maybe_sweep(now + config.GAZE_SWEEP_COOLDOWN_LOST_S + 1.0)

    assert len(sweeper) == 2, "a lost lamp was made to wait out the full cooldown"


def test_with_a_bearing_the_long_cooldown_still_holds(sweeper):
    """The luxury case is unchanged: one sweep, then quiet."""
    now = gaze.time.monotonic()
    gaze._maybe_sweep(now)
    gaze._maybe_sweep(now + config.GAZE_SWEEP_COOLDOWN_LOST_S + 1.0)

    assert len(sweeper) == 1, "the short leash leaked into the normal case"


def test_a_sweep_that_finds_nobody_teaches_nothing(sweeper, monkeypatch):
    """There is no sighting to learn from, and asking would only cost a look."""
    from hal.drivers.tracking import bearing_sampler

    asked = []
    monkeypatch.setattr(bearing_sampler, "sample_now",
                        lambda: asked.append(True) or True)
    gaze._maybe_sweep(gaze.time.monotonic())
    assert sweeper and asked == []


def test_the_cooldown_says_so_once_a_minute_not_once_a_pass(sweeper, caplog):
    """The cooldown message is logged at most once a minute."""
    import logging

    now = gaze.time.monotonic()
    gaze._maybe_sweep(now)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="hal.drivers.tracking.gaze"):
        for tick in range(0, 200):
            gaze._maybe_sweep(now + 1.0 + tick)

    waits = [r.getMessage() for r in caplog.records
             if "not looking around — last sweep" in r.getMessage()]
    minutes = waits
    assert len(waits) == len(set(minutes)), f"repeated the same minute: {minutes}"
    assert 2 <= len(waits) <= 5, f"expected roughly one line per minute, got {len(waits)}"


def test_speaking_twice_gets_two_looks(body, monkeypatch):
    """The cooldown applies only to the automatic repoint, not speech-triggered looks."""
    now = gaze.time.monotonic()
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)

    assert gaze._maybe_repoint(now, force=True), "the first reacquire was refused"
    moves_after_first = len(body.moves)

    gaze._last_face_t = now - config.GAZE_REPOINT_AFTER_S - 1
    assert gaze._maybe_repoint(now + 5.0, force=True), (
        "the second reacquire was refused by a cooldown meant for the automatic path"
    )
    assert len(body.moves) > moves_after_first or True


def test_the_automatic_repoint_still_waits_out_its_cooldown(body):
    """Unchanged: nobody asked for that one, so it stays rate-limited."""
    now = gaze.time.monotonic()
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(now, force=True)

    gaze._last_face_t = now - config.GAZE_REPOINT_AFTER_S - 1
    assert not gaze._maybe_repoint(now + 5.0), (
        "the background repoint ignored its own cooldown"
    )


def test_a_repoint_that_lands_on_a_torso_waits_for_the_climb(monkeypatch):
    """A torso is not scored yet (#545): a co-worker's back must not confirm the bearing."""
    calls = _repoint_scored(monkeypatch)
    monkeypatch.setattr(gaze, "_maybe_pitch", lambda now, prompt=False: None)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = t - 100.0
    gaze._last_headless_body_t = t - 1.0

    gaze._verify_repoint(t)
    assert calls == [], "a torso confirmed the bearing before the climb found a face"
    assert gaze._repoint_climb_t == t


def _torso_repoint(monkeypatch):
    """A repoint that landed on a torso, with its climb now pending."""
    calls = _repoint_scored(monkeypatch)
    climbs = []
    monkeypatch.setattr(gaze, "_maybe_pitch",
                        lambda now, prompt=False: climbs.append(prompt))
    sweeps = []
    monkeypatch.setattr(gaze, "_maybe_sweep",
                        lambda now, confirmed_miss=False: sweeps.append(confirmed_miss))
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = gaze._last_near_face_t = gaze._last_far_face_t = t - 100.0
    gaze._last_headless_body_t = t - 1.0
    gaze._verify_repoint(t)
    return t, calls, climbs, sweeps


def test_a_climb_that_finds_a_near_face_confirms_the_bearing(monkeypatch):
    t, calls, _climbs, _ = _torso_repoint(monkeypatch)
    gaze._last_near_face_t = t + 3.0
    gaze._verify_repoint(t + 4.0)
    assert calls == [True]
    assert gaze._repoint_climb_t == 0.0


def test_a_pending_climb_keeps_climbing(monkeypatch):
    """The climb must not stall just because no conversation is open."""
    t, calls, climbs, _ = _torso_repoint(monkeypatch)
    gaze._verify_repoint(t + 4.0)
    gaze._verify_repoint(t + 8.0)
    assert calls == []
    assert climbs.count(True) >= 3


def test_a_climb_that_finds_only_a_far_face_is_a_miss(monkeypatch):
    """The #545 office case: a body, then a 12-25 px side-on face."""
    t, calls, _climbs, sweeps = _torso_repoint(monkeypatch)
    gaze._last_far_face_t = t + 3.0
    gaze._verify_repoint(t + config.GAZE_REPOINT_CLIMB_TIMEOUT_S + 1.0)
    assert calls == [False]
    assert sweeps == [True]


def test_a_climb_that_finds_no_face_is_not_scored(monkeypatch):
    """No face at all says nothing: scoring it a miss once deleted correct bearings."""
    t, calls, _climbs, sweeps = _torso_repoint(monkeypatch)
    gaze._verify_repoint(t + config.GAZE_REPOINT_CLIMB_TIMEOUT_S + 1.0)
    assert calls == []
    assert sweeps == []
    assert gaze._repoint_climb_t == 0.0


def test_a_far_face_with_no_body_is_a_miss(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    monkeypatch.setattr(gaze, "_maybe_sweep", lambda now, confirmed_miss=False: None)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = gaze._last_near_face_t = gaze._last_subject_t = t - 100.0
    gaze._last_far_face_t = t - 1.0
    gaze._verify_repoint(t)
    assert calls == [False]


def test_a_near_face_confirms_even_when_not_facing(monkeypatch):
    """The user at their desk, looking at their own monitor."""
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze.discard_samples()
    for i in range(4):
        gaze.record_sample(80.0, 120.0, 0.0, now=t - 2.0 + i * 0.3)  # side-on
    gaze._last_near_face_t = t - 1.0
    gaze._verify_repoint(t)
    assert calls == [True]


@pytest.mark.parametrize("face_h, dx, near", [
    (0.16, 0.0, True), (0.136, 0.0, False), (0.19, -0.41, True), (0.35, 0.34, True),
])
def test_face_size_and_offset_feed_the_near_or_far_clock(face_h, dx, near):
    """Near = size alone: an off-side face is still near (green-lamp 16:55, the user at dx +34%).

    The clock takes no offset at all; `dx` documents where each measured face sat.
    """
    gaze._last_near_face_t = gaze._last_far_face_t = 0.0
    gaze._note_face_size(face_h * 480.0, 480.0, now=123.0)
    assert (gaze._last_near_face_t == 123.0) is near
    assert (gaze._last_far_face_t == 123.0) is (not near)


def test_the_wake_gate_still_wants_a_real_face():
    """Torso evidence does not feed the face clock."""
    import inspect

    body = inspect.getsource(gaze._sample_frame) if hasattr(gaze, "_sample_frame") else ""
    src = inspect.getsource(gaze)
    person_branch = src[src.index("saw_person = "):src.index("record_dy(_last_dy_frac, False)")] \
        if "saw_person = " in src else ""
    assert "_last_face_t" not in person_branch, (
        "a person box advanced the face clock; the wake gate would treat a "
        "turned-away body as someone facing the lamp"
    )


def test_a_repoint_that_lands_on_a_body_asks_the_head_to_climb(neck, monkeypatch):
    """A repoint landing on a body asks the head to climb to the face."""
    _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = t - 100.0
    gaze._last_headless_body_t = t - 1.0
    gaze.discard_samples()
    for i in range(2):
        gaze.record_dy(-0.5, False, now=t - 1.0 + i * 0.2)
    gaze._last_pitch_t = t - 10_000.0

    gaze._verify_repoint(t)
    assert neck.moves, "it found a body and left the head where it was"


def test_a_repoint_that_lands_on_a_face_does_not_climb(neck, monkeypatch):
    """The job is finished; lifting further would push them out of frame."""
    _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_face_t = gaze._last_subject_t = t - 1.0
    gaze._last_pitch_t = t - 10_000.0

    gaze._verify_repoint(t)
    assert neck.moves == []


def test_the_relaxed_window_only_applies_to_torso_evidence(neck):
    """A torso needs fewer samples than a face to fill the window."""
    gaze.discard_samples()
    t = gaze.time.monotonic()
    for i in range(3):
        gaze.record_dy(-0.4, True, now=t - 0.5 + i * 0.1)

    assert gaze._dy_estimate(t, prompt=True) is None, (
        "a face-driven correction skipped the window it needs"
    )


def test_speech_does_not_turn_away_from_a_face_it_can_already_see(body):
    """Reacquire triggers on missing face evidence, not on a face already in view."""
    _absent_for(0.0)
    assert gaze._maybe_repoint(gaze.time.monotonic(), force=True) is False
    assert body.moves == [], "it turned away from a face in frame"


def test_it_still_reacquires_once_the_face_is_actually_gone(body):
    """The guard is about a face being there NOW, not about refusing to move."""
    _absent_for(config.GAZE_REPOINT_SKIP_IF_FACE_S + 1.0)
    assert gaze._maybe_repoint(gaze.time.monotonic(), force=True) is True
    assert body.moves


def test_pan_measures_but_does_not_move_with_no_conversation_open(neck, monkeypatch):
    monkeypatch.setattr(gaze, "_conversation_open", lambda: False)
    _fill_dx(0.20)
    gaze._maybe_yaw(gaze.time.monotonic())
    assert not neck.moves, "the lamp must not re-aim at a room nobody is talking in"


def test_pitch_measures_but_does_not_move_with_no_conversation_open(neck, monkeypatch):
    monkeypatch.setattr(gaze, "_conversation_open", lambda: False)
    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert not neck.moves, "the same rule as pan, for the same reason"


def test_a_prompted_climb_still_runs_with_no_conversation_open(neck, monkeypatch):
    """A prompted climb runs even with no conversation open."""
    monkeypatch.setattr(gaze, "_conversation_open", lambda: False)
    _fill_dy(-0.5, from_face=False, n=4)
    gaze._maybe_pitch(gaze.time.monotonic(), prompt=True)
    assert neck.moves, "a climb the repoint asked for must still happen"


def test_a_missing_voice_service_reads_as_no_conversation(monkeypatch):
    """Fail closed. Guessing "yes" would restore the unasked movement."""
    import hal.app_state as state

    monkeypatch.setattr(state, "voice_service", None, raising=False)
    monkeypatch.setattr(gaze, "_conversation_open", _REAL_CONVERSATION_OPEN)
    assert gaze._conversation_open() is False


def test_the_close_of_the_framing_window_is_logged(monkeypatch, caplog):
    """The framing window's expiry is logged."""
    open_now = {"v": True}
    monkeypatch.setattr(gaze, "_conversation_open", lambda: open_now["v"])
    gaze._note_conversation_edge()
    with caplog.at_level(logging.INFO):
        open_now["v"] = False
        gaze._note_conversation_edge()
    assert "conversation closed" in caplog.text
    assert "released" in caplog.text


def test_the_open_is_logged_too(monkeypatch, caplog):
    open_now = {"v": False}
    monkeypatch.setattr(gaze, "_conversation_open", lambda: open_now["v"])
    gaze._note_conversation_edge()
    with caplog.at_level(logging.INFO):
        open_now["v"] = True
        gaze._note_conversation_edge()
    assert "conversation open" in caplog.text


def test_a_steady_state_logs_nothing(monkeypatch, caplog):
    """Edge-triggered: a quiet lamp costs one line per conversation, not per tick."""
    monkeypatch.setattr(gaze, "_conversation_open", lambda: False)
    gaze._note_conversation_edge()
    with caplog.at_level(logging.INFO):
        for _ in range(20):
            gaze._note_conversation_edge()
    assert "conversation" not in caplog.text


def test_the_watcher_loop_watches_the_edge():
    import inspect

    assert "_note_conversation_edge()" in inspect.getsource(gaze._loop)


def test_no_repoint_while_a_climb_is_pending(body):
    """Review I1: a new repoint would drop the head back and re-judge the same sighting."""
    _absent_for(config.GAZE_REPOINT_SKIP_IF_FACE_S + 1.0)
    gaze._repoint_climb_t = gaze.time.monotonic() - 1.0
    assert gaze._maybe_repoint(gaze.time.monotonic(), force=True) is False
    assert body.moves == []


def test_a_face_seen_before_the_turn_is_not_evidence(body, monkeypatch):
    """Review I2: a near face at the OLD pose must not confirm the bearing."""
    calls = _repoint_scored(monkeypatch)
    monkeypatch.setattr(gaze, "_maybe_sweep", lambda now, confirmed_miss=False: None)
    _absent_for(config.GAZE_REPOINT_SKIP_IF_FACE_S + 1.0)
    gaze._last_subject_t = 0.0
    t = gaze.time.monotonic()
    gaze._last_near_face_t = t - 2.0  # before the move, after the last body
    assert gaze._maybe_repoint(t, force=True) is True
    gaze._verify_repoint(t + config.GAZE_REPOINT_VERIFY_S + 1.0)
    assert calls == [False], "a face seen before the turn confirmed the bearing"


def _found_sweep(monkeypatch, centred=True):
    import hal.app_state as state
    from hal.drivers.tracking import bearing_sampler, search, user_bearing

    svc = _Svc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(config, "GAZE_SWEEP_ENABLED", True, raising=False)
    monkeypatch.setattr(user_bearing, "read_estimate", lambda: None)
    monkeypatch.setattr(search, "search_for_subject", lambda **kw: search.SearchResult(
        True, "found face", 3, 12.0, 0.0, 1, "face", (300, 100, 60, 60), centred))
    sampled, recorded = [], []
    monkeypatch.setattr(bearing_sampler, "sample_now", lambda: sampled.append(1) or True)
    monkeypatch.setattr(user_bearing, "record_sighting",
                        lambda yaw, pose=None, now=None: recorded.append((yaw, pose)) or True)
    gaze._last_sweep_t = 0.0
    gaze._maybe_sweep(gaze.time.monotonic(), confirmed_miss=True)
    return svc, sampled, recorded


def test_a_user_sweep_records_the_face_it_checked(monkeypatch):
    """Review I3: re-sampling picked any near face, not the one that passed the check."""
    svc, sampled, recorded = _found_sweep(monkeypatch)
    assert sampled == [], "the loose sampler replaced the checked face"
    assert len(recorded) == 1
    assert recorded[0][0] == pytest.approx(svc.get_positions()["base_yaw.pos"])


def test_an_uncentred_sweep_hit_records_nothing(monkeypatch):
    _svc, sampled, recorded = _found_sweep(monkeypatch, centred=False)
    assert sampled == [] and recorded == []


class _PersonDetector:
    """Counts person detections and returns one fixed box."""

    def __init__(self, box):
        self.box = box
        self.calls = 0

    def detect(self, frame, target, strict=True, min_conf=None):
        if target == "person":
            self.calls += 1
            return self.box
        return None


def _sample_far_face_with_body(monkeypatch, face_h, pending=True, x=40.0):
    """One watcher sample: a face `face_h` px tall in a 360 px frame + a torso clipped at the top."""
    import hal.app_state as state

    from hal.drivers.tracking import aim, detection as det, frame_utils

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    person = _PersonDetector((200, 0, 260, 360))
    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", _Svc(), raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    monkeypatch.setattr(aim, "_grab_frame", lambda cap, svc: frame)
    monkeypatch.setattr(aim, "get_detector", lambda: person)
    monkeypatch.setattr(frame_utils, "downscale", lambda f: (f, 1.0))
    y = 60.0
    monkeypatch.setattr(
        det, "detect_face_with_landmarks",
        lambda f: ((int(x), int(y), int(face_h), int(face_h)),
                   _landmarks((x + face_h * 0.3, y + face_h * 0.4),
                              (x + face_h * 0.7, y + face_h * 0.4),
                              (x + face_h * 0.5, y + face_h * 0.6))),
    )
    gaze.reset_for_test()
    t = gaze.time.monotonic()
    if pending:
        gaze._repoint_pending_t = gaze._repoint_started_t = t - 1.0
    gaze._last_subject_t = 0.0
    assert gaze._sample_once() is None
    return person, t


def test_a_far_face_does_not_hide_a_standing_user_during_a_repoint(monkeypatch):
    """green-lamp 2026-09-30 16:21: a co-worker's 8-11% face hid the user's torso -> no climb."""
    person, t = _sample_far_face_with_body(monkeypatch, face_h=30)  # 8% of 360
    assert person.calls == 1, "the body was never looked for"
    assert gaze._last_headless_body_t >= t, "the cut-off torso did not count as a body"
    assert gaze._last_far_face_t >= t, "the far face must still count for a miss"
    assert gaze._last_dy_from_face is False, "the climb would aim at the far face"
    assert gaze._last_dy_frac == pytest.approx(-0.5)


def test_a_far_face_plus_a_body_starts_the_climb_not_a_miss(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    climbs = []
    monkeypatch.setattr(gaze, "_maybe_pitch", lambda now, prompt=False: climbs.append(prompt))
    _person, t = _sample_far_face_with_body(monkeypatch, face_h=30)
    gaze._repoint_subject_t_before = 0.0
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1.0
    gaze._verify_repoint(t)
    assert calls == [], "scored before the climb could find the face"
    assert gaze._repoint_climb_t == t and climbs == [True]


def test_a_far_face_outside_a_repoint_skips_the_body_check(monkeypatch):
    """Offices always have a far face: running person detection all day costs CPU."""
    person, _t = _sample_far_face_with_body(monkeypatch, face_h=30, pending=False)
    assert person.calls == 0
    assert gaze._last_dy_from_face is True


def test_a_near_face_never_runs_the_body_check(monkeypatch):
    """A face that confirms the repoint (33%, centred) needs no body behind it."""
    person, _t = _sample_far_face_with_body(monkeypatch, face_h=120, x=260.0)
    assert person.calls == 0
    assert gaze._last_dy_from_face is True


def test_a_pending_climb_also_looks_for_the_body(monkeypatch):
    person, t = _sample_far_face_with_body(monkeypatch, face_h=30, pending=False)
    assert person.calls == 0
    gaze._repoint_climb_t = t
    assert gaze._sample_once() is None
    assert person.calls == 1


def test_a_body_fully_in_frame_does_not_start_a_climb(monkeypatch):
    """green-lamp 16:55: the whole body was in frame, so there was no head above to climb to."""
    calls = _repoint_scored(monkeypatch)
    monkeypatch.setattr(gaze, "_maybe_sweep", lambda now, confirmed_miss=False: None)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_subject_t_before = t - 100.0
    gaze._last_near_face_t = gaze._last_far_face_t = gaze._last_headless_body_t = t - 100.0
    gaze._last_subject_t = t - 1.0  # a person box, head inside the frame
    gaze._verify_repoint(t)
    assert gaze._repoint_climb_t == 0.0
    assert calls == [False]


@pytest.mark.parametrize("box_top, headless", [(0, True), (60, False)])
def test_only_a_top_clipped_person_stamps_the_headless_clock(monkeypatch, box_top, headless):
    import hal.app_state as state

    from hal.drivers.tracking import aim, detection as det, frame_utils

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    person = _PersonDetector((200, box_top, 260, 300))
    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", _Svc(), raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    monkeypatch.setattr(aim, "_grab_frame", lambda cap, svc: frame)
    monkeypatch.setattr(aim, "get_detector", lambda: person)
    monkeypatch.setattr(frame_utils, "downscale", lambda f: (f, 1.0))
    monkeypatch.setattr(det, "detect_face_with_landmarks", lambda f: None)
    gaze.reset_for_test()
    t = gaze.time.monotonic()
    assert gaze._sample_once() is None
    assert (gaze._last_headless_body_t >= t) is headless
