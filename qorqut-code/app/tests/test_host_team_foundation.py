"""Permanent global workers: eligibility, constrained Ordubashy management and who may reach a worker's terminal.

One synthetic host (real assembly, catalog, policy and runtime) with the configured Ordubashy and Learning Applier
registered beside any number of workers that hold the fixture's synthetic ``global-worker`` role. The owner
establishes everything the cases start from; every management case is an actual operation under the actual policy, and the
cases that need a grant the owner would give set it with the real ``permissions_set``/``agents_update``. No tmux, native
session, provider, model or network is used."""

from __future__ import annotations

import sqlite3
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id

from bazarlyk.agents import roles
from bazarlyk.agents.global_scope import active_global
from bazarlyk.agents.operations import WORKER_BARRED_CAPABILITIES, WORKER_DEFAULT_SCOPE, published_role_pin
from bazarlyk.agents.worker_access import is_worker, require_worker_manager
from host_foundation_support import (WORKER_ROLE, build_instance, create_registered_global, create_worker, historical_marketing,
                                     owner_global_context,
                                     publish_role, register_sources, select_coordinator, set_agent_overrides)

MANAGEMENT = {"agents_create": "allow", "agents_update": "allow", "permissions_set": "allow", "agents_deactivate": "allow"}
BASE_CAPABILITIES = ("read_global_context", "manage_ecosystem_agents")
SEQUENCE = itertools.count(1)


def scope(capabilities):
    return {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": list(capabilities)}


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("team-foundation"))
    first = ids["first"]
    register_sources(instance, first, "first")
    created = instance.invoke(owner_global_context(instance), "agents_create", {
        "role": publish_role(instance, "ordubashy"), "plugins": [], "name": "ordubashy", "display_name": "Ordubashy",
        "assignment_brief": "Synthetic coordinator.", "scope": scope(BASE_CAPABILITIES)})
    assert created.status == "ok", created.error
    ordubashy = instance.registered["ordubashy"] = created.data["agent_id"]
    select_coordinator(instance, ordubashy)
    publish_role(instance, WORKER_ROLE)
    host = SimpleNamespace(instance=instance, db=instance.database, first=first, ordubashy=ordubashy,
                           learner=create_registered_global(instance, "learning-applier"), owner=owner_global_context(instance))
    # Agents that hold the management capability and an explicit allow, so a refusal below is the target/grant rule alone.
    host.admin_worker = create_worker(instance, "admin-worker", capabilities=BASE_CAPABILITIES)
    for identity in (host.admin_worker, host.learner):
        grant(host, identity, BASE_CAPABILITIES)
        set_agent_overrides(instance, identity, MANAGEMENT, (first,))
    set_agent_overrides(instance, ordubashy, MANAGEMENT, (first,))
    instance.runtime.freeze_registry()
    yield host
    instance.runtime.stop()


@pytest.fixture(autouse=True)
def configured_ordubashy(host):
    """Every case starts with Ordubashy as configured, with the base capabilities and the management allows."""
    yield
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (host.ordubashy,))
    set_agent_overrides(host.instance, host.ordubashy, MANAGEMENT, (host.first,))
    grant(host, host.ordubashy, BASE_CAPABILITIES)


# ----- helpers ---------------------------------------------------------------------------------------------------------

def agent(host, identity):
    return host.instance.runtime.agent_context(Principal(identity, "agent"))


def revision(host, identity):
    with host.db.read() as uow:
        return uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()[0]


def profile(host, identity):
    with host.db.read() as uow:
        return active_global(uow.connection, identity)


def call(host, ctx, operation, arguments, target=None):
    options = InvocationOptions(expected_revision=revision(host, target) if target else None, idempotency_key=new_id())
    return host.instance.runtime.application.invoke(ctx, operation, arguments, options)


def update(host, ctx, target, **changes):
    return call(host, ctx, "agents_update", {"agent_id": target, "changes": changes}, target)


def permissions(host, ctx, target, overrides):
    return call(host, ctx, "permissions_set", {"agent_id": target, "overrides": overrides}, target)


def grant(host, identity, capabilities):
    """The owner sets an agent's global capabilities (a no-op when they already are those)."""
    if sorted(profile(host, identity)["scope"]["global_capabilities"]) != sorted(capabilities):
        result = update(host, host.owner, identity, scope=scope(capabilities))
        assert result.status == "ok", result.error


def worker(host, capabilities=("read_global_context",)):
    return create_worker(host.instance, f"worker-{next(SEQUENCE)}", capabilities=capabilities)


def refused(result, code="forbidden"):
    codes = (code,) if isinstance(code, str) else code
    assert result.status == "error" and result.error["code"] in codes, (result.status, result.error)
    return result.error


def ok(result):
    assert result.status == "ok", result.error
    return result.data


# ----- repeatable identity ---------------------------------------------------------------------------------------------

