"""Transactional messages and handling; delivery truthfully awaits runtime integration."""

from uuid import UUID

from ..agents.registry import get_row
from ..app import Change, Operation
from ..contracts import CoreError, JobProgress, JobResolution
from ..storage.events import EventInput, append_event
from ..storage.jobs import JobHandler, after
from . import records
from .models import (
    DeliveriesGet, DeliveryResult, EntryReference, HandlingView, MessageDeliveryJob,
    MessagePage, MessageSent, MessagesAcknowledge, MessagesHandled, MessagesList, MessagesSend,
)


class Messaging:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def operations(self):
        return (
            Operation("messages_send", MessagesSend, True, self.send, MessageSent),
            Operation("messages_list", MessagesList, False, self.list, MessagePage),
            Operation("messages_acknowledge", MessagesAcknowledge, True, self.acknowledge, MessagesHandled),
            Operation("deliveries_get", DeliveriesGet, False, self.deliveries_get, DeliveryResult),
        )

    def handler(self):
        # External intent remains recovery-held even though this increment never
        # attempts terminal input. A future dispatcher must retain this store.
        return JobHandler("message_delivery", MessageDeliveryJob, True, self._execute_delivery, self._inspect_delivery)

    def send(self, tx, args, context, operation_id):
        recipient = get_row(tx, args.recipient_agent_id)
        actor = tx.connection.execute("SELECT * FROM actors WHERE actor_id=?", (str(context.actor_id),)).fetchone()
        sender = get_row(tx, actor["agent_id"]) if actor["agent_id"] is not None else None
        thread_id = args.thread_id
        if args.reply_to_message_id is not None:
            parent = records.message(tx, args.reply_to_message_id)
            if thread_id is not None and parent.thread_id != thread_id:
                raise CoreError("invalid_argument", "Reply and supplied thread disagree")
            thread_id = parent.thread_id
        now = self.db.clock()
        if thread_id is None:
            thread_id = self.db.id_factory()
            tx.connection.execute("INSERT INTO message_threads VALUES (?,?,?,?)",
                (str(thread_id), str(context.actor_id), recipient["actor_id"], now))
        else:
            thread = records.thread(tx, thread_id)
            endpoints = (str(context.actor_id), recipient["actor_id"])
            if endpoints not in ((thread["endpoint_a_actor_id"], thread["endpoint_b_actor_id"]),
                                 (thread["endpoint_b_actor_id"], thread["endpoint_a_actor_id"])):
                raise CoreError("invalid_argument", "Thread endpoints do not match this sender and recipient")
        message_id = self.db.id_factory()
        tx.connection.execute("""INSERT INTO messages (message_id,thread_id,reply_to_message_id,sender_actor_id,
            sender_agent_id,recipient_agent_id,body,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(message_id), str(thread_id), str(args.reply_to_message_id) if args.reply_to_message_id else None,
             str(context.actor_id), sender["id"] if sender else None, recipient["id"], args.body, now, now))
        unsafe = any((ord(char) < 0x20 and char not in "\n\t") or 0x7f <= ord(char) <= 0x9f for char in args.body)
        state, reason = ("failed", "unsafe_terminal_text") if unsafe else ("pending", "runtime_unavailable")
        tx.connection.execute("INSERT INTO message_deliveries VALUES (?,?,?,?,?)", (str(message_id), recipient["id"], state, reason, now))
        message = records.message(tx, message_id)
        targets = [sender["id"]] if sender else []
        if recipient["id"] not in targets:
            targets.append(recipient["id"])
        entries = [self.app.conversations.append_message(tx, target, message, operation_id=operation_id) for target in targets]
        if not unsafe:
            self.app.jobs.enqueue(tx, "message_delivery", MessageDeliveryJob(message_id=message_id),
                operation_id=operation_id, target_id=args.recipient_agent_id)
        seq = append_event(tx, EventInput(kind="message.saved", actor_id=context.actor_id, agent_id=args.recipient_agent_id,
            operation_id=operation_id, payload={"message_id": str(message_id), "thread_id": str(thread_id),
                "sender_agent_id": sender["id"] if sender else None, "recipient_agent_id": recipient["id"],
                "delivery_state": state, "delivery_reason": reason}))
        self.db.fault("message_after_event")
        return Change(MessageSent(message=message, delivery=records.delivery(tx, message_id),
            entries=[EntryReference(**e.model_dump(include=set(EntryReference.model_fields))) for e in entries]), seq, "accepted")

    def acknowledge(self, tx, args, context, operation_id):
        receipts = []
        # Validate the complete batch before inserting even its first receipt.
        for message_id in args.message_ids:
            records.message(tx, message_id)
            previous = records.handling(tx, message_id)
            if previous and (previous.handled_by != context.actor_id or previous.note != args.note):
                raise CoreError("invalid_transition", "Message already has a different immutable handling receipt")
            receipts.append(previous)
        seq = None
        for index, message_id in enumerate(args.message_ids):
            if receipts[index] is not None:
                continue
            receipt = HandlingView(message_id=message_id, handled_by=context.actor_id, handled_at=self.db.clock(), note=args.note)
            tx.connection.execute("INSERT INTO message_handling VALUES (?,?,?,?)",
                (str(message_id), str(receipt.handled_by), receipt.handled_at, receipt.note))
            receipts[index] = receipt
            seq = append_event(tx, EventInput(kind="message.handled", actor_id=context.actor_id, operation_id=operation_id,
                payload={"message_id": str(message_id), "handled_by": str(context.actor_id)}))
        self.db.fault("message_after_handling")
        return Change(MessagesHandled(items=receipts), seq)

    def deliveries_get(self, tx, args, context, operation_id):
        records.message(tx, args.message_id)
        return Change(DeliveryResult(delivery=records.delivery(tx, args.message_id), attempts=[]))

    def list(self, tx, args, context, operation_id):
        agent_id = args.agent_id
        if agent_id is None and args.thread_id is None:
            actor = tx.connection.execute("SELECT agent_id FROM actors WHERE actor_id=?", (str(context.actor_id),)).fetchone()
            if actor[0] is None:
                raise CoreError("invalid_argument", "An owner message list requires an agent or thread filter")
            agent_id = UUID(actor[0])
        return self._page(tx, args, agent_id)

    def _page(self, tx, args, agent_id):
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if args.recovery_epoch is not None and str(args.recovery_epoch) != epoch:
            raise CoreError("target_changed", "Message cursor belongs to another recovery epoch")
        where, values = ["m.message_seq>?"], [args.after_seq]
        if agent_id is not None:
            get_row(tx, agent_id)
            where.append("(m.sender_agent_id=? OR m.recipient_agent_id=?)")
            values.extend((str(agent_id), str(agent_id)))
        if args.thread_id is not None:
            records.thread(tx, args.thread_id)
            where.append("m.thread_id=?")
            values.append(str(args.thread_id))
        if args.unhandled_only:
            where.append("NOT EXISTS (SELECT 1 FROM message_handling h WHERE h.message_id=m.message_id)")
        rows = tx.connection.execute("SELECT m.* FROM messages m WHERE " + " AND ".join(where) + " ORDER BY m.message_seq LIMIT ?",
            (*values, args.limit + 1)).fetchall()
        items = [{"message": dict(row), "delivery": records.delivery(tx, row["message_id"]),
            "handling": records.handling(tx, row["message_id"])} for row in rows[:args.limit]]
        return Change(MessagePage(items=items, next_after_seq=items[-1]["message"]["message_seq"] if items else args.after_seq,
            has_more=len(rows) > args.limit, recovery_epoch=epoch))

    def _delivery_resolution(self, tx, job, claim=None):
        if claim is not None:
            self.app.jobs._current_claim(tx, claim)
        else:
            current = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if current is None or dict(current) != job:
                raise CoreError("target_changed", "Delivery job changed during inspection")
        args = MessageDeliveryJob.model_validate_json(job["args_json"])
        message = records.message(tx, args.message_id)
        get_row(tx, message.recipient_agent_id)
        if job["kind"] != "message_delivery" or job["target_id"] != str(message.recipient_agent_id):
            raise CoreError("target_changed", "Delivery job target disagrees")
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        held = job["wait_reason"] == "recovery_review" or job["recovery_epoch"] != epoch
        return JobResolution(state="waiting", reason="recovery_review" if held else "dependency_unavailable",
            next_attempt_at=after(self.db.clock(), 60), progress=JobProgress(phase="recovery_held" if held else "runtime_unavailable"),
            error=CoreError("uncertain_effect" if held else "unsupported_capability",
                "Message delivery is held for recovery review" if held else "Message saved; runtime dispatch is not implemented").error)

    def _execute_delivery(self, claim):
        with self.db.read() as tx:
            job = dict(self.app.jobs._current_claim(tx, claim))
            return self._delivery_resolution(tx, job, claim)

    def _inspect_delivery(self, job):
        with self.db.read() as tx:
            return self._delivery_resolution(tx, job)

    def hold_for_recovery(self, tx, context, operation_id):
        """Called within explicit restore activation; never release a held delivery."""
        tx.require_write()
        rows = tx.connection.execute("SELECT * FROM message_deliveries WHERE state IN ('pending','deferred') AND reason IS NOT 'recovery_review'").fetchall()
        for row in rows:
            tx.connection.execute("UPDATE message_deliveries SET state='deferred',reason='recovery_review',updated_at=? WHERE message_id=?",
                (self.db.clock(), row["message_id"]))
            append_event(tx, EventInput(kind="message.delivery_changed", actor_id=context.actor_id,
                agent_id=row["recipient_agent_id"], operation_id=operation_id, payload={"message_id": row["message_id"],
                    "old_state": row["state"], "old_reason": row["reason"], "state": "deferred", "reason": "recovery_review"}))
