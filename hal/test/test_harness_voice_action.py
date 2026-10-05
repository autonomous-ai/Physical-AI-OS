"""Local API adapter, confirmation localization and hardware privacy policy."""

import copy
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import requests

from hal.drivers.harness import client as client
from hal.drivers.harness import actions as action
from hal.i18n import localized_phrase, PHRASE_HARNESS_ON, PHRASE_HARNESS_OFF


class HarnessClientTests(unittest.TestCase):
    def request(self, payload, status=200):
        session = MagicMock()
        session.post.return_value.json.return_value = payload
        session.post.return_value.status_code = status
        with patch.object(client.requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = client.request_voice_toggle("gesture-id")
        self.assertFalse(session.trust_env)
        session.post.assert_called_once_with(client.OS_HARNESS_GESTURE_URL,
                                            json={"gestureId": "gesture-id"}, timeout=15,
                                            allow_redirects=False)
        return result

    def test_success_and_false_are_authoritative(self):
        for data in ({"enabled": False}, {"enabled": True, "focusAvailable": True, "agentId": "agent"}):
            self.assertEqual(self.request({"status": 1, "data": data}), data)

    def test_failure_code_and_invalid_success(self):
        with self.assertRaises(client.HarnessGestureError) as caught:
            self.request({"status": 0, "data": {"code": "no_agents"}}, 409)
        self.assertEqual(caught.exception.code, "no_agents")
        for data in ({}, {"enabled": "false"}, {"enabled": True}):
            with self.assertRaises(client.HarnessGestureError):
                self.request({"status": 1, "data": data})

    def test_disable_and_focus_use_explicit_commands(self):
        with patch.object(client.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {"status": 1, "data": {"enabled": False}}
            client.request_voice_disable("exit-id")
            self.assertEqual(session.post.call_args.kwargs["json"], {"gestureId": "exit-id", "action": "disable"})
            session.post.return_value.json.return_value = {
                "status": 1, "data": {"enabled": True, "focusAvailable": True, "agentId": "next"}}
            client.request_focus_step("step-id", "next", 42)
            self.assertTrue(session.post.call_args.args[0].endswith("/voice-mode/focus"))
            self.assertEqual(session.post.call_args.kwargs["json"],
                             {"gestureId": "step-id", "direction": "next", "generation": 42})
            self.assertFalse(session.trust_env)

    def test_focus_step_accepts_lagging_focus_snapshot(self):
        with patch.object(client.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {
                "status": 1, "data": {"enabled": True, "focusAvailable": False, "agentId": ""}}
            self.assertFalse(client.request_focus_step("step-id", "next", 42)["focusAvailable"])
            with self.assertRaises(client.HarnessGestureError):
                client.request_voice_toggle("toggle-id")

    def test_timeout_never_retries(self):
        with patch.object(client.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.side_effect = requests.Timeout()
            with self.assertRaises(client.HarnessGestureError):
                client.request_voice_toggle("one")
            session.post.assert_called_once()


class HarnessActionTests(unittest.TestCase):
    def setUp(self):
        sleeping = patch.object(action.state, "_sleeping", False)
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def test_sleeping_swipe_cannot_toggle_harness_or_wake(self):
        with patch.object(action.state, "_sleeping", True), \
                patch.object(action.state, "_hw_mic_switch_muted", False), \
                patch.object(action, "request_voice_toggle") as request, \
                patch.object(action, "_show_feedback") as feedback, \
                patch("hal.drivers.button_actions._speak_gesture_ack") as speak, \
                patch("hal.drivers.button_actions._wake_if_sleepy") as wake:
            action.toggle_harness_voice()
            request.assert_not_called()
            feedback.assert_not_called()
            speak.assert_not_called()
            wake.assert_not_called()

    def test_feedback_uses_device_overlay_and_restores_after_duration(self):
        from hal.board.presets_overlay import _TABLES, apply_device_presets
        from hal.presets import BUTTON_LED_PRESETS

        with ExitStack() as stack:
            for table in _TABLES.values():
                stack.enter_context(patch.dict(table, copy.deepcopy(table)))
            apply_device_presets("lamp", str(Path(__file__).resolve().parents[2] / "robots"))
            stack.enter_context(patch.object(action.state, "rgb_service", MagicMock()))
            stack.enter_context(patch.object(action.state, "_effect_thread", None))
            stack.enter_context(patch("hal.drivers.harness.led.set_enabled"))
            restore = stack.enter_context(patch.object(action.state, "_schedule_led_restore"))
            def start_effect(request):
                action.state._effect_thread = object()
            start = stack.enter_context(patch("hal.routes.led.start_led_effect", side_effect=start_effect))
            for enabled, color in ((False, [2, 2, 2]),):
                action._show_feedback(enabled)
                request = start.call_args.args[0]
                self.assertEqual(request.color, color)
                self.assertEqual(request.effect, "blink")
                self.assertEqual(request.duration_ms, 300)
                self.assertTrue(request.transient)
                restore.assert_called_with(0.4)
            BUTTON_LED_PRESETS["harness_off"].update(effect="blink", duration_ms=900)
            action._show_feedback(False)
            self.assertEqual(start.call_args.args[0].effect, "blink")
            restore.assert_called_with(1.0)
            restore.reset_mock()
            start.side_effect = None
            action._show_feedback(False)
            restore.assert_not_called()

    def test_all_languages_and_config_lookup(self):
        expected_off = {"en": "Harness is off. You’re back with the assistant on your device.", "vi": "Đã tắt Harness, trở về trợ lý trên thiết bị.",
                        "zh-CN": "Harness 已关闭，已切回设备上的助手。", "zh-TW": "Harness 已關閉，已切回裝置上的助理。"}
        from hal.presets import LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW
        for lang, expected in zip((LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW), expected_off.values()):
            self.assertEqual(localized_phrase(PHRASE_HARNESS_OFF, lang), expected)
            self.assertIn("Test agent", localized_phrase(PHRASE_HARNESS_ON, lang).format(agent="Test agent"))
            with patch("hal.config._os_cfg_get", return_value=lang):
                self.assertEqual(action.confirmation_phrase({"enabled": False}), expected)
                self.assertIn("Friendly", action.confirmation_phrase({"enabled": True, "agentId": "id", "agentName": "Friendly"}))
                from hal.i18n import PHRASE_HARNESS_UNPAIRED
                self.assertEqual(action.failure_phrase("harness_unpaired"),
                                 localized_phrase(PHRASE_HARNESS_UNPAIRED, lang))
                self.assertNotEqual(action.failure_phrase("harness_unpaired"),
                                    action.failure_phrase("harness_offline"))
                self.assertTrue(action.failure_phrase("no_agents"))
                self.assertTrue(action.failure_phrase("harness_offline"))
                self.assertTrue(action.failure_phrase("busy"))

    def test_privacy_switch_prevents_api_and_ack(self):
        with patch.object(action.state, "_hw_mic_switch_muted", True), patch.object(action, "request_voice_toggle") as request:
            action.toggle_harness_voice()
            request.assert_not_called()

    def test_action_unique_ids_and_actual_result_ack(self):
        with patch.object(action.state, "_hw_mic_switch_muted", False), \
                patch.object(action, "request_voice_toggle", return_value={"enabled": False}) as request, \
                patch.object(action, "_show_feedback") as led, \
                patch("hal.drivers.button_actions._speak_gesture_ack") as speak:
            action.toggle_harness_voice()
            action.toggle_harness_voice()
            self.assertNotEqual(request.call_args_list[0].args, request.call_args_list[1].args)
            led.assert_called_with(False)
            speak.assert_called_with(localized_phrase(PHRASE_HARNESS_OFF), "MPR121")

    def test_unknown_outcome_never_announces_success(self):
        with patch.object(action.state, "_hw_mic_switch_muted", False), \
                patch.object(action, "request_voice_toggle", side_effect=client.HarnessGestureError("unknown")), \
                patch.object(action, "_show_feedback") as led, \
                patch("hal.drivers.button_actions._speak_gesture_ack") as speak:
            action.toggle_harness_voice()
            led.assert_not_called()
            speak.assert_called_once_with(action.failure_phrase("unknown"), "MPR121")
