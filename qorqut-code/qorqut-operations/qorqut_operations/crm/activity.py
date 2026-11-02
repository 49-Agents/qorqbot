"""Recorded email / message / development observations, and the record's own local activity.

A recorded observation is data an authenticated recorder wrote down about a Task: it never sends, fetches, verifies or
approves anything, and it is never rewritten. A correction is a new observation naming an earlier one of the same Task. The
per-Task ``event_key`` makes a retried import return the original observation.

``local_activity`` is one bounded, newest-first page over what this domain owns for a Task: its own team discussion and
system developments, its recorded observations, and the CRM-membership developments of Tables the reader can also read.
The order is the single total order ``(occurred_at, activity id)``, so a page's cursor is just that position and no source
event is ever skipped or repeated, however the sources interleave. The first page fixes an ingestion cutoff (``as_of``):
an observation recorded after it never enters a later page of that browsing session, even when its ``occurred_at`` is
backdated. Nothing here reads a foreign Task, Signoff, private conversation or provider.

A membership development is an event of its Table's discussion, so it is shown only while the reader may read that Table and
its discussion (``tables_get`` and ``tables_thread_get``) in the Table's own scope. The scan over a record's developments
reads bounded batches and continues until the page is full or they are exhausted: a Table the reader cannot read hides
neither a later readable one nor the continuation of the page.
"""

from __future__ import annotations

import base64
import json

from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, timestamp
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, new_id, plain
from qorqut_operations.tasks.scope import task_context, task_namespace
from qorqut_operations.team_channel import reject_owner_team_write

from .access import ScopeCache, task_resource
from .refs import activity_id, event_key_checked, observed_at, record_ref, source_checked

PAGE_MAXIMUM = 100
BATCH = 100       # development rows read per query while scanning the Tables a record has been in
# Recording shares text in the record's history exactly as a Task comment does, so it is held to the same authority and the
# same owner-channel rule in the record's scope.
RECORD_AS = "tasks_comment"
KEY = "(kind || ':' || ? || ':' || id)"


def one_line(text, limit=500):
    return " ".join(text.split())[:limit] or "(empty)"


