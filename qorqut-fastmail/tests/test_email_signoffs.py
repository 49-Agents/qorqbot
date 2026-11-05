"""Exact message review, edits, owner approval and delivery using synthetic Fastmail only."""
from dataclasses import replace
import pytest
from qorqut_operations.contracts import ChangeSet, new_id, plain
from qorqut_operations.application.plans import apply_records
from fastmail_host import build, agent_context, call, send_arguments, detail, approve, approved_job

@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


def propose(host, operation='fastmail_send', **changes):
    result = call(host, agent_context(host, host.ordubashy), operation, send_arguments(**changes))
    assert result.status == 'signoff_required', result.to_dict()
    return result


def amend(host, proposal, changes, actor=None, key=None):
    return host.runtime.signoffs.amend(actor or host.owner_context, proposal['id'], changes=changes,
        proposal_revision=proposal['proposal_revision'], payload_hash=proposal['payload_hash'],
        expected_status_revision=proposal['status_revision'], idempotency_key=key or new_id())


def test_edit_then_approve_sends_exactly_revised_email_once(host):
    result = propose(host)
    original = detail(host, result.signoff['id'])
    assert original['editable_fields'] == ['subject', 'body']
    assert original['outcome']['stage'] == 'awaiting_approval'
    assert host.engine.posts == []
    with host.instance.database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    changes = {'subject': 'Revised subject', 'body': 'Edited <body> & content\nSecond line'}
    key = new_id()
    changed = amend(host, original, changes, key=key)
    assert changed.status == 'ok', changed.to_dict()
    revised = changed.data['signoff']
    assert revised['id'] != original['id'] and revised['state'] == 'pending'
    assert detail(host, original['id'])['preview'] == original['preview']
    assert amend(host, original, changes, key=key).data['signoff']['id'] == revised['id']
    assert approve(host, original).status == 'error'
    assert host.engine.posts == []
    assert approve(host, revised).status == 'accepted'
    host.runtime.signoffs.run_one(revised['id'])
    applied = detail(host, revised['id'])
    assert applied['state'] == 'applied', applied
    assert applied['outcome']['stage'] == 'queued'
    job = applied['apply_result']['data']['job_id']
    assert host.runtime.background.run_one(job)['state'] == 'succeeded'
    assert host.runtime.background.run_one(job) is None
    assert len(host.engine.posts) == 1
    post = host.engine.posts[0]
    for key, value in {**send_arguments(), **changes}.items():
        assert post[key] == value
    outcome = detail(host, revised['id'])['outcome']
    assert outcome['stage'] == 'submitted' and outcome['provider_submission_accepted']
    assert not outcome['recipient_delivery_confirmed']


@pytest.mark.parametrize('changes', [{'to': ['other@example.test']}, {'identity_id': 'id-2'}, {'attachments': []},
    {'subject': ''}, {'subject': 'hello\nBcc: hidden@example.test'}, {'body': ' '}, {'body': 'x'*50001}])
def test_invalid_or_unauthorized_field_edits_do_not_send(host, changes):
    original = detail(host, propose(host).signoff['id'])
    assert amend(host, original, changes).status == 'error'
    assert detail(host, original['id'])['state'] == 'pending'
    assert not host.engine.posts


def test_only_owner_dashboard_can_edit_and_owner_cannot_bypass_review(host):
    original = detail(host, propose(host).signoff['id'])
    for actor in (agent_context(host, host.ordubashy), replace(host.owner_context, source='mcp')):
        assert amend(host, original, {'body': 'Changed'}, actor).status == 'error'
    assert call(host, host.owner_context, 'fastmail_send', send_arguments()).status == 'error'
    assert not host.engine.posts


def test_send_requires_owner_proof_even_with_a_forged_queued_job(host):
    result = propose(host)
    # Simulate an illicit job inserted without the owner ever deciding the proposal.
    with host.instance.database.transaction(business_id=None) as uow:
        row = uow.repository('signoffs', business_id=None).get(result.signoff['id'])
        plan = ChangeSet.from_bytes(row['change_set'], expected_hash=row['payload_hash'])
        apply_records(agent_context(host, host.ordubashy), plan, uow)
        job = next(c.resource.resource_id for c in plan.changes if c.resource.resource_type == 'jobs')
    assert host.runtime.background.run_one(job)['error']['code'] == 'forbidden'
    assert not host.engine.posts


def test_forward_preview_and_dispatch_are_bound_to_source_content(host):
    result = propose(host, 'fastmail_forward', forward_message_id='m-1')
    original = detail(host, result.signoff['id'])
    assert any('Original body' in effect for effect in original['preview']['effects'])
    job = approved_job(host, result)
    host.engine.messages['m-1']['body']['content'] = 'Different original'
    assert host.runtime.background.run_one(job)['error']['code'] == 'invalid_plan'
    assert not host.engine.posts


def test_uncertain_send_is_not_shown_as_sent(host):
    result = propose(host)
    job = approved_job(host, result)
    host.engine.mode = 'lost_before_record'
    host.runtime.background.run_one(job)
    assert detail(host, result.signoff['id'])['outcome']['stage'] == 'reconciling'
    for _ in range(7):
        host.runtime.background.run_one(job)
    outcome = detail(host, result.signoff['id'])['outcome']
    assert outcome['stage'] == 'unconfirmed' and not outcome['provider_submission_accepted']
    assert len(host.engine.posts) == 1
