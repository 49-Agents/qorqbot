"""The conversation between the owner and Ordubashy: a read for the owner and Ordubashy.

``conversation_history_search`` reads it in two ways. Without ``query`` it lists, oldest first, what the owner wrote in a
time window or the owner's last N messages:

* ``chat``: every agent's native conversation. The index (``native_owner_turns``) holds where each turn starts in the
  transcript archive; the text is read from the archive for this answer and is stored nowhere else.
* ``thread``: the private owner–Ordubashy channel of a Task, Signoff, Table, Project or Focus Reference.
* ``note``: what the owner wrote personally in a Task or Table discussion, on a Signoff or on a Focus card.

Each item is the exact text with its time and place and, where the records can tell, what followed it: the reply that
came after it and the record changes the addressed agent wrote before the owner's next message there. That is matched by
time and place, not by meaning; whether an ask was met is the reader's judgement.

With ``query`` it searches both sides of those places over a number of days, newest first: the owner's messages and
Ordubashy's chat replies and thread messages. A reply is read from the archive at the position its completion
(``native_replies``) recorded, exactly as an owner turn is. Only what the chat shows is searched: no tool call, no tool
result, no message between agents and no reply of another agent.

A source the caller may not read now is left out, exactly as its own read would refuse it. A turn that may be a private
input is returned without text. Nothing is written: no read position moves and nothing is acknowledged.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, schema, utc_clock
from qorqut_operations.contracts import DomainError

from qorqut.inbox.private_conversations import UNAVAILABLE
from qorqut.management.identity import configured_ordubashy, installation_owner
from qorqut.native_archive import CaptureError
from qorqut.native_chat import store
from qorqut.native_chat.projection import SPEAKER_AGENT, SPEAKER_USER
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
SEARCH_DAYS = 4
MAX_DAYS = MAX_HOURS // 24
SEARCH_LIMIT = 10
SEARCH_MAX = 50
# The newest messages one search reads. A period that holds more is searched from ``searched_from`` on.
SEARCH_SCAN = 6000
MAX_WORDS = 12
EXCERPT = 240
WORD = re.compile(r"\w+", re.UNICODE)
SIDES = ("owner", "ordubashy")

SEARCH_DESCRIPTION = (
    "Only the owner and Ordubashy may call it. The conversation between the owner and Ordubashy, read in one of two ways. "
    "WITH query it searches: it returns the messages of the period that hold every word of query, newest first. Searched: "
    "the owner's messages in every agent's chat, in the private owner threads (on Tasks, Signoffs, Tables, Projects and Focus "
    "References) and in the owner's own notes; and Ordubashy's chat replies and thread messages. Not "
    "searched: what other agents replied, tool calls and what tools returned, messages between agents, WhatsApp, mail, and "
    "the text of Tasks and Signoffs themselves. query is the few words that must appear, in any "
    "order: a name, a number, a subject. Not a sentence. Case is ignored. A word matches any word that starts with it: "
    "reserv finds reservation, but mail does not find fastmail. The period is days back from now (4 when you give none, 90 "
    "at most), or hours, or since/until (ISO-8601 with a UTC offset). by narrows the search to one side: by: owner returns "
    "only what the owner wrote, by: ordubashy only what Ordubashy wrote. Without by, both are searched and Ordubashy's own "
    "replies are most of the hits. "
    "Each item has id, at, channel (chat, thread or note), "
    "by (owner or ordubashy), the agent whose chat it is or the source it was written on, excerpt (a part of the "
    "text around the first word found) and chars. An excerpt is not the message: read it with conversation_history_get(id) "
    "before you rely on it or quote it. more: true means more messages matched than limit (10 by default, 50 at most): add a "
    "word or shorten the period. searched_from is the oldest moment that was searched. No items means only that no searched "
    "message of that period holds every word. It never means the thing was not said. "
    "WITHOUT query it lists: the owner's exact messages, oldest first, across every agent's chat (channel chat), the private "
    "owner threads (thread) and the owner's own notes in Task or Table discussions, on Signoffs and on Focus cards (note). "
    "Choose the period with hours (default 24), days, since/until or last_n (the newest N messages). Each item has id, at, "
    "channel, by (owner), via (chat only: chat, telegram, broadcast, direct_message, or typed for text entered in the agent's "
    "terminal), the agent or source it was written to, text (cut at text_limit, 2000 by default; read the rest with "
    "conversation_history_get), chars, truncated, redacted (a possibly private input: no text) and followed_by: the reply "
    "that came after it and up to five record changes the addressed agent wrote before the owner's next message there. "
    "followed_by is matched by time and place, not by meaning: read the records before you call an ask done. unanswered "
    "counts the items with no reply after them. Follow next_cursor. "
    "In both: chat_covered_from is when indexing of the owner's chat messages began; the owner's chat messages before it are "
    "not read. gaps names what could not be read. Changes nothing and marks nothing read.")
GET_DESCRIPTION = (
    "Only the owner and Ordubashy may call it. One message by the id conversation_history_search returned, "
    "with its full text (up to 200000 characters). An owner message comes with followed_by. A chat reply comes with after: "
    "the owner message it came after in that chat. Changes nothing.")

TIME = {"type": "string", "minLength": 1, "maxLength": 40}
LIST_SCHEMA = schema({
    "scope": {"const": "global"},
    "query": {"type": "string", "minLength": 1, "maxLength": 200,
              "description": "The few words that must all appear, in any order. Not a sentence. Leave out to list instead."},
    "hours": {"type": "integer", "minimum": 1, "maximum": MAX_HOURS},
    "days": {"type": "integer", "minimum": 1, "maximum": MAX_DAYS},
    "since": TIME, "until": TIME,
    "by": {"enum": list(SIDES), "description": "With query only: search what this side wrote. Leave out for both sides."},
    "last_n": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT},
    "channels": {"type": "array", "items": {"enum": list(CHANNELS)}, "minItems": 1, "uniqueItems": True},
    "agent_id": UUID,
    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT,
              "description": "Without query: messages per page, 50 by default. With query: 10 by default, 50 at most."},
    "text_limit": {"type": "integer", "minimum": 200, "maximum": 20_000},
    "cursor": {"type": "string", "maxLength": 2000},
})
GET_SCHEMA = schema({"scope": {"const": "global"}, "id": {"type": "string", "minLength": 6, "maxLength": 200}}, ("id",))

# The owner wrote these personally: a relay recorded by an agent is the agent's account of what the owner said elsewhere.
NOTE_ROWS = """
SELECT 'task-note:' || m.id AS item, m.created_at AS at, m.body AS body, m.item_type AS source_type, m.item_id AS source_id,
       m.project_id AS project_id
  FROM thread_messages m WHERE m.kind='comment' AND m.author_id=:owner AND m.recorder_id=:owner AND m.item_type IN ('task','table')
