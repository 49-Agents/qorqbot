"""The shared discussion of a Task or an Action: the one writer of its messages and the one bounded reader.

A message is append-only. It records the actual recorder (the authenticated principal that wrote it) and the
attributed speaker; they differ only for a supported relay, where the registered coordinator reports an explicit
statement or interpreted intent of the installation owner, or a report from another current global agent. That is attribution, never a grant:
the relay carries no permission to decide, approve or write anything the recorder could not write itself, and the
operation that carries it is authorized for the recorder exactly as for any other comment.

Reading is governed by the item's own authorization, done by the operation that calls ``read_messages``; reading
never follows an item and never acknowledges an inbox event.

Every message has an audience, derived from what is stored and never written twice. A message is for the owner when
the owner wrote it, when it is an owner relay, or when an agent posted it for the owner: explicitly (``for_owner``),
as the outcome of closing the work, or as the first reply after the owner's own message. Everything else, including
every system entry, is a working note: agents read all of it, and a reader may ask for the owner's part only."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json

from qorqut_operations.application.plans import IDENTITY, PAGE, page, page_position, schema
from qorqut_operations.contracts import DomainError, new_id, utc_now
from qorqut_operations.conversations import author, normalize_summary
from qorqut_operations.discussion.follows import ThreadItem
from qorqut_operations.global_agents import global_profile, is_coordinator
from qorqut_operations import owner_text

BODY = {"type": "string", "minLength": 1, "maxLength": 20000}
RELAY_SOURCE = schema({"provider": {"enum": ["claude", "codex", "owner_conversation", "thread", "agent_message"]},
                       "session_id": IDENTITY, "message_id": IDENTITY, "conversation_id": IDENTITY})
RELAY = {"description": instruction_text('threads-optional-attribution-of-a-relayed-statement-the-registered'),
         "oneOf": [
             schema({"kind": {"const": "explicit_owner_statement"}, "owner_id": IDENTITY,
                     "statement_text": {"type": "string", "minLength": 1, "maxLength": 20000}, "source": RELAY_SOURCE},
                    ("kind", "owner_id", "statement_text")),
             schema({"kind": {"const": "owner_intent"}, "owner_id": IDENTITY,
                     "statement_text": {"type": "string", "minLength": 1, "maxLength": 20000}, "source": RELAY_SOURCE},
                    ("kind", "owner_id", "statement_text")),
             schema({"kind": {"const": "agent_report"}, "agent_id": IDENTITY,
                     "report_text": {"type": "string", "minLength": 1, "maxLength": 20000}, "source": RELAY_SOURCE},
                    ("kind", "agent_id", "report_text"))]}
THREAD_READ = {**PAGE}
SELF = {"kind": "self"}
NOTE_REF_LIMIT = 10
OWNER_RELAYS = ("explicit_owner_statement", "owner_intent")
OWNER_POST_LIMIT = 600
FOR_OWNER = {"type": "boolean", "description": (
    "true: this note is for the owner to read: a result, a question, a blocker the owner can remove, or a reply to the "
    f"owner. At most {OWNER_POST_LIMIT} characters, in plain words, with no record identifiers, short IDs, commits or "
    "revision numbers; link records with note_refs and put detail in a linked file. false or omitted: a working note for "
    "the agents, hidden from the owner by default. A note written straight after the owner's own message, and the result "
    "of tasks_complete, are shown to the owner without it when they read that way; otherwise they stay working notes.")}
AUDIENCE = {"enum": ["owner", "all"], "description": (
    "owner: only what is for the owner (the owner's messages, owner relays and notes posted for the owner); the result "
    "also says how many working notes were left out. all (default): every note.")}
# The stored facts that make a message the owner's to read, for a filter over ``thread_messages``. Every term is true
# or false, never unknown, so the filter and its complement together cover every row.
FOR_OWNER_SQL = ("(kind='comment' AND (coalesce(json_extract(attribution_json,'$.audience'),'')='owner' "
                 "OR coalesce(json_extract(attribution_json,'$.kind'),'') IN ('explicit_owner_statement','owner_intent') "
                 "OR EXISTS (SELECT 1 FROM principals p WHERE p.id=thread_messages.author_id AND p.kind='human')))")


@dataclass(frozen=True)
class DiscussionReferences:
    """How a discussion resolves the typed references of its notes: the Task reference ports, shared with Actions.

    ``resolve(ctx, refs, uow, builder=None, *, strict=False)`` resolves references under the caller's own authority;
    ``message(ctx, uow)`` returns the display resolver of one stored message's references."""

    types: tuple[str, ...]
    resolve: Callable
    message: Callable

    @property
    def schema(self) -> dict:
        return {"type": "array", "maxItems": NOTE_REF_LIMIT,
                "items": schema({"type": {"enum": list(self.types)}, "id": IDENTITY}, ("type", "id"))}


