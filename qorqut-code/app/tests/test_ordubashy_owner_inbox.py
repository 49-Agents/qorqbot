"""Owner-task following uses real event triggers and current source authorization."""
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.domains.stories.operations import StoryOperations
from bazarlyk.domains.tasks import TaskOperations
from bazarlyk.inbox.service import InboxService
from test_signoffs import Scenario


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(Scenario, '_install_task_operations', lambda s: TaskOperations().register(s.registry))
    s = Scenario(tmp_path)
    stories = StoryOperations()
    stories.register(s.registry, s.app)
    s.inbox = InboxService(s.app, SimpleNamespace(stories=stories), resolver=s.resolver).register()
    s.ordubashy = s.add_agent('ordubashy')
    with s.db.transaction() as u:
        u.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': s.owner.id})
        u.repository('business_memberships', business_id=s.other_business).insert({'principal_id': s.ordubashy.id})
    s.ctx = lambda: s.agent_context(s.ordubashy)
    s.call = lambda op, args: s.app.invoke(s.ctx(), op, args, {'idempotency_key': new_id()})
    return s


def task(s, *, identity='owner-task', business=None, assigned=True):
    business = business or s.business
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.repository('tasks', business_id=business).insert({'id': identity, 'title': 'Owner work', 'status': 'open', 'created_by': s.owner.id})
        if assigned:
            u.repository('relationships', business_id=business).insert({'id': new_id(), 'from_type': 'task', 'from_id': identity,
                'to_type': 'person', 'to_id': s.owner.id, 'role': 'assigned_to'})
    return identity


def events(s):
    with s.db.read() as u:
        return s.inbox.pending(s.ctx(), u)['items']


def follows(s):
    result = s.call('inbox_subscriptions_list', {})
    assert result.status == 'ok', result.to_dict()
    return list(result.data['items'])


def edit(s, identity='owner-task', *, actor=None, business=None, **changes):
    with s.db.transaction() as u:
        u.actor_id = actor or s.owner.id
        row = u.repository('tasks', business_id=business or s.business).get(identity)
        u.repository('tasks', business_id=business or s.business).update(identity, changes, expected_revision=row['revision'])


def unassign(s, identity='owner-task'):
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.connection.execute("DELETE FROM relationships WHERE business_id=? AND from_type='task' AND from_id=? AND to_id=? AND role='assigned_to'",
            (s.business, identity, s.owner.id))


def test_future_assignments_comments_completion_and_no_self_echo(inbox):
    s = inbox
    task(s)
    assert len(events(s)) == 1  # Creation and its assignment deduplicate.
    row = follows(s)[0]
    assert row['automatic'] and row['id'] is None and row['source_id'] == 'owner-task'
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_subscriptions').fetchone()[0] == 0
    edit(s, status='done')
    edit(s, status='open')
    edit(s, archived=True)
    assert len(events(s)) == 4 and len(follows(s)) == 1
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.repository('task_notes', business_id=s.business).insert({'id': new_id(), 'task_id': 'owner-task', 'body': 'New evidence', 'created_by': s.owner.id})
    assert len(events(s)) == 5
    edit(s, actor=s.ordubashy.id, title='Self edit')
    assert len(events(s)) == 5


def test_manual_follow_is_independent_and_deliveries_deduplicate(inbox):
    s = inbox
    task(s)
    result = s.call('inbox_subscribe', {'source_type': 'tasks', 'source_id': 'owner-task'})
    assert result.status == 'ok', result.to_dict()
    assert len(follows(s)) == 1 and follows(s)[0]['automatic'] and follows(s)[0]['id'] == result.data['subscription_id']
    edit(s, title='Both routes')
    assert len(events(s)) == 2
    s.call('inbox_unsubscribe', {'subscription_id': result.data['subscription_id']})
    assert len(follows(s)) == 1
    edit(s, title='Still automatic')
    assert len(events(s)) == 3
    manual = s.call('inbox_subscribe', {'source_type': 'tasks', 'source_id': 'owner-task'})
    unassign(s)
    assert len(follows(s)) == 1 and not follows(s)[0].get('automatic')
    before = len(events(s))
    edit(s, title='Manual survives removal')
    assert len(events(s)) == before + 1
    s.call('inbox_unsubscribe', {'subscription_id': manual.data['subscription_id']})
    edit(s, title='No remaining follower')
    assert len(events(s)) == before + 1


def test_removal_stops_only_future_deliveries_and_reassignment_resumes(inbox):
    s = inbox
    task(s)
    unassign(s)
    assert follows(s) == [] and len(events(s)) == 1
    edit(s, title='Unassigned update')
    assert len(events(s)) == 1
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.repository('relationships', business_id=s.business).insert({'id': new_id(), 'from_type': 'task', 'from_id': 'owner-task',
            'to_type': 'person', 'to_id': s.owner.id, 'role': 'assigned_to'})
    assert len(events(s)) == 2 and len(follows(s)) == 1


