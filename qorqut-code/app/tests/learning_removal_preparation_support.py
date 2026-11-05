"""A synthetic predecessor instance for the offline learning-removal preparation coordinator.

Everything lives under a pytest ``tmp_path`` and is built by the real owner paths: the old Learning Applier role is the
authored role at the roles repository revision that still granted the retired learning tools, published and adopted by
real ``roles_publish``/``agents_create``/``agents_update`` Application calls (their genuine receipts are in the store),
native preparation is the real ``prepare_agent`` with a version stand-in, and the schema is converted to the frozen
predecessor by the migration's own frozen statements. Nothing reads a live instance, contacts a provider or launches a
model. The historical operation metadata is declared on the fixture's own open registry only, exactly as the private
preparation phase does, and never installs a handler.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, utc_now
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.bootstrap import learning_removal_provenance as provenance
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.extensions import NO_EXTENSIONS
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.source_snapshot import component_digest
from host_foundation_support import (build_instance, create_registered_global, owner_global_context, publish_role,
                                     register_sources, legacy_roles_source)

# The roles revision whose Learning Applier 1.15.2 still granted the ten retired learning operations.
OLD_ROLES_REVISION = "808dfb4"
OLD_ROLE_FILES = ("roles/learning-applier/role.yaml", "roles/learning-applier/BRIEF.md")
VERSIONS = {"codex": "codex-cli 0.156.1", "claude-code": "2.1.263 (Claude Code)"}
ENDPOINT = "http://127.0.0.1:9988"
RETIRED_OVERRIDES = {"learnings_issue_action": "allow", "learnings_work_start": "allow"}
# An owner's own grant that the adoption must keep: only the retired names are removed.
CUSTOM_OVERRIDES = {"notes_list": "allow"}


@dataclass
class Predecessor:
    """The disposable instance after conversion: its paths, workspace, ids and what the fixture created."""

    paths: object
    workspace: Path
    extensions: object
    owner: str
    business: str
    agents: dict = field(default_factory=dict)         # role -> agent id
    old_pin: dict = field(default_factory=dict)        # the old learning-applier artifact pin
    receipts: dict = field(default_factory=dict)       # name -> genuine receipt/idempotency evidence


def _role_git(*arguments, check=True):
    return subprocess.run(["git", "-C", str(legacy_roles_source()), *arguments], check=check, capture_output=True)


def old_role_bytes(relative: str) -> bytes:
    return _role_git("show", f"{OLD_ROLES_REVISION}:{relative}").stdout


def use_old_role_source(workspace: Path) -> dict:
    """Overwrite the workspace copy's Learning Applier files with the old revision; the new bytes come back."""
    kept = {}
    for relative in OLD_ROLE_FILES:
        target = Path(workspace) / "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles/native" / relative
        kept[relative] = target.read_bytes()
        data = old_role_bytes(relative)
        if relative.endswith("role.yaml"):
            data = data.replace(b"roles/learning-applier/BRIEF.md", b"roles/imported-roles/native/roles/learning-applier/BRIEF.md")
            data = data.replace(b"skills/learning-application/", b"overseer/overseer-role-skills/native/skills/learning-application/")
            for name in (b"owner-input", b"action-recording", b"ordubashy-browser-control"):
                data = data.replace(b"skills/" + name + b"/", b"operations/imported-role-skills/native/skills/" + name + b"/")
        target.write_bytes(data)
    return kept


def use_new_role_source(workspace: Path, kept: dict) -> None:
    for relative, data in kept.items():
        (Path(workspace) / "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles/native" / relative).write_bytes(data)


def source_paths(tmp_path) -> InstancePaths:
    """Explicit paths whose host components are disposable clean Git copies of the executing sources (so the real source
    identity verification runs) and whose roles component is the workspace's role-source copy."""
    from test_learning_removal_migration import source_binding
    base = source_binding()
    root = Path(tmp_path) / "instance"
    root.mkdir(parents=True, exist_ok=True)
    components = {name: base / name for name in ("app", "operations_engine", "context_engine", "dashboard")}
    components["roles"] = Path(tmp_path) / "workspace/qorqut-agentic/src/qorqut_agentic/resources/defaults"
    return InstancePaths(root, components)


