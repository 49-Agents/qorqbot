from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.overseer import executor
from operational_support import AGENT2, BUSINESS, OperationalHost


def test_task_gateway_reads_real_state_and_visible_outcome(tmp_path, monkeypatch):
    host = OperationalHost(tmp_path)
    runtime = SimpleNamespace(database=host.database, application=host.app)
    ctx = host.agent(AGENT2)
    monkeypatch.setattr(executor, 'current_overseer', lambda runtime, context, uow, capability=None: (context, None))
    task = host.create_task('Synthetic development')
    reference = {'provider': 'operations', 'kind': 'task', 'id': task['id'], 'revision': str(task['revision']),
                 'namespace': {'kind': 'business', 'business_id': BUSINESS}}
    gateway = executor.ExternalTaskGateway(runtime)
    with pytest.raises(DomainError):
        gateway.resolve_task(reference)
    with executor.acting_as(ctx):
        first = gateway.resolve_task(reference)
        assert first['state'] == 'open' and first['result_refs'] == [] and first['completion_summary'] is None
        host.data('tasks_complete', {'task_id': task['id'], 'status': 'done', 'note': 'Implemented and checked.',
                                    'summary': 'Change checked', 'note_refs': [{'type': 'dev_ticket', 'id': 'T-1'}]},
                  host.agent(), expected_revision=task['revision'])
        completed = gateway.resolve_task(reference)
        assert completed['state'] == 'done' and completed['completion_summary'] == 'Change checked'
        assert completed['result_refs'][0]['kind'] == 'task_message'
        assert completed['reference']['revision'] != reference['revision']


def test_satisfaction_gateway_requires_exact_final_passed_receipt(tmp_path, monkeypatch):
    import hashlib
    import json
    from uuid import uuid4
    host = OperationalHost(tmp_path)
    ctx = host.agent(AGENT2)
    reference = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': str(uuid4()),
                 'check_run_id': str(uuid4()), 'check_id': 'catalog'}
    record = {'schema_version': 1, 'application_id': reference['application_id'], 'check_run_id': reference['check_run_id'],
              'phase': 'post_merge', 'plan_revision': 1, 'checked_commit': 'a' * 40, 'checked_tree': 'b' * 40,
              'run_state': 'passed', 'receipt': {'check_id': 'catalog', 'kind': 'catalog', 'outcome': 'passed',
                'started_at': '2026-10-27T00:00:00Z', 'finished_at': '2026-10-27T00:01:00Z', 'exit_code': None,
                'tool_receipt_ref': None, 'output_ref': None, 'diagnostic': None}}
    def result(ref):
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        return {'reference': ref, 'record': record, 'content_hash': digest}
    component = SimpleNamespace(get=lambda identity: {'delta_id': 'synthetic'}, get_check_receipt=result)
    layer = SimpleNamespace(overseer=SimpleNamespace(applications=component, deltas=SimpleNamespace(get=lambda identity: {})))
    runtime = SimpleNamespace(database=host.database, agentic_layer=layer, overseer=SimpleNamespace(_check_delta=lambda context, delta: delta))
    monkeypatch.setattr(executor, 'current_overseer', lambda runtime, context, uow, capability=None: (context, None))
    gate = executor.SatisfactionGateway(runtime)
    with executor.acting_as(ctx):
        valid = gate.resolve_satisfaction_reference(reference, kind='check_receipt')
        assert valid['outcome'] == 'passed' and valid['checked_commit'] == 'a' * 40
        with pytest.raises(DomainError):
            gate.resolve_satisfaction_reference({**reference, 'outcome': 'passed'}, kind='check_receipt')
        record['receipt']['outcome'] = 'uncertain'
        with pytest.raises(DomainError):
            gate.resolve_satisfaction_reference(reference, kind='check_receipt')
