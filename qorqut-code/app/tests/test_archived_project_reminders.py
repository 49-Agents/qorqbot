"""The coordinator's reminder reads leave archived projects out; the owner still sees everything."""
import sqlite3
from types import SimpleNamespace as NS
import unittest

from qorqut.reminders.legacy import LegacyReminders
from qorqut.reminders.service import Access, ReminderService


class ArchivedProjectReminderTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "CREATE TABLE projects(id TEXT PRIMARY KEY, archived_at TEXT);"
            "CREATE TABLE reminders(id TEXT PRIMARY KEY, project_id TEXT, recipient_id TEXT, state TEXT, text TEXT,"
            " due_at TEXT, next_at TEXT, created_at TEXT, revision INTEGER);"
            "INSERT INTO projects VALUES('live',NULL),('gone','2026-11-01');"
            "INSERT INTO reminders VALUES('r-live','live','owner','scheduled','a',NULL,'2026-12-01','2026-11-01',1),"
            "('r-gone','gone','owner','scheduled','b',NULL,'2026-12-01','2026-11-01',1),"
            "('r-global',NULL,'owner','scheduled','c',NULL,'2026-12-01','2026-11-01',1);")
        self.addCleanup(self.connection.close)
        self.uow = NS(connection=self.connection)
        self.service = object.__new__(ReminderService)
        self.rows = {row["id"]: dict(row) for row in self.connection.execute("SELECT * FROM reminders")}

    def reached(self, kind):
        access = Access(NS(principal_id="someone"), kind)
        return {name for name, row in self.rows.items() if self.service.reachable(access, row, self.uow)}

    def test_the_coordinator_does_not_reach_archived_project_reminders_and_the_owner_does(self):
        self.assertEqual(self.reached("coordinator"), {"r-live", "r-global"})
        self.assertEqual(self.reached("owner"), {"r-live", "r-gone", "r-global"})

    def test_the_owner_reminder_list_leaves_archived_projects_out_for_the_coordinator(self):
        legacy = object.__new__(LegacyReminders)
        listed = lambda kind: {item["reminder_id"] for item in legacy.listing(Access(None, kind), "owner", self.uow)["reminders"]}
        self.assertEqual(listed("coordinator"), {"r-live", "r-global"})
        self.assertEqual(listed("owner"), {"r-live", "r-gone", "r-global"})


if __name__ == "__main__":
    unittest.main()
