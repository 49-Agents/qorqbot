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


class TransitionTest(unittest.TestCase):
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
        for name in ("recognise", "integrity", "backup", "transaction", "open"):
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
        with mock.patch.object(agentic_migration, "_execute_additions", side_effect=sqlite3.OperationalError("fault")), \
                self.assertRaises(DomainError):
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


if __name__ == "__main__":
    unittest.main()
