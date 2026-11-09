"""Explicit offline removal of the retired seven learning-engine tables: ``v1.3-legacy-learning-removal-1``.

The five shared learning tables stay exactly as they are; the retired procedural engine's own seven tables become the
smallest predecessor schema metadata in ``learning_removal_predecessor.json`` (their exact DDL, table specifications and
immutable columns). Nothing here is importable as a learning engine: the frozen file only lets this transition
reconstruct the pre-removal composition, compile it in memory and recognise the exact stored predecessor. The target
composition is the ordinary trusted host composition of the intended release, so a fresh initialization never creates
the seven tables and normal startup never repairs a store.

``inspect`` and ``prepare`` are strictly read-only: SQLite is opened ``mode=ro`` (``immutable`` for a fully checkpointed
store so no file, lock or sidecar is created), the exact predecessor composition is compared object for object and the
deterministic plan binds the canonical paths, the selected extension set, both target identities, the entire schema
inventory, the exact 7-table/15-index/6-trigger whitelist, the gate counts and the reviewed preparation-plan references.
A complete ``-wal``/``-shm`` pair from an unclean stop is read coherently; an incomplete pair is refused before any
connection exists.

``begin_attempt`` takes the one instance lifetime lock (or verifies a caller's already-held lock), revalidates the plan
and the exact actual predecessor, re-runs every data/activity gate, publishes one fresh verified private backup and
captures immutable baseline A. The later offline coordinator may then perform its reviewed preparation stages while
holding that same ownership, and pass their concrete receipts to ``attempt.apply``: the schema transaction captures
baseline B immediately before DDL, reconciles every A-to-B row and file difference against those receipts (a difference
that is not exactly accounted, or any change to the five shared learning tables, refuses), drops the seven tables
child-first with individual ``execute`` calls and foreign keys on, verifies the exact target identity/schema/integrity/
foreign keys and every retained row against B, commits once, reopens read-only to verify again, and writes a durable
completion receipt. A failure before commit rolls the whole DDL transaction back to B and keeps A plus the backup. A
retry against the exact completed target is a verified no-op with no second backup; a missing completion receipt after
an uncertain commit is reconciled from the retained attempt evidence instead of replaying anything.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from qorqut_operations.contracts import ChangeSet, DomainError, canonical_bytes, canonical_hash, plain, utc_now
from qorqut_operations.storage.assembly import Assembly, StorageContribution
from qorqut_operations.storage.database import APPLICATION_ID, Database
from qorqut_operations.storage.inventory import (IDENTITY_TABLE, CompiledTarget, compile_target, describe, differences,
                                                 read_objects)
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.bootstrap import learning_removal_provenance as provenance
from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS, ExtensionSet
from qorqut.instance_paths import COMPONENTS as INSTANCE_COMPONENTS, InstancePathError, InstancePaths
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import LOCK_NAME, InstanceOwnership
from qorqut.private_files import (fsync_directory, read_private_file, require_private_directory,
                                    write_private_file)

TRANSITION_ID = "v1.3-legacy-learning-removal-1"
PLAN_FORMAT = 1
PREDECESSOR_FORMAT = 1
MAX_PLAN_BYTES = 128 * 1024
MAX_PREPARATION_BYTES = 512 * 1024
MAX_PREPARATION_STEPS = 32
MAX_PREPARATION_FILES = 512
MAX_PREPARATION_FILE_BYTES = 64 * 1024 * 1024
MAX_PREPARATION_TREES = 16
MAX_TREE_FILES = 8192
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_CATALOG_FILES = 512
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_REPORTED_REFERENCES = 8
MANIFEST_FORMAT = 1
MANIFEST_CHUNK_ROWS = 5000
MAX_MANIFEST_INDEX_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_CHUNK_BYTES = 64 * 1024 * 1024
BACKUP_PREFIX = ".legacy-learning-removal-backup-"
BACKUP_DEADLINE_SECONDS = 120.0
BUSY_TIMEOUT_SECONDS = 120.0
RECEIPT_DIRECTORY = "learning-removal"
FILENAME_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")

# Child-first order from the frozen specification. SQLite also removes each dropped table's attached indexes and
# triggers; no automatic ``sqlite_autoindex`` object is ever named in a manual DROP.
REMOVED_TABLES = ("learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                  "learning_engine_packages", "learning_engine_enrollments", "learning_engine_state", "learning_issues")
REMOVED_INDEXES = ("learning_issue_actions_issue", "learning_issue_actions_replay", "learning_issue_actions_workspace",
                   "learning_issue_candidates_ordinal", "learning_issue_candidates_workspace",
                   "learning_issue_links_issue", "learning_issue_links_predecessor", "learning_issue_links_workspace",
                   "learning_engine_enrollments_workspace", "learning_engine_packages_candidate",
                   "learning_engine_packages_workspace", "learning_engine_state_workspace",
                   "learning_issues_engine_identity", "learning_issues_policy", "learning_issues_workspace")
REMOVED_TRIGGERS = ("learning_engine_packages_no_delete", "learning_engine_packages_no_update",
                    "learning_issue_actions_no_delete", "learning_issue_actions_no_update",
                    "learning_issue_links_no_delete", "learning_issue_links_no_update")
REMOVED_OPERATIONS = ("learnings_engine_import", "learnings_engine_enrollment_set", "learnings_issue_action",
                      "learnings_work_list", "learnings_work_get", "learnings_work_start", "learnings_work_defer",
                      "learnings_work_done", "learnings_work_drop", "learnings_work_note_set")
REMOVED_PROGRAM = "learnings.engine_extract"
RETAINED_LEARNING_TABLES = ("learning_candidates", "learning_decisions", "learning_history", "learning_runs",
                            "learning_scan_state")
# Pending apply receipts may only carry these retained learning effects; everything else blocks or is unrelated.
FACTUAL_RECEIPT_TABLES = frozenset(RETAINED_LEARNING_TABLES)
FACTUAL_RECEIPT_OPERATIONS = frozenset({"learnings_record_batch", "learnings_review", "learnings_edit",
                                        "learnings_decide", "learnings_keep_both", "learnings_extract"})
NONTERMINAL_JOB_STATES = ("queued", "running", "applying", "waiting_signoff", "cancel_requested", "paused_for_restart")
# A failed job is terminal history unless it is scheduled for another attempt.
ACTIVE_JOB_PREDICATE = ("(state IN ('queued','running','applying','waiting_signoff','cancel_requested',"
                        "'paused_for_restart') OR (state='failed' AND next_retry_at IS NOT NULL))")
UNFINISHED_SIGNOFF_STATES = ("pending", "approved")
# Every supported replay entrypoint matters, not only recover_pending's narrower SQL. PreparedFileExecutor.pending and
# resume reach a failed receipt through its stored invocation even after FileApplier removed its claims.
ACTIVE_RECEIPT_STATES = ("prepared", "applying")
ACTIVE_RECEIPT_PREDICATE = ("(state IN ('prepared','applying') OR (state='failed' AND "
                            "json_valid(progress_json) AND "
                            "json_extract(progress_json, '$.invocation.input_hash') IS NOT NULL))")
# SignoffService retry accepts an approved signoff that failed with a retryable error.
ACTIVE_SIGNOFF_PREDICATE = ("(state IN ('pending','approved') OR (state='failed' AND "
                            "json_valid(error_json) AND json_extract(error_json, '$.retryable') = 1 AND "
                            "json_valid(decision_json) AND json_extract(decision_json, '$.kind') = 'approve'))")
STEP_KINDS = ("role_adoption", "profile_adoption", "managed_upgrade", "prepared_catalog", "extension_declaration",
              "native_preparation")

# Declared typed-document columns only: a reference is recognised by its semantic position, never by a substring scan of
# every text column. Engine blocks/identities in the learning documents are the engine dependency itself; typed
# operation/resource names are the retired program and tables.
LEARNING_DOCUMENTS = (("learning_candidates", ("candidate_json", "work_targets_json", "work_outcome_json", "filing_json")),
                      ("learning_decisions", ("changes_json",)),
                      ("learning_history", ("before_json", "after_json")),
                      ("learning_runs", ("result_json",)),
                      ("learning_scan_state", ("state_json",)))
OPERATION_DOCUMENTS = (("jobs", ("arguments_json", "definition_ref_json", "result_json")),
                       ("routines", ("arguments_json", "metadata_json")),
                       ("signoffs", ("preview_json", "expected_targets_json", "policy_binding_json", "affected_scope_json",
                                     "decision_json", "apply_result_json", "error_json", "execution_json",
                                     "legacy_evidence_json")),
                       ("apply_receipts", ("progress_json", "result_json", "error_json")))
OPERATION_KEYS = frozenset({"operation", "operation_name", "program", "job_kind", "target_operation",
                            "source_operation", "effect_operation", "required_operation"})
RESOURCE_KEYS = frozenset({"resource_type", "table", "table_name", "target_table"})
PLAN_KEYS = frozenset({"format", "transition", "instance_root", "database", "selected_extensions", "predecessor",
                       "target", "removal", "schema", "preparation", "catalogs", "release_evidence", "gates",
                       "plan_digest"})
PREPARATION_KEYS = frozenset({"release", "steps", "files", "trees", "inputs"})
RELEASE_KEYS = frozenset({"export", "manifest", "manifest_sha256", "selection", "selection_sha256", "components"})
OPTIONAL_RELEASE_KEYS = frozenset({"extension_components"})
PREPARATION_RECEIPT_KEYS = frozenset({"format", "transition", "plan_digest", "steps"})


# -- frozen predecessor metadata ---------------------------------------------------------------------------------

def _predecessor_file() -> dict:
    raw = Path(__file__).with_name("learning_removal_predecessor.json").read_bytes()
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_installation", "The frozen predecessor metadata is not valid JSON") from exc
    if (not isinstance(value, dict) or value.get("format") != PREDECESSOR_FORMAT
            or value.get("transition") != TRANSITION_ID or value.get("target_name") != "qorqut-v1.2"
            or value.get("target_version") != 1 or value.get("owner") != "learning"):
        raise DomainError("invalid_installation", "The frozen predecessor metadata is not this transition")
    return value


def retired_operation_entries():
    """The historical authority metadata of the ten retired operations, exactly as the frozen predecessor declared it.

    Only the private preparation phase may declare these (never a handler): an old published role still names them, so
    loading, publishing against and adopting away from it needs their metadata while the predecessor is still the store.
    The strict target registry never declares them."""
    from qorqut_operations.application.registry import CatalogEntry
    operations = _predecessor_file().get("operations")
    if not isinstance(operations, dict) or set(operations) != set(REMOVED_OPERATIONS):
        raise DomainError("invalid_installation", "The frozen predecessor operation metadata is not the retired set")
    entries = []
    for name in REMOVED_OPERATIONS:
        item = operations[name]
        if (not isinstance(item, dict) or set(item) != {"supports_signoff", "effect_kind", "exposure", "owner_only"}
                or type(item["supports_signoff"]) is not bool or type(item["owner_only"]) is not bool
                or not isinstance(item["exposure"], list) or not isinstance(item["effect_kind"], str)
                or not set(item["exposure"]) <= {"mcp", "dashboard", "worker"}):
            raise DomainError("invalid_installation", f"The frozen operation metadata of {name} is invalid")
        try:
            entries.append(CatalogEntry(name, item["supports_signoff"], item["effect_kind"],
                                        frozenset(item["exposure"]), item["owner_only"]))
        except ValueError as exc:
            raise DomainError("invalid_installation", f"The frozen operation metadata of {name} is invalid") from exc
    return tuple(entries)


def _predecessor_metadata():
    value = _predecessor_file()
    statements = value.get("removed")
    tables, immutable = value.get("tables"), value.get("immutable_fields")
    if not isinstance(statements, list) or not all(isinstance(item, str) for item in statements):
        raise DomainError("invalid_installation", "The frozen predecessor statements are invalid")
    if (not isinstance(tables, dict) or set(tables) != set(REMOVED_TABLES) or not isinstance(immutable, dict)
            or set(immutable) - set(REMOVED_TABLES)):
        raise DomainError("invalid_installation", "The frozen predecessor table metadata is incomplete")
    found = {"table": [], "index": [], "trigger": []}
    for statement in statements:
        kind, name = _object_of(statement)
        if kind is None:
            raise DomainError("invalid_installation", "A frozen predecessor statement is not a schema object")
        found[kind].append(name)
    if (sorted(found["table"]) != sorted(REMOVED_TABLES) or sorted(found["index"]) != sorted(REMOVED_INDEXES)
            or sorted(found["trigger"]) != sorted(REMOVED_TRIGGERS)):
        raise DomainError("invalid_installation", "The frozen predecessor statements do not match the removal whitelist")
    specs = {}
    for table, spec in tables.items():
        if (not isinstance(spec, dict) or set(spec) != {"keys", "scope_column", "mutable", "revision_column"}
                or not isinstance(spec["keys"], list) or not spec["keys"]
                or any(not isinstance(key, str) for key in spec["keys"])
                or (spec["scope_column"] is not None and not isinstance(spec["scope_column"], str))
                or type(spec["mutable"]) is not bool
                or (spec["revision_column"] is not None and not isinstance(spec["revision_column"], str))):
            raise DomainError("invalid_installation", f"The frozen specification of {table} is invalid")
        specs[table] = TableSpec(tuple(spec["keys"]), spec["scope_column"], spec["mutable"], spec["revision_column"])
    fields = {}
    for table, names in immutable.items():
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise DomainError("invalid_installation", f"The frozen immutable columns of {table} are invalid")
        fields[table] = frozenset(names)
    return tuple(statements), specs, fields


def _object_of(statement):
    for pattern, kind in ((re.compile(r'^CREATE TABLE "?([a-z_][a-z0-9_]*)"?'), "table"),
                          (re.compile(r"^CREATE (?:UNIQUE )?INDEX ([a-z_][a-z0-9_]*)"), "index"),
                          (re.compile(r"^CREATE TRIGGER ([a-z_][a-z0-9_]*)"), "trigger")):
        match = pattern.match(statement)
        if match:
            return kind, match.group(1)
    return None, None


# -- trusted compositions -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Transition:
    extensions: ExtensionSet
    predecessor: Assembly
    target: Assembly
    predecessor_objects: CompiledTarget
    target_objects: CompiledTarget
    removal: dict
    schema: dict


def _identity(assembly: Assembly) -> dict:
    return {"name": assembly.target.name, "version": assembly.target.version, "digest": assembly.target.digest}


def _reconstruct_predecessor(extensions: ExtensionSet) -> Assembly:
    statements, specs, immutable = _predecessor_metadata()
    feed = ChangeFeed()
    composition = HostComposition()
    replaced = 0
    for contribution in host_contributions(feed, extensions):
        if type(contribution) is StorageContribution and contribution.owner == "learning":
            for statement in contribution.schema:
                kind, name = _object_of(statement)
                if name in REMOVED_TABLES or name in REMOVED_INDEXES or name in REMOVED_TRIGGERS:
                    raise DomainError("invalid_installation", "The host learning contribution still declares a removed "
                                      "engine object; this transition needs the post-removal target composition")
            composition.add(StorageContribution("learning", {**contribution.tables, **specs},
                                                {**contribution.immutable_fields, **immutable},
                                                (*contribution.schema, *statements)))
            replaced += 1
        else:
            composition.add(contribution)
    if replaced != 1:
        raise DomainError("invalid_installation", "The host composition has no single learning storage contribution")
    return composition.freeze()


def _removal_delta(before: CompiledTarget, after: CompiledTarget) -> dict:
    removed = before.objects.keys() - after.objects.keys()
    added = after.objects.keys() - before.objects.keys()
    changed = sorted(key for key in before.objects.keys() & after.objects.keys()
                     if before.objects[key] != after.objects[key])
    expected = ({("table", name) for name in REMOVED_TABLES} | {("index", name) for name in REMOVED_INDEXES}
                | {("trigger", name) for name in REMOVED_TRIGGERS})
    if removed != expected or added or changed:
        raise DomainError("invalid_installation", "Compiling the intended removal composition did not produce exactly "
                          "the frozen seven-table object delta", details={
                              "removed": [f"{kind} {name}" for kind, name in sorted(removed)][:8],
                              "added": [f"{kind} {name}" for kind, name in sorted(added)][:8],
                              "changed": [f"{kind} {name}" for kind, name in changed][:8]})
    return {"tables": list(REMOVED_TABLES), "indexes": sorted(REMOVED_INDEXES), "triggers": sorted(REMOVED_TRIGGERS),
            "operations": list(REMOVED_OPERATIONS), "program": REMOVED_PROGRAM}


def _extension_references(extensions: ExtensionSet) -> list[str]:
    """A selected extension may not own or require a table this transition removes; that is a source refusal."""
    found = []
    for contribution in extensions.storage():
        for name in contribution.tables:
            if name in REMOVED_TABLES:
                found.append(f"extension {contribution.owner} owns {name}")
        for name in contribution.required_tables:
            if name in REMOVED_TABLES:
                found.append(f"extension {contribution.owner} requires {name}")
        for statement in contribution.schema:
            kind, name = _object_of(statement)
            if name in REMOVED_TABLES or name in REMOVED_INDEXES or name in REMOVED_TRIGGERS:
                found.append(f"extension {contribution.owner} declares {kind} {name}")
            for table in REMOVED_TABLES:
                if re.search(rf"\b{re.escape(table)}\b", statement):
                    found.append(f"extension {contribution.owner} references {table}")
    return sorted(set(found))


def _transition_for(extensions=NO_EXTENSIONS) -> _Transition:
    if not isinstance(extensions, ExtensionSet):
        raise DomainError("invalid_configuration", "The migration needs the explicit extension selection")
    reference = _extension_references(extensions)
    if reference:
        raise DomainError("invalid_installation", "A selected extension depends on a table this transition removes",
                          details={"references": reference[:MAX_REPORTED_REFERENCES]})
    predecessor = _reconstruct_predecessor(extensions)
    target = host_assembly(ChangeFeed(), extensions)
    before, after = compile_target(predecessor), compile_target(target)
    removal = _removal_delta(before, after)
    removed = sorted(before.objects.keys() - after.objects.keys())
    schema = {"predecessor_objects_sha256": _inventory_digest(before.objects),
              "target_objects_sha256": _inventory_digest(after.objects),
              "removed_objects_sha256": canonical_hash([[kind, name, *before.objects[(kind, name)]]
                                                         for kind, name in removed]),
              "counts": {"tables": len(REMOVED_TABLES), "indexes": len(REMOVED_INDEXES),
                         "triggers": len(REMOVED_TRIGGERS)}}
    return _Transition(extensions=extensions, predecessor=predecessor, target=target, predecessor_objects=before,
                       target_objects=after, removal=removal, schema=schema)


def _inventory_digest(objects) -> str:
    return hashlib.sha256(canonical_bytes([(kind, name, owner, sql)
                                           for (kind, name), (owner, sql) in sorted(objects.items())])).hexdigest()


# -- read-only observation ----------------------------------------------------------------------------------------

def inspect_migration(paths: InstancePaths, *, extensions=NO_EXTENSIONS) -> dict:
    """Read-only recognition of the Operations store: missing, predecessor, target or unknown, plus gate counts."""
    transition = _transition_for(extensions)
    report = _static_report(paths, transition)
    report.update(_observation(paths, transition))
    if report["state"] == "predecessor":
        report["gates"] = _gate_summary(_gates(paths, transition, extensions))
    elif report["state"] == "target":
        report["gates"] = _already_target_gates()
    else:
        report["gates"] = {"ready": False, "unavailable": True}
    return report


def _static_report(paths, transition: _Transition) -> dict:
    _require_paths(paths)
    return {"format": PLAN_FORMAT, "transition": TRANSITION_ID, "instance_root": str(paths.instance_root),
            "database": str(_database_path(paths)), "selected_extensions": list(transition.extensions.ids),
            "predecessor": _identity(transition.predecessor), "target": _identity(transition.target),
            "removal": transition.removal, "schema": transition.schema}


def _plan_body(paths, transition: _Transition, preparation) -> dict:
    normalized = _normalise_preparation(preparation)
    return {**_static_report(paths, transition), "preparation": normalized,
            "release_evidence": _release_evidence(paths, normalized, transition)}


def _observation(paths, transition: _Transition) -> dict:
    database = _database_path(paths)
    if not os.path.lexists(database):
        return {"state": "missing", "identity": None, "application_id": None, "user_version": None, "problems": {}}
    _require_store_file(database)
    connection = _connect(database, "ro")
    try:
        connection.execute("PRAGMA query_only = ON")
        state, identity, application_id, user_version, problems = _recognise(connection, transition)
    except sqlite3.Error as exc:
        raise DomainError("invalid_database", "The existing Operations database cannot be read") from exc
    finally:
        connection.close()
    return {"state": state, "identity": identity, "application_id": application_id, "user_version": user_version,
            "problems": problems}


def _recognise(connection, transition: _Transition):
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    expected_version = transition.target.target.version
    identity = _stored_identity(connection)
    objects = read_objects(connection)
    predecessor = differences(transition.predecessor_objects.objects, objects)
    target = differences(transition.target_objects.objects, objects)
    pragmas = application_id == APPLICATION_ID and user_version == expected_version
    if pragmas and identity == _identity(transition.predecessor) and not predecessor:
        return "predecessor", identity, application_id, user_version, {}
    if pragmas and identity == _identity(transition.target) and not target:
        return "target", identity, application_id, user_version, {}
    problems = {"predecessor": describe(predecessor), "target": describe(target)}
    if application_id != APPLICATION_ID:
        problems["application_id"] = f"unexpected {application_id}; expected {APPLICATION_ID}"
    if user_version != expected_version:
        problems["user_version"] = f"unexpected {user_version}; expected {expected_version}"
    return "unknown", identity, application_id, user_version, problems


def _stored_identity(connection):
    try:
        rows = connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"').fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != 1:
        return None
    name, version, digest = rows[0]
    return {"name": name, "version": version, "digest": digest}


# -- deterministic read-only prepare -----------------------------------------------------------------------------

def prepare_migration(paths: InstancePaths, *, extensions=NO_EXTENSIONS, preparation) -> dict:
    """The deterministic, bounded, read-only plan for this exact instance and preparation-plan reference set.

    Only the recognised predecessor or an already exact target is accepted. The plan carries no record contents or
    transcript bytes: only identities, object inventories, gate counts and file hashes. Any source, path, extension,
    sidecar or release change makes the saved plan refuse later instead of being guessed around.
    """
    transition = _transition_for(extensions)
    observation = _observation(paths, transition)
    if observation["state"] not in ("predecessor", "target"):
        raise DomainError("unsupported_schema", "The Operations store is not exactly the recognised predecessor or target "
                          "of this transition", details={"state": observation["state"],
                                                          "identity": observation["identity"],
                                                          "application_id": observation["application_id"],
                                                          "user_version": observation["user_version"],
                                                          "problems": observation["problems"]})
    normalized = _normalise_preparation(preparation)
    gates = (_gates(paths, transition, extensions, preparation=normalized) if observation["state"] == "predecessor"
             else _already_target_gates())
    catalogs = _native_catalogs(paths, transition, extensions)
    unsupported = [step for step in normalized["steps"] if step["kind"] in UNSUPPORTED_EVIDENCE_KINDS]
    if unsupported:
        raise DomainError("unverified_preparation", "This transition has no authoritative evidence adapter yet for "
                          "these preparation steps; they fail closed until integration supplies one",
                          details={"kinds": sorted({step["kind"] for step in unsupported})})
    if normalized["steps"] and normalized.get("inputs") is None:
        raise DomainError("unverified_preparation", "A plan with preparation steps needs the coordinator's bound inputs: "
                          "a bare step list can be neither executed nor verified")
    _require_preparation_plan_files(paths, normalized, transition)
    if observation["state"] == "predecessor":
        _require_policy_coverage(gates, normalized, transition)
        declared_native = {step["identity"] for step in normalized["steps"] if step["kind"] == "native_preparation"}
        uncovered = [item for item in catalogs["paths"]
                     if not declared_native or (item.get("agent_id") and f"native:{item['agent_id']}" not in declared_native)]
        if uncovered:
            raise DomainError("invalid_configuration", "The prepared native catalogs still name retired operations; the "
                              "plan needs a concrete native-preparation update step for each affected agent", details={
                                  "catalogs": [item["path"] for item in uncovered][:MAX_REPORTED_REFERENCES]})
    body = {**_static_report(paths, transition), "preparation": normalized, "catalogs": catalogs,
            "release_evidence": _release_evidence(paths, normalized, transition),
            "gates": _already_target_gates() if observation["state"] == "target" else _gate_summary(gates)}
    return {**body, "plan_digest": canonical_hash(body)}


def _gate_summary(gates) -> dict:
    summary = {key: gates[key] for key in ("removed_tables", "engine_links", "typed_references", "engine_jobs",
                                           "engine_routines", "engine_signoffs", "resource_claims", "engine_receipts",
                                           "incompatible_receipts", "pending_factual_receipts",
                                           "unrelated_pending_receipts", "unclassified_receipts",
                                           "incompatible_signoffs", "pending_factual_signoffs",
                                           "unclassified_signoffs", "unverified_receipts", "unverified_signoffs")}
    profiles = gates["policy_profiles"]
    summary["policy_profiles"] = len(profiles)
    summary["policy_digest"] = canonical_hash(sorted([profile["agent_id"], profile["role_id"],
                                                      profile["role_version"], profile["role_digest"],
                                                      profile["role_state"], profile["stale_defaults"],
                                                      profile["stale_overrides"], profile["requires_profile"],
                                                      profile["requires_role"]] for profile in profiles))
    summary["ready"] = not any(gates[key] for key in ("engine_links", "typed_references", "engine_jobs", "engine_routines",
                                                      "engine_signoffs", "resource_claims", "engine_receipts",
                                                      "incompatible_receipts", "unclassified_receipts",
                                                      "incompatible_signoffs", "unclassified_signoffs",
                                                      "unverified_receipts", "unverified_signoffs"))
    summary["ready"] = summary["ready"] and not any(gates["removed_tables"].values())
    return summary


def _already_target_gates() -> dict:
    return {"removed_tables": {}, "engine_links": 0, "typed_references": 0, "engine_jobs": 0, "engine_routines": 0,
            "engine_signoffs": 0, "resource_claims": 0, "engine_receipts": 0, "incompatible_receipts": 0,
            "pending_factual_receipts": 0, "unrelated_pending_receipts": 0, "unclassified_receipts": 0,
            "incompatible_signoffs": 0, "pending_factual_signoffs": 0, "unclassified_signoffs": 0,
            "unverified_receipts": 0, "unverified_signoffs": 0, "policy_profiles": 0, "policy_digest": canonical_hash([]), "ready": True}


def _normalise_preparation(preparation) -> dict:
    """The reviewed release/role/managed/catalog plan references plus the exact affected file set."""
    if not isinstance(preparation, dict) or set(preparation) - PREPARATION_KEYS or "release" not in preparation:
        raise DomainError("invalid_configuration", "Prepare needs the reviewed preparation-plan references")
    release = preparation["release"]
    if (not isinstance(release, dict) or not RELEASE_KEYS <= set(release)
            or set(release) - RELEASE_KEYS - OPTIONAL_RELEASE_KEYS):
        raise DomainError("invalid_configuration", "The release reference needs the explicit export, manifest and "
                          "selection inputs with their verified digests")
    export_root = _absolute_path(release["export"], "The release export root")
    manifest = _absolute_path(release["manifest"], "The release source manifest")
    selection = _absolute_path(release["selection"], "The extension-selection file")
    if manifest != export_root / "SOURCE-MANIFEST.json":
        raise DomainError("invalid_configuration", "The release manifest must be SOURCE-MANIFEST.json in its export root")
    if not HASH.fullmatch(release["manifest_sha256"]) or not HASH.fullmatch(release["selection_sha256"]):
        raise DomainError("invalid_configuration", "Release/selection digests must be SHA-256 values")
    components = release["components"]
    if not isinstance(components, dict) or not components:
        raise DomainError("invalid_configuration", "The release reference needs the host component correspondence map")
    normalized_components = {}
    for path_name, manifest_name in components.items():
        if path_name not in INSTANCE_COMPONENTS or not isinstance(manifest_name, str) or not IDENTITY.fullmatch(manifest_name):
            raise DomainError("invalid_configuration", "A release component correspondence entry is invalid")
        provenance.split_component(manifest_name)
        normalized_components[path_name] = manifest_name
    if len(set(normalized_components.values())) != len(normalized_components):
        raise DomainError("invalid_configuration", "Two host component roots cannot share one release component")
    extension_components = release.get("extension_components")
    normalized_release = {"export": str(export_root), "manifest": str(manifest),
                          "manifest_sha256": release["manifest_sha256"], "selection": str(selection),
                          "selection_sha256": release["selection_sha256"],
                          "components": dict(sorted(normalized_components.items()))}
    if extension_components is not None:
        if (not isinstance(extension_components, dict) or len(extension_components) > MAX_PREPARATION_STEPS
                or not all(isinstance(key, str) and provenance.PACKAGE.fullmatch(key) and isinstance(value, str)
                           and IDENTITY.fullmatch(value) for key, value in extension_components.items())):
            raise DomainError("invalid_configuration", "The extension component references are invalid")
        for value in extension_components.values():
            provenance.split_component(value)
        normalized_release["extension_components"] = dict(sorted(extension_components.items()))
    release = normalized_release
    steps = preparation.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_PREPARATION_STEPS:
        raise DomainError("invalid_configuration", "The preparation steps are missing or exceed their bound")
    normalized_steps = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"kind", "identity", "digest"}:
            raise DomainError("invalid_configuration", "A preparation step needs exactly kind, identity and digest")
        if step["kind"] not in STEP_KINDS:
            raise DomainError("invalid_configuration", "Unknown preparation step kind: " + str(step["kind"]))
        if not isinstance(step["identity"], str) or not IDENTITY.fullmatch(step["identity"]):
            raise DomainError("invalid_configuration", "A preparation step identity is invalid")
        if not isinstance(step["digest"], str) or not HASH.fullmatch(step["digest"]):
            raise DomainError("invalid_configuration", "A preparation step digest is not a SHA-256")
        normalized_steps.append({"kind": step["kind"], "identity": step["identity"], "digest": step["digest"]})
    if len({(step["kind"], step["identity"]) for step in normalized_steps}) != len(normalized_steps):
        raise DomainError("invalid_configuration", "A preparation step is declared twice")
    files = preparation.get("files", [])
    if not isinstance(files, list) or len(files) > MAX_PREPARATION_FILES:
        raise DomainError("invalid_configuration", "The preparation file list is missing or exceeds its bound")
    normalized_files = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise DomainError("invalid_configuration", "A preparation file needs exactly path and sha256")
        path = _absolute_path(item["path"], "A preparation file")
        if item["sha256"] is not None and (not isinstance(item["sha256"], str) or not HASH.fullmatch(item["sha256"])):
            raise DomainError("invalid_configuration", "A preparation file hash is not a SHA-256 or null")
        normalized_files.append({"path": str(path), "sha256": item["sha256"]})
    normalized_files.sort(key=lambda item: item["path"])
    if len({item["path"] for item in normalized_files}) != len(normalized_files):
        raise DomainError("invalid_configuration", "A preparation file is declared twice")
    trees = preparation.get("trees", [])
    if not isinstance(trees, list) or len(trees) > MAX_PREPARATION_TREES:
        raise DomainError("invalid_configuration", "The preparation tree list is missing or exceeds its bound")
    normalized_trees = []
    for item in trees:
        if not isinstance(item, dict) or set(item) != {"root", "inventory"} or not isinstance(item["inventory"], str) \
                or not HASH.fullmatch(item["inventory"]):
            raise DomainError("invalid_configuration", "A preparation tree needs exactly its root and inventory digest")
        normalized_trees.append({"root": str(_absolute_path(item["root"], "A preparation tree root")),
                                 "inventory": item["inventory"]})
    normalized_trees.sort(key=lambda item: item["root"])
    roots = [item["root"] for item in normalized_trees]
    if len(set(roots)) != len(roots) or any(a != b and (b + os.sep).startswith(a + os.sep) for a in roots for b in roots):
        raise DomainError("invalid_configuration", "A preparation tree is declared twice or nested in another")
    result = {"release": release,
              "steps": sorted(normalized_steps, key=lambda item: (item["kind"], item["identity"])),
              "files": normalized_files, "trees": normalized_trees}
    inputs = preparation.get("inputs")
    if inputs is not None:
        if (not isinstance(inputs, dict) or set(inputs) != {"request", "derived"}
                or len(canonical_bytes(inputs)) > MAX_PREPARATION_BYTES):
            raise DomainError("invalid_configuration", "The coordinator input binding has an unexpected shape or exceeds "
                              "its bound")
        result["inputs"] = inputs
    return result


def _declaration_value(value, depth=0, seen=None):
    """A deterministic, non-secret projection of an extension declaration for identity comparison."""
    from collections.abc import Mapping
    from dataclasses import fields, is_dataclass
    from types import MappingProxyType
    if depth > 8:
        return {"type": "truncated"}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"real": repr(value)}
    if isinstance(value, Path):
        return {"path": str(value)}
    if callable(value):
        return {"callable": f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', getattr(value, '__name__', ''))}"}
    if isinstance(value, (Mapping, MappingProxyType)):
        return {str(key): _declaration_value(item, depth + 1, seen) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_declaration_value(item, depth + 1, seen) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_declaration_value(item, depth + 1, seen) for item in value), key=repr)
    if is_dataclass(value):
        return {"type": f"{type(value).__module__}.{type(value).__qualname__}",
                "fields": {field.name: _declaration_value(getattr(value, field.name), depth + 1, seen)
                           for field in fields(value)}}
    seen = set() if seen is None else seen
    identity = id(value)
    if identity in seen:
        return {"type": "cycle"}
    seen.add(identity)
    public = {name: item for name, item in vars(value).items()
              if not name.startswith("_") and not callable(item)}
    if public:
        return {"type": f"{type(value).__module__}.{type(value).__qualname__}",
                "attrs": {name: _declaration_value(item, depth + 1, seen)
                          for name, item in sorted(public.items())}}
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _extension_declaration(extension) -> dict:
    attrs = {}
    for name in ("config", "host", "token_setting"):
        if hasattr(extension, name):
            attrs[name] = _declaration_value(getattr(extension, name))
    declarations = {}
    for name in ("storage", "catalog", "history", "workspace_types", "item_kinds", "sources",
                 "calendar_providers", "native_connections", "error_statuses", "operation_guards",
                 "note_derivations", "assets", "package_validators", "integration_adapter", "policy_rules", "private_signoffs",
                 "operation_guidance", "owner_input_sources", "global_discovery_operations"):
        method = getattr(extension, name, None)
        if not callable(method):
            continue
        try:
            declarations[name] = _declaration_value(method())
        except Exception as exc:
            declarations[name] = {"error": type(exc).__name__}
    return {"id": extension.id, "type": f"{type(extension).__module__}.{type(extension).__qualname__}",
            "attrs": attrs, "declarations": declarations}


def _source_tree_digest(root: Path) -> str | None:
    """Freshly hash every relevant regular file/mode/type; never trust an mtime cache."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return None
    digest = hashlib.sha256()

    def visit(directory: Path, prefix: str) -> bool:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            if name in {".git", "__pycache__", ".pytest_cache"}:
                continue
            relative = (prefix + "/" + name) if prefix else name
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            if stat.S_ISLNK(status.st_mode):
                return False
            if stat.S_ISDIR(status.st_mode):
                if not visit(Path(entry.path), relative):
                    return False
                continue
            if not stat.S_ISREG(status.st_mode):
                return False
            encoded = relative.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            # Git tracks only the executable bit; normalize ordinary permission noise while still detecting a
            # mode change that alters whether the source file is executable.
            digest.update((0o755 if status.st_mode & 0o111 else 0o644).to_bytes(4, "big"))
            try:
                data = Path(entry.path).read_bytes()
            except OSError:
                return False
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return True

    return digest.hexdigest() if visit(root, "") else None


