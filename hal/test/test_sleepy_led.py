"""Regression tests for sleepy owning the LED strip."""
import threading
import unittest
from unittest import mock

import hal.app_state as state
from hal.presets import EMO_HAPPY, EMO_IDLE, EMO_LISTENING, EMO_SLEEPY


class _FakeRGB:
    def __init__(self):
        self.clear_calls = 0
        self.dispatches = []

    def clear(self):
        self.clear_calls += 1

    def dispatch(self, command, value):
        self.dispatches.append((command, value))


class _PendingRestore:
    def __init__(self):
        self.cancelled = False

    def is_alive(self):
        return True

    def cancel(self):
        self.cancelled = True


class TestSleepyLED(unittest.TestCase):
    _STATE_FIELDS = (
        "rgb_service", "animation_service", "voice_service", "tts_service", "music_service",
        "_sleeping", "_current_emotion", "_mic_muted", "_speaker_muted",
        "_sleepy_auto_muted_mic", "_sleepy_auto_muted_speaker", "_mic_muted_led",
        "_restore_timer", "_effect_thread", "_effect_stop", "_effect_name",
        "_effect_base_color", "_user_led_state", "_tts_speaking", "_music_playing",
    )

    def setUp(self):
        self.saved = {name: getattr(state, name) for name in self._STATE_FIELDS}
        state.rgb_service = _FakeRGB()
        state.animation_service = None
        state.voice_service = None
        state.tts_service = None
        state.music_service = None
        state._sleeping = True
        state._current_emotion = EMO_SLEEPY
        state._mic_muted = False
        state._speaker_muted = False
        state._sleepy_auto_muted_mic = False
        state._sleepy_auto_muted_speaker = False
        state._mic_muted_led = False
        state._restore_timer = None
        state._effect_thread = None
        state._effect_stop = threading.Event()
        state._effect_name = None
        state._effect_base_color = None
        state._user_led_state = {"type": "solid", "color": [20, 30, 40]}
        state._tts_speaking = False
        state._music_playing = False

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(state, name, value)

    def test_sleepy_cancels_pending_restore_and_blocks_muted_red_repaint(self):
        pending = _PendingRestore()
        state._restore_timer = pending

        state._finalize_sleepy_peripherals(mute_mic=True, mute_speaker=True)

        self.assertTrue(pending.cancelled)
        self.assertIsNone(state._restore_timer)
        self.assertTrue(state._mic_muted)
        self.assertEqual(state.rgb_service.clear_calls, 1)

        state._mic_muted_led = True
        state._restore_user_led()
        state._start_mic_muted_effect()

        self.assertEqual(state.rgb_service.clear_calls, 2)
        self.assertEqual(state.rgb_service.dispatches, [])

    def test_sleepy_blocks_late_tts_and_music_wave_starts(self):
        state._on_tts_speak_start()
        state._on_music_play_start()

        self.assertFalse(state._tts_speaking)
        self.assertFalse(state._music_playing)
        self.assertEqual(state.rgb_service.dispatches, [])

    def test_wake_clears_restored_mute_indicator_before_late_led_restore(self):
        state._sleeping = False
        state._current_emotion = EMO_IDLE
        state._sleepy_auto_muted_mic = True
        state._mic_muted = True
        state._mic_muted_led = True
        state._tts_speaking = True
        with (
            mock.patch.object(state, "_hw_mic_switch_muted", False),
            mock.patch.object(state, "_active_scene", None),
            mock.patch.object(state, "_persist_sleep_state"),
            mock.patch.object(state, "_start_mic_muted_effect") as muted_led,
        ):
            state._wake_sleepy_peripherals()
            self.assertFalse(state._mic_muted)
            self.assertFalse(state._mic_muted_led)
            state._on_tts_speak_end()
            state._on_music_play_end()
            muted_led.assert_not_called()

    def test_wake_preserves_indicator_when_hardware_privacy_still_locks_mic(self):
        state._sleeping = False
        state._sleepy_auto_muted_mic = True
        state._mic_muted = True
        state._mic_muted_led = True
        with (
            mock.patch.object(state, "_hw_mic_switch_muted", True),
            mock.patch.object(state, "_persist_sleep_state"),
        ):
            state._wake_sleepy_peripherals()
        self.assertTrue(state._mic_muted)
        self.assertTrue(state._mic_muted_led)

    def test_shared_button_wake_does_not_restore_privacy_red_after_feedback(self):
        from hal.drivers import button_actions

        state._mic_muted = True
        state._speaker_muted = True
        state._sleepy_auto_muted_mic = True
        state._sleepy_auto_muted_speaker = True
        state._mic_muted_led = True
        with (
            mock.patch.object(state, "_hw_mic_switch_muted", False),
            mock.patch.object(state, "_active_scene", None),
            mock.patch.object(state, "_enrolling", False),
            mock.patch.object(state, "tracker_service", None),
            mock.patch.object(state, "_save_boot_sidecar"),
            mock.patch.object(state, "_auto_camera_on"),
            mock.patch.object(state, "_apply_emotion_led_display"),
            mock.patch.object(state, "_schedule_led_restore"),
            mock.patch.object(state, "_start_mic_muted_effect") as muted_led,
            mock.patch.object(button_actions, "_cancel_agent_speech"),
            mock.patch.object(button_actions, "_grant_wakeword_focus"),
        ):
            button_actions.single_click_action("MPR121-test", announce=False, chime=False)
            self.assertFalse(state._sleeping)
            self.assertFalse(state._mic_muted)
            self.assertFalse(state._speaker_muted)
            self.assertFalse(state._mic_muted_led)
            state._tts_speaking = True
            state._on_tts_speak_end()
            state._restore_user_led()
            muted_led.assert_not_called()

    def test_muted_indicator_can_resume_after_wake(self):
        state._sleeping = False
        state._mic_muted_led = True

        with mock.patch.object(state, "_start_preset_effect") as start_effect:
            state._start_mic_muted_effect()

        start_effect.assert_called_once_with(state.STATUS_LED_PRESETS["mic_muted"], "led-mic-muted")

    def test_clear_listening_cue_restores_led_without_persistent_idle_effect(self):
        state._sleeping = False
        state._current_emotion = EMO_LISTENING

        with mock.patch.object(state, "_restore_user_led") as restore:
            self.assertTrue(state.clear_listening_cue())

        self.assertEqual(state._current_emotion, EMO_IDLE)
        restore.assert_called_once()

    def test_clear_listening_cue_preserves_newer_emotion(self):
        state._current_emotion = EMO_HAPPY

        with mock.patch.object(state, "_restore_user_led") as restore:
            self.assertFalse(state.clear_listening_cue())

        restore.assert_not_called()

if __name__ == "__main__":
    unittest.main()
