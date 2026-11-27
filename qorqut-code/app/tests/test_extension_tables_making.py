"""Making the tables of an extension's own: the one transaction (``tables_transition`` through ``_apply_transition``),
the comparison of a declaration with the store's record (``judge``), and ``make`` on a plain store.

The store here is a new store made by ``initialize_target``; the start that runs ``make`` for real is in
``test_extension_tables_runtime.py``."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from qorqut.bootstrap import agentic_migration, extension_tables as making
from qorqut.composition.assembly import host_assembly
from qorqut.composition.extension_tables import (ADDS, SAME, contribution_of, judge, record_contribution, recorded,
                                                 row_of)
from qorqut.extensions import NO_EXTENSIONS
from qorqut.extension_registry import Loaded
from qorqut.instance_paths import InstancePaths
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.specs import TableSpec

from test_extension_tables import ROW, SIMPLE, SIMPLE_INDEX

ITEMS = StorageContribution("tablenote", {"tablenote_items": TableSpec(("id",))}, schema=(SIMPLE,))
WITH_INDEX = StorageContribution("tablenote", {"tablenote_items": TableSpec(("id",))}, schema=(SIMPLE, SIMPLE_INDEX))
TAGS = "CREATE TABLE tablenote_tags (id TEXT PRIMARY KEY, item TEXT REFERENCES tablenote_items(id))"


def objects(path):
    with closing(sqlite3.connect(path)) as connection:
        return sorted(connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall(),
                      key=lambda row: (row[0], row[1])), \
            connection.execute("SELECT name, version, digest FROM target_identity").fetchall()


class JudgeTest(unittest.TestCase):
    def test_a_declaration_is_judged_against_the_record(self):
        first = row_of("tablenote", "1.0.0", (ITEMS,))
        recorded_first = (record_contribution(), contribution_of(first))
        self.assertEqual(judge("tablenote", "1.0.0", (ITEMS,), ()), (ADDS, first))
        self.assertEqual(judge("tablenote", "1.0.0", (), ()), (SAME, None))
        self.assertEqual(judge("tablenote", "1.0.0", (ITEMS,), recorded_first), (SAME, None))
        # An update that adds an index and a table is made; only the new statements run.
        update = (WITH_INDEX, StorageContribution("tablenote", {}, schema=(TAGS,)))
        self.assertEqual(judge("tablenote", "2.0.0", update, recorded_first), (ADDS, row_of("tablenote", "2.0.0", update)))
        # An older version, with fewer statements, leaves the record as it is.
        newer = (record_contribution(), contribution_of(row_of("tablenote", "2.0.0", (WITH_INDEX,))))
        self.assertEqual(judge("tablenote", "1.0.0", (ITEMS,), newer), (SAME, None))
        for declared, note in (
                ((StorageContribution("tablenote", {"tablenote_items": TableSpec(("id",))},
                                      schema=(SIMPLE.replace("label TEXT", "label TEXT NOT NULL"),)),),
                 "its statements are not the ones it made its tables with"),
                ((StorageContribution("tablenote", {"tablenote_items": TableSpec(("label",))}, schema=(SIMPLE,)),),
                 "it changes the specification of a table it made before"),
                ((StorageContribution("tablenote", {"tablenote_items": TableSpec(("label",))},
                                      schema=(SIMPLE, SIMPLE_INDEX)),),
                 "it changes the specification of a table it made before")):
            with self.subTest(note), self.assertRaises(DomainError) as raised:
                judge("tablenote", "2.0.0", declared, recorded_first)
            self.assertIn(note, raised.exception.message)


class JudgeKeepsEverySpecificationTest(unittest.TestCase):
    """Each table the record created keeps its specification and immutable columns exactly, a table without either
    included, whether the declaration adds statements, has the same ones, or is an older version's."""
    ITEMS = "CREATE TABLE tablenote_items (id TEXT PRIMARY KEY, label TEXT)"
    TAGS = "CREATE TABLE tablenote_tags (id TEXT PRIMARY KEY, label TEXT)"
    INDEX = "CREATE INDEX tablenote_items_label ON tablenote_items(label)"
    NEW = "CREATE TABLE tablenote_more (id TEXT PRIMARY KEY)"

    def declare(self, statements, specs=None, immutable=None):
        return (StorageContribution("tablenote", specs if specs is not None else {"tablenote_items": TableSpec(("id",))},
                                    immutable or {}, schema=statements),)

    def test_a_changed_or_new_specification_of_a_recorded_table_is_refused(self):
        record = (record_contribution(), contribution_of(row_of("tablenote", "1", self.declare((self.ITEMS, self.TAGS)))))
        items, tags = TableSpec(("id",)), TableSpec(("label",), mutable=True)
        for name, declared in (
                ("the same statements, a specification for the table that had none",
                 self.declare((self.ITEMS, self.TAGS), {"tablenote_items": items, "tablenote_tags": tags})),
                ("an added index, a specification for the table that had none",
                 self.declare((self.ITEMS, self.TAGS, self.INDEX), {"tablenote_items": items, "tablenote_tags": tags})),
                ("an added index, immutable columns for a table that had none",
                 self.declare((self.ITEMS, self.TAGS, self.INDEX), immutable={"tablenote_items": frozenset({"label"})})),
                ("an added index, the specification of a table taken away",
                 self.declare((self.ITEMS, self.TAGS, self.INDEX), {})),
                ("an older version with another specification",
                 self.declare((self.ITEMS,), {"tablenote_items": TableSpec(("label",))}))):
            with self.subTest(name), self.assertRaises(DomainError) as raised:
                judge("tablenote", "2", declared, record)
            self.assertIn("it changes the specification of a table it made before", raised.exception.message)
        # A new table may come with its specification; an older version with the recorded ones leaves the record.
        added = self.declare((self.ITEMS, self.TAGS, self.NEW), {"tablenote_items": items, "tablenote_more": tags})
        self.assertEqual(judge("tablenote", "2", added, record), (ADDS, row_of("tablenote", "2", added)))
        self.assertEqual(judge("tablenote", "0", self.declare((self.ITEMS,)), record), (SAME, None))


