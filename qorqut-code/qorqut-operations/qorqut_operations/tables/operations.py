"""Business-shared and Global general tables, explicit schema edits and atomic row batches."""

from datetime import date
from dataclasses import replace
import json
import math

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, canonical_bytes, new_id, plain
from qorqut_operations.application.plans import (PAGE, TEXT, UUID, PlanBuilder, apply_records, page, prepare_job, ref,
                                                 require_revision, rows, schema, utc_clock)
from qorqut_operations.records.associations import TASK_IDS, link_new_resource
from .history import TableHistory

KEY = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
LABEL = {"type": "string", "minLength": 1, "maxLength": 200}
COLUMN = schema({"id": KEY, "name": LABEL, "type": {"enum": ["text", "number", "boolean", "date", "json"]}}, ("id", "name", "type"))
COLUMNS = {"type": "array", "items": COLUMN, "minItems": 1, "maxItems": 100}
TAGS = schema({key: {"type": "array", "items": LABEL, "uniqueItems": True, "maxItems": 100}
               for key in ("topics",)})
FIELDS = {"title": LABEL, "description": {"type": "string", "maxLength": 10000}, "columns": COLUMNS,
          "tags": TAGS, "archived": {"type": "boolean"}}
CELLS = {"type": "object", "maxProperties": 100, "propertyNames": KEY}
ROW_ID = {"anyOf": [UUID, {"type": "string", "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"}]}
ROW = schema({"id": ROW_ID, "cells": CELLS, "source_upload_id": {"anyOf": [UUID, {"type": "null"}]}}, ("cells",))
MAX_ROWS, MAX_SNAPSHOT = 50000, 10 * 1024 * 1024
SCOPE = {"scope": {"enum": ["business", "global"]}}
ARCHIVE = "tables_archive"


def table_namespace(ctx, args):
    if args.get("scope") == "global" or (not ctx.business_id and args.get("scope") != "business"):
        return "global"
    return "business:" + (ctx.business_id or "")


def table_context(ctx, args, uow):
    """The Global namespace has no business; only the owner and active ecosystem agents may enter it."""
    if table_namespace(ctx, args) != "global":
        return ctx
    if ctx.home_business_id is not None:
        raise DomainError("forbidden", "Project agents cannot access Global tables")
    if ctx.principal_kind == "human":
        installation = uow.repository("installation").get("service")
        allowed = installation and installation["owner_principal_id"] == ctx.principal_id
    else:
        profile = uow.repository("agent_profiles", business_id=None).get(ctx.principal_id)
        allowed = (ctx.principal_kind == "agent" and profile and profile["status"] == "active"
                   and profile["profile_json"]["scope"]["kind"] == "ecosystem")
    if not allowed or "read_global_context" not in ctx.global_capabilities:
        raise DomainError("forbidden", "Global tables require the owner or an authorized global agent")
    return replace(ctx, business_id=None, review_business_id=None)


SCOPED_RESOURCES = frozenset({"data_tables", "data_rows", "table_versions", "jobs"})
# Business publication keeps its original program so jobs queued before Global tables still match their frozen definition.
VERSION_PROGRAM, GLOBAL_VERSION_PROGRAM = "tables.version", "tables.global-version"


def version_program(business_id):
    return GLOBAL_VERSION_PROGRAM if business_id is None else VERSION_PROGRAM


def plan_program(plan):
    return GLOBAL_VERSION_PROGRAM if plan.target_namespace == "global" else VERSION_PROGRAM


def archives(before, values):
    """Whether a table change moves it from active to archived."""
    return values.get("archived") is True and not (before and before["archived"])


def columns_checked(columns):
    ids, names = set(), set()
    for column in columns:
        identity, name = column["id"], column["name"].strip().casefold()
        if not name or identity in ids or name in names:
            raise DomainError("validation_error", "Column IDs and names must be non-empty and unique")
        ids.add(identity)
        names.add(name)
    return {column["id"]: column for column in columns}