def _git_tree_digest(root: Path, head: str, tree: str) -> str | None:
    """Hash the immutable captured commit object; never trust a mutable branch or an unverified tree id."""
    import io
    import tarfile
    try:
        actual_tree = subprocess.run(["git", "-C", str(root), "rev-parse", f"{head}^{{tree}}"], check=True,
                                     capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if actual_tree != tree:
        return None
    key = (str(Path(root).resolve()), head, tree)
    cached = _GIT_TREE_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", head], check=True,
                                 capture_output=True).stdout
        staging = Path(tempfile.mkdtemp(prefix="learning-removal-git-tree-"))
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                tar.extractall(staging, filter="data")
            value = _source_tree_digest(staging)
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError):
        return None
    if value is not None:
        _GIT_TREE_DIGEST_CACHE[key] = value
    return value


_GIT_TREE_DIGEST_CACHE = {}


def _loaded_component_roots() -> dict:
    import qorqut
    import qorqut_context
    import qorqut_operations
    return {"app": Path(qorqut.__file__).resolve().parent.parent,
            "operations_engine": Path(qorqut_operations.__file__).resolve().parent.parent,
            "context_engine": Path(qorqut_context.__file__).resolve().parent.parent}


def _release_evidence(paths: InstancePaths, preparation: dict, transition: _Transition) -> dict:
    """Verify the actual exported source tree and explicit extension selection bound by the plan."""
    release = preparation["release"]
    export_root, manifest_path = Path(release["export"]), Path(release["manifest"])
    selection_path = Path(release["selection"])
    try:
        if export_root.is_symlink() or not export_root.is_dir():
            raise DomainError("invalid_configuration", "The release export root is missing or unsafe")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise DomainError("invalid_configuration", "The release source manifest is missing or unsafe")
        if selection_path.is_symlink() or not selection_path.is_file():
            raise DomainError("invalid_configuration", "The extension-selection file is missing or unsafe")
        manifest_bytes = manifest_path.read_bytes()
        selection_bytes = selection_path.read_bytes()
    except OSError as exc:
        raise DomainError("invalid_configuration", "The release evidence is unreadable") from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != release["manifest_sha256"]:
        raise DomainError("operation_conflict", "The release source manifest changed from its reviewed digest")
    if hashlib.sha256(selection_bytes).hexdigest() != release["selection_sha256"]:
        raise DomainError("operation_conflict", "The extension-selection file changed from its reviewed digest")
    from qorqut.source_snapshot import SnapshotError, component_digest, snapshot_sha256, validate_export
    from qorqut.installation import extensions_configuration
    from qorqut.extensions import select
    try:
        manifest, manifest_sha = validate_export(export_root)
        selected = extensions_configuration(selection_path)
        entries = selected.get("extensions") if isinstance(selected, dict) else None
        if not isinstance(entries, list):
            raise DomainError("invalid_configuration", "The extension selection has no factory list")
        selected_set = select(entries)
    except (SnapshotError, OSError, ValueError, TypeError, DomainError) as exc:
        if isinstance(exc, DomainError) and exc.code == "invalid_configuration":
            raise
        raise DomainError("invalid_configuration", "The release export or extension selection failed its integrity "
                          "validation") from exc
    if manifest_sha != release["manifest_sha256"]:
        raise DomainError("operation_conflict", "The validated release manifest digest differs from its reference")
    if selected_set.ids != transition.extensions.ids:
        raise DomainError("operation_conflict", "The explicit extension selection does not match the compiled "
                          "transition selection")
    expected_declarations = {extension.id: _extension_declaration(extension)
                             for extension in transition.extensions.extensions}
    actual_declarations = {extension.id: _extension_declaration(extension)
                           for extension in selected_set.extensions}
    if actual_declarations != expected_declarations:
        raise DomainError("operation_conflict", "The explicit extension declarations/configuration do not match the "
                          "compiled transition selection", details={"extensions": list(selected_set.ids)})
    extension_ids = list(selected_set.ids)
    extension_sources = provenance.extension_evidence(selected_set, entries, release, manifest, manifest_sha, export_root)
    components = {item["name"]: item for item in manifest["components"]}
    mapping = release["components"]
    if set(mapping) != set(paths.components):
        raise DomainError("invalid_configuration", "The release component correspondence must name every and only the "
                          "explicit host component roots")
    prepared = provenance.prepared_root(export_root)
    loaded_roots = {} if prepared is not None else _loaded_component_roots()
    summary = {}
    for path_name, reference in sorted(mapping.items()):
        manifest_name, subpath = provenance.split_component(reference)
        component = components.get(manifest_name)
        if component is None:
            raise DomainError("invalid_configuration", "A release component correspondence entry names no exported "
                              "component")
        root = paths.component(path_name)
        if root.is_symlink():
            raise DomainError("invalid_configuration", "A host component root is a symlink")
        exported_component = provenance.exported_source(export_root, component, subpath)
        if (root / ".git").exists() and not subpath:
            committed_digest = _committed_source(root, component, path_name, exported_component)
            entry = {"commit": component["commit"], "tree": component["tree"],
                     "snapshot_sha256": component["snapshot_sha256"]}
        else:
            committed_digest, entry = _installed_source(root, component, manifest["components"], subpath,
                                                        exported_component, path_name)
        if path_name in loaded_roots:
            loaded_digest = _source_tree_digest(loaded_roots[path_name])
            if loaded_digest is None or loaded_digest != committed_digest:
                raise DomainError("operation_conflict", "The verified source is not the source loaded by this process",
                                  details={"component": path_name})
        summary[reference] = entry
    return {"manifest_sha256": manifest_sha, "selection_sha256": release["selection_sha256"],
            "selection_declarations": canonical_hash(actual_declarations),
            "extensions": sorted(set(extension_ids)), "components": summary,
            "extension_sources": extension_sources}


