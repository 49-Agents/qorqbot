import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.service.local import LocalFiles
from test_environment_get import reader
from test_environment_discovery import service, invoke


def test_registered_roles_control_discovery_values_and_terminals(service):
    root = service.runtime.root
    (root / ".env.private.json").write_text(json.dumps({
        "PRIVATE": {"value": "synthetic-secret", "allowed_roles": ["ordubashy"]},
        "SHARED": {"value": "private-replacement", "allowed_roles": ["ordubashy"]}}))
    (root / ".env").write_text("SHARED=ordinary\n")
    agent = reader(service, global_scope=True)
    assert invoke(service, agent, "environment_get", {"key": "PRIVATE"}).data["value"] == "synthetic-secret"
    assert invoke(service, agent, args={"names": ["PRIVATE"]}).data["items"][0]["configured"]
    local = LocalFiles(service.runtime)
    profile, _ = service.runtime.resolver.profile(agent)
    assert local.agent_environment(None, profile)["SHARED"] == "private-replacement"
    other = SimpleNamespace(role=SimpleNamespace(id="founder"))
    assert local.agent_environment(None, other) == {"SHARED": "ordinary"}
    assert "PRIVATE" not in local.agent_environment(service.fixture.business_id, profile)
    assert "PRIVATE" not in local.environment()
    assert not invoke(service, service.agent, args={"names": ["PRIVATE"]}).data["items"][0]["configured"]
    (service.fixture.hq / ".env.private.json").write_text(json.dumps({
        "LOCAL_PRIVATE": {"value": "hidden-local", "allowed_roles": ["ordubashy"]}}))
    business_reader = reader(service)
    assert not invoke(service, business_reader, "environment_get", {"key": "LOCAL_PRIVATE"}).data["found"]
    assert not invoke(service, business_reader, args={"names": ["LOCAL_PRIVATE"]}).data["items"][0]["configured"]
    owner_read = service.runtime.application.invoke(service.fixture.ctx, "environment_get", {"key": "LOCAL_PRIVATE"})
    assert owner_read.data["value"] == "hidden-local"
    # Re-read policy on each invocation, including an already-resolved context.
    ctx = service.runtime.resolver.invocation_context(agent)
    (root / ".env.private.json").write_text(json.dumps({
        "PRIVATE": {"value": "synthetic-secret", "allowed_roles": []}}))
    assert not service.runtime.application.invoke(ctx, "environment_get", {"key": "PRIVATE"}).data["found"]


@pytest.mark.parametrize("content", ['[]', '{', '{"KEY":{"value":"x","allowed_roles":"ordubashy"}}',
                                     '{"BIZ_ACTOR":{"value":"x","allowed_roles":[]}}'])
def test_invalid_private_configuration_fails_closed(tmp_path, content):
    local = LocalFiles(SimpleNamespace(root=tmp_path))
    (tmp_path / ".env.private.json").write_text(content)
    with pytest.raises(DomainError):
        local.private_environment(role="ordubashy")


def test_private_settings_reject_links(tmp_path):
    local = LocalFiles(SimpleNamespace(root=tmp_path))
    (tmp_path / "target").write_text('{}')
    (tmp_path / ".env.private.json").symlink_to(tmp_path / "target")
    with pytest.raises(DomainError):
        local.private_environment(role="ordubashy")