def commit_roles(workspace) -> None:
    """Commit the role-source copy as it finally stands so the release evidence can verify it as clean Git source."""
    from test_learning_removal_migration import _commit_all
    _commit_all(Path(workspace) / "qorqut-agentic/src/qorqut_agentic/resources/defaults")


def declare_historical_operations(registry) -> None:
    """Metadata-only declarations on an open registry (never a handler), as the private preparation phase makes."""
    for entry in removal.retired_operation_entries():
        registry.declare(entry)


def convert_to_predecessor(paths, extensions=NO_EXTENSIONS) -> None:
    """Make the target-shaped store the exact predecessor: the frozen seven tables/objects plus the predecessor identity."""
    transition = removal._transition_for(extensions)
    statements, _specs, _immutable = removal._predecessor_metadata()
    connection = sqlite3.connect(paths.database, isolation_level=None)
    install_sql_functions(connection)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements:
            connection.execute(statement)
        connection.execute(f'UPDATE "{removal.IDENTITY_TABLE}" SET digest = ?', (transition.predecessor.target.digest,))
        connection.commit()
    finally:
        connection.close()


def build_predecessor(tmp_path, *, extensions=NO_EXTENSIONS, agentic_sources=(), native=True, overrides=True,
                      context=None, prepare=None, old_role=True):
    """The complete synthetic predecessor with an old active Learning Applier, an unrelated Ordubashy, a native
    registration and the old role's genuine receipts. Returns the ``Predecessor`` with every runtime released.

    ``context`` is the optional ``ContextSettings`` the fixture runtime composes the Context with. ``prepare`` is called as
    ``prepare(instance, ids)`` while the runtime is still live, to leave genuine pending work behind (a real interrupted
    operation) before the schema is converted. ``old_role=False`` registers the current Learning Applier role (which grants no
    retired operation), so the transition needs no role or profile adoption."""
    instance, ids = build_instance(tmp_path, businesses=("harbor",), extensions=extensions,
                                   agentic_sources=agentic_sources, paths_factory=source_paths, context=context)
    runtime = instance.runtime
    declare_historical_operations(runtime.registry)
    kept = use_old_role_source(instance.workspace) if old_role else {}
    overrides = overrides and old_role
    register_sources(instance, ids["harbor"], "harbor")
    agent = create_registered_global(instance, "learning-applier")
    ordubashy = create_registered_global(instance, "ordubashy")
    context = owner_global_context(instance)
    receipts = {}
    if overrides:
        with instance.database.read() as uow:
            revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()[0]
        result = runtime.application.invoke(
            context, "agents_update", {"agent_id": agent, "changes": {"tool_policy": {"overrides": {**RETIRED_OVERRIDES, **CUSTOM_OVERRIDES}}}},
            InvocationOptions(idempotency_key="fixture-old-overrides", expected_revision=revision))
        assert result.status == "ok", result.error
        receipts["overrides"] = "fixture-old-overrides"
    if native:
        prepare_agent(runtime.resolver, Principal(agent, "agent"), endpoint=ENDPOINT, versions=VERSIONS)
    old_pin = dict(instance.role_pins["learning-applier"])
    use_new_role_source(instance.workspace, kept)
    if prepare is not None:
        prepare(instance, ids)
    runtime.release_instance()
    instance.database.close()
    convert_to_predecessor(instance.paths, extensions)
    return Predecessor(instance.paths, instance.workspace, extensions, instance.owner, ids["harbor"],
                       {"learning-applier": agent, "ordubashy": ordubashy}, old_pin, receipts)


COMPONENTS = ("app", "operations_engine", "context_engine", "dashboard")
UPSTREAM_GUIDANCE = "qorqut-code/app/bazarlyk/agentic/assets/inbox.md"


