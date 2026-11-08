"""Context filing workflows over registered source documents: uploads, note and journal snapshots, retry, retract, completion.

The Operations source service owns the source documents; the shared Context filing queue (``filing.ContextFiling``) owns what
is frozen and queued from them. This module is the one real adapter between the two:
it prepares the filing operations in the source service's plan builder, passing that one service and its store explicitly, so a
filed source is written exactly once, in one prepared plan, one unit of work and one receipt, under the original operation names
and versions. It also owns the automatic filing trigger of accepted learnings (``prepare_learning_intake``); ordinary document
and table edits never file."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import hashlib

from qorqut_context.integration import filing as context_filing
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.application.plans import DATE, TEXT, UUID, apply_records, ref, require_revision, schema
from qorqut_operations.contracts import DomainError, OperationResult, new_id
from qorqut_operations.documents.business_notes import KIND as BUSINESS_NOTE, TARGET
from qorqut_operations.documents.journal_access import JOURNAL_KINDS, require_owner
from qorqut_operations.documents.materials import ENCOUNTER_ORIGINS
from qorqut_operations.documents.note_snapshot import filing_snapshot, snapshot_hash
from qorqut_operations.documents.operations import GLOBAL_NOTE_OPS, GLOBAL_OWNER_CONTEXT, DocumentSources, append_plan
from qorqut_operations.documents.serialization import BODY, HASH, split_document
from qorqut_operations.records.associations import TASK_IDS
from qorqut_operations.team_channel import is_installation_owner

from .filing import DOCUMENT_KINDS as FILED_KINDS, RECEIPT_OPERATION

# Journal uploads are owner-only Global note operations beside the source service's own.
GLOBAL_NOTES = GLOBAL_NOTE_OPS | {"journal_upload_to_filer"}
PARTICIPANT = schema({"person_id": UUID, "name": TEXT}, ("name",))
PARTICIPANTS = {"type": "array", "items": PARTICIPANT, "maxItems": 20}
MUTATIONS = {
    "notes_upload": ({"document_id": UUID, "expected_content_hash": HASH, "task_ids": TASK_IDS, TARGET: UUID}, ("document_id",)),
    "journal_upload_to_filer": ({"document_id": UUID, "expected_content_hash": HASH}, ("document_id", "expected_content_hash")),
    "context_upload": ({"body": BODY, "title": TEXT, "source": TEXT, "task_ids": TASK_IDS,
        "origin": {"enum": list(ENCOUNTER_ORIGINS)}, "occurred_on": DATE, "participants": PARTICIPANTS}, ("body",)),
    "context_retry": ({"upload_id": UUID}, ("upload_id",)),
    "context_retract": ({"upload_id": UUID}, ("upload_id",)),
    # The one completion operation of a registered Context filing job. It takes only the stable receipt ID; the
    # filing, job, program, worker and scope are re-resolved from storage.
    RECEIPT_OPERATION: ({"receipt_id": UUID}, ("receipt_id",)),
}
FILING_DESCRIPTIONS = {
    RECEIPT_OPERATION: instruction_text('operations-apply-the-terminal-outcome-of-one-registered-context'),
    "context_retry": instruction_text('operations-retry-the-current-source-of-an-unfiled-upload'),
    "context_retract": instruction_text('operations-owner-only-queue-the-removal-of-the-exact'),
}
# The authority metadata of the filing operations (declared once, here; the host's registry takes it from ``registration``).
ENTRIES = (
    CatalogEntry("notes_upload", True, "records"),
    CatalogEntry("journal_upload_to_filer", True, "compound", frozenset({"dashboard"}), True),
    CatalogEntry("context_upload", True, "records"),
    CatalogEntry("context_retry", True, "records"),
    CatalogEntry("context_retract", True, "records", frozenset({"dashboard", "worker"}), True),
    CatalogEntry(RECEIPT_OPERATION, True, "records", frozenset({"worker"})),
)
WORKERS = {RECEIPT_OPERATION}
GLOBAL_CONTEXT = WORKERS | {"context_retry", "context_retract"}
GLOBAL_SCOPED = GLOBAL_CONTEXT | GLOBAL_OWNER_CONTEXT


def business_namespace(ctx, args):
    return 'business:' + ctx.business_id if ctx.business_id else 'global'


class FilingOperations:
    """``sources`` is the Operations source service; ``filing`` the host's ``ContextFiling`` or None when filing is not composed
    (the filing operations are then registered but unavailable, exactly as before)."""

    def __init__(self, sources, filing, *, global_upload_agent=None):
        self.sources, self.store, self.filing = sources, sources.store, filing
        # Optional trusted host resolver: return the current authorized agent ID from this transaction.
        # Without it Global uploads retain the owner-only boundary. Role names never confer this authority.
        self.global_upload_agent = global_upload_agent

    def require_global_upload(self, ctx, uow):
        if is_installation_owner(ctx, uow):
            return
        if ctx.principal_kind == "agent" and self.global_upload_agent is not None:
            identity = self.global_upload_agent(uow)
            if identity is not None and ctx.principal_id == identity:
                return
        raise DomainError("forbidden", "Only the owner or the authorized Global coordinator can upload Global context")

    def require_filing(self):
        if self.filing is None:
            raise DomainError("operation_unavailable", "Context filing is not composed on this installation")
        return self.filing

    def register(self, registry):
        for name, (properties, required) in MUTATIONS.items():
            registry.register(DocumentSources.mutation_descriptor(name, properties, required,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_targets,
                description=FILING_DESCRIPTIONS.get(name, ""),
                scope_kinds={"global"} if name == "journal_upload_to_filer" else {"business", "global"} if name in GLOBAL_SCOPED else {"business", "agent_workspace"},
                resolve_namespace=business_namespace if name in GLOBAL_SCOPED else DocumentSources.own_namespace if name.startswith("notes_") else None,
                global_capability="read_global_context" if name in GLOBAL_SCOPED or name in GLOBAL_NOTES else None,
                # Legacy authoring remains a dashboard/worker compatibility surface only.
                # Agent and owner-session MCP clients use canonical context_edit/instruct/upload.
                exposure={"worker"} if name in WORKERS else {"dashboard"} if name == "journal_upload_to_filer" else {"dashboard", "worker"} if name == "context_retract" else {"mcp", "dashboard", "worker"},
                owner_only=name in {"context_retract", "journal_upload_to_filer"}))
        return self

    def _upload_row(self, ctx, identity, uow):
        row = self.store.get(ctx, identity, uow)
        if row["kind"] not in FILED_KINDS:
            raise DomainError("validation_error", "Only context uploads are filed into the shared Context")
        return row

    def _encounter(self, builder, uow, args):
        """Validate call or meeting details and pin each picked Person to this plan."""
        ctx, origin = builder.ctx, args.get("origin")
        if origin is None:
            if "participants" in args or "occurred_on" in args:
                raise DomainError("validation_error", "Participants and dates apply to call or meeting uploads")
            return {}
        participants, identities, names = [], set(), set()
        for item in args.get("participants", []):
            name, identity = item["name"].strip(), item.get("person_id")
            if not name:
                raise DomainError("validation_error", "Participant name cannot be blank")
            if name.casefold() in names:
                raise DomainError("validation_error", "Participant names must be distinct")
            names.add(name.casefold())
            if identity:
                if identity in identities:
                    raise DomainError("validation_error", "Participant Persons must be distinct")
                identities.add(identity)
                row = self.store.get(ctx, identity, uow, kinds={"context_person"})
                if row["business_id"] != ctx.business_id:
                    raise DomainError("not_found", "Participant is not a Person in this project")
                if row.get("archived"):
                    raise DomainError("validation_error", "Participant is archived; restore them or choose another Person")
                builder.expect(ref("documents", identity, ctx.business_id), row)
            # The owner's chosen display name is kept; it is never replaced by the Person's stored name.
            participants.append({"person_id": identity, "name": name} if identity else {"name": name})
        return {"origin": origin, **({"occurred_on": args["occurred_on"]} if "occurred_on" in args else {}),
                **({"participants": participants} if "participants" in args else {})}

    def _upload(self, builder, uow, *, body, title, source, metadata=None, source_refs=()):
        """Create the upload document and, in the same plan, its first source version, registered job and queue input."""
        identity = new_id()
        stored = {"title": title, "date": builder.at[:10], "status": "pending", "source": source,
                  "by": builder.ctx.principal_id, **(metadata or {})}
        self.store.write(builder, uow, identity=identity, kind="context_upload",
            path="context/_inbox/" + builder.at[:10] + "-" + identity + ".md", body=body, metadata=stored,
            source_refs=list(source_refs), operation_name="context_upload")
        # The source is frozen from the document exactly as the store writes it (classification included), so the version's
        # digest and the digest recomputed from the stored document later agree for an unchanged source.
        written = next(change.values for change in builder.changes if change.resource.resource_type == "documents"
                       and change.resource.resource_id == identity)
        self.require_filing().admit_document(builder, uow, identity=identity, kind="context_upload", body=body,
                                             metadata=written["metadata_json"], source_refs=written["source_refs_json"])
        return identity

    def prepare_learning_intake(self, ctx, args, uow, *, metadata, source_refs):
        # Only a trusted domain caller can mark a structured learning snapshot.
        builder = self.sources.builder("context_upload", ctx, args)
        identity = self._upload(builder, uow, **args, metadata=metadata, source_refs=source_refs)
        self.sources.add_indexes(builder, uow)
        job = next(change.values["id"] for change in builder.changes if change.resource.resource_type == "jobs")
        return builder.build(), identity, job

    def prepare(self, operation, ctx, args, options, uow):
        if operation == 'context_retry' and ctx.business_id is None:
            require_owner(ctx, uow)
        self.require_filing()
        if operation == 'context_retract' and ctx.principal_kind != 'human':
            raise DomainError('forbidden', 'Only the owner retracts a Context source')
        if operation == 'context_upload' and ctx.business_id is None:
            self.require_global_upload(ctx, uow)
            if args.get('task_ids'):
                raise DomainError('validation_error', 'Task associations are per project')
        self.sources.check_global_notes(operation, ctx, args, uow, GLOBAL_NOTES)
        builder = self.sources.builder(operation, ctx, args, namespace="global" if operation == "journal_upload_to_filer" else None)
        if operation == "journal_upload_to_filer":
            require_owner(ctx, uow)
            source = self.store.get(ctx, args["document_id"], uow, kinds=JOURNAL_KINDS, write=True)
            require_revision(source, options.expected_revision)
            raw = self.store.read_bytes(source, uow)
            digest = hashlib.sha256(raw).hexdigest()
            if (digest != source["content_hash"].removeprefix("sha256:")
                    or digest != args["expected_content_hash"].removeprefix("sha256:")):
                raise DomainError("revision_conflict", "Journal entry changed; save and reload it before uploading")
            file_meta, original_body = split_document(raw)
            note_meta = {**source["metadata_json"], **file_meta}
            body = filing_snapshot(original_body, note_meta)
            self._upload(builder, uow, body=body, title=note_meta.get("title") or "Daily",
                source="global-journal",
                metadata={"format": "notes-v1", "journal_source_id": source["id"],
                    "journal_source_kind": source["kind"], "journal_source_business_id": source["business_id"],
                    "journal_source_revision": source["revision"], "journal_source_hash": digest,
                    "journal_source_date": note_meta.get("date"), "journal_uploader_id": ctx.principal_id,
                    "journal_uploaded_at": builder.at},
                source_refs=[{"resource_type": "documents", "resource_id": source["id"]}])
        elif operation in {"context_upload", "notes_upload"}:
            source = self.store.get(ctx, args["document_id"], uow, kinds={"business_note", "journal", BUSINESS_NOTE}, write=True) if operation == "notes_upload" else None
            encounter = {}
            if source:
                self.sources.note_target(ctx, args, source)
                require_revision(source, options.expected_revision)
                file_meta, original_body = split_document(self.store.read_bytes(source, uow))
                note_meta = {**source["metadata_json"], **file_meta}
                digest = snapshot_hash(original_body, note_meta)
                if note_meta.get("upload_snapshot_hash") == digest:
                    raise DomainError("conflict", "This version is already submitted; use Retry if filing failed")
                body = filing_snapshot(original_body, note_meta)
                title = note_meta.get("title", "Untitled")
            else:
                body, title = args["body"], args.get("title", "Context upload")
                encounter = self._encounter(builder, uow, args)
            identity = self._upload(builder, uow, body=body, title=title,
                source=args.get("source", "notes" if source else "upload"),
                metadata={"format": "notes-v1", "source_revision": source["revision"], "source_snapshot_hash": digest} if source else encounter,
                source_refs=[{"resource_type": "documents", "resource_id": source["id"]}] if source else [])
            if source:
                self.store.write(builder, uow, kind=BUSINESS_NOTE if source["kind"] == BUSINESS_NOTE else "business_note", before=source, metadata={**note_meta, "uploaded": builder.at, "upload_id": identity, "upload_snapshot_hash": digest},
                    expected_revision=options.expected_revision, expected_content_hash=args.get("expected_content_hash"))
        elif operation == "context_retract":
            row = self._upload_row(ctx, args["upload_id"], uow)
            if options.expected_revision is not None:
                require_revision(row, options.expected_revision)
            accepted = [item for item in context_filing.admission.versions(uow, "document", row["id"]) if item["operation"] != "remove"
                        and item["input_state"] == "published"]
            if not accepted:
                raise DomainError("invalid_transition", "This upload has no published source version to retract; a pending "
                                  "input is discarded with context_queue_discard")
            self.filing.admit_document(builder, uow, identity=row["id"], kind=row["kind"], body=None, metadata=row["metadata_json"],
                                       retracts=accepted[-1]["source_revision"])
        elif operation == "context_retry":
            row = self._upload_row(ctx, args["upload_id"], uow)
            if options.expected_revision is not None:
                require_revision(row, options.expected_revision)
            builder.expect(ref("documents", row["id"], ctx.business_id), row)
            self.filing.retry(builder, uow, ctx, row, options, append_plan)
            if not builder.changes:
                return builder.build()
        elif operation == RECEIPT_OPERATION:
            self.filing.prepare_receipt(builder, uow, args)
            return builder.build()
        else:
            raise DomainError("operation_unavailable", "Document operation is not implemented")
        return self.sources.finalize(builder, uow, operation, args)

    def validate_targets(self, ctx, plan, uow):
        if plan.operation_name == "context_upload" and ctx.business_id is None:
            self.require_global_upload(ctx, uow)
        self.sources.validate_targets(ctx, plan, uow, global_note_operations=GLOBAL_NOTES)

    def apply(self, ctx, plan, uow):
        if plan.operation_name == RECEIPT_OPERATION:
            apply_records(ctx, plan, uow)
            self.sources.refresh(plan, uow)
            stored = context_filing.completion.receipt(uow, plan.arguments["receipt_id"])
            return {"receipt": {"id": stored["id"], "filing_id": stored["filing_id"], "outcome": stored["outcome"],
                                "state": stored["state"]}, "application": stored["application_json"]}
        result = self.sources.apply(ctx, plan, uow)
        jobs = [change for change in plan.changes if change.resource.resource_type == "jobs"]
        if plan.operation_name in {"context_retry", "context_retract"}:
            # The document already exists with its file, so its applicable filing (the new version when this plan admitted
            # one) can be read now. A brand-new upload has no file until its effect runs, so it is read separately.
            requested_id = plan.arguments.get("document_id", plan.arguments.get("upload_id"))
            row = uow.repository("documents", business_id=ctx.business_id).get(requested_id, required=True)
            result["filing"], _ = self.filing.document_filing(uow, ctx, row)
            if not jobs and result["filing"]["job_id"]:
                state = uow.repository("jobs", business_id=ctx.business_id).get(result["filing"]["job_id"], required=True)["state"]
                result["job"] = {"id": result["filing"]["job_id"], "state": state}
                return OperationResult("", "accepted", data=result, job=result["job"])
        if jobs:
            result["job"] = {"id": jobs[-1].resource.resource_id, "state": "queued"}
            return OperationResult("", "accepted", data=result, job=result["job"])
        return result
