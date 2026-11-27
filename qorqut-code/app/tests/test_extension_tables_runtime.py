"""Tables of an extension's own in a real start: a new instance seeded as ``qorqut.bootstrap.initialize`` seeds one, then
starts of the real ``build_runtime`` with ``make_tables=True``, as the service starts.

The instance holds the ``network`` example (no code part); a test places the ``tablenote`` example beside it. Each test
starts from the same seeded instance, put back in place before it. Nothing of the start is replaced except where a test
says so: the fault after the copy (``_execute_additions`` raises once) and, for the unfinished install, the journal file
written by hand."""
from contextlib import closing
import importlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest import mock

from qorqut_operations.contracts import plain

from test_extension_runtime import EXAMPLE, RuntimeCase

TABLENOTE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "tablenote"


def rename(folder, ext_id, table):
    """A copy of ``tablenote`` in ``folder`` under another id, whose one table is ``table``."""
    target = folder / ext_id
    shutil.copytree(TABLENOTE, target)
    package = target / "code" / "qorqut_tablenote"
    package.rename(target / "code" / f"qorqut_{ext_id}")
    for path in target.rglob("*"):
        if path.is_file():
            text = path.read_text().replace("tablenote_items", table).replace("tablenote", ext_id)
            path.write_text(text.replace("Table Note", ext_id.title()).replace("TableNote", ext_id.title()))
    (target / "plugin" / "skills" / "tablenote").rename(target / "plugin" / "skills" / ext_id)
    return target


class TablesRuntimeCase(RuntimeCase):
    FOLDER = EXAMPLE

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pristine = cls.paths.instance_root.parent / "pristine"
        shutil.copytree(cls.paths.instance_root, cls.pristine, symlinks=True)

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.paths.instance_root)
        shutil.copytree(self.pristine, self.paths.instance_root, symlinks=True)
        path, modules = list(sys.path), set(sys.modules)

        def restore():
            sys.path[:] = path
            for name in set(sys.modules) - modules:
                if name.split(".")[0].startswith("qorqut_") and name.split(".")[0] not in ("qorqut_operations",
                                                                                          "qorqut_context", "qorqut_agentic"):
                    del sys.modules[name]
            importlib.invalidate_caches()
        self.addCleanup(restore)

    def start(self, before=None):
        from qorqut.service.runtime import build_runtime
        runtime = build_runtime(self.export, paths=self.paths, make_tables=True)
        if before is not None:
            before(runtime)
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime

    def place(self, folder=TABLENOTE):
        shutil.copytree(folder, self.paths.installed_extensions / folder.name)

    def copies(self):
        folder = self.paths.instance_root / "extension-backups" / "tables"
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    def store(self, query):
        with closing(sqlite3.connect(self.paths.database)) as connection:
            return connection.execute(query).fetchall()

    def entry(self, runtime, ext_id):
        listed = plain(self.invoke(runtime, "extensions_list", {}).data)["extensions"]
        return next(item for item in listed if item["id"] == ext_id)

    def count(self, runtime):
        ordubashy = self.created["agents"]["ordubashy"]
        switched = self.invoke(runtime, "extensions_switch", {"extension": "tablenote", "agent_id": ordubashy, "on": True})
        self.assertEqual(switched.status, "ok", plain(switched.error))
        from qorqut_operations.contracts import InvocationOptions, Principal, new_id
        context = runtime.resolver.invocation_context(Principal(ordubashy, "agent"))
        answer = runtime.application.invoke(context, "tablenote_count", {"scope": "global"},
                                            InvocationOptions(idempotency_key=new_id()))
        self.assertEqual(answer.status, "ok", plain(answer.error))
        return plain(answer.data)["count"]


class MakingAtStartTest(TablesRuntimeCase):
    def test_an_instance_without_table_code_takes_no_lock_and_keeps_its_digest(self):
        from qorqut.bootstrap import agentic_migration
        from qorqut.composition.assembly import host_assembly
        from qorqut.service.changes import ChangeFeed
        with mock.patch.object(agentic_migration, "_apply_transition", side_effect=AssertionError("made")), \
                mock.patch("qorqut.service.ownership.InstanceOwnership.acquire", side_effect=AssertionError("locked")):
            from qorqut.service.runtime import build_runtime
            runtime = build_runtime(self.export, paths=self.paths, make_tables=True)
        self.assertEqual(runtime.database.assembly.target, host_assembly(ChangeFeed()).target)
        self.assertEqual((self.copies(), self.store("SELECT name FROM sqlite_master WHERE name = 'extensiontables'")),
                         ([], []))

    def test_the_first_start_makes_the_table_and_the_second_writes_nothing(self):
        from qorqut.bootstrap import agentic_migration
        from qorqut.composition.extension_tables import recorded
        self.place()
        with self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING) as logged:
            runtime = self.start()
        self.assertIn("extension_tables_made extension=tablenote version=1.0.0 applied=yes", logged.output[0])
        self.assertEqual(self.entry(runtime, "tablenote")["status"], "ready")
        self.assertEqual(self.count(runtime), 0)
        self.assertEqual(self.store("SELECT id, version FROM extensiontables"), [("tablenote", "1.0.0")])
        self.assertEqual(len(self.copies()), 1)
        # The stored identity is what the record, read from the file, composes.
        self.assertEqual(runtime.database.assembly.target.digest, self.store("SELECT digest FROM target_identity")[0][0])
        self.assertEqual([item.owner for item in recorded(self.paths.database)], ["extensiontables", "extension.tablenote"])
        runtime.stop()
        record = self.store("SELECT * FROM extensiontables")
        with mock.patch.object(agentic_migration, "_apply_transition", side_effect=AssertionError("made again")):
            again = self.start()
        self.assertEqual(self.entry(again, "tablenote")["status"], "ready")
        self.assertEqual((len(self.copies()), self.store("SELECT * FROM extensiontables")), (1, record))

    def test_a_fault_after_the_copy_leaves_it_needing_attention_and_the_next_start_makes_its_own_copy(self):
        from qorqut.bootstrap import agentic_migration
        self.place()
        with mock.patch.object(agentic_migration, "_execute_additions", side_effect=sqlite3.OperationalError("fault")), \
                self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING):
            runtime = self.start()
        entry = self.entry(runtime, "tablenote")
        self.assertEqual(entry["status"], "needs_attention")
        self.assertTrue(entry["note"].startswith("Its tables could not be made"), entry["note"])
        self.assertEqual((len(self.copies()), self.store("SELECT name FROM sqlite_master WHERE name LIKE 'tablenote%'")),
                         (1, []))
        runtime.stop()
        later = self.start()
        self.assertEqual(self.entry(later, "tablenote")["status"], "ready")
        self.assertEqual((len(self.copies()), self.count(later)), (2, 0))

    def test_a_table_that_clashes_with_a_core_table_leaves_it_needing_attention_and_qorqbot_starts(self):
        variant = rename(self.paths.instance_root.parent / "variants", "agent", "agent_profiles")
        self.addCleanup(shutil.rmtree, variant.parent)
        self.place(variant)
        before = self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        with self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING):
            runtime = self.start()
        entry = self.entry(runtime, "agent")
        self.assertEqual(entry["status"], "needs_attention")
        self.assertTrue(entry["note"].startswith("Its tables could not be made: they do not fit the store"), entry["note"])
        self.assertEqual(self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"), before)
        self.assertEqual(self.copies(), [])

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
    unittest.main()
