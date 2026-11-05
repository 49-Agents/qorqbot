import json
import time

import pytest
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.overseer.archive import PrivateArchive
from native_chat_support import Native, count
from test_agentic_access import registration


def test_overseer_chat_capture_precedes_delivery_and_survives_scrub(tmp_path):
    native = Native(tmp_path, 'claude', role='learning-applier')
    native.publish()
    pin = native.pin()
    with pytest.raises(DomainError) as missing:
        native.chat.send(native.owner, native.agent, 'Review the changes.', 'chat-key-000001', pin)
    assert missing.value.code == 'overseer_storage_unavailable'
    assert count(native.runtime, 'native_inputs') == 0 and native.pane.typed() == []
    root = native.runtime.paths.overseer_archive
    root.mkdir(mode=0o700, parents=True)
    sent = native.chat.send(native.owner, native.agent, 'Review the changes.', 'chat-key-000001', pin)
    identity = sent['input']['id']
    native.pass_()
    native.pass_()
    with native.runtime.database.read() as uow:
        row = uow.connection.execute('SELECT * FROM overseer_owner_inputs WHERE input_id=?', (identity,)).fetchone()
        native_row = uow.connection.execute('SELECT * FROM native_inputs WHERE id=?', (identity,)).fetchone()
        assert native_row['state'] == 'submitted' and native_row['body'] is None
        material = json.loads(row['material_ref_json'])
        assert json.loads(row['source_json'])['owner_principal_id'] == native.owner
    archive = PrivateArchive(root, lambda ref: None, lambda ref, action: None)
    record = archive.get_retention(material)['records'][0]
    assert record['text'] == 'Review the changes.' and record['source']['native_input_id'] == identity
    registration_id = registration(native.instance, native.agent)
    ctx = native.runtime.resolver.runtime_context(Principal(native.agent, 'agent'), session_registration_id=registration_id)
    saved = native.runtime.overseer_retention.owner_request(ctx, identity)
    assert saved['eligible'] and saved['source_text'] == 'Review the changes.'
    assert not saved['delivery']['processing_confirmed'] and saved['delivery']['native_entry_ref'] is None
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE credentials SET revoked_at='2000-01-01T00:00:00Z' WHERE id="
                               '(SELECT credential_id FROM machine_registrations WHERE id=?)', (registration_id,))
    with pytest.raises(DomainError):
        native.runtime.overseer_retention.owner_request(ctx, identity)
    repeated = native.chat.send(native.owner, native.agent, 'Review the changes.', 'chat-key-000001', pin)
    assert repeated['created'] is False and repeated['input']['id'] == identity
    assert count(native.runtime, 'overseer_owner_inputs') == 1 and native.pane.typed() == ['Review the changes.']
    assert count(native.runtime, 'overseer_retention_events') >= 1


