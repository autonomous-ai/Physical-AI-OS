#!/usr/bin/env python3
"""Device-local Harness client with durable voice selection and uncertain-send protection."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

BASE = 'http://127.0.0.1:5000/api/harness'
MAX_BYTES = 3 * 1024 * 1024
KNOWN_RECEIPT_STATES = ('queued', 'delivered', 'started', 'completed', 'rejected')
AGENT_RECAP_MAX_CHARS = 1000


def api(path, payload=None, timeout=40):
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    req = urllib.request.Request(BASE + path, data=raw, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        # OS includes actionable preparation/delivery guidance in error envelopes.
        response = error
    with response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Harness response exceeds limit')
    envelope = json.loads(data)
    if envelope.get('status') != 1:
        raise ValueError(str(envelope.get('message') or 'Harness request failed'))
    return envelope.get('data')


def request(kind, **fields):
    return api('/request', {'type': kind, 'requestId': str(uuid.uuid4()), **fields})


def require_connection():
    """Distinguish missing pairing from an offline paired computer before dispatch."""
    status = api('/status')
    if status.get('paired') is not True:
        raise ValueError('HARNESS_UNPAIRED: This device has no Harness pairing. This connection check attempted no remote operation; it says nothing about earlier deliveries. Apply the skill fallback policy for fresh undispatched tasks. If the task requires Harness, pair in Harness Desktop Settings > Devices using the OS Monitor code.')
    if status.get('connected') is not True:
        raise ValueError('HARNESS_OFFLINE: Harness is disconnected. This connection check attempted no remote operation; it says nothing about earlier deliveries. Apply the skill fallback policy for fresh undispatched tasks. If the task requires Harness, open Harness on the paired computer and check the local network. Pairing is saved; do not pair again.')
    return status


def save(path, value):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.harness-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def bounded_agents(listing):
    """Keep each agent's optional recap headline a bounded one-line string; drop anything else."""
    agents = listing.get('agents')
    if not isinstance(agents, list):
        return listing
    for agent in agents:
        if not isinstance(agent, dict) or 'recap' not in agent:
            continue
        recap = agent.pop('recap')
        recap = ' '.join(recap.split()) if isinstance(recap, str) else ''
        if recap:
            agent['recap'] = recap[:AGENT_RECAP_MAX_CHARS]
    return listing


STORE_CAPABILITIES = ('store.list', 'store.inspect', 'agent.prepare', 'operation.get')
PREPARATION_WAIT_SECONDS = 90
STORE_ACTIONS = ('store-list', 'store-inspect', 'prepare', 'operation', 'dispatch', 'workflow-receipt', 'workflow-status', 'workflow-resolve')


def validate_response(response):
    if response is None:
        return
    if not isinstance(response, dict) or set(response) != {'run_id', 'channel'}:
        raise ValueError('response must contain only run_id and channel')
    if not isinstance(response['run_id'], str) or not 1 <= len(response['run_id']) <= 128:
        raise ValueError('response run_id is invalid')
    if response['channel'] not in ('voice', 'web'):
        raise ValueError('response channel must be voice or web')


