"""Reading Task records on behalf of a CRM: current authority in the Task's own scope, never the CRM's.

A membership belongs to its Table's scope while the Task keeps its source scope, so every read switches to the Task's
scope and asks the installed policy whether the caller may currently use ``tasks_get`` there, for that exact Task: a
delegated worker's assignment names the Task records it may read, and the same selector a direct ``tasks_get`` carries is
checked here, so one permitted Task never opens another. Linking, pinning or membership grants nothing: an unavailable Task
is reported without identity, labels, values or counts, and the answer is recomputed on every read.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from qorqut_operations.application.plans import ref, timestamp
from qorqut_operations.contracts import DomainError
from qorqut_operations.tags.catalog import labels_by_item
from qorqut_operations.tables.crm_model import CUSTOM_TYPES
from qorqut_operations.tables.operations import table_context
from qorqut_operations.tasks.scope import task_context
from qorqut_operations.tasks.types import attributes, custom_fields

from .refs import namespace_of

# Errors that mean "this caller cannot see that Task right now" (an archived Project is excluded from active work, so its
# Tasks are unavailable like any other revoked source). Any other error is a real failure and propagates.
HIDDEN = frozenset({"forbidden", "policy_denied", "not_found", "invalid_principal", "operation_unavailable", "scope_required",
                    "business_archived"})
PROFILE_PREFIX, CUSTOM_PREFIX = "customer_profile.", "fields."
CUSTOMER_ONLY = frozenset({"customer_kind", "customer_state"})


class ScopeCache:
    """Per-call memory of what one caller may read: each Task and Table source (exactly), and each scope's configuration."""

    def __init__(self):
        self.allowed, self.contexts, self.fields, self.labels, self.threads = {}, {}, {}, {}, {}


def task_resource(business_id, task_id):
    """The pinned Task. A Global Task pinned from a business plan needs its explicit ``global`` namespace."""
    return ref("tasks", task_id, business_id, "global" if business_id is None else None)


