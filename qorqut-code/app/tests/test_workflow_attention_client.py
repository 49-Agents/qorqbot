"""Offline workflow outbox checks with a synthetic engine and MCP peer."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qorqut_agentic.errors import AgenticError
from bazarlyk.agentic import client


def test_start_is_not_duplicated_after_host_sync_failure(tmp_path, monkeypatch):
    root = tmp_path / 'native'
    folder = root / '.bazarlyk'
    folder.mkdir(parents=True)
    registration_id, agent_id, session_id = map(str, (uuid4(), uuid4(), uuid4()))
    registration = {'schema_version': 1, 'registration_id': registration_id, 'agent_id': agent_id,
                    'workspace': str(root), 'service_url': 'http://synthetic.invalid', 'credential_file': '.bazarlyk/credential'}
    path = folder / 'registration.json'
    path.write_text(json.dumps(registration))
    session = folder / 'session-binding.json'
    session.write_text(json.dumps({'registration_id': registration_id, 'session_id': session_id, 'generation': 4}))
    session.chmod(0o600)
    actor = {'actor_id': agent_id, 'actor_kind': 'agent'}
    access = {'actor': actor, 'instance_id': 'inst_' + '0' * 32, 'registration_id': registration_id,
              'selection': {'package_ids': ['sample']}}
    run = {'run_id': 'run-1', 'actor': actor}
    layer = SimpleNamespace(workflows=SimpleNamespace(get=lambda identity: {'run': run}))
    calls = []
    def dispatch(_layer, request):
        calls.append(request)
        return {'run': run, 'operation_id': request['arguments']['operation_id']}
    monkeypatch.setattr(client, 'dispatch', dispatch)
    native = client.NativeClient(path)
    failures = [True]
    def rpc(_method, params):
        if params['name'] == 'attention_workflow_sync' and failures.pop(0):
            raise RuntimeError('synthetic failure')
        payload = {'instance_id': access['instance_id'], 'operation': 'workflows.start', 'operation_id': 'start-1'}
        return {'structuredContent': {'status': 'ok', 'data': {'version': 1, 'payload': payload, 'signature': 'synthetic'}}}
    native._call = rpc
    args = {'ref': 'sample/flow', 'operation_id': 'start-1', 'actor': actor, 'selection': access['selection']}
    user_args = {'ref': 'sample/flow', 'operation_id': 'start-1'}
    with pytest.raises(AgenticError) as error:
        native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)
    assert error.value.code == 'attention_sync_pending' and error.value.details['run_id'] == 'run-1'
    failures.append(False)
    access['selection'] = {'package_ids': ['sample', 'unrelated']}
    args['selection'] = access['selection']
    result = native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)
    assert result['run']['run_id'] == 'run-1' and len(calls) == 1
    assert native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)['run']['run_id'] == 'run-1'
    with pytest.raises(AgenticError) as conflict:
        native._attention_mutation(layer, 'workflows.start', {**args, 'ref': 'sample/other'}, {}, {}, access,
                                   user_arguments={**user_args, 'ref': 'sample/other'})
    assert conflict.value.code == 'operation_conflict'
    with pytest.raises(AgenticError):
        native._attention_mutation(layer, 'workflows.start', {**args, 'operation_id': None}, {}, {}, access)
    assert len(calls) == 1


def test_interrupted_start_replays_the_same_engine_operation(tmp_path, monkeypatch):
    root = tmp_path / 'native'; folder = root / '.bazarlyk'; folder.mkdir(parents=True)
    registration_id, agent_id, session_id = map(str, (uuid4(), uuid4(), uuid4()))
    registration = {'schema_version': 1, 'registration_id': registration_id, 'agent_id': agent_id,
                    'workspace': str(root), 'service_url': 'http://synthetic.invalid', 'credential_file': '.bazarlyk/credential'}
    path = folder / 'registration.json'; path.write_text(json.dumps(registration))
    binding = folder / 'session-binding.json'; binding.write_text(json.dumps({'registration_id': registration_id,
        'session_id': session_id, 'generation': 1})); binding.chmod(0o600)
    actor = {'actor_id': agent_id, 'actor_kind': 'agent'}
    access = {'actor': actor, 'instance_id': 'inst_' + '1' * 32, 'registration_id': registration_id,
              'selection': {'package_ids': ['sample']}}
    run = {'run_id': 'run-saved', 'actor': actor}
    layer = SimpleNamespace(workflows=SimpleNamespace(get=lambda identity: {'run': run}))
    created = {}; invoked = []
    def dispatch(_layer, request):
        op = request['arguments']['operation_id']; invoked.append(op)
        if op not in created:
            created[op] = run
            raise RuntimeError('synthetic interruption after engine commit')
        return {'run': created[op], 'operation_id': op}
    monkeypatch.setattr(client, 'dispatch', dispatch)
    native = client.NativeClient(path)
    native._call = lambda method, params: {'structuredContent': {'status': 'ok', 'data': {'version': 1,
        'payload': {'instance_id': access['instance_id'], 'operation': 'workflows.start', 'operation_id': 'stable-1'},
        'signature': 'synthetic'}}}
    args = {'ref': 'sample/flow', 'operation_id': 'stable-1', 'actor': actor, 'selection': access['selection']}
    user_args = {'ref': 'sample/flow', 'operation_id': 'stable-1'}
    with pytest.raises(RuntimeError): native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)
    access['selection'] = {'package_ids': ['sample', 'new-grant']}
    args['selection'] = access['selection']
    with pytest.raises(AgenticError) as denied:
        native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)
    assert denied.value.code == 'access_unavailable' and invoked == ['stable-1']
    access['selection'] = {'package_ids': ['sample']}
    args['selection'] = access['selection']
    assert native._attention_mutation(layer, 'workflows.start', args, {}, {}, access, user_arguments=user_args)['run']['run_id'] == 'run-saved'
    assert invoked == ['stable-1', 'stable-1'] and len(created) == 1