def failing_after_the_trial(error):
    """``_execute_additions`` as it is, except that the second call, the real transaction after the trial and the copy,
    raises ``error``: a fault after the copy."""
    real, calls = agentic_migration._execute_additions, []

    def execute(connection, contributions):
        calls.append(None)
        if len(calls) == 2:
            raise error
        return real(connection, contributions)
    return execute


class PlainStoreCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name).resolve()
        root = folder / "instance"
        (root / "operations" / ".runtime").mkdir(parents=True, mode=0o700)
        components = {name: folder / name for name in ("app", "dashboard")}
        for path in components.values():
            path.mkdir()
        self.paths = InstancePaths(root, components)
        Database(self.paths.database, assembly=host_assembly(ChangeFeed())).initialize_target().close()

    def copies(self):
        folder = self.paths.instance_root / making.BACKUPS / making.AT_START
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


class TransitionTest(PlainStoreCase):
    def test_the_tables_are_made_and_the_record_read_back_from_the_file_composes_the_stored_identity(self):
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
            report = making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)
        self.assertTrue(report["applied"])
        composed = host_assembly(ChangeFeed(), NO_EXTENSIONS, tables=recorded(self.paths.database))
        _, identity = objects(self.paths.database)
        self.assertEqual(identity, [(composed.target.name, composed.target.version, composed.target.digest)])
        Database(self.paths.database, assembly=composed).open_existing().close()
        self.assertEqual(len(self.copies()), 1)
        # Each phase's time is in the log line (finding 9).
        [line] = logged.output
        for name in ("recognise", "trial", "integrity", "backup", "transaction", "open"):
            self.assertIn(f"{name}_seconds=", line)
        # Made again: the store is the target already, so no copy and nothing written.
        before = self.paths.database.read_bytes()
        self.assertFalse(making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)["applied"])
        self.assertEqual((self.paths.database.read_bytes(), len(self.copies())), (before, 1))

    def test_rows_that_read_back_otherwise_roll_the_transaction_back(self):
        before = objects(self.paths.database)
        transition, inside, readback = agentic_migration.tables_transition(NO_EXTENSIONS, (), (ROW,))
        # A row the transaction did not make tables for: what the store records no longer composes the target.
        other = row_of("tableother", "1.0.0", (StorageContribution(
            "tableother", {}, schema=("CREATE TABLE tableother_x (id TEXT PRIMARY KEY)",)),))

        def writes_another_row(connection):
            inside(connection)
            connection.execute("UPDATE extensiontables SET contribution = ? WHERE id = 'tablenote'",
                               (row_of("tablenote", "1.0.0", (ITEMS,))["contribution"],))

        def writes_one_more_row(connection):
            inside(connection)
            connection.execute("INSERT INTO extensiontables (id, version, contribution) VALUES (:id, :version, :contribution)",
                               other)

        for changed in (writes_another_row, writes_one_more_row):
            with self.subTest(changed), self.assertRaises(DomainError):
                agentic_migration.apply_tables(self.paths, transition, inside=changed, readback=readback,
                                               backup=making.copy_for(self.paths, making.AT_START))
            self.assertEqual(objects(self.paths.database), before)
        self.assertEqual(recorded(self.paths.database), ())

    def test_a_fault_after_the_copy_leaves_it_and_the_next_attempt_makes_its_own(self):
        before = objects(self.paths.database)
        with mock.patch.object(agentic_migration, "_execute_additions",
                               failing_after_the_trial(sqlite3.OperationalError("fault"))), self.assertRaises(DomainError):
            making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)
        self.assertEqual((objects(self.paths.database), len(self.copies())), (before, 1))
        self.assertTrue(making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)["applied"])
        self.assertEqual(len(self.copies()), 2)

    def test_an_update_runs_only_its_new_statements_and_keeps_the_rows(self):
        making.make_one(self.paths, NO_EXTENSIONS, "tablenote", row_of("tablenote", "1.0.0", (ITEMS,)))
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute("INSERT INTO tablenote_items (id, label) VALUES ('a', 'kept')")
            connection.commit()
        update = row_of("tablenote", "2.0.0", (WITH_INDEX, StorageContribution("tablenote", {}, schema=(TAGS,))))
        making.make_one(self.paths, NO_EXTENSIONS, "tablenote", update)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            self.assertEqual(connection.execute("SELECT id, label FROM tablenote_items").fetchall(), [("a", "kept")])
            self.assertEqual(connection.execute("SELECT version FROM extensiontables").fetchall(), [("2.0.0",)])
        self.assertEqual(recorded(self.paths.database)[1], contribution_of(update))

    def test_make_refuses_tables_that_clash_and_writes_nothing(self):
        before = objects(self.paths.database)
        clash = row_of("agent", "1.0.0", (StorageContribution(
            "agent", {"agent_profiles": TableSpec(("id",))}, schema=("CREATE TABLE agent_profiles (id TEXT PRIMARY KEY)",)),))
        loaded = {"agent": Loaded(None, (), Path("/nowhere"), clash), "other": Loaded(None, (), Path("/nowhere"))}
        self.assertEqual(making.due(loaded), {"agent": clash})
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            refused = making.make(self.paths, NO_EXTENSIONS, loaded)
        self.assertEqual(list(refused), ["agent"])
        self.assertIn("they do not fit the store", refused["agent"])
        self.assertEqual((objects(self.paths.database), self.copies()), (before, []))
        # With nothing due, nothing is read, locked or written.
        with mock.patch.object(making, "make_one", side_effect=AssertionError("made")):
            self.assertEqual(making.make(self.paths, NO_EXTENSIONS, {"other": loaded["other"]}), {})


