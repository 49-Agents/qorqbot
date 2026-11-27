"""A staged install or update whose tables are not made, or whose change is interrupted: the change is staged and applied
as in ``test_extension_tables_change.py``, on the same seeded instance, without a start where none is needed.

Nothing is replaced except where a test says so: an interruption at a phase of the change (``_fault_hook``), a
selection that cannot be read (the ``selection`` callable the service passes), and the open after the commit
(``agentic_migration.Database``) failing once."""
import sqlite3
from unittest import mock

from qorqut_operations.contracts import DomainError

from test_extension_change import Interrupted
from test_extension_tables_change import FIRST, INDEX, TAGS, ChangeTablesCase

NO_ANSWER = "Its code could not be loaded: the process that loads it ended without an answer."
KEPT = "The tables it has in the database stay."


class CodeNotLoadedTest(ChangeTablesCase):
    def staged_variant(self, added="", replace=None):
        """Stage version 1.0.0 as built, then change its code in the staged folder: code that loaded where the bundle
        was built and does not load here."""
        from qorqut.bootstrap import extension_change
        change_id = self.stage(self.version("1.0.0"))
        source = next(extension_change.staging(self.paths).iterdir()) / "tablenote" / "code" / "qorqut_tablenote" \
            / "extension.py"
        text = source.read_text()
        source.write_text((replace(text) if replace else text) + added)
        return change_id

    def assertNothingInstalled(self, change_id):
        from qorqut.bootstrap import extension_change
        self.assertFalse(self.installed().exists())
        self.assertEqual(list(extension_change.staging(self.paths).iterdir()), [])
        self.assertEqual((self.change_copies(change_id), self.tables()), ([], []))
        self.assertEqual(self.store("SELECT name FROM sqlite_master WHERE name = 'extensiontables'"), [])

    def test_a_staged_folder_whose_code_does_not_load_fails_and_nothing_is_installed(self):
        change_id = self.staged_variant(replace=lambda text: text.replace("return TableNote()",
                                                                          "raise ZeroDivisionError('not said')"))
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged:
            self.assertEqual(self.apply(), {"state": "failed", "note": "Its code could not be loaded: ZeroDivisionError."})
        self.assertIn("kind=fault error=ZeroDivisionError", logged.output[0])
        self.assertNothingInstalled(change_id)

    def test_a_child_that_crashes_fails_the_change_and_nothing_is_installed(self):
        change_id = self.staged_variant("\nimport os\nos._exit(3)\n")
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            self.assertEqual(self.apply(), {"state": "failed", "note": NO_ANSWER})
        self.assertNothingInstalled(change_id)

    def test_a_selection_that_cannot_be_read_stops_the_start_and_the_change_stays(self):
        from qorqut.bootstrap import extension_change
        self.stage(self.version("1.0.0"))

        def unreadable():
            raise DomainError("invalid_installation", "a configured extension is gone")
        with self.assertRaises(DomainError):
            extension_change.apply_pending(self.paths, unreadable)
        self.assertIsNone(extension_change.pending(self.paths)["result"])
        self.assertTrue(any(extension_change.staging(self.paths).iterdir()))
        self.assertEqual(self.tables(), [])


class FailedAfterTheCommitTest(ChangeTablesCase):
    def test_an_open_that_fails_after_the_commit_says_the_tables_stay(self):
        from qorqut.bootstrap import agentic_migration
        self.stage(self.version("1.0.0"))
        calls = []

        class OpensOnceBadly(agentic_migration.Database):
            def open_existing(self):
                calls.append(self.path)
                raise DomainError("invalid_database", "not opened")
        with mock.patch.object(agentic_migration, "Database", OpensOnceBadly), \
                self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            result = self.apply()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["note"].endswith(KEPT), result["note"])
        self.assertEqual([row[1] for row in self.tables()], ["tablenote_items"])
        self.assertFalse(self.installed().exists())

    def test_a_retry_whose_code_then_does_not_load_says_the_tables_stay(self):
        from qorqut.bootstrap import extension_change
        self.stage(self.version("1.0.0"))

        def stop_before_the_move(phase):
            if phase == "backed-up":
                raise Interrupted(phase)
        with mock.patch.object(extension_change, "_fault_hook", stop_before_the_move), \
                self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"), self.assertRaises(Interrupted):
            self.apply()
        # Before the next start the staged code no longer loads: its process ends without an answer.
        staged = next(extension_change.staging(self.paths).iterdir()) / "tablenote"
        source = staged / "code" / "qorqut_tablenote" / "extension.py"
        source.write_text(source.read_text() + "\nimport os\nos._exit(3)\n")
        with self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"):
            self.assertEqual(self.apply(), {"state": "failed", "note": f"{NO_ANSWER} {KEPT}"})
        self.assertEqual([row[1] for row in self.tables()], ["tablenote_items"])


class InterruptedUpdateTest(ChangeTablesCase):
    def test_an_update_interrupted_after_its_tables_and_the_backup_finishes_at_the_next_start(self):
        from qorqut.bootstrap import extension_change
        self.stage(self.version("1.0.0"))
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.finish()
        with sqlite3.connect(self.paths.database) as connection:
            connection.execute("INSERT INTO tablenote_items (id, text) VALUES ('a', 'kept')")
        connection.close()
        change_id = self.stage(self.version("2.0.0", (FIRST, INDEX, TAGS)))

        def stop_before_the_move(phase):
            if phase == "backed-up":
                raise Interrupted(phase)
        with mock.patch.object(extension_change, "_fault_hook", stop_before_the_move), \
                self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING"), self.assertRaises(Interrupted):
            self.apply()
        backup = self.paths.instance_root / "extension-backups" / change_id / "tablenote"
        self.assertIn("version: 1.0.0", (backup / "extension.yaml").read_text())
        self.assertFalse(self.installed().exists())
        self.assertEqual(self.store("SELECT id, version FROM extensiontables"), [("tablenote", "2.0.0")])
        before = self.paths.database.read_bytes()
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.assertEqual(self.paths.database.read_bytes(), before)
        self.assertIn("version: 2.0.0", (self.installed() / "extension.yaml").read_text())
        self.assertEqual(self.store("SELECT id, text FROM tablenote_items"), [("a", "kept")])
        self.assertEqual(len(self.change_copies(change_id)), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
