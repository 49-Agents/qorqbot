"""The owner's own words over a period, wherever they were written: a read for the owner and the coordinator.

``owner_inputs_list`` lists, oldest first, what the owner wrote in a time window or the owner's last N messages:

* ``chat``: every agent's native conversation. The index (``native_owner_turns``) holds where each turn starts in the
  transcript archive; the text is read from the archive for this answer and is stored nowhere else.
* ``thread``: the private owner–Ordubashy channel of a Task, Signoff, Table, Project or Focus Reference, side chats included.
* ``note``: what the owner wrote personally in a Task or Table discussion, on a Signoff or on a Focus card.

Each item is the exact text with its time and place and, where the records can tell, what followed it: the reply that
came after it and the record changes the addressed agent wrote before the owner's next message there. That is matched by
time and place, not by meaning; whether an ask was met is the reader's judgement.

A source the caller may not read now is left out, exactly as its own read would refuse it. A turn that may be a private
input is returned without text. Nothing is written: no read position moves and nothing is acknowledged.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, schema, utc_clock
from qorqut_operations.contracts import DomainError

from qorqut.inbox.private_conversations import UNAVAILABLE
from qorqut.management.identity import configured_ordubashy, installation_owner
from qorqut.native_archive import CaptureError
from qorqut.native_chat import store
from qorqut.native_chat.projection import SPEAKER_USER
from qorqut.native_chat.reader import ConversationReader

CHANNELS = ("chat", "thread", "note")
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 90
PAGE_LIMIT = 100
DEFAULT_LIMIT = 50
TEXT_LIMIT = 2000
FULL_TEXT_LIMIT = 200_000
PAGE_CHARACTERS = 40_000
PREVIEW = 180
RECORDS_PER_ITEM = 5
RECORD_SCAN = 2000
FIRST, LAST = "", "9999"

LIST_DESCRIPTION = (
    "Owner and coordinator read. The owner's exact messages, oldest first, across every agent's chat (channel chat), the private "
    "owner threads on Tasks, Signoffs, Tables, Projects and Focus References (thread) and the owner's own notes in Task or Table "
    "discussions, on Signoffs and on Focus cards (note). Choose the period with hours (default 24), since/until (ISO-8601 with a "
    "UTC offset) or last_n (the newest N messages). Each item has id, at, channel, via (chat only: chat, telegram, broadcast, "
    "direct_message, or typed for text entered in the agent's terminal), the agent or source it was written to, text (cut at "
    "text_limit, 2000 by default; read the rest with owner_input_get), chars, truncated, redacted (a possibly private input: no "
    "text) and followed_by: the reply that came after it and up to five record changes the addressed agent wrote before the "
    "owner's next message there. followed_by is matched by time and place, not by meaning: read the records before you call an "
    "ask done. unanswered counts the items with no reply after them. chat_covered_from is when chat indexing began; chat "
    "messages before it are not listed. gaps names what could not be read. Follow next_cursor. Changes nothing and marks "
    "nothing read.")
GET_DESCRIPTION = ("Owner and coordinator read. One item of owner_inputs_list by its id, with its full text (up to 200000 "
                   "characters). Changes nothing.")

TIME = {"type": "string", "minLength": 1, "maxLength": 40}
LIST_SCHEMA = schema({
    "scope": {"const": "global"},
    "hours": {"type": "integer", "minimum": 1, "maximum": MAX_HOURS},
    "since": TIME, "until": TIME,
    "last_n": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT},
    "channels": {"type": "array", "items": {"enum": list(CHANNELS)}, "minItems": 1, "uniqueItems": True},
    "agent_id": UUID,
    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT},
    "text_limit": {"type": "integer", "minimum": 200, "maximum": 20_000},
    "cursor": {"type": "string", "maxLength": 2000},
})
GET_SCHEMA = schema({"scope": {"const": "global"}, "id": {"type": "string", "minLength": 6, "maxLength": 200}}, ("id",))

# The owner wrote these personally: a relay recorded by an agent is the agent's account of what the owner said elsewhere.
NOTE_ROWS = """
SELECT 'task-note:' || m.id AS item, m.created_at AS at, m.body AS body, m.item_type AS source_type, m.item_id AS source_id,
       m.business_id AS business_id
  FROM thread_messages m WHERE m.kind='comment' AND m.author_id=:owner AND m.recorder_id=:owner AND m.item_type IN ('task','table')
UNION ALL
SELECT 'signoff-note:' || n.id, n.created_at, n.body, 'signoff', n.signoff_id, s.review_business_id
  FROM signoff_notes n JOIN signoffs s ON s.id=n.signoff_id WHERE n.created_by=:owner
