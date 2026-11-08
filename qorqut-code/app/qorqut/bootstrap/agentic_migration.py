"""Explicit offline Operations schema transition ``v1.3-agentic-overseer-1``.

The host composition has two additive storage owners the deployed v1.3/1073 store never had: ``agentic-inbox`` (the
transport cursor of the module change feed) and ``overseer`` (the bridge's private associations, signoffs and scheduled
occurrence records). Opening either composition requires the stored target identity, so the old store is refused and
this module is the one explicit, offline way to add exactly those two contributions. Nothing calls it on startup, on a
read, from chat or from a schedule, and ordinary ``Database.open_existing``, the service start and the fresh-instance
initializer keep their strict semantics.

The transition is compiled from the same trusted ``HostComposition`` the host opens: the predecessor is the current
contributions without the two named ``StorageContribution`` owners, the target is the full composition, with the same
explicitly selected extensions on both sides. Preparation proves that the target schema is a purely additive extension
of the predecessor schema (no removed or changed object, no blanket script) and freezes the transition id, the
canonical instance, the selected extension ids, both target identities, the added object names and one canonical plan
digest. Inspection and preparation are read-only (SQLite ``mode=ro``/``query_only``; a fully checkpointed store is
opened ``immutable`` so it creates no file, directory, binding or lock either). A store left with a complete
``-wal``/``-shm`` pair from an unclean stop is read through an ordinary read-only snapshot that includes the pending WAL
content and creates no missing artifact; an incomplete pair is refused before any connection exists, because this module
never checkpoints, copies, repairs or ignores WAL content, and a state is recognised only with the Operations
``application_id`` and target ``user_version`` as well as the identity and schema.

Apply takes the saved plan, owns the instance lifetime lock (or verifies the caller's already-held one for a
coordinated offline release), rechecks the plan and the exact actual predecessor, publishes a verified private SQLite
backup (SQLite's backup API, 0600 inside an owned 0700 directory, never replaced), then runs the two contributions'
individual trusted DDL statements and the one identity-digest update in a single ``BEGIN IMMEDIATE`` transaction. Any
exception before ``COMMIT`` rolls back every DDL and identity effect; a failed or interrupted attempt is reported,
never inferred or repaired. A retry against an exact target store is a validated no-op: no DDL, no second backup. The
predecessor is never rewritten, no row content is copied, and no agentic tree, profile, permission or runtime service
is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.storage.assembly import Assembly, StorageContribution
from qorqut_operations.storage.database import APPLICATION_ID, Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, CompiledTarget, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS, ExtensionSet
from qorqut.instance_paths import InstancePathError, InstancePaths
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import LOCK_NAME, InstanceOwnership
from qorqut.private_files import fsync_directory, read_private_file, require_private_directory, write_private_file

TRANSITION_ID = "v1.3-agentic-overseer-1"
PLAN_FORMAT = 1
# The two additive contributions this transition installs, named by their trusted owner identity. The predecessor is the
# host composition without exactly these, the target is the full composition; nothing else may differ.
EXCLUDED_OWNERS = ("agentic-inbox", "overseer")
PLAN_KEYS = frozenset({"format", "transition", "instance_root", "database", "selected_extensions", "predecessor",
                       "target", "additions", "plan_digest"})
MAX_PLAN_BYTES = 64 * 1024
BACKUP_PREFIX = ".agentic-overseer-backup-"
BACKUP_DEADLINE_SECONDS = 60.0
BUSY_TIMEOUT_SECONDS = 60.0
SQLITE_TYPES = ("table", "index", "trigger")


# -- trusted compositions ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Transition:
    """The compiled predecessor and target of one explicit extension selection, and the additive object delta."""

    extensions: ExtensionSet
    predecessor: Assembly
    target: Assembly
    contributions: tuple[StorageContribution, ...]
    additions: dict
    predecessor_objects: CompiledTarget
    target_objects: CompiledTarget
    name: str = TRANSITION_ID


def _identity(assembly: Assembly) -> dict:
    return {"name": assembly.target.name, "version": assembly.target.version, "digest": assembly.target.digest}


def _additions(before: CompiledTarget, after: CompiledTarget) -> dict:
    """The added object names, grouped by SQLite object type. Anything removed or changed is a refusal, never a repair."""
    removed = sorted(before.objects.keys() - after.objects.keys())
    changed = sorted(key for key in before.objects.keys() & after.objects.keys()
                     if before.objects[key] != after.objects[key])
    if removed or changed:
        raise DomainError("invalid_installation", "The selected host composition is not an additive extension of the "
                          "predecessor schema; this transition never drops, alters or rewrites a predecessor object",
                          details={"removed": [f"{kind} {name}" for kind, name in removed][:8],
                                   "changed": [f"{kind} {name}" for kind, name in changed][:8]})
    added = after.objects.keys() - before.objects.keys()
    unexpected = sorted({kind for kind, _name in added} - set(SQLITE_TYPES))
    if unexpected:
        raise DomainError("invalid_installation", "The selected host composition adds an unsupported schema object type",
                          details={"types": unexpected})
    return {"tables": [name for kind, name in sorted(added) if kind == "table"],
            "indexes": [name for kind, name in sorted(added) if kind == "index"],
            "triggers": [name for kind, name in sorted(added) if kind == "trigger"]}


def _transition_for(extensions=NO_EXTENSIONS) -> _Transition:
    """Compile the one named transition for this explicit extension selection. Trusted composition only: a caller can
    select installed extensions, never supply SQL, an assembly or another migration."""
    if not isinstance(extensions, ExtensionSet):
        raise DomainError("invalid_configuration", "The migration needs the explicit extension selection")
    feed = ChangeFeed()
    contributions = host_contributions(feed, extensions)
    composition = HostComposition()
    excluded = []
    for contribution in contributions:
        owner = getattr(contribution, "owner", None)
        if owner not in EXCLUDED_OWNERS:
            composition.add(contribution)
            continue
        if type(contribution) is not StorageContribution:
            raise DomainError("invalid_installation", f"The host contribution {owner} is not storage this transition adds")
        excluded.append(contribution)
    if [item.owner for item in excluded] != list(EXCLUDED_OWNERS):
        raise DomainError("invalid_installation", "The host composition no longer carries exactly the two named agentic "
                          "storage contributions: " + ", ".join(EXCLUDED_OWNERS))
    for contribution in excluded:
        for statement in contribution.schema:
            if not statement.lstrip().upper().startswith("CREATE "):
                raise DomainError("invalid_installation", "The additive contributions may only create schema objects")
    predecessor = composition.freeze()
    target = host_assembly(feed, extensions)
    before, after = compile_target(predecessor), compile_target(target)
    return _Transition(extensions=extensions, predecessor=predecessor, target=target, contributions=tuple(excluded),
                       additions=_additions(before, after), predecessor_objects=before, target_objects=after)


# -- read-only inspection and preparation ------------------------------------------------------------------------

def inspect_migration(paths: InstancePaths, *, extensions=NO_EXTENSIONS) -> dict:
    """Read-only recognition of the Operations store: missing, predecessor, target or unknown.

    Opens SQLite ``mode=ro`` with ``query_only``; when the store is fully checkpointed it is opened ``immutable``, so no
    file, directory, binding or lock is created. A complete ``-wal``/``-shm`` pair is read through an ordinary read-only
    snapshot (pending WAL included, no missing artifact); an incomplete pair is refused before connecting. The Operations
    pragmas, the identity table and the schema catalog are compared with the compiled compositions, so a wrong
    application id/userversion, a copied digest over a drifted or partial schema, or an extra object is reported as
    unknown. This never raises for a missing or unrecognised store: it reports it.
    """
    transition = _transition_for(extensions)
    report = _static_report(paths, transition)
    report.update(_observation(paths, transition))
    return report


def prepare_migration(paths: InstancePaths, *, extensions=NO_EXTENSIONS):
    """Read-only plan for this exact instance and selection; refuses anything that is not exactly recognised.

    The plan is deterministic and carries no record contents or credentials. Apply revalidates it against a freshly
    compiled composition before it acts, so a changed source tree, another instance or another extension selection is
    refused instead of guessed.
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
    body = _plan_body(paths, transition)
    return {**body, "plan_digest": canonical_hash(body)}


