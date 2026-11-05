"""Focused synthetic proof of the explicit offline transition ``v1.3-legacy-learning-removal-1``.

Every store is disposable and built from the trusted host composition. The predecessor is the exact frozen pre-removal
composition reconstructed by the module under test; the target is the ordinary current composition. The release evidence
is a synthetic source-export tree under the test's temporary directory, never a live instance, delivered configuration,
provider, native session or service. Preparation evidence is never hand-written here: a caller-authored receipt, row or
hash is refused, and the genuine role, profile, managed-upgrade and native stages are proved through the real operations in
``test_learning_removal_preparation.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from uuid import UUID

import pytest
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, PreparedChange,
                                         ResourceRef, canonical_hash)
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.bootstrap.learning_removal import (REMOVED_INDEXES, REMOVED_TABLES, REMOVED_TRIGGERS, TRANSITION_ID,
                                                 apply_migration, begin_attempt, inspect_migration, prepare_migration,
                                                 row_fingerprint, table_manifest, validate_preparation_receipt)
from bazarlyk.catalog.packages import files_digest
from bazarlyk.composition import schema_ddl, tables
from bazarlyk.extensions import NO_EXTENSIONS, Extension, select
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.operations_history.catalog import catalog as history_catalog
from bazarlyk.service.ownership import InstanceOwnership
from bazarlyk.source_snapshot import snapshot_sha256
from bazarlyk.storage import exports as storage_exports

PREDECESSOR_DIGEST = "dbeb44a586d6341c8e1802a71c70cca491648d02126c2e5ce79d49f51cf010a1"
TARGET_DIGEST = "bf5c15191543d54b00f4c415b0b782c7b895decbb112cd913d8011e0e89d5a0d"
PREDECESSOR_OBJECTS_SHA256 = "abecf01176a68315522f369ce6fb0afbf439146210cc5d405c31e118baf57bcc"
TARGET_OBJECTS_SHA256 = "4ec8a14a7cf62619c765ecef24946727e5b7ba90c4fed787ea0a6378575b29f5"
REMOVED_OBJECTS_SHA256 = "27e094ac954493bf1434210e364fbaae12f093945e7948e66f16df4ee36a9cae"
COMPONENTS = ("app", "operations_engine", "context_engine", "dashboard", "roles")
OWNER = "6b6f0f0f-0000-4000-8000-000000000001"
AGENT = "6b6f0f0f-0000-4000-8000-000000000002"
BUSINESS = "6b6f0f0f-0000-4000-8000-000000000010"
STAMP = "2026-01-01T00:00:00Z"


REPO_ROOT = Path(__file__).resolve().parents[2]
GROUP_ROOT = REPO_ROOT.parent
SOURCE_ROOTS = {"app": REPO_ROOT / "app", "operations_engine": REPO_ROOT / "qorqut-operations",
                "context_engine": REPO_ROOT / "qorqut-context", "dashboard": REPO_ROOT / "biz-dashboard",
                "roles": GROUP_ROOT / "qorqut-roles"}
_EXPORT_CACHE = {}
_SOURCE_BINDING = None
_TEMPORARY_TREES = []


def cleanup_temporary_trees():
    """Remove every disposable source copy and export this module's fixtures created (they are large and never needed
    after the test module that asked for them)."""
    global _SOURCE_BINDING
    for tree in _TEMPORARY_TREES:
        shutil.rmtree(tree, ignore_errors=True)
    _TEMPORARY_TREES.clear()
    _EXPORT_CACHE.clear()
    _SOURCE_BINDING = None


@pytest.fixture(scope="module", autouse=True)
def _remove_temporary_trees():
    yield
    cleanup_temporary_trees()


def _git(source, *arguments):
    return subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True, text=True).stdout.strip()


def t_git_commit(source, message):
    _git(source, "-c", "user.name=Legacy migration fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "add", "-A")
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Legacy migration fixture", "-c",
                    "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet",
                    "-m", message], check=True, capture_output=True, text=True)


def _commit_all(source):
    _git(source, "init", "--quiet")
    _git(source, "-c", "user.name=Legacy migration fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "add", "-A")
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Legacy migration fixture", "-c",
                    "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet",
                    "-m", "Synthetic source binding"], check=True, capture_output=True, text=True)


def source_binding():
    """A disposable exact copy of the executing component sources as clean Git source artifacts."""
    global _SOURCE_BINDING
    if _SOURCE_BINDING is None:
        base = Path(tempfile.mkdtemp(prefix="legacy-learning-source-"))
        _TEMPORARY_TREES.append(base)
        for component, source in SOURCE_ROOTS.items():
            shutil.copytree(source, base / component, ignore=shutil.ignore_patterns(".git"))
            _commit_all(base / component)
        _SOURCE_BINDING = base
    return _SOURCE_BINDING


def make_paths(tmp_path, name="instance", *, source_copy=False):
    """An instance bound to the executing component sources (or a disposable copy for a mutation test)."""
    root = tmp_path / name
    root.mkdir()
    base = source_binding()
    if not source_copy:
        components = {component: base / component for component in COMPONENTS}
    else:
        components = {}
        destination = tmp_path / (name + "-components")
        for component in COMPONENTS:
            shutil.copytree(base / component, destination / component)
            components[component] = destination / component
    return InstancePaths(root, components)


def release_for(paths, extensions=NO_EXTENSIONS, entries=None):
    """A real format-1 source export whose component snapshots equal the bound executing component sources."""
    cacheable = entries is not None
    entries = [] if entries is None else entries
    root_key = tuple(str(paths.component(name).resolve()) for name in COMPONENTS)
    key = hashlib.sha256((json.dumps(entries, sort_keys=True) + repr(root_key)).encode()).hexdigest()
    export = _EXPORT_CACHE.get(key) if cacheable else None
    if export is None:
        export = Path(tempfile.mkdtemp(prefix="legacy-learning-release-"))
        _TEMPORARY_TREES.append(export)
        for name in COMPONENTS:
            shutil.copytree(paths.component(name), export / name, ignore=shutil.ignore_patterns(".git"))
        manifest = {"format": 1, "components": [
            {"name": name, "destination": name, "commit": _git(paths.component(name), "rev-parse", "HEAD"),
             "tree": _git(paths.component(name), "rev-parse", "HEAD^{tree}"),
             "snapshot_sha256": snapshot_sha256(export / name)} for name in COMPONENTS]}
        extension_components = export_extension_packages(export, manifest, entries)
        (export / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        (export / "extension-components.json").write_text(json.dumps(extension_components) + "\n")
        (export / "selection.json").write_text(json.dumps({"extensions": entries}) + "\n")
        if cacheable:
            _EXPORT_CACHE[key] = export
    manifest = json.loads((export / "SOURCE-MANIFEST.json").read_text())
    return _release_reference(paths, export, manifest)


def export_extension_packages(export, manifest, entries):
    """Add each selected factory's loaded package to the export as its own component (the real export's layout:
    ``extensions/<component>/<package>``); returns the ``extension_components`` reference. The commit and tree of such a
    fixture component are synthetic: the testenv's installed packages have no Git origin."""
    mapping = {}
    for entry in entries or ():
        package = entry["factory"].partition(":")[0].partition(".")[0]
        if package in mapping:
            continue
        name = package.replace("_", "-")
        loaded = Path(__import__(package).__file__).resolve().parent
        shutil.copytree(loaded, export / "extensions" / name / package, ignore=shutil.ignore_patterns("__pycache__"))
        digest = hashlib.sha256(name.encode()).hexdigest()
        manifest["components"].append({"name": name, "destination": f"extensions/{name}", "commit": digest[:40],
                                       "tree": digest[24:], "snapshot_sha256": snapshot_sha256(export / "extensions" / name)})
        mapping[package] = name
    return mapping