class RecordAccess:
    def __init__(self, crm):
        self.crm = crm

    # -- scope and authority ---------------------------------------------------------------------------------------

    def scoped(self, ctx, business_id, uow):
        """``(context, task operation arguments, namespace)`` of a Task scope; a Global or foreign project is checked."""
        if business_id is None:
            base = replace(ctx, business_id=None, review_business_id=None)
            return task_context(base, {"scope": "global"}, uow), {"scope": "global"}, "global"
        return ctx.select_business(business_id), {}, namespace_of(business_id)

    def authorize(self, ctx, business_id, task_id, uow):
        """The caller's Task-scoped context, or the error that makes this exact Task unavailable to them.

        A delegated call is authorized with the selector a direct ``tasks_get`` of the Task would carry, so the assignment's
        record boundary applies to the Task itself and not only to its scope."""
        scoped, args, namespace = self.scoped(ctx, business_id, uow)
        application = self.crm.application
        decision = application.authorize(scoped, application.registry.get("tasks_get"), (), uow, namespace=namespace,
                                         arguments={"task_id": task_id, **args} if scoped.delegation_assignment_id is not None else None)
        if decision.mode != "allow":
            raise DomainError("policy_denied", "Reading Tasks in this scope is not permitted")
        return scoped, args, namespace

    def readable(self, ctx, business_id, task_id, uow, cache: ScopeCache):
        """Whether the caller may currently read this Task (memoized for one call, per Task)."""
        key = (business_id, task_id)
        if key not in cache.allowed:
            try:
                cache.contexts[business_id] = self.authorize(ctx, business_id, task_id, uow)
                cache.allowed[key] = True
            except DomainError as error:
                if error.code not in HIDDEN:
                    raise
                cache.allowed[key] = False
        return cache.allowed[key]

    def context_for(self, ctx, business_id, task_id, uow, cache: ScopeCache):
        """The Task-scoped context of a Task this call has authorized, authorizing it now if it has not been."""
        if not self.readable(ctx, business_id, task_id, uow, cache):
            raise DomainError("policy_denied", "Reading Tasks in this scope is not permitted")
        return cache.contexts[business_id][0]

    def table_scoped(self, ctx, business_id, uow):
        """``(context, scope arguments)`` of a Table scope, as the Table operations would run there."""
        if business_id is None:
            return table_context(replace(ctx, business_id=None, review_business_id=None), {"scope": "global"}, uow), {"scope": "global"}
        return ctx.select_business(business_id), {}

    def source_readable(self, ctx, business_id, table_id, uow, cache: ScopeCache):
        """Whether the caller may read this Table's discussion now: the Table and its thread, each for this exact Table.

        Both are authorized in the Table's own scope with the ``table_id`` selector a direct call carries, so a delegated
        assignment's record boundary applies to the Table too."""
        key = (business_id, table_id)
        if key not in cache.threads:
            try:
                scoped, arguments = self.table_scoped(ctx, business_id, uow)
                application = self.crm.application
                selector = {**arguments, "table_id": table_id} if scoped.delegation_assignment_id is not None else None
                allowed = all(application.authorize(scoped, application.registry.get(name), (), uow, namespace=namespace_of(business_id),
                                                    arguments=selector).mode == "allow" for name in ("tables_get", "tables_thread_get"))
            except DomainError as error:
                if error.code not in HIDDEN:
                    raise
                allowed = False
            cache.threads[key] = allowed
        return cache.threads[key]

    def row(self, business_id, task_id, uow):
        return uow.repository("tasks", business_id=business_id).get(task_id)

    # -- resolution ------------------------------------------------------------------------------------------------

    def resolve(self, ctx, reference, uow, builder=None):
        """The Task's normal public DTO under current Task read policy; pins its exact row on ``builder`` when preparing."""
        scoped, args, _ = self.authorize(ctx, reference["business_id"], reference["id"], uow)
        row = self.row(reference["business_id"], reference["id"], uow)
        if row is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        if builder is not None:
            builder.expect(task_resource(reference["business_id"], reference["id"]), row)
        return self.crm.tasks.read("tasks_get", scoped, {"task_id": reference["id"], **args}, uow)["task"]

    def require(self, ctx, reference, uow, builder=None):
        """Authorize and pin the Task without building its DTO; returns the raw row."""
        self.authorize(ctx, reference["business_id"], reference["id"], uow)
        row = self.row(reference["business_id"], reference["id"], uow)
        if row is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        if builder is not None:
            builder.expect(task_resource(reference["business_id"], reference["id"]), row)
        return row

    def fields(self, scoped, business_id, uow, cache: ScopeCache):
        if business_id not in cache.fields:
            cache.fields[business_id] = self.crm.tasks.configuration(scoped, uow)[0]["fields"]
        return cache.fields[business_id]

    def definitions(self, ctx, business_id, task_id, uow, cache: ScopeCache):
        """The custom Task fields the record's own scope defines now (what its values are judged against)."""
        return self.fields(self.context_for(ctx, business_id, task_id, uow, cache), business_id, uow, cache)

    def view(self, ctx, row, uow, cache: ScopeCache, now=None):
        """The list-sized public Task DTO (its discussion page and related-link expansion are left to ``tasks_get``)."""
        scoped = self.context_for(ctx, row["business_id"], row["id"], uow, cache)
        return self.crm.tasks.detail(scoped, row, uow, self.fields(scoped, row["business_id"], uow, cache),
                                     now or timestamp(self.crm.clock), notes=False)

    # -- shared-field projection -----------------------------------------------------------------------------------

    def source(self, row, uow, cache: ScopeCache):
        """The shared fields a projection reads, from a raw Task row (the same values the public DTO carries)."""
        business_id = row["business_id"]
        if business_id not in cache.labels:
            cache.labels[business_id] = labels_by_item(uow, business_id, "task")
        return {"title": row["title"], "status": row["status"], **attributes(row), "fields": custom_fields(row),
                "tags": cache.labels[business_id].get(row["id"], [])}

    @staticmethod
    def project(record, column, definitions):
        """``(value, error)`` of a projected column for one Task DTO. A mismatch is surfaced, never coerced.

        A custom field is judged by the definition of the Task's own scope, which may differ from the Table's scope and
        may have changed since the column was made: no definition there shows nothing, and a definition whose type the
        column cannot hold is a mismatch however the stored value looks."""
        field = column["record_field"]
        customer = record.get("task_type") == "customer"
        if field.startswith(PROFILE_PREFIX):
            value = (record.get("customer_profile") or {}).get(field[len(PROFILE_PREFIX):]) if customer else None
        elif field in CUSTOMER_ONLY:
            value = record.get(field) if customer else None
        elif field.startswith(CUSTOM_PREFIX):
            name = field[len(CUSTOM_PREFIX):]
            if name not in definitions:
                return None, None
            if column["type"] not in ("json", CUSTOM_TYPES.get(definitions[name]["type"])):
                return None, "type_mismatch"
            value = (record.get("fields") or {}).get(name)
        else:
            value = record.get(field)
        if value is None or fits(value, column["type"]):
            return value, None
        return None, "type_mismatch"

    def render(self, entry, columns, record, definitions):
        """The entry's cells: its local values plus the allowed current Task fields, and any projection errors."""
        local = {column["id"] for column in columns if column.get("record_field") is None}
        cells = {key: value for key, value in entry["cells_json"].items() if key in local}
        errors = {}
        for column in columns:
            if column.get("record_field") is not None:
                cells[column["id"]], error = self.project(record, column, definitions)
                if error:
                    errors[column["id"]] = error
        return cells, errors


def fits(value, kind):
    if kind == "json":
        return True
    if kind == "text":
        return isinstance(value, str)
    if kind == "boolean":
        return type(value) is bool
    if kind == "number":
        return type(value) in (int, float) and value == value and value not in (float("inf"), float("-inf"))
    if kind == "date":
        try:
            return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    return False
