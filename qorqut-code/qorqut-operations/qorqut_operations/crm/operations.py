"""The CRM domain service: memberships of existing Task records in a CRM table, the Table discussion and recorded activity.

``CRMOperations`` is composed by a trusted host after the Task service and the neutral item resolver exist, and is bound
to the Table service with ``TableOperations.bind_crm``. It owns no second Task, discussion or permission engine: a new
entry's Task is made by ``tasks_create``'s own prepare and apply inside the Table's one plan, Table comments and follows
are the existing item discussion with the ``table`` type, and every Task read is judged by the installed policy in the
Task's own scope. Construction is inert and nothing is discovered by name.
"""

from __future__ import annotations

from qorqut_operations.application.engine import Application
from qorqut_operations.application.plans import IDENTITY, PAGE, TEXT, UUID, plain, schema, utc_clock
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import ItemResolver
from qorqut_operations.discussion.threads import BODY, RELAY
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.tables.operations import CELLS, KEY, SCOPE, TableOperations, table_context, table_namespace
from qorqut_operations.tasks.operations import TaskOperations
from qorqut_operations.tasks.scope import task_namespace

from .access import RecordAccess
from .activity import Activity
from .entries import Entries
from .exports import CLASSIFICATIONS
from .refs import EVENT_KINDS, EVENT_KEY_LIMIT, RECORD_REF, SOURCE
from .schema import IMMUTABLE_FIELDS, SCHEMA, TABLE_NAMES, TABLES, storage_contribution
from .threads import Threads

READ_NAMES = ("crm_entries_list", "tables_thread_get")
MUTATION_NAMES = ("crm_entries_add", "crm_entries_update", "crm_entries_remove", "crm_activity_record", "tables_comment",
                  "tables_follow")
