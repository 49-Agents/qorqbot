"""Fixed program registration and the retained background service assembly.

The generic machinery (program registry, authority, serial job service, routine scheduler and operations) is the
Operations engine's. This module registers the host's concrete programs on the one ``ProgramRegistry``, installs the
routine/learning/job operations and attaches the one ``BackgroundService`` with its ``JobObserver`` and scheduler.
The program registry is frozen with the operation registry by ``Runtime.freeze_registry`` before traffic."""

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import BoundedWriter
from bazarlyk.composition.catalog import install_contribution
from qorqut_operations.application.plans import IDENTITY, UUID, schema
from bazarlyk.domains.learnings.operations import EXTRACT_ARGUMENTS, LearningOperations
from qorqut_operations.routines.operations import RoutineOperations
from bazarlyk.reports.service import DailyReportOperations, DailyReportProgram
from .context import ContextPrograms
from .email import EmailProgram
from .account_email import AccountEmailProgram
from .metrics import MetricCollectionProgram
from .learnings import LearningProgram
from .models import ModelCalls
from qorqut_operations.background.programs import Program
from .repository import repository_status
from .platform_actions import PlatformPrograms
from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.service import BackgroundService

DOCUMENT_RESOURCES = ("documents", "managed_document_files", "document_index_sources", "agent_profiles", "principals", "business_memberships", "jobs")
FILING_RESOURCES = (*DOCUMENT_RESOURCES, "relationships", "stories", "action_logs", "action_log_labels")
DELIVERY_RESOURCES = (*FILING_RESOURCES, "tasks", "task_notes", "business_configs")


def _report_completed(runtime, row):
    """Wake the Telegram bridge after a report's artifacts and completed state are committed (no I/O, no write)."""
    from bazarlyk.telegram.reports import notify_completed
    notify_completed(runtime, row)


