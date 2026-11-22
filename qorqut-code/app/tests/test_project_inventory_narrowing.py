"""The Project list leaves archived and closed Tasks out in SQL, so it does not read hundreds of Tasks to show a few."""
import json
import sqlite3
import unittest
from types import SimpleNamespace as NS

from qorqut.domains.projects.inventory import Inventory

# (id, status, archived, task type or None for an ordinary Task)
TASKS = (("open", "open", 0, None), ("waiting", "waiting", 0, "task"), ("email-done", "done", 0, "email"),
         ("done", "done", 0, None), ("cancelled", "cancelled", 0, "task"), ("story-done", "done", 0, "story"),
         ("customer-cancelled", "cancelled", 0, "customer"), ("open-archived", "open", 1, None),
         ("done-archived", "done", 1, None), ("story-archived", "done", 1, "story"))
FLAGS = [{"include_closed": closed, "include_archived": archived} for closed in (False, True) for archived in (False, True)]


class TaskNarrowingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE tasks (project_id TEXT, id TEXT, status TEXT, archived INTEGER, fields_json TEXT, updated_at TEXT)")
        for position, (identity, status, archived, task_type) in enumerate(TASKS):
            fields = {"$task": {"task_type": task_type, "overview": 'A "story" about a "customer".'}} if task_type else {"note": "story"}
            self.connection.execute("INSERT INTO tasks VALUES ('p1',?,?,?,?,?)",
                                    (identity, status, archived, json.dumps(fields), f"2026-11-{10 + position:02d}T00:00:00Z"))
        self.connection.execute("INSERT INTO tasks VALUES ('other','elsewhere','open',0,'{}','2026-11-01T00:00:00Z')")
        self.inventory = Inventory(NS(), NS(contributions=()))

    def selected(self, args):
        selects, params = self.inventory._selects(NS(), "p1", {"section": "work", **args})
        self.assertEqual(len(selects), 1)
        return {row["id"] for row in self.connection.execute(selects[0], params)}

    def test_sql_keeps_exactly_the_owned_tasks_the_list_shows(self):
        for args in FLAGS:
            shown = {identity for identity, status, archived, task_type in TASKS
                     if Inventory.visible({"type": "task", "state": status, "task_type": task_type or "task", "_archived": bool(archived)}, args)}
            self.assertEqual(self.selected(args), shown, args)

    def test_default_list_leaves_out_archived_and_closed_ordinary_tasks(self):
        self.assertEqual(self.selected({}), {"open", "waiting", "story-done", "customer-cancelled"})
        self.assertEqual(self.selected({"include_closed": True, "include_archived": True}), {identity for identity, *_ in TASKS})

    def test_narrowing_is_fixed_sql_with_no_values(self):
        self.assertEqual(Inventory._task_narrowing({"include_closed": True, "include_archived": True}), "")
        self.assertEqual(Inventory._task_narrowing({"include_closed": True}), " AND archived=0")
        self.assertNotIn("?", Inventory._task_narrowing({}))


if __name__ == "__main__":
    unittest.main()
