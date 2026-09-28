"""Fixture contracts for system personas and legacy text-prefix cleanup."""
from patch_fixture_support import contract_suite


class PatchContract(contract_suite('caller_context_persona')):
    def test_removes_old_user_text_prefix(self):
        self.bb.write_text('''class BlueBubbles:
    def receive(self, text):
        # _CALLER_CONTEXT_APPLIED
        _caller_ctx = "shop"
        _nl = "\\n"
        text = _caller_ctx + _nl + _nl + "Customer message: " + text
        return text
''')
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        namespace = {}
        exec(self.bb.read_text(), namespace)
        self.assertEqual(namespace['BlueBubbles']().receive('hello'), 'hello')
        self.assertNotIn('_CALLER_CONTEXT_APPLIED', self.bb.read_text())