def _static_report(paths: InstancePaths, transition: _Transition) -> dict:
    _require_paths(paths)
    return {"format": PLAN_FORMAT, "transition": transition.name, "instance_root": str(paths.instance_root),
            "database": str(_database_path(paths)), "selected_extensions": list(transition.extensions.ids),
            "predecessor": _identity(transition.predecessor), "target": _identity(transition.target),
            "additions": transition.additions}


def _plan_body(paths: InstancePaths, transition: _Transition) -> dict:
    return _static_report(paths, transition)


def _observation(paths: InstancePaths, transition: _Transition) -> dict:
    """The one read-only store observation shared by inspect and prepare."""
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
    """Compare the Operations pragmas, the stored identity and the whole schema catalog with both compiled inventories.

    A state is recognised only with the Operations ``application_id``, the expected ``user_version``, the exact identity
    triple and the exact object catalog. A valid schema and identity with a wrong pragma is ``unknown``, so the read-only
    gate refuses it just like apply does; the diagnostics name the observed and expected values and never a definition.
    """
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


# -- apply -------------------------------------------------------------------------------------------------------

def apply_migration(paths: InstancePaths, plan, *, backup, extensions=NO_EXTENSIONS, ownership=None) -> dict:
    """Apply the saved plan to this exact instance while it is offline.

    The saved plan is validated first (identity, transition, canonical instance, extension selection, additive delta).
    The instance lifetime lock is taken here, or the caller's already-held one for one continuous coordinated offline
    lock is verified and never released. The exact actual predecessor is rechecked, a verified private backup is
    published, and the additive DDL plus the one identity-digest update commit as a single ``BEGIN IMMEDIATE`` unit.
    A recognised target store is a validated no-op: no DDL and no new backup.
    """
    transition = _transition_for(extensions)
    validated = _validated_plan(paths, plan, transition)
    return _apply_transition(paths, transition, validated, backup=backup, ownership=ownership)


