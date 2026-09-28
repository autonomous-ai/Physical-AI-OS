"""Run with an isolated environment containing jsonschema==4.26.0.

python -m unittest discover -s skills/harness-use/tests -p contract_validation.py
"""
import copy
import json
import unittest
import uuid

import jsonschema
import test_store_workflow as workflow_tests

FIXTURES = workflow_tests.FIXTURES


class ContractValidationTests(unittest.TestCase):
    setUp = workflow_tests.StoreWorkflowTests.setUp
    rpc = workflow_tests.StoreWorkflowTests.rpc
    run_action = workflow_tests.StoreWorkflowTests.run_action
    record = workflow_tests.StoreWorkflowTests.record

    def test_authoritative_fixture_and_emitted_store_frames_match_schemas(self):
        request_schema = json.loads((FIXTURES / 'request.schema.json').read_text())
        response_schema = json.loads((FIXTURES / 'response.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(request_schema)
        jsonschema.Draft202012Validator.check_schema(response_schema)
        for step in self.fixture['steps']:
            jsonschema.validate(step['request'], request_schema)
            if 'response' in step:
                jsonschema.validate(step['response'], response_schema)
        self.run_action('store-list', {'query': 'Blender', 'limit': 5})
        self.run_action('store-inspect', {'packageId': 'autonomous/blender'})
        self.run_action('prepare', self.params)
        self.run_action('operation')
        for kind, fields in self.calls:
            if kind not in ('store.list', 'store.inspect', 'agent.prepare', 'operation.get'):
                continue
            wire = copy.deepcopy(fields)
            wire.pop('response', None)  # Local routing metadata is stripped by OS.
            jsonschema.validate({'type': kind, 'requestId': str(uuid.uuid4()), **wire}, request_schema)
        malformed = copy.deepcopy(self.fixture['steps'][2]['request'])
        malformed['text'] = 'must never send task during preparation'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(malformed, request_schema)
