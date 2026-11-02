"""Small shared helpers for explicit domain plans, not a caller-controlled CRUD API.

``PlanBuilder`` prepares an exact ``ChangeSet`` from a domain's snapshot reads, and ``apply_records`` writes its
record effects inside the applying transaction. Table specifications come from the running unit's frozen assembly
(or an explicit ``tables`` mapping); the domain-specific behaviour v1 interleaved here (Story event allocation,
collection limits, legacy Action inserts, forced aggregate revisions, shared history) is reached only through the
assembly's ``RecordEffectHooks``."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import re

from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, FieldChange,
    PreparedChange, PreviewResource, ResourceRef, StructuredPreview, canonical_bytes, canonical_hash, plain)
from qorqut_operations.storage.database import active_tables
from qorqut_operations.storage.repositories import decode_row

TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
IDENTITY = {"type": "string", "minLength": 1, "maxLength": 200}
UUID = {"type": "string", "format": "uuid"}
DATE = {"type": "string", "format": "date"}
REVISION = {"type": "integer", "minimum": 1}
STRING_MAP = {"type": "object", "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4000}}
PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "cursor": {"type": "string", "maxLength": 2000}}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
UNSET = object()


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def utc_clock():
    return datetime.now(timezone.utc)


def timestamp(clock=utc_clock):
    value = clock()
    if value.tzinfo is None:
        raise ValueError("Domain clocks must return aware timestamps")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_revision(row, expected):
    if row is None:
        if expected is not None:
            raise DomainError("revision_conflict", "Resource no longer exists")
    elif row["revision"] != expected:
        raise DomainError("revision_conflict", "Current resource revision is required", current_revision=row["revision"])


def rows(uow, table, business_id, *, where="", parameters=(), maximum=20000):
    spec = uow.database.assembly.tables[table]
    if not spec.scope_column:
        raise ValueError("Domain collection reads must be project scoped")
    clause = f'"{spec.scope_column}" = ?' if business_id is not None else f'"{spec.scope_column}" IS NULL'
    values = [business_id] if business_id is not None else []
    if where:
        clause += " AND (" + where + ")"
        values += list(parameters)
    found = uow.connection.execute(f'SELECT * FROM "{table}" WHERE {clause} LIMIT ?', [*values, maximum + 1]).fetchall()
    if len(found) > maximum:
        raise DomainError("result_too_large", "Narrow the query period or filters")
    return [decode_row(row) for row in found]


def page_position(arguments, business_id, operation):
    binding = canonical_hash({"business_id": business_id, "operation": operation,
                              "filters": {k: v for k, v in arguments.items() if k not in ("cursor", "limit")}})
    cursor = arguments.get("cursor")
    after = None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if decoded["binding"] != binding or not isinstance(decoded["after"], list):
                raise ValueError
            after = tuple(decoded["after"])
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise DomainError("validation_error", "Invalid or differently scoped list cursor") from exc
    return binding, after


def page(items, arguments, business_id, operation, *, key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True):
    binding, after = page_position(arguments, business_id, operation)
    items = sorted(items, key=key, reverse=reverse)
    if after is not None:
        try:
            items = [item for item in items if (key(item) < after if reverse else key(item) > after)]
        except TypeError as exc:
            raise DomainError("validation_error", "Invalid or differently scoped list cursor") from exc
    limit = arguments.get("limit", 50)
    selected = items[:limit]
    next_cursor = None
    if len(items) > limit:
        next_cursor = base64.urlsafe_b64encode(canonical_bytes({"binding": binding, "after": key(selected[-1])})).decode("ascii")
    return selected, next_cursor


def ref(table, key, business_id, namespace=None, *, tables=None):
    spec = active_tables(tables)[table]
    keys = tuple(value for value in spec.keys if value != spec.scope_column)
    identity = str(key) if len(keys) == 1 and not isinstance(key, dict) else canonical_bytes(key).decode()
    return ResourceRef(table, identity, business_id, namespace)


def record_key(resource, *, tables=None):
    spec = active_tables(tables)[resource.resource_type]
    keys = tuple(value for value in spec.keys if value != spec.scope_column)
    return resource.resource_id if len(keys) == 1 else json.loads(resource.resource_id)


class PlanBuilder:
    def __init__(self, ctx, operation, arguments, title, *, clock=utc_clock, namespace=None, affected_scope=None,
                 tables=None, version=1):
        self.ctx, self.operation, self.arguments, self.title = ctx, operation, arguments, title
        # The registered descriptor's version: an operation whose input or effect contract changed incompatibly declares a higher one.
        self.version = version
        # Without an explicit catalog a builder uses the running unit's, so it must be made inside prepare.
        self.tables = active_tables(tables)
        # A Global context (no selected business) defaults to the Global namespace.
        self.namespace = namespace or ("business:" + ctx.business_id if ctx.business_id else "global")
        self.affected_scope = affected_scope or ({"kind": "business", "business_id": ctx.business_id}
                                                 if ctx.business_id else {"kind": "global"})
        self.at = timestamp(clock)
        self.targets, self.changes, self.previews, self.resources, self.effects = {}, [], [], [], []

    def expect(self, resource, before):
        spec = self.tables.get(resource.resource_type)
        revision = before.get(spec.revision_column) if before and spec and spec.revision_column else None
        expected = ExpectedTarget(resource, revision=revision, exists=before is not None)
        old = self.targets.get(resource)
        if old is not None and old != expected:
            # A revision pin may tighten an earlier existence-only pin.
            if old != ExpectedTarget(resource, exists=True) or not expected.exists:
                raise DomainError("invalid_plan", "Prepared snapshots disagree")
        self.targets[resource] = expected
        return resource

    def record(self, table, key, before, values, *, action=None, operation_name=None, business_id=UNSET, label=None, namespace=None):
        business_id = self.ctx.business_id if business_id is UNSET else business_id
        resource = self.expect(ref(table, key, business_id, namespace, tables=self.tables), before)
        action = action or ("update" if before else "insert")
        values = plain(values)
        if action == "insert" and "created_at" in values:
            values["created_at"] = values["created_at"] or self.at
        self.changes.append(PreparedChange("record", resource, action, values, operation_name=operation_name))
        preview = PreviewResource(table, resource.resource_id, label or str(key))
        self.resources.append(preview)
        visible = values if action != "delete" else {key: None for key in before}
        fields = tuple(FieldChange(key, (before or {}).get(key), value) for key, value in visible.items()
                       if key not in {"created_at", "updated_at"} and (before or {}).get(key) != value)
        self.previews.append(StructuredPreview(preview, fields))
        return resource

    def policy_effect(self, resource, operation, description):
        self.changes.append(PreparedChange("record", resource, "policy_effect", {"description": description}, operation_name=operation))

    def build(self):
        scope = self.affected_scope
        resources = {}
        for resource in self.resources:
            value = plain(resource)
            resources.setdefault((value["resource_type"], value["resource_id"]), resource)
        return ChangeSet(self.operation, self.version, self.ctx.principal_id, self.namespace, self.arguments, scope,
            tuple(self.targets.values()), tuple(self.changes), ChangePreview(self.title, self.arguments.get("rationale", ""), scope,
                tuple(resources.values()), tuple(self.previews), tuple(self.effects)),
            review_business_id=self.ctx.review_business_id, proposer_agent_id=self.ctx.agent_id)


def apply_records(ctx, plan, uow):
    """Apply a plan's record/job effects in order; file effects and policy markers are not rows.

    The engine's own ``append_context_review`` effect (a review decision, its commit time allocated here) is written
    directly. Every other effect is first offered to the domain's ``apply_effect`` hook (a returned sequence means the
    domain wrote it); otherwise it is the generic insert/update/delete with revision CAS against the prepared target."""
    hooks = uow.database.assembly.record_hooks
    targets = {target.resource: target for target in plan.expected_targets}
    result = []
    for change in plan.changes:
        if change.kind == "file" or change.action == "policy_effect":
            continue
        if change.kind not in {"record", "job"}:
            raise DomainError("invalid_plan", "Unknown prepared effect kind")
        table = change.resource.resource_type
        repository = uow.repository(table, business_id=change.resource.business_id)
        key = record_key(change.resource)
        values = plain(change.values)
        if change.action == "append_context_review" and table == "context_reviews":
            # An engine-owned effect: the decision's content, identity, attribution and source token are frozen in the
            # prepared plan; only its commit time is allocated here, in the applying transaction. Review decisions are
            # append-only and, as in v1, record no shared-history event.
            values["created_at"] = timestamp()
            result.append(repository.insert(values))
            continue
        applied = hooks.apply_effect(ctx, plan, change, uow, repository, key, values)
        if applied is not None:
            result.extend(applied)
        elif change.action == "insert":
            result.append(repository.insert(values))
            hooks.record_applied(uow, plan, table, None, result[-1])
        elif change.action == "update":
            before = repository.get(key) if table in hooks.tracked_tables else None
            result.append(repository.update(key, values, expected_revision=targets[change.resource].revision,
                                            force_revision=bool(hooks.force_revision(plan, change))))
            hooks.record_applied(uow, plan, table, before, result[-1])
        elif change.action == "delete":
            result.append(repository.delete(key, expected_revision=targets[change.resource].revision))
            hooks.record_applied(uow, plan, table, result[-1], None)
        else:
            raise DomainError("invalid_plan", "Unrecognized prepared record action")
    return result


def prepare_job(builder, uow, kind, arguments, *, programs, definition_extra=None):
    """Prepare a background job through the host's registered programs (a typed port, not imported here)."""
    if not hasattr(programs, "prepare_job") or programs.get(kind) is None:
        raise DomainError("operation_unavailable", "Named background program is not installed")
    return programs.prepare_job(builder, uow, kind, arguments, definition_extra=definition_extra)
