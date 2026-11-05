"""The global-only host foundation (v1.2 GLOBAL_AGENTS_ONLY G017-G019), on a synthetic composed host.

Real: the stock host assembly and runtime (resolver, policy, application, the agent and catalog operations, business creation,
local seed, business restore, the business setup consumer, Settings reads, preparation, terminal control and the native Chat
store), the real Context queue with ``context_instruct``/``context_edit`` and a disposable real Context. Faked: only the model
runner (it fails if called), the clock and the synthetic principals and files under a pytest ``tmp_path``
(``context_edit_support``). No operational data, provider, model, network or live registration is used.

Contract: the active roster is the two singleton global roles (Founder and Global Marketing are retired) with at most one holder each, and any number of workers; their business access is every
active business, derived in each transaction with no profile or membership write; businesses own resources only, so founding,
seed and restore create no agent; a business-home identity never operates, whatever its stored status."""

import json
from pathlib import Path
import shutil

import pytest
import yaml

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.graph_edits")

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, utc_now

from bazarlyk.agents.global_scope import active_business_participant, effective_business_ids, reaches_business
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.native_chat import store as native_store
from bazarlyk.transport.terminals import TerminalService
from context_edit_support import build, put_node
from host_foundation_support import create_worker, historical_business_agent, select_coordinator

GLOBALS = ("ordubashy", "learning-applier")
VERSIONS = {"codex": "codex-cli 0.156.1", "claude-code": "2.1.263 (Claude Code)"}


@pytest.fixture
def host(tmp_path):
    built = build(tmp_path, coordinator=False)                       # the cases register the supported globals themselves
    yield built
    built.no_model()


def establish(host, role, *, scope_ids=(), overrides=None, capabilities=("read_global_context",)):
    """The owner's controlled establishment of one missing supported global."""
    arguments = {"role": host.pin(role), "plugins": [], "name": role, "display_name": role.title(),
                 "assignment_brief": "Synthetic global agent.",
                 "scope": {"kind": "ecosystem", "allowed_business_ids": list(scope_ids), "global_capabilities": list(capabilities)}}
    if role == "ordubashy":
        arguments["runtime"] = "codex"
    if overrides:
        arguments["tool_policy"] = {"overrides": overrides}
    identity = host.data(host.human(None), "agents_create", arguments)["agent_id"]
    if role == "ordubashy":
        select_coordinator(host.instance, identity)
    return identity


def all_globals(host, overrides=None):
    """The supported singletons plus one worker (``colleague``): every current global role, each with one registration."""
    found = {role: establish(host, role, overrides=(overrides or {}).get(role)) for role in GLOBALS}
    colleague = create_worker(host.instance, "colleague")
    if (overrides or {}).get("colleague"):
        revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", colleague)[0]["revision"]
        host.data(host.human(None), "permissions_set", {"agent_id": colleague, "overrides": overrides["colleague"]},
                  expected_revision=revision)
    return {**found, "colleague": colleague}


def legacy_business_agent(host, business="first", *, status="active", role="business-agent", pin=None):
    """A historical business-home identity as v1 stored it (its status may not have been migrated yet)."""
    return historical_business_agent(host.instance, host.ids[business], status=status, role=role, pin=pin)