def _release_reference(paths, export, manifest):
    manifest_path = export / "SOURCE-MANIFEST.json"
    selection_path = export / "selection.json"
    reference = {"export": str(export), "manifest": str(manifest_path),
                 "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                 "selection": str(selection_path),
                 "selection_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
                 "components": {name: name for name in COMPONENTS}}
    extension_components = json.loads((export / "extension-components.json").read_text())
    if extension_components:
        reference["extension_components"] = extension_components
    return reference


def preparation(paths, steps=(), files=(), extensions=NO_EXTENSIONS, entries=None):
    return {"release": release_for(paths, extensions, entries=entries),
            "steps": [dict(step) for step in steps],
            "files": [dict(item) for item in files]}


def plan_for(paths, steps=(), files=(), extensions=NO_EXTENSIONS, entries=None):
    return prepare_migration(paths, extensions=extensions,
                             preparation=preparation(paths, steps, files, extensions, entries))


def make_predecessor(tmp_path, name="instance", *, source_copy=False):
    transition = removal._transition_for(NO_EXTENSIONS)
    paths = make_paths(tmp_path, name, source_copy=source_copy)
    Database(paths.database, assembly=transition.predecessor).initialize_target()
    return paths, transition


def raw(paths, *, foreign_keys=True):
    connection = sqlite3.connect(paths.database, isolation_level=None)
    install_sql_functions(connection)
    connection.create_function("inbox_actor", 0, lambda: OWNER)
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def raw_file(path, *, foreign_keys=True):
    connection = sqlite3.connect(Path(path), isolation_level=None)
    install_sql_functions(connection)
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def private_directory(tmp_path, name="backups"):
    directory = tmp_path / name
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def backup_path(tmp_path, name="before.sqlite3"):
    return private_directory(tmp_path) / name


def apply_with(paths, plan, tmp_path, *, preparation_receipt=None, name="before.sqlite3", extensions=NO_EXTENSIONS):
    return apply_migration(paths, plan, backup=backup_path(tmp_path, name), preparation=preparation_receipt,
                           extensions=extensions)


def seed_principal(connection):
    connection.execute("INSERT INTO principals (id, kind, display_name, active, revision, created_at, updated_at) "
                       "VALUES (?, 'human', 'Owner', 1, 1, ?, ?)", (OWNER, STAMP, STAMP))


ROLE_SCHEMA = {"schema_version": 1, "id": "learning-applier", "version": "1.0.0", "scope_kinds": ["ecosystem"],
               "brief": "BRIEF.md", "scope_brief": "ecosystem-agent", "skills": [],
               "default_tool_policy": {"learnings_decide": "allow"}}


def publish_role(connection, paths, *, role_id="learning-applier", version="1.0.0", defaults=None):
    """Write and register one exact published role artifact through the same layout the catalog reads."""
    manifest = {**ROLE_SCHEMA, "id": role_id, "version": version,
                "default_tool_policy": defaults or {"learnings_decide": "allow"}}
    files = {"role.yaml": (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
             "BRIEF.md": b"Role brief for the synthetic migration test.\n"}
    digest = files_digest(files)
    root = paths.runtime / "artifacts" / "roles" / role_id / version / digest.removeprefix("sha256:")
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    connection.execute("INSERT INTO artifacts (kind, artifact_id, version, digest, manifest_schema_version, "
                       "stored_path, metadata_json, created_at) VALUES ('role', ?, ?, ?, 1, ?, ?, ?)",
                       (role_id, version, digest, str(root), json.dumps({"manifest_path": "role.yaml"}), STAMP))
    return {"id": role_id, "version": version, "digest": digest}, root, files


def seed_installation(connection):
    connection.execute("INSERT INTO installation (id, installation_id, owner_principal_id, revision, created_at, "
                       "updated_at) VALUES ('service', '11111111-1111-4111-8111-111111111111', ?, 1, ?, ?)",
                       (OWNER, STAMP, STAMP))


def seed_identity(connection, paths, *, stale_role=False, overrides=None):
    """An active profile. ``stale_role`` publishes retired default names; ``overrides`` adds profile names."""
    seed_principal(connection)
    seed_installation(connection)
    defaults = {"learnings_decide": "allow"}
    if stale_role:
        defaults["learnings_work_done"] = "allow"
    role, _root, _files = publish_role(connection, paths, defaults=defaults)
    profile = {"agent_id": OWNER, "name": "owner", "status": "active", "role": role,
               "tool_policy": {"overrides": dict(overrides or {})}}
    connection.execute("INSERT INTO agent_profiles (agent_id, principal_id, home_business_id, namespace, name, "
                       "workspace, status, profile_json, revision, created_at, updated_at) "
                       "VALUES (?, ?, NULL, 'ecosystem', 'owner', 'owner', 'active', ?, 1, ?, ?)",
                       (OWNER, OWNER, json.dumps(profile), STAMP, STAMP))
    return profile, role


def add_candidate(connection, identity="candidate-1", document=None):
    document = document or {"bucket": "context", "scope": "global", "tag": "fact", "text": "A retained fact",
                            "created": STAMP, "revision": 1, "state": "open"}
    connection.execute("INSERT INTO learning_candidates (business_id, id, candidate_json, revision, created_at, "
                       "updated_at, work_state, agent_note, work_targets_json, work_outcome_json, filing_json) "
                       "VALUES (NULL, ?, ?, 1, ?, ?, NULL, '', '[]', '{}', '{}')",
                       (identity, json.dumps(document), STAMP, STAMP))
    return identity


def add_decision(connection, candidate="candidate-1", changes=None, state="accepted"):
    connection.execute("INSERT INTO learning_decisions (business_id, candidate_id, state, feedback, changes_json, "
                       "decided_by, revision, created_at, updated_at) VALUES (NULL, ?, ?, NULL, ?, ?, 1, ?, ?)",
                       (candidate, state, json.dumps(changes or {}), OWNER, STAMP, STAMP))


def add_history(connection, identity="history-1", candidate="candidate-1", after=None):
    connection.execute("INSERT INTO learning_history (business_id, id, candidate_id, event, actor_id, before_json, "
                       "after_json, created_at) VALUES (NULL, ?, ?, 'learnings_decide', ?, '{}', ?, ?)",
                       (identity, candidate, OWNER, json.dumps(after or {"state": "accepted"}), STAMP))


def add_issue(connection, issue="issue-1"):
    connection.execute("INSERT INTO learning_issues (business_id, issue_id, engine_id, engine_scope_id, category, "
                       "policy_state, hold_state, engine_status, current_candidate_id, stale, issue_revision, "
                       "material_evidence_revision, evidence_fingerprint, support_json, eligibility_json, projected_at, "
                       "revision, created_at, updated_at) VALUES (NULL, ?, 'engine-1', 'global', 'category', 'tracking', "
                       "'none', 'bound', NULL, 0, 1, 1, 'fp', '{}', '{}', ?, 1, ?, ?)",
                       (issue, STAMP, STAMP, STAMP))
    return issue


def add_receipt(connection, identity, operation, plan, state="prepared", result=None):
    connection.execute("INSERT INTO apply_receipts (id, principal_id, target_namespace, operation_name, payload_hash, "
                       "change_set, state, progress_json, result_json, revision, created_at, updated_at) VALUES "
                       "(?, ?, 'global', ?, ?, ?, ?, '{}', ?, 1, ?, ?)",
                       (identity, OWNER, operation, plan.payload_hash, plan.canonical, state,
                        None if result is None else json.dumps(result), STAMP, STAMP))


def factual_plan(*, operation="learnings_record_batch", document=None):
    candidate = document or {"bucket": "context", "scope": "global", "tag": "fact", "text": "pending", "created": STAMP}
    return ChangeSet(operation_name=operation, operation_version=1, principal_id=OWNER, target_namespace="global",
                     arguments={"items": [candidate]}, affected_scope={"kind": "global"}, expected_targets=(),
                     changes=(PreparedChange("record", ResourceRef("learning_candidates", "pending-candidate"),
                                             "insert", {"candidate_json": candidate}),),
                     preview={"title": "pending"})


def tamper_identity(paths, digest):
    connection = raw(paths, foreign_keys=False)
    try:
        connection.execute("UPDATE target_identity SET digest=?", (digest,))
        connection.commit()
    finally:
        connection.close()


# -- frozen composition, fresh target and the overt schema guards ------------------------------------------------

def test_frozen_transition_whitelist_and_retained_objects():
    transition = removal._transition_for(NO_EXTENSIONS)
    assert transition.predecessor.target.digest == PREDECESSOR_DIGEST
    assert transition.target.target.digest == TARGET_DIGEST
    before, after = transition.predecessor_objects.objects, transition.target_objects.objects
    assert removal._inventory_digest(before) == PREDECESSOR_OBJECTS_SHA256
    assert removal._inventory_digest(after) == TARGET_OBJECTS_SHA256
    removed = {("table", name) for name in REMOVED_TABLES} | {("index", name) for name in REMOVED_INDEXES} | {
        ("trigger", name) for name in REMOVED_TRIGGERS}
    assert set(before) - set(after) == removed
    assert set(after) < set(before)
    assert all(after[key] == definition for key, definition in before.items() if key in after)
    assert removal.canonical_hash([[kind, name, *before[(kind, name)]] for kind, name in sorted(removed)]) == \
        REMOVED_OBJECTS_SHA256
    assert transition.schema["counts"] == {"tables": 7, "indexes": 15, "triggers": 6}
    for name in REMOVED_TABLES:
        assert name not in schema_ddl.OWNER_TABLES["learning"] and name not in tables.TABLE_SPECS
        assert name not in storage_exports.BUSINESS_TABLES and name not in storage_exports.GLOBAL_TABLES
    assert set(history_catalog().tables).isdisjoint(REMOVED_TABLES)


def test_fresh_initialization_has_only_the_new_schema_and_never_repairs(tmp_path):
    transition = removal._transition_for(NO_EXTENSIONS)
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=transition.target).initialize_target()
    objects = read_objects(raw(paths))
    assert not ({("table", name) for name in REMOVED_TABLES} & set(objects))
    assert {name for kind, name in objects if kind == "table" and name.startswith("learning_")} == {
        "learning_candidates", "learning_decisions", "learning_history", "learning_runs", "learning_scan_state"}
    assert inspect_migration(paths)["state"] == "target"
    Database(paths.database, assembly=transition.target).open_existing()
    with pytest.raises(DomainError):
        Database(paths.database, assembly=transition.predecessor).open_existing()