UNION ALL
SELECT 'focus-note:' || n.id, n.created_at, n.body, 'focus_card', n.card_id, NULL FROM focus_notes n WHERE n.created_by=:owner
"""
ROWS = {
    "chat": "SELECT 'chat:' || t.id AS item, t.occurred_at AS at, t.* FROM native_owner_turns t",
    "thread": ("SELECT 'thread:' || m.id AS item, m.created_at AS at, m.body AS body, m.sequence AS sequence, c.id AS conversation_id, "
               "c.source_type AS source_type, c.source_id AS source_id, c.business_id AS business_id "
               "FROM owner_conversation_messages m JOIN owner_conversations c ON c.id=m.conversation_id "
               "WHERE c.peer_agent_id IS NULL AND m.author_role='owner'"),
    "note": NOTE_ROWS,
}


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def moment(text: str, name: str) -> datetime:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("validation_error", f"{name} must be an ISO-8601 timestamp") from None
    if value.tzinfo is None:
        raise DomainError("validation_error", f"{name} must carry a UTC offset")
    return value


def cut(text: str | None, limit: int = PREVIEW) -> str:
    return " ".join((text or "").split())[:limit]


class OwnerInputs:
    def __init__(self, runtime, *, clock=utc_clock):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock

    def register(self):
        common = dict(scope_kinds=frozenset({"global"}), global_capability="read_global_context", effect_kind="read",
                      supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
                      resolve_namespace=lambda ctx, args: "global")
        self.runtime.registry.register(OperationDescriptor("owner_inputs_list", LIST_SCHEMA, description=LIST_DESCRIPTION,
                                                           read=self.list, **common))
        self.runtime.registry.register(OperationDescriptor("owner_input_get", GET_SCHEMA, description=GET_DESCRIPTION,
                                                           read=self.get, **common))
        self.runtime.owner_inputs = self
        return self

    # ----- who may read ----------------------------------------------------------------------------------------------

    def caller(self, ctx, uow):
        """The installation owner or the configured coordinator in person, at Global. A delegated identity acts for its parent
        on one assignment and never reads the owner's words across the installation."""
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        owner, coordinator = installation_owner(uow), configured_ordubashy(uow)
        personal = ctx.delegation_assignment_id is None
        if not personal or not owner or not ((ctx.principal_kind == "human" and ctx.principal_id == owner)
                                              or (ctx.principal_kind == "agent" and coordinator and ctx.principal_id == coordinator)):
            raise DomainError("forbidden", "The owner's messages are read by the owner or the coordinator")
        return _Reading(self, ctx, uow, owner, coordinator)

    # ----- the reads -------------------------------------------------------------------------------------------------

    def list(self, ctx, args, options, uow):
        reading = self.caller(ctx, uow)
        window = self.window(args)
        channels = [name for name in CHANNELS if name in args.get("channels", CHANNELS)]
        limit = args.get("last_n") or args.get("limit", DEFAULT_LIMIT)
        rows = reading.rows(channels, window, limit + 1, args.get("agent_id"), newest="last_n" in args)
        more = len(rows) > limit
        rows = rows[:limit] if "last_n" not in args else rows[-limit:]
        items, used, consumed = [], 0, None
        text_limit = args.get("text_limit", TEXT_LIMIT)
        for position, row in enumerate(rows):
            consumed = row
            item = reading.item(row, text_limit)
            if item is None:
                continue
            items.append(item)
            used += len(item["text"] or "")
            if used >= PAGE_CHARACTERS and position + 1 < len(rows):
                more = True
                break
        reading.follow(items, window["until"])
        items = _merge_broadcasts(items)
        next_cursor = None
        if more and consumed is not None and "last_n" not in args:
            next_cursor = _encode({**window, "after": [consumed["at"], consumed["item"]], "filters": _filters(args)})
        covered = uow.connection.execute("SELECT min(indexed_at) FROM native_owner_turns").fetchone()[0]
        gaps = list(reading.gaps.values())
        if "chat" in channels and (covered is None or window["since"] < covered):
            gaps.append({"code": "chat_not_indexed_before", "at": covered,
                         "message": "Chat messages written before chat indexing began are not listed."})
        return {"since": window["since"], "until": window["until"], "channels": channels, "items": items,
                "unanswered": sum(1 for item in items if not item["redacted"] and item["followed_by"]["reply"] is None),
                "chat_covered_from": covered, "gaps": gaps, "next_cursor": next_cursor}

    def get(self, ctx, args, options, uow):
        reading = self.caller(ctx, uow)
        kind = args["id"].split(":", 1)[0]
        channel = {"chat": "chat", "thread": "thread", "task-note": "note", "signoff-note": "note", "focus-note": "note"}.get(kind)
        if channel is None:
            raise DomainError("validation_error", "id must be an item id that owner_inputs_list returned")
        rows = reading.connection.execute(f"SELECT * FROM ({ROWS[channel]}) WHERE item=:item",
                                          {"owner": reading.owner, "item": args["id"]}).fetchall()
        item = reading.item({**dict(rows[0]), "channel": channel}, FULL_TEXT_LIMIT) if len(rows) == 1 else None
        if item is None:
            if reading.gaps:
                raise DomainError("operation_unavailable", "The text of this message cannot be read from the archive now",
                                  details={"gaps": list(reading.gaps.values())})
            raise DomainError("not_found", "This owner message does not exist or is not readable now")
        reading.follow([item], stamp(self.clock()))
        return {"item": item}

    # ----- the period ------------------------------------------------------------------------------------------------

    def window(self, args):
        """``{since, until, after}`` in the stored UTC text form. A cursor carries the period of its first page, so a window
        given in hours does not slide between pages."""
        if "cursor" in args:
            if "last_n" in args:
                raise DomainError("validation_error", "last_n returns one page; it takes no cursor")
            value = _decode(args["cursor"])
            if value.get("filters") != _filters(args):
                raise DomainError("validation_error", "The cursor belongs to a read with other arguments")
            return {"since": value["since"], "until": value["until"], "after": tuple(value["after"])}
        if "hours" in args and "since" in args:
            raise DomainError("validation_error", "Give hours or since, not both")
        now = self.clock()
        until = moment(args["until"], "until") if "until" in args else now
        if "since" in args:
            since = moment(args["since"], "since")
        elif "hours" in args or "last_n" not in args:
            since = until - timedelta(hours=args.get("hours", DEFAULT_HOURS))
        else:
            since = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if since >= until:
            raise DomainError("validation_error", "since must be earlier than until")
        return {"since": stamp(since), "until": stamp(until), "after": (FIRST, FIRST)}


