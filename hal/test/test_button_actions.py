"""Focused tests for shared physical-button actions."""

import threading
from unittest import mock

import hal.app_state as state
from hal.drivers import button_actions
from hal.routes import music as music_routes


class _FakeTracker:
    def __init__(self, is_tracking: bool):
        self.is_tracking = is_tracking
        self.stop = mock.Mock()


def test_single_click_stops_active_tracking_even_with_hardware_mic_muted():
    tracker = _FakeTracker(is_tracking=True)
    with (
        mock.patch.object(state, "tracker_service", tracker),
        mock.patch.object(state, "_hw_mic_switch_muted", True),
    ):
        button_actions.single_click_action("test")

    tracker.stop.assert_called_once_with()


def test_single_click_does_not_stop_an_inactive_tracker():
    tracker = _FakeTracker(is_tracking=False)
    with (
        mock.patch.object(state, "tracker_service", tracker),
        mock.patch.object(state, "_hw_mic_switch_muted", True),
    ):
        button_actions.single_click_action("test")

    tracker.stop.assert_not_called()


class _FakeMusic:
    """Minimal MusicService stand-in: playing + stop, nothing else."""

    def __init__(self, playing: bool = True):
        self.available = True
        self.playing = playing
        self.stop = mock.Mock()


def _quiet_single_click(**state_overrides):
    """Run single_click_action with the network / speaker side effects stubbed."""
    patches = [
        mock.patch.object(button_actions, "_cancel_agent_speech"),
        mock.patch.object(button_actions, "_wake_if_sleepy"),
        mock.patch.object(button_actions, "_grant_wakeword_focus"),
        mock.patch.object(button_actions, "play_ack_chime"),
        mock.patch.object(button_actions, "announce_listening_cue"),
        mock.patch("hal.routes.voice.stop_tts"),
        mock.patch("hal.routes.voice.unmute_mic"),
        mock.patch("hal.routes.music.unmute_speaker"),
        mock.patch.object(state, "tracker_service", None),
        mock.patch.object(state, "_hw_mic_switch_muted", None),
    ]
    for key, value in state_overrides.items():
        patches.append(mock.patch.object(state, key, value))
    state._music_cancel_ms = 0.0
    for p in patches:
        p.start()
    try:
        button_actions.single_click_action("test")
    finally:
        for p in reversed(patches):
            p.stop()


def test_single_click_stops_music_even_when_mic_is_muted():
    """The mic-muted branch used to unmute the mic and leave music playing."""
    musicsvc = _FakeMusic(playing=True)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=True, _speaker_muted=False)
        musicsvc.stop.assert_called_once_with()
    finally:
        state._music_cancel_ms = 0.0


def test_single_click_stops_music_on_the_stop_speaker_branch():
    musicsvc = _FakeMusic(playing=True)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=False, _speaker_muted=False)
        musicsvc.stop.assert_called_once_with()
    finally:
        state._music_cancel_ms = 0.0


def test_single_click_arms_the_music_cancel_watermark():
    """A cancelled turn's pending play tool call must be refused, not played."""
    from hal.models import MusicPlayRequest

    musicsvc = _FakeMusic(playing=False)
    try:
        _quiet_single_click(music_service=musicsvc, _mic_muted=False, _speaker_muted=False)
        assert state.music_cancel_active() is True

        with (
            mock.patch.object(state, "music_service", musicsvc),
            mock.patch.object(state, "_speaker_muted", False),
        ):
            result = music_routes.audio_play(MusicPlayRequest(query="some song"))
        assert result == {"status": "suppressed"}
    finally:
        state._music_cancel_ms = 0.0


def test_music_cancel_watermark_expires():
    import time

    try:
        state._music_cancel_ms = time.monotonic() - (state.MUSIC_CANCEL_GUARD_S + 1.0)
        assert state.music_cancel_active() is False
    finally:
        state._music_cancel_ms = 0.0


def test_triple_click_only_maps_the_gesture_to_reboot_action():
    with mock.patch.object(button_actions, "reboot_action") as reboot:
        button_actions.triple_click_action("test button")

    reboot.assert_called_once_with("test button")


def test_hold_release_boundaries_do_not_escalate_early():
    for duration, expected in (
        (0.1, None), (1.999, None), (2.0, "sleep"), (4.999, "sleep"),
        (5.0, "shutdown"), (9.999, "shutdown"), (10.0, "reset"),
    ):
        with (
            mock.patch.object(button_actions, "sleep_action") as sleep,
            mock.patch.object(button_actions, "shutdown_action") as shutdown,
            mock.patch.object(button_actions, "factory_reset_action") as reset,
        ):
            button_actions.hold_release_action(duration, "MPR121")
            for name, action in (("sleep", sleep), ("shutdown", shutdown), ("reset", reset)):
                if name == expected:
                    action.assert_called_once_with("MPR121")
                else:
                    action.assert_not_called()