def register_programs(programs, *, models=None, context_reader=None, transcript_source=None,
                      platform_actions=None, file_applier=None, reddit_adapter=None, maintenance=None, email=None, tables=None, metric_collector=None,
                      context_base=None, documents=None, backup_downloader=None, stripe_revenue=None, daily_reports=None,
                      learning_engine=None):
    models = models or ModelCalls(None)
    context = ContextPrograms(models, context_reader, context_base=context_base)
    learning = LearningProgram(models, transcript_source, learning_engine)

    revision = {"type": "integer", "minimum": 1}

    def add(identity, arguments, run, outputs=(), resources=(), permissions=(), **kwargs):
        programs.register(Program(identity, 1, arguments, run,
            {name: "allow" for name in (*outputs, *permissions)}, outputs, resources, **kwargs))

    add("context.summarize", schema({"document_id": UUID, "expected_revision": revision}, ["document_id", "expected_revision"]),
        context.summarize, ("context_summary_apply",), DOCUMENT_RESOURCES,
        scope_kinds=("business", "global"), global_capabilities=("read_global_context",))
    add("context.map", schema({"upload_id": UUID, "expected_revision": revision, "title": {"type": "string", "maxLength": 2000}},
        ["upload_id", "expected_revision"]), context.map, ("context_map_apply",), DOCUMENT_RESOURCES,
        scope_kinds=("business", "global"), global_capabilities=("read_global_context",))
    add("context.file", schema({"upload_id": UUID, "expected_revision": revision}, ["upload_id", "expected_revision"]),
        context.file, ("context_filing_apply", "context_filing_fail"), FILING_RESOURCES,
        ("context_topics_create", "context_notes_create", "context_people_create", "context_people_update", "logs_add"),
        scope_kinds=("business", "global"), global_capabilities=("read_global_context",))
    if documents is not None:
        from .contextbase import ContextBasePrograms
        base = ContextBasePrograms(context_base, documents)
        add("context.base_file", schema({"upload_id": UUID, "expected_revision": revision, "retract": {"type": "boolean"},
                                         "force": {"type": "boolean"}}, ["upload_id"]), base.file,
            ("context_base_receipt",), DOCUMENT_RESOURCES, scope_kinds=("business", "global"), global_capabilities=("read_global_context",))
        add("context.base_instruct", schema({"text": {"type": "string", "minLength": 1, "maxLength": 20000},
            "people": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}, "maxItems": 20}}, ["text"]),
            base.instruct, (), (), (), scope_kinds=("business", "global"), global_capabilities=("read_global_context",), timeout_seconds=600)
    add("learnings.extract", EXTRACT_ARGUMENTS, learning.run, ("learnings_record_batch",),
        ("learning_candidates", "learning_decisions", "learning_scan_state", "learning_runs", "learning_history"), timeout_seconds=7200,
        scope_kinds=("business", "global"), global_capabilities=("read_global_context",))
    add("learnings.extract_global", EXTRACT_ARGUMENTS, learning.run, ("learnings_record_batch",),
        ("learning_candidates", "learning_decisions", "learning_scan_state", "learning_runs", "learning_history"), timeout_seconds=7200,
        scope_kinds=("global",), global_capabilities=("read_global_context",))
    from . import learning_engine as engine
    add(engine.PROGRAM, engine.ARGUMENTS, engine.EngineProgram(learning, learning_engine).run if learning_engine is not None else None,
        (engine.OPERATION,), engine.RESOURCES, timeout_seconds=7200, scope_kinds=("business", "global"),
        global_capabilities=("read_global_context",), unavailable_reason=None if learning_engine is not None else engine.UNAVAILABLE)
    from qorqut_operations.application.plans import DATE
    add("metrics.sync", schema({"source_id": IDENTITY, "date": DATE}, ("source_id",)), MetricCollectionProgram(metric_collector).run,
        ("metrics_collection_apply",), ("business_configs", "data_tables", "data_rows", "table_versions", "jobs"), timeout_seconds=120)
    from .backups import ARGUMENTS as BACKUP_ARGUMENTS, BackupPullProgram
    pull = BackupPullProgram(backup_downloader).run
    add("backups.pull", BACKUP_ARGUMENTS, pull, timeout_seconds=900, external=True, recover=pull)
    add("repository.status", schema({}), repository_status)
    from .stripe_revenue import PROGRAM as STRIPE_REVENUE, StripeRevenueProgram
    add(STRIPE_REVENUE, schema({}), (stripe_revenue or StripeRevenueProgram()).run, scope_kinds=("global",), timeout_seconds=300)
    if daily_reports is not None:
        programs.register(daily_reports.definition())
    if tables is not None:
        # Business jobs keep this registration byte-for-byte: queued and running jobs carry its frozen digest.
        add("tables.version", schema({"version_id": UUID}, ("version_id",)), tables.history.run,
            external=True, recover=tables.history.run)
        # Global table history has its own internal program (same runner and recovery), prepared only by the table operations.
        add("tables.global-version", schema({"version_id": UUID}, ("version_id",)), tables.history.run,
            external=True, recover=tables.history.run, scope_kinds=("global",), global_capabilities=("read_global_context",))
    add("email.sync", schema({}), EmailProgram(email).run, ("email_record_batch",),
        ("email_mailboxes", "email_conversations", "email_messages", "documents", "managed_document_files", "relationships", "signoffs"))
    from bazarlyk.domains.email.settings import UPDATE as EMAIL_UPDATE
    configure_email = EmailProgram(email).configure
    add("email.configure", EMAIL_UPDATE, configure_email, ("email_record_batch",),
        ("email_mailboxes", "email_conversations", "email_messages", "documents", "managed_document_files", "relationships", "signoffs"),
        external=True, recover=configure_email, timeout_seconds=120)
    account_email = AccountEmailProgram(email)
    add("email.account_mutation", schema({"scope": {"const": "global"}, "account_id": IDENTITY, "operation": {"enum": [
        "account_email_message_update", "account_email_message_delete", "account_email_folder_create", "account_email_folder_update",
        "account_email_folder_delete", "account_email_draft_create", "account_email_send", "account_email_reply", "account_email_forward",
        "account_email_vacation_set", "account_email_masked_email_set"]},
        "arguments": {"type": "object", "maxProperties": 30}}, ("scope", "account_id", "operation", "arguments")),
        account_email.run, (), (), (), scope_kinds=("global",), global_capabilities=("manage_fastmail",),
        external=True, recover=account_email.recover, timeout_seconds=120)
    from .scoped_email import ScopedEmailProgram
    scoped_email = ScopedEmailProgram(email)
    add("email.scoped_mutation", schema({"scope": {"enum": ["business", "global"]},
        "account_id": IDENTITY, "operation": {"type": "string"}, "arguments": {"type": "object"}},
        ("scope", "account_id", "operation", "arguments")), scoped_email.run, ("email_mutation_record",),
        ("email_messages", "email_conversations"), scope_kinds=("business", "global"),
        global_capabilities=("manage_fastmail",), external=True, recover=scoped_email.recover, timeout_seconds=120)
    from .owner_email import ARGUMENTS as OWNER_EMAIL, OPERATION as OWNER_RECORD, OwnerEmailProgram
    add("email.owner_sync", OWNER_EMAIL, OwnerEmailProgram(email).run, (OWNER_RECORD,),
        ("email_mailboxes", "email_conversations", "email_messages"), scope_kinds=("global",),
        global_capabilities=("manage_fastmail",), timeout_seconds=300)
    add("email.attachment", schema({"message_id": UUID, "attachment_id": IDENTITY}, ("message_id", "attachment_id")),
        EmailProgram(email).attachment, ("assets_upload",), ("documents", "managed_document_files"))
    add("learnings.apply_accepted", schema({}), None, unavailable_reason=
        "The preserved learn-apply routine references a program that was never implemented.", scope_kinds=("global", "business"))
    if platform_actions is not None:
        PlatformPrograms(platform_actions, file_applier, reddit_adapter).register(programs)
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
        private_account_job = (row["kind"] in {"email.account_mutation", "email.owner_sync"} or (row["kind"] == "email.scoped_mutation" and row["business_id"] is None)
                               or bool((row.get("definition_ref_json") or {}).get("account_email_operation")))
        private_access_revoked = False
        if private_account_job:
            # Account jobs freeze provider payloads in the job row. Revalidate
            # the current account grant and coordinator before exposing them.
            try:
                runtime.application.account_email._access(ctx, uow)
            except (DomainError, AttributeError):
                private_access_revoked = True
        if private_account_job and private_access_revoked and ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Private account job details require the current owner")
        if (ctx.principal_kind == "agent" and
                (private_access_revoked or (not private_account_job and
                                            row["initiated_by_principal_id"] != ctx.principal_id))):
            # Shared coordination needs status, not another agent's provider
            # payloads, private source text or frozen plugin configuration.
            fields = ("id", "business_id", "kind", "state", "created_at", "updated_at", "attempts",
                      "next_retry_at", "initiated_by_principal_id")
            public = {key: row[key] for key in fields}
            error = row.get("error_json")
            public["error_json"] = ({key: error[key] for key in ("code", "retryable") if key in error}
                if isinstance(error, dict) else None)
            return {"job": public, "details_available": False}
        return {"job": row, "details_available": True}

    from .owner_email import register_record
    register_record(runtime.registry)
    DailyReportOperations(programs, paths=lambda: runtime.paths).register(runtime.registry)
    runtime.registry.register(OperationDescriptor("jobs_get", schema({"job_id": UUID, "scope": {"enum": ["business", "global"]}}, ["job_id"]),
        effect_kind="read", supports_signoff=False, scope_kinds={"business", "global"},
        resolve_namespace=lambda c,a: "global" if a.get("scope") == "global" else "business:" + c.business_id,
        global_capability="manage_global_routines", read=job_get,
        description="Inspect a scoped background job. The initiating agent and owner can read its full result; "
            "other agents receive status and error codes for coordination without private payloads."))

    runtime.background_operations = (routines, learnings)
    return routines, learnings