def _apply_transition(paths, transition, validated, *, backup, ownership=None):
    acquired_here = ownership is None
    owner = InstanceOwnership(paths).acquire() if acquired_here else _caller_ownership(paths, ownership)
    try:
        _verify_ownership(owner)
        database = _database_path(paths)
        _require_store_file(database)
        connection = _connect(database, "rw")
        try:
            _verify_ownership(owner)
            state, identity, application_id, user_version, problems = _recognise(connection, transition)
            if application_id != APPLICATION_ID or user_version != transition.target.target.version:
                raise DomainError("unsupported_schema", "The existing store is not this Operations target schema version",
                                  details={"application_id": application_id, "user_version": user_version})
            _require_integrity(connection)
            if state == "target":
                return _report(paths, transition, validated, applied=False, already_migrated=True, backup=None)
            if state != "predecessor":
                raise DomainError("unsupported_schema", "The Operations store is not exactly the predecessor of this "
                                  "transition; refusing to repair or infer it", details={"state": state,
                                                                                         "identity": identity,
                                                                                         "problems": problems})
            _require_store(connection, transition, "predecessor")
            _verify_ownership(owner)
            preserved = _preserved_store_properties(connection)
            backup_report = _publish_backup(connection, backup, transition)
            _verify_ownership(owner)
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_ownership(owner)
                _require_store(connection, transition, "predecessor")
                _execute_additions(connection, transition.contributions)
                _update_identity(connection, transition.target)
                _require_store(connection, transition, "target")
                _require_references(connection)
                if _preserved_store_properties(connection) != preserved:
                    raise DomainError("unsupported_schema", "The transition changed an application id or user version")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except sqlite3.Error as exc:
            raise DomainError("storage_error", "The existing Operations database could not be read or migrated") from exc
        finally:
            connection.close()
        _verify_ownership(owner)
        # The ordinary strict target open is the last local proof; the transition is durable at this point, so a failure
        # here is reported as a committed store that does not validate, never silently retried or repaired.
        try:
            Database(database, assembly=transition.target).open_existing()
        except DomainError as exc:
            raise DomainError("migration_verification_failed", "The transition committed but the result does not open "
                              "through the target composition; report it, never restart the old host",
                              details={"backup": backup_report["path"]}) from exc
        return _report(paths, transition, validated, applied=True, already_migrated=False, backup=backup_report)
    finally:
        if acquired_here:
            owner.release()


def _report(paths, transition, plan, *, applied, already_migrated, backup) -> dict:
    return {"format": PLAN_FORMAT, "transition": transition.name, "instance_root": str(paths.instance_root),
            "database": str(_database_path(paths)), "selected_extensions": list(transition.extensions.ids),
            "predecessor": _identity(transition.predecessor), "target": _identity(transition.target),
            "additions": transition.additions, "plan_digest": plan["plan_digest"], "applied": applied,
            "already_migrated": already_migrated, "backup": backup}


