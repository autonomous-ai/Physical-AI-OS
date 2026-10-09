"""Sound escalation habituates: one shock per noise, not one every few minutes."""

from hal.drivers.sensing.perceptions.processors import sound


def _perception():
    p = sound.SoundPerception.__new__(sound.SoundPerception)
    p._count = 0
    p._window_start = 0.0
    p._last_passed = 0.0
    p._suppress_until = 0.0
    p._habituated = False
    p._last_heard = 0.0
    return p


def _persistent_events(p, start, end, every):
    t, hits = start, []
    while t < end:
        send, _occ, persistent = p._track(t)
        if send and persistent:
            hits.append(t)
        t += every
    return hits


def test_continuous_noise_startles_once():
    p = _perception()
    hits = _persistent_events(p, 1000.0, 1000.0 + 30 * 60, every=5.0)
    assert len(hits) == 1, f"startled {len(hits)} times in 30 min of one noise"


def test_a_new_noise_after_real_quiet_counts_from_scratch():
    p = _perception()
    _persistent_events(p, 1000.0, 1100.0, every=5.0)
    later = 1100.0 + sound._WINDOW_DURATION_S + sound._SUPPRESS_DURATION_S + 1.0
    send, occurrence, persistent = p._track(later)
    assert send and occurrence == 1 and not persistent