def store_workflow(action, params, context, persist):
    """Journal before each side effect; a reserved task is never automatically replayed."""
    if action == 'workflow-status':
        workflows = context.get('workflows', {})
        return {'workflow': workflows.get(params['intent_id'])} if params.get('intent_id') else {'workflows': workflows}
    if action == 'workflow-resolve':
        record = context.get('workflows', {}).get(params.get('intent_id'))
        if not record or not record.get('task') or params.get('resolution') != 'do_not_retry':
            raise ValueError('Existing task and explicit do_not_retry resolution required')
        record['task']['state'] = 'abandoned'
        record['history'].append({'resolution': 'do_not_retry', 'resent': False})
        persist()
        return {'resolved': True, 'resent': False, 'cancelled': False}
    status = require_connection()
    if not all(cap in status.get('capabilities', []) for cap in STORE_CAPABILITIES):
        raise ValueError('UNSUPPORTED_CAPABILITY: Update Harness CLI on the paired computer; Store requires store.list, store.inspect, agent.prepare, operation.get')
    machine = status.get('machine_id')
    if not machine:
        raise ValueError('Missing paired machine identity')
    if action in ('store-list', 'store-inspect'):
        fields = ('query', 'offset', 'limit') if action == 'store-list' else ('packageId',)
        return request(action.replace('-', '.'), **{k: params[k] for k in fields if k in params})
    intent_id = params.get('intent_id')
    if not isinstance(intent_id, str) or not 1 <= len(intent_id) <= 128:
        raise ValueError('A stable intent_id of 1 to 128 characters is required')
    workflows = context.setdefault('workflows', {})
    record = workflows.get(intent_id)
    if record is None:
        if action != 'prepare':
            raise ValueError('Unknown intent_id; prepare and persist the original intent first')
        text = params.get('text')
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384:
            raise ValueError('text must contain 1 to 16384 UTF-8 bytes')
        if not isinstance(params.get('packageId'), str) or not params['packageId']:
            raise ValueError('packageId required')
        response = params.get('response')
        validate_response(response)
        if response and (context.get('completedResponseRunID') == response['run_id'] or any(
                (w.get('response') or {}).get('run_id') == response['run_id'] for w in workflows.values())):
            raise ValueError('Response route already belongs to an intent; resume that intent_id')
        if context.get('pending'):
            raise ValueError('Previous delivery unresolved; reconcile it first')
        if len(workflows) >= 256:
            raise ValueError('Workflow journal limit reached; archive resolved history explicitly')
        if params.get('agentId') and 'workspace' in params:
            raise ValueError('Choose an existing agent or prepare a workspace, not both')
        if not params.get('agentId') and not isinstance(params.get('workspace'), dict):
            raise ValueError('workspace required for preparation')
        record = {'intent_id': intent_id, 'text': text, 'packageId': params['packageId'],
                  'machineId': machine, 'preparationKey': str(uuid.uuid4()),
                  'taskKey': str(uuid.uuid4()), 'history': [],
                  'peerFingerprint': status.get('fingerprint')}
        if response:
            record['response'] = response
        if params.get('agentId'):
            record['selectedAgentId'] = params['agentId']
        else:
            record['workspace'] = params['workspace']
        workflows[intent_id] = record
        persist()
    if record['machineId'] != machine:
        raise ValueError('Pairing changed; retained intent belongs to the previous computer')
    if record.get('peerFingerprint') and status.get('fingerprint') != record['peerFingerprint']:
        raise ValueError('Paired identity changed; reconcile retained intent')
    for key in ('text', 'packageId', 'workspace'):
        if key in params and params[key] != record.get(key):
            raise ValueError('IDEMPOTENCY_CONFLICT: Intent parameters are immutable; reconcile the existing intent')
    if 'agentId' in params and params['agentId'] != record.get('selectedAgentId'):
        raise ValueError('IDEMPOTENCY_CONFLICT: Existing candidate cannot change')

    if 'response' in params:
        validate_response(params['response'])
        if record.get('task') and params['response'] != record.get('response'):
            raise ValueError('Dispatched response route is immutable')
        if params['response'] and any(w is not record and (w.get('response') or {}).get('run_id') == params['response']['run_id'] for w in workflows.values()):
            raise ValueError('Response route already belongs to another intent')
        record.setdefault('originalResponse', record.get('response'))
        record['response'] = params['response']
        persist()
    route = {'response': record['response']} if record.get('response') else {}

    # A poll invocation is bounded already; also bound repeated invocations in
    # the same user turn. Persist the deadline so subprocess/reconnect cannot
    # renew it. Only a new response run may resume the same saved intent.
    def wait_expired():
        if record.get('task') or record.get('selectedAgentId'):
            return None
        budget = record.get('preparationWait')
        run_id = (record.get('response') or {}).get('run_id') or (budget or {}).get('run_id', intent_id)
        if not budget or budget.get('run_id') != run_id:
            budget = {'run_id': run_id, 'deadline': time.time() + PREPARATION_WAIT_SECONDS}
            record['preparationWait'] = budget
            persist()
        if not budget.get('expired') and time.time() < budget['deadline']:
            return None
        budget['expired'] = True
        persist()
        return {'wait_status': 'expired', 'code': 'PREPARATION_WAIT_EXPIRED',
                'intent_id': intent_id, 'operationId': record.get('operationId'),
                'taskDispatched': False, 'resume_required': True,
                'guidance': 'Stop polling and end this turn with an honest explanation: Harness preparation has not become ready within the wait budget; the task has not been sent. The preparation may continue remotely. Keep this intent and keys. Resume only on a new user turn with its response run_id; do not create another agent or send the task now.'}

    if action in ('prepare', 'operation', 'dispatch'):
        expired = wait_expired()
        if expired:
            return expired

    def remember(result):
        operation = result.get('operation')
        if operation:
            if (not isinstance(operation, dict) or not isinstance(operation.get('operationId'), str)
                    or not operation['operationId']
                    or operation.get('state') not in ('accepted', 'running', 'ready', 'failed', 'needs_user_action')
                    or operation.get('taskDispatched') is not False):
                raise ValueError('Malformed preparation operation; reconcile retained intent')
            if operation.get('machineId') != machine or operation.get('packageId') != record['packageId']:
                raise ValueError('Operation target does not match retained intent')
            if record.get('operationId') and record['operationId'] != operation.get('operationId'):
                raise ValueError('Operation identity changed; inspect the journal')
            record['operationId'] = operation['operationId']
            record['operation'] = operation
            if operation.get('agentId'):
                record['agentId'] = operation['agentId']
        record['lastResult'] = result
        persist()
        return result

    def inspect_candidate():
        result = request('store.inspect', packageId=record['packageId'])
        if result.get('error'):
            return remember(result)
        pool = result.get('candidates', [])
        if result.get('candidatesTruncated') and not any(a.get('agentId') == record['selectedAgentId'] for a in pool):
            listing = request('agents.list')
            if listing.get('error'):
                return remember(listing)
            if listing.get('machineId') != machine:
                raise ValueError('Agent listing machine mismatch')
            pool = [{**a, 'machineId': machine} for a in listing.get('agents', [])]
        candidates = [a for a in pool
                      if a.get('agentId') == record['selectedAgentId']
                      and a.get('machineId') == machine and a.get('packageId') == record['packageId']
                      and a.get('runtime') == 'ready']
        if result.get('machineId') != machine or len(candidates) != 1:
            raise ValueError('Selected agent is not a verified ready package candidate; inspect its project/runtime')
        record['agentId'] = candidates[0]['agentId']
        record['candidate'] = candidates[0]
        persist()
        return {'candidate': candidates[0], 'taskDispatched': False}

    if action == 'prepare':
        if record.get('task'):
            return {'workflow': record, 'resent': False}
        if record.get('selectedAgentId'):
            return inspect_candidate()
        if record.get('operationId'):
            return remember(request('operation.get', operationId=record['operationId'], **route))
        return remember(request('agent.prepare', machineId=machine, packageId=record['packageId'],
                                workspace=record['workspace'], idempotencyKey=record['preparationKey'], **route))
    if action == 'operation':
        if record.get('task'):
            return {'workflow': record, 'resent': False}
        wait_seconds = params.get('wait_seconds', 0)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 20:
            raise ValueError('wait_seconds must be between 0 and 20')
        if record.get('selectedAgentId'):
            return inspect_candidate()
        if not record.get('operationId'):
            raise ValueError('Preparation acknowledgement missing; resume prepare with the same intent_id')
        deadline = time.time() + wait_seconds
        while True:
            expired = wait_expired()
            if expired:
                return expired
            delay = max(0, record.get('nextPollAt', 0) - time.time())
            if delay > 0:
                if time.time() + delay > deadline:
                    return {'workflow': record, 'retry_after_seconds': round(delay, 2)}
                time.sleep(delay)
            expired = wait_expired()
            if expired:
                return expired
            result = remember(request('operation.get', operationId=record['operationId'], **route))
            expired = wait_expired()
            if expired:
                return expired
            limited = result.get('error', {}).get('code') in ('RATE_LIMITED', 'BACKPRESSURE')
            record['pollBackoff'] = min(20, max(4, record.get('pollBackoff', 2) * 2)) if limited else 2
            record['nextPollAt'] = time.time() + record['pollBackoff']
            persist()
            if result.get('operation', {}).get('state') not in ('accepted', 'running') and not limited:
                return result
            if record['nextPollAt'] > deadline:
                return {**result, 'retry_after_seconds': record['pollBackoff']}
    if action == 'workflow-receipt':
        task = record.get('task')
        if not task:
            return {'receipt': None, 'taskDispatched': False}
        current_instance = status.get('server_instance_id')
        if not task.get('serverInstanceId') or current_instance != task['serverInstanceId']:
            if task.get('receipt', {}).get('state') in KNOWN_RECEIPT_STATES:
                return {'receipt': task['receipt'], 'historical': True, 'serverInstanceId': task.get('serverInstanceId'),
                        'currentServerInstanceId': current_instance, 'resent': False}
            record['reconciliation'] = 'needs_user_action: Harness daemon identity changed or is unknown; inspect agent history, never resend automatically'
            persist()
            return {'receipt': task.get('receipt'), 'guidance': record['reconciliation'], 'resent': False}
        result = request('receipt.get', idempotencyKey=record['taskKey'])
        record['history'].append({'serverInstanceId': current_instance, 'result': result})
        receipt = result.get('receipt')
        if receipt and receipt.get('state') in KNOWN_RECEIPT_STATES:
            task['receipt'] = receipt
            task['state'] = 'confirmed'
            record.pop('reconciliation', None)
            if record.get('response'):
                context['completedResponseRunID'] = record['response']['run_id']
        elif task.get('receipt', {}).get('state') in KNOWN_RECEIPT_STATES:
            persist()
            return {'receipt': task['receipt'], 'historical': True, 'serverInstanceId': task.get('serverInstanceId'), 'resent': False}
        else:
            record['reconciliation'] = 'needs_user_action: Delivery receipt unavailable; inspect agent history, never resend automatically'
        persist()
        return {**result, 'guidance': record.get('reconciliation'), 'resent': False}
    if action == 'dispatch':
        if record.get('task'):
            raise ValueError('Task already reserved; inspect workflow-receipt, never resend automatically')
        if record.get('response') and context.get('completedResponseRunID') == record['response']['run_id']:
            raise ValueError('Response route already has a confirmed delivery; return NO_REPLY')
        if context.get('pending'):
            raise ValueError('Previous delivery unresolved; reconcile it first')
        if any(w.get('task', {}).get('state') == 'reserved' for w in workflows.values()):
            raise ValueError('Previous workflow delivery unresolved; reconcile it first')
        if record.get('selectedAgentId'):
            if inspect_candidate().get('error'):
                return record['lastResult']
        else:
            if not record.get('operationId'):
                raise ValueError('Preparation has no acknowledged operation; resume prepare')
            result = remember(request('operation.get', operationId=record['operationId'], **route))
            if result.get('error') or result.get('operation', {}).get('state') != 'ready':
                return result
            operation = result['operation']
            if not isinstance(operation.get('agentId'), str) or not operation['agentId'] or operation.get('taskDispatched') is not False:
                raise ValueError('Ready operation must name an agent and confirm taskDispatched false')
        expired = wait_expired()
        if expired:
            return expired
        # Refresh identity immediately before reserving; preparation may span reconnects.
        current = require_connection()
        if (current.get('machine_id') != machine or not current.get('server_instance_id')
                or not all(cap in current.get('capabilities', []) for cap in STORE_CAPABILITIES)
                or record.get('peerFingerprint') and current.get('fingerprint') != record['peerFingerprint']):
            raise ValueError('Connected machine/server identity unavailable; do not dispatch')
        expired = wait_expired()
        if expired:
            return expired
        target = {'machineId': machine, 'agentId': record['agentId']}
        record['task'] = {'state': 'reserved', 'serverInstanceId': current['server_instance_id'], **target}
        context.update(target)
        persist()
        payload = {'text': record['text']}
        if record.get('response'):
            payload['response'] = record['response']
        result = request('turn.send', **target, idempotencyKey=record['taskKey'], **payload)
        record['history'].append({'serverInstanceId': current['server_instance_id'], 'result': result})
        receipt = result.get('receipt')
        if receipt and receipt.get('state') in KNOWN_RECEIPT_STATES:
            record['task'].update(state='confirmed', receipt=receipt)
            if record.get('response'):
                context['completedResponseRunID'] = record['response']['run_id']
        persist()
        return result
    raise ValueError('Unknown workflow action')


