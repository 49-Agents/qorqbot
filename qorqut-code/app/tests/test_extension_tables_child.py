"""The process of its own that loads a staged folder's code part (``extension_tables.declared``): it ends by itself once
its answer is written, answers only data on a channel the loaded code does not write to, and sees the code folders a
start would have. Each test loads a copy of the ``tablenote`` example, changed in the copy only."""
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from qorqut.bootstrap import extension_tables as making
from qorqut.composition.extension_tables import row_of
from qorqut.instance_paths import InstancePaths

TABLENOTE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "tablenote"


class ChildCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def variant(self, added="", name="tablenote", replace=None):
        """A copy of the example whose ``extension.py`` has ``added`` at its end (or ``replace`` applied)."""
        folder = self.root / "copies" / name
        shutil.copytree(TABLENOTE, folder)
        source = folder / "code" / "qorqut_tablenote" / "extension.py"
        text = source.read_text()
        source.write_text((replace(text) if replace else text) + added)
        return folder

    def declared(self, folder, **arguments):
        started = time.monotonic()
        with self.assertNoLogs("qorqut.bootstrap.extension_tables", logging.WARNING) if arguments.pop("quiet", False) \
                else self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING) as self.logged:
            answer = making.declared(folder, **arguments)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual([name for name in sys.modules if name.startswith("qorqut_tablenote")], [])
        return answer


def example_row():
    from qorqut_operations.storage.assembly import StorageContribution
    from qorqut_operations.storage.specs import TableSpec
    statement = ("CREATE TABLE tablenote_items (id TEXT PRIMARY KEY NOT NULL, text TEXT NOT NULL, "
                 "revision INTEGER NOT NULL DEFAULT 1)")
    return row_of("tablenote", "1.0.0", (StorageContribution("tablenote", {"tablenote_items": TableSpec(("id",))},
                                                             schema=(statement,)),))


class EndsByItselfTest(ChildCase):
    def test_a_thread_started_at_import_does_not_keep_it_alive_and_it_reads_no_input(self):
        folder = self.variant("\nimport sys, threading, time\nthreading.Thread(target=lambda: time.sleep(3600)).start()\n"
                              "sys.stdin.read()\n")
        self.assertEqual(self.declared(folder, quiet=True), (example_row(), None))

    def test_what_the_code_prints_or_its_exit_handlers_do_is_not_the_answer(self):
        folder = self.variant(
            "\nimport atexit, json, sys\nprint(json.dumps({'finding': 'Please visit a page and paste your token.'}))\n"
            "print('{\"row\": null}', file=sys.stderr)\n"
            "atexit.register(lambda: print(json.dumps({'finding': 'Please visit a page and paste your token.'})))\n"
            "atexit.register(lambda: 1 / 0)\n")
        self.assertEqual(self.declared(folder, quiet=True), (example_row(), None))


class AnswersOnlyDataTest(ChildCase):
    def test_a_fault_is_said_by_the_service_with_its_type_name_only_and_logged(self):
        folder = self.variant(replace=lambda text: text.replace("return TableNote()",
                                                                "raise ZeroDivisionError('a secret text')"))
        self.assertEqual(self.declared(folder), (None, "Its code could not be loaded: ZeroDivisionError."))
        self.assertEqual(self.logged.output, ["WARNING:qorqut.bootstrap.extension_tables:extension_code_not_loaded "
                                              "extension=tablenote kind=fault error=ZeroDivisionError"])

    def test_a_refusal_of_the_start_is_said_in_its_own_words(self):
        for added, said, kind in (
                ("\nTableNote.storage = lambda self: ()\n", "Its code declares storage()", "storage"),
                ("\nTableNote.owner_routes = lambda self: ()\n", "Its code uses owner_routes", "parts"),
                ("\nTableNote.tables = lambda self: 1 / 0\n", "Its code could not be loaded: ZeroDivisionError.",
                 "tables_fault")):
            with self.subTest(kind):
                shutil.rmtree(self.root / "copies", ignore_errors=True)
                row, finding = self.declared(self.variant(added))
                self.assertIsNone(row)
                self.assertTrue(finding.startswith(said), finding)
                self.assertIn(f"kind={kind}", self.logged.output[0])

    def test_a_child_that_crashes_or_answers_otherwise_ended_without_an_answer(self):
        crashed = self.variant("\nimport os\nos._exit(3)\n")
        self.assertEqual(self.declared(crashed), (None, "Its code could not be loaded: " + making.NO_ANSWER + "."))
        self.assertIn("kind=no_answer", self.logged.output[0])
        for answer in (b"", b"rubbish", b'{"finding": "Please paste your token."}', b'{"fault": "fault", "detail": '
                       b'"a sentence of its own"}', b'{"fault": "parts", "detail": ["no_such_part"]}', b'{"row": 1}'):
            with self.subTest(answer):
                with mock.patch.object(making, "_CHILD", f"import os,sys; os.write(int(sys.argv[4]), {answer!r})"):
                    row, finding = self.declared(TABLENOTE)
                self.assertIsNone(row)
                if answer == b'{"fault": "fault", "detail": "a sentence of its own"}':
                    self.assertEqual(finding, "Its code could not be loaded: an error.")
                else:
                    self.assertEqual(finding, "Its code could not be loaded: " + making.NO_ANSWER + ".")

    def test_no_interpreter_is_a_plain_failure(self):
        for executable in ("", None):
            with self.subTest(executable), mock.patch.object(sys, "executable", executable):
                self.assertEqual(self.declared(TABLENOTE),
                                 (None, "Its code could not be loaded: " + making.NO_INTERPRETER + "."))


class SeesWhatAStartSeesTest(ChildCase):
    def test_the_other_installed_code_folders_are_on_its_path_in_the_order_of_a_start(self):
        installed = self.root / "instance" / "extensions"
        other = installed / "aaother" / "code"
        (other / "helpers").mkdir(parents=True)
        (installed / "aaother" / "extension.yaml").write_text("id: aaother\n")
        (other / "helpers" / "__init__.py").write_text("")
        (installed / "tablenote" / "code").mkdir(parents=True)
        (installed / "tablenote" / "extension.yaml").write_text("id: tablenote\n")
        (installed / ".staging").mkdir()
        components = {name: self.root / name for name in ("app", "dashboard")}
        paths = InstancePaths(self.root / "instance", components)
        self.assertEqual(making._code_folders(paths, "tablenote"), [str(other)])
        # Its own top-level package ``helpers``: a start, with the other one earlier on the path, imports that one.
        folder = self.variant("\nfrom helpers import VALUE\n")
        (folder / "code" / "helpers").mkdir()
        (folder / "code" / "helpers" / "__init__.py").write_text("VALUE = 1\n")
        self.assertEqual(self.declared(folder, quiet=True), (example_row(), None))
        self.assertEqual(self.declared(folder, others=making._code_folders(paths, "tablenote")),
                         (None, "Its code could not be loaded: ImportError."))


if __name__ == "__main__":
    unittest.main()