def test_owner_request_starts_only_bound_review_and_replays(tmp_path, monkeypatch):
    from pathlib import Path
    from uuid import uuid4
    from bazarlyk.overseer.invocation import start_from_owner
    from bazarlyk.overseer.inputs import create_input_request
    from host_foundation_support import grant_capabilities, owner_global_context

    native = Native(tmp_path, 'claude', role='learning-applier')
    native.publish()
    native.runtime.paths.overseer_archive.mkdir(mode=0o700, parents=True)
    pin = native.pin()
    sent = native.chat.send(native.owner, native.agent, 'Review the proposed changes with me.', 'chat-key-000002', pin)
    native.pass_()
    grant_capabilities(native.instance, native.agent, 'manage_agentic_layer', 'read_agent_experience')
    registration_id = registration(native.instance, native.agent)
    ctx = native.runtime.resolver.runtime_context(Principal(native.agent, 'agent'), session_registration_id=registration_id)
    request = native.runtime.overseer_retention.owner_request(ctx, sent['input']['id'])
    layer = native.runtime.agentic_layer
    # Native already installs the current packaged procedures.
    layer.catalog.read_entry('overseer/review')
    invocation = {'reference': request['reference']}
    meaning = {'kind': 'review', 'arguments': {}, 'rationale': 'The owner requested a review.'}
    result = start_from_owner(native.runtime, ctx, 'review', invocation, meaning, 'synthetic-review')
    assert result['state'] == 'linked'
    assert layer.workflows.get(result['run_id'])['run']['workflow_ref'] == 'overseer/review'
    assert start_from_owner(native.runtime, ctx, 'review', invocation, meaning, 'synthetic-review') == result
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) VALUES (?, 'synthetic', '{\"id\":\"synthetic\"}', 1, 'paused')", (native.agent,))
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, ctx, 'review', invocation, meaning, 'synthetic-review')
    with native.runtime.database.transaction() as uow:
        uow.connection.execute('DELETE FROM native_session_owners WHERE agent_id=?', (native.agent,))
    import asyncio
    from bazarlyk.transport.auth import AuthenticatedPrincipal
    from bazarlyk.transport.mcp import MCPTransport
    with native.runtime.database.read() as uow:
        row = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (native.agent,)).fetchone()
        credential = uow.connection.execute('SELECT credential_id FROM machine_registrations WHERE id=?', (registration_id,)).fetchone()[0]
    changed = native.runtime.application.invoke(owner_global_context(native.instance), 'agents_update',
        {'agent_id': native.agent, 'changes': {'tool_policy': {'overrides': {'overseer_review_start': 'allow',
            'overseer_evidence_list': 'allow', 'overseer_episode_get': 'allow', 'tasks_get': 'allow',
            'signoffs_comment': 'allow', 'tasks_comment': 'allow', 'tasks_create': 'allow', 'request_user_input': 'allow'}}}},
        {'expected_revision': row[0], 'idempotency_key': 'synthetic-mcp-policy'})
    assert changed.status == 'ok', changed.error
    ctx = native.runtime.resolver.runtime_context(Principal(native.agent, 'agent'), session_registration_id=registration_id)
    actor = AuthenticatedPrincipal(Principal(native.agent, 'agent'), credential, registration_id)
    transport = MCPTransport(native.runtime, None, [])
    wire = asyncio.run(transport._single_result('overseer_review_start',
        {'invocation': invocation, 'interpretation': meaning, 'operation_id': 'synthetic-review'}, actor))
    assert wire['status'] == 'ok', wire
    assert wire['data']['run_id'] == result['run_id']
    source_task = native.instance.invoke(native.instance.human(native.instance.business), 'tasks_create', {'title': 'Synthetic evidence'})
    assert source_task.status == 'ok', source_task.error
    task_record = source_task.to_dict()['data']['task']
    task_id = task_record['id']
    owner_ctx = native.instance.human(native.instance.business)
    for index in range(4):
        task_view = native.runtime.application.invoke(owner_ctx, 'tasks_get', {'task_id': task_id},
            {'business_id': native.instance.business})
        assert task_view.status == 'ok', task_view.error
        revision = task_view.to_dict()['data']['task']['revision']
        comment = native.runtime.application.invoke(owner_ctx, 'tasks_comment',
            {'task_id': task_id, 'text': f'Synthetic retained thread entry {index}: ' + ('evidence ' * 24)},
            {'business_id': native.instance.business, 'expected_revision': revision,
             'idempotency_key': f'episode-host-comment-{index}'})
        assert comment.status == 'ok', comment.error
    selection = {'mode': 'explicit', 'namespaces': [{'kind': 'business', 'business_id': native.instance.business}],
                 'task_ids': [task_id], 'agent_ids': []}
    delivery = native.runtime.application.invoke(ctx, 'overseer_evidence_list', {'run_id': result['run_id'], 'selection': selection})
    assert delivery.status == 'ok', delivery.error
    page = delivery.to_dict()['data']
    source = next(item for item in page['items'] if item['kind'] == 'task' and item['record_id'] == task_id)
    from bazarlyk.overseer.access import EvidenceAccess
    gate = EvidenceAccess(native.runtime, ctx)
    saved = PrivateArchive(native.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention).get_retention(page['presentation_ref'])
    assert saved['records'][0]['result'] == {key: value for key, value in page.items() if key != 'presentation_ref'}
    import hashlib
    from bazarlyk.overseer.coverage import AnalysisCoverage
    from bazarlyk.overseer.evidence import EvidenceProvider
    payload = {'run_ref': saved['run_ref'], 'records': saved['records'], 'source_refs': saved['source_refs']}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    provider = EvidenceProvider(native.runtime, ctx, native.runtime.paths.overseer_archive, gate,
                                overseer_ids={native.agent}, coverage=AnalysisCoverage(layer))
    assert provider.capture_presentation(saved['run_ref'], saved['records'], saved['source_refs'], 'presentation:' + digest) == page['presentation_ref']
    with monkeypatch.context() as patch:
        def refuse(*args, **kwargs):
            raise DomainError('evidence_unavailable', 'Synthetic archive failure')
        patch.setattr(PrivateArchive, 'capture_retention', refuse)
        failed = native.runtime.application.invoke(ctx, 'overseer_evidence_list', {'run_id': result['run_id'], 'selection': selection})
        assert failed.status == 'error' and 'data' not in failed.to_dict()
    wrong = native.runtime.application.invoke(ctx, 'overseer_evidence_list', {'run_id': 'run_unadmitted', 'selection': selection})
    assert wrong.status == 'error'
    episode_sources = {item['record_id']: item for item in page['items']
                       if item.get('episode_id') == source['episode_id']}
    assert len(episode_sources) >= 5
    cursor, page_count, offsets, fragments = None, 0, {}, {identity: [] for identity in episode_sources}
    started = time.perf_counter()
    while True:
        arguments = {'run_id': result['run_id'], 'episode_ref': {'id': source['episode_id']},
                     'frontier': page['frontier'], 'max_chars': 400}
        if cursor is not None:
            arguments['cursor'] = cursor
        delivery = native.runtime.application.invoke(ctx, 'overseer_episode_get', arguments)
        assert delivery.status == 'ok', delivery.error
        episode_page = delivery.to_dict()['data']
        page_count += 1
        assert episode_page['presentation_ref'] != page['presentation_ref']
        retained = PrivateArchive(native.runtime.paths.overseer_archive, gate.authorize_source,
            gate.authorize_retention).get_retention(episode_page['presentation_ref'])
        assert retained['records'][0]['result'] == {
            key: value for key, value in episode_page.items() if key != 'presentation_ref'}
        assert retained['source_refs'] == episode_page['source_refs']
        for record in episode_page['records']:
            identity = record['source_ref']['record_id']
            assert identity in episode_sources
            assert record['offset'] == offsets.get(identity, 0)
            offsets[identity] = offsets.get(identity, 0) + len(record['content_fragment'])
            fragments[identity].append(record['content_fragment'])
        cursor = episode_page['next_cursor']
        if cursor is None:
            break
    elapsed = time.perf_counter() - started
    assert page_count > 1 and len(offsets) == len(episode_sources)
    expected_content = {identity: json.dumps(provider.get_source(ref)['content'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False) for identity, ref in episode_sources.items()}
    assert {identity: ''.join(parts) for identity, parts in fragments.items()} == expected_content
    print(f'synthetic_real_evidenceaccess_delivery_seconds={elapsed:.6f} pages={page_count} sources={len(episode_sources)}')
    actor_ref = {'actor_id': native.agent, 'actor_kind': 'agent'}
    layer.workflows.pause(result['run_id'], actor_ref, 'synthetic-pause')
    paused = native.runtime.application.invoke(ctx, 'overseer_evidence_list', {'run_id': result['run_id'], 'selection': selection})
    assert paused.status == 'error'
    layer.workflows.resume(result['run_id'], actor_ref, 'synthetic-resume')
    with pytest.raises(DomainError) as error:
        start_from_owner(native.runtime, ctx, 'review', invocation, {**meaning, 'rationale': 'Different reading.'}, 'changed')
    assert error.value.code == 'operation_conflict'
    assert count(native.runtime, 'overseer_invocations') == 1

    fixture = json.loads(Path('/home/alp/qorqbot-v1.3/execution/overseer-v1/fixtures/contracts-v1.json').read_text())
    proposal, delta = fixture['proposals'][0], fixture['delta_revision']
    proposal['sources'] = [{'ref': source, 'role': 'supporting'}]
    event_ids = [source['origin_event_id']] if source['origin_event_id'] else []
    episode_ids = [source['episode_id']] if source['episode_id'] else []
    delta['support_summary'] = {'proposal_count': 1, 'event_count': len(event_ids), 'episode_count': len(episode_ids),
                                'event_refs': event_ids, 'episode_refs': episode_ids}
    layer.overseer.proposals.record(proposal, 'proposal-1')
    layer.overseer.deltas.create(delta, 'delta-1')
    layer.overseer.review.save_batch({'schema_version': 1, 'id': str(uuid4()), 'run_id': result['run_id'],
        'items': [{'delta_id': delta['delta_id'], 'revision': 1, 'ranking_reason': delta['ranking_reason']}],
        'presentation_ref': None, 'created_at': '2026-10-27T19:00:00Z'}, 'batch-1')
    binding_id = str(uuid4())
    binding = {'schema_version': 1, 'id': binding_id, 'purpose': 'review', 'run_id': result['run_id'],
        'targets': {'delta_id': delta['delta_id'], 'delta_revision': 1, 'application_id': None, 'plan_revision': None},
        'owner_scope': {'kind': 'global', 'business_id': None}, 'request_key': 'overseer:' + binding_id,
        'request_ref': None, 'choice_actions': {'accept': 'accept', 'later': 'later'}}
    form = {'title': 'Review change', 'explanation': 'Choose whether to accept the shown change.', 'kind': 'question',
        'selection_mode': 'single', 'selection_required': True,
        'choices': [{'id': 'accept', 'label': 'Accept', 'explanation': 'Accept the shown change.'},
                    {'id': 'later', 'label': 'Later', 'explanation': 'Leave it undecided.'}]}
    invoke = native.runtime.application.invoke
    def crash_after_create(context, name, *args, **kwargs):
        result = invoke(context, name, *args, **kwargs)
        if name == 'signoffs_input_create' and result.status == 'ok':
            raise RuntimeError('synthetic crash after canonical Signoff creation')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(native.runtime.application, 'invoke', crash_after_create)
        with pytest.raises(RuntimeError, match='synthetic crash'):
            create_input_request(native.runtime, ctx, binding, form, 'request-1')
    with native.runtime.database.read() as uow:
        signoff_id = uow.connection.execute('SELECT id FROM signoffs WHERE request_key=?', (binding['request_key'],)).fetchone()[0]
        assert uow.connection.execute('SELECT count(*) FROM overseer_signoffs WHERE binding_id=?', (binding_id,)).fetchone()[0] == 0
    signoff = native.runtime.signoffs.get(owner_global_context(native.instance), signoff_id).to_dict()['data']['signoff']
    assert signoff['input']['response_policy'] == 'owner_only'
    answered = native.runtime.signoffs.respond(owner_global_context(native.instance), signoff['id'],
        proposal_revision=signoff['proposal_revision'], payload_hash=signoff['payload_hash'],
        expected_status_revision=signoff['status_revision'], idempotency_key='owner-answer', selected_choices=['accept'],
        notes='Only if the scope is narrowed.')
    assert answered.status == 'ok', answered.error
    answer = answered.to_dict()['data']['signoff']
    created = create_input_request(native.runtime, ctx, binding, form, 'request-1')
    assert created['signoff_id'] == signoff_id
    response_ref = {**created['binding']['request_ref'], 'status_revision': answer['status_revision']}
    ambiguous = native.runtime.overseer.mutate('overseer_input_reconcile', ctx, {'binding_id': binding_id,
        'response': {'reference': response_ref,
        'interpretation': {'clarity': 'conditional', 'action': 'accept', 'rationale': 'The answer depends on narrowing the scope.'}},
        'operation_id': 'response-1'})
    assert ambiguous['status'] == 'needs_clarification'
    assert layer.overseer.deltas.get(delta['delta_id'])['decision'] == 'undecided'
    next_id = str(uuid4())
    replacement = {**binding, 'id': next_id, 'request_key': 'overseer:' + next_id, 'resolves_input_ids': [binding_id]}
    next_request = create_input_request(native.runtime, ctx, replacement, form, 'request-2')
    next_signoff = native.runtime.signoffs.get(owner_global_context(native.instance), next_request['signoff_id']).to_dict()['data']['signoff']
    next_answer = native.runtime.signoffs.respond(owner_global_context(native.instance), next_signoff['id'],
        proposal_revision=next_signoff['proposal_revision'], payload_hash=next_signoff['payload_hash'],
        expected_status_revision=next_signoff['status_revision'], idempotency_key='owner-answer-2', selected_choices=['accept'])
    assert next_answer.status == 'ok', next_answer.error
    next_ref = {**next_request['binding']['request_ref'], 'status_revision': next_answer.to_dict()['data']['signoff']['status_revision']}
    decision = native.runtime.overseer.mutate('overseer_input_reconcile', ctx,
        {'binding_id': next_id, 'response': {'reference': next_ref, 'interpretation': None}, 'operation_id': 'response-2'})
    assert decision['action'] == 'accept'
    assert layer.overseer.applications.get(decision['application_id'])['phase'] == 'queued'
    assert create_input_request(native.runtime, ctx, binding, form, 'request-1')['signoff_id'] == signoff['id']
    from bazarlyk.overseer.retention_access import RetentionEventAccess
    event_key = 'signoff:' + binding_id
    native.runtime.overseer_retention.drain._event(event_key)
    with native.runtime.database.read() as uow:
        material = uow.connection.execute('SELECT material_ref_json FROM overseer_retained_materials WHERE event_key=?', (event_key,)).fetchone()
    gate = RetentionEventAccess(native.runtime, event_key)
    records = PrivateArchive(native.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(material[0]))['records']
    assert any(r['kind'] == 'signoff_note' and r['body'] == 'Only if the scope is narrowed.' for r in records)
    assert any(r['kind'] == 'signoff' and r['state'] == 'answered' for r in records)
    comment = native.runtime.application.invoke(ctx, 'signoffs_comment',
        {'scope': 'global', 'signoff_id': signoff_id, 'text': 'A later independent comment.'},
        {'idempotency_key': 'later-comment'})
    assert comment.status == 'ok', comment.error
    native.runtime.overseer_retention.drain._event(event_key)
    with native.runtime.database.read() as uow:
        materials = uow.connection.execute('SELECT material_ref_json FROM overseer_retained_materials WHERE event_key=?', (event_key,)).fetchall()
    records = [record for material in materials for record in PrivateArchive(
        native.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(material[0]))['records']]
    assert any(r['kind'] == 'signoff_note' and r['body'] == 'A later independent comment.' for r in records)
    # A real typed presentation is linked only to its explicitly named delta.
    import copy
    from chat_input_support import present, result_record, wire
    from bazarlyk.overseer.reads import OverseerReads
    observed = copy.deepcopy(proposal)
    observed['id'] = str(uuid4())
    observed['sources'] = [{'ref': source, 'role': 'supporting'}]
    relevant = copy.deepcopy(delta)
    relevant['delta_id'] = str(uuid4())
    relevant['proposal_ids'] = [observed['id']]
    event_ids = [source['origin_event_id']] if source['origin_event_id'] else []
    episode_ids = [source['episode_id']] if source['episode_id'] else []
    relevant['support_summary'] = {'proposal_count': 1, 'event_count': len(event_ids), 'episode_count': len(episode_ids),
                                   'event_refs': event_ids, 'episode_refs': episode_ids}
    layer.overseer.proposals.record(observed, 'observed-proposal')
    layer.overseer.deltas.create(relevant, 'observed-delta')
    shown = present(native, 'Here is the specific change.', [{'type': 'delta', 'id': relevant['delta_id']},
                    {'type': 'signoff', 'id': signoff_id}], ctx=ctx)
    assert shown.status == 'ok', shown.error
    native.write(result_record(native, wire(shown)))
    native.pass_(3)
    with native.runtime.database.read() as uow:
        owner_ctx = native.runtime.policy.context_for(native.owner, business_id=None, review_business_id=None, uow=uow, source='dashboard')
    page = OverseerReads(native.runtime, owner_ctx).discussion('delta', relevant['delta_id'])
    assert any(item['text'] == 'Here is the specific change.' and item['kind'] == 'presentation' for item in page['items'])
    # P05 is still outside Gate A: use its explicit task-link contract fake,
    # while all host identities, workflow admission and Task records are real.
    from types import SimpleNamespace
    from bazarlyk.overseer.executor import development_task
    apply_sent = native.chat.send(native.owner, native.agent, 'Apply the accepted change.', 'chat-apply-0001', pin)
    native.pass_()
    apply_request = native.runtime.overseer_retention.owner_request(ctx, apply_sent['input']['id'])
    app_id = decision['application_id']
    apply_meaning = {'kind': 'application', 'arguments': {'application_id': app_id}, 'rationale': 'The owner selected this application.'}
    admission = start_from_owner(native.runtime, ctx, 'application', {'reference': apply_request['reference']}, apply_meaning, 'apply-start')
    original_apps = layer.overseer.applications
    actual_app = original_apps.get(app_id)
    assert actual_app['run_id'] == admission['run_id']
    app_state = {**actual_app, 'phase': 'working', 'plan_revision': 1, 'agreement_ref': {'synthetic': 'owner-agreed'},
                 'plan': {'delta_revision': actual_app['accepted_revision'], 'external_requirements': [{'id': 'development'}, {'id': 'created-work'}]}, 'external_tasks': []}
    def link_task(application_id, requirement_id, task_ref, **options):
        assert application_id == app_id and requirement_id in ('development', 'created-work')
        app_state['external_tasks'] = [item for item in app_state['external_tasks'] if item['requirement_id'] != requirement_id]
        app_state['external_tasks'].append({'requirement_id': requirement_id, 'task_ref': task_ref,
                                            'observation': None, 'linked_plan_revision': 1})
        return {'application': dict(app_state), 'required_input': None}
    fake_apps = SimpleNamespace(get=lambda identity, plan_revision=None: dict(app_state), link_external_task=link_task)
    with monkeypatch.context() as patch:
        patch.setattr(layer.overseer, 'applications', fake_apps)
        task_view = native.runtime.application.invoke(ctx, 'tasks_get', {'task_id': task_id},
                                                       {'business_id': native.instance.business})
        assert task_view.status == 'ok', task_view.error
        task_record = task_view.to_dict()['data']['task']
        task_ref = {'provider': 'operations', 'kind': 'task', 'id': task_id, 'revision': str(task_record['revision']),
                    'namespace': {'kind': 'business', 'business_id': native.instance.business}}
        linked = development_task(native.runtime, ctx, 'overseer_development_task_link',
            {'application_id': app_id, 'requirement_id': 'development', 'task_ref': task_ref,
             'expected_revision': app_state['revision'], 'operation_id': 'link-development'})
        assert linked['application']['external_tasks'][0]['task_ref'] == task_ref
        replay = development_task(native.runtime, ctx, 'overseer_development_task_link',
            {'application_id': app_id, 'requirement_id': 'development', 'task_ref': task_ref,
             'expected_revision': app_state['revision'], 'operation_id': 'link-development'})
        assert replay['application']['external_tasks'][0]['task_ref'] == task_ref
        assert count(native.runtime, 'overseer_retention_events', "kind='task'") == 1
        comment = native.runtime.application.invoke(ctx, 'tasks_comment', {'task_id': task_id, 'text': 'The requested work is checked.'},
            {'business_id': native.instance.business, 'expected_revision': task_record['revision'], 'idempotency_key': 'task-feedback'})
        assert comment.status == 'ok', comment.error
        with native.runtime.database.read() as uow:
            task_event = uow.connection.execute("SELECT event_key FROM overseer_retention_events WHERE kind='task'").fetchone()[0]
        native.runtime.overseer_retention.drain._event(task_event)
        with native.runtime.database.read() as uow:
            task_material = uow.connection.execute('SELECT material_ref_json FROM overseer_retained_materials WHERE event_key=?', (task_event,)).fetchone()[0]
        task_gate = RetentionEventAccess(native.runtime, task_event)
        task_records = PrivateArchive(native.runtime.paths.overseer_archive, task_gate.authorize_source, task_gate.authorize_retention).get_retention(json.loads(task_material))['records']
        assert any(item['message']['body'] == 'The requested work is checked.' for item in task_records)
        discussion = OverseerReads(native.runtime, owner_ctx).discussion('application', app_id)
        assert any(item['text'] == 'The requested work is checked.' for item in discussion['items'])
        create_args = {'application_id': app_id, 'requirement_id': 'created-work',
                       'task': {'title': 'Implement the external change', 'business_id': native.instance.business, 'assignee_ids': []},
                       'expected_revision': app_state['revision'], 'operation_id': 'create-development'}
        created = development_task(native.runtime, ctx, 'overseer_development_task_create', create_args)
        repeated = development_task(native.runtime, ctx, 'overseer_development_task_create', create_args)
        assert created['task']['id'] == repeated['task']['id']
        with pytest.raises(DomainError) as error:
            development_task(native.runtime, ctx, 'overseer_development_task_create',
                {**create_args, 'task': {**create_args['task'], 'title': 'A different task'}})
        assert error.value.code == 'operation_conflict'
        assert count(native.runtime, 'overseer_development_tasks') == 2
    from dataclasses import replace
    from qorqut_operations.contracts import new_id
    from host_foundation_support import revoke_capabilities
    denied = present(native, 'Unavailable reference', [{'type': 'delta', 'id': str(uuid4())}], ctx=ctx)
    assert denied.status == 'error'
    wrong_registration = replace(ctx, session_registration_id=new_id())
    assert present(native, 'Wrong registration', [], ctx=wrong_registration).status == 'error'
    delegated = replace(ctx, delegation_binding_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
    assert present(native, 'Delegated caller', [], ctx=delegated).status == 'error'
    routine = {'reference': {'provider': 'qorqut', 'kind': 'extraction_due', 'id': 'synthetic-due', 'revision': '1'}}
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, ctx, 'extraction', routine,
                         {'kind': 'extraction', 'arguments': {}, 'rationale': 'Routine is not yet installed.'}, 'routine-start')
    with native.runtime.database.read() as uow:
        revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (native.agent,)).fetchone()[0]
    source_denied = native.runtime.application.invoke(owner_global_context(native.instance), 'agents_update',
        {'agent_id': native.agent, 'changes': {'tool_policy': {'overrides': {'request_user_input': 'allow', 'tasks_get': 'deny'}}}},
        {'expected_revision': revision, 'idempotency_key': 'deny-presentation-source'})
    assert source_denied.status == 'ok', source_denied.error
    assert present(native, 'Denied source', [{'type': 'delta', 'id': relevant['delta_id']}], ctx=ctx).status == 'error'
    with native.runtime.database.read() as uow:
        revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (native.agent,)).fetchone()[0]
    denied_policy = native.runtime.application.invoke(owner_global_context(native.instance), 'agents_update',
        {'agent_id': native.agent, 'changes': {'tool_policy': {'overrides': {'request_user_input': 'deny'}}}},
        {'expected_revision': revision, 'idempotency_key': 'deny-presentation'})
    assert denied_policy.status == 'ok', denied_policy.error
    assert present(native, 'Denied by policy', [], ctx=ctx).status == 'error'
    with native.runtime.database.read() as uow:
        revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (native.agent,)).fetchone()[0]
    allowed_policy = native.runtime.application.invoke(owner_global_context(native.instance), 'agents_update',
        {'agent_id': native.agent, 'changes': {'tool_policy': {'overrides': {'request_user_input': 'allow'}}}},
        {'expected_revision': revision, 'idempotency_key': 'restore-presentation'})
    assert allowed_policy.status == 'ok', allowed_policy.error
    revoke_capabilities(native.instance, native.agent, 'manage_agentic_layer')
    assert present(native, 'Missing capability', [], ctx=ctx).status == 'error'
    grant_capabilities(native.instance, native.agent, 'manage_agentic_layer')
    native.end()
    native.pass_()
    assert present(native, 'No continuing session', [], ctx=ctx).status == 'error'


