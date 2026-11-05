import io

import httpx
import pytest

from bazarlyk.background.backups import BackupDownloader
from qorqut_operations.contracts import DomainError


def test_account_clients_select_business_proxy_and_fail_before_network_when_unavailable(monkeypatch):
    selected, requests, options = [], [], []
    unavailable = [False]
    def route(business):
        selected.append(business)
        if unavailable[0]:
            raise DomainError('environment_unavailable', 'Business gateway unavailable')
        return 'http://172.18.9.2:18080'
    original = httpx.Client
    def client(**kwargs):
        options.append(dict(kwargs))
        # Observe the actual client configuration; inject only the remote server.
        kwargs.pop('proxy')
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'complete': True, 'rows': []})
        return original(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, 'Client', client)
    download = BackupDownloader(lambda _: {'URL': 'https://account.example/daily', 'TOKEN': 'secret'}, proxy_resolver=route)
    assert download('one', 'URL', 'TOKEN', io.BytesIO(), 5) > 0
    assert selected == ['one'] and options[0]['proxy'] == 'http://172.18.9.2:18080'
    assert options[0]['trust_env'] is False and options[0]['follow_redirects'] is False

    unavailable[0] = True
    with pytest.raises(DomainError):
        download('one', 'URL', 'TOKEN', io.BytesIO(), 5)
    assert len(requests) == 1
