"""Starts that do not finish what they began: a fault after the copy of the database leaves the extension needing
attention and the next start makes the tables with a copy of its own, and an unfinished install stops the start before
a staged change is applied. Each start is the real ``build_runtime`` (or the service's ``main``) on the seeded instance
of ``test_extension_tables_runtime.py``; nothing is replaced except the fault (``_execute_additions`` raises once) and
the install journal, written by hand."""
import logging
import sqlite3
from unittest import mock

from qorqut.extension_registry import TABLES_NOT_MADE

from test_extension_tables_runtime import TablesRuntimeCase


class InterruptedStartTest(TablesRuntimeCase):
    def test_a_fault_after_the_copy_leaves_it_needing_attention_and_the_next_start_makes_its_own_copy(self):
        from qorqut.bootstrap import agentic_migration
        self.place()
        from test_extension_tables_making import failing_after_the_trial
        with mock.patch.object(agentic_migration, "_execute_additions",
                               failing_after_the_trial(sqlite3.OperationalError("fault"))), \
                self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING):
            runtime = self.start()
        entry = self.entry(runtime, "tablenote")
        self.assertEqual(entry["status"], "needs_attention")
        self.assertEqual(entry["note"], TABLES_NOT_MADE)
        self.assertEqual((len(self.copies()), self.store("SELECT name FROM sqlite_master WHERE name LIKE 'tablenote%'")),
                         (1, []))
        runtime.stop()
        later = self.start()
        self.assertEqual(self.entry(later, "tablenote")["status"], "ready")
        self.assertEqual((len(self.copies()), self.count(later)), (2, 0))

    def test_an_unfinished_install_stops_the_start_before_a_staged_change(self):
        from qorqut.bootstrap import extension_change
        from qorqut.bootstrap.extension_install import JOURNAL, _save
        from qorqut.service import main as service_main
        self.place()
        self.start().stop()
        self.assertTrue(extension_change.stage_removal(self.paths, "tablenote", restart=lambda reason: None).staged)
        _save(self.paths.instance_root / "config" / JOURNAL, {"format": 1, "phase": "prepared"})
        components = [f"{name}={path}" for name, path in self.paths.components.items()]
        with self.assertLogs("qorqut.service.main", logging.ERROR):
            status = service_main.main(["--workspace", str(self.export), "--instance-root", str(self.paths.instance_root),
                                        *(argument for item in components for argument in ("--component", item))])
        self.assertEqual(status, 1)
        self.assertIsNone(extension_change.pending(self.paths)["result"])
        self.assertTrue((self.paths.installed_extensions / "tablenote").is_dir())


if __name__ == "__main__":
    import unittest
    unittest.main()
