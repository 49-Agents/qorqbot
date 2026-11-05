from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import re
import socket
import subprocess
import time

import pytest
from qorqut_operations.contracts import DomainError, Principal
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.docker import Docker, Ownership
from bazarlyk.sandbox.listener import BusinessAgentListeners
from bazarlyk.sandbox.readiness import AgentReadiness, _PROBE
from bazarlyk.sandbox.runtime import RunningEnvironment
from bazarlyk.sandbox.tmux import DockerTmuxBroker


BUSINESS = "00000000-0000-4000-8000-000000000001"
CONTAINER = "a" * 64

_MCP_CHECK = r'''
import json, sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4
from bazarlyk.bootstrap.client import auth_headers, load_registration

opener = build_opener(ProxyHandler({}))
payload = json.load(sys.stdin)
base = payload["endpoint"].rstrip("/")

def call(authorization, path, method, body=None, session=None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = Request(base + path, data=data, method=method, headers={
        "Authorization": authorization, "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "Host": base.removeprefix("http://")})
    if session:
        request.add_header("Mcp-Session-Id", session)
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read(1_000_001)
    except HTTPError as error:
        return error.code, dict(error.headers), error.read(1_000_001)
    except (OSError, URLError):
        return 599, {}, b""

def initialize(registration):
    authorization = auth_headers(load_registration(Path(registration)))["Authorization"]
    status, headers, data = call(authorization, "/mcp", "POST", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "qorqut-readiness-mutation", "version": "1"}}})
    if status != 200:
        raise SystemExit(20)
    session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
    if not session:
        raise SystemExit(21)
    status, _, _ = call(authorization, "/mcp", "POST", {
        "jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    if status not in (200, 202):
        raise SystemExit(22)
    return authorization, session

registrations = payload["registrations"]
first = initialize(registrations[0])
second = initialize(registrations[1])
for (authorization, session), expected in zip((first, second), payload["agents"]):
    status, _, data = call(authorization, "/mcp", "POST", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "tasks_create", "arguments": {
            "title": "Native readiness attribution", "idempotency_key": str(uuid4())}}}, session)
    if status != 200:
        raise SystemExit(23)
    result = json.loads(data).get("result", {})
    if result.get("isError") or not result.get("content"):
        raise SystemExit(24)
    created = json.loads(result["content"][0]["text"])
    if created.get("status") != "ok" or created.get("data", {}).get("task", {}).get("created_by") != expected:
        raise SystemExit(25)
status, _, _ = call(second[0], "/mcp", "POST", {
    "jsonrpc": "2.0", "id": 3, "method": "tools/list"}, first[1])
if status not in (401, 403, 404):
    raise SystemExit(26)
for authorization, session in (first, second):
    call(authorization, "/mcp", "DELETE", None, session)
'''


class Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params):
        return SimpleNamespace(fetchall=lambda: self.rows)


class Read:
    def __init__(self, connection):
        self.connection, self.active = connection, False

    def __enter__(self):
        self.active = True
        return SimpleNamespace(connection=self.connection)

    def __exit__(self, *args):
        self.active = False
        return False


class Database:
    def __init__(self, rows):
        self.rows, self.reads = rows, []

    def read(self):
        handle = Read(Connection(self.rows))
        self.reads.append(handle)
        return handle


class Resolver:
    def __init__(self, database):
        self.database = database

    def profile(self, principal, *, connection=None):
        return SimpleNamespace(agent_id=principal.id), {}


class FakeDocker:
    def __init__(self, ownership, paths, rows):
        self.ownership, self.paths, self.rows = ownership, paths, rows
        self.name = paths.resource_prefix + "-g3-agent"
        self.probes = []

    def owned(self, kind, identity, ownership):
        assert ownership == self.ownership
        if kind == "container" and identity in {self.name, CONTAINER}:
            return {"Id": CONTAINER, "Config": {"Labels": {**ownership.labels,
                    "qorqut.role": "agent", "qorqut.generation": "3"}},
                    "HostConfig": {"ReadonlyRootfs": True},
                    "State": {"Running": True}}
        return None

    def exec(self, container_id, ownership, *args, input=None, timeout=90):
        self.probes.append((args, input))
        if args[:1] == ("/opt/qorqut/venv/bin/python",):
            return "QORQUT_STATUS:1"
        if args[:2] == ("sh", "-c"):
            return "QORQUT_STATUS:1"
        if len(args) >= 2 and args[:2] == ("/bin/sh", "-c"):
            return "QORQUT_STATUS:1"
        return ""