@pytest.mark.parametrize("engine_table", REMOVED_TABLES)
def test_a_nonempty_engine_table_refuses_without_deleting(tmp_path, engine_table):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_issue(connection)
        if engine_table != "learning_issues":
            add_candidate(connection)
            _insert_engine_reference(connection, engine_table)
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    assert plan["gates"]["removed_tables"][engine_table] == 1 and plan["gates"]["ready"] is False
    backup = backup_path(tmp_path)
    with pytest.raises(DomainError) as refused:
        begin_attempt(paths, plan, backup=backup)
    assert refused.value.code == "release_blocked"
    assert not backup.exists() and inspect_migration(paths)["state"] == "predecessor"
    connection = raw(paths)
    try:
        assert connection.execute(f'SELECT COUNT(*) FROM "{engine_table}"').fetchone()[0] == 1
    finally:
        connection.close()


def _insert_engine_reference(connection, table):
    if table == "learning_issue_candidates":
        connection.execute("INSERT INTO learning_issue_candidates (business_id, candidate_id, issue_id, ordinal, "
                           "predecessor_candidate_id, package_revision, created_by, created_at) VALUES "
                           "(NULL, 'candidate-1', 'issue-1', 1, NULL, 1, ?, ?)", (OWNER, STAMP))
    elif table == "learning_issue_actions":
        connection.execute("INSERT INTO learning_issue_actions (business_id, id, issue_id, issue_revision, candidate_id, "
                           "candidate_revision, action, actor_id, actor_kind, idempotency_key, policy_revision_before, "
                           "policy_revision_after, before_json, after_json, baseline_json, created_at) VALUES "
                           "(NULL, 'action-1', 'issue-1', 1, NULL, NULL, 'note', ?, 'human', NULL, NULL, NULL, '{}', '{}', "
                           "'{}', ?)", (OWNER, STAMP))
    elif table == "learning_issue_links":
        connection.execute("INSERT INTO learning_issue_links (business_id, link_id, issue_id, predecessor_issue_id, "
                           "relation, recorded_at) VALUES (NULL, 'link-1', 'issue-1', 'issue-0', 'split', ?)", (STAMP,))
    elif table == "learning_engine_packages":
        connection.execute("INSERT INTO learning_engine_packages (business_id, engine_id, issue_id, package_revision, "
                           "ordinal, package_hash, material_evidence_revision, outcome, candidate_id, package_json, "
                           "received_by, created_at) VALUES (NULL, 'engine-1', 'issue-1', 1, 1, ?, 1, 'created', "
                           "'candidate-1', '{}', ?, ?)", ("c" * 64, OWNER, STAMP))
    elif table == "learning_engine_enrollments":
        connection.execute("INSERT INTO learning_engine_enrollments (business_id, agent_id, engine_id, active, epoch, "
                           "enrolled_by, revision, created_at, updated_at) VALUES (NULL, ?, 'engine-1', 1, 1, ?, 1, ?, ?)",
                           (AGENT, OWNER, STAMP, STAMP))
    elif table == "learning_engine_state":
        connection.execute("INSERT INTO learning_engine_state (business_id, session_id, engine_id, agent_id, epoch, "
                           "transcript_hash, status, lifecycle, coverage, attempts, detail_json, revision, created_at, "
                           "updated_at) VALUES (NULL, 'session-1', 'engine-1', ?, 1, ?, 'retry', 'complete', 'complete', "
                           "1, '{}', 1, ?, ?)", (AGENT, "d" * 64, STAMP, STAMP))
    else:
        raise AssertionError(table)


@pytest.mark.parametrize("kind", ("raw_engine", "effective_engine", "engine_job", "engine_routine", "engine_signoff",
                                  "engine_receipt", "engine_claim", "unclassified"))