def attach_background(runtime, programs, *, model_client_factory=None, transcript_source=None,
                      context_reader=None, reddit_adapter=None, clock=None, maintenance=None, metric_collector=None,
                      backup_downloader=None, stripe_revenue=None, daily_reports=None, learning_engine=None):
    if runtime.background is not None:
        raise ValueError("Background service is already attached")
    if programs is not runtime.programs:
        raise ValueError("Background attachment uses the runtime's one program registry")
    register_programs(programs, models=ModelCalls(model_client_factory),
        context_reader=context_reader or runtime.domains.documents.snapshot_for_job,
        transcript_source=transcript_source, platform_actions=runtime.domains.platform_actions,
        file_applier=runtime.file_applier, reddit_adapter=reddit_adapter,
        maintenance=maintenance, email=runtime.domains.email, tables=runtime.domains.tables, metric_collector=metric_collector,
        context_base=lambda: getattr(runtime, "context_base", None), documents=runtime.domains.documents,
        backup_downloader=backup_downloader, stripe_revenue=stripe_revenue,
        daily_reports=daily_reports or DailyReportProgram(paths=lambda: runtime.paths, on_completed=lambda row: _report_completed(runtime, row)),
        learning_engine=learning_engine)
    authority = runtime.authority
    if getattr(runtime.domains, "ideas", None):
        runtime.domains.ideas.conversion.worker_authority = authority
    runtime.policy.worker_profiles = authority.worker_profiles
    runtime.policy.job_consequence_verifier = authority.verify_consequence
    runtime.policy.job_plan_validator = authority.validate_plan
    routines, learnings = register_operations(runtime, programs, authority, clock=clock)
    # The host's incident reporter observes every job result after its own commit; it cannot change the outcome.
    background = BackgroundService(runtime.application, programs, authority, locks=runtime.locks,
                                   job_observer=runtime.application.incident_reporter,
                                   scheduler=RoutineScheduler(routines, runtime.database), clock=clock)
    background.routines, background.learnings = routines, learnings

    runtime.background = background
    # Recovery uses the service's real bounded stop/join, rather than inferring
    # durability from a job row or a process label.
    if runtime.recovery_quiescer is not None:
        runtime.recovery_quiescer.background = BoundedWriter(background, "background")
    return background
