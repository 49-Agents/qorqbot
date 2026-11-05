"""The ``automations_*`` tools: scoped automation records, their script, schedule, lifecycle, runs, grants and agent answers.

Scope. An automation is a normal scoped record: Global (``scope: global``, ``business_id`` NULL) or owned by one Project (the selected
Project). Every tool takes the same ``scope`` argument the routine tools use, resolves its namespace the same way and runs through the
engine's ordinary authorization, so a Project automation is only visible and manageable inside its Project.

Authority. Management - create, edit, script, schedule, lifecycle, runs, grants and answers - belongs to the configured Ordubashy holding
the explicit ``manage_automations`` capability (an ecosystem profile with the capability alone is not enough), re-checked when a plan is
applied. Reads follow the engine's current operation policy: the owner and the manager see the whole record; every other authorized
reader sees a safe projection and the published output and history. Nothing a script runs can call any ``automations_*`` mutation.

Composition, not duplication. The script is one Python tool entry in this extension's own Agentic plugin package, written through the
existing Agentic authoring batch (validated, journaled, attributed); the schedule is an ordinary routine of the ``automation.run`` program
composed through the existing routine definition rules in the same plan as the automation record; a question for the managing agent is
answered through the existing delegated input answer. Owner-granted authority is typed and exact (see ``guard``) and always an owner
Signoff; mandatory domain Signoffs (supported email and WhatsApp actions, platform proposals) and hard owner-only operations are never
lifted by a grant."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from qorqut_agentic.errors import AgenticError, ConflictError
from qorqut_agentic.schemas import validate_instance

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import (PAGE, TEXT, PlanBuilder, apply_records, page, page_position, prepare_job, ref,
                                                  require_revision, rows, schema)
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain, utc_now
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_operations.storage.repositories import decode_row

from . import steps, waits
from .events import ordubashy
from .guard import ACTION_SIGNOFF_OPERATIONS, forbidden, grant_id, subject_argument
from .records import (CAPABILITY, GRANT_SCHEMA, MAX_ARGUMENT_BYTES, MAX_GRANTS, MAX_SCRIPT_BYTES, PHASES_SCHEMA, PROGRAM,
                      READ_CAPABILITY, RESTRICTIONS_SCHEMA, SCRIPT_PACKAGE, SCRIPT_REF, SLUG, SOURCE, TERMINAL_JOB_STATES, bounded,
                      encoded, entry_id, generated_owner, namespace, normalize_grants, normalize_phases, public, reader_view,
                      script_ref_for, slugify)

EXPOSURE = frozenset({"mcp"})
ID = {"type": "string", "format": "uuid"}
SCOPE = {"scope": {"enum": ["business", "global"]}}
AUTOMATION = {**SCOPE, "automation_id": ID}
HISTORY_PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "cursor": PAGE["cursor"]}
# Capabilities an owner grant can never supply: agent/instruction/permission administration and this extension's own management.
UNGRANTABLE_CAPABILITIES = frozenset({"manage_ecosystem_agents", "coordinate_businesses", "manage_delegated_assignments", "publish_roles",
                                      "publish_plugins", "manage_shared_instructions", "manage_agentic_layer", CAPABILITY})
JSON_SCHEMA = {"type": "object", "description": "A JSON Schema (draft 2020-12) object."}
FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 200},
    "description": {"type": "string", "maxLength": 8000},
    "arguments": {"type": "object", "description": "Bounded input the script reads as run.input (frozen at each run's admission)."},
    "phases": PHASES_SCHEMA,
    "restrictions": RESTRICTIONS_SCHEMA}
SCRIPT_FIELDS = {
    "script_source": {"type": "string", "minLength": 1, "maxLength": MAX_SCRIPT_BYTES,
                      "description": "The complete Python source of this automation's own script entry."},
    "input_schema": JSON_SCHEMA, "output_schema": JSON_SCHEMA}

READS = {
    "automations_list": (schema({**SCOPE, "state": {"enum": ["active", "paused", "archived"]}, **PAGE}),
                         "List the automations of one scope with their state and latest run."),
    "automations_get": (schema(AUTOMATION, ("automation_id",)),
                        "Read one automation. The owner and managing agent see the full record, schedule, waiting questions and recent "
                        "runs; every other authorized reader sees the title, description, state, phases and recent published output."),
    "automations_history": (schema({**AUTOMATION, "view": {"enum": ["runs", "outputs", "steps"]}, "run_id": ID, **HISTORY_PAGE},
                                   ("automation_id",)),
                            "Page an automation's runs, its published outputs, or (manager only, with run_id) one run's durable steps."),
}
SCRIPT_READ = {"automations_script_get": (schema(AUTOMATION, ("automation_id",)),
                                          "Read this automation's script source and revisions (the managing agent only).")}
MUTATIONS = {
    "automations_create": (schema({**SCOPE, "slug": {"type": "string", "pattern": SLUG.pattern}, **FIELDS, **SCRIPT_FIELDS,
                                   "script_ref": {"type": "string", "pattern": SCRIPT_REF.pattern,
                                                  "description": "An existing Python tool entry of the scripting-and-automation plugin "
                                                                 "(give this or script_source)."}}, ("title",)),
                           "Create an automation bound to its Python script. The creator follows its output once."),
    "automations_update": (schema({**AUTOMATION, **FIELDS, "script_ref": {"type": "string", "pattern": SCRIPT_REF.pattern}},
                                  ("automation_id",)),
                           "Edit an automation's record. A run in flight keeps the script and input it was admitted with."),
    "automations_script_write": (schema({**AUTOMATION, **SCRIPT_FIELDS, "expected_code_sha256": {"type": "string", "minLength": 64,
                                                                                                  "maxLength": 64}},
                                        ("automation_id", "script_source")),
                                 "Write this automation's own script entry through Agentic authoring (replacing needs the current "
                                 "code sha256). A run already in flight keeps the version it was admitted with."),
    "automations_schedule_set": (schema({**AUTOMATION, "schedule": {"type": "string", "maxLength": 100},
                                         "timezone": {"type": "string", "maxLength": 100}, "status": {"enum": ["active", "paused"]},
                                         "why": TEXT}, ("automation_id", "schedule", "timezone")),
                                 "Create or change the schedule: an ordinary routine of the automation.run program bound to this "
                                 "automation in the same step."),
    "automations_pause": (schema({**AUTOMATION, "reason": TEXT}, ("automation_id", "reason")),
                          "Pause one automation and its schedule: no new run is admitted and an in-flight run's next call is refused."),
    "automations_resume": (schema(AUTOMATION, ("automation_id",)), "Resume a paused automation and its schedule."),
    "automations_archive": (schema({**AUTOMATION, "reason": TEXT}, ("automation_id",)),
                            "Archive an automation and pause its schedule (history is kept)."),
    "automations_run": (schema(AUTOMATION, ("automation_id",)),
                        "Queue one manual run, or return the existing unresolved run (an automation has at most one)."),
    "automations_run_cancel": (schema({**SCOPE, "run_id": ID}, ("run_id",)), "Request cancellation of one unresolved automation run."),
    "automations_grant_revoke": (schema({**AUTOMATION, "grant_id": {"type": "string", "minLength": 4, "maxLength": 40}, "run_id": ID},
                                        ("automation_id", "grant_id")),
                                 "Revoke a persistent grant, or with run_id a run-only grant; takes effect on the script's next call."),
    "automations_input_answer": (schema({**AUTOMATION, "signoff_id": ID, "proposal_revision": {"type": "integer", "minimum": 1},
                                         "payload_hash": {"type": "string", "minLength": 1},
                                         "expected_status_revision": {"type": "integer", "minimum": 1}, "summary": TEXT,
                                         "notes": {"type": "string", "maxLength": 8000},
                                         "selected_choices": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}},
                                        ("automation_id", "signoff_id", "proposal_revision", "payload_hash",
                                         "expected_status_revision")),
                                 "Answer a question this automation's script asked the managing agent; bound to the exact proposal "
                                 "revision, payload hash and status revision. Never answers an owner-only request."),
}
SIGNOFFS = {
    "automations_grant_add": (schema({**AUTOMATION, "grant": GRANT_SCHEMA}, ("automation_id", "grant")),
                              "Propose a persistent owner grant for this automation only: one operation, optionally the one Global "
                              "capability it declares and one resource. The owner approves the exact grant."),
    "automations_run_with_grants": (schema({**AUTOMATION, "run_id": ID, "run_grants": {"type": "array", "minItems": 1, "maxItems": 20,
                                                                                         "items": GRANT_SCHEMA}},
                                           ("automation_id", "run_id", "run_grants")),
                                    "Propose run-only grants for one already-waiting run; the owner approves the exact grants and they "
                                    "end with that run."),
}
READ_NAMES = frozenset(READS)


def catalog_entries() -> tuple[CatalogEntry, ...]:
    return (*(CatalogEntry(name, False, "read", EXPOSURE, False) for name in (*READS, *SCRIPT_READ)),
            *(CatalogEntry(name, False, "records", EXPOSURE, False) for name in MUTATIONS),
            *(CatalogEntry(name, True, "records", EXPOSURE, False) for name in SIGNOFFS))


def business_of(ctx, args):
    """The scope of this call: ``None`` for Global, otherwise the selected Project."""
    if args.get("scope") == "global":
        return None
    if not ctx.business_id:
        raise DomainError("validation_error", "A Project automation needs a selected Project; pass scope: global for a Global one")
    return ctx.business_id


def _agentic(exc: AgenticError) -> DomainError:
    if isinstance(exc, ConflictError) or exc.code in {"conflict", "revision_conflict", "stale_revision"}:
        return DomainError("revision_conflict", exc.message, details=exc.details)
    if exc.code in {"validation_failed", "invalid_request", "limit_exceeded", "unsafe_path", "protected_agentic_area"}:
        return DomainError("validation_error", exc.message, details=exc.details)
    return DomainError("script_unavailable", exc.message, details=exc.details)


class AutomationOperations:
    def __init__(self, runtime, extension):
        self.runtime, self.ext = runtime, extension
        self.routines = RoutineOperations(runtime.programs, None)

    # -- registration --------------------------------------------------------------------------------------------
    def register(self, registry):
        common = dict(scope_kinds={"business", "global"}, resolve_namespace=namespace, global_capability=CAPABILITY)
        for name, (arguments, text) in READS.items():
            registry.register(OperationDescriptor(name, arguments, effect_kind="read", supports_signoff=False, exposure=EXPOSURE,
                                                  default_mode="allow", description=text,
                                                  global_read_capability_alternatives=frozenset({READ_CAPABILITY}),
                                                  read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow), **common))
        for name, (arguments, text) in SCRIPT_READ.items():
            registry.register(OperationDescriptor(name, arguments, effect_kind="read", supports_signoff=False, exposure=EXPOSURE,
                                                  default_mode="allow", description=text,
                                                  read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow), **common))
        for name, (arguments, text) in MUTATIONS.items():
            registry.register(OperationDescriptor(name, arguments, effect_kind="records", supports_signoff=False, exposure=EXPOSURE,
                                                  default_mode="allow", description=text,
                                                  prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                                                  apply=self.apply, **common))
        for name, (arguments, text) in SIGNOFFS.items():
            registry.register(OperationDescriptor(name, arguments, effect_kind="records", supports_signoff=True, exposure=EXPOSURE,
                                                  default_mode="signoff", description=text,
                                                  prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                                                  apply=self.apply, validate_targets=self.validate_targets, **common))

    # -- access --------------------------------------------------------------------------------------------------
    @staticmethod
    def is_manager(ctx, uow) -> bool:
        """The configured Ordubashy holding the explicit capability, acting directly (never from inside a script run)."""
        return (ctx.principal_kind == "agent" and ctx.automation_run_id is None and CAPABILITY in ctx.global_capabilities
                and ctx.principal_id == ordubashy(uow))

    def require_manager(self, ctx, uow):
        if not self.is_manager(ctx, uow):
            raise DomainError("forbidden", "Automations are managed by the configured Ordubashy with the explicit automation grant")

    def full_view(self, ctx, uow) -> bool:
        if ctx.automation_run_id is not None:
            return False
        if ctx.principal_kind == "human":
            installation = uow.repository("installation").get("service")
            return bool(installation and installation["owner_principal_id"] == ctx.principal_id)
        return self.is_manager(ctx, uow)

    @property
    def layer(self):
        layer = getattr(self.runtime, "agentic_layer", None)
        if layer is None:
            raise DomainError("operation_unavailable", "The Agentic layer is not installed on this host")
        return layer

    @staticmethod
    def row(automation_id, business, uow):
        row = uow.repository("automations", business_id=business).get(automation_id)
        if row is None:
            raise DomainError("not_found", "Automation does not exist")
        return row

    @staticmethod
    def builder(ctx, op, args, business, title):
        scoped = replace(ctx, business_id=business, review_business_id=business) if business is None else ctx
        return PlanBuilder(scoped, op, args, title, namespace=namespace(ctx, args))

    # -- reads ---------------------------------------------------------------------------------------------------
    def read(self, op, ctx, args, uow):
        business = business_of(ctx, args)
        full = self.full_view(ctx, uow)
        if op == "automations_list":
            where, parameters = ("state=?", (args["state"],)) if "state" in args else ("", ())
            values = [self._summary(row, uow, full) for row in rows(uow, "automations", business, where=where, parameters=parameters)]
            items, cursor = page(values, args, business, op, key=lambda row: (row["updated_at"], row["id"]))
            return {"automations": items, "next_cursor": cursor}
        row = self.row(args["automation_id"], business, uow)
        if op == "automations_script_get":
            self.require_manager(ctx, uow)
            try:
                return plain({**self.layer.tools.read_code(row["script_ref"]), "script_ref": row["script_ref"]})
            except AgenticError as exc:
                raise _agentic(exc) from exc
        if op == "automations_history":
            return self._history(row, business, args, full, uow)
        runs = self._runs(uow, row["id"], 10 if full else 3, full)
        outputs = self._outputs(uow, row["id"], 10, None, with_data=False)
        if not full:
            return plain({**reader_view(row), "latest_run": runs[0] if runs else None, "outputs": outputs[0], "source": SOURCE})
        result = {**public(row), "runs": runs, "outputs": outputs[0], "source": SOURCE, "schedule": self._schedule(row, uow)}
        pending = self._pending_run(uow, row["id"])
        if pending is not None:
            waiting = steps.waiting_for_run(uow, pending["id"])
            result["waiting"] = [{"step_key": step["step_key"], "kind": step["kind"], "operation": step["operation"],
                                  "wait": (step["wait_json"] or {}).get("kind"), "since": step["updated_at"]} for step in waiting]
            result["questions"] = [question for question in (waits.pending_question(uow, step) for step in waiting) if question]
        return plain(result)

    def _pending_run(self, uow, automation_id):
        job = self.ext.guard.unresolved_run(uow, automation_id)
        return dict(job) if job else None

    def _summary(self, row, uow, full):
        runs = self._runs(uow, row["id"], 1, full)
        if not full:
            return {**reader_view(row), "latest_run": runs[0] if runs else None}
        return {**{key: row[key] for key in ("id", "business_id", "slug", "title", "state", "pause_reason", "script_ref", "routine_id",
                                              "revision", "updated_at")}, "latest_run": runs[0] if runs else None}

    @staticmethod
    def _runs(uow, automation_id, limit, full, after=None):
        clause, parameters = "", []
        if after is not None:
            clause, parameters = " AND (created_at, id) < (?, ?)", list(after)
        found = uow.connection.execute(
            "SELECT * FROM jobs WHERE kind=? AND json_extract(arguments_json,'$.automation_id')=?" + clause +
            " ORDER BY created_at DESC, id DESC LIMIT ?", (PROGRAM, automation_id, *parameters, limit))
        result = []
        for job in map(decode_row, found):
            item = {"run_id": job["id"], "state": job["state"], "created_at": job["created_at"], "updated_at": job["updated_at"]}
            if full:
                error = job["error_json"] or {}
                item.update(attempts=job["attempts"], error_code=error.get("code"), error_message=error.get("message"),
                            trigger=(job["definition_ref_json"] or {}).get("trigger", "manual"))
            result.append(item)
        return result

    @staticmethod
    def _outputs(uow, automation_id, limit, after, *, with_data):
        clause, parameters = "", []
        if after is not None:
            clause, parameters = " AND (created_at, id) < (?, ?)", list(after)
        found = uow.connection.execute(
            "SELECT * FROM automation_steps WHERE automation_id=? AND kind='publish' AND state='succeeded'" + clause +
            " ORDER BY created_at DESC, id DESC LIMIT ?", (automation_id, *parameters, limit + 1)).fetchall()
        values = []
        for row in map(steps._decode, found):
            result = row["result_json"] or {}
            item = {"id": row["id"], "run_id": row["run_id"], "step_key": row["step_key"], "created_at": row["created_at"],
                    "event_id": result.get("event_id"), "summary": result.get("summary")}
            if with_data:
                item["data"] = result.get("data")
            values.append(item)
        return values[:limit], values

    def _history(self, row, business, args, full, uow):
        view = args.get("view", "runs")
        args = {**args, "limit": args.get("limit", 20)}
        limit = args["limit"]
        _binding, after = page_position(args, business, "automations_history:" + view)
        if view == "steps":
            if not full:
                raise DomainError("forbidden", "Step history is available to the owner and the managing agent")
            if "run_id" not in args:
                raise DomainError("validation_error", "view steps needs a run_id")
            values = [self._step(step) for step in steps.page_for_run(uow, args["run_id"], after=after, limit=limit)
                      if step["automation_id"] == row["id"]]
            items, cursor = page(values, args, business, "automations_history:steps", key=lambda s: (s["created_at"], s["id"]), reverse=False)
            return plain({"steps": items, "next_cursor": cursor})
        if view == "outputs":
            _first, values = self._outputs(uow, row["id"], limit, after, with_data=True)
            items, cursor = page(values, args, business, "automations_history:outputs", key=lambda o: (o["created_at"], o["id"]))
            return plain({"outputs": items, "next_cursor": cursor})
        values = self._runs(uow, row["id"], limit + 1, full, after)
        items, cursor = page(values, args, business, "automations_history:runs", key=lambda r: (r["created_at"], r["run_id"]))
        return plain({"runs": items, "next_cursor": cursor})

    @staticmethod
    def _step(step):
        return {key: step[key] for key in ("id", "step_key", "attempt", "parent_step_id", "kind", "operation", "state", "effect",
                                           "wait_json", "result_json", "error_json", "log_expired_at", "created_at", "closed_at")}

    def _schedule(self, row, uow):
        if not row["routine_id"]:
            return None
        found = self._routine(row, uow)
        if found is None:
            return None
        return {"routine_id": found["id"], "schedule": found["schedule"], "timezone": found["timezone"], "status": found["status"],
                "revision": found["revision"]}

    @staticmethod
    def _routine(row, uow):
        business = row["business_id"]
        key = {"namespace": business or "global", "id": row["routine_id"]}     # a Project routine's namespace is the Project id
        found = uow.repository("routines", business_id=business).get(key)
        return found if found is not None and not found["metadata_json"].get("retired") else None

    # -- mutations -----------------------------------------------------------------------------------------------
    def prepare(self, op, ctx, args, options, uow):
        business = business_of(ctx, args)
        self.require_manager(ctx, uow)
        builder = self.builder(ctx, op, args, business, op.removeprefix("automations_").replace("_", " ").capitalize() + " automation")
        name = op.removeprefix("automations_")
        if name == "create":
            self._create(builder, ctx, args, business, uow)
        elif name == "run_cancel":
            self._cancel(builder, args, business, uow)
        elif name == "input_answer":
            self._answer(builder, ctx, args, business, uow)
        else:
            row = self.row(args["automation_id"], business, uow)
            if options.expected_revision is not None:
                require_revision(row, options.expected_revision)
            getattr(self, "_" + name)(builder, ctx, args, row, business, uow)
        return builder.build()

    # create / update / script ------------------------------------------------------------------------------------
    def _create(self, builder, ctx, args, business, uow):
        slug = self._unique_slug(args, business, uow)
        arguments = bounded(args.get("arguments", {}), MAX_ARGUMENT_BYTES, "Automation input")
        if ("script_source" in args) == ("script_ref" in args):
            raise DomainError("validation_error", "Give exactly one of script_source (a new script) or script_ref (an existing entry)")
        identity = new_id()
        if "script_source" in args:
            script_ref = script_ref_for(identity)       # a stable, record-unique entry: never shared with, or derived from, another record
            self._check_source(args["script_source"], args.get("input_schema", {"type": "object"}), args.get("output_schema", {"type": "object"}),
                               arguments)
        else:
            script_ref = args["script_ref"]
            self._check_script(script_ref, arguments)
            self._require_exclusive(script_ref, identity, uow)
        restrictions = self._check_restrictions(args.get("restrictions", {}), business)
        values = {"id": identity, "business_id": business, "slug": slug, "title": args["title"], "description": args.get("description", ""),
                  "state": "active", "script_ref": script_ref, "arguments_json": arguments,
                  "phases_json": normalize_phases(args.get("phases", [])), "routine_id": None, "grants_json": [],
                  "restrictions_json": restrictions, "saved_json": {}, "admitted_json": {}, "created_by_principal_id": ctx.principal_id}
        builder.record("automations", identity, None, values, label=args["title"])
        if "script_source" in args:
            builder.effects.append(f"Writes the Python script {script_ref} ({len(args['script_source'].encode('utf-8'))} bytes) through "
                                   "Agentic authoring.")
        builder.effects.append("The creator follows this automation's output events once; unsubscribing later is respected.")

    @staticmethod
    def _unique_slug(args, business, uow):
        slug = args.get("slug") or slugify(args["title"])
        taken = {row[0] for row in uow.connection.execute("SELECT slug FROM automations WHERE business_id IS ? AND slug LIKE ?",
                                                          (business, slug + "%"))}
        if slug in taken:
            if args.get("slug"):
                raise DomainError("conflict", "That automation slug is already used in this scope")
            suffix = 2
            while f"{slug}-{suffix}" in taken:
                suffix += 1
            slug = f"{slug}-{suffix}"
        return slug

    def _check_schema(self, value, what):
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise DomainError("validation_error", f"{what} is not a valid JSON Schema: {exc.message}") from exc
        bounded(value, 32768, what)

    def _check_source(self, source, input_schema, output_schema, arguments):
        if len(source.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise DomainError("validation_error", f"The script exceeds {MAX_SCRIPT_BYTES} bytes")
        self._check_schema(input_schema, "input_schema")
        self._check_schema(output_schema, "output_schema")
        problems = validate_instance(arguments, input_schema)
        if problems:
            raise DomainError("validation_error", "The automation input does not match the script's input schema",
                              details={"issues": problems[:5]})

    def _check_script(self, script_ref, arguments):
        """An existing script entry must be a Python tool of the owned plugin, and the input must fit its declared schema."""
        if not SCRIPT_REF.fullmatch(script_ref):
            raise DomainError("validation_error", f"script_ref must name a tool entry of the {SCRIPT_PACKAGE} plugin")
        try:
            described = self.layer.tools.describe(script_ref)
        except AgenticError as exc:
            raise DomainError("validation_error", f"The script {script_ref} is not available: {exc.message}") from exc
        if described["runtime"] != "python":
            raise DomainError("validation_error", "Automation scripts use the python runtime alias only")
        problems = validate_instance(arguments, described["input_schema"])
        if problems:
            raise DomainError("validation_error", "The automation input does not match the script's input schema",
                              details={"issues": problems[:5]})
        return described

    def _check_restrictions(self, restrictions, business):
        for key in ("allowed_operations", "denied_operations"):
            unknown = sorted(name for name in restrictions.get(key, ()) if not self.runtime.registry.known(name))
            if unknown:
                raise DomainError("validation_error", f"{key} names operations that are not installed: " + ", ".join(unknown[:10]))
        return plain(restrictions)

    def _update(self, builder, ctx, args, row, business, uow):
        if row["state"] == "archived":
            raise DomainError("invalid_transition", "An archived automation cannot be edited")
        values = {}
        for field, column in (("title", "title"), ("description", "description"), ("script_ref", "script_ref")):
            if field in args:
                values[column] = args[field]
        arguments = bounded(args["arguments"], MAX_ARGUMENT_BYTES, "Automation input") if "arguments" in args else plain(row["arguments_json"])
        if "arguments" in args:
            values["arguments_json"] = arguments
        if "phases" in args:
            values["phases_json"] = normalize_phases(args["phases"])
        if "restrictions" in args:
            values["restrictions_json"] = self._check_restrictions(args["restrictions"], business)
        if not values:
            raise DomainError("validation_error", "Nothing to update")
        if "script_ref" in args and args["script_ref"] != row["script_ref"]:
            self._require_idle(row, uow)
        if "script_ref" in args or "arguments" in args:
            self._check_script(args.get("script_ref", row["script_ref"]), arguments)
        if "script_ref" in args and args["script_ref"] != row["script_ref"]:
            self._require_exclusive(args["script_ref"], row["id"], uow)
        builder.record("automations", row["id"], row, values, label=row["title"])

    @staticmethod
    def _require_exclusive(script_ref, identity, uow):
        """An existing script entry may belong to one automation record only, in any scope and state. A generated ``auto-`` entry
        belongs to the record whose id it embeds and can never be adopted by another; any other entry must not already be used."""
        owner = generated_owner(script_ref)
        if owner is not None and owner != identity.replace("-", ""):
            raise DomainError("validation_error", "That script entry was generated for another automation and cannot be reused")
        taken = uow.connection.execute("SELECT slug FROM automations WHERE script_ref=? AND id<>? LIMIT 1", (script_ref, identity)).fetchone()
        if taken is not None:
            raise DomainError("conflict", f"That script entry already belongs to the automation {taken['slug']}; an entry has exactly "
                              "one owner, so give this automation its own script (script_source) instead")

    def _require_idle(self, row, uow):
        pending = self.ext.guard.unresolved_run(uow, row["id"])
        if pending is not None:
            raise DomainError("automation_run_pending", "This automation has an unresolved run; wait for it or cancel it first",
                              details={"job_id": pending["id"], "reason": pending["state"], "automation_id": row["id"]})

    def _script_write(self, builder, ctx, args, row, business, uow):
        if row["state"] == "archived":
            raise DomainError("invalid_transition", "An archived automation cannot be edited")
        if row["script_ref"] != script_ref_for(row["id"]):
            raise DomainError("validation_error", "Only the automation's own script entry can be written here; use update to point at "
                              "another entry")
        # An edit never waits for a run: a run that already froze its snapshot is immune, and a queued run keeps the exact version it
        # was admitted with (copied here, before the edit and outside every write transaction) while later runs use the edit.
        self.ext.program.retain(row["id"])
        input_schema = args.get("input_schema", {"type": "object"})
        self._check_source(args["script_source"], input_schema, args.get("output_schema", {"type": "object"}), plain(row["arguments_json"]))
        try:
            current = self.layer.tools.read_code(row["script_ref"])
        except AgenticError as exc:
            if exc.code != "not_found":
                raise _agentic(exc) from exc
            current = None
        if current is not None and args.get("expected_code_sha256") != current["code_sha256"]:
            raise DomainError("revision_conflict", "Replacing the script needs the current code sha256 (automations_script_get)",
                              details={"current_code_sha256": current["code_sha256"]})
        builder.record("automations", row["id"], row, {"saved_json": row["saved_json"]}, label=row["title"])
        builder.effects.append(f"Writes the Python script {row['script_ref']} ({len(args['script_source'].encode('utf-8'))} bytes) through "
                               "Agentic authoring; a run admitted afterwards uses it.")

    # schedule and lifecycle --------------------------------------------------------------------------------------
    def _schedule_set(self, builder, ctx, args, row, business, uow):
        if row["state"] == "archived":
            raise DomainError("invalid_transition", "An archived automation cannot be scheduled")
        status = args.get("status", "active") if row["state"] == "active" else "paused"
        definition = {"name": f"Automation {row['slug']}"[:80], "why": args.get("why", ""), "schedule": args["schedule"],
                      "timezone": args["timezone"], "status": status, "program": PROGRAM, "arguments": {"automation_id": row["id"]},
                      "target": "business" if business else "root"}
        scope = {"scope": "business" if business else "global"}
        existing = self._routine(row, uow) if row["routine_id"] else None
        if row["routine_id"] and existing is None:
            raise DomainError("conflict", "The bound routine is gone; the schedule must be repaired by the owner")
        if existing is not None:
            self.check_routine(existing, row)
            values = self.routines.definition_values(builder.ctx, {**scope, "definition": definition}, existing)
            builder.record("routines", {"namespace": existing["namespace"], "id": existing["id"]}, existing, values, label=definition["name"])
            return
        identity = new_id()
        ns = business or "global"
        values = {"namespace": ns, "id": identity, "business_id": business, "scope": "business" if business else "global",
                  **self.routines.definition_values(builder.ctx, {**scope, "definition": definition})}
        builder.record("routines", {"namespace": ns, "id": identity}, None, values, label=definition["name"])
        builder.record("automations", row["id"], row, {"routine_id": identity}, label=row["title"])

    @staticmethod
    def check_routine(routine, automation):
        """The bound routine must be exactly this automation's: the automation.run program, its automation_id and its scope."""
        if (routine["job_kind"] != PROGRAM or plain(routine["arguments_json"]) != {"automation_id": automation["id"]}
                or routine["business_id"] != automation["business_id"]):
            raise DomainError("conflict", "The bound routine is not this automation's schedule")

    def _lifecycle(self, builder, row, uow, values, routine_status):
        builder.record("automations", row["id"], row, values, label=row["title"])
        routine = self._routine(row, uow) if row["routine_id"] else None
        if routine is not None and routine["status"] != routine_status:
            self.check_routine(routine, row)
            builder.record("routines", {"namespace": routine["namespace"], "id": routine["id"]}, routine, {"status": routine_status},
                           label=routine["name"])

    def _pause(self, builder, ctx, args, row, business, uow):
        if row["state"] != "active":
            raise DomainError("invalid_transition", "Only an active automation can be paused")
        self._lifecycle(builder, row, uow, {"state": "paused", "pause_reason": args["reason"]}, "paused")

    def _resume(self, builder, ctx, args, row, business, uow):
        if row["state"] != "paused":
            raise DomainError("invalid_transition", "Only a paused automation can be resumed")
        self._lifecycle(builder, row, uow, {"state": "active", "pause_reason": None}, "active")

    def _archive(self, builder, ctx, args, row, business, uow):
        if row["state"] == "archived":
            raise DomainError("invalid_transition", "Already archived")
        self._lifecycle(builder, row, uow, {"state": "archived", "pause_reason": args.get("reason")}, "paused")

    # runs ---------------------------------------------------------------------------------------------------------
    def _run(self, builder, ctx, args, row, business, uow):
        """Queue the run, or - when one is unresolved - a no-change plan whose result names the existing run."""
        job_arguments = {"automation_id": row["id"], "trigger": "manual"}
        try:
            self.ext.guard.admit(builder.ctx, job_arguments, uow)
        except DomainError as exc:
            if exc.code != "automation_run_pending":
                raise
            builder.expect(ref("automations", row["id"], business), row)
            builder.policy_effect(ref("automations", row["id"], business), builder.operation, "An unresolved run already exists")
            return
        prepare_job(builder, uow, PROGRAM, job_arguments, programs=self.runtime.programs, definition_extra={"automation_id": row["id"]})

    @staticmethod
    def _cancel(builder, args, business, uow):
        job = uow.repository("jobs", business_id=business).get(args["run_id"], required=True)
        if job["kind"] != PROGRAM or job["state"] in TERMINAL_JOB_STATES or job["state"] == "cancel_requested":
            raise DomainError("revision_conflict", "This automation run is not cancellable")
        builder.record("jobs", job["id"], job, {"state": "cancel_requested", "next_retry_at": None}, business_id=business)
        builder.effects.append("No further script calls are admitted and the child is stopped. The run stays cancel_requested - and the "
                               "automation admits no new run - until every queued or in-flight external effect of it is settled from "
                               "its recorded Signoff, job and receipt; nothing already dispatched is undone.")

    # grants -------------------------------------------------------------------------------------------------------
    def validate_grant(self, grant, business, uow):
        """A grant is exact and typed: an installed operation, never a forbidden/owner-only/supported-action operation, optionally the
        one Global capability that operation declares (Global automations only) and one existing resource type."""
        registry = self.runtime.registry
        operation = grant["operation"]
        if not registry.known(operation):
            raise DomainError("validation_error", f"{operation} is not an installed operation")
        descriptor = registry.get(operation)
        if forbidden(operation) or descriptor.owner_only or operation in ACTION_SIGNOFF_OPERATIONS:
            raise DomainError("validation_error", f"{operation} can never be granted to an automation; it keeps its own approval")
        if ("business" if business else "global") not in descriptor.scope_kinds:
            raise DomainError("validation_error", f"{operation} is not available in this automation's scope")
        capability = grant.get("capability")
        if capability is not None:
            declared = {descriptor.global_capability, *descriptor.global_read_capability_alternatives} - {None}
            if business is not None:
                raise DomainError("validation_error", "A Project automation uses the Project's own authority; a Global capability grant "
                                  "is only for Global automations")
            if capability not in declared or capability in UNGRANTABLE_CAPABILITIES:
                raise DomainError("validation_error", f"{capability} is not a capability {operation} declares or it cannot be granted")
        if "resource_type" in grant and grant["resource_type"] not in uow.database.assembly.tables:
            raise DomainError("validation_error", f"{grant['resource_type']} is not a known resource type")
        if "resource_id" in grant and subject_argument(descriptor, grant["resource_type"]) is None:
            # The engine can tie a call to one record only when it names that record by a single required <type>_id argument
            # (a read is then resolved to that exact record before any data is returned; a mutation's whole prepared target set
            # must be exactly that record). Any other shape would be broader than the grant reads, so it is refused here.
            raise DomainError("validation_error", f"{operation} cannot be limited to one {grant['resource_type']} record: it does not "
                              "name a single such record by one required <type>_id argument. Grant the operation itself, or none.")
        return grant

    def _grant_add(self, builder, ctx, args, row, business, uow):
        if row["state"] == "archived":
            raise DomainError("invalid_transition", "An archived automation cannot be granted authority")
        grant = self.validate_grant(normalize_grants([args["grant"]])[0], business, uow)
        grants = [item for item in plain(row["grants_json"]) if grant_id(item) != grant_id(grant)]
        if len(grants) >= MAX_GRANTS:
            raise DomainError("validation_error", f"At most {MAX_GRANTS} grants are kept")
        builder.record("automations", row["id"], row, {"grants_json": [*grants, grant]}, label=row["title"])
        builder.effects.append("The owner is approving this exact persistent grant for this automation only: " + encoded(grant))

    def _grant_revoke(self, builder, ctx, args, row, business, uow):
        if "run_id" in args:
            found = next((step for step in steps.grant_steps(uow, args["run_id"])
                          if step["automation_id"] == row["id"] and grant_id(step["result_json"]["grant"]) == args["grant_id"]), None)
            if found is None:
                raise DomainError("not_found", "No such run-only grant")
            builder.record("automation_steps", found["id"], found, {"state": "cancelled", "closed_at": builder.at}, label=found["step_key"])
            return
        grants = plain(row["grants_json"])
        kept = [item for item in grants if grant_id(item) != args["grant_id"]]
        if len(kept) == len(grants):
            raise DomainError("not_found", "No such grant")
        builder.record("automations", row["id"], row, {"grants_json": kept}, label=row["title"])

    def _run_with_grants(self, builder, ctx, args, row, business, uow):
        run = self._waiting_run(row, args["run_id"], business, uow)
        grants = [self.validate_grant(grant, business, uow) for grant in normalize_grants(args["run_grants"])]
        builder.expect(ref("jobs", run["id"], business), run)
        for number, grant in enumerate(grants):
            identity = new_id()
            builder.record("automation_steps", identity, None, {
                "id": identity, "automation_id": row["id"], "business_id": business, "run_id": run["id"],
                "step_key": f"grant:{grant_id(grant)}", "attempt": 1, "kind": "grant", "operation": grant["operation"],
                "request_hash": canonical_hash(grant), "state": "running", "effect": "none", "result_json": {"grant": grant},
                "debug_json": {}}, label=f"Run-only grant {number + 1}")
        builder.effects.append("Run-only grants for this waiting run only, ending with it: " + "; ".join(
            grant["operation"] + (f" on {grant['resource_type']} {grant['resource_id']}" if "resource_id" in grant else "") +
            (f" with {grant['capability']}" if "capability" in grant else "") for grant in grants))

    @staticmethod
    def _waiting_run(row, run_id, business, uow):
        """The run a run-only grant may target: this automation's own, unresolved and already waiting on a decision or outcome."""
        run = uow.repository("jobs", business_id=business).get(run_id)
        if (run is None or run["kind"] != PROGRAM or run["arguments_json"].get("automation_id") != row["id"]
                or run["state"] in TERMINAL_JOB_STATES or run["state"] == "cancel_requested"):
            raise DomainError("not_found", "That is not an open run of this automation")
        if not steps.waiting_for_run(uow, run_id):
            raise DomainError("invalid_transition", "Run-only grants are only given to a run that is already waiting")
        return run

    # agent answers -----------------------------------------------------------------------------------------------
    def _answer(self, builder, ctx, args, business, uow):
        row = self.row(args["automation_id"], business, uow)
        step = self._question_step(row, args, uow)
        signoff = uow.repository("signoffs", business_id=business).get(args["signoff_id"], required=True)
        if (signoff["proposal_revision"] != args["proposal_revision"] or signoff["payload_hash"] != args["payload_hash"]
                or signoff["status_revision"] != args["expected_status_revision"]):
            raise DomainError("stale_signoff", "The question changed; read its current details first")
        builder.expect(ref("signoffs", signoff["id"], business), signoff)
        builder.effects.append("Answers the automation's question for the managing agent; this never answers an owner-only request.")
        return step

    @staticmethod
    def _question_step(row, args, uow):
        """The waiting question step this answer is for: this automation's, asked of the managing agent, bound to this exact proposal."""
        found = uow.connection.execute(
            "SELECT * FROM automation_steps WHERE automation_id=? AND state='waiting' AND json_extract(wait_json,'$.kind')='input' "
            "AND json_extract(wait_json,'$.signoff_id')=?", (row["id"], args["signoff_id"])).fetchone()
        step = steps._decode(found)
        wait = (step or {}).get("wait_json") or {}
        if (step is None or wait.get("audience") != "agent" or wait.get("proposal_revision") != args["proposal_revision"]
                or wait.get("payload_hash") != args["payload_hash"]):
            raise DomainError("forbidden", "No waiting question of this automation is addressed to the managing agent with that binding")
        return step

    def validate_targets(self, ctx, plan, uow):
        """At approval and at delayed application: the proposer is still the configured Ordubashy and the target is still live."""
        self.require_manager(ctx, uow)
        args = plain(plan.arguments)
        business = business_of(ctx, args)
        row = self.row(args["automation_id"], business, uow)
        if row["state"] == "archived":
            raise DomainError("invalid_plan", "The automation was archived after this grant was proposed")
        grants = [args["grant"]] if "grant" in args else args.get("run_grants", [])
        for grant in grants:
            self.validate_grant(grant, business, uow)
        if "run_id" in args:
            self._waiting_run(row, args["run_id"], business, uow)

    # -- apply ---------------------------------------------------------------------------------------------------
    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        op = plan.operation_name
        self.require_manager(ctx, uow)                  # management authority and scope are checked again at application
        business = business_of(ctx, args)
        scoped = replace(ctx, business_id=business, review_business_id=business) if business is None else ctx
        if op == "automations_input_answer":
            return self._apply_answer(scoped, plan, args, business, uow)
        if op == "automations_run":
            return self._apply_run(scoped, plan, args, business, uow)
        if op == "automations_run_with_grants":
            row = self.row(args["automation_id"], business, uow)
            self._waiting_run(row, args["run_id"], business, uow)       # still waiting at the moment the owner's approval applies
        if op in ("automations_create", "automations_script_write"):
            self._author(scoped, plan, op, args, business, uow)
        result = apply_records(scoped, plan, uow)
        if op == "automations_create":
            row = next(item for item in result if isinstance(item, dict) and "slug" in item and "script_ref" in item)
            # Default-follow happens exactly once, on creation; a later explicit unsubscribe is never reversed.
            uow.connection.execute(
                "INSERT OR IGNORE INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) "
                "VALUES(?,?,?,?,?,0,?)", (new_id(), ctx.principal_id, business, SOURCE, row["id"], utc_now()))
            return {"automation_id": row["id"], "slug": row["slug"], "script_ref": row["script_ref"]}
        if op == "automations_run_with_grants":
            self._bind_grants(plan, result, uow)
            return {"run_id": args["run_id"], "grants": len(args["run_grants"])}
        if op == "automations_script_write":
            return self._script_result(args, business, uow)
        return {"automation_id": args.get("automation_id"), "run_id": args.get("run_id")}

    def _apply_run(self, ctx, plan, args, business, uow):
        job_args = next((plain(c.values.get("arguments_json")) for c in plan.changes if c.resource.resource_type == "jobs"), None)
        if job_args is None:
            existing = self.ext.guard.unresolved_run(uow, args["automation_id"])
            return {"run_id": existing["id"] if existing else None, "already_running": True}
        try:
            self.ext.guard.admit(ctx, job_args, uow)     # the atomic check: same write transaction as the job insert below
        except DomainError as exc:
            if exc.code != "automation_run_pending":
                raise
            return {"run_id": exc.details["job_id"], "already_running": True}
        result = apply_records(ctx, plan, uow)
        job = next((item for item in result if isinstance(item, dict) and item.get("kind") == PROGRAM), None)
        return {"run_id": job["id"] if job else None, "already_running": False}

    def _apply_answer(self, ctx, plan, args, business, uow):
        row = self.row(args["automation_id"], business, uow)
        self._question_step(row, args, uow)
        shim = SimpleNamespace(arguments=args, target_namespace=plan.target_namespace, operation_name="signoffs_input_answer_delegated")
        outcome = self.runtime.application.signoffs.inputs.apply_delegated(ctx, shim, uow)
        data = getattr(outcome, "data", outcome)
        return {"automation_id": row["id"], "signoff": (data or {}).get("signoff")}

    def _bind_grants(self, plan, result, uow):
        """Bind each stored run-only grant to the approved Signoff that granted it (found by the plan's exact payload hash)."""
        signoff = uow.connection.execute(
            "SELECT id, proposal_revision FROM signoffs WHERE operation_name='automations_run_with_grants' AND payload_hash=? "
            "ORDER BY created_at DESC LIMIT 1", (plan.payload_hash,)).fetchone()
        for item in result:
            if isinstance(item, dict) and item.get("kind") == "grant":
                steps.settle(uow, item["id"], "running", result={**plain(item["result_json"]), "binding": {
                    "signoff_id": signoff["id"] if signoff else None, "proposal_revision": signoff["proposal_revision"] if signoff else None,
                    "payload_hash": plan.payload_hash, "granted_at": utc_now()}})

    # -- script authoring (existing Agentic authoring, attributed to the managing agent) --------------------------------
    def _author(self, ctx, plan, op, args, business, uow):
        if op == "automations_create":
            if "script_source" not in args:
                return
            created = next(plain(change.values) for change in plan.changes if change.resource.resource_type == "automations")
            script_ref, expected, label = created["script_ref"], None, created["slug"]
        else:
            row = self.row(args["automation_id"], business, uow)
            if row["script_ref"] != script_ref_for(row["id"]):
                raise DomainError("validation_error", "Only the automation's own script entry can be written here")
            script_ref, expected, label = row["script_ref"], args.get("expected_code_sha256"), row["slug"]
        layer = self.layer
        attribution = {"actor_id": ctx.principal_id, "actor_kind": "agent", "reason": f"Automation {label} script"}
        definition = {"schema_version": 1, "description": f"Automation script for {label}.", "runtime": "python",
                      "input_schema": args.get("input_schema", {"type": "object"}),
                      "output_schema": args.get("output_schema", {"type": "object"}),
                      "limits": {"timeout_seconds": self.ext.config.invocation_seconds, "max_output_bytes": 1024 * 1024,
                                 "max_stderr_bytes": 64 * 1024}}
        entry = {"id": entry_id(script_ref), "description": f"Python script of the {label} automation.",
                 "when_to_use": f"Only the {label} automation runs this script."}
        try:
            self.ensure_plugin(attribution)
            if self._authored(script_ref, args["script_source"], definition):
                return          # an earlier, interrupted apply of this very plan wrote it before its records committed
            if op != "automations_create":
                self._require_retained(row, uow)
            layer.authoring.write_tool(attribution, SCRIPT_PACKAGE, entry, definition, args["script_source"], expected_code_sha256=expected)
        except AgenticError as exc:
            raise _agentic(exc) from exc

    def _authored(self, script_ref, source, definition):
        """Whether the entry already holds exactly this code and these schemas. Authoring is written before the records commit, so an
        apply that was interrupted after it (or rolled back) is completed by its retry instead of conflicting with its own write:
        a new script's name embeds its record's fresh id, so only this plan can have written it."""
        try:
            current = self.layer.tools.read_code(script_ref)
        except AgenticError:
            return False
        return (current["code_sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
                and current["input_schema"] == definition["input_schema"] and current["output_schema"] == definition["output_schema"])

    def _require_retained(self, row, uow):
        """A queued run that has not frozen its snapshot must have its admitted version kept before the script is replaced (the
        preparation does that outside the transaction); otherwise the edit asks to be retried instead of breaking the run."""
        for run in self.ext.guard.unstaged_runs(uow, row["id"], 1):
            if not self.ext.program.retained_ready(run["id"], row["admitted_json"]):
                raise DomainError("revision_conflict", "A queued run of this automation has not yet kept the script version it was "
                                  "admitted with; repeat the edit in a moment", retryable=True)

    def ensure_plugin(self, attribution):
        """Seed the bundled plugin package (manifest, skills, template) the first time a script is written. An existing package -
        including every authored script entry in it - is never overwritten."""
        from .extension import plugin_root
        try:
            self.layer.authoring.import_package(attribution, SCRIPT_PACKAGE, str(plugin_root()),
                                                provenance={"origin": "qorqut-automation", "kind": "bundled-plugin"})
        except ConflictError as exc:
            reasons = {item.get("reason") for item in exc.details.get("conflicts", ())}
            if "package_exists" not in reasons:
                raise

    def _script_result(self, args, business, uow):
        row = self.row(args["automation_id"], business, uow)
        try:
            current = self.layer.tools.describe(row["script_ref"])
        except AgenticError as exc:
            raise _agentic(exc) from exc
        return {"automation_id": row["id"], "script_ref": row["script_ref"], "definition_revision": current["definition_revision"],
                "code_revision": current["code_revision"]}
