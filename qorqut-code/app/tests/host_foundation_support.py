"""Synthetic host-foundation instances for the focused acceptance cases.

Everything lives under a pytest ``tmp_path``: an instance root, a protected-configuration workspace holding a copy of
the reusable role-source repository, and component locations pointing at this checkout's own children. No operational
data, configuration, credential or registration is read, and no model, provider, browser or network is used.
"""

from __future__ import annotations

import json
import shutil
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from instance_support import CODE, synthetic_paths, workspace_root
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, utc_now

APP = CODE / "app"
# The authored singleton global roles: one registered holder each. Businesses have no resident agents. Founder and Global
# Marketing are retired: only ``historical_founder`` seeds a retained one, for attribution and rejection checks.
SUPPORTED_GLOBAL_ROLES = ("ordubashy", "learning-applier")
# The one repeatable global role: any number of permanent workers hold it. Its role package is supplied by the Skills work; the
# fixtures below synthesise a minimal one inside the test workspace's role-source *copy* when the checkout does not carry it.
WORKER_ROLE = "global-worker"
WORKER_MANIFEST = """schema_version: 1
id: global-worker
version: 1.0.0
scope_kinds: [ecosystem]
brief: roles/global-worker/BRIEF.md
scope_brief: ecosystem-agent
skills: []
default_tool_policy:
  actions_config_get: allow
  actions_get: allow
  actions_list: allow
  actions_record: allow
  agent_config_get: allow
"""
# Each one's registration name and display name, as the four named global workspaces register them.
REGISTRATION_NAMES = {"ordubashy": ("ordubashy", "Ordubashy"), "learning-applier": ("learning-applier", "Learning Applier")}


# A minimal Ordubashy manifest for checks whose subject is not the role package (the authored role source is owned and versioned
# elsewhere). It is written only into the test workspace's role-source *copy*, never the original.
SYNTHETIC_ORDUBASHY_MANIFEST = """schema_version: 1
id: ordubashy
version: 9.0.0
scope_kinds: [ecosystem]
brief: roles/imported-roles/native/roles/ordubashy/BRIEF.md
scope_brief: ecosystem-agent
skills: []
default_tool_policy:
  actions_config_get: allow
"""


def synthetic_ordubashy(instance, allow=()) -> None:
    """Replace Ordubashy's manifest inside this instance's role-source copy, before the role is first published.

    ``allow`` names further operations the role's default tool policy allows (a check about their boundary needs them)."""
    manifest = SYNTHETIC_ORDUBASHY_MANIFEST + "".join(f"  {name}: allow\n" for name in allow)
    (Path(instance.workspace) / "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles/native/roles/ordubashy/role.yaml").write_text(manifest)


def role_root() -> Path:
    """The canonical distribution defaults root used for active native roles."""
    return Path(os.environ.get("QORQUT_TEST_ROLE_ROOT", workspace_root() / "qorqut-agentic/src/qorqut_agentic/resources/defaults"))


def roles_source():
    source = role_root()
    if not (source / "roles/imported-roles/native/roles/ordubashy/role.yaml").is_file():
        pytest.skip("the workspace has no canonical agentic defaults")
    return source


def legacy_roles_source():
    """Historical repository, only for tests that explicitly verify its history."""
    source = workspace_root() / "qorqut-roles/agent-roles"
    if not (source / "roles/ordubashy/role.yaml").is_file():
        pytest.skip("the historical role source is unavailable")
    return source


def make_workspace(tmp_path: Path) -> Path:
    """Copy the canonical distribution as the active native role source."""
    workspace = tmp_path / "workspace"
    source = roles_source()
    copy = workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
    shutil.copytree(source, copy)
    return workspace


def make_paths(tmp_path: Path, name="instance"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return synthetic_paths(root, roles=tmp_path / "workspace/qorqut-agentic/src/qorqut_agentic/resources/defaults")


def snapshot(connection, tables):
    """Row counts of the named tables, to compare a database before and after a refused or rolled-back action."""
    return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}


def tree(path: Path):
    """Every path below ``path`` (relative, sorted); the empty list for a missing directory."""
    path = Path(path)
    return sorted(str(item.relative_to(path)) for item in path.rglob("*")) if path.is_dir() else []


