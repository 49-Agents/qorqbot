"""``crm_activity_list``: one record's authorized activity - its own discussion, recorded observations and CRM-membership
developments (the Domain's ``CRMOperations.local_activity``) plus the events of the sources it is directly and explicitly
linked to.

Only direct links count: Task links (either direction), ``concerns``/``related_to`` relationships (either direction) and
typed references in the record's own comments. Nothing is inferred and nothing is followed recursively. Every linked source
is opened with the caller's own current authority on every page; one that is absent, archived away or no longer readable
is skipped without a trace. The read is pure: it never writes, acknowledges an inbox event, marks a conversation read or
calls a provider. Private owner conversations and native session stores are never read.

Paging. Every stream is ordered by ``(instant, event id)`` descending. A page is the top ``limit`` events of the union of
the streams that are strictly older than the last emitted position, and the union of each stream's own newest ``limit + 1``
events always contains them, so the sources are visited one after another with only the best candidates kept: no source
count, discovery depth or history length is capped, and no history is held in memory. The cursor stores the cutoff ``as_of``
(events recorded after it are excluded, including backdated ones), a fingerprint of the complete direct-link set (a changed
set invalidates the cursor), the last emitted position - which resumes every stream, the Domain's included - and the
Domain cursor of the window that holds the first unconsumed local event, so a page never re-reads the local history from
its start. A malformed cursor is a ``validation_error``."""

from __future__ import annotations

import base64
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import IDENTITY, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash
from qorqut_operations.tasks import links as task_links
from qorqut_operations.tasks.scope import task_context, task_namespace

from .sources import Row, Sources, Target, instant

NAME = "crm_activity_list"
DEFAULT_LIMIT = 50
MAXIMUM_LIMIT = 100
# The Domain stream is read in windows of this many items.
WINDOW = 100
# Direct links are discovered in keyset batches of this many rows; the set itself is not capped.
DISCOVERY_BATCH = 200
CURSOR_VERSION = 1
ROLES = ("concerns", "related_to")
SOURCE_KINDS = ("task", "signoff", "action", "material")
NOTE_KINDS = ("task", "signoff", "material")