def test_two_workers_register_independently_with_uuid_workspaces(host):
    first, second = worker(host), worker(host)
    assert first != second
    with host.db.read() as uow:
        for identity in (first, second):
            data = active_global(uow.connection, identity)
            assert data["role"]["id"] == WORKER_ROLE and is_worker(uow.connection, identity)
            row = uow.connection.execute("SELECT workspace, home_business_id, namespace FROM agent_profiles WHERE agent_id=?",
                                         (identity,)).fetchone()
            assert Path(row["workspace"]) == host.instance.paths.operations / "agents" / identity
            assert (row["home_business_id"], row["namespace"]) == (None, "ecosystem")
        for singleton in (host.ordubashy, host.learner, new_id()):
            assert not is_worker(uow.connection, singleton)


def test_the_singleton_roles_stay_one_active_holder_each(host):
    for role, (name, display) in {"learning-applier": ("learner-2", "Learner Two"), "ordubashy": ("ordubashy-2", "Two")}.items():
        result = host.instance.invoke(host.owner, "agents_create", {
            "role": publish_role(host.instance, role), "plugins": [], "name": name, "display_name": display,
            "assignment_brief": "A duplicate.", "scope": scope(("read_global_context",))})
        assert refused(result, "roster_conflict")["details"]["agent_ids"], role


def test_the_newest_published_role_version_is_found_by_numeric_order():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE artifacts(kind TEXT, artifact_id TEXT, version TEXT, digest TEXT)")
    connection.executemany("INSERT INTO artifacts VALUES (?,?,?,?)", [
        ("role", "global-worker", "1.9.0", "sha256:nine"), ("role", "global-worker", "1.10.0", "sha256:ten"),
        ("role", "global-worker", "1.2.0", "sha256:two"), ("role", "founder", "9.0.0", "sha256:founder"),
        ("plugin", "global-worker", "9.9.9", "sha256:plugin")])
    assert published_role_pin(connection, "global-worker") == {"id": "global-worker", "version": "1.10.0", "digest": "sha256:ten"}
    assert published_role_pin(connection, "missing") is None


# ----- creation ---------------------------------------------------------------------------------------------------------

def test_ordubashy_creates_a_worker_from_a_name_and_an_assignment_alone(host):
    data = ok(call(host, agent(host, host.ordubashy), "agents_create", {"name": "scout", "assignment_brief": "Watch the queue."}))
    stored = profile(host, data["agent_id"])
    with host.db.read() as uow:
        pin = published_role_pin(uow.connection, WORKER_ROLE)
    assert stored["role"] == pin and stored["name"] == stored["display_name"] == "scout"
    assert stored["scope"] == {**WORKER_DEFAULT_SCOPE} and stored["plugins"] == [] and stored["status"] == "active"
    assert stored["runtime"] == profile(host, host.ordubashy)["runtime"] == "codex"      # a worker starts on Ordubashy's runtime
    assert stored["tool_policy"] == {"overrides": {}}


def test_a_worker_can_start_on_either_native_runtime_and_the_default_is_a_floor_not_a_cap(host):
    for runtime in ("claude-code", "codex"):
        data = ok(call(host, agent(host, host.ordubashy), "agents_create", {
            "name": f"runtime-{next(SEQUENCE)}", "assignment_brief": "Either runtime.", "runtime": runtime}))
        assert profile(host, data["agent_id"])["runtime"] == runtime


def test_a_creation_still_names_what_it_is_for(host):
    ordubashy = agent(host, host.ordubashy)
    assert refused(call(host, ordubashy, "agents_create", {"name": "no-brief"}), "validation_error")
    assert refused(call(host, ordubashy, "agents_create", {"assignment_brief": "No name."}), "validation_error")


def test_only_the_owner_establishes_other_roles_and_only_the_configured_ordubashy_adds_workers(host):
    learner_pin = publish_role(host.instance, "learning-applier")
    ordubashy = agent(host, host.ordubashy)
    refused(call(host, ordubashy, "agents_create", {"role": learner_pin, "name": "f2", "display_name": "F2",
                                                    "assignment_brief": "Not a worker.", "scope": scope(("read_global_context",))}))
    for caller in (host.admin_worker, host.learner):                  # capable and allowed, but not the configured Ordubashy
        refused(call(host, agent(host, caller), "agents_create", {"name": "peer", "assignment_brief": "A peer."}))
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    refused(call(host, ordubashy, "agents_create", {"name": "unconfigured", "assignment_brief": "No coordinator."}))
    assert ok(call(host, host.owner, "agents_create", {"name": f"owned-{next(SEQUENCE)}", "assignment_brief": "Owner's own."}))


def test_a_creation_cannot_exceed_what_the_creator_holds(host):
    ordubashy = agent(host, host.ordubashy)
    error = refused(call(host, ordubashy, "agents_create", {"name": "greedy", "assignment_brief": "Too much.",
                                                            "scope": scope(("read_global_context", "manage_calendar"))}))
    assert "exceed" in error["message"] or error.get("details")
    grant(host, host.ordubashy, (*BASE_CAPABILITIES, "manage_calendar"))
    assert ok(call(host, agent(host, host.ordubashy), "agents_create", {
        "name": f"calendar-{next(SEQUENCE)}", "assignment_brief": "May use the calendar.",
        "scope": scope(("read_global_context", "manage_calendar"))}))