UNION ALL
SELECT 'signoff-note:' || n.id, n.created_at, n.body, 'signoff', n.signoff_id, s.review_project_id
  FROM signoff_notes n JOIN signoffs s ON s.id=n.signoff_id WHERE n.created_by=:owner
UNION ALL
SELECT 'focus-note:' || n.id, n.created_at, n.body, 'focus_card', n.card_id, NULL FROM focus_notes n WHERE n.created_by=:owner
"""
THREAD_ROWS = ("SELECT 'thread:' || m.id AS item, m.created_at AS at, m.body AS body, m.sequence AS sequence, c.id AS conversation_id, "
               "c.source_type AS source_type, c.source_id AS source_id, c.project_id AS project_id, m.author_role AS author_role "
               "FROM owner_conversation_messages m JOIN owner_conversations c ON c.id=m.conversation_id "
               "WHERE c.peer_agent_id IS NULL")
ROWS = {
    "chat": "SELECT 'chat:' || t.id AS item, t.occurred_at AS at, t.* FROM native_owner_turns t",
    "thread": THREAD_ROWS + " AND m.author_role='owner'",
    "note": NOTE_ROWS,
}
# Both sides, for the search: the owner's messages and what Ordubashy wrote. Every chat reply of Ordubashy is something the
# owner was told. Another agent's reply is not searched.
SEARCH_ROWS = {
    "chat": ROWS["chat"],
    "reply": ("SELECT 'reply:' || r.id AS item, r.completed_at AS at, r.* FROM native_replies r "
              "WHERE r.agent_id=:coordinator"),
    "thread": THREAD_ROWS,
    "note": NOTE_ROWS,
}
# ``by``: the rows of one side. A kind of row that side never writes is not read at all.
SIDE_ROWS = {
    "owner": {"chat": "", "thread": " AND author_role='owner'", "note": ""},
    "ordubashy": {"reply": "", "thread": " AND author_role<>'owner'"},
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


class Words:
    """The distinct words of one query, case-folded."""

    def __init__(self, query):
        self.all = tuple(dict.fromkeys(word.casefold() for word in WORD.findall(query)))
        if not self.all:
            raise DomainError("validation_error", "query needs at least one word")
        if len(self.all) > MAX_WORDS:
            raise DomainError("validation_error", f"query has more than {MAX_WORDS} words: give the few words that must appear")

    def held(self, text) -> bool:
        """Whether the text holds every word."""
        folded = (text or "").casefold()
        return all(position(folded, word) >= 0 for word in self.all)


def position(folded, word):
    """Where ``word`` first begins a word of the case-folded text, or -1: at the start, or after anything that is not a
    letter or a digit (a space, a hyphen, the underscore of a variable name)."""
    found = folded.find(word)
    while found > 0 and folded[found - 1].isalnum():
        found = folded.find(word, found + 1)
    return found


def excerpt(text, words, width=EXCERPT):
    """About ``width`` characters of ``text`` on one line, around the first of the words found in it."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    folded = flat.casefold()
    found = [place for place in (position(folded, word) for word in words.all) if place >= 0]
    begin = min(max(0, min(found) - width // 4) if found else 0, len(flat) - width)
    return ("…" if begin else "") + flat[begin:begin + width] + ("…" if begin + width < len(flat) else "")


class OwnerInputs:
    def __init__(self, runtime, *, clock=utc_clock):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock

    def register(self):
        common = dict(scope_kinds=frozenset({"global"}), global_capability="read_global_context", effect_kind="read",
                      supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
                      resolve_namespace=lambda ctx, args: "global")
        self.runtime.registry.register(OperationDescriptor("conversation_history_search", LIST_SCHEMA, description=SEARCH_DESCRIPTION,
                                                           read=self.list, **common))
        self.runtime.registry.register(OperationDescriptor("conversation_history_get", GET_SCHEMA, description=GET_DESCRIPTION,
                                                           read=self.get, **common))
        self.runtime.owner_inputs = self
        return self

    # ----- who may read ----------------------------------------------------------------------------------------------

    def caller(self, ctx, uow, *, any_agent=False):
        """The installation owner or the configured coordinator in person, at Global; with ``any_agent``, any agent in person.
        A delegated identity acts for its parent on one assignment and never reads the owner's words across the installation."""
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        owner, coordinator = installation_owner(uow), configured_ordubashy(uow)
        personal = ctx.delegation_assignment_id is None
        agent = ctx.principal_kind == "agent" and (any_agent or coordinator and ctx.principal_id == coordinator)
        if not personal or not owner or not ((ctx.principal_kind == "human" and ctx.principal_id == owner) or agent):
            raise DomainError("forbidden", "The conversation with the owner is read by the owner or Ordubashy")
        return _Reading(self, ctx, uow, owner, coordinator)

    # ----- the reads -------------------------------------------------------------------------------------------------

    def list(self, ctx, args, options, uow):
        reading = self.caller(ctx, uow)
        if "query" in args:
            return self.search(reading, args)
        if "by" in args:
            raise DomainError("validation_error", "by belongs to a search. Without query, the owner's messages are listed")
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
        covered, gaps = self.coverage(reading, channels, window)
        return {"since": window["since"], "until": window["until"], "channels": channels, "items": items,
                "unanswered": sum(1 for item in items if not item["redacted"] and item["followed_by"]["reply"] is None),
                "chat_covered_from": covered, "gaps": gaps, "next_cursor": next_cursor}

    @staticmethod
    def coverage(reading, channels, window):
        """``(chat_covered_from, gaps)``: when indexing of the owner's chat messages began, and what could not be read."""
        covered = reading.connection.execute("SELECT min(indexed_at) FROM native_owner_turns").fetchone()[0]
        gaps = list(reading.gaps.values())
        if "chat" in channels and (covered is None or window["since"] < covered):
            gaps.append({"code": "chat_not_indexed_before", "at": covered,
                         "message": "The owner's chat messages written before chat indexing began are not read."})
        return covered, gaps

    def search(self, reading, args):
        """The messages of the period, on both sides, that hold every word of the query: the newest first."""
        for name in ("last_n", "cursor"):
            if name in args:
                raise DomainError("validation_error", f"{name} belongs to the list. With query, give days, hours or since/until")
        words = Words(args["query"])
        limit = args.get("limit", SEARCH_LIMIT)
        if limit > SEARCH_MAX:
            raise DomainError("validation_error", f"With query, limit is at most {SEARCH_MAX}")
        window = self.window(args)
        channels = [name for name in CHANNELS if name in args.get("channels", CHANNELS)]
        rows = reading.rows(channels, window, SEARCH_SCAN + 1, args.get("agent_id"), newest=True, both=True, by=args.get("by"))
        capped = len(rows) > SEARCH_SCAN
        rows = rows[-SEARCH_SCAN:]
        items, more, broadcasts = [], False, set()
        for row in reversed(rows):
            text = reading.text(row)
            if text is None or not words.held(text):
                continue
            if row["channel"] == "chat" and not row["reply"] and row["via"] == "broadcast":
                # One broadcast is a turn in every agent's chat: one hit.
                if (text, row["at"][:16]) in broadcasts:
                    continue
                broadcasts.add((text, row["at"][:16]))
            if len(items) == limit:
                more = True
                break
            hit = reading.hit(row, text, words)
            if hit is not None:
                items.append(hit)
        covered, gaps = self.coverage(reading, channels, window)
        # ``searched_from`` is later than ``since`` only when the period held more messages than one search reads.
        return {"query": args["query"], "words": list(words.all), "by": args.get("by", "both"), "since": window["since"],
                "until": window["until"], "channels": channels, "order": "newest_first", "items": items, "more": more,
                "searched_from": rows[0]["at"] if capped else window["since"], "chat_covered_from": covered, "gaps": gaps}

    def get(self, ctx, args, options, uow):
        reading = self.caller(ctx, uow)
        kind = args["id"].split(":", 1)[0]
        channel = {"chat": "chat", "reply": "reply", "thread": "thread", "task-note": "note", "signoff-note": "note",
                   "focus-note": "note"}.get(kind)
        if channel is None:
            raise DomainError("validation_error", "id must be an item id that conversation_history_search returned")
        # A thread message of either side has an id; the other channels are read as the list reads them.
        table = SEARCH_ROWS if channel in ("reply", "thread") else ROWS
        rows = reading.connection.execute(f"SELECT * FROM ({table[channel]}) WHERE item=:item",
                                          {"owner": reading.owner, "coordinator": reading.coordinator, "item": args["id"]}).fetchall()
        row = {**dict(rows[0]), "channel": "chat" if channel == "reply" else channel, "reply": channel == "reply"} if len(rows) == 1 else None
        item = None if row is None else reading.reply(row, FULL_TEXT_LIMIT) if row["reply"] else reading.item(row, FULL_TEXT_LIMIT)
        if item is None:
            if reading.gaps:
                raise DomainError("operation_unavailable", "The text of this message cannot be read from the archive now",
                                  details={"gaps": list(reading.gaps.values())})
            raise DomainError("not_found", "This message does not exist or is not readable now")
        if item["by"] == "owner":
            reading.follow([item], stamp(self.clock()))
        else:
            item.pop("_row", None)
        return {"item": item}

    def addressed(self, ctx, uow, *, since, until, ids=(), limit=PAGE_LIMIT):
        """The owner's messages to the calling agent itself, oldest first: its own chat and, for the coordinator, the private
        owner threads. The newest ``limit`` of each written in ``[since, until)``, and the ones named by ``ids`` whenever
        they were written. Each is ``{id, at, channel, place, text, redacted}``; ``text`` is None for a possibly private
        input and for a message that cannot be read now, so a message never drops out of the list for that. For any agent in
        person, and for the owner, whose messages are the coordinator's. A compaction handoff answers for exactly these."""
        reading = self.caller(ctx, uow, any_agent=True)
        agent = reading.ctx.principal_id if reading.ctx.principal_kind == "agent" else reading.coordinator
        window = {"since": since, "until": until, "after": (FIRST, FIRST)}
        sources = (("chat", agent), ("thread", None)) if agent == reading.coordinator else (("chat", agent),)
        rows = {row["item"]: row for channel, owner_of in sources
                for row in reading.rows([channel], window, limit, owner_of, newest=True)}
        for identity in ids:
            channel = identity.split(":", 1)[0]
            if identity in rows or channel not in dict(sources):
                continue
            own = " AND agent_id=:agent" if channel == "chat" and agent != reading.coordinator else ""
            found = reading.connection.execute(f"SELECT * FROM ({ROWS[channel]}) WHERE item=:item{own}",
                                               {"owner": reading.owner, "item": identity, "agent": agent}).fetchall()
            if len(found) == 1:
                rows[identity] = {**dict(found[0]), "channel": channel}
        return [reading.message(row) for row in sorted(rows.values(), key=lambda row: (row["at"], row["item"]))]

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
        if sum(name in args for name in ("hours", "days", "since")) > 1:
            raise DomainError("validation_error", "Give one of hours, days or since")
        now = self.clock()
        until = moment(args["until"], "until") if "until" in args else now
        if "since" in args:
            since = moment(args["since"], "since")
        elif "days" in args:
            since = until - timedelta(days=args["days"])
        elif "hours" in args or "last_n" not in args:
            # A search looks back over days; the list is of the last day.
            since = until - timedelta(hours=args.get("hours", SEARCH_DAYS * 24 if "query" in args else DEFAULT_HOURS))
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

    def rows(self, channels, window, limit, agent_id, *, newest, both=False, by=None):
        """The next ``limit`` rows of the period across the channels in time order; the newest ``limit`` for ``last_n``.
        ``both`` reads both sides of the conversation: Ordubashy's chat replies and thread messages too. ``by``
        keeps one side of them."""
        table, found = SEARCH_ROWS if both else ROWS, []
        for channel in channels:
            if agent_id is not None and channel != "chat":
                continue
            where = "at>=:since AND at<:until AND (at,item)>(:after_at,:after_item)"
            if channel == "chat" and agent_id is not None:
                where += " AND agent_id=:agent"
            order = "at DESC,item DESC" if newest else "at,item"
            for name in (("chat", "reply") if both and channel == "chat" else (channel,)):
                if by is not None and name not in SIDE_ROWS[by]:
                    continue
                side = SIDE_ROWS[by][name] if by is not None else ""
                found.extend({**dict(row), "channel": channel, "reply": name == "reply"} for row in self.connection.execute(
                    f"SELECT * FROM ({table[name]}) WHERE {where}{side} ORDER BY {order} LIMIT :limit",
                    {"owner": self.owner, "coordinator": self.coordinator, "since": window["since"], "until": window["until"],
                     "after_at": window["after"][0], "after_item": window["after"][1], "agent": agent_id, "limit": limit}))
        found.sort(key=lambda row: (row["at"], row["item"]), reverse=newest)
        found = found[:limit]
        return found[::-1] if newest else found

    # ----- one item --------------------------------------------------------------------------------------------------

    def item(self, row, text_limit):
        """The public item of one row, or ``None`` when its place is not readable now."""
        base = {"id": row["item"], "at": row["at"], "channel": row["channel"], "by": side(row)}
        if row["channel"] == "chat":
            agent = self.agent(row["agent_id"])
            if row["redacted"]:
                return {**base, "via": row["via"], "agent": agent, "text": None, "chars": None, "truncated": False,
                        "redacted": True, "followed_by": {"reply": None, "records": []}, "_row": row}
            text = self.turn_text(row)
            if text is None:
                return None
            return {**base, "via": row["via"], "agent": agent, **_text(text, text_limit), "redacted": False, "_row": row}
        source = self.source(row["source_type"], row["source_id"], row["project_id"])
        if source is None:
            return None
        return {**base, "source": source, **_text(row["body"], text_limit), "redacted": False, "_row": row}

    def message(self, row):
        """One row as a compaction handoff lists it: where it was written and its whole text."""
        item = self.item(row, FULL_TEXT_LIMIT) or {}
        source = item.get("source")
        return {"id": row["item"], "at": row["at"], "channel": row["channel"],
                "place": source and {key: source[key] for key in ("type", "id", "project_id", "title")},
                "text": item.get("text"), "redacted": bool(item.get("redacted"))}

    def agent(self, agent_id):
        if agent_id not in self.agents:
            found = store.agent_identity(self.connection, agent_id) or {"agent_id": agent_id, "display_name": None}
            self.agents[agent_id] = {"id": found["agent_id"], "name": found["display_name"],
                                     **({"role": found["role"]["id"]} if found.get("role") else {})}
        return self.agents[agent_id]

    def source(self, kind, identity, project_id):
        """Where a thread message or note was written, through the caller's own read of that record; ``None`` when that read
        is refused or the record is gone."""
        key = (kind, identity, project_id)
        if key not in self.sources:
            self.sources[key] = self._source(kind, identity, project_id)
        return self.sources[key]

    def _source(self, kind, identity, project_id):
        threads = self.service.runtime.private_conversations
        try:
            if kind == "focus_card":
                self.service.app.authorize(self.ctx, self.service.app.registry.get("focus_get"), (), self.uow, namespace="global")
                card = self.connection.execute("SELECT display_name,reference_json FROM focus_cards WHERE id=?", (identity,)).fetchone()
                if card is None:
                    return None
                title = card["display_name"] or (json.loads(card["reference_json"] or "{}").get("title")) or "Focus card"
                return {"type": "focus_card", "id": identity, "project_id": None, "title": title}
            source = {"type": kind, "id": identity, "project_id": project_id}
            from qorqut.inbox.thread_views import project
            _, source = threads.resolve(self.ctx, {"source": source}, self.uow)
            shown = project(threads, self.ctx, source, self.uow)
            return {"type": kind, "id": identity, "project_id": project_id, "title": shown.get("label"),
                    **({"project": shown["project_slug"]} if shown.get("project_slug") else {})}
        except DomainError as error:
            if error.code not in UNAVAILABLE:
                raise
            return None

    def entries(self, row):
        """The chat entries of one indexed position, read from the archive as exactly those bytes; ``None`` when it cannot be
        read now."""
        key = (row["agent_id"], row["provider"], row["conversation_id"])
        if key not in self.archives:
            self.archives[key] = self._archive(*key)
        opened = self.archives[key]
        if opened is None:
            return None
        archive, receipt = opened
        try:
            return ConversationReader._project(archive, receipt, row["provider"], row["conversation_id"], row["raw_offset"],
                                               row["raw_offset"] + row["raw_length"], bounded=True)
        except (CaptureError, ValueError) as error:
            self.gap(row["agent_id"], getattr(error, "code", "archive_unavailable"))
            return None

    def turn_text(self, row):
        """The exact text of one indexed turn, read from the archive at its recorded position."""
        entries = self.entries(row)
        if entries is None:
            return None
        for entry in entries:
            if entry.speaker == SPEAKER_USER and entry.offset == row["raw_offset"] and entry.key == row["identity_key"]:
                return entry.text
        self.gap(row["agent_id"], "turn_not_found")
        return None

    def reply_text(self, row):
        """The text of one chat reply, read from the archive at the position its completion recorded."""
        entries = self.entries(row)
        if entries is None:
            return None
        for entry in entries:
            if entry.speaker == SPEAKER_AGENT and entry.offset == row["raw_offset"] and entry.text:
                return entry.text
        self.gap(row["agent_id"], "reply_not_found")
        return None

    # ----- both sides, for the search --------------------------------------------------------------------------------

    def text(self, row):
        """The text of one row of the search, or ``None`` when there is none to read now."""
        if row["channel"] != "chat":
            return row["body"]
        if row["reply"]:
            return self.reply_text(row)
        return None if row["redacted"] else self.turn_text(row)

    def asked(self, row):
        """The owner's turn a chat reply came after in its conversation, or ``None``."""
        found = self.connection.execute(
            "SELECT 'chat:' || t.id AS item, t.occurred_at AS at, t.* FROM native_owner_turns t WHERE t.agent_id=? AND t.provider=? "
            "AND t.conversation_id=? AND t.archive_id=? AND t.raw_offset<? ORDER BY t.raw_offset DESC LIMIT 1",
            (row["agent_id"], row["provider"], row["conversation_id"], row["archive_id"], row["raw_offset"])).fetchone()
        return dict(found) if found else None

    def hit(self, row, text, words):
        """One search hit, or ``None`` when its place is not readable now. A reply that came after a possibly private input
        may repeat it, so it is not returned either."""
        base = {"id": row["item"], "at": row["at"], "channel": row["channel"]}
        found = {"excerpt": excerpt(text, words), "chars": len(text)}
        if row["channel"] == "chat":
            if not row["reply"]:
                return {**base, "by": "owner", "via": row["via"], "agent": self.agent(row["agent_id"]), **found}
            turn = self.asked(row)
            if turn is not None and turn["redacted"]:
                return None
            return {**base, "by": "ordubashy", "agent": self.agent(row["agent_id"]), **found}
        source = self.source(row["source_type"], row["source_id"], row["project_id"])
        if source is None:
            return None
        return {**base, "by": side(row), "source": source, **found}

    def reply(self, row, text_limit):
        """One chat reply in full with the owner message it came after, or ``None`` when it cannot be returned now."""
        turn = self.asked(row)
        if turn is not None and turn["redacted"]:
            return None
        text = self.reply_text(row)
        if text is None:
            return None
        after = None
        if turn is not None:
            asked = self.turn_text(turn)
            after = {"id": turn["item"], "at": turn["at"], "preview": None if asked is None else cut(asked)}
        return {"id": row["item"], "at": row["at"], "channel": "chat", "by": "ordubashy", "agent": self.agent(row["agent_id"]),
                **_text(text, text_limit), "redacted": False, "after": after}

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
                "SELECT created_at,summary,body FROM thread_messages WHERE project_id IS ? AND item_type=? AND item_id=? "
                "AND kind='comment' AND created_at>? AND author_id IS NOT ? ORDER BY created_at LIMIT 1",
                (row["project_id"], row["source_type"], identity, row["at"], self.owner)).fetchone()
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
            "SELECT project_id,item_type,item_id,body,recorder_id,created_at FROM thread_messages WHERE kind='system' "
            f"AND item_type IN ('task','table') AND recorder_id IN ({marks}) AND created_at>=? AND created_at<? "
            "ORDER BY created_at LIMIT ?",
            (*actors, min(span[2] for span in spans), max(span[3] for span in spans), RECORD_SCAN)).fetchall()
        found = []
        for item, actor, begins, ends in spans:
            written = []
            for line in lines:
                if line["recorder_id"] != actor or not begins <= line["created_at"] < ends:
                    continue
                source = self.source(line["item_type"], line["item_id"], line["project_id"])
                if source is not None:
                    written.append({"at": line["created_at"], "record": source, "change": cut(line["body"], 200)})
            found.append((item, written))
        return found


def side(row):
    """Who wrote a row: ``owner`` or ``ordubashy``. The thread table stores Ordubashy's messages under the role name
    ``coordinator``; the answer names him."""
    if row["channel"] == "chat":
        return "ordubashy" if row.get("reply") else "owner"
    return "owner" if (row.get("author_role") or "owner") == "owner" else "ordubashy"


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
    kept = {name: args[name] for name in ("channels", "agent_id", "hours", "days", "since", "until") if name in args}
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
        raise DomainError("validation_error", "Invalid conversation history cursor") from None