def make(tmp_path):
    root = tmp_path.resolve()
    workspace = root / "business"
    (workspace / "hq").mkdir(parents=True)
    rows = [{"agent_id": f"00000000-0000-4000-8000-00000000000{index}",
             "workspace": str(workspace / "hq" / "agents" / str(index))} for index in (1, 2)]
    for row in rows:
        Path(row["workspace"]).mkdir(parents=True)
    business = {"id": BUSINESS, "workspace": str(workspace), "hq_path": str(workspace / "hq")}
    paths = BusinessPaths.registered(root, business)
    ownership = Ownership(hashlib.sha256(str(root).encode()).hexdigest()[:20], BUSINESS)
    docker = FakeDocker(ownership, paths, rows)
    config = SimpleNamespace(enabled=True)
    return AgentReadiness(Resolver(Database(rows)), docker=docker), business, paths, config, docker


def assert_no_read_transaction(readiness):
    assert not any(handle.active for handle in readiness.resolver.database.reads)


def test_readiness_prepares_and_probes_each_active_registration(tmp_path, monkeypatch):
    readiness, business, paths, config, docker = make(tmp_path)
    prepared, probed = [], []

    def prepare_check(resolver, principal, **kwargs):
        assert_no_read_transaction(readiness)
        prepared.append((principal, kwargs))
    monkeypatch.setattr("bazarlyk.sandbox.readiness.prepare_agent", prepare_check)
    monkeypatch.setattr(readiness, "_probe",
        lambda broker, endpoint, registration: probed.append((broker, endpoint, registration)))
    environment = SimpleNamespace(workstation_id=CONTAINER, generation=3, agent_api_url="http://172.20.0.1:1148")
    assert readiness(business, paths, config, environment) is True
    assert [principal.id for principal, _ in prepared] == [
        "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    assert all(kwargs["interpreter"] == "/opt/qorqut/venv/bin/python" for _, kwargs in prepared)
    assert all(kwargs["endpoint"] == environment.agent_api_url for _, kwargs in prepared)
    assert len(probed) == 2 and all(item[0].generation == 3 for item in probed)


def test_disabled_configuration_refuses_readiness(tmp_path):
    readiness, business, paths, config, _ = make(tmp_path)
    config.enabled = False
    with pytest.raises(DomainError) as failure:
        readiness(business, paths, config, SimpleNamespace(workstation_id=CONTAINER, generation=3,
            agent_api_url="http://172.20.0.1:1148"))
    assert getattr(failure.value, "code", None) == "configuration_missing"


def test_repeated_health_reuses_versions_but_rechecks_current_registrations(tmp_path, monkeypatch):
    readiness, business, paths, config, docker = make(tmp_path)
    version_calls, prepared, probed = [], [], []
    def versions(broker):
        version_calls.append(broker.container_id)
        return {"claude-code": "2.1.269", "codex": "0.154.0"}
    monkeypatch.setattr(DockerTmuxBroker, "installed_versions", versions)
    monkeypatch.setattr("bazarlyk.sandbox.readiness.prepare_agent", lambda resolver, principal, **kw: prepared.append(principal.id))
    monkeypatch.setattr(readiness, "_probe", lambda *args: probed.append(args[-1]))
    environment = SimpleNamespace(workstation_id=CONTAINER, generation=3, agent_api_url="http://172.20.0.1:1148")
    assert readiness(business, paths, config, environment)
    remaining = readiness.resolver.database.rows[0]
    readiness.resolver.database.rows = [remaining]
    assert readiness(business, paths, config, environment)
    assert version_calls == [CONTAINER]
    assert prepared == [BUSINESS, "00000000-0000-4000-8000-000000000002", remaining["agent_id"]]
    assert len(probed) == 3
    # Cached versions cannot make a stopped or foreign observed target ready.
    monkeypatch.setattr(docker, "owned", lambda *args: None)
    with pytest.raises(DomainError, match="not running"):
        readiness(business, paths, config, environment)
    assert len(probed) == 3


@pytest.mark.parametrize("replacement,generation", [("b" * 64, 3), (CONTAINER, 4)])
def test_versions_are_reprobed_for_a_different_container_or_generation(tmp_path, monkeypatch, replacement, generation):
    readiness, business, paths, config, docker = make(tmp_path)
    calls = []
    def versions(broker):
        calls.append((broker.container_id, broker.generation))
        return {"claude-code": "2.1.269", "codex": "0.154.0"}
    monkeypatch.setattr(DockerTmuxBroker, "installed_versions", versions)
    monkeypatch.setattr("bazarlyk.sandbox.readiness.prepare_agent", lambda *args, **kw: None)
    monkeypatch.setattr(readiness, "_probe", lambda *args: None)
    env = SimpleNamespace(workstation_id=CONTAINER, generation=3, agent_api_url="http://172.20.0.1:1148")
    assert readiness(business, paths, config, env)
    env.workstation_id, env.generation = replacement, generation
    monkeypatch.setattr(docker, "owned", lambda kind, identity, owner: {
        "Id": identity, "State": {"Running": True},
        "Config": {"Labels": {**owner.labels, "qorqut.role": "agent", "qorqut.generation": str(generation)}}})
    assert readiness(business, paths, config, env)
    assert calls == [(CONTAINER, 3), (replacement, generation)]


def test_mutable_container_versions_are_never_reused(tmp_path, monkeypatch):
    readiness, business, paths, config, docker = make(tmp_path)
    calls = []
    def versions(broker):
        calls.append(broker.container_id)
        return {"claude-code": "2.1.269", "codex": "0.154.0"}
    monkeypatch.setattr(DockerTmuxBroker, "installed_versions", versions)
    monkeypatch.setattr("bazarlyk.sandbox.readiness.prepare_agent", lambda *args, **kw: None)
    monkeypatch.setattr(readiness, "_probe", lambda *args: None)
    observed = docker.owned("container", CONTAINER, docker.ownership)
    observed["HostConfig"]["ReadonlyRootfs"] = False
    monkeypatch.setattr(docker, "owned", lambda *args: observed)
    env = SimpleNamespace(workstation_id=CONTAINER, generation=3, agent_api_url="http://172.20.0.1:1148")
    assert readiness(business, paths, config, env)
    assert readiness(business, paths, config, env)
    assert calls == [CONTAINER, CONTAINER]


def test_failed_version_observations_are_not_cached(tmp_path, monkeypatch):
    readiness, business, paths, config, _ = make(tmp_path)
    calls = []
    def versions(broker):
        calls.append(broker.container_id)
        if len(calls) == 1:
            raise DomainError("environment_unavailable", "Transient version probe failure")
        return {"claude-code": "2.1.269", "codex": "0.154.0"}
    monkeypatch.setattr(DockerTmuxBroker, "installed_versions", versions)
    monkeypatch.setattr("bazarlyk.sandbox.readiness.prepare_agent", lambda *args, **kw: None)
    monkeypatch.setattr(readiness, "_probe", lambda *args: None)
    env = SimpleNamespace(workstation_id=CONTAINER, generation=3, agent_api_url="http://172.20.0.1:1148")
    with pytest.raises(DomainError, match="Transient"):
        readiness(business, paths, config, env)
    assert readiness(business, paths, config, env)
    assert calls == [CONTAINER, CONTAINER]


@pytest.mark.skipif(not os.environ.get("QORQUT_TEST_AGENT_READINESS_IMAGE"),
    reason="set QORQUT_TEST_AGENT_READINESS_IMAGE for the disposable native probe")
def test_real_prepared_registration_reaches_restricted_agent_listener(tmp_path):
    """Run the actual probe in a bounded container against the agent-only app.

    This is intentionally opt-in: it starts one temporary bridge container and
    a local listener, but performs only preparation/MCP discovery and no model or
    business mutation. The image must contain the pinned interpreter and the
    installed Bazarlyk bootstrap modules.
    """
    from test_agent_listener import ServiceFixture

    service = ServiceFixture(tmp_path)
    with service.fixture.db.read() as uow:
        business = dict(uow.repository("businesses").get(service.fixture.business_id))
    paths = BusinessPaths.registered(service.fixture.root, business)
    ownership = Ownership(hashlib.sha256(str(paths.root).encode()).hexdigest()[:20], paths.business_id)
    registration = paths.workspace / "hq" / "agents" / "growth" / ".bazarlyk" / "registration.json"
    assert registration.is_file()

    command = tuple(os.environ.get("QORQUT_TEST_DOCKER_COMMAND", "sudo -n docker").split())
    calls = []
    def runner(*args, **kwargs):
        result = subprocess.run(*args, **kwargs)
        calls.append(result)
        return result
    docker = Docker(command=command, runner=runner)
    network_name = "qorqut-readiness-net-" + service.fixture.business_id[-8:]
    docker.call("network", "create", "--internal", "--label", "qorqut.readiness=test", network_name)
    network = json.loads(docker.call("network", "inspect", network_name))[0]
    gateway = network["IPAM"]["Config"][0]["Gateway"]
    bridge = "br-" + network["Id"][:12]
    port = 1148
    endpoint = f"http://{gateway}:{port}"
    firewall = ("sudo", "-n", "iptables", "-I", "INPUT", "1", "-i", bridge,
                "-p", "tcp", "-d", gateway, "--dport", str(port), "-j", "ACCEPT")
    subprocess.run(firewall, check=True, capture_output=True, text=True, timeout=30)
    container = None
    listeners = BusinessAgentListeners(service.runtime)
    try:
        labels = {**ownership.labels, "qorqut.role": "agent", "qorqut.generation": "1"}
        args = ["create", "--name", "qorqut-readiness-" + service.fixture.business_id[-8:],
            "--network", network_name, "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "256m", "--memory-swap", "256m",
            "--cpus", "0.5", "--pids-limit", "64", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=755",
            "--mount", f"type=bind,src={paths.workspace},dst={paths.workspace},readonly"]
        for key, value in labels.items():
            args.extend(("--label", key + "=" + value))
        args.extend(("--entrypoint", "/bin/sh", os.environ["QORQUT_TEST_AGENT_READINESS_IMAGE"],
            "-c", "while :; do sleep 60; done"))
        container = docker.call(*args).splitlines()[-1].strip()
        docker.call("start", container)
        inspect = json.loads(docker.call("inspect", container))[0]
        address = inspect["NetworkSettings"]["Networks"][network_name]["IPAddress"]
        environment = RunningEnvironment(ownership, 1, container, "b" * 64, "c" * 64,
            address, "172.31.0.3", gateway, gateway, f"http://{gateway}:{port}")
        listeners.ensure(business, SimpleNamespace(agent_api_port=port), environment)
        broker = DockerTmuxBroker(docker, ownership, container, 1, SimpleNamespace())
        config = HostConfig("sha256:" + "a" * 64, "sha256:" + "b" * 64,
            ResourceLimits(256, 256, 128, 1, 128, 8), port, enabled=True)
        try:
            readiness = AgentReadiness(service.fixture.resolver, docker=docker)
            readiness(business, paths, config, environment)
            broker._exec("/opt/qorqut/venv/bin/python", "-I", "-c", _MCP_CHECK,
                input=json.dumps({"endpoint": endpoint, "registrations": [
                    str(registration), str(paths.workspace / "hq" / "agents" / "manager" / ".bazarlyk" / "registration.json")],
                    "agents": [service.agent.id, service.manager.id]}))
        except DomainError:
            diagnostic = next((match.group(0) for match in [
                re.search(r"QORQUT_PROBE_FAIL:\d+:\d+", calls[-1].stderr or "")
            ] if match), "no probe diagnostic")
            pytest.fail("native readiness probe failed: " + diagnostic)
    finally:
        listeners.stop()
        if container:
            docker.call("rm", "-f", container, timeout=180)
        subprocess.run(("sudo", "-n", "iptables", "-D", "INPUT", "-i", bridge,
            "-p", "tcp", "-d", gateway, "--dport", str(port), "-j", "ACCEPT"),
            check=False, capture_output=True, text=True, timeout=30)
        docker.call("network", "rm", network_name)


def test_probe_is_direct_and_checks_listener_boundaries():
    assert "ProxyHandler({})" in _PROBE
    assert "/api/agent/readiness" in _PROBE
    assert "/api/root/agents" in _PROBE
    assert "tools/list" in _PROBE
    assert "Mcp-Session-Id" in _PROBE
