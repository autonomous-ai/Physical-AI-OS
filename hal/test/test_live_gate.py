import unittest

import numpy as np

from hal.drivers.voice._internal.live_gate import AdaptiveLiveGate


class GateTests(unittest.TestCase):
    RATE = 16000

    def frame(self, level, seconds=0.02):
        return np.full(round(self.RATE * seconds), round(level * 32768), dtype=np.int16)

    def test_echo_silenced_and_buffers_bounded(self):
        gate = AdaptiveLiveGate()
        for _ in range(500):
            output = gate.process(self.frame(0.025), self.RATE, True, 0.15)
            self.assertFalse(np.any(output))
            self.assertLessEqual(gate.buffered_samples, self.RATE * 0.3)
        self.assertFalse(gate.speaking)
        self.assertFalse(gate.duck)

    def test_warmup_uses_written_audio_not_capture_time_and_drops_early_echo(self):
        gate = AdaptiveLiveGate()
        for _ in range(250):
            result = gate.process(self.frame(0.3), self.RATE, True, 0.1,
                                  playback_seconds=2.99)
            self.assertFalse(np.any(result))
            self.assertFalse(gate.duck)
            self.assertFalse(gate.barge_in)
        self.assertEqual(gate.buffered_samples, 0)
        for i in range(12):
            result = gate.process(self.frame(0.3), self.RATE, True, 0.1,
                                  playback_seconds=3.0)
            self.assertFalse(gate.barge_in)
            self.assertFalse(np.any(result))
        self.assertFalse(gate.duck)
        # A rejected warm-up utterance must end before a fresh onset can pass.
        for _ in range(25):
            gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        for i in range(12):
            result = gate.process(self.frame(0.3), self.RATE, True, 0.1,
                                  playback_seconds=3.0)
            self.assertEqual(gate.barge_in, i == 11)
        self.assertTrue(gate.duck)
        self.assertEqual(np.count_nonzero(result), 12 * 320)

    def test_sustained_echo_crossing_warmup_never_becomes_new_speech(self):
        gate = AdaptiveLiveGate()
        echo = np.full(320, 6000, dtype=np.int16)
        for i in range(250):
            result = gate.process(echo, self.RATE, True, 400 / 32768,
                                  playback_seconds=(i + 1) * 0.02)
            self.assertFalse(np.any(result))
            self.assertFalse(gate.speaking)
            self.assertFalse(gate.speech_started)
            self.assertFalse(gate.barge_in)
            self.assertFalse(gate.duck)
        for _ in range(25):
            gate.process(self.frame(0), self.RATE, True, 400 / 32768,
                         playback_seconds=5.0)
        for i in range(12):
            result = gate.process(echo, self.RATE, True, 400 / 32768,
                                  playback_seconds=5.0)
            self.assertEqual(gate.barge_in, i == 11)
        self.assertTrue(gate.speaking)
        self.assertEqual(np.count_nonzero(result == 6000), 12 * 320)
        self.assertGreaterEqual(gate.replayed_samples, 11 * 320)

    def test_warmup_does_not_block_idle_microphone(self):
        gate = AdaptiveLiveGate()
        frame = self.frame(0.3)
        for _ in range(8):
            np.testing.assert_array_equal(gate.process(frame, self.RATE, False, 0), frame)
        self.assertTrue(gate.speaking)

    def test_speech_prefix_and_current_frame_not_duplicated(self):
        gate = AdaptiveLiveGate()
        frames = []
        for i in range(12):
            frame = np.full(320, 9000 + i, dtype=np.int16)
            frames.append(frame)
            result = gate.process(frame, self.RATE, True, 0.1, playback_seconds=3.0)
            if i < 11:
                self.assertFalse(np.any(result))
        self.assertTrue(gate.speaking)
        self.assertTrue(gate.duck)
        np.testing.assert_array_equal(result, np.concatenate(frames))
        following = self.frame(0.3)
        np.testing.assert_array_equal(gate.process(following, self.RATE, True, 0.1, playback_seconds=3.0), following)
        self.assertEqual(gate.buffered_samples, 0)

    def test_idle_forwarded_and_subthreshold_playback_noise_tracked(self):
        gate = AdaptiveLiveGate()
        low = self.frame(0.001)
        for _ in range(100):
            np.testing.assert_array_equal(gate.process(low, self.RATE, False, 0), low)
        self.assertLess(gate.noise, 0.0011)
        noise = gate.noise
        for _ in range(100):
            gate.process(self.frame(0.05), self.RATE, True, 0.5)
        self.assertGreater(gate.noise, noise)

    def test_playback_residual_updates_noise_floor_like_demo(self):
        for dt in (0.02, 0.064):
            gate = AdaptiveLiveGate()
            elapsed = 0
            while elapsed + dt <= 10.000001:
                gate.process(self.frame(600 / 32768, dt), self.RATE, True,
                             400 / 32768, playback_seconds=elapsed)
                elapsed += dt
            self.assertAlmostEqual(gate.noise * 32768, 559, delta=2)
            self.assertAlmostEqual(gate.threshold * 32768, 2794, delta=10)
            self.assertFalse(gate.barge_in)

    def test_confirmation_duration_independent_of_frame_size(self):
        for dt in (0.01, 0.02, 0.032, 0.064):
            gate = AdaptiveLiveGate()
            elapsed = 0
            while not gate.speaking:
                gate.process(self.frame(0.3, dt), self.RATE, True, 0.1, playback_seconds=3.0)
                elapsed += dt
                self.assertLess(elapsed, 0.4)
            self.assertGreaterEqual(elapsed + 1e-9, 0.24)
            self.assertLess(elapsed, 0.24 + dt + 1e-9)

    def test_end_and_unduck(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        for _ in range(25):
            gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertFalse(gate.speaking)
        for _ in range(50):
            gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertFalse(gate.duck)

    def test_tail_and_no_replay_when_playback_ends(self):
        gate = AdaptiveLiveGate()
        gate.process(self.frame(0.001), self.RATE, True, 0.1, playback_seconds=3.0)
        for _ in range(14):
            self.assertFalse(np.any(gate.process(self.frame(0.001), self.RATE, False, 0)))
        frame = self.frame(0.001)
        for _ in range(2):
            result = gate.process(frame, self.RATE, False, 0)
        np.testing.assert_array_equal(result, frame)
        self.assertEqual(gate.buffered_samples, 0)

    def test_short_reply_tail_admits_idle_speech_without_warmup(self):
        gate = AdaptiveLiveGate()
        gate.process(self.frame(0.001), self.RATE, True, 0.1, playback_seconds=1.0)
        for i in range(8):
            result = gate.process(self.frame(0.02), self.RATE, False, 0,
                                  playback_seconds=1.0)
            if i < 7:
                self.assertFalse(np.any(result))
        self.assertTrue(gate.risk)
        self.assertTrue(gate.speaking)
        self.assertFalse(gate.duck)
        self.assertFalse(gate.barge_in)
        self.assertEqual(np.count_nonzero(result), 8 * 320)
        self.assertEqual(gate.replayed_samples, 7 * 320)

    def test_unduck_timer_retries_after_long_speech_ends(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        # The 1.5 s deadline passes while speaking; retry is due at 1.9 s.
        for _ in range(80):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        for _ in range(34):
            gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertFalse(gate.speaking)
        self.assertTrue(gate.duck)
        # Release at the 2.3 s retry: only 700 ms after speech ended.
        gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertFalse(gate.duck)

    def test_provider_clear_duck_preserves_speech_and_does_not_rearm(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertTrue(gate.duck)
        gate.clear_duck()
        self.assertTrue(gate.speaking)
        self.assertIsNone(gate._unduck_at)
        gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertFalse(gate.duck)
        self.assertFalse(gate.barge_in)

    def test_duck_releases_when_echo_tail_ends_before_timer(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        for _ in range(16):
            gate.process(self.frame(0.3), self.RATE, False, 0, playback_seconds=3.0)
        self.assertFalse(gate.risk)
        self.assertFalse(gate.duck)

    def test_reset_clears_duck_and_state(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertTrue(gate.duck)
        gate.reset()
        self.assertFalse(gate.duck)
        self.assertFalse(gate.speaking)
        self.assertEqual(gate.buffered_samples, 0)
        self.assertEqual(gate.coupling_db, -8)
        self.assertEqual(gate.noise, 0.003)

    def test_existing_idle_speech_requires_fresh_playback_confirmation(self):
        gate = AdaptiveLiveGate()
        for _ in range(8):
            gate.process(self.frame(0.3), self.RATE, False, 0)
        self.assertTrue(gate.speaking)
        interruptions = 0
        for i in range(20):
            result = gate.process(self.frame(0.3), self.RATE, True, 0.1, playback_seconds=3.0)
            interruptions += int(gate.barge_in)
            if i < 11:
                self.assertFalse(np.any(result))
            if i == 11:
                self.assertEqual(gate.replayed_samples, 11 * 320)
        self.assertEqual(interruptions, 1)

    def test_mono_column_and_rate_change_reset(self):
        gate = AdaptiveLiveGate()
        for _ in range(12):
            gate.process(self.frame(0.3).reshape(-1, 1), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertTrue(gate.duck)
        output = gate.process(np.full((480, 1), 9000, np.int16), 24000, True, 0.1, playback_seconds=3.0)
        self.assertEqual(output.shape, (480,))
        self.assertFalse(np.any(output))
        self.assertFalse(gate.duck)
        self.assertFalse(gate.speaking)
        self.assertEqual(gate.buffered_samples, 480)

    def test_sudden_loud_noise_is_not_learned_as_ambient(self):
        gate = AdaptiveLiveGate()
        initial = gate.noise
        for _ in range(100):
            gate.idle_threshold(0.1, duration=0.064)
        self.assertEqual(gate.noise, initial)

    def test_coupling_floor_and_output_envelope_expiry(self):
        gate = AdaptiveLiveGate()
        for _ in range(1600):
            gate.process(self.frame(0), self.RATE, True, 0.1, playback_seconds=3.0)
        self.assertEqual(gate.coupling_db, -30.0)
        for _ in range(26):
            gate.process(self.frame(0), self.RATE, False, 0)
        self.assertEqual(gate.output_envelope, 0)
        self.assertFalse(gate.risk)


if __name__ == '__main__':
    unittest.main()
