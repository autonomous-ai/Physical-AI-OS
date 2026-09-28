"""Execute embedded installers against disposable, executable upstream fixtures."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
BB_PATH = '/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py'
RUN_PATH = '/usr/local/lib/hermes-agent/gateway/run.py'
BB = '''from __future__ import annotations
import asyncio
from types import (
    SimpleNamespace,
)
web = SimpleNamespace(Response=lambda **kwargs: "dropped")
class Platform:
    pass
class BlueBubbles(Platform):
    async def receive(self, record, payload):
        is_from_me = record.get("isFromMe", False)
        if is_from_me:
            return web.Response(text="ok")
        return "accepted"
    async def send(self, content):
        text = self.format_message(content)
        return text
    async def dispatch(self, event, session_chat_id):
        task = asyncio.create_task(self.handle_message(event))
        await task
    def format_message(self, content):
        return content
    def registration(self):
        return {"events": ["new-message", "updated-message"], "url": "callback"}
'''
RUN = '''class Gateway:
    def _get_system_prompt_for_channel(self, platform, chat_id):
        """Resolve persona."""
        return "default"
'''
CHAIN = ['imessage_only_service_filter', 'relax_chat_guid_check', 'sms_prefix_drop', 'sender_short_code_drop']


def contract_suite(patch):
    class PatchContract(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.directory = Path(self.temp.name)
            self.bb = self.directory / 'bluebubbles.py'
            self.run = self.directory / 'run.py'
            self.bb.write_text(BB)
            self.run.write_text(RUN)
            prerequisites = CHAIN[:CHAIN.index(patch)] if patch in CHAIN else []
            if patch == 'caller_context_file_fallback':
                prerequisites = ['caller_context_persona']
            for prerequisite in prerequisites:
                self.assertEqual(self.apply(prerequisite).returncode, 0)
            self.target = self.run if patch.startswith('caller_context') else self.bb

        def apply(self, name=patch):
            source = (ROOT / 'patches' / (name + '.py')).read_text()
            source = source.replace(BB_PATH, str(self.bb)).replace(RUN_PATH, str(self.run))
            return subprocess.run([sys.executable, '-B', '-c', source], capture_output=True, text=True)

        def test_apply_compile_and_repeat(self):
            result = self.apply()
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in (self.bb, self.run):
                compile(path.read_text(), str(path), 'exec')
            before = (self.bb.read_bytes(), self.run.read_bytes())
            result = self.apply()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, (self.bb.read_bytes(), self.run.read_bytes()))

        def test_unsupported_anchor_leaves_source_unchanged(self):
            self.target.write_text('value = 1\n')
            result = self.apply()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.target.read_text(), 'value = 1\n')

        def test_invalid_result_never_overwrites_source(self):
            original = self.target.read_text() + '\ninvalid syntax here!\n'
            self.target.write_text(original)
            result = self.apply()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.target.read_text(), original)

        def test_missing_target(self):
            self.target.unlink()
            self.assertEqual(self.apply().returncode, 2)

        def test_patched_behavior(self):
            result = self.apply()
            self.assertEqual(result.returncode, 0, result.stderr)
            namespace = {}
            exec(self.target.read_text(), namespace)
            if patch.startswith('caller_context'):
                from unittest.mock import mock_open, patch as mock_patch
                instance = namespace['Gateway']()
                with mock_patch.dict('os.environ', {'BLUEBUBBLES_CALLER_CONTEXT': 'Shop persona'}):
                    self.assertEqual(instance._get_system_prompt_for_channel('bluebubbles', 'chat'), 'Shop persona')
                    self.assertEqual(instance._get_system_prompt_for_channel('telegram', 'chat'), 'default')
                if patch.endswith('file_fallback'):
                    with mock_patch.dict('os.environ', {'BLUEBUBBLES_CALLER_CONTEXT': ''}), mock_patch('builtins.open', mock_open(read_data='File persona\n')):
                        self.assertEqual(instance._get_system_prompt_for_channel('bluebubbles', 'chat'), 'File persona')
                return
            instance = namespace['BlueBubbles']()
            if patch == 'dedup_webhook_events':
                self.assertEqual(instance.registration(), {'events': ['new-message'], 'url': 'callback'})
            elif patch == 'strip_markers':
                self.assertEqual(asyncio.run(instance.send('[ignore] Hello [HW:/audio/play:{"q":"hi"}]')), 'Hello')
                self.assertEqual(asyncio.run(instance.send('↪ Redirected current run elsewhere')), '')
            elif patch == 'typing_indicator':
                async def scenario():
                    events = []
                    async def typing(chat):
                        events.append(chat)
                    async def handle(event):
                        await asyncio.sleep(0)
                        raise ValueError('handler failed')
                    instance.send_typing = typing
                    instance.handle_message = handle
                    with self.assertRaisesRegex(ValueError, 'handler failed'):
                        await instance.dispatch('event', 'chat')
                    self.assertEqual(events, ['chat'])
                    self.assertEqual(len(asyncio.all_tasks()), 1)
                asyncio.run(scenario())
            else:
                def receive(record):
                    return asyncio.run(instance.receive(record, {}))
                self.assertEqual(receive({'service': 'SMS'}), 'dropped')
                self.assertEqual(receive({'service': 'iMessage', 'handle': {'address': '+123456789'}}), 'accepted')
                if patch != CHAIN[0]:
                    self.assertEqual(receive({'service': 'iMessage', 'chatGuid': 'any;-;+123456789'}), 'accepted')
                if patch in CHAIN[2:]:
                    self.assertEqual(receive({'chats': [{'guid': 'SMS;-;888'}]}), 'dropped')
                if patch == CHAIN[3]:
                    for address in ['888', '18001091']:
                        self.assertEqual(receive({'handle': {'address': address}}), 'dropped')
                    self.assertEqual(receive({'sender': 'person@example.com'}), 'accepted')
    PatchContract.__name__ = ''.join(word.title() for word in patch.split('_')) + 'Contract'
    return PatchContract
