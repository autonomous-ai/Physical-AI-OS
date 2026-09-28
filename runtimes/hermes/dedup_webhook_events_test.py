"""Fixture contracts for subscription edits, including compact dictionaries."""
from patch_fixture_support import BB, contract_suite


class PatchContract(contract_suite('dedup_webhook_events')):
    def test_onboarding_patch_order_compiles_and_repeats(self):
        order = [
            'caller_context_persona', 'caller_context_file_fallback',
            'strip_markers', 'typing_indicator', 'dedup_webhook_events',
            'imessage_only_service_filter', 'relax_chat_guid_check',
            'sms_prefix_drop', 'sender_short_code_drop',
        ]
        for name in order:
            result = self.apply(name)
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in (self.bb, self.run):
                compile(path.read_text(), str(path), 'exec')
        before = (self.bb.read_bytes(), self.run.read_bytes())
        for name in order:
            result = self.apply(name)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (self.bb.read_bytes(), self.run.read_bytes()))

    def test_inline_and_multiline_payloads_preserve_other_fields(self):
        for events in ['["new-message", "updated-message"]', '[\n            "new-message",\n            "updated-message"\n        ]']:
            with self.subTest(events=events):
                source = BB.replace('["new-message", "updated-message"]', events)
                source += '\n_MESSAGE_EVENTS = {"new-message", "updated-message"}\n'
                self.bb.write_text(source)
                result = self.apply()
                self.assertEqual(result.returncode, 0, result.stderr)
                namespace = {}
                exec(self.bb.read_text(), namespace)
                self.assertEqual(namespace['BlueBubbles']().registration(), {'events': ['new-message'], 'url': 'callback'})
                self.assertEqual(namespace['_MESSAGE_EVENTS'], {'new-message', 'updated-message'})