def business_role_source(host, role_id):
    """An authored business-scoped role source; current role sources have none, so a synthetic copy stands in for v1's."""
    root = host.instance.workspace / "synthetic-historical-role-source"
    if not root.exists():
        shutil.copytree(host.instance.workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults", root)
    sources = root / "roles"
    if not (sources / role_id).is_dir():
        shutil.copytree(sources / "imported-roles/native/roles/ordubashy", sources / role_id)
        manifest = yaml.safe_load((sources / role_id / "role.yaml").read_text())
        manifest.update({"id": role_id, "scope_kinds": ["business"], "scope_brief": "business-agent"})
        (sources / role_id / "role.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return sources.parent


def historical_role_artifact(host, role_id="business-agent"):
    """An immutable business-role artifact exactly as v1 publication stored it; publishing one is no longer possible."""
    from bazarlyk.catalog.packages import load_package
    package = load_package(business_role_source(host, role_id), f"roles/{role_id}/role.yaml", "role", host.runtime.registry)
    catalog = host.runtime.publisher.catalog
    stored = catalog.artifact_path(package)
    for path, content in package.files.items():
        (stored / path).parent.mkdir(parents=True, exist_ok=True)
        (stored / path).write_bytes(content)
    with host.database.transaction() as uow:
        uow.repository("artifacts").insert({"kind": "role", "artifact_id": package.id, "version": package.version,
            "digest": package.digest, "manifest_schema_version": 1, "stored_path": str(stored),
            "metadata_json": {"manifest_path": package.manifest_path, "source_id": role_id}})
    return package.ref


def agent_rows(host):
    return {row["agent_id"]: (row["status"], row["revision"], row["home_business_id"])
            for row in host.query("SELECT agent_id, status, revision, home_business_id FROM agent_profiles")}


def agent_memberships(host):
    return host.query("SELECT m.business_id, m.principal_id, m.active FROM business_memberships m "
                      "JOIN principals p ON p.id=m.principal_id WHERE p.kind='agent' ORDER BY m.business_id, m.principal_id")


def decoded(value):
    return value if isinstance(value, dict) else json.loads(value)


def stored_scope(host, agent_id):
    return decoded(host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", agent_id)[0]["profile_json"])["scope"]


def active_ids(host):
    return {row["id"] for row in host.query("SELECT id FROM businesses WHERE archived_at IS NULL")}


# -- the roster -------------------------------------------------------------------------------------------------------------

def test_the_roster_is_the_supported_singletons_and_workers_with_one_holder_per_singleton_and_only_the_owner_establishes_one(host):
    agents = all_globals(host)
    for role, identity in agents.items():
        assert stored_scope(host, identity)["allowed_business_ids"] == []                     # nothing saved as authority
        shown = host.read(host.human("first"), "agents_get", {"agent_id": identity})["agent"]
        assert shown["role"]["id"] == ("global-worker" if role == "colleague" else role) and shown["scope"]["kind"] == "ecosystem"
        assert shown["scope"]["allowed_business_ids"] == sorted(active_ids(host))             # the computed read projection
    # A second holder of a supported role, a business home and a business-scoped role are refused.
    second = {"role": host.pin("learning-applier"), "plugins": [], "name": "applier-two", "display_name": "Applier Two",
              "assignment_brief": "Duplicate.", "scope": {"kind": "ecosystem", "allowed_business_ids": [],
                                                          "global_capabilities": ["read_global_context"]}}
    assert host.error(host.human(None), "agents_create", second) == "roster_conflict"
    homed = {**second, "name": "homed", "scope": {"kind": "business", "business_id": host.ids["first"]}}
    assert host.error(host.human("first"), "agents_create", homed) == "invalid_profile"
    # Business-scoped roles are not publishable: the checked-in map registers no source for one, and an installed map that
    # still registers an authored one (as a v1-era registry may) is refused too.
    for retired in ("business-agent", "dev"):
        codes = []
        for registered in (False, True):
            if registered:
                host.runtime.publisher.sources[retired] = ("role", business_role_source(host, retired), f"roles/{retired}/role.yaml")
            with host.database.read() as uow:
                with pytest.raises(DomainError) as refused:
                    host.runtime.publisher.prepare(host.human(None), {"source_id": retired},
                                                   InvocationOptions(idempotency_key="retired-" + retired), uow, "role")
            codes.append(refused.value.code)
        assert codes == ["not_found", "retired_role"], codes
    # Product agents never create agents, even with a Signoff-capable default.
    coordinator = host.as_agent(agents["ordubashy"])
    assert host.error(coordinator, "agents_create", {**second, "role": host.pin("learning-applier"), "name": "spawned"}) == "forbidden"
    assert len([row for row in agent_rows(host).values() if row[0] == "active"]) == 3 and agent_memberships(host) == []
    # A deactivated holder frees its role for a new owner-established identity; the old one is never revived.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", agents["learning-applier"])[0]["revision"]
    host.data(host.human(None), "agents_deactivate", {"agent_id": agents["learning-applier"]}, expected_revision=revision)
    replacement = establish(host, "learning-applier")
    assert replacement != agents["learning-applier"] and agent_rows(host)[agents["learning-applier"]][0] == "inactive"


# -- derived access ---------------------------------------------------------------------------------------------------------

def test_effective_scope_is_every_active_business_without_any_profile_or_membership_write(host):
    agents = all_globals(host)
    before = agent_rows(host)
    third = new_id()
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": third, "slug": "third", "name": "Third", "workspace": str(host.paths.business_root("third")),
                                             "hq_path": str(host.paths.business_hq("third"))})
        uow.repository("business_memberships", business_id=third).insert({"principal_id": host.owner, "is_owner": True})
    host.ids["third"] = third
    for identity in agents.values():
        ctx = host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=third)
        assert ctx.allowed_business_ids == frozenset(host.ids.values()) | {third} and ctx.business_id == third
    assert agent_rows(host) == before and agent_memberships(host) == []                     # no profile or membership write
    # Archiving removes the business from every global at once; a saved legacy list never grants it back.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), host.ids["second"]))
        profile = json.loads(uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?",
                                                    (agents["ordubashy"],)).fetchone()[0])
        profile["scope"]["allowed_business_ids"] = sorted(host.ids.values())
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(profile), agents["ordubashy"]))
    with host.database.read() as uow:
        assert effective_business_ids(uow.connection, agents["ordubashy"]) == frozenset({host.ids["first"], third})
        assert not reaches_business(uow.connection, agents["ordubashy"], host.ids["second"])
    with pytest.raises(DomainError):
        host.runtime.resolver.invocation_context(Principal(agents["ordubashy"], "agent"), business_id=host.ids["second"])
    shown = host.read(host.human("first"), "agents_get", {"agent_id": agents["learning-applier"]})["agent"]
    assert shown["scope"]["allowed_business_ids"] == sorted({host.ids["first"], third})
    # Deactivation ends derived access; humans keep their membership semantics.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", agents["colleague"])[0]["revision"]
    host.data(host.human(None), "agents_deactivate", {"agent_id": agents["colleague"]}, expected_revision=revision)
    with host.database.read() as uow:
        assert effective_business_ids(uow.connection, agents["colleague"]) == frozenset()
        assert active_business_participant(uow.connection, third, host.owner)
        assert not active_business_participant(uow.connection, third, new_id())


