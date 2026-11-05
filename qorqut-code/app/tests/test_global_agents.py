"""Global roster and owner terminal isolation; no Claude/model execution."""
from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.service.main import load_terminal_broker
from bazarlyk.transport.terminals import TerminalService
from test_agent_configuration import REPO, PATHS
from test_service_transports import ORIGIN, ServiceFixture


def global_agent(service, name="learning-applier"):
    return service.fixture.create(name, "learning-applier", display_name="Learning Applier", scope={
        "kind": "ecosystem", "allowed_business_ids": [service.fixture.business_id],
        "global_capabilities": ["read_global_context", "manage_shared_instructions", "publish_roles", "publish_plugins"]})


@pytest.fixture
def fixture(tmp_path):
    service = ServiceFixture(tmp_path)
    service.applier = global_agent(service)
    with service.runtime.database.read() as uow:
        service.global_owner = service.runtime.policy.context_for(service.fixture.owner.id, business_id=None, review_business_id=None, uow=uow)
    return service


def test_rosters_use_explicit_scope_and_current_grants(fixture):
    app = fixture.runtime.application
    result = app.invoke(fixture.global_owner, "agents_list", {"scope": "global"})
    assert result.status == "ok", result.to_dict()
    assert {a["agent_id"] for a in result.data["agents"]} == {fixture.applier.id, fixture.manager.id}
    business = app.invoke(fixture.fixture.ctx, "agents_list", {})
    assert [a["agent_id"] for a in business.data["agents"]] == [fixture.agent.id]
    assert app.invoke(fixture.fixture.ctx, "agents_list", {"scope": "global"}).status == "error"
    assert app.invoke(fixture.global_owner, "agents_list", {}).status == "error"
    agent_context = fixture.runtime.resolver.invocation_context(fixture.agent)
    assert app.invoke(agent_context, "agents_list", {"scope": "global"}).status == "error"
    reader = fixture.runtime.resolver.invocation_context(fixture.manager)
    assert app.invoke(reader, "agents_list", {"scope": "global"}).status == "ok"
    with fixture.fixture.db.transaction() as uow:
        row = uow.repository("agent_profiles", business_id=None).get(fixture.manager.id)
        profile = row["profile_json"]
        profile["scope"]["global_capabilities"] = []
        uow.repository("agent_profiles", business_id=None).update(fixture.manager.id, {"profile_json": profile}, expected_revision=row["revision"])
    assert app.invoke(reader, "agents_list", {"scope": "global"}).status == "error"


class Mirror:
    cols, rows, done = 100, 30, False
    def __init__(self): self.inputs = []
    def write(self, data): self.inputs.append(data)
    def resize(self, rows, cols): self.rows, self.cols = rows, cols
    def snapshot(self): return b"isolated shell", 0
    def tail_from(self, cursor): return b"", cursor


class Broker:
    def __init__(self): self.sessions, self.mirrors, self.opened = set(), {}, []
    tmux_available = staticmethod(lambda: True)
    session_name = staticmethod(lambda slug, name: slug + "__" + name)
    session_prefix = staticmethod(lambda slug: slug + "__")
    attach_cmd = staticmethod(lambda session: ["tmux", "attach", session])
    def ensure_session(self, session, shell, cwd, **kwargs):
        self.sessions.add(session); self.opened.append((session, shell, cwd, kwargs))
    def get_or_start(self, key, *args, **kwargs): return self.mirrors.setdefault(key, Mirror())
    def list_sessions(self, prefix): return [s for s in self.sessions if s.startswith(prefix)]
    def get(self, key): return self.mirrors.get(key)
    def resize_session(self, *args): pass
    def kill_session(self, session):
        if session in self.sessions: self.sessions.remove(session); return True
        return False
    def stop(self, key): return self.mirrors.pop(key, None) is not None


