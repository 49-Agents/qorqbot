"""Tables of an extension's own: the statement checker, and the record row that composes the same target."""
import json
from pathlib import Path
import tempfile
import unittest

from qorqut.composition.assembly import HostComposition, host_contributions
from qorqut.composition.extension_tables import check_statements, contribution_of, owner_of, row_of
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
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


if __name__ == "__main__":
    unittest.main()
