"""A removed extension's tables stay: the store opens at a start without its code, its rows and its record row are kept,
and a new install of it finds them. "Remove and delete its data" deletes its private folder only.

The change is staged and applied as in ``test_extension_tables_change.py``; each start is the real ``build_runtime``."""
import shutil
import sqlite3

from test_extension_tables_change import ChangeTablesCase


class RemovalKeepsTest(ChangeTablesCase):
    def remove(self, delete_data=False):
        from qorqut.bootstrap import extension_change
        outcome = extension_change.stage_removal(self.paths, "tablenote", delete_data=delete_data,
                                                 restart=lambda reason: True)
        self.assertTrue(outcome.staged, outcome.note)
        return self.apply()

    def test_a_removal_keeps_the_rows_and_a_new_install_reuses_them(self):
        self.install()
        record = self.store("SELECT * FROM extensiontables")
        identity = self.identity()
        self.assertEqual(self.remove(), {"state": "done", "note": None})
        # The store opens with no code of it: its tables are composed from the record.
        runtime = self.start()
        self.finish()
        self.assertFalse(self.installed().exists())
        self.assertNotIn("tablenote", [item["id"] for item in self.listed(runtime)])
        self.assertEqual(runtime.database.assembly.target.digest, identity[0][2])
        self.assertEqual((self.store("SELECT * FROM extensiontables"), self.identity()), (record, identity))
        self.assertEqual(self.store("SELECT id, text FROM tablenote_items"), [("a", "kept")])
        runtime.stop()
        # Installed again: nothing is made, and its tool counts the row it wrote before.
        shutil.rmtree(self.release / "1.0.0")  # the same bundle, built again
        change_id = self.stage(self.version("1.0.0"))
        self.assertEqual(self.apply(), {"state": "done", "note": None})
        self.assertEqual(self.change_copies(change_id), [])
        again = self.start()
        self.assertEqual(self.entry(again, "tablenote")["status"], "ready")
        self.assertEqual(self.count(again), 1)
        self.assertEqual((self.store("SELECT * FROM extensiontables"), self.identity()), (record, identity))

    def test_remove_and_delete_its_data_deletes_the_private_folder_only(self):
        from qorqut.extension_private import PrivateFolder
        self.install()
        private = PrivateFolder(self.paths.instance_root, "tablenote").path
        private.mkdir(parents=True, exist_ok=True)
        (private / "kept-by-it.txt").write_text("x")
        record, identity = self.store("SELECT * FROM extensiontables"), self.identity()
        self.assertEqual(self.remove(delete_data=True), {"state": "done", "note": None})
        self.assertFalse(private.exists())
        self.assertEqual((self.store("SELECT * FROM extensiontables"), self.identity(),
                          self.store("SELECT id, text FROM tablenote_items")), (record, identity, [("a", "kept")]))
        runtime = self.start()
        self.assertNotIn("tablenote", [item["id"] for item in self.listed(runtime)])
        with sqlite3.connect(self.paths.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tablenote_items").fetchone(), (1,))
        connection.close()

    def listed(self, runtime):
        from qorqut_operations.contracts import plain
        return plain(self.invoke(runtime, "extensions_list", {}).data)["extensions"]


if __name__ == "__main__":
    import unittest
    unittest.main()