def test_active_engine_and_unprovable_pending_state_refuses(tmp_path, kind):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection)
        if kind == "raw_engine":
            connection.execute("UPDATE learning_candidates SET candidate_json=? WHERE id='candidate-1'",
                               (json.dumps({"bucket": "delta", "engine": {"engine_id": "e"}}),))
        elif kind == "effective_engine":
            add_decision(connection, changes={"engine": {"engine_id": "e", "issue_id": "i"}})
        elif kind == "engine_job":
            connection.execute("INSERT INTO jobs (id, business_id, target_namespace, kind, principal_id, "
                               "initiated_by_principal_id, arguments_json, definition_ref_json, dedupe_key, state, "
                               "attempts, revision, created_at, updated_at) VALUES ('job-1', NULL, 'global', "
                               "'learnings.engine_extract', ?, NULL, '{}', '{}', 'd', 'queued', 0, 1, ?, ?)",
                               (OWNER, STAMP, STAMP))
        elif kind == "engine_routine":
            connection.execute("INSERT INTO routines (namespace, business_id, id, name, scope, target, schedule, timezone, "
                               "status, job_kind, arguments_json, timeout_seconds, metadata_json, revision, created_at, "
                               "updated_at) VALUES ('global', NULL, 'r', 'E', 'global', 'root', '* * * * *', 'UTC', "
                               "'paused', 'learnings.engine_extract', '{}', 60, '{}', 1, ?, ?)", (STAMP, STAMP))
        elif kind == "engine_signoff":
            plan = factual_plan(operation="learnings_work_done")
            connection.execute("INSERT INTO signoffs (id, review_scope_kind, proposer_principal_id, operation_name, "
                               "operation_version, proposal_revision, payload_hash, change_set, preview_json, "
                               "expected_targets_json, policy_binding_json, affected_scope_json, state, updated_sequence, "
                               "created_at, updated_at) VALUES ('s', 'global', ?, 'learnings_work_done', 1, 1, ?, ?, '{}', "
                               "'[]', '{}', '{}', 'pending', 1, ?, ?)", (OWNER, plan.payload_hash, plan.canonical, STAMP, STAMP))
        elif kind == "engine_receipt":
            add_receipt(connection, "r", "learnings_work_done", factual_plan(operation="learnings_work_done"))
        elif kind == "engine_claim":
            add_receipt(connection, "r", "tasks_create", factual_plan(operation="tasks_create"))
            connection.execute("INSERT INTO resource_claims VALUES ('global', 'learning_issues', 'i', 'r', ?)", (STAMP,))
        else:
            connection.execute("INSERT INTO apply_receipts (id, principal_id, target_namespace, operation_name, "
                               "payload_hash, change_set, state, progress_json, revision, created_at, updated_at) VALUES "
                               "('r', ?, 'global', 'x', ?, 'bad', 'prepared', '{}', 1, ?, ?)",
                               (OWNER, "e" * 64, STAMP, STAMP))
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    assert plan["gates"]["ready"] is False
    with pytest.raises(DomainError) as refused:
        begin_attempt(paths, plan, backup=backup_path(tmp_path))
    assert refused.value.code == "release_blocked"
    assert inspect_migration(paths)["state"] == "predecessor"


