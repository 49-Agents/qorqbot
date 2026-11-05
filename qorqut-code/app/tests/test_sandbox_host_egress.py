"""Sandbox host-side provider routing uses a verified first hop for overrides."""

import io
import json
from types import SimpleNamespace
from urllib.request import Request
import socket

import httpx
import pytest

from bazarlyk.background.backups import BackupDownloader
from qorqut_operations.contracts import DomainError
from bazarlyk.background.providers import BackgroundModelClient, ProviderConfig, ProviderError
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.service.egress import BusinessEgress, RequiredAccountProxy
from bazarlyk.service.local import LocalFiles
from bazarlyk.service.main import background_model_factory
from foundation_support import BUSINESS, database_at


class Sandbox:
    def __init__(self, *, proxy="http://172.18.9.2:18080", error=None):
        self.proxy, self.error, self.calls = proxy, error, []

    def account_proxy(self, business_id):
        self.calls.append(business_id)
        if self.error:
            raise self.error
        return self.proxy


def fixture(tmp_path, *, selected=True):
    database = database_at(tmp_path)
    runtime = SimpleNamespace(root=tmp_path, database=database)
    local, sandbox = LocalFiles(runtime), Sandbox()
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=root-model\n")
    (tmp_path / "first/hq").mkdir(parents=True, exist_ok=True)
    if selected:
        with database.transaction() as uow:
            SandboxStateStore().insert_intent(uow, BUSINESS)
    return database, local, sandbox


def host_config(root, *, enabled=True, shared=()):
    path = root / ".bazarlyk/sandbox-host.json"
    path.parent.mkdir(exist_ok=True)
    image = "sha256:" + "a" * 64
    path.write_text(json.dumps({"schema_version": 1, "workstation_image": image, "browser_image": image,
        "agent_api_port": 1148, "enabled": enabled, "shared_environment_keys": list(shared),
        "limits": {"agent_memory_mb": 256, "browser_memory_mb": 256, "gateway_memory_mb": 128,
                   "cpus": 1, "pids": 128, "disk_gb": 8}}))
    path.chmod(0o600)
    return path


def recording_http(monkeypatch):
    requests, options = [], []
    original = httpx.Client

    def client(**kwargs):
        options.append(dict(kwargs))
        kwargs.pop("proxy")
        return original(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"complete": True, "rows": []})),
            **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return requests, options


def test_selected_custom_model_destinations_use_gateway_before_client_for_local_private_and_foreign_hosts(tmp_path):
    _database, local, sandbox = fixture(tmp_path)
    seen = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda config, **options: seen.append((config, options)) or config)
    for destination in ("https://localhost:8443/v1", "https://10.0.0.8/v1", "https://foreign.example/v1"):
        (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=" + destination + "\nDEEPSEEK_API_KEY=business-model\n")
        model(BUSINESS)
        assert seen[-1][0].base_url == destination
        assert seen[-1][0].api_key == "business-model"
        assert seen[-1][1] == {"proxy": "http://172.18.9.2:18080"}
    assert sandbox.calls == [BUSINESS, BUSINESS, BUSINESS]


def test_selected_plain_http_model_override_is_rejected_before_gateway_or_transport(tmp_path):
    _database, local, sandbox = fixture(tmp_path)
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=http://localhost:8080/v1\nDEEPSEEK_API_KEY=business-model\n")
    invoked = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda *_args, **_kwargs: invoked.append(True))
    with pytest.raises(DomainError, match="must use HTTPS"):
        model(BUSINESS)
    assert sandbox.calls == [] and invoked == []


def test_proxied_http_model_cannot_construct_an_ambient_direct_opener(monkeypatch):
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: pytest.fail("direct opener constructed"))
    with pytest.raises(DomainError, match="must use HTTPS"):
        BackgroundModelClient(ProviderConfig("business-model", base_url="http://foreign.example/v1"),
                              proxy="http://172.18.9.2:18080")


def test_selected_default_or_exact_same_base_keeps_trusted_direct_route(tmp_path):
    _database, local, sandbox = fixture(tmp_path)
    seen = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda config, **options: seen.append((config, options)) or config)
    model(BUSINESS)
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=https://api.deepseek.com/\n")
    model(BUSINESS)
    assert [options for _config, options in seen] == [{}, {}]
    assert [config.api_key for config, _options in seen] == ["root-model", "root-model"]
    assert sandbox.calls == []


@pytest.mark.parametrize("destination", ("https://api.deepseek.com:444", "https://api.deepseek.com/v1"))
def test_selected_same_hostname_with_changed_port_or_base_is_still_a_custom_gateway_route(tmp_path, destination):
    _database, local, sandbox = fixture(tmp_path)
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=" + destination + "\nDEEPSEEK_API_KEY=business-model\n")
    seen = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda config, **options: seen.append((config, options)) or config)
    model(BUSINESS)
    assert seen[0][1] == {"proxy": "http://172.18.9.2:18080"} and sandbox.calls == [BUSINESS]


