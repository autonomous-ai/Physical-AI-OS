"""Harness ON refuses sleepy from every caller (absence timer, API, button)."""
import unittest
from unittest.mock import patch

import hal.app_state as state
from hal.models import EmotionRequest
from hal.routes import emotion


class HarnessBlocksSleepTests(unittest.TestCase):
    def setUp(self):
        sleeping = patch.object(state, "_sleeping", False)
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def test_sleepy_is_ignored_while_harness_on(self):
        state._sleeping = False
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode",
                   return_value={"enabled": True, "generation": 1}):
            result = emotion.express_emotion(EmotionRequest(emotion="sleepy"), source="test")
        self.assertEqual(result["status"], "ignored")
        self.assertFalse(state._sleeping)

    def test_unavailable_harness_blocks_sleep(self):
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode",
                   return_value={"enabled": False, "generation": -1, "unavailable": True}):
            result = emotion.express_emotion(EmotionRequest(emotion="sleepy"), source="test")
        self.assertEqual(result["status"], "ignored")
        self.assertFalse(state._sleeping)

    def test_confirmed_off_does_not_block(self):
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode",
                   return_value={"enabled": False, "generation": 1}):
            self.assertFalse(emotion.harness_blocks_sleep())

    def test_enabling_harness_during_sleep_announcement_blocks_transition(self):
        from hal.drivers import button_actions
        with patch("hal.drivers.voice._internal.harness_voice.read_voice_mode", side_effect=[
                {"enabled": False, "generation": 1},
                {"enabled": True, "generation": 2},
        ]) as read_mode, patch.object(button_actions, "_tts_available", return_value=True), \
                patch.object(state, "tts_service") as tts, \
                patch.object(button_actions.time, "sleep"):
            button_actions.sleep_action("test")
        tts.speak_cached.assert_called_once()
        self.assertEqual(read_mode.call_count, 2)
        self.assertFalse(state._sleeping)


if __name__ == "__main__":
    unittest.main()
