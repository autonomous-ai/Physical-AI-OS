"""Backchannel/filler-only turns are dropped, real requests are not."""

from hal.drivers.voice._internal.realtime_turn import is_nonactionable_transcript


def test_drops_filler_only():
    for s in ["okay", "Okay.", "OK!", "yeah", "yeah yeah", "uh", "uh-huh",
              "mm-hmm", "hmm", "one sec", "hold on", "right"]:
        assert is_nonactionable_transcript(s), s


def test_keeps_real_requests():
    for s in ["okay do it", "turn the light off", "football.", "what's your name",
              "hello how are you", "play some music", "one second please turn it off"]:
        assert not is_nonactionable_transcript(s), s


def test_empty_is_not_dropped_here():
    assert not is_nonactionable_transcript("")
    assert not is_nonactionable_transcript("   ")


if __name__ == "__main__":
    test_drops_filler_only()
    test_keeps_real_requests()
    test_empty_is_not_dropped_here()
    print("ok")