def test_offline_observer_retains_visible_output_and_redacts_unknown_users(tmp_path):
    from host_foundation_support import grant_capabilities
    from bazarlyk.overseer.retention_access import RetentionEventAccess
    native = Native(tmp_path, 'claude', role='learning-applier')
    native.publish()
    root = native.runtime.paths.overseer_archive
    root.mkdir(mode=0o700, parents=True)
    native.write(native.claude_user('Secret from unknown source'), native.claude_user('Another unknown input'),
                 native.claude_assistant('Visible agent reply'))
    native.pass_()
    assert count(native.runtime, 'overseer_retained_materials') == 0
    grant_capabilities(native.instance, native.agent, 'manage_agentic_layer')
    registration_id = registration(native.instance, native.agent)
    native.end()
    native.build()  # restart the observer, with the same committed receipt and no MCP request
    native.pass_()
    with native.runtime.database.read() as uow:
        rows = uow.connection.execute("SELECT m.event_key,m.material_ref_json FROM overseer_retained_materials m JOIN overseer_retention_events e ON e.event_key=m.event_key WHERE e.kind='native_capture'").fetchall()
    assert rows
    records = []
    for row in rows:
        gate = RetentionEventAccess(native.runtime, row['event_key'])
        records.extend(PrivateArchive(root, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(row['material_ref_json']))['records'])
    users = [r for r in records if r['source_kind'] == 'native_user']
    assert len({r['entry_id'] for r in users}) == 2
    assert all(r['text'] is None and r['actor_kind'] == 'unknown' and r['gaps'] == [{'code': 'sensitivity_provenance_unresolved'}] for r in users)
    assert any(r['text'] == 'Visible agent reply' and r['actor_kind'] == 'agent' for r in records)
    assert 'Secret from unknown source' not in json.dumps(records)
    gate = RetentionEventAccess(native.runtime, rows[0]['event_key'])
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE credentials SET expires_at='2000-01-01T00:00:00Z' WHERE id=(SELECT credential_id FROM machine_registrations WHERE id=?)", (registration_id,))
    with pytest.raises(DomainError):
        PrivateArchive(root, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(rows[0]['material_ref_json']))
    with native.runtime.database.transaction() as uow:
        uow.connection.execute('UPDATE credentials SET expires_at=NULL WHERE id=(SELECT credential_id FROM machine_registrations WHERE id=?)', (registration_id,))
        original_workspace = uow.connection.execute('SELECT workspace FROM machine_registrations WHERE id=?', (registration_id,)).fetchone()[0]
        uow.connection.execute('UPDATE machine_registrations SET workspace=? WHERE id=?', (original_workspace + '-other', registration_id))
    with pytest.raises(DomainError):
        PrivateArchive(root, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(rows[0]['material_ref_json']))
    with native.runtime.database.transaction() as uow:
        uow.connection.execute('UPDATE machine_registrations SET workspace=? WHERE id=?', (original_workspace, registration_id))
        event_source = uow.connection.execute('SELECT source_json FROM overseer_retention_events WHERE event_key=?', (rows[0]['event_key'],)).fetchone()[0]
        wrong = json.loads(event_source); wrong['conversation_id'] = 'wrong-conversation'
        uow.connection.execute('UPDATE overseer_retention_events SET source_json=? WHERE event_key=?', (json.dumps(wrong), rows[0]['event_key']))
    with pytest.raises(DomainError):
        PrivateArchive(root, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(rows[0]['material_ref_json']))
    with native.runtime.database.transaction() as uow:
        uow.connection.execute('UPDATE overseer_retention_events SET source_json=? WHERE event_key=?', (event_source, rows[0]['event_key']))
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE credentials SET revoked_at='2000-01-01T00:00:00Z' WHERE id=(SELECT credential_id FROM machine_registrations WHERE id=?)", (registration_id,))
    with pytest.raises(DomainError):
        PrivateArchive(root, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(rows[0]['material_ref_json']))


