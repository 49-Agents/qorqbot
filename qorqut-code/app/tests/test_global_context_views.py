from starlette.testclient import TestClient

from test_service_transports import ServiceFixture


def test_global_library_routes_return_scoped_metadata_and_bounded_body(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.background.start = lambda: None
    document = service.fixture.document('global-material', 'Global evidence <literal>' * 1000, 'context/material.md')
    with service.runtime.database.transaction() as uow:
        uow.repository('documents', business_id=None).update(document['id'],
            {'kind': 'context_note', 'metadata_json': {'title': 'Global filed material'}}, expected_revision=document['revision'])
    with TestClient(service.web) as client:
        service.login(client)
        result = client.get('/api/root/context-library')
        assert result.status_code == 200, result.text
        assert result.json()['data']['items'][0].get('title') == 'Global filed material', result.text
        result = client.get('/api/root/context-library/' + document['id'])
        assert result.status_code == 200, result.text
        data = result.json()['data']
        assert len(data['document']['body']) == 12000 and data['next_cursor']
        result = client.get('/api/root/context-library/' + document['id'], params={'cursor': data['next_cursor']})
        assert result.status_code == 200, result.text
