"""Tables of an extension's own: the statement checker, the record row that composes the same target, and reading the
record from a store.

The helpers at the top also serve ``test_extension_tables_openers.py``. Writing a record into a store is not product code
yet, so ``store_with_record`` writes it here, as a test helper."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from qorqut.composition import extension_tables
from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.composition.extension_tables import (check_statements, contribution_of, history, owner_of, record_contribution,
                                                 recorded, row_of)
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.specs import TableSpec

ITEMS = ("CREATE TABLE tablenote_items (id TEXT PRIMARY KEY, label TEXT NOT NULL, parent TEXT "
         "REFERENCES tablenote_items(id), revision INTEGER NOT NULL DEFAULT 1)")
INDEX = "CREATE INDEX tablenote_items_label ON tablenote_items(label) WHERE label IS NOT NULL"
TRIGGER = ("CREATE TRIGGER tablenote_items_guard BEFORE UPDATE OF label ON tablenote_items BEGIN "
           "SELECT RAISE(ABORT, 'empty label') WHERE NEW.label = ''; "
           "UPDATE tablenote_items SET parent = NULL WHERE parent = OLD.id AND NEW.id <> OLD.id; END")


def declaration(owner="tablenote"):
    return StorageContribution(owner, {"tablenote_items": TableSpec(("id",))}, {"tablenote_items": frozenset({"id"})},
                               (ITEMS, INDEX, TRIGGER))


SIMPLE = "CREATE TABLE tablenote_items (id TEXT PRIMARY KEY, label TEXT, revision INTEGER NOT NULL DEFAULT 1)"
SIMPLE_INDEX = "CREATE INDEX tablenote_items_label ON tablenote_items(label)"
ROW = row_of("tablenote", "1.0.0", (StorageContribution("tablenote", {"tablenote_items": TableSpec(("id",))},
                                                       schema=(SIMPLE, SIMPLE_INDEX)),))
ATTACHED = "attached.sqlite3"


def contributions(rows=(ROW,)):
    """What ``recorded`` gives for a store holding ``rows``."""
    return (record_contribution(), *(contribution_of(row) for row in rows))


def write_rows(path, rows):
    with closing(sqlite3.connect(path)) as connection:
        connection.executemany("INSERT INTO extensiontables (id, version, contribution) VALUES (:id, :version, :contribution)",
                               rows)
        connection.commit()


def store_with_record(path, assembly, rows=(ROW,)):
    """A new store for ``assembly`` (composed with ``contributions(rows)``) with the rows written into its record."""
    Database(path, assembly=assembly).initialize_target().close()
    write_rows(path, rows)


def edit_row(path, contribution, ext_id="tablenote"):
    """Replace the recorded contribution text of one row, as an edit by hand would."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE extensiontables SET contribution = ? WHERE id = ?", (contribution, ext_id))
        connection.commit()


def attaching(folder):
    """A canonical recorded contribution whose second statement attaches a file in ``folder``."""
    payload = json.loads(ROW["contribution"])
    payload["statements"] = [SIMPLE, f"ATTACH DATABASE '{Path(folder) / ATTACHED}' AS other"]
    return canonical_bytes(payload).decode()


def digest(*extra):
    composition = HostComposition()
    for contribution in (*host_contributions(ChangeFeed()), *extra):
        composition.add(contribution)
    return composition.freeze().target.digest


class StatementCheckTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()

    def refused(self, statements, note):
        with self.assertRaises(DomainError) as raised:
            check_statements("tablenote", statements)
        self.assertEqual(raised.exception.code, "invalid_installation")
        self.assertIn(note, raised.exception.message)
        self.assertEqual(list(self.folder.iterdir()), [])
        return raised.exception.message

    def test_a_plain_table_index_and_trigger_pass(self):
        self.assertEqual(check_statements("tablenote", (ITEMS, INDEX, TRIGGER)), {
            ("table", "tablenote_items"): "tablenote_items", ("index", "tablenote_items_label"): "tablenote_items",
            ("trigger", "tablenote_items_guard"): "tablenote_items"})
        # An automatic index and the AUTOINCREMENT sequence are part of the one table a statement creates.
        self.assertEqual(set(check_statements("tablenote", (
            "CREATE TABLE tablenote_seq (n INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE)",))),
            {("table", "tablenote_seq")})

    def test_every_refused_form_is_stopped_before_it_runs_and_no_file_appears(self):
        attached, copied = self.folder / "attached.sqlite3", self.folder / "copy.sqlite3"
        own = "CREATE TABLE tablenote_items (id TEXT PRIMARY KEY)"
        cases = {
            "ATTACH": [f"ATTACH DATABASE '{attached}' AS other"],
            "VACUUM INTO": [own, f"VACUUM INTO '{copied}'"],
            "PRAGMA": ["PRAGMA user_version = 7"],
            "CREATE TABLE AS SELECT FROM tasks": ["CREATE TABLE tablenote_copy AS SELECT * FROM tasks"],
            "CREATE TABLE AS SELECT from its own": [own, "CREATE TABLE tablenote_copy AS SELECT * FROM tablenote_items"],
            "IF NOT EXISTS over an existing name": [own, "CREATE TABLE IF NOT EXISTS tablenote_items (other TEXT)"],
            "a view": ["CREATE VIEW tablenote_view AS SELECT 1"],
            "a temporary table": ["CREATE TEMP TABLE tablenote_scratch (id TEXT)"],
            "a virtual table": ["CREATE VIRTUAL TABLE tablenote_search USING fts5(body)"],
            "a trigger on tasks": ["CREATE TRIGGER tablenote_watch AFTER INSERT ON tasks BEGIN SELECT 1; END"],
            "a trigger on its own table that deletes from tasks": [
                own, "CREATE TRIGGER tablenote_clear AFTER DELETE ON tablenote_items BEGIN "
                     "DELETE FROM tasks WHERE id = OLD.id; END"],
            "REFERENCES tasks": ["CREATE TABLE tablenote_links (id TEXT PRIMARY KEY, task TEXT REFERENCES tasks(id))"],
            "an index named outside the prefix": [own, "CREATE INDEX other_index ON tablenote_items(id)"],
            "an insert": [own, "INSERT INTO tablenote_items VALUES ('seed')"],
            "a transaction": ["BEGIN"],
            "a table named outside the prefix": ["CREATE TABLE notes (id TEXT)"],
            "two statements in one string": [own + "; CREATE TABLE tablenote_more (id TEXT)"],
        }
        notes = {
            "ATTACH": "it holds ATTACH", "VACUUM INTO": "statement 2: it holds ATTACH", "PRAGMA": "it holds PRAGMA",
            "CREATE TABLE AS SELECT FROM tasks": "statement 1", "CREATE TABLE AS SELECT from its own": "it holds a SELECT",
            "IF NOT EXISTS over an existing name": "does not create exactly the one object it names",
            "a view": "it holds a view", "a temporary table": "it holds a temporary object",
            "a virtual table": "it holds a virtual table", "a trigger on tasks": "statement 1",
            "a trigger on its own table that deletes from tasks": "a write to tablenote_items",
            "REFERENCES tasks": "refers to tasks", "an index named outside the prefix": "is not named tablenote_",
            "an insert": "it holds an insert", "a transaction": "it holds a transaction",
            "a table named outside the prefix": "is not named tablenote_",
            "two statements in one string": "statement 1",
        }
        for name, statements in cases.items():
            with self.subTest(name):
                self.refused(statements, notes[name])

    def test_a_trigger_body_is_checked_as_it_would_fire_with_the_other_tables_present(self):
        # The body is authorized when a write that fires it is prepared, so a body that writes to another extension's
        # table (which cannot exist beside it in the check) is refused as well.
        self.refused(["CREATE TABLE tablenote_items (id TEXT PRIMARY KEY)",
                      "CREATE TRIGGER tablenote_copy AFTER INSERT ON tablenote_items BEGIN "
                      "INSERT INTO other_items(id) VALUES (NEW.id); END"], "a write to tablenote_items")

    def test_the_id_and_the_statements_must_be_given(self):
        for ext_id, statements in (("Tablenote", (ITEMS,)), ("tablenote", ()), ("tablenote", ("",)),
                                   ("tablenote", (None,))):
            with self.subTest(ext_id=ext_id, statements=statements), self.assertRaises(DomainError):
                check_statements(ext_id, statements)


