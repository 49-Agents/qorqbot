"""Fixed program registration and the retained background service assembly.

The generic machinery (program registry, authority, serial job service, routine scheduler and operations) is the
Operations engine's. This module registers the host's concrete programs on the one ``ProgramRegistry``, installs the
routine/learning/job operations and attaches the one ``BackgroundService`` with its ``JobObserver`` and scheduler.
The program registry is frozen with the operation registry by ``Runtime.freeze_registry`` before traffic."""

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import BoundedWriter
from bazarlyk.composition.catalog import install_contribution
from qorqut_operations.application.plans import UUID, schema
from bazarlyk.domains.learnings.operations import EXTRACT_ARGUMENTS, LearningOperations
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_context.integration.filing.programs import FilingPrograms
from .whatsapp_send import WhatsAppSendProgram
from .learnings import LearningProgram
from .models import ModelCalls
from qorqut_operations.background.programs import Program
from .repository import repository_status
from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.service import BackgroundService


def register_programs(programs, *, models=None, transcript_source=None,
                      maintenance=None, tables=None,
                      backup_downloader=None, whatsapp_send=None):
    models = models or ModelCalls(None)
    learning = LearningProgram(models, transcript_source)

    def add(identity, arguments, run, outputs=(), resources=(), permissions=(), **kwargs):
        programs.register(Program(identity, 1, arguments, run,
            {name: "allow" for name in (*outputs, *permissions)}, outputs, resources, **kwargs))

    # The final Context filing programs: completion-only, no model call and no document read of their own.
    FilingPrograms().register(programs)
    # One Global learning stream (v1.2 G022): the learning programs are Global only, so no business routine, job or worker
    # membership is created for them and historical business learning is left as it is.
    add("learnings.extract", EXTRACT_ARGUMENTS, learning.run, ("learnings_record_batch",),
        ("learning_candidates", "learning_decisions", "learning_scan_state", "learning_runs", "learning_history"), timeout_seconds=7200,
        scope_kinds=("global",), global_capabilities=("read_global_context",))
    add("learnings.extract_global", EXTRACT_ARGUMENTS, learning.run, ("learnings_record_batch",),
        ("learning_candidates", "learning_decisions", "learning_scan_state", "learning_runs", "learning_history"), timeout_seconds=7200,
        scope_kinds=("global",), global_capabilities=("read_global_context",))
    # The standalone procedural engine program is not registered at all in this target: its old name is an ordinary
    # unknown program, its historical jobs stay readable, and nothing can run it or import its issue packages.
    from .backups import ARGUMENTS as BACKUP_ARGUMENTS, BackupPullProgram
    pull = BackupPullProgram(backup_downloader).run
    add("backups.pull", BACKUP_ARGUMENTS, pull, timeout_seconds=900, external=True, recover=pull)
    add("repository.status", schema({}), repository_status)
    if tables is not None:
        # Business jobs keep this registration byte-for-byte: queued and running jobs carry its frozen digest.
        add("tables.version", schema({"version_id": UUID}, ("version_id",)), tables.history.run,
            external=True, recover=tables.history.run)
        # Global table history has its own internal program (same runner and recovery), prepared only by the table operations.
        add("tables.global-version", schema({"version_id": UUID}, ("version_id",)), tables.history.run,
            external=True, recover=tables.history.run, scope_kinds=("global",), global_capabilities=("read_global_context",))
    # Owner-approved personal WhatsApp sends. Queued only by the whatsapp_send operation's approved Signoff; the program
    # refuses any job it cannot trace to that Signoff, and a routine can never name it.
    whatsapp = WhatsAppSendProgram(whatsapp_send)
    add("whatsapp.send", schema({"scope": {"const": "global"}, "account_ref": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "chat_id": {"type": "string", "minLength": 3, "maxLength": 160}, "text": {"type": "string", "minLength": 1, "maxLength": 16384}},
        ("scope", "account_ref", "chat_id", "text")), whatsapp.run, (), (), (), scope_kinds=("global",),
        external=True, recover=whatsapp.recover, timeout_seconds=120, routine_allowed=False)
    if maintenance is not None:
        add("maintenance.nightly", schema({}), maintenance.run, timeout_seconds=1800,
            scope_kinds=("global",), external=True, recover=maintenance.run)
    return programs