def test_terminal_history_is_preserved_but_runnable_paths_still_block(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        plan = factual_plan(operation="learnings_work_done")
        add_receipt(connection, "old", "tasks_create", plan, state="applied",
                    result={"operation": "learnings_work_done", "ended": True})
        connection.execute("INSERT INTO jobs (id, business_id, target_namespace, kind, principal_id, "
                           "initiated_by_principal_id, arguments_json, definition_ref_json, dedupe_key, state, attempts, "
                           "revision, created_at, updated_at) VALUES ('old-job', NULL, 'global', "
                           "'learnings.engine_extract', ?, NULL, '{}', '{}', 'd', 'succeeded', 1, 1, ?, ?)",
                           (OWNER, STAMP, STAMP))
        connection.execute("INSERT INTO signoffs (id, review_scope_kind, proposer_principal_id, operation_name, "
                           "operation_version, proposal_revision, payload_hash, change_set, preview_json, "
                           "expected_targets_json, policy_binding_json, affected_scope_json, state, updated_sequence, "
                           "created_at, updated_at) VALUES ('old-signoff', 'global', ?, 'learnings_work_done', 1, 1, ?, ?, "
                           "'{}', '[]', '{}', '{}', 'applied', 1, ?, ?)", (OWNER, plan.payload_hash, plan.canonical,
                                                                          STAMP, STAMP))
        connection.commit()
    finally:
        connection.close()
    assert plan_for(paths)["gates"]["ready"] is True
    paths2, _ = make_predecessor(tmp_path, "runnable")
    connection = raw(paths2)
    try:
        seed_principal(connection)
        connection.execute("INSERT INTO jobs (id, business_id, target_namespace, kind, principal_id, "
                           "initiated_by_principal_id, arguments_json, definition_ref_json, dedupe_key, state, attempts, "
                           "revision, created_at, updated_at) VALUES ('job', NULL, 'global', "
                           "'learnings.engine_extract', ?, NULL, '{}', '{}', 'd', 'queued', 0, 1, ?, ?)",
                           (OWNER, STAMP, STAMP))
        connection.commit()
    finally:
        connection.close()
    assert plan_for(paths2)["gates"]["engine_jobs"] == 1


# -- policy, paths, sidecars and release evidence --------------------------------------------------------------

def test_effective_policy_needs_the_concrete_adoption_steps_without_hardcoding_a_role(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_identity(connection, paths, overrides={"learnings_work_done": "allow"})
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DomainError):
        plan_for(paths)
    profile_step = {"kind": "profile_adoption", "identity": f"profile:{OWNER}", "digest": "b" * 64}
    with pytest.raises(DomainError) as unsupported:
        plan_for(paths, steps=(profile_step,))
    assert unsupported.value.code == "unverified_preparation"

    stale, _ = make_predecessor(tmp_path, "stale-role")
    connection = raw(stale)
    try:
        seed_identity(connection, stale, stale_role=True)
        connection.commit()
    finally:
        connection.close()
    role_step = {"kind": "role_adoption", "identity": "role:learning-applier", "digest": "c" * 64}
    with pytest.raises(DomainError) as unsupported_role:
        plan_for(stale, steps=(role_step,))
    assert unsupported_role.value.code == "unverified_preparation"
    with pytest.raises(DomainError) as unsupported_both:
        plan_for(stale, steps=(role_step, profile_step))
    assert unsupported_both.value.code == "unverified_preparation"

    clean, _ = make_predecessor(tmp_path, "clean-role")
    connection = raw(clean)
    try:
        seed_identity(connection, clean)
        connection.commit()
    finally:
        connection.close()
    assert plan_for(clean)["gates"]["policy_profiles"] == 0


def test_pre_crm_unknown_and_drifted_schema_refuses(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    tamper_identity(paths, "d" + "0" * 63)
    with pytest.raises(DomainError) as refused:
        plan_for(paths)
    assert refused.value.code == "unsupported_schema" and inspect_migration(paths)["state"] == "unknown"

    extra, _ = make_predecessor(tmp_path, "extra")
    connection = raw(extra)
    try:
        connection.execute("CREATE TABLE unexpected_private (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DomainError):
        plan_for(extra)
    assert inspect_migration(extra)["state"] == "unknown"

    partial, _ = make_predecessor(tmp_path, "partial")
    connection = raw(partial)
    try:
        connection.execute("DROP INDEX learning_issue_links_predecessor")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DomainError):
        plan_for(partial)


def test_release_component_and_selection_changes_refuse(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    plan = plan_for(paths)
    release = plan["preparation"]["release"]
    manifest = Path(release["manifest"])
    original = manifest.read_bytes()
    manifest.write_text(json.dumps({"format": 1, "components": []}) + "\n")
    with pytest.raises(DomainError):
        begin_attempt(paths, plan, backup=backup_path(tmp_path))
    manifest.write_bytes(original)
    selection = Path(release["selection"])
    original_selection = selection.read_bytes()
    try:
        selection.write_text(json.dumps({"extensions": [{"factory": "not_installed.extension:create", "config": {}}]}) + "\n")
        with pytest.raises(DomainError):
            begin_attempt(paths, plan, backup=backup_path(tmp_path, "second.sqlite3"))
    finally:
        selection.write_bytes(original_selection)
    assert not (Path(tmp_path) / "backups" / "second.sqlite3").exists()


def test_incomplete_sidecars_and_changed_declared_files_refuse(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    writer = raw(paths)
    try:
        writer.execute("UPDATE change_sequences SET value = value + 1 WHERE name='signoffs'")
        writer.commit()
        assert inspect_migration(paths)["state"] == "predecessor"
        Path(str(paths.database) + "-wal").unlink()
        with pytest.raises(DomainError):
            inspect_migration(paths)
    finally:
        writer.close()


# -- exact apply, preservation, rollback and backup ------------------------------------------------------------

def test_apply_preserves_retained_rows_and_publishes_backup_and_receipt(tmp_path):
    paths, transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection)
        add_decision(connection, changes={"text": "edited"})
        add_candidate(connection, "candidate-2", {"bucket": "delta", "scope": "global", "tag": "rule",
                                                  "text": "Historical delta", "created": STAMP})
        add_history(connection, after={"state": "accepted", "reconciled_into": None})
        before = removal.baseline_manifest(connection, assembly=transition.predecessor)
        identity = removal._identity_values(connection)
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    backup = backup_path(tmp_path)
    result = apply_migration(paths, plan, backup=backup)
    assert result["applied"] is True and result["verification"]["retained_rows"] == "exact"
    assert result["baseline_b"] is not None and Path(result["receipt"]).is_file()
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["baseline_b"] == result["baseline_b"] and receipt["backup"] == result["backup"]
    assert backup.is_file() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    connection = raw_file(backup)
    try:
        assert removal._identity_values(connection) == identity
    finally:
        connection.close()
    connection = raw(paths)
    try:
        objects = read_objects(connection)
        assert not ({("table", name) for name in REMOVED_TABLES} & set(objects))
        for table, manifest in before["tables"].items():
            if table in REMOVED_TABLES or table == "target_identity":
                continue
            assert table_manifest(connection, table, assembly=transition.target)["digest"] == manifest["digest"]
    finally:
        connection.close()
    assert inspect_migration(paths)["state"] == "target"


@pytest.mark.parametrize("fail_after", tuple(range(7)) + ("identity",))
def test_failure_at_every_drop_and_before_commit_rolls_back_to_b(tmp_path, monkeypatch, fail_after):
    paths, transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection)
        connection.commit()
        baseline = removal.baseline_manifest(connection, assembly=transition.predecessor)
    finally:
        connection.close()
    plan = plan_for(paths)
    backup = backup_path(tmp_path)
    attempt = begin_attempt(paths, plan, backup=backup)
    connection = raw(paths)
    try:
        connection.execute("UPDATE principals SET display_name='Changed during preparation' WHERE id=?", (OWNER,))
        connection.commit()
        current = removal.baseline_manifest(connection, assembly=transition.predecessor)
    finally:
        connection.close()
    # The caller changes a row but this is an A=B plan: the transition must refuse before DDL.
    with pytest.raises(DomainError):
        attempt.apply(None)
    attempt.close()
    assert inspect_migration(paths)["state"] == "predecessor"
    connection = raw(paths)
    try:
        assert removal.baseline_manifest(connection, assembly=transition.predecessor)["tables"] == current["tables"]
    finally:
        connection.close()
    Database(backup, assembly=transition.predecessor).validate_snapshot(backup)

    # A clean attempt with no steps exercises a failure at every DROP and before commit.
    paths2, transition2 = make_predecessor(tmp_path, "drops")
    connection = raw(paths2)
    try:
        seed_principal(connection)
        connection.commit()
        original_b = removal.baseline_manifest(connection, assembly=transition2.predecessor)
    finally:
        connection.close()
    plan2 = plan_for(paths2)
    attempt2 = begin_attempt(paths2, plan2, backup=backup_path(tmp_path, "drops.sqlite3"))
    original_drop = removal._drop_table
    dropped = []

    def failing_drop(connection_, table):
        if fail_after == "identity":
            return original_drop(connection_, table)
        if len(dropped) >= fail_after:
            raise DomainError("storage_error", "synthetic DDL failure")
        dropped.append(table)
        return original_drop(connection_, table)

    monkeypatch.setattr(removal, "_drop_table", failing_drop)
    if fail_after == "identity":
        monkeypatch.setattr(removal, "_update_identity",
                            lambda *_: (_ for _ in ()).throw(DomainError("storage_error", "synthetic identity failure")))
    with pytest.raises(DomainError):
        attempt2.apply(None)
    attempt2.close()
    assert inspect_migration(paths2)["state"] == "predecessor"
    connection = raw(paths2)
    try:
        assert removal.baseline_manifest(connection, assembly=transition2.predecessor)["tables"] == original_b["tables"]
    finally:
        connection.close()


def test_exact_target_retry_is_a_verified_noop_and_preserves_evidence(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    plan = plan_for(paths)
    first = apply_with(paths, plan, tmp_path)
    second = backup_path(tmp_path, "second.sqlite3")
    retry = apply_migration(paths, plan, backup=second)
    assert retry["applied"] is False and retry["already_migrated"] is True and retry["reconciled"] is False
    assert retry["backup"] == first["backup"] and retry["baseline_b"] == first["baseline_b"]
    assert not second.exists() and Path(retry["receipt"]).read_text() == Path(first["receipt"]).read_text()


def test_lost_completion_and_changed_row_refuse_recovery(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection)
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    original = removal.PlanAttempt._publish_completion
    removal.PlanAttempt._publish_completion = lambda *_a, **_k: (_ for _ in ()).throw(OSError("lost receipt"))
    try:
        with pytest.raises(DomainError) as uncertain:
            apply_with(paths, plan, tmp_path)
    finally:
        removal.PlanAttempt._publish_completion = original
    assert uncertain.value.code == "migration_receipt_failed"
    connection = raw(paths)
    try:
        connection.execute("UPDATE learning_candidates SET revision=2 WHERE id='candidate-1'")
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
    finally:
        connection.close()
    with pytest.raises(DomainError):
        apply_migration(paths, plan, backup=backup_path(tmp_path, "never.sqlite3"))


def test_unverifiable_evidence_and_unsupported_steps_fail_closed(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_identity(connection, paths, overrides={"learnings_work_done": "allow"})
        connection.commit()
    finally:
        connection.close()
    profile_step = {"kind": "profile_adoption", "identity": f"profile:{OWNER}", "digest": "b" * 64}
    with pytest.raises(DomainError) as refused:
        plan_for(paths, steps=(profile_step,))
    assert refused.value.code == "unverified_preparation"
    assert inspect_migration(paths)["state"] == "predecessor"

    for kind in ("managed_upgrade", "prepared_catalog", "native_preparation", "extension_declaration"):
        with pytest.raises(DomainError) as unsupported:
            plan_for(tmp_path and paths, steps=({"kind": kind, "identity": kind + ":x", "digest": "c" * 64},))
        assert unsupported.value.code == "unverified_preparation"


# -- the coordinator's genuine role/profile/managed/native evidence is proved in test_learning_removal_preparation.py


def test_an_unrelated_or_unsupported_claim_cannot_ride_a_real_receipt(tmp_path):
    # The same evidence adapter is exercised above; here a direct caller claim is refused.
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    attempt = begin_attempt(paths, plan, backup=backup_path(tmp_path))
    connection = raw(paths)
    try:
        connection.execute("UPDATE change_sequences SET value=value+1 WHERE name='signoffs'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DomainError) as refused:
        attempt.apply(None)
    attempt.close()
    assert refused.value.code == "release_blocked"


# -- pending plans, native catalogs, restore rehearsal, real extension composition ------------------------------

def test_stale_delta_and_factual_pending_plans_refuse(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection, "candidate-1", {"bucket": "delta", "scope": "global", "tag": "rule",
                                                  "text": "historic"})
        stale = ChangeSet("learnings_review", 1, OWNER, "global", {"learning_id": "candidate-1"},
                          {"kind": "global"},
                          (ExpectedTarget(ResourceRef("learning_candidates", "candidate-1"), revision=1),),
                          (PreparedChange("record", ResourceRef("learning_decisions", "candidate-1"),
                                          "insert", {"candidate_id": "candidate-1", "state": "accepted",
                                                     "changes_json": {}}),),
                          ChangePreview("review", "", {}, (), (), ()))
        add_receipt(connection, "stale", "learnings_review", stale)
        add_receipt(connection, "factual", "learnings_record_batch", factual_plan(operation="learnings_record_batch"))
        connection.commit()
    finally:
        connection.close()
    gates = plan_for(paths)["gates"]
    assert gates["incompatible_receipts"] >= 1
    assert gates["unverified_receipts"] >= 1
    assert gates["ready"] is False


def test_native_catalog_survey_is_complete_strict_and_business_aware(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        connection.execute("INSERT INTO businesses (id, slug, workspace, hq_path, name, stage, timezone, config_json, "
                           "revision, created_at, updated_at) VALUES (?, 'one', 'one', 'one', 'One', '', 'UTC', '{}', "
                           "1, ?, ?)", (BUSINESS, STAMP, STAMP))
        seed_identity(connection, paths)
        global_workspace = paths.agent_workspace(OWNER)
        global_workspace.mkdir(parents=True)
        (global_workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"bazarlyk:learnings_work_done": {}}}))
        business_agent = AGENT
        connection.execute("INSERT INTO principals (id, kind, display_name, active, revision, created_at, updated_at) "
                           "VALUES (?, 'agent', 'Business', 1, 1, ?, ?)", (business_agent, STAMP, STAMP))
        connection.execute("INSERT INTO agent_profiles VALUES (?, ?, ?, ?, 'business', 'business', 'active', ?, 1, "
                           "?, ?)", (business_agent, business_agent, BUSINESS, BUSINESS,
                                     json.dumps({"agent_id": business_agent, "role": {"id": "learning-applier"},
                                                 "tool_policy": {"overrides": {}}}), STAMP, STAMP))
        connection.commit()
    finally:
        connection.close()
    catalogs = removal._native_catalogs(paths, removal._transition_for(NO_EXTENSIONS), NO_EXTENSIONS)
    assert any(item["agent_id"] == OWNER for item in catalogs["paths"])
    assert any(item.get("scope") == "business" for item in catalogs["surveys"])


def test_restore_bundle_rehearses_database_and_file_recovery(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    config = paths.instance_root / "config" / "affected-file.json"
    config.parent.mkdir()
    config.write_text('{"binding":1}\n')
    plan = plan_for(paths, files=({"path": str(config),
                                   "sha256": hashlib.sha256(config.read_bytes()).hexdigest()},))
    attempt = begin_attempt(paths, plan, backup=backup_path(tmp_path))
    config.write_text('{"binding":2}\n')
    verify = removal.verify_file_bundle(attempt.attempt_directory, plan["preparation"], attempt.baseline_a,
                                        expected_digest=attempt.attempt_metadata["restore"]["digest"])
    rehearsal = tmp_path / "restored"
    rehearsal.mkdir()
    restored = removal.restore_file_bundle(attempt.attempt_directory / "restore", target_root=rehearsal)
    assert restored and (rehearsal / "config" / "affected-file.json").read_text() == '{"binding":1}\n'
    shutil.copy2(attempt.backup["path"], rehearsal / "operations.sqlite3")
    Database(rehearsal / "operations.sqlite3", assembly=removal._transition_for(NO_EXTENSIONS).predecessor).open_existing()
    attempt.close()


def test_real_content_fastmail_paper_selection_compiles_the_independently_frozen_identities(tmp_path):
    pytest.importorskip("qorqut_content.extension")
    pytest.importorskip("qorqut_paper.extension")
    pytest.importorskip("qorqut_fastmail.extension")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import qorqut_content.extension as content
    import qorqut_fastmail.extension as fastmail
    import qorqut_paper.extension as paper
    key = tmp_path / "paper-key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    entries = [{"factory": "qorqut_content.extension:create",
                "config": {"schema_version": 1, "enabled": False}},
               {"factory": "qorqut_fastmail.extension:create",
                "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": str(tmp_path / "token")}},
               {"factory": "qorqut_paper.extension:create",
                "config": {"host": {"base_url": "https://outreach.example.test", "audience": "outreach",
                                    "subject": "owner@example.test", "private_key_file": str(key),
                                    "dashboard_origin": "https://dashboard.example.test"},
                           "token_root": str(tmp_path / "paper-tokens")}}]
    selection = select(entries)
    transition = removal._transition_for(selection)
    assert transition.predecessor.target.digest == "8276e39d7065fc636158dd0a77a51102287028876657737ae10c81f9dc5a4e22"
    assert transition.target.target.digest == "0844307b7e31bb8c1a53b53fe8c6bd659a1fa9f3dc522c9c11b41d65bd7997f6"
    assert len(transition.predecessor_objects.objects) == 597
    assert len(transition.target_objects.objects) == 569
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=transition.predecessor).initialize_target()
    connection = raw(paths)
    try:
        seed_principal(connection)
        connection.execute("INSERT INTO businesses (id, slug, workspace, hq_path, name, stage, timezone, config_json, "
                           "revision, created_at, updated_at) VALUES (?, 'one', 'one', 'one', 'One', '', 'UTC', '{}', "
                           "1, ?, ?)", (BUSINESS, STAMP, STAMP))
        connection.execute("INSERT INTO content_session_invocations (principal_id, business_id, operation_name, "
                           "request_id, operation_version, normalized_request_sha256, recorder_operation_id, session_id, "
                           "prepared_plan_json, prepared_request_path, prepared_inputs_json, state, original_result_json, "
                           "error_provenance_json, created_at, updated_at) VALUES (?, ?, 'content_test', 'request-1', 1, "
                           "?, 'recorder-1', 'session-1', '{}', NULL, '{}', 'reserved', NULL, '[]', ?, ?)",
                           (OWNER, BUSINESS, "a" * 64, STAMP, STAMP))
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths, extensions=selection, entries=entries)
    assert plan["gates"]["ready"] is True
    evidence = plan["release_evidence"]
    assert sorted(evidence["extension_sources"]["packages"]) == ["qorqut_content", "qorqut_fastmail", "qorqut_paper"]
    assert evidence["extension_sources"]["installation"] is None
    result = apply_migration(paths, plan, backup=backup_path(tmp_path), extensions=selection)
    assert result["applied"] is True and result["verification"]["retained_rows"] == "exact"
    connection = raw(paths)
    try:
        assert connection.execute("SELECT COUNT(*) FROM content_session_invocations").fetchone()[0] == 1
        assert connection.execute("SELECT request_id FROM content_session_invocations").fetchone()[0] == "request-1"
        assert connection.execute("SELECT digest FROM target_identity WHERE id='target'").fetchone()[0] == \
            "0844307b7e31bb8c1a53b53fe8c6bd659a1fa9f3dc522c9c11b41d65bd7997f6"
    finally:
        connection.close()