def retained_context(state, params):
    """Expose task evidence across namespaces without choosing or changing a target."""
    offset, limit = params.get('offset', 0), params.get('limit', 20)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('context offset must be nonnegative and limit must be 1 to 20')
    contexts, tasks = [], []
    for conversation, context in sorted(state.items()):
        if 'conversation_id' in params and params['conversation_id'] != conversation:
            continue
        contexts.append({'conversation_id': conversation,
                         **{k: context[k] for k in ('agentId', 'machineId') if k in context}})
        last = context.get('lastTask')
        if last and not params.get('intent_id'):
            tasks.append({'conversation_id': conversation, 'kind': 'last_request', **last})
        for intent_id, record in sorted(context.get('workflows', {}).items()):
            if params.get('intent_id') and params['intent_id'] != intent_id:
                continue
            task = record.get('task', {})
            operation = record.get('lastResult', {}).get('operation', {})
            tasks.append({'conversation_id': conversation, 'kind': 'workflow',
                          **{k: record[k] for k in ('intent_id', 'text', 'agentId', 'machineId',
                                                   'workspace', 'packageId') if k in record},
                          'operationState': operation.get('state'),
                          'deliveryState': task.get('state'),
                          'receiptState': task.get('receipt', {}).get('state')})
    page = tasks[offset:offset + limit]
    next_offset = offset + len(page)
    return {'contexts': contexts, 'tasks': page, 'totalTasks': len(tasks),
            'truncated': offset > 0 or next_offset < len(tasks),
            'nextOffset': next_offset if next_offset < len(tasks) else None,
            'guidance': 'Historical task evidence only, not automatic target selection. Match the user task and verify the current agent/workspace with list and recap. Mutation requires an explicit target.'}


