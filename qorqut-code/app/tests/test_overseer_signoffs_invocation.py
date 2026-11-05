"""Synthetic real Signoffs as narrowly bound workflow invocations."""
import json

import pytest
from qorqut_operations.contracts import DomainError, Principal
from native_chat_support import Native
from test_agentic_access import registration
from host_foundation_support import grant_capabilities, owner_global_context
from bazarlyk.overseer.invocation import start_from_owner
from bazarlyk.overseer.workflow_requests import create_request, request_view
from bazarlyk.overseer.archive import PrivateArchive
from bazarlyk.overseer.retention_access import RetentionEventAccess


def test_real_workflow_signoff_recovery_and_explicit_start(tmp_path, monkeypatch):
    n = Native(tmp_path, 'claude', role='learning-applier')
    n.publish()
    n.runtime.paths.overseer_archive.mkdir(mode=0o700, parents=True)
    n.pin()
    grant_capabilities(n.instance, n.agent, 'manage_agentic_layer')
    with n.runtime.database.read() as uow:
        profile_revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (n.agent,)).fetchone()[0]
    configured = n.runtime.application.invoke(owner_global_context(n.instance), 'agents_update',
        {'agent_id': n.agent, 'changes': {'tool_policy': {'overrides': {'overseer_owner_requests_list': 'allow'}}}},
        {'expected_revision': profile_revision, 'idempotency_key': 'synthetic-policy'})
    assert configured.status == 'ok', configured.error
    reg = registration(n.instance, n.agent)
    ctx = n.runtime.resolver.runtime_context(Principal(n.agent, 'agent'), session_registration_id=reg)
    layer = n.runtime.agentic_layer
    # The composed Native fixture already installs the current Overseer package.
    params = dict(request_key='workflow-review-1', kind='review', arguments={}, question='Should we begin review?', operation_id='workflow-create-1')
    invoke = n.runtime.application.invoke
    def crash(context, name, *args, **kwargs):
        result = invoke(context, name, *args, **kwargs)
        if name == 'signoffs_input_create' and result.status == 'ok':
            raise RuntimeError('synthetic lost association')
        return result
    with monkeypatch.context() as p:
        p.setattr(n.runtime.application, 'invoke', crash)
        with pytest.raises(RuntimeError, match='lost association'):
            create_request(n.runtime, ctx, **params)
    result = create_request(n.runtime, ctx, **params)
    assert result['response_reference'] is None
    assert layer.workflows.list()['total'] == 0
    sid = result['request']['request_ref']['signoff_id']
    signoff = n.runtime.signoffs.get(owner_global_context(n.instance), sid).to_dict()['data']['signoff']
    assert signoff['input']['response_policy'] == 'owner_only'
    assert signoff['input']['choices'][0]['label'] == 'Start workflow'
    from host_foundation_support import create_registered_global, select_coordinator
    coordinator = create_registered_global(n.instance, 'ordubashy')
    select_coordinator(n.instance, coordinator)
    coordinator_registration = registration(n.instance, coordinator)
    coordinator_ctx = n.runtime.resolver.runtime_context(Principal(coordinator, 'agent'), session_registration_id=coordinator_registration)
    delegated = n.runtime.application.invoke(coordinator_ctx, 'signoffs_input_answer_delegated',
        {'scope': 'global', 'signoff_id': sid, 'proposal_revision': signoff['proposal_revision'],
         'payload_hash': signoff['payload_hash'], 'expected_status_revision': signoff['status_revision'],
         'selected_choices': ['start']}, {'idempotency_key': 'delegated-workflow-answer'})
    assert delegated.status == 'error'
    assert request_view(n.runtime, ctx, sid)['response_reference'] is None
    assert create_request(n.runtime, ctx, **params) == result
    with pytest.raises(DomainError):
        create_request(n.runtime, ctx, **{**params, 'question': 'A different request?'})
    page = n.runtime.application.invoke(ctx, 'overseer_owner_requests_list', {})
    assert page.status == 'ok', page.error
    assert any(item.get('request', {}).get('id') == result['request']['id'] for item in page.to_dict()['data']['items'])
    answered = n.runtime.signoffs.respond(owner_global_context(n.instance), sid, proposal_revision=signoff['proposal_revision'],
        payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'], idempotency_key='answer-1',
        selected_choices=['start'], notes='Start only after checking the scope.')
    assert answered.status == 'ok', answered.error
    view = request_view(n.runtime, ctx, sid)
    assert layer.workflows.list()['total'] == 0
    ref = view['response_reference']
    assert view['response']['notes'] == 'Start only after checking the scope.'
    intent = {'kind': 'review', 'arguments': {}, 'rationale': 'The owner requested this review.'}
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'review', {'reference': ref}, intent, 'start-1')
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'review', {'reference': ref}, intent, 'start-1',
                         {'clarity': 'conditional', 'action': 'start', 'rationale': 'The answer has a condition.'})
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'application', {'reference': ref}, {**intent, 'kind': 'application'}, 'start-1',
                         {'clarity': 'explicit', 'action': 'start', 'rationale': 'Start.'})
    interpretation = {'clarity': 'explicit', 'action': 'start', 'rationale': 'The owner directs review after checking scope.'}
    started = start_from_owner(n.runtime, ctx, 'review', {'reference': ref}, intent, 'start-1', interpretation)
    assert started['state'] == 'linked'
    assert start_from_owner(n.runtime, ctx, 'review', {'reference': ref}, intent, 'start-1', interpretation) == started
    assert 'Start only after checking the scope.' in started['invocation']['source_text']
    event = 'workflow-signoff:' + result['request']['id']
    n.runtime.overseer_retention.drain._event(event)
    with n.runtime.database.read() as uow:
        material = uow.connection.execute('SELECT material_ref_json FROM overseer_retained_materials WHERE event_key=?', (event,)).fetchone()
    gate = RetentionEventAccess(n.runtime, event)
    records = PrivateArchive(n.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(material[0]))['records']
    assert any(r['kind'] == 'signoff_note' and r['body'] == 'Start only after checking the scope.' for r in records)
    def answer(key, selected, notes=''):
        created = create_request(n.runtime, ctx, key, 'extraction', {}, 'Should extraction begin?', key + '-op')
        identity = created['request']['request_ref']['signoff_id']
        question = n.runtime.signoffs.get(owner_global_context(n.instance), identity).to_dict()['data']['signoff']
        outcome = n.runtime.signoffs.respond(owner_global_context(n.instance), identity,
            proposal_revision=question['proposal_revision'], payload_hash=question['payload_hash'],
            expected_status_revision=question['status_revision'], idempotency_key=key + '-answer',
            selected_choices=selected, notes=notes)
        assert outcome.status == 'ok', outcome.error
        return request_view(n.runtime, ctx, identity)['response_reference']
    extraction = {'kind': 'extraction', 'arguments': {}, 'rationale': 'The owner requests extraction.'}
    later = answer('workflow-later', ['later'])
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'extraction', {'reference': later}, extraction, 'later-start', interpretation)
    free = answer('workflow-free', [], 'Please begin extraction now.')
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'extraction', {'reference': free}, extraction, 'free-start')
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'extraction', {'reference': {**free, 'revision': '0' * 64}}, extraction, 'wrong-revision', interpretation)
    with n.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) VALUES (?, 'synthetic', '{\"id\":\"synthetic\"}', 1, 'paused')", (n.agent,))
    with pytest.raises(DomainError):
        start_from_owner(n.runtime, ctx, 'extraction', {'reference': free}, extraction, 'free-start', interpretation)
    with n.runtime.database.transaction() as uow:
        uow.connection.execute('DELETE FROM native_session_owners WHERE agent_id=?', (n.agent,))
    result = start_from_owner(n.runtime, ctx, 'extraction', {'reference': free}, extraction, 'free-start', interpretation)
    assert result['state'] == 'linked' and 'Please begin extraction now.' in result['invocation']['source_text']
    lost = dict(request_key='workflow-lost-answer', kind='review', arguments={}, question='Begin a separate review?', operation_id='lost-answer-op')
    with monkeypatch.context() as p:
        p.setattr(n.runtime.application, 'invoke', crash)
        with pytest.raises(RuntimeError, match='lost association'):
            create_request(n.runtime, ctx, **lost)
    with n.runtime.database.read() as uow:
        lost_id = uow.connection.execute('SELECT id FROM signoffs WHERE request_key=?', (lost['request_key'],)).fetchone()[0]
    lost_signoff = n.runtime.signoffs.get(owner_global_context(n.instance), lost_id).to_dict()['data']['signoff']
    answered = n.runtime.signoffs.respond(owner_global_context(n.instance), lost_id,
        proposal_revision=lost_signoff['proposal_revision'], payload_hash=lost_signoff['payload_hash'],
        expected_status_revision=lost_signoff['status_revision'], idempotency_key='lost-answer', selected_choices=['start'])
    assert answered.status == 'ok', answered.error
    recovered = create_request(n.runtime, ctx, **lost)
    assert recovered['request']['request_ref']['signoff_id'] == lost_id and recovered['response_reference']['id'] == lost_id
    clear = start_from_owner(n.runtime, ctx, 'review', {'reference': recovered['response_reference']}, intent, 'clear-start')
    assert clear['state'] == 'linked'
    cursor, seen = None, []
    while True:
        options = {'limit': 1, **({'cursor': cursor} if cursor else {})}
        listed = n.runtime.application.invoke(ctx, 'overseer_owner_requests_list', options)
        assert listed.status == 'ok', listed.error
        page = listed.to_dict()['data']
        seen.extend(item['request']['id'] for item in page['items'] if item.get('request'))
        cursor = page['next_cursor']
        if not cursor: break
    assert len(seen) == len(set(seen)) == 4