def test_a_business_home_identity_never_operates_whatever_its_stored_status(host):
    agents = all_globals(host)
    unmigrated = legacy_business_agent(host, pin=historical_role_artifact(host))
    inactive = legacy_business_agent(host, "second", status="inactive")
    resolver, owner = host.runtime.resolver, host.human(None)
    for identity in (unmigrated, inactive):
        with pytest.raises(DomainError) as refused:
            resolver.profile(Principal(identity, "agent"))
        assert refused.value.code == "inactive_agent"
        with pytest.raises(DomainError):
            prepare_agent(resolver, Principal(identity, "agent"), endpoint="http://127.0.0.1:9988", versions=VERSIONS)
        with host.database.read() as uow:
            assert effective_business_ids(uow.connection, identity) == frozenset()
            assert not active_business_participant(uow.connection, host.ids["first"], identity)  # its old membership is inert
            assert native_store.active_profile(uow.connection, identity, host.paths) is None
            assert native_store.active_profile(uow.connection, identity, host.paths, any_status=True)["agent_id"] == identity
            with pytest.raises(DomainError) as control:
                host.runtime.terminal_control.target(owner, identity, uow)
            assert control.value.code == "forbidden"
    with host.database.read() as uow:
        observed = {row["agent_id"] for row in native_store.observed_agents(uow.connection, host.paths)}
        assert observed == set(agents.values())
        with pytest.raises(DomainError):
            host.runtime.terminal_control.message_target(owner, unmigrated, uow)
    # The trusted broadcast port never types into a business-home identity's terminal.
    terminals = TerminalService(host.runtime, None, service_url="http://127.0.0.1:9")
    assert terminals.input_existing(host.ids["first"], unmigrated, "hello") == {"state": "unavailable", "detail": "Agent is unavailable"}
    # Historical profiles stay readable as records; the supported globals keep operating.
    assert host.read(host.human("first"), "agents_list", {"include_inactive": True})["agents"]
    assert {resolver.profile(Principal(identity, "agent"))[0].role.id for identity in agents.values()} == {*GLOBALS, "global-worker"}
    # The owner can still close an unmigrated business-home identity; its leftover membership closes with it.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", unmigrated)[0]["revision"]
    host.data(host.human("first"), "agents_deactivate", {"agent_id": unmigrated}, expected_revision=revision)
    assert agent_rows(host)[unmigrated][0] == "inactive"
    assert [row["active"] for row in agent_memberships(host) if row["principal_id"] == unmigrated] == [0]


# -- businesses own resources only -------------------------------------------------------------------------------------------

def create_args(slug):
    return {"slug": slug, "name": slug.title(), "summary": "Help repair shops handle arrivals."}


