"""The owner's pinned-threads row at the top of Focus, for the owner and the configured coordinator.

The row is the owner's own presentation file (``qorqut.dashboard_preferences.thread_pins``); the dashboard reads and writes it
through its own route. ``thread_pins_list`` and ``thread_pins_set`` give the configured coordinator (Ordubashy) the same two
actions as agent tools, for when the owner asks him to pin or unpin a thread. The file written is always the installation
owner's, never the caller's. A pin grants nothing: a thread is pinned only after the caller's own ordinary read of its record
succeeds (the same read the private owner channel and ``conversation_history_search`` use), and a stored pin is shown by title only when
that read still succeeds. Nothing else of the owner's Focus is writable here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.records.references import KINDS, Ref, References
from qorqut_operations.signoffs.lookup import reference_kind

from qorqut.dashboard_preferences import MAX_THREAD_PINS, thread_pins
from qorqut.domains.owner_inputs import _Reading
from qorqut.management.identity import configured_ordubashy, installation_owner

LIST_DESCRIPTION = (
    "Owner and coordinator read. Lists the threads the owner has pinned in the pinned-threads row at the top of the owner's Focus "
    "dashboard, in the order shown. Each item has source (type, id and project_id: the record whose private thread it is; the "
    "project_id is the Project's id, or null for a Global record), the title shown on the pin and readable. When readable is "
    "true, title is the record's current title; when it is false you may not read that record now, so no title is given. Pin "
    "or unpin with thread_pins_set. Changes nothing.")
SET_DESCRIPTION = (
    "Owner and coordinator write. Pins or unpins one thread in the pinned-threads row at the top of the owner's Focus dashboard; "
    "this changes what the owner sees there, so use it only when the owner asks you to. source names the record whose thread "
    "it is: type (task, project, table, reference or signoff) and id; the record's project is found from its id. pinned true "
    "pins it, false unpins it; the call sets that state and repeating it changes nothing. Pinning needs a record that exists "
    "and that you may read, and refuses otherwise; unpinning is always allowed. title is optional (at most 200 characters; "
    "the record's own title is used when omitted). At most 50 threads can be pinned, and a newly pinned thread goes to the "
    "end of the row. Returns source, pinned and how many threads are now pinned. This touches only the pinned-threads row, "
    "never the owner's Focus notes.")

SOURCE = {"type": "object", "additionalProperties": False, "required": ["type", "id"], "properties": {
    "type": {"type": "string", "pattern": "^[a-z][a-z_]{0,39}$"},
    "id": {"type": "string", "minLength": 1, "maxLength": 200},
    "project_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200}}}
LIST_SCHEMA = schema({"scope": {"const": "global"}})
SET_SCHEMA = schema({"scope": {"const": "global"}, "source": SOURCE, "title": {"type": "string", "maxLength": 200},
                     "pinned": {"type": "boolean"}}, ("source", "pinned"))


class ThreadPins:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        common = dict(scope_kinds=frozenset({"global"}), global_capability="read_global_context", supports_signoff=False,
                      exposure=frozenset({"mcp"}), resolve_namespace=lambda ctx, args: "global")
        self.runtime.registry.register(OperationDescriptor(
            "thread_pins_list", LIST_SCHEMA, description=LIST_DESCRIPTION, effect_kind="read", read=self.list, **common))
        # The source names its record by type and id; its project is found from the id (a reference is Global).
        source = References(Ref(("source",)), kinds={**KINDS, "signoff": reference_kind(self.app.signoffs)})
        self.runtime.registry.register(OperationDescriptor(
            "thread_pins_set", SET_SCHEMA, description=SET_DESCRIPTION, effect_kind="records", prepare=self.prepare,
            apply=self.apply, complete_references=source, **common))
        self.runtime.thread_pins = self
        return self

    def caller(self, ctx, uow):
        """The installation owner or the configured coordinator in person, at Global. A delegated identity acts for its parent
        on one assignment and never touches the owner's dashboard."""
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        owner, coordinator = installation_owner(uow), configured_ordubashy(uow)
        personal = ctx.delegation_assignment_id is None
        if not personal or not owner or not ((ctx.principal_kind == "human" and ctx.principal_id == owner)
                                              or (ctx.principal_kind == "agent" and coordinator and ctx.principal_id == coordinator)):
            raise DomainError("forbidden", "The owner's pinned threads are for the owner or the coordinator")
        return _Reading(self, ctx, uow, owner, coordinator)

    def list(self, ctx, args, options, uow):
        reading = self.caller(ctx, uow)
        items = []
        for pin in thread_pins(self.runtime.paths.runtime, reading.owner)["threads"]:
            shown = reading.source(**self.key(pin["source"]))
            items.append({"source": pin["source"], "title": shown["title"] if shown else None, "readable": shown is not None,
                          **({"stored_title": pin["title"]} if shown and shown["title"] != pin["title"] else {})})
        return {"threads": items}

    @staticmethod
    def key(source):
        return {"kind": source["type"], "identity": source["id"], "project_id": source["project_id"]}

    def check(self, ctx, args, uow):
        """The caller, the source to store and its title. A pin reads the record through the caller's own read and stores the
        record's canonical identity, so one record is never pinned twice under two spellings; unpinning needs no read."""
        reading = self.caller(ctx, uow)
        given = self.report_task(args["source"], uow) or args["source"]
        if not args["pinned"]:
            return reading, self.canonical(reading, given, uow) or given, args.get("title", "")
        shown = reading.source(**self.key(given))
        if shown is None:
            raise DomainError("not_found", "This record does not exist or you may not read it, so its thread cannot be pinned")
        return reading, self.canonical(reading, given, uow) or given, args["title"] if "title" in args else (shown["title"] or "")[:200]

    @staticmethod
    def report_task(source, uow):
        """A report has no thread of its own: pinning or unpinning a report acts on the Task it belongs to."""
        if source["type"] != "signoff":
            return None
        try:
            row = uow.connection.execute("SELECT task_id,task_project_id FROM signoff_report_tasks WHERE signoff_id=?",
                                         (source["id"],)).fetchone()
        except sqlite3.OperationalError:
            return None
        return {"type": "task", "id": row["task_id"], "project_id": row["task_project_id"]} if row else None

    def canonical(self, reading, given, uow):
        try:
            _, resolved = self.runtime.private_conversations.resolve(reading.ctx, {"source": dict(given)}, uow)
        except DomainError:
            return None
        if not isinstance(resolved, dict) or resolved.get("type") != given["type"] or not resolved.get("id"):
            return None
        return {"type": resolved["type"], "id": resolved["id"], "project_id": resolved.get("project_id")}

    def prepare(self, ctx, args, options, uow):
        self.check(ctx, args, uow)
        return PlanBuilder(ctx, "thread_pins_set", args, "Pin or unpin a thread in the owner's Focus row").build()

    def apply(self, ctx, plan, uow):
        args = plan.arguments
        reading, source, title = self.check(ctx, args, uow)
        source = {"type": source["type"], "id": source["id"], "project_id": source["project_id"]}
        saved = thread_pins(self.runtime.paths.runtime, reading.owner, {"source": source, "title": title, "pinned": args["pinned"]})
        return {"source": source, "pinned": args["pinned"], "pinned_count": len(saved["threads"]), "limit": MAX_THREAD_PINS}
