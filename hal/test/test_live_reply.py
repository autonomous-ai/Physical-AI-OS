"""Late synthesis/text must not restart a provider-interrupted live reply."""
import threading
import unittest
from hal.drivers.voice._internal.live_reply import LiveReplyGuard


class LiveReplyTests(unittest.TestCase):
    def test_cancel_after_provider_terminal_blocks_tail_and_accepts_new_turn(self):
        guard = LiveReplyGuard()
        calls = []
        guard.play('old', lambda: calls.append('old'))
        guard.cancel(lambda: calls.append('stop'))
        self.assertFalse(guard.play('old', lambda: calls.append('late')))
        self.assertFalse(guard.observe('old'))
        self.assertTrue(guard.play('new', lambda: calls.append('new')))
        self.assertEqual(calls, ['old', 'stop', 'new'])

    def test_server_cancel_targets_old_reply_without_poisoning_new_identity(self):
        guard = LiveReplyGuard()
        guard.observe('new')
        stops = []
        guard.cancel(lambda: stops.append('stop'), key='old')
        self.assertEqual(stops, [])
        self.assertTrue(guard.allowed('new'))
        self.assertFalse(guard.allowed('old'))

    def test_cancellation_serializes_with_inflight_enqueue(self):
        guard = LiveReplyGuard()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def enqueue():
            entered.set()
            self.assertTrue(release.wait(2))
            calls.append('queued')
        writer = threading.Thread(target=lambda: guard.play('old', enqueue))
        stopper = threading.Thread(target=lambda: (guard.cancel(lambda: calls.append('stop')), cancelled.set()))
        writer.start()
        self.assertTrue(entered.wait(2))
        stopper.start()
        self.assertFalse(cancelled.wait(.02))
        release.set()
        writer.join(2)
        stopper.join(2)
        self.assertFalse(writer.is_alive() or stopper.is_alive())
        self.assertEqual(calls, ['queued', 'stop'])
        self.assertFalse(guard.play('old', lambda: calls.append('late')))


if __name__ == '__main__':
    unittest.main()