def _validated_plan(paths, plan, transition: _Transition) -> dict:
    """Freeze the plan identity: exact keys, transition, canonical instance, selection and both compiled identities."""
    if not isinstance(plan, dict):
        raise DomainError("invalid_configuration", "The saved migration plan is missing")
    body = dict(plan)
    digest = body.pop("plan_digest", None)
    if set(body) != PLAN_KEYS - {"plan_digest"}:
        raise DomainError("invalid_configuration", "The saved migration plan has an unexpected shape")
    if type(body["format"]) is not int or body["format"] != PLAN_FORMAT or body["transition"] != transition.name:
        raise DomainError("invalid_configuration", "The saved migration plan is not this transition")
    if not isinstance(digest, str) or canonical_hash(body) != digest:
        raise DomainError("operation_conflict", "The saved migration plan was changed or corrupted after preparation")
    expected = _plan_body(paths, transition)
    if body != expected:
        raise DomainError("operation_conflict", "The saved migration plan does not describe this instance, extension "
                          "selection and compiled composition; prepare it again", details={
                              "plan": {key: body[key] for key in ("instance_root", "database", "selected_extensions")},
                              "expected": {key: expected[key] for key in ("instance_root", "database",
                                                                          "selected_extensions")}})
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


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


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
    """Accept a verified caller-owned instance lifetime lock for one continuous coordinated offline transition."""
    if not isinstance(ownership, InstanceOwnership):
        raise DomainError("invalid_configuration", "The supplied ownership is not the instance lifetime owner")
    if not ownership.held:
        raise DomainError("ownership_lost", "The supplied instance ownership is not held")
    try:
        supplied = ownership.path
    except InstancePathError as exc:
        raise DomainError("ownership_lost", "The supplied instance ownership path is unsafe") from exc
    if supplied != _lifetime_path(paths):
        raise DomainError("invalid_configuration", "The supplied instance ownership belongs to another instance "
                          "lifetime path")
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
    """The URI of a safe read-only inspection: no missing WAL cache file is ever created and no WAL content is ignored.

    SQLite opens a WAL database read-only together with its ``-wal``/``-shm`` pair. A fully checkpointed store has
    neither sidecar, so it is complete in its main file and is opened ``immutable``: no file, no lock, same catalog. A
    store left with a *complete* pair (both files, as an unclean stop leaves it) is read through an ordinary read-only
    connection; SQLite may use or update that existing shared-memory cache, but creates no missing artifact. An
    *incomplete* pair is refused before any connection exists: this command never checkpoints, deletes, copies, infers
    or ignores pending WAL content. A safe offline inspection therefore needs either the main file alone or the whole
    coherent pair.
    """
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
    """Open the existing store read-only or read/write; never create a missing store and never follow a link."""
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


def _preserved_store_properties(connection) -> tuple:
    return connection.execute("PRAGMA application_id").fetchone()[0], connection.execute("PRAGMA user_version").fetchone()[0]


