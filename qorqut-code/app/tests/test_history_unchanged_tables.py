"""A history snapshot checks and serializes only the tables whose rows changed; the history it writes is the same."""
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut_operations.history_exports import (ExportCatalog, History, HistoryContribution, Integer, TableExport, Text)
from qorqut_operations.history_exports import catalog as catalog_module
from qorqut_operations.history_exports._git import REF


class Database:
    """The read unit a history export needs: read-only, one connection, a composed schema identity."""

    assembly = SimpleNamespace(target=SimpleNamespace(name="test", version=1, digest="0" * 64))

    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def read(self):
        connection = self.connect()
        try:
            yield SimpleNamespace(writable=False, connection=connection, database=self)
        finally:
            connection.close()

    def write(self, sql, *parameters):
        connection = self.connect()
        with connection:
            connection.execute(sql, parameters)
        connection.close()


CATALOG = ExportCatalog((HistoryContribution("test", exports=(
    TableExport("notes", {"id": Text(), "body": Text(), "position": Integer()}, ("id",), shard_hex=1),
    TableExport("tags", {"id": Text(), "label": Text()}, ("id",)),
    TableExport("empty", {"id": Text()}, ("id",)),
)),))


class UnchangedTableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.operations = Path(self.temp.name).resolve() / "operations"
        (self.operations / ".runtime").mkdir(parents=True)
        self.repository = self.operations / ".runtime/history.git"
        self.database = Database(self.operations / ".runtime/operations.sqlite3")
        self.database.write("CREATE TABLE notes(id TEXT PRIMARY KEY, body TEXT NOT NULL, position INTEGER NOT NULL)")
        self.database.write("CREATE TABLE tags(id TEXT PRIMARY KEY, label TEXT NOT NULL)")
        self.database.write("CREATE TABLE empty(id TEXT PRIMARY KEY)")
        for index in range(40):
            self.database.write("INSERT INTO notes VALUES (?,?,?)", f"note-{index:02d}", f"body {index}", index)
        for index in range(5):
            self.database.write("INSERT INTO tags VALUES (?,?)", f"tag-{index}", f"label {index}")
        self.fail_areas = False
        self.history = self.make()
        self.history.initialize()
        checked = []
        real = ExportCatalog._row

        def row(export, source):
            checked.append(export.table)
            return real(export, source)
        patch = mock.patch.object(ExportCatalog, "_row", staticmethod(row))
        patch.start()
        self.addCleanup(patch.stop)
        self.checked = checked

    def make(self):
        def areas(uow):
            if self.fail_areas:
                raise RuntimeError("no areas today")
            return []
        return History(self.database, locations=lambda: (self.operations, self.repository), resolve_areas=lambda selection: (),
                       safe_segment=lambda value: True, catalog=CATALOG, areas=areas, clock=lambda: "2026-01-01T00:00:00Z")

    def snapshot(self, history=None):
        del self.checked[:]
        return (history or self.history).snapshot()

    def git(self, *arguments):
        return subprocess.run(["git", "--git-dir", str(self.repository), *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_an_unchanged_database_is_not_checked_again(self):
        first = self.snapshot()
        self.assertEqual({"notes": 40, "tags": 5}, {name: self.checked.count(name) for name in ("notes", "tags")})
        second = self.snapshot()
        self.assertEqual([], self.checked)
        self.assertFalse(second["changed"])
        self.assertEqual((first["commit"], first["tree"], first["rows"]), (second["commit"], second["tree"], second["rows"]))
        self.assertEqual({"empty": 0, "notes": 40, "tags": 5}, second["rows"])

    def test_only_the_changed_table_is_checked_and_the_history_is_what_a_full_export_writes(self):
        self.snapshot()
        self.database.write("UPDATE tags SET label = 'renamed' WHERE id = 'tag-3'")
        changed = self.snapshot()
        self.assertEqual(["tags"] * 5, self.checked)
        self.assertTrue(changed["changed"])
        self.assertIn("renamed", self.git("show", f"{REF}:database/tags.jsonl"))
        full = self.snapshot(self.make())                    # another object remembers nothing and checks every row
        self.assertEqual(45, len(self.checked))
        self.assertEqual((False, changed["tree"], changed["commit"]), (full["changed"], full["tree"], full["commit"]))

    def test_rows_added_and_removed_change_exactly_their_shard_files(self):
        self.snapshot()
        before = set(self.git("ls-tree", "-r", "--name-only", REF).splitlines())
        self.database.write("DELETE FROM notes")
        self.database.write("INSERT INTO empty VALUES ('now-present')")
        summary = self.snapshot()
        after = set(self.git("ls-tree", "-r", "--name-only", REF).splitlines())
        self.assertEqual({"empty": 1, "notes": 0, "tags": 5}, summary["rows"])
        self.assertEqual({"database/empty.jsonl", "database/manifest.json", "database/tags.jsonl"}, after)
        self.assertTrue(any(path.startswith("database/notes/") for path in before))
        self.assertEqual(["empty"], self.checked)
        self.assertEqual(summary["tree"], self.snapshot(self.make())["tree"])

    def test_a_pass_that_fails_forgets_what_was_remembered(self):
        self.snapshot()
        self.fail_areas = True
        with self.assertRaises(DomainError):
            self.snapshot()
        self.fail_areas = False
        self.snapshot()
        self.assertEqual(45, len(self.checked))
        self.snapshot()
        self.assertEqual([], self.checked)

    def test_files_the_current_commit_does_not_hold_are_made_again(self):
        first = self.snapshot()
        self.database.write("UPDATE tags SET label = 'renamed' WHERE id = 'tag-3'")
        second = self.snapshot()
        # Something else moved the history back: the remembered tag file is not in the commit this pass builds on.
        self.git("update-ref", REF, first["commit"])
        third = self.snapshot()
        self.assertEqual(["tags"] * 5, self.checked)
        self.assertEqual(second["tree"], third["tree"])
        self.assertEqual(first["commit"], third["previous_commit"])

    def test_a_row_order_change_only_costs_a_fresh_check(self):
        first = self.snapshot()
        self.database.write("DELETE FROM tags WHERE id = 'tag-0'")
        self.database.write("INSERT INTO tags VALUES ('tag-0', 'label 0')")    # same rows, another order on disk
        second = self.snapshot()
        self.assertEqual(["tags"] * 5, self.checked)
        self.assertEqual((False, first["tree"]), (second["changed"], second["tree"]))


class FingerprintTests(unittest.TestCase):
    def test_it_depends_on_values_types_and_order_only(self):
        rows = [{"id": "a", "count": 1, "ratio": 1.5, "blob": b"x", "none": None}, {"id": "b", "count": 2}]
        same = [{"id": "".join(["a"]), "count": int("1"), "ratio": 3 / 2, "blob": bytes([120]), "none": None},
                {"id": "b", "count": 2}]
        fingerprint = catalog_module._fingerprint
        self.assertEqual(fingerprint(rows), fingerprint(same))
        self.assertEqual(64, len(fingerprint(rows)))
        different = [
            [{"id": "a", "count": 1.0, "ratio": 1.5, "blob": b"x", "none": None}, {"id": "b", "count": 2}],
            [{"id": "a", "count": True, "ratio": 1.5, "blob": b"x", "none": None}, {"id": "b", "count": 2}],
            [{"id": "a", "count": "1", "ratio": 1.5, "blob": b"x", "none": None}, {"id": "b", "count": 2}],
            [{"count": 1, "id": "a", "ratio": 1.5, "blob": b"x", "none": None}, {"id": "b", "count": 2}],
            [rows[1], rows[0]], rows[:1], [],
        ]
        seen = {fingerprint(rows)}
        for other in different:
            seen.add(fingerprint(other))
        self.assertEqual(len(different) + 1, len(seen))

    def test_many_rows_are_the_same_in_any_chunking(self):
        rows = [{"id": str(index)} for index in range(catalog_module.FINGERPRINT_ROWS * 2 + 7)]
        self.assertEqual(catalog_module._fingerprint(rows), catalog_module._fingerprint([dict(row) for row in rows]))
        self.assertNotEqual(catalog_module._fingerprint(rows), catalog_module._fingerprint(rows[:-1]))

    def test_rows_it_cannot_encode_have_none_and_are_never_unchanged(self):
        self.assertIsNone(catalog_module._fingerprint([{"id": Decimal("1")}]))
        self.assertIsNone(catalog_module._fingerprint([object()]))


if __name__ == "__main__":
    unittest.main()