# ----- who may reach a worker --------------------------------------------------------------------------------------------

CALLERS = ("owner", "ordubashy", "admin-worker", "learner")
TARGETS = ("worker", "learning-applier", "ordubashy")


def caller_context(host, caller):
    return host.owner if caller == "owner" else agent(host, {"ordubashy": host.ordubashy, "admin-worker": host.admin_worker,
                                                              "learner": host.learner}[caller])


def target_identity(host, kind):
    return worker(host) if kind == "worker" else {"learning-applier": host.learner, "ordubashy": host.ordubashy}[kind]


@pytest.mark.parametrize("operation", ["agents_update", "permissions_set"])
@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("kind", TARGETS)
def test_only_the_owner_and_ordubashy_reach_a_worker_and_ordubashy_reaches_nothing_else(host, caller, kind, operation):
    target = target_identity(host, kind)
    before = revision(host, target)
    ctx = caller_context(host, caller)
    result = (update(host, ctx, target, assignment_brief="Reviewed.") if operation == "agents_update"
              else permissions(host, ctx, target, {"actions_record": "deny"}))
    if caller == "owner" or (caller == "ordubashy" and kind == "worker"):
        assert ok(result)["profile_revision"] == before + 1
    else:
        # Policy may refuse first (Learning Applier holds no agent administration even when allowed); the target rule is the backstop.
        refused(result, ("forbidden", "policy_denied"))
        assert revision(host, target) == before


def test_a_worker_never_administers_itself_or_a_peer(host):
    peer = worker(host)
    admin = agent(host, host.admin_worker)
    refused(update(host, admin, peer, assignment_brief="Not yours."))
    refused(update(host, admin, host.admin_worker, assignment_brief="Not even yours."))
    refused(call(host, admin, "agents_deactivate", {"agent_id": peer}, peer))
    assert profile(host, peer)["status"] == "active"


def test_ordubashy_deactivates_a_worker_and_only_a_worker(host):
    target, ordubashy = worker(host), agent(host, host.ordubashy)
    refused(call(host, ordubashy, "agents_deactivate", {"agent_id": host.learner}, host.learner))
    assert profile(host, host.learner) is not None
    ok(call(host, ordubashy, "agents_deactivate", {"agent_id": target}, target))
    with host.db.read() as uow:
        assert active_global(uow.connection, target) is None and not is_worker(uow.connection, target)
    # An inactive worker is history: nothing manages or starts it any more.
    refused(update(host, agent(host, host.ordubashy), target, assignment_brief="Back again."))


def test_the_shared_manager_check_is_the_one_definition(host):
    target = worker(host)
    with host.db.read() as uow:
        require_worker_manager(host.owner, uow, target)
        require_worker_manager(agent(host, host.ordubashy), uow, target)
        for caller in (host.admin_worker, host.learner, target):
            with pytest.raises(DomainError) as refusal:
                require_worker_manager(agent(host, caller), uow, target)
            assert refusal.value.code == "forbidden"
        for other in (host.learner, host.ordubashy, new_id()):
            with pytest.raises(DomainError):
                require_worker_manager(host.owner, uow, other)          # not even the owner "manages" a non-worker here


# ----- no grant escalation ---------------------------------------------------------------------------------------------

def test_a_worker_cannot_be_given_a_core_tool_mode_above_the_managers(host):
    target = worker(host)
    set_agent_overrides(host.instance, host.ordubashy, {**MANAGEMENT, "tasks_create": "signoff", "notes_create": "deny"}, (host.first,))
    ordubashy = agent(host, host.ordubashy)
    assert list(refused(permissions(host, ordubashy, target, {"tasks_create": "allow"}))["details"]["operations"]) == ["tasks_create"]
    assert list(refused(permissions(host, agent(host, host.ordubashy), target, {"notes_create": "allow"}))["details"]["operations"]) == [
        "notes_create"]
    assert refused(permissions(host, agent(host, host.ordubashy), target, {"notes_create": "signoff"}))
    assert profile(host, target)["tool_policy"] == {"overrides": {}}
    # Equal or lower is fine, and so is a tool the manager holds at the same mode.
    assert ok(permissions(host, agent(host, host.ordubashy), target,
                          {"tasks_create": "signoff", "notes_create": "deny", "actions_record": "allow"}))


def test_an_edit_is_judged_on_the_whole_proposed_grant_not_the_field_it_names(host):
    # Owner-given overrides above the manager's current mode stay as they are; the manager cannot reapply or raise them.
    target = worker(host)
    ok(permissions(host, host.owner, target, {"tasks_create": "allow"}))
    set_agent_overrides(host.instance, host.ordubashy, {**MANAGEMENT, "tasks_create": "signoff"}, (host.first,))
    ok(update(host, agent(host, host.ordubashy), target, assignment_brief="An unrelated edit keeps what is held."))
    ok(permissions(host, agent(host, host.ordubashy), target, {"tasks_create": "allow"}))      # unchanged: nothing is added
    ok(permissions(host, agent(host, host.ordubashy), target, {"tasks_create": "signoff"}))     # a reduction
    refused(permissions(host, agent(host, host.ordubashy), target, {"tasks_create": "allow"}))  # a reraise is an escalation
    refused(permissions(host, agent(host, host.ordubashy), target, {"tasks_create": "allow", "actions_record": "allow"}))


