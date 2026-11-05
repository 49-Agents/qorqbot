"""Real synthetic host: shared owner instructions use the delta/Signoff queue."""
import asyncio
from dataclasses import replace

import pytest
from qorqut_operations.contracts import DomainError, Principal
from bazarlyk.overseer import direct
from bazarlyk.transport.auth import AuthenticatedPrincipal
from bazarlyk.transport.mcp import MCPTransport
from host_foundation_support import create_registered_global, grant_capabilities, owner_global_context, select_coordinator
from native_chat_support import Native, count
from test_agentic_access import registration

TEXT = 'Whenever writing an email, never use an em dash.\nKeep this limited to emails.'
SCOPE = {'description': 'Email composition', 'project_ids': [], 'work_types': ['email'],
         'workflow_refs': [], 'conditions': [], 'exceptions': []}


def _setup(tmp_path, role='ordubashy'):
    n = Native(tmp_path, 'codex' if role == 'ordubashy' else 'claude', role=role)
    if role == 'ordubashy':
        select_coordinator(n.instance, n.agent)
        create_registered_global(n.instance, 'learning-applier')
    n.runtime.paths.overseer_archive.mkdir(mode=0o700, parents=True)
    grant_capabilities(n.instance, n.agent, 'manage_agentic_layer',
                       *(['read_agent_experience'] if role == 'learning-applier' else []))
    n.publish()
    sent = n.chat.send(n.owner, n.agent, TEXT, 'direct-owner-instruction', n.pin())
    n.pass_()
    reg = registration(n.instance, n.agent)
    ctx = n.runtime.resolver.runtime_context(Principal(n.agent, 'agent'), session_registration_id=reg)
    with n.runtime.database.read() as uow:
        credential = uow.connection.execute('SELECT credential_id FROM machine_registrations WHERE id=?', (reg,)).fetchone()[0]
        assert uow.connection.execute('SELECT body FROM native_inputs WHERE id=?', (sent['input']['id'],)).fetchone()[0] is None
    actor = AuthenticatedPrincipal(Principal(n.agent, 'agent'), credential, reg)
    transport = MCPTransport(n.runtime, None, [])
    n.wire = lambda name, args: asyncio.run(transport._single_result(name, args, actor))
    n.args = dict(request_key='direct-email-1', instruction_text=TEXT, summary='Avoid em dashes in emails',
                  change_direction='Add an email-only rule to the email composition skill.', scope=SCOPE,
                  target_refs=['email/composition'], source_id=sent['input']['id'])
    return n, ctx


def answer(n, result, choice, notes=''):
    s = result['signoff']
    response = n.runtime.signoffs.respond(owner_global_context(n.instance), s['id'],
        proposal_revision=s['proposal_revision'], payload_hash=s['payload_hash'],
        expected_status_revision=s['status_revision'], idempotency_key='owner-answer-'+s['id'],
        selected_choices=[choice], notes=notes)
    assert response.status == 'ok', response.error
    current = n.runtime.signoffs.get(owner_global_context(n.instance), s['id']).to_dict()['data']['signoff']
    return {'reference': {'provider': 'qorqut_signoff', 'signoff_id': s['id'],
        'proposal_revision': s['proposal_revision'], 'payload_hash': s['payload_hash'],
        'status_revision': current['status_revision']}, 'interpretation': None}


