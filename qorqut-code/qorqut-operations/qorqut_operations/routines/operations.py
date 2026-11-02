"""Typed routine definitions and manual run intents; no executable argv input."""

from dataclasses import replace
from datetime import datetime, timezone

from qorqut_operations.departments import normalize_departments, ACCEPTED_IDS
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.background.schedule import RoutineError, next_slot, parse_cron, zone
from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain
from qorqut_operations.application.plans import (IDENTITY, PAGE, TEXT, UUID, PlanBuilder, apply_records, page, ref,
    require_revision, rows, schema, page_position)
from qorqut_operations.storage.repositories import decode_row
from .reads import activity, history_scope

DEFINITION = schema({"name": {"type": "string", "minLength": 1, "maxLength": 80}, "why": TEXT,
    "schedule": {"type": "string", "maxLength": 100}, "timezone": {"type": "string", "maxLength": 100},
    "status": {"enum": ["active", "paused"]}, "program": {"type": "string", "maxLength": 100},
    "arguments": {"type": "object"}, "target": {"enum": ["business", "each-business", "root"]},
    "departments": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True,
        "items": {"enum": list(ACCEPTED_IDS)}},
    "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 7200}},
    ["name", "schedule", "timezone", "status", "program", "arguments"])
SCOPE = {"scope": {"enum": ["business", "global"]}}
SINCE = {"anyOf": [{"type": "string", "format": "date-time"},
                    {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"}]}


def namespace(ctx, args):
    return "global" if args.get("scope") == "global" else "business:" + (ctx.business_id or "")


# Authority metadata of the routine operations (deployed v1 catalog lines, unchanged).
ROUTINE_READS = ("routines_list", "routines_get", "routines_preview", "routines_runs", "routines_costs")
ROUTINE_MUTATIONS = ("routines_create", "routines_update", "routines_delete", "routines_pause", "routines_resume",
                     "routines_run", "routines_stop")
ROUTINE_ENTRIES = (*(CatalogEntry(name, False, "read") for name in ROUTINE_READS),
                   *(CatalogEntry(name, True, "records") for name in ROUTINE_MUTATIONS))


class RoutineOperations:
    """Construction is inert and needs no application: the descriptors go into the host's frozen catalog through
    ``contribution()`` and the application then calls their handlers."""

    def __init__(self, programs, authority, *, clock=None):
        self.programs, self.authority = programs, authority
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def scope(ctx, args):
        return (None, "global") if args.get("scope") == "global" else (ctx.business_id, ctx.business_id)

    def row(self, ctx, args, uow):
        business, ns = self.scope(ctx, args)
        return uow.repository("routines", business_id=business).get({"namespace": ns, "id": args["routine_id"]}, required=True)

    def dto(self, row):
        program = self.programs.get(row["job_kind"])
        progress = row["metadata_json"].get("legacy_progress")
        reason = {"unavailable": "Imported scheduler progress has not been supplied",
                  "recovery_required": "An unfinished legacy routine needs an owner disposition"}.get(progress)
        return {**row, "available": bool(program and not program["unavailable_reason"]),
                "unavailable_reason": program["unavailable_reason"] if program else "Program is not registered",
                "scheduling_available": bool(program and not program["unavailable_reason"] and not reason),
                "scheduling_unavailable_reason": reason,
                "program": row["job_kind"], "arguments": row["arguments_json"], "departments": normalize_departments(row["departments_json"])}

    def list(self, ctx, args, options, uow):
        business, ns = self.scope(ctx, args)
        values = [self.dto(row) for row in rows(uow, "routines", business)
                  if row["namespace"] == ns and not row["metadata_json"].get("retired")]
        items, cursor = page(values, args, business, "routines_list")
        return {"items": items, "next_cursor": cursor}

    def get(self, ctx, args, options, uow):
        return {"routine": self.dto(self.row(ctx, args, uow))}

    def preview(self, ctx, args, options, uow):
        try:
            tz = zone(args["timezone"])
            cursor = datetime.fromisoformat(args["after"].replace("Z", "+00:00")) if args.get("after") else self.clock()
            if cursor.tzinfo is None:
                raise ValueError("Preview instant requires a timezone")
            slots = []
            for _ in range(5):
                cursor = next_slot(args["schedule"], cursor, tz)
                slots.append({"utc": cursor.isoformat().replace("+00:00", "Z"), "local": cursor.astimezone(tz).isoformat()})
            return {"slots": slots, "timezone": args["timezone"]}
        except (RoutineError, ValueError) as exc:
            raise DomainError("validation_error", str(exc)) from exc

    def history(self, ctx, args, options, uow):
        business, ns = self.scope(ctx, args)
        if args.get("view"):
            return activity(uow, ctx, args, self.clock())
        clause, parameters = history_scope(ctx, args)
        if args.get("costs"):
            values = [decode_row(row) for row in uow.connection.execute(
                "SELECT namespace,routine_id,business_id,cost_json FROM routine_runs WHERE " + clause, parameters)]
            by_routine, by_business = {}, {}
            for value in values:
                key = value["namespace"] + "/" + value["routine_id"]
                amount = value["cost_json"].get("usd", 0)
                by_routine[key] = round(by_routine.get(key, 0) + amount, 8)
                target = value["business_id"] or "global"
                by_business[target] = round(by_business.get(target, 0) + amount, 8)
            return {"usd": round(sum(x["cost_json"].get("usd", 0) for x in values), 8), "runs": len(values),
                "routines": by_routine, "businesses": by_business}
        _, after = page_position(args, business, "routines_runs")
        if after is not None:
            if len(after) != 2 or any(not isinstance(value, str) for value in after):
                raise DomainError("validation_error", "Invalid routine history cursor")
            clause += " AND (created_at,id)<(?,?)"
            parameters.extend(after)
        values = [decode_row(row) for row in uow.connection.execute(
            "SELECT * FROM routine_runs WHERE " + clause + " ORDER BY created_at DESC,id DESC LIMIT ?",
            [*parameters, args.get("limit", 50) + 1])]
        items, cursor = page(values, args, business, "routines_runs")
        return {"items": items, "next_cursor": cursor}

    def _builder(self, ctx, name, args, title):
        target = replace(ctx, business_id=None) if args.get("scope") == "global" else ctx
        return PlanBuilder(target, name, args, title, namespace=namespace(ctx, args))

    @staticmethod
    def _plan(builder, routine):
        plan = builder.build()
        action = builder.operation.removeprefix("routines_").capitalize()
        preview = plain(plan.preview)
        preview["title"] = f"{action} {routine['name']}"
        preview["rationale"] = routine.get("metadata_json", {}).get("why", "")
        # Owner cards use readable resource labels; exact structured fields remain
        # bound in the stored proposal for validation and application.
        for resource in preview["resources"]:
            resource["label"] = routine["name"]
        for change in preview["changes"]:
            change["resource"]["label"] = routine["name"]
        if builder.operation in {"routines_create", "routines_update"}:
            target = {"root": "Global workspace", "each-business": "Each authorized business", "business": "This business"}[routine["target"]]
            preview["effects"] = [f"{routine['job_kind']} · {routine['schedule']} ({routine['timezone']}) · {target} · {routine['status']}"]
        plan = replace(plan, preview=preview)
        if builder.namespace == "global":
            scope = {"kind": "global", "namespace": "routines"}
            return replace(plan, affected_scope=scope, preview={**plain(plan.preview), "affected_scope": scope})
        return plan

    def definition_values(self, ctx, args, previous=None):
        data = args["definition"]
        if (data['program'] in {'learnings.extract', 'learnings.extract_global'}
                and data['arguments'].get('supersession_mode') == 'apply' and ctx.principal_kind != 'human'):
            raise DomainError('forbidden', 'Only the owner may configure automatic learning supersession')
        try:
            parse_cron(data["schedule"])
            zone(data["timezone"])
        except RoutineError as exc:
            raise DomainError("validation_error", str(exc)) from exc
        program = self.programs.require(data["program"], available=False)
        if program.unavailable_reason:
            if data["status"] != "paused":
                raise DomainError("operation_unavailable", program.unavailable_reason)
        else:
            program.validate(data["arguments"])
        business, ns = self.scope(ctx, args)
        target = data.get("target", "business" if business else "root")
        if (business is not None and target != "business") or (business is None and target == "business"):
            raise DomainError("validation_error", "Routine target does not match its scope")
        if ("global" if target == "root" else "business") not in program.scope_kinds:
            raise DomainError("validation_error", "Registered program does not support this routine target")
        old_meta = plain(previous["metadata_json"]) if previous else {}
        changed = previous is None or (previous["schedule"], previous["timezone"]) != (data["schedule"], data["timezone"])
        return {"name": data["name"], "schedule": data["schedule"], "timezone": data["timezone"],
            "status": data["status"], "job_kind": data["program"], "arguments_json": data["arguments"], "target": target,
            "timeout_seconds": data.get("timeout_seconds", program.timeout_seconds),
            "departments_json": normalize_departments(data.get("departments", ["operations"])),
            "metadata_json": {**old_meta, "why": data.get("why", ""), "schedule_revision":
                old_meta.get("schedule_revision", previous["revision"] if previous else 0) + int(changed)}}

    def targets(self, ctx, row, uow):
        if row["scope"] == "business":
            return [row["business_id"]]
        if row["target"] == "root":
            return [None]
        # One schedule/run group, with isolated outputs and cursors per business. A saved target keeps its meaning: an
        # each-business routine never also runs in Global, and a root one never fans out.
        return [x[0] for x in uow.connection.execute("SELECT id FROM businesses ORDER BY id")
                if x[0] in ctx.allowed_business_ids]

    def add_run(self, builder, row, business_id, uow, *, trigger="manual", slot=None, missed=0, group_id=None):
        run_id, job_id = new_id(), new_id()
        group_id = group_id or canonical_hash([row["namespace"], row["id"], trigger, slot or run_id])
        context = replace(builder.ctx, business_id=business_id)
        values = self.programs.job_values(context, row["job_kind"], row["arguments_json"], uow, job_id=job_id,
            definition_extra={"routine_run_id": run_id, "routine_id": row["id"], "routine_revision": row["revision"],
                "trigger": trigger, "run_timeout_seconds": row["timeout_seconds"], "scope": row["scope"],
                "run_group_id": group_id, "missed": missed})
        builder.record("routine_runs", run_id, None, {"id": run_id, "namespace": row["namespace"], "routine_id": row["id"],
            "business_id": business_id, "trigger": trigger, "slot": slot, "state": "queued", "started_at": builder.at,
            "result_json": {"job_id": job_id, "run_group_id": group_id, "missed": missed, "definition": {
                "name": row["name"], "program": row["job_kind"], "arguments": row["arguments_json"],
                "departments": normalize_departments(row["departments_json"]), "revision": row["revision"], "timezone": row["timezone"]}}}, business_id=business_id)
        builder.record("jobs", job_id, None, values, business_id=business_id)
        return job_id

    def prepare(self, name, ctx, args, options, uow):
        builder = self._builder(ctx, name, args, name.replace("_", " ").capitalize())
        business, ns = self.scope(ctx, args)
        if name == "routines_create":
            identity = new_id()
            values = {"namespace": ns, "id": identity, "business_id": business, "scope": "global" if business is None else "business",
                      **self.definition_values(ctx, args)}
            builder.record("routines", {"namespace": ns, "id": identity}, None, values)
        else:
            row = self.row(ctx, args, uow)
            if row["metadata_json"].get("retired"):
                raise DomainError("not_found", "Routine is retired")
            require_revision(row, options.expected_revision)
            key = {"namespace": ns, "id": row["id"]}
            if name == "routines_update":
                values = self.definition_values(ctx, args, row)
                builder.record("routines", key, row, values)
            elif name in {"routines_pause", "routines_resume", "routines_delete"}:
                if name == "routines_resume":
                    self.programs.require(row["job_kind"])
                values = {"status": "active" if name == "routines_resume" else "paused"}
                if name == "routines_delete":
                    values["metadata_json"] = {**row["metadata_json"], "retired": True}
                builder.record("routines", key, row, values)
            elif name == "routines_run":
                if row["metadata_json"].get("retired"):
                    raise DomainError("not_found", "Routine is retired")
                builder.expect(ref("routines", key, business), row)
                targets = self.targets(ctx, row, uow)
                if not targets:
                    raise DomainError("forbidden", "Routine has no authorized business targets")
                group_id = new_id()
                for target in targets:
                    active = uow.connection.execute("SELECT 1 FROM jobs WHERE json_extract(definition_ref_json,'$.routine_id')=? "
                        "AND business_id IS ? AND state IN ('queued','running','applying','waiting_signoff','cancel_requested') LIMIT 1",
                        (row["id"], target)).fetchone()
                    if active:
                        raise DomainError("busy", "Routine is already running or queued", retryable=True)
                    self.add_run(builder, row, target, uow, group_id=group_id)
            elif name == "routines_stop":
                target_business = args.get("target_business_id", business)
                if target_business is not None and (target_business not in ctx.allowed_business_ids
                        or business is not None and target_business != business):
                    raise DomainError("forbidden", "Routine job target is outside this scope")
                job = uow.repository("jobs", business_id=args.get("target_business_id", business)).get(args["job_id"], required=True)
                if job["definition_ref_json"].get("routine_id") != row["id"] or job["state"] not in {"queued", "running", "applying", "waiting_signoff"}:
                    raise DomainError("revision_conflict", "Routine job is no longer stoppable")
                if "outputs" in (job["result_json"] or {}) or "dispatch_intent" in (job["result_json"] or {}):
                    raise DomainError("invalid_transition", "Prepared application or external dispatch has begun; pending signoffs can be rejected separately")
                builder.expect(ref("routines", key, business), row)
                builder.record("jobs", job["id"], job, {"state": "cancel_requested"}, business_id=job["business_id"])
        return self._plan(builder, values if name == "routines_create" else {**row, **(values if name == "routines_update" else {})})

    @staticmethod
    def apply(ctx, plan, uow):
        result = apply_records(ctx, plan, uow)
        return {"records": result, "job_ids": [x["id"] for x in result if isinstance(x, dict) and "dedupe_key" in x]}

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("routines", ROUTINE_ENTRIES, self.descriptors())

    def descriptors(self):
        descriptors = []
        common = dict(scope_kinds=frozenset({"business", "global"}), global_capability="manage_global_routines", resolve_namespace=namespace)
        for name, properties, handler in (
            ("routines_list", PAGE, self.list), ("routines_get", {"routine_id": IDENTITY}, self.get),
            ("routines_runs", {**PAGE, "routine_id": IDENTITY, "since": SINCE,
                "view": {"enum": ["recent", "hourly"]}}, self.history),
            ("routines_costs", {"routine_id": IDENTITY, "since": SINCE},
                lambda c,a,o,u: self.history(c,{**a,"costs":True},o,u)),
            ("routines_preview", {"schedule": {"type": "string", "maxLength": 100}, "timezone": {"type": "string", "maxLength": 100},
                                  "after": {"type": "string", "format": "date-time"}}, self.preview)):
            required = ["routine_id"] if name == "routines_get" else (["schedule", "timezone"] if name == "routines_preview" else [])
            descriptors.append(OperationDescriptor(name, schema({**SCOPE, **properties}, required), effect_kind="read",
                supports_signoff=False, read=handler, **common))
        for name in ("routines_create", "routines_update", "routines_pause", "routines_resume", "routines_delete", "routines_run", "routines_stop"):
            properties, required = {**SCOPE}, []
            if name != "routines_create":
                properties["routine_id"], required = IDENTITY, ["routine_id"]
            if name in {"routines_create", "routines_update"}:
                properties["definition"], required = DEFINITION, [*required, "definition"]
            if name == "routines_stop":
                properties.update(job_id=UUID, target_business_id=UUID)
                required.append("job_id")
            descriptors.append(OperationDescriptor(name, schema(properties, required),
                prepare=lambda c,a,o,u,n=name: self.prepare(n,c,a,o,u), apply=self.apply, **common))
        return tuple(descriptors)