def _require_integrity(connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise DomainError("invalid_database", "The existing Operations database failed its integrity check")
    _require_references(connection)


def _require_references(connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DomainError("invalid_database", "The existing Operations database contains invalid references")


def _require_store(connection, transition: _Transition, expected: str) -> None:
    """Prove the exact recognized state inside the transaction: identity triple and the whole object catalog."""
    state, identity, application_id, user_version, problems = _recognise(connection, transition)
    if state != expected:
        raise DomainError("unsupported_schema", f"The store is not exactly the recognised {expected} of this transition",
                          details={"state": state, "identity": identity, "problems": problems})
    if application_id != APPLICATION_ID or user_version != transition.target.target.version:
        raise DomainError("unsupported_schema", "The store is not this Operations target schema version",
                          details={"application_id": application_id, "user_version": user_version})


def _execute_additions(connection, contributions) -> None:
    """Each trusted additive contribution's own DDL statements, in registration order; never one blanket script."""
    for contribution in contributions:
        for statement in contribution.schema:
            connection.execute(statement)


def _update_identity(connection, target: Assembly) -> None:
    """Change only the target identity digest; name, version, created_at and the id stay byte for byte."""
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


# -- verified private backup --------------------------------------------------------------------------------------

def _publish_backup(connection, destination, transition: _Transition) -> dict:
    """A SQLite-API backup of the whole validated predecessor, published once at an explicit new destination.

    It is written beside the destination under a private temporary name (0600 in an owned 0700 directory), validated
    against the predecessor composition with the engine's snapshot check, fsynced and hashed, then hard-linked to the
    destination so an existing file or symlink is refused rather than replaced. A failure before publication removes
    only the temporary name; a published backup is preserved for recovery and is never deleted here.
    """
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
                # The backup is published as one self-contained file: SQLite's backup copies the WAL journal-mode
                # header, and a verified snapshot that needed sidecar files would not be a private backup of its own.
                if target.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                    raise DomainError("invalid_backup", "The backup copy did not become a self-contained store")
            finally:
                target.close()
        except sqlite3.Error as exc:
            raise DomainError("invalid_backup", "The verified backup could not be written") from exc
        # The engine's own snapshot validation: exact predecessor identity and schema, integrity and references.
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


# -- explicit optional-extension additions ------------------------------------------------------------------------

def extension_transition(previous, selected):
    """Compile a strictly additive selection using the same schema comparison and executor as the named migration."""
    if not isinstance(previous, ExtensionSet) or not isinstance(selected, ExtensionSet) or not set(previous.ids) < set(selected.ids):
        raise DomainError("invalid_installation", "Extension storage requires a strictly additive selection")
    predecessor = host_assembly(ChangeFeed(), previous)
    target = host_assembly(ChangeFeed(), selected)
    before, after = compile_target(predecessor), compile_target(target)
    old_owners = {item.owner for item in previous.storage()}
    contributions = tuple(item for item in selected.storage() if item.owner not in old_owners)
    if any(not statement.lstrip().upper().startswith("CREATE ") for item in contributions for statement in item.schema):
        raise DomainError("invalid_installation", "An extension install may only create schema objects")
    return _Transition(selected, predecessor, target, contributions, _additions(before, after), before, after,
                       name="v1.3-extension-addition-1")


def prepare_extension_storage(paths, *, previous, selected):
    transition = extension_transition(previous, selected)
    observed = _observation(paths, transition)
    if observed["state"] not in ("predecessor", "target"):
        raise DomainError("unsupported_schema", "The installed schema does not match this extension addition", details=observed)
    body = _plan_body(paths, transition)
    return {**body, "plan_digest": canonical_hash(body)}


def apply_extension_storage(paths, plan, *, previous, selected, backup, ownership):
    transition = extension_transition(previous, selected)
    validated = _validated_plan(paths, plan, transition)
    if transition.predecessor.target == transition.target.target:
        _caller_ownership(paths, ownership)
        Database(paths.database, assembly=transition.target).validate_snapshot(paths.database)
        return _report(paths, transition, validated, applied=False, already_migrated=True, backup=None)
    return _apply_transition(paths, transition, validated, backup=backup, ownership=ownership)


# -- command line -------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("inspect", "read-only recognition of the installed Operations store"),
                            ("prepare", "read-only canonical plan for the transition (print or --save)"),
                            ("apply", "apply a saved plan offline under the instance lifetime lock")):
        command = commands.add_parser(name, help=help_text)
        add_instance_arguments(command)
        command.add_argument("--extensions-config", type=Path, default=None,
                             help="Explicit extension selection in the trusted installation-config format")
        if name == "prepare":
            command.add_argument("--save", type=Path, default=None, help="New private 0600 file for the plan")
        if name == "apply":
            command.add_argument("--plan", type=Path, required=True, help="The saved plan file (0600 in a 0700 dir)")
            command.add_argument("--backup", type=Path, required=True,
                                 help="New backup file (explicit path in an existing owned 0700 directory)")
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        extensions = _selected_extensions(options.extensions_config)
        if options.command == "inspect":
            result = inspect_migration(paths, extensions=extensions)
        elif options.command == "prepare":
            result = prepare_migration(paths, extensions=extensions)
            if options.save is not None:
                _save_plan(result, options.save)
        else:
            result = apply_migration(paths, _read_plan(options.plan), backup=options.backup, extensions=extensions)
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


def _selected_extensions(path):
    if path is None:
        return NO_EXTENSIONS
    from qorqut.extensions import select
    from qorqut.installation import extensions_configuration
    return select(extensions_configuration(path)["extensions"])


def _save_plan(plan, path: Path) -> None:
    path = Path(path)
    if not path.is_absolute():
        raise DomainError("invalid_configuration", "The plan destination needs an explicit absolute path")
    write_private_file(path.parent, path.name, (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())