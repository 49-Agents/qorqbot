"""Synthetic NativeClient and real Attention/Agentic boundary (requires Attention data contribution)."""
from dataclasses import replace
import json
import sqlite3
from pathlib import Path

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from bazarlyk.agentic.client import NativeClient
from host_foundation_support import (build_instance, create_registered_global, owner_global_context,
                                     select_coordinator, set_agent_overrides, register_sources)
from test_agentic_access import registration
from test_agentic_client import fixture as agentic_fixture


def test_real_service_bridge_workflow_lifecycle_and_races(tmp_path, agentic_fixture, monkeypatch):
    from bazarlyk.agentic import installation
    open_installation = installation.open_installation
    def use_fixture_layer(*args, **kwargs):
        _, view = open_installation(*args, **kwargs)
        return agentic_fixture.layer, view
    monkeypatch.setattr(installation, 'open_installation', use_fixture_layer)
    host, _ = build_instance(tmp_path / 'host')
    try:
        from bazarlyk.agentic import attention as bridge_module
        evidence = bridge_module.workflow_evidence
        evidence_reads = []
        def verify_no_operations_writer(*args, **kwargs):
            # A separate writer can enter while the bridge reads Agentic evidence.
            with sqlite3.connect(host.database.path, timeout=0, isolation_level=None) as probe:
                probe.execute('BEGIN IMMEDIATE')
                probe.execute('ROLLBACK')
            evidence_reads.append(1)
            return evidence(*args, **kwargs)
        monkeypatch.setattr(bridge_module, 'workflow_evidence', verify_no_operations_writer)
        register_sources(host, host.business, 'first')
        agent = select_coordinator(host, create_registered_global(host, 'ordubashy'))
        reg = registration(host, agent)
        attention = host.runtime.application.ordubashy_attention
        set_agent_overrides(host, agent, {'tasks_get': 'allow', 'inbox_list': 'allow',
            'attention_workflow_context': 'allow', 'attention_workflow_sync': 'allow'}, (host.business,))
        owner = owner_global_context(host)
        def owner_call(name, args):
            result = host.invoke(owner, name, {'scope': 'global', **args})
            assert result.status == 'ok', result.to_dict()
            return plain(result.data)
        one = owner_call('tasks_create', {'id': 'one', 'title': 'First'})['task']
        two = owner_call('tasks_create', {'id': 'two', 'title': 'Second'})['task']
        ref1 = {'type': 'task', 'id': one['id'], 'business_id': None}
        ref2 = {'type': 'task', 'id': two['id'], 'business_id': None}
        owner_call('attention_add', {'task': ref1}); owner_call('attention_add', {'task': ref2})
        consumer = new_id()
        with host.database.transaction() as uow:
            row = uow.connection.execute('SELECT workspace FROM agent_profiles WHERE agent_id=?', (agent,)).fetchone()
            workspace = Path(row['workspace'])
            binding = {'registration_id': reg, 'agent_id': agent, 'session_id': consumer, 'workspace': str(workspace)}
            uow.connection.execute('INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation) VALUES(?,?,?,1)',
                                   (agent, consumer, json.dumps(binding)))
        folder = workspace / '.bazarlyk'; folder.mkdir(parents=True, exist_ok=True)
        registration_path = folder / 'registration.json'
        registration_path.write_text(json.dumps({'schema_version': 1, 'registration_id': reg, 'agent_id': agent,
            'workspace': str(workspace), 'service_url': 'http://synthetic.invalid', 'credential_file': '.bazarlyk/credential'}))
        session = folder / 'session-binding.json'
        session.write_text(json.dumps({'registration_id': reg, 'session_id': consumer, 'generation': 1})); session.chmod(0o600)
        ctx = host.runtime.resolver.runtime_context(Principal(agent, 'agent'), session_registration_id=reg)
        actor = {'actor_id': agent, 'actor_kind': 'agent'}
        selection = {'package_ids': ['sample'], 'project_id': None, 'work_type': None}
        race = {'enabled': False}

        class Bridge:
            def handle(self, request):
                if request['method'].startswith('notifications/'):
                    return None
                identity = request['id']
                if request['method'] == 'initialize':
                    result = {}
                else:
                    name, args = request['params']['name'], dict(request['params']['arguments'])
                    if name == 'agentic_access':
                        data = {'operation': args['operation'], 'actor': actor, 'selection': selection,
                            'root': str(agentic_fixture.layer.root), 'state': str(agentic_fixture.layer.state),
                            'instance_id': agentic_fixture.layer.instance_id, 'can_manage': False,
                            'profile_revision': 1, 'registration_id': reg, 'attention_required': True}
                        result = {'structuredContent': {'status': 'ok', 'data': data}}
                    else:
                        if name == 'attention_workflow_sync' and race['enabled']:
                            race['enabled'] = False
                            current = owner_call('attention_get', {})
                            owner_call('attention_focus_set', {'task': ref1, 'focus': ref1,
                                'intent': 'Owner selection', 'expected_revision': current['revision']})
                        key = args.pop('idempotency_key', None)
                        outcome = host.runtime.application.invoke(ctx, name, args, InvocationOptions(idempotency_key=key))
                        result = {'structuredContent': outcome.to_dict(), 'isError': outcome.status != 'ok'}
                return {'jsonrpc': '2.0', 'id': identity, 'result': result}
            def close(self): pass
        native = NativeClient(registration_path, bridge=Bridge())
        def execute(operation, arguments, *, task=None, intent=None):
            request = {'operation': operation, 'arguments': arguments}
            if task is not None: request['attention'] = {'task': task, 'intent': intent}
            return native.execute(request)
        started = execute('workflows.start', {'ref': 'sample/flow', 'operation_id': 'start-a'}, task=ref1, intent='First intent')
        run_id = started['run']['run_id']
        paused = execute('workflows.pause', {'run_id': run_id, 'operation_id': 'pause-a'})
        assert paused['run']['state'] == 'paused'
        resumed = execute('workflows.resume', {'run_id': run_id, 'operation_id': 'resume-a'}, task=ref2, intent='Second intent')
        assert resumed['attention']['outcome'] == 'attention_synchronized'
        with host.database.read() as uow:
            state = attention.state(uow, agent)
            association = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent, run_id)).fetchone()
            assert state['focus_json']['id'] == run_id and state['work_task_json'] == ref2
            assert json.loads(association['work_task_json']) == ref2 and association['detached_at'] is None

        # Old paused runs without an Attention association may be explicitly resumed with a selected Task.
        detached = agentic_fixture.layer.workflows.start('sample/flow', actor, selection=selection, operation_id='old-start')
        old_id = detached['run']['run_id']
        agentic_fixture.layer.workflows.pause(old_id, actor, 'old-pause')
        adopted = execute('workflows.resume', {'run_id': old_id, 'operation_id': 'old-resume'}, task=ref1, intent='Resume old')
        assert adopted['attention']['outcome'] == 'attention_synchronized'
        cancel = agentic_fixture.layer.workflows.start('sample/flow', actor, selection=selection, operation_id='other-start')
        cancelled = execute('workflows.cancel', {'run_id': cancel['run']['run_id'], 'operation_id': 'other-cancel'})
        assert cancelled['attention']['reason'] == 'association_absent'
        with host.database.read() as uow:
            assert attention.state(uow, agent)['focus_json']['id'] == old_id
        execute('workflows.pause', {'run_id': old_id, 'operation_id': 'old-pause-2'})
        race['enabled'] = True
        raced = execute('workflows.resume', {'run_id': old_id, 'operation_id': 'old-resume-2'}, task=ref2, intent='Changed work')
        assert raced['run']['state'] == 'running' and raced['attention']['reason'] == 'focus_changed'
        with host.database.read() as uow:
            state = attention.state(uow, agent)
            assoc = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent, old_id)).fetchone()
            assert state['focus_json'] == ref1 and state['intent'] == 'Owner selection'
            assert json.loads(assoc['work_task_json']) == ref1 and assoc['detached_at'] is not None
        completing = execute('workflows.start', {'ref': 'sample/flow', 'operation_id': 'complete-start'},
                             task=ref2, intent='Finish synthetic workflow')
        complete_id = completing['run']['run_id']
        completed = execute('workflows.complete_step', {'run_id': complete_id, 'step_id': 'step',
            'operation_id': 'complete-step', 'output': {'passed': True}})
        assert completed['run']['state'] == 'completed'
        with host.database.read() as uow:
            assert attention.state(uow, agent)['focus_json'] is None
        assert evidence_reads
    finally:
        host.runtime.release_instance()
        host.database.close()