def test_existing_tasks_are_covered_without_replaying_history(inbox):
    s = inbox
    with s.db.transaction() as u:
        u.repository('installation').delete('service', expected_revision=1)
    task(s)
    assert not events(s) and not follows(s)
    with s.db.transaction() as u:
        u.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': s.owner.id})
    assert not events(s) and len(follows(s)) == 1
    edit(s, title='First future update')
    assert len(events(s)) == 1


def test_owner_identity_and_business_scope_not_display_names(inbox):
    s = inbox
    task(s, business=s.other_business)
    task(s, identity='someone-else', assigned=False)
    with s.db.transaction() as u:
        u.repository('principals').update(s.viewer.id, {'display_name': s.owner.display_name}, expected_revision=1)
        u.repository('relationships', business_id=s.business).insert({'id': new_id(), 'from_type': 'task', 'from_id': 'someone-else',
            'to_type': 'person', 'to_id': s.viewer.id, 'role': 'assigned_to'})
    assert len(follows(s)) == 1 and follows(s)[0]['business_id'] == s.other_business
    assert all(e['source_id'] != 'someone-else' for e in events(s))
    with s.db.transaction() as u:
        u.repository('business_memberships', business_id=s.other_business).update(s.ordubashy.id, {'active': False}, expected_revision=1)
    assert not follows(s) and not events(s)
    edit(s, business=s.other_business, title='Revoked business')
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_deliveries WHERE agent_id=?', (s.ordubashy.id,)).fetchone()[0] == 1


def test_current_permissions_roles_and_transaction_rollback(inbox):
    s = inbox
    task(s)
    s.change_override('tasks_get', 'deny', s.ordubashy)
    assert not follows(s) and not events(s)
    s.change_override('tasks_get', 'allow', s.ordubashy)
    assert len(follows(s)) == 1 and len(events(s)) == 1
    with pytest.raises(RuntimeError):
        with s.db.transaction() as u:
            u.connection.execute("UPDATE tasks SET title='Never committed' WHERE id='owner-task'")
            raise RuntimeError('rollback')
    assert len(events(s)) == 1
    with s.db.transaction() as u:
        profile = u.repository('agent_profiles', business_id=None).get(s.ordubashy.id)
        data = profile['profile_json']
        data['role'] = dict(s.roles['global-marketing'])
        u.repository('agent_profiles', business_id=None).update(s.ordubashy.id, {'profile_json': data}, expected_revision=profile['revision'])
    with s.db.read() as u:
        from bazarlyk.inbox.owner_tasks import ordubashies
        assert s.ordubashy.id not in ordubashies(u)
    edit(s, title='No automatic follow after role change')
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_deliveries WHERE agent_id=?', (s.ordubashy.id,)).fetchone()[0] == 1


def test_assignment_uses_final_transaction_state(inbox):
    s = inbox
    task(s, assigned=False)
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        edge = new_id()
        repo = u.repository('relationships', business_id=s.business)
        repo.insert({'id': edge, 'from_type': 'task', 'from_id': 'owner-task',
            'to_type': 'person', 'to_id': s.owner.id, 'role': 'assigned_to'})
        repo.delete(edge, expected_revision=1)
    assert not events(s) and not follows(s)


@pytest.mark.parametrize('revocation', ['owner_membership', 'owner_identity', 'agent_identity', 'scope'])
def test_revocations_stop_automatic_following(inbox, revocation):
    s = inbox
    task(s, business=s.other_business)
    with s.db.transaction() as u:
        if revocation == 'owner_membership':
            u.repository('business_memberships', business_id=s.other_business).update(s.owner.id, {'active': False}, expected_revision=1)
        elif revocation in {'owner_identity', 'agent_identity'}:
            identity = s.owner.id if revocation == 'owner_identity' else s.ordubashy.id
            u.repository('principals').update(identity, {'active': False}, expected_revision=1)
        else:
            profile = u.repository('agent_profiles', business_id=None).get(s.ordubashy.id)
            data = profile['profile_json']
            data['profile_revision'] += 1
            data['scope']['allowed_business_ids'] = [s.business]
            u.repository('agent_profiles', business_id=None).update(s.ordubashy.id, {'profile_json': data}, expected_revision=profile['revision'])
    if revocation == 'scope':
        # Ordubashy is required to cover every registered business. A corrupted
        # narrower profile fails closed rather than silently keeping old reads.
        with pytest.raises(DomainError, match='every registered business'):
            follows(s)
    elif revocation != 'agent_identity':
        assert not follows(s)
    edit(s, business=s.other_business, title='No new delivery')
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_deliveries WHERE agent_id=?', (s.ordubashy.id,)).fetchone()[0] == 1
