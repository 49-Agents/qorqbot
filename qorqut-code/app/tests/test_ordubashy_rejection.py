from dataclasses import replace

import pytest

from qorqut_operations.contracts import new_id
from test_signoffs import Scenario


def proposal(s):
    result = s.app.invoke(s.agent_context(), 'tasks_create', {'title': 'Needs review'}, {'idempotency_key': new_id()})
    assert result.status == 'signoff_required', result.to_dict()
    return s.service.get(s.owner_context, result.signoff['id']).data['signoff']


def arguments(row, **changes):
    return {'signoff_id': row['id'], 'proposal_revision': row['proposal_revision'],
        'payload_hash': row['payload_hash'], 'expected_status_revision': row['status_revision'],
        'feedback': 'Not ready: clarify the scope.', **changes}


def test_reject_notes_attribution_replay_and_no_approval(tmp_path):
    s = Scenario(tmp_path)
    from test_management_backend import install_coordinator
    actor = install_coordinator(s)
    row = proposal(s)
    ctx = s.agent_context(actor)
    args, options = arguments(row), {'idempotency_key': new_id()}
    result = s.app.invoke(ctx, 'signoffs_reject', args, options)
    assert result.status == 'ok', result.to_dict()
    saved = result.data['signoff']
    assert saved['state'] == 'rejected'
    assert saved['decision']['by'] == actor.id
    assert saved['decision']['feedback'] == args['feedback']
    assert s.app.invoke(ctx, 'signoffs_reject', args, options).status == 'ok'
    assert s.service.run_pending() == 0
    assert s.service.review(ctx, row['id'], decision='approve', proposal_revision=row['proposal_revision'],
        payload_hash=row['payload_hash'], expected_status_revision=row['status_revision'], idempotency_key=new_id()).status == 'error'


@pytest.mark.parametrize('change', [
    {'expected_status_revision': 999}, {'proposal_revision': 999}, {'payload_hash': 'wrong'},
    {'decision': 'approve'}, {'feedback': 'x' * 8001},
])
def test_reject_requires_exact_binding_and_fixed_action(tmp_path, change):
    s = Scenario(tmp_path)
    row = proposal(s)
    result = s.app.invoke(s.agent_context(s.add_agent('ordubashy')), 'signoffs_reject', arguments(row, **change), {'idempotency_key': new_id()})
    assert result.status == 'error'
    assert s.service.get(s.owner_context, row['id']).data['signoff']['state'] == 'pending'


def test_other_roles_denied_even_with_override_and_revocation(tmp_path):
    s = Scenario(tmp_path)
    row = proposal(s)
    s.change_override('signoffs_reject', 'allow')
    assert s.app.invoke(s.agent_context(), 'signoffs_reject', arguments(row), {'idempotency_key': new_id()}).status == 'error'
    from test_management_backend import install_coordinator
    actor = install_coordinator(s)
    ctx = s.agent_context(actor)
    s.change_override('signoffs_reject', 'deny', actor)
    assert s.app.invoke(ctx, 'signoffs_reject', arguments(row), {'idempotency_key': new_id()}).status == 'error'


def test_wrong_business_and_input_requests_denied(tmp_path):
    from test_signoff_inputs import FORM
    s = Scenario(tmp_path)
    from test_management_backend import install_coordinator
    actor = install_coordinator(s)
    row = proposal(s)
    ctx = s.agent_context(actor)
    wrong = replace(ctx, business_id=s.other_business, review_business_id=s.other_business)
    assert s.app.invoke(wrong, 'signoffs_reject', arguments(row), {'idempotency_key': new_id()}).status == 'error'
    created = s.app.invoke(ctx, 'signoffs_input_create', {'request_key': 'question', 'form': FORM}, {'idempotency_key': new_id()})
    assert created.status == 'ok', created.to_dict()
    assert s.app.invoke(ctx, 'signoffs_reject', arguments(created.data['signoff']), {'idempotency_key': new_id()}).status == 'error'


def test_global_rejection_and_already_decided_denied(tmp_path):
    s = Scenario(tmp_path)
    from test_management_backend import install_coordinator
    actor = install_coordinator(s)
    doc = s.document(global_scope=True)
    staged = s.app.invoke(s.agent_context(actor), 'shared_instructions_update',
        {'document_ids': [doc['id']], 'text': 'Proposed'}, {'idempotency_key': new_id()})
    assert staged.status == 'signoff_required'
    ctx = replace(s.agent_context(actor), business_id=None, review_business_id=None)
    row = s.service.get(ctx, staged.signoff['id']).data['signoff']
    result = s.app.invoke(ctx, 'signoffs_reject', arguments(row, scope='global'), {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    again = s.app.invoke(ctx, 'signoffs_reject', arguments(result.data['signoff'], scope='global'), {'idempotency_key': new_id()})
    assert again.status == 'error'
    assert s.service.run_pending() == 0


def test_owner_task_completion_reopening_and_stale_revision(tmp_path, monkeypatch):
    from bazarlyk.domains.tasks import TaskOperations
    monkeypatch.setattr(Scenario, '_install_task_operations', lambda self: TaskOperations().register(self.registry))
    s = Scenario(tmp_path)
    from test_management_backend import install_coordinator
    actor = install_coordinator(s)
    ctx = s.agent_context(actor)
    with s.db.transaction() as u:
        u.repository('tasks', business_id=s.business).insert({'id': 'owner-task', 'title': 'Owner work', 'status': 'open', 'created_by': s.owner.id})
        u.repository('relationships', business_id=s.business).insert({'id': new_id(), 'from_type': 'task', 'from_id': 'owner-task',
            'to_type': 'person', 'to_id': s.owner.id, 'role': 'assigned_to'})
    def task():
        return s.app.invoke(ctx, 'tasks_get', {'task_id': 'owner-task'}).data['task']
    before = task()
    result = s.app.invoke(ctx, 'tasks_complete', {'task_id': 'owner-task'}, {'expected_revision': before['revision'], 'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    assert task()['closed'] and task()['assignee_ids'] == before['assignee_ids']
    stale = s.app.invoke(ctx, 'tasks_update', {'task_id': 'owner-task', 'changes': {'status': 'open'}},
        {'expected_revision': before['revision'], 'idempotency_key': new_id()})
    assert stale.status == 'error'
    result = s.app.invoke(ctx, 'tasks_update', {'task_id': 'owner-task', 'changes': {'status': 'open'}},
        {'expected_revision': task()['revision'], 'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    assert not task()['closed'] and task()['assignee_ids'] == before['assignee_ids']
