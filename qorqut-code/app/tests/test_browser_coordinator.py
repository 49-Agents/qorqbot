"""Every Global agent may explicitly lease an active business's shared browser, host or sandbox, without membership.

Synthetic only: a temporary instance built from this branch's own Global role sources and the reviewed engine helper; real
credentials, machine registrations, assignments and worker bindings in that instance's database; a fake managed host
target; a fake sandbox binding whose connector reaches a fake Chrome on a 127.0.0.1 test socket; and a recording runner in
place of the browser runtime. No Docker, provider, browser, model, service or real configuration is used.
"""
import hashlib
import json
import secrets
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
import yaml
from websockets.sync.client import connect as ws_connect

import host_foundation_support as support
from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.bootstrap import browser
from bazarlyk.browser import access as access_module, config, control, coordinator
from bazarlyk.browser.cdp import BrowserCDPProxy
from bazarlyk.browser.operations import BROWSER_OPERATION
from bazarlyk.browser.socket import BrowserBinding
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.transport.auth import AuthenticatedPrincipal, Authentication
from host_foundation_support import build_instance, register_sources, set_agent_overrides
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, utc_now
from test_browser_cdp import SocketConnector, Upstream

GLOBALS = ("ordubashy", "global-worker", "learning-applier")


class Sandbox:
    """Only an observed binding: this fake has no listener origin, launch, restart or Docker access to call."""

    docker = None

    def __init__(self, binding):
        self.binding, self.reads = binding, 0

    def observed_browser_binding(self, business_id):
        self.reads += 1
        if self.binding is None or business_id != self.binding.business_id:
            raise DomainError("operation_unavailable", "Sandbox browser generation is not committed ready", retryable=True)
        return self.binding


class Connector(SocketConnector):
    """The real connector's exact-binding rule over a fake Chrome socket: a replaced binding is never connected."""

    def __init__(self, port, current):
        super().__init__(port)
        self.current, self.bindings = current, []

    def connect(self, binding):
        if self.current() != binding:
            raise DomainError("environment_conflict", "Browser CDP binding is stale")
        self.bindings.append(binding)
        return super().connect(binding)