def test_root_http_routes_do_not_borrow_a_business_or_inherit_its_environment(fixture):
    prepared = []
    broker = Broker()
    terminal = TerminalService(fixture.runtime, broker, service_url="http://testserver/mcp",
        version_provider=lambda: "2.1.263", preparation=lambda *args, **kwargs: prepared.append(kwargs),
        environment=lambda business: {"BUSINESS_ONLY": business["slug"]}, capture_transcripts=True)
    fixture.web.state.http_api.terminals = terminal
    path = "/api/root/terminal/" + fixture.applier.id
    with TestClient(fixture.web) as client:
        roster = client.get("/api/root/agents").json()["data"]["agents"]
        assert {a["agent_id"] for a in roster} == {fixture.applier.id, fixture.manager.id}
        assert not broker.opened and not prepared
        assert client.get("/api/root/terminals").json() == []
        assert client.post(path + "/start", json={}).status_code == 403
        assert client.post(path + "/start", json={}, headers={"Origin": "http://elsewhere"}).status_code == 403
        assert client.post(path + "/start", json={"business_id": fixture.fixture.business_id}, headers=ORIGIN).status_code == 422
        assert client.post("/api/root/terminal/" + fixture.agent.id + "/start", json={}, headers=ORIGIN).status_code == 404
        assert client.post("/api/biz/widget/terminal/" + fixture.applier.id + "/start", json={}, headers=ORIGIN).status_code == 404
        result = client.post(path + "/start", json={}, headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert broker.opened[-1][1:] == (["/bin/bash", "-i"], str(fixture.runtime.root / "agents/learning-applier"), {"env": {}, "cols": 120, "rows": 32})
        assert prepared[-1]["capture_transcripts"] is True
        assert client.get("/api/root/terminals").json()[0]["agent_id"] == fixture.applier.id
        assert client.get("/api/biz/widget/terminals").json() == []
        assert client.post(path + "/input", json={"data": "typed text"}, headers=ORIGIN).status_code == 200
        assert next(iter(broker.mirrors.values())).inputs == [b"typed text"]
        assert client.post(path + "/resize", json={"cols": 80, "rows": 24}, headers=ORIGIN).status_code == 200
        assert client.post(path + "/resize", json={"cols": 0}, headers=ORIGIN).status_code == 422
        assert client.post(path + "/stop", json={}, headers=ORIGIN).json()["ok"]
        assert client.get("/api/root/terminals").json() == []
        with fixture.runtime.database.transaction() as uow:
            row = uow.repository("agent_profiles", business_id=None).get(fixture.applier.id)
            uow.repository("agent_profiles", business_id=None).update(fixture.applier.id, {"status": "inactive"}, expected_revision=row["revision"])
        assert [a["agent_id"] for a in client.get("/api/root/agents").json()["data"]["agents"]] == [fixture.manager.id]
        assert client.post(path + "/input", json={"data": "blocked"}, headers=ORIGIN).status_code == 404


def test_root_workspace_terminal_is_a_plain_qorqut_shell(fixture, tmp_path):
    prepared = []
    broker = Broker()
    terminal_workspace = tmp_path / "qorqut"
    terminal_workspace.mkdir()
    terminal = TerminalService(fixture.runtime, broker, service_url="http://testserver/mcp",
        version_provider=lambda: "2.1.263", preparation=lambda *args, **kwargs: prepared.append(kwargs),
        global_environment=lambda: {"GLOBAL_ONLY": "yes"}, capture_transcripts=True,
        workspace=terminal_workspace)
    fixture.web.state.http_api.terminals = terminal
    path = "/api/root/workspace-terminal"
    with TestClient(fixture.web) as client:
        assert client.post(path + "/start", json={}).status_code == 403
        result = client.post(path + "/start", json={}, headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert result.json()["workspace"] == str(terminal_workspace)
        assert broker.opened[-1] == ("global__workspace", ["/bin/bash", "-i"],
            str(terminal_workspace), {"env": {"GLOBAL_ONLY": "yes"}, "cols": 120, "rows": 32})
        assert prepared == []
        assert client.get("/api/root/terminals").json() == []
        assert client.post(path + "/input", json={"data": "pwd"}, headers=ORIGIN).status_code == 200
        assert next(iter(broker.mirrors.values())).inputs == [b"pwd"]
        assert client.post(path + "/resize", json={"cols": 90, "rows": 25}, headers=ORIGIN).status_code == 200
        assert client.post(path + "/stop", json={}, headers=ORIGIN).json()["ok"]


def test_root_workspace_terminal_slots_are_separate_shells(fixture, tmp_path):
    broker = Broker()
    terminal_workspace = tmp_path / "qorqut"
    terminal_workspace.mkdir()
    fixture.web.state.http_api.terminals = TerminalService(fixture.runtime, broker, service_url="http://testserver/mcp",
        version_provider=lambda: "2.1.263", global_environment=lambda: {}, workspace=terminal_workspace)
    with TestClient(fixture.web) as client:
        for path, session in (("/api/root/workspace-terminal", "global__workspace"),
                              ("/api/root/workspace-terminal/2", "global__workspace-2"),
                              ("/api/root/workspace-terminal/3", "global__workspace-3")):
            result = client.post(path + "/start", json={}, headers=ORIGIN)
            assert result.status_code == 200, result.text
            assert broker.opened[-1][0] == session
        assert len(broker.mirrors) == 3
        assert client.post("/api/root/workspace-terminal/4/start", json={}, headers=ORIGIN).status_code == 404
        assert client.post("/api/root/workspace-terminal/1/start", json={}, headers=ORIGIN).status_code == 404

def test_root_terminal_rechecks_owner_and_workspace_before_preparing(fixture, tmp_path):
    prepared = []
    terminal = TerminalService(fixture.runtime, Broker(), service_url="http://testserver",
        version_provider=lambda: "2.1.263", preparation=lambda *args, **kwargs: prepared.append(kwargs))
    # A business owner who is not the installation owner cannot open root terminals.
    with fixture.runtime.database.transaction() as uow:
        other = new_id()
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Business owner"})
        uow.repository("business_memberships", business_id=fixture.fixture.business_id).insert({"principal_id": other, "is_owner": True})
    with fixture.runtime.database.read() as uow:
        owner = fixture.runtime.policy.context_for(other, business_id=None, review_business_id=None, uow=uow)
    with pytest.raises(DomainError, match="installation owner"):
        terminal.open(owner, None, fixture.applier.id)
    with pytest.raises(DomainError):
        terminal.open(fixture.runtime.resolver.invocation_context(fixture.applier), None, fixture.applier.id)
    workspace = fixture.runtime.root / "agents/learning-applier"
    workspace.rename(workspace.with_name("saved-workspace"))
    workspace.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError): terminal.open(fixture.global_owner, None, fixture.applier.id)
    assert not prepared


def test_global_tmux_shell_survives_mirror_disconnect(fixture, tmp_path, monkeypatch):
    from bazarlyk.bootstrap import preparation
    # Release code has no adjacent venv; attachment must use the service runtime.
    release_module = tmp_path / "release/bazarlyk/bootstrap/preparation.py"
    monkeypatch.setattr(preparation, "__file__", str(release_module))
    broker = load_terminal_broker(PATHS)
    if not broker.tmux_available(): pytest.skip("tmux is unavailable")
    broker.TMUX_SOCK = "global-agent-test-" + new_id()
    config = tmp_path / "tmux.conf"; config.write_text(broker.TMUX_CONF)
    monkeypatch.setattr(broker, "_conf_path", lambda: str(config))
    terminal = TerminalService(fixture.runtime, broker, service_url="http://testserver/mcp", version_provider=lambda: "2.1.263")
    try:
        mirror, _, row = terminal.open(fixture.global_owner, None, fixture.applier.id)
        key, session, cwd = terminal.binding(None, row)
        assert Path(cwd) == fixture.runtime.root / "agents/learning-applier"
        assert broker.session_alive(session)
        assert broker.session_name("global", "learning-applier") != session
        broker.stop(key)
        assert broker.session_alive(session)
        attached, _, _ = terminal.open(fixture.global_owner, None, fixture.applier.id)
        assert attached is not mirror
        assert terminal.listing(fixture.global_owner, None)[0]["agent_id"] == fixture.applier.id
    finally:
        for key in broker.keys(): broker.stop(key)
        broker.tmux("kill-server")


def test_global_stream_ends_when_agent_is_deactivated(fixture, monkeypatch):
    import bazarlyk.transport.terminals as module
    clock = iter(range(0, 100, 6))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    terminal = TerminalService(fixture.runtime, Broker(), service_url="http://testserver/mcp",
        version_provider=lambda: "2.1.263", preparation=lambda *args, **kwargs: None)
    async def disconnected(): return False
    request = SimpleNamespace(method="GET", headers={}, is_disconnected=disconnected)
    async def check():
        response = await terminal.route(request, fixture.global_owner, None, ["terminal", fixture.applier.id, "stream"])
        stream = response.body_iterator
        assert (await anext(stream)).startswith(b"event: replay")
        assert (await anext(stream)).startswith(b"data: ")
        with fixture.runtime.database.transaction() as uow:
            repo = uow.repository("agent_profiles", business_id=None)
            row = repo.get(fixture.applier.id)
            repo.update(fixture.applier.id, {"status": "inactive"}, expected_revision=row["revision"])
        assert (await anext(stream)).startswith(b"event: done")
        with pytest.raises(StopAsyncIteration): await anext(stream)
    asyncio.run(check())
