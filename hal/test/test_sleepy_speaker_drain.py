"""The going-to-sleep line must survive sleepy muting the speaker.

os-server fires `[HW:/emotion:sleepy]` and only waits 100ms before POSTing the
reply text to TTS, and HAL needs roughly as long to reach
_finalize_sleepy_peripherals — so muting the speaker there raced the
announcement and usually won, and `/voice/speak-queue` answered
`suppressed -- speaker muted`. The drain removes the race.
"""
import os, sys, threading, time, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class FakeTTS:
    """Minimal stand-in for TTSService: only `speaking` matters to the drain."""

    def __init__(self, speaking=False):
        self.speaking = speaking
        self.stop_calls = 0
        self.stopped = threading.Event()

    def stop(self):
        self.stop_calls += 1
        self.speaking = False
        self.stopped.set()

    def speak_after(self, delay_s, duration_s):
        """Start speaking `delay_s` from now and stop `duration_s` later."""
        def run():
            time.sleep(delay_s)
            self.speaking = True
            time.sleep(duration_s)
            self.speaking = False
        threading.Thread(target=run, daemon=True).start()


class SleepySpeakerDrainTest(unittest.TestCase):
    def setUp(self):
        from hal import app_state as state
        from hal.presets import EMO_SLEEPY
        self.state = state
        self.EMO_SLEEPY = EMO_SLEEPY

        self._saved = {
            "grace": state.SLEEPY_SPEAKER_GRACE_S,
            "cap": state.SLEEPY_SPEAKER_DRAIN_MAX_S,
            "poll": state._SLEEPY_DRAIN_POLL_S,
            "tts": state.tts_service,
            "music": state.music_service,
            "voice": state.voice_service,
            "rgb": state.rgb_service,
            "emotion": state._current_emotion,
            "sleeping": state._sleeping,
            "spk": state._speaker_muted,
            "mic": state._mic_muted,
            "auto_spk": state._sleepy_auto_muted_speaker,
            "auto_mic": state._sleepy_auto_muted_mic,
        }
        # Milliseconds, not seconds — the ratios are what the drain reasons about.
        state.SLEEPY_SPEAKER_GRACE_S = 0.20
        state.SLEEPY_SPEAKER_DRAIN_MAX_S = 1.0
        state._SLEEPY_DRAIN_POLL_S = 0.01
        state.music_service = None
        state.voice_service = None
        state.rgb_service = None
        state._current_emotion = EMO_SLEEPY
        state._sleeping = True
        state._speaker_muted = False
        state._mic_muted = False
        state._sleepy_auto_muted_speaker = False
        state._sleepy_auto_muted_mic = False

    def tearDown(self):
        state = self.state
        state._cancel_sleepy_speaker_drain()
        s = self._saved
        state.SLEEPY_SPEAKER_GRACE_S = s["grace"]
        state.SLEEPY_SPEAKER_DRAIN_MAX_S = s["cap"]
        state._SLEEPY_DRAIN_POLL_S = s["poll"]
        state.tts_service = s["tts"]
        state.music_service = s["music"]
        state.voice_service = s["voice"]
        state.rgb_service = s["rgb"]
        state._current_emotion = s["emotion"]
        state._sleeping = s["sleeping"]
        state._speaker_muted = s["spk"]
        state._mic_muted = s["mic"]
        state._sleepy_auto_muted_speaker = s["auto_spk"]
        state._sleepy_auto_muted_mic = s["auto_mic"]

    def _wait_for_mute(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state._speaker_muted:
                return True
            time.sleep(0.01)
        return False

    # --- the bug this exists for -------------------------------------------

    def test_announcement_arriving_after_the_marker_still_plays(self):
        """The real shape: sleepy lands first, the line follows ~100ms later."""
        state = self.state
        tts = FakeTTS()
        state.tts_service = tts
        # Scaled to the shortened grace: the line arrives a third of the way in.
        tts.speak_after(delay_s=0.06, duration_s=0.25)

        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)

        # The window the announcement needs must stay open for it.
        self.assertFalse(state._speaker_muted, "speaker muted before the line arrived")
        time.sleep(0.15)
        self.assertFalse(state._speaker_muted, "speaker muted mid-announcement")
        self.assertEqual(tts.stop_calls, 0, "in-flight announcement was cut off")

        self.assertTrue(self._wait_for_mute(), "speaker never muted after the line finished")
        self.assertTrue(state._sleepy_auto_muted_speaker, "mute must be marked sleep-owned")

    def test_silent_sleep_still_mutes_after_the_grace(self):
        """No announcement ever comes (NO_REPLY, agent error, TTS down)."""
        state = self.state
        state.tts_service = FakeTTS()
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self.assertFalse(state._speaker_muted, "muted before the grace even elapsed")
        self.assertTrue(self._wait_for_mute(), "silent sleep never muted the speaker")

    # --- the regressions it must not cause ---------------------------------

    def test_waking_mid_drain_leaves_the_speaker_alone(self):
        state = self.state
        tts = FakeTTS()
        state.tts_service = tts
        tts.speak_after(delay_s=0.02, duration_s=0.6)

        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        time.sleep(0.08)

        # What routes/emotion.py does on a wake emotion.
        state._sleeping = False
        state._current_emotion = "stretching"
        state._wake_sleepy_peripherals()

        time.sleep(0.8)
        self.assertFalse(state._speaker_muted, "drain muted a device that had woken up")
        self.assertFalse(state._sleepy_auto_muted_speaker)

    def test_a_stalled_tts_cannot_hold_the_speaker_open_forever(self):
        """The cap is the whole reason the window is safe to open at all."""
        state = self.state
        tts = FakeTTS(speaking=True)   # never stops
        state.tts_service = tts

        started = time.monotonic()
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self.assertTrue(self._wait_for_mute(timeout=3.0), "stalled TTS blocked the mute")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, state.SLEEPY_SPEAKER_DRAIN_MAX_S * 0.5,
                                "muted early — the drain did not wait for the announcement")
        self.assertLess(elapsed, state.SLEEPY_SPEAKER_DRAIN_MAX_S + 0.5,
                        "drain ran past its cap")
        # Muting only gates playback that has not started; the speech still in
        # progress at the cap has to be stopped or the cap bounds nothing and
        # a sleeping device keeps talking.
        # The mute flag is published before stop() runs outside privacy.lock.
        # Wait for that side effect instead of racing the background worker.
        self.assertTrue(tts.stopped.wait(0.5), "the stalled speech was left playing past the cap")
        self.assertEqual(tts.stop_calls, 1, "the stalled speech was left playing past the cap")
        self.assertFalse(tts.speaking, "device still speaking after the drain capped out")

    def test_the_commit_waits_for_the_wake_path_lock(self):
        """The guard alone was not enough — the commit must hold privacy.lock.

        Without it, a drain already past its guard writes the mute after a
        wake has restored, leaving an awake device silent, marked sleep-owned,
        and with nothing left to undo it.
        """
        state = self.state
        state.tts_service = FakeTTS()
        committed = threading.Event()

        def commit():
            state._mute_speaker_for_sleep()
            committed.set()

        with state.privacy.lock:   # the lock _wake_sleepy_peripherals holds
            threading.Thread(target=commit, daemon=True).start()
            self.assertFalse(committed.wait(0.15), "the mute committed without taking the lock")
            self.assertFalse(state._speaker_muted, "speaker muted while the wake path held the lock")
            # The wake this drain lost to.
            state._sleeping = False
            state._current_emotion = "stretching"

        self.assertTrue(committed.wait(1.0), "the mute never ran after the lock was released")
        self.assertFalse(state._speaker_muted, "drain muted a device that had woken up")
        self.assertFalse(state._sleepy_auto_muted_speaker)

    def test_a_repeat_sleepy_does_not_stack_drains(self):
        """presence.away / night scene can re-send sleepy on a sleeping device."""
        state = self.state
        state.tts_service = FakeTTS()
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        first = state._sleepy_drain_cancel
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self.assertIsNot(state._sleepy_drain_cancel, first)
        self.assertTrue(first.is_set(), "the superseded drain was left running")
        self.assertTrue(self._wait_for_mute())

    def test_music_still_stops_immediately(self):
        """Music is not an announcement; it has no claim on the drain window."""
        class FakeMusic:
            playing = True
            stopped = False

            def stop(self):
                FakeMusic.stopped = True
                FakeMusic.playing = False

        state = self.state
        state.tts_service = FakeTTS()
        state.music_service = FakeMusic()
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self.assertTrue(FakeMusic.stopped, "music kept playing into sleep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
