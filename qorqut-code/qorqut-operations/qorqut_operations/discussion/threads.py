"""The shared discussion of a Task or an Action: the one writer of its messages and the one bounded reader.

A message is append-only. It records the actual recorder (the authenticated principal that wrote it) and the
attributed speaker; they differ only for a supported relay, where the registered coordinator reports an explicit
statement of the installation owner or a report from another registered agent. That is attribution, never a grant:
the relay carries no permission to decide, approve or write anything the recorder could not write itself, and the
operation that carries it is authorized for the recorder exactly as for any other comment.

Reading is governed by the item's own authorization, done by the operation that calls ``read_messages``; reading
never follows an item and never acknowledges an inbox event."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json

from qorqut_operations.application.plans import IDENTITY, PAGE, page, page_position, schema
from qorqut_operations.contracts import DomainError, new_id, utc_now
from qorqut_operations.conversations import author, normalize_summary
from qorqut_operations.discussion.follows import ThreadItem
from qorqut_operations.roles import ORDUBASHY_ROLE

BODY = {"type": "string", "minLength": 1, "maxLength": 20000}
RELAY_SOURCE = schema({"provider": {"enum": ["claude", "codex"]}, "session_id": IDENTITY, "message_id": IDENTITY})
RELAY = {"description": "Optional attribution of a relayed statement. The registered coordinator may record an explicit "
         "owner statement, or report what another registered agent said. The recorder is always kept; this is "
         "attribution only and never approval.",
         "oneOf": [
             schema({"kind": {"const": "explicit_owner_statement"}, "owner_id": IDENTITY,
                     "statement_text": {"type": "string", "minLength": 1, "maxLength": 20000}, "source": RELAY_SOURCE},
                    ("kind", "owner_id", "statement_text")),
             schema({"kind": {"const": "agent_report"}, "agent_id": IDENTITY,
                     "report_text": {"type": "string", "minLength": 1, "maxLength": 20000}, "source": RELAY_SOURCE},
                    ("kind", "agent_id", "report_text"))]}
THREAD_READ = {**PAGE}
SELF = {"kind": "self"}
NOTE_REF_LIMIT = 10


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
    row = uow.connection.execute(
        "SELECT a.profile_json FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
        "WHERE a.agent_id=? AND a.status='active' AND p.active=1 AND p.kind='agent'", (principal_id,)).fetchone()
    if row is None:
        return False
    profile = json.loads(row["profile_json"])
    return profile.get("role", {}).get("id") == ORDUBASHY_ROLE and profile.get("scope", {}).get("kind") == "ecosystem"


def attribution(ctx, uow, relay: Mapping | None) -> tuple[str, dict]:
    """The attributed speaker and the stored attribution of a comment by ``ctx``'s principal."""
    if relay is None:
        return ctx.principal_id, dict(SELF)
    relay = dict(relay)
    kind = relay.get("kind")
    if ctx.principal_kind != "agent" or not _registered_coordinator(uow, ctx.principal_id):
        raise DomainError("forbidden", "Only the registered coordinator may relay a statement")
    if kind == "explicit_owner_statement":
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        if owner is None or owner["owner_principal_id"] != relay["owner_id"]:
            raise DomainError("validation_error", "An owner relay must name the registered installation owner")
        stored = {"kind": kind, "owner_id": relay["owner_id"], "statement_text": relay["statement_text"]}
        speaker = relay["owner_id"]
    elif kind == "agent_report":
        speaker = relay["agent_id"]
        agent = uow.connection.execute(
            "SELECT 1 FROM agent_profiles a JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=? AND a.status='active' "
            "AND p.active=1 AND p.kind='agent'", (speaker,)).fetchone()
        if speaker == ctx.principal_id or agent is None:
            raise DomainError("validation_error", "An agent report must name another active registered agent")
        stored = {"kind": kind, "agent_id": speaker, "report_text": relay["report_text"]}
    else:
        raise DomainError("validation_error", "Unknown relay kind")
    if relay.get("source"):
        stored["source"] = dict(relay["source"])
    return speaker, stored


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
                 guard: Callable | None = None, operation_name: str | None = None) -> dict | None:
    """The one discussion write of an operation on a Task or an Action: its comment, summary and typed references,
    with attribution. Returns the recorded row, or None when the operation carries no note.

    ``resolve(refs)`` resolves the typed references with the caller's own authority (the Task reference ports);
    ``guard()`` is the caller's extra refusal, run once a note is known to be present."""
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
    values = message_values(ctx, item, body=note, summary=summary, refs=refs, speaker=speaker, attributed=attributed, at=builder.at)
    builder.record("thread_messages", values["id"], None, values, label="comment", operation_name=operation_name)
    return values


def system_entry(uow, item: ThreadItem, *, recorder_id, body, change, event_id=None) -> str:
    """A derived account of what an operation changed (state, details), written beside the event it produced."""
    identity = new_id()
    uow.connection.execute(
        "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,attribution_json,change_json,"
        "event_id,created_at) VALUES(?,?,?,?, 'system',?,?, '{\"kind\":\"system\"}',?,?,?)",
        (item.business_id, identity, item.item_type, item.item_id, body, recorder_id, json.dumps(change, sort_keys=True),
         event_id, utc_now()))
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
        view["refs"] = resolve_refs(view)
    return author(view, uow)


def read_messages(uow, item: ThreadItem, arguments: Mapping, operation: str, principal_id: str, *,
                  resolve_refs: Callable | None = None, kinds=("comment", "system")) -> dict:
    """One bounded newest-first page, returned oldest-first. The cursor is bound to the item, principal and filters."""
    normalized = {"item_id": item.item_id, "principal_id": principal_id, "limit": min(arguments.get("limit", 20), 100),
                  "kinds": list(kinds), **({"cursor": arguments["cursor"]} if arguments.get("cursor") else {})}
    _, after = page_position(normalized, item.business_id, operation)
    bounds = " AND (created_at,id)<(?,?)" if after else ""
    marks = ",".join("?" for _ in kinds)
    rows = uow.connection.execute(
        f"SELECT * FROM thread_messages WHERE business_id IS ? AND item_type=? AND item_id=? AND kind IN ({marks}){bounds} "
        "ORDER BY created_at DESC,id DESC LIMIT ?",
        [item.business_id, item.item_type, item.item_id, *kinds, *(after or ()), normalized["limit"] + 1]).fetchall()
    selected, cursor = page([dict(row) for row in rows], normalized, item.business_id, operation)
    return {"thread_id": item.thread_id, "messages": [message_view(row, uow, resolve_refs) for row in reversed(selected)],
            "next_cursor": cursor}


def message_count(uow, item: ThreadItem) -> int:
    return uow.connection.execute(
        "SELECT count(*) FROM thread_messages WHERE business_id IS ? AND item_type=? AND item_id=? AND kind='comment'",
        (item.business_id, item.item_type, item.item_id)).fetchone()[0]
