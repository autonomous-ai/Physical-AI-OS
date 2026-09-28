"""Durable Store orchestration tests. All RPC/model boundaries are mocked."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_harness import harness

FIXTURES = Path(__file__).resolve().parents[3] / 'docs/contracts/autonomous-device-store-v1'


class StoreWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.fixture = json.loads((FIXTURES / 'blender.fixture.json').read_text())
        self.accepted = self.fixture['steps'][2]['response']
        self.ready = self.fixture['steps'][3]['response']
        self.status = dict(paired=True, connected=True, machine_id='mac-example',
                           capabilities=list(harness.STORE_CAPABILITIES), server_instance_id='server-1', fingerprint='peer-1')
        self.params = dict(intent_id='user-turn-1', text=self.fixture['taskRequest']['text'],
                           packageId='autonomous/blender', workspace={'kind': 'new', 'name': 'airplane'},
                           response={'run_id': 'run-1', 'channel': 'web'})
        self.calls = []
        self.fail_kind = None
        self.operation = self.ready
        self.receipt = {'state': 'queued'}
        self.candidate = {'agentId': 'existing', 'machineId': 'mac-example', 'packageId': 'autonomous/blender',
                          'runtime': 'ready', 'workspace': '/tmp/project', 'engine': 'claude', 'state': 'idle'}
        for name, value in [('api', lambda *args, **kwargs: copy.deepcopy(self.status)), ('request', self.rpc)]:
            mock = patch.object(harness, name, side_effect=value)
            mock.start()
            self.addCleanup(mock.stop)

    def rpc(self, kind, **fields):
        self.calls.append((kind, copy.deepcopy(fields)))
        if kind == self.fail_kind:
            raise OSError('lost acknowledgement')
        if kind == 'agent.prepare':
            # Reservation must exist on disk even if the process dies during RPC.
            workflows = json.loads(self.path.read_text())['voice']['workflows']
            self.assertIn(fields['idempotencyKey'], [w['preparationKey'] for w in workflows.values()])
            return copy.deepcopy(self.accepted)
        if kind == 'operation.get':
            return copy.deepcopy(self.operation)
        if kind == 'store.inspect':
            return {'machineId': 'mac-example', 'candidates': [self.candidate]}
        if kind == 'store.list':
            return {'machineId': 'mac-example', 'packages': [], 'nextOffset': None}
        if kind == 'turn.send':
            self.assertEqual(self.record()['task']['state'], 'reserved')
        return {'receipt': self.receipt}

    def run_action(self, action, params=None):
        return harness.run(action, {'intent_id': 'user-turn-1'} if params is None else params, self.path)

    def record(self):
        return json.loads(self.path.read_text())['voice']['workflows']['user-turn-1']

    def test_store_dispatch_keeps_prepared_target_without_selector(self):
        with patch.object(harness, 'api', side_effect=lambda *args, **kwargs: copy.deepcopy(self.status)) as api:
            self.run_action('prepare', self.params)
            self.run_action('operation')
            self.run_action('dispatch')
            self.assertFalse(any(c.args[0] == '/select-agent' for c in api.call_args_list))
        sent = [fields for kind, fields in self.calls if kind == 'turn.send']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['agentId'], self.record()['agentId'])

    def test_house_workflow_namespace_cannot_fall_back_to_airplane_for_followup(self):
        # Simulate a legacy per-run namespace created before the stable-scope guard.
        self.path.write_text(json.dumps({'voice': {'agentId': 'airplane', 'machineId': 'mac-example'},
                                         'device-chat-house': {}}))
        params = {**self.params, 'conversation_id': 'device-chat-house', 'text': 'Create a house',
                  'workspace': {'kind': 'new', 'name': 'house'},
                  'response': {'run_id': 'device-chat-house', 'channel': 'web'}}
        dispatched = []
        def rpc(kind, **fields):
            if kind == 'agent.prepare':
                return copy.deepcopy(self.accepted)
            if kind == 'operation.get':
                ready = copy.deepcopy(self.ready)
                ready['operation']['agentId'] = 'house'
                return ready
            if kind == 'agents.list':
                return {'machineId': 'mac-example', 'agents': [
                    {'agentId': 'airplane', 'name': 'Blender'}, {'agentId': 'house', 'name': 'Blender'}]}
            if kind == 'turn.send':
                dispatched.append(fields)
                return {'receipt': {'state': 'queued'}}
            raise AssertionError(kind)
        with patch.object(harness, 'request', side_effect=rpc):
            self.run_action('prepare', params)
            self.run_action('dispatch', {'conversation_id': 'device-chat-house', 'intent_id': params['intent_id']})
            before = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'EXPLICIT_TARGET_REQUIRED'):
                self.run_action('send', {'text': 'Add trees to the garden'})
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(len(dispatched), 1)
            evidence = self.run_action('context', {})
            house = next(t for t in evidence['tasks'] if t.get('intent_id') == params['intent_id'])
            self.assertEqual(house['agentId'], 'house')
            self.assertEqual(house['text'], 'Create a house')
            self.assertEqual(house['conversation_id'], 'device-chat-house')
            self.run_action('send', {'conversation_id': house['conversation_id'],
                                    'agentId': house['agentId'], 'text': 'Add trees to the garden'})
        self.assertEqual([d['agentId'] for d in dispatched], ['house', 'house'])
        self.assertEqual(json.loads(self.path.read_text())['voice']['agentId'], 'airplane')

    def test_wait_budget_survives_retries_and_preserves_intent(self):
        with patch.object(harness.time, 'time', return_value=1000):
            self.run_action('prepare', self.params)
        saved = self.record()
        self.operation = self.accepted
        with patch.object(harness.time, 'time', return_value=1091):
            for action in ('operation', 'prepare', 'dispatch'):
                result = self.run_action(action)
                self.assertEqual(result['code'], 'PREPARATION_WAIT_EXPIRED')
                self.assertFalse(result['taskDispatched'])
        self.assertEqual([kind for kind, _ in self.calls], ['agent.prepare'])
        self.assertEqual(self.record()['preparationKey'], saved['preparationKey'])
        self.assertEqual(self.record()['taskKey'], saved['taskKey'])
        self.assertNotIn('task', self.record())
        self.operation = self.ready
        with patch.object(harness.time, 'time', return_value=1100):
            result = self.run_action('operation', {'intent_id': 'user-turn-1',
                'response': {'run_id': 'new-user-turn', 'channel': 'web'}})
            self.assertEqual(result['operation']['state'], 'ready')
            self.run_action('dispatch')
        self.assertEqual([kind for kind, _ in self.calls].count('agent.prepare'), 1)
        self.assertEqual([kind for kind, _ in self.calls].count('turn.send'), 1)
        self.assertEqual(self.record()['taskKey'], saved['taskKey'])

    def test_slow_readiness_reply_cannot_dispatch_after_wait_deadline(self):
        with patch.object(harness.time, 'time', return_value=1000):
            self.run_action('prepare', self.params)
        clock = [1089]
        original = self.rpc
        def slow_rpc(kind, **fields):
            result = original(kind, **fields)
            if kind == 'operation.get':
                clock[0] = 1091
            return result
        with patch.object(harness, 'request', side_effect=slow_rpc), patch.object(harness.time, 'time', side_effect=lambda: clock[0]):
            result = self.run_action('dispatch')
        self.assertEqual(result['code'], 'PREPARATION_WAIT_EXPIRED')
        self.assertNotIn('task', self.record())
        self.assertFalse(any(kind == 'turn.send' for kind, _ in self.calls))

    def test_fixture_preparation_then_exact_once_dispatch(self):
        self.run_action('prepare', self.params)
        self.run_action('operation')
        result = self.run_action('dispatch')
        self.assertEqual(result['receipt']['state'], 'queued')
        record = self.record()
        self.assertNotEqual(record['preparationKey'], record['taskKey'])
        sends = [fields for kind, fields in self.calls if kind == 'turn.send']
        self.assertEqual(len(sends), 1)
        for field in ('text', 'agentId', 'machineId'):
            self.assertEqual(sends[0][field], self.fixture['taskRequest'][field])
        self.assertEqual(record['task']['serverInstanceId'], 'server-1')
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            self.run_action('dispatch')
        with self.assertRaisesRegex(ValueError, 'retained workflow'):
            self.run_action('send', {'agentId': 'agent-example', 'text': self.params['text']})

    def test_lost_prepare_ack_retries_same_key_parameters(self):
        self.fail_kind = 'agent.prepare'
        with self.assertRaises(OSError):
            self.run_action('prepare', self.params)
        saved = self.record()
        self.fail_kind = None
        self.run_action('prepare')
        requests = [f for k, f in self.calls if k == 'agent.prepare']
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(saved['taskKey'], self.record()['taskKey'])
        self.run_action('prepare')
        self.assertEqual([k for k, _ in self.calls].count('agent.prepare'), 2)

    def test_parameters_are_immutable(self):
        self.run_action('prepare', self.params)
        for field, value in [('text', 'different'), ('workspace', {'kind': 'new'}), ('packageId', 'other/package')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'IDEMPOTENCY_CONFLICT'):
                self.run_action('prepare', {'intent_id': 'user-turn-1', field: value})

    def test_timeout_and_restart_cannot_resend(self):
        self.run_action('prepare', self.params)
        self.fail_kind = 'turn.send'
        with self.assertRaises(OSError):
            self.run_action('dispatch')
        self.status['server_instance_id'] = 'server-2'
        self.fail_kind = None
        result = self.run_action('workflow-receipt')
        self.assertIn('needs_user_action', result['guidance'])
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            self.run_action('dispatch')
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.run_action('send', {'agentId': 'agent-example', 'text': 'try again'})
        self.assertEqual([k for k, _ in self.calls].count('turn.send'), 1)

    def test_same_instance_receipt_reconciles_without_send(self):
        self.run_action('prepare', self.params)
        self.fail_kind = 'turn.send'
        with self.assertRaises(OSError):
            self.run_action('dispatch')
        self.fail_kind = None
        self.run_action('workflow-receipt')
        self.assertEqual(self.record()['task']['state'], 'confirmed')
        self.assertEqual(len(self.record()['history']), 1)

    def test_missing_receipt_is_never_permission_to_retry(self):
        self.run_action('prepare', self.params)
        self.receipt = None
        self.run_action('dispatch')
        result = self.run_action('workflow-receipt')
        self.assertIn('needs_user_action', result['guidance'])
        self.assertEqual(self.record()['task']['state'], 'reserved')

    def test_all_capabilities_required_offline_preserves_journal(self):
        for capability in harness.STORE_CAPABILITIES:
            self.status['capabilities'] = [c for c in harness.STORE_CAPABILITIES if c != capability]
            with self.assertRaisesRegex(ValueError, 'Update Harness CLI'):
                self.run_action('prepare', self.params)
        self.assertFalse(self.path.exists())
        self.status['capabilities'] = list(harness.STORE_CAPABILITIES)
        self.run_action('prepare', self.params)
        before = self.path.read_bytes()
        self.status['connected'] = False
        with self.assertRaisesRegex(ValueError, 'HARNESS_OFFLINE'):
            self.run_action('operation')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.run_action('workflow-status')['workflow']['text'], self.params['text'])

    def test_needs_action_failed_and_running_do_not_dispatch(self):
        self.run_action('prepare', self.params)
        for state in ('accepted', 'running', 'failed', 'needs_user_action'):
            self.operation = copy.deepcopy(self.ready)
            self.operation['operation'].update(state=state, guidance='Open Desktop', error={'code': 'ENGINE_ACTION_REQUIRED', 'message': 'Login needed'})
            result = self.run_action('dispatch')
            self.assertEqual(result['operation']['guidance'], 'Open Desktop')
            self.assertNotIn('task', self.record())
        self.assertNotIn('turn.send', [k for k, _ in self.calls])

    def test_operation_not_found_preserves_id_and_no_prepare(self):
        self.run_action('prepare', self.params)
        self.operation = {'error': {'code': 'OPERATION_NOT_FOUND', 'message': 'Unknown'}}
        self.run_action('dispatch')
        self.run_action('prepare')
        self.assertEqual([k for k, _ in self.calls].count('agent.prepare'), 1)
        self.assertEqual(self.record()['operationId'], self.accepted['operation']['operationId'])

    def test_explicit_candidate_requires_exact_package_and_runtime(self):
        params = {**self.params, 'agentId': 'existing'}
        del params['workspace']
        self.run_action('prepare', params)
        self.candidate['runtime'] = 'starting'
        with self.assertRaisesRegex(ValueError, 'verified ready'):
            self.run_action('dispatch')
        self.candidate['runtime'] = 'ready'
        self.run_action('dispatch')
        self.assertNotIn('agent.prepare', [k for k, _ in self.calls])
        self.assertEqual(self.record()['task']['agentId'], 'existing')

    def test_current_response_can_change_before_dispatch_original_stays(self):
        self.run_action('prepare', self.params)
        new = {'run_id': 'run-2', 'channel': 'voice'}
        self.run_action('operation', {'intent_id': 'user-turn-1', 'response': new})
        self.run_action('dispatch')
        self.assertEqual(self.record()['originalResponse'], self.params['response'])
        self.assertEqual(self.record()['response'], new)
        self.assertEqual(self.calls[-1][1]['response'], new)

    def test_repair_same_machine_changes_identity_blocks(self):
        self.run_action('prepare', self.params)
        self.status['fingerprint'] = 'new-peer'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.run_action('operation')

    def test_second_intent_cannot_reuse_response_route(self):
        self.run_action('prepare', self.params)
        with self.assertRaisesRegex(ValueError, 'already belongs'):
            self.run_action('prepare', {**self.params, 'intent_id': 'new'})

    def test_operation_mismatched_target_is_not_saved(self):
        self.run_action('prepare', self.params)
        self.operation = copy.deepcopy(self.ready)
        self.operation['operation']['machineId'] = 'different'
        with self.assertRaisesRegex(ValueError, 'target does not match'):
            self.run_action('operation')
        self.assertEqual(self.record()['operation']['state'], 'accepted')

    def test_explicit_abandon_allows_new_work_never_old_dispatch(self):
        self.run_action('prepare', self.params)
        self.fail_kind = 'turn.send'
        with self.assertRaises(OSError):
            self.run_action('dispatch')
        self.status['connected'] = False
        self.run_action('workflow-resolve', {'intent_id': 'user-turn-1', 'resolution': 'do_not_retry'})
        self.assertEqual(self.record()['task']['state'], 'abandoned')
        self.status['connected'] = True
        self.fail_kind = None
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            self.run_action('dispatch')
        self.run_action('prepare', {**self.params, 'intent_id': 'user-turn-2', 'response': {'run_id': 'run-2', 'channel': 'web'}})
        self.assertEqual(len(json.loads(self.path.read_text())['voice']['workflows']), 2)

    def test_poll_cadence_and_rate_limit_backoff_persist(self):
        self.run_action('prepare', self.params)
        self.operation = {'error': {'code': 'RATE_LIMITED', 'message': 'Slow down'}}
        with patch.object(harness.time, 'time', return_value=100):
            result = self.run_action('operation')
            self.assertEqual(result['retry_after_seconds'], 4)
            count = len(self.calls)
            self.run_action('operation')
            self.assertEqual(len(self.calls), count)
        with patch.object(harness.time, 'time', return_value=105):
            result = self.run_action('operation')
            self.assertEqual(result['retry_after_seconds'], 8)

    def test_malformed_ready_cannot_use_stale_agent(self):
        self.run_action('prepare', self.params)
        self.run_action('operation')
        self.operation = copy.deepcopy(self.ready)
        self.operation['operation']['agentId'] = None
        with self.assertRaisesRegex(ValueError, 'Ready operation must'):
            self.run_action('dispatch')
        self.assertNotIn('task', self.record())

    def test_completed_normal_route_cannot_dispatch(self):
        self.run_action('prepare', self.params)
        state = json.loads(self.path.read_text())
        state['voice']['completedResponseRunID'] = 'run-1'
        self.path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'confirmed delivery'):
            self.run_action('dispatch')

    def test_confirmed_receipt_survives_daemon_restart(self):
        self.run_action('prepare', self.params)
        self.run_action('dispatch')
        self.status['server_instance_id'] = 'server-2'
        result = self.run_action('workflow-receipt')
        self.assertEqual(result['receipt']['state'], 'queued')
        self.assertTrue(result['historical'])
        self.assertNotIn('guidance', result)
        self.assertEqual(self.record()['task']['state'], 'confirmed')

    def test_failed_local_reservation_never_sends(self):
        self.run_action('prepare', self.params)
        original_save = harness.save

        def fail_task_save(path, state):
            if state['voice']['workflows']['user-turn-1'].get('task'):
                raise OSError('disk full')
            original_save(path, state)

        with patch.object(harness, 'save', side_effect=fail_task_save):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_action('dispatch')
        self.assertNotIn('turn.send', [k for k, _ in self.calls])
        self.assertNotIn('task', self.record())

    def test_truncated_candidate_uses_recorded_package_from_agents_list(self):
        original_rpc = self.rpc

        def rpc(kind, **fields):
            if kind == 'store.inspect':
                return {'machineId': 'mac-example', 'candidates': [], 'candidatesTruncated': True}
            if kind == 'agents.list':
                candidate = dict(self.candidate)
                candidate.pop('machineId')
                return {'machineId': 'mac-example', 'agents': [candidate]}
            return original_rpc(kind, **fields)

        params = {**self.params, 'agentId': 'existing'}
        del params['workspace']
        with patch.object(harness, 'request', side_effect=rpc):
            self.run_action('prepare', params)
            self.run_action('dispatch')
        self.assertEqual(self.record()['task']['agentId'], 'existing')

    def test_operation_wait_is_bounded_and_polls_every_two_seconds(self):
        self.run_action('prepare', self.params)
        self.operation = copy.deepcopy(self.accepted)
        clock = [100]

        def sleep(seconds):
            self.assertEqual(seconds, 2)
            clock[0] += seconds

        with patch.object(harness.time, 'time', side_effect=lambda: clock[0]), patch.object(harness.time, 'sleep', side_effect=sleep):
            result = self.run_action('operation', {'intent_id': 'user-turn-1', 'wait_seconds': 5})
        self.assertEqual(clock[0], 104)
        self.assertEqual(result['retry_after_seconds'], 2)
        self.assertEqual([k for k, _ in self.calls].count('operation.get'), 3)

    def test_operation_after_dispatch_does_not_emit_preparation_progress(self):
        self.run_action('prepare', self.params)
        self.run_action('dispatch')
        count = len(self.calls)
        result = self.run_action('operation')
        self.assertIn('task', result['workflow'])
        self.assertEqual(len(self.calls), count)


class StoreAPIErrorTests(unittest.TestCase):
    def test_http_error_preserves_os_recovery_guidance(self):
        import io
        from urllib.error import HTTPError

        message = 'Harness preparation is unknown; retry the same preparation key and parameters or poll operation.get'
        error = HTTPError(harness.BASE + '/request', 502, 'Bad Gateway', {},
                          io.BytesIO(json.dumps({'status': 0, 'data': None, 'message': message}).encode()))
        with patch.object(harness.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(ValueError, 'same preparation key and parameters'):
                harness.api('/request', {'type': 'agent.prepare'})
        self.assertTrue(error.closed)
