"""Default enrollment and normal reminder lifecycle on isolated real stores."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.storage.database import Database
from qorqut.composition.assembly import host_assembly
from qorqut.reminders import defaults, defaults_upgrade, timing
from qorqut.reminders.service import ReminderService
from qorqut.service.changes import ChangeFeed


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.db = Database(self.path, assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.owner = str(uuid4())
        self.ctx = InvocationContext(Principal(self.owner, "human"))
        self.authorize = mock.Mock(return_value=SimpleNamespace(mode="allow"))
        app = SimpleNamespace(policy=SimpleNamespace(context_for=lambda *a, **kw: self.ctx),
                              current_context=lambda ctx, uow: ctx, authorize=self.authorize,
                              registry=SimpleNamespace(get=lambda name: name))
        self.service = ReminderService(SimpleNamespace(application=app, inbox=SimpleNamespace(clock=lambda: self.now)))
        with self.db.transaction() as uow:
            self.principal(uow, self.owner, "human")
            uow.repository("installation").insert({"id": "service", "installation_id": str(uuid4()),
                "owner_principal_id": self.owner, "created_at": timing.stamp(self.now), "updated_at": timing.stamp(self.now)})

    def principal(self, uow, identity, kind):
        uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": identity,
            "active": 1, "created_at": timing.stamp(self.now), "updated_at": timing.stamp(self.now)})

    def agent(self, *, role="global-worker", runtime="codex", status="active", malformed=False):
        identity = str(uuid4())
        with self.db.transaction() as uow:
            self.principal(uow, identity, "agent")
            profile = {"status": status, "scope": {"kind": "ecosystem"}, "role": {"id": role}, "runtime": runtime}
            uow.repository("agent_profiles", business_id=None).insert({"agent_id": identity, "principal_id": identity,
                "namespace": "ecosystem", "name": identity, "workspace": "/synthetic/" + identity,
                "status": status, "profile_json": [] if malformed else profile,
                "created_at": timing.stamp(self.now), "updated_at": timing.stamp(self.now)})
        return identity

    def reconcile(self):
        with self.db.transaction() as uow:
            return self.service.reconcile(uow)

    def reminders(self):
        with self.db.read() as uow:
            return [dict(r) for r in uow.connection.execute("SELECT * FROM reminders ORDER BY recipient_id")]

    def test_all_roles_and_runtimes_enroll_on_normal_pass_and_new_agents_follow(self):
        identities = [self.agent(role=role, runtime=runtime) for role, runtime in
                      [("ordubashy", "codex"), ("global-worker", "claude-code"), ("learning-applier", "codex")]]
        self.assertEqual(self.reminders(), [])  # construction/inspection is inert
        self.reconcile()
        self.assertEqual({r["recipient_id"] for r in self.reminders()}, set(identities))
        for row in self.reminders():
            self.assertEqual(row["state"], "scheduled")
            self.assertEqual(row["creator_id"], self.owner)
            self.assertFalse(row["time_critical"])
            self.assertLessEqual(len(row["text"]), 240)
            first = timing.parse_stamp(row["next_at"])
            self.assertEqual(timing.first_slot_after(row["schedule"], row["timezone"], first) - first, timedelta(hours=24))
        before = self.reminders()
        self.service.defaults = defaults.BrowserCleanupDefaults(self.service)  # simulated restart
        self.reconcile()
        self.assertEqual(self.reminders(), before)
        identities.append(self.agent())
        self.reconcile()
        self.assertEqual({r["recipient_id"] for r in self.reminders()}, set(identities))

    def test_cancel_edit_snooze_and_readdress_survive_restart(self):
        for action in ("cancel", "edit", "snooze", "readdress"):
            with self.subTest(action=action):
                identity = self.agent()
                self.reconcile()
                row = next(r for r in self.reminders() if r["recipient_id"] == identity)
                with self.db.transaction() as uow:
                    changes = (self.service.cancellation(row) if action == "cancel" else
                               self.service.snoozing(row, {"at": timing.stamp(self.now + timedelta(days=3))}, self.now, self.owner)
                               if action == "snooze" else {"text": "Customized cleanup"} if action == "edit" else
                               {"recipient_id": self.owner})
                    changes.pop("close", None)
                    self.service.save(uow, row, changes, timing.stamp(self.now))
                before = self.reminders()
                self.service.defaults = defaults.BrowserCleanupDefaults(self.service)
                self.reconcile()
                self.assertEqual(self.reminders(), before)

    def legacy(self, identity, *, cancelled=False):
        with self.db.transaction() as uow:
            task_id = str(uuid4())
            uow.repository("tasks", business_id=None).insert({"id": task_id, "title": defaults.LEGACY_TITLE,
                "status": "open", "created_by": self.owner, "created_at": timing.stamp(self.now),
                "updated_at": timing.stamp(self.now)})
            args = {"recipient_id": identity, "text": "Daily browser cleanup: follow the linked task.",
                    "recurrence": {"schedule": "23 4 * * *", "timezone": "America/La_Paz"}}
            row = self.service.new_row(self.service.caller(self.ctx, uow), args, self.now, uow)
            row["linked_item_json"] = json.dumps({"type": "task", "id": task_id, "business_id": None})
            if cancelled:
                row["state"] = "cancelled"
            self.service.insert(uow, row)
        return row

    def test_adopts_existing_and_cancelled_series_without_changing_them(self):
        identities = [self.agent(), self.agent()]
        for identity, cancelled in zip(identities, (False, True)):
            self.legacy(identity, cancelled=cancelled)
        before = self.reminders()
        self.reconcile()
        self.assertEqual(self.reminders(), before)
        with self.db.transaction() as uow:
            for row in before:
                self.service.save(uow, row, {"text": "Owner customization", "linked_item_json": None}, timing.stamp(self.now))
        before = self.reminders()
        self.service.defaults = defaults.BrowserCleanupDefaults(self.service)
        self.reconcile()
        self.assertEqual(self.reminders(), before)

    def test_ambiguous_existing_series_are_not_duplicated(self):
        identity = self.agent()
        self.legacy(identity)
        self.legacy(identity)
        with self.assertLogs(defaults.LOG, level="WARNING"):
            self.reconcile()
        self.assertEqual(len(self.reminders()), 2)
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT count(*) FROM browser_cleanup_enrollments").fetchone()[0], 0)

    def test_new_agents_share_existing_readable_cleanup_task(self):
        original = self.agent()
        saved = self.legacy(original)
        link = json.loads(saved["linked_item_json"])
        self.service.runtime.focus = SimpleNamespace(resolve=lambda ctx, ref, uow:
            uow.repository("tasks", business_id=None).get(ref["id"]))
        self.reconcile()
        newcomer = self.agent()
        self.reconcile()
        row = next(r for r in self.reminders() if r["recipient_id"] == newcomer)
        self.assertEqual(json.loads(row["linked_item_json"]), link)
        self.assertEqual(len(self.reminders()), 2)

    def test_unreadable_task_is_not_attached_to_new_default(self):
        self.legacy(self.agent())
        newcomer = self.agent()
        self.service.runtime.focus = SimpleNamespace(resolve=lambda *args: None)
        self.reconcile()
        row = next(r for r in self.reminders() if r["recipient_id"] == newcomer)
        self.assertIsNone(row["linked_item_json"])

    def test_inactive_retired_and_malformed_profiles_are_not_enrolled(self):
        self.agent(status="inactive")
        self.agent(role="retired-role")
        self.agent(malformed=True)
        self.reconcile()
        self.assertEqual(self.reminders(), [])

    def test_denied_owner_and_inactive_owner_do_not_seed(self):
        self.agent()
        self.authorize.return_value = SimpleNamespace(mode="deny")
        with self.assertLogs(defaults.LOG, level="WARNING"):
            self.reconcile()
        self.assertEqual(self.reminders(), [])
        self.authorize.return_value = SimpleNamespace(mode="allow")
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (self.owner,))
        self.reconcile()
        self.assertEqual(self.reminders(), [])

    def test_retirement_does_not_reset_opt_out_on_reactivation(self):
        identity = self.agent()
        self.reconcile()
        row = self.reminders()[0]
        with self.db.transaction() as uow:
            self.service.save(uow, row, {"state": "cancelled"}, timing.stamp(self.now))
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (identity,))
        self.reconcile()
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (identity,))
        self.reconcile()
        self.assertEqual(len(self.reminders()), 1)
        self.assertEqual(self.reminders()[0]["state"], "cancelled")

    def test_transaction_rollback_keeps_no_partial_enrollment(self):
        self.agent()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as uow:
                self.service.reconcile(uow)
                raise RuntimeError("rollback")
        self.assertEqual(self.reminders(), [])
        self.reconcile()
        self.assertEqual(len(self.reminders()), 1)

    def test_bounded_passes_eventually_enroll_everyone(self):
        identities = [self.agent() for _ in range(defaults.BATCH + 3)]
        self.reconcile()
        self.assertLessEqual(len(self.reminders()), defaults.BATCH)
        for _ in range(5):
            self.reconcile()
        self.assertEqual({r["recipient_id"] for r in self.reminders()}, set(identities))

    def test_failed_first_candidate_does_not_starve_later_agents(self):
        identities = sorted([self.agent(), self.agent()])
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json='[]' WHERE agent_id=?", (identities[0],))
        with mock.patch.object(defaults, "BATCH", 1):
            self.reconcile()
            self.assertEqual(self.reminders(), [])
            self.reconcile()
        self.assertEqual(self.reminders()[0]["recipient_id"], identities[1])

    def test_due_dismissal_and_next_day_use_existing_reminder_lifecycle(self):
        self.agent()
        self.reconcile()
        self.now = timing.parse_stamp(self.reminders()[0]["next_at"])
        self.assertEqual(self.reconcile()["fired"], 1)
        row = self.reminders()[0]
        self.assertEqual(row["state"], "due")
        with self.db.transaction() as uow:
            changes = self.service.dismissal(row, {"occurrence_id": row["current_occurrence_id"]})
            reason = changes.pop("close")
            self.service.close_occurrence(uow, row["current_occurrence_id"], reason, timing.stamp(self.now))
            self.service.save(uow, row, changes, timing.stamp(self.now))
        self.assertEqual(self.reminders()[0]["state"], "scheduled")
        self.now += timedelta(hours=24)
        self.assertEqual(self.reconcile()["fired"], 1)
        self.assertEqual(len(self.reminders()), 1)


class UpgradeTest(unittest.TestCase):
    def test_exact_upgrade_is_read_only_on_inspect_and_idempotent_on_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            predecessor, target = defaults_upgrade.compositions()
            db = Database(path, assembly=predecessor).initialize_target()
            db.close()
            with sqlite3.connect(path, isolation_level=None) as connection:
                self.assertEqual(defaults_upgrade.inspect(connection)["store"], "predecessor")
                self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (defaults.TABLE,)).fetchone())
                self.assertEqual(defaults_upgrade.apply(connection)["schema"], "created")
                self.assertEqual(defaults_upgrade.apply(connection)["schema"], "already_current")
                self.assertEqual(defaults_upgrade.inspect(connection)["store"], "target")
                self.assertEqual(connection.execute("SELECT count(*) FROM reminders").fetchone()[0], 0)
            db = Database(path, assembly=target)
            db.open_existing()
            db.close()

    def test_unknown_schema_is_refused_without_partial_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            db = Database(path, assembly=defaults_upgrade.compositions()[0]).initialize_target()
            db.close()
            with sqlite3.connect(path, isolation_level=None) as connection:
                connection.execute("CREATE TABLE unexpected (id TEXT)")
                with self.assertRaises(DomainError):
                    defaults_upgrade.apply(connection)
                self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (defaults.TABLE,)).fetchone())

    def test_failure_after_ddl_rolls_back_schema_identity_and_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            db = Database(path, assembly=defaults_upgrade.compositions()[0]).initialize_target()
            db.close()
            with sqlite3.connect(path, isolation_level=None) as connection:
                with mock.patch.object(defaults_upgrade, "recognise", side_effect=["predecessor", DomainError("invalid_database", "probe")]):
                    with self.assertRaises(DomainError):
                        defaults_upgrade.apply(connection)
                self.assertEqual(defaults_upgrade.inspect(connection)["store"], "predecessor")
                self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (defaults.TABLE,)).fetchone())


if __name__ == "__main__":
    unittest.main()