def test_creation_and_restore_create_project_resources_and_no_agent(host):
    # Only the supported global roles are published: no business-agent or Dev package exists or is needed.
    # The configured Ordubashy creates the project (the owner asks it to); it reaches the project without any profile write.
    coordinator = establish(host, "ordubashy", capabilities=("coordinate_businesses", "read_global_context"),
                            overrides={"business_create": "allow"})
    assert {row["artifact_id"] for row in host.query("SELECT artifact_id FROM artifacts WHERE kind='role'")} == {"ordubashy"}
    before = agent_rows(host)
    assert host.error(host.human(None), "business_create", create_args("owner-made")) == "forbidden"
    started = host.call(host.as_agent(coordinator, None), "business_create", create_args("repair"))
    assert started.status == "ok", (started.status, started.error)
    repair = host.query("SELECT id FROM businesses WHERE slug='repair'")[0]["id"]
    for business in (repair,):
        assert host.count("agent_profiles", "home_business_id=?", business) == 0
        # The owner's membership is the only human or agent one; registered system workers keep theirs.
        assert [(row["principal_id"], row["is_owner"]) for row in host.query(
            "SELECT m.principal_id, m.is_owner FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
            "WHERE m.business_id=? AND p.kind IN ('human','agent')", business)] == [(host.owner, 1)]
        for identity in (coordinator,):
            assert business in host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business).allowed_business_ids
    assert agent_rows(host) == before and agent_memberships(host) == []
    # Restore returns the resources only; a historical business-home identity stays inactive.
    historical = legacy_business_agent(host, "first", status="inactive")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), host.ids["first"]))
    archived = agent_rows(host)
    with host.database.read() as uow:
        assert not reaches_business(uow.connection, coordinator, host.ids["first"])
    owner = InvocationContext(Principal(host.owner, "human"), allowed_business_ids=frozenset(), source="dashboard",
                              global_capabilities=host.human(None).global_capabilities)
    revision = host.query("SELECT revision FROM businesses WHERE id=?", host.ids["first"])[0]["revision"]
    restored = host.call(owner, "business_restore", {"business_id": host.ids["first"], "rationale": "Back on the menu."},
                         expected_revision=revision)
    assert restored.status == "ok", (restored.status, restored.error)
    assert agent_rows(host) == archived and agent_rows(host)[historical][0] == "inactive"
    assert host.query("SELECT active FROM principals WHERE id=?", historical)[0]["active"] == 0
    with host.database.read() as uow:
        assert reaches_business(uow.connection, coordinator, host.ids["first"])


# -- shared Context writers --------------------------------------------------------------------------------------------------

def test_all_current_globals_instruct_and_edit_the_shared_graph_including_global_under_their_current_policy(host):
    grants = {"colleague": {"context_edit": "allow", "context_instruct": "allow"},
              "learning-applier": {"context_edit": "allow", "context_instruct": "allow"}}
    agents = all_globals(host, grants)
    host.seed([("aaaaa", "Alice")], scope=host.scope("first"), event_id="s0001")
    host.seed([("ccccc", "Global fact")], scope={"kind": "global"}, event_id="s0003")
    for role, identity in agents.items():
        for business in ("first", None):
            data = host.data(host.as_agent(identity, business), "context_instruct", {"text": f"{role} records a fact."})
            job = host.job(data["job"]["id"])
            assert job["kind"] == "context.instruct" and job["business_id"] == (host.ids[business] if business else None)
        queued = host.queued(host.as_agent(identity, None), host.plan(put_node("ccccc", f"Global fact by {role}")))
        assert queued["input"]["state"] == "queued"
        [attribution] = [row["attribution_json"] for row in host.bindings() if row["input_id"] == queued["input"]["id"]]
        assert attribution["principal_id"] == identity and attribution["role_id"] == ("global-worker" if role == "colleague" else role)
    # Policy still applies: an explicit deny wins over the supported role, and non-owner humans stay refused.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", agents["ordubashy"])[0]["revision"]
    host.data(host.human(None), "permissions_set", {"agent_id": agents["ordubashy"], "overrides": {"context_edit": "deny"}},
              expected_revision=revision)
    assert host.refused(host.as_agent(agents["ordubashy"], None), host.plan(put_node("ccccc", "Denied"))) == "policy_denied"
    stranger = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Member"})
        uow.repository("business_memberships", business_id=host.ids["first"]).insert({"principal_id": stranger, "is_owner": False})
    member = InvocationContext(Principal(stranger, "human"), business_id=host.ids["first"],
                               allowed_business_ids=frozenset({host.ids["first"]}), source="dashboard")
    assert host.refused(member, host.plan(put_node("aaaaa", "Member change"))) in {"policy_denied", "forbidden"}


