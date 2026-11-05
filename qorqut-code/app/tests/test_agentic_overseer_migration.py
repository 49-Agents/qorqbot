"""Focused synthetic proof of the explicit offline Operations transition ``v1.3-agentic-overseer-1``.

Every store here is disposable and built from the trusted host composition itself: the predecessor is the exact current
``HostComposition`` without the two named additive ``StorageContribution`` owners, created by the ordinary
``Database.initialize_target`` (so its stored identity is the composition's own digest), and the target is the full
composition. No live instance, delivered configuration, native session, provider or runtime service is involved, no
migration is faked, and the only data is synthetic rows in unique temporary instances.

``test_frozen_transition_identities_come_from_the_trusted_composition`` pins the predecessor of the CRM baseline
with the retired learning-engine tables removed, the composed target and the exact schema-object inventory hash derived
from source, so a fixture cannot quietly mirror a helper instead of the real composition.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat

import pytest
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.bootstrap import agentic_migration
from bazarlyk.bootstrap.agentic_migration import apply_migration, inspect_migration, prepare_migration
from bazarlyk.extensions import Extension, NO_EXTENSIONS, select
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.ownership import InstanceOwnership

# Frozen Project-aware predecessor and combined Project/Overseer target. The earlier
# deployed schema must first pass the separate explicit Project source conversion.
POST_PROJECT_PREDECESSOR_DIGEST = "b46e64b9de762405dee4aed6905473893e97a8d2050b4d6948fa4a9cc5fd51b4"
COMPOSED_TARGET_DIGEST = "6cd2113a51097fbba3ede5fb73b93951e2d8979e7b7116093448af40333377a7"
PREDECESSOR_OBJECTS_SHA256 = "7a1f487e452aa544150e56e17a6f41114132607305c38215b6f323539df0507c"
TARGET_OBJECTS_SHA256 = "cae3c01efb91fddb8a56624d5bf87043a8b02972861e0234bcdf0cdaeaf4c1e5"
ADDED_OBJECTS = {"tables": 17, "indexes": 9, "triggers": 2}

OWNER = "5b1d0e0e-0000-4000-8000-000000000001"
AGENT = "5b1d0e0e-0000-4000-8000-000000000002"
BUSINESS = "5b1d0e0e-0000-4000-8000-000000000010"
STAMP = "2026-01-01T00:00:00Z"
EXTENSION_TABLE = "r02_fixture_notes"
EXTENSION_MARKER = "synthetic extension data"


class FixtureExtension(Extension):
    """A selected extension whose own storage is already part of both compositions, with data and a foreign key."""

    id = "r02_fixture"

    def storage(self):
        return (StorageContribution(
            self.id,
            {EXTENSION_TABLE: TableSpec(("id",), "business_id")},
            {},
            (f"""CREATE TABLE {EXTENSION_TABLE} (
                    id TEXT PRIMARY KEY,
                    business_id TEXT NOT NULL REFERENCES businesses(id),
                    body TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1)""",
             f"CREATE INDEX {EXTENSION_TABLE}_business ON {EXTENSION_TABLE}(business_id)")),)


def fixture_selection():
    return select(instances=[FixtureExtension()])


def make_paths(tmp_path, name="instance"):
    root = tmp_path / name
    root.mkdir()
    return InstancePaths(root, {})


def transition(paths, extensions=NO_EXTENSIONS):
    """A real predecessor store, created by the ordinary initializer from the compiled predecessor composition."""
    compiled = agentic_migration._transition_for(extensions)
    Database(paths.database, assembly=compiled.predecessor).initialize_target()
    return compiled


def raw(path):
    connection = sqlite3.connect(path)
    install_sql_functions(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def seed(path, *, extension_table=None):
    """Synthetic operational, native/private and (optionally) extension rows; every trigger is real."""
    statements = (
        ("INSERT INTO principals VALUES (?, 'human', 'Owner', 1, 1, ?, ?)", (OWNER, STAMP, STAMP)),
        ("INSERT INTO principals VALUES (?, 'agent', 'Synthetic agent', 1, 1, ?, ?)", (AGENT, STAMP, STAMP)),
        ("""INSERT INTO businesses (id, slug, workspace, hq_path, name, stage, timezone, config_json, revision,
               created_at, updated_at) VALUES (?, 'synthetic-project', 'synthetic-project',
               'projects/synthetic-project/hq', 'Synthetic project', '', 'UTC', '{}', 1, ?, ?)""",
         (BUSINESS, STAMP, STAMP)),
        ("""INSERT INTO agent_profiles (agent_id, principal_id, home_business_id, namespace, name, workspace, status,
               profile_json, revision, created_at, updated_at) VALUES (?, ?, NULL, 'ecosystem', 'Synthetic agent',
               'synthetic-agent', 'active', '{"scope":{}}', 1, ?, ?)""", (AGENT, AGENT, STAMP, STAMP)),
        ("""INSERT INTO tasks (business_id, id, title, status, due, fields_json, archived, created_by, revision,
               created_at, updated_at, snoozed_until, snooze_generation, kind, parent_id)
               VALUES (?, 'task-1', 'Synthetic task', 'open', NULL, '{}', 0, ?, 1, ?, ?, NULL, 0, 'ordinary', NULL)""",
         (BUSINESS, OWNER, STAMP, STAMP)),
        ("""INSERT INTO agent_current_work (agent_id, item_json, work_updated_at, work_reviewed_at, revision)
               VALUES (?, '{"kind":"task"}', ?, ?, 1)""", (AGENT, STAMP, STAMP)),
        ("""INSERT INTO native_sessions VALUES ('session-1', ?, 'claude', 'conversation-1', 1, 'terminal', 1234,
               ?, NULL, NULL, ?, ?, NULL, NULL, NULL)""", (AGENT, STAMP, STAMP, STAMP)),
        ("""INSERT INTO learning_candidates (business_id, id, candidate_json, revision, created_at, updated_at)
               VALUES (NULL, 'candidate-1', '{"bucket":"delta"}', 1, ?, ?)""", (STAMP, STAMP)),
        ("""INSERT INTO inbox_events (business_id, source_type, source_id, source_revision, actor_id, event_type,
               summary, visibility_json, created_at) VALUES (?, 'task', 'task-1', 1, ?, 'created',
               'Synthetic event', '{}', ?)""", (BUSINESS, OWNER, STAMP)),
        ("""INSERT INTO owner_conversations (id, source_key, business_id, source_type, source_id, audience, revision,
               latest_sequence, created_at, updated_at, peer_agent_id)
               VALUES ('conversation-1', 'task:task-1', ?, 'task', 'task-1', 'owner_ordubashy', 1, 0, ?, ?, NULL)""",
         (BUSINESS, STAMP, STAMP)),
    )
    connection = raw(path)
    try:
        for statement, values in statements:
            connection.execute(statement, values)
        if extension_table is not None:
            connection.execute(f"INSERT INTO {extension_table} (id, business_id, body, revision) VALUES (?, ?, ?, 1)",
                               ("note-1", BUSINESS, EXTENSION_MARKER))
        connection.commit()
    finally:
        connection.close()


def rows(path):
    """Every row of every existing table, independent of insertion order and never a definition or a secret."""
    connection = raw(path)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        return {table: sorted((tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')), key=repr)
                for table in tables}
    finally:
        connection.close()


def identity_columns(path):
    connection = raw(path)
    try:
        return connection.execute("SELECT id, name, version, created_at FROM target_identity").fetchall()
    finally:
        connection.close()


def digest(path):
    connection = raw(path)
    try:
        return connection.execute("SELECT digest FROM target_identity").fetchone()[0]
    finally:
        connection.close()


def pragmatic(path):
    connection = raw(path)
    try:
        return (connection.execute("PRAGMA application_id").fetchone()[0],
                connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def tree(root):
    return sorted(str(item.relative_to(root)) for item in Path(root).rglob("*"))


def table_names(path):
    connection = raw(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def inventory_digest(objects):
    return hashlib.sha256(canonical_bytes([(kind, name, owner, sql)
                                           for (kind, name), (owner, sql) in sorted(objects.items())])).hexdigest()


def private_directory(tmp_path, name):
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def set_pragma(path, statement):
    """Change one Operations pragma through an ordinary write connection (a synthetic drifted store)."""
    connection = raw(path)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def held_wal_store(tmp_path, name="wal-source"):
    """A recognised predecessor with one committed data change that only its held WAL carries."""
    paths = make_paths(tmp_path, name)
    transition(paths)
    writer = raw(paths.database)
    writer.execute("UPDATE change_sequences SET value = value + 1 WHERE name = 'signoffs'")
    writer.commit()
    return paths, writer


def copy_store(source, tmp_path, name, sidecars):
    """Copy a coherent main file (and named sidecars) from a held WAL connection into a new instance."""
    root = tmp_path / name
    root.mkdir()
    target = InstancePaths(root, {})
    target.database.parent.mkdir(parents=True)
    shutil.copy2(source.database, target.database)
    for suffix in sidecars:
        shutil.copy2(str(source.database) + suffix, str(target.database) + suffix)
    return target


# -- the frozen transition, derived from source ----------------------------------------------------------------

def test_frozen_transition_identities_come_from_the_trusted_composition(tmp_path):
    compiled = agentic_migration._transition_for(NO_EXTENSIONS)
    assert [contribution.owner for contribution in compiled.contributions] == ["agentic-inbox", "overseer"]
    assert compiled.predecessor.target.name == compiled.target.target.name == "qorqut-v1.2"
    assert compiled.predecessor.target.version == compiled.target.target.version == 1
    assert compiled.predecessor.target.digest == POST_PROJECT_PREDECESSOR_DIGEST
    assert compiled.target.target.digest == COMPOSED_TARGET_DIGEST
    # The immutable identities above are the assembly's hash over the compiled statements, table specifications and
    # immutable columns; the additional object inventory hashes pin the exact schema objects those statements build.
    assert inventory_digest(compiled.predecessor_objects.objects) == PREDECESSOR_OBJECTS_SHA256
    assert inventory_digest(compiled.target_objects.objects) == TARGET_OBJECTS_SHA256
    assert {kind: len(names) for kind, names in compiled.additions.items()} == ADDED_OBJECTS
    # Additive only: every predecessor object exists byte for byte in the target, and the additions are exactly the two
    # named contributions' own objects.
    assert all(compiled.target_objects.objects[key] == value
               for key, value in compiled.predecessor_objects.objects.items())
    assert set(compiled.predecessor_objects.objects) < set(compiled.target_objects.objects)
    added = set(compiled.target_objects.objects) - set(compiled.predecessor_objects.objects)
    assert {name for kind, name in added if kind == "table"} == set(compiled.additions["tables"])
    # The same composition is what the real initializer records: the fixture is not a mirrored helper.
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=compiled.predecessor).initialize_target()
    report = inspect_migration(paths)
    assert report["state"] == "predecessor"
    assert report["identity"]["digest"] == POST_PROJECT_PREDECESSOR_DIGEST
    assert digest(paths.database) == POST_PROJECT_PREDECESSOR_DIGEST


# -- read-only planning ----------------------------------------------------------------------------------------

def test_inspection_and_preparation_are_read_only_and_carry_no_rows(tmp_path):
    paths = make_paths(tmp_path)
    transition(paths)
    seed(paths.database)
    before = tree(paths.instance_root)
    report = inspect_migration(paths)
    assert report["state"] == "predecessor"
    assert report["selected_extensions"] == []
    assert {kind: len(names) for kind, names in report["additions"].items()} == ADDED_OBJECTS
    plan = prepare_migration(paths)
    assert tree(paths.instance_root) == before                      # no lock, sidecar, directory, binding or backup
    assert prepare_migration(paths) == plan                         # deterministic
    assert set(plan) == {"format", "transition", "instance_root", "database", "selected_extensions", "predecessor",
                         "target", "additions", "plan_digest"}
    assert plan["transition"] == "v1.3-agentic-overseer-1" and plan["instance_root"] == str(paths.instance_root)
    assert plan["database"] == str(paths.database) and plan["instance_root"] == str(paths.instance_root)
    assert plan["plan_digest"] == canonical_hash({key: value for key, value in plan.items() if key != "plan_digest"})
    encoded = json.dumps(plan)
    assert "Synthetic task" not in encoded and EXTENSION_MARKER not in encoded and "conversation-1" not in encoded


def test_inspection_reports_instead_of_raising_a_missing_store(tmp_path):
    paths = make_paths(tmp_path)
    report = inspect_migration(paths)
    assert report["state"] == "missing" and report["identity"] is None
    with pytest.raises(DomainError, match="not exactly the recognised"):
        prepare_migration(paths)


# -- the transition itself -------------------------------------------------------------------------------------

def test_apply_preserves_every_row_and_both_compositions_open_their_own_store(tmp_path):
    paths = make_paths(tmp_path)
    compiled = transition(paths)
    seed(paths.database)
    Database(paths.database, assembly=compiled.predecessor).open_existing()      # ordinary predecessor open passes
    before_rows, before_identity, before_pragmas = rows(paths.database), identity_columns(paths.database), pragmatic(paths.database)
    plan = prepare_migration(paths)
    backup_directory = private_directory(tmp_path, "backups")
    backup = backup_directory / "predecessor.sqlite3"
    result = apply_migration(paths, plan, backup=backup)
    assert result["applied"] is True and result["already_migrated"] is False
    assert result["predecessor"]["digest"] == POST_PROJECT_PREDECESSOR_DIGEST
    assert result["target"]["digest"] == COMPOSED_TARGET_DIGEST and result["plan_digest"] == plan["plan_digest"]
    assert result["backup"] == {"path": str(backup), "sha256": hashlib.sha256(backup.read_bytes()).hexdigest()}
    # Every existing row is unchanged; only target_identity.digest moved and its other columns are byte for byte.
    after_rows = rows(paths.database)
    unchanged = {table: value for table, value in before_rows.items() if table != "target_identity"}
    assert {table: after_rows[table] for table in unchanged} == unchanged
    assert after_rows["target_identity"] != before_rows["target_identity"]           # only the digest moved
    assert identity_columns(paths.database) == before_identity
    assert pragmatic(paths.database) == before_pragmas
    assert digest(paths.database) == COMPOSED_TARGET_DIGEST
    added_tables = set(compiled.additions["tables"])
    assert added_tables <= table_names(paths.database)
    connection = raw(paths.database)
    try:
        assert all(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in added_tables)
    finally:
        connection.close()
    # The result opens exactly through the target composition; the predecessor identity is refused now.
    Database(paths.database, assembly=compiled.target).open_existing()
    with pytest.raises(DomainError):
        Database(paths.database, assembly=compiled.predecessor).open_existing()
    # The retained backup is one private self-contained file that opens exactly through the predecessor composition.
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700
    assert sorted(path.name for path in backup_directory.iterdir()) == [backup.name]
    Database(backup, assembly=compiled.predecessor).open_existing()
    with pytest.raises(DomainError):
        Database(backup, assembly=compiled.target).open_existing()


def test_a_selected_synthetic_extension_with_data_survives_the_same_transition(tmp_path):
    paths = make_paths(tmp_path)
    selection = fixture_selection()
    compiled = transition(paths, selection)
    assert compiled.extensions.ids == ("r02_fixture",)
    seed(paths.database, extension_table=EXTENSION_TABLE)
    before_rows = rows(paths.database)
    plan = prepare_migration(paths, extensions=selection)
    assert plan["selected_extensions"] == ["r02_fixture"]
    result = apply_migration(paths, plan, backup=private_directory(tmp_path, "backups") / "extension.sqlite3",
                             extensions=selection)
    assert result["applied"] is True
    after_rows = rows(paths.database)
    unchanged = {table: value for table, value in before_rows.items() if table != "target_identity"}
    assert {table: after_rows[table] for table in unchanged} == unchanged
    connection = raw(paths.database)
    try:
        assert connection.execute(f"SELECT body, business_id FROM {EXTENSION_TABLE} WHERE id='note-1'").fetchone() == (
            EXTENSION_MARKER, BUSINESS)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()
    Database(paths.database, assembly=compiled.target).open_existing()


def test_a_retry_on_the_target_is_a_validated_no_op_without_a_second_backup(tmp_path):
    paths = make_paths(tmp_path)
    compiled = transition(paths)
    plan = prepare_migration(paths)
    backup = private_directory(tmp_path, "backups") / "first.sqlite3"
    assert apply_migration(paths, plan, backup=backup)["applied"] is True
    committed = digest(paths.database)
    second = private_directory(tmp_path, "second") / "never.sqlite3"
    result = apply_migration(paths, plan, backup=second)
    assert result == {**result, "applied": False, "already_migrated": True, "backup": None}
    assert not second.exists() and digest(paths.database) == committed
    assert prepare_migration(paths)["plan_digest"] == plan["plan_digest"]
    Database(paths.database, assembly=compiled.target).open_existing()


# -- ownership -------------------------------------------------------------------------------------------------

def test_lifetime_ownership_refuses_a_running_host_and_caller_ownership_is_never_released(tmp_path):
    paths = make_paths(tmp_path)
    compiled = transition(paths)
    plan = prepare_migration(paths)
    backup_directory = private_directory(tmp_path, "backups")
    held = InstanceOwnership(paths).acquire()
    try:
        with pytest.raises(DomainError) as refused:
            apply_migration(paths, plan, backup=backup_directory / "busy.sqlite3")
        assert refused.value.code == "instance_in_use"
    finally:
        held.release()
    assert inspect_migration(paths)["state"] == "predecessor"
    assert not (backup_directory / "busy.sqlite3").exists()
    # A caller-owned lifetime lock is verified and kept for the coordinated caller's own later phases.
    outer = InstanceOwnership(paths).acquire()
    try:
        result = apply_migration(paths, plan, backup=backup_directory / "caller.sqlite3", ownership=outer)
        assert result["applied"] is True
        assert outer.held is True and outer.path == paths.runtime / "instance-lifetime.lock"
        with pytest.raises(DomainError):
            InstanceOwnership(paths).acquire()                       # still one continuous exclusive owner
    finally:
        outer.release()
    assert inspect_migration(paths)["state"] == "target"
    Database(paths.database, assembly=compiled.target).open_existing()
    # A constructed but unheld owner, and an owner of another instance, are both refused before anything is touched.
    with pytest.raises(DomainError, match="not held"):
        apply_migration(paths, plan, backup=backup_directory / "unheld.sqlite3", ownership=InstanceOwnership(paths))
    other = make_paths(tmp_path, "other")
    other.runtime.mkdir(parents=True)
    foreign = InstanceOwnership(other).acquire()
    try:
        with pytest.raises(DomainError, match="another instance"):
            apply_migration(paths, plan, backup=backup_directory / "foreign.sqlite3", ownership=foreign)
    finally:
        foreign.release()
    assert not (backup_directory / "foreign.sqlite3").exists()


# -- fail-closed stores and plans ------------------------------------------------------------------------------

def test_missing_symlinked_unknown_and_partial_stores_fail_closed(tmp_path):
    # Missing database: planning refuses, apply refuses after taking its own lock, nothing is created or repaired.
    missing = make_paths(tmp_path, "missing")
    compiled = transition(missing)
    plan = prepare_migration(missing)
    missing.database.unlink()
    assert inspect_migration(missing)["state"] == "missing"
    with pytest.raises(DomainError, match="not exactly the recognised"):
        prepare_migration(missing)
    with pytest.raises(DomainError) as refused:
        apply_migration(missing, plan, backup=private_directory(tmp_path, "missing-backups") / "never.sqlite3")
    assert refused.value.code == "invalid_database"
    assert not missing.database.exists()
    # A symlinked store is refused, never followed.
    linked = make_paths(tmp_path, "linked")
    transition(linked)
    plan = prepare_migration(linked)
    real = linked.database.with_name("real.sqlite3")
    linked.database.rename(real)
    os.symlink(real.name, linked.database)
    with pytest.raises(DomainError):
        inspect_migration(linked)
    with pytest.raises(DomainError):
        prepare_migration(linked)
    with pytest.raises(DomainError):
        apply_migration(linked, plan, backup=private_directory(tmp_path, "linked-backups") / "never.sqlite3")
    assert os.path.islink(real.parent / "operations.sqlite3") and real.exists()
    # A stored identity digest that no longer matches this composition (the schema itself untouched) is unknown, too.
    wrong_identity = make_paths(tmp_path, "wrong-identity")
    transition(wrong_identity)
    plan = prepare_migration(wrong_identity)
    connection = raw(wrong_identity.database)
    try:
        connection.execute("UPDATE target_identity SET digest = ? WHERE id = 'target'", ("0" * 64,))
        connection.commit()
    finally:
        connection.close()
    assert inspect_migration(wrong_identity)["state"] == "unknown"
    with pytest.raises(DomainError):
        prepare_migration(wrong_identity)
    with pytest.raises(DomainError):
        apply_migration(wrong_identity, plan,
                        backup=private_directory(tmp_path, "identity-backups") / "never.sqlite3")
    assert digest(wrong_identity.database) == "0" * 64                        # never rewritten by the refusal
    # A copied valid identity over a drifted schema is an unknown store: never a fingerprint-only bypass.
    drifted = make_paths(tmp_path, "drifted")
    transition(drifted)
    plan = prepare_migration(drifted)
    connection = raw(drifted.database)
    try:
        connection.execute("CREATE TABLE unexpected_object (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    assert inspect_migration(drifted)["state"] == "unknown"
    with pytest.raises(DomainError):
        prepare_migration(drifted)
    with pytest.raises(DomainError) as refused:
        apply_migration(drifted, plan, backup=private_directory(tmp_path, "drifted-backups") / "never.sqlite3")
    assert refused.value.code == "unsupported_schema"
    assert "unexpected_object" in table_names(drifted.database)                        # never repaired
    assert digest(drifted.database) == POST_PROJECT_PREDECESSOR_DIGEST
    # A partial predecessor (one contribution's table missing) is unknown as well.
    partial = make_paths(tmp_path, "partial")
    transition(partial)
    plan = prepare_migration(partial)
    connection = raw(partial.database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE telegram_state")
        connection.commit()
    finally:
        connection.close()
    assert inspect_migration(partial)["state"] == "unknown"
    with pytest.raises(DomainError):
        prepare_migration(partial)
    with pytest.raises(DomainError):
        apply_migration(partial, plan, backup=private_directory(tmp_path, "partial-backups") / "never.sqlite3")
    assert "telegram_state" not in table_names(partial.database)
    # A file that is not an SQLite database is refused as unreadable, never opened for writing.
    corrupt = make_paths(tmp_path, "corrupt")
    transition(corrupt)
    plan = prepare_migration(corrupt)
    corrupt.database.write_bytes(b"not an sqlite database")
    with pytest.raises(DomainError, match="cannot be read"):
        inspect_migration(corrupt)
    with pytest.raises(DomainError):
        prepare_migration(corrupt)
    with pytest.raises(DomainError):
        apply_migration(corrupt, plan, backup=private_directory(tmp_path, "corrupt-backups") / "never.sqlite3")
    assert corrupt.database.read_bytes() == b"not an sqlite database"


def test_wrong_plan_instance_selection_and_transition_are_refused(tmp_path):
    paths = make_paths(tmp_path)
    transition(paths)
    plan = prepare_migration(paths)
    backups = private_directory(tmp_path, "backups")
    with pytest.raises(DomainError, match="changed or corrupted"):
        apply_migration(paths, {**plan, "target": {**plan["target"], "digest": "0" * 64}}, backup=backups / "a.sqlite3")
    changed = {**plan, "target": {**plan["target"], "digest": "0" * 64}}
    changed.pop("plan_digest")
    changed["plan_digest"] = canonical_hash(changed)              # even a correctly recomputed digest is refused
    with pytest.raises(DomainError, match="does not describe this instance"):
        apply_migration(paths, changed, backup=backups / "b.sqlite3")
    other_transition = {**plan, "transition": "v1.3-something-else"}
    other_transition.pop("plan_digest")
    other_transition["plan_digest"] = canonical_hash(other_transition)
    with pytest.raises(DomainError, match="not this transition"):
        apply_migration(paths, other_transition, backup=backups / "c.sqlite3")
    with pytest.raises(DomainError, match="unexpected shape"):
        apply_migration(paths, {**plan, "unexpected": True}, backup=backups / "d.sqlite3")
    # The same plan for another instance root (even with the same relative layout) is refused.
    other = make_paths(tmp_path, "other")
    transition(other)
    with pytest.raises(DomainError, match="does not describe this instance"):
        apply_migration(other, plan, backup=private_directory(tmp_path, "other-backups") / "never.sqlite3")
    # A different explicit extension selection is refused in both directions; nothing is applied.
    with pytest.raises(DomainError, match="does not describe this instance"):
        apply_migration(paths, plan, backup=backups / "e.sqlite3", extensions=fixture_selection())
    extension_paths = make_paths(tmp_path, "extension")
    transition(extension_paths, fixture_selection())
    extension_plan = prepare_migration(extension_paths, extensions=fixture_selection())
    with pytest.raises(DomainError, match="does not describe this instance"):
        apply_migration(extension_paths, extension_plan, backup=backups / "f.sqlite3")
    assert inspect_migration(paths)["state"] == "predecessor"
    assert inspect_migration(extension_paths, extensions=fixture_selection())["state"] == "predecessor"
    assert sorted(path.name for path in backups.iterdir()) == []


# -- rollback, backup preservation and backup safety -----------------------------------------------------------

def test_injected_failure_rolls_back_schema_and_identity_and_keeps_the_verified_backup(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    compiled = transition(paths)
    seed(paths.database)
    before_rows = rows(paths.database)
    plan = prepare_migration(paths)
    backups = private_directory(tmp_path, "backups")
    identity_failure = backups / "identity-failure.sqlite3"
    real_update = agentic_migration._update_identity

    def fail_identity(connection, target):
        raise DomainError("synthetic_failure", "synthetic failure after the additive DDL")

    monkeypatch.setattr(agentic_migration, "_update_identity", fail_identity)
    with pytest.raises(DomainError, match="synthetic failure"):
        apply_migration(paths, plan, backup=identity_failure)
    monkeypatch.setattr(agentic_migration, "_update_identity", real_update)
    assert inspect_migration(paths)["state"] == "predecessor"
    assert digest(paths.database) == POST_PROJECT_PREDECESSOR_DIGEST
    assert not (set(compiled.additions["tables"]) & table_names(paths.database))       # every DDL effect rolled back
    restored = rows(paths.database)
    assert {table: restored[table] for table in before_rows} == before_rows                         # nothing moved
    Database(identity_failure, assembly=compiled.predecessor).open_existing()          # the backup is preserved
    with pytest.raises(DomainError):
        Database(paths.database, assembly=compiled.target).open_existing()
    # A failure after only some of the additive statements behaves the same.
    ddl_failure = backups / "ddl-failure.sqlite3"
    real_execute = agentic_migration._execute_additions

    def fail_midway(connection, contributions):
        connection.execute(contributions[0].schema[0])
        raise DomainError("synthetic_failure", "synthetic failure mid-DDL")

    monkeypatch.setattr(agentic_migration, "_execute_additions", fail_midway)
    with pytest.raises(DomainError, match="mid-DDL"):
        apply_migration(paths, plan, backup=ddl_failure)
    monkeypatch.setattr(agentic_migration, "_execute_additions", real_execute)
    assert inspect_migration(paths)["state"] == "predecessor"
    assert digest(paths.database) == POST_PROJECT_PREDECESSOR_DIGEST
    Database(ddl_failure, assembly=compiled.predecessor).open_existing()
    # The retained backups still validate the predecessor, and a forward retry applies once.
    retry = backups / "retry.sqlite3"
    assert apply_migration(paths, plan, backup=retry)["applied"] is True
    Database(paths.database, assembly=compiled.target).open_existing()
    Database(identity_failure, assembly=compiled.predecessor).open_existing()
    Database(ddl_failure, assembly=compiled.predecessor).open_existing()


def test_backup_destinations_are_explicit_private_and_never_replaced(tmp_path):
    paths = make_paths(tmp_path)
    transition(paths)
    plan = prepare_migration(paths)
    # A group- or world-accessible directory is refused.
    open_directory = tmp_path / "open"
    open_directory.mkdir()
    os.chmod(open_directory, 0o755)
    with pytest.raises(DomainError, match="0700"):
        apply_migration(paths, plan, backup=open_directory / "never.sqlite3")
    assert not (open_directory / "never.sqlite3").exists()
    # A missing directory is refused, and a relative destination is refused.
    with pytest.raises(DomainError, match="missing"):
        apply_migration(paths, plan, backup=tmp_path / "absent" / "never.sqlite3")
    with pytest.raises(DomainError, match="absolute"):
        apply_migration(paths, plan, backup=Path("never.sqlite3"))
    # An existing file or symlink is refused and preserved, and the store is untouched.
    private = private_directory(tmp_path, "backups")
    existing = private / "exists.sqlite3"
    existing.write_bytes(b"synthetic existing content")
    os.chmod(existing, 0o600)
    with pytest.raises(DomainError, match="already exists"):
        apply_migration(paths, plan, backup=existing)
    assert existing.read_bytes() == b"synthetic existing content"
    link = private / "link.sqlite3"
    os.symlink(existing, link)
    with pytest.raises(DomainError, match="already exists"):
        apply_migration(paths, plan, backup=link)
    assert link.is_symlink() and existing.read_bytes() == b"synthetic existing content"
    assert inspect_migration(paths)["state"] == "predecessor"
    assert digest(paths.database) == POST_PROJECT_PREDECESSOR_DIGEST


# -- Operations pragmas and WAL sidecars -----------------------------------------------------------------------

def test_wrong_operations_pragmas_are_unknown_on_both_recognised_states(tmp_path):
    """A valid schema and identity with a wrong application id or user version is never a recognised state."""
    cases = (("application_id", "PRAGMA application_id = 1234", 1234),
             ("user_version", "PRAGMA user_version = 999", 999))
    for label, statement, value in cases:
        predecessor = make_paths(tmp_path, "predecessor-" + label)
        transition(predecessor)
        plan = prepare_migration(predecessor)
        set_pragma(predecessor.database, statement)
        before = tree(predecessor.instance_root)
        report = inspect_migration(predecessor)
        assert report["state"] == "unknown"
        assert report[label] == value and label in report["problems"]
        with pytest.raises(DomainError) as refused:
            prepare_migration(predecessor)
        assert refused.value.code == "unsupported_schema"
        assert tree(predecessor.instance_root) == before                      # inspection/preparation wrote nothing
        backups = private_directory(tmp_path, "backup-p-" + label)
        with pytest.raises(DomainError):
            apply_migration(predecessor, plan, backup=backups / "never.sqlite3")
        assert not (backups / "never.sqlite3").exists()
    for label, statement, value in cases:
        target = make_paths(tmp_path, "target-" + label)
        transition(target)
        plan = prepare_migration(target)
        apply_migration(target, plan, backup=private_directory(tmp_path, "backup-t-" + label) / "before.sqlite3")
        assert inspect_migration(target)["state"] == "target"                 # the fully checkpointed target first
        set_pragma(target.database, statement)
        before = tree(target.instance_root)
        report = inspect_migration(target)
        assert report["state"] == "unknown"
        assert report[label] == value and label in report["problems"]
        with pytest.raises(DomainError) as refused:
            prepare_migration(target)
        assert refused.value.code == "unsupported_schema"
        assert tree(target.instance_root) == before
        backups = private_directory(tmp_path, "backup-t2-" + label)
        with pytest.raises(DomainError):
            apply_migration(target, plan, backup=backups / "never.sqlite3")
        assert not (backups / "never.sqlite3").exists()


def test_an_incomplete_wal_sidecar_pair_is_refused_without_creating_artifacts(tmp_path):
    """Only the main file or a whole coherent pair is a safe read-only inspection; never a repaired/stale one."""
    source, writer = held_wal_store(tmp_path)
    try:
        missing_shm = copy_store(source, tmp_path, "missing-shm", ("-wal",))
        missing_wal = copy_store(source, tmp_path, "missing-wal", ())
        (Path(str(missing_wal.database) + "-shm")).write_bytes(b"")
        for fixture in (missing_shm, missing_wal):
            before = tree(fixture.instance_root)
            with pytest.raises(DomainError) as refused:
                inspect_migration(fixture)
            assert refused.value.code == "invalid_database"
            with pytest.raises(DomainError):
                prepare_migration(fixture)
            assert tree(fixture.instance_root) == before                      # no missing sidecar was created
    finally:
        writer.close()


def test_a_complete_wal_pair_is_inspected_without_new_artifacts_and_checkpointed_stores_stay_immutable(tmp_path):
    """A complete pair from an unclean stop is read coherently (pending WAL included); the main-only store is immutable."""
    source, writer = held_wal_store(tmp_path)
    try:
        complete = copy_store(source, tmp_path, "complete", ("-wal", "-shm"))
    finally:
        writer.close()
    # The copied main file alone still carries the seeded value; the committed change lives in the copied WAL.
    connection = sqlite3.connect(Path(complete.database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        pending = connection.execute("SELECT value FROM change_sequences WHERE name='signoffs'").fetchone()[0]
    finally:
        connection.close()
    assert pending == 1
    before = tree(complete.instance_root)
    report = inspect_migration(complete)
    assert report["state"] == "predecessor" and report["identity"]["digest"] == POST_PROJECT_PREDECESSOR_DIGEST
    plan = prepare_migration(complete)
    assert plan["plan_digest"]
    assert tree(complete.instance_root) == before                            # no artifact was added
    # A fully checkpointed store (main file only) keeps the immutable, no-lock, no-artifact path for both states.
    checkpointed = make_paths(tmp_path, "checkpointed")
    transition(checkpointed)
    before = tree(checkpointed.instance_root)
    assert inspect_migration(checkpointed)["state"] == "predecessor"
    assert prepare_migration(checkpointed)["plan_digest"]
    assert tree(checkpointed.instance_root) == before
    apply_migration(checkpointed, prepare_migration(checkpointed),
                    backup=private_directory(tmp_path, "checkpointed-backups") / "before.sqlite3")
    assert inspect_migration(checkpointed)["state"] == "target"


# -- CLI -------------------------------------------------------------------------------------------------------

def test_the_cli_inspects_prepares_saves_and_applies_one_plan(tmp_path, capsys):
    paths = make_paths(tmp_path)
    compiled = transition(paths)
    private = private_directory(tmp_path, "private")
    backups = private_directory(tmp_path, "backups")

    def run(*arguments):
        assert agentic_migration.main([*arguments, "--instance-root", str(paths.instance_root)]) == 0
        return json.loads(capsys.readouterr().out)["result"]

    assert run("inspect")["state"] == "predecessor"
    plan_file = private / "plan.json"
    plan = run("prepare", "--save", str(plan_file))
    assert plan_file.is_file() and stat.S_IMODE(plan_file.stat().st_mode) == 0o600
    assert json.loads(plan_file.read_text()) == plan
    applied = run("apply", "--plan", str(plan_file), "--backup", str(backups / "cli.sqlite3"))
    assert applied["applied"] is True and applied["plan_digest"] == plan["plan_digest"]
    assert run("inspect")["state"] == "target"
    Database(paths.database, assembly=compiled.target).open_existing()
    # A failure is a structured JSON error with a non-zero status, not a traceback.
    status = agentic_migration.main(["apply", "--instance-root", str(paths.instance_root),
                                     "--plan", str(private / "missing.json"),
                                     "--backup", str(backups / "never.sqlite3")])
    assert status == 1 and json.loads(capsys.readouterr().out)["ok"] is False