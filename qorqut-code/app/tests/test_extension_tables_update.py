"""A staged update of an extension with tables: one that adds an index and a table keeps the rows, one that changes a
table it made before fails and leaves everything as it was. Staged and applied as in ``test_extension_tables_change.py``,
on the same seeded instance; each start is the real ``build_runtime``."""
import sys

from test_extension_tables_change import FIRST, INDEX, TAGS, ChangeTablesCase


class StagedUpdateTest(ChangeTablesCase):
    def test_an_update_that_adds_an_index_and_a_table_keeps_the_rows(self):
        self.install()
        # A real start first, so version 1.0.0's code folder is on this process's import path, as in the service that
        # stages and applies the update: the process that reads the tables must load the staged code, not that one.
        installed = str((self.installed() / "code").resolve())
        self.start().stop()
        self.assertIn(installed, sys.path)
        self.stage(self.version("2.0.0", (FIRST, INDEX, TAGS)))
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
            self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.assertIn("extension=tablenote version=2.0.0 applied=yes", logged.output[0])
        self.assertEqual(self.store("SELECT id, text FROM tablenote_items"), [("a", "kept")])
        self.assertEqual(self.store("SELECT id, version FROM extensiontables"), [("tablenote", "2.0.0")])
        self.assertEqual([row[1] for row in self.tables()], ["tablenote_items_text", "tablenote_items", "tablenote_tags"])
        runtime = self.start()
        self.assertEqual((self.entry(runtime, "tablenote")["status"], self.entry(runtime, "tablenote")["version"]),
                         ("ready", "2.0.0"))
        self.assertEqual(self.count(runtime), 1)

    def test_an_update_that_changes_its_create_table_fails_and_leaves_everything_as_it_was(self):
        self.install()
        changed = [FIRST.replace("text TEXT NOT NULL", "text TEXT")]
        self.stage(self.version("2.0.0", changed))
        before = (self.tables(), self.store("SELECT * FROM extensiontables"), self.identity(),
                  self.store("SELECT id, text FROM tablenote_items"), (self.installed() / "extension.yaml").read_text())
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            result = self.apply()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["note"], "This version changes tables it made earlier, which an update may not do. "
                                         "The tables it has in the database stay.")
        self.assertEqual((self.tables(), self.store("SELECT * FROM extensiontables"), self.identity(),
                          self.store("SELECT id, text FROM tablenote_items"),
                          (self.installed() / "extension.yaml").read_text()), before)
        from qorqut.bootstrap import extension_change
        self.assertEqual(list(extension_change.staging(self.paths).iterdir()), [])
        runtime = self.start()
        self.assertEqual((self.entry(runtime, "tablenote")["status"], self.entry(runtime, "tablenote")["version"]),
                         ("ready", "1.0.0"))


if __name__ == "__main__":
    import unittest
    unittest.main()