class _Reading:
    """One answer: the caller's identity, the sources already resolved and the archives already opened."""

    def __init__(self, service, ctx, uow, owner, coordinator):
        self.service, self.ctx, self.uow, self.owner, self.coordinator = service, ctx, uow, owner, coordinator
        self.connection = uow.connection
        self.sources, self.agents, self.archives, self.gaps = {}, {}, {}, {}

    # ----- rows ------------------------------------------------------------------------------------------------------

    def rows(self, channels, window, limit, agent_id, *, newest):
        """The next ``limit`` rows of the period across the channels in time order; the newest ``limit`` for ``last_n``."""
        found = []
        for channel in channels:
            if agent_id is not None and channel != "chat":
                continue
            where = "at>=:since AND at<:until AND (at,item)>(:after_at,:after_item)"
            if channel == "chat" and agent_id is not None:
                where += " AND agent_id=:agent"
            order = "at DESC,item DESC" if newest else "at,item"
            found.extend({**dict(row), "channel": channel} for row in self.connection.execute(
                f"SELECT * FROM ({ROWS[channel]}) WHERE {where} ORDER BY {order} LIMIT :limit",
                {"owner": self.owner, "since": window["since"], "until": window["until"], "after_at": window["after"][0],
                 "after_item": window["after"][1], "agent": agent_id, "limit": limit}))
        found.sort(key=lambda row: (row["at"], row["item"]), reverse=newest)
        found = found[:limit]
        return found[::-1] if newest else found

    # ----- one item --------------------------------------------------------------------------------------------------

    def item(self, row, text_limit):
        """The public item of one row, or ``None`` when its place is not readable now."""
        base = {"id": row["item"], "at": row["at"], "channel": row["channel"]}
        if row["channel"] == "chat":
            agent = self.agent(row["agent_id"])
            if row["redacted"]:
                return {**base, "via": row["via"], "agent": agent, "text": None, "chars": None, "truncated": False,
                        "redacted": True, "followed_by": {"reply": None, "records": []}, "_row": row}
            text = self.turn_text(row)
            if text is None:
                return None
            return {**base, "via": row["via"], "agent": agent, **_text(text, text_limit), "redacted": False, "_row": row}
        source = self.source(row["source_type"], row["source_id"], row["business_id"])
        if source is None:
            return None
        return {**base, "source": source, **_text(row["body"], text_limit), "redacted": False, "_row": row}

    def agent(self, agent_id):
        if agent_id not in self.agents:
            found = store.agent_identity(self.connection, agent_id) or {"agent_id": agent_id, "display_name": None}
            self.agents[agent_id] = {"id": found["agent_id"], "name": found["display_name"],
                                     **({"role": found["role"]["id"]} if found.get("role") else {})}
        return self.agents[agent_id]

    def source(self, kind, identity, business_id):
        """Where a thread message or note was written, through the caller's own read of that record; ``None`` when that read
        is refused or the record is gone."""
        key = (kind, identity, business_id)
        if key not in self.sources:
            self.sources[key] = self._source(kind, identity, business_id)
        return self.sources[key]

    def _source(self, kind, identity, business_id):
        threads = self.service.runtime.private_conversations
        try:
            if kind == "focus_card":
                self.service.app.authorize(self.ctx, self.service.app.registry.get("focus_get"), (), self.uow, namespace="global")
                card = self.connection.execute("SELECT display_name,reference_json FROM focus_cards WHERE id=?", (identity,)).fetchone()
                if card is None:
                    return None
                title = card["display_name"] or (json.loads(card["reference_json"] or "{}").get("title")) or "Focus card"
                return {"type": "focus_card", "id": identity, "business_id": None, "title": title}
            source = {"type": kind, "id": identity, "business_id": business_id}
            from qorqut.inbox.thread_views import project
            _, source = threads.resolve(self.ctx, {"source": source}, self.uow)
            shown = project(threads, self.ctx, source, self.uow)
            return {"type": kind, "id": identity, "business_id": business_id, "title": shown.get("label"),
                    **({"business": shown["business_slug"]} if shown.get("business_slug") else {})}
        except DomainError as error:
            if error.code not in UNAVAILABLE:
                raise
            return None

    def turn_text(self, row):
        """The exact text of one indexed turn, read from the archive at its recorded position."""
        key = (row["agent_id"], row["provider"], row["conversation_id"])
        if key not in self.archives:
            self.archives[key] = self._archive(*key)
        opened = self.archives[key]
        if opened is None:
            return None
        archive, receipt = opened
        try:
            entries = ConversationReader._project(archive, receipt, row["provider"], row["conversation_id"], row["raw_offset"],
                                                  row["raw_offset"] + row["raw_length"])
        except (CaptureError, ValueError) as error:
            self.gap(row["agent_id"], getattr(error, "code", "archive_unavailable"))
            return None
        for entry in entries:
            if entry.speaker == SPEAKER_USER and entry.offset == row["raw_offset"] and entry.key == row["identity_key"]:
                return entry.text
        self.gap(row["agent_id"], "turn_not_found")
        return None

    def _archive(self, agent_id, provider, conversation_id):
        chat = self.service.runtime.native_chat
        # A retired agent's conversations stay readable: the owner's words to it are still part of the period.
        profile = store.active_profile(self.connection, agent_id, any_status=True)
        capture = store.get_capture(self.connection, agent_id, provider, conversation_id)
        if profile is None or capture is None or not capture["receipt_id"]:
            self.gap(agent_id, "conversation_unavailable")
            return None
        try:
            archive = chat.indexer.archive(profile)
            return archive, archive.load_receipt(capture["receipt_id"])
        except (CaptureError, ValueError) as error:
            self.gap(agent_id, getattr(error, "code", "archive_unavailable"))
            return None

    def gap(self, agent_id, code):
        self.gaps.setdefault((agent_id, code), {"code": code, "agent": self.agent(agent_id)})

    # ----- what followed ---------------------------------------------------------------------------------------------

    def follow(self, items, until):
        """Add ``followed_by`` to each item and drop the private row it was built from."""
        spans = []
        for item in items:
            row = item.pop("_row")
            if item["redacted"]:
                continue
            reply, actor, ends = getattr(self, "_after_" + item["channel"])(row)
            item["followed_by"] = {"reply": reply, "records": []}
            if actor is not None:
                spans.append((item, actor, row["at"], ends or until))
        for item, lines in self.records(spans):
            item["followed_by"]["records"] = lines[:RECORDS_PER_ITEM]
            if len(lines) > RECORDS_PER_ITEM:
                item["followed_by"]["more_records"] = len(lines) - RECORDS_PER_ITEM

    def _after_chat(self, row):
        place = (row["agent_id"], row["provider"], row["conversation_id"], row["archive_id"], row["raw_offset"])
        reply = self.connection.execute(
            "SELECT completed_at,preview FROM native_replies WHERE agent_id=? AND provider=? AND conversation_id=? AND archive_id=? "
            "AND raw_offset>? AND (origin='user' OR user_work=1) ORDER BY raw_offset LIMIT 1", place).fetchone()
        following = self.connection.execute(
            "SELECT occurred_at FROM native_owner_turns WHERE agent_id=? AND provider=? AND conversation_id=? AND archive_id=? "
            "AND raw_offset>? ORDER BY raw_offset LIMIT 1", place).fetchone()
        return (_reply(reply["completed_at"], reply["preview"]) if reply else None, row["agent_id"],
                following["occurred_at"] if following else None)

    def _after_thread(self, row):
        reply = self.connection.execute(
            "SELECT created_at,summary,body FROM owner_conversation_messages WHERE conversation_id=? AND sequence>? "
            "AND author_role='coordinator' ORDER BY sequence LIMIT 1", (row["conversation_id"], row["sequence"])).fetchone()
        following = self.connection.execute(
            "SELECT created_at FROM owner_conversation_messages WHERE conversation_id=? AND sequence>? AND author_role='owner' "
            "ORDER BY sequence LIMIT 1", (row["conversation_id"], row["sequence"])).fetchone()
        return (_reply(reply["created_at"], reply["summary"] or reply["body"]) if reply else None, self.coordinator,
                following["created_at"] if following else None)

    def _after_note(self, row):
        kind, identity = row["item"].split(":", 1)[0], row["source_id"]
        if kind == "task-note":
            reply = self.connection.execute(
                "SELECT created_at,summary,body FROM thread_messages WHERE business_id IS ? AND item_type=? AND item_id=? "
                "AND kind='comment' AND created_at>? AND author_id IS NOT ? ORDER BY created_at LIMIT 1",
                (row["business_id"], row["source_type"], identity, row["at"], self.owner)).fetchone()
        else:
            table, column = ("signoff_notes", "signoff_id") if kind == "signoff-note" else ("focus_notes", "card_id")
            reply = self.connection.execute(
                f"SELECT created_at,summary,body FROM {table} WHERE {column}=? AND created_at>? AND created_by IS NOT ? "
                "ORDER BY sequence LIMIT 1", (identity, row["at"], self.owner)).fetchone()
        return (_reply(reply["created_at"], reply["summary"] or reply["body"]) if reply else None), None, None

    def records(self, spans):
        """For each ``(item, actor, from, to)``: the record changes that actor wrote in that span, oldest first. One bounded
        scan of the system lines serves the whole page; a line on a record the caller may not read is left out."""
        if not spans:
            return []
        actors = sorted({actor for _, actor, _, _ in spans})
        marks = ",".join("?" * len(actors))
        lines = self.connection.execute(
            "SELECT business_id,item_type,item_id,body,recorder_id,created_at FROM thread_messages WHERE kind='system' "
            f"AND item_type IN ('task','table') AND recorder_id IN ({marks}) AND created_at>=? AND created_at<? "
            "ORDER BY created_at LIMIT ?",
            (*actors, min(span[2] for span in spans), max(span[3] for span in spans), RECORD_SCAN)).fetchall()
        found = []
        for item, actor, begins, ends in spans:
            written = []
            for line in lines:
                if line["recorder_id"] != actor or not begins <= line["created_at"] < ends:
                    continue
                source = self.source(line["item_type"], line["item_id"], line["business_id"])
                if source is not None:
                    written.append({"at": line["created_at"], "record": source, "change": cut(line["body"], 200)})
            found.append((item, written))
        return found