def test_capabilities_pass_down_only_when_held_and_never_the_barred_ones(host):
    target, ordubashy = worker(host), agent(host, host.ordubashy)
    error = refused(update(host, ordubashy, target, scope=scope(("read_global_context", "manage_calendar"))))
    assert list(error["details"]["capabilities"]) == ["manage_calendar"]
    grant(host, host.ordubashy, (*BASE_CAPABILITIES, "manage_calendar", "coordinate_businesses"))
    ordubashy = agent(host, host.ordubashy)
    ok(update(host, ordubashy, target, scope=scope(("read_global_context", "manage_calendar"))))
    # Held by Ordubashy, yet a worker never receives agent administration or the old peer authority.
    for barred in sorted(WORKER_BARRED_CAPABILITIES & {"manage_ecosystem_agents", "coordinate_businesses"}):
        error = refused(update(host, agent(host, host.ordubashy), target, scope=scope(("read_global_context", barred))))
        assert list(error["details"]["capabilities"]) == [barred]
    assert profile(host, target)["scope"]["global_capabilities"] == ["manage_calendar", "read_global_context"]
    # The owner is not bound by the ceiling.
    ok(update(host, host.owner, target, scope=scope(("read_global_context", "manage_ecosystem_agents"))))


def test_plugin_tools_are_not_part_of_the_comparison_and_a_creation_may_carry_a_plugin(host):
    from bazarlyk.bootstrap.seed import apply_initial_plan
    import shutil

    from host_foundation_support import APP

    instance = host.instance
    shutil.copytree(APP / "plugins" / "agent-terminal-control", instance.workspace / "qorqut-code" / "app" / "plugins" / "agent-terminal-control",
                    dirs_exist_ok=True)
    with instance.database.read() as uow:
        plan = instance.runtime.publisher.prepare(host.owner, {"source_id": "agent-terminal-control"},
                                                  InvocationOptions(idempotency_key=new_id()), uow, "plugin")
    plugin = apply_initial_plan(instance.database, host.owner, plan, instance.runtime.publisher,
                                instance.runtime.publisher.resolve_file_target, instance.runtime.locks)["artifact"]
    data = ok(call(host, agent(host, host.ordubashy), "agents_create", {
        "name": f"plugged-{next(SEQUENCE)}", "assignment_brief": "Carries a plugin.", "plugins": [{**plugin, "config": {}}]}))
    assert [item["id"] for item in profile(host, data["agent_id"])["plugins"]] == ["agent-terminal-control"]
    # The plugin's own tools keep their separate source rules: a worker never controls a terminal.
    tools = {row["canonical_operation"]: row for row in instance.runtime.resolver.resolve_agent_config(
        Principal(data["agent_id"], "agent")).data["tools"]}
    assert not tools["agent_terminal_restart"]["available"]


def test_a_prepared_plan_is_rechecked_with_current_authority_when_it_is_validated_and_when_it_is_applied(host):
    target, operations = worker(host), host.instance.runtime.agents
    ctx = agent(host, host.ordubashy)
    set_agent_overrides(host.instance, host.ordubashy, {**MANAGEMENT, "tasks_create": "allow"}, (host.first,))
    options = InvocationOptions(expected_revision=revision(host, target), idempotency_key=new_id())
    with host.db.read() as uow:
        plan = operations.prepare("permissions_set", ctx, {"agent_id": target, "overrides": {"tasks_create": "allow"}}, options, uow)
        operations.validate_targets(ctx, plan, uow)                                  # authorized when it was proposed
    set_agent_overrides(host.instance, host.ordubashy, {**MANAGEMENT, "tasks_create": "signoff"}, (host.first,))
    for step in ("validate", "apply"):
        with pytest.raises(DomainError) as refusal:
            if step == "validate":
                with host.db.read() as uow:
                    operations.validate_targets(ctx, plan, uow)
            else:
                with host.db.transaction() as uow:
                    operations.apply(ctx, plan, uow)
        assert refusal.value.code == "forbidden" and "manager" in refusal.value.message, step
    assert profile(host, target)["tool_policy"] == {"overrides": {}}


def test_a_queued_plan_for_a_worker_fails_closed_when_the_manager_or_target_changes(host):
    target, operations = worker(host), host.instance.runtime.agents
    ctx = agent(host, host.ordubashy)
    options = InvocationOptions(expected_revision=revision(host, target), idempotency_key=new_id())
    with host.db.read() as uow:
        plan = operations.prepare("agents_update", ctx, {"agent_id": target, "changes": {"assignment_brief": "Queued."}}, options, uow)
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    with pytest.raises(DomainError) as refusal:
        with host.db.read() as uow:
            operations.validate_targets(ctx, plan, uow)
    assert refusal.value.code == "forbidden"
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (host.ordubashy,))
    ok(call(host, host.owner, "agents_deactivate", {"agent_id": target}, target))
    with pytest.raises(DomainError) as refusal:
        with host.db.read() as uow:
            operations.validate_targets(ctx, plan, uow)
    assert refusal.value.code == "forbidden"