class CRMActivityOperations:
    """The host's aggregate activity read over the Domain's local activity and the composed source readers."""

    def __init__(self, application, crm, tasks, items, signoffs, *, clock=utc_clock):
        self.application, self.crm, self.tasks, self.clock = application, crm, tasks, clock
        self.sources = Sources(application, items, tasks, signoffs)

    def register(self, registry=None):
        registry = registry or self.application.registry
        registry.register(OperationDescriptor(
            NAME, schema({
                "scope": {"enum": ["business", "global"]}, "task_id": IDENTITY,
                "include_linked": {"type": "boolean", "description": "Also merge the events of sources this record is directly linked to. Default true."},
                "cursor": {"type": "string", "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAXIMUM_LIMIT}}, ("task_id",)),
            effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"business", "global"}),
            global_capability="read_global_context", resolve_namespace=task_namespace,
            description="One record's activity, newest first: its own discussion, recorded emails/messages/developments and CRM "
                        "membership changes, plus the events of Tasks, Signoffs, Actions and documents it is directly and "
                        "explicitly linked to (each read with your current access; unreadable sources are omitted). Reading "
                        "never acknowledges, sends or changes anything. Page with next_cursor.",
            read=lambda ctx, args, options, uow: self.read(ctx, args, uow)))
        self.application.crm_activity = self
        return self

    # ----- the read -----------------------------------------------------------------------------------------------

    def read(self, ctx, args, uow):
        ctx = task_context(ctx, args, uow)
        task_id = args["task_id"]
        record_ref = {"type": "task", "id": task_id, "business_id": ctx.business_id}
        self.crm.resolve_record(ctx, record_ref, uow)
        include_linked = args.get("include_linked", True)
        limit = min(args.get("limit", DEFAULT_LIMIT), MAXIMUM_LIMIT)
        binding = canonical_hash({"operation": NAME, "principal": ctx.principal_id, "business_id": ctx.business_id,
                                  "task_id": task_id, "include_linked": include_linked})
        candidates = self.candidates(ctx, task_id, uow) if include_linked else []
        fingerprint = canonical_hash([target.key for target in candidates])[:32]
        resume = self.decode(args.get("cursor"), binding, fingerprint)
        as_of = resume["a"] if resume else timestamp(self.clock)
        position = tuple(resume["k"]) if resume and resume.get("k") else None
        need = limit + 1

        local, idle = self.local_stream(ctx, task_id, uow, as_of, resume.get("l") if resume else None, position, need)
        best = self.linked_top(ctx, candidates, uow, as_of, position, need)
        combined = sorted([*local, *((row, None) for row in best)], key=lambda pair: pair[0].key, reverse=True)
        chosen = combined[:limit]
        cursor = None
        if len(combined) > limit:
            states = [after for _, after in chosen if after is not None]
            cursor = self.encode({"v": CURSOR_VERSION, "b": binding, "f": fingerprint, "a": as_of, "k": list(chosen[-1][0].key),
                                  "l": states[-1] if states else idle})
        return {"record_ref": record_ref, "items": [row.event for row, _ in chosen], "next_cursor": cursor, "as_of": as_of}

    # ----- streams ------------------------------------------------------------------------------------------------

    def local_stream(self, ctx, task_id, uow, as_of, state, position, need):
        """The Domain's own activity strictly older than ``position`` as ``(row, state after it)`` pairs, and the state to keep
        when none of them is emitted.

        ``state`` is the Domain cursor of the window that held the first unconsumed event. Its events are recomputed from
        the *current* data and cut at the last emitted stable position ``(instant, event id)``, never at a count of
        previously visible rows, so an event that has since become unreadable cannot make a different one disappear. Windows
        are read until enough eligible events are found or the stream is truly exhausted. The Domain contract is a stream
        ordered by ``(instant, event id)`` descending; one that is not is refused rather than silently reordered."""
        if state and state.get("done"):
            return [], {"done": True}
        token = (state or {}).get("t")
        found, previous, idle = [], None, None
        while True:
            page = self.crm.local_activity(ctx, task_id, {"limit": WINDOW, "as_of": as_of, **({"cursor": token} if token else {})}, uow)
            rows = sorted((Row((instant(item["occurred_at"]), item["id"]), item) for item in page["items"]),
                          key=lambda row: row.key, reverse=True)
            if rows and previous is not None and rows[0].key >= previous:
                raise DomainError("internal_error", "The Domain's activity stream is not ordered by time and event id")
            if rows:
                previous = rows[-1].key
            eligible = [row for row in rows if position is None or row.key < tuple(position)]
            if eligible and idle is None:
                idle = {"t": token} if token else {"t": None}
            consumed = {"t": page["next_cursor"]} if page["next_cursor"] else {"done": True}
            for index, row in enumerate(eligible):
                found.append((row, consumed if index == len(eligible) - 1 else ({"t": token} if token else {"t": None})))
            if len(found) >= need or not page["next_cursor"]:
                break
            token = page["next_cursor"]
        return found[:need], idle if idle is not None else {"done": True}

    def linked_top(self, ctx, candidates, uow, as_of, position, need):
        """The ``need`` newest events older than ``position`` across every readable direct source, visiting the sources one at
        a time. Each source offers at most its own newest ``need`` events, and the overall top ``need`` is always among
        them, so nothing beyond the best candidates is retained however many sources or events there are."""
        best = []
        for target in candidates:
            opened = self.sources.open(ctx, target, uow)
            if opened is None:
                continue
            best.extend(self.sources.rows(target, opened, uow, as_of, position, need))
            if len(best) > need:
                best = sorted(best, key=lambda row: row.key, reverse=True)[:need]
        return best

    # ----- direct links -------------------------------------------------------------------------------------------

    def candidates(self, ctx, task_id, uow):
        """Every directly linked source of the record, deduplicated by scoped identity, before any authorization.

        Each kind of link is discovered completely, in keyset batches of ``DISCOVERY_BATCH`` rows, so a source is neither
        missed nor reordered however many exist, and the whole set is what the cursor fingerprint covers."""
        scope = ctx.business_id
        found = {}

        def add(kind, business_id, identity):
            target = Target(kind, business_id, identity)
            if target.kind == "task" and (business_id, identity) == (scope, task_id):
                return
            found.setdefault(target.key, target)

        row = uow.repository("tasks", business_id=scope).get(task_id, required=True)
        for link in task_links.stored(row):
            add("task", link["business_id"], link["task_id"])
        for other in self.batches(
                uow, "SELECT t.rowid AS position,t.business_id,t.id FROM tasks t WHERE t.rowid>? AND EXISTS ("
                "SELECT 1 FROM json_each(t.fields_json, '$.\"$task_links\"') edge "
                "WHERE json_extract(edge.value,'$.business_id') IS ? AND json_extract(edge.value,'$.task_id')=?) "
                "ORDER BY t.rowid LIMIT ?", (scope, task_id)):
            add("task", other["business_id"], other["id"])
        for edge in self.batches(
                uow, "SELECT rowid AS position,from_type,from_id,to_type,to_id FROM relationships WHERE rowid>? AND business_id IS ? "
                "AND role IN (?,?) AND ((from_type='task' AND from_id=?) OR (to_type='task' AND to_id=?)) ORDER BY rowid LIMIT ?",
                (scope, *ROLES, task_id, task_id)):
            other = (edge["to_type"], edge["to_id"]) if (edge["from_type"], edge["from_id"]) == ("task", task_id) \
                else (edge["from_type"], edge["from_id"])
            self.add_reference(uow, scope, *other, add)
        for message in self.batches(
                uow, "SELECT rowid AS position,refs_json FROM thread_messages WHERE rowid>? AND business_id IS ? AND item_type='task' "
                "AND item_id=? AND kind='comment' AND refs_json<>'[]' ORDER BY rowid LIMIT ?", (scope, task_id)):
            for item in json.loads(message["refs_json"]):
                if isinstance(item, dict) and item.get("type") in NOTE_KINDS and isinstance(item.get("id"), str):
                    self.add_reference(uow, scope, item["type"], item["id"], add)
        return [found[key] for key in sorted(found)]

    @staticmethod
    def batches(uow, sql, parameters):
        """Every row of ``sql`` (``rowid>?`` first, ``LIMIT ?`` last, ordered by ``rowid`` and selecting it as ``position``) in
        stable keyset batches, so discovery never stops at a fixed row count and never holds more than one batch."""
        after = 0
        while True:
            rows = uow.connection.execute(sql, (after, *parameters, DISCOVERY_BATCH)).fetchall()
            yield from rows
            if len(rows) < DISCOVERY_BATCH:
                return
            after = rows[-1]["position"]

    @staticmethod
    def add_reference(uow, scope, kind, identity, add):
        """Canonical scoped target of a relationship endpoint or note reference; other kinds are not activity sources."""
        if kind == "signoff":
            row = uow.connection.execute("SELECT review_business_id FROM signoffs WHERE id=?", (identity,)).fetchone()
            if row is not None:
                add("signoff", row["review_business_id"], identity)
        elif kind == "action":
            if identity.startswith("global/"):
                add("action", None, identity)
            elif identity.startswith("business/") and identity.count("/") >= 2:
                add("action", identity.split("/", 2)[1], identity)
            else:
                row = uow.connection.execute("SELECT action_key,business_id FROM action_logs WHERE id=? AND business_id IS ?",
                                             (identity, scope)).fetchone()
                if row is not None:
                    add("action", row["business_id"], row["action_key"])
        elif kind in ("task", "material"):
            add(kind, scope, identity)

    # ----- cursor -------------------------------------------------------------------------------------------------

    @staticmethod
    def encode(payload):
        return base64.urlsafe_b64encode(canonical_bytes(payload)).decode("ascii")

    @staticmethod
    def decode(cursor, binding, fingerprint):
        if not cursor:
            return None
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            valid = (isinstance(decoded, dict) and set(decoded) <= {"v", "b", "f", "a", "k", "l"}
                     and decoded.get("v") == CURSOR_VERSION and decoded.get("b") == binding
                     and isinstance(decoded.get("f"), str) and isinstance(decoded.get("a"), str)
                     and _position(decoded.get("k")) and _state(decoded.get("l")))
            if valid:
                instant(decoded["a"])
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            raise DomainError("validation_error", "Invalid or differently scoped activity cursor") from exc
        if not valid:
            raise DomainError("validation_error", "Invalid or differently scoped activity cursor")
        if decoded["f"] != fingerprint:
            raise DomainError("revision_conflict", "The record's links changed while paging; start the activity listing again")
        return decoded


def _position(value):
    """A stream position: absent, or an ``(instant, event id)`` pair of texts."""
    return value is None or (isinstance(value, list) and len(value) == 2 and all(isinstance(part, str) for part in value))


def _state(value):
    """The Domain stream's resume state: absent, exhausted, or the cursor (possibly none) of the window to read next."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value == {"done": True}:
        return True
    return set(value) == {"t"} and (value["t"] is None or (isinstance(value["t"], str) and len(value["t"]) <= 4000))