@dataclass
class Instance:
    """A fully built synthetic host: paths, an initialised assembled database and the retained runtime."""

    paths: object
    workspace: Path
    database: object
    runtime: object
    feed: object
    owner: str
    business: str
    role_pins: dict = field(default_factory=dict)          # role -> its one published artifact pin
    registered: dict = field(default_factory=dict)         # role -> its one registered global identity
    workers: dict = field(default_factory=dict)            # name -> a registered global-worker identity

    def human(self, business=None, **options):
        business = business or self.business
        return InvocationContext(Principal(self.owner, "human"), business_id=business,
                                 allowed_business_ids=frozenset({business}), source="dashboard", **options)

    def invoke(self, ctx, name, arguments, key="new"):
        options = InvocationOptions(idempotency_key=new_id() if key == "new" else key)
        return self.runtime.application.invoke(ctx, name, arguments, options)


def build_instance(tmp_path: Path, *, feed=None, businesses=("first",), register=True, extra=(), without=(), owned=True,
                   extensions=None, agentic_sources=(), paths_factory=None, context=None):
    """Initialise an empty synthetic target explicitly, register an owner and businesses, and build the runtime.

    The runtime is assembled with mutation admission refused, like production; ``owned`` (the default) takes the instance
    through the owned entry (ownership, target validation, admission) without starting any service, so a check can mutate.
    ``owned=False`` leaves the runtime exactly as assembly returns it.

    ``paths_factory`` builds the explicit instance paths instead of the default checkout-bound ones (a check whose subject is the
    verified source identity binds the components to disposable clean Git copies).

    ``extra`` are further trusted contributions composed after the host's own, for a check of a piece whose contribution the
    installed composition does not carry yet. ``without`` names contribution owners to leave out, for a check of a deliberately
    incomplete custom assembly. ``extensions`` is the startup ``ExtensionSet`` the store and runtime are composed for (none by
    default, a core-only host). ``context`` is the optional ``ContextSettings`` the runtime composes the Context with."""
    from bazarlyk.extensions import NO_EXTENSIONS
    from bazarlyk.composition.assembly import HostComposition, host_assembly, host_contributions, initialize_empty_target
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.runtime import build_runtime

    workspace = make_workspace(tmp_path)
    paths = paths_factory(tmp_path) if paths_factory is not None else make_paths(tmp_path)
    feed = feed if feed is not None else ChangeFeed()
    extensions = NO_EXTENSIONS if extensions is None else extensions
    if extra or without:
        composition = HostComposition()
        for contribution in (*(c for c in host_contributions(feed, extensions) if getattr(c, "owner", None) not in without), *extra):
            composition.add(contribution)
        assembly = composition.freeze()
    else:
        assembly = host_assembly(feed, extensions)
    database = initialize_empty_target(paths, assembly, extensions)
    owner, ids = new_id(), {}
    if register:
        with database.transaction() as uow:
            now = utc_now()
            uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
            uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                                   "owner_principal_id": owner, "created_at": now, "updated_at": now})
            for slug in businesses:
                ids[slug] = new_id()
                uow.repository("businesses").insert({
                    "id": ids[slug], "slug": slug, "name": slug.title(), "workspace": str(paths.business_root(slug)),
                    "hq_path": str(paths.business_hq(slug))})
                uow.repository("business_memberships", business_id=ids[slug]).insert(
                    {"principal_id": owner, "is_owner": True})
    from agentic_installation_support import provision
    provision(paths, selected=extensions.ids, roles=workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults", optional=agentic_sources)
    runtime = build_runtime(workspace, paths=paths, database=database, extensions=extensions, context=context)
    if owned:
        runtime.own_instance()
    return Instance(paths, workspace, database, runtime, feed, owner, ids.get(businesses[0]) if ids else None), ids


# Explicit synthetic ordinary project documents. Creating a project registers none (it is empty); a fixture that needs an existing
# product, customer, brand or positioning document makes one here, as an owner or agent would have, never through production.
PROJECT_DOCUMENTS = {
    "product": "# Product\n\n## Agent Brief\nSynthetic fixture product direction.\n",
    "icp": "# Customer\n\n## Agent Brief\nSynthetic fixture customer description.\n",
    "brand": "# Brand\n\n## Agent Brief\nSynthetic fixture brand voice.\n",
    "positioning": "# Positioning\n\n## Agent Brief\nSynthetic fixture positioning.\n",
}


def register_project_documents(instance: Instance, business: str, slug: str):
    """Explicit synthetic ordinary documents of one project: files under its ``hq/context`` and their registered records."""
    from bazarlyk.bootstrap.preparation import _write_file
    from bazarlyk.catalog.packages import content_hash
    from qorqut_operations.document_history.files import seed_document

    rows = []
    for key, text in PROJECT_DOCUMENTS.items():
        relative, body = f"context/{key}.md", text.encode()
        path = instance.paths.business_hq(slug) / relative
        if not path.exists():
            _write_file(path, body)
        with instance.database.transaction() as uow:
            repository = uow.repository("documents", business_id=business)
            existing = repository.list(filters={"namespace": business, "path": relative}, limit=1)
            if existing:
                rows.append(existing[0])
                continue
            row = repository.insert({"id": new_id(), "namespace": business, "path": relative, "kind": "context",
                                     "content_hash": content_hash(path.read_bytes()),
                                     "metadata_json": {"bootstrap_key": key, "authored": True}})
            seed_document(uow.connection, row, path.read_bytes(), author="system:test-fixture", operation="register_project_documents")
            rows.append(row)
    return rows


def register_sources(instance: Instance, business: str, slug: str):
    """The packaged global instructions as host registration seeds them, and one project's explicit synthetic documents."""
    from bazarlyk.bootstrap.seed import register_context_sources

    register_context_sources(instance.database, instance.paths)
    register_project_documents(instance, business, slug)


def owner_global_context(instance: Instance):
    """The installation owner in the Global target, holding every global capability (the owner's registration surface)."""
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES

    return InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")


def publish_role(instance: Instance, role: str) -> dict:
    """Publish one supported global role's authored source through the real catalog, once per role; its exact artifact pin."""
    from bazarlyk.bootstrap.seed import apply_initial_plan

    if role not in instance.role_pins:
        runtime, owner = instance.runtime, owner_global_context(instance)
        with instance.database.read() as uow:
            plan = runtime.publisher.prepare(owner, {"source_id": role}, InvocationOptions(idempotency_key=f"seed-{role}"), uow, "role")
        instance.role_pins[role] = apply_initial_plan(instance.database, owner, plan, runtime.publisher,
                                                      runtime.publisher.resolve_file_target, runtime.locks)["artifact"]
    return instance.role_pins[role]


def create_registered_global(instance: Instance, role: str) -> str:
    """The one registered global agent holding the supported ``role``, created once by the owner through the real catalog and
    ``agents_create``: ecosystem scope, no home, the role's own runtime default and no extra grant. Its business reach is every
    active business, derived per transaction (the saved list stays empty). The roster has one holder per role, so a repeated
    request deliberately returns that first identity; a caller never gets another role by asking again."""
    if role not in SUPPORTED_GLOBAL_ROLES:
        raise ValueError(f"{role!r} is not a supported global role")
    if role not in instance.registered:
        name, display_name = REGISTRATION_NAMES[role]
        created = instance.invoke(owner_global_context(instance), "agents_create", {
            "role": publish_role(instance, role), "plugins": [], "name": name, "display_name": display_name,
            "assignment_brief": "Synthetic fixture agent.",
            "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}})
        assert created.status == "ok", created.error
        instance.registered[role] = created.data["agent_id"]
    return instance.registered[role]


FOUNDER_CAPABILITIES = ("manage_ideas", "read_global_context")


def retained_role_pin(instance: Instance, role: str, version: str) -> dict:
    """HISTORICAL SETUP: the artifact of a retired role that a live installation still retains; publishing it is fenced now.

    An installation that ran the role keeps the immutable artifact, which the canonical deactivation path still resolves. The
    fixture publishes a minimal synthetic package under that identity once (the authored source is gone from the role
    repository), with the retirement fence lifted for this call only and the source registered only for it, never through a
    live creation or publication path."""
    from unittest import mock

    from bazarlyk.catalog import operations as catalog_operations

    if role not in instance.role_pins:
        source = Path(instance.workspace) / f"retained-{role}"
        (source / f"roles/{role}").mkdir(parents=True)
        (source / f"roles/{role}/BRIEF.md").write_text(f"Historical {role} brief, retained only as an immutable artifact.\n")
        (source / f"roles/{role}/role.yaml").write_text(
            f"schema_version: 1\nid: {role}\nversion: {version}\nscope_kinds: [ecosystem]\nbrief: roles/{role}/BRIEF.md\n"
            "skills: []\nscope_brief: ecosystem-agent\ndefault_tool_policy:\n  notes_list: allow\n")
        sources = instance.runtime.publisher.sources
        sources[role] = ("role", source, f"roles/{role}/role.yaml")
        try:
            with mock.patch.object(catalog_operations, "RETIRED_ROLE_IDS", frozenset()):
                publish_role(instance, role)
        finally:
            del sources[role]
    return instance.role_pins[role]


def retained_founder_pin(instance: Instance) -> dict:
    return retained_role_pin(instance, "founder", "2.6.0")


def historical_founder(instance: Instance, *, status="active", principal_active=True, capabilities=FOUNDER_CAPABILITIES,
                      retained=False) -> str:
    """HISTORICAL SETUP: a retained Founder registration as v1 stored it, seeded only for attribution, continuation and rejection.

    Founder is retired: nothing creates, publishes, prepares or authenticates it, so a fixture that needs the old identity makes
    the isolated rows here rather than through a live path. ``status`` may still be an unmigrated ``active`` (it must never
    operate) or ``inactive`` (the deactivated end state the cutover reaches). The role pin is the retained immutable one; no
    artifact is published unless ``retained`` seeds the one a live installation keeps. The named ``founder`` workspace mapping is
    kept, as the historical registration used it."""
    pin = retained_founder_pin(instance) if retained else {"id": "founder", "version": "2.6.0", "digest": "sha256:" + "0" * 64}
    return _historical_global(instance, "founder", "Founder", pin, status, principal_active, capabilities)


def historical_marketing(instance: Instance, *, status="active", principal_active=True, capabilities=("read_global_context",),
                         retained=False) -> str:
    """HISTORICAL SETUP: a retained Global Marketing registration as it was stored before the role was retired.

    Like the Founder it is retired: nothing registers, switches to, prepares or authenticates it, so a fixture that needs the old
    identity (attribution, rejection, deactivation) makes the isolated rows here. The role pin names an artifact that is not
    published unless ``retained`` seeds the one a live installation keeps."""
    pin = (retained_role_pin(instance, "global-marketing", "1.11.0") if retained
           else {"id": "global-marketing", "version": "1.11.0", "digest": "sha256:" + "0" * 64})
    return _historical_global(instance, "global-marketing", "Global Marketing", pin, status, principal_active, capabilities,
                              name="marketing")


def _historical_global(instance, key, display_name, pin, status, principal_active, capabilities, *, name=None) -> str:
    if key in instance.registered:
        return instance.registered[key]
    name = name or key
    identity, now = new_id(), utc_now()
    with instance.database.transaction() as uow:
        template = json.loads(uow.connection.execute(
            "SELECT profile_json FROM agent_profiles WHERE status='active' AND home_business_id IS NULL ORDER BY created_at LIMIT 1"
        ).fetchone()["profile_json"])
        profile = {**template, "agent_id": identity, "profile_revision": 1, "name": name, "display_name": display_name,
                   "status": status, "runtime": "claude-code",
                   "role": pin,
                   "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": list(capabilities)},
                   "assignment_brief": "Historical.", "plugins": [], "tool_policy": {"overrides": {}}}
        uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": display_name, "active": principal_active})
        uow.connection.execute(
            "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, status, profile_json, "
            "created_at, updated_at) VALUES (?,?,NULL,'ecosystem',?,?,?,?,?,?)",
            (identity, identity, name, str(instance.paths.operations / "agents" / name), status, json.dumps(profile), now, now))
    instance.registered[key] = identity
    return identity


def create_worker(instance: Instance, name: str, *, runtime=None, plugins=(), capabilities=("read_global_context",)) -> str:
    """One more permanent global worker, created once per ``name`` by the owner through the real catalog and ``agents_create``
    with the fixture's synthetic worker role. Unlike the singleton roles, any number of workers register."""
    if name not in instance.workers:
        arguments = {"role": publish_role(instance, WORKER_ROLE), "plugins": list(plugins), "name": name,
                     "display_name": name.title(), "assignment_brief": "Synthetic worker.",
                     "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": list(capabilities)}}
        if runtime:
            arguments["runtime"] = runtime
        created = instance.invoke(owner_global_context(instance), "agents_create", arguments)
        assert created.status == "ok", created.error
        instance.workers[name] = created.data["agent_id"]
    return instance.workers[name]


def global_agent_without_capability(instance: Instance, role: str) -> str:
    """The registered global agent of ``role`` whose stored scope no longer holds ``read_global_context``.

    Registration refuses an ecosystem agent without that capability and no operation edits it afterwards, so the case makes the
    agent normally and then clears the capability in its stored scope, the way a revoked grant would read. The worker role stands
    for a worker named ``bare``."""
    identity = create_worker(instance, "bare") if role == WORKER_ROLE else create_registered_global(instance, role)
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, '$.scope.global_capabilities', json('[]')) "
                               "WHERE agent_id=?", (identity,))
    return identity