# ----- exposure and eligibility -------------------------------------------------------------------------------------------

def test_agent_administration_is_exposed_only_with_its_capability(host):
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES  # noqa: F401  (the capability names are a closed set)

    runtime = host.instance.runtime
    names = {descriptor.name for descriptor in runtime.registry.installed() if descriptor.global_capability == "manage_ecosystem_agents"}
    assert {"agents_create", "agents_update", "agents_deactivate", "permissions_set"} <= names
    plain = worker(host)
    ok(permissions(host, host.owner, plain, {name: "allow" for name in names}))      # allowed by policy, still no capability
    rows = {row["canonical_operation"]: row for row in runtime.resolver.resolve_agent_config(Principal(plain, "agent")).data["tools"]}
    for name in names:
        assert not rows[name]["available"] and rows[name]["denial_reason"] == "capability_not_granted", name
        assert call(host, agent(host, plain), name, {}).status == "error"
    rows = {row["canonical_operation"]: row for row in runtime.resolver.resolve_agent_config(Principal(host.ordubashy, "agent")).data["tools"]}
    assert all(rows[name]["available"] for name in ("agents_create", "agents_update", "agents_deactivate", "permissions_set"))


def test_worker_eligibility_grants_no_default_context_write_or_telegram_but_an_explicit_grant_works(host):
    from bazarlyk.composition.policy import TELEGRAM_ROLES
    from bazarlyk.shared_context.service import INSTRUCT_ROLES

    assert WORKER_ROLE in INSTRUCT_ROLES and INSTRUCT_ROLES == roles.SUPPORTED_GLOBAL_ROLES      # eligibility only
    assert TELEGRAM_ROLES == {roles.ORDUBASHY_ROLE}
    target = worker(host)

    def rows():
        return {row["canonical_operation"]: row
                for row in host.instance.runtime.resolver.resolve_agent_config(Principal(target, "agent")).data["tools"]}
    for name in ("context_instruct", "context_edit"):                                  # no default write grant
        assert not rows()[name]["available"] and rows()[name]["denial_reason"] == "policy_denied", name
    ok(permissions(host, host.owner, target, {"context_instruct": "allow", "context_edit": "allow"}))
    assert all(rows()[name]["available"] and rows()[name]["effective_mode"] == "allow" for name in ("context_instruct", "context_edit"))
    ok(permissions(host, host.owner, target, {}))                                       # revoked
    assert not any(rows()[name]["available"] for name in ("context_instruct", "context_edit"))


def test_a_worker_starts_with_ordubashys_codex_model_and_effort_only():
    from bazarlyk.bootstrap.codex_config import ROLE_MODEL_DEFAULTS, role_model_default

    assert role_model_default("ecosystem", WORKER_ROLE) == ROLE_MODEL_DEFAULTS[roles.ORDUBASHY_ROLE]
    assert role_model_default("business", WORKER_ROLE) is None
    assert role_model_default("ecosystem", "learning-applier") == ROLE_MODEL_DEFAULTS["learning-applier"]
    for retired in ("founder", "global-marketing"):                                                         # retired: no default
        assert retired not in ROLE_MODEL_DEFAULTS and role_model_default("ecosystem", retired) is None


# ----- terminal reach ---------------------------------------------------------------------------------------------------

def test_terminal_control_belongs_to_ordubashy_and_the_owner_and_reaches_workers_only(host):
    control = host.instance.runtime.terminal_control
    target = worker(host)
    with host.db.read() as uow:
        assert control.target(agent(host, host.ordubashy), target, uow)["agent_id"] == target
        for kind in (host.learner,):
            assert control.target(host.owner, kind, uow)["agent_id"] == kind       # the owner keeps every global
            with pytest.raises(DomainError) as refusal:
                control.target(agent(host, host.ordubashy), kind, uow)
            assert refusal.value.code == "forbidden"
        for caller in (host.admin_worker, host.learner):
            with pytest.raises(DomainError):
                control.actor(agent(host, caller), uow)
            with pytest.raises(DomainError):
                control.target(agent(host, caller), target, uow)
        with pytest.raises(DomainError):
            control.target(agent(host, host.ordubashy), host.ordubashy, uow)    # never itself


# ----- worker display names and bundled icons --------------------------------------------------------------------------

CATALOG = ("bot", "brain", "sparkles", "zap", "rocket", "flame", "ghost", "cat", "dog", "bird", "fish", "bug", "cpu", "terminal",
           "code", "pen-tool", "megaphone", "calculator", "chart-column", "database", "search", "globe", "shield", "wrench",
           "triangle", "circle", "square", "lightbulb", "flag", "hand")


def read(host, caller, operation, arguments=None):
    return host.instance.runtime.application.invoke(caller, operation, arguments or {}, InvocationOptions(idempotency_key=new_id()))