def _committed_source(root: Path, component: dict, path_name: str, exported_component: Path) -> str:
    """A Git checkout: clean, at the exported commit/tree, and its source, committed tree and export all identical."""
    try:
        environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, env=environment).stdout.strip()
        tree = subprocess.run(["git", "-C", str(root), "rev-parse", f"{head}^{{tree}}"], check=True,
                              capture_output=True, text=True, env=environment).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", "status", "--porcelain"],
                               check=True, capture_output=True, text=True, env=environment).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DomainError("invalid_configuration", "A host component's source revision cannot be read") from exc
    if dirty or head != component["commit"] or tree != component["tree"]:
        raise DomainError("operation_conflict", "The host component source is not the exact clean exported "
                          "revision", details={"component": path_name})
    committed_digest = _git_tree_digest(root, head, tree)
    source_digest = _source_tree_digest(root)
    export_digest = _source_tree_digest(exported_component)
    if committed_digest is None or source_digest is None or export_digest is None:
        raise DomainError("operation_conflict", "A complete source/export/committed tree cannot be verified",
                          details={"component": path_name})
    if source_digest != committed_digest or export_digest != committed_digest:
        raise DomainError("operation_conflict", "The source or manifest-selected export is not the exact committed "
                          "Git tree", details={"component": path_name})
    return committed_digest


def _installed_source(root: Path, component: dict, components: list, subpath: str, exported_component: Path,
                      path_name: str):
    """A non-Git installed source (a prepared ``src`` tree or a copy of one): byte-identical to the validated export."""
    from qorqut.source_snapshot import SnapshotError, snapshot_sha256
    exclusions = [] if subpath else provenance.descendant_exclusions(component, components)
    try:
        installed = snapshot_sha256(root, exclude=exclusions)
        exported = snapshot_sha256(exported_component, exclude=exclusions)
    except (SnapshotError, OSError) as exc:
        raise DomainError("operation_conflict", "A non-Git host component or its export cannot be verified",
                          details={"component": path_name}) from exc
    if installed != exported or (not subpath and installed != component["snapshot_sha256"]):
        raise DomainError("operation_conflict", "The installed host component is not the exact validated export",
                          details={"component": path_name})
    loaded = _source_tree_digest(exported_component)
    if loaded is None:
        raise DomainError("operation_conflict", "A complete exported tree cannot be verified",
                          details={"component": path_name})
    entry = {"commit": component["commit"], "tree": component["tree"], "snapshot_sha256": component["snapshot_sha256"],
             "installed_sha256": installed, "subpath": subpath}
    return loaded, entry


def _require_preparation_plan_files(paths, preparation, transition: _Transition) -> None:
    """Every declared affected file is an existing regular file or a genuinely absent planned creation."""
    database = _database_path(paths)
    for item in preparation["files"]:
        path = Path(item["path"])
        _require_instance_file(paths, path, "A planned preparation file")
        if path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
            raise DomainError("invalid_configuration", "The preparation file list names the Operations database itself")
        current = _file_hash(path)
        if item["sha256"] != current["sha256"]:
            raise DomainError("operation_conflict", "A declared preparation file changed before preparation", details={
                "path": str(path), "expected": item["sha256"], "observed": current["sha256"]})
    for item in preparation.get("trees", []):
        root = Path(item["root"])
        _require_instance_file(paths, root, "A planned preparation tree")
        if tree_inventory_digest(root) != item["inventory"]:
            raise DomainError("operation_conflict", "A declared preparation tree changed before preparation",
                              details={"root": str(root)})
    if preparation.get("inputs") is not None:
        from qorqut.bootstrap import learning_removal_preparation as coordinator
        coordinator.bind_inputs(paths, transition, preparation["inputs"], preparation["release"])


def _require_policy_coverage(gates, preparation, transition: _Transition) -> None:
    """Every active policy that grants a retired operation needs its concrete adoption step."""
    declared = {(step["kind"], step["identity"]) for step in preparation["steps"]}
    uncovered = []
    for profile in gates["policy_profiles"]:
        if profile["requires_profile"] and ("profile_adoption", f"profile:{profile['agent_id']}") not in declared:
            uncovered.append({"agent_id": profile["agent_id"], "required": "profile_adoption"})
        if profile["requires_role"] and ("role_adoption", f"role:{profile['role_id']}") not in declared:
            uncovered.append({"agent_id": profile["agent_id"], "required": "role_adoption", "role": profile["role_id"]})
    if uncovered:
        raise DomainError("invalid_configuration", "Active profile policies still grant retired learning operations; "
                          "the preparation plan needs their concrete adoption steps",
                          details={"profiles": uncovered[:MAX_REPORTED_REFERENCES],
                                   "total": len(uncovered)})


NATIVE_CATALOG_LOCATIONS = (".mcp.json", ".claude/settings.json", ".claude/settings.local.json",
                            ".codex/config.toml", ".codex/hooks.json", "AGENTS.md", "CLAUDE.md",
                            ".qorqut/claude-launch-settings.json")


def _native_catalogs(paths, transition: _Transition, extensions, *, connection=None) -> dict:
    """Complete read-only survey of every active profile's native prepared-tool documents.

    Every workspace is inspected (or explicitly recorded absent), and every present document is read strictly:
    symlinks, non-regular files, unreadable or malformed documents and exceeded bounds fail closed instead of being
    silently treated as absent. The result is never truncated.
    """
    database = _database_path(paths)
    if not os.path.lexists(database):
        return {"paths": [], "surveys": []}
    query = ("SELECT p.agent_id, p.home_business_id, b.slug FROM agent_profiles p "
             "LEFT JOIN businesses b ON b.id=p.home_business_id WHERE p.status='active' ORDER BY p.agent_id")
    if connection is None:
        opened = _connect(database, "ro")
        try:
            rows = opened.execute(query).fetchall()
        finally:
            opened.close()
    else:
        rows = connection.execute(query).fetchall()
    found, surveys = [], []
    inspected = 0
    for agent_id, home, slug in rows:
        if home is not None and (not isinstance(slug, str) or not slug):
            raise DomainError("invalid_profile", "A business profile has no resolvable business workspace",
                              details={"agent_id": agent_id})
        try:
            workspace = paths.agent_workspace(agent_id, business_folder=slug if home else None)
        except (InstancePathError, DomainError) as exc:
            raise DomainError("invalid_configuration", "An active profile's workspace path is unsafe",
                              details={"agent_id": agent_id}) from exc
        survey = {"agent_id": agent_id, "workspace": str(workspace),
                  "scope": "business" if home else "global", "documents": {}}
        if not os.path.lexists(workspace):
            survey["state"] = "workspace_absent"
            surveys.append(survey)
            continue
        if os.path.islink(workspace) or not workspace.is_dir():
            raise DomainError("invalid_configuration", "An active profile's workspace is not a real directory",
                              details={"agent_id": agent_id, "workspace": str(workspace)})
        survey["state"] = "inspected"
        for name in NATIVE_CATALOG_LOCATIONS:
            path = workspace / name
            if not os.path.lexists(path):
                survey["documents"][name] = {"state": "absent"}
                continue
            inspected += 1
            if inspected > MAX_CATALOG_FILES:
                raise DomainError("limit_exceeded", "The native prepared-catalog inventory exceeds its bound")
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise DomainError("invalid_configuration", "A native prepared catalog is not a plain file")
                if info.st_size > MAX_CATALOG_BYTES:
                    raise DomainError("limit_exceeded", "A native prepared catalog exceeds its bound")
                document = _read_catalog(path)
                digest = _file_budget_hash(path)
            except DomainError:
                raise
            except OSError as exc:
                raise DomainError("invalid_configuration", "A native prepared catalog is unreadable",
                                  details={"path": str(path)}) from exc
            tools = sorted(_removed_tool_names(document))
            survey["documents"][name] = {"state": "inspected", "sha256": digest, "tools": tools}
            if tools:
                found.append({"path": str(path), "sha256": digest, "tools": tools, "agent_id": agent_id})
        surveys.append(survey)
    found.sort(key=lambda item: item["path"])
    return {"paths": found, "surveys": surveys}


def _read_catalog(path: Path):
    """Parse one present native catalog strictly; malformed content is a compatibility refusal, never absence."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DomainError("invalid_configuration", "A native prepared catalog is unreadable") from exc
    if len(data) > MAX_CATALOG_BYTES:
        raise DomainError("limit_exceeded", "A native prepared catalog exceeds its bound")
    try:
        if path.suffix == ".toml":
            import tomllib
            return tomllib.loads(data.decode("utf-8"))
        if path.suffix == ".md":
            return data.decode("utf-8")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise DomainError("invalid_configuration", "A native prepared catalog is malformed",
                          details={"path": str(path)}) from exc


def _file_budget_hash(path):
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise DomainError("invalid_configuration", "A native prepared catalog could not be hashed") from exc


def _removed_tool_names(document) -> set[str]:
    found = set()

    def walk(value):
        if isinstance(value, str):
            for name in (*REMOVED_OPERATIONS, REMOVED_PROGRAM):
                if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", value):
                    found.add(name)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(document)
    return found


# -- data and activity gates -------------------------------------------------------------------------------------

def _gates(paths, transition: _Transition, extensions, *, with_references=False, preparation=None):
    """The real data/activity gates, read under the caller's already exclusive offline ownership when applying."""
    database = _database_path(paths)
    connection = _connect(database, "ro")
    try:
        connection.execute("PRAGMA query_only = ON")
        return _gate_rows(connection, transition, extensions, with_references=with_references, paths=paths,
                          target=_target_for(paths, transition, extensions, preparation))
    finally:
        connection.close()


def _agentic_installed(paths) -> bool:
    from qorqut.agentic.installation import RECORD
    return (Path(paths.instance_root) / "config" / RECORD).exists()


class _Target:
    """The strict target composition the gates judge against, composed once and only when first needed.

    ``runtime`` is the host's composed target runtime (every late registration included, no retired tool declared, the
    actual Context filing limit when the request names its configuration): the one authoritative source of the operation
    names, the policy and the semantic validators. It never opens the Operations database and writes nothing. A store
    without an installed agentic layer has no target composition; its saved plans can then be named only, never proved."""

    def __init__(self, paths, transition: "_Transition", extensions, workspace=None, context=None):
        self.paths, self.transition, self.extensions = paths, transition, extensions
        self.workspace, self.context = workspace, context
        self._runtime, self._registry = None, None

    @property
    def runtime(self):
        if self.paths is None or not _agentic_installed(self.paths):
            return None
        if self._runtime is None:
            from qorqut.bootstrap.learning_removal_runtime import inert_runtime
            try:
                settings = None
                if self.context is not None:
                    from qorqut.bootstrap.learning_removal_preparation import _context_settings
                    settings = _context_settings({"context": self.context})
                self._runtime = inert_runtime(self.paths, self.transition, self.workspace, context=settings)
            except DomainError:
                raise
            except Exception as exc:
                raise DomainError("unclassified_pending_plan", "The strict target composition cannot be built for "
                                  "the gates") from exc
        return self._runtime

    @property
    def registry(self):
        runtime = self.runtime
        if runtime is not None:
            return runtime.registry
        if self._registry is None:
            # No installed agentic layer: the builtin catalog alone is a strictly smaller name list, so a plan naming a
            # later registration is blocked, never admitted.
            self._registry = _pending_registry(self.extensions)
        return self._registry


def _target_for(paths, transition: "_Transition", extensions, preparation=None) -> "_Target":
    """The target composition for the gates, bound to the reviewed coordinator inputs when the plan carries them."""
    request = ((preparation or {}).get("inputs") or {}).get("request") or {}
    return _Target(paths, transition, extensions, workspace=request.get("workspace"), context=request.get("context"))


def _gate_rows(connection, transition: _Transition, extensions, *, with_references=False, paths=None, target=None):
    target = target or _Target(paths, transition, extensions)
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}
    if not set(REMOVED_TABLES) <= tables:
        raise DomainError("unsupported_schema", "The store does not contain all seven engine tables")
    counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in REMOVED_TABLES}
    engine_links, typed = _scan_documents(connection)
    engine_jobs = _nonterminal_engine_jobs(connection)
    engine_routines = _active_engine_routines(connection)
    engine_signoffs = _pending_engine_signoffs(connection)
    claims = _engine_claims(connection)
    receipts = _pending_receipts(connection, extensions, target)
    signoffs = _pending_signoffs(connection, extensions, target)
    policy_deficits = _policy_deficits(connection, paths, extensions, target)
    return {"removed_tables": counts, "engine_links": len(engine_links), "typed_references": len(typed),
            "engine_jobs": len(engine_jobs), "engine_routines": len(engine_routines),
            "engine_signoffs": len(engine_signoffs), "resource_claims": len(claims),
            "engine_receipts": receipts["engine"], "incompatible_receipts": receipts["incompatible"],
            "pending_factual_receipts": receipts["factual"], "unrelated_pending_receipts": receipts["unrelated"],
            "unclassified_receipts": receipts["unclassified"],
            "incompatible_signoffs": signoffs["incompatible"], "pending_factual_signoffs": signoffs["factual"],
            "unclassified_signoffs": signoffs["unclassified"], "unverified_receipts": receipts["unverified"],
            "unverified_signoffs": signoffs["unverified"], "policy_profiles": policy_deficits,
            "reference_details": {"engine_links": engine_links[:MAX_REPORTED_REFERENCES],
                                  "typed_references": typed[:MAX_REPORTED_REFERENCES],
                                  "engine_jobs": engine_jobs[:MAX_REPORTED_REFERENCES],
                                  "engine_routines": engine_routines[:MAX_REPORTED_REFERENCES],
                                  "engine_signoffs": engine_signoffs[:MAX_REPORTED_REFERENCES],
                                  "resource_claims": claims[:MAX_REPORTED_REFERENCES],
                                  "unclassified_receipts": receipts["unclassified_refs"][:MAX_REPORTED_REFERENCES],
                                  "incompatible_signoffs": signoffs["details"][:MAX_REPORTED_REFERENCES]},
            "receipt_details": receipts["details"]}


def _scan_documents(connection):
    """Engine dependency in retained learning records and typed references in declared operational documents."""
    engine_links, typed = [], []
    tables = _tables(connection)
    for table, columns in LEARNING_DOCUMENTS:
        if table not in tables:
            continue
        selected = ", ".join(_quote(column) for column in columns)
        rows = connection.execute(f'SELECT rowid, {selected} FROM "{table}"').fetchall()
        for row in rows:
            for column, value in zip(columns, row[1:]):
                document = _decode(value)
                if document is None:
                    continue
                codes = _references(document, issue_keys=True)
                if codes:
                    target = engine_links if any(code == "engine" or code == "engine_issue" for code in codes) else typed
                    target.append({"table": table, "rowid": row[0], "column": column,
                                   "references": sorted(codes)[:4]})
    for table, columns in OPERATION_DOCUMENTS:
        if table not in tables:
            continue
        selected = ", ".join(_quote(column) for column in columns)
        key = "rowid"
        if table == "jobs":
            rows = connection.execute(f'SELECT {key}, {selected} FROM "{table}" WHERE ' + ACTIVE_JOB_PREDICATE).fetchall()
        elif table == "signoffs":
            rows = connection.execute(f'SELECT {key}, {selected} FROM "{table}" WHERE ' + ACTIVE_SIGNOFF_PREDICATE).fetchall()
        elif table == "apply_receipts":
            rows = connection.execute(f'SELECT {key}, {selected} FROM "{table}" WHERE ' + ACTIVE_RECEIPT_PREDICATE).fetchall()
        else:
            rows = connection.execute(f'SELECT {key}, {selected} FROM "{table}"').fetchall()
        for row in rows:
            for column, value in zip(columns, row[1:]):
                document = _decode(value)
                if document is None:
                    continue
                codes = _references(document)
                if codes:
                    typed.append({"table": table, "id": row[0], "column": column, "references": sorted(codes)[:4]})
    if "apply_receipts" in tables:
        for row in connection.execute("SELECT id, change_set FROM apply_receipts WHERE " + ACTIVE_RECEIPT_PREDICATE):
            try:
                plan = ChangeSet.from_bytes(row[1])
            except DomainError:
                typed.append({"table": "apply_receipts", "id": row[0], "column": "change_set",
                              "references": ["unreadable_pending_plan"]})
                continue
            codes = _references(plan.to_dict())
            if codes:
                typed.append({"table": "apply_receipts", "id": row[0], "column": "change_set",
                              "references": sorted(codes)[:4]})
    return engine_links, typed


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _decode(value):
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _references(value, *, issue_keys=False) -> set[str]:
    found = set()

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "engine":
                    if child not in (None, "", {}, []):
                        found.add("engine")
                elif key in ("engine_id", "engine_scope_id", "engine_status"):
                    if child not in (None, ""):
                        found.add("engine")
                elif issue_keys and key == "issue_id" and child not in (None, ""):
                    found.add("engine_issue")
                elif key in OPERATION_KEYS and isinstance(child, str) and _retired_operation(child):
                    found.add("removed_operation:" + child)
                elif key in RESOURCE_KEYS and isinstance(child, str) and child in REMOVED_TABLES:
                    found.add("removed_table:" + child)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return found


def _retired_operation(name):
    return name in REMOVED_OPERATIONS or name == REMOVED_PROGRAM


def _profile_policy_names(document) -> set[str]:
    found = set()

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and _retired_operation(key):
                    found.add("removed_operation:" + key)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str) and _retired_operation(item):
            found.add("removed_operation:" + item)

    walk(document)
    return found


def _nonterminal_engine_jobs(connection):
    found = []
    rows = connection.execute("SELECT id, kind, arguments_json, definition_ref_json, result_json FROM jobs WHERE "
                              + ACTIVE_JOB_PREDICATE)
    for row in rows:
        codes = {code for value in row[2:] for code in _references(_decode(value) or {})}
        if row[1] == REMOVED_PROGRAM or codes:
            found.append({"job_id": row[0], "kind": row[1], "references": sorted(codes)[:3]})
    return found


