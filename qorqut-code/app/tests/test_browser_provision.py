import json

import pytest

from bazarlyk.browser.provision import provision, split_proxy
from qorqut_operations.contracts import DomainError
from test_environment_discovery import service, invoke
from test_environment_get import reader


@pytest.mark.parametrize("url", ["http://host", "http://u@host:80", "http://u:p@host:80/path",
    "http://u:p@host:80?secret=x", "http://u%3Ax:p@host:80", "http://u:p%0a@host:80",
    "http://u:p@ho\nst:80", "", "direct://host:80"])
def test_invalid_input_is_sanitized(url):
    with pytest.raises(DomainError) as error:
        split_proxy(url)
    assert "secret=x" not in str(error.value)


def test_split_authenticated_and_ip_authorized():
    assert split_proxy("http://u%40tenant:p%3Aword@proxy.example:80\n") == (
        "http://proxy.example:80", {"username": "u@tenant", "password": "p:word"})
    assert split_proxy("socks5://[::1]:1080") == ("socks5://[::1]:1080", {"username": "", "password": ""})


def test_owner_provision_is_repeatable_private_and_does_not_start_browser(service):
    runtime = service.runtime
    with runtime.database.read() as uow:
        original = uow.repository("businesses").get(service.fixture.business_id)
    value = provision(runtime, original["slug"], "http://private-user:private-password@proxy.example:80")
    repeated = provision(runtime, original["slug"], "http://private-user:private-password@proxy.example:80")
    assert value == repeated and value["started"] is False
    with runtime.database.read() as uow:
        after = uow.repository("businesses").get(service.fixture.business_id)
        assert after["revision"] == original["revision"] + 1
        assert {k: v for k, v in after["config_json"].items() if k != "browser"} == original["config_json"]
    secret = runtime.root / ".bazarlyk/secrets/browser-proxy" / (value["business_id"] + ".json")
    assert secret.stat().st_mode & 0o777 == 0o600
    assert not (runtime.root / ".bazarlyk/browsers" / value["business_id"]).exists()
    assert "private-password" not in json.dumps(value)
    # Even an explicitly granted Settings reader cannot retrieve relay secrets.
    agent = reader(service)
    for key in ("PROXY_PASSWORD", "browser-proxy", "password"):
        result = invoke(service, agent, "environment_get", {"key": key})
        assert "private-password" not in json.dumps(result.to_dict())
    for changed in ("http://private-user:new@proxy.example:80", "http://u:p@other.example:80"):
        with pytest.raises(DomainError):
            provision(runtime, original["slug"], changed)
    assert json.loads(secret.read_text())["password"] == "private-password"


def test_unknown_business_writes_no_credentials(service):
    with pytest.raises(DomainError):
        provision(service.runtime, "unknown-business", "http://u:p@proxy.example:80")
    assert not (service.runtime.root / ".bazarlyk/secrets/browser-proxy").exists()