def test_the_icon_list_is_the_exact_bundled_catalog_and_the_dashboard_draws_every_entry(host):
    import re

    from host_foundation_support import CODE

    data = ok(read(host, agent(host, host.ordubashy), "agents_icons_list"))
    assert [icon["id"] for icon in data["icons"]] == list(CATALOG)
    assert all(set(icon) == {"id", "label"} and icon["label"] for icon in data["icons"])
    # The glyphs already live in the dashboard's trusted AICONS table; the backend carries only IDs.
    source = (CODE / "biz-dashboard" / "js" / "core.js").read_text()
    start = source.index("const AICONS = {")
    table = source[start:source.index("\n};", start)]
    drawn = set(re.findall(r'^\s*"?([a-z0-9-]+)"?:\s*[\'"]<', table, re.M))
    assert set(CATALOG) <= drawn


def test_the_icon_list_follows_the_ordinary_agent_list_grant(host):
    target = worker(host)
    refused(read(host, agent(host, target), "agents_icons_list"), ("policy_denied", "forbidden"))
    ok(permissions(host, host.owner, target, {"agents_list": "allow"}))
    assert len(ok(read(host, agent(host, target), "agents_icons_list"))["icons"]) == len(CATALOG)
    ok(permissions(host, host.owner, target, {"agents_list": "deny"}))
    refused(read(host, agent(host, target), "agents_icons_list"), ("policy_denied", "forbidden"))


def test_new_worker_persists_a_default_icon_across_renames(host):
    identity = worker(host)
    assert profile(host, identity)["presentation"]["icon"] == "bot"
    ok(update(host, agent(host, host.ordubashy), identity, display_name="New Name"))
    assert profile(host, identity)["presentation"]["icon"] == "bot"


def test_ordubashy_names_a_worker_and_picks_a_bundled_icon_that_survives_rename(host):
    data = ok(call(host, agent(host, host.ordubashy), "agents_create", {
        "name": f"named-{next(SEQUENCE)}", "assignment_brief": "Has a face.", "display_name": "Nova",
        "presentation": {"icon": "rocket"}}))
    identity = data["agent_id"]
    before = profile(host, identity)
    assert (before["display_name"], before["presentation"]) == ("Nova", {"icon": "rocket"})
    with host.db.read() as uow:
        workspace = uow.connection.execute("SELECT workspace FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()[0]
    ok(update(host, agent(host, host.ordubashy), identity, display_name="Nova Prime"))
    ok(update(host, agent(host, host.ordubashy), identity, presentation={"icon": "megaphone"}))
    ok(update(host, agent(host, host.ordubashy), identity, display_name="Nova Final"))
    after = profile(host, identity)
    assert (after["display_name"], after["presentation"]["icon"], after["agent_id"]) == ("Nova Final", "megaphone", identity)
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT workspace FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()[0] == workspace


@pytest.mark.parametrize("icon", ["not-an-icon", "Rocket", "<svg onload=alert(1)></svg>", "https://example.test/icon.svg",
                                  "data:image/svg+xml;base64,PHN2Zz4=", "../rocket", ""])
def test_a_new_or_changed_worker_icon_must_be_a_bundled_id(host, icon):
    ordubashy = agent(host, host.ordubashy)
    refused(call(host, ordubashy, "agents_create", {"name": f"bad-{next(SEQUENCE)}", "assignment_brief": "No.",
                                                    "presentation": {"icon": icon}}), "validation_error")
    target = worker(host)
    refused(update(host, ordubashy, target, presentation={"icon": icon}), "validation_error")
    refused(update(host, host.owner, target, presentation={"icon": icon}), "validation_error")
    assert profile(host, target)["presentation"]["icon"] == "bot"


def test_a_stored_worker_icon_is_never_rewritten_and_other_roles_keep_free_form_presentation(host):
    legacy = worker(host)
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.presentation',"
                               "json('{\"icon\":\"business\"}')) WHERE agent_id=?", (legacy,))
    ok(update(host, agent(host, host.ordubashy), legacy, display_name="Still Legacy"))        # unchanged icon: not revalidated
    assert profile(host, legacy)["presentation"] == {"icon": "business"}
    refused(update(host, agent(host, host.ordubashy), legacy, presentation={"icon": "business-2"}),
            "validation_error")
    ok(update(host, host.owner, host.learner, presentation={"icon": "lightbulb"}))
    ok(update(host, host.owner, host.learner, presentation={"icon": "not-in-the-worker-catalog"}))


# ----- private runtime gates: calendar, account email ------------------------------------------------------------------

def gates(host):
    from bazarlyk.calendar.operations import CalendarOperations
    from bazarlyk.domains.account_email import account_authority

    runtime = host.instance.runtime

    def calendar(identity):
        with host.db.read() as uow:
            CalendarOperations(runtime).access(agent(host, identity), uow)

    def account(identity):
        with host.db.read() as uow:
            account_authority(agent(host, identity), uow, runtime.resolver)

    return {"calendar": calendar, "account": account}


