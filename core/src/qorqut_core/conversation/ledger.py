"""SQL-only insertion and cursor reads over the permanent conversation ledger."""

from uuid import UUID

from ..agents.registry import get_row
from ..app import Change, Operation
from ..contracts import CoreError, canonical
from ..files.references import reference_record
from ..messaging import records
from .inbox_reference import inbox_references
from .models import BrowserRequestSource, ConversationList, ConversationPage, Entry, FileReferenceSource, MessageSource, NativeSource, Publication, PublishJob


def entry_view(row, native=None, reference=None) -> Entry:
    if row["source_type"] == "browser_request":
        source = BrowserRequestSource(request_id=row["browser_request_id"])
    elif row["source_type"] == "file_reference":
        source = FileReferenceSource(reference=reference)
    elif row["source_type"] == "native":
        source = NativeSource(**{key: row[key] for key in (
            "run_id", "provider", "source_id", "source_epoch", "offset", "length")},
            **{key: native[key] for key in ("sha256", "parser_version", "native_id", "part_index", "timestamp_origin")})
    else:
        source = MessageSource(type=row["source_type"], **{key: row[key] for key in (
            "message_id", "run_id", "provider", "source_id", "source_epoch", "offset", "length")})
    return Entry(**{key: row[key] for key in (
        "entry_id", "agent_id", "conversation_id", "conversation_seq", "kind", "role", "direction",
        "text", "sender_actor_id", "occurred_at", "recorded_at")}, source=source)


def entry_views(tx, rows):
    return [entry_view(row, tx.connection.execute("SELECT * FROM conversation_native WHERE entry_id=?",
        (row["entry_id"],)).fetchone() if row["source_type"] == "native" else None,
        reference_record(tx, row["file_reference_id"]) if row["source_type"] == "file_reference" else None) for row in rows]


def transcript_line(entry: Entry) -> bytes:
    """Public format v1 deliberately excludes the mutable presentation views."""
    return (canonical({"format_version": 1, **entry.model_dump(mode="json", include=set(Entry.model_fields))}) + "\n").encode("utf-8")