def _text(text, limit):
    return {"text": text[:limit], "chars": len(text), "truncated": len(text) > limit}


def _reply(at, text):
    return {"at": at, "preview": cut(text)}


def _merge_broadcasts(items):
    """One broadcast reaches every agent as its own turn; list it once, with everyone it was typed to."""
    merged, seen = [], {}
    for item in items:
        key = (item["text"], item["at"][:16]) if item["channel"] == "chat" and item.get("via") == "broadcast" else None
        if key is not None and key in seen:
            seen[key].setdefault("also_to", []).append(item["agent"])
            continue
        if key is not None:
            seen[key] = item
        merged.append(item)
    return merged


def _filters(args):
    """What must stay the same between the pages of one read."""
    kept = {name: args[name] for name in ("channels", "agent_id", "hours", "since", "until") if name in args}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode("ascii")


def _decode(cursor):
    try:
        value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        if (set(value) != {"since", "until", "after", "filters"} or not isinstance(value["after"], list) or len(value["after"]) != 2
                or not all(isinstance(part, str) for part in (value["since"], value["until"], value["filters"], *value["after"]))):
            raise ValueError("cursor")
        return value
    except (ValueError, TypeError, AttributeError):
        raise DomainError("validation_error", "Invalid owner inputs cursor") from None