@pytest.mark.parametrize("gate,capability", [("calendar", "manage_calendar"), ("account", "manage_fastmail")])
def test_private_calendar_and_account_gates_admit_a_worker_only_with_the_explicit_capability(host, gate, capability):
    check = gates(host)[gate]
    plain = worker(host)
    with pytest.raises(DomainError) as denied:
        check(plain)
    assert denied.value.code == "forbidden"
    granted = worker(host, capabilities=("read_global_context", capability))
    check(granted)                                                  # the capability on a worker is the whole grant
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (granted,))
    with pytest.raises(DomainError):
        check(granted)                                              # a deactivated worker loses it at once


@pytest.mark.parametrize("gate,capability", [("calendar", "manage_calendar"), ("account", "manage_fastmail")])
def test_the_private_gates_still_refuse_every_other_role_that_holds_the_capability(host, gate, capability):
    check = gates(host)[gate]
    for identity in (host.learner,):
        before = profile(host, identity)["scope"]["global_capabilities"]
        grant(host, identity, sorted({*before, capability}))
        try:
            with pytest.raises(DomainError) as denied:
                check(identity)
            assert denied.value.code == "forbidden", identity
        finally:
            grant(host, identity, before)


# ----- generated guidance ----------------------------------------------------------------------------------------------

PREPARE_VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.156.1"}


def test_generated_guidance_selects_the_permanent_team_and_sends_a_worker_to_ordubashy(tmp_path, monkeypatch):
    import yaml
    from bazarlyk.bootstrap import diagnostics
    from bazarlyk.bootstrap.preparation import prepare_agent

    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    monkeypatch.setattr(diagnostics, "prepared_helpers", lambda *_: {"ready": True})
    instance, ids = build_instance(tmp_path)
    try:
        register_sources(instance, ids["first"], "first")
        for role in ("ordubashy", WORKER_ROLE):                      # the actual roles declare the allowlisted Outreach connection
            declared = yaml.safe_load((instance.paths.component("roles") / "roles" / role / "role.yaml").read_text())
            if "outreach" not in (declared.get("external_connections") or []):
                pytest.skip(f"the role source's {role} does not declare Outreach (needs the completed role artifact)")
        ordubashy = create_registered_global(instance, "ordubashy")
        member = create_worker(instance, "guided-worker")

        def guidance(identity):
            result = prepare_agent(instance.runtime.resolver, Principal(identity, "agent"), endpoint="http://127.0.0.1:9988",
                                   versions=PREPARE_VERSIONS)
            workspace = Path(result["workspace"])
            return {name: (workspace / name).read_text() for name in ("AGENTS.md", "CLAUDE.md")}

        manager, teammate = guidance(ordubashy), guidance(member)
        text = "".join(manager.values())
        assert "means a permanent global worker by default" in text and "agent_work_update" in text
        assert "have the worker verify its result and generally trust its completion report" in text
        assert "verify what it reports" not in text
        assert "If the selected worker's lifecycle or a needed tool is unavailable" in text
        assert "If durable dispatch or a needed worker tool is unavailable" not in text      # legacy dispatch is scoped
        assert "A legacy delegated assignment applies only when the owner or Task explicitly" in text
        assert "means a durable Qorqut assignment for" not in text           # the old unscoped routing is gone
        assert "native Codex subagent" in text and "do not substitute native helpers" in text      # runtime rules kept
        assert "Global Marketing" not in text and "Report this to the owner; ordinary work is unaffected." in text
        mate = "".join(teammate.values())
        assert "Ask Ordubashy about cold-email campaigns; ordinary work is unaffected." in mate
        assert "Global Marketing" not in mate and "Operational delegation" not in mate      # a worker is not a delegator
        for each in (text, mate):                                   # the ownership boundary admits Ordubashy's assignment only
            assert "Ordubashy may pass that owner-authorized implementation to a permanent global worker" in each
            assert "no further owner message is needed" in each
            assert "does not assign it to you" in each and "Never edit Qorqut engine or dashboard code" in each
            assert "that alone never authorizes publication, adoption, access or permission changes, merging, pushing" in each
    finally:
        instance.runtime.stop()


def test_a_worker_profile_is_a_global_browser_surface_but_a_business_role_is_not():
    from bazarlyk.service.browser_surfaces import GLOBAL_ROLES, global_profile

    assert WORKER_ROLE in GLOBAL_ROLES
    assert global_profile({"scope": {"kind": "ecosystem"}, "role": {"id": WORKER_ROLE}})
    assert not global_profile({"scope": {"kind": "business"}, "role": {"id": WORKER_ROLE}})
    assert not global_profile({"scope": {"kind": "ecosystem"}, "role": {"id": "business-agent"}})


# ----- persisted model settings, retired Marketing, workspace eligibility -------------------------------------------

def _prepared_instance(tmp_path, monkeypatch):
    from bazarlyk.bootstrap import diagnostics

    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    monkeypatch.setattr(diagnostics, "prepared_helpers", lambda *_: {"ready": True})
    instance, ids = build_instance(tmp_path)
    register_sources(instance, ids["first"], "first")
    return instance