def test_hold_release_maps_each_duration_to_one_explicit_action():
    with (
        mock.patch.object(button_actions, "sleep_action") as sleep,
        mock.patch.object(button_actions, "shutdown_action") as shutdown,
        mock.patch.object(button_actions, "factory_reset_action") as factory_reset,
    ):
        button_actions.hold_release_action(button_actions.SLEEP_HOLD_DURATION, "test button")
        sleep.assert_called_once_with("test button")
        shutdown.assert_not_called()
        factory_reset.assert_not_called()

        button_actions.hold_release_action(button_actions.LONG_PRESS_DURATION, "test button")
        shutdown.assert_called_once_with("test button")
        factory_reset.assert_not_called()

        button_actions.hold_release_action(button_actions.FACTORY_RESET_DURATION, "test button")
        factory_reset.assert_called_once_with("test button")

        assert button_actions.button_hold_tier(
            button_actions.FACTORY_RESET_DURATION, factory_reset=False) == 2

        button_actions.hold_release_action(
            button_actions.FACTORY_RESET_DURATION, "MPR121", factory_reset=False)
        factory_reset.assert_called_once_with("test button")
        assert shutdown.call_count == 2


def test_swipe_sleeps_when_awake():
    """A swipe means sleep — one meaning, no direction, no state branch."""
    with mock.patch.object(state, "_sleeping", False), \
         mock.patch.object(button_actions, "sleep_action") as slept:
        button_actions.swipe_action(source="TTP223")
    slept.assert_called_once_with("TTP223")


def test_swipe_on_a_sleeping_device_still_routes_to_sleep():
    """A swipe while sleeping still routes to sleep and does not wake."""
    with mock.patch.object(state, "_sleeping", True), \
         mock.patch.object(button_actions, "sleep_action") as slept, \
         mock.patch.object(button_actions, "_wake_if_sleepy") as woke:
        button_actions.swipe_action(source="TTP223")
    slept.assert_called_once_with("TTP223")
    woke.assert_not_called()


class _FakeTTS:
    def __init__(self, available=True):
        self.available = available
        self.spoken = []

    def speak_cached(self, text, interruptible=False, **kw):
        self.spoken.append(text)
        return True


def _mic_toggle(muted: bool, speaker_muted=False, enrolling=False, hw_switch=None):
    """Run the double-tap toggle against a fake TTS and report what it said."""
    tts = _FakeTTS()
    with mock.patch.object(state, "_mic_muted", muted), \
         mock.patch.object(state, "_speaker_muted", speaker_muted), \
         mock.patch.object(state, "_enrolling", enrolling), \
         mock.patch.object(state, "_hw_mic_switch_muted", hw_switch), \
         mock.patch.object(state, "tts_service", tts), \
         mock.patch("hal.routes.voice.mute_mic") as mute, \
         mock.patch("hal.routes.voice.unmute_mic") as unmute:
        button_actions.mic_toggle_action(source="TTP223")
    for t in threading.enumerate():
        if t.name.endswith("gesture-ack"):
            t.join(timeout=1)
    return tts.spoken, mute, unmute


def _pool(pools):
    """The pool the device's current language will actually draw from."""
    from hal.i18n import DEFAULT_LANG

    return pools.get(button_actions._current_lang()) or pools.get(DEFAULT_LANG, [])


def test_muting_the_mic_says_so():
    """Muting via double tap speaks a confirmation."""
    from hal.i18n import MIC_MUTED_PHRASES_BY_LANG

    spoken, mute, unmute = _mic_toggle(muted=False)
    mute.assert_called_once()
    unmute.assert_not_called()
    assert spoken and spoken[0] in _pool(MIC_MUTED_PHRASES_BY_LANG), spoken


def test_unmuting_the_mic_says_so():
    from hal.i18n import MIC_UNMUTED_PHRASES_BY_LANG

    spoken, mute, unmute = _mic_toggle(muted=True)
    unmute.assert_called_once()
    mute.assert_not_called()
    assert spoken and spoken[0] in _pool(MIC_UNMUTED_PHRASES_BY_LANG), spoken


def test_every_mic_phrase_states_which_way_the_toggle_went():
    """Mute confirmations must clearly state the microphone state."""
    from hal.i18n import (
        MIC_MUTED_PHRASES_BY_LANG as MUTED,
        MIC_UNMUTED_PHRASES_BY_LANG as UNMUTED,
    )

    cues = {
        "en": ("listen", "hear", "ear"),
        "vi": ("nghe", "tai"),
        "ja": ("聞", "耳"),
        "zh-CN": ("听", "耳"),
        "zh-TW": ("聽", "耳"),
    }
    for pools in (MUTED, UNMUTED):
        for lang, phrases in pools.items():
            assert phrases, lang
            for text in phrases:
                low = text.lower()
                assert any(c in low for c in cues[lang]), (lang, text)
    for lang in MUTED:
        assert not set(MUTED[lang]) & set(UNMUTED[lang]), lang


def test_a_muted_speaker_stays_silent():
    """A muted speaker means the user chose silence; the LED still reports."""
    spoken, mute, _ = _mic_toggle(muted=False, speaker_muted=True)
    mute.assert_called_once()
    assert spoken == [], spoken


def test_a_refused_toggle_says_nothing():
    """Enrolment blocks the mute toggle and its announcement."""
    spoken, mute, unmute = _mic_toggle(muted=False, enrolling=True)
    mute.assert_not_called()
    unmute.assert_not_called()
    assert spoken == [], spoken
