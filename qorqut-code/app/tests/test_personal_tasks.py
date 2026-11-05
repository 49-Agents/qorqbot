"""Personal task aggregation preserves business ownership and current authority."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.transport.compatibility import DashboardCompatibility
from test_service_transports import ServiceFixture, ORIGIN


def test_personal_feed_scope_pagination_and_business_mutations(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path, full=True)
    runtime, fixture = service.runtime, service.fixture
    monkeypatch.setattr(runtime.background, 'start', lambda: None)
    owner, other, colleague = fixture.owner.id, new_id(), new_id()
    hq = fixture.root / 'beta/hq'
    hq.mkdir(parents=True)
    with runtime.database.transaction() as uow:
        uow.repository('businesses').insert({'id': other, 'slug': 'beta', 'name': 'Beta', 'hq_path': str(hq), 'workspace': str(hq.parent)})
        uow.repository('principals').insert({'id': colleague, 'kind': 'human', 'display_name': 'Colleague'})
        for business in (fixture.business_id, other):
            if business == other:
                uow.repository('business_memberships', business_id=business).insert({'principal_id': owner, 'is_owner': True})
            uow.repository('business_memberships', business_id=business).insert({'principal_id': colleague})
            repo = uow.repository('tasks', business_id=business)
            for identity, assignee, status in [('shared-id', owner, 'open'), ('closed', owner, 'done'), ('unassigned', None, 'open'), ('colleague', colleague, 'open')]:
                repo.insert({'id': identity, 'title': identity, 'status': status, 'created_by': owner})
                if assignee:
                    uow.repository('relationships', business_id=business).insert({'id': new_id(), 'from_type': 'task', 'from_id': identity, 'to_type': 'person', 'to_id': assignee, 'role': 'assigned_to'})
        for index in range(205):
            uow.repository('tasks', business_id=other).insert({'id': f'page-{index:03}', 'title': 'Paged task', 'status': 'open', 'created_by': owner})
            uow.repository('relationships', business_id=other).insert({'id': new_id(), 'from_type': 'task', 'from_id': f'page-{index:03}', 'to_type': 'person', 'to_id': owner, 'role': 'assigned_to'})
    with TestClient(service.web) as client:
        response = client.get('/api/me/tasks')
        assert response.status_code == 200, response.text
        feed = response.json()
        assert feed['principal_id'] == owner
        tasks = feed['tasks']
        assert len(tasks) == 209
        assert all(owner in task['assignee_ids'] for task in tasks)
        assert len({(task['business_id'], task['id']) for task in tasks}) == 209
        assert {task['business']['slug'] for task in tasks} == {'widget', 'beta'}
        assert all(task['business']['id'] == task['business_id'] for task in tasks)
        assert sum(task['status'] == 'done' for task in tasks) == 2
        assert client.get('/api/me/tasks', params={'assignee_id': colleague}).json() == feed
        beta = client.get('/api/biz/beta/task/shared-id').json()['task']
        changed = client.post('/api/biz/beta/task/shared-id', json={'expected_revision': beta['revision'], 'set': {'status': 'done'}}, headers=ORIGIN)
        assert changed.status_code == 200, changed.text
        assert client.get('/api/biz/widget/task/shared-id').json()['task']['status'] == 'open'
        stale = client.post('/api/biz/beta/task/shared-id', json={'expected_revision': beta['revision'], 'set': {'status': 'open'}}, headers=ORIGIN)
        assert stale.status_code == 409
        with runtime.database.transaction() as uow:
            uow.repository('business_memberships', business_id=other).update(owner, {'active': False}, expected_revision=1)
        remaining = client.get('/api/me/tasks').json()['tasks']
        assert len(remaining) == 2
        assert all(task['business_id'] == fixture.business_id for task in remaining)
        assert client.get('/api/biz/beta/task/shared-id').status_code in (403, 404)

    compatibility = DashboardCompatibility(runtime, background=runtime.background)
    with runtime.database.read() as uow:
        actor = runtime.policy.context_for(service.agent.id, business_id=fixture.business_id, review_business_id=fixture.business_id, uow=uow)
    with pytest.raises(DomainError, match='human principal'):
        compatibility._global_get(SimpleNamespace(method='GET'), actor, ['me', 'tasks'])
    with runtime.database.read() as uow:
        human = runtime.policy.context_for(owner, business_id=None, review_business_id=None, uow=uow)
    with pytest.raises(DomainError):
        compatibility._global_get(SimpleNamespace(method='POST'), human, ['me', 'tasks'])
    restricted = replace(human, allowed_business_ids=frozenset())
    assert compatibility._global_get(SimpleNamespace(method='GET'), restricted, ['me', 'tasks']).body.endswith(b'"tasks":[]}')