def run(action, params, path=None):
    if not isinstance(params, dict):
        raise ValueError('Parameters must be an object')
    path = Path(path or os.environ.get('HARNESS_VOICE_STATE', str(Path.home() / '.local/state/autonomous/harness-voice.json')))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conversation = params.get('conversation_id', 'voice')
    if not isinstance(conversation, str) or not 1 <= len(conversation) <= 128:
        raise ValueError('Invalid conversation_id')
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        if action == 'context':
            return retained_context(state, params)
        response = params.get('response')
        if (conversation not in state and isinstance(response, dict)
                and conversation == response.get('run_id')):
            raise ValueError('UNSTABLE_CONVERSATION_ID: response.run_id identifies one turn, not a conversation. Omit conversation_id for voice or use a stable conversation scope. Existing legacy workflows must be resumed in their retained namespace.')
        if conversation not in state and len(state) >= 64:
            raise ValueError('Conversation state limit reached; reuse the stable channel conversation ID')
        context = state.setdefault(conversation, {})
        if action in STORE_ACTIONS:
            return store_workflow(action, params, context, lambda: save(path, state))
        if action in ('send', 'answer'):
            for workflow in context.get('workflows', {}).values():
                if workflow.get('task', {}).get('state') == 'reserved':
                    raise ValueError('Workflow delivery unresolved; use workflow-receipt, never resend automatically')
                if action == 'send' and (params.get('intent_id') == workflow['intent_id'] or
                        (params.get('response') and params['response'].get('run_id') == (workflow.get('response') or {}).get('run_id')) or
                        params.get('text') == workflow['text']):
                    raise ValueError('This task belongs to a retained workflow; use dispatch with its intent_id')
        if action == 'resolve':
            if params.get('resolution') != 'do_not_retry':
                raise ValueError('Explicit do_not_retry resolution required')
            pending = context.pop('pending', None)
            if pending and pending.get('operation') == 'turn.send' and context.get('lastTask'):
                context['lastTask']['deliveryState'] = 'do_not_retry'
            save(path, state)
            return {'resolved': True, 'resent': False}
        if action == 'receipt':
            pending = context.get('pending')
            if not pending:
                return {'receipt': None, 'pending': False}
            status = require_connection()
            if status.get('machine_id') != pending['machineId']:
                raise ValueError('Pairing changed; unresolved request belongs to the previous computer')
            result = request('receipt.get', idempotencyKey=pending['idempotencyKey'])
            receipt = result.get('receipt')
            if receipt and receipt.get('state') in KNOWN_RECEIPT_STATES:
                if pending.get('responseRunID'):
                    context['completedResponseRunID'] = pending['responseRunID']
                if pending.get('operation') == 'turn.send' and context.get('lastTask'):
                    context['lastTask'].update(deliveryState='resolved', receiptState=receipt['state'])
                context.pop('pending', None)
                save(path, state)
            return result
        explicit = params.get('agentId') or params.get('agent')
        if action in ('send', 'answer', 'stop') and not explicit:
            raise ValueError('EXPLICIT_TARGET_REQUIRED: Inspect context/list and the matching task workspace, then supply agentId or a unique agent name. Never use the retained default or resend a previous request as a fallback.')
        initial_connection = require_connection()
        listing = request('agents.list')
        if listing.get('error'):
            return listing
        if action == 'list':
            return bounded_agents(listing)
        agents = listing.get('agents', [])
        machine = listing.get('machineId')
        selected = explicit or context.get('agentId')
        if not explicit and context.get('machineId') != machine:
            raise ValueError('Select an agent on the paired computer first')
        matches = [a for a in agents if a.get('agentId') == selected or (explicit and a.get('name') == selected)]
        if len(matches) != 1:
            raise ValueError('Select exactly one agent from list; the retained target is never replaced automatically')
        target = {'machineId': machine, 'agentId': matches[0]['agentId']}
        if action == 'select':
            context.update(target)
            save(path, state)
            return {'selected': target}
        if action in ('status', 'recap'):
            # Default to the newest turn only: its recap/text pair is what selection and progress checks need.
            return request(action, **target, **({'n': params.get('n', 1)} if action == 'recap' else {}))
        if context.get('pending'):
            raise ValueError('A previous delivery is unresolved. Inspect receipt/status; do not resend automatically')
        response = params.get('response')
        if response is not None:
            if not isinstance(response, dict) or set(response) != {'run_id', 'channel'}:
                raise ValueError('response must contain only run_id and channel')
            if not isinstance(response['run_id'], str) or not response['run_id'] or len(response['run_id']) > 128:
                raise ValueError('response run_id is invalid')
            if response['channel'] not in ('voice', 'web'):
                raise ValueError('response channel must be voice or web')
            if context.get('completedResponseRunID') == response['run_id']:
                raise ValueError('This response route already has a confirmed delivery; return NO_REPLY')
        if action == 'send':
            text = params.get('text')
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384:
                raise ValueError('text must contain 1 to 16384 UTF-8 bytes')
            kind, payload = 'turn.send', {'text': text}
            if response is not None:
                payload['response'] = response
        elif action == 'stop':
            kind, payload = 'turn.stop', {}
        elif action == 'answer':
            if not isinstance(params.get('questionRequestId'), str) or not isinstance(params.get('answers'), dict):
                raise ValueError('questionRequestId and answers required')
            kind, payload = 'question.answer', {'questionRequestId': params['questionRequestId'], 'answers': params['answers']}
            if response is not None:
                payload['response'] = response
        else:
            raise ValueError('Unknown action')
        if action == 'send':
            # Resolve once before reserving delivery. Uncertain sends retain this exact target.
            try:
                decision = api('/select-agent', {**target, 'text': text}, timeout=4)
            except (OSError, ValueError, TypeError):
                decision = None
            if (isinstance(decision, dict) and decision.get('mode') == 'jev'
                    and decision.get('machineId') == machine
                    and isinstance(decision.get('agentId'), str)
                    and sum(a.get('agentId') == decision['agentId'] for a in agents) == 1):
                target = {'machineId': machine, 'agentId': decision['agentId']}
            current_connection = require_connection()
            if (current_connection.get('machine_id') != machine
                    or initial_connection.get('machine_id') != machine
                    or current_connection.get('server_instance_id') != initial_connection.get('server_instance_id')):
                raise ValueError('Harness connection identity changed during selection; inspect status before sending')
        key = str(uuid.uuid4())
        context.update(target)
        context['pending'] = {
            **target,
            'idempotencyKey': key,
            'operation': kind,
            **({'responseRunID': response['run_id']} if response is not None else {}),
        }
        if action == 'send':
            context['lastTask'] = {**target, 'text': text, 'recordedAt': time.time(), 'deliveryState': 'reserved'}
        save(path, state)
        result = request(kind, **target, idempotencyKey=key, **payload)
        receipt = result.get('receipt')
        if receipt and receipt.get('state') in KNOWN_RECEIPT_STATES:
            if response is not None:
                context['completedResponseRunID'] = response['run_id']
            context.pop('pending', None)
        elif result.get('error', {}).get('code') in {
            'INVALID_REQUEST', 'UNSUPPORTED_CAPABILITY', 'MISSING_TARGET', 'MACHINE_MISMATCH',
            'AGENT_NOT_FOUND', 'QUESTION_STALE', 'PAYLOAD_TOO_LARGE', 'RATE_LIMITED', 'BACKPRESSURE',
        }:
            # Only explicit pre-dispatch refusals resolve uncertainty. INTERNAL/REVOKED may be late.
            context.pop('pending', None)
        if action == 'send':
            context['lastTask']['deliveryState'] = 'reserved' if context.get('pending') else 'resolved'
            context['lastTask']['receiptState'] = (receipt or {}).get('state')
        save(path, state)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['context', 'list', 'select', 'send', 'stop', 'status', 'recap', 'answer', 'receipt', 'resolve', *STORE_ACTIONS])
    parser.add_argument('params', nargs='?', default='{}')
    args = parser.parse_args()
    try:
        result = run(args.action, json.loads(sys.stdin.read() if args.params == '-' else args.params))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, urllib.error.URLError) as error:
        note = 'If a mutation was reserved, delivery remains unresolved. Inspect receipt before sending again.'
        if args.action in ('prepare', 'operation'):
            note = 'Inspect workflow-status. Resume the SAME intent_id/parameters or poll its retained operation; do not create a new preparation key.'
        elif args.action in ('dispatch', 'workflow-receipt'):
            note = 'Inspect workflow-receipt for this intent_id. Never resend a reserved task automatically, including after daemon restart.'
        print(json.dumps({'error': str(error), 'note': note}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
