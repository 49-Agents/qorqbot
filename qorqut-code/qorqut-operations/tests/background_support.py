"""A synthetic host for the extracted registered-program machinery: fake programs, the real engine parts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qorqut_operations import background
from qorqut_operations.application import Application, CatalogContribution, CatalogEntry, OperationDescriptor, compose_catalog
from qorqut_operations.background import (BackgroundAuthority, BackgroundService, Program, ProgramPending,
                                          ProgramRegistry, ProgramResult, RoutineScheduler)
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.routines import RoutineOperations
from qorqut_operations.storage import LockPaths
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.repositories import decode_row
from signoff_support import INSTALLATION, item_prepare, record_apply
from support import BUSINESS, HUMAN, Demo, database_at

UTC = timezone.utc
LABEL_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}, "count": {"type": "integer"}},
                "required": ["label"], "additionalProperties": False}
REPORT_PERMISSIONS = {"items_create": "allow"}


def output(label, operation="items_create"):
    return {"operation": operation, "arguments": {"id": "report-" + label, "title": "Report " + label}}


class Programs:
    """Concrete synthetic programs. ``calls`` records every body execution for exact-once assertions."""

    def __init__(self):
        self.calls, self.pending, self.outputs = [], {}, {}

    def report(self, control, arguments):
        self.calls.append(("report", arguments["label"]))
        return ProgramResult(outputs=(output(arguments["label"]),), data={"label": arguments["label"]})

    def slow(self, control, arguments):
        self.calls.append(("slow", arguments["label"]))
        if self.pending.get(arguments["label"], 0) > 0:
            self.pending[arguments["label"]] -= 1
            return ProgramPending(retry_after=120)
        return ProgramResult(outputs=(output(arguments["label"]),))

    def many(self, control, arguments):
        self.calls.append(("many", arguments["label"]))
        return ProgramResult(outputs=(output(arguments["label"] + "-a"), output(arguments["label"] + "-b")))

    def rogue(self, control, arguments):
        self.calls.append(("rogue", arguments["label"]))
        return ProgramResult(outputs=(output(arguments["label"], "items_other"),))

    def raises(self, control, arguments):
        """Raise what ``label`` names: an unexpected fault, or a ``DomainError`` carrying that code."""
        self.calls.append(("raises", arguments["label"]))
        if arguments["label"] == "boom":
            raise RuntimeError("synthetic program fault")
        raise DomainError(arguments["label"], "synthetic " + arguments["label"], retryable=arguments["label"] == "busy")

    def incomplete(self, control, arguments):
        self.calls.append(("incomplete", arguments["label"]))
        return ProgramResult(completion_error={"code": "synthetic_incomplete", "message": "Synthetic completion error"})

    def registered(self):
        spec = dict(input_schema=LABEL_SCHEMA, permissions=REPORT_PERMISSIONS, outputs=("items_create",),
                    resource_types=("items",), scope_kinds=("business",))
        return (
            Program("demo.report", 1, run=self.report, **spec),
            Program("demo.slow", 1, run=self.slow, **spec),
            Program("demo.many", 1, run=self.many, **spec),
            Program("demo.rogue", 1, run=self.rogue, **spec),
            # Declared outputs, but no resource kinds: the output exceeds its declaration.
            Program("demo.overreach", 1, run=self.report, **{**spec, "resource_types": ()}),
            # Declared output operation without a worker grant for it.
            Program("demo.ungranted", 1, run=self.report, **{**spec, "permissions": {}}),
            # Queued only by an approved domain operation, never schedulable.
            Program("demo.direct", 1, run=self.report, **{**spec, "routine_allowed": False}),
            Program("demo.raises", 1, run=self.raises, **spec),
            Program("demo.incomplete", 1, run=self.incomplete, **spec),
            Program("demo.offline", 1, input_schema=LABEL_SCHEMA, run=None, unavailable_reason="Provider is not installed"),
        )


class BackgroundHost:
    def __init__(self, tmp_path, *, now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC)):
        self.now = now
        self.programs_impl = Programs()
        self.database = database_at(tmp_path, demo=Demo(), extra=(background.storage_contribution(),))
        self.locks = LockPaths.under(tmp_path / "locks")
        self.programs = ProgramRegistry()
        for program in self.programs_impl.registered():
            self.programs.register(program)
        self.authority = BackgroundAuthority(self.database, self.programs)
        self.programs.freeze()
        self.operations = RoutineOperations(self.programs, self.authority, clock=lambda: self.now)
        entries = (CatalogEntry("items_create", True, "records"), CatalogEntry("items_other", True, "records"))
        descriptors = (OperationDescriptor("items_create", {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}}}, prepare=item_prepare, apply=record_apply),
            OperationDescriptor("items_other", {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "string"}, "title": {"type": "string"}}}, prepare=item_prepare, apply=record_apply))
        self.catalog = compose_catalog([CatalogContribution("demo", entries, descriptors),
                                        self.operations.contribution()], include_batch=False)
        self.policy = PolicyResolver(self.catalog.registry, None, rules=PolicyRules(),
            worker_profiles=self.authority.worker_profiles, job_consequence_verifier=self.authority.verify_consequence,
            job_plan_validator=self.authority.validate_plan)
        self.admission = RecoveryAdmission()
        self.app = Application(self.database, self.catalog.registry, policy=self.policy, writer_admission=self.admission)
        self.scheduler = RoutineScheduler(self.operations, self.database)
        self.service = BackgroundService(self.app, self.programs, self.authority, locks=self.locks,
                                         scheduler=self.scheduler, clock=lambda: self.now)
        with self.database.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION,
                                                   "owner_principal_id": HUMAN})
            self.workers = self.authority.register_workers(uow, [BUSINESS])

    @staticmethod
    def owner(business=BUSINESS, **kwargs):
        return InvocationContext(Principal(HUMAN, "human"), business_id=business,
                                 allowed_business_ids=frozenset({business}), source="dashboard", **kwargs)

    def invoke(self, operation, arguments, ctx=None, key=None, **options):
        return self.app.invoke(ctx or self.owner(), operation, arguments, {"idempotency_key": key or new_id(), **options})

    def routine(self, program="demo.report", *, name="Daily", schedule="30 1 * * *", tz="America/New_York",
                status="active", label="daily", **extra):
        definition = {"name": name, "why": "synthetic", "schedule": schedule, "timezone": tz, "status": status,
                      "program": program, "arguments": {"label": label}, "target": "business", **extra}
        result = self.invoke("routines_create", {"definition": definition})
        assert result.status == "ok", result.error
        return next(row for row in result.data["records"] if "job_kind" in row)

    def run_manual(self, routine):
        result = self.invoke("routines_run", {"routine_id": routine["id"]}, expected_revision=routine["revision"])
        assert result.status == "ok", result.error
        return result.data["job_ids"][0]

    def jobs(self):
        with self.database.read() as uow:
            return [decode_row(row) for row in uow.connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid")]

    def job(self, job_id):
        with self.database.read() as uow:
            return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def items(self):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute("SELECT * FROM items ORDER BY id")]

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now
