"""Fixed program registration and the retained background service assembly.

The generic machinery (program registry, authority, queue job service, routine scheduler and operations) is the
Operations engine's. This module registers the host's concrete programs on the one ``ProgramRegistry``, installs the
routine/learning/job operations and attaches the one ``BackgroundService`` with its ``JobObserver`` and scheduler.
The program registry is frozen with the operation registry by ``Runtime.freeze_registry`` before traffic."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut.signoffs.privacy import PRIVATE_JOB_KINDS
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import BoundedWriter
from qorqut.composition.catalog import install_contribution
from qorqut_operations.application.plans import UUID, schema
from qorqut.domains.learnings.operations import EXTRACT_ARGUMENTS, LearningOperations
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_context.integration.filing.programs import FilingPrograms
from .learnings import LearningProgram
from .models import ModelCalls
from qorqut_operations.background.programs import Program
from .repository import repository_status
from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.service import BackgroundService


def register_programs(programs, *, models=None, transcript_source=None,
                      maintenance=None, tables=None,
                      backup_downloader=None):
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
        if row["kind"] in PRIVATE_JOB_KINDS:
            # The frozen recipient and text live only in the job and its Signoff. The installation owner reads the job;
            # every agent (the proposer included) gets status and error codes only.
            installation = uow.repository("installation").get("service")
            if ctx.principal_kind == "human" and installation and installation["owner_principal_id"] == ctx.principal_id:
                return {"job": row, "details_available": True}
            if ctx.principal_kind != "agent":
                raise DomainError("forbidden", "Private job details require the installation owner")
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
        read=job_get, description=instruction_text('integration-inspect-a-scoped-background-job-the-initiating-agent')))

    runtime.background_operations = (routines, learnings)
    return routines, learnings


def attach_background(runtime, programs, *, model_client_factory=None, transcript_source=None,
                      clock=None, maintenance=None, backup_downloader=None):
    if runtime.background is not None:
        raise ValueError("Background service is already attached")
    if programs is not runtime.programs:
        raise ValueError("Background attachment uses the runtime's one program registry")
    register_programs(programs, models=ModelCalls(model_client_factory),
        transcript_source=transcript_source,
        maintenance=maintenance, tables=runtime.domains.tables,
        backup_downloader=backup_downloader)
    authority = runtime.authority
    if getattr(runtime.domains, "business_creation", None):
        runtime.domains.business_creation.worker_authority = authority
    runtime.policy.worker_profiles = authority.worker_profiles
    runtime.policy.job_consequence_verifier = authority.verify_consequence
    runtime.policy.job_plan_validator = authority.validate_plan
    # The bounded, model-free Global scheduled-extraction program: it records one due occurrence per scheduled job and
    # never starts, resumes or consults a native conversation (P07/A018).
    from qorqut.overseer.due import register_program as register_overseer_due
    register_overseer_due(runtime, programs)
    routines, learnings = register_operations(runtime, programs, authority, clock=clock)
    # Each selected provider declares its own routing. Absent extensions create no provider queues.
    # Capture this trusted configuration once; callers cannot reroute queued work.
    queues = {}
    for program in programs:
        if program.background_queue != "general":
            queues.setdefault(program.background_queue, []).append(program.id)
    # The host's incident reporter observes every job result after its own commit; it cannot change the outcome.
    background = BackgroundService(runtime.application, programs, authority, locks=runtime.locks,
                                   job_observer=runtime.application.incident_reporter,
                                   scheduler=RoutineScheduler(routines, runtime.database), clock=clock,
                                   queues=queues)
    background.routines, background.learnings = routines, learnings

    runtime.background = background
    # Recovery uses the service's real bounded stop/join, rather than inferring
    # durability from a job row or a process label.
    if runtime.recovery_quiescer is not None:
        runtime.recovery_quiescer.background = BoundedWriter(background, "background", running=lambda x: x.is_running())
    return background