def combined_release(tmp_path, p, *, upstream=True, name="release-export", entries=None) -> dict:
    """ONE real format-1 export holding both the instance's component sources (their real commits and trees) and the
    agentic component names the managed upgrade reads. An upstream guidance change is exported when ``upstream``: the
    plan the managed upgrade reviews then has a genuine three-way update. ``entries`` is the selected extension factory list
    written as the selection file; each factory package's loaded source is verified against (and, where the fixture's
    sibling source differs from what the test environment installed, replaced by) its exported extension component."""
    from test_learning_removal_migration import _git
    paths = p.paths
    source = paths.instance_root.parent / (paths.instance_root.name + "-source-fixture")
    export = Path(tmp_path) / name
    shutil.copytree(source, export)
    manifest = json.loads((export / "SOURCE-MANIFEST.json").read_text())
    roles = paths.component("roles")
    defaults = export / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
    shutil.rmtree(defaults)
    shutil.copytree(roles, defaults, ignore=shutil.ignore_patterns(".git"))
    for component in COMPONENTS:
        shutil.copytree(paths.component(component), export / component, ignore=shutil.ignore_patterns(".git"))
        manifest["components"].append({"name": component, "destination": component,
                                       "commit": _git(paths.component(component), "rev-parse", "HEAD"),
                                       "tree": _git(paths.component(component), "rev-parse", "HEAD^{tree}")})
    if upstream:
        guidance = export / UPSTREAM_GUIDANCE
        guidance.write_text(guidance.read_text() + "\nUpstream guidance added by this release.\n")
    extension_components = {}
    for entry in entries or ():
        package = entry["factory"].partition(":")[0].partition(".")[0]
        component = package.replace("_", "-")
        by_name = {item["name"]: item for item in manifest["components"]}
        loaded = Path(__import__(package).__file__).resolve().parent
        exported = export / by_name[component]["destination"] / package
        if not provenance.same_package(loaded, exported):
            shutil.rmtree(exported, ignore_errors=True)
            shutil.copytree(loaded, exported, ignore=shutil.ignore_patterns("__pycache__"))
        extension_components[package] = component
    for component in manifest["components"]:
        component["snapshot_sha256"] = component_digest(export, component, manifest["components"])
    (export / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (export / "selection.json").write_text(json.dumps({"extensions": list(entries or [])}) + "\n")
    reference = {"export": str(export), "manifest": str(export / "SOURCE-MANIFEST.json"),
                 "manifest_sha256": hashlib.sha256((export / "SOURCE-MANIFEST.json").read_bytes()).hexdigest(),
                 "selection": str(export / "selection.json"),
                 "selection_sha256": hashlib.sha256((export / "selection.json").read_bytes()).hexdigest(),
                 "components": {**{component: component for component in COMPONENTS}, "roles": "qorqut-agentic:src/qorqut_agentic/resources/defaults"}}
    if extension_components:
        reference["extension_components"] = extension_components
    return reference


CONTEXT_CANDIDATE = "5b1d0e0e-0000-4000-8000-0000000000c1"


def seed_interrupted_review(instance, *, key="intake-interrupted", candidate_id=CONTEXT_CANDIDATE, markdown_bytes=0,
                            state="accepted"):
    """A genuine interrupted factual intake: an owner's ``learnings_review`` over a Context-bucket candidate whose prepared
    file apply fails once, leaving the durable retryable receipt the real executor resumes. ``markdown_bytes`` enlarges the
    accepted source document so a configured Context source limit matters. Returns the arguments and idempotency key."""
    from bazarlyk.background.integration import attach_background
    runtime = instance.runtime
    instance.paths.business_hq("harbor").mkdir(parents=True, exist_ok=True)
    if not getattr(instance, "_background_ready", False):
        attach_background(runtime, runtime.programs, model_client_factory=lambda _business: None,
                          transcript_source=lambda business_id, agent_id=None: iter(()))
        with instance.database.transaction() as uow:
            runtime.authority.register_workers(uow, [instance.business])
        runtime.freeze_registry()
        instance._background_ready = True
    now = utc_now()
    text = "Keep the launch date" + (" " + "x" * markdown_bytes if markdown_bytes else "")
    with instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (instance.business, candidate_id, json.dumps({"text": text, "scope": "biz", "bucket": "context",
                                                          "tag": "decision", "run": "run-1",
                                                          "evidence": {"session": "s-1"}}), now, now))
        uow.connection.execute("INSERT OR IGNORE INTO learning_runs(business_id,id,result_json,created_at) VALUES (?,?,?,?)",
                               (instance.business, "run-1", json.dumps({"cores": {"s-1": {"markdown": "core"}, "s-2": {"markdown": "old"}}}), now))
    arguments = {"learning_id": candidate_id, "state": state}
    applier = runtime.application.file_executor.file_applier
    with mock.patch.object(applier, "apply", side_effect=DomainError("operation_failed", "Synthetic interruption",
                                                                      retryable=True)):
        result = runtime.application.invoke(instance.human(instance.business), "learnings_review", arguments,
                                            InvocationOptions(idempotency_key=key, expected_revision=1))
    assert result.status == "error" and result.error["code"] == "operation_failed", result.to_dict()
    return {"arguments": arguments, "key": key}


