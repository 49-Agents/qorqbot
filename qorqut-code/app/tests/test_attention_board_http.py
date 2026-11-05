from starlette.testclient import TestClient
from qorqut_operations.contracts import new_id
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from host_foundation_support import build_instance, register_sources, create_registered_global, select_coordinator
ORIGIN = {'Origin':'http://testserver'}

def root_operation(client, name, args):
    response = client.post('/api/root/operations/'+name, headers=ORIGIN, json=args)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ok', response.text
    return response.json()['data']


def test_attention_board_owner_http_lifecycle(tmp_path):
    host, _ = build_instance(tmp_path)
    register_sources(host, host.business, 'first')
    agent = select_coordinator(host, create_registered_global(host, 'ordubashy'))
    auth = Authentication(host.database, host.paths)
    dashboard = tmp_path/'dashboard'
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    app = create_app(host.runtime, dashboard_root=dashboard, origins=['http://testserver'], authentication=auth, manage_lifecycle=False)
    with TestClient(app) as client:
        client.cookies.set('bazarlyk_session', auth.issue_browser())
        args = {'scope':'global', 'agent_id':agent}
        assert root_operation(client, 'attention_get', args)['items'] == []
        assert 'workers' in root_operation(client, 'workers_list', {'scope': 'global'})
        task = root_operation(client, 'tasks_create', {'scope': 'global', 'title': 'Board HTTP test', 'idempotency_key': new_id()})['task']
        root_operation(client, 'attention_add', {**args, 'task': {'type':'task', 'id':task['id'], 'business_id':None}, 'idempotency_key':new_id()})
        item = root_operation(client, 'attention_get', args)['items'][0]
        assert item['task_ref']['id'] == task['id']
        root_operation(client, 'attention_remove', {**args, 'membership_id': item['membership_id'], 'expected_membership_revision':item['membership_revision'], 'idempotency_key':new_id()})
        assert root_operation(client, 'attention_get', args)['items'] == []
        assert client.post('/api/root/operations/attention_get', json=args).status_code == 403
        denied = client.post('/api/root/operations/attention_get', headers=ORIGIN, json={**args,'business_id':new_id()})
        assert denied.status_code == 403
