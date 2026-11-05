"""The authorized direct-link source readers behind ``crm_activity_list``.

A source is one explicitly linked record: a Task, Signoff, Action or supporting document. Each reader does two things only:

* ``open``: the *current* authorized read of the source through the composed readers the host already uses for Focus and
  relationships (``ItemResolver`` and the Signoff service), admitted exactly as a direct call of the source's read
  operation is (``bazarlyk.read_admission``: exposure, Global capability and a delegated assignment's record boundary,
  not only the policy decision). A source the caller cannot read now, or that no longer exists, returns ``None`` and
  discloses nothing (no label, snippet or count). The source's *discussion* (a Task's or Action's thread, a Signoff's
  notes) is a separate read: it is admitted through ``tasks_thread_get``, ``actions_thread_get`` or ``signoffs_notes_list``
  with the same exactness, and a caller refused there receives none of its text, label, count or paging position while
  the source's own non-discussion developments remain.
* ``rows``: one bounded page of that source's own event metadata, newest first, strictly older than a stored position and
  recorded no later than the first page's cutoff. Nothing is copied into the entry; events are read from their
  authoritative tables on every page and carry the source's canonical identity, actor and time.

Every stream, and the merge across streams, is ordered by the pair ``(instant, event id)`` descending. ``instant`` is the
canonical UTC form of the event's own time and the event id is a deterministic text built from the source identity, so a
position in a cursor is exactly such a pair and a page that consumed part of a fetched window resumes without skipping
or repeating an event. Stored timestamps are written canonically by the Domain clocks; lifecycle times that live in JSON
are normalized here. Nothing in this module writes, acknowledges an inbox event, marks a conversation read or calls a
provider."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import NamedTuple

from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE as ITEM_UNAVAILABLE
from qorqut_operations.records.resources import DOCUMENT_READS
from qorqut_operations.tasks import links as task_links
from qorqut_operations.tasks.scope import task_context

from bazarlyk.read_admission import admit_read, may_read

# Refusals that mean "this reader cannot read that source now"; the source is then skipped without a trace.
UNAVAILABLE = frozenset({*ITEM_UNAVAILABLE, "policy_denied", "validation_error", "business_archived", "context_refresh_required",
                         "scope_required", "invalid_principal"})
SUMMARY = 500
BODY = 20000
# Signoff operations whose notes quote an exact recipient or message text keep that text in the Signoff itself.
SENSITIVE_SIGNOFF_OPERATIONS = frozenset({"whatsapp_send"})
# A stored document-version time is the document store's own ISO form (``+00:00``); the stream position uses ``Z``.
VERSION_INSTANT = "replace(created_at,'+00:00','Z')"


def instant(value) -> str:
    """The canonical UTC form (microseconds, ``Z``) of a stored or supplied timestamp, so sources compare."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def scope_name(business_id) -> str:
    return business_id or "global"


def snippet(text, limit=SUMMARY):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def actor(uow, principal_id):
    """The public identity of an event's actor: id, display name and kind. ``None`` for a system event."""
    if not principal_id:
        return None
    row = uow.connection.execute("SELECT display_name,kind FROM principals WHERE id=?", (principal_id,)).fetchone()
    return {"id": principal_id, "display_name": (row["display_name"] or None) if row else None, "kind": row["kind"] if row else None}


@dataclass(frozen=True)
class Target:
    """One explicitly linked record: its kind, owning scope and identity. Equal scoped targets are the same source.

    An Action's identity is its canonical ``action_key`` and its scope the business that stores it (``None`` for a Global
    Action); every other kind uses its bare identity in its own scope."""

    kind: str
    business_id: str | None
    id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{scope_name(self.business_id)}:{self.id}"


class Row(NamedTuple):
    """One event with its stream position ``(instant, event id)``."""

    key: tuple[str, str]
    event: dict


def older(position, time_column, id_column, prefix):
    """SQL restricting a sub-query to rows strictly older than ``position``, an ``(instant, event id)`` pair, where the
    event id of a row is ``prefix`` followed by its ``id_column``. Returns the clause and its bound parameters."""
    if position is None:
        return "", ()
    when, event_id = position
    return (f" AND ({time_column}<? OR ({time_column}=? AND (? || {id_column})<?))", (when, when, prefix, event_id))


def keep(key, position):
    return position is None or tuple(key) < tuple(position)


def newest(rows, position, limit):
    """The stream's own newest-first order restricted to events strictly older than ``position``."""
    selected = sorted((row for row in rows if keep(row.key, position)), key=lambda row: row.key, reverse=True)
    return selected[:limit]


