"""Shared defaults, explicit overrides, and owner-only secret settings."""
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError
from bazarlyk.service.local import LocalFiles
from bazarlyk.transport.terminals import TerminalService
from test_global_agents import Broker, global_agent
from test_service_transports import ORIGIN, ServiceFixture


@pytest.fixture
def service(tmp_path):
    return ServiceFixture(tmp_path, full=True)


def test_global_defaults_override_removal_and_persistence(service):
    local = LocalFiles(service.runtime)
    shared = [{"key": "SHARED_KEY", "value": "global-value"}, {"key": "GLOBAL_ONLY", "value": "shared"}]
    with TestClient(service.web) as client:
        saved = client.post("/api/root/env", json={"pairs": shared}, headers=ORIGIN)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert (service.runtime.root / ".env").stat().st_mode & 0o777 == 0o600
        assert client.get("/api/biz/widget/env").json() == {"pairs": [], "inherited": shared}
        override = [{"key": "SHARED_KEY", "value": "business-value"}, {"key": "BUSINESS_ONLY", "value": "private"}]
        assert client.post("/api/biz/widget/env", json={"pairs": override}, headers=ORIGIN).status_code == 200
        assert local.environment(service.fixture.business_id) == {
            "SHARED_KEY": "business-value", "GLOBAL_ONLY": "shared", "BUSINESS_ONLY": "private"}
        assert local.environment() == {"SHARED_KEY": "global-value", "GLOBAL_ONLY": "shared"}
        assert client.post("/api/biz/widget/env", json={"pairs": []}, headers=ORIGIN).status_code == 200
        assert LocalFiles(service.runtime).environment(service.fixture.business_id) == local.environment()
        shared[0]["value"] = "rotated"
        client.post("/api/root/env", json={"pairs": shared}, headers=ORIGIN)
        assert local.environment(service.fixture.business_id)["SHARED_KEY"] == "rotated"


def test_invalid_global_settings_leave_existing_secrets_untouched(service):
    with TestClient(service.web) as client:
        original = [{"key": "SHARED", "value": "original"}]
        client.post("/api/root/env", json={"pairs": original}, headers=ORIGIN)
        for pairs in ([{"key": "BIZ_ACTOR", "value": "human:owner"}],
                      [{"key": "BAD-KEY", "value": "x"}], [{"key": "KEY", "value": "x\ny"}], "invalid"):
            assert client.post("/api/root/env", json={"pairs": pairs}, headers=ORIGIN).status_code == 422
        assert client.get("/api/root/env").json()["pairs"] == original
        assert client.post("/api/root/env", json={"pairs": []}).status_code == 403


def test_the_retired_named_desktop_endpoint_is_gone(service):
    with TestClient(service.web) as client:
        assert client.get("/api/root/desktop-servers/marketing-agent-remote-control-desktop-server").status_code == 404
        assert not hasattr(LocalFiles(service.runtime), "desktop_server")


def test_owner_checks_and_global_symlink_rejection(service, tmp_path):
    local = LocalFiles(service.runtime)
    with service.runtime.database.read() as uow:
        agent = service.runtime.policy.context_for(service.manager.id, business_id=service.fixture.business_id, review_business_id=service.fixture.business_id, uow=uow)
        owner = service.runtime.policy.context_for(service.fixture.owner.id, business_id=None, review_business_id=None, uow=uow)
    for context in (agent, replace(agent, business_id=None)):
        for action in (lambda: local.environment_settings(context), lambda: local.replace_environment(context, [])):
            with pytest.raises(DomainError):
                action()
    external = tmp_path / "external"
    external.write_text("SECRET=keep\n")
    (service.runtime.root / ".env").symlink_to(external)
    with pytest.raises(DomainError):
        local.environment_settings(owner)
    with pytest.raises(DomainError):
        local.replace_environment(owner, [])
    assert external.read_text() == "SECRET=keep\n"


def test_global_and_business_terminal_environment_are_distinct(service):
    local = LocalFiles(service.runtime)
    (service.runtime.root / ".env").write_text("SHARED=global\nBIZ_ACTOR=untrusted\n")
    (service.fixture.hq / ".env").write_text("SHARED=business\nPRIVATE=business-only\n")
    (service.runtime.root / ".env.private.json").write_text(
        '{"ROLE_PRIVATE":{"value":"role-only","allowed_roles":["learning-applier"]}}')
    agent = global_agent(service)
    broker = Broker()
    terminal = TerminalService(service.runtime, broker, service_url="http://testserver/mcp",
        environment=local.environment, global_environment=local.environment, version_provider=lambda: "2.1.81",
        preparation=lambda *args, **kwargs: None, agent_environment=local.agent_environment)
    with service.runtime.database.read() as uow:
        owner = service.runtime.policy.context_for(service.fixture.owner.id, business_id=None, review_business_id=None, uow=uow)
        business = dict(uow.repository("businesses").get(service.fixture.business_id))
    terminal.open(owner, None, agent.id)
    assert broker.opened[-1][-1]["env"] == {"SHARED": "global", "ROLE_PRIVATE": "role-only"}
    terminal.open(service.fixture.ctx, business, service.agent.id)
    assert broker.opened[-1][-1]["env"] == {"SHARED": "business", "PRIVATE": "business-only"}


def test_model_factory_inherits_global_defaults_and_business_overrides(service, monkeypatch):
    from bazarlyk.service.main import assemble
    from test_agent_configuration import REPO, PATHS
    captured = {}
    monkeypatch.setattr("bazarlyk.background.integration.attach_background",
                        lambda runtime, programs, **kwargs: captured.update(kwargs))
    monkeypatch.setattr("bazarlyk.service.main.BackgroundModelClient", lambda config: config)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "machine-default")
    (service.runtime.root / ".env").write_text("DEEPSEEK_API_KEY=global-key\n")
    (service.fixture.hq / ".env").write_text("DEEPSEEK_API_KEY=business-key\n")
    assemble(service.fixture.root, REPO, paths=PATHS, origins=["http://testserver"], endpoint="http://testserver/mcp",
             database=service.fixture.db, broker=Broker(), nightly_maintenance=None)
    factory = captured["model_client_factory"]
    assert factory(None).api_key == "global-key"
    assert factory(service.fixture.business_id).api_key == "business-key"
    (service.fixture.hq / ".env").write_text("")
    assert factory(service.fixture.business_id).api_key == "global-key"
    (service.runtime.root / ".env").write_text("DEEPSEEK_API_KEY=rotated\n")
    assert factory(None).api_key == "rotated"