def grant_capabilities(instance: Instance, agent_id: str, *capabilities: str) -> None:
    """The owner adds global capabilities to one registered agent through the real ``agents_update`` scope edit."""
    _edit_capabilities(instance, agent_id, lambda held: {*held, *capabilities})


def revoke_capabilities(instance: Instance, agent_id: str, *capabilities: str) -> None:
    """The owner removes global capabilities from one registered agent through the same real scope edit."""
    _edit_capabilities(instance, agent_id, lambda held: set(held) - set(capabilities))


def _edit_capabilities(instance: Instance, agent_id: str, edit) -> None:
    with instance.database.read() as uow:
        row = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
    scope = json.loads(row["profile_json"])["scope"]
    scope = {**scope, "global_capabilities": sorted(edit(scope["global_capabilities"]))}
    result = instance.runtime.application.invoke(owner_global_context(instance), "agents_update",
                                                 {"agent_id": agent_id, "changes": {"scope": scope}},
                                                 InvocationOptions(expected_revision=row["revision"], idempotency_key=new_id()))
    assert result.status == "ok", result.error


def select_coordinator(instance: Instance, agent_id: str):
    """The owner selects the registered Ordubashy as the installation's management coordinator (the real operation)."""
    selected = instance.invoke(owner_global_context(instance), "management_coordinator_set", {"coordinator_id": agent_id})
    assert selected.status == "ok", selected.error
    return agent_id


