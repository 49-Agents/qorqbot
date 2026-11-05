from dataclasses import replace

from starlette.testclient import TestClient
from qorqut_operations.contracts import InvocationOptions, new_id
from test_service_transports import ORIGIN, ServiceFixture


def test_global_notes_crud_scope_and_no_context_upload(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path, full=True)
    monkeypatch.setattr(service.runtime.background, 'start', lambda: None)
    fixture = service.fixture
    with TestClient(service.web) as client:
        service.login(client)
        assert client.get('/api/root/notes').json() == []
        created = client.post('/api/root/notes', json={'title':'Global journal', 'content':'First thought', 'date':'2026-09-10'}, headers=ORIGIN)
        assert created.status_code == 200, created.text
        note = created.json()
        identity = note['id']
        assert note['content'] == 'First thought'
        assert note['date'] == '2026-09-10'
        business = fixture.db
        with business.read() as uow:
            slug = uow.repository('businesses').get(fixture.business_id)['slug']
            row = uow.repository('documents', business_id=None).get(identity)
            assert row['kind'] == 'global_note' and row['owning_agent_id'] is None
        assert not client.get(f'/api/biz/{slug}/notes').json()
        def update(values, version=note):
            return client.post(f'/api/root/notes/{identity}', json={**values,
                'expected_revision':version['revision'], 'expected_content_hash':version['content_hash']}, headers=ORIGIN)
        changed = update({'title':'Renamed', 'content':'- One\nTwo', 'pinned':True, 'statuses':{'0':'done'}})
        assert changed.status_code == 200, changed.text
        current = changed.json()
        assert current['pinned'] and current['title'] == 'Renamed'
        assert current['content'] == '- One\nTwo'
        assert current['statuses'] == {'0':'done'}
        assert update({'title':'Stale'}).status_code != 200
        assert client.get('/api/root/notes').json()[0]['title'] == 'Renamed'
        cross = client.post(f'/api/biz/{slug}/notes/{identity}',json={'title':'Wrong scope'},headers=ORIGIN)
        assert cross.status_code != 200
        upload = client.post(f'/api/root/notes/{identity}/upload',json={},headers=ORIGIN)
        assert upload.status_code != 200
        deleted = client.post(f'/api/root/notes/{identity}/delete',json={
            'expected_revision':current['revision'], 'expected_content_hash':current['content_hash']},headers=ORIGIN)
        assert deleted.status_code == 200, deleted.text
        assert client.get('/api/root/notes').json() == []
        with business.read() as uow:
            assert uow.repository('documents',business_id=None).get(identity)['archived']
            assert not uow.connection.execute("select id from jobs where kind='context.file'").fetchall()
        # A scoped agent cannot use the owner's notebook, even with global context capability.
        with business.read() as uow:
            agent_context = service.runtime.policy.context_for(service.agent.id, business_id=fixture.business_id, review_business_id=fixture.business_id, uow=uow)
        result = service.runtime.application.invoke(replace(agent_context,business_id=None,home_business_id=None,
            global_capabilities=frozenset({'read_global_context'})), 'notes_get', {'document_id':identity}, InvocationOptions())
        assert result.status == 'error'

        with business.read() as uow:
            owner_context = service.runtime.policy.context_for(fixture.owner.id,
                business_id=None, review_business_id=None, uow=uow)
        standalone = service.runtime.application.invoke(replace(owner_context, allowed_business_ids=frozenset()),
            'notes_create', {'title':'No business needed', 'body':'Root notebook'},
            InvocationOptions(idempotency_key=new_id()))
        assert standalone.status == 'ok', standalone.to_dict()