class FastProxy(BrowserCDPProxy):
    """The production proxy with a short WebSocket lease check, so an in-flight replacement is seen within the test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, lease_interval=0.2, **kwargs)


def register_global(instance, owner, role, businesses):
    """Publish one Global role through the real catalog and register its ecosystem agent.

    The host registration model still requires Ordubashy to list every business; the other three hold no membership.
    """
    from bazarlyk.bootstrap.seed import apply_initial_plan
    runtime = instance.runtime
    with instance.database.read() as uow:
        plan = runtime.publisher.prepare(owner, {"source_id": role}, InvocationOptions(idempotency_key="seed-" + role), uow, "role")
    pin = apply_initial_plan(instance.database, owner, plan, runtime.publisher, runtime.publisher.resolve_file_target,
                             runtime.locks)["artifact"]
    created = instance.invoke(owner, "agents_create", {
        "role": pin, "plugins": [], "name": role + "-agent", "display_name": role.title(), "assignment_brief": "Synthetic fixture agent.",
        "scope": {"kind": "ecosystem", "allowed_business_ids": sorted(businesses) if role == "ordubashy" else [],
                  "global_capabilities": ["read_global_context"]}})
    assert created.status == "ok", created.error
    return created.data["agent_id"]


def register_machine(e, agent_id):
    """A real credential and machine registration for one Global agent, authenticated like any bearer request."""
    token, credential, registration, now = secrets.token_hex(32), new_id(), new_id(), utc_now()
    with e.db.transaction() as uow:
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()[0]
        uow.connection.execute("INSERT INTO credentials (id,principal_id,token_hash,created_at,updated_at) VALUES (?,?,?,?,?)",
                               (credential, agent_id, hashlib.sha256(token.encode()).hexdigest(), now, now))
        uow.connection.execute(
            "INSERT INTO machine_registrations (id,principal_id,workspace,service_url,credential_id,profile_revision,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (registration, agent_id, str(e.root / "machines" / registration), "http://127.0.0.1:9", credential, revision, now, now))
    return e.auth.agent("Bearer " + token)


def make_env(tmp_path, monkeypatch, *, without_skill=(), worker_skill=None, worker_grant=True):
    monkeypatch.setattr(support, "ROLE_ID", "ordubashy", raising=False)  # this branch's role sources are Global-only
    monkeypatch.delenv("QORQUT_APP_MANIFEST", raising=False)
    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    monkeypatch.setattr(config, "port_available", lambda port: True)
    monkeypatch.setattr(access_module, "BrowserCDPProxy", FastProxy)
    instance, ids = build_instance(tmp_path, businesses=("host", "boxed", "archived"))
    for role in without_skill:
        # An explicit synthetic copy of a published role that does not select browser control.
        manifest = instance.paths.component("roles") / "roles" / role / "role.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["skills"] = [skill for skill in data["skills"] if skill["id"] != "browser-control"]
        manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    if worker_skill is not None:
        manifest = instance.paths.component("roles") / "roles" / support.WORKER_ROLE / "role.yaml"
        data = yaml.safe_load(manifest.read_text())
        selected = [skill["id"] for skill in data.get("skills", [])]
        if "browser-control" not in selected:
            pytest.skip("the role source's global-worker does not select browser-control (needs the completed role artifact)")
        if not worker_skill:
            # Only the negative case edits its private copy: it drops that one selected skill and keeps every other.
            data["skills"] = [skill for skill in data["skills"] if skill["id"] != "browser-control"]
            manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    for slug, identity in ids.items():
        register_sources(instance, identity, slug)
    owner = InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset(ids.values()),
                              global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    agents = {role: register_global(instance, owner, role, ids.values()) for role in GLOBALS}
    if worker_grant:   # a worker's role default grants no browser operation: the owner allows it, as for any other worker
        set_agent_overrides(instance, agents["global-worker"], {BROWSER_OPERATION: "allow"}, [ids["host"], ids["boxed"]])
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (agents["ordubashy"],))
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), ids["archived"]))
        businesses = uow.repository("businesses")
        for slug in ("host", "boxed"):     # the owner's sandbox viewer also reads the business's enabled browser
            row = businesses.get(ids[slug], required=True)
            value = {**row["config_json"], "browser": {"enabled": True, "proxy_server": "http://proxy.example:8080"}}
            config.prepare_config(value, row["config_json"], paths=instance.paths, business_id=ids[slug],
                                  principal_kind="human", uow=uow)
            businesses.update(ids[slug], {"config_json": value}, expected_revision=row["revision"])
        SandboxStateStore().insert_intent(uow, ids["boxed"])
    upstream = Upstream()
    binding = BrowserBinding(ids["boxed"], "a" * 64, 1, "boxed-1", upstream.port)
    sandbox = Sandbox(binding)
    monkeypatch.setattr(instance.runtime, "sandbox", sandbox)
    access = instance.runtime.browser_access
    connectors = []

    def connector(business_id, current):
        assert business_id == ids["boxed"]
        connectors.append(Connector(upstream.port, current))
        return connectors[-1]
    monkeypatch.setattr(access, "_connector", connector)
    state = {"instance": "host-1"}

    def ready(directory, conf):
        return {"cdp_endpoint": f"http://127.0.0.1:{conf['cdp_port']}", "instance": state["instance"],
                "generation": conf["generation"]}
    monkeypatch.setattr(control, "ready_target", ready)
    e = SimpleNamespace(instance=instance, db=instance.database, root=tmp_path, ids=ids, host=ids["host"], boxed=ids["boxed"],
                        archived=ids["archived"], agents=agents, access=access, sandbox=sandbox, upstream=upstream,
                        connectors=connectors, state=state, calls=[], runtimes=[], during=None,
                        auth=Authentication(instance.database, instance.paths))
    e.actors = {role: register_machine(e, agents[role]) for role in GLOBALS}

    def run(workspace, command, *, target, instance, capture):
        endpoint = target[0]
        call = {"workspace": workspace, "command": command, "endpoint": endpoint, "instance": instance, "executable": target[1]}
        e.calls.append(call)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        (Path(workspace) / "shot.txt").write_text("evidence")
        if e.during is not None:
            return e.during(endpoint, call)
        if endpoint.startswith(f"http://127.0.0.1:{upstream.port}") or "/" in endpoint.removeprefix("http://"):
            call["version"] = cdp_get(endpoint)
        return 0, '{"ok":true}', ""

    def executable(runtime_dir):
        e.runtimes.append(runtime_dir)
        return Path("/bin/true")
    monkeypatch.setattr(browser, "run", run)
    monkeypatch.setattr(browser, "runtime_executable", executable)
    monkeypatch.setattr(browser, "load_target", lambda workspace: (
        "http://127.0.0.1:9225", Path("/opt/global-runtime/node_modules/.bin/agent-browser")))
    return e


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    yield e
    e.upstream.close()


def cdp_get(endpoint, path="/json/version"):
    response = httpx.get(endpoint + path, timeout=5, trust_env=False)
    return response.status_code, (response.json() if response.status_code == 200 else None)


def tool(e, action, who="ordubashy", **body):
    actor = e.actors[who] if isinstance(who, str) else who
    return e.access.tool(actor, action, body)


def acquire(e, who="ordubashy", business=None, **extra):
    return tool(e, "acquire", who, protocol="handles", business_id=business or e.host, **extra)


def rows(e, sql, *args):
    with e.db.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, args)]


def turns(e, requester=None):
    return rows(e, "SELECT * FROM browser_turns" + (" WHERE requester_id LIKE ?" if requester else "") + " ORDER BY created_at",
                *([requester] if requester else []))


def lease(e, business=None):
    path = config.browser_state_directory(e.instance.paths, business or e.host) / "lease.json"
    return json.loads(path.read_text()) if path.exists() else None


def refuse(code, match, call, *args, **kwargs):
    with pytest.raises(DomainError, match=match) as raised:
        call(*args, **kwargs)
    assert raised.value.code == code, raised.value.code
    return raised.value


def saved_token(e, handle):
    return json.loads(rows(e, "SELECT arguments_json FROM browser_handles WHERE id=?", handle)[0]["arguments_json"])["turn_token"]


def closed(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


def durable_worker(e, business, parent="ordubashy"):
    """A running assignment of a Global parent with its real worker credential binding."""
    runtime, parent_id = e.instance.runtime, e.agents[parent]
    store = runtime.delegation
    token = secrets.token_hex(32)
    with e.db.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business,
            scope={"kind": "business", "business_id": business, "browser_key": "business:" + business,
                   "record_ids": {}, "source_ids": {}}, objective="browser step", plan={"steps": ["acquire", "act", "release"]},
            allowed_operations=["browser_use"], resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
        running = store.claim(uow, saved["id"], actor_principal_id=parent_id, dispatcher_ready=True)
        store.bind_runtime(uow, running["id"], parent_principal_id=parent_id, generation=running["execution_generation"],
                           lease_id=running["lease_id"], expected_revision=running["revision"],
                           credential_hash=hashlib.sha256(token.encode()).hexdigest(),
                           registration_id=e.actors[parent].registration_id, expires_at=running["lease_until"])
    return e.auth.agent("Bearer " + token)


def test_every_global_leases_host_and_sandbox_businesses_without_membership(env):
    e = env
    host_runtime = e.access.runtime_directory()
    for role in GLOBALS:
        for business in (e.host, e.boxed):
            if role != "ordubashy":
                assert not rows(e, "SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=?", business,
                                e.agents[role])
            receipt = acquire(e, role, business)       # no consumer_id: the default is enough to begin
            assert receipt["status"] == "owning" and receipt["business_id"] == business and receipt["lease_until"]
            [turn] = [t for t in turns(e) if t["state"] == "owning"]
            assert turn["requester_id"] == coordinator.requester_id(e.agents[role], "default")
            token = saved_token(e, receipt["handle"])
            ran = tool(e, "command", role, handle=receipt["handle"], command=["observe", "overview"], action_id=role + business)
            assert ran["exit_code"] == 0 and ran["action"]["state"] == "succeeded"
            call = e.calls[-1]
            # The host runtime runs every command; a sandbox browser is reached through a per-command loopback proxy.
            assert e.runtimes[-1] == host_runtime and "/opt/qorqut-browser" not in json.dumps(e.runtimes)
            if business == e.host:
                assert call["endpoint"].startswith("http://127.0.0.1:") and call["instance"] == "host-1"
            else:
                port = int(call["endpoint"].removeprefix("http://127.0.0.1:").split("/")[0])
                assert call["endpoint"] == f"http://127.0.0.1:{port}/{token}" and call["instance"] == "boxed-1"
                status, version = call["version"]
                assert status == 200 and version["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}/{token}/devtools/page/abc"
                assert closed(port) and e.connectors[-1].bindings == [e.sandbox.binding]
            # No reply carries a token or endpoint.
            reply = json.dumps([receipt, ran])
            assert token not in reply and "127.0.0.1" not in reply and "cdp_endpoint" not in reply
            assert tool(e, "release", role, handle=receipt["handle"])["status"] == "released"
            assert lease(e, business) == {}


def test_omitting_business_id_keeps_the_own_lease_free_browser(env):
    e = env
    for role in ("ordubashy", "learning-applier"):
        receipt = tool(e, "acquire", role, protocol="handles")
        assert receipt == {"status": "owning", "handle": e.access.native_handle(e.agents[role], "default")}
        assert tool(e, "command", role, handle=receipt["handle"], command=["snapshot"])["exit_code"] == 0
        assert e.calls[-1]["endpoint"] == "http://127.0.0.1:9225"
    assert not turns(e) and lease(e) is None and lease(e, e.boxed) is None


def test_refused_callers_hold_nothing(env):
    e = env
    caller = e.agents["global-worker"]

    def refused(code, match, who="global-worker", business=None):
        before = (turns(e), lease(e), lease(e, e.boxed))
        refuse(code, match, acquire, e, who, business)
        assert (turns(e), lease(e), lease(e, e.boxed)) == before
        assert all(turn["state"] != "owning" for turn in before[0])

    refused("forbidden", "not active", business=e.archived)
    refused("forbidden", "not active", business=new_id())
    refuse("validation_error", "requires protocol=handles", tool, e, "acquire", "global-worker", business_id=e.host)
    # A worker credential and an unregistered or human caller never select a business.
    worker = durable_worker(e, e.host)
    refuse("forbidden", "exact project scope|exact configured", tool, e, "acquire", worker, protocol="handles", business_id=e.boxed)
    refuse("forbidden", "registered agent", tool, e, "acquire", AuthenticatedPrincipal(Principal(caller, "agent")),
           protocol="handles", business_id=e.host)
    refuse("forbidden", "registered agent", tool, e, "acquire", AuthenticatedPrincipal(Principal(e.instance.owner, "human")),
           protocol="handles", business_id=e.host)
    # A revoked, expired or rebound credential is refused at once.
    for column, value in (("revoked_at", utc_now()), ("expires_at", "2000-01-01T00:00:00Z")):
        with e.db.transaction() as uow:
            uow.connection.execute(f"UPDATE credentials SET {column}=? WHERE id=?", (value, e.actors["global-worker"].credential_id))
        refused("forbidden", "current credential and machine registration", business=e.boxed)
        with e.db.transaction() as uow:
            uow.connection.execute(f"UPDATE credentials SET {column}=NULL WHERE id=?", (e.actors["global-worker"].credential_id,))
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE machine_registrations SET credential_id=NULL WHERE id=?", (e.actors["global-worker"].registration_id,))
    refused("forbidden", "current credential and machine registration")
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE machine_registrations SET credential_id=? WHERE id=?",
                               (e.actors["global-worker"].credential_id, e.actors["global-worker"].registration_id))
    assert acquire(e, "global-worker")["status"] == "owning"
    tool(e, "release", "global-worker", handle=rows(e, "SELECT id FROM browser_handles")[0]["id"])
    # An inactive principal and a retired profile.
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (caller,))
    refused("forbidden", "current credential|active agent")
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (caller,))
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (caller,))
    refused("forbidden", "current credential")
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='active' WHERE agent_id=?", (caller,))
    # A registration whose role is not one of the Globals selects nothing, even with the skill and operation.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(coordinator, "GLOBAL_ROLES", coordinator.GLOBAL_ROLES - {"global-worker"})
        refused("forbidden", "limited to active Global agents")
    # A business-home agent cannot be registered from these Global-only role sources; a profile edited into one is historical.
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET home_business_id=?, namespace=? WHERE agent_id=?",
                               (e.host, e.host, caller))
    refused("inactive_agent", "historical")


def test_read_global_context_alone_cannot_use_browser_turn(env):
    e = env
    descriptor = e.instance.runtime.registry.get(BROWSER_OPERATION)
    assert descriptor.global_capability == "read_global_context"
    learning = e.agents["learning-applier"]
    handle = acquire(e, "learning-applier", e.boxed)["handle"]
    set_agent_overrides(e.instance, learning, {BROWSER_OPERATION: "deny"}, (e.host, e.boxed))
    context = e.instance.runtime.resolver.runtime_context(Principal(learning, "agent"))
    assert "read_global_context" in context.global_capabilities
    application = e.instance.runtime.application
    with e.db.read() as uow:
        refuse("policy_denied", "denied by the active policy", application.authorize, context, descriptor, (), uow,
               namespace=application.namespace(context, descriptor, {"action": "acquire"}))
    executed = len(e.calls)
    for action, body in (("status", {}), ("renew", {}), ("checkpoint", {"checkpoint": {}}), ("release", {}),
                         ("command", {"command": ["snapshot"], "action_id": "denied"})):
        refuse("policy_denied", "denied", tool, e, action, "learning-applier", handle=handle, **body)
    refuse("policy_denied", "denied", acquire, e, "learning-applier", e.host)
    assert len(e.calls) == executed and [t["state"] for t in turns(e)] == ["owning"]


def test_a_role_without_the_browser_skill_is_refused(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, without_skill=("learning-applier",))
    try:
        learning = e.agents["learning-applier"]
        descriptor = e.instance.runtime.registry.get(BROWSER_OPERATION)
        context = e.instance.runtime.resolver.runtime_context(Principal(learning, "agent"))
        application = e.instance.runtime.application
        with e.db.read() as uow:   # read_global_context and an allowed operation are not enough on their own
            assert application.authorize(context, descriptor, (), uow,
                namespace=application.namespace(context, descriptor, {"action": "acquire"})).mode == "allow"
        for business in (e.host, e.boxed):
            refuse("forbidden", "does not select browser control", acquire, e, "learning-applier", business)
        assert not turns(e) and acquire(e, "ordubashy", e.boxed)["status"] == "owning"
    finally:
        e.upstream.close()


def test_a_global_worker_uses_the_business_browser_only_with_an_explicit_grant(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, worker_skill=True, worker_grant=False)
    try:
        worker, both = e.agents["global-worker"], (e.host, e.boxed)
        package = e.instance.runtime.resolver.catalog.get("role", e.instance.runtime.resolver.profile(
            Principal(worker, "agent"))[0].data["role"])
        assert "bazarlyk-role-browser-control" in {skill["name"] for skill in package.skills}       # the actual role, unchanged
        refuse("policy_denied", "denied", acquire, e, "global-worker", e.host)       # selecting the skill grants nothing
        assert not turns(e)
        set_agent_overrides(e.instance, worker, {BROWSER_OPERATION: "allow"}, both)
        for business in both:
            receipt = acquire(e, "global-worker", business)
            assert receipt["status"] == "owning" and not rows(
                e, "SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=?", business, worker)
            refuse("busy", "Another task owns this project browser", acquire, e, "ordubashy", business)   # one exclusive lease
            assert tool(e, "command", "global-worker", handle=receipt["handle"], command=["observe", "overview"],
                        action_id="worker-" + business)["exit_code"] == 0
            assert tool(e, "release", "global-worker", handle=receipt["handle"])["status"] == "released"
            assert lease(e, business) == {}
        held = acquire(e, "global-worker", e.host)["handle"]
        set_agent_overrides(e.instance, worker, {BROWSER_OPERATION: "deny"}, both)   # the grant is revoked while it holds the lease
        executed = len(e.calls)
        refuse("policy_denied", "denied", tool, e, "command", "global-worker", handle=held, command=["snapshot"], action_id="revoked")
        refuse("policy_denied", "denied", acquire, e, "global-worker", e.boxed)
        assert len(e.calls) == executed
    finally:
        e.upstream.close()


def test_a_global_worker_whose_role_does_not_select_browser_control_is_refused_even_with_the_grant(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, worker_skill=False)
    try:
        set_agent_overrides(e.instance, e.agents["global-worker"], {BROWSER_OPERATION: "allow"}, (e.host, e.boxed))
        for business in (e.host, e.boxed):
            refuse("forbidden", "does not select browser control", acquire, e, "global-worker", business)
        assert not turns(e) and acquire(e, "ordubashy", e.boxed)["status"] == "owning"
    finally:
        e.upstream.close()


def test_globals_owner_and_workers_share_one_exclusive_lease(env):
    from bazarlyk.sandbox.browser_viewer_service import BusinessBrowserViewers
    e = env
    viewer_config = SimpleNamespace(viewer_port=1, owner_addresses=("127.0.0.1",))
    viewers = BusinessBrowserViewers(e.instance.runtime, config_loader=lambda paths: viewer_config)
    for business in (e.host, e.boxed):
        first = acquire(e, "ordubashy", business)["handle"]
        before = lease(e, business)
        for other in ("global-worker", "learning-applier"):
            refuse("busy", "Another task owns this project browser", acquire, e, other, business)
        # A refused acquisition leaves nothing queued and never touches the holder.
        assert len(turns(e, "coordinator:%")) == 1 + (2 if business == e.boxed else 0) and lease(e, business) == before
        if business == e.boxed:
            # The owner's sandbox viewer queues through the same turns and cannot take it either.
            refuse("busy", "Another task owns this project browser", viewers._control, {"client": ("127.0.0.1", 1)},
                   business, "acquire", "e" * 64, viewer_config)
            assert lease(e, business) == before and not turns(e, e.instance.owner)
        # A durable worker queues behind it instead of taking Chrome.
        worker = durable_worker(e, business)
        waiting = tool(e, "acquire", worker, protocol="handles")
        assert waiting["status"] == "waiting" and waiting["position"] == 1
        assert tool(e, "command", "ordubashy", handle=first, command=["snapshot"], action_id="still-" + business)["exit_code"] == 0
        assert tool(e, "release", "ordubashy", handle=first)["status"] == "released" and lease(e, business) == {}
        # The waiting worker keeps its FIFO place; a Global agent does not jump ahead of it.
        refuse("busy", "Another task", acquire, e, "global-worker", business)
        store = e.instance.runtime.delegation
        with e.db.transaction() as uow:
            resumed = store.claim(uow, worker.delegation_assignment_id, actor_principal_id=e.agents["ordubashy"],
                                  dispatcher_ready=True)
            token = secrets.token_hex(32)
            store.bind_runtime(uow, resumed["id"], parent_principal_id=e.agents["ordubashy"],
                               generation=resumed["execution_generation"], lease_id=resumed["lease_id"],
                               expected_revision=resumed["revision"], credential_hash=hashlib.sha256(token.encode()).hexdigest(),
                               registration_id=e.actors["ordubashy"].registration_id, expires_at=resumed["lease_until"])
        worker = e.auth.agent("Bearer " + token)
        granted = tool(e, "acquire", worker, protocol="handles")
        assert granted["status"] == "owning"
        refuse("busy", "Another task", acquire, e, "ordubashy", business)
        tool(e, "release", worker, handle=granted["handle"])
        last = acquire(e, "global-worker", business)["handle"]
        tool(e, "release", "global-worker", handle=last)


def test_assignment_sandbox_command_uses_the_same_loopback_proxy(env):
    e = env
    worker = durable_worker(e, e.boxed)
    granted = tool(e, "acquire", worker, protocol="handles")
    assert granted["status"] == "owning" and "target" not in granted
    ran = tool(e, "command", worker, handle=granted["handle"], command=["snapshot"], action_id="worker-1")
    assert ran["exit_code"] == 0 and e.calls[-1]["version"][0] == 200 and e.runtimes[-1] == e.access.runtime_directory()
    port = int(e.calls[-1]["endpoint"].removeprefix("http://127.0.0.1:").split("/")[0])
    assert closed(port)

    # A worker binding revoked mid-command loses the proxy at once.
    def revoke(endpoint, call):
        call["before"] = cdp_get(endpoint)[0]
        with e.db.transaction() as uow:
            uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=? WHERE id=?",
                                   (utc_now(), worker.delegation_binding_id))
        call["after"] = cdp_get(endpoint)[0]
        return 0, "", ""
    e.during = revoke
    tool(e, "command", worker, handle=granted["handle"], command=["snapshot"], action_id="worker-2")
    assert (e.calls[-1]["before"], e.calls[-1]["after"]) == (200, 403)


def test_handle_selectors_identity_and_supersession_are_fixed(env):
    e = env
    receipt = acquire(e, "global-worker", e.boxed, consumer_id=new_id())
    handle = receipt["handle"]
    for change in ({"business_id": e.host}, {"browser_key": "business:" + e.host}, {"consumer_id": new_id()}):
        refuse("forbidden", "selectors cannot be changed", tool, e, "status", "global-worker", handle=handle, **change)
    # Another Global agent, and the same principal with another credential/registration, cannot use it.
    refuse("forbidden", "unavailable for this caller", tool, e, "status", "ordubashy", handle=handle)
    again = register_machine(e, e.agents["global-worker"])
    refuse("forbidden", "unavailable for this caller", tool, e, "status", again, handle=handle)
    refuse("validation_error", "only on acquire", tool, e, "status", "global-worker", business_id=e.boxed)
    refuse("validation_error", "lease credentials are managed", tool, e, "status", "global-worker", handle=handle,
           turn_token="a" * 64)
    # A repeat acquisition rotates the lease token and retires the earlier handle; no newer turn is adopted.
    consumer = json.loads(rows(e, "SELECT arguments_json FROM browser_handles WHERE id=?", handle)[0]["arguments_json"])["consumer_id"]
    newest = acquire(e, "global-worker", e.boxed, consumer_id=consumer)["handle"]
    assert newest != handle and saved_token(e, newest) != saved_token(e, handle) and lease(e, e.boxed)["token"] == saved_token(e, newest)
    for action in ("status", "command", "release"):
        refuse("stale_browser_handle", "expired or was released|replaced by a newer", tool, e, action, "global-worker",
               handle=handle, **({"command": ["snapshot"], "action_id": "old"} if action == "command" else {}))
    assert tool(e, "status", "global-worker", handle=newest)["status"] == "owning"
    # A replaced browser generation or instance is stale for the handle; release still lets go.
    e.sandbox.binding = BrowserBinding(e.boxed, "b" * 64, 2, "boxed-2", e.upstream.port)
    for action in ("status", "renew", "command"):
        refuse("stale_browser_handle", "replaced or reconfigured", tool, e, action, "global-worker", handle=newest,
               **({"command": ["snapshot"], "action_id": "late"} if action == "command" else {}))
    assert tool(e, "release", "global-worker", handle=newest)["status"] == "released" and lease(e, e.boxed) == {}
    # An expired lease is stale for every use.
    late = acquire(e, "global-worker", e.boxed)["handle"]
    real = e.access.turns.clock
    e.access.turns.clock = lambda: "2999-01-01T00:00:00.000000Z"
    try:
        for action in ("status", "renew"):
            refuse("stale_browser_handle", "expired or was released", tool, e, action, "global-worker", handle=late)
    finally:
        e.access.turns.clock = real
    tool(e, "release", "global-worker", handle=late)


def test_a_replaced_browser_lease_or_caller_rejects_in_flight_proxy_access(env):
    e = env
    handle = acquire(e, "ordubashy", e.boxed)["handle"]
    replaced = BrowserBinding(e.boxed, "c" * 64, 2, "boxed-2", e.upstream.port)

    def replace_browser(endpoint, call):
        call["before"] = cdp_get(endpoint)[0]
        e.sandbox.binding = replaced
        call["after"] = cdp_get(endpoint)[0]
        e.sandbox.binding = original
        return 0, "", ""
    original = e.sandbox.binding
    e.during = replace_browser
    tool(e, "command", "ordubashy", handle=handle, command=["snapshot"], action_id="replace")
    assert (e.calls[-1]["before"], e.calls[-1]["after"]) == (200, 403)

    def open_socket(endpoint, call):
        status, version = cdp_get(endpoint)
        with ws_connect(version["webSocketDebuggerUrl"], open_timeout=5) as socket_:
            socket_.send("hello")
            call["echo"] = socket_.recv(timeout=5)
            e.sandbox.binding = replaced         # the open socket's periodic check sees the replacement
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    socket_.send("still")
                    socket_.recv(timeout=1)
                    time.sleep(0.05)
            except Exception as exc:
                call["closed"] = type(exc).__name__
        e.sandbox.binding = original
        return 0, "", ""
    e.during = open_socket
    tool(e, "command", "ordubashy", handle=handle, command=["snapshot"], action_id="socket")
    assert e.calls[-1]["echo"] == "hello" and e.calls[-1].get("closed")

    # A released lease, a revoked credential and a denied operation each end in-flight access.
    def lose(change, undo):
        def during(endpoint, call):
            call["before"] = cdp_get(endpoint)[0]
            with e.db.transaction() as uow:
                uow.connection.execute(*change)
            call["after"] = cdp_get(endpoint)[0]
            with e.db.transaction() as uow:
                uow.connection.execute(*undo)
            return 0, "", ""
        return during
    turn_id = turns(e, "coordinator:%")[0]["id"]
    lease_until = turns(e, "coordinator:%")[0]["lease_until"]
    for number, (change, undo) in enumerate((
            (("UPDATE browser_turns SET lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?", (turn_id,)),
             ("UPDATE browser_turns SET lease_until=? WHERE id=?", (lease_until, turn_id))),
            (("UPDATE credentials SET revoked_at=? WHERE id=?", (utc_now(), e.actors["ordubashy"].credential_id)),
             ("UPDATE credentials SET revoked_at=NULL WHERE id=?", (e.actors["ordubashy"].credential_id,))),
            (("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), e.boxed)),
             ("UPDATE businesses SET archived_at=NULL WHERE id=?", (e.boxed,))))):
        e.during = lose(change, undo)
        tool(e, "command", "ordubashy", handle=handle, command=["snapshot"], action_id=f"lost-{number}")
        assert (e.calls[-1]["before"], e.calls[-1]["after"]) == (200, 403), number
    # A capability for another path, origin or token is refused by the proxy itself.

    def foreign(endpoint, call):
        base, token = endpoint.rsplit("/", 1)
        call["other_token"] = httpx.get(base + "/" + "f" * 64 + "/json/version", timeout=5, trust_env=False).status_code
        call["origin"] = httpx.get(endpoint + "/json/version", headers={"Origin": "http://evil.example"}, timeout=5,
                                   trust_env=False).status_code
        call["path"] = httpx.get(endpoint + "/json/new?about:blank", timeout=5, trust_env=False).status_code
        return 0, "", ""
    e.during = foreign
    tool(e, "command", "ordubashy", handle=handle, command=["snapshot"], action_id="foreign")
    assert (e.calls[-1]["other_token"], e.calls[-1]["origin"], e.calls[-1]["path"]) == (403, 403, 400)


def test_listener_closes_and_uncertain_actions_need_reconcile(env):
    e = env
    handle = acquire(e, "ordubashy", e.boxed)["handle"]
    ports = []

    def timed_out(endpoint, call):
        ports.append(int(endpoint.removeprefix("http://127.0.0.1:").split("/")[0]))
        assert cdp_get(endpoint)[0] == 200
        raise browser.BrowserTimeout("Browser command timed out")
    e.during = timed_out
    result = tool(e, "command", "ordubashy", handle=handle, command=["click", "e1"], action_id="click-1")
    assert result["action"]["state"] == "uncertain" and closed(ports[-1])
    ran = len(e.calls)
    # No blind retry: the same identity needs reconciling; a new command and release are fenced.
    refuse("browser_reconcile", "reconcile it before retrying", tool, e, "command", "ordubashy", handle=handle,
           command=["click", "e1"], action_id="click-1")
    refuse("browser_busy", "already in flight", tool, e, "command", "ordubashy", handle=handle, command=["click", "e2"],
           action_id="click-2")
    refuse("browser_busy", "command is in flight", tool, e, "release", "ordubashy", handle=handle)
    refuse("busy", "Another task", acquire, e, "global-worker", e.boxed)
    assert len(e.calls) == ran
    settled = tool(e, "reconcile", "ordubashy", handle=handle, action_id="click-1", pending_outcome={"observed": "clicked"})
    assert settled["action"]["state"] == "reconciled" and turns(e)[0]["state"] == "released" and lease(e, e.boxed) == {}
    # A listener start failure is never a successful or untracked command.
    handle = acquire(e, "global-worker", e.boxed)["handle"]
    from bazarlyk.sandbox import listener

    def broken(self, *, timeout=10):
        raise DomainError("environment_unavailable", "Business listener did not start")
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(listener.ListenerServer, "start", broken)
        e.during = None
        failed = tool(e, "command", "global-worker", handle=handle, command=["snapshot"], action_id="no-listener")
    assert failed["action"]["state"] == "uncertain" and len(e.calls) == ran
    refuse("browser_reconcile", "reconcile", tool, e, "command", "global-worker", handle=handle, command=["snapshot"],
           action_id="no-listener")


def test_reads_never_start_chrome_or_an_agent(env):
    e = env
    handle = acquire(e, "ordubashy", e.boxed)["handle"]
    reads = e.sandbox.reads
    for action in ("status", "renew", "checkpoint"):
        tool(e, action, "ordubashy", handle=handle, **({"checkpoint": {"tab": "t1"}} if action == "checkpoint" else {}))
    assert e.sandbox.reads == reads + 3 and not e.calls and not e.connectors
    tool(e, "release", "ordubashy", handle=handle)
    # A stopped sandbox browser holds no turn and starts nothing.
    e.sandbox.binding = None
    refuse("operation_unavailable", "not committed ready", acquire, e, "ordubashy", e.boxed)
    assert all(t["state"] != "owning" for t in turns(e)) and not e.calls

    def stopped(directory, conf):
        raise DomainError("operation_unavailable", "The managed project browser is stopped or needs an operator restart")
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(control, "ready_target", stopped)
        refuse("operation_unavailable", "stopped", acquire, e, "ordubashy", e.host)
    assert all(t["state"] != "owning" for t in turns(e))


def test_the_operation_accepts_no_endpoint_and_describes_every_global_role(env):
    e = env
    descriptor = e.instance.runtime.registry.get(BROWSER_OPERATION)
    for extra in ({"cdp_endpoint": "http://127.0.0.1:9222"}, {"docker": "x"}, {"url": "http://example.com"}):
        with pytest.raises(DomainError):
            descriptor.validate({"action": "acquire", "protocol": "handles", "business_id": e.boxed, **extra})
    text = descriptor.description + json.dumps(descriptor.input_schema, default=lambda value: dict(value) if hasattr(value, "keys") else list(value))
    for phrase in ("Ordubashy, workers, Learning Applier", "no membership is needed",
                   "consumer_id stays optional", "a busy browser is refused (never taken)", "no browser endpoint is returned"):
        assert phrase in text
    assert "Onbashy" not in text and "session UUID" not in text and "configured Ordubashy" not in text


def test_mcp_bridge_passes_only_an_explicit_business(env):
    from bazarlyk.transport.mcp import MCPTransport, split_options
    e = env
    runtime = e.instance.runtime
    actor = e.actors["ordubashy"]

    class Caller:
        def exposes(self, descriptor): return True
        def check_operation(self, name): pass
        def validate_options(self, actor, name, options): pass
        def validate_arguments(self, actor, name, args): pass
        def invocation_context(self, actor, *, canonical_name, options):
            # Identity and policy from the real resolver; the business selected by ordinary task context.
            context = runtime.resolver.runtime_context(actor.principal, session_registration_id=actor.registration_id)
            return context.select_business(e.host)

    args, options = split_options({"action": "acquire", "business_id": e.host},
                                  domain_fields=runtime.registry.get(BROWSER_OPERATION).input_schema["properties"])
    assert args["business_id"] == e.host and options.business_id is None
    transport = MCPTransport(runtime, authentication=None, origins=[], caller=Caller())
    call = lambda body: anyio.run(transport._single_result, BROWSER_OPERATION, {**body, "idempotency_key": new_id()}, actor)
    # Task context alone selects nothing: the own browser is used and nothing is leased.
    plain = call({"action": "acquire", "protocol": "handles"})
    assert plain["status"] == "ok" and plain["data"]["handle"] == e.access.native_handle(e.agents["ordubashy"], "default")
    assert not turns(e) and lease(e) is None
    explicit = call({"action": "acquire", "protocol": "handles", "business_id": e.boxed})
    assert explicit["status"] == "ok" and explicit["data"]["business_id"] == e.boxed
    handle = explicit["data"]["handle"]
    ran = call({"action": "command", "handle": handle, "command": ["snapshot"], "action_id": "via-mcp"})
    assert ran["status"] == "ok" and e.calls[-1]["version"][0] == 200
    assert saved_token(e, handle) not in json.dumps([explicit, ran])
    changed = call({"action": "status", "handle": handle, "business_id": e.host})
    assert changed["status"] == "error" and "selectors cannot be changed" in json.dumps(changed)
    assert call({"action": "release", "handle": handle})["status"] == "ok"


def test_owner_viewer_control_is_owner_only(env):
    e = env
    owner, token = Principal(e.instance.owner, "human"), "e" * 64
    resolver = e.instance.runtime.resolver
    target = control.business_control(resolver, owner, "acquire", token, owner_business_id=e.host)
    assert target["instance"] == "host-1" and lease(e)["owner_id"] == e.instance.owner
    assert control.business_control(resolver, owner, "check", token, owner_business_id=e.host)["business_id"] == e.host
    refuse("busy", "Another task", acquire, e, "ordubashy")
    for agent in GLOBALS:
        refuse("forbidden", "registered project owner", control.business_control, resolver, Principal(e.agents[agent], "agent"),
               "acquire", "a" * 64, owner_business_id=e.host)
    assert control.business_control(resolver, owner, "release", token, owner_business_id=e.host) == {"released": True}
    with pytest.raises(TypeError):
        control.business_control(resolver, owner, "acquire", token)
    # The owner's sandbox viewer holds its own exclusive turn; no Global agent takes it until it detaches.
    from bazarlyk.sandbox.browser_viewer_service import BusinessBrowserViewers
    viewer_config = SimpleNamespace(viewer_port=1, owner_addresses=("127.0.0.1",))
    viewers = BusinessBrowserViewers(e.instance.runtime, config_loader=lambda paths: viewer_config)
    peer = {"client": ("127.0.0.1", 1)}
    assert viewers._control(peer, e.boxed, "acquire", token, viewer_config)["instance"] == "boxed-1"
    assert lease(e, e.boxed)["owner_id"] == e.instance.owner
    for agent in GLOBALS:
        refuse("busy", "Another task", acquire, e, agent, e.boxed)
    viewers._control(peer, e.boxed, "release", token, viewer_config)
    assert acquire(e, "learning-applier", e.boxed)["status"] == "owning"
    refuse("busy", "Another task", viewers._control, peer, e.boxed, "acquire", token, viewer_config)
