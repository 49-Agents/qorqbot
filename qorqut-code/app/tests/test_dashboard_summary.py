from dataclasses import replace
from qorqut_operations.contracts import new_id
from foundation_support import BUSINESS, HUMAN, OTHER, context, insert_action
from test_business_progress import fixture


def test_small_summary_counts_scope_and_task_visibility(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as u:
        u.repository('installation').insert({'id':'service','installation_id':new_id(),'owner_principal_id':HUMAN})
        u.connection.execute('DELETE FROM business_memberships WHERE business_id=?',(OTHER,))
        for biz in [BUSINESS, OTHER]:
            for status, archived, snoozed in [('open',0,0),('done',0,0),('open',1,0),('open',0,1)]:
                task = new_id()
                u.repository('tasks',business_id=biz).insert({'id':task,'title':'Task','status':status,'archived':archived,'created_by':HUMAN,'snooze_indefinite':snoozed})
                u.repository('relationships',business_id=biz).insert({'id':new_id(),'from_type':'task','from_id':task,'to_type':'person','to_id':HUMAN,'role':'assigned_to'})
            for at in ['2026-09-13T01:00:00Z','2026-09-12T02:00:00Z','2026-01-01T01:00:00Z','2026-09-14T01:00:00Z']:
                insert_action(u, biz, new_id(), summary='Action', actor_id=HUMAN, occurred_at=at)
    ctx = replace(context(global_capabilities=frozenset({'read_global_context'})),business_id=None,review_business_id=None)
    result = app.invoke(ctx,'dashboard_summary_get',{})
    assert result.status == 'ok', result.to_dict()
    assert len(result.data['items']) == 1
    item = result.data['items'][0]
    assert item['business_id'] == BUSINESS and item['tasks'] == 1
    assert len(item['activity']) == 14 and sum(r['count'] for r in item['activity']) == 2
    assert item['signoffs'] == 0 and item['requests'] == 0
    assert not any(key in item for key in ['notes','sessions','logs','tasks_list'])
    with db.transaction() as u: u.connection.execute('DELETE FROM installation')
    assert app.invoke(ctx,'dashboard_summary_get',{}).status == 'error'


def test_summary_routes_and_light_agent_roster(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_service_transports import ServiceFixture, ORIGIN
    from bazarlyk.transport.compatibility import DashboardCompatibility
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        response = client.post('/api/root/operations/dashboard_summary_get', json={}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert isinstance(response.json()['data']['items'], list)
        response = client.post('/api/biz/widget/operations/context_navigation_get', json={}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        counts = response.json()['data']['counts']
        graph = client.post('/api/biz/widget/operations/context_graph_get', json={}, headers=ORIGIN).json()['data']
        assert counts == {kind:sum(n['type'] == kind for n in graph['nodes']) for kind in ('story','table','product','person')}
        def no_details(*args):
            raise AssertionError('Roster must not load agent plans, notes or session history')
        monkeypatch.setattr(DashboardCompatibility, 'agent_view', no_details)
        response = client.get('/api/biz/widget/agents?summary=1')
        assert response.status_code == 200, response.text
        assert response.json()