def register_operations(runtime, programs, authority, *, clock=None):
    """Install the complete public contract without starting background workers.

    Preparation and the HTTP service must resolve the same tool registry. Worker
    attachment supplies real program adapters later, without changing that registry.
    ``programs`` and ``authority`` are the runtime's one registry and the one authority bound to it.
    """
    if programs is not runtime.programs or authority is not runtime.authority:
        raise ValueError("Routine operations use the runtime's one program registry and background authority")
    if getattr(runtime, "background_operations", None) is not None:
        routines, learnings = runtime.background_operations
        if clock is not None:
            routines.clock = clock
        return routines, learnings
    routines = RoutineOperations(programs, authority, clock=clock)
    install_contribution(runtime.registry, routines.contribution())
    learnings = LearningOperations(runtime.application, programs, authority, documents=runtime.domains.documents).register()

    def job_get(ctx, args, options, uow):
        business_id = None if args.get("scope") == "global" else ctx.business_id
        row = uow.repository("jobs", business_id=business_id).get(args["job_id"], required=True)
        if row["kind"] == "whatsapp.send":
            # The frozen recipient and text live only in the job and its Signoff. The installation owner reads the job;
            # every agent (the proposer included) gets status and error codes only.
            installation = uow.repository("installation").get("service")
            if ctx.principal_kind == "human" and installation and installation["owner_principal_id"] == ctx.principal_id:
                return {"job": row, "details_available": True}
            if ctx.principal_kind != "agent":
                raise DomainError("forbidden", "Personal WhatsApp job details require the installation owner")
            public = {key: row[key] for key in ("id", "business_id", "kind", "state", "created_at", "updated_at", "attempts",
                                                "next_retry_at", "initiated_by_principal_id")}
            error = row.get("error_json")
            public["error_json"] = ({key: error[key] for key in ("code", "retryable") if key in error}
                                    if isinstance(error, dict) else None)
            return {"job": public, "details_available": False}
        # A job bound to a Context filing also reports that filing (ids, state, completion and source currency); the frozen
        # reason text stays with the initiator and the owner, other agents get the error code only.
        filing = runtime.domains.documents.job_filing(uow, ctx, row)
        extra = {} if filing is None else {"filing": filing}
        if ctx.principal_kind == "agent" and row["initiated_by_principal_id"] != ctx.principal_id:
            # Shared coordination needs status, not another agent's provider
            # payloads, private source text or frozen plugin configuration.
            fields = ("id", "business_id", "kind", "state", "created_at", "updated_at", "attempts",
                      "next_retry_at", "initiated_by_principal_id")
            public = {key: row[key] for key in fields}
            error = row.get("error_json")
            public["error_json"] = ({key: error[key] for key in ("code", "retryable") if key in error}
                if isinstance(error, dict) else None)
            return {"job": public, "details_available": False, **extra}
        return {"job": row, "details_available": True, **extra}

    runtime.registry.register(OperationDescriptor("jobs_get", schema({"job_id": UUID, "scope": {"enum": ["business", "global"]}}, ["job_id"]),
        effect_kind="read", supports_signoff=False, scope_kinds={"business", "global"},
        resolve_namespace=lambda c,a: "global" if a.get("scope") == "global" else "business:" + c.business_id,
        global_capability="manage_global_routines",
        read=job_get, description="Inspect a scoped background job. The initiating agent and owner can read its full result; "
            "other agents receive status and error codes for coordination without private payloads."))

    runtime.background_operations = (routines, learnings)
    return routines, learnings


def _before_tick(ticks):
    """One no-argument callable that runs every extension tick; one failing tick never skips the others, and the first error is
    re-raised afterwards so the service records it."""
    def run():
        first = None
        for tick in ticks:
            try:
                tick()
            except Exception as exc:  # noqa: BLE001 - recorded by the service after the remaining ticks ran
                first = first or exc
        if first is not None:
            raise first
    return run


def attach_background(runtime, programs, *, model_client_factory=None, transcript_source=None,
                      clock=None, maintenance=None, backup_downloader=None):
    if runtime.background is not None:
        raise ValueError("Background service is already attached")
    if programs is not runtime.programs:
        raise ValueError("Background attachment uses the runtime's one program registry")
    register_programs(programs, models=ModelCalls(model_client_factory),
        transcript_source=transcript_source,
        maintenance=maintenance, tables=runtime.domains.tables,
        backup_downloader=backup_downloader,
        whatsapp_send=getattr(runtime.application, "whatsapp_send", None))
    authority = runtime.authority
    if getattr(runtime.domains, "business_creation", None):
        runtime.domains.business_creation.worker_authority = authority
    runtime.policy.worker_profiles = authority.worker_profiles
    runtime.policy.job_consequence_verifier = authority.verify_consequence
    runtime.policy.job_plan_validator = authority.validate_plan
    # The bounded, model-free Global scheduled-extraction program: it records one due occurrence per scheduled job and
    # never starts, resumes or consults a native conversation (P07/A018).
    from bazarlyk.overseer.due import register_program as register_overseer_due
    register_overseer_due(runtime, programs)
    routines, learnings = register_operations(runtime, programs, authority, clock=clock)
    # The host's incident reporter observes every job result after its own commit; it cannot change the outcome.
    # Selected extensions reuse this one service's cadence: their bounded callables run before each tick (``before_tick``); an
    # installation with none passes nothing and is unchanged.
    selection = getattr(runtime, "selection", None)
    ticks = tuple(tick for extension in (selection.extensions if selection else ()) if (tick := extension.background_tick()) is not None)
    options = {"before_tick": _before_tick(ticks)} if ticks else {}
    background = BackgroundService(runtime.application, programs, authority, locks=runtime.locks,
                                   job_observer=runtime.application.incident_reporter,
                                   scheduler=RoutineScheduler(routines, runtime.database), clock=clock, **options)
    background.routines, background.learnings = routines, learnings

    runtime.background = background
    # Recovery uses the service's real bounded stop/join, rather than inferring
    # durability from a job row or a process label.
    if runtime.recovery_quiescer is not None:
        runtime.recovery_quiescer.background = BoundedWriter(background, "background")
    return background
