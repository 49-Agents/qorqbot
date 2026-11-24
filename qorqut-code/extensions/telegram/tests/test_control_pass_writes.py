"""The Telegram control pass's four write steps on a real store: what an idle pass opens, and what a pass with work does.

The steps are the blocking-input expiry, the dispatch claim, the cleanup claim and the owner-input reconcile. The pinned
identity, configuration and provider are stand-ins; everything else is the real bridge over a real store.
"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest

from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.database import Database

from qorqut.composition.assembly import host_assembly
from qorqut.extensions import ExtensionSet
from qorqut.service.changes import ChangeFeed
from qorqut_telegram import store
from qorqut_telegram.bridge import TelegramBridge
from qorqut_telegram.config import ChatBinding, TelegramConfig
from qorqut_telegram.extension import Telegram

NOW = "2026-11-01T09:00:00.000000Z"
PAST = "2026-11-01T08:00:00.000000Z"
LATER = "2026-11-01T10:00:00.000000Z"


class WriteUnits:
    """Counts every write unit the database opens, by its entry point, and the write statements run in it."""

    def __init__(self, database):
        self.opened = []
        for name in ("transaction", "write_if_needed"):
            original = getattr(database, name)

            def entry(*args, _name=name, _original=original, **kwargs):
                self.opened.append((_name, kwargs.get("operation")))
                return _original(*args, **kwargs)
            setattr(database, name, entry)

    def take(self):
        opened, self.opened = self.opened, []
        return opened


class Api:
    def __init__(self):
        self.sent, self.deleted = [], []

    def send_message(self, chat_id, text, reply_to=None, **topic):
        self.sent.append(text)
        return {"message_id": 700 + len(self.sent)}

    def delete_message(self, chat_id, provider_id):
        self.deleted.append(provider_id)


class ControlPassWrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Database(Path(folder.name) / "operations.sqlite",
                                 assembly=host_assembly(ChangeFeed(), ExtensionSet((Telegram(),)))).initialize_target()
        self.addCleanup(self.database.close)
        self.agent = new_id()
        with self.database.transaction() as uow:
            uow.repository("principals").insert({"id": self.agent, "kind": "agent", "display_name": "agent"})
        application = NS(writer_admission=None, ensure_writes_open=Application.ensure_writes_open)
        self.bridge = TelegramBridge(NS(database=self.database, application=application), state_dir=folder.name,
                                     clock=lambda: store.parse(NOW))
        self.chat = ChatBinding("signoffs", -1001, "group")
        self.config = TelegramConfig(enabled=True, chats=(self.chat,))
        self.binding = {"generation": 1, "fingerprint": self.config.binding_fingerprint(), "owner": None, "installation": None}
        self.bridge.config = self.config
        self.bridge.current_config = lambda: self.config
        self.bridge.check_binding = lambda uow: dict(self.binding)
        self.bridge.api = self.api = Api()
        self.units = WriteUnits(self.database)

    # ----- fixtures -------------------------------------------------------------------------------------------------

    def message(self, uow, **values):
        return store.insert_message(uow, direction=values.pop("direction", "outbound"), chat=self.chat,
                                    purpose=values.pop("purpose", "notice"), body=values.pop("body", "text"), now=NOW, **values)

    def pending_request(self, expires_at):
        with self.database.transaction() as uow:
            return self.message(uow, purpose="blocking_input",
                                request={"request_key": new_id(), "input_mode": "text", "expires_at": expires_at})["id"]

    def queued_send(self, next_attempt_at=None):
        with self.database.transaction() as uow:
            row = self.message(uow)
            outbox, _ = store.enqueue(uow, self.chat, 1, self.binding["fingerprint"], source_kind="notice", source_key=new_id(),
                                      payload={"text": "hello"}, now=NOW, message_id=row["id"], next_attempt_at=next_attempt_at)
            return outbox["id"]

    def queued_cleanup(self, *, provider_id=None, not_before=None):
        with self.database.transaction() as uow:
            row = self.message(uow, telegram_message_id=provider_id, direction="inbound", purpose="owner")
            uow.repository("telegram_cleanup_jobs").insert({"id": new_id(), "message_id": row["id"], "reason": "test",
                "not_before": not_before, "created_at": NOW, "updated_at": NOW})
            return row["id"]

    def queued_input(self, expires_at):
        with self.database.transaction() as uow:
            row = self.message(uow, direction="inbound", purpose="owner")
            identity = new_id()
            uow.repository("telegram_owner_inputs").insert({"id": identity, "purpose": "owner_message",
                "correlation_id": new_id(), "message_id": row["id"], "agent_id": self.agent,
                "owner_principal_id": self.agent, "config_generation": 1, "binding_fingerprint": "f" * 64,
                "target_fingerprint": "t" * 64, "expires_at": expires_at, "created_at": NOW, "updated_at": NOW})
            return identity

    def read(self, sql, *args):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, args)]

    def steps(self):
        return {"requests.expire": self.bridge.dispatcher.expire_requests,
                "dispatch.claim": lambda: self.bridge.dispatcher.dispatch_one(uploads=False),
                "cleanup.claim": self.bridge.cleanup.one,
                "input.reconcile": self.bridge.owner_inputs.reconcile}

    # ----- an idle pass ---------------------------------------------------------------------------------------------

    def test_an_idle_pass_finds_nothing_to_do(self):
        # Work that is not due yet: a request that expires later, a send and a cleanup waiting for their time, an input
        # that expires later.
        self.pending_request(LATER)
        self.queued_send(next_attempt_at=LATER)
        self.queued_cleanup(not_before=LATER)
        self.queued_input(LATER)
        self.units.take()
        for name, step in self.steps().items():
            with self.subTest(step=name):
                self.assertFalse(step())
                self.assertEqual(self.units.take(), [("transaction", "telegram." + name)])
        self.assertEqual(self.api.sent + self.api.deleted, [])

    def test_an_idle_pass_still_refuses_during_maintenance(self):
        with self.database.transaction() as uow:
            uow.repository("maintenance_state").insert({"id": "service", "state": "closed", "message": "closed for a test"})
        for name, step in self.steps().items():
            with self.subTest(step=name), self.assertRaises(DomainError) as raised:
                step()
            self.assertEqual(raised.exception.code, "maintenance")

    def test_an_idle_pass_still_refuses_a_moved_binding(self):
        def moved(uow):
            raise DomainError("binding_changed", "moved")
        self.bridge.check_binding = moved
        for name in ("dispatch.claim", "cleanup.claim"):
            with self.subTest(step=name), self.assertRaises(DomainError) as raised:
                self.steps()[name]()
            self.assertEqual(raised.exception.code, "binding_changed")

    # ----- a pass with work -----------------------------------------------------------------------------------------

    def test_an_expired_request_is_closed(self):
        request = self.pending_request(PAST)
        self.assertTrue(self.bridge.dispatcher.expire_requests())
        self.assertEqual([row["request_state"] for row in self.read("SELECT * FROM telegram_messages WHERE id=?", request)],
                         ["expired"])
        self.assertFalse(self.bridge.dispatcher.expire_requests())

    def test_a_due_send_is_claimed_and_sent(self):
        outbox = self.queued_send()
        self.assertTrue(self.bridge.dispatcher.dispatch_one(uploads=False))
        self.assertEqual(self.api.sent, ["hello"])
        row, = self.read("SELECT * FROM telegram_outbox WHERE id=?", outbox)
        self.assertEqual((row["state"], row["provider_message_id"], row["attempts"]), ("sent", 701, 1))
        self.assertFalse(self.bridge.dispatcher.dispatch_one(uploads=False))

    def test_a_due_cleanup_is_claimed_and_deleted(self):
        never_sent = self.queued_cleanup()
        known = self.queued_cleanup(provider_id=55)
        self.assertTrue(self.bridge.cleanup.one())
        self.assertEqual(self.api.deleted, [55])
        rows = {row["message_id"]: row for row in self.read("SELECT * FROM telegram_cleanup_jobs")}
        self.assertEqual((rows[never_sent]["state"], rows[never_sent]["error_code"]), ("deleted", "never_sent"))
        self.assertEqual(rows[known]["state"], "deleted")
        self.assertFalse(self.bridge.cleanup.one())

    def test_an_expired_input_is_closed(self):
        identity = self.queued_input(PAST)
        self.assertTrue(self.bridge.owner_inputs.reconcile())
        row, = self.read("SELECT * FROM telegram_owner_inputs WHERE id=?", identity)
        self.assertEqual((row["state"], row["error_code"]), ("expired", "expired"))
        self.assertFalse(self.bridge.owner_inputs.reconcile())


if __name__ == "__main__":
    unittest.main()
