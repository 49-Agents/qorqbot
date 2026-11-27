"""The release rules that keep extension table names and core table names apart (``tools/distribution/extension_names.py``):
core names come from the app's own composition, ids from index files."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from qorqut_operations.storage.assembly import StorageContribution

TOOL = Path(__file__).resolve().parents[1].parent / "tools" / "distribution" / "extension_names.py"


def load_tool():
    # Loaded under a name of its own and not kept in sys.modules, so nothing is left behind for other test modules.
    spec = importlib.util.spec_from_file_location("tool_extension_names", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(ext_id):
    return {"id": ext_id, "version": "1.0.0", "name": ext_id, "summary": "A test extension.", "requires_qorqbot": ">=1.0.0",
            "platforms": ["linux"], "setup_kind": "none", "requires_extensions": [], "folder_sha256": "0" * 64,
            "bundles": [{"platform": "any", "url": f"https://example.invalid/{ext_id}.tar.gz", "sha256": "1" * 64,
                         "size": 1}]}


class ExtensionNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()
        cls.core = cls.tool.core_names()

    def index(self, *ids):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "extensions-index.json"
        path.write_text(json.dumps({"format": 1, "app_version": "1.7.0", "extensions": [entry(i) for i in ids]}))
        return path

    def test_the_core_names_are_the_composed_store_with_no_extension(self):
        self.assertIn("agent_activity", self.core)
        self.assertIn("target_identity", self.core)
        self.assertEqual(self.tool.clashes(["network", "keyed"], ["tablenote"], self.core), [])

    def test_the_names_of_the_extensions_the_app_ships_are_names_an_id_is_checked_against(self):
        # The release tools' own lists: the optional connectors and the extensions the app's bundle carries.
        factories = self.tool.shipped_factories()
        self.assertIn("qorqut_telegram.extension:create", factories)
        self.assertIn("qorqut_whatsapp.extension:create", factories)
        # Telegram's own tables are composed from this checkout's package, in a process of its own.
        telegram = [name for name in self.core if name.startswith("telegram_")]
        self.assertIn("telegram_events", telegram)
        self.assertEqual(self.tool.taken_prefix("telegram", self.core), telegram[0])
        self.assertTrue(self.tool.clashes([], ["telegram"], self.core)[0].startswith("Rule (b): the new extension id "
                                                                                      "telegram cannot be used"))
        self.assertEqual([name for name in sys.modules if name.startswith("qorqut_telegram")], [])
        # With no shipped extension the core alone is composed.
        core_only = self.tool.core_names(factories=())
        self.assertFalse(any(name.startswith("telegram_") for name in core_only))
        self.assertLess(set(core_only), set(self.core))

    def test_a_core_table_named_after_a_published_id_breaks_rule_a(self):
        table = StorageContribution("tablenote-test", schema=("CREATE TABLE tablenote_x (id TEXT PRIMARY KEY)",))
        names = self.tool.core_names(extra=(table,))
        self.assertIn("tablenote_x", names)
        self.assertEqual(self.tool.clashes(["tablenote"], names=names), [
            "Rule (a): the name tablenote_x, which Qorqbot can put into a database, starts with tablenote_, and "
            "tablenote is a published extension id."])

    def test_a_new_id_that_starts_a_core_name_breaks_rule_b(self):
        found = self.tool.clashes([], ["agent"], self.core)
        self.assertTrue(found)
        self.assertIn("Rule (b): the new extension id agent cannot be used, as the name agent_activity, which Qorqbot can "
                      "put into a database, starts with agent_.", found)
        self.assertTrue(all(line.startswith("Rule (b): the new extension id agent ") for line in found))

    def test_an_id_that_is_another_id_followed_by_more_breaks_the_id_rule_in_either_order(self):
        said = ["The id rule: the extension id ab_c is the id ab followed by _ and more."]
        self.assertEqual(self.tool.clashes(["ab"], ["ab_c"], self.core), said)
        self.assertEqual(self.tool.clashes(["ab_c"], ["ab"], self.core), said)
        self.assertEqual(self.tool.clashes(["ab", "ab_c"], names=self.core), said)
        self.assertEqual(self.tool.clashes(["ab", "abc"], names=self.core), [])

    def test_the_ids_of_the_extensions_the_app_ships_count_as_published_for_the_id_rule(self):
        self.assertIn("telegram", self.tool.shipped_ids())
        self.assertEqual(self.tool.clashes([], ["telegram_bot"], self.core),
                         ["The id rule: the extension id telegram_bot is the id telegram followed by _ and more."])
        self.assertEqual(self.tool.id_clashes(["telegram_bot"]), [("telegram", "telegram_bot")])
        self.assertEqual(self.tool.id_clashes(["google"]), [("google", "google_calendar")])

    def test_a_shipped_package_this_copy_does_not_hold_is_left_out_and_said(self):
        # As in the release export, which leaves the optional Fastmail package out: neither a folder nor importable.
        tool, out = load_tool(), io.StringIO()
        factories = ("qorqut_telegram.extension:create", "qorqut_not_in_this_copy.extension:create")
        with contextlib.redirect_stderr(out):
            self.assertEqual(tool.shipped_ids(factories), ("telegram",))
            self.assertEqual(tool.shipped_ids(factories), ("telegram",))
        self.assertEqual(out.getvalue(), "The names and ids of the extensions in qorqut_not_in_this_copy were not "
                                         "checked: this copy of Qorqbot does not hold that package.\n")
        self.assertEqual(tool.core_names(factories=factories), self.tool.core_names(factories=factories[:1]))

    def test_a_shipped_package_that_is_importable_but_not_a_folder_of_this_tree_is_composed_from_where_it_is(self):
        tool, out = load_tool(), io.StringIO()
        place = next(root / "qorqut-fastmail" for root in tool.PACKAGE_ROOTS if (root / "qorqut-fastmail").is_dir())
        tool.PACKAGE_ROOTS = tuple(root for root in tool.PACKAGE_ROOTS if not (root / "qorqut-fastmail").is_dir())
        self.assertIsNone(tool._package_folder("qorqut_fastmail.extension:create"))
        # On the import path from a place that is no folder of the tree's package roots, as an installed wheel is: the
        # test does not lean on what the interpreter that runs it has installed.
        with mock.patch.object(sys, "path", [*sys.path, str(place)]), contextlib.redirect_stderr(out):
            self.assertIn("fastmail", tool.shipped_ids())
        self.assertEqual(out.getvalue(), "")

    def test_a_package_two_folders_of_this_tree_hold_is_refused(self):
        tool = load_tool()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for folder in ("one", "two"):
            (root / folder / "qorqut_twice").mkdir(parents=True)
        tool.PACKAGE_ROOTS = (root,)
        with self.assertRaisesRegex(ValueError, "This tree holds the package qorqut_twice more than once."):
            tool.shipped_ids(("qorqut_twice.extension:create",))

    def test_the_command_reads_the_index_files_and_says_each_clash(self):
        released, new = self.index("ab", "network"), self.index("network", "ab_c", "agent")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.tool.main(["--released", str(released), "--new", str(new)]), 1)
        lines = out.getvalue().splitlines()
        self.assertIn("The id rule: the extension id ab_c is the id ab followed by _ and more.", lines)
        self.assertTrue(any(line.startswith("Rule (b): the new extension id agent ") for line in lines))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.tool.main(["--released", str(self.index("network"))]), 0)
        self.assertEqual(out.getvalue(), "No extension id and name clash.\n")


if __name__ == "__main__":
    unittest.main()