def cells_checked(cells, columns):
    if set(cells) - columns.keys():
        raise DomainError("validation_error", "Cells name an unknown column", columns=sorted(set(cells) - columns.keys()))
    for key, value in cells.items():
        if value is None:
            continue
        kind = columns[key]["type"]
        valid = (kind == "json" or kind == "text" and isinstance(value, str)
                 or kind == "boolean" and type(value) is bool
                 or kind == "number" and (type(value) is int or type(value) is float and math.isfinite(value)))
        if kind == "date":
            try:
                valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
        if not valid:
            raise DomainError("validation_error", f"Column {key} expects {kind} or null; values are never silently converted")
    try:
        encoded = json.dumps(plain(cells), ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise DomainError("validation_error", "Cells must contain finite JSON values") from exc
    if len(encoded) > 200000:
        raise DomainError("validation_error", "A row must be at most 200 KB")


class TableOperations:
    def __init__(self, database, operations_root, project_hq, *, clock=utc_clock, job_programs=None):
        self.database, self.clock, self.job_programs = database, clock, job_programs
        self.history = TableHistory(database, operations_root, project_hq)

    def register(self, registry):
        reads = {
            "tables_list": ({**PAGE, "query": TEXT, "include_archived": {"type": "boolean"},
                             "topic": LABEL}, ()),
            "tables_get": ({"table_id": UUID}, ("table_id",)),
            "tables_rows_list": ({**PAGE, "table_id": UUID, "query": TEXT, "equals": CELLS, "order_by": KEY, "descending": {"type": "boolean"}}, ("table_id",)),
            "tables_history": ({**PAGE, "table_id": UUID}, ("table_id",)),
        }
        scoped = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context", resolve_namespace=table_namespace)
        descriptions = {
            "tables_list": "Find shared project tables by title or topic. All project agents use the same tables. Use scope=global for Global general tables.",
            "tables_get": "Read a table's columns, revision, associations and Git history status before editing it. Global tables need scope=global.",
            "tables_rows_list": "Read paginated table rows, optionally searching values or matching cells exactly. Pagination restarts if the table changes.",
            "tables_history": "Read the saved Git commit or pending snapshot for each table revision. Does not return raw upload contents.",
            "tables_create": "Create a shared project table, or with scope=global a Global general table (owner and authorized global agents; no Task associations or uploads). Choose stable column IDs, display names and text/number/boolean/date/json types. No formulas execute. Creating an archived Global table needs owner approval.",
            "tables_update": "Update title, description, tags, archive state or the full column list with expected_revision. Rename a column by keeping its ID. Removing a column deletes its cells. Type changes must fit existing values. Archiving a Global table needs owner approval; restoring one does not.",
            "tables_rows_write": "Atomically insert rows without IDs, patch existing rows by ID, or remove rows with delete_ids. Supply the table's expected_revision. Omitted cells are preserved; null clears a cell. Every batch gets one Git snapshot. Reuse the idempotency key after a lost response.",
            "tables_history_retry": "Queue publication of a pending table version and any earlier pending versions in this project, or in Global with scope=global. Never modifies table data.",
            ARCHIVE: "Archive a table with expected_revision. For Global tables an agent's request always needs the installation owner's approval; restore with tables_update archived=false. Archived tables keep their data but reject row edits.",
        }
        for name, (fields, required) in reads.items():
            registry.register(OperationDescriptor(name, schema({**SCOPE, **fields}, required), description=descriptions[name],
                effect_kind="read", supports_signoff=False, **scoped,
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        writes = {
            "tables_create": ({**FIELDS, "task_ids": TASK_IDS}, ("title", "columns")),
            "tables_update": ({"table_id": UUID, "changes": {**schema(FIELDS), "minProperties": 1}}, ("table_id", "changes")),
            "tables_rows_write": ({"table_id": UUID, "rows": {"type": "array", "items": ROW, "maxItems": 1000},
                                   "delete_ids": {"type": "array", "items": ROW_ID, "maxItems": 1000, "uniqueItems": True}}, ("table_id",)),
            "tables_history_retry": ({"version_id": UUID}, ("version_id",)),
            ARCHIVE: ({"table_id": UUID}, ("table_id",)),
        }
        for name, (fields, required) in writes.items():
            registry.register(OperationDescriptor(name, schema({**SCOPE, **fields, "rationale": TEXT}, required), description=descriptions[name],
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow), apply=self.apply,
                validate_targets=self.validate_scope, **scoped))

    @staticmethod
    def table(ctx, identity, uow):
        return uow.repository("data_tables", business_id=ctx.business_id).get(identity, required=True)

    @staticmethod
    def table_rows(ctx, identity, uow):
        return rows(uow, "data_rows", ctx.business_id, where="table_id=?", parameters=(identity,), maximum=MAX_ROWS)

    @staticmethod
    def row_view(row):
        return {("cells" if key == "cells_json" else key): value for key, value in row.items()}

    def view(self, ctx, row, uow):
        result = {({"columns_json": "columns", "tags_json": "tags"}.get(key, key)): value for key, value in row.items()}
        result["archived"] = bool(row["archived"])
        result["row_count"] = uow.connection.execute("SELECT count(*) FROM data_rows WHERE business_id IS ? AND table_id=?",
            (ctx.business_id, row["id"])).fetchone()[0]
        version = uow.connection.execute("SELECT id,git_commit FROM table_versions WHERE business_id IS ? AND table_id=? ORDER BY sequence DESC LIMIT 1",
            (ctx.business_id, row["id"])).fetchone()
        result["versioning"] = {"state": "saved" if version and version["git_commit"] else "pending",
            "version_id": version["id"] if version else None, "commit": version["git_commit"] if version else None}
        return result

    def read(self, operation, ctx, args, uow):
        ctx = table_context(ctx, args, uow)
        if operation == "tables_list":
            found = rows(uow, "data_tables", ctx.business_id)
            found = [row for row in found if (args.get("include_archived") or not row["archived"])
                and (not args.get("query") or args["query"].casefold() in (row["title"] + " " + row["description"]).casefold())
                and (not args.get("topic") or args["topic"] in row["tags_json"].get("topics", []))]
            selected, cursor = page(found, args, ctx.business_id, operation)
            return {"items": [self.view(ctx, row, uow) for row in selected], "next_cursor": cursor}
        table = self.table(ctx, args["table_id"], uow)
        if operation == "tables_history":
            found = [dict(row) for row in uow.connection.execute(
                "SELECT id,table_id,table_revision,sequence,actor_id,operation,path,snapshot_hash,git_commit,created_at "
                "FROM table_versions WHERE business_id IS ? AND table_id=?", (ctx.business_id, args["table_id"]))]
            selected, cursor = page(found, args, ctx.business_id, operation, key=lambda row: (row["sequence"], row["id"]))
            return {"items": selected, "next_cursor": cursor}
        if operation == "tables_get":
            return {"table": self.view(ctx, table, uow)}
        columns = columns_checked(table["columns_json"])
        cells_checked(args.get("equals", {}), columns)
        found = [row for row in self.table_rows(ctx, table["id"], uow)
                 if all(key in row["cells_json"] and (row["cells_json"][key] == value if columns[key]["type"] == "number"
                     else canonical_bytes(row["cells_json"][key]) == canonical_bytes(value)) for key, value in args.get("equals", {}).items())
                 and (not args.get("query") or args["query"].casefold() in json.dumps(list(row["cells_json"].values()), ensure_ascii=False).casefold())]
        key = lambda row: (row["position"], row["id"])
        if args.get("order_by"):
            column = columns.get(args["order_by"])
            if column is None or column["type"] == "json":
                raise DomainError("validation_error", "Sort by a known text, number, boolean or date column")
            def key(row):
                value = row["cells_json"].get(args["order_by"])
                return value is not None, value if value is not None else "", row["position"], row["id"]
        selected, cursor = page(found, {**args, "table_revision": table["revision"]}, ctx.business_id, operation, key=key, reverse=args.get("descending", False))
        return {"items": [self.row_view(row) for row in selected], "next_cursor": cursor, "table_revision": table["revision"]}

    def prepare(self, operation, ctx, args, options, uow):
        ctx = table_context(ctx, args, uow)
        builder = PlanBuilder(ctx, operation, args, operation.replace("_", " ").title(), clock=self.clock)
        scoped = builder.ctx.business_id is None
        if operation == "tables_history_retry":
            version = uow.repository("table_versions", business_id=ctx.business_id).get(args["version_id"], required=True)
            prepare_job(builder, uow, version_program(builder.ctx.business_id), {"version_id": version["id"]}, programs=self.job_programs)
            return builder.build()
        if operation == "tables_create":
            identity = new_id()
            if scoped and args.get("task_ids"):
                raise DomainError("validation_error", "Global tables have no Task associations")
            values = {"id": identity, "title": args["title"].strip(), "description": args.get("description", ""),
                "columns_json": args["columns"], "tags_json": args.get("tags", {}), "archived": args.get("archived", False),
                "created_by": ctx.principal_id, "created_at": builder.at, "updated_at": builder.at}
            columns_checked(values["columns_json"])
            if not values["title"]:
                raise DomainError("validation_error", "Table title must not be blank")
            resource = builder.record("data_tables", identity, None, values, label=values["title"])
            if scoped and archives(None, values):
                builder.policy_effect(resource, ARCHIVE, "Archives the new Global table")
            if not scoped:
                link_new_resource(builder, uow, "data_tables", identity, args.get("task_ids", ()))
        else:
            identity = args["table_id"]
            current = self.table(ctx, identity, uow)
            require_revision(current, options.expected_revision)
            all_rows = self.table_rows(ctx, identity, uow)
            if operation in {"tables_update", ARCHIVE}:
                changes = {"archived": True} if operation == ARCHIVE else args["changes"]
                if operation == ARCHIVE and current["archived"]:
                    raise DomainError("validation_error", "The table is already archived")
                values = {{"columns": "columns_json", "tags": "tags_json"}.get(key, key): value for key, value in changes.items()}
                if "title" in values:
                    values["title"] = values["title"].strip()
                    if not values["title"]:
                        raise DomainError("validation_error", "Table title must not be blank")
                if "columns_json" in values:
                    columns = columns_checked(values["columns_json"])
                    for row in all_rows:
                        cells = {key: value for key, value in row["cells_json"].items() if key in columns}
                        cells_checked(cells, columns)
                        if cells != row["cells_json"]:
                            builder.record("data_rows", row["id"], row, {"cells_json": cells, "updated_by": ctx.principal_id})
            else:
                if current["archived"]:
                    raise DomainError("validation_error", "Unarchive the table before editing rows")
                updates, deletes = args.get("rows", []), set(args.get("delete_ids", []))
                if not updates and not deletes:
                    raise DomainError("validation_error", "Supply rows or delete_ids")
                self.append_rows(builder, uow, current, all_rows, updates, deletes)
                values = {}
            resource = builder.record("data_tables", identity, current, {**values, "updated_at": builder.at}, label=current["title"])
            if archives(current, values) and (scoped or operation == ARCHIVE):
                # Owner approval is the archive effect's own policy, whichever operation carries it.
                builder.policy_effect(resource, ARCHIVE, "Archives the table; its data stays available to restore")
        version_id = new_id()
        self.history.prepare(builder, identity, version_id, uow)
        prepare_job(builder, uow, version_program(builder.ctx.business_id), {"version_id": version_id}, programs=self.job_programs)
        return builder.build()

    def append_rows(self, builder, uow, table, all_rows, updates, deletes):
        ctx, identity = builder.ctx, table["id"]
        columns = columns_checked(table["columns_json"])
        existing = {row["id"]: row for row in all_rows}
        position = max((row["position"] for row in all_rows), default=-1) + 1
        seen = set(deletes)
        if deletes - existing.keys():
            raise DomainError("not_found", "A row to delete does not belong to this table")
        for key in deletes:
            builder.record("data_rows", key, existing[key], {}, action="delete")
        for item in updates:
            key = item.get("id") or new_id()
            if ctx.business_id is None and item.get("source_upload_id") is not None:
                raise DomainError("validation_error", "Global table rows cannot reference a Data Upload")
            if key in seen:
                raise DomainError("validation_error", "A batch cannot change a row more than once")
            seen.add(key)
            before = existing.get(key)
            if "id" in item and before is None:
                raise DomainError("not_found", "A row to update does not belong to this table; omit id to insert")
            cells = {**(before or {}).get("cells_json", {}), **item["cells"]}
            cells_checked(cells, columns)
            changes = {"cells_json": cells, "updated_by": ctx.principal_id, "updated_at": builder.at}
            if "source_upload_id" in item:
                source = item["source_upload_id"]
                if source:
                    upload = uow.repository("documents", business_id=ctx.business_id).get(source, required=True)
                    if upload["kind"] != "data_upload" or upload["availability"] != "ready":
                        raise DomainError("validation_error", "Source must be a ready Data Upload in this project")
                changes["source_upload_id"] = source
            if not before:
                changes.update(id=key, table_id=identity, position=position, created_by=ctx.principal_id, created_at=builder.at)
                position += 1
            builder.record("data_rows", key, before, changes)
        if len(existing) - len(deletes) + sum("id" not in item for item in updates) > MAX_ROWS:
            raise DomainError("validation_error", f"A table supports up to {MAX_ROWS} rows")

    def apply_all(self, ctx, plan, uow):
        """Apply a plan once and capture every affected table in its transaction."""
        applied = apply_records(ctx, replace(plan, changes=tuple(change for change in plan.changes if change.action != "capture_table_version")), uow)
        results = []
        for change in (c for c in plan.changes if c.action == "capture_table_version"):
            table = self.table(ctx, change.values["table_id"], uow)
            snapshot = {"schema_version": 1, "table": table, "rows": sorted(self.table_rows(ctx, table["id"], uow), key=lambda row: row["id"])}
            if len(canonical_bytes(snapshot)) > MAX_SNAPSHOT:
                raise DomainError("validation_error", "A table snapshot must be at most 10 MiB; split this dataset into tables")
            self.history.capture(ctx, table, snapshot, change, uow)
            job = next(r for r in applied if isinstance(r, dict) and r.get("kind") == plan_program(plan) and r["arguments_json"]["version_id"] == change.values["id"])
            deleted = {c.resource.resource_id for c in plan.changes if c.resource.resource_type == "data_rows" and c.action == "delete"}
            results.append({"table": self.view(ctx, table, uow),
                "rows": [self.row_view(r) for r in applied if isinstance(r, dict) and r.get("table_id") == table["id"] and "cells_json" in r and r["id"] not in deleted],
                "deleted_ids": sorted(deleted), "job_id": job["id"]})
        return results, applied

    @staticmethod
    def validate_scope(ctx, plan, uow):
        """Re-derive scope and archive authority from the stored plan, never from an ID's shape."""
        business = None if plan.target_namespace == "global" else plan.target_namespace.removeprefix("business:")
        for resource in [*(target.resource for target in plan.expected_targets), *(change.resource for change in plan.changes)]:
            if resource.resource_type in SCOPED_RESOURCES and resource.business_id != business:
                raise DomainError("invalid_plan", "Prepared table effects must stay in the operation's scope")
        if business is not None:
            return
        for change in plan.changes:
            if change.resource.resource_type != "data_tables" or change.action not in {"insert", "update"}:
                continue
            before = uow.repository("data_tables", business_id=None).get(change.resource.resource_id) if change.action == "update" else None
            if archives(before, change.values) and not any(
                    other.action == "policy_effect" and other.operation_name == ARCHIVE and other.resource == change.resource
                    for other in plan.changes):
                raise DomainError("invalid_plan", "Archiving a Global table requires its archive approval effect")

    def apply(self, ctx, plan, uow):
        if plan.target_namespace == "global":
            ctx = table_context(ctx, {"scope": "global"}, uow)
        results, applied = self.apply_all(ctx, plan, uow)
        if plan.operation_name == "tables_history_retry":
            job = next(r for r in applied if isinstance(r, dict) and r.get("kind") == plan_program(plan))
            return {"job_id": job["id"], "version_id": job["arguments_json"]["version_id"]}
        return results[0]
