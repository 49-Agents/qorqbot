import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import patch
from uuid import UUID, uuid4

from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.messaging.notifications import notification_text
from qorqut_core.storage.database import Database
from qorqut_core.storage.migrations import MIGRATIONS
from test_foundation import InjectedCrash

from .support import ChatCase


class Messages(ChatCase):
    def test_atomic_send_exact_body_and_receipt_replay(self):
        body = "  文🙂\n\t\x00\r\x1b\x7f\x85  "
        counts = self.counts()
        for point in ("conversation_after_entry", "message_after_event", "operation_after_result"):
            with self.subTest(point=point):
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.send(body=body, sender=self.actor(self.b))
                self.assertEqual(self.counts(), counts)
                for table in ("messages", "message_threads", "message_deliveries", "conversation_entries", "conversation_publications"):
                    self.assertEqual(self.rows(table), [])
        saved = self.send(body=body, sender=self.actor(self.b))
        self.assertEqual(saved.outcome, "accepted")
        self.assertEqual(saved.data["message"]["body"], body)
        self.assertEqual(saved.data["delivery"]["state"], "pending")
        self.assertEqual(saved.data["delivery"]["reason"], "runtime_unavailable")
        self.assertEqual(len(saved.data["entries"]), 2)
        counts = self.counts()
        self.restart()
        self.assertEqual(saved, self.send(body=body, sender=self.actor(self.b)))
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.app.invoke("messages_send", self.actor(self.b), {
            "idempotency_key": "send", "recipient_agent_id": self.a["id"], "body": "changed"}).error.code, "idempotency_conflict")
        self.assertEqual(len([j for j in self.rows("jobs") if j["kind"] == "message_delivery"]), 1)
        self.pump()
        self.assertEqual(self.conversation()["items"][0]["text"], body)
        self.assertEqual(json.loads(self.path().read_bytes())["text"], body)
        self.assertEqual(self.messages(message_id=saved.data["message"]["message_id"])["items"][0]["message"]["body"], body)
        self.fault.point = "transaction_after_commit"
        with self.assertRaises(InjectedCrash):
            self.send("lost-response", "saved before response loss")
        self.restart()
        recovered = self.send("lost-response", "saved before response loss")
        self.assertEqual(len(self.rows("messages")), 2)
        self.assertEqual(self.messages(agent_id=self.a["id"])["items"][-1]["message"], recovered.data["message"])

    def test_notification_is_only_the_canonical_uuid(self):
        message_id = UUID("ABCDEF12-3456-4789-ABCD-0123456789AB")
        text = notification_text(message_id)
        self.assertEqual(text, "[Qorqut inbox abcdef12-3456-4789-abcd-0123456789ab]")
        self.assertEqual(len(text.encode("ascii")), 51)
        for value in (str(message_id), "\x1b injected", None):
            with self.assertRaises(TypeError):
                notification_text(value)

    def test_exact_id_reads_intersect_filters_without_implicit_self(self):
        first = self.send("first", sender=self.actor(self.a), recipient=self.b).data["message"]
        other = self.send("other", recipient=self.onbash).data["message"]
        filters = {"message_id": first["message_id"]}
        for caller in (self.context, self.actor(self.a), self.actor(self.b), self.actor(self.onbash)):
            page = self.messages(caller=caller, **filters)
            self.assertEqual([item["message"] for item in page["items"]], [first])
            self.assertIsNone(page["items"][0]["handling"])
            self.assertFalse(page["has_more"])
            self.assertEqual(page["next_after_seq"], first["message_seq"])
        self.assertEqual(len(self.messages(**filters, agent_id=self.a["id"], thread_id=first["thread_id"], limit=1)["items"]), 1)
        for extra in ({"agent_id": self.onbash["id"]}, {"thread_id": other["thread_id"]},
                      {"after_seq": first["message_seq"], "recovery_epoch": page["recovery_epoch"]}):
            self.assertEqual(self.messages(**filters, **extra)["items"], [])
        self.assertEqual(self.app.invoke("messages_list", self.context, {**filters, "after_seq": 1}).error.code, "invalid_argument")
        self.assertEqual(self.app.invoke("messages_list", self.context,
            {**filters, "recovery_epoch": str(uuid4())}).error.code, "target_changed")
        for extra in ({"message_id": str(uuid4())}, {**filters, "agent_id": str(uuid4())},
                      {**filters, "thread_id": str(uuid4())}):
            self.assertEqual(self.app.invoke("messages_list", self.context, extra).error.code, "not_found")
        receipt = self.ack([first["message_id"]], caller=self.actor(self.onbash)).data["items"][0]
        self.assertEqual(self.messages(**filters, unhandled_only=True)["items"], [])
        self.assertEqual(self.messages(**filters)["items"][0]["handling"], receipt)

    def test_pre_d43_failed_receipt_replays_without_resurrection(self):
        # Actual pre-D43 code writes the historical receipt into disposable data.
        base = "0fc5ffbc71cc0036f81967e34e9b670846e1fc6e"
        repo = Path(__file__).resolve().parents[2]
        archive = subprocess.run(["git", "-C", str(repo), "archive", base, "src"], capture_output=True)
        if archive.returncode:
            self.skipTest("Pinned pre-D43 source is unavailable")
        source = self.root / "pre-d43"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(source, filter="data")
        self.app.close()
        # The old executable must receive a genuine v7 store, even after this
        # test's normal fixture has advanced to a newer host schema.
        self.settings = self.settings.model_copy(update={"data_root": self.root / "pre-d43-data"})
        migrate = Database.migrate
        with patch("qorqut_core.storage.database.SCHEMA_VERSION", 7), \
                patch.object(Database, "migrate", lambda db: migrate(db, migrations=MIGRATIONS[:7])):
            self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
            self.a = self.create("historical-agent").data["agent"]
            self.pump()
            self.app.close()
        arguments = {"idempotency_key": "historical", "recipient_agent_id": self.a["id"], "body": "\x00\r\x1b\x85"}
        script = """
import json, sys
from qorqut_core.app import Application
from qorqut_core.config import InstallationSettings
from qorqut_core.contracts import Invocation
app = Application.open(InstallationSettings.model_validate_json(sys.argv[1]))
try:
    print(app.invoke('messages_send', Invocation.model_validate_json(sys.argv[2]), json.loads(sys.argv[3])).model_dump_json())
finally:
    app.close()
"""
        produced = subprocess.run([sys.executable, "-B", "-c", script, self.settings.model_dump_json(),
            self.context.model_dump_json(), json.dumps(arguments)], capture_output=True, text=True,
            env={"PATH": os.defpath, "PYTHONPATH": str(source / "src"), "PYTHONNOUSERSITE": "1"})
        self.assertEqual(produced.returncode, 0, produced.stderr)
        old = json.loads(produced.stdout)
        self.assertTrue(old["ok"], old)
        self.assertEqual(old["data"]["delivery"]["reason"], "unsafe_terminal_text")
        self.app.db.lock.acquire()
        try:
            self.app.db.migrate()
        finally:
            self.app.db.lock.close()
        self.restart()
        watched = ("operations", "messages", "message_deliveries", "events", "jobs")
        before = {table: self.rows(table) for table in watched}
        self.assertEqual(self.app.invoke("messages_send", self.context, arguments).model_dump(mode="json"), old)
        self.assertEqual({table: self.rows(table) for table in watched}, before)
        self.recovery()
        item = self.messages(message_id=old["data"]["message"]["message_id"])["items"][0]
        self.assertEqual(item["delivery"], old["data"]["delivery"])
        self.assertFalse(any(job["kind"] == "message_delivery" for job in self.rows("jobs")))
        self.assertEqual(self.app.invoke("messages_send", self.context, arguments).model_dump(mode="json"), old)
        new = self.send("new-controls", arguments["body"])
        self.assertEqual(new.data["delivery"]["state"], "pending")

    def test_validation_authorship_threads_and_immutable_handling(self):
        for body in ("", "🙂" * 16385, "\ud800"):
            result = self.app.invoke("messages_send", self.context,
                {"idempotency_key": "invalid", "recipient_agent_id": self.a["id"], "body": body})
            self.assertEqual(result.error.code, "invalid_argument")
        for extra in ({"sender_actor_id": str(uuid4())}, {"recipient_agent_id": str(uuid4())}):
            result = self.app.invoke("messages_send", self.context,
                {"idempotency_key": "invalid", "recipient_agent_id": self.a["id"], "body": "text", **extra})
            self.assertFalse(result.ok)
        first = self.send("first", sender=self.actor(self.a), recipient=self.b).data["message"]
        reply = self.send("reply", sender=self.actor(self.b), recipient=self.a,
            reply_to_message_id=first["message_id"]).data["message"]
        self.assertEqual(reply["thread_id"], first["thread_id"])
        self.assertIsNone(self.messages(agent_id=self.b["id"])["items"][0]["handling"])
        self.assertEqual(len(self.messages(caller=self.actor(self.onbash), thread_id=first["thread_id"])["items"]), 2)
        self.assertEqual(self.messages(agent_id=self.onbash["id"], thread_id=first["thread_id"])["items"], [])
        for changes in ({"thread_id": first["thread_id"]},
                        {"reply_to_message_id": first["message_id"], "thread_id": str(uuid4())}):
            result = self.app.invoke("messages_send", self.context,
                {"idempotency_key": "wrong-endpoints", "recipient_agent_id": self.b["id"], "body": "bad routing", **changes})
            self.assertEqual(result.error.code, "invalid_argument")
        first_id, reply_id = first["message_id"], reply["message_id"]
        result = self.ack([first_id, str(uuid4())])
        self.assertEqual(result.error.code, "not_found")
        self.assertEqual(self.rows("message_handling"), [])
        self.assertEqual(self.ack([first_id, first_id]).error.code, "invalid_argument")
        receipt = self.ack([first_id], caller=self.actor(self.onbash), note="Handled intentionally\n").data["items"][0]
        self.assertEqual(receipt["handled_by"], self.onbash["actor_id"])
        repeat = self.ack([first_id], "compatible", caller=self.actor(self.onbash), note="Handled intentionally\n")
        self.assertEqual(repeat.data["items"][0], receipt)
        conflict = self.ack([reply_id, first_id], "conflict", note="different")
        self.assertEqual(conflict.error.code, "invalid_transition")
        self.assertEqual(len(self.rows("message_handling")), 1)
        self.assertEqual(self.conversation(self.b)["items"][0]["delivery"]["state"], "pending")
        self.assertEqual(len(self.messages(agent_id=self.b["id"], unhandled_only=True)["items"]), 1)

    def test_cursor_global_order_filters_and_runtime_dependency(self):
        first = self.send("first", sender=self.actor(self.a), recipient=self.b)
        self.send("unrelated", recipient=self.onbash)
        self.send("second", sender=self.actor(self.b), recipient=self.a)
        self_message = self.send("self", sender=self.actor(self.a), recipient=self.a)
        self.assertEqual(len(self_message.data["entries"]), 1)
        page = self.messages(caller=self.actor(self.a), limit=1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_after_seq"], first.data["message"]["message_seq"])
        page2 = self.messages(agent_id=self.a["id"], after_seq=page["next_after_seq"], recovery_epoch=page["recovery_epoch"])
        self.assertEqual([i["message"]["message_seq"] for i in page2["items"]], [3, 4])
        self.assertFalse(page2["has_more"])
        self.assertEqual(self.app.invoke("messages_list", self.context).error.code, "invalid_argument")
        self.assertEqual(self.app.invoke("messages_list", self.context, {
            "agent_id": self.a["id"], "after_seq": 1}).error.code, "invalid_argument")
        self.assertEqual(self.app.invoke("messages_list", self.context, {
            "thread_id": str(uuid4())}).error.code, "not_found")
        self.assertEqual(self.app.invoke("messages_list", self.context, {
            "agent_id": self.a["id"], "recovery_epoch": str(uuid4())}).error.code, "target_changed")
        self.pump()
        jobs = [j for j in self.rows("jobs") if j["kind"] == "message_delivery"]
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j["state"] == "waiting" and j["wait_reason"] == "dependency_unavailable" for j in jobs))
        delivery = self.app.invoke("deliveries_get", self.context, {"message_id": first.data["message"]["message_id"]})
        self.assertEqual(delivery.data["attempts"], [])
        self.assertEqual(delivery.data["delivery"]["reason"], "runtime_unavailable")
        self.assertEqual(self.app.jobs.run_next(worker_id=uuid4()), None)

    def test_message_handling_and_ledger_refuse_update_delete_replace(self):
        message = self.send().data["message"]
        self.assertTrue(self.ack([message["message_id"]]).ok)
        for table in ("message_threads", "messages", "message_handling", "conversation_entries"):
            row = self.rows(table)[0]
            key = next(iter(row))
            for sql in (f"UPDATE {table} SET {key}={key}", f"DELETE FROM {table}",
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with self.subTest(sql=sql), self.assertRaises(CoreError):
                    with self.app.db.transaction() as tx:
                        tx.connection.execute(sql)
        self.assertEqual(self.rows("messages")[0]["body"], message["body"])
