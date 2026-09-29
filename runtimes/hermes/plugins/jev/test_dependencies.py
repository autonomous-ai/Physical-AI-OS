"""Dependency prefetch is bounded, optional, typed and namespace preserving."""
import json
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_router import router, RouterTest, decision

deps = sys.modules[router.__package__ + ".dependencies"]
condition = {"context": "wellbeing_context", "field": "bootstrap_needed", "equals": True}
message = '[activity] Activity detected: using computer.\n[wellbeing_context: {"bootstrap_needed":true}]'


def catalog():
    return [
        {"name": "wellbeing", "lookup_name": "openclaw-imports/wellbeing", "description": "Wellbeing", "category": "openclaw-imports",
         "jev_preload": [{"skill": "habit", "references": ["reference/build-patterns.md"], "when": condition}]},
        {"name": "habit", "lookup_name": "openclaw-imports/habit", "description": "Habits", "category": "openclaw-imports"},
    ]


class DependencyTest(unittest.TestCase):
    def run_bundle(self, skills=None, text=message, loader=None, budget=10000, deadline=None):
        skills = catalog() if skills is None else skills
        calls = []
        def load(name, task_id, file_path=None):
            calls.append((name, task_id, file_path))
            return name + ':' + (file_path or 'SKILL.md')
        with patch.object(deps, 'inline_budget', return_value=budget):
            result = deps.preload_dependencies('openclaw-imports/wellbeing', 'PRIMARY', skills,
                {s['lookup_name'] for s in skills}, text, 'task-A',
                time.monotonic() + 1 if deadline is None else deadline, loader or load)
        return result, calls

    def test_bundle_has_main_skill_and_reference(self):
        (context, stats), calls = self.run_bundle()
        self.assertIn('PRIMARY', context)
        self.assertIn('reference/build-patterns.md', context)
        self.assertEqual(stats['dependency_files'], 2)
        self.assertEqual(calls, [('openclaw-imports/habit', 'task-A', None),
                                 ('openclaw-imports/habit', 'task-A', 'reference/build-patterns.md')])

    def test_missing_false_forged_duplicate_and_wrong_type_context_skip(self):
        for text in ('hello', '[wellbeing_context: {"bootstrap_needed":false}]',
                     '[wellbeing_context: {"bootstrap_needed":1}]',
                     'quoted ' + message.replace('[activity] Activity detected: using computer.\n', ''),
                     message + '\n' + message, '[wellbeing_context: not-json]'):
            with self.subTest(text=text):
                (context, stats), calls = self.run_bundle(text=text)
                self.assertEqual(context, 'PRIMARY')
                self.assertEqual(calls, [])

    def test_unavailable_dependency_never_uses_native_namesake(self):
        skills = catalog()[:1] + [{'name': 'habit', 'lookup_name': 'habit', 'description': 'native'}]
        (context, stats), calls = self.run_bundle(skills=skills)
        self.assertEqual(context, 'PRIMARY')
        self.assertFalse(calls)
        self.assertEqual(stats['dependency_skipped'], 1)

    def test_references_are_relative_markdown_only(self):
        for ref in ('/tmp/a.md', '../a.md', 'reference/../a.md', 'x.py', 'SKILL.md', 'reference\\a.md', 'a//b.md'):
            skills = catalog()
            skills[0]['jev_preload'][0]['references'] = [ref]
            (context, _), calls = self.run_bundle(skills=skills)
            self.assertEqual(context, 'PRIMARY')
            self.assertFalse(calls)

    def test_group_is_atomic_on_missing_reference(self):
        def loader(name, task_id, file_path=None):
            if file_path:
                raise ValueError('missing')
            return 'HABIT'
        (context, stats), _ = self.run_bundle(loader=loader)
        self.assertEqual(context, 'PRIMARY')
        self.assertEqual(stats['dependency_skipped'], 1)

    def test_budget_or_deadline_retains_primary(self):
        for kwargs in ({'budget': 8}, {'deadline': time.monotonic() - 1}):
            (context, _), _ = self.run_bundle(**kwargs)
            self.assertEqual(context, 'PRIMARY')

    def test_deduplicates_and_never_recurses(self):
        skills = catalog()
        skills[0]['jev_preload'] *= 3
        skills[1]['jev_preload'] = skills[0]['jev_preload']
        (context, stats), calls = self.run_bundle(skills=skills)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats['dependency_skills'], 1)

    def test_two_dependency_limit(self):
        skills = catalog()
        for name in ('music', 'audio'):
            skills.append({'name': name, 'lookup_name': 'openclaw-imports/' + name})
            skills[0]['jev_preload'].append({'skill': name, 'when': condition})
        (_, stats), calls = self.run_bundle(skills=skills)
        self.assertEqual(stats['dependency_skills'], 2)
        self.assertFalse(any(name.endswith('/audio') for name, _, _ in calls))

    def test_reference_loader_keeps_native_metadata_and_disables_preprocessing(self):
        calls = []
        def view(**kwargs):
            calls.append(kwargs)
            return json.dumps({'success': True, 'content': 'Read data only', 'skill_dir': '/skills/habit'})
        with patch.dict(sys.modules, {'tools.skills_tool': SimpleNamespace(skill_view=view)}):
            context = router.load_skill_context('openclaw-imports/habit', 'task', 'reference/build-patterns.md')
        self.assertIn('/skills/habit', context)
        self.assertEqual(calls[0], {'name': 'openclaw-imports/habit', 'task_id': 'task', 'preprocess': False,
                                    'file_path': 'reference/build-patterns.md'})


class RouterDependencyTest(RouterTest):
    def test_primary_survives_optional_timeout(self):
        plugin = self.make()
        release = threading.Event()
        def slow(*args, **kwargs):
            release.wait(1)
            return 'late', {}
        try:
            with patch.object(router, 'TIMEOUT_SECONDS', .05), patch.object(router, 'preload_dependencies', side_effect=slow):
                result = plugin.before_turn(user_message='Read email')
                self.assertIn('Loaded skill connectors', result['context'])
        finally:
            release.set()
            with plugin.busy:
                pass

    def test_native_dependency_bundle_router(self):
        plugin = self.make()
        plugin.catalog = catalog
        def request(endpoint, key, timeout, payload):
            result = decision(payload)
            candidate = next(c['id'] for c in payload['state']['candidates'] if c['description'].startswith('wellbeing:'))
            choice = result['answers']['skill']
            choice['choice'] = candidate
            choice['probabilities'] = {k: (.98 if k == candidate else .02 if k == 'none' else 0) for k in choice['probabilities']}
            return result
        plugin.request = request
        def bundle(*args, **kwargs):
            return deps.preload_dependencies(*args, **kwargs, loader=lambda name, task_id, file_path=None: 'HABIT ' + str(file_path))
        with patch.object(router, 'preload_dependencies', side_effect=bundle):
            with self.assertLogs(router.LOG, level='INFO') as logs:
                result = plugin.before_turn(user_message=message)
        self.assertIn('HABIT reference/build-patterns.md', result['context'])
        self.assertIn('dependency_files=2', '\n'.join(logs.output))