@pytest.mark.parametrize('role', ['ordubashy', 'learning-applier'])
def test_real_shared_submission_and_acceptance_only_queues(tmp_path, role):
    n, ctx = _setup(tmp_path, role)
    layer = n.runtime.agentic_layer
    before = {str(p.relative_to(layer.root)): p.read_bytes() for p in layer.root.rglob('*') if p.is_file() and '.git' not in p.parts}
    wire = n.wire('agentic_change_propose', n.args)
    assert wire['status'] == 'ok', wire
    result = wire['data']
    assert result['signoff']['input']['response_policy'] == 'owner_only'
    assert {c['label'] for c in result['signoff']['input']['choices']} == {'Accept', 'Reject', 'Revise', 'Later'}
    assert count(n.runtime, 'signoffs') == 1
    assert n.wire('agentic_change_propose', n.args)['data'] == result
    assert layer.overseer.proposals.list()['items'][0]['origin_type'] == 'direct_instruction'
    # Its exact original owner message remains readable after transient-body scrubbing.
    from bazarlyk.overseer.evidence import CaptureJournal
    from bazarlyk.overseer.access import EvidenceAccess
    source = layer.overseer.proposals.get(result['proposal_id'])['sources'][0]['ref']
    from bazarlyk.agents.global_scope import active_role_holder
    with n.runtime.database.read() as uow:
        holder = active_role_holder(uow.connection, 'learning-applier')
    journal = CaptureJournal(n.runtime.paths.overseer_archive, EvidenceAccess(n.runtime, ctx), overseer_ids={holder})
    assert journal.get_source(source)['text'] == TEXT
    response = answer(n, result, 'accept')
    reconciled = n.wire('agentic_change_reconcile', {'submission_id': result['submission_id'],
        'response': response, 'operation_id': 'shared-accept'})
    assert reconciled['status'] == 'ok', reconciled
    assert reconciled['data']['delta']['decision'] == 'accepted'
    assert layer.workflows.list()['total'] == 0
    with layer.overseer.proposals.ctx.store.reader() as conn:
        queued = conn.execute('SELECT phase FROM delta_applications').fetchall()
        assert [r[0] for r in queued] == ['queued']
    assert not reconciled['data']['active_files_changed']
    assert before == {str(p.relative_to(layer.root)): p.read_bytes() for p in layer.root.rglob('*') if p.is_file() and '.git' not in p.parts}
    assert n.wire('agentic_change_get', {'submission_id': result['submission_id']})['status'] == 'ok'
    from bazarlyk.overseer.reads import OverseerReads
    views = OverseerReads(n.runtime, owner_global_context(n.instance))
    assert any(item['id'] == result['delta']['id'] for item in views.deltas(decision='accepted')['items'])
    assert views.evidence(result['delta']['id'], result['proposal_id'], 0)['content']['text'] == TEXT
    from bazarlyk.overseer.evidence import HostScanner
    with n.runtime.database.read() as uow:
        assert HostScanner._review_signoff(result['signoff']['id'], uow)
    if role == 'learning-applier':
        import json
        from bazarlyk.overseer.archive import PrivateArchive
        event = 'signoff:' + result['binding']['binding']['id']
        n.runtime.overseer_retention.drain._event(event)
        with n.runtime.database.read() as uow:
            retained = uow.connection.execute('SELECT material_ref_json FROM overseer_retained_materials WHERE event_key=?', (event,)).fetchone()
        assert retained is not None
        gate = EvidenceAccess(n.runtime, ctx)
        saved = PrivateArchive(n.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention).get_retention(json.loads(retained[0]))
        assert saved['records']
    for operation in ('authoring.apply', 'history.restore'):
        denied = n.wire('agentic_access', {'operation': operation})
        assert denied['status'] == 'error' and denied['error']['code'] == 'owner_input_required', denied
    if role == 'ordubashy':
        assert n.wire('agentic_access', {'operation': 'overseer.proposals.list'})['status'] == 'error'
        assert n.wire('overseer_proposals_list', {})['status'] == 'error'


def test_shared_submission_recovers_lost_signoff_and_rejects_forgery(tmp_path, monkeypatch):
    n, ctx = _setup(tmp_path)
    for args in ({**n.args, 'instruction_text': TEXT+' Invented.'}, {**n.args, 'scope': {**SCOPE, 'project_ids': ['00000000-0000-4000-8000-000000000001']}}):
        assert n.wire('agentic_change_propose', args)['status'] == 'error'
    assert count(n.runtime, 'signoffs') == 0
    invoke = n.runtime.application.invoke
    def lost(context, name, *args, **kwargs):
        result = invoke(context, name, *args, **kwargs)
        if name == 'signoffs_input_create' and result.status == 'ok':
            raise RuntimeError('synthetic lost reply')
        return result
    with monkeypatch.context() as p:
        p.setattr(n.runtime.application, 'invoke', lost)
        with pytest.raises(RuntimeError, match='lost reply'):
            direct.propose(n.runtime, ctx, **n.args)
    result = direct.propose(n.runtime, ctx, **n.args)
    assert count(n.runtime, 'signoffs') == 1
    assert len(n.runtime.agentic_layer.overseer.proposals.list()['items']) == 1
    assert n.wire('agentic_change_propose', {**n.args, 'change_direction': 'Different change'})['status'] == 'error'
    response = answer(n, result, 'reject')
    outcome = direct.reconcile(n.runtime, ctx, submission_id=result['submission_id'], response=response, operation_id='reject-direct')
    assert outcome['delta']['decision'] == 'rejected'
    assert direct.propose(n.runtime, ctx, **n.args)['delta']['decision'] == 'rejected'
    with pytest.raises(DomainError):
        direct.get(n.runtime, replace(ctx, session_registration_id=None), result['submission_id'])
    with n.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE credentials SET revoked_at='2000-01-01T00:00:00Z' WHERE id=(SELECT credential_id FROM machine_registrations WHERE id=?)", (ctx.session_registration_id,))
    assert n.wire('agentic_change_get', {'submission_id': result['submission_id']})['status'] == 'error'


def test_comments_cannot_be_silently_treated_as_acceptance(tmp_path):
    n, ctx = _setup(tmp_path)
    result = direct.propose(n.runtime, ctx, **n.args)
    response = answer(n, result, 'accept', 'Only if you first show me the narrower wording.')
    outcome = n.wire('agentic_change_reconcile', {'submission_id': result['submission_id'],
        'response': response, 'operation_id': 'conditional-direct'})
    assert outcome['status'] == 'ok', outcome
    assert outcome['data']['result']['status'] == 'needs_clarification'
    with n.runtime.agentic_layer.overseer.proposals.ctx.store.reader() as conn:
        assert conn.execute('SELECT COUNT(*) FROM delta_applications').fetchone()[0] == 0