def open_target_runtime(p, context=None):
    """The ordinary target runtime over the migrated store, as startup builds it (background programs attached, workers
    registered, registry frozen). Returns ``(runtime, owner_context)``; the caller releases ``runtime.release_instance()``."""
    from qorqut_operations.contracts import InvocationContext
    from qorqut_operations.storage.database import Database
    from bazarlyk.background.integration import attach_background
    from bazarlyk.composition.assembly import host_assembly
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.runtime import build_runtime
    database = Database(p.paths.database, assembly=host_assembly(ChangeFeed(), p.extensions)).open_existing()
    runtime = build_runtime(p.workspace, paths=p.paths, database=database, extensions=p.extensions, context=context)
    runtime.own_instance()
    attach_background(runtime, runtime.programs, model_client_factory=lambda _business: None,
                      transcript_source=lambda business_id, agent_id=None: iter(()))
    with database.transaction() as uow:
        runtime.authority.register_workers(uow, [p.business])
    runtime.freeze_registry()
    return runtime, InvocationContext(Principal(p.owner, "human"), business_id=p.business,
                                      allowed_business_ids=frozenset({p.business}), source="dashboard")


def forge_receipt(instance, base_plan, key, arguments, *, as_operation=None):
    """A fabricated saved plan stored under its own idempotency key (receipt bindings are immutable by trigger, so a forged
    plan is a fabricated receipt, exactly like a tampered store). Returns the receipt id."""
    from qorqut_operations.contracts import canonical_hash, plain
    from qorqut_operations.storage.executor import PreparedFileExecutor
    app, options = instance.runtime.application, InvocationOptions(idempotency_key=key, expected_revision=1)
    owner = instance.human(instance.business)
    with instance.database.read() as uow:
        context = app._options_context(app.current_context(owner, uow), options)
        namespace = app.namespace(context, app.registry.get(as_operation or base_plan.operation_name), arguments)
        bound = app.registry.bind_arguments(as_operation or base_plan.operation_name, arguments)
    input_hash = canonical_hash({"operation": base_plan.operation_name, "version": 1, "arguments": bound,
                                 "options": {**plain(options), "business_id": context.business_id,
                                             "review_business_id": context.review_business_id, "idempotency_key": None}})
    receipt_id = PreparedFileExecutor._id(context, namespace, as_operation or base_plan.operation_name, options)
    now = utc_now()
    with instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,payload_hash,change_set,signoff_id,"
            "state,progress_json,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,NULL,'prepared',?,1,?,?)",
            (receipt_id, context.principal_id, namespace, base_plan.operation_name, base_plan.payload_hash,
             base_plan.canonical, json.dumps({"invocation": {"input_hash": input_hash, "idempotency_key": key,
                                                              "options": plain(options)}}), now, now))
    return receipt_id


def saved_plan(instance, key):
    """The decoded immutable ``ChangeSet`` of the keyed interrupted receipt, read back from its durable row."""
    from qorqut_operations.contracts import ChangeSet
    with instance.database.read() as uow:
        row = uow.connection.execute("SELECT change_set, payload_hash FROM apply_receipts WHERE progress_json LIKE ?",
                                     (f'%"{key}"%',)).fetchone()
    return ChangeSet.from_bytes(row[0], expected_hash=row[1])