class Sources:
    """Opens and reads the four kinds of direct source with the caller's own authority."""

    def __init__(self, application, items, tasks, signoffs):
        self.app, self.items, self.tasks, self.signoffs = application, items, tasks, signoffs

    # ----- authorization --------------------------------------------------------------------------------------------

    def selected(self, ctx, business_id, uow):
        """The caller's context for a source's own scope, refreshed from current access."""
        if business_id is None:
            return replace(task_context(ctx, {"scope": "global"}, uow), review_business_id=None)
        return self.app.current_context(replace(ctx, business_id=business_id, review_business_id=business_id), uow)

    def scoped(self, operation, business_id, arguments):
        """``arguments`` of the read ``operation`` with its ``scope`` where the operation takes one."""
        properties = self.app.registry.get(operation).input_schema.get("properties", {})
        return {**arguments, **({"scope": "business" if business_id else "global"} if "scope" in properties else {})}

    def selector(self, operation):
        """The one record selector argument of a read (the name a delegated assignment's boundary is checked against)."""
        required = tuple(self.app.registry.get(operation).input_schema.get("required") or ())
        if len(required) != 1:
            raise DomainError("operation_unavailable", "This read has no single record selector")
        return required[0]

    def admit(self, selected, operation, business_id, arguments, uow):
        """Admit the caller's direct call of ``operation`` on exactly this record (raises when refused)."""
        return admit_read(self.app, selected, operation, self.scoped(operation, business_id, arguments), uow)

    def discussion(self, selected, operation, business_id, arguments, uow):
        """Whether the caller may read the source's discussion now; a refusal discloses nothing."""
        try:
            return may_read(self.app, selected, operation, self.scoped(operation, business_id, arguments), uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return False
            raise

    def open(self, ctx, target, uow):
        """The authorized descriptor of one source, or ``None`` when it is unavailable to this caller now.

        ``discussion`` tells whether the source's thread or notes may be read as well."""
        try:
            return getattr(self, "open_" + target.kind)(ctx, target, uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return None
            raise

    def open_task(self, ctx, target, uow):
        # The Task reference reader refreshes the caller's access in the Task's own scope and admits ``tasks_get`` with
        # the exact record (including a delegated assignment's boundary).
        row = task_links.resolve(self.tasks, ctx, {"business_id": target.business_id, "task_id": target.id}, uow)
        selected = self.selected(ctx, target.business_id, uow)
        return {"label": row["title"],
                "discussion": self.discussion(selected, "tasks_thread_get", target.business_id, {"task_id": target.id}, uow)}

    def open_signoff(self, ctx, target, uow):
        selected = self.selected(ctx, target.business_id, uow)
        self.admit(selected, "signoffs_get", target.business_id, {"signoff_id": target.id}, uow)
        row = self.signoffs._row(self.signoffs._context(selected, uow), target.id, uow)
        if row["review_business_id"] != target.business_id:
            raise DomainError("not_found", "Select this signoff's owning review scope")
        return {"label": (row["preview_json"] or {}).get("title") or target.id, "row": row,
                "discussion": self.discussion(selected, "signoffs_notes_list", target.business_id, {"signoff_id": target.id}, uow)}

    def open_action(self, ctx, target, uow):
        selected = self.selected(ctx, target.business_id, uow)
        view = self.items.resolve(selected, {"type": "action", "id": target.id}, uow)
        self.admit(selected, "actions_get", target.business_id, {"action_id": target.id}, uow)
        row = uow.connection.execute("SELECT * FROM action_logs WHERE action_key=?", (target.id,)).fetchone()
        if row is None or row["business_id"] != target.business_id:
            raise DomainError("not_found", "Select this Action's owning scope")
        return {"label": view.get("label") or snippet(row["action_text"], 80), "row": row,
                "discussion": self.discussion(selected, "actions_thread_get", target.business_id, {"action_id": target.id}, uow)}

    def open_material(self, ctx, target, uow):
        selected = self.selected(ctx, target.business_id, uow)
        view = self.items.resolve(selected, {"type": "material", "id": target.id}, uow)
        document = None
        if view.get("resource_type") == "documents":
            document = uow.connection.execute("SELECT * FROM documents WHERE id=?", (target.id,)).fetchone()
            if document is not None and document["business_id"] != target.business_id:
                raise DomainError("not_found", "Select this document's owning scope")
            if document is not None:
                # The read operation of this kind of document, admitted for exactly this document.
                operation = DOCUMENT_READS.get(document["kind"], "context_get")
                self.admit(selected, operation, target.business_id, {self.selector(operation): target.id}, uow)
        return {"label": view.get("label") or view.get("title") or target.id, "document": document, "discussion": False}

    # ----- event pages ----------------------------------------------------------------------------------------------

    def rows(self, target, opened, uow, as_of, position, limit):
        """At most ``limit`` events of ``target``, newest first, strictly older than ``position`` and recorded by ``as_of``."""
        return getattr(self, "rows_" + target.kind)(target, opened, uow, as_of, position, limit)

    @staticmethod
    def source(target, label):
        return {"type": "document" if target.kind == "material" else target.kind, "id": target.id,
                "business_id": target.business_id, "label": label}

    def thread_rows(self, target, opened, uow, item_type, business_id, item_id, as_of, position, limit):
        """The source's discussion events. Everything in the thread - comments and the system entries written beside them -
        is the thread's own content, so none of it is read unless the caller may read that thread now."""
        if not opened["discussion"]:
            return []
        prefix = f"{target.key}:thread:"
        clause, params = older(position, "created_at", "id", prefix)
        found = uow.connection.execute(
            "SELECT * FROM thread_messages WHERE business_id IS ? AND item_type=? AND item_id=? AND created_at<=?" + clause +
            " ORDER BY created_at DESC,id DESC LIMIT ?", (business_id, item_type, item_id, as_of, *params, limit)).fetchall()
        events = []
        for row in found:
            comment = row["kind"] == "comment"
            # The stored identity, as the thread itself shows it: the attributed speaker of a comment, and for a system
            # entry the authenticated principal whose change produced it (its recorder).
            event = {"id": prefix + row["id"], "kind": "comment" if comment else "development",
                     "occurred_at": instant(row["created_at"]),
                     "actor": actor(uow, row["author_id"] or row["recorder_id"]), "summary": snippet(row["summary"] or row["body"]),
                     "source": self.source(target, opened["label"]), "origin": "linked"}
            relay = json.loads(row["attribution_json"] or "{}")
            if comment and relay.get("kind") not in (None, "self"):
                # A relayed statement or report keeps who relayed it; only identities are copied, never the relayed text.
                event["attribution"] = {key: relay[key] for key in ("kind", "owner_id", "agent_id") if key in relay}
                event["recorded_by"] = actor(uow, row["recorder_id"])
            if comment:
                event["body"] = row["body"][:BODY]
            elif row["change_json"]:
                event["change"] = json.loads(row["change_json"])
            events.append(Row((event["occurred_at"], event["id"]), event))
        return events

    def rows_task(self, target, opened, uow, as_of, position, limit):
        return self.thread_rows(target, opened, uow, "task", target.business_id, target.id, as_of, position, limit)

    def rows_action(self, target, opened, uow, as_of, position, limit):
        action = opened["row"]
        events = self.thread_rows(target, opened, uow, "action", action["business_id"], action["id"], as_of, position, limit)
        prefix = f"{target.key}:revision:"
        clause, params = older(position, "created_at", "revision", prefix)
        revisions = uow.connection.execute(
            "SELECT * FROM action_revisions WHERE action_key=? AND operation<>'add' AND created_at<=?" + clause +
            " ORDER BY created_at DESC,(? || revision) DESC LIMIT ?", (target.id, as_of, *params, prefix, limit)).fetchall()
        wording = {"edit": "Action edited: ", "remove": "Action withdrawn: "}
        for row in revisions:
            snapshot = json.loads(row["snapshot_json"])
            event = {"id": prefix + str(row["revision"]), "kind": "development", "occurred_at": instant(row["created_at"]),
                     "actor": actor(uow, row["recorded_by"]),
                     "summary": snippet(wording[row["operation"]] + str(snapshot.get("text", ""))),
                     "source": self.source(target, opened["label"]), "origin": "linked",
                     "change": {"kind": "action", "operation": row["operation"], "revision": row["revision"]}}
            events.append(Row((event["occurred_at"], event["id"]), event))
        # The Action itself happened when it says it did; it may have been recorded later (backdated), and a recording
        # made after the cutoff is not part of this listing.
        if instant(action["created_at"]) <= as_of:
            event = {"id": f"{target.key}:added", "kind": "development", "occurred_at": instant(action["occurred_at"]),
                     "recorded_at": instant(action["created_at"]),
                     "actor": actor(uow, action["performed_by"] or action["recorded_by"]),
                     "summary": "Action recorded", "source": self.source(target, opened["label"]),
                     "origin": "linked", "change": {"kind": "action", "operation": "add", "revision": 1}}
            events.append(Row((event["occurred_at"], event["id"]), event))
        return newest(events, position, limit)

    def rows_signoff(self, target, opened, uow, as_of, position, limit):
        signoff, label = opened["row"], opened["label"]
        sensitive = signoff["operation_name"] in SENSITIVE_SIGNOFF_OPERATIONS
        prefix = f"{target.key}:note:"
        clause, params = older(position, "created_at", "id", prefix)
        # The notes are the Signoff's discussion; its proposal, decision and application below are its own record.
        notes = uow.connection.execute(
            "SELECT * FROM signoff_notes WHERE signoff_id=? AND created_at<=?" + clause + " ORDER BY created_at DESC,id DESC LIMIT ?",
            (target.id, as_of, *params, limit)).fetchall() if opened["discussion"] else []
        events = []
        for note in notes:
            event = {"id": prefix + note["id"], "kind": "comment", "occurred_at": instant(note["created_at"]),
                     "actor": actor(uow, note["created_by"]),
                     "summary": "Note added" if sensitive else snippet(note["summary"] or note["body"]),
                     "source": self.source(target, label), "origin": "linked"}
            if not sensitive:
                event["body"] = note["body"][:BODY]
            events.append(Row((event["occurred_at"], event["id"]), event))
        decision = signoff["decision_json"] or {}
        answered = {"approve": "Signoff approved", "reject": "Signoff rejected", "respond": "Signoff answered"}
        lifecycle = [("proposed", signoff["created_at"], "Signoff proposed", signoff["proposer_principal_id"])]
        if decision.get("at"):
            lifecycle.append(("decided", decision["at"], answered.get(decision.get("kind"), "Signoff decided"), decision.get("by")))
        if signoff["applied_at"]:
            lifecycle.append(("applied", signoff["applied_at"], "Signoff applied", None))
        for name, when, summary, who in lifecycle:
            when = instant(when)
            if when <= as_of:
                event = {"id": f"{target.key}:{name}", "kind": "development", "occurred_at": when, "actor": actor(uow, who),
                         "summary": summary, "source": self.source(target, label), "origin": "linked",
                         "change": {"kind": "signoff", "state": name}}
                events.append(Row((when, event["id"]), event))
        return newest(events, position, limit)

    def rows_material(self, target, opened, uow, as_of, position, limit):
        document = opened["document"]
        if document is None:
            return []
        prefix = f"{target.key}:version:"
        # Metadata only: the stored text of a version is never read here. Version rows are immutable and numbered; the page is
        # the next ``limit`` versions in the canonical event order (instant, then event id) strictly older than the stream
        # position, selected by the database, so any number of versions is reachable and none is held in memory.
        clause, params = "", ()
        if position is not None:
            when, event_id = position
            clause = f" AND ({VERSION_INSTANT}<? OR ({VERSION_INSTANT}=? AND (? || version)<?))"
            params = (when, when, prefix, event_id)
        versions = uow.connection.execute(
            f"SELECT version,author_id,created_at FROM qbdv_document_versions WHERE namespace=? AND document_id=? AND {VERSION_INSTANT}<=?"
            + clause + f" ORDER BY {VERSION_INSTANT} DESC,(? || version) DESC LIMIT ?",
            (document["namespace"], target.id, as_of, *params, prefix, limit)).fetchall()
        events = []
        for row in versions:
            when = instant(row["created_at"])
            event = {"id": prefix + str(row["version"]), "kind": "development", "occurred_at": when,
                     "actor": actor(uow, row["author_id"]),
                     "summary": "Document added" if row["version"] == 1 else f"Document updated (version {row['version']})",
                     "source": self.source(target, opened["label"]), "origin": "linked",
                     "change": {"kind": "document", "version": row["version"]}}
            events.append(Row((when, event["id"]), event))
        unversioned = uow.connection.execute("SELECT 1 FROM qbdv_document_versions WHERE namespace=? AND document_id=? LIMIT 1",
                                             (document["namespace"], target.id)).fetchone() is None
        if unversioned and instant(document["created_at"]) <= as_of:
            # A document without stored text versions (a file-backed one) is known by its record alone.
            when = instant(document["created_at"])
            event = {"id": f"{target.key}:added", "kind": "development", "occurred_at": when, "actor": None,
                     "summary": "Document added", "source": self.source(target, opened["label"]), "origin": "linked",
                     "change": {"kind": "document", "version": None}}
            events.append(Row((when, event["id"]), event))
        return newest(events, position, limit)