class RecordRowTest(unittest.TestCase):
    def test_a_row_turned_back_builds_the_same_target_digest(self):
        canonical = contribution_of(row_of("tablenote", "1.0.0", (declaration(owner_of("tablenote")),)))
        self.assertEqual(digest(canonical), digest(declaration(owner_of("tablenote"))))
        # A declaration under another owner name is recorded under the extension's own owner.
        self.assertEqual(contribution_of(row_of("tablenote", "1.0.0", (declaration(),))), canonical)
        self.assertNotEqual(digest(declaration()), digest(canonical))

    def test_the_row_is_canonical_json(self):
        row = row_of("tablenote", "1.2.0", (declaration(),))
        self.assertEqual(set(row), {"id", "version", "contribution"})
        self.assertEqual((row["id"], row["version"]), ("tablenote", "1.2.0"))
        self.assertEqual(json.loads(row["contribution"]), {
            "format": 1, "statements": [ITEMS, INDEX, TRIGGER],
            "tables": {"tablenote_items": {"keys": ["id"], "scope_column": None, "mutable": True,
                                           "revision_column": "revision"}},
            "immutable": {"tablenote_items": ["id"]}})
        self.assertEqual(row_of("tablenote", "1.2.0", (declaration(),)), row)

    def test_a_row_that_is_not_exactly_what_row_of_writes_is_refused(self):
        row = row_of("tablenote", "1.0.0", (declaration(),))
        payload = json.loads(row["contribution"])
        edits = {
            "spaces": json.dumps(payload),
            "another format": json.dumps({**payload, "format": 2}, sort_keys=True, separators=(",", ":")),
            "an unknown key": json.dumps({**payload, "extra": 1}, sort_keys=True, separators=(",", ":")),
            "a table its statements do not create": json.dumps(
                {**payload, "tables": {**payload["tables"], "tablenote_other": payload["tables"]["tablenote_items"]}},
                sort_keys=True, separators=(",", ":")),
            "ATTACH": json.dumps({**payload, "statements": ["ATTACH DATABASE 'x.sqlite3' AS other"]},
                                 sort_keys=True, separators=(",", ":")),
            "not JSON": "{",
        }
        for name, text in edits.items():
            with self.subTest(name), self.assertRaises(DomainError) as raised:
                contribution_of({**row, "contribution": text})
            self.assertEqual(raised.exception.code, "invalid_installation")
        for broken in ({"id": "tablenote", "version": "1.0.0"}, {**row, "id": "Tablenote"}, {**row, "version": ""}):
            with self.subTest(broken=broken), self.assertRaises(DomainError):
                contribution_of(broken)

    def test_only_tables_immutable_columns_and_statements_are_recorded(self):
        for contribution in (StorageContribution("tablenote", schema=(ITEMS,), validators=(lambda connection: None,)),
                             StorageContribution("tablenote", schema=(ITEMS,), required_tables=frozenset({"tasks"}))):
            with self.subTest(contribution=contribution), self.assertRaises(DomainError):
                row_of("tablenote", "1.0.0", (contribution,))



class RecordedTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        self.path = self.folder / "operations.sqlite3"

    def files(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.folder.iterdir()}

    def test_a_store_without_the_record_composes_today_s_assembly_writes_nothing_and_reads_one_query(self):
        from test_migration_upgrade import SyntheticExtension
        from qorqut.extensions import ExtensionSet
        for extensions in (NO_EXTENSIONS, ExtensionSet((SyntheticExtension(),))):
            with self.subTest(extensions=extensions.ids):
                today = host_assembly(ChangeFeed(), extensions)
                Database(self.path, assembly=today).initialize_target().close()
                before = self.files()
                statements = []
                connect = sqlite3.connect

                def traced(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    connection.set_trace_callback(statements.append)
                    return connection

                with mock.patch.object(extension_tables.sqlite3, "connect", traced):
                    self.assertEqual(recorded(self.path), ())
                self.assertEqual(statements, ["PRAGMA query_only = ON",
                                              "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'extensiontables'"])
                self.assertEqual(host_assembly(ChangeFeed(), extensions, tables=recorded(self.path)).target, today.target)
                # The store itself is unchanged. Like ``open_existing``, which reads it the same way next, the read-only
                # connection may leave SQLite's own -wal and -shm files of a WAL store, and no other file.
                after = self.files()
                self.assertEqual(after[self.path.name], before[self.path.name])
                copy = self.folder / "copy" / self.path.name
                copy.parent.mkdir()
                copy.write_bytes(self.path.read_bytes())
                Database(copy, assembly=today).open_existing().close()
                self.assertEqual(set(after), {path.name for path in copy.parent.iterdir()})
                for path in self.folder.iterdir():
                    if path.is_dir():
                        for item in path.iterdir():
                            item.unlink()
                        path.rmdir()
                    else:
                        path.unlink()
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.set_trace_callback(statements.append)
                    statements.clear()
                    self.assertEqual(recorded(connection), ())
                    self.assertEqual(len(statements), 1)

    def test_a_store_with_the_record_gives_the_record_then_each_row(self):
        assembly = host_assembly(ChangeFeed(), tables=contributions())
        store_with_record(self.path, assembly)
        self.assertEqual(recorded(self.path), contributions())
        self.assertEqual([owner for owner, _ in assembly.schema][-2:], ["extensiontables", "extension.tablenote"])
        self.assertEqual(Database(self.path, assembly=assembly).open_existing().assembly.target, assembly.target)
        # An empty record is the record table alone.
        empty = self.folder / "empty.sqlite3"
        store_with_record(empty, host_assembly(ChangeFeed(), tables=contributions(())), ())
        self.assertEqual(recorded(empty), (record_contribution(),))

    def test_a_row_that_fails_the_check_refuses_the_store_and_nothing_runs(self):
        store_with_record(self.path, host_assembly(ChangeFeed(), tables=contributions()))
        edit_row(self.path, attaching(self.folder))
        with self.assertRaises(DomainError) as raised:
            recorded(self.path)
        self.assertEqual(raised.exception.code, "invalid_database")
        self.assertIn("ATTACH", raised.exception.message)
        self.assertFalse((self.folder / ATTACHED).exists())

    def test_a_row_edited_by_hand_is_refused(self):
        assembly = host_assembly(ChangeFeed(), tables=contributions())
        store_with_record(self.path, assembly)
        payload = json.loads(ROW["contribution"])
        # Not in canonical form: refused while reading the record.
        edit_row(self.path, json.dumps(payload, indent=1))
        with self.assertRaises(DomainError) as raised:
            recorded(self.path)
        self.assertEqual(raised.exception.code, "invalid_database")
        # Canonical, and its statements pass the check, but it is not what the store was made with: the identity differs.
        payload["statements"] = [SIMPLE.replace("label TEXT", "label TEXT, note TEXT"), SIMPLE_INDEX]
        edit_row(self.path, canonical_bytes(payload).decode())
        edited = host_assembly(ChangeFeed(), tables=recorded(self.path))
        with self.assertRaises(DomainError) as raised:
            Database(self.path, assembly=edited).open_existing()
        self.assertEqual(raised.exception.code, "invalid_database")
        # An id edited by hand names an owner whose objects are not its own.
        edit_row(self.path, ROW["contribution"])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE extensiontables SET id = 'othernote'")
            connection.commit()
        with self.assertRaises(DomainError):
            recorded(self.path)

    def test_a_link_is_refused_as_open_existing_refuses_it(self):
        store_with_record(self.path, host_assembly(ChangeFeed(), tables=contributions()))
        link = self.folder / "link.sqlite3"
        link.symlink_to(self.path)
        linked_folder = self.folder / "linked"
        linked_folder.symlink_to(self.folder)
        for path in (link, linked_folder / self.path.name):
            with self.subTest(path=path), self.assertRaises(DomainError) as raised:
                recorded(path)
            self.assertEqual(raised.exception.code, "invalid_database")
        self.assertEqual(recorded(self.folder / "missing.sqlite3"), ())

    def test_pending_wal_content_and_a_writer_holding_the_store(self):
        store_with_record(self.path, host_assembly(ChangeFeed(), tables=contributions(())), ())
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO extensiontables (id, version, contribution) VALUES (?, ?, ?)",
                       (ROW["id"], ROW["version"], ROW["contribution"]))
        self.assertTrue(Path(str(self.path) + "-wal").stat().st_size > 0)
        # Committed but not yet checkpointed: read from the WAL.
        self.assertEqual(recorded(self.path), contributions())
        # A writer in the middle of a transaction does not hold the read up, and its uncommitted row is not seen.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM extensiontables")
        self.assertEqual(recorded(self.path), contributions())
        writer.execute("ROLLBACK")

    def test_an_older_store_and_a_file_that_is_not_a_store(self):
        from qorqut.migration import upgrade
        Database(self.path, assembly=upgrade.chain()[0][0]).initialize_target().close()
        self.assertEqual(recorded(self.path), ())
        other = self.folder / "other.sqlite3"
        other.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(DomainError) as raised:
            recorded(other)
        self.assertEqual(raised.exception.code, "invalid_database")

    def test_the_record_and_the_recorded_tables_are_classified_for_the_history(self):
        from qorqut.operations_history.catalog import catalog
        self.assertEqual(history(host_assembly(ChangeFeed())), ())
        assembly = host_assembly(ChangeFeed(), tables=contributions())
        classified = catalog(history(assembly)).classifications
        self.assertEqual({name: item.kind for name, (_, item) in classified.items() if name in ("extensiontables", "tablenote_items")},
                         {"extensiontables": "private_excluded", "tablenote_items": "private_excluded"})
        store_with_record(self.path, assembly)
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite%'")}
        self.assertLessEqual({"extensiontables", "tablenote_items"}, set(catalog(history(assembly)).tables) & tables)
        self.assertEqual(tables - set(catalog(history(assembly)).tables), tables - set(catalog().tables) - {
            "extensiontables", "tablenote_items"})


    def test_the_runtime_history_is_bound_with_those_classifications(self):
        from types import SimpleNamespace
        from qorqut.operations_history import composition
        for assembly in (host_assembly(ChangeFeed()), host_assembly(ChangeFeed(), tables=contributions())):
            runtime = SimpleNamespace(database=SimpleNamespace(assembly=assembly), paths=None, selection=NO_EXTENSIONS,
                                      changes=SimpleNamespace(add_commit_listener=lambda listener: None))
            with mock.patch.object(composition, "history_for", return_value=SimpleNamespace(snapshot=lambda: None)) as bound:
                composition.bind(runtime)
            self.assertEqual(bound.call_args.args[2], history(assembly))


if __name__ == "__main__":
    unittest.main()