def _active_engine_routines(connection):
    found = []
    for row in connection.execute("SELECT namespace, id, job_kind, arguments_json FROM routines "
                                  "WHERE status IN ('active','paused')"):
        codes = _references(_decode(row[3]) or {})
        if row[2] == REMOVED_PROGRAM or codes:
            found.append({"routine": f"{row[0]}/{row[1]}", "job_kind": row[2], "references": sorted(codes)[:3]})
    return found


def _pending_engine_signoffs(connection):
    found = []
    for row in connection.execute("SELECT id, operation_name, change_set FROM signoffs WHERE " +
                                  ACTIVE_SIGNOFF_PREDICATE):
        codes = set()
        if _retired_operation(row[1]):
            codes.add("removed_operation:" + row[1])
        try:
            plan = ChangeSet.from_bytes(row[2])
            codes |= _references(plan.to_dict())
        except DomainError:
            codes.add("unreadable_pending_plan")
        if codes:
            found.append({"signoff_id": row[0], "operation": row[1], "references": sorted(codes)[:3]})
    return found


def _engine_claims(connection):
    found = []
    for row in connection.execute("SELECT target_namespace, resource_type, resource_id, apply_id FROM resource_claims"):
        if row[1] in REMOVED_TABLES:
            found.append({"resource": f"{row[0]}/{row[1]}:{row[2]}", "apply_id": row[3],
                          "references": ["removed_table:" + row[1]]})
    return found


def _pending_registry(extensions):
    try:
        from qorqut.composition.catalog import builtin_registry
        return builtin_registry(extensions)
    except Exception as exc:
        raise DomainError("unclassified_pending_plan", "The target operation registry cannot be composed for pending-"
                          "plan classification") from exc


def _contains_bucket(value, bucket="delta") -> bool:
    if isinstance(value, dict):
        return value.get("bucket") == bucket or any(_contains_bucket(child, bucket) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_bucket(child, bucket) for child in value)
    return False


def _candidate_ids(plan: ChangeSet) -> dict:
    """Every stored candidate identity referenced by the plan's actual effects, with any planned replacement value."""
    found = {}

    def add(business_id, identity, value=None):
        if isinstance(identity, str) and identity:
            found.setdefault((business_id, identity), value)

    def walk(value, business_id=None):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"candidate_id", "learning_id"} and isinstance(child, str):
                    add(business_id, child)
                if key == "earlier" and isinstance(child, dict):
                    add(business_id, child.get("id"))
                walk(child, business_id)
            if isinstance(value.get("id"), str) and value.get("bucket") is not None:
                add(business_id, value["id"], value)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, business_id)

    for change in plan.changes:
        if change.resource.resource_type not in RETAINED_LEARNING_TABLES:
            continue
        business_id = change.resource.business_id
        values = plain(change.values)
        if change.resource.resource_type == "learning_candidates":
            candidate = values.get("candidate_json") if isinstance(values, dict) else None
            add(business_id, change.resource.resource_id, candidate if isinstance(candidate, dict) else None)
            if isinstance(candidate, dict):
                walk(candidate, business_id)
        elif change.resource.resource_type == "learning_decisions":
            add(business_id, values.get("candidate_id") if isinstance(values, dict) else None)
            walk(values, business_id)
        elif change.resource.resource_type == "learning_history":
            add(business_id, values.get("candidate_id") if isinstance(values, dict) else None)
            walk(values, business_id)
    walk(plan.arguments)
    return found


def _effective_candidate(connection, business_id, identity, planned=None, planned_decisions=None):
    row = connection.execute("SELECT candidate_json FROM learning_candidates WHERE business_id IS ? AND id=?",
                             (business_id, identity)).fetchone()
    candidate = planned if isinstance(planned, dict) else (_decode(row[0]) if row is not None else None)
    if not isinstance(candidate, dict):
        return None
    decision = connection.execute("SELECT changes_json FROM learning_decisions WHERE business_id IS ? AND candidate_id=?",
                                  (business_id, identity)).fetchone()
    merged = _decode(decision[0]) if decision is not None else {}
    merged = merged if isinstance(merged, dict) else {}
    if planned_decisions:
        merged.update(planned_decisions)
    if merged.get("bucket") == "delta":
        return merged
    return candidate


def _classify_pending_plan(connection, operation, plan: ChangeSet, registry, *, target_validation=False) -> str:
    """Conservative effect classification: only provably factual, registered, removed-reference-free plans pass."""
    if _retired_operation(operation) or _retired_operation(plan.operation_name):
        return "engine"
    if _references(plan.to_dict()):
        return "engine"
    if not registry.known(plan.operation_name):
        return "unclassified"
    learning = [change for change in plan.changes if change.resource.resource_type in RETAINED_LEARNING_TABLES]
    known_factual = plan.operation_name in FACTUAL_RECEIPT_OPERATIONS
    if known_factual and target_validation:
        # The shared validator understands retained embedded evidence and same-batch reconciliation. Do not reject
        # those valid plans using the older row-only survey; the target Application must prove the entire plan next.
        return "factual"
    if not learning:
        # A registered non-learning operation is not proof that its actual effect is independent of the removed
        # engine; only an explicit target-registry compatibility adapter can establish that. Fail closed.
        return "factual" if known_factual else "unclassified"
    if not known_factual and not any(change.operation_name in FACTUAL_RECEIPT_OPERATIONS for change in plan.changes):
        return "incompatible"
    if any(change.resource.resource_type in REMOVED_TABLES for change in plan.changes):
        return "engine"
    if _contains_bucket(plan.arguments):
        return "incompatible"
    for change in learning:
        if _contains_bucket(plain(change.values)):
            return "incompatible"
    planned_decisions = {}
    for change in learning:
        if change.resource.resource_type == "learning_decisions" and isinstance(change.values, dict):
            planned = change.values.get("changes_json")
            if isinstance(planned, dict):
                planned_decisions[(change.resource.business_id, change.values.get("candidate_id") or change.resource.resource_id)] = planned
    for (business_id, identity), planned in _candidate_ids(plan).items():
        effective = _effective_candidate(connection, business_id, identity, planned,
                                         planned_decisions.get((business_id, identity)))
        if effective is None:
            return "incompatible"
        if effective.get("bucket") == "delta":
            return "incompatible"
    return "factual"


def _pending_receipts(connection, extensions=None, target=None):
    counts = {"engine": 0, "incompatible": 0, "factual": 0, "unrelated": 0, "unclassified": 0, "unverified": 0,
              "unclassified_refs": [], "details": {}}
    registry = target.registry if target is not None else _pending_registry(extensions or NO_EXTENSIONS)
    # Materialised first: each proof builds a unit of work that (re)registers SQL functions on this connection, which SQLite
    # refuses while a statement of the same connection is still open.
    rows = connection.execute("SELECT id, operation_name, payload_hash, change_set FROM apply_receipts WHERE " +
                              ACTIVE_RECEIPT_PREDICATE).fetchall()
    for row in rows:
        receipt_id, operation, payload_hash, raw = row
        try:
            plan = ChangeSet.from_bytes(raw, expected_hash=payload_hash)
            if plan.operation_name != operation:
                raise DomainError("invalid_plan", "The pending receipt operation does not match its plan")
        except DomainError:
            counts["unclassified"] += 1
            counts["unclassified_refs"].append({"receipt_id": receipt_id, "operation": operation,
                                                "references": ["unreadable_pending_plan"]})
            continue
        status = _classify_pending_plan(connection, operation, plan, registry,
                                        target_validation=target is not None and target.runtime is not None)
        reason = None
        if status == "factual":
            # A saved plan is never called resumable from its own row: the real target contracts prove it, read-only.
            status, reason = _prove_factual(target, connection, "receipt", receipt_id)
        counts[status] += 1
        if status in ("incompatible", "unverified"):
            counts["details"][receipt_id] = {"operation": operation, "status": status,
                                             "reason": reason or ("procedural_or_unprovable_effects"
                                                                  if status == "incompatible" else
                                                                  "target_factual_validation_unavailable")}
    return counts


def _prove_factual(target, connection, kind: str, identity: str) -> tuple[str, str | None]:
    """``("factual", None)`` when the target Application contracts accept the saved plan; otherwise why not."""
    if target is None or target.runtime is None:
        return "unverified", "target_factual_validation_unavailable"
    from qorqut.bootstrap import learning_removal_pending as pending
    try:
        (pending.prove_receipt if kind == "receipt" else pending.prove_signoff)(target, connection, identity)
    except DomainError as exc:
        return ("unverified" if exc.code in ("unverified_pending_plan", "unclassified_pending_plan") else
                "incompatible"), exc.code
    except Exception:
        return "unverified", "target_factual_validation_failed"
    return "factual", None


def _pending_signoffs(connection, extensions=None, target=None):
    """Pending/approved Signoffs pass through the same actual-effect classification as application receipts."""
    counts = {"engine": 0, "incompatible": 0, "factual": 0, "unrelated": 0, "unclassified": 0, "unverified": 0,
              "details": []}
    registry = target.registry if target is not None else _pending_registry(extensions or NO_EXTENSIONS)
    rows = connection.execute("SELECT id, operation_name, payload_hash, change_set, category FROM signoffs WHERE " +
                              ACTIVE_SIGNOFF_PREDICATE).fetchall()
    for row in rows:
        signoff_id, operation = row[0], row[1]
        try:
            plan = ChangeSet.from_bytes(row[3], expected_hash=row[2])
            if plan.operation_name != operation:
                raise DomainError("invalid_plan", "The pending signoff operation does not match its plan")
        except DomainError:
            counts["unclassified"] += 1
            counts["details"].append({"signoff_id": signoff_id, "operation": operation,
                                      "references": ["unreadable_pending_plan"]})
            continue
        if row[4] == "input_request":
            from qorqut.bootstrap import learning_removal_pending as pending
            try:
                pending.prove_input_request(target, connection, signoff_id)
                status, reason = "factual", None
            except DomainError as exc:
                status = "unverified" if exc.code == "unverified_pending_plan" else "incompatible"
                reason = exc.code
            except Exception:
                status, reason = "unverified", "target_input_validation_failed"
        else:
            status = _classify_pending_plan(connection, operation, plan, registry,
                                            target_validation=target is not None and target.runtime is not None)
            reason = None
            if status == "factual":
                status, reason = _prove_factual(target, connection, "signoff", signoff_id)
        counts[status] += 1
        if status in ("incompatible", "engine", "unclassified", "unverified"):
            counts["details"].append({"signoff_id": signoff_id, "operation": operation, "status": status,
                                      **({"reason": reason} if reason else {})})
    return counts


def _published_role_defaults(connection, paths: InstancePaths, role: dict):
    """Load and digest-verify the exact published role package pinned by an active profile."""
    if (not isinstance(role, dict) or not isinstance(role.get("id"), str) or not isinstance(role.get("version"), str)
            or not isinstance(role.get("digest"), str)):
        raise DomainError("invalid_profile", "An active profile has no valid role reference")
    row = connection.execute("SELECT kind, artifact_id, digest, stored_path, metadata_json FROM artifacts "
                             "WHERE kind='role' AND artifact_id=? AND version=? AND digest=?",
                             (role["id"], role["version"], role["digest"])).fetchone()
    if row is None:
        raise DomainError("missing_artifact", "The profile's published role package is missing")
    artifacts_root = (paths.runtime / "artifacts").resolve()
    root = Path(row[3])
    try:
        root = root.resolve(strict=True)
        root.relative_to(artifacts_root)
        metadata = json.loads(row[4], object_pairs_hook=_unique)
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("invalid_artifact", "The published role package is unavailable") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("manifest_path"), str):
        raise DomainError("invalid_artifact", "The published role package has no manifest reference")
    try:
        from qorqut.catalog.packages import (ROLE_SCHEMA, artifact_file, files_digest, relative_path,
                                               validate_schema)
        from qorqut_operations.storage.structured import parse_yaml
        manifest_path = relative_path(metadata["manifest_path"])
        manifest_raw = artifact_file(root, manifest_path)
        manifest = parse_yaml(manifest_raw.decode("utf-8"))
        validate_schema(manifest, ROLE_SCHEMA, manifest_path)
        files = {manifest_path: manifest_raw}
        files[relative_path(manifest["brief"])] = artifact_file(root, manifest["brief"])
        for skill in manifest["skills"]:
            path = relative_path(skill["path"])
            files[path] = artifact_file(root, path)
            subtree = root / PurePosixPath(path).parent
            for child in sorted(subtree.rglob("*")):
                if child.is_symlink():
                    raise DomainError("invalid_artifact", "A published skill contains a symlink")
                if child.is_file():
                    name = child.relative_to(root).as_posix()
                    files[relative_path(name)] = artifact_file(root, name)
        digest = files_digest(files)
    except DomainError:
        raise
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise DomainError("invalid_artifact", "The published role package is unreadable") from exc
    if digest != role["digest"]:
        raise DomainError("artifact_conflict", "The published role package bytes do not match the profile pin")
    defaults = manifest.get("default_tool_policy")
    if not isinstance(defaults, dict):
        raise DomainError("invalid_artifact", "The published role package has no default tool policy")
    return defaults


def _policy_deficits(connection, paths, extensions=NO_EXTENSIONS, target=None) -> list[dict]:
    """Effective role defaults plus profile overrides that name a retired or unknown target operation."""
    registry = target.registry if target is not None else _pending_registry(extensions)
    profiles = []
    for row in connection.execute("SELECT agent_id, profile_json FROM agent_profiles WHERE status='active'"):
        document = _decode(row[1])
        if not isinstance(document, dict):
            raise DomainError("invalid_profile", "An active profile is not a JSON object")
        role = document.get("role") if isinstance(document.get("role"), dict) else {}
        overrides = (((document.get("tool_policy") or {}).get("overrides")) if isinstance(document.get("tool_policy"), dict)
                     else {})
        if not isinstance(overrides, dict):
            raise DomainError("invalid_profile", "An active profile's tool overrides are not an object")
        stale_overrides = sorted(name for name in overrides
                                 if isinstance(name, str) and (_retired_operation(name) or not registry.known(name)))
        defaults, role_state = {}, "unavailable"
        if paths is not None:
            try:
                defaults = _published_role_defaults(connection, paths, role)
                role_state = "verified"
            except DomainError as exc:
                role_state = "unavailable:" + exc.code
        stale_defaults = sorted(name for name in defaults
                                if isinstance(name, str) and (_retired_operation(name) or not registry.known(name)))
        requires_role = bool(stale_defaults) or role_state != "verified"
        requires_profile = bool(stale_overrides) or requires_role
        if requires_profile:
            profiles.append({"agent_id": row[0], "role_id": role.get("id"), "role_version": role.get("version"),
                             "role_digest": role.get("digest"), "role_state": role_state,
                             "stale_defaults": stale_defaults[:MAX_REPORTED_REFERENCES],
                             "stale_overrides": stale_overrides[:MAX_REPORTED_REFERENCES],
                             "requires_role": requires_role, "requires_profile": requires_profile})
    return profiles


# -- baselines and exact row fingerprints -----------------------------------------------------------------------

def row_fingerprint(columns, values) -> str:
    """A type-exact fingerprint of one row: text/JSON bytes, integers, reals, blobs and NULL stay distinguishable."""
    payload = []
    for column, value in zip(columns, values):
        if value is None:
            payload.append([column, "null"])
        elif isinstance(value, bool):
            payload.append([column, "int", int(value)])
        elif isinstance(value, int):
            payload.append([column, "int", value])
        elif isinstance(value, float):
            payload.append([column, "real", repr(value)])
        elif isinstance(value, str):
            payload.append([column, "text", value])
        elif isinstance(value, (bytes, bytearray, memoryview)):
            payload.append([column, "blob", bytes(value).hex()])
        else:  # pragma: no cover - sqlite3 returns only the handled scalar types
            raise DomainError("storage_error", "A row value is not a SQLite scalar")
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _column_names(connection, table):
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _key_columns(connection, table, assembly=None):
    columns = _column_names(connection, table)
    spec = assembly.tables.get(table) if assembly is not None else None
    if spec is not None:
        keys = tuple(spec.keys)
    elif table == IDENTITY_TABLE:
        keys = ("id",)
    else:
        declared = [row for row in connection.execute(f'PRAGMA table_info("{table}")') if row[5]]
        keys = tuple(row[1] for row in sorted(declared, key=lambda row: row[5]))
        if not keys:
            keys = ("id",) if "id" in columns else columns
    missing = [name for name in keys if name not in columns]
    if missing or not keys:
        raise DomainError("unsupported_schema", f"The table {table} has no usable primary-key specification")
    return columns, tuple(keys)


def table_manifest(connection, table, *, assembly=None) -> dict:
    """Every row of one table as ``key -> exact fingerprint``; a bounded private projection, never row contents."""
    columns, keys = _key_columns(connection, table, assembly)
    positions = [columns.index(name) for name in keys]
    selected = ", ".join(_quote(column) for column in columns)
    rows = {}
    for row in connection.execute(f'SELECT {selected} FROM "{table}"'):
        key = canonical_bytes([_json_scalar(row[index]) for index in positions]).decode("utf-8")
        rows[key] = row_fingerprint(columns, row)
    payload = {"keys": list(keys), "count": len(rows), "rows": rows}
    payload["digest"] = canonical_hash(payload)
    return payload


def _json_scalar(value):
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if isinstance(value, float):
        return {"real": repr(value)}
    return value


def baseline_manifest(connection, *, assembly=None) -> dict:
    """All existing tables' exact row manifests plus their combined digest. Callers keep it private."""
    tables = {}
    for name in sorted(_tables(connection)):
        if name.startswith("sqlite_"):
            continue
        tables[name] = table_manifest(connection, name, assembly=assembly)
    payload = {"tables": tables}
    payload["digest"] = canonical_hash({name: value["digest"] for name, value in tables.items()})
    return payload


def manifest_difference(before: dict, after: dict) -> dict:
    """Every table/key whose exact fingerprint changed, appeared or disappeared, with both fingerprints."""
    changed = {}
    for table in sorted(set(before["tables"]) | set(after["tables"])):
        old_rows = before["tables"].get(table, {"rows": {}})["rows"]
        new_rows = after["tables"].get(table, {"rows": {}})["rows"]
        difference = {}
        for key in sorted(set(old_rows) | set(new_rows)):
            if old_rows.get(key) != new_rows.get(key):
                difference[key] = {"before": old_rows.get(key), "after": new_rows.get(key)}
        if difference:
            changed[table] = difference
    return changed


# -- durable chunked baseline evidence ---------------------------------------------------------------------------