CATALOG_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in READ_NAMES),
    *(CatalogEntry(name, name != "tables_follow", "records") for name in MUTATION_NAMES),
)
STAGE = {"anyOf": [KEY, {"type": "null"}]}
LIMIT = {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": PAGE["cursor"]}
# The Task-creation fields an entry's ``create`` may carry: tasks_create's own, minus the Focus presentation and scope.
NOT_CREATE = frozenset({"focus", "scope", "rationale"})


class CRMOperations:
    def __init__(self, application, tables, tasks, items, *, clock=utc_clock):
        if not isinstance(application, Application):
            raise AssemblyError("CRM operations need the Application whose registry and policy authorize their reads")
        if not isinstance(tables, TableOperations):
            raise AssemblyError("CRM operations need the Table service built by trusted assembly code")
        if not isinstance(tasks, TaskOperations):
            raise AssemblyError("CRM operations need the Task service built by trusted assembly code")
        if not isinstance(items, ItemResolver):
            raise AssemblyError("CRM operations need the frozen ItemResolver built by trusted assembly code")
        if not callable(clock):
            raise AssemblyError("The CRM clock is a callable returning an aware timestamp")
        self.application, self.tables, self.tasks, self.items, self.clock = application, tables, tasks, items, clock
        self.access, self.entries = RecordAccess(self), Entries(self)
        self.threads, self.activity = Threads(self), Activity(self)

    # -- registration ----------------------------------------------------------------------------------------------

    def register(self, registry):
        table_scope = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                           resolve_namespace=table_namespace)
        task_scope = {**table_scope, "resolve_namespace": task_namespace}
        create = {key: value for key, value in plain(next(d for d in self.tasks.descriptors() if d.name == "tasks_create")
                                                     .input_schema)["properties"].items() if key not in NOT_CREATE}
        cells = {"cells": CELLS, "stage_id": STAGE}
        refs = self.tasks.note_references().schema
        reads = {
            "crm_entries_list": ({"table_id": UUID, "query": TEXT, "stage_id": KEY, "include_archived": {"type": "boolean"},
                                  "include_removed": {"type": "boolean"}, "order_by": KEY, "descending": {"type": "boolean"},
                                  "equals": CELLS, **LIMIT}, ("table_id",),
                                 "Read a CRM table's entries (memberships of existing Task records) in stable position order, 50 "
                                 "by default and at most 100 per page. Each entry shows its local cells, the current Task fields "
                                 "its projected columns show, and the Task itself; a Task you cannot read appears as a bare "
                                 "unavailable placeholder. Sort and equals use local columns only. Removed entries and archived "
                                 "Tasks are hidden unless requested."),
            "tables_thread_get": ({"table_id": UUID, **LIMIT}, ("table_id",),
                                  "Read one bounded page of a table's shared discussion (oldest first within the page; "
                                  "next_cursor continues to older messages), including the structured developments of CRM "
                                  "entry changes you are allowed to see, and your own follow state."),
        }
        for name, (fields, required, description) in reads.items():
            registry.register(OperationDescriptor(
                name, schema({**SCOPE, **fields}, required), description=description, effect_kind="read", supports_signoff=False,
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow), **table_scope))
        writes = {
            "crm_entries_add": ({"table_id": UUID, "record": RECORD_REF, "create": schema(create, ("title",)), **cells}, ("table_id",),
                                "Add an entry to a CRM table with the table's expected_revision: name exactly one of record (an "
                                "existing Task of any type, by its scoped reference) or create (a new Customer, by default, made "
                                "by tasks_create in the same atomic change). cells hold local values only; shared fields are edited "
                                "on the Task with tasks_update. Adding a removed entry restores it with its data; adding an active "
                                "one returns it unchanged unless different values conflict.", ("tables_rows_write",)),
            "crm_entries_update": ({"table_id": UUID, "entry_id": UUID, "changes": {**schema(cells), "minProperties": 1}},
                                   ("table_id", "entry_id", "changes"),
                                   "Change an entry's local cells (omitted keys are kept, null clears) and/or its stage (null "
                                   "clears) with the table's expected_revision. Shared Task fields are never written here.",
                                   ("tables_rows_write",)),
            "crm_entries_remove": ({"table_id": UUID, "entry_id": UUID}, ("table_id", "entry_id"),
                                   "Remove an entry from a CRM table with the table's expected_revision. This is a soft removal "
                                   "of the membership only: the Task, its thread and its history are untouched and adding the "
                                   "Task again restores the entry.", ("tables_rows_write",)),
        }
        for name, (fields, required, description, derived) in writes.items():
            registry.register(OperationDescriptor(
                name, schema({**SCOPE, **fields, "rationale": TEXT}, required), description=description, derived_effects=derived,
                prepare=lambda ctx, args, options, uow, op=name: self.entries.prepare(op, ctx, args, options, uow),
                apply=self.entries.apply, validate_targets=self.entries.validate,
                replay_validator=lambda ctx, args, replay, uow, op=name: self.entries.validate_replay(op, ctx, args, replay, uow),
                **table_scope))
        registry.register(OperationDescriptor(
            "tables_comment", schema({**SCOPE, "table_id": UUID, "text": BODY, "note_refs": refs, "summary": {
                "type": "string", "minLength": 1, "maxLength": 500, "pattern": r"\S"}, "relay": RELAY, "rationale": TEXT},
                ("table_id", "text")),
            description="Add a message to a table's shared discussion (ordinary and CRM tables alike). An archived table can still "
                        "be discussed. The installation owner's team comments follow the same channel rule as Task discussions. "
                        "Commenting needs the table's row-write permission as a policy decision only: it grants no data change.",
            derived_effects=("tables_rows_write",), prepare=self.threads.prepare_comment, apply=self.threads.apply_comment,
            validate_targets=self.threads.validate_comment, replay_validator=self.threads.validate_comment_replay, **table_scope))
        registry.register(OperationDescriptor(
            "tables_follow", schema({**SCOPE, "table_id": UUID, "following": {"type": "boolean"}}, ("table_id", "following")),
            description="Follow or unfollow a table's discussion for yourself. Creators and commenters follow by default; an "
                        "explicit unfollow stays until you follow again. Following a table never follows its entries' Tasks.",
            derived_effects=("tables_get",), supports_signoff=False, prepare=self.threads.prepare_follow, apply=self.threads.apply_follow,
            replay_validator=self.threads.validate_follow_replay, **table_scope))
        registry.register(OperationDescriptor(
            "crm_activity_record", schema({**SCOPE, "task_id": IDENTITY, "kind": {"enum": list(EVENT_KINDS)}, "summary": {
                "type": "string", "minLength": 1, "maxLength": 500, "pattern": r"\S"}, "body": BODY,
                "occurred_at": {"type": "string", "minLength": 1, "maxLength": 64}, "source": SOURCE,
                "event_key": {"type": "string", "minLength": 1, "maxLength": EVENT_KEY_LIMIT},
                "corrects_event_id": UUID, "rationale": TEXT},
                ("task_id", "kind", "summary", "body", "occurred_at", "source", "event_key")),
            description="Record an email, message or development you observed about a Task, with its source reference and the time "
                        "it really happened. This is an append-only record attributed to you: it sends nothing, fetches nothing, "
                        "verifies nothing and approves nothing. The same event_key returns the original; a correction is a new "
                        "event naming the earlier one (corrects_event_id).", derived_effects=("tasks_comment",),
            prepare=self.activity.prepare_record, apply=self.activity.apply_record, validate_targets=self.activity.validate_record,
            replay_validator=self.activity.validate_replay, **task_scope))
        return self

    def recheck(self, ctx, operation, namespace, uow, *extra):
        """The current policy of an operation's composed effects, for a saved receipt.

        A fresh call has them decided with its plan; a replayed receipt skips the plan, so its result is returned only
        while the same effects (and ``extra`` ones the original call composed) are still permitted."""
        registry = self.application.registry
        for name in (*registry.get(operation).derived_effects, *extra):
            effect = registry.get(name)
            if namespace == "global" and effect.global_capability and effect.global_capability not in ctx.global_capabilities:
                raise DomainError("forbidden", "Composed effect requires an ungranted global capability")
            self.application.authorize(ctx, effect, (), uow, namespace=namespace)

    # -- the seams the host and the Table service use --------------------------------------------------------------

    def read(self, operation, ctx, args, uow):
        if operation == "tables_thread_get":
            return self.threads.read(ctx, args, uow)
        ctx = table_context(ctx, args, uow)
        return self.entries.list_entries(ctx, self.entries.table(ctx, args["table_id"], uow), args, uow)

    def list_entries(self, ctx, table, args, uow, operation="crm_entries_list"):
        """The Table service's dispatch of a CRM table's row read to its entries (``tables_rows_list`` semantics)."""
        return self.entries.list_entries(ctx, table, args, uow, operation)

    def field_definitions(self, ctx, uow):
        """The Task custom field definitions of the context's scope, which a projected column's type must agree with."""
        return self.tasks.configuration(ctx, uow)[0]["fields"]

    def local_activity(self, ctx, task_id, args, uow):
        """``{items, next_cursor, as_of}``: this record's own discussion, recorded observations and CRM developments."""
        return self.activity.local(ctx, task_id, args, uow)

    def resolve_record(self, ctx, ref, uow, builder=None):
        """The scoped Task's normal public DTO under current Task read policy (pinned on ``builder`` when preparing)."""
        return self.access.resolve(ctx, ref, uow, builder)


__all__ = ["CATALOG_ENTRIES", "CLASSIFICATIONS", "CRMOperations", "IMMUTABLE_FIELDS", "MUTATION_NAMES", "READ_NAMES", "SCHEMA",
           "TABLES", "TABLE_NAMES", "storage_contribution"]