def historical_business_agent(instance: Instance, business_id: str, *, status="active", role="business-agent", pin=None) -> str:
    """HISTORICAL / INVALID: a business-home identity exactly as v1 stored it, seeded only for rejection and history checks.

    Businesses have no resident agents and nothing creates one now; these are isolated rows (an agent principal, a profile
    rehomed from a registered global's stored shape, and its old membership). ``status`` may still be unmigrated (``active``);
    ``pin`` is the exact historical role artifact it holds, when one is stored. It never operates, whatever is stored."""
    identity, now = new_id(), utc_now()
    with instance.database.transaction() as uow:
        template = json.loads(uow.connection.execute(
            "SELECT profile_json FROM agent_profiles WHERE status='active' AND home_business_id IS NULL ORDER BY created_at LIMIT 1"
        ).fetchone()["profile_json"])
        profile = {**template, "agent_id": identity, "profile_revision": 1, "name": "onbashy", "display_name": "Onbashy",
                   "status": "active" if status == "active" else "inactive", "runtime": "claude-code",
                   "role": dict(pin) if pin else {**template["role"], "id": role},
                   "scope": {"kind": "business", "business_id": business_id}, "assignment_brief": "Historical.", "plugins": [],
                   "tool_policy": {"overrides": {}}}
        uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": "Onbashy", "active": status == "active"})
        uow.connection.execute(
            "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, status, profile_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (identity, identity, business_id, business_id, "onbashy", str(Path(instance.paths.instance_root).parent / f"legacy-{identity}"),
             status, json.dumps(profile), now, now))
        uow.repository("business_memberships", business_id=business_id).insert({"principal_id": identity, "is_owner": False})
    return identity


def set_agent_overrides(instance: Instance, agent_id: str, overrides: dict, businesses):
    """The owner's explicit permission overrides for one registered agent: the only place a new mutation gets a grant."""
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES

    with instance.database.read() as uow:
        revision, home = uow.connection.execute(
            "SELECT revision, home_business_id FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
    ctx = InvocationContext(Principal(instance.owner, "human"), business_id=home or sorted(businesses)[0],
                            allowed_business_ids=frozenset(businesses), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    result = instance.runtime.application.invoke(ctx, "permissions_set", {"agent_id": agent_id, "overrides": overrides},
                                                 InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
    assert result.status == "ok", result.error
    return result.data


def agent_context(agent_id: str, business: str):
    return InvocationContext(Principal(agent_id, "agent"), business_id=business, source="mcp")