def write_manifest_bundle(directory: Path, name: str, manifest: dict) -> dict:
    """Write one exact baseline as a private, chunked, self-indexed bundle.

    Row detail is gzip-compressed per bounded chunk; the index carries each table's exact digest and the hash of
    every chunk. Recovery reads only the small index and verifies chunk integrity without loading the whole store,
    so an exhaustive store manifest is never written in a representation that a later retry refuses to read.
    """
    base = _require_private_directory(Path(directory) / name, create=True)
    tables_directory = _require_private_directory(base / "tables", create=True)
    table_index = {}
    for table, value in sorted(manifest["tables"].items()):
        prefix = hashlib.sha256(table.encode("utf-8")).hexdigest()[:16]
        chunks = []
        rows = sorted(value["rows"].items())
        for offset in range(0, len(rows), MANIFEST_CHUNK_ROWS):
            chunk = rows[offset:offset + MANIFEST_CHUNK_ROWS]
            payload = json.dumps({"table": table, "keys": list(value["keys"]), "rows": dict(chunk)},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            compressed = gzip.compress(payload, compresslevel=6, mtime=0)
            if len(compressed) > MAX_MANIFEST_CHUNK_BYTES:
                raise DomainError("limit_exceeded", "A baseline manifest chunk exceeds its bound")
            file_name = f"{prefix}.{len(chunks):06d}.json.gz"
            written = _require_private_directory(tables_directory, create=False) / file_name
            write_private_file(tables_directory, file_name, compressed)
            chunks.append({"file": file_name, "sha256": hashlib.sha256(compressed).hexdigest(), "rows": len(chunk)})
        table_index[table] = {"keys": list(value["keys"]), "count": value["count"], "digest": value["digest"],
                              "chunks": chunks}
    index = {"format": MANIFEST_FORMAT, "tables": table_index, "files": manifest["files"],
             "identity": manifest["identity"], "digest": manifest["digest"]}
    index_bytes = (json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    write_private_file(base, "index.json", index_bytes)
    return {"path": str(base), "digest": manifest["digest"],
            "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "tables": len(table_index), "rows": sum(value["count"] for value in table_index.values())}


def read_manifest_index(directory: Path, name: str) -> dict:
    """Read and validate the small durable index; missing, altered or truncated evidence is never skipped."""
    base = Path(directory) / name
    try:
        require_private_directory(base)
        raw = read_private_file(base / "index.json", max_bytes=MAX_MANIFEST_INDEX_BYTES)
        index = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (DomainError, OSError, ValueError, TypeError) as exc:
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest index is missing or unreadable",
                          details={"bundle": str(base)}) from exc
    if not isinstance(index, dict) or set(index) != {"format", "tables", "files", "identity", "digest"}:
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest index has an unexpected shape")
    if (index["format"] != MANIFEST_FORMAT or not isinstance(index["tables"], dict) or not index["tables"]
            or not isinstance(index["files"], dict) or not isinstance(index["identity"], dict)
            or not isinstance(index["digest"], str) or not HASH.fullmatch(index["digest"])):
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest index is not this format")
    for path, value in index["files"].items():
        if (not isinstance(path, str) or not isinstance(value, dict)
                or set(value) != {"sha256", "exists", "mode"}
                or not isinstance(value["exists"], bool)
                or (value["exists"] and (not isinstance(value["sha256"], str) or not HASH.fullmatch(value["sha256"])
                                        or type(value["mode"]) is not int))
                or (not value["exists"] and (value["sha256"] is not None or value["mode"] is not None))):
            raise DomainError("migration_evidence_invalid", "A durable baseline file entry is invalid")
    for table, value in index["tables"].items():
        if (not isinstance(table, str) or not isinstance(value, dict)
                or set(value) != {"keys", "count", "digest", "chunks"}
                or not isinstance(value["keys"], list) or type(value["count"]) is not int or value["count"] < 0
                or not isinstance(value["digest"], str) or not HASH.fullmatch(value["digest"])
                or not isinstance(value["chunks"], list) or sum(chunk.get("rows", -1) for chunk in value["chunks"]) != value["count"]):
            raise DomainError("migration_evidence_invalid", "A durable table manifest index entry is invalid",
                              details={"table": table})
        for chunk in value["chunks"]:
            if (not isinstance(chunk, dict) or set(chunk) != {"file", "sha256", "rows"}
                    or not isinstance(chunk["file"], str) or not FILENAME_SAFE.fullmatch(chunk["file"])
                    or not isinstance(chunk["sha256"], str) or not HASH.fullmatch(chunk["sha256"])
                    or type(chunk["rows"]) is not int or chunk["rows"] < 0):
                raise DomainError("migration_evidence_invalid", "A durable manifest chunk entry is invalid")
    if index["digest"] != canonical_hash({"tables": {name: value["digest"] for name, value in index["tables"].items()},
                                           "files": index["files"], "identity": index["identity"]}):
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest index digest was altered")
    return index


def verify_manifest_bundle(directory: Path, name: str, *, expected_digest=None) -> dict:
    """Verify the durable index and every chunk hash; return the index for digest/file comparisons."""
    base = Path(directory) / name
    index = read_manifest_index(directory, name)
    if expected_digest is not None and index["digest"] != expected_digest:
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest digest does not match its receipt")
    tables_directory = base / "tables"
    total = 0
    for table, value in sorted(index["tables"].items()):
        reconstructed = {}
        for chunk in value["chunks"]:
            try:
                require_private_directory(tables_directory)
                raw = read_private_file(tables_directory / chunk["file"], max_bytes=MAX_MANIFEST_CHUNK_BYTES)
                if hashlib.sha256(raw).hexdigest() != chunk["sha256"]:
                    raise DomainError("migration_evidence_invalid", "A durable baseline manifest chunk was altered")
                payload = json.loads(gzip.decompress(raw), object_pairs_hook=_unique, parse_constant=_reject_constant)
                if (not isinstance(payload, dict) or set(payload) != {"table", "keys", "rows"}
                        or payload["table"] != table or payload["keys"] != value["keys"]
                        or not isinstance(payload["rows"], dict) or len(payload["rows"]) != chunk["rows"]):
                    raise DomainError("migration_evidence_invalid", "A durable baseline manifest chunk is inconsistent")
                if set(payload["rows"]) & set(reconstructed):
                    raise DomainError("migration_evidence_invalid", "A durable baseline manifest repeats a row key")
                reconstructed.update(payload["rows"])
            except (DomainError, OSError, ValueError, TypeError, gzip.BadGzipFile) as exc:
                raise DomainError("migration_evidence_invalid", "A durable baseline manifest chunk is missing or corrupt",
                                  details={"table": table, "file": chunk["file"]}) from exc
            total += chunk["rows"]
        expected = canonical_hash({"keys": value["keys"], "count": value["count"], "rows": reconstructed})
        if len(reconstructed) != value["count"] or expected != value["digest"]:
            raise DomainError("migration_evidence_invalid", "A durable table manifest does not match its rows",
                              details={"table": table})
    if total != sum(value["count"] for value in index["tables"].values()):
        raise DomainError("migration_evidence_invalid", "A durable baseline manifest is incomplete")
    return index


def manifest_summary(index: dict) -> dict:
    """The small shape used for exact retained-row comparison during recovery and no-op verification."""
    return {"tables": {name: {"keys": value["keys"], "count": value["count"], "digest": value["digest"]}
                       for name, value in index["tables"].items()},
            "files": index["files"], "identity": index["identity"], "digest": index["digest"]}


def _file_state(path: Path) -> dict:
    """The current digest of one planned file; absent, symlinked, non-regular and oversized files stay explicit."""
    if not os.path.lexists(path):
        return {"sha256": None, "exists": False, "mode": None}
    if os.path.islink(path) or not os.path.isfile(path):
        raise DomainError("invalid_configuration", "A planned preparation file is a symlink or not a regular file",
                          details={"path": str(path)})
    info = os.lstat(path)
    if info.st_size > MAX_PREPARATION_FILE_BYTES:
        raise DomainError("invalid_configuration", "A planned preparation file exceeds its documented bound",
                          details={"path": str(path)})
    with path.open("rb") as handle:
        return {"sha256": hashlib.file_digest(handle, "sha256").hexdigest(), "exists": True,
                "mode": stat.S_IMODE(info.st_mode)}


def tree_files(root: Path) -> dict:
    """Every regular file under a preparation-owned tree (absent root = empty), path-safe and bounded.

    A symlink, a special file, an oversized file or a tree beyond its documented bounds refuses; empty directories are
    not inventoried. The state is the file manifest's own ``sha256``/``exists``/``mode`` shape."""
    root = Path(root)
    if not os.path.lexists(root):
        return {}
    if os.path.islink(root) or not os.path.isdir(root):
        raise DomainError("invalid_configuration", "A preparation tree root is a symlink or not a directory",
                          details={"root": str(root)})
    result, total = {}, 0

    def walk(directory):
        nonlocal total
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_symlink():
                    raise DomainError("invalid_configuration", "A preparation tree crosses a symlink",
                                      details={"path": entry.path})
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise DomainError("invalid_configuration", "A preparation tree holds a special file",
                                      details={"path": entry.path})
                if len(result) >= MAX_TREE_FILES:
                    raise DomainError("limit_exceeded", "A preparation tree exceeds its documented file bound",
                                      details={"root": str(root)})
                state = _file_state(Path(entry.path))
                total += os.lstat(entry.path).st_size
                if total > MAX_TREE_BYTES:
                    raise DomainError("limit_exceeded", "A preparation tree exceeds its documented size bound",
                                      details={"root": str(root)})
                result[entry.path] = state

    walk(str(root))
    return result


def tree_inventory_digest(root: Path) -> str:
    return canonical_hash([[path, state["sha256"], state["mode"]] for path, state in sorted(tree_files(root).items())])


def _under_tree(path: str, preparation) -> bool:
    return any(path.startswith(item["root"] + os.sep) for item in preparation.get("trees", []))


def _file_manifest(preparation) -> dict:
    manifest = {item["path"]: _file_state(Path(item["path"])) for item in preparation["files"]}
    for item in preparation.get("trees", []):
        manifest.update(tree_files(Path(item["root"])))
    return manifest


def _require_instance_file(paths: InstancePaths, path: Path, what: str) -> None:
    """Require a lexical absolute path inside the real instance root, with no symlinked ancestor."""
    try:
        root = Path(paths.instance_root).resolve(strict=True)
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError
        lexical = Path(os.path.abspath(candidate))
        lexical.relative_to(Path(os.path.abspath(paths.instance_root)))
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
        probe = candidate
        while True:
            if probe.is_symlink():
                raise ValueError
            if probe == probe.parent:
                break
            probe = probe.parent
            try:
                probe.relative_to(Path(os.path.abspath(paths.instance_root)))
            except ValueError:
                break
    except (OSError, ValueError) as exc:
        raise DomainError("invalid_configuration", f"{what} must be inside the instance root without symlinks") from exc


def write_file_bundle(paths: InstancePaths, directory: Path, preparation: dict, baseline_a: dict) -> dict:
    """A private restore bundle of the complete reviewed affected file set, captured before preparation.

    The bundle keeps exact bytes (content-addressed), absence and the executable mode, never a path outside the
    instance. It is evidence for rollback/rehearsal only; this transition never executes a live restore.
    """
    base = _require_private_directory(Path(directory) / "restore", create=True)
    blobs = _require_private_directory(base / "blobs", create=True)
    files = {}
    for name in sorted(baseline_a["files"]):
        path = Path(name)
        _require_instance_file(paths, path, "A planned preparation file")
        state = _file_state(path)
        if state["exists"]:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise DomainError("invalid_configuration", "A planned preparation file is not a private regular file")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != state["sha256"]:
                raise DomainError("operation_conflict", "A planned preparation file changed while it was backed up")
            name = state["sha256"]
            if not (blobs / name).exists():
                write_private_file(blobs, name, data)
            files[str(path)] = {"exists": True, "sha256": state["sha256"], "size": len(data),
                                "mode": stat.S_IMODE(info.st_mode)}
        else:
            files[str(path)] = {"exists": False, "sha256": None, "size": None, "mode": None}
    body = {"format": MANIFEST_FORMAT, "instance_root": str(paths.instance_root), "files": files,
            "trees": [item["root"] for item in preparation.get("trees", [])]}
    body["digest"] = canonical_hash(body)
    raw = (json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    write_private_file(base, "index.json", raw)
    if ({path: {"sha256": value["sha256"], "exists": value["exists"],
                "mode": value["mode"] if value["exists"] else None}
         for path, value in files.items()} != baseline_a["files"]):
        raise DomainError("operation_conflict", "The file restore bundle does not match baseline A")
    return {"path": str(base), "digest": body["digest"], "sha256": hashlib.sha256(raw).hexdigest(),
            "files": len(files), "present": sum(1 for value in files.values() if value["exists"])}


def verify_file_bundle(directory: Path, preparation: dict, baseline_a: dict, *, expected_digest=None) -> dict:
    """Verify every blob, absence and mode against baseline A and the reviewed file set."""
    base = Path(directory) / "restore"
    try:
        require_private_directory(base)
        raw = read_private_file(base / "index.json", max_bytes=MAX_MANIFEST_INDEX_BYTES)
        body = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (DomainError, OSError, ValueError, TypeError) as exc:
        raise DomainError("migration_evidence_invalid", "The file restore bundle is missing or unreadable") from exc
    if not isinstance(body, dict) or set(body) != {"format", "instance_root", "files", "trees", "digest"}:
        raise DomainError("migration_evidence_invalid", "The file restore bundle has an unexpected shape")
    if body["format"] != MANIFEST_FORMAT or not isinstance(body["files"], dict) or not isinstance(body["trees"], list) \
            or body["digest"] != canonical_hash({key: body[key] for key in ("format", "instance_root", "files", "trees")}):
        raise DomainError("migration_evidence_invalid", "The file restore bundle was altered")
    if expected_digest is not None and body["digest"] != expected_digest:
        raise DomainError("migration_evidence_invalid", "The file restore bundle digest does not match its attempt")
    if set(body["files"]) != set(baseline_a["files"]) or ("trees" in preparation and body["trees"] != [
            item["root"] for item in preparation["trees"]]):
        raise DomainError("migration_evidence_invalid", "The file restore bundle does not cover the reviewed files")
    blobs = base / "blobs"
    for path, value in body["files"].items():
        if not isinstance(value, dict) or set(value) != {"exists", "sha256", "size", "mode"}:
            raise DomainError("migration_evidence_invalid", "A file restore entry is invalid")
        if value["exists"]:
            if (not isinstance(value["sha256"], str) or not HASH.fullmatch(value["sha256"])
                    or type(value["size"]) is not int or type(value["mode"]) is not int):
                raise DomainError("migration_evidence_invalid", "A file restore entry is invalid")
            try:
                require_private_directory(blobs)
                data = read_private_file(blobs / value["sha256"], max_bytes=MAX_PREPARATION_FILE_BYTES)
            except (DomainError, OSError) as exc:
                raise DomainError("migration_evidence_invalid", "A file restore blob is missing") from exc
            if len(data) != value["size"] or hashlib.sha256(data).hexdigest() != value["sha256"]:
                raise DomainError("migration_evidence_invalid", "A file restore blob does not match its index")
        elif value != {"exists": False, "sha256": None, "size": None, "mode": None}:
            raise DomainError("migration_evidence_invalid", "An absent file restore entry is invalid")
    if ({path: {"sha256": value["sha256"], "exists": value["exists"],
                "mode": value["mode"] if value["exists"] else None}
         for path, value in body["files"].items()} != baseline_a["files"]):
        raise DomainError("migration_evidence_invalid", "The file restore bundle does not match baseline A")
    return body


def restore_file_bundle(bundle_directory: Path, *, target_root: Path) -> list[str]:
    """Rehearse a restore of the reviewed files into an explicit synthetic root. Never targets a live instance.

    The caller supplies a disposable root; every original path is mapped under it. Database bytes are restored
    separately from the verified backup with ordinary SQLite tooling.
    """
    base = Path(bundle_directory)
    raw = read_private_file(base / "index.json", max_bytes=MAX_MANIFEST_INDEX_BYTES)
    body = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    if (not isinstance(body, dict) or set(body) != {"format", "instance_root", "files", "trees", "digest"}
            or body.get("format") != MANIFEST_FORMAT or not isinstance(body.get("instance_root"), str)
            or not isinstance(body.get("files"), dict) or not isinstance(body.get("trees"), list)
            or body.get("digest") != canonical_hash({key: body[key] for key in ("format", "instance_root", "files",
                                                                                "trees")})):
        raise DomainError("invalid_configuration", "The file restore bundle is invalid")
    for original, value in body["files"].items():
        if not isinstance(original, str) or not isinstance(value, dict) or set(value) != {"exists", "sha256", "size", "mode"}:
            raise DomainError("invalid_configuration", "A file restore entry is invalid")
        if value["exists"]:
            if (not isinstance(value["sha256"], str) or not HASH.fullmatch(value["sha256"])
                    or type(value["size"]) is not int or type(value["mode"]) is not int):
                raise DomainError("invalid_configuration", "A file restore entry is invalid")
            blob = read_private_file(base / "blobs" / value["sha256"], max_bytes=MAX_PREPARATION_FILE_BYTES)
            if len(blob) != value["size"] or hashlib.sha256(blob).hexdigest() != value["sha256"]:
                raise DomainError("migration_evidence_invalid", "A file restore blob does not match its index")
        elif value != {"exists": False, "sha256": None, "size": None, "mode": None}:
            raise DomainError("invalid_configuration", "An absent file restore entry is invalid")
    root = Path(target_root)
    if not root.is_absolute() or ".." in root.parts or os.path.islink(root) or not root.is_dir():
        raise DomainError("invalid_configuration", "A restore rehearsal needs an existing explicit directory")
    instance_root = Path(body["instance_root"])
    restored = []
    for original, value in sorted(body["files"].items()):
        try:
            relative = Path(original).relative_to(instance_root)
        except ValueError as exc:
            raise DomainError("invalid_configuration", "A file restore path is outside its instance") from exc
        target = root / relative
        if value["exists"]:
            data = read_private_file(base / "blobs" / value["sha256"], max_bytes=MAX_PREPARATION_FILE_BYTES)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private_file(target.parent, target.name, data)
            os.chmod(target, value["mode"])
            restored.append(str(target))
        else:
            if os.path.lexists(target):
                info = os.lstat(target)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise DomainError("invalid_configuration", "An absent restore target is not a plain file")
                target.unlink()
            restored.append(str(target))
    for tree in body["trees"]:
        # A file a stage created inside a reviewed tree is absent at baseline A: the rehearsal removes it.
        try:
            mapped = root / Path(tree).relative_to(instance_root)
        except ValueError as exc:
            raise DomainError("invalid_configuration", "A file restore tree is outside its instance") from exc
        for path in sorted(tree_files(mapped)):
            if str(Path(instance_root) / Path(path).relative_to(root)) not in body["files"]:
                Path(path).unlink()
                restored.append(path)
    return restored


def _identity_values(connection) -> dict:
    rows = connection.execute(f'SELECT id, name, version, created_at FROM "{IDENTITY_TABLE}"').fetchall()
    if len(rows) != 1:
        raise DomainError("unsupported_schema", "The store does not carry one target identity row")
    return dict(zip(("id", "name", "version", "created_at"), tuple(rows[0])))


def _baseline_with_files(tables: dict, files: dict, identity: dict) -> dict:
    payload = {"tables": tables, "files": files, "identity": identity}
    payload["digest"] = canonical_hash({"tables": {name: value["digest"] for name, value in tables.items()},
                                        "files": {name: value for name, value in files.items()},
                                        "identity": identity})
    return payload


# -- preparation receipt reconciliation --------------------------------------------------------------------------

SUPPORTED_EVIDENCE_KINDS = {}
# The first-wave slice cannot prove a record-only agents_update/roles_publish application receipt and its complete
# bookkeeping through the genuine Application file/record paths. Keep them explicit unsupported kinds until the
# integration coordinator supplies that evidence; do not accept a fabricated apply_receipts row as proof.
UNSUPPORTED_EVIDENCE_KINDS = ("prepared_catalog", "extension_declaration")


def _installation_owner(connection) -> str:
    row = connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
    if row is None or not isinstance(row[0], str):
        raise DomainError("unverified_preparation", "The instance has no authoritative owner principal; preparation "
                          "evidence cannot be verified")
    return row[0]


def _preparation_evidence(step, connection, paths: InstancePaths, plan_attempt, baseline_a, baseline_b) -> dict:
    """The authoritative evidence of one step: the persisted receipts, files and private immutable step evidence.

    The allowed row keys and file paths are derived from the reviewed intent's operation semantics and the retained API
    receipts (``learning_removal_evidence``); a kind without such a verifier stays explicitly unverified."""
    kind = step.get("kind") if isinstance(step, dict) else None
    if kind in UNSUPPORTED_EVIDENCE_KINDS or not isinstance(kind, str):
        raise DomainError("unverified_preparation", "This transition has no authoritative evidence adapter; the preparation "
                          "kind remains unverified until integration supplies genuine execution evidence",
                          details={"kind": kind})
    from qorqut.bootstrap import learning_removal_evidence as step_evidence
    return step_evidence.verify_step(step, connection, paths, plan_attempt, baseline_a, baseline_b)


def validate_preparation_receipt(receipt, *, plan: dict, baseline_a: dict, baseline_b: dict, connection=None,
                                 plan_attempt=None) -> dict:
    """The reviewed A-to-B accounting validator: exact row/file changes only, and shared learning stays untouched.

    ``receipt`` is the coordinator's concrete record of the authorized preparation steps; ``baseline_a``/``baseline_b``
    are the manifests captured by this transition (``files`` keys present). Every declared before/after fingerprint must
    equal the real A/B value, every real difference must be claimed exactly once, and no step may claim the five shared
    learning tables or an object this transition removes.
    """
    if not isinstance(receipt, dict) or set(receipt) != PREPARATION_RECEIPT_KEYS:
        raise DomainError("invalid_configuration", "The preparation receipt has an unexpected shape")
    if receipt["format"] != 1 or receipt["transition"] != TRANSITION_ID:
        raise DomainError("invalid_configuration", "The preparation receipt is not this transition")
    if receipt["plan_digest"] != plan["plan_digest"]:
        raise DomainError("operation_conflict", "The preparation receipt belongs to another transition plan")
    steps = receipt["steps"]
    if not isinstance(steps, list) or len(steps) != len(plan["preparation"]["steps"]):
        raise DomainError("operation_conflict", "The preparation receipt does not account for exactly the planned steps")
    expected = {(step["kind"], step["identity"]): step for step in plan["preparation"]["steps"]}
    if {(step.get("kind"), step.get("identity")) for step in steps if isinstance(step, dict)} != set(expected):
        raise DomainError("operation_conflict", "The preparation receipt steps are not exactly the reviewed plan's steps")
    claimed_rows, claimed_files, accounting = {}, {}, []
    inputs = plan["preparation"].get("inputs")
    if inputs is not None:
        order = [(intent["kind"], intent["identity"]) for intent in inputs["derived"]["intents"]]
        if [(step.get("kind"), step.get("identity")) for step in steps if isinstance(step, dict)] != order:
            raise DomainError("operation_conflict", "The preparation receipt steps are not in the reviewed execution order")
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"kind", "identity", "digest", "operation", "receipt_id", "rows",
                                                       "files"}:
            raise DomainError("invalid_configuration", "A preparation step has an unexpected shape")
        identity = (step.get("kind"), step.get("identity"))
        if identity not in expected:
            raise DomainError("operation_conflict", "A preparation receipt step is not in the reviewed plan")
        if step.get("digest") != expected[identity]["digest"]:
            raise DomainError("operation_conflict", "A preparation step's plan digest changed")
        if not isinstance(step.get("operation"), str) or not OPERATION_NAME.fullmatch(step["operation"]):
            raise DomainError("invalid_configuration", "A preparation step needs its concrete operation identity")
        if not isinstance(step.get("receipt_id"), str) or not IDENTITY.fullmatch(step["receipt_id"]):
            raise DomainError("invalid_configuration", "A preparation step needs its concrete operation receipt identity")
        rows, files = step.get("rows", []), step.get("files", [])
        if not isinstance(rows, list) or not isinstance(files, list):
            raise DomainError("invalid_configuration", "A preparation step's row/file accounting is missing")
        if connection is None or plan_attempt is None:
            raise DomainError("unverified_preparation", "A preparation receipt with steps needs authoritative persisted "
                              "evidence; caller-authored hashes are never sufficient")
        evidence = _preparation_evidence(step, connection, plan_attempt.paths, plan_attempt, baseline_a, baseline_b)
        if not rows and not files and not evidence.get("noop"):
            raise DomainError("operation_conflict", "A preparation step claims no concrete row or file effect")
        recorded = evidence.get("claims")
        if recorded is not None and (
                sorted((row.get("table"), row.get("keys"), row.get("before"), row.get("after")) for row in rows if isinstance(row, dict))
                != sorted((row["table"], row["keys"], row["before"], row["after"]) for row in recorded["rows"])
                or sorted((item.get("path"), item.get("before"), item.get("after")) for item in files if isinstance(item, dict))
                != sorted((item["path"], item["before"], item["after"]) for item in recorded["files"])):
            raise DomainError("operation_conflict", "A preparation step's claims are not the durable step evidence",
                              details={"identity": step["identity"]})
        for row in rows:
            _claim_row(row, identity, plan, baseline_a, baseline_b, claimed_rows, allowed=evidence["rows"])
        for item in files:
            _claim_file(item, identity, plan, baseline_a, baseline_b, claimed_files, allowed=evidence["files"])
        accounting.append({"kind": step["kind"], "identity": step["identity"], "plan_digest": step["digest"],
                           "operation": step["operation"], "receipt_id": step["receipt_id"],
                           "rows": len(rows), "files": len(files)})
    differences = manifest_difference(baseline_a, baseline_b)
    unexplained = []
    for (table, key), chain in sorted(claimed_rows.items()):
        change = differences.get(table, {}).get(key)
        if change is None or change["after"] != chain["after"]:
            raise DomainError("operation_conflict", "A preparation row chain does not end at baseline B",
                              details={"table": table, "key": key, "steps": chain["steps"]})
    for path, chain in sorted(claimed_files.items()):
        after = baseline_b["files"].get(path)
        if (None if after is None else after["sha256"]) != chain["after"]:
            raise DomainError("operation_conflict", "A preparation file chain does not end at baseline B",
                              details={"path": path, "steps": chain["steps"]})
    for table, changed in sorted(differences.items()):
        for key, change in sorted(changed.items()):
            if (table, key) not in claimed_rows:
                unexplained.append({"table": table, "key": key, "before": change["before"], "after": change["after"]})
    file_differences = {path for path in sorted(set(baseline_a["files"]) | set(baseline_b["files"]))
                        if baseline_a["files"].get(path) != baseline_b["files"].get(path)}
    for path in sorted(file_differences - set(claimed_files)):
        unexplained.append({"file": path, "before": baseline_a["files"].get(path),
                            "after": baseline_b["files"].get(path)})
    if unexplained:
        raise DomainError("release_blocked", "Baseline A to B contains changes the authorized preparation receipts do "
                          "not exactly account for", details={"unexplained": unexplained[:MAX_REPORTED_REFERENCES],
                                                              "total": len(unexplained)})
    body = {"format": 1, "transition": TRANSITION_ID, "plan_digest": plan["plan_digest"], "steps": accounting,
            "rows_changed": sum(item["rows"] for item in accounting),
            "files_changed": sum(item["files"] for item in accounting)}
    return {**body, "digest": canonical_hash(body)}


def _claim_row(row, identity, plan, baseline_a, baseline_b, claimed, *, allowed=None):
    if not isinstance(row, dict) or set(row) != {"table", "keys", "before", "after"}:
        raise DomainError("invalid_configuration", "A preparation row claim needs table, keys, before and after")
    table = row["table"]
    if table in RETAINED_LEARNING_TABLES:
        raise DomainError("operation_conflict", "Preparation may not change a shared learning table", details={"table": table})
    if table in REMOVED_TABLES:
        raise DomainError("operation_conflict", "Preparation may not touch a removed engine table", details={"table": table})
    if table in (IDENTITY_TABLE, "sqlite_sequence"):
        raise DomainError("operation_conflict", "Preparation may not claim the target identity", details={"table": table})
    if table not in baseline_a["tables"]:
        raise DomainError("operation_conflict", "A preparation claim names an unknown table", details={"table": table})
    keys = baseline_a["tables"][table]["keys"]
    key = _claim_key(row["keys"], keys)
    actual = manifest_difference({"tables": {table: baseline_a["tables"][table]}},
                                 {"tables": {table: baseline_b["tables"][table]}}).get(table, {}).get(key)
    if actual is None:
        raise DomainError("operation_conflict", "A preparation receipt claims a row that did not change exactly", details={
            "table": table, "key": key})
    if allowed is not None and (table, key) not in allowed:
        raise DomainError("operation_conflict", "A preparation step claims a row its authoritative receipt did not "
                          "authorize", details={"table": table, "key": key})
    if row["before"] == row["after"]:
        raise DomainError("operation_conflict", "A preparation step claims a row it did not change",
                          details={"table": table, "key": key})
    chain = claimed.get((table, key))
    # An ordered chain: the first claim starts exactly at baseline A, every later claim starts exactly where the previous
    # one ended, and the last must end exactly at baseline B (checked once for the whole reconciliation).
    expected_before = actual["before"] if chain is None else chain["after"]
    if row["before"] != expected_before:
        raise DomainError("operation_conflict", "A preparation receipt row fingerprint does not continue its chain from "
                          "baseline A" if chain is None else "Two preparation steps claim the same row without an "
                          "ordered before/after chain", details={"table": table, "key": key})
    if chain is None:
        claimed[(table, key)] = {"after": row["after"], "steps": [identity]}
    else:
        chain["after"] = row["after"]
        chain["steps"].append(identity)


def _claim_key(keys, expected):
    if isinstance(keys, dict):
        if set(keys) != set(expected):
            raise DomainError("invalid_configuration", "A preparation row key names the wrong columns")
        values = [keys[name] for name in expected]
    elif isinstance(keys, list):
        if len(keys) != len(expected):
            raise DomainError("invalid_configuration", "A preparation row key names the wrong columns")
        values = keys
    else:
        raise DomainError("invalid_configuration", "A preparation row key is missing")
    return canonical_bytes([_json_scalar(value) for value in values]).decode("utf-8")


def _claim_file(item, identity, plan, baseline_a, baseline_b, claimed, *, allowed=None):
    if not isinstance(item, dict) or set(item) != {"path", "before", "after"}:
        raise DomainError("invalid_configuration", "A preparation file claim needs path, before and after")
    path = str(_absolute_path(item["path"], "A preparation file claim"))
    if path not in {entry["path"] for entry in plan["preparation"]["files"]} and not _under_tree(path, plan["preparation"]):
        raise DomainError("operation_conflict", "A preparation claim names an undeclared file", details={"path": path})
    before_state, after_state = baseline_a["files"].get(path), baseline_b["files"].get(path)
    before = None if before_state is None else before_state["sha256"]
    after = None if after_state is None else after_state["sha256"]
    if before == after:
        raise DomainError("operation_conflict", "A preparation receipt claims a file that did not change",
                          details={"path": path})
    if item["before"] == item["after"]:
        raise DomainError("operation_conflict", "A preparation step claims a file it did not change", details={"path": path})
    if allowed is not None and path not in allowed:
        raise DomainError("operation_conflict", "A preparation step claims a file its authoritative receipt did not "
                          "authorize", details={"path": path})
    chain = claimed.get(path)
    expected_before = before if chain is None else chain["after"]
    if item["before"] != expected_before:
        raise DomainError("operation_conflict", "A preparation receipt file fingerprint does not continue its chain from "
                          "baseline A" if chain is None else "Two preparation steps claim the same file without an "
                          "ordered before/after chain", details={"path": path})
    if before_state is not None and after_state is not None and before_state["exists"] and after_state["exists"]:
        if before_state.get("mode") != after_state.get("mode"):
            raise DomainError("operation_conflict", "A preparation receipt changed a file mode without an exact claim",
                              details={"path": path})
    if chain is None:
        claimed[path] = {"after": item["after"], "steps": [identity]}
    else:
        chain["after"] = item["after"]
        chain["steps"].append(identity)


# -- apply --------------------------------------------------------------------------------------------------------

@dataclass
class PlanAttempt:
    """One owned, verified transition attempt: baseline A is durable, the preparation phase belongs to the caller."""

    paths: InstancePaths
    transition: _Transition
    plan: dict
    owner: InstanceOwnership
    acquired_here: bool
    backup: dict
    baseline_a: dict
    attempt_directory: Path | None
    attempt_id: str | None
    started_at: str | None = None
    attempt_metadata: dict | None = None
    finished: dict | None = None

    def apply(self, preparation=None) -> dict:
        if self.finished is not None:
            return self.finished
        _verify_ownership(self.owner)
        database = _database_path(self.paths)
        connection = _connect(database, "rw")
        try:
            _verify_ownership(self.owner)
            state, _identity_value, _application, _version, _problems = _recognise(connection, self.transition)
            if state != "predecessor":
                raise DomainError("unsupported_schema", "The store is not the exact predecessor when apply begins",
                                  details={"state": state})
            _require_integrity(connection)
            _require_removed_empty(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise DomainError("busy", "The exclusive schema transaction could not begin", retryable=True) from exc
            try:
                _verify_ownership(self.owner)
                _require_store(connection, self.transition, "predecessor")
                _require_removed_empty(connection)
                _verify_attempt_evidence_files(self)
                _verify_backup_against_a(self.paths, self.transition, self.backup, self.baseline_a)
                if self.plan["preparation"].get("inputs") is not None:
                    from qorqut.bootstrap import learning_removal_preparation as coordinator
                    coordinator.final_inputs(self)
                if _release_evidence(self.paths, self.plan["preparation"], self.transition) != self.plan["release_evidence"]:
                    raise DomainError("operation_conflict", "The verified release inputs changed before the schema "
                                      "transaction")
                gates_b = _gate_rows(connection, self.transition, self.transition.extensions, paths=self.paths,
                                     target=_target_for(self.paths, self.transition, self.transition.extensions,
                                                        self.plan["preparation"]))
                _require_ready(gates_b)
                _require_policy_clear(gates_b["policy_profiles"])
                if _native_catalogs(self.paths, self.transition, self.transition.extensions,
                                    connection=connection)["paths"]:
                    raise DomainError("release_blocked", "Prepared native catalogs still name retired operations at "
                                      "baseline B")
                files_b = _file_manifest(self.plan["preparation"])
                baseline_b = _baseline_with_files(baseline_manifest(connection, assembly=self.transition.predecessor)["tables"],
                                                  files_b, _identity_values(connection))
                reconciliation = _reconcile(self.plan, self.baseline_a, baseline_b, preparation,
                                            connection=connection, plan_attempt=self)
                accounting = reconciliation if reconciliation is not None else _empty_accounting(self.plan)
                _publish_precommit_evidence(self, baseline_b, accounting)
                for table in REMOVED_TABLES:
                    _drop_table(connection, table)
                _update_identity(connection, self.transition.target)
                _require_store(connection, self.transition, "target")
                _require_integrity(connection)
                _require_preserved(connection, self.transition, baseline_b)
                preserved = (connection.execute("PRAGMA application_id").fetchone()[0],
                             connection.execute("PRAGMA user_version").fetchone()[0])
                if preserved != (APPLICATION_ID, self.transition.target.target.version):
                    raise DomainError("unsupported_schema", "The transition changed an application id or user version")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise DomainError("unsupported_schema", "Foreign-key enforcement was not enabled during the DDL")
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
        finally:
            connection.close()
        _verify_ownership(self.owner)
        # Reopen read-only and verify the committed target and declared files against B before claiming completion.
        verification = _verify_target(self.paths, self.transition, baseline_b)
        if _file_manifest(self.plan["preparation"]) != baseline_b["files"]:
            raise DomainError("migration_verification_failed", "A reviewed preparation file changed after the commit")
        result = self._report(applied=True, already_migrated=False, backup=self.backup, baseline_b=baseline_b,
                              accounting=accounting, verification=verification)
        try:
            receipt = self._publish_completion(result)
        except DomainError:
            raise
        except (OSError, ValueError) as exc:
            raise DomainError("migration_receipt_failed", "The target schema may already be committed, but the durable "
                              "completion receipt could not be written; inspect the store and reconcile, never replay",
                              details={"may_be_committed": True, "target": _identity(self.transition.target),
                                       "backup": self.backup}) from exc
        result["receipt"] = str(receipt)
        self.finished = result
        return result

    def abort(self) -> None:
        """Drop only this attempt's ownership; baseline A, the attempt receipt and the backup stay as evidence."""
        self.close()

    def close(self) -> None:
        if self.acquired_here and self.owner.held:
            self.owner.release()

    def _report(self, *, applied, already_migrated, backup, baseline_b, accounting, verification,
                reconciled=False, receipt=None) -> dict:
        return {"format": PLAN_FORMAT, "transition": TRANSITION_ID, "instance_root": str(self.paths.instance_root),
                "database": str(_database_path(self.paths)),
                "selected_extensions": list(self.transition.extensions.ids),
                "predecessor": _identity(self.transition.predecessor),
                "target": _identity(self.transition.target), "plan_digest": self.plan["plan_digest"],
                "applied": applied, "already_migrated": already_migrated, "reconciled": reconciled,
                "backup": backup, "baseline_a": self.baseline_a["digest"],
                "baseline_b": None if baseline_b is None else baseline_b["digest"],
                "preparation": None if accounting is None else {key: accounting[key] for key in
                                                                 ("digest", "rows_changed", "files_changed", "steps")},
                "verification": verification, "receipt": receipt}

    def _publish_completion(self, result) -> Path:
        directory = _require_private_directory(_receipt_directory(self.paths) / self.plan["plan_digest"])
        attempt = _require_private_directory(directory / self.attempt_id) if self.attempt_id else None
        target_directory = attempt or directory
        body = {**result, "completed_at": utc_now()}
        return _write_private_json(target_directory, "completion.json", body)


def begin_attempt(paths: InstancePaths, plan, *, backup, extensions=NO_EXTENSIONS, ownership=None) -> PlanAttempt:
    """Acquire one exclusive ownership boundary, revalidate the plan, take a verified backup and baseline A.

    The returned attempt keeps the caller's or its own lifetime lock. The caller may perform the reviewed preparation
    stages now and hand their receipts to ``attempt.apply``; the attempt never releases a caller-owned lock.
    """
    transition = _transition_for(extensions)
    validated = _validated_plan(paths, plan, transition, exact_inputs=False)
    acquired_here = ownership is None
    owner = InstanceOwnership(paths).acquire() if acquired_here else _caller_ownership(paths, ownership)
    try:
        _verify_ownership(owner)
        database = _database_path(paths)
        _require_store_file(database)
        connection = _connect(database, "rw")
        try:
            state, _identity_value, _application, _version, _problems = _recognise(connection, transition)
            if state == "target":
                return _target_attempt(paths, transition, validated, owner, acquired_here)
            if state != "predecessor":
                raise DomainError("unsupported_schema", "The Operations store is not exactly the predecessor of this "
                                  "transition; refusing to repair or infer it", details={"state": state})
            _refuse_prior_attempt_evidence(paths, validated["plan_digest"])
            validated = _validated_plan(paths, plan, transition, exact_inputs=True)
            _require_integrity(connection)
            _require_gates(connection, paths, transition, validated, extensions)
            _verify_ownership(owner)
            backup_report = _publish_backup(connection, backup, transition)
            baseline_a = _baseline_with_files(baseline_manifest(connection, assembly=transition.predecessor)["tables"],
                                              _file_manifest(validated["preparation"]), _identity_values(connection))
            _verify_ownership(owner)
            started_at = utc_now()
            attempt_id = started_at.replace(":", "").replace(".", "").replace("-", "") + "-" + str(uuid4())
            directory = _require_private_directory(_receipt_directory(paths) / validated["plan_digest"],
                                                   create=True)
            attempt_directory = _require_private_directory(directory / attempt_id, create=True)
            manifest_a = write_manifest_bundle(attempt_directory, "baseline-a", baseline_a)
            restore = write_file_bundle(paths, attempt_directory, validated["preparation"], baseline_a)
            attempt_metadata = {
                "format": PLAN_FORMAT, "transition": TRANSITION_ID, "state": "attempt",
                "plan_digest": validated["plan_digest"], "attempt_id": attempt_id,
                "instance_root": str(paths.instance_root), "database": str(database),
                "selected_extensions": list(transition.extensions.ids),
                "predecessor": _identity(transition.predecessor), "target": _identity(transition.target),
                "backup": backup_report, "baseline_a": baseline_a["digest"],
                "baseline_a_bundle": manifest_a, "restore": restore, "started_at": started_at}
            _write_private_json(attempt_directory, "attempt.json", attempt_metadata)
        finally:
            connection.close()
        return PlanAttempt(paths=paths, transition=transition, plan=validated, owner=owner,
                           acquired_here=acquired_here, backup=backup_report, baseline_a=baseline_a,
                           attempt_directory=attempt_directory, attempt_id=attempt_id, started_at=started_at,
                           attempt_metadata=attempt_metadata)
    except BaseException:
        if acquired_here and owner.held:
            owner.release()
        raise


def apply_migration(paths: InstancePaths, plan, *, backup, preparation=None, extensions=NO_EXTENSIONS,
                    ownership=None) -> dict:
    """The exact database phase in one call: begin the owned attempt and apply it with the supplied receipt.

    Reviewed preparation orchestration uses ``begin_attempt``/``PlanAttempt.apply`` so it can perform its authorized
    stages between baseline A and baseline B. This convenience is for an A=B transition (or an exact-target no-op).
    """
    attempt = begin_attempt(paths, plan, backup=backup, extensions=extensions, ownership=ownership)
    try:
        return attempt.apply(preparation)
    finally:
        attempt.close()


def _target_attempt(paths, transition, plan, owner, acquired_here) -> PlanAttempt:
    """An exact target is never accepted on schema alone: verify its durable B/A/backup evidence first."""
    evidence = _load_attempt_evidence(paths, plan["plan_digest"])
    if evidence is None:
        if plan["preparation"]["steps"] or plan["preparation"]["files"]:
            raise DomainError("migration_evidence_invalid", "An exact target without durable attempt evidence cannot "
                              "account for prepared steps or affected files")
        attempt = PlanAttempt(paths=paths, transition=transition, plan=plan, owner=owner, acquired_here=acquired_here,
                              backup=None, baseline_a={"digest": None}, attempt_directory=None, attempt_id=None)
        verification = _verify_target(paths, transition, None)
        attempt.finished = {**_noop_report(attempt, completion=None), "reconciled": False, "fresh_target": True,
                            "verification": verification}
        return attempt
    verification = _verify_target_against_b(paths, transition, evidence, plan)
    attempt = PlanAttempt(paths=paths, transition=transition, plan=plan, owner=owner, acquired_here=acquired_here,
                          backup=evidence["backup"], baseline_a={"digest": evidence["baseline_a"]},
                          attempt_directory=evidence["directory"], attempt_id=evidence["attempt_id"],
                          started_at=evidence["metadata"].get("started_at"), attempt_metadata=evidence["metadata"])
    if evidence["completion"] is not None:
        attempt.finished = _completed_retry_report(attempt, evidence["completion"], verification)
        return attempt
    result = _noop_report(attempt, completion=None)
    result.update({"reconciled": True, "already_migrated": True, "applied": False,
                   "backup": evidence["backup"], "baseline_a": evidence["baseline_a"],
                   "baseline_b": evidence["baseline_b"], "preparation": evidence["preparation"],
                   "verification": verification, "fresh_target": False})
    receipt = _write_private_json(evidence["directory"], "completion.json", {
        **result, "reconciled_after_commit": True,
        "note": "The exact target commit was proven from durable baseline B, the verified A-to-B reconciliation, the "
                "private database backup and the restored-file bundle. No DDL, receipt replay or new backup was performed.",
        "completed_at": utc_now()})
    result["receipt"] = str(receipt)
    attempt.finished = result
    return attempt


def _noop_report(attempt: PlanAttempt, *, completion) -> dict:
    return {"format": PLAN_FORMAT, "transition": TRANSITION_ID, "instance_root": str(attempt.paths.instance_root),
            "database": str(_database_path(attempt.paths)),
            "selected_extensions": list(attempt.transition.extensions.ids),
            "predecessor": _identity(attempt.transition.predecessor),
            "target": _identity(attempt.transition.target), "plan_digest": attempt.plan["plan_digest"],
            "applied": False, "already_migrated": True, "reconciled": False,
            "backup": None if completion is None else completion.get("backup"),
            "baseline_a": attempt.baseline_a.get("digest"),
            "baseline_b": None if completion is None else completion.get("baseline_b"),
            "preparation": None if completion is None else completion.get("preparation"),
            "verification": {"target": "exact", "identity": _identity(attempt.transition.target),
                             "integrity": "ok", "foreign_keys": "ok"},
            "receipt": None if completion is None else str(completion)}


def _completed_retry_report(attempt: PlanAttempt, completion: dict, verification: dict) -> dict:
    """A completed retry preserves the original backup/A/B/accounting references and re-verifies the current target."""
    report = {key: completion.get(key) for key in ("format", "transition", "instance_root", "database",
                                                   "selected_extensions", "predecessor", "target", "plan_digest",
                                                   "applied", "already_migrated", "reconciled", "backup", "baseline_a",
                                                   "baseline_b", "preparation")}
    report.update({"applied": False, "already_migrated": True, "reconciled": False, "verification": verification,
                   "receipt": str(attempt.attempt_directory / "completion.json") if attempt.attempt_directory else None})
    return report


def _read_evidence_json(path: Path, *, max_bytes=MAX_MANIFEST_INDEX_BYTES, label="migration evidence") -> dict:
    try:
        require_private_directory(Path(path).parent)
        raw = read_private_file(path, max_bytes=max_bytes)
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (DomainError, OSError, ValueError, TypeError) as exc:
        raise DomainError("migration_evidence_invalid", f"The {label} is missing or unreadable",
                          details={"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise DomainError("migration_evidence_invalid", f"The {label} is not a JSON object")
    return value


def _load_attempt_evidence(paths, plan_digest):
    """Load the newest complete attempt evidence strictly; an incomplete prior attempt is never treated as fresh."""
    base = _receipt_directory(paths) / plan_digest
    if not base.exists():
        return None
    if not base.is_dir() or os.path.islink(base):
        raise DomainError("migration_evidence_invalid", "The transition receipt location is not a private directory")
    entries = sorted(base.iterdir())
    attempt_directories = [entry for entry in entries if entry.is_dir() and not entry.is_symlink()]
    if not attempt_directories:
        raise DomainError("migration_evidence_invalid", "An exact target has incomplete or missing attempt evidence",
                          details={"entries": [entry.name for entry in entries][:MAX_REPORTED_REFERENCES]})
    records = []
    for directory in attempt_directories:
        metadata = _read_evidence_json(directory / "attempt.json", max_bytes=MAX_PREPARATION_BYTES,
                                       label="attempt receipt")
        if (metadata.get("format") != PLAN_FORMAT or metadata.get("transition") != TRANSITION_ID
                or metadata.get("state") != "attempt" or metadata.get("plan_digest") != plan_digest
                or metadata.get("attempt_id") != directory.name
                or not isinstance(metadata.get("started_at"), str)):
            raise DomainError("migration_evidence_invalid", "An attempt receipt does not bind this plan and attempt")
        records.append((metadata["started_at"], directory, metadata))
    _started, directory, metadata = sorted(records, key=lambda item: (item[0], item[1].name))[-1]
    attempt_id = directory.name
    restore_reference = metadata.get("restore")
    if (not isinstance(restore_reference, dict) or not isinstance(restore_reference.get("digest"), str)
            or not isinstance(restore_reference.get("path"), str)
            or not isinstance(restore_reference.get("files"), int)):
        raise DomainError("migration_evidence_invalid", "The attempt receipt has no valid restore bundle reference")
    index_a = verify_manifest_bundle(directory, "baseline-a", expected_digest=metadata.get("baseline_a"))
    restore = verify_file_bundle(directory, {}, {"files": index_a["files"]}, expected_digest=restore_reference["digest"])
    marker = _read_evidence_json(directory / "precommit.json", label="pre-commit marker")
    if (marker.get("format") != MANIFEST_FORMAT or marker.get("state") != "precommit"
            or marker.get("plan_digest") != plan_digest or marker.get("attempt_id") != attempt_id
            or marker.get("instance_root") != metadata.get("instance_root")
            or marker.get("database") != metadata.get("database")
            or marker.get("selected_extensions") != metadata.get("selected_extensions")
            or marker.get("predecessor") != metadata.get("predecessor")
            or marker.get("target") != metadata.get("target")
            or marker.get("baseline_a") != metadata.get("baseline_a")
            or not isinstance(marker.get("baseline_b"), str) or not HASH.fullmatch(marker["baseline_b"])
            or (marker.get("backup") or {}).get("sha256") != (metadata.get("backup") or {}).get("sha256")
            or marker.get("restore") != restore_reference
            or marker.get("reconciliation") is None):
        raise DomainError("migration_evidence_invalid", "The pre-commit marker does not bind this attempt")
    index_b = verify_manifest_bundle(directory, "baseline-b", expected_digest=marker.get("baseline_b"))
    if marker.get("baseline_b_bundle", {}).get("digest") != index_b["digest"]:
        raise DomainError("migration_evidence_invalid", "The pre-commit marker does not bind the B bundle")
    reconciliation = _read_evidence_json(directory / "reconciliation.json", label="reconciliation receipt")
    body = {key: value for key, value in reconciliation.items() if key != "digest"}
    if (reconciliation.get("digest") != canonical_hash(body)
            or reconciliation.get("format") != MANIFEST_FORMAT or reconciliation.get("plan_digest") != plan_digest
            or reconciliation.get("attempt_id") != attempt_id
            or reconciliation.get("baseline_a") != index_a["digest"]
            or reconciliation.get("baseline_b") != index_b["digest"]
            or marker.get("reconciliation") != reconciliation.get("digest")):
        raise DomainError("migration_evidence_invalid", "The durable reconciliation does not bind baseline A and B")
    accounting = reconciliation.get("accounting")
    if (not isinstance(accounting, dict) or accounting.get("plan_digest") != plan_digest
            or accounting.get("digest") != canonical_hash({key: value for key, value in accounting.items()
                                                           if key != "digest"})):
        raise DomainError("migration_evidence_invalid", "The durable A-to-B accounting is invalid")
    backup = metadata.get("backup")
    if not isinstance(backup, dict) or marker.get("backup") != backup:
        raise DomainError("migration_evidence_invalid", "The attempt backup reference is invalid")
    backup_path = Path(backup.get("path", ""))
    _require_store_file(backup_path)
    with backup_path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != backup.get("sha256"):
            raise DomainError("migration_evidence_invalid", "The private backup changed after the transition")
    completion = None
    completion_path = directory / "completion.json"
    if completion_path.exists():
        completion = _read_evidence_json(completion_path, label="completion receipt")
        if (completion.get("plan_digest") != plan_digest
                or completion.get("baseline_a") != index_a["digest"]
                or completion.get("baseline_b") != index_b["digest"]
                or (completion.get("backup") or {}).get("sha256") != backup.get("sha256")
                or (completion.get("preparation") or {}).get("digest") != accounting.get("digest")
                or (completion.get("verification") or {}).get("retained_rows") not in {"exact", "not_available_for_noop"}):
            raise DomainError("migration_evidence_invalid", "The completion receipt does not bind this attempt evidence")
    return {"directory": directory, "attempt_id": attempt_id, "metadata": metadata, "index_a": index_a,
            "index_b": index_b, "marker": marker, "reconciliation": reconciliation,
            "accounting": accounting, "backup": backup, "baseline_a": index_a["digest"],
            "baseline_b": index_b["digest"], "preparation": accounting, "completion": completion}


def _verify_target_against_b(paths, transition: _Transition, evidence: dict, plan: dict) -> dict:
    """Inspect the exact target, then prove retained rows/files/identity against durable B and A/backup evidence."""
    database = _database_path(paths)
    _require_store_file(database)
    metadata = evidence["metadata"]
    marker = evidence["marker"]
    if (metadata.get("instance_root") != str(paths.instance_root) or metadata.get("database") != str(database)
            or metadata.get("selected_extensions") != list(transition.extensions.ids)
            or metadata.get("predecessor") != _identity(transition.predecessor)
            or metadata.get("target") != _identity(transition.target)):
        raise DomainError("migration_evidence_invalid", "The durable attempt evidence belongs to another instance, "
                          "extension selection or composition")
    if (marker.get("instance_root") != str(paths.instance_root) or marker.get("database") != str(database)
            or marker.get("selected_extensions") != list(transition.extensions.ids)
            or marker.get("predecessor") != _identity(transition.predecessor)
            or marker.get("target") != _identity(transition.target)):
        raise DomainError("migration_evidence_invalid", "The pre-commit marker belongs to another instance, extension "
                          "selection or composition")
    summary_a, summary_b = manifest_summary(evidence["index_a"]), manifest_summary(evidence["index_b"])
    file_changes = len({path for path in set(summary_a["files"]) | set(summary_b["files"])
                        if summary_a["files"].get(path) != summary_b["files"].get(path)})
    table_changes = sum(1 for table in set(summary_a["tables"]) | set(summary_b["tables"])
                        if summary_a["tables"].get(table, {}).get("digest") !=
                        summary_b["tables"].get(table, {}).get("digest"))
    accounting = evidence["accounting"]
    planned_steps = {(step["kind"], step["identity"], step["digest"]) for step in plan["preparation"]["steps"]}
    recorded_steps = {(step.get("kind"), step.get("identity"), step.get("plan_digest"))
                      for step in accounting.get("steps", []) if isinstance(step, dict)}
    if (recorded_steps != planned_steps or accounting.get("files_changed") != file_changes
            or (accounting.get("rows_changed") == 0) != (table_changes == 0)):
        raise DomainError("migration_evidence_invalid", "The durable accounting does not match the plan and the exact "
                          "A-to-B differences")
    connection = _connect(database, "ro")
    try:
        connection.execute("PRAGMA query_only = ON")
        state, _identity_value, application_id, user_version, problems = _recognise(connection, transition)
        if state != "target":
            raise DomainError("migration_evidence_invalid", "Prior evidence is not an exact committed target",
                              details={"state": state, "problems": problems})
        _require_integrity(connection)
        _require_preserved(connection, transition, manifest_summary(evidence["index_b"]))
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DomainError("migration_evidence_invalid", "Foreign keys are not enabled during recovery verification")
        if _identity_values(connection) != evidence["index_b"]["identity"]:
            raise DomainError("migration_evidence_invalid", "The target identity changed after the commit")
    finally:
        connection.close()
    current_files = _file_manifest(plan["preparation"])
    if current_files != evidence["index_b"]["files"]:
        raise DomainError("migration_evidence_invalid", "A reviewed preparation file changed after the commit")
    _verify_backup_against_a(paths, transition, evidence["backup"], manifest_summary(evidence["index_a"]))
    return {"target": "exact", "identity": _identity(transition.target), "application_id": APPLICATION_ID,
            "user_version": transition.target.target.version, "integrity": "ok", "foreign_keys": "ok",
            "retained_rows": "exact", "baseline_b": evidence["baseline_b"]}


def _refuse_prior_attempt_evidence(paths, plan_digest) -> None:
    directory = _receipt_directory(paths) / plan_digest
    if directory.exists() and any(directory.iterdir()):
        raise DomainError("migration_evidence_conflict", "A prior attempt for this plan already has durable evidence; "
                          "inspect and reconcile it instead of taking a second backup or repeating the transition",
                          details={"plan_digest": plan_digest})


def _verify_attempt_evidence_files(attempt: PlanAttempt) -> None:
    """Recheck the durable A bundle and restore bundle before the destructive phase."""
    if attempt.attempt_directory is None:
        return
    metadata = attempt.attempt_metadata or {}
    verify_manifest_bundle(attempt.attempt_directory, "baseline-a", expected_digest=attempt.baseline_a["digest"])
    restore = metadata.get("restore")
    if not isinstance(restore, dict) or not isinstance(restore.get("digest"), str):
        raise DomainError("migration_evidence_invalid", "The attempt has no durable restore bundle reference")
    verify_file_bundle(attempt.attempt_directory, attempt.plan["preparation"], attempt.baseline_a,
                       expected_digest=restore["digest"])
    if (metadata.get("baseline_a") != attempt.baseline_a["digest"]
            or (metadata.get("backup") or {}).get("sha256") != (attempt.backup or {}).get("sha256")):
        raise DomainError("migration_evidence_invalid", "The attempt evidence does not bind baseline A and its backup")


def _verify_backup_against_a(paths, transition: _Transition, backup: dict | None, baseline_a: dict) -> None:
    """The private SQLite backup must still be byte-identical and row-identical to baseline A before DDL."""
    if not isinstance(backup, dict) or not isinstance(backup.get("path"), str):
        raise DomainError("migration_evidence_invalid", "The attempt has no verified backup reference")
    path = Path(backup["path"])
    _require_store_file(path)
    info = os.lstat(path)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DomainError("migration_evidence_invalid", "The backup is not a private file owned by this user")
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != backup.get("sha256"):
            raise DomainError("migration_evidence_invalid", "The backup bytes changed after baseline A")
    connection = _connect(path, "ro")
    try:
        connection.execute("PRAGMA query_only = ON")
        state, identity, _application, _version, problems = _recognise(connection, transition)
        if state != "predecessor":
            raise DomainError("migration_evidence_invalid", "The backup is not the exact predecessor",
                              details={"state": state, "problems": problems})
        _require_integrity(connection)
        for table in sorted(baseline_a["tables"]):
            current = table_manifest(connection, table, assembly=transition.predecessor)
            if current["digest"] != baseline_a["tables"][table]["digest"]:
                raise DomainError("migration_evidence_invalid", "The backup does not match baseline A",
                                  details={"table": table})
        if _identity_values(connection) != baseline_a["identity"]:
            raise DomainError("migration_evidence_invalid", "The backup target identity does not match baseline A")
    finally:
        connection.close()


def _empty_accounting(plan: dict) -> dict:
    body = {"format": 1, "transition": TRANSITION_ID, "plan_digest": plan["plan_digest"], "steps": [],
            "rows_changed": 0, "files_changed": 0}
    return {**body, "digest": canonical_hash(body)}


def _publish_precommit_evidence(attempt: PlanAttempt, baseline_b: dict, reconciliation: dict | None) -> dict:
    """Durably record exact B, the verified A-to-B accounting and a pre-commit marker before any DROP is attempted."""
    if attempt.attempt_directory is None:
        raise DomainError("migration_evidence_invalid", "The transition has no durable attempt directory")
    manifest_b = write_manifest_bundle(attempt.attempt_directory, "baseline-b", baseline_b)
    accounting = reconciliation if reconciliation is not None else _empty_accounting(attempt.plan)
    reconciliation_body = {"format": MANIFEST_FORMAT, "plan_digest": attempt.plan["plan_digest"],
                           "attempt_id": attempt.attempt_id, "baseline_a": attempt.baseline_a["digest"],
                           "baseline_b": baseline_b["digest"], "accounting": accounting}
    reconciliation_body["digest"] = canonical_hash(reconciliation_body)
    _write_private_json(attempt.attempt_directory, "reconciliation.json", reconciliation_body)
    marker = {"format": MANIFEST_FORMAT, "transition": TRANSITION_ID, "state": "precommit",
              "plan_digest": attempt.plan["plan_digest"], "attempt_id": attempt.attempt_id,
              "instance_root": str(attempt.paths.instance_root), "database": str(_database_path(attempt.paths)),
              "selected_extensions": list(attempt.transition.extensions.ids),
              "predecessor": _identity(attempt.transition.predecessor),
              "target": _identity(attempt.transition.target),
              "backup": attempt.backup, "baseline_a": attempt.baseline_a["digest"],
              "baseline_b": baseline_b["digest"], "baseline_b_bundle": manifest_b,
              "restore": (attempt.attempt_metadata or {}).get("restore"),
              "reconciliation": reconciliation_body["digest"], "recorded_at": utc_now()}
    _write_private_json(attempt.attempt_directory, "precommit.json", marker)
    return marker


def _reconcile(plan, baseline_a, baseline_b, preparation, *, connection=None, plan_attempt=None):
    if preparation is None:
        if plan["preparation"]["steps"]:
            raise DomainError("invalid_configuration", "The plan has authorized preparation steps but no preparation "
                              "receipt was supplied")
        if manifest_difference(baseline_a, baseline_b) or baseline_a["files"] != baseline_b["files"]:
            raise DomainError("release_blocked", "The database or planned files changed between baseline A and baseline "
                              "B without an authorized preparation receipt")
        return None
    return validate_preparation_receipt(preparation, plan=plan, baseline_a=baseline_a, baseline_b=baseline_b,
                                        connection=connection, plan_attempt=plan_attempt)


def _drop_table(connection, table):
    if table not in REMOVED_TABLES:
        raise DomainError("invalid_configuration", "The transition may drop only its frozen engine tables")
    connection.execute(f'DROP TABLE "{table}"')


# -- in-transaction and post-commit verification -----------------------------------------------------------------

def _update_identity(connection, target: Assembly) -> None:
    before = connection.execute(f'SELECT id, name, version, created_at FROM "{IDENTITY_TABLE}"').fetchall()
    if len(before) != 1 or before[0][0] != "target" or (before[0][1], before[0][2]) != (target.target.name,
                                                                                        target.target.version):
        raise DomainError("unsupported_schema", "The stored target identity cannot be updated by this transition")
    changed = connection.execute(f'UPDATE "{IDENTITY_TABLE}" SET digest = ? WHERE id = \'target\'',
                                 (target.target.digest,))
    if changed.rowcount != 1:
        raise DomainError("unsupported_schema", "The stored target identity was not updated exactly once")
    after = connection.execute(f'SELECT id, name, version, created_at FROM "{IDENTITY_TABLE}"').fetchall()
    if [tuple(row) for row in after] != [tuple(row) for row in before]:
        raise DomainError("unsupported_schema", "The transition changed more than the target identity digest")


def _require_preserved(connection, transition: _Transition, baseline_b: dict) -> None:
    """Every retained table's exact rows persist from B; only the target identity digest may have changed."""
    for table in sorted(_tables(connection)):
        if table.startswith("sqlite_") or table in REMOVED_TABLES or table == IDENTITY_TABLE:
            continue
        before = baseline_b["tables"].get(table)
        if before is None:
            raise DomainError("unsupported_schema", "A table appeared after baseline B", details={"table": table})
        current = table_manifest(connection, table, assembly=transition.target)
        if current["digest"] != before["digest"]:
            old_rows = before.get("rows", {})
            changed = sorted(key for key in set(old_rows) | set(current["rows"])
                             if old_rows.get(key) != current["rows"].get(key))
            raise DomainError("unsupported_schema", "The transition changed retained rows", details={
                "table": table, "rows": changed[:MAX_REPORTED_REFERENCES], "count": len(changed)})
    identity = connection.execute(f'SELECT id, name, version, digest, created_at FROM "{IDENTITY_TABLE}"').fetchall()
    if len(identity) != 1:
        raise DomainError("unsupported_schema", "The target identity row is not unique")
    row = identity[0]
    previous = baseline_b.get("identity")
    if previous is None:
        raise DomainError("unsupported_schema", "Baseline B has no target identity")
    if ((row[0], row[1], row[2], row[4]) != (previous["id"], previous["name"], previous["version"],
                                             previous["created_at"])
            or row[3] != transition.target.target.digest):
        raise DomainError("unsupported_schema", "The transition changed more than the target identity digest")


def _verify_target(paths, transition: _Transition, baseline_b) -> dict:
    database = _database_path(paths)
    _require_store_file(database)
    connection = _connect(database, "ro")
    try:
        connection.execute("PRAGMA query_only = ON")
        state, _identity_value, application_id, user_version, problems = _recognise(connection, transition)
        if state != "target":
            raise DomainError("migration_verification_failed", "The transition did not produce the exact target store",
                              details={"state": state, "problems": problems})
        _require_integrity(connection)
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DomainError("migration_verification_failed", "Foreign-key enforcement is not enabled")
        if baseline_b is not None:
            _require_preserved(connection, transition, baseline_b)
        return {"target": "exact", "identity": _identity(transition.target), "application_id": application_id,
                "user_version": user_version, "integrity": "ok", "foreign_keys": "ok",
                "retained_rows": "exact" if baseline_b is not None else "not_available_for_noop"}
    finally:
        connection.close()


def _require_gates(connection, paths, transition, plan, extensions) -> None:
    gates = _gate_rows(connection, transition, extensions, paths=paths,
                       target=_target_for(paths, transition, extensions, plan["preparation"]))
    summary = _gate_summary(gates)
    if summary != plan["gates"]:
        raise DomainError("operation_conflict", "The store's data/activity gate state changed after preparation; "
                          "prepare the transition again", details={"prepared": plan["gates"], "observed": summary})
    _require_ready(gates)
    if _native_catalogs(paths, transition, extensions, connection=connection)["paths"] and not any(
            step["kind"] == "native_preparation" for step in plan["preparation"]["steps"]):
        raise DomainError("release_blocked", "The prepared native catalogs changed after preparation")


def _require_policy_clear(profiles) -> None:
    if profiles:
        raise DomainError("release_blocked", "Active profiles still carry retired operation names or an unverified "
                          "role pin at baseline B; the authorized adoption is not complete",
                          details={"profiles": profiles[:MAX_REPORTED_REFERENCES], "total": len(profiles)})


def _require_ready(gates) -> None:
    summary = _gate_summary(gates)
    if summary["ready"]:
        return
    blocked = {key: value for key, value in summary.items() if key != "ready" and value}
    raise DomainError("release_blocked", "The store is not ready for the learning-table removal transition",
                      details={"blocked": blocked, "references": gates["reference_details"],
                               "total_blocked_kinds": len(blocked)})


def _require_removed_empty(connection) -> None:
    counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in REMOVED_TABLES}
    nonempty = {table: count for table, count in counts.items() if count}
    if nonempty:
        references = {}
        for table in nonempty:
            try:
                rows = connection.execute(f'SELECT rowid FROM "{table}" ORDER BY rowid LIMIT ?',
                                          (MAX_REPORTED_REFERENCES,)).fetchall()
                references[table] = [row[0] for row in rows]
            except sqlite3.Error:
                references[table] = []
        raise DomainError("release_blocked", "An engine table is not empty; this transition never deletes records",
                          details={"tables": nonempty, "references": references})


def _require_store(connection, transition: _Transition, expected: str) -> None:
    state, identity, application_id, user_version, problems = _recognise(connection, transition)
    if state != expected:
        raise DomainError("unsupported_schema", f"The store is not exactly the recognised {expected} of this transition",
                          details={"state": state, "identity": identity, "problems": problems})
    if application_id != APPLICATION_ID or user_version != transition.target.target.version:
        raise DomainError("unsupported_schema", "The store is not this Operations target schema version",
                          details={"application_id": application_id, "user_version": user_version})


def _require_integrity(connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise DomainError("invalid_database", "The existing Operations database failed its integrity check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DomainError("invalid_database", "The existing Operations database contains invalid references")


# -- plan and receipt files --------------------------------------------------------------------------------------

def _validated_plan(paths, plan, transition: _Transition, *, exact_inputs=True) -> dict:
    """Verify the saved plan's shape, digest and frozen identities; optionally re-hash every preparation input."""
    if not isinstance(plan, dict):
        raise DomainError("invalid_configuration", "The saved migration plan is missing")
    body = dict(plan)
    digest = body.pop("plan_digest", None)
    if set(body) != PLAN_KEYS - {"plan_digest"}:
        raise DomainError("invalid_configuration", "The saved migration plan has an unexpected shape")
    if type(body["format"]) is not int or body["format"] != PLAN_FORMAT or body["transition"] != TRANSITION_ID:
        raise DomainError("invalid_configuration", "The saved migration plan is not this transition")
    if not isinstance(digest, str) or canonical_hash(body) != digest:
        raise DomainError("operation_conflict", "The saved migration plan was changed or corrupted after preparation")
    static = _static_report(paths, transition)
    if (body["instance_root"] != static["instance_root"] or body["database"] != static["database"]
            or body["selected_extensions"] != static["selected_extensions"]
            or body["predecessor"] != static["predecessor"] or body["target"] != static["target"]
            or body["removal"] != static["removal"] or body["schema"] != static["schema"]):
        raise DomainError("operation_conflict", "The saved migration plan does not describe this instance, extension "
                          "selection and compiled composition; prepare it again", details={
                              "plan": {key: body[key] for key in ("instance_root", "database", "selected_extensions")},
                              "expected": {key: static[key] for key in ("instance_root", "database",
                                                                        "selected_extensions")}})
    if body["preparation"] != _normalise_preparation(body["preparation"]):
        raise DomainError("invalid_configuration", "The saved preparation references are not in their canonical form")
    if not exact_inputs:
        return plan
    _require_preparation_plan_files(paths, body["preparation"], transition)
    expected = {**_plan_body(paths, transition, body["preparation"]),
                "catalogs": _native_catalogs(paths, transition, transition.extensions)}
    if {key: body[key] for key in set(expected) - {"gates"}} != {key: expected[key] for key in set(expected) - {"gates"}}:
        raise DomainError("operation_conflict", "One or more preparation inputs changed after the plan was prepared; "
                          "prepare the transition again")
    return plan


def _read_plan(path) -> dict:
    path = Path(path)
    if not path.is_absolute():
        raise DomainError("invalid_configuration", "The saved migration plan needs an explicit absolute path")
    raw = read_private_file(path, max_bytes=MAX_PLAN_BYTES)
    try:
        plan = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", "The saved migration plan is not valid JSON") from exc
    if not isinstance(plan, dict):
        raise DomainError("invalid_configuration", "The saved migration plan is not a JSON object")
    return plan


def read_preparation_receipt(path) -> dict:
    """Read a coordinator's concrete preparation receipt from its explicit private file."""
    path = Path(path)
    if not path.is_absolute():
        raise DomainError("invalid_configuration", "The preparation receipt needs an explicit absolute path")
    raw = read_private_file(path, max_bytes=MAX_PREPARATION_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", "The preparation receipt is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DomainError("invalid_configuration", "The preparation receipt is not a JSON object")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


def _save_plan(plan, path: Path) -> None:
    path = Path(path)
    if not path.is_absolute():
        raise DomainError("invalid_configuration", "The plan destination needs an explicit absolute path")
    write_private_file(path.parent, path.name, (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# -- instance, ownership and database handling -------------------------------------------------------------------

def _require_paths(paths) -> None:
    if not isinstance(paths, InstancePaths):
        raise DomainError("invalid_configuration", "The migration needs the explicit instance paths")


def _database_path(paths: InstancePaths) -> Path:
    _require_paths(paths)
    try:
        return paths.database
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", f"The Operations database path is unsafe: {exc}") from exc


def _lifetime_path(paths: InstancePaths) -> Path:
    try:
        return paths.runtime / LOCK_NAME
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", f"The instance lifetime path is unsafe: {exc}") from exc


def _caller_ownership(paths: InstancePaths, ownership) -> InstanceOwnership:
    if not isinstance(ownership, InstanceOwnership):
        raise DomainError("invalid_configuration", "The supplied ownership is not the instance lifetime owner")
    if not ownership.held:
        raise DomainError("ownership_lost", "The supplied instance ownership is not held")
    try:
        supplied = ownership.path
    except InstancePathError as exc:
        raise DomainError("ownership_lost", "The supplied instance ownership path is unsafe") from exc
    if supplied != _lifetime_path(paths):
        raise DomainError("invalid_configuration", "The supplied instance ownership belongs to another instance lifetime")
    ownership.verify()
    return ownership


def _verify_ownership(ownership: InstanceOwnership) -> None:
    ownership.verify()


def _require_store_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise DomainError("invalid_database", "The existing Operations database is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DomainError("invalid_database", "The existing Operations database is not a plain regular file")
    if info.st_uid != os.geteuid():
        raise DomainError("invalid_configuration", "The existing Operations database is not owned by this user")


def _readonly_uri(database: Path) -> str:
    """No missing WAL cache file is ever created and no WAL content is ignored; an incomplete pair is refused."""
    wal, shm = Path(str(database) + "-wal"), Path(str(database) + "-shm")
    wal_exists, shm_exists = os.path.lexists(wal), os.path.lexists(shm)
    if wal_exists != shm_exists:
        raise DomainError("invalid_database", "The Operations store has an incomplete SQLite WAL/shm sidecar pair; a "
                          "safe offline inspection needs a fully checkpointed store (main file only) or a complete "
                          "-wal and -shm pair. This command never checkpoints, deletes, copies or ignores WAL content",
                          details={"wal": wal_exists, "shm": shm_exists})
    if not wal_exists:
        return Path(database).resolve().as_uri() + "?mode=ro&immutable=1"
    for sidecar in (wal, shm):
        try:
            info = os.lstat(sidecar)
        except OSError as exc:
            raise DomainError("invalid_database", "A SQLite sidecar of the Operations store is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise DomainError("invalid_database", "A SQLite sidecar of the Operations store is not a plain file owned "
                              "by this user")
    return Path(database).resolve().as_uri() + "?mode=ro"


def _connect(database: Path, mode: str):
    if mode not in ("ro", "rw"):
        raise ValueError("Unsupported database mode")
    uri = _readonly_uri(database) if mode == "ro" else Path(database).resolve().as_uri() + "?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        raise DomainError("invalid_database", "The existing Operations database cannot be opened") from exc
    install_sql_functions(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _absolute_path(value, what: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise DomainError("invalid_configuration", f"{what} must be a path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise DomainError("invalid_configuration", f"{what} needs an explicit absolute path without traversal")
    return Path(os.path.abspath(path))


def _file_hash(path: Path) -> dict:
    return _file_state(path)


def _receipt_directory(paths: InstancePaths) -> Path:
    try:
        return paths.runtime / RECEIPT_DIRECTORY
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", "The transition receipt location is unsafe") from exc


def _require_private_directory(path: Path, *, create=False) -> Path:
    try:
        return require_private_directory(path, create=create)
    except DomainError as exc:
        if create:
            raise DomainError("invalid_configuration", "The transition receipt directory is unsafe") from exc
        raise


def _write_private_json(directory: Path, name: str, value) -> Path:
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return write_private_file(directory, name, data)


# -- verified private backup --------------------------------------------------------------------------------------

def _publish_backup(connection, destination, transition: _Transition) -> dict:
    destination = Path(destination)
    if not destination.is_absolute():
        raise DomainError("invalid_configuration", "The backup destination needs an explicit absolute path")
    if ".." in destination.parts or "\0" in str(destination):
        raise DomainError("invalid_configuration", "The backup destination contains an invalid path component")
    destination = Path(os.path.abspath(destination))
    require_private_directory(destination.parent)
    if os.path.lexists(destination):
        raise DomainError("backup_exists", "The backup destination already exists; a backup is never replaced")
    descriptor, name = tempfile.mkstemp(prefix=BACKUP_PREFIX, suffix=".sqlite3", dir=str(destination.parent))
    os.close(descriptor)
    temporary = Path(name)
    started = time.monotonic()

    def deadline(_status, _remaining, _total):
        if time.monotonic() - started > BACKUP_DEADLINE_SECONDS:
            raise DomainError("busy", "The verified backup exceeded its bounded deadline", retryable=True)

    try:
        try:
            target = sqlite3.connect(str(temporary))
            try:
                connection.backup(target, pages=128, progress=deadline, sleep=0.05)
                if target.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                    raise DomainError("invalid_backup", "The backup copy did not become a self-contained store")
            finally:
                target.close()
        except sqlite3.Error as exc:
            raise DomainError("invalid_backup", "The verified backup could not be written") from exc
        Database(temporary, assembly=transition.predecessor).validate_snapshot(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        written = os.lstat(temporary)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise DomainError("backup_exists", "The backup destination already exists; a backup is never replaced") from exc
        fsync_directory(destination.parent)
        published = os.lstat(destination)
        if (not stat.S_ISREG(published.st_mode) or published.st_uid != os.geteuid() or published.st_mode & 0o077
                or (published.st_dev, published.st_ino) != (written.st_dev, written.st_ino)):
            raise DomainError("invalid_backup", "The published backup is not the private file this transition wrote")
        return {"path": str(destination), "sha256": digest}
    finally:
        for leftover in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
            try:
                leftover.unlink()
            except OSError:
                pass


# -- command line ------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("inspect", "read-only recognition and gate counts of the installed Operations store"),
                            ("prepare", "read-only deterministic plan (print or --save)"),
                            ("apply", "apply a saved plan offline under the exclusive instance lifetime lock")):
        command = commands.add_parser(name, help=help_text)
        add_instance_arguments(command)
        command.add_argument("--extensions-config", type=Path, default=None,
                             help="Explicit extension selection in the trusted installation-config format")
        if name == "prepare":
            command.add_argument("--preparation-plan", type=Path, default=None,
                                 help="Reviewed preparation-plan references in private JSON")
            command.add_argument("--coordinator-request", type=Path, default=None,
                                 help="The coordinator inputs (private JSON); with --release-reference the preparation "
                                      "plan is derived read-only from the actual instance")
            command.add_argument("--release-reference", type=Path, default=None,
                                 help="The verified release reference (private JSON) for --coordinator-request")
            command.add_argument("--save", type=Path, default=None, help="New private 0600 file for the plan")
        if name == "apply":
            command.add_argument("--plan", type=Path, required=True, help="The saved plan file (0600 in a 0700 dir)")
            command.add_argument("--backup", type=Path, required=True,
                                 help="New backup file (explicit path in an existing owned 0700 directory)")
            command.add_argument("--preparation", type=Path, default=None,
                                 help="Optional concrete preparation receipt (A-to-B accounting)")
            command.add_argument("--coordinate", action="store_true",
                                 help="Run the reviewed preparation stages and the migration under one continuous "
                                      "instance owner (the plan must carry the coordinator inputs)")
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        extensions = _selected_extensions(options.extensions_config)
        if options.command == "inspect":
            result = inspect_migration(paths, extensions=extensions)
        elif options.command == "prepare":
            if (options.preparation_plan is None) == (options.coordinator_request is None):
                raise DomainError("invalid_configuration", "Prepare needs exactly one of --preparation-plan or "
                                  "--coordinator-request")
            if options.coordinator_request is not None:
                result = _coordinated_plan(paths, extensions, options)
            else:
                preparation = read_preparation_receipt(options.preparation_plan)
                result = prepare_migration(paths, extensions=extensions, preparation=preparation)
            if options.save is not None:
                _save_plan(result, options.save)
        elif options.coordinate:
            if options.preparation is not None:
                raise DomainError("invalid_configuration", "--coordinate produces its own preparation receipt")
            from qorqut.bootstrap import learning_removal_preparation as coordinator
            result = coordinator.run(paths, _read_plan(options.plan), backup=options.backup, extensions=extensions)
        else:
            preparation = read_preparation_receipt(options.preparation) if options.preparation is not None else None
            result = apply_migration(paths, _read_plan(options.plan), backup=options.backup,
                                     preparation=preparation, extensions=extensions)
        response = {"ok": True, "result": result}
        status = 0
    except DomainError as exc:
        response = {"ok": False, "error": exc.to_dict()}
        status = 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        response = {"ok": False, "error": {"code": "invalid_request", "message": str(exc), "retryable": False}}
        status = 1
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return status


def _coordinated_plan(paths, extensions, options):
    from qorqut.bootstrap import learning_removal_preparation as coordinator
    if options.release_reference is None:
        raise DomainError("invalid_configuration", "--coordinator-request needs --release-reference")
    request = coordinator.read_request(options.coordinator_request)
    try:
        raw = json.loads(read_private_file(options.release_reference, max_bytes=MAX_PREPARATION_BYTES),
                         object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", "The release reference is not valid JSON") from exc
    release = _normalise_preparation({"release": raw})["release"]
    return coordinator.plan_preparation(paths, request, release, extensions=extensions)


def _selected_extensions(path):
    if path is None:
        return NO_EXTENSIONS
    from qorqut.extensions import select
    from qorqut.installation import extensions_configuration
    return select(extensions_configuration(path)["extensions"])


if __name__ == "__main__":
    raise SystemExit(main())
