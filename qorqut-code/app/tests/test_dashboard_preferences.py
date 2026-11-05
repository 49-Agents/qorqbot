from concurrent.futures import ThreadPoolExecutor

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.dashboard_preferences import pins


def test_pins_persist_and_old_browser_does_not_resurrect_unpinned_agent(tmp_path):
    assert pins(tmp_path, 'owner') == {'initialized': False, 'pinned_agents': []}
    assert pins(tmp_path, 'owner', {'initialize': ['a', 'a']})['pinned_agents'] == ['a']
    pins(tmp_path, 'owner', {'agent_id': 'a', 'pinned': False})
    assert pins(tmp_path, 'owner', {'initialize': ['a']}) == {'initialized': True, 'pinned_agents': []}
    pins(tmp_path, 'owner', {'agent_id': '__qorqut_workspace_terminal__', 'pinned': True})
    assert pins(tmp_path, 'owner')['pinned_agents'] == ['__qorqut_workspace_terminal__']
    assert not pins(tmp_path, 'different-owner')['initialized']


def test_concurrent_pin_writes_preserve_each_other_and_retries(tmp_path):
    with ThreadPoolExecutor() as executor:
        list(executor.map(lambda i: pins(tmp_path, 'owner', {'agent_id': str(i), 'pinned': True}), range(20)))
    pins(tmp_path, 'owner', {'agent_id': '1', 'pinned': True})
    assert set(pins(tmp_path, 'owner')['pinned_agents']) == {str(i) for i in range(20)}
    assert len(pins(tmp_path, 'owner')['pinned_agents']) == 20
    with pytest.raises(DomainError): pins(tmp_path, 'owner', {'agent_id': 'a', 'pinned': 'yes'})
    assert len(pins(tmp_path, 'owner')['pinned_agents']) == 20


def test_owner_http_pin_route_rejects_bearer_credentials(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ORIGIN, ServiceFixture
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.background.start = lambda: None
    try:
        with TestClient(service.web) as client:
            service.login(client)
            route = '/api/root/agent-pins'
            response = client.post(route, headers=ORIGIN, json={'agent_id': 'saved-agent', 'pinned': True})
            assert response.status_code == 200, response.text
            assert response.json()['data']['pinned_agents'] == ['saved-agent']
            assert client.get(route, headers=ORIGIN).json()['data']['pinned_agents'] == ['saved-agent']
            assert response.headers['cache-control'] == 'private, no-store'
            for method in ('get', 'post'):
                kwargs = {'json': {'initialize': []}} if method == 'post' else {}
                response = getattr(client, method)(route, headers={**ORIGIN, 'Authorization': 'Bearer invalid'}, **kwargs)
                assert response.status_code == 403
    finally:
        service.runtime.stop()
