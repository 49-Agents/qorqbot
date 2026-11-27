"""What a start does with an extension whose tables are not made: one built without the step, one whose copy of the
database cannot be written, and the service's own start refusing a wrong ``--project`` after its runtime is built.

Each start is the real ``build_runtime`` on the seeded instance of ``test_extension_tables_runtime.py``. Nothing is
replaced except where a test says so."""
import logging
from unittest import mock

from qorqut_operations.contracts import plain

from test_extension_tables_runtime import TablesRuntimeCase


class NotMadeTest(TablesRuntimeCase):
    def assertGone(self, runtime, ext_id, tool):
        """A refused code part is out of everything the runtime uses: the selection (its install and its services),
        the loaded code (its set-up handler) and the tool names."""
        self.assertNotIn(ext_id, runtime.selection.ids)
        self.assertNotIn(ext_id, runtime.extension_registry.code)
        self.assertIsNone(runtime.extension_registry.setup_handler(ext_id))
        self.assertIsNone(runtime.extension_registry.extension_of(tool))
        self.assertNotIn(ext_id, [declaration.name.split(".")[0] for declaration in runtime.extension_services()])
        answer = self.invoke(runtime, tool, {})
        self.assertNotEqual(answer.status, "ok", plain(answer.data))

    def test_a_runtime_built_without_the_step_leaves_an_extension_with_tables_to_make_out(self):
        from qorqut.service.runtime import build_runtime
        self.place()
        before = self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        with self.assertLogs("qorqut.extension_registry", logging.WARNING):
            runtime = build_runtime(self.export, paths=self.paths)
        runtime.start()
        self.addCleanup(runtime.stop)
        entry = self.entry(runtime, "tablenote")
        self.assertEqual(entry["status"], "needs_attention")
        self.assertIn("next start", entry["note"])
        self.assertGone(runtime, "tablenote", "tablenote_count")
        self.assertEqual((self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"), self.copies()),
                         (before, []))

    def test_a_copy_that_cannot_be_written_leaves_it_needing_attention_and_qorqbot_starts(self):
        folder = self.paths.instance_root / "extension-backups"
        folder.mkdir(mode=0o500)
        self.addCleanup(folder.chmod, 0o700)
        self.place()
        with self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING) as logged:
            runtime = self.start()
        self.assertIn("code=os_error error=PermissionError", logged.output[0])
        entry = self.entry(runtime, "tablenote")
        self.assertEqual(entry["status"], "needs_attention")
        self.assertIn("copy of the database", entry["note"])
        self.assertGone(runtime, "tablenote", "tablenote_count")
        self.assertEqual(self.store("SELECT name FROM sqlite_master WHERE name LIKE 'tablenote%'"), [])

    def test_a_wrong_project_is_refused_after_the_runtime_is_built_and_the_runtime_is_closed(self):
        from qorqut.service import main as service_main
        from qorqut.service.runtime import Runtime
        stopped = []
        original = Runtime.stop

        def stop(runtime):
            stopped.append(runtime)
            return original(runtime)
        components = [f"{name}={path}" for name, path in self.paths.components.items()]
        with mock.patch.object(Runtime, "stop", stop), self.assertLogs("qorqut.service.main", logging.ERROR) as logged:
            status = service_main.main(["--workspace", str(self.export), "--instance-root", str(self.paths.instance_root),
                                        "--project", str(self.paths.instance_root.parent / "not-a-project"),
                                        *(argument for item in components for argument in ("--component", item))])
        self.assertEqual(status, 1)
        self.assertIn("--project accepts registered project folders only", logged.output[0])
        self.assertEqual(len(stopped), 1)
        self.assertTrue(stopped[0].database._closed)


if __name__ == "__main__":
    import unittest
    unittest.main()