class Conversations:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def operations(self):
        return (Operation("conversation_list", ConversationList, False, self.list, ConversationPage,
            prepare=self.app.browsers.prepare_conversation),)

    def append_browser_request(self, tx, request, *, operation_id):
        """The creation prompt/actor are immutable; status is a separate read view."""
        tx.require_write()
        agent = get_row(tx, request["agent_id"])
        cursor = tx.connection.execute("SELECT last_seq FROM conversation_cursors WHERE agent_id=?", (agent["id"],)).fetchone()
        if cursor is None:
            tx.connection.execute("INSERT INTO conversation_cursors VALUES (?,?,0)", (agent["id"], agent["conversation_id"]))
        last = cursor[0] if cursor else 0
        if last == 2**63 - 1:
            raise CoreError("storage_blocked", "Conversation cursor exhausted")
        actor = tx.connection.execute("SELECT kind FROM actors WHERE actor_id=?", (request["requester_actor_id"],)).fetchone()
        entry = Entry(entry_id=self.db.id_factory(), agent_id=agent["id"], conversation_id=agent["conversation_id"],
            conversation_seq=last + 1, kind="browser_request", role="user" if actor["kind"] == "owner" else "agent",
            direction="outgoing", text=request["prompt"], sender_actor_id=request["requester_actor_id"],
            source=BrowserRequestSource(request_id=request["request_id"]), occurred_at=request["created_at"], recorded_at=request["created_at"])
        date = entry.recorded_at[:10]
        tx.connection.execute("UPDATE conversation_cursors SET last_seq=? WHERE agent_id=?", (entry.conversation_seq, agent["id"]))
        tx.connection.execute("""INSERT INTO conversation_entries (entry_id,agent_id,conversation_id,conversation_seq,
            kind,role,direction,text,sender_actor_id,source_type,browser_request_id,occurred_at,recorded_at,recording_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(entry.entry_id), agent["id"], agent["conversation_id"], entry.conversation_seq,
            entry.kind, entry.role, entry.direction, entry.text, str(entry.sender_actor_id), entry.source.type,
            request["request_id"], entry.occurred_at, entry.recorded_at, date))
        if tx.connection.execute("SELECT 1 FROM conversation_publications WHERE agent_id=? AND recording_date=?", (agent["id"], date)).fetchone() is None:
            tx.connection.execute("INSERT INTO conversation_publications (agent_id,recording_date) VALUES (?,?)", (agent["id"], date))
        self.app.jobs.enqueue(tx, "conversation_publish", PublishJob(agent_id=agent["id"], date=date, required_seq=entry.conversation_seq),
            operation_id=operation_id, target_id=entry.agent_id)
        self.db.fault("conversation_after_entry")
        return entry

    def append_native(self, tx, agent_id, item, provenance, *, operation_id=None):
        """Internal SQL-only native insertion; caller verifies the archived bytes."""
        from .native import append_native

        return append_native(self, tx, agent_id, item, provenance, operation_id=operation_id)

    def append_file_reference(self, tx, reference, *, operation_id):
        """Append only the exact saved observation, never an inferred path/card."""
        tx.require_write()
        if reference_record(tx, reference.reference_id) != reference:
            raise CoreError("invalid_transition", "Entry must use the exact saved file reference")
        agent = get_row(tx, reference.agent_id)
        if agent["conversation_id"] != str(reference.conversation_id):
            raise CoreError("target_changed", "File reference conversation binding disagrees")
        existing = tx.connection.execute("SELECT * FROM conversation_entries WHERE file_reference_id=?",
            (str(reference.reference_id),)).fetchone()
        if existing:
            return entry_view(existing, reference=reference)
        cursor = tx.connection.execute("SELECT last_seq FROM conversation_cursors WHERE agent_id=?", (agent["id"],)).fetchone()
        if cursor is None:
            tx.connection.execute("INSERT INTO conversation_cursors VALUES (?,?,0)", (agent["id"], agent["conversation_id"]))
        last_seq = cursor[0] if cursor else 0
        if last_seq == 2**63 - 1:
            raise CoreError("storage_blocked", "Conversation cursor exhausted")
        actor = tx.connection.execute("SELECT kind FROM actors WHERE actor_id=?", (str(reference.actor_id),)).fetchone()
        entry = Entry(entry_id=self.db.id_factory(), agent_id=reference.agent_id, conversation_id=reference.conversation_id,
            conversation_seq=last_seq + 1, kind="file_reference", role="user" if actor["kind"] == "owner" else "agent",
            direction="outgoing", text=reference.label, sender_actor_id=reference.actor_id,
            source=FileReferenceSource(reference=reference), occurred_at=reference.observed_at, recorded_at=reference.recorded_at)
        date = entry.recorded_at[:10]
        tx.connection.execute("UPDATE conversation_cursors SET last_seq=? WHERE agent_id=?", (entry.conversation_seq, agent["id"]))
        tx.connection.execute("""INSERT INTO conversation_entries (entry_id,agent_id,conversation_id,conversation_seq,
            kind,role,direction,text,sender_actor_id,source_type,file_reference_id,occurred_at,recorded_at,recording_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(entry.entry_id), agent["id"], agent["conversation_id"], entry.conversation_seq, entry.kind, entry.role,
             entry.direction, entry.text, str(entry.sender_actor_id), entry.source.type, str(reference.reference_id),
             entry.occurred_at, entry.recorded_at, date))
        if tx.connection.execute("SELECT 1 FROM conversation_publications WHERE agent_id=? AND recording_date=?",
                (agent["id"], date)).fetchone() is None:
            tx.connection.execute("INSERT INTO conversation_publications (agent_id,recording_date) VALUES (?,?)", (agent["id"], date))
        self.app.jobs.enqueue(tx, "conversation_publish", PublishJob(agent_id=agent["id"], date=date,
            required_seq=entry.conversation_seq), operation_id=operation_id, target_id=reference.agent_id)
        self.db.fault("conversation_after_entry")
        return entry

    def append_message(self, tx, agent_id, message, *, operation_id):
        """Internal platform insertion seam; not a public/native assistant writer.

        Identity, role, content and timestamps come from the saved message, and
        the conversation always comes from the registered agent.
        """
        tx.require_write()
        if records.message(tx, message.message_id) != message:
            raise CoreError("invalid_transition", "Entry must use the exact saved message")
        agent = get_row(tx, agent_id)
        sender = message.sender_agent_id == UUID(agent["id"])
        if not sender and message.recipient_agent_id != UUID(agent["id"]):
            raise CoreError("invalid_argument", "Message does not belong to this agent's conversation")
        existing = tx.connection.execute("SELECT * FROM conversation_entries WHERE agent_id=? AND message_id=?",
            (agent["id"], str(message.message_id))).fetchone()
        if existing:
            return entry_view(existing)
        cursor = tx.connection.execute("SELECT last_seq FROM conversation_cursors WHERE agent_id=?", (agent["id"],)).fetchone()
        if cursor is None:
            tx.connection.execute("INSERT INTO conversation_cursors VALUES (?,?,0)", (agent["id"], agent["conversation_id"]))
        last_seq = cursor[0] if cursor else 0
        if last_seq == 2**63 - 1:
            raise CoreError("storage_blocked", "Conversation cursor exhausted")
        entry = Entry(entry_id=self.db.id_factory(), agent_id=agent["id"], conversation_id=agent["conversation_id"],
            conversation_seq=last_seq + 1, kind="message", role="agent" if message.sender_agent_id else "user",
            direction="outgoing" if sender else "incoming", text=message.body, sender_actor_id=message.sender_actor_id,
            source=MessageSource(message_id=message.message_id), occurred_at=message.occurred_at,
            recorded_at=self.db.clock())
        date = entry.recorded_at[:10]
        tx.connection.execute("UPDATE conversation_cursors SET last_seq=? WHERE agent_id=?", (entry.conversation_seq, agent["id"]))
        tx.connection.execute("""INSERT INTO conversation_entries (entry_id,agent_id,conversation_id,conversation_seq,
            kind,role,direction,text,sender_actor_id,source_type,message_id,occurred_at,recorded_at,recording_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(entry.entry_id), agent["id"], agent["conversation_id"], entry.conversation_seq, entry.kind, entry.role,
             entry.direction, entry.text, str(entry.sender_actor_id), entry.source.type, str(message.message_id),
             entry.occurred_at, entry.recorded_at, date))
        if tx.connection.execute("SELECT 1 FROM conversation_publications WHERE agent_id=? AND recording_date=?",
                (agent["id"], date)).fetchone() is None:
            tx.connection.execute("INSERT INTO conversation_publications (agent_id,recording_date) VALUES (?,?)", (agent["id"], date))
        self.app.jobs.enqueue(tx, "conversation_publish", PublishJob(agent_id=agent["id"], date=date,
            required_seq=entry.conversation_seq), operation_id=operation_id, target_id=UUID(agent["id"]))
        self.db.fault("conversation_after_entry")
        return entry

    def publication(self, tx, agent_id):
        pending = tx.connection.execute("""SELECT count(*) FROM conversation_entries e
            JOIN conversation_publications p ON p.agent_id=e.agent_id AND p.recording_date=e.recording_date
            WHERE e.agent_id=? AND e.conversation_seq>p.through_seq""", (str(agent_id),)).fetchone()[0]
        error = tx.connection.execute("""SELECT error_code,error_message FROM conversation_publications
            WHERE agent_id=? AND error_code IS NOT NULL ORDER BY recording_date LIMIT 1""", (str(agent_id),)).fetchone()
        return Publication(pending_entries=pending, state="blocked" if error else "pending" if pending else "current",
            error={"code": error[0], "message": error[1]} if error else None)

    def list(self, tx, args, context, operation_id, *, browser_observation=(None, None)):
        from ..browser.service import request_row, request_view
        agent = get_row(tx, args.agent_id)
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if args.recovery_epoch is not None and str(args.recovery_epoch) != epoch:
            raise CoreError("target_changed", "Conversation cursor belongs to another recovery epoch")
        where, values = "agent_id=?", [agent["id"]]
        if args.after_seq is not None:
            where += " AND conversation_seq>?"
            values.append(args.after_seq)
        elif args.before_seq is not None:
            where += " AND conversation_seq<?"
            values.append(args.before_seq)
        order = "ASC" if args.after_seq is not None else "DESC"
        rows = tx.connection.execute(f"SELECT * FROM conversation_entries WHERE {where} ORDER BY conversation_seq {order} LIMIT ?",
            (*values, args.limit)).fetchall()
        if order == "DESC":
            rows.reverse()
        entries = entry_views(tx, rows)
        references = inbox_references(tx, entries)
        items = [{**entry.model_dump(mode="json"),
            "delivery": records.delivery(tx, row["message_id"]) if row["message_id"] else None,
            "handling": records.handling(tx, row["message_id"]) if row["message_id"] else None,
            "inbox_reference": references.get(entry.entry_id),
            "browser_request": request_view(tx, request_row(tx, row["browser_request_id"]), *browser_observation)
                if row["source_type"] == "browser_request" else None}
            for row, entry in zip(rows, entries)]
        first, last = (rows[0]["conversation_seq"], rows[-1]["conversation_seq"]) if rows else (None, None)
        # Empty pages still tell the caller on which side saved entries exist.
        older = ("conversation_seq<?", first) if rows else (
            "conversation_seq<=?" if args.after_seq is not None else "conversation_seq<?",
            args.after_seq if args.after_seq is not None else args.before_seq or 0)
        newer = ("conversation_seq>?", last) if rows else (
            "conversation_seq>=?" if args.before_seq is not None else "conversation_seq>?",
            args.before_seq if args.before_seq is not None else args.after_seq or 0)
        def exists(bound):
            return tx.connection.execute(f"SELECT 1 FROM conversation_entries WHERE agent_id=? AND {bound[0]} LIMIT 1",
                (agent["id"], bound[1])).fetchone() is not None
        return Change(ConversationPage(agent_id=agent["id"], conversation_id=agent["conversation_id"], recovery_epoch=epoch,
            items=items, first_seq=first, last_seq=last, has_older=exists(older), has_newer=exists(newer),
            publication=self.publication(tx, args.agent_id)))