def _prepare(instance, identity):
    from bazarlyk.bootstrap.preparation import prepare_agent

    result = prepare_agent(instance.runtime.resolver, Principal(identity, "agent"), endpoint="http://127.0.0.1:9988",
                           versions=PREPARE_VERSIONS)
    return Path(result["workspace"])


def _settings(workspace):
    import tomllib

    document = tomllib.loads((workspace / ".codex/config.toml").read_text())
    return document.get("model"), document.get("model_reasoning_effort")


def _choose(workspace, model, effort, **foreign):
    """The operator's persisted choice in the generated Codex file, beside content that must never be inherited."""
    import tomllib

    import tomli_w

    path = workspace / ".codex/config.toml"
    document = tomllib.loads(path.read_text())
    document["model"], document["model_reasoning_effort"] = model, effort
    document.update(foreign)
    path.write_text(tomli_w.dumps(document))


def test_a_worker_inherits_ordubashys_persisted_model_once_and_then_keeps_its_own(tmp_path, monkeypatch):
    from bazarlyk.bootstrap.codex_config import ROLE_MODEL_DEFAULTS

    instance = _prepared_instance(tmp_path, monkeypatch)
    try:
        manager = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
        default = ROLE_MODEL_DEFAULTS[roles.ORDUBASHY_ROLE]
        manager_workspace = _prepare(instance, manager)
        assert _settings(manager_workspace) == default
        early = _prepare(instance, create_worker(instance, "early"))
        assert _settings(early) == default                                     # nothing selected: the runtime default
        _choose(manager_workspace, "manager-model", "medium", api_key="never-copied", mcp_servers={"private": {"command": "x"}})
        fresh = _prepare(instance, create_worker(instance, "fresh"))
        assert _settings(fresh) == ("manager-model", "medium")                 # a new worker inherits the selection ...
        import tomllib
        copied = tomllib.loads((fresh / ".codex/config.toml").read_text())
        assert "api_key" not in copied and "private" not in copied["mcp_servers"]   # ... and only those two keys
        _choose(fresh, "worker-model", "low")
        _choose(manager_workspace, "later-model", "high")
        _prepare(instance, instance.workers["fresh"])
        assert _settings(fresh) == ("worker-model", "low")                     # re-preparation keeps the worker's own choice
        assert _settings(_prepare(instance, instance.workers["early"])) == default
        assert _settings(_prepare(instance, create_worker(instance, "later"))) == ("later-model", "high")
        assert _settings(_prepare(instance, manager)) == default               # Ordubashy's own behavior is unchanged
        (manager_workspace / ".codex/config.toml").unlink()                    # nothing persisted anywhere: the default again
        assert _settings(_prepare(instance, create_worker(instance, "bare"))) == default
        learner = create_registered_global(instance, "learning-applier")
        assert _settings(_prepare(instance, learner)) == ROLE_MODEL_DEFAULTS["learning-applier"]
    finally:
        instance.runtime.stop()


def _revision(instance, identity):
    with instance.database.read() as uow:
        return uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()[0]


def test_global_marketing_is_retired_no_new_registration_or_switch(tmp_path):
    instance, ids = build_instance(tmp_path)               # no Marketing is registered, so only the retirement fence can refuse
    try:
        register_sources(instance, ids["first"], "first")
        pin, owner = {"id": "global-marketing", "version": "1.11.0", "digest": "sha256:" + "0" * 64}, owner_global_context(instance)
        arguments = {"role": pin, "name": "spawn", "assignment_brief": "No.", "scope": scope(("read_global_context",)),
                     "plugins": [], "display_name": "Spawn"}
        refused(instance.invoke(owner, "agents_create", arguments), "retired_role")        # even the owner cannot create it
        member = create_worker(instance, "switch-candidate")
        with instance.database.read() as uow:
            revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (member,)).fetchone()[0]
        switched = instance.runtime.application.invoke(
            owner, "agents_update", {"agent_id": member, "changes": {"role": pin}},
            InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
        refused(switched, "retired_role")
        with instance.database.read() as uow:
            assert active_global(uow.connection, member)["role"]["id"] == WORKER_ROLE
        description = instance.runtime.registry.get("agents_create").description
        assert "retired roles cannot be registered or switched to" in description
        assert "cannot exceed the manager's own grants" not in description and "published plugins are selected independently" in description
    finally:
        instance.runtime.stop()


def test_an_existing_historical_marketing_profile_is_only_closed(host):
    historical = historical_marketing(host.instance, retained=True)
    def status():
        with host.db.read() as uow:
            return uow.connection.execute("SELECT status FROM agent_profiles WHERE agent_id=?", (historical,)).fetchone()[0]

    assert status() == "active"
    refused(call(host, host.owner, "agents_update", {"agent_id": historical, "changes": {"assignment_brief": "Edited."}}, historical),
            "retired_role")                                                      # a retired role is never edited again ...
    ok(call(host, host.owner, "agents_deactivate", {"agent_id": historical}, historical))      # ... only closed
    assert status() == "inactive"
