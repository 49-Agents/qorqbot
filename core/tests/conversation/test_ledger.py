from uuid import UUID, uuid4

from qorqut_core.messaging import records

from messaging.support import ChatCase


class Ledger(ChatCase):
    def test_latest_before_after_empty_pages_and_equal_text_identity(self):
        self.assertEqual(self.conversation()["publication"], {"pending_entries": 0, "state": "current", "error": None})
        self.assertEqual(self.rows("conversation_cursors"), [])
        for i in range(5):
            self.send(str(i), "identical")
        page = self.conversation(limit=2)
        epoch = page["recovery_epoch"]
        self.assertEqual([i["conversation_seq"] for i in page["items"]], [4, 5])
        self.assertTrue(page["has_older"])
        self.assertFalse(page["has_newer"])
        self.assertNotEqual(page["items"][0]["entry_id"], page["items"][1]["entry_id"])
        self.assertEqual(page["publication"]["pending_entries"], 5)
        before = self.conversation(before_seq=4, recovery_epoch=epoch, limit=2)
        self.assertEqual([i["conversation_seq"] for i in before["items"]], [2, 3])
        self.assertTrue(before["has_older"] and before["has_newer"])
        after = self.conversation(after_seq=0, recovery_epoch=epoch, limit=2)
        self.assertEqual([i["conversation_seq"] for i in after["items"]], [1, 2])
        self.assertFalse(after["has_older"])
        self.assertTrue(after["has_newer"])
        for args, flags in (({"before_seq": 1}, (False, True)), ({"before_seq": 0}, (False, True)),
                            ({"after_seq": 5}, (True, False)), ({"after_seq": 99}, (True, False))):
            empty = self.conversation(recovery_epoch=epoch, **args)
            self.assertEqual(empty["items"], [])
            self.assertIsNone(empty["first_seq"])
            self.assertIsNone(empty["last_seq"])
            self.assertEqual((empty["has_older"], empty["has_newer"]), flags)
        for args in ({"after_seq": 0}, {"before_seq": 0}, {"before_seq": 4, "after_seq": 0, "recovery_epoch": epoch}):
            self.assertEqual(self.app.invoke("conversation_list", self.context, {"agent_id": self.a["id"], **args}).error.code, "invalid_argument")
        self.assertEqual(self.app.invoke("conversation_list", self.context, {
            "agent_id": self.a["id"], "recovery_epoch": str(uuid4())}).error.code, "target_changed")
        counts = self.counts()
        self.conversation()
        self.assertEqual(self.counts(), counts)

    def test_internal_replay_registered_conversation_and_separate_handling(self):
        saved = self.send(sender=self.actor(self.a), recipient=self.onbash)
        entry = saved.data["entries"][0]
        with self.app.db.transaction() as tx:
            message = records.message(tx, saved.data["message"]["message_id"])
            replay = self.app.conversations.append_message(tx, UUID(self.a["id"]), message, operation_id=saved.operation_id)
        self.assertEqual(str(replay.entry_id), entry["entry_id"])
        self.assertEqual(len(self.rows("conversation_entries")), 2)
        for agent, direction in ((self.a, "outgoing"), (self.onbash, "incoming")):
            item = self.conversation(agent)["items"][0]
            self.assertEqual(item["conversation_id"], agent["conversation_id"])
            self.assertEqual(item["role"], "agent")
            self.assertEqual(item["direction"], direction)
            self.assertTrue(all(value is None for key, value in item["source"].items() if key not in ("type", "message_id")))
        before = self.conversation()["items"][0]
        self.assertTrue(self.ack([saved.data["message"]["message_id"]], note="Done").ok)
        after = self.conversation()["items"][0]
        self.assertEqual({k: v for k, v in before.items() if k != "handling"}, {k: v for k, v in after.items() if k != "handling"})
        self.assertEqual(after["handling"]["handled_by"], str(self.owner.actor_id))