class Activity:
    def __init__(self, crm):
        self.crm = crm

    # -- recording -------------------------------------------------------------------------------------------------

    def prepare_record(self, ctx, args, options, uow):
        ctx = task_context(ctx, args, uow)
        reject_owner_team_write(ctx, uow, RECORD_AS)
        row = uow.repository("tasks", business_id=ctx.business_id).get(args["task_id"])
        if row is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        if options.expected_revision is not None:
            require_revision(row, options.expected_revision)  # the Task's revision; the Task itself is not changed
        builder = PlanBuilder(ctx, "crm_activity_record", args, "Record observation", clock=self.crm.clock,
                              namespace=task_namespace(ctx, args))
        builder.expect(task_resource(ctx.business_id, row["id"]), row)
        source = source_checked(args["source"])
        occurred = observed_at(args["occurred_at"], builder.at)
        key = event_key_checked(args["event_key"])
        corrects = args.get("corrects_event_id")
        earlier = uow.repository("crm_activity_events", business_id=ctx.business_id).get(corrects) if corrects is not None else None
        if corrects is not None and (earlier is None or earlier["task_id"] != row["id"]):
            raise DomainError("validation_error", "A correction must name an earlier observation of the same Task")
        content = {"kind": args["kind"], "summary": args["summary"], "body": args["body"], "occurred_at": occurred,
                   "source": source, "corrects_event_id": corrects}
        digest = canonical_hash(content)
        existing = self.by_key(ctx.business_id, row["id"], key, uow)
        if existing is not None:
            if existing["content_hash"] != digest:
                raise DomainError("conflict", "This event key already recorded different content for the Task",
                                  details={"event_id": existing["id"]})
            builder.effects.append("The observation was already recorded; nothing is written.")
            return builder.build()
        identity = new_id()
        builder.record("crm_activity_events", identity, None, {
            "id": identity, "task_id": row["id"], "event_key": key, "kind": args["kind"], "summary": args["summary"].strip(),
            "body": args["body"], "occurred_at": occurred, "recorded_at": builder.at, "recorded_by": ctx.principal_id,
            "source_json": source, "corrects_event_id": corrects, "content_hash": digest}, label=key)
        builder.effects.append("Records what an agent observed in an email, message or development. It sends nothing, fetches "
                               "nothing and approves nothing, and it changes no Task field.")
        return builder.build()

    @staticmethod
    def by_key(business_id, task_id, key, uow):
        found = uow.connection.execute(
            "SELECT * FROM crm_activity_events WHERE business_id IS ? AND task_id=? AND event_key=?", (business_id, task_id, key)).fetchone()
        return dict(found) if found else None

    def apply_record(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        if any(change.resource.resource_type == "crm_activity_events" for change in plan.changes):
            reject_owner_team_write(ctx, uow, RECORD_AS)
        written = apply_records(ctx, plan, uow)
        found = written[0] if written else self.by_key(ctx.business_id, plan.arguments["task_id"],
                                                         plan.arguments["event_key"], uow)
        row = uow.connection.execute("SELECT * FROM crm_activity_events WHERE business_id IS ? AND id=?",
                                     (ctx.business_id, found["id"])).fetchone()
        return {"event": self.recorded_view(dict(row), uow), "duplicate": not written}

    def validate_record(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        reject_owner_team_write(ctx, uow, RECORD_AS)
        args = plan.arguments
        if uow.repository("tasks", business_id=ctx.business_id).get(args["task_id"]) is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        for change in plan.changes:
            values = change.values
            if (change.resource.resource_type != "crm_activity_events" or change.action != "insert"
                    or values["task_id"] != args["task_id"] or values["event_key"] != args["event_key"]
                    or values["recorded_by"] != ctx.principal_id):
                raise DomainError("invalid_plan", "The change must record the observation the request names")
            source_checked(values["source_json"])

    def validate_replay(self, ctx, args, replay, uow):
        """A saved result is returned only while the recorder may still comment on the Task, in the same channel."""
        if replay.status != "ok":
            return
        ctx = task_context(ctx, args, uow)
        reject_owner_team_write(ctx, uow, RECORD_AS)
        self.crm.recheck(ctx, "crm_activity_record", task_namespace(ctx, args), uow)
        if uow.repository("tasks", business_id=ctx.business_id).get(args["task_id"]) is None:
            raise DomainError("not_found", "Task does not exist in this scope")

    # -- views -----------------------------------------------------------------------------------------------------

    def actor(self, principal_id, uow):
        principal = uow.repository("principals").get(principal_id) if principal_id else None
        if principal is None:
            return None
        return {"id": principal["id"], "kind": principal["kind"], "display_name": principal.get("display_name", "")}

    def recorded_view(self, row, uow):
        source = json.loads(row["source_json"]) if isinstance(row["source_json"], str) else row["source_json"]
        corrects = row["corrects_event_id"]
        view = {"id": activity_id(row["kind"], row["business_id"], row["id"]), "kind": row["kind"], "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"], "actor": self.actor(row["recorded_by"], uow), "summary": row["summary"],
                "body": row["body"], "origin": "recorded",
                "source": {"type": "crm_activity_event", "id": row["id"], "business_id": row["business_id"],
                           **({"label": source["title"]} if source.get("title") else {}),
                           **({"url": source["url"]} if source.get("url") else {}), "reference": source}}
        if corrects:
            earlier = uow.connection.execute("SELECT kind FROM crm_activity_events WHERE business_id IS ? AND id=?",
                                             (row["business_id"], corrects)).fetchone()
            view["change"] = {"kind": "correction", "corrects_event_id": corrects,
                              "corrects": activity_id(earlier["kind"], row["business_id"], corrects)}
        return view

    def thread_view(self, row, task, uow):
        """The Task's own discussion entry: a comment, or the system development of one of its changes."""
        speaker = row["author_id"] or row["recorder_id"]
        actor = self.actor(speaker, uow)
        attribution = json.loads(row["attribution_json"])
        if row["recorder_id"] != speaker:
            actor = {**(actor or {}), "recorder_id": row["recorder_id"], "attribution": attribution}
        view = {"id": row["_key"], "kind": row["kind"], "occurred_at": row["created_at"], "recorded_at": row["created_at"],
                "actor": actor, "summary": row["summary"] or one_line(row["body"]), "body": row["body"], "origin": "own",
                "source": {"type": "task", "id": task["id"], "business_id": task["business_id"], "label": task["title"]}}
        if row["change_json"]:
            view["change"] = json.loads(row["change_json"])
        return view

    def membership_view(self, row, uow):
        table = uow.repository("data_tables", business_id=row["business_id"]).get(row["item_id"])
        return {"id": row["_key"], "kind": "crm_membership", "occurred_at": row["created_at"], "recorded_at": row["created_at"],
                "actor": self.actor(row["recorder_id"], uow), "summary": row["body"], "origin": "crm_membership",
                "source": {"type": "table", "id": row["item_id"], "business_id": row["business_id"],
                           **({"label": table["title"]} if table else {})},
                "change": json.loads(row["change_json"])}

    # -- the record's own activity ---------------------------------------------------------------------------------

    @staticmethod
    def cursor(args, binding):
        """``(after, as_of)`` of a page request; the cursor is bound to the record and carries the first page's cutoff."""
        if not args.get("cursor"):
            return None, None
        try:
            decoded = json.loads(base64.urlsafe_b64decode(args["cursor"].encode("ascii")))
            if decoded["binding"] != binding or len(decoded["after"]) != 2 or not isinstance(decoded["as_of"], str):
                raise ValueError
            return tuple(decoded["after"]), decoded["as_of"]
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise DomainError("validation_error", "Invalid or differently scoped activity cursor") from exc

    def local(self, ctx, task_id, args, uow):
        """The Activity page of the record's own discussion, recorded observations and readable CRM developments."""
        namespace = task_namespace(ctx, args)
        business_id = None if namespace == "global" else ctx.business_id
        reference = record_ref(business_id, task_id)
        row = self.crm.access.require(ctx, reference, uow)  # the Task must be readable now, in its own scope
        binding = canonical_hash({"operation": "crm_local_activity", "record": reference})
        after, cursor_as_of = self.cursor(args, binding)
        if cursor_as_of is not None and args.get("as_of") not in (None, cursor_as_of):
            raise DomainError("validation_error", "The cursor already fixes the cutoff of this browsing session")
        as_of = cursor_as_of or args.get("as_of") or timestamp(self.crm.clock)
        limit = min(args.get("limit", 50), PAGE_MAXIMUM)
        candidates = [*self.own(business_id, task_id, namespace, as_of, after, limit, uow),
                      *self.recorded(business_id, task_id, namespace, as_of, after, limit, uow),
                      *self.memberships(ctx, business_id, task_id, namespace, as_of, after, limit, uow)]
        candidates.sort(key=lambda item: (item["_when"], item["_key"]), reverse=True)
        selected = candidates[:limit]
        more = len(candidates) > limit
        cursor = None
        if more:
            last = selected[-1]
            cursor = base64.urlsafe_b64encode(canonical_bytes({"binding": binding, "after": [last["_when"], last["_key"]],
                                                              "as_of": as_of})).decode("ascii")
        items = [self.build(item, row, uow) for item in selected]
        return {"items": items, "next_cursor": cursor, "as_of": as_of}

    def build(self, item, task, uow):
        if item["_source"] == "own":
            return self.thread_view(item["_row"], task, uow)
        if item["_source"] == "recorded":
            return self.recorded_view(item["_row"], uow)
        return self.membership_view(item["_row"], uow)

    @staticmethod
    def bounded(sql, params, when, after, namespace, limit, uow):
        if after:
            sql += f" AND ({when}, {KEY}) < (?, ?)"
            params = [*params, namespace, *after]
        found = uow.connection.execute(f"{sql} ORDER BY {when} DESC, {KEY} DESC LIMIT ?", [*params, namespace, limit + 1]).fetchall()
        return [dict(row) for row in found]

    def own(self, business_id, task_id, namespace, as_of, after, limit, uow):
        sql = (f"SELECT *, {KEY} AS _key FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id=? "
               "AND created_at<=?")
        found = self.bounded(sql, [namespace, business_id, task_id, as_of], "created_at", after, namespace, limit, uow)
        return [{"_when": row["created_at"], "_key": row["_key"], "_source": "own", "_row": row} for row in found]

    def recorded(self, business_id, task_id, namespace, as_of, after, limit, uow):
        sql = f"SELECT *, {KEY} AS _key FROM crm_activity_events WHERE business_id IS ? AND task_id=? AND recorded_at<=?"
        found = self.bounded(sql, [namespace, business_id, task_id, as_of], "occurred_at", after, namespace, limit, uow)
        return [{"_when": row["occurred_at"], "_key": row["_key"], "_source": "recorded", "_row": row} for row in found]

    def memberships(self, ctx, business_id, task_id, namespace, as_of, after, limit, uow):
        """Developments of CRMs that contain, or once contained, the record, from Tables whose discussion the reader may read.

        The scan runs newest first in bounded batches, filtering each development by its Table's current authority, until
        ``limit + 1`` readable ones are found or the developments run out, so the page and its continuation are exact."""
        cache, found, position = ScopeCache(), [], after
        key = ("('crm_membership:' || (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) "
               "|| ':' || id)")
        sql = (f"SELECT *, {key} AS _key FROM thread_messages WHERE item_type='table' AND kind='system' "
               "AND json_extract(change_json,'$.record.id')=? AND json_extract(change_json,'$.record.business_id') IS ? "
               "AND created_at<=?")
        while len(found) <= limit:
            params, text = [task_id, business_id, as_of], sql
            if position:
                text += f" AND (created_at, {key}) < (?, ?)"
                params += list(position)
            batch = [dict(row) for row in uow.connection.execute(f"{text} ORDER BY created_at DESC, {key} DESC LIMIT ?",
                                                                 [*params, BATCH])]
            for row in batch:
                if len(found) > limit:
                    break
                if self.crm.access.source_readable(ctx, row["business_id"], row["item_id"], uow, cache):
                    found.append({"_when": row["created_at"], "_key": row["_key"], "_source": "membership", "_row": row})
            if len(batch) < BATCH:
                break
            position = (batch[-1]["created_at"], batch[-1]["_key"])
        return found