def test_real_reconciler_null_principal_is_retained_as_observed_telegram_owner(tmp_path):
    from qorqut_operations.contracts import new_id, utc_now
    from bazarlyk.native_chat.contracts import InputRequest, TYPE_TELEGRAM
    from host_foundation_support import grant_capabilities, owner_global_context
    n = Native(tmp_path, 'claude', role='learning-applier')
    n.publish()
    n.runtime.paths.overseer_archive.mkdir(mode=0o700, parents=True)
    grant_capabilities(n.instance, n.agent, 'manage_agentic_layer')
    with n.runtime.database.read() as uow:
        revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (n.agent,)).fetchone()[0]
    change = n.runtime.application.invoke(owner_global_context(n.instance), 'agents_update',
        {'agent_id': n.agent, 'changes': {'tool_policy': {'overrides': {'agents_get': 'allow', 'overseer_owner_requests_list': 'allow'}}}},
        {'expected_revision': revision, 'idempotency_key': 'telegram-synthetic-policy'})
    assert change.status == 'ok', change.error
    reg = registration(n.instance, n.agent)
    message_id, telegram_id, correlation = new_id(), new_id(), new_id()
    now = utc_now()
    words = 'Please examine the saved evidence.'
    typed = 'Qorqut owner message via Telegram.\n[Telegram message ' + message_id + ']\n' + words
    with n.runtime.database.transaction() as uow:
        uow.connection.execute('''INSERT INTO telegram_messages(id,direction,chat_name,chat_id,purpose,body,agent_id,occurred_at,created_at,updated_at)
            VALUES (?, 'inbound', 'ordubashy', -5, 'owner', ?, ?, ?, ?, ?)''', (message_id, words, n.agent, now, now, now))
        uow.connection.execute('''INSERT INTO telegram_owner_inputs(id,purpose,correlation_id,message_id,agent_id,owner_principal_id,
            config_generation,binding_fingerprint,target_fingerprint,state,created_at,updated_at)
            VALUES (?, 'owner_message', ?, ?, ?, ?, 1, ?, ?, 'uncertain', ?, ?)''',
            (telegram_id, correlation, message_id, n.agent, n.owner, 'a' * 64, 'b' * 64, now, now))
    n.pane.enter_works = False
    outcome = n.arbiter.submit(InputRequest(n.agent, TYPE_TELEGRAM, 'telegram:' + telegram_id, typed))
    assert outcome.state == 'uncertain'
    n.pane.pasted = None
    n.write(n.claude_user(typed))
    n.pass_(2)
    with n.runtime.database.read() as uow:
        raw = uow.connection.execute('SELECT principal_id,state,reason,evidence_json FROM native_inputs WHERE id=?', (outcome.input_id,)).fetchone()
        assert raw['principal_id'] is None and raw['state'] == 'uncertain' and raw['reason'] == 'transcript_confirmed'
        assert json.loads(raw['evidence_json'])['native_input_observed'] is True
    ctx = n.runtime.resolver.runtime_context(Principal(n.agent, 'agent'), session_registration_id=reg)
    request = n.runtime.overseer_retention.owner_request(ctx, outcome.input_id)
    assert request['eligible'] and request['source_text'] == words
    assert request['source']['native_principal_id'] is None
    assert request['source']['owner_principal_source'] == 'telegram_owner_input'
    assert request['source']['owner_principal_ref']['record_id'] == telegram_id
    assert request['delivery']['state'] == 'uncertain' and not request['delivery']['processing_confirmed']
    assert request['delivery']['native_entry_ref'] is not None
    assert count(n.runtime, 'overseer_owner_inputs', 'input_id=?', (outcome.input_id,)) == 1
    listing = n.runtime.application.invoke(ctx, 'overseer_owner_requests_list', {})
    assert listing.status == 'ok', listing.error
    assert any(item.get('reference') == request['reference'] for item in listing.to_dict()['data']['items'])
