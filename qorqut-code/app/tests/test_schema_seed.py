"""The empty-schema seed behind ``database_at`` and ``Fixture`` (tests/schema_seed.py)."""

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.storage import Database, TABLES
from bazarlyk.storage import database as storage
from foundation_support import HUMAN, database_at
from schema_seed import _fingerprint, initialized


def shape(database):
    with database.read() as uow:
        execute = uow.connection.execute
        return ([tuple(row) for row in execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")],
                [tuple(row) for row in execute("SELECT version, checksum FROM schema_migrations ORDER BY version")],
                [execute(f"PRAGMA {name}").fetchone()[0] for name in ("user_version", "application_id", "journal_mode")],
                execute("PRAGMA integrity_check").fetchone()[0], execute("PRAGMA foreign_key_check").fetchall())


def first_applied_at(database):
    with database.read() as uow:
        return uow.connection.execute("SELECT applied_at FROM schema_migrations WHERE version=1").fetchone()[0]


def name_owner(database, name):
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": name})


def owner_name(database):
    with database.read() as uow:
        row = uow.repository("principals").get(HUMAN)
        return row and row["display_name"]


def test_copy_has_exactly_the_schema_real_initialization_builds(tmp_path):
    copied = initialized(Database(tmp_path / "copy" / "state.sqlite3"))
    real = Database(tmp_path / "real" / "state.sqlite3").initialize()
    # Same private, single-file result: no -wal/-shm left over from validating the copy.
    assert [entry.name for entry in copied.path.parent.iterdir()] == [entry.name for entry in real.path.parent.iterdir()] == ["state.sqlite3"]
    assert copied.path.stat().st_mode & 0o777 == real.path.stat().st_mode & 0o777 == 0o600
    assert shape(copied) == shape(real)
    assert shape(copied)[3:] == ("ok", [])
    assert {table: copied.columns(table) for table in TABLES} == {table: real.columns(table) for table in TABLES}


def test_copies_are_independent_databases_and_the_seed_stays_empty(tmp_path):
    first, second = database_at(tmp_path / "first"), database_at(tmp_path / "second")
    assert first is not second and first.path != second.path and first.path.stat().st_ino != second.path.stat().st_ino
    with first.transaction() as uow:
        uow.repository("principals").update(HUMAN, {"display_name": "Changed"}, expected_revision=1)
    assert owner_name(first) == "Changed" and owner_name(second) == "Owner"
    later = initialized(Database(tmp_path / "later" / "state.sqlite3"))
    with later.read() as uow:
        assert uow.repository("principals").list() == [] and uow.repository("businesses").list() == []
    assert first_applied_at(first) == first_applied_at(second) == first_applied_at(later)


def test_existing_destination_keeps_its_contents(tmp_path):
    path = tmp_path / "state.sqlite3"
    original = Database(path).initialize()
    name_owner(original, "Kept")
    original.close()  # the row now lives in the main file, not a write-ahead log
    assert owner_name(initialized(Database(path))) == "Kept"


def test_explicit_opt_out_and_symlinked_paths_run_real_initialization(tmp_path):
    seeded = first_applied_at(initialized(Database(tmp_path / "seeded" / "state.sqlite3")))
    assert first_applied_at(initialized(Database(tmp_path / "seeded-again" / "state.sqlite3"))) == seeded
    assert first_applied_at(database_at(tmp_path / "real", real_initialization=True)) != seeded
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target")
    linked = initialized(Database(tmp_path / "link" / "state.sqlite3"))
    assert first_applied_at(linked) != seeded and (tmp_path / "target" / "state.sqlite3").is_file()


def test_historical_migrations_get_a_real_historical_schema_and_never_touch_the_seed(tmp_path, monkeypatch):
    migrations = Database.migrations()
    before = first_applied_at(initialized(Database(tmp_path / "before" / "state.sqlite3")))
    with monkeypatch.context() as old:
        old.setattr(Database, "migrations", staticmethod(lambda: migrations[:21]))
        historical = database_at(tmp_path / "historical")
        with historical.read() as uow:
            assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert first_applied_at(historical) != before
    current = database_at(tmp_path / "current")
    assert shape(current)[2][0] == len(migrations) and first_applied_at(current) == before


def test_patched_migration_hooks_run_through_real_initialization(tmp_path, monkeypatch):
    ran = []
    hook = storage.MIGRATION_HOOKS[100]
    monkeypatch.setitem(storage.MIGRATION_HOOKS, 100, lambda connection: (ran.append(True), hook(connection))[1])
    assert shape(database_at(tmp_path / "hooked"))[2][0] == len(Database.migrations())
    assert ran == [True]


@pytest.mark.parametrize("patched", ["initialize", "migrations", "open_existing", "_connect"])
def test_patched_initialization_inputs_are_never_served_from_the_seed(tmp_path, monkeypatch, patched):
    database = Database(tmp_path / "state.sqlite3")
    assert _fingerprint(database) == tuple((version, checksum) for version, checksum, _ in Database.migrations())
    original = Database.__dict__[patched]
    monkeypatch.setattr(Database, patched, staticmethod(lambda: []) if patched == "migrations" else (lambda *args, **kwargs: None))
    assert _fingerprint(database) is None
    monkeypatch.setattr(Database, patched, original)
    assert _fingerprint(database) is not None


def test_subclass_databases_are_never_served_from_the_seed(tmp_path):
    class Custom(Database):
        pass

    assert _fingerprint(Custom(tmp_path / "state.sqlite3")) is None
