"""Behavior tests for retained target and uncertain-send protection; no network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('harness_skill', Path(__file__).parents[1] / 'scripts/harness.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class HarnessSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'voice.json'
        self.connection = {'paired': True, 'connected': True, 'machine_id': 'machine'}
        api_mock = patch.object(harness, 'api', side_effect=lambda *args, **kwargs: dict(self.connection))
        api_mock.start()
        self.addCleanup(api_mock.stop)
        self.mutations = []
        self.uncertain = False

    def request(self, kind, **fields):
        if kind == 'agents.list':
            return {'machineId': 'machine', 'agents': [{'agentId': 'a', 'name': 'Project'}, {'agentId': 'b', 'name': 'Other'}]}
        if kind in ('turn.send', 'question.answer'):
            self.mutations.append(fields)
            if self.uncertain:
                raise OSError('response lost')
            return {'receipt': {'state': 'queued'}}
        if kind == 'receipt.get':
            return {'receipt': {'state': 'started'}}
        return {'state': 'running', **fields}

    def selection_api(self, decision):
        def call(path, payload=None, timeout=40):
            if path == '/select-agent':
                self.assertEqual(payload, {'agentId': 'a', 'machineId': 'machine', 'text': 'Add trees'})
                self.assertEqual(timeout, 4)
                if isinstance(decision, Exception):
                    raise decision
                return decision
            return dict(self.connection)
        return call

    def test_jev_target_owns_delivery_and_persisted_task(self):
        decision = {'mode': 'jev', 'machineId': 'machine', 'agentId': 'b'}
        with patch.object(harness, 'api', side_effect=self.selection_api(decision)), patch.object(harness, 'request', self.request):
            harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
        state = json.loads(self.path.read_text())['voice']
        self.assertEqual(self.mutations[0]['agentId'], 'b')
        self.assertEqual(state['agentId'], 'b')
        self.assertEqual(state['lastTask']['agentId'], 'b')
        self.assertEqual(state['lastTask']['receiptState'], 'queued')
        self.assertNotIn('pending', state)

    def test_jev_uncertain_unavailable_and_invalid_selection_keep_main_target(self):
        for decision in [None, OSError('timeout'), ValueError('bad JSON'), [],
                         {'mode': 'fallback', 'machineId': 'machine', 'agentId': 'b'},
                         {'mode': 'disabled', 'machineId': 'machine', 'agentId': 'b'},
                         {'mode': 'jev', 'machineId': 'other', 'agentId': 'b'},
                         {'mode': 'jev', 'machineId': 'machine', 'agentId': 'missing'},
                         {'mode': 'jev', 'machineId': 'machine', 'agentId': []}]:
            with self.subTest(decision=decision), patch.object(harness, 'api', side_effect=self.selection_api(decision)), patch.object(harness, 'request', self.request):
                harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
                self.assertEqual(self.mutations[-1]['agentId'], 'a')
                self.assertEqual(json.loads(self.path.read_text())['voice']['lastTask']['agentId'], 'a')

    def test_jev_duplicate_candidate_falls_back_to_main(self):
        def rpc(kind, **fields):
            result = self.request(kind, **fields)
            if kind == 'agents.list':
                result['agents'].append({'agentId': 'b', 'name': 'Duplicate'})
            return result
        decision = {'mode': 'jev', 'machineId': 'machine', 'agentId': 'b'}
        with patch.object(harness, 'api', side_effect=self.selection_api(decision)), patch.object(harness, 'request', rpc):
            harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'a')

    def test_jev_lost_ack_preserves_final_target_and_does_not_reselect(self):
        self.uncertain = True
        decision = {'mode': 'jev', 'machineId': 'machine', 'agentId': 'b'}
        with patch.object(harness, 'api', side_effect=self.selection_api(decision)) as api, patch.object(harness, 'request', self.request):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
            state = json.loads(self.path.read_text())['voice']
            self.assertEqual(state['pending']['agentId'], 'b')
            self.assertEqual(state['lastTask']['agentId'], 'b')
            before = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'unresolved'):
                harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(sum(c.args[0] == '/select-agent' for c in api.call_args_list), 1)
            self.assertEqual(len(self.mutations), 1)

    def test_connection_identity_change_during_selection_does_not_reserve(self):
        for changed in [{'machine_id': 'other'}, {'server_instance_id': 'restarted'}]:
            self.connection = {'paired': True, 'connected': True, 'machine_id': 'machine'}
            def call(path, payload=None, timeout=40):
                if path == '/select-agent':
                    self.connection.update(changed)
                    return {'mode': 'jev', 'machineId': 'machine', 'agentId': 'b'}
                return dict(self.connection)
            with self.subTest(changed=changed), patch.object(harness, 'api', side_effect=call), patch.object(harness, 'request', self.request):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    harness.run('send', {'agentId': 'a', 'text': 'Add trees'}, self.path)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.mutations, [])

    def test_answer_and_stop_never_call_selector(self):
        with patch.object(harness, 'api', side_effect=lambda *args, **kwargs: dict(self.connection)) as api, patch.object(harness, 'request', self.request):
            harness.run('answer', {'agentId': 'a', 'questionRequestId': 'q', 'answers': {}}, self.path)
            harness.run('stop', {'agentId': 'a'}, self.path)
            self.assertFalse(any(c.args[0] == '/select-agent' for c in api.call_args_list))

    def test_disconnected_actions_do_not_dispatch_or_change_saved_state(self):
        original = {'voice': {'agentId': 'a', 'machineId': 'machine',
                              'pending': {'machineId': 'machine', 'idempotencyKey': 'old'}}}
        self.path.write_text(json.dumps(original))
        before = self.path.read_bytes()
        for paired, code in [(False, 'HARNESS_UNPAIRED'), (True, 'HARNESS_OFFLINE')]:
            self.connection.update(paired=paired, connected=False)
            for action in ['list', 'select', 'send', 'stop', 'status', 'recap', 'answer', 'receipt']:
                with self.subTest(paired=paired, action=action), patch.object(harness, 'request') as request:
                    with self.assertRaisesRegex(ValueError, code):
                        harness.run(action, {'agentId': 'a', 'text': 'work'}, self.path)
                    request.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), before)

    def test_offline_send_does_not_reserve_and_reconnect_allows_explicit_send(self):
        self.connection['connected'] = False
        with patch.object(harness, 'request', self.request):
            with self.assertRaisesRegex(ValueError, 'HARNESS_OFFLINE'):
                harness.run('send', {'agentId': 'a', 'text': 'work'}, self.path)
            self.assertFalse(self.path.exists())
            self.connection['connected'] = True
            self.assertEqual(self.mutations, [])
            harness.run('send', {'agentId': 'a', 'text': 'work'}, self.path)
            self.assertEqual(len(self.mutations), 1)

    def test_local_resolution_and_empty_receipt_work_offline(self):
        self.connection.update(paired=False, connected=False)
        self.assertEqual(harness.run('receipt', {}, self.path), {'receipt': None, 'pending': False})
        result = harness.run('resolve', {'resolution': 'do_not_retry'}, self.path)
        self.assertFalse(result['resent'])

    def test_no_implicit_target(self):
        with patch.object(harness, 'request', self.request):
            with self.assertRaises(ValueError):
                harness.run('send', {'text': 'do work'}, self.path)
            self.assertEqual(self.mutations, [])

    def test_new_per_turn_namespace_is_rejected_before_network_or_persistence(self):
        with patch.object(harness, 'request') as rpc, patch.object(harness, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'UNSTABLE_CONVERSATION_ID'):
                harness.run('send', {'conversation_id': 'web-turn-1', 'agentId': 'a', 'text': 'work',
                    'response': {'run_id': 'web-turn-1', 'channel': 'web'}}, self.path)
            rpc.assert_not_called()
            api.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_existing_per_turn_namespace_can_resume_without_migration(self):
        self.path.write_text(json.dumps({'web-turn-1': {'agentId': 'b', 'machineId': 'machine'}}))
        with patch.object(harness, 'request', self.request):
            harness.run('send', {'conversation_id': 'web-turn-1', 'agentId': 'b', 'text': 'work',
                'response': {'run_id': 'web-turn-1', 'channel': 'web'}}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'b')
        self.assertEqual(set(json.loads(self.path.read_text())), {'web-turn-1'})

    def test_retained_selection_cannot_authorize_any_mutation(self):
        with patch.object(harness, 'request', self.request):
            harness.run('select', {'agentId': 'a'}, self.path)
            before = self.path.read_bytes()
            for action in ('send', 'answer', 'stop'):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'EXPLICIT_TARGET_REQUIRED'):
                    harness.run(action, {'text': 'add trees', 'questionRequestId': 'q', 'answers': {}}, self.path)
                self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(harness.run('status', {}, self.path)['agentId'], 'a')
        self.assertEqual(self.mutations, [])

    def test_context_is_read_only_offline_and_paginates_without_hiding_tasks(self):
        self.path.write_text(json.dumps({'voice': {'agentId': 'a', 'machineId': 'machine',
            'workflows': {str(i): {'intent_id': str(i), 'text': 'original task ' + str(i),
                                 'agentId': 'a', 'preparationKey': 'secret', 'taskKey': 'secret'}
                          for i in range(23)}}}))
        before = self.path.read_bytes()
        self.connection.update(connected=False, paired=False)
        with patch.object(harness, 'request') as rpc, patch.object(harness, 'api') as api:
            first = harness.run('context', {}, self.path)
            second = harness.run('context', {'offset': first['nextOffset']}, self.path)
            selected = harness.run('context', {'conversation_id': 'voice', 'intent_id': '5'}, self.path)
            self.assertEqual(first['totalTasks'], 23)
            self.assertEqual(len(first['tasks']), 20)
            self.assertTrue(first['truncated'])
            self.assertEqual(len(second['tasks']), 3)
            self.assertIsNone(second['nextOffset'])
            self.assertEqual(selected['tasks'][0]['text'], 'original task 5')
            self.assertNotIn('secret', json.dumps(first))
            rpc.assert_not_called()
            api.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_context_retains_original_normal_request_after_lost_ack(self):
        self.uncertain = True
        with patch.object(harness, 'request', self.request):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'b', 'text': 'Add trees to the garden'}, self.path)
            task = harness.run('context', {}, self.path)['tasks'][0]
        self.assertEqual(task['text'], 'Add trees to the garden')
        self.assertEqual(task['agentId'], 'b')
        self.assertEqual(task['deliveryState'], 'reserved')

    def test_explicit_new_target_overrides_retained_agent_and_owns_followup(self):
        with patch.object(harness, 'request', self.request):
            harness.run('send', {'agentId': 'a', 'text': 'first task'}, self.path)
            harness.run('send', {'agent': 'Other', 'text': 'review the first task'}, self.path)
            harness.run('send', {'agentId': 'b', 'text': 'add tests for that review'}, self.path)
        self.assertEqual([m['agentId'] for m in self.mutations], ['a', 'b', 'b'])

    def test_candidate_inspection_does_not_change_followup_target(self):
        with patch.object(harness, 'request', self.request):
            harness.run('select', {'agentId': 'a'}, self.path)
            harness.run('list', {}, self.path)
            harness.run('status', {'agentId': 'b'}, self.path)
            # Inspection reads only the newest recap/text pair unless more turns are requested.
            self.assertEqual(harness.run('recap', {'agentId': 'b'}, self.path)['n'], 1)
            self.assertEqual(harness.run('recap', {'agentId': 'b', 'n': 3}, self.path)['n'], 3)
            self.assertEqual(self.mutations, [])
            harness.run('send', {'agentId': 'a', 'text': 'continue the original task'}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'a')

    def test_missing_explicit_target_never_falls_back_to_retained_agent(self):
        with patch.object(harness, 'request', self.request):
            harness.run('select', {'agentId': 'a'}, self.path)
            with self.assertRaises(ValueError):
                harness.run('send', {'agent': 'Missing', 'text': 'review'}, self.path)
        self.assertEqual(self.mutations, [])

    def test_ambiguous_name_requires_id_before_dispatch(self):
        base = self.request

        def request(kind, **fields):
            if kind == 'agents.list':
                return {'machineId': 'machine', 'agents': [
                    {'agentId': 'a', 'name': 'Claude Code'},
                    {'agentId': 'b', 'name': 'Claude Code'},
                ]}
            return base(kind, **fields)

        with patch.object(harness, 'request', request):
            with self.assertRaises(ValueError):
                harness.run('send', {'agent': 'Claude Code', 'text': 'review'}, self.path)
            self.assertEqual(self.mutations, [])
            harness.run('send', {'agentId': 'b', 'text': 'review'}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'b')

    def test_list_keeps_bounded_recap_headline_per_agent(self):
        base = self.request

        def request(kind, **fields):
            if kind == 'agents.list':
                return {'machineId': 'machine', 'agents': [
                    {'agentId': 'a', 'name': 'Project', 'engine': 'claude', 'state': 'idle', 'recap': ' Fixed reconnect\n in client.ts '},
                    {'agentId': 'b', 'name': 'Other', 'engine': 'codex', 'state': 'running'},
                    {'agentId': 'c', 'name': 'Long', 'recap': 'x' * (harness.AGENT_RECAP_MAX_CHARS + 500)},
                    {'agentId': 'd', 'name': 'Odd', 'recap': 42},
                ]}
            return base(kind, **fields)

        with patch.object(harness, 'request', request):
            agents = harness.run('list', {}, self.path)['agents']
        self.assertEqual(agents[0]['recap'], 'Fixed reconnect in client.ts')
        # No summarised turn yet → no field at all; the model must read that as unknown.
        self.assertNotIn('recap', agents[1])
        self.assertEqual(len(agents[2]['recap']), harness.AGENT_RECAP_MAX_CHARS)
        self.assertNotIn('recap', agents[3])
        self.assertEqual(self.mutations, [])

    def test_recap_text_never_matches_an_agent_name(self):
        base = self.request

        def request(kind, **fields):
            if kind == 'agents.list':
                return {'machineId': 'machine', 'agents': [
                    {'agentId': 'a', 'name': 'Project', 'recap': 'Other'},
                    {'agentId': 'b', 'name': 'Other', 'recap': 'Project'},
                ]}
            return base(kind, **fields)

        with patch.object(harness, 'request', request):
            harness.run('send', {'agent': 'Other', 'text': 'review'}, self.path)
            with self.assertRaises(ValueError):
                harness.run('send', {'agent': 'Fixed reconnect', 'text': 'review'}, self.path)
        self.assertEqual([m['agentId'] for m in self.mutations], ['b'])

    def test_uncertain_send_is_persisted_and_never_replayed(self):
        self.uncertain = True
        with patch.object(harness, 'request', self.request):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])
            with self.assertRaises(ValueError):
                harness.run('send', {'agentId': 'a', 'text': 'try again'}, self.path)
            status = harness.run('status', {}, self.path)
            self.assertEqual(status['agentId'], 'a')
            self.assertEqual(len(self.mutations), 1)

    def test_receipt_resolves_pending_without_a_second_send(self):
        self.uncertain = True
        with patch.object(harness, 'request', self.request), patch.object(harness, 'api', return_value={'paired': True, 'connected': True, 'machine_id': 'machine'}):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])
            self.uncertain = False
            harness.run('receipt', {}, self.path)
            self.assertNotIn('pending', json.loads(self.path.read_text())['voice'])
            last = harness.run('context', {}, self.path)['tasks'][0]
            self.assertEqual(last['deliveryState'], 'resolved')
            self.assertEqual(last['receiptState'], 'started')
            harness.run('send', {'agentId': 'a', 'text': 'follow up'}, self.path)
            self.assertEqual([m['agentId'] for m in self.mutations], ['a', 'a'])

    def test_internal_error_retains_uncertainty(self):
        base = self.request
        def request(kind, **fields):
            return {'error': {'code': 'INTERNAL'}} if kind == 'turn.send' else base(kind, **fields)
        with patch.object(harness, 'request', request):
            harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])

    def test_send_passes_valid_direct_response_routing(self):
        with patch.object(harness, 'request', self.request):
            harness.run('send', {
                'agentId': 'a',
                'text': 'do work',
                'response': {'run_id': 'device-chat-42', 'channel': 'web'},
            }, self.path)
        self.assertEqual(
            self.mutations[0]['response'],
            {'run_id': 'device-chat-42', 'channel': 'web'},
        )

    def test_confirmed_response_route_cannot_send_the_same_turn_twice(self):
        response = {'run_id': 'device-chat-42', 'channel': 'web'}
        with patch.object(harness, 'request', self.request):
            harness.run('send', {'agentId': 'a', 'text': 'do work', 'response': response}, self.path)
            with self.assertRaisesRegex(ValueError, 'already has a confirmed delivery'):
                harness.run('send', {'agentId': 'a', 'text': 'do work again', 'response': response}, self.path)
            harness.run('send', {
                'agentId': 'a',
                'text': 'new user turn',
                'response': {'run_id': 'device-chat-43', 'channel': 'web'},
            }, self.path)
        self.assertEqual(len(self.mutations), 2)

    def test_receipt_marks_an_uncertain_response_route_as_complete(self):
        self.uncertain = True
        response = {'run_id': 'device-chat-42', 'channel': 'web'}
        with patch.object(harness, 'request', self.request), patch.object(harness, 'api', return_value={'paired': True, 'connected': True, 'machine_id': 'machine'}):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work', 'response': response}, self.path)
            self.uncertain = False
            harness.run('receipt', {}, self.path)
            with self.assertRaisesRegex(ValueError, 'already has a confirmed delivery'):
                harness.run('send', {'agentId': 'a', 'text': 'do work again', 'response': response}, self.path)

    def test_answer_passes_valid_direct_response_routing(self):
        with patch.object(harness, 'request', self.request):
            harness.run('answer', {
                'agentId': 'a',
                'questionRequestId': 'question-42',
                'answers': {'location': 'Hanoi'},
                'response': {'run_id': 'device-chat-43', 'channel': 'voice'},
            }, self.path)
        self.assertEqual(
            self.mutations[0]['response'],
            {'run_id': 'device-chat-43', 'channel': 'voice'},
        )


if __name__ == '__main__':
    unittest.main()
