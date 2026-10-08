"""Behavior tests for the schedule helper; reads temp files only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts/schedule.py'


def load_module():
    spec = importlib.util.spec_from_file_location('schedule_skill', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScheduleHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = patch.dict(os.environ, {'OS_CONFIG_PATH': str(self.dir / 'config.json')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.mod = load_module()

    def write(self, name, data):
        (self.dir / name).write_text(data if isinstance(data, str) else json.dumps(data))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.mod.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def sample(self):
        return {
            'timezone': 'Asia/Ho_Chi_Minh',
            'schedules': [
                {'id': 'a1', 'name': 'Morning brief', 'enabled': True, 'kind': 'agent',
                 'instructions': 'Read the news.', 'requires': ['gmail'],
                 'schedule': {'repeat': 'weekly', 'days': [1, 2], 'time': '08:00'},
                 'next_run_at': '2026-10-12T01:02:00Z', 'last_run_at': '2026-10-08T01:01:00Z',
                 'last_run_status': 'skipped', 'last_run_summary': 'missing connector: gmail'},
                {'id': 'b2', 'name': 'Water', 'enabled': False,
                 'schedule': {'repeat': 'interval', 'every_ms': 5400000}},
            ],
        }

    def test_list_counts_and_times_in_device_timezone(self):
        self.write('schedules.json', self.sample())
        code, out, _ = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertIn('Scheduled tasks: 2 (1 enabled, 1 disabled)', out)
        self.assertIn('weekly on Monday, Tuesday at 08:00', out)
        self.assertIn('next run: 2026-10-12 08:02 Asia/Ho_Chi_Minh', out)
        self.assertIn('last run: 2026-10-08 08:01 skipped (missing connector: gmail)', out)
        self.assertIn('needs connectors: gmail', out)
        self.assertIn('every 90 min', out)
        self.assertIn('agent runtime created with its own scheduler', out)

    def test_disabled_task_has_no_next_run(self):
        self.write('schedules.json', self.sample())
        _, out, _ = self.run_cli('list')
        water = out.split('- Water')[1]
        self.assertNotIn('next run', water)

    def test_missing_file_is_empty_list(self):
        code, out, _ = self.run_cli('list')
        self.assertEqual(code, 0)
        self.assertIn('Scheduled tasks: 0 (0 enabled, 0 disabled)', out)

    def test_corrupt_file_fails_with_empty_stdout(self):
        self.write('schedules.json', '{not json')
        code, out, err = self.run_cli('list')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('not valid JSON', err)

    def test_pending_changes_are_marked(self):
        self.write('schedules.json', self.sample())
        self.write('schedule-intents.json', {'intents': [
            {'intent_id': 'i1', 'op': 'delete', 'schedule_id': 'a1'},
            {'intent_id': 'i2', 'op': 'create',
             'schedule': {'name': 'Stretch', 'schedule': {'repeat': 'daily', 'time': '10:00'}}},
        ]})
        _, out, _ = self.run_cli('list')
        self.assertIn('a delete was requested on the device and is not confirmed yet', out)
        self.assertIn('Stretch  [not confirmed yet]  daily at 10:00', out)
        self.assertIn('will not run until confirmed', out)

    def test_show_and_unknown_id(self):
        self.write('schedules.json', self.sample())
        code, out, _ = self.run_cli('show', 'a1')
        self.assertEqual(code, 0)
        self.assertIn('instructions: Read the news.', out)
        code, out, err = self.run_cli('show', 'zzz')
        self.assertEqual((code, out), (3, ''))
        self.assertIn('no scheduled task with id zzz', err)

    def test_usage_error(self):
        code, out, err = self.run_cli('delete', 'a1')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('schedule.py list', err)

    def test_unknown_timezone_falls_back_to_utc(self):
        data = self.sample()
        data['timezone'] = 'Not/AZone'
        self.write('schedules.json', data)
        _, out, _ = self.run_cli('list')
        self.assertIn('next run: 2026-10-12 01:02', out)


if __name__ == '__main__':
    unittest.main()