class MakingFaultsTest(PlainStoreCase):
    LABELS = "CREATE TABLE tablenote_items (id TEXT PRIMARY KEY, label TEXT)"
    UNIQUE = "CREATE UNIQUE INDEX tablenote_items_label ON tablenote_items(label)"

    def row(self, ext_id, version, *statements):
        return row_of(ext_id, version, (StorageContribution(ext_id, {}, schema=statements),))

    def test_statements_that_do_not_fit_the_rows_cost_no_copy_at_any_start(self):
        making.make_one(self.paths, NO_EXTENSIONS, "tablenote", self.row("tablenote", "1", self.LABELS))
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.executemany("INSERT INTO tablenote_items VALUES (?, 'same')", (("a",), ("b",)))
            connection.commit()
        before, copies = objects(self.paths.database), self.copies()
        due = {"tablenote": Loaded(None, (), Path("/nowhere"), self.row("tablenote", "2", self.LABELS, self.UNIQUE))}
        for _ in range(2):
            with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
                refused = making.make(self.paths, NO_EXTENSIONS, due)
            self.assertEqual(refused, {"tablenote": "Its tables could not be made: this version's changes to its "
                                                    "tables do not fit the data already in them"})
            self.assertIn("code=tables_do_not_fit_rows error=DomainError", logged.output[-1])
            self.assertEqual((objects(self.paths.database), self.copies()), (before, copies))

    def test_one_start_with_several_extensions_due_makes_one_copy(self):
        due = {ext_id: Loaded(None, (), Path("/nowhere"),
                              self.row(ext_id, "1", f"CREATE TABLE {ext_id}_x (id TEXT PRIMARY KEY)"))
               for ext_id in ("tablenote", "tableother", "tablethird")}
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
            self.assertEqual(making.make(self.paths, NO_EXTENSIONS, due), {})
        self.assertEqual(len(logged.output), 3)
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual([item.owner for item in recorded(self.paths.database)][1:],
                         ["extension.tablenote", "extension.tableother", "extension.tablethird"])

    def test_a_copy_that_cannot_be_written_refuses_the_extension_and_leaves_the_store(self):
        folder = self.paths.instance_root / making.BACKUPS
        folder.mkdir(mode=0o500)
        self.addCleanup(folder.chmod, 0o700)
        before = objects(self.paths.database)
        due = {"tablenote": Loaded(None, (), Path("/nowhere"), ROW)}
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
            refused = making.make(self.paths, NO_EXTENSIONS, due)
        self.assertEqual(refused, {"tablenote": "Its tables could not be made: the copy of the database that is made "
                                                "first could not be written"})
        self.assertIn("code=os_error error=PermissionError", logged.output[-1])
        self.assertEqual(objects(self.paths.database), before)

    def test_tables_already_made_are_opened_and_closed_again(self):
        making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)
        closed = []
        original = Database.close

        def close(database):
            closed.append(database.path)
            return original(database)
        with mock.patch.object(Database, "close", close), self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            self.assertFalse(making.make_one(self.paths, NO_EXTENSIONS, "tablenote", ROW)["applied"])
        self.assertEqual(closed, [self.paths.database])


if __name__ == "__main__":
    unittest.main()
