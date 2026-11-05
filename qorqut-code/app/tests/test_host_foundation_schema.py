"""Acceptance case 2: the real host schema and mandatory hooks.

The composed target schema is the committed contribution set, engine-validated; a host missing a mandatory hook, repeating a
contribution, or opened over a store of another identity is refused before anything is written."""

import hashlib
import sqlite3

import pytest

from host_foundation_support import build_instance, make_paths, tree
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import (AssemblyError, FileEffectContribution, RecordEffectContribution,
                                                StorageContribution, TransactionContribution)
from qorqut_operations.storage.specs import TableSpec

from bazarlyk.composition.assembly import (HostComposition, host_assembly, host_contributions, initialize_empty_target,
                                           open_database)
from qorqut_operations.document_history.files import DocumentReceiptHistory
from qorqut_operations.document_history.lifecycle import DocumentLifecycle
from bazarlyk.composition.records import HostRecordEffects
from bazarlyk.service.changes import ChangeFeed


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def composed(feed, *, drop=(), extra=(), **options):
    composition = HostComposition(**options)
    for contribution in host_contributions(feed):
        if not any(isinstance(contribution, kind) and contribution.owner == owner for kind, owner in drop):
            composition.add(contribution)
    for contribution in extra:
        composition.add(contribution)
    return composition


def test_empty_target_initialises_explicitly_and_reopens_with_the_same_identity(tmp_path):
    paths = make_paths(tmp_path)
    assert tree(paths.instance_root) == []
    assembly = host_assembly(ChangeFeed())
    database = initialize_empty_target(paths, assembly)
    assert paths.database.is_file() and database.assembly.target.name == "qorqut-v1.2"

    reopened = open_database(paths, host_assembly(ChangeFeed()))
    assert reopened.assembly.target == assembly.target
    with reopened.read() as uow:
        names = {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Representative engine-owned and host-owned tables share the one database.
    assert {"principals", "businesses", "idempotency_receipts", "apply_receipts", "tasks", "documents",
            "inbox_changes", "agent_profiles", "signoffs"} <= names
    # Each composed table is owned by exactly one contribution and carries one engine table spec.
    assert {"tasks", "documents", "inbox_changes", "businesses"} <= set(assembly.tables)


@pytest.mark.parametrize("dropped, message", [
    ((RecordEffectContribution, "records"), "record effect hooks"),
    ((FileEffectContribution, "documents"), "receipt history"),
])
def test_a_missing_mandatory_hook_is_refused_at_freeze(dropped, message):
    with pytest.raises(AssemblyError, match=message):
        composed(ChangeFeed(), drop=(dropped,)).freeze()


def test_a_document_resource_without_its_lifecycle_is_refused():
    receipt_only = FileEffectContribution("receipts", receipt_history=DocumentReceiptHistory(byte_capture={}))
    with pytest.raises(AssemblyError, match="resource lifecycle for documents"):
        composed(ChangeFeed(), drop=((FileEffectContribution, "documents"),), extra=(receipt_only,)).freeze()


def test_repeated_ownership_and_untyped_contributions_are_refused():
    feed = ChangeFeed()
    composition = composed(feed)
    with pytest.raises(AssemblyError, match="Duplicate host contribution"):
        composition.add(RecordEffectContribution("records", HostRecordEffects()))
    with pytest.raises(AssemblyError, match="Duplicate host table ownership"):
        composition.add(StorageContribution("second-tasks", {"tasks": TableSpec(("id",))}, {},
                                            ("CREATE TABLE tasks(id TEXT PRIMARY KEY)",)))
    with pytest.raises(AssemblyError, match="Only one change publisher"):
        composed(feed).add(TransactionContribution("other-feed", change_publisher=ChangeFeed()))
    with pytest.raises(AssemblyError, match="typed contributions"):
        composed(feed).add(object())
    with pytest.raises(AssemblyError, match="registered for owner|Duplicate host contribution"):
        composed(feed).add(FileEffectContribution("documents", {"x_resource": DocumentLifecycle()}))


def test_the_host_change_publisher_must_be_its_change_feed():
    with pytest.raises(TypeError, match="ChangeFeed"):
        host_contributions(object())


def test_other_identity_or_non_target_stores_are_refused_without_mutation(tmp_path):
    paths = make_paths(tmp_path)
    initialize_empty_target(paths, host_assembly(ChangeFeed()))
    before = digest(paths.database)
    for options, message in (({"target_version": 2}, "target schema version"),
                             ({"target_name": "other-target"}, "target identity")):
        with pytest.raises(DomainError, match=message):
            open_database(paths, composed(ChangeFeed(), **options).freeze())
    with pytest.raises(DomainError, match="already has tables"):
        initialize_empty_target(paths, host_assembly(ChangeFeed()))
    assert digest(paths.database) == before

    legacy = make_paths(tmp_path, "legacy")
    legacy.runtime.mkdir(parents=True)
    connection = sqlite3.connect(legacy.database)
    connection.executescript("CREATE TABLE schema_migrations(version INTEGER); CREATE TABLE tasks(id TEXT);")
    connection.commit()
    connection.close()
    legacy_before = digest(legacy.database)
    with pytest.raises(DomainError, match="not an Operations target store"):
        open_database(legacy, host_assembly(ChangeFeed()))
    with pytest.raises(DomainError, match="already has tables"):
        initialize_empty_target(legacy, host_assembly(ChangeFeed()))
    assert digest(legacy.database) == legacy_before
    assert tree(legacy.operations) == [".runtime", ".runtime/operations.sqlite3"]


def test_host_construction_and_a_missing_store_create_nothing(tmp_path):
    from bazarlyk.service.runtime import build_runtime
    from host_foundation_support import make_workspace

    workspace = make_workspace(tmp_path)
    paths = make_paths(tmp_path)
    host_assembly(ChangeFeed())
    assert tree(paths.instance_root) == []
    with pytest.raises(DomainError, match="missing or crosses a symbolic link"):
        open_database(paths, host_assembly(ChangeFeed()))
    with pytest.raises(DomainError, match="installed agentic layer"):
        build_runtime(workspace, paths=paths)
    assert tree(paths.instance_root) == []


def test_a_built_instance_serves_only_through_the_one_assembled_database(tmp_path):
    instance, _ = build_instance(tmp_path)
    runtime = instance.runtime
    assert runtime.root == instance.paths.operations and runtime.database.path == instance.paths.database
    assert runtime.database.assembly.change_publisher is instance.feed
    runtime.freeze_registry()
    assert runtime.registry.frozen
    # The old engine tree is gone: nothing but the engine package exposes the application engine.
    import importlib.util
    for old in ("bazarlyk.application", "bazarlyk.storage.database", "bazarlyk.contracts"):
        assert importlib.util.find_spec(old) is None, old
