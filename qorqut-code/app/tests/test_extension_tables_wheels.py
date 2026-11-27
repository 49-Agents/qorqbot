"""An extension whose tables come from a module of its dependency wheel: the wheel is built here, the bundle by the real
``build_folder`` (whose load of the code part has the wheel unpacked after ``code/``), the install staged and applied
by ``apply_pending`` (whose process that reads the tables has the staged ``deps/site/``), then the real start. Each
start is the real ``build_runtime`` on the seeded instance of ``test_extension_tables_runtime.py``."""
import contextlib
import io
import json
import logging

from qorqut import extension_index

from test_extension_change import builder, generator
from test_extension_deps import make_wheel
from test_extension_tables_change import ChangeTablesCase

COLUMN = "tiny_column"


class TablesFromAWheelTest(ChangeTablesCase):
    def variant(self):
        folder = self.version("1.0.0")
        code = folder / "code"
        source = code / "qorqut_tablenote" / "extension.py"
        source.write_text(source.read_text() + (
            "\nfrom qorqbot_tiny_dep import COLUMN\n"
            "STATEMENTS = (f'CREATE TABLE {TABLE} (id TEXT PRIMARY KEY NOT NULL, text TEXT NOT NULL, '\n"
            "              f'revision INTEGER NOT NULL DEFAULT 1, {COLUMN} TEXT)',)\n"))
        pyproject = code / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace(
            "[project]\n", '[project]\ndependencies = ["qorqbot-tiny-dep==1.0.0"]\n', 1))
        (code / "constraints.txt").write_text("qorqbot-tiny-dep==1.0.0\n")
        wheels = self.release / "wheels"
        wheels.mkdir()
        return folder, make_wheel(wheels, files={"qorqbot_tiny_dep/__init__.py": f"COLUMN = {COLUMN!r}\n".encode()})

    def test_the_builder_the_staged_install_and_the_start_all_import_the_wheel(self):
        from qorqut import extension_contract as contract
        from qorqut.bootstrap import extension_change
        folder, wheel = self.variant()
        out = self.release / "out"
        out.mkdir()
        bundle = builder.build_folder(folder, "any", out / "tablenote.tar.gz", [wheel])["path"]
        (out / extension_index.INDEX_FILE).write_text(json.dumps(generator.build_index([folder], [bundle], "1.7.0",
                                                                                       out.as_uri())))
        entry, chosen = extension_index.load(out / extension_index.INDEX_FILE).entry_for("tablenote", "linux-x86_64")
        self.assertTrue(extension_change.stage(self.paths, entry, chosen, restart=lambda reason: True).staged)
        with self.assertLogs("qorqut.bootstrap.extension_tables", logging.WARNING):
            self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.finish()
        made = self.store("SELECT sql FROM sqlite_master WHERE name = 'tablenote_items'")[0][0]
        self.assertIn(COLUMN, made)
        runtime = self.start()
        self.assertEqual(self.entry(runtime, "tablenote")["status"], "ready")
        self.assertEqual(self.count(runtime), 0)
        runtime.stop()
        # The same code through --load: the installed folder, whose deps/site/ is unpacked, loads; the source folder,
        # given no wheels, does not.
        for given, status, said in ((self.installed(), 0, None),
                                    (folder, 1, "Its code could not be loaded: ModuleNotFoundError.")):
            with self.subTest(str(given)):
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    self.assertEqual(contract.main(["--load", str(given)]), status, printed.getvalue())
                if said:
                    self.assertIn(said, printed.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
