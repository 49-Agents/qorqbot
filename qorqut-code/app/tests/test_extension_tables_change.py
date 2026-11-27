"""Tables of an extension's own in a staged install, and the case the staged update and removal tests share: the
``tablenote`` example's bundle is built and indexed as a release does, staged by ``extension_change.stage``, applied by
``apply_pending`` as the service's start applies it, and the instance is then started with the real ``build_runtime``.

Nothing of the change or the start is replaced except where a test says so: an interruption is raised at a phase of the
change (``_fault_hook``) or inside the tables' transaction (``_execute_additions``), standing in for a process that
stops there."""
import hashlib
import json
import shutil
import sqlite3
import sys
from unittest import mock

import yaml

from qorqut import extension_index

from test_extension_change import Interrupted, builder, generator
from test_extension_tables_runtime import SHARED, TABLENOTE, TablesRuntimeCase

# The example's one statement, as its code declares it.
FIRST = ("CREATE TABLE tablenote_items (id TEXT PRIMARY KEY NOT NULL, text TEXT NOT NULL, "
         "revision INTEGER NOT NULL DEFAULT 1)")
INDEX = "CREATE INDEX tablenote_items_text ON tablenote_items(text)"
TAGS = "CREATE TABLE tablenote_tags (id TEXT PRIMARY KEY NOT NULL, item TEXT NOT NULL REFERENCES tablenote_items(id))"


class ChangeTablesCase(TablesRuntimeCase):
    def setUp(self):
        super().setUp()
        self.release = self.paths.instance_root.parent / "release"
        shutil.rmtree(self.release, ignore_errors=True)
        self.release.mkdir()
        self.addCleanup(shutil.rmtree, self.release, ignore_errors=True)

    def version(self, version, statements=None):
        """A copy of ``tablenote`` at ``version``; ``statements`` change its list of statements."""
        folder = self.release / version / "tablenote"
        shutil.copytree(TABLENOTE, folder)
        manifest = folder / "extension.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["version"] = version
        manifest.write_text(yaml.safe_dump(data, sort_keys=False))
        if statements is not None:
            code = folder / "code" / "qorqut_tablenote" / "extension.py"
            code.write_text(code.read_text() + f"\nSTATEMENTS = {tuple(statements)!r}\n")
        return folder

    def stage(self, folder):
        from qorqut.bootstrap import extension_change
        out = self.built(folder)
        entry, chosen = extension_index.load(out / extension_index.INDEX_FILE).entry_for("tablenote", "linux-x86_64")
        outcome = extension_change.stage(self.paths, entry, chosen, restart=lambda reason: True)
        self.assertTrue(outcome.staged, outcome.note)
        return extension_change.pending(self.paths)["change_id"]

    def built(self, folder):
        """The folder with the bundle of ``folder`` and its index, built as a release builds them, once per process for
        the same files."""
        digest = hashlib.sha256()
        for path in sorted(item for item in folder.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes() + b"\0")
        out = SHARED["pristine"].parent / "bundles" / digest.hexdigest()
        if not out.is_dir():
            out.mkdir(parents=True)
            bundle = builder.build_folder(folder, "any", out / "tablenote.tar.gz")["path"]
            (out / extension_index.INDEX_FILE).write_text(json.dumps(generator.build_index([folder], [bundle], "1.7.0",
                                                                                           out.as_uri())))
        return out

    def apply(self):
        from qorqut.bootstrap import extension_change
        extension_change.apply_pending(self.paths)
        return extension_change.pending(self.paths)["result"]

    def finish(self):
        from qorqut.bootstrap import extension_change
        return extension_change.finish(self.paths, report=lambda change: None)

    def installed(self):
        return self.paths.installed_extensions / "tablenote"

    def change_copies(self, change_id):
        folder = self.paths.instance_root / "extension-backups" / change_id
        return sorted(path.name for path in folder.glob("*.sqlite3"))

    def tables(self):
        return self.store("SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'tablenote%' ORDER BY type, name")

    def identity(self):
        return self.store("SELECT name, version, digest FROM target_identity")

    def install(self):
        """Version 1.0.0 installed and one row written to its table: done once per process, and the
        instance copied back to that state for every later test."""
        saved = SHARED["pristine"].parent / "installed"
        if saved.is_dir():
            shutil.rmtree(self.paths.instance_root)
            shutil.copytree(saved, self.paths.instance_root, symlinks=True)
            return
        self.stage(self.version("1.0.0"))
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.finish()
        with sqlite3.connect(self.paths.database) as connection:
            connection.execute("INSERT INTO tablenote_items (id, text) VALUES ('a', 'kept')")
        connection.close()
        shutil.copytree(self.paths.instance_root, saved, symlinks=True)


class StagedInstallTest(ChangeTablesCase):
    def test_a_staged_install_makes_the_tables_before_the_folder_is_in_and_an_interrupted_move_finishes_later(self):
        from qorqut.bootstrap import extension_change
        change_id = self.stage(self.version("1.0.0"))

        def stop_before_the_move(phase):
            if phase == "backed-up":
                raise Interrupted(phase)
        with mock.patch.object(extension_change, "_fault_hook", stop_before_the_move), \
                self.assertLogs("qorqut.bootstrap.extension_tables", "WARNING") as logged, self.assertRaises(Interrupted):
            self.apply()
        self.assertIn("extension_tables_made extension=tablenote version=1.0.0 applied=yes", logged.output[0])
        # The tables and the record are in the store; the folder is not in yet; one copy, under the change id.
        self.assertFalse(self.installed().exists())
        self.assertEqual([row[1] for row in self.tables()], ["tablenote_items"])
        self.assertEqual(self.store("SELECT id, version FROM extensiontables"), [("tablenote", "1.0.0")])
        self.assertEqual(len(self.change_copies(change_id)), 1)
        # Nothing of the staged folder was imported into this process: its code was loaded in a process of its own.
        self.assertEqual([name for name in sys.modules if name.startswith("qorqut_tablenote")], [])
        self.assertFalse(any("tablenote" in entry for entry in sys.path))
        # The next start finds the tables made, writes nothing more and moves the folder in.
        before = self.paths.database.read_bytes()
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.assertEqual((self.paths.database.read_bytes(), len(self.change_copies(change_id))), (before, 1))
        self.assertTrue((self.installed() / "extension.yaml").is_file())
        runtime = self.start()
        self.assertEqual(self.entry(runtime, "tablenote")["status"], "ready")
        self.assertEqual(self.count(runtime), 0)

    def test_an_interruption_inside_the_transaction_leaves_the_store_and_a_retry_makes_its_own_copy(self):
        from qorqut.bootstrap import agentic_migration
        change_id = self.stage(self.version("1.0.0"))
        before = self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"), self.identity()
        from test_extension_tables_making import failing_after_the_trial
        with mock.patch.object(agentic_migration, "_execute_additions", failing_after_the_trial(Interrupted("transaction"))), \
                self.assertRaises(Interrupted):
            self.apply()
        self.assertEqual((self.store("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"), self.identity()),
                         before)
        self.assertEqual((len(self.change_copies(change_id)), self.installed().exists()), (1, False))
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.assertEqual(len(self.change_copies(change_id)), 2)
        self.assertEqual(self.entry(self.start(), "tablenote")["status"], "ready")


if __name__ == "__main__":
    import unittest
    unittest.main()