def _registered_coordinator(uow, principal_id) -> bool:
    """Whether the principal is the installation's configured, current Global Ordubashy."""
    return is_coordinator(uow.connection, principal_id)


def attribution(ctx, uow, relay: Mapping | None) -> tuple[str, dict]:
    """The attributed speaker and the stored attribution of a comment by ``ctx``'s principal."""
    if relay is None:
        return ctx.principal_id, dict(SELF)
    relay = dict(relay)
    kind = relay.get("kind")
    if (ctx.principal_kind != "agent" or ctx.delegation_assignment_id
            or not _registered_coordinator(uow, ctx.principal_id)):
        raise DomainError("forbidden", "Only the registered coordinator may relay a statement")
    if kind in {"explicit_owner_statement", "owner_intent"}:
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        if owner is None or owner["owner_principal_id"] != relay["owner_id"]:
            raise DomainError("validation_error", "An owner relay must name the registered installation owner")
        stored = {"kind": kind, "owner_id": relay["owner_id"], "statement_text": relay["statement_text"]}
        speaker = relay["owner_id"]
    elif kind == "agent_report":
        # A new report names another current global; a stored report keeps its attribution whatever its speaker became.
        speaker = relay["agent_id"]
        if speaker == ctx.principal_id or global_profile(uow.connection, speaker) is None:
            raise DomainError("validation_error", "An agent report must name another current global agent")
        stored = {"kind": kind, "agent_id": speaker, "report_text": relay["report_text"]}
    else:
        raise DomainError("validation_error", "Unknown relay kind")
    if relay.get("source"):
        stored["source"] = dict(relay["source"])
    return speaker, stored


def save_attribution(ctx, uow, message_type, message_id, relay):
    """Keep represented speech separate from authenticated senders and reply routing."""
    speaker, value = attribution(ctx, uow, relay)
    if relay is not None:
        uow.connection.execute(
            "INSERT INTO message_attributions(message_type,message_id,author_id,recorder_id,attribution_json) VALUES(?,?,?,?,?)",
            (message_type, message_id, speaker, ctx.principal_id, json.dumps(value)))
    return speaker, value


def read_attribution(uow, message_type, message_id, recorder_id):
    row = uow.connection.execute(
        "SELECT * FROM message_attributions WHERE message_type=? AND message_id=?", (message_type, message_id)).fetchone()
    return {"author_id": row["author_id"] if row else recorder_id, "recorder_id": recorder_id,
            "attribution": json.loads(row["attribution_json"]) if row else dict(SELF)}


def owner_post(note: str) -> None:
    """Refuse a note that is posted for the owner but is not written for a person to read."""
    if len(note) > OWNER_POST_LIMIT:
        raise DomainError("validation_error",
                          f"A note for the owner is at most {OWNER_POST_LIMIT} characters: say the result, the question or the "
                          "blocker, and put the detail in a linked file or a working note")
    if owner_text.problem(note):
        raise DomainError("validation_error",
                          "A note for the owner names no record identifiers, short IDs, commits or revision numbers: name "
                          "things in words and link the record with note_refs instead")