def test_baseline_b_policy_gate_blocks_a_stale_repeated_profile_before_any_drop(tmp_path, monkeypatch):
    paths, transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        old_profile, _role = seed_identity(connection, paths, overrides={"learnings_work_done": "allow"})
        connection.commit()
    finally:
        connection.close()
    target_profile = {**old_profile, "role": old_profile["role"],
                      "tool_policy": {"overrides": {"learnings_work_done": "allow"}}}
    step = {"kind": "profile_adoption", "identity": f"profile:{OWNER}",
            "digest": canonical_hash(target_profile)}
    with pytest.raises(DomainError) as refused:
        plan_for(paths, steps=(step,))
    assert refused.value.code == "unverified_preparation"
    assert inspect_migration(paths)["state"] == "predecessor"

def test_changed_source_component_or_selection_refuses_before_backup(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    plan = plan_for(paths)
    assert plan["gates"]["ready"] is True
    (paths.component("app") / "bazarlyk" / "bootstrap" / "learning_removal.py").write_text("# changed source\n")
    with pytest.raises(DomainError) as refused:
        begin_attempt(paths, plan, backup=backup_path(tmp_path))
    assert refused.value.code in {"operation_conflict", "invalid_configuration"}
    assert not backup_path(tmp_path).exists()
    assert inspect_migration(paths)["state"] == "predecessor"

def test_native_catalog_symlink_and_malformed_document_fail_closed(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_identity(connection, paths)
        connection.commit()
    finally:
        connection.close()
    workspace = paths.agent_workspace(OWNER)
    workspace.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text("{}")
    (workspace / ".mcp.json").symlink_to(target)
    with pytest.raises(DomainError):
        removal._native_catalogs(paths, removal._transition_for(NO_EXTENSIONS), NO_EXTENSIONS)
    (workspace / ".mcp.json").unlink()
    (workspace / ".mcp.json").write_text("{not json")
    with pytest.raises(DomainError):
        removal._native_catalogs(paths, removal._transition_for(NO_EXTENSIONS), NO_EXTENSIONS)


def test_restore_rehearsal_refuses_a_tampered_blob(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    config = paths.instance_root / "config" / "affected-file.json"
    config.parent.mkdir()
    config.write_text('{"binding":1}\n')
    plan = plan_for(paths, files=({"path": str(config),
                                   "sha256": hashlib.sha256(config.read_bytes()).hexdigest()},))
    attempt = begin_attempt(paths, plan, backup=backup_path(tmp_path))
    blob = next((attempt.attempt_directory / "restore" / "blobs").iterdir())
    blob.write_bytes(b"tampered")
    rehearsal = tmp_path / "restored"
    rehearsal.mkdir()
    with pytest.raises(DomainError):
        removal.restore_file_bundle(attempt.attempt_directory / "restore", target_root=rehearsal)
    attempt.close()


def test_missing_precommit_evidence_on_an_exact_target_is_never_treated_as_fresh(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    plan = plan_for(paths)
    result = apply_with(paths, plan, tmp_path)
    attempt_dir = Path(result["receipt"]).parent
    (attempt_dir / "precommit.json").unlink()
    with pytest.raises(DomainError) as refused:
        apply_migration(paths, plan, backup=backup_path(tmp_path, "again.sqlite3"))
    assert refused.value.code == "migration_evidence_invalid"


def test_stale_delta_decision_makes_a_pending_review_incompatible(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection, "candidate-1", {"bucket": "context", "scope": "global", "tag": "fact",
                                                  "text": "retained"})
        add_decision(connection, "candidate-1", {"bucket": "delta", "items": []})
        review = ChangeSet("learnings_review", 1, OWNER, "global", {"learning_id": "candidate-1"},
                           {"kind": "global"},
                           (ExpectedTarget(ResourceRef("learning_candidates", "candidate-1"), revision=1),),
                           (PreparedChange("record", ResourceRef("learning_decisions", "candidate-1"),
                                           "replace", {"candidate_id": "candidate-1", "changes_json": {}}),),
                           ChangePreview("review", "", {}, (), (), ()))
        add_receipt(connection, "review", "learnings_review", review)
        connection.commit()
    finally:
        connection.close()
    gates = plan_for(paths)["gates"]
    assert gates["incompatible_receipts"] >= 1
    assert gates["ready"] is False


def test_large_store_manifest_is_chunked_and_recoverable(tmp_path, monkeypatch):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        for index in range(7):
            add_candidate(connection, f"candidate-{index}", {"bucket": "context", "scope": "global", "tag": "fact",
                                                             "text": f"fact {index}"})
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(removal, "MANIFEST_CHUNK_ROWS", 1)
    plan = plan_for(paths)
    result = apply_with(paths, plan, tmp_path)
    assert result["applied"] is True
    bundle = Path(result["receipt"]).parent / "baseline-b"
    assert len(list((bundle / "tables").iterdir())) >= 7
    retry = apply_migration(paths, plan, backup=backup_path(tmp_path, "retry.sqlite3"))
    assert retry["applied"] is False and retry["already_migrated"] is True


def test_a_genuinely_fresh_target_is_distinct_and_needs_no_receipt(tmp_path):
    transition = removal._transition_for(NO_EXTENSIONS)
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=transition.target).initialize_target()
    plan = prepare_migration(paths, extensions=NO_EXTENSIONS, preparation=preparation(paths))
    result = apply_migration(paths, plan, backup=backup_path(tmp_path))
    assert result["applied"] is False and result["fresh_target"] is True and result["already_migrated"] is True
    assert result["backup"] is None and result["receipt"] is None
    assert not backup_path(tmp_path).exists()
    assert not (paths.runtime / "learning-removal").exists()


def test_a_tampered_baseline_b_bundle_is_not_recovered(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    plan = plan_for(paths)
    result = apply_with(paths, plan, tmp_path)
    attempt_dir = Path(result["receipt"]).parent
    chunk = next((attempt_dir / "baseline-b" / "tables").iterdir())
    chunk.write_bytes(b"truncated")
    with pytest.raises(DomainError) as refused:
        apply_migration(paths, plan, backup=backup_path(tmp_path, "never.sqlite3"))
    assert refused.value.code == "migration_evidence_invalid"


def test_a_wrong_operation_never_authorizes_preparation(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_identity(connection, paths, overrides={"learnings_work_done": "allow"})
        connection.commit()
    finally:
        connection.close()
    step = {"kind": "profile_adoption", "identity": f"profile:{OWNER}",
            "digest": "b" * 64, "operation": "agents_create"}
    with pytest.raises(DomainError) as refused:
        plan_for(paths, steps=(step,))
    assert refused.value.code in {"unverified_preparation", "invalid_configuration"}
    assert inspect_migration(paths)["state"] == "predecessor"



def test_failed_retryable_receipt_without_claims_still_blocks(tmp_path):
    from types import SimpleNamespace
    from qorqut_operations.contracts import InvocationOptions
    from qorqut_operations.storage.executor import PreparedFileExecutor
    paths, _transition = make_predecessor(tmp_path)
    ctx = SimpleNamespace(principal_id=OWNER)
    descriptor = SimpleNamespace(name="learnings_work_done")
    options = InvocationOptions.from_mapping({"idempotency_key": "failed-review-receipt"})
    input_hash = "f" * 64
    receipt_id = PreparedFileExecutor._id(ctx, "global", descriptor.name, options)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_receipt(connection, receipt_id, "learnings_work_done",
                    factual_plan(operation="learnings_work_done"), state="failed")
        connection.execute("UPDATE apply_receipts SET error_json=?, progress_json=? WHERE id=?",
                           (json.dumps({"code": "interrupted", "retryable": True}),
                            json.dumps({"invocation": {"input_hash": input_hash, "options": {"idempotency_key": options.idempotency_key},
                                                       "idempotency_key": options.idempotency_key}}), receipt_id))
        connection.commit()
        # The real pending lookup still returns this failed receipt after FileApplier removed all claims.
        executor = PreparedFileExecutor(Database(paths.database, assembly=_transition.predecessor), None)
        executor.database.open_existing()
        with executor.database.read() as uow:
            pending = executor.pending(ctx, descriptor, options, input_hash, uow, namespace="global")
        assert pending.operation_name == "learnings_work_done"
        assert connection.execute("SELECT COUNT(*) FROM resource_claims WHERE apply_id=?", (receipt_id,)).fetchone()[0] == 0
    finally:
        connection.close()
    plan = plan_for(paths)
    assert plan["gates"]["ready"] is False
    assert plan["gates"]["engine_receipts"] >= 1
    with pytest.raises(DomainError):
        apply_with(paths, plan, tmp_path)


def test_failed_approved_retryable_signoff_still_blocks(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    pending = factual_plan(operation="learnings_work_done")
    connection = raw(paths)
    try:
        seed_principal(connection)
        connection.execute("INSERT INTO signoffs (id, review_scope_kind, proposer_principal_id, operation_name, "
                           "operation_version, proposal_revision, payload_hash, change_set, preview_json, "
                           "expected_targets_json, policy_binding_json, affected_scope_json, state, updated_sequence, "
                           "created_at, updated_at, decision_json, error_json) VALUES ('failed-signoff', 'global', ?, "
                           "'learnings_work_done', 1, 1, ?, ?, '{}', '[]', '{}', '{}', 'failed', 1, ?, ?, ?, ?)",
                           (OWNER, pending.payload_hash, pending.canonical, STAMP, STAMP,
                            json.dumps({"kind": "approve", "by": OWNER, "at": STAMP}),
                            json.dumps({"code": "storage_error", "retryable": True, "effect_status": "none"})))
        connection.commit()
    finally:
        connection.close()
    plan = plan_for(paths)
    assert plan["gates"]["ready"] is False
    assert plan["gates"]["engine_signoffs"] >= 1
    with pytest.raises(DomainError):
        apply_with(paths, plan, tmp_path)


def test_real_factory_selection_with_a_different_declaration_is_refused(tmp_path):
    entries = [{"factory": "qorqut_fastmail.extension:create",
                "config": {"engine_url": "http://127.0.0.1:8787",
                           "admin_token_file": str(tmp_path / "token")}}]
    selection = select(entries)
    paths = make_paths(tmp_path, source_copy=True)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    Database(paths.database, assembly=removal._transition_for(selection).predecessor).initialize_target()
    prep = preparation(paths, extensions=selection, entries=entries)
    other = [{"factory": "qorqut_fastmail.extension:create",
              "config": {"engine_url": "http://127.0.0.1:9797",
                         "admin_token_file": str(tmp_path / "token")}}]
    selection_file = Path(prep["release"]["selection"])
    selection_file.write_text(json.dumps({"extensions": other}) + "\n")
    prep["release"]["selection_sha256"] = hashlib.sha256(selection_file.read_bytes()).hexdigest()
    with pytest.raises(DomainError) as caught:
        prepare_migration(paths, extensions=selection, preparation=prep)
    assert caught.value.code == "operation_conflict"


def test_component_without_the_executing_transition_bytes_is_refused(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    plan = plan_for(paths)
    (paths.component("app") / "bazarlyk" / "bootstrap" / "learning_removal.py").write_text("# not the executing source\n")
    with pytest.raises(DomainError) as caught:
        begin_attempt(paths, plan, backup=backup_path(tmp_path))
    assert caught.value.code in {"operation_conflict", "invalid_configuration"}


def test_real_factory_selection_is_prepared_by_the_cli_with_verified_provenance(tmp_path, capsys):
    from bazarlyk.telegram.private_files import write_private_file
    entries = [{"factory": "qorqut_content.extension:create",
                "config": {"schema_version": 1, "enabled": False}}]
    selection = select(entries)
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=removal._transition_for(selection).predecessor).initialize_target()
    prep = preparation(paths, extensions=selection, entries=entries)
    private = private_directory(tmp_path, "private")
    prep_file = write_private_file(private, "preparation.json", (json.dumps(prep, sort_keys=True) + "\n").encode())
    save = private / "plan.json"
    arguments = ["prepare", "--instance-root", str(paths.instance_root),
                 "--extensions-config", str(prep["release"]["selection"]),
                 "--preparation-plan", str(prep_file), "--save", str(save)]
    for name, location in paths.components.items():
        arguments += ["--component", f"{name}={location}"]
    assert removal.main(arguments) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is True and response["result"]["gates"]["ready"] is True
    assert list(response["result"]["release_evidence"]["extension_sources"]["packages"]) == ["qorqut_content"]
    assert save.is_file()




def test_release_digest_cache_does_not_reuse_preserved_timestamp_export(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    prep = preparation(paths)
    assert prepare_migration(paths, preparation=prep)["gates"]["ready"] is True
    export = Path(prep["release"]["export"])
    target = export / "app" / "bazarlyk/bootstrap/learning_removal.py"
    old = target.stat()
    target.write_text("# replaced after the first warm check\n")
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    manifest_path = Path(prep["release"]["manifest"])
    manifest = json.loads(manifest_path.read_text())
    next(item for item in manifest["components"] if item["name"] == "app")["snapshot_sha256"] = \
        snapshot_sha256(export / "app")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    prep["release"]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(DomainError) as refused:
        prepare_migration(paths, preparation=prep)
    assert refused.value.code == "operation_conflict"
    assert not backup_path(tmp_path).exists()


def test_release_uses_the_verified_manifest_destination(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    prep = preparation(paths)
    assert prepare_migration(paths, preparation=prep)["gates"]["ready"] is True
    export = Path(prep["release"]["export"])
    shutil.copytree(export / "app", export / "actual-app")
    (export / "actual-app/bazarlyk/bootstrap/learning_removal.py").write_text("# destination has no transition\n")
    manifest_path = Path(prep["release"]["manifest"])
    manifest = json.loads(manifest_path.read_text())
    app = next(item for item in manifest["components"] if item["name"] == "app")
    app["destination"] = "actual-app"
    app["snapshot_sha256"] = snapshot_sha256(export / "actual-app")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    prep["release"]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(DomainError) as refused:
        prepare_migration(paths, preparation=prep)
    assert refused.value.code == "operation_conflict"


def test_ignored_untracked_source_file_refuses(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    dashboard = paths.component("dashboard")
    with (dashboard / ".gitignore").open("a") as stream:
        stream.write("\nignored-review-file.js\n")
    t_git_commit(dashboard, "Add ignored fixture exclusion")
    assert plan_for(paths)["gates"]["ready"] is True
    (dashboard / "ignored-review-file.js").write_text("// not in the committed tree\n")
    prep = preparation(paths)
    with pytest.raises(DomainError) as refused:
        prepare_migration(paths, preparation=prep)
    assert refused.value.code == "operation_conflict"
    assert not backup_path(tmp_path).exists()


def test_executable_mode_change_in_source_refuses(tmp_path):
    paths, _transition = make_predecessor(tmp_path, source_copy=True)
    plan = plan_for(paths)
    target = paths.component("app") / "bazarlyk" / "bootstrap" / "learning_removal.py"
    target.chmod(0o700)
    with pytest.raises(DomainError) as refused:
        begin_attempt(paths, plan, backup=backup_path(tmp_path))
    assert refused.value.code == "operation_conflict"


def test_git_tree_digest_binds_the_captured_commit_not_mutable_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    (repo / "file.txt").write_text("A\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "A"], check=True)
    head_a = _git(repo, "rev-parse", "HEAD")
    tree_a = _git(repo, "rev-parse", "HEAD^{tree}")
    digest_a = removal._git_tree_digest(repo, head_a, tree_a)
    assert digest_a is not None
    (repo / "file.txt").write_text("B\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "B"], check=True)
    head_b = _git(repo, "rev-parse", "HEAD")
    tree_b = _git(repo, "rev-parse", "HEAD^{tree}")
    assert removal._git_tree_digest(repo, head_a, tree_a) == digest_a
    assert removal._git_tree_digest(repo, head_b, tree_b) != digest_a
    assert removal._git_tree_digest(repo, head_a, tree_b) is None