def test_selected_custom_override_never_inherits_root_key_and_stale_gateway_rejects_before_client(tmp_path):
    _database, local, sandbox = fixture(tmp_path)
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=https://foreign.example/v1\n")
    invoked = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda *_args, **_kwargs: invoked.append(True))
    with pytest.raises(DomainError, match="own credential"):
        model(BUSINESS)
    assert sandbox.calls == [BUSINESS] and invoked == []

    sandbox.error = DomainError("environment_conflict", "stale gateway")
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=https://foreign.example/v1\nDEEPSEEK_API_KEY=business-model\n")
    with pytest.raises(DomainError, match="stale gateway"):
        model(BUSINESS)
    assert invoked == []


def test_legacy_absent_intent_and_global_model_routes_remain_direct(tmp_path):
    _database, local, sandbox = fixture(tmp_path, selected=False)
    (tmp_path / "first/hq/.env").write_text("DEEPSEEK_BASE_URL=https://foreign.example/v1\n")
    seen = []
    model = background_model_factory(local, sandbox,
        client_factory=lambda config, **options: seen.append((config, options)) or config)
    model(BUSINESS)
    model(None)
    assert [options for _config, options in seen] == [{}, {}]
    assert [config.api_key for config, _options in seen] == ["root-model", "root-model"]
    assert sandbox.calls == []


def test_mandatory_urllib_proxy_ignores_no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    handler = RequiredAccountProxy("http://172.18.9.2:18080")
    request = Request("https://foreign.example/v1/chat/completions")
    handler.https_open(request)
    assert request.host == "172.18.9.2:18080" and request._tunnel_host == "foreign.example"


def test_background_https_opener_uses_gateway_socket_even_when_no_proxy_matches_everything(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    calls = []

    def refused(address, *_args, **_kwargs):
        calls.append(address)
        raise OSError("fixture connection refused")

    monkeypatch.setattr(socket, "create_connection", refused)
    client = BackgroundModelClient(ProviderConfig("business-model", base_url="https://foreign.example/v1", attempts=1),
                                   proxy="http://172.18.9.2:18080", sleep=lambda _: None)
    with pytest.raises(ProviderError, match="unavailable"):
        client.chat([{"role": "user", "content": "offline fixture"}])
    assert calls and all(address[0] == "172.18.9.2" and address[1] == 18080 for address in calls)


def pull(downloader, token_env):
    return downloader(BUSINESS, "ENDPOINT", token_env, io.BytesIO(), 5)


def test_selected_backup_pull_cannot_select_unshared_root_tokens_and_allows_shared_or_own_credentials(tmp_path, monkeypatch):
    _database, local, sandbox = fixture(tmp_path)
    host_config(tmp_path, shared=("SHARED_TOKEN",))
    (tmp_path / ".env").write_text("ROOT_TOKEN=root-only\nSHARED_TOKEN=approved-root\nBIZ_ACTOR=forged\n")
    (tmp_path / "first/hq/.env").write_text("ENDPOINT=https://backup.example/daily\nOWN_TOKEN=business-only\n")
    requests, options = recording_http(monkeypatch)
    downloader = BackupDownloader(BusinessEgress(local, sandbox).provider_environment, proxy_resolver=sandbox.account_proxy)

    with pytest.raises(DomainError, match="token is not configured"):
        pull(downloader, "ROOT_TOKEN")
    assert requests == options == [] and sandbox.calls == []

    assert pull(downloader, "SHARED_TOKEN") > 0
    assert pull(downloader, "OWN_TOKEN") > 0
    assert [request.headers["authorization"] for request in requests] == ["Bearer approved-root", "Bearer business-only"]
    assert [entry["proxy"] for entry in options] == ["http://172.18.9.2:18080", "http://172.18.9.2:18080"]
    assert sandbox.calls == [BUSINESS, BUSINESS]


@pytest.mark.parametrize("configuration", ("missing", "disabled", "invalid"))
def test_selected_backup_pull_fails_closed_when_host_configuration_is_not_usable(tmp_path, monkeypatch, configuration):
    _database, local, sandbox = fixture(tmp_path)
    (tmp_path / "first/hq/.env").write_text("ENDPOINT=https://backup.example/daily\nTOKEN=business-only\n")
    if configuration == "disabled":
        host_config(tmp_path, enabled=False)
    elif configuration == "invalid":
        path = host_config(tmp_path)
        path.write_text("not json")
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: pytest.fail("backup request constructed"))
    downloader = BackupDownloader(BusinessEgress(local, sandbox).provider_environment, proxy_resolver=sandbox.account_proxy)
    with pytest.raises(DomainError):
        pull(downloader, "TOKEN")
    assert sandbox.calls == []


def test_legacy_backup_pull_keeps_merged_environment_without_host_sandbox_configuration(tmp_path, monkeypatch):
    _database, local, sandbox = fixture(tmp_path, selected=False)
    (tmp_path / ".env").write_text("ROOT_TOKEN=legacy-root\n")
    (tmp_path / "first/hq/.env").write_text("ENDPOINT=https://backup.example/daily\n")
    requests, options = recording_http(monkeypatch)
    downloader = BackupDownloader(BusinessEgress(local, sandbox).provider_environment, proxy_resolver=lambda _business: None)
    assert pull(downloader, "ROOT_TOKEN") > 0
    assert requests[0].headers["authorization"] == "Bearer legacy-root" and options[0]["proxy"] is None
    assert sandbox.calls == []