def reads_as_owner_post(note: str) -> bool:
    """Whether a note the owner would be shown without asking is written for them; one that is not stays a working note."""
    return len(note) <= OWNER_POST_LIMIT and owner_text.problem(note) is None


def replies_to_owner(uow, item: ThreadItem) -> bool:
    """Whether the newest comment of the thread was written here by a person, so the next agent comment answers it.

    A relayed owner statement does not count: the owner said it somewhere else and is answered there."""
    row = uow.connection.execute(
        "SELECT p.kind, m.author_id=m.recorder_id FROM thread_messages m LEFT JOIN principals p ON p.id=m.author_id "
        "WHERE m.business_id IS ? AND m.item_type=? AND m.item_id=? AND m.kind='comment' "
        "ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (item.business_id, item.item_type, item.item_id)).fetchone()
    return bool(row and row[0] == "human" and row[1])


def audience(view: Mapping) -> str:
    """``owner`` or ``working`` for one projected message."""
    attributed = view.get("attribution") or {}
    if view["kind"] == "comment" and (attributed.get("audience") == "owner" or attributed.get("kind") in OWNER_RELAYS
                                      or view.get("author_kind") == "human"):
        return "owner"
    return "working"


def message_values(ctx, item: ThreadItem, *, body, summary, refs, speaker, attributed, at) -> dict:
    """The row of a comment, for a plan to record. ``summary`` requires its full body, as in v1."""
    summary = normalize_summary(summary)
    if not isinstance(body, str) or not body.strip():
        raise DomainError("validation_error", "A comment cannot be blank")
    identity = new_id()
    return {"id": identity, "item_type": item.item_type, "item_id": item.item_id, "kind": "comment", "body": body,
            "summary": summary, "refs_json": list(refs or ()), "author_id": speaker, "recorder_id": ctx.principal_id,
            "attribution_json": attributed, "created_at": at}


def plan_comment(ctx, uow, builder, item: ThreadItem, *, note, summary, refs, relay, resolve: Callable,
                 guard: Callable | None = None, operation_name: str | None = None, for_owner: bool | None = None,
                 outcome: bool = False) -> dict | None:
    """The one discussion write of an operation on a Task or an Action: its comment, summary and typed references,
    with attribution. Returns the recorded row, or None when the operation carries no note.

    ``resolve(refs)`` resolves the typed references with the caller's own authority (the Task reference ports);
    ``guard()`` is the caller's extra refusal, run once a note is known to be present. ``for_owner`` is the agent's own
    statement of who the note is for; left unsaid, a note that is the ``outcome`` of the operation or that answers the
    owner's latest message is for the owner, and any other is a working note."""
    summary = normalize_summary(summary)
    refs = list(refs or ())
    if summary and not note:
        raise DomainError("validation_error", "A summary requires its full note")
    if refs and not note:
        raise DomainError("validation_error", "Note references require a note")
    if relay and not note:
        raise DomainError("validation_error", "A relayed statement requires its note")
    if not note:
        return None
    if not note.strip():
        raise DomainError("validation_error", "A comment cannot be blank")
    if guard is not None:
        guard()
    resolve(refs)
    speaker, attributed = attribution(ctx, uow, relay)
    if ctx.principal_kind == "agent":
        if for_owner:
            owner_post(note)
            attributed = {**attributed, "audience": "owner"}
        elif (for_owner is None and attributed["kind"] == "self" and reads_as_owner_post(note)
              and (outcome or replies_to_owner(uow, item))):
            attributed = {**attributed, "audience": "owner"}
    values = message_values(ctx, item, body=note, summary=summary, refs=refs, speaker=speaker, attributed=attributed, at=builder.at)
    builder.record("thread_messages", values["id"], None, values, label="comment", operation_name=operation_name)
    return values


def system_entry(uow, item: ThreadItem, *, recorder_id, body, change, event_id=None, at=None) -> str:
    """A derived account of what an operation changed (state, details), written beside the event it produced.

    ``at`` is the operation's own timestamp when its domain keeps one clock for everything it writes; omitted, now."""
    identity = new_id()
    uow.connection.execute(
        "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,attribution_json,change_json,"
        "event_id,created_at) VALUES(?,?,?,?, 'system',?,?, '{\"kind\":\"system\"}',?,?,?)",
        (item.business_id, identity, item.item_type, item.item_id, body, recorder_id, json.dumps(change, sort_keys=True),
         event_id, at or utc_now()))
    return identity


def attach_event(uow, item: ThreadItem, message_ids, event_id) -> None:
    """Bind the messages an operation wrote to the one event it emitted (set once; the row is otherwise immutable)."""
    for identity in message_ids:
        uow.connection.execute(
            "UPDATE thread_messages SET event_id=? WHERE business_id IS ? AND id=? AND event_id IS NULL",
            (event_id, item.business_id, identity))


def message_view(row, uow, resolve_refs: Callable | None = None) -> dict:
    """One message in the shape the retained Task notes had (author projection included), plus the explicit
    attribution fields."""
    view = {key: row[key] for key in ("id", "item_type", "item_id", "kind", "body", "summary", "author_id",
                                       "recorder_id", "event_id", "created_at")}
    view.update(entry_kind="note" if row["kind"] == "comment" else "system", created_by=row["author_id"] or row["recorder_id"],
                attribution=json.loads(row["attribution_json"]), refs=json.loads(row["refs_json"]),
                change=json.loads(row["change_json"]) if row["change_json"] else None)
    if row["kind"] == "comment" and resolve_refs is not None:
        # The stored scope of the references travels beside the message, not inside its public shape.
        view["refs"] = resolve_refs({**view, "reference_namespace": row["reference_namespace"] if "reference_namespace" in row.keys() else None})
    view = author(view, uow)
    view["audience"] = audience(view)
    return view


def read_messages(uow, item: ThreadItem, arguments: Mapping, operation: str, principal_id: str, *,
                  resolve_refs: Callable | None = None, kinds=("comment", "system"), audience: str | None = None) -> dict:
    """One bounded newest-first page, returned oldest-first. The cursor is bound to the item, principal and filters.

    ``audience="owner"`` returns only what is for the owner and adds ``hidden``: how many messages of ``kinds`` in the
    whole thread were left out. Any other value reads everything, as before."""
    owner_only = audience == "owner"
    normalized = {"item_id": item.item_id, "principal_id": principal_id, "limit": min(arguments.get("limit", 20), 100),
                  "kinds": list(kinds), **({"audience": "owner"} if owner_only else {}),
                  **({"cursor": arguments["cursor"]} if arguments.get("cursor") else {})}
    _, after = page_position(normalized, item.business_id, operation)
    bounds = " AND (created_at,id)<(?,?)" if after else ""
    marks = ",".join("?" for _ in kinds)
    scope = f"business_id IS ? AND item_type=? AND item_id=? AND kind IN ({marks})"
    selection = [item.business_id, item.item_type, item.item_id, *kinds]
    rows = uow.connection.execute(
        f"SELECT * FROM thread_messages WHERE {scope}{' AND ' + FOR_OWNER_SQL if owner_only else ''}{bounds} "
        "ORDER BY created_at DESC,id DESC LIMIT ?", [*selection, *(after or ()), normalized["limit"] + 1]).fetchall()
    selected, cursor = page([dict(row) for row in rows], normalized, item.business_id, operation)
    result = {"thread_id": item.thread_id, "messages": [message_view(row, uow, resolve_refs) for row in reversed(selected)],
              "next_cursor": cursor}
    if owner_only:
        result["hidden"] = uow.connection.execute(
            f"SELECT count(*) FROM thread_messages WHERE {scope} AND NOT ({FOR_OWNER_SQL})", selection).fetchone()[0]
    return result


def message_count(uow, item: ThreadItem) -> int:
    return uow.connection.execute(
        "SELECT count(*) FROM thread_messages WHERE business_id IS ? AND item_type=? AND item_id=? AND kind='comment'",
        (item.business_id, item.item_type, item.item_id)).fetchone()[0]