# -- integration readers -----------------------------------------------------------------------------------------------------

def write_settings(host, business="first"):
    hq = host.paths.business_hq(business)
    hq.mkdir(parents=True, exist_ok=True)
    (hq / ".env").write_text("SHARED_KEY=shared\n")
    (hq / ".env.private.json").write_text(json.dumps({"CODER_KEY": {"value": "private", "allowed_roles": ["ordubashy"]}}))


def test_settings_reads_use_derived_access_and_the_exact_environment_policy(host):
    agents = all_globals(host)
    write_settings(host)
    first = host.ids["first"]
    assert agent_memberships(host) == []
    # Ordubashy's role allows environment_get and the private overlay names it; the worker has neither grant.
    shown = host.read(host.as_agent(agents["ordubashy"], "first"), "environment_get", {"key": "CODER_KEY"})
    assert "private" in json.dumps(shown)
    assert host.error(host.as_agent(agents["colleague"], "first"), "environment_get", {"key": "SHARED_KEY"}) in {"policy_denied", "forbidden"}
    # An explicit deny, an inactive identity, a business-home identity and an archived business all fail.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", agents["ordubashy"])[0]["revision"]
    host.data(host.human(None), "permissions_set", {"agent_id": agents["ordubashy"], "overrides": {"environment_get": "deny"}},
              expected_revision=revision)
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", agents["colleague"])[0]["revision"]
    host.data(host.human(None), "agents_deactivate", {"agent_id": agents["colleague"]}, expected_revision=revision)
    legacy = legacy_business_agent(host)
    assert host.error(host.as_agent(agents["ordubashy"], "first"), "environment_get", {"key": "CODER_KEY"}) in {"policy_denied", "forbidden"}
    for identity in (agents["colleague"], legacy):
        with pytest.raises(DomainError):
            host.as_agent(identity, "first")                                  # an inactive or business-home identity has no context
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), first))
    with host.database.read() as uow:
        assert not reaches_business(uow.connection, agents["learning-applier"], first)


# -- generated preparation ---------------------------------------------------------------------------------------------------

def test_preparation_removes_a_previously_managed_ui_qa_connection_and_keeps_foreign_servers(host):
    agent = create_worker(host.instance, "colleague", runtime="claude-code")
    resolver, principal = host.runtime.resolver, Principal(agent, "agent")
    first = prepare_agent(resolver, principal, endpoint="http://127.0.0.1:9988", versions=VERSIONS)
    workspace = first["workspace"]
    mcp = Path(workspace) / ".mcp.json"
    document = json.loads(mcp.read_text())
    document["mcpServers"]["ui-qa"] = {"type": "stdio", "command": "node", "args": ["ui-qa.mjs"]}
    document["mcpServers"]["custom"] = {"type": "stdio", "command": "custom-server"}
    mcp.write_text(json.dumps(document))
    settings = Path(workspace) / ".claude/settings.json"
    enabled = json.loads(settings.read_text())
    enabled["enabledMcpjsonServers"] = [*enabled.get("enabledMcpjsonServers", []), "ui-qa", "custom"]
    settings.write_text(json.dumps(enabled))
    with host.database.transaction() as uow:
        row = uow.connection.execute("SELECT id, projection_json FROM machine_registrations WHERE principal_id=?", (agent,)).fetchone()
        projection = json.loads(row["projection_json"])
        projection["prepared_connections"] = [*projection.get("prepared_connections", []), "ui-qa"]
        uow.connection.execute("UPDATE machine_registrations SET projection_json=? WHERE id=?", (json.dumps(projection), row["id"]))
    again = prepare_agent(resolver, principal, endpoint="http://127.0.0.1:9988", versions=VERSIONS)
    assert again["workspace"] == workspace
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert "ui-qa" not in servers and servers["custom"] == {"type": "stdio", "command": "custom-server"}
    enabled = json.loads(settings.read_text()).get("enabledMcpjsonServers", [])
    assert "ui-qa" not in enabled and "custom" in enabled
    projection = decoded(host.query("SELECT projection_json FROM machine_registrations WHERE principal_id=?", agent)[0]["projection_json"])
    assert "ui-qa" not in projection.get("prepared_connections", [])
    orientation = (Path(workspace) / "CLAUDE.md").read_text()
    assert "ui-qa" not in orientation and "Never edit Qorqut engine or dashboard code" in orientation
