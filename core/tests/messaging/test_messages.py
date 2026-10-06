from uuid import uuid4

from qorqut_core.contracts import CoreError
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
        self.assertEqual(saved.data["delivery"]["state"], "failed")
        self.assertEqual(saved.data["delivery"]["reason"], "unsafe_terminal_text")
        self.assertEqual(len(saved.data["entries"]), 2)
        counts = self.counts()
        self.restart()
        self.assertEqual(saved, self.send(body=body, sender=self.actor(self.b)))
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.app.invoke("messages_send", self.actor(self.b), {
            "idempotency_key": "send", "recipient_agent_id": self.a["id"], "body": "changed"}).error.code, "idempotency_conflict")
        self.assertFalse(any(j["kind"] == "message_delivery" for j in self.rows("jobs")))
        self.pump()
        self.assertEqual(self.conversation()["items"][0]["text"], body)
        self.fault.point = "transaction_after_commit"
        with self.assertRaises(InjectedCrash):
            self.send("lost-response", "saved before response loss")
        self.restart()
        recovered = self.send("lost-response", "saved before response loss")
        self.assertEqual(len(self.rows("messages")), 2)
        self.assertEqual(self.messages(agent_id=self.a["id"])["items"][-1]["message"], recovered.data["message"])

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
